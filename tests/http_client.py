# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kleiner HTTP-Client für Tests gegen den echten Server (Fixture ``server_port``).

Host, Origin, Cookie und CSRF-Token lassen sich frei setzen. ``get``/``post`` schließen
die Verbindung nach jeder Anfrage; ``KeepAlive`` schickt mehrere Anfragen über EINE
Verbindung. Hostnamen (*.example) und Zugangsdaten in den Tests sind erfundene Werte.
"""
import http.client
import urllib.parse

TIMEOUT = 10        # Sekunden; ein hängender Handler soll den Test scheitern lassen
FOREIGN_ORIGIN = "http://evil.example"


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


def _encode(form):
    return None if form is None else urllib.parse.urlencode(form)


def send(port, method, path, headers=None, body=None):
    """Eine Anfrage an 127.0.0.1:<port>; die Verbindung wird immer geschlossen."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
    try:
        conn.request(method, path, body=body,
                     headers={"Connection": "close", **(headers or {})})
        resp = conn.getresponse()
        return Response(resp.status, resp.headers, resp.read())
    finally:
        conn.close()


def get(port, path, headers=None):
    return send(port, "GET", path, headers)


def post(port, path, headers=None, form=None, csrf=None):
    """POST als Formular; ``csrf`` wird als Feld ``csrf`` mitgeschickt (wie ``appPost``)."""
    if csrf is not None:
        form = {**(form or {}), "csrf": csrf}
    hdrs = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    return send(port, "POST", path, hdrs, _encode(form or {}))


def own_origin(port):
    return f"http://127.0.0.1:{port}"


class KeepAlive:
    """Mehrere Anfragen über EINE TCP-Verbindung (HTTP/1.1 Keep-Alive).

    ``http.client`` öffnet bei Bedarf stillschweigend eine neue Verbindung; ``request``
    prüft deshalb mit, dass der Server die Verbindung offen gehalten hat und der Socket
    derselbe geblieben ist – sonst wäre ein Keep-Alive-Test wirkungslos.
    """

    def __init__(self, port):
        self._conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
        self._sock = None

    def request(self, method, path, headers=None, form=None):
        hdrs = dict(headers or {})
        if form is not None:
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        self._conn.request(method, path, body=_encode(form), headers=hdrs)
        resp = self._conn.getresponse()
        body = resp.read()
        assert not resp.will_close, "der Server hat die Verbindung nicht offen gehalten"
        if self._sock is None:
            self._sock = self._conn.sock
        assert self._conn.sock is self._sock, "die Anfrage lief über eine neue Verbindung"
        return Response(resp.status, resp.headers, body)

    def get(self, path, headers=None):
        return self.request("GET", path, headers)

    def post(self, path, headers=None, form=None, csrf=None):
        if csrf is not None:
            form = {**(form or {}), "csrf": csrf}
        return self.request("POST", path, headers, form or {})

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
