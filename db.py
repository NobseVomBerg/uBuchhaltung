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
                    replacement = f"'{param.replace('"', '""')}'"
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Categories (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT,
                Parent_ID INTEGER
            )
        ''')

        # Accounts table for bank accounts and cash accounts
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Accounts (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT NOT NULL,
                Owner TEXT,
                IBAN TEXT,
                BIC TEXT,
                BankName TEXT,
                IsCash INTEGER DEFAULT 0,
                UNIQUE(Name)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Belege (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Nummer TEXT,
                Datum DATE,
                Dateiname TEXT,
                Pfad TEXT,
                Info TEXT
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
                Betrag REAL,
                SkrBuchJoinId INTEGER
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS SkrBuch (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                JoinId INTEGER,
                KntNr TEXT,
                BetragNetto REAL,
                Steuer REAL
            )
        ''')

        conn.commit()
        conn.close()
        
        # Ensure the default "Kasse" account exists
        self.ensure_kasse_exists()

    # Table Belege (Receipts)
    def fetch_receipts(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Belege')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_receipt(self, number, date, filename, path, info):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO Belege (Nummer, Datum, Dateiname, Pfad, Info)
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
                UPDATE Belege
                SET Datum = ?, Dateiname = ?, Pfad = ?, Info = ?
                WHERE Nummer = ?
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

    def insert_transaction(self, dateBooking, amount, own_iban="", foreign_iban="", note="", receipt_number=None, dateTax=None, skr_join_id=None, log_description=None):
        """Insert a transaction into Zahlung table
        
        Args:
            dateBooking: Transaction date (DatumBuch)
            amount: Amount in EUR (positive = credit/Haben, negative = debit/Soll)
            own_iban: Own bank account IBAN
            foreign_iban: Foreign bank account IBAN
            note: Transaction note/purpose (Zweck)
            receipt_number: Receipt reference number
            dateTax: Secondary date (DatumSteuer), optional
            skr_join_id: SKR book join ID, optional
            log_description: Description for SQL logging (optional)
        """
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        sql_template = '''INSERT INTO Zahlung (DatumBuch, DatumSteuer, BankEigen, BankFremd, Zweck, BelegNummer, Betrag, SkrBuchJoinId)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)'''
        params = (dateBooking, dateTax, own_iban, foreign_iban, note, receipt_number, amount, skr_join_id)
        
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
    
    def update_transaction(self, transaction_id, dateBooking, amount, own_iban="", foreign_iban="", note="", receipt_number=None, dateSteuer=None, skr_join_id=None, log_description=None):
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
            skr_join_id: SKR book join ID, optional
            log_description: Description for SQL logging (optional)
        """
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        sql_template = '''UPDATE Zahlung
            SET DatumBuch=?, DatumSteuer=?, BankEigen=?, BankFremd=?, Zweck=?, BelegNummer=?, Betrag=?, SkrBuchJoinId=?
            WHERE ID=?'''
        params = (dateBooking, dateSteuer, own_iban, foreign_iban, note, receipt_number, amount, skr_join_id, transaction_id)
        
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

    # Table SkrBuch
    def fetch_skrbuch(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM SkrBuch')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_skrbuch(self, join_id, knt_nr, betrag_netto, steuer):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO SkrBuch (JoinId, KntNr, BetragNetto, Steuer)
            VALUES (?, ?, ?, ?)
        ''', (join_id, knt_nr, betrag_netto, steuer))
        conn.commit()
        conn.close()

    def update_skrbuch(self, id, join_id, knt_nr, betrag_netto, steuer):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE SkrBuch
            SET JoinId = ?, KntNr = ?, BetragNetto = ?, Steuer = ?
            WHERE ID = ?
        ''', (join_id, knt_nr, betrag_netto, steuer, id))
        conn.commit()
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
                INSERT INTO Accounts (Name, Owner, IBAN, BIC, BankName, IsCash)
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

    def insert_account(self, name, holder, iban, bic, bank_name, is_cash=0):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO Accounts (Name, Owner, IBAN, BIC, BankName, IsCash)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, holder, iban, bic, bank_name, is_cash))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting account:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_account(self, account_id, name, holder, iban, bic, bank_name):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Accounts
                SET Name = ?, Owner = ?, IBAN = ?, BIC = ?, BankName = ?
                WHERE ID = ? AND IsCash = 0
            ''', (name, holder, iban, bic, bank_name, account_id))
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
