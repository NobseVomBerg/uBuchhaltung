import sqlite3
import os
import json

class Database:
    def __init__(self, db_name="./data/buch.db"):
        self.db_name = db_name
        # Ensure the directory exists
        db_dir = os.path.dirname(self.db_name)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.initialize_database()
    
    def _get_connection(self):
        """Get a database connection with foreign keys enabled"""
        conn = sqlite3.connect(self.db_name)
        conn.execute('PRAGMA foreign_keys = ON')
        return conn
    
    def _log_sql(self, sql_template, params, description):
        """Helper function to log SQL statements
        
        Args:
            sql_template: SQL template string (e.g., 'INSERT INTO Zahlung (...) VALUES (?, ?, ...)')
            params: Tuple of parameters
            description: Description for the log entry
        """
        try:
            from document_parser import DocumentParser
            parser = DocumentParser()
            # Build readable SQL string by replacing ? with actual values
            sql_statement = sql_template
            for param in params:
                if param is None:
                    replacement = 'NULL'
                elif isinstance(param, str):
                    escaped_param = param.replace('"', '""')
                    replacement = f"'{escaped_param}'"
                elif isinstance(param, (int, float)):
                    replacement = str(param)
                else:
                    replacement = str(param)
                sql_statement = sql_statement.replace('?', replacement, 1)
            parser.log_sql(sql_statement, params, description)
        except ImportError:
            pass  # Parser not available, skip logging

    def initialize_database(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        # Chart of Accounts = Standard Konto Rahmen, 03/04 in Germany or 07 for Austria
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ChartOfAccounts (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Framework INTEGER,
                AccountNumber INTEGER,
                Name TEXT,
                Description TEXT,
                IsStandard INTEGER DEFAULT 0,
                PrivateSharePercent INTEGER DEFAULT 0,
                UNIQUE(Framework, AccountNumber)
            )
        ''')

        # Categories for private Things (Immobilien, Krankenbelege, Versicherungen, ...)
        # Text: zusätzliche Infos, z.B. für welchen Teil der Steuererklärung
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Categories (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT,
                Text TEXT,
                Parent_ID INTEGER
            )
        ''')

        # Accounts table for bank accounts and cash accounts
        # Number: IBAN or CreditCardNr
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Accounts (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT NOT NULL,
                Owner TEXT,
                Number TEXT,
                BIC TEXT,
                BankName TEXT,
                IsCash INTEGER DEFAULT 0,
                SKRAccount INTEGER,
                UNIQUE(Name)
            )
        ''')

        # ── Contacts: normalized 4-table structure (Option C) ─────────────────────
        # Base table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Contacts (
                ID           INTEGER PRIMARY KEY AUTOINCREMENT,
                ContactType  TEXT NOT NULL DEFAULT 'customer',
                EntityType   TEXT NOT NULL DEFAULT 'company',
                DisplayName  TEXT,
                CustomerNumber TEXT,
                Email        TEXT,
                Phone        TEXT,
                Notes        TEXT,
                Logo         TEXT,
                UNIQUE(CustomerNumber)
            )
        ''')

        # Addresses (1:n – AddressType='main' is the primary address)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ContactAddresses (
                ID           INTEGER PRIMARY KEY AUTOINCREMENT,
                ContactID    INTEGER NOT NULL,
                AddressType  TEXT NOT NULL DEFAULT 'main',
                AddressLine1 TEXT,
                Street       TEXT,
                PostalCode   TEXT,
                City         TEXT,
                Country      TEXT DEFAULT 'DE',
                UNIQUE(ContactID, AddressType),
                FOREIGN KEY (ContactID) REFERENCES Contacts(ID) ON DELETE CASCADE
            )
        ''')

        # Person-specific fields (1:1)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS PersonDetails (
                ContactID        INTEGER PRIMARY KEY,
                Salutation       TEXT,
                Title            TEXT,
                FirstName        TEXT,
                LastName         TEXT,
                DateOfBirth      TEXT,
                CompanyContactID INTEGER,
                CompanyName_Free TEXT,
                FOREIGN KEY (ContactID) REFERENCES Contacts(ID) ON DELETE CASCADE,
                FOREIGN KEY (CompanyContactID) REFERENCES Contacts(ID)
            )
        ''')

        # Company-specific fields (1:1)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS CompanyDetails (
                ContactID    INTEGER PRIMARY KEY,
                CompanyName  TEXT,
                LegalForm    TEXT,
                TaxID        TEXT,
                BuyerRouteID TEXT,
                FOREIGN KEY (ContactID) REFERENCES Contacts(ID) ON DELETE CASCADE
            )
        ''')

        # Documents (Belege)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Documents (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Number TEXT,
                Date DATE,
                Filename TEXT,
                Path TEXT,
                Info TEXT
            )
        ''')

        # BookingGroups (Helper for linking Documents and Bookings with m:n together)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS BookingGroups (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Description TEXT,
                CreatedDate DATE,
                TotalAmount REAL
            )
        ''')

        # Bookings (replaces Zahlung with enhanced structure)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Bookings (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                DateBooking DATE NOT NULL,
                DateTax DATE,
                BookingGroup_ID INTEGER,
                Account_ID INTEGER,
                ForeignBankAccount TEXT,
                RecipientClient TEXT,
                Contact_ID INTEGER,
                COA_ID INTEGER,
                CounterCOA_ID INTEGER,
                Category_ID INTEGER,
                Amount REAL NOT NULL,
                Currency TEXT DEFAULT 'EUR',
                TaxRate REAL,
                TaxAmount REAL,
                Text TEXT,
                DocumentNumber TEXT,
                BookingType TEXT DEFAULT 'entry',
                ParentBooking_ID INTEGER,
                Status TEXT,
                FOREIGN KEY (BookingGroup_ID) REFERENCES BookingGroups(ID),
                FOREIGN KEY (Account_ID) REFERENCES Accounts(ID),
                FOREIGN KEY (Contact_ID) REFERENCES Contacts(ID),
                FOREIGN KEY (COA_ID) REFERENCES ChartOfAccounts(ID),
                FOREIGN KEY (CounterCOA_ID) REFERENCES ChartOfAccounts(ID),
                FOREIGN KEY (Category_ID) REFERENCES Categories(ID),
                FOREIGN KEY (ParentBooking_ID) REFERENCES Bookings(ID)
            )
        ''')

        # BookingDocuments (Junction table for many-to-many relationship)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS BookingDocuments (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Booking_ID INTEGER NOT NULL,
                Document_ID INTEGER NOT NULL,
                RelationType TEXT,
                FOREIGN KEY (Booking_ID) REFERENCES Bookings(ID),
                FOREIGN KEY (Document_ID) REFERENCES Documents(ID),
                UNIQUE(Booking_ID, Document_ID)
            )
        ''')

        # Articles table for product/service catalog
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Articles (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT NOT NULL,
                Unit TEXT DEFAULT 'Stk.',
                UnitPrice REAL DEFAULT 0,
                TaxRate REAL DEFAULT 19,
                Description TEXT,
                Active INTEGER DEFAULT 1
            )
        ''')

        # NumberRanges table for invoice/receipt numbering
        # Type: 'invoice' (Ausgangsrechnungen), 'receipt_company' (Belege Firma), 'receipt_category' (Belege Kategorien)
        # Format: e.g., 'R' for Rechnungen, 'B' for Belege, etc.
        # Suffix: optional suffix for subdivision (e.g., '_A', '_B') – appended after the number
        # NumberFormat: format template, e.g. '{yy}{l}{nnn}{s}' → '26F001' or '26F001_A'
        # CurrentNumber: the last used number in this range
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS NumberRanges (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Type TEXT NOT NULL,
                Year INTEGER NOT NULL,
                Letter TEXT NOT NULL,
                Prefix TEXT DEFAULT '',
                CurrentNumber INTEGER DEFAULT 0,
                Description TEXT,
                NumberFormat TEXT DEFAULT '{yy}{l}{nnn}{s}',
                UNIQUE(Type, Year, Letter, Prefix)
            )
        ''')

        # Invoices table for storing issued invoices (XRechnung-compliant)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Invoices (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InvoiceNumber TEXT UNIQUE NOT NULL,
                InvoiceDate DATE NOT NULL,
                
                -- Seller (own company) - Snapshot
                OwnCompanyId INTEGER,
                SellerName TEXT NOT NULL,
                SellerCompany TEXT NOT NULL,
                SellerStreet TEXT,
                SellerPostalCode TEXT,
                SellerCity TEXT,
                SellerCountry TEXT DEFAULT 'DE',
                SellerVATID TEXT,
                SellerEmail TEXT,
                SellerPhone TEXT,
                
                -- Buyer (customer) - Snapshot
                CustomerId INTEGER,
                BuyerName TEXT NOT NULL,
                BuyerCompany TEXT NOT NULL,
                BuyerStreet TEXT,
                BuyerPostalCode TEXT,
                BuyerCity TEXT,
                BuyerCountry TEXT DEFAULT 'DE',
                BuyerVATID TEXT,
                BuyerReference TEXT,
                BuyerRouteID TEXT,
                
                -- Order reference
                OrderNumber TEXT,
                
                -- XRechnung specific
                Currency TEXT DEFAULT 'EUR',
                DeliveryDate DATE,
                
                -- Payment terms
                PaymentTerms TEXT,
                PaymentDueDate DATE,
                SkontoDays INTEGER,
                SkontoPercent REAL,
                
                -- Bank details - Snapshot
                BankAccountId INTEGER,
                BankName TEXT,
                BankIBAN TEXT,
                BankBIC TEXT,
                
                -- Totals
                TaxCategory TEXT DEFAULT 'S',
                TaxRate REAL NOT NULL,
                SumNet REAL NOT NULL,
                TaxAmount REAL NOT NULL,
                SumGross REAL NOT NULL,
                AmountDue REAL NOT NULL,
                
                -- Management
                Status TEXT DEFAULT 'draft',
                PDFPath TEXT,
                XMLPath TEXT,
                CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                UpdatedAt DATETIME,
                
                FOREIGN KEY (OwnCompanyId) REFERENCES Contacts(ID),
                FOREIGN KEY (CustomerId) REFERENCES Contacts(ID),
                FOREIGN KEY (BankAccountId) REFERENCES Accounts(ID)
            )
        ''')

        # InvoiceItems table for invoice line items
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS InvoiceItems (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InvoiceId INTEGER NOT NULL,
                Position INTEGER NOT NULL,
                ArticleId INTEGER,
                Description TEXT NOT NULL,
                Quantity REAL NOT NULL,
                Unit TEXT DEFAULT 'C62',
                PricePerUnit REAL NOT NULL,
                TotalNet REAL NOT NULL,
                TaxCategory TEXT DEFAULT 'S',
                TaxRate REAL NOT NULL,
                FOREIGN KEY (InvoiceId) REFERENCES Invoices(ID) ON DELETE CASCADE,
                FOREIGN KEY (ArticleId) REFERENCES Articles(ID)
            )
        ''')

        conn.commit()
        conn.close()
        
        # Run migrations
        self._run_migrations()
        
        # Ensure the default "Kasse" account exists
        self.ensure_kasse_exists()

    def _run_migrations(self):
        """Run database migrations for new tables and columns"""
        conn = self._get_connection()
        cursor = conn.cursor()

        # AssetCategories table (AfA-Tabellen nach BMF)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS AssetCategories (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT NOT NULL,
                UsefulLifeYears INTEGER NOT NULL,
                DepreciationMethod TEXT DEFAULT 'linear',
                COA_ID INTEGER,
                Notes TEXT,
                FOREIGN KEY (COA_ID) REFERENCES ChartOfAccounts(ID)
            )
        ''')

        # Assets table (Anlagenverzeichnis)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Assets (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InventoryNumber TEXT UNIQUE,
                Name TEXT NOT NULL,
                Description TEXT,
                AssetCategory_ID INTEGER,
                COA_ID INTEGER,
                PurchaseDate DATE NOT NULL,
                PurchasePrice REAL NOT NULL,
                UsefulLifeYears INTEGER NOT NULL,
                DepreciationMethod TEXT DEFAULT 'linear',
                SerialNumber TEXT,
                Location TEXT,
                Supplier_ID INTEGER,
                Document_ID INTEGER,
                Booking_ID INTEGER,
                SaleDate DATE,
                SalePrice REAL,
                Status TEXT DEFAULT 'active',
                Notes TEXT,
                Parent_ID INTEGER,
                CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (AssetCategory_ID) REFERENCES AssetCategories(ID),
                FOREIGN KEY (COA_ID) REFERENCES ChartOfAccounts(ID),
                FOREIGN KEY (Supplier_ID) REFERENCES Contacts(ID),
                FOREIGN KEY (Document_ID) REFERENCES Documents(ID),
                FOREIGN KEY (Booking_ID) REFERENCES Bookings(ID),
                FOREIGN KEY (Parent_ID) REFERENCES Assets(ID)
            )
        ''')

        # AssetDepreciations table (AfA-Plan)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS AssetDepreciations (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Asset_ID INTEGER NOT NULL,
                Year INTEGER NOT NULL,
                DepreciationAmount REAL NOT NULL,
                BookValue REAL NOT NULL,
                Booking_ID INTEGER,
                Status TEXT DEFAULT 'planned',
                BookedAt DATETIME,
                FOREIGN KEY (Asset_ID) REFERENCES Assets(ID) ON DELETE CASCADE,
                FOREIGN KEY (Booking_ID) REFERENCES Bookings(ID),
                UNIQUE(Asset_ID, Year)
            )
        ''')

        # InvoicePayments: Zahlungsverknüpfungen Rechnung ↔ Buchung
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS InvoicePayments (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                InvoiceID INTEGER NOT NULL,
                BookingID INTEGER NOT NULL,
                Amount REAL NOT NULL,
                PaymentDate DATE NOT NULL,
                Notes TEXT,
                CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (InvoiceID) REFERENCES Invoices(ID) ON DELETE CASCADE,
                FOREIGN KEY (BookingID) REFERENCES Bookings(ID)
            )
        ''')

        # DATEV Steuerschlüssel (BU-Schlüssel) → Steuersatz-Zuordnung
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS TaxKeys (
                Code TEXT PRIMARY KEY,
                Description TEXT NOT NULL,
                TaxRate REAL,
                TaxType TEXT
            )
        ''')

        conn.commit()
        conn.close()

        # Migration: Add NumberFormat column to NumberRanges if not exists
        conn2 = self._get_connection()
        cursor2 = conn2.cursor()
        try:
            cursor2.execute("ALTER TABLE NumberRanges ADD COLUMN NumberFormat TEXT DEFAULT '{yy}{l}{nnn}{s}'")
            conn2.commit()
        except Exception:
            pass  # Column already exists
        finally:
            conn2.close()

        # Seed-Daten aus seed_data/ laden
        self._seed_chart_of_accounts()
        self._seed_asset_categories()
        self._seed_tax_keys()
    
    def _load_seed_json(self, filename):
        """Lädt eine JSON-Datei aus dem seed_data/-Verzeichnis."""
        seed_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seed_data')
        filepath = os.path.join(seed_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _seed_tax_keys(self):
        """Seed der DATEV-Steuerschlüssel (BU-Schlüssel) aus seed_data/tax_keys.json.

        Quelle: https://help-center.apps.datev.de/documents/1008613
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM TaxKeys')
        if cursor.fetchone()[0] > 0:
            conn.close()
            return

        data = self._load_seed_json('tax_keys.json')
        rows = [(e['code'], e['description'], e['tax_rate'], e['tax_type']) for e in data]
        cursor.executemany(
            'INSERT OR IGNORE INTO TaxKeys (Code, Description, TaxRate, TaxType) VALUES (?,?,?,?)',
            rows)
        conn.commit()
        conn.close()

    def get_tax_rate_for_bu(self, bu_code: str):
        """Steuersatz für einen BU-Schlüssel aus DB holen.

        Returns:
            float | None: Steuersatz als Dezimalzahl (0.19) oder None.
        """
        if not bu_code:
            return None
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT TaxRate FROM TaxKeys WHERE Code=?', (bu_code,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def _get_bank_coa_ids(self, cursor):
        """Ermittelt COA-IDs die echten Bankkonten entsprechen.

        Basiert auf der Accounts-Tabelle (SKRAccount-Nr.), nicht auf
        einem hart codierten Bereich.  Dadurch wird z.B. 1460
        (Verrechnungskonto) korrekt NICHT als Bankkonto eingestuft.

        Returns:
            set[int]: COA-IDs die Bankkonten sind.
        """
        cursor.execute('''
            SELECT c.ID
            FROM ChartOfAccounts c
            JOIN Accounts a ON c.AccountNumber = a.SKRAccount
            WHERE a.SKRAccount IS NOT NULL
        ''')
        return {row[0] for row in cursor.fetchall()}

    # Table Documents (Receipts)
    def fetch_receipts(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Documents')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_receipt(self, number, date, filename, path, info):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO Documents (Number, Date, Filename, Path, Info)
                VALUES (?, ?, ?, ?, ?)
            ''', (number, date, filename, path, info))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting receipt:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_receipt(self, receipt_id, number, date, filename, path, info):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Documents
                SET Number = ?, Date = ?, Filename = ?, Path = ?, Info = ?
                WHERE ID = ?
            ''', (number, date, filename, path, info, receipt_id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating receipt:", e)
            conn.rollback()
        finally:
            conn.close()

    def get_receipt_by_number(self, number):
        """Get a single receipt by number"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Documents WHERE Number = ?', (number,))
        row = cursor.fetchone()
        conn.close()
        return row

    def delete_receipt(self, number):
        """Delete a receipt by number"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM Documents WHERE Number = ?', (number,))
        conn.commit()
        conn.close()

    # Table Bookings
    def fetch_bookings(self):
        """Fetch all bookings ordered by date descending"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Bookings ORDER BY DateBooking DESC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def fetch_bookings_grouped(self):
        """Fetch bookings for display, with split groups aggregated.

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

        # 1. Bank transactions (top-level parents via ParentBooking_ID)
        cursor.execute('''
            SELECT * FROM Bookings
            WHERE BookingType = 'bank'
            ORDER BY DateBooking DESC
        ''')
        bank_rows = cursor.fetchall()

        # 2. Child bookings linked to bank transactions via ParentBooking_ID
        cursor.execute('''
            SELECT * FROM Bookings
            WHERE ParentBooking_ID IS NOT NULL
            ORDER BY ParentBooking_ID, DateBooking
        ''')
        children_by_parent = {}
        for r in cursor.fetchall():
            pid = r[18]  # ParentBooking_ID
            children_by_parent.setdefault(pid, []).append(r)

        # 3. Normal (ungrouped) bookings — not bank, not child, not in legacy group
        #    Rein liquide Spiegelbuchungen und resolved Debitoren ausblenden.
        cursor.execute('''
            SELECT * FROM Bookings
            WHERE (BookingType IS NULL OR BookingType = 'entry')
              AND ParentBooking_ID IS NULL
              AND BookingGroup_ID IS NULL
              AND (Status IS NULL OR Status != 'resolved')
            ORDER BY DateBooking DESC
        ''')
        normal = []
        for r in cursor.fetchall():
            coa_id = r[8]          # COA_ID
            counter_coa_id = r[9]  # CounterCOA_ID
            if coa_id in doppik_coa_ids and counter_coa_id in doppik_coa_ids:
                continue  # Doppik-Eintrag verbergen
            normal.append({'type': 'normal', 'date': r[1] or '', 'booking': r})

        # 4. Legacy group summaries (BookingGroup_ID, for old imports)
        #    Resolved Debitoren-Entries ausblenden.
        cursor.execute('''
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
            GROUP BY bg.ID
            ORDER BY MIN(b.DateBooking) DESC
        ''')
        groups_raw = cursor.fetchall()

        # 5. Legacy children (BookingGroup_ID) — only unlinked, not resolved
        cursor.execute('''
            SELECT * FROM Bookings
            WHERE BookingGroup_ID IS NOT NULL
              AND ParentBooking_ID IS NULL
              AND (Status IS NULL OR Status != 'resolved')
            ORDER BY BookingGroup_ID, DateBooking
        ''')
        children_by_group = {}
        for r in cursor.fetchall():
            gid = r[3]  # BookingGroup_ID
            children_by_group.setdefault(gid, []).append(r)

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
            })

        # Build legacy group dicts — skip empty groups (all members linked)
        groups = []
        for g in groups_raw:
            gid, desc, date, total, count, account_id, currency, contact_id = g
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
                  recipient_client, contact_id, coa_id, counter_coa_id, category_id, amount, currency,
                  tax_rate, tax_amount, text, document_number, booking_type, parent_booking_id)
        
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
        ''', (date, amount, account_id, foreign_bank_account, text))
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0

    def get_linked_entry_for_bank(self, bank_booking_id: int):
        """Hole die wichtigsten Felder des ersten verknüpften Entry-Bookings.

        Für Bank-Buchungen, die über ParentBooking_ID mit Entry-Buchungen
        verknüpft sind.  Doppik-Einträge (COA = Bankkonto) werden übersprungen.

        Returns:
            tuple(COA_ID, CounterCOA_ID, TaxRate, TaxAmount, DocumentNumber,
                  Contact_ID, Category_ID) oder None.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        cursor.execute('''
            SELECT COA_ID, CounterCOA_ID, TaxRate, TaxAmount,
                   DocumentNumber, Contact_ID, Category_ID
            FROM Bookings
            WHERE ParentBooking_ID = ?
            ORDER BY ID
        ''', (bank_booking_id,))
        for row in cursor.fetchall():
            coa_id = row[0]
            counter_coa_id = row[1]
            if not (coa_id in bank_coa_ids and counter_coa_id in bank_coa_ids):
                conn.close()
                return row
        conn.close()
        return None

    def find_unlinked_booking_by_date_amount(self, date: str, amount: float):
        """Suche nach einer WISO-Buchung/-Gruppe (Account_ID IS NULL) anhand Datum + Betrag.

        Stufe 1 – Einzelbuchung: exakter Treffer auf DateBooking + Amount.
        Stufe 2 – Split-Gruppe:  SUM(Amount) der Gruppe entspricht dem Bankbetrag,
                                  alle Mitglieder sind noch unverknüpft (Account_ID IS NULL).

        Sonderfall Mehrreferenz (z.B. DocumentNumber = '25F009, 25F073'):
        Die Buchungen teilen sich eine kombinierte Referenz und landen dadurch
        bereits in einer BookingGroup → wird automatisch über Stufe 2 abgedeckt.

        Returns:
            ('single', booking_id)  – eindeutige Einzelbuchung
            ('group',  group_id)    – eindeutige Split-Gruppe
            None                    – kein eindeutiger Treffer
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Stufe 1: einzelne, noch nicht verknüpfte Buchung (kein Split)
        cursor.execute('''
            SELECT ID FROM Bookings
            WHERE DateBooking = ? AND Amount = ?
              AND Account_ID IS NULL AND BookingGroup_ID IS NULL
        ''', (date, amount))
        rows = cursor.fetchall()
        if len(rows) == 1:
            conn.close()
            return ('single', rows[0][0])

        # Stufe 2: Split-Gruppe, bei der der Gesamtbetrag passt
        # Bedingung: ALLE Mitglieder der Gruppe sind noch unverknüpft
        #            UND mindestens eine Buchung liegt auf dem gesuchten Datum
        #            (erlaubt leichte Datumsabweichungen innerhalb der Gruppe)
        cursor.execute('''
            SELECT b.BookingGroup_ID,
                   ROUND(SUM(b.Amount), 2)                              AS total,
                   COUNT(*)                                             AS cnt,
                   SUM(CASE WHEN b.Account_ID IS NULL THEN 1 ELSE 0 END) AS unlinked
            FROM Bookings b
            WHERE b.BookingGroup_ID IS NOT NULL
              AND b.DateBooking = ?
            GROUP BY b.BookingGroup_ID
            HAVING cnt = unlinked
               AND total = ROUND(?, 2)
        ''', (date, amount))
        rows = cursor.fetchall()
        conn.close()
        if len(rows) == 1:
            return ('group', rows[0][0])

        return None   # 0 oder mehrere Treffer → nicht verlässlich verknüpfbar

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
                RecipientClient=?, contact_id=?, COA_ID=?, CounterCOA_ID=?, Category_ID=?, Amount=?, Currency=?,
                TaxRate=?, TaxAmount=?, Text=?, DocumentNumber=?, BookingType=COALESCE(?, BookingType), ParentBooking_ID=COALESCE(?, ParentBooking_ID)
            WHERE ID=?'''
        
        params = (date_booking, date_tax, booking_group_id, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, counter_coa_id, category_id, amount, currency,
                  tax_rate, tax_amount, text, document_number, booking_type, parent_booking_id, booking_id)
        
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
        return booking

    # Table BookingGroups
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
        ''', (description, created_date, total_amount))
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
        return rows
    
    def get_bookings_in_group(self, group_id):
        """Get all bookings belonging to a specific group"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Bookings WHERE BookingGroup_ID=? ORDER BY DateBooking, ID', (group_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def update_booking_group(self, group_id, description, total_amount=None):
        """Beschreibung und Erwartungsbetrag einer Gruppe aktualisieren."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE BookingGroups SET Description=?, TotalAmount=? WHERE ID=?',
            (description, total_amount, group_id)
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

    # Table BookingDocuments
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
        return rows
    
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

    # Table ChartOfAccounts
    def fetch_chart_of_accounts(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM ChartOfAccounts')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_chart_of_accounts(self, framework, account_number, name, description, is_standard=0, private_share_percent=0):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard, PrivateSharePercent)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (framework, account_number, name, description, is_standard, private_share_percent or 0))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting into ChartOfAccounts:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_chart_of_accounts(self, id, framework, account_number, name, description, private_share_percent=None):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            # PrivateSharePercent ist für ALLE Konten editierbar (auch Standard)
            if private_share_percent is not None:
                cursor.execute(
                    'UPDATE ChartOfAccounts SET PrivateSharePercent = ? WHERE ID = ?',
                    (private_share_percent, id))
            # Andere Felder nur für Nicht-Standard-Konten
            cursor.execute('''
                UPDATE ChartOfAccounts
                SET Framework = ?, AccountNumber = ?, Name = ?, Description = ?
                WHERE ID = ? AND IsStandard = 0
            ''', (framework, account_number, name, description, id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating ChartOfAccounts:", e)
            conn.rollback()
        finally:
            conn.close()

    # Table Accounts
    def ensure_kasse_exists(self):
        """Ensure the default 'Kasse' account exists and cannot be deleted"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM Accounts WHERE IsCash = 1')
        count = cursor.fetchone()[0]
        if count == 0:
            cursor.execute('''
                INSERT INTO Accounts (Name, Owner, Number, BIC, BankName, IsCash)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ("Kasse", "", "", "", "", 1))
            conn.commit()
        conn.close()

    def _seed_asset_categories(self):
        """Seed der BMF-AfA-Kategorien aus seed_data/asset_categories.json."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM AssetCategories')
        if cursor.fetchone()[0] > 0:
            conn.close()
            return

        data = self._load_seed_json('asset_categories.json')
        rows = [(e['name'], e['useful_life_years'], e['depreciation_method'],
                 e['coa_id'], e['notes']) for e in data]
        cursor.executemany('''
            INSERT INTO AssetCategories (Name, UsefulLifeYears, DepreciationMethod, COA_ID, Notes)
            VALUES (?, ?, ?, ?, ?)
        ''', rows)
        conn.commit()
        conn.close()

    def _seed_chart_of_accounts(self):
        """Seed der Standard-SKR-Konten aus seed_data/chart_of_accounts_skr04.json.

        Falls seed_data/private/chart_of_accounts_custom.json existiert,
        werden zusätzlich benutzerspezifische Konten geladen.
        Ein optionaler ``overrides``-Block in der Custom-Datei erlaubt es,
        Felder vorhandener Standard-Konten nachträglich zu ändern
        (z. B. ``PrivateSharePercent``).
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM ChartOfAccounts')
        if cursor.fetchone()[0] > 0:
            conn.close()
            return

        # Standard-Kontenrahmen
        data = self._load_seed_json('chart_of_accounts_skr04.json')
        framework = data['framework']
        rows = [(framework, e['account_number'], e['name'], e['description'],
                 1 if e['is_standard'] else 0,
                 e.get('private_share_percent', 0))
                for e in data['accounts']]

        # Private Ergänzungen (optional)
        seed_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seed_data', 'private')
        private_file = os.path.join(seed_dir, 'chart_of_accounts_custom.json')
        overrides = []
        if os.path.exists(private_file):
            with open(private_file, 'r', encoding='utf-8') as f:
                pdata = json.load(f)
            pfw = pdata.get('framework', framework)
            rows += [(pfw, e['account_number'], e['name'], e['description'],
                      1 if e.get('is_standard') else 0,
                      e.get('private_share_percent', 0))
                     for e in pdata.get('accounts', [])]
            overrides = pdata.get('overrides', [])

        cursor.executemany('''
            INSERT OR IGNORE INTO ChartOfAccounts
                (Framework, AccountNumber, Name, Description, IsStandard, PrivateSharePercent)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', rows)

        # Overrides anwenden (z. B. Privatanteil für Standard-Konten setzen)
        for ov in overrides:
            acct_nr = ov.get('account_number')
            if acct_nr is None:
                continue
            psp = ov.get('private_share_percent')
            if psp is not None:
                cursor.execute('''
                    UPDATE ChartOfAccounts SET PrivateSharePercent = ?
                    WHERE AccountNumber = ? AND Framework = ?
                ''', (psp, acct_nr, ov.get('framework', framework)))

        conn.commit()
        conn.close()

    # ─── First-run / Test data ────────────────────────────────────────────────

    def is_first_run(self) -> bool:
        """Prüft ob die App noch nicht eingerichtet wurde.

        Gilt als erster Start wenn:
        - kein Kontakt mit ContactType='own' existiert, UND
        - kein echtes Bankkonto (IsCash=0) angelegt ist.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM Contacts WHERE ContactType='own'")
        has_own = cursor.fetchone()[0] > 0
        cursor.execute("SELECT COUNT(*) FROM Accounts WHERE IsCash=0")
        has_bank = cursor.fetchone()[0] > 0
        conn.close()
        return not has_own and not has_bank

    def load_test_seed_data(self):
        """Lädt Testdaten aus seed_data/test/ (Kontakte, Bankkonten, Belege, Buchungen).

        Idempotent: bereits vorhandene Einträge werden übersprungen.
        Dient sowohl dem Setup-Wizard ('Mit Testdaten starten') als auch
        dem pytest-Fixture db_with_coa.
        """
        seed_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seed_data', 'test')

        # ── Kontakte ──────────────────────────────────────────────────────────
        contacts_file = os.path.join(seed_dir, 'test_contacts.json')
        if os.path.exists(contacts_file):
            with open(contacts_file, 'r', encoding='utf-8') as f:
                contacts = json.load(f)
            conn = self._get_connection()
            cursor = conn.cursor()
            for c in contacts:
                # Eigene-Daten-Kontakt nur einmalig anlegen
                if c.get('contact_type') == 'own':
                    cursor.execute("SELECT COUNT(*) FROM Contacts WHERE ContactType='own'")
                    if cursor.fetchone()[0] > 0:
                        continue
                conn.close()
                try:
                    self.insert_contact(
                        contact_type=c.get('contact_type', 'customer'),
                        entity_type=c.get('entity_type', 'company'),
                        display_name=c.get('display_name'),
                        customer_number=c.get('customer_number'),
                        email=c.get('email', ''),
                        phone=c.get('phone', ''),
                        notes=c.get('notes', ''),
                        street=c.get('street', ''),
                        postal_code=c.get('postal_code', ''),
                        city=c.get('city', ''),
                        country=c.get('country', 'DE'),
                        company_name=c.get('company_name', ''),
                        legal_form=c.get('legal_form', ''),
                        tax_id=c.get('tax_id', ''),
                        salutation=c.get('salutation', ''),
                        first_name=c.get('first_name', ''),
                        last_name=c.get('last_name', ''),
                    )
                except Exception:
                    pass  # Duplikat oder Constraint-Fehler – überspringen
                conn = self._get_connection()
                cursor = conn.cursor()
            conn.close()

        # ── Bankkonten ────────────────────────────────────────────────────────
        accounts_file = os.path.join(seed_dir, 'test_accounts.json')
        if os.path.exists(accounts_file):
            with open(accounts_file, 'r', encoding='utf-8') as f:
                accounts = json.load(f)
            for a in accounts:
                self.insert_account(
                    name=a['name'],
                    holder=a.get('holder', ''),
                    number=a.get('number', ''),
                    bic=a.get('bic', ''),
                    bank_name=a.get('bank_name', ''),
                    is_cash=a.get('is_cash', 0),
                    skr_account=a.get('skr_account'),
                )

        # ── Dokumente (Belege) ────────────────────────────────────────────────
        documents_file = os.path.join(seed_dir, 'test_documents.json')
        if os.path.exists(documents_file):
            with open(documents_file, 'r', encoding='utf-8') as f:
                documents = json.load(f)
            for d in documents:
                conn = self._get_connection()
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM Documents WHERE Number=?', (d['number'],))
                exists = cursor.fetchone()[0] > 0
                conn.close()
                if not exists:
                    self.insert_receipt(
                        number=d['number'],
                        date=d['date'],
                        filename=d.get('filename', ''),
                        path=d.get('path', ''),
                        info=d.get('info', ''),
                    )

        # ── Buchungen ─────────────────────────────────────────────────────────
        bookings_file = os.path.join(seed_dir, 'test_bookings.json')
        if os.path.exists(bookings_file):
            with open(bookings_file, 'r', encoding='utf-8') as f:
                bookings_data = json.load(f)

            # Lookup-Maps einmalig aufbauen
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT Name, ID FROM Accounts')
            acct_map = {row[0]: row[1] for row in cursor.fetchall()}
            cursor.execute('SELECT AccountNumber, ID FROM ChartOfAccounts')
            coa_map = {row[0]: row[1] for row in cursor.fetchall()}
            conn.close()

            for b in bookings_data:
                doc_nr = b.get('document_number')
                # Duplikat-Prüfung über DocumentNumber + BookingType='bank'
                if doc_nr:
                    conn = self._get_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT COUNT(*) FROM Bookings WHERE DocumentNumber=? AND BookingType='bank'",
                        (doc_nr,),
                    )
                    if cursor.fetchone()[0] > 0:
                        conn.close()
                        continue
                    conn.close()

                account_id = acct_map.get(b.get('account_name'))
                parent_id = self.insert_booking(
                    date_booking=b['date'],
                    amount=b['amount'],
                    account_id=account_id,
                    booking_type=b.get('booking_type', 'bank'),
                    text=b.get('text', ''),
                    document_number=doc_nr,
                )

                for child in b.get('children', []):
                    coa_id = coa_map.get(child.get('coa_number'))
                    counter_coa_id = coa_map.get(child.get('counter_coa_number'))
                    tax_rate = child.get('tax_rate')
                    child_amount = child['amount']
                    tax_amount = None
                    if tax_rate and child_amount:
                        tax_amount = round(
                            abs(child_amount) - abs(child_amount) / (1 + tax_rate), 2
                        )
                        if child_amount < 0:
                            tax_amount = -tax_amount
                    self.insert_booking(
                        date_booking=child['date'],
                        amount=child_amount,
                        coa_id=coa_id,
                        counter_coa_id=counter_coa_id,
                        tax_rate=tax_rate,
                        tax_amount=tax_amount,
                        booking_type='entry',
                        text=child.get('text', ''),
                        document_number=child.get('document_number'),
                        parent_booking_id=parent_id,
                    )


    # ─── Asset Categories ────────────────────────────────────────────────────

    def fetch_asset_categories(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM AssetCategories ORDER BY Name ASC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_asset_category_by_id(self, category_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM AssetCategories WHERE ID = ?', (category_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def insert_asset_category(self, name, useful_life_years, depreciation_method='linear', coa_id=None, notes=''):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO AssetCategories (Name, UsefulLifeYears, DepreciationMethod, COA_ID, Notes)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, useful_life_years, depreciation_method, coa_id, notes))
        conn.commit()
        conn.close()

    def update_asset_category(self, category_id, name, useful_life_years, depreciation_method, coa_id, notes):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE AssetCategories SET Name=?, UsefulLifeYears=?, DepreciationMethod=?, COA_ID=?, Notes=?
            WHERE ID=?
        ''', (name, useful_life_years, depreciation_method, coa_id, notes, category_id))
        conn.commit()
        conn.close()

    def delete_asset_category(self, category_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM AssetCategories WHERE ID = ?', (category_id,))
        conn.commit()
        conn.close()

    # ─── Assets ─────────────────────────────────────────────────────────────

    def _generate_inventory_number(self, purchase_date):
        """Generate inventory number: INV-YY-###"""
        conn = self._get_connection()
        cursor = conn.cursor()
        year_short = str(purchase_date)[:4][-2:]  # e.g. '25' from '2025-01-01'
        pattern = f"INV-{year_short}-%"
        cursor.execute("SELECT COUNT(*) FROM Assets WHERE InventoryNumber LIKE ?", (pattern,))
        count = cursor.fetchone()[0]
        conn.close()
        return f"INV-{year_short}-{count + 1:03d}"

    def fetch_assets(self, status=None, parent_only=True):
        """Fetch assets with optional status filter. By default only top-level assets."""
        conn = self._get_connection()
        cursor = conn.cursor()
        conditions = []
        params = []
        if status:
            conditions.append('a.Status = ?')
            params.append(status)
        if parent_only:
            conditions.append('a.Parent_ID IS NULL')
        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        cursor.execute(f'''
            SELECT a.*, ac.Name as CategoryName, c.DisplayName as SupplierName
            FROM Assets a
            LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
            LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
            {where}
            ORDER BY a.PurchaseDate DESC
        ''', params)
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_asset_by_id(self, asset_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT a.*, ac.Name as CategoryName, c.DisplayName as SupplierName
            FROM Assets a
            LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
            LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
            WHERE a.ID = ?
        ''', (asset_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def get_asset_children(self, parent_id):
        """Fetch sub-assets (extensions) of a parent asset"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT a.*, ac.Name as CategoryName, c.Name as SupplierName
            FROM Assets a
            LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
            LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
            WHERE a.Parent_ID = ?
            ORDER BY a.PurchaseDate ASC
        ''', (parent_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_asset(self, name, purchase_date, purchase_price, useful_life_years,
                     description='', asset_category_id=None, coa_id=None,
                     depreciation_method='linear', serial_number='', location='',
                     supplier_id=None, document_id=None, booking_id=None,
                     notes='', parent_id=None):
        inv_number = self._generate_inventory_number(purchase_date)
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO Assets (InventoryNumber, Name, Description, AssetCategory_ID, COA_ID,
                PurchaseDate, PurchasePrice, UsefulLifeYears, DepreciationMethod,
                SerialNumber, Location, Supplier_ID, Document_ID, Booking_ID,
                Notes, Parent_ID, Status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        ''', (inv_number, name, description, asset_category_id, coa_id,
              purchase_date, purchase_price, useful_life_years, depreciation_method,
              serial_number, location, supplier_id, document_id, booking_id,
              notes, parent_id))
        asset_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return asset_id

    def update_asset(self, asset_id, name, purchase_date, purchase_price, useful_life_years,
                     description='', asset_category_id=None, coa_id=None,
                     depreciation_method='linear', serial_number='', location='',
                     supplier_id=None, document_id=None, booking_id=None,
                     notes='', status='active'):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE Assets SET
                Name=?, Description=?, AssetCategory_ID=?, COA_ID=?,
                PurchaseDate=?, PurchasePrice=?, UsefulLifeYears=?, DepreciationMethod=?,
                SerialNumber=?, Location=?, Supplier_ID=?, Document_ID=?, Booking_ID=?,
                Notes=?, Status=?
            WHERE ID=?
        ''', (name, description, asset_category_id, coa_id,
              purchase_date, purchase_price, useful_life_years, depreciation_method,
              serial_number, location, supplier_id, document_id, booking_id,
              notes, status, asset_id))
        conn.commit()
        conn.close()

    def sell_asset(self, asset_id, sale_date, sale_price):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE Assets SET SaleDate=?, SalePrice=?, Status='sold' WHERE ID=?
        ''', (sale_date, sale_price, asset_id))
        conn.commit()
        conn.close()

    def delete_asset(self, asset_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM Assets WHERE ID = ?', (asset_id,))
        conn.commit()
        conn.close()

    # ─── Asset Depreciations ─────────────────────────────────────────────────

    def get_depreciations_for_asset(self, asset_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM AssetDepreciations WHERE Asset_ID = ? ORDER BY Year ASC
        ''', (asset_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def calculate_depreciation_plan(self, purchase_price, purchase_date, useful_life_years,
                                     depreciation_method='linear'):
        """Calculate full AfA plan. Returns list of dicts per year.
        purchase_date: 'YYYY-MM-DD'
        Handles partial first/last year (months-based).
        Degressive: 25% fixed, switches to linear when linear is higher.
        GWG: if purchase_price <= 800, full write-off in purchase year.
        """
        import datetime as dt
        plan = []

        if not purchase_price or not purchase_date or not useful_life_years:
            return plan

        try:
            pd = dt.date.fromisoformat(str(purchase_date)[:10])
        except Exception:
            return plan

        purchase_year = pd.year
        # Months remaining in purchase year (including purchase month)
        months_in_first_year = 13 - pd.month  # e.g. March → 10 months

        # GWG: Sofortabschreibung up to 800 €
        if purchase_price <= 800.0:
            plan.append({
                'year': purchase_year,
                'book_value_start': purchase_price,
                'depreciation': round(purchase_price, 2),
                'book_value_end': 0.0,
                'method': 'GWG',
            })
            return plan

        if depreciation_method == 'linear':
            annual = purchase_price / useful_life_years
            remaining = purchase_price
            first_depr = round(annual * months_in_first_year / 12, 2)
            plan.append({
                'year': purchase_year,
                'book_value_start': round(remaining, 2),
                'depreciation': first_depr,
                'book_value_end': round(remaining - first_depr, 2),
                'method': 'linear',
            })
            remaining -= first_depr
            year = purchase_year + 1
            while remaining > 0.005:
                depr = round(min(annual, remaining), 2)
                plan.append({
                    'year': year,
                    'book_value_start': round(remaining, 2),
                    'depreciation': depr,
                    'book_value_end': round(remaining - depr, 2),
                    'method': 'linear',
                })
                remaining = round(remaining - depr, 2)
                year += 1

        else:  # degressive
            deg_rate = 0.25  # 25% fixed (§ 7 Abs. 2 EStG 2025)
            linear_annual = purchase_price / useful_life_years
            remaining = purchase_price
            year = purchase_year
            first = True
            while remaining > 0.005:
                deg_depr = remaining * deg_rate
                lin_depr = remaining / max(1, useful_life_years - (year - purchase_year))
                # Switch to linear when linear is higher
                if lin_depr >= deg_depr:
                    method = 'linear'
                    annual_depr = lin_depr
                else:
                    method = 'degressiv'
                    annual_depr = deg_depr
                # Partial first year
                if first:
                    annual_depr = annual_depr * months_in_first_year / 12
                    first = False
                annual_depr = round(min(annual_depr, remaining), 2)
                plan.append({
                    'year': year,
                    'book_value_start': round(remaining, 2),
                    'depreciation': annual_depr,
                    'book_value_end': round(remaining - annual_depr, 2),
                    'method': method,
                })
                remaining = round(remaining - annual_depr, 2)
                year += 1

        return plan

    def get_book_value_at_date(self, asset_id, at_date=None):
        """Calculate current book value of an asset at a given date."""
        import datetime as dt
        asset = self.get_asset_by_id(asset_id)
        if not asset:
            return 0.0
        purchase_price = asset[7]   # PurchasePrice
        purchase_date = asset[6]    # PurchaseDate
        useful_life = asset[8]      # UsefulLifeYears
        method = asset[9]           # DepreciationMethod
        if at_date is None:
            at_date = dt.date.today()
        plan = self.calculate_depreciation_plan(purchase_price, purchase_date, useful_life, method)
        current_year = at_date.year
        book_value = purchase_price
        for entry in plan:
            if entry['year'] <= current_year:
                book_value = entry['book_value_end']
            else:
                break
        return max(0.0, book_value)

    def book_depreciation(self, asset_id, year, account_id, coa_id_expense,
                          coa_id_asset, description=None):
        """Book an AfA entry: creates a Booking and marks depreciation as posted."""
        import datetime as dt
        asset = self.get_asset_by_id(asset_id)
        if not asset:
            raise ValueError(f"Asset {asset_id} not found")
        plan = self.calculate_depreciation_plan(
            asset[7], asset[6], asset[8], asset[9])
        year_entry = next((e for e in plan if e['year'] == year), None)
        if not year_entry:
            raise ValueError(f"No depreciation planned for year {year}")
        amount = year_entry['depreciation']
        if not description:
            description = f"AfA {asset[2]} {year} ({asset[1]})"
        booking_date = f"{year}-12-31"
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO Bookings (DateBooking, DateTax, Account_ID, COA_ID,
                Amount, Currency, Text, BookingType, Status)
            VALUES (?, ?, ?, ?, ?, 'EUR', ?, 'entry', 'posted')
        ''', (booking_date, booking_date, account_id, coa_id_expense,
              -abs(amount), description))
        booking_id = cursor.lastrowid
        # Upsert into AssetDepreciations
        cursor.execute('''
            INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Booking_ID, Status, BookedAt)
            VALUES (?, ?, ?, ?, ?, 'posted', CURRENT_TIMESTAMP)
            ON CONFLICT(Asset_ID, Year) DO UPDATE SET
                Booking_ID=excluded.Booking_ID, Status='posted', BookedAt=CURRENT_TIMESTAMP,
                DepreciationAmount=excluded.DepreciationAmount, BookValue=excluded.BookValue
        ''', (asset_id, year, amount, year_entry['book_value_end'], booking_id))
        conn.commit()
        conn.close()
        return booking_id

    def fetch_accounts(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Accounts ORDER BY IsCash DESC, Name ASC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_account_by_id(self, account_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Accounts WHERE ID = ?', (account_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def insert_account(self, name, holder, number, bic, bank_name, is_cash=0, skr_account=None):
        # Kassenkonten bekommen standardmäßig SKR 1460 (Verrechnungskonto)
        if is_cash and not skr_account:
            skr_account = 1460
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                INSERT INTO Accounts (Name, Owner, Number, BIC, BankName, IsCash, SKRAccount)
                VALUES (?, ?, ?, ?, ?, ?, ?)'''
        params = (name, holder, number, bic, bank_name, is_cash, skr_account)
        try:
            cursor.execute(sql_template, params)
            conn.commit()
            # Log SQL after successful commit
            self._log_sql(sql_template, params, "Insert new account")
        except sqlite3.IntegrityError as e:
            print("Error inserting account:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_account(self, account_id, name, holder, number, bic, bank_name, skr_account=None):
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                UPDATE Accounts
                SET Name = ?, Owner = ?, Number = ?, BIC = ?, BankName = ?, SKRAccount = ?
                WHERE ID = ?'''
        params = (name, holder, number, bic, bank_name, skr_account, account_id)
        try:
            cursor.execute(sql_template, params)
            conn.commit()
            # Log SQL after successful commit
            self._log_sql(sql_template, params, "Update account")
        except sqlite3.IntegrityError as e:
            print("Error updating account:", e)
            conn.rollback()
        finally:
            conn.close()

    def delete_account(self, account_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        # Only allow deletion if it's not the Kasse account
        cursor.execute('DELETE FROM Accounts WHERE ID = ? AND IsCash = 0', (account_id,))
        conn.commit()
        conn.close()

    def get_table_statistics(self):
        """Get statistics about all tables in the database
        
        Returns:
            list: List of tuples (table_name, row_count)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Get all table names
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = cursor.fetchall()
        
        statistics = []
        for table in tables:
            table_name = table[0]
            # Skip sqlite internal tables
            if table_name.startswith('sqlite_'):
                continue
            
            # Get row count for each table
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            count = cursor.fetchone()[0]
            statistics.append((table_name, count))
        
        conn.close()
        return statistics

    def export_to_sql(self, filepath: str) -> tuple[int, int]:
        """Export all table data as INSERT statements to a .sql file.
        Skips sqlite internal tables and tables with 0 rows.
        Returns (tables_exported, rows_exported)."""
        import os
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall() if not row[0].startswith('sqlite_')]

        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        tables_exported = 0
        rows_exported = 0

        with open(filepath, 'w', encoding='utf-8') as f:
            import datetime
            f.write(f"-- DB-Export {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("-- Direkt verwendbar im SQL-Konsolenbereich (INSERT-Statements)\n\n")

            for table in tables:
                cursor.execute(f"SELECT * FROM [{table}]")
                rows = cursor.fetchall()
                if not rows:
                    continue

                col_names = [d[0] for d in cursor.description]
                cols_sql = ', '.join(f'[{c}]' for c in col_names)

                f.write(f"-- {table}\n")
                f.write(f"INSERT INTO {table} ({cols_sql}) VALUES\n")
                
                for idx, row in enumerate(rows):
                    vals = []
                    for v in row:
                        if v is None:
                            vals.append('NULL')
                        elif isinstance(v, (int, float)):
                            vals.append(str(v))
                        else:
                            escaped = str(v).replace("'", "''")
                            vals.append(f"'{escaped}'")
                    vals_sql = ', '.join(vals)
                    
                    # Letzte Zeile bekommt Semikolon, alle anderen ein Komma
                    if idx == len(rows) - 1:
                        f.write(f"({vals_sql});\n")
                    else:
                        f.write(f"({vals_sql}),\n")
                    rows_exported += 1
                f.write("\n")
                tables_exported += 1

        conn.close()
        return tables_exported, rows_exported

    # ── DATEV-Methoden ────────────────────────────────────────────────────────

    def fetch_bookings_range(self, date_from: str, date_to: str):
        """Buchungen eines Datumsbereichs für den DATEV-Export laden.

        Args:
            date_from: 'YYYY-MM-DD' – einschließlich
            date_to:   'YYYY-MM-DD' – einschließlich

        Returns:
            List of tuples (SELECT * FROM Bookings ORDER BY DateBooking)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM Bookings WHERE DateBooking >= ? AND DateBooking <= ? '
            "AND BookingType != 'bank' "
            'ORDER BY DateBooking ASC',
            (date_from, date_to),
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_coa_id_to_number_map(self) -> dict:
        """Liefert {coa_id: account_number} für alle ChartOfAccounts-Einträge."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT ID, AccountNumber FROM ChartOfAccounts')
        result = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()
        return result

    def get_coa_id_by_account_number(self, account_number, framework: int = None):
        """COA-ID anhand der Kontonummer nachschlagen.

        Args:
            account_number: SKR-Kontonummer (int oder str)
            framework:      Kontenrahmen-Nr. (z.B. 3, 4, 7) – optional

        Returns:
            int COA-ID oder None
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        if framework is not None:
            cursor.execute(
                'SELECT ID FROM ChartOfAccounts WHERE AccountNumber=? AND Framework=? LIMIT 1',
                (int(account_number), int(framework)),
            )
        else:
            cursor.execute(
                'SELECT ID FROM ChartOfAccounts WHERE AccountNumber=? LIMIT 1',
                (int(account_number),),
            )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def update_bookings_date_tax_batch(self, booking_ids: list, date_tax: str):
        """DateTax für mehrere Buchungen auf einmal setzen (nach DATEV-Export).

        Args:
            booking_ids: Liste von Booking-IDs
            date_tax:    'YYYY-MM-DD'
        """
        if not booking_ids:
            return
        conn = self._get_connection()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(booking_ids))
        cursor.execute(
            f'UPDATE Bookings SET DateTax=? WHERE ID IN ({placeholders})',
            [date_tax] + list(booking_ids),
        )
        conn.commit()
        conn.close()

    def get_account_id_by_skr(self, skr_number: int):
        """Account-ID anhand der SKRAccount-Nummer nachschlagen.

        Args:
            skr_number: SKR-Kontonummer (z.B. 1810, 1460)

        Returns:
            int Account-ID oder None
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM Accounts WHERE SKRAccount=? LIMIT 1', (int(skr_number),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def import_wiso_csv(self, csv_bytes: bytes) -> dict:
        """WISO Mein Büro CSV-Export in die Bookings-Tabelle importieren.
        
        Unterstützt zwei Formate:
        
        1. Original-Export (9 Spalten):
           ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER
           
        2. Tabellen-Export (6 Spalten):
           Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr.;Betrag
           
        Format wird automatisch erkannt. Tabellen-Export aktualisiert bestehende Buchungen.

        Returns:
            dict: {imported: int, updated: int, skipped: int, errors: list[str], format: str}
        """
        import csv, io, datetime

        # Encoding-Erkennung: CP1252 zuerst, dann Fallback
        text = None
        for enc in ('cp1252', 'utf-8-sig', 'utf-8', 'latin-1'):
            try:
                text = csv_bytes.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            return {'imported': 0, 'updated': 0, 'skipped': 0,
                    'errors': ['Encoding der Datei nicht erkennbar'], 'format': 'unknown'}
        
        # Format-Erkennung über Header-Zeile
        # Anführungszeichen und Whitespace aus den Spaltenbezeichnungen entfernen
        first_line = text.split('\n')[0].strip()
        headers = [h.strip().strip('"').strip("'") for h in first_line.split(';')]
        
        # Tabellen-Format erkennen (Empf./Auft. + Verwendungszweck vorhanden)
        if any('Empf' in h or 'Auft' in h for h in headers) and any('Verwendungszweck' in h for h in headers):
            return self._import_wiso_table_format(text)
        # Original-Format erkennen (KONTO + GEGENKONTO vorhanden)
        elif 'KONTO' in headers and 'GEGENKONTO' in headers:
            return self._import_wiso_original_format(text)
        else:
            return {'imported': 0, 'updated': 0, 'skipped': 0,
                    'errors': [f'Unbekanntes Format. Gefundene Spalten: {", ".join(headers)}'], 
                    'format': 'unknown'}
    
    def _import_wiso_original_format(self, text: str) -> dict:
        """Import des Original WISO-Exports (9 Spalten).
        
        CSV-Spalten:
            ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER

        Mapping:
            KONTO      → ChartOfAccounts.AccountNumber → COA_ID
            GEGENKONTO → ChartOfAccounts.AccountNumber → CounterCOA_ID
            SCHLUESSEL → BU-Schlüssel → TaxRate (401=19%, 402=7%, 121=0%)

        Returns:
            dict: {imported: int, updated: int, skipped: int, errors: list[str]}
        """
        import csv, io, datetime

        # BU-Schlüssel → Steuersatz aus DB-Tabelle laden
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT Code, TaxRate FROM TaxKeys')
        BU_TO_TAXRATE = {row[0]: row[1] for row in cursor.fetchall()}

        # Lookup-Maps einmalig aufbauen
        cursor.execute('SELECT AccountNumber, ID FROM ChartOfAccounts')
        coa_map = {row[0]: row[1] for row in cursor.fetchall()}

        # Bankkonten-SKR-Nummern für Vorzeichen-Logik
        cursor.execute('SELECT SKRAccount FROM Accounts WHERE SKRAccount IS NOT NULL')
        liquid_account_nrs = {row[0] for row in cursor.fetchall()}
        conn.close()

        reader = csv.DictReader(io.StringIO(text), delimiter=';', quotechar='"')
        # Spaltennamen normalisieren (führende/nachgestellte Leerzeichen entfernen)
        if reader.fieldnames:
            reader.fieldnames = [f.strip() for f in reader.fieldnames]
        imported = 0
        updated = 0
        skipped = 0
        skipped_rows = []       # Liste übersprungener Zeilen mit Details
        missing_coa = set()     # SKR-Kontonummern (KONTO), die nicht in ChartOfAccounts gefunden wurden
        missing_counter_coa = set()  # SKR-Kontonummern (GEGENKONTO), die nicht in ChartOfAccounts gefunden wurden
        errors = []

        def _is_liquid(nr):
            """Prüft ob die SKR-Nummer ein liquides Konto ist (Bank/Kasse/Verrechnungskonto)."""
            return nr is not None and (
                nr in liquid_account_nrs  # aus Accounts-Tabelle (z.B. 1810)
                or 1000 <= nr <= 1099     # Kasse (SKR04)
                or nr == 1460             # Verrechnungskonto
            )

        # ── Pass 1: alle Zeilen parsen ───────────────────────────────────────
        parsed_rows = []  # list of dicts
        for i, row in enumerate(reader, 1):
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            try:
                date_str = row.get('DATUM', '').strip()[:10]
                try:
                    booking_date = datetime.datetime.strptime(date_str, '%d.%m.%Y').strftime('%Y-%m-%d')
                except ValueError:
                    errors.append(f"Zeile {i}: Ungültiges Datum '{date_str}'")
                    continue

                amount_str = row.get('BRUTTOBETRAG', '').strip()
                if ',' in amount_str:
                    amount_str = amount_str.replace('.', '').replace(',', '.')
                try:
                    amount = float(amount_str)
                except ValueError:
                    errors.append(f"Zeile {i}: Ungültiger Betrag '{amount_str}'")
                    continue

                konto_str = row.get('KONTO', '').strip()
                try:
                    konto_nr = int(konto_str) if konto_str else None
                    coa_id = coa_map.get(konto_nr) if konto_nr is not None else None
                    if konto_nr is not None and coa_id is None:
                        missing_coa.add(konto_nr)
                except (ValueError, TypeError):
                    coa_id = None
                    konto_nr = None

                gegenkonto_str = row.get('GEGENKONTO', '').strip()
                try:
                    gegenkonto_nr = int(gegenkonto_str) if gegenkonto_str else None
                    counter_coa_id = coa_map.get(gegenkonto_nr) if gegenkonto_nr is not None else None
                    if gegenkonto_nr is not None and counter_coa_id is None:
                        missing_counter_coa.add(gegenkonto_nr)
                except (ValueError, TypeError):
                    counter_coa_id = None
                    gegenkonto_nr = None

                # Vorzeichen: GEGENKONTO = liquides Konto → Abgang (negativ)
                #             KONTO      = liquides Konto → Zugang (positiv)
                if _is_liquid(gegenkonto_nr) and not _is_liquid(konto_nr):
                    amount = -abs(amount)
                elif _is_liquid(konto_nr) and not _is_liquid(gegenkonto_nr):
                    amount = abs(amount)

                schluessel = row.get('SCHLUESSEL', '').strip()
                tax_rate = BU_TO_TAXRATE.get(schluessel)
                # 4405→4400 Umbuchung: implizit 19% USt, auch ohne BU-Schlüssel
                if tax_rate is None and konto_nr == 4405 and gegenkonto_nr == 4400:
                    tax_rate = 0.19
                # Steuerbetrag berechnen (Brutto → MwSt-Anteil)
                tax_amount = None
                if tax_rate is not None and tax_rate > 0 and amount != 0:
                    tax_amount = round(abs(amount) - abs(amount) / (1 + tax_rate), 2)
                    if amount < 0:
                        tax_amount = -tax_amount
                text_val = row.get('TEXT', '').strip()
                doc_number = row.get('REFERENZNUMMER', '').strip()

                parsed_rows.append({
                    'zeile': i, 'date': booking_date, 'amount': amount,
                    'coa_id': coa_id, 'counter_coa_id': counter_coa_id,
                    'konto_nr': konto_nr, 'konto_str': konto_str,
                    'tax_rate': tax_rate, 'tax_amount': tax_amount,
                    'text': text_val, 'doc': doc_number,
                })
            except Exception as e:
                errors.append(f"Zeile {i}: {str(e)}")

        # ── Pass 2: Split-Gruppen erkennen (gleiche REFERENZNUMMER + Datum) ──
        # Mehrere Zeilen mit gleicher Referenz = eine Kontoauszugs-Buchung
        # wurde buchhalterisch auf mehrere Konten aufgeteilt.
        from collections import defaultdict
        ref_groups = defaultdict(list)
        for pr in parsed_rows:
            key = (pr['doc'], pr['date']) if pr['doc'] else None
            if key:
                ref_groups[key].append(pr)

        # booking_group_id_for_key: key → int (wird bei Bedarf angelegt)
        group_id_cache = {}

        dup_conn = self._get_connection()
        dup_cur  = dup_conn.cursor()

        def _get_or_create_group(key, total_amount, date):
            if key in group_id_cache:
                return group_id_cache[key]
            dup_cur.execute(
                'INSERT INTO BookingGroups (Description, CreatedDate, TotalAmount) VALUES (?,?,?)',
                (key[0], date, total_amount)  # key = (doc_number, date)
            )
            gid = dup_cur.lastrowid
            group_id_cache[key] = gid
            return gid

        for pr in parsed_rows:
            try:
                doc_number   = pr['doc']
                booking_date = pr['date']
                amount       = pr['amount']
                coa_id       = pr['coa_id']

                # Duplikat-Prüfung
                if doc_number:
                    if coa_id is not None:
                        dup_cur.execute(
                            'SELECT COUNT(*) FROM Bookings WHERE DocumentNumber=? AND DateBooking=? AND COA_ID=? AND Amount=?',
                            (doc_number, booking_date, coa_id, amount)
                        )
                    else:
                        dup_cur.execute(
                            'SELECT COUNT(*) FROM Bookings WHERE DocumentNumber=? AND DateBooking=? AND COA_ID IS NULL AND Amount=?',
                            (doc_number, booking_date, amount)
                        )
                    if dup_cur.fetchone()[0] > 0:
                        skipped += 1
                        skipped_rows.append({
                            'zeile': pr['zeile'], 'datum': booking_date,
                            'ref': doc_number, 'konto': pr['konto_str'],
                            'betrag': amount, 'text': pr['text'][:60],
                        })
                        continue

                # BookingGroup_ID ermitteln wenn zugehörige Gruppe >1 Zeile hat
                booking_group_id = None
                if doc_number:
                    key = (doc_number, booking_date)
                    if len(ref_groups.get(key, [])) > 1:
                        total = sum(abs(r['amount']) for r in ref_groups[key])
                        booking_group_id = _get_or_create_group(key, total, booking_date)

                dup_cur.execute('''
                    INSERT INTO Bookings
                        (DateBooking, BookingGroup_ID, COA_ID, CounterCOA_ID,
                         Amount, TaxRate, TaxAmount, Text, DocumentNumber, BookingType)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                ''', (booking_date, booking_group_id, coa_id, pr['counter_coa_id'],
                      amount, pr['tax_rate'], pr['tax_amount'], pr['text'],
                      doc_number, 'entry'))
                imported += 1

            except Exception as e:
                errors.append(f"Zeile {pr['zeile']}: {str(e)}")

        dup_conn.commit()
        dup_conn.close()

        return {
            'imported':          imported,
            'updated':           updated,
            'skipped':           skipped,
            'skipped_rows':      skipped_rows,
            'missing_coa':       sorted(missing_coa),
            'missing_counter_coa': sorted(missing_counter_coa),
            'errors':            errors,
            'format':            'original'
        }
    
    def _import_wiso_table_format(self, text: str) -> dict:
        """Import des WISO Tabellen-Exports (6 Spalten).
        
        CSV-Spalten:
            Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr./opt. Beleg Nr.;Betrag
        
        Dieser Import aktualisiert bestehende Buchungen mit zusätzlichen Daten:
        - RecipientClient (Empf./Auft.)
        - Text (Verwendungszweck) - Zeilenumbrüche werden in Leerzeichen konvertiert
        - Kategorie → COA_ID Mapping
        - Suche nach: Datum + DocumentNumber + Amount
        
        Hinweis: Zeilenumbrüche in Textfeldern (z.B. bei Überweisungstexten) werden 
        automatisch durch Leerzeichen ersetzt, um Kompatibilität zu gewährleisten.
        
        Returns:
            dict: {imported: int, updated: int, skipped: int, errors: list[str]}
        """
        import csv, io, datetime
        
        # Lookup-Map für Kategorie-Beschreibung → COA_ID
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT Name, ID FROM ChartOfAccounts')
        coa_name_map = {row[0].lower(): row[1] for row in cursor.fetchall()}
        conn.close()

        reader = csv.DictReader(io.StringIO(text), delimiter=';', quotechar='"')
        # Spaltennamen normalisieren (führende/nachgestellte Leerzeichen entfernen)
        if reader.fieldnames:
            reader.fieldnames = [f.strip() for f in reader.fieldnames]
        imported = 0
        updated = 0
        skipped = 0
        not_found = []
        errors = []

        conn = self._get_connection()
        cursor = conn.cursor()
        for i, row in enumerate(reader, 1):
            # Zeilenwerte normalisieren
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            try:
                # Datum parsen – Footer-Zeilen (reine Ziffernfolge als Datum) überspringen
                date_str = row.get('Buchungsdatum', '').strip()[:10]
                if not date_str or date_str.lstrip('-').isdigit():
                    skipped += 1
                    continue
                try:
                    booking_date = datetime.datetime.strptime(date_str, '%d.%m.%Y').strftime('%Y-%m-%d')
                except ValueError:
                    errors.append(f"Zeile {i}: Ungültiges Datum '{date_str}'")
                    continue
                
                # Empfänger/Auftraggeber (Zeilenumbrüche normalisieren)
                recipient = ' '.join(row.get('Empf./Auft.', '').split())
                
                # Konto-Nr. / IBAN (neues Feld im erweiterten Tabellen-Export)
                iban = ' '.join(row.get('Konto-Nr. / IBAN', '').split())
                
                # Verwendungszweck (Zeilenumbrüche normalisieren)
                purpose = ' '.join(row.get('Verwendungszweck', '').split())
                
                # Belegnummer (flexibel für beide Varianten)
                doc_number = row.get('opt. Beleg Nr.', row.get('Beleg Nr.', '')).strip()
                
                # Betrag parsen – deutsches Format z.B. -41,25 oder 1.234,56
                amount_str = row.get('Betrag', '').strip()
                if ',' in amount_str:
                    amount_str = amount_str.replace('.', '').replace(',', '.')
                try:
                    amount = float(amount_str)
                except ValueError:
                    errors.append(f"Zeile {i}: Ungültiger Betrag '{amount_str}'")
                    continue
                
                # Kategorie → COA_ID
                category_desc = ' '.join(row.get('Kategorie', '').split())
                coa_id_from_category = coa_name_map.get(category_desc.lower()) if category_desc else None
                
                # Suche nach bestehender Buchung: Datum + Belegnummer + Betrag
                # Betrag-Suche mit ABS(), da Original-Export positive und
                # Tabellen-Export negative Vorzeichen verwenden kann.

                if doc_number:
                    # Beleg-Nr. kann im Original als Mehrfach-Ref gespeichert sein,
                    # z.B. "25F009, 25F073" – LIKE-Suche fängt alle Varianten ab.
                    cursor.execute('''
                        SELECT ID, RecipientClient, Text, COA_ID, ForeignBankAccount
                        FROM Bookings 
                        WHERE DateBooking=? AND ABS(Amount)=ABS(?)
                          AND (
                            DocumentNumber = ?
                            OR DocumentNumber LIKE (? || ',%')
                            OR DocumentNumber LIKE ('%,' || ?)
                            OR DocumentNumber LIKE ('%,' || ? || ',%')
                          )
                        LIMIT 1
                    ''', (booking_date, amount, doc_number, doc_number, doc_number, doc_number))
                else:
                    # Ohne Belegnummer: Datum + |Betrag| (LIMIT 2 für Mehrdeutigkeitsprüfung)
                    cursor.execute('''
                        SELECT ID, RecipientClient, Text, COA_ID, ForeignBankAccount
                        FROM Bookings 
                        WHERE DateBooking=? AND ABS(Amount)=ABS(?) AND (DocumentNumber IS NULL OR DocumentNumber='')
                        LIMIT 2
                    ''', (booking_date, amount))
                
                rows = cursor.fetchall()

                if len(rows) == 0:
                    not_found.append({
                        'zeile':  i,
                        'datum':  booking_date,
                        'beleg':  doc_number,
                        'betrag': amount,
                        'text':   purpose[:60],
                    })
                    continue
                
                if len(rows) > 1:
                    # Ohne Belegnummer und mehrdeutig → überspringen
                    skipped += 1
                    continue
                
                existing = rows[0]
                booking_id           = existing[0]
                current_recipient    = existing[1] or ''
                current_coa_id       = existing[3]
                current_foreign_bank = existing[4] or ''
                
                update_fields = []
                update_values = []
                
                # Empfänger immer überschreiben, wenn im Tabellen-Export vorhanden
                if recipient:
                    update_fields.append('RecipientClient=?')
                    update_values.append(recipient)
                
                # IBAN/Konto-Nr. immer überschreiben, wenn im Tabellen-Export vorhanden
                if iban:
                    update_fields.append('ForeignBankAccount=?')
                    update_values.append(iban)
                
                # Verwendungszweck nur setzen, wenn noch leer (Original-Text bleibt erhalten)
                if purpose and not (existing[2] or ''):
                    update_fields.append('Text=?')
                    update_values.append(purpose)
                
                # COA nur setzen, wenn noch nicht vorhanden
                if coa_id_from_category and not current_coa_id:
                    update_fields.append('COA_ID=?')
                    update_values.append(coa_id_from_category)
                
                if update_fields:
                    update_values.append(booking_id)
                    cursor.execute(
                        f'UPDATE Bookings SET {", ".join(update_fields)} WHERE ID=?',
                        update_values
                    )
                    updated += 1
                else:
                    skipped += 1

            except Exception as e:
                errors.append(f"Zeile {i}: {str(e)}")

        conn.commit()
        conn.close()

        return {
            'imported':  imported,
            'updated':   updated,
            'skipped':   skipped,
            'not_found': not_found,
            'errors':    errors,
            'format':    'table'
        }

    # ── Auto-Linking: Bank ↔ Entry ────────────────────────────────────────────

    def link_bank_to_entries(self) -> dict:
        """Verknüpft Bank-Buchungen (BookingType='bank') mit passenden
        Entry-Buchungen (BookingType='entry') über ParentBooking_ID.

        Matching-Strategien (in dieser Reihenfolge):

        Stufe 1 – Datum + normalisierter Empfänger + ABS(Betrag):
            Leerzeichen in RecipientClient werden komprimiert (REPLACE+LOWER).
            Doppik-Entries (COA 1460-1940) werden rausgefiltert.
            Mehrfach-Treffer (z.B. Fraenk) werden 1:1 zugeordnet.

        Stufe 2 – Datum + ABS(Betrag):
            Ohne Empfänger-Bedingung, Doppik-Filter aktiv.
            Eindeutiger Treffer wird verknüpft.

        Stufe 3 – Split-Gruppe: Datum + ABS(SUM der Gruppenmitglieder):
            Für Bank-Buchungen die einer BookingGroup (Split) entsprechen.

        Stufe 3b – Rechnungs-Split: Datum + ABS(SUM/Anzahl):
            Für Ausgangsrechnungs-Zahlungen die als Doppelbuchung
            erfasst werden (z.B. Bank 1810 + Erlöse 4405).  Die Gruppe
            wird nur verknüpft, wenn mindestens ein Mitglied ein
            Bank-COA hat.

        Stufe 3c – Privatanteil-Split: Datum + SUM ohne Privatentnahme-Offset:
            Für Split-Gruppen deren Summe durch eine positive
            Privatentnahme-Gegenbuchung (COA 2100–2199) verfälscht wird.
            Die Gruppensumme abzüglich des positiven Privatanteils
            muss dem Bankbetrag entsprechen.

        Stufe 3d – Sammelzahlung: Datum + mehrere Rechnungsnummern im Text:
            Für Bank-Buchungen mit komma-getrennten Rechnungsnummern im
            Text (z.B. "2025011,2025010").  Die Entries mehrerer
            BookingGroups werden zusammengefasst.  Summe der Bank-COA-
            Entries über alle Gruppen muss dem Bankbetrag entsprechen.

        Stufe 4 – DocumentNumber als Tiebreaker:
            Falls Stufe 2 mehrere Treffer liefert, wird versucht
            ob genau einer die passende Belegnummer enthält.

        Stufe 5 – Text-Token-Matching:
            Letzte Chance: Extrahiert lange Ziffernfolgen (>= 8 Stellen)
            aus dem Banktext und sucht denselben Token im Entry-Text.
            Deckt Fälle wie fraenk-Rechnungsnummern oder andere
            Transaktions-IDs im Verwendungszweck ab.

        Stufe 6 – Text-Similarity-Matching (fehlende BelegNr):
            Wenn mehrere Entries auf Datum+Betrag matchen, aber weder
            DocumentNumber- noch Token-Tiebreak greifen (z.B.
            Privatentnahmen ohne BelegNr), wird der Entry mit dem
            ähnlichsten Text gewählt (SequenceMatcher, normalisiert).
            Nur wenn der Beste eindeutig besser ist als der Zweitbeste
            und die Ähnlichkeit > 50 % beträgt.

        Stufe 7 – Debitoren-Auflösung (nach der Hauptschleife):
            Debitoren-Entries (COA 10000) bei Rechnungserstellung haben
            ein früheres Datum als die spätere Zahlung und können daher
            nie per Datum matchen.  Wenn eine Zahlung-Entry (gleiche
            DocumentNumber, CounterCOA=Debitoren) bereits verknüpft ist,
            wird der Debitoren-Entry als Status='resolved' markiert.

        Nach dem Linken wird der Text der Bank-Buchung durch den Text der
        Entry-Buchung ersetzt (WISO-kuratierter Text hat Vorrang).

        Returns:
            dict mit { 'linked': int, 'skipped': int, 'repaired': int,
                        'resolved': int, 'errors': list[str] }
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # ── Schritt 0: Altdaten-Reparatur ─────────────────────────────────
        cursor.execute('''
            UPDATE Bookings SET BookingType = 'bank'
            WHERE (BookingType IS NULL OR BookingType = 'entry')
              AND Account_ID IS NOT NULL
              AND COA_ID IS NULL
              AND CounterCOA_ID IS NULL
        ''')
        repaired = cursor.rowcount
        if repaired:
            conn.commit()

        # Doppik-COA-IDs — nur echte Bankkonten (aus Accounts-Tabelle)
        bank_coa_ids = self._get_bank_coa_ids(cursor)

        # Privatentnahmen-COA-IDs (SKR04 Konten 2100-2199)
        cursor.execute('''
            SELECT ID FROM ChartOfAccounts
            WHERE AccountNumber >= 2100 AND AccountNumber < 2200
        ''')
        private_coa_ids = {r[0] for r in cursor.fetchall()}

        # ── Schritt 1: Alle unverknüpften Bank-Buchungen laden ───────────
        cursor.execute('''
            SELECT b.ID, b.DateBooking, b.Amount, b.Account_ID,
                   b.DocumentNumber, b.ForeignBankAccount,
                   b.RecipientClient, b.Text
            FROM Bookings b
            WHERE b.BookingType = 'bank'
              AND b.ID NOT IN (
                  SELECT ParentBooking_ID FROM Bookings
                  WHERE ParentBooking_ID IS NOT NULL
              )
            ORDER BY b.DateBooking, b.Amount
        ''')
        bank_bookings = cursor.fetchall()

        linked = 0
        skipped = 0
        errors = []
        already_linked_entry_ids = set()  # Für 1:1 Multi-Match (Fraenk)

        def _norm(s):
            """Empfänger normalisieren: Leerzeichen komprimieren + lowercase."""
            return ' '.join((s or '').split()).lower()

        def _filter_doppik(entries):
            """Rein liquide Spiegelbuchungen rausfiltern (COA und Gegenkonto)."""
            filtered = []
            for e in entries:
                coa_id = e[3]
                counter_coa_id = e[4]
                if coa_id in bank_coa_ids and counter_coa_id in bank_coa_ids:
                    continue
                filtered.append(e)
            return filtered

        def _filter_already(entries):
            """Bereits in diesem Durchlauf verknüpfte Entries rausfiltern."""
            return [e for e in entries if e[0] not in already_linked_entry_ids]

        import re
        from difflib import SequenceMatcher
        _TOKEN_RE = re.compile(r'\d{6,}')

        def _extract_tokens(text):
            """Ziffernfolgen (>=6 Stellen) aus Text extrahieren.

            Dient als eindeutige Kennung (Rechnungs-/Transaktionsnummern),
            z.B. '1040749116593' (fraenk EREF) oder '870136' (SHBB RNR)."""
            return set(_TOKEN_RE.findall(text or ''))

        def _token_tiebreak(bank_text, entries, text_idx=2):
            """Unter mehreren Entries denjenigen finden, der einen
            gemeinsamen numerischen Token mit dem Banktext teilt.

            Wenn mehrere Entries überlappende Tokens haben (z.B. weil
            gemeinsame CRED-/IBAN-Nummern in allen PayPal-Texten stehen),
            wird der Entry mit den *meisten* gemeinsamen Tokens genommen,
            sofern er eindeutig mehr hat als alle anderen.

            Args:
                bank_text: Text der Bank-Buchung
                entries:   Kandidaten-Liste (Tuples)
                text_idx:  Index des Text-Feldes im Tuple (default 2)

            Returns:
                Einzel-Entry-Tuple oder None.
            """
            bank_tokens = _extract_tokens(bank_text)
            if not bank_tokens:
                return None
            matches = [e for e in entries
                       if _extract_tokens(e[text_idx]) & bank_tokens]
            if len(matches) == 1:
                return matches[0]
            if len(matches) >= 2:
                # Score = Anzahl gemeinsamer Tokens; höchster gewinnt
                scored = [(len(_extract_tokens(e[text_idx]) & bank_tokens), e)
                          for e in matches]
                scored.sort(key=lambda x: x[0], reverse=True)
                if scored[0][0] > scored[1][0]:
                    return scored[0][1]
            return None

        def _do_link(bank_id, entry_id, entry_group_id, entry_text):
            """Verknüpfe entry (oder ganze Gruppe) mit bank."""
            if entry_group_id:
                # Alle Gruppenmitglieder verknüpfen
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE BookingGroup_ID = ? AND BookingType = 'entry'
                ''', (bank_id, entry_group_id))
                # Alle Gruppen-IDs als bereits verknüpft markieren
                cursor.execute(
                    'SELECT ID FROM Bookings WHERE BookingGroup_ID = ?',
                    (entry_group_id,))
                for r in cursor.fetchall():
                    already_linked_entry_ids.add(r[0])
            else:
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE ID = ?
                ''', (bank_id, entry_id))
                already_linked_entry_ids.add(entry_id)
            # WISO-Text auf die Bank-Buchung übernehmen (manuell kuratiert)
            if entry_text:
                cursor.execute(
                    'UPDATE Bookings SET Text = ? WHERE ID = ?',
                    (entry_text, bank_id))

        for bank in bank_bookings:
            (bank_id, bank_date, bank_amount, bank_account_id,
             bank_docnr, bank_iban, bank_recipient, bank_text) = bank

            abs_amount = round(abs(bank_amount), 2)
            recip_norm = _norm(bank_recipient)

            # ── Stufe 1: Datum + Empfänger (normalisiert) + ABS(Betrag) ──
            if recip_norm:
                cursor.execute('''
                    SELECT ID, BookingGroup_ID, Text, COA_ID, CounterCOA_ID, RecipientClient FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NULL
                      AND DateBooking = ?
                      AND ABS(ABS(Amount) - ?) < 0.005
                ''', (bank_date, abs_amount))
                raw = cursor.fetchall()
                entries = _filter_already(_filter_doppik(
                    [e for e in raw if _norm(e[5]) == recip_norm
                     or _norm(e[2]) != '' and recip_norm in _norm(e[2])]
                ))
                if not entries:
                    # Fallback: direkter DB-Vergleich (REPLACE normalisiert)
                    cursor.execute('''
                        SELECT ID, BookingGroup_ID, Text, COA_ID, CounterCOA_ID FROM Bookings
                        WHERE BookingType = 'entry'
                          AND ParentBooking_ID IS NULL
                          AND DateBooking = ?
                          AND ABS(ABS(Amount) - ?) < 0.005
                          AND LOWER(REPLACE(REPLACE(REPLACE(TRIM(
                              COALESCE(RecipientClient,'')), '  ', ' '), '  ', ' '), '  ', ' '))
                            = ?
                    ''', (bank_date, abs_amount, recip_norm))
                    entries = _filter_already(_filter_doppik(cursor.fetchall()))
                if len(entries) == 1:
                    _do_link(bank_id, entries[0][0], None, entries[0][2])
                    linked += 1
                    continue
                if len(entries) >= 2:
                    # Mehrere Treffer: Token-Tiebreak (z.B. Fraenk-Nummern)
                    token_hit = _token_tiebreak(bank_text, entries)
                    if token_hit:
                        _do_link(bank_id, token_hit[0], None, token_hit[2])
                        linked += 1
                        continue
                    # Fallback: ersten verfügbaren nehmen
                    _do_link(bank_id, entries[0][0], None, entries[0][2])
                    linked += 1
                    continue

            # ── Stufe 2: Datum + ABS(Betrag) ─────────────────────────────
            cursor.execute('''
                                SELECT ID, BookingGroup_ID, Text, COA_ID, CounterCOA_ID, DocumentNumber
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND ABS(ABS(Amount) - ?) < 0.005
            ''', (bank_date, abs_amount))
            entries = _filter_already(_filter_doppik(cursor.fetchall()))
            if len(entries) == 1:
                # Nur diesen Entry linken, NICHT die ganze Gruppe
                _do_link(bank_id, entries[0][0], None, entries[0][2])
                linked += 1
                continue

            # ── Stufe 4: DocumentNumber als Tiebreaker ───────────────────
            if len(entries) > 1 and bank_docnr:
                doc_match = [e for e in entries
                             if e[5] and (bank_docnr in e[5] or e[5] in bank_docnr)]
                if len(doc_match) == 1:
                    # Nur diesen Entry linken, NICHT die ganze Gruppe
                    _do_link(bank_id, doc_match[0][0], None, doc_match[0][2])
                    linked += 1
                    continue

            # ── Stufe 3: Split-Gruppe — SUM(Betrag) passt ────────────────
            cursor.execute('''
                SELECT b.BookingGroup_ID, COUNT(*) AS cnt
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.BookingGroup_ID IS NOT NULL
                  AND b.DateBooking = ?
                GROUP BY b.BookingGroup_ID
                HAVING ABS(ABS(SUM(b.Amount)) - ?) < 0.005
            ''', (bank_date, abs_amount))
            groups = cursor.fetchall()
            # Bereits verknüpfte Gruppen rausfiltern
            groups = [g for g in groups if g[0] not in
                      {eid for eid in already_linked_entry_ids}]
            if len(groups) == 1:
                group_id = groups[0][0]
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE BookingGroup_ID = ? AND BookingType = 'entry'
                      AND DateBooking = ?
                ''', (bank_id, group_id, bank_date))
                cursor.execute(
                    'SELECT ID FROM Bookings WHERE BookingGroup_ID = ?'
                    ' AND DateBooking = ?',
                    (group_id, bank_date))
                for r in cursor.fetchall():
                    already_linked_entry_ids.add(r[0])
                linked += 1
                continue

            # ── Stufe 3b: Rechnungs-Split — Betrag = SUM/Anzahl ─────────
            # Muster: Ausgangsrechnung wird bezahlt → 2 Entries mit
            # gleichem Betrag (COA Bank + COA Erlöse), SUM = 2× Bankbetrag.
            # Erkennung: Ein Gruppenmitglied hat COA = Bankkonto.
            cursor.execute('''
                SELECT b.BookingGroup_ID, COUNT(*) AS cnt,
                       SUM(b.Amount) AS total
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.BookingGroup_ID IS NOT NULL
                  AND b.DateBooking = ?
                GROUP BY b.BookingGroup_ID
                HAVING cnt > 1
                   AND ABS(ABS(total / cnt) - ?) < 0.005
            ''', (bank_date, abs_amount))
            inv_groups = cursor.fetchall()
            # Filtern: Gruppe muss ein Mitglied mit Bank-COA haben
            inv_matches = []
            for g in inv_groups:
                gid = g[0]
                if gid in already_linked_entry_ids:
                    continue
                cursor.execute(
                    'SELECT COA_ID FROM Bookings WHERE BookingGroup_ID = ? AND BookingType = ?',
                    (gid, 'entry'))
                coa_ids = {r[0] for r in cursor.fetchall()}
                if coa_ids & bank_coa_ids:  # mindestens ein Bank-COA
                    inv_matches.append(gid)
            if len(inv_matches) == 1:
                group_id = inv_matches[0]
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE BookingGroup_ID = ? AND BookingType = 'entry'
                      AND DateBooking = ?
                ''', (bank_id, group_id, bank_date))
                cursor.execute(
                    'SELECT ID FROM Bookings WHERE BookingGroup_ID = ?'
                    ' AND DateBooking = ?',
                    (group_id, bank_date))
                for r in cursor.fetchall():
                    already_linked_entry_ids.add(r[0])
                linked += 1
                continue

            # ── Stufe 3c: Privatanteil-Split ─────────────────────────────
            # Muster: Split-Gruppe enthält eine positive Gegenbuchung auf
            # ein Privatentnahme-Konto (2100–2199), die den Bankbetrag
            # verfälscht.  Erkennung: Gruppensumme ohne positive
            # Privatentnahme-Einträge ≈ Bankbetrag.
            cursor.execute('''
                SELECT b.BookingGroup_ID,
                       SUM(b.Amount) AS total,
                       SUM(CASE WHEN b.Amount > 0 AND b.COA_ID IN
                           (SELECT ID FROM ChartOfAccounts
                            WHERE AccountNumber >= 2100 AND AccountNumber < 2200)
                           THEN b.Amount ELSE 0 END) AS private_offset
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.BookingGroup_ID IS NOT NULL
                  AND b.DateBooking = ?
                GROUP BY b.BookingGroup_ID
                HAVING private_offset > 0
                   AND ABS(ABS(total - private_offset) - ?) < 0.005
            ''', (bank_date, abs_amount))
            priv_groups = cursor.fetchall()
            priv_matches = [g[0] for g in priv_groups
                            if g[0] not in already_linked_entry_ids]
            if len(priv_matches) == 1:
                group_id = priv_matches[0]
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE BookingGroup_ID = ? AND BookingType = 'entry'
                      AND DateBooking = ?
                ''', (bank_id, group_id, bank_date))
                cursor.execute(
                    'SELECT ID FROM Bookings WHERE BookingGroup_ID = ?'
                    ' AND DateBooking = ?',
                    (group_id, bank_date))
                for r in cursor.fetchall():
                    already_linked_entry_ids.add(r[0])
                linked += 1
                continue

            # ── Stufe 3d: Sammelzahlung ────────────────────────────────
            # Muster: Bank-Text enthält mehrere komma- oder leerzeichen-
            # getrennte Rechnungsnummern (z.B. "2025011,2025010").
            # Die zugehörigen Entries liegen in verschiedenen
            # BookingGroups.  Summe der Bank-COA-Entries über alle
            # Gruppen muss dem Bankbetrag entsprechen.
            doc_nr_candidates = set(re.findall(r'\b\d{4,}\b',
                                               bank_text or ''))
            if len(doc_nr_candidates) >= 2:
                ph = ','.join('?' * len(doc_nr_candidates))
                cursor.execute(f'''
                    SELECT ID, BookingGroup_ID, Text, COA_ID,
                           CounterCOA_ID, DocumentNumber, Amount
                    FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NULL
                      AND DateBooking = ?
                      AND DocumentNumber IN ({ph})
                ''', (bank_date, *doc_nr_candidates))
                sammel_entries = _filter_already(cursor.fetchall())
                doc_nrs_found = {e[5] for e in sammel_entries}
                if len(doc_nrs_found) >= 2 and len(sammel_entries) >= 2:
                    bank_coa_sum = sum(
                        e[6] for e in sammel_entries
                        if e[3] in bank_coa_ids)
                    if abs(abs(bank_coa_sum) - abs_amount) < 0.005:
                        for e in sammel_entries:
                            cursor.execute(
                                'UPDATE Bookings SET ParentBooking_ID = ?'
                                ' WHERE ID = ?',
                                (bank_id, e[0]))
                            already_linked_entry_ids.add(e[0])
                        linked += 1
                        continue

            # ── Stufe 5: Text-Token-Matching (letzte Chance) ───────────
            # Suche unter allen ungelinkten Entries desselben Datums+Betrags
            # nach einem gemeinsamen numerischen Token (>= 6 Stellen) im
            # Buchungstext.  Deckt z.B. fraenk-EREF-Nummern, SHBB-RNR-
            # Nummern und andere Fälle mit Transaktions-IDs im Text ab.
            cursor.execute('''
                SELECT ID, BookingGroup_ID, Text, COA_ID, CounterCOA_ID
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND ABS(ABS(Amount) - ?) < 0.005
            ''', (bank_date, abs_amount))
            all_candidates = _filter_already(_filter_doppik(cursor.fetchall()))
            token_hit = _token_tiebreak(bank_text, all_candidates)
            if token_hit:
                _do_link(bank_id, token_hit[0], None, token_hit[2])
                linked += 1
                continue

            # ── Stufe 6: Text-Similarity (fehlende BelegNr) ────────────
            # Wenn mehrere Entries zum selben Datum+Betrag passen, aber
            # weder DocNr- noch Token-Tiebreak greift (z.B. Privatent-
            # nahmen ohne BelegNr), wird der Entry mit dem ähnlichsten
            # Text gewählt.  Normalisierung: Leerzeichen entfernen,
            # lowercase.  Eindeutig bester Score (> zweitbester und > 0.5)
            # wird verknüpft.
            if len(all_candidates) >= 2:
                def _text_norm(s):
                    return ''.join((s or '').lower().split())
                bank_norm = _text_norm(bank_text)
                if bank_norm:
                    scored = [
                        (SequenceMatcher(None, bank_norm,
                                         _text_norm(e[2])).ratio(), e)
                        for e in all_candidates
                    ]
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if (scored[0][0] > scored[1][0]
                            and scored[0][0] > 0.5):
                        best = scored[0][1]
                        _do_link(bank_id, best[0], None, best[2])
                        linked += 1
                        continue

            skipped += 1

        # ── Stufe 7: Debitoren-Auflösung ─────────────────────────────────
        # Debitoren-Entries (COA 10000) entstehen bei Rechnungserstellung
        # und haben ein früheres Datum als Bank- und Zahlungsbuchungen.
        # Sie können nie per Datum-Match verknüpft werden.
        # Lösung: Wenn eine Zahlung-Entry (COA = Bank, CounterCOA =
        # Debitoren) mit gleicher DocumentNumber bereits verknüpft ist,
        # setze Status = 'resolved' auf dem Debitoren-Entry.
        cursor.execute('''
            SELECT ID FROM ChartOfAccounts
            WHERE AccountNumber = 10000
        ''')
        debitoren_row = cursor.fetchone()
        resolved_count = 0
        if debitoren_row:
            debitoren_coa_id = debitoren_row[0]
            cursor.execute('''
                SELECT ID, DocumentNumber
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND COA_ID = ?
                  AND (Status IS NULL OR Status != 'resolved')
            ''', (debitoren_coa_id,))
            debitoren_entries = cursor.fetchall()

            for deb_id, doc_nr in debitoren_entries:
                if not doc_nr:
                    continue
                # Suche eine verknüpfte Zahlung-Entry mit gleicher DocNr
                # und CounterCOA = Debitoren (d.h. Zahlung auf Debitor)
                cursor.execute('''
                    SELECT ID FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NOT NULL
                      AND DocumentNumber = ?
                      AND CounterCOA_ID = ?
                    LIMIT 1
                ''', (doc_nr, debitoren_coa_id))
                if cursor.fetchone():
                    cursor.execute(
                        "UPDATE Bookings SET Status = 'resolved'"
                        " WHERE ID = ?",
                        (deb_id,))
                    resolved_count += 1

        conn.commit()
        conn.close()
        return {'linked': linked, 'skipped': skipped, 'repaired': repaired,
                'resolved': resolved_count, 'errors': errors}

    # ── Table Contacts (normalized Option C) ───────────────────────────────────

    _CONTACTS_QUERY = '''
        SELECT
            c.ID,                          -- 0  id
            c.ContactType,                 -- 1  contact_type
            c.CustomerNumber,              -- 2  customer_number
            COALESCE(
                c.DisplayName,
                CASE c.EntityType
                    WHEN 'company' THEN cd.CompanyName
                    WHEN 'person'  THEN TRIM(
                        COALESCE(pd.Title      || ' ', '') ||
                        COALESCE(pd.FirstName  || ' ', '') ||
                        COALESCE(pd.LastName,           '')
                    )
                    ELSE c.DisplayName
                END
            )                          AS display_name,  -- 3
            CASE c.EntityType
                WHEN 'company' THEN cd.CompanyName
                WHEN 'person'  THEN COALESCE(
                    cd2.CompanyName,
                    pd.CompanyName_Free
                )
                ELSE NULL
            END                        AS company_name,  -- 4
            ca.Street,                     -- 5  street
            ca.PostalCode,                 -- 6  postal_code
            ca.City,                       -- 7  city
            COALESCE(ca.Country, 'DE')  AS country,       -- 8
            c.Email,                       -- 9  email
            c.Phone,                       -- 10 phone
            COALESCE(cd.TaxID,       '') AS tax_id,        -- 11
            c.Notes,                       -- 12 notes
            c.Logo,                        -- 13 logo
            COALESCE(cd.BuyerRouteID,'') AS buyer_route_id,-- 14
            c.EntityType,                  -- 15 entity_type  (NEW)
            c.DisplayName AS display_name_manual, -- 16      (NEW)
            cd.LegalForm,                  -- 17 legal_form   (NEW)
            pd.Salutation,                 -- 18 salutation   (NEW)
            pd.Title,                      -- 19 title        (NEW)
            pd.FirstName,                  -- 20 first_name   (NEW)
            pd.LastName,                   -- 21 last_name    (NEW)
            pd.DateOfBirth,                -- 22 date_of_birth(NEW)
            pd.CompanyContactID,           -- 23              (NEW)
            pd.CompanyName_Free,           -- 24              (NEW)
            ca.AddressLine1                -- 25 address_line1(NEW)
        FROM Contacts c
        LEFT JOIN CompanyDetails    cd ON c.ID = cd.ContactID
        LEFT JOIN PersonDetails     pd ON c.ID = pd.ContactID
        LEFT JOIN ContactAddresses  ca ON c.ID = ca.ContactID AND ca.AddressType = 'main'
        LEFT JOIN CompanyDetails    cd2 ON cd2.ContactID = pd.CompanyContactID
    '''

    def fetch_contacts(self, contact_type=None, entity_type=None):
        """Fetch contacts, optionally filtered by ContactType and/or EntityType.
        Returns sqlite3.Row objects (support both index and column-name access)."""
        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        conditions = []
        params = []
        if contact_type:
            conditions.append('c.ContactType = ?')
            params.append(contact_type)
        if entity_type:
            conditions.append('c.EntityType = ?')
            params.append(entity_type)

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        cursor.execute(f'{self._CONTACTS_QUERY} {where} ORDER BY display_name ASC', params)
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_contact_by_id(self, contact_id):
        """Get full contact row by ID (same column layout as fetch_contacts)."""
        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f'{self._CONTACTS_QUERY} WHERE c.ID = ?', (contact_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def insert_contact(self, contact_type='customer', entity_type='company',
                       display_name=None, customer_number=None,
                       email='', phone='', notes='', logo='',
                       # address
                       address_line1='', street='', postal_code='', city='', country='DE',
                       # company
                       company_name='', legal_form='', tax_id='', buyer_route_id='',
                       # person
                       salutation='', title='', first_name='', last_name='',
                       date_of_birth='', company_contact_id=None, company_name_free=''):
        """Insert a new contact with sub-table records."""
        conn = self._get_connection()
        cursor = conn.cursor()
        if not customer_number or not str(customer_number).strip():
            customer_number = None
        try:
            cursor.execute(
                'INSERT INTO Contacts (ContactType, EntityType, DisplayName, CustomerNumber, Email, Phone, Notes, Logo) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (contact_type, entity_type, display_name or None, customer_number,
                 email, phone, notes, logo)
            )
            contact_id = cursor.lastrowid

            cursor.execute(
                'INSERT INTO ContactAddresses (ContactID, AddressType, AddressLine1, Street, PostalCode, City, Country) '
                'VALUES (?, \'main\', ?, ?, ?, ?, ?)',
                (contact_id, address_line1 or None, street, postal_code, city, country or 'DE')
            )

            if entity_type == 'company':
                cursor.execute(
                    'INSERT INTO CompanyDetails (ContactID, CompanyName, LegalForm, TaxID, BuyerRouteID) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (contact_id, company_name or None, legal_form or None,
                     tax_id or None, buyer_route_id or None)
                )
            elif entity_type == 'person':
                cursor.execute(
                    'INSERT INTO PersonDetails '
                    '(ContactID, Salutation, Title, FirstName, LastName, DateOfBirth, CompanyContactID, CompanyName_Free) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (contact_id, salutation or None, title or None,
                     first_name, last_name, date_of_birth or None,
                     int(company_contact_id) if company_contact_id else None,
                     company_name_free or None)
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f'Error inserting contact: {e}')
            raise
        finally:
            conn.close()

    def update_contact(self, contact_id, contact_type='customer', entity_type='company',
                       display_name=None, customer_number=None,
                       email='', phone='', notes='', logo='',
                       address_line1='', street='', postal_code='', city='', country='DE',
                       company_name='', legal_form='', tax_id='', buyer_route_id='',
                       salutation='', title='', first_name='', last_name='',
                       date_of_birth='', company_contact_id=None, company_name_free=''):
        """Update an existing contact and all sub-table records."""
        conn = self._get_connection()
        cursor = conn.cursor()
        if not customer_number or not str(customer_number).strip():
            customer_number = None
        try:
            cursor.execute(
                'UPDATE Contacts SET ContactType=?, EntityType=?, DisplayName=?, CustomerNumber=?, '
                'Email=?, Phone=?, Notes=?, Logo=? WHERE ID=?',
                (contact_type, entity_type, display_name or None, customer_number,
                 email, phone, notes, logo, contact_id)
            )
            # Address: delete + re-insert
            cursor.execute('DELETE FROM ContactAddresses WHERE ContactID=? AND AddressType=\'main\'',
                           (contact_id,))
            cursor.execute(
                'INSERT INTO ContactAddresses (ContactID, AddressType, AddressLine1, Street, PostalCode, City, Country) '
                'VALUES (?, \'main\', ?, ?, ?, ?, ?)',
                (contact_id, address_line1 or None, street, postal_code, city, country or 'DE')
            )
            # Entity details: delete + re-insert
            cursor.execute('DELETE FROM CompanyDetails WHERE ContactID=?', (contact_id,))
            cursor.execute('DELETE FROM PersonDetails  WHERE ContactID=?', (contact_id,))
            if entity_type == 'company':
                cursor.execute(
                    'INSERT INTO CompanyDetails (ContactID, CompanyName, LegalForm, TaxID, BuyerRouteID) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (contact_id, company_name or None, legal_form or None,
                     tax_id or None, buyer_route_id or None)
                )
            elif entity_type == 'person':
                cursor.execute(
                    'INSERT INTO PersonDetails '
                    '(ContactID, Salutation, Title, FirstName, LastName, DateOfBirth, CompanyContactID, CompanyName_Free) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (contact_id, salutation or None, title or None,
                     first_name, last_name, date_of_birth or None,
                     int(company_contact_id) if company_contact_id else None,
                     company_name_free or None)
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f'Error updating contact: {e}')
            raise
        finally:
            conn.close()

    def delete_contact(self, contact_id):
        """Delete contact (sub-tables are CASCADE deleted)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM Contacts WHERE ID = ?', (contact_id,))
        conn.commit()
        conn.close()

    # Table Articles
    def fetch_articles(self, active_only=False):
        """Fetch all articles
        
        Args:
            active_only: If True, only return active articles (default: False)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        if active_only:
            cursor.execute('SELECT * FROM Articles WHERE Active = 1 ORDER BY Name')
        else:
            cursor.execute('SELECT * FROM Articles ORDER BY Name')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_article_by_id(self, article_id):
        """Get article by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Articles WHERE ID = ?', (article_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def insert_article(self, name, unit="Stk.", unit_price=0, tax_rate=19, description="", active=1):
        """Insert new article
        
        Args:
            name: Article name (required)
            unit: Unit of measurement (default: Stk.)
            unit_price: Net unit price (default: 0)
            tax_rate: Tax rate in percent (default: 19)
            description: Optional description
            active: Whether article is active (default: 1=True)
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        sql_template = '''
            INSERT INTO Articles (Name, Unit, UnitPrice, TaxRate, Description, Active)
            VALUES (?, ?, ?, ?, ?, ?)'''
        params = (name, unit, unit_price, tax_rate, description, active)

        try:
            cursor.execute(sql_template, params)
            conn.commit()
            self._log_sql(sql_template, params, "Insert article")
        except sqlite3.IntegrityError as e:
            print("Error inserting article:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_article(self, article_id, name, unit="Stk.", unit_price=0, tax_rate=19, description="", active=1):
        """Update existing article"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            sql_template = '''
                UPDATE Articles
                SET Name = ?, Unit = ?, UnitPrice = ?, TaxRate = ?, Description = ?, Active = ?
                WHERE ID = ?'''
            params = (name, unit, unit_price, tax_rate, description, active, article_id)
            cursor.execute(sql_template, params)
            conn.commit()
            self._log_sql(sql_template, params, "Update article")
        except sqlite3.IntegrityError as e:
            print("Error updating article:", e)
            conn.rollback()
        finally:
            conn.close()

    def delete_article(self, article_id):
        """Delete article"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM Articles WHERE ID = ?', (article_id,))
        conn.commit()
        conn.close()

    # Table NumberRanges

    @staticmethod
    def _apply_number_format(fmt: str, year: int, letter: str, number: int, suffix: str = '') -> str:
        """Apply a format template to produce a number string.

        Placeholders:
          {yyyy}  - 4-digit year (e.g. 2026)
          {yy}    -  2-digit year (e.g. 26)
          {l}     - letter (uppercase)
          {nnn}   - 3-digit zero-padded number (e.g. 001)
          {nn}    - 2-digit zero-padded number
          {n}     - unpadded number
          {s}     - suffix (appended as-is, empty when not set)

        Default template '{yy}{l}{nnn}{s}' produces '26F001' or '26F002_A'.
        """
        DEFAULT_FORMAT = '{yy}{l}{nnn}{s}'
        template = fmt if fmt else DEFAULT_FORMAT
        result = template
        result = result.replace('{yyyy}', str(year))
        result = result.replace('{yy}', str(year)[-2:])
        result = result.replace('{l}', letter.upper())
        result = result.replace('{nnn}', f'{number:03d}')
        result = result.replace('{nn}', f'{number:02d}')
        result = result.replace('{n}', str(number))
        result = result.replace('{s}', suffix or '')
        return result

    def fetch_number_ranges(self, range_type=None):
        """Fetch all number ranges, optionally filtered by type
        
        Args:
            range_type: Optional filter ('invoice', 'receipt_company', 'receipt_category')
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        if range_type:
            cursor.execute('SELECT * FROM NumberRanges WHERE Type = ? ORDER BY Year DESC, Letter, Prefix', (range_type,))
        else:
            cursor.execute('SELECT * FROM NumberRanges ORDER BY Type, Year DESC, Letter, Prefix')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_number_range(self, range_type, year, letter, prefix=''):
        """Get a specific number range"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM NumberRanges 
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()
        conn.close()
        return row

    def get_number_range_by_id(self, range_id):
        """Get number range by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM NumberRanges WHERE ID = ?', (range_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def insert_number_range(self, range_type, year, letter, prefix='', current_number=0, description='', number_format=None):
        """Insert a new number range

        Args:
            range_type: 'invoice', 'receipt_company', or 'receipt_category'
            year: 4-digit year (will be stored as-is, displayed as 2-digit)
            letter: Single letter identifier (e.g., 'R' for Rechnung)
            prefix: Optional suffix for subdivision (e.g., '_A') – appended after the number
            current_number: Starting number (default: 0)
            description: Optional description
            number_format: Format template (default: '{yy}{l}{nnn}{s}')
        """
        fmt = number_format if number_format else '{yy}{l}{nnn}{s}'
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO NumberRanges (Type, Year, Letter, Prefix, CurrentNumber, Description, NumberFormat)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (range_type, year, letter, prefix, current_number, description, fmt))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting number range:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_number_range(self, range_id, year, letter, prefix='', current_number=0, description='', number_format=None):
        """Update existing number range"""
        fmt = number_format if number_format else '{yy}{l}{nnn}{s}'
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE NumberRanges
                SET Year = ?, Letter = ?, Prefix = ?, CurrentNumber = ?, Description = ?, NumberFormat = ?
                WHERE ID = ?
            ''', (year, letter, prefix, current_number, description, fmt, range_id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating number range:", e)
            conn.rollback()
        finally:
            conn.close()

    def delete_number_range(self, range_id):
        """Delete number range"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM NumberRanges WHERE ID = ?', (range_id,))
        conn.commit()
        conn.close()

    def get_next_number(self, range_type, year, letter, prefix=''):
        """Get the next available number in a range and increment the counter

        Returns the full formatted number string (e.g., '26R001' or '26R001_A')
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Try to get existing range
        cursor.execute('''
            SELECT ID, CurrentNumber, NumberFormat FROM NumberRanges
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()

        if row:
            range_id, current, number_format = row
            next_num = current + 1
            cursor.execute('UPDATE NumberRanges SET CurrentNumber = ? WHERE ID = ?', (next_num, range_id))
        else:
            # Create new range for this combination
            next_num = 1
            number_format = '{yy}{l}{nnn}{s}'
            cursor.execute('''
                INSERT INTO NumberRanges (Type, Year, Letter, Prefix, CurrentNumber, Description, NumberFormat)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (range_type, year, letter, prefix, next_num, '', number_format))

        conn.commit()
        conn.close()

        return self._apply_number_format(number_format, year, letter, next_num, prefix)

    def format_number(self, year, letter, suffix, number, number_format=None):
        """Format a number using the given template (default: '{yy}{l}{nnn}{s}')"""
        fmt = number_format if number_format else '{yy}{l}{nnn}{s}'
        return self._apply_number_format(fmt, year, letter, number, suffix)

    def parse_number(self, number_str):
        """Parse a formatted number string back to components
        
        Returns: (year, letter, prefix, number) or None if invalid
        """
        import re
        # Pattern: 2-digit year, 1 letter, 3+ digits, optional prefix (_Letter)
        match = re.match(r'^(\d{2})([A-Z])(\d{3,})(_[A-Z])?$', number_str)
        if match:
            year_short = int(match.group(1))
            # Convert 2-digit year to 4-digit (assuming 2000s)
            year = 2000 + year_short if year_short < 100 else year_short
            letter = match.group(2)
            number = int(match.group(3))
            prefix = match.group(4) or ''
            return (year, letter, prefix, number)
        return None

    def shift_numbers_up(self, range_type, year, letter, prefix, from_number):
        """Shift all numbers >= from_number up by 1 in the specified range
        
        This is used when inserting a number that already exists.
        Note: This updates the stored CurrentNumber if needed.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Get current max number
        cursor.execute('''
            SELECT ID, CurrentNumber FROM NumberRanges 
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()
        
        if row:
            range_id, current = row
            if from_number <= current:
                # Increment current number since we're inserting
                cursor.execute('UPDATE NumberRanges SET CurrentNumber = ? WHERE ID = ?', (current + 1, range_id))
        
        conn.commit()
        conn.close()
        
        # Return the new number that was inserted
        return from_number

    def get_current_number_info(self, range_type, year, letter, prefix=''):
        """Get info about the current state of a number range

        Returns: dict with 'current_number', 'next_number', 'formatted_next'
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT CurrentNumber, NumberFormat FROM NumberRanges
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()
        conn.close()

        current = row[0] if row else 0
        number_format = row[1] if row else '{yy}{l}{nnn}{s}'
        next_num = current + 1

        return {
            'current_number': current,
            'next_number': next_num,
            'formatted_next': self._apply_number_format(number_format, year, letter, next_num, prefix)
        }

    # ==================== INVOICES ====================
    
    def fetch_invoices(self, status=None):
        """Fetch invoices, optionally filtered by status"""
        conn = self._get_connection()
        cursor = conn.cursor()
        if status:
            cursor.execute('SELECT * FROM Invoices WHERE Status = ? ORDER BY InvoiceDate DESC, InvoiceNumber DESC', (status,))
        else:
            cursor.execute('SELECT * FROM Invoices ORDER BY InvoiceDate DESC, InvoiceNumber DESC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_invoice_by_id(self, invoice_id):
        """Get a single invoice by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Invoices WHERE ID = ?', (invoice_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def get_invoice_items(self, invoice_id):
        """Get all items for an invoice"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', (invoice_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_invoice(self, invoice_data):
        """Insert a new invoice with all fields
        
        Args:
            invoice_data: Dictionary with all invoice fields
        
        Returns:
            invoice_id: The ID of the newly created invoice
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''
            INSERT INTO Invoices (
                InvoiceNumber, InvoiceDate,
                OwnCompanyId, SellerName, SellerCompany, SellerStreet, SellerPostalCode, SellerCity, SellerCountry, SellerVATID, SellerEmail, SellerPhone,
                CustomerId, BuyerName, BuyerCompany, BuyerStreet, BuyerPostalCode, BuyerCity, BuyerCountry, BuyerVATID, BuyerReference, BuyerRouteID,
                OrderNumber, Currency, DeliveryDate,
                PaymentTerms, PaymentDueDate, SkontoDays, SkontoPercent,
                BankAccountId, BankName, BankIBAN, BankBIC,
                TaxCategory, TaxRate, SumNet, TaxAmount, SumGross, AmountDue,
                Status, PDFPath, XMLPath
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        '''
        
        params = (
            invoice_data.get('invoice_number'),
            invoice_data.get('invoice_date'),
            invoice_data.get('own_company_id'),
            invoice_data.get('seller_name'),
            invoice_data.get('seller_company'),
            invoice_data.get('seller_street'),
            invoice_data.get('seller_postal_code'),
            invoice_data.get('seller_city'),
            invoice_data.get('seller_country', 'DE'),
            invoice_data.get('seller_vat_id'),
            invoice_data.get('seller_email'),
            invoice_data.get('seller_phone'),
            invoice_data.get('customer_id'),
            invoice_data.get('buyer_name'),
            invoice_data.get('buyer_company'),
            invoice_data.get('buyer_street'),
            invoice_data.get('buyer_postal_code'),
            invoice_data.get('buyer_city'),
            invoice_data.get('buyer_country', 'DE'),
            invoice_data.get('buyer_vat_id'),
            invoice_data.get('buyer_reference'),
            invoice_data.get('buyer_route_id'),
            invoice_data.get('order_number'),
            invoice_data.get('currency', 'EUR'),
            invoice_data.get('delivery_date'),
            invoice_data.get('payment_terms'),
            invoice_data.get('payment_due_date'),
            invoice_data.get('skonto_days'),
            invoice_data.get('skonto_percent'),
            invoice_data.get('bank_account_id'),
            invoice_data.get('bank_name'),
            invoice_data.get('bank_iban'),
            invoice_data.get('bank_bic'),
            invoice_data.get('tax_category', 'S'),
            invoice_data.get('tax_rate'),
            invoice_data.get('sum_net'),
            invoice_data.get('tax_amount'),
            invoice_data.get('sum_gross'),
            invoice_data.get('amount_due'),
            invoice_data.get('status', 'finalized'),
            invoice_data.get('pdf_path'),
            invoice_data.get('xml_path')
        )
        
        try:
            cursor.execute(sql_template, params)
            invoice_id = cursor.lastrowid
            conn.commit()
            self._log_sql(sql_template, params, "Insert invoice")
            return invoice_id
        except Exception as e:
            print(f"Error inserting invoice: {e}")
            print(f"Params: {params}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_invoice_item(self, item_data):
        """Insert an invoice item
        
        Args:
            item_data: Dictionary with item fields
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''
            INSERT INTO InvoiceItems (
                InvoiceId, Position, ArticleId, Description, Quantity, Unit, PricePerUnit, TotalNet, TaxCategory, TaxRate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''
        
        params = (
            item_data.get('invoice_id'),
            item_data.get('position'),
            item_data.get('article_id'),
            item_data.get('description'),
            item_data.get('quantity'),
            item_data.get('unit', 'C62'),
            item_data.get('price_per_unit'),
            item_data.get('total_net'),
            item_data.get('tax_category', 'S'),
            item_data.get('tax_rate')
        )
        
        try:
            cursor.execute(sql_template, params)
            conn.commit()
            self._log_sql(sql_template, params, "Insert invoice item")
        except Exception as e:
            print(f"Error inserting invoice item: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_invoice_status(self, invoice_id, status):
        """Update invoice status"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE Invoices SET Status = ?, UpdatedAt = CURRENT_TIMESTAMP WHERE ID = ?', (status, invoice_id))
            conn.commit()
        except Exception as e:
            print(f"Error updating invoice status: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_invoice(self, invoice_id):
        """Delete an invoice and its items (cascade)"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('DELETE FROM Invoices WHERE ID = ?', (invoice_id,))
            conn.commit()
        except Exception as e:
            print(f"Error deleting invoice: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def link_invoice_to_transaction(self, invoice_id, transaction_id, amount_paid):
        """Link an invoice to a payment booking via InvoicePayments and recalculate AmountDue."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT SumGross FROM Invoices WHERE ID = ?', (invoice_id,))
            invoice_data = cursor.fetchone()
            if not invoice_data:
                raise ValueError(f"Invoice {invoice_id} not found")

            # Get payment date from booking
            cursor.execute('SELECT DateBooking FROM Bookings WHERE ID = ?', (transaction_id,))
            booking = cursor.fetchone()
            payment_date = booking[0] if booking else None

            # Prevent duplicate links for the same booking
            cursor.execute(
                'SELECT ID FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                (invoice_id, transaction_id))
            if cursor.fetchone():
                raise ValueError(f"Booking {transaction_id} is already linked to invoice {invoice_id}")

            cursor.execute('''
                INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate)
                VALUES (?, ?, ?, ?)
            ''', (invoice_id, transaction_id, amount_paid, payment_date))

            # Recalculate AmountDue from all payments
            cursor.execute(
                'SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                (invoice_id,))
            total_paid = cursor.fetchone()[0]
            new_due = invoice_data[0] - total_paid

            cursor.execute('''
                UPDATE Invoices
                SET AmountDue = ?, UpdatedAt = CURRENT_TIMESTAMP
                WHERE ID = ?
            ''', (new_due, invoice_id))

            # Auto-update status
            if abs(new_due) < 0.01:
                new_status = 'paid'
            elif total_paid > 0:
                new_status = 'partial'
            else:
                new_status = None

            if new_status:
                cursor.execute('''
                    UPDATE Invoices SET Status = ?, UpdatedAt = CURRENT_TIMESTAMP WHERE ID = ?
                ''', (new_status, invoice_id))

            conn.commit()
            print(f"Invoice {invoice_id} linked to booking {transaction_id}, new due: {new_due:.2f}")

        except Exception as e:
            print(f"Error linking invoice to transaction: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_invoice_payments(self, invoice_id):
        """Return all payment entries for a given invoice.

        Returns list of tuples:
          (ID, InvoiceID, BookingID, Amount, PaymentDate, Notes, BookingReference)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ip.ID, ip.InvoiceID, ip.BookingID, ip.Amount, ip.PaymentDate, ip.Notes,
                   COALESCE(b.Reference, b.BookingText, '') AS BookingRef
            FROM InvoicePayments ip
            LEFT JOIN Bookings b ON b.ID = ip.BookingID
            WHERE ip.InvoiceID = ?
            ORDER BY ip.PaymentDate, ip.ID
        ''', (invoice_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def delete_invoice_payment(self, payment_id):
        """Remove an InvoicePayments entry and recalculate AmountDue on the invoice."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT InvoiceID, Amount FROM InvoicePayments WHERE ID = ?', (payment_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"InvoicePayment {payment_id} not found")
            invoice_id = row[0]

            cursor.execute('DELETE FROM InvoicePayments WHERE ID = ?', (payment_id,))

            # Recalculate AmountDue
            cursor.execute('SELECT SumGross FROM Invoices WHERE ID = ?', (invoice_id,))
            sum_gross = cursor.fetchone()[0]
            cursor.execute(
                'SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                (invoice_id,))
            total_paid = cursor.fetchone()[0]
            new_due = sum_gross - total_paid

            # Recalculate status
            if abs(new_due) < 0.01:
                new_status = 'paid'
            elif total_paid > 0:
                new_status = 'partial'
            else:
                new_status = 'finalized'

            cursor.execute('''
                UPDATE Invoices SET AmountDue = ?, Status = ?, UpdatedAt = CURRENT_TIMESTAMP
                WHERE ID = ?
            ''', (new_due, new_status, invoice_id))

            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_invoice_pdf_path(self, invoice_id, pdf_path):
        """Update PDFPath field for an invoice
        
        Args:
            invoice_id: ID of the invoice
            pdf_path: Path to the PDF file
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Invoices 
                SET PDFPath = ?,
                    UpdatedAt = CURRENT_TIMESTAMP
                WHERE ID = ?
            ''', (pdf_path, invoice_id))
            
            conn.commit()
        except Exception as e:
            print(f"Error updating invoice PDF path: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_overdue_invoices(self):
        """Get all overdue invoices (sent status, past due date, amount due > 0)
        
        Returns:
            List of invoice tuples with overdue invoices
        """
        from datetime import date
        today = date.today().isoformat()
        
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM Invoices 
            WHERE Status IN ('finalized', 'sent') 
            AND PaymentDueDate < ?
            AND (AmountDue IS NULL OR AmountDue > 0.01)
            ORDER BY PaymentDueDate ASC
        ''', (today,))
        invoices = cursor.fetchall()
        conn.close()
        return invoices
    
    def get_invoices_due_soon(self, days=7):
        """Get invoices due within the next N days
        
        Args:
            days: Number of days to look ahead
            
        Returns:
            List of invoice tuples
        """
        from datetime import date, timedelta
        today = date.today()
        future_date = (today + timedelta(days=days)).isoformat()
        today_str = today.isoformat()
        
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM Invoices 
            WHERE Status IN ('finalized', 'sent') 
            AND PaymentDueDate BETWEEN ? AND ?
            AND (AmountDue IS NULL OR AmountDue > 0.01)
            ORDER BY PaymentDueDate ASC
        ''', (today_str, future_date))
        invoices = cursor.fetchall()
        conn.close()
        return invoices

    # ── Dashboard helpers ─────────────────────────────────────────────

    def get_dashboard_monthly(self, date_from: str, date_to: str,
                              account_ids: list | None = None):
        """Monthly 3-way split of bookings for the dashboard.

        Includes both bank-type bookings (for bank accounts) and standalone
        entry-type bookings (for cash accounts like Kasse).

        Categories:
          - Einnahmen: bookings with Amount > 0, excluding private
          - Privatentnahmen: all bookings (positive + negative) associated
            with COA AccountNumber 2100-2199 (netted: Einlagen vs Entnahmen)
          - Betriebsausgaben: negative bookings not associated with private COA

        Args:
            date_from:   Start date  (YYYY-MM-DD)
            date_to:     End date    (YYYY-MM-DD)
            account_ids: Optional list of Account IDs to include.
                         None = all accounts.

        Returns:
            dict  keys = month int 1-12, values = dict with
            'income', 'private', 'expense' (all float, expense <= 0).
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # ── Determine bank account IDs and cash COA IDs ───────────────
        if account_ids:
            ph = ','.join('?' * len(account_ids))
            cursor.execute(
                f"SELECT ID, SKRAccount, IsCash FROM Accounts WHERE ID IN ({ph})",
                account_ids)
        else:
            cursor.execute("SELECT ID, SKRAccount, IsCash FROM Accounts")
        accounts = cursor.fetchall()

        bank_acct_ids = [a[0] for a in accounts if not a[2]]
        cash_skr = [a[1] for a in accounts if a[2]]
        cash_coa_ids = []
        if cash_skr:
            cursor.execute(
                f"SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN "
                f"({','.join('?' * len(cash_skr))})", cash_skr)
            cash_coa_ids = [r[0] for r in cursor.fetchall()]

        # ── Build booking-type filter ─────────────────────────────────
        type_parts = []
        type_params: list = []
        if bank_acct_ids:
            ph = ','.join('?' * len(bank_acct_ids))
            type_parts.append(
                f"(b.BookingType='bank' AND b.Account_ID IN ({ph}))")
            type_params += bank_acct_ids
        if cash_coa_ids:
            ph = ','.join('?' * len(cash_coa_ids))
            type_parts.append(
                f"(b.BookingType='entry' AND b.ParentBooking_ID IS NULL "
                f"AND (b.COA_ID IN ({ph}) OR b.CounterCOA_ID IN ({ph})))")
            type_params += cash_coa_ids * 2

        if not type_parts:
            conn.close()
            return {m: {'income': 0, 'private': 0, 'expense': 0}
                    for m in range(1, 13)}

        acct_filter = ' AND (' + ' OR '.join(type_parts) + ')'

        # ── Privatentnahmen condition ─────────────────────────────────
        # Bank bookings: child entry has COA or CounterCOA 2100-2199
        # Entry bookings: own COA or CounterCOA is 2100-2199
        private_cond = '''
            AND (
                (b.BookingType='bank' AND EXISTS (
                    SELECT 1 FROM Bookings e
                    JOIN ChartOfAccounts c
                      ON (c.ID = e.COA_ID OR c.ID = e.CounterCOA_ID)
                    WHERE e.ParentBooking_ID = b.ID
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
                OR
                (b.BookingType='entry' AND EXISTS (
                    SELECT 1 FROM ChartOfAccounts c
                    WHERE (c.ID = b.COA_ID OR c.ID = b.CounterCOA_ID)
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
            )'''
        not_private_cond = '''
            AND NOT (
                (b.BookingType='bank' AND EXISTS (
                    SELECT 1 FROM Bookings e
                    JOIN ChartOfAccounts c
                      ON (c.ID = e.COA_ID OR c.ID = e.CounterCOA_ID)
                    WHERE e.ParentBooking_ID = b.ID
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
                OR
                (b.BookingType='entry' AND EXISTS (
                    SELECT 1 FROM ChartOfAccounts c
                    WHERE (c.ID = b.COA_ID OR c.ID = b.CounterCOA_ID)
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
            )'''

        p_base = [date_from, date_to] + type_params

        income_rows = cursor.execute(f'''
            SELECT CAST(strftime('%m', b.DateBooking) AS INTEGER),
                   COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              AND b.Amount > 0
              {acct_filter}
              {not_private_cond}
            GROUP BY strftime('%m', b.DateBooking)
        ''', p_base).fetchall()

        private_rows = cursor.execute(f'''
            SELECT CAST(strftime('%m', b.DateBooking) AS INTEGER),
                   COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              {acct_filter}
              {private_cond}
            GROUP BY strftime('%m', b.DateBooking)
        ''', p_base).fetchall()

        expense_rows = cursor.execute(f'''
            SELECT CAST(strftime('%m', b.DateBooking) AS INTEGER),
                   COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              AND b.Amount < 0
              {acct_filter}
              {not_private_cond}
            GROUP BY strftime('%m', b.DateBooking)
        ''', p_base).fetchall()

        income_map  = {row[0]: row[1] for row in income_rows}
        private_map = {row[0]: row[1] for row in private_rows}
        expense_map = {row[0]: row[1] for row in expense_rows}

        result = {
            month: {
                'income':  round(income_map.get(month, 0), 2),
                'private': round(private_map.get(month, 0), 2),
                'expense': round(expense_map.get(month, 0), 2),
            }
            for month in range(1, 13)
        }

        conn.close()
        return result

    def get_dashboard_totals(self, date_from: str, date_to: str,
                             account_ids: list | None = None):
        """Aggregate totals for the dashboard metric cards.

        Includes both bank-type bookings (for bank accounts) and standalone
        entry-type bookings (for cash accounts like Kasse).

        Returns dict with:
          income, private, expense, balance,
          bank_count, unlinked_count
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # ── Determine bank account IDs and cash COA IDs ───────────────
        if account_ids:
            ph = ','.join('?' * len(account_ids))
            cursor.execute(
                f"SELECT ID, SKRAccount, IsCash FROM Accounts WHERE ID IN ({ph})",
                account_ids)
        else:
            cursor.execute("SELECT ID, SKRAccount, IsCash FROM Accounts")
        accounts = cursor.fetchall()

        bank_acct_ids = [a[0] for a in accounts if not a[2]]
        cash_skr = [a[1] for a in accounts if a[2]]
        cash_coa_ids = []
        if cash_skr:
            cursor.execute(
                f"SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN "
                f"({','.join('?' * len(cash_skr))})", cash_skr)
            cash_coa_ids = [r[0] for r in cursor.fetchall()]

        # ── Build booking-type filter ─────────────────────────────────
        type_parts = []
        type_params: list = []
        if bank_acct_ids:
            ph = ','.join('?' * len(bank_acct_ids))
            type_parts.append(
                f"(b.BookingType='bank' AND b.Account_ID IN ({ph}))")
            type_params += bank_acct_ids
        if cash_coa_ids:
            ph = ','.join('?' * len(cash_coa_ids))
            type_parts.append(
                f"(b.BookingType='entry' AND b.ParentBooking_ID IS NULL "
                f"AND (b.COA_ID IN ({ph}) OR b.CounterCOA_ID IN ({ph})))")
            type_params += cash_coa_ids * 2

        if not type_parts:
            conn.close()
            return {'income': 0, 'private': 0, 'expense': 0,
                    'balance': 0, 'bank_count': 0, 'unlinked_count': 0}

        acct_filter = ' AND (' + ' OR '.join(type_parts) + ')'
        params = [date_from, date_to] + type_params

        # ── Privatentnahmen condition ─────────────────────────────────
        private_cond = '''
            AND (
                (b.BookingType='bank' AND EXISTS (
                    SELECT 1 FROM Bookings e
                    JOIN ChartOfAccounts c
                      ON (c.ID = e.COA_ID OR c.ID = e.CounterCOA_ID)
                    WHERE e.ParentBooking_ID = b.ID
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
                OR
                (b.BookingType='entry' AND EXISTS (
                    SELECT 1 FROM ChartOfAccounts c
                    WHERE (c.ID = b.COA_ID OR c.ID = b.CounterCOA_ID)
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
            )'''
        not_private_cond = '''
            AND NOT (
                (b.BookingType='bank' AND EXISTS (
                    SELECT 1 FROM Bookings e
                    JOIN ChartOfAccounts c
                      ON (c.ID = e.COA_ID OR c.ID = e.CounterCOA_ID)
                    WHERE e.ParentBooking_ID = b.ID
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
                OR
                (b.BookingType='entry' AND EXISTS (
                    SELECT 1 FROM ChartOfAccounts c
                    WHERE (c.ID = b.COA_ID OR c.ID = b.CounterCOA_ID)
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
            )'''

        income = cursor.execute(f'''
            SELECT COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              AND b.Amount > 0 {acct_filter}
              {not_private_cond}
        ''', params).fetchone()[0]

        private = cursor.execute(f'''
            SELECT COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              {acct_filter}
              {private_cond}
        ''', params).fetchone()[0]

        expense = cursor.execute(f'''
            SELECT COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              AND b.Amount < 0 {acct_filter}
              {not_private_cond}
        ''', params).fetchone()[0]

        bank_count = cursor.execute(f'''
            SELECT COUNT(*) FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ? {acct_filter}
        ''', params).fetchone()[0]

        # Unlinked = bank bookings without child entries
        unlinked_params = [date_from, date_to]
        unlinked_filter = ''
        if bank_acct_ids:
            ph = ','.join('?' * len(bank_acct_ids))
            unlinked_filter = f' AND b.Account_ID IN ({ph})'
            unlinked_params += bank_acct_ids
        unlinked = cursor.execute(f'''
            SELECT COUNT(*) FROM Bookings b
            WHERE b.BookingType = 'bank'
              AND b.DateBooking BETWEEN ? AND ? {unlinked_filter}
              AND NOT EXISTS (
                  SELECT 1 FROM Bookings e
                  WHERE e.ParentBooking_ID = b.ID)
        ''', unlinked_params).fetchone()[0]

        conn.close()
        return {
            'income':         round(income, 2),
            'private':        round(private, 2),
            'expense':        round(expense, 2),
            'balance':        round(income + private + expense, 2),
            'bank_count':     bank_count,
            'unlinked_count': unlinked,
        }

    # ── EÜR ───────────────────────────────────────────────────────────

    def get_euer_data(self, date_from: str, date_to: str,
                      account_ids: list | None = None) -> list:
        """EÜR-Auswertung: Saldo pro SKR-Konto im Zeitraum.

        Für jede Entry-Buchung wird das „Zweck-Konto" ermittelt:
        - Wenn COA_ID ein liquides Konto (Bank/Kasse) ist → CounterCOA_ID
        - Sonst → COA_ID

        Rein liquide Spiegelbuchungen (Doppik) werden ignoriert.

        Returns:
            list of (AccountNumber, Name, total_amount)
            sortiert nach AccountNumber, nur Konten mit Saldo ≠ 0.
        """
        from collections import defaultdict

        conn = self._get_connection()
        cursor = conn.cursor()

        # Liquide Konten: Bankkonten (aus Accounts-Tabelle) + Kassenkonten
        bank_coa_ids = self._get_bank_coa_ids(cursor)

        if account_ids:
            ph = ','.join('?' * len(account_ids))
            cursor.execute(
                f"SELECT ID, SKRAccount, IsCash FROM Accounts "
                f"WHERE ID IN ({ph})", account_ids)
        else:
            cursor.execute("SELECT ID, SKRAccount, IsCash FROM Accounts")
        accts = cursor.fetchall()

        bank_acct_ids = [a[0] for a in accts if not a[2]]
        cash_skr = [a[1] for a in accts if a[2]]
        cash_coa_ids: set[int] = set()
        if cash_skr:
            ph = ','.join('?' * len(cash_skr))
            cursor.execute(
                f"SELECT ID FROM ChartOfAccounts "
                f"WHERE AccountNumber IN ({ph})", cash_skr)
            cash_coa_ids = {r[0] for r in cursor.fetchall()}

        liquid_coa_ids = bank_coa_ids | cash_coa_ids

        # ── 1. Entry-Kinder von Bank-Buchungen ───────────────────────
        entries: list[tuple] = []
        if bank_acct_ids:
            ph = ','.join('?' * len(bank_acct_ids))
            cursor.execute(f"""
              SELECT e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0)
                FROM Bookings e
                JOIN Bookings p ON p.ID = e.ParentBooking_ID
                WHERE e.BookingType = 'entry'
                  AND p.Account_ID IN ({ph})
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
            """, bank_acct_ids + [date_from, date_to])
            entries.extend(cursor.fetchall())

        # ── 2. Standalone Kassen-Buchungen ────────────────────────────
        if cash_coa_ids:
            coa_list = list(cash_coa_ids)
            ph = ','.join('?' * len(coa_list))
            cursor.execute(f"""
              SELECT e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0)
                FROM Bookings e
                WHERE e.BookingType = 'entry'
                  AND e.ParentBooking_ID IS NULL
                  AND (e.COA_ID IN ({ph}) OR e.CounterCOA_ID IN ({ph}))
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
            """, coa_list + coa_list + [date_from, date_to])
            entries.extend(cursor.fetchall())

        # ── Einnahmen-COA-IDs ermitteln (für USt-Zuordnung auf 3806) ──
        cursor.execute("""
            SELECT ID FROM ChartOfAccounts
            WHERE AccountNumber IN (4400, 4640, 4845)
        """)
        income_coa_ids = {r[0] for r in cursor.fetchall()}

        cursor.execute("""
            SELECT ID, AccountNumber FROM ChartOfAccounts
            WHERE AccountNumber IN (1401, 1406)
        """)
        input_tax_coa_ids = {acct_nr: coa_id for coa_id, acct_nr in cursor.fetchall()}

        # ── Zweck-Konto bestimmen und aggregieren ─────────────────────
        # Netto-Beträge pro Konto + virtuelle USt-Zeile (3806)
        totals: dict[int, float] = defaultdict(float)
        ust_total: float = 0.0   # nur USt aus Einnahmen → 3806
        for amount, coa_id, counter_coa_id, tax_amount, tax_rate in entries:
            netto = amount - tax_amount  # Brutto → Netto

            purpose_coa_id = None

            # Doppik-Spiegel überspringen (beide liquid)
            if coa_id in liquid_coa_ids and (
                    counter_coa_id is None
                    or counter_coa_id in liquid_coa_ids):
                continue

            if coa_id in liquid_coa_ids:
                # Cash-flow: liquides Konto → Zweck = CounterCOA
                if counter_coa_id:
                    purpose_coa_id = counter_coa_id
                    totals[counter_coa_id] += netto
                    if counter_coa_id in income_coa_ids:
                        ust_total += tax_amount
            elif counter_coa_id and counter_coa_id in liquid_coa_ids:
                # Cash-flow: Zweck = COA, Gegenstück ist liquid
                purpose_coa_id = coa_id
                totals[coa_id] += netto
                if coa_id in income_coa_ids:
                    ust_total += tax_amount
            else:
                # Umbuchung (keine Seite liquid, z.B. 4405→4400):
                # Betrag auf beide Konten verteilen, damit z.B.
                # Erlöse unter 4400 erscheinen und 4405 reduziert wird.
                if counter_coa_id:
                    purpose_coa_id = counter_coa_id
                    totals[counter_coa_id] += netto
                    if counter_coa_id in income_coa_ids:
                        ust_total += tax_amount
                if coa_id:
                    totals[coa_id] -= netto

            # Vorsteuer aus Ausgaben separat auf 1401/1406 ausweisen.
            if (purpose_coa_id is not None
                    and purpose_coa_id not in income_coa_ids
                    and tax_amount != 0):
                input_tax_account = None
                if abs(tax_rate - 0.07) < 0.0001:
                    input_tax_account = 1401
                elif abs(tax_rate - 0.19) < 0.0001:
                    input_tax_account = 1406
                if input_tax_account in input_tax_coa_ids:
                    totals[input_tax_coa_ids[input_tax_account]] += tax_amount

        # ── COA-Details laden ─────────────────────────────────────────
        result: list[tuple] = []
        if totals:
            ph = ','.join('?' * len(totals))
            cursor.execute(f"""
                SELECT ID, AccountNumber, Name FROM ChartOfAccounts
                WHERE ID IN ({ph})
            """, list(totals.keys()))
            for coa_id, acct_nr, name in cursor.fetchall():
                total = round(totals[coa_id], 2)
                if abs(total) >= 0.01:
                    result.append((acct_nr, name, total))

        # ── Virtuelles Konto 3806 (Umsatzsteuer) ─────────────────────
        ust_total = round(ust_total, 2)
        if abs(ust_total) >= 0.01:
            # Name aus DB holen, falls vorhanden
            cursor.execute(
                "SELECT Name FROM ChartOfAccounts WHERE AccountNumber = 3806"
            )
            row_3806 = cursor.fetchone()
            name_3806 = row_3806[0] if row_3806 else 'Umsatzsteuer 19%'
            result.append((3806, name_3806, ust_total))

        conn.close()
        result.sort(key=lambda x: x[0])
        return result