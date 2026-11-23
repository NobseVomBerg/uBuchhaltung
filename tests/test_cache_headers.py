# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Jede Antwort trägt genau EINEN ``Cache-Control``-Kopf (2.5.15) – entschieden an der Route, nie an der Query.

Vorher schrieb ``end_headers`` bei jeder Antwort einen Kopf und entschied dabei an der ganzen Adresse samt Query: Endete sie auf ``.css``,
``.js`` oder ``.png``, galt sie als statische Datei – ``public, max-age=86400``, einen Tag lang in jedem Zwischenspeicher. So wurde jede
Seite mit einem angehängten Parameter öffentlich speicherbar (``/dashboard?x=.png``, ``/bwa/pdf?…&accounts=.png``). Die statischen
Dateien bekamen dazu zwei Köpfe: ``serve_static_file`` schrieb seinen eigenen, ``end_headers`` einen zweiten daneben.

* Seiten und Dokumente: genau ``no-cache``; eine Endung in der Query ändert daran nichts.
* Statische Dateien (``/buch.css``, ``/favicon.ico``, Logos): genau der Kopf aus ``serve_static_file``, auch bei 304.
* ``/backup/download``, ``/receipts/file`` und die Suchen: genau ``no-store`` – auch bei einer Ablehnung und mit einer Endung in der Query.
* Fehler, Weiterleitungen und eine Keep-Alive-Verbindung mit mehreren Antworten: nie zwei Köpfe, nie der Kopf der Vorgängerin.
* ``end_headers`` entscheidet nicht mehr an einer Endung.

Gemessen wird immer an ALLEN Werten des Kopfes (``get_all``), nicht am ersten: So fällt ein doppelt geschriebener Kopf auf.
Alle Namen und Inhalte sind erfundene Testwerte.
"""
import ast
import inspect
import io
import os
import socket
import textwrap

import pytest

import userctx
import version
from server.app import SimpleWebServer, _header_safe
from tests import installation
from tests.document_helpers import make_books
from tests.http_client import FOREIGN_ORIGIN, TIMEOUT, KeepAlive, get, own_origin, post

STYLESHEET = 'public, max-age=31536000, immutable'      # Versionierte Datei (?v=…): ein Jahr, ohne Nachfrage
ICON = 'public, max-age=86400'
PNG = b'\x89PNG\r\n\x1a\nBildbytes'
PDF = b'%PDF-1.4\n% ein Beleg\n'


# ── Hilfen ───────────────────────────────────────────────────────────────────
def cache_headers(response):
    """ALLE Werte des Kopfes ``Cache-Control`` einer Antwort (nicht nur der erste)."""
    return response.headers.get_all('Cache-Control') or []


def user_dir():
    return os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN)


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(content)
    return path


def raw_exchange(port, request):
    """Eine rohe Anfrage (Bytes) über einen Socket; liest, bis der Server schließt. Liefert den Statuscode und die Köpfe als Liste von
    ``(Name, Wert)``. Der Server schließt nach jeder Fehlerantwort von ``send_error`` (``Connection: close``)."""
    sock = socket.create_connection(('127.0.0.1', port), timeout=TIMEOUT)
    try:
        sock.sendall(request)
        received = b''
        while True:
            block = sock.recv(65536)
            if not block:
                break
            received += block
    finally:
        sock.close()
    head = received.split(b'\r\n\r\n', 1)[0].decode('latin-1').split('\r\n')
    return int(head[0].split()[1]), [tuple(line.split(': ', 1)) for line in head[1:]]


def raw_cache_headers(headers):
    return [value for name, value in headers if name.lower() == 'cache-control']


@pytest.fixture
def books(auto_login):
    """Eigene Firma, ein Kunde, eine Rechnung und ein Angebot (ohne eigene Firma wäre ``/dashboard`` die Ersteinrichtung)."""
    return make_books(auto_login)


# ── Seiten ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('path', ['/dashboard', '/about', '/bwa', '/transactions', '/invoice'])
def test_a_page_carries_exactly_one_no_cache_header(server_port, books, path):
    resp = get(server_port, path)
    assert resp.status == 200
    assert cache_headers(resp) == ['no-cache']


@pytest.mark.parametrize('path, status', [
    ('/dashboard?x=.png', 200), ('/dashboard?x=.css', 200), ('/bwa?year=2026&month=3&x=.png', 200),
    ('/bwa/pdf?year=2026&month=3&accounts=.png', 200), ('/transactions?q=.png', 200), ('/invoice?x=.js', 200),
    pytest.param('/about?a=1&b=.js', 200, id='about-ignoriert-die-Query'),             # /about liest nur das Ergebnis des DB-Exports aus der Query
    pytest.param('/gibtsnicht?a=1&b=.js', 404, id='unbekannte-Adresse-404')])         # die Fehlerseite einer unbekannten Adresse (früher stand hier /about)
def test_an_extension_at_the_end_of_the_address_never_makes_a_page_public(server_port, books, path, status):
    """Eine Seite wird nicht zur „statischen Datei“, weil ihre Adresse auf ``.png`` endet: Früher stand dort ``public, max-age=86400`` –
    auch bei der PDF-Auslieferung und bei der Fehlerseite einer unbekannten Adresse."""
    resp = get(server_port, path)
    assert resp.status == status
    assert cache_headers(resp) == ['no-cache']
    if path.startswith('/bwa/pdf'):
        assert resp.headers['Content-Type'] == 'application/pdf' and resp.body.startswith(b'%PDF-')


@pytest.mark.parametrize('route, status', [
    pytest.param('/invoice/pdf_download?id={invoice}&x=.png', 404, id='Rechnung-ohne-Datei'),
    pytest.param('/worktime/pdf?person=abc&x=.png', 400, id='Stundenzettel-ohne-Person')])
def test_a_document_route_with_an_extension_in_the_address_is_never_public_either(server_port, books, route, status):
    """Auch die Auslieferung eines Dokuments (hier ohne Datei oder Person: eine Fehlerantwort) trägt genau ``no-cache``."""
    resp = get(server_port, route.format(invoice=books.invoice))
    assert resp.status == status
    assert cache_headers(resp) == ['no-cache']


# ── Statische Dateien ────────────────────────────────────────────────────────
@pytest.mark.parametrize('path', ['/buch.css', f'/buch.css?v={version.APP_VERSION}', '/buch.css?v=1'])
def test_the_stylesheet_carries_exactly_its_one_year_header(server_port, auto_login, path):
    """``serve_static_file`` schreibt ``public, max-age=31536000, immutable``; ``end_headers`` schrieb bei ``/buch.css?v=…`` ein zweites
    ``no-cache`` dazu – der Browser las beide."""
    resp = get(server_port, path)
    assert resp.status == 200 and resp.headers['Content-Type'] == 'text/css'
    assert cache_headers(resp) == [STYLESHEET]


@pytest.mark.parametrize('path, expected', [
    ('/buch.css', STYLESHEET), (f'/buch.css?v={version.APP_VERSION}', STYLESHEET), ('/favicon.ico', ICON)])
def test_a_not_modified_answer_carries_the_same_one_header(server_port, auto_login, path, expected):
    first = get(server_port, path)
    stamp = first.headers['Last-Modified']
    assert first.status == 200 and cache_headers(first) == [expected]
    again = get(server_port, path, {'If-Modified-Since': stamp})
    assert again.status == 304 and again.body == b''
    assert cache_headers(again) == [expected]


def test_the_icon_carries_exactly_its_one_day_header(server_port, auto_login):
    resp = get(server_port, '/favicon.ico')
    assert resp.status == 200 and resp.headers['Content-Type'] == 'image/x-icon'
    assert cache_headers(resp) == [ICON]


@pytest.mark.parametrize('query', ['', '?v=1', '?x=.css'])
def test_a_logo_carries_exactly_the_no_cache_of_serve_static_file(server_port, auto_login, query):
    """``serve_static_file`` schreibt für ein Logo ``no-cache``; ``end_headers`` schrieb wegen der Endung ``.png`` ``public, max-age=86400``
    dazu: Die Antwort trug beide."""
    write(os.path.join(user_dir(), 'logos', 'firmenlogo.png'), PNG)
    resp = get(server_port, '/data/logos/firmenlogo.png' + query)
    assert resp.status == 200 and resp.body == PNG and resp.headers['Content-Type'] == 'image/png'
    assert cache_headers(resp) == ['no-cache']


def test_a_not_modified_logo_carries_the_one_no_cache_too(server_port, auto_login):
    write(os.path.join(user_dir(), 'logos', 'firmenlogo.png'), PNG)
    stamp = get(server_port, '/data/logos/firmenlogo.png').headers['Last-Modified']
    again = get(server_port, '/data/logos/firmenlogo.png', {'If-Modified-Since': stamp})
    assert again.status == 304 and cache_headers(again) == ['no-cache']


def test_a_shared_seed_logo_carries_exactly_the_one_no_cache_too(server_port, auto_login, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(str(tmp_path / 'seed_data' / 'private' / 'logo.png'), PNG)
    resp = get(server_port, '/seed_data/private/logo.png')
    assert resp.status == 200 and resp.body == PNG
    assert cache_headers(resp) == ['no-cache']


# ── Nie ablegen ──────────────────────────────────────────────────────────────
NEVER_STORED = [
    pytest.param('/backup/download?file=fehlt.zip', 404, id='Sicherung-fehlt'),
    pytest.param('/receipts/file?id=99999', 404, id='Beleg-fehlt'),
    pytest.param('/receipts/search?q=x', 200, id='Belegsuche'),
    pytest.param('/transactions/search?q=x', 200, id='Buchungssuche'),
]


@pytest.mark.parametrize('tail', ['', '&x=.png', '&x=.css', '&x=.js'])
@pytest.mark.parametrize('path, status', NEVER_STORED)
def test_downloads_and_searches_are_never_stored_whatever_the_address_ends_with(server_port, books, path, status, tail):
    resp = get(server_port, path + tail)
    assert resp.status == status
    assert cache_headers(resp) == ['no-store']


@pytest.mark.parametrize('path', ['/receipts/search?q=.png', '/transactions/search?q=.png', '/receipts/search?q=.js'])
def test_a_search_for_an_extension_is_never_stored(server_port, books, path):
    """Die Adresse endet auf den Suchbegriff: Eine Suche nach „.png“ sähe sonst wie eine statische Datei aus."""
    resp = get(server_port, path)
    assert resp.status == 200
    assert cache_headers(resp) == ['no-store']


@pytest.mark.parametrize('path', [
    '/backup/download?file=fehlt.zip', '/receipts/file?id=99999', '/receipts/search?q=x', '/transactions/search?q=x'])
def test_a_refusal_for_a_foreign_origin_is_never_stored_either(server_port, books, path):
    resp = get(server_port, path, {'Origin': FOREIGN_ORIGIN})
    assert resp.status == 403
    assert cache_headers(resp) == ['no-store']


def test_a_backup_download_carries_exactly_no_store(server_port, auto_login):
    """Der Weg mit Köpfen, die ``_send_backup_download`` selbst schreibt, und der Datei hinterher."""
    write(os.path.join(user_dir(), 'backup', '20260105_Backup.zip'), b'inhalt')
    resp = get(server_port, '/backup/download?file=20260105_Backup.zip&x=.png')
    assert resp.status == 200 and resp.body == b'inhalt'
    assert cache_headers(resp) == ['no-store']


@pytest.mark.parametrize('tail', ['', '&inline=1', '&x=.png'])
def test_a_receipt_file_carries_exactly_no_store(server_port, auto_login, tail):
    db = auto_login.db
    write(os.path.join(user_dir(), 'Belege', '2026', '26B001_rechnung.pdf'), PDF)
    db.insert_receipt('26B001', '2026-03-04', 'rechnung.pdf', 'Belege/2026/26B001_rechnung.pdf', '')
    receipt_id = db.fetch_receipts()[-1][0]
    resp = get(server_port, f'/receipts/file?id={receipt_id}{tail}')
    assert resp.status == 200 and resp.body == PDF
    assert cache_headers(resp) == ['no-store']


# ── Fehler und Weiterleitungen ───────────────────────────────────────────────
def answer(port, method, path, csrf):
    if method == 'POST':
        return post(port, path, headers={'Origin': own_origin(port)}, csrf=csrf)
    return get(port, path)


@pytest.mark.parametrize('method, path, status', [
    pytest.param('GET', '/gibt-es-nicht', 404, id='404'),
    pytest.param('GET', '/gibt-es-nicht.png', 404, id='404-mit-Endung'),
    pytest.param('GET', '/bwa/pdf', 400, id='400'),
    pytest.param('GET', '/masterdata/skr/add_standard', 405, id='405'),
    pytest.param('GET', '/contacts', 301, id='301'),
    pytest.param('POST', '/masterdata/skr/add_standard', 303, id='303'),
    pytest.param('POST', '/gibt-es-nicht', 404, id='404-nach-POST'),
])
def test_an_error_or_a_redirect_carries_exactly_one_no_cache_header(server_port, books, method, path, status):
    resp = answer(server_port, method, path, books.csrf)
    assert resp.status == status
    assert cache_headers(resp) == ['no-cache']


@pytest.mark.parametrize('request_bytes, status', [
    # Eine kaputte Anfragezeile geht ohne Leerzeile hinaus: So bleibt im Server nichts ungelesen, er schließt mit FIN statt mit RST
    pytest.param(b'XYZ\r\n', 400, id='kaputte-Anfragezeile'),
    pytest.param(b'GET / HTTP/9.9\r\n', 505, id='unbekannte-Version'),
    pytest.param(b'PUT /about HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n', 501, id='unbekannte-Methode'),
])
def test_an_error_of_the_standard_library_carries_exactly_one_header_too(server_port, books, request_bytes, status):
    """Auch die Fehlerantworten über ``send_error`` laufen durch ``end_headers`` – mit und ohne gesetzte Adresse."""
    code, headers = raw_exchange(server_port, request_bytes)
    assert code == status
    assert raw_cache_headers(headers) == ['no-cache']
    assert ('X-Frame-Options', 'DENY') in headers and ('X-Content-Type-Options', 'nosniff') in headers


# ── Mehrere Antworten auf EINER Verbindung ───────────────────────────────────
def test_each_answer_on_one_connection_carries_its_own_one_header(server_port, books):
    """Eine Keep-Alive-Verbindung beantwortet viele Anfragen mit derselben Instanz des Handlers: Dass ein Kopf schon geschrieben wurde,
    darf nicht in die nächste Antwort wandern (die Seite bekäme sonst gar keinen)."""
    sequence = [
        ('/buch.css', 200, STYLESHEET), ('/about', 200, 'no-cache'), ('/buch.css?v=1', 200, STYLESHEET),
        ('/receipts/search?q=x', 200, 'no-store'), ('/dashboard?x=.png', 200, 'no-cache'), ('/favicon.ico', 200, ICON),
        ('/gibt-es-nicht', 404, 'no-cache'), ('/data/logos/gibt-es-nicht.png', 404, 'no-cache'),
        ('/backup/download?file=fehlt.zip', 404, 'no-store'), ('/about', 200, 'no-cache'),
    ]
    with KeepAlive(server_port) as conn:
        stamp = conn.get('/buch.css').headers['Last-Modified']
        answers = [(path, status, expected, conn.get(path)) for path, status, expected in sequence]
        not_modified = conn.get('/buch.css', {'If-Modified-Since': stamp})
        after = conn.get('/about')
    for path, status, expected, resp in answers:
        assert resp.status == status and cache_headers(resp) == [expected], path
    assert not_modified.status == 304 and cache_headers(not_modified) == [STYLESHEET]
    assert after.status == 200 and cache_headers(after) == ['no-cache']


def test_the_interim_100_continue_does_not_take_the_header_of_the_final_answer(server_port, books):
    """Auf ``Expect: 100-continue`` schickt die Standardbibliothek zuerst ``100 Continue`` – auch diese Zwischenantwort läuft durch
    ``end_headers``. Der Client (``http.client``) überliest sie; die Endantwort trägt trotzdem genau ihren einen Kopf."""
    headers = {'Origin': own_origin(server_port), 'Expect': '100-continue'}
    resp = post(server_port, '/masterdata/skr/add_standard', headers=headers, csrf=books.csrf)
    assert resp.status == 303
    assert cache_headers(resp) == ['no-cache']


# ── Der Aufbau von end_headers ───────────────────────────────────────────────
def bare_handler(path='/about'):
    """Ein ``SimpleWebServer`` ohne Verbindung: Die Köpfe gehen mit ``end_headers`` in einen Puffer (``wfile``)."""
    handler = SimpleWebServer.__new__(SimpleWebServer)
    handler.request_version = 'HTTP/1.1'
    handler._headers_buffer = []
    handler.wfile = io.BytesIO()
    handler.close_connection = False
    handler.log_request = lambda *args, **kwargs: None
    if path is not None:
        handler.path = path
    return handler


def written_cache_headers(handler):
    head = handler.wfile.getvalue().split(b'\r\n\r\n', 1)[0].decode('latin-1').split('\r\n')
    return [line for line in head if line.lower().startswith('cache-control:')]


@pytest.mark.parametrize('name', ['Cache-Control', 'cache-control', 'CACHE-CONTROL', 'Cache-\r\nControl'])
def test_a_cache_control_of_the_handler_stays_the_only_one_whatever_its_spelling(name):
    """Verglichen wird der Name nach dem Bereinigen (``_header_safe``) und ohne Rücksicht auf die Schreibweise."""
    handler = bare_handler('/about')
    handler.send_header(name, 'private, max-age=60')
    handler.end_headers()
    assert written_cache_headers(handler) == [f'{_header_safe(name)}: private, max-age=60']


def test_a_cache_control_in_the_headers_of_respond_stays_the_only_one():
    handler = bare_handler('/about')
    handler.respond(200, 'Text', headers={'cache-control': 'private'})
    assert written_cache_headers(handler) == ['cache-control: private']


@pytest.mark.parametrize('path, expected', [
    ('/about', 'no-cache'), ('/dashboard?x=.png', 'no-cache'), ('/receipts/file?id=1&x=.png', 'no-store'),
    ('/backup/download', 'no-store'), ('/transactions/search?q=.png', 'no-store'),
    pytest.param(None, 'no-cache', id='kaputte-Anfragezeile-ohne-Adresse')])
def test_without_one_of_its_own_the_handler_gets_the_one_of_its_route(path, expected):
    handler = bare_handler(path)
    handler.end_headers()
    assert written_cache_headers(handler) == [f'Cache-Control: {expected}']


def test_the_mark_of_one_answer_is_gone_for_the_next_one_on_the_same_instance():
    handler = bare_handler('/about')
    handler.send_header('Cache-Control', 'private')
    handler.end_headers()
    handler.wfile = io.BytesIO()
    handler.end_headers()
    assert written_cache_headers(handler) == ['Cache-Control: no-cache']


def test_the_mark_is_gone_even_when_the_answer_could_not_be_sent():
    class Broken(io.RawIOBase):
        def write(self, data):
            raise BrokenPipeError

    handler = bare_handler('/about')
    handler.wfile = Broken()
    handler.send_header('Cache-Control', 'private')
    with pytest.raises(BrokenPipeError):
        handler.end_headers()
    assert handler._cache_control_sent is False


def test_end_headers_decides_on_the_route_and_never_on_an_extension():
    """Die Endungs-Regel gibt es nicht mehr: kein ``endswith``, keine Endung und kein ``public`` in ``end_headers`` – ``public`` schreibt allein
    ``serve_static_file`` (für die Dateien, die es ausliefert)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(SimpleWebServer.end_headers)))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    strings = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert 'endswith' not in attributes
    assert [text for text in strings if text.startswith('.') or 'public' in text] == []
