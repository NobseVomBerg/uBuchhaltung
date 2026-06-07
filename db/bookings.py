"""Database-Mixin: bookings."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class BookingsMixin:
    def fetch_bookings(self):
        """Fetch all bookings ordered by date descending"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Bookings ORDER BY DateBooking DESC')
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 11, 14) for r in rows]  # Amount(11), TaxAmount(14)
    def fetch_bookings_grouped(self, date_from=None, date_to=None):
        """Fetch bookings for display, with split groups aggregated.

        Args:
            date_from / date_to: optionaler Zeitraum 'YYYY-MM-DD' (einschließlich).
                Beide gesetzt → nur Buchungen dieses Zeitraums. Splits/Bank-Kinder
                werden über Parent bzw. Gruppe einbezogen, damit kein Split zerreißt.


        Returns a flat list of dicts, each with a 'type' key:

        - 'normal': ungrouped booking  →  {'type': 'normal',  'date': str, 'booking': tuple}
        - 'group':  split group header →  {'type': 'group',   'date': str, 'group_id': int,
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
            "AND ParentBooking_ID IS NULL AND BookingGroup_ID IS NULL "
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

        # 4. Legacy group summaries (BookingGroup_ID, for old imports)
        #    Resolved Debitoren-Entries ausblenden. Im Zeitraum-Modus nur Gruppen
        #    mit mind. einer Buchung im Zeitraum (Gruppe bleibt aber vollständig).
        group_filter = ("AND bg.ID IN (SELECT BookingGroup_ID FROM Bookings "
                        "WHERE BookingGroup_ID IS NOT NULL "
                        "AND DateBooking BETWEEN ? AND ?) ") if use_range else ""
        cursor.execute(f'''
            SELECT
                bg.ID,
                COALESCE(bg.Description, ''),
                MIN(b.DateBooking),
                SUM(b.Amount),
                COUNT(*),
                MAX(b.Account_ID),
                MAX(b.Currency),
                MAX(b.Contact_ID)
            FROM BookingGroups bg
            JOIN Bookings b ON b.BookingGroup_ID = bg.ID
            WHERE b.ParentBooking_ID IS NULL
              AND (b.Status IS NULL OR b.Status != 'resolved')
              {group_filter}
            GROUP BY bg.ID
            ORDER BY MIN(b.DateBooking) DESC
        ''', rng if use_range else ())
        groups_raw = cursor.fetchall()

        # 5. Legacy children (BookingGroup_ID) — only unlinked, not resolved
        lc_filter = ("AND BookingGroup_ID IN (SELECT BookingGroup_ID FROM Bookings "
                     "WHERE BookingGroup_ID IS NOT NULL "
                     "AND DateBooking BETWEEN ? AND ?) ") if use_range else ""
        cursor.execute(f'''
            SELECT * FROM Bookings
            WHERE BookingGroup_ID IS NOT NULL
              AND ParentBooking_ID IS NULL
              AND (Status IS NULL OR Status != 'resolved')
              {lc_filter}
            ORDER BY BookingGroup_ID, DateBooking
        ''', rng if use_range else ())
        children_by_group = {}
        for r in cursor.fetchall():
            gid = r[3]  # BookingGroup_ID
            children_by_group.setdefault(gid, []).append(self._euro_row(r, 11, 14))

        conn.close()

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

        # Build legacy group dicts — skip empty groups (all members linked)
        groups = []
        for g in groups_raw:
            gid, desc, date, total, count, account_id, currency, contact_id = g
            total = from_minor(total or 0)  # SUM(b.Amount) Minor Units -> Euro-Decimal
            group_children = children_by_group.get(gid, [])
            if not group_children:
                continue  # alle Mitglieder sind bereits verknüpft
            children = [
                {'type': 'child', 'group_id': gid, 'date': c[1] or '', 'booking': c}
                for c in group_children
            ]
            # Ersten sichtbaren Child als Info-Quelle nutzen
            first_child = group_children[0] if group_children else None
            groups.append({
                'type':        'group',
                'date':        date or '',
                'group_id':    gid,
                'description': desc,
                'amount':      total,
                'count':       count,
                'account_id':  account_id,
                'currency':    currency or 'EUR',
                'contact_id':  contact_id,
                'children':    children,
                # Merged-Felder vom ersten Kind (für Kasse-Splits etc.)
                'first_recipient':  first_child[6] if first_child else None,
                'first_text':       first_child[15] if first_child else None,
                'first_coa_id':     first_child[8] if first_child else None,
                'first_ccoa_id':    first_child[9] if first_child else None,
            })

        # Merge top-level items sorted by date descending
        top_level = banks + groups + normal
        top_level.sort(key=lambda x: x['date'], reverse=True)

        # Build flat result: parent row immediately followed by its children
        result = []
        for item in top_level:
            result.append(item)
            if item['type'] in ('group', 'bank'):
                result.extend(item.get('children', []))

        return result
    def insert_booking(self, date_booking, amount, account_id=None, foreign_bank_account="", 
                       recipient_client="", contact_id=None, coa_id=None, category_id=None,
                       currency="EUR", tax_rate=None, tax_amount=None, text="", 
                       document_number=None, date_tax=None, booking_group_id=None, 
                       counter_coa_id=None, log_description=None,
                       booking_type='entry', parent_booking_id=None):
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
            booking_group_id: FK to BookingGroups (for split bookings)
            log_description: Description for SQL logging (optional)
            booking_type: 'bank', 'entry', or 'split_child' (default: 'entry')
            parent_booking_id: FK to parent Bookings row (bank transaction)
        
        Returns:
            int: ID of inserted booking
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''INSERT INTO Bookings 
            (DateBooking, DateTax, BookingGroup_ID, Account_ID, ForeignBankAccount, 
             RecipientClient, Contact_ID, COA_ID, CounterCOA_ID, Category_ID, Amount, Currency, 
             TaxRate, TaxAmount, Text, DocumentNumber, BookingType, ParentBooking_ID)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
        
        params = (date_booking, date_tax, booking_group_id, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, counter_coa_id, category_id,
                  to_minor(amount or 0), currency,
                  tax_rate, self._minor_opt(tax_amount), text, document_number, booking_type, parent_booking_id)

        cursor.execute(sql_template, params)
        conn.commit()
        last_id = cursor.lastrowid
        conn.close()
        
        # Optional SQL logging
        if log_description:
            self._log_sql(sql_template, params, log_description)
        
        return last_id
    def check_booking_exists(self, date, amount, account_id=None, foreign_bank_account="", text=""):
        """Check if a booking with same parameters already exists"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM Bookings
            WHERE DateBooking=? AND Amount=? AND Account_ID=? AND ForeignBankAccount=? AND Text=?
        ''', (date, to_minor(amount or 0), account_id, foreign_bank_account, text))
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    def update_booking(self, booking_id, date_booking, amount, account_id=None, 
                       foreign_bank_account="", recipient_client="", contact_id=None, 
                       coa_id=None, category_id=None, currency="EUR", tax_rate=None, 
                       tax_amount=None, text="", document_number=None, 
                       date_tax=None, booking_group_id=None, counter_coa_id=None, log_description=None,
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
            SET DateBooking=?, DateTax=?, BookingGroup_ID=?, Account_ID=?, ForeignBankAccount=?,
                RecipientClient=?, Contact_ID=?, COA_ID=?, CounterCOA_ID=?, Category_ID=?, Amount=?, Currency=?,
                TaxRate=?, TaxAmount=?, Text=?, DocumentNumber=?, BookingType=COALESCE(?, BookingType), ParentBooking_ID=COALESCE(?, ParentBooking_ID)
            WHERE ID=?'''
        
        params = (date_booking, date_tax, booking_group_id, account_id, foreign_bank_account,
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
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Alle betroffenen IDs: Parent + direkte Kinder
        cursor.execute('SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', (booking_id,))
        child_ids = [row[0] for row in cursor.fetchall()]
        all_ids = [booking_id] + child_ids
        placeholders = ','.join('?' * len(all_ids))

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
    def get_booking_by_id(self, booking_id):
        """Get a single booking by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Bookings WHERE ID=?', (booking_id,))
        booking = cursor.fetchone()
        conn.close()
        return self._euro_row(booking, 11, 14)  # Amount(11), TaxAmount(14)
    def create_booking_group(self, description="", total_amount=None):
        """Create a new booking group for split bookings
        
        Args:
            description: Description of the booking group
            total_amount: Expected total amount for validation
            
        Returns:
            int: ID of created booking group
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        from datetime import date
        created_date = date.today().isoformat()
        
        cursor.execute('''
            INSERT INTO BookingGroups (Description, CreatedDate, TotalAmount)
            VALUES (?, ?, ?)
        ''', (description, created_date, self._minor_opt(total_amount)))
        conn.commit()
        group_id = cursor.lastrowid
        conn.close()
        return group_id
    def fetch_booking_groups(self):
        """Fetch all booking groups"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM BookingGroups ORDER BY CreatedDate DESC')
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 3) for r in rows]  # TotalAmount (Index 3) -> Euro-Decimal
    def get_bookings_in_group(self, group_id):
        """Get all bookings belonging to a specific group"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Bookings WHERE BookingGroup_ID=? ORDER BY DateBooking, ID', (group_id,))
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 11, 14) for r in rows]  # Amount(11), TaxAmount(14)
    def update_booking_group(self, group_id, description, total_amount=None):
        """Beschreibung und Erwartungsbetrag einer Gruppe aktualisieren."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE BookingGroups SET Description=?, TotalAmount=? WHERE ID=?',
            (description, self._minor_opt(total_amount), group_id)
        )
        conn.commit()
        conn.close()
    def delete_booking_group(self, group_id):
        """Gruppe löschen. Zugehörige Buchungen werden aus der Gruppe gelöst (nicht gelöscht)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE Bookings SET BookingGroup_ID=NULL WHERE BookingGroup_ID=?', (group_id,))
        cursor.execute('DELETE FROM BookingGroups WHERE ID=?', (group_id,))
        conn.commit()
        conn.close()
    def unlink_booking_from_group(self, booking_id):
        """Buchung aus ihrer Gruppe lösen (BookingGroup_ID auf NULL setzen)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE Bookings SET BookingGroup_ID=NULL WHERE ID=?', (booking_id,))
        conn.commit()
        conn.close()
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
