"""TODO #4 – Phase 1: Authentifizierung, Sessions und Benutzer-Isolation.

Alle Tests verwenden anonyme Test-Zugangsdaten und eine temporäre auth.db.
"""
import os

import pytest

import auth
import userctx
from db import Database
from server import handlers
from server.pages_users import PageUsers


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


# ── userctx-Pfadauflösung & Isolation ────────────────────────────────────────
def test_userctx_default_single_user(monkeypatch):
    monkeypatch.delenv("PYBUCH_AUTH", raising=False)
    userctx.clear()
    assert userctx.auth_enabled() is False
    assert userctx.user_db_path() == userctx.DEFAULT_DB
    assert userctx.user_data_dir() == userctx.DATA_ROOT


def test_userctx_per_user_paths(monkeypatch):
    monkeypatch.setenv("PYBUCH_AUTH", "1")
    try:
        userctx.set_user("alice")
        assert userctx.user_db_path() == os.path.join("./data", "users", "alice", "buch.db")
        assert userctx.user_data_dir() == os.path.join("./data", "users", "alice")
    finally:
        userctx.clear()


def test_two_users_have_isolated_databases(tmp_path):
    """Zwei Nutzer-DBs sind vollständig getrennt (eigene Inhalte)."""
    db_a = Database(db_name=str(tmp_path / "a" / "buch.db"))
    db_b = Database(db_name=str(tmp_path / "b" / "buch.db"))
    db_a.insert_article(name="Nur-A", unit_price=1.0)
    assert [r[1] for r in db_a.fetch_articles()] == ["Nur-A"]
    assert db_b.fetch_articles() == []          # B sieht A nicht


# ── Phase 2: Datei-Isolation ─────────────────────────────────────────────────
def test_user_subdir_default_single_user(monkeypatch, tmp_path):
    monkeypatch.delenv("PYBUCH_AUTH", raising=False)
    monkeypatch.setattr(userctx, "DATA_ROOT", str(tmp_path))
    userctx.clear()
    d = userctx.user_subdir("logos")
    assert d == os.path.join(str(tmp_path), "logos")
    assert os.path.isdir(d)


def test_user_subdir_per_user_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("PYBUCH_AUTH", "1")
    monkeypatch.setattr(userctx, "DATA_ROOT", str(tmp_path))
    try:
        userctx.set_user("alice")
        da = userctx.user_subdir("logos")
        userctx.set_user("bob")
        db = userctx.user_subdir("logos")
    finally:
        userctx.clear()
    assert da != db
    assert da == os.path.join(str(tmp_path), "users", "alice", "logos")
    assert db == os.path.join(str(tmp_path), "users", "bob", "logos")


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
