# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Zahlungszuordnung Rechnung ↔ Bankbewegung.

Eine Zuordnung besteht aus genau einer InvoicePayments-Zeile und den Buchungssätzen, die die
App für sie erzeugt hat. Diese Sätze sind eindeutig bestimmt durch

* ``ParentBooking_ID`` = die Bankbewegung der Zuordnung,
* ``DocumentNumber``   = die Rechnungsnummer,
* ``Status``           = ``PAYMENT_MARK`` (``'invoice_payment'``).

Es gibt genau EINE Stelle, die Zahlungen zuordnet (``assign_invoice_payment``: Zahlungseingang auf eine
Rechnung), EINE, die eine Stornorechnung erstattet (``assign_storno_refund``: Zahlungsausgang auf eine
Stornorechnung, seit 2.2.3), und EINE, die Zuordnungen entfernt (``unassign_invoice_payment``);
``delete_transaction`` und der WISO-Import gehen über dieselben Bausteine. Anlegen und Entfernen laufen in
einer Transaktion mit ``BEGIN IMMEDIATE``: Prüfung, Schreiben und Neuberechnung der Rechnung sehen denselben
Stand – zwei gleichzeitige Anfragen legen das Paar (Rechnung, Bankbewegung) nie zweimal an.
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

# Die Erstattung einer Stornorechnung (``assign_storno_refund``) und der Schutz des Guthabens
REFUND_ONLY_STORNO_MESSAGE = 'Erstatten lässt sich nur auf eine Stornorechnung.'
REFUND_ONLY_OUTGOING_MESSAGE = 'Nur Zahlungsausgänge lassen sich als Erstattung zuordnen.'
REFUND_NO_CREDIT_MESSAGE = 'Zu dieser Stornorechnung gibt es kein Guthaben zu erstatten.'
REFUND_EXHAUSTED_MESSAGE = 'Kein offener Betrag zuordenbar (Guthaben oder Buchung ausgeschöpft)'
ALREADY_REFUNDED_MESSAGE = ('Zu dieser Rechnung wurde bereits erstattet – erst die Erstattung an der Stornorechnung '
                            'entfernen.')
MOVE_REFUNDED_HINT = ('Die Zahlungen der stornierten Rechnung {number} bleiben dort: Zu ihr wurde bereits erstattet. '
                      'Bitte von Hand zuordnen.')

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

# Die Spalten, die ``_assign_in_tx`` für jeden Satz einer Zuordnung schreibt, in der Reihenfolge des INSERT (dazu die festen
# Werte BookingType 'entry' und AutoMirror 0). Daran erkennt der Umzug der Zahlungen beim Versenden einer Ersatzrechnung, ob
# die Sätze einer Zuordnung noch die der App sind (``payment_rows``, ``InvoicePaymentsMixin._same_payment_row``).
PAYMENT_ROW_COLUMNS = ('DateBooking', 'DateTax', 'RecipientClient', 'Contact_ID', 'COA_ID', 'CounterCOA_ID', 'Amount',
                       'Currency', 'TaxRate', 'TaxAmount', 'Text', 'DocumentNumber', 'ParentBooking_ID', 'Status',
                       'EntryForm')
# Dazu prüft der Vergleich: BookingType (fest 'entry') und was die App an so einem Satz nie schreibt – er muss leer sein, gesetzt
# hat es ein Nutzer oder ein Import
_PAYMENT_ROW_UNWRITTEN = ('BookingType', 'Account_ID', 'ForeignBankAccount', 'Category_ID', 'SourceGroup')
_PAYMENT_ROW_INSERT = ('INSERT INTO Bookings (DateBooking, DateTax, RecipientClient, Contact_ID, '
                       'COA_ID, CounterCOA_ID, Amount, Currency, TaxRate, TaxAmount, Text, '
                       "DocumentNumber, BookingType, ParentBooking_ID, AutoMirror, Status, "
                       "EntryForm) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'entry', ?, 0, ?, ?)")

# Ein Satz einer Zuordnung: ``values`` {Spalte: Wert} für ``PAYMENT_ROW_COLUMNS``, ``description`` für das Replay-Log
PaymentRow = namedtuple('PaymentRow', 'values description')


def payment_rows(*, number, customer_id, bank_id, bank_date, bank_tax_date, recipient, currency, amount, tax_rate,
                 tax_value, bank_coa, revenue_coa, debtor_side=None, waiting_side=None):
    """Die Buchungssätze, die die App für eine Zuordnung schreibt – die EINE Stelle, die ihre Werte festlegt: ``_assign_in_tx``
    schreibt sie, der Umzug der Zahlungen vergleicht den Bestand damit.

    ``number``: Nummer der Rechnung (Beleg-Nr.), ``customer_id``: ihr Kunde, ``bank_id``/``bank_date``/``bank_tax_date``/
    ``recipient``/``currency``: die Bankbewegung, ``amount``: der zugeordnete Betrag (Minor Units; bei der Erstattung einer
    Stornorechnung, ``assign_storno_refund``, negativ – ebenso ``tax_value``, und ``number`` ist dann die Nummer der
    Stornorechnung), ``tax_rate`` (als
    Bruchteil, ``None`` ohne Steuer) und ``tax_value`` (Minor Units) die Steuer der Zuordnung, ``bank_coa``/``revenue_coa``:
    Konto der Bank und Erlöskonto (COA-IDs). Mit Forderung (``debtor_side`` und ``waiting_side`` gegeben) sind es „Bank an
    Debitoren“ (``liquid_first``, ohne Steuer) und „Wartekonto an Erlöskonto“ (``transfer``, mit Steuer), ohne Forderung
    der Einzelsatz „Erlöskonto an Bank“ (``purpose_first``, mit Steuer). Die Formen stehen fest, nicht aus der Kontenliste
    abgeleitet."""
    def row(coa_id, counter_coa_id, form, text, taxed, description):
        return PaymentRow({
            'DateBooking': bank_date, 'DateTax': bank_tax_date, 'RecipientClient': recipient or '',
            'Contact_ID': customer_id, 'COA_ID': coa_id, 'CounterCOA_ID': counter_coa_id, 'Amount': amount,
            'Currency': currency or 'EUR', 'TaxRate': tax_rate if taxed else None,
            'TaxAmount': tax_value if taxed else None, 'Text': text, 'DocumentNumber': number,
            'ParentBooking_ID': bank_id, 'Status': PAYMENT_MARK, 'EntryForm': form}, description)

    # Eine Zahlung ist positiv, eine Erstattung negativ – der Text nennt, was es ist (die Zahlungssätze bleiben wortgleich)
    paid, moved = ('Zahlung', 'Umb.') if amount >= 0 else ('Erstattung', 'Umb. Erstattung')
    if debtor_side and waiting_side:
        return [row(bank_coa, debtor_side, FORM_LIQUID_FIRST, f'{paid} zu Re. {number}', False,
                    'Payment child (bank to debtor)'),
                row(waiting_side, revenue_coa, FORM_TRANSFER, f'{moved} zu Re. {number}', True,
                    'Payment child (open item to revenue)')]
    return [row(revenue_coa, bank_coa, FORM_PURPOSE_FIRST, f'{paid} zu Re. {number}', True,
                'Payment child (revenue to bank)')]


# Warum eine Zuordnung beim Umzug auf die Ersatzrechnung an der stornierten Rechnung bleibt: Kennung → Klartext. In dieser
# Reihenfolge nennt der Hinweis die vorkommenden Gründe, je Grund einmal.
MOVE_SKIP_REASONS = {
    'import': 'Zuordnung aus einem Import',
    'unmarked': 'die Buchungssätze der Zahlung stammen aus einer älteren Version oder wurden von Hand angelegt',
    'no_skr': 'das Bankkonto hat keine SKR-Nummer',
    'overbooked': 'die Bankbewegung ist überbucht',
    'edited': 'die Buchungssätze der Zahlung wurden von Hand geändert oder ergänzt oder tragen einen Beleg',
    'tax_date': ('die Buchungssätze der Zahlung tragen ein anderes Steuerdatum als die Bankbewegung '
                 '(z. B. nach einem DATEV-Export)'),
    'revenue_account': 'das Erlöskonto der Zahlung ist nicht das heute für den Steuersatz hinterlegte',
    'multiple': 'die Bankbewegung ist mehrfach zugeordnet',
    'no_incoming': 'keine Bankbewegung mit Zahlungseingang',
    'rebook': 'die Zuordnung ließ sich nicht neu buchen',
}

# Die stornierte Rechnung, wie der Umzug sie braucht: ID, Nummer, Brutto und Steuer (Minor Units), Steuersatz (wie in
# ``Invoices.TaxRate``) und Kunde
_Replaced = namedtuple('_Replaced', 'id number gross tax_total rate customer_id')

# Eine geprüfte und ausgerechnete Erstattung (``assign_storno_refund``): Nummer und Kunde der Stornorechnung, die stornierte
# Rechnung und ihr Steuersatz (wie in ``Invoices.TaxRate``), die Bankbewegung (Konto, Datum, Steuerdatum, Empfänger, Währung),
# der erstattete Betrag und seine Steuer (positiv, Minor Units; ``tax`` ist ``None`` ohne Steuersatz) und das Erlöskonto der
# Zahlungen (``None``: keine Zuordnung mit markierten Sätzen)
_Refund = namedtuple('_Refund', 'number customer_id original_id rate account_id bank_date bank_tax_date recipient '
                                'currency amount tax revenue_coa')


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
    """Ergebnis von ``assign_invoice_payment`` und ``assign_storno_refund``: ob die Zuordnung
    zustande kam, bei einer Ablehnung der Klartext für die Oberfläche (sonst ``None``), die ID
    der Zuordnung und das Erlöskonto (COA-ID), auf das gebucht wurde."""
    __slots__ = ()


def _refused(message):
    return PaymentAssignment(False, message, None, None)


class PaymentMoveFailed(ValueError):
    """Der Umzug der Zahlungen einer stornierten Rechnung auf ihre Ersatzrechnung ist an etwas Unerwartetem gescheitert (ein
    Datenbankfehler mitten im Umzug); ``str(exc)`` ist der Klartext. ``Database.set_invoice_status`` macht daraus eine Ablehnung
    und rollt damit alles zurück, was der Wechsel in seiner Transaktion geschrieben hat (``db.invoice_lock._Refusal`` ist von
    hier aus nicht erreichbar: Zirkelimport). Eine Zuordnung, die sich nur nicht neu buchen lässt, ist dagegen erwartet: Sie
    bleibt an der stornierten Rechnung, und der Versand gelingt (``_move_replaced_payments``)."""


class PaymentRemovalRefused(ValueError):
    """Das Entfernen einer Zuordnung (``unassign_invoice_payment``) wird abgelehnt, weil das Guthaben eines Storno-Paares
    sonst negativ würde (mehr erstattet als gezahlt); ``str(exc)`` ist der Klartext für die Oberfläche."""


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
    def _bank_effective_sql(bank_coa_ids):
        """SQL-Bedingung (ohne ``AND``) und Parameter: Der Buchungssatz ``k`` ist bankwirksam – er berührt ein eigenes
        Bankkonto oder ist nicht ganz kontiert (``db.matching.is_bank_effective``). Die EINE Bedingung für den freien Rest
        einer Bankbewegung, ob Eingang (``_free_minor_sql``) oder Ausgang (``_free_outgoing_minor_sql``)."""
        marks = ','.join('?' * len(bank_coa_ids))
        return (f'(k.COA_ID IS NULL OR k.CounterCOA_ID IS NULL '
                f'OR k.COA_ID IN ({marks}) OR k.CounterCOA_ID IN ({marks}))'), list(bank_coa_ids) * 2

    @staticmethod
    def _free_minor_sql(bank_coa_ids, alias='b'):
        """SQL-Ausdruck (und Parameter) für den freien Rest einer Bankbewegung in Minor Units:

            Betrag − MAX(Σ Zuordnungen, Σ bankwirksame Buchungssätze darunter)

        Die EINE Formel für Zuordnung, Kandidatenlisten und Kontakt-Guthaben. Der Rest kann
        negativ sein (überbucht). Bankwirksam: ``db.matching.is_bank_effective``. Für einen Ausgang gilt
        ``_free_outgoing_minor_sql``."""
        effective, params = InvoicePaymentsMixin._bank_effective_sql(bank_coa_ids)
        expr = (f'{alias}.Amount - MAX('
                f'COALESCE((SELECT SUM(ip.Amount) FROM InvoicePayments ip '
                f'WHERE ip.BookingID = {alias}.ID), 0), '
                f'COALESCE((SELECT SUM(k.Amount) FROM Bookings k '
                f'WHERE k.ParentBooking_ID = {alias}.ID '
                f'AND {effective}), 0))')
        return expr, params

    @staticmethod
    def _free_outgoing_minor_sql(bank_coa_ids, alias='b'):
        """SQL-Ausdruck (und Parameter) für den freien Rest einer ausgehenden Bankbewegung (Betrag < 0) in Minor Units,
        als positive Zahl:

            |Betrag| − MAX(−Σ Zuordnungen, −Σ bankwirksame Buchungssätze darunter)

        Die Formel für die Erstattung einer Stornorechnung (``assign_storno_refund``) und ihre Kandidatenliste; die
        Bedingung „bankwirksam“ ist dieselbe wie in ``_free_minor_sql``. Der Rest kann negativ sein (überbucht)."""
        effective, params = InvoicePaymentsMixin._bank_effective_sql(bank_coa_ids)
        expr = (f'-{alias}.Amount - MAX('
                f'-COALESCE((SELECT SUM(ip.Amount) FROM InvoicePayments ip '
                f'WHERE ip.BookingID = {alias}.ID), 0), '
                f'-COALESCE((SELECT SUM(k.Amount) FROM Bookings k '
                f'WHERE k.ParentBooking_ID = {alias}.ID '
                f'AND {effective}), 0))')
        return expr, params

    def _booking_free_minor(self, conn, booking_id, bank_coa_ids):
        """Freier Rest einer Bankbewegung in Minor Units (siehe ``_free_minor_sql``)."""
        expr, params = self._free_minor_sql(bank_coa_ids)
        row = conn.execute(f'SELECT {expr} FROM Bookings b WHERE b.ID = ?',
                           params + [booking_id]).fetchone()
        return row[0] if row and row[0] is not None else 0

    def _booking_free_outgoing_minor(self, conn, booking_id, bank_coa_ids):
        """Freier Rest einer ausgehenden Bankbewegung in Minor Units, positiv (siehe ``_free_outgoing_minor_sql``)."""
        expr, params = self._free_outgoing_minor_sql(bank_coa_ids)
        row = conn.execute(f'SELECT {expr} FROM Bookings b WHERE b.ID = ?',
                           params + [booking_id]).fetchone()
        return row[0] if row and row[0] is not None else 0

    @staticmethod
    def _booking_tax_rate(rate):
        """Der Steuersatz, den die Sätze einer Zuordnung tragen, aus ``Invoices.TaxRate``: Der steht teils als 0.19, teils
        als 19; -1 ist der Sentinel für „§ 19, keine USt“ (wie 0 oder kein Satz: ``None``)."""
        return None if not rate or rate < 0 else (rate if rate <= 1 else rate / 100)

    @staticmethod
    def _prorated_tax(tax_total, amount, gross):
        """Die Steuer eines Teils der Rechnung: anteilig am Betrag, auf den Cent gerundet (halb auf)."""
        return round_minor(prorate(tax_total, amount, gross), 2)

    def _allocations_tax(self, conn, invoice_id, number, gross, tax_total, except_booking=None):
        """Die Steuer, die die Zuordnungen einer Rechnung tragen: Hat eine markierte Sätze, zählt deren gebuchte Steuer (nur
        die Zahlungssätze tragen Steuer, nicht „Bank an Debitoren“); sonst (Altbestand, WISO-Import) ihr anteiliger Betrag –
        eine Suche über die Beleg-Nr. fände Sätze mit anderer oder fehlender Nummer nicht. ``except_booking``: eine
        Bankbewegung, deren Zuordnung nicht mitzählt. Die EINE Rechenstelle für den schließenden Teil einer Zahlung
        (``_remaining_tax``) und die Erstattung einer Stornorechnung (``assign_storno_refund``)."""
        skip, params = ((' AND BookingID <> ?', (invoice_id, except_booking)) if except_booking is not None
                        else ('', (invoice_id,)))
        taxed = 0
        for other_booking, other_amount in conn.execute(
                f'SELECT BookingID, SUM(Amount) FROM InvoicePayments WHERE InvoiceID = ?{skip} GROUP BY BookingID',
                params).fetchall():
            marked = self._payment_children(conn, other_booking, number)
            if marked:
                taxed += conn.execute(
                    f"SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE ID IN "
                    f"({','.join('?' * len(marked))})", marked).fetchone()[0]
            else:
                taxed += self._prorated_tax(tax_total, other_amount, gross)
        return taxed

    def _remaining_tax(self, conn, invoice_id, number, gross, tax_total, except_booking=None):
        """Die Steuer des Teils, der die Rechnung schließt: Rechnungssteuer minus die Steuer der anderen Zuordnungen
        (``_allocations_tax``). ``except_booking``: eine Bankbewegung, deren Zuordnung nicht mitzählt – so fragt der Umzug,
        was diese Zuordnung als schließender Teil bekommen hätte."""
        return tax_total - self._allocations_tax(conn, invoice_id, number, gross, tax_total, except_booking)

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

    def _bank_coa_of(self, account_id, what='Zahlung'):
        """Das SKR-Konto (COA-ID) des Bankkontos einer Bewegung: ``(Fehlertext, None)`` oder ``(None, COA-ID)``. Ohne
        SKR-Nummer lässt sich nichts kontieren. ``what``: das Wort in der Meldung ('Zahlung' oder 'Erstattung')."""
        account = self.get_account_by_id(account_id) if account_id else None
        if account is None:
            return f'Für die {what} fehlt das Bankkonto.', None
        if not account[7]:
            return ('Das Bankkonto hat keine SKR-Nummer – bitte in den Stammdaten ergänzen, '
                    f'sonst lässt sich die {what} nicht kontieren.'), None
        bank_coa = self.get_coa_id_by_account_number(account[7])
        if bank_coa is None:
            return f'Das SKR-Konto {account[7]} des Bankkontos fehlt im Kontenrahmen.', None
        return None, bank_coa

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
        error, bank_coa = self._bank_coa_of(account_id)
        if error:
            return error, None
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
        Rechnungen; das Bankkonto braucht eine SKR-Nummer. Negative Zuordnungen gibt es sonst nur
        im WISO-Import (``link_invoice_to_transaction``); die Erstattung einer Stornorechnung
        legt ``assign_storno_refund`` an.

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
        # Der schließende Teil bekommt den Rest der Rechnungssteuer, jeder andere den anteiligen Betrag
        if alloc == due:
            tax_part = self._remaining_tax(conn, invoice_id, number, gross, tax_total)
        else:
            tax_part = self._prorated_tax(tax_total, alloc, gross)
        tax_rate = self._booking_tax_rate(rate)
        tax_value = tax_part if tax_rate else None

        debtor_side, waiting_side = self._receivable_sides(conn, number)

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

        # Die Formen stehen fest, nicht aus der Kontenliste abgeleitet: „Bank an Debitoren“ hat das Geldkonto vorne,
        # „Wartekonto an Erlöskonto“ ist eine Umbuchung, „Erlöskonto an Bank“ das Zweckkonto vorne (``payment_rows``).
        for row in payment_rows(number=number, customer_id=customer_id, bank_id=booking_id, bank_date=bank_date,
                                bank_tax_date=bank_tax_date, recipient=recipient, currency=currency, amount=alloc,
                                tax_rate=tax_rate, tax_value=tax_value, bank_coa=ctx['bank_coa'],
                                revenue_coa=ctx['revenue_coa'], debtor_side=debtor_side, waiting_side=waiting_side):
            params = tuple(row.values[column] for column in PAYMENT_ROW_COLUMNS)
            conn.execute(_PAYMENT_ROW_INSERT, params)
            tx.log.append((_PAYMENT_ROW_INSERT, params, row.description))

        self._recalc_invoice(conn, invoice_id)
        return PaymentAssignment(True, None, payment_id, ctx['revenue_coa'])

    # ── Erstattung einer Stornorechnung ──────────────────────────────────────
    def assign_storno_refund(self, storno_id, booking_id, amount=None, revenue_coa_id=None):
        """Eine ausgehende Bankbewegung (ganz oder teilweise) als Erstattung einer Stornorechnung zuordnen.

        Eigener Weg neben ``assign_invoice_payment``. Das Guthaben des Kunden – Σ Zuordnungen beider Belege des Paares
        (stornierte Rechnung und Stornorechnung, ``get_storno_credit``) – gleicht die Erstattung aus: Sie ist die Zuordnung
        eines Zahlungsausgangs (Bewegung mit Betrag < 0) an der Stornorechnung, mit negativem Betrag. Der erstattete Betrag
        ist min(Guthaben, freier Rest der Bewegung, ``amount``); ``amount=None`` heißt „so viel wie möglich“. Der freie Rest
        eines Ausgangs ist ``|Betrag| − MAX(−Σ Zuordnungen, −Σ bankwirksame Sätze)`` (``_free_outgoing_minor_sql``), eine
        Sammel-Überweisung also mehrere Erstattungen derselben Bewegung. Das Bankkonto braucht eine SKR-Nummer; der Betrag
        wird auf Cent gerundet (halb auf), bevor er gekappt wird.

        Geschrieben werden die InvoicePayments-Zeile (an der Stornorechnung, Betrag −r) und die Buchungssätze, alle mit
        Datum, Steuerdatum, Empfänger und Währung der Bewegung, der **Nummer der Stornorechnung** als Beleg-Nr. und der Marke
        ``PAYMENT_MARK`` – über ``payment_rows``, dieselbe Stelle wie die Zuordnung einer Zahlung, mit negativem Betrag:
        Hat die Stornorechnung eine Gegenbuchung (``_receivable_sides``), ``Bank an Debitoren`` (ohne Steuer) und
        ``Wartekonto an Erlöskonto`` (mit Steuer), sonst der Einzelsatz ``Erlöskonto an Bank``.

        Die Steuer nimmt zurück, was die Zahlungen an der stornierten Rechnung gebucht haben (``_allocations_tax``): Gleicht
        die Erstattung das Guthaben ganz aus, ist es der Rest – die Steuer der Zahlungen abzüglich der früherer Erstattungen –,
        sonst der anteilige Betrag (Cent, halb auf; nie mehr als der Rest). Die Summe über alle Erstattungen ist so genau die
        Steuer der Zahlungen. Ohne Steuersatz (§ 19, 0 %) gibt es keine.

        Das Erlöskonto ist das der Zahlungen (``_payment_revenue_coa`` über die markierten Sätze ihrer jüngsten Zuordnung); hat
        keine Zuordnung markierte Sätze (Import), das für den Steuersatz hinterlegte (``resolve_revenue_coa``), und gibt es
        auch das nicht, wählt der Nutzer es (``revenue_coa_id``, ein Erlöskonto, sonst Ablehnung). Konten, die das Auflösen
        anlegen könnte, entstehen vor der Transaktion in eigenen Verbindungen – erst, wenn die Erstattung sich buchen lässt.

        Abgelehnt wird mit Klartext, nichts geschrieben: keine Stornorechnung, keine ausgehende Bankbewegung, das Paar ist
        schon zugeordnet, es gibt kein Guthaben, die Bewegung ist ausgeschöpft, das Bankkonto hat keine SKR-Nummer, kein
        Erlöskonto. Geprüft wird vor der Transaktion und, mit frisch gelesenem Stand, in ihr (``BEGIN IMMEDIATE``).

        Returns ``PaymentAssignment(ok, message, payment_id, revenue_coa_id)`` wie ``assign_invoice_payment``."""
        requested = None
        if amount is not None:
            requested = round_minor(to_minor(amount), 2)
            if requested <= 0:
                return _refused('Der Betrag muss größer 0 sein.')
        bank_coa_ids = self.get_bank_coa_ids()

        # Vorprüfung ohne Schreibsperre: abgelehnt wird, bevor ein Konto angelegt wird
        conn = self._get_connection()
        try:
            refusal, plan = self._refund_plan(conn, storno_id, booking_id, requested, bank_coa_ids)
        finally:
            conn.close()
        if refusal:
            return _refused(refusal)
        error, bank_coa = self._bank_coa_of(plan.account_id, 'Erstattung')
        if error:
            return _refused(error)
        fallback = None
        if plan.revenue_coa is None:
            fallback = self.resolve_revenue_coa(plan.rate)
            if fallback is None:
                fallback, error = self._chosen_revenue_coa(revenue_coa_id)
                if error:
                    return _refused(error)
        account_id, rate = plan.account_id, plan.rate

        with self.transaction() as tx:
            conn = tx.conn
            conn.execute('BEGIN IMMEDIATE')
            refusal, plan = self._refund_plan(conn, storno_id, booking_id, requested, bank_coa_ids)
            if refusal:
                return _refused(refusal)
            revenue_coa = plan.revenue_coa or fallback
            if plan.rate != rate or plan.account_id != account_id or revenue_coa is None:
                return _refused('Rechnung oder Bankbewegung wurde inzwischen geändert – bitte erneut versuchen.')

            debtor_side, waiting_side = self._receivable_sides(conn, plan.number)
            try:
                cursor = conn.execute(
                    'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)',
                    (storno_id, booking_id, -plan.amount, plan.bank_date))
            except sqlite3.IntegrityError:
                # der eindeutige Index auf (Rechnung, Bewegung) – dasselbe wie die Prüfung im Plan
                return _refused('Buchung ist bereits mit dieser Rechnung verknüpft')
            payment_id = cursor.lastrowid
            tx.log.append(('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)',
                           (storno_id, booking_id, -plan.amount, plan.bank_date), 'Link storno invoice to booking (refund)'))

            tax_rate = self._booking_tax_rate(plan.rate)
            for row in payment_rows(number=plan.number, customer_id=plan.customer_id, bank_id=booking_id,
                                    bank_date=plan.bank_date, bank_tax_date=plan.bank_tax_date,
                                    recipient=plan.recipient, currency=plan.currency, amount=-plan.amount,
                                    tax_rate=tax_rate, tax_value=-plan.tax if tax_rate else None, bank_coa=bank_coa,
                                    revenue_coa=revenue_coa, debtor_side=debtor_side, waiting_side=waiting_side):
                params = tuple(row.values[column] for column in PAYMENT_ROW_COLUMNS)
                conn.execute(_PAYMENT_ROW_INSERT, params)
                tx.log.append((_PAYMENT_ROW_INSERT, params, row.description))

            self._recalc_invoice(conn, storno_id)
        return PaymentAssignment(True, None, payment_id, revenue_coa)

    def _refund_plan(self, conn, storno_id, booking_id, requested, bank_coa_ids):
        """Eine Erstattung prüfen und ausrechnen – nur lesend, in der Verbindung (Transaktion) ``conn``: die Stornorechnung
        ``storno_id`` mit ihrer stornierten Rechnung, die Bankbewegung ``booking_id``, das Guthaben, der freie Rest der
        Bewegung (``bank_coa_ids``: die eigenen Bankkonten) und der gewünschte Betrag ``requested`` (Minor Units, auf Cent
        gerundet, oder ``None``). Returns ``(Klartext, None)`` bei einer Ablehnung, sonst ``(None, _Refund)``."""
        storno = conn.execute(
            'SELECT InvoiceNumber, DocumentType, InvoiceKind, CancelsInvoiceId, CustomerId, SumGross, TaxAmount '
            'FROM Invoices WHERE ID = ?', (storno_id,)).fetchone()
        if storno is None or (storno[1] or 'invoice') != 'invoice':
            return 'Rechnung nicht gefunden', None
        number, _type, kind, original_id, customer_id, gross, tax_total = storno
        original = None
        if kind == STORNO_KIND and original_id is not None:
            original = conn.execute('SELECT InvoiceNumber, SumGross, TaxAmount, TaxRate FROM Invoices WHERE ID = ?',
                                    (original_id,)).fetchone()
        if original is None:
            return REFUND_ONLY_STORNO_MESSAGE, None
        bank = conn.execute(
            'SELECT BookingType, Amount, Account_ID, DateBooking, DateTax, RecipientClient, Currency '
            'FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()
        if bank is None:
            return 'Buchung nicht gefunden', None
        if bank[0] != 'bank' or (bank[1] or 0) >= 0:
            return REFUND_ONLY_OUTGOING_MESSAGE, None
        if conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                        (storno_id, booking_id)).fetchone():
            return 'Buchung ist bereits mit dieser Rechnung verknüpft', None
        original_number, original_gross, original_tax, rate = original

        paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                            (original_id,)).fetchone()[0]
        refunded = -conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                                 (storno_id,)).fetchone()[0]
        credit = paid - refunded
        if paid <= 0 or credit <= 0:
            return REFUND_NO_CREDIT_MESSAGE, None
        free = self._booking_free_outgoing_minor(conn, booking_id, bank_coa_ids)
        if free <= 0:
            return REFUND_EXHAUSTED_MESSAGE, None
        refund = min(credit, free, credit if requested is None else requested)

        tax = None
        if self._booking_tax_rate(rate):
            paid_tax = self._allocations_tax(conn, original_id, original_number, original_gross or 0, original_tax or 0)
            refunded_tax = -self._allocations_tax(conn, storno_id, number, gross or 0, tax_total or 0)
            if refund == credit:
                tax = paid_tax - refunded_tax
            else:
                tax = min(self._prorated_tax(paid_tax, refund, paid), paid_tax - refunded_tax)
            tax = max(tax, 0)
        return None, _Refund(number, customer_id, original_id, rate, bank[2], bank[3], bank[4], bank[5], bank[6],
                             refund, tax, self._refund_revenue_coa(conn, original_id, original_number))

    def _refund_revenue_coa(self, conn, original_id, original_number):
        """Das Erlöskonto (COA-ID), auf das die Zahlungen der stornierten Rechnung gebucht sind: das der jüngsten Zuordnung,
        deren markierte Sätze eines nennen (``_payment_revenue_coa``) – wenn es ein Erlöskonto ist, sonst keines. ``None``,
        wenn keine Zuordnung markierte Sätze hat (Import, Altbestand)."""
        for (bank_id,) in conn.execute('SELECT BookingID FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID DESC',
                                       (original_id,)).fetchall():
            coa = self._payment_revenue_coa(conn, self._payment_children(conn, bank_id, original_number))
            if coa is not None:
                return coa if coa in {row[0] for row in self._income_coas(conn.cursor())} else None
        return None

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

        Die Erstattung einer Stornorechnung (``assign_storno_refund``) geht auf demselben Weg: Zuordnung und markierte Sätze
        gehen, Forderung und Gegenbuchung des Paares bleiben, das Guthaben ist danach wieder da. Eine **Zahlung** an einer
        stornierten Rechnung mit Stornorechnung lässt sich dagegen nicht entfernen, wenn danach mehr erstattet als gezahlt wäre
        (:class:`PaymentRemovalRefused`, in der Transaktion geprüft) – erst die Erstattung entfernen.

        Returns die Rechnungs-ID; ``ValueError``, wenn es die Zuordnung nicht gibt, :class:`PaymentRemovalRefused` (auch ein
        ``ValueError``), wenn das Guthaben sonst negativ würde."""
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            blocker = self._refund_removal_blocker(tx.conn, payment_id)
            if blocker:
                raise PaymentRemovalRefused(blocker)
            invoice_id = self._unassign(tx.conn, payment_id)
        if invoice_id is None:
            raise ValueError(f"InvoicePayment {payment_id} not found")
        return invoice_id

    @staticmethod
    def _refund_removal_blocker(conn, payment_id):
        """Klartext, wenn das Entfernen dieser Zuordnung das Guthaben eines Storno-Paares negativ machte, sonst ``None``: Sie ist
        eine Zahlung (Betrag > 0) an einer stornierten Rechnung mit Stornorechnung, und danach wären die Zahlungen kleiner als
        die Erstattungen. In der Transaktion von ``conn``."""
        row = conn.execute(
            'SELECT ip.InvoiceID, ip.Amount, s.ID FROM InvoicePayments ip '
            'JOIN Invoices s ON s.CancelsInvoiceId = ip.InvoiceID AND s.InvoiceKind = ? WHERE ip.ID = ?',
            (STORNO_KIND, payment_id)).fetchone()
        if row is None or row[1] <= 0:
            return None
        original_id, amount, storno_id = row
        paid, storno_sum = (conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                                         (invoice_id,)).fetchone()[0] for invoice_id in (original_id, storno_id))
        return ALREADY_REFUNDED_MESSAGE if paid - amount + storno_sum < 0 else None

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

    def _debtor_coa_ids(self, conn):
        return {r[0] for r in conn.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?',
                                           (self.DEBTOR_ACCOUNT_NUMBER,))}

    def _receivable_sides(self, conn, number):
        """``(Debitorenkonto, Wartekonto)`` der Forderung zu dieser Rechnungsnummer – die Konten, auf die eine Zuordnung
        „Bank an Debitoren“ und „Wartekonto an Erlöskonto“ bucht –, ``(None, None)`` ohne Forderung (dann ist es der
        Einzelsatz „Erlöskonto an Bank“). Die Forderung wird wie überall gesucht (``receivable_condition``): ein Satz mit
        10000 im Soll und der Rechnungsnummer, kein WISO-Storno „10000 an 1810“; die älteste gilt."""
        receivable_sql, receivable_params = self._receivable_sql(conn.cursor())
        receivable = conn.execute(
            f"SELECT COA_ID, CounterCOA_ID FROM Bookings WHERE DocumentNumber = ? "
            f"AND BookingType = 'entry' AND {receivable_sql} ORDER BY ID LIMIT 1",
            [number] + receivable_params).fetchone()
        if not receivable:
            return None, None
        # Normalfall „10000 an Wartekonto“; negativ gebucht steht 10000 als Gegenkonto
        return receivable if receivable[0] in self._debtor_coa_ids(conn) else (receivable[1], receivable[0])

    # ── Umzug auf die Ersatzrechnung ─────────────────────────────────────────
    def _same_payment_row(self, stored, expected, ignore_tax_date=False):
        """Ist der gespeicherte Satz (Spalte → Wert) genau der erwartete (``payment_rows``)? Der Steuersatz zählt als gleich,
        wenn es derselbe Satz ist (0.19 wie 19, ``_tax_rate_key``), ein leerer Empfänger wie ein fehlender. Was die App nie
        schreibt – Konto, Fremdkonto, Kategorie, Quellklammer – muss leer sein. ``ignore_tax_date`` lässt das Steuerdatum
        aus: nur, um den Grund zu benennen (der DATEV-Export stempelt es auf die exportierten Sätze) – umgezogen wird so
        ein Satz trotzdem nicht."""
        for column in PAYMENT_ROW_COLUMNS:
            if ignore_tax_date and column == 'DateTax':
                continue
            have, want = stored[column], expected[column]
            if column == 'TaxRate':
                same = self._tax_rate_key(have) == self._tax_rate_key(want)
            elif column == 'RecipientClient':
                same = (have or '') == (want or '')
            else:
                same = have == want
            if not same:
                return False
        return (stored['BookingType'] == 'entry' and stored['Account_ID'] is None and not stored['ForeignBankAccount']
                and stored['Category_ID'] is None and not stored['SourceGroup'])

    def _revenue_coa_if_as_written(self, conn, old, bank_id, bank, amount, bank_coa, ignore_tax_date=False):
        """Das Erlöskonto der Zuordnung (stornierte Rechnung ``old``, Bankbewegung ``bank_id`` mit den Angaben ``bank``, wie
        ``_movable_payment`` sie liest, über ``amount``, Bankkonto ``bank_coa``), wenn ihre Sätze GENAU die sind, die
        ``_assign_in_tx`` dafür schreibt – sonst ``None``. Der Umzug entfernt die Sätze und schreibt sie neu: Was ein Nutzer
        daran geändert hat, ginge still verloren, und ein Satz mit anderer Beleg-Nr. bliebe als Doppelbuchung stehen. Es gilt:

        1. Satzbestand: unter der Bewegung stehen zu dieser Zuordnung (``Status`` = Marke, ``DocumentNumber`` = Nummer der
           Rechnung) genau die erwarteten Sätze – „Bank an Debitoren“ (``liquid_first``) und „Wartekonto an Erlöskonto“
           (``transfer``) oder der Einzelsatz „Erlöskonto an Bank“ (``purpose_first``), keiner mehr, keiner weniger;
        2. jede Spalte hat den Wert, den ``payment_rows`` für diese Zuordnung ergibt: Datum, Steuerdatum, Empfänger und Währung
           der Bewegung, Kunde der Rechnung, Betrag, Text, Steuersatz, das Bankkonto der Bewegung; die übrigen Konten in der
           Rolle, die die Form verlangt (Debitoren, ein Wartekonto, ein Erlöskonto); Kategorie, Konto und Fremdkonto leer;
        3. der Steuerbetrag ist einer der beiden, die die Zuordnungslogik ergibt: anteilig (``_prorated_tax``) oder, als
           schließender Teil, der Rest (``_remaining_tax``, ohne die Zuordnung selbst);
        4. an keinem Satz hängt etwas, das ``_delete_booking_rows`` mit ihm wegräumt (``_booking_attachments_exist``);
        5. unter der Bewegung steht kein markierter Satz, dessen Beleg-Nr. zu keiner Rechnung gehört, die der Bewegung
           zugeordnet ist.

        ``ignore_tax_date`` prüft dasselbe ohne das Steuerdatum – ``_movable_payment`` benennt damit den Grund."""
        if conn.execute(
                "SELECT 1 FROM Bookings k WHERE k.ParentBooking_ID = ? AND k.Status = ? AND NOT EXISTS ("
                "SELECT 1 FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                "WHERE ip.BookingID = k.ParentBooking_ID AND i.InvoiceNumber = k.DocumentNumber) LIMIT 1",
                (bank_id, PAYMENT_MARK)).fetchone():
            return None
        # Ein Satz ohne Marke unter der Bewegung, der zur Rechnung gehört (ihre Beleg-Nr.) oder die Debitoren berührt – etwa
        # ein von Hand ergänztes Skonto „… an 10000“ –, bliebe beim Umzug an der stornierten Rechnung zurück: Die neue
        # Rechnung zeigte einen offenen Rest, den es nicht gibt
        debtor_ids = sorted(self._debtor_coa_ids(conn))
        debtor_marks = ','.join('?' * len(debtor_ids)) or 'NULL'
        if conn.execute(
                "SELECT 1 FROM Bookings WHERE ParentBooking_ID = ? AND (Status IS NULL OR Status <> ?) AND ("
                f"DocumentNumber = ? OR COA_ID IN ({debtor_marks}) OR CounterCOA_ID IN ({debtor_marks})) LIMIT 1",
                [bank_id, PAYMENT_MARK, old.number, *debtor_ids, *debtor_ids]).fetchone():
            return None
        columns = ('ID', *PAYMENT_ROW_COLUMNS, *_PAYMENT_ROW_UNWRITTEN)
        rows = [dict(zip(columns, values)) for values in conn.execute(
            f"SELECT {', '.join(columns)} FROM Bookings WHERE ParentBooking_ID = ? AND Status = ? AND DocumentNumber = ? "
            "ORDER BY ID", (bank_id, PAYMENT_MARK, old.number))]
        # Debitoren- und Wartekonto sind die der Forderung, wie ``_assign_in_tx`` sie heute fände – nicht irgendein Konto
        # in dieser Rolle
        debtor_side, waiting_side = self._receivable_sides(conn, old.number)
        wanted_forms = [FORM_LIQUID_FIRST, FORM_TRANSFER] if debtor_side and waiting_side else [FORM_PURPOSE_FIRST]
        if sorted(row['EntryForm'] or '' for row in rows) != wanted_forms:
            return None
        if self._booking_attachments_exist(conn, [row['ID'] for row in rows]):
            return None

        by_form = {row['EntryForm']: row for row in rows}
        revenue_coa = self._payment_revenue_coa(conn, [row['ID'] for row in rows])
        if revenue_coa not in {row[0] for row in self._income_coas(conn.cursor())}:
            return None
        if len(rows) == 2 and (debtor_side not in debtor_ids or waiting_side not in self._waiting_coa_ids(conn)):
            return None

        tax_rate = self._booking_tax_rate(old.rate)
        taxes = {None}
        if tax_rate:
            taxes = {self._prorated_tax(old.tax_total, amount, old.gross)}
            # Den Rest der Rechnungssteuer bekommt nur der Teil, der die Rechnung geschlossen hat – also nur, wenn sie voll
            # bezahlt war. Bei einer offenen Rechnung wäre „der Rest“ die ganze übrige Steuer: ein Wert, den die App nie
            # schreibt, ein Nutzer von Hand aber schon
            paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                                (old.id,)).fetchone()[0]
            if paid >= old.gross:
                taxes.add(self._remaining_tax(conn, old.id, old.number, old.gross, old.tax_total,
                                              except_booking=bank_id))
        for tax_value in taxes:
            expected = payment_rows(
                number=old.number, customer_id=old.customer_id, bank_id=bank_id, bank_date=bank[3], bank_tax_date=bank[4],
                recipient=bank[5], currency=bank[6], amount=amount, tax_rate=tax_rate, tax_value=tax_value,
                bank_coa=bank_coa, revenue_coa=revenue_coa, debtor_side=debtor_side, waiting_side=waiting_side)
            if all(self._same_payment_row(by_form[row.values['EntryForm']], row.values, ignore_tax_date)
                   for row in expected):
                return revenue_coa
        return None

    def _movable_payment(self, conn, old, new_id, bank_id, amount, bank_coas, bank_coa_ids):
        """Lässt sich die Zuordnung (stornierte Rechnung ``old``, Bankbewegung ``bank_id``) über ``amount`` umziehen? Nur, was
        die App genau so geschrieben hat, wie ``_assign_in_tx`` es schreiben würde, und was sich eindeutig neu schreiben lässt:
        eine Bankbewegung mit Zahlungseingang, deren Konto eine SKR-Nummer hat (``bank_coas``) und die nicht überbucht ist;
        keine Sätze mit Quellklammer (WISO) darunter; genau eine Zuordnung zum Paar und keine der neuen Rechnung zur selben
        Bewegung; und markierte Sätze, die unverändert sind (``_revenue_coa_if_as_written``).

        ``old``: die stornierte Rechnung (``_Replaced``). Returns ``((Account_ID der Bewegung, Erlöskonto der bisherigen
        Sätze), None)``, sonst ``(None, Grund)`` – der Grund ist ein Schlüssel von ``MOVE_SKIP_REASONS``."""
        bank = conn.execute('SELECT BookingType, Amount, Account_ID, DateBooking, DateTax, RecipientClient, Currency '
                            'FROM Bookings WHERE ID = ?', (bank_id,)).fetchone()
        if bank is None or bank[0] != 'bank' or (bank[1] or 0) <= 0:
            return None, 'no_incoming'
        if amount <= 0:
            return None, 'import'                                    # eine negative Zuordnung kennt nur der WISO-Import
        if bank[2] not in bank_coas:
            return None, 'no_skr'
        if conn.execute("SELECT 1 FROM Bookings WHERE ParentBooking_ID = ? AND SourceGroup IS NOT NULL "
                        "AND SourceGroup <> '' LIMIT 1", (bank_id,)).fetchone():
            return None, 'import'
        if not self._payment_children(conn, bank_id, old.number):
            # keine markierten Sätze: die Zuordnung eines Imports – oder Sätze ohne Marke zu dieser Rechnung (eine ältere
            # Version der App, von Hand angelegt)
            unmarked = conn.execute('SELECT 1 FROM Bookings WHERE ParentBooking_ID = ? AND DocumentNumber = ? LIMIT 1',
                                    (bank_id, old.number)).fetchone()
            return None, 'unmarked' if unmarked else 'import'
        if (conn.execute('SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                         (old.id, bank_id)).fetchone()[0] != 1
                or conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                                (new_id, bank_id)).fetchone()):
            return None, 'multiple'
        revenue_coa = self._revenue_coa_if_as_written(conn, old, bank_id, bank, amount, bank_coas[bank[2]])
        if revenue_coa is None:
            # Weicht allein das Steuerdatum ab (der DATEV-Export stempelt es auf die exportierten Sätze), sagt der
            # Hinweis das – die Zuordnung bleibt trotzdem: Die Sätze stehen so in einem Export
            only_tax_date = self._revenue_coa_if_as_written(conn, old, bank_id, bank, amount, bank_coas[bank[2]],
                                                            ignore_tax_date=True) is not None
            return None, 'tax_date' if only_tax_date else 'edited'
        if self._booking_free_minor(conn, bank_id, bank_coa_ids) < 0:
            return None, 'overbooked'
        return (bank[2], revenue_coa), None

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
        Forderung der stornierten Rechnung samt Gegenbuchung bleibt stehen.

        Umgezogen wird nur, was die App genau so geschrieben hat (``_movable_payment``): Import, eine überbuchte Bewegung, ein
        Bankkonto ohne SKR-Nummer, Sätze, die jemand von Hand geändert oder ergänzt hat oder an denen ein Beleg hängt – es
        bleibt an der stornierten Rechnung und steht im Hinweis, mit dem Grund. Beurteilt wird jede Zuordnung gegen den
        Stand VOR dem ersten Umzug: Der Steuer-Rest des schließenden Teils hängt von den anderen Zuordnungen ab, die sich mit
        jedem Umzug ändern. Nichts zieht um, wenn Kunde oder Währung abweichen, für den Steuersatz der neuen Rechnung kein
        Erlöskonto hinterlegt ist oder zur stornierten Rechnung schon erstattet wurde (``assign_storno_refund``; Σ Zuordnungen
        der Stornorechnung < 0 – der Hinweis nennt es). Eine Sammelzahlung zieht nur mit der Zuordnung um, die zur stornierten
        Rechnung gehört.

        Jede Zuordnung läuft in einem Savepoint: Lässt sie sich nicht vollständig mit den erwarteten Beträgen neu buchen,
        geht der Savepoint zurück (die Zuordnung liegt unverändert an der stornierten Rechnung, ihre Einträge im Replay-Log
        ``tx.log`` entfallen), sie zählt als übersprungen, und die nächste kommt an die Reihe. Nur ein Datenbankfehler mitten
        im Umzug ist unerwartet: Er löst :class:`PaymentMoveFailed` aus, und der Aufrufer rollt alles zurück.

        Returns ``(Betrag in Minor Units, der umgezogen ist, Hinweis für den Nutzer oder None)``."""
        conn = tx.conn
        new_id, old_id = row['ID'], move['replaced_id']
        old = conn.execute('SELECT InvoiceNumber, DocumentType, Status, TaxRate, CustomerId, Currency, SumGross, TaxAmount '
                           'FROM Invoices WHERE ID = ?', (old_id,)).fetchone()
        if (old is None or (old[1] or 'invoice') != 'invoice' or old[2] != 'cancelled'
                or not conn.execute('SELECT 1 FROM Invoices WHERE CancelsInvoiceId = ?', (old_id,)).fetchone()):
            return 0, None
        old_number, _type, _status, old_rate, old_customer, old_currency, old_gross, old_tax = old
        replaced = _Replaced(old_id, old_number, old_gross or 0, old_tax or 0, old_rate, old_customer)
        total = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                             (old_id,)).fetchone()[0]
        if total <= 0:
            return 0, None
        # Wurde schon erstattet (Zuordnungen an der Stornorechnung), gäbe der Umzug Geld doppelt aus: nichts zieht um
        if conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID IN '
                        '(SELECT ID FROM Invoices WHERE CancelsInvoiceId = ?)', (old_id,)).fetchone()[0] < 0:
            return 0, MOVE_REFUNDED_HINT.format(number=old_number)

        def euro(minor):
            return fmt_eur(from_minor(minor))

        if row['CustomerId'] != old_customer or (row['Currency'] or 'EUR') != (old_currency or 'EUR'):
            return 0, (f'Die Zahlungen der stornierten Rechnung {old_number} ({euro(total)}) bleiben dort: '
                       'Kunde oder Währung dieser Rechnung weichen ab.')
        if move['revenue_coa'] is None:
            return 0, (f'Die Zahlungen der stornierten Rechnung {old_number} ({euro(total)}) wurden nicht übernommen: '
                       'Für den Steuersatz dieser Rechnung ist kein Erlöskonto hinterlegt. Bitte die Zuordnung dort '
                       'entfernen und hier mit Erlöskonto zuordnen.')

        def booked(result, expected):
            """Die Zuordnung kam mit genau dem Betrag zustande."""
            if not result.ok:
                return False
            amount = conn.execute('SELECT Amount FROM InvoicePayments WHERE ID = ?', (result.payment_id,)).fetchone()
            return amount is not None and amount[0] == expected

        payments = conn.execute(
            'SELECT ID, BookingID, Amount FROM InvoicePayments WHERE InvoiceID = ? ORDER BY PaymentDate, ID',
            (old_id,)).fetchall()
        positive = sum(amount for _pay_id, _bank_id, amount in payments if amount > 0)   # eine negative bleibt unberührt
        same_rate = self._tax_rate_key(old_rate) == self._tax_rate_key(row['TaxRate'])

        def verdict(bank_id, amount):
            found, reason = self._movable_payment(conn, replaced, new_id, bank_id, amount, move['bank_coas'],
                                                  move['bank_coa_ids'])
            # Bei gleichem Steuersatz bleibt der Erlös auf seinem Konto: Liegt die Zahlung auf einem anderen als dem heute
            # hinterlegten (von Hand geändert, oder inzwischen ein anderes gelernt), buchte der Umzug sie im vergangenen
            # Zeitraum um
            if found is not None and same_rate and found[1] != move['revenue_coa']:
                return None, 'revenue_account'
            return found, reason

        verdicts = [verdict(bank_id, amount) for _pay_id, bank_id, amount in payments]
        moved = skipped = 0
        reasons = set()
        for (pay_id, bank_id, amount), (found, reason) in zip(payments, verdicts):
            if found is None:
                if amount > 0:
                    skipped += amount
                    reasons.add(reason)
                continue
            due_new = (row['SumGross'] or 0) - conn.execute(
                'SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?', (new_id,)).fetchone()[0]
            if due_new <= 0:
                break
            account_id, revenue_coa_old = found
            part = min(amount, due_new)
            log_length = len(tx.log)
            try:
                conn.execute('SAVEPOINT payment_move')
                self._unassign(conn, pay_id)
                rebooked = booked(self._assign_in_tx(
                    tx, new_id, bank_id, part,
                    {'tax_rate': row['TaxRate'], 'account_id': account_id,
                     'bank_coa': move['bank_coas'][account_id], 'revenue_coa': move['revenue_coa'],
                     'bank_coa_ids': move['bank_coa_ids']}), part)
                if rebooked and amount > part:
                    rebooked = booked(self._assign_in_tx(
                        tx, old_id, bank_id, amount - part,
                        {'tax_rate': old_rate, 'account_id': account_id, 'bank_coa': move['bank_coas'][account_id],
                         'revenue_coa': revenue_coa_old, 'bank_coa_ids': move['bank_coa_ids']},
                        allow_cancelled=True), amount - part)
                if not rebooked:
                    conn.execute('ROLLBACK TO payment_move')
                    del tx.log[log_length:]
                conn.execute('RELEASE payment_move')
            except sqlite3.Error as exc:
                raise PaymentMoveFailed(
                    f'Die Zahlungen der stornierten Rechnung {old_number} ließen sich nicht übernehmen: '
                    f'Datenbankfehler ({exc}). Die Rechnung wurde nicht versendet.') from exc
            if rebooked:
                moved += part
            else:
                skipped += amount
                reasons.add('rebook')

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
            why = '; '.join(text for key, text in MOVE_SKIP_REASONS.items() if key in reasons)
            notes.append(f'{euro(skipped)} ließen sich nicht automatisch übernehmen und bleiben an der stornierten '
                         f'Rechnung: {why}.')
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
                                 Forderung noch in den Büchern steht;
        * ``refund_exceeds_credit`` zu einer Stornorechnung ist mehr erstattet als zur stornierten Rechnung
                                 gezahlt wurde (Σ Zuordnungen beider Belege < 0) – etwa nach dem Löschen der
                                 Bankbewegung einer Zahlung. Eine intakte Erstattung (Zuordnung an der
                                 Stornorechnung mit ihren Sätzen) ergibt in keiner Kategorie einen Befund.

        ``no_settlement`` und ``transfer_mismatch`` betrachten nur Zuordnungen, deren Buchung
        eine Bankbewegung ohne Sätze mit Quellklammer (``SourceGroup``) ist: Der WISO-Import
        liefert seine Sätze selbst (und kennt Barzahlungen ohne Bankbewegung). Die beiden
        letzten Kategorien zählen nur Forderungen, die die App selbst gebucht hat
        (``_app_receivable_amounts``), damit ein Import keinen Fehlalarm auslöst; ein intaktes Paar
        mit Guthaben (Zahlungen an der stornierten Rechnung) ergibt keinen Befund.

        Die Abfragen gehen von den Zuordnungen aus bzw. fassen je Rechnung vorab zusammen; die
        Rechnungsnummern mit Forderung und die Forderungen der App (mit ihren Beträgen) werden
        je einmal gelesen. Der Aufwand wächst so linear mit dem Bestand (4000 Bewegungen/Forderungen,
        1000 Zuordnungen: unter einer Sekunde), die Zahl der Anweisungen nicht mit der Zahl der Paare.
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
        pairs = conn.execute(
            "SELECT o.ID, o.InvoiceNumber, s.InvoiceNumber FROM Invoices s "
            "JOIN Invoices o ON o.ID = s.CancelsInvoiceId "
            f"WHERE s.InvoiceKind = ?{pair_only} ORDER BY o.InvoiceNumber, s.ID",
            [STORNO_KIND, *only_params, *only_params]).fetchall()
        # Von Hand storniert (Altbestand), keine Stornorechnung – und die Forderung steht noch in den Büchern
        legacy = conn.execute(
            "SELECT i.ID, i.InvoiceNumber FROM Invoices i WHERE i.Status = 'cancelled' "
            "AND COALESCE(i.SumGross, 0) > 0 AND i.InvoiceKind IS NOT ? "
            f"AND NOT EXISTS (SELECT 1 FROM Invoices s WHERE s.CancelsInvoiceId = i.ID) AND {is_invoice}{only} "
            "ORDER BY i.InvoiceNumber", [STORNO_KIND, *only_params]).fetchall()

        # Die Forderungen der App werden für alle Paare und Rechnungen zusammen einmal gelesen – je Rechnung nachzuschlagen
        # wäre quadratisch (``DocumentNumber`` ist nicht indiziert). Der Filter auf eine Rechnung liest gezielt.
        app_receivables = self._app_receivable_amounts(conn) if invoice_id is None and (pairs or legacy) else None

        def app_receivables_of(number):
            if app_receivables is not None:
                return app_receivables.get(number, [])
            return self._app_receivable_amounts(conn, number).get(number, [])

        for original_id, original_number, storno_number in pairs:
            has_receivable = bool(app_receivables_of(original_number))
            has_counter = bool(app_receivables_of(storno_number))
            if has_receivable and not has_counter:
                add('storno_counter_mismatch', original_id, original_number,
                    f'Zur Rechnung steht die Forderung, aber die Gegenbuchung der Stornorechnung '
                    f'{storno_number} fehlt – auf dem Debitorenkonto bleibt der Betrag stehen.')
            elif has_counter and not has_receivable:
                add('storno_counter_mismatch', original_id, original_number,
                    f'Die Stornorechnung {storno_number} hat eine Gegenbuchung, aber zur Rechnung steht '
                    f'keine Forderung der App – auf dem Debitorenkonto steht ein Haben-Überhang.')

        for inv, number in legacy:
            receivable = app_receivables_of(number)
            if receivable:
                booked = sum(amount for _booking_id, amount in receivable)
                add('legacy_cancelled_open', inv, number,
                    f'Die Rechnung ist storniert, ihre Forderung steht aber noch in den Büchern ({euro(booked)}). '
                    f'Abhilfe: „Stornorechnung nachträglich erstellen“.')

        # Mehr erstattet als gezahlt: die Zuordnungen des Paares (Zahlungen minus Erstattungen) ergeben ein negatives Guthaben,
        # etwa nach dem Löschen der Bankbewegung einer Zahlung. Je Rechnung vorab zusammengefasst, nicht je Paar nachgeschlagen.
        for storno_id, storno_number, original_number, paid, refunded in conn.execute(
                "WITH sums AS (SELECT InvoiceID, SUM(Amount) AS Total FROM InvoicePayments GROUP BY InvoiceID) "
                "SELECT s.ID, s.InvoiceNumber, o.InvoiceNumber, COALESCE(po.Total, 0), -COALESCE(ps.Total, 0) "
                "FROM Invoices s JOIN Invoices o ON o.ID = s.CancelsInvoiceId "
                "LEFT JOIN sums po ON po.InvoiceID = o.ID LEFT JOIN sums ps ON ps.InvoiceID = s.ID "
                f"WHERE s.InvoiceKind = ?{pair_only} AND COALESCE(po.Total, 0) + COALESCE(ps.Total, 0) < 0 "
                "ORDER BY o.InvoiceNumber, s.ID", [STORNO_KIND, *only_params, *only_params]):
            add('refund_exceeds_credit', storno_id, storno_number,
                f'Zur Stornorechnung {storno_number} ist mehr erstattet ({euro(refunded)}) als zur Rechnung '
                f'{original_number} gezahlt wurde ({euro(paid)}).')
        return findings
