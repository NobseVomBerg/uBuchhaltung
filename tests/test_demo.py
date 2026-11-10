# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Demo-Modus (UBUCHHALTUNG_DEMO): Seeding nur in leeres Verzeichnis, nie in
bestehende Daten, Banner nur wenn wirklich Demo."""
import os
import sqlite3

import pytest

import demo
import userctx
from server.pages import Header1


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(userctx, "DATA_ROOT", str(root))
    userctx._mode_cache.clear()
    monkeypatch.delenv(demo.ENV_VAR, raising=False)
    yield root
    userctx._mode_cache.clear()


def _count(root, table):
    con = sqlite3.connect(str(root / "buch.db"))
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _tree(root):
    """Inhalt unterhalb von root: {relativer Pfad: Dateiinhalt bzw. None bei Ordnern}."""
    return {path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in sorted(root.rglob("*"))}


def _add_test_artifacts(root):
    """tests/ wie nach einem pytest-Lauf im Datenverzeichnis (data/tests/tmp/…)."""
    artifact = root / "tests" / "tmp" / "irgendwas"
    artifact.mkdir(parents=True)
    (artifact / "probe.txt").write_text("Testartefakt", encoding="utf-8")
    (root / "tests" / "cache").mkdir()


def test_disabled_without_env(data_root):
    logs = []
    assert demo.prepare(log=logs.append) == demo.DISABLED
    assert not data_root.exists()
    assert logs == []


def test_seeds_missing_directory(data_root, monkeypatch):
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    assert userctx.get_mode() == "single"          # Ersteinrichtung übersprungen
    assert (data_root / demo.MARKER_FILE).is_file()
    assert _count(data_root, "Contacts") > 0
    assert _count(data_root, "Invoices") > 0
    from db import Database
    assert not Database(str(data_root / "buch.db")).is_first_run()


def test_seeds_empty_existing_directory(data_root, monkeypatch):
    data_root.mkdir()
    monkeypatch.setenv(demo.ENV_VAR, "true")
    assert demo.prepare(log=lambda m: None) == demo.SEEDED


@pytest.mark.parametrize("entries", [("tests",), ("lost+found",), ("lost+found", "tests")])
def test_seeds_directory_holding_only_ignored_entries(data_root, monkeypatch, entries):
    """Nur tests/ (Artefakte des letzten pytest-Laufs, data/tests/) und lost+found machen
    ein Verzeichnis nicht 'belegt' – sie bleiben beim Befüllen unangetastet."""
    data_root.mkdir()
    for name in entries:
        (data_root / name).mkdir()
    if "tests" in entries:
        _add_test_artifacts(data_root)
    before = {path: content for path, content in _tree(data_root).items()
              if path.split("/")[0] in entries}
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    assert (data_root / demo.MARKER_FILE).is_file()
    assert _count(data_root, "Invoices") > 0
    after = {path: content for path, content in _tree(data_root).items()
             if path.split("/")[0] in entries}
    assert after == before


@pytest.mark.parametrize("name", ["config.json", "buch.db", "auth.db", "sql_operations.log"])
def test_refuses_test_artifacts_plus_a_real_file(data_root, monkeypatch, name):
    _add_test_artifacts(data_root)
    (data_root / name).write_text("{}")
    before = _tree(data_root)
    monkeypatch.setenv(demo.ENV_VAR, "1")
    logs = []
    assert demo.prepare(log=logs.append) == demo.REFUSED
    assert _tree(data_root) == before                  # nichts geladen, nichts angelegt
    assert not (data_root / demo.MARKER_FILE).exists()
    assert any("WARNUNG" in m for m in logs)


def test_refuses_a_tests_folder_below_the_top_level(data_root, monkeypatch):
    # Übergangen wird nur der Eintrag tests/ direkt im Datenverzeichnis
    (data_root / "Belege" / "tests").mkdir(parents=True)
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.prepare(log=lambda m: None) == demo.REFUSED
    assert not (data_root / demo.MARKER_FILE).exists()


def test_second_start_does_not_reseed(data_root, monkeypatch):
    monkeypatch.setenv(demo.ENV_VAR, "1")
    demo.prepare(log=lambda m: None)
    before = _count(data_root, "Invoices")
    assert demo.prepare(log=lambda m: None) == demo.RESUMED
    assert _count(data_root, "Invoices") == before


def test_refuses_existing_database(data_root, monkeypatch):
    """Echte Buchhaltung: bestehende DB bleibt unangetastet."""
    from db import Database
    data_root.mkdir()
    db_path = str(data_root / "buch.db")
    Database(db_path)                               # leere, aber vorhandene DB
    monkeypatch.setenv(demo.ENV_VAR, "1")
    logs = []
    assert demo.prepare(log=logs.append) == demo.REFUSED
    assert _count(data_root, "Contacts") == 0
    assert _count(data_root, "Invoices") == 0
    assert not (data_root / demo.MARKER_FILE).exists()
    assert any("WARNUNG" in m for m in logs)


@pytest.mark.parametrize("name", ["config.json", "auth.db", "sql_operations.log"])
def test_refuses_any_existing_file(data_root, monkeypatch, name):
    data_root.mkdir()
    (data_root / name).write_text("{}")
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.prepare(log=lambda m: None) == demo.REFUSED
    assert not (data_root / "buch.db").exists()
    assert not (data_root / demo.MARKER_FILE).exists()


def test_refuses_multi_user_data(data_root, monkeypatch):
    (data_root / "users" / "alice").mkdir(parents=True)
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.prepare(log=lambda m: None) == demo.REFUSED


def test_banner_only_in_demo_mode(data_root, monkeypatch):
    assert demo.BANNER_TEXT not in Header1()
    # Variable gesetzt, aber echte Daten (kein Marker) → kein Banner
    data_root.mkdir()
    (data_root / "buch.db").write_text("")
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.BANNER_TEXT not in Header1()
    # echtes Demo-Verzeichnis → Banner
    (data_root / "buch.db").unlink()
    (data_root / demo.MARKER_FILE).write_text("x")
    assert demo.is_active()
    assert "demoBanner" in Header1() and "Demo-Modus" in Header1()
    # Marker vorhanden, Variable weg → kein Banner
    monkeypatch.delenv(demo.ENV_VAR)
    assert demo.BANNER_TEXT not in Header1()
