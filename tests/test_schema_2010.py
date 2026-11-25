# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.10.0 (Schema 2010): Leistungszeitraum, Zahlungsart, Zusatzfelder der Bankbewegung, „exportiert“.

Neu sind drei Spalten am Ende von ``Invoices`` (Index 56 bis 58, alle NULL erlaubt, ohne Vorgabe): ``ServiceDateEnd`` (Ende des Leistungszeitraums,
NULL = nur das Leistungsdatum), ``PaymentMeans`` (Zahlungsart nach UNTDID 4461, NULL = Überweisung) und ``QuoteStatusBefore`` (Status des Angebots vor
der Umwandlung, NULL = ``accepted``); dazu die Tabellen ``BankTransactionDetails`` (Zusatzfelder einer Bankbewegung aus CAMT) und ``BookingExports``
(der Satz stand in einem DATEV-Stapel) – beide mit Fremdschlüssel auf ``Bookings`` und ``ON DELETE CASCADE`` – und der Index ``idx_bookings_sourcegroup``.
``Bookings`` selbst bleibt bei 24 Spalten. Geprüft wird:

* Eine frische und eine migrierte Datenbank haben dieselben Spalten in derselben Reihenfolge, dieselben Tabellen und Indizes – auch aus 2.9.x bis 2.0.x
  und 1.x.
* Der Bestand bleibt, wie er war: jede Spalte jeder Zeile jeder Tabelle; die neuen Werte sind überall NULL, die neuen Tabellen leer – nichts wird umgedeutet.
* Vor der Umstellung entsteht eine Sicherung (Stand 2009), scheitert sie, bleibt die Datenbank unberührt; bricht die Umstellung ab, rollt sie ganz zurück
  (Spalten, Tabellen, Index), und ein zweiter Start klappt. Eine Datenbank aus einer neueren Version (2011) wird nicht geöffnet.
* Die strengen Leser (``payment_means_code``, ``stored_payment_means``, ``service_period``, ``normalize_service_end``, ``restored_quote_status``,
  ``clean_bank_text``, ``clean_value_date``) und die Bausteine der Datenbankschicht (``insert_booking(bank_details=…)``, ``get_bank_details``,
  ``bank_stock``, ``mark_datev_export``, ``exported_booking_ids``).

Namen und Nummern sind erfundene Testwerte.
"""
import datetime
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import version
from db import Database, MigrationBackupError, SchemaTooNewError, _CoreMixin
from db.bookings import BankDetails
from db.invoices import (PAYMENT_MEANS, PAYMENT_MEANS_DEFAULT, PAYMENT_MEANS_INVALID, SERVICE_PERIOD_MESSAGE, SERVICE_PERIOD_START_MESSAGE,
                         InvoiceCol, normalize_service_end, payment_means_code, restored_quote_status, service_period, stored_payment_means)
from db.schema import DETAILS_SCHEMA
from importers.base import BANK_REF_PLACEHOLDERS, MAX_BANK_TEXT, clean_bank_text, clean_value_date
from tests import installation
from tests.document_helpers import add_contacts, add_document
from tests.entry_form_world import (to_state_2000, to_state_2001, to_state_2002, to_state_2003, to_state_2004, to_state_2005, to_state_2006,
                                    to_state_2007, to_state_2008, to_state_2009)

NEW_COLUMNS = ['ServiceDateEnd', 'PaymentMeans', 'QuoteStatusBefore']
OLD_INVOICE_COLUMNS = 56                          # Index 0 bis 55 (…, MixedTaxRates) bis 2.9.x
NEW_TABLES = {'BankTransactionDetails', 'BookingExports'}
INDEX = 'idx_bookings_sourcegroup'
BOOKING_COLUMNS = 24                              # ID … DatevKey: ``Bookings`` bekommt keine Spalte


DAY = datetime.date(2026, 6, 9)                 # der Tag der Umwandlung (``today=``): Die Rechnung aus dem Angebot bekommt ihre Nummer im Kreis dieses Jahres


def _reopen(path):
    """Die Datei so öffnen, als wäre das Programm neu gestartet worden."""
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def _info(path, table='Invoices'):
    with closing(sqlite3.connect(path)) as con:
        return [row[1:] for row in con.execute(f'PRAGMA table_info({table})')]     # ohne die laufende Nummer


def _names(path, table='Invoices'):
    return [row[0] for row in _info(path, table)]


def _tables(path):
    with closing(sqlite3.connect(path)) as con:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def _index_sql(path, name):
    with closing(sqlite3.connect(path)) as con:
        row = con.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)).fetchone()
    return row[0] if row else None


def _backups(path):
    directory = Path(path).parent / 'backup'
    return sorted(directory.iterdir()) if directory.is_dir() else []


def _contents(path):
    with closing(sqlite3.connect(path)) as con:
        tables = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        return {t: con.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables}


def _sql(path, query, params=()):
    with closing(sqlite3.connect(path)) as con:
        return con.execute(query, params).fetchall()


@pytest.fixture
def stock(tmp_path):
    """Ein Bestand im Stand von 2.9.x: Rechnungen in mehreren Status, eine bezahlte mit Bankbewegung, eine Rechnung aus einem Angebot und Sätze mit
    Quellklammer (``SourceGroup``) – so, wie 2.9.x sie schrieb (ohne die Spalten, Tabellen und den Index von 2.10.x)."""
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    own, customer = add_contacts(db)
    db.insert_number_range('invoice', DAY.year, 'R', current_number=10)
    add_document(db, '26R001', 'invoice', own, customer, status='sent')
    add_document(db, '26R002', 'invoice', own, customer, status='draft')
    paid = add_document(db, '26R003', 'invoice', own, customer, status='paid')
    add_document(db, '26R004', 'invoice', own, customer, status='cancelled')
    quote = add_document(db, '26A001', 'quote', own, customer, status='sent')
    assert db.convert_quote_to_invoice(quote, today=DAY)
    bank = db.insert_booking('2026-07-01', 119.0, booking_type='bank')
    db.link_invoice_to_transaction(paid, bank, 119.0)
    entry = db.insert_booking('2026-07-02', -20.0, booking_type='entry', document_number='ER-1', text='Porto')
    with closing(sqlite3.connect(path)) as con:
        con.execute("UPDATE Bookings SET SourceGroup = 'sg-1' WHERE ID IN (?, ?)", (bank, entry))
        con.commit()
    to_state_2009(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


# ── Der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_stock_is_a_2_9_x_database_before_the_migration(stock):
    assert installation.user_version(stock) == 2009
    assert len(_names(stock)) == OLD_INVOICE_COLUMNS and _names(stock)[-1] == 'MixedTaxRates'
    assert not set(NEW_COLUMNS) & set(_names(stock)) and not NEW_TABLES & _tables(stock)
    assert _index_sql(stock, INDEX) is None
    assert len(_names(stock, 'Bookings')) == BOOKING_COLUMNS
    assert len(_sql(stock, "SELECT 1 FROM Invoices WHERE SourceQuoteId IS NOT NULL")) == 1       # die Rechnung aus dem Angebot


def test_the_step_back_to_2009_can_be_repeated(stock):
    to_state_2009(stock)
    to_state_2009(stock)
    assert installation.user_version(stock) == 2009 and len(_names(stock)) == OLD_INVOICE_COLUMNS


def test_this_program_is_a_2_10_x_program():
    assert DETAILS_SCHEMA == 2010
    assert version.SCHEMA_VERSION >= 2010 and version.SCHEMA_STEP >= 10
    assert version.APP_VERSION.startswith('2.') and int(version.APP_VERSION.split('.')[1]) >= 10


# ── Frisch = migriert ────────────────────────────────────────────────────────

def test_a_fresh_database_ends_the_invoice_columns_with_the_three_new_ones(tmp_db):
    names = _names(tmp_db.db_name)
    assert names[OLD_INVOICE_COLUMNS:] == NEW_COLUMNS and len(names) == OLD_INVOICE_COLUMNS + 3
    assert [names.index(column) for column in NEW_COLUMNS] == [56, 57, 58]
    assert _info(tmp_db.db_name)[56:] == [('ServiceDateEnd', 'DATE', 0, None, 0), ('PaymentMeans', 'TEXT', 0, None, 0),
                                          ('QuoteStatusBefore', 'TEXT', 0, None, 0)]       # NULL erlaubt, keine Vorgabe: NULL heißt „wie bisher“


def test_a_fresh_database_has_the_new_tables_the_index_and_24_booking_columns(tmp_db):
    path = tmp_db.db_name
    assert NEW_TABLES <= _tables(path)
    assert _info(path, 'BankTransactionDetails') == [('Booking_ID', 'INTEGER', 0, None, 1), ('ValueDate', 'DATE', 0, None, 0),
                                                     ('EndToEndId', 'TEXT', 0, None, 0), ('MandateRef', 'TEXT', 0, None, 0),
                                                     ('CreditorId', 'TEXT', 0, None, 0), ('BankRef', 'TEXT', 0, None, 0)]
    assert _info(path, 'BookingExports') == [('Booking_ID', 'INTEGER', 0, None, 1), ('ExportedAt', 'DATETIME', 1, None, 0)]
    assert _index_sql(path, INDEX) is not None and 'SourceGroup' in _index_sql(path, INDEX)
    assert len(_names(path, 'Bookings')) == BOOKING_COLUMNS and _names(path, 'Bookings')[-1] == 'DatevKey'


def test_a_fresh_and_a_migrated_database_have_the_same_columns_tables_and_indexes(stock, tmp_path):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    _reopen(stock)

    assert _names(stock)[OLD_INVOICE_COLUMNS:] == NEW_COLUMNS
    assert _info(stock) == _info(fresh)                                                  # Name, Typ, NOT NULL, Vorgabe, PK
    for table in ('BankTransactionDetails', 'BookingExports', 'Bookings'):
        assert _info(stock, table) == _info(fresh, table), table
    assert _tables(stock) == _tables(fresh) and NEW_TABLES <= _tables(stock)
    with closing(sqlite3.connect(stock)) as a, closing(sqlite3.connect(fresh)) as b:
        for query in ("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name",
                      "SELECT sql FROM sqlite_master WHERE name LIKE 'ux_%' OR name LIKE 'idx_%' ORDER BY name",
                      "SELECT sql FROM sqlite_master WHERE type = 'table' AND name IN ('BankTransactionDetails', 'BookingExports') ORDER BY name"):
            assert [r[0] for r in a.execute(query)] == [r[0] for r in b.execute(query)]
    assert _index_sql(stock, INDEX) == _index_sql(fresh, INDEX) is not None              # auf einem Bestand mit Zeilen angelegt
    assert installation.user_version(stock) == installation.user_version(fresh) == version.SCHEMA_VERSION


@pytest.mark.parametrize('older', [to_state_2009, to_state_2008, to_state_2007, to_state_2006, to_state_2005, to_state_2004, to_state_2003,
                                   to_state_2002, to_state_2001, to_state_2000, None],
                         ids=['aus-2.9.x', 'aus-2.8.x', 'aus-2.7.x', 'aus-2.6.x', 'aus-2.5.x', 'aus-2.4.x', 'aus-2.3.x', 'aus-2.2.x', 'aus-2.1.x',
                              'aus-2.0.x', 'aus-1.x'])
def test_older_stocks_end_with_the_same_column_order_as_a_fresh_database(stock, tmp_path, older):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    if older is not None:
        older(stock)
    else:
        installation.stamp_user_version(stock, 4)
    _reopen(stock)

    for table in ('Accounts', 'Contacts', 'Bookings', 'CompanyDetails', 'Invoices', 'InvoiceItems', 'InvoicePayments', 'TaxRates',
                  'BankTransactionDetails', 'BookingExports'):
        assert _info(stock, table) == _info(fresh, table), table
    assert _names(stock)[OLD_INVOICE_COLUMNS:] == NEW_COLUMNS
    assert _index_sql(stock, INDEX) == _index_sql(fresh, INDEX) is not None
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_the_older_steps_also_take_the_new_parts_away(stock):
    """Jeder ältere Stand beginnt mit ``to_state_2009``: Er trägt die neuen Spalten, Tabellen und den Index nie – sonst wäre er kein Stand von damals."""
    _reopen(stock)
    assert set(NEW_COLUMNS) <= set(_names(stock)) and NEW_TABLES <= _tables(stock) and _index_sql(stock, INDEX)

    to_state_2008(stock)

    assert not set(NEW_COLUMNS) & set(_names(stock)) and not NEW_TABLES & _tables(stock) and _index_sql(stock, INDEX) is None
    assert installation.user_version(stock) == 2008


def test_the_column_positions_of_the_invoice_readers_stay_where_they_were(stock):
    """Was ``Invoices`` positionsbasiert liest, bleibt unberührt: alles bis ``MixedTaxRates`` (Index 55) steht, wo es stand – die drei neuen Spalten
    hängen hinten an (Index 56 bis 58), und die Namen in ``InvoiceCol`` zeigen darauf."""
    before = _names(stock)
    _reopen(stock)

    after = _names(stock)
    assert after[:OLD_INVOICE_COLUMNS] == before and after[OLD_INVOICE_COLUMNS:] == NEW_COLUMNS
    assert (after[35], after[36], after[39], after[40], after[45]) == ('TaxRate', 'SumNet', 'AmountDue', 'Status', 'DocumentType')
    assert after[InvoiceCol.MIXED_TAX_RATES] == 'MixedTaxRates' and InvoiceCol.MIXED_TAX_RATES == 55
    assert (InvoiceCol.SERVICE_DATE_END, InvoiceCol.PAYMENT_MEANS, InvoiceCol.QUOTE_STATUS_BEFORE) == (56, 57, 58)
    assert [after[InvoiceCol.SERVICE_DATE_END], after[InvoiceCol.PAYMENT_MEANS], after[InvoiceCol.QUOTE_STATUS_BEFORE]] == NEW_COLUMNS


def test_the_new_tables_hang_on_the_booking_with_cascade(stock):
    _reopen(stock)
    for table in ('BankTransactionDetails', 'BookingExports'):
        with closing(sqlite3.connect(stock)) as con:
            keys = con.execute(f'PRAGMA foreign_key_list({table})').fetchall()
        assert [(key[2], key[3], key[4], key[6]) for key in keys] == [('Bookings', 'Booking_ID', 'ID', 'CASCADE')], table     # Tabelle, von, nach, ON DELETE


# ── Der Bestand bleibt ───────────────────────────────────────────────────────

def test_existing_rows_stay_untouched_and_the_new_values_are_empty(stock):
    before = _contents(stock)
    assert len(before['Invoices']) == 6 and len(before['InvoiceItems']) == 6 and len(before['Bookings']) == 2
    assert len(before['Invoices'][0]) == OLD_INVOICE_COLUMNS and not NEW_TABLES & set(before)

    _reopen(stock)

    after = _contents(stock)
    assert [row[:OLD_INVOICE_COLUMNS] for row in after['Invoices']] == before['Invoices']          # jede Spalte jeder Zeile
    assert all(row[OLD_INVOICE_COLUMNS:] == (None, None, None) for row in after['Invoices'])        # überall NULL – nichts wird umgedeutet
    for table, rows in before.items():
        if table != 'Invoices':
            assert after[table] == rows, table                                                       # alle anderen Tabellen, Zeile für Zeile
    assert after['BankTransactionDetails'] == [] and after['BookingExports'] == []                   # die neuen Tabellen sind leer
    assert [row[21] for row in after['Bookings']] == ['sg-1', 'sg-1']                                # die Quellklammer bleibt, wie sie war


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
    assert installation.user_version(str(backup)) == 2009
    assert not set(NEW_COLUMNS) & set(_names(str(backup))) and not NEW_TABLES & _tables(str(backup))
    assert installation.dump(str(backup)) == before                                  # Zeile für Zeile der alte Stand
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_failing_backup_stops_the_opening_and_leaves_the_stock_at_2009(stock):
    before = installation.dump(stock)
    (Path(stock).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError):
        _reopen(stock)

    assert installation.user_version(stock) == 2009 and installation.dump(stock) == before
    assert len(_names(stock)) == OLD_INVOICE_COLUMNS and not NEW_TABLES & _tables(stock)
    (Path(stock).parent / 'backup').unlink()                                         # Ursache behoben: jetzt klappt es
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[OLD_INVOICE_COLUMNS:] == NEW_COLUMNS


def test_an_aborted_migration_rolls_everything_back_columns_tables_and_index(stock, monkeypatch):
    before = installation.dump(stock)
    original = Database._ensure_details_schema

    def boom(self, cursor):
        original(self, cursor)
        assert set(NEW_COLUMNS) <= {row[1] for row in cursor.execute('PRAGMA table_info(Invoices)')}        # die Spalten sind schon angefügt …
        assert NEW_TABLES <= {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}   # … Tabellen und Index angelegt …
        assert cursor.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (INDEX,)).fetchone()
        raise RuntimeError('Abbruch mitten in der Umstellung')                       # … und dann scheitert die Umstellung

    monkeypatch.setattr(Database, '_ensure_details_schema', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(stock)
    finally:
        monkeypatch.undo()

    assert installation.user_version(stock) == 2009
    assert len(_names(stock)) == OLD_INVOICE_COLUMNS and not NEW_TABLES & _tables(stock) and _index_sql(stock, INDEX) is None
    assert installation.dump(stock) == before                                        # Spalten, Tabellen, Index, Zeilen wie vorher

    _reopen(stock)                                                                   # und beim nächsten Start klappt es
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[OLD_INVOICE_COLUMNS:] == NEW_COLUMNS
    assert NEW_TABLES <= _tables(stock) and _index_sql(stock, INDEX)


def test_a_column_that_is_already_there_does_not_stop_the_migration(stock):
    with closing(sqlite3.connect(stock)) as con:
        con.execute('ALTER TABLE Invoices ADD COLUMN ServiceDateEnd DATE')           # halb durchgeführt
        con.commit()

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock)[OLD_INVOICE_COLUMNS:] == NEW_COLUMNS                        # nicht doppelt, in der Reihenfolge der frischen Datenbank


def test_a_database_of_a_newer_version_is_not_opened(stock):
    installation.stamp_user_version(stock, max(2011, version.SCHEMA_VERSION + 1))
    before = installation.dump(stock)

    with pytest.raises(SchemaTooNewError):
        Database(db_name=stock)

    assert installation.dump(stock) == before and len(_names(stock)) == OLD_INVOICE_COLUMNS      # nichts angefasst, nichts zurückgestempelt


# ── Die strengen Leser der Rechnung ──────────────────────────────────────────

@pytest.mark.parametrize('raw, code', [
    pytest.param(None, '58', id='NULL-ist-Überweisung'), ('58', '58'), ('10', '10'), ('48', '48'), ('1', '1'),
    pytest.param('59', None, id='Lastschrift-ist-nicht-vorgesehen'), pytest.param('', None, id='leer'), (' 10', None), ('10 ', None), ('01', None),
    ('ZZZ', None), (10, None), (58, None), (1, None), (True, None), (False, None), (1.0, None), (b'10', None), ([], None)])
def test_payment_means_code_reads_strictly(raw, code):
    assert payment_means_code(raw) == code


def test_the_payment_means_are_four_and_the_default_is_the_transfer():
    assert list(PAYMENT_MEANS) == ['58', '10', '48', '1'] and PAYMENT_MEANS_DEFAULT == '58'       # Reihenfolge = Auswahl in der Maske
    assert all(isinstance(label, str) and label for label in PAYMENT_MEANS.values())


@pytest.mark.parametrize('value, stored', [(None, None), ('', None), ('58', None), ('10', '10'), ('48', '48'), ('1', '1')])
def test_stored_payment_means_writes_null_for_the_transfer(value, stored):
    assert stored_payment_means(value) == stored


@pytest.mark.parametrize('value', ['59', ' 10', '10 ', '0', 'ZZZ', 10, 58, 1, True, False, 1.0, b'10', ['10']])
def test_stored_payment_means_refuses_everything_else(value):
    with pytest.raises(ValueError, match=PAYMENT_MEANS_INVALID):
        stored_payment_means(value)


@pytest.mark.parametrize('start, end, period', [
    ('2026-03-01', '2026-03-31', (datetime.date(2026, 3, 1), datetime.date(2026, 3, 31))),
    ('2026-03-31', '2026-04-01', (datetime.date(2026, 3, 31), datetime.date(2026, 4, 1))),
    pytest.param('2026-03-01', '2026-03-01', None, id='gleicher-Tag-ist-kein-Zeitraum'),
    pytest.param('2026-03-31', '2026-03-01', None, id='Ende-vor-Beginn'),
    (None, '2026-03-31', None), ('2026-03-01', None, None), ('2026-03-01', '', None), ('', '2026-03-31', None),
    ('2026-03-01', 'abc', None), ('2026-3-1', '2026-03-31', None), ('2026-03-01', '2026-3-31', None), ('2026-03-01', '2026-02-30', None),
    ('2026-03-01', ' 2026-03-31', None), ('2026-03-01', '2026-03-31T00:00:00', None), ('2026-03-01', '20260331', None),
    ('1899-12-31', '2026-01-01', None), ('2026-01-01', '2201-01-01', None),
    pytest.param(datetime.date(2026, 3, 1), datetime.date(2026, 3, 31), None, id='keine-Texte')])
def test_service_period_needs_two_iso_dates_and_an_end_after_the_start(start, end, period):
    assert service_period(start, end) == period


@pytest.mark.parametrize('start, end, result', [
    ('2026-03-01', None, None), ('2026-03-01', '', None), (None, None, None), (None, '', None),
    ('2026-03-01', '2026-03-31', '2026-03-31'),
    pytest.param('2026-03-01', '2026-03-01', None, id='gleicher-Tag-ist-kein-Zeitraum')])
def test_normalize_service_end_gives_the_end_or_none(start, end, result):
    assert normalize_service_end(start, end) == result


@pytest.mark.parametrize('start, end, message', [
    ('2026-03-01', 'abc', SERVICE_PERIOD_MESSAGE), ('2026-03-01', '2026-02-30', SERVICE_PERIOD_MESSAGE), ('2026-03-01', '2026-3-31', SERVICE_PERIOD_MESSAGE),
    ('2026-03-01', ' 2026-03-31', SERVICE_PERIOD_MESSAGE), ('2026-03-01', 5, SERVICE_PERIOD_MESSAGE), ('2026-03-01', True, SERVICE_PERIOD_MESSAGE),
    ('', 'abc', SERVICE_PERIOD_MESSAGE),                                                 # das Ende wird zuerst gelesen
    ('2026-03-31', '2026-03-01', SERVICE_PERIOD_MESSAGE),                                # Ende vor Beginn
    (None, '2026-03-31', SERVICE_PERIOD_START_MESSAGE), ('', '2026-03-31', SERVICE_PERIOD_START_MESSAGE),
    ('abc', '2026-03-31', SERVICE_PERIOD_START_MESSAGE), ('2026-3-1', '2026-03-31', SERVICE_PERIOD_START_MESSAGE)])
def test_normalize_service_end_refuses_with_the_plain_text(start, end, message):
    with pytest.raises(ValueError) as refused:
        normalize_service_end(start, end)
    assert str(refused.value) == message


def test_the_service_period_texts_are_plain_sentences():
    assert SERVICE_PERIOD_MESSAGE == 'Das Ende des Leistungszeitraums muss ein gültiges Datum nach dem Leistungsdatum sein.'
    assert SERVICE_PERIOD_START_MESSAGE == 'Ein Leistungszeitraum braucht das Leistungsdatum als Beginn.'
    assert PAYMENT_MEANS_INVALID == 'Unbekannte Zahlungsart.'


@pytest.mark.parametrize('raw, status', [
    ('sent', 'sent'), ('draft', 'draft'), ('accepted', 'accepted'),
    pytest.param(None, 'accepted', id='Bestand-NULL'), ('converted', 'accepted'), ('rejected', 'accepted'), ('xyz', 'accepted'), ('', 'accepted'),
    ('Sent', 'accepted'), (' sent', 'accepted'), (1, 'accepted'), (True, 'accepted'), (b'sent', 'accepted'), (['sent'], 'accepted')])
def test_restored_quote_status_reads_strictly(raw, status):
    assert restored_quote_status(raw) == status


# ── Die Reinigung der Bankfelder ─────────────────────────────────────────────

@pytest.mark.parametrize('raw, cleaned', [
    ('REF-1', 'REF-1'), ('  REF-1  ', 'REF-1'), ('a' * MAX_BANK_TEXT, 'a' * MAX_BANK_TEXT), ('Ä Ö ü ß', 'Ä Ö ü ß'),
    pytest.param('a' * (MAX_BANK_TEXT + 1), '', id='36-Zeichen-sind-zu-lang'),
    ('', ''), ('   ', ''), (None, ''), (5, ''), (b'REF', ''), (['REF'], ''),
    pytest.param('ab\x00cd', '', id='Steuerzeichen'), ('a\nb', ''), ('a\tb', ''), ('REF\x7f', ''),
    ('NOTPROVIDED', ''), ('notprovided', ''), ('NonRef', ''), ('NOTAVAILABLE', ''), (' NONREF ', ''),
    pytest.param('NOT PROVIDED', 'NOT PROVIDED', id='nur-die-genauen-Platzhalter')])
def test_clean_bank_text_keeps_only_what_can_be_read_safely(raw, cleaned):
    assert clean_bank_text(raw) == cleaned


def test_the_bank_placeholders_are_the_three_known_ones():
    assert BANK_REF_PLACEHOLDERS == frozenset({'notprovided', 'nonref', 'notavailable'}) and MAX_BANK_TEXT == 35


@pytest.mark.parametrize('raw, cleaned', [
    ('2026-03-01', '2026-03-01'), ('2026-12-31', '2026-12-31'),
    ('', ''), (None, ''), (20260301, ''), (b'2026-03-01', ''), ('2026-3-1', ''), ('20260301', ''), ('01.03.2026', ''), ('2026-02-30', ''),
    ('2026-13-01', ''), (' 2026-03-01', ''), ('2026-03-01 ', ''), ('2026-03-01T00:00:00', ''), ('abc', ''), (datetime.date(2026, 3, 1), '')])
def test_clean_value_date_is_a_calendar_day_in_iso_form_or_empty(raw, cleaned):
    assert clean_value_date(raw) == cleaned


# ── insert_invoice schreibt die drei Spalten ─────────────────────────────────

def _head(**over):
    fields = {'invoice_number': '26R201', 'invoice_date': '2026-03-04', 'delivery_date': '2026-03-04', 'seller_name': 'Eigene Firma GmbH',
              'seller_company': 'Eigene Firma GmbH', 'buyer_name': 'Max Mustermann', 'buyer_company': 'Malermeister Pinsel', 'tax_rate': 0.19,
              'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0, 'amount_due': 119.0, 'status': 'draft'}
    fields.update(over)
    return fields


def _invoice_values(db, invoice_id):
    return _sql(db.db_name, 'SELECT ServiceDateEnd, PaymentMeans, QuoteStatusBefore FROM Invoices WHERE ID = ?', (invoice_id,))[0]


def test_insert_invoice_writes_the_three_new_columns(tmp_db):
    invoice_id = tmp_db.insert_invoice(_head(service_date_end='2026-03-31', payment_means='10', quote_status_before='sent'))
    assert _invoice_values(tmp_db, invoice_id) == ('2026-03-31', '10', 'sent')


def test_insert_invoice_without_the_new_keys_leaves_them_null(tmp_db):
    assert _invoice_values(tmp_db, tmp_db.insert_invoice(_head())) == (None, None, None)


@pytest.mark.parametrize('given, stored', [(None, None), ('', None), ('58', None), ('10', '10'), ('48', '48'), ('1', '1')])
def test_insert_invoice_writes_the_transfer_as_null(tmp_db, given, stored):
    assert _invoice_values(tmp_db, tmp_db.insert_invoice(_head(payment_means=given)))[1] == stored


@pytest.mark.parametrize('given', ['59', ' 10', 10, True, 1])
def test_insert_invoice_refuses_an_unknown_payment_means_before_writing(tmp_db, given):
    with pytest.raises(ValueError, match=PAYMENT_MEANS_INVALID):
        tmp_db.insert_invoice(_head(payment_means=given))
    assert _sql(tmp_db.db_name, 'SELECT COUNT(*) FROM Invoices') == [(0,)]


@pytest.mark.parametrize('given', ['converted', 'rejected', 'xyz', '', 1, True])
def test_insert_invoice_refuses_a_quote_status_that_cannot_come_back(tmp_db, given):
    with pytest.raises(ValueError):
        tmp_db.insert_invoice(_head(quote_status_before=given))
    assert _sql(tmp_db.db_name, 'SELECT COUNT(*) FROM Invoices') == [(0,)]


@pytest.mark.parametrize('status', ['draft', 'sent', 'accepted'])
def test_insert_invoice_takes_every_convertible_quote_status(tmp_db, status):
    assert _invoice_values(tmp_db, tmp_db.insert_invoice(_head(quote_status_before=status)))[2] == status


# ── Zusatzfelder der Bankbewegung: schreiben und lesen ───────────────────────

DETAILS = {'value_date': '2026-07-03', 'end_to_end_id': 'E2E-4711', 'mandate_ref': 'MANDAT-1', 'creditor_id': 'DE98ZZZ09999999999', 'bank_ref': 'BANK-0815'}


def _detail_rows(db):
    return _sql(db.db_name, 'SELECT Booking_ID, ValueDate, EndToEndId, MandateRef, CreditorId, BankRef FROM BankTransactionDetails ORDER BY Booking_ID')


def test_a_bank_row_with_details_gets_one_details_row(tmp_db):
    booking_id = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details=DETAILS)
    assert _detail_rows(tmp_db) == [(booking_id, '2026-07-03', 'E2E-4711', 'MANDAT-1', 'DE98ZZZ09999999999', 'BANK-0815')]


def test_empty_values_are_stored_as_null_and_unusable_ones_are_dropped(tmp_db):
    booking_id = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details={
        'value_date': 'kein Datum', 'end_to_end_id': 'NOTPROVIDED', 'mandate_ref': '', 'creditor_id': 'x' * 36, 'bank_ref': '  BANK-1  '})
    assert _detail_rows(tmp_db) == [(booking_id, None, None, None, None, 'BANK-1')]


@pytest.mark.parametrize('details', [None, {}, {'bank_ref': ''}, {'bank_ref': 'NONREF', 'end_to_end_id': 'NOTPROVIDED', 'value_date': 'x'},
                                     {'value_date': None, 'end_to_end_id': None, 'mandate_ref': None, 'creditor_id': None, 'bank_ref': None}],
                         ids=['None', 'leer', 'leerer-Wert', 'nur-Platzhalter', 'alles-None'])
def test_a_bank_row_without_anything_usable_gets_no_details_row(tmp_db, details):
    tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details=details)
    assert _detail_rows(tmp_db) == []


@pytest.mark.parametrize('booking_type', ['entry', 'split_child'])
def test_only_a_bank_row_gets_details(tmp_db, booking_type):
    tmp_db.insert_booking('2026-07-02', 12.5, booking_type=booking_type, bank_details=DETAILS)
    assert _detail_rows(tmp_db) == []


def test_the_details_are_written_in_the_transaction_of_the_booking(tmp_db):
    class Abort(Exception):
        pass

    with pytest.raises(Abort):
        with tmp_db.transaction() as tx:
            tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details=DETAILS, tx=tx)
            assert _detail_rows(tmp_db) == []                                    # noch nicht festgeschrieben
            raise Abort()
    assert _detail_rows(tmp_db) == [] and _sql(tmp_db.db_name, 'SELECT COUNT(*) FROM Bookings WHERE BookingType = ?', ('bank',)) == [(0,)]

    with tmp_db.transaction() as tx:
        booking_id = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details=DETAILS, tx=tx)
    assert [row[0] for row in _detail_rows(tmp_db)] == [booking_id]


def test_the_details_go_into_the_replay_log_like_the_booking(tmp_db):
    with tmp_db.transaction() as tx:
        booking_id = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details=DETAILS, tx=tx, log_description='Test')
        assert [entry[2] for entry in tx.log][0] == 'Test' and len(tx.log) == 2
        sql, params, _description = tx.log[1]
        assert sql.lstrip().startswith('INSERT INTO BankTransactionDetails') and params[0] == booking_id
    with tmp_db.transaction() as tx:                                             # ohne Beschreibung steht auch der Satz nicht im Protokoll
        tmp_db.insert_booking('2026-07-03', 12.5, booking_type='bank', bank_details=DETAILS, tx=tx)
        assert tx.log == []


def test_without_a_transaction_the_details_are_logged_after_the_commit(tmp_db, monkeypatch):
    seen = []
    monkeypatch.setattr(Database, '_log_sql', lambda self, sql, params, description: seen.append((sql.split('(')[0].strip(), len(_detail_rows(self)))))
    tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details=DETAILS, log_description='Test')
    assert seen == [('INSERT INTO Bookings', 1), ('INSERT INTO BankTransactionDetails', 1)]         # beide erst, als die Zeile schon festgeschrieben ist


def test_deleting_the_booking_deletes_its_details(tmp_db):
    first = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details=DETAILS)
    second = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details={**DETAILS, 'bank_ref': 'BANK-2'})

    tmp_db.delete_transaction(first)

    assert [row[0] for row in _detail_rows(tmp_db)] == [second]                  # CASCADE: nichts bleibt verwaist


def test_get_bank_details_gives_none_or_the_cleaned_values(tmp_db):
    plain = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank')
    full = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details=DETAILS)
    partial = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details={'bank_ref': 'BANK-1'})

    assert tmp_db.get_bank_details(plain) is None and tmp_db.get_bank_details(99999) is None
    assert tmp_db.get_bank_details(full) == BankDetails('2026-07-03', 'E2E-4711', 'MANDAT-1', 'DE98ZZZ09999999999', 'BANK-0815')
    assert tmp_db.get_bank_details(partial) == BankDetails('', '', '', '', 'BANK-1')
    assert BankDetails._fields == ('value_date', 'end_to_end_id', 'mandate_ref', 'creditor_id', 'bank_ref')


def test_get_bank_details_reads_again_what_the_sql_console_may_have_written(tmp_db):
    booking_id = tmp_db.insert_booking('2026-07-02', 12.5, booking_type='bank', bank_details=DETAILS)
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute("UPDATE BankTransactionDetails SET ValueDate = 'gestern', EndToEndId = ?, MandateRef = 'NOTPROVIDED', CreditorId = x'4142', "
                    "BankRef = 'ok-1' WHERE Booking_ID = ?", ('x' * 40, booking_id))
        con.commit()
    assert tmp_db.get_bank_details(booking_id) == BankDetails('', '', '', '', 'ok-1')           # Unlesbares wird leer, der Rest bleibt

    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute("UPDATE BankTransactionDetails SET BankRef = 'a\nb' WHERE Booking_ID = ?", (booking_id,))
        con.commit()
    assert tmp_db.get_bank_details(booking_id) is None                                          # alles unlesbar: wie ohne Zeile


# ── Der Bestand für die Duplikatprüfung ──────────────────────────────────────

def test_bank_stock_lists_each_booking_with_its_reference_in_id_order(tmp_db):
    tmp_db.insert_account('Giro', 'Eigene Firma', 'DE89370400440532013000', 'COBADEFFXXX', 'Testbank', skr_account=1800)
    account = tmp_db.get_account_id_by_skr(1800)
    first = tmp_db.insert_booking('2026-07-02', -12.5, account_id=account, booking_type='bank', bank_details={'bank_ref': 'A-1'})
    second = tmp_db.insert_booking('2026-07-02', -12.5, account_id=account, booking_type='bank')
    third = tmp_db.insert_booking('2026-07-02', -12.5, account_id=account, booking_type='bank', bank_details={'bank_ref': 'C-3'})
    tmp_db.insert_booking('2026-07-03', -12.5, account_id=account, booking_type='bank', bank_details={'bank_ref': 'OTHER-DAY'})
    tmp_db.insert_booking('2026-07-02', -12.0, account_id=account, booking_type='bank', bank_details={'bank_ref': 'OTHER-AMOUNT'})
    with closing(sqlite3.connect(tmp_db.db_name)) as con:                               # eine Referenz, die nicht lesbar ist, zählt als keine
        con.execute("UPDATE BankTransactionDetails SET BankRef = 'NONREF' WHERE BankRef = 'C-3'")
        con.commit()

    assert tmp_db.bank_stock('2026-07-02', -12.5, account) == [(first, 'A-1'), (second, ''), (third, '')]


def test_bank_stock_has_the_same_length_as_check_booking_exists(tmp_db):
    """Dieselbe Bedingung (Datum, Betrag, Konto) – ``check_booking_exists`` bleibt die Zählung, die WISO-Import und Importvorschau kennen."""
    tmp_db.insert_account('Giro', 'Eigene Firma', 'DE89370400440532013000', 'COBADEFFXXX', 'Testbank', skr_account=1800)
    tmp_db.insert_account('Zweitkonto', 'Eigene Firma', 'DE02200505501015871393', 'HASPDEHHXXX', 'Testbank', skr_account=1810)
    giro, other = tmp_db.get_account_id_by_skr(1800), tmp_db.get_account_id_by_skr(1810)
    tmp_db.insert_booking('2026-07-02', -12.5, account_id=giro, booking_type='bank', bank_details={'bank_ref': 'A-1'})
    tmp_db.insert_booking('2026-07-02', -12.5, account_id=giro, booking_type='bank')
    tmp_db.insert_booking('2026-07-02', -12.5, account_id=giro, booking_type='entry')               # jede Buchungsart des Kontos zählt
    tmp_db.insert_booking('2026-07-02', -12.5, account_id=other, booking_type='bank')
    tmp_db.insert_booking('2026-07-02', 12.5, account_id=giro, booking_type='bank')
    tmp_db.insert_booking('2026-07-03', -12.5, account_id=giro, booking_type='bank')
    tmp_db.insert_booking('2026-07-02', -12.5, booking_type='bank')                                 # ohne Konto

    for key in [('2026-07-02', -12.5, giro), ('2026-07-02', -12.5, other), ('2026-07-02', 12.5, giro), ('2026-07-03', -12.5, giro),
                ('2026-07-04', -12.5, giro), ('2026-07-02', -12.5, None), ('2026-07-02', 0, giro), ('2026-07-02', None, giro)]:
        assert len(tmp_db.bank_stock(*key)) == tmp_db.check_booking_exists(*key), key
    assert len(tmp_db.bank_stock('2026-07-02', -12.5, giro)) == 3


# ── „Exportiert“ ─────────────────────────────────────────────────────────────

def _entries(db, count):
    return [db.insert_booking('2026-07-02', -float(n + 1), booking_type='entry', document_number=f'ER-{n}') for n in range(count)]


def _exports(db):
    return dict(_sql(db.db_name, 'SELECT Booking_ID, ExportedAt FROM BookingExports ORDER BY Booking_ID'))


def _tax_dates(db):
    return dict(_sql(db.db_name, 'SELECT ID, DateTax FROM Bookings ORDER BY ID'))


def test_mark_datev_export_stamps_the_tax_date_and_marks_exactly_those_rows(tmp_db):
    one, two, three = _entries(tmp_db, 3)

    tmp_db.mark_datev_export([one, three], '2026-08-01')

    assert _tax_dates(tmp_db) == {one: '2026-08-01', two: None, three: '2026-08-01'}
    assert sorted(_exports(tmp_db)) == [one, three]
    assert all(isinstance(moment, str) and len(moment) == 19 for moment in _exports(tmp_db).values())          # CURRENT_TIMESTAMP, UTC


def test_a_second_export_changes_the_tax_date_as_before_but_never_the_first_export_time(tmp_db):
    one, two = _entries(tmp_db, 2)
    tmp_db.mark_datev_export([one], '2026-08-01')
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute("UPDATE BookingExports SET ExportedAt = '2026-08-01 10:00:00'")          # der erste Export liegt weit zurück
        con.commit()

    tmp_db.mark_datev_export([one, two], '2026-09-15')

    assert _tax_dates(tmp_db) == {one: '2026-09-15', two: '2026-09-15'}                       # das Steuerdatum wie bei ``update_bookings_date_tax_batch``
    exports = _exports(tmp_db)
    assert exports[one] == '2026-08-01 10:00:00' and two in exports and exports[two] != '2026-08-01 10:00:00'       # nur der erste Export zählt


def test_update_bookings_date_tax_batch_stays_and_marks_nothing(tmp_db):
    one, = _entries(tmp_db, 1)
    tmp_db.update_bookings_date_tax_batch([one], '2026-08-01')
    assert _tax_dates(tmp_db) == {one: '2026-08-01'} and _exports(tmp_db) == {}


def test_mark_datev_export_with_nothing_to_mark_changes_nothing(tmp_db):
    one, = _entries(tmp_db, 1)
    tmp_db.mark_datev_export([], '2026-08-01')
    assert _tax_dates(tmp_db) == {one: None} and _exports(tmp_db) == {}


def test_mark_datev_export_ignores_an_id_without_a_booking_like_the_old_batch_did(tmp_db):
    one, = _entries(tmp_db, 1)
    tmp_db.mark_datev_export([one, 99999], '2026-08-01')
    assert _tax_dates(tmp_db) == {one: '2026-08-01'} and sorted(_exports(tmp_db)) == [one]


def test_mark_datev_export_handles_more_ids_than_sqlite_takes_in_one_statement(tmp_db):
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.executemany("INSERT INTO Bookings (DateBooking, Amount, BookingType) VALUES ('2026-07-02', ?, 'entry')", [(n,) for n in range(1, 1202)])
        con.commit()
        ids = [row[0] for row in con.execute('SELECT ID FROM Bookings ORDER BY ID')]
    assert len(ids) == 1201

    tmp_db.mark_datev_export(ids, '2026-08-01')

    assert len(_exports(tmp_db)) == 1201 and set(_tax_dates(tmp_db).values()) == {'2026-08-01'}
    with closing(tmp_db._get_connection()) as con:
        assert tmp_db.exported_booking_ids(con, ids) == set(ids)


def test_a_failing_mark_rolls_the_tax_dates_back_too(tmp_db):
    one, two = _entries(tmp_db, 2)
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute('DROP TABLE BookingExports')                                          # das Markieren scheitert nach dem Steuerdatum
        con.commit()

    with pytest.raises(sqlite3.OperationalError):
        tmp_db.mark_datev_export([one, two], '2026-08-01')

    assert _tax_dates(tmp_db) == {one: None, two: None}                                   # alles oder nichts


def test_the_replay_log_of_the_mark_is_written_after_the_commit(tmp_db, monkeypatch):
    one, = _entries(tmp_db, 1)
    seen = []
    monkeypatch.setattr(Database, '_log_sql', lambda self, sql, params, description: seen.append((sql.split(' WHERE')[0].split('(')[0].strip(), len(_exports(self)))))

    tmp_db.mark_datev_export([one], '2026-08-01')

    assert seen and all(count == 1 for _statement, count in seen)                         # im Protokoll erst, als die Markierung festgeschrieben war
    assert any(statement.startswith('UPDATE Bookings') for statement, _count in seen)


def test_exported_booking_ids_names_the_marked_rows_among_the_asked(tmp_db):
    one, two, three = _entries(tmp_db, 3)
    tmp_db.mark_datev_export([one, three], '2026-08-01')
    with closing(tmp_db._get_connection()) as con:
        assert tmp_db.exported_booking_ids(con, [one, two, three, 99999]) == {one, three}
        assert tmp_db.exported_booking_ids(con, [two]) == set()
        assert tmp_db.exported_booking_ids(con, []) == set()


def test_the_mark_goes_when_the_booking_goes(tmp_db):
    one, two = _entries(tmp_db, 2)
    tmp_db.mark_datev_export([one, two], '2026-08-01')

    tmp_db.delete_transaction(one)

    assert sorted(_exports(tmp_db)) == [two]                                              # CASCADE: nichts bleibt verwaist
