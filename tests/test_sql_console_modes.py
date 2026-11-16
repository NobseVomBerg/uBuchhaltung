# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""SQL-Konsole: Lesemodus und Schreibmodus (seit 2.3.11).

Das Formularfeld ``write`` mit genau dem Wert ``1`` schaltet den Schreibmodus ein, alles andere (auch fehlend)
bleibt im Lesemodus:

  1. Lesemodus (Standard): ``PRAGMA query_only = ON`` und ein Authorizer, der nur Abfragen durchlässt. Jedes Schreiben
     wird abgelehnt – auch über CTE, Trigger und Sichten –, die Datenbank bleibt Byte für Byte, wie sie war, und
     ``query_only`` lässt sich nicht abschalten. Jeder Befehl läuft für sich, ein Fehler stoppt die folgenden nicht,
  2. Schreibmodus: Fremdschlüssel an, ein Wachstumslimit für die Datei (``max_page_count``) und EINE Transaktion für alle
     Befehle. Der erste Fehler, ein Abbruch (Zeit, Platte) und ein scheiterndes COMMIT nehmen alles zurück – auch
     Strukturbefehle. BEGIN, COMMIT, ROLLBACK, SAVEPOINT und ``PRAGMA foreign_keys`` mit Wert sind gesperrt,
  3. Platte: Der Fortschrittshandler prüft in größeren Abständen den freien Platz im Ordner der Datenbank und im
     Temp-Ordner (Mindestmaß und Verbrauch seit Beginn); ein Fehler beim Messen bricht nichts ab,
  4. SQL-Protokoll: Nach dem COMMIT geht jeder Befehl, der etwas Schreibendes verlangt hat, ins Protokoll – mit dem
     Benutzer, ohne Kommentare, nie bei Abfragen und nie bei einer zurückgerollten Anfrage; scheitert das Protokoll,
     bleibt die Änderung und die Antwort trägt eine Warnung ohne Fehlertext,
  5. eine Anfrage zur Zeit; die Sperre wird in jedem Fall wieder frei; die Fehlerliste ist gekappt,
  6. HTTP: ``write`` zählt nur mit dem Wert ``1``.

Die Sperren (ATTACH, PRAGMA-Listen, Funktionen) stehen in tests/test_sql_console.py und
tests/test_sql_console_authorizer.py, Zeit- und Größengrenzen in tests/test_sql_console_limits.py, das Trennen der
Befehle in tests/test_sql_console_statements.py, die Seite in tests/test_sql_console_page.py. Alle Namen und Werte
sind erfunden.
"""
import contextlib
import json
import os
import shutil
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

import userctx
from server import handlers
from tests import installation
from tests.http_client import post
from tests.test_sql_console import (BOTH_MODES, CONSOLE_DENIED, NOTHING_CHANGED, READONLY, _post_sql, _query,  # noqa: F401
                                    _run, denied, probe_db)

BUSY = "Es läuft bereits eine Anfrage der SQL-Konsole – bitte warten."
LOCKED = ("Die Datenbank ist gerade gesperrt (ein anderer Vorgang schreibt) – nichts wurde ausgeführt. "
          "Bitte gleich noch einmal versuchen.")
LOG_WARNING = "Die Änderungen sind gespeichert, ließen sich aber nicht ins SQL-Protokoll schreiben."
DISK_ABORT = "abgebrochen – die Anfrage belegte zu viel Plattenplatz."
GB = 1_000_000_000


# ── Hilfen ───────────────────────────────────────────────────────────────────
@pytest.fixture
def mode_db(probe_db):
    """``probe_db`` (Tabelle Probe mit x/1 und y/2) dazu: ein Index, eine Protokolltabelle samt Triggern auf Probe, eine
    Sicht mit INSTEAD-OF-Trigger und ein Eltern/Kind-Paar mit Fremdschlüssel – die Umwege, über die ein Schreiben im
    Lesemodus sonst gelänge."""
    with contextlib.closing(sqlite3.connect(probe_db.db_name)) as con:
        con.executescript("""
            CREATE INDEX IProbe ON Probe (A);
            CREATE TABLE Protokoll (Was TEXT);
            CREATE TRIGGER probe_ins AFTER INSERT ON Probe BEGIN INSERT INTO Protokoll VALUES ('insert'); END;
            CREATE TRIGGER probe_upd AFTER UPDATE ON Probe BEGIN INSERT INTO Protokoll VALUES ('update'); END;
            CREATE TRIGGER probe_del AFTER DELETE ON Probe BEGIN INSERT INTO Protokoll VALUES ('delete'); END;
            CREATE VIEW SichtProbe AS SELECT A, B FROM Probe;
            CREATE TRIGGER sicht_ins INSTEAD OF INSERT ON SichtProbe
                BEGIN INSERT INTO Probe VALUES (NEW.A, NEW.B); END;
            CREATE TABLE Eltern (ID INTEGER PRIMARY KEY);
            CREATE TABLE Kind (ID INTEGER PRIMARY KEY, Eltern_ID INTEGER REFERENCES Eltern (ID));
            INSERT INTO Eltern VALUES (1);
        """)
    return probe_db


def _state(db):
    """Zustand der Datenbank zum Vorher/Nachher-Vergleich: Dateiinhalt Byte für Byte (sha256), logischer Inhalt
    (iterdump) und die Begleitdateien (-journal, -wal …)."""
    folder, name = os.path.split(db.db_name)
    return (installation.file_digest(db.db_name), installation.dump(db.db_name),
            sorted(entry for entry in os.listdir(folder) if entry.startswith(name)))


def _numbers(n):
    """Die Zahlen 1..n als Unterabfrage (rekursive CTE)."""
    return f"(WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT {n}) SELECT x FROM c)"


LONG_QUERY = f"SELECT count(*) FROM {_numbers(300_000)}"        # läuft einige Millionen Schritte: der Fortschrittshandler kommt oft


def _log_files():
    """Inhalt der beiden SQL-Protokolle des aktuellen Benutzers: ``(sql_operations.sql, sql_operations.log)`` – ``None``,
    wo die Datei fehlt."""
    contents = []
    for name in ("sql_operations.sql", "sql_operations.log"):
        path = os.path.join(userctx.user_data_dir(), name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                contents.append(handle.read())
        else:
            contents.append(None)
    return tuple(contents)


def _logged_commands():
    """Die Zeilen von sql_operations.sql (ein Befehl je Zeile, mit abschließendem Semikolon)."""
    sql, _ = _log_files()
    return [] if sql is None else sql.splitlines()


class CountingClock:
    """Ersatz für ``handlers.time``: zählt die Aufrufe von ``monotonic()`` und bleibt bei 0 – die Frist läuft nie ab. Jeder
    Aufruf nach den ersten beiden (Frist, vor Befehl 1) ist ein Aufruf des Fortschrittshandlers."""

    def __init__(self):
        self.calls = 0

    def monotonic(self):
        self.calls += 1
        return 0.0


class ScriptedClock:
    """Wie in tests/test_sql_console_limits.py: ``monotonic()`` liefert nacheinander die vorgegebenen Werte."""

    def __init__(self, *ticks):
        self.ticks, self.calls = ticks, 0

    def monotonic(self):
        tick = self.ticks[min(self.calls, len(self.ticks) - 1)]
        self.calls += 1
        return tick


# ── Teil 1: Lesemodus ────────────────────────────────────────────────────────
WRITES = {
    "INSERT": "INSERT INTO Probe VALUES ('z', 3)",
    "INSERT-SELECT": "INSERT INTO Probe SELECT A, B FROM Probe",
    "INSERT-DEFAULT-VALUES": "INSERT INTO Probe DEFAULT VALUES",
    "REPLACE": "REPLACE INTO Probe VALUES ('x', 1)",
    "UPSERT": "INSERT INTO Eltern VALUES (1) ON CONFLICT (ID) DO UPDATE SET ID = 5",
    "INSERT-RETURNING": "INSERT INTO Probe VALUES ('z', 3) RETURNING A",
    "UPDATE": "UPDATE Probe SET B = B + 1",
    "UPDATE-RETURNING": "UPDATE Probe SET B = 5 RETURNING B",
    "DELETE": "DELETE FROM Probe",
    "DELETE-WHERE-RETURNING": "DELETE FROM Probe WHERE B = 1 RETURNING A",
    "CTE-INSERT": "WITH n AS (SELECT 'z' AS a, 3 AS b) INSERT INTO Probe SELECT a, b FROM n",
    "CTE-UPDATE": "WITH n AS (SELECT 9 AS b) UPDATE Probe SET B = (SELECT b FROM n)",
    "CTE-DELETE": "WITH n AS (SELECT 1 AS b) DELETE FROM Probe WHERE B IN (SELECT b FROM n)",
    "Sicht-mit-INSTEAD-OF-Trigger": "INSERT INTO SichtProbe VALUES ('z', 3)",
    "Kindtabelle": "INSERT INTO Kind VALUES (1, 1)",
    "CREATE-TABLE": "CREATE TABLE Neu (A TEXT)",
    "CREATE-TABLE-AS": "CREATE TABLE Neu AS SELECT * FROM Probe",
    "CREATE-TEMP-TABLE": "CREATE TEMP TABLE Neu (A TEXT)",
    "CREATE-INDEX": "CREATE INDEX INeu ON Probe (B)",
    "CREATE-VIEW": "CREATE VIEW VNeu AS SELECT 1",
    "CREATE-TRIGGER": "CREATE TRIGGER tneu AFTER INSERT ON Eltern BEGIN INSERT INTO Protokoll VALUES ('neu'); END",
    "DROP-TABLE": "DROP TABLE Probe",
    "DROP-TABLE-mit-Triggern": "DROP TABLE Protokoll",
    "DROP-INDEX": "DROP INDEX IProbe",
    "DROP-VIEW": "DROP VIEW SichtProbe",
    "DROP-TRIGGER": "DROP TRIGGER probe_ins",
    "ALTER-ADD-COLUMN": "ALTER TABLE Probe ADD COLUMN C TEXT",
    "ALTER-RENAME-TABLE": "ALTER TABLE Probe RENAME TO Probe2",
    "ALTER-RENAME-COLUMN": "ALTER TABLE Probe RENAME COLUMN A TO AA",
    "ANALYZE": "ANALYZE",
    "REINDEX": "REINDEX",
    "VACUUM": "VACUUM",
    "ATTACH": "ATTACH ':memory:' AS m",
    "Schema-direkt": "UPDATE sqlite_master SET name = name WHERE 0",
    "EXPLAIN-INSERT": "EXPLAIN INSERT INTO Probe VALUES ('z', 3)",
    "PRAGMA-query_only-aus": "PRAGMA query_only = OFF",
    "PRAGMA-query_only-0": "PRAGMA query_only = 0",
    "PRAGMA-query_only-lesen": "PRAGMA query_only",
    "PRAGMA-user_version": "PRAGMA user_version = 3",
    "BEGIN": "BEGIN",
    "BEGIN-IMMEDIATE": "BEGIN IMMEDIATE",
    "BEGIN-EXCLUSIVE": "BEGIN EXCLUSIVE TRANSACTION",
    "COMMIT": "COMMIT",
    "END": "END",
    "ROLLBACK": "ROLLBACK",
    "SAVEPOINT": "SAVEPOINT a",
    "RELEASE": "RELEASE a",
    "ROLLBACK-TO": "ROLLBACK TO a",
}


#: Was SQLite selbst schon vor dem Authorizer ablehnt (mit seinem eigenen Text)
SQLITE_REFUSES_FIRST = {"Schema-direkt": "table sqlite_master may not be modified"}


@pytest.mark.parametrize("name", WRITES.keys())
def test_reading_mode_denies_every_kind_of_write_and_leaves_the_database_as_it_was(mode_db, name):
    before = _state(mode_db)
    expected = denied(False)
    if name in SQLITE_REFUSES_FIRST:
        expected["errors"] = [f"Befehl 1: {SQLITE_REFUSES_FIRST[name]}"]
    assert _run(mode_db, WRITES[name]) == expected
    assert _state(mode_db) == before
    assert _query(mode_db, "SELECT COUNT(*) FROM Protokoll") == [(0,)]               # kein Trigger ist gelaufen


def test_the_reading_mode_is_the_default_for_everything_but_the_exact_value_1(mode_db):
    for form in ({"sql_commands": ["INSERT INTO Probe VALUES ('z', 3)"]},
                 {"sql_commands": ["INSERT INTO Probe VALUES ('z', 3)"], "write": ["0"]},
                 {"sql_commands": ["INSERT INTO Probe VALUES ('z', 3)"], "write": [""]}):
        assert json.loads(handlers.handle_execute_sql(mode_db, form)) == denied(False)
    assert _query(mode_db, "SELECT COUNT(*) FROM Probe") == [(2,)]


def test_query_only_cannot_be_switched_off_and_the_inserts_behind_it_stay_denied(mode_db):
    before = _state(mode_db)
    result = _run(mode_db, "PRAGMA query_only = OFF; INSERT INTO Probe VALUES ('z', 3); "
                           "PRAGMA query_only = 0; DELETE FROM Probe")
    assert result["success_count"] == 0 and result["total"] == 4
    assert result["errors"] == [f"Befehl {n}: {READONLY}" for n in (1, 2, 3, 4)]
    assert _state(mode_db) == before


def test_query_only_is_on_no_pragma_list():
    assert "query_only" not in handlers._CONSOLE_PRAGMAS_READ | handlers._CONSOLE_PRAGMAS_WITH_ARG


@pytest.mark.parametrize("sql", ["INSERT INTO Probe VALUES ('z', 3)", "UPDATE Probe SET B = 5", "DELETE FROM Probe",
                                 "CREATE TABLE Neu (A TEXT)", "DROP TABLE Probe", "ANALYZE",
                                 "ALTER TABLE Probe ADD COLUMN C TEXT"])
def test_query_only_alone_stops_writes_when_the_authorizer_lets_everything_pass(mode_db, monkeypatch, sql):
    """Die zweite Schicht des Lesemodus: ``PRAGMA query_only = ON``, noch vor dem Authorizer gesetzt. Selbst ein
    Authorizer, der alles durchlässt, bekäme kein Schreiben durch."""
    monkeypatch.setattr(handlers, "_ConsoleAuthorizer", lambda write: (lambda *args: sqlite3.SQLITE_OK))
    before = _state(mode_db)
    assert _run(mode_db, sql) == denied(False)
    assert _state(mode_db) == before


READS = [
    "SELECT * FROM Probe ORDER BY B",
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c LIMIT 5) SELECT sum(x) FROM c",
    "SELECT * FROM SichtProbe",
    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
    "SELECT * FROM pragma_table_info('Probe')",
    "SELECT upper(A), length(A), json_extract('[1,2]', '$[1]') FROM Probe",
    "PRAGMA table_info(Probe)", "PRAGMA foreign_key_check", "PRAGMA integrity_check", "PRAGMA quick_check",
    "PRAGMA user_version", "EXPLAIN QUERY PLAN SELECT * FROM Probe WHERE A = 'x'",
]


@pytest.mark.parametrize("sql", READS)
def test_reading_mode_still_reads_and_changes_nothing(mode_db, sql):
    before = _state(mode_db)
    result = _run(mode_db, sql)
    assert (result["success_count"], result["errors"], result["total"]) == (1, [], 1)
    assert "rolled_back" not in result
    assert _state(mode_db) == before


def test_the_reading_mode_answers_have_the_old_shape(mode_db):
    assert _run(mode_db, "SELECT 1") == {"success_count": 1, "total": 1, "errors": [], "output": "1\n1"}


# ── Teil 1: Entscheidungen des Authorizers je Modus ──────────────────────────
WRITE_ACTIONS = [sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_CREATE_TABLE,
                 sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_CREATE_TRIGGER,
                 sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_VIEW,
                 sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_TABLE,
                 sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_TRIGGER,
                 sqlite3.SQLITE_DROP_TEMP_TABLE, sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_VIEW,
                 sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_ALTER_TABLE,
                 sqlite3.SQLITE_ANALYZE, sqlite3.SQLITE_REINDEX]
READ_ACTIONS = [sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE]


def _decide(write, action, arg1=None, arg2=None):
    """Was der Authorizer einer Anfrage entscheidet – und ob er den Befehl als schreibend merkt."""
    authorizer = handlers._ConsoleAuthorizer(write)
    return authorizer(action, arg1, arg2, None, None), authorizer.wrote


@pytest.mark.parametrize("action", WRITE_ACTIONS)
def test_the_reading_authorizer_denies_every_writing_action(action):
    assert _decide(False, action, "Probe") == (sqlite3.SQLITE_DENY, False)


@pytest.mark.parametrize("action", WRITE_ACTIONS + [sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT])
def test_the_reading_authorizer_denies_every_action_that_is_not_a_read(action):
    assert _decide(False, action, "BEGIN")[0] == sqlite3.SQLITE_DENY


@pytest.mark.parametrize("action", READ_ACTIONS)
def test_the_reading_authorizer_lets_reads_pass(action):
    assert _decide(False, action, "Probe", "A") == (sqlite3.SQLITE_OK, False)


@pytest.mark.parametrize("action", READ_ACTIONS)
def test_the_writing_authorizer_lets_reads_pass_without_marking_them_as_writes(action):
    assert _decide(True, action, "Probe", "A") == (sqlite3.SQLITE_OK, False)


@pytest.mark.parametrize("action", WRITE_ACTIONS)
def test_the_writing_authorizer_lets_writes_pass_and_marks_them(action):
    assert _decide(True, action, "Probe") == (sqlite3.SQLITE_OK, True)


@pytest.mark.parametrize("action, arg1", [(sqlite3.SQLITE_TRANSACTION, "BEGIN"), (sqlite3.SQLITE_TRANSACTION, "COMMIT"),
                                          (sqlite3.SQLITE_TRANSACTION, "ROLLBACK"), (sqlite3.SQLITE_SAVEPOINT, "BEGIN"),
                                          (sqlite3.SQLITE_SAVEPOINT, "RELEASE"), (sqlite3.SQLITE_SAVEPOINT, "ROLLBACK")])
def test_the_writing_authorizer_denies_transaction_and_savepoint_commands(action, arg1):
    assert _decide(True, action, arg1) == (sqlite3.SQLITE_DENY, False)


@pytest.mark.parametrize("write, arg1, arg2, expected", [
    pytest.param(True, "foreign_keys", "OFF", sqlite3.SQLITE_DENY, id="schreibend-mit-Wert"),
    pytest.param(True, "FOREIGN_KEYS", "1", sqlite3.SQLITE_DENY, id="schreibend-gross"),
    pytest.param(True, "foreign_keys", "", sqlite3.SQLITE_DENY, id="schreibend-mit-leerem-Wert"),
    pytest.param(True, "foreign_keys", None, sqlite3.SQLITE_OK, id="schreibend-lesen"),
    pytest.param(True, "defer_foreign_keys", "ON", sqlite3.SQLITE_OK, id="schreibend-defer-mit-Wert"),
    pytest.param(True, "defer_foreign_keys", None, sqlite3.SQLITE_OK, id="schreibend-defer-lesen"),
    pytest.param(True, "table_info", "Probe", sqlite3.SQLITE_OK, id="schreibend-Abfrage-PRAGMA"),
    pytest.param(True, "user_version", "7", sqlite3.SQLITE_DENY, id="schreibend-Liste-gilt-weiter"),
    pytest.param(False, "foreign_keys", "OFF", sqlite3.SQLITE_OK, id="lesend-mit-Wert-harmlos"),
    pytest.param(False, "user_version", "7", sqlite3.SQLITE_DENY, id="lesend-Liste-gilt"),
])
def test_foreign_keys_pragma_with_a_value_is_denied_only_while_writing(write, arg1, arg2, expected):
    decision, wrote = _decide(write, sqlite3.SQLITE_PRAGMA, arg1, arg2)
    assert (decision, wrote) == (expected, False)             # ein PRAGMA gilt nie als schreibender Befehl


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("action, arg1", [(sqlite3.SQLITE_ATTACH, "x.db"), (sqlite3.SQLITE_DETACH, "x"),
                                          (sqlite3.SQLITE_PRAGMA, "optimize"), (sqlite3.SQLITE_PRAGMA, "gibtesnicht"),
                                          (sqlite3.SQLITE_PRAGMA, None)])
def test_both_authorizers_keep_the_permanent_blocks(write, action, arg1):
    assert _decide(write, action, arg1)[0] == sqlite3.SQLITE_DENY


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("name", ["fts3_tokenizer", "Load_Extension"])
def test_both_authorizers_keep_the_function_blocks(write, name):
    assert _decide(write, sqlite3.SQLITE_FUNCTION, None, name)[0] == sqlite3.SQLITE_DENY


def test_the_actions_the_reading_authorizer_lets_through_are_exactly_these():
    assert handlers._CONSOLE_READ_ACTIONS == {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                                              sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_PRAGMA}


# ── Teil 2: Schreibmodus ─────────────────────────────────────────────────────
def test_writing_mode_commits_everything_when_all_commands_succeed(mode_db):
    result = _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); UPDATE Probe SET B = 7 WHERE A = 'z'; "
                           "DELETE FROM Probe WHERE A = 'x'", write=True)
    assert result == {"success_count": 3, "total": 3, "errors": [], "output": "", "rolled_back": False}
    assert _query(mode_db, "SELECT A, B FROM Probe ORDER BY B") == [("y", 2), ("z", 7)]
    assert _query(mode_db, "SELECT Was FROM Protokoll ORDER BY rowid") == [("insert",), ("update",), ("delete",)]


def test_writing_mode_changes_are_visible_to_another_connection_after_the_answer(mode_db):
    _run(mode_db, "CREATE TABLE Neu (A TEXT); INSERT INTO Neu VALUES ('a')", write=True)
    assert _query(mode_db, "SELECT A FROM Neu") == [("a",)]
    with contextlib.closing(sqlite3.connect(mode_db.db_name, timeout=0.2, isolation_level=None)) as con:
        con.execute("BEGIN EXCLUSIVE")                       # nichts hält eine Sperre
        con.execute("ROLLBACK")


def test_an_error_in_command_3_of_5_undoes_everything_including_structure_commands(mode_db):
    before = _state(mode_db)
    result = _run(mode_db, "CREATE TABLE Neu (A TEXT); INSERT INTO Probe VALUES ('z', 3); "
                           "INSERT INTO Gibtesnicht VALUES (1); SELEKT 1; CREATE TABLE Neu2 (A TEXT)", write=True)
    assert result == {"success_count": 0, "total": 5, "output": "", "rolled_back": True,
                      "errors": ["Befehl 3: no such table: Gibtesnicht", NOTHING_CHANGED]}
    assert _state(mode_db) == before                                              # auch der Tabellenbau ist weg
    assert _query(mode_db, "SELECT name FROM sqlite_master WHERE name LIKE 'Neu%'") == []


def test_commands_after_the_failing_one_do_not_run(mode_db, monkeypatch):
    ran = []
    real_cell = handlers._console_cell
    monkeypatch.setattr(handlers, "_console_cell", lambda value: ran.append(value) or real_cell(value))
    result = _run(mode_db, "SELECT 'eins'; SELEKT 1; SELECT 'drei'", write=True)
    assert result["errors"] == ['Befehl 2: near "SELEKT": syntax error', NOTHING_CHANGED]
    assert ran == ["eins"] and result["output"] == ""                              # Befehl 3 lief nie


def test_the_output_of_an_undone_request_is_not_shown(mode_db):
    result = _run(mode_db, "SELECT A FROM Probe; SELEKT 1", write=True)
    assert result["output"] == "" and result["success_count"] == 0


def test_a_failing_commit_undoes_everything(mode_db):
    """Aufgeschobene Fremdschlüssel-Prüfung (``PRAGMA defer_foreign_keys``): Die Verletzung fällt erst beim COMMIT auf."""
    before = _state(mode_db)
    result = _run(mode_db, "CREATE TABLE Neu (A TEXT); PRAGMA defer_foreign_keys = ON; "
                           "INSERT INTO Kind VALUES (1, 99)", write=True)
    assert result == {"success_count": 0, "total": 3, "output": "", "rolled_back": True,
                      "errors": ["Beim Festschreiben (COMMIT): FOREIGN KEY constraint failed", NOTHING_CHANGED]}
    assert _state(mode_db) == before
    assert _query(mode_db, "SELECT COUNT(*) FROM Kind") == [(0,)]

    fixed = _run(mode_db, "PRAGMA defer_foreign_keys = ON; INSERT INTO Kind VALUES (1, 99); "
                          "INSERT INTO Eltern VALUES (99)", write=True)          # Kontrolle: der Eltern-Satz folgt noch
    assert (fixed["errors"], fixed["rolled_back"]) == ([], False)
    assert _query(mode_db, "SELECT Eltern_ID FROM Kind") == [(99,)]
    assert not handlers._console_lock.locked()


def test_foreign_keys_are_on_while_writing_and_off_while_reading(mode_db):
    assert _run(mode_db, "SELECT * FROM pragma_foreign_keys", write=True)["output"] == "foreign_keys\n1"
    assert _run(mode_db, "SELECT * FROM pragma_foreign_keys")["output"] == "foreign_keys\n0"
    assert _run(mode_db, "PRAGMA foreign_keys", write=True)["output"] == "foreign_keys\n1"


def test_foreign_keys_hold_in_writing_mode(mode_db):
    before = _state(mode_db)
    refused = _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); INSERT INTO Kind VALUES (1, 99)", write=True)
    assert refused["errors"] == ["Befehl 2: FOREIGN KEY constraint failed", NOTHING_CHANGED]
    assert _state(mode_db) == before

    assert _run(mode_db, "INSERT INTO Kind VALUES (1, 1)", write=True)["errors"] == []
    for sql in ("DELETE FROM Eltern WHERE ID = 1", "DROP TABLE Eltern", "UPDATE Eltern SET ID = 2"):
        result = _run(mode_db, sql, write=True)
        assert result["errors"] == ["Befehl 1: FOREIGN KEY constraint failed", NOTHING_CHANGED], sql
    assert _query(mode_db, "SELECT ID FROM Eltern") == [(1,)]


@pytest.mark.parametrize("sql", [
    "BEGIN", "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE TRANSACTION", "COMMIT", "END", "ROLLBACK", "SAVEPOINT a",
    "RELEASE a", "RELEASE SAVEPOINT a", "ROLLBACK TO a", "ROLLBACK TO SAVEPOINT a",
    "PRAGMA foreign_keys = OFF", "PRAGMA foreign_keys = ON", "PRAGMA foreign_keys(0)", "PRAGMA main.foreign_keys = 0",
    "PRAGMA max_page_count = 999999999", "PRAGMA query_only = OFF",
])
def test_writing_mode_denies_transaction_commands_and_foreign_keys_with_a_value(mode_db, sql):
    before = _state(mode_db)
    result = _run(mode_db, f"INSERT INTO Probe VALUES ('z', 3); {sql}; INSERT INTO Probe VALUES ('q', 4)", write=True)
    assert result == denied(True, number=2, total=3)
    assert _state(mode_db) == before


def test_the_message_for_denied_commands_names_the_transaction_commands():
    assert "BEGIN, COMMIT, ROLLBACK, SAVEPOINT" in CONSOLE_DENIED and "PRAGMA foreign_keys mit Wert" in CONSOLE_DENIED
    assert CONSOLE_DENIED.startswith("nicht erlaubt – ATTACH, DETACH, VACUUM sowie verändernde oder unbekannte PRAGMAs")


def test_writing_mode_cannot_edit_the_schema_table(mode_db):
    before = _state(mode_db)
    result = _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); UPDATE sqlite_master SET name = name WHERE 0", write=True)
    assert result["errors"] == ["Befehl 2: table sqlite_master may not be modified", NOTHING_CHANGED]
    assert _state(mode_db) == before


@pytest.mark.parametrize("sql", ["PRAGMA foreign_keys", "PRAGMA defer_foreign_keys = ON", "PRAGMA defer_foreign_keys",
                                 "SELECT * FROM pragma_foreign_keys", "PRAGMA max_page_count", "PRAGMA table_info(Probe)"])
def test_writing_mode_allows_the_pragmas_that_stay_useful(mode_db, sql):
    result = _run(mode_db, sql, write=True)
    assert (result["errors"], result["rolled_back"]) == ([], False)


def test_a_locked_database_is_reported_in_plain_text_and_nothing_runs(mode_db, monkeypatch):
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: real_connect(*args, **{**kwargs, "timeout": 0.1}))
    with contextlib.closing(real_connect(mode_db.db_name, isolation_level=None)) as holder:
        holder.execute("BEGIN IMMEDIATE")                    # ein anderer Vorgang schreibt gerade
        started = time.perf_counter()
        result = _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); SELECT 1", write=True)
        assert time.perf_counter() - started < 3
        assert result == {"success_count": 0, "total": 2, "output": "", "errors": [LOCKED]}
        assert _run(mode_db, "SELECT COUNT(*) FROM Probe")["output"] == "COUNT(*)\n2"       # Lesen geht weiter
        holder.execute("ROLLBACK")
    assert _query(mode_db, "SELECT COUNT(*) FROM Probe") == [(2,)]
    assert not handlers._console_lock.locked()
    assert _run(mode_db, "INSERT INTO Probe VALUES ('z', 3)", write=True)["errors"] == []


# ── Teil 2: Wachstum der Datei ───────────────────────────────────────────────
def test_the_growth_limit_default_is_one_gigabyte():
    assert handlers._CONSOLE_MAX_GROWTH_BYTES == 1_000_000_000


def test_writing_mode_sets_max_page_count_to_the_current_size_plus_the_growth_limit(mode_db, monkeypatch):
    pages = _query(mode_db, "PRAGMA page_count")[0][0]
    size = _query(mode_db, "PRAGMA page_size")[0][0]
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_GROWTH_BYTES", 40 * size)
    assert _run(mode_db, "PRAGMA max_page_count", write=True)["output"] == f"max_page_count\n{pages + 40}"
    assert _run(mode_db, "PRAGMA max_page_count")["output"] != f"max_page_count\n{pages + 40}"     # lesend: nicht gesetzt


def test_writing_mode_limits_how_far_the_file_may_grow(mode_db, monkeypatch):
    size = _query(mode_db, "PRAGMA page_size")[0][0]
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_GROWTH_BYTES", 40 * size)
    file_size = os.path.getsize(mode_db.db_name)
    before = _state(mode_db)

    result = _run(mode_db, "CREATE TABLE Gross (X BLOB); INSERT INTO Gross SELECT zeroblob(4000) FROM "
                           f"{_numbers(200)}", write=True)
    assert result["errors"] == ["Befehl 2: database or disk is full", NOTHING_CHANGED]
    assert (result["success_count"], result["rolled_back"]) == (0, True)
    assert _state(mode_db) == before and os.path.getsize(mode_db.db_name) == file_size

    small = _run(mode_db, "CREATE TABLE Klein (X BLOB); INSERT INTO Klein VALUES (zeroblob(4000))", write=True)
    assert (small["errors"], small["rolled_back"]) == ([], False)                  # innerhalb der Grenze geht es


def test_the_growth_limit_counts_from_the_size_at_the_start_of_each_request(mode_db, monkeypatch):
    size = _query(mode_db, "PRAGMA page_size")[0][0]
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_GROWTH_BYTES", 30 * size)
    fill = f"INSERT INTO Gross SELECT zeroblob(4000) FROM {_numbers(20)}"
    assert _run(mode_db, "CREATE TABLE Gross (X BLOB)", write=True)["errors"] == []
    for _ in range(3):                                         # jede Anfrage darf wieder um 30 Seiten wachsen
        assert _run(mode_db, fill, write=True)["errors"] == []
    assert _query(mode_db, "SELECT COUNT(*) FROM Gross") == [(60,)]


# ── Teil 3: Platte ───────────────────────────────────────────────────────────
TEMP_DIR = "/nirgendwo/temp"


def _disk(monkeypatch, db_dir, db_free=(100 * GB,), temp_free=(100 * GB,), fail=False):
    """``shutil.disk_usage`` und den Temp-Ordner im Handler ersetzen: Je Ordner (Datenordner der Datenbank, Temp-Ordner)
    liefert die Messung nacheinander die Werte der Liste, danach immer den letzten – der erste Wert ist der zu Beginn
    der Anfrage. ``fail`` lässt jede Messung nach der ersten scheitern. Liefert die Liste der Messungen."""
    calls = []
    series = {os.path.abspath(db_dir): list(db_free), TEMP_DIR: list(temp_free)}

    def disk_usage(folder):
        key = os.path.abspath(folder) if folder != TEMP_DIR else TEMP_DIR
        assert key in series, folder
        calls.append(key)
        if fail and len([c for c in calls if c == key]) > 1:
            raise OSError("nicht messbar")
        values = series[key]
        free = values.pop(0) if len(values) > 1 else values[0]
        return SimpleNamespace(total=1000 * GB, used=1000 * GB - free, free=free)

    monkeypatch.setattr(handlers, "shutil", SimpleNamespace(disk_usage=disk_usage))
    monkeypatch.setattr(handlers, "tempfile", SimpleNamespace(gettempdir=lambda: TEMP_DIR))
    monkeypatch.setattr(handlers, "_CONSOLE_DISK_CHECK_EVERY", 1)
    return calls


def _disk_abort(number, write):
    errors = [f"Befehl {number}: {DISK_ABORT}"] + ([NOTHING_CHANGED] if write else [])
    result = {"success_count": 0, "total": 2, "errors": errors, "output": ""}
    return {**result, "rolled_back": True} if write else result


def _folder(db):
    return os.path.dirname(db.db_name)


@BOTH_MODES
def test_the_request_is_aborted_when_it_has_used_more_than_the_disk_limit(mode_db, monkeypatch, write):
    _disk(monkeypatch, _folder(mode_db), db_free=(100 * GB, 98 * GB))          # 2 GB weniger als zu Beginn
    first = "INSERT INTO Probe VALUES ('z', 3)" if write else "SELECT 1"
    before = _state(mode_db)
    assert _run(mode_db, f"{first}; {LONG_QUERY}", write) == _disk_abort(2, write)
    assert _state(mode_db) == before                                           # im Schreibmodus ist auch das INSERT weg
    assert not handlers._console_lock.locked()


def test_the_disk_limit_is_measured_in_the_temp_folder_too(mode_db, monkeypatch):
    _disk(monkeypatch, _folder(mode_db), temp_free=(100 * GB, 98 * GB))
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}") == _disk_abort(2, False)


@BOTH_MODES
def test_the_request_is_aborted_when_the_free_space_falls_below_the_minimum(mode_db, monkeypatch, write):
    _disk(monkeypatch, _folder(mode_db), db_free=(100 * GB, 200_000_000))      # 200 MB < 256 MB
    first = "INSERT INTO Probe VALUES ('z', 3)" if write else "SELECT 1"
    assert _run(mode_db, f"{first}; {LONG_QUERY}", write) == _disk_abort(2, write)


def test_a_folder_that_is_small_from_the_start_and_does_not_shrink_aborts_nothing(mode_db, monkeypatch):
    """Ein knapp bemessener Temp-Ordner (64 MB) liegt immer unter dem Minimum – solange die Anfrage dort nichts belegt,
    läuft sie."""
    _disk(monkeypatch, _folder(mode_db), temp_free=(64_000_000,))
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}")["errors"] == []
    _disk(monkeypatch, _folder(mode_db), db_free=(64_000_000, 64_000_001))     # mehr Platz als zu Beginn: ebenso
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}")["errors"] == []
    _disk(monkeypatch, _folder(mode_db), temp_free=(64_000_000, 63_999_999))   # gesunken und unter dem Minimum
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}") == _disk_abort(2, False)


def test_the_free_space_in_the_temp_folder_counts_for_the_minimum_too(mode_db, monkeypatch):
    _disk(monkeypatch, _folder(mode_db), temp_free=(100 * GB, 10_000_000))
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}") == _disk_abort(2, False)


def test_a_request_inside_both_limits_runs_through(mode_db, monkeypatch):
    calls = _disk(monkeypatch, _folder(mode_db), db_free=(100 * GB, 99_500_000_000, 99_200_000_000))
    result = _run(mode_db, f"INSERT INTO Probe VALUES ('z', 3); {LONG_QUERY}", write=True)
    assert (result["errors"], result["rolled_back"]) == ([], False)
    assert len(calls) > 2                                                      # es wurde gemessen


def test_the_limits_apply_to_the_values_in_force(mode_db, monkeypatch):
    _disk(monkeypatch, _folder(mode_db), db_free=(100 * GB, 99 * GB))          # 1 GB weniger: bei gleich ist nicht „mehr als“
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}")["errors"] == []
    _disk(monkeypatch, _folder(mode_db), db_free=(100 * GB, 99 * GB - 1))      # ein Byte mehr: zu viel
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}") == _disk_abort(2, False)
    _disk(monkeypatch, _folder(mode_db), db_free=(100 * GB, 99 * GB - 1))
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_DISK_BYTES", 2 * GB)           # mit größerer Grenze läuft es
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}")["errors"] == []


def test_the_minimum_free_space_is_a_value_you_can_set(mode_db, monkeypatch):
    _disk(monkeypatch, _folder(mode_db), db_free=(10 * GB, 1 * GB))
    monkeypatch.setattr(handlers, "_CONSOLE_MIN_FREE_BYTES", 2 * GB)
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}") == _disk_abort(2, False)


@BOTH_MODES
def test_an_error_while_measuring_aborts_nothing(mode_db, monkeypatch, write):
    calls = _disk(monkeypatch, _folder(mode_db), fail=True)
    result = _run(mode_db, f"SELECT 1; {LONG_QUERY}", write)
    assert result["errors"] == [] and result["success_count"] == 2
    assert len(calls) > 2                                                      # es wurde versucht


def test_a_missing_temp_folder_aborts_nothing(mode_db, monkeypatch):
    def broken():
        raise FileNotFoundError("kein Temp-Ordner")

    monkeypatch.setattr(handlers, "tempfile", SimpleNamespace(gettempdir=broken))
    assert _run(mode_db, f"SELECT 1; {LONG_QUERY}")["errors"] == []


def test_the_disk_is_checked_every_hundredth_call_of_the_progress_handler_not_every_time(mode_db, monkeypatch):
    assert handlers._CONSOLE_DISK_CHECK_EVERY == 100
    clock = CountingClock()
    monkeypatch.setattr(handlers, "time", clock)
    calls = []
    real_usage = shutil.disk_usage
    monkeypatch.setattr(handlers, "shutil", SimpleNamespace(disk_usage=lambda folder: calls.append(folder) or real_usage(folder)))
    assert _run(mode_db, f"SELECT count(*) FROM {_numbers(1_000_000)}")["errors"] == []
    progress_calls = clock.calls - 2                      # Frist und die Prüfung vor Befehl 1 sind keine Aufrufe des Handlers
    checks = progress_calls // 100
    assert progress_calls >= 200 and checks >= 2
    assert len(calls) == 2 + 2 * checks                   # zu Beginn je Ordner einmal, dann je Prüfung je Ordner
    assert len(calls) < progress_calls                    # nicht bei jedem Aufruf


def test_the_disk_defaults_are_the_agreed_ones():
    assert handlers._CONSOLE_MIN_FREE_BYTES == 256_000_000
    assert handlers._CONSOLE_MAX_DISK_BYTES == 1_000_000_000


def test_a_short_request_never_asks_for_the_disk(mode_db, monkeypatch):
    calls = _disk(monkeypatch, _folder(mode_db), db_free=(100 * GB, 1))
    monkeypatch.setattr(handlers, "_CONSOLE_DISK_CHECK_EVERY", 100)
    assert _run(mode_db, "INSERT INTO Probe VALUES ('z', 3)", write=True)["errors"] == []
    assert len(calls) == 2                                # nur die Messung zu Beginn


# ── Teil 4: SQL-Protokoll ────────────────────────────────────────────────────
@pytest.fixture
def as_anna():
    """Der Test arbeitet als angemeldeter Benutzer ``anna`` einer eingerichteten Installation: Das Protokoll liegt in
    ``users/anna/``. Der Kontext wird nach dem Test vom conftest geleert."""
    userctx.set_mode("multi")
    userctx.set_user("anna")
    return "anna"


def test_only_the_writing_commands_of_a_committed_request_go_to_the_log(mode_db, as_anna):
    result = _run(mode_db, "SELECT A FROM Probe; INSERT INTO Probe VALUES ('z', 3); PRAGMA table_info(Probe); "
                           "CREATE TABLE Neu (A TEXT); EXPLAIN SELECT 1; UPDATE Probe SET B = 4 WHERE A = 'z'; "
                           "WITH n AS (SELECT 1) SELECT * FROM n", write=True)
    assert (result["success_count"], result["errors"]) == (7, [])
    assert _logged_commands() == ["INSERT INTO Probe VALUES ('z', 3);", "CREATE TABLE Neu (A TEXT);",
                                  "UPDATE Probe SET B = 4 WHERE A = 'z';"]
    detailed = _log_files()[1]
    assert detailed.count("Description: SQL-Konsole (anna)") == 3
    assert "SELECT" not in "".join(_logged_commands()) and "Parameters: ()" in detailed


def test_the_description_names_the_logged_in_user_or_none(mode_db):
    _run(mode_db, "INSERT INTO Probe VALUES ('z', 3)", write=True)              # ohne Benutzer-Kontext
    assert "Description: SQL-Konsole\n" in _log_files()[1]
    userctx.set_mode("multi")
    userctx.set_user("bernd")
    _run(mode_db, "INSERT INTO Probe VALUES ('q', 4)", write=True)
    assert "Description: SQL-Konsole (bernd)\n" in _log_files()[1]


@BOTH_MODES
def test_a_request_without_a_writing_command_writes_no_log(mode_db, as_anna, write):
    _run(mode_db, "SELECT * FROM Probe; PRAGMA table_info(Probe); PRAGMA defer_foreign_keys = ON", write)
    assert _log_files() == (None, None)


def test_reading_mode_never_logs(mode_db, as_anna):
    _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); SELECT 1")                 # lesend: das INSERT ist abgelehnt
    assert _log_files() == (None, None)


def test_an_undone_request_is_not_logged(mode_db, as_anna):
    _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); SELEKT 1", write=True)
    _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); PRAGMA user_version = 3", write=True)
    _run(mode_db, "PRAGMA defer_foreign_keys = ON; INSERT INTO Kind VALUES (1, 99)", write=True)    # COMMIT scheitert
    assert _log_files() == (None, None)


def test_the_log_is_written_after_the_commit(mode_db, as_anna, monkeypatch):
    """Beim ersten Eintrag steht die Änderung schon in der Datenbank – und die Verbindung der Konsole hält keine Sperre mehr."""
    seen = []
    real_log = handlers.DocumentParser.log_sql

    def watching(self, sql, parameters, description=""):
        with contextlib.closing(sqlite3.connect(mode_db.db_name, timeout=0.2, isolation_level=None)) as other:
            seen.append(other.execute("SELECT COUNT(*) FROM Probe WHERE B = 3").fetchone()[0])
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        return real_log(self, sql, parameters, description)

    monkeypatch.setattr(handlers.DocumentParser, "log_sql", watching)
    assert _run(mode_db, "INSERT INTO Probe VALUES ('z', 3)", write=True)["errors"] == []
    assert seen == [1]


def test_identical_commands_are_logged_each_time(mode_db, as_anna):
    """SQLite prüft einen zweiten, gleich lautenden Befehl nicht noch einmal gegen den Authorizer, wenn die Verbindung
    ihre Statements zwischenspeichert – die Konsole schaltet den Cache aus."""
    _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); INSERT INTO Probe VALUES ('z', 3); "
                  "INSERT INTO Probe VALUES ('z', 3)", write=True)
    assert _logged_commands() == ["INSERT INTO Probe VALUES ('z', 3);"] * 3


def test_a_log_failure_keeps_the_change_and_warns_without_the_error_text(mode_db, as_anna, monkeypatch, capsys):
    def failing(self, sql, parameters, description=""):
        raise OSError("Platte voll: /geheimer/pfad/sql_operations.sql")

    monkeypatch.setattr(handlers.DocumentParser, "log_sql", failing)
    result = _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); INSERT INTO Probe VALUES ('q', 4)", write=True)
    assert result == {"success_count": 2, "total": 2, "errors": [], "output": "", "rolled_back": False,
                      "warnings": [LOG_WARNING]}
    assert "geheimer" not in json.dumps(result) and "Platte" not in json.dumps(result)
    assert _query(mode_db, "SELECT COUNT(*) FROM Probe WHERE B > 2") == [(2,)]       # die Änderung steht
    assert "Platte voll" in capsys.readouterr().out                                    # der Fehler steht im Server-Log
    assert not handlers._console_lock.locked()


def test_a_log_failure_stops_logging_the_rest(mode_db, as_anna, monkeypatch):
    attempts = []

    def failing(self, sql, parameters, description=""):
        attempts.append(sql)
        raise OSError("kaputt")

    monkeypatch.setattr(handlers.DocumentParser, "log_sql", failing)
    _run(mode_db, "INSERT INTO Probe VALUES ('z', 3); INSERT INTO Probe VALUES ('q', 4)", write=True)
    assert len(attempts) == 1


def test_no_warning_appears_when_the_log_works_or_nothing_needs_logging(mode_db, as_anna):
    assert "warnings" not in _run(mode_db, "INSERT INTO Probe VALUES ('z', 3)", write=True)
    assert "warnings" not in _run(mode_db, "SELECT 1", write=True)
    assert "warnings" not in _run(mode_db, "SELECT 1")


COMMENTED = """-- Zeitbereich leeren; und noch ein Satz
/* Kopf; Block */
UPDATE Probe SET B = 9 -- setzen, nicht löschen
WHERE A = 'x'; -- danach
INSERT INTO Probe VALUES ('a -- b', 5) /* nicht; kürzen */ ;
INSERT INTO Probe VALUES ('c /* d */', 6);
CREATE TABLE "Ta--belle" ([Sp/*alte] TEXT);
CREATE TRIGGER t_neu AFTER INSERT ON "Ta--belle" BEGIN INSERT INTO Protokoll VALUES ('neu'); END;
INSERT INTO "Ta--belle" VALUES ('v')
"""


def test_the_logged_commands_carry_no_comments_but_keep_texts_and_names(mode_db, as_anna):
    result = _run(mode_db, COMMENTED, write=True)
    assert (result["errors"], result["success_count"]) == ([], 6)
    assert _logged_commands() == [
        "UPDATE Probe SET B = 9 WHERE A = 'x';",
        "INSERT INTO Probe VALUES ('a -- b', 5);",
        "INSERT INTO Probe VALUES ('c /* d */', 6);",
        'CREATE TABLE "Ta--belle" ([Sp/*alte] TEXT);',
        'CREATE TRIGGER t_neu AFTER INSERT ON "Ta--belle" BEGIN INSERT INTO Protokoll VALUES (\'neu\'); END;',
        'INSERT INTO "Ta--belle" VALUES (\'v\');',
    ]


def test_replaying_the_log_gives_the_same_database(mode_db, as_anna, tmp_path):
    """Das Protokoll ist ein Nachspiel-Protokoll: Auf einer Kopie des Standes von vorher ausgeführt, ergibt es den Stand
    nachher – mit Kommentaren und mehrzeiligen Befehlen im Eingabetext."""
    copy = tmp_path / "vorher.db"
    shutil.copyfile(mode_db.db_name, copy)
    assert _run(mode_db, COMMENTED, write=True)["errors"] == []

    with contextlib.closing(sqlite3.connect(copy)) as replay:
        replay.executescript("\n".join(_logged_commands()))
    assert installation.dump(str(copy)) == installation.dump(mode_db.db_name)


def test_the_log_text_helper_strips_comments_and_the_closing_semicolon_only():
    text = handlers._console_log_text
    assert text("INSERT INTO T VALUES (1);") == "INSERT INTO T VALUES (1)"
    assert text("  SELECT 1 ;;  ") == "SELECT 1"
    assert text("-- a\n/* b */ SELECT 'x -- y' /* z */ -- w") == "SELECT 'x -- y'"
    assert text("SELECT 'it''s -- ok'") == "SELECT 'it''s -- ok'"
    assert text('SELECT "a--b", [c/*d*/], `e--f`') == 'SELECT "a--b", [c/*d*/], `e--f`'
    assert text("SELECT 1 /* offen") == "SELECT 1"
    assert text("a/**/b") == "a b"


# ── Teil 5: eine Anfrage zur Zeit ────────────────────────────────────────────
def _parked(monkeypatch):
    """Hält die Anfrage, die gerade ein Ergebnis ausgibt, an, bis der Test sie freigibt. Liefert ``(started, release)``."""
    started, release = threading.Event(), threading.Event()
    real_cell = handlers._console_cell

    def parked(value):
        started.set()
        assert release.wait(30)
        return real_cell(value)

    monkeypatch.setattr(handlers, "_console_cell", parked)
    return started, release


def test_a_second_request_is_refused_at_once_while_one_runs_and_the_lock_is_free_afterwards(mode_db):
    first = []
    with pytest.MonkeyPatch.context() as patch:
        started, release = _parked(patch)
        thread = threading.Thread(target=lambda: first.append(_run(mode_db, "SELECT 1")))
        thread.start()
        try:
            assert started.wait(30)
            assert handlers._console_lock.locked()
            t = time.perf_counter()
            second = _run(mode_db, "SELECT 2")
            assert time.perf_counter() - t < 2                                   # sofort, ohne zu warten
            assert second == {"success_count": 0, "total": 1, "output": "", "errors": [BUSY]}
            writing = _run(mode_db, "INSERT INTO Probe VALUES ('z', 3)", write=True)
            assert writing == {"success_count": 0, "total": 1, "output": "", "errors": [BUSY]}
            assert _query(mode_db, "SELECT COUNT(*) FROM Probe") == [(2,)]       # nichts ausgeführt
        finally:
            release.set()
            thread.join(30)
    assert first == [{"success_count": 1, "total": 1, "errors": [], "output": "1\n1"}]
    assert not handlers._console_lock.locked()
    assert _run(mode_db, "SELECT 3")["output"] == "3\n3"


def test_http_a_second_request_gets_the_busy_answer_while_one_runs(server_port, auto_login):
    first = []
    with pytest.MonkeyPatch.context() as patch:
        started, release = _parked(patch)
        thread = threading.Thread(target=lambda: first.append(_post_sql(server_port, "SELECT 1", auto_login)))
        thread.start()
        try:
            assert started.wait(30)
            second = _post_sql(server_port, "SELECT 2", auto_login)
            assert second.status == 200
            assert json.loads(second.text) == {"success_count": 0, "total": 1, "output": "", "errors": [BUSY]}
        finally:
            release.set()
            thread.join(30)
    assert json.loads(first[0].text) == {"success_count": 1, "total": 1, "errors": [], "output": "1\n1"}
    assert json.loads(_post_sql(server_port, "SELECT 3", auto_login).text)["output"] == "3\n3"


def test_the_lock_is_free_after_a_refused_input(mode_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_STATEMENTS", 1)
    assert "zu viele Befehle" in _run(mode_db, "SELECT 1; SELECT 2")["errors"][0]
    assert not handlers._console_lock.locked()
    assert _run(mode_db, "SELECT 1")["errors"] == []


@pytest.mark.parametrize("write", [False, True], ids=["lesend", "schreibend"])
@pytest.mark.parametrize("scenario", ["Fehler", "Zeitlimit", "Platte", "unerwartet"])
def test_the_lock_is_free_after_an_error_a_timeout_a_full_disk_and_an_unexpected_exception(mode_db, scenario, write):
    sql = "SELECT 1; SELEKT 1"
    with pytest.MonkeyPatch.context() as patch:
        if scenario == "Zeitlimit":
            patch.setattr(handlers, "time", ScriptedClock(0, 0, 100))
            sql = "SELECT 1; SELECT 2"
        elif scenario == "Platte":
            _disk(patch, _folder(mode_db), db_free=(100 * GB, 1))
            sql = f"SELECT 1; {LONG_QUERY}"
        elif scenario == "unerwartet":
            def boom(cursor):
                raise RuntimeError("kaputt")

            patch.setattr(handlers, "_console_result_lines", boom)
        result = _run(mode_db, sql, write)
    assert result["errors"], result
    assert not handlers._console_lock.locked()
    assert _run(mode_db, "SELECT 1", write)["errors"] == []                       # die nächste Anfrage läuft


@pytest.mark.parametrize("write", [False, True], ids=["lesend", "schreibend"])
def test_an_unexpected_error_is_reported_and_undoes_the_request(mode_db, monkeypatch, write):
    def boom(cursor):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(handlers, "_console_result_lines", boom)
    before = _state(mode_db)
    result = _run(mode_db, "SELECT 1" if not write else "INSERT INTO Probe VALUES ('z', 3); SELECT 1", write)
    if write:
        assert result == {"success_count": 0, "total": 2, "output": "", "rolled_back": True,
                          "errors": ["kaputt", NOTHING_CHANGED]}
    else:
        assert result == {"success_count": 0, "total": 1, "output": "", "errors": ["kaputt"]}
    assert _state(mode_db) == before


# ── Teil 5: Fehlerliste ──────────────────────────────────────────────────────
def test_the_error_cap_default_is_100():
    assert handlers._CONSOLE_MAX_ERRORS == 100


@pytest.mark.parametrize("failing, length, last", [
    pytest.param(1, 1, 'Befehl 1: near "SELEKT": syntax error', id="einer"),
    pytest.param(100, 100, 'Befehl 100: near "SELEKT": syntax error', id="genau-100"),
    pytest.param(101, 101, "… 1 weitere Fehler", id="101"),
    pytest.param(150, 101, "… 50 weitere Fehler", id="150"),
    pytest.param(1000, 101, "… 900 weitere Fehler", id="1000"),
])
def test_the_error_list_is_capped_with_a_line_for_the_rest(mode_db, failing, length, last):
    result = _run(mode_db, "; ".join(["SELEKT 1"] * failing))
    assert (result["success_count"], result["total"]) == (0, failing)
    assert len(result["errors"]) == length and result["errors"][-1] == last
    assert result["errors"][0] == 'Befehl 1: near "SELEKT": syntax error'


def test_the_error_cap_counts_only_errors_and_keeps_the_successes(mode_db):
    commands = ["SELEKT 1" if i % 2 else "SELECT 1" for i in range(300)]
    result = _run(mode_db, "; ".join(commands))
    assert (result["success_count"], result["total"]) == (150, 300)
    assert len(result["errors"]) == 101 and result["errors"][-1] == "… 50 weitere Fehler"
    assert result["errors"][1] == 'Befehl 4: near "SELEKT": syntax error'


def test_the_error_cap_uses_the_value_in_force(mode_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_ERRORS", 3)
    result = _run(mode_db, "; ".join(["SELEKT 1"] * 5))
    assert result["errors"][:3] == [f'Befehl {n}: near "SELEKT": syntax error' for n in (1, 2, 3)]
    assert result["errors"][3:] == ["… 2 weitere Fehler"]


def test_writing_mode_never_has_more_than_the_error_and_the_sentence(mode_db):
    result = _run(mode_db, "; ".join(["SELEKT 1"] * 500), write=True)
    assert result["errors"] == ['Befehl 1: near "SELEKT": syntax error', NOTHING_CHANGED]


# ── Teil 6: HTTP ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value, writes", [
    pytest.param("1", True, id="1"),
    pytest.param(None, False, id="fehlt"),
    pytest.param("0", False, id="0"),
    pytest.param("", False, id="leer"),
    pytest.param("true", False, id="true"),
    pytest.param("on", False, id="on"),
    pytest.param("yes", False, id="yes"),
    pytest.param("ja", False, id="ja"),
    pytest.param("01", False, id="01"),
    pytest.param("11", False, id="11"),
    pytest.param(" 1", False, id="Leerzeichen-davor"),
    pytest.param("1 ", False, id="Leerzeichen-danach"),
    pytest.param("2", False, id="2"),
    pytest.param("-1", False, id="minus-1"),
    pytest.param("１", False, id="Ziffer-in-Vollbreite"),
])
def test_http_only_the_exact_value_1_switches_writing_on(server_port, auto_login, value, writes):
    form = {"sql_commands": "CREATE TABLE Probe (X TEXT)", **({} if value is None else {"write": value})}
    resp = post(server_port, "/execute_sql", form=form, csrf=auto_login.csrf)
    assert resp.status == 200
    body = json.loads(resp.text)
    tables = _query(auto_login.db, "SELECT name FROM sqlite_master WHERE name = 'Probe'")
    if writes:
        assert body == {"success_count": 1, "total": 1, "errors": [], "output": "", "rolled_back": False}
        assert tables == [("Probe",)]
    else:
        assert body == denied(False)
        assert tables == []


def test_http_write_mode_answers_with_rolled_back_and_logs_the_user(server_port, auto_login):
    resp = _post_sql(server_port, "CREATE TABLE Probe (X TEXT); INSERT INTO Probe VALUES ('a')", auto_login, write=True)
    assert json.loads(resp.text) == {"success_count": 2, "total": 2, "errors": [], "output": "", "rolled_back": False}
    log = os.path.join(userctx.DATA_ROOT, userctx.USERS_DIR, auto_login.name, "sql_operations.log")
    with open(log, encoding="utf-8") as handle:
        assert f"Description: SQL-Konsole ({auto_login.name})" in handle.read()

    failed = _post_sql(server_port, "INSERT INTO Probe VALUES ('b'); SELEKT 1", auto_login, write=True)
    assert json.loads(failed.text) == {"success_count": 0, "total": 2, "output": "", "rolled_back": True,
                                       "errors": ['Befehl 2: near "SELEKT": syntax error', NOTHING_CHANGED]}
    assert _query(auto_login.db, "SELECT X FROM Probe") == [("a",)]


def test_http_reading_mode_works_as_the_diagnosis_tool_uses_it(server_port, auto_login):
    """tools/mcp_ubuchhaltung.py schickt nur ``sql_commands`` (ein SELECT, nie ``write``) – es läuft im Lesemodus."""
    resp = post(server_port, "/execute_sql", form={"sql_commands": "SELECT * FROM (\nSELECT 1 AS a\n) LIMIT 0"},
                csrf=auto_login.csrf)
    assert json.loads(resp.text) == {"success_count": 1, "total": 1, "errors": [], "output": "a"}
