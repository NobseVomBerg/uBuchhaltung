# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Demo-Modus (UBUCHHALTUNG_DEMO): Die Demodaten gehören dem Benutzer ``demo`` und liegen
ausschließlich unter data/users/demo/. Befüllt wird nur, wenn dieses Verzeichnis fehlt oder
leer ist und es keinen Benutzer ``demo`` gibt; andere Daten bleiben unberührt. Banner und
Demo-Anmeldung gibt es nur mit Variable UND Marker.

Das Verhalten im Server (Besucher ist ``demo``, keine Verwaltung, keine SQL-Konsole) prüft
test_demo_http.py. Das Demo-Szenario selbst (rund zwei Sekunden) laden nur
``test_seeds_the_demo_scenario`` und test_demo_seed.py; die übrigen Tests ersetzen es durch
``light_scenario``, denn sie prüfen nur, WO und WANN geschrieben wird.
"""
import os
import sqlite3
from pathlib import Path

import pytest

import auth
import demo
import demo_seed
import userctx
from db import Database
from server.pages import Header1
from tests import installation
from tests.installation import tree


@pytest.fixture
def data_root():
    """Datenverzeichnis des Tests (conftest.py: unter tmp_path, vom Code selbst angelegt)."""
    return Path(userctx.DATA_ROOT)


@pytest.fixture
def light_scenario(monkeypatch):
    """Befüllt wird nur mit den Testdaten (seed_data/test), ohne das Demo-Szenario."""
    monkeypatch.setattr(demo_seed, "load_demo_scenario", lambda db: None)


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setenv(demo.ENV_VAR, "1")


def _demo_dir(root):
    return root / "users" / "demo"


def _count(root, table):
    con = sqlite3.connect(str(_demo_dir(root) / "buch.db"))
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _add_test_artifacts(root):
    """tests/ wie nach einem pytest-Lauf im Datenverzeichnis (data/tests/tmp/…)."""
    artifact = root / "tests" / "tmp" / "irgendwas"
    artifact.mkdir(parents=True)
    (artifact / "probe.txt").write_text("Testartefakt", encoding="utf-8")
    (root / "tests" / "cache").mkdir()


def _real_installation(root):
    """Echte Bücher: Administrator ``admin`` mit automatischer Anmeldung, Datenbank und
    Dateien unter users/admin/. Liefert das Verzeichnis des Administrators."""
    installation.set_up()
    installation.open_db("admin")
    admin_dir = root / "users" / "admin"
    (admin_dir / "invoices" / "2026").mkdir(parents=True)
    (admin_dir / "invoices" / "2026" / "R26001 Probekunde.pdf").write_bytes(b"%PDF-1.7 echte Rechnung")
    (admin_dir / "Belege").mkdir()
    (admin_dir / "Belege" / "beleg.pdf").write_bytes(b"%PDF-1.7 echter Beleg")
    return admin_dir


# ── Variable ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True), (" 1 ", True),
    ("0", False), ("", False), ("no", False), ("false", False), ("off", False), ("2", False),
])
def test_requested_accepts_only_truthy_values(monkeypatch, value, expected):
    monkeypatch.setenv(demo.ENV_VAR, value)
    assert demo.requested() is expected


def test_not_requested_without_the_variable():
    assert demo.requested() is False


# ── prepare(): ohne Variable nichts ──────────────────────────────────────────
def test_disabled_without_env(data_root):
    logs = []
    assert demo.prepare(log=logs.append) == demo.DISABLED
    assert not data_root.exists()
    assert logs == []


def test_disabled_without_env_leaves_existing_data_alone(data_root):
    _real_installation(data_root)
    before = tree(data_root)
    assert demo.prepare(log=lambda m: None) == demo.DISABLED
    assert tree(data_root) == before


# ── prepare(): Befüllen ──────────────────────────────────────────────────────
def test_seeds_the_demo_scenario(data_root, demo_on):
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    assert userctx.get_mode() == "multi"           # Ersteinrichtung übersprungen
    assert (_demo_dir(data_root) / demo.MARKER_FILE).is_file()
    assert _count(data_root, "Contacts") > 0
    assert _count(data_root, "Invoices") > 0
    assert not Database(str(_demo_dir(data_root) / "buch.db")).is_first_run()


def test_seeding_a_fresh_install_writes_only_config_auth_and_users(data_root, demo_on,
                                                                   light_scenario):
    assert not data_root.exists()
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    assert sorted(os.listdir(data_root)) == ["auth.db", "config.json", "users"]
    assert os.listdir(data_root / "users") == ["demo"]
    assert installation.read_config() == {"mode": "multi"}       # keine automatische Anmeldung


def test_seeded_demo_files_live_below_users_demo(data_root, demo_on, light_scenario):
    demo.prepare(log=lambda m: None)
    entries = os.listdir(_demo_dir(data_root))
    assert demo.MARKER_FILE in entries and "buch.db" in entries
    # das SQL-Protokoll der Befüllung gehört ebenfalls dem Demo-Benutzer, nicht der Datenwurzel
    assert not (data_root / "sql_operations.log").exists()
    assert not (data_root / "buch.db").exists()


def test_seeded_demo_user_exists_and_is_no_administrator(data_root, demo_on, light_scenario):
    demo.prepare(log=lambda m: None)
    assert auth.user_exists("demo") is True
    assert auth.is_admin("demo") is False
    assert auth.count_admins() == 0
    assert [name for name, *_ in auth.list_users()] == ["demo"]
    # Das Passwort ist zufällig und bleibt unbekannt – angemeldet wird über den Türsteher
    assert not any(auth.authenticate("demo", guess) for guess in ("demo", "", "geheim123"))


def test_seeds_empty_existing_directory(data_root, demo_on, light_scenario):
    data_root.mkdir()
    assert demo.prepare(log=lambda m: None) == demo.SEEDED


@pytest.mark.parametrize("make", [
    pytest.param(lambda root: (root / "users").mkdir(parents=True), id="users-leer"),
    pytest.param(lambda root: _demo_dir(root).mkdir(parents=True), id="users-demo-leer"),
])
def test_seeds_into_an_empty_demo_directory(data_root, demo_on, light_scenario, make):
    make(data_root)
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    assert (_demo_dir(data_root) / demo.MARKER_FILE).is_file()


@pytest.mark.parametrize("entries", [("tests",), ("lost+found",), ("lost+found", "tests")])
def test_seeds_directory_holding_only_ignored_entries(data_root, demo_on, light_scenario, entries):
    """tests/ (Artefakte des letzten pytest-Laufs, data/tests/) und lost+found liegen neben
    users/ und bleiben beim Befüllen unangetastet."""
    data_root.mkdir()
    for name in entries:
        (data_root / name).mkdir()
    if "tests" in entries:
        _add_test_artifacts(data_root)
    before = {path: content for path, content in tree(data_root).items()
              if path.split(os.sep)[0] in entries}
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    assert (_demo_dir(data_root) / demo.MARKER_FILE).is_file()
    after = {path: content for path, content in tree(data_root).items()
             if path.split(os.sep)[0] in entries}
    assert after == before


# ── prepare(): Demo neben echten Daten ───────────────────────────────────────
def test_seeding_next_to_real_books_leaves_them_byte_identical(data_root, demo_on, light_scenario):
    admin_dir = _real_installation(data_root)
    admin_before = tree(admin_dir)
    config_before = (data_root / "config.json").read_bytes()
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    assert tree(admin_dir) == admin_before
    assert (data_root / "config.json").read_bytes() == config_before   # schon eingerichtet: nichts neu geschrieben
    assert (_demo_dir(data_root) / demo.MARKER_FILE).is_file()


def test_seeding_next_to_real_books_changes_nothing_outside_users_demo(data_root, demo_on,
                                                                      light_scenario):
    _real_installation(data_root)
    (data_root / "lost+found").mkdir()
    _add_test_artifacts(data_root)
    before = tree(data_root)
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    after = tree(data_root)
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    outside = {path for path in changed
               if path != "auth.db" and path.split(os.sep)[:2] != ["users", "demo"]}
    assert outside == set()
    assert "auth.db" in changed                       # dort kommt der Benutzer demo hinzu


def test_seeding_next_to_real_books_keeps_the_administrator_and_auto_login(data_root, demo_on,
                                                                           light_scenario):
    _real_installation(data_root)
    demo.prepare(log=lambda m: None)
    assert auth.is_admin("admin") is True and auth.is_admin("demo") is False
    assert auth.auto_login_user() == "admin"          # gilt wieder, sobald die Variable fehlt


def test_seeding_next_to_unmigrated_legacy_files_does_not_touch_them(data_root, demo_on,
                                                                    light_scenario):
    # Aufruf ohne vorherigen Umzug: Dateien direkt in data/ gehören nicht zur Demo
    data_root.mkdir()
    (data_root / "buch.db").write_bytes(b"alte Buchhaltung")
    (data_root / "invoices").mkdir()
    (data_root / "invoices" / "R1.pdf").write_bytes(b"%PDF-1.7")
    before = tree(data_root)
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    after = tree(data_root)
    assert all(after.get(path) == digest for path, digest in before.items())


# ── prepare(): Weigern ───────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["file", "dir"])
@pytest.mark.parametrize("name", ["buch.db", "sql_operations.log", "invoices", "tests"])
def test_refuses_a_non_empty_demo_directory(data_root, demo_on, name, kind):
    path = _demo_dir(data_root) / name
    path.parent.mkdir(parents=True)
    if kind == "dir":
        path.mkdir()
        (path / "x.txt").write_text("echt", encoding="utf-8")
    else:
        path.write_text("{}")
    before = tree(data_root)
    logs = []
    assert demo.prepare(log=logs.append) == demo.REFUSED
    assert tree(data_root) == before                        # nichts geladen, nichts angelegt
    assert not (_demo_dir(data_root) / demo.MARKER_FILE).exists()
    assert any("WARNUNG" in m for m in logs)


def test_refuses_existing_demo_database_and_keeps_it(data_root, demo_on):
    """Echte Buchhaltung unter dem Namen demo: bestehende DB bleibt unangetastet."""
    db_path = _demo_dir(data_root) / "buch.db"
    db_path.parent.mkdir(parents=True)
    Database(str(db_path))                          # leere, aber vorhandene DB
    before = tree(data_root)
    logs = []
    assert demo.prepare(log=logs.append) == demo.REFUSED
    assert _count(data_root, "Contacts") == 0
    assert _count(data_root, "Invoices") == 0
    assert tree(data_root) == before
    assert any("WARNUNG" in m for m in logs)


def test_refuses_when_a_user_demo_exists_without_marker(data_root, demo_on, light_scenario):
    auth.create_user("demo", "geheim123", is_admin=False, allow_reserved=True)
    before = tree(data_root)
    logs = []
    assert demo.prepare(log=logs.append) == demo.REFUSED
    assert tree(data_root) == before                        # kein Verzeichnis, keine DB
    assert not _demo_dir(data_root).exists()
    assert any("WARNUNG" in m for m in logs)


def test_refuses_when_a_user_demo_exists_even_if_the_directory_is_empty(data_root, demo_on,
                                                                       light_scenario):
    auth.create_user("demo", "geheim123", is_admin=False, allow_reserved=True)
    _demo_dir(data_root).mkdir(parents=True)
    assert demo.prepare(log=lambda m: None) == demo.REFUSED
    assert os.listdir(_demo_dir(data_root)) == []


def test_refusal_keeps_a_users_password(data_root, demo_on, light_scenario):
    # Ein Benutzer demo mit eigenem Passwort (Demo-Modus früher beendet): nichts wird ersetzt
    auth.create_user("demo", "eigenes-passwort", is_admin=False, allow_reserved=True)
    demo.prepare(log=lambda m: None)
    assert auth.authenticate("demo", "eigenes-passwort") is True


def test_refuses_a_tests_folder_inside_the_demo_directory(data_root, demo_on):
    # Im Demo-Verzeichnis zählt jeder Eintrag als Belegung, auch ein Ordner tests/
    (_demo_dir(data_root) / "tests").mkdir(parents=True)
    assert demo.prepare(log=lambda m: None) == demo.REFUSED
    assert not (_demo_dir(data_root) / demo.MARKER_FILE).exists()


# ── prepare(): Neustart ──────────────────────────────────────────────────────
def test_second_start_does_not_reseed(data_root, demo_on, light_scenario):
    demo.prepare(log=lambda m: None)
    before = _count(data_root, "Contacts")
    assert before > 0
    assert demo.prepare(log=lambda m: None) == demo.RESUMED
    assert _count(data_root, "Contacts") == before


def test_second_start_changes_nothing(data_root, demo_on, light_scenario):
    demo.prepare(log=lambda m: None)
    before = tree(data_root)
    logs = []
    assert demo.prepare(log=logs.append) == demo.RESUMED
    assert tree(data_root) == before
    assert any("keine erneute Befüllung" in m for m in logs)


def test_the_marker_wins_over_a_user_demo_and_other_files(data_root, demo_on, light_scenario):
    demo.prepare(log=lambda m: None)
    (_demo_dir(data_root) / "von-hand.txt").write_text("später dazugekommen", encoding="utf-8")
    before = tree(data_root)
    assert demo.prepare(log=lambda m: None) == demo.RESUMED
    assert tree(data_root) == before


# ── prepare(): Benutzer-Kontext ──────────────────────────────────────────────
def test_no_user_context_is_left_behind_after_seeding(data_root, demo_on, light_scenario):
    demo.prepare(log=lambda m: None)
    assert userctx.get_user() is None


def test_no_user_context_is_left_behind_when_seeding_fails(data_root, demo_on, monkeypatch):
    def boom(db):
        raise RuntimeError("Befüllen abgebrochen")
    monkeypatch.setattr(demo_seed, "load_demo_scenario", boom)
    with pytest.raises(RuntimeError, match="Befüllen abgebrochen"):
        demo.prepare(log=lambda m: None)
    assert userctx.get_user() is None


def test_failed_seeding_leaves_no_marker_and_is_not_repeated_blindly(data_root, demo_on,
                                                                    monkeypatch):
    def boom(db):
        raise RuntimeError("Befüllen abgebrochen")
    with monkeypatch.context() as patched:
        patched.setattr(demo_seed, "load_demo_scenario", boom)
        with pytest.raises(RuntimeError):
            demo.prepare(log=lambda m: None)
    assert not (_demo_dir(data_root) / demo.MARKER_FILE).exists()
    assert demo.active_user() is None                  # ohne Marker kein Demo-Besucher
    before = tree(data_root)
    # Beim nächsten Start (das Szenario funktioniert wieder) ist das Verzeichnis belegt …
    assert demo.prepare(log=lambda m: None) == demo.REFUSED
    assert tree(data_root) == before                   # … und wird nicht blind erneut befüllt


# ── is_active() / active_user() / Banner ────────────────────────────────────
@pytest.mark.parametrize("variable, marker, active", [
    pytest.param(True, True, True, id="Variable-und-Marker"),
    pytest.param(True, False, False, id="nur-Variable"),
    pytest.param(False, True, False, id="nur-Marker"),
    pytest.param(False, False, False, id="weder-noch"),
])
def test_demo_is_active_only_with_variable_and_marker(data_root, monkeypatch, variable, marker,
                                                      active):
    if variable:
        monkeypatch.setenv(demo.ENV_VAR, "1")
    if marker:
        _demo_dir(data_root).mkdir(parents=True)
        (_demo_dir(data_root) / demo.MARKER_FILE).write_text("x")
    assert demo.is_active() is active
    assert demo.active_user() == ("demo" if active else None)


def test_marker_elsewhere_does_not_activate_the_demo(data_root, monkeypatch):
    # Der Marker zählt nur unter users/demo/ – ein .demo im Wurzelverzeichnis (früheres
    # Demo-Verzeichnis, wird beim Start umgezogen) oder bei einem anderen Benutzer nicht
    monkeypatch.setenv(demo.ENV_VAR, "1")
    (data_root / "users" / "alice").mkdir(parents=True)
    (data_root / "users" / "alice" / demo.MARKER_FILE).write_text("x")
    (data_root / demo.MARKER_FILE).write_text("x")
    assert demo.is_active() is False


def test_banner_only_in_demo_mode(data_root, monkeypatch):
    assert demo.BANNER_TEXT not in Header1()
    # Variable gesetzt, aber echte Daten (kein Marker) → kein Banner
    (data_root / "users" / "admin").mkdir(parents=True)
    (data_root / "users" / "admin" / "buch.db").write_text("")
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.BANNER_TEXT not in Header1()
    # Demo-Verzeichnis mit Marker → Banner
    _demo_dir(data_root).mkdir(parents=True)
    (_demo_dir(data_root) / demo.MARKER_FILE).write_text("x")
    assert demo.is_active()
    assert "demoBanner" in Header1() and demo.BANNER_TEXT in Header1()
    # Marker vorhanden, Variable weg → kein Banner
    monkeypatch.delenv(demo.ENV_VAR)
    assert demo.BANNER_TEXT not in Header1()
