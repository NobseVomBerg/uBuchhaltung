"""Kern-Infrastruktur der Datenbankschicht: Verbindungsaufbau, Init-Guard,
Geld-Grenzkonvertierung und SQL-Logging. Basis-Mixin fuer Database."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
import threading


def coa_id(framework, account_number):
    """Berechnete ChartOfAccounts-ID: 1. Ziffer = Rahmen, dahinter 5 Ziffern Kontonummer.

    Bsp.: SKR4 (Framework 4), Konto 6850 -> 406850.
    """
    return int(framework) * 100000 + int(account_number)


class _CoreMixin:
    _initialized_dbs = set()
    _init_lock = threading.Lock()
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
            ca.AddressLine1,               -- 25 address_line1
            c.Abbreviation,                -- 26 abbreviation
            (SELECT GROUP_CONCAT(ctl.TypeKey, ',')
             FROM ContactTypeLinks ctl
             WHERE ctl.ContactID = c.ID
             ORDER BY ctl.TypeKey)         AS type_keys,        -- 27
            pd.JobTitle,                   -- 28 job_title
            pd.Department,                 -- 29 department
            COALESCE(pd.IsPrimaryContact, 0) AS is_primary_contact, -- 30
            (SELECT GROUP_CONCAT(pr.RoleKey, ',')
             FROM PersonRoles pr
             WHERE pr.ContactID = c.ID
             ORDER BY pr.RoleKey)          AS role_keys         -- 31
        FROM Contacts c
        LEFT JOIN CompanyDetails    cd ON c.ID = cd.ContactID
        LEFT JOIN PersonDetails     pd ON c.ID = pd.ContactID
        LEFT JOIN ContactAddresses  ca ON c.ID = ca.ContactID AND ca.AddressType = 'main'
        LEFT JOIN CompanyDetails    cd2 ON cd2.ContactID = pd.CompanyContactID
    '''
    def __init__(self, db_name=None):
        # Ohne expliziten Pfad richtet sich die DB nach dem angemeldeten Benutzer
        # (Mehrbenutzer-Betrieb, TODO #4). Im Single-User-Default ist das
        # weiterhin ./data/buch.db. Ein explizit übergebenes db_name (z. B. Tests)
        # gewinnt immer.
        if db_name is None:
            import userctx
            db_name = userctx.user_db_path()
        self.db_name = db_name
        # Ensure the directory exists
        db_dir = os.path.dirname(self.db_name)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        key = os.path.abspath(self.db_name)
        if key not in _CoreMixin._initialized_dbs:
            with _CoreMixin._init_lock:
                if key not in _CoreMixin._initialized_dbs:   # double-checked locking
                    self.initialize_database()
                    _CoreMixin._initialized_dbs.add(key)      # erst nach Erfolg markieren
    def _get_connection(self):
        """Get a database connection with foreign keys enabled"""
        conn = sqlite3.connect(self.db_name)
        conn.execute('PRAGMA foreign_keys = ON')
        return conn
    @staticmethod
    def _minor_opt(value):
        """to_minor, aber None bleibt None (fuer optionale Geldspalten)."""
        return None if value is None or value == '' else to_minor(value)
    @staticmethod
    def _euro_row(row, *money_indices):
        """Wandelt die angegebenen Spalten einer DB-Zeile von Minor Units (int)
        in Euro-Decimal um. Geld wird intern als Festkomma-Integer gespeichert
        (siehe money.py); Konsumenten erhalten Euro-Decimal. None bleibt None.

        Gibt None zurueck, wenn row None ist (z.B. fetchone ohne Treffer).
        """
        if row is None:
            return None
        row = list(row)
        for i in money_indices:
            if row[i] is not None:
                row[i] = from_minor(row[i])
        return tuple(row)
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

            def _sql_literal(param):
                if param is None:
                    return 'NULL'
                if isinstance(param, str):
                    # SQL-konform: einfache Anführungszeichen verdoppeln
                    return "'" + param.replace("'", "''") + "'"
                return str(param)

            # Platzhalter ersetzen: Template an '?' splitten und Literale
            # einflechten – ein '?' INNERHALB eines Parameterwerts kann so
            # keine nachfolgenden Ersetzungen verschieben.
            pieces = sql_template.split('?')
            if len(pieces) == len(params) + 1:
                out = [pieces[0]]
                for param, tail in zip(params, pieces[1:]):
                    out.append(_sql_literal(param))
                    out.append(tail)
                sql_statement = ''.join(out)
            else:
                # Platzhalter-Anzahl passt nicht (sollte nicht vorkommen):
                # Template unersetzt loggen statt falsch zu ersetzen
                sql_statement = sql_template
            parser.log_sql(sql_statement, params, description)
        except ImportError:
            pass  # Parser not available, skip logging
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
