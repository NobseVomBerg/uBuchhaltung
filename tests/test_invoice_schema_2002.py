# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.2.0 (Schema 2002): Fassungen und Protokoll einer Rechnung.

``Invoices`` bekommt am Ende fünf Spalten (``InvoiceKind``, ``CancelsInvoiceId``, ``ReplacesInvoiceId``,
``SentAt``, ``DocumentVersion``), dazu entstehen die Tabellen ``InvoiceCorrections`` und
``InvoiceDocumentVersions`` und der eindeutige Index auf ``CancelsInvoiceId``. Geprüft wird:

* Eine frische und eine migrierte Datenbank haben dieselbe Spaltenreihenfolge (die Rechnungs-Zeilen werden
  positionsbasiert gelesen, ``InvoiceCol``), dieselben Tabellen und Indizes.
* Der Bestand bleibt, wie er war: jede Spalte jeder Zeile, alle anderen Tabellen; die neuen Werte sind NULL,
  die neuen Tabellen leer – nichts wird umgedeutet.
* Vor der Umstellung entsteht eine Sicherung (Stand 2001), scheitert sie, bleibt die Datenbank unberührt; bricht
  die Umstellung ab, rollt sie ganz zurück (auch die angelegten Tabellen), und ein zweiter Start klappt.
* Die neuen Tabellen haben keinen Fremdschlüssel; der Index ``ux_invoices_cancels`` erlaubt jede Rechnung nur
  einmal als Ziel eines Stornobelegs (NULL beliebig oft).
* Das Lösch-Skript der SQL-Konsole („Zeitbereich löschen“) nimmt die neuen Tabellen mit.

Namen und Nummern sind erfundene Testwerte.
"""
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import version
from db import Database, MigrationBackupError, _CoreMixin
from db.invoices import InvoiceCol
from server.handlers import handle_execute_sql
from server.pages_miscellaneous import PageMiscellaneous
from tests import installation
from tests.document_helpers import add_contacts, add_document
from tests.entry_form_world import to_state_2000, to_state_2001

NEW_COLUMNS = ['InvoiceKind', 'CancelsInvoiceId', 'ReplacesInvoiceId', 'SentAt', 'DocumentVersion']
NEW_TABLES = ['InvoiceCorrections', 'InvoiceDocumentVersions']


def _reopen(path):
    """Die Datei so öffnen, als wäre das Programm neu gestartet worden."""
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def _info(path, table):
    with closing(sqlite3.connect(path)) as con:
        return [row[1:] for row in con.execute(f'PRAGMA table_info({table})')]     # ohne die laufende Nummer


def _names(path, table='Invoices'):
    return [row[0] for row in _info(path, table)]


def _tables(path):
    with closing(sqlite3.connect(path)) as con:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _index_sql(path, name):
    with closing(sqlite3.connect(path)) as con:
        row = con.execute("SELECT tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                          (name,)).fetchone()
    return row


def _backups(path):
    directory = Path(path).parent / 'backup'
    return sorted(directory.iterdir()) if directory.is_dir() else []


def _contents(path, skip=()):
    """Alle Zeilen aller Tabellen (ohne die in ``skip``) – ein Stand zum Vergleichen."""
    with closing(sqlite3.connect(path)) as con:
        tables = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        return {t: con.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables if t not in skip}


@pytest.fixture
def stock(tmp_path):
    """Ein Bestand im Stand von 2.1.x: Rechnungen in mehreren Status (eine mit abgelegter Datei), ein
    Angebot, Positionen, eine Zahlung."""
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    own, customer = add_contacts(db)
    pdf = tmp_path / 'invoices' / '2026' / '26R001 Malermeister Pinsel.pdf'
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b'%PDF-1.4 versendet')
    add_document(db, '26R001', 'invoice', own, customer, status='sent', pdf_path=str(pdf))
    add_document(db, '26R002', 'invoice', own, customer, status='draft')
    paid = add_document(db, '26R003', 'invoice', own, customer, status='paid')
    add_document(db, '26R004', 'invoice', own, customer, status='cancelled')
    add_document(db, '26A001', 'quote', own, customer, status='accepted')
    booking = db.insert_booking('2026-07-01', 119.0, booking_type='bank')
    db.link_invoice_to_transaction(paid, booking, 119.0)
    to_state_2001(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


# ── Der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_stock_is_a_2_1_x_database_before_the_migration(stock):
    assert installation.user_version(stock) == 2001
    assert not set(NEW_COLUMNS) & set(_names(stock)) and not set(NEW_TABLES) & _tables(stock)
    assert _index_sql(stock, 'ux_invoices_cancels') is None
    assert len(_names(stock)) == 50


def test_this_program_is_a_2_2_x_program():
    assert version.SCHEMA_VERSION >= 2002 and version.SCHEMA_STEP >= 2


# ── Frisch = migriert ────────────────────────────────────────────────────────

def test_a_fresh_and_a_migrated_database_have_the_same_columns_tables_and_indexes(stock, tmp_path):
    fresh = str(tmp_path / 'fresh.db')
    Database(db_name=fresh)
    _reopen(stock)

    assert _names(stock)[-5:] == NEW_COLUMNS and len(_names(stock)) == 55
    for table in ['Invoices', *NEW_TABLES]:
        assert _info(stock, table) == _info(fresh, table), table                # Name, Typ, NOT NULL, Vorgabe, PK
    assert _tables(stock) == _tables(fresh)
    for index in ('ux_invoices_cancels', 'idx_invoicecorrections_invoice'):
        assert _index_sql(stock, index) == _index_sql(fresh, index) and _index_sql(stock, index) is not None
    with closing(sqlite3.connect(stock)) as a, closing(sqlite3.connect(fresh)) as b:
        query = "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        assert [r[0] for r in a.execute(query)] == [r[0] for r in b.execute(query)]
    assert installation.user_version(stock) == installation.user_version(fresh) == version.SCHEMA_VERSION


def test_the_column_positions_of_the_row_reader_match_the_schema(stock):
    _reopen(stock)
    names = _names(stock)
    assert names[InvoiceCol.DOCUMENT_TYPE] == 'DocumentType'
    assert names[InvoiceCol.INVOICE_KIND] == 'InvoiceKind' and names[InvoiceCol.CANCELS_INVOICE_ID] == 'CancelsInvoiceId'
    assert names[InvoiceCol.REPLACES_INVOICE_ID] == 'ReplacesInvoiceId' and names[InvoiceCol.SENT_AT] == 'SentAt'
    assert names[InvoiceCol.DOCUMENT_VERSION] == 'DocumentVersion'
    assert (InvoiceCol.INVOICE_KIND, InvoiceCol.DOCUMENT_VERSION) == (50, 54)


# ── Der Bestand bleibt ───────────────────────────────────────────────────────

#: Was die Umstellung auf 2.3.0 (Schema 2003) an den Tabellen ändert, die diese Prüfung mitliest: ``Bookings`` und
#: ``CompanyDetails`` bekommen Spalten am Ende (alle NULL), fünf Zeilen von ``TaxKeys`` werden berichtigt. Hier gilt der
#: Stand von 2.2.x; Schema 2003 prüft ``test_datev_schema_2003.py``.
_ADDED_BY_2003 = {'Bookings': 1, 'CompanyDetails': 2}
_CORRECTED_BY_2003 = ('100', '120', '121', '122', '490')


def test_existing_rows_stay_untouched_and_the_new_values_are_empty(stock):
    before_invoices = _contents(stock)['Invoices']
    before_rest = _contents(stock, skip={'Invoices'})
    assert len(before_invoices) == 5

    _reopen(stock)

    after = _contents(stock)
    assert [row[:50] for row in after['Invoices']] == before_invoices           # jede Spalte jeder Zeile
    assert all(row[50:] == (None,) * 5 for row in after['Invoices'])
    for table, rows in before_rest.items():
        if table in _ADDED_BY_2003:                                             # die Spalten von 2.3.0 am Ende, leer
            added = _ADDED_BY_2003[table]
            assert all(row[-added:] == (None,) * added for row in after[table]), table
            after[table] = [row[:-added] for row in after[table]]
        elif table == 'TaxKeys':
            rows = [row for row in rows if row[0] not in _CORRECTED_BY_2003]
            after[table] = [row for row in after[table] if row[0] not in _CORRECTED_BY_2003]
        assert after[table] == rows, table                                      # alle anderen Tabellen
    assert after['InvoiceCorrections'] == [] and after['InvoiceDocumentVersions'] == []


def test_an_old_invoice_reads_as_version_1_without_a_history(stock):
    db = _reopen(stock)
    sent = next(i for i in db.fetch_invoices() if i[1] == '26R001')
    assert sent[InvoiceCol.DOCUMENT_VERSION] is None and sent[InvoiceCol.SENT_AT] is None
    assert db.correction_notice(sent[0]) is None
    assert db.get_invoice_protocol(sent[0]) == [] and db.get_document_versions(sent[0]) == []
    assert db.was_sent_before(sent[0]) is False                                 # nichts wird umgedeutet


# ── Sicherung, Abbruch, Wiederholung ─────────────────────────────────────────

def test_the_migration_makes_a_backup_of_the_old_state_first(stock):
    before = installation.dump(stock)

    _reopen(stock)

    (backup,) = _backups(stock)
    assert backup.name.endswith(f'_vor_{version.APP_VERSION}_buch.db') and backup.name[:8].isdigit()
    assert installation.user_version(str(backup)) == 2001
    assert not set(NEW_COLUMNS) & set(_names(str(backup)))
    assert installation.dump(str(backup)) == before                             # Zeile für Zeile der alte Stand
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_a_failing_backup_stops_the_opening_and_leaves_the_stock_at_2001(stock):
    before = installation.dump(stock)
    (Path(stock).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError):
        _reopen(stock)

    assert installation.user_version(stock) == 2001 and installation.dump(stock) == before
    assert not set(NEW_COLUMNS) & set(_names(stock)) and not set(NEW_TABLES) & _tables(stock)
    (Path(stock).parent / 'backup').unlink()                                    # Ursache behoben: jetzt klappt es
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION


def test_an_aborted_migration_rolls_everything_back_including_the_new_tables(stock, monkeypatch):
    before = installation.dump(stock)

    def boom(self, cursor):
        cursor.execute('CREATE TABLE InvoiceCorrections (halb INTEGER)')         # mitten im Anlegen
        raise RuntimeError('Abbruch mitten in der Umstellung')

    monkeypatch.setattr(Database, '_ensure_invoice_version_schema', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(stock)
    finally:
        monkeypatch.undo()

    assert installation.user_version(stock) == 2001
    assert not set(NEW_COLUMNS) & set(_names(stock)) and not set(NEW_TABLES) & _tables(stock)
    assert installation.dump(stock) == before                                   # Spalten, Tabellen, Zeilen wie vorher

    _reopen(stock)                                                              # und beim nächsten Start klappt es
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[-5:] == NEW_COLUMNS


def test_a_second_start_changes_nothing_and_makes_no_second_backup(stock):
    _reopen(stock)
    after_first, backups = installation.dump(stock), _backups(stock)

    _reopen(stock)
    _reopen(stock)

    assert installation.dump(stock) == after_first and _backups(stock) == backups


def test_a_2_0_x_stock_goes_through_both_steps_in_one_backed_up_run(stock):
    to_state_2000(stock)
    assert installation.user_version(stock) == 2000

    _reopen(stock)

    assert installation.user_version(stock) == version.SCHEMA_VERSION
    assert _names(stock)[-5:] == NEW_COLUMNS and 'EntryForm' in _names(stock, 'Bookings')
    (backup,) = _backups(stock)
    assert installation.user_version(str(backup)) == 2000


def test_a_1_x_stock_gets_the_new_columns_as_well(stock):
    installation.stamp_user_version(stock, 4)
    _reopen(stock)
    assert installation.user_version(stock) == version.SCHEMA_VERSION and _names(stock)[-5:] == NEW_COLUMNS


# ── Index und Fremdschlüssel ─────────────────────────────────────────────────

def test_a_cancelled_invoice_can_be_the_target_of_one_credit_note_only(stock):
    db = _reopen(stock)
    with closing(db._get_connection()) as con:
        ids = [r[0] for r in con.execute('SELECT ID FROM Invoices ORDER BY ID LIMIT 4')]
        con.execute('UPDATE Invoices SET CancelsInvoiceId = ? WHERE ID = ?', (ids[0], ids[1]))
        con.execute('UPDATE Invoices SET CancelsInvoiceId = NULL WHERE ID = ?', (ids[2],))
        con.execute('UPDATE Invoices SET CancelsInvoiceId = NULL WHERE ID = ?', (ids[3],))     # NULL beliebig oft
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            con.execute('UPDATE Invoices SET CancelsInvoiceId = ? WHERE ID = ?', (ids[0], ids[2]))
        con.rollback()


def test_the_new_tables_have_no_foreign_key_and_survive_deleting_an_invoice(stock):
    db = _reopen(stock)
    for table in NEW_TABLES:
        assert _fk_list(stock, table) == []
    with closing(db._get_connection()) as con:
        draft = con.execute("SELECT ID FROM Invoices WHERE InvoiceNumber = '26R002'").fetchone()[0]
        con.execute("INSERT INTO InvoiceCorrections (InvoiceID, InvoiceNumber, Version, Action, Field, OldValue, "
                    "NewValue) VALUES (?, '26R002', 2, 'correct', 'OrderNumber', 'a', 'b')", (draft,))
        con.execute("INSERT INTO InvoiceDocumentVersions (InvoiceID, Version, Kind, RelPath, Reason) "
                    "VALUES (?, 1, 'pdf', '2026/Fassungen/x/y.pdf', 'corrected')", (draft,))
        con.commit()
    db.delete_draft_invoice(draft)
    with closing(sqlite3.connect(stock)) as con:
        assert con.execute('SELECT COUNT(*) FROM InvoiceCorrections WHERE InvoiceID = ?', (draft,)).fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM InvoiceDocumentVersions WHERE InvoiceID = ?',
                           (draft,)).fetchone()[0] == 1


def _fk_list(path, table):
    with closing(sqlite3.connect(path)) as con:
        return con.execute(f'PRAGMA foreign_key_list({table})').fetchall()


def test_one_file_kind_per_version_and_invoice(stock):
    db = _reopen(stock)
    with closing(db._get_connection()) as con:
        con.execute("INSERT INTO InvoiceDocumentVersions (InvoiceID, Version, Kind, RelPath, Reason) "
                    "VALUES (1, 1, 'pdf', 'a.pdf', 'corrected')")
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            con.execute("INSERT INTO InvoiceDocumentVersions (InvoiceID, Version, Kind, RelPath, Reason) "
                        "VALUES (1, 1, 'pdf', 'b.pdf', 'reopened')")
        con.execute("INSERT INTO InvoiceDocumentVersions (InvoiceID, Version, Kind, RelPath, Reason) "
                    "VALUES (1, 1, 'xml', 'a.xml', 'corrected')")                    # die andere Art geht
        con.execute("INSERT INTO InvoiceDocumentVersions (InvoiceID, Version, Kind, RelPath, Reason) "
                    "VALUES (1, 2, 'pdf', 'c.pdf', 'reopened')")                     # eine andere Fassung auch
        con.rollback()


# ── Das Lösch-Skript der SQL-Konsole ─────────────────────────────────────────

def _range_delete_script(db, date_from, date_to):
    """Das Lösch-Skript „Zeitbereich löschen“ der Seite „Verschiedenes“, so zusammengesetzt, wie das
    JavaScript der Seite (``buildRangeDeleteSql``) es tut."""
    html = PageMiscellaneous(db)
    start = html.index('function buildRangeDeleteSql()')
    body = html[start:html.index("document.getElementById('sql_input').value = sql;", start)]
    env = {'from': date_from, 'to': date_to}

    def evaluate(expression):
        parts = []
        for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|([A-Za-z_]\w*)', expression):
            parts.append(match.group(1).replace('\\n', '\n') if match.group(2) is None
                         else env[match.group(2)])
        return ''.join(parts)

    for name in ('b', 'd', 'i'):
        env[name] = evaluate(re.search(rf'const {name} = (.*?);\n', body).group(1))
    return evaluate(re.search(r'const sql =(.*?);\s*$', body.rstrip(), re.S).group(1))


def test_the_delete_script_takes_the_protocol_and_the_versions_of_the_deleted_invoices_along(stock):
    db = _reopen(stock)
    with closing(db._get_connection()) as con:
        con.execute("UPDATE Invoices SET InvoiceDate = '2025-02-01' WHERE InvoiceNumber = '26R001'")
        ids = {r[0]: r[1] for r in con.execute('SELECT InvoiceNumber, ID FROM Invoices')}
        for number, invoice_id in (('26R001', ids['26R001']), ('26R002', ids['26R002'])):
            con.execute("INSERT INTO InvoiceCorrections (InvoiceID, InvoiceNumber, Version, Action, Field, "
                        "OldValue, NewValue) VALUES (?, ?, 2, 'correct', 'OrderNumber', 'a', 'b')",
                        (invoice_id, number))
            con.execute("INSERT INTO InvoiceDocumentVersions (InvoiceID, Version, Kind, RelPath, Reason) "
                        "VALUES (?, 1, 'pdf', '2026/Fassungen/x/y.pdf', 'corrected')", (invoice_id,))
        con.execute('UPDATE Invoices SET CancelsInvoiceId = ? WHERE InvoiceNumber = ?', (ids['26R001'], '26R004'))
        con.execute('UPDATE Invoices SET ReplacesInvoiceId = ? WHERE InvoiceNumber = ?', (ids['26R001'], '26R003'))
        con.commit()
    script = _range_delete_script(db, '2025-01-01', '2025-12-31')              # nur 26R001 liegt im Zeitraum
    assert 'DELETE FROM InvoiceCorrections' in script and 'DELETE FROM InvoiceDocumentVersions' in script

    answer = json.loads(handle_execute_sql(db, {'sql_commands': [script]}))

    assert not answer['errors'] and answer['success_count'] == answer['total']
    with closing(sqlite3.connect(stock)) as con:
        assert [r[0] for r in con.execute('SELECT InvoiceID FROM InvoiceCorrections')] == [ids['26R002']]
        assert [r[0] for r in con.execute('SELECT InvoiceID FROM InvoiceDocumentVersions')] == [ids['26R002']]
        assert con.execute('SELECT COUNT(*) FROM Invoices WHERE InvoiceNumber = ?', ('26R001',)).fetchone()[0] == 0
        pointers = con.execute('SELECT CancelsInvoiceId, ReplacesInvoiceId FROM Invoices '
                               "WHERE InvoiceNumber IN ('26R003', '26R004')").fetchall()
        assert pointers == [(None, None), (None, None)]                          # die Verweise auf Gelöschtes sind leer
