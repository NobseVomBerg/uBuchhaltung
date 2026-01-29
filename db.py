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
                Customer_ID INTEGER,
                COA_ID INTEGER,
                Category_ID INTEGER,
                Amount REAL NOT NULL,
                Currency TEXT DEFAULT 'EUR',
                TaxRate REAL,
                TaxAmount REAL,
                Text TEXT,
                DocumentNumber TEXT,
                BookingType TEXT,
                Status TEXT DEFAULT 'posted',
                FOREIGN KEY (BookingGroup_ID) REFERENCES BookingGroups(ID),
                FOREIGN KEY (Account_ID) REFERENCES Accounts(ID),
                FOREIGN KEY (Customer_ID) REFERENCES Contacts(ID),
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

        conn.commit()
        conn.close()
        
        # Ensure the default "Kasse" account exists
        self.ensure_kasse_exists()

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
                       recipient_client="", customer_id=None, coa_id=None, category_id=None,
                       currency="EUR", tax_rate=None, tax_amount=None, text="", 
                       document_number=None, booking_type=None, status="posted", 
                       date_tax=None, booking_group_id=None, log_description=None):
        """Insert a new booking into Bookings table
        
        Args:
            date_booking: Transaction date (required)
            amount: Amount (positive = credit/Haben, negative = debit/Soll)
            account_id: FK to Accounts table
            foreign_bank_account: External IBAN/account number
            recipient_client: Name of recipient/client
            customer_id: FK to Customers table
            coa_id: FK to ChartOfAccounts (SKR)
            category_id: FK to Categories
            currency: Currency code (default: EUR)
            tax_rate: Tax rate as decimal (e.g., 0.19 for 19%)
            tax_amount: Calculated tax amount
            text: Notes/purpose
            document_number: External document reference
            booking_type: 'income' or 'expense'
            status: 'draft', 'posted', 'cancelled' (default: posted)
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
             RecipientClient, Customer_ID, COA_ID, Category_ID, Amount, Currency, 
             TaxRate, TaxAmount, Text, DocumentNumber, BookingType, Status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
        
        params = (date_booking, date_tax, booking_group_id, account_id, foreign_bank_account,
                  recipient_client, customer_id, coa_id, category_id, amount, currency,
                  tax_rate, tax_amount, text, document_number, booking_type, status)
        
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
                       foreign_bank_account="", recipient_client="", customer_id=None, 
                       coa_id=None, category_id=None, currency="EUR", tax_rate=None, 
                       tax_amount=None, text="", document_number=None, booking_type=None, 
                       status="posted", date_tax=None, booking_group_id=None, log_description=None):
        """Update an existing booking
        
        Args:
            booking_id: ID of booking to update
            [same parameters as insert_booking]
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''UPDATE Bookings
            SET DateBooking=?, DateTax=?, BookingGroup_ID=?, Account_ID=?, ForeignBankAccount=?,
                RecipientClient=?, Customer_ID=?, COA_ID=?, Category_ID=?, Amount=?, Currency=?,
                TaxRate=?, TaxAmount=?, Text=?, DocumentNumber=?, BookingType=?, Status=?
            WHERE ID=?'''
        
        params = (date_booking, date_tax, booking_group_id, account_id, foreign_bank_account,
                  recipient_client, customer_id, coa_id, category_id, amount, currency,
                  tax_rate, tax_amount, text, document_number, booking_type, status, booking_id)
        
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

    def insert_chart_of_accounts(self, framework, account_number, name, description):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description)
                VALUES (?, ?, ?, ?)
            ''', (framework, account_number, name, description))
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
                WHERE ID = ?
            ''', (framework, account_number, name, description, id))
            conn.commit()
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

        self.insert_chart_of_accounts(4, 6815, "Betriebsbedarf", "")

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
        try:
            cursor.execute('''
                INSERT INTO Accounts (Name, Owner, Number, BIC, BankName, IsCash)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, holder, number, bic, bank_name, is_cash))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting account:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_account(self, account_id, name, holder, number, bic, bank_name):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Accounts
                SET Name = ?, Owner = ?, Number = ?, BIC = ?, BankName = ?
                WHERE ID = ? AND IsCash = 0
            ''', (name, holder, number, bic, bank_name, account_id))
            conn.commit()
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
        try:
            cursor.execute('''
                INSERT INTO Contacts (ContactType, CustomerNumber, Name, Company, Street, PostalCode, City, Country, Email, Phone, TaxID, Notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (contact_type, customer_number, name, company, street, postal_code, city, country, email, phone, tax_id, notes))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting contact:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_contact(self, contact_id, name, contact_type="customer", customer_number="", company="", street="", postal_code="", city="", country="", email="", phone="", tax_id="", notes=""):
        """Update existing contact"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Contacts
                SET ContactType = ?, CustomerNumber = ?, Name = ?, Company = ?, Street = ?, PostalCode = ?, City = ?, Country = ?, Email = ?, Phone = ?, TaxID = ?, Notes = ?
                WHERE ID = ?
            ''', (contact_type, customer_number, name, company, street, postal_code, city, country, email, phone, tax_id, notes, contact_id))
            conn.commit()
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

    # Legacy method names for backward compatibility
    def fetch_customers(self):
        """Legacy method - fetch only customers"""
        return self.fetch_contacts(contact_type='customer')
    
    def get_customer_by_id(self, customer_id):
        """Legacy method - get contact by ID"""
        return self.get_contact_by_id(customer_id)
    
    def insert_customer(self, customer_number, name, company="", street="", postal_code="", city="", country="", email="", phone="", tax_id="", notes=""):
        """Legacy method - insert customer"""
        return self.insert_contact(name, 'customer', customer_number, company, street, postal_code, city, country, email, phone, tax_id, notes)
    
    def update_customer(self, customer_id, customer_number, name, company="", street="", postal_code="", city="", country="", email="", phone="", tax_id="", notes=""):
        """Legacy method - update customer"""
        return self.update_contact(customer_id, name, 'customer', customer_number, company, street, postal_code, city, country, email, phone, tax_id, notes)
    
    def delete_customer(self, customer_id):
        """Legacy method - delete contact"""
        return self.delete_contact(customer_id)
