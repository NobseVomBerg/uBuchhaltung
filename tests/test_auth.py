# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""TODO #4 – Phase 1: Authentifizierung, Sessions und Benutzer-Isolation.

Alle Tests verwenden anonyme Test-Zugangsdaten und eine temporäre auth.db. Die automatische
Anmeldung und die Ersteinrichtung prüfen test_auto_login*.py und test_setup_start.py.
"""
import os
from urllib.parse import parse_qs, urlsplit

import pytest

import auth
import userctx
from db import Database
from server import handlers
from server.pages_users import PageUsers
from tests import installation


@pytest.fixture
def auth_db(tmp_path):
    return str(tmp_path / "auth.db")


# ── Passwort-Hashing ─────────────────────────────────────────────────────────
def test_password_hash_roundtrip(auth_db):
    auth.create_user("alice", "geheim123", db_path=auth_db)
    assert auth.authenticate("alice", "geheim123", auth_db) is True
    assert auth.authenticate("alice", "falsch", auth_db) is False


def test_password_not_stored_plaintext(auth_db):
    auth.create_user("alice", "geheim123", db_path=auth_db)
    import sqlite3
    con = sqlite3.connect(auth_db)
    pwhash, salt = con.execute("SELECT PwHash, Salt FROM Users WHERE Username='alice'").fetchone()
    con.close()
    assert b"geheim123" not in bytes(pwhash)
    assert len(bytes(salt)) == 16 and len(bytes(pwhash)) == 32


def test_unknown_user_authenticates_false(auth_db):
    auth.init_auth_db(auth_db)
    assert auth.authenticate("niemand", "x", auth_db) is False


def test_username_validation(auth_db):
    assert auth.valid_username("ab") is False        # zu kurz
    assert auth.valid_username("alice_1") is True
    assert auth.valid_username("bad/name") is False  # Slash (Path-Traversal)
    with pytest.raises(ValueError):
        auth.create_user("x", "geheim123", db_path=auth_db)
    with pytest.raises(ValueError):
        auth.create_user("alice", "kurz", db_path=auth_db)  # Passwort zu kurz


# ── reservierter Benutzername 'tests' (Ordner für Testartefakte im Datenverzeichnis) ──
RESERVED_MESSAGE = "Dieser Benutzername ist reserviert."


@pytest.mark.parametrize("name", ["tests", "Tests", "TESTS", "tEsTs"])
def test_reserved_username_is_invalid(name):
    assert auth.valid_username(name) is False


@pytest.mark.parametrize("name", ["tester", "test", "tests2", "my-tests", "_tests"])
def test_names_similar_to_the_reserved_one_stay_valid(name):
    assert auth.valid_username(name) is True


@pytest.mark.parametrize("name", ["tests", "Tests", "TESTS"])
def test_create_user_rejects_reserved_name(auth_db, name):
    with pytest.raises(ValueError) as exc:
        auth.create_user(name, "geheim123", db_path=auth_db)
    assert str(exc.value) == RESERVED_MESSAGE
    assert auth.has_any_user(auth_db) is False                      # nichts angelegt


@pytest.mark.parametrize("name", ["tester", "test", "tests2"])
def test_create_user_accepts_names_similar_to_the_reserved_one(auth_db, name):
    auth.create_user(name, "geheim123", db_path=auth_db)
    assert auth.user_exists(name, auth_db) is True


def test_duplicate_user_raises(auth_db):
    auth.create_user("alice", "geheim123", db_path=auth_db)
    with pytest.raises(ValueError):
        auth.create_user("alice", "anderes123", db_path=auth_db)


def test_set_password_and_delete(auth_db):
    auth.create_user("alice", "geheim123", db_path=auth_db)
    auth.set_password("alice", "neuespass1", db_path=auth_db)
    assert auth.authenticate("alice", "geheim123", auth_db) is False
    assert auth.authenticate("alice", "neuespass1", auth_db) is True
    auth.delete_user("alice", db_path=auth_db)
    assert auth.authenticate("alice", "neuespass1", auth_db) is False


def test_admin_flag_and_listing(auth_db):
    auth.create_user("admin", "geheim123", is_admin=True, db_path=auth_db)
    auth.create_user("bob", "geheim123", db_path=auth_db)
    assert auth.is_admin("admin", auth_db) is True
    assert auth.is_admin("bob", auth_db) is False
    names = [u[0] for u in auth.list_users(auth_db)]
    assert names == ["admin", "bob"]


# ── Sessions ─────────────────────────────────────────────────────────────────
def test_session_lifecycle(auth_db):
    auth.create_user("alice", "geheim123", db_path=auth_db)
    token = auth.create_session("alice", auth_db)
    assert auth.get_session_user(token, auth_db) == "alice"
    auth.delete_session(token, auth_db)
    assert auth.get_session_user(token, auth_db) is None


def test_session_expiry(auth_db):
    auth.create_user("alice", "geheim123", db_path=auth_db)
    token = auth.create_session("alice", auth_db, ttl=-1)   # bereits abgelaufen
    assert auth.get_session_user(token, auth_db) is None


def test_get_session_user_none_token(auth_db):
    assert auth.get_session_user(None, auth_db) is None
    assert auth.get_session_user("", auth_db) is None


def test_has_any_user(auth_db):
    assert auth.has_any_user(auth_db) is False
    auth.create_user("alice", "geheim123", db_path=auth_db)
    assert auth.has_any_user(auth_db) is True


@pytest.fixture
def data_root(monkeypatch, tmp_path):
    """Isolierter Datenbereich + leerer Modus-Cache je Test."""
    monkeypatch.setattr(userctx, "DATA_ROOT", str(tmp_path))
    userctx._mode_cache.clear()
    userctx.clear()
    yield str(tmp_path)
    userctx._mode_cache.clear()
    userctx.clear()


# ── App-Modus (Datei-Konfig): eingerichtet ('multi') oder nicht ──────────────
def test_mode_unconfigured(data_root):
    assert userctx.get_mode() is None
    assert userctx.auth_enabled() is False


def test_set_mode_persists_and_drives_auth(data_root):
    userctx.set_mode("multi")
    assert userctx.get_mode() == "multi"
    assert userctx.auth_enabled() is True
    # frisch aus der Datei gelesen (Cache geleert) bleibt es 'multi'
    installation.forget_config_cache()
    assert userctx.get_mode() == "multi"
    assert installation.read_config() == {"mode": "multi"}


@pytest.mark.parametrize("mode", ["single", "foo", "", None])
def test_set_mode_accepts_only_multi(data_root, mode):
    with pytest.raises(ValueError):
        userctx.set_mode(mode)
    assert not os.path.exists(userctx.config_path())            # nichts geschrieben
    assert userctx.get_mode() is None


def test_set_mode_single_does_not_unset_a_set_up_installation(data_root):
    userctx.set_mode("multi")
    with pytest.raises(ValueError):
        userctx.set_mode("single")
    assert userctx.get_mode() == "multi"
    assert userctx.auth_enabled() is True


@pytest.mark.parametrize("config", [
    pytest.param({"mode": "single"}, id="Einzelbenutzer-Modus-aus-Version-1"),
    pytest.param({"mode": "foo"}, id="unbekannter-Modus"),
    pytest.param({"mode": ""}, id="leerer-Modus"),
    pytest.param({"auto_login": "admin"}, id="kein-Modus"),
    pytest.param({}, id="leer"),
])
def test_any_mode_but_multi_in_config_means_not_set_up(data_root, config):
    # Den früheren Einzelbenutzer-Modus kennt nur noch layout.migrate() (beim Start)
    installation.write_config(config)
    assert userctx.get_mode() is None
    assert userctx.auth_enabled() is False
    assert userctx.get_auto_login() is None


# ── userctx-Pfadauflösung & Isolation ────────────────────────────────────────
def test_userctx_without_user_context_points_to_data_root(data_root):
    # Werkzeuge und Tests ohne Benutzer-Kontext: DATA_ROOT, auch nach der Einrichtung
    assert userctx.auth_enabled() is False
    assert userctx.user_db_path() == os.path.join(data_root, "buch.db")
    assert userctx.user_data_dir() == data_root
    userctx.set_mode("multi")
    assert userctx.get_user() is None
    assert userctx.user_db_path() == os.path.join(data_root, "buch.db")
    assert userctx.user_data_dir() == data_root


def test_user_without_set_up_installation_has_no_own_directory(data_root):
    # Ohne eingerichtete Installation gibt es kein users/<Name>/, auch mit gesetztem Benutzer
    userctx.set_user("alice")
    assert userctx.user_data_dir() == data_root
    assert userctx.user_db_path() == os.path.join(data_root, "buch.db")


def test_userctx_per_user_paths(data_root):
    userctx.set_mode("multi")
    userctx.set_user("alice")
    assert userctx.user_db_path() == os.path.join(data_root, "users", "alice", "buch.db")
    assert userctx.user_data_dir() == os.path.join(data_root, "users", "alice")


def test_two_users_have_isolated_databases(tmp_path):
    """Zwei Nutzer-DBs sind vollständig getrennt (eigene Inhalte)."""
    db_a = Database(db_name=str(tmp_path / "a" / "buch.db"))
    db_b = Database(db_name=str(tmp_path / "b" / "buch.db"))
    db_a.insert_article(name="Nur-A", unit_price=1.0)
    assert [r[1] for r in db_a.fetch_articles()] == ["Nur-A"]
    assert db_b.fetch_articles() == []          # B sieht A nicht


# ── Phase 2: Datei-Isolation ─────────────────────────────────────────────────
def test_user_subdir_without_user_context(data_root):
    d = userctx.user_subdir("logos")
    assert d == os.path.join(data_root, "logos")
    assert os.path.isdir(d)


def test_user_subdir_per_user_isolated(data_root):
    userctx.set_mode("multi")
    userctx.set_user("alice")
    da = userctx.user_subdir("logos")
    userctx.set_user("bob")
    db = userctx.user_subdir("logos")
    assert da != db
    assert da == os.path.join(data_root, "users", "alice", "logos")
    assert db == os.path.join(data_root, "users", "bob", "logos")


def test_document_parser_follows_userctx(data_root):
    import document_parser
    # ohne Benutzer-Kontext ⇒ Basis = Datenwurzel
    p = document_parser.DocumentParser()
    assert p.log_dir == data_root
    assert p.data_dir == os.path.join(data_root, "Belege")
    # Mehrbenutzer ⇒ unter users/<user>/
    userctx.set_mode("multi")
    userctx.set_user("alice")
    p2 = document_parser.DocumentParser()
    assert p2.log_dir == os.path.join(data_root, "users", "alice")
    assert p2.data_dir == os.path.join(data_root, "users", "alice", "Belege")
    # Explizite Pfade gewinnen weiterhin
    p3 = document_parser.DocumentParser(data_dir=os.path.join(data_root, "x"), log_dir=data_root)
    assert p3.data_dir == os.path.join(data_root, "x")


def test_header_shows_name_and_logout_only_for_a_login_session(data_root):
    from server.pages import Header1
    # kein Benutzer: weder Name noch Abmelden-Link
    assert "👤" not in Header1("dashboard") and "Abmelden" not in Header1("dashboard")
    # Benutzer ohne eingerichtete Installation (Werkzeuge): ebenfalls nichts
    userctx.set_user("admin")
    assert "👤" not in Header1("dashboard") and "Abmelden" not in Header1("dashboard")
    # Anmeldung mit Sitzung: Name + Abmelden
    userctx.set_mode("multi")
    h = Header1("dashboard")
    assert "👤 admin" in h and "/logout" in h and "Abmelden" in h


def test_header_shows_name_but_no_logout_for_the_automatic_login(data_root):
    from server.pages import Header1
    userctx.set_mode("multi")
    userctx.set_user("admin")
    userctx.set_auto_login_active(True)       # es gibt keine Sitzung, die man beenden könnte
    h = Header1("dashboard")
    assert "👤 admin" in h
    assert "/logout" not in h and "Abmelden" not in h


def test_ensure_directories_creates_only_the_data_root_and_users(data_root):
    import server.app as app
    typ = {"logos", "invoices", "quotes", "worktime"}
    # Nicht eingerichtet: nur die Datenwurzel, keine Typ-Ordner, kein users/
    app._ensure_directories()
    assert os.listdir(data_root) == []
    # Ein Altbestand (früherer Einzelbenutzer-Modus) bekommt keine Typ-Ordner mehr
    installation.write_config({"mode": "single"})
    app._ensure_directories()
    assert set(os.listdir(data_root)) == {"config.json"}
    # Eingerichtet: users/, aber keine globalen Typ-Ordner
    userctx.set_mode("multi")
    app._ensure_directories()
    assert set(os.listdir(data_root)) == {"config.json", "users"}
    assert typ & set(os.listdir(data_root)) == set()


# ── Phase 3: Benutzerverwaltung ──────────────────────────────────────────────
def test_set_admin_and_count(auth_db):
    auth.create_user("admin", "geheim123", is_admin=True, db_path=auth_db)
    auth.create_user("bob", "geheim123", db_path=auth_db)
    assert auth.count_admins(auth_db) == 1
    auth.set_admin("bob", True, db_path=auth_db)
    assert auth.is_admin("bob", auth_db) is True
    assert auth.count_admins(auth_db) == 2
    auth.set_admin("bob", False, db_path=auth_db)
    assert auth.count_admins(auth_db) == 1


def test_user_exists(auth_db):
    auth.init_auth_db(auth_db)
    assert auth.user_exists("ghost", auth_db) is False
    auth.create_user("ghost", "geheim123", db_path=auth_db)
    assert auth.user_exists("ghost", auth_db) is True


def test_csrf_token_roundtrip():
    tok = auth.csrf_for("session-xyz")
    assert tok and auth.check_csrf("session-xyz", tok) is True
    assert auth.check_csrf("session-xyz", "falsch") is False
    assert auth.check_csrf("andere-session", tok) is False
    assert auth.check_csrf("", tok) is False


def test_handler_create_and_delete_user(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DB", str(tmp_path / "auth.db"))
    handlers.handle_user_create({"username": ["neuer"], "password": ["geheim123"]})
    assert auth.user_exists("neuer") is True
    loc = handlers.handle_user_delete({"username": ["neuer"]})
    assert auth.user_exists("neuer") is False
    assert loc.startswith("/users")


def test_handler_last_admin_protected(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DB", str(tmp_path / "auth.db"))
    auth.create_user("admin", "geheim123", is_admin=True)
    # letzter Admin: weder löschen noch herabstufen
    loc = handlers.handle_user_delete({"username": ["admin"]})
    assert "err=" in loc and auth.user_exists("admin")
    loc = handlers.handle_user_toggle_admin({"username": ["admin"]})
    assert "err=" in loc and auth.is_admin("admin") is True


def test_handler_create_invalid_username(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DB", str(tmp_path / "auth.db"))
    loc = handlers.handle_user_create({"username": ["x"], "password": ["geheim123"]})
    assert "err=" in loc


def test_handler_create_reserved_username_reports_it_and_creates_nobody(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DB", str(tmp_path / "auth.db"))
    loc = handlers.handle_user_create({"username": ["Tests"], "password": ["geheim123"]})
    assert parse_qs(urlsplit(loc).query) == {"err": [RESERVED_MESSAGE]}     # kommt beim Nutzer an
    assert auth.has_any_user() is False


def test_page_users_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUTH_DB", str(tmp_path / "auth.db"))
    auth.create_user("admin", "geheim123", is_admin=True)
    auth.create_user("bob", "geheim123")
    html = PageUsers("admin", auth.list_users(), csrf="tok123")
    assert "Benutzerverwaltung" in html and "bob" in html
    assert 'name="csrf" value="tok123"' in html
    assert "/users/create" in html and "/users/delete" in html
    # eigenes Konto (admin) zeigt keine Lösch-/Toggle-Buttons
    assert "(eigenes Konto)" in html
