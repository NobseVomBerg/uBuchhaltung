# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Ersteinrichtung: der erste Administrator, mit oder ohne Anmeldung (``POST /setup/start``).

Solange die Installation nicht eingerichtet ist ODER eingerichtet ohne Administrator (und keine
Demo läuft), sind nur ``/setup`` und ``POST /setup/start`` erreichbar – ohne CSRF-Token (es gibt
noch keine Sitzung), aber mit Herkunfts- und Hostnamen-Prüfung. Danach ist die Einrichtung
gesperrt. Drei Teile:
  1. handlers.handle_setup_start() ohne Server,
  2. die Seite PageSetupStart,
  3. HTTP gegen einen echten Server (Fixture ``server_port``, Anfragen aus tests/http_client.py).

Benutzernamen, Passwörter und Hostnamen (*.example) sind erfundene Testwerte.
"""
import os
import re
import secrets

import pytest

import auth
import demo
import userctx
from server import handlers
from server.pages_setup import PageSetupStart
from tests import installation
from tests.http_client import FOREIGN_ORIGIN, KeepAlive, get, own_origin, post
from tests.installation import PASSWORD, read_config, write_config

XSS = '<script>alert(1)</script>"\''


def _start(**fields):
    return handlers.handle_setup_start({key: [value] for key, value in fields.items()})


def _nothing_created():
    """Weder Datenverzeichnis noch config.json noch auth.db: es wurde nichts angelegt."""
    return not os.path.exists(userctx.DATA_ROOT)


# ── Teil 1: handle_setup_start() ─────────────────────────────────────────────
def test_without_login_creates_an_administrator_with_automatic_login():
    assert _start(username="admin", login="none") == (True, "admin", False, None)
    assert auth.is_admin("admin") is True
    assert userctx.get_mode() == "multi"
    assert userctx.get_auto_login() == "admin"
    assert read_config() == {"mode": "multi", "auto_login": "admin"}
    assert auth.auto_login_user() == "admin"


def test_without_login_the_password_is_a_random_secret(monkeypatch):
    lengths = []

    def fake_token(nbytes):
        lengths.append(nbytes)
        return "zufallspasswort-fuer-den-test"
    monkeypatch.setattr(secrets, "token_urlsafe", fake_token)
    _start(username="admin", login="none")
    assert lengths == [24]
    assert auth.authenticate("admin", "zufallspasswort-fuer-den-test") is True
    assert not any(auth.authenticate("admin", guess) for guess in ("", "admin", PASSWORD))


def test_with_login_creates_an_administrator_with_the_chosen_password():
    assert _start(username="admin", login="password", password=PASSWORD,
                  password2=PASSWORD) == (True, "admin", True, None)
    assert auth.authenticate("admin", PASSWORD) is True
    assert auth.is_admin("admin") is True
    assert userctx.get_mode() == "multi"
    assert userctx.get_auto_login() is None
    assert read_config() == {"mode": "multi"}


def test_the_username_is_stripped():
    assert _start(username="  admin ", login="none")[:2] == (True, "admin")
    assert auth.user_exists("admin") is True


def test_setting_up_does_not_log_anybody_in_for_the_calling_thread():
    _start(username="admin", login="none")
    assert userctx.get_user() is None


@pytest.mark.parametrize("fields, message", [
    pytest.param(dict(password=PASSWORD, password2="anderes123"), "stimmen nicht überein",
                 id="ungleich"),
    pytest.param(dict(password="kurz", password2="kurz"), "zu kurz", id="zu-kurz"),
    pytest.param(dict(password="12345", password2="12345"), "zu kurz", id="fünf-Zeichen"),
    pytest.param(dict(password="", password2=""), "zu kurz", id="leer"),
    pytest.param(dict(), "zu kurz", id="Felder-fehlen"),
])
def test_with_login_bad_passwords_are_reported_and_nothing_is_created(fields, message):
    ok, username, with_login, error = _start(username="admin", login="password", **fields)
    assert (ok, username, with_login) == (False, "admin", True)
    assert message in error
    assert _nothing_created()
    assert userctx.get_mode() is None


@pytest.mark.parametrize("name", ["", " ", "ab", "a b", "x" * 33, "bad/name", "../etc", "ümlaut",
                                  "adm!n", "a\nb"],
                         ids=["leer", "Leerzeichen", "zu-kurz", "Leerzeichen-im-Namen", "zu-lang",
                              "Slash", "Pfad", "Umlaut", "Sonderzeichen", "Zeilenumbruch"])
@pytest.mark.parametrize("login", ["none", "password"])
def test_invalid_usernames_are_reported_and_nothing_is_created(name, login):
    ok, username, with_login, error = _start(username=name, login=login, password=PASSWORD,
                                             password2=PASSWORD)
    assert ok is False
    assert username == name.strip()                      # die Eingabe kommt ins Formular zurück
    assert with_login is (login == "password")
    assert "Ungültiger Benutzername" in error
    assert _nothing_created()


@pytest.mark.parametrize("name", ["tests", "Tests", "TESTS", "demo", "Demo", "DEMO"])
@pytest.mark.parametrize("login", ["none", "password"])
def test_reserved_usernames_are_reported_and_nothing_is_created(name, login):
    result = _start(username=name, login=login, password=PASSWORD, password2=PASSWORD)
    assert result == (False, name, login == "password", "Dieser Benutzername ist reserviert.")
    assert _nothing_created()
    assert userctx.get_mode() is None


@pytest.mark.parametrize("name", ["demo2", "tester", "my-demo", "tests2"])
def test_names_similar_to_the_reserved_ones_are_fine(name):
    assert _start(username=name, login="none")[:2] == (True, name)


def test_an_existing_name_is_reported():
    userctx.set_mode("multi")
    auth.create_user("alice", PASSWORD)
    ok, username, _, error = _start(username="alice", login="none")
    assert (ok, username) == (False, "alice")
    assert "existiert bereits" in error
    assert auth.is_admin("alice") is False               # der Benutzer wird nicht befördert
    assert userctx.get_auto_login() is None


@pytest.mark.parametrize("login", ["none", "password"])
def test_a_second_setup_is_refused_and_changes_nothing(login):
    installation.set_up(auto_login=(login == "none"))
    config_before = read_config()
    users_before = auth.list_users()
    result = _start(username="zweiter", login=login, password=PASSWORD, password2=PASSWORD)
    assert result == (None, "", False, None)
    assert read_config() == config_before
    assert auth.list_users() == users_before
    assert auth.user_exists("zweiter") is False


def test_the_setup_stays_open_while_there_is_no_administrator():
    # eingerichtet, aber nur ein normaler Benutzer (z. B. ein Administrator wurde von Hand entfernt)
    userctx.set_mode("multi")
    auth.create_user("alice", PASSWORD)
    assert _start(username="admin", login="password", password=PASSWORD, password2=PASSWORD)[0] is True
    assert auth.is_admin("admin") is True
    assert auth.is_admin("alice") is False


def test_a_headless_config_keeps_the_setup_open_and_its_other_keys():
    write_config({"mode": "multi", "extra": "bleibt"})
    auth.init_auth_db()                                  # das macht run_server() vor dem ersten Request
    assert _start(username="admin", login="none")[:2] == (True, "admin")
    assert read_config() == {"mode": "multi", "extra": "bleibt", "auto_login": "admin"}


def test_a_legacy_single_config_counts_as_not_set_up():
    # Den früheren Einzelbenutzer-Modus zieht layout.migrate() beim Start um; ohne Umzug gilt
    # "nicht eingerichtet" – die Einrichtung schreibt dann den neuen Modus
    write_config({"mode": "single"})
    assert _start(username="admin", login="none")[:2] == (True, "admin")
    assert read_config()["mode"] == "multi"


# ── Teil 1b: während der Demo ────────────────────────────────────────────────
@pytest.fixture
def running_demo(monkeypatch):
    """Demo-Daten ohne Administrator, Variable gesetzt und Marker vorhanden."""
    userctx.set_mode("multi")
    auth.create_user("demo", PASSWORD, is_admin=False, allow_reserved=True)
    demo_dir = os.path.join(userctx.DATA_ROOT, "users", "demo")
    os.makedirs(demo_dir)
    with open(os.path.join(demo_dir, demo.MARKER_FILE), "w", encoding="utf-8") as f:
        f.write("x")
    monkeypatch.setenv(demo.ENV_VAR, "1")


@pytest.mark.parametrize("login", ["none", "password"])
def test_the_setup_is_refused_while_the_demo_is_running_even_without_an_administrator(running_demo,
                                                                                      login):
    assert auth.count_admins() == 0
    config_before = read_config()
    result = _start(username="admin", login=login, password=PASSWORD, password2=PASSWORD)
    assert result == (None, "", False, None)
    assert auth.count_admins() == 0
    assert auth.user_exists("admin") is False
    assert read_config() == config_before                # kein auto_login eingetragen


def test_the_setup_is_open_again_when_the_demo_variable_is_gone(running_demo, monkeypatch):
    monkeypatch.delenv(demo.ENV_VAR)
    assert _start(username="admin", login="none")[:2] == (True, "admin")
    assert auth.is_admin("demo") is False


def test_the_setup_stays_open_with_the_variable_but_without_the_marker(running_demo):
    os.remove(os.path.join(userctx.DATA_ROOT, "users", "demo", demo.MARKER_FILE))
    assert _start(username="admin", login="none")[:2] == (True, "admin")


# ── Teil 2: PageSetupStart ───────────────────────────────────────────────────
def test_setup_page_posts_to_setup_start_with_both_buttons():
    html = PageSetupStart()
    assert re.search(r'<form method="POST" action="/setup/start">', html)
    assert 'name="login" value="none"' in html
    assert 'name="login" value="password"' in html
    for field in ("username", "password", "password2"):
        assert f'name="{field}"' in html
    assert "Ohne Anmeldung starten" in html and "Mit Passwort starten" in html


def test_setup_page_proposes_admin_as_name():
    assert 'name="username" required value="admin"' in PageSetupStart()


def test_setup_page_has_no_csrf_token_yet():
    html = PageSetupStart()
    assert "<meta name='csrf-token' content=''>" in html     # noch keine Sitzung
    assert '<input type="hidden" name="csrf"' not in html


def test_setup_page_shows_no_error_by_default():
    assert "error-box" not in PageSetupStart()


def test_setup_page_shows_the_error():
    html = PageSetupStart(error_msg="Die Passwörter stimmen nicht überein.", username="meinname")
    assert 'class="rectRounded error-box">Die Passwörter stimmen nicht überein.<' in html
    assert 'value="meinname"' in html


def test_setup_page_escapes_error_and_username():
    html = PageSetupStart(error_msg=XSS, username=XSS)
    assert XSS not in html
    assert "<script>alert(1)</script>" not in html
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 2     # Meldung und Eingabefeld
    assert 'value="&lt;script&gt;' in html


# ── Teil 3: HTTP ─────────────────────────────────────────────────────────────
def _cookie(response):
    token = response.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    return {"Cookie": f"session={token}"}


def _meta_csrf(html):
    return re.search(r"<meta name='csrf-token' content='([^']*)'>", html).group(1)


_BEFORE_SETUP = ["/", "/about", "/users", "/login", "/logout", "/masterdata", "/invoice",
                 "/miscellaneous", "/setup-admin", "/setup/mode", "/data/logos/x.png"]


@pytest.mark.parametrize("path", _BEFORE_SETUP)
def test_before_setup_every_page_leads_to_the_setup(server_port, path):
    resp = get(server_port, path)
    assert resp.status == 303
    assert resp.headers["Location"] == "/setup"


@pytest.mark.parametrize("path", ["/execute_sql", "/users/create", "/users/auto-login", "/login",
                                  "/setup-admin", "/setup/mode", "/setup/save", "/setup/load_testdata",
                                  "/masterdata/articles/delete?id=1", "/upload_receipts"])
def test_before_setup_every_post_leads_to_the_setup_too(server_port, path):
    resp = post(server_port, path, form={"x": "1", "username": "admin", "mode": "single"})
    assert resp.status == 303
    assert resp.headers["Location"] == "/setup"
    assert _nothing_created()


def test_a_post_redirected_to_the_setup_does_not_disturb_the_next_request_on_the_connection(server_port):
    # Die Weiterleitung kommt, bevor der Body gelesen wurde: er muss verworfen werden
    with KeepAlive(server_port) as conn:
        first = conn.post("/execute_sql", form={"sql_commands": "SELECT 1", "fuellung": "x" * 300})
        assert first.status == 303 and first.headers["Location"] == "/setup"
        second = conn.get("/setup")
        assert second.status == 200 and 'action="/setup/start"' in second.text


def test_before_setup_the_setup_page_is_shown(server_port):
    resp = get(server_port, "/setup")
    assert resp.status == 200
    assert "Ersteinrichtung" in resp.text
    assert 'action="/setup/start"' in resp.text
    assert "Abmelden" not in resp.text and "👤" not in resp.text


def test_before_setup_there_is_no_csrf_token(server_port):
    assert _meta_csrf(get(server_port, "/setup").text) == ""


def test_before_setup_the_stylesheet_is_served(server_port):
    assert get(server_port, "/buch.css").status == 200


def test_browsing_before_setup_creates_nothing(server_port):
    for path in _BEFORE_SETUP + ["/setup", "/buch.css", "/favicon.ico"]:
        get(server_port, path)
    assert _nothing_created()                           # keine config.json, auth.db oder buch.db


def test_setup_admin_is_gone(server_port):
    resp = get(server_port, "/setup-admin")
    assert resp.status == 303 and resp.headers["Location"] == "/setup"


def test_start_without_login_works_without_cookie_afterwards(server_port):
    resp = post(server_port, "/setup/start", form={"username": "admin", "login": "none"})
    assert resp.status == 303 and resp.headers["Location"] == "/"
    assert "Set-Cookie" not in resp.headers
    assert read_config() == {"mode": "multi", "auto_login": "admin"}
    assert auth.is_admin("admin") is True
    page = get(server_port, "/about")
    assert page.status == 200 and "👤 admin" in page.text and "Abmelden" not in page.text


def test_start_without_login_shows_the_contact_form_next(server_port):
    post(server_port, "/setup/start", form={"username": "admin", "login": "none"})
    assert get(server_port, "/").headers["Location"] == "/setup"      # erster Start: Stammdaten
    page = get(server_port, "/setup")
    assert page.status == 200 and "Eigene Kontaktdaten" in page.text
    assert 'action="/setup/start"' not in page.text


def test_start_with_password_starts_a_session(server_port):
    resp = post(server_port, "/setup/start",
                form={"username": "admin", "login": "password", "password": PASSWORD,
                      "password2": PASSWORD})
    assert resp.status == 303 and resp.headers["Location"] == "/"
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith("session=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert read_config() == {"mode": "multi"}
    assert get(server_port, "/about").headers["Location"] == "/login"     # ohne Cookie: Anmeldung
    page = get(server_port, "/about", headers=_cookie(resp))
    assert page.status == 200 and "👤 admin" in page.text and "Abmelden" in page.text
    assert _meta_csrf(page.text) == auth.csrf_for(_cookie(resp)["Cookie"].split("=", 1)[1])


def test_start_with_password_logs_in_with_that_password_later(server_port):
    post(server_port, "/setup/start", form={"username": "admin", "login": "password",
                                            "password": PASSWORD, "password2": PASSWORD})
    resp = post(server_port, "/login", headers={"Origin": own_origin(server_port)},
                form={"username": "admin", "password": PASSWORD})
    assert resp.status == 303 and resp.headers["Set-Cookie"].startswith("session=")


@pytest.mark.parametrize("fields, message", [
    pytest.param({"password": PASSWORD, "password2": "anderes123"}, "stimmen nicht überein",
                 id="ungleiche-Passwörter"),
    pytest.param({"password": "kurz", "password2": "kurz"}, "zu kurz", id="kurzes-Passwort"),
])
def test_start_with_bad_passwords_shows_the_form_again_and_creates_nothing(server_port, fields, message):
    resp = post(server_port, "/setup/start",
                form={"username": "meinname", "login": "password", **fields})
    assert resp.status == 200
    assert message in resp.text
    assert 'value="meinname"' in resp.text               # die Eingabe bleibt stehen
    assert 'action="/setup/start"' in resp.text
    assert _nothing_created()
    assert get(server_port, "/").headers["Location"] == "/setup"      # weiterhin nicht eingerichtet


@pytest.mark.parametrize("name, message", [
    ("ab", "Ungültiger Benutzername"),
    ("bad/name", "Ungültiger Benutzername"),
    ("tests", "reserviert"),
    ("demo", "reserviert"),
])
def test_start_with_a_bad_name_shows_the_form_again_and_creates_nothing(server_port, name, message):
    resp = post(server_port, "/setup/start", form={"username": name, "login": "none"})
    assert resp.status == 200 and message in resp.text
    assert _nothing_created()


def test_the_error_page_escapes_the_name(server_port):
    resp = post(server_port, "/setup/start", form={"username": XSS, "login": "none"})
    assert resp.status == 200
    assert XSS not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text


# -- zweite Einrichtung ---------------------------------------------------------
def test_a_second_setup_after_starting_without_login_is_403_and_changes_nothing(server_port):
    post(server_port, "/setup/start", form={"username": "admin", "login": "none"})
    config_before, users_before = read_config(), auth.list_users()
    csrf = auth.csrf_for("auto-login:admin")             # so kommt der Besucher an das Token
    resp = post(server_port, "/setup/start", csrf=csrf,
                form={"username": "zweiter", "login": "none"})
    assert resp.status == 403
    assert "bereits abgeschlossen" in resp.text
    assert read_config() == config_before and auth.list_users() == users_before


def test_a_second_setup_without_token_is_403_as_well(server_port):
    post(server_port, "/setup/start", form={"username": "admin", "login": "none"})
    resp = post(server_port, "/setup/start", form={"username": "zweiter", "login": "none"})
    assert resp.status == 403
    assert auth.user_exists("zweiter") is False


def test_a_second_setup_after_starting_with_password_is_403_and_changes_nothing(server_port):
    first = post(server_port, "/setup/start", form={"username": "admin", "login": "password",
                                                    "password": PASSWORD, "password2": PASSWORD})
    config_before, users_before = read_config(), auth.list_users()
    headers = _cookie(first)
    csrf = auth.csrf_for(headers["Cookie"].split("=", 1)[1])
    resp = post(server_port, "/setup/start", headers=headers, csrf=csrf,
                form={"username": "zweiter", "login": "none"})
    assert resp.status == 403 and "bereits abgeschlossen" in resp.text
    assert read_config() == config_before and auth.list_users() == users_before


def test_a_second_setup_without_session_after_starting_with_password_leads_to_the_login(server_port):
    post(server_port, "/setup/start", form={"username": "admin", "login": "password",
                                            "password": PASSWORD, "password2": PASSWORD})
    resp = post(server_port, "/setup/start", form={"username": "zweiter", "login": "none"})
    assert resp.status == 303 and resp.headers["Location"] == "/login"
    assert auth.user_exists("zweiter") is False


def test_the_removed_mode_route_changes_nothing(server_port):
    post(server_port, "/setup/start", form={"username": "admin", "login": "none"})
    config_before = read_config()
    resp = post(server_port, "/setup/mode", csrf=auth.csrf_for("auto-login:admin"),
                form={"mode": "single"})
    assert resp.status == 404
    assert read_config() == config_before
    userctx._mode_cache.clear()
    assert userctx.get_mode() == "multi"


def test_the_old_admin_page_redirects_to_the_setup_for_a_session(server_port):
    first = post(server_port, "/setup/start", form={"username": "admin", "login": "password",
                                                    "password": PASSWORD, "password2": PASSWORD})
    resp = get(server_port, "/setup-admin", headers=_cookie(first))
    assert resp.status == 303 and resp.headers["Location"] == "/setup"


# -- Herkunft und Hostname ------------------------------------------------------
def test_start_with_foreign_origin_is_403_and_creates_nothing(server_port):
    resp = post(server_port, "/setup/start", headers={"Origin": FOREIGN_ORIGIN},
                form={"username": "admin", "login": "none"})
    assert resp.status == 403
    assert _nothing_created()
    assert userctx.get_mode() is None


def test_start_with_foreign_fetch_site_is_403_and_creates_nothing(server_port):
    resp = post(server_port, "/setup/start", headers={"Sec-Fetch-Site": "cross-site"},
                form={"username": "admin", "login": "none"})
    assert resp.status == 403
    assert _nothing_created()


def test_start_with_own_origin_is_accepted(server_port):
    resp = post(server_port, "/setup/start", headers={"Origin": own_origin(server_port)},
                form={"username": "admin", "login": "none"})
    assert resp.status == 303
    assert auth.is_admin("admin") is True


def test_the_setup_page_under_a_foreign_host_is_403(server_port):
    resp = get(server_port, "/setup", headers={"Host": "evil.example"})
    assert resp.status == 403
    assert "evil.example" not in resp.text


def test_start_under_a_foreign_host_is_403_and_creates_nothing(server_port):
    # DNS-Rebinding: Origin und Host stimmen überein, der Name gehört aber jemand anderem
    resp = post(server_port, "/setup/start",
                headers={"Host": "evil.example", "Origin": FOREIGN_ORIGIN},
                form={"username": "admin", "login": "none"})
    assert resp.status == 403
    assert _nothing_created()


def test_the_setup_works_under_a_host_released_by_the_environment(server_port, monkeypatch):
    monkeypatch.setenv("UBUCHHALTUNG_ALLOWED_HOSTS", "buch.example.org")
    headers = {"Host": "buch.example.org", "Origin": "http://buch.example.org"}
    assert get(server_port, "/setup", headers={"Host": "buch.example.org"}).status == 200
    resp = post(server_port, "/setup/start", headers=headers,
                form={"username": "admin", "login": "none"})
    assert resp.status == 303
    assert auth.is_admin("admin") is True


@pytest.mark.parametrize("host", ["localhost", "nas.local", "192.0.2.10:2824", "[::1]:8080"])
def test_the_setup_works_under_local_hosts(server_port, host):
    assert get(server_port, "/setup", headers={"Host": host}).status == 200


# -- Headless: config.json vorab angelegt ----------------------------------------
@pytest.fixture
def headless():
    """Wie nach ``{"mode":"multi"}`` von Hand; run_server() legt die Auth-Tabellen vorab an."""
    write_config({"mode": "multi"})
    auth.init_auth_db()


def test_a_headless_config_shows_the_setup(server_port, headless):
    assert get(server_port, "/").headers["Location"] == "/setup"
    page = get(server_port, "/setup")
    assert page.status == 200 and 'action="/setup/start"' in page.text


def test_a_headless_config_can_be_set_up(server_port, headless):
    resp = post(server_port, "/setup/start", form={"username": "admin", "login": "none"})
    assert resp.status == 303
    assert read_config() == {"mode": "multi", "auto_login": "admin"}
    assert "👤 admin" in get(server_port, "/about").text


def test_a_headless_config_with_only_a_normal_user_still_shows_the_setup(server_port, headless):
    auth.create_user("alice", PASSWORD)
    assert get(server_port, "/login").headers["Location"] == "/setup"      # keine Anmeldeseite
    resp = post(server_port, "/login", headers={"Origin": own_origin(server_port)},
                form={"username": "alice", "password": PASSWORD})
    assert resp.status == 303 and resp.headers["Location"] == "/setup"
    assert "Set-Cookie" not in resp.headers                  # alice meldet sich nicht an
    assert auth.count_admins() == 0
