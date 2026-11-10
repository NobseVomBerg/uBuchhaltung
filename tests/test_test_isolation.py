# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Wächter für die Testisolation: kein Test schreibt ins echte <Projekt>/data.

Die autouse-Fixture ``_isolated_data_root`` (conftest.py) biegt ``userctx.DATA_ROOT``
und ``auth.AUTH_DB`` je Test auf ein Verzeichnis unter ``tmp_path``. Ohne sie landeten
die SQL-Protokolle jedes Tests im ``./data`` des Projektordners – auf dem
Entwicklungsrechner die echten Bücher. Zusätzlich vergleicht ``_real_data_untouched``
das echte data/ vor und nach dem ganzen Lauf (Hilfen in ``tests/data_guard.py``).

Alle Inhalte sind synthetische Testdaten.
"""
import os
from decimal import Decimal

import pytest

import auth
import userctx
from db import Database
from tests import data_guard

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DATA = os.path.join(PROJECT_ROOT, "data")


def _is_below(path, base):
    """Liegt path in base (oder ist base)? Symlinks und '..' aufgelöst."""
    path, base = os.path.realpath(path), os.path.realpath(base)
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:                       # z. B. verschiedene Laufwerke unter Windows
        return False


def _file_state(path):
    """(Größe, mtime_ns) oder None, wenn es die Datei nicht gibt."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Datenwurzel je Test ──────────────────────────────────────────────────────
def test_data_root_is_below_tmp_path_and_not_the_project_data(tmp_path):
    root = userctx.DATA_ROOT
    assert os.path.isabs(root)                   # unabhängig vom Arbeitsverzeichnis
    assert _is_below(root, tmp_path)
    assert os.path.realpath(root) != os.path.realpath(REAL_DATA)
    assert os.path.realpath(root) != os.path.realpath("./data")


def test_data_root_is_not_created_upfront():
    # Wie bei einer Erstinstallation: Der Code legt das Verzeichnis selbst an
    assert not os.path.exists(userctx.DATA_ROOT)


@pytest.mark.parametrize("run", [1, 2, 3])
def test_every_test_gets_its_own_data_root(run):
    """Jeder Test beginnt mit einer leeren Datenwurzel. Der Pfad selbst darf sich
    wiederholen: Ältere pytest-Versionen (bis mindestens 9.0.3) vergeben den Namen
    eines schon gelöschten Testverzeichnisses erneut."""
    assert not os.path.exists(userctx.DATA_ROOT)
    os.makedirs(userctx.DATA_ROOT)
    with open(os.path.join(userctx.DATA_ROOT, "spur.txt"), "w", encoding="utf-8") as f:
        f.write(f"Lauf {run}")                           # der nächste Lauf darf das nicht vorfinden


def test_paths_derived_from_data_root_and_auth_db_stay_below_tmp_path(tmp_path):
    for path in (userctx.config_path(), userctx.user_data_dir(), userctx.user_db_path(),
                 userctx.user_subdir("logos", create=False), auth.AUTH_DB):
        assert _is_below(path, userctx.DATA_ROOT), path
        assert _is_below(path, tmp_path), path


def test_default_database_is_created_below_tmp_path(tmp_path):
    db = Database()                              # wie der Server pro Anfrage: Pfad aus userctx
    assert _is_below(db.db_name, tmp_path)
    assert os.path.isfile(db.db_name)


def test_app_mode_starts_neutral():
    assert userctx.get_mode() is None
    assert userctx.auth_enabled() is False
    assert userctx.get_user() is None


def test_isolation_fixtures_are_active_for_every_test(request):
    assert {"_isolated_data_root", "_neutral_app_mode", "_neutral_environment",
            "_real_data_untouched"} <= set(request.fixturenames)


def test_app_environment_variables_do_not_leak_into_tests():
    # Auch wenn der Entwickler sie in der Shell gesetzt hat (UBUCHHALTUNG_DEMO=1 pytest …)
    assert "UBUCHHALTUNG_ALLOWED_HOSTS" not in os.environ
    assert "UBUCHHALTUNG_DEMO" not in os.environ


# ── protokollierter Schreibvorgang ───────────────────────────────────────────
def test_logged_write_creates_sql_log_below_tmp_path_not_in_project_data(db_with_coa, tmp_path):
    real_logs = [os.path.join(REAL_DATA, name)
                 for name in ("sql_operations.log", "sql_operations.sql")]
    before = [_file_state(path) for path in real_logs]

    db_with_coa.insert_booking("2026-03-01", Decimal("10.00"), text="Isolationsprobe",
                               booking_type="entry", log_description="Testisolation: Wächtereintrag")

    log = os.path.join(userctx.user_data_dir(), "sql_operations.log")
    replay = os.path.join(userctx.user_data_dir(), "sql_operations.sql")
    assert _is_below(log, tmp_path) and _is_below(replay, tmp_path)
    assert "Testisolation: Wächtereintrag" in _read(log)
    assert "Isolationsprobe" in _read(replay)
    # das echte data/ blieb unberührt (gab es die Dateien nicht, gibt es sie weiter nicht)
    assert [_file_state(path) for path in real_logs] == before


# ── Ablage der Testläufe unter data/tests/ ───────────────────────────────────
def test_tmp_path_lies_below_data_tests_tmp(tmp_path, request):
    expected = os.path.join(REAL_DATA, "tests", "tmp")
    if request.config.option.basetemp or os.environ.get("PYTEST_DEBUG_TEMPROOT") != expected:
        pytest.skip("--basetemp oder PYTEST_DEBUG_TEMPROOT von außen vorgegeben "
                    "(oder der Projektordner ist schreibgeschützt)")
    assert _is_below(tmp_path, expected)


def test_pytest_cache_lies_below_data_tests_cache(request):
    config = request.config
    if getattr(config, "cache", None) is None or config.getini("cache_dir") != "data/tests/cache":
        pytest.skip("Cache-Plugin abgeschaltet oder cache_dir überschrieben")
    assert _is_below(config.cache.mkdir("isolation"), os.path.join(REAL_DATA, "tests", "cache"))


# ── der Wächter selbst (tests/data_guard.py) ─────────────────────────────────
@pytest.fixture
def guarded(tmp_path):
    """Nachbau eines data/-Ordners mit Testartefakten und einem 'echten' Protokoll."""
    root = tmp_path / "echtes_data"
    (root / "tests" / "tmp").mkdir(parents=True)
    (root / "tests" / "tmp" / "lauf.txt").write_text("x")
    (root / "pending_imports").mkdir()
    (root / "sql_operations.log").write_text("alt")
    return root


def test_guard_snapshot_skips_the_tests_folder_and_nothing_else(guarded, tmp_path):
    snap = data_guard.snapshot(str(guarded))
    assert set(snap) == {"pending_imports", "sql_operations.log"}
    assert snap["pending_imports"] is None
    assert snap["sql_operations.log"][0] == len("alt")
    assert data_guard.snapshot(str(tmp_path / "gibt_es_nicht")) == {}


def test_guard_does_not_skip_a_nested_folder_named_tests(guarded):
    (guarded / "Belege" / "tests").mkdir(parents=True)
    assert "Belege" + os.sep + "tests" in data_guard.snapshot(str(guarded))


def test_guard_ignores_everything_inside_the_tests_folder(guarded):
    before = data_guard.snapshot(str(guarded))
    (guarded / "tests" / "tmp" / "neu.db").write_text("neu")
    (guarded / "tests" / "cache").mkdir()
    (guarded / "tests" / "tmp" / "lauf.txt").unlink()
    assert data_guard.diff(before, data_guard.snapshot(str(guarded))) == {}


def _add_replay_log(root):
    (root / "sql_operations.sql").write_text("neu")


def _grow_log(root):
    with open(root / "sql_operations.log", "a", encoding="utf-8") as f:
        f.write("mehr")


def _touch_log(root):
    os.utime(root / "sql_operations.log", ns=(1, 1))      # gleiche Größe, anderer Zeitstempel


def _remove_log(root):
    (root / "sql_operations.log").unlink()


def _add_folder(root):
    (root / "Belege").mkdir()


def _add_pending_file(root):
    (root / "pending_imports" / "x.json").write_text("{}")


@pytest.mark.parametrize("change, group, path", [
    pytest.param(_add_replay_log, "neu", "sql_operations.sql", id="neue-Datei"),
    pytest.param(_grow_log, "geändert", "sql_operations.log", id="Datei-gewachsen"),
    pytest.param(_touch_log, "geändert", "sql_operations.log", id="nur-Zeitstempel"),
    pytest.param(_remove_log, "entfernt", "sql_operations.log", id="Datei-entfernt"),
    pytest.param(_add_folder, "neu", "Belege", id="neuer-Ordner"),
    pytest.param(_add_pending_file, "neu", os.path.join("pending_imports", "x.json"),
                 id="neue-Datei-in-Unterordner"),
])
def test_guard_detects_changes_outside_the_tests_folder(guarded, change, group, path):
    before = data_guard.snapshot(str(guarded))
    change(guarded)
    assert data_guard.diff(before, data_guard.snapshot(str(guarded))) == {group: [path]}


def test_guard_reports_no_difference_for_an_untouched_folder(guarded):
    assert data_guard.diff(data_guard.snapshot(str(guarded)),
                           data_guard.snapshot(str(guarded))) == {}


def test_guard_message_names_the_paths_and_cuts_long_lists():
    changes = {"neu": [f"d{n:02d}.json" for n in range(13)], "geändert": ["sql_operations.log"]}
    text = data_guard.message(changes, "/x/data")
    assert "/x/data" in text
    assert "d00.json" in text and "d09.json" in text
    assert "d10.json" not in text and "+3" in text
    assert "geändert: sql_operations.log" in text
