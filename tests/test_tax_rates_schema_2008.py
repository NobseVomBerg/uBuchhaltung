# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.8.0 (Schema 2008): die Steuersätze der Stammdaten und die Momentaufnahme an der Position.

Neu sind die Tabelle ``TaxRates`` (Bezeichnung, Satz in Hundertstel Prozent, Art ``standard``/``exempt``, Befreiungsgrund, Erlöskonto, aktiv,
Reihenfolge) – beim Anlegen genau einmal mit den Sätzen aus ``tax_groups.MIXED_TAX_RATES`` vorbelegt –, drei Spalten am Ende von
``InvoiceItems`` (``TaxRateBp`` Index 11, ``TaxExemptReason`` 12, ``RevenueCoaId`` 13; ``TaxCategory`` gab es schon, ``'E'`` heißt dort künftig
„steuerfrei“) und der Index ``idx_invoiceitems_invoice``. Geprüft wird:

* Eine frische und eine migrierte Datenbank haben dieselben Spalten in derselben Reihenfolge, dieselben Tabellen und Indizes – auch aus 2.6.x,
  2.5.x, 2.4.x, 2.3.x, 2.2.x, 2.1.x, 2.0.x und 1.x; alles, was bis 2.7.x an einem Index der Position stand, bleibt dort.
* Die Vorbelegung entsteht genau einmal (nicht bei jedem Start, nicht nach dem Löschen aller Einträge), die Sätze nennt allein ``MIXED_TAX_RATES``.
* Der Bestand bleibt, wie er war: jede Spalte jeder Zeile, alle anderen Tabellen; die neuen Werte sind überall NULL und die Art ``'S'`` – nichts
  wird umgedeutet.
* Vor der Umstellung entsteht eine Sicherung (Stand 2007), scheitert sie, bleibt die Datenbank unberührt; bricht die Umstellung ab, rollt sie ganz
  zurück (Spalten, Tabelle, Vorbelegung), und ein zweiter Start klappt. Eine Datenbank aus einer neueren Version (2009) wird nicht geöffnet.
* ``insert_invoice_item`` schreibt die Momentaufnahme, wie sie kommt (fehlt sie: NULL bzw. ``'S'``); Stornorechnung, Ersatzrechnung und Angebot →
  Rechnung nehmen sie unverändert mit (die Stornorechnung negiert nur Menge und Positionssumme).
* ``coa_is_referenced`` kennt Steuersätze und Rechnungspositionen: Ein Konto, das dort als Erlöskonto steht, lässt sich nicht löschen.

Die Tests schreiben die neuen Werte direkt in die Tabellen (die Wege der App, die sie setzen, prüfen eigene Tests). Namen und Nummern sind
erfundene Testwerte.
"""
import datetime
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import version
from db import Database, MigrationBackupError, SchemaTooNewError, _CoreMixin, coa_id
import db.invoice_payments
from db.invoices import ITEM_SNAPSHOT_COLUMNS, ITEM_SNAPSHOT_KEYS
from db.schema import TAX_RATES_SCHEMA
from server import handlers
from tax_groups import MIXED_TAX_RATES, pct_text
from tests import installation
from tests.document_helpers import add_contacts, add_document
from tests.entry_form_world import (to_state_2000, to_state_2001, to_state_2002, to_state_2003, to_state_2004, to_state_2005,
                                    to_state_2006, to_state_2007)

NEW_COLUMNS = ['TaxRateBp', 'TaxExemptReason', 'RevenueCoaId']
OLD_ITEM_COLUMNS = 11                       # Index 0 bis 10 (…, TaxCategory, TaxRate) bis 2.7.x
TAX_RATE_COLUMNS = ['ID', 'Label', 'RateBp', 'Kind', 'ExemptReason', 'RevenueCoaId', 'VatCoaId', 'InputTaxCoaId', 'Active', 'SortOrder', 'CreatedAt']
INDEX = 'idx_invoiceitems_invoice'
EXEMPT_REASON = 'Steuerfreie Leistung (erfundener Testgrund)'
USER = 'anna'


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


def _rates(path):
    """Die Einträge der Stammdaten ohne ID und Zeitstempel: (Bezeichnung, Satz, Art, Grund, Erlöskonto, aktiv, Reihenfolge)."""
    with closing(sqlite3.connect(path)) as con:
        return con.execute('SELECT Label, RateBp, Kind, ExemptReason, RevenueCoaId, Active, SortOrder FROM TaxRates ORDER BY ID').fetchall()


def _foreign_keys(path, table):
    """Die Fremdschlüssel einer Tabelle als (Zieltabelle, Spalte, Zielspalte)."""
    with closing(sqlite3.connect(path)) as con:
        return sorted((row[2], row[3], row[4]) for row in con.execute(f'PRAGMA foreign_key_list({table})'))


@pytest.fixture
def stock(tmp_path):
    """Ein Bestand im Stand von 2.7.x: Rechnungen in mehreren Status (eine mit 7 % im Kopf, aber 19 % an der Position – so einen Bestand deutet nichts
    um –, eine ohne Steuerausweis, eine mit Steuersatz je Position), ein Angebot, Positionen, eine Zahlung."""
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    own, customer = add_contacts(db)
    add_document(db, '26R001', 'invoice', own, customer, status='sent')
    add_document(db, '26R002', 'invoice', own, customer, status='draft', tax_rate=0.07, sum_net=100.0, tax_amount=7.0,
                 sum_gross=107.0, amount_due=107.0)
    paid = add_document(db, '26R003', 'invoice', own, customer, status='paid')
    add_document(db, '26R004', 'invoice', own, customer, status='draft', tax_rate=-1, tax_amount=0.0, sum_gross=100.0,
                 amount_due=100.0)
    mixed = add_document(db, '26R005', 'invoice', own, customer, status='draft', tax_rate=0.19, sum_net=150.0, tax_amount=22.5,
                         sum_gross=172.5, amount_due=172.5, mixed_tax_rates=1)
    db.insert_invoice_item({'invoice_id': mixed, 'position': 2, 'description': 'Fachbuch', 'quantity': 1, 'unit': 'Stk.',
                            'price_per_unit': 50.0, 'total_net': 50.0, 'tax_category': 'S', 'tax_rate': 7})
    add_document(db, '26A001', 'quote', own, customer, status='accepted')
    booking = db.insert_booking('2026-07-01', 119.0, booking_type='bank')
    db.link_invoice_to_transaction(paid, booking, 119.0)
    to_state_2007(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


# ── Der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_stock_is_a_2_7_x_database_before_the_migration(stock):
    assert installation.user_version(stock) == 2007
    assert _names(stock, 'InvoiceItems')[-1] == 'TaxRate' and len(_names(stock, 'InvoiceItems')) == OLD_ITEM_COLUMNS
    assert 'TaxRates' not in _tables(stock)
    with closing(sqlite3.connect(stock)) as con:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (INDEX,)).fetchone() is None
        assert con.execute('SELECT COUNT(*) FROM InvoiceItems').fetchone()[0] == 7


def test_the_step_back_to_2007_can_be_repeated(stock):
    to_state_2007(stock)
    to_state_2007(stock)
    assert installation.user_version(stock) == 2007 and 'TaxRates' not in _tables(stock)
    assert _names(stock, 'InvoiceItems')[-1] == 'TaxRate'


def test_this_program_is_a_2_8_x_program():
    assert TAX_RATES_SCHEMA == 2008
    assert version.SCHEMA_VERSION >= 2008 and version.SCHEMA_STEP >= 8
    assert version.APP_VERSION.startswith('2.') and int(version.APP_VERSION.split('.')[1]) >= 8


# ── Frisch = migriert ────────────────────────────────────────────────────────

def test_a_fresh_database_has_the_snapshot_columns_last_at_index_11_to_13(tmp_db):
    names = _names(tmp_db.db_name, 'InvoiceItems')
    assert names[OLD_ITEM_COLUMNS:] == NEW_COLUMNS and len(names) == OLD_ITEM_COLUMNS + 3
    assert [names.index(column) for column in NEW_COLUMNS] == [11, 12, 13]
    assert _info(tmp_db.db_name, 'InvoiceItems')[-3:] == [('TaxRateBp', 'INTEGER', 0, None, 0), ('TaxExemptReason', 'TEXT', 0, None, 0),
                                                          ('RevenueCoaId', 'INTEGER', 0, None, 0)]        # NULL erlaubt, keine Vorgabe
    assert names[9] == 'TaxCategory' and names[10] == 'TaxRate'
    assert _info(tmp_db.db_name, 'InvoiceItems')[9] == ('TaxCategory', 'TEXT', 0, "'S'", 0)              # die Art bleibt 'S' ohne Angabe


def test_the_tax_rates_table_has_the_planned_columns(tmp_db):
    assert _info(tmp_db.db_name, 'TaxRates') == [
        ('ID', 'INTEGER', 0, None, 1), ('Label', 'TEXT', 1, None, 0), ('RateBp', 'INTEGER', 1, None, 0),
        ('Kind', 'TEXT', 1, "'standard'", 0), ('ExemptReason', 'TEXT', 0, None, 0), ('RevenueCoaId', 'INTEGER', 0, None, 0),
        ('VatCoaId', 'INTEGER', 0, None, 0), ('InputTaxCoaId', 'INTEGER', 0, None, 0),
        ('Active', 'INTEGER', 1, '1', 0), ('SortOrder', 'INTEGER', 1, '0', 0), ('CreatedAt', 'DATETIME', 0, 'CURRENT_TIMESTAMP', 0)]
    assert _names(tmp_db.db_name, 'TaxRates') == TAX_RATE_COLUMNS


def test_a_fresh_and_a_migrated_database_have_the_same_columns_tables_and_indexes(stock, tmp_path):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    _reopen(stock)

    assert _names(stock, 'InvoiceItems')[OLD_ITEM_COLUMNS:] == NEW_COLUMNS
    for table in ('InvoiceItems', 'TaxRates'):
        assert _info(stock, table) == _info(fresh, table), table                       # Name, Typ, NOT NULL, Vorgabe, PK
    assert _tables(stock) == _tables(fresh)
    with closing(sqlite3.connect(stock)) as a, closing(sqlite3.connect(fresh)) as b:
        for query in ("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name",
                      "SELECT sql FROM sqlite_master WHERE name LIKE 'ux_%' OR name LIKE 'idx_%' ORDER BY name"):
            assert [r[0] for r in a.execute(query)] == [r[0] for r in b.execute(query)]
        assert INDEX in [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type = 'index'")]
    assert installation.user_version(stock) == installation.user_version(fresh) == version.SCHEMA_VERSION
    assert _rates(stock) == _rates(fresh)                                              # auch die Vorbelegung


@pytest.mark.parametrize('older', [to_state_2007, to_state_2006, to_state_2005, to_state_2004, to_state_2003, to_state_2002, to_state_2001,
                                   to_state_2000, None],
                         ids=['aus-2.7.x', 'aus-2.6.x', 'aus-2.5.x', 'aus-2.4.x', 'aus-2.3.x', 'aus-2.2.x', 'aus-2.1.x', 'aus-2.0.x', 'aus-1.x'])
def test_older_stocks_end_with_the_same_column_order_as_a_fresh_database(stock, tmp_path, older):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    if older is not None:
        older(stock)
    else:
        installation.stamp_user_version(stock, 4)
    _reopen(stock)

    for table in ('Accounts', 'Contacts', 'Bookings', 'CompanyDetails', 'Invoices', 'InvoiceItems', 'TaxRates'):
        assert _info(stock, table) == _info(fresh, table), table
    assert _names(stock, 'InvoiceItems')[OLD_ITEM_COLUMNS:] == NEW_COLUMNS
    assert _rates(stock) == _rates(fresh)
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_the_column_positions_of_the_item_readers_stay_where_they_were(stock):
    """Was die Positionen positionsbasiert liest (``SELECT *``: Index 2, 4 bis 10), bleibt, wo es stand – die neuen Spalten hängen hinten an."""
    before = _names(stock, 'InvoiceItems')
    _reopen(stock)

    after = _names(stock, 'InvoiceItems')
    assert after[:OLD_ITEM_COLUMNS] == before and after[OLD_ITEM_COLUMNS:] == NEW_COLUMNS
    assert [after[index] for index in (2, 4, 5, 6, 7, 8, 9, 10)] == [
        'Position', 'Description', 'Quantity', 'Unit', 'PricePerUnit', 'TotalNet', 'TaxCategory', 'TaxRate']


def test_the_items_of_an_invoice_are_read_through_an_index(tmp_db):
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        assert con.execute('SELECT sql FROM sqlite_master WHERE name = ?', (INDEX,)).fetchone()[0] == (
            f'CREATE INDEX {INDEX} ON InvoiceItems(InvoiceId)')
        plan = ' '.join(row[3] for row in con.execute('EXPLAIN QUERY PLAN SELECT * FROM InvoiceItems WHERE InvoiceId = 1 ORDER BY Position'))
    assert INDEX in plan


def test_the_accounts_are_foreign_keys_of_the_tax_rates_and_the_items_carry_none_fresh_like_migrated(stock, tmp_path):
    """``TaxRates`` entsteht in beiden Fällen mit demselben CREATE und trägt die Fremdschlüssel auf den Kontenrahmen (Erlös-, Umsatzsteuer- und
    Vorsteuerkonto). ``InvoiceItems.RevenueCoaId`` trägt keinen – weder frisch noch migriert: So sind beide Datenbanken gleich, und die Momentaufnahme
    einer Position hängt nicht an einem Schlüssel. ``coa_is_referenced`` prüft alle – das Löschen eines Kontos hängt nie am Fremdschlüssel."""
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    _reopen(stock)

    assert [fk for fk in _foreign_keys(fresh, 'TaxRates')] == [('ChartOfAccounts', 'InputTaxCoaId', 'ID'), ('ChartOfAccounts', 'RevenueCoaId', 'ID'),
                                                             ('ChartOfAccounts', 'VatCoaId', 'ID')]
    assert _foreign_keys(stock, 'TaxRates') == _foreign_keys(fresh, 'TaxRates')
    assert not [fk for fk in _foreign_keys(fresh, 'InvoiceItems') if fk[1] == 'RevenueCoaId']
    assert _foreign_keys(stock, 'InvoiceItems') == _foreign_keys(fresh, 'InvoiceItems')


def test_a_start_that_broke_off_after_creating_the_table_is_seeded_at_the_next_start(stock):
    """Bricht der erste Versuch der Umstellung ab, nachdem die Tabelle angelegt, aber bevor etwas abgeschlossen war, kann sie leer liegen bleiben, während
    die Datenbank noch im alten Stand ist: Der nächste Start belegt sie dann vor. (Eine Datenbank im Stand 2008 mit geleerter Tabelle bleibt leer – der
    Test darüber hält es fest.)"""
    with closing(sqlite3.connect(stock)) as con:
        con.execute('CREATE TABLE TaxRates (ID INTEGER PRIMARY KEY AUTOINCREMENT, Label TEXT NOT NULL, RateBp INTEGER NOT NULL, '
                    "Kind TEXT NOT NULL DEFAULT 'standard', ExemptReason TEXT, RevenueCoaId INTEGER, VatCoaId INTEGER, InputTaxCoaId INTEGER, "
                    'Active INTEGER NOT NULL DEFAULT 1, SortOrder INTEGER NOT NULL DEFAULT 0, CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP)')
        con.commit()
        assert con.execute('PRAGMA user_version').fetchone()[0] == 2007

    _reopen(stock)

    assert [(label, bp) for label, bp, *_ in _rates(stock)] == [(f'{pct_text(rate)} %', int(rate * 100)) for rate in MIXED_TAX_RATES]


# ── Die Vorbelegung ──────────────────────────────────────────────────────────

def test_the_seed_is_exactly_the_rates_of_the_mixed_list_derived_not_copied(tmp_db):
    expected = [(f'{pct_text(rate)} %', int(rate * 100), 'standard', None, None, 1, number * 10)
                for number, rate in enumerate(MIXED_TAX_RATES, 1)]

    assert _rates(tmp_db.db_name) == expected
    assert len(expected) == len(MIXED_TAX_RATES) >= 1
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        assert [row[0] for row in con.execute('SELECT ID FROM TaxRates ORDER BY ID')] == list(range(1, len(expected) + 1))
        assert con.execute('SELECT COUNT(*) FROM TaxRates WHERE CreatedAt IS NULL').fetchone()[0] == 0


def test_the_seed_is_the_same_in_a_migrated_database(stock):
    _reopen(stock)

    assert _rates(stock) == [(f'{pct_text(rate)} %', int(rate * 100), 'standard', None, None, 1, number * 10)
                             for number, rate in enumerate(MIXED_TAX_RATES, 1)]


def test_after_deleting_every_entry_none_comes_back_at_the_next_start(tmp_db):
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute('DELETE FROM TaxRates')
        con.commit()

    _reopen(tmp_db.db_name)
    assert _rates(tmp_db.db_name) == []
    _reopen(tmp_db.db_name)
    assert _rates(tmp_db.db_name) == []                                                # auch beim zweiten Start nicht


def test_after_the_migration_a_deleted_entry_stays_deleted_and_a_changed_one_stays_changed(stock):
    _reopen(stock)
    with closing(sqlite3.connect(stock)) as con:
        con.execute('DELETE FROM TaxRates WHERE ID = 1')
        con.execute("UPDATE TaxRates SET Label = 'Ermäßigt', Active = 0, SortOrder = 5 WHERE ID = 2")
        con.execute("INSERT INTO TaxRates (Label, RateBp, Kind, ExemptReason, SortOrder) VALUES ('Steuerfrei', 0, 'exempt', ?, 30)",
                    (EXEMPT_REASON,))
        con.commit()
    before = _rates(stock)

    _reopen(stock)
    _reopen(stock)

    assert _rates(stock) == before and len(before) == 2


# ── Der Bestand bleibt ───────────────────────────────────────────────────────

def test_existing_rows_stay_untouched_and_the_new_values_are_null(stock):
    before = _contents(stock)
    assert 'TaxRates' not in before and len(before['InvoiceItems']) == 7 and len(before['InvoicePayments']) == 1

    _reopen(stock)

    after = _contents(stock)
    assert [row[:OLD_ITEM_COLUMNS] for row in after['InvoiceItems']] == before['InvoiceItems']     # jede Spalte jeder Zeile
    assert all(row[OLD_ITEM_COLUMNS:] == (None, None, None) for row in after['InvoiceItems'])      # überall NULL – nichts wird umgedeutet
    assert {row[9] for row in after['InvoiceItems']} == {'S'}                                      # keine Art wird zu 'E'
    assert all(row[-1] is None for row in after['InvoicePayments'])                                # seit 2.9.0 eine Spalte am Ende, leer (test_skonto_schema_2009.py)
    after['InvoicePayments'] = [row[:-1] for row in after['InvoicePayments']]
    assert all(row[-3:] == (None, None, None) for row in after['Invoices'])                        # seit 2.10.0 drei Spalten am Ende, leer (test_schema_2010.py)
    after['Invoices'] = [row[:-3] for row in after['Invoices']]
    for table, rows in before.items():
        if table != 'InvoiceItems':
            assert after[table] == rows, table                                                     # alle anderen Tabellen, Zeile für Zeile
    with closing(sqlite3.connect(stock)) as con:
        assert con.execute('SELECT COUNT(*) FROM InvoiceItems WHERE TaxRateBp IS NOT NULL OR TaxExemptReason IS NOT NULL '
                           'OR RevenueCoaId IS NOT NULL').fetchone()[0] == 0


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
    assert installation.user_version(str(backup)) == 2007
    assert _names(str(backup), 'InvoiceItems')[-1] == 'TaxRate' and 'TaxRates' not in _tables(str(backup))
    assert installation.dump(str(backup)) == before                                  # Zeile für Zeile der alte Stand
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_failing_backup_stops_the_opening_and_leaves_the_stock_at_2007(stock):
    before = installation.dump(stock)
    (Path(stock).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError):
        _reopen(stock)

    assert installation.user_version(stock) == 2007 and installation.dump(stock) == before
    assert 'TaxRates' not in _tables(stock) and _names(stock, 'InvoiceItems')[-1] == 'TaxRate'
    (Path(stock).parent / 'backup').unlink()                                         # Ursache behoben: jetzt klappt es
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock, 'InvoiceItems')[-3:] == NEW_COLUMNS


def test_an_aborted_migration_rolls_back_the_columns_the_table_the_seed_and_the_index(stock, monkeypatch):
    before = installation.dump(stock)
    original = Database._ensure_tax_rates_schema

    def boom(self, cursor):
        original(self, cursor)                                                       # Tabelle, Vorbelegung und Index sind angelegt …
        assert [row[1] for row in cursor.execute('PRAGMA table_info(InvoiceItems)')][-3:] == NEW_COLUMNS     # … die Spalten schon angefügt …
        assert cursor.execute('SELECT COUNT(*) FROM TaxRates').fetchone()[0] == len(MIXED_TAX_RATES)
        raise RuntimeError('Abbruch mitten in der Umstellung')                       # … und dann scheitert die Umstellung

    monkeypatch.setattr(Database, '_ensure_tax_rates_schema', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(stock)
    finally:
        monkeypatch.undo()

    assert installation.user_version(stock) == 2007
    assert _names(stock, 'InvoiceItems')[-1] == 'TaxRate' and 'TaxRates' not in _tables(stock)
    assert installation.dump(stock) == before                                        # Spalten, Tabellen, Index, Zeilen wie vorher

    _reopen(stock)                                                                   # und beim nächsten Start klappt es – mit EINER Vorbelegung
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock, 'InvoiceItems')[-3:] == NEW_COLUMNS
    assert len(_rates(stock)) == len(MIXED_TAX_RATES)


def test_columns_that_are_already_there_do_not_stop_the_migration(stock):
    with closing(sqlite3.connect(stock)) as con:
        con.execute('ALTER TABLE InvoiceItems ADD COLUMN TaxRateBp INTEGER')         # halb durchgeführt
        con.execute('ALTER TABLE InvoiceItems ADD COLUMN TaxExemptReason TEXT')
        con.commit()

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock, 'InvoiceItems')[OLD_ITEM_COLUMNS:] == NEW_COLUMNS           # nicht doppelt, in der Reihenfolge der frischen Datenbank


def test_a_database_of_a_newer_version_is_not_opened(stock):
    installation.stamp_user_version(stock, max(2009, version.SCHEMA_VERSION + 1))
    before = installation.dump(stock)

    with pytest.raises(SchemaTooNewError):
        Database(db_name=stock)

    assert installation.dump(stock) == before and 'TaxRates' not in _tables(stock)   # nichts angefasst, nichts zurückgestempelt


# ── Die Position schreibt die Momentaufnahme ─────────────────────────────────

def _make_document(db, number='26R200'):
    own, customer = add_contacts(db)
    return add_document(db, number, 'invoice', own, customer, status='draft')


def _stored(db, invoice_id):
    """Die Momentaufnahme jeder Position, roh: (Art, Satz in Hundertstel Prozent, Befreiungsgrund, Erlöskonto) – die neuen Spalten samt ihrem Typ."""
    with closing(sqlite3.connect(db.db_name)) as con:
        return con.execute('SELECT TaxCategory, TaxRateBp, TaxExemptReason, RevenueCoaId, typeof(TaxRateBp), typeof(TaxExemptReason), '
                           'typeof(RevenueCoaId) FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', (invoice_id,)).fetchall()


def test_the_snapshot_columns_are_named_in_one_place_and_match_the_table(tmp_db):
    assert ITEM_SNAPSHOT_COLUMNS == ('TaxCategory', 'TaxRateBp', 'TaxExemptReason', 'RevenueCoaId')
    assert ITEM_SNAPSHOT_KEYS == {'TaxCategory': 'tax_category', 'TaxRateBp': 'tax_rate_bp', 'TaxExemptReason': 'tax_exempt_reason',
                                  'RevenueCoaId': 'revenue_coa_id'}
    names = _names(tmp_db.db_name, 'InvoiceItems')
    assert [names.index(column) for column in ITEM_SNAPSHOT_COLUMNS] == [9, 11, 12, 13]


def test_insert_invoice_item_writes_null_and_s_without_the_keys(tmp_db):
    invoice_id = _make_document(tmp_db)                                              # add_document legt die Position ohne die Schlüssel an
    tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 2, 'description': 'Zweite Position', 'quantity': 1,
                                'price_per_unit': 10.0, 'total_net': 10.0, 'tax_rate': 19})

    assert _stored(tmp_db, invoice_id) == [('S', None, None, None, 'null', 'null', 'null')] * 2


def test_insert_invoice_item_writes_the_snapshot_as_it_comes(tmp_db):
    invoice_id = _make_document(tmp_db)
    values = {'tax_category': 'E', 'tax_rate_bp': 0, 'tax_exempt_reason': EXEMPT_REASON, 'revenue_coa_id': coa_id(4, 4400)}
    assert set(values) == set(ITEM_SNAPSHOT_KEYS.values())                           # jeder Schlüssel der Konstante hat einen Wert
    tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 2, 'description': 'Steuerfreie Leistung', 'quantity': 1,
                                'price_per_unit': 10.0, 'total_net': 10.0, 'tax_rate': 0, **values})
    tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 3, 'description': 'Reduzierte Leistung', 'quantity': 1,
                                'price_per_unit': 10.0, 'total_net': 10.0, 'tax_rate': 5.5, 'tax_rate_bp': 550})

    assert _stored(tmp_db, invoice_id)[1:] == [('E', 0, EXEMPT_REASON, coa_id(4, 4400), 'integer', 'text', 'integer'),
                                               ('S', 550, None, None, 'integer', 'null', 'null')]
    with closing(sqlite3.connect(tmp_db.db_name)) as con:                            # gelesen wie jede Position: SELECT *, Index 11 bis 13
        row = con.execute('SELECT * FROM InvoiceItems WHERE InvoiceId = ? AND Position = 2', (invoice_id,)).fetchone()
    assert row[9] == 'E' and row[10] == 0 and row[11:] == (0, EXEMPT_REASON, coa_id(4, 4400))
    assert tmp_db.get_invoice_items(invoice_id)[1][11:] == (0, EXEMPT_REASON, coa_id(4, 4400))


def test_insert_invoice_item_with_a_transaction_writes_the_snapshot_with_it_and_into_the_replay_log(tmp_db):
    invoice_id = _make_document(tmp_db)

    with tmp_db.transaction() as tx:
        tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 2, 'description': 'Steuerfreie Leistung', 'quantity': 1,
                                    'price_per_unit': 10.0, 'total_net': 10.0, 'tax_category': 'E', 'tax_rate': 0, 'tax_rate_bp': 0,
                                    'tax_exempt_reason': EXEMPT_REASON, 'revenue_coa_id': coa_id(4, 4400)}, tx=tx)
        entries = [params for sql, params, _description in tx.log if 'INSERT INTO InvoiceItems' in sql]

    assert _stored(tmp_db, invoice_id)[1][:4] == ('E', 0, EXEMPT_REASON, coa_id(4, 4400))
    assert entries and entries[0][-3:] == (0, EXEMPT_REASON, coa_id(4, 4400))        # die letzten drei Parameter: im Replay-Log


def test_the_snapshot_constants_live_in_the_lowest_invoice_module():
    """``invoice_lock`` und ``invoice_versions`` lesen die Positionen ebenfalls; ``db.invoices`` importiert ``invoice_lock`` – die Konstanten stehen
    deshalb in ``db.invoice_payments``, ``db.invoices`` reicht dieselben Objekte weiter."""
    assert db.invoice_payments.ITEM_SNAPSHOT_KEYS is ITEM_SNAPSHOT_KEYS
    assert db.invoice_payments.ITEM_SNAPSHOT_COLUMNS is ITEM_SNAPSHOT_COLUMNS


# ── Kopierwege: Storno, Ersatzrechnung, Angebot → Rechnung ───────────────────

ITEM_COLUMNS = ('Position', 'Quantity', 'PricePerUnit', 'TotalNet', 'TaxRate') + ITEM_SNAPSHOT_COLUMNS
FIRST_SNAPSHOT = {'tax_category': 'S', 'tax_rate_bp': 1900, 'tax_exempt_reason': None, 'revenue_coa_id': coa_id(4, 4400)}
SECOND_SNAPSHOT = {'tax_category': 'E', 'tax_rate_bp': 0, 'tax_exempt_reason': EXEMPT_REASON, 'revenue_coa_id': coa_id(4, 4300)}


@pytest.fixture
def books(tmp_db):
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    own, customer = add_contacts(tmp_db)
    return SimpleNamespace(db=tmp_db, own=own, customer=customer)


def _document(books, number, kind, status):
    """Ein Beleg über 169,00 mit zwei Positionen, die je eine Momentaufnahme tragen – direkt geschrieben: Kein Weg der App setzt sie in dieser
    Version. Die Werte sind in jeder Spalte verschieden, damit eine Kopie, die eine Spalte vergisst, auffällt."""
    assert set(FIRST_SNAPSHOT) == set(SECOND_SNAPSHOT) == set(ITEM_SNAPSHOT_KEYS.values())
    document_id = add_document(books.db, number, kind, books.own, books.customer, status=status, sum_net=150.0, tax_amount=19.0,
                               sum_gross=169.0, amount_due=169.0, mixed_tax_rates=1)      # zwei Sätze: ein Beleg mit Steuersatz je Position
    with closing(sqlite3.connect(books.db.db_name)) as con:
        con.execute('DELETE FROM InvoiceItems WHERE InvoiceId = ?', (document_id,))
        con.commit()
    books.db.insert_invoice_item({'invoice_id': document_id, 'position': 1, 'description': 'Standardleistung', 'quantity': 2,
                                  'unit': 'C62', 'price_per_unit': 50.0, 'total_net': 100.0, 'tax_rate': 19, **FIRST_SNAPSHOT})
    books.db.insert_invoice_item({'invoice_id': document_id, 'position': 2, 'description': 'Steuerfreie Leistung', 'quantity': 1,
                                  'unit': 'C62', 'price_per_unit': 50.0, 'total_net': 50.0, 'tax_rate': 0, **SECOND_SNAPSHOT})
    return document_id


def _item_rows(db, invoice_id):
    with closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(f'SELECT {", ".join(ITEM_COLUMNS)} FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position, ID',
                           (invoice_id,)).fetchall()


def test_the_fixture_documents_carry_two_different_snapshots(books):
    original = _document(books, 'T-100', 'invoice', 'sent')

    rows = _item_rows(books.db, original)

    assert [row[5:] for row in rows] == [('S', 1900, None, coa_id(4, 4400)), ('E', 0, EXEMPT_REASON, coa_id(4, 4300))]
    assert [row[:5] for row in rows] == [(1, 2.0, 500000, 1000000, 19.0), (2, 1.0, 500000, 500000, 0.0)]


def test_a_storno_invoice_takes_the_snapshot_along_and_negates_only_quantity_and_total(books):
    original = _document(books, 'T-100', 'invoice', 'sent')

    storno = books.db.cancel_invoice(original, '2026-07-01', USER, today=datetime.date(2026, 7, 1))

    assert storno.ok is True, storno.message
    assert _item_rows(books.db, storno.storno_id) == [
        (position, -quantity, price, -total, rate, *snapshot) for position, quantity, price, total, rate, *snapshot in _item_rows(books.db, original)]
    assert [row[5:] for row in _item_rows(books.db, storno.storno_id)] == [('S', 1900, None, coa_id(4, 4400)),
                                                                           ('E', 0, EXEMPT_REASON, coa_id(4, 4300))]      # gleich, nicht negiert
    assert _item_rows(books.db, original)[0][3] == 1000000                           # die Ursprungsrechnung bleibt, wie sie war


def test_a_replacement_invoice_takes_the_snapshot_along_unchanged(books):
    original = _document(books, 'T-100', 'invoice', 'sent')
    assert books.db.cancel_invoice(original, '2026-07-01', USER, today=datetime.date(2026, 7, 1)).ok is True

    reissue = books.db.create_replacement_draft(original, USER, today=datetime.date(2026, 7, 2))

    assert reissue.ok is True, reissue.message
    assert _item_rows(books.db, reissue.invoice_id) == _item_rows(books.db, original)       # Menge, Summe, Satz und Momentaufnahme: alles gleich


def test_converting_a_quote_takes_the_snapshot_along_unchanged(books):
    quote = _document(books, '26A100', 'quote', 'accepted')

    invoice_id = books.db.convert_quote_to_invoice(quote, today=datetime.date(2026, 7, 3))

    assert invoice_id and invoice_id != quote
    assert _item_rows(books.db, invoice_id) == _item_rows(books.db, quote)
    assert [row[5:] for row in _item_rows(books.db, invoice_id)] == [('S', 1900, None, coa_id(4, 4400)), ('E', 0, EXEMPT_REASON, coa_id(4, 4300))]


def test_converting_a_quote_without_a_snapshot_keeps_null_and_s(books):
    quote = add_document(books.db, '26A101', 'quote', books.own, books.customer, status='accepted')      # wie der ganze Bestand

    invoice_id = books.db.convert_quote_to_invoice(quote, today=datetime.date(2026, 7, 3))

    assert [row[5:] for row in _item_rows(books.db, invoice_id)] == [('S', None, None, None)]


# ── coa_is_referenced ────────────────────────────────────────────────────────

def _custom_account(db, number=9701):
    db.insert_chart_of_accounts(4, number, 'Erlöse Testkonto', 'G')
    return coa_id(4, number)


def test_an_account_that_is_the_revenue_account_of_a_tax_rate_counts_as_referenced(tmp_db):
    cid = _custom_account(tmp_db)
    assert tmp_db.coa_is_referenced(cid) is False
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute("INSERT INTO TaxRates (Label, RateBp, Kind, RevenueCoaId) VALUES ('Reduziert', 550, 'standard', ?)", (cid,))
        con.commit()

    assert tmp_db.coa_is_referenced(cid) is True

    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute('UPDATE TaxRates SET RevenueCoaId = NULL WHERE RevenueCoaId = ?', (cid,))
        con.commit()
    assert tmp_db.coa_is_referenced(cid) is False


def test_an_account_that_is_the_revenue_account_of_an_invoice_item_counts_as_referenced(tmp_db):
    cid = _custom_account(tmp_db)
    invoice_id = _make_document(tmp_db)
    assert tmp_db.coa_is_referenced(cid) is False
    tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 2, 'description': 'Steuerfreie Leistung', 'quantity': 1,
                                'price_per_unit': 10.0, 'total_net': 10.0, 'tax_category': 'E', 'tax_rate': 0, 'tax_rate_bp': 0,
                                'tax_exempt_reason': EXEMPT_REASON, 'revenue_coa_id': cid})

    assert tmp_db.coa_is_referenced(cid) is True

    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute('DELETE FROM InvoiceItems WHERE RevenueCoaId = ?', (cid,))
        con.commit()
    assert tmp_db.coa_is_referenced(cid) is False


def test_deleting_such_an_account_is_refused_with_one_fixed_sentence_and_the_account_stays(tmp_db):
    cid = _custom_account(tmp_db)
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute("INSERT INTO TaxRates (Label, RateBp, Kind, RevenueCoaId) VALUES ('Reduziert', 550, 'standard', ?)", (cid,))
        con.commit()

    status, location = handlers.handle_delete_skr(tmp_db, cid)

    assert status == 303 and location.startswith('/masterdata/skr?msg=') and location.endswith('&type=error')
    message = parse_qs(urlsplit(location).query)['msg'][0]
    assert message == 'Konto wird in Buchungen, Anlagen, Steuersätzen oder Rechnungen verwendet und kann nicht gelöscht werden.'
    assert any(row[0] == cid for row in tmp_db.fetch_chart_of_accounts())


def test_an_account_nothing_refers_to_is_still_deleted(tmp_db):
    cid = _custom_account(tmp_db)

    status, location = handlers.handle_delete_skr(tmp_db, cid)

    assert status == 303 and location.endswith('&type=success')
    assert not any(row[0] == cid for row in tmp_db.fetch_chart_of_accounts())
