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

        # Contacts table for managing customers, suppliers, own data, and other contacts
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Contacts (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                ContactType TEXT DEFAULT 'customer',
                CustomerNumber TEXT,
                Name TEXT NOT NULL,
                Company TEXT,
                Street TEXT,
                PostalCode TEXT,
                City TEXT,
                Country TEXT,
                Email TEXT,
                Phone TEXT,
                TaxID TEXT,
                Notes TEXT,
                Logo TEXT,
                UNIQUE(CustomerNumber)
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

        conn.commit()
        conn.close()
        
        # Run migrations
        self._migrate_contacts_logo_column()
        
        # Ensure the default "Kasse" account exists
        self.ensure_kasse_exists()
    
    def _migrate_contacts_logo_column(self):
        """Add Logo column to Contacts table if it doesn't exist"""
        conn = self._get_connection()
        cursor = conn.cursor()
        # Check if Logo column exists
        cursor.execute("PRAGMA table_info(Contacts)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'Logo' not in columns:
            try:
                cursor.execute('ALTER TABLE Contacts ADD COLUMN Logo TEXT')
                conn.commit()
                print("Migration: Added Logo column to Contacts table")
            except sqlite3.OperationalError as e:
                print(f"Migration warning: {e}")
        conn.close()
        
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

    # Initialize the database with some content
    def init_content(self):
        self.insert_receipt("12F123", "2012-05-01", "testBeleg01.pdf", "./2012/", "Testbeleg")
        self.insert_receipt("12F124", "2012-05-02", "testBeleg02.pdf", "./2012/", "noch ein Testbeleg")
        self.insert_receipt("12F125", "2012-05-03", "testBeleg03.pdf", "./2012/", "und noch einer")
        self.insert_receipt("12F126", "2012-05-04", "testBeleg04.pdf", "./2012/", "")

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

    # Table Contacts (replaces Customers)
    def fetch_contacts(self, contact_type=None):
        """Fetch all contacts, optionally filtered by type
        
        Args:
            contact_type: Filter by type ('customer', 'supplier', 'insurance', 'own', 'other')
                         If None, returns all contacts
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        if contact_type:
            cursor.execute('SELECT * FROM Contacts WHERE ContactType = ? ORDER BY Name ASC', (contact_type,))
        else:
            cursor.execute('SELECT * FROM Contacts ORDER BY Name ASC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_contact_by_id(self, contact_id):
        """Get contact by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Contacts WHERE ID = ?', (contact_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def insert_contact(self, name, contact_type="customer", customer_number="", company="", street="", postal_code="", city="", country="", email="", phone="", tax_id="", notes=""):
        """Insert new contact
        
        Args:
            name: Name (required)
            contact_type: Type of contact ('customer', 'supplier', 'insurance', 'own', 'other')
            customer_number: Optional customer number (for customers)
            [other contact fields]
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                INSERT INTO Contacts (ContactType, CustomerNumber, Name, Company, Street, PostalCode, City, Country, Email, Phone, TaxID, Notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
        params = (contact_type, customer_number, name, company, street, postal_code, city, country, email, phone, tax_id, notes)
        try:
            cursor.execute(sql_template, params)
            conn.commit()
            # Log SQL after successful commit
            self._log_sql(sql_template, params, "Insert new contact")
        except sqlite3.IntegrityError as e:
            print("Error inserting contact:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_contact(self, contact_id, name, contact_type="customer", customer_number="", company="", street="", postal_code="", city="", country="", email="", phone="", tax_id="", notes="", logo=""):
        """Update existing contact"""
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                UPDATE Contacts
                SET ContactType = ?, CustomerNumber = ?, Name = ?, Company = ?, Street = ?, PostalCode = ?, City = ?, Country = ?, Email = ?, Phone = ?, TaxID = ?, Notes = ?, Logo = ?
                WHERE ID = ?'''
        params = (contact_type, customer_number, name, company, street, postal_code, city, country, email, phone, tax_id, notes, logo, contact_id)
        try:
            cursor.execute(sql_template, params)
            conn.commit()
            # Log SQL after successful commit
            self._log_sql(sql_template, params, "Update contact")
        except sqlite3.IntegrityError as e:
            print("Error updating contact:", e)
            conn.rollback()
        finally:
            conn.close()

    def delete_contact(self, contact_id):
        """Delete contact"""
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
        try:
            cursor.execute('''
                INSERT INTO Articles (Name, Unit, UnitPrice, TaxRate, Description, Active)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, unit, unit_price, tax_rate, description, active))
            conn.commit()
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
            cursor.execute('''
                UPDATE Articles
                SET Name = ?, Unit = ?, UnitPrice = ?, TaxRate = ?, Description = ?, Active = ?
                WHERE ID = ?
            ''', (name, unit, unit_price, tax_rate, description, active, article_id))
            conn.commit()
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
