# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.3.0 (Schema 2003): der BU-Schlüssel am Buchungssatz und die DATEV-Nummern der eigenen Firma.

``Bookings`` bekommt am Ende die Spalte ``DatevKey`` (Index 23), ``CompanyDetails`` die Spalten
``DatevConsultantNo`` und ``DatevClientNo``; ``TaxKeys`` wird berichtigt und ergänzt (100, 120, 121, 122, 490).
Geprüft wird:

* Eine frische und eine migrierte Datenbank haben dieselbe Spaltenreihenfolge (die Buchungs-Zeilen werden
  positionsbasiert gelesen), dieselben Tabellen und Indizes – auch aus 2.1.x, 2.0.x und 1.x.
* Der Bestand bleibt, wie er war: jede Spalte jeder Zeile; die neuen Werte sind NULL – nichts wird umgedeutet.
* ``TaxKeys``: die fünf Zeilen stimmen mit ``seed_data/tax_keys.json`` überein, die übrigen bleiben; eine leere
  Tabelle wird beim Seeden vollständig angelegt.
* Vor der Umstellung entsteht eine Sicherung (Stand 2002), scheitert sie, bleibt die Datenbank unberührt; bricht
  die Umstellung ab, rollt sie ganz zurück, und ein zweiter Start klappt.

Namen und Nummern sind erfundene Testwerte.
"""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import version
from db import Database, MigrationBackupError, _CoreMixin
from db.schema import DATEV_KEY_SCHEMA, TAX_KEY_CORRECTIONS
from tests import installation
from tests.document_helpers import add_contacts
from tests.entry_form_world import to_state_2000, to_state_2001, to_state_2002

NEW_BOOKING_COLUMN = 'DatevKey'
NEW_COMPANY_COLUMNS = ['DatevConsultantNo', 'DatevClientNo']
SEED = Path(__file__).resolve().parent.parent / 'seed_data' / 'tax_keys.json'


def _reopen(path):
    """Die Datei so öffnen, als wäre das Programm neu gestartet worden."""
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def _info(path, table):
    with closing(sqlite3.connect(path)) as con:
        return [row[1:] for row in con.execute(f'PRAGMA table_info({table})')]     # ohne die laufende Nummer


def _names(path, table='Bookings'):
    return [row[0] for row in _info(path, table)]


def _backups(path):
    directory = Path(path).parent / 'backup'
    return sorted(directory.iterdir()) if directory.is_dir() else []


def _contents(path, skip=()):
    with closing(sqlite3.connect(path)) as con:
        tables = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        return {t: con.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables if t not in skip}


def _tax_keys(path, codes=None):
    with closing(sqlite3.connect(path)) as con:
        rows = con.execute('SELECT Code, Description, TaxRate, TaxType FROM TaxKeys ORDER BY Code').fetchall()
    return {row[0]: row for row in rows if codes is None or row[0] in codes}


@pytest.fixture
def stock(tmp_path):
    """Ein Bestand im Stand von 2.2.x: eigene Firma und Kunde, eine Bankbewegung mit Buchungssätzen, eine
    Forderung, eine eigenständige Buchung."""
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    add_contacts(db)
    coa = db.get_coa_id_by_account_number
    bank = db.insert_booking('2026-03-05', -119.0, booking_type='bank', text='Bürobedarf')
    db.insert_booking('2026-03-05', -119.0, coa_id=coa(6815), counter_coa_id=coa(1800), tax_rate=0.19,
                      tax_amount=-19.0, document_number='ER-1', parent_booking_id=bank)
    db.insert_booking('2026-03-06', 119.0, coa_id=coa(10000), counter_coa_id=coa(4405), tax_rate=0.19,
                      tax_amount=19.0, document_number='26R001', entry_form='transfer')
    db.insert_booking('2026-03-07', -50.0, coa_id=coa(6815), counter_coa_id=coa(1460), document_number='ER-2')
    to_state_2002(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


# ── Der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_stock_is_a_2_2_x_database_before_the_migration(stock):
    assert installation.user_version(stock) == 2002
    assert NEW_BOOKING_COLUMN not in _names(stock) and len(_names(stock)) == 23
    assert not set(NEW_COMPANY_COLUMNS) & set(_names(stock, 'CompanyDetails'))
    keys = _tax_keys(stock, {'100', '120', '121', '122', '490'})
    assert set(keys) == {'121', '490'} and keys['121'][3] == 'steuerfrei' and keys['490'][2] == 0.0


def test_this_program_is_a_2_3_x_program():
    assert DATEV_KEY_SCHEMA == 2003
    assert version.SCHEMA_VERSION >= 2003 and version.SCHEMA_STEP >= 3
    assert version.APP_VERSION.startswith('2.') and int(version.APP_VERSION.split('.')[1]) >= 3


# ── Frisch = migriert ────────────────────────────────────────────────────────

def test_a_fresh_and_a_migrated_database_have_the_same_columns_tables_and_indexes(stock, tmp_path):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    _reopen(stock)

    assert _names(stock)[-2:] == ['EntryForm', NEW_BOOKING_COLUMN] and len(_names(stock)) == 24
    assert _names(stock, 'CompanyDetails')[-2:] == NEW_COMPANY_COLUMNS
    for table in ('Bookings', 'CompanyDetails'):
        assert _info(stock, table) == _info(fresh, table), table                # Name, Typ, NOT NULL, Vorgabe, PK
    with closing(sqlite3.connect(stock)) as a, closing(sqlite3.connect(fresh)) as b:
        for query in ("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
                      "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            assert [r[0] for r in a.execute(query)] == [r[0] for r in b.execute(query)]
    assert installation.user_version(stock) == installation.user_version(fresh) == version.SCHEMA_VERSION


@pytest.mark.parametrize('older', [to_state_2001, to_state_2000, None], ids=['aus-2.1.x', 'aus-2.0.x', 'aus-1.x'])
def test_older_stocks_end_with_the_same_column_order_as_a_fresh_database(stock, tmp_path, older):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    if older is not None:
        older(stock)
    else:
        installation.stamp_user_version(stock, 4)
    _reopen(stock)

    for table in ('Bookings', 'CompanyDetails', 'Invoices'):
        assert _info(stock, table) == _info(fresh, table), table
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_the_column_positions_of_the_row_reader_match_the_schema(stock):
    db = _reopen(stock)
    names = _names(stock)
    assert names[22] == 'EntryForm' and names[23] == NEW_BOOKING_COLUMN        # der Export liest Spalte 22 und 23
    assert names[21] == 'SourceGroup' and names[16] == 'DocumentNumber'
    rows = db.fetch_bookings_range('2026-03-01', '2026-03-31')
    assert rows and all(len(row) == 24 and row[23] is None for row in rows)


# ── Der Bestand bleibt ───────────────────────────────────────────────────────

def test_existing_rows_stay_untouched_and_the_new_values_are_empty(stock):
    before = _contents(stock, skip={'TaxKeys'})
    assert len(before['Bookings']) == 4 and len(before['CompanyDetails']) == 2

    _reopen(stock)

    after = _contents(stock, skip={'TaxKeys'})
    assert [row[:23] for row in after['Bookings']] == before['Bookings']          # jede Spalte jeder Zeile
    assert all(row[23] is None for row in after['Bookings'])
    assert [row[:5] for row in after['CompanyDetails']] == before['CompanyDetails']
    assert all(row[5:] == (None, None) for row in after['CompanyDetails'])
    for table, rows in before.items():
        if table not in ('Bookings', 'CompanyDetails'):
            assert after[table] == rows, table                                    # alle anderen Tabellen


def test_the_own_company_has_no_numbers_after_the_migration(stock):
    db = _reopen(stock)
    assert db.get_datev_numbers() == (None, None)


# ── TaxKeys ──────────────────────────────────────────────────────────────────

def test_the_tax_keys_are_corrected_and_completed(stock):
    others_before = {code: row for code, row in _tax_keys(stock).items() if code not in ('100', '120', '121', '122', '490')}

    _reopen(stock)

    after = _tax_keys(stock)
    assert after['100'] == ('100', 'Umsatzsteuer 0 %', 0.0, 'USt')
    assert after['120'] == ('120', 'Verwendung von Gegenständen 0 %', 0.0, 'USt')
    assert after['121'] == ('121', 'Verwendung von Gegenständen 19 %', 0.19, 'USt')          # bisher „steuerfrei“
    assert after['122'] == ('122', 'Verwendung von Gegenständen 7 %', 0.07, 'USt')
    assert after['490'] == ('490', 'ohne Vorsteuerabzug', None, 'keine')                    # kein Satz
    assert {code: row for code, row in after.items() if code in others_before} == others_before
    assert len(after) == len(others_before) + 5 == 53


def test_a_correct_row_is_left_alone_and_a_wrong_row_is_corrected_in_place(stock):
    """Kein ``INSERT OR REPLACE``: Eine schon richtige Zeile wird nicht neu geschrieben, eine falsche an ihrer Stelle
    berichtigt – ihre ``rowid`` bleibt, keine Zeile wandert ans Ende der Tabelle (die Reihenfolge eines Dumps bliebe sonst
    nicht, wie sie war)."""
    with closing(sqlite3.connect(stock)) as con:
        con.execute("INSERT INTO TaxKeys (Code, Description, TaxRate, TaxType) VALUES ('100', 'Umsatzsteuer 0 %', 0.0, 'USt')")
        con.commit()
        before = dict(con.execute('SELECT Code, rowid FROM TaxKeys'))
    assert _tax_keys(stock)['121'][1] == 'Steuerfrei (Kleinunternehmer etc.)'            # falsch (2.2.x)

    _reopen(stock)

    with closing(sqlite3.connect(stock)) as con:
        after = dict(con.execute('SELECT Code, rowid FROM TaxKeys'))
    assert _tax_keys(stock)['121'][1:] == ('Verwendung von Gegenständen 19 %', 0.19, 'USt')
    assert all(after[code] == rowid for code, rowid in before.items())                  # 100, 121, 490 und alle übrigen
    assert set(after) - set(before) == {'120', '122'}                                   # nur die fehlenden kamen dazu


def test_the_corrections_are_the_rows_of_the_seed_file(tmp_path):
    seed = {entry['code']: (entry['code'], entry['description'], entry['tax_rate'], entry['tax_type'])
            for entry in json.loads(SEED.read_text(encoding='utf-8'))}
    assert len(seed) == 53
    for row in TAX_KEY_CORRECTIONS:
        assert seed[row[0]] == row
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    assert _tax_keys(fresh) == seed                                                    # die frische Datenbank = die Datei


def test_an_empty_tax_key_table_is_seeded_completely_and_not_with_five_rows(stock):
    with closing(sqlite3.connect(stock)) as con:
        con.execute('DELETE FROM TaxKeys')
        con.commit()

    _reopen(stock)

    assert len(_tax_keys(stock)) == 53 and _tax_keys(stock)['401'][2] == 0.19


def test_a_second_start_changes_nothing_and_makes_no_second_backup(stock):
    _reopen(stock)
    after_first, backups = installation.dump(stock), _backups(stock)

    _reopen(stock)
    _reopen(stock)

    assert installation.dump(stock) == after_first and _backups(stock) == backups


def test_a_tax_key_changed_by_the_user_after_the_migration_stays_changed(stock):
    _reopen(stock)
    with closing(sqlite3.connect(stock)) as con:
        con.execute("UPDATE TaxKeys SET Description = 'eigene Bezeichnung' WHERE Code = '100'")
        con.commit()

    _reopen(stock)

    assert _tax_keys(stock)['100'][1] == 'eigene Bezeichnung'


# ── Sicherung, Abbruch, Wiederholung ─────────────────────────────────────────

def test_the_migration_makes_a_backup_of_the_old_state_first(stock):
    before = installation.dump(stock)

    _reopen(stock)

    (backup,) = _backups(stock)
    assert backup.name.endswith(f'_vor_{version.APP_VERSION}_buch.db') and backup.name[:8].isdigit()
    assert installation.user_version(str(backup)) == 2002
    assert NEW_BOOKING_COLUMN not in _names(str(backup))
    assert installation.dump(str(backup)) == before                             # Zeile für Zeile der alte Stand
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_failing_backup_stops_the_opening_and_leaves_the_stock_at_2002(stock):
    before = installation.dump(stock)
    (Path(stock).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError):
        _reopen(stock)

    assert installation.user_version(stock) == 2002 and installation.dump(stock) == before
    assert NEW_BOOKING_COLUMN not in _names(stock)
    (Path(stock).parent / 'backup').unlink()                                    # Ursache behoben: jetzt klappt es
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_an_aborted_migration_rolls_everything_back(stock, monkeypatch):
    before = installation.dump(stock)

    def boom(self, cursor):
        cursor.execute("INSERT OR REPLACE INTO TaxKeys VALUES ('100', 'halb', 1.0, 'USt')")     # mitten im Berichtigen
        raise RuntimeError('Abbruch mitten in der Umstellung')

    monkeypatch.setattr(Database, '_correct_tax_keys', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(stock)
    finally:
        monkeypatch.undo()

    assert installation.user_version(stock) == 2002
    assert NEW_BOOKING_COLUMN not in _names(stock) and not set(NEW_COMPANY_COLUMNS) & set(_names(stock, 'CompanyDetails'))
    assert installation.dump(stock) == before                                   # Spalten, Tabellen, Zeilen wie vorher

    _reopen(stock)                                                              # und beim nächsten Start klappt es
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[-1] == NEW_BOOKING_COLUMN


def test_columns_that_are_already_there_do_not_stop_the_migration(stock):
    with closing(sqlite3.connect(stock)) as con:
        con.execute('ALTER TABLE Bookings ADD COLUMN DatevKey INTEGER')           # halb durchgeführt, Zahl noch 2002
        con.commit()

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock)[-1] == NEW_BOOKING_COLUMN and _names(stock, 'CompanyDetails')[-2:] == NEW_COMPANY_COLUMNS


def test_the_new_columns_take_no_default_and_no_constraint(stock):
    _reopen(stock)
    for table, column in (('Bookings', 'DatevKey'), ('CompanyDetails', 'DatevConsultantNo'),
                          ('CompanyDetails', 'DatevClientNo')):
        (info,) = [row for row in _info(stock, table) if row[0] == column]
        assert info[2] == 0 and info[3] is None and info[4] == 0, (column, info)     # nicht NOT NULL, ohne Vorgabe, kein PK
