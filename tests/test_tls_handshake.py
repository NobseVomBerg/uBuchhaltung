# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""TLS-Handshake je Verbindung im eigenen Thread (2.5.9): ``build_server``, ``SimpleWebServer.setup()``/``handle()`` und
``TLS_HANDSHAKE_TIMEOUT``.

Fehlfall: Mit ``UBUCHHALTUNG_CERT``/``UBUCHHALTUNG_KEY`` umhüllte ``run_server`` den lauschenden Socket mit ``wrap_socket(…, server_side=True)``.
Der TLS-Handshake lief damit in ``accept()`` – in der einen Schleife, die neue Verbindungen annimmt, und ohne Zeitgrenze. Ein Client, der nur per
TCP verbindet und nichts sendet (ein Portscan, eine Verbindung ohne TLS, ein abgebrochener Browser), hielt die Annahme ALLER weiteren
Verbindungen auf, bis er aufgab. Jetzt gilt:

* ``build_server`` legt den Server an. Sein lauschender Socket verhandelt kein TLS (``do_handshake_on_connect=False``): ``accept()`` nimmt die
  Verbindung nur an.
* Den Handshake führt ``SimpleWebServer.setup()`` aus – im Thread der Verbindung, mit der Zeitgrenze ``TLS_HANDSHAKE_TIMEOUT`` (15 s). Danach gilt
  die Zeitgrenze der Verbindung (2.5.8) wieder.
* Scheitert er (kein TLS, zu langsam, abgebrochen, Zertifikat abgelehnt), schließt der Server die Verbindung still: ``handle()`` bearbeitet nichts,
  es gibt keinen Log-Eintrag und keine Antwort.

Die Tests sprechen echtes TLS gegen einen Server aus ``build_server``. Das Zertifikat entsteht je Testlauf mit dem Programm ``openssl`` (Fixture
``certificate``; nichts davon liegt im Repository), ohne ``openssl`` werden die Tests, die es brauchen, übersprungen. Fristen sind großzügig
(``WAIT``), geprüft wird nie eine genaue Zeit; jeder Socket hat eine Zeitgrenze, damit ein Test ohne die Änderung scheitert, statt zu hängen.
"""
import contextlib
import hashlib
import http.client
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from types import SimpleNamespace

import pytest

from server import app as server_app
from tests import installation
from tests.http_client import get
from tests.test_connection_timeout import FORM, FORM_ROUTE, INCOMPLETE, LIMIT, WAIT, BigPage, Wire

SHORT_HANDSHAKE = 0.5      # ``TLS_HANDSHAKE_TIMEOUT`` in den Tests, die auf den Ablauf der Frist warten


# ── Zertifikat, Server und Verbindungen ──────────────────────────────────────
@pytest.fixture(scope='session')
def certificate(tmp_path_factory):
    """Zertifikat und Schlüssel dieses Testlaufs (selbst signiert, für localhost und 127.0.0.1, zwei Tage gültig): ``cert`` und ``key`` sind
    Dateipfade unter dem temporären Ordner des Laufs. Ohne das Programm ``openssl`` werden die Tests, die es brauchen, übersprungen."""
    openssl = shutil.which('openssl')
    if openssl is None:
        pytest.skip('openssl nicht installiert – ohne Zertifikat lässt sich kein TLS-Server aufbauen')
    folder = tmp_path_factory.mktemp('tls')
    cert, key = folder / 'cert.pem', folder / 'key.pem'
    subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '2',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   check=True, capture_output=True, timeout=60)
    return SimpleNamespace(cert=str(cert), key=str(key))


def trusting(certificate):
    """Ein Client-Kontext, der genau dem Zertifikat dieses Testlaufs vertraut (Prüfung von Kette und Hostname bleibt an)."""
    return ssl.create_default_context(cafile=certificate.cert)


def server_context(certificate):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate.cert, certificate.key)
    return context


class Finished:
    """Zählt die Verbindungen, die der Server zu Ende bearbeitet hat – nach ``shutdown_request``, also nach allem, was er dabei noch ausgibt
    (``handle_error`` schreibt vorher). So lässt sich auf „der Server blieb still“ erst prüfen, wenn er wirklich fertig ist."""

    def __init__(self):
        self._count = 0
        self._changed = threading.Condition()

    def add(self):
        with self._changed:
            self._count += 1
            self._changed.notify_all()

    def wait_for(self, count):
        with self._changed:
            return self._changed.wait_for(lambda: self._count >= count, timeout=WAIT)


@contextlib.contextmanager
def serving(httpd):
    """``httpd`` in einem Daemon-Thread bedienen (wie ``_serve``/``_stop`` in conftest.py); liefert den Port."""
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=WAIT)


@pytest.fixture
def tls_server(certificate, monkeypatch):
    """Der HTTPS-Server aus ``build_server`` (Daemon-Thread, freier Port): ``port``, ``log`` (was er protokollieren würde – die Testumgebung
    schaltet ``log_message`` sonst stumm) und ``finished`` (``Finished``). Die Installation richtet der Test ein (``auto_login``)."""
    log = []
    monkeypatch.setattr(server_app.SimpleWebServer, 'log_message', lambda self, format, *args: log.append(format % args))
    httpd = server_app.build_server('127.0.0.1', 0, certificate.cert, certificate.key)
    finished = Finished()
    original = httpd.shutdown_request

    def shutdown_request(request):
        try:
            original(request)
        finally:
            finished.add()

    httpd.shutdown_request = shutdown_request
    with serving(httpd) as port:
        yield SimpleNamespace(port=port, log=log, finished=finished)


class TlsWire(Wire):
    """Eine verschlüsselte Verbindung zum Server, geprüft gegen das Zertifikat dieses Testlaufs; sonst wie ``Wire``."""

    def __init__(self, port, certificate):
        raw = socket.create_connection(('127.0.0.1', port), timeout=WAIT)
        try:
            self.sock = trusting(certificate).wrap_socket(raw, server_hostname='localhost')
        except BaseException:
            raw.close()
            raise
        self.port = port
        self.stream = self.sock.makefile('rb')


def silent_connection(port):
    """Eine TCP-Verbindung, auf der kein einziges Byte gesendet wird (kein TLS)."""
    return contextlib.closing(socket.create_connection(('127.0.0.1', port), timeout=WAIT))


def read_to_end(sock):
    """Alles, was der Server sendet, bis er die Verbindung beendet (auch ein Abbruch durch ihn beendet es)."""
    received = b''
    while True:
        try:
            piece = sock.recv(65536)
        except ConnectionResetError:
            return received
        if not piece:
            return received
        received += piece


def available(sock):
    """Was auf ``sock`` ohne Warten zu lesen ist (ein geschlossener Socket hat nichts)."""
    received = b''
    try:
        sock.setblocking(False)
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            received += piece
    except OSError:                                                # BlockingIOError: Es ist nichts mehr da; sonst: der Socket ist zu
        pass
    return received


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.fixture
def waits_long(monkeypatch):
    """Die Frist des Handshakes und die Zeitgrenze der Verbindung liegen weit über der Wartezeit der Tests (``WAIT``): Was dann noch
    rechtzeitig endet, endet, weil der Server die Verbindung sofort aufgibt – nicht, weil eine Frist abläuft. Ein Handler, der nach einem
    gescheiterten Handshake noch eine Anfrage zu lesen versucht, hielte die Verbindung bis zum Ablauf der Frist offen."""
    monkeypatch.setattr(server_app, 'TLS_HANDSHAKE_TIMEOUT', 5 * WAIT)
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', 5 * WAIT)


# ── Die Zahlen und der Aufbau ────────────────────────────────────────────────
def test_the_limit_is_the_documented_one():
    assert server_app.TLS_HANDSHAKE_TIMEOUT == 15


def test_the_tls_server_does_not_negotiate_in_accept(certificate):
    httpd = server_app.build_server('127.0.0.1', 0, certificate.cert, certificate.key)
    try:
        assert httpd.tls is True
        assert isinstance(httpd.socket, ssl.SSLSocket)
        assert httpd.socket.do_handshake_on_connect is False       # ``accept()`` nimmt nur an, der Handshake gehört in ``setup()``
    finally:
        httpd.server_close()


@pytest.mark.parametrize('with_cert, with_key', [
    pytest.param(False, False, id='beides-fehlt'),
    pytest.param(True, False, id='ohne-Schluessel'),
    pytest.param(False, True, id='ohne-Zertifikat'),
])
def test_without_both_certificate_and_key_the_server_speaks_plain_http(certificate, auto_login, monkeypatch, with_cert, with_key):
    monkeypatch.setattr(server_app.SimpleWebServer, 'log_message', lambda *args, **kwargs: None)
    handshakes = []
    monkeypatch.setattr(ssl.SSLSocket, 'do_handshake', lambda self, *args, **kwargs: handshakes.append(self))
    httpd = server_app.build_server('127.0.0.1', 0, certificate.cert if with_cert else None, certificate.key if with_key else None)
    assert httpd.tls is False and not isinstance(httpd.socket, ssl.SSLSocket)
    with serving(httpd) as port:
        assert get(port, '/about').status == 200
    assert handshakes == []                                        # ``setup()`` versucht auf einer Klartext-Verbindung keinen Handshake


@pytest.mark.parametrize('tls', [True, False])
def test_run_server_builds_the_server_with_build_server(monkeypatch, capsys, tls):
    monkeypatch.delenv('UBUCHHALTUNG_CERT', raising=False)
    monkeypatch.delenv('UBUCHHALTUNG_KEY', raising=False)
    port = free_port()
    calls = []

    class Stopping:
        """Statt des Servers: ``serve_forever()`` kehrt wie nach Strg+C sofort zurück. Auch ein ``run_server``, das den Server selbst
        anlegt, bindet damit keinen Port und blockiert nicht – der Test scheitert dann an ``calls``, statt zu hängen."""
        tls = False
        closed = False

        def __init__(self, *args):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            self.closed = True

    built = Stopping()
    built.tls = tls

    def fake_build(host, port, certfile=None, keyfile=None):
        calls.append((host, port, certfile, keyfile))
        return built

    monkeypatch.setattr(server_app, 'build_server', fake_build)
    monkeypatch.setattr(server_app, 'ThreadingHTTPServer', Stopping)
    given = ('cert.pem', 'key.pem') if tls else (None, None)
    server_app.run_server(host='127.0.0.1', port=port, certfile=given[0], keyfile=given[1])
    assert calls == [('127.0.0.1', port, given[0], given[1])]
    assert f"Starting server on {'https' if tls else 'http'}://127.0.0.1:{port} ..." in capsys.readouterr().out
    assert built.closed


# ── ``setup()`` und ``handle()`` für sich (ein Socketpaar statt des Netzes) ──
def tls_handler(certificate, connection):
    """Ein ``SimpleWebServer`` für eine Verbindung, wie ``accept()`` sie dem Server übergibt: serverseitig TLS, der Handshake ist noch nicht gelaufen."""
    handler = server_app.SimpleWebServer.__new__(server_app.SimpleWebServer)
    handler.request = server_context(certificate).wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
    return handler


def test_setup_runs_the_handshake_with_its_own_limit_and_then_puts_the_limit_of_the_connection_back(certificate, monkeypatch):
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', 7.5)
    monkeypatch.setattr(server_app, 'TLS_HANDSHAKE_TIMEOUT', 11.5)
    limits = []
    original = ssl.SSLSocket.do_handshake

    def recording(self, *args, **kwargs):
        if self.server_side:
            limits.append(self.gettimeout())                       # die Zeitgrenze, unter der der Handshake läuft
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLSocket, 'do_handshake', recording)
    ours, theirs = socket.socketpair()
    theirs.settimeout(WAIT)
    handler = tls_handler(certificate, ours)
    peer = {}

    def connect():
        try:
            peer['sock'] = trusting(certificate).wrap_socket(theirs, server_hostname='localhost')
        except BaseException as error:
            peer['error'] = error

    client = threading.Thread(target=connect, daemon=True)
    client.start()
    try:
        handler.setup()
        client.join(WAIT)
        assert 'error' not in peer and 'sock' in peer
        assert handler._tls_failed is False
        assert limits == [11.5]
        assert handler.connection.version() is not None            # der Handshake ist durch
        assert handler.connection.gettimeout() == 7.5              # danach gilt die Zeitgrenze der Verbindung
        assert isinstance(handler.rfile, server_app._TimedReader) and isinstance(handler.wfile, server_app._SteppedWriter)
        handler.wfile.write(b'antwort')                            # und die Verbindung trägt Daten
        assert peer['sock'].recv(100) == b'antwort'
        handler.finish()
    finally:
        handler.request.close()
        if 'sock' in peer:
            peer['sock'].close()


@pytest.mark.parametrize('peer_does', ['clear text', 'silence', 'hang up'])
def test_a_failed_handshake_ends_setup_quietly_and_nothing_is_handled_or_sent(certificate, monkeypatch, peer_does):
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', LIMIT)
    monkeypatch.setattr(server_app, 'TLS_HANDSHAKE_TIMEOUT', 0.2)
    ours, theirs = socket.socketpair()
    handler = tls_handler(certificate, ours)
    try:
        if peer_does == 'clear text':
            theirs.sendall(b'GET /about HTTP/1.1\r\nHost: localhost\r\n\r\n')      # Klartext statt TLS
        elif peer_does == 'hang up':
            theirs.close()
        handler.setup()                                            # wirft nicht: kein TLS, zu langsam, abgebrochen
        assert handler._tls_failed is True
        handler.rfile.readline = lambda *args: pytest.fail('handle() liest auf einer Verbindung ohne TLS eine Anfrage')
        handler.handle()                                           # bearbeitet nichts: Es wird keine Anfrage gelesen
        answered = available(theirs)                               # was der gescheiterte Handshake selbst gesendet hat (ein TLS-Alarm)
        assert b'HTTP/' not in answered
        handler.finish()                                           # wirft nicht und sendet nichts mehr
        assert handler.wfile.closed and handler.rfile.closed
        assert available(theirs) == b''
    finally:
        handler.request.close()
        theirs.close()


def test_a_plain_connection_gets_no_handshake(monkeypatch):
    monkeypatch.setattr(ssl.SSLSocket, 'do_handshake', lambda *args, **kwargs: pytest.fail('Handshake auf einer Klartext-Verbindung'))
    ours, theirs = socket.socketpair()
    handler = server_app.SimpleWebServer.__new__(server_app.SimpleWebServer)
    handler.request = ours
    try:
        handler.setup()
        assert handler._tls_failed is False
        handler.finish()
    finally:
        ours.close()
        theirs.close()


# ── HTTPS gegen den echten Server ────────────────────────────────────────────
def test_https_works_and_a_connection_carries_more_than_one_request(tls_server, auto_login, certificate):
    with TlsWire(tls_server.port, certificate) as wire:
        for _ in range(2):                                         # Keep-Alive: zwei Anfragen über EINE Verbindung
            wire.request('GET', '/about')
            assert wire.response().status == 200


@pytest.mark.parametrize('silent', [1, 10])
def test_clients_that_only_connect_do_not_hold_up_the_next_connection(tls_server, auto_login, certificate, silent):
    """Der Fehlfall: Die stummen Verbindungen kamen zuerst, der Server nimmt sie an und wartet je Verbindung im eigenen Thread auf ihr TLS.
    Der Client danach bekommt seine Antwort sofort – früher hing sein Handshake, bis die stummen aufgaben."""
    with contextlib.ExitStack() as stack:
        for _ in range(silent):
            stack.enter_context(silent_connection(tls_server.port))
        with TlsWire(tls_server.port, certificate) as wire:
            for _ in range(2):
                wire.request('GET', '/about')
                assert wire.response().status == 200


def test_a_silent_client_is_closed_after_the_handshake_limit_without_a_line_in_the_log(tls_server, auto_login, monkeypatch, capsys):
    monkeypatch.setattr(server_app, 'TLS_HANDSHAKE_TIMEOUT', SHORT_HANDSHAKE)
    with silent_connection(tls_server.port) as silent:
        assert silent.recv(1) == b''                               # der Server schließt, ohne etwas zu senden
    assert tls_server.finished.wait_for(1)
    assert tls_server.log == [] and capsys.readouterr().err == ''


def test_clear_text_on_the_tls_port_is_closed_without_an_answer_and_the_server_stays_quiet(tls_server, auto_login, certificate, capsys,
                                                                                         waits_long):
    with silent_connection(tls_server.port) as plain:
        plain.sendall(f'GET /about HTTP/1.1\r\nHost: 127.0.0.1:{tls_server.port}\r\nConnection: close\r\n\r\n'.encode('ascii'))
        received = read_to_end(plain)
    assert b'HTTP/' not in received                                # keine Antwort der App im Klartext
    assert tls_server.finished.wait_for(1)
    assert tls_server.log == [] and capsys.readouterr().err == ''  # weder Log-Zeile noch Traceback
    with TlsWire(tls_server.port, certificate) as wire:            # der Server antwortet danach normal
        wire.request('GET', '/about')
        assert wire.response().status == 200


def test_a_client_that_rejects_the_certificate_leaves_the_server_quiet(tls_server, auto_login, certificate, capsys, waits_long):
    with silent_connection(tls_server.port) as raw:
        with pytest.raises(ssl.SSLCertVerificationError):
            ssl.create_default_context().wrap_socket(raw, server_hostname='localhost')     # kennt das Zertifikat dieses Laufs nicht
    assert tls_server.finished.wait_for(1)
    assert tls_server.log == [] and capsys.readouterr().err == ''
    with TlsWire(tls_server.port, certificate) as wire:
        wire.request('GET', '/about')
        assert wire.response().status == 200


def test_the_limit_of_the_connection_applies_once_the_handshake_is_done(tls_server, auto_login, certificate, monkeypatch):
    """Die Zeitgrenze der Verbindung (2.5.8) gilt auch über TLS: Eine Verbindung im Leerlauf wird geschlossen, ein Body, der nicht zu Ende
    kommt, ist 408. Die Frist des Handshakes liegt hier weit über der Wartezeit des Clients – nur die Grenze der Verbindung kann ihn schließen."""
    monkeypatch.setattr(server_app.SimpleWebServer, 'timeout', LIMIT)
    monkeypatch.setattr(server_app, 'TLS_HANDSHAKE_TIMEOUT', 5 * WAIT)
    with TlsWire(tls_server.port, certificate) as wire:            # TLS steht, es kommt keine Anfrage
        assert wire.closed()
    with TlsWire(tls_server.port, certificate) as wire:
        wire.request('POST', FORM_ROUTE, FORM, b'name=Teilbeitrag&unit=St', declared=1000)
        answer = wire.response()
        assert answer.status == 408 and answer.headers['connection'] == 'close'
        assert f'<p>{INCOMPLETE}</p>' in answer.body.decode()
        assert wire.closed()
    with TlsWire(tls_server.port, certificate) as wire:            # und der Server antwortet weiter
        wire.request('GET', '/about')
        assert wire.response().status == 200


def test_a_large_response_arrives_whole_over_tls(tls_server, auto_login, certificate, monkeypatch):
    """Die Stückelung (``_SteppedWriter``) arbeitet auch mit ``SSLSocket``: Jedes Byte kommt an, auch bei vielen Stücken."""
    monkeypatch.setattr(server_app, 'SEND_CHUNK', 64 * 1024)
    page = BigPage(monkeypatch, 3_000_017, varied=True)            # kein Vielfaches der Stückgröße
    with TlsWire(tls_server.port, certificate) as wire:
        wire.request('GET', '/about')
        answer = wire.response()
    assert answer.status == 200 and len(answer.body) == page.size
    assert hashlib.sha256(answer.body).digest() == hashlib.sha256(page.text.encode('ascii')).digest()


def test_the_session_cookie_is_secure_on_the_tls_server(tls_server, certificate):
    """Der Server aus ``build_server`` gilt als HTTPS-Server (``httpd.tls``): Das Sitzungs-Cookie trägt ``Secure`` (der Proxy-Weg:
    test_secure_cookie_proxy.py)."""
    installation.set_up(auto_login=False)
    connection = http.client.HTTPSConnection('127.0.0.1', tls_server.port, timeout=WAIT, context=trusting(certificate))
    try:
        form = urllib.parse.urlencode({'username': installation.ADMIN, 'password': installation.PASSWORD})
        connection.request('POST', '/login', body=form, headers={'Content-Type': 'application/x-www-form-urlencoded',
                                                                 'Origin': f'https://127.0.0.1:{tls_server.port}'})
        response = connection.getresponse()
        response.read()
        cookie = response.getheader('Set-Cookie')
    finally:
        connection.close()
    assert response.status == 303 and cookie.startswith('session=')
    assert {'HttpOnly', 'SameSite=Strict', 'Path=/', 'Secure'} <= set(cookie.split('; ')[1:])


# ── Viele Verbindungen auf einmal ────────────────────────────────────────────
def test_the_server_takes_a_burst_of_connections_and_drops_every_silent_one(tls_server, monkeypatch):
    """Zwanzig Verbindungen im selben Augenblick, die nur verbinden und schweigen: Der Server nimmt jede an (die Warteschlange des
    Betriebssystems fasst ``ACCEPT_QUEUE``, nicht nur die 5 der Bibliothek) und schließt jede nach der Handshake-Frist."""
    monkeypatch.setattr(server_app, 'TLS_HANDSHAKE_TIMEOUT', 1.0)
    assert server_app.ACCEPT_QUEUE == 128
    sockets = []
    try:
        for _ in range(20):
            sock = socket.socket()
            sock.setblocking(False)
            with contextlib.suppress(BlockingIOError):
                sock.connect(('127.0.0.1', tls_server.port))
            sockets.append(sock)
        open_ones = list(sockets)
        deadline = time.monotonic() + WAIT
        while open_ones and time.monotonic() < deadline:
            for sock in list(open_ones):
                try:
                    if sock.recv(1) == b'':
                        open_ones.remove(sock)
                except BlockingIOError:
                    pass
                except OSError:
                    open_ones.remove(sock)
            time.sleep(0.05)
        assert not open_ones, f'{len(open_ones)} von 20 Verbindungen hat der Server nie geschlossen'
    finally:
        for sock in sockets:
            sock.close()


def test_the_plain_server_has_the_same_queue(monkeypatch):
    httpd = server_app.build_server('127.0.0.1', 0)
    try:
        assert httpd.request_queue_size == server_app.ACCEPT_QUEUE and httpd.tls is False
    finally:
        httpd.server_close()
