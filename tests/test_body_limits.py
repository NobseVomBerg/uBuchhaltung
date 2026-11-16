# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Obergrenzen für Anfragegrößen (2.3.6), gegen einen echten Server auf 127.0.0.1.

Vorher las jede Stelle so viele Bytes, wie ``Content-Length`` behauptete. Jetzt gilt:

* Die erklärte Länge muss aus ASCII-Ziffern bestehen (kein ``abc``, ``-1``, ``1e3``, ``+5``), und es darf kein
  ``Transfer-Encoding`` dastehen (``chunked`` kennt der Server nicht): sonst 400 „Ungültige Anfrage.“.
* Ist sie größer als die Grenze der Route – 64 KB ohne angemeldeten Benutzer (``/login``, ``/logout``,
  ``/setup/start``), 10 MB für Formulare und JSON, 200 MB für ``/upload_receipts`` und ``/wiso/import`` –,
  kommt 413, **ohne dass der Body gelesen wird** (die Tests schicken nur die Kopfzeilen: Läse der Server den
  Body, bliebe er bis zum Timeout hängen), und die Verbindung wird geschlossen.
* ``_drain_body`` liest höchstens 1 MB weg; was darüber erklärt ist, bleibt liegen, und die Verbindung geht zu.

Die Antwort ist je nach Route HTML oder JSON; die Upload-Seite liest ``data.error``. Namen und Zugangsdaten
sind erfundene Testwerte.
"""
import email.message
import http.client
import json
import re
import socket
from types import SimpleNamespace

import pytest

from server import app as server_app
from server.app import (MAX_DRAIN_BODY, MAX_FORM_BODY, MAX_PREAUTH_BODY, MAX_UPLOAD_BODY,
                        SimpleWebServer)
from tests import installation
from tests.http_client import FOREIGN_ORIGIN, TIMEOUT, KeepAlive, get, own_origin

MULTIPART = {'Content-Type': 'multipart/form-data; boundary=GRENZE'}
FORM = {'Content-Type': 'application/x-www-form-urlencoded'}
JSON = {'Content-Type': 'application/json'}
INVALID = 'Ungültige Anfrage.'

# Werte, die keine gültige Länge sind (ein „²“ gilt für ``str.isdigit()``, ist aber keine ASCII-Ziffer)
BAD_LENGTHS = ['abc', '-1', '1e3', '+5', '1.5', '1 2', '0x10', '1_0', '²', '1' * 16]


def exchange(port, path, headers, body=None, method='POST', half_close=False):
    """Eine Anfrage mit genau diesen Kopfzeilen (ein fehlendes ``Content-Length`` fügt niemand hinzu). Liefert
    ``(Status, Antwort-Objekt, Body)``; ``half_close`` schließt danach die Senderichtung (der Body war kürzer
    als erklärt)."""
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=TIMEOUT)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders(body)
        if half_close:
            conn.sock.shutdown(socket.SHUT_WR)
        resp = conn.getresponse()
        return resp.status, resp, resp.read()
    finally:
        conn.close()


def assert_closed(resp):
    assert resp.will_close and resp.getheader('Connection') == 'close'


def headers_of(pairs):
    """Header-Objekt wie im Handler (``email.message.Message``)."""
    msg = email.message.Message()
    for name, value in pairs.items():
        msg[name] = value
    return msg


# ── Routen: Einrichtung, Anmeldung ───────────────────────────────────────────
@pytest.fixture
def login_site():
    """Anmeldung mit Passwort (keine automatische): vor der Anmeldung gilt die kleine Grenze."""
    installation.set_up(auto_login=False)


def login_headers(port, **extra):
    return {'Origin': own_origin(port), **FORM, **extra}


def test_login_over_the_preauth_limit_is_413_without_reading_the_body(server_port, login_site):
    status, resp, body = exchange(server_port, '/login', login_headers(
        server_port, **{'Content-Length': str(MAX_PREAUTH_BODY + 1)}))
    assert status == 413
    assert 'Die Anfrage ist zu groß (höchstens 64 KB).' in body.decode()
    assert_closed(resp)
    assert get(server_port, '/login').status == 200               # der Server bedient danach weiter


def test_login_exactly_at_the_limit_is_still_read(server_port, login_site):
    prefix = 'username=alice&password=falsch&fuellung='
    body = (prefix + 'x' * (MAX_PREAUTH_BODY - len(prefix))).encode()
    assert len(body) == MAX_PREAUTH_BODY
    status, resp, answer = exchange(server_port, '/login', login_headers(
        server_port, **{'Content-Length': str(len(body))}), body)
    assert status == 200 and 'falsch' in answer.decode()
    assert not resp.will_close


def test_logout_has_the_same_small_limit(server_port, login_site):
    status, resp, _ = exchange(server_port, '/logout', {'Origin': own_origin(server_port), **FORM,
                                                        'Content-Length': str(MAX_PREAUTH_BODY + 1)})
    assert status == 413
    assert_closed(resp)


def test_the_first_setup_has_the_small_limit_too(server_port):
    # keine eingerichtete Installation: noch niemand ist angemeldet
    status, resp, body = exchange(server_port, '/setup/start', {**FORM, 'Content-Length': str(MAX_PREAUTH_BODY + 1)})
    assert status == 413 and 'höchstens 64 KB' in body.decode()
    assert_closed(resp)


@pytest.mark.parametrize('length', [None] + BAD_LENGTHS)
def test_login_without_a_usable_length_is_400(server_port, login_site, length):
    headers = login_headers(server_port, **({} if length is None else {'Content-Length': length}))
    status, resp, body = exchange(server_port, '/login', headers)
    assert status == 400 and INVALID in body.decode()
    assert_closed(resp)
    assert get(server_port, '/login').status == 200


def test_login_with_chunked_encoding_is_400(server_port, login_site):
    status, resp, body = exchange(server_port, '/login', login_headers(
        server_port, **{'Transfer-Encoding': 'chunked'}), b'5\r\nhallo\r\n0\r\n\r\n')
    assert status == 400 and INVALID in body.decode()
    assert_closed(resp)


def test_a_body_that_is_not_utf8_is_400_and_keeps_the_connection(server_port, login_site):
    body = b'username=alice&password=\xff\xfe'
    with KeepAlive(server_port) as conn:
        first = conn.request('POST', '/login', login_headers(server_port), body=body)
        assert first.status == 400 and INVALID in first.text
        assert conn.get('/login').status == 200


def test_a_body_shorter_than_declared_is_400(server_port, login_site):
    status, resp, body = exchange(server_port, '/login', login_headers(
        server_port, **{'Content-Length': '100'}), b'username=alice', half_close=True)
    assert status == 400 and INVALID in body.decode()
    assert_closed(resp)


# ── Routen nach der Anmeldung: Formular, JSON, Uploads ───────────────────────
FORM_ROUTE = '/masterdata/articles/add'
JSON_ROUTE = '/invoice/status'
UPLOAD_ROUTE = '/upload_receipts'
WISO_ROUTE = '/wiso/import'


@pytest.fixture(params=['Formular', 'JSON', 'Upload', 'WISO-Import'])
def route(request, auto_login):
    """Eine der vier Routen, die ihren Body über ``_read_body`` bzw. selbst lesen: Pfad, Kopfzeilen (mit dem
    CSRF-Token, wo es vor dem Lesen geprüft wird), Grenze und ob die Antwort JSON oder HTML ist."""
    csrf = auto_login.csrf
    return {
        'Formular': SimpleNamespace(path=FORM_ROUTE, headers=FORM, limit=MAX_FORM_BODY, as_json=False),
        'JSON': SimpleNamespace(path=JSON_ROUTE, headers={**JSON, 'X-CSRF-Token': csrf},
                                limit=MAX_FORM_BODY, as_json=True),
        'Upload': SimpleNamespace(path=UPLOAD_ROUTE, headers={**MULTIPART, 'X-CSRF-Token': csrf},
                                  limit=MAX_UPLOAD_BODY, as_json=True),
        'WISO-Import': SimpleNamespace(path=WISO_ROUTE, headers=MULTIPART,
                                       limit=MAX_UPLOAD_BODY, as_json=False),
    }[request.param]


def error_text(route, body):
    """Der Fehlertext der Antwort: das JSON-Feld ``error`` (die Upload-Seite liest ``data.error``) bzw. der
    Absatz der HTML-Antwort."""
    if route.as_json:
        data = json.loads(body)
        assert data['success'] is False
        return data['error']
    return re.search(r'<p>(.*)</p>', body.decode()).group(1)


def limit_text(limit):
    return f'{limit // (1024 * 1024)} MB'


def test_a_body_over_the_limit_is_413_without_being_read(server_port, route):
    status, resp, body = exchange(server_port, route.path, {**route.headers, 'Content-Length': str(route.limit + 1)})
    assert status == 413
    assert f'Die Anfrage ist zu groß (höchstens {limit_text(route.limit)}).' in error_text(route, body)
    assert_closed(resp)
    assert get(server_port, '/about').status == 200


@pytest.mark.parametrize('length', [None] + BAD_LENGTHS)
def test_a_missing_or_unusable_length_is_400_without_being_read(server_port, route, length):
    headers = dict(route.headers)
    if length is not None:
        headers['Content-Length'] = length
    status, resp, body = exchange(server_port, route.path, headers)
    assert status == 400 and error_text(route, body) == INVALID
    assert_closed(resp)
    assert get(server_port, '/about').status == 200


def test_chunked_encoding_is_400(server_port, route):
    status, resp, body = exchange(server_port, route.path, {**route.headers, 'Transfer-Encoding': 'chunked'},
                                  b'5\r\nhallo\r\n0\r\n\r\n')
    assert status == 400 and error_text(route, body) == INVALID
    assert_closed(resp)


def test_a_form_over_the_preauth_limit_is_fine_once_somebody_is_logged_in(server_port, auto_login):
    # 64 KB + 1 sind nach der Anmeldung kein Grund zur Ablehnung: Das Formular wird gelesen und scheitert erst am Token
    body = b'csrf=falsch&fuellung=' + b'x' * MAX_PREAUTH_BODY
    status, resp, _ = exchange(server_port, FORM_ROUTE, {**FORM, 'Content-Length': str(len(body))}, body)
    assert status == 403
    assert not resp.will_close


def test_a_rejected_upload_with_a_huge_body_is_not_drained_and_closes_the_connection(server_port, auto_login):
    # kein Token: abgelehnt, bevor irgendein Handler liest – die erklärten 5 MB werden nicht weggelesen
    declared = 5 * 1024 * 1024
    assert declared > MAX_DRAIN_BODY
    status, resp, _ = exchange(server_port, UPLOAD_ROUTE, {**MULTIPART, 'Content-Length': str(declared)})
    assert status == 403
    assert_closed(resp)


def test_a_post_from_a_foreign_origin_with_a_huge_body_is_403_and_closes_the_connection(server_port, auto_login):
    declared = 5 * 1024 * 1024
    status, resp, _ = exchange(server_port, FORM_ROUTE, {'Origin': FOREIGN_ORIGIN, **FORM,
                                                         'Content-Length': str(declared)})
    assert status == 403
    assert_closed(resp)


def test_a_rejected_post_with_a_small_body_is_drained_and_keeps_the_connection(server_port, auto_login):
    with KeepAlive(server_port) as conn:
        first = conn.post(FORM_ROUTE, headers={'Origin': FOREIGN_ORIGIN}, form={'fuellung': 'x' * 300})
        assert first.status == 403
        assert conn.get('/about').status == 200


def test_a_post_with_chunked_encoding_that_is_refused_early_closes_the_connection(server_port, auto_login):
    status, resp, _ = exchange(server_port, FORM_ROUTE, {'Origin': FOREIGN_ORIGIN, **FORM,
                                                         'Transfer-Encoding': 'chunked'}, b'0\r\n\r\n')
    assert status == 403
    assert_closed(resp)


def test_a_get_without_a_body_keeps_the_connection_when_it_is_redirected_to_the_login(server_port, login_site):
    # _drain_body hat hier nichts zu lesen und darf die Verbindung nicht schließen
    with KeepAlive(server_port) as conn:
        first = conn.get('/about')
        assert first.status == 303 and first.headers['Location'] == '/login'
        assert conn.get('/login').status == 200


# ── die erklärte Länge, ohne Server ──────────────────────────────────────────
@pytest.mark.parametrize('headers, expected', [
    pytest.param({'Content-Length': '0'}, 0, id='null'),
    pytest.param({'Content-Length': '42'}, 42, id='Zahl'),
    pytest.param({'Content-Length': ' 7 '}, 7, id='Leerraum-aussen'),
    pytest.param({'Content-Length': '007'}, 7, id='führende-Nullen'),
    pytest.param({'Content-Length': '9' * 15}, int('9' * 15), id='fünfzehn-Stellen'),
    pytest.param({'Content-Length': '9' * 16}, None, id='sechzehn-Stellen'),
    pytest.param({}, None, id='fehlt'),
    pytest.param({'Content-Length': ''}, None, id='leer'),
    pytest.param({'Content-Length': 'abc'}, None, id='Buchstaben'),
    pytest.param({'Content-Length': '-1'}, None, id='negativ'),
    pytest.param({'Content-Length': '+5'}, None, id='Plus'),
    pytest.param({'Content-Length': '1e3'}, None, id='Exponent'),
    pytest.param({'Content-Length': '²'}, None, id='keine-ASCII-Ziffer'),
    pytest.param({'Content-Length': '5', 'Transfer-Encoding': 'chunked'}, None, id='chunked-trotz-Laenge'),
    pytest.param({'Content-Length': '5', 'Transfer-Encoding': 'identity'}, None, id='jedes-Transfer-Encoding'),
    pytest.param({'Transfer-Encoding': 'chunked'}, None, id='chunked-ohne-Laenge'),
])
def test_declared_length(headers, expected):
    handler = SimpleNamespace(headers=headers_of(headers))
    assert SimpleWebServer._declared_length(handler) == expected


@pytest.mark.parametrize('limit, text', [(64 * 1024, '64 KB'), (1024, '1 KB'), (10 * 1024 * 1024, '10 MB'),
                                         (1024 * 1024, '1 MB'), (200 * 1024 * 1024, '200 MB'),
                                         (2 * 1024 ** 3, '2 GB'), (1536 * 1024 * 1024, '1536 MB')])
def test_size_text(limit, text):
    assert server_app._size_text(limit) == text


def test_a_length_that_is_declared_twice_is_none():
    """Zwei ``Content-Length``-Zeilen: Welche gälte, wäre Auslegungssache (und hinter einem Proxy eine
    andere als hier) – auch wenn beide gleich sind."""
    for values in (('5', '5'), ('5', '6'), ('0', '99999999')):
        msg = email.message.Message()
        for value in values:
            msg['Content-Length'] = value
        assert SimpleWebServer._declared_length(SimpleNamespace(headers=msg)) is None


def test_a_login_with_two_lengths_is_400_and_closes_the_connection(server_port, login_site):
    conn = http.client.HTTPConnection('127.0.0.1', server_port, timeout=TIMEOUT)
    try:
        conn.putrequest('POST', '/login', skip_accept_encoding=True)
        for name, value in login_headers(server_port).items():
            conn.putheader(name, value)
        conn.putheader('Content-Length', '3')
        conn.putheader('Content-Length', '30')
        conn.endheaders(b'a=b')
        resp = conn.getresponse()
        assert resp.status == 400 and INVALID in resp.read().decode()
        assert_closed(resp)
    finally:
        conn.close()


def test_an_upload_without_a_content_type_is_400_as_json(server_port, auto_login):
    csrf = re.search(r"name='csrf-token' content='([0-9a-f]+)'", get(server_port, '/about').body.decode()).group(1)
    status, resp, body = exchange(server_port, '/upload_receipts',
                                  {'Origin': own_origin(server_port), 'X-CSRF-Token': csrf, 'Content-Length': '0'})
    assert status == 400 and json.loads(body)['error'] == 'Ungültiger Content-Type'


def test_the_limits_are_the_documented_ones():
    assert MAX_PREAUTH_BODY == 64 * 1024 and MAX_FORM_BODY == 10 * 1024 * 1024
    assert MAX_UPLOAD_BODY == 200 * 1024 * 1024 and MAX_DRAIN_BODY == 1024 * 1024
