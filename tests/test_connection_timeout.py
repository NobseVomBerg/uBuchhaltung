# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zeitgrenze je Verbindung (2.5.8): ``SimpleWebServer.timeout``, ``_TimedReader``, ``_SteppedWriter`` und der Body, der nicht zu Ende
kommt (408).

Fehlfall: Eine Verbindung, auf der nichts mehr geschah – ein Client, der seinen Body nicht zu Ende schickte, die Antwort nicht abholte
oder eine Keep-Alive-Verbindung offen ließ –, hielt ihren Thread und (seit 2.5.6) die Lesesperre des Datenverzeichnisses beliebig lange:
Das Wiederherstellen fand dann nie einen Augenblick ohne laufende Anfrage. Jetzt gilt:

* Auf jeder Verbindung gilt die Zeitgrenze ``CONNECTION_TIMEOUT`` je Lese- bzw. Sendeschritt (``SimpleWebServer.timeout``); danach
  schließt der Server sie. Antworten gehen in Stücken von ``SEND_CHUNK`` hinaus (``_SteppedWriter``): Die Grenze gilt je Stück, wer
  stetig liest, bekommt alles; nach einem Fehlschlag geht nichts mehr hinaus, eine Fehlerantwort wartet nicht noch einmal die Frist ab.
* Ein Body, der nicht zu Ende kommt, ist 408 „Die Anfrage wurde nicht vollständig gesendet.“ mit ``Connection: close`` (``_read_body``).
  Wo ein Handler den Body selbst liest, schließt ``_TimedReader`` die Verbindung in jedem Fall; die Lesesperre ist danach frei.
* Die Bearbeitung selbst hat keine Zeitgrenze, und ein Upload, der stetig sendet, läuft beliebig lange.

Die Tests setzen die Zeitgrenze klein (``SimpleWebServer.timeout``, gilt für Verbindungen, die danach entstehen) und warten auf
Ereignisse mit großzügiger Frist (``WAIT``) – geprüft wird nie eine genaue Zeit. Alle Namen und Inhalte sind erfundene Testwerte.
"""
import hashlib
import io
import json
import os
import re
import socket
import socketserver
import struct
import threading
import time
import urllib.parse
from types import SimpleNamespace

import pytest

import userctx
from server import app as server_app
from server import backup as backup_mod
from server import dirlock
from tests import installation
from tests.http_client import get, own_origin

LIMIT = 1.0          # Zeitgrenze der Verbindungen in diesen Tests (die echte beträgt 120 Sekunden)
WAIT = 20            # Sekunden, die ein Test auf ein Ereignis wartet, bevor er scheitert
INCOMPLETE = 'Die Anfrage wurde nicht vollständig gesendet.'
FORM = {'Content-Type': 'application/x-www-form-urlencoded'}
JSON = {'Content-Type': 'application/json'}
MULTIPART = {'Content-Type': 'multipart/form-data; boundary=GRENZE'}
RAW = {'Content-Type': 'application/octet-stream'}
FORM_ROUTE = '/masterdata/articles/add'
JSON_ROUTE = '/invoice/status'


@pytest.fixture
def short_limit(monkeypatch):
    """Verbindungen, die ab jetzt entstehen, geben nach ``LIMIT`` Sekunden ohne Fortschritt auf."""
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', LIMIT)
    return LIMIT


# ── Hilfen ───────────────────────────────────────────────────────────────────
def admin_dir():
    return os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN)


def files_under(root):
    """Alle Dateien unter ``root`` (relativ), ohne die Datenbank samt Begleitdateien: Was ein abgebrochener Upload zurücklassen
    könnte, steht hier."""
    return sorted(os.path.relpath(os.path.join(base, name), root)
                  for base, _dirs, names in os.walk(root) for name in names if not name.startswith('buch.db'))


class Wire:
    """Eine rohe Verbindung zum Server: Der Test schickt Anfragen von Hand – auch nur zur Hälfte – und liest die Antworten."""

    def __init__(self, port):
        self.port = port
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=WAIT)
        self.stream = self.sock.makefile('rb')

    def send(self, raw):
        self.sock.sendall(raw)

    def request(self, method, path, headers=None, body=b'', declared=None):
        """Eine Anfrage schicken. ``declared`` ist die erklärte ``Content-Length`` (Standard: die Länge von ``body``): Ist sie
        größer, kommt der Rest des Bodys nie."""
        lines = [f'{method} {path} HTTP/1.1', f'Host: 127.0.0.1:{self.port}', f'Origin: {own_origin(self.port)}']
        if method == 'POST':
            lines.append(f'Content-Length: {len(body) if declared is None else declared}')
        lines += [f'{name}: {value}' for name, value in (headers or {}).items()]
        self.send(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii') + body)

    def response(self, pace=None):
        """Die nächste Antwort (Status, Kopfzeilen in Kleinbuchstaben, Body) – ``None``, wenn die Verbindung ohne Antwort zuging.
        ``pace`` = ``(Bytes, Sekunden)``: Der Body wird in so großen Stücken mit so langen Pausen dazwischen gelesen."""
        status_line = self.stream.readline()
        if not status_line:
            return None
        headers = {}
        while True:
            line = self.stream.readline()
            if line in (b'\r\n', b''):
                break
            name, _, value = line.decode('latin-1').partition(':')
            headers[name.strip().lower()] = value.strip()
        remaining = int(headers.get('content-length', '0'))
        pieces = []
        while remaining:
            piece = self.stream.read(remaining if pace is None else min(pace[0], remaining))
            assert piece, 'der Server hat die Antwort abgebrochen'
            pieces.append(piece)
            remaining -= len(piece)
            if pace is not None:
                time.sleep(pace[1])
        return SimpleNamespace(status=int(status_line.split()[1]), headers=headers, body=b''.join(pieces))

    def closed(self):
        """Hat der Server die Verbindung geschlossen? Er hat bis zu ``WAIT`` Sekunden Zeit, sonst scheitert der Test."""
        return self.stream.read(1) == b''

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.stream.close()
        self.sock.close()


class BigPage:
    """Ersetzt die Seite „Über“: Der erste Aufruf liefert einen großen Text und meldet, dass der Server ihn gleich sendet; jeder weitere
    die echte Seite (so antwortet der Server danach wieder normal)."""

    def __init__(self, monkeypatch, size, varied=False):
        self.size = size
        self.text = os.urandom(size // 2 + 1).hex()[:size] if varied else 'x' * size
        self.entered = threading.Event()
        self._original = server_app.pages.PageAbout
        self._used = False
        monkeypatch.setattr(server_app.pages, 'PageAbout', self)

    def __call__(self):
        if self._used:
            return self._original()
        self._used = True
        self.entered.set()
        return self.text


def drain(sock):
    """Liest ``sock`` bis zum Ende der Verbindung leer und liefert die Zahl der Bytes (auch ein Abbruch durch den Server beendet es)."""
    received = 0
    while True:
        try:
            block = sock.recv(1024 * 1024)
        except ConnectionResetError:
            return received
        if not block:
            return received
        received += len(block)


# ── Die Zahlen und der Einbau ────────────────────────────────────────────────
def test_the_limits_are_the_documented_ones():
    assert server_app.CONNECTION_TIMEOUT == 120
    assert server_app.SEND_CHUNK == 256 * 1024
    assert server_app.SimpleWebServer.timeout == server_app.CONNECTION_TIMEOUT


def test_setup_puts_the_limit_on_the_connection_and_wraps_both_streams():
    ours, theirs = socket.socketpair()
    handler = server_app.SimpleWebServer.__new__(server_app.SimpleWebServer)
    handler.request = ours
    try:
        handler.setup()
        assert ours.gettimeout() == server_app.CONNECTION_TIMEOUT      # die Vorgabe des Servers, nicht ein Wert des Tests
        assert isinstance(handler.rfile, server_app._TimedReader)
        assert isinstance(handler.wfile, server_app._SteppedWriter)
        theirs.sendall(b'GET /about HTTP/1.1\r\n')
        assert handler.rfile.readline() == b'GET /about HTTP/1.1\r\n'
        assert handler.wfile.write(b'antwort') == 7
        assert theirs.recv(100) == b'antwort'
        handler.finish()
        assert handler.wfile.closed and handler.rfile.closed
    finally:
        ours.close()
        theirs.close()


# ── Leerlauf und Keep-Alive ──────────────────────────────────────────────────
def test_an_idle_connection_is_closed_after_the_limit(server_port, auto_login, short_limit):
    with Wire(server_port) as wire:                                # nichts senden: der Server gibt nach der Zeitgrenze auf
        assert wire.closed()
    assert get(server_port, '/about').status == 200                # und antwortet danach normal


def test_a_keep_alive_connection_is_closed_once_it_stays_unused_past_the_limit(server_port, auto_login, monkeypatch):
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', 3 * LIMIT)       # großzügig: die Anfragen folgen sofort aufeinander
    with Wire(server_port) as wire:
        for _ in range(2):
            wire.request('GET', '/about')
            assert wire.response().status == 200                   # zwei Anfragen über EINE Verbindung
        assert wire.closed()                                       # danach liegt sie brach: die Zeitgrenze schließt sie
    assert get(server_port, '/about').status == 200                # eine neue Verbindung geht


# ── Ein Body, der nicht zu Ende kommt ────────────────────────────────────────
@pytest.fixture(params=['Formular', 'JSON'])
def body_route(request, auto_login):
    """Eine Route, die ihren Body über ``_read_body`` liest: Pfad, Kopfzeilen (mit dem CSRF-Token, wo es vor dem Lesen geprüft wird)
    und der Anfang eines Bodys, der nie zu Ende kommt."""
    return {
        'Formular': SimpleNamespace(path=FORM_ROUTE, headers=FORM, as_json=False, start=b'name=Teilbeitrag&unit=St'),
        'JSON': SimpleNamespace(path=JSON_ROUTE, headers={**JSON, 'X-CSRF-Token': auto_login.csrf}, as_json=True,
                                start=b'{"invoiceId": 1, "status": '),
    }[request.param]


def test_a_body_that_never_arrives_is_408_and_closes_the_connection(server_port, short_limit, body_route):
    with Wire(server_port) as wire:
        wire.request('POST', body_route.path, body_route.headers, body_route.start, declared=1000)
        answer = wire.response()
        assert answer.status == 408 and answer.headers['connection'] == 'close'
        if body_route.as_json:
            assert json.loads(answer.body) == {'success': False, 'error': INCOMPLETE}
        else:
            assert f'<p>{INCOMPLETE}</p>' in answer.body.decode()
        assert wire.closed()
    assert get(server_port, '/about').status == 200                # der Server antwortet weiter


def test_the_login_form_gets_the_same_408(server_port, short_limit):
    installation.set_up(auto_login=False)
    with Wire(server_port) as wire:
        wire.request('POST', '/login', FORM, b'username=alice&passw', declared=200)
        answer = wire.response()
        assert answer.status == 408 and INCOMPLETE in answer.body.decode()
        assert answer.headers['connection'] == 'close'
        assert wire.closed()
    assert get(server_port, '/login').status == 200


def test_a_body_that_never_arrives_is_never_saved(server_port, auto_login, short_limit):
    """Der Anfang des Bodys ist ein gültiges Formular – gäbe der Server ihn an den Handler, entstünde ein Artikel. Derselbe Body, ganz
    gesendet, legt ihn an."""
    full = urllib.parse.urlencode({'csrf': auto_login.csrf, 'name': 'Teilbeitrag', 'unit': 'Stk.', 'unit_price': '5',
                                   'tax_rate': '19', 'active': '1'}).encode()
    with Wire(server_port) as wire:
        wire.request('POST', FORM_ROUTE, FORM, full[:len(full) // 2], declared=len(full))
        assert wire.response().status == 408
    assert auto_login.db.fetch_articles() == []
    with Wire(server_port) as wire:
        wire.request('POST', FORM_ROUTE, FORM, full)
        assert wire.response().status == 303
    assert [row[1] for row in auto_login.db.fetch_articles()] == ['Teilbeitrag']


# Routen, die den Body selbst lesen (der Handler bzw. ``wiso_upload.store``), und was der Server dort auf eine Zeitüberschreitung
# antwortet: ``None`` = nichts, die Verbindung geht zu (``handle_one_request`` meldet es im Log); 400 = ``UploadIncomplete``.
SELF_READING = [
    pytest.param('/upload_receipts', MULTIPART, None, id='upload_receipts'),
    pytest.param('/wiso/import', MULTIPART, None, id='wiso-import'),
    pytest.param('/masterdata/contacts/upload-logo', MULTIPART, None, id='upload-logo'),
    pytest.param('/wiso/fdb-upload?slot=client', RAW, 400, id='fdb-upload'),
]


@pytest.mark.parametrize('path, headers, status', SELF_READING)
def test_a_route_that_reads_its_body_itself_closes_the_connection_and_leaves_no_file(
        server_port, auto_login, short_limit, path, headers, status):
    assert get(server_port, '/about').status == 200                # zuerst alles anlegen lassen, was der Server beim Start braucht
    before = files_under(admin_dir())
    with Wire(server_port) as wire:
        wire.request('POST', path, {**headers, 'X-CSRF-Token': auto_login.csrf}, b'0123456789', declared=1000)
        answer = wire.response()
        assert (answer and answer.status) == status
        if answer:
            assert answer.headers['connection'] == 'close'
        assert wire.closed()
    assert get(server_port, '/about').status == 200                # der Server antwortet weiter
    assert files_under(admin_dir()) == before                      # keine Teildatei (bei fdb-upload auch kein ``*.part``)
    assert not [name for name in files_under(admin_dir()) if name.endswith('.part')]
    staging = os.path.join(admin_dir(), 'upload_tmp', 'wiso_fdb')
    assert not os.path.isdir(staging) or os.listdir(staging) == []     # und kein Zwischenordner des Uploads


def test_a_refused_post_whose_body_does_not_arrive_still_ends_with_the_connection_closed(server_port, auto_login, short_limit):
    """Ohne Token wird abgelehnt (403) und der Body weggelesen (``_drain_body``) – kommt er nie, geht die Verbindung nach der Antwort zu,
    statt den Rest als nächste Anfrage zu lesen."""
    with Wire(server_port) as wire:
        wire.request('POST', '/upload_receipts', MULTIPART, b'0123456789', declared=1000)
        answer = wire.response()
        assert answer.status == 403 and answer.headers['connection'] == 'close'
        assert wire.closed()
    assert get(server_port, '/about').status == 200


def test_a_handler_that_swallows_the_timeout_cannot_keep_the_connection_open(server_port, auto_login, short_limit, monkeypatch):
    """Fängt ein Handler die Zeitüberschreitung beim Lesen und antwortet trotzdem, trägt die Antwort ``Connection: close``: Der Leser ist
    nach einer Zeitüberschreitung unbrauchbar, und der halbe Body dürfte nie als nächste Anfrage gelten (``_TimedReader``)."""
    def swallowing(request_handler, db):
        try:
            request_handler.rfile.read(int(request_handler.headers['Content-Length']))
        except TimeoutError:
            pass
        return 200, json.dumps({'ok': True})

    monkeypatch.setattr(server_app.upload_handler, 'handle_file_upload', swallowing)
    with Wire(server_port) as wire:
        wire.request('POST', '/upload_receipts', {**MULTIPART, 'X-CSRF-Token': auto_login.csrf}, b'0123456789', declared=1000)
        answer = wire.response()
        assert answer.status == 200 and answer.headers['connection'] == 'close'
        assert wire.closed()
    assert get(server_port, '/about').status == 200


# ── Die Lesesperre des Datenverzeichnisses ───────────────────────────────────
def test_a_stalled_post_gives_the_directory_lock_back_when_the_connection_ends(server_port, auto_login, monkeypatch):
    """Seit 2.5.6 läuft jede Anfrage unter der Lesesperre ihres Verzeichnisses: Hinge die Verbindung ewig, fände das Wiederherstellen nie
    einen Augenblick ohne laufende Anfrage."""
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', 3 * LIMIT)       # der Test prüft die Sperre, solange die Anfrage hängt
    reading = threading.Event()
    original = server_app.SimpleWebServer._read_body

    def announcing(self, *args, **kwargs):
        reading.set()                                              # unter der Lesesperre, vor dem ersten gelesenen Byte
        return original(self, *args, **kwargs)

    monkeypatch.setattr(server_app.SimpleWebServer, '_read_body', announcing)
    with Wire(server_port) as wire:
        wire.request('POST', FORM_ROUTE, FORM, b'name=Teil', declared=1000)
        assert reading.wait(WAIT), 'die Anfrage kam nie beim Lesen des Bodys an'
        with pytest.raises(dirlock.DirBusy) as refused:            # solange sie auf den Rest wartet, hält sie die Sperre
            with dirlock.exclusive(admin_dir(), timeout=0.2):
                pytest.fail('das Wiederherstellen begann, während eine Anfrage lief')
        assert str(refused.value) == dirlock.WAIT_REFUSED
        assert wire.response().status == 408
        assert wire.closed()
    with dirlock.exclusive(admin_dir(), timeout=WAIT):             # nach dem Ende der Anfrage ist das Verzeichnis frei
        pass


# ── Antworten: der langsame und der stetige Leser ────────────────────────────
def test_a_client_that_stops_reading_is_dropped_and_the_directory_is_free_again(server_port, auto_login, short_limit, monkeypatch):
    page = BigPage(monkeypatch, 64 * 1024 * 1024)
    with Wire(server_port) as wire:
        wire.request('GET', '/about')
        assert page.entered.wait(WAIT), 'die Anfrage kam nie bei der Seite an'
        # Nichts wird abgeholt: Die Puffer laufen voll, der Server gibt nach der Zeitgrenze auf und lässt die Sperre los.
        with dirlock.exclusive(admin_dir(), timeout=WAIT):
            pass
        assert drain(wire.sock) < page.size                        # die Antwort blieb unvollständig, die Verbindung ist zu
    assert get(server_port, '/about').status == 200


@pytest.mark.parametrize('chunk', [1024, 64 * 1024, 4 * 1024 * 1024])
def test_a_client_that_keeps_reading_gets_every_byte_whatever_the_chunk_size(server_port, auto_login, monkeypatch, chunk):
    monkeypatch.setattr(server_app, 'SEND_CHUNK', chunk)
    page = BigPage(monkeypatch, 1_000_003, varied=True)           # kein Vielfaches einer der Stückgrößen
    answer = get(server_port, '/about')
    assert answer.status == 200 and len(answer.body) == page.size
    assert hashlib.sha256(answer.body).digest() == hashlib.sha256(page.text.encode('ascii')).digest()


def test_a_slow_but_steady_reader_gets_a_response_that_takes_longer_than_the_limit(server_port, auto_login, monkeypatch):
    """Die Grenze gilt je Stück, nicht für die ganze Antwort: Der Client liest langsam, aber ohne Pause über der Grenze."""
    limit = 2 * LIMIT
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', limit)
    page = BigPage(monkeypatch, 16 * 1024 * 1024)
    with Wire(server_port) as wire:
        wire.request('GET', '/about')
        answer = wire.response(pace=(64 * 1024, 0.02))
    assert answer.status == 200 and len(answer.body) == page.size


# ── Ein Upload, der stetig sendet ────────────────────────────────────────────
def test_an_upload_that_sends_in_intervals_under_the_limit_is_accepted_however_long_it_takes(server_port, auto_login, monkeypatch):
    limit = 3 * LIMIT
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', limit)
    content = os.urandom(5 * 1000)
    started = time.monotonic()
    with Wire(server_port) as wire:
        wire.request('POST', '/wiso/fdb-upload?slot=client', {**RAW, 'X-CSRF-Token': auto_login.csrf}, content[:1000],
                     declared=len(content))
        for index in range(1, 5):
            time.sleep(LIMIT)                                      # jede Pause liegt weit unter der Grenze, alle zusammen darüber
            wire.send(content[index * 1000:(index + 1) * 1000])
        answer = wire.response()
    assert time.monotonic() - started > limit
    assert answer.status == 200
    token = json.loads(answer.body)['upload']
    stored = os.path.join(admin_dir(), 'upload_tmp', 'wiso_fdb', token, 'DB1.FDB')
    with open(stored, 'rb') as handle:
        assert handle.read() == content


# ── ``_SteppedWriter`` für sich ──────────────────────────────────────────────
class RecordingRaw:
    """Ein Strom, der jeden Schreibaufruf festhält; am ``fail_at``-ten Aufruf (ab 1) scheitert er mit ``error``."""

    def __init__(self, fail_at=None, error=None):
        self.calls = 0
        self.writes = []
        self._fail_at, self._error = fail_at, error

    def write(self, data):
        self.calls += 1
        if self.calls == self._fail_at:
            raise self._error
        self.writes.append(bytes(data))
        return len(data)


@pytest.mark.parametrize('size, chunk, steps', [
    pytest.param(0, 4, [], id='leer'),
    pytest.param(3, 4, [3], id='kleiner-als-ein-Stueck'),
    pytest.param(4, 4, [4], id='genau-ein-Stueck'),
    pytest.param(8, 4, [4, 4], id='ganze-Stuecke'),
    pytest.param(9, 4, [4, 4, 1], id='mit-Rest'),
])
@pytest.mark.parametrize('kind', [bytes, bytearray, memoryview])
def test_the_writer_sends_in_steps_of_the_chunk_size(monkeypatch, size, chunk, steps, kind):
    monkeypatch.setattr(server_app, 'SEND_CHUNK', chunk)
    raw, handler = RecordingRaw(), SimpleNamespace(close_connection=False)
    data = bytes(range(size))
    assert server_app._SteppedWriter(raw, handler).write(kind(data)) == size
    assert [len(piece) for piece in raw.writes] == steps
    assert b''.join(raw.writes) == data
    assert handler.close_connection is False


@pytest.mark.parametrize('error', [TimeoutError('timed out'), BrokenPipeError(), ConnectionResetError()])
def test_after_a_failed_step_the_next_write_fails_at_once_without_touching_the_connection(monkeypatch, error):
    monkeypatch.setattr(server_app, 'SEND_CHUNK', 4)
    raw, handler = RecordingRaw(fail_at=2, error=error), SimpleNamespace(close_connection=False)
    writer = server_app._SteppedWriter(raw, handler)
    with pytest.raises(type(error)):
        writer.write(b'0123456789')                                # das erste Stück geht hinaus, am zweiten scheitert es
    assert raw.writes == [b'0123'] and handler.close_connection is True
    calls = raw.calls
    with pytest.raises(ConnectionAbortedError):
        writer.write(b'eine Fehlerantwort')
    with pytest.raises(ConnectionAbortedError):
        writer.write(b'')
    assert raw.calls == calls                                      # die Verbindung wurde nicht noch einmal angefasst


def test_the_writer_passes_everything_else_through():
    raw = io.BytesIO()
    writer = server_app._SteppedWriter(raw, SimpleNamespace(close_connection=False))
    writer.write(b'abc')
    writer.flush()
    assert not writer.closed and raw.getvalue() == b'abc'
    writer.close()
    assert writer.closed and raw.closed


def test_a_download_chunk_of_one_mebibyte_becomes_four_steps_and_copy_to_counts_every_byte():
    """Der Download einer Sicherung (``_send_backup_download``) übergibt ``backup.copy_to`` das ``write`` des Schreibers: Ein Stück von
    1 MiB wird zu vier Schritten, und ``copy_to`` zählt weiter jedes gesendete Byte."""
    raw, handler = RecordingRaw(), SimpleNamespace(close_connection=False)
    writer = server_app._SteppedWriter(raw, handler)
    content = os.urandom(2 * backup_mod.DOWNLOAD_CHUNK + 123)
    assert backup_mod.DOWNLOAD_CHUNK == 4 * server_app.SEND_CHUNK
    assert backup_mod.copy_to(io.BytesIO(content), len(content), writer.write) == len(content)
    assert [len(piece) for piece in raw.writes] == [server_app.SEND_CHUNK] * 8 + [123]
    assert b''.join(raw.writes) == content
    assert handler.close_connection is False


def test_a_real_send_timeout_ends_the_connection_and_finish_stays_quiet(monkeypatch, capsys):
    """Mit einem echten Socket, dessen Gegenstelle nichts liest: Das Senden scheitert nach der Zeitgrenze, der nächste ``write`` sofort,
    und ``finish()`` der Standardbibliothek wirft nicht und schreibt nichts ins Log."""
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', 0.3)
    ours, theirs = socket.socketpair()
    handler = server_app.SimpleWebServer.__new__(server_app.SimpleWebServer)
    handler.request = ours
    try:
        handler.setup()
        handler.close_connection = False
        with pytest.raises(TimeoutError):
            handler.wfile.write(b'x' * (64 * 1024 * 1024))         # weit mehr, als die Puffer fassen
        assert handler.close_connection is True
        with pytest.raises(ConnectionAbortedError):
            handler.wfile.write(b'noch eine Antwort')
        handler.finish()
        assert handler.wfile.closed and handler.rfile.closed
    finally:
        ours.close()
        theirs.close()
    assert capsys.readouterr().err == ''


def test_after_a_failed_send_nothing_more_is_sent_on_the_connection(server_port, auto_login, monkeypatch, capsys):
    """Im Server: Scheitert das Senden der Antwort, versucht der allgemeine Fehlerzweig eine 500 – sie erreicht die Verbindung nicht mehr
    (sonst wartete sie erneut die volle Zeitgrenze)."""
    real_write = socketserver._SocketWriter.write
    attempts = []

    def failing_write(self, data):
        attempts.append(len(data))
        raise TimeoutError('timed out')

    monkeypatch.setattr(socketserver._SocketWriter, 'write', failing_write)
    with Wire(server_port) as wire:
        wire.request('GET', '/about')
        assert wire.response() is None                             # keine Antwort: Die Verbindung geht zu
    monkeypatch.setattr(socketserver._SocketWriter, 'write', real_write)
    assert len(attempts) == 1                                      # nur der erste Versuch erreichte die Verbindung
    assert not re.search(r', in finish$', capsys.readouterr().err, re.M)         # ``finish()`` selbst warf nichts
    assert get(server_port, '/about').status == 200


# ── ``_TimedReader`` für sich ────────────────────────────────────────────────
def test_the_reader_passes_data_and_everything_else_through():
    raw = io.BufferedReader(io.BytesIO(b'erste Zeile\nrest der Daten'))
    handler = SimpleNamespace(close_connection=False)
    reader = server_app._TimedReader(raw, handler)
    assert reader.readline() == b'erste Zeile\n'
    assert reader.peek(1)[:1] == b'r'
    assert reader.read(4) == b'rest'
    target = bytearray(4)
    assert reader.readinto(target) == 4 and bytes(target) == b' der'
    assert reader.read1(100) == b' Daten'
    assert handler.close_connection is False
    assert not reader.closed
    reader.close()
    assert reader.closed and raw.closed


class DeadStream:
    """Ein Strom, auf dem jedes Lesen in die Zeitüberschreitung läuft."""

    def _dead(self, *args):
        raise TimeoutError('timed out')

    read = readline = readinto = read1 = peek = _dead


@pytest.mark.parametrize('call, args', [('read', (5,)), ('readline', ()), ('readinto', (bytearray(4),)), ('read1', (5,)),
                                        ('peek', (1,))])
def test_a_read_timeout_marks_the_connection_for_closing_and_reaches_the_caller(call, args):
    handler = SimpleNamespace(close_connection=False)
    reader = server_app._TimedReader(DeadStream(), handler)
    with pytest.raises(TimeoutError):
        getattr(reader, call)(*args)
    assert handler.close_connection is True


# ── Das Server-Log ───────────────────────────────────────────────────────────
@pytest.fixture
def log_lines(server_port, monkeypatch):
    """Was der Server protokolliert (die Testumgebung schaltet ``log_message`` sonst stumm)."""
    lines = []
    monkeypatch.setattr(server_app.SimpleWebServer, 'log_message',
                        lambda self, format, *args: lines.append(format % args))
    return lines


def test_an_idle_connection_is_closed_without_a_line_in_the_log(server_port, auto_login, short_limit, log_lines):
    """Jeder Browser lässt Keep-Alive-Verbindungen offen: Dass der Server sie nach der Zeitgrenze schließt, ist der Normalfall und
    gehört nicht ins Log – weder für eine Verbindung ohne Anfrage noch für eine, die nach ihrer letzten Anfrage brachliegt."""
    with Wire(server_port) as wire:
        assert wire.closed()
    with Wire(server_port) as wire:
        wire.request('GET', '/about')
        assert wire.response().status == 200
        assert wire.closed()
    assert any('/about' in line for line in log_lines)             # die Anfrage selbst steht da
    assert not [line for line in log_lines if 'timed out' in line]


def test_a_request_that_stalls_after_it_began_is_named_in_the_log(server_port, auto_login, short_limit, log_lines):
    """Kommt der Body einer begonnenen Anfrage nicht an und antwortet die Route darauf nicht selbst, steht eine Zeile im Log."""
    with Wire(server_port) as wire:
        wire.request('POST', '/upload_receipts', {**MULTIPART, 'X-CSRF-Token': auto_login.csrf}, b'0123456789', declared=1000)
        assert wire.closed()
    assert [line for line in log_lines if line.startswith('Request timed out')]


def test_a_client_that_does_not_fetch_a_page_costs_one_line_and_no_error_500(
        server_port, auto_login, short_limit, monkeypatch, log_lines, capsys):
    """Holt ein Client die Antwort einer Seite nicht ab, gibt es nichts mehr zu senden: kein Versuch einer Fehlerseite, kein
    Traceback – eine Zeile im Log."""
    page = BigPage(monkeypatch, 64 * 1024 * 1024)
    with Wire(server_port) as wire:
        wire.request('GET', '/about')
        assert page.entered.wait(WAIT)
        assert drain_after(wire.sock, 3 * LIMIT) < page.size       # der Server hat aufgegeben
    assert get(server_port, '/about').status == 200
    assert len([line for line in log_lines if line.startswith('Request timed out')]) == 1
    assert not [line for line in log_lines if '" 500' in line]
    assert 'Traceback' not in capsys.readouterr().err


def test_a_client_that_hangs_up_while_a_page_is_sent_costs_one_line_and_no_error_500(
        server_port, auto_login, monkeypatch, log_lines, capsys):
    page = BigPage(monkeypatch, 64 * 1024 * 1024)
    wire = Wire(server_port)
    wire.request('GET', '/about')
    assert page.entered.wait(WAIT)
    wire.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))    # hart beenden (RST)
    wire.stream.close()
    wire.sock.close()
    deadline = time.monotonic() + WAIT
    while not [line for line in log_lines if line.startswith('Antwort nicht zugestellt')]:
        assert time.monotonic() < deadline, 'der Server hat das Ende der Verbindung nie bemerkt'
        time.sleep(0.05)
    assert get(server_port, '/about').status == 200
    assert not [line for line in log_lines if '" 500' in line]
    assert 'Traceback' not in capsys.readouterr().err


def drain_after(sock, pause):
    """Erst ``pause`` Sekunden nichts abholen (die Puffer laufen voll, der Server läuft in seine Zeitgrenze), dann alles lesen."""
    time.sleep(pause)
    return drain(sock)
