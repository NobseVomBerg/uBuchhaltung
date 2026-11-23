# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kern-Infrastruktur der Datenbankschicht: Verbindungsaufbau, Init-Guard,
Geld-Grenzkonvertierung und SQL-Logging. Basis-Mixin fuer Database."""
import sqlite3
import os
import json
from contextlib import contextmanager
from decimal import Decimal
from money import to_minor, from_minor
import threading


def coa_id(framework, account_number):
    """Berechnete ChartOfAccounts-ID: 1. Ziffer = Rahmen, dahinter 5 Ziffern Kontonummer.

    Bsp.: SKR4 (Framework 4), Konto 6850 -> 406850.
    """
    return int(framework) * 100000 + int(account_number)


class ForeignKeyError(sqlite3.IntegrityError):
    """Fremdschlüssel-Verletzung im Klartext: welcher Verweis ins Leere zeigt.

    SQLite meldet nur „FOREIGN KEY constraint failed“ – nicht, welcher Verweis
    betroffen ist. Die Meldung dieser Ausnahme nennt ihn und ist für die
    Oberfläche gedacht.
    """


class DeleteRefused(ValueError):
    """Ein Datensatz lässt sich nicht löschen, weil andere Daten auf ihn verweisen. Die Meldung ist Klartext für den Nutzer."""


def reference_texts(conn, checks):
    """Die Verweise, die es gibt, als Texte („3 Rechnungen“, „1 Angebot“) – in der Reihenfolge von ``checks``.

    ``checks`` ist eine Liste ``(sql, params, einzahl, mehrzahl)``: ``sql`` ein ``SELECT COUNT(*) …`` mit
    Platzhaltern für ``params``. Ein Zähler von 0 ergibt keinen Text. Die Abfragen laufen auf ``conn``, also in
    der Transaktion, in der danach gelöscht wird."""
    texts = []
    for sql, params, singular, plural in checks:
        count = conn.execute(sql, params).fetchone()[0]
        if count:
            texts.append(f'{count} {singular if count == 1 else plural}')
    return texts


# Klartext-Namen der Verweise, die der Nutzer in Formularen auswählt (Schlüssel
# klein geschrieben); alle übrigen erscheinen als Tabelle.Spalte.
FK_LABELS = {
    ('invoices', 'owncompanyid'): 'Eigene Firma',
    ('invoices', 'customerid'): 'Kunde',
    ('invoices', 'bankaccountid'): 'Bankkonto',
    ('invoices', 'sourcequoteid'): 'Quell-Angebot',
    ('invoiceitems', 'invoiceid'): 'Rechnung',
    ('invoiceitems', 'articleid'): 'Artikel',
}


def explain_fk_violation(conn, table, row):
    """Klartext, welche Verweise von ``row`` (Spalte → Wert) ins Leere zeigen.

    Geprüft wird gegen die Fremdschlüssel, die *diese* Datenbank für ``table``
    tatsächlich führt (``PRAGMA foreign_key_list``), nicht gegen das, was der
    Code erwartet – so fällt auch ein abweichendes Schema auf. Muss auf
    derselben Verbindung laufen wie die gescheiterte Anweisung, damit noch
    nicht committete Zeilen derselben Transaktion mitzählen.
    """
    values = {str(k).lower(): v for k, v in row.items()}
    missing, present = [], []
    for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
        parent, column, parent_column = fk[2], fk[3], fk[4]
        value = values.get(column.lower())
        if value is None:
            continue                      # NULL verweist nie ins Leere
        label = FK_LABELS.get((table.lower(), column.lower()), f'{table}.{column}')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                            "AND name = ? COLLATE NOCASE", (parent,)).fetchone():
            missing.append(f'{label} {value}: die Tabelle {parent} fehlt')
            continue
        hit = conn.execute(f'SELECT 1 FROM "{parent}" WHERE "{parent_column or "rowid"}" = ? '
                           'LIMIT 1', (value,)).fetchone()
        if hit:
            present.append(f'{label} {value} → {parent}')
        else:
            missing.append(f'{label} mit ID {value} gibt es nicht (Tabelle {parent})')
    if missing:
        return 'Verweis ins Leere – ' + '; '.join(missing)
    return ('Verweis ins Leere laut SQLite, obwohl alle Verweise vorhanden sind ('
            + (', '.join(present) or 'keine gesetzt')
            + ') – Datenbank prüfen (PRAGMA foreign_key_check, integrity_check)')


@contextmanager
def fk_explained(conn, table, row):
    """Übersetzt ein „FOREIGN KEY constraint failed“ der umschlossenen Anweisung
    in eine :class:`ForeignKeyError` mit Klartext (siehe explain_fk_violation)."""
    try:
        yield
    except sqlite3.IntegrityError as exc:
        if isinstance(exc, ForeignKeyError) or 'FOREIGN KEY' not in str(exc):
            raise
        raise ForeignKeyError(explain_fk_violation(conn, table, row)) from exc


class Transaction:
    """Mehrere Schreibschritte in EINER Transaktion: alles oder nichts.

    Methoden mit ``tx=``-Parameter schreiben über ``tx.conn`` und legen ihre
    Einträge für das Replay-Log in ``tx.log`` ab. Ins Log (sql_operations.sql)
    gehen sie erst nach dem Commit – ein zurückgerolltes Statement darf dort
    nie landen.
    """

    def __init__(self, db):
        self._db = db
        self.conn = db._get_connection()
        self.log = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        if exc_type is None:
            for entry in self.log:
                try:
                    self._db._log_sql(*entry)
                except Exception as e:
                    # Die Daten sind festgeschrieben. Ein Fehler beim Protokollieren
                    # (Platte voll, Rechte) darf den Vorgang nicht als gescheitert
                    # melden – sonst speichert der Nutzer dieselbe Rechnung noch einmal.
                    print(f"WARNUNG: Replay-Log konnte nicht geschrieben werden: {e}")
                    break
        return False


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
             ORDER BY pr.RoleKey)          AS role_keys,        -- 31
            cd.DatevConsultantNo           AS datev_consultant_no, -- 32 (2.3.0, nur die eigene Firma trägt sie)
            cd.DatevClientNo               AS datev_client_no,  -- 33
            c.VatMethod                    AS vat_method        -- 34 (2.4.0, Rohwert: gelesen wird er mit contacts.vat_method)
        FROM Contacts c
        LEFT JOIN CompanyDetails    cd ON c.ID = cd.ContactID
        LEFT JOIN PersonDetails     pd ON c.ID = pd.ContactID
        LEFT JOIN ContactAddresses  ca ON c.ID = ca.ContactID AND ca.AddressType = 'main'
        LEFT JOIN CompanyDetails    cd2 ON cd2.ContactID = pd.CompanyContactID
    '''
    def __init__(self, db_name=None):
        # Ohne expliziten Pfad richtet sich die DB nach dem angemeldeten Benutzer
        # (data/users/<user>/buch.db, TODO #4); ohne Benutzer-Kontext (Werkzeuge,
        # Tests) ist das ./data/buch.db. Ein explizit übergebenes db_name (z. B. Tests)
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
        if not self._is_initialized(key):
            with _CoreMixin._init_lock:
                if not self._is_initialized(key):             # double-checked locking
                    self.initialize_database()
                    _CoreMixin._initialized_dbs.add(key)      # erst nach Erfolg markieren

    @staticmethod
    def _is_initialized(key):
        """Wurde die Datei unter ``key`` in diesem Prozess schon eingerichtet – und ist
        sie es noch?

        Der Pfad allein reicht nicht: Die Datei kann seither ersetzt worden sein
        (zurückgespielte Sicherung eines älteren Standes, neu angelegte Datei am
        selben Ort). Deshalb zählt zusätzlich die Schema-Zahl in der Datei – weicht
        sie ab, läuft ``initialize_database`` erneut (Migration bzw. Sperre gegen
        neuere Datenbanken).
        """
        if key not in _CoreMixin._initialized_dbs:
            return False
        from version import SCHEMA_VERSION
        if not os.path.isfile(key):
            return False
        try:
            conn = sqlite3.connect(key)
            try:
                return conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            finally:
                conn.close()
        except sqlite3.Error:
            return False

    def _get_connection(self):
        """Get a database connection with foreign keys enabled"""
        conn = sqlite3.connect(self.db_name)
        conn.execute('PRAGMA foreign_keys = ON')
        return conn
    def transaction(self):
        """Mehrere Schreibschritte atomar ausführen (siehe :class:`Transaction`)."""
        return Transaction(self)
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
        """Je Tabelle der Datenbank die Zahl ihrer Zeilen: ``[(Name, Anzahl)]``, nach Namen sortiert, ohne die internen Tabellen von SQLite.

        Der Name steht als Bezeichner in Anführungszeichen in der Abfrage (ein ``"`` darin verdoppelt): Eine über die SQL-Konsole angelegte Tabelle
        kann Leerzeichen oder Sonderzeichen im Namen tragen. Lässt sich eine Tabelle nicht zählen (etwa eine virtuelle Tabelle, deren Modul fehlt),
        steht statt der Zahl ``'–'`` da – die Übersicht der anderen bleibt."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            statistics = []
            for (table_name,) in cursor.fetchall():
                name = str(table_name)
                if name.startswith('sqlite_'):
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                try:
                    cursor.execute(f'SELECT COUNT(*) FROM {quoted}')
                    count = cursor.fetchone()[0]
                except sqlite3.Error:
                    count = '–'
                statistics.append((name, count))
            return statistics
        finally:
            conn.close()

    @staticmethod
    def _sql_identifier(name):
        """Ein Tabellen- oder Spaltenname als Bezeichner in Anführungszeichen (ein ``"`` darin verdoppelt)."""
        return '"' + str(name).replace('"', '""') + '"'

    @staticmethod
    def _sql_literal(value):
        """Ein Wert aus der Datenbank als SQL-Literal für den DB-Export: NULL, Zahl, ``X'…'`` für Binärdaten, sonst Text in Hochkommas."""
        if value is None:
            return 'NULL'
        if isinstance(value, bool):
            return '1' if value else '0'
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value != value:
                return 'NULL'
            if value in (float('inf'), float('-inf')):
                return '9e999' if value > 0 else '-9e999'
            return repr(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "X'" + bytes(value).hex() + "'"
        return "'" + str(value).replace("'", "''") + "'"

    def export_to_sql(self, filepath: str) -> tuple[int, int]:
        """Alle Tabellen als INSERT-Befehle in eine .sql-Datei schreiben (ohne die internen Tabellen von SQLite und ohne leere Tabellen).

        Tabellen- und Spaltennamen stehen als Bezeichner in Anführungszeichen (``_sql_identifier``), Werte als Literale (``_sql_literal``) –
        ein Name aus der SQL-Konsole mit Leerzeichen, Klammern oder einem Semikolon bleibt ein Name und wird nie ein eigener Befehl; die
        Kommentarzeile je Tabelle nennt den Namen ohne Zeilenumbrüche. Returns ``(Tabellen, Zeilen)``."""
        import datetime
        import os
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [str(row[0]) for row in cursor.fetchall() if not str(row[0]).startswith('sqlite_')]

            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            tables_exported = 0
            rows_exported = 0
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"-- DB-Export {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("-- Direkt verwendbar im SQL-Konsolenbereich (INSERT-Statements)\n\n")
                for table in tables:
                    quoted = self._sql_identifier(table)
                    cursor.execute(f'SELECT * FROM {quoted}')
                    rows = cursor.fetchall()
                    if not rows:
                        continue
                    cols_sql = ', '.join(self._sql_identifier(d[0]) for d in cursor.description)
                    f.write('-- ' + ' '.join(table.split()) + '\n')
                    f.write(f'INSERT INTO {quoted} ({cols_sql}) VALUES\n')
                    last = len(rows) - 1
                    for idx, row in enumerate(rows):
                        vals_sql = ', '.join(self._sql_literal(v) for v in row)
                        # Letzte Zeile bekommt Semikolon, alle anderen ein Komma
                        f.write(f'({vals_sql});\n' if idx == last else f'({vals_sql}),\n')
                        rows_exported += 1
                    f.write('\n')
                    tables_exported += 1
            return tables_exported, rows_exported
        finally:
            conn.close()


# So viele Treffer liefern die Suchen der Belege und Buchungen höchstens – auch bei einem direkten Aufruf der Datenbankschicht.
SEARCH_LIMIT_MAX = 50


def search_limit(limit):
    """``limit`` einer Suche, auf 1 bis ``SEARCH_LIMIT_MAX`` begrenzt: SQLite liest ein ``LIMIT`` unter 0 als „ohne Grenze“."""
    return max(1, min(int(limit), SEARCH_LIMIT_MAX))


def fold_text(value):
    """Text für die Suche ohne Rücksicht auf die Schreibweise (``str.casefold``, auch Umlaute und ß); NULL bleibt NULL."""
    return None if value is None else str(value).casefold()


def add_fold_function(conn):
    """Die SQL-Funktion ``ub_fold(x)`` (= ``fold_text``) auf ``conn`` anmelden – nur für die Suchen der Belege und Buchungen."""
    conn.create_function('ub_fold', 1, fold_text, deterministic=True)
