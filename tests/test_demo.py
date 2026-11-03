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
