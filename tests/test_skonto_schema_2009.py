# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.9.0 (Schema 2009): Skonto an der Zahlungszuordnung.

Neu ist die Spalte ``InvoicePayments.SkontoAmount`` (ganze Zahl in Minor Units, NULL = kein Skonto) – als letzte Spalte der Tabelle, in derselben
Reihenfolge wie im ``CREATE TABLE`` einer frischen Datenbank. ``Amount`` bleibt das Geld, ``SkontoAmount`` ist der Rest der Rechnung, den die
Zuordnung ausbucht. Geprüft wird:

* Eine frische und eine migrierte Datenbank haben dieselben Spalten in derselben Reihenfolge, dieselben Tabellen und Indizes – auch aus 2.7.x, 2.6.x,
  2.5.x, 2.4.x, 2.3.x, 2.2.x, 2.1.x, 2.0.x und 1.x; der eindeutige Index auf (Rechnung, Bewegung) bleibt.
* Der Bestand bleibt, wie er war: jede Spalte jeder Zeile, alle anderen Tabellen; der neue Wert ist überall NULL – nichts wird umgedeutet.
* Vor der Umstellung entsteht eine Sicherung (Stand 2008), scheitert sie, bleibt die Datenbank unberührt; bricht die Umstellung ab, rollt sie ganz
  zurück (die Spalte ist danach wieder weg), und ein zweiter Start klappt. Eine Datenbank aus einer neueren Version (2010) wird nicht geöffnet.
* Die Lesestelle ``get_invoice_skonto`` nennt nur Zuordnungen mit einem Skonto über 0; ``get_invoice_payments`` behält sein Tupel.

Namen und Nummern sind erfundene Testwerte.
"""
import os
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

import version
from db import Database, MigrationBackupError, SchemaTooNewError, _CoreMixin
from db.schema import SKONTO_SCHEMA
from tests import installation
from tests.document_helpers import add_contacts, add_document
from tests.entry_form_world import (to_state_2000, to_state_2001, to_state_2002, to_state_2003, to_state_2004, to_state_2005,
                                    to_state_2006, to_state_2007, to_state_2008)

NEW_COLUMN = 'SkontoAmount'
OLD_COLUMNS = ['ID', 'InvoiceID', 'BookingID', 'Amount', 'PaymentDate', 'Notes', 'CreatedAt']      # bis 2.8.x
INDEX = 'ux_invoicepayments_invoice_booking'


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
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


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
    """Ein Bestand im Stand von 2.8.x: Rechnungen in mehreren Status, eine bezahlte mit Zuordnung und zwei Teilzahlungen einer anderen."""
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    own, customer = add_contacts(db)
    add_document(db, '26R001', 'invoice', own, customer, status='sent')
    paid = add_document(db, '26R003', 'invoice', own, customer, status='paid')
    partial = add_document(db, '26R004', 'invoice', own, customer, status='partial_payment', amount_due=19.0)
    first = db.insert_booking('2026-07-01', 119.0, booking_type='bank')
    db.link_invoice_to_transaction(paid, first, 119.0)
    second = db.insert_booking('2026-07-02', 60.0, booking_type='bank')
    db.link_invoice_to_transaction(partial, second, 60.0)
    third = db.insert_booking('2026-07-03', 40.0, booking_type='bank')
    db.link_invoice_to_transaction(partial, third, 40.0)
    to_state_2008(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


# ── Der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_stock_is_a_2_8_x_database_before_the_migration(stock):
    assert installation.user_version(stock) == 2008
    assert _names(stock, 'InvoicePayments') == OLD_COLUMNS
    with closing(sqlite3.connect(stock)) as con:
        assert con.execute('SELECT COUNT(*) FROM InvoicePayments').fetchone()[0] == 3


def test_the_step_back_to_2008_can_be_repeated(stock):
    to_state_2008(stock)
    to_state_2008(stock)
    assert installation.user_version(stock) == 2008 and _names(stock, 'InvoicePayments') == OLD_COLUMNS


def test_this_program_is_a_2_9_x_program():
    assert SKONTO_SCHEMA == 2009
    assert version.SCHEMA_VERSION >= 2009 and version.SCHEMA_STEP >= 9
    assert version.APP_VERSION.startswith('2.') and int(version.APP_VERSION.split('.')[1]) >= 9


# ── Frisch = migriert ────────────────────────────────────────────────────────

def test_a_fresh_database_has_the_skonto_column_last(tmp_db):
    names = _names(tmp_db.db_name, 'InvoicePayments')
    assert names == OLD_COLUMNS + [NEW_COLUMN] and names.index(NEW_COLUMN) == 7
    assert _info(tmp_db.db_name, 'InvoicePayments')[-1] == (NEW_COLUMN, 'INTEGER', 0, None, 0)        # NULL erlaubt, keine Vorgabe: NULL heißt „kein Skonto“


def test_a_fresh_and_a_migrated_database_have_the_same_columns_tables_and_indexes(stock, tmp_path):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    _reopen(stock)

    assert _names(stock, 'InvoicePayments')[-1] == NEW_COLUMN
    assert _info(stock, 'InvoicePayments') == _info(fresh, 'InvoicePayments')          # Name, Typ, NOT NULL, Vorgabe, PK
    assert _tables(stock) == _tables(fresh)
    with closing(sqlite3.connect(stock)) as a, closing(sqlite3.connect(fresh)) as b:
        for query in ("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name",
                      "SELECT sql FROM sqlite_master WHERE name LIKE 'ux_%' OR name LIKE 'idx_%' ORDER BY name"):
            assert [r[0] for r in a.execute(query)] == [r[0] for r in b.execute(query)]
        assert INDEX in [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type = 'index'")]       # der eindeutige Index auf (Rechnung, Bewegung) bleibt
    assert installation.user_version(stock) == installation.user_version(fresh) == version.SCHEMA_VERSION


@pytest.mark.parametrize('older', [to_state_2008, to_state_2007, to_state_2006, to_state_2005, to_state_2004, to_state_2003, to_state_2002, to_state_2001,
                                   to_state_2000, None],
                         ids=['aus-2.8.x', 'aus-2.7.x', 'aus-2.6.x', 'aus-2.5.x', 'aus-2.4.x', 'aus-2.3.x', 'aus-2.2.x', 'aus-2.1.x', 'aus-2.0.x', 'aus-1.x'])
def test_older_stocks_end_with_the_same_column_order_as_a_fresh_database(stock, tmp_path, older):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    if older is not None:
        older(stock)
    else:
        installation.stamp_user_version(stock, 4)
    _reopen(stock)

    for table in ('Accounts', 'Contacts', 'Bookings', 'CompanyDetails', 'Invoices', 'InvoiceItems', 'InvoicePayments', 'TaxRates'):
        assert _info(stock, table) == _info(fresh, table), table
    assert _names(stock, 'InvoicePayments')[-1] == NEW_COLUMN
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_the_older_steps_also_take_the_column_away(stock):
    """Jeder ältere Stand beginnt mit ``to_state_2008``: Er trägt die Spalte nie – sonst wäre er kein Stand von damals."""
    _reopen(stock)
    assert NEW_COLUMN in _names(stock, 'InvoicePayments')

    to_state_2007(stock)

    assert NEW_COLUMN not in _names(stock, 'InvoicePayments') and installation.user_version(stock) == 2007


def test_the_column_positions_of_the_payment_readers_stay_where_they_were(stock):
    """Was die Zuordnungen mit festen Namen und Tupeln liest, bleibt: Die neue Spalte hängt hinten an, ``get_invoice_payments`` behält sein Tupel."""
    before = _names(stock, 'InvoicePayments')
    db = _reopen(stock)

    after = _names(stock, 'InvoicePayments')
    assert after[:len(before)] == before and after[len(before):] == [NEW_COLUMN]
    with closing(sqlite3.connect(stock)) as con:
        invoice_id = con.execute('SELECT InvoiceID FROM InvoicePayments ORDER BY ID LIMIT 1').fetchone()[0]
    (row,) = db.get_invoice_payments(invoice_id)
    assert len(row) == 7 and row[3] == Decimal('119')                                  # (ID, InvoiceID, BookingID, Amount, PaymentDate, Notes, BookingRef)


# ── Der Bestand bleibt ───────────────────────────────────────────────────────

def test_existing_rows_stay_untouched_and_the_new_value_is_null(stock):
    before = _contents(stock)
    assert len(before['InvoicePayments']) == 3 and len(before['InvoicePayments'][0]) == len(OLD_COLUMNS)

    _reopen(stock)

    after = _contents(stock)
    assert [row[:len(OLD_COLUMNS)] for row in after['InvoicePayments']] == before['InvoicePayments']        # jede Spalte jeder Zeile
    assert all(row[-1] is None for row in after['InvoicePayments'])                                           # überall NULL – nichts wird umgedeutet
    for table, rows in before.items():
        if table not in ('InvoicePayments', 'TaxRates'):
            assert after[table] == rows, table                                                               # alle anderen Tabellen, Zeile für Zeile
    with closing(sqlite3.connect(stock)) as con:
        assert con.execute('SELECT COUNT(*) FROM InvoicePayments WHERE SkontoAmount IS NOT NULL').fetchone()[0] == 0


def test_an_old_stock_counts_the_open_amount_as_before(stock):
    """Ohne Skonto ist jede Rechnung des Bestands so offen wie vorher (``Amount`` allein, ``SkontoAmount`` NULL zählt 0): Die Prüfliste nennt nach der Umstellung weder
    einen offenen Betrag, der nicht zu den Zuordnungen passt, noch ein Skonto."""
    db = _reopen(stock)

    assert [f['category'] for f in db.find_payment_inconsistencies() if f['category'] in ('amount_due_mismatch', 'skonto_mismatch')] == []


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
    assert installation.user_version(str(backup)) == 2008
    assert _names(str(backup), 'InvoicePayments') == OLD_COLUMNS
    assert installation.dump(str(backup)) == before                                  # Zeile für Zeile der alte Stand
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_failing_backup_stops_the_opening_and_leaves_the_stock_at_2008(stock):
    before = installation.dump(stock)
    (Path(stock).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError):
        _reopen(stock)

    assert installation.user_version(stock) == 2008 and installation.dump(stock) == before
    assert _names(stock, 'InvoicePayments') == OLD_COLUMNS
    (Path(stock).parent / 'backup').unlink()                                         # Ursache behoben: jetzt klappt es
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock, 'InvoicePayments')[-1] == NEW_COLUMN


def test_an_aborted_migration_rolls_back_the_column(stock, monkeypatch):
    before = installation.dump(stock)
    original = Database._ensure_tax_rates_schema

    def boom(self, cursor):
        original(self, cursor)
        assert [row[1] for row in cursor.execute('PRAGMA table_info(InvoicePayments)')][-1] == NEW_COLUMN        # die Spalte ist schon angefügt …
        raise RuntimeError('Abbruch mitten in der Umstellung')                       # … und dann scheitert die Umstellung

    monkeypatch.setattr(Database, '_ensure_tax_rates_schema', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(stock)
    finally:
        monkeypatch.undo()

    assert installation.user_version(stock) == 2008
    assert _names(stock, 'InvoicePayments') == OLD_COLUMNS
    assert installation.dump(stock) == before                                        # Spalten, Tabellen, Index, Zeilen wie vorher

    _reopen(stock)                                                                   # und beim nächsten Start klappt es
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock, 'InvoicePayments')[-1] == NEW_COLUMN


def test_a_column_that_is_already_there_does_not_stop_the_migration(stock):
    with closing(sqlite3.connect(stock)) as con:
        con.execute(f'ALTER TABLE InvoicePayments ADD COLUMN {NEW_COLUMN} INTEGER')  # halb durchgeführt
        con.commit()

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock, 'InvoicePayments') == OLD_COLUMNS + [NEW_COLUMN]            # nicht doppelt


def test_a_database_of_a_newer_version_is_not_opened(stock):
    installation.stamp_user_version(stock, max(2010, version.SCHEMA_VERSION + 1))
    before = installation.dump(stock)

    with pytest.raises(SchemaTooNewError):
        Database(db_name=stock)

    assert installation.dump(stock) == before and _names(stock, 'InvoicePayments') == OLD_COLUMNS       # nichts angefasst, nichts zurückgestempelt


# ── Die Lesestelle ───────────────────────────────────────────────────────────

def _payment_rows(db):
    """Drei Zuordnungen zu drei Rechnungen: mit Skonto, ohne (NULL) und mit 0 – dazu ein Wert, der keine ganze Zahl ist (per SQL-Konsole)."""
    own, customer = add_contacts(db)
    ids = [add_document(db, f'26S00{n}', 'invoice', own, customer, status='sent') for n in range(1, 5)]
    banks = [db.insert_booking('2026-07-01', 100.0, booking_type='bank') for _ in ids]
    payments = []
    for invoice_id, booking_id in zip(ids, banks):
        db.link_invoice_to_transaction(invoice_id, booking_id, 100.0)
        with closing(sqlite3.connect(db.db_name)) as con:
            payments.append(con.execute('SELECT ID FROM InvoicePayments WHERE InvoiceID = ?', (invoice_id,)).fetchone()[0])
    return ids, payments


def test_get_invoice_skonto_names_only_assignments_with_a_discount_above_zero(tmp_db):
    ids, payments = _payment_rows(tmp_db)
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute('UPDATE InvoicePayments SET SkontoAmount = 23800 WHERE ID = ?', (payments[0],))
        con.execute('UPDATE InvoicePayments SET SkontoAmount = 0 WHERE ID = ?', (payments[2],))
        con.execute("UPDATE InvoicePayments SET SkontoAmount = 'abc' WHERE ID = ?", (payments[3],))        # Text: nicht lesbar, zählt nicht
        con.commit()

    assert tmp_db.get_invoice_skonto(ids[0]) == {payments[0]: Decimal('2.38')}
    assert tmp_db.get_invoice_skonto(ids[1]) == {}                                   # NULL
    assert tmp_db.get_invoice_skonto(ids[2]) == {}                                   # 0
    assert tmp_db.get_invoice_skonto(ids[3]) == {}                                   # Text
    assert tmp_db.get_invoice_skonto(999999) == {}                                   # unbekannte Rechnung
    (value,) = tmp_db.get_invoice_skonto(ids[0]).values()
    assert isinstance(value, Decimal)


def test_get_invoice_skonto_does_not_widen_the_payment_tuple(tmp_db):
    ids, payments = _payment_rows(tmp_db)
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute('UPDATE InvoicePayments SET SkontoAmount = 23800 WHERE ID = ?', (payments[0],))
        con.commit()

    (row,) = tmp_db.get_invoice_payments(ids[0])

    assert len(row) == 7 and row[0] == payments[0]                                   # (ID, InvoiceID, BookingID, Amount, PaymentDate, Notes, BookingRef)
