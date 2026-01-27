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
        conn = sqlite3.connect(self.db_name)
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

        # Customer table for managing customers
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Customers (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
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
                ID INTEGER PRIMARY KEY AUTOINCREMENT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Zahlung (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                DatumBuch DATE,
                DatumSteuer DATE,
                BankEigen TEXT,
                BankFremd TEXT,
                Zweck TEXT,
                BelegNummer TEXT,
                Betrag REAL
            )
        ''')

        conn.commit()
        conn.close()
        
        # Ensure the default "Kasse" account exists
        self.ensure_kasse_exists()

    # Table Documents (Receipts)
    def fetch_receipts(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Documents')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_receipt(self, number, date, filename, path, info):
        conn = sqlite3.connect(self.db_name)
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

    def update_receipt(self, number, date, filename, path, info):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Documents
                SET Date = ?, Filename = ?, Path = ?, Info = ?
                WHERE Number = ?
            ''', (date, filename, path, info, number))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating receipt:", e)
            conn.rollback()
        finally:
            conn.close()

    # Table Zahlung
    def fetch_zahlung(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Zahlung ORDER BY DatumBuch DESC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_transaction(self, dateBooking, amount, own_iban="", foreign_iban="", note="", receipt_number=None, dateTax=None, log_description=None):
        """Insert a transaction into Zahlung table
        
        Args:
            dateBooking: Transaction date (DatumBuch)
            amount: Amount in EUR (positive = credit/Haben, negative = debit/Soll)
            own_iban: Own bank account IBAN
            foreign_iban: Foreign bank account IBAN
            note: Transaction note/purpose (Zweck)
            receipt_number: Receipt reference number
            dateTax: Secondary date (DatumSteuer), optional
            log_description: Description for SQL logging (optional)
        """
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        sql_template = '''INSERT INTO Zahlung (DatumBuch, DatumSteuer, BankEigen, BankFremd, Zweck, BelegNummer, Betrag)
            VALUES (?, ?, ?, ?, ?, ?, ?)'''
        params = (dateBooking, dateTax, own_iban, foreign_iban, note, receipt_number, amount)
        
        cursor.execute(sql_template, params)
        conn.commit()
        last_id = cursor.lastrowid
        conn.close()
        
        # Optional SQL logging
        if log_description:
            self._log_sql(sql_template, params, log_description)
        
        return last_id
    
    def check_transaction_exists(self, date, amount, own_iban, foreign_iban="", note=""):
        """Check if a transaction with same date, amount, IBANs, and note already exists
        
        Args:
            date: Transaction date (DatumBuch)
            amount: Amount in EUR
            own_iban: Own bank account IBAN
            foreign_iban: Foreign bank account IBAN (optional)
            note: Transaction note/purpose (Zweck) for additional uniqueness check
            
        Returns:
            True if duplicate exists, False otherwise
        """
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM Zahlung
            WHERE DatumBuch=? AND Betrag=? AND BankEigen=? AND BankFremd=? AND Zweck=?
        ''', (date, amount, own_iban, foreign_iban, note))
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    
    def update_transaction(self, transaction_id, dateBooking, amount, own_iban="", foreign_iban="", note="", receipt_number=None, dateSteuer=None, log_description=None):
        """Update an existing transaction in Zahlung table
        
        Args:
            transaction_id: ID of the transaction to update
            dateBooking: Transaction date (DatumBuch)
            amount: Amount in EUR (positive = credit/Haben, negative = debit/Soll)
            own_iban: Own bank account IBAN
            foreign_iban: Foreign bank account IBAN
            note: Transaction note/purpose (Zweck)
            receipt_number: Receipt reference number
            dateSteuer: Secondary date (DatumSteuer), optional
            log_description: Description for SQL logging (optional)
        """
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        sql_template = '''UPDATE Zahlung
            SET DatumBuch=?, DatumSteuer=?, BankEigen=?, BankFremd=?, Zweck=?, BelegNummer=?, Betrag=?
            WHERE ID=?'''
        params = (dateBooking, dateSteuer, own_iban, foreign_iban, note, receipt_number, amount, transaction_id)
        
        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()
        
        # Optional SQL logging
        if log_description:
            self._log_sql(sql_template, params, log_description)
    
    def get_transaction_by_id(self, transaction_id):
        """Get a single transaction by ID"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Zahlung WHERE ID=?', (transaction_id,))
        transaction = cursor.fetchone()
        conn.close()
        return transaction

    # Table ChartOfAccounts
    def fetch_chart_of_accounts(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM ChartOfAccounts')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_chart_of_accounts(self, framework, account_number, name, description):
        conn = sqlite3.connect(self.db_name)
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
        conn = sqlite3.connect(self.db_name)
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
        conn = sqlite3.connect(self.db_name)
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
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Accounts ORDER BY IsCash DESC, Name ASC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_account_by_id(self, account_id):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Accounts WHERE ID = ?', (account_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def insert_account(self, name, holder, number, bic, bank_name, is_cash=0):
        conn = sqlite3.connect(self.db_name)
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
        conn = sqlite3.connect(self.db_name)
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
        conn = sqlite3.connect(self.db_name)
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
        conn = sqlite3.connect(self.db_name)
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

    # Table Customers
    def fetch_customers(self):
        """Fetch all customers"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Customers ORDER BY Name ASC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_customer_by_id(self, customer_id):
        """Get customer by ID"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Customers WHERE ID = ?', (customer_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def insert_customer(self, customer_number, name, company="", street="", postal_code="", city="", country="", email="", phone="", tax_id="", notes=""):
        """Insert new customer"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO Customers (CustomerNumber, Name, Company, Street, PostalCode, City, Country, Email, Phone, TaxID, Notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (customer_number, name, company, street, postal_code, city, country, email, phone, tax_id, notes))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting customer:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_customer(self, customer_id, customer_number, name, company="", street="", postal_code="", city="", country="", email="", phone="", tax_id="", notes=""):
        """Update existing customer"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Customers
                SET CustomerNumber = ?, Name = ?, Company = ?, Street = ?, PostalCode = ?, City = ?, Country = ?, Email = ?, Phone = ?, TaxID = ?, Notes = ?
                WHERE ID = ?
            ''', (customer_number, name, company, street, postal_code, city, country, email, phone, tax_id, notes, customer_id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating customer:", e)
            conn.rollback()
        finally:
            conn.close()

    def delete_customer(self, customer_id):
        """Delete customer"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM Customers WHERE ID = ?', (customer_id,))
        conn.commit()
        conn.close()
