# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Migration auf 2.1.0: Jeder Buchungssatz bekommt seine Form (``Bookings.EntryForm``).

Die Migration darf nichts umdeuten. Der Bestand bekommt die Form, mit der 2.0.x ihn las; danach zeigen
EÜR, DATEV-Zeilen, Dashboard und die Prüfung auf unstimmige Splits **dieselben Werte wie vorher**. Als
Maßstab dienen Werte, die der Stand 2.0.8 über dieselbe Datenbank gelesen hat
(``fixtures/entry_form_golden_2_0_8.json``; Aufbau und Neuerzeugung: ``entry_form_world.py``) – für einen
Bestand mit allen Schreibwegen und Kassen mit 1600, mit dem Verrechnungskonto 1460 und ohne Nummer.

Dazu die Sicherung vor der Umstellung (Kopie über die Backup-API; scheitert sie, bleibt die Datenbank
unverändert auf 2000), die Wiederholbarkeit (ein zweiter Start ändert nichts), der eindeutige Index auf
den Zahlungszuordnungen und das Zurückspielen einer 2.0.x-Sicherung.
"""
import datetime
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import version
from db import Database, MigrationBackupError, SchemaTooNewError, _CoreMixin
from db.matching import (
    FORM_LIQUID_FIRST, FORM_MIRROR_COA, FORM_MIRROR_LEGACY, FORM_PURPOSE_FIRST, FORM_TRANSFER,
    read_entry_form,
)
from tests import entry_form_world as world
from tests import installation

GOLDEN = json.loads((Path(__file__).parent / 'fixtures' / 'entry_form_golden_2_0_8.json')
                    .read_text(encoding='utf-8'))
CASH_IDS = {1600: 'kasse-1600', 1460: 'kasse-1460', None: 'kasse-ohne-skr'}


def _reopen(path):
    """Die Datei so öffnen, als wäre das Programm neu gestartet worden."""
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def _columns(path):
    with closing(sqlite3.connect(path)) as con:
        return [row[1] for row in con.execute('PRAGMA table_info(Bookings)')]


def _indexes(path):
    with closing(sqlite3.connect(path)) as con:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _forms(path):
    """{ID: (Typ, Form, Konto, Gegenkonto)} aller Buchungen."""
    with closing(sqlite3.connect(path)) as con:
        return {row[0]: row[1:] for row in con.execute(
            'SELECT ID, BookingType, EntryForm, COA_ID, CounterCOA_ID FROM Bookings')}


def _backups(path):
    directory = Path(path).parent / 'backup'
    return sorted(directory.iterdir()) if directory.is_dir() else []


@pytest.fixture(params=list(CASH_IDS), ids=list(CASH_IDS.values()))
def old(request, tmp_path):
    """Ein Bestand im Stand von 2.0.x: (Pfad, Kassennummer)."""
    path = str(tmp_path / 'buch.db')
    world.build(path, request.param)
    world.to_state_2000(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path, request.param


@pytest.fixture
def old_1600(tmp_path):
    path = str(tmp_path / 'buch.db')
    world.build(path, 1600)
    world.to_state_2000(path)
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return path


def _roundtrip(value):
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


# ── der Ausgangspunkt ────────────────────────────────────────────────────────

def test_the_world_is_a_2_0_x_database_before_the_migration(old):
    path, _cash = old
    assert installation.user_version(path) == 2000
    assert 'EntryForm' not in _columns(path)
    assert 'ux_invoicepayments_invoice_booking' not in _indexes(path)
    with closing(sqlite3.connect(path)) as con:
        kinds = dict(con.execute('SELECT BookingType, COUNT(*) FROM Bookings GROUP BY 1').fetchall())
    assert kinds['entry'] > 40 and kinds['bank'] > 10


# ── die Auswertungen bleiben, wie sie waren ──────────────────────────────────

def test_migration_leaves_euer_datev_dashboard_and_split_check_as_they_were(old):
    """EÜR (alle Konten, nur Kasse, ein Bankkonto), jede Zeile des DATEV-Stapels (Betrag, Soll/Haben,
    Konten, BU-Schlüssel), Dashboard (Summen und Monate, ebenso gefiltert) und die unstimmigen Splits:
    nach der Migration dieselben Werte, die 2.0.8 über die Datenbank las."""
    path, cash = old

    after = world.evaluate(path)               # öffnet die Datenbank und migriert sie

    assert installation.user_version(path) == version.SCHEMA_VERSION == 2001
    expected = GOLDEN[str(cash)]
    assert set(after) == set(expected)
    for section in expected:
        assert _roundtrip(after[section]) == expected[section], section


def test_the_golden_values_have_teeth_the_world_covers_every_difficult_shape():
    """Die Sollwerte sind nur dann ein Maßstab, wenn sie die kritischen Fälle enthalten (hier die der
    Kasse 1600: die Importspiegel mit dem verkehrten Kennzeichen, beide Konventionen, der Bereich
    1000–1099, Splits, die 2.0.x als unstimmig meldet)."""
    golden = GOLDEN['1600']
    marker = {row[6]: row[2] for row in golden['datev']}
    assert marker['CSV-AB'] == 'H' and marker['CSV-EZ'] == 'H'           # die Importspiegel von 2.0.x
    assert marker['ALT-4'] == 'S'                                         # Spiegel in der anderen Konvention
    assert marker['ALT-1'] == 'S' and marker['ALT-2'] == 'S'              # Konten im Bereich 1000–1099
    assert golden['unbalanced']                                           # Splits, die 2.0.x meldet
    assert golden['euer'] and golden['dashboard_cash']['totals']


# ── die Formen des Bestands ──────────────────────────────────────────────────

def test_every_entry_gets_a_form_and_bank_rows_get_none(old):
    path, _cash = old

    _reopen(path)

    forms = _forms(path)
    assert all((kind == 'entry') == (form is not None) for kind, form, _c, _k in forms.values())
    assert {form for kind, form, _c, _k in forms.values() if kind == 'entry'} <= {
        FORM_PURPOSE_FIRST, FORM_LIQUID_FIRST, FORM_TRANSFER, FORM_MIRROR_LEGACY}


def test_the_form_is_the_classification_of_2_0_x_including_the_old_cash_range(old_1600):
    path = old_1600
    db = _reopen(path)
    with closing(db._get_connection()) as conn:
        legacy_ids = db._get_legacy_payment_coa_ids(conn.cursor())
    assert db.get_payment_coa_ids() < legacy_ids                  # der Bereich gehört nur der Migration

    for _id, (kind, form, coa, counter) in _forms(path).items():
        if kind != 'entry':
            continue
        expected = read_entry_form(None, coa, counter, legacy_ids)
        assert form == expected, (coa, counter)


def test_mirrors_become_legacy_mirrors_and_the_range_rows_keep_their_old_reading(old_1600):
    path = old_1600
    _reopen(path)
    with closing(sqlite3.connect(path)) as con:
        forms = dict(con.execute("SELECT DocumentNumber, EntryForm FROM Bookings WHERE "
                                 "DocumentNumber LIKE 'ALT-%' OR DocumentNumber LIKE 'CSV-%'"))
    for mirror in ('CSV-AB', 'CSV-EZ', 'CSV-VK', 'ALT-3', 'ALT-4', 'ALT-5', 'ALT-6', 'ALT-7', 'ALT-15'):
        assert forms[mirror] == FORM_MIRROR_LEGACY, mirror
    assert forms['ALT-1'] == FORM_PURPOSE_FIRST           # 6815 an 1050: 1050 galt als Geldkonto
    assert forms['ALT-14'] == FORM_PURPOSE_FIRST          # 6850 an 1050, Kind einer Bankbewegung
    assert forms['ALT-2'] == FORM_LIQUID_FIRST            # 1050 an 4400
    assert forms['ALT-9'] == FORM_TRANSFER
    assert forms['ALT-10'] == FORM_PURPOSE_FIRST and forms['ALT-11'] == FORM_LIQUID_FIRST
    assert forms['ALT-12'] == FORM_PURPOSE_FIRST          # nichts kontiert: die EÜR bucht nichts
    assert forms['CSV-KA'] == FORM_PURPOSE_FIRST and forms['CSV-BE'] == FORM_LIQUID_FIRST
    assert forms['CSV-UM'] == FORM_TRANSFER


def test_the_migration_does_not_touch_a_row_that_already_has_a_form(old_1600):
    """Der Schritt füllt nur, was leer ist (so ist er wiederholbar)."""
    path = old_1600
    with closing(sqlite3.connect(path)) as con:
        con.execute('ALTER TABLE Bookings ADD COLUMN EntryForm TEXT')
        con.execute("UPDATE Bookings SET EntryForm = 'transfer' WHERE DocumentNumber = 'ALT-1'")
        con.commit()

    _reopen(path)

    with closing(sqlite3.connect(path)) as con:
        assert con.execute("SELECT EntryForm FROM Bookings WHERE DocumentNumber = 'ALT-1'"
                           ).fetchone() == (FORM_TRANSFER,)
        assert con.execute("SELECT EntryForm FROM Bookings WHERE DocumentNumber = 'ALT-2'"
                           ).fetchone() == (FORM_LIQUID_FIRST,)


# ── Gegenprobe: eine falsch gefüllte Form fällt auf ──────────────────────────

@pytest.mark.parametrize('document, wrong, section', [
    ('CSV-AB', FORM_MIRROR_COA, 'datev'),             # der Importspiegel: Kennzeichen kippt auf S
    ('CSV-KA', FORM_LIQUID_FIRST, 'datev'),           # Ausgabe als „Geldkonto vorne“: H statt S
    ('ALT-1', FORM_TRANSFER, 'datev'),                # Konto im Bereich 1000–1099 als Umbuchung
    ('CSV-VK', FORM_MIRROR_COA, 'unbalanced'),        # Spiegel mit Position: Split geht plötzlich auf
])
def test_a_wrongly_filled_form_does_not_pass_the_comparison(old_1600, document, wrong, section):
    path = old_1600
    _reopen(path)
    assert _roundtrip(world.evaluate(path))[section] == GOLDEN['1600'][section]    # richtig gefüllt: gleich

    with closing(sqlite3.connect(path)) as con:
        con.execute('UPDATE Bookings SET EntryForm = ? WHERE DocumentNumber = ?', (wrong, document))
        con.commit()

    assert _roundtrip(world.evaluate(path))[section] != GOLDEN['1600'][section]


def test_filling_all_forms_from_the_current_account_list_would_not_pass_the_comparison(old_1600):
    """Die naheliegende, falsche Migration: die Form mit den Zahlungskonten von HEUTE (ohne den
    früheren Bereich 1000–1099) bestimmen. Sie deutete Bestand um."""
    path = old_1600
    db = _reopen(path)
    money = db.get_payment_coa_ids()
    with closing(sqlite3.connect(path)) as con:
        for row_id, (kind, _form, coa, counter) in _forms(path).items():
            if kind == 'entry':
                con.execute('UPDATE Bookings SET EntryForm = ? WHERE ID = ?',
                            (read_entry_form(None, coa, counter, money), row_id))
        con.commit()

    after = _roundtrip(world.evaluate(path))

    assert after['datev'] != GOLDEN['1600']['datev']


# ── Sicherung, Wiederholung ──────────────────────────────────────────────────

def test_the_migration_makes_a_backup_of_the_old_state_first(old_1600):
    path = old_1600
    before = installation.dump(path)

    _reopen(path)

    (backup,) = _backups(path)
    assert backup.name.endswith(f'_vor_{version.APP_VERSION}_buch.db') and backup.name[:8].isdigit()
    assert installation.user_version(str(backup)) == 2000
    assert 'EntryForm' not in _columns(str(backup))
    assert installation.dump(str(backup)) == before                # Zeile für Zeile der alte Stand
    assert installation.user_version(path) == 2001                 # die Datenbank selbst: migriert


def test_the_backup_is_never_overwritten(old_1600):
    path = old_1600
    target = Path(path).parent / 'backup'
    target.mkdir()
    taken = target / f"{datetime.date.today():%Y%m%d}_vor_{version.APP_VERSION}_buch.db"
    taken.write_bytes(b'ein anderer Stand')

    _reopen(path)

    assert taken.read_bytes() == b'ein anderer Stand'
    assert len(_backups(path)) == 2


def test_a_second_start_changes_nothing(old_1600):
    path = old_1600
    _reopen(path)
    after_first = installation.dump(path)
    backups = _backups(path)

    _reopen(path)
    _reopen(path)

    assert installation.dump(path) == after_first
    assert _backups(path) == backups                               # und legt keine zweite Sicherung an


def test_the_normal_start_of_a_current_database_makes_no_backup(tmp_path):
    path = str(tmp_path / 'buch.db')
    Database(db_name=path)
    _reopen(path)
    assert _backups(path) == []


def test_a_failing_backup_stops_the_opening_and_leaves_the_database_at_2000(old_1600):
    path = old_1600
    before = installation.dump(path)
    (Path(path).parent / 'backup').write_text('ist eine Datei, kein Ordner')

    with pytest.raises(MigrationBackupError) as error:
        _reopen(path)

    assert 'Sicherung' in str(error.value) and 'nicht geöffnet und nicht verändert' in str(error.value)
    assert installation.user_version(path) == 2000
    assert 'EntryForm' not in _columns(path)
    assert installation.dump(path) == before

    (Path(path).parent / 'backup').unlink()                        # Ursache behoben: jetzt klappt es
    _reopen(path)
    assert installation.user_version(path) == 2001 and len(_backups(path)) == 1


def test_a_backup_that_cannot_be_written_is_not_left_behind(old_1600, monkeypatch):
    """Die Kopie scheitert mitten im Schreiben (Platte voll): kein Rest im Ordner, die Datenbank bleibt."""
    import db.schema as schema
    path = old_1600

    class FailingConnection:
        def __init__(self, real):
            self._real = real

        def backup(self, destination, **kwargs):
            destination.execute('CREATE TABLE halb (x)')          # die Zieldatei existiert schon
            destination.commit()
            raise sqlite3.OperationalError('disk I/O error')

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = sqlite3.connect
    calls = []

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        calls.append(args[0])
        # 1: initialize_database selbst, 2: die Quelle der Sicherung
        return FailingConnection(connection) if len(calls) == 2 else connection

    monkeypatch.setattr(schema.sqlite3, 'connect', connect)
    try:
        with pytest.raises(MigrationBackupError):
            _reopen(path)
    finally:
        monkeypatch.undo()

    assert installation.user_version(path) == 2000
    assert _backups(path) == []                                    # keine halbe Kopie


def test_every_migration_run_of_an_existing_database_is_backed_up_and_named_after_the_program(
        old_1600, monkeypatch):
    """Nicht nur der Schritt auf 2.1.0: Fragt irgendein künftiger Schritt (hier ein erfundener 2.2.0 mit
    der Schema-Zahl 2002) eine bestehende Datenbank um, entsteht vorher eine Sicherung – mit der Version
    des Programms im Namen, das umstellt."""
    path = old_1600
    current = version.APP_VERSION
    _reopen(path)                                                  # Schema 2001: erste Sicherung
    assert [b.name[9:] for b in _backups(path)] == [f'vor_{current}_buch.db']
    monkeypatch.setattr(version, 'SCHEMA_STEP', 2)
    monkeypatch.setattr(version, 'SCHEMA_VERSION', 2002)
    monkeypatch.setattr(version, 'APP_VERSION', '2.2.0')

    _reopen(path)

    assert installation.user_version(path) == 2002
    names = sorted(b.name[9:] for b in _backups(path))
    assert names == [f'vor_{current}_buch.db', 'vor_2.2.0_buch.db']
    newest = next(b for b in _backups(path) if 'vor_2.2.0' in b.name)
    assert installation.user_version(str(newest)) == 2001          # der Stand vor dem Lauf


def test_the_backup_is_published_only_when_it_is_complete(old_1600, monkeypatch):
    """Die Kopie entsteht unter ``<Name>.part`` und bekommt ihren Namen erst mit ``os.replace``."""
    import db.schema as schema
    path = old_1600
    seen = []
    real_replace = os.replace

    def spy(source, target):
        seen.append((source.endswith('.part'), os.path.exists(source), os.path.exists(target),
                     installation.user_version(source)))
        return real_replace(source, target)

    monkeypatch.setattr(schema.os, 'replace', spy)
    _reopen(path)
    monkeypatch.undo()

    assert seen == [(True, True, False, 2000)]                     # vollständig und geprüft, Ziel frei
    assert [b.name for b in _backups(path) if b.name.endswith('.part')] == []
    assert len(_backups(path)) == 1


def test_an_interrupted_backup_leaves_no_file_that_looks_like_a_backup(old_1600, monkeypatch):
    import db.schema as schema
    path = old_1600

    def broken_replace(source, target):
        raise OSError('Platte weg')

    monkeypatch.setattr(schema.os, 'replace', broken_replace)
    try:
        with pytest.raises(MigrationBackupError):
            _reopen(path)
    finally:
        monkeypatch.undo()

    assert _backups(path) == []                                    # weder Ziel noch ``.part``
    assert installation.user_version(path) == 2000


def test_an_aborted_migration_leaves_the_database_exactly_as_it_was(old_1600, monkeypatch):
    """Die Umstellung läuft in einer Transaktion: Bricht sie mitten im Befüllen ab, gibt es keine leere
    Spalte und keine halbe Befüllung – der nächste Start versucht es noch einmal."""
    path = old_1600
    before = installation.dump(path)

    def boom(self, cursor):
        cursor.execute("UPDATE Bookings SET EntryForm = 'transfer' WHERE ID = (SELECT MIN(ID) FROM Bookings)")
        raise RuntimeError('Abbruch mitten im Befüllen')

    monkeypatch.setattr(Database, '_fill_entry_forms', boom)
    try:
        with pytest.raises(RuntimeError, match='Abbruch'):
            _reopen(path)
    finally:
        monkeypatch.undo()

    assert 'EntryForm' not in _columns(path)
    assert installation.user_version(path) == 2000
    assert installation.dump(path) == before

    _reopen(path)                                                  # und beim nächsten Start klappt es
    assert installation.user_version(path) == 2001
    assert all(form is not None for kind, form, _c, _k in _forms(path).values() if kind == 'entry')


def test_the_migration_of_an_old_1x_database_is_one_transaction_too(old_1600, monkeypatch):
    path = old_1600
    installation.stamp_user_version(path, 4)
    before = installation.dump(path)

    def boom(self, cursor):
        raise RuntimeError('Abbruch')

    monkeypatch.setattr(Database, '_fill_entry_forms', boom)
    try:
        with pytest.raises(RuntimeError):
            _reopen(path)
    finally:
        monkeypatch.undo()

    assert installation.user_version(path) == 4 and 'EntryForm' not in _columns(path)
    assert installation.dump(path) == before


def test_filling_the_forms_is_fast_for_a_large_stock(tmp_path):
    """50 000 Sätze mit 3 000 verschiedenen Kontopaaren: eingeordnet wird je Paar einmal, geschrieben
    gebündelt. Die Grenze ist großzügig (gemessen: deutlich weniger als eine Sekunde)."""
    import time
    path = str(tmp_path / 'buch.db')
    db = Database(db_name=path)
    with closing(db._get_connection()) as conn:
        coa_ids = [row[0] for row in conn.execute(
            'SELECT ID FROM ChartOfAccounts ORDER BY AccountNumber LIMIT 55')]
        pairs = [(a, b) for a in coa_ids for b in coa_ids]                  # 3 025 Paare
        conn.executemany(
            "INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, BookingType) "
            "VALUES ('2026-01-02', ?, ?, 10000, 'entry')",
            [pairs[index % len(pairs)] for index in range(50_000)])
        conn.execute("INSERT INTO Bookings (DateBooking, Amount, BookingType) "
                     "VALUES ('2026-01-02', 10000, 'bank')")
        conn.commit()
    legacy_ids = None

    with closing(db._get_connection()) as conn:
        cursor = conn.cursor()
        legacy_ids = db._get_legacy_payment_coa_ids(cursor)
        started = time.perf_counter()
        db._fill_entry_forms(cursor)
        conn.commit()
        elapsed = time.perf_counter() - started
        rows = cursor.execute("SELECT COA_ID, CounterCOA_ID, EntryForm, BookingType FROM Bookings").fetchall()

    assert elapsed < 5, f'{elapsed:.2f} s'
    assert len(rows) == 50_001
    assert all((form is None) == (kind == 'bank') for _a, _b, form, kind in rows)
    for coa_id, counter_id, form, kind in rows[:3025]:
        assert form == read_entry_form(None, coa_id, counter_id, legacy_ids)


def test_a_database_that_is_too_new_is_still_refused_and_not_backed_up(old_1600):
    path = old_1600
    installation.stamp_user_version(path, version.SCHEMA_VERSION + 1)

    with pytest.raises(SchemaTooNewError):
        Database(db_name=path)

    assert _backups(path) == []


def test_the_migration_stamps_the_database_so_that_2_0_x_refuses_it(old_1600):
    """2.0.x liest die Zahl in der Datenbank und öffnet nichts, was höher ist als 2000 – die
    migrierte Datenbank trägt 2001 und wird nie zurückgestempelt."""
    path = old_1600
    _reopen(path)
    assert installation.user_version(path) == 2001 > 2000
    _reopen(path)
    assert installation.user_version(path) == 2001


# ── der Server: Fehlerseite statt Absturz ────────────────────────────────────

def test_the_server_shows_a_page_when_the_backup_before_the_migration_fails(auto_login, server_port):
    from tests.http_client import get
    path = auto_login.db.db_name
    world.to_state_2000(path)
    before = installation.dump(path)
    blocker = Path(path).parent / 'backup'
    blocker.write_text('ist eine Datei, kein Ordner')

    response = get(server_port, '/about')

    assert response.status == 500
    assert 'Sicherung vor der Umstellung gescheitert' in response.text
    assert 'nicht geöffnet und nicht verändert' in response.text
    assert path not in response.text and 'Traceback' not in response.text
    assert installation.user_version(path) == 2000 and installation.dump(path) == before

    blocker.unlink()                                               # Ursache behoben: die Seite kommt
    assert get(server_port, '/about').status == 200
    assert installation.user_version(path) == 2001
    assert len(_backups(path)) == 1


# ── eine 1.x-Datenbank ───────────────────────────────────────────────────────

def test_a_1x_database_is_backed_up_and_migrated_through_all_steps(old_1600):
    path = old_1600
    installation.stamp_user_version(path, 4)                       # Stand 1.x

    _reopen(path)

    assert installation.user_version(path) == 2001
    assert len(_backups(path)) == 1 and installation.user_version(str(_backups(path)[0])) == 4
    assert all(form is not None for kind, form, _c, _k in _forms(path).values() if kind == 'entry')


# ── eine 2.0.x-Sicherung zurückspielen ───────────────────────────────────────

def test_a_2_0_x_backup_can_be_restored_and_is_migrated_on_the_next_start(old_1600, tmp_path):
    from server import backup as backup_module
    user_dir = tmp_path / 'restore_user'
    user_dir.mkdir()
    db_path = user_dir / 'buch.db'
    db_path.write_bytes(Path(old_1600).read_bytes())
    archive, _size = backup_module.create_backup(str(user_dir), str(db_path), scope='db')
    db_path.unlink()
    db_path.write_bytes(b'')                                       # die laufende Datenbank ist hin

    backup_module.restore_backup(str(user_dir), os.path.basename(archive))

    assert installation.user_version(str(db_path)) == 2000
    _reopen(str(db_path))
    assert installation.user_version(str(db_path)) == 2001
    assert any(path.name.endswith(f'_vor_{version.APP_VERSION}_buch.db')
               for path in _backups(str(db_path)))


# ── der eindeutige Index auf den Zahlungszuordnungen ──────────────────────────

INDEX = 'ux_invoicepayments_invoice_booking'


def test_a_new_database_has_the_unique_index(tmp_path):
    path = str(tmp_path / 'buch.db')
    Database(db_name=path)
    assert INDEX in _indexes(path)


def test_an_existing_database_without_duplicates_gets_the_index(old_1600):
    path = old_1600
    assert INDEX not in _indexes(path)
    _reopen(path)
    assert INDEX in _indexes(path)


def _duplicate_payments(path):
    with closing(sqlite3.connect(path)) as con:
        invoice, booking = con.execute(
            'SELECT InvoiceID, BookingID FROM InvoicePayments ORDER BY ID').fetchone()
        con.execute('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) '
                    "VALUES (?, ?, 1000, '2026-02-01')", (invoice, booking))
        con.commit()
    return invoice, booking


def test_an_existing_database_with_duplicate_payments_starts_without_the_index(old_1600, capsys):
    """Kein Fehler, kein Abbruch: Die Prüfliste „Zahlungszuordnungen“ meldet Dubletten schon."""
    path = old_1600
    invoice, _booking = _duplicate_payments(path)

    db = _reopen(path)

    assert INDEX not in _indexes(path)
    assert installation.user_version(path) == 2001
    assert 'doppelt zugeordnet' in capsys.readouterr().out
    assert [f for f in db.find_payment_inconsistencies(invoice) if f['category'] == 'duplicate_payment']


def test_the_index_appears_at_the_first_start_after_the_duplicates_are_gone(old_1600):
    path = old_1600
    invoice, booking = _duplicate_payments(path)
    _reopen(path)
    assert INDEX not in _indexes(path)

    with closing(sqlite3.connect(path)) as con:
        con.execute('DELETE FROM InvoicePayments WHERE ID = (SELECT MAX(ID) FROM InvoicePayments '
                    'WHERE InvoiceID = ? AND BookingID = ?)', (invoice, booking))
        con.commit()
    _reopen(path)

    assert INDEX in _indexes(path)


class _Blind:
    """Verbindung, die dem Aufrufer die Prüfung auf ein vorhandenes Paar verschweigt – so, wie sie bei
    zwei gleichzeitigen Anfragen sein kann; erst der Index fängt das zweite Einfügen."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args):
        if sql.strip().startswith('SELECT') and 'FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?' in sql:
            return self._real.execute('SELECT 1 WHERE 0')
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _paid_pair(tmp_db):
    from tests.entry_form_helpers import bank_account
    from server.handlers import link_booking_to_invoice_capped
    account = bank_account(tmp_db, 'Index-Bank', 1800)
    invoice = tmp_db.insert_invoice({
        'invoice_number': 'IX-1', 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19,
        'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0, 'amount_due': 119.0,
        'status': 'sent'})
    tmp_db.ensure_receivable_booking(invoice)
    bank = tmp_db.insert_booking('2026-02-01', 119, account_id=account, booking_type='bank')
    assert link_booking_to_invoice_capped(tmp_db, invoice, bank, amount=50)[0]
    return invoice, bank


def test_assigning_the_same_pair_twice_is_refused_as_already_linked(tmp_db):
    invoice, bank = _paid_pair(tmp_db)
    assignment = tmp_db.assign_invoice_payment(invoice, bank, 10)
    assert not assignment.ok and 'bereits mit dieser Rechnung verknüpft' in assignment.message


def test_the_index_catches_a_second_insert_that_slipped_past_the_check(tmp_db, monkeypatch):
    from db import core
    invoice, bank = _paid_pair(tmp_db)
    count = len(tmp_db.get_invoice_payments(invoice))
    real_init = core.Transaction.__init__

    def blind_init(self, db):
        real_init(self, db)
        self.conn = _Blind(self.conn)

    monkeypatch.setattr(core.Transaction, '__init__', blind_init)
    monkeypatch.setattr(tmp_db, 'get_booking_allocations', lambda booking_id: [])

    assignment = tmp_db.assign_invoice_payment(invoice, bank, 10)

    assert not assignment.ok and 'bereits mit dieser Rechnung verknüpft' in assignment.message
    assert len(tmp_db.get_invoice_payments(invoice)) == count          # nichts ist hinzugekommen


def test_the_wiso_link_also_reports_a_pair_that_is_linked_already(tmp_db, monkeypatch):
    invoice, bank = _paid_pair(tmp_db)
    with pytest.raises(ValueError, match='already linked'):
        tmp_db.link_invoice_to_transaction(invoice, bank, 10)

    real_connection = tmp_db._get_connection
    monkeypatch.setattr(tmp_db, '_get_connection', lambda: _Blind(real_connection()))
    with pytest.raises(ValueError, match='already linked'):         # der Index, nicht die Prüfung
        tmp_db.link_invoice_to_transaction(invoice, bank, 10)
    monkeypatch.undo()
    assert len(tmp_db.get_invoice_payments(invoice)) == 1
