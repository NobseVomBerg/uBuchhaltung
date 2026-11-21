# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: document_readings – die Lesung eines Belegs (Tabelle ``DocumentReadings``, seit 2.6.0).

Die Lesung sind die aus der Datei eines Belegs gelesenen Felder, getrennt von dem, was der Nutzer am Beleg pflegt
(``Documents``): abgeleitete Daten, die sich jederzeit neu lesen lassen. Darum gehen ihre Schreibwege nicht ins SQL-Replay-Log
(``_log_sql``), und sie zählt nie als Verweis, der das Löschen eines Belegs sperrt (``delete_receipt`` nimmt sie mit).

Dazu die Vorschläge zu einem Beleg: ``receipt_match_candidates`` (die Kandidaten, EINE lesende Abfrage) und ``suggest_bookings_for_receipt``
(Abfrage und Rangfolge aus ``db/receipt_matching.py``; leer, solange der Beleg an einer Buchung hängt: ``receipt_is_linked``). Sie
verknüpfen nichts."""
import sqlite3

from .bookings import RELATION_DEFAULT
from .invoice_payments import PAYMENT_MARK
from .matching import MIRROR_FORMS
from .receipt_matching import MAX_CANDIDATES, candidate_window, rank_candidates, usable_gross

#: Die Spalten einer Lesung außer ``ID`` und ``Document_ID``, in der Reihenfolge der Tabelle. Die einzige Quelle von
#: Spaltennamen im SQL-Text von ``save_document_reading`` – Schlüssel aus ``row`` gehen nie dorthin.
READING_COLUMNS = ('Status', 'Source', 'ReaderVersion', 'ReadAt', 'DocDate', 'GrossMinor', 'NetMinor', 'TaxMinor', 'TaxRate',
                   'Currency', 'SenderName', 'SenderVatId', 'InvoiceNumber', 'Kind', 'Raw', 'Text')

# Schreiben einer Lesung (Platzhalter: ``Document_ID``, dann die Spalten in der Reihenfolge von ``READING_COLUMNS``). Der Text
# entsteht einmal aus der festen Liste oben – nie aus Werten der Anfrage.
_UPSERT = ('INSERT INTO DocumentReadings (Document_ID, ' + ', '.join(READING_COLUMNS) + ') VALUES (?' + ', ?' * len(READING_COLUMNS)
           + ') ON CONFLICT(Document_ID) DO UPDATE SET ' + ', '.join(f'{column} = excluded.{column}' for column in READING_COLUMNS))

#: So viele Belege liefert ``unread_receipt_ids`` höchstens je Aufruf.
UNREAD_LIMIT_MAX = 1000

# Hängt der Beleg an irgendeiner Buchung (gleich welcher Art der Verknüpfung)? Platzhalter: die ID des Belegs.
_RECEIPT_IS_LINKED = 'SELECT 1 FROM BookingDocuments WHERE Document_ID = ? LIMIT 1'

# Ungelesen: Der Beleg hat eine Datei, und es gibt keine Lesung oder eine eines älteren Lesers (Platzhalter: dessen Version).
_UNREAD_FROM_WHERE = ("FROM Documents d LEFT JOIN DocumentReadings r ON r.Document_ID = d.ID "
                      "WHERE TRIM(COALESCE(d.Path, '')) <> '' AND (r.ID IS NULL OR r.ReaderVersion < ?)")

# Kandidaten für einen Beleg (``receipt_match_candidates``). Die Platzhalter sind ``?``: Die Bedingung der Forderung
# (``Database._receivable_sql``, dieselbe wie im Editor, ``_unlinked_entry_sql``) liefert positionelle Parameter – alle Parameter
# stehen deshalb in der Reihenfolge ihrer Platzhalter im Text, und die Methode hängt sie in genau dieser Reihenfolge an. Der Text entsteht
# aus festen Teilen, der festen Zahl der Spiegel-Formen und dem Baustein der Forderung – nie aus einem Wert der Anfrage.
#
# Gleicher Betrag ohne Vorzeichen; eine Bankbewegung oder ein eigenständiger Satz (kein Kind einer Bankbewegung, kein Zahlungssatz einer
# Rechnungszuordnung, kein Spiegel – weder markiert noch mit Spiegel-Form; eine Zeile ohne Form gilt nicht als Spiegel –, keine
# Forderungsbuchung einer eigenen Rechnung); weder die Zeile noch eines ihrer Kinder ist schon mit diesem Beleg verknüpft.
# ``linked``: die Zeile selbst oder eines ihrer Kinder trägt schon einen Beleg der Art ``receipt`` (je ein EXISTS: so nutzt jede Hälfte
# einen Index). Kontaktname: der Anzeigename, sonst der Firmenname; ``CompanyDetails.ContactID`` ist der Primärschlüssel, der Join liefert
# je Kontakt höchstens eine Zeile und verdoppelt keine Buchung.
_MIRROR_MARKS = ', '.join('?' * len(MIRROR_FORMS))
_CANDIDATES_BEFORE_RECEIVABLE = f'''
    SELECT b.ID, b.DateBooking, b.Amount, b.Currency, b.RecipientClient, b.Text, b.DocumentNumber, b.BookingType,
           b.ParentBooking_ID, COALESCE(NULLIF(TRIM(c.DisplayName), ''), cd.CompanyName, ''),
           EXISTS (SELECT 1 FROM BookingDocuments r WHERE r.Booking_ID = b.ID AND r.RelationType = ?)
           OR EXISTS (SELECT 1 FROM Bookings k JOIN BookingDocuments r ON r.Booking_ID = k.ID
                      WHERE k.ParentBooking_ID = b.ID AND r.RelationType = ?)
    FROM Bookings b
    LEFT JOIN Contacts c ON c.ID = b.Contact_ID
    LEFT JOIN CompanyDetails cd ON cd.ContactID = c.ID
    WHERE ABS(b.Amount) = ?
      AND (b.BookingType = 'bank'
           OR (b.BookingType = 'entry' AND b.ParentBooking_ID IS NULL AND COALESCE(b.Status, '') <> ?
               AND COALESCE(b.AutoMirror, 0) = 0
               AND (b.EntryForm IS NULL OR b.EntryForm NOT IN ({_MIRROR_MARKS}))
               AND NOT '''
_CANDIDATES_AFTER_RECEIVABLE = '''))
      AND NOT EXISTS (SELECT 1 FROM BookingDocuments d WHERE d.Booking_ID = b.ID AND d.Document_ID = ?)
      AND NOT EXISTS (SELECT 1 FROM Bookings k JOIN BookingDocuments d ON d.Booking_ID = k.ID
                      WHERE k.ParentBooking_ID = b.ID AND d.Document_ID = ?)'''
_CANDIDATES_FROM = ' AND b.DateBooking >= ?'
_CANDIDATES_TO = ' AND b.DateBooking <= ?'
_CANDIDATES_ORDER = ' ORDER BY b.DateBooking, b.ID LIMIT ?'


class DocumentReadingsMixin:
    def save_document_reading(self, document_id, expected_path, row):
        """Die Lesung eines Belegs schreiben (eine neue Zeile, oder die vorhandene wird ersetzt: je Beleg höchstens eine).
        Returns ``True`` – oder ``False``, ohne etwas zu schreiben, wenn es den Beleg nicht mehr gibt oder sein Pfad nicht
        mehr ``expected_path`` ist (``None`` und ``''`` gelten als gleich): Der Leser hat dann eine andere Datei gelesen, als
        der Beleg jetzt trägt. Prüfung und Schreiben laufen in einer Transaktion.

        ``row`` trägt die Spalten aus ``READING_COLUMNS``; was fehlt, wird NULL (NOT NULL sind ``Status``, ``ReaderVersion``
        und ``ReadAt``: ohne sie lehnt die Datenbank ab). Ein anderer Schlüssel ist ein ``ValueError``, bevor irgendetwas
        geschieht."""
        unknown = sorted(str(key) for key in row if key not in READING_COLUMNS)
        if unknown:
            raise ValueError('Unbekannte Spalten der Lesung: ' + ', '.join(unknown))
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            found = conn.execute('SELECT Path FROM Documents WHERE ID = ?', (document_id,)).fetchone()
            if found is None or (found[0] or '') != (expected_path or ''):
                conn.rollback()
                return False
            conn.execute(_UPSERT, (document_id, *(row.get(column) for column in READING_COLUMNS)))
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_document_reading(self, document_id):
        """Die Lesung eines Belegs mit allen Spalten (Schlüssel = Spaltennamen, auch ``ID`` und ``Document_ID``) – oder
        ``None``, wenn es keine gibt."""
        conn = self._get_connection()
        try:
            conn.row_factory = sqlite3.Row
            found = conn.execute('SELECT * FROM DocumentReadings WHERE Document_ID = ?', (document_id,)).fetchone()
            return None if found is None else dict(found)
        finally:
            conn.close()

    def document_reading_summaries(self):
        """Die Lesungen aller Belege in EINER Abfrage, für die Liste der Belege: ``{ID des Belegs: (Status, Brutto in Minor
        Units, Währung, Absender, Version des Lesers)}`` – Belege ohne Lesung fehlen. Liest nie ``Text`` oder ``Raw``."""
        conn = self._get_connection()
        try:
            return {document_id: (status, gross, currency, sender, reader_version)
                    for document_id, status, gross, currency, sender, reader_version in conn.execute(
                        'SELECT Document_ID, Status, GrossMinor, Currency, SenderName, ReaderVersion FROM DocumentReadings')}
        finally:
            conn.close()

    def unread_receipt_ids(self, reader_version, limit=100):
        """Die IDs der Belege, die noch gelesen werden müssen, nach ID: Der Beleg hat eine Datei (``Path`` nicht leer), und es
        gibt keine Lesung oder eine mit einer älteren ``ReaderVersion`` als ``reader_version``. Höchstens ``limit``, begrenzt
        auf 1 bis ``UNREAD_LIMIT_MAX``."""
        count = max(1, min(int(limit), UNREAD_LIMIT_MAX))
        conn = self._get_connection()
        try:
            return [row[0] for row in conn.execute(
                f'SELECT d.ID {_UNREAD_FROM_WHERE} ORDER BY d.ID LIMIT ?', (int(reader_version), count))]
        finally:
            conn.close()

    def count_unread_receipts(self, reader_version):
        """Wie viele Belege noch gelesen werden müssen (dieselbe Bedingung wie ``unread_receipt_ids``, ohne Grenze)."""
        conn = self._get_connection()
        try:
            return conn.execute(f'SELECT COUNT(*) {_UNREAD_FROM_WHERE}', (int(reader_version),)).fetchone()[0]
        finally:
            conn.close()

    def own_identity(self):
        """Namen und USt-IdNrn. der eigenen Firma(en) als ``(Namen, USt-IdNrn.)`` – woran ein Leser auf einem Beleg die eigene
        Seite erkennt. Eigen ist, was die App sonst auch so nennt (``ContactType = 'own'``, wie bei ``get_datev_numbers``).

        Namen: ``DisplayName`` und Firmenname, getrimmt, nicht leer, ohne Dubletten. USt-IdNrn.: nur Angaben, die
        ``export.einvoice_model.tax_id_kind`` als USt-IdNr. liest – in deren bereinigter Schreibweise (``DE-123 456 789``
        wird ``DE123456789``); eine Steuernummer im selben Feld zählt nicht."""
        from export.einvoice_model import tax_id_kind        # ``export`` importiert diese Schicht: erst zur Laufzeit
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT c.DisplayName, cd.CompanyName, cd.TaxID FROM Contacts c "
                "LEFT JOIN CompanyDetails cd ON cd.ContactID = c.ID WHERE c.ContactType = 'own' ORDER BY c.ID").fetchall()
        finally:
            conn.close()
        names, vat_ids = [], []
        for display_name, company_name, tax_id in rows:
            for name in (display_name, company_name):
                name = '' if name is None else str(name).strip()
                if name and name not in names:
                    names.append(name)
            kind, number = tax_id_kind(tax_id)
            if kind == 'vat' and number not in vat_ids:
                vat_ids.append(number)
        return names, vat_ids

    def receipt_match_candidates(self, document_id, gross_minor, date_from, date_to, limit=MAX_CANDIDATES):
        """Die Buchungen, die zu einem Beleg passen könnten – eine Abfrage, rein lesend, auf EINER Verbindung (davor liest
        ``_receivable_sql`` darauf noch das Debitorenkonto und die Zahlungskonten). Jede Zeile ist ein ``dict`` mit ``id``, ``date``,
        ``amount`` (Minor Units), ``text``, ``recipient``, ``number`` (Beleg-Nr.; roh, auch ``None``), ``contact_name`` (der
        Anzeigename des Kontakts der Buchung, ist er leer sein Firmenname, ohne Kontakt ``''``), ``linked`` (die Zeile oder eines ihrer
        Kinder trägt schon einen Beleg der Art ``receipt``) und ``row``: das Tupel in der Form von ``search_bookings`` (``ID,
        DateBooking, Amount, Currency, RecipientClient, Text, DocumentNumber, BookingType, ParentBooking_ID``), damit
        ``server.receipt_links.booking_payload`` es unverändert nimmt. Das ist genau die Eingabe von
        ``receipt_matching.rank_candidates``.

        Ein Treffer hat denselben Betrag ohne Vorzeichen wie ``gross_minor`` und ein Buchungsdatum von ``date_from`` bis ``date_to``
        (beide ISO-Daten, beide Tage gehören dazu; ``None``: diese Grenze entfällt – ohne beide gibt es keine Datumsbedingung). Er ist
        eine Bankbewegung oder ein eigenständiger Buchungssatz: nie das Kind einer Bankbewegung (dessen Betrag ist ein Teil), nie der
        Zahlungssatz einer Rechnungszuordnung (``PAYMENT_MARK``), nie ein Spiegel (``AutoMirror`` oder eine der ``MIRROR_FORMS``), nie
        die Forderungsbuchung einer eigenen Rechnung (``receivable_condition``, auch die Gegenbuchung einer Stornorechnung: sie gehört
        zur Rechnung, nicht zu einem Beleg; derselbe Baustein wie bei den Kandidaten des Editors) – und nie eine Buchung, bei der
        schon DIESER Beleg an der Zeile oder an einem ihrer Kinder hängt (gleich welcher Art der Verknüpfung). Ein Satz mit
        ``Status='resolved'`` bleibt Kandidat. Älteste zuerst, dann nach ID, höchstens ``limit`` (begrenzt auf 1 bis
        ``MAX_CANDIDATES``). Alle Werte gehen als Parameter in die Abfrage."""
        conn = self._get_connection()
        try:
            receivable_sql, receivable_params = self._receivable_sql(conn.cursor(), alias='b')
            sql = _CANDIDATES_BEFORE_RECEIVABLE + receivable_sql + _CANDIDATES_AFTER_RECEIVABLE
            # in der Reihenfolge der Platzhalter: zweimal die Art der Verknüpfung (``linked``), Betrag, Zahlungsmarke, Spiegel-Formen,
            # die Forderung, zweimal der Beleg (Zeile, Kinder) – danach die Grenzen des Fensters und die Zahl der Treffer
            params = [RELATION_DEFAULT, RELATION_DEFAULT, abs(int(gross_minor)), PAYMENT_MARK, *MIRROR_FORMS, *receivable_params,
                      document_id, document_id]
            if date_from is not None:
                sql += _CANDIDATES_FROM
                params.append(date_from)
            if date_to is not None:
                sql += _CANDIDATES_TO
                params.append(date_to)
            params.append(max(1, min(int(limit), MAX_CANDIDATES)))
            rows = conn.execute(sql + _CANDIDATES_ORDER, params).fetchall()
        finally:
            conn.close()
        return [{'id': row[0], 'date': row[1], 'amount': row[2], 'text': row[5], 'recipient': row[4], 'number': row[6],
                 'contact_name': row[9], 'linked': bool(row[10]), 'row': tuple(row[:9])} for row in rows]

    def receipt_is_linked(self, document_id):
        """Hängt der Beleg an mindestens einer Buchung (gleich welcher Art der Verknüpfung)? Rein lesend."""
        conn = self._get_connection()
        try:
            return conn.execute(_RECEIPT_IS_LINKED, (document_id,)).fetchone() is not None
        finally:
            conn.close()

    def suggest_bookings_for_receipt(self, document_id):
        """Die Buchungen, die zu einem Beleg vorgeschlagen werden – rein lesend, verknüpft wird nichts. Returns ``[(Suggestion,
        Kandidat), …]`` in der Rangfolge von ``receipt_matching.rank_candidates`` (der Kandidat ist das ``dict`` von
        ``receipt_match_candidates``, samt ``row``).

        Vorgeschlagen wird nur, solange der Beleg an keiner Buchung hängt: Hat er eine Zeile in ``BookingDocuments`` (gleich welcher
        Art), ist die Liste leer – das wird auf der Verbindung geprüft, die auch den Beleg liest, bevor Kandidaten gelesen werden. Nach dem
        Lösen der letzten Verknüpfung kommen die Vorschläge wieder.

        Maßgebliches Datum ist das gelesene (``DocDate``), sonst das Belegdatum (``Documents.Date``) – das erste, das ein ISO-Datum ist;
        daraus ergibt sich das Fenster (``candidate_window``). Leer bleibt die Liste ohne Beleg, ohne Lesung, ohne brauchbaren
        Bruttobetrag (``usable_gross``: Status ``ok``, Währung leer oder ``EUR``, Betrag ungleich 0) und ohne ein Datum für das Fenster."""
        conn = self._get_connection()
        try:
            receipt = conn.execute('SELECT Date FROM Documents WHERE ID = ?', (document_id,)).fetchone()
            if receipt is not None and conn.execute(_RECEIPT_IS_LINKED, (document_id,)).fetchone() is not None:
                return []
        finally:
            conn.close()
        reading = None if receipt is None else self.get_document_reading(document_id)
        gross = usable_gross(reading)
        if gross is None:
            return []
        doc_date = window = None
        for day in (reading['DocDate'], receipt[0]):
            window = candidate_window(day)
            if window is not None:
                doc_date = day
                break
        if window is None:
            return []
        candidates = self.receipt_match_candidates(document_id, gross, *window)
        by_id = {candidate['id']: candidate for candidate in candidates}
        return [(suggestion, by_id[suggestion.booking_id]) for suggestion in rank_candidates(reading, candidates, doc_date)]
