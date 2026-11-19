# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.5.0 (Schema 2005): das Kennzeichen „reines Geschäftskonto“ am Bankkonto.

``Accounts`` bekommt am Ende die Spalte ``BusinessOnly`` (ganze Zahl, nicht NULL, Vorgabe 1: 1 = reines Geschäftskonto,
0 = gemischt genutzt). Geprüft wird:

* Eine frische und eine migrierte Datenbank haben dieselbe Spaltenreihenfolge (alles, was ``Accounts``-Zeilen positionsbasiert
  liest – ``IsCash`` an Index 6, ``SKRAccount`` an Index 7 –, bleibt, wo es war; die neue Spalte steht ganz hinten), dieselben
  Tabellen und Indizes – auch aus 2.3.x, 2.2.x, 2.1.x, 2.0.x und 1.x.
* Der Bestand bleibt, wie er war: jede Spalte jeder Zeile, alle anderen Tabellen; der neue Wert ist überall 1 (reines
  Geschäftskonto: die Importe laufen wie bisher) – nichts wird umgedeutet.
* Vor der Umstellung entsteht eine Sicherung (Stand 2004), scheitert sie, bleibt die Datenbank unberührt; bricht die
  Umstellung ab, rollt sie ganz zurück, und ein zweiter Start klappt.
* Die DB-Schicht schreibt nur 0 und 1 (``insert_account``, ``update_account``), und ``account_allows_selection`` liest streng.

Namen und Nummern sind erfundene Testwerte; die IBANs stammen aus der erlaubten Liste.
"""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import version
from db import Database, MigrationBackupError, _CoreMixin
from db.schema import BUSINESS_ONLY_SCHEMA
from tests import installation
from tests.entry_form_world import to_state_2000, to_state_2001, to_state_2002, to_state_2003, to_state_2004

NEW_COLUMN = 'BusinessOnly'
OLD_ACCOUNT_COLUMNS = ['ID', 'Name', 'Owner', 'Number', 'BIC', 'BankName', 'IsCash', 'SKRAccount']
IBAN_BUSINESS = 'DE89370400440532013000'
IBAN_MIXED = 'DE02120300000000202051'


def _reopen(path):
    """Die Datei so öffnen, als wäre das Programm neu gestartet worden."""
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def _info(path, table):
    with closing(sqlite3.connect(path)) as con:
        return [row[1:] for row in con.execute(f'PRAGMA table_info({table})')]     # ohne die laufende Nummer


def _names(path, table='Accounts'):
    return [row[0] for row in _info(path, table)]


def _backups(path):
    directory = Path(path).parent / 'backup'
    return sorted(directory.iterdir()) if directory.is_dir() else []


def _contents(path):
    with closing(sqlite3.connect(path)) as con:
        tables = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        return {t: con.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables}


def _execute(db, sql, params=()):
    with closing(db._get_connection()) as con:
        con.execute(sql, params)
        con.commit()


def _stored(db, account_id):
    with closing(db._get_connection()) as con:
        return con.execute('SELECT BusinessOnly FROM Accounts WHERE ID = ?', (account_id,)).fetchone()[0]


@pytest.fixture
def stock(tmp_path):
    """Ein Bestand im Stand von 2.4.x: die Kasse und zwei Bankkonten, eine Bankbewegung mit Buchungssatz."""
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    db.insert_account('Giro Alpha', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', skr_account=1800)
    db.insert_account('Giro Beta', 'Erika Probe', IBAN_MIXED, 'TESTDEFF', 'Testbank', skr_account=1810)
    account = db.fetch_accounts()[-1][0]
    coa = db.get_coa_id_by_account_number
    bank = db.insert_booking('2026-03-05', -119.0, account_id=account, booking_type='bank', text='Bürobedarf')
    db.insert_booking('2026-03-05', -119.0, coa_id=coa(6815), counter_coa_id=coa(1810), tax_rate=0.19,
                      tax_amount=-19.0, document_number='ER-1', parent_booking_id=bank)
    to_state_2004(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


# ── Der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_stock_is_a_2_4_x_database_before_the_migration(stock):
    assert installation.user_version(stock) == 2004
    assert _names(stock) == OLD_ACCOUNT_COLUMNS
    assert len(_contents(stock)['Accounts']) == 3                                  # die Kasse und zwei Bankkonten


def test_this_program_is_a_2_5_x_program():
    assert BUSINESS_ONLY_SCHEMA == 2005
    assert version.SCHEMA_VERSION >= 2005 and version.SCHEMA_STEP >= 5
    assert version.APP_VERSION.startswith('2.') and int(version.APP_VERSION.split('.')[1]) >= 5


# ── Frisch = migriert ────────────────────────────────────────────────────────

def test_a_fresh_and_a_migrated_database_have_the_same_columns_tables_and_indexes(stock, tmp_path):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    _reopen(stock)

    assert _names(stock) == [*OLD_ACCOUNT_COLUMNS, NEW_COLUMN] == _names(fresh)
    assert _info(stock, 'Accounts') == _info(fresh, 'Accounts')                   # Name, Typ, NOT NULL, Vorgabe, PK
    with closing(sqlite3.connect(stock)) as a, closing(sqlite3.connect(fresh)) as b:
        for query in ("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
                      "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            assert [r[0] for r in a.execute(query)] == [r[0] for r in b.execute(query)]
    assert installation.user_version(stock) == installation.user_version(fresh) == version.SCHEMA_VERSION


@pytest.mark.parametrize('older', [to_state_2003, to_state_2002, to_state_2001, to_state_2000, None],
                         ids=['aus-2.3.x', 'aus-2.2.x', 'aus-2.1.x', 'aus-2.0.x', 'aus-1.x'])
def test_older_stocks_end_with_the_same_column_order_as_a_fresh_database(stock, tmp_path, older):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    if older is not None:
        older(stock)
    else:
        installation.stamp_user_version(stock, 4)
    _reopen(stock)

    for table in ('Accounts', 'Contacts', 'Bookings', 'CompanyDetails', 'Invoices'):
        assert _info(stock, table) == _info(fresh, table), table
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_2_3_x_stock_goes_through_both_steps_in_one_backed_up_run(stock):
    to_state_2003(stock)
    assert installation.user_version(stock) == 2003

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock)[-1] == NEW_COLUMN and _names(stock, 'Contacts')[-1] == 'VatMethod'
    (backup,) = _backups(stock)
    assert installation.user_version(str(backup)) == 2003


def test_the_column_positions_of_the_account_readers_stay_where_they_were(stock):
    """Was ``Accounts`` positionsbasiert liest, bleibt unberührt: ``IsCash`` an Index 6, ``SKRAccount`` an Index 7, IBAN an
    Index 3 – die neue Spalte steht ganz hinten (Index 8)."""
    before = _contents(stock)['Accounts']
    db = _reopen(stock)

    assert _names(stock).index(NEW_COLUMN) == len(OLD_ACCOUNT_COLUMNS) == 8
    rows = db.fetch_accounts()
    assert [row[:8] for row in rows] == sorted(before, key=lambda row: (-row[6], row[1]))   # IsCash DESC, Name
    for row in rows:
        assert len(row) == 9 and row[8] == 1
    kasse = next(row for row in rows if row[6] == 1)
    giro = next(row for row in rows if row[1] == 'Giro Alpha')
    assert (kasse[1], kasse[7]) == ('Kasse', 1600) and (giro[3], giro[7]) == (IBAN_BUSINESS, 1800)
    assert db.get_account_by_id(giro[0]) == giro


# ── Der Bestand bleibt ───────────────────────────────────────────────────────

def test_existing_rows_stay_untouched_and_the_new_value_is_one(stock):
    before = _contents(stock)
    assert len(before['Accounts']) == 3 and len(before['Bookings']) == 2

    _reopen(stock)

    after = _contents(stock)
    assert all(row[-1] == 1 for row in after['Accounts'])                          # überall 1 – nichts wird umgedeutet
    assert [row[:-1] for row in after['Accounts']] == before['Accounts']          # jede Spalte jeder Zeile
    for table, rows in before.items():
        if table not in ('Accounts', 'Contacts'):                                   # Contacts: Schema 2004, siehe dort
            assert after[table] == rows, table                                    # alle anderen Tabellen


def test_nobody_allows_a_selection_after_the_migration(stock):
    db = _reopen(stock)
    assert [db.account_allows_selection(row[0]) for row in db.fetch_accounts()] == [False, False, False]


def test_a_second_start_changes_nothing_and_makes_no_second_backup(stock):
    _reopen(stock)
    after_first, backups = installation.dump(stock), _backups(stock)

    _reopen(stock)
    _reopen(stock)

    assert installation.dump(stock) == after_first and _backups(stock) == backups


# ── Sicherung, Abbruch, Wiederholung ─────────────────────────────────────────

def test_the_migration_makes_a_backup_of_the_old_state_first(stock):
    before = installation.dump(stock)

    _reopen(stock)

    (backup,) = _backups(stock)
    assert backup.name.endswith(f'_vor_{version.APP_VERSION}_buch.db') and backup.name[:8].isdigit()
    assert installation.user_version(str(backup)) == 2004
    assert NEW_COLUMN not in _names(str(backup))
    assert installation.dump(str(backup)) == before                             # Zeile für Zeile der alte Stand
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_failing_backup_stops_the_opening_and_leaves_the_stock_at_2004(stock):
    before = installation.dump(stock)
    (Path(stock).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError):
        _reopen(stock)

    assert installation.user_version(stock) == 2004 and installation.dump(stock) == before
    assert NEW_COLUMN not in _names(stock)
    (Path(stock).parent / 'backup').unlink()                                    # Ursache behoben: jetzt klappt es
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[-1] == NEW_COLUMN


def test_an_aborted_migration_rolls_the_new_column_back(stock, monkeypatch):
    before = installation.dump(stock)

    def boom(self, cursor):
        columns = [row[1] for row in cursor.execute('PRAGMA table_info(Accounts)')]
        assert NEW_COLUMN in columns                                            # die Spalte ist schon angelegt …
        raise RuntimeError('Abbruch mitten in der Umstellung')                  # … und der Schritt danach scheitert

    monkeypatch.setattr(Database, '_ensure_invoice_version_schema', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(stock)
    finally:
        monkeypatch.undo()

    assert installation.user_version(stock) == 2004
    assert NEW_COLUMN not in _names(stock)
    assert installation.dump(stock) == before                                   # Spalten, Tabellen, Zeilen wie vorher

    _reopen(stock)                                                              # und beim nächsten Start klappt es
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[-1] == NEW_COLUMN


def test_a_column_that_is_already_there_does_not_stop_the_migration(stock):
    with closing(sqlite3.connect(stock)) as con:
        con.execute('ALTER TABLE Accounts ADD COLUMN BusinessOnly INTEGER NOT NULL DEFAULT 1')   # halb durchgeführt
        con.commit()

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock) == [*OLD_ACCOUNT_COLUMNS, NEW_COLUMN]                  # nicht doppelt


def test_the_new_column_is_not_null_with_the_default_one(stock):
    _reopen(stock)
    (info,) = [row for row in _info(stock, 'Accounts') if row[0] == NEW_COLUMN]
    assert info == (NEW_COLUMN, 'INTEGER', 1, '1', 0)                          # NOT NULL, Vorgabe 1, kein PK
    with closing(sqlite3.connect(stock)) as con:
        with pytest.raises(sqlite3.IntegrityError, match='NOT NULL'):
            con.execute('UPDATE Accounts SET BusinessOnly = NULL')              # NULL gibt es nicht: nicht einmal per SQL


# ── Die DB-Schicht schreibt nur 0 und 1 ──────────────────────────────────────

def test_a_new_account_is_a_business_account_unless_said_otherwise(tmp_db):
    tmp_db.insert_account('Giro Alpha', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', skr_account=1800)
    tmp_db.insert_account('Giro Beta', 'Erika Probe', IBAN_MIXED, 'TESTDEFF', 'Testbank', skr_account=1810,
                          business_only=False)
    by_name = {row[1]: row for row in tmp_db.fetch_accounts()}
    assert by_name['Giro Alpha'][8] == 1 and by_name['Giro Beta'][8] == 0
    assert by_name['Kasse'][8] == 1                                               # die Kasse trägt die Vorgabe


@pytest.mark.parametrize('value', [2, -1, 'ja', '1', None, 0.5, 1.0 + 1, [1]])
def test_insert_account_refuses_anything_but_zero_or_one(tmp_db, value):
    with pytest.raises(ValueError):
        tmp_db.insert_account('Giro Alpha', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', business_only=value)
    assert [row[1] for row in tmp_db.fetch_accounts()] == ['Kasse']              # nichts geschrieben


@pytest.mark.parametrize('value, stored', [(True, 1), (False, 0), (1, 1), (0, 0)])
def test_insert_account_stores_exactly_zero_or_one(tmp_db, value, stored):
    tmp_db.insert_account('Giro Alpha', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', business_only=value)
    (account,) = [row for row in tmp_db.fetch_accounts() if row[1] == 'Giro Alpha']
    assert account[8] == stored and type(account[8]) is int


def test_update_account_leaves_the_flag_unless_told(tmp_db):
    tmp_db.insert_account('Giro Alpha', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', skr_account=1800,
                          business_only=False)
    (account_id,) = [row[0] for row in tmp_db.fetch_accounts() if row[1] == 'Giro Alpha']

    tmp_db.update_account(account_id, 'Giro Alpha 2', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', 1800)
    assert _stored(tmp_db, account_id) == 0                                       # None = unverändert
    tmp_db.update_account(account_id, 'Giro Alpha 2', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', 1800,
                          business_only=True)
    assert _stored(tmp_db, account_id) == 1
    tmp_db.update_account(account_id, 'Giro Alpha 2', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', 1800,
                          business_only=False)
    assert _stored(tmp_db, account_id) == 0
    tmp_db.update_account(account_id, 'Giro Alpha 3', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', 1800,
                          business_only=None)
    assert _stored(tmp_db, account_id) == 0 and tmp_db.get_account_by_id(account_id)[1] == 'Giro Alpha 3'


@pytest.mark.parametrize('value', [2, 'ja', '0', 0.5, [0]])
def test_update_account_refuses_anything_but_zero_or_one_and_writes_nothing(tmp_db, value):
    tmp_db.insert_account('Giro Alpha', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', skr_account=1800)
    (account_id,) = [row[0] for row in tmp_db.fetch_accounts() if row[1] == 'Giro Alpha']
    with pytest.raises(ValueError):
        tmp_db.update_account(account_id, 'Anderer Name', 'Erika Probe', IBAN_BUSINESS, 'TESTDEFF', 'Testbank', 1800,
                              business_only=value)
    assert tmp_db.get_account_by_id(account_id)[1] == 'Giro Alpha' and _stored(tmp_db, account_id) == 1
