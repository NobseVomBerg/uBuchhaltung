# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Sitzungen enden, wenn sich an der Anmeldung etwas ändert (2.3.6).

* Ein neues Passwort beendet alle Sitzungen des Benutzers – nur wer sein *eigenes* Passwort ändert, behält die
  Sitzung, mit der er es tut (``set_password(…, keep_session=…)``, ``handle_user_reset_password``).
* Die automatische Anmeldung ein- und ausschalten beendet alle Sitzungen aller Benutzer; nach dem Ausschalten
  gilt nur die neue Sitzung des Administrators (sie wird erst nach dem Löschen angelegt).
* Legt der Administrator einen Benutzer an, zu dessen Namen es schon ein nicht leeres Datenverzeichnis gibt,
  sagt die Erfolgsmeldung es dazu: Der neue Benutzer arbeitet mit diesen Daten weiter.

Namen und Passwörter sind erfundene Testwerte.
"""
import os
from urllib.parse import parse_qs, urlsplit

import pytest

import auth
import userctx
from server import handlers
from tests import installation
from tests.http_client import get, post

NEW_PASSWORD = 'neues-passwort-123'
RESET = '/users/reset-password'
SWITCH = '/users/auto-login'


def cookie(token):
    return {'Cookie': f'session={token}'}


def reset_password(port, token, user, password=NEW_PASSWORD):
    """Passwort von ``user`` setzen, angemeldet mit der Sitzung ``token`` (Administrator)."""
    return post(port, RESET, headers=cookie(token), csrf=auth.csrf_for(token),
                form={'username': user, 'password': password})


@pytest.fixture
def sessions():
    """Anmeldung mit Passwort: Administrator ``admin`` und Nutzerin ``alice``, jeweils mit zwei Sitzungen."""
    installation.set_up(auto_login=False, users=[('alice', False)])
    return {name: [auth.create_session(name), auth.create_session(name)] for name in ('admin', 'alice')}


def alive(tokens):
    return [auth.get_session_user(token) is not None for token in tokens]


# ── Passwort setzen ──────────────────────────────────────────────────────────
def test_setting_the_password_of_another_user_ends_all_of_that_users_sessions(server_port, sessions):
    resp = reset_password(server_port, sessions['admin'][0], 'alice')
    assert resp.status == 303 and resp.headers['Location'].startswith('/users?info=')
    assert alive(sessions['alice']) == [False, False]
    assert alive(sessions['admin']) == [True, True]                   # die des Administrators bleiben
    assert auth.authenticate('alice', NEW_PASSWORD) is True


def test_setting_the_own_password_keeps_the_current_session_and_ends_the_others(server_port, sessions):
    current, other = sessions['admin']
    resp = reset_password(server_port, current, 'admin')
    assert resp.status == 303 and resp.headers['Location'].startswith('/users?info=')
    assert alive([current, other]) == [True, False]
    assert alive(sessions['alice']) == [True, True]                   # fremde Sitzungen unberührt
    assert get(server_port, '/users', headers=cookie(current)).status == 200      # und sie trägt weiter
    assert auth.authenticate('admin', NEW_PASSWORD) is True


def test_a_refused_password_leaves_every_session_alone(server_port, sessions):
    resp = reset_password(server_port, sessions['admin'][0], 'alice', password='kurz')
    assert resp.status == 303 and resp.headers['Location'].startswith('/users?err=')
    assert alive(sessions['alice'] + sessions['admin']) == [True] * 4
    assert auth.authenticate('alice', installation.PASSWORD) is True


def test_set_password_keeps_only_the_named_session_of_that_user():
    installation.set_up(auto_login=False, users=[('alice', False), ('bob', False)])
    mine, other, bobs = auth.create_session('alice'), auth.create_session('alice'), auth.create_session('bob')
    auth.set_password('alice', NEW_PASSWORD, keep_session=mine)
    assert alive([mine, other, bobs]) == [True, False, True]
    auth.set_password('alice', NEW_PASSWORD + '!')                    # ohne Angabe: keine bleibt
    assert alive([mine, other, bobs]) == [False, False, True]


def test_the_handler_keeps_a_session_only_for_the_logged_in_user(sessions):
    userctx.set_user('admin')
    mine, other = sessions['admin']
    # Was nicht zum angemeldeten Benutzer gehört, rettet nichts – auch nicht Alices eigenes Token
    handlers.handle_user_reset_password({'username': ['alice'], 'password': [NEW_PASSWORD]}, sessions['alice'][0])
    assert alive(sessions['alice']) == [False, False] and alive([mine, other]) == [True, True]
    handlers.handle_user_reset_password({'username': ['admin'], 'password': [NEW_PASSWORD]}, mine)
    assert alive([mine, other]) == [True, False]
    handlers.handle_user_reset_password({'username': ['admin'], 'password': [NEW_PASSWORD + '!']})
    assert alive([mine, other]) == [False, False]                     # ohne Sitzungstoken (automatische Anmeldung)


# ── Automatische Anmeldung ───────────────────────────────────────────────────
def test_switching_the_automatic_login_on_ends_all_sessions(server_port, sessions):
    token = sessions['admin'][0]
    resp = post(server_port, SWITCH, headers=cookie(token), csrf=auth.csrf_for(token), form={'action': 'enable'})
    assert resp.status == 303 and resp.headers['Location'].startswith('/users?info=')
    assert userctx.get_auto_login() == 'admin'
    assert alive(sessions['admin'] + sessions['alice']) == [False] * 4
    assert installation.session_count() == 0


def test_switching_it_off_ends_all_sessions_and_only_the_new_one_counts(server_port, auto_login):
    auth.create_user('alice', installation.PASSWORD)
    old = [auth.create_session('admin'), auth.create_session('alice')]
    resp = post(server_port, SWITCH, csrf=auto_login.csrf,
                form={'action': 'disable', 'password': NEW_PASSWORD, 'password2': NEW_PASSWORD})
    assert resp.status == 303 and resp.headers['Location'].startswith('/users?info=')
    new = resp.headers['Set-Cookie'].split(';')[0].split('=', 1)[1]
    assert alive(old) == [False, False]
    assert auth.get_session_user(new) == 'admin'                      # erst gelöscht, dann angelegt
    assert installation.session_count() == 1
    assert get(server_port, '/users', headers=cookie(new)).status == 200


def test_a_refused_switch_leaves_the_sessions_alone(server_port, auto_login):
    old = auth.create_session('admin')
    resp = post(server_port, SWITCH, csrf=auto_login.csrf,
                form={'action': 'disable', 'password': NEW_PASSWORD, 'password2': 'anderes-123456'})
    assert resp.headers['Location'].startswith('/users?err=')
    assert alive([old]) == [True]


def test_delete_all_sessions_empties_the_table():
    installation.set_up(auto_login=False, users=[('alice', False)])
    tokens = [auth.create_session('admin'), auth.create_session('alice')]
    auth.delete_all_sessions()
    assert alive(tokens) == [False, False] and installation.session_count() == 0


# ── Benutzer anlegen: Hinweis auf ein vorhandenes Datenverzeichnis ───────────
def create(name='neuer'):
    location = handlers.handle_user_create({'username': [name], 'password': ['geheim-12345']})
    return parse_qs(urlsplit(location).query)


def users_dir(name='neuer'):
    return os.path.join(userctx.DATA_ROOT, userctx.USERS_DIR, name)


def test_a_new_user_without_a_data_directory_gets_the_plain_message():
    assert create() == {'info': ['Benutzer „neuer" angelegt.']}
    assert auth.user_exists('neuer')


def test_a_new_user_whose_name_has_a_data_directory_is_created_and_told_about_it():
    os.makedirs(users_dir())
    with open(os.path.join(users_dir(), 'buch.db'), 'wb') as f:
        f.write(b'Bestand')
    assert create() == {'info': ['Benutzer „neuer" angelegt. Für diesen Namen gibt es bereits ein '
                                 'Datenverzeichnis – der Benutzer arbeitet mit diesen Daten weiter.']}
    assert auth.user_exists('neuer')                                  # angelegt wird trotzdem


def test_an_empty_data_directory_is_nothing_to_report():
    os.makedirs(users_dir())
    assert create() == {'info': ['Benutzer „neuer" angelegt.']}


def test_something_that_is_no_directory_is_nothing_to_report():
    os.makedirs(os.path.dirname(users_dir()))
    with open(users_dir(), 'wb') as f:
        f.write(b'keine Mappe')
    assert create() == {'info': ['Benutzer „neuer" angelegt.']}
    assert auth.user_exists('neuer')


def test_a_refused_new_user_says_nothing_about_a_directory():
    auth.init_auth_db()
    os.makedirs(users_dir('kurz1'))
    with open(os.path.join(users_dir('kurz1'), 'x'), 'wb') as f:
        f.write(b'x')
    location = handlers.handle_user_create({'username': ['kurz1'], 'password': ['1234567']})
    assert list(parse_qs(urlsplit(location).query)) == ['err']
    assert not auth.user_exists('kurz1')
