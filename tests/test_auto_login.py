# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Automatische Anmeldung (``auto_login`` in config.json): der Betrieb für eine Person.

Fünf Teile ohne Server (die Anfragen selbst prüft test_auto_login_http.py):
  1. userctx: Einstellung in config.json, Cache, kaputte Dateien, Kennzeichen im Request,
  2. auth.auto_login_user(): gilt nur für einen vorhandenen Administrator,
  3. handlers.handle_user_auto_login(): ein- und ausschalten,
  4. der Schutz des Benutzers der automatischen Anmeldung und des Demo-Benutzers,
  5. die Benutzerverwaltung (PageUsers).

Benutzernamen und Passwörter sind erfundene Testwerte.
"""
import json
import os
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

import auth
import userctx
from server import handlers
from server.pages_users import PageUsers
from tests import installation
from tests.installation import PASSWORD, read_config, write_config

NEW_PASSWORD = "neues-passwort"
XSS = '<script>alert(1)</script>"\''


def _query(location):
    """Redirect-Ziel der Benutzerverwaltung als {'err'|'info': Text}."""
    assert location.startswith("/users")
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


@pytest.fixture
def users():
    """Eingerichtet, aber noch ohne automatische Anmeldung: Administratoren ``admin`` und
    ``carla``, normaler Benutzer ``bob``."""
    installation.set_up(auto_login=False, users=[("bob", False), ("carla", True)])


# ── Teil 1: userctx ──────────────────────────────────────────────────────────
def test_auto_login_is_none_before_setup():
    assert userctx.get_auto_login() is None


def test_set_auto_login_requires_a_set_up_installation():
    with pytest.raises(ValueError):
        userctx.set_auto_login("admin")
    assert not os.path.exists(userctx.config_path())     # nichts geschrieben
    assert userctx.get_auto_login() is None


def test_set_auto_login_persists_next_to_mode():
    userctx.set_mode("multi")
    userctx.set_auto_login("admin")
    assert read_config() == {"mode": "multi", "auto_login": "admin"}
    installation.forget_config_cache()                 # frisch aus der Datei gelesen
    assert userctx.get_mode() == "multi"
    assert userctx.get_auto_login() == "admin"


def test_set_auto_login_none_switches_it_off():
    userctx.set_mode("multi")
    userctx.set_auto_login("admin")
    userctx.set_auto_login(None)
    assert userctx.get_auto_login() is None
    assert read_config() == {"mode": "multi"}          # Schlüssel entfernt, Modus bleibt
    installation.forget_config_cache()
    assert userctx.get_mode() == "multi"
    assert userctx.get_auto_login() is None


def test_set_auto_login_replaces_the_name():
    userctx.set_mode("multi")
    userctx.set_auto_login("admin")
    userctx.set_auto_login("carla")
    assert userctx.get_auto_login() == "carla"
    assert read_config()["auto_login"] == "carla"


def test_set_auto_login_keeps_foreign_keys_in_config():
    write_config({"mode": "multi", "extra": {"a": [1, 2]}, "zahl": 7})
    userctx.set_auto_login("admin")
    assert read_config() == {"mode": "multi", "extra": {"a": [1, 2]}, "zahl": 7, "auto_login": "admin"}
    userctx.set_auto_login(None)
    assert read_config() == {"mode": "multi", "extra": {"a": [1, 2]}, "zahl": 7}


def test_set_mode_keeps_auto_login_and_foreign_keys_in_config():
    write_config({"auto_login": "admin", "extra": "bleibt"})
    userctx.set_mode("multi")
    assert read_config() == {"mode": "multi", "auto_login": "admin", "extra": "bleibt"}


def test_auto_login_in_config_is_ignored_before_setup():
    write_config({"auto_login": "admin"})              # kein "mode": nicht eingerichtet
    assert userctx.get_mode() is None
    assert userctx.get_auto_login() is None


@pytest.mark.parametrize("value", [1, True, ["admin"], {"name": "admin"}, "", None],
                         ids=["Zahl", "Wahrheitswert", "Liste", "Objekt", "leer", "null"])
def test_auto_login_with_wrong_type_in_config_means_off(value):
    write_config({"mode": "multi", "auto_login": value})
    assert userctx.get_auto_login() is None


@pytest.mark.parametrize("raw", ["", "{", "kein json", "[1, 2]", '"admin"', "null", "42", "true"],
                         ids=["leer", "abgeschnitten", "Text", "Liste", "Zeichenkette", "null",
                              "Zahl", "Wahrheitswert"])
def test_broken_or_non_object_config_does_not_crash(raw):
    write_config(raw)
    assert userctx.get_mode() is None
    assert userctx.auth_enabled() is False
    assert userctx.get_auto_login() is None


def test_a_broken_config_is_replaced_when_the_installation_is_set_up():
    write_config("[1, 2]")
    userctx.set_mode("multi")
    userctx.set_auto_login("admin")
    assert read_config() == {"mode": "multi", "auto_login": "admin"}


def test_auto_login_is_read_once_per_config_file():
    userctx.set_mode("multi")
    userctx.set_auto_login("admin")
    with open(userctx.config_path(), "w", encoding="utf-8") as f:
        f.write('{"mode": "multi"}')                   # von Hand geändert, ohne den Cache zu leeren
    assert userctx.get_auto_login() == "admin"         # bedient aus dem Speicher
    installation.forget_config_cache()
    assert userctx.get_auto_login() is None            # nach dem Leeren gilt die Datei


def test_the_cache_follows_set_auto_login_without_stale_values():
    userctx.set_mode("multi")
    assert userctx.get_auto_login() is None
    userctx.set_auto_login("admin")
    assert userctx.get_auto_login() == "admin"
    userctx.set_auto_login(None)
    assert userctx.get_auto_login() is None


def test_auto_login_is_kept_per_data_root(tmp_path, monkeypatch):
    userctx.set_mode("multi")
    userctx.set_auto_login("admin")
    other = tmp_path / "andere-installation"
    other.mkdir()
    (other / "config.json").write_text(json.dumps({"mode": "multi", "auto_login": "carla"}),
                                       encoding="utf-8")
    with monkeypatch.context() as elsewhere:
        elsewhere.setattr(userctx, "DATA_ROOT", str(other))
        assert userctx.get_auto_login() == "carla"     # nicht der Wert der ersten Installation
    assert userctx.get_auto_login() == "admin"         # und umgekehrt (beide stehen im Cache)


def test_auto_login_flag_defaults_to_off():
    assert userctx.auto_login_active() is False


def test_set_auto_login_active_marks_the_current_request():
    userctx.set_auto_login_active(True)
    assert userctx.auto_login_active() is True
    userctx.set_auto_login_active(False)
    assert userctx.auto_login_active() is False


def test_auto_login_flag_is_per_thread():
    userctx.set_auto_login_active(True)
    seen = []
    thread = threading.Thread(target=lambda: seen.append(userctx.auto_login_active()))
    thread.start()
    thread.join()
    assert seen == [False]                              # der Nachbar-Request ist nicht betroffen
    assert userctx.auto_login_active() is True


def test_clear_resets_user_token_and_auto_login_flag():
    userctx.set_user("admin")
    userctx.set_csrf_token("token")
    userctx.set_auto_login_active(True)
    userctx.clear()
    assert userctx.get_user() is None
    assert userctx.get_csrf_token() == ""
    assert userctx.auto_login_active() is False


# ── Teil 2: auth.auto_login_user() ───────────────────────────────────────────
def test_auto_login_user_is_the_configured_administrator(users):
    userctx.set_auto_login("admin")
    assert auth.auto_login_user() == "admin"


def test_auto_login_user_is_none_without_the_setting(users):
    assert auth.auto_login_user() is None


def test_auto_login_user_is_none_for_an_unknown_user(users):
    userctx.set_auto_login("gespenst")
    assert userctx.get_auto_login() == "gespenst"
    assert auth.auto_login_user() is None


def test_auto_login_user_is_none_for_a_non_administrator(users):
    userctx.set_auto_login("bob")
    assert auth.auto_login_user() is None


def test_auto_login_user_follows_the_administrator_flag(users):
    userctx.set_auto_login("carla")
    assert auth.auto_login_user() == "carla"
    auth.set_admin("carla", False)
    assert auth.auto_login_user() is None               # herabgestuft: die Einstellung gilt nicht mehr
    auth.set_admin("carla", True)
    assert auth.auto_login_user() == "carla"


def test_auto_login_user_is_none_after_the_user_is_deleted(users):
    userctx.set_auto_login("carla")
    auth.delete_user("carla")
    assert auth.auto_login_user() is None


def test_auto_login_user_ignores_the_setting_before_setup():
    auth.create_user("admin", PASSWORD, is_admin=True)
    write_config({"auto_login": "admin"})               # ohne "mode": multi
    assert auth.auto_login_user() is None


def test_auto_login_user_with_explicit_auth_db(tmp_path):
    other_db = str(tmp_path / "andere-auth.db")
    auth.create_user("admin", PASSWORD, is_admin=True, db_path=other_db)
    auth.init_auth_db()                                 # Standard-auth.db ohne Benutzer
    userctx.set_mode("multi")
    userctx.set_auto_login("admin")
    assert auth.auto_login_user() is None               # in der Standard-auth.db gibt es keinen admin
    assert auth.auto_login_user(other_db) == "admin"


# ── Teil 3: handle_user_auto_login() ─────────────────────────────────────────
def _enable(current_user, **extra):
    post_data = {"action": ["enable"], **{k: [v] for k, v in extra.items()}}
    return handlers.handle_user_auto_login(post_data, current_user)


def _disable(password=NEW_PASSWORD, password2=None):
    post_data = {"action": ["disable"], "password": [password],
                 "password2": [password if password2 is None else password2]}
    return handlers.handle_user_auto_login(post_data, "admin")


def test_enable_switches_it_on_for_the_administrator(users):
    location, login_as = _enable("admin")
    assert login_as is None
    assert "admin" in _query(location)["info"]
    assert userctx.get_auto_login() == "admin"
    assert read_config()["auto_login"] == "admin"
    assert auth.auto_login_user() == "admin"


def test_enable_is_only_for_oneself(users):
    # Ein mitgeschickter Benutzername ändert nichts: eingeschaltet wird für den Aufrufer
    location, _ = _enable("admin", username="carla")
    assert "info" in _query(location)
    assert userctx.get_auto_login() == "admin"


@pytest.mark.parametrize("current_user", ["bob", "gespenst", None, ""],
                         ids=["Nicht-Administrator", "unbekannt", "niemand", "leer"])
def test_enable_is_refused_for_anyone_but_an_administrator(users, current_user):
    location, login_as = _enable(current_user)
    assert login_as is None
    assert "Administrator" in _query(location)["err"]
    assert userctx.get_auto_login() is None
    assert "auto_login" not in read_config()


@pytest.mark.parametrize("requester", ["admin", "carla", "bob"])
def test_enable_when_already_active_is_an_error(users, requester):
    userctx.set_auto_login("admin")
    location, login_as = _enable(requester)
    assert login_as is None
    assert "bereits aktiv" in _query(location)["err"]
    assert userctx.get_auto_login() == "admin"          # der bisherige bleibt


@pytest.mark.parametrize("stale", ["gespenst", "bob"], ids=["Benutzer-gibt-es-nicht", "kein-Administrator"])
def test_enable_replaces_a_setting_that_no_longer_applies(users, stale):
    userctx.set_auto_login(stale)                       # gilt nicht (auto_login_user() ist None)
    location, _ = _enable("carla")
    assert "info" in _query(location)
    assert userctx.get_auto_login() == "carla"


def test_disable_when_not_active_is_an_error(users):
    location, login_as = _disable()
    assert login_as is None
    assert "nicht aktiv" in _query(location)["err"]
    assert auth.authenticate("admin", NEW_PASSWORD) is False


def test_disable_with_different_passwords_is_an_error_and_stays_active(users):
    userctx.set_auto_login("admin")
    location, login_as = _disable(NEW_PASSWORD, "anderes-passwort")
    assert login_as is None
    assert "stimmen nicht überein" in _query(location)["err"]
    assert userctx.get_auto_login() == "admin"
    assert read_config()["auto_login"] == "admin"
    assert auth.authenticate("admin", NEW_PASSWORD) is False      # Passwort unverändert
    assert auth.authenticate("admin", PASSWORD) is True


@pytest.mark.parametrize("password", ["", "kurz", "12345"], ids=["leer", "kurz", "fünf-Zeichen"])
def test_disable_with_a_too_short_password_is_an_error_and_stays_active(users, password):
    userctx.set_auto_login("admin")
    location, login_as = _disable(password)
    assert login_as is None
    assert "zu kurz" in _query(location)["err"]
    assert userctx.get_auto_login() == "admin"
    assert auth.authenticate("admin", PASSWORD) is True           # niemand sperrt sich aus


def test_disable_sets_the_password_and_switches_to_the_login(users):
    userctx.set_auto_login("admin")
    location, login_as = _disable()
    assert login_as == "admin"                          # für ihn legt der Server eine Sitzung an
    assert "ausgeschaltet" in _query(location)["info"]
    assert auth.authenticate("admin", NEW_PASSWORD) is True
    assert auth.authenticate("admin", PASSWORD) is False
    assert userctx.get_auto_login() is None
    assert auth.auto_login_user() is None
    assert read_config() == {"mode": "multi"}           # Eintrag entfernt, Modus bleibt
    assert auth.is_admin("admin") is True


def test_enable_after_disable_works_again(users):
    userctx.set_auto_login("admin")
    _disable()
    location, _ = _enable("admin")
    assert "info" in _query(location)
    assert auth.auto_login_user() == "admin"


@pytest.mark.parametrize("action", [None, "", "toggle", "ENABLE", "enable "],
                         ids=["fehlt", "leer", "unbekannt", "Großschreibung", "Leerzeichen"])
def test_unknown_action_is_an_error_and_changes_nothing(users, action):
    userctx.set_auto_login("admin")
    post_data = {} if action is None else {"action": [action]}
    location, login_as = handlers.handle_user_auto_login(post_data, "admin")
    assert login_as is None
    assert _query(location)["err"] == "Unbekannte Aktion."
    assert userctx.get_auto_login() == "admin"
    assert auth.authenticate("admin", PASSWORD) is True


# ── Teil 4: Schutz des Benutzers der automatischen Anmeldung ────────────────
def test_the_auto_login_user_cannot_be_deleted(users):
    userctx.set_auto_login("admin")
    location = handlers.handle_user_delete({"username": ["admin"]})
    assert "automatisch angemeldet" in _query(location)["err"]
    assert auth.user_exists("admin") is True


def test_the_auto_login_user_cannot_be_deleted_even_with_other_administrators(users):
    userctx.set_auto_login("carla")
    assert auth.count_admins() == 2
    handlers.handle_user_delete({"username": ["carla"]})
    assert auth.user_exists("carla") is True


def test_the_auto_login_user_cannot_be_demoted(users):
    userctx.set_auto_login("carla")
    location = handlers.handle_user_toggle_admin({"username": ["carla"]})
    assert "automatisch angemeldet" in _query(location)["err"]
    assert auth.is_admin("carla") is True


def test_other_users_can_still_be_deleted_and_demoted(users):
    userctx.set_auto_login("admin")
    assert "info" in _query(handlers.handle_user_toggle_admin({"username": ["carla"]}))
    assert auth.is_admin("carla") is False
    assert "info" in _query(handlers.handle_user_delete({"username": ["bob"]}))
    assert auth.user_exists("bob") is False


def test_after_switching_off_the_user_can_be_demoted_again(users):
    userctx.set_auto_login("carla")
    _, login_as = handlers.handle_user_auto_login(
        {"action": ["disable"], "password": [NEW_PASSWORD], "password2": [NEW_PASSWORD]}, "carla")
    assert login_as == "carla"
    assert "info" in _query(handlers.handle_user_toggle_admin({"username": ["carla"]}))
    assert auth.is_admin("carla") is False


# ── Teil 4b: der Demo-Benutzer wird nie Administrator ───────────────────────
@pytest.fixture
def demo_user(users):
    auth.create_user("demo", PASSWORD, is_admin=False, allow_reserved=True)


def test_the_demo_user_cannot_be_made_an_administrator(demo_user):
    location = handlers.handle_user_toggle_admin({"username": ["demo"]})
    assert "Demo" in _query(location)["err"]
    assert auth.is_admin("demo") is False
    assert auth.count_admins() == 2


@pytest.mark.parametrize("name", ["demo", "Demo", "DEMO", "tests"])
def test_reserved_names_cannot_be_created_in_the_user_management(users, name):
    location = handlers.handle_user_create({"username": [name], "password": [PASSWORD]})
    assert "reserviert" in _query(location)["err"]
    assert auth.user_exists(name) is False


def test_other_users_can_still_be_promoted(demo_user):
    location = handlers.handle_user_toggle_admin({"username": ["bob"]})
    assert "info" in _query(location)
    assert auth.is_admin("bob") is True


# ── Teil 5: Benutzerverwaltung (PageUsers) ───────────────────────────────────
def _page(auto_login_user=None, **kwargs):
    return PageUsers("admin", [("admin", 1, "2026-01-01 10:00:00"), ("bob", 0, "2026-01-02 10:00:00")],
                     "tok123", auto_login_user=auto_login_user, **kwargs)


def test_users_page_has_the_auto_login_box_in_both_states():
    assert "Automatische Anmeldung</h2>" in _page()
    assert "Automatische Anmeldung</h2>" in _page(auto_login_user="admin")


def test_users_page_offers_to_switch_it_on_when_off():
    html = _page()
    assert 'action="/users/auto-login"' in html
    assert 'name="action" value="enable"' in html
    assert "Für admin einschalten" in html
    assert 'value="disable"' not in html
    assert "Aktiv für" not in html


def test_users_page_offers_to_switch_it_off_and_set_a_password_when_on():
    html = _page(auto_login_user="admin")
    assert "Aktiv für <strong>admin</strong>" in html
    assert 'action="/users/auto-login"' in html
    assert 'name="action" value="disable"' in html
    assert 'name="password"' in html and 'name="password2"' in html
    assert 'value="enable"' not in html


@pytest.mark.parametrize("auto_login_user", [None, "admin"], ids=["aus", "ein"])
def test_users_page_switch_forms_carry_the_csrf_token(auto_login_user):
    html = _page(auto_login_user=auto_login_user)
    form = html[html.index('action="/users/auto-login"'):]
    form = form[:form.index("</form>")]
    assert 'name="csrf" value="tok123"' in form


def test_users_page_warns_before_enabling():
    html = _page()
    assert "confirm(" in html[html.index('action="/users/auto-login"'):]
    assert "Nur im eigenen Netz" in html


@pytest.mark.parametrize("kind", ["error_msg", "info_msg"])
def test_users_page_escapes_messages(kind):
    html = _page(**{kind: XSS})
    assert XSS not in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


@pytest.mark.parametrize("auto_login_user", [None, "admin"], ids=["aus", "ein"])
def test_users_page_escapes_names_too(auto_login_user):
    # Die Namensregel lässt <, " und ' gar nicht zu – die Seite verlässt sich nicht darauf
    html = PageUsers(XSS, [(XSS, 1, XSS)], "tok", auto_login_user=XSS if auto_login_user else None)
    assert XSS not in html
    assert "<script>alert(1)</script>" not in html
