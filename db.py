import sqlite3

class Database:
    def __init__(self, db_name="buch.db"):
        self.db_name = db_name
        self.initialize_database()

    def initialize_database(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Belege (
                Nummer TEXT,
                Datum DATE,
                Dateiname TEXT,
                Pfad TEXT,
                Info TEXT,
                UNIQUE(Nummer),
                UNIQUE(Dateiname, Pfad)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Zahlung (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Datum1 DATE,
                Datum2 DATE,
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

        # RahmenNr is 03/04 in Germany or 07 for Austria
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Skr (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                RahmenNr INTEGER,
                Konto INTEGER,
                Name TEXT,
                Gruppe TEXT,
                UNIQUE(RahmenNr, Konto)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Konten (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                Bezeichnung TEXT NOT NULL,
                Inhaber TEXT,
                IBAN TEXT,
                BIC TEXT,
                BankName TEXT,
                IstKasse INTEGER DEFAULT 0,
                UNIQUE(Bezeichnung)
            )
        ''')

        conn.commit()
        conn.close()
        
        # Ensure the default "Kasse" account exists
        self.ensure_kasse_exists()

    # Table Belege
    def fetch_belege(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Belege')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_beleg(self, nummer, datum, dateiname, pfad, info):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO Belege (Nummer, Datum, Dateiname, Pfad, Info)
                VALUES (?, ?, ?, ?, ?)
            ''', (nummer, datum, dateiname, pfad, info))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting Beleg:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_beleg(self, nummer, datum, dateiname, pfad, info):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Belege
                SET Datum = ?, Dateiname = ?, Pfad = ?, Info = ?
                WHERE Nummer = ?
            ''', (datum, dateiname, pfad, info, nummer))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating Beleg:", e)
            conn.rollback()
        finally:
            conn.close()

    # Table Zahlung
    def fetch_zahlung(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Zahlung')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_zahlung(self, datum1, datum2, bank_eigen, bank_fremd, zweck, beleg_nummer, betrag, skr_buch_join_id):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO Zahlung (Datum1, Datum2, BankEigen, BankFremd, Zweck, BelegNummer, Betrag, SkrBuchJoinId)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (datum1, datum2, bank_eigen, bank_fremd, zweck, beleg_nummer, betrag, skr_buch_join_id))
        conn.commit()
        conn.close()

    def update_zahlung(self, id, datum1, datum2, bank_eigen, bank_fremd, zweck, beleg_nummer, betrag, skr_buch_join_id):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE Zahlung
            SET Datum1 = ?, Datum2 = ?, BankEigen = ?, BankFremd = ?, Zweck = ?, BelegNummer = ?, Betrag = ?, SkrBuchJoinId = ?
            WHERE ID = ?
        ''', (datum1, datum2, bank_eigen, bank_fremd, zweck, beleg_nummer, betrag, skr_buch_join_id, id))
        conn.commit()
        conn.close()

    # Table Skr
    def fetch_skr(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Skr')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def insert_skr(self, rid, konto, name, gruppe):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO Skr (RahmenNr, Konto, Name, Gruppe)
                VALUES (?, ?, ?, ?)
            ''', (rid, konto, name, gruppe))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting into Skr:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_skr(self, id, rid, konto, name, gruppe):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Skr
                SET RahmenNr = ?, Konto = ?, Name = ?, Gruppe = ?
                WHERE ID = ?
            ''', (rid, konto, name, gruppe, id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating Skr:", e)
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
        self.insert_beleg("12F123", "2012-05-01", "testBeleg01.pdf", "./2012/", "Testbeleg")
        self.insert_beleg("12F124", "2012-05-02", "testBeleg02.pdf", "./2012/", "noch ein Testbeleg")
        self.insert_beleg("12F125", "2012-05-03", "testBeleg03.pdf", "./2012/", "und noch einer")
        self.insert_beleg("12F126", "2012-05-04", "testBeleg04.pdf", "./2012/", "")

        self.insert_skr(4, 6815, "Betriebsbedarf", "1")

    # Table Konten
    def ensure_kasse_exists(self):
        """Ensure the default 'Kasse' account exists and cannot be deleted"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM Konten WHERE IstKasse = 1')
        count = cursor.fetchone()[0]
        if count == 0:
            cursor.execute('''
                INSERT INTO Konten (Bezeichnung, Inhaber, IBAN, BIC, BankName, IstKasse)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ("Kasse", "", "", "", "", 1))
            conn.commit()
        conn.close()

    def fetch_konten(self):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Konten ORDER BY IstKasse DESC, Bezeichnung ASC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_konto_by_id(self, konto_id):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Konten WHERE ID = ?', (konto_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def insert_konto(self, bezeichnung, inhaber, iban, bic, bankname, ist_kasse=0):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO Konten (Bezeichnung, Inhaber, IBAN, BIC, BankName, IstKasse)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (bezeichnung, inhaber, iban, bic, bankname, ist_kasse))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting Konto:", e)
            conn.rollback()
        finally:
            conn.close()

    def update_konto(self, konto_id, bezeichnung, inhaber, iban, bic, bankname):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Konten
                SET Bezeichnung = ?, Inhaber = ?, IBAN = ?, BIC = ?, BankName = ?
                WHERE ID = ? AND IstKasse = 0
            ''', (bezeichnung, inhaber, iban, bic, bankname, konto_id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating Konto:", e)
            conn.rollback()
        finally:
            conn.close()

    def delete_konto(self, konto_id):
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        # Only allow deletion if it's not the Kasse account
        cursor.execute('DELETE FROM Konten WHERE ID = ? AND IstKasse = 0', (konto_id,))
        conn.commit()
        conn.close()
