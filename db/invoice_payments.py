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
from collections import namedtuple

from money import to_minor, from_minor, prorate, round_minor, fmt_eur
from .matching import REVENUE_ACCOUNT_RANGE
from .reporting import is_income_account

PAYMENT_MARK = 'invoice_payment'

# Auf diese Status lässt sich eine Zahlung zuordnen: ab „versendet“ ist die Rechnung ein
# offener Posten. 'partial' ist der Alt-Wert von 'partial_payment'.
PAYABLE_STATUSES = ('sent', 'overdue', 'partial_payment', 'partial')

# 0,01 EUR in Minor Units: Ein Rest darunter gilt als bezahlt.
_PAID_RESIDUE = 100

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
        return 'paid' if (gross - paid) * sign < _PAID_RESIDUE else 'partial_payment'
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


def payment_blocker(doc_type, status, gross):
    """Warum sich auf diese Rechnung keine Zahlung zuordnen lässt – ``None``, wenn doch."""
    if (doc_type or 'invoice') != 'invoice':
        return 'Rechnung nicht gefunden'
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
        in der Transaktion von ``conn``, ohne zu committen."""
        row = conn.execute('SELECT SumGross, Status, PaymentDueDate FROM Invoices WHERE ID = ?',
                           (invoice_id,)).fetchone()
        if row is None:
            return
        gross, status, due_date = row[0] or 0, row[1], row[2]
        paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments '
                            'WHERE InvoiceID = ?', (invoice_id,)).fetchone()[0]
        if adjust_status:
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
        blocked = payment_blocker(invoice[45], invoice[40], invoice[38])
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
            'debtor_coa': self.get_coa_id_by_account_number(self.DEBTOR_ACCOUNT_NUMBER),
            'bank_coa_ids': self.get_bank_coa_ids(),
        }

    def payment_precheck(self, invoice_id, account_id, revenue_coa_id=None):
        """Fehlertext, wenn sich auf diese Rechnung über dieses Bankkonto keine Zahlung zuordnen
        lässt – sonst ``None``. Für Masken, die die Bankbewegung erst noch anlegen: Wer prüft,
        bevor er schreibt, hinterlässt keine Bewegung ohne Zuordnung. ``revenue_coa_id`` wie
        bei ``assign_invoice_payment``."""
        return self._payment_prerequisites(invoice_id, account_id, revenue_coa_id)[0]

    # ── Erlöskonto: Wahl, wenn keines hinterlegt ist ────────────────────────
    def get_revenue_coas(self):
        """Die Konten, aus denen der Nutzer das Erlöskonto einer Zahlung wählen kann: Erlöskonten
        4000–4999 (``REVENUE_ACCOUNT_RANGE``) ohne die Wartekonten, die keine Erlöse sind
        (``db.reporting.is_income_account``). Returns ``[(COA_ID, Kontonummer, Name)]`` nach
        Kontonummer."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                'SELECT ID, AccountNumber, Name FROM ChartOfAccounts '
                'WHERE AccountNumber BETWEEN ? AND ? ORDER BY AccountNumber, ID',
                REVENUE_ACCOUNT_RANGE).fetchall()
        finally:
            conn.close()
        return [row for row in rows if is_income_account(row[1])]

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
        Rechnung überein.

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
            requested = to_minor(amount)
            if requested <= 0:
                return _refused('Der Betrag muss größer 0 sein.')

        with self.transaction() as tx:
            conn = tx.conn
            conn.execute('BEGIN IMMEDIATE')
            invoice = conn.execute(
                'SELECT InvoiceNumber, DocumentType, Status, SumGross, TaxAmount, TaxRate, '
                'CustomerId FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()
            if invoice is None:
                return _refused('Rechnung nicht gefunden')
            number, doc_type, status, gross, tax_total, rate, customer_id = invoice
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
            blocked = payment_blocker(doc_type, status, gross)
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
                # Der schließende Teil bekommt den Rest der Rechnungssteuer: alles, was unter den
                # Bewegungen der Rechnung schon als Steuer zu ihr gebucht ist – auch Sätze aus
                # Altbeständen ohne Marke (nur die Zahlungssätze tragen Steuer, nicht „Bank an
                # Debitoren“)
                booked_tax = conn.execute(
                    'SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE DocumentNumber = ? '
                    'AND ParentBooking_ID IN '
                    '(SELECT BookingID FROM InvoicePayments WHERE InvoiceID = ?)',
                    (number, invoice_id)).fetchone()[0]
                tax_part = tax_total - booked_tax
            else:
                tax_part = round_minor(prorate(tax_total, alloc, gross), 2)
            # TaxRate steht teils als 0.19, teils als 19; -1 ist der Sentinel für „§ 19, keine USt“
            tax_rate = None if not rate or rate < 0 else (rate if rate <= 1 else rate / 100)
            tax_value = tax_part if tax_rate else None

            debtor_coa = ctx['debtor_coa']
            receivable = None
            if debtor_coa is not None:
                receivable = conn.execute(
                    "SELECT COA_ID, CounterCOA_ID FROM Bookings WHERE DocumentNumber = ? "
                    "AND COA_ID = ? AND BookingType = 'entry' ORDER BY ID LIMIT 1",
                    (number, debtor_coa)).fetchone()

            cursor = conn.execute(
                'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) '
                'VALUES (?, ?, ?, ?)', (invoice_id, booking_id, alloc, bank_date))
            payment_id = cursor.lastrowid
            tx.log.append(('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, '
                           'PaymentDate) VALUES (?, ?, ?, ?)',
                           (invoice_id, booking_id, alloc, bank_date), 'Link invoice to booking'))

            def child(coa_id, counter_coa_id, text, taxed, description):
                sql = ('INSERT INTO Bookings (DateBooking, DateTax, RecipientClient, Contact_ID, '
                       'COA_ID, CounterCOA_ID, Amount, Currency, TaxRate, TaxAmount, Text, '
                       "DocumentNumber, BookingType, ParentBooking_ID, AutoMirror, Status) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'entry', ?, 0, ?)")
                params = (bank_date, bank_tax_date, recipient or '', customer_id, coa_id,
                          counter_coa_id, alloc, currency or 'EUR',
                          tax_rate if taxed else None, tax_value if taxed else None, text,
                          number, booking_id, PAYMENT_MARK)
                conn.execute(sql, params)
                tx.log.append((sql, params, description))

            if receivable and receivable[0] and receivable[1]:
                child(ctx['bank_coa'], receivable[0], f'Zahlung zu Re. {number}', False,
                      'Payment child (bank to debtor)')
                child(receivable[1], ctx['revenue_coa'], f'Umb. zu Re. {number}', True,
                      'Payment child (open item to revenue)')
            else:
                child(ctx['revenue_coa'], ctx['bank_coa'], f'Zahlung zu Re. {number}', True,
                      'Payment child (revenue to bank)')

            self._recalc_invoice(conn, invoice_id)
        return PaymentAssignment(True, None, payment_id, ctx['revenue_coa'])

    # ── Entfernen ────────────────────────────────────────────────────────────
    def _after_payment_removed(self, conn, invoice_id):
        """Nach dem Entfernen von Zahlungen: Die Forderung gilt wieder als offen, wenn kein
        Satz „… an Debitoren“ mehr zur Rechnung übrig ist (Stufe 7 des Auto-Abgleichs hatte
        sie auf 'resolved' gesetzt); AmountDue und Status der Rechnung neu rechnen."""
        row = conn.execute('SELECT InvoiceNumber FROM Invoices WHERE ID = ?',
                           (invoice_id,)).fetchone()
        if row is None:
            return
        debtor = '(SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?)'
        if not conn.execute(
                f"SELECT 1 FROM Bookings WHERE BookingType = 'entry' "
                f"AND ParentBooking_ID IS NOT NULL AND DocumentNumber = ? "
                f"AND CounterCOA_ID IN {debtor} LIMIT 1",
                (row[0], self.DEBTOR_ACCOUNT_NUMBER)).fetchone():
            conn.execute(
                f"UPDATE Bookings SET Status = NULL WHERE BookingType = 'entry' "
                f"AND ParentBooking_ID IS NULL AND Status = 'resolved' "
                f"AND DocumentNumber = ? AND COA_ID IN {debtor}",
                (row[0], self.DEBTOR_ACCOUNT_NUMBER))
        self._recalc_invoice(conn, invoice_id)

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
        stehen und ist damit wieder offen. Alles in einer Transaktion.

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

    # ── Prüfung ──────────────────────────────────────────────────────────────
    def find_payment_inconsistencies(self, invoice_id=None):
        """Unstimmigkeiten der Zahlungszuordnungen – rein lesend, ändert nichts.

        ``invoice_id`` grenzt auf eine Rechnung ein. Returns eine Liste von Befunden
        ``{'category', 'invoice_id', 'invoice_number', 'text'}`` (Klartext, nicht maskiert);
        ``invoice_id`` ist ``None``, wenn die Rechnungsnummer keiner Rechnung gehört.
        Kategorien:

        * ``no_settlement``      Zuordnung ohne Satz „Bank an Debitoren“ (die Rechnung hat
                                 eine Forderung);
        * ``receivable_attached`` die Forderung hängt an einer Bankbewegung;
        * ``orphan_payment_rows`` Zahlungssatz unter einer Bankbewegung ohne Zuordnung;
        * ``transfer_mismatch``  zugeordneter Betrag ≠ auf das Erlöskonto umgebuchter;
        * ``payment_on_entry``   Zuordnung hängt an einem Buchungssatz, nicht an einer
                                 Bankbewegung;
        * ``duplicate_payment``  dasselbe Paar (Rechnung, Bewegung) mehrfach zugeordnet;
        * ``outgoing_payment``   Zuordnung auf einen Ausgang;
        * ``status_unsent``      Forderung gebucht, Status aber Entwurf/abgeschlossen;
        * ``amount_due_mismatch`` offener Rest passt nicht zu den Zuordnungen.
        """
        conn = self._get_connection()
        try:
            return self._payment_findings(conn, invoice_id)
        finally:
            conn.close()

    def _payment_findings(self, conn, invoice_id):
        debtor = self.DEBTOR_ACCOUNT_NUMBER
        waiting = tuple(number for number, _name in self.OPEN_REVENUE_ACCOUNTS.values())
        is_invoice = "(i.DocumentType = 'invoice' OR i.DocumentType IS NULL)"
        only = ' AND i.ID = ?' if invoice_id is not None else ''
        only_params = [invoice_id] if invoice_id is not None else []
        # Rechnungsnummern, zu denen eine Forderung (Debitoren-Satz) gebucht ist
        has_receivable = (
            "WITH recv AS (SELECT DISTINCT DocumentNumber AS Number FROM Bookings "
            "WHERE BookingType = 'entry' AND COA_ID IN "
            "(SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?)) ")
        findings = []

        def add(category, inv_id, number, text):
            findings.append({'category': category, 'invoice_id': inv_id,
                             'invoice_number': number or '', 'text': text})

        def euro(minor):
            return fmt_eur(from_minor(minor or 0))

        for inv, number, pay_id, bank_id, amount in conn.execute(
                has_receivable +
                "SELECT i.ID, i.InvoiceNumber, ip.ID, ip.BookingID, ip.Amount "
                "FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID "
                "JOIN Bookings b ON b.ID = ip.BookingID AND b.BookingType = 'bank' "
                "JOIN recv ON recv.Number = i.InvoiceNumber "
                f"WHERE NOT EXISTS (SELECT 1 FROM Bookings s WHERE s.ParentBooking_ID = b.ID "
                f"AND s.DocumentNumber = i.InvoiceNumber AND s.CounterCOA_ID IN "
                f"(SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?)){only} "
                "ORDER BY i.InvoiceNumber, ip.ID",
                [debtor, debtor] + only_params):
            add('no_settlement', inv, number,
                f'Zuordnung #{pay_id} ({euro(amount)}) auf Bewegung #{bank_id}: Der '
                f'Buchungssatz „Bank an Debitoren“ fehlt.')

        receivable_sql, receivable_params = self._receivable_sql(conn.cursor(), alias='r')
        for rec_id, number, bank_id, amount, inv in conn.execute(
                "SELECT r.ID, r.DocumentNumber, r.ParentBooking_ID, r.Amount, "
                "(SELECT i.ID FROM Invoices i WHERE i.InvoiceNumber = r.DocumentNumber) "
                f"FROM Bookings r WHERE r.BookingType = 'entry' AND r.ParentBooking_ID IS NOT NULL "
                f"AND {receivable_sql} ORDER BY r.ID", receivable_params):
            if invoice_id is None or inv == invoice_id:
                add('receivable_attached', inv, number,
                    f'Die Forderung #{rec_id} ({euro(amount)}) hängt an der Bankbewegung '
                    f'#{bank_id}; sie gehört zur Rechnung, nicht zur Bewegung.')

        for row_id, bank_id, number, amount, inv in conn.execute(
                "SELECT k.ID, k.ParentBooking_ID, k.DocumentNumber, k.Amount, "
                "(SELECT i.ID FROM Invoices i WHERE i.InvoiceNumber = k.DocumentNumber) "
                "FROM Bookings k WHERE k.ParentBooking_ID IS NOT NULL "
                "AND (k.SourceGroup IS NULL OR k.SourceGroup = '') "
                "AND (k.Status = ? OR k.Text LIKE 'Zahlung zu Re. %' "
                "OR k.Text LIKE 'Umb. zu Re. %') "
                "AND NOT EXISTS (SELECT 1 FROM InvoicePayments ip "
                "JOIN Invoices i2 ON i2.ID = ip.InvoiceID WHERE ip.BookingID = k.ParentBooking_ID "
                "AND i2.InvoiceNumber = k.DocumentNumber) ORDER BY k.ID", (PAYMENT_MARK,)):
            if invoice_id is None or inv == invoice_id:
                add('orphan_payment_rows', inv, number,
                    f'Zahlungssatz #{row_id} ({euro(amount)}) unter Bewegung #{bank_id} hat '
                    f'keine Zuordnung zur Rechnung.')

        marks = ','.join('?' * len(waiting))
        for inv, number, paid, moved in conn.execute(
                has_receivable.rstrip() + ", moved AS (SELECT k.DocumentNumber AS Number, "
                "SUM(k.Amount) AS Total FROM Bookings k "
                "JOIN ChartOfAccounts c ON c.ID = k.COA_ID WHERE k.ParentBooking_ID IS NOT NULL "
                f"AND c.AccountNumber IN ({marks}) GROUP BY k.DocumentNumber) "
                "SELECT i.ID, i.InvoiceNumber, "
                "COALESCE((SELECT SUM(ip.Amount) FROM InvoicePayments ip "
                "WHERE ip.InvoiceID = i.ID), 0), COALESCE(moved.Total, 0) "
                "FROM Invoices i JOIN recv ON recv.Number = i.InvoiceNumber "
                "LEFT JOIN moved ON moved.Number = i.InvoiceNumber "
                f"WHERE {is_invoice}{only} ORDER BY i.InvoiceNumber",
                [debtor] + list(waiting) + only_params):
            if paid != moved:
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
                has_receivable +
                "SELECT i.ID, i.InvoiceNumber, i.Status FROM Invoices i "
                "JOIN recv ON recv.Number = i.InvoiceNumber "
                f"WHERE i.Status IN ('draft', 'finalized') AND {is_invoice}{only} "
                "ORDER BY i.InvoiceNumber", [debtor] + only_params):
            add('status_unsent', inv, number,
                f'Die Rechnung steht als Forderung in den Büchern, hat aber noch den Status '
                f'„{status}“ (nicht versendet).')

        for inv, number, gross, due, paid in conn.execute(
                "SELECT i.ID, i.InvoiceNumber, i.SumGross, i.AmountDue, "
                "COALESCE((SELECT SUM(Amount) FROM InvoicePayments WHERE InvoiceID = i.ID), 0) "
                f"FROM Invoices i WHERE {is_invoice}{only} ORDER BY i.InvoiceNumber",
                only_params):
            if due is None or due < 0 or due != (gross or 0) - paid:
                add('amount_due_mismatch', inv, number,
                    f'Offen sind {euro(due)}, nach den Zuordnungen müssten es '
                    f'{euro((gross or 0) - paid)} sein.')
        return findings

