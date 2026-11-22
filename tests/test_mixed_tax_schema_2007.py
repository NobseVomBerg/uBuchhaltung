# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.7.0 (Schema 2007): das Kennzeichen „Steuersatz je Position“ am Beleg.

``Invoices`` bekommt am Ende die Spalte ``MixedTaxRates`` (ganze Zahl, ohne Vorgabe: NULL = ein Satz für den ganzen Beleg – der
ganze Bestand –, 1 = Satz je Position). Geprüft wird:

* Eine frische und eine migrierte Datenbank haben dieselbe Spaltenreihenfolge (die Rechnungs-Zeilen werden positionsbasiert gelesen,
  ``InvoiceCol``: alles, was bis 2.6.x an einem Index stand, bleibt dort; die neue Spalte steht ganz hinten, Index 55), dieselben
  Tabellen und Indizes – auch aus 2.5.x, 2.4.x, 2.3.x, 2.2.x, 2.1.x, 2.0.x und 1.x.
* Der Bestand bleibt, wie er war: jede Spalte jeder Zeile, alle anderen Tabellen; der neue Wert ist überall NULL – nichts wird
  umgedeutet.
* Vor der Umstellung entsteht eine Sicherung (Stand 2006), scheitert sie, bleibt die Datenbank unberührt; bricht die Umstellung ab,
  rollt sie ganz zurück, und ein zweiter Start klappt. Eine Datenbank aus einer neueren Version (2008) wird nicht geöffnet.
* ``has_mixed_rates`` liest streng: Nur die ganze Zahl 1 ist wahr. ``insert_invoice`` schreibt nur ``None`` oder 1.

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
from db import invoices as db_invoices
from db.invoices import InvoiceCol, MIXED_LINK_NOT_BOOKABLE, has_mixed_rates
from db.schema import MIXED_TAX_SCHEMA
from tests import installation
from tests.document_helpers import add_contacts, add_document
from tests.entry_form_world import (to_state_2000, to_state_2001, to_state_2002, to_state_2003, to_state_2004,
                                    to_state_2005, to_state_2006)

NEW_COLUMN = 'MixedTaxRates'
OLD_INVOICE_COLUMNS = 55                    # Index 0 bis 54 (…, DocumentVersion) bis 2.6.x


def _reopen(path):
    """Die Datei so öffnen, als wäre das Programm neu gestartet worden."""
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def _info(path, table='Invoices'):
    with closing(sqlite3.connect(path)) as con:
        return [row[1:] for row in con.execute(f'PRAGMA table_info({table})')]     # ohne die laufende Nummer


def _names(path, table='Invoices'):
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
    """Ein Bestand im Stand von 2.6.x: Rechnungen in mehreren Status (eine mit 7 % im Kopf, aber 19 % an der Position – so einen
    Bestand deutet nichts um –, eine ohne Steuerausweis), ein Angebot, Positionen, eine Zahlung."""
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    own, customer = add_contacts(db)
    add_document(db, '26R001', 'invoice', own, customer, status='sent')
    add_document(db, '26R002', 'invoice', own, customer, status='draft', tax_rate=0.07, sum_net=100.0, tax_amount=7.0,
                 sum_gross=107.0, amount_due=107.0)
    paid = add_document(db, '26R003', 'invoice', own, customer, status='paid')
    add_document(db, '26R004', 'invoice', own, customer, status='draft', tax_rate=-1, tax_amount=0.0, sum_gross=100.0,
                 amount_due=100.0)
    add_document(db, '26A001', 'quote', own, customer, status='accepted')
    booking = db.insert_booking('2026-07-01', 119.0, booking_type='bank')
    db.link_invoice_to_transaction(paid, booking, 119.0)
    to_state_2006(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


# ── Der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_stock_is_a_2_6_x_database_before_the_migration(stock):
    assert installation.user_version(stock) == 2006
    assert NEW_COLUMN not in _names(stock) and len(_names(stock)) == OLD_INVOICE_COLUMNS
    assert len(_contents(stock)['Invoices']) == 5


def test_the_step_back_to_2006_can_be_repeated(stock):
    to_state_2006(stock)
    to_state_2006(stock)
    assert installation.user_version(stock) == 2006 and NEW_COLUMN not in _names(stock)


def test_this_program_is_a_2_7_x_program():
    assert MIXED_TAX_SCHEMA == 2007
    assert version.SCHEMA_VERSION >= 2007 and version.SCHEMA_STEP >= 7
    assert version.APP_VERSION.startswith('2.') and int(version.APP_VERSION.split('.')[1]) >= 7


# ── Frisch = migriert ────────────────────────────────────────────────────────

def test_a_fresh_database_has_the_column_last_at_index_55(tmp_db):
    names = _names(tmp_db.db_name)
    assert names[-1] == NEW_COLUMN and names.index(NEW_COLUMN) == InvoiceCol.MIXED_TAX_RATES == 55 == len(names) - 1
    assert _info(tmp_db.db_name)[-1] == (NEW_COLUMN, 'INTEGER', 0, None, 0)            # ganze Zahl, NULL erlaubt, keine Vorgabe
    assert names[InvoiceCol.DOCUMENT_VERSION] == 'DocumentVersion'


def test_a_fresh_and_a_migrated_database_have_the_same_columns_tables_and_indexes(stock, tmp_path):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    _reopen(stock)

    assert _names(stock)[-1] == NEW_COLUMN and len(_names(stock)) == OLD_INVOICE_COLUMNS + 1
    assert _info(stock) == _info(fresh)                                              # Name, Typ, NOT NULL, Vorgabe, PK
    with closing(sqlite3.connect(stock)) as a, closing(sqlite3.connect(fresh)) as b:
        for query in ("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
                      "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name",
                      "SELECT sql FROM sqlite_master WHERE name LIKE 'ux_%' OR name LIKE 'idx_%' ORDER BY name"):
            assert [r[0] for r in a.execute(query)] == [r[0] for r in b.execute(query)]
    assert installation.user_version(stock) == installation.user_version(fresh) == version.SCHEMA_VERSION


@pytest.mark.parametrize('older', [to_state_2005, to_state_2004, to_state_2003, to_state_2002, to_state_2001, to_state_2000, None],
                         ids=['aus-2.5.x', 'aus-2.4.x', 'aus-2.3.x', 'aus-2.2.x', 'aus-2.1.x', 'aus-2.0.x', 'aus-1.x'])
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
    assert _names(stock)[-1] == NEW_COLUMN
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_the_column_positions_of_the_invoice_readers_stay_where_they_were(stock):
    """Was ``Invoices`` positionsbasiert liest, bleibt unberührt: alles bis ``DocumentVersion`` (Index 54) steht, wo es stand –
    die neue Spalte hängt hinten an (Index 55)."""
    before = _names(stock)
    _reopen(stock)

    after = _names(stock)
    assert after[:OLD_INVOICE_COLUMNS] == before and after[OLD_INVOICE_COLUMNS] == NEW_COLUMN
    assert (after[35], after[36], after[39], after[40], after[45]) == ('TaxRate', 'SumNet', 'AmountDue', 'Status', 'DocumentType')
    assert (after[InvoiceCol.INVOICE_KIND], after[InvoiceCol.DOCUMENT_VERSION]) == ('InvoiceKind', 'DocumentVersion')


# ── Der Bestand bleibt ───────────────────────────────────────────────────────

def test_existing_rows_stay_untouched_and_the_new_value_is_null(stock):
    before = _contents(stock)
    assert len(before['Invoices']) == 5 and len(before['InvoiceItems']) == 5 and len(before['InvoicePayments']) == 1

    db = _reopen(stock)

    after = _contents(stock)
    assert all(row[-1] is None for row in after['Invoices'])                       # überall NULL – nichts wird umgedeutet
    assert [row[:-1] for row in after['Invoices']] == before['Invoices']            # jede Spalte jeder Zeile
    assert all(row[-3:] == (None, None, None) for row in after['InvoiceItems'])    # seit 2.8.0 drei Spalten am Ende, leer (test_tax_rates_schema_2008.py)
    after['InvoiceItems'] = [row[:-3] for row in after['InvoiceItems']]
    for table, rows in before.items():
        if table != 'Invoices':
            assert after[table] == rows, table                                     # alle anderen Tabellen
    assert not any(has_mixed_rates(row[InvoiceCol.MIXED_TAX_RATES]) for row in db.fetch_invoices() + db.fetch_quotes())


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
    assert installation.user_version(str(backup)) == 2006
    assert NEW_COLUMN not in _names(str(backup))
    assert installation.dump(str(backup)) == before                                 # Zeile für Zeile der alte Stand
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_failing_backup_stops_the_opening_and_leaves_the_stock_at_2006(stock):
    before = installation.dump(stock)
    (Path(stock).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError):
        _reopen(stock)

    assert installation.user_version(stock) == 2006 and installation.dump(stock) == before
    assert NEW_COLUMN not in _names(stock)
    (Path(stock).parent / 'backup').unlink()                                       # Ursache behoben: jetzt klappt es
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[-1] == NEW_COLUMN


def test_an_aborted_migration_rolls_the_new_column_back(stock, monkeypatch):
    before = installation.dump(stock)

    def boom(self, cursor):
        assert NEW_COLUMN in [row[1] for row in cursor.execute('PRAGMA table_info(Invoices)')]     # die Spalte ist schon angelegt …
        raise RuntimeError('Abbruch mitten in der Umstellung')                                       # … und der Schritt danach scheitert

    monkeypatch.setattr(Database, '_ensure_invoice_version_schema', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(stock)
    finally:
        monkeypatch.undo()

    assert installation.user_version(stock) == 2006
    assert NEW_COLUMN not in _names(stock)
    assert installation.dump(stock) == before                                       # Spalten, Tabellen, Zeilen wie vorher

    _reopen(stock)                                                                  # und beim nächsten Start klappt es
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[-1] == NEW_COLUMN


def test_a_column_that_is_already_there_does_not_stop_the_migration(stock):
    with closing(sqlite3.connect(stock)) as con:
        con.execute('ALTER TABLE Invoices ADD COLUMN MixedTaxRates INTEGER')        # halb durchgeführt
        con.commit()

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock)[OLD_INVOICE_COLUMNS:] == [NEW_COLUMN]                      # nicht doppelt


def test_a_database_of_a_newer_version_is_not_opened(stock):
    installation.stamp_user_version(stock, max(2008, version.SCHEMA_VERSION + 1))
    before = installation.dump(stock)

    with pytest.raises(SchemaTooNewError):
        Database(db_name=stock)

    assert installation.dump(stock) == before and NEW_COLUMN not in _names(stock)   # nichts angefasst, nichts zurückgestempelt


# ── has_mixed_rates liest streng ─────────────────────────────────────────────

def test_only_the_whole_number_one_means_a_rate_per_item():
    assert has_mixed_rates(1) is True
    for value in (None, 0, 2, -1, True, False, 1.0, 0.5, Decimal(1), '1', 'x', '', b'1', [1], (1,), {1: 1}):
        assert has_mixed_rates(value) is False, repr(value)


def test_the_database_layer_names_the_reader_and_the_sentence_in_db_invoices():
    """Die Lesefunktion und der Satz des einen Wegs, der so einen Beleg noch ablehnt (``link_invoice_to_transaction``), stehen für jede Stelle im Modul
    der Rechnungen (``db.invoices``). Den allgemeinen Satz „lässt sich in dieser Version noch nicht buchen“ gibt es nicht mehr: Alle anderen Wege buchen
    je Satz (oder rechnen, wie der Neuaufbau, nur den offenen Betrag)."""
    assert InvoiceCol.MIXED_TAX_RATES == 55
    assert MIXED_LINK_NOT_BOOKABLE == 'Einer Rechnung mit mehreren Steuersätzen lässt sich eine Zahlung nur mit ihren Buchungssätzen zuordnen.'
    assert not hasattr(db_invoices, 'MIXED_NOT_BOOKABLE')


# ── Geschrieben wird nur NULL oder 1 ─────────────────────────────────────────

def _head(**over):
    fields = {'invoice_number': 'T-1', 'invoice_date': '2026-03-04', 'seller_name': 'Eigene Firma GmbH',
              'seller_company': 'Eigene Firma GmbH', 'buyer_name': 'Max Mustermann', 'buyer_company': 'Kunde GmbH',
              'tax_rate': 0.19, 'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0, 'amount_due': 119.0}
    fields.update(over)
    return fields


def _stored(db):
    with closing(sqlite3.connect(db.db_name)) as con:
        return [row[0] for row in con.execute('SELECT MixedTaxRates FROM Invoices ORDER BY ID')]


def test_insert_invoice_writes_null_without_the_key_and_with_none_and_one_with_one(tmp_db):
    tmp_db.insert_invoice(_head(invoice_number='T-1'))
    tmp_db.insert_invoice(_head(invoice_number='T-2', mixed_tax_rates=None))
    tmp_db.insert_invoice(_head(invoice_number='T-3', mixed_tax_rates=1))

    assert _stored(tmp_db) == [None, None, 1]
    with closing(sqlite3.connect(tmp_db.db_name)) as con:                          # gespeichert als ganze Zahl, nicht als Text
        assert con.execute('SELECT typeof(MixedTaxRates) FROM Invoices WHERE InvoiceNumber = ?', ('T-3',)).fetchone()[0] == 'integer'


@pytest.mark.parametrize('value', [0, 2, -1, True, False, 1.0, '1', 'ja', b'1', [1]],
                         ids=['0', '2', 'minus-1', 'True', 'False', '1.0', 'Text-1', 'Text', 'Bytes', 'Liste'])
def test_insert_invoice_refuses_every_other_value_before_it_writes(tmp_db, value):
    before = installation.dump(tmp_db.db_name)

    with pytest.raises(ValueError):
        tmp_db.insert_invoice(_head(mixed_tax_rates=value))

    assert installation.dump(tmp_db.db_name) == before and _stored(tmp_db) == []


def test_insert_invoice_with_a_transaction_writes_the_flag_with_it(tmp_db):
    with tmp_db.transaction() as tx:
        tmp_db.insert_invoice(_head(mixed_tax_rates=1), tx=tx)
        entries = [params for sql, params, _description in tx.log if 'INSERT INTO Invoices' in sql]

    assert _stored(tmp_db) == [1] and entries and entries[0][-1] == 1             # auch im Replay-Log (letzter Parameter)
