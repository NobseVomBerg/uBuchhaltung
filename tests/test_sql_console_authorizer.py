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
  3. alles andere bleibt erlaubt: DML, DDL, Transaktionen, ``WITH``, EXPLAIN, ANALYZE …

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
from tests.test_sql_console import CONSOLE_DENIED, _post_sql, _query, _run

DENIED = {"success_count": 0, "total": 1, "errors": [f"Befehl 1: {CONSOLE_DENIED}"], "output": ""}

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
@pytest.mark.parametrize("name", READ_PRAGMAS)
def test_read_pragma_without_argument_is_allowed_and_changes_nothing(console_db, name):
    before = _state(console_db)
    forms = [f"PRAGMA {name}", f"PRAGMA {name.upper()}"]
    if _has_table_function(name):
        forms += [f"SELECT * FROM pragma_{name}", f"SELECT * FROM PRAGMA_{name.upper()}"]
    for sql in forms:
        result = _run(console_db, sql)
        assert (result["success_count"], result["errors"]) == (1, []), sql
    if not _has_table_function(name):       # kennt SQLite die Form nicht: dessen Fehler, nicht „nicht erlaubt“
        result = _run(console_db, f"SELECT * FROM pragma_{name}")
        assert result["success_count"] == 0
        assert result["errors"] == [f"Befehl 1: no such table: pragma_{name}"]
    assert _state(console_db) == before


@pytest.mark.parametrize("sql", [
    pytest.param("PRAGMA USER_VERSION", id="Grossbuchstaben"),
    pytest.param("pragma User_Version", id="gemischt"),
    pytest.param("PRAGMA Table_Info(Probe)", id="table_info-gemischt"),
    pytest.param("PRAGMA FOREIGN_KEYS = ON", id="foreign_keys-gross"),
    pytest.param("Pragma Integrity_Check", id="integrity_check-gemischt"),
    pytest.param("SELECT * FROM PRAGMA_TABLE_INFO('Probe')", id="Tabellenfunktion-gross"),
    pytest.param("SELECT * FROM Pragma_User_Version", id="Tabellenfunktion-gemischt"),
])
def test_pragma_names_are_matched_case_insensitively(console_db, sql):
    result = _run(console_db, sql)
    assert (result["success_count"], result["errors"]) == (1, [])


@pytest.mark.parametrize("name", READ_PRAGMAS)
def test_read_pragma_with_a_value_is_denied(console_db, name):
    before = _state(console_db)
    for sql in (f"PRAGMA {name} = 1", f"PRAGMA {name}(1)", f"PRAGMA main.{name} = 1",
                f"PRAGMA {name.upper()} = 1", f"PRAGMA \"{name}\" = 1"):
        assert _run(console_db, sql) == DENIED, sql
    assert _state(console_db) == before


@pytest.mark.parametrize("name", READ_WITH_FUNCTION)
def test_read_pragma_table_function_cannot_take_a_value(console_db, name):
    """Über ``pragma_<name>(…)`` lässt sich kein Wert setzen: SQLite lehnt das Argument ab
    (kein Argument vorgesehen bzw. kein Schema namens 1) oder der Authorizer."""
    before = _state(console_db)
    result = _run(console_db, f"SELECT * FROM pragma_{name}(1)")
    assert result["success_count"] == 0
    assert len(result["errors"]) == 1
    assert result["output"] == ""
    assert _state(console_db) == before


# ── Teil 1: PRAGMAs – Abfrage-PRAGMAs mit Argument ───────────────────────────
@pytest.mark.parametrize("name", ARG_PRAGMAS)
def test_query_pragma_is_allowed_with_and_without_argument(console_db, name):
    arg = ARG_FOR.get(name, "1")
    before = _state(console_db)
    for sql in (f"PRAGMA {name}", f"PRAGMA {name}({arg})", f"PRAGMA {name} = {arg}",
                f"PRAGMA main.{name}({arg})", f"PRAGMA {name.upper()}({arg})"):
        result = _run(console_db, sql)
        assert (result["success_count"], result["errors"]) == (1, []), sql
    assert _state(console_db) == before


@pytest.mark.parametrize("name", ARG_WITH_FUNCTION)
def test_query_pragma_table_function_is_allowed(console_db, name):
    arg = TABLE_ARG_FOR.get(name, ARG_FOR.get(name, "1"))
    forms = [f"SELECT * FROM pragma_{name}"]
    if name in ARG_FOR and name not in ("foreign_keys", "defer_foreign_keys"):
        forms.append(f"SELECT * FROM pragma_{name}('{arg}')")
    for sql in forms:
        result = _run(console_db, sql)
        assert (result["success_count"], result["errors"]) == (1, []), sql


def test_foreign_keys_pragma_still_takes_effect(console_db):
    """Erlaubt heißt hier: es wirkt auf die Verbindung der Konsole (OFF/ON schaltet die Prüfung)."""
    off = _run(console_db, "PRAGMA foreign_keys = OFF; INSERT INTO Kind VALUES (1, 99)")
    assert (off["success_count"], off["errors"]) == (2, [])

    on = _run(console_db, "PRAGMA foreign_keys = ON; INSERT INTO Kind VALUES (2, 99)")
    assert on["success_count"] == 1
    assert on["errors"] == ["Befehl 2: FOREIGN KEY constraint failed"]
    assert _query(console_db, "SELECT ID, Eltern_ID FROM Kind ORDER BY ID") == [(1, 99)]


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
def test_console_denies_pragma_and_changes_nothing(console_db, sql):
    before = _state(console_db)
    assert _run(console_db, sql) == DENIED
    assert _state(console_db) == before


@pytest.mark.parametrize("name", UNLISTED_PRAGMAS)
def test_every_pragma_sqlite_knows_but_the_lists_do_not_is_denied(console_db, name):
    """Läuft über ``pragma_pragma_list`` der laufenden SQLite-Version – ein künftig neues PRAGMA
    fällt ohne Zutun unter die Sperre, mit und ohne Argument, auch als Tabellenfunktion."""
    before = _state(console_db)
    forms = [f"PRAGMA {name}", f"PRAGMA {name} = 1", f"PRAGMA {name}(1)", f"PRAGMA main.{name}"]
    if _has_table_function(name):
        forms.append(f"SELECT * FROM pragma_{name}")
    for sql in forms:
        assert _run(console_db, sql) == DENIED, sql
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
def test_denied_optimize_creates_no_statistics(console_db, tmp_path, sql):
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

    result = _run(console_db, f"{prime}; {sql}")
    assert (result["success_count"], result["errors"]) == (1, [f"Befehl 2: {CONSOLE_DENIED}"])
    assert _stat_tables(console_db) == []


def test_denied_incremental_vacuum_keeps_the_free_pages(console_db, tmp_path):
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
        assert _run(console_db, sql) == DENIED
    assert _query(console_db, "PRAGMA freelist_count") == [(free,)]
    assert _state(console_db) == before


def test_denied_wal_checkpoint_leaves_the_wal_file_alone(console_db):
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
            assert _run(console_db, sql) == DENIED
            assert os.path.getsize(wal) == wal_size, sql
            assert installation.file_digest(path) == main_digest, sql

        keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")           # Kontrolle: ohne Sperre leer
        assert os.path.getsize(wal) == 0


def test_denied_query_only_does_not_make_the_connection_read_only(console_db):
    result = _run(console_db, "PRAGMA query_only = 1; INSERT INTO Probe VALUES ('z', 3)")
    assert result["success_count"] == 1
    assert result["errors"] == [f"Befehl 1: {CONSOLE_DENIED}"]
    assert _query(console_db, "SELECT A FROM Probe WHERE B = 3") == [("z",)]


def test_denied_journal_mode_leaves_the_rollback_journal_mode(console_db):
    before = _state(console_db)
    assert _run(console_db, "PRAGMA journal_mode = WAL") == DENIED
    assert not os.path.exists(console_db.db_name + "-wal")
    assert _query(console_db, "PRAGMA journal_mode") == [("delete",)]
    assert _state(console_db) == before


@pytest.mark.parametrize("sql", [
    pytest.param("PRAGMA user_version = 7", id="user_version"),
    pytest.param("PRAGMA main.user_version = 7", id="user_version-main"),
    pytest.param("PRAGMA schema_version = 1", id="schema_version"),
    pytest.param("PRAGMA application_id = 7", id="application_id"),
])
def test_denied_header_pragmas_leave_the_header_values(console_db, sql):
    names = ("user_version", "schema_version", "application_id")
    before = [_query(console_db, f"PRAGMA {n}") for n in names]
    assert _run(console_db, sql) == DENIED
    assert [_query(console_db, f"PRAGMA {n}") for n in names] == before


def test_view_over_a_denied_pragma_function_stays_denied_when_queried(console_db):
    result = _run(console_db, "CREATE VIEW Opt AS SELECT * FROM pragma_optimize; SELECT * FROM Opt")
    assert result["success_count"] == 1                    # die Sicht entsteht, ihre Abfrage bleibt gesperrt
    assert result["errors"] == [f"Befehl 2: {CONSOLE_DENIED}"]
    assert result["output"] == ""
    assert _stat_tables(console_db) == []


def test_view_over_an_allowed_pragma_function_can_be_queried(console_db):
    result = _run(console_db, "CREATE VIEW Version AS SELECT * FROM pragma_user_version; SELECT * FROM Version")
    assert (result["success_count"], result["errors"]) == (2, [])
    assert result["output"] == f"user_version\n{installation.user_version(console_db.db_name)}"


def test_pragma_denial_does_not_stop_the_other_commands(console_db):
    result = _run(console_db, "PRAGMA optimize; INSERT INTO Probe VALUES ('z', 3); "
                              "PRAGMA table_info(Probe); PRAGMA user_version = 5; SELECT 1")
    assert result["success_count"] == 3
    assert result["total"] == 5
    assert result["errors"] == [f"Befehl 1: {CONSOLE_DENIED}", f"Befehl 4: {CONSOLE_DENIED}"]
    assert result["output"] == "1\n1"
    assert _query(console_db, "SELECT A FROM Probe WHERE B = 3") == [("z",)]


def test_pragma_table_functions_show_the_data_they_are_allowed_to(console_db):
    result = _run(console_db, "SELECT name, type FROM pragma_table_info('Probe') ORDER BY cid")
    assert result["output"] == "name;type\nA;TEXT\nB;INTEGER"
    result = _run(console_db, "SELECT user_version FROM pragma_user_version")
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
def test_console_denies_fts3_tokenizer(console_db, sql):
    before = _state(console_db)
    assert _run(console_db, sql) == DENIED
    assert _state(console_db) == before


@pytest.mark.parametrize("sql", [
    pytest.param("SELECT load_extension('x')", id="ein-Argument"),
    pytest.param("SELECT Load_Extension('x', 'y')", id="zwei-Argumente-gemischt"),
    pytest.param("SELECT hex(load_extension('x'))", id="verschachtelt"),
])
def test_console_denies_load_extension(console_db, sql):
    assert _run(console_db, sql) == DENIED


@requires_fts3_tokenizer
def test_denied_function_does_not_stop_the_other_commands(console_db):
    result = _run(console_db, "SELECT 1; SELECT fts3_tokenizer('simple'); SELECT 2")
    assert result["success_count"] == 2
    assert result["errors"] == [f"Befehl 2: {CONSOLE_DENIED}"]
    assert result["output"] == "1\n1\n2\n2"


@requires_fts3_tokenizer
def test_view_calling_fts3_tokenizer_cannot_be_queried_in_the_console(console_db):
    """Ein per Konsole angelegter VIEW prüft SQLite erst beim Abfragen. Dort bleibt der Aufruf
    wirkungslos: der Authorizer lehnt ihn ab, oder SQLite selbst (``unsafe use of
    fts3_tokenizer()`` – die Funktion ist nur für Aufrufe auf oberster Ebene gedacht)."""
    result = _run(console_db, "CREATE VIEW VTok AS SELECT fts3_tokenizer('simple') AS t; "
                              "SELECT * FROM VTok")
    assert result["success_count"] == 1
    assert result["total"] == 2
    assert result["errors"] in ([f"Befehl 2: {CONSOLE_DENIED}"],
                                ["Befehl 2: unsafe use of fts3_tokenizer()"])
    assert result["output"] == ""


def test_other_functions_stay_allowed(console_db):
    result = _run(console_db, "SELECT upper('abc') AS u, length('abc') AS l, "
                              "json_extract('{\"a\": [1, 2]}', '$.a[1]') AS j, "
                              "hex(randomblob(4)) IS NOT NULL AS r, length(zeroblob(10)) AS z")
    assert result["errors"] == []
    assert result["output"] == "u;l;j;r;z\nABC;3;2;1;10"


@requires_fts5
def test_fts5_tables_can_be_created_and_queried(console_db):
    result = _run(console_db, "CREATE VIRTUAL TABLE Volltext USING fts5(Body); "
                              "INSERT INTO Volltext VALUES ('hallo welt'), ('guten tag'); "
                              "SELECT Body FROM Volltext WHERE Volltext MATCH 'welt'")
    assert (result["success_count"], result["errors"]) == (3, [])
    assert result["output"] == "Body\nhallo welt"


def test_http_denials_arrive_as_json(server_port, auto_login):
    resp = _post_sql(server_port, "PRAGMA optimize; SELECT 1; SELECT load_extension('x'); "
                                  "PRAGMA table_info(Contacts)", auto_login)
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("application/json")
    assert json.loads(resp.text) == {"success_count": 2, "total": 4, "output": "1\n1",
                                     "errors": [f"Befehl 1: {CONSOLE_DENIED}", f"Befehl 3: {CONSOLE_DENIED}"]}
    assert _stat_tables(auto_login.db) == []


# ── Teil 3: alles andere bleibt erlaubt ──────────────────────────────────────
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
    pytest.param("BEGIN; INSERT INTO Probe VALUES ('b', 3); COMMIT",
                 "SELECT A FROM Probe WHERE B = 3", [("b",)], id="BEGIN-COMMIT"),
    pytest.param("BEGIN; INSERT INTO Probe VALUES ('b', 3); ROLLBACK",
                 "SELECT A FROM Probe WHERE B = 3", [], id="BEGIN-ROLLBACK"),
    pytest.param("SAVEPOINT s; INSERT INTO Probe VALUES ('s', 4); ROLLBACK TO s; "
                 "INSERT INTO Probe VALUES ('t', 5); RELEASE s",
                 "SELECT A FROM Probe WHERE B > 3", [("t",)], id="SAVEPOINT"),
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
def test_console_still_runs_these_commands(console_db, sql, check, expected):
    result = _run(console_db, sql)
    assert (result["success_count"], result["errors"]) == (_count(sql), [])
    assert result["total"] == _count(sql)
    assert _query(console_db, check) == expected


@pytest.mark.parametrize("sql, output", [
    pytest.param("SELECT A, B FROM Probe ORDER BY B", "A;B\nx;1\ny;2", id="SELECT"),
    pytest.param("select A from Probe where B = 2", "A\ny", id="select-klein"),
    pytest.param("  \n SELECT 1 AS n", "n\n1", id="SELECT-nach-Leerraum"),
    pytest.param("WITH t AS (SELECT 1 AS a) SELECT a FROM t", "", id="WITH-ohne-Ausgabe"),
    pytest.param("EXPLAIN SELECT 1", "", id="EXPLAIN-ohne-Ausgabe"),
    pytest.param("PRAGMA table_info(Probe)", "", id="PRAGMA-ohne-Ausgabe"),
])
def test_only_commands_starting_with_select_print_a_result(console_db, sql, output):
    result = _run(console_db, sql)
    assert result["errors"] == []
    assert result["output"] == output
