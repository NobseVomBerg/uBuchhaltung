# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""SQL-Konsole: Grenzen je Anfrage (``handle_execute_sql``).

Eine endlose rekursive CTE hielte sonst die Sperre auf der Datenbank und einen Thread bis zum
Neustart; ein riesiger Wert oder eine riesige Ausgabe könnte den Prozess per Speichermangel
beenden. Deshalb gelten je Anfrage:

  1. ein Zeitlimit für alle Befehle zusammen, auch beim Lesen der Ergebnisse: Fortschrittshandler
     alle 10000 Schritte plus Prüfung vor jedem Befehl. Bei Überschreitung wird zurückgerollt
     (``success_count`` 0, genau ein Fehler, keine Ausgabe), danach hält nichts mehr eine Sperre.
     Ein Fehlertext mit „interrupted“ gilt nur bei wirklich abgelaufener Frist als Abbruch,
  2. Größengrenzen für einzelne Werte (``SQLITE_LIMIT_LENGTH``) und Spalten
     (``SQLITE_LIMIT_COLUMN``), beide nur mit ``Connection.setlimit`` (ab Python 3.11),
  3. Ausgabegrenzen für Zeilen (Kopfzeilen zählen mit) und UTF-8-Bytes über alle SELECTs der Anfrage;
     danach wird nicht weiter gelesen und eine Hinweiszeile angehängt, die Befehle laufen und
     werden festgeschrieben.

Die Grenzen sind Modulkonstanten, die ``handle_execute_sql`` bei jedem Aufruf liest – die Tests
setzen sie klein. Zeitabläufe laufen über künstliche Uhren (``handlers.time`` wird durch ein
Objekt mit ``monotonic()`` ersetzt); nur der Durchlauf mit echter Uhr und der HTTP-Test warten
ein Limit von einer Sekunde ab. „Endlos“ heißt hier 100 Millionen Zeilen: Ohne Zeitlimit liefe
das rund zehn Sekunden – ein kaputtes Limit lässt einen Test scheitern, statt die Suite aufzuhalten.
"""
import contextlib
import json
import sqlite3
import time
import tracemalloc
from types import SimpleNamespace

import pytest

from server import handlers
from server.pages_miscellaneous import PageMiscellaneous
from tests.http_client import get
from tests.test_sql_console import _post_sql, _query, _run, probe_db  # noqa: F401 (probe_db: Fixture)

NOTE_START = "… Ausgabe gekürzt (mehr als "
requires_setlimit = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setlimit"),
    reason="sqlite3.Connection.setlimit gibt es erst ab Python 3.11")


# ── Hilfen ───────────────────────────────────────────────────────────────────
def _numbers(n):
    """Die Zahlen 1..n als Unterabfrage (rekursive CTE)."""
    return f"(WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT {n}) SELECT x FROM c)"


ENDLESS = 100_000_000
ENDLESS_CTE = (f"WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT {ENDLESS}) "
               "SELECT count(*) FROM c")
# Ein Ergebnis, das nur eine Grenze beenden kann – aber begrenzt: Wer es ganz einliest (Fehler in der
# Ausgabegrenze), braucht dafür einige hundert MB, nicht Gigabytes wie bei 100 Millionen Zeilen.
MANY_ROWS = 5_000_000


def _timeout_error(i, limit):
    return (f"Befehl {i}: abgebrochen – Zeitlimit von {limit} s überschritten. "
            "Nicht festgeschriebene Änderungen dieser Anfrage wurden zurückgenommen. "
            "Was schon festgeschrieben war, bleibt bestehen: Befehle vor einem COMMIT und "
            "Strukturbefehle (CREATE, ALTER, DROP) vor der ersten Datenänderung.")


def _timeout_result(i, total, limit):
    return {"success_count": 0, "total": total, "errors": [_timeout_error(i, limit)], "output": ""}


class ScriptedClock:
    """Ersatz für ``handlers.time``: ``monotonic()`` liefert nacheinander die vorgegebenen Werte,
    danach immer den letzten. Reihenfolge der Aufrufe in ``handle_execute_sql``: Start (Frist =
    Wert + Limit), vor jedem Befehl, dann je Fortschrittshandler-Aufruf."""

    def __init__(self, *ticks):
        self.ticks = ticks
        self.calls = 0

    def monotonic(self):
        tick = self.ticks[min(self.calls, len(self.ticks) - 1)]
        self.calls += 1
        return tick


class StepClock:
    """Ersatz für ``handlers.time``: jeder Aufruf von ``monotonic()`` liegt eine Sekunde nach dem
    vorigen (0, 1, 2 …). Mit Limit 20 bricht der Fortschrittshandler nach rund 20 Aufrufen ab –
    das sind ein paar hunderttausend Schritte, nicht 20 echte Sekunden."""

    def __init__(self):
        self.calls = 0

    def monotonic(self):
        tick = float(self.calls)
        self.calls += 1
        return tick


@pytest.fixture
def step_clock(monkeypatch):
    """StepClock mit Zeitlimit 20 (reicht für einige Befehle, bevor der Fortschrittshandler zuschlägt)."""
    clock = StepClock()
    monkeypatch.setattr(handlers, "time", clock)
    monkeypatch.setattr(handlers, "_CONSOLE_TIME_LIMIT_S", 20)
    return clock


def _assert_not_locked(db):
    """Nichts hält mehr eine Sperre: Ein zweiter Zugriff bekommt sofort die Exklusivsperre
    (ein Leser, ein offener Schreibvorgang oder eine liegengebliebene Anweisung ließen sie scheitern)."""
    with contextlib.closing(sqlite3.connect(db.db_name, timeout=0.2, isolation_level=None)) as con:
        con.execute("BEGIN EXCLUSIVE")
        con.execute("ROLLBACK")


PROBE_ROWS = [("x", 1), ("y", 2)]


def _probe(db):
    return _query(db, "SELECT A, B FROM Probe ORDER BY B")


@pytest.fixture
def plain_db(tmp_path):
    """Eine schlichte SQLite-Datei mit der Tabelle Probe statt der App-Datenbank. Die Wert- und die
    Spaltengrenze gelten auch beim Einlesen des Schemas: Mit klein gesetzter Grenze ließe sich das
    Schema der App (Invoices: 50 Spalten, 3 kB CREATE-Anweisung) nicht mehr laden, und jeder Befehl
    scheiterte schon daran – der Test bewiese dann nichts über den einzelnen Wert."""
    path = str(tmp_path / "schlicht.db")
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE Probe (A TEXT, B INTEGER)")
        con.execute("INSERT INTO Probe VALUES ('x', 1), ('y', 2)")
        con.commit()
    return SimpleNamespace(db_name=path)


# ── Teil 1: Zeitlimit ────────────────────────────────────────────────────────
def test_endless_cte_after_an_insert_is_aborted_in_time_and_rolled_back(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_TIME_LIMIT_S", 1)
    start = time.perf_counter()
    result = _run(probe_db, f"INSERT INTO Probe VALUES ('z', 3); {ENDLESS_CTE}")
    elapsed = time.perf_counter() - start

    assert result == _timeout_result(2, 2, 1)
    assert elapsed < 3
    assert _probe(probe_db) == PROBE_ROWS                  # das INSERT ist nicht gespeichert
    with contextlib.closing(sqlite3.connect(probe_db.db_name, timeout=0.2)) as con:
        con.execute("INSERT INTO Probe VALUES ('n', 4)")   # eine zweite Verbindung schreibt sofort
        con.commit()
    assert _probe(probe_db) == PROBE_ROWS + [("n", 4)]


ENDLESS_STATEMENTS = {
    "CTE": ENDLESS_CTE,
    "Unterabfrage": f"SELECT (SELECT count(*) FROM {_numbers(ENDLESS)}) AS n",
    "Unterabfrage-in-WHERE": f"SELECT A FROM Probe WHERE B < (SELECT count(*) FROM {_numbers(ENDLESS)})",
    "UPDATE": f"UPDATE Probe SET B = (SELECT count(*) FROM {_numbers(ENDLESS)})",
    "DELETE": f"DELETE FROM Probe WHERE B < (SELECT count(*) FROM {_numbers(ENDLESS)})",
    # begrenzt, damit ein kaputtes Limit hier nicht eine riesige Tabelle schreibt
    "INSERT-SELECT": f"INSERT INTO Probe SELECT 'q', x FROM {_numbers(3_000_000)}",
    "Lesen-des-Ergebnisses": f"SELECT x FROM {_numbers(3_000_000)}",
}


@pytest.mark.parametrize("endless", ENDLESS_STATEMENTS.values(), ids=ENDLESS_STATEMENTS.keys())
def test_endless_statement_is_aborted_by_the_time_limit(probe_db, monkeypatch, step_clock, endless):
    # Ausgabegrenzen aus dem Weg: Hier kann nur das Zeitlimit die Anfrage beenden
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 10**9)
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_BYTES", 10**12)
    result = _run(probe_db, f"INSERT INTO Probe VALUES ('z', 3); {endless}")
    assert result == _timeout_result(2, 2, 20)
    assert _probe(probe_db) == PROBE_ROWS
    _assert_not_locked(probe_db)


def test_the_abort_can_hit_while_the_result_is_being_read(probe_db, monkeypatch, step_clock):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 10**9)
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_BYTES", 10**12)
    cells = []
    real_cell = handlers._console_cell
    monkeypatch.setattr(handlers, "_console_cell", lambda value: cells.append(value) or real_cell(value))

    result = _run(probe_db, f"SELECT x FROM {_numbers(3_000_000)}")
    assert result == _timeout_result(1, 1, 20)
    assert 0 < len(cells) < 3_000_000          # Zeilen wurden gelesen, aber nicht alle
    _assert_not_locked(probe_db)


def test_deadline_passed_before_the_next_command_stops_before_running_it(probe_db, monkeypatch):
    # Uhr: Start, vor Befehl 1, vor Befehl 2 – dort ist die Frist (0 + 60) vorbei
    monkeypatch.setattr(handlers, "time", ScriptedClock(0, 0, 100))
    result = _run(probe_db, "CREATE TABLE Eins (X TEXT); CREATE TABLE Zwei (X TEXT); "
                            "CREATE TABLE Drei (X TEXT)")
    assert result == _timeout_result(2, 3, 60)
    names = _query(probe_db, "SELECT name FROM sqlite_master WHERE name IN ('Eins', 'Zwei', 'Drei')")
    assert names == [("Eins",)]                # Strukturbefehl vor dem Abbruch steht; Befehl 2 lief nie


def test_deadline_passed_between_two_commands_rolls_back_the_first(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "time", ScriptedClock(0, 0, 100))
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3); INSERT INTO Probe VALUES ('q', 4)")
    assert result == _timeout_result(2, 2, 60)
    assert _probe(probe_db) == PROBE_ROWS


def test_deadline_passed_before_the_first_command_runs_nothing(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "time", ScriptedClock(0, 100))
    result = _run(probe_db, "CREATE TABLE Eins (X TEXT); INSERT INTO Probe VALUES ('z', 3)")
    assert result == _timeout_result(1, 2, 60)
    assert _query(probe_db, "SELECT name FROM sqlite_master WHERE name = 'Eins'") == []
    assert _probe(probe_db) == PROBE_ROWS


def test_reaching_the_deadline_exactly_is_not_exceeding_it(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "time", ScriptedClock(0, 0, 60))
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3); INSERT INTO Probe VALUES ('q', 4)")
    assert result == {"success_count": 2, "total": 2, "errors": [], "output": ""}
    assert _probe(probe_db) == PROBE_ROWS + [("z", 3), ("q", 4)]


def test_the_message_names_the_limit_in_force(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "time", ScriptedClock(0, 100))
    monkeypatch.setattr(handlers, "_CONSOLE_TIME_LIMIT_S", 7)
    assert _run(probe_db, "SELECT 1")["errors"] == [_timeout_error(1, 7)]


# Die Meldung verspricht nur, was stimmt: zurückgenommen wird die offene Transaktion – Python
# beginnt sie erst vor dem ersten INSERT/UPDATE/DELETE. DDL davor und alles vor einem COMMIT steht schon.
def test_structure_commands_before_the_first_data_change_stay(probe_db, step_clock):
    result = _run(probe_db, f"CREATE TABLE Neu (X TEXT); {ENDLESS_CTE}")
    assert result == _timeout_result(2, 2, 20)
    assert _query(probe_db, "SELECT name FROM sqlite_master WHERE name = 'Neu'") == [("Neu",)]


def test_structure_commands_after_a_data_change_are_rolled_back_with_it(probe_db, step_clock):
    result = _run(probe_db, "CREATE TABLE zz1 (x); INSERT INTO zz1 VALUES (1); CREATE TABLE zz2 (x); "
                            f"{ENDLESS_CTE}")
    assert result == _timeout_result(4, 4, 20)
    assert _query(probe_db, "SELECT name FROM sqlite_master WHERE name IN ('zz1', 'zz2') ORDER BY name") \
        == [("zz1",)]                                      # zz1 steht schon, zz2 war Teil der Transaktion
    assert _query(probe_db, "SELECT COUNT(*) FROM zz1") == [(0,)]


def test_data_changes_before_a_commit_stay_and_the_ones_after_it_go(probe_db, step_clock):
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3); COMMIT; "
                            f"INSERT INTO Probe VALUES ('q', 4); {ENDLESS_CTE}")
    assert result == _timeout_result(4, 4, 20)
    assert _probe(probe_db) == PROBE_ROWS + [("z", 3)]


def test_an_explicit_transaction_is_rolled_back_as_a_whole(probe_db, step_clock):
    result = _run(probe_db, "BEGIN; INSERT INTO Probe VALUES ('z', 3); CREATE TABLE zz2 (x); "
                            f"{ENDLESS_CTE}")
    assert result == _timeout_result(4, 4, 20)
    assert _probe(probe_db) == PROBE_ROWS
    assert _query(probe_db, "SELECT name FROM sqlite_master WHERE name = 'zz2'") == []
    _assert_not_locked(probe_db)


def test_a_transaction_opened_by_a_savepoint_is_rolled_back_too(probe_db, step_clock):
    result = _run(probe_db, "SAVEPOINT a; INSERT INTO Probe VALUES ('z', 3); CREATE TABLE zz2 (x); "
                            f"{ENDLESS_CTE}")
    assert result == _timeout_result(4, 4, 20)
    assert _probe(probe_db) == PROBE_ROWS
    assert _query(probe_db, "SELECT name FROM sqlite_master WHERE name = 'zz2'") == []
    _assert_not_locked(probe_db)


def test_commands_after_the_abort_do_not_run(probe_db, step_clock):
    result = _run(probe_db, f"{ENDLESS_CTE}; CREATE TABLE Danach (X TEXT); INSERT INTO Probe VALUES ('z', 3)")
    assert result == _timeout_result(1, 3, 20)
    assert _query(probe_db, "SELECT name FROM sqlite_master WHERE name = 'Danach'") == []
    assert _probe(probe_db) == PROBE_ROWS


# ── Teil 1: ohne Überschreitung bleibt alles wie vorher ──────────────────────
def test_without_overrun_commands_run_and_commit_and_errors_are_per_command(probe_db):
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3); SELEKT 1; "
                            "INSERT INTO Probe VALUES ('q', 4); SELECT A FROM Probe WHERE B > 2 ORDER BY B")
    assert result == {"success_count": 3, "total": 4, "output": "A\nz\nq",
                      "errors": ['Befehl 2: near "SELEKT": syntax error']}
    assert _probe(probe_db) == PROBE_ROWS + [("z", 3), ("q", 4)]


def test_an_error_raised_while_reading_a_row_is_a_normal_error_not_a_timeout(probe_db):
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3); "
                            "SELECT abs(x) FROM (SELECT 1 AS x UNION ALL SELECT -9223372036854775808); "
                            "INSERT INTO Probe VALUES ('q', 4)")
    assert result["errors"] == ["Befehl 2: integer overflow"]
    assert _probe(probe_db) == PROBE_ROWS + [("z", 3), ("q", 4)]


@pytest.mark.parametrize("failing", [
    pytest.param("SELECT * FROM interrupted", id="Tabelle"),
    pytest.param("SELECT interrupted FROM Probe", id="Spalte"),
    pytest.param("SELECT interrupted(1)", id="Funktion"),
])
def test_an_error_text_with_interrupted_is_no_timeout_while_the_deadline_has_not_passed(probe_db, failing):
    result = _run(probe_db, f"CREATE TABLE zz3 (x); INSERT INTO zz3 VALUES (1); {failing}; SELECT 2")
    assert result["success_count"] == 3
    assert result["total"] == 4
    assert len(result["errors"]) == 1
    assert result["errors"][0].startswith("Befehl 3: ")
    assert "interrupted" in result["errors"][0] and "Zeitlimit" not in result["errors"][0]
    assert result["output"] == "2\n2"
    assert _query(probe_db, "SELECT x FROM zz3") == [(1,)]            # nichts zurückgerollt


def test_the_table_called_interrupted_is_simply_missing(probe_db):
    assert _run(probe_db, "SELECT * FROM interrupted")["errors"] == ["Befehl 1: no such table: interrupted"]


def test_the_console_page_names_the_time_limit_in_force(probe_db):
    """Der Hinweis über dem Eingabefeld darf nicht von der Grenze abweichen, die tatsächlich gilt."""
    html = PageMiscellaneous(probe_db)
    assert 'id="sql_input"' in html
    assert f"läuft höchstens {handlers._CONSOLE_TIME_LIMIT_S} Sekunden" in html


# ── Teil 1: HTTP ─────────────────────────────────────────────────────────────
def test_http_endless_cte_returns_the_error_and_the_server_keeps_answering(server_port, auto_login,
                                                                           monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_TIME_LIMIT_S", 1)
    db = auto_login.db
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute("CREATE TABLE Probe (A TEXT, B INTEGER)")
        con.commit()

    start = time.perf_counter()
    resp = _post_sql(server_port, f"INSERT INTO Probe VALUES ('z', 3); {ENDLESS_CTE}", auto_login)
    assert time.perf_counter() - start < 5
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    assert json.loads(resp.text) == _timeout_result(2, 2, 1)
    assert _query(db, "SELECT COUNT(*) FROM Probe") == [(0,)]

    resp = _post_sql(server_port, "INSERT INTO Probe VALUES ('n', 4); SELECT A FROM Probe", auto_login)
    assert resp.status == 200                                # zweite Anfrage: keine Sperre, normale Antwort
    assert json.loads(resp.text) == {"success_count": 2, "total": 2, "errors": [], "output": "A\nn"}
    assert get(server_port, "/miscellaneous").status == 200


# ── Teil 2: Größe einzelner Werte ────────────────────────────────────────────
@requires_setlimit
@pytest.mark.parametrize("sql", [
    pytest.param("SELECT zeroblob(2000)", id="zeroblob"),
    pytest.param("SELECT length(zeroblob(2000))", id="length-zeroblob"),
    pytest.param("SELECT length(randomblob(2000))", id="randomblob"),
    pytest.param("SELECT hex(randomblob(600))", id="hex-randomblob"),
    pytest.param("SELECT length(hex(zeroblob(600)))", id="hex-zeroblob"),
    pytest.param(f"SELECT group_concat(hex(zeroblob(300)), ',') FROM {_numbers(3)}", id="group_concat"),
    pytest.param("INSERT INTO Probe VALUES (hex(randomblob(600)), 9)", id="INSERT"),
])
def test_value_over_the_limit_is_an_error_for_that_command(plain_db, monkeypatch, sql):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_VALUE_BYTES", 1000)
    result = _run(plain_db, f"{sql}; SELECT 1")
    assert result == {"success_count": 1, "total": 2, "output": "1\n1",     # nur das SELECT 1
                      "errors": ["Befehl 1: string or blob too big"]}
    assert _probe(plain_db) == PROBE_ROWS

    monkeypatch.setattr(handlers, "_CONSOLE_MAX_VALUE_BYTES", 100_000)  # Kontrolle: mit mehr Platz läuft es
    assert _run(plain_db, sql)["errors"] == []


@requires_setlimit
@pytest.mark.parametrize("sql, length", [
    pytest.param("SELECT length(zeroblob(1000))", 1000, id="zeroblob-genau-an-der-Grenze"),
    pytest.param("SELECT length(randomblob(1000))", 1000, id="randomblob-genau-an-der-Grenze"),
    pytest.param("SELECT length(hex(randomblob(400)))", 800, id="hex-knapp-darunter"),
    pytest.param("SELECT length(group_concat(hex(zeroblob(100)), ',')) "
                 f"FROM {_numbers(4)}", 4 * 200 + 3, id="group_concat-knapp-darunter"),
])
def test_value_up_to_the_limit_still_works(plain_db, monkeypatch, sql, length):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_VALUE_BYTES", 1000)
    result = _run(plain_db, sql)
    assert result["errors"] == []
    assert result["output"].split("\n")[1] == str(length)


@requires_setlimit
def test_one_byte_over_the_limit_fails(plain_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_VALUE_BYTES", 1000)
    for sql in ("SELECT length(zeroblob(1001))", "SELECT length(randomblob(1001))"):
        result = _run(plain_db, sql)
        assert (result["success_count"], result["errors"]) == (0, ["Befehl 1: string or blob too big"]), sql


@requires_setlimit
def test_stored_value_over_the_limit_cannot_be_read_but_does_not_stop_the_others(plain_db, monkeypatch):
    with contextlib.closing(sqlite3.connect(plain_db.db_name)) as con:
        con.execute("INSERT INTO Probe VALUES (?, 3)", ("g" * 5000,))
        con.commit()
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_VALUE_BYTES", 1000)
    result = _run(plain_db, "SELECT A FROM Probe WHERE B = 3; SELECT A FROM Probe WHERE B < 3 ORDER BY B; "
                            "SELECT B FROM Probe WHERE B = 3")
    # die kleine Spalte derselben Zeile (Befehl 3) lässt sich lesen
    assert result == {"success_count": 2, "total": 3, "output": "A\nx\ny\nB\n3",
                      "errors": ["Befehl 1: string or blob too big"]}


@requires_setlimit
def test_the_production_value_limit_stops_a_gigabyte_blob_quickly(probe_db):
    limit = handlers._CONSOLE_MAX_VALUE_BYTES
    start = time.perf_counter()
    for sql in ("SELECT zeroblob(1000000000)", "SELECT length(zeroblob(1000000000))",
                f"SELECT length(zeroblob({limit + 1}))", f"SELECT hex(randomblob({limit}))"):
        result = _run(probe_db, sql)
        assert result["success_count"] == 0 and "too big" in result["errors"][0], sql
        assert result["output"] == "", sql
    assert time.perf_counter() - start < 3

    fits = _run(probe_db, f"SELECT length(zeroblob({limit})) AS n")        # genau an der Grenze geht
    assert (fits["errors"], fits["output"]) == ([], f"n\n{limit}")


# ── Teil 2: Spalten ──────────────────────────────────────────────────────────
def _select_columns(n):
    return "SELECT " + ", ".join(f"{i} AS c{i}" for i in range(n))


def _create_table_columns(name, n):
    return f"CREATE TABLE {name} (" + ", ".join(f"c{i} INTEGER" for i in range(n)) + ")"


@requires_setlimit
@pytest.mark.parametrize("limit", [None, 8], ids=["Grenze-der-App", "klein-gesetzt"])
def test_result_with_more_columns_than_the_limit_is_an_error(plain_db, monkeypatch, limit):
    if limit is not None:
        monkeypatch.setattr(handlers, "_CONSOLE_MAX_COLUMNS", limit)
    limit = handlers._CONSOLE_MAX_COLUMNS

    fits = _run(plain_db, _select_columns(limit))
    assert fits["errors"] == []
    assert fits["output"].split("\n")[0].split(";") == [f"c{i}" for i in range(limit)]

    result = _run(plain_db, f"{_select_columns(limit + 1)}; SELECT 1")
    assert result == {"success_count": 1, "total": 2, "output": "1\n1",
                      "errors": ["Befehl 1: too many columns in result set"]}


@requires_setlimit
@pytest.mark.parametrize("limit", [None, 8], ids=["Grenze-der-App", "klein-gesetzt"])
def test_table_with_more_columns_than_the_limit_cannot_be_created_or_extended(plain_db, monkeypatch, limit):
    if limit is not None:
        monkeypatch.setattr(handlers, "_CONSOLE_MAX_COLUMNS", limit)
    limit = handlers._CONSOLE_MAX_COLUMNS

    result = _run(plain_db, f"{_create_table_columns('Breit', limit)}; "
                            f"{_create_table_columns('Zubreit', limit + 1)}; "
                            "ALTER TABLE Breit ADD COLUMN extra INTEGER")
    assert result["success_count"] == 1
    assert [e.split(":")[0] for e in result["errors"]] == ["Befehl 2", "Befehl 3"]
    assert result["errors"][0] == "Befehl 2: too many columns on Zubreit"
    assert result["errors"][1].startswith("Befehl 3: too many columns on ")
    names = _query(plain_db, "SELECT name FROM sqlite_master WHERE name IN ('Breit', 'Zubreit')")
    assert names == [("Breit",)]
    assert len(_query(plain_db, "PRAGMA table_info(Breit)")) == limit


@requires_setlimit
def test_a_row_holds_at_most_value_limit_times_column_limit(plain_db, monkeypatch):
    """Python holt eine Ergebniszeile ganz, bevor die Ausgabegrenze prüfen kann: Erst Wert- ×
    Spaltengrenze begrenzt, was eine einzelne Zeile an Speicher belegt."""
    # Mindestens 5 Spalten: so breit ist sqlite_master, dessen Zeilen SQLite beim Einlesen des
    # Schemas gegen dieselbe Grenze prüft.
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_VALUE_BYTES", 2001)
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_COLUMNS", 6)
    cell = "hex(zeroblob(1000))"                           # 2000 Zeichen, die Zuordnung braucht 2001

    widest = _run(plain_db, "SELECT " + ", ".join([cell] * 6))
    assert widest["errors"] == []
    assert len(widest["output"].split("\n")[1]) == 6 * 2000 + 5        # sechs Werte, fünf Semikolons

    too_many = _run(plain_db, "SELECT " + ", ".join([cell] * 7))
    assert too_many["errors"] == ["Befehl 1: too many columns in result set"]
    too_big = _run(plain_db, "SELECT " + ", ".join(["hex(zeroblob(1001))"] + [cell] * 5))
    assert too_big["errors"] == ["Befehl 1: string or blob too big"]


def test_every_table_of_the_app_can_be_read_in_full_width(probe_db):
    """Die Grenzen dürfen die Konsole für die App-Tabellen nicht unbrauchbar machen."""
    tables = [name for (name,) in _query(probe_db, "SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert "Bookings" in tables and "Invoices" in tables
    failed = {}
    for table in tables:
        result = _run(probe_db, f'SELECT * FROM "{table}" LIMIT 1')
        if result["errors"]:
            failed[table] = result["errors"]
    assert failed == {}


def test_the_limits_are_not_loosened():
    assert 0 < handlers._CONSOLE_TIME_LIMIT_S <= 60
    assert 0 < handlers._CONSOLE_MAX_VALUE_BYTES <= 2_000_000
    assert 0 < handlers._CONSOLE_MAX_COLUMNS <= 250
    assert 0 < handlers._CONSOLE_MAX_OUTPUT_ROWS <= 200_000
    assert 0 < handlers._CONSOLE_MAX_OUTPUT_BYTES <= 20_000_000


# ── Teil 3: Ausgabegrenzen ───────────────────────────────────────────────────
def test_exactly_the_row_limit_is_not_truncated_one_more_row_is(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 5)
    full = _run(probe_db, f"SELECT x FROM {_numbers(4)}")           # Kopf + 4 Zeilen = 5 Zeilen
    assert full["output"] == "x\n1\n2\n3\n4"
    assert full["output"].count(NOTE_START) == 0

    cut = _run(probe_db, f"SELECT x FROM {_numbers(5)}")            # Kopf + 5 Zeilen = 6 Zeilen
    lines = cut["output"].split("\n")
    assert lines[:5] == ["x", "1", "2", "3", "4"]
    assert lines[5] == "… Ausgabe gekürzt (mehr als 5 Zeilen bzw. 20 MB) – bitte mit WHERE oder LIMIT eingrenzen"
    assert len(lines) == 6
    assert (cut["success_count"], cut["total"], cut["errors"]) == (1, 1, [])


def test_the_row_limit_counts_headers_and_spans_several_selects(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 5)
    result = _run(probe_db, "SELECT 1; SELECT 2; SELECT 3")
    lines = result["output"].split("\n")
    assert lines[:5] == ["1", "1", "2", "2", "3"]     # Kopf des dritten passt, seine Zeile nicht mehr
    assert lines[5].startswith(NOTE_START) and len(lines) == 6
    assert (result["success_count"], result["total"]) == (3, 3)


# Zeilen 'x' (Kopf), 'ä' (2 Bytes), '€' (3), '😀' (4) – dazu je ein Zeilenumbruch: 2, 3, 4, 5 Bytes
MULTIBYTE = "SELECT x FROM (SELECT 'ä' AS x UNION ALL SELECT '€' UNION ALL SELECT '😀')"


@pytest.mark.parametrize("limit, kept", [
    pytest.param(14, ["x", "ä", "€", "😀"], id="genau-die-Summe"),
    pytest.param(13, ["x", "ä", "€"], id="ein-Byte-zu-wenig"),
    pytest.param(9, ["x", "ä", "€"], id="bis-zum-Euro-Zeichen"),
    pytest.param(8, ["x", "ä"], id="ein-Byte-weniger-als-bis-zum-Euro-Zeichen"),
    pytest.param(4, ["x"], id="nur-der-Kopf"),
    pytest.param(1, [], id="nicht-mal-der-Kopf"),
])
def test_the_byte_limit_counts_utf8_bytes_and_newlines_exactly(probe_db, monkeypatch, limit, kept):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_BYTES", limit)
    result = _run(probe_db, MULTIBYTE)
    lines = result["output"].split("\n") if result["output"] else []
    if kept == ["x", "ä", "€", "😀"]:
        assert lines == kept                                        # nichts gekürzt, keine Hinweiszeile
    else:
        assert lines[:-1] == kept
        assert lines[-1] == (f"… Ausgabe gekürzt (mehr als 200000 Zeilen bzw. {limit} Bytes) "
                             "– bitte mit WHERE oder LIMIT eingrenzen")
    assert (result["success_count"], result["errors"]) == (1, [])


def test_the_byte_limit_spans_several_selects(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_BYTES", 6)    # „1\n1\n2\n“ = 6 Bytes
    result = _run(probe_db, "SELECT 1; SELECT 2; SELECT 3")
    lines = result["output"].split("\n")
    assert lines[:3] == ["1", "1", "2"]
    assert lines[3].startswith(NOTE_START) and len(lines) == 4


@pytest.mark.parametrize("max_bytes, size_text", [
    pytest.param(20_000_000, "20 MB", id="Vorgabe"),
    pytest.param(3_000_000, "3 MB", id="volle-Megabyte"),
    pytest.param(1_500_000, "1500000 Bytes", id="kein-Vielfaches"),
    pytest.param(2000, "2000 Bytes", id="klein"),
])
def test_the_note_names_the_byte_limit_as_mb_or_bytes(probe_db, monkeypatch, max_bytes, size_text):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 3)
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_BYTES", max_bytes)
    result = _run(probe_db, f"SELECT x FROM {_numbers(10)}")
    assert result["output"].split("\n")[-1] == (f"… Ausgabe gekürzt (mehr als 3 Zeilen bzw. {size_text}) "
                                                "– bitte mit WHERE oder LIMIT eingrenzen")


def test_the_note_comes_exactly_once_and_last(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 4)
    result = _run(probe_db, f"SELECT x FROM {_numbers(10)}; SELECT x FROM {_numbers(10)}; SELECT 7")
    lines = result["output"].split("\n")
    assert result["output"].count("Ausgabe gekürzt") == 1
    assert lines[-1].startswith(NOTE_START)
    assert sum(line.startswith(NOTE_START) for line in lines) == 1
    assert lines[:-1] == ["x", "1", "2", "3"]                       # das zweite SELECT liefert nichts mehr


def test_commands_after_the_truncation_still_run_and_are_committed(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 5)
    result = _run(probe_db, f"SELECT x FROM {_numbers(100)}; INSERT INTO Probe VALUES ('n', 9); "
                            "SELECT 'spaet' AS s; SELEKT 1; DELETE FROM Probe WHERE A = 'x'")
    assert result["success_count"] == 4
    assert result["total"] == 5
    assert result["errors"] == ['Befehl 4: near "SELEKT": syntax error']
    assert "spaet" not in result["output"]                          # später Ausgabe wird nicht mehr gelesen
    assert result["output"].split("\n")[-1].startswith(NOTE_START)
    assert _probe(probe_db) == [("y", 2), ("n", 9)]


def test_a_later_select_that_would_still_fit_adds_nothing_after_the_cut(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_BYTES", 10)
    result = _run(probe_db, "SELECT 'aaaaaaaaaaaaaaaaaaaa' AS h; SELECT 7 AS n")
    lines = result["output"].split("\n")
    assert lines[0] == "h"                           # der Kopf passt (2 Bytes), die Zeile (21) nicht
    assert lines[1].startswith(NOTE_START) and len(lines) == 2      # „n“ und „7“ hätten noch hineingepasst
    assert (result["success_count"], result["errors"]) == (2, [])


def test_the_header_alone_is_cut_when_nothing_fits(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 0)
    result = _run(probe_db, "SELECT 1 AS a; INSERT INTO Probe VALUES ('n', 9)")
    assert result["output"].startswith(NOTE_START) and "\n" not in result["output"]
    assert (result["success_count"], result["errors"]) == (2, [])
    assert ("n", 9) in _probe(probe_db)


def test_a_huge_select_is_ended_by_the_row_limit_without_a_timeout(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 1000)
    start = time.perf_counter()
    result = _run(probe_db, f"SELECT x FROM {_numbers(MANY_ROWS)}")
    assert time.perf_counter() - start < 3
    lines = result["output"].split("\n")
    assert (result["success_count"], result["errors"]) == (1, [])
    assert len(lines) == 1001                                       # 1000 Zeilen (Kopf + 999) + Hinweiszeile
    assert lines[0] == "x" and lines[999] == "999" and lines[-1].startswith(NOTE_START)
    _assert_not_locked(probe_db)


def test_a_huge_select_as_the_last_command_leaves_no_lock_and_commits(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 10)
    result = _run(probe_db, f"INSERT INTO Probe VALUES ('n', 9); SELECT x FROM {_numbers(MANY_ROWS)}")
    assert (result["success_count"], result["errors"]) == (2, [])
    assert ("n", 9) in _probe(probe_db)
    _assert_not_locked(probe_db)


def test_http_truncated_output_arrives_as_json_with_the_note(server_port, auto_login, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 3)
    resp = _post_sql(server_port, f"SELECT x FROM {_numbers(10)}", auto_login)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["output"].split("\n")[:3] == ["x", "1", "2"]
    assert body["output"].split("\n")[3].startswith(NOTE_START)
    assert (body["success_count"], body["errors"]) == (1, [])


def test_the_output_is_read_row_by_row_and_stops_at_the_limit(probe_db, monkeypatch):
    """2000 Zeilen à 100 kB (200 MB) bei einer Bytegrenze von 1 MB: Die Konsole liest nur die
    paar Zeilen, die hineinpassen – nicht erst alles und kürzt dann."""
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_BYTES", 1_000_000)
    cells = []
    real_cell = handlers._console_cell
    monkeypatch.setattr(handlers, "_console_cell", lambda value: cells.append(1) or real_cell(value))

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        result = _run(probe_db, f"SELECT hex(zeroblob(50000)) AS h FROM {_numbers(2000)}")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        if not was_tracing:
            tracemalloc.stop()

    lines = result["output"].split("\n")
    assert lines[0] == "h" and lines[-1].startswith(NOTE_START)
    assert len(lines) == 11                                         # Kopf, 9 Zeilen à 100001 Bytes, Hinweis
    assert len(cells) <= 20                                         # nicht alle 2000 Zeilen angefasst
    assert peak < 20_000_000                                        # statt der 200 MB des ganzen Ergebnisses


# ── Teil 3: Zellen ───────────────────────────────────────────────────────────
def test_blob_null_numbers_and_text_in_the_output(probe_db):
    result = _run(probe_db, "SELECT NULL AS n, 1 AS i, 2.5 AS f, 'text' AS t, x'0102' AS b, zeroblob(3) AS z")
    assert result["errors"] == []
    assert result["output"] == "n;i;f;t;b;z\n;1;2.5;text;<BLOB 2 Bytes>;<BLOB 3 Bytes>"


def test_blob_output_shows_the_size_not_the_content(probe_db):
    with contextlib.closing(sqlite3.connect(probe_db.db_name)) as con:
        con.execute("CREATE TABLE Datei (Name TEXT, Inhalt BLOB)")
        con.execute("INSERT INTO Datei VALUES ('a.bin', ?)", (bytes(range(256)) * 40,))
        con.commit()
    result = _run(probe_db, "SELECT Name, Inhalt FROM Datei")
    assert result["output"] == "Name;Inhalt\na.bin;<BLOB 10240 Bytes>"


@pytest.mark.parametrize("value, text", [
    pytest.param(None, "", id="NULL"),
    pytest.param(b"\x00\x01", "<BLOB 2 Bytes>", id="bytes"),
    pytest.param(bytearray(b"abc"), "<BLOB 3 Bytes>", id="bytearray"),
    pytest.param(memoryview(b"abcd"), "<BLOB 4 Bytes>", id="memoryview"),
    pytest.param(b"", "<BLOB 0 Bytes>", id="leerer-BLOB"),
    pytest.param("text", "text", id="Text"),
    pytest.param("", "", id="leerer-Text"),
    pytest.param(7, "7", id="Ganzzahl"),
    pytest.param(2.5, "2.5", id="Kommazahl"),
    pytest.param(0, "0", id="Null"),
])
def test_console_cell(value, text):
    assert handlers._console_cell(value) == text


def test_select_output_stays_semicolon_separated_with_a_header(probe_db):
    result = _run(probe_db, "SELECT A, B, A || B AS ab FROM Probe ORDER BY B")
    assert result["output"] == "A;B;ab\nx;1;x1\ny;2;y2"
