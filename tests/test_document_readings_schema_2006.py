# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.6.0 (Schema 2006): die Lesung eines Belegs (``DocumentReadings``) und ihre DB-Schicht.

Neu ist die Tabelle ``DocumentReadings`` (eine Zeile je Beleg: die aus der Datei gelesenen Felder, getrennt von dem, was der
Nutzer am Beleg pflegt) samt Index auf dem Bruttobetrag; keine vorhandene Tabelle ändert sich. Geprüft wird:

* Eine frische und eine migrierte Datenbank haben dieselbe Tabelle (Spalten, Fremdschlüssel, SQL), dieselben Tabellen und
  Indizes – auch aus 2.4.x, 2.3.x, 2.2.x, 2.1.x, 2.0.x und 1.x.
* Der Bestand bleibt, wie er war; die neue Tabelle ist leer, ein Beleg mit Datei gilt als ungelesen – nichts wird umgedeutet.
* Vor der Umstellung entsteht eine Sicherung (Stand 2005), scheitert sie, bleibt die Datenbank unberührt; bricht die Umstellung
  nach dem Anlegen der Tabelle ab, rollt sie ganz zurück (auch die Tabelle), und ein zweiter Start klappt.
* ``save_document_reading`` schreibt höchstens eine Lesung je Beleg (die vorhandene wird ersetzt), lehnt einen gelöschten Beleg
  und einen geänderten Pfad ab, ohne etwas zu schreiben, nimmt nur bekannte Spalten und schreibt nie ins Replay-Log.
* ``delete_receipt`` nimmt die Lesung mit (die Lesung sperrt das Löschen nicht, eine Ablehnung lässt sie stehen), ein rohes
  ``DELETE`` auf ``Documents`` kaskadiert, und die Vorlage „Zeitbereich löschen“ der SQL-Konsole läuft im Schreibmodus durch.
* ``document_reading_summaries`` liest nie ``Text`` oder ``Raw``; „ungelesen“ zählt Belege mit Datei ohne Lesung oder mit älterem
  Leser; ``own_identity`` liefert die Namen und nur die echten USt-IdNrn. der eigenen Firmen.

Namen und Nummern sind erfundene Testwerte; die USt-IdNrn. stammen aus der erlaubten Liste.
"""
import contextlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import version
from db import Database, DeleteRefused, MigrationBackupError, _CoreMixin
from db.document_readings import READING_COLUMNS, UNREAD_LIMIT_MAX
from db.schema import DOCUMENT_READINGS_SCHEMA
from money import to_minor
from server.handlers import handle_execute_sql
from tests import installation
from tests.document_helpers import add_contacts
from tests.entry_form_world import (to_state_2000, to_state_2001, to_state_2002, to_state_2003, to_state_2004,
                                    to_state_2005)
from tests.test_invoice_schema_2002 import _range_delete_script

NEW_TABLE = 'DocumentReadings'
NEW_INDEX = 'idx_documentreadings_gross'
DAY = '2026-03-04'
FILE = 'Belege/2026/B-1_beleg.pdf'

#: Die Tabelle, wie sie 2.6.0 anlegt: (Name, Typ, NOT NULL, Vorgabe, Teil des Primärschlüssels)
EXPECTED_COLUMNS = [
    ('ID', 'INTEGER', 0, None, 1), ('Document_ID', 'INTEGER', 1, None, 0), ('Status', 'TEXT', 1, None, 0),
    ('Source', 'TEXT', 0, None, 0), ('ReaderVersion', 'INTEGER', 1, None, 0), ('ReadAt', 'DATETIME', 1, None, 0),
    ('DocDate', 'DATE', 0, None, 0), ('GrossMinor', 'INTEGER', 0, None, 0), ('NetMinor', 'INTEGER', 0, None, 0),
    ('TaxMinor', 'INTEGER', 0, None, 0), ('TaxRate', 'REAL', 0, None, 0), ('Currency', 'TEXT', 0, None, 0),
    ('SenderName', 'TEXT', 0, None, 0), ('SenderVatId', 'TEXT', 0, None, 0), ('InvoiceNumber', 'TEXT', 0, None, 0),
    ('Kind', 'TEXT', 0, None, 0), ('Raw', 'TEXT', 0, None, 0), ('Text', 'TEXT', 0, None, 0)]
FOREIGN_KEY = [(0, 0, 'Documents', 'Document_ID', 'ID', 'NO ACTION', 'CASCADE', 'NONE')]


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _reopen(path):
    """Die Datei so öffnen, als wäre das Programm neu gestartet worden."""
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def _info(path, table):
    with closing(sqlite3.connect(path)) as con:
        return [row[1:] for row in con.execute(f'PRAGMA table_info({table})')]     # ohne die laufende Nummer


def _names(path, table):
    return [row[0] for row in _info(path, table)]


def _tables(path):
    with closing(sqlite3.connect(path)) as con:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _index_names(path):
    with closing(sqlite3.connect(path)) as con:
        return [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _sql_of(path, kind, name):
    with closing(sqlite3.connect(path)) as con:
        row = con.execute('SELECT tbl_name, sql FROM sqlite_master WHERE type = ? AND name = ?', (kind, name)).fetchone()
    return row


def _foreign_keys(path, table):
    with closing(sqlite3.connect(path)) as con:
        return con.execute(f'PRAGMA foreign_key_list({table})').fetchall()


def _backups(path):
    directory = Path(path).parent / 'backup'
    return sorted(directory.iterdir()) if directory.is_dir() else []


def _contents(path, skip=()):
    """Alle Zeilen aller Tabellen (ohne die in ``skip``) – ein Stand zum Vergleichen."""
    with closing(sqlite3.connect(path)) as con:
        tables = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        return {t: con.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables if t not in skip}


def raw(db, sql, *args):
    with closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql, args).fetchall()


def run_sql(db, sql, *args):
    with closing(sqlite3.connect(db.db_name)) as con:
        con.execute(sql, args)
        con.commit()


def assert_not_locked(db):
    """Die Datenbank ist frei: Eine zweite Verbindung bekommt die Schreibsperre sofort."""
    with closing(sqlite3.connect(db.db_name, timeout=0.5)) as con:
        con.execute('BEGIN IMMEDIATE')
        con.rollback()


def add_receipt(db, number='B-1', path=FILE, date=DAY):
    """Einen Beleg anlegen; liefert seine ID."""
    db.insert_receipt(number, date, 'beleg.pdf', path, 'Info')
    return db.fetch_receipts()[-1][0]


def reading(**over):
    """Eine vollständige Lesung (Schlüssel = Spalten); ``over`` überschreibt Felder."""
    row = {'Status': 'ok', 'Source': 'text', 'ReaderVersion': 1, 'ReadAt': '2026-03-05 10:00:00', 'DocDate': DAY,
           'GrossMinor': to_minor('119.00'), 'NetMinor': to_minor('100.00'), 'TaxMinor': to_minor('19.00'), 'TaxRate': 0.19,
           'Currency': 'EUR', 'SenderName': 'Lieferant Probe GmbH', 'SenderVatId': 'DE987654321',
           'InvoiceNumber': 'RE-2026-17', 'Kind': 'invoice', 'Raw': '{"GrossMinor": "Zeile 12"}',
           'Text': 'Rechnung RE-2026-17 über 119,00 EUR'}
    row.update(over)
    return row


@pytest.fixture
def logged(monkeypatch):
    """Alles, was die Datenbankschicht ins Replay-Log schreiben will, als ``(SQL, Parameter, Beschreibung)`` – ohne Datei."""
    entries = []
    monkeypatch.setattr(Database, '_log_sql', lambda self, sql, params, description: entries.append((sql, params, description)))
    return entries


@pytest.fixture
def stock(tmp_path):
    """Ein Bestand im Stand von 2.5.x: eigene Firma und Kunde, ein Beleg mit Datei, ein Papierbeleg, eine Bankbewegung,
    verknüpft mit dem ersten Beleg."""
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    add_contacts(db)
    first = add_receipt(db, 'B-1', FILE)
    add_receipt(db, 'B-2', '', '2026-03-05')
    db.link_booking_to_document(db.insert_booking('2026-03-05', -119.0, booking_type='bank'), first)
    to_state_2005(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


# ── Der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_stock_is_a_2_5_x_database_before_the_migration(stock):
    assert installation.user_version(stock) == 2005
    assert NEW_TABLE not in _tables(stock) and _sql_of(stock, 'index', NEW_INDEX) is None
    assert len(_contents(stock)['Documents']) == 2


def test_the_step_back_to_2005_can_be_repeated(stock):
    to_state_2005(stock)
    to_state_2005(stock)
    assert installation.user_version(stock) == 2005 and NEW_TABLE not in _tables(stock)


def test_this_program_is_a_2_6_x_program():
    assert DOCUMENT_READINGS_SCHEMA == 2006
    assert version.SCHEMA_VERSION >= 2006 and version.SCHEMA_STEP >= 6
    assert version.APP_VERSION.startswith('2.') and int(version.APP_VERSION.split('.')[1]) >= 6


# ── Frisch = migriert ────────────────────────────────────────────────────────

def test_the_table_is_what_the_schema_says(tmp_db):
    assert _info(tmp_db.db_name, NEW_TABLE) == EXPECTED_COLUMNS
    assert _foreign_keys(tmp_db.db_name, NEW_TABLE) == FOREIGN_KEY
    assert _sql_of(tmp_db.db_name, 'index', NEW_INDEX)[0] == NEW_TABLE
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        assert [row[2] for row in con.execute(f'PRAGMA index_info({NEW_INDEX})')] == ['GrossMinor']
    assert READING_COLUMNS == tuple(name for name, *_ in EXPECTED_COLUMNS[2:])     # die Liste der DB-Schicht = die Tabelle


@pytest.mark.parametrize('older', [None, to_state_2004, to_state_2003, to_state_2002, to_state_2001, to_state_2000, 'one'],
                         ids=['aus-2.5.x', 'aus-2.4.x', 'aus-2.3.x', 'aus-2.2.x', 'aus-2.1.x', 'aus-2.0.x', 'aus-1.x'])
def test_a_fresh_and_a_migrated_database_have_the_same_table_tables_and_indexes(stock, tmp_path, older):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    if older == 'one':
        installation.stamp_user_version(stock, 4)
    elif older is not None:
        older(stock)
    _reopen(stock)

    assert _info(stock, NEW_TABLE) == _info(fresh, NEW_TABLE) == EXPECTED_COLUMNS
    assert _foreign_keys(stock, NEW_TABLE) == _foreign_keys(fresh, NEW_TABLE) == FOREIGN_KEY
    assert _sql_of(stock, 'table', NEW_TABLE) == _sql_of(fresh, 'table', NEW_TABLE) is not None
    assert _sql_of(stock, 'index', NEW_INDEX) == _sql_of(fresh, 'index', NEW_INDEX) is not None
    assert _tables(stock) == _tables(fresh) and _index_names(stock) == _index_names(fresh)
    for table in ('Documents', 'Accounts', 'Contacts', 'Bookings', 'CompanyDetails', 'Invoices'):
        assert _info(stock, table) == _info(fresh, table), table                     # sonst ändert sich nichts
    assert installation.user_version(stock) == installation.user_version(fresh) == version.SCHEMA_VERSION


def test_a_2_4_x_stock_goes_through_both_steps_in_one_backed_up_run(stock):
    to_state_2004(stock)
    assert installation.user_version(stock) == 2004

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert NEW_TABLE in _tables(stock) and 'BusinessOnly' in _names(stock, 'Accounts')
    (backup,) = _backups(stock)
    assert installation.user_version(str(backup)) == 2004


# ── Der Bestand bleibt ───────────────────────────────────────────────────────

def test_existing_rows_stay_untouched_and_the_new_table_is_empty(stock):
    before = _contents(stock)
    assert len(before['Documents']) == 2 and len(before['BookingDocuments']) == 1 and len(before['Contacts']) == 2

    _reopen(stock)

    after = _contents(stock)
    assert after.pop(NEW_TABLE) == []
    after.pop('TaxRates')                                                             # seit 2.8.0 angelegt und vorbelegt: test_tax_rates_schema_2008.py
    assert after.pop('BankTransactionDetails') == [] and after.pop('BookingExports') == []        # seit 2.10.0 angelegt, leer: test_schema_2010.py
    assert all(row[-3:] == (None, None, None) for row in after['Invoices'])           # seit 2.10.0 drei Spalten am Ende, leer (test_schema_2010.py)
    after['Invoices'] = [row[:-3] for row in after['Invoices']]
    assert after == before                                                            # jede Zeile jeder Tabelle


def test_after_the_migration_a_receipt_with_a_file_counts_as_unread_and_nothing_is_read(stock):
    db = _reopen(stock)
    (with_file, _paper) = [row[0] for row in db.fetch_receipts()]
    assert db.document_reading_summaries() == {} and db.get_document_reading(with_file) is None
    assert db.unread_receipt_ids(1) == [with_file] and db.count_unread_receipts(1) == 1


def test_a_second_start_changes_nothing_and_makes_no_second_backup(stock):
    _reopen(stock)
    after_first, backups = installation.dump(stock), _backups(stock)

    _reopen(stock)
    _reopen(stock)

    assert installation.dump(stock) == after_first and _backups(stock) == backups


def test_the_schema_step_can_run_again_on_a_current_database(tmp_db):
    before = installation.dump(tmp_db.db_name)
    with closing(tmp_db._get_connection()) as con:
        tmp_db._ensure_document_readings_schema(con.cursor())
        con.commit()
    assert installation.dump(tmp_db.db_name) == before


# ── Sicherung, Abbruch, Wiederholung ─────────────────────────────────────────

def test_the_migration_makes_a_backup_of_the_old_state_first(stock):
    before = installation.dump(stock)

    _reopen(stock)

    (backup,) = _backups(stock)
    assert backup.name.endswith(f'_vor_{version.APP_VERSION}_buch.db') and backup.name[:8].isdigit()
    assert installation.user_version(str(backup)) == 2005
    assert NEW_TABLE not in _tables(str(backup))
    assert installation.dump(str(backup)) == before                                  # Zeile für Zeile der alte Stand
    assert installation.user_version(stock) == version.SCHEMA_VERSION and NEW_TABLE in _tables(stock)


def test_a_failing_backup_stops_the_opening_and_leaves_the_stock_at_2005(stock):
    before = installation.dump(stock)
    (Path(stock).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError):
        _reopen(stock)

    assert installation.user_version(stock) == 2005 and installation.dump(stock) == before
    assert NEW_TABLE not in _tables(stock)
    (Path(stock).parent / 'backup').unlink()                                         # Ursache behoben: jetzt klappt es
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION and NEW_TABLE in _tables(stock)


def test_an_aborted_migration_rolls_the_new_table_back(stock, monkeypatch):
    before = installation.dump(stock)
    original = Database._ensure_document_readings_schema

    def boom(self, cursor):
        original(self, cursor)
        assert NEW_TABLE in {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        raise RuntimeError('Abbruch mitten in der Umstellung')                       # Tabelle und Index sind schon angelegt

    monkeypatch.setattr(Database, '_ensure_document_readings_schema', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(stock)
    finally:
        monkeypatch.undo()

    assert installation.user_version(stock) == 2005
    assert NEW_TABLE not in _tables(stock) and _sql_of(stock, 'index', NEW_INDEX) is None
    assert installation.dump(stock) == before                                        # Tabellen, Indizes, Zeilen wie vorher

    _reopen(stock)                                                                   # und beim nächsten Start klappt es
    assert installation.user_version(stock) == version.SCHEMA_VERSION and NEW_TABLE in _tables(stock)
    assert _info(stock, NEW_TABLE) == EXPECTED_COLUMNS


# ── Die Tabelle selbst ───────────────────────────────────────────────────────

def test_one_reading_per_receipt_and_a_missing_receipt_is_refused_by_the_database(tmp_db):
    receipt = add_receipt(tmp_db)
    insert = ('INSERT INTO DocumentReadings (Document_ID, Status, ReaderVersion, ReadAt) '
              "VALUES (?, 'ok', 1, '2026-03-05 10:00:00')")
    with closing(tmp_db._get_connection()) as con:
        con.execute(insert, (receipt,))
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            con.execute(insert, (receipt,))
        with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
            con.execute(insert, (receipt + 100,))
        con.rollback()


@pytest.mark.parametrize('column', ['Status', 'ReaderVersion', 'ReadAt', 'Document_ID'])
def test_the_four_required_columns_are_not_null(tmp_db, column):
    receipt = add_receipt(tmp_db)
    values = {'Document_ID': receipt, 'Status': 'ok', 'ReaderVersion': 1, 'ReadAt': '2026-03-05 10:00:00'}
    del values[column]
    with closing(tmp_db._get_connection()) as con:
        with pytest.raises(sqlite3.IntegrityError, match='NOT NULL'):
            con.execute(f"INSERT INTO DocumentReadings ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                        tuple(values.values()))


# ── save_document_reading / get_document_reading ─────────────────────────────

def test_a_reading_is_saved_and_read_back_with_all_columns(tmp_db):
    receipt = add_receipt(tmp_db)
    row = reading()

    assert tmp_db.save_document_reading(receipt, FILE, row) is True

    stored = tmp_db.get_document_reading(receipt)
    assert stored == {'ID': 1, 'Document_ID': receipt, **row}
    assert type(stored['GrossMinor']) is int and stored['GrossMinor'] == 1190000    # Minor Units der App (money.SCALE)
    assert type(stored['TaxRate']) is float and stored['TaxRate'] == 0.19
    assert tmp_db.get_document_reading(receipt + 1) is None


def test_a_missing_key_is_stored_as_null(tmp_db):
    receipt = add_receipt(tmp_db)
    assert tmp_db.save_document_reading(receipt, FILE, {'Status': 'empty', 'ReaderVersion': 2, 'ReadAt': '2026-03-05 10:00:00'})
    stored = tmp_db.get_document_reading(receipt)
    assert {key: value for key, value in stored.items() if value is not None} == {
        'ID': 1, 'Document_ID': receipt, 'Status': 'empty', 'ReaderVersion': 2, 'ReadAt': '2026-03-05 10:00:00'}


def test_saving_again_replaces_the_reading_of_the_receipt(tmp_db):
    receipt, other = add_receipt(tmp_db), add_receipt(tmp_db, 'B-2', 'Belege/2026/B-2_beleg.pdf')
    assert tmp_db.save_document_reading(receipt, FILE, reading())
    assert tmp_db.save_document_reading(other, 'Belege/2026/B-2_beleg.pdf', reading(InvoiceNumber='RE-2026-18'))

    assert tmp_db.save_document_reading(
        receipt, FILE, {'Status': 'partial', 'ReaderVersion': 2, 'ReadAt': '2026-04-01 08:00:00', 'GrossMinor': 5000000})

    assert raw(tmp_db, 'SELECT COUNT(*) FROM DocumentReadings WHERE Document_ID = ?', receipt) == [(1,)]
    stored = tmp_db.get_document_reading(receipt)
    assert (stored['ID'], stored['Status'], stored['ReaderVersion'], stored['GrossMinor']) == (1, 'partial', 2, 5000000)
    assert stored['SenderName'] is None and stored['Text'] is None and stored['Raw'] is None   # ersetzt, nicht ergänzt
    assert tmp_db.get_document_reading(other)['InvoiceNumber'] == 'RE-2026-18'                 # die andere bleibt
    assert raw(tmp_db, 'SELECT COUNT(*) FROM DocumentReadings') == [(2,)]


def test_a_changed_path_is_refused_and_the_old_reading_stays(tmp_db):
    receipt = add_receipt(tmp_db)
    assert tmp_db.save_document_reading(receipt, FILE, reading())
    before = tmp_db.get_document_reading(receipt)

    assert tmp_db.save_document_reading(receipt, 'Belege/2026/anderes.pdf', reading(Status='partial')) is False
    assert tmp_db.save_document_reading(receipt, '', reading(Status='partial')) is False

    assert tmp_db.get_document_reading(receipt) == before
    assert_not_locked(tmp_db)


def test_a_changed_path_writes_nothing_where_there_was_no_reading(tmp_db):
    receipt = add_receipt(tmp_db)
    assert tmp_db.save_document_reading(receipt, 'Belege/2026/anderes.pdf', reading()) is False
    assert raw(tmp_db, 'SELECT COUNT(*) FROM DocumentReadings') == [(0,)]


@pytest.mark.parametrize('stored, expected', [('', None), (None, ''), ('', ''), (None, None)],
                         ids=['leer-gegen-None', 'NULL-gegen-leer', 'leer-gegen-leer', 'NULL-gegen-None'])
def test_none_and_the_empty_path_count_as_the_same_path(tmp_db, stored, expected):
    receipt = add_receipt(tmp_db, path=stored)
    assert raw(tmp_db, 'SELECT Path FROM Documents WHERE ID = ?', receipt) == [(stored,)]
    assert tmp_db.save_document_reading(receipt, expected, reading()) is True


@pytest.mark.parametrize('expected', [None, ''])
def test_a_receipt_with_a_file_is_not_read_as_one_without(tmp_db, expected):
    receipt = add_receipt(tmp_db)
    assert tmp_db.save_document_reading(receipt, expected, reading()) is False
    assert tmp_db.get_document_reading(receipt) is None


def test_a_receipt_that_is_gone_is_refused(tmp_db):
    receipt = add_receipt(tmp_db)
    assert tmp_db.save_document_reading(99999, FILE, reading()) is False            # gab es nie
    tmp_db.delete_receipt(receipt)
    assert tmp_db.save_document_reading(receipt, FILE, reading()) is False          # inzwischen gelöscht
    assert raw(tmp_db, 'SELECT COUNT(*) FROM DocumentReadings') == [(0,)]
    assert_not_locked(tmp_db)


@pytest.mark.parametrize('extra', [{'Bogus': 1}, {'Path': FILE}, {'ID': 7}, {'Document_ID': 1},
                                   {'Status) VALUES (1, 1, 1, 1); DROP TABLE Documents; --': 'x'}], ids=repr)
def test_an_unknown_key_is_a_value_error_and_nothing_is_written(tmp_db, extra):
    receipt = add_receipt(tmp_db)
    with pytest.raises(ValueError, match='Unbekannte Spalten'):
        tmp_db.save_document_reading(receipt, FILE, {**reading(), **extra})
    assert raw(tmp_db, 'SELECT COUNT(*) FROM DocumentReadings') == [(0,)]
    assert raw(tmp_db, 'SELECT COUNT(*) FROM Documents') == [(1,)]
    assert_not_locked(tmp_db)


def test_a_missing_required_value_is_refused_by_the_database_and_leaves_no_lock(tmp_db):
    receipt = add_receipt(tmp_db)
    assert tmp_db.save_document_reading(receipt, FILE, reading())
    before = tmp_db.get_document_reading(receipt)
    without_status = {key: value for key, value in reading(GrossMinor=1).items() if key != 'Status'}

    with pytest.raises(sqlite3.IntegrityError, match='NOT NULL'):
        tmp_db.save_document_reading(receipt, FILE, without_status)

    assert tmp_db.get_document_reading(receipt) == before                           # die Lesung davor bleibt
    assert_not_locked(tmp_db)


def test_a_reading_never_goes_into_the_replay_log(tmp_db, logged):
    receipt = add_receipt(tmp_db)
    tmp_db.insert_article(name='Probe', unit_price=10, tax_rate=19)                 # Gegenprobe: dieser Weg schreibt ins Log
    assert len(logged) == 1

    tmp_db.save_document_reading(receipt, FILE, reading())
    tmp_db.save_document_reading(receipt, FILE, reading(Status='partial'))
    tmp_db.save_document_reading(receipt, 'anderer/Pfad.pdf', reading())
    tmp_db.get_document_reading(receipt)
    tmp_db.document_reading_summaries()
    tmp_db.delete_receipt(receipt)

    assert len(logged) == 1


# ── document_reading_summaries ───────────────────────────────────────────────

def test_the_summaries_are_one_query_and_never_read_text_or_raw(tmp_db, monkeypatch):
    first, second, _third = (add_receipt(tmp_db, f'B-{n}', f'Belege/2026/B-{n}.pdf') for n in (1, 2, 3))
    big = 'x' * 200_000
    tmp_db.save_document_reading(first, 'Belege/2026/B-1.pdf', reading(Text=big, Raw=big))
    tmp_db.save_document_reading(second, 'Belege/2026/B-2.pdf', reading(Status='partial', ReaderVersion=3, GrossMinor=None,
                                                                         Currency=None, SenderName=None))
    statements = []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    result = tmp_db.document_reading_summaries()
    monkeypatch.undo()

    assert result == {first: ('ok', 1190000, 'EUR', 'Lieferant Probe GmbH', 1),     # der dritte Beleg hat keine Lesung
                      second: ('partial', None, None, None, 3)}
    assert len(statements) == 1
    sql = statements[0]
    assert sql.lstrip().upper().startswith('SELECT') and 'DocumentReadings' in sql
    assert '*' not in sql and not re.search(r'\b(Text|Raw)\b', sql)                 # nie die schweren Spalten


# ── ungelesen ────────────────────────────────────────────────────────────────

def test_unread_means_a_file_and_no_reading_or_an_older_reader(tmp_db):
    with_file = add_receipt(tmp_db, 'B-1', 'Belege/2026/1.pdf')
    add_receipt(tmp_db, 'B-2', '')                                                  # Papierbeleg: nichts zu lesen
    add_receipt(tmp_db, 'B-3', None)
    add_receipt(tmp_db, 'B-4', '   ')                                               # nur Leerzeichen gilt als „keine Datei“
    old = add_receipt(tmp_db, 'B-5', 'Belege/2026/5.pdf')
    current = add_receipt(tmp_db, 'B-6', 'Belege/2026/6.pdf')
    newer = add_receipt(tmp_db, 'B-7', 'Belege/2026/7.pdf')
    paper_with_reading = add_receipt(tmp_db, 'B-8', '')
    for receipt_id, path, reader in ((old, 'Belege/2026/5.pdf', 1), (current, 'Belege/2026/6.pdf', 2),
                                     (newer, 'Belege/2026/7.pdf', 3), (paper_with_reading, '', 1)):
        assert tmp_db.save_document_reading(receipt_id, path, reading(ReaderVersion=reader))

    assert tmp_db.unread_receipt_ids(2) == [with_file, old] and tmp_db.count_unread_receipts(2) == 2
    assert tmp_db.unread_receipt_ids(1) == [with_file] and tmp_db.count_unread_receipts(1) == 1
    assert tmp_db.unread_receipt_ids(3) == [with_file, old, current] and tmp_db.count_unread_receipts(3) == 3
    assert tmp_db.unread_receipt_ids(4) == [with_file, old, current, newer] and tmp_db.count_unread_receipts(4) == 4


def test_a_receipt_stays_unread_until_its_reading_is_current(tmp_db):
    receipt = add_receipt(tmp_db)
    assert tmp_db.unread_receipt_ids(1) == [receipt]
    tmp_db.save_document_reading(receipt, FILE, reading(ReaderVersion=1))
    assert tmp_db.unread_receipt_ids(1) == [] and tmp_db.count_unread_receipts(1) == 0
    assert tmp_db.unread_receipt_ids(2) == [receipt]                                # ein besserer Leser liest neu
    tmp_db.save_document_reading(receipt, FILE, reading(ReaderVersion=2))
    assert tmp_db.unread_receipt_ids(2) == [] and tmp_db.count_unread_receipts(2) == 0


def test_the_limit_of_the_unread_list_is_clamped_and_the_count_has_none(tmp_db):
    with closing(tmp_db._get_connection()) as con:
        con.executemany('INSERT INTO Documents (Number, Date, Filename, Path, Info) VALUES (?, ?, ?, ?, ?)',
                        [(f'B-{n}', DAY, 'beleg.pdf', f'Belege/2026/{n}.pdf', '') for n in range(UNREAD_LIMIT_MAX + 5)])
        con.commit()
    ids = [row[0] for row in raw(tmp_db, 'SELECT ID FROM Documents ORDER BY ID')]

    assert tmp_db.unread_receipt_ids(1) == ids[:100]                                # Vorgabe: 100, nach ID
    assert tmp_db.unread_receipt_ids(1, limit=3) == ids[:3]
    assert tmp_db.unread_receipt_ids(1, limit=0) == ids[:1] and tmp_db.unread_receipt_ids(1, limit=-7) == ids[:1]
    assert tmp_db.unread_receipt_ids(1, limit=10 ** 6) == ids[:UNREAD_LIMIT_MAX] and UNREAD_LIMIT_MAX == 1000
    assert tmp_db.count_unread_receipts(1) == UNREAD_LIMIT_MAX + 5


# ── Löschen ──────────────────────────────────────────────────────────────────

def test_deleting_a_receipt_takes_its_reading_along(tmp_db):
    gone, stays = add_receipt(tmp_db, 'B-1', 'Belege/2026/1.pdf'), add_receipt(tmp_db, 'B-2', 'Belege/2026/2.pdf')
    tmp_db.save_document_reading(gone, 'Belege/2026/1.pdf', reading())
    tmp_db.save_document_reading(stays, 'Belege/2026/2.pdf', reading(InvoiceNumber='RE-2026-18'))

    assert tmp_db.delete_receipt(gone) == 'Belege/2026/1.pdf'                       # nur mit der Lesung: kein „Verweis“

    assert raw(tmp_db, 'SELECT Document_ID FROM DocumentReadings') == [(stays,)]
    assert tmp_db.get_document_reading(gone) is None
    assert tmp_db.get_document_reading(stays)['InvoiceNumber'] == 'RE-2026-18'
    assert raw(tmp_db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', gone) == [(0,)]
    assert_not_locked(tmp_db)


def test_a_refused_delete_leaves_the_reading_in_place(tmp_db):
    receipt = add_receipt(tmp_db)
    tmp_db.save_document_reading(receipt, FILE, reading())
    tmp_db.link_booking_to_document(tmp_db.insert_booking(DAY, 10), receipt)
    before = tmp_db.get_document_reading(receipt)

    with pytest.raises(DeleteRefused) as refused:
        tmp_db.delete_receipt(receipt)

    assert str(refused.value) == 'Der Beleg lässt sich nicht löschen, solange andere Daten auf ihn verweisen: 1 Buchung.'
    assert 'Lesung' not in str(refused.value)                                       # die Lesung zählt nie als Verweis
    assert tmp_db.get_document_reading(receipt) == before
    assert raw(tmp_db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', receipt) == [(1,)]
    assert_not_locked(tmp_db)


def test_a_failing_delete_of_the_receipt_takes_the_reading_back_with_it(tmp_db):
    """Beide Löschungen laufen in EINER Transaktion: Scheitert die des Belegs, ist auch die der Lesung zurückgenommen."""
    receipt = add_receipt(tmp_db)
    tmp_db.save_document_reading(receipt, FILE, reading())
    before = tmp_db.get_document_reading(receipt)
    run_sql(tmp_db, "CREATE TRIGGER nein BEFORE DELETE ON Documents BEGIN SELECT RAISE(ABORT, 'Probefehler'); END")

    with pytest.raises(sqlite3.IntegrityError, match='Probefehler'):
        tmp_db.delete_receipt(receipt)

    assert tmp_db.get_document_reading(receipt) == before
    assert raw(tmp_db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', receipt) == [(1,)]
    assert_not_locked(tmp_db)


def test_a_raw_delete_of_the_receipt_cascades_to_the_reading(tmp_db):
    gone, stays = add_receipt(tmp_db, 'B-1', 'Belege/2026/1.pdf'), add_receipt(tmp_db, 'B-2', 'Belege/2026/2.pdf')
    tmp_db.save_document_reading(gone, 'Belege/2026/1.pdf', reading())
    tmp_db.save_document_reading(stays, 'Belege/2026/2.pdf', reading())

    with closing(tmp_db._get_connection()) as con:                                  # Fremdschlüssel an, wie in der App
        con.execute('DELETE FROM Documents WHERE ID = ?', (gone,))
        con.commit()

    assert raw(tmp_db, 'SELECT Document_ID FROM DocumentReadings') == [(stays,)]
    assert raw(tmp_db, 'PRAGMA foreign_key_check') == []


def test_the_delete_script_of_the_console_takes_the_readings_of_the_deleted_receipts_along(tmp_db):
    inside = add_receipt(tmp_db, 'B-1', 'Belege/2025/1.pdf', '2025-06-01')
    outside = add_receipt(tmp_db, 'B-2', 'Belege/2026/2.pdf', '2026-06-01')
    tmp_db.save_document_reading(inside, 'Belege/2025/1.pdf', reading())
    tmp_db.save_document_reading(outside, 'Belege/2026/2.pdf', reading())
    script = _range_delete_script(tmp_db, '2025-01-01', '2025-12-31')              # nur der erste Beleg liegt im Zeitraum
    assert 'DELETE FROM DocumentReadings WHERE Document_ID IN (SELECT ID FROM Documents WHERE Date' in script
    assert script.index('DELETE FROM DocumentReadings') < script.rindex('DELETE FROM Documents')   # davor, wie die anderen

    answer = json.loads(handle_execute_sql(tmp_db, {'sql_commands': [script], 'write': ['1']}))

    assert not answer['errors'] and answer['success_count'] == answer['total'] and answer['rolled_back'] is False
    assert raw(tmp_db, 'SELECT ID FROM Documents') == [(outside,)]
    assert raw(tmp_db, 'SELECT Document_ID FROM DocumentReadings') == [(outside,)]
    assert raw(tmp_db, 'PRAGMA foreign_key_check') == []


# ── Die eigene Firma ─────────────────────────────────────────────────────────

def test_without_an_own_company_there_is_no_identity(tmp_db):
    tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='Kunde Probe',
                          company_name='Kunde Probe GmbH', tax_id='DE987654321')
    assert tmp_db.own_identity() == ([], [])


def test_own_identity_names_and_only_the_real_vat_ids(tmp_db):
    own = dict(contact_type='own', entity_type='company')
    tmp_db.insert_contact(display_name='Probe Eigen', company_name='Probe Eigen GmbH', tax_id='USt-IdNr. DE 123 456 789', **own)
    tmp_db.insert_contact(display_name='Zweitfirma Probe', company_name='Zweitfirma Probe', tax_id='12/345/67890', **own)
    tmp_db.insert_contact(display_name='Drittfirma Probe', company_name='Drittfirma Probe GmbH', tax_id='HRB 12345', **own)
    tmp_db.insert_contact(display_name='Viertfirma Probe', company_name='Viertfirma Probe GmbH', tax_id='DE12345678', **own)
    tmp_db.insert_contact(display_name='Fünftfirma Probe', company_name='Fünftfirma Probe GmbH', tax_id='de-987-654-321', **own)
    tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='Kunde Probe',
                          company_name='Kunde Probe GmbH', tax_id='DE112233445')   # kein eigener Kontakt: zählt nicht

    names, vat_ids = tmp_db.own_identity()

    assert names == ['Probe Eigen', 'Probe Eigen GmbH', 'Zweitfirma Probe', 'Drittfirma Probe', 'Drittfirma Probe GmbH',
                     'Viertfirma Probe', 'Viertfirma Probe GmbH', 'Fünftfirma Probe', 'Fünftfirma Probe GmbH']
    assert vat_ids == ['DE123456789', 'DE987654321']          # bereinigte Schreibweise; Steuernummer, HRB, zu kurz: nein


def test_own_identity_has_no_empty_names_and_no_duplicates(tmp_db):
    own = dict(contact_type='own', entity_type='company')
    tmp_db.insert_contact(display_name='  ', company_name='Nur Firma GmbH', tax_id='DE123456789', **own)
    tmp_db.insert_contact(display_name=' Gleich Probe ', company_name='Gleich Probe', tax_id='DE-123 456 789', **own)
    tmp_db.insert_contact(display_name='', company_name='', tax_id='', **own)
    tmp_db.insert_contact(display_name='Nur Anzeige', company_name='', tax_id=None, **own)

    names, vat_ids = tmp_db.own_identity()

    assert names == ['Nur Firma GmbH', 'Gleich Probe', 'Nur Anzeige']
    assert vat_ids == ['DE123456789']                                                # dieselbe Nummer in zwei Schreibweisen: einmal


def test_an_own_person_counts_with_the_display_name(tmp_db):
    tmp_db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe', first_name='Erika',
                          last_name='Probe')
    assert tmp_db.own_identity() == (['Erika Probe'], [])
