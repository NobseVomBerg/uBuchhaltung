# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: bookings."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from db.matching import is_bank_effective


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

        Die Klammer ist, wenn vorhanden, die aus dem Quellsystem übernommene
        ``SourceGroup`` (WISO: ``ACCOUNTINGID``) – sie ist ausdrücklich und
        muss nicht geraten werden. Sonst gilt weiter die Beleg-Nr. am selben
        Tag, dieselbe Regel wie im Auto-Abgleich (_link_docnr_group).

        Einzelne Buchungen bleiben unverändert 'normal'.
        """
        buckets = {}
        order = []
        for item in normal:
            booking = item['booking']
            source_group = (booking[21] or '').strip() if len(booking) > 21 else ''
            if source_group:
                key = ('src', source_group)
            else:
                docnr = (booking[16] or '').strip()
                key = (docnr, item['date']) if docnr else None
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
            if coa_id in doppik_coa_ids and counter_coa_id in doppik_coa_ids:
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
                if not (c[8] in doppik_coa_ids and c[9] in doppik_coa_ids):
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
                       auto_mirror=False):
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
        
        Returns:
            int: ID of inserted booking
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''INSERT INTO Bookings
            (DateBooking, DateTax, Account_ID, ForeignBankAccount,
             RecipientClient, Contact_ID, COA_ID, CounterCOA_ID, Category_ID, Amount, Currency,
             TaxRate, TaxAmount, Text, DocumentNumber, BookingType, ParentBooking_ID, AutoMirror)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''

        params = (date_booking, date_tax, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, counter_coa_id, category_id,
                  to_minor(amount or 0), currency,
                  tax_rate, self._minor_opt(tax_amount), text, document_number, booking_type,
                  parent_booking_id, 1 if auto_mirror else 0)

        cursor.execute(sql_template, params)
        conn.commit()
        last_id = cursor.lastrowid
        conn.close()
        
        # Optional SQL logging
        if log_description:
            self._log_sql(sql_template, params, log_description)
        
        return last_id
    def backfill_booking_fields(self, booking_id, coa_id=None, tax_rate=None,
                                tax_amount=None, contact_id=None,
                                document_number=None):
        """Leere Felder einer Buchung nachtragen – vorhandene Werte werden
        NIE überschrieben (COALESCE). Für die automatische SKR-Zuordnung beim
        nachträglichen Verknüpfen einer Zahlung mit einer Rechnung (todo #2).
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE Bookings SET
                COA_ID     = COALESCE(COA_ID, ?),
                TaxRate    = COALESCE(TaxRate, ?),
                TaxAmount  = COALESCE(TaxAmount, ?),
                Contact_ID = COALESCE(Contact_ID, ?),
                DocumentNumber = CASE WHEN DocumentNumber IS NULL
                                        OR DocumentNumber = ''
                                      THEN ? ELSE DocumentNumber END
            WHERE ID = ?
        ''', (coa_id, tax_rate, self._minor_opt(tax_amount), contact_id,
              document_number, booking_id))
        conn.commit()
        conn.close()

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
                       Text, DocumentNumber
                FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID
            ''', (group_parent,))
        else:
            cursor.execute('''
                SELECT COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount,
                       Text, DocumentNumber
                FROM Bookings WHERE ID = ?
            ''', (hit_id,))
        raw = cursor.fetchall()
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        conn.close()

        rows = []
        for coa, counter, amount, rate, tax, text, docnr in raw:
            if coa in bank_coa_ids and counter in bank_coa_ids:
                continue                      # reiner Doppik-Spiegel
            # Liquide-zuerst gebuchte Sätze: das Zweckkonto steht hinten
            purpose = coa
            if coa in bank_coa_ids and counter not in bank_coa_ids:
                purpose = counter
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
        sql_template = '''UPDATE Bookings
            SET DateBooking=?, DateTax=?, RecipientClient=?, COA_ID=?,
                CounterCOA_ID=?, Amount=?, Currency=?, TaxRate=?, TaxAmount=?,
                Text=?, DocumentNumber=?
            WHERE ID=?'''
        params = (date_booking, date_tax, recipient_client, coa_id,
                  counter_coa_id, to_minor(amount or 0), currency,
                  tax_rate, self._minor_opt(tax_amount), text, document_number,
                  child_id)
        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()
        if log_description:
            self._log_sql(sql_template, params, log_description)

    def update_booking(self, booking_id, date_booking, amount, account_id=None,
                       foreign_bank_account="", recipient_client="", contact_id=None, 
                       coa_id=None, category_id=None, currency="EUR", tax_rate=None, 
                       tax_amount=None, text="", document_number=None, 
                       date_tax=None, counter_coa_id=None, log_description=None,
                       booking_type=None, parent_booking_id=None):
        """Update an existing booking
        
        Args:
            booking_id: ID of booking to update
            [same parameters as insert_booking]
            booking_type: 'bank', 'entry', or 'split_child' (None = keep current)
            parent_booking_id: FK to parent Bookings row (None = keep current)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''UPDATE Bookings
            SET DateBooking=?, DateTax=?, Account_ID=?, ForeignBankAccount=?,
                RecipientClient=?, Contact_ID=?, COA_ID=?, CounterCOA_ID=?, Category_ID=?, Amount=?, Currency=?,
                TaxRate=?, TaxAmount=?, Text=?, DocumentNumber=?, BookingType=COALESCE(?, BookingType), ParentBooking_ID=COALESCE(?, ParentBooking_ID)
            WHERE ID=?'''
        
        params = (date_booking, date_tax, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, counter_coa_id, category_id,
                  to_minor(amount or 0), currency,
                  tax_rate, self._minor_opt(tax_amount), text, document_number, booking_type, parent_booking_id, booking_id)

        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()

        # Optional SQL logging
        if log_description:
            self._log_sql(sql_template, params, log_description)
    def delete_transaction(self, booking_id: int):
        """Buchung (und verknüpfte Kinder via ParentBooking_ID) löschen.

        Bereinigt vor dem Löschen alle referenzierenden Zeilen:
        BookingDocuments und InvoicePayments werden gelöscht,
        Assets.Booking_ID und AssetDepreciations.Booking_ID werden auf NULL gesetzt.
        Rechnungen gelöschter Zahlungen werden neu berechnet (AmountDue/Status),
        damit keine verwaisten "bezahlt"-Zustände zurückbleiben (todo #2).
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Alle betroffenen IDs: Parent + direkte Kinder
        cursor.execute('SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', (booking_id,))
        child_ids = [row[0] for row in cursor.fetchall()]
        all_ids = [booking_id] + child_ids
        placeholders = ','.join('?' * len(all_ids))

        cursor.execute(f'SELECT DISTINCT InvoiceID FROM InvoicePayments '
                       f'WHERE BookingID IN ({placeholders})', all_ids)
        affected_invoice_ids = [row[0] for row in cursor.fetchall()]

        cursor.execute(f'DELETE FROM BookingDocuments WHERE Booking_ID IN ({placeholders})', all_ids)
        cursor.execute(f'DELETE FROM InvoicePayments WHERE BookingID IN ({placeholders})', all_ids)
        cursor.execute(f'UPDATE Assets SET Booking_ID = NULL WHERE Booking_ID IN ({placeholders})', all_ids)
        cursor.execute(f'UPDATE AssetDepreciations SET Booking_ID = NULL WHERE Booking_ID IN ({placeholders})', all_ids)

        if child_ids:
            child_placeholders = ','.join('?' * len(child_ids))
            cursor.execute(f'DELETE FROM Bookings WHERE ID IN ({child_placeholders})', child_ids)
        cursor.execute('DELETE FROM Bookings WHERE ID = ?', (booking_id,))

        conn.commit()
        conn.close()

        for inv_id in affected_invoice_ids:
            self.recalc_invoice_payment_state(inv_id)
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
        
        Args:
            booking_id: ID of the booking
            document_id: ID of the document
            relation_type: Type of relation (e.g., 'invoice', 'receipt', 'contract')
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO BookingDocuments (Booking_ID, Document_ID, RelationType)
                VALUES (?, ?, ?)
            ''', (booking_id, document_id, relation_type))
            conn.commit()
        except sqlite3.IntegrityError:
            # Link already exists
            conn.rollback()
        finally:
            conn.close()
    def get_documents_for_booking(self, booking_id):
        """Get all documents linked to a booking"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT d.*, bd.RelationType 
            FROM Documents d
            JOIN BookingDocuments bd ON d.ID = bd.Document_ID
            WHERE bd.Booking_ID = ?
        ''', (booking_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows
    def get_bookings_for_document(self, document_id):
        """Get all bookings linked to a document"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT b.*, bd.RelationType 
            FROM Bookings b
            JOIN BookingDocuments bd ON b.ID = bd.Booking_ID
            WHERE bd.Document_ID = ?
        ''', (document_id,))
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 11, 14) for r in rows]  # b.* -> Amount(11), TaxAmount(14)
    def unlink_booking_from_document(self, booking_id, document_id):
        """Remove link between booking and document"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM BookingDocuments 
            WHERE Booking_ID = ? AND Document_ID = ?
        ''', (booking_id, document_id))
        conn.commit()
        conn.close()
