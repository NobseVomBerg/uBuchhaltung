# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Body einer Anfrage gilt nie als nächste Anfrage (2.5.8, ``SimpleWebServer._settle_body``).

Fehlfall: Antwortete ein Zweig, ohne den erklärten Body zu lesen – eine GET-Anfrage mit ``Content-Length``, eine frühe Ablehnung
(``/upload_receipts`` oder ``/wiso/import`` ohne Multipart) –, blieb der Body auf der Keep-Alive-Verbindung liegen, und der Server
las ihn als nächste Anfrage. Jetzt schließt der Server jede Anfrage ab: Ein kleiner Rest wird weggelesen (die Verbindung bleibt
benutzbar), in jedem anderen Fall geht sie nach der Antwort zu.

Geprüft wird am Draht: Der Body trägt eine vollständige zweite Anfrage. Sie darf nie beantwortet werden und nie im Protokoll
des Servers stehen.
"""
import socket

import pytest

from server import app as server_app
from tests.http_client import own_origin

WAIT = 20                                   # Sekunden, die ein Test auf den Server wartet, bevor er scheitert
HIDDEN = '/masterdata/articles'             # die Adresse der Anfrage, die im Body steckt


@pytest.fixture
def log_lines(server_port, monkeypatch):
    """Was der Server protokolliert (jede beantwortete Anfrage steht dort mit ihrer Adresse)."""
    lines = []
    monkeypatch.setattr(server_app.SimpleWebServer, 'log_message',
                        lambda self, format, *args: lines.append(format % args))
    return lines


def hidden_request(port):
    return f'GET {HIDDEN} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode('ascii')


def head(port, method, path, extra):
    lines = [f'{method} {path} HTTP/1.1', f'Host: 127.0.0.1:{port}', f'Origin: {own_origin(port)}', *extra]
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('ascii')


def statuses(port, raw, follow_up=True):
    """``raw`` schicken, danach (``follow_up``) eine gewöhnliche letzte Anfrage mit ``Connection: close``, und alles lesen, bis der
    Server die Verbindung schließt: die Statuscodes aller Antworten in ihrer Reihenfolge. Hat der Server die Verbindung schon
    geschlossen, scheitert das Nachschicken still."""
    with socket.create_connection(('127.0.0.1', port), timeout=WAIT) as sock:
        sock.sendall(raw)
        received = b''
        if follow_up:
            # erst die Antwort auf ``raw`` abwarten, damit die letzte Anfrage nicht selbst als Rest eines Bodys gelesen wird
            while b'\r\n\r\n' not in received:
                block = sock.recv(65536)
                if not block:
                    break
                received += block
            try:
                sock.sendall(head(port, 'GET', '/buch.css', ['Connection: close']))
            except OSError:
                pass
        while True:
            try:
                block = sock.recv(65536)
            except ConnectionResetError:
                break
            if not block:
                break
            received += block
    return response_statuses(received)


def response_statuses(received):
    """Die Statuscodes der Antworten in ``received`` – Antwort für Antwort gelesen (Kopf, dann ``Content-Length`` Bytes Body), nicht
    nach Zeilen gesucht: Ein Body endet nicht mit einem Zeilenumbruch."""
    found = []
    while received:
        assert received.startswith(b'HTTP/1.1 '), received[:80]
        top, separator, rest = received.partition(b'\r\n\r\n')
        assert separator, 'unvollständiger Antwortkopf'
        lines = top.split(b'\r\n')
        found.append(int(lines[0].split()[1]))
        length = next((int(line.split(b':', 1)[1]) for line in lines[1:] if line.lower().startswith(b'content-length:')), 0)
        received = rest[length:]
    return found


def never_served(log_lines):
    return not [line for line in log_lines if HIDDEN in line]


@pytest.mark.parametrize('path', ['/about', '/buch.css', '/miscellaneous'])
def test_the_body_of_a_get_request_is_not_served_as_the_next_request(server_port, auto_login, log_lines, path):
    inner = hidden_request(server_port)
    raw = head(server_port, 'GET', path, [f'Content-Length: {len(inner)}']) + inner
    assert statuses(server_port, raw) == [200, 200]                 # die Seite und die letzte Anfrage – keine dritte Antwort
    assert never_served(log_lines)


@pytest.mark.parametrize('path, status', [('/upload_receipts', 400), ('/wiso/import', 303)])
def test_an_early_refusal_that_leaves_the_body_unread_does_not_serve_it(server_port, auto_login, log_lines, path, status):
    inner = hidden_request(server_port)
    raw = head(server_port, 'POST', path, ['Content-Type: text/plain', f'X-CSRF-Token: {auto_login.csrf}',
                                           f'Content-Length: {len(inner)}']) + inner
    assert statuses(server_port, raw) == [status, 200]
    assert never_served(log_lines)


def test_a_request_with_transfer_encoding_closes_the_connection_after_the_answer(server_port, auto_login, log_lines):
    inner = hidden_request(server_port)
    raw = head(server_port, 'GET', '/about', ['Transfer-Encoding: chunked']) + inner
    assert statuses(server_port, raw) == [200]                      # danach ist die Verbindung zu: auch die letzte Anfrage bleibt aus
    assert never_served(log_lines)


def test_an_unread_rest_beyond_the_drain_limit_closes_the_connection(server_port, auto_login, log_lines, monkeypatch):
    monkeypatch.setattr(server_app, 'MAX_DRAIN_BODY', 10)
    inner = hidden_request(server_port)
    raw = head(server_port, 'GET', '/about', [f'Content-Length: {len(inner)}']) + inner
    assert statuses(server_port, raw) == [200]
    assert never_served(log_lines)


def test_a_declared_body_that_never_comes_gets_its_answer_and_then_the_connection_closes(
        server_port, auto_login, log_lines, monkeypatch):
    """Die Antwort wartet nicht auf den Rest; danach wartet der Server kurz (``DRAIN_TIMEOUT``) und schließt – ohne Zeile im Log."""
    monkeypatch.setattr(server_app, 'DRAIN_TIMEOUT', 0.3)
    raw = head(server_port, 'GET', '/about', ['Content-Length: 50'])
    assert statuses(server_port, raw, follow_up=False) == [200]     # gelesen bis zum Ende: Der Server hat geschlossen
    assert not [line for line in log_lines if 'timed out' in line]


def test_a_body_that_was_read_leaves_the_connection_open(server_port, auto_login, log_lines):
    """Der gewöhnliche Fall bleibt: Nach einem gelesenen Formular geht die nächste Anfrage über dieselbe Verbindung."""
    body = b'scope=db'
    raw = head(server_port, 'POST', '/backup/create', ['Content-Type: application/x-www-form-urlencoded',
                                                      f'Content-Length: {len(body)}']) + body
    assert statuses(server_port, raw) == [403, 200]                 # ohne Token abgelehnt – gelesen wurde der Body trotzdem
    assert never_served(log_lines)


def test_the_reader_counts_what_was_consumed_and_not_what_was_only_looked_at():
    import io
    handler = type('Handler', (), {'close_connection': False})()
    reader = server_app._TimedReader(io.BufferedReader(io.BytesIO(b'erste\nzweite\ndritte Zeile und mehr')), handler)
    assert reader.readline() == b'erste\n' and reader.count == 6
    assert reader.peek(3)[:3] == b'zwe' and reader.count == 6       # nur angesehen
    assert reader.read(7) == b'zweite\n' and reader.count == 13
    target = bytearray(6)
    assert reader.readinto(target) == 6 and reader.count == 19
    reader.count = 0                                                # so setzt der Server es nach den Kopfzeilen zurück
    assert reader.read() == b' Zeile und mehr' and reader.count == 15
    assert reader.read(5) == b'' and reader.count == 15
