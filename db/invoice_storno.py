# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Stornorechnung (seit 2.2.1).

Eine versendete Rechnung mit positivem Betrag wird nicht mehr von Hand auf „storniert“ gesetzt, sondern über
``cancel_invoice``: Es entsteht ein eigener Beleg, die **Stornorechnung** – mit eigener Nummer aus dem Rechnungskreis
(Jahr des Stornodatums), dem Kopf der Ursprungsrechnung, ihren Positionen mit negativer Menge und exakt negierten
Summen, dem Status „versendet“, ``InvoiceKind = 'storno'`` und ``CancelsInvoiceId`` = die Ursprungsrechnung. Die
Ursprungsrechnung wird „storniert“.

Die Forderung der Ursprungsrechnung („Debitoren an Wartekonto“) wird dabei nie gelöscht. Hat die App sie gebucht,
bucht die Stornierung die **Gegenbuchung** dazu: „Debitoren an Wartekonto“ mit negativem Betrag und negativer Steuer
zur Nummer und zum Datum der Stornorechnung (die Forderung einer Gutschrift, ``db.matching.receivable_condition``).
Hat die Ursprungsrechnung keine Forderung der App (Import, Steuersatz ohne Wartekonto, importierte oder von Hand
gebuchte Forderung), gibt es keine Gegenbuchung. Zahlungen bleiben an der stornierten Rechnung; ihr Betrag ist das
**Guthaben** des Kunden (``get_storno_credits``), und beim Versenden der Ersatzrechnung gehen sie auf diese über
(``InvoicePaymentsMixin._move_replaced_payments``). Der Stornorechnung lässt sich keine Zahlung zuordnen.

Eine von Hand stornierte Rechnung ohne Stornorechnung (Altbestand) bekommt sie nachträglich: Es entsteht dieselbe
Stornorechnung, die Rechnung bleibt „storniert“ (ihr Status und ihr Protokoll ändern sich nicht), und steht ihre
Forderung noch in den Büchern, folgt die Gegenbuchung dazu.

**Neu ausstellen** (``create_replacement_draft``, seit 2.2.2): Zu einer stornierten Rechnung mit Stornorechnung legt die
App auf Wunsch den Entwurf einer neuen Rechnung an (``Invoices.ReplacesInvoiceId`` = die stornierte Rechnung), mit
Kunde und Positionen der alten, neuer Nummer und dem heutigen Datum – höchstens einen je stornierter Rechnung.

Alles – Prüfung, Nummer, Beleg, Positionen, Status der Ursprungsrechnung, Protokoll, Gegenbuchung, Neuberechnung –
läuft in EINER Transaktion (``BEGIN IMMEDIATE``): Es gelingt ganz oder gar nicht, und eine verbrauchte Nummer gibt
es nach einem Fehler nicht. Das Dokument der Stornorechnung erzeugt der Aufrufer danach.

Das Stornodatum liegt nie in der Zukunft (nach dem heutigen Tag des Servers, ``_today``): Ein Tippfehler (2062) legte
sonst einen Nummernkreis 2062 an, und das ließe sich nicht umkehren. Eine Rechnung ohne positiven Betrag (Gutschrift,
0-€-Rechnung) bekommt keine Stornorechnung; sie lässt sich über den Status auf „storniert“ setzen
(``db.invoice_lock.TRANSITIONS``, Art ``cancel``).
"""
import datetime
import sqlite3
from collections import namedtuple

from money import from_minor
from .core import ForeignKeyError
from .invoice_lock import (
    CHANGED_MESSAGE, EDITABLE_STATUSES, LOCKED_STATUSES, STORNO_KIND, _Refusal, canonical_status,
    document_version, invoice_row, is_storno)
from .invoice_versions import is_iso_date
from .numbering import NumberRangeError

#: Zahlungstext der Stornorechnung (steht im Dokument als Zahlungsbedingung)
STORNO_PAYMENT_TEXT = 'Der Betrag wird verrechnet bzw. erstattet.'

STORNO_DATE_MESSAGE = 'Das Stornodatum fehlt oder ist kein gültiges Datum.'
STORNO_DATE_BEFORE_MESSAGE = 'Das Stornodatum darf nicht vor dem Rechnungsdatum liegen.'
STORNO_DATE_FUTURE_MESSAGE = 'Das Stornodatum darf nicht in der Zukunft liegen.'
NO_POSITIVE_AMOUNT_MESSAGE = ('Eine Rechnung ohne positiven Betrag bekommt keine Stornorechnung – sie lässt sich '
                              'über den Status auf „Storniert“ setzen.')
STORNO_FAILED_PREFIX = 'Die Stornierung konnte nicht gespeichert werden'
STORNO_FAILED_MESSAGE = f'{STORNO_FAILED_PREFIX} – nichts wurde geändert.'
ALREADY_CANCELLED_MESSAGE = 'Die Rechnung ist bereits storniert.'
#: Hinweis an den Nutzer, wenn die Stornierung keine Gegenbuchung anlegt
NO_COUNTER_BOOKING_HINT = ('Zu dieser Rechnung gibt es keine von der App gebuchte Forderung – es wurde keine '
                           'Gegenbuchung angelegt.')

#: Ergebnis von ``cancel_invoice``: ob storniert wurde, bei einer Ablehnung der Klartext und der Statuscode für die
#: Antwort (nichts wurde geschrieben), sonst ID und Nummer der Stornorechnung und ob die Forderung eine Gegenbuchung
#: bekommen hat
Storno = namedtuple('Storno', 'ok message code storno_id storno_number counter_booked',
                    defaults=(None, None, False))

REISSUE_STORNO_MESSAGE = 'Eine Stornorechnung lässt sich nicht neu ausstellen.'
REISSUE_NOT_CANCELLED_MESSAGE = 'Neu ausgestellt wird nur eine stornierte Rechnung.'
REISSUE_NO_STORNO_MESSAGE = 'Zu dieser Rechnung gibt es keine Stornorechnung – erst die Stornorechnung erstellen.'
REISSUE_FAILED_PREFIX = 'Die neue Rechnung konnte nicht angelegt werden'
REISSUE_FAILED_MESSAGE = f'{REISSUE_FAILED_PREFIX} – nichts wurde geändert.'

#: Ergebnis von ``create_replacement_draft``: ob der Entwurf angelegt wurde, bei einer Ablehnung der Klartext und der
#: Statuscode (nichts wurde geschrieben), sonst ID und Nummer des Entwurfs
Reissue = namedtuple('Reissue', 'ok message code invoice_id invoice_number', defaults=(None, None))

#: Beide Belege eines Storno-Paares und das Guthaben des Kunden: die Zuordnungen beider Belege zusammen (Minor Units).
#: Heute trägt nur die stornierte Rechnung Zahlungen; was später an der Stornorechnung hängt (eine Erstattung), zählt hier mit.
_CREDIT_SQL = ('SELECT s.ID AS StornoID, s.InvoiceNumber AS StornoNumber, s.InvoiceDate AS StornoDate, '
               'o.ID AS OriginalID, o.InvoiceNumber AS OriginalNumber, '
               '(SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = o.ID) '
               '+ (SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = s.ID) AS Credit '
               'FROM Invoices s JOIN Invoices o ON o.ID = s.CancelsInvoiceId WHERE s.InvoiceKind = ?')


def _today():
    """Der heutige Tag des Servers (Ortsdatum) – eine eigene Funktion, damit Tests ihn stellen können."""
    return datetime.date.today()


def _negated(value):
    """``-value``, ohne aus einer 0 eine ``-0.0`` zu machen."""
    return -value if value else value


class InvoiceStornoMixin:
    def cancel_invoice(self, invoice_id, storno_date, user_name=None, *, today=None):
        """Eine versendete Rechnung stornieren: die Stornorechnung anlegen, die Rechnung auf „storniert“ setzen und – hat
        die App ihre Forderung gebucht – die Gegenbuchung dazu. ``storno_date`` (ISO-Datum) ist das Rechnungsdatum der
        Stornorechnung und bestimmt Nummernkreis (Jahr) und Datum der Gegenbuchung; ``user_name`` steht im Protokoll.
        ``today``: der Tag, nach dem kein Stornodatum liegen darf (``datetime.date``); ohne Angabe der heutige Tag des
        Servers. Nur die Demodaten geben ihn an – sie laufen zu einem Szenariodatum, das auch ein anderer Tag sein kann.

        Abgelehnt wird (``Storno.ok`` falsch, ``message`` im Klartext, ``code`` der Statuscode, nichts geschrieben):
        ein Stornodatum, das kein ISO-Datum ist oder nach heute liegt (400); eine unbekannte ID oder die ID eines
        Angebots (404); eine Stornorechnung, ein Entwurf, eine abgeschlossene oder eine schon mit Stornorechnung
        stornierte Rechnung, ein unbekannter Status, eine Rechnung ohne positiven Betrag (sie wird über den Status
        storniert), eine Rechnung mit Stornorechnung (409); ein Stornodatum vor dem Rechnungsdatum (400); ein
        Nummernkreis ohne brauchbare Nummer (409); ein Fehler beim Speichern (409) – zeigt dabei ein Verweis der
        Rechnung ins Leere (``ForeignKeyError``), steht er im Klartext in der Meldung.

        Eine Rechnung, die schon „storniert“ ist, aber keine Stornorechnung hat (von Hand storniert, Altbestand), bekommt
        sie nachträglich: Status und Protokoll der Rechnung bleiben, wie sie sind; die Gegenbuchung gibt es nur, wenn die
        App ihre Forderung gebucht hat und sie noch in den Büchern steht.

        Die Konten der Forderung werden vor der Transaktion aufgelöst (sie legt bei Bedarf Konten an – in eigenen
        Verbindungen, nie darin); eine abgelehnte Stornierung legt auch keines an. Das Dokument der Stornorechnung
        erzeugt der Aufrufer nach dem Commit. Returns :class:`Storno`."""
        if not is_iso_date(storno_date):
            return Storno(False, STORNO_DATE_MESSAGE, 400)
        if storno_date > (today or _today()).isoformat():
            return Storno(False, STORNO_DATE_FUTURE_MESSAGE, 400)
        # Vorprüfung ohne Schreibsperre: Abgelehnt wird vor dem Auflösen der Konten; die Prüfung in der Transaktion
        # (derselbe Aufruf) entscheidet endgültig
        conn = self._get_connection()
        try:
            row = invoice_row(conn, invoice_id)
            blocked = self._storno_blocker(conn, row, storno_date)
        finally:
            conn.close()
        if blocked:
            return Storno(False, *blocked)
        accounts, accounts_key = self._receivable_accounts(row['TaxRate']), self._tax_rate_key(row['TaxRate'])

        try:
            with self.transaction() as tx:
                conn = tx.conn
                conn.execute('BEGIN IMMEDIATE')
                row = invoice_row(conn, invoice_id)
                blocked = self._storno_blocker(conn, row, storno_date)
                if blocked:
                    raise _Refusal(*blocked)
                if accounts_key != self._tax_rate_key(row['TaxRate']):
                    raise _Refusal(CHANGED_MESSAGE, 409)          # der Steuersatz hat sich inzwischen geändert
                try:
                    number = self.allocate_invoice_number(conn, storno_date)
                except NumberRangeError as exc:
                    raise _Refusal(str(exc), 409) from None

                storno_id = self.insert_invoice(self._storno_head(row, number, storno_date), tx=tx)
                self._execute_logged(conn, tx, 'Stornorechnung kennzeichnen',
                                     'UPDATE Invoices SET InvoiceKind = ?, CancelsInvoiceId = ?, '
                                     'SentAt = CURRENT_TIMESTAMP WHERE ID = ?',
                                     (STORNO_KIND, invoice_id, storno_id))
                self._copy_items(conn, tx, invoice_id, storno_id, negate=True)

                old = canonical_status(row['Status'])
                if old != 'cancelled':
                    if self._execute_logged(
                            conn, tx, 'Rechnung stornieren',
                            "UPDATE Invoices SET Status = 'cancelled', UpdatedAt = CURRENT_TIMESTAMP "
                            "WHERE ID = ? AND COALESCE(Status, 'draft') = ?",
                            (invoice_id, row['Status'] or 'draft')).rowcount != 1:
                        raise _Refusal(CHANGED_MESSAGE, 409)
                    self._log_invoice_change(conn, invoice_id, row['InvoiceNumber'], user_name, 'status', 'Status',
                                             old, 'cancelled', document_version(row))
                # eine schon stornierte Rechnung (nachträgliche Stornorechnung) ändert sich nicht
                self._log_invoice_change(conn, storno_id, number, user_name, 'status', 'Status', None, 'sent', 1)

                # Die Gegenbuchung gibt es nur zu einer Forderung, die die App gebucht hat; die Forderung der
                # Ursprungsrechnung bleibt in jedem Fall stehen
                counter_booked = False
                if self._app_receivable_ids(conn, row['InvoiceNumber']):
                    counter_booked = self._book_receivable(tx, invoice_row(conn, storno_id), accounts) is not None

                self._recalc_invoice(conn, invoice_id, adjust_status=False)
                self._recalc_invoice(conn, storno_id, adjust_status=False)
        except _Refusal as refusal:
            return Storno(False, str(refusal), refusal.code)
        except ForeignKeyError as exc:
            # Ein Verweis der Rechnung (Bankkonto, Kunde, eigene Firma …) zeigt ins Leere, etwa nach einem Eingriff über
            # die SQL-Konsole: Der Klartext nennt ihn, geschrieben ist nichts, die Nummer nicht verbraucht
            print(f"Stornierung der Rechnung {invoice_id} nicht gespeichert: {exc}")
            return Storno(False, f'{STORNO_FAILED_PREFIX}: {exc}', 409)
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.IntegrityError) and 'CancelsInvoiceId' in str(exc):
                # der eindeutige Index auf der Stornorechnung: ein gleichzeitiger zweiter Aufruf war schneller
                existing = self.find_storno_invoice(invoice_id)
                if existing:
                    return Storno(False, _already_message(existing[1]), 409)
            print(f"Stornierung der Rechnung {invoice_id} nicht gespeichert: {exc}")
            return Storno(False, STORNO_FAILED_MESSAGE, 409)
        return Storno(True, None, 200, storno_id, number, counter_booked)

    def create_replacement_draft(self, original_id, user_name=None, *, today=None):
        """Zu einer stornierten Rechnung mit Stornorechnung den Entwurf einer neuen Rechnung anlegen („Neu
        ausstellen“): Kopf und Positionen der stornierten Rechnung, eine neue Nummer aus dem Rechnungskreis des
        heutigen Jahres, das heutige Datum, die Fälligkeit um dasselbe Zahlungsziel verschoben, Status „Entwurf“ und
        ``ReplacesInvoiceId`` = die stornierte Rechnung. Beim Versenden gehen die Zahlungen der stornierten Rechnung
        auf sie über (``InvoicePaymentsMixin._move_replaced_payments``).

        Eine Transaktion (``BEGIN IMMEDIATE``): Es gibt höchstens eine Ersatzrechnung je stornierter Rechnung, und
        scheitert etwas, ist auch die Nummer nicht verbraucht. Ein neuer Entwurf wird, wie jeder, nicht protokolliert
        (``user_name`` bleibt ohne Wirkung). ``today``: wie bei ``cancel_invoice`` der Tag, der als „heute“ gilt.

        Abgelehnt wird (``Reissue.ok`` falsch, ``message`` im Klartext, ``code`` der Statuscode, nichts geschrieben):
        eine unbekannte ID oder die ID eines Angebots (404); eine Stornorechnung, eine Rechnung, die nicht „storniert“
        ist, eine stornierte ohne Stornorechnung, eine, die schon eine Ersatzrechnung hat, ein Nummernkreis ohne
        brauchbare Nummer und ein Fehler beim Speichern (409) – zeigt dabei ein Verweis der Rechnung ins Leere
        (``ForeignKeyError``), steht er im Klartext in der Meldung. Returns :class:`Reissue`."""
        day = (today or _today()).isoformat()
        try:
            with self.transaction() as tx:
                conn = tx.conn
                conn.execute('BEGIN IMMEDIATE')
                row = invoice_row(conn, original_id)
                blocked = self._reissue_blocker(conn, row)
                if blocked:
                    raise _Refusal(*blocked)
                try:
                    number = self.allocate_invoice_number(conn, day)
                except NumberRangeError as exc:
                    raise _Refusal(str(exc), 409) from None
                new_id = self.insert_invoice(self._replacement_head(row, number, day), tx=tx)
                self._execute_logged(conn, tx, 'Ersatzrechnung kennzeichnen',
                                     'UPDATE Invoices SET ReplacesInvoiceId = ? WHERE ID = ?', (original_id, new_id))
                self._copy_items(conn, tx, original_id, new_id)
        except _Refusal as refusal:
            return Reissue(False, str(refusal), refusal.code)
        except ForeignKeyError as exc:
            print(f"Ersatzrechnung zu {original_id} nicht gespeichert: {exc}")
            return Reissue(False, f'{REISSUE_FAILED_PREFIX}: {exc}', 409)
        except sqlite3.Error as exc:
            print(f"Ersatzrechnung zu {original_id} nicht gespeichert: {exc}")
            return Reissue(False, REISSUE_FAILED_MESSAGE, 409)
        return Reissue(True, None, 200, new_id, number)

    @staticmethod
    def _reissue_blocker(conn, row):
        """Warum lässt sich zur gespeicherten Rechnung ``row`` (Spalte → Wert, ``None`` wenn es sie nicht gibt oder sie
        ein Angebot ist) keine Ersatzrechnung anlegen? ``(Klartext, Statuscode)`` der ersten Prüfung, die ablehnt, sonst
        ``None``. In der Transaktion von ``conn``."""
        if row is None:
            return 'Rechnung nicht gefunden.', 404
        if is_storno(row):
            return REISSUE_STORNO_MESSAGE, 409
        if canonical_status(row['Status']) != 'cancelled':
            return REISSUE_NOT_CANCELLED_MESSAGE, 409
        if not conn.execute('SELECT 1 FROM Invoices WHERE CancelsInvoiceId = ?', (row['ID'],)).fetchone():
            return REISSUE_NO_STORNO_MESSAGE, 409
        existing = conn.execute('SELECT ID, InvoiceNumber FROM Invoices WHERE ReplacesInvoiceId = ?',
                                (row['ID'],)).fetchone()
        if existing:
            return _replacement_exists_message(existing[1]), 409
        return None

    @staticmethod
    def _execute_logged(conn, tx, description, sql, params):
        """Eine Anweisung in der Transaktion ausführen und fürs Replay-Log vormerken (es schreibt erst nach dem
        Commit). Returns den Cursor."""
        cursor = conn.execute(sql, params)
        tx.log.append((sql, params, description))
        return cursor

    def _storno_blocker(self, conn, row, storno_date):
        """Warum lässt sich die gespeicherte Rechnung ``row`` (Spalte → Wert, ``None`` wenn es sie nicht gibt oder sie ein
        Angebot ist) zu diesem Datum nicht stornieren? ``(Klartext, Statuscode)`` der ersten Prüfung, die ablehnt,
        sonst ``None``. In der Transaktion von ``conn``. Eine schon stornierte Rechnung ohne Stornorechnung (von
        Hand storniert) lässt sich stornieren: Sie bekommt die Stornorechnung nachträglich."""
        if row is None:
            return 'Rechnung nicht gefunden.', 404
        if is_storno(row):
            return 'Eine Stornorechnung lässt sich nicht stornieren.', 409
        status = canonical_status(row['Status'])
        if status in EDITABLE_STATUSES:
            return 'Ein Entwurf wird nicht storniert – er lässt sich löschen, solange er ein Entwurf ist.', 409
        if status == 'cancelled':
            # von Hand storniert (Altbestand): Die Stornorechnung entsteht nachträglich, solange es keine gibt
            if conn.execute('SELECT 1 FROM Invoices WHERE CancelsInvoiceId = ?', (row['ID'],)).fetchone():
                return ALREADY_CANCELLED_MESSAGE, 409
        elif status not in LOCKED_STATUSES:
            # status_group() hielte einen unbekannten Status für gesperrt – storniert wird nur ein bekannter
            return f'Der Status „{status}“ ist unbekannt – die Rechnung lässt sich nicht stornieren.', 409
        if (row['SumGross'] or 0) <= 0:
            return NO_POSITIVE_AMOUNT_MESSAGE, 409
        existing = conn.execute('SELECT ID, InvoiceNumber, InvoiceDate FROM Invoices WHERE CancelsInvoiceId = ?',
                                (row['ID'],)).fetchone()
        if existing:
            return _already_message(existing[1]), 409
        if storno_date < str(row['InvoiceDate'] or '')[:10]:
            return STORNO_DATE_BEFORE_MESSAGE, 400
        return None

    @staticmethod
    def _storno_head(row, number, storno_date):
        """Die Angaben der Stornorechnung für ``insert_invoice``: der Kopf der Ursprungsrechnung ``row`` (Absender,
        Kunde samt Anschrift-Momentaufnahme, Käufer-USt-IdNr., Leitweg-ID, Bestellnummer, Bank, Währung, Steuer,
        Leistungsdatum) mit neuer Nummer, dem Stornodatum als Rechnungsdatum, ohne Fälligkeit und Skonto, mit dem
        Zahlungstext der Stornorechnung und den Summen der Ursprungsrechnung – exakt negiert, nichts neu
        gerechnet. Rest = Brutto, Status „versendet“."""
        def euro(minor):
            return from_minor(-minor)
        return {
            **_copied_head(row),
            'invoice_number': number, 'invoice_date': storno_date,
            'payment_terms': STORNO_PAYMENT_TEXT, 'payment_due_date': None,
            'skonto_days': None, 'skonto_percent': None,
            'sum_net': euro(row['SumNet']), 'tax_amount': euro(row['TaxAmount']),
            'sum_gross': euro(row['SumGross']), 'amount_due': euro(row['SumGross']),
            'status': 'sent',
        }

    @staticmethod
    def _replacement_head(row, number, day):
        """Die Angaben der Ersatzrechnung für ``insert_invoice``: der Kopf der stornierten Rechnung ``row`` (wie bei der
        Stornorechnung: Absender, Kunde samt Anschrift-Momentaufnahme, Käufer-USt-IdNr., Leitweg-ID, Bestellnummer, Bank,
        Währung, Steuer, Leistungsdatum) mit neuer Nummer, dem heutigen Tag als Rechnungsdatum, Zahlungshinweis und Skonto
        der Rechnung, der Fälligkeit um dasselbe Zahlungsziel verschoben (``_shifted_due_date``) und den Summen der
        stornierten Rechnung – positiv, nichts neu gerechnet. Rest = Brutto, Status „Entwurf“."""
        return {
            **_copied_head(row),
            'invoice_number': number, 'invoice_date': day,
            'payment_terms': row['PaymentTerms'], 'payment_due_date': _shifted_due_date(row, day),
            'skonto_days': row['SkontoDays'], 'skonto_percent': row['SkontoPercent'],
            'sum_net': from_minor(row['SumNet']), 'tax_amount': from_minor(row['TaxAmount']),
            'sum_gross': from_minor(row['SumGross']), 'amount_due': from_minor(row['SumGross']),
            'status': 'draft',
        }

    def _copy_items(self, conn, tx, source_id, target_id, negate=False):
        """Die Positionen des Belegs ``source_id`` in derselben Reihenfolge als Positionen von ``target_id``. Mit
        ``negate`` (Stornorechnung) sind Menge und Positionssumme negiert, Einzelpreis und alles andere unverändert; ohne
        (Ersatzrechnung) bleibt alles, wie es ist."""
        rows = conn.execute(
            'SELECT Position, ArticleId, Description, Quantity, Unit, PricePerUnit, TotalNet, TaxCategory, TaxRate '
            'FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position, ID', (source_id,)).fetchall()
        for position, article, description, quantity, unit, price, total, category, rate in rows:
            self.insert_invoice_item({
                'invoice_id': target_id, 'position': position, 'article_id': article,
                'description': description, 'quantity': _negated(quantity) if negate else quantity, 'unit': unit,
                'price_per_unit': from_minor(price or 0),
                'total_net': from_minor(_negated(total or 0) if negate else (total or 0)),
                'tax_category': category, 'tax_rate': rate}, tx=tx)

    def find_storno_invoice(self, invoice_id):
        """Die Stornorechnung zu dieser Rechnung als ``(ID, Nummer, Datum)`` – ``None``, wenn es keine gibt."""
        conn = self._get_connection()
        try:
            return conn.execute('SELECT ID, InvoiceNumber, InvoiceDate FROM Invoices WHERE CancelsInvoiceId = ?',
                                (invoice_id,)).fetchone()
        finally:
            conn.close()

    def find_cancelled_invoice(self, storno_id):
        """Die Rechnung, die diese Stornorechnung storniert, als ``(ID, Nummer, Datum)`` – ``None``, wenn es sie nicht
        gibt oder der Beleg keine Stornorechnung ist."""
        conn = self._get_connection()
        try:
            return conn.execute(
                'SELECT o.ID, o.InvoiceNumber, o.InvoiceDate FROM Invoices s JOIN Invoices o ON o.ID = s.CancelsInvoiceId '
                'WHERE s.ID = ? AND s.InvoiceKind = ?', (storno_id, STORNO_KIND)).fetchone()
        finally:
            conn.close()

    def find_replacement_invoice(self, original_id):
        """Die Ersatzrechnung zu dieser stornierten Rechnung als ``(ID, Nummer, Status)`` – ``None``, wenn es keine gibt."""
        conn = self._get_connection()
        try:
            return conn.execute('SELECT ID, InvoiceNumber, Status FROM Invoices WHERE ReplacesInvoiceId = ? ORDER BY ID',
                                (original_id,)).fetchone()
        finally:
            conn.close()

    def find_replaced_invoice(self, invoice_id):
        """Die stornierte Rechnung, die diese Rechnung ersetzt, als ``(ID, Nummer, ID der Stornorechnung, Nummer der
        Stornorechnung)`` – die beiden letzten ``None``, solange es keine Stornorechnung gibt; ``None``, wenn die Rechnung
        keine andere ersetzt oder die ersetzte nicht (mehr) existiert."""
        conn = self._get_connection()
        try:
            return conn.execute(
                'SELECT o.ID, o.InvoiceNumber, s.ID, s.InvoiceNumber FROM Invoices r JOIN Invoices o ON o.ID = r.ReplacesInvoiceId '
                'LEFT JOIN Invoices s ON s.CancelsInvoiceId = o.ID WHERE r.ID = ?', (invoice_id,)).fetchone()
        finally:
            conn.close()

    # ── Guthaben eines Storno-Paares ─────────────────────────────────────────
    # Guthaben = alle Zuordnungen beider Belege des Paares zusammen (Rechnung und Stornorechnung): Was der Kunde auf die
    # stornierte Rechnung gezahlt hat und noch nicht auf eine neue Rechnung übergegangen ist oder erstattet wurde.
    # Rein lesend; ein Statuswechsel an der Stornorechnung gehört nicht dazu.
    def get_storno_credits(self):
        """Das Guthaben je Stornorechnung als ``{ID der Stornorechnung: Betrag (Euro, Decimal)}`` – für jedes Paar ein
        Eintrag, auch mit Guthaben 0. Eine Abfrage für die ganze Rechnungsliste."""
        conn = self._get_connection()
        try:
            return {row[0]: from_minor(row[5]) for row in conn.execute(_CREDIT_SQL, (STORNO_KIND,))}
        finally:
            conn.close()

    def get_storno_credit(self, invoice_id):
        """Das Guthaben des Storno-Paares, zu dem dieser Beleg gehört (die stornierte Rechnung oder ihre Stornorechnung),
        in Euro (Decimal); ``None`` für jeden anderen Beleg."""
        conn = self._get_connection()
        try:
            row = conn.execute(f'{_CREDIT_SQL} AND (s.ID = ? OR o.ID = ?)', (STORNO_KIND, invoice_id, invoice_id)).fetchone()
        finally:
            conn.close()
        return from_minor(row[5]) if row else None

    def get_storno_credits_for_contact(self, contact_id):
        """Die Storno-Paare dieses Kunden mit Guthaben über 0, das jüngste Datum der Stornorechnung zuerst:
        ``[(ID der Stornorechnung, Nummer, Datum, ID der stornierten Rechnung, Nummer, Guthaben€)]``."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                f'SELECT * FROM ({_CREDIT_SQL} AND o.CustomerId = ?) WHERE Credit > 0 '
                'ORDER BY StornoDate DESC, StornoID DESC', (STORNO_KIND, contact_id)).fetchall()
        finally:
            conn.close()
        return [(*row[:5], from_minor(row[5])) for row in rows]


def _already_message(number):
    return f'Zu dieser Rechnung gibt es bereits die Stornorechnung {number}.'


def _replacement_exists_message(number):
    return f'Zu dieser Rechnung gibt es bereits die neue Rechnung {number}.'


def _copied_head(row):
    """Was Stornorechnung und Ersatzrechnung vom Kopf der Ursprungsrechnung ``row`` (Spalte → Wert) übernehmen: der
    Absender mit seiner Firma, der Kunde samt Anschrift-Momentaufnahme, Käufer-USt-IdNr., Leitweg-ID, Bestellnummer,
    Bankverbindung, Währung, Steuersatz und -kategorie und das Leistungsdatum – ohne Nummer, Datum, Zahlungsbedingungen,
    Summen, Status und Dokumente."""
    return {
        'own_company_id': row['OwnCompanyId'],
        'seller_name': row['SellerName'], 'seller_company': row['SellerCompany'],
        'seller_street': row['SellerStreet'], 'seller_postal_code': row['SellerPostalCode'],
        'seller_city': row['SellerCity'], 'seller_country': row['SellerCountry'],
        'seller_vat_id': row['SellerVATID'], 'seller_email': row['SellerEmail'],
        'seller_phone': row['SellerPhone'],
        'customer_id': row['CustomerId'],
        'buyer_name': row['BuyerName'], 'buyer_company': row['BuyerCompany'],
        'buyer_street': row['BuyerStreet'], 'buyer_postal_code': row['BuyerPostalCode'],
        'buyer_city': row['BuyerCity'], 'buyer_country': row['BuyerCountry'],
        'buyer_vat_id': row['BuyerVATID'], 'buyer_reference': row['BuyerReference'],
        'buyer_route_id': row['BuyerRouteID'],
        'order_number': row['OrderNumber'], 'currency': row['Currency'],
        'delivery_date': row['DeliveryDate'],
        'bank_account_id': row['BankAccountId'], 'bank_name': row['BankName'],
        'bank_iban': row['BankIBAN'], 'bank_bic': row['BankBIC'],
        'tax_category': row['TaxCategory'], 'tax_rate': row['TaxRate'],
        'pdf_path': None, 'xml_path': None, 'document_type': 'invoice',
    }


def _shifted_due_date(row, day):
    """Die Fälligkeit der Ersatzrechnung: Das Zahlungsziel der stornierten Rechnung (Fälligkeit minus Rechnungsdatum, in
    Tagen) ab dem neuen Rechnungsdatum ``day`` (ISO-Datum). ``None``, wenn eines der Daten fehlt oder kein Datum ist oder
    das Ziel negativ wäre – die Maske rechnet die Fälligkeit beim Kopieren ebenso neu."""
    try:
        invoice_day = datetime.date.fromisoformat(str(row['InvoiceDate'] or '')[:10])
        due_day = datetime.date.fromisoformat(str(row['PaymentDueDate'] or '')[:10])
        term = (due_day - invoice_day).days
        if term < 0:
            return None
        return (datetime.date.fromisoformat(day) + datetime.timedelta(days=term)).isoformat()
    except (ValueError, OverflowError):
        return None
