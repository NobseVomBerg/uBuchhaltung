# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Zahlungszuordnung Rechnung ↔ Bankbewegung.

Eine Zuordnung besteht aus genau einer InvoicePayments-Zeile und den Buchungssätzen, die die
App für sie erzeugt hat. Diese Sätze sind eindeutig bestimmt durch

* ``ParentBooking_ID`` = die Bankbewegung der Zuordnung,
* ``DocumentNumber``   = die Rechnungsnummer,
* ``Status``           = ``PAYMENT_MARK`` (``'invoice_payment'``).

Es gibt genau EINE Stelle, die Zuordnungen anlegt (``assign_invoice_payment``) und EINE, die sie
entfernt (``unassign_invoice_payment``); ``delete_transaction`` und der WISO-Import gehen über
dieselben Bausteine. Anlegen und Entfernen laufen in einer Transaktion mit ``BEGIN IMMEDIATE``:
Prüfung, Schreiben und Neuberechnung der Rechnung sehen denselben Stand – zwei gleichzeitige
Anfragen legen das Paar (Rechnung, Bankbewegung) nie zweimal an.
"""
import datetime
import sqlite3
from collections import namedtuple

from money import to_minor, from_minor, prorate, round_minor, fmt_eur, is_covered
from .matching import FORM_LIQUID_FIRST, FORM_PURPOSE_FIRST, FORM_TRANSFER, REVENUE_ACCOUNT_RANGE
from .reporting import is_income_account

PAYMENT_MARK = 'invoice_payment'

# Wert von ``Invoices.InvoiceKind`` einer Stornorechnung (``db/invoice_storno.py``). Hier definiert, weil
# dieses Modul das unterste der Rechnungsmodule ist; ``db.invoice_lock`` reicht den Namen weiter.
STORNO_KIND = 'storno'
STORNO_PAYMENT_MESSAGE = 'Einer Stornorechnung lässt sich keine Zahlung zuordnen.'

# Auf diese Status lässt sich eine Zahlung zuordnen: ab „versendet“ ist die Rechnung ein
# offener Posten. 'partial' ist der Alt-Wert von 'partial_payment'.
PAYABLE_STATUSES = ('sent', 'overdue', 'partial_payment', 'partial')

# Ab „versendet“ ist die Rechnung ein offener Posten und hat eine Forderungsbuchung; davor
# (Entwurf, abgeschlossen) und nach einem Storno von Hand (Altbestand, ohne Stornorechnung) nicht.
# Zustands- statt übergangsgesteuert, damit auch Sprünge wie abgeschlossen → bezahlt richtig landen.
# Eine Rechnung mit Stornorechnung behält ihre Forderung in jedem Fall (``_drop_receivable``).
RECEIVABLE_STATUSES = frozenset({'sent', 'partial_payment', 'overdue', 'paid'})

NO_REVENUE_COA = ('Für den Steuersatz dieser Rechnung ist kein Erlöskonto hinterlegt – bitte ein '
                  'Erlöskonto wählen.')
NOT_A_REVENUE_COA = 'Das gewählte Konto ist kein Erlöskonto.'


def next_payment_status(current, gross, paid, due_date, today):
    """Status einer Rechnung, nachdem sich ihre Zahlungen geändert haben.

    ``gross``/``paid`` in Minor Units, ``due_date``/``today`` als ISO-Datum. Vorzeichenbewusst
    (die WISO-Gutschrift hat einen negativen Brutto- und negative Zahlungen):

    * ``cancelled`` bleibt ``cancelled`` (der Rest wird trotzdem neu gerechnet);
    * Zahlung in Höhe der Rechnung (Rest unter einem Cent) → ``paid``, sonst ``partial_payment``;
    * keine Zahlung mehr, Status bezahlt/Teilzahlung → ``overdue`` (Fälligkeit überschritten)
      bzw. ``sent`` – nie ``finalized``: ab „versendet“ ist die Rechnung eingefroren;
    * alles andere bleibt, wie es ist. Entwurf/abgeschlossen setzt diese Funktion nie.
    """
    if current == 'cancelled':
        return current
    sign = (gross > 0) - (gross < 0)
    if paid * sign > 0:
        return 'paid' if is_covered(gross, paid) else 'partial_payment'
    if current in ('paid', 'partial_payment', 'partial'):
        return 'overdue' if due_date and str(due_date)[:10] < today else 'sent'
    return current


class PaymentAssignment(namedtuple('PaymentAssignment',
                                   'ok message payment_id revenue_coa_id')):
    """Ergebnis von ``assign_invoice_payment``: ob die Zuordnung zustande kam, bei einer
    Ablehnung der Klartext für die Oberfläche (sonst ``None``), die ID der Zuordnung und das
    Erlöskonto (COA-ID), auf das gebucht wurde."""
    __slots__ = ()


def _refused(message):
    return PaymentAssignment(False, message, None, None)


class PaymentMoveFailed(ValueError):
    """Der Umzug der Zahlungen einer stornierten Rechnung auf ihre Ersatzrechnung ist gescheitert; ``str(exc)`` ist der
    Klartext. ``Database.set_invoice_status`` macht daraus eine Ablehnung und rollt damit alles zurück, was der Wechsel in
    seiner Transaktion geschrieben hat (``db.invoice_lock._Refusal`` ist von hier aus nicht erreichbar: Zirkelimport)."""


def payment_blocker(doc_type, status, gross, kind=None):
    """Warum sich auf diese Rechnung keine Zahlung zuordnen lässt – ``None``, wenn doch. ``kind`` ist
    ``Invoices.InvoiceKind``: Einer Stornorechnung wird nie eine Zahlung zugeordnet."""
    if (doc_type or 'invoice') != 'invoice':
        return 'Rechnung nicht gefunden'
    if kind == STORNO_KIND:
        return STORNO_PAYMENT_MESSAGE
    if status == 'paid':
        return 'Die Rechnung ist bereits bezahlt.'
    if status == 'cancelled':
        return 'Die Rechnung ist storniert.'
    if status not in PAYABLE_STATUSES:
        return ('Die Rechnung ist noch nicht versendet – Zahlungen lassen sich erst nach dem '
                'Versenden zuordnen.')
    if (gross or 0) <= 0:
        return ('Nur Rechnungen mit positivem Bruttobetrag lassen sich durch eine Zahlung '
                'ausgleichen.')
    return None


class InvoicePaymentsMixin:
    # ── Bausteine ────────────────────────────────────────────────────────────
    def _recalc_invoice(self, conn, invoice_id, adjust_status=True, today=None):
        """AmountDue (und, mit ``adjust_status``, der Status) aus den Zahlungen neu berechnen –
        in der Transaktion von ``conn``, ohne zu committen. Der Status einer Stornorechnung ändert sich
        dabei nie."""
        row = conn.execute('SELECT SumGross, Status, PaymentDueDate, InvoiceKind FROM Invoices WHERE ID = ?',
                           (invoice_id,)).fetchone()
        if row is None:
            return
        gross, status, due_date, kind = row[0] or 0, row[1], row[2], row[3]
        paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments '
                            'WHERE InvoiceID = ?', (invoice_id,)).fetchone()[0]
        if adjust_status and kind != STORNO_KIND:
            status = next_payment_status(status, gross, paid, due_date,
                                         today or datetime.date.today().isoformat())
        conn.execute('UPDATE Invoices SET AmountDue = ?, Status = ?, UpdatedAt = CURRENT_TIMESTAMP '
                     'WHERE ID = ?', (gross - paid, status, invoice_id))

    @staticmethod
    def _free_minor_sql(bank_coa_ids, alias='b'):
        """SQL-Ausdruck (und Parameter) für den freien Rest einer Bankbewegung in Minor Units:

            Betrag − MAX(Σ Zuordnungen, Σ bankwirksame Buchungssätze darunter)

        Die EINE Formel für Zuordnung, Kandidatenlisten und Kontakt-Guthaben. Der Rest kann
        negativ sein (überbucht). Bankwirksam: ``db.matching.is_bank_effective``."""
        marks = ','.join('?' * len(bank_coa_ids))
        expr = (f'{alias}.Amount - MAX('
                f'COALESCE((SELECT SUM(ip.Amount) FROM InvoicePayments ip '
                f'WHERE ip.BookingID = {alias}.ID), 0), '
                f'COALESCE((SELECT SUM(k.Amount) FROM Bookings k '
                f'WHERE k.ParentBooking_ID = {alias}.ID '
                f'AND (k.COA_ID IS NULL OR k.CounterCOA_ID IS NULL '
                f'OR k.COA_ID IN ({marks}) OR k.CounterCOA_ID IN ({marks}))), 0))')
        return expr, list(bank_coa_ids) * 2

    def _booking_free_minor(self, conn, booking_id, bank_coa_ids):
        """Freier Rest einer Bankbewegung in Minor Units (siehe ``_free_minor_sql``)."""
        expr, params = self._free_minor_sql(bank_coa_ids)
        row = conn.execute(f'SELECT {expr} FROM Bookings b WHERE b.ID = ?',
                           params + [booking_id]).fetchone()
        return row[0] if row and row[0] is not None else 0

    @staticmethod
    def _payment_children(conn, bank_id, invoice_number):
        """IDs der Buchungssätze, die die App für die Zuordnung (Bankbewegung, Rechnung)
        angelegt hat: die markierten Kinder mit der Rechnungsnummer. Sätze mit Quellklammer
        (WISO) gehören nie dazu."""
        return [row[0] for row in conn.execute(
            "SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND Status = ? "
            "AND DocumentNumber = ? AND (SourceGroup IS NULL OR SourceGroup = '') ORDER BY ID",
            (bank_id, PAYMENT_MARK, invoice_number))]

    @staticmethod
    def _payment_revenue_coa(conn, child_ids):
        """Das Erlöskonto (COA-ID), auf das die Sätze ``child_ids`` einer Zuordnung gebucht sind – in der Form, in
        der die App sie schreibt: das Gegenkonto der Umbuchung „Wartekonto an Erlöskonto“, sonst das Konto des
        Einzelsatzes „Erlöskonto an Bank“. ``None``, wenn die Sätze keine der beiden Formen haben."""
        if not child_ids:
            return None
        rows = conn.execute(
            f"SELECT EntryForm, COA_ID, CounterCOA_ID FROM Bookings WHERE ID IN ({','.join('?' * len(child_ids))}) "
            "ORDER BY ID", list(child_ids)).fetchall()
        for form, _coa, counter in rows:
            if form == FORM_TRANSFER:
                return counter
        for form, coa, _counter in rows:
            if form == FORM_PURPOSE_FIRST:
                return coa
        return None

    def _payment_prerequisites(self, invoice_id, account_id, revenue_coa_id=None):
        """Was vor einer Zuordnung geklärt sein muss, in eigenen Verbindungen: Beim Auflösen
        des Erlöskontos werden bei Bedarf Konten angelegt – das darf nicht innerhalb von
        ``BEGIN IMMEDIATE`` geschehen (ein zweiter Schreiber sperrte sich selbst).

        Das Erlöskonto ergibt sich aus der Rechnung (``resolve_revenue_coa``); ``revenue_coa_id``
        zählt nur, wenn dafür keines hinterlegt ist, und muss dann ein Erlöskonto sein.

        Returns ``(Fehlertext, None)`` oder ``(None, Kontext)``."""
        invoice = self.get_document(invoice_id, 'invoice')
        if invoice is None:
            return 'Rechnung nicht gefunden', None
        # Spalten der Rechnungszeile: 38 = SumGross, 40 = Status, 45 = DocumentType, 50 = InvoiceKind
        blocked = payment_blocker(invoice[45], invoice[40], invoice[38],
                                  invoice[50] if len(invoice) > 50 else None)
        if blocked:
            return blocked, None
        account = self.get_account_by_id(account_id) if account_id else None
        if account is None:
            return 'Für die Zahlung fehlt das Bankkonto.', None
        if not account[7]:
            return ('Das Bankkonto hat keine SKR-Nummer – bitte in den Stammdaten ergänzen, '
                    'sonst lässt sich die Zahlung nicht kontieren.'), None
        bank_coa = self.get_coa_id_by_account_number(account[7])
        if bank_coa is None:
            return f'Das SKR-Konto {account[7]} des Bankkontos fehlt im Kontenrahmen.', None
        revenue_coa = self.resolve_revenue_coa(invoice[35])
        if revenue_coa is None:
            revenue_coa, error = self._chosen_revenue_coa(revenue_coa_id)
            if error:
                return error, None
        return None, {
            'tax_rate': invoice[35], 'account_id': account_id, 'bank_coa': bank_coa,
            'revenue_coa': revenue_coa,
            'bank_coa_ids': self.get_bank_coa_ids(),
        }

    def payment_precheck(self, invoice_id, account_id, revenue_coa_id=None):
        """Fehlertext, wenn sich auf diese Rechnung über dieses Bankkonto keine Zahlung zuordnen
        lässt – sonst ``None``. Für Masken, die die Bankbewegung erst noch anlegen: Wer prüft,
        bevor er schreibt, hinterlässt keine Bewegung ohne Zuordnung. ``revenue_coa_id`` wie
        bei ``assign_invoice_payment``."""
        return self._payment_prerequisites(invoice_id, account_id, revenue_coa_id)[0]

    # ── Erlöskonto: Wahl, wenn keines hinterlegt ist ────────────────────────
    @staticmethod
    def _income_coas(cursor):
        """Die Erlöskonten 4000–4999 ohne die Wartekonten (``db.reporting.is_income_account``)
        als ``[(COA_ID, Kontonummer, Name)]`` nach Kontonummer."""
        rows = cursor.execute(
            'SELECT ID, AccountNumber, Name FROM ChartOfAccounts '
            'WHERE AccountNumber BETWEEN ? AND ? ORDER BY AccountNumber, ID',
            REVENUE_ACCOUNT_RANGE).fetchall()
        return [row for row in rows if is_income_account(row[1])]

    def get_revenue_coas(self):
        """Die Konten, aus denen der Nutzer das Erlöskonto einer Zahlung wählen kann: Erlöskonten
        4000–4999 (``REVENUE_ACCOUNT_RANGE``) ohne die Wartekonten, die keine Erlöse sind
        (``db.reporting.is_income_account``). Returns ``[(COA_ID, Kontonummer, Name)]`` nach
        Kontonummer."""
        conn = self._get_connection()
        try:
            return self._income_coas(conn.cursor())
        finally:
            conn.close()

    def _chosen_revenue_coa(self, revenue_coa_id):
        """Das vom Nutzer gewählte Erlöskonto, geprüft: ``(COA-ID, None)`` oder
        ``(None, Fehlertext)``. Gilt nur, wenn ``resolve_revenue_coa`` keines liefert."""
        if revenue_coa_id is None or revenue_coa_id == '':
            return None, NO_REVENUE_COA
        try:
            chosen = int(revenue_coa_id)
        except (TypeError, ValueError):
            return None, NOT_A_REVENUE_COA
        if chosen not in {row[0] for row in self.get_revenue_coas()}:
            return None, NOT_A_REVENUE_COA
        return chosen, None

    def revenue_coa_choice_needed(self, tax_rate):
        """Muss der Nutzer das Erlöskonto wählen, weil für diesen Steuersatz keines hinterlegt
        ist? Steuerfrei/§ 19 nie: Das Konto 4185 legt die Zuordnung bei Bedarf selbst an."""
        if self._revenue_rate_key(tax_rate) == 0:
            return False
        return self.resolve_revenue_coa(tax_rate, create=False) is None

    def suggest_revenue_coa(self, tax_rate):
        """Vorauswahl für die Kontenwahl – nur das, nie eine stille Entscheidung: das Erlöskonto
        (``get_revenue_coas``), das unter den Buchungen mit diesem Steuersatz am häufigsten als
        Konto oder Gegenkonto vorkommt (derselbe Vergleich des Satzes wie in
        ``resolve_revenue_coa``), bei Gleichstand das mit der kleinsten Kontonummer. Gibt es
        keinen Treffer: ``None``. Rein lesend."""
        revenue = {row[0]: row[1] for row in self.get_revenue_coas()}
        if not revenue:
            return None
        key = self._revenue_rate_key(tax_rate)
        rate_sql = self._revenue_rate_key_sql('TaxRate')
        marks = ','.join('?' * len(revenue))
        conn = self._get_connection()
        try:
            counts = conn.execute(
                f'SELECT coa, COUNT(*) FROM ('
                f'SELECT COA_ID AS coa FROM Bookings WHERE {rate_sql} = ? AND COA_ID IN ({marks}) '
                f'UNION ALL '
                f'SELECT CounterCOA_ID FROM Bookings WHERE {rate_sql} = ? '
                f'AND CounterCOA_ID IN ({marks})) GROUP BY coa',
                [key, *revenue, key, *revenue]).fetchall()
        finally:
            conn.close()
        if not counts:
            return None
        return min(counts, key=lambda row: (-row[1], revenue[row[0]]))[0]

    # ── Zuordnen ─────────────────────────────────────────────────────────────
    def assign_invoice_payment(self, invoice_id, booking_id, amount=None, revenue_coa_id=None):
        """Eine Bankbewegung (ganz oder teilweise) einer Rechnung als Zahlung zuordnen.

        Der Betrag ist min(Rest der Rechnung, freier Rest der Bewegung, ``amount``);
        ``amount=None`` heißt „so viel wie möglich“. Eine Überzahlung bleibt als freier Rest
        der Bewegung stehen (Guthaben beim Kunden), eine Sammelzahlung sind mehrere Zuordnungen
        derselben Bewegung, eine Teilzahlung mehrere Bewegungen auf eine Rechnung.

        Zugelassen sind nur Zahlungseingänge (Bewegung mit Betrag > 0) auf versendete
        Rechnungen; das Bankkonto braucht eine SKR-Nummer. Negative Zuordnungen (Erstattung,
        Gutschrift) gibt es nur im WISO-Import (``link_invoice_to_transaction``).

        Geschrieben werden die InvoicePayments-Zeile und die Buchungssätze, alle mit Datum,
        Steuerdatum und Empfänger der Bewegung, der Rechnungsnummer als Beleg-Nr. und der
        Marke ``PAYMENT_MARK``:

        * Mit Forderung (Debitoren an Wartekonto, beim Versenden gebucht):
          ``Bank an Debitoren`` (ohne Steuer) und ``Wartekonto an Erlöskonto`` (mit Steuer).
        * Ohne Forderung (Steuersatz ohne Wartekonto, Bestand vor den Forderungsbuchungen):
          ``Erlöskonto an Bank`` mit Steuer – über den zugeordneten Betrag, nicht den der
          Bewegung.

        Die Steuer eines Teils ist der anteilige Rechnungsbetrag (auf den Cent gerundet); der
        Teil, der die Rechnung schließt, bekommt den Rest – die Summe stimmt so exakt mit der
        Rechnung überein. Abgezogen wird die Steuer der anderen Zuordnungen der Rechnung: bei
        markierten Sätzen die tatsächlich gebuchte, bei Zuordnungen ohne markierte Sätze
        (Altbestand, WISO-Import) ihr anteiliger Betrag.

        Der Betrag wird auf Cent gerundet (halb auf), bevor er gekappt wird – ein Bruchteil eines
        Cents bliebe sonst als Rest auf dem Debitorenkonto stehen und ließe sich nicht exportieren.

        Das Erlöskonto ergibt sich aus der Rechnung (``resolve_revenue_coa``). Nur wenn dafür
        keines hinterlegt ist (z. B. 5 %, 16 % ohne Historie), wählt der Nutzer es:
        ``revenue_coa_id`` muss dann ein Erlöskonto sein (``get_revenue_coas``), sonst lehnt die
        Zuordnung ab. Ist eines hinterlegt, wird ``revenue_coa_id`` ignoriert. Geraten wird nie.

        Returns ``PaymentAssignment(ok, message, payment_id, revenue_coa_id)``; ``message`` ist
        bei einer Ablehnung ein Klartext für die Oberfläche, ``revenue_coa_id`` das Erlöskonto,
        auf das gebucht wurde.
        """
        if self.get_document(invoice_id, 'invoice') is None:
            return _refused('Rechnung nicht gefunden')
        booking = self.get_booking_by_id(booking_id)
        if booking is None:
            return _refused('Buchung nicht gefunden')
        if booking[17] != 'bank':
            return _refused('Nur Bankbewegungen lassen sich einer Rechnung zuordnen.')
        if any(a[1] == invoice_id for a in self.get_booking_allocations(booking_id)):
            return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
        error, ctx = self._payment_prerequisites(invoice_id, booking[4], revenue_coa_id)
        if error:
            return _refused(error)
        requested = None
        if amount is not None:
            requested = round_minor(to_minor(amount), 2)
            if requested <= 0:
                return _refused('Der Betrag muss größer 0 sein.')

        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            result = self._assign_in_tx(tx, invoice_id, booking_id, requested, ctx)
        return result

    def _assign_in_tx(self, tx, invoice_id, booking_id, requested, ctx, allow_cancelled=False):
        """Der Teil von ``assign_invoice_payment``, der in einer Transaktion läuft: prüfen, die Zuordnung und
        ihre Buchungssätze schreiben, die Rechnung neu rechnen. ``tx`` hat die Schreibsperre schon (``BEGIN
        IMMEDIATE`` – der Aufrufer holt sie); der Baustein committet nie. Er wird vom Umzug der Zahlungen beim
        Versenden einer Ersatzrechnung wieder benutzt (``_move_replaced_payments``).

        ``requested``: der gewünschte Betrag in Minor Units, schon auf Cent gerundet, oder ``None`` („so viel
        wie möglich“). ``ctx``: die Schlüssel ``tax_rate, account_id, bank_coa, revenue_coa, bank_coa_ids``
        (``_payment_prerequisites``). ``allow_cancelled``: Auch eine stornierte Rechnung (mit positivem Betrag, keine
        Stornorechnung) nimmt die Zuordnung an – nur für den Rest, der beim Umzug an der stornierten Rechnung bleibt.

        Jede Ablehnung kommt vor dem ersten Schreiben (auch der ``IntegrityError`` beim Einfügen): Eine
        abgelehnte Zuordnung hinterlässt nichts. Returns :class:`PaymentAssignment`."""
        conn = tx.conn
        invoice = conn.execute(
            'SELECT InvoiceNumber, DocumentType, Status, SumGross, TaxAmount, TaxRate, '
            'CustomerId, InvoiceKind FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()
        if invoice is None:
            return _refused('Rechnung nicht gefunden')
        number, doc_type, status, gross, tax_total, rate, customer_id, kind = invoice
        if (doc_type or 'invoice') != 'invoice':
            return _refused('Rechnung nicht gefunden')
        bank = conn.execute(
            'SELECT BookingType, Amount, Account_ID, DateBooking, DateTax, RecipientClient, '
            'Currency FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()
        if bank is None or bank[0] != 'bank':
            return _refused('Nur Bankbewegungen lassen sich einer Rechnung zuordnen.')
        # Das Paar zuerst: ein zweiter, gleichzeitiger Klick bekommt diese Meldung, auch wenn
        # der erste die Rechnung inzwischen bezahlt gemacht hat.
        if conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                        (invoice_id, booking_id)).fetchone():
            return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
        blocked = (None if allow_cancelled and status == 'cancelled' and kind != STORNO_KIND
                   and (gross or 0) > 0 else payment_blocker(doc_type, status, gross, kind))
        if blocked:
            return _refused(blocked)
        _, bank_amount, account_id, bank_date, bank_tax_date, recipient, currency = bank
        if rate != ctx['tax_rate'] or account_id != ctx['account_id']:
            return _refused('Rechnung oder Bankbewegung wurde inzwischen geändert – bitte '
                            'erneut versuchen.')
        if (bank_amount or 0) <= 0:
            return _refused('Nur Zahlungseingänge lassen sich einer Rechnung zuordnen.')

        paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments '
                            'WHERE InvoiceID = ?', (invoice_id,)).fetchone()[0]
        due = gross - paid
        free = self._booking_free_minor(conn, booking_id, ctx['bank_coa_ids'])
        alloc = min(due, free, due if requested is None else requested)
        if alloc <= 0:
            return _refused('Kein offener Betrag zuordenbar (Rechnung oder Buchung '
                            'ausgeschöpft)')

        tax_total = tax_total or 0
        if alloc == due:
            # Der schließende Teil bekommt den Rest der Rechnungssteuer: Rechnungssteuer minus
            # die Steuer der anderen Zuordnungen. Hat eine markierte Sätze, zählt deren
            # gebuchte Steuer (nur die Zahlungssätze tragen Steuer, nicht „Bank an
            # Debitoren“); sonst (Altbestand, WISO-Import) ihr anteiliger Betrag – eine Suche
            # über die Beleg-Nr. fände Sätze mit anderer oder fehlender Nummer nicht.
            taxed_before = 0
            for other_booking, other_amount in conn.execute(
                    'SELECT BookingID, SUM(Amount) FROM InvoicePayments WHERE InvoiceID = ? '
                    'GROUP BY BookingID', (invoice_id,)).fetchall():
                marked = self._payment_children(conn, other_booking, number)
                if marked:
                    taxed_before += conn.execute(
                        f"SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE ID IN "
                        f"({','.join('?' * len(marked))})", marked).fetchone()[0]
                else:
                    taxed_before += round_minor(prorate(tax_total, other_amount, gross), 2)
            tax_part = tax_total - taxed_before
        else:
            tax_part = round_minor(prorate(tax_total, alloc, gross), 2)
        # TaxRate steht teils als 0.19, teils als 19; -1 ist der Sentinel für „§ 19, keine USt“
        tax_rate = None if not rate or rate < 0 else (rate if rate <= 1 else rate / 100)
        tax_value = tax_part if tax_rate else None

        # Die Forderung wird wie überall gesucht (``receivable_condition``): ein Satz mit 10000
        # im Soll und der Rechnungsnummer, kein WISO-Storno „10000 an 1810“
        receivable_sql, receivable_params = self._receivable_sql(conn.cursor())
        receivable = conn.execute(
            f"SELECT COA_ID, CounterCOA_ID FROM Bookings WHERE DocumentNumber = ? "
            f"AND BookingType = 'entry' AND {receivable_sql} ORDER BY ID LIMIT 1",
            [number] + receivable_params).fetchone()
        if receivable:
            debtor_ids = {r[0] for r in conn.execute(
                'SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?',
                (self.DEBTOR_ACCOUNT_NUMBER,))}
            # Normalfall „10000 an Wartekonto“; negativ gebucht steht 10000 als Gegenkonto
            debtor_side, waiting_side = (receivable if receivable[0] in debtor_ids
                                         else (receivable[1], receivable[0]))
        else:
            debtor_side = waiting_side = None

        try:
            cursor = conn.execute(
                'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) '
                'VALUES (?, ?, ?, ?)', (invoice_id, booking_id, alloc, bank_date))
        except sqlite3.IntegrityError:
            # der eindeutige Index auf (Rechnung, Bewegung) – dasselbe wie die Prüfung oben
            return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
        payment_id = cursor.lastrowid
        tx.log.append(('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, '
                       'PaymentDate) VALUES (?, ?, ?, ?)',
                       (invoice_id, booking_id, alloc, bank_date), 'Link invoice to booking'))

        def child(coa_id, counter_coa_id, form, text, taxed, description):
            sql = ('INSERT INTO Bookings (DateBooking, DateTax, RecipientClient, Contact_ID, '
                   'COA_ID, CounterCOA_ID, Amount, Currency, TaxRate, TaxAmount, Text, '
                   "DocumentNumber, BookingType, ParentBooking_ID, AutoMirror, Status, "
                   "EntryForm) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'entry', ?, 0, ?, ?)")
            params = (bank_date, bank_tax_date, recipient or '', customer_id, coa_id,
                      counter_coa_id, alloc, currency or 'EUR',
                      tax_rate if taxed else None, tax_value if taxed else None, text,
                      number, booking_id, PAYMENT_MARK, form)
            conn.execute(sql, params)
            tx.log.append((sql, params, description))

        # Die Formen stehen fest, nicht aus der Kontenliste abgeleitet: „Bank an Debitoren“
        # hat das Geldkonto vorne, „Wartekonto an Erlöskonto“ ist eine Umbuchung, „Erlöskonto
        # an Bank“ das Zweckkonto vorne.
        if debtor_side and waiting_side:
            child(ctx['bank_coa'], debtor_side, FORM_LIQUID_FIRST,
                  f'Zahlung zu Re. {number}', False, 'Payment child (bank to debtor)')
            child(waiting_side, ctx['revenue_coa'], FORM_TRANSFER,
                  f'Umb. zu Re. {number}', True, 'Payment child (open item to revenue)')
        else:
            child(ctx['revenue_coa'], ctx['bank_coa'], FORM_PURPOSE_FIRST,
                  f'Zahlung zu Re. {number}', True, 'Payment child (revenue to bank)')

        self._recalc_invoice(conn, invoice_id)
        return PaymentAssignment(True, None, payment_id, ctx['revenue_coa'])

    # ── Entfernen ────────────────────────────────────────────────────────────
    def _after_payment_removed(self, conn, invoice_id):
        """Nach dem Entfernen von Zahlungen, in der Transaktion von ``conn``:

        * Die Forderung gilt wieder als offen, wenn die übrigen Sätze „… an Debitoren“ sie nicht
          mehr ausgleichen (Stufe 7 des Auto-Abgleichs hatte sie auf 'resolved' gesetzt, siehe
          ``money.is_covered``).
        * AmountDue und Status der Rechnung werden neu gerechnet.
        * Ist keine Zahlung mehr übrig und der Status der Rechnung kein Forderungsstatus
          (storniert, Entwurf, abgeschlossen), geht auch die Forderungsbuchung – so wie beim
          Statuswechsel (``RECEIVABLE_STATUSES``) und nur die, die die App selbst gebucht hat
          (``_drop_receivable``: nie ein Satz mit ``SourceGroup``). Die Reihenfolge „erst
          stornieren, dann die Zahlung entfernen“ endet damit im selben Zustand wie die
          umgekehrte – außer bei einer Rechnung mit Stornorechnung: Deren Forderung bleibt, samt der
          Gegenbuchung der Stornorechnung (``_drop_receivable``)."""
        row = conn.execute('SELECT InvoiceNumber FROM Invoices WHERE ID = ?',
                           (invoice_id,)).fetchone()
        if row is None:
            return
        number = row[0]
        debtor_ids = [r[0] for r in conn.execute(
            'SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?', (self.DEBTOR_ACCOUNT_NUMBER,))]
        if debtor_ids:
            marks = ','.join('?' * len(debtor_ids))
            standalone = conn.execute(
                f"SELECT ID, Amount, Status FROM Bookings WHERE BookingType = 'entry' "
                f"AND ParentBooking_ID IS NULL AND DocumentNumber = ? AND COA_ID IN ({marks})",
                [number] + debtor_ids).fetchall()
            for booking_id, amount, status in standalone:
                if status == 'resolved' and not is_covered(
                        amount or 0, self._debtor_settled_minor(conn.cursor(), number, debtor_ids)):
                    conn.execute('UPDATE Bookings SET Status = NULL WHERE ID = ?', (booking_id,))
        self._recalc_invoice(conn, invoice_id)

        status = conn.execute('SELECT Status FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()[0]
        if status not in RECEIVABLE_STATUSES:
            self._drop_receivable(conn, invoice_id, number)

    def _unassign(self, conn, payment_id):
        """Eine Zuordnung samt ihren Buchungssätzen in der Transaktion von ``conn`` entfernen.

        Die Sätze gehen nur mit, wenn für das Paar (Rechnung, Bewegung) keine weitere
        Zuordnung bleibt (Doppel aus Altbeständen). Returns die Rechnungs-ID oder ``None``,
        wenn es die Zuordnung nicht gibt."""
        row = conn.execute('SELECT ip.InvoiceID, ip.BookingID, i.InvoiceNumber '
                           'FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID '
                           'WHERE ip.ID = ?', (payment_id,)).fetchone()
        if row is None:
            return None
        invoice_id, booking_id, number = row
        conn.execute('DELETE FROM InvoicePayments WHERE ID = ?', (payment_id,))
        if not conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                            (invoice_id, booking_id)).fetchone():
            children = self._payment_children(conn, booking_id, number)
            if children:
                self._delete_booking_rows(conn, children)
        self._after_payment_removed(conn, invoice_id)
        return invoice_id

    def unassign_invoice_payment(self, payment_id):
        """Eine Zuordnung entfernen: ihre Buchungssätze, die Zuordnung selbst, danach die
        Rechnung neu rechnen (Rest, Status: zurück auf versendet bzw. überfällig, nie auf
        abgeschlossen; eine stornierte Rechnung bleibt storniert). Die Forderung bleibt
        stehen und ist damit wieder offen – außer die Rechnung ist kein offener Posten mehr
        (storniert von Hand, ohne Stornorechnung) und es war ihre letzte Zahlung
        (``_after_payment_removed``). Alles in einer Transaktion.

        Returns die Rechnungs-ID; ``ValueError``, wenn es die Zuordnung nicht gibt."""
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            invoice_id = self._unassign(tx.conn, payment_id)
        if invoice_id is None:
            raise ValueError(f"InvoicePayment {payment_id} not found")
        return invoice_id

    def remove_assignment_for_child(self, conn, child_id):
        """Einen markierten Buchungssatz einer Zuordnung löschen heißt: die ganze Zuordnung
        entfernen (Zahlungs- und Umbuchungssatz bleiben nie einzeln übrig). In der Transaktion
        von ``conn``. Gibt es zum Satz keine Zuordnung mehr, wird nur er selbst gelöscht."""
        row = conn.execute('SELECT ParentBooking_ID, DocumentNumber FROM Bookings WHERE ID = ?',
                           (child_id,)).fetchone()
        if row is None:
            return
        payment_ids = [r[0] for r in conn.execute(
            'SELECT ip.ID FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID '
            'WHERE ip.BookingID = ? AND i.InvoiceNumber = ? ORDER BY ip.ID', row)]
        for payment_id in payment_ids:
            self._unassign(conn, payment_id)
        if conn.execute('SELECT 1 FROM Bookings WHERE ID = ?', (child_id,)).fetchone():
            self._delete_booking_rows(conn, [child_id])

    # ── Umzug auf die Ersatzrechnung ─────────────────────────────────────────
    def _movable_payment(self, conn, old_id, old_number, new_id, bank_id, amount, bank_coas, bank_coa_ids):
        """Lässt sich die Zuordnung (stornierte Rechnung ``old_id``, Bankbewegung ``bank_id``) über ``amount`` umziehen?
        Nur, was die App selbst angelegt hat und was sich eindeutig neu schreiben lässt: eine Bankbewegung mit
        Zahlungseingang, deren Konto eine SKR-Nummer hat (``bank_coas``) und die nicht überbucht ist, ohne Sätze mit
        Quellklammer (WISO), mit den markierten Sätzen der App – alle über ``amount`` und mit erkennbarem Erlöskonto
        (``_payment_revenue_coa``) –, genau eine Zuordnung zum Paar und keine der neuen Rechnung zur selben Bewegung.
        Returns ``(Account_ID der Bewegung, Erlöskonto der bisherigen Sätze)`` oder ``None``."""
        bank = conn.execute('SELECT BookingType, Amount, Account_ID FROM Bookings WHERE ID = ?', (bank_id,)).fetchone()
        if bank is None or bank[0] != 'bank' or (bank[1] or 0) <= 0 or amount <= 0 or bank[2] not in bank_coas:
            return None
        if conn.execute("SELECT 1 FROM Bookings WHERE ParentBooking_ID = ? AND SourceGroup IS NOT NULL "
                        "AND SourceGroup <> '' LIMIT 1", (bank_id,)).fetchone():
            return None
        children = self._payment_children(conn, bank_id, old_number)
        if not children:
            return None
        if conn.execute('SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                        (old_id, bank_id)).fetchone()[0] != 1:
            return None
        if conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                        (new_id, bank_id)).fetchone():
            return None
        if conn.execute(f"SELECT 1 FROM Bookings WHERE ID IN ({','.join('?' * len(children))}) AND Amount <> ? LIMIT 1",
                        [*children, amount]).fetchone():
            return None
        if self._booking_free_minor(conn, bank_id, bank_coa_ids) < 0:
            return None
        revenue_coa = self._payment_revenue_coa(conn, children)
        return None if revenue_coa is None else (bank[2], revenue_coa)

    def _move_replaced_payments(self, tx, row, move):
        """Beim Versenden einer Ersatzrechnung (``Invoices.ReplacesInvoiceId``, ``db/invoice_storno.py``) gehen die
        Zahlungen der stornierten Rechnung auf sie über – in der Transaktion ``tx`` des Statuswechsels
        (``Database.set_invoice_status``), nach der Forderungsbuchung der neuen Rechnung (``row``: ihre Zeile). ``move``:
        was ``Database._payment_move_context`` vor der Transaktion aufgelöst hat (``replaced_id``, ``revenue_coa``,
        ``bank_coas`` {Account_ID: COA-ID}, ``bank_coa_ids``).

        Je Zuordnung der stornierten Rechnung, die älteste Zahlung zuerst und solange die neue Rechnung noch offen ist:
        Die Zuordnung samt ihren Sätzen wird entfernt (``_unassign``), die neue Rechnung bekommt den Betrag, den sie
        noch braucht, mit ihren eigenen Sätzen (Datum der Bankbewegung, ihr Steuersatz und ihr Erlöskonto,
        ``_assign_in_tx``), und ein Rest bleibt an der stornierten Rechnung – dort wieder mit Sätzen auf dem bisherigen
        Erlöskonto (``allow_cancelled``). Die Steuer jedes Teils rechnet ``_assign_in_tx`` wie bei jeder Zuordnung; die
        Forderung der stornierten Rechnung samt Gegenbuchung bleibt stehen. Nicht umgezogen wird, was sich nicht
        eindeutig neu schreiben lässt (``_movable_payment``: Import, eine überbuchte Bewegung, ein Bankkonto ohne
        SKR-Nummer, von Hand geänderte Sätze) – es bleibt an der stornierten Rechnung und steht im Hinweis –, und
        nichts, wenn Kunde oder Währung abweichen oder für den Steuersatz der neuen Rechnung kein Erlöskonto
        hinterlegt ist. Eine Sammelzahlung zieht nur mit der Zuordnung um, die zur stornierten Rechnung gehört.

        Returns ``(Betrag in Minor Units, der umgezogen ist, Hinweis für den Nutzer oder None)``. Scheitert eine
        Zuordnung, obwohl die Voraussetzungen stimmten, folgt :class:`PaymentMoveFailed`: Der Aufrufer rollt alles zurück."""
        conn = tx.conn
        new_id, old_id = row['ID'], move['replaced_id']
        old = conn.execute('SELECT InvoiceNumber, DocumentType, Status, TaxRate, CustomerId, Currency '
                           'FROM Invoices WHERE ID = ?', (old_id,)).fetchone()
        if (old is None or (old[1] or 'invoice') != 'invoice' or old[2] != 'cancelled'
                or not conn.execute('SELECT 1 FROM Invoices WHERE CancelsInvoiceId = ?', (old_id,)).fetchone()):
            return 0, None
        old_number, _type, _status, old_rate, old_customer, old_currency = old
        total = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                             (old_id,)).fetchone()[0]
        if total <= 0:
            return 0, None

        def euro(minor):
            return fmt_eur(from_minor(minor))

        if row['CustomerId'] != old_customer or (row['Currency'] or 'EUR') != (old_currency or 'EUR'):
            return 0, (f'Die Zahlungen der stornierten Rechnung {old_number} ({euro(total)}) bleiben dort: '
                       'Kunde oder Währung dieser Rechnung weichen ab.')
        if move['revenue_coa'] is None:
            return 0, (f'Die Zahlungen der stornierten Rechnung {old_number} ({euro(total)}) wurden nicht übernommen: '
                       'Für den Steuersatz dieser Rechnung ist kein Erlöskonto hinterlegt. Bitte die Zuordnung dort '
                       'entfernen und hier mit Erlöskonto zuordnen.')

        def check(result, expected):
            """Die Zuordnung kam mit genau dem Betrag zustande – sonst alles zurück."""
            booked = None
            if result.ok:
                booked = conn.execute('SELECT Amount FROM InvoicePayments WHERE ID = ?',
                                      (result.payment_id,)).fetchone()
            if not result.ok or booked is None or booked[0] != expected:
                reason = result.message if not result.ok else 'Der zugeordnete Betrag weicht ab.'
                raise PaymentMoveFailed(
                    f'Die Zahlungen der stornierten Rechnung {old_number} ließen sich nicht übernehmen: '
                    f'{reason if reason.endswith(".") else reason + "."} Die Rechnung wurde nicht versendet.')

        payments = conn.execute(
            'SELECT ID, BookingID, Amount FROM InvoicePayments WHERE InvoiceID = ? ORDER BY PaymentDate, ID',
            (old_id,)).fetchall()
        positive = sum(amount for _pay_id, _bank_id, amount in payments if amount > 0)   # eine negative bleibt unberührt
        moved = skipped = 0
        for pay_id, bank_id, amount in payments:
            found = self._movable_payment(conn, old_id, old_number, new_id, bank_id, amount, move['bank_coas'],
                                          move['bank_coa_ids'])
            if found is None:
                skipped += max(amount, 0)
                continue
            due_new = (row['SumGross'] or 0) - conn.execute(
                'SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?', (new_id,)).fetchone()[0]
            if due_new <= 0:
                break
            account_id, revenue_coa_old = found
            part = min(amount, due_new)
            self._unassign(conn, pay_id)
            check(self._assign_in_tx(
                tx, new_id, bank_id, part,
                {'tax_rate': row['TaxRate'], 'account_id': account_id, 'bank_coa': move['bank_coas'][account_id],
                 'revenue_coa': move['revenue_coa'], 'bank_coa_ids': move['bank_coa_ids']}), part)
            if amount > part:
                check(self._assign_in_tx(
                    tx, old_id, bank_id, amount - part,
                    {'tax_rate': old_rate, 'account_id': account_id, 'bank_coa': move['bank_coas'][account_id],
                     'revenue_coa': revenue_coa_old, 'bank_coa_ids': move['bank_coa_ids']},
                    allow_cancelled=True), amount - part)
            moved += part

        kept = positive - moved - skipped
        notes = []
        if moved:
            notes.append(f'Zahlungen der stornierten Rechnung {old_number} über {euro(moved)} sind jetzt dieser '
                         'Rechnung zugeordnet.')
        else:
            notes.append(f'Es wurden keine Zahlungen der stornierten Rechnung {old_number} übernommen.')
        if kept > 0:
            notes.append(f'{euro(kept)} bleiben als Guthaben des Kunden an der stornierten Rechnung.')
        if moved and self._tax_rate_key(old_rate) != self._tax_rate_key(row['TaxRate']):
            notes.append('Der Steuersatz weicht ab – die Umsatzsteuer am Zahlungstag ändert sich.')
        if skipped:
            notes.append(f'{euro(skipped)} ließen sich nicht automatisch übernehmen (Import oder Sammelbuchung) und '
                         'bleiben an der stornierten Rechnung.')
        return moved, ' '.join(notes)

    # ── Prüfung ──────────────────────────────────────────────────────────────
    def find_payment_inconsistencies(self, invoice_id=None):
        """Unstimmigkeiten der Zahlungszuordnungen – rein lesend, ändert nichts.

        ``invoice_id`` grenzt auf eine Rechnung ein. Returns eine Liste von Befunden
        ``{'category', 'invoice_id', 'invoice_number', 'text'}`` (Klartext, nicht maskiert);
        ``invoice_id`` ist ``None``, wenn die Rechnungsnummer keiner Rechnung gehört.
        Kategorien:

        * ``no_settlement``      Zuordnung ohne Satz „Bank an Debitoren“ (die Rechnung hat
                                 eine Forderung);
        * ``settlement_without_receivable`` Sätze „Bank an Debitoren“ der App, aber keine
                                 Forderung (mehr) zur Rechnung – 10000 steht im Haben-Überhang;
        * ``receivable_attached`` die Forderung hängt an einer Bankbewegung;
        * ``orphan_payment_rows`` Zahlungssatz unter einer Bankbewegung ohne Zuordnung: die
                                 Sätze der App (Marke, Text) und bankwirksame Erlössätze mit
                                 einer Rechnungsnummer als Beleg-Nr., zu denen es für das Paar
                                 (Rechnung, Bewegung) keine Zuordnung gibt;
        * ``transfer_mismatch``  zugeordneter Betrag ≠ auf das Erlöskonto umgebuchter;
        * ``payment_on_entry``   Zuordnung hängt an einem Buchungssatz, nicht an einer
                                 Bankbewegung;
        * ``duplicate_payment``  dasselbe Paar (Rechnung, Bewegung) mehrfach zugeordnet;
        * ``outgoing_payment``   Zuordnung auf einen Ausgang;
        * ``status_unsent``      Forderung gebucht, Status aber Entwurf/abgeschlossen;
        * ``amount_due_mismatch`` offener Rest passt nicht zu den Zuordnungen;
        * ``storno_counter_mismatch`` bei einer Rechnung mit Stornorechnung steht nur eine der beiden
                                 Buchungen („Forderung“ der Rechnung, Gegenbuchung der Stornorechnung);
        * ``legacy_cancelled_open`` eine von Hand stornierte Rechnung ohne Stornorechnung, deren
                                 Forderung noch in den Büchern steht.

        ``no_settlement`` und ``transfer_mismatch`` betrachten nur Zuordnungen, deren Buchung
        eine Bankbewegung ohne Sätze mit Quellklammer (``SourceGroup``) ist: Der WISO-Import
        liefert seine Sätze selbst (und kennt Barzahlungen ohne Bankbewegung). Die beiden
        letzten Kategorien zählen nur Forderungen, die die App selbst gebucht hat
        (``_app_receivable_ids``), damit ein Import keinen Fehlalarm auslöst; ein intaktes Paar
        mit Guthaben (Zahlungen an der stornierten Rechnung) ergibt keinen Befund.

        Die Abfragen gehen von den Zuordnungen aus bzw. fassen je Rechnung vorab zusammen; die
        Rechnungsnummern mit Forderung werden einmal gelesen. Der Aufwand wächst so linear mit
        dem Bestand (4000 Bewegungen/Forderungen, 1000 Zuordnungen: unter einer Sekunde).
        """
        conn = self._get_connection()
        try:
            return self._payment_findings(conn, invoice_id)
        finally:
            conn.close()

    def _payment_findings(self, conn, invoice_id):
        debtor = self.DEBTOR_ACCOUNT_NUMBER
        waiting = tuple(number for number, _name in self.OPEN_REVENUE_ACCOUNTS.values())
        cursor = conn.cursor()
        is_invoice = "(i.DocumentType = 'invoice' OR i.DocumentType IS NULL)"
        only = ' AND i.ID = ?' if invoice_id is not None else ''
        only_params = [invoice_id] if invoice_id is not None else []
        # Bankbewegung mit Sätzen aus dem Quellsystem (WISO): die Sätze gehören dem Import
        has_source_group = ("EXISTS (SELECT 1 FROM Bookings w WHERE w.ParentBooking_ID = b.ID "
                            "AND w.SourceGroup IS NOT NULL AND w.SourceGroup <> '')")
        findings = []

        def add(category, inv_id, number, text):
            findings.append({'category': category, 'invoice_id': inv_id,
                             'invoice_number': number or '', 'text': text})

        def euro(minor):
            return fmt_eur(from_minor(minor or 0))

        # Rechnungsnummern, zu denen eine Forderung gebucht ist (``receivable_condition``),
        # einmal gelesen – nicht je Zuordnung nachgeschlagen
        receivable_sql, receivable_params = self._receivable_sql(cursor, alias='r')
        number_filter, number_params = '', []
        if invoice_id is not None:
            row = conn.execute('SELECT InvoiceNumber FROM Invoices WHERE ID = ?',
                               (invoice_id,)).fetchone()
            if row is None:
                return findings
            number_filter, number_params = ' AND r.DocumentNumber = ?', [row[0]]
        with_receivable = {row[0] for row in conn.execute(
            f"SELECT DISTINCT r.DocumentNumber FROM Bookings r WHERE r.BookingType = 'entry' "
            f"AND r.DocumentNumber IS NOT NULL AND {receivable_sql}{number_filter}",
            receivable_params + number_params)}

        for inv, number, pay_id, bank_id, amount in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, ip.ID, ip.BookingID, ip.Amount "
                "FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                "JOIN Bookings b ON b.ID = ip.BookingID AND b.BookingType = 'bank' "
                "WHERE NOT EXISTS (SELECT 1 FROM Bookings s WHERE s.ParentBooking_ID = b.ID "
                "AND s.DocumentNumber = i.InvoiceNumber AND s.CounterCOA_ID IN "
                f"(SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?)) "
                f"AND NOT {has_source_group}{only} ORDER BY i.InvoiceNumber, ip.ID",
                [debtor] + only_params):
            if number in with_receivable:
                add('no_settlement', inv, number,
                    f'Zuordnung #{pay_id} ({euro(amount)}) auf Bewegung #{bank_id}: Der '
                    f'Buchungssatz „Bank an Debitoren“ fehlt.')

        for inv, number, count, total in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, COUNT(*), SUM(k.Amount) FROM Bookings k "
                f"JOIN Invoices i ON i.InvoiceNumber = k.DocumentNumber AND {is_invoice} "
                "WHERE k.Status = ? AND k.ParentBooking_ID IS NOT NULL AND k.CounterCOA_ID IN "
                f"(SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?){only} "
                "GROUP BY i.ID, i.InvoiceNumber ORDER BY i.InvoiceNumber",
                [PAYMENT_MARK, debtor] + only_params):
            if number not in with_receivable:
                add('settlement_without_receivable', inv, number,
                    f'Es gibt {count} Zahlungssatz(sätze) „Bank an Debitoren“ ({euro(total)}), '
                    f'aber keine Forderung mehr zur Rechnung – auf dem Debitorenkonto steht ein '
                    f'Haben-Überhang.')

        for rec_id, number, bank_id, amount, inv in conn.execute(
                "SELECT r.ID, r.DocumentNumber, r.ParentBooking_ID, r.Amount, "
                "(SELECT i.ID FROM Invoices i WHERE i.InvoiceNumber = r.DocumentNumber) "
                f"FROM Bookings r WHERE r.BookingType = 'entry' AND r.ParentBooking_ID IS NOT NULL "
                f"AND {receivable_sql} ORDER BY r.ID", receivable_params):
            if invoice_id is None or inv == invoice_id:
                add('receivable_attached', inv, number,
                    f'Die Forderung #{rec_id} ({euro(amount)}) hängt an der Bankbewegung '
                    f'#{bank_id}; sie gehört zur Rechnung, nicht zur Bewegung.')

        # Zahlungssätze ohne Zuordnung: die Marke oder der Text der App – oder ein bankwirksamer
        # Erlössatz (Erlöskonto gegen ein Zahlungskonto) mit einer Rechnungsnummer als Beleg-Nr.,
        # unabhängig vom Text (Altbestand „Zahlung verbuchen mit Konto“ nach dem Entfernen)
        revenue_ids = [row[0] for row in self._income_coas(cursor)]
        payment_ids = sorted(self._get_payment_coa_ids(cursor))
        rev = ','.join('?' * len(revenue_ids))
        pay = ','.join('?' * len(payment_ids))
        for row_id, bank_id, number, amount, inv in conn.execute(
                "SELECT k.ID, k.ParentBooking_ID, k.DocumentNumber, k.Amount, "
                "(SELECT i.ID FROM Invoices i WHERE i.InvoiceNumber = k.DocumentNumber) "
                "FROM Bookings k WHERE k.ParentBooking_ID IS NOT NULL "
                "AND (k.SourceGroup IS NULL OR k.SourceGroup = '') "
                "AND (k.Status = ? OR k.Text LIKE 'Zahlung zu Re. %' OR k.Text LIKE 'Umb. zu Re. %' "
                "OR (k.BookingType = 'entry' "
                f"AND ((k.COA_ID IN ({rev}) AND k.CounterCOA_ID IN ({pay})) "
                f"OR (k.CounterCOA_ID IN ({rev}) AND k.COA_ID IN ({pay}))) "
                "AND EXISTS (SELECT 1 FROM Bookings p WHERE p.ID = k.ParentBooking_ID "
                "AND p.BookingType = 'bank') "
                "AND EXISTS (SELECT 1 FROM Invoices x WHERE x.InvoiceNumber = k.DocumentNumber "
                "AND (x.DocumentType = 'invoice' OR x.DocumentType IS NULL)))) "
                "AND NOT EXISTS (SELECT 1 FROM InvoicePayments ip "
                "JOIN Invoices i2 ON i2.ID = ip.InvoiceID WHERE ip.BookingID = k.ParentBooking_ID "
                "AND i2.InvoiceNumber = k.DocumentNumber) ORDER BY k.ID",
                [PAYMENT_MARK] + revenue_ids + payment_ids + revenue_ids + payment_ids):
            if invoice_id is None or inv == invoice_id:
                add('orphan_payment_rows', inv, number,
                    f'Zahlungssatz #{row_id} ({euro(amount)}) unter Bewegung #{bank_id} hat '
                    f'keine Zuordnung zur Rechnung.')

        # Zugeordnet (Bankbewegungen ohne Sätze des Imports) gegen umgebucht (Sätze „Wartekonto
        # an Erlöskonto“ der App/des Altbestands), je Rechnung vorab zusammengefasst
        marks = ','.join('?' * len(waiting))
        for inv, number, paid, moved in conn.execute(
                "WITH paid AS (SELECT ip.InvoiceID AS InvoiceID, SUM(ip.Amount) AS Total "
                "FROM InvoicePayments ip JOIN Bookings b ON b.ID = ip.BookingID "
                f"WHERE b.BookingType = 'bank' AND NOT {has_source_group} "
                "GROUP BY ip.InvoiceID), "
                "moved AS (SELECT k.DocumentNumber AS Number, SUM(k.Amount) AS Total "
                "FROM Bookings k JOIN ChartOfAccounts c ON c.ID = k.COA_ID "
                "WHERE k.ParentBooking_ID IS NOT NULL AND (k.SourceGroup IS NULL OR k.SourceGroup = '') "
                f"AND c.AccountNumber IN ({marks}) GROUP BY k.DocumentNumber) "
                "SELECT i.ID, i.InvoiceNumber, COALESCE(paid.Total, 0), COALESCE(moved.Total, 0) "
                "FROM Invoices i LEFT JOIN paid ON paid.InvoiceID = i.ID "
                "LEFT JOIN moved ON moved.Number = i.InvoiceNumber "
                f"WHERE {is_invoice}{only} AND COALESCE(paid.Total, 0) <> COALESCE(moved.Total, 0) "
                "ORDER BY i.InvoiceNumber", list(waiting) + only_params):
            if number in with_receivable:
                add('transfer_mismatch', inv, number,
                    f'Zugeordnet sind {euro(paid)}, auf das Erlöskonto umgebucht {euro(moved)}.')

        for inv, number, pay_id, booking_id, amount in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, ip.ID, b.ID, ip.Amount "
                "FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                "JOIN Bookings b ON b.ID = ip.BookingID "
                "WHERE COALESCE(b.BookingType, 'entry') <> 'bank' "
                f"AND (b.SourceGroup IS NULL OR b.SourceGroup = ''){only} "
                "ORDER BY i.InvoiceNumber, ip.ID", only_params):
            add('payment_on_entry', inv, number,
                f'Zuordnung #{pay_id} ({euro(amount)}) hängt am Buchungssatz #{booking_id}, '
                f'nicht an einer Bankbewegung.')

        for inv, number, bank_id, count, total in conn.execute(
                "SELECT ip.InvoiceID, i.InvoiceNumber, ip.BookingID, COUNT(*), SUM(ip.Amount) "
                "FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                f"WHERE 1 = 1{only} GROUP BY ip.InvoiceID, ip.BookingID HAVING COUNT(*) > 1 "
                "ORDER BY i.InvoiceNumber", only_params):
            add('duplicate_payment', inv, number,
                f'Bewegung #{bank_id} ist {count}-mal zugeordnet (zusammen {euro(total)}).')

        for inv, number, pay_id, bank_id, bank_amount, amount in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, ip.ID, b.ID, b.Amount, ip.Amount "
                "FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                f"JOIN Bookings b ON b.ID = ip.BookingID WHERE b.Amount < 0 AND ip.Amount > 0{only} "
                "ORDER BY i.InvoiceNumber, ip.ID", only_params):
            add('outgoing_payment', inv, number,
                f'Zuordnung #{pay_id}: Bewegung #{bank_id} ist ein Ausgang ({euro(bank_amount)}), '
                f'zugeordnet sind {euro(amount)}.')

        for inv, number, status in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, i.Status FROM Invoices i "
                f"WHERE i.Status IN ('draft', 'finalized') AND {is_invoice}{only} "
                "ORDER BY i.InvoiceNumber", only_params):
            if number in with_receivable:
                add('status_unsent', inv, number,
                    f'Die Rechnung steht als Forderung in den Büchern, hat aber noch den Status '
                    f'„{status}“ (nicht versendet).')

        for inv, number, gross, due, paid, kind in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, i.SumGross, i.AmountDue, "
                "COALESCE((SELECT SUM(Amount) FROM InvoicePayments WHERE InvoiceID = i.ID), 0), "
                "i.InvoiceKind "
                f"FROM Invoices i WHERE {is_invoice}{only} ORDER BY i.InvoiceNumber",
                only_params):
            # Der Rest einer Stornorechnung ist ihr negativer Betrag (es gibt keine Zahlung): kein Befund
            overpaid = due is not None and due < 0 and kind != STORNO_KIND
            if due is None or overpaid or due != (gross or 0) - paid:
                add('amount_due_mismatch', inv, number,
                    f'Offen sind {euro(due)}, nach den Zuordnungen müssten es '
                    f'{euro((gross or 0) - paid)} sein.')

        # Storno-Paare: Die Forderung der Rechnung und die Gegenbuchung der Stornorechnung gehören zusammen
        pair_only = ' AND (o.ID = ? OR s.ID = ?)' if invoice_id is not None else ''
        for original_id, original_number, storno_number in conn.execute(
                "SELECT o.ID, o.InvoiceNumber, s.InvoiceNumber FROM Invoices s "
                "JOIN Invoices o ON o.ID = s.CancelsInvoiceId "
                f"WHERE s.InvoiceKind = ?{pair_only} ORDER BY o.InvoiceNumber, s.ID",
                [STORNO_KIND, *only_params, *only_params]).fetchall():
            has_receivable = bool(self._app_receivable_ids(conn, original_number))
            has_counter = bool(self._app_receivable_ids(conn, storno_number))
            if has_receivable and not has_counter:
                add('storno_counter_mismatch', original_id, original_number,
                    f'Zur Rechnung steht die Forderung, aber die Gegenbuchung der Stornorechnung '
                    f'{storno_number} fehlt – auf dem Debitorenkonto bleibt der Betrag stehen.')
            elif has_counter and not has_receivable:
                add('storno_counter_mismatch', original_id, original_number,
                    f'Die Stornorechnung {storno_number} hat eine Gegenbuchung, aber zur Rechnung steht '
                    f'keine Forderung der App – auf dem Debitorenkonto steht ein Haben-Überhang.')

        # Von Hand storniert (Altbestand), keine Stornorechnung – und die Forderung steht noch in den Büchern
        for inv, number in conn.execute(
                "SELECT i.ID, i.InvoiceNumber FROM Invoices i WHERE i.Status = 'cancelled' "
                "AND COALESCE(i.SumGross, 0) > 0 AND i.InvoiceKind IS NOT ? "
                f"AND NOT EXISTS (SELECT 1 FROM Invoices s WHERE s.CancelsInvoiceId = i.ID) AND {is_invoice}{only} "
                "ORDER BY i.InvoiceNumber", [STORNO_KIND, *only_params]).fetchall():
            receivable = self._app_receivable_ids(conn, number)
            if receivable:
                booked = conn.execute(
                    f"SELECT COALESCE(SUM(Amount), 0) FROM Bookings WHERE ID IN ({','.join('?' * len(receivable))})",
                    receivable).fetchone()[0]
                add('legacy_cancelled_open', inv, number,
                    f'Die Rechnung ist storniert, ihre Forderung steht aber noch in den Büchern ({euro(booked)}). '
                    f'Abhilfe: „Stornorechnung nachträglich erstellen“.')
        return findings
