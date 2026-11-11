# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
r"""Diagnose-MCP-Server für uBuchhaltung – nur lesend, nur Standardbibliothek.

Claude Code startet dieses Skript als stdio-MCP-Server und kann damit eine laufende
Instanz (z. B. einen Docker-Container im LAN) untersuchen, ohne Browser. Der Server
spricht die Instanz per HTTP an wie ein Skript: Anmeldung über ``/login``, CSRF-Token aus
dem ``<meta>``-Tag der Seiten, SQL über die vorhandene SQL-Konsole (``/execute_sql``),
Seiten per GET.

Was gesendet wird (und sonst nichts):

* SQL: genau eine SELECT-Abfrage ohne ``;`` (``check_select``) an ``POST /execute_sql``.
  INSERT, UPDATE, DELETE, WITH, PRAGMA, ATTACH und mehrere Anweisungen lehnt der Server
  ab, bevor etwas gesendet wird. ``pragma_<name>`` als Tabellenfunktion ist nur für die
  rein lesenden Namen in ``_READ_PRAGMAS`` erlaubt (``pragma_optimize`` würde schreiben).
  Weil die Konsole kein Zeitlimit hat, sind WITH/RECURSIVE, zeroblob, randomblob und
  Abfragen über 20 000 Zeichen gesperrt (Heuristik, keine vollständige Last-Sperre).
* Seiten: ``GET``, und nur aus ``_PAGE_ALLOWLIST`` (``check_page_path``), einer Liste
  geprüfter Anzeigeseiten mit den je Seite erlaubten Query-Parametern. Aktionen,
  PDF-Erzeugung, ``/login`` und ``/logout`` stehen nicht darin.
* ``POST /login`` für die Anmeldung. Weitere POSTs gibt es nicht. Zugangsdaten gehen über
  ``http://`` nur an den eigenen Rechner, sonst nur mit ``UBUCHHALTUNG_MCP_ALLOW_HTTP``.

Das Passwort steht nie in Logs, Fehlermeldungen oder Werkzeugausgaben. stdout gehört
allein dem MCP-Protokoll, Logs gehen nach stderr. Antworten der Instanz werden auf 16 MiB
und die doppelte Zeitgrenze je Anfrage begrenzt.

Werkzeuge: ``info``, ``sql``, ``db_check``, ``schema``, ``page``.

Umgebungsvariablen (werden erst beim ersten Werkzeugaufruf gelesen):

* ``UBUCHHALTUNG_MCP_URL``         Adresse der Instanz, z. B. ``https://nas.local:2824`` (Pflicht)
* ``UBUCHHALTUNG_MCP_CREDENTIAL``  Zielname in der Windows-Anmeldeinformationsverwaltung
                                  bzw. keyring-Dienst (Standard ``uBuchhaltung-MCP``)
* ``UBUCHHALTUNG_MCP_USER``        Benutzername; überschreibt den aus dem Windows-Eintrag
* ``UBUCHHALTUNG_MCP_PASSWORD``    Passwort im Klartext (Notlösung, nur zusammen mit USER)
* ``UBUCHHALTUNG_MCP_TIMEOUT``     Zeitlimit je Anfrage in Sekunden (Standard 30)
* ``UBUCHHALTUNG_MCP_CAFILE``      CA-Datei für HTTPS mit eigenem Zertifikat
* ``UBUCHHALTUNG_MCP_ALLOW_HTTP``  ``1``/``true``/``yes``/``ja``: Zugangsdaten bewusst über
                                  unverschlüsseltes ``http://`` an einen fremden Rechner senden

Zugangsdaten werden in dieser Reihenfolge gesucht: ``..._PASSWORD`` (zusammen mit
``..._USER``), Windows-Anmeldeinformationsverwaltung, keyring (mit ``..._USER``). Ohne
Zugangsdaten reicht es bei automatischer Anmeldung der Instanz; verlangt sie eine
Anmeldung, meldet das Werkzeug einen Fehler mit Einrichtungshinweis.

Einrichtung (Windows, PowerShell):

1. Zugangsdaten ablegen (fragt das Passwort ab; es liegt dann verschlüsselt in der Windows-Anmeldeinformationsverwaltung)::

       cmdkey /generic:uBuchhaltung-MCP /user:<Benutzer> /pass

   Das Konto braucht Administratorrechte (SQL-Konsole).

2. Server nur lokal registrieren – die Adresse bleibt so außerhalb des Repos. Besser ist
   HTTPS (``https://…`` und bei eigenem Zertifikat ``--env UBUCHHALTUNG_MCP_CAFILE=<Pfad>``).
   Im reinen LAN ohne HTTPS braucht es die bewusste Ausnahme ``UBUCHHALTUNG_MCP_ALLOW_HTTP=1``::

       claude mcp add --scope local ubuchhaltung --env UBUCHHALTUNG_MCP_URL=http://nas.local:2824 --env UBUCHHALTUNG_MCP_ALLOW_HTTP=1 -- python C:\Pfad\zu\uBuchhaltung\tools\mcp_ubuchhaltung.py

3. Optional ohne Nachfrage freigeben, in ``.claude/settings.local.json``. Alle Werkzeuge::

       {"permissions": {"allow": ["mcp__ubuchhaltung"]}}

   oder einzeln, z. B. ``["mcp__ubuchhaltung__info", "mcp__ubuchhaltung__schema", "mcp__ubuchhaltung__db_check"]``.

Ausführlich: DEPLOYMENT.md, Abschnitt „Diagnose mit Claude Code (MCP, nur lesend)“.
"""
import http.client
import ipaddress
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

SERVER_NAME = "ubuchhaltung"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
USER_AGENT = "uBuchhaltung-MCP/1.0"
DEFAULT_CREDENTIAL = "uBuchhaltung-MCP"
DEFAULT_TIMEOUT = 30.0
MAX_TEXT = 50_000
MAX_BODY = 16 * 1024 * 1024        # Antworten der Instanz werden nach 16 MiB abgebrochen
MAX_QUERY_CHARS = 20_000
_CHUNK = 64 * 1024
INSTRUCTIONS = ("Nur lesender Zugriff auf eine uBuchhaltung-Instanz: SQL ausschließlich als "
                "einzelnes SELECT (nur freigegebene pragma_-Funktionen), Seiten nur aus einer Liste "
                "geprüfter Anzeigeseiten mit freigegebenen Query-Parametern (GET).")

_SETUP_HINT = (
    "Die Instanz verlangt eine Anmeldung, es sind aber keine Zugangsdaten hinterlegt. "
    "Einrichtung unter Windows: cmdkey /generic:uBuchhaltung-MCP /user:<Benutzer> /pass "
    "(fragt das Passwort ab). Andere Systeme: UBUCHHALTUNG_MCP_USER setzen und das Passwort "
    "mit keyring ablegen; als Notlösung UBUCHHALTUNG_MCP_PASSWORD.")
_SESSION_REFUSED = "Die Instanz nimmt die Sitzung nicht an (Weiterleitung zur Anmeldeseite)."
_PLAIN_HTTP_REFUSED = (
    "Zugangsdaten werden über http nicht gesendet – HTTPS mit UBUCHHALTUNG_MCP_CAFILE nutzen "
    "oder bewusst UBUCHHALTUNG_MCP_ALLOW_HTTP=1 setzen")
_NO_TIME_LIMIT = "aus Lastgründen gesperrt (die SQL-Konsole der Instanz hat kein Zeitlimit)"
_URL_SAFE = "/?&=%+#:@!$'()*,;[]"
_CSRF_RE = re.compile(r"""<meta\s+name=['"]csrf-token['"]\s+content=['"]([^'"]*)['"]""",
                      re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _log(message):
    print(f"ubuchhaltung-mcp: {message}", file=sys.stderr, flush=True)


def clip(text, limit=MAX_TEXT):
    """Text auf ``limit`` Zeichen kürzen, mit Hinweis auf die Gesamtlänge."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… gekürzt ({len(text)} Zeichen insgesamt)"


# ── Lesewächter ──────────────────────────────────────────────────────────────
class ReadOnlyViolation(ValueError):
    """Die Abfrage würde die Lesegarantie verletzen."""


class QueryError(Exception):
    """Die SQL-Konsole der Instanz meldet einen Fehler."""


# pragma_<name> als Tabellenfunktion führt das PRAGMA aus. Der Authorizer der Konsole sperrt nur Pragmas
# MIT Argument; pragma_optimize (ANALYZE, legt sqlite_stat1 an) liefe also durch und würde festgeschrieben.
_READ_PRAGMAS = frozenset({
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
    "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check", "user_version",
    "journal_mode", "page_count", "page_size", "freelist_count", "compile_options", "database_list",
})
_PRAGMA_FUNCTION = re.compile(r"pragma_(\w+)", re.IGNORECASE)
# Die Konsole hat kein Zeitlimit: Eine Endlosabfrage hielte die Datenbank für alle gesperrt. SQLite
# rekursiert auch ohne das Wort RECURSIVE, und Funktionsnamen lassen sich per Kommentar oder
# Anführungszeichen von der Klammer trennen – deshalb zählt schon das Wort. Das ist eine Heuristik:
# Kreuzprodukte über große Tabellen fängt sie nicht.
_LOAD_WORD = re.compile(r"\b(?:with|recursive|zeroblob|randomblob)\b", re.IGNORECASE)


def check_select(query):
    """Genau ein SELECT ohne ``;`` durchlassen und es ohne führende Kommentare zurückgeben.

    Die Konsole der Instanz teilt naiv am ``;`` und führt auch Schreibbefehle aus; sie
    erkennt Ergebnisse nur an einem Befehl, der mit SELECT beginnt. Ein ``;`` wird daher
    überall abgelehnt, auch in Zeichenketten und Kommentaren. Weitere Sperren gelten dem
    Text insgesamt (auch Kommentaren und Zeichenketten): nicht freigegebene ``pragma_``-
    Funktionen und Wörter, die Last erzeugen.
    """
    if not isinstance(query, str):
        raise ReadOnlyViolation("Die Abfrage muss ein Text sein.")
    if "\x00" in query:
        raise ReadOnlyViolation("Die Abfrage enthält ein Nullzeichen.")
    if len(query) > MAX_QUERY_CHARS:
        raise ReadOnlyViolation(f"Abfragen über {MAX_QUERY_CHARS} Zeichen sind {_NO_TIME_LIMIT}.")
    s = query.strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if ";" in s:
        raise ReadOnlyViolation(
            "Nur eine Anweisung – ';' ist nicht erlaubt (auch nicht in Zeichenketten oder Kommentaren).")
    while True:
        if s.startswith("--"):
            s = s.partition("\n")[2].lstrip()
        elif s.startswith("/*"):
            end = s.find("*/", 2)
            if end < 0:
                raise ReadOnlyViolation("Nicht geschlossener Kommentar (/* ohne */).")
            s = s[end + 2:].lstrip()
        else:
            break
    if "/*" in s:
        # Ein offener Kommentar am Ende verschluckte die Klammer und das LIMIT der Umhüllung.
        raise ReadOnlyViolation("Blockkommentare nur am Anfang erlaubt.")
    if not re.match(r"(?i)select\b", s):
        raise ReadOnlyViolation("Nur SELECT-Abfragen sind erlaubt.")
    for name in _PRAGMA_FUNCTION.findall(query):
        if name.lower() not in _READ_PRAGMAS:
            raise ReadOnlyViolation(f"PRAGMA-Funktion nicht freigegeben: pragma_{name.lower()}")
    word = _LOAD_WORD.search(query)
    if word:
        raise ReadOnlyViolation(f"'{word.group(0).lower()}' ist {_NO_TIME_LIMIT}.")
    return s


_PERIOD = frozenset({"from", "to", "date_from", "date_to"})


def _keys(*names, period=False):
    return frozenset(names) | (_PERIOD if period else frozenset())


# Einige GET-Routen der App ändern Daten oder erzeugen und speichern PDFs (z. B. /quote/delete,
# /invoice/pdf_generate, /quote/pdf_generate, /quote/pdf_download, /worktime/pdf). Deshalb eine
# Positivliste; neue Einträge nur für GET-Handler, die nachweislich nichts schreiben.
# Auch die Query-Schlüssel gehören zur Prüfung, weil manche Seiten je nach Parameter schreiben
# (Beispiel: /transactions?from_invoice=… legt über die Erlöskonto-Auflösung ein Konto an). Neue
# Schlüssel nur mit Nachweis. /invoice/xml_download fehlt bewusst: Es liest jeden Pfad, der in
# Invoices.XMLPath steht, ohne ihn zu prüfen.
_PAGE_ALLOWLIST = {
    "/": _keys("acct", period=True),
    "/dashboard": _keys("acct", period=True),
    "/about": _keys(),
    "/invoice": _keys("search", "status", "id", period=True),
    "/invoice/view": _keys("id"),
    "/invoice/edit": _keys("id"),
    "/invoice/reminders": _keys(),
    "/quote": _keys("search", "status", "id"),
    "/quote/edit": _keys("id"),
    "/masterdata": _keys(),
    "/masterdata/articles": _keys(),
    "/masterdata/articles/edit": _keys("id"),
    "/masterdata/contacts": _keys("type", "entity", "error"),
    "/masterdata/contacts/new": _keys("entity", "error"),
    "/masterdata/contacts/edit": _keys("id", "error"),
    "/masterdata/skr": _keys("msg", "type"),
    "/masterdata/skr/edit": _keys("id"),
    "/masterdata/skr/copy": _keys("id"),
    "/masterdata/bankaccounts": _keys(),
    "/masterdata/bankaccounts/edit": _keys("id"),
    "/masterdata/numberranges": _keys(),
    "/masterdata/numberranges/edit": _keys("id"),
    "/miscellaneous": _keys(),
    "/receipts": _keys(period=True),
    "/receipts/edit": _keys("number"),
    "/transactions": _keys(period=True),
    "/transactions/edit": _keys("id", period=True),
    "/assets": _keys("status"),
    "/assets/new": _keys("parent_id"),
    "/assets/edit": _keys("id"),
    "/assets/view": _keys("id"),
    "/asset_categories": _keys(),
    "/asset_categories/edit": _keys("id"),
    "/worktime": _keys("person", "error", period=True),
    "/worktime/edit": _keys("id", "person", period=True),
    "/users": _keys("info", "err"),
    "/trips": _keys("person", "error", period=True),
    "/trips/edit": _keys("id", "person", period=True),
}


def check_page_path(path):
    """Pfad (mit optionaler Query) für einen GET prüfen: Pfadteil und Query-Schlüssel müssen in
    ``_PAGE_ALLOWLIST`` stehen. Geprüft wird der rohe Text – ohne Dekodierung des Pfades."""
    if not isinstance(path, str) or not path:
        raise ValueError("Der Pfad muss ein nicht leerer Text sein.")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Der Pfad muss mit '/' beginnen (nicht mit '//').")
    if "\\" in path or "#" in path or re.search(r"[\x00-\x1f\x7f\s]", path):
        raise ValueError("Der Pfad darf keine Backslashes, '#', Steuerzeichen oder Leerzeichen enthalten.")
    page, _, query = path.partition("?")
    allowed = _PAGE_ALLOWLIST.get(page)
    if allowed is None:
        raise ValueError("Seite nicht freigegeben – das Werkzeug liest nur geprüfte Anzeigeseiten "
                         "(_PAGE_ALLOWLIST in tools/mcp_ubuchhaltung.py)")
    for key, _value in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if key not in allowed:
            raise ValueError(f"Query-Parameter nicht freigegeben: {key}")
    return path


# ── Zugangsdaten ─────────────────────────────────────────────────────────────
def _decode_credential_blob(raw):
    """Passwort-Blob eines Eintrags der Anmeldeinformationsverwaltung (UTF-16-LE) als Text; ein abschließendes Nullzeichen entfällt.

    Die Fehlermeldung nennt weder Bytes noch Länge – sonst gäbe sie Teile des Passworts preis.
    """
    try:
        text = raw.decode("utf-16-le")
    except UnicodeDecodeError:
        raise ValueError("Eintrag in der Anmeldeinformationsverwaltung ist kein UTF-16-Text – bitte mit cmdkey neu anlegen") from None
    return text[:-1] if text.endswith("\x00") else text


def _read_windows_credential(target):
    """(Benutzer, Passwort) aus der Windows-Anmeldeinformationsverwaltung oder ``None``.

    Passwort = UTF-16-LE-Blob des generischen Eintrags, wie ``cmdkey /generic:… /pass`` ihn anlegt.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None

    pointer = ctypes.POINTER(CREDENTIALW)()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(pointer)):      # 1 = CRED_TYPE_GENERIC
        return None
    try:
        credential = pointer.contents
        size = credential.CredentialBlobSize
        blob = bytes(credential.CredentialBlob[:size]) if size else b""
        return credential.UserName or None, _decode_credential_blob(blob)
    finally:
        advapi32.CredFree(pointer)


def load_credentials(env):
    """(Benutzer, Passwort) aus Umgebung, Windows-Anmeldeinformationsverwaltung oder keyring; sonst ``(None, None)``."""
    target = (env.get("UBUCHHALTUNG_MCP_CREDENTIAL") or "").strip() or DEFAULT_CREDENTIAL
    user = (env.get("UBUCHHALTUNG_MCP_USER") or "").strip() or None
    password = env.get("UBUCHHALTUNG_MCP_PASSWORD") or None
    if password is not None:
        if not user:
            raise ValueError("UBUCHHALTUNG_MCP_PASSWORD gilt nur zusammen mit UBUCHHALTUNG_MCP_USER.")
        return user, password
    stored = _read_windows_credential(target)
    if stored is not None:
        return user or stored[0], stored[1]
    if user:
        try:
            import keyring
        except ImportError:
            keyring = None
        if keyring is not None:
            password = keyring.get_password(target, user)
            if password:
                return user, password
    return None, None


# ── HTTP-Client ──────────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _session_cookie(headers):
    for value in headers.get_all("Set-Cookie") or []:
        match = re.match(r"\s*session=([^;\s]+)", value)
        if match:
            return match.group(1)
    return None


def _unexpected(status, headers, body):
    detail = f"Status {status}"
    location = headers.get("Location")
    if location:
        detail += f", Weiterleitung nach {location}"
    text = " ".join(clip(html_to_text(body.decode("utf-8", "replace")), 300).split()) if body else ""
    return f"Unerwartete Antwort der Instanz ({detail})" + (f": {text}" if text else "")


class _LimitExceeded(ConnectionError):
    """Antwort zu groß oder zu langsam (kein Verbindungsfehler: nicht als „nicht erreichbar“ melden)."""


def _is_loopback(host):
    """Gehört ``host`` zum eigenen Rechner (localhost, 127.0.0.0/8, ::1)?"""
    host = (host or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class UBuchhaltungClient:
    """Spricht eine uBuchhaltung-Instanz an: Anmeldung, CSRF-Token, SELECT über die SQL-Konsole, GET."""

    def __init__(self, base_url, username=None, password=None, timeout=DEFAULT_TIMEOUT, cafile=None,
                 allow_http=False):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self.timeout = timeout
        self.allow_http = allow_http
        self.session_token = None
        self.csrf_token = None
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if cafile:
            handlers.append(urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=cafile)))
        self._opener = urllib.request.build_opener(*handlers)

    def _read_limited(self, response, deadline):
        """Antwortkörper stückweise lesen, begrenzt auf ``MAX_BODY`` Bytes und bis ``deadline``.

        ``read1`` macht je Runde höchstens einen Zugriff auf den Socket; so wird die Frist auch
        bei einer Gegenstelle eingehalten, die nur tröpfchenweise sendet.
        """
        chunks = []
        size = 0
        while True:
            if time.monotonic() > deadline:
                raise _LimitExceeded(f"Antwort dauert zu lange (> {2 * self.timeout:g} s)")
            chunk = response.read1(_CHUNK)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > MAX_BODY:
                raise _LimitExceeded(f"Antwort zu groß (> {MAX_BODY // (1024 * 1024)} MB) – Abfrage eingrenzen")
            chunks.append(chunk)

    def _request(self, method, path, form=None, headers=None):
        """(Status, Header, Body); HTTP-Fehlerstatus und Weiterleitungen sind normale Antworten.

        Gesamtfrist je Anfrage: das Doppelte des Zeitlimits (siehe ``_read_limited``).
        """
        data = None
        request_headers = {"User-Agent": USER_AGENT}
        if self.session_token:
            request_headers["Cookie"] = f"session={self.session_token}"
        if form is not None:
            data = urllib.parse.urlencode(form).encode("ascii")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request_headers.update(headers or {})
        request = urllib.request.Request(self.base_url + urllib.parse.quote(path, safe=_URL_SAFE),
                                         data=data, headers=request_headers, method=method)
        deadline = time.monotonic() + 2 * self.timeout
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.status, response.headers, self._read_limited(response, deadline)
        except urllib.error.HTTPError as err:
            try:
                return err.code, err.headers, self._read_limited(err, deadline)
            finally:
                err.close()
        except _LimitExceeded:
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError) as err:
            raise ConnectionError(f"Instanz nicht erreichbar: {getattr(err, 'reason', err)}") from None

    @staticmethod
    def _login_redirect(status, headers):
        if not 300 <= status < 400:
            return False
        return urllib.parse.urlsplit(headers.get("Location") or "").path == "/login"

    def login(self):
        if not self.username or self._password is None:
            raise PermissionError(_SETUP_HINT)
        parts = urllib.parse.urlsplit(self.base_url)
        if parts.scheme == "http" and not _is_loopback(parts.hostname) and not self.allow_http:
            raise PermissionError(_PLAIN_HTTP_REFUSED)
        self.session_token = None
        status, headers, _ = self._request(
            "POST", "/login", form={"username": self.username, "password": self._password})
        token = _session_cookie(headers) if status == 303 else None
        if token:
            self.session_token = token
            self.csrf_token = None
            return
        if status == 200:
            raise PermissionError(
                "Anmeldung fehlgeschlagen – Benutzername oder Passwort falsch "
                "(Eintrag in der Windows-Anmeldeinformationsverwaltung prüfen)")
        raise PermissionError(f"Anmeldung fehlgeschlagen – unerwartete Antwort der Instanz (Status {status})")

    def ensure_session(self):
        """Sitzung sicherstellen und das CSRF-Token der Seite /about merken; liefert deren HTML."""
        status, headers, body = self._request("GET", "/about")
        if self._login_redirect(status, headers):
            self.login()
            status, headers, body = self._request("GET", "/about")
            if self._login_redirect(status, headers):
                raise PermissionError(_SESSION_REFUSED)
        if status != 200:
            raise ConnectionError(_unexpected(status, headers, body))
        page = body.decode("utf-8", "replace")
        match = _CSRF_RE.search(page)
        self.csrf_token = match.group(1) if match else None
        return page

    def get(self, path):
        status, headers, body = self._request("GET", path)
        if self._login_redirect(status, headers):
            self.login()
            status, headers, body = self._request("GET", path)
            if self._login_redirect(status, headers):
                raise PermissionError(_SESSION_REFUSED)
        return status, headers, body

    def _execute(self, sql):
        """Einen einzelnen SELECT über /execute_sql schicken; liefert die Ausgabezeilen der Konsole
        (erste Zeile = Spaltennamen)."""
        if ";" in sql or not sql.startswith("SELECT"):
            raise ReadOnlyViolation("Nur ein einzelnes SELECT ohne ';' darf an die SQL-Konsole gehen.")
        if not self.csrf_token:
            self.ensure_session()
        for attempt in (1, 2):
            status, headers, body = self._request(
                "POST", "/execute_sql", form={"sql_commands": sql},
                headers={"X-CSRF-Token": self.csrf_token or ""})
            if self._login_redirect(status, headers):
                if attempt == 2:
                    raise PermissionError(_SESSION_REFUSED)
                self.login()
                self.ensure_session()
            elif status == 403 and b"Sicherheits-Token" in body:
                if attempt == 2:
                    raise PermissionError("Das Sicherheits-Token wurde von der Instanz abgelehnt.")
                self.ensure_session()
            else:
                break
        try:
            result = json.loads(body.decode("utf-8"))
            errors = result["errors"]
            output = result["output"]
        except (ValueError, KeyError, TypeError):
            raise ConnectionError(_unexpected(status, headers, body)) from None
        if errors:
            message = "; ".join(str(error) for error in errors)
            if "blob" in message.lower():
                message += " – BLOB-Spalten mit hex(x) oder length(x) abfragen"
            raise QueryError(message)
        return output.split("\n") if output else []

    def run_select(self, query, max_rows):
        """SELECT über die SQL-Konsole: (Spaltennamen, Zeilen als Werte-Listen, weitere vorhanden?).

        Zweistufig, weil die Konsole Spalten mit ``;`` und Zeilen mit Zeilenumbruch trennt und
        Werte beides enthalten können: Zuerst liefert ``LIMIT 0`` die Spaltennamen, dann gibt
        ``json_array(…)`` jede Zeile als eine JSON-Zeile aus. Die Umhüllung begrenzt die Zeilenzahl
        schon in der Instanz; ihr Zeilenumbruch vor ``)`` lässt ``q`` mit ``--`` enden.
        """
        q = check_select(query)
        limit = int(max_rows)
        head = self._execute(f"SELECT * FROM (\n{q}\n) LIMIT 0")
        if len(head) > 1:
            raise QueryError("Spaltenname mit Zeilenumbruch – bitte mit AS benennen")
        columns = head[0].split(";") if head else []
        if not columns or "" in columns:
            raise QueryError("Leerer Spaltenname – bitte mit AS benennen")
        names = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
        lines = self._execute(f"SELECT json_array({names}) FROM (\n{q}\n) LIMIT {limit + 1}")
        try:
            rows = [json.loads(line) for line in lines[1:]]        # lines[0]: Name des Ausdrucks
        except ValueError:
            raise ConnectionError("Unerwartete Zeilenausgabe der Instanz (kein JSON)") from None
        return columns, rows[:limit], len(rows) > limit


def client_from_env(env):
    """Client aus den Umgebungsvariablen bauen (Adresse, Zugangsdaten, Zeitlimit, CA-Datei)."""
    url = (env.get("UBUCHHALTUNG_MCP_URL") or "").strip()
    if not url:
        raise ValueError("UBUCHHALTUNG_MCP_URL ist nicht gesetzt (Adresse der Instanz, "
                         "z. B. https://nas.local:2824).")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("UBUCHHALTUNG_MCP_URL muss mit http:// oder https:// beginnen und einen Host nennen.")
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise ValueError("UBUCHHALTUNG_MCP_URL darf weder Zugangsdaten noch Query oder Fragment enthalten.")
    raw_timeout = (env.get("UBUCHHALTUNG_MCP_TIMEOUT") or "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT
    except ValueError:
        timeout = 0.0
    if not timeout > 0:
        raise ValueError("UBUCHHALTUNG_MCP_TIMEOUT muss eine Zahl größer 0 sein (Sekunden).")
    user, password = load_credentials(env)
    cafile = (env.get("UBUCHHALTUNG_MCP_CAFILE") or "").strip() or None
    allow_http = (env.get("UBUCHHALTUNG_MCP_ALLOW_HTTP") or "").strip().lower() in ("1", "true", "yes", "ja")
    return UBuchhaltungClient(url.rstrip("/"), user, password, timeout, cafile, allow_http)


# ── HTML → Text ──────────────────────────────────────────────────────────────
_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})
_BLOCK_TAGS = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table",
                         "section", "form", "ul", "ol", "dl", "dt", "dd", "header", "footer", "nav",
                         "main", "article", "title"})


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._lines = []
        self._current = []
        self._skip = 0
        self._cells = 0

    def _break(self, force=False):
        line = " ".join("".join(self._current).split())
        self._current = []
        if line or force:
            self._lines.append(line)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif self._skip:
            return
        elif tag == "br":
            self._break(force=True)
        elif tag in ("td", "th"):
            if self._cells:
                self._current.append(" | ")
            self._cells += 1
        elif tag in _BLOCK_TAGS:
            self._break()
            if tag == "tr":
                self._cells = 0

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif not self._skip and tag in _BLOCK_TAGS and tag != "br":
            self._break()
            if tag == "tr":
                self._cells = 0

    def handle_data(self, data):
        if not self._skip:
            self._current.append(data)

    def text(self):
        self._break()
        lines = []
        for line in self._lines:
            if line or (lines and lines[-1]):
                lines.append(line)
        return "\n".join(lines).strip()


def html_to_text(markup):
    """HTML als lesbaren Text: Blockelemente als Zeilen, Tabellenzellen mit `` | ``, ohne Skripte und Stile."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return parser.text()


# ── Werkzeuge ────────────────────────────────────────────────────────────────
def _tool_info(client, args):
    page = client.ensure_session()
    who = client.username if client.session_token else "automatische Anmeldung"
    return (f"Instanz: {client.base_url}\nAngemeldet als: {who}\n\n"
            + clip(html_to_text(page), 4000))


def _json_line(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _row_text(row):
    """Werte einer Zeile lesbar, mit `` | `` getrennt (NULL leer)."""
    return " | ".join("" if value is None else str(value) for value in row)


def _tool_sql(client, args):
    columns, rows, more = client.run_select(args["query"], args["max_rows"])
    footer = (f"{len(rows)} Zeilen gezeigt – weitere vorhanden (LIMIT/max_rows)" if more
              else f"{len(rows)} Zeile(n)")
    return "\n".join([_json_line(columns), *map(_json_line, rows)]) + f"\n\n{footer}"


def _first(client, query):
    rows = client.run_select(query, 1)[1]
    return _row_text(rows[0][:1]) if rows else "unbekannt"


def _tool_db_check(client, args):
    check = "integrity_check" if args["full"] else "quick_check"
    out = []
    _, rows, more = client.run_select(f"SELECT * FROM pragma_{check}", 200)
    if rows == [["ok"]]:
        out.append(f"Integrität ({check}): ok")
    else:
        out += [f"Integrität ({check}): FEHLER", *map(_row_text, rows)]
        if more:
            out.append("… weitere Meldungen vorhanden")
    columns, rows, more = client.run_select("SELECT * FROM pragma_foreign_key_check", 200)
    if rows:
        out += [f"Fremdschlüssel: {len(rows)}{'+' if more else ''} Verstöße ({' | '.join(columns)})",
                *map(_row_text, rows)]
    else:
        out.append("Fremdschlüssel: keine Verstöße")
    out.append("Schema-Version (user_version): "
               + _first(client, "SELECT * FROM pragma_user_version"))
    out.append("Journal-Modus: " + _first(client, "SELECT * FROM pragma_journal_mode"))
    out.append("Tabellen: " + _first(
        client, "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'")
        + " (einschließlich interner sqlite_-Tabellen)")
    return "\n".join(out)


def _tool_schema(client, args):
    table = args.get("table")
    if table is None:
        return _schema_overview(client)
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError("Ungültiger Tabellenname (erlaubt: Buchstaben, Ziffern, Unterstrich).")
    _, rows, _ = client.run_select(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'", 1)
    if not rows:
        raise ValueError(f"Die Tabelle '{table}' gibt es nicht.")
    sections = [
        ("CREATE-Anweisung", ("SELECT replace(replace(sql, char(13), ' '), char(10), ' ') AS sql "
                              f"FROM sqlite_master WHERE type='table' AND name='{table}'")),
        ("Spalten", f"SELECT * FROM pragma_table_info('{table}')"),
        ("Fremdschlüssel", f"SELECT * FROM pragma_foreign_key_list('{table}')"),
        ("Indizes", f"SELECT * FROM pragma_index_list('{table}')"),
    ]
    out = [f"Tabelle {table}"]
    for title, query in sections:
        columns, rows, _ = client.run_select(query, 500)
        out.append(f"\n== {title} ==")
        out += [" | ".join(columns), *map(_row_text, rows)] if rows else ["(keine)"]
    return "\n".join(out)


def _schema_overview(client):
    _, name_rows, _ = client.run_select(
        "SELECT name FROM sqlite_master WHERE type='table' AND substr(name, 1, 7) <> 'sqlite_' "
        "ORDER BY name", 2000)
    names = [row[0] for row in name_rows]
    valid = [name for name in names if _IDENTIFIER.fullmatch(name)]
    invalid = [name for name in names if not _IDENTIFIER.fullmatch(name)]
    out = [f"Tabellen ({len(names)}, ohne interne sqlite_-Tabellen), Name: Zeilen"]
    for start in range(0, len(valid), 100):
        block = valid[start:start + 100]
        query = " UNION ALL ".join(
            f"SELECT '{name}' AS tabelle, COUNT(*) AS zeilen FROM \"{name}\"" for name in block)
        _, rows, _ = client.run_select(query, 100)
        out += [f"{name}: {count}" for name, count in rows]
    if invalid:
        out.append("Nicht abgefragt (ungültiger Name): " + ", ".join(invalid))
    return "\n".join(out)


_TEXT_TYPES = ("text/plain", "application/json", "application/xml", "text/xml", "text/csv")


def _tool_page(client, args):
    status, headers, body = client.get(check_page_path(args["path"]))
    out = [f"Status {status}"]
    kind = headers.get_content_type()
    if 300 <= status < 400:
        out.append(f"Weiterleitung nach {headers.get('Location') or '(ohne Ziel)'}")
    elif kind == "text/html":
        out.append(html_to_text(body.decode("utf-8", "replace")))
    elif kind in _TEXT_TYPES:
        out.append(body.decode("utf-8", "replace"))
    else:
        out.append(f"Binärinhalt ({kind}), {len(body)} Bytes – nicht ausgegeben")
    return clip("\n".join(out), args["max_chars"])


_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
              "idempotentHint": True, "openWorldHint": False}


def _tool(name, title, description, properties=None, required=()):
    schema = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return {"name": name, "title": title, "description": description,
            "inputSchema": schema, "annotations": dict(_READ_ONLY)}


TOOLS = [
    _tool("info", "Instanz-Info",
          "Zeigt Adresse, Anmeldung und Version der verbundenen uBuchhaltung-Instanz (Seite /about)."),
    _tool("sql", "SQL-Abfrage (nur SELECT)",
          "Führt genau ein SELECT ohne ';' in der Datenbank des angemeldeten Benutzers aus. "
          "Ausgabe: erste Zeile die Spaltennamen als JSON-Array, dann je Zeile ein JSON-Array "
          "(NULL = null, Zahlen als Zahlen), zuletzt die Zeilenzahl. Geldbeträge stehen als Ganzzahl "
          "in Minor Units (SCALE 4): 1190000 = 119,00 €. PRAGMAs nur als lesende Tabellenfunktion, "
          "z. B. SELECT * FROM pragma_foreign_key_check. Gesperrt (Last): WITH/RECURSIVE, zeroblob, "
          "randomblob. BLOB-Spalten mit hex(x) oder length(x) abfragen; gleiche Spaltennamen mit AS "
          "unterscheiden (SQLite benennt sie sonst ID, ID:1 um).",
          {"query": {"type": "string", "description": "Genau ein SELECT, ohne ';'."},
           "max_rows": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200,
                        "description": "Höchstzahl der ausgegebenen Zeilen."}},
          required=("query",)),
    _tool("db_check", "Datenbank prüfen",
          "Prüft die Datenbank: quick_check (mit full: integrity_check), Fremdschlüssel, "
          "Schema-Version, Journal-Modus und Tabellenzahl.",
          {"full": {"type": "boolean", "default": False,
                    "description": "Gründliche integrity_check statt quick_check (langsamer)."}}),
    _tool("schema", "Schema und Tabellen",
          "Ohne table: alle Tabellen mit Zeilenzahl. Mit table: CREATE-Anweisung, Spalten, "
          "Fremdschlüssel und Indizes dieser Tabelle.",
          {"table": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                     "description": "Tabellenname, z. B. Invoices."}}),
    _tool("page", "Seite abrufen (nur GET)",
          "Ruft eine geprüfte Anzeigeseite der Instanz per GET ab und gibt sie als Text aus, "
          "z. B. /about, /invoice?id=3, /masterdata/contacts, /users. Nur freigegebene Pfade und "
          "Query-Parameter; Aktionen, PDF-Erzeugung, /login und /logout sind gesperrt.",
          {"path": {"type": "string", "description": "Pfad der Seite mit optionaler Query, z. B. /about."},
           "max_chars": {"type": "integer", "minimum": 500, "maximum": 50000, "default": 8000,
                         "description": "Höchstzahl der ausgegebenen Zeichen."}},
          required=("path",)),
]
_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}
_HANDLERS = {"info": _tool_info, "sql": _tool_sql, "db_check": _tool_db_check,
             "schema": _tool_schema, "page": _tool_page}
_TYPE_NAMES = {"string": ("ein Text", str), "integer": ("eine ganze Zahl", int),
               "boolean": ("wahr oder falsch", bool)}


def _validate_arguments(schema, arguments):
    """Argumente gegen das inputSchema prüfen und Standardwerte ergänzen (ValueError bei Fehlern)."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("Die Argumente müssen ein Objekt sein.")
    properties = schema["properties"]
    for key in arguments:
        if key not in properties:
            raise ValueError(f"Unbekanntes Argument: '{key}'.")
    for key in schema.get("required", ()):
        if key not in arguments:
            raise ValueError(f"Pflichtargument fehlt: '{key}'.")
    checked = {}
    for key, spec in properties.items():
        if key not in arguments:
            if "default" in spec:
                checked[key] = spec["default"]
            continue
        value = arguments[key]
        label, kind = _TYPE_NAMES[spec["type"]]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ValueError(f"Argument '{key}' muss {label} sein.")
        if kind is int and not spec["minimum"] <= value <= spec["maximum"]:
            raise ValueError(f"Argument '{key}' muss zwischen {spec['minimum']} und {spec['maximum']} liegen.")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
            raise ValueError(f"Argument '{key}' hat ein ungültiges Format.")
        checked[key] = value
    return checked


# ── MCP-Protokoll (JSON-RPC 2.0, eine Nachricht je Zeile) ────────────────────
class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _valid_id(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": clip(text)}], "isError": is_error}


class Server:
    """MCP-Server: ``handle`` beantwortet eine Nachricht, ``serve`` liest und schreibt Zeilen."""

    def __init__(self, env=None, client=None):
        self._env = os.environ if env is None else env
        self._client = client

    def _get_client(self):
        if self._client is None:
            self._client = client_from_env(self._env)
        return self._client

    def handle(self, message):
        """Eine JSON-RPC-Nachricht beantworten; ``None`` bei Benachrichtigungen (ohne ``id``)."""
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            request_id = message.get("id") if isinstance(message, dict) else None
            return _error(request_id if _valid_id(request_id) else None, -32600, "Ungültige Anfrage")
        if "id" not in message:
            return None
        request_id = message["id"]
        if not _valid_id(request_id):
            return _error(None, -32600, "Ungültige Anfrage (id)")
        try:
            result = self._dispatch(message["method"], message.get("params"))
        except _RpcError as err:
            return _error(request_id, err.code, err.message)
        except Exception as err:
            _log(f"Interner Fehler: {type(err).__name__}: {err}")
            return _error(request_id, -32603, f"Interner Fehler: {type(err).__name__}: {err}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method, params):
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            return {"protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call_tool(params)
        raise _RpcError(-32601, f"Methode nicht gefunden: {method}")

    def _call_tool(self, params):
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            raise _RpcError(-32602, "Ungültige Parameter: 'name' fehlt.")
        name = params["name"]
        tool = _TOOLS_BY_NAME.get(name)
        if tool is None:
            raise _RpcError(-32602, f"Unbekanntes Werkzeug: {name}")
        try:
            arguments = _validate_arguments(tool["inputSchema"], params.get("arguments"))
            return _tool_result(_HANDLERS[name](self._get_client(), arguments))
        except (ReadOnlyViolation, ValueError, PermissionError, ConnectionError, QueryError) as err:
            return _tool_result(str(err), True)
        except Exception as err:
            _log(f"Interner Fehler im Werkzeug {name}: {type(err).__name__}: {err}")
            return _tool_result(f"Interner Fehler: {type(err).__name__}: {err}", True)

    def handle_line(self, line):
        """Eine Eingabezeile (JSON, auch Batch) beantworten; ``None``, wenn nichts zu senden ist."""
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            return _error(None, -32700, "Ungültiges JSON")
        if isinstance(message, list):
            if not message:
                return _error(None, -32600, "Ungültige Anfrage (leerer Batch)")
            replies = [reply for reply in map(self.handle, message) if reply is not None]
            return replies or None
        return self.handle(message)

    def serve(self, instream, outstream):
        """Zeilen aus ``instream`` (Bytes, UTF-8) beantworten, bis die Eingabe endet."""
        for raw in instream:
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                reply = _error(None, -32700, "Ungültiges JSON (kein UTF-8)")
            else:
                if not line.strip():
                    continue
                reply = self.handle_line(line)
            if reply is not None:
                outstream.write(json.dumps(reply, ensure_ascii=False).encode("utf-8", "replace") + b"\n")
                outstream.flush()


def main():
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr          # ein verirrtes print() darf das Protokoll nicht stören
    _log(f"{SERVER_NAME} {SERVER_VERSION} (stdio) gestartet")
    try:
        Server().serve(sys.stdin.buffer, protocol_out)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
