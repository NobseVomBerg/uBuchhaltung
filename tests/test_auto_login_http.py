# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Automatische Anmeldung über HTTP (echter Server auf 127.0.0.1, freier Port).

Der Betrieb für eine Person: ``config.json`` nennt ``auto_login``, der Administrator
arbeitet ohne Sitzung und ohne Passwort. Geprüft wird der Türsteher ``_gate()``:
Zugriff ohne Cookie, Weiterleitungen von Anmeldeseite und Abmeldung, fremde Sitzungen,
Hostname und Herkunft, das an die Anmeldung gebundene CSRF-Token, Keep-Alive-Verbindungen
sowie der Schalter ``POST /users/auto-login`` (aus mit Passwort, wieder ein).

Fixtures ``server_port`` und ``auto_login`` stehen in conftest.py, Anfragen stellt
tests/http_client.py. Namen, Passwörter und Hostnamen (*.example.org) sind erfundene Testwerte.
"""
import os
import re
from urllib.parse import unquote

import pytest

import auth
import userctx
from tests import installation
from tests.http_client import FOREIGN_ORIGIN, KeepAlive, get, own_origin, post
from tests.installation import PASSWORD, read_config

NEW_PASSWORD = "neues-passwort"
SWITCH = "/users/auto-login"


@pytest.fixture
def others(auto_login):
    """Weitere Benutzer neben dem automatisch angemeldeten ``admin``: ``alice`` (normaler
    Benutzer) und ``carla`` (ebenfalls Administratorin), jeweils mit eigener Datenbank."""
    auth.create_user("alice", PASSWORD)
    auth.create_user("carla", PASSWORD, is_admin=True)
    return {name: installation.open_db(name) for name in ("alice", "carla")}


def _cookie(token):
    return {"Cookie": f"session={token}"}


def _session_token(response):
    return response.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]


def _meta_csrf(html):
    return re.search(r"<meta name='csrf-token' content='([^']*)'>", html).group(1)


def _disable(port, csrf, password=NEW_PASSWORD, password2=None, headers=None):
    return post(port, SWITCH, headers=headers, csrf=csrf,
                form={"action": "disable", "password": password,
                      "password2": password if password2 is None else password2})


# ── Zugriff ohne Cookie ──────────────────────────────────────────────────────
def test_pages_open_without_cookie_as_the_administrator(server_port, auto_login):
    resp = get(server_port, "/about")
    assert resp.status == 200
    assert "👤 admin" in resp.text


def test_header_shows_the_name_but_no_logout_link(server_port, auto_login):
    html = get(server_port, "/about").text
    assert "👤 admin" in html
    assert "/logout" not in html and "Abmelden" not in html


def test_pages_carry_the_csrf_token_of_the_automatic_login(server_port, auto_login):
    token = _meta_csrf(get(server_port, "/about").text)
    assert token == auto_login.csrf == auth.csrf_for("auto-login:admin")


def test_requests_run_on_the_administrators_database(server_port, auto_login):
    auto_login.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    assert "Probeartikel" in get(server_port, "/masterdata/articles").text


def test_no_database_appears_in_the_data_root(server_port, auto_login):
    for path in ("/about", "/masterdata", "/buch.css", "/login", "/logout", "/setup"):
        get(server_port, path)
    assert not os.path.exists(os.path.join(userctx.DATA_ROOT, "buch.db"))
    assert os.path.isfile(os.path.join(userctx.DATA_ROOT, "users", "admin", "buch.db"))


def test_static_files_need_no_user(server_port, auto_login):
    resp = get(server_port, "/buch.css")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/css"


# ── Anmeldeseite und Abmeldung sind abgeschaltet ────────────────────────────
@pytest.mark.parametrize("path", ["/login", "/logout", "/setup-admin"])
def test_login_pages_redirect_to_the_start_page(server_port, auto_login, path):
    resp = get(server_port, path)
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    assert "Set-Cookie" not in resp.headers


def test_posting_credentials_to_login_starts_no_session(server_port, auto_login):
    resp = post(server_port, "/login", headers={"Origin": own_origin(server_port)},
                form={"username": "admin", "password": PASSWORD})
    assert resp.status == 303
    assert resp.headers["Location"] == "/"
    assert "Set-Cookie" not in resp.headers
    assert installation.session_count() == 0                 # auch in der auth.db entsteht keine


def test_logout_does_not_end_a_session(server_port, auto_login):
    token = auth.create_session("admin")
    resp = get(server_port, "/logout", headers=_cookie(token))
    assert resp.status == 303 and resp.headers["Location"] == "/"
    assert "Set-Cookie" not in resp.headers
    assert auth.get_session_user(token) == "admin"


# ── Sitzungen anderer Benutzer zählen nicht ─────────────────────────────────
@pytest.mark.parametrize("name", ["alice", "carla"], ids=["normaler-Benutzer", "anderer-Administrator"])
def test_a_session_of_another_user_is_treated_as_the_administrator(server_port, auto_login,
                                                                    others, name):
    token = auth.create_session(name)
    html = get(server_port, "/about", headers=_cookie(token)).text
    assert "👤 admin" in html
    assert f"👤 {name}" not in html
    assert "Abmelden" not in html


def test_a_session_of_another_user_sees_the_administrators_data_only(server_port, auto_login, others):
    auto_login.db.insert_article(name="Artikel des Administrators", unit_price=10, tax_rate=19)
    others["alice"].insert_article(name="Artikel von Alice", unit_price=10, tax_rate=19)
    html = get(server_port, "/masterdata/articles", headers=_cookie(auth.create_session("alice"))).text
    assert "Artikel des Administrators" in html
    assert "Artikel von Alice" not in html


@pytest.mark.parametrize("name", ["alice", "carla", "admin"],
                         ids=["normaler-Benutzer", "anderer-Administrator", "eigene-Sitzung"])
def test_the_csrf_token_of_a_session_is_rejected(server_port, auto_login, others, name):
    # Gebunden ist das Token an "auto-login:admin" – nicht an irgendeine Sitzung
    auto_login.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    article_id = auto_login.db.fetch_articles()[0][0]
    token = auth.create_session(name)
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                headers=_cookie(token), csrf=auth.csrf_for(token))
    assert resp.status == 403
    assert auto_login.db.get_article_by_id(article_id) is not None


def test_a_session_cookie_does_not_change_the_valid_token(server_port, auto_login, others):
    auto_login.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    article_id = auto_login.db.fetch_articles()[0][0]
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                headers=_cookie(auth.create_session("alice")), csrf=auto_login.csrf)
    assert resp.status == 303
    assert auto_login.db.get_article_by_id(article_id) is None


# ── Hostname (DNS-Rebinding) ─────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/about", "/", "/buch.css", "/favicon.ico", "/login"])
def test_foreign_host_is_403_for_every_path(server_port, auto_login, path):
    resp = get(server_port, path, headers={"Host": "evil.example"})
    assert resp.status == 403
    assert "evil.example" not in resp.text
    assert auto_login.csrf not in resp.text            # das Token verlässt den Server nicht


def test_foreign_host_post_is_403_even_with_matching_origin_and_valid_token(server_port, auto_login):
    # Der Angreifer lässt seinen Namen auf diesen Rechner zeigen: Origin und Host stimmen überein
    auto_login.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    article_id = auto_login.db.fetch_articles()[0][0]
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                headers={"Host": "evil.example", "Origin": FOREIGN_ORIGIN}, csrf=auto_login.csrf)
    assert resp.status == 403
    assert "Hostname" in resp.text
    assert auto_login.db.get_article_by_id(article_id) is not None


@pytest.mark.parametrize("host", ["localhost", "localhost:8080", "nas.local", "rechner.lan",
                                  "192.0.2.10:2824", "[::1]:8080", "buchhaltung"])
def test_local_hosts_are_accepted(server_port, auto_login, host):
    resp = get(server_port, "/about", headers={"Host": host})
    assert resp.status == 200
    assert "👤 admin" in resp.text


@pytest.mark.parametrize("host", ["buch.example.org", "buch.example.org:8443", "BUCH.Example.ORG"])
def test_host_released_by_environment_variable_is_accepted(server_port, auto_login, monkeypatch, host):
    assert get(server_port, "/about", headers={"Host": host}).status == 403
    monkeypatch.setenv("UBUCHHALTUNG_ALLOWED_HOSTS", "buch.example.org, andere.example.org:8443")
    resp = get(server_port, "/about", headers={"Host": host})
    assert resp.status == 200
    assert "👤 admin" in resp.text


def test_released_host_can_write_with_the_token_from_its_own_origin(server_port, auto_login, monkeypatch):
    monkeypatch.setenv("UBUCHHALTUNG_ALLOWED_HOSTS", "buch.example.org")
    auto_login.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    article_id = auto_login.db.fetch_articles()[0][0]
    resp = post(server_port, f"/masterdata/articles/delete?id={article_id}",
                headers={"Host": "buch.example.org", "Origin": "http://buch.example.org"},
                csrf=auto_login.csrf)
    assert resp.status == 303
    assert auto_login.db.get_article_by_id(article_id) is None


def test_behind_a_reverse_proxy_the_forwarded_host_counts_for_the_origin(server_port, auto_login):
    # nginx Proxy Manager, Caddy, Traefik: Host bleibt die interne Adresse, der öffentliche Name
    # steht in X-Forwarded-Host (und im Origin des Browsers)
    auto_login.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    article_id = auto_login.db.fetch_articles()[0][0]
    headers = {"Origin": "https://buch.example.org", "X-Forwarded-Host": "buch.example.org"}
    path = f"/masterdata/articles/delete?id={article_id}"
    assert post(server_port, path, headers=headers, csrf=auto_login.csrf).status == 303
    assert auto_login.db.get_article_by_id(article_id) is None


def test_behind_a_reverse_proxy_a_foreign_origin_is_still_403(server_port, auto_login):
    headers = {"Origin": FOREIGN_ORIGIN, "X-Forwarded-Host": "buch.example.org"}
    resp = post(server_port, "/masterdata/articles/delete?id=1", headers=headers, csrf=auto_login.csrf)
    assert resp.status == 403


def test_a_name_that_is_released_for_another_installation_is_not_accepted(server_port, auto_login,
                                                                          monkeypatch):
    monkeypatch.setenv("UBUCHHALTUNG_ALLOWED_HOSTS", "buch.example.org")
    assert get(server_port, "/about", headers={"Host": "evil.example"}).status == 403


# ── Herkunft ─────────────────────────────────────────────────────────────────
def test_switch_from_a_foreign_origin_is_403_and_changes_nothing(server_port, auto_login):
    resp = _disable(server_port, auto_login.csrf, headers={"Origin": FOREIGN_ORIGIN})
    assert resp.status == 403
    assert userctx.get_auto_login() == "admin"
    assert auth.authenticate("admin", NEW_PASSWORD) is False
    assert read_config()["auto_login"] == "admin"


def test_switch_from_a_foreign_fetch_site_is_403(server_port, auto_login):
    resp = _disable(server_port, auto_login.csrf, headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status == 403
    assert userctx.get_auto_login() == "admin"


# ── Keep-Alive: der Zustand einer Anfrage bleibt nicht an der Verbindung hängen ───────────
def test_switching_off_applies_to_the_next_request_on_an_open_connection(server_port, auto_login):
    with KeepAlive(server_port) as conn:
        assert conn.get("/about").status == 200
        userctx.set_auto_login(None)                       # zwischen zwei Anfragen derselben Verbindung
        resp = conn.get("/about")
        assert resp.status == 303
        assert resp.headers["Location"] == "/login"
        assert "👤" not in resp.text


def test_switching_on_applies_to_the_next_request_on_an_open_connection(server_port, auto_login):
    userctx.set_auto_login(None)
    with KeepAlive(server_port) as conn:
        assert conn.get("/about").status == 303
        userctx.set_auto_login("admin")
        resp = conn.get("/about")
        assert resp.status == 200 and "👤 admin" in resp.text


def test_the_csrf_key_follows_each_request_on_an_open_connection(server_port, auto_login):
    # Anfrage 1 läuft automatisch angemeldet; Anfrage 2 kommt mit Sitzung, nachdem die
    # automatische Anmeldung aus ist. Ihr Token ist an die Sitzung gebunden, nicht mehr an
    # "auto-login:admin" – der Schlüssel der ersten Anfrage darf nicht hängen bleiben.
    auto_login.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    article_id = auto_login.db.fetch_articles()[0][0]
    with KeepAlive(server_port) as conn:
        assert conn.get("/about").status == 200
        userctx.set_auto_login(None)
        token = auth.create_session("admin")
        resp = conn.post(f"/masterdata/articles/delete?id={article_id}", headers=_cookie(token),
                         csrf=auth.csrf_for(token))
        assert resp.status == 303
    assert auto_login.db.get_article_by_id(article_id) is None


def test_the_header_follows_each_request_on_an_open_connection(server_port, auto_login):
    # Anfrage 1 (POST, automatisch angemeldet) hinterlässt keinen Zustand für Anfrage 2: Sie kommt
    # mit Sitzung und bekommt den Abmelden-Link, den es bei der automatischen Anmeldung nicht gibt
    with KeepAlive(server_port) as conn:
        assert conn.post("/masterdata/articles/delete?id=99999", csrf=auto_login.csrf).status == 303
        userctx.set_auto_login(None)
        token = auth.create_session("admin")
        page = conn.get("/about", headers=_cookie(token))
        assert page.status == 200
        assert "👤 admin" in page.text and "Abmelden" in page.text


def test_the_auto_login_token_is_rejected_after_switching_off_on_an_open_connection(server_port,
                                                                                    auto_login):
    auto_login.db.insert_article(name="Probeartikel", unit_price=10, tax_rate=19)
    article_id = auto_login.db.fetch_articles()[0][0]
    with KeepAlive(server_port) as conn:
        assert conn.get("/about").status == 200
        userctx.set_auto_login(None)
        token = auth.create_session("admin")
        resp = conn.post(f"/masterdata/articles/delete?id={article_id}", headers=_cookie(token),
                         csrf=auto_login.csrf)             # Token der automatischen Anmeldung
        assert resp.status == 403
    assert auto_login.db.get_article_by_id(article_id) is not None


@pytest.mark.parametrize("case, expected_status", [
    pytest.param("login", 303, id="Anmeldeseite-leitet-um"),
    pytest.param("foreign-origin", 403, id="fremde-Herkunft"),
    pytest.param("foreign-host", 403, id="fremder-Host"),
    pytest.param("no-token", 403, id="Formular-ohne-Token"),
])
def test_a_rejected_post_does_not_disturb_the_next_request_on_the_connection(server_port, auto_login,
                                                                              case, expected_status):
    # Die Antwort kommt, bevor der Body gelesen wurde: er muss verworfen werden, sonst läse der
    # Server ihn als Anfang der nächsten Anfrage derselben Verbindung
    body = {"x": "1" * 200}
    headers = {
        "login": {"Origin": own_origin(server_port)},
        "foreign-origin": {"Origin": FOREIGN_ORIGIN},
        "foreign-host": {"Host": "evil.example"},
        "no-token": {},
    }[case]
    path = "/login" if case == "login" else "/masterdata/articles/delete?id=1"
    with KeepAlive(server_port) as conn:
        first = conn.post(path, headers=headers, form=body,
                          csrf=None if case == "no-token" else auto_login.csrf)
        assert first.status == expected_status
        second = conn.get("/about")
        assert second.status == 200
        assert "👤 admin" in second.text


def test_a_json_post_without_token_does_not_disturb_the_next_request(server_port, auto_login):
    # Routen, die ihren Body selbst lesen (fetch), prüfen das Token vorab und verwerfen den Body
    payload = '{"invoice_id": 99999, "status": "sent", "fuellung": "' + "x" * 500 + '"}'
    with KeepAlive(server_port) as conn:
        first = conn.request("POST", "/invoice/status", {"Content-Type": "application/json"},
                             body=payload)
        assert first.status == 403
        second = conn.get("/about")
        assert second.status == 200 and "👤 admin" in second.text


# ── Schalter: POST /users/auto-login ─────────────────────────────────────────
def test_the_users_page_shows_the_active_automatic_login(server_port, auto_login):
    html = get(server_port, "/users").text
    assert "Aktiv für <strong>admin</strong>" in html
    assert 'name="action" value="disable"' in html
    assert _meta_csrf(html) == auto_login.csrf
    assert f'name="csrf" value="{auto_login.csrf}"' in html


def test_disabling_sets_the_password_and_starts_a_session(server_port, auto_login):
    resp = _disable(server_port, auto_login.csrf)
    assert resp.status == 303
    assert resp.headers["Location"].startswith("/users?info=")
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith("session=")
    assert {"HttpOnly", "SameSite=Strict", "Path=/"} <= set(cookie.split("; ")[1:])
    assert "Secure" not in cookie                        # der Test-Server läuft ohne TLS
    assert auth.get_session_user(_session_token(resp)) == "admin"
    assert userctx.get_auto_login() is None
    assert "auto_login" not in read_config()
    assert auth.authenticate("admin", NEW_PASSWORD) is True


def test_after_disabling_a_visitor_without_cookie_must_log_in(server_port, auto_login):
    _disable(server_port, auto_login.csrf)
    resp = get(server_port, "/about")
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert get(server_port, "/login").status == 200
    assert get(server_port, "/users").headers["Location"] == "/login"


def test_after_disabling_the_new_session_works_and_shows_the_logout_link(server_port, auto_login):
    token = _session_token(_disable(server_port, auto_login.csrf))
    resp = get(server_port, "/about", headers=_cookie(token))
    assert resp.status == 200
    assert "👤 admin" in resp.text and "Abmelden" in resp.text and "/logout" in resp.text
    assert _meta_csrf(resp.text) == auth.csrf_for(token)       # jetzt an die Sitzung gebunden


def test_after_disabling_the_login_accepts_only_the_new_password(server_port, auto_login):
    _disable(server_port, auto_login.csrf)
    origin = {"Origin": own_origin(server_port)}
    ok = post(server_port, "/login", headers=origin, form={"username": "admin", "password": NEW_PASSWORD})
    assert ok.status == 303 and ok.headers["Location"] == "/"
    assert auth.get_session_user(_session_token(ok)) == "admin"
    for password in (PASSWORD, "", "falsch"):
        bad = post(server_port, "/login", headers=origin, form={"username": "admin", "password": password})
        assert bad.status == 200 and "falsch" in bad.text and "Set-Cookie" not in bad.headers


def test_enabling_again_with_the_session_switches_the_login_off_again(server_port, auto_login):
    token = _session_token(_disable(server_port, auto_login.csrf))
    resp = post(server_port, SWITCH, headers=_cookie(token), csrf=auth.csrf_for(token),
                form={"action": "enable"})
    assert resp.status == 303
    assert resp.headers["Location"].startswith("/users?info=")
    assert "Set-Cookie" not in resp.headers
    assert read_config()["auto_login"] == "admin"
    again = get(server_port, "/about")                      # wieder ohne Cookie
    assert again.status == 200 and "👤 admin" in again.text and "Abmelden" not in again.text


def test_the_users_page_offers_enabling_while_the_login_is_active(server_port, auto_login):
    token = _session_token(_disable(server_port, auto_login.csrf))
    html = get(server_port, "/users", headers=_cookie(token)).text
    assert 'name="action" value="enable"' in html
    assert "Für admin einschalten" in html
    assert f'name="csrf" value="{auth.csrf_for(token)}"' in html


@pytest.mark.parametrize("token", [None, "falsch"], ids=["ohne-Token", "falsches-Token"])
def test_the_switch_needs_the_csrf_token(server_port, auto_login, token):
    resp = _disable(server_port, token)
    assert resp.status == 403
    assert userctx.get_auto_login() == "admin"
    assert auth.authenticate("admin", NEW_PASSWORD) is False


@pytest.mark.parametrize("password, password2, message", [
    pytest.param(NEW_PASSWORD, "anderes-passwort", "stimmen nicht überein", id="verschieden"),
    pytest.param("kurz", "kurz", "zu kurz", id="zu-kurz"),
    pytest.param("", "", "zu kurz", id="leer"),
])
def test_disabling_with_bad_passwords_reports_an_error_and_stays_active(server_port, auto_login,
                                                                         password, password2, message):
    resp = _disable(server_port, auto_login.csrf, password, password2)
    assert resp.status == 303
    assert resp.headers["Location"].startswith("/users?err=")
    assert message in unquote(resp.headers["Location"])
    assert "Set-Cookie" not in resp.headers
    assert userctx.get_auto_login() == "admin"
    assert get(server_port, "/about").status == 200         # weiterhin ohne Anmeldung erreichbar


# ── Benutzerverwaltung nur für Administratoren ──────────────────────────────
@pytest.fixture
def login_users():
    """Anmeldung mit Passwort (keine automatische): Administrator ``admin``, Nutzerin ``alice``."""
    installation.set_up(auto_login=False, users=[("alice", False)])
    return {name: auth.create_session(name) for name in ("admin", "alice")}


def test_a_post_without_session_does_not_disturb_the_next_request_on_the_connection(server_port,
                                                                                    login_users):
    # Anmeldung mit Passwort: Ohne Sitzung führt der POST zur Anmeldeseite, der Body wird verworfen
    with KeepAlive(server_port) as conn:
        first = conn.post("/masterdata/articles/delete?id=1", form={"fuellung": "x" * 300}, csrf="egal")
        assert first.status == 303 and first.headers["Location"] == "/login"
        second = conn.get("/login")
        assert second.status == 200 and "Anmeldung" in second.text


def test_logout_ends_the_session_and_clears_the_cookie(server_port, login_users):
    token = login_users["alice"]
    assert get(server_port, "/about", headers=_cookie(token)).status == 200
    resp = get(server_port, "/logout", headers=_cookie(token))
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert resp.headers["Set-Cookie"].startswith("session=;") and "Max-Age=0" in resp.headers["Set-Cookie"]
    assert auth.get_session_user(token) is None
    assert get(server_port, "/about", headers=_cookie(token)).headers["Location"] == "/login"


def test_a_non_administrator_cannot_see_the_user_management(server_port, login_users):
    assert get(server_port, "/users", headers=_cookie(login_users["alice"])).status == 403
    assert get(server_port, "/users", headers=_cookie(login_users["admin"])).status == 200


def test_the_stammdaten_page_offers_the_user_management_only_to_administrators(server_port,
                                                                              login_users):
    assert "/users" in get(server_port, "/masterdata", headers=_cookie(login_users["admin"])).text
    assert "/users" not in get(server_port, "/masterdata", headers=_cookie(login_users["alice"])).text


def test_the_automatically_logged_in_administrator_sees_the_user_management(server_port, auto_login):
    assert "/users" in get(server_port, "/masterdata").text


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_a_non_administrator_cannot_use_the_switch(server_port, login_users, action):
    token = login_users["alice"]
    resp = post(server_port, SWITCH, headers=_cookie(token), csrf=auth.csrf_for(token),
                form={"action": action, "password": NEW_PASSWORD, "password2": NEW_PASSWORD})
    assert resp.status == 403
    assert userctx.get_auto_login() is None
    assert "auto_login" not in read_config()
    assert auth.authenticate("alice", NEW_PASSWORD) is False


def test_an_administrator_with_a_session_can_switch_the_automatic_login_on(server_port, login_users):
    token = login_users["admin"]
    resp = post(server_port, SWITCH, headers=_cookie(token), csrf=auth.csrf_for(token),
                form={"action": "enable"})
    assert resp.status == 303 and resp.headers["Location"].startswith("/users?info=")
    assert userctx.get_auto_login() == "admin"
    # ab jetzt braucht niemand mehr ein Cookie – auch alice nicht, sie bleibt aber ausgesperrt
    assert "👤 admin" in get(server_port, "/about").text
    assert "👤 admin" in get(server_port, "/about", headers=_cookie(login_users["alice"])).text
