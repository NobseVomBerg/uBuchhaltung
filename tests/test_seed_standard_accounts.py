# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Fehlende Standardkonten: Der Start legt nichts nach, ergänzt wird nur auf ausdrücklichen Wunsch (2.3.8).

Ein Standardkonto lässt sich nach Entfernen des Standard-Hakens löschen – legte der Start es nach, käme es bei jedem Öffnen
der Datenbank wieder. Deshalb gilt:

* ``_seed_chart_of_accounts`` füllt NUR eine leere Tabelle (beide Seed-Dateien, danach die ``overrides`` der privaten
  Datei). Der Kontenrahmen einer bestehenden Datenbank bleibt beim Öffnen, wie er ist – auch 1600 und 1610 (2.0.5,
  ``tests/test_cash_account_1600.py``).
* ``Database.missing_standard_accounts`` nennt, was dem Bestand gegenüber den Seed-Dateien fehlt: nur Standardkonten, weder
  die ID noch das Paar (Rahmen, Nummer) vergeben, aufsteigend nach Nummer – rein lesend.
* ``Database.add_missing_standard_accounts`` legt genau diese an (EINE Transaktion, ``INSERT OR IGNORE``, kein UPDATE, keine
  ``overrides``) und liefert ihre Zahl; aufgerufen nur von ``POST /masterdata/skr/add_standard``.
* Die Seite des Kontenrahmens zeigt einen Hinweis mit dem Knopf „Standardkonten ergänzen“ (mit Rückfrage), solange etwas
  fehlt; danach meldet ``?added=<n>`` die Zahl mit festem Text – aus der Adresse kommt nur die Zahl, nie Text.

Die Tests arbeiten auf einer Kopie der Seed-Dateien (``seed_tree``): Eine private Datei auf dem Rechner des
Entwicklers (``seed_data/private/``) bleibt unberücksichtigt. Alle Konten, Namen und Nummern sind erfundene Testwerte.
"""
import contextlib
import html
import json
import os
import re
import shutil
import sqlite3
import threading
import urllib.parse
from types import SimpleNamespace

import pytest

import db.seed as seed_module
from db import Database, _CoreMixin, coa_id
from server.pages_masterdata import ADDED_MAX, MISSING_NAMES_SHOWN, PageSkr, _added_flash
from tests import installation
from tests.dom_helpers import Dom
from tests.http_client import FOREIGN_ORIGIN, get, own_origin, post

SEED_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'seed_data')
COLUMNS = 'ID, Framework, AccountNumber, Name, Description, IsStandard, PrivateSharePercent, ShowInMenu'
HOSTILE = '<script>alert(1)</script> "x" & \'y\''
LEAD_ONE = 'Im Kontenrahmen dieses Bestands fehlt 1 Standardkonto der aktuellen Version: '
LEAD_MANY = 'Im Kontenrahmen dieses Bestands fehlen {n} Standardkonten der aktuellen Version: '
CONFIRM_ONE = ("appConfirmPost('/masterdata/skr/add_standard', "
               "'Das fehlende Standardkonto anlegen? Vorhandene Konten bleiben unverändert.')")
CONFIRM_MANY = ("appConfirmPost('/masterdata/skr/add_standard', "
                "'Die {n} fehlenden Standardkonten anlegen? Vorhandene Konten bleiben unverändert.')")

PRIVATE = {
    'framework': 4,
    'accounts': [
        {'account_number': 9990, 'name': 'Eigenes Standardkonto', 'description': 'Eigene Gruppe',
         'is_standard': True, 'private_share_percent': 10},
        {'account_number': 9991, 'name': 'Eigenes Konto', 'description': 'Eigene Gruppe', 'is_standard': False},
    ],
    'overrides': [{'account_number': 4400, 'private_share_percent': 25}],
}


@pytest.fixture(autouse=True)
def seed_tree(tmp_path, monkeypatch):
    """Die App sucht ihre Seed-Dateien relativ zu ``db/seed.py``: Der Test lenkt sie auf eine Kopie ohne ``private/``
    und ``test/`` um."""
    root = tmp_path / 'project'
    shutil.copytree(SEED_DATA, root / 'seed_data', ignore=shutil.ignore_patterns('private', 'test'))
    (root / 'db').mkdir()
    monkeypatch.setattr(seed_module, '__file__', str(root / 'db' / 'seed.py'))
    return root


@pytest.fixture
def seed_file(seed_tree):
    with open(seed_tree / 'seed_data' / 'chart_of_accounts_skr04.json', encoding='utf-8') as handle:
        return json.load(handle)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / 'buch.db')


# ── Hilfen ───────────────────────────────────────────────────────────────────
def reopen(path):
    """Die Datei gilt für diesen Prozess als noch nicht eingerichtet – wie beim nächsten Start der App."""
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def execute(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute(sql, args)
        con.commit()


def rows(db):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(f'SELECT {COLUMNS} FROM ChartOfAccounts ORDER BY ID').fetchall()


def row(db, number, framework=4):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(f'SELECT {COLUMNS} FROM ChartOfAccounts WHERE Framework = ? AND AccountNumber = ?',
                           (framework, number)).fetchone()


def seeded(seed_file, number):
    """Die Zeile, die die Seed-Datei für dieses Konto vorgibt (``ShowInMenu`` ist beim Anlegen 1)."""
    entry = next(a for a in seed_file['accounts'] if a['account_number'] == number)
    return (coa_id(seed_file['framework'], number), seed_file['framework'], number, entry['name'], entry['description'],
            1 if entry['is_standard'] else 0, entry.get('private_share_percent', 0), 1)


def standard_entries(seed_file):
    """``[(Nummer, Name)]`` aller Standardkonten der Seed-Datei, aufsteigend nach Nummer."""
    return sorted((a['account_number'], a['name']) for a in seed_file['accounts'] if a['is_standard'])


def delete_standard(db, *numbers):
    """Konten des Rahmens 4 roh entfernen (so sieht ein Bestand aus, dem Standardkonten fehlen)."""
    marks = ', '.join('?' * len(numbers))
    execute(db, f'DELETE FROM ChartOfAccounts WHERE Framework = 4 AND AccountNumber IN ({marks})', *numbers)


def occupy_number(db):
    """Ein Konto des Nutzers mit der Nummer 4400, aber anderer ID."""
    execute(db, 'INSERT INTO ChartOfAccounts (ID, Framework, AccountNumber, Name, Description, IsStandard) '
                'VALUES (999001, 4, 4400, ?, ?, 0)', 'Fremd', 'Fremde Gruppe')


def occupy_id(db):
    """Ein Konto des Nutzers mit der ID, die das Standardkonto 4400 bekäme, aber anderer Nummer und anderem Rahmen."""
    execute(db, 'INSERT INTO ChartOfAccounts (ID, Framework, AccountNumber, Name, Description, IsStandard) '
                'VALUES (404400, 9, 1, ?, ?, 0)', 'Fremd', 'Fremde Gruppe')


def write_private(seed_tree, data=PRIVATE):
    private = seed_tree / 'seed_data' / 'private'
    private.mkdir(exist_ok=True)
    (private / 'chart_of_accounts_custom.json').write_text(json.dumps(data), encoding='utf-8')


def not_locked(db):
    """Nach dem Aufruf hält keine Verbindung mehr die Schreibsperre: Ein anderer Schreiber kommt sofort durch."""
    con = sqlite3.connect(db.db_name, timeout=0.5)
    try:
        con.execute('BEGIN IMMEDIATE')
        con.rollback()
    finally:
        con.close()


class WatchedConnection(sqlite3.Connection):
    """Eine Verbindung, die merkt, ob sie geschlossen wurde – und ob dabei noch eine Transaktion offen war (dann hat
    niemand zurückgerollt)."""
    closed = False
    open_transaction_at_close = None

    def close(self):
        self.closed = True
        self.open_transaction_at_close = self.in_transaction
        super().close()


class FailingInsert(WatchedConnection):
    """Eine Verbindung, deren ``executemany`` die erste Zeile schreibt und dann scheitert (wie ein voller Datenträger)."""

    def executemany(self, sql, parameters):
        super().executemany(sql, list(parameters)[:1])
        raise sqlite3.OperationalError('simulierter Fehler beim Schreiben')


def watch(db, monkeypatch, factory=WatchedConnection):
    """Alle Verbindungen, die ``db`` von jetzt an öffnet (mit Fremdschlüsseln wie ``_get_connection``)."""
    opened = []

    def connect():
        conn = sqlite3.connect(db.db_name, factory=factory)
        conn.execute('PRAGMA foreign_keys = ON')
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, '_get_connection', connect)
    return opened


def paragraph(entries):
    """Der Satz des Hinweiskastens für diese fehlenden Konten (``[(Nummer, Name)]``, aufsteigend): höchstens 20 Namen,
    der Rest als Zahl – hier bewusst ausgeschrieben statt aus der Seite übernommen."""
    count = len(entries)
    lead = LEAD_ONE if count == 1 else LEAD_MANY.format(n=count)
    names = ', '.join(f'{number} {html.escape(name)}' for number, name in entries[:20])
    if count > 20:
        names += f' und {count - 20} weitere'
    return f'{lead}{names}.'


def box_text(page):
    """Der Satz des Hinweiskastens – ``None``, wenn die Seite keinen zeigt."""
    found = re.findall(r'<p>(Im Kontenrahmen dieses Bestands fehl[^<]*)</p>', page)
    assert len(found) <= 1, found
    return found[0] if found else None


def the_button(page):
    """Attribute des einen Knopfes „Standardkonten ergänzen“."""
    found = [a for a in Dom(page).all('button') if 'add_standard' in (a.get('onclick') or '')]
    assert len(found) == 1, found
    return found[0]


def flash(page):
    """Meldung und Art aus dem ``appMsg(…)``-Aufruf der Seite – ``None``, wenn es keinen gibt (die Funktionsdefinition
    im Fuß zählt nicht). Die Meldung steht als JSON im Skriptblock, die Art in Anführungszeichen dahinter."""
    for script in Dom(page).scripts:
        found = re.search(r'function\(\)\{appMsg\(', script)
        if found:
            message, end = json.JSONDecoder().raw_decode(script, found.end())
            kind = re.match(r""",['"](\w+)['"]\);""", script[end:])
            assert kind, script[end:end + 40]
            return [message, kind.group(1)]
    return None


def outside_scripts(page):
    return re.sub(r'<script\b.*?</script>', '', page, flags=re.S)


# ── eine frische Datenbank ───────────────────────────────────────────────────
def test_a_fresh_database_gets_exactly_the_accounts_of_the_seed_file(path, seed_file):
    db = Database(db_name=path)
    expected = [seeded(seed_file, a['account_number']) for a in seed_file['accounts']]
    assert sorted(rows(db)) == sorted(expected)
    assert len(rows(db)) == len(seed_file['accounts']) > 0


def test_the_compared_columns_are_all_columns_of_the_table(path):
    with contextlib.closing(sqlite3.connect(Database(db_name=path).db_name)) as con:
        columns = [r[1] for r in con.execute('PRAGMA table_info(ChartOfAccounts)')]
    assert columns == [c.strip() for c in COLUMNS.split(',')]


def test_reopening_an_intact_database_changes_nothing(path):
    db = Database(db_name=path)
    before = rows(db)
    assert rows(reopen(path)) == before
    assert rows(reopen(path)) == before


# ── der Start legt nie Konten nach ───────────────────────────────────────────
def test_a_standard_account_the_user_deleted_does_not_come_back_at_the_start(path):
    """Der Weg des Nutzers: Standard-Haken entfernen, Konto löschen – der nächste Start legt es nicht wieder an."""
    db = Database(db_name=path)
    complete = rows(db)
    db.update_chart_of_accounts(404400, 4, 4400, 'Erlöse 19% USt', 'Betriebliche Erträge', is_standard=0)
    assert db.delete_chart_of_accounts(404400) is True
    after_deleting = rows(db)
    assert row(db, 4400) is None and len(after_deleting) == len(complete) - 1
    for _ in range(3):
        assert rows(reopen(path)) == after_deleting


def test_a_standard_account_removed_by_hand_does_not_come_back_at_the_start(path):
    db = Database(db_name=path)
    delete_standard(db, 4400, 6805)
    gone = rows(db)
    assert rows(reopen(path)) == gone
    assert row(reopen(path), 4400) is None and row(db, 6805) is None


def test_a_database_without_any_standard_account_keeps_what_it_has_at_the_start(path):
    db = Database(db_name=path)
    assert db.insert_chart_of_accounts(4, 9999, 'Eigenes Konto', 'Eigene Gruppe')
    execute(db, 'DELETE FROM ChartOfAccounts WHERE IsStandard = 1')
    only_mine = [(409999, 4, 9999, 'Eigenes Konto', 'Eigene Gruppe', 0, 0, 1)]
    assert rows(db) == only_mine
    assert rows(reopen(path)) == only_mine


def test_the_cash_accounts_are_no_special_case_and_ensure_cash_coa_still_creates_1600(path, seed_file):
    db = Database(db_name=path)
    assert row(db, 1600) == seeded(seed_file, 1600) and row(db, 1610) == seeded(seed_file, 1610)   # frisch: beide
    execute(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (1600, 1610)')
    db = reopen(path)
    assert row(db, 1600) is None and row(db, 1610) is None                  # die ältere Datenbank bleibt, wie sie ist
    assert row(db, 4400) == seeded(seed_file, 4400)                          # alles andere ist unberührt da
    assert db.ensure_cash_coa() == coa_id(4, 1600)                          # 1600 legt die App an, wo eine Kasse es braucht
    assert row(db, 1600)[3:6] == ('Kasse', 'Umlaufvermögenskonten', 1)
    assert row(db, 1610) is None
    assert db.missing_standard_accounts() == [(1610, seeded(seed_file, 1610)[3])]    # 1610 fehlt wie jedes andere Konto


def test_the_private_file_is_read_by_the_first_start_only(path, seed_tree):
    write_private(seed_tree)
    db = Database(db_name=path)                                 # frisch: beide Dateien, danach die Overrides
    assert row(db, 9990) == (409990, 4, 9990, 'Eigenes Standardkonto', 'Eigene Gruppe', 1, 10, 1)
    assert row(db, 9991) == (409991, 4, 9991, 'Eigenes Konto', 'Eigene Gruppe', 0, 0, 1)
    assert row(db, 4400)[6] == 25

    execute(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (9990, 9991)')   # der Nutzer räumt auf
    execute(db, 'UPDATE ChartOfAccounts SET PrivateSharePercent = 0 WHERE AccountNumber = 4400')
    after_cleaning = rows(db)
    assert rows(reopen(path)) == after_cleaning                 # nichts kommt wieder, kein Override greift noch einmal
    assert row(db, 9990) is None and row(db, 9991) is None and row(db, 4400)[6] == 0


def test_without_a_private_file_nothing_private_appears(path):
    db = Database(db_name=path)
    assert row(db, 9990) is None and row(db, 9991) is None
    assert row(reopen(path), 9990) is None
    assert db.missing_standard_accounts() == []


@pytest.mark.parametrize('failing', [False, True],
                         ids=['Frühabbruch-bei-vollem-Kontenrahmen', 'Fehler-beim-Lesen-der-Dateien'])
def test_the_start_closes_its_connection_on_every_path(path, monkeypatch, failing):
    db = Database(db_name=path)
    if failing:
        execute(db, 'DELETE FROM ChartOfAccounts')

        def unreadable():
            raise OSError('simulierter Fehler beim Lesen')

        monkeypatch.setattr(db, '_chart_seed', unreadable)
    before = rows(db)
    opened = watch(db, monkeypatch)
    if failing:
        with pytest.raises(OSError):
            db._seed_chart_of_accounts()
    else:
        db._seed_chart_of_accounts()
    assert len(opened) == 1 and opened[0].closed
    assert rows(db) == before
    not_locked(db)


# ── Database.missing_standard_accounts ───────────────────────────────────────
def test_a_complete_chart_misses_nothing(path):
    assert Database(db_name=path).missing_standard_accounts() == []


def test_deleted_standard_accounts_are_named_with_the_name_of_the_seed_file_in_ascending_order(path):
    db = Database(db_name=path)
    delete_standard(db, 6805, 4400)                             # in der Reihenfolge verkehrt
    assert db.missing_standard_accounts() == [(4400, 'Erlöse 19% USt'), (6805, 'Telefon')]


def test_every_standard_account_is_listed_the_cash_accounts_like_any_other(path, seed_file):
    db = Database(db_name=path)
    execute(db, 'DELETE FROM ChartOfAccounts')
    missing = db.missing_standard_accounts()
    assert missing == standard_entries(seed_file)
    assert (1600, 'Kasse') in missing and (1610, 'Nebenkasse 1') in missing
    assert [number for number, _ in missing] == sorted(number for number, _ in missing)


def test_an_occupied_number_is_not_missing(path):
    db = Database(db_name=path)
    delete_standard(db, 4400)
    occupy_number(db)
    assert db.missing_standard_accounts() == []


def test_an_occupied_id_is_not_missing(path):
    db = Database(db_name=path)
    delete_standard(db, 4400)
    occupy_id(db)
    assert db.missing_standard_accounts() == []


def test_what_the_user_changed_is_not_missing(path):
    db = Database(db_name=path)
    execute(db, "UPDATE ChartOfAccounts SET Name = 'Mein Name', Description = 'Meine Gruppe', PrivateSharePercent = 40, "
                'ShowInMenu = 0, IsStandard = 0 WHERE Framework = 4 AND AccountNumber = 4400')
    assert db.missing_standard_accounts() == []


def test_the_private_file_counts_by_its_standard_flag(path, seed_tree):
    db = Database(db_name=path)                                 # ohne private Datei angelegt
    write_private(seed_tree)
    assert db.missing_standard_accounts() == [(9990, 'Eigenes Standardkonto')]      # 9991 hat keinen Standard-Haken


def test_listing_is_read_only_and_holds_no_lock(path):
    db = Database(db_name=path)
    delete_standard(db, 4400, 6805)
    before = installation.dump(path)
    assert len(db.missing_standard_accounts()) == 2
    assert installation.dump(path) == before
    not_locked(db)


def test_listing_closes_its_connection_even_when_it_fails(path, monkeypatch):
    db = Database(db_name=path)

    def boom(conn, rows):
        raise RuntimeError('simulierter Fehler')

    monkeypatch.setattr(Database, '_free_standard_rows', staticmethod(boom))
    opened = watch(db, monkeypatch)
    with pytest.raises(RuntimeError):
        db.missing_standard_accounts()
    assert len(opened) == 1 and opened[0].closed
    not_locked(db)


# ── Database.add_missing_standard_accounts ───────────────────────────────────
def test_it_creates_exactly_the_missing_accounts_with_the_values_of_the_seed_file(path, seed_file):
    db = Database(db_name=path)
    complete = rows(db)
    delete_standard(db, 4400, 6805, 1600)
    assert db.add_missing_standard_accounts() == 3
    assert rows(db) == complete
    for number in (4400, 6805, 1600):
        assert row(db, number) == seeded(seed_file, number)
    assert db.missing_standard_accounts() == []
    after = installation.dump(path)
    assert db.add_missing_standard_accounts() == 0              # ein zweiter Aufruf legt nichts an und ändert nichts
    assert installation.dump(path) == after


def test_a_complete_chart_gets_nothing_and_stays_byte_for_byte_the_same(path):
    db = Database(db_name=path)
    before = installation.dump(path)
    assert db.add_missing_standard_accounts() == 0
    assert installation.dump(path) == before


def test_it_changes_no_existing_account_in_any_column(path, seed_file):
    db = Database(db_name=path)
    execute(db, "UPDATE ChartOfAccounts SET Name = 'Mein Name', Description = 'Meine Gruppe', PrivateSharePercent = 40, "
                'ShowInMenu = 0, IsStandard = 0 WHERE Framework = 4 AND AccountNumber = 4400')
    execute(db, 'UPDATE ChartOfAccounts SET PrivateSharePercent = 50, ShowInMenu = 0 '
                'WHERE Framework = 4 AND AccountNumber = 6805')
    assert db.insert_chart_of_accounts(4, 9999, 'Eigenes Konto', 'Eigene Gruppe', private_share_percent=20)
    delete_standard(db, 3806, 4185, 1810)
    before = rows(db)
    assert db.add_missing_standard_accounts() == 3
    after = rows(db)
    assert set(before) <= set(after) and len(after) == len(before) + 3         # kein vorhandenes Konto verändert
    assert set(after) - set(before) == {seeded(seed_file, number) for number in (3806, 4185, 1810)}
    assert row(db, 4400) == (404400, 4, 4400, 'Mein Name', 'Meine Gruppe', 0, 40, 0)


def test_an_occupied_number_is_skipped_without_an_error(path):
    db = Database(db_name=path)
    delete_standard(db, 4400, 6805)
    occupy_number(db)
    before = rows(db)
    assert db.add_missing_standard_accounts() == 1              # nur 6805: Für 4400 ist die Nummer belegt
    assert set(before) <= set(rows(db)) and len(rows(db)) == len(before) + 1
    assert row(db, 4400) == (999001, 4, 4400, 'Fremd', 'Fremde Gruppe', 0, 0, 1)
    assert row(db, 6805)[3] == 'Telefon'


def test_an_occupied_id_is_skipped_without_an_error(path):
    db = Database(db_name=path)
    delete_standard(db, 4400, 6805)
    occupy_id(db)
    before = rows(db)
    assert db.add_missing_standard_accounts() == 1              # nur 6805: Die ID von 4400 ist belegt
    assert set(before) <= set(rows(db)) and len(rows(db)) == len(before) + 1
    assert row(db, 4400) is None
    assert row(db, 1, framework=9)[:5] == (404400, 9, 1, 'Fremd', 'Fremde Gruppe')
    assert row(db, 6805)[3] == 'Telefon'


def test_no_overrides_and_no_private_accounts_without_the_standard_flag(path, seed_tree):
    db = Database(db_name=path)                                 # ohne private Datei angelegt
    write_private(seed_tree, {**PRIVATE, 'overrides': [{'account_number': 4400, 'private_share_percent': 25},
                                                       {'account_number': 6805, 'private_share_percent': 30}]})
    execute(db, 'UPDATE ChartOfAccounts SET PrivateSharePercent = 7 WHERE Framework = 4 AND AccountNumber = 6805')
    delete_standard(db, 4400)
    assert db.add_missing_standard_accounts() == 2              # 4400 und das Standardkonto 9990 der privaten Datei
    assert row(db, 4400)[6] == 0                                # der Wert der Standarddatei – der Override (25) greift nicht
    assert row(db, 6805)[6] == 7                                # und ein vorhandenes Konto bekommt keinen (30)
    assert row(db, 9990) == (409990, 4, 9990, 'Eigenes Standardkonto', 'Eigene Gruppe', 1, 10, 1)
    assert row(db, 9991) is None                                # ohne Standard-Haken nie
    assert db.missing_standard_accounts() == []


def test_the_state_is_read_inside_the_transaction_when_adding(path, monkeypatch):
    db = Database(db_name=path)
    delete_standard(db, 4400)
    seen = []
    original = Database._free_standard_rows

    def spy(conn, rows):
        seen.append(conn.in_transaction)
        return original(conn, rows)

    monkeypatch.setattr(Database, '_free_standard_rows', staticmethod(spy))
    db.missing_standard_accounts()
    db.add_missing_standard_accounts()
    assert seen == [False, True]                                # das Nennen liest nur, das Anlegen liest in der Sperre


def test_an_account_that_appears_between_listing_and_adding_is_no_conflict(path):
    db = Database(db_name=path)
    delete_standard(db, 4400, 6805)
    assert [number for number, _ in db.missing_standard_accounts()] == [4400, 6805]      # so zeigt es die Seite
    occupy_number(db)                                           # inzwischen belegt jemand die 4400
    assert db.add_missing_standard_accounts() == 1
    assert row(db, 4400)[:5] == (999001, 4, 4400, 'Fremd', 'Fremde Gruppe')
    assert row(db, 6805)[3] == 'Telefon'


def test_a_failure_while_writing_rolls_everything_back_and_closes_the_connection(path, monkeypatch):
    db = Database(db_name=path)
    delete_standard(db, 4400, 6805, 1600)
    before = installation.dump(path)
    opened = watch(db, monkeypatch, FailingInsert)
    with pytest.raises(sqlite3.OperationalError):
        db.add_missing_standard_accounts()
    assert len(opened) == 1 and opened[0].closed
    assert opened[0].open_transaction_at_close is False         # zurückgerollt, bevor die Verbindung zugeht
    assert installation.dump(path) == before                    # auch die erste, schon geschriebene Zeile ist wieder weg
    not_locked(db)
    assert Database(db_name=path).add_missing_standard_accounts() == 3          # und danach geht es


def test_two_simultaneous_calls_create_every_account_exactly_once(path):
    db = Database(db_name=path)
    delete_standard(db, 4400, 6805, 1600)
    barrier, results, errors = threading.Barrier(2), [], []

    def call():
        try:
            barrier.wait()
            results.append(Database(db_name=path).add_missing_standard_accounts())
        except BaseException as exc:                            # noqa: BLE001 – der Test soll jeden Fehler zeigen
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and sorted(results) == [0, 3]
    assert db.missing_standard_accounts() == []


# ── die Seite des Kontenrahmens ──────────────────────────────────────────────
def test_a_complete_chart_shows_no_box(path):
    page = PageSkr(Database(db_name=path))
    assert box_text(page) is None
    assert 'add_standard' not in page and 'Standardkonten ergänzen' not in page


def test_one_missing_account_is_named_in_the_singular_with_a_button_that_asks_first(path):
    db = Database(db_name=path)
    delete_standard(db, 4400)
    page = PageSkr(db)
    assert box_text(page) == LEAD_ONE + '4400 Erlöse 19% USt.'
    button = the_button(page)
    assert button['type'] == 'button' and 'coloredButton' in button['class']
    assert button['onclick'] == CONFIRM_ONE
    assert '>Standardkonten ergänzen</button>' in page


def test_several_missing_accounts_are_counted_and_named_in_ascending_order(path):
    db = Database(db_name=path)
    delete_standard(db, 6805, 1600, 4400)
    page = PageSkr(db)
    assert box_text(page) == LEAD_MANY.format(n=3) + '1600 Kasse, 4400 Erlöse 19% USt, 6805 Telefon.'
    assert the_button(page)['onclick'] == CONFIRM_MANY.format(n=3)


@pytest.mark.parametrize('count', [19, 20, 21, 25])
def test_at_most_twenty_names_are_shown_and_the_rest_is_counted(path, seed_file, count):
    assert MISSING_NAMES_SHOWN == 20
    db = Database(db_name=path)
    entries = standard_entries(seed_file)[:count]
    delete_standard(db, *[number for number, _ in entries])
    page = PageSkr(db)
    assert box_text(page) == paragraph(entries)
    assert (' weitere' in box_text(page)) == (count > 20)
    assert the_button(page)['onclick'] == CONFIRM_MANY.format(n=count)


def test_the_names_are_escaped_and_only_the_count_goes_into_the_confirmation(path, seed_tree):
    source = seed_tree / 'seed_data' / 'chart_of_accounts_skr04.json'
    data = json.loads(source.read_text(encoding='utf-8'))
    next(a for a in data['accounts'] if a['account_number'] == 4400)['name'] = HOSTILE
    source.write_text(json.dumps(data), encoding='utf-8')
    db = Database(db_name=path)
    delete_standard(db, 4400)
    page = PageSkr(db)
    assert box_text(page) == LEAD_ONE + html.escape(f'4400 {HOSTILE}') + '.'
    assert HOSTILE not in page and '<script>alert(1)</script>' not in page
    assert the_button(page)['onclick'] == CONFIRM_ONE           # kein Name in der Rückfrage


def test_the_box_has_no_form_and_no_link_to_the_route(path):
    db = Database(db_name=path)
    delete_standard(db, 4400)
    page = PageSkr(db)
    dom = Dom(page)
    assert not [f for f in dom.all('form') if 'add_standard' in (f.get('action') or '')]
    assert not [a for a in dom.all('a') if 'add_standard' in (a.get('href') or '') or 'add_standard' in (a.get('onclick') or '')]
    assert 'formaction="/masterdata/skr/add_standard"' not in page
    assert the_button(page)['onclick'].startswith('appConfirmPost(')           # nie ohne Rückfrage


def test_viewing_the_page_writes_nothing(path):
    db = Database(db_name=path)
    delete_standard(db, 4400, 6805)
    before = installation.dump(path)
    PageSkr(db)
    PageSkr(db, added=2)
    PageSkr(db, edit_id=403100)
    assert installation.dump(path) == before
    assert [number for number, _ in db.missing_standard_accounts()] == [4400, 6805]
    not_locked(db)


@pytest.mark.parametrize('added, text', [
    pytest.param(0, 'Es fehlte kein Standardkonto.', id='keines'),
    pytest.param(1, '1 Standardkonto ergänzt.', id='eines'),
    pytest.param(2, '2 Standardkonten ergänzt.', id='mehrere'),
    pytest.param(ADDED_MAX, f'{ADDED_MAX} Standardkonten ergänzt.', id='die-Obergrenze'),
])
def test_the_page_reports_the_number_added_with_a_fixed_text(path, added, text):
    page = PageSkr(Database(db_name=path), added=added)
    assert flash(page) == [text, 'success']
    assert text not in outside_scripts(page)                    # nur als JSON im Skriptblock, nie als Markup


@pytest.mark.parametrize('added', [None, -1, ADDED_MAX + 1, 10 ** 12, True, False, '3', 3.0, '<script>alert(1)</script>'],
                         ids=lambda value: repr(value))
def test_anything_but_a_whole_number_in_range_shows_no_report(path, added):
    assert _added_flash(added) == (None, 'info')
    assert flash(PageSkr(Database(db_name=path), added=added)) is None


def test_a_message_of_the_address_wins_over_the_number(path):
    page = PageSkr(Database(db_name=path), msg='Das Konto wurde gelöscht.', msg_type='error', added=3)
    assert flash(page) == ['Das Konto wurde gelöscht.', 'error']


# ── über HTTP ────────────────────────────────────────────────────────────────
MISSING_OVER_HTTP = (1810, 3806, 4185)


@pytest.fixture
def web(server_port, auto_login, seed_file):
    """Der Server mit der Datenbank des Administrators, dem drei Standardkonten fehlen."""
    delete_standard(auto_login.db, *MISSING_OVER_HTTP)
    return SimpleNamespace(port=server_port, db=auto_login.db, csrf=auto_login.csrf,
                           entries=[(number, seeded(seed_file, number)[3]) for number in MISSING_OVER_HTTP])


def add_over_http(web):
    return post(web.port, '/masterdata/skr/add_standard', headers={'Origin': own_origin(web.port)}, csrf=web.csrf)


def test_the_page_names_what_is_missing_over_http(web):
    page = get(web.port, '/masterdata/skr')
    assert page.status == 200
    assert box_text(page.text) == paragraph(web.entries)
    assert the_button(page.text)['onclick'] == CONFIRM_MANY.format(n=3)
    assert flash(page.text) is None


def test_the_route_adds_the_missing_accounts_and_the_page_reports_the_number(web, seed_file):
    resp = add_over_http(web)
    assert resp.status == 303 and resp.headers['Location'] == '/masterdata/skr?added=3'
    for number in MISSING_OVER_HTTP:
        assert row(web.db, number) == seeded(seed_file, number)
    page = get(web.port, resp.headers['Location'])
    assert page.status == 200
    assert flash(page.text) == ['3 Standardkonten ergänzt.', 'success']
    assert box_text(page.text) is None and 'add_standard' not in page.text
    assert web.db.missing_standard_accounts() == []


def test_a_second_call_adds_nothing(web):
    assert add_over_http(web).headers['Location'] == '/masterdata/skr?added=3'
    after_first = installation.dump(web.db.db_name)
    resp = add_over_http(web)
    assert resp.status == 303 and resp.headers['Location'] == '/masterdata/skr?added=0'
    assert installation.dump(web.db.db_name) == after_first
    assert flash(get(web.port, resp.headers['Location']).text) == ['Es fehlte kein Standardkonto.', 'success']


def test_one_account_is_reported_in_the_singular(web):
    web.db.add_missing_standard_accounts()
    delete_standard(web.db, 4400)
    resp = add_over_http(web)
    assert resp.status == 303 and resp.headers['Location'] == '/masterdata/skr?added=1'
    assert flash(get(web.port, resp.headers['Location']).text) == ['1 Standardkonto ergänzt.', 'success']


def test_get_on_the_route_is_405_and_creates_nothing(web):
    before = installation.dump(web.db.db_name)
    for target in ('/masterdata/skr/add_standard', '/masterdata/skr/add_standard?added=3'):
        assert get(web.port, target).status == 405
    assert installation.dump(web.db.db_name) == before


@pytest.mark.parametrize('headers, csrf', [
    pytest.param({'Origin': FOREIGN_ORIGIN}, 'valid', id='fremde-Herkunft'),
    pytest.param({'Origin': 'own'}, None, id='ohne-Token'),
    pytest.param({'Origin': 'own'}, 'falsch', id='falsches-Token'),
])
def test_the_route_refuses_a_foreign_origin_and_a_missing_or_wrong_token(web, headers, csrf):
    before = installation.dump(web.db.db_name)
    headers = {'Origin': own_origin(web.port) if headers['Origin'] == 'own' else headers['Origin']}
    resp = post(web.port, '/masterdata/skr/add_standard', headers=headers, csrf=web.csrf if csrf == 'valid' else csrf)
    assert resp.status == 403
    assert installation.dump(web.db.db_name) == before


def test_viewing_the_page_writes_nothing_over_http(web):
    before = installation.dump(web.db.db_name)
    for target in ('/masterdata/skr', '/masterdata/skr?added=3', '/masterdata/skr/edit?id=404400',
                   '/masterdata/skr/copy?id=404400'):
        assert get(web.port, target).status == 200, target
    assert installation.dump(web.db.db_name) == before
    assert len(web.db.missing_standard_accounts()) == 3


@pytest.mark.parametrize('value', ['abc', '<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "';alert(1);//",
                                   '-1', '1e3', '1.5', '0x10', '9' * 30, '9223372036854775807', '-9223372036854775808',
                                   str(ADDED_MAX + 1), ''])
def test_an_address_value_that_is_no_whole_number_in_range_shows_no_report(web, value):
    page = get(web.port, '/masterdata/skr?' + urllib.parse.urlencode({'added': value}))
    assert page.status == 200
    assert flash(page.text) is None and 'Standardkonten ergänzt' not in page.text
    if re.search(r'[<>"\']', value):
        assert value not in page.text                           # aus der Adresse kommt nie Text auf die Seite


@pytest.mark.parametrize('value, text', [
    pytest.param('0', 'Es fehlte kein Standardkonto.', id='keines'),
    pytest.param('1', '1 Standardkonto ergänzt.', id='eines'),
    pytest.param('2', '2 Standardkonten ergänzt.', id='mehrere'),
    pytest.param('007', '7 Standardkonten ergänzt.', id='führende-Nullen'),
    pytest.param(str(ADDED_MAX), f'{ADDED_MAX} Standardkonten ergänzt.', id='die-Obergrenze'),
])
def test_a_whole_number_in_range_shows_only_the_fixed_text(web, value, text):
    page = get(web.port, '/masterdata/skr?' + urllib.parse.urlencode({'added': value}))
    assert page.status == 200 and flash(page.text) == [text, 'success']
    assert text not in outside_scripts(page.text)


def test_a_message_of_the_address_wins_over_the_number_over_http(web):
    query = urllib.parse.urlencode({'msg': 'Das Konto wurde gelöscht.', 'type': 'error', 'added': 3})
    page = get(web.port, '/masterdata/skr?' + query)
    assert page.status == 200 and flash(page.text) == ['Das Konto wurde gelöscht.', 'error']
