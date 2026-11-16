# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""SQL-Konsole: PRAGMA-Positivliste und gesperrte Funktionen (Authorizer).

Ergänzt tests/test_sql_console.py (ATTACH/DETACH/VACUUM, Berechtigung, HTTP). Hier geht es
um den Rest des Authorizers, immer über ``handle_execute_sql`` gegen eine echte
Datenbankdatei der App:

  1. PRAGMAs laufen nur per Positivliste: die Namen aus ``_CONSOLE_PRAGMAS_READ`` ohne
     Argument (und dann nachweislich ohne die Datei zu verändern), die aus
     ``_CONSOLE_PRAGMAS_WITH_ARG`` mit und ohne. Alles andere ist gesperrt – auch als
     Tabellenfunktion ``pragma_<name>``, auch ``optimize``/``incremental_vacuum``/
     ``wal_checkpoint`` ohne Argument und alles, was SQLite selbst noch als PRAGMA kennt
     (``pragma_pragma_list``, zur Laufzeit abgefragt: ein künftig neues PRAGMA fällt so
     automatisch unter die Sperre),
  2. gesperrte Funktionen (``fts3_tokenizer``, ``load_extension``),
  3. alles andere bleibt im Schreibmodus erlaubt: DML, DDL, ``WITH``, EXPLAIN, ANALYZE … (nicht mehr BEGIN,
     COMMIT, ROLLBACK und SAVEPOINT: Alle Befehle laufen dort in der einen Transaktion der Anfrage).

Die Sperren dieser Datei gelten in beiden Modi und laufen deshalb für beide (``BOTH_MODES``): Im Lesemodus
(Standard) lehnt die Konsole sie wie alles Schreibende ab und läuft mit dem nächsten Befehl weiter, im Schreibmodus
beendet der erste abgelehnte Befehl die Anfrage, und alles ist zurückgenommen (``denied``). Die Modi selbst stehen in
tests/test_sql_console_modes.py.

``fts3_tokenizer`` wird nie mit einer Adresse aufgerufen (Absturzgefahr), sondern nur mit
dem Namen und nur über die Konsole mit ihrer Sperre. Die Grenzen (Zeit, Größe, Ausgabe)
stehen in tests/test_sql_console_limits.py.
"""
import contextlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import auth
import userctx
from db import Database
from server import handlers
from tests import installation
from tests.test_sql_console import (BOTH_MODES, CONSOLE_DENIED, NOTHING_CHANGED, READONLY, _post_sql, _query,
                                    _run, denied)

READ_PRAGMAS = sorted(handlers._CONSOLE_PRAGMAS_READ)
ARG_PRAGMAS = sorted(handlers._CONSOLE_PRAGMAS_WITH_ARG)
# Ein sinnvoller Wert je Abfrage-PRAGMA; ein neu aufgenommener Name bekommt "1".
ARG_FOR = {
    "table_info": "Probe", "table_xinfo": "Probe", "table_list": "Probe", "index_list": "Probe",
    "index_info": "IProbe", "index_xinfo": "IProbe", "foreign_key_list": "Kind",
    "foreign_key_check": "Kind", "integrity_check": "1", "quick_check": "1",
    "foreign_keys": "ON", "defer_foreign_keys": "OFF", "case_sensitive_like": "OFF",
}
# Dasselbe als Tabellenfunktion pragma_<name>(…): das erste Argument ist hier ein Tabellenname.
TABLE_ARG_FOR = {"integrity_check": "Probe", "quick_check": "Probe"}


def _sqlite_pragmas():
    """Alle PRAGMAs, die die laufende SQLite-Version selbst kennt."""
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as con:
            return sorted(row[0] for row in con.execute("SELECT name FROM pragma_pragma_list"))
    except sqlite3.Error:
        return []


def _has_table_function(name):
    """Kennt diese SQLite-Version ``pragma_<name>`` als Tabellenfunktion?"""
    with contextlib.closing(sqlite3.connect(":memory:")) as con:
        try:
            con.execute(f"SELECT * FROM pragma_{name}").fetchall()
        except sqlite3.OperationalError:
            return False
    return True


SQLITE_PRAGMAS = _sqlite_pragmas()
UNLISTED_PRAGMAS = [n for n in SQLITE_PRAGMAS if n not in READ_PRAGMAS and n not in ARG_PRAGMAS]
READ_WITH_FUNCTION = [n for n in READ_PRAGMAS if _has_table_function(n)]
ARG_WITH_FUNCTION = [n for n in ARG_PRAGMAS if _has_table_function(n)]


def _has_function(name):
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as con:
            return con.execute("SELECT COUNT(*) FROM pragma_function_list WHERE name = ?",
                               (name,)).fetchone()[0] > 0
    except sqlite3.Error:
        return False


def _has_module(name):
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as con:
            return con.execute("SELECT COUNT(*) FROM pragma_module_list WHERE name = ?",
                               (name,)).fetchone()[0] > 0
    except sqlite3.Error:
        return False


requires_fts3_tokenizer = pytest.mark.skipif(
    not _has_function("fts3_tokenizer"),
    reason="diese SQLite-Version kennt fts3_tokenizer() nicht – ohne die Funktion fragt SQLite "
           "den Authorizer nie danach")
requires_fts5 = pytest.mark.skipif(not _has_module("fts5"), reason="diese SQLite-Version hat kein FTS5")
requires_drop_column = pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35),
                                          reason="ALTER TABLE … DROP COLUMN gibt es erst ab SQLite 3.35")


# ── Hilfen ───────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def _console_template(tmp_path_factory):
    """Eine frisch eingerichtete App-Datenbank (wie ``tmp_db``) mit einer kleinen Tabelle samt
    Index, einem Eltern/Kind-Paar (Fremdschlüssel) und etwas Inhalt – das sind die Tabellennamen,
    die die PRAGMA-Argumente brauchen. Einmal je Datei angelegt: Die Einrichtung einer neuen
    Datenbank kostet ~70 ms, eine Kopie davon ~4 ms – bei einigen hundert Fällen der Unterschied."""
    root = tmp_path_factory.mktemp("konsole_vorlage")
    template = str(root / "vorlage.db")
    with pytest.MonkeyPatch.context() as patch:           # die Einrichtung darf nicht ins echte data/
        patch.setattr(userctx, "DATA_ROOT", str(root / "data"))
        patch.setattr(auth, "AUTH_DB", str(root / "data" / "auth.db"))
        Database(db_name=template)
    with contextlib.closing(sqlite3.connect(template)) as con:
        con.executescript("""
            CREATE TABLE Probe (A TEXT, B INTEGER);
            CREATE INDEX IProbe ON Probe (A);
            INSERT INTO Probe VALUES ('x', 1), ('y', 2);
            CREATE TABLE Eltern (ID INTEGER PRIMARY KEY);
            INSERT INTO Eltern VALUES (1);
            CREATE TABLE Kind (ID INTEGER PRIMARY KEY, Eltern_ID INTEGER REFERENCES Eltern (ID));
        """)
    return template


@pytest.fixture
def console_db(_console_template, tmp_path):
    """Eigene Kopie der Vorlage als ``Database`` – jeder Test verändert nur seine."""
    path = str(tmp_path / "konsole.db")
    shutil.copyfile(_console_template, path)
    return Database(db_name=path)


def _state(db):
    """Zustand der Datenbank zum Vorher/Nachher-Vergleich: Dateiinhalt Byte für Byte (sha256),
    logischer Inhalt (iterdump) und die Begleitdateien (-journal, -wal, -shm …)."""
    path = Path(db.db_name)
    return (installation.file_digest(db.db_name),
            sorted(p.name for p in path.parent.glob(path.name + "*")),
            installation.dump(db.db_name))


def _stat_tables(db):
    return _query(db, "SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_stat%'")


def _count(sql):
    """Anzahl der durch Semikolon getrennten Befehle (wie die Konsole sie zählt)."""
    return len([part for part in sql.split(";") if part.strip()])


# ── Teil 0: die Entscheidung des Authorizers selbst ──────────────────────────
# Die Grundsperren in ``_console_authorizer`` (ATTACH, DETACH, PRAGMA-Listen, Funktionen) gelten in beiden Modi und
# lassen alles andere offen; was der Authorizer einer Anfrage je Modus daraus macht (``_ConsoleAuthorizer``: im
# Lesemodus nur Abfragen, im Schreibmodus ohne Transaktionsbefehle), prüft tests/test_sql_console_modes.py.
@pytest.mark.parametrize("action, arg1, arg2, expected", [
    pytest.param(sqlite3.SQLITE_ATTACH, "x.db", None, sqlite3.SQLITE_DENY, id="ATTACH"),
    pytest.param(sqlite3.SQLITE_DETACH, "x", None, sqlite3.SQLITE_DENY, id="DETACH"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "user_version", None, sqlite3.SQLITE_OK, id="Leseliste-ohne-Argument"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "USER_VERSION", None, sqlite3.SQLITE_OK, id="Leseliste-gross"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "user_version", "7", sqlite3.SQLITE_DENY, id="Leseliste-mit-Wert"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "user_version", "", sqlite3.SQLITE_DENY, id="Leseliste-mit-leerem-Wert"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "table_info", None, sqlite3.SQLITE_OK, id="Abfrage-ohne-Argument"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "table_info", "Probe", sqlite3.SQLITE_OK, id="Abfrage-mit-Argument"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "Table_Info", "Probe", sqlite3.SQLITE_OK, id="Abfrage-gemischt"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "optimize", None, sqlite3.SQLITE_DENY, id="optimize"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "OPTIMIZE", None, sqlite3.SQLITE_DENY, id="optimize-gross"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "gibtesnicht", None, sqlite3.SQLITE_DENY, id="unbekannt"),
    pytest.param(sqlite3.SQLITE_PRAGMA, None, None, sqlite3.SQLITE_DENY, id="ohne-Namen"),
    pytest.param(sqlite3.SQLITE_PRAGMA, "", None, sqlite3.SQLITE_DENY, id="leerer-Name"),
    pytest.param(sqlite3.SQLITE_SELECT, None, None, sqlite3.SQLITE_OK, id="SELECT"),
    pytest.param(sqlite3.SQLITE_INSERT, "Probe", None, sqlite3.SQLITE_OK, id="INSERT"),
    pytest.param(sqlite3.SQLITE_CREATE_TABLE, "Neu", None, sqlite3.SQLITE_OK, id="CREATE-TABLE"),
    pytest.param(sqlite3.SQLITE_DROP_TABLE, "Probe", None, sqlite3.SQLITE_OK, id="DROP-TABLE"),
    pytest.param(sqlite3.SQLITE_TRANSACTION, "BEGIN", None, sqlite3.SQLITE_OK, id="Transaktion"),
])
def test_authorizer_decisions(action, arg1, arg2, expected):
    assert handlers._console_authorizer(action, arg1, arg2, None, None) == expected


# ── Teil 1: PRAGMAs – Leseliste ──────────────────────────────────────────────
@BOTH_MODES
@pytest.mark.parametrize("name", READ_PRAGMAS)
def test_read_pragma_without_argument_is_allowed_and_changes_nothing(console_db, name, write):
    before = _state(console_db)
    forms = [f"PRAGMA {name}", f"PRAGMA {name.upper()}"]
    if _has_table_function(name):
        forms += [f"SELECT * FROM pragma_{name}", f"SELECT * FROM PRAGMA_{name.upper()}"]
    for sql in forms:
        result = _run(console_db, sql, write)
        assert (result["success_count"], result["errors"]) == (1, []), sql
    if not _has_table_function(name):       # kennt SQLite die Form nicht: dessen Fehler, nicht „nicht erlaubt“
        result = _run(console_db, f"SELECT * FROM pragma_{name}", write)
        assert result["success_count"] == 0
        assert result["errors"] == [f"Befehl 1: no such table: pragma_{name}"] + ([NOTHING_CHANGED] if write else [])
    assert _state(console_db) == before


@BOTH_MODES
@pytest.mark.parametrize("sql", [
    pytest.param("PRAGMA USER_VERSION", id="Grossbuchstaben"),
    pytest.param("pragma User_Version", id="gemischt"),
    pytest.param("PRAGMA Table_Info(Probe)", id="table_info-gemischt"),
    pytest.param("Pragma Integrity_Check", id="integrity_check-gemischt"),
    pytest.param("SELECT * FROM PRAGMA_TABLE_INFO('Probe')", id="Tabellenfunktion-gross"),
    pytest.param("SELECT * FROM Pragma_User_Version", id="Tabellenfunktion-gemischt"),
])
def test_pragma_names_are_matched_case_insensitively(console_db, sql, write):
    result = _run(console_db, sql, write)
    assert (result["success_count"], result["errors"]) == (1, [])


def test_pragma_names_are_matched_case_insensitively_for_foreign_keys(console_db):
    # Lesemodus: mit Wert erlaubt; Schreibmodus: gesperrt, in jeder Schreibweise
    assert _run(console_db, "PRAGMA FOREIGN_KEYS = ON")["errors"] == []
    for sql in ("PRAGMA FOREIGN_KEYS = ON", "pragma Foreign_Keys(0)", "PRAGMA main.foreign_keys = OFF"):
        assert _run(console_db, sql, write=True) == denied(True), sql


@BOTH_MODES
@pytest.mark.parametrize("name", READ_PRAGMAS)
def test_read_pragma_with_a_value_is_denied(console_db, name, write):
    before = _state(console_db)
    for sql in (f"PRAGMA {name} = 1", f"PRAGMA {name}(1)", f"PRAGMA main.{name} = 1",
                f"PRAGMA {name.upper()} = 1", f"PRAGMA \"{name}\" = 1"):
        assert _run(console_db, sql, write) == denied(write), sql
    assert _state(console_db) == before


@BOTH_MODES
@pytest.mark.parametrize("name", READ_WITH_FUNCTION)
def test_read_pragma_table_function_cannot_take_a_value(console_db, name, write):
    """Über ``pragma_<name>(…)`` lässt sich kein Wert setzen: SQLite lehnt das Argument ab
    (kein Argument vorgesehen bzw. kein Schema namens 1) oder der Authorizer."""
    before = _state(console_db)
    result = _run(console_db, f"SELECT * FROM pragma_{name}(1)", write)
    assert result["success_count"] == 0
    assert len(result["errors"]) == (2 if write else 1)            # im Schreibmodus dazu: „Nichts wurde geändert“
    assert result["output"] == ""
    assert _state(console_db) == before


# ── Teil 1: PRAGMAs – Abfrage-PRAGMAs mit Argument ───────────────────────────
@BOTH_MODES
@pytest.mark.parametrize("name", ARG_PRAGMAS)
def test_query_pragma_is_allowed_with_and_without_argument(console_db, name, write):
    arg = ARG_FOR.get(name, "1")
    before = _state(console_db)
    forms = [f"PRAGMA {name}", f"PRAGMA {name}({arg})", f"PRAGMA {name} = {arg}",
             f"PRAGMA main.{name}({arg})", f"PRAGMA {name.upper()}({arg})"]
    if write and name == "foreign_keys":        # mit Wert gesperrt: alle Befehle laufen in einer Transaktion
        assert _run(console_db, forms[0], write)["errors"] == []
        forms = forms[1:]
        for sql in forms:
            assert _run(console_db, sql, write) == denied(True), sql
        assert _state(console_db) == before
        return
    for sql in forms:
        result = _run(console_db, sql, write)
        assert (result["success_count"], result["errors"]) == (1, []), sql
    assert _state(console_db) == before


@BOTH_MODES
@pytest.mark.parametrize("name", ARG_WITH_FUNCTION)
def test_query_pragma_table_function_is_allowed(console_db, name, write):
    arg = TABLE_ARG_FOR.get(name, ARG_FOR.get(name, "1"))
    forms = [f"SELECT * FROM pragma_{name}"]
    if name in ARG_FOR and name not in ("foreign_keys", "defer_foreign_keys"):
        forms.append(f"SELECT * FROM pragma_{name}('{arg}')")
    for sql in forms:
        result = _run(console_db, sql, write)
        assert (result["success_count"], result["errors"]) == (1, []), sql


def test_foreign_keys_pragma_with_a_value_changes_only_the_reading_connection(console_db):
    """Im Lesemodus bleibt das PRAGMA mit Wert erlaubt, harmlos (nichts lässt sich schreiben): es wirkt auf die
    Verbindung der Konsole. Der Schreibmodus sperrt es – dort sind die Fremdschlüssel immer an
    (tests/test_sql_console_modes.py)."""
    assert _run(console_db, "PRAGMA foreign_keys = OFF; SELECT * FROM pragma_foreign_keys")["output"] == \
        "foreign_keys\n0"
    assert _run(console_db, "PRAGMA foreign_keys = ON; SELECT * FROM pragma_foreign_keys")["output"] == \
        "foreign_keys\n1"


# ── Teil 1: PRAGMAs – alles andere ist gesperrt ──────────────────────────────
@pytest.mark.parametrize("sql", [
    pytest.param("PRAGMA optimize", id="optimize"),
    pytest.param("SELECT * FROM pragma_optimize", id="optimize-Tabellenfunktion"),
    pytest.param("SELECT * FROM Pragma_Optimize", id="optimize-Tabellenfunktion-gemischt"),
    pytest.param("PRAGMA optimize(0x10002)", id="optimize-mit-Maske"),
    pytest.param("SELECT * FROM pragma_optimize(0x10002)", id="optimize-Tabellenfunktion-mit-Maske"),
    pytest.param("PRAGMA incremental_vacuum", id="incremental_vacuum"),
    pytest.param("PRAGMA incremental_vacuum(10)", id="incremental_vacuum-mit-Wert"),
    pytest.param("PRAGMA wal_checkpoint", id="wal_checkpoint"),
    pytest.param("PRAGMA wal_checkpoint(TRUNCATE)", id="wal_checkpoint-TRUNCATE"),
    pytest.param("PRAGMA shrink_memory", id="shrink_memory"),
    pytest.param("PRAGMA journal_mode = DELETE", id="journal_mode-DELETE"),
    pytest.param("PRAGMA journal_mode = WAL", id="journal_mode-WAL"),
    pytest.param("PRAGMA user_version = 7", id="user_version"),
    pytest.param("PRAGMA user_version(7)", id="user_version-Klammer"),
    pytest.param("PRAGMA main.user_version = 7", id="user_version-main"),
    pytest.param("PRAGMA schema_version = 1", id="schema_version"),
    pytest.param("PRAGMA writable_schema = 1", id="writable_schema"),
    pytest.param("PRAGMA writable_schema", id="writable_schema-ohne-Argument"),
    pytest.param("PRAGMA query_only = 1", id="query_only"),
    pytest.param("PRAGMA trusted_schema", id="trusted_schema-ohne-Argument"),
    pytest.param("PRAGMA analysis_limit", id="analysis_limit-ohne-Argument"),
    pytest.param("PRAGMA locking_mode = EXCLUSIVE", id="locking_mode-setzen"),
    pytest.param("PRAGMA synchronous = 0", id="synchronous-setzen"),
    pytest.param("PRAGMA page_size = 512", id="page_size-setzen"),
    pytest.param("PRAGMA auto_vacuum = 1", id="auto_vacuum-setzen"),
    pytest.param("PRAGMA OPTIMIZE", id="Grossschreibung"),
    pytest.param("pragma Optimize", id="gemischte-Schreibung"),
    pytest.param("/* x */ PRAGMA optimize", id="Blockkommentar-davor"),
    pytest.param("-- x\nPRAGMA optimize", id="Zeilenkommentar-davor"),
    pytest.param("   \n\tPRAGMA optimize", id="Leerraum-davor"),
    pytest.param('PRAGMA "optimize"', id="optimize-in-Anfuehrungszeichen"),
    pytest.param("PRAGMA [optimize]", id="optimize-in-eckigen-Klammern"),
    pytest.param("PRAGMA main.optimize", id="optimize-mit-Schema"),
    pytest.param("EXPLAIN PRAGMA user_version = 5", id="EXPLAIN-davor"),
    pytest.param("EXPLAIN QUERY PLAN PRAGMA optimize", id="EXPLAIN-QUERY-PLAN-davor"),
    pytest.param("CREATE TABLE Opt AS SELECT * FROM pragma_optimize", id="CREATE-TABLE-AS"),
    pytest.param("PRAGMA gibtesnicht", id="unbekannter-Name"),
    pytest.param("PRAGMA gibtesnicht = 1", id="unbekannter-Name-mit-Wert"),
    pytest.param("PRAGMA stats", id="Debug-PRAGMA"),
])
@BOTH_MODES
def test_console_denies_pragma_and_changes_nothing(console_db, sql, write):
    before = _state(console_db)
    assert _run(console_db, sql, write) == denied(write)
    assert _state(console_db) == before


@BOTH_MODES
@pytest.mark.parametrize("name", UNLISTED_PRAGMAS)
def test_every_pragma_sqlite_knows_but_the_lists_do_not_is_denied(console_db, name, write):
    """Läuft über ``pragma_pragma_list`` der laufenden SQLite-Version – ein künftig neues PRAGMA
    fällt ohne Zutun unter die Sperre, mit und ohne Argument, auch als Tabellenfunktion."""
    before = _state(console_db)
    forms = [f"PRAGMA {name}", f"PRAGMA {name} = 1", f"PRAGMA {name}(1)", f"PRAGMA main.{name}"]
    if _has_table_function(name):
        forms.append(f"SELECT * FROM pragma_{name}")
    for sql in forms:
        assert _run(console_db, sql, write) == denied(write), sql
    assert _state(console_db) == before


def test_sqlite_lists_the_pragmas_the_completeness_test_runs_over():
    """Wächter: Ist die Liste leer oder fehlen Kernnamen, liefe der Vollständigkeitstest über
    nichts – dann soll er lauter scheitern als still übersprungen werden."""
    assert {"optimize", "user_version", "journal_mode", "table_info"} <= set(SQLITE_PRAGMAS)
    assert {"optimize", "wal_checkpoint", "incremental_vacuum", "writable_schema"} <= set(UNLISTED_PRAGMAS)


def test_the_pragma_lists_are_exactly_the_reviewed_ones():
    """Die Positivlisten sind eine Sicherheitsentscheidung: Wer einen Namen aufnimmt oder streicht,
    ändert dieses Verhalten bewusst – und damit auch diese Liste. (Die Tests daneben laufen über die
    Listen des Programms selbst und bemerkten eine gestrichene Zeile sonst nicht.)"""
    assert handlers._CONSOLE_PRAGMAS_READ == {
        "user_version", "schema_version", "application_id", "data_version", "journal_mode", "synchronous",
        "locking_mode", "page_size", "page_count", "max_page_count", "freelist_count", "encoding",
        "auto_vacuum", "cache_size", "busy_timeout", "secure_delete", "temp_store", "recursive_triggers",
        "database_list", "collation_list", "compile_options", "function_list", "module_list", "pragma_list",
    }
    assert handlers._CONSOLE_PRAGMAS_WITH_ARG == {
        "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
        "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check", "foreign_keys",
        "defer_foreign_keys", "case_sensitive_like",
    }


def test_the_denied_functions_are_exactly_the_reviewed_ones():
    assert handlers._CONSOLE_FUNCTIONS_DENIED == {"fts3_tokenizer", "load_extension"}


def test_the_two_pragma_lists_are_lowercase_and_do_not_overlap():
    # Der Authorizer vergleicht kleingeschrieben: ein Name mit Großbuchstaben träfe nie. Doppelte
    # Einträge wären toter Code, der eine Wahl vortäuscht.
    assert all(name == name.lower() for name in READ_PRAGMAS + ARG_PRAGMAS)
    assert not handlers._CONSOLE_PRAGMAS_READ & handlers._CONSOLE_PRAGMAS_WITH_ARG


def test_pragmas_that_change_the_database_are_on_neither_list():
    """Diese Sperren nicht aufweichen: Wer eines davon freigibt, muss diesen Test ändern."""
    dangerous = {"optimize", "incremental_vacuum", "wal_checkpoint", "shrink_memory", "writable_schema",
                 "query_only", "trusted_schema", "analysis_limit", "journal_size_limit", "mmap_size",
                 "temp_store_directory", "wal_autocheckpoint", "hard_heap_limit", "soft_heap_limit",
                 "threads", "legacy_alter_table", "ignore_check_constraints", "read_uncommitted",
                 "count_changes", "fullfsync", "checkpoint_fullfsync", "cell_size_check"}
    assert not dangerous & (handlers._CONSOLE_PRAGMAS_READ | handlers._CONSOLE_PRAGMAS_WITH_ARG)


@pytest.mark.parametrize("sql", ["PRAGMA optimize", "SELECT * FROM pragma_optimize",
                                 "PRAGMA optimize(0x10002)"])
@BOTH_MODES
def test_denied_optimize_creates_no_statistics(console_db, tmp_path, sql, write):
    prime = "INSERT INTO Probe VALUES ('p', 3)"
    control = tmp_path / "kontrolle.db"
    shutil.copyfile(console_db.db_name, control)
    with contextlib.closing(sqlite3.connect(control)) as con:        # ohne Authorizer
        con.execute(prime)
        con.execute(sql).fetchall()
        con.commit()
        made_statistics = con.execute("SELECT COUNT(*) FROM sqlite_master "
                                      "WHERE name = 'sqlite_stat1'").fetchone()[0] == 1
    if not made_statistics:
        pytest.skip("diese SQLite-Version legt mit der Anweisung keine Statistik an – "
                    "ohne Wirkung gibt es nichts zu verhindern")

    if write:                      # die Änderung davor, dann das PRAGMA: abgelehnt, und die Änderung ist zurückgenommen
        assert _run(console_db, f"{prime}; {sql}", write) == denied(True, number=2, total=2)
        assert _query(console_db, "SELECT COUNT(*) FROM Probe WHERE B = 3") == [(0,)]
    else:
        assert _run(console_db, sql) == denied(False)
    assert _stat_tables(console_db) == []


@BOTH_MODES
def test_denied_incremental_vacuum_keeps_the_free_pages(console_db, tmp_path, write):
    path = console_db.db_name
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as con:
        con.execute("PRAGMA auto_vacuum = INCREMENTAL")
        con.execute("VACUUM")                                       # übernimmt die Einstellung
        con.execute("CREATE TABLE Gross (X BLOB)")
        con.execute("INSERT INTO Gross SELECT zeroblob(4000) FROM "
                    "(WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 50) "
                    "SELECT x FROM c)")
        con.execute("DROP TABLE Gross")
        free = con.execute("PRAGMA freelist_count").fetchone()[0]
    assert free > 0

    control = tmp_path / "kontrolle.db"                             # ohne Authorizer wären sie weg
    shutil.copyfile(path, control)
    with contextlib.closing(sqlite3.connect(control)) as con:
        con.execute("PRAGMA incremental_vacuum").fetchall()
        assert con.execute("PRAGMA freelist_count").fetchone()[0] < free

    before = _state(console_db)
    for sql in ("PRAGMA incremental_vacuum", "PRAGMA incremental_vacuum(10)"):
        assert _run(console_db, sql, write) == denied(write)
    assert _query(console_db, "PRAGMA freelist_count") == [(free,)]
    assert _state(console_db) == before


@BOTH_MODES
def test_denied_wal_checkpoint_leaves_the_wal_file_alone(console_db, write):
    path = console_db.db_name
    wal = path + "-wal"
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as keeper:
        if keeper.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
            pytest.skip("WAL-Modus ist auf diesem Dateisystem nicht verfügbar")
        keeper.execute("PRAGMA wal_autocheckpoint = 0")
        keeper.execute("INSERT INTO Probe VALUES ('w', 5)")         # steht nur in der WAL-Datei
        wal_size = os.path.getsize(wal)
        main_digest = installation.file_digest(path)
        assert wal_size > 0

        for sql in ("PRAGMA wal_checkpoint", "PRAGMA wal_checkpoint(TRUNCATE)"):
            assert _run(console_db, sql, write) == denied(write)
            assert os.path.getsize(wal) == wal_size, sql
            assert installation.file_digest(path) == main_digest, sql

        keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")           # Kontrolle: ohne Sperre leer
        assert os.path.getsize(wal) == 0


@BOTH_MODES
def test_denied_query_only_pragma_does_not_change_what_the_connection_may_do(console_db, write):
    result = _run(console_db, "PRAGMA query_only = 1; INSERT INTO Probe VALUES ('z', 3)", write)
    assert result["success_count"] == 0                 # lesend: beide abgelehnt; schreibend: Abbruch beim ersten
    assert result["errors"][0] == f"Befehl 1: {CONSOLE_DENIED if write else READONLY}"
    assert _query(console_db, "SELECT A FROM Probe WHERE B = 3") == []


@BOTH_MODES
def test_denied_journal_mode_leaves_the_rollback_journal_mode(console_db, write):
    before = _state(console_db)
    assert _run(console_db, "PRAGMA journal_mode = WAL", write) == denied(write)
    assert not os.path.exists(console_db.db_name + "-wal")
    assert _query(console_db, "PRAGMA journal_mode") == [("delete",)]
    assert _state(console_db) == before


@BOTH_MODES
@pytest.mark.parametrize("sql", [
    pytest.param("PRAGMA user_version = 7", id="user_version"),
    pytest.param("PRAGMA main.user_version = 7", id="user_version-main"),
    pytest.param("PRAGMA schema_version = 1", id="schema_version"),
    pytest.param("PRAGMA application_id = 7", id="application_id"),
])
def test_denied_header_pragmas_leave_the_header_values(console_db, sql, write):
    names = ("user_version", "schema_version", "application_id")
    before = [_query(console_db, f"PRAGMA {n}") for n in names]
    assert _run(console_db, sql, write) == denied(write)
    assert [_query(console_db, f"PRAGMA {n}") for n in names] == before


@BOTH_MODES
def test_view_over_a_denied_pragma_function_stays_denied_when_queried(console_db, write):
    created = _run(console_db, "CREATE VIEW Opt AS SELECT * FROM pragma_optimize", write=True)
    assert (created["success_count"], created["errors"]) == (1, [])    # die Sicht entsteht, ihre Abfrage bleibt gesperrt
    assert _run(console_db, "SELECT * FROM Opt", write) == denied(write)
    assert _stat_tables(console_db) == []


@BOTH_MODES
def test_view_over_an_allowed_pragma_function_can_be_queried(console_db, write):
    created = _run(console_db, "CREATE VIEW Version AS SELECT * FROM pragma_user_version", write=True)
    assert (created["success_count"], created["errors"]) == (1, [])
    result = _run(console_db, "SELECT * FROM Version", write)
    assert (result["success_count"], result["errors"]) == (1, [])
    assert result["output"] == f"user_version\n{installation.user_version(console_db.db_name)}"


def test_pragma_denial_does_not_stop_the_other_commands_while_reading(console_db):
    result = _run(console_db, "PRAGMA optimize; SELECT 1; PRAGMA table_info(Probe); "
                              "PRAGMA user_version = 5; SELECT 2")
    assert result["success_count"] == 3
    assert result["total"] == 5
    assert result["errors"] == [f"Befehl 1: {READONLY}", f"Befehl 4: {READONLY}"]
    assert result["output"] == ("1\n1\ncid;name;type;notnull;dflt_value;pk\n0;A;TEXT;0;;0\n1;B;INTEGER;0;;0"
                                "\n2\n2")


def test_pragma_denial_ends_the_request_and_undoes_the_others_while_writing(console_db):
    result = _run(console_db, "INSERT INTO Probe VALUES ('z', 3); PRAGMA optimize; "
                              "PRAGMA table_info(Probe); PRAGMA user_version = 5; SELECT 1", write=True)
    assert result == denied(True, number=2, total=5)
    assert _query(console_db, "SELECT A FROM Probe WHERE B = 3") == []


@BOTH_MODES
def test_pragma_table_functions_show_the_data_they_are_allowed_to(console_db, write):
    result = _run(console_db, "SELECT name, type FROM pragma_table_info('Probe') ORDER BY cid", write)
    assert result["output"] == "name;type\nA;TEXT\nB;INTEGER"
    result = _run(console_db, "SELECT user_version FROM pragma_user_version", write)
    assert result["output"] == f"user_version\n{installation.user_version(console_db.db_name)}"


# ── Teil 2: gesperrte Funktionen ─────────────────────────────────────────────
@pytest.mark.parametrize("name", ["fts3_tokenizer", "FTS3_TOKENIZER", "Fts3_Tokenizer",
                                  "load_extension", "Load_Extension", "LOAD_EXTENSION"])
def test_authorizer_denies_the_dangerous_functions_in_any_spelling(name):
    # SQLite meldet Funktionen unter ihrem eingetragenen (kleingeschriebenen) Namen; die Sperre
    # darf sich trotzdem nicht auf eine Schreibweise verlassen.
    assert handlers._console_authorizer(sqlite3.SQLITE_FUNCTION, None, name, None, None) == sqlite3.SQLITE_DENY


@pytest.mark.parametrize("name", ["upper", "length", "json_extract", "fts5", "zeroblob", "randomblob",
                                  "count", "", None])
def test_authorizer_allows_other_functions(name):
    assert handlers._console_authorizer(sqlite3.SQLITE_FUNCTION, None, name, None, None) == sqlite3.SQLITE_OK


@requires_fts3_tokenizer
@BOTH_MODES
@pytest.mark.parametrize("sql", [
    pytest.param("SELECT fts3_tokenizer('simple')", id="Aufruf"),
    pytest.param("SELECT hex(FTS3_TOKENIZER('simple'))", id="Grossschreibung-verschachtelt"),
    pytest.param("SELECT 1 WHERE EXISTS (SELECT fts3_tokenizer('simple'))", id="Unterabfrage"),
    pytest.param("SELECT length(x) FROM (SELECT fts3_tokenizer('simple') AS x)", id="Unterabfrage-in-FROM"),
    pytest.param("WITH c AS (SELECT fts3_tokenizer('simple') AS t) SELECT t FROM c", id="CTE"),
    pytest.param("SELECT A FROM Probe WHERE fts3_tokenizer('simple') IS NOT NULL", id="WHERE"),
    pytest.param("INSERT INTO Probe VALUES (hex(fts3_tokenizer('simple')), 9)", id="INSERT"),
    pytest.param("UPDATE Probe SET A = hex(fts3_tokenizer('simple'))", id="UPDATE"),
    pytest.param("CREATE TABLE Neu (A, CHECK (fts3_tokenizer('simple') IS NOT NULL))", id="CHECK-Bedingung"),
    pytest.param("CREATE INDEX INeu ON Probe (A) WHERE fts3_tokenizer('simple') IS NOT NULL",
                 id="Teilindex"),
])
def test_console_denies_fts3_tokenizer(console_db, sql, write):
    before = _state(console_db)
    assert _run(console_db, sql, write) == denied(write)
    assert _state(console_db) == before


@BOTH_MODES
@pytest.mark.parametrize("sql", [
    pytest.param("SELECT load_extension('x')", id="ein-Argument"),
    pytest.param("SELECT Load_Extension('x', 'y')", id="zwei-Argumente-gemischt"),
    pytest.param("SELECT hex(load_extension('x'))", id="verschachtelt"),
])
def test_console_denies_load_extension(console_db, sql, write):
    assert _run(console_db, sql, write) == denied(write)


@requires_fts3_tokenizer
def test_denied_function_does_not_stop_the_other_commands_while_reading(console_db):
    result = _run(console_db, "SELECT 1; SELECT fts3_tokenizer('simple'); SELECT 2")
    assert result["success_count"] == 2
    assert result["errors"] == [f"Befehl 2: {READONLY}"]
    assert result["output"] == "1\n1\n2\n2"


@requires_fts3_tokenizer
def test_denied_function_ends_the_request_while_writing(console_db):
    result = _run(console_db, "SELECT 1; SELECT fts3_tokenizer('simple'); SELECT 2", write=True)
    assert result == denied(True, number=2, total=3)


@requires_fts3_tokenizer
@BOTH_MODES
def test_view_calling_fts3_tokenizer_cannot_be_queried_in_the_console(console_db, write):
    """Ein per Konsole angelegter VIEW prüft SQLite erst beim Abfragen. Dort bleibt der Aufruf
    wirkungslos: der Authorizer lehnt ihn ab, oder SQLite selbst (``unsafe use of
    fts3_tokenizer()`` – die Funktion ist nur für Aufrufe auf oberster Ebene gedacht)."""
    created = _run(console_db, "CREATE VIEW VTok AS SELECT fts3_tokenizer('simple') AS t", write=True)
    assert (created["success_count"], created["errors"]) == (1, [])
    result = _run(console_db, "SELECT * FROM VTok", write)
    assert result["success_count"] == 0
    assert result["errors"][0] in (f"Befehl 1: {CONSOLE_DENIED if write else READONLY}",
                                   "Befehl 1: unsafe use of fts3_tokenizer()")
    assert result["output"] == ""


@BOTH_MODES
def test_other_functions_stay_allowed(console_db, write):
    result = _run(console_db, "SELECT upper('abc') AS u, length('abc') AS l, "
                              "json_extract('{\"a\": [1, 2]}', '$.a[1]') AS j, "
                              "hex(randomblob(4)) IS NOT NULL AS r, length(zeroblob(10)) AS z", write)
    assert result["errors"] == []
    assert result["output"] == "u;l;j;r;z\nABC;3;2;1;10"


@requires_fts5
def test_fts5_tables_can_be_created_and_queried(console_db):
    result = _run(console_db, "CREATE VIRTUAL TABLE Volltext USING fts5(Body); "
                              "INSERT INTO Volltext VALUES ('hallo welt'), ('guten tag'); "
                              "SELECT Body FROM Volltext WHERE Volltext MATCH 'welt'", write=True)
    assert (result["success_count"], result["errors"]) == (3, [])
    assert result["output"] == "Body\nhallo welt"


@BOTH_MODES
def test_http_denials_arrive_as_json(server_port, auto_login, write):
    resp = _post_sql(server_port, "PRAGMA optimize; SELECT 1; SELECT load_extension('x'); "
                                  "PRAGMA table_info(Contacts)", auto_login, write)
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    body = json.loads(resp.text)
    if write:                                        # der erste Fehler beendet die Anfrage, alles ist zurückgenommen
        assert body == denied(True, number=1, total=4)
    else:                                            # jeder Befehl für sich: die beiden erlaubten laufen
        assert (body["success_count"], body["total"]) == (2, 4)
        assert body["errors"] == [f"Befehl 1: {READONLY}", f"Befehl 3: {READONLY}"]
        assert body["output"].startswith("1\n1\ncid;name;type;notnull;dflt_value;pk\n")
    assert _stat_tables(auto_login.db) == []


# ── Teil 3: alles andere bleibt im Schreibmodus erlaubt ──────────────────────
# BEGIN, COMMIT, ROLLBACK und SAVEPOINT nicht mehr: Alle Befehle laufen in der einen Transaktion der Anfrage
# (tests/test_sql_console_modes.py).
@pytest.mark.parametrize("sql, check, expected", [
    pytest.param("INSERT INTO Kind VALUES (5, 1); REPLACE INTO Kind VALUES (5, NULL)",
                 "SELECT ID, Eltern_ID FROM Kind", [(5, None)], id="REPLACE"),
    pytest.param("CREATE INDEX IProbeB ON Probe (B)",
                 "SELECT name FROM sqlite_master WHERE name = 'IProbeB'", [("IProbeB",)], id="CREATE-INDEX"),
    pytest.param("DROP INDEX IProbe",
                 "SELECT name FROM sqlite_master WHERE name = 'IProbe'", [], id="DROP-INDEX"),
    pytest.param("CREATE VIEW VProbe AS SELECT A FROM Probe WHERE B > 1",
                 "SELECT A FROM VProbe", [("y",)], id="CREATE-VIEW"),
    pytest.param("CREATE VIEW VProbe AS SELECT A FROM Probe; DROP VIEW VProbe",
                 "SELECT name FROM sqlite_master WHERE name = 'VProbe'", [], id="DROP-VIEW"),
    pytest.param("ALTER TABLE Probe RENAME TO Probe2",
                 "SELECT A, B FROM Probe2 ORDER BY B", [("x", 1), ("y", 2)], id="ALTER-RENAME"),
    pytest.param("ALTER TABLE Probe ADD COLUMN C TEXT DEFAULT 'n'",
                 "SELECT A, C FROM Probe ORDER BY B", [("x", "n"), ("y", "n")], id="ALTER-ADD-COLUMN"),
    pytest.param("ALTER TABLE Probe DROP COLUMN B",
                 "SELECT * FROM Probe ORDER BY A", [("x",), ("y",)], id="ALTER-DROP-COLUMN",
                 marks=requires_drop_column),
    pytest.param("WITH t AS (SELECT 3 AS n) INSERT INTO Probe SELECT 'w', n FROM t",
                 "SELECT A, B FROM Probe WHERE A = 'w'", [("w", 3)], id="WITH-INSERT"),
    pytest.param("EXPLAIN SELECT * FROM Probe",
                 "SELECT COUNT(*) FROM Probe", [(2,)], id="EXPLAIN"),
    pytest.param("EXPLAIN QUERY PLAN SELECT * FROM Probe WHERE A = 'x'",
                 "SELECT COUNT(*) FROM Probe", [(2,)], id="EXPLAIN-QUERY-PLAN"),
    pytest.param("ANALYZE",
                 "SELECT name FROM sqlite_master WHERE name = 'sqlite_stat1'", [("sqlite_stat1",)],
                 id="ANALYZE"),
    pytest.param("REINDEX",
                 "SELECT COUNT(*) FROM Probe", [(2,)], id="REINDEX"),
    pytest.param("INSERT INTO Probe SELECT A || '2', B + 10 FROM Probe",
                 "SELECT A, B FROM Probe WHERE B > 10 ORDER BY B", [("x2", 11), ("y2", 12)],
                 id="INSERT-SELECT"),
])
def test_console_still_runs_these_commands_in_write_mode(console_db, sql, check, expected):
    result = _run(console_db, sql, write=True)
    assert (result["success_count"], result["errors"]) == (_count(sql), [])
    assert result["total"] == _count(sql)
    assert _query(console_db, check) == expected


@BOTH_MODES
@pytest.mark.parametrize("sql, output", [
    pytest.param("SELECT A, B FROM Probe ORDER BY B", "A;B\nx;1\ny;2", id="SELECT"),
    pytest.param("select A from Probe where B = 2", "A\ny", id="select-klein"),
    pytest.param("  \n SELECT 1 AS n", "n\n1", id="SELECT-nach-Leerraum"),
    pytest.param("WITH t AS (SELECT 1 AS a) SELECT a FROM t", "a\n1", id="WITH"),
    pytest.param("PRAGMA table_info(Probe)", "cid;name;type;notnull;dflt_value;pk\n0;A;TEXT;0;;0\n1;B;INTEGER;0;;0",
                 id="PRAGMA"),
])
def test_every_command_with_columns_prints_a_result(console_db, sql, output, write):
    result = _run(console_db, sql, write)
    assert result["errors"] == []
    assert result["output"] == output


@BOTH_MODES
def test_explain_prints_its_program(console_db, write):
    result = _run(console_db, "EXPLAIN SELECT 1", write)
    assert result["errors"] == []
    lines = result["output"].split("\n")
    assert lines[0] == "addr;opcode;p1;p2;p3;p4;p5;comment" and len(lines) > 2
