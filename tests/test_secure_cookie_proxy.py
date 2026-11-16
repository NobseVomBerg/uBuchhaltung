# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Secure-Cookie hinter einem TLS-Proxy (2.3.6).

Spricht der Server selbst TLS (``UBUCHHALTUNG_CERT``/``-KEY``), tragen Sitzungs- und Abmelde-Cookie ``Secure``
(test_auto_login_http.py). Terminiert ein Reverse-Proxy das TLS und spricht mit dem Server unverschlüsselt,
meldet er es in ``X-Forwarded-Proto``: Gilt der **erste** Wert dieser Liste (der des Clients) als ``https``,
bekommen beide Cookies ebenfalls ``Secure`` – sonst nicht. Die Entscheidung gilt je Anfrage, nicht je Verbindung.

Namen und Zugangsdaten sind erfundene Testwerte.
"""
import email.message

import pytest

import auth
from server.app import forwarded_https
from tests import installation
from tests.http_client import KeepAlive, own_origin, post

PROTO = 'X-Forwarded-Proto'


def headers_of(pairs):
    msg = email.message.Message()
    for name, value in pairs.items():
        msg[name] = value
    return msg


def flags(response):
    return set(response.headers['Set-Cookie'].split('; ')[1:])


@pytest.mark.parametrize('value, expected', [
    pytest.param('https', True, id='https'),
    pytest.param('HTTPS', True, id='Schreibweise'),
    pytest.param('  https ', True, id='Leerraum'),
    pytest.param('https, http', True, id='erster-Wert-https'),
    pytest.param('https,http', True, id='ohne-Leerzeichen'),
    pytest.param('http', False, id='http'),
    pytest.param('http, https', False, id='erster-Wert-http'),
    pytest.param('', False, id='leer'),
    pytest.param(',https', False, id='leerer-erster-Wert'),
    pytest.param('httpss', False, id='nur-ähnlich'),
    pytest.param('wss', False, id='anderes-Schema'),
    pytest.param('https://', False, id='mit-Doppelpunkt'),
])
def test_forwarded_https(value, expected):
    assert forwarded_https(headers_of({PROTO: value})) is expected


def test_without_the_header_nothing_is_forwarded():
    assert forwarded_https(headers_of({})) is False


@pytest.fixture
def login_site():
    installation.set_up(auto_login=False)


def login(port, **headers):
    return post(port, '/login', headers={'Origin': own_origin(port), **headers},
                form={'username': 'admin', 'password': installation.PASSWORD})


def logout(port, **headers):
    token = auth.create_session('admin')
    return post(port, '/logout', headers={'Cookie': f'session={token}', **headers}, csrf=auth.csrf_for(token))


@pytest.mark.parametrize('request_headers, secure', [
    pytest.param({PROTO: 'https'}, True, id='https'),
    pytest.param({PROTO: 'HTTPS'}, True, id='Schreibweise'),
    pytest.param({PROTO: 'https, http'}, True, id='Kette-mit-https-vorn'),
    pytest.param({}, False, id='ohne-Header'),
    pytest.param({PROTO: 'http'}, False, id='http'),
    pytest.param({PROTO: 'http, https'}, False, id='Kette-mit-http-vorn'),
])
def test_the_session_and_the_logout_cookie_are_secure_only_behind_a_tls_proxy(server_port, login_site,
                                                                              request_headers, secure):
    resp = login(server_port, **request_headers)
    assert resp.status == 303 and resp.headers['Set-Cookie'].startswith('session=')
    assert {'HttpOnly', 'SameSite=Strict', 'Path=/'} <= flags(resp)
    assert ('Secure' in flags(resp)) is secure
    resp = logout(server_port, **request_headers)
    assert resp.status == 303 and 'Max-Age=0' in flags(resp)
    assert ('Secure' in flags(resp)) is secure


def test_the_cookie_of_the_first_setup_is_secure_behind_a_tls_proxy(server_port):
    form = {'username': 'admin', 'login': 'password', 'password': installation.PASSWORD,
            'password2': installation.PASSWORD}
    resp = post(server_port, '/setup/start', headers={PROTO: 'https'}, form=form)
    assert resp.status == 303 and 'Secure' in flags(resp)


def test_the_decision_is_made_per_request_not_per_connection(server_port, login_site):
    with KeepAlive(server_port) as conn:
        origin = {'Origin': own_origin(server_port)}
        first = conn.post('/login', headers={**origin, PROTO: 'https'},
                          form={'username': 'admin', 'password': installation.PASSWORD})
        second = conn.post('/login', headers=origin,
                           form={'username': 'admin', 'password': installation.PASSWORD})
    assert 'Secure' in flags(first) and 'Secure' not in flags(second)


def test_the_tls_server_still_marks_its_cookies_secure_without_the_header(tls_server_port):
    installation.set_up(auto_login=False)
    resp = login(tls_server_port)
    assert 'Secure' in flags(resp)
    assert 'Secure' in flags(logout(tls_server_port))


def test_the_header_does_not_change_anything_else(server_port, login_site):
    plain, proxied = login(server_port), login(server_port, **{PROTO: 'https'})
    assert plain.status == proxied.status == 303
    assert plain.headers['Location'] == proxied.headers['Location']
    assert flags(plain) | {'Secure'} == flags(proxied)
