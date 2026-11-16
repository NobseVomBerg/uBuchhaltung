# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""SQL-Konsole: Befehle trennen und Ergebnisse ausgeben (seit 2.3.11).

Zwei Teile:
  1. ``_console_statements`` trennt eine Eingabe nicht mehr an jedem ``;``, sondern nur dort, wo
     ``sqlite3.complete_statement`` den Text seit dem letzten Befehl als vollständig ansieht: Ein Semikolon in einer
     Zeichenkette, einem Bezeichner, einem Kommentar oder im Rumpf eines Triggers (``BEGIN … END;``) zerbricht nichts.
     Der Rest ohne abschließendes ``;`` ist der letzte Befehl, leere Befehle entfallen. Drei Grenzen gelten, bevor
     etwas läuft (Eingabe in Bytes, Zahl der Befehle, je Befehl geprüfte Semikolons), und die letzte hält die
     Laufzeit in Schranken: Ein Text voller Semikolons in einer Zeichenkette würde sonst quadratisch,
  2. die Konsole gibt das Ergebnis jedes Befehls aus, dessen Cursor Spalten hat – SELECT, WITH, PRAGMA, EXPLAIN und
     ``… RETURNING …`` –, nicht nur das von Befehlen, die mit SELECT beginnen. Die Ausgabegrenzen gelten dafür
     unverändert.

Alle Namen und Werte sind erfunden. Die Modi der Konsole (lesend/schreibend) stehen in
tests/test_sql_console_modes.py.
"""
import time

import pytest

from server import handlers
from tests.test_sql_console import _query, _run, probe_db  # noqa: F401 (probe_db: Fixture)

LIMIT_TEXT = "nichts wurde ausgeführt"


def _statements(sql):
    return handlers._console_statements(sql)


def _refused(sql):
    """Der Text, mit dem ``_console_statements`` die Eingabe ablehnt."""
    with pytest.raises(handlers._ConsoleRefused) as refused:
        _statements(sql)
    return str(refused.value)


# ── Teil 1: Wo getrennt wird ─────────────────────────────────────────────────
TRIGGER = ("CREATE TRIGGER t_ins AFTER INSERT ON T BEGIN INSERT INTO Log VALUES (1); "
           "INSERT INTO Log VALUES (2); END;")


@pytest.mark.parametrize("sql, expected", [
    pytest.param("SELECT 1", ["SELECT 1"], id="ein-Befehl-ohne-Semikolon"),
    pytest.param("SELECT 1;", ["SELECT 1;"], id="ein-Befehl-mit-Semikolon"),
    pytest.param("SELECT 1; SELECT 2", ["SELECT 1;", "SELECT 2"], id="der-Rest-ist-der-letzte-Befehl"),
    pytest.param("  SELECT 1 ;\n\n SELECT 2 ;  ", ["SELECT 1 ;", "SELECT 2 ;"], id="Leerraum-aussen-fällt-weg"),
    pytest.param("SELECT 'a;b'; SELECT 2;", ["SELECT 'a;b';", "SELECT 2;"], id="Semikolon-in-einfachen-Anführungszeichen"),
    pytest.param("SELECT 'it''s;ok'; SELECT 2", ["SELECT 'it''s;ok';", "SELECT 2"], id="verdoppeltes-Anführungszeichen"),
    pytest.param('SELECT "a;b" AS x; SELECT 2', ['SELECT "a;b" AS x;', "SELECT 2"], id="Semikolon-in-doppelten-Anführungszeichen"),
    pytest.param("SELECT [a;b] FROM t; SELECT 2", ["SELECT [a;b] FROM t;", "SELECT 2"], id="Semikolon-in-eckigen-Klammern"),
    pytest.param("SELECT `a;b` FROM t; SELECT 2", ["SELECT `a;b` FROM t;", "SELECT 2"], id="Semikolon-in-Backticks"),
    pytest.param("SELECT 1 -- Kommentar; mit Semikolon\n; SELECT 2",
                 ["SELECT 1 -- Kommentar; mit Semikolon\n;", "SELECT 2"], id="Semikolon-im-Zeilenkommentar"),
    pytest.param("SELECT 1 /* Block; Kommentar */; SELECT 2",
                 ["SELECT 1 /* Block; Kommentar */;", "SELECT 2"], id="Semikolon-im-Blockkommentar"),
    pytest.param("SELECT 1; -- nur Kommentar; ohne Ende", ["SELECT 1;", "-- nur Kommentar; ohne Ende"],
                 id="Kommentar-ohne-Befehl-am-Schluss"),
    pytest.param(f"{TRIGGER} SELECT 3", [TRIGGER, "SELECT 3"], id="Trigger-mit-Rumpf"),
    pytest.param("CREATE TRIGGER t AFTER INSERT ON T BEGIN SELECT CASE WHEN 1 THEN 2 END; END; SELECT 3",
                 ["CREATE TRIGGER t AFTER INSERT ON T BEGIN SELECT CASE WHEN 1 THEN 2 END; END;", "SELECT 3"],
                 id="Trigger-mit-CASE-END-im-Rumpf"),
    pytest.param("SELECT CASE WHEN 1 THEN 2 END; SELECT 3", ["SELECT CASE WHEN 1 THEN 2 END;", "SELECT 3"],
                 id="CASE-END-ausserhalb-eines-Triggers"),
    pytest.param(";;  ;\n; SELECT 1;;", ["SELECT 1;"], id="leere-Befehle-entfallen"),
    pytest.param("-- Kopf\nSELECT 1; /* x */ SELECT 2", ["-- Kopf\nSELECT 1;", "/* x */ SELECT 2"],
                 id="Kommentare-vor-dem-Befehl-bleiben-dran"),
    pytest.param("SELECT 1; SELECT 'x;y'", ["SELECT 1;", "SELECT 'x;y'"], id="Semikolon-in-der-Zeichenkette-des-letzten"),
    pytest.param("SELECT 'a;", ["SELECT 'a;"], id="offene-Zeichenkette-ist-ein-Befehl-mit-Fehler"),
    pytest.param("SELECT 'ä;ö'; SELECT '€'", ["SELECT 'ä;ö';", "SELECT '€'"], id="Umlaute-und-Eurozeichen"),
    pytest.param("", [], id="leer"),
    pytest.param(" \n\t ", [], id="nur-Leerraum"),
    pytest.param(";", [], id="nur-Semikolon"),
    pytest.param(" ; ; ", [], id="nur-Semikolons-und-Leerraum"),
])
def test_statements_are_split_where_sqlite_sees_a_complete_command(sql, expected):
    assert _statements(sql) == expected


def test_a_semicolon_inside_text_does_not_break_the_command_when_run(probe_db):
    result = _run(probe_db, "INSERT INTO Probe VALUES ('a;b', 3); SELECT A FROM Probe WHERE B = 3", write=True)
    assert (result["success_count"], result["total"], result["errors"]) == (2, 2, [])
    assert result["output"] == "A\na;b"
    assert _query(probe_db, "SELECT A FROM Probe WHERE B = 3") == [("a;b",)]


def test_a_script_with_text_comments_a_trigger_and_a_last_command_without_semicolon_runs_in_full(probe_db):
    script = """
        -- Tabellen; und ein Trigger mit zwei Befehlen im Rumpf
        CREATE TABLE T (A TEXT);
        CREATE TABLE Log (W TEXT);
        CREATE TRIGGER t_ins AFTER INSERT ON T BEGIN INSERT INTO Log VALUES ('ins;ert'); INSERT INTO Log VALUES ('zwei'); END;
        INSERT INTO T VALUES ('a;b'); -- Kommentar; mit Semikolon
        INSERT INTO T VALUES ('c') /* Block; Kommentar */ ;;
        ;
        INSERT INTO T VALUES ('letzter')
    """
    result = _run(probe_db, script, write=True)
    assert (result["success_count"], result["total"], result["errors"]) == (6, 6, [])
    assert _query(probe_db, "SELECT A FROM T ORDER BY rowid") == [("a;b",), ("c",), ("letzter",)]
    assert _query(probe_db, "SELECT W FROM Log ORDER BY rowid") == [("ins;ert",), ("zwei",)] * 3


def test_empty_commands_are_not_counted(probe_db):
    result = _run(probe_db, ";;; INSERT INTO Probe VALUES ('x', 3);; ;  ;", write=True)
    assert (result["success_count"], result["total"], result["errors"]) == (1, 1, [])


@pytest.mark.parametrize("sql", ["", "   \n ", ";", " ; ;; ", "\t;\n"])
def test_input_without_a_command_is_refused_in_both_modes(probe_db, sql):
    for write in (False, True):
        assert _run(probe_db, sql, write) == {"success_count": 0, "total": 0, "output": "",
                                              "errors": ["Keine SQL-Befehle eingegeben."]}


# ── Teil 1: Grenzen, vor dem Ausführen geprüft ───────────────────────────────
def test_the_limits_are_the_agreed_ones():
    assert handlers._CONSOLE_MAX_SQL_BYTES == 10_000_000
    assert handlers._CONSOLE_MAX_STATEMENTS == 200_000
    assert handlers._CONSOLE_MAX_SPLIT_TRIES == 2_000
    assert handlers._CONSOLE_MAX_SPLIT_SCAN == 500_000_000


def test_the_input_may_have_exactly_the_byte_limit_but_not_one_more(monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_SQL_BYTES", 20)
    assert _statements("SELECT 1; SELECT 22;") == ["SELECT 1;", "SELECT 22;"]          # genau 20 Bytes
    text = _refused("SELECT 1; SELECT 222;")                                               # 21 Bytes
    assert "zu groß" in text and "20 Bytes" in text and LIMIT_TEXT in text


def test_the_byte_limit_counts_utf8_bytes_not_characters(monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_SQL_BYTES", 12)
    assert _statements("SELECT 'ab';") == ["SELECT 'ab';"]          # 12 Zeichen, 12 Bytes
    assert "zu groß" in _refused("SELECT 'ä€';")                    # 12 Zeichen, aber 15 Bytes


def test_the_statement_limit_counts_commands_and_not_empty_ones(monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_STATEMENTS", 3)
    assert len(_statements("SELECT 1; SELECT 2; SELECT 3;")) == 3
    assert len(_statements(";;;; SELECT 1;;; SELECT 2; ;SELECT 3;;;;")) == 3
    for sql in ("SELECT 1; SELECT 2; SELECT 3; SELECT 4;", "SELECT 1; SELECT 2; SELECT 3; SELECT 4"):
        text = _refused(sql)
        assert "zu viele Befehle" in text and "3" in text and LIMIT_TEXT in text


def test_a_refused_input_runs_nothing_and_says_so(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_STATEMENTS", 2)
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3); SELECT 1; SELECT 2", write=True)
    assert result["success_count"] == 0 and result["total"] == 0 and result["output"] == ""
    assert len(result["errors"]) == 1 and "zu viele Befehle" in result["errors"][0]
    assert "rolled_back" not in result                                   # es lief nichts, es gibt nichts zurückzunehmen
    assert _query(probe_db, "SELECT COUNT(*) FROM Probe WHERE B = 3") == [(0,)]


def test_the_split_tries_count_the_semicolons_checked_per_command_and_start_over_with_each(monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_SPLIT_TRIES", 3)
    assert _statements("SELECT 'a;b;c;d'") == ["SELECT 'a;b;c;d'"]                    # drei geprüfte Semikolons
    assert len(_statements("SELECT 'a;b;c;d'; SELECT 'a;b;c;d';")) == 2               # je Befehl neu gezählt
    text = _refused("SELECT 'a;b;c;d;e'")                                              # das vierte ist eins zu viel
    assert text.startswith("Befehl 1: ") and "zu viele Semikolons" in text and LIMIT_TEXT in text
    assert _refused("SELECT 1; SELECT 'a;b;c;d;e'").startswith("Befehl 2: ")
    assert "zu viele Semikolons" in _refused("SELECT 1 -- a;b;c;d;e")                  # auch im Kommentar


def test_the_split_tries_default_to_2000(monkeypatch):
    fits = "SELECT '" + ";" * 2000 + "'"
    assert len(_statements(fits)) == 1
    assert "zu viele Semikolons" in _refused("SELECT '" + ";" * 2001 + "'")


def test_input_with_a_null_character_is_refused_with_text(probe_db):
    text = _refused("SELECT 1;\x00 SELECT 2")
    assert "Nullzeichen" in text and LIMIT_TEXT in text
    result = _run(probe_db, "SELECT 1;\x00", write=True)
    assert result["success_count"] == 0 and "Nullzeichen" in result["errors"][0]


def test_input_with_an_unencodable_character_is_refused_with_text():
    assert "ungültiges Zeichen" in _refused("SELECT '\ud800';")


# ── Teil 1: Laufzeit ─────────────────────────────────────────────────────────
def test_a_text_full_of_semicolons_in_a_string_is_refused_quickly():
    """Ohne die Grenze wären das 900 000 Prüfungen über einen immer längeren Text (quadratisch, Minuten)."""
    started = time.perf_counter()
    text = _refused("SELECT '" + ";" * 900_000 + "'")
    assert time.perf_counter() - started < 2
    assert "zu viele Semikolons" in text


def test_semicolons_far_apart_in_one_long_string_stay_within_the_scan_budget():
    """1999 Semikolons weit verstreut in einer Zeichenkette: Jede Prüfung liest den Text bis dahin erneut. 400 Kilobyte
    bleiben unter dem Budget (_CONSOLE_MAX_SPLIT_SCAN: rund 400 Millionen Zeichen erneut gelesen) und gehen durch, zehn
    Megabyte lägen weit darüber und werden früh abgelehnt – mehr Arbeit lassen die Grenzen nicht zu."""
    sql = "SELECT '" + ("a" * 199 + ";") * 1999 + "'"
    started = time.perf_counter()
    assert len(_statements(sql)) == 1
    big = "SELECT '" + ("a" * 4999 + ";") * 1999 + "'"
    assert len(big.encode()) <= handlers._CONSOLE_MAX_SQL_BYTES
    assert "zu viele Semikolons" in _refused(big)
    assert time.perf_counter() - started < 10


def test_the_scan_budget_counts_what_failed_tries_read_again(monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_SPLIT_SCAN", 40)
    assert len(_statements("SELECT 1; SELECT 2; SELECT 3; SELECT 4; SELECT 5; SELECT 6;")) == 6   # nichts erneut gelesen
    assert _statements("SELECT 'a;b'") == ["SELECT 'a;b'"]                                         # 10 Zeichen
    text = _refused("SELECT 'aaaaaaaaaaaaaaaaaaaa;bbbbbbbbbbbbbbbbbbbb;c'")                         # 29 + 50 Zeichen
    assert text.startswith("Befehl 1: ") and "zu viele Semikolons" in text and LIMIT_TEXT in text


def test_the_largest_input_of_empty_commands_and_the_most_commands_are_quick():
    started = time.perf_counter()
    assert _statements(";" * handlers._CONSOLE_MAX_SQL_BYTES) == []
    assert len(_statements("SELECT 1;" * handlers._CONSOLE_MAX_STATEMENTS)) == handlers._CONSOLE_MAX_STATEMENTS
    assert "zu viele Befehle" in _refused("SELECT 1;" * (handlers._CONSOLE_MAX_STATEMENTS + 1))
    assert time.perf_counter() - started < 30


# ── Teil 2: Ergebnisse ───────────────────────────────────────────────────────
@pytest.mark.parametrize("write", [False, True], ids=["lesend", "schreibend"])
def test_pragma_with_columns_prints_its_result(probe_db, write):
    result = _run(probe_db, "PRAGMA table_info(Probe); PRAGMA user_version", write)
    assert result["errors"] == []
    lines = result["output"].split("\n")
    assert lines[:3] == ["cid;name;type;notnull;dflt_value;pk", "0;A;TEXT;0;;0", "1;B;INTEGER;0;;0"]
    assert lines[3] == "user_version" and lines[4].isdigit()


@pytest.mark.parametrize("write", [False, True], ids=["lesend", "schreibend"])
def test_with_prints_its_result(probe_db, write):
    result = _run(probe_db, "WITH n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n LIMIT 3) SELECT x FROM n", write)
    assert result["errors"] == [] and result["output"] == "x\n1\n2\n3"


@pytest.mark.parametrize("write", [False, True], ids=["lesend", "schreibend"])
def test_explain_prints_its_program(probe_db, write):
    result = _run(probe_db, "EXPLAIN SELECT A FROM Probe; EXPLAIN QUERY PLAN SELECT A FROM Probe", write)
    assert result["errors"] == []
    lines = result["output"].split("\n")
    assert lines[0] == "addr;opcode;p1;p2;p3;p4;p5;comment"
    assert "id;parent;notused;detail" in lines and len(lines) > 5


def test_returning_prints_the_rows_the_command_changed(probe_db):
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3), ('q', 4) RETURNING A, B; "
                            "UPDATE Probe SET B = B * 10 WHERE A = 'z' RETURNING B; "
                            "DELETE FROM Probe WHERE A = 'q' RETURNING A", write=True)
    assert (result["success_count"], result["errors"]) == (3, [])
    assert result["output"] == "A;B\nz;3\nq;4\nB\n30\nA\nq"
    assert _query(probe_db, "SELECT A, B FROM Probe ORDER BY B") == [("x", 1), ("y", 2), ("z", 30)]


def test_a_command_without_columns_prints_nothing(probe_db):
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3); CREATE TABLE Neu (A TEXT); PRAGMA user_version = 0",
                  write=True)
    assert result["success_count"] == 0                 # das PRAGMA mit Wert ist gesperrt …
    result = _run(probe_db, "INSERT INTO Probe VALUES ('z', 3); CREATE TABLE Neu (A TEXT); DELETE FROM Probe WHERE B = 3",
                  write=True)
    assert (result["success_count"], result["output"]) == (3, "")


def test_the_output_limits_apply_to_every_kind_of_result(probe_db, monkeypatch):
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 4)
    result = _run(probe_db, "PRAGMA table_info(Probe); WITH n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n LIMIT 9) "
                            "SELECT x FROM n; SELECT 'spaet'")
    lines = result["output"].split("\n")
    assert lines[:3] == ["cid;name;type;notnull;dflt_value;pk", "0;A;TEXT;0;;0", "1;B;INTEGER;0;;0"]
    assert lines[3] == "x"                                          # der Kopf des nächsten Ergebnisses ist die vierte Zeile
    assert lines[4].startswith("… Ausgabe gekürzt") and len(lines) == 5
    assert (result["success_count"], result["errors"]) == (3, [])


def test_a_cut_returning_result_still_commits_every_change(probe_db, monkeypatch):
    """Das Ergebnis eines ``… RETURNING …`` wird nur bis zur Grenze gelesen; die Änderung ist schon ganz gemacht, und
    das COMMIT darf nicht an dem halb gelesenen Ergebnis scheitern („SQL statements in progress“)."""
    monkeypatch.setattr(handlers, "_CONSOLE_MAX_OUTPUT_ROWS", 3)
    result = _run(probe_db, "INSERT INTO Probe SELECT 'r', x FROM (WITH RECURSIVE c(x) AS (SELECT 10 UNION ALL "
                            "SELECT x + 1 FROM c LIMIT 50) SELECT x FROM c) RETURNING B", write=True)
    assert (result["success_count"], result["errors"], result["rolled_back"]) == (1, [], False)
    assert result["output"].split("\n")[:3] == ["B", "10", "11"]
    assert result["output"].split("\n")[3].startswith("… Ausgabe gekürzt")
    assert _query(probe_db, "SELECT COUNT(*) FROM Probe WHERE A = 'r'") == [(50,)]
