# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Import aus der WISO-Mein-Büro-Datenbank.

Gegenstück zu :mod:`db.wiso_import` (CSV/Text), aber aus der Firebird-Datei
selbst. Der Unterschied ist nicht die Menge, sondern die Genauigkeit: die
Datenbank nennt mit ``ACCOUNTINGID`` ausdrücklich, welche Teilbuchungen
zusammengehören. Sie landet in ``Bookings.SourceGroup`` und ersetzt dort das
Raten über Beleg-Nr. und Datum.

Das Lesen der Datei erledigt :mod:`importers.wiso_fdb`; hier steht nur die
Abbildung auf ``Bookings``, ``Assets`` und ``AssetDepreciations``.

CSV-Import und Datenbank-Import lassen sich nacheinander benutzen. Grundsatz: Was
der CSV-Import schon angelegt hat (``entry`` ohne ``SourceGroup``), übernimmt dieser
Import nur, wenn es nach Schlüssel (Beleg-Nr., Datum, Konto, Betrag) **und Text**
derselbe Satz ist und das Gegenkonto nicht widerspricht – die Buchung bekommt die
``SourceGroup`` und wird nicht ein zweites Mal angelegt. Im Zweifel legt er neu an
und meldet ähnliche vorhandene Buchungen als möglicherweise doppelt (Regeln:
:meth:`WisoFdbImportMixin._insert_wiso_bookings`).
"""
import datetime
from collections import namedtuple

from db.core import coa_id
from db.matching import (
    FORM_MIRROR_COA, FORM_MIRROR_LEGACY, LEGACY_CASH_NUMBERS, LEGACY_CASH_RANGE, MIRROR_FORMS,
)
from money import from_minor, to_minor

#: uBuchhaltung rechnet in SKR04.
SKR04 = 4

#: So viele übernommene und so viele möglicherweise doppelte Buchungen nennt das
#: Ergebnis einzeln; die Zähler zählen alle.
MAX_ADOPTED_ROWS = 500

#: Eine Buchung des CSV-Imports als Kandidat; Beleg-Nr. und Text sind für den
#: Vergleich schon aufbereitet (Leerraum zusammengefasst, Text ohne Groß-/Kleinschreibung),
#: ``shown`` ist der gespeicherte Text, ``form`` die gespeicherte Form (``Bookings.EntryForm``).
_CsvRow = namedtuple('_CsvRow', 'id doc counter text shown form')

#: SKR04-Konten 1000–1099 (Vorräte): Ältere Versionen haben sie als Kasse gelesen.
STOCK_RANGE = LEGACY_CASH_NUMBERS


def _squash(value):
    """Leerraum zusammenfassen: „RE  12“ und „RE 12“ gelten als dieselbe Beleg-Nr."""
    return ' '.join((value or '').split())


def _comparable(value):
    """Text für den Vergleich: Leerraum zusammengefasst, Groß-/Kleinschreibung egal.

    Das Auslassungszeichen „…“ zählt als Leerraum: In der WISO-Datenbank ist es das Byte
    0x85, das als latin-1 gelesen ein Zeilentrenner (U+0085) ist und schon beim Lesen der
    Datenbank wie Leerraum behandelt wird – der Text kommt von dort ohne „…“ an."""
    value = (value or '').replace('\u2026', ' ').replace('\x85', ' ')
    return _squash(value).casefold()


def _as_csv_text(value):
    """Text eines Datenbank-Satzes in der Lesart der CSV – nur für den Vergleich.

    Die CSV wird als cp1252 gelesen, die WISO-Datenbank als latin-1: „€“, „–“, „…“ und
    andere Zeichen aus 0x80–0x9F kämen von dort als Steuerzeichen an und glichen nie dem
    Text der CSV-Zeile. Vor dem Zusammenfassen des Leerraums anwenden (U+0085 gälte
    sonst als Leerraum). Am gespeicherten Text ändert sich nichts; was sich nicht nach
    latin-1 kodieren lässt, bleibt, wie es ist.
    """
    value = value or ''
    try:
        return value.encode('latin-1').decode('cp1252', 'replace')
    except UnicodeEncodeError:
        return value


def _matching_row(rows, counter_coa, text, locked=False, fallback=False):
    """Die Zeile des CSV-Imports, die zu einem WISO-Satz passt – oder ``None``.

    ``rows`` haben schon dieselbe Beleg-Nr. und sind nach ID sortiert; ``text`` ist
    die Vergleichsform des WISO-Satzes. Der Text muss immer gleich und nicht leer
    sein – zwei leere Texte beweisen nichts. Dazu das Gegenkonto: gleich (beide
    bekannt) oder die Zeile hat keines. Ein anderes Gegenkonto passt nie. Gleiches
    Gegenkonto geht vor, dann die kleinste ID.

    ``locked`` (mehrdeutiger Schlüssel) und ``fallback`` (Ersatzsuche ohne Konto)
    lassen nur das gleiche Gegenkonto zu.
    """
    if not text:
        return None
    open_row = None
    for row in rows:
        if row.text != text:
            continue
        if row.counter is not None:
            if row.counter == counter_coa:
                return row
        elif open_row is None and not (locked or fallback):
            open_row = row
    return open_row


class WisoFdbImportMixin:
    """Buchungssätze und Anlagen aus einer ``.FDB``-Datei übernehmen."""

    def import_wiso_fdb(self, path, standard_chart_path=None,
                        with_assets=True, with_invoices=True) -> dict:
        """WISO-Mandantendatenbank importieren.

        Args:
            path: ``DB1.FDB`` des Mandanten.
            standard_chart_path: ``DB0.FDB`` – liefert Konten, die im
                Mandantenrahmen fehlen. Optional.
            with_assets: Anlagenverzeichnis samt AfA-Plan mitnehmen.
            with_invoices: Kunden und Ausgangsrechnungen samt Positionen
                mitnehmen. Die Kundennummern bleiben die aus WISO.

        Returns:
            dict mit ``imported``, ``skipped``, ``adopted`` (Buchungen aus dem
            CSV-Import, die wiedererkannt statt neu angelegt wurden),
            ``adopted_rows`` (davon die ersten 500 mit ``id``, ``date``,
            ``amount`` in Euro und dem ``text`` des WISO-Satzes),
            ``possible_duplicates_count`` (vorhandene Buchungen, die einem neu
            angelegten Satz in Datum, Konto und Betrag gleichen, aber nicht
            übernommen wurden), ``possible_duplicates`` (davon die ersten 500
            mit ``id``, ``date``, ``amount`` in Euro und ``text`` der vorhandenen
            Buchung), ``assets``, ``depreciations``, ``tax_rows_skipped``,
            ``memo_rows_skipped``, ``missing_coa`` und ``errors``. Dazu
            ``negative_gross`` (Sätze mit negativem Bruttobetrag, deren Vorzeichen der Import
            ersetzt hat: bei genau einer liquiden Seite richtet es sich nach der Geldbewegung, ein
            Storno oder eine Gutschrift wird so wie eine normale Buchung gelesen),
            ``stock_range_accounts`` (Konten 1000–1099 in den Sätzen: ältere Versionen haben sie
            als Kasse gelesen; je Konto ``nummer``, ``bezeichnung``, ``anzahl`` der Sätze),
            ``legacy_mirrors`` (Spiegel der Datenbank, die als schon vorhandene Spiegel des
            Bestands wiedererkannt wurden: Sie stehen noch in der alten Lesart, ihre Form und
            ihr Betrag bleiben unverändert) und ``form_conflicts_count``/``form_conflicts``
            (schon importierte Sätze, deren gespeicherte Form von der des frischen Imports
            abweicht: nur gemeldet, die ersten 500 mit ``id``, ``date``, ``amount``, ``text``,
            ``stored`` und ``fresh``).
        """
        from importers.wiso_fdb import WisoDatabase

        result = {'imported': 0, 'skipped': 0, 'adopted': 0, 'adopted_rows': [],
                  'possible_duplicates_count': 0, 'possible_duplicates': [],
                  'negative_gross': 0, 'stock_range_accounts': [], 'legacy_mirrors': 0,
                  'form_conflicts_count': 0, 'form_conflicts': [],
                  'assets': 0, 'depreciations': 0, 'tax_rows_skipped': 0,
                  'memo_rows_skipped': 0, 'missing_coa': {}, 'created_coa': {},
                  'unresolved_accounts': 0, 'blocker': [], 'hints': [],
                  'asset_warnings': [], 'customers': 0, 'invoices': 0,
                  'invoice_items': 0, 'invoices_skipped': 0,
                  'invoice_warnings': [], 'payments_linked': 0,
                  'payment_warnings': [], 'errors': []}
        try:
            wiso = WisoDatabase(path, standard_chart_path)
        except Exception as exc:                       # defekte/fremde Datei
            result['errors'].append(f'{path}: {exc}')
            return result

        with wiso:
            blocker, hints = wiso.check()
            result['hints'] = hints
            if blocker:
                result['blocker'] = blocker
                result['errors'] += blocker
                return result
            data = wiso.read(self._liquid_skr_accounts())
            result['tax_rows_skipped'] = data.tax_rows_skipped
            result['memo_rows_skipped'] = data.memo_rows_skipped
            result['missing_coa'] = {
                number: {'anzahl': count,
                         'bezeichnung': data.unmapped_labels.get(number, '')}
                for number, count in sorted(data.unmapped_accounts.items(),
                                            key=lambda kv: -kv[1])}
            result['created_coa'] = self._create_missing_coa(data)
            result['negative_gross'] = data.negative_gross
            result['stock_range_accounts'] = self._stock_range_accounts(data.bookings)
            self._insert_wiso_bookings(data.bookings, result,
                                       self._recipients_by_invoice(data))
            if with_assets:
                self._insert_wiso_assets(data.assets, result)
            if with_invoices:
                contacts = self._insert_wiso_customers(data.customers, result)
                self._insert_wiso_invoices(data, contacts, result)
        return result

    def link_wiso_invoice_payments(self, path, standard_chart_path=None) -> dict:
        """Zahlungen den Rechnungen zuordnen – **nach** dem Auto-Abgleich.

        Eigener Schritt und nicht Teil von :meth:`import_wiso_fdb`, weil die
        Reihenfolge zählt: verknüpft werden soll die Bankbewegung, und die
        hängt erst nach ``link_bank_to_entries`` an den Buchungssätzen. Vorher
        aufgerufen, landet die Zahlung am Buchungssatz – nicht falsch, aber
        weniger nützlich, und ein zweiter Lauf korrigiert es nicht mehr.
        """
        from importers.wiso_fdb import WisoDatabase

        result = {'payments_linked': 0, 'payment_warnings': [], 'errors': []}
        try:
            wiso = WisoDatabase(path, standard_chart_path)
        except Exception as exc:
            result['errors'].append(f'{path}: {exc}')
            return result
        with wiso:
            self._link_wiso_invoice_payments(wiso.read(self._liquid_skr_accounts()),
                                             result)
        return result

    # ------------------------------------------------------------------
    def _liquid_skr_accounts(self):
        """SKR-Nummern der eigenen Zahlungskonten – sie bestimmen das Vorzeichen."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                'SELECT SKRAccount FROM Accounts WHERE SKRAccount IS NOT NULL'
            ).fetchall()
        finally:
            conn.close()
        return {row[0] for row in rows}

    def _coa_map(self, cursor):
        cursor.execute('SELECT AccountNumber, ID FROM ChartOfAccounts')
        return {row[0]: row[1] for row in cursor.fetchall()}

    def _stock_range_accounts(self, bookings):
        """Konten 1000–1099 (SKR04: Vorräte) in den Sätzen: ``[{'nummer', 'bezeichnung',
        'anzahl'}]`` nach Nummer. Ältere Versionen haben sie als Kasse gelesen – ein Satz
        darauf kann eine Kasse meinen. Die Bezeichnung steht im Kontenrahmen (nach
        ``_create_missing_coa``)."""
        counts = {}
        for booking in bookings:
            for number in {booking.account, booking.counter_account}:
                if number in STOCK_RANGE:
                    counts[number] = counts.get(number, 0) + 1
        if not counts:
            return []
        conn = self._get_connection()
        try:
            names = dict(conn.execute(
                'SELECT AccountNumber, Name FROM ChartOfAccounts WHERE AccountNumber BETWEEN ? AND ?',
                (LEGACY_CASH_RANGE[0], LEGACY_CASH_RANGE[1])).fetchall())
        finally:
            conn.close()
        return [{'nummer': number, 'bezeichnung': names.get(number, ''), 'anzahl': count}
                for number, count in sorted(counts.items())]

    def _create_missing_coa(self, data):
        """SKR04-Konten anlegen, die der eigene Kontenrahmen noch nicht kennt.

        Hier wird nichts geraten: Nummer **und** Bezeichnung stammen aus WISOs
        eigener Umschlüsselungstabelle. Ohne diesen Schritt verlören Buchungen
        auf Konten, die uBuchhaltung nicht mitbringt, ihre Kontierung und
        stünden in der Übersicht als offen.
        """
        wanted = {}
        for account in data.chart.values():
            if account.skr04:
                wanted.setdefault(account.skr04, account.text)
        used = {number for booking in data.bookings
                for number in (booking.account, booking.counter_account)
                if number is not None}
        used |= {number for asset in data.assets
                 for number in (asset.account, asset.depreciation_account)
                 if number is not None}

        conn = self._get_connection()
        cursor = conn.cursor()
        known = set(self._coa_map(cursor))
        created = {}
        for number in sorted(used - known):
            name = wanted.get(number) or f'SKR04 {number}'
            cursor.execute('''
                INSERT OR IGNORE INTO ChartOfAccounts
                    (ID, Framework, AccountNumber, Name, Description,
                     IsStandard, PrivateSharePercent, ShowInMenu)
                VALUES (?,?,?,?,?,0,0,1)
            ''', (coa_id(SKR04, number), SKR04, number, name[:120],
                  'aus der WISO-Datenbank übernommen'))
            created[number] = name
        conn.commit()
        conn.close()
        return created

    @staticmethod
    def _recipients_by_invoice(data):
        """Rechnungs-Id → Kundenname.

        ``MOV_FINACC_ACCRECORDS`` führt keinen Empfänger – den kennt sonst nur
        der Kontoauszug. Für rechnungsbezogene Buchungen lässt er sich aber
        ableiten: die Buchung nennt ihre Rechnung, die Rechnung den Kunden.
        """
        return {invoice.source_id: invoice.buyer_name
                for invoice in data.invoices if invoice.buyer_name}

    def _insert_wiso_bookings(self, bookings, result, recipients=None):
        """Buchungssätze schreiben; ``SourceGroup`` trägt die Split-Klammer.

        Duplikate erkennt die Quell-Id (``SourceGroup`` + Betrag + Konto reicht
        nicht, weil ein Vorgang gleichbetragige Zeilen enthalten darf): schon
        vorhandene ``SourceGroup``-Werte werden übersprungen, damit ein zweiter
        Lauf nichts verdoppelt.

        Der CSV-Import schreibt keine ``SourceGroup``. Seine Buchungen übernimmt
        dieser Import statt sie neu anzulegen (``result['adopted']``, die ersten
        500 mit Datum, Betrag und Text in ``result['adopted_rows']``) – aber nur,
        was nach Schlüssel **und Text** derselbe Satz ist. Im Zweifel wird neu
        angelegt und gemeldet: lieber eine sichtbare zweite Buchung als eine still
        falsch übernommene.

        * Kandidat ist eine ``entry`` ohne ``SourceGroup``, ohne ``AutoMirror`` und
          ohne ``Account_ID`` mit gleichem Datum, Betrag und Konto sowie gleicher
          Beleg-Nr. (Leerraum zusammengefasst). Eigene Einfügungen dieses Laufs
          sind nie Kandidaten.
        * Der Text muss immer gleich sein (Leerraum zusammengefasst, Groß-/Klein-
          schreibung egal; der Text des WISO-Satzes wird dafür von latin-1 nach
          cp1252 umgesetzt, gespeichert wird er unverändert) und darf nicht leer
          sein: Zwei leere Texte beweisen nichts, solche Sätze werden neu angelegt.
        * Das Gegenkonto muss gleich sein (beide bekannt) oder dem Kandidaten
          fehlen; ein anderes Gegenkonto passt nie. Gewählt wird zuerst der
          Kandidat mit gleichem Gegenkonto, dann der ohne, bei Gleichstand die
          kleinste ID. Jeder Kandidat wird höchstens einmal verbraucht – zwei
          gleich hohe Teilbuchungen übernehmen zwei CSV-Zeilen.
        * Mehrdeutigkeitssperre: Tragen Sätze dieses Imports mit gleichem Datum,
          Betrag, Konto, Beleg-Nr. und Text verschiedene Gegenkonten, übernehmen
          sie keinen Kandidaten ohne Gegenkonto – welcher Satz zu welcher Zeile
          gehört, ließe sich nur raten. Kandidaten mit gleichem Gegenkonto bleiben
          erlaubt.
        * Fehlte der CSV-Zeile das Konto (im Kontenrahmen unbekannt), wird
          ersatzweise ohne Konto gesucht: nur wenn es zum Schlüssel mit Konto beim
          Laden keinen Kandidaten gab (nicht, wenn sie bloß aufgebraucht sind), und
          nur bei gleichem bekanntem Gegenkonto und gleichem, nicht leerem Text.
        * Übernommen wird mit einem UPDATE (im Replay-Log): ``SourceGroup`` setzen,
          leere Konten und einen leeren Empfänger füllen, Steuersatz und
          Steuerbetrag ergänzen, wenn beide fehlen. Alles andere – Text,
          Verknüpfungen, Änderungen des Nutzers – bleibt.
        * Die Form (``Bookings.EntryForm``) schreibt der Import mit, wie er das Vorzeichen
          gesetzt hat (``WisoBooking.form``) – für Sätze, die er neu anlegt. Die Form einer
          vorhandenen Zeile ändert kein Import: Ein übernommener Spiegel des Bestands
          (``mirror_legacy``) bleibt einer, nur seine Quellgruppe wird gesetzt, und der Import
          zählt ihn (``legacy_mirrors``). Füllt die Übernahme ein fehlendes Konto, ändern sich
          die Konten des Satzes – dann gilt für ihn die Form des Imports.
        * Ein Spiegel des Bestands mit umgekehrtem Vorzeichen (``mirror_legacy``, Betrag
          −gross: Richtung auf dem Gegenkonto) wird übernommen, wenn Konto, Gegenkonto, Beleg-Nr.
          und (nicht leerer) Text gleich sind und nur das Vorzeichen abweicht, und die Datei
          nicht selbst eine Zeile mit dem Betrag dieses Spiegels enthält (dann wäre es deren
          Duplikat). Form und Betrag bleiben. Mehrdeutigkeit und „im Zweifel neu anlegen“ gelten
          wie oben.
        * Bekannte ``SourceGroup`` (Re-Import): Statt zu überspringen, wird der Satz mit der
          Zeile der Gruppe verglichen (Datum, Konto, Gegenkonto, |Betrag|). Ein
          ``mirror_legacy``-Spiegel wird gezählt (``legacy_mirrors``); weicht eine andere
          gespeicherte Form von der des frischen Imports ab, wird das nur gemeldet
          (``form_conflicts``) – nichts wird umgedeutet.
        * Möglicherweise doppelt: Hat dieser Lauf zu Datum, Betrag und Konto (bzw.
          der Ersatzsuche) einen Satz neu angelegt, werden die dazu geladenen,
          nicht übernommenen Kandidaten gemeldet (``possible_duplicates_count``,
          die ersten 500 in ``possible_duplicates``) – ohne Rücksicht auf Text und
          Beleg-Nr.
        """
        recipients = recipients or {}
        adopt_sql = '''
            UPDATE Bookings SET
                SourceGroup     = COALESCE(?, SourceGroup),
                COA_ID          = COALESCE(COA_ID, ?),
                CounterCOA_ID   = COALESCE(CounterCOA_ID, ?),
                TaxRate         = CASE WHEN TaxRate IS NULL AND TaxAmount IS NULL
                                       THEN ? ELSE TaxRate END,
                TaxAmount       = CASE WHEN TaxRate IS NULL AND TaxAmount IS NULL
                                       THEN ? ELSE TaxAmount END,
                RecipientClient = COALESCE(NULLIF(RecipientClient, ''), ?, RecipientClient),
                EntryForm       = COALESCE(?, EntryForm)
            WHERE ID = ?
        '''
        with self.transaction() as tx:
            cursor = tx.conn.cursor()
            coa_map = self._coa_map(cursor)
            cursor.execute("SELECT DISTINCT SourceGroup FROM Bookings "
                           "WHERE SourceGroup IS NOT NULL AND SourceGroup <> ''")
            known_groups = {row[0] for row in cursor.fetchall()}
            pools = {}
            created_for = set()

            def csv_rows(key):
                """Zeilen des CSV-Imports zu (Datum, Betrag, Konto) als Paar
                (Beleg-Nrn. beim Laden, noch freie Zeilen); je Schlüssel einmal
                geladen – vor den eigenen Einfügungen dieses Laufs, die so nie
                Kandidaten sind – und von den Aufrufern nacheinander verbraucht.
                Die Beleg-Nr. wird in Python verglichen, damit Leerraum nicht zählt."""
                if key not in pools:
                    cursor.execute(
                        "SELECT ID, DocumentNumber, CounterCOA_ID, Text, EntryForm FROM Bookings "
                        "WHERE BookingType = 'entry' AND COALESCE(SourceGroup, '') = '' "
                        "AND COALESCE(AutoMirror, 0) = 0 AND Account_ID IS NULL "
                        "AND DateBooking = ? AND Amount = ? AND COA_ID IS ? "
                        "ORDER BY ID", key)
                    rows = [_CsvRow(row[0], _squash(row[1]), row[2],
                                    _comparable(row[3]), row[3] or '', row[4])
                            for row in cursor.fetchall()]
                    pools[key] = ({row.doc for row in rows}, rows)
                return pools[key]

            # Einmal über alle Sätze: Schlüssel mit verschiedenen Gegenkonten sind
            # mehrdeutig (auch Sätze, die schon eine SourceGroup haben, zählen mit).
            prepared = []
            counters = {}
            accounts = {}      # dasselbe für die Ersatzsuche: gleicher Satz, verschiedene Konten
            for booking in bookings:
                coa = coa_map.get(booking.account)
                counter_coa = coa_map.get(booking.counter_account)
                amount = to_minor(booking.amount or 0)
                number = _squash(booking.document_number)
                text = _comparable(_as_csv_text(booking.text))
                key = (booking.date, amount, coa, number, text)
                counters.setdefault(key, set()).add(counter_coa)
                accounts.setdefault((booking.date, amount, number, text, counter_coa),
                                    set()).add(coa)
                prepared.append((booking, coa, counter_coa, amount, number, text, key))
            ambiguous = {key for key, seen in counters.items() if len(seen) > 1}
            # Die Ersatzsuche kennt das Konto der CSV-Zeile nicht: Unterscheiden sich
            # zwei Sätze nur im Konto (z. B. zwei gleich hohe Löhne auf zwei
            # Personenkonten), ließe sich nicht sagen, welche Zeile zu welchem gehört.
            ambiguous_accounts = {key for key, seen in accounts.items() if len(seen) > 1}

            group_pools = {}

            def group_rows(group):
                """Zeilen einer schon importierten Gruppe (Re-Import), je Gruppe einmal geladen und
                von den Aufrufern nacheinander verbraucht."""
                if group not in group_pools:
                    cursor.execute(
                        "SELECT ID, DateBooking, COA_ID, CounterCOA_ID, Amount, EntryForm, Text "
                        "FROM Bookings WHERE BookingType = 'entry' AND SourceGroup = ? "
                        "ORDER BY ID", (group,))
                    group_pools[group] = [list(row) for row in cursor.fetchall()]
                return group_pools[group]

            def note_conflict(row_id, date, minor, text, stored, fresh):
                result['form_conflicts_count'] += 1
                if len(result['form_conflicts']) < MAX_ADOPTED_ROWS:
                    result['form_conflicts'].append(
                        {'id': row_id, 'date': date, 'amount': float(from_minor(minor)),
                         'text': text or '', 'stored': stored, 'fresh': fresh})

            def deviates(stored, fresh):
                """Weicht die gespeicherte Form von der des frischen Imports ab? Zwei Spiegel
                sind sich einig – ob ihr Betrag auf dem Konto oder dem Gegenkonto steht, sagt die
                Form selbst."""
                return stored != fresh and not (stored in MIRROR_FORMS and fresh in MIRROR_FORMS)

            for booking, coa, counter_coa, amount, number, text, key in prepared:
                if booking.group and booking.group in known_groups:
                    # Re-Import: Nichts wird neu angelegt und nichts geändert. Der Satz wird mit
                    # seiner Zeile verglichen – ein Spiegel des Bestands wird gezählt, eine
                    # abweichende Form nur gemeldet.
                    pool = group_rows(booking.group)
                    same = [row for row in pool
                            if row[1] == booking.date and row[2] == coa and row[3] == counter_coa
                            and abs(row[4]) == abs(amount)]
                    row = next((r for r in same if r[4] == amount), same[0] if same else None)
                    if row is not None:
                        pool.remove(row)
                        row_id, _date, _coa, _counter, row_amount, stored, row_text = row
                        if stored == FORM_MIRROR_LEGACY and booking.form == FORM_MIRROR_COA:
                            result['legacy_mirrors'] += 1
                        elif stored and deviates(stored, booking.form):
                            note_conflict(row_id, booking.date, row_amount, row_text, stored,
                                          booking.form)
                    result['skipped'] += 1
                    continue
                if booking.account is not None and coa is None:
                    result['unresolved_accounts'] += 1
                if booking.counter_account is not None and counter_coa is None:
                    result['unresolved_accounts'] += 1

                searched = [(booking.date, amount, coa)]
                loaded, free = csv_rows(searched[0])
                match = _matching_row([row for row in free if row.doc == number],
                                      counter_coa, text, locked=key in ambiguous)
                found_without_account = False
                if (match is None and coa is not None and number not in loaded
                        and (booking.date, amount, number, text, counter_coa)
                        not in ambiguous_accounts):
                    searched.append((booking.date, amount, None))
                    free = csv_rows(searched[1])[1]
                    match = _matching_row([row for row in free if row.doc == number],
                                          counter_coa, text, fallback=True)
                    found_without_account = match is not None
                flipped = False
                if (match is None and booking.form == FORM_MIRROR_COA and coa is not None
                        and counter_coa is not None
                        and (booking.date, -amount, coa, number, text) not in counters):
                    # Ein Spiegel des Bestands mit umgekehrtem Vorzeichen: gleiche Buchung, der
                    # Betrag steht auf dem Gegenkonto. Das Gegenkonto muss gleich sein (locked).
                    free = csv_rows((booking.date, -amount, coa))[1]
                    match = _matching_row(
                        [row for row in free
                         if row.doc == number and row.form == FORM_MIRROR_LEGACY],
                        counter_coa, text, locked=True)
                    flipped = match is not None
                if match is not None:
                    free.remove(match)
                    stored = match.form
                    form_value = None
                    if not stored or found_without_account or (
                            match.counter is None and counter_coa is not None):
                        # Zeile ohne Form, oder die Übernahme füllt ein fehlendes Konto: Die Konten
                        # des Satzes ändern sich, also gilt die Form des Imports für sie
                        form_value = booking.form
                    elif stored == FORM_MIRROR_LEGACY and booking.form == FORM_MIRROR_COA:
                        result['legacy_mirrors'] += 1        # bleibt, wie sie ist
                    elif deviates(stored, booking.form):
                        note_conflict(match.id, booking.date, -amount if flipped else amount,
                                      match.shown, stored, booking.form)
                    params = (booking.group or None, coa, counter_coa,
                              booking.tax_rate, self._minor_opt(booking.tax_amount),
                              recipients.get(booking.invoice_id) or None, form_value, match.id)
                    cursor.execute(adopt_sql, params)
                    tx.log.append((adopt_sql, params, 'WISO-Datenbank-Import: '
                                                      'Buchung aus CSV-Import übernommen'))
                    result['adopted'] += 1
                    if len(result['adopted_rows']) < MAX_ADOPTED_ROWS:
                        result['adopted_rows'].append(
                            {'id': match.id, 'date': booking.date,
                             'amount': booking.amount or 0.0, 'text': booking.text})
                    continue

                cursor.execute('''
                    INSERT INTO Bookings
                        (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate,
                         TaxAmount, Text, DocumentNumber, BookingType, SourceGroup,
                         RecipientClient, EntryForm)
                    VALUES (?,?,?,?,?,?,?,?,'entry',?,?,?)
                ''', (booking.date, coa, counter_coa, amount, booking.tax_rate,
                      self._minor_opt(booking.tax_amount), booking.text,
                      booking.document_number or None, booking.group or None,
                      recipients.get(booking.invoice_id) or None, booking.form or None))
                result['imported'] += 1
                created_for.update(searched)

            # Nicht übernommene Kandidaten zu Schlüsseln, für die dieser Lauf einen
            # Satz neu angelegt hat: dieselbe Buchung zweimal? Das entscheidet der Nutzer.
            leftovers = {row.id: (key, row) for key in created_for for row in pools[key][1]}
            result['possible_duplicates_count'] = len(leftovers)
            for row_id in sorted(leftovers)[:MAX_ADOPTED_ROWS]:
                (date, amount, _coa), row = leftovers[row_id]
                result['possible_duplicates'].append(
                    {'id': row_id, 'date': date,
                     'amount': float(from_minor(amount)), 'text': row.shown})

    def _insert_wiso_customers(self, customers, result):
        """Kunden übernehmen; die Kundennummer bleibt die aus WISO.

        Returns die Zuordnung Kundennummer → Contacts.ID, die die Rechnungen
        brauchen. Vorhandene Kundennummern werden nicht angetastet – wer den
        Kontakt in uBuchhaltung schon gepflegt hat, soll ihn behalten.
        """
        conn = self._get_connection()
        try:
            known = {row[0]: row[1] for row in conn.execute(
                'SELECT CustomerNumber, ID FROM Contacts '
                'WHERE CustomerNumber IS NOT NULL')}
        finally:
            conn.close()

        for customer in customers:
            number = str(customer.number) if customer.number is not None else None
            if number is None or number in known:
                continue
            try:
                contact_id = self.insert_contact(
                    contact_type='customer', entity_type=customer.entity_type,
                    display_name=customer.display_name or None,
                    customer_number=number, email=customer.email,
                    phone=customer.phone, notes=customer.notes,
                    address_line1=customer.address_line1, street=customer.street,
                    postal_code=customer.postal_code, city=customer.city,
                    country=customer.country,
                    company_name=customer.company_name, tax_id=customer.vat_id,
                    first_name=customer.first_name, last_name=customer.last_name)
            except Exception as exc:                # z. B. doppeltes Kürzel
                result['errors'].append(
                    f'Kunde {number} ({customer.display_name}): {exc}')
                continue
            if contact_id:
                known[number] = contact_id
                result['customers'] += 1
        return known

    def _insert_wiso_invoices(self, data, contacts, result):
        """Rechnungen samt Positionen übernehmen.

        Der Absender steht in WISO nur einmal (Firmenstammdaten) und wird als
        Momentaufnahme in jede Rechnung geschrieben – so hält es auch die
        Rechnungsmaske von uBuchhaltung.

        Die Rechnungsnummer ist der Wiedererkennungsschlüssel: was es schon
        gibt, bleibt unverändert.
        """
        conn = self._get_connection()
        try:
            known = {row[0] for row in conn.execute(
                'SELECT InvoiceNumber FROM Invoices')}
            # Die eigene Firma steht als Kontakt vom Typ 'own' im Stamm; die
            # Rechnung verweist darauf und führt den Absender zusätzlich als
            # Momentaufnahme, damit spätere Änderungen alte Belege nicht ändern.
            own = conn.execute(
                "SELECT ID FROM Contacts WHERE ContactType = 'own' "
                'ORDER BY ID LIMIT 1').fetchone()
            # Bankverbindung für den Zahlungshinweis: WISO führt sie nicht an
            # der Rechnung, sondern im Briefbogen. Genommen wird das eigene
            # Geschäftskonto – ein Zahlungskonto mit IBAN, das keine Kasse ist.
            bank = conn.execute(
                'SELECT ID, BankName, Number, BIC FROM Accounts '
                "WHERE COALESCE(IsCash, 0) = 0 AND COALESCE(Number, '') != '' "
                'ORDER BY ID LIMIT 1').fetchone()
        finally:
            conn.close()
        own_company_id = own[0] if own else None
        if bank is None:
            result['invoice_warnings'].append(
                'Kein Geschäftskonto mit IBAN in der Kontenverwaltung – die '
                'Rechnungen bleiben ohne Bankverbindung')

        company = data.company
        for invoice in data.invoices:
            if invoice.number in known:
                result['invoices_skipped'] += 1
                continue
            due = None
            if invoice.date and invoice.payment_days:
                due = (datetime.date.fromisoformat(invoice.date)
                       + datetime.timedelta(days=invoice.payment_days)).isoformat()
            invoice_id = self.insert_invoice({
                'invoice_number': invoice.number,
                'invoice_date': invoice.date,
                'own_company_id': own_company_id,
                'seller_name': (company.name if company else '') or 'unbekannt',
                'seller_company': (company.company if company else '') or 'unbekannt',
                'seller_street': company.street if company else '',
                'seller_postal_code': company.postal_code if company else '',
                'seller_city': company.city if company else '',
                'seller_country': company.country if company else 'DE',
                'seller_vat_id': company.vat_id if company else '',
                'seller_email': company.email if company else '',
                'seller_phone': company.phone if company else '',
                'customer_id': contacts.get(str(invoice.customer_number)),
                'buyer_name': invoice.buyer_name or 'unbekannt',
                'buyer_company': invoice.buyer_company or '',
                'buyer_street': invoice.buyer_street,
                'buyer_postal_code': invoice.buyer_postal_code,
                'buyer_city': invoice.buyer_city,
                'buyer_country': invoice.buyer_country,
                'delivery_date': invoice.delivery_date,
                'payment_due_date': due,
                'bank_account_id': bank[0] if bank else None,
                'bank_name': bank[1] if bank else None,
                'bank_iban': bank[2] if bank else None,
                'bank_bic': bank[3] if bank else None,
                'tax_rate': invoice.tax_rate,
                'sum_net': invoice.sum_net, 'tax_amount': invoice.tax_amount,
                'sum_gross': invoice.sum_gross,
                # Bezahlt heißt: nichts mehr offen.
                'amount_due': 0 if invoice.status == 'paid' else invoice.sum_gross,
                'status': invoice.status,
                'intro_text': invoice.intro_text or None,
                'closing_text': invoice.closing_text or None,
            })
            known.add(invoice.number)
            result['invoices'] += 1
            for item in invoice.items:
                self.insert_invoice_item({
                    'invoice_id': invoice_id, 'position': item.position,
                    'description': item.description or '(ohne Bezeichnung)',
                    'quantity': item.quantity, 'unit': item.unit,
                    'price_per_unit': item.price_per_unit,
                    'total_net': item.total_net,
                    'tax_rate': item.tax_rate if item.tax_rate is not None
                    else invoice.tax_rate,
                })
                result['invoice_items'] += 1
            for warning in invoice.warnings:
                result['invoice_warnings'].append(
                    f'Rechnung {invoice.number}: {warning}')

    def _link_wiso_invoice_payments(self, data, result):
        """Zahlungen ihren Rechnungen zuordnen – über WISOs ``INVID``.

        WISO hängt an jede Buchung eines Rechnungsvorgangs die Rechnungs-Id.
        Zahlung ist davon aber nur die Zeile, die ein **Zahlungskonto**
        berührt; die Forderungsbuchung (Debitor an Erlöse) trägt dieselbe Id
        und darf nicht mitgezählt werden.

        Verknüpft wird bevorzugt die **Bankbewegung** des Vorgangs – sie ist
        das, was der Kontoauszug zeigt. Fehlt sie (für alte Jahre gibt es
        keine Auszüge), tritt der Buchungssatz selbst an ihre Stelle.

        Der Betrag ist der des Buchungssatzes, wie der Import ihn gespeichert hat: Bei genau
        einer liquiden Seite trägt er die Richtung der Geldbewegung (``WisoDatabase._signed``),
        ein negativer Bruttobetrag verliert dabei sein Vorzeichen (``negative_gross`` im
        Ergebnis des Imports). Eine stornierte und neu gebuchte Zahlung, die WISO als hin,
        zurück, hin ablegt, ist darin nicht vom Original zu unterscheiden; ``link_invoice_to_transaction``
        ordnet jedes Paar Rechnung–Bewegung höchstens einmal zu und meldet Doppeltes in
        ``payment_warnings``. Eine Gutschrift lässt sich so nicht vorzeichenrichtig zuordnen – bis das
        Vorzeichen negativer Bruttobeträge an echten Daten gemessen und festgeschrieben ist
        (``docs/aufgaben.md``), bleibt es bei dieser Lesart.
        """
        from importers.wiso_fdb import DEFAULT_LIQUID_ACCOUNTS

        liquid = set(DEFAULT_LIQUID_ACCOUNTS) | self._liquid_skr_accounts()
        numbers = {invoice.source_id: invoice.number for invoice in data.invoices}
        if not numbers:
            return

        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT InvoiceNumber, ID FROM Invoices')
        invoice_ids = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.execute("SELECT SourceGroup, ID, ParentBooking_ID, Amount "
                       "FROM Bookings WHERE SourceGroup IS NOT NULL "
                       "AND SourceGroup != '' AND BookingType = 'entry'")
        by_group = {}
        for group, booking_id, parent_id, amount in cursor.fetchall():
            by_group.setdefault(group, []).append((booking_id, parent_id, amount))
        cursor.execute('SELECT InvoiceID, BookingID FROM InvoicePayments')
        already = {tuple(row) for row in cursor.fetchall()}
        conn.close()

        for booking in data.bookings:
            number = numbers.get(booking.invoice_id)
            invoice_id = invoice_ids.get(number) if number else None
            if invoice_id is None:
                continue
            if not ({booking.account, booking.counter_account} & liquid):
                continue                       # Forderungs-/Umbuchungszeile
            target = self._payment_booking(by_group.get(booking.group, []),
                                           booking.amount)
            if target is None or (invoice_id, target) in already:
                continue
            try:
                self.link_invoice_to_transaction(invoice_id, target,
                                                 booking.amount)
            except Exception as exc:           # bereits verknüpft o. Ä.
                result['payment_warnings'].append(f'Rechnung {number}: {exc}')
                continue
            already.add((invoice_id, target))
            result['payments_linked'] += 1

    @staticmethod
    def _payment_booking(candidates, amount):
        """Aus den Buchungen einer Quellgruppe die Zahlung heraussuchen.

        Die Bankbewegung ist die richtige Verknüpfung – sie steht im
        Kontoauszug. Nur wenn keine da ist, tritt der betragsgleiche
        Buchungssatz an ihre Stelle. Verglichen wird **mit Vorzeichen**, sonst
        ließe sich eine Zahlung nicht von ihrer Stornierung unterscheiden.
        """
        minor = to_minor(amount or 0)
        passend = [c for c in candidates if abs((c[2] or 0) - minor) < 50]
        if not passend:
            return None
        for booking_id, parent_id, _amount in passend:
            if parent_id:
                return parent_id
        return passend[0][0]

    def _insert_wiso_assets(self, assets, result):
        """Anlagegüter samt AfA-Plan übernehmen.

        Die Inventarnummer ist der Schlüssel: ein zweiter Lauf aktualisiert
        das vorhandene Anlagegut, statt es zu verdoppeln.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        coa_map = self._coa_map(cursor)

        for asset in assets:
            number = str(asset.number) if asset.number is not None else None
            cursor.execute('SELECT ID FROM Assets WHERE InventoryNumber = ?',
                           (number,))
            row = cursor.fetchone()
            # Anschaffung und Verkauf stehen beide **netto** in den Büchern –
            # nur so ist der Vergleich mit dem Restbuchwert aussagekräftig.
            values = (asset.label or 'Anlagegut', coa_map.get(asset.account),
                      asset.purchase_date, to_minor(asset.purchase_price or 0),
                      asset.useful_life_years or 1, asset.sale_date,
                      self._minor_opt(asset.sale_price),
                      'sold' if asset.sale_date else 'active')
            if row:
                asset_id = row[0]
                # COALESCE beim Erlös: laesst sich der Verkaufspreis nicht aus
                # WISO ableiten, darf ein zweiter Lauf einen von Hand
                # gepflegten Wert nicht wieder ausloeschen.
                cursor.execute('''
                    UPDATE Assets SET Name=?, COA_ID=?, PurchaseDate=?,
                        PurchasePrice=?, UsefulLifeYears=?, SaleDate=?,
                        SalePrice=COALESCE(?, SalePrice), Status=?
                    WHERE ID=?
                ''', values + (asset_id,))
            else:
                cursor.execute('''
                    INSERT INTO Assets
                        (Name, COA_ID, PurchaseDate, PurchasePrice,
                         UsefulLifeYears, SaleDate, SalePrice, Status,
                         InventoryNumber, DepreciationMethod)
                    VALUES (?,?,?,?,?,?,?,?,?,'linear')
                ''', values + (number,))
                asset_id = cursor.lastrowid
            result['assets'] += 1
            for warning in asset.warnings:
                result['asset_warnings'].append(
                    f'{asset.label or number}: {warning}')

            for entry in asset.depreciations:
                if entry.year is None:
                    continue
                cursor.execute('''
                    INSERT INTO AssetDepreciations
                        (Asset_ID, Year, DepreciationAmount, BookValue, Status)
                    VALUES (?,?,?,?,'booked')
                    ON CONFLICT(Asset_ID, Year) DO UPDATE SET
                        DepreciationAmount=excluded.DepreciationAmount,
                        BookValue=excluded.BookValue
                ''', (asset_id, entry.year, to_minor(entry.amount or 0),
                      to_minor(entry.book_value or 0)))
                result['depreciations'] += 1

        conn.commit()
        conn.close()
