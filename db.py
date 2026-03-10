import sqlite3
import os

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
                Category_ID INTEGER,
                Amount REAL NOT NULL,
                Currency TEXT DEFAULT 'EUR',
                TaxRate REAL,
                TaxAmount REAL,
                Text TEXT,
                DocumentNumber TEXT,
                FOREIGN KEY (BookingGroup_ID) REFERENCES BookingGroups(ID),
                FOREIGN KEY (Account_ID) REFERENCES Accounts(ID),
                FOREIGN KEY (Contact_ID) REFERENCES Contacts(ID),
                FOREIGN KEY (COA_ID) REFERENCES ChartOfAccounts(ID),
                FOREIGN KEY (Category_ID) REFERENCES Categories(ID)
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
        # Prefix: optional prefix for subdivision (e.g., '_A', '_B')
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

        conn.commit()
        conn.close()

        # Seed default AssetCategories if empty
        self._seed_asset_categories()
    
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

    def insert_booking(self, date_booking, amount, account_id=None, foreign_bank_account="", 
                       recipient_client="", contact_id=None, coa_id=None, category_id=None,
                       currency="EUR", tax_rate=None, tax_amount=None, text="", 
                       document_number=None, date_tax=None, booking_group_id=None, log_description=None):
        """Insert a new booking into Bookings table
        
        Args:
            date_booking: Transaction date (required)
            amount: Amount (positive = credit/Haben, negative = debit/Soll)
            account_id: FK to Accounts table
            foreign_bank_account: External IBAN/account number
            recipient_client: Name of recipient/client
            contact_id: FK to Contacts table
            coa_id: FK to ChartOfAccounts (SKR)
            category_id: FK to Categories
            currency: Currency code (default: EUR)
            tax_rate: Tax rate as decimal (e.g., 0.19 for 19%)
            tax_amount: Calculated tax amount
            text: Notes/purpose
            document_number: External document reference
            date_tax: Tax date (optional)
            booking_group_id: FK to BookingGroups (for split bookings)
            log_description: Description for SQL logging (optional)
        
        Returns:
            int: ID of inserted booking
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''INSERT INTO Bookings 
            (DateBooking, DateTax, BookingGroup_ID, Account_ID, ForeignBankAccount, 
             RecipientClient, Contact_ID, COA_ID, Category_ID, Amount, Currency, 
             TaxRate, TaxAmount, Text, DocumentNumber)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
        
        params = (date_booking, date_tax, booking_group_id, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, category_id, amount, currency,
                  tax_rate, tax_amount, text, document_number)
        
        cursor.execute(sql_template, params)
        conn.commit()
        last_id = cursor.lastrowid
        conn.close()
        
        # Optional SQL logging
        if log_description:
            self._log_sql(sql_template, params, log_description)
        
        return last_id
    
    def check_booking_exists(self, date, amount, account_id=None, foreign_bank_account="", text=""):
        """Check if a booking with same parameters already exists
        
        Args:
            date: Booking date
            amount: Amount
            account_id: Account ID
            foreign_bank_account: Foreign bank account
            text: Text/notes
            
        Returns:
            bool: True if duplicate exists, False otherwise
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM Bookings
            WHERE DateBooking=? AND Amount=? AND Account_ID=? AND ForeignBankAccount=? AND Text=?
        ''', (date, amount, account_id, foreign_bank_account, text))
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    
    def update_booking(self, booking_id, date_booking, amount, account_id=None, 
                       foreign_bank_account="", recipient_client="", contact_id=None, 
                       coa_id=None, category_id=None, currency="EUR", tax_rate=None, 
                       tax_amount=None, text="", document_number=None, 
                       date_tax=None, booking_group_id=None, log_description=None):
        """Update an existing booking
        
        Args:
            booking_id: ID of booking to update
            [same parameters as insert_booking]
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''UPDATE Bookings
            SET DateBooking=?, DateTax=?, BookingGroup_ID=?, Account_ID=?, ForeignBankAccount=?,
                RecipientClient=?, contact_id=?, COA_ID=?, Category_ID=?, Amount=?, Currency=?,
                TaxRate=?, TaxAmount=?, Text=?, DocumentNumber=?
            WHERE ID=?'''
        
        params = (date_booking, date_tax, booking_group_id, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, category_id, amount, currency,
                  tax_rate, tax_amount, text, document_number, booking_id)
        
        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()
        
        # Optional SQL logging
        if log_description:
            self._log_sql(sql_template, params, log_description)
    
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
        cursor.execute('SELECT * FROM Bookings WHERE BookingGroup_ID=?', (group_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows

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

    def insert_chart_of_accounts(self, framework, account_number, name, description, is_standard=0):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard)
                VALUES (?, ?, ?, ?, ?)
            ''', (framework, account_number, name, description, is_standard))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting into ChartOfAccounts:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_chart_of_accounts(self, id, framework, account_number, name, description):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE ChartOfAccounts
                SET Framework = ?, AccountNumber = ?, Name = ?, Description = ?
                WHERE ID = ? AND IsStandard = 0
            ''', (framework, account_number, name, description, id))
            conn.commit()
            if cursor.rowcount == 0:
                print("Warning: Cannot update standard account or account not found")
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
        """Seed default AfA categories from BMF table if empty"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM AssetCategories')
        count = cursor.fetchone()[0]
        if count == 0:
            categories = [
                # IT & Elektronik
                ("EDV-Hardware (PC, Notebook, Server)", 3, "both", None, "BMF AV-Tabelle: 3 Jahre"),
                ("Drucker, Scanner, Kopierer", 3, "both", None, "BMF AV-Tabelle: 3 Jahre"),
                ("Tablets und Smartphones", 3, "both", None, "BMF AV-Tabelle: 3 Jahre"),
                ("Bildschirme / Monitore", 3, "both", None, "BMF AV-Tabelle: 3 Jahre"),
                ("Netzwerktechnik (Router, Switch)", 3, "both", None, "BMF AV-Tabelle: 3 Jahre"),
                ("Software (ERP, kaufm. Software)", 3, "linear", None, "BMF AV-Tabelle: 3 Jahre, nur linear"),
                ("Software (sonstige)", 3, "linear", None, "BMF AV-Tabelle: 3 Jahre, nur linear"),
                # Büro & Einrichtung
                ("Büromöbel (Schreibtisch, Regal)", 13, "both", None, "BMF AV-Tabelle: 13 Jahre"),
                ("Bürostühle", 13, "both", None, "BMF AV-Tabelle: 13 Jahre"),
                ("Aktenschränke, Tresore", 10, "both", None, "BMF AV-Tabelle: 10 Jahre"),
                ("Beleuchtung", 10, "both", None, "BMF AV-Tabelle: 10 Jahre"),
                # Kommunikation
                ("Telefonanlage", 10, "both", None, "BMF AV-Tabelle: 10 Jahre"),
                ("Fax, Anrufbeantworter", 5, "both", None, "BMF AV-Tabelle: 5 Jahre"),
                # Fahrzeuge
                ("PKW (Personenkraftwagen)", 6, "both", None, "BMF AV-Tabelle: 6 Jahre"),
                ("LKW (bis 3,5t)", 9, "both", None, "BMF AV-Tabelle: 9 Jahre"),
                ("LKW (über 3,5t)", 9, "both", None, "BMF AV-Tabelle: 9 Jahre"),
                ("Anhänger", 10, "both", None, "BMF AV-Tabelle: 10 Jahre"),
                ("Motorrad / Motorroller", 7, "both", None, "BMF AV-Tabelle: 7 Jahre"),
                ("Fahrrad / E-Bike (betrieblich)", 7, "both", None, "BMF AV-Tabelle: 7 Jahre"),
                # Maschinen & Geräte
                ("Maschinen (allgemein)", 13, "both", None, "BMF AV-Tabelle: 13 Jahre"),
                ("Werkzeug (elektrisch)", 8, "both", None, "BMF AV-Tabelle: 8 Jahre"),
                ("Messgeräte, Laborgeräte", 5, "both", None, "BMF AV-Tabelle: 5 Jahre"),
                ("Produktionsanlagen", 15, "both", None, "BMF AV-Tabelle: 15 Jahre"),
                # Gebäude & Ausstattung
                ("Ladeneinrichtung", 10, "both", None, "BMF AV-Tabelle: 10 Jahre"),
                ("Klimaanlagen", 15, "both", None, "BMF AV-Tabelle: 15 Jahre"),
                ("Solaranlage (Photovoltaik)", 20, "both", None, "BMF AV-Tabelle: 20 Jahre"),
                # Sonstiges
                ("Kamera, Foto-/Videoequipment", 7, "both", None, "BMF AV-Tabelle: 7 Jahre"),
                ("Musikinstrumente (betrieblich)", 10, "both", None, "BMF AV-Tabelle: 10 Jahre"),
                ("Werbeanlagen, Schilder", 10, "both", None, "BMF AV-Tabelle: 10 Jahre"),
                ("Sonstige Wirtschaftsgüter", 10, "both", None, "Eigener Eintrag – bitte Nutzungsdauer prüfen"),
            ]
            for cat in categories:
                cursor.execute('''
                    INSERT INTO AssetCategories (Name, UsefulLifeYears, DepreciationMethod, COA_ID, Notes)
                    VALUES (?, ?, ?, ?, ?)
                ''', cat)
            conn.commit()
        conn.close()

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
        if status:
            if parent_only:
                cursor.execute('''
                    SELECT a.*, ac.Name as CategoryName, c.DisplayName as SupplierName
                    FROM Assets a
                    LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
                    LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
                    WHERE a.Status = ? AND a.Parent_ID IS NULL
                    ORDER BY a.PurchaseDate DESC
                ''', (status,))
            else:
                cursor.execute('''
                    SELECT a.*, ac.Name as CategoryName, c.DisplayName as SupplierName
                    FROM Assets a
                    LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
                    LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
                    WHERE a.Status = ?
                    ORDER BY a.PurchaseDate DESC
                ''', (status,))
        else:
            if parent_only:
                cursor.execute('''
                    SELECT a.*, ac.Name as CategoryName, c.DisplayName as SupplierName
                    FROM Assets a
                    LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
                    LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
                    WHERE a.Parent_ID IS NULL
                    ORDER BY a.PurchaseDate DESC
                ''')
            else:
                cursor.execute('''
                    SELECT a.*, ac.Name as CategoryName, c.DisplayName as SupplierName
                    FROM Assets a
                    LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
                    LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
                    ORDER BY a.PurchaseDate DESC
                ''')
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
            VALUES (?, ?, ?, ?, ?, 'EUR', ?, 'expense', 'posted')
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

    def insert_account(self, name, holder, number, bic, bank_name, is_cash=0):
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                INSERT INTO Accounts (Name, Owner, Number, BIC, BankName, IsCash)
                VALUES (?, ?, ?, ?, ?, ?)'''
        params = (name, holder, number, bic, bank_name, is_cash)
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

    def update_account(self, account_id, name, holder, number, bic, bank_name):
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                UPDATE Accounts
                SET Name = ?, Owner = ?, Number = ?, BIC = ?, BankName = ?
                WHERE ID = ? AND IsCash = 0'''
        params = (name, holder, number, bic, bank_name, account_id)
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
                for row in rows:
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
                    f.write(f"INSERT INTO [{table}] ({cols_sql}) VALUES ({vals_sql});\n")
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
                    (SELECT cd2.CompanyName FROM CompanyDetails cd2
                     WHERE cd2.ContactID = pd.CompanyContactID),
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

    def insert_number_range(self, range_type, year, letter, prefix='', current_number=0, description=''):
        """Insert a new number range
        
        Args:
            range_type: 'invoice', 'receipt_company', or 'receipt_category'
            year: 4-digit year (will be stored as-is, displayed as 2-digit)
            letter: Single letter identifier (e.g., 'R' for Rechnung)
            prefix: Optional prefix for subdivision (e.g., '_A')
            current_number: Starting number (default: 0)
            description: Optional description
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO NumberRanges (Type, Year, Letter, Prefix, CurrentNumber, Description)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (range_type, year, letter, prefix, current_number, description))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting number range:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_number_range(self, range_id, year, letter, prefix='', current_number=0, description=''):
        """Update existing number range"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE NumberRanges
                SET Year = ?, Letter = ?, Prefix = ?, CurrentNumber = ?, Description = ?
                WHERE ID = ?
            ''', (year, letter, prefix, current_number, description, range_id))
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
        
        Returns the full formatted number string (e.g., '26R001' or '26R_A001')
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Try to get existing range
        cursor.execute('''
            SELECT ID, CurrentNumber FROM NumberRanges 
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()
        
        if row:
            range_id, current = row
            next_num = current + 1
            cursor.execute('UPDATE NumberRanges SET CurrentNumber = ? WHERE ID = ?', (next_num, range_id))
        else:
            # Create new range for this combination
            next_num = 1
            cursor.execute('''
                INSERT INTO NumberRanges (Type, Year, Letter, Prefix, CurrentNumber, Description)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (range_type, year, letter, prefix, next_num, ''))
        
        conn.commit()
        conn.close()
        
        # Format: YY[Letter]###[Prefix] (e.g., 26R001 or 26R001_A)
        year_short = str(year)[-2:]
        return f"{year_short}{letter}{next_num:03d}{prefix}"

    def format_number(self, year, letter, prefix, number):
        """Format a number according to the pattern YY[Letter]###[Prefix]"""
        year_short = str(year)[-2:]
        return f"{year_short}{letter}{number:03d}{prefix}"

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
            SELECT CurrentNumber FROM NumberRanges 
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()
        conn.close()
        
        current = row[0] if row else 0
        next_num = current + 1
        year_short = str(year)[-2:]
        
        return {
            'current_number': current,
            'next_number': next_num,
            'formatted_next': f"{year_short}{letter}{prefix}{next_num:03d}"
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
        """Link an invoice to a payment transaction and update AmountDue
        
        Args:
            invoice_id: ID of the invoice
            transaction_id: ID of the transaction (payment)
            amount_paid: Amount paid in this transaction
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            # Get current invoice data
            cursor.execute('SELECT AmountDue, SumGross FROM Invoices WHERE ID = ?', (invoice_id,))
            invoice_data = cursor.fetchone()
            if not invoice_data:
                raise ValueError(f"Invoice {invoice_id} not found")
            
            current_due = invoice_data[0] or invoice_data[1]  # Use SumGross if AmountDue is None
            new_due = current_due - amount_paid
            
            # Update AmountDue
            cursor.execute('''
                UPDATE Invoices 
                SET AmountDue = ?,
                    UpdatedAt = CURRENT_TIMESTAMP
                WHERE ID = ?
            ''', (new_due, invoice_id))
            
            # Update transaction to reference invoice (if Notes field exists)
            cursor.execute('''
                UPDATE Transactions 
                SET Info = 'Rechnung: ' || (SELECT InvoiceNumber FROM Invoices WHERE ID = ?)
                WHERE ID = ?
            ''', (invoice_id, transaction_id))
            
            # If fully paid, update status
            if abs(new_due) < 0.01:  # Allow for rounding errors
                cursor.execute('''
                    UPDATE Invoices 
                    SET Status = 'paid',
                        UpdatedAt = CURRENT_TIMESTAMP
                    WHERE ID = ?
                ''', (invoice_id,))
            
            conn.commit()
            print(f"Invoice {invoice_id} linked to transaction {transaction_id}, new due: {new_due:.2f}")
            
        except Exception as e:
            print(f"Error linking invoice to transaction: {e}")
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
