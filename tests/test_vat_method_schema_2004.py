# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.4.0 (Schema 2004): die Besteuerungsart der eigenen Firma.

``Contacts`` bekommt am Ende die Spalte ``VatMethod`` (Text, NULL = nicht festgelegt). Geprüft wird:

* Eine frische und eine migrierte Datenbank haben dieselbe Spaltenreihenfolge (die Kontakt-Abfrage liest die Zeilen
  positionsbasiert, ``VatMethod`` ist dort Index 34), dieselben Tabellen und Indizes – auch aus 2.2.x, 2.1.x, 2.0.x und 1.x.
* Der Bestand bleibt, wie er war: jede Spalte jeder Zeile, alle anderen Tabellen; der neue Wert ist NULL – nichts wird
  umgedeutet.
* Vor der Umstellung entsteht eine Sicherung (Stand 2003), scheitert sie, bleibt die Datenbank unberührt; bricht die
  Umstellung ab, rollt sie ganz zurück, und ein zweiter Start klappt.

Namen und Nummern sind erfundene Testwerte.
"""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import version
from db import Database, MigrationBackupError, _CoreMixin
from db.schema import VAT_METHOD_SCHEMA
from server.pages_miscellaneous import PageMiscellaneous
from tests import installation
from tests.document_helpers import add_contacts
from tests.entry_form_world import to_state_2000, to_state_2001, to_state_2002, to_state_2003

NEW_COLUMN = 'VatMethod'
OLD_CONTACT_COLUMNS = ['ID', 'ContactType', 'EntityType', 'DisplayName', 'CustomerNumber', 'Abbreviation', 'Email', 'Phone',
                       'Notes', 'Logo']


def _reopen(path):
    """Die Datei so öffnen, als wäre das Programm neu gestartet worden."""
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def _info(path, table):
    with closing(sqlite3.connect(path)) as con:
        return [row[1:] for row in con.execute(f'PRAGMA table_info({table})')]     # ohne die laufende Nummer


def _names(path, table='Contacts'):
    return [row[0] for row in _info(path, table)]


def _backups(path):
    directory = Path(path).parent / 'backup'
    return sorted(directory.iterdir()) if directory.is_dir() else []


def _contents(path):
    with closing(sqlite3.connect(path)) as con:
        tables = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        return {t: con.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables}


@pytest.fixture
def stock(tmp_path):
    """Ein Bestand im Stand von 2.3.x: eigene Firma, Kunde und eine eigene Person, eine Bankbewegung mit Buchungssatz."""
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    add_contacts(db)
    db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                      first_name='Erika', last_name='Probe')
    coa = db.get_coa_id_by_account_number
    bank = db.insert_booking('2026-03-05', -119.0, booking_type='bank', text='Bürobedarf')
    db.insert_booking('2026-03-05', -119.0, coa_id=coa(6815), counter_coa_id=coa(1800), tax_rate=0.19,
                      tax_amount=-19.0, document_number='ER-1', parent_booking_id=bank)
    to_state_2003(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


# ── Der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_stock_is_a_2_3_x_database_before_the_migration(stock):
    assert installation.user_version(stock) == 2003
    assert _names(stock) == OLD_CONTACT_COLUMNS


def test_this_program_is_a_2_4_x_program():
    assert VAT_METHOD_SCHEMA == 2004
    assert version.SCHEMA_VERSION >= 2004 and version.SCHEMA_STEP >= 4
    assert version.APP_VERSION.startswith('2.') and int(version.APP_VERSION.split('.')[1]) >= 4


# ── Frisch = migriert ────────────────────────────────────────────────────────

def test_a_fresh_and_a_migrated_database_have_the_same_columns_tables_and_indexes(stock, tmp_path):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    _reopen(stock)

    assert _names(stock) == [*OLD_CONTACT_COLUMNS, NEW_COLUMN] == _names(fresh)
    assert _info(stock, 'Contacts') == _info(fresh, 'Contacts')                  # Name, Typ, NOT NULL, Vorgabe, PK
    with closing(sqlite3.connect(stock)) as a, closing(sqlite3.connect(fresh)) as b:
        for query in ("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
                      "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            assert [r[0] for r in a.execute(query)] == [r[0] for r in b.execute(query)]
    assert installation.user_version(stock) == installation.user_version(fresh) == version.SCHEMA_VERSION


@pytest.mark.parametrize('older', [to_state_2002, to_state_2001, to_state_2000, None],
                         ids=['aus-2.2.x', 'aus-2.1.x', 'aus-2.0.x', 'aus-1.x'])
def test_older_stocks_end_with_the_same_column_order_as_a_fresh_database(stock, tmp_path, older):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    if older is not None:
        older(stock)
    else:
        installation.stamp_user_version(stock, 4)
    _reopen(stock)

    for table in ('Contacts', 'Bookings', 'CompanyDetails', 'Invoices'):
        assert _info(stock, table) == _info(fresh, table), table
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_2_2_x_stock_goes_through_both_steps_in_one_backed_up_run(stock):
    to_state_2002(stock)
    assert installation.user_version(stock) == 2002

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock)[-1] == NEW_COLUMN and 'DatevKey' in _names(stock, 'Bookings')
    assert _names(stock, 'CompanyDetails')[-2:] == ['DatevConsultantNo', 'DatevClientNo']
    (backup,) = _backups(stock)
    assert installation.user_version(str(backup)) == 2002


def test_the_column_position_of_the_contact_query_matches_the_schema(stock):
    db = _reopen(stock)
    assert _names(stock).index(NEW_COLUMN) == len(OLD_CONTACT_COLUMNS)           # die neue Spalte steht ganz am Ende
    own = next(row for row in db.fetch_contacts(contact_type='own') if row[3] == 'Eigene Firma')
    row = db.get_contact_by_id(own[0])
    assert len(row) == 35 and row[34] is None and row['vat_method'] is None
    assert row[3] == 'Eigene Firma' and row[11] == 'DE123456789' and row[32] is None and row[33] is None


# ── Der Bestand bleibt ───────────────────────────────────────────────────────

def test_existing_rows_stay_untouched_and_the_new_value_is_empty(stock):
    before = _contents(stock)
    assert len(before['Contacts']) == 3 and len(before['Bookings']) == 2

    _reopen(stock)

    after = _contents(stock)
    assert all(row[-1] is None for row in after['Contacts'])                      # überall NULL – nichts wird umgedeutet
    assert [row[:-1] for row in after['Contacts']] == before['Contacts']          # jede Spalte jeder Zeile
    # Das Öffnen stellt bis auf den heutigen Stand um: Schema 2005 (2.5.0) hängt ``Accounts.BusinessOnly`` an (überall 1 =
    # reines Geschäftskonto), geprüft in ``test_business_only_schema_2005.py``
    assert all(row[-1] == 1 for row in after['Accounts'])
    after['Accounts'] = [row[:-1] for row in after['Accounts']]
    for table, rows in before.items():
        if table != 'Contacts':
            assert after[table] == rows, table                                    # alle anderen Tabellen


def test_nobody_has_a_tax_method_after_the_migration(stock):
    db = _reopen(stock)
    assert db.get_vat_method() is None
    assert 'Soll-Versteuerung eingetragen' not in PageMiscellaneous(db)


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
    assert installation.user_version(str(backup)) == 2003
    assert NEW_COLUMN not in _names(str(backup))
    assert installation.dump(str(backup)) == before                             # Zeile für Zeile der alte Stand
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_failing_backup_stops_the_opening_and_leaves_the_stock_at_2003(stock):
    before = installation.dump(stock)
    (Path(stock).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError):
        _reopen(stock)

    assert installation.user_version(stock) == 2003 and installation.dump(stock) == before
    assert NEW_COLUMN not in _names(stock)
    (Path(stock).parent / 'backup').unlink()                                    # Ursache behoben: jetzt klappt es
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[-1] == NEW_COLUMN


def test_an_aborted_migration_rolls_the_new_column_back(stock, monkeypatch):
    before = installation.dump(stock)

    def boom(self, cursor):
        columns = [row[1] for row in cursor.execute('PRAGMA table_info(Contacts)')]
        assert NEW_COLUMN in columns                                            # die Spalte ist schon angelegt …
        raise RuntimeError('Abbruch mitten in der Umstellung')                  # … und der Schritt danach scheitert

    monkeypatch.setattr(Database, '_ensure_invoice_version_schema', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(stock)
    finally:
        monkeypatch.undo()

    assert installation.user_version(stock) == 2003
    assert NEW_COLUMN not in _names(stock)
    assert installation.dump(stock) == before                                   # Spalten, Tabellen, Zeilen wie vorher

    _reopen(stock)                                                              # und beim nächsten Start klappt es
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[-1] == NEW_COLUMN


def test_a_column_that_is_already_there_does_not_stop_the_migration(stock):
    with closing(sqlite3.connect(stock)) as con:
        con.execute('ALTER TABLE Contacts ADD COLUMN VatMethod TEXT')            # halb durchgeführt, Zahl noch 2003
        con.commit()

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock) == [*OLD_CONTACT_COLUMNS, NEW_COLUMN]                  # nicht doppelt


def test_the_new_column_takes_no_default_and_no_constraint(stock):
    _reopen(stock)
    (info,) = [row for row in _info(stock, 'Contacts') if row[0] == NEW_COLUMN]
    assert info == (NEW_COLUMN, 'TEXT', 0, None, 0)                            # nicht NOT NULL, ohne Vorgabe, kein PK
