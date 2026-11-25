# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: bookings."""
import sqlite3
import os
import json
from decimal import Decimal
from typing import NamedTuple
from money import to_minor, from_minor
from db import datev_keys
from db.core import add_fold_function, fold_text, search_limit
from db.matching import (
    ENTRY_FORMS, SIDE_COUNTER, derive_entry_form, is_bank_effective, is_mirror_row,
    purpose_in_counter, read_entry_form, reform,
)
from db.invoice_payments import PAYMENT_MARK, BankChangeRefused, PaymentRemovalRefused

# Alle Verweise auf ``Bookings.ID`` im Schema als (Tabelle, Spalte) – das, was ``_delete_booking_rows`` mit einer Buchung
# wegräumt (BookingDocuments, InvoicePayments), umbiegt (Assets, AssetDepreciations), was mit ihr per ``ON DELETE CASCADE``
# geht (BankTransactionDetails, BookingExports) oder woran es sonst scheiterte (eine Kindbuchung, ``ParentBooking_ID``).
# ``_booking_attachments_exist`` fragt danach; ``tests/test_invoice_reissue.py`` hält die Liste gegen das Schema.
BOOKING_REFERENCES = (
    ('BookingDocuments', 'Booking_ID'),
    ('InvoicePayments', 'BookingID'),
    ('Assets', 'Booking_ID'),
    ('AssetDepreciations', 'Booking_ID'),
    ('Bookings', 'ParentBooking_ID'),
    ('BankTransactionDetails', 'Booking_ID'),
    ('BookingExports', 'Booking_ID'),
)

RELATION_TYPES = ('receipt', 'invoice', 'contract', 'other')        # Positivliste für BookingDocuments.RelationType
RELATION_DEFAULT = 'receipt'
RELATION_LABELS = {'receipt': 'Beleg', 'invoice': 'Rechnung', 'contract': 'Vertrag', 'other': 'Sonstiges'}
BOOKING_GONE = 'Diese Buchung gibt es nicht (mehr). Bitte die Seite neu laden.'
RECEIPT_GONE = 'Diesen Beleg gibt es nicht (mehr). Bitte die Seite neu laden.'


class LinkRefused(ValueError):
    """Verknüpfen abgelehnt (Buchung oder Beleg fehlt); der Text ist Klartext für den Nutzer, nichts ist geschrieben."""


class LinkResult(NamedTuple):
    created: bool            # eine neue Zeile in BookingDocuments (False: es gab sie schon)
    number_taken: bool       # die Buchung hat die Nummer des Belegs übernommen
    document_number: str     # die Beleg-Nr. der Buchung nach dem Verknüpfen ('' = keine)
    number_in_use: bool      # nicht übernommen, weil eine andere Buchung diese oder eine ähnliche Nummer trägt
    has_entries: bool        # die Buchung ist eine Bankbewegung mit Buchungssätzen (sie erben die Nummer nicht)


class BankDetails(NamedTuple):
    """Die Zusatzfelder einer Bankbewegung aus ihrem CAMT-Auszug (``BankTransactionDetails``), gelesen von ``get_bank_details``; ``''`` = nicht vorhanden oder nicht lesbar."""
    value_date: str          # Valuta, ISO
    end_to_end_id: str       # End-to-End-Referenz
    mandate_ref: str         # Mandatsreferenz
    creditor_id: str         # Gläubiger-ID
    bank_ref: str            # Bankreferenz (``Ntry/AcctSvcrRef``)


# Bedingung 6 der Übernahme (``link_receipt_to_booking``): Trägt eine ANDERE Buchung diese oder eine ähnliche Nummer? Ähnlich heißt: die
# eine ist in der anderen enthalten, in beiden Richtungen (``INSTR`` – ``26B001`` gegen ``26B0010`` und ``26B001_A``), ohne Leerraum am Rand und ohne
# Rücksicht auf die Schreibweise. Die eigene Familie zählt nicht: die Sätze der Buchung, ihre Bankbewegung und deren übrige Sätze gehören
# zum selben Vorgang, der Abgleich fasst sie nicht an. ``TRIM``, ``LOWER`` und ``INSTR`` lesen jeden Wert als Text (auch eine Zahl oder
# einen Blob aus einem Altbestand); ``:n`` ist die Nummer des Belegs, ``:parent`` die Bankbewegung der Buchung oder NULL.
_NUMBER_IN_USE_SQL = '''
    SELECT 1 FROM Bookings
    WHERE ID != :id
      AND TRIM(COALESCE(DocumentNumber, '')) != ''
      AND NOT (ParentBooking_ID IS NOT NULL AND ParentBooking_ID = :id)
      AND NOT (:parent IS NOT NULL AND (ID = :parent OR (ParentBooking_ID IS NOT NULL AND ParentBooking_ID = :parent)))
      AND (INSTR(LOWER(TRIM(DocumentNumber)), LOWER(:n)) > 0 OR INSTR(LOWER(:n), LOWER(TRIM(DocumentNumber))) > 0)
    LIMIT 1
'''


class BookingsMixin:
    def fetch_bookings(self):
        """Fetch all bookings ordered by date descending"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Bookings ORDER BY DateBooking DESC')
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 11, 14) for r in rows]  # Amount(11), TaxAmount(14)
    @staticmethod
    def _group_entries_by_document(normal):
        """Buchungssätze desselben Belegs zu einer Split-Zeile zusammenfassen.

        Betrifft Belege ohne Bankbewegung – Kasse, Verrechnungskonto, von Hand
        erfasste Splits.

        Die Klammer ist die Beleg-Nr. am selben Tag – dieselbe Regel wie im
        Auto-Abgleich (_link_docnr_group). Erst wo keine Beleg-Nr. vorliegt,
        greift die aus dem Quellsystem übernommene ``SourceGroup``
        (WISO: ``ACCOUNTINGID``).

        Die Reihenfolge ist wichtig und nicht umkehrbar: WISO vergibt für jede
        Buchung gegen das Verrechnungskonto eine **eigene** ACCOUNTINGID, auch
        wenn mehrere zu einem Beleg gehören. Die Quellgruppe ist damit feiner
        als der Beleg und würde ihn auseinanderreißen. Umgekehrt ist sie nie
        gröber – im Echtbestand liegt keine einzige Quellgruppe über zwei
        Belegen.

        Einzelne Buchungen bleiben unverändert 'normal'.
        """
        buckets = {}
        order = []
        for item in normal:
            booking = item['booking']
            docnr = (booking[16] or '').strip()
            source_group = (booking[21] or '').strip() if len(booking) > 21 else ''
            if docnr:
                key = (docnr, item['date'])
            elif source_group:
                key = ('src', source_group)
            else:
                key = None
            if key is None:
                order.append(('single', item))
                continue
            if key not in buckets:
                buckets[key] = []
                order.append(('group', key))
            buckets[key].append(item)

        result = []
        seq = 0
        for kind, payload in order:
            if kind == 'single':
                result.append(payload)
                continue
            members = buckets[payload]
            if len(members) == 1:
                result.append(members[0])
                continue
            # Datum und Beleg-Nr. stammen aus der Gruppe selbst – bei einer
            # Klammer aus dem Quellsystem steckt beides nicht im Schlüssel.
            first = members[0]['booking']
            date = members[0]['date']
            docnr = (first[16] or '').strip()
            # Laufende Kennung statt Beleg-Nr.: sie dient nur als DOM-Griff zum
            # Auf- und Zuklappen und darf keine Sonderzeichen mitschleppen.
            seq += 1
            gid = f'g{seq}'
            children = [{'type': 'child', 'group_id': gid,
                         'date': m['date'], 'booking': m['booking']}
                        for m in members]
            result.append({
                'type': 'group',
                'group_id': gid,
                'date': date,
                'amount': sum((m['booking'][11] or 0) for m in members),
                'currency': first[12] or 'EUR',
                'description': docnr,
                'count': len(members),
                'account_id': next((m['booking'][4] for m in members if m['booking'][4]), None),
                'contact_id': next((m['booking'][7] for m in members if m['booking'][7]), None),
                'first_coa_id': first[8],
                'first_ccoa_id': first[9],
                'first_recipient': first[6],
                'first_text': first[15],
                'children': children,
            })
        return result

    def fetch_bookings_grouped(self, date_from=None, date_to=None):
        """Fetch bookings for display, with split groups aggregated.

        Args:
            date_from / date_to: optionaler Zeitraum 'YYYY-MM-DD' (einschließlich).
                Beide gesetzt → nur Buchungen dieses Zeitraums. Splits/Bank-Kinder
                werden über Parent bzw. Gruppe einbezogen, damit kein Split zerreißt.


        Returns a flat list of dicts, each with a 'type' key:

        - 'normal': ungrouped booking  →  {'type': 'normal',  'date': str, 'booking': tuple}
                                            'description': str, 'amount': float, 'count': int,
                                            'account_id': int|None, 'currency': str,
                                            'contact_id': int|None}
        - 'child':  individual split   →  {'type': 'child',   'group_id': int, 'date': str,
                                            'booking': tuple}
        - 'bank':   bank transaction   →  {'type': 'bank',    'date': str, 'booking': tuple,
                                            'children': list, 'linked': bool,
                                            'entry_text': str|None, 'entry_coa_id': int|None,
                                            'entry_counter_coa_id': int|None,
                                            'entry_docnr': str|None,
                                            'entry_category_id': int|None,
                                            'entry_contact_id': int|None}

        Bank rows with linked entries carry merged data from the first child so
        the template can render a single merged row. Rein liquide Spiegel-
        Buchungen (COA und Gegenkonto beide Bank-/Liquidkonten) werden aus der
        Normalliste ausgeblendet.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Doppik-COA-IDs laden — nur echte Bankkonten (aus Accounts-Tabelle)
        doppik_coa_ids = self._get_bank_coa_ids(cursor)

        # Optionaler Zeitraum-Filter. Bank-Kinder und Split-Gruppen werden über
        # ihren Parent bzw. ihre Gruppe einbezogen (Subquery) – so zerreißt kein
        # Split und die SQLite-Parametergrenze wird nicht überschritten.
        use_range = bool(date_from and date_to)
        rng       = (date_from, date_to)

        # 1. Bank transactions (top-level parents via ParentBooking_ID)
        if use_range:
            cursor.execute(
                "SELECT * FROM Bookings WHERE BookingType = 'bank' "
                "AND DateBooking BETWEEN ? AND ? ORDER BY DateBooking DESC", rng)
        else:
            cursor.execute(
                "SELECT * FROM Bookings WHERE BookingType = 'bank' "
                "ORDER BY DateBooking DESC")
        bank_rows = [self._euro_row(r, 11, 14) for r in cursor.fetchall()]  # Amount(11), TaxAmount(14)

        # 2. Child bookings linked to bank transactions via ParentBooking_ID
        #    (alle Kinder der im Zeitraum liegenden Bank-Buchungen)
        if use_range:
            cursor.execute(
                "SELECT * FROM Bookings WHERE ParentBooking_ID IS NOT NULL "
                "AND ParentBooking_ID IN (SELECT ID FROM Bookings "
                "WHERE BookingType = 'bank' AND DateBooking BETWEEN ? AND ?) "
                "ORDER BY ParentBooking_ID, DateBooking", rng)
        else:
            cursor.execute(
                "SELECT * FROM Bookings WHERE ParentBooking_ID IS NOT NULL "
                "ORDER BY ParentBooking_ID, DateBooking")
        children_by_parent = {}
        for r in cursor.fetchall():
            pid = r[18]  # ParentBooking_ID
            children_by_parent.setdefault(pid, []).append(self._euro_row(r, 11, 14))

        # 3. Normal (ungrouped) bookings — not bank, not child, not in legacy group
        #    Rein liquide Spiegelbuchungen und resolved Debitoren ausblenden.
        normal_sql = (
            "SELECT * FROM Bookings "
            "WHERE (BookingType IS NULL OR BookingType = 'entry') "
            "AND ParentBooking_ID IS NULL "
            "AND (Status IS NULL OR Status != 'resolved')")
        if use_range:
            cursor.execute(normal_sql + " AND DateBooking BETWEEN ? AND ? "
                           "ORDER BY DateBooking DESC", rng)
        else:
            cursor.execute(normal_sql + " ORDER BY DateBooking DESC")
        normal = []
        for r in cursor.fetchall():
            coa_id = r[8]          # COA_ID
            counter_coa_id = r[9]  # CounterCOA_ID
            if is_mirror_row(r[22], coa_id, counter_coa_id, doppik_coa_ids):  # EntryForm
                continue  # Doppik-Eintrag verbergen
            normal.append({'type': 'normal', 'date': r[1] or '', 'booking': self._euro_row(r, 11, 14)})

        conn.close()

        # 3b. Belege ohne Bankbewegung zusammenfassen (Kasse, Verrechnungskonto,
        #     manuelle Splits): mehrere Buchungssätze desselben Belegs am selben
        #     Tag gehören zusammen. Bei Bankbewegungen leistet das
        #     ParentBooking_ID – hier gibt es keine, und ohne diese Klammer
        #     stünden die Teile lose nebeneinander.
        normal = self._group_entries_by_document(normal)

        # Build bank dicts with merged entry data
        banks = []
        for b in bank_rows:
            bid = b[0]
            raw_children = children_by_parent.get(bid, [])
            children = [
                {'type': 'child', 'group_id': f'b{bid}', 'date': c[1] or '', 'booking': c}
                for c in raw_children
            ]
            # Merge: ersten (nicht-Doppik) Child als Entry-Quelle nutzen
            entry_src = None
            for c in raw_children:
                if not is_mirror_row(c[22], c[8], c[9], doppik_coa_ids):
                    entry_src = c
                    break
            banks.append({
                'type':     'bank',
                'date':     b[1] or '',
                'booking':  b,
                'children': children,
                'linked':   len(raw_children) > 0,
                'entry_text':             entry_src[15] if entry_src else None,
                'entry_coa_id':           entry_src[8]  if entry_src else None,
                'entry_counter_coa_id':   entry_src[9]  if entry_src else None,
                'entry_docnr':            entry_src[16] if entry_src else None,
                'entry_category_id':      entry_src[10] if entry_src else None,
                'entry_contact_id':       entry_src[7]  if entry_src else None,
                'entry_tax_rate':         entry_src[13] if entry_src else None,
            })

        # Merge top-level items sorted by date descending
        top_level = banks + normal
        top_level.sort(key=lambda x: x['date'], reverse=True)

        # Build flat result: parent row immediately followed by its children
        result = []
        for item in top_level:
            result.append(item)
            if item['type'] in ('bank', 'group'):
                result.extend(item.get('children', []))

        return result
    def insert_booking(self, date_booking, amount, account_id=None, foreign_bank_account="", 
                       recipient_client="", contact_id=None, coa_id=None, category_id=None,
                       currency="EUR", tax_rate=None, tax_amount=None, text="", 
                       document_number=None, date_tax=None,
                       counter_coa_id=None, log_description=None,
                       booking_type='entry', parent_booking_id=None,
                       auto_mirror=False, entry_form=None, tx=None, bank_details=None):
        """Insert a new booking into Bookings table
        
        Args:
            date_booking: Transaction date (required)
            amount: Amount (positive = credit/Haben, negative = debit/Soll)
            account_id: FK to Accounts table
            foreign_bank_account: External IBAN/account number
            recipient_client: Name of recipient/client
            contact_id: FK to Contacts table
            coa_id: FK to ChartOfAccounts (SKR) - Sollkonto
            counter_coa_id: FK to ChartOfAccounts (SKR) - Habenkonto/Gegenkonto
            category_id: FK to Categories
            currency: Currency code (default: EUR)
            tax_rate: Tax rate as decimal (e.g., 0.19 for 19%)
            tax_amount: Calculated tax amount
            text: Notes/purpose
            document_number: External document reference
            date_tax: Tax date (optional)
            log_description: Description for SQL logging (optional)
            booking_type: 'bank', 'entry', or 'split_child' (default: 'entry')
            parent_booking_id: FK to parent Bookings row (bank transaction)
            entry_form: Form des Buchungssatzes (``db.matching.ENTRY_FORMS``). Ohne Angabe wird sie
                bei einem ``entry`` aus den Konten abgeleitet (``derive_entry_form`` mit den
                Zahlungskonten von jetzt): Der Betrag bezieht sich auf das Gegenkonto, wie die
                Buchungsmaske ihn schreibt – ein Spiegel wird so ``mirror_counter``. Schreiber mit
                einer anderen Konvention (Importe, Umbuchungen) geben die Form ausdrücklich an.
                Eine Bankzeile hat keine Form.
            tx: optionale :class:`db.core.Transaction` – dann wird darin geschrieben und erst mit
                ihr committet (der Eintrag im Replay-Log folgt nach dem Commit)
            bank_details: die Zusatzfelder einer Bankbewegung aus CAMT als Dict (``value_date``, ``end_to_end_id``, ``mandate_ref``,
                ``creditor_id``, ``bank_ref``). Nur bei ``booking_type='bank'`` und nur, wenn nach ``clean_value_date``/``clean_bank_text``
                mindestens ein Wert bleibt, entsteht eine Zeile in ``BankTransactionDetails`` – in derselben Verbindung vor dem Commit
                (leere Werte als NULL); sie kommt wie der Satz ins Replay-Log.
        
        Returns:
            int: ID of inserted booking
        """
        if entry_form is not None and entry_form not in ENTRY_FORMS:
            raise ValueError(f"Unbekannte Form eines Buchungssatzes: {entry_form!r}")
        conn = tx.conn if tx else self._get_connection()
        cursor = conn.cursor()
        if booking_type != 'entry':
            entry_form = None
        elif entry_form is None:
            entry_form = derive_entry_form(coa_id, counter_coa_id,
                                           self._get_payment_coa_ids(cursor), SIDE_COUNTER)
        
        sql_template = '''INSERT INTO Bookings
            (DateBooking, DateTax, Account_ID, ForeignBankAccount,
             RecipientClient, Contact_ID, COA_ID, CounterCOA_ID, Category_ID, Amount, Currency,
             TaxRate, TaxAmount, Text, DocumentNumber, BookingType, ParentBooking_ID, AutoMirror,
             EntryForm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''

        params = (date_booking, date_tax, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, counter_coa_id, category_id,
                  to_minor(amount or 0), currency,
                  tax_rate, self._minor_opt(tax_amount), text, document_number, booking_type,
                  parent_booking_id, 1 if auto_mirror else 0, entry_form)

        cursor.execute(sql_template, params)
        last_id = cursor.lastrowid
        details_sql = details_params = None
        if booking_type == 'bank' and bank_details:
            from importers.base import clean_bank_text, clean_value_date
            values = (clean_value_date(bank_details.get('value_date')), clean_bank_text(bank_details.get('end_to_end_id')),
                      clean_bank_text(bank_details.get('mandate_ref')), clean_bank_text(bank_details.get('creditor_id')),
                      clean_bank_text(bank_details.get('bank_ref')))
            if any(values):
                details_sql = ('INSERT INTO BankTransactionDetails (Booking_ID, ValueDate, EndToEndId, MandateRef, CreditorId, BankRef) '
                               'VALUES (?, ?, ?, ?, ?, ?)')
                details_params = (last_id, *(value or None for value in values))
                cursor.execute(details_sql, details_params)
        if not tx:
            conn.commit()
            conn.close()
        
        # Optional SQL logging
        if log_description:
            entries = [(sql_template, params, log_description)]
            if details_sql:
                entries.append((details_sql, details_params, 'Insert bank transaction details'))
            for entry in entries:
                if tx:
                    tx.log.append(entry)
                else:
                    self._log_sql(*entry)
        
        return last_id
    def backfill_booking_fields(self, booking_id, coa_id=None, tax_rate=None,
                                tax_amount=None, contact_id=None,
                                document_number=None):
        """Leere Felder einer Buchung nachtragen – vorhandene Werte werden
        NIE überschrieben (COALESCE). Für die automatische SKR-Zuordnung beim
        nachträglichen Verknüpfen einer Zahlung mit einer Rechnung (todo #2).

        Bekommt ein Buchungssatz sein Konto erst hier, ändert sich seine Kontierung: Die Form
        wird neu abgeleitet (``db.matching.reform``), der Betrag behält sein Soll/Haben – und mit
        ihm der gespeicherte Steuerbetrag, der das Vorzeichen des Betrags trägt. Dann steht die ganze
        Änderung (Konto, Form, negierter Betrag und Steuerbetrag) als eine Anweisung im Protokoll – sonst
        ließe sich der Stand nicht nachspielen.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        old = cursor.execute('SELECT BookingType, EntryForm, COA_ID, CounterCOA_ID, Amount, TaxAmount '
                             'FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()
        form = amount = negated_tax = None
        if old and old[0] == 'entry' and old[2] is None and coa_id is not None:
            money_ids = self._get_payment_coa_ids(cursor)
            old_form = read_entry_form(old[1], old[2], old[3], money_ids)
            form, new_amount = reform(old_form, old[2], old[3], coa_id, old[3], money_ids, old[4])
            amount = new_amount if new_amount != old[4] else None
            if amount is not None and old[5]:
                negated_tax = -old[5]
        sql_template = '''
            UPDATE Bookings SET
                COA_ID     = COALESCE(COA_ID, ?),
                TaxRate    = COALESCE(TaxRate, ?),
                TaxAmount  = COALESCE(?, TaxAmount, ?),
                Contact_ID = COALESCE(Contact_ID, ?),
                DocumentNumber = CASE WHEN DocumentNumber IS NULL
                                        OR DocumentNumber = ''
                                      THEN ? ELSE DocumentNumber END,
                EntryForm  = COALESCE(?, EntryForm),
                Amount     = COALESCE(?, Amount)
            WHERE ID = ?
        '''
        params = (coa_id, tax_rate, negated_tax, self._minor_opt(tax_amount), contact_id,
                  document_number, form, amount, booking_id)
        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()
        if form is not None:
            self._log_sql(sql_template, params,
                          'Buchung nachgetragen: Konto, Form, Betrag und Steuerbetrag')

    def check_booking_exists(self, date, amount, account_id=None, foreign_bank_account="", text=""):
        """Anzahl vorhandener Buchungen mit gleichem Datum/Betrag/Konto.

        Absichtlich OHNE Text-/IBAN-Vergleich: beide Felder überschreibt der
        WISO-Tabellen-Export-Import nachträglich (Verwendungszweck, Konto-Nr.),
        wodurch ein Kontoauszug-Re-Import die Buchungen sonst nicht wiederfindet.
        Rückgabe ist die Anzahl (truthy = vorhanden), damit Aufrufer bei
        mehrfach identischen Transaktionen zählbasiert deduplizieren können.
        foreign_bank_account/text bleiben aus API-Kompatibilität erhalten.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM Bookings
            WHERE DateBooking=? AND Amount=? AND Account_ID=?
        ''', (date, to_minor(amount or 0), account_id))
        count = cursor.fetchone()[0]
        conn.close()
        return count
    def bank_stock(self, date, amount, account_id):
        """Der Bestand, den ``check_booking_exists`` zählt – dieselbe Bedingung (Datum, Betrag, Konto), mit der Bankreferenz je Zeile: ``[(Buchungs-ID, Bankreferenz)]``, nach ID, ``''`` =
        keine (oder nicht lesbar: ``clean_bank_text``). Die Länge der Liste ist genau die Zahl, die ``check_booking_exists`` liefert; dieses bleibt unverändert (auch für ``db/wiso_import.py``)."""
        from importers.base import clean_bank_text
        conn = self._get_connection()
        try:
            rows = conn.execute('''
                SELECT b.ID, d.BankRef FROM Bookings b
                LEFT JOIN BankTransactionDetails d ON d.Booking_ID = b.ID
                WHERE b.DateBooking=? AND b.Amount=? AND b.Account_ID=?
                ORDER BY b.ID
            ''', (date, to_minor(amount or 0), account_id)).fetchall()
        finally:
            conn.close()
        return [(booking_id, clean_bank_text(bank_ref)) for booking_id, bank_ref in rows]
    def get_bank_details(self, booking_id):
        """Die Zusatzfelder der Bankbewegung ``booking_id`` aus ihrem CAMT-Auszug als :class:`BankDetails` – oder ``None``, wenn es keine Zeile gibt oder nichts davon lesbar ist. Jeder Wert geht
        erneut durch ``clean_value_date`` bzw. ``clean_bank_text`` (die SQL-Konsole kann alles schreiben): Unlesbares wird ``''``. Rein lesend."""
        from importers.base import clean_bank_text, clean_value_date
        conn = self._get_connection()
        try:
            row = conn.execute('SELECT ValueDate, EndToEndId, MandateRef, CreditorId, BankRef FROM BankTransactionDetails WHERE Booking_ID = ?',
                               (booking_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        details = BankDetails(clean_value_date(row[0]), *(clean_bank_text(value) for value in row[1:]))
        return details if any(details) else None
    def find_history_suggestion(self, recipient, exclude_booking_id=None):
        """Zuletzt kontierte Buchung mit ähnlichem Empfänger (todo #1).

        Grundlage der Vorschlagsfunktion: Wer denselben Zahlungspartner schon
        einmal kontiert hat, will fast immer dasselbe SKR-Konto und denselben
        Steuersatz wieder. Gesucht wird zuerst exakt (Leerzeichen ignoriert,
        Groß-/Kleinschreibung egal), danach über das längste Wort des
        Empfängers – Kontoauszüge schreiben denselben Partner selten zweimal
        identisch ("O2 Germany GmbH" vs. "O2 Germany").

        Gefunden wird die Buchung; geliefert werden ihre Buchungssätze: bei
        einem Split alle Teilbuchungen, sonst die eine. Damit kann der
        Aufrufer eine wiederkehrende Aufteilung als Ganzes übernehmen.

        Returns dict oder None:
          {'source_id', 'date', 'recipient', 'is_split',
           'rows': [{'coa_id', 'counter_coa_id', 'amount'€, 'tax_rate',
                     'tax_amount'€, 'text', 'document_number'}]}
        """
        norm = ' '.join((recipient or '').split()).replace(' ', '').lower()
        if not norm:
            return None
        conn = self._get_connection()
        cursor = conn.cursor()

        exclude = exclude_booking_id or -1
        # Teilbuchungen tragen den Empfänger nicht immer selbst – bei
        # importierten Splits steht er nur an der Bankbewegung. Deshalb zählt
        # ersatzweise der Empfänger des Elternteils.
        base = '''
            SELECT b.ID, b.DateBooking,
                   COALESCE(NULLIF(b.RecipientClient, ''), p.RecipientClient),
                   b.ParentBooking_ID, b.BookingType
            FROM Bookings b
            LEFT JOIN Bookings p ON p.ID = b.ParentBooking_ID
            WHERE b.COA_ID IS NOT NULL
              AND COALESCE(NULLIF(b.RecipientClient, ''), p.RecipientClient, '') != ''
              AND b.ID != ? AND COALESCE(b.ParentBooking_ID, -1) != ?
        '''
        eff_recipient = ("COALESCE(NULLIF(b.RecipientClient, ''),"
                         " p.RecipientClient, '')")
        cursor.execute(base + f'''
              AND LOWER(REPLACE({eff_recipient}, ' ', '')) = ?
            ORDER BY b.DateBooking DESC, b.ID DESC LIMIT 1
        ''', (exclude, exclude, norm))
        hit = cursor.fetchone()

        if not hit:
            # Fallback: längstes Wort des Empfängers als Kern (>= 4 Zeichen)
            words = sorted((recipient or '').split(), key=len, reverse=True)
            token = next((w for w in words if len(w) >= 4), None)
            if token:
                cursor.execute(base + f'''
                      AND LOWER({eff_recipient}) LIKE ?
                    ORDER BY b.DateBooking DESC, b.ID DESC LIMIT 1
                ''', (exclude, exclude, f'%{token.lower()}%'))
                hit = cursor.fetchone()

        if not hit:
            conn.close()
            return None

        hit_id, date, found_recipient, parent_id, booking_type = hit
        # Buchungssätze der Fundstelle: Split → alle Geschwister, sonst sie selbst
        group_parent = parent_id or (hit_id if booking_type == 'bank' else None)
        if group_parent:
            cursor.execute('''
                SELECT COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount,
                       Text, DocumentNumber, EntryForm
                FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID
            ''', (group_parent,))
        else:
            cursor.execute('''
                SELECT COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount,
                       Text, DocumentNumber, EntryForm
                FROM Bookings WHERE ID = ?
            ''', (hit_id,))
        raw = cursor.fetchall()
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        conn.close()

        rows = []
        for coa, counter, amount, rate, tax, text, docnr, stored_form in raw:
            if is_mirror_row(stored_form, coa, counter, bank_coa_ids):
                continue                      # reiner Doppik-Spiegel
            # Geldkonto vorne („Bank an 1460“ eingeschlossen): das Zweckkonto steht hinten
            purpose = counter if purpose_in_counter(stored_form, coa, counter,
                                                    bank_coa_ids) else coa
            # Umbuchungen (Privatanteil, Wartekonto) brauchen ihr Gegenkonto
            # mit: ohne das würde eine Kopie zur Bankbewegung und die Summe
            # der Teilbuchungen stimmte nicht mehr.
            nobank = not is_bank_effective(coa, counter, bank_coa_ids)
            rows.append({
                'coa_id': purpose,
                'counter_coa_id': counter if nobank else None,
                'nobank': nobank,
                'amount': from_minor(amount or 0),
                'tax_rate': rate,
                'tax_amount': from_minor(tax) if tax is not None else None,
                'text': text or '',
                'document_number': docnr or '',
            })
        if not rows:
            return None
        return {'source_id': group_parent or hit_id, 'date': date,
                'recipient': found_recipient, 'is_split': len(rows) > 1,
                'rows': rows}

    def get_bookings_by_import_key(self, date, amount, account_id):
        """Buchungen zum Import-Duplikatschlüssel (Datum, Betrag, Konto).

        Grundlage für den Text-Backfill beim Re-Import von Kontoauszügen.
        Returns: [(ID, Text)]
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ID, Text FROM Bookings
            WHERE DateBooking=? AND Amount=? AND Account_ID=?
        ''', (date, to_minor(amount or 0), account_id))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def update_booking_text(self, booking_id, text):
        """Nur das Text-Feld einer Buchung setzen (Text-Backfill).

        Entry-Kindbuchungen, deren Text noch leer ist oder dem alten
        Bank-Text entspricht, ziehen mit – Bank-Zeile und Buchungssatz
        bleiben so konsistent. Abweichende Kind-Texte bleiben unberührt.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT Text FROM Bookings WHERE ID=?', (booking_id,))
        row = cursor.fetchone()
        old_text = row[0] if row else None
        cursor.execute('UPDATE Bookings SET Text=? WHERE ID=?',
                       (text, booking_id))
        cursor.execute('''
            UPDATE Bookings SET Text=?
            WHERE ParentBooking_ID=? AND (Text IS NULL OR Text='' OR Text=?)
        ''', (text, booking_id, old_text or ''))
        conn.commit()
        conn.close()

    def sync_entry_child(self, child_id, date_booking, date_tax, amount,
                         recipient_client, coa_id, counter_coa_id, currency,
                         tax_rate, tax_amount, text, document_number,
                         log_description=None):
        """Auto-erzeugten Buchungssatz an die geänderte Bank-Buchung angleichen.

        NUR für Kinder aufrufen, die nachweislich der 1:1-Spiegel der
        Bank-Zeile sind (siehe handle_add_transaction) – eigenständig
        erfasste, bloß verknüpfte Buchungen haben eigene Angaben und eine
        eigene Kontierungsrichtung, die hier zerstört würden.

        Betrag und Steuer werden immer gemeinsam gesetzt: TaxAmount ist ein
        aus dem Betrag abgeleiteter Wert; einzeln geschrieben ergäbe die
        EÜR-Rechnung ``Netto = Amount − TaxAmount`` einen erfundenen Wert.
        Contact_ID bleibt bewusst erhalten (die Maske kennt nur Kunden-
        Kontakte und würde andere stillschweigend leeren), ebenso
        Account_ID, BookingType, ParentBooking_ID, Status.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        # Die Maske schreibt den Betrag der Bankbewegung, das Gegenkonto ist das Konto der Bank:
        # Zweckkonto vorne, bei zwei Geldkonten ein Spiegel mit Betrag auf dem Gegenkonto.
        form = derive_entry_form(coa_id, counter_coa_id, self._get_payment_coa_ids(cursor),
                                 SIDE_COUNTER)
        sql_template = '''UPDATE Bookings
            SET DateBooking=?, DateTax=?, RecipientClient=?, COA_ID=?,
                CounterCOA_ID=?, Amount=?, Currency=?, TaxRate=?, TaxAmount=?,
                Text=?, DocumentNumber=?, EntryForm=?
            WHERE ID=?'''
        params = (date_booking, date_tax, recipient_client, coa_id,
                  counter_coa_id, to_minor(amount or 0), currency,
                  tax_rate, self._minor_opt(tax_amount), text, document_number,
                  form, child_id)
        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()
        if log_description:
            self._log_sql(sql_template, params, log_description)

    _BOOKING_FIELD_COLUMNS = {
        'date_booking': 'DateBooking', 'date_tax': 'DateTax', 'account_id': 'Account_ID',
        'foreign_bank_account': 'ForeignBankAccount', 'recipient_client': 'RecipientClient',
        'contact_id': 'Contact_ID', 'coa_id': 'COA_ID', 'counter_coa_id': 'CounterCOA_ID',
        'category_id': 'Category_ID', 'amount': 'Amount', 'currency': 'Currency',
        'tax_rate': 'TaxRate', 'tax_amount': 'TaxAmount', 'text': 'Text',
        'document_number': 'DocumentNumber', 'parent_booking_id': 'ParentBooking_ID',
        'entry_form': 'EntryForm', 'datev_key': 'DatevKey',
    }

    def _datev_key_stale(self, conn, booking_id, values):
        """Fällt der mitgebrachte BU-Schlüssel (``Bookings.DatevKey``) dieses Satzes weg? Ja, wenn er einen hat und
        sich Konto, Gegenkonto oder Steuersatz gegenüber dem gespeicherten Stand ändern – er passt dann nicht mehr
        zu den Konten und zum Satz, der Export leitet ihn neu ab. ``values`` sind die geänderten Spalten, wie
        ``update_booking_fields`` sie schreibt; nennt der Aufrufer ``datev_key`` selbst, bestimmt er ihn
        (nie wegfallen). Der Steuersatz zählt als Prozentzahl: 0.19 und 19 sind derselbe, kein Satz und 0 auch. Die
        EINE Stelle für ``update_booking_fields`` und ``update_booking``; kein Schreibweg der Masken setzt den
        Schlüssel, die Importe füllen ihn nur."""
        if 'datev_key' in values or not ({'coa_id', 'counter_coa_id', 'tax_rate'} & set(values)):
            return False
        old = conn.execute('SELECT DatevKey, COA_ID, CounterCOA_ID, TaxRate FROM Bookings WHERE ID=?',
                           (booking_id,)).fetchone()
        if old is None or old[0] is None:
            return False
        return (('coa_id' in values and values['coa_id'] != old[1])
                or ('counter_coa_id' in values and values['counter_coa_id'] != old[2])
                or ('tax_rate' in values and datev_keys.percent(values['tax_rate']) != datev_keys.percent(old[3])))

    def _form_after_accounts_change(self, conn, booking_id, values):
        """Form eines Buchungssatzes, dessen Konto oder Gegenkonto sich ändert – ``values`` sind
        die geänderten Spalten (Minor Units), wie ``update_booking_fields`` sie schreibt. Nur für
        einen ``entry``, dessen Konten sich wirklich ändern und für den die Form nicht
        ausdrücklich genannt ist; sonst ``None``. Returns ``(form, amount, tax_amount)``: ``amount``
        ist der negierte Betrag, wenn die Form auf die andere Seite kippt und der Aufrufer keinen
        eigenen mitgibt (``db.matching.reform``), sonst ``None`` – dann bleibt der Betrag, wie er ist.
        Ein Betrag, der unverändert zurückkommt, gilt als nicht mitgegeben: Die Masken schicken
        den gespeicherten Betrag immer mit, und ein Wechsel des Zweckkontos soll Soll/Haben
        erhalten (ein anderer Betrag gilt dagegen für die neue Kontierung).

        Mit dem Betrag dreht der **Steuerbetrag**: Er trägt das Vorzeichen des Betrags (die EÜR
        rechnet Netto = Betrag − Steuer). ``tax_amount`` ist der negierte gespeicherte Steuerbetrag,
        wenn der Betrag negiert wird und der Aufrufer ihn nicht oder unverändert mitgibt; ein Steuerbetrag,
        den der Aufrufer ändert (auch leert), gilt wie eingegeben, ebenso ein Satz ohne Steuer
        – dann ist es ``None`` und der Aufrufer schreibt, was er hat. Alle Schreibwege, die den Betrag
        so negieren, nehmen diesen Wert mit (``update_booking_fields``, ``update_booking``, der
        Tabellen-Export, ``backfill_booking_fields``)."""
        if 'entry_form' in values or not ({'coa_id', 'counter_coa_id'} & set(values)):
            return None
        old = conn.execute('SELECT BookingType, EntryForm, COA_ID, CounterCOA_ID, Amount, TaxAmount '
                           'FROM Bookings WHERE ID=?', (booking_id,)).fetchone()
        if old is None or old[0] != 'entry':
            return None
        coa_id = values.get('coa_id', old[2])
        counter_coa_id = values.get('counter_coa_id', old[3])
        if (coa_id, counter_coa_id) == (old[2], old[3]):
            return None
        money_ids = self._get_payment_coa_ids(conn.cursor())
        old_form = read_entry_form(old[1], old[2], old[3], money_ids)
        form, amount = reform(old_form, old[2], old[3], coa_id, counter_coa_id, money_ids, old[4])
        resent = values.get('amount', old[4]) == old[4]
        negated = resent and amount != old[4]
        tax_amount = None
        if negated and old[5] and values.get('tax_amount', old[5]) == old[5]:
            tax_amount = -old[5]
        return form, (amount if negated else None), tax_amount

    def update_booking_fields(self, booking_id, log_description=None, tx=None, **fields):
        """Nur die genannten Felder einer Buchung ändern; alle anderen bleiben, wie sie sind.

        Für Masken, die nicht jede Spalte führen: Was das Formular nicht kennt
        (Gegenkonto, Kategorie …), darf beim Speichern nicht geleert werden.
        Feldnamen wie bei ``insert_booking``; ``amount``/``tax_amount`` in Euro.

        Ändern sich Konto oder Gegenkonto eines Buchungssatzes, bekommt er die dazu passende
        Form (``db.matching.reform``), sofern der Aufrufer sie nicht selbst als ``entry_form``
        nennt. Ohne eigenen ``amount`` – oder mit dem unveränderten gespeicherten – bleibt dabei
        das Soll/Haben erhalten: Kippt die Form auf die andere Seite, wird der Betrag negiert –
        und mit ihm der Steuerbetrag, sofern ihn der Aufrufer nicht ändert
        (``_form_after_accounts_change``).

        Ändern sich Konto, Gegenkonto oder Steuersatz, fällt der mitgebrachte BU-Schlüssel
        (``datev_key``) weg, sofern der Aufrufer ihn nicht selbst nennt (``_datev_key_stale``).
        ``datev_key`` selbst ist ein Schlüssel von 1 bis 9999 oder ``None`` (sonst ``ValueError``).

        ``tx``: optionale :class:`db.core.Transaction` – dann wird darin geschrieben, ohne Commit und ohne die Verbindung zu
        schließen, und der Eintrag im Replay-Log folgt erst nach ihrem Commit (wie bei ``insert_booking``).
        """
        unknown = set(fields) - set(self._BOOKING_FIELD_COLUMNS)
        if unknown:
            raise TypeError(f"Unbekannte Buchungsfelder: {sorted(unknown)}")
        if not fields:
            return
        if fields.get('entry_form') is not None and fields['entry_form'] not in ENTRY_FORMS:
            raise ValueError(f"Unbekannte Form eines Buchungssatzes: {fields['entry_form']!r}")
        values = dict(fields)
        if 'amount' in values:
            values['amount'] = to_minor(values['amount'] or 0)
        if 'tax_amount' in values:
            values['tax_amount'] = self._minor_opt(values['tax_amount'])
        if 'datev_key' in values:
            values['datev_key'] = datev_keys.checked_key(values['datev_key'])
        conn = tx.conn if tx else self._get_connection()
        try:
            if self._datev_key_stale(conn, booking_id, values):
                values['datev_key'] = None
            reformed = self._form_after_accounts_change(conn, booking_id, values)
            if reformed is not None:
                values['entry_form'] = reformed[0]
                if reformed[1] is not None:
                    values['amount'] = reformed[1]
                if reformed[2] is not None:
                    values['tax_amount'] = reformed[2]
            assignments = ', '.join(f"{self._BOOKING_FIELD_COLUMNS[name]}=?" for name in values)
            sql_template = f"UPDATE Bookings SET {assignments} WHERE ID=?"
            params = tuple(values.values()) + (booking_id,)
            conn.execute(sql_template, params)
            if not tx:
                conn.commit()
        finally:
            if not tx:
                conn.close()
        if log_description:
            if tx:
                tx.log.append((sql_template, params, log_description))
            else:
                self._log_sql(sql_template, params, log_description)

    def update_booking(self, booking_id, date_booking, amount, account_id=None,
                       foreign_bank_account="", recipient_client="", contact_id=None, 
                       coa_id=None, category_id=None, currency="EUR", tax_rate=None, 
                       tax_amount=None, text="", document_number=None, 
                       date_tax=None, counter_coa_id=None, log_description=None,
                       booking_type=None, parent_booking_id=None):
        """Update an existing booking
        
        Setzt ALLE Spalten – Masken, die nicht alle Felder führen, nehmen
        ``update_booking_fields``.
        
        Args:
            booking_id: ID of booking to update
            [same parameters as insert_booking]
            booking_type: 'bank', 'entry', or 'split_child' (None = keep current)
            parent_booking_id: FK to parent Bookings row (None = keep current)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Ändern sich die Konten eines Buchungssatzes, bekommt er die dazu passende Form
        # (kippt sie, drehen Betrag und unveränderter Steuerbetrag mit)
        reformed = self._form_after_accounts_change(
            conn, booking_id, {'coa_id': coa_id, 'counter_coa_id': counter_coa_id,
                               'amount': to_minor(amount or 0),
                               'tax_amount': self._minor_opt(tax_amount)})
        # Ändern sich Konto, Gegenkonto oder Steuersatz, fällt der mitgebrachte BU-Schlüssel weg
        key_stale = self._datev_key_stale(
            conn, booking_id, {'coa_id': coa_id, 'counter_coa_id': counter_coa_id, 'tax_rate': tax_rate})
        sql_template = '''UPDATE Bookings
            SET DateBooking=?, DateTax=?, Account_ID=?, ForeignBankAccount=?,
                RecipientClient=?, Contact_ID=?, COA_ID=?, CounterCOA_ID=?, Category_ID=?, Amount=?, Currency=?,
                TaxRate=?, TaxAmount=?, Text=?, DocumentNumber=?, BookingType=COALESCE(?, BookingType), ParentBooking_ID=COALESCE(?, ParentBooking_ID),
                EntryForm=COALESCE(?, EntryForm), DatevKey=CASE WHEN ? THEN NULL ELSE DatevKey END
            WHERE ID=?'''
        
        params = (date_booking, date_tax, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, counter_coa_id, category_id,
                  reformed[1] if reformed and reformed[1] is not None else to_minor(amount or 0),
                  currency,
                  tax_rate,
                  reformed[2] if reformed and reformed[2] is not None else self._minor_opt(tax_amount),
                  text, document_number, booking_type, parent_booking_id,
                  reformed[0] if reformed else None, 1 if key_stale else 0, booking_id)

        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()

        # Optional SQL logging
        if log_description:
            self._log_sql(sql_template, params, log_description)
    @staticmethod
    def _delete_booking_rows(conn, booking_ids):
        """Buchungen samt aller Verweise auf sie löschen, in der Transaktion von ``conn``:
        BookingDocuments und InvoicePayments werden gelöscht, Assets.Booking_ID wird auf NULL
        gesetzt. Eine AfA-Planzeile verliert ihre Buchung und geht zurück auf 'planned' – das
        Jahr lässt sich neu buchen. Gelöscht wird nur, was in ``booking_ids`` steht – die
        Kinder davon zuerst, dann die übrigen (Fremdschlüssel)."""
        if not booking_ids:
            return
        marks = ','.join('?' * len(booking_ids))
        conn.execute(f'DELETE FROM BookingDocuments WHERE Booking_ID IN ({marks})', booking_ids)
        conn.execute(f'DELETE FROM InvoicePayments WHERE BookingID IN ({marks})', booking_ids)
        conn.execute(f'UPDATE Assets SET Booking_ID = NULL WHERE Booking_ID IN ({marks})',
                     booking_ids)
        conn.execute("UPDATE AssetDepreciations SET Booking_ID = NULL, Status = 'planned', "
                     f"BookedAt = NULL WHERE Booking_ID IN ({marks})", booking_ids)
        conn.execute(f'DELETE FROM Bookings WHERE ID IN ({marks}) AND ParentBooking_ID IN ({marks})',
                     booking_ids + booking_ids)
        conn.execute(f'DELETE FROM Bookings WHERE ID IN ({marks})', booking_ids)

    @staticmethod
    def _booking_attachments_exist(conn, booking_ids):
        """Hängt an einer dieser Buchungen etwas, das ``_delete_booking_rows`` mit ihr wegräumt oder umbiegt – ein Beleg, eine
        Zuordnung, eine Anlage, ein AfA-Jahr, eine Kindbuchung (``BOOKING_REFERENCES``)? Rein lesend, in der Transaktion von
        ``conn``. Wer Buchungen löscht und neu schreibt, darf das nur, wenn nichts daran hängt: Es ginge sonst mit verloren."""
        if not booking_ids:
            return False
        marks = ','.join('?' * len(booking_ids))
        sql = ' UNION ALL '.join(f'SELECT 1 FROM {table} WHERE {column} IN ({marks})' for table, column in BOOKING_REFERENCES)
        return conn.execute(f'{sql} LIMIT 1', list(booking_ids) * len(BOOKING_REFERENCES)).fetchone() is not None

    def delete_transaction(self, booking_id: int):
        """Buchung (und verknüpfte Kinder via ParentBooking_ID) löschen – in EINER Transaktion,
        auch die Neuberechnung der betroffenen Rechnungen.

        Bereinigt vor dem Löschen alle referenzierenden Zeilen (``_delete_booking_rows``).
        Rechnungen gelöschter Zahlungen werden neu berechnet (AmountDue/Status), damit keine
        verwaisten "bezahlt"-Zustände zurückbleiben (todo #2).

        * **Bankbewegung:** Ihre Kinder und alle Zuordnungen gehen mit. Eine Forderungsbuchung
          (``db.matching.receivable_condition``), die ein Altbestand als Kind angehängt hat,
          gehört zur Rechnung und bleibt: Sie wird abgehängt (``ParentBooking_ID`` und Status
          leer) und ist damit wieder offen.
        * **Buchungssatz einer Zuordnung** (Marke ``'invoice_payment'``, auch aus dem
          Split-Editor): Die ganze Zuordnung wird entfernt (``unassign_invoice_payment``) –
          Zahlungs- und Umbuchungssatz bleiben nie einzeln übrig.
        * **Eintrag mit eigener Zuordnung** (Altbestand): Die Zuordnung wird gelöscht.

        Eine **Zahlung** an einer stornierten Rechnung mit Stornorechnung geht auf keinem dieser Wege verloren, wenn danach mehr
        erstattet als gezahlt wäre: ``PaymentRemovalRefused`` (``_refund_removal_blocker``, vor dem ersten Schreiben in der
        Transaktion geprüft, nichts ist geändert) – erst die Erstattung an der Stornorechnung entfernen. Das Löschen einer
        Erstattung bleibt frei. Ebenso geht keine Zahlung, solange eine andere Zuordnung derselben Rechnung ein Skonto trägt
        (``SKONTO_REMOVAL_REFUSED``) – erst die Zahlung mit dem Skonto entfernen; die Bewegung, die beide trägt, geht ganz.
        """
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            self._delete_transaction_in(conn, booking_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _delete_transaction_in(self, conn, booking_id):
        """Der Körper von ``delete_transaction`` in der laufenden Transaktion von ``conn``, die der Aufrufer mit
        ``BEGIN IMMEDIATE`` eröffnet hat: ohne Beginn, Commit und Rollback – das gehört dem Aufrufer. Wirft
        :class:`PaymentRemovalRefused` vor dem ersten Schreiben. Wer ihn innerhalb einer Transaktion braucht (der Bank-Editor,
        ``save_bank_with_rows``), darf ``delete_transaction`` nicht aufrufen: Sie öffnet eine eigene Verbindung und hängte an
        der Schreibsperre der laufenden."""
        row = conn.execute('SELECT Status, ParentBooking_ID FROM Bookings WHERE ID = ?',
                           (booking_id,)).fetchone()
        if row is None:
            return
        if row[0] == PAYMENT_MARK and row[1] is not None:
            self.remove_assignment_for_child(conn, booking_id)
            return

        deleted_ids, kept_ids = self._delete_scope(conn, booking_id)
        marks = ','.join('?' * len(deleted_ids))
        payment_ids = [r[0] for r in conn.execute(
            f'SELECT ID FROM InvoicePayments WHERE BookingID IN ({marks}) ORDER BY ID', deleted_ids)]
        blocker = self._refund_removal_blocker(conn, payment_ids)
        if blocker:
            raise PaymentRemovalRefused(blocker)
        affected_invoice_ids = [r[0] for r in conn.execute(
            f'SELECT DISTINCT InvoiceID FROM InvoicePayments WHERE BookingID IN ({marks})',
            deleted_ids)]

        if kept_ids:
            kept_marks = ','.join('?' * len(kept_ids))
            conn.execute('UPDATE Bookings SET ParentBooking_ID = NULL, Status = NULL '
                         f'WHERE ID IN ({kept_marks})', kept_ids)
        self._delete_booking_rows(conn, deleted_ids)
        for invoice_id in affected_invoice_ids:
            self._after_payment_removed(conn, invoice_id)

    def _delete_scope(self, conn, booking_id):
        """Was ``delete_transaction`` mit dieser Buchung (keinem markierten Satz einer Zuordnung) tut: ``(gelöscht, abgehängt)``
        als ID-Listen – die Buchung und ihre Kinder, außer den Forderungsbuchungen (``db.matching.receivable_condition``), die
        nur abgehängt werden. Rein lesend, in der Transaktion von ``conn``."""
        receivable_sql, receivable_params = self._receivable_sql(conn.cursor())
        child_ids = [r[0] for r in conn.execute(
            'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', (booking_id,))]
        kept_ids = [r[0] for r in conn.execute(
            "SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND BookingType = 'entry' "
            f"AND {receivable_sql}", [booking_id] + receivable_params)]
        return [booking_id] + [c for c in child_ids if c not in kept_ids], kept_ids

    def _payments_removed_by_delete(self, conn, booking_id):
        """IDs der Zuordnungen (``InvoicePayments``), die ``delete_transaction(booking_id)`` entfernt – rein lesend, in der
        Transaktion von ``conn``: bei einem markierten Satz einer Zuordnung die ganze Zuordnung (``remove_assignment_for_child``),
        sonst alle Zuordnungen der gelöschten Buchungen (``_delete_scope``)."""
        row = conn.execute('SELECT Status, ParentBooking_ID, DocumentNumber FROM Bookings WHERE ID = ?',
                           (booking_id,)).fetchone()
        if row is None:
            return []
        if row[0] == PAYMENT_MARK and row[1] is not None:
            return self._assignment_ids(conn, row[1], row[2])
        deleted_ids, _kept_ids = self._delete_scope(conn, booking_id)
        marks = ','.join('?' * len(deleted_ids))
        return [r[0] for r in conn.execute(
            f'SELECT ID FROM InvoicePayments WHERE BookingID IN ({marks}) ORDER BY ID', deleted_ids)]

    def split_removal_blocker(self, booking_ids):
        """Klartext, wenn der Bank-Editor die Zeilen dieser Sätze nicht alle entfernen dürfte (``remove_split_row``, nacheinander
        für jeden Satz), weil dabei die Zahlung einer stornierten Rechnung wegfiele, zu der schon erstattet wurde
        (``PaymentRemovalRefused``) – sonst ``None``. Rein lesend: Die Maske fragt es vor dem ersten Schreiben, damit ein
        Speichern ganz oder gar nicht gelingt. Geprüft werden alle Sätze zusammen (zwei Zahlungen, von denen jede allein gehen
        dürfte, zusammen aber nicht; trägt eine ein Skonto, geht die andere Zahlung derselben Rechnung nur mit ihr zusammen), und nur die, die das Entfernen löscht: ein importierter Satz
        oder eine AfA-Buchung wird nur abgehängt (``detachable_split_rows``) und nimmt keine Zuordnung mit."""
        ids = [int(booking_id) for booking_id in booking_ids]
        detached = self.detachable_split_rows(ids)
        conn = self._get_connection()
        try:
            payment_ids = set()
            for booking_id in ids:
                if booking_id not in detached:
                    payment_ids.update(self._payments_removed_by_delete(conn, booking_id))
            return self._refund_removal_blocker(conn, payment_ids)
        finally:
            conn.close()

    def detachable_split_rows(self, booking_ids):
        """Welche dieser Sätze löst der Bank-Editor beim Entfernen ihrer Zeile nur von der Bankbewegung,
        statt sie zu löschen? Returns die IDs als ``set``.

        Es sind Sätze, die nachweislich nicht im Editor entstanden sind: importierte (mit
        Quellklammer ``SourceGroup``), AfA-Buchungen (ein Jahr des AfA-Plans verweist mit
        ``Booking_ID`` auf sie – so erkennt ``db/assets.py`` ein Jahr als gebucht) und
        Forderungsbuchungen (``db.matching.receivable_condition``), die ein Altbestand als Kind
        angehängt hat – sie gehören zur Rechnung, wie beim Löschen der Bankbewegung
        (``delete_transaction``). Ein markierter Zahlungssatz (``PAYMENT_MARK``) nie: Er geht mit
        seiner ganzen Zuordnung."""
        ids = [int(booking_id) for booking_id in booking_ids]
        if not ids:
            return set()
        conn = self._get_connection()
        try:
            return self._detachable_split_rows(conn, ids)
        finally:
            conn.close()

    def payment_row_ids(self, booking_ids):
        """Welche dieser Sätze tragen die Marke einer Rechnungszahlung oder Erstattung (``Status = PAYMENT_MARK``)? Returns die IDs als ``set``. Rein lesend. Der Buchungssatz-Editor zeigt
        ihre Zeilen schreibgeschützt (``server.pages_transactions``); geschrieben wird so ein Satz im Editor nie (``server.handlers._plan_split_rows``, das die Marke aus dem
        gespeicherten Stand liest). Die Tupelform von ``get_child_bookings_for_bank`` bleibt dafür, wie sie ist (positionsbasierte Leser)."""
        ids = [int(booking_id) for booking_id in booking_ids]
        if not ids:
            return set()
        conn = self._get_connection()
        try:
            return {row[0] for row in conn.execute(
                f"SELECT ID FROM Bookings WHERE Status = ? AND ID IN ({','.join('?' * len(ids))})", [PAYMENT_MARK, *ids])}
        finally:
            conn.close()

    def _detachable_split_rows(self, conn, ids):
        """``detachable_split_rows`` auf der Verbindung ``conn`` (in deren Transaktion, wenn sie eine führt); ``ids`` sind
        ganze Zahlen."""
        if not ids:
            return set()
        marks = ','.join('?' * len(ids))
        receivable_sql, receivable_params = self._receivable_sql(conn.cursor(), alias='b')
        rows = conn.execute(
            f"SELECT b.ID FROM Bookings b WHERE b.ID IN ({marks}) AND b.BookingType = 'entry' "
            "AND COALESCE(b.Status, '') != ? "
            "AND (COALESCE(b.SourceGroup, '') != '' "
            "     OR EXISTS (SELECT 1 FROM AssetDepreciations d WHERE d.Booking_ID = b.ID) "
            f"     OR ({receivable_sql}))",
            ids + [PAYMENT_MARK] + list(receivable_params)).fetchall()
        return {row[0] for row in rows}

    def remove_split_row(self, booking_id):
        """Die Zeile eines Satzes im Buchungssatz-Editor der Bankmaske entfernen.

        Ein Satz, der nachweislich nicht im Editor entstanden ist (``detachable_split_rows``: Import,
        AfA-Buchung), wird **abgehängt**: ``ParentBooking_ID`` leer und ein vom Abgleich gesetzter Status
        ``resolved`` zurückgesetzt – wie beim Abhängen einer Forderung (``delete_transaction``) –,
        sonst bleibt jede Spalte, wie sie ist. Er ist danach wieder eine eigenständige Buchung (Liste,
        Zuordnen-Widget, EÜR wie vor dem Zuordnen), das AfA-Jahr bleibt gebucht und der nächste
        Abgleich findet den Satz wieder. Jeder andere Satz wird gelöscht wie bei ``delete_transaction``
        (``_delete_transaction_in``, markierte Zahlungssätze mit ihrer ganzen Zuordnung) – mit dessen Prüfung: Die Zahlung einer
        stornierten Rechnung, zu der schon erstattet wurde, geht nicht (``PaymentRemovalRefused``; der Editor fragt das vorab,
        ``split_removal_blocker``). ``delete_transaction`` selbst – eine Buchung ausdrücklich aus der Liste löschen – bleibt
        unverändert.

        Eine eigene Transaktion (``BEGIN IMMEDIATE``); der Editor schreibt über ``_remove_split_row_in`` in der seinen."""
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            self._remove_split_row_in(tx, booking_id)

    def _remove_split_row_in(self, tx, booking_id):
        """``remove_split_row`` in der laufenden Transaktion ``tx`` (mit ``BEGIN IMMEDIATE`` eröffnet): ein löschbarer Satz geht
        über ``_delete_transaction_in``, ein abzuhängender wird gelöst, und sein Eintrag im Replay-Log folgt nach dem Commit."""
        if booking_id not in self._detachable_split_rows(tx.conn, [int(booking_id)]):
            self._delete_transaction_in(tx.conn, booking_id)
            return
        sql = ("UPDATE Bookings SET ParentBooking_ID = NULL, "
               "Status = CASE WHEN Status = 'resolved' THEN NULL ELSE Status END WHERE ID = ?")
        tx.conn.execute(sql, (booking_id,))
        tx.log.append((sql, (booking_id,), "Split row detach"))

    def _split_rows_dropped(self, conn, bank_id, kept_ids):
        """IDs der Sätze unter der Bankbewegung, deren Zeile der Nutzer im Editor entfernt hat: alle außer ``kept_ids`` und den
        Doppik-Spiegeln (COA und Gegenkonto beide eigene Bank-/Kassenkonten) – die tauchen im Editor nicht auf und bleiben deshalb
        unberührt. Rein lesend, auf ``conn`` (in deren Transaktion, wenn sie eine führt). Die EINE Stelle dieser Regel."""
        bank_coa_ids = self._get_bank_coa_ids(conn.cursor())
        rows = conn.execute('SELECT ID, COA_ID, CounterCOA_ID, EntryForm FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID',
                            (bank_id,)).fetchall()
        return [row[0] for row in rows
                if row[0] not in kept_ids and not is_mirror_row(row[3], row[1], row[2], bank_coa_ids)]

    def dropped_split_rows(self, bank_id, kept_ids):
        """``_split_rows_dropped`` auf einer eigenen Verbindung – für das Planen des Editors (``server.handlers``), das den Stand
        vor dem ersten Schreiben liest und prüft (``split_removal_blocker``)."""
        conn = self._get_connection()
        try:
            return self._split_rows_dropped(conn, bank_id, kept_ids)
        finally:
            conn.close()

    def _write_split_rows_in(self, tx, bank_id, planned):
        """Die geplanten Buchungssätze einer Bankbewegung in der laufenden Transaktion ``tx`` schreiben. ``planned``: die Zeilen
        aus ``server.handlers._plan_split_rows`` – ``[(ID oder None, Felder, zuordnen)]``: eine neue Zeile (ID ``None``) wird
        angelegt, eine vorhandene Buchung mit ``zuordnen`` der Bankbewegung zugeordnet (``ParentBooking_ID``), jede andere nur
        mit den Feldern des Editors geändert (``update_booking_fields``). Ein markierter Zahlungssatz kommt nur mit Datum, Steuerdatum
        und Empfänger (oder ganz ohne Felder: dann schreibt ``update_booking_fields`` nichts); Konto, Gegenkonto und Steuersatz fasst
        der Editor bei ihm nie an – Form und BU-Schlüssel bleiben deshalb, wie sie sind.

        Was der Nutzer entfernt hat (``_split_rows_dropped``), geht über ``_remove_split_row_in``: gelöscht, ein Satz aber, der
        nicht im Editor entstanden ist (Import, AfA-Buchung), nur von der Bankbewegung abgehängt. Alles auf ``tx.conn`` – hier darf
        keine Methode aufgerufen werden, die selbst schreibt oder ``BEGIN`` ausführt (``delete_transaction``,
        ``remove_split_row``, ``unassign_invoice_payment``, ``update_booking_fields`` ohne ``tx``): Sie öffnete eine zweite
        Verbindung und hinge an der Schreibsperre dieser (5 s, dann „database is locked“)."""
        keep_ids = set()
        for rid, fields, adopt in planned:
            if rid is not None:
                if adopt:
                    fields = {**fields, 'parent_booking_id': bank_id}
                self.update_booking_fields(rid, log_description="Split row adopt" if adopt else "Split row update",
                                           tx=tx, **fields)
                keep_ids.add(rid)
            else:
                new_id = self.insert_booking(booking_type='entry', parent_booking_id=bank_id,
                                             log_description="Split row insert", tx=tx, **fields)
                keep_ids.add(new_id)
        for child_id in self._split_rows_dropped(tx.conn, bank_id, keep_ids):
            self._remove_split_row_in(tx, child_id)

    def save_bank_with_rows(self, bank_id, bank_fields=None, plan=None):
        """Eine Bankbewegung und die Buchungssätze ihres Editors in EINER Transaktion speichern: ganz oder gar nicht, und ins
        Replay-Log (``tx.log``) kommt erst etwas, wenn sie festgeschrieben ist – in der Reihenfolge Bankzeile, Zeilen, Abhängen.
        Jeder Fehler, auch eine Ablehnung, nimmt alles zurück; die Ausnahme geht an den Aufrufer.

        ``bank_id``: die Bankbewegung, ``None`` für eine neue. ``bank_fields``: bei einer vorhandenen die Spalten, die geändert
        werden (wie ``update_booking_fields``), bei einer neuen die Argumente von ``insert_booking``; ``None`` fasst die Bankzeile
        nicht an (nur die Sätze). ``plan``: Aufruf ohne Argumente, der die geplanten Zeilen liefert (``_write_split_rows_in``) –
        oder ``None``, wenn es keinen Editor gibt.

        Ablauf: Schreibsperre holen (``BEGIN IMMEDIATE``), dann **planen – vor dem ersten Schreiben**: ``plan`` liest über eigene
        Verbindungen; unter der Sperre ändert kein anderer Schreiber den Stand, und solange diese Transaktion noch nichts
        geschrieben hat, dürfen jene Verbindungen lesen. Eine Ablehnung des Plans kommt so vor dem ersten Schreiben. Dann die
        Bankzeile, dann die Sätze (``_write_split_rows_in``: neue, geänderte, zugeordnete, entfernte).

        Zuletzt prüft ``_assigned_bank_change_blocker``, ob bei einer vorhandenen Bankbewegung Betrag, Bankkonto oder Datum
        geändert wurden, obwohl ihr eine Rechnungszahlung oder Erstattung zugeordnet ist (:class:`BankChangeRefused`): Es zählen die
        Zuordnungen, die nach dem Schreiben noch übrig sind – wer im Editor derselben Anfrage alle Zeilen der Zahlung entfernt, darf
        den Betrag mit ändern. Nichts wird umgerechnet: Die Änderung wird abgelehnt, bis der Nutzer die Zuordnung entfernt hat.

        Returns die ID der Bankbewegung (bei einer neuen die neue)."""
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            stored = None
            if bank_id is not None:
                stored = tx.conn.execute('SELECT Amount, Account_ID, DateBooking FROM Bookings WHERE ID = ?',
                                         (bank_id,)).fetchone()
            planned = plan() if plan else None
            if bank_id is None:
                bank_id = self.insert_booking(**bank_fields, log_description="Manual booking entry", tx=tx)
            elif bank_fields:
                self.update_booking_fields(bank_id, log_description="Manual booking update", tx=tx, **bank_fields)
            if planned is not None:
                self._write_split_rows_in(tx, bank_id, planned)
            if stored is not None and bank_fields:
                blocker = self._assigned_bank_change_blocker(tx.conn, bank_id, stored, bank_fields)
                if blocker:
                    raise BankChangeRefused(blocker)
        return bank_id

    def get_booking_by_id(self, booking_id):
        """Get a single booking by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Bookings WHERE ID=?', (booking_id,))
        booking = cursor.fetchone()
        conn.close()
        return self._euro_row(booking, 11, 14)  # Amount(11), TaxAmount(14)
    def link_booking_to_document(self, booking_id, document_id, relation_type="receipt"):
        """Create a link between a booking and a document

        Ohne Übernahme der Beleg-Nr. – die App verknüpft über ``link_receipt_to_booking``.

        Args:
            booking_id: ID of the booking
            document_id: ID of the document
            relation_type: Type of relation (e.g., 'invoice', 'receipt', 'contract')

        Raises:
            ValueError: Die Buchung oder der Beleg gibt es nicht (Fremdschlüssel-Verstoß) – Klartext für den Nutzer, nichts
                geschrieben. Dass es die Verknüpfung schon gibt (UNIQUE), ist kein Fehler und bleibt still.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO BookingDocuments (Booking_ID, Document_ID, RelationType)
                VALUES (?, ?, ?)
            ''', (booking_id, document_id, relation_type))
            conn.commit()
        except sqlite3.IntegrityError as error:
            conn.rollback()
            detail = str(error)
            if 'FOREIGN KEY' in detail:
                raise ValueError('Buchung oder Beleg gibt es nicht (mehr).') from error
            if 'UNIQUE' not in detail and 'PRIMARY KEY' not in detail:
                raise
            # Link already exists
        finally:
            conn.close()
    def link_receipt_to_booking(self, booking_id, document_id, relation_type=RELATION_DEFAULT):
        """Einen Beleg einer Buchung zuordnen – der Weg der Oberfläche (Maske und Seite des Belegs). Prüfen, Zeile und
        Beleg-Nr. laufen in EINER Transaktion (``BEGIN IMMEDIATE``): ganz oder gar nicht. Gibt es die Buchung oder den Beleg nicht
        (mehr), ist es eine :class:`LinkRefused` mit Klartext, und nichts ist geschrieben. Das Zuordnen ist idempotent: Gibt es die
        Verknüpfung schon, bleibt sie – auch ihre Art – und es entsteht keine zweite Zeile.

        Verknüpfbar ist jede Zeile von ``Bookings``: Bankbewegung, Satz einer Bankbewegung und eigenständiger Satz.
        ``relation_type`` steht in ``RELATION_TYPES``, sonst gilt ``RELATION_DEFAULT``.

        **Die Beleg-Nr.** Die übernommene Nummer ist eine Datenänderung an der Buchung und wirkt wie eine von Hand eingetragene –
        auch auf den automatischen Abgleich (``link_bank_to_entries``: dieselbe Nummer am selben Tag fasst eigenständige Sätze zu EINEM
        Beleg zusammen, Stufe 4 vergleicht die Nummer einer Bankbewegung als Teilzeichenkette). Deshalb übernimmt die Buchung die
        Nummer des Belegs nur, wenn alle Bedingungen zutreffen (``update_booking_fields(tx=…)``, im Replay-Log erst nach dem Commit;
        auch, wenn die Verknüpfung schon bestand):

        1. Die Buchung trägt keine Beleg-Nr. (NULL, leer, nur Leerraum).
        2. Der Beleg trägt eine Nummer (ohne Leerraum am Rand).
        3. Die Verknüpfung ist von der Art ``receipt`` – maßgeblich ist die Art, die nach dem Aufruf in ``BookingDocuments`` steht
           (bestand die Verknüpfung schon, ihre gespeicherte, sonst ``relation_type``); ``invoice``, ``contract`` und ``other`` übernehmen nie.
        4. Die Buchung ist kein Zahlungssatz einer Rechnungszuordnung (``Status='invoice_payment'``: er trägt immer die Rechnungsnummer).
        5. Keine Rechnung trägt die Nummer (``Invoices.InvoiceNumber``): Forderung und Zahlungssätze werden über die Rechnungsnummer
           gefunden, das Zuordnen darf sie weder erzeugen noch stören.
        6. Keine andere Buchung trägt diese oder eine ähnliche Nummer (``_NUMBER_IN_USE_SQL``; ausgenommen die eigene Familie: ihre
           Sätze, ihre Bankbewegung, deren übrige Sätze). Trifft 1 bis 5 zu und 6 nicht, bleibt die Nummer leer: ``number_in_use``.

        Nur die verknüpfte Zeile: Die Kinder einer Bankbewegung bleiben unberührt (wie beim Speichern der Maske, dort führt jedes
        Kind seine eigene Nummer); ``has_entries`` sagt es der Meldung (die Buchung ist eine Bankbewegung mit Sätzen).

        Innerhalb der Transaktion nur Bausteine mit ``tx`` – nie eine öffentliche Methode, die selbst ``BEGIN`` ausführt.

        Returns :class:`LinkResult`."""
        if relation_type not in RELATION_TYPES:
            relation_type = RELATION_DEFAULT
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            # CAST: ein Altbestand kann in diesen Spalten eine Zahl oder einen Blob tragen – Python liest immer Text
            booking = tx.conn.execute('SELECT CAST(DocumentNumber AS TEXT), Status, BookingType, ParentBooking_ID '
                                      'FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()
            if booking is None:
                raise LinkRefused(BOOKING_GONE)
            receipt = tx.conn.execute('SELECT CAST(Number AS TEXT) FROM Documents WHERE ID = ?', (document_id,)).fetchone()
            if receipt is None:
                raise LinkRefused(RECEIPT_GONE)
            stored = tx.conn.execute('SELECT RelationType FROM BookingDocuments WHERE Booking_ID = ? AND Document_ID = ?',
                                     (booking_id, document_id)).fetchone()
            if stored is None:
                tx.conn.execute('INSERT INTO BookingDocuments (Booking_ID, Document_ID, RelationType) VALUES (?, ?, ?)',
                                (booking_id, document_id, relation_type))
            kind = relation_type if stored is None else stored[0]
            number = (receipt[0] or '').strip()
            current = (booking[0] or '').strip()
            has_entries = booking[2] == 'bank' and tx.conn.execute(
                'SELECT 1 FROM Bookings WHERE ParentBooking_ID = ? LIMIT 1', (booking_id,)).fetchone() is not None
            take = (not current and bool(number) and kind == 'receipt' and booking[1] != PAYMENT_MARK
                    and not tx.conn.execute('SELECT 1 FROM Invoices WHERE InvoiceNumber = ?', (number,)).fetchone())
            in_use = take and tx.conn.execute(
                _NUMBER_IN_USE_SQL, {'id': booking_id, 'parent': booking[3], 'n': number}).fetchone() is not None
            take = take and not in_use
            if take:
                self.update_booking_fields(booking_id, log_description='Receipt link: document number', tx=tx,
                                           document_number=number)
            return LinkResult(stored is None, take, number if take else current, in_use, has_entries)
    def unlink_receipt_from_booking(self, booking_id, document_id):
        """Die Verknüpfung einer Buchung mit einem Beleg lösen. Returns ``True``, wenn es sie gab (``False``: es gab sie nicht –
        das Lösen ist idempotent).

        Die Beleg-Nr. der Buchung bleibt stehen: Woher eine Nummer stammt, ist nicht gespeichert – „nur löschen, wenn sie gleich der
        Belegnummer ist“ nähme auch eine von Hand eingetragene, gleich lautende mit, und eine Nummer, die schon in einem
        DATEV-Stapel stand, fehlte danach. Sie bleibt damit auch beim Löschen des Belegs in Gebrauch (``release_receipt_number``
        gibt sie nicht frei)."""
        conn = self._get_connection()
        try:
            cursor = conn.execute('DELETE FROM BookingDocuments WHERE Booking_ID = ? AND Document_ID = ?',
                                  (booking_id, document_id))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()
    def get_documents_for_booking(self, booking_id):
        """Die Belege einer Buchung, jüngster zuerst: Rohzeilen ``(ID, Number, Date, Filename, Path, Info, RelationType)``."""
        conn = self._get_connection()
        try:
            return conn.execute('''
                SELECT d.ID, d.Number, d.Date, d.Filename, d.Path, d.Info, bd.RelationType
                FROM BookingDocuments bd
                JOIN Documents d ON d.ID = bd.Document_ID
                WHERE bd.Booking_ID = ?
                ORDER BY d.Date DESC, d.ID DESC
            ''', (booking_id,)).fetchall()
        finally:
            conn.close()
    def get_bookings_for_document(self, document_id):
        """Die Buchungen eines Belegs, jüngste zuerst: Rohzeilen ``(ID, DateBooking, Amount, Currency, RecipientClient, Text,
        DocumentNumber, BookingType, ParentBooking_ID, RelationType)`` – der Betrag bleibt in Minor Units."""
        conn = self._get_connection()
        try:
            return conn.execute('''
                SELECT b.ID, b.DateBooking, b.Amount, b.Currency, b.RecipientClient, b.Text, b.DocumentNumber, b.BookingType,
                       b.ParentBooking_ID, bd.RelationType
                FROM BookingDocuments bd
                JOIN Bookings b ON b.ID = bd.Booking_ID
                WHERE bd.Document_ID = ?
                ORDER BY b.DateBooking DESC, b.ID DESC
            ''', (document_id,)).fetchall()
        finally:
            conn.close()
    def search_bookings(self, text, date_prefix, amount_minor, limit):
        """Buchungen für die Suche der Seite eines Belegs – rein lesend: Rohzeilen ``(ID, DateBooking, Amount, Currency,
        RecipientClient, Text, DocumentNumber, BookingType, ParentBooking_ID)``, jüngste zuerst, höchstens ``limit`` (begrenzt
        auf 1 bis ``SEARCH_LIMIT_MAX``).

        Gefunden wird, was ``text`` in Empfänger, Verwendungszweck oder Beleg-Nr. enthält – ohne Rücksicht auf die Schreibweise,
        auch bei Umlauten und ß (``ub_fold`` + ``INSTR``: ``LIKE`` faltet in SQLite nur ASCII, und ``%`` und ``_`` sind hier
        gewöhnliche Zeichen) –, dazu die Buchungen, deren Datum mit ``date_prefix`` beginnt (``JJJJ``, ``JJJJ-MM``,
        ``JJJJ-MM-TT``), und die, deren Betrag ohne Vorzeichen genau ``amount_minor`` ist (Minor Units). ``date_prefix`` und
        ``amount_minor`` sind ``None``, wenn die Eingabe keines von beiden ist. Ohne ``text`` gibt es keine Bedingung: die
        jüngsten Buchungen."""
        needle = fold_text(text or '')
        select = ('SELECT ID, DateBooking, Amount, Currency, RecipientClient, Text, DocumentNumber, BookingType, ParentBooking_ID '
                  'FROM Bookings')
        order = ' ORDER BY DateBooking DESC, ID DESC LIMIT :n'
        if needle:
            sql = (select + ' WHERE INSTR(ub_fold(RecipientClient), :t) > 0 OR INSTR(ub_fold(Text), :t) > 0'
                            ' OR INSTR(ub_fold(DocumentNumber), :t) > 0'
                            ' OR (:d IS NOT NULL AND SUBSTR(DateBooking, 1, LENGTH(:d)) = :d)'
                            ' OR (:a IS NOT NULL AND ABS(Amount) = :a)' + order)
            params = {'t': needle, 'd': date_prefix, 'a': amount_minor, 'n': search_limit(limit)}
        else:
            sql, params = select + order, {'n': search_limit(limit)}
        conn = self._get_connection()
        try:
            add_fold_function(conn)
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
