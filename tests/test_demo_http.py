# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Demo-Modus im Server: Solange UBUCHHALTUNG_DEMO gesetzt ist und users/demo/ den Marker trägt,
arbeitet jeder Besucher ohne Anmeldung als ``demo`` – kein Administrator, also keine
Benutzerverwaltung, keine SQL-Konsole und keine Ersteinrichtung. Die Demo-Anmeldung geht der
automatischen Anmeldung eines Administrators vor, gilt aber nicht für einen ``demo`` mit
Administratorrecht. Wie die Demodaten entstehen, prüft test_demo.py.

Echter Server (Fixture ``server_port``); die Demodaten bauen die Fixtures hier schnell von Hand
auf (Benutzer, Datenbank, Marker), ``test_the_state_made_by_prepare_works_in_the_server`` nimmt
den Weg über demo.prepare(). Namen und Passwörter sind erfundene Testwerte.
"""
import contextlib
import json
import os
import re
import sqlite3
from types import SimpleNamespace

import pytest

import auth
import demo
import demo_seed
import userctx
from tests import installation
from tests.http_client import FOREIGN_ORIGIN, KeepAlive, get, own_origin, post
from tests.installation import PASSWORD, read_config

ADMIN_ONLY = "Die SQL-Konsole steht nur Administratoren zur Verfügung."


def _add_demo():
    """Benutzer ``demo`` (kein Administrator) mit Datenbank und Marker unter users/demo/."""
    userctx.set_mode("multi")
    auth.create_user("demo", PASSWORD, is_admin=False, allow_reserved=True)
    db = installation.open_db("demo")
    with open(os.path.join(userctx.DATA_ROOT, "users", "demo", demo.MARKER_FILE), "w",
              encoding="utf-8") as f:
        f.write("x")
    return SimpleNamespace(db=db, csrf=auth.csrf_for("auto-login:demo"), cookie={})


@pytest.fixture
def demo_site(monkeypatch):
    """Nur Demodaten (kein Administrator), Variable gesetzt: der Fall eines Demo-Containers."""
    monkeypatch.setenv(demo.ENV_VAR, "1")
    return _add_demo()


@pytest.fixture
def demo_beside_admin(monkeypatch):
    """Echte Bücher mit automatischer Anmeldung des Administrators ``admin`` – und daneben die
    Demodaten, Variable gesetzt."""
    installation.set_up()
    monkeypatch.setenv(demo.ENV_VAR, "1")
    return _add_demo()


def _meta_csrf(html):
    return re.search(r"<meta name='csrf-token' content='([^']*)'>", html).group(1)


# ── Der Besucher ist demo ────────────────────────────────────────────────────
def test_the_visitor_is_demo_without_logging_in(server_port, demo_site):
    resp = get(server_port, "/about")
    assert resp.status == 200
    assert "👤 demo" in resp.text
    assert "Abmelden" not in resp.text and "/logout" not in resp.text


@pytest.mark.parametrize("path", ["/about", "/masterdata", "/miscellaneous", "/masterdata/articles"])
def test_every_page_shows_the_demo_banner(server_port, demo_site, path):
    resp = get(server_port, path)
    assert resp.status == 200
    assert "demoBanner" in resp.text and demo.BANNER_TEXT in resp.text


def test_the_csrf_token_is_bound_to_the_demo_visitor(server_port, demo_site):
    assert _meta_csrf(get(server_port, "/about").text) == demo_site.csrf


@pytest.mark.parametrize("path", ["/login", "/logout", "/setup-admin"])
def test_login_pages_lead_back_to_the_start_page(server_port, demo_site, path):
    resp = get(server_port, path)
    assert resp.status == 303 and resp.headers["Location"] == "/"


def test_the_visitor_changes_the_demo_data(server_port, demo_site):
    demo_site.db.insert_article(name="Demoartikel", unit_price=10, tax_rate=19)
    article_id = demo_site.db.fetch_articles()[0][0]
    assert "Demoartikel" in get(server_port, "/masterdata/articles").text
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}", csrf=demo_site.csrf)
    assert resp.status == 303
    assert demo_site.db.get_article_by_id(article_id) is None


def test_a_write_needs_the_demo_token(server_port, demo_site):
    demo_site.db.insert_article(name="Demoartikel", unit_price=10, tax_rate=19)
    article_id = demo_site.db.fetch_articles()[0][0]
    for token in (None, "falsch", auth.csrf_for("auto-login:admin")):
        resp = post(server_port, f"/masterdata/articles/delete?id={article_id}", csrf=token)
        assert resp.status == 403
    assert demo_site.db.get_article_by_id(article_id) is not None


def test_the_visitor_only_ever_touches_users_demo(server_port, demo_site):
    for path in ("/about", "/masterdata", "/miscellaneous", "/buch.css", "/login", "/setup"):
        get(server_port, path)
    post(server_port, "/masterdata/articles/delete?id=99999", csrf=demo_site.csrf)
    assert sorted(os.listdir(userctx.DATA_ROOT)) == ["auth.db", "config.json", "users"]
    assert os.listdir(os.path.join(userctx.DATA_ROOT, "users")) == ["demo"]


def test_a_session_cookie_of_another_user_does_not_change_the_visitor(server_port, demo_site):
    auth.create_user("alice", PASSWORD)
    html = get(server_port, "/about", headers={"Cookie": f"session={auth.create_session('alice')}"}).text
    assert "👤 demo" in html and "alice" not in html


# ── demo ist kein Administrator ──────────────────────────────────────────────
def test_the_user_management_is_403(server_port, demo_site):
    resp = get(server_port, "/users")
    assert resp.status == 403
    assert "Administratorrechte" in resp.text


def test_the_stammdaten_page_does_not_offer_the_user_management(server_port, demo_site):
    assert "/users" not in get(server_port, "/masterdata").text


@pytest.mark.parametrize("path, form", [
    ("/users/create", {"username": "eindringling", "password": PASSWORD, "is_admin": "1"}),
    ("/users/reset-password", {"username": "demo", "password": "neues-passwort"}),
    ("/users/toggle-admin", {"username": "demo"}),
    ("/users/delete", {"username": "demo"}),
    ("/users/auto-login", {"action": "enable"}),
])
def test_user_management_posts_are_403_and_change_nothing(server_port, demo_site, path, form):
    users_before = auth.list_users()
    resp = post(server_port, path, csrf=demo_site.csrf, form=form)
    assert resp.status == 403
    assert auth.list_users() == users_before
    assert userctx.get_auto_login() is None
    assert auth.authenticate("demo", "neues-passwort") is False


def test_the_sql_console_is_403_and_runs_nothing(server_port, demo_site):
    resp = post(server_port, "/execute_sql", csrf=demo_site.csrf,
                form={"sql_commands": "CREATE TABLE Hack (X TEXT); DELETE FROM Contacts"})
    assert resp.status == 403
    assert resp.headers["Content-Type"].startswith("application/json")
    assert json.loads(resp.text) == {"success_count": 0, "total": 0, "output": "", "errors": [ADMIN_ONLY]}
    with contextlib.closing(sqlite3.connect(demo_site.db.db_name)) as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE name = 'Hack'").fetchall() == []


def test_the_miscellaneous_page_has_no_sql_console(server_port, demo_site):
    page = get(server_port, "/miscellaneous").text
    assert 'id="sql_input"' not in page and "executeSql" not in page
    assert "nur Administratoren zur Verfügung" in page


# ── keine Ersteinrichtung während der Demo ───────────────────────────────────
def test_start_is_403_and_creates_no_administrator_even_if_there_is_none(server_port, demo_site):
    assert auth.count_admins() == 0
    config_before = read_config()
    resp = post(server_port, "/setup/start", csrf=demo_site.csrf,
                form={"username": "uebernahme", "login": "none"})
    assert resp.status == 403
    assert "bereits abgeschlossen" in resp.text
    assert auth.count_admins() == 0
    assert auth.user_exists("uebernahme") is False
    assert read_config() == config_before                 # kein auto_login eingetragen


def test_start_with_a_password_is_403_too(server_port, demo_site):
    resp = post(server_port, "/setup/start", csrf=demo_site.csrf,
                form={"username": "uebernahme", "login": "password", "password": PASSWORD,
                      "password2": PASSWORD})
    assert resp.status == 403
    assert "Set-Cookie" not in resp.headers
    assert auth.count_admins() == 0


def test_start_without_the_token_is_403_as_well(server_port, demo_site):
    resp = post(server_port, "/setup/start", form={"username": "uebernahme", "login": "none"})
    assert resp.status == 403
    assert auth.count_admins() == 0


def test_the_setup_page_does_not_offer_the_administrator_setup(server_port, demo_site):
    page = get(server_port, "/setup").text
    assert 'action="/setup/start"' not in page


def test_start_is_403_beside_real_books_too(server_port, demo_beside_admin):
    resp = post(server_port, "/setup/start", csrf=demo_beside_admin.csrf,
                form={"username": "zweiter", "login": "none"})
    assert resp.status == 403
    assert auth.user_exists("zweiter") is False


# ── Vorrang vor der automatischen Anmeldung ──────────────────────────────────
def test_the_demo_takes_precedence_over_the_automatic_login(server_port, demo_beside_admin):
    html = get(server_port, "/about").text
    assert "👤 demo" in html and "👤 admin" not in html
    assert demo.BANNER_TEXT in html
    assert _meta_csrf(html) == demo_beside_admin.csrf


def test_the_visitor_beside_real_books_is_no_administrator(server_port, demo_beside_admin):
    assert get(server_port, "/users").status == 403
    resp = post(server_port, "/execute_sql", csrf=demo_beside_admin.csrf, form={"sql_commands": "SELECT 1"})
    assert resp.status == 403


def test_the_visitor_beside_real_books_does_not_see_them(server_port, demo_beside_admin):
    admin_db = installation.open_db("admin")
    admin_db.insert_article(name="Echter Artikel", unit_price=10, tax_rate=19)
    demo_beside_admin.db.insert_article(name="Demoartikel", unit_price=10, tax_rate=19)
    page = get(server_port, "/masterdata/articles").text
    assert "Demoartikel" in page and "Echter Artikel" not in page


def test_the_token_of_the_automatic_login_is_rejected_during_the_demo(server_port, demo_beside_admin):
    admin_db = installation.open_db("admin")
    admin_db.insert_article(name="Echter Artikel", unit_price=10, tax_rate=19)
    article_id = admin_db.fetch_articles()[0][0]
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                csrf=auth.csrf_for("auto-login:admin"))
    assert resp.status == 403
    assert admin_db.get_article_by_id(article_id) is not None


@pytest.mark.parametrize("name", ["admin", "alice"])
def test_other_users_cannot_log_in_while_the_demo_runs(server_port, monkeypatch, name):
    installation.set_up(auto_login=False, users=[("alice", False)])      # Anmeldung mit Passwort
    monkeypatch.setenv(demo.ENV_VAR, "1")
    _add_demo()
    resp = post(server_port, "/login", headers={"Origin": own_origin(server_port)},
                form={"username": name, "password": PASSWORD})
    assert resp.status == 303 and resp.headers["Location"] == "/"
    assert "Set-Cookie" not in resp.headers
    token = auth.create_session(name)                                    # auch eine echte Sitzung zählt nicht
    html = get(server_port, "/about", headers={"Cookie": f"session={token}"}).text
    assert "👤 demo" in html and f"👤 {name}" not in html


def test_without_a_password_login_the_visitor_is_demo_without_any_cookie(server_port, monkeypatch):
    installation.set_up(auto_login=False)
    monkeypatch.setenv(demo.ENV_VAR, "1")
    _add_demo()
    resp = get(server_port, "/about")
    assert resp.status == 200 and "👤 demo" in resp.text


# ── Ohne Variable oder Marker gilt die Demo nicht mehr ──────────────────────
def test_without_the_variable_the_automatic_login_of_the_administrator_is_back(
        server_port, demo_beside_admin, monkeypatch):
    monkeypatch.delenv(demo.ENV_VAR)
    html = get(server_port, "/about").text
    assert "👤 admin" in html and "👤 demo" not in html
    assert "demoBanner" not in html
    assert get(server_port, "/users").status == 200


def test_without_the_marker_the_variable_alone_does_not_log_in_as_demo(server_port, demo_beside_admin):
    os.remove(os.path.join(userctx.DATA_ROOT, "users", "demo", demo.MARKER_FILE))
    html = get(server_port, "/about").text
    assert "👤 admin" in html and "demoBanner" not in html


def test_without_the_variable_and_without_an_administrator_the_setup_is_shown(server_port, demo_site,
                                                                              monkeypatch):
    monkeypatch.delenv(demo.ENV_VAR)
    resp = get(server_port, "/about")
    assert resp.status == 303 and resp.headers["Location"] == "/setup"
    page = get(server_port, "/setup")
    assert page.status == 200 and 'action="/setup/start"' in page.text
    assert "demoBanner" not in page.text and "👤" not in page.text


def test_without_the_variable_the_setup_can_create_the_first_administrator(server_port, demo_site,
                                                                           monkeypatch):
    monkeypatch.delenv(demo.ENV_VAR)
    resp = post(server_port, "/setup/start", form={"username": "admin", "login": "none"})
    assert resp.status == 303
    assert auth.is_admin("admin") is True
    assert auth.is_admin("demo") is False


def test_without_the_user_demo_nobody_is_logged_in_as_demo(server_port, monkeypatch):
    # Variable und Marker sind noch da, der Benutzer wurde in der Benutzerverwaltung gelöscht
    installation.set_up(auto_login=False)
    monkeypatch.setenv(demo.ENV_VAR, "1")
    _add_demo()
    auth.delete_user("demo")
    assert demo.active_user() == "demo"                 # Variable + Marker allein melden niemanden an
    resp = get(server_port, "/about")
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert get(server_port, "/login").status == 200


# ── demo mit Administratorrecht wird nicht angemeldet ───────────────────────
def test_a_demo_user_with_the_administrator_flag_is_not_logged_in(server_port, demo_site):
    auth.set_admin("demo", True)                      # direkt in der auth.db, an der Verwaltung vorbei
    resp = get(server_port, "/about")
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert get(server_port, "/users").headers["Location"] == "/login"
    assert get(server_port, "/login").status == 200


def test_a_demo_user_with_the_administrator_flag_gets_no_session_either(server_port, demo_site):
    auth.set_admin("demo", True)
    resp = post(server_port, "/login", headers={"Origin": own_origin(server_port)},
                form={"username": "demo", "password": "irgendwas"})
    assert resp.status == 200 and "Set-Cookie" not in resp.headers


def test_the_automatic_login_wins_over_a_demo_user_with_the_administrator_flag(server_port,
                                                                               demo_beside_admin):
    auth.set_admin("demo", True)
    html = get(server_port, "/about").text
    assert "👤 admin" in html and "👤 demo" not in html


def test_a_demo_user_with_the_administrator_flag_opens_no_setup(server_port, demo_site):
    # Es gibt jetzt einen Administrator (demo): die Ersteinrichtung ist vorbei, auch ohne Anmeldung
    auth.set_admin("demo", True)
    resp = post(server_port, "/setup/start", form={"username": "uebernahme", "login": "none"})
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert auth.user_exists("uebernahme") is False


def test_the_administrator_cannot_make_demo_an_administrator_via_the_user_management(server_port,
                                                                                    monkeypatch):
    installation.set_up(auto_login=False)
    auth.create_user("demo", PASSWORD, allow_reserved=True)
    token = auth.create_session("admin")
    resp = post(server_port, "/users/toggle-admin", headers={"Cookie": f"session={token}"},
                csrf=auth.csrf_for(token), form={"username": "demo"})
    assert resp.status == 303
    assert "err=" in resp.headers["Location"]
    assert auth.is_admin("demo") is False


# ── Hostname und Verbindung ──────────────────────────────────────────────────
def test_a_foreign_host_is_403_during_the_demo(server_port, demo_site):
    resp = get(server_port, "/about", headers={"Host": "evil.example"})
    assert resp.status == 403
    assert demo_site.csrf not in resp.text


def test_a_foreign_origin_is_403_during_the_demo(server_port, demo_site):
    resp = post(server_port, "/masterdata/articles/delete?id=1", headers={"Origin": FOREIGN_ORIGIN},
                csrf=demo_site.csrf)
    assert resp.status == 403


def test_the_demo_ends_for_the_next_request_on_an_open_connection(server_port, demo_beside_admin,
                                                                  monkeypatch):
    with KeepAlive(server_port) as conn:
        assert "👤 demo" in conn.get("/about").text
        monkeypatch.delenv(demo.ENV_VAR)
        later = conn.get("/about").text
        assert "👤 admin" in later and "👤 demo" not in later


# ── der Weg über demo.prepare() ──────────────────────────────────────────────
def test_the_state_made_by_prepare_works_in_the_server(server_port, monkeypatch):
    monkeypatch.setattr(demo_seed, "load_demo_scenario", lambda db: None)
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    page = get(server_port, "/about")
    assert page.status == 200
    assert "👤 demo" in page.text and demo.BANNER_TEXT in page.text
    assert get(server_port, "/users").status == 403
    token = _meta_csrf(page.text)
    resp = post(server_port, "/setup/start", csrf=token, form={"username": "admin", "login": "none"})
    assert resp.status == 403
    assert auth.count_admins() == 0
