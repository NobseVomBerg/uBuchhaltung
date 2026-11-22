# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Main HTTP server class with routing
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlsplit
from http.cookies import SimpleCookie
import contextlib
import datetime
import ipaddress
import json
import socket
import ssl
import sys
import unicodedata
from db import Database, DeleteRefused, MigrationBackupError, SchemaTooNewError
from export.pdf_core import LOGO_CONTENT_TYPES, logo_extension
import os
import auth
import demo
import userctx

from . import backup
from . import dirlock
from . import pages
from . import handlers
from . import receipt_files
from . import receipt_links
from . import upload_handler
from . import wiso_upload
from .pages_login import PageLogin
from .pages_users import PageUsers
from .period import period_cookie_header, period_month, resolve_period
from .pages_assets import (
    PageAssets, PageAssetView, PageAssetEdit,
    PageAssetCategories, PageAssetCategoryEdit,
)
from .pages_masterdata import (
    PageMasterData, PageArticles, PageArticleEdit,
    PageSkr, PageSkrEdit,
    PageBankAccounts, PageBankAccountEdit,
    PageNumberRanges, PageNumberRangesEdit,
)
from .pages_contacts import PageContacts, PageContactNew, PageContactEdit
from .pages_miscellaneous import PageMiscellaneous
from .pages_transactions import PageTransactions
from .pages_dashboard import PageDashboard
from .pages_bwa import PDF_FAILED_TEXT, BwaQuery, PageBwa, read_pdf_query, read_variant
from .pages_euer import EUER_PDF_FAILED_TEXT, EuerQuery, PageEuer, read_euer_pdf_query
from .pages_susa import SUSA_PDF_FAILED_TEXT, PageSusa, SusaQuery, read_susa_pdf_query
from .pages_liquidity import LIQUIDITY_PDF_FAILED_TEXT, PageLiquidity
from .pages_receipts import PageReceipts, PageReceiptEdit
from .pages_setup import PageSetup, PageSetupStart
from .pages_invoice import PageInvoice
from .pages_quote import PageQuote
from .pages_worktime import CUSTOMER_ALL, PageWorkTimes, customer_filter, customer_query
from .pages_trips import PageTrips

def parse_form(post_body: str) -> dict:
    """Formular-POST in {Feldname: [Werte]} zerlegen.

    ``keep_blank_values`` ist hier zwingend: Formulare mit gleichnamigen
    Feldern lesen zeilenweise über den Index (Buchungssatz-Editor: split_id,
    split_amount, …). Verschwiege parse_qs ein leer gelassenes Feld, rutschten
    alle folgenden Zeilen um eins – der Steuerbetrag der einen Buchung landete
    stillschweigend auf der anderen.
    """
    return parse_qs(post_body, keep_blank_values=True)


def content_disposition(disposition, filename):
    """Content-Disposition-Headerwert für Downloads bauen.

    Dateinamen nach der Namenskonvention "[Nummer] [Kundenname]" enthalten
    Leerzeichen und ggf. Umlaute – dafür braucht der Header Quoting plus
    RFC-5987-Kodierung (``filename*``); ``filename`` bleibt ASCII-Fallback.
    """
    fallback = (unicodedata.normalize('NFKD', filename)
                .encode('ascii', 'ignore').decode('ascii'))
    fallback = fallback.replace('"', '').replace('\\', '') or 'download'
    return (f'{disposition}; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}")


_HEADER_UNSAFE = str.maketrans('', '', '\r\n\0')


def _header_safe(value):
    """Name oder Wert eines HTTP-Headers ohne CR, LF und NUL, nur mit Latin-1-Zeichen.

    ``http.server`` schreibt Header als Latin-1; jedes Zeichen darüber hinaus (ein „€“ oder
    Emoji in einem Dateinamen oder einer Adresse) würde dort einen UnicodeEncodeError und
    damit einen 500 auslösen. Es wird durch ``?`` ersetzt.
    """
    return str(value).translate(_HEADER_UNSAFE).encode('latin-1', 'replace').decode('latin-1')


# IDs sind Schlüssel in SQLite (64 Bit mit Vorzeichen): Was darüber hinausgeht, kann es nicht geben –
# und ließe sich nicht einmal an eine Abfrage binden (OverflowError, also 500).
_INT64_MIN, _INT64_MAX = -2 ** 63, 2 ** 63 - 1


def _int_param(qs, name):
    """Ganzzahliger Query-Parameter (``qs`` aus parse_qs) oder ``None``, wenn er fehlt,
    keine Ganzzahl ist oder nicht in 64 Bit passt."""
    try:
        value = int(qs[name][0])
    except (KeyError, IndexError, ValueError):
        return None
    return value if _INT64_MIN <= value <= _INT64_MAX else None


def _int_params(qs, name):
    """Alle ganzzahligen Werte eines Query-Parameters; was keine Ganzzahl ist, entfällt."""
    values = []
    for raw in qs.get(name, []):
        try:
            value = int(raw)
        except ValueError:
            continue
        if _INT64_MIN <= value <= _INT64_MAX:
            values.append(value)
    return values


def _id_param(qs, name='id'):
    """ID einer Seite, die ein bestimmtes Objekt zeigt (bearbeiten, ansehen, herunterladen). Fehlt sie oder
    ist sie keine Ganzzahl, gilt ``-1``: Das ist nie eine vorhandene ID, die Seite antwortet also wie auf
    jede unbekannte ID (Meldung „nicht gefunden“ oder leere Maske), statt mit 500."""
    value = _int_param(qs, name)
    return -1 if value is None else value


def _customer_param(qs):
    """Kundenfilter der Arbeitszeiten aus der Adresse (``customer_filter``): ``''`` (fehlt oder alle), ``'none'`` oder eine
    Kunden-ID; ``None``, wenn der Wert ungültig ist. Die Seiten lesen das wie einen Filter (ungültig = nicht angegeben,
    ``or CUSTOMER_ALL``), der Stundenzettel lehnt es ab."""
    return customer_filter(qs.get('customer', [''])[0])


def _trip_check_params(qs):
    """Fahrzeug und Jahr der Lückenprüfung des Fahrtenbuchs (``?check_vehicle=…&check_year=…``) – beides oder nichts:
    das Fahrzeug getrimmt, nicht leer und höchstens 500 Zeichen, das Jahr genau vier ASCII-Ziffern von 1900 bis 2999.
    Was nicht passt, heißt ``(None, None)``: keine Prüfung, und kein Fehler."""
    vehicle = qs.get('check_vehicle', [''])[0].strip()
    year = qs.get('check_year', [''])[0]
    if (vehicle and len(vehicle) <= 500 and len(year) == 4 and year.isascii() and year.isdigit()
            and 1900 <= int(year) <= 2999):
        return vehicle, int(year)
    return None, None


def forwarded_https(headers):
    """Hat ein Reverse-Proxy die Anfrage über HTTPS entgegengenommen? Der erste Wert von
    ``X-Forwarded-Proto`` (bei einer Kette der des Clients) muss ``https`` sein."""
    return (headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower() == 'https'


# ── Obergrenzen für Anfragegrößen ────────────────────────────────────────────
# Ohne Grenze liest jede Stelle so viele Bytes, wie Content-Length behauptet. Geprüft wird die erklärte
# Länge, bevor etwas gelesen wird; was darüber liegt, wird mit 413 abgelehnt.
MAX_PREAUTH_BODY = 64 * 1024              # ohne angemeldeten Benutzer: /login, /logout, /setup/start
MAX_FORM_BODY = 10 * 1024 * 1024          # Formulare und JSON
MAX_UPLOAD_BODY = 200 * 1024 * 1024       # /upload_receipts, /receipts/upload, /wiso/import
MAX_DRAIN_BODY = 1024 * 1024              # was _drain_body höchstens wegliest (darüber: Verbindung schließen)


def _size_text(limit):
    if limit >= 1024 ** 3 and limit % 1024 ** 3 == 0:
        return f"{limit // 1024 ** 3} GB"
    return f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024 else f"{limit // 1024} KB"


# ── Zeitgrenze je Verbindung ─────────────────────────────────────────────────
# Ohne Zeitgrenze hält ein Client, der seinen Body nicht zu Ende schickt, die Antwort nicht abholt oder eine Keep-Alive-Verbindung
# offen lässt, einen Thread des Servers und – ab dem Betreten von ``_run_locked`` – die Lesesperre des Datenverzeichnisses beliebig lange.
CONNECTION_TIMEOUT = 120      # Sekunden ohne Fortschritt auf einer Verbindung (je Lese- bzw. Sendeschritt), dann wird sie geschlossen
SEND_CHUNK = 256 * 1024       # Antworten gehen in Stücken dieser Größe hinaus – die Zeitgrenze gilt je Stück, nicht für die ganze Antwort
DRAIN_TIMEOUT = 2             # Sekunden, die der Server nach der Antwort auf den ungelesenen Rest eines Bodys wartet (``_settle_body``)
TLS_HANDSHAKE_TIMEOUT = 15    # Sekunden für den TLS-Handshake einer neuen Verbindung, dann wird sie geschlossen
ACCEPT_QUEUE = 128            # Verbindungen, die das Betriebssystem annimmt, bevor der Server sie abholt (Standard der Bibliothek: 5)


class _TimedReader:
    """Der lesende Strom einer Verbindung (``rfile``) mit einer Zusatzregel: Eine Zeitüberschreitung beim Lesen (``TimeoutError``) beendet
    die Verbindung nach der laufenden Anfrage (``close_connection``). Danach ist der Leser ohnehin unbrauchbar (jedes weitere Lesen
    scheitert), und ein halb gelesener Body darf nie als nächste Anfrage gelesen werden – auch dort nicht, wo ein Handler den Body selbst
    liest und die Ausnahme fängt. Dazu zählt er die gelesenen Bytes (``count``; der Handler setzt es nach den Kopfzeilen auf 0): So
    weiß der Server nach der Antwort, ob der erklärte Body der Anfrage ganz gelesen ist (``_settle_body``). Alles andere reicht der
    Wrapper unverändert an den Strom durch."""

    _READS = frozenset({'read', 'readline', 'readlines', 'readinto', 'readinto1', 'read1', 'peek'})

    def __init__(self, raw, handler):
        self._raw = raw
        self._handler = handler
        self.count = 0

    def __getattr__(self, name):
        attribute = getattr(self._raw, name)
        if name not in self._READS:
            return attribute

        def read_or_close(*args, **kwargs):
            try:
                result = attribute(*args, **kwargs)
            except TimeoutError:
                self._handler.close_connection = True
                raise
            if name == 'readlines':
                self.count += sum(len(line) for line in result)
            elif name != 'peek':                    # peek verbraucht nichts
                self.count += result if isinstance(result, int) else len(result or b'')
            return result
        return read_or_close


class _SteppedWriter:
    """Der schreibende Strom einer Verbindung (``wfile``): Eine Antwort geht in Stücken von ``SEND_CHUNK`` hinaus. Die Zeitgrenze des
    Sockets gilt für ``sendall`` sonst für die GANZE Antwort – eine große Datei bräche auf einer langsamen Leitung ab, obwohl der Client
    stetig liest. So gilt sie je Stück: Wer nichts mehr abholt, scheitert nach der Zeitgrenze, wer liest, bekommt alles. Scheitert ein
    Stück, ist die Verbindung am Ende (``close_connection``, ``failed``), und jeder weitere ``write`` wirft sofort
    ``ConnectionAbortedError``, ohne sie noch einmal anzufassen – eine Fehlerantwort wartete sonst erneut die volle Zeitgrenze. Alles
    andere (``flush``, ``close``, ``closed``) reicht der Wrapper unverändert an den Strom durch. Der Rohstrom schreibt mit ``sendall``
    (``wbufsize = 0``): Ein ``write`` kehrt erst zurück, wenn das ganze Stück draußen ist – sein Rückgabewert wird nicht gebraucht."""

    def __init__(self, raw, handler):
        self._raw = raw
        self._handler = handler
        self.failed = False

    def write(self, data):
        if self.failed:
            raise ConnectionAbortedError('Die Verbindung ist nach einem Sendefehler beendet.')
        view = memoryview(data).cast('B')
        try:
            for start in range(0, view.nbytes, SEND_CHUNK):
                self._raw.write(view[start:start + SEND_CHUNK])
        except OSError:                      # auch TimeoutError und ConnectionError
            self.failed = True
            self._handler.close_connection = True
            raise
        return view.nbytes

    def __getattr__(self, name):
        return getattr(self._raw, name)


# ── Schutz vor fremden Webseiten (CSRF, DNS-Rebinding) ───────────────────────
# Hostnamen, die nur im eigenen Netz auflösbar sind – ein Angreifer kann sie
# nicht per öffentlichem DNS auf diesen Rechner umbiegen.
_PRIVATE_HOST_SUFFIXES = ('.localhost', '.local', '.lan', '.home.arpa',
                          '.internal', '.fritz.box')


def _allowed_hosts_env():
    """Zusätzlich freigegebene Hostnamen aus UBUCHHALTUNG_ALLOWED_HOSTS (Kommaliste)."""
    raw = os.environ.get('UBUCHHALTUNG_ALLOWED_HOSTS', '')
    return {_hostname_of(h) for h in raw.split(',') if h.strip()}


def _hostname_of(hostport):
    """Hostname ohne Port, klein geschrieben, IPv6 ohne eckige Klammern."""
    h = (hostport or '').strip().lower()
    if h.startswith('['):
        return h[1:].split(']', 1)[0]
    if h.count(':') == 1:
        h = h.split(':', 1)[0]
    return h.rstrip('.')


def _strip_default_port(netloc):
    for suffix in (':80', ':443'):
        if netloc.endswith(suffix):
            return netloc[:-len(suffix)]
    return netloc


def host_is_trusted(host_header):
    """Darf unter diesem Host-Header ohne Login gearbeitet werden?

    Schutz vor DNS-Rebinding: Eine fremde Webseite kann ihren eigenen Domainnamen
    auf 127.0.0.1 bzw. eine LAN-Adresse zeigen lassen und wäre dann aus Sicht des
    Browsers "dieselbe Herkunft" wie die App. IP-Adressen, localhost, Namen ohne
    Punkt, rein lokale Endungen und der eigene Rechnername lassen sich so nicht
    umbiegen; alles andere muss über UBUCHHALTUNG_ALLOWED_HOSTS freigegeben sein.
    """
    name = _hostname_of(host_header)
    if not name:
        return True                       # kein Host-Header: kein Browser
    if name == 'localhost' or ('.' not in name and ':' not in name):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    if name == socket.gethostname().lower().rstrip('.'):
        return True
    return name in _allowed_hosts_env()


def request_is_same_origin(headers):
    """Stammt eine schreibende Anfrage von einer Seite der App selbst?

    Reihenfolge: Sec-Fetch-Site (schicken Browser nur über HTTPS/localhost),
    sonst Origin gegen Host bzw. X-Forwarded-Host (Reverse-Proxy). Fehlen beide
    Header, ist es kein Browser (curl, Skripte) – dort gibt es kein CSRF.
    """
    site = headers.get('Sec-Fetch-Site')
    if site:
        return site.strip().lower() in ('same-origin', 'none')
    origin = headers.get('Origin')
    if origin is None:
        return True
    origin = origin.strip().lower()
    if not origin or origin == 'null':
        return False
    try:
        netloc = _strip_default_port(urlsplit(origin).netloc)
    except ValueError:                    # kaputter Origin-Header (z. B. "http://[::1")
        return False
    if not netloc:
        return False
    own = {_strip_default_port((headers.get('Host') or '').strip().lower()),
           _strip_default_port((headers.get('X-Forwarded-Host') or '')
                               .split(',')[0].strip().lower())}
    own.discard('')
    if netloc in own:
        return True
    return _hostname_of(netloc) in _allowed_hosts_env()


# Aktionen mit Seiteneffekt – nur per POST, siehe `_post_action_routes`; die
# Parameter bleiben in der Query. `/masterdata/skr/add_standard` hat keinen.
_ACTION_PATHS = frozenset({
    "/masterdata/articles/delete",
    "/masterdata/contacts/delete",
    "/masterdata/skr/delete",
    "/masterdata/skr/togglemenu",
    "/masterdata/skr/add_standard",
    "/masterdata/bankaccounts/delete",
    "/receipts/delete",
    "/transactions/delete",
    "/masterdata/numberranges/delete",
    "/assets/delete",
    "/asset_categories/delete",
    "/worktime/delete",
    "/trips/delete",
    "/quote/delete",
    "/invoice/delete",
})

# Dokumente erzeugen und ablegen: ebenfalls nur per POST (GET → 405), aber mit JSON-Antwort
# statt Weiterleitung. Parameter in der Query; das CSRF-Token kommt wie bei /invoice/save
# als Header (fetch), siehe `_post_generate_document`. Abmelden (POST /logout) liegt bei
# /login in `_auth_routes_*`.
_GENERATE_PATHS = frozenset({
    "/invoice/pdf_generate",
    "/quote/pdf_generate",
})

# Rechnungen und Angebote per fetch: JSON hinein, JSON hinaus, siehe `_post_json_route`. Dazu das Verknüpfen von Belegen
# und Buchungen (2.5.12), das Neuaufbauen einer Zahlungszuordnung (2.5.13) und das Lesen eines Belegs (2.6.0).
_JSON_PATHS = frozenset({
    "/invoice/save",
    "/invoice/send-email",
    "/invoice/link-payment",
    "/invoice/delete-payment",
    "/invoice/status",
    "/invoice/cancel",
    "/invoice/reissue",
    "/invoice/rebuild-payments",
    "/quote/save",
    "/quote/status",
    "/bookings/link-document",
    "/bookings/unlink-document",
    "/receipts/read",
})

# Aktionen mit Seiteneffekt unter den JSON-Routen: GET ergibt 405 (wie bei `_ACTION_PATHS`/`_GENERATE_PATHS`).
# Stornieren und Neu ausstellen legen einen Beleg an, siehe `handlers.handle_invoice_cancel` und
# `handlers.handle_invoice_reissue`. Belege verknüpfen (2.5.12): `handlers.handle_booking_link_document` und
# `handlers.handle_booking_unlink_document`. Zuordnung neu aufbauen (2.5.13): `handlers.handle_invoice_rebuild_payments` –
# auch der Probelauf ist ein POST (er nimmt kurz die Schreibsperre). Einen Beleg lesen (2.6.0): `handlers.handle_receipt_read` –
# schreibt die Lesung in die Datenbank und startet einen Kindprozess.
_POST_ONLY_JSON_PATHS = frozenset({
    "/invoice/cancel",
    "/invoice/reissue",
    "/invoice/rebuild-payments",
    "/bookings/link-document",
    "/bookings/unlink-document",
    "/receipts/read",
})

# Die Suchen der Belege und Buchungen (2.5.12) und die Vorschläge zu einem Beleg (2.6.0, `handlers.handle_receipt_suggestions`): GET,
# rein lesend, JSON-Antwort – kein Seiteneffekt, aber die Seiten lesen JSON, also kommt auch die 503 während einer Wiederherstellung
# als JSON (`_JSON_ANSWER_ROUTES`). Alle drei antworten nur auf Anfragen der eigenen Seiten (Herkunftsprüfung in `do_GET`) und nie aus
# einem Cache (`end_headers`).
_SEARCH_PATHS = frozenset({
    "/receipts/search",
    "/receipts/suggestions",
    "/transactions/search",
})

# Uploads mit eigener Prüfung vor dem ersten gelesenen Byte: nur per POST (GET → 405), JSON-Antwort, CSRF-Token als Header.
# /wiso/fdb-upload: die WISO-Datenbank als roher Body (XMLHttpRequest, das Footer-JS hängt das Token nur an fetch an), siehe
# `_post_wiso_fdb_upload`; /receipts/upload: die Dateien der Belege-Seite als Multipart (fetch), siehe `_post_receipts_upload`.
_UPLOAD_PATHS = frozenset({
    "/wiso/fdb-upload",
    "/receipts/upload",
})

# Jede Anfrage eines angemeldeten Benutzers läuft unter der Lesesperre seines Datenverzeichnisses (``dirlock``, siehe
# ``_run_locked``); während einer Wiederherstellung antwortet der Server mit 503. Ausgenommen sind diese Adressen (nur GET): Der
# Download einer Sicherung liest nur aus backup/ – das eine Wiederherstellung nie anfasst – und kann lange dauern, er darf sie nicht
# aufhalten; die statischen Dateien gehören zu keinem Datenverzeichnis. Logos (``/data/logos/``) liegen im Datenordner: gesperrt.
_LOCK_FREE_ROUTES = frozenset({"/backup/download", "/buch.css", "/favicon.ico"})

# Routen, deren Seiten JSON erwarten (fetch/XMLHttpRequest): Die 503 während einer Wiederherstellung kommt dort als JSON, mit den
# Schlüsseln, die die Seiten lesen (``success``/``error``; ``ok``/``error`` beim Import der Auszüge; ``errors`` der SQL-Konsole).
_JSON_ANSWER_ROUTES = (_JSON_PATHS | _GENERATE_PATHS | _UPLOAD_PATHS | _SEARCH_PATHS
                       | frozenset({"/upload_receipts", "/masterdata/contacts/upload-logo",
                                    "/confirm_transactions", "/execute_sql"}))


class SimpleWebServer(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = CONNECTION_TIMEOUT      # ``StreamRequestHandler.setup`` legt es als Zeitgrenze auf jede Verbindung (je Lese-/Sendeschritt)

    def setup(self):
        super().setup()
        # Bei eigenem TLS (``build_server``) ist die Verbindung hier noch nicht verhandelt: Der Handshake läuft in diesem Thread, nie in
        # ``accept()`` – dort hielte ein Client, der nur verbindet und schweigt, die Annahme aller neuen Verbindungen auf.
        self._tls_failed = False
        if isinstance(self.connection, ssl.SSLSocket):
            try:
                self.connection.settimeout(TLS_HANDSHAKE_TIMEOUT)
                self.connection.do_handshake()
                self.connection.settimeout(self.timeout)
            except OSError:            # auch ssl.SSLError, TimeoutError, ConnectionError: kein TLS, zu langsam, abgebrochen
                self._tls_failed = True
        self.rfile = _TimedReader(self.rfile, self)
        self.wfile = _SteppedWriter(self.wfile, self)

    def handle(self):
        """Nach einem gescheiterten TLS-Handshake gibt es nichts zu bearbeiten: Die Verbindung wird still geschlossen (kein Eintrag im Log –
        ein Client ohne TLS oder einer, der das Zertifikat ablehnt, ist kein Fehler des Servers)."""
        if self._tls_failed:
            return
        super().handle()

    # Eine Keep-Alive-Verbindung, auf der bis zur Zeitgrenze keine weitere Anfrage kommt, ist der Normalfall (jeder Browser lässt
    # Verbindungen offen): Sie wird still geschlossen. Protokolliert wird eine Zeitüberschreitung nur, wenn eine Anfrage begonnen hatte.
    #
    # Ließ sich die Antwort nicht zustellen (der Client ist weg oder holt sie nicht ab), gibt es nichts mehr zu senden: Die breiten
    # ``except``-Zweige reichen den Fehler dann weiter, statt eine 500 zu versuchen (``if self.wfile.failed: raise``), und hier endet er
    # mit EINER Logzeile – bei einer Zeitüberschreitung schreibt sie die Standardbibliothek.
    def handle_one_request(self):
        self._request_begun = False
        self._request_parsed = False
        try:
            super().handle_one_request()
        except ConnectionError as exc:
            if not self.wfile.failed:
                raise
            self.close_connection = True
            self.log_error("Antwort nicht zugestellt (%s)", type(exc).__name__)
            return
        if self._request_parsed and not self.close_connection:
            self._settle_body()

    def parse_request(self):
        self._request_begun = True
        parsed = super().parse_request()
        if parsed:
            self._request_parsed = True
            self.rfile.count = 0                    # ab hier zählt der Body
        return parsed

    def _settle_body(self):
        """Nach der Antwort: Was vom erklärten Body der Anfrage ungelesen blieb, darf auf einer Keep-Alive-Verbindung nie als nächste
        Anfrage gelesen werden – gleich, welcher Zweig geantwortet hat (eine Anfrage mit Body, wo keiner erwartet wird; eine frühe
        Ablehnung). Ein Rest bis ``MAX_DRAIN_BODY`` wird weggelesen, mit kurzer Frist (``DRAIN_TIMEOUT``): Kommt er ganz, bleibt die
        Verbindung benutzbar. In jedem anderen Fall – größerer Rest, ``Transfer-Encoding``, keine gültige Länge, der Rest kommt
        nicht – wird sie geschlossen."""
        if self.headers.get('Transfer-Encoding') is not None:
            self.close_connection = True
            return
        if self.headers.get('Content-Length') is None:
            return
        length = self._declared_length()
        if length is None:
            self.close_connection = True
            return
        rest = length - self.rfile.count
        if rest <= 0:
            return
        if rest > MAX_DRAIN_BODY:
            self.close_connection = True
            return
        drained = False
        try:
            previous = self.connection.gettimeout()
            self.connection.settimeout(DRAIN_TIMEOUT)
            try:
                drained = len(self.rfile.read(rest)) == rest
            finally:
                self.connection.settimeout(previous)
        except OSError:                             # auch TimeoutError: der Rest kam nicht
            drained = False
        if not drained:
            self.close_connection = True

    def log_error(self, format, *args):
        if format.startswith('Request timed out') and not getattr(self, '_request_begun', True):
            return
        super().log_error(format, *args)

    def end_headers(self):
        # Jede Antwort trägt genau EINEN Cache-Control, entschieden an der Route (die Adresse ohne Query), nie an einer Endung: Hat der Handler
        # selbst einen geschrieben (``serve_static_file``, ``respond(…, headers=…)``), bleibt es dabei; sonst gilt ``no-store`` für die
        # Sicherungen, die Dateien der Belege und die Suchen, für alles andere ``no-cache``.
        try:
            if not getattr(self, '_cache_control_sent', False):
                route = getattr(self, 'path', '').split('?', 1)[0]      # ``path`` fehlt bei einer kaputten Anfragezeile
                if route in ('/backup/download', '/receipts/file') or route in _SEARCH_PATHS:
                    self.send_header('Cache-Control', 'no-store')       # der ganze Datenbestand, ein Beleg bzw. die Treffer einer Suche: nirgends ablegen
                else:
                    self.send_header('Cache-Control', 'no-cache')       # Seiten und Dokumente: ablegen erlaubt, aber vor jeder Nutzung nachfragen
            # Für ALLE Antworten: kein MIME-Sniffing und kein Einrahmen durch fremde Seiten
            # (Clickjacking, etwa auf „Status setzen“ bei automatischer Anmeldung). Die App rahmt
            # sich nicht selbst ein – PDFs öffnen sich als eigene Seite in einem neuen Tab –, daher
            # DENY statt SAMEORIGIN.
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')

            super().end_headers()                                       # Call the Base
        finally:
            # Eine Keep-Alive-Verbindung beantwortet viele Anfragen mit dieser Instanz: Das Merkmal gilt nur für die laufende Antwort
            self._cache_control_sent = False

    def send_error(self, code, message=None, explain=None):
        # Kaputte Anfragezeile: Bis dahin gilt die Anfrage als HTTP/0.9, und je nach Patch-Stand
        # der Standardbibliothek ginge die Fehlerantwort dann ohne Statuszeile und ohne Header
        # hinaus – also auch ohne die Schutz-Header aus end_headers. Fehler bekommen immer beides.
        if getattr(self, 'request_version', '') == 'HTTP/0.9':
            self.request_version = ''
        super().send_error(code, message, explain)

    def send_header(self, keyword, value):
        # Letzte Sicherung gegen Header-Injection, egal woher ein Wert stammt (Query,
        # Cookie, Formular, Dateiname): CR/LF/NUL kommen nie in einen Header.
        keyword, value = _header_safe(keyword), _header_safe(value)
        super().send_header(keyword, value)
        if keyword.lower() == 'cache-control':
            self._cache_control_sent = True                             # ``end_headers`` schreibt dann keinen zweiten dazu

    # ── Anfragekörper ────────────────────────────────────────────────────────
    def _declared_length(self):
        """Länge des Bodys laut ``Content-Length`` oder ``None``, wenn es keine gültige gibt: Der Header
        fehlt, steht mehrfach da, ist nicht nur aus ASCII-Ziffern gebaut (kein Vorzeichen, kein Exponent, kein Leerraum
        dazwischen) oder hat mehr als 15 Stellen – oder die Anfrage trägt ``Transfer-Encoding``
        (``chunked`` kennt der Server nicht, die Länge stünde nirgends)."""
        if self.headers.get('Transfer-Encoding') is not None:
            return None
        declared = self.headers.get_all('Content-Length') or []
        if len(declared) != 1:        # fehlt – oder steht mehrfach da: Welche Länge gälte, wäre Auslegungssache
            return None
        raw = declared[0].strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 15:
            return None
        return int(raw)

    def _refuse_body(self, status, text, as_json=False):
        """Eine Anfrage ablehnen, ohne ihren Body zu lesen. Die Verbindung wird danach geschlossen
        (``Connection: close``): Sonst läse der Server die ungelesenen Bytes als nächste Anfrage."""
        self.close_connection = True
        if as_json:
            self._respond_json(status, {'success': False, 'error': text},
                               headers={"Connection": "close"})
        else:
            self.respond(status, f"<h1>{status}</h1><p>{text}</p>", headers={"Connection": "close"})

    def _check_body_size(self, limit, as_json=False):
        """Hat die Anfrage eine gültige Länge von höchstens ``limit`` Bytes? Sonst ist die Antwort schon
        gesendet (400 bei fehlender oder ungültiger Länge, 413 bei zu großer) und der Rückgabewert
        ``False``; der Body bleibt ungelesen."""
        length = self._declared_length()
        if length is None:
            self._refuse_body(400, 'Ungültige Anfrage.', as_json)
            return False
        if length > limit:
            self._refuse_body(413, f'Die Anfrage ist zu groß (höchstens {_size_text(limit)}).', as_json)
            return False
        return True

    def _read_body(self, limit, as_json=False):
        """Body der Anfrage als Bytes, höchstens ``limit`` Bytes lang. ``None``, wenn schon geantwortet wurde: ``_check_body_size``
        (400/413), es kamen weniger Bytes an, als die Länge versprach (400), oder der Client schickt den Rest nicht mehr und die
        Zeitgrenze der Verbindung läuft ab (408; die Verbindung wird danach geschlossen)."""
        if not self._check_body_size(limit, as_json):
            return None
        length = self._declared_length()
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            self._refuse_body(408, 'Die Anfrage wurde nicht vollständig gesendet.', as_json)
            return None
        if len(body) < length:
            self._refuse_body(400, 'Ungültige Anfrage.', as_json)
            return None
        return body

    def _decode_form(self, raw):
        """Body eines Formulars als Text. Kein UTF-8: 400, Rückgabe ``None`` (der Body ist gelesen, die
        Verbindung bleibt benutzbar)."""
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            self.respond(400, "<h1>400</h1><p>Ungültige Anfrage.</p>")
            return None

    def _query(self):
        """Query-String der Anfrage als ``{Name: [Werte]}`` – leer ohne ``?`` (und ohne ``#…``)."""
        return parse_qs(urlsplit(self.path).query)

    def _drain_body(self):
        """Ungelesenen Request-Body verwerfen (verhindert Keep-Alive-Desync bei
        POST-Redirects ohne vorherige Body-Verarbeitung). Was sich nicht sicher weglesen lässt – keine
        gültige Länge, ``chunked`` oder mehr als ``MAX_DRAIN_BODY`` –, bleibt liegen, und die Verbindung
        wird nach der Antwort geschlossen. Bei einer Anfrage ganz ohne Body (die meisten GET) gibt es
        nichts zu tun."""
        if self.headers.get('Content-Length') is None and self.headers.get('Transfer-Encoding') is None:
            return
        length = self._declared_length()
        if length is None or length > MAX_DRAIN_BODY:
            self.close_connection = True
            return
        try:
            if length:
                self.rfile.read(length)
        except Exception:
            self.close_connection = True          # halb weggelesen: der Rest darf nie als nächste Anfrage gelten

    # ── Authentifizierung (TODO #4) ──────────────────────────────────────────
    def _maybe_db(self):
        """Database() nur erzeugen, wenn sie auch genutzt werden darf: ``None``, solange
        die Installation nicht eingerichtet ist oder kein Benutzer gesetzt ist (kein
        verwaistes data/buch.db); sonst die Datenbank des Benutzers."""
        if userctx.get_mode() is None or userctx.get_user() is None:
            return None
        return Database()

    def _open_db(self):
        """Wie ``_maybe_db``; stammt die Datenbank von einer neueren Programmversion,
        wird das dem Nutzer gesagt (Antwort gesendet, Rückgabe ``False``)."""
        try:
            return self._maybe_db()
        except SchemaTooNewError as e:
            print(f"FEHLER: {e}")
            self._drain_body()
            self.respond(500, "<h1>Datenbank ist neuer als das Programm</h1>"
                              "<p>Diese Datenbank wurde von einer neueren Version von "
                              "uBuchhaltung angelegt oder geändert. Sie wurde nicht geöffnet "
                              "und nicht verändert. Bitte die neuere Programmversion verwenden; "
                              "Einzelheiten stehen im Server-Log.</p>")
            return False
        except MigrationBackupError as e:
            print(f"FEHLER: {e}")
            self._drain_body()
            self.respond(500, "<h1>Sicherung vor der Umstellung gescheitert</h1>"
                              "<p>Die Datenbank muss auf eine neuere Programmversion umgestellt "
                              "werden. Vorher legt das Programm eine Sicherung an – und die ist "
                              "gescheitert. Die Datenbank wurde nicht geöffnet und nicht "
                              "verändert. Bitte Schreibrechte und freien Speicherplatz im Ordner "
                              "der Datenbank prüfen; Einzelheiten stehen im Server-Log.</p>")
            return False

    def _session_token(self):
        raw = self.headers.get('Cookie')
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get('session')
        return morsel.value if morsel else None

    def _csrf_key(self):
        """Woran der CSRF-Token gebunden ist: an die Sitzung – bei automatischer
        Anmeldung (keine Sitzung) an einen festen Schlüssel je Benutzer."""
        auto = getattr(self, '_auto_user', None)
        return f"auto-login:{auto}" if auto else self._session_token()

    @staticmethod
    def _session_cookie_header(token):
        parts = [f"session={token}", "HttpOnly", "SameSite=Strict", "Path=/"]
        if userctx.tls_enabled():
            parts.append("Secure")
        return "; ".join(parts)

    @staticmethod
    def _logout_cookie_header():
        parts = ["session=", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=0"]
        if userctx.tls_enabled():
            parts.append("Secure")
        return "; ".join(parts)

    def _require_admin(self):
        """True, wenn der angemeldete Nutzer Administrator ist; sonst wird bereits
        eine Antwort (404/403) gesendet."""
        if not userctx.auth_enabled():
            self.respond(404, "Seite nicht gefunden.")
            return False
        user = userctx.get_user()
        if not (user and auth.is_admin(user)):
            self.respond(403, "<h1>403</h1><p>Kein Zugriff – Administratorrechte erforderlich.</p>")
            return False
        return True

    def _check_csrf(self, post_data=None):
        """CSRF-Token prüfen (zusätzlich zu SameSite=Strict).

        Token kommt aus dem Formularfeld ``csrf`` (post_data) oder dem Header
        ``X-CSRF-Token`` (fetch-Aufrufe, siehe Footer-JS). Er ist an die Session
        (bzw. ``_csrf_key``) gebunden. Ausgenommen ist die Ersteinrichtung (noch kein
        Administrator, also noch keine Sitzung): dort schützen die Herkunfts- und die
        Hostnamen-Prüfung des Türstehers.
        """
        if not userctx.auth_enabled() or getattr(self, '_needs_setup', False):
            return True
        token = post_data.get('csrf', [''])[0] if post_data else ''
        if not token:
            token = self.headers.get('X-CSRF-Token', '')
        if not auth.check_csrf(self._csrf_key(), token):
            if post_data is None:
                # Header-Check-Pfad: Body wurde noch nicht gelesen → verwerfen
                # (mit post_data ist er bereits konsumiert; erneutes read blockiert)
                self._drain_body()
            self.respond(403, "<h1>403</h1><p>Ungültiges oder fehlendes Sicherheits-Token.</p>")
            return False
        return True

    def _gate(self):
        """Auth-Türsteher. Setzt den angemeldeten Benutzer in den Request-Kontext.

        Returns True, wenn die Anfrage weiterverarbeitet werden darf; False, wenn
        bereits eine (Redirect-)Antwort gesendet wurde.
        """
        # Keep-Alive: derselbe Thread bedient mehrere Requests einer Verbindung –
        # daher den Kontext zu Beginn JEDES Requests leeren (kein User-Leak).
        userctx.clear()
        # HTTPS gilt, wenn der Server selbst TLS spricht oder ein Reverse-Proxy es meldet: Auch hinter einem
        # TLS-Proxy soll das Sitzungs-Cookie Secure tragen.
        userctx.set_tls(getattr(self.server, 'tls', False) or forwarded_https(self.headers))
        # Schreibende Anfragen nur von Seiten der App selbst – immer, zusätzlich zum
        # CSRF-Token.
        if self.command not in ('GET', 'HEAD') and not request_is_same_origin(self.headers):
            self._drain_body()
            self.respond(403, "<h1>403</h1><p>Anfrage von fremder Herkunft abgelehnt.</p>")
            return False
        self._auto_user = None           # Handler-Objekt lebt über Keep-Alive-Requests hinweg
        mode = userctx.get_mode()
        # Demo-Anmeldung hat Vorrang vor der automatischen Anmeldung eines Administrators;
        # `demo` ist kein Administrator (keine Benutzerverwaltung, keine SQL-Konsole).
        demo_user = demo.active_user() if mode == 'multi' else None
        if demo_user and (not auth.user_exists(demo_user) or auth.is_admin(demo_user)):
            demo_user = None                 # Demo-Besucher sind nie Administrator
        admin_exists = auth.admin_exists()
        if mode is None and admin_exists:
            # Es gibt Administratoren, aber config.json fehlt oder ist unlesbar (von Hand
            # bearbeitet, beschädigt). Das ist keine Ersteinrichtung – sonst könnte sich
            # hier jeder einen weiteren Administrator anlegen.
            self._drain_body()
            self.respond(503, "<h1>Konfiguration fehlt oder ist beschädigt</h1>"
                              "<p>Die Datei <code>config.json</code> im Datenverzeichnis fehlt oder "
                              "ist nicht lesbar, obwohl diese Installation bereits eingerichtet ist. "
                              "Bitte die Datei wiederherstellen, z. B. mit dem Inhalt "
                              "<code>{\"mode\": \"multi\"}</code> (siehe DEPLOYMENT.md).</p>")
            return False
        has_admin = mode == 'multi' and admin_exists
        auto = demo_user or (auth.auto_login_user() if has_admin else None)
        # Ersteinrichtung: solange es keinen Administrator gibt (und keine Demo läuft)
        needs_setup = mode is None or (mode == 'multi' and not has_admin and not demo_user)
        self._needs_setup = needs_setup  # für do_GET (/setup) und _check_csrf
        # Wo niemand ein Passwort eingeben muss (Ersteinrichtung, automatische
        # Anmeldung), schützt nur der Hostname vor DNS-Rebinding; mit Anmeldung hängt
        # der Zugriff an der Sitzung des echten Hostnamens.
        if (needs_setup or auto) and not host_is_trusted(self.headers.get('Host')):
            self._drain_body()
            self.respond(403, "<h1>403</h1><p>Dieser Hostname ist nicht freigegeben. "
                              "Zusätzliche Namen lassen sich über die Umgebungsvariable "
                              "<code>UBUCHHALTUNG_ALLOWED_HOSTS</code> erlauben "
                              "(siehe DEPLOYMENT.md).</p>")
            return False
        path = self.path.split('?', 1)[0]
        if path in ('/buch.css', '/favicon.ico'):
            return True
        if needs_setup:
            # Noch kein Administrator: ausschließlich die Ersteinrichtung zulassen –
            # sonst entstünde ein verwaistes data/buch.db, bevor der Administrator
            # angelegt ist.
            if path in ('/setup', '/setup/start'):
                return True
            self._drain_body()
            self.respond(303, "", headers={"Location": "/setup"})
            return False
        # Ab hier ist die Installation eingerichtet (mode == 'multi').
        if auto:
            # Automatische Anmeldung: jede Anfrage läuft ohne Sitzung als dieser
            # Benutzer. Anmeldeseite und andere Benutzer sind so lange abgeschaltet.
            self._auto_user = auto
            userctx.set_user(auto)
            userctx.set_auto_login_active(True)
            userctx.set_csrf_token(auth.csrf_for(self._csrf_key()))
            if path in ('/login', '/logout', '/setup-admin'):
                self._drain_body()
                self.respond(303, "", headers={"Location": "/"})
                return False
            return True
        user = auth.get_session_user(self._session_token())
        if user:
            userctx.set_user(user)
            userctx.set_csrf_token(auth.csrf_for(self._csrf_key()))
            return True
        if path in ('/login', '/logout'):
            return True                                  # Anmeldeseite / Logout
        self._drain_body()
        self.respond(303, "", headers={"Location": "/login"})
        return False

    def _run_locked(self, dispatch):
        """Die Anfrage unter der Lesesperre des Datenverzeichnisses bearbeiten (``dirlock``): Während eine Wiederherstellung läuft,
        antwortet der Server für diesen Benutzer mit 503 statt aus halb ersetzten Dateien. Ohne angemeldeten Benutzer gibt es kein
        Datenverzeichnis (Anmeldung, Ersteinrichtung, statische Dateien); ``_LOCK_FREE_ROUTES`` gelten nur für GET – eine POST-Anfrage
        an so eine Adresse öffnet die Datenbank des Benutzers und gehört unter die Sperre.

        ``_gate()`` läuft davor: Es bestimmt den Benutzer und braucht das Datenverzeichnis nicht. Die 503 entsteht nur beim Betreten
        der Sperre, nie aus einer ``DirBusy``, die ``dispatch`` selbst wirft."""
        route = self.path.split('?', 1)[0]
        if userctx.get_user() is None or (self.command == 'GET' and route in _LOCK_FREE_ROUTES):
            dispatch()
            return
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(dirlock.shared(userctx.user_data_dir()))
            except dirlock.DirBusy:
                self._refuse_restoring(route)
                return
            dispatch()

    def _refuse_restoring(self, route):
        """503 während einer Wiederherstellung: kurzer Hinweis mit ``Retry-After`` – als JSON, wo die Seite JSON erwartet
        (``_JSON_ANSWER_ROUTES``), sonst als Klartext. Der Body einer POST-Anfrage bleibt ungelesen, die Verbindung wird danach
        geschlossen (wie bei ``_refuse_body``); danach ist der Kontext der Anfrage geleert."""
        headers = {"Retry-After": str(dirlock.RETRY_AFTER_SECONDS)}
        if self.command == 'POST':
            self.close_connection = True
            headers["Connection"] = "close"
        else:
            self._drain_body()
        if route in _JSON_ANSWER_ROUTES:
            text = dirlock.RESTORE_RUNNING
            self._respond_json(503, {'success': False, 'ok': False, 'error': text, 'errors': [text]}, headers=headers)
        else:
            self.respond(503, dirlock.RESTORE_RUNNING, headers=headers, content_type="text/plain; charset=utf-8")
        userctx.clear()

    def _respond_post_only(self):
        """405 für eine Aktion, die es nur per POST gibt."""
        self.respond(405, "<h1>405</h1><p>Diese Aktion ist nur per POST möglich.</p>",
                     headers={"Allow": "POST"})

    def _auth_routes_get(self):
        """Behandelt GET /login, /logout (405: Abmelden gibt es nur per POST) und /setup-admin
        (alte Lesezeichen: Weiterleitung auf die Ersteinrichtung /setup). True, wenn erledigt."""
        if not userctx.auth_enabled():
            return False                                 # Login-UI erst nach der Ersteinrichtung
        path = self.path.split('?', 1)[0]
        if path == '/login':
            self.respond(200, PageLogin())
            return True
        if path == '/setup-admin':
            self.respond(303, "", headers={"Location": "/setup"})
            return True
        if path == '/logout':
            self._respond_post_only()
            return True
        return False

    def _auth_routes_post(self):
        """Behandelt POST /login und POST /logout. True, wenn erledigt."""
        if not userctx.auth_enabled():
            return False                                 # Login-UI erst nach der Ersteinrichtung
        path = self.path.split('?', 1)[0]
        if path not in ('/login', '/logout'):
            return False
        raw = self._read_body(MAX_PREAUTH_BODY)
        if raw is None:
            return True
        post_body = self._decode_form(raw)
        if post_body is None:
            return True
        post_data = parse_qs(post_body)
        if path == '/logout':
            # Mit gültiger Sitzung nur mit CSRF-Token. Ohne (abgelaufen, anderes Fenster) gibt
            # es nichts zu schützen und kein Token zu prüfen: nur zur Anmeldeseite.
            if userctx.get_user() and not self._check_csrf(post_data):
                return True
            auth.delete_session(self._session_token())
            self.respond(303, "", headers={"Location": "/login",
                                            "Set-Cookie": self._logout_cookie_header()})
            return True
        outcome = handlers.handle_login(post_data, client=self.client_address[0])
        if outcome.ok:
            token = auth.create_session(outcome.username)
            self.respond(303, "", headers={"Location": "/",
                         "Set-Cookie": self._session_cookie_header(token)})
        else:
            headers = {"Retry-After": str(outcome.retry_after)} if outcome.retry_after else None
            self.respond(outcome.status, PageLogin(error_msg=outcome.error, username=outcome.username),
                         headers=headers)
        return True

    def _send_invoice_document(self, invoice_id, kind, content_type,
                               disposition='attachment', doc_type='invoice'):
        """Abgelegtes Rechnungsdokument ausliefern (PDF/XRechnung; Angebote: PDF).

        Fehlt die Datei oder gehört die ID zu einem Beleg der anderen Art (``doc_type``:
        ``'invoice'`` oder ``'quote'``), geht eine kurze Klartext-Meldung mit 404 zurück, die
        das Frontend direkt anzeigen kann (todo #3). ``X-Filename`` trägt den
        Dateinamen prozentkodiert, damit der Browser-seitige Blob-Download ihn
        auch mit Umlauten übernehmen kann (todo #1).
        """
        data, filename, error = handlers.handle_invoice_document_download(
            invoice_id, kind, doc_type)
        self._send_document(data, filename, error, content_type, disposition)

    def _send_version_document(self, qs):
        """Eine abgelegte Fassung (``/invoice/version_download?id=…&file=…&year=…``) ausliefern – nur lesend. Die
        Antwort für ein unbekanntes, fremdes oder ungültiges Ziel ist immer dieselbe: 404 mit Klartext."""
        invoice_id = _int_param(qs, 'id')
        filename = qs.get('file', [None])[0]
        year = qs.get('year', [None])[0]
        if invoice_id is None or filename is None:
            self._send_document(None, None, 'Diese Fassung gibt es nicht.', '', 'attachment')
            return
        data, name, error = handlers.handle_invoice_version_download(invoice_id, filename, year)
        content_type = 'application/xml' if (name or '').lower().endswith('.xml') else 'application/pdf'
        self._send_document(data, name, error, content_type, 'attachment')

    def _send_document(self, data, filename, error, content_type, disposition):
        """Ein Dokument als Antwort: bei ``data is None`` 404 mit dem Klartext ``error``, sonst die Bytes
        mit dem Dateinamen im Kopf (``X-Filename``, prozentkodiert)."""
        if data is None:
            body = (error or 'Dokument nicht gefunden.').encode('utf-8')
            self.send_response(404)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Disposition", content_disposition(disposition, filename))
        self.send_header("X-Filename", quote(filename, safe=''))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_backup_download(self):
        """Eine Sicherung des angemeldeten Benutzers ausliefern (``GET /backup/download?file=<Name>``) – nur lesend.

        Die Datei kann Gigabyte groß sein: Sie geht in Stücken aus dem offenen Deskriptor hinaus (``backup.copy_to``), nie ganz
        durch den Speicher, mit ``Content-Disposition: attachment`` und ihrer Größe als ``Content-Length`` (ein ``Range``-Kopf wird
        überhört: immer die ganze Datei). Welche Dateien es gibt, entscheidet allein ``backup.downloadable_backups``; ein unbekannter,
        fremder oder ungültiger Name ist immer dieselbe Antwort (404, ohne Pfad). Die Route läuft vor dem Öffnen der Datenbank
        (``do_GET``): Sicherungen lassen sich auch holen, wenn sich die Datenbank nicht öffnen lässt.

        Nach den Köpfen darf nichts mehr in den allgemeinen 500-Zweig fallen – er schriebe eine zweite Antwort in den laufenden
        Body: Ein Fehler beim Senden endet mit geschlossener Verbindung, der Client sieht eine unvollständige Antwort."""
        plain = "text/plain; charset=utf-8"
        if not request_is_same_origin(self.headers):
            self.respond(403, backup.DOWNLOAD_FOREIGN, content_type=plain)
            return
        names = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get('file', [])
        try:
            if userctx.get_user() is None or len(names) != 1:
                raise backup.BackupNotFound(backup.DOWNLOAD_NOT_FOUND)
            source, entry = backup.open_download(userctx.user_data_dir(), names[0])
        except backup.BackupNotFound:
            self.respond(404, backup.DOWNLOAD_NOT_FOUND, content_type=plain)
            return
        except backup.BackupRefused as refused:
            self.respond(409, str(refused), content_type=plain)
            return
        except Exception:
            import traceback
            traceback.print_exc()
            self.respond(500, "Die Sicherung konnte nicht geöffnet werden – Einzelheiten stehen im Server-Log.",
                         content_type=plain)
            return
        with source:
            previous_timeout = self.connection.gettimeout()
            try:
                # je gesendetem Stück: Ein Client, der nicht mehr liest, hält Verbindung und Datei sonst unbegrenzt
                self.connection.settimeout(backup.DOWNLOAD_SEND_TIMEOUT)
                self.send_response(200)
                self.send_header("Content-type", "application/octet-stream")
                self.send_header("Content-Disposition", content_disposition("attachment", entry.name))
                self.send_header("Content-Length", str(entry.size))
                self.end_headers()
                sent = backup.copy_to(source, entry.size, self.wfile.write)
            except (ConnectionError, TimeoutError):
                self.close_connection = True                    # der Client hat abgebrochen
                print(f"Download abgebrochen: {entry.name!r}", file=sys.stderr)
                return
            except OSError:
                import traceback
                traceback.print_exc()
                self.close_connection = True
                return
            self.connection.settimeout(previous_timeout)
            if sent < entry.size:
                self.close_connection = True                    # die Datei ist während des Sendens kürzer geworden
                print(f"Download unvollständig ({sent} von {entry.size} Bytes): {entry.name!r}", file=sys.stderr)

    def _send_receipt_file(self, db):
        """Die Datei eines Belegs ausliefern (``GET /receipts/file?id=<n>[&inline=1]``) – nur lesend.

        Welche Datei es gibt, entscheidet allein ``receipt_files.open_receipt`` (``stored_receipt_path``: nur innerhalb von
        ``Belege/`` und ``Documents/`` des angemeldeten Benutzers; ein Link, ein Ordner oder eine Pipe ist keine Datei). Ein
        unbekannter, ungültiger oder fremder Beleg und ein Pfad außerhalb sind je dieselbe Antwort: 404 mit festem Satz, nie mit
        einem Pfad (der abgelehnte Pfad steht nur im Server-Log). Die Herkunft prüft die Route wie ``/backup/download``
        (``request_is_same_origin``: eine fremde ``Sec-Fetch-Site`` ergibt 403).

        Die Datei geht in Stücken aus dem offenen Deskriptor hinaus (``backup.copy_to``, mit der Zeitgrenze je Stück wie bei der
        Sicherung), nie ganz durch den Speicher. Angezeigt wird sie nur mit ``inline=1`` UND wenn Endung und Anfangsbytes zur
        Positivliste passen (``receipt_files.inline_type``: PDF und Rasterbilder) – dann ``Content-Disposition: inline`` mit dem
        Typ der Positivliste; alles andere ist immer ``attachment`` mit ``application/octet-stream`` (ein HTML oder SVG liefe
        sonst als Skript im Ursprung der App).

        Nach den Köpfen darf nichts mehr in den allgemeinen 500-Zweig fallen – er schriebe eine zweite Antwort in den laufenden
        Body: Ein Fehler beim Senden endet mit geschlossener Verbindung."""
        plain = "text/plain; charset=utf-8"
        if not request_is_same_origin(self.headers):
            self.respond(403, receipt_files.FOREIGN, content_type=plain)
            return
        qs = self._query()
        receipt_id = _id_param(qs)
        row = db.get_receipt_by_id(receipt_id)
        if row is None:
            self.respond(404, receipt_files.NOT_FOUND, content_type=plain)
            return
        try:
            opened = receipt_files.open_receipt(row[4])
        except Exception:
            import traceback
            traceback.print_exc()
            self.respond(500, receipt_files.OPEN_FAILED, content_type=plain)
            return
        if opened is None:
            self.respond(404, receipt_files.NO_FILE, content_type=plain)
            return
        if qs.get('inline', [''])[0] == '1' and opened.inline:
            content_type, disposition = opened.inline, 'inline'
        else:
            content_type, disposition = 'application/octet-stream', 'attachment'
        with opened.source as source:
            name = str(row[3] or '').strip() or opened.name
            disposition_header, filename_header = content_disposition(disposition, name), quote(name, safe='')
            previous_timeout = self.connection.gettimeout()
            try:
                # je gesendetem Stück: Ein Client, der nicht mehr liest, hält Verbindung und Datei sonst unbegrenzt
                self.connection.settimeout(backup.DOWNLOAD_SEND_TIMEOUT)
                self.send_response(200)
                self.send_header("Content-type", content_type)
                self.send_header("Content-Disposition", disposition_header)
                self.send_header("X-Filename", filename_header)
                self.send_header("Content-Length", str(opened.size))
                self.end_headers()
                sent = backup.copy_to(source, opened.size, self.wfile.write)
            except (ConnectionError, TimeoutError):
                self.close_connection = True                    # der Client hat abgebrochen
                print(f"Download abgebrochen: Beleg {receipt_id}", file=sys.stderr)
                return
            except OSError:
                import traceback
                traceback.print_exc()
                self.close_connection = True
                return
            self.connection.settimeout(previous_timeout)
            if sent < opened.size:
                self.close_connection = True                    # die Datei ist während des Sendens kürzer geworden
                print(f"Download unvollständig ({sent} von {opened.size} Bytes): Beleg {receipt_id}", file=sys.stderr)

    def _respond_after_delete(self, delete, target, list_path, error_param):
        """303 auf die Listenseite ``list_path`` nach ``delete(target)``. Lehnt das Löschen ab (``DeleteRefused``: andere
        Daten verweisen auf den Datensatz), geht die Meldung als Query-Parameter ``error_param`` mit – die Seite zeigt
        sie nur als JSON im Skriptblock über ``appMsg`` – und nichts ist gelöscht."""
        location = list_path
        try:
            delete(target)
        except DeleteRefused as exc:
            location = f"{list_path}?{error_param}={quote(str(exc), safe='')}"
        self.respond(303, "", headers={"Location": location})

    def _respond_form_result(self, result, location=None):
        """Antwort auf das Ergebnis eines Formular-Handlers ``(Status, Text)``. Bei Erfolg und bei einer Meldung auf der Seite
        (303) ist der Text die Adresse der Weiterleitung – ``location`` ersetzt sie, wo die Route sie festlegt. Eine Ablehnung
        (ab 400: ungültige Anfrage, nichts gespeichert; 500: Fehler ohne Fehlertext) ist der Klartext im Body – sonst stünde
        er im ``Location``-Kopf, und der Browser zeigte eine leere Seite."""
        status_code, text = result
        if status_code >= 400:
            self.respond(status_code, text, content_type="text/plain; charset=utf-8")
        else:
            self.respond(status_code, "", headers={"Location": text if location is None else location})

    def _post_action_routes(self, db):
        """Aktionen mit Seiteneffekt (Löschen, Umschalten, Lösen). Parameter stehen
        in der Query, der Body trägt nur den CSRF-Token. Returns True, wenn die
        Anfrage hier beantwortet wurde."""
        path = self.path.split('?', 1)[0]
        if path not in _ACTION_PATHS:
            return False
        qs = self._query()
        # Pflichtparameter der Aktion: Fehlt einer oder ist er keine Ganzzahl,
        # kommt 400 – wie bei /invoice/delete, nicht KeyError/ValueError und damit 500.
        item_id = _int_param(qs, "id")
        if path == "/masterdata/skr/add_standard":
            invalid = False                         # die Aktion nennt keine ID
        else:
            invalid = item_id is None
        if invalid:
            self.respond(400, "Ungültige oder fehlende ID.", content_type="text/plain; charset=utf-8")
            return True
        if path == "/masterdata/articles/delete":
            self._respond_after_delete(db.delete_article, item_id, "/masterdata/articles", "error")
        elif path == "/masterdata/contacts/delete":
            self._respond_after_delete(db.delete_contact, item_id, "/masterdata/contacts", "error")
        elif path == "/masterdata/skr/delete":
            status_code, location = handlers.handle_delete_skr(db, item_id)
            self.respond(status_code, "", headers={"Location": location})
        elif path == "/masterdata/skr/togglemenu":
            status_code, location = handlers.handle_toggle_skr_menu(db, item_id)
            self.respond(status_code, "", headers={"Location": location})
        elif path == "/masterdata/skr/add_standard":
            added = db.add_missing_standard_accounts()
            self.respond(303, "", headers={"Location": f"/masterdata/skr?added={added}"})
        elif path == "/masterdata/bankaccounts/delete":
            self._respond_after_delete(db.delete_account, item_id, "/masterdata/bankaccounts", "error")
        elif path == "/receipts/delete":
            self._respond_after_delete(lambda receipt: handlers.delete_receipt_with_file(db, receipt),
                                       item_id, "/receipts", "err")
        elif path == "/transactions/delete":
            status_code, location = handlers.handle_delete_transaction(db, item_id)
            self.respond(status_code, "", headers={"Location": location})
        elif path == "/masterdata/numberranges/delete":
            db.delete_number_range(item_id)
            self.respond(303, "", headers={"Location": "/masterdata/numberranges"})
        elif path == "/assets/delete":
            try:
                db.delete_asset(item_id)
                location = "/assets"
            except ValueError as exc:      # gebuchte AfA oder Unteranlagen: nicht löschen
                location = f"/assets/view?id={item_id}&err={quote(str(exc), safe='')}"
            self.respond(303, "", headers={"Location": location})
        elif path == "/asset_categories/delete":
            self._respond_after_delete(db.delete_asset_category, item_id, "/asset_categories", "err")
        elif path == "/worktime/delete":
            db.delete_worktime(item_id)
            person_id = qs['person'][0] if qs.get('person') else ''
            date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
            customer = _customer_param(qs) or CUSTOMER_ALL      # ein gültiger Kundenfilter bleibt erhalten
            self.respond(303, "", headers={"Location":
                f"/worktime?person={quote(str(person_id), safe='')}&from={quote(date_from, safe='')}&to={quote(date_to, safe='')}"
                f"{customer_query(customer)}"})
        elif path == "/trips/delete":
            db.delete_trip(item_id)
            person_id = qs['person'][0] if qs.get('person') else ''
            date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
            self.respond(303, "", headers={"Location":
                f"/trips?person={quote(str(person_id), safe='')}&from={quote(date_from, safe='')}&to={quote(date_to, safe='')}"})
        elif path == "/quote/delete":
            db.delete_quote(item_id)
            self.respond(303, "", headers={"Location": "/quote"})
        elif path == "/invoice/delete":
            status_code, location = handlers.handle_delete_invoice(db, item_id)
            if status_code == 404:
                self.respond(404, location, content_type="text/plain; charset=utf-8")
            else:
                self.respond(status_code, "", headers={"Location": location})
        return True

    def _post_generate_document(self, db, route):
        """POST /invoice/pdf_generate und /quote/pdf_generate: PDF (bei einer Rechnung mit
        E-Rechnungsdaten auch das XML) erzeugen und ablegen; Antwort als JSON. Heruntergeladen
        wird anschließend die abgelegte Datei (``/…/pdf_download``). Jede Route erzeugt nur
        für ihren Belegtyp; die ID eines anderen Belegs ergibt 404 und ändert nichts."""
        self._drain_body()
        qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
        doc_id = _int_param(qs, 'id')
        if doc_id is None:
            self.respond(400, json.dumps({'success': False, 'error': 'Ungültige oder fehlende ID'}),
                         content_type="application/json")
            return
        doc_type = 'quote' if route == "/quote/pdf_generate" else 'invoice'
        if db.get_document(doc_id, doc_type) is None:
            self._respond_json(404, {'success': False, 'error':
                                     'Angebot nicht gefunden.' if doc_type == 'quote'
                                     else 'Rechnung nicht gefunden.'})
            return
        if route == "/invoice/pdf_generate":
            result = handlers.handle_invoice_pdf_generate(doc_id)
            if result.get('pdf_path'):
                payload = {
                    'success': True,
                    'pdf_path': result['pdf_path'],
                    'filename': result['pdf_filename'],
                    'xml_filename': result.get('xml_filename'),
                    'message': 'PDF erfolgreich erstellt',
                }
                if result.get('error'):
                    payload['warning'] = result['error']
                status_code = 200
            else:
                payload = {'success': False,
                           'error': result.get('error') or 'Fehler beim Erstellen der PDF'}
                status_code = 500
        else:
            pdf_bytes, pdf_path = handlers.handle_quote_pdf_generate(doc_id)
            if pdf_bytes and pdf_path:
                payload, status_code = {'success': True, 'pdf_path': pdf_path}, 200
            else:
                payload, status_code = {'success': False,
                                        'error': 'Fehler beim Erstellen der PDF'}, 500
        self.respond(status_code, json.dumps(payload), content_type="application/json")

    def _respond_json(self, status_code, payload, headers=None):
        self.respond(status_code, json.dumps(payload), headers=headers,
                     content_type="application/json")

    def _post_wiso_fdb_upload(self):
        """POST /wiso/fdb-upload?slot=client|standard[&upload=<Kennung>]: die WISO-Datenbank (``DB1.FDB`` im Slot ``client``,
        ``DB0.FDB`` im Slot ``standard``) als roher Body in den Zwischenordner des Benutzers legen
        (``server/wiso_upload.py``). Antwort: JSON ``{"success": true, "upload": <Kennung>}``; der Import (``/wiso/fdb-import``)
        nennt dann nur die Kennung.

        Der Body ist die Datei selbst und wird in Stücken auf die Platte geschrieben, nie im Ganzen gelesen. Was sich ohne
        ihn entscheiden lässt – Token, Demo-Modus, Slot und Kennung, Länge, freier Platz –, kommt vor dem ersten gelesenen
        Byte; eine Ablehnung schließt die Verbindung (``_refuse_body``). ``client`` ohne Kennung legt einen neuen Ordner an
        (und räumt dabei den eines älteren Uploads weg), mit gültiger Kennung ersetzt er die Datei in deren Ordner;
        ``standard`` braucht eine gültige Kennung, in deren Ordner ``DB1.FDB`` schon liegt. Scheitert der Upload, bleibt kein
        in dieser Anfrage angelegter Ordner zurück. Meldungen nennen nie einen Pfad (Einzelheiten stehen im Server-Log)."""
        if not self._check_csrf():
            return
        if demo.is_active():
            self._refuse_body(403, wiso_upload.DEMO_REFUSED, as_json=True)
            return
        qs = self._query()
        slot = qs.get('slot', [''])[0]
        token = qs.get('upload', [''])[0]
        folder = wiso_upload.upload_dir(token)
        if slot == 'client':
            valid = not token or folder is not None
        elif slot == 'standard':
            valid = wiso_upload.files(token)[0] is not None
        else:
            valid = False
        if not valid:
            self._refuse_body(400, 'Ungültige Anfrage.', as_json=True)
            return
        if not self._check_body_size(wiso_upload.MAX_WISO_DB_BODY, as_json=True):
            return
        length = self._declared_length()
        if length == 0:
            self._refuse_body(400, 'Die Datei ist leer.', as_json=True)
            return
        if not wiso_upload.enough_space(length):
            self._refuse_body(507, 'Nicht genug freier Speicherplatz für den Upload.', as_json=True)
            return

        created = folder is None
        try:
            if created:
                token = wiso_upload.begin()
            elif slot == 'client':
                wiso_upload.sweep(keep=token)
            wiso_upload.store(token, slot, self.rfile, length)
        except wiso_upload.UploadIncomplete:
            status_code, text = 400, 'Ungültige Anfrage.'
        except wiso_upload.UploadFailed:
            import traceback
            traceback.print_exc()
            status_code, text = 500, ('Die Datei konnte nicht gespeichert werden – '
                                      'Einzelheiten stehen im Server-Log.')
        except BaseException:
            if created:
                wiso_upload.discard(token)
            raise
        else:
            self._respond_json(200, {'success': True, 'upload': token})
            return
        if created:
            wiso_upload.discard(token)
        self._refuse_body(status_code, text, as_json=True)

    def _post_receipts_upload(self, db):
        """POST /receipts/upload: die Dateien der Belege-Seite (Multipart, Feld ``files``) als Belege ablegen
        (``upload_handler.handle_receipt_upload``: Nummer, Datum, Dateiname, Datei unter ``Belege/<Jahr>/``). Antwort: JSON
        ``{"success": true, "files": [...]}`` mit einem Eintrag je Datei.

        Vor dem ersten gelesenen Byte stehen das CSRF-Token (Header), der Demo-Modus (die Besucher teilen ein Verzeichnis: 403, die
        Verbindung geht zu) und die Größe (``_read_body``: 400/413, höchstens ``MAX_UPLOAD_BODY``). Meldungen nennen nie einen Pfad
        oder den Text einer Ausnahme (Einzelheiten stehen im Server-Log). ``POST /upload_receipts`` bleibt der Import der Seite
        „Buchungen“."""
        if not self._check_csrf():
            return
        if demo.is_active():
            self._refuse_body(403, upload_handler.RECEIPTS_DEMO_REFUSED, as_json=True)
            return
        body = self._read_body(MAX_UPLOAD_BODY, as_json=True)
        if body is None:
            return
        try:
            status_code, payload = upload_handler.handle_receipt_upload(db, self.headers.get('Content-Type') or '', body)
        except Exception:
            if self.wfile.failed:
                raise                               # die Antwort ließ sich nicht zustellen: nichts mehr senden
            import traceback
            traceback.print_exc()
            self._respond_json(500, {'success': False, 'error': 'Interner Fehler – Einzelheiten stehen im Server-Log.'})
            return
        self._respond_json(status_code, payload)

    def _post_json_route(self, path, db):
        """POST-Routen der Rechnungen und Angebote, des Verknüpfens von Belegen, des Neuaufbaus einer Zahlungszuordnung und des Lesens
        eines Belegs, die JSON lesen und JSON antworten (fetch).

        Ein unbrauchbarer Body – fehlende oder ungültige Content-Length, kein JSON-Objekt,
        fehlende Pflichtfelder (``handlers.InvalidRequest``) – wird mit 400 „Ungültige Anfrage.“
        beantwortet, ein zu großer (mehr als ``MAX_FORM_BODY``) mit 413, ein unerwarteter Fehler mit
        500. Der Traceback bleibt im Server-Log; die Verbindung bricht nie ohne Antwort ab."""
        post_body = self._read_body(MAX_FORM_BODY, as_json=True)
        if post_body is None:             # Antwort gesendet, ein etwaiger Body bleibt ungelesen
            return
        try:
            if path in ("/invoice/save", "/invoice/send-email", "/quote/save"):
                handler = {"/invoice/save": handlers.handle_invoice_save,
                           "/invoice/send-email": handlers.handle_send_invoice_email,
                           "/quote/save": handlers.handle_quote_save}[path]
                body = handler(post_body)
                # Das Speichern einer Rechnung meldet eine gesperrte Angabe mit 409 (JsonBody.status)
                status_code, response = getattr(body, 'status', 200), body.decode('utf-8')
            elif path in ("/invoice/link-payment", "/invoice/delete-payment"):
                handler = {"/invoice/link-payment": handlers.handle_link_invoice_payment,
                           "/invoice/delete-payment": handlers.handle_delete_invoice_payment}[path]
                status_code, msg = handler(post_body)
                response = json.dumps({'success': True} if status_code == 200
                                      else {'success': False, 'error': msg})
            elif path in ("/bookings/link-document", "/bookings/unlink-document", "/invoice/rebuild-payments",
                          "/receipts/read"):
                handler = {"/bookings/link-document": handlers.handle_booking_link_document,
                           "/bookings/unlink-document": handlers.handle_booking_unlink_document,
                           "/invoice/rebuild-payments": handlers.handle_invoice_rebuild_payments,
                           "/receipts/read": handlers.handle_receipt_read}[path]
                status_code, response = handler(db, post_body)
            else:                  # /invoice/status, /invoice/cancel, /invoice/reissue, /quote/status
                handler = {"/invoice/status": handlers.handle_update_invoice_status,
                           "/invoice/cancel": handlers.handle_invoice_cancel,
                           "/invoice/reissue": handlers.handle_invoice_reissue,
                           "/quote/status": handlers.handle_update_quote_status}[path]
                status_code, response = handler(post_body)
        except handlers.InvalidRequest:
            self._respond_json(400, {'success': False, 'error': 'Ungültige Anfrage.'})
            return
        except Exception:
            if self.wfile.failed:
                raise                               # die Antwort ließ sich nicht zustellen: nichts mehr senden
            import traceback
            traceback.print_exc()
            self._respond_json(500, {'success': False, 'error':
                                     'Interner Fehler – Einzelheiten stehen im Server-Log.'})
            return
        self.respond(status_code, response, content_type="application/json")

    def do_GET(self):
        if not self._gate():
            return
        self._run_locked(self._dispatch_get)

    def _dispatch_get(self):
        if self._auth_routes_get():
            userctx.clear()
            return
        if self.path.split('?', 1)[0] == "/backup/download":
            # Vor dem Öffnen der Datenbank (sie ist hier nicht nötig) und mit eigener Fehlerbehandlung: Nach den Köpfen
            # darf keine zweite Antwort mehr folgen, siehe _send_backup_download
            try:
                self._send_backup_download()
            finally:
                userctx.clear()
            return
        # Ohne angemeldeten Nutzer keine (globale) DB anlegen – hier sind nur
        # public/statische Routen erreichbar (z. B. /buch.css). Sonst entstünde
        # fälschlich ein data/buch.db.
        db = self._open_db()
        if db is False:
            return
        try:
            route = self.path.split('?', 1)[0]
            if (route in _ACTION_PATHS or route in _GENERATE_PATHS or route in _POST_ONLY_JSON_PATHS
                    or route in _UPLOAD_PATHS):
                self._respond_post_only()
                return
            if self.path == "/" or self.path == "/dashboard" or self.path.startswith("/dashboard?"):
                if db.is_first_run():
                    self.respond(303, "", headers={"Location": "/setup"})
                    return
                qs = self._query()
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                account_ids = _int_params(qs, 'acct') or None
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageDashboard(db, date_from, date_to, account_ids), headers=hdrs)
            elif route == "/bwa":
                # Betriebswirtschaftliche Auswertung: schreibt nichts in den Bestand. Der Zeitraum ist der gemeinsame der Seiten (Adresse >
                # Cookie > laufendes Jahr, das Cookie wie überall), der Monat folgt daraus; nur die Darstellung kommt aus der Adresse
                qs = self._query()
                today = datetime.date.today()
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                year, month = period_month(date_from, date_to, today)
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageBwa(db, BwaQuery(year, month, read_variant(qs)), date_from, date_to, today), headers=hdrs)
            elif route == "/bwa/pdf":
                qs = self._query()
                query, error = read_pdf_query(qs)
                if error:
                    self.respond(400, error, content_type="text/plain; charset=utf-8")
                else:
                    with_accounts = qs.get('accounts', ['0'])[0] in ('1', 'true', 'on')
                    pdf_bytes, filename = handlers.handle_bwa_pdf(db, query, with_accounts, datetime.date.today())
                    if pdf_bytes is None:
                        self.respond(500, PDF_FAILED_TEXT, content_type="text/plain; charset=utf-8")
                    else:
                        self._send_document(pdf_bytes, filename, None, 'application/pdf', 'attachment')
            elif route == "/euer":
                # Einnahmenüberschussrechnung: schreibt nichts in den Bestand. Der Zeitraum ist der gemeinsame der Seiten (Adresse >
                # Cookie > laufendes Jahr, das Cookie wie überall); das heutige Datum bestimmt nur die Jahresknöpfe
                date_from, date_to, set_cookie = resolve_period(self._query(), self.headers.get('Cookie'))
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageEuer(db, EuerQuery(date_from, date_to), datetime.date.today()), headers=hdrs)
            elif route == "/euer/pdf":
                query, error = read_euer_pdf_query(self._query())
                if error:
                    self.respond(400, error, content_type="text/plain; charset=utf-8")
                else:
                    pdf_bytes, filename = handlers.handle_euer_pdf(db, query, datetime.date.today())
                    if pdf_bytes is None:
                        self.respond(500, EUER_PDF_FAILED_TEXT, content_type="text/plain; charset=utf-8")
                    else:
                        self._send_document(pdf_bytes, filename, None, 'application/pdf', 'attachment')
            elif route == "/susa":
                # Summen- und Saldenliste: schreibt nichts in den Bestand (kein Steuerdatum). Der Zeitraum ist der gemeinsame der Seiten
                # (Adresse > Cookie > laufendes Jahr, das Cookie wie überall), der Monat folgt daraus
                today = datetime.date.today()
                date_from, date_to, set_cookie = resolve_period(self._query(), self.headers.get('Cookie'))
                year, month = period_month(date_from, date_to, today)
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageSusa(db, SusaQuery(year, month), date_from, date_to, today), headers=hdrs)
            elif route == "/susa/pdf":
                query, error = read_susa_pdf_query(self._query())
                if error:
                    self.respond(400, error, content_type="text/plain; charset=utf-8")
                else:
                    pdf_bytes, filename = handlers.handle_susa_pdf(db, query, datetime.date.today())
                    if pdf_bytes is None:
                        self.respond(500, SUSA_PDF_FAILED_TEXT, content_type="text/plain; charset=utf-8")
                    else:
                        self._send_document(pdf_bytes, filename, None, 'application/pdf', 'attachment')
            elif route == "/liquiditaet":
                # Liquiditätsrechnung: wie die BWA – gemeinsamer Zeitraum samt Cookie, der Monat folgt daraus, die Darstellung kommt aus der Adresse
                qs = self._query()
                today = datetime.date.today()
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                year, month = period_month(date_from, date_to, today)
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageLiquidity(db, BwaQuery(year, month, read_variant(qs)), date_from, date_to, today), headers=hdrs)
            elif route == "/liquiditaet/pdf":
                qs = self._query()
                query, error = read_pdf_query(qs)
                if error:
                    self.respond(400, error, content_type="text/plain; charset=utf-8")
                else:
                    with_accounts = qs.get('accounts', ['0'])[0] in ('1', 'true', 'on')
                    pdf_bytes, filename = handlers.handle_liquidity_pdf(db, query, with_accounts, datetime.date.today())
                    if pdf_bytes is None:
                        self.respond(500, LIQUIDITY_PDF_FAILED_TEXT, content_type="text/plain; charset=utf-8")
                    else:
                        self._send_document(pdf_bytes, filename, None, 'application/pdf', 'attachment')
            elif self.path == "/setup":
                # Erststart: solange noch kein Administrator angelegt ist, die
                # Ersteinrichtung zeigen; danach das gewohnte Kontaktformular.
                if self._needs_setup:
                    self.respond(200, PageSetupStart())
                else:
                    self.respond(200, PageSetup(db))
            elif self.path == "/about":
                self.respond(200, pages.PageAbout())
            elif self.path == "/invoice" or self.path.startswith("/invoice?"):
                # Parse query parameters for filters
                query_components = self._query()
                # Zeitraum aus Query/Cookie/Default – gilt seitenübergreifend
                date_from, date_to, set_cookie = resolve_period(query_components, self.headers.get('Cookie'))
                filters = {'date_from': date_from, 'date_to': date_to}
                if 'search' in query_components:
                    filters['search'] = query_components['search'][0]
                if 'status' in query_components:
                    filters['status'] = query_components['status'][0]
                invoice_id = _int_param(query_components, 'id')
                # "Als neu kopieren": Formular mit den Daten der Quellrechnung,
                # aber als neue Rechnung (todo #4)
                copy_from = _int_param(query_components, 'copy_from')
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageInvoice(db, filters, invoice_id, copy_from=copy_from,
                                              err=query_components.get('err', [None])[0]),
                             headers=hdrs)
            elif self.path.startswith("/invoice/view") or self.path.startswith("/invoice/edit"):
                query_components = self._query()
                invoice_id = _id_param(query_components)
                self.respond(200, PageInvoice(
                    db, {}, invoice_id,
                    payment_error=query_components.get("payment_error", [None])[0]))
            elif self.path.startswith("/invoice/xml_download"):
                # Abgelegte XRechnung ausliefern – nur herunterladen, nie frisch erzeugen
                # (todo #3); eine Route, die das XML neu baute, gibt es nicht.
                self._send_invoice_document(_id_param(self._query()), 'xml', "application/xml")
                return
            elif self.path.split('?', 1)[0] == "/invoice/version_download":
                # Eine abgelegte Fassung (Ordner Fassungen/<ID>) ausliefern – rein lesend; der Dateiname
                # ist ein reiner Name, gefunden wird er nur unter den Dokumenten dieser Rechnung, mit year
                # in genau diesem Jahresordner (ein leeres year= ist angegeben und ungültig)
                self._send_version_document(parse_qs(self.path.split('?', 1)[1], keep_blank_values=True)
                                            if '?' in self.path else {})
                return
            elif self.path.startswith("/invoice/pdf_download"):
                # Abgelegtes Rechnungs-PDF ausliefern – nur herunterladen, nicht
                # neu erzeugen (todo #3). inline=1 zeigt es im Browser an.
                query_components = self._query()
                invoice_id = _id_param(query_components)
                disposition = 'inline' if query_components.get('inline') else 'attachment'
                self._send_invoice_document(invoice_id, 'pdf', "application/pdf",
                                            disposition=disposition)
                return
            elif self.path == "/invoice/reminders":
                self.respond(200, pages.PageReminders(db))
            # ── Angebote (Quotes) ─────────────────────────────────────────
            elif self.path == "/quote" or self.path.startswith("/quote?"):
                qs = self._query()
                filters = {}
                if 'search' in qs:
                    filters['search'] = qs['search'][0]
                if 'status' in qs:
                    filters['status'] = qs['status'][0]
                quote_id = _int_param(qs, 'id')
                self.respond(200, PageQuote(db, filters, quote_id, err=qs.get('err', [None])[0]))
            elif self.path.startswith("/quote/edit"):
                self.respond(200, PageQuote(db, {}, _id_param(self._query())))
            elif self.path.startswith("/quote/pdf_download"):
                # Abgelegte Angebots-PDF ausliefern – wie bei Rechnungen nie neu erzeugt.
                # inline=1 zeigt sie im Browser an (Viewer-Tab nach dem Erzeugen).
                qs = self._query()
                disposition = 'inline' if qs.get('inline') else 'attachment'
                self._send_invoice_document(_id_param(qs), 'pdf', "application/pdf",
                                            disposition=disposition, doc_type='quote')
                return
            elif self.path == "/dashboard":
                self.respond(200, PageDashboard(db))
            # ── Master Data (Stammdaten) ──────────────────────────────────
            elif self.path == "/masterdata":
                self.respond(200, PageMasterData(db))
            # Articles
            elif self.path == "/masterdata/articles" or self.path.startswith("/masterdata/articles?"):
                qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                self.respond(200, PageArticles(db, error=qs.get('error', [None])[0]))
            elif self.path.startswith("/masterdata/articles/edit"):
                self.respond(200, PageArticleEdit(db, _id_param(self._query())))
            # Contacts
            elif self.path == "/masterdata/contacts" or self.path.startswith("/masterdata/contacts?"):
                qs = self._query()
                c_type   = qs.get('type',   [None])[0]
                c_entity = qs.get('entity', [None])[0]
                c_error  = qs.get('error',  [None])[0]
                self.respond(200, PageContacts(db, contact_type_filter=c_type,
                                               entity_type_filter=c_entity, error_msg=c_error))
            elif self.path.startswith("/masterdata/contacts/new"):
                qs = self._query()
                entity  = qs.get('entity', ['company'])[0]
                c_error = qs.get('error',  [None])[0]
                self.respond(200, PageContactNew(db, entity_type=entity, error_msg=c_error))
            elif self.path.startswith("/masterdata/contacts/edit"):
                qs = self._query()
                contact_id = _id_param(qs)
                c_error    = qs.get('error', [None])[0]
                self.respond(200, PageContactEdit(db, contact_id, error_msg=c_error))
            elif self.path.startswith("/masterdata/contacts/check-abbreviation"):
                import json as _json
                qs = self._query()
                value = qs.get('value', [''])[0].strip().upper()
                exclude_id = qs.get('exclude_id', [None])[0]
                if not value:
                    self.respond(200, _json.dumps({"exists": False}), content_type="application/json")
                else:
                    is_unique, suggestion = db.check_abbreviation_unique(value, exclude_id)
                    self.respond(200, _json.dumps({"exists": not is_unique, "suggestion": suggestion}),
                                 content_type="application/json")
            # SKR (Chart of Accounts)
            elif self.path == "/masterdata/skr" or self.path.startswith("/masterdata/skr?"):
                qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                msg = qs.get('msg', [None])[0]
                msg_type = qs.get('type', ['info'])[0]
                self.respond(200, PageSkr(db, msg=msg, msg_type=msg_type, added=_int_param(qs, 'added')))
            elif self.path.startswith("/masterdata/skr/edit"):
                self.respond(200, PageSkrEdit(db, _id_param(self._query())))
            elif self.path.startswith("/masterdata/skr/copy"):
                self.respond(200, PageSkr(db, copy_from_id=_id_param(self._query())))
            # Bank Accounts
            elif self.path == "/masterdata/bankaccounts" or self.path.startswith("/masterdata/bankaccounts?"):
                self.respond(200, PageBankAccounts(db, error=self._query().get("error", [None])[0]))
            elif self.path.startswith("/masterdata/bankaccounts/edit"):
                query_components = self._query()
                account_id = _id_param(query_components)
                self.respond(200, PageBankAccountEdit(
                    db, account_id, error=query_components.get("error", [None])[0]))
            # ──────────────────────────────────────────────────────────────
            elif self.path == "/miscellaneous" or self.path.startswith("/miscellaneous?"):
                qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                self.respond(200, PageMiscellaneous(
                    db, check_payments=qs.get("check_payments", [""])[0] == "1",
                    datev_confirm=qs.get("datev_export", [""])[0] == "confirm",
                    datev_from=qs.get("datev_from", [None])[0], datev_to=qs.get("datev_to", [None])[0],
                    datev_check=qs.get("datev_check", [None])[0], msg=qs.get("msg", [None])[0]))
            elif self.path == "/receipts" or self.path.startswith("/receipts?"):
                qs = self._query()
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageReceipts(db, date_from, date_to, err=qs.get("err", [None])[0]), headers=hdrs)
            elif self.path.split('?', 1)[0] == "/receipts/file":
                self._send_receipt_file(db)
                return
            elif route in _SEARCH_PATHS:
                # Rein lesend, aber mit Herkunftsprüfung wie die Datei eines Belegs: die Treffer sind Daten des Benutzers
                suggestions = route == "/receipts/suggestions"
                if not request_is_same_origin(self.headers):
                    self._respond_json(403, {'success': False, 'error': receipt_links.SUGGEST_FOREIGN if suggestions
                                             else receipt_links.SEARCH_FOREIGN})
                    return
                if suggestions:
                    status_code, body = handlers.handle_receipt_suggestions(db, _id_param(self._query()))
                else:
                    search = handlers.handle_receipt_search if route == "/receipts/search" else handlers.handle_booking_search
                    status_code, body = search(db, self._query())
                self.respond(status_code, body, content_type="application/json")
            elif self.path.startswith("/receipts/edit"):
                self.respond(200, PageReceiptEdit(db, _id_param(self._query())))
            elif self.path == "/transactions" or self.path.startswith("/transactions?"):
                qs = self._query()
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageTransactions(db, date_from=date_from, date_to=date_to,
                                                   from_invoice=_int_param(qs, "from_invoice"),
                                                   revenue_coa_id=_int_param(qs, "revenue_coa_id"),
                                                   err=qs.get("err", [None])[0]),
                             headers=hdrs)
            elif self.path.startswith("/transactions/edit"):
                query_components = self._query()
                transaction_id = _id_param(query_components)
                date_from, date_to, _ = resolve_period(query_components, self.headers.get('Cookie'))
                self.respond(200, PageTransactions(db, edit_transaction_id=transaction_id,
                                                   date_from=date_from, date_to=date_to))
            elif self.path.startswith("/transactions/suggest"):
                qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                status_code, body = handlers.handle_transaction_suggest(db, qs)
                self.respond(status_code, body, content_type="application/json")
            elif self.path == "/masterdata/numberranges" or self.path.startswith("/masterdata/numberranges?"):
                qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                self.respond(200, PageNumberRanges(db, err=qs.get('err', [None])[0]))
            elif self.path.startswith("/masterdata/numberranges/edit"):
                query_components = self._query()
                range_id = _id_param(query_components)
                self.respond(200, PageNumberRangesEdit(db, range_id, err=query_components.get('err', [None])[0]))
            # ── Assets / Anlagenverzeichnis ───────────────────────────────
            elif self.path == "/assets" or self.path.startswith("/assets?"):
                status_filter = ''
                if '?' in self.path:
                    qc = parse_qs(self.path.split('?', 1)[1])
                    status_filter = qc.get('status', [''])[0]
                self.respond(200, PageAssets(db, status_filter=status_filter))
            elif self.path == "/assets/new" or self.path.startswith("/assets/new?"):
                self.respond(200, PageAssetEdit(db, asset_id=None,
                                                parent_id=_int_param(self._query(), 'parent_id')))
            elif self.path.startswith("/assets/edit"):
                self.respond(200, PageAssetEdit(db, asset_id=_id_param(self._query())))
            elif self.path.startswith("/assets/view"):
                qc = self._query()
                self.respond(200, PageAssetView(db, _id_param(qc), err=qc.get("err", [None])[0]))
            elif self.path == "/asset_categories" or self.path.startswith("/asset_categories?"):
                self.respond(200, PageAssetCategories(db, err=self._query().get("err", [None])[0]))
            elif self.path.startswith("/asset_categories/edit"):
                self.respond(200, PageAssetCategoryEdit(db, _id_param(self._query())))
            # ── Zeiten / Arbeitszeiten ────────────────────────────────────
            elif self.path == "/worktime" or self.path.startswith("/worktime?"):
                qs = self._query()
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                person_id = _int_param(qs, 'person')
                error_msg = qs['error'][0] if qs.get('error') else None
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageWorkTimes(db, person_id=person_id,
                                                date_from=date_from, date_to=date_to,
                                                error_msg=error_msg,
                                                customer=_customer_param(qs) or CUSTOMER_ALL), headers=hdrs)
            elif self.path.startswith("/worktime/edit"):
                qs = self._query()
                edit_id = _id_param(qs)
                person_id = _int_param(qs, 'person')
                date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
                self.respond(200, PageWorkTimes(db, person_id=person_id, date_from=date_from,
                                                date_to=date_to, edit_id=edit_id,
                                                customer=_customer_param(qs) or CUSTOMER_ALL))
            elif self.path.startswith("/worktime/pdf"):
                qs = self._query()
                person_id = _int_param(qs, "person")
                if person_id is None:
                    self.respond(400, "Ungültige oder fehlende Person.",
                                 content_type="text/plain; charset=utf-8")
                    return
                # Ein Kundenwert, der sich nicht lesen lässt (oder keinen Kontakt trifft), ergibt keinen Zettel für alle
                customer = _customer_param(qs)
                if customer is None or (isinstance(customer, int) and db.get_contact_by_id(customer) is None):
                    self.respond(400, "Ungültiger Kunde.", content_type="text/plain; charset=utf-8")
                    return
                date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
                with_notes = qs.get('notes', ['0'])[0] in ('1', 'true', 'on')
                with_location = qs.get('location', ['0'])[0] in ('1', 'true', 'on')
                with_empty_days = qs.get('empty', ['1'])[0] not in ('0', 'false', 'off')     # Standard: an
                disposition = 'inline' if qs.get('inline') else 'attachment'
                pdf_bytes, filename = handlers.handle_worktime_pdf(db, person_id, date_from, date_to,
                                                                   with_notes=with_notes,
                                                                   with_location=with_location,
                                                                   customer=customer,
                                                                   with_empty_days=with_empty_days)
                if pdf_bytes:
                    self.send_response(200)
                    self.send_header("Content-type", "application/pdf")
                    self.send_header("Content-Disposition", content_disposition(disposition, filename))
                    self.send_header("Content-Length", str(len(pdf_bytes)))
                    self.end_headers()
                    self.wfile.write(pdf_bytes)
                else:
                    self.send_response(500)
                    self.send_header("Content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write("Fehler beim Erstellen des PDF".encode("utf-8"))
                return
            # ── Benutzerverwaltung (nur Admin) ────────────────────────────
            elif self.path == "/users" or self.path.startswith("/users?"):
                if not self._require_admin():
                    return
                qs = self._query()
                info = qs['info'][0] if qs.get('info') else None
                err = qs['err'][0] if qs.get('err') else None
                csrf = auth.csrf_for(self._csrf_key())
                self.respond(200, PageUsers(userctx.get_user(), auth.list_users(),
                                            csrf, error_msg=err, info_msg=info,
                                            auto_login_user=auth.auto_login_user()))
            # ── Zeiten / Fahrten ──────────────────────────────────────────
            elif self.path == "/trips" or self.path.startswith("/trips?"):
                qs = self._query()
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                person_id = _int_param(qs, 'person')
                error_msg = qs['error'][0] if qs.get('error') else None
                check_vehicle, check_year = _trip_check_params(qs)
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageTrips(db, person_id=person_id,
                                            date_from=date_from, date_to=date_to,
                                            error_msg=error_msg, check_vehicle=check_vehicle,
                                            check_year=check_year), headers=hdrs)
            elif self.path.startswith("/trips/edit"):
                qs = self._query()
                edit_id = _id_param(qs)
                person_id = _int_param(qs, 'person')
                date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
                self.respond(200, PageTrips(db, person_id=person_id, date_from=date_from,
                                            date_to=date_to, edit_id=edit_id))
            # ─────────────────────────────────────────────────────────────
            # ── Legacy Redirects (Compatibility) ──────────────────────────
            # Old URLs redirect to new Master Data structure
            elif self.path == "/articles" or self.path.startswith("/articles?"):
                self.respond(301, "", headers={"Location": "/masterdata/articles"})
            elif self.path.startswith("/articles/"):
                new_path = self.path.replace("/articles/", "/masterdata/articles/")
                self.respond(301, "", headers={"Location": new_path})
            elif self.path == "/contacts" or self.path.startswith("/contacts?"):
                self.respond(301, "", headers={"Location": "/masterdata/contacts"})
            elif self.path.startswith("/contacts/"):
                new_path = self.path.replace("/contacts/", "/masterdata/contacts/")
                self.respond(301, "", headers={"Location": new_path})
            elif self.path == "/skr":
                self.respond(301, "", headers={"Location": "/masterdata/skr"})
            elif self.path.startswith("/edit_skr"):
                new_path = self.path.replace("/edit_skr", "/masterdata/skr/edit")
                self.respond(301, "", headers={"Location": new_path})
            # ──────────────────────────────────────────────────────────────
            elif self.path.split('?', 1)[0] == "/buch.css":
                # Wird mit ?v=<APP_VERSION> referenziert (Cache-Buster) und darf
                # daher lange gecacht werden – ohne Revalidierung pro Seitenwechsel
                # (no-cache verursachte sichtbares Flackern/FOUC bei Navigation).
                self.serve_static_file("buch.css", "text/css",
                                       cache_control="public, max-age=31536000, immutable")
            elif self.path.split('?', 1)[0] == "/favicon.ico":
                self.serve_static_file("favicon.ico", "image/x-icon",
                                       cache_control="public, max-age=86400")
            elif self.path.startswith("/data/logos/"):
                # Logos liegen im logos-Verzeichnis des angemeldeten Nutzers
                # (data/users/<Name>/logos). Es wird nur der Dateiname genutzt,
                # gegen das Nutzerverzeichnis aufgeloest → kein Cross-User-Zugriff,
                # kein Path-Traversal. Ausgeliefert werden nur Bilddateien
                # (Positivliste LOGO_CONTENT_TYPES): kein SVG/HTML aus dem Ursprung der App.
                from urllib.parse import unquote
                fname = os.path.basename(unquote(self.path.split('?', 1)[0]))
                base = os.path.realpath(userctx.user_subdir('logos', create=False))
                try:
                    target = os.path.realpath(os.path.join(base, fname))
                except ValueError:               # z. B. %00 im Namen: so eine Datei gibt es nicht (nicht: 500)
                    target = ''
                if (logo_extension(fname) and logo_extension(target)
                        and (target == base or target.startswith(base + os.sep)) and os.path.isfile(target)):
                    self.serve_static_file(target, self.guess_content_type(target))
                else:
                    self.respond(404, "Datei nicht gefunden.")
            elif self.path.startswith("/seed_data/private/"):
                # Geteilte Privat-Seed-Logos (nur Eigentümer-Setup) – global,
                # mit Path-Traversal-Schutz und derselben Positivliste wie oben.
                from urllib.parse import unquote
                rel_path = unquote(self.path.split('?', 1)[0])[1:]
                base = os.path.realpath("seed_data/private")
                try:
                    target = os.path.realpath(rel_path)
                except ValueError:               # z. B. %00 im Pfad: so eine Datei gibt es nicht (nicht: 500)
                    target = ''
                if (logo_extension(rel_path) and logo_extension(target)
                        and (target == base or target.startswith(base + os.sep)) and os.path.isfile(target)):
                    self.serve_static_file(target, self.guess_content_type(target))
                else:
                    self.respond(404, "Datei nicht gefunden.")
            else:
                self.respond(404, "Seite nicht gefunden.")
        except Exception:
            if self.wfile.failed:
                raise                               # die Antwort ließ sich nicht zustellen: nichts mehr senden
            # Vollen Traceback nur ins Server-Log, nie an den Client
            # (Information Disclosure). Client erhaelt eine generische Seite.
            import traceback
            traceback.print_exc()
            self.respond(500, "<h1>Server Fehler</h1><p>Es ist ein interner Fehler "
                              "aufgetreten. Details stehen im Server-Log.</p>")
        finally:
            userctx.clear()

    def serve_static_file(self, filename, content_type, cache_control="no-cache"):
        """Statische Datei mit Last-Modified/304-Unterstützung ausliefern.

        cache_control: Default "no-cache" (cachen erlaubt, aber vor jeder
        Nutzung revalidieren). Versionierte Ressourcen (?v=…-Links) können
        stattdessen lange gecacht werden ("max-age=…, immutable").
        """
        try:
            file_mtime = os.path.getmtime(filename)
            file_mtime_string  = self.date_time_string(file_mtime)

            if_modified_since = self.headers.get('If-Modified-Since')
            if if_modified_since and if_modified_since.strip() == file_mtime_string:
                self.send_response(304)
                self.send_header("Cache-Control", cache_control)
                self.end_headers()
                return

            with open(filename, 'rb') as file:
                data = file.read()
                self.send_response(200)
                self.send_header("Content-type", content_type)
                self.send_header("Last-Modified", file_mtime_string)
                self.send_header("Cache-Control", cache_control)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except FileNotFoundError:
            self.respond(404, "Datei nicht gefunden.")
    
    def guess_content_type(self, filename):
        """Guess content type based on file extension"""
        extension = os.path.splitext(filename)[1].lower()
        if extension in LOGO_CONTENT_TYPES:
            return LOGO_CONTENT_TYPES[extension]
        elif extension == '.css':
            return 'text/css'
        elif extension == '.js':
            return 'application/javascript'
        else:
            return 'application/octet-stream'

    def do_POST(self):
        if not self._gate():
            return
        self._run_locked(self._dispatch_post)

    def _dispatch_post(self):
        if self._auth_routes_post():
            userctx.clear()
            return
        db = self._open_db()
        if db is False:
            return

        # ── CSRF für fetch-Routen (Body wird erst im Handler gelesen): Token
        # kommt als X-CSRF-Token-Header (Footer-JS umhüllt window.fetch).
        # /wiso/import ist ein klassisches Multipart-Formular und prüft das
        # csrf-Feld selbst im Handler; alle übrigen Formular-Routen werden
        # unten nach parse_qs über das Feld 'csrf' geprüft.
        if self.path in _JSON_PATHS or self.path in ("/upload_receipts",
                                                      "/masterdata/contacts/upload-logo"):
            if not self._check_csrf():
                return

        # Dokumente erzeugen (JSON-Antwort): Token aus dem Header, Parameter in der Query
        route = self.path.split('?', 1)[0]
        if route in _GENERATE_PATHS:
            if not self._check_csrf():
                return
            self._post_generate_document(db, route)
            return

        # Uploads mit eigener Prüfung (WISO-Datenbank als roher Body, Dateien der Belege-Seite als Multipart): die Methode prüft
        # Token, Demo-Modus und Größe selbst, bevor sie liest
        if route in _UPLOAD_PATHS:
            if route == "/receipts/upload":
                self._post_receipts_upload(db)
            else:
                self._post_wiso_fdb_upload()
            return

        # Handle file upload separately (der Handler liest den Body selbst: Demo-Modus und Größe vorher prüfen, das Token steht oben)
        if self.path == "/upload_receipts":
            if demo.is_active():
                # Demo-Modus: die Besucher teilen ein Verzeichnis – keine Dateien eines Besuchers auf dem Server (wie /receipts/upload)
                self._refuse_body(403, upload_handler.STATEMENTS_DEMO_REFUSED, as_json=True)
                return
            if not self._check_body_size(MAX_UPLOAD_BODY, as_json=True):
                return
            status_code, response = upload_handler.handle_file_upload(self, db)
            self.respond(status_code, response, content_type="application/json")
            return

        # Handle WISO CSV import (multipart file upload)
        if self.path == "/wiso/import":
            if not self._check_body_size(MAX_UPLOAD_BODY):
                return
            status_code, location = handlers.handle_wiso_import(self, db)
            self.respond(status_code, "", headers={"Location": location})
            return

        # Handle contact logo upload (multipart file upload)
        if self.path == "/masterdata/contacts/upload-logo":
            response_data = handlers.handle_logo_upload(self)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", str(len(response_data)))
            if self.close_connection:            # abgelehnt, ohne den Body zu lesen (handle_logo_upload)
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response_data)
            return

        # Rechnungen und Angebote per fetch (JSON), dazu das Verknüpfen und das Lesen von Belegen
        if self.path in _JSON_PATHS:
            self._post_json_route(self.path, db)
            return

        # Parse regular form data (vor der Ersteinrichtung gibt es noch niemanden, der angemeldet wäre)
        raw = self._read_body(MAX_PREAUTH_BODY if getattr(self, '_needs_setup', False) else MAX_FORM_BODY)
        if raw is None:
            return
        post_body = self._decode_form(raw)
        if post_body is None:
            return
        post_data = parse_form(post_body)

        # ── CSRF für alle Formular-Routen (Feld 'csrf' oder Header) ───────────
        if not self._check_csrf(post_data):
            return

        # Route to appropriate handler
        try:
            if self._post_action_routes(db):
                return
            # ── Benutzerverwaltung (nur Admin) ────────────────────────────
            if self.path == "/users/auto-login":
                if not self._require_admin():
                    return
                location, login_as = handlers.handle_user_auto_login(post_data, userctx.get_user())
                headers = {"Location": location}
                if login_as:
                    # Nach dem Ausschalten bleibt der Administrator angemeldet (neue Sitzung)
                    headers["Set-Cookie"] = self._session_cookie_header(auth.create_session(login_as))
                self.respond(303, "", headers=headers)
            elif self.path in ("/users/create", "/users/delete",
                               "/users/reset-password", "/users/toggle-admin"):
                if not self._require_admin():
                    return
                handler = {
                    "/users/create": handlers.handle_user_create,
                    "/users/delete": handlers.handle_user_delete,
                    "/users/reset-password": lambda data: handlers.handle_user_reset_password(
                        data, self._session_token()),
                    "/users/toggle-admin": handlers.handle_user_toggle_admin,
                }[self.path]
                location = handler(post_data)
                self.respond(303, "", headers={"Location": location})
            elif self.path == "/quote/convert":
                status_code, location = handlers.handle_convert_quote_to_invoice(post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/add_receipt":
                status_code, location = handlers.handle_add_receipt(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/update_receipt":
                self._respond_form_result(handlers.handle_update_receipt(db, post_data))
            elif self.path == "/confirm_transactions":
                status_code, response = handlers.handle_confirm_import(db, post_data)
                self.respond(status_code, response, content_type="application/json")
            elif self.path == "/transactions/add":
                self._respond_form_result(handlers.handle_add_transaction(db, post_data))
            elif self.path == "/masterdata/bankaccounts/add":
                self._respond_form_result(handlers.handle_add_bankaccount(db, post_data))
            elif self.path == "/masterdata/bankaccounts/update":
                self._respond_form_result(handlers.handle_update_bankaccount(db, post_data))
            elif self.path == "/masterdata/numberranges/add":
                self._respond_form_result(handlers.handle_add_number_range(db, post_data))
            elif self.path == "/masterdata/numberranges/update":
                self._respond_form_result(handlers.handle_update_number_range(db, post_data))
            # ── Master Data POST Routes ───────────────────────────────────
            # SKR
            elif self.path == "/masterdata/skr/add":
                self._respond_form_result(handlers.handle_add_skr(db, post_data))
            elif self.path == "/masterdata/skr/update":
                self._respond_form_result(handlers.handle_update_skr(db, post_data))
            # Contacts
            elif self.path == "/masterdata/contacts/add":
                self._respond_form_result(handlers.handle_add_contact(db, post_data))
            elif self.path == "/masterdata/contacts/update":
                self._respond_form_result(handlers.handle_update_contact(db, post_data))
            # Zeiten / Arbeitszeiten
            elif self.path == "/worktime/add":
                self._respond_form_result(handlers.handle_add_worktime(db, post_data))
            elif self.path == "/worktime/update":
                self._respond_form_result(handlers.handle_update_worktime(db, post_data))
            # Zeiten / Fahrten
            elif self.path == "/trips/add":
                status_code, location = handlers.handle_add_trip(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/trips/update":
                status_code, location = handlers.handle_update_trip(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            # Articles
            elif self.path == "/masterdata/articles/add":
                self._respond_form_result(handlers.handle_add_article(db, post_data))
            elif self.path == "/masterdata/articles/update":
                self._respond_form_result(handlers.handle_update_article(db, post_data))
            # ──────────────────────────────────────────────────────────────
            # ── Assets ────────────────────────────────────────────────────
            elif self.path == "/assets/add":
                self._respond_form_result(handlers.handle_add_asset(db, post_data))
            elif self.path == "/assets/update":
                self._respond_form_result(handlers.handle_update_asset(db, post_data))
            elif self.path == "/assets/depreciate":
                self._respond_form_result(handlers.handle_book_depreciation(db, post_data))
            elif self.path == "/assets/sell":
                self._respond_form_result(handlers.handle_asset_sale(db, post_data))
            elif self.path == "/asset_categories/add":
                self._respond_form_result(handlers.handle_add_asset_category(db, post_data))
            elif self.path == "/asset_categories/update":
                self._respond_form_result(handlers.handle_update_asset_category(db, post_data))
            # ── Datensicherung / Wiederherstellung ────────────────────────
            elif self.path == "/backup/create":
                status_code, location = handlers.handle_backup_create(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/backup/restore":
                status_code, location = handlers.handle_backup_restore(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            # ─────────────────────────────────────────────────────────────
            # ──────────────────────────────────────────────────────────────
            # ── Legacy POST Redirects (Compatibility) ─────────────────────
            elif self.path == "/add_skr":
                self._respond_form_result(handlers.handle_add_skr(db, post_data), "/masterdata/skr")
            elif self.path == "/update_skr":
                self._respond_form_result(handlers.handle_update_skr(db, post_data), "/masterdata/skr")
            elif self.path == "/add_contact":
                self._respond_form_result(handlers.handle_add_contact(db, post_data), "/masterdata/contacts")
            elif self.path == "/update_contact":
                self._respond_form_result(handlers.handle_update_contact(db, post_data), "/masterdata/contacts")
            elif self.path == "/articles/add":
                self._respond_form_result(handlers.handle_add_article(db, post_data))
            elif self.path == "/articles/update":
                self._respond_form_result(handlers.handle_update_article(db, post_data))
            # ──────────────────────────────────────────────────────────────
            elif self.path == "/execute_sql":
                if not auth.sql_console_allowed():
                    import json as _json
                    self.respond(403, _json.dumps({
                        "success_count": 0, "total": 0, "output": "",
                        "errors": ["Die SQL-Konsole steht nur Administratoren zur Verfügung."],
                    }, ensure_ascii=False), content_type="application/json")
                else:
                    content = handlers.handle_execute_sql(db, post_data)
                    self.respond(200, content, content_type="application/json")
            elif self.path == "/wiso/fdb-import":
                status_code, location = handlers.handle_wiso_fdb_import(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/db_export":
                status_code, location = handlers.handle_db_export(db)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/datev/export":
                result = handlers.handle_datev_export(db, post_data)
                if isinstance(result[0], bytes):
                    csv_data, filename = result
                    self.send_response(200)
                    self.send_header("Content-type", "text/plain; charset=windows-1252")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{filename}"')
                    self.send_header("Content-Length", str(len(csv_data)))
                    self.end_headers()
                    self.wfile.write(csv_data)
                else:
                    status_code, location = result
                    self.respond(status_code, "", headers={"Location": location})
                return
            # ── Setup ─────────────────────────────────────────────────────
            elif self.path == "/setup/start":
                ok, username, with_login, err = handlers.handle_setup_start(post_data)
                if ok is None:
                    self.respond(403, "<h1>403</h1><p>Die Ersteinrichtung ist bereits abgeschlossen.</p>")
                elif not ok:
                    self.respond(200, PageSetupStart(error_msg=err, username=username))
                else:
                    headers = {"Location": "/"}
                    if with_login:
                        headers["Set-Cookie"] = self._session_cookie_header(auth.create_session(username))
                    self.respond(303, "", headers=headers)
            elif self.path == "/setup/save":
                status_code, response = handlers.handle_setup_save(db, post_data)
                if status_code == 303:
                    self.respond(status_code, "", headers={"Location": response})
                else:
                    self.respond(status_code, response)
            # ─────────────────────────────────────────────────────────────
            else:
                self.respond(404, "Seite nicht gefunden.")
        except Exception:
            if self.wfile.failed:
                raise                               # die Antwort ließ sich nicht zustellen: nichts mehr senden
            # Vollen Traceback nur ins Server-Log, nie an den Client.
            import traceback
            traceback.print_exc()
            self.respond(500, "<h1>Server Fehler</h1><p>Es ist ein interner Fehler "
                              "aufgetreten. Details stehen im Server-Log.</p>")

    def respond(self, status_code, content, headers=None, content_type="text/html"):
        # Send HTTP status and headers
        self.send_response(status_code)
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        # Wird die Verbindung nach dieser Antwort geschlossen (Body ungelesen, ``_drain_body``), sagt der
        # Server es auch – sonst hielte der Client sie für offen.
        if self.close_connection and not any(key.lower() == "connection" for key in (headers or {})):
            self.send_header("Connection", "close")
        self.send_header("Content-type", content_type)
        encoded = content.encode("utf-8") if content else b""
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()

        # Write content to response
        if encoded:
            self.wfile.write(encoded)


def _ensure_directories():
    """Fehlende Verzeichnisse anlegen (idempotent): die Datenwurzel und, sobald die
    Installation eingerichtet ist, ``users/``.

    Es werden keine globalen Typ-Verzeichnisse angelegt – die Dateien jedes Nutzers
    entstehen bei Bedarf unter data/users/<user>/.
    """
    import os
    root = userctx.DATA_ROOT
    os.makedirs(root, exist_ok=True)
    if userctx.get_mode() == "multi":
        os.makedirs(os.path.join(root, "users"), exist_ok=True)


def build_server(host, port, certfile=None, keyfile=None):
    """Den HTTP-Server anlegen (``ThreadingHTTPServer`` mit ``SimpleWebServer``), mit Zertifikat und Schlüssel als HTTPS-Server.

    Der lauschende Socket verhandelt kein TLS selbst (``do_handshake_on_connect=False``): ``accept()`` nimmt die Verbindung nur an, den
    Handshake führt je Verbindung ``SimpleWebServer.setup()`` in ihrem eigenen Thread aus, mit der Zeitgrenze ``TLS_HANDSHAKE_TIMEOUT``.
    ``httpd.tls`` sagt, ob der Server HTTPS spricht (aktiviert das Secure-Flag der Cookies). Die Funktion legt nur an – sie startet
    nichts und gibt nichts aus.

    Die Warteschlange angenommener Verbindungen ist ``ACCEPT_QUEUE`` lang: Mit dem Standard der Bibliothek (5) blieben bei mehreren
    Browsern, die je einige Verbindungen zugleich öffnen, einzelne im Betriebssystem liegen, bis wieder Platz ist.
    """
    httpd = ThreadingHTTPServer((host, port), SimpleWebServer, bind_and_activate=False)
    httpd.request_queue_size = ACCEPT_QUEUE
    try:
        httpd.server_bind()
        httpd.server_activate()
    except BaseException:
        httpd.server_close()
        raise
    httpd.tls = False
    if certfile and keyfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        httpd.tls = True
    return httpd


# Start web server
def run_server(host=None, port=None, certfile=None, keyfile=None):
    """Web-Server starten.

    Defaults kommen aus Umgebungsvariablen (für Deployment im LAN/VM):
      UBUCHHALTUNG_HOST (Default 'localhost'; '0.0.0.0' für Netzzugriff),
      UBUCHHALTUNG_PORT (Default 8080),
      UBUCHHALTUNG_CERT / UBUCHHALTUNG_KEY (PEM-Dateien ⇒ HTTPS, self-signed möglich).

    Die Ersteinrichtung legt den ersten Administrator an und trägt ``"mode": "multi"``
    in data/config.json ein (für Headless-Setup ggf. vorab mit {"mode":"multi"} anlegen).
    Eine Ablage aus Version 1.x zieht ``layout.migrate()`` nach data/users/<Name>/ um;
    main.py ruft es vor dem Start auf, hier läuft es zur Sicherheit noch einmal
    (idempotent), falls der Server über einen anderen Einstieg gestartet wird.
    """
    import socket, sys
    host = host or os.environ.get("UBUCHHALTUNG_HOST", "localhost")
    port = int(port or os.environ.get("UBUCHHALTUNG_PORT", "8080"))
    certfile = certfile or os.environ.get("UBUCHHALTUNG_CERT")
    keyfile = keyfile or os.environ.get("UBUCHHALTUNG_KEY")

    import layout
    if layout.needs_migration():
        layout.migrate()
    _ensure_directories()
    if userctx.auth_enabled():
        auth.init_auth_db()                      # Auth-Tabellen vor dem ersten Request anlegen
    # Prüfen ob der Port schon belegt ist
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        if _s.connect_ex((host, port)) == 0:
            print(f"\nFEHLER: Port {port} ist bereits belegt – läuft noch eine andere Server-Instanz?")
            print(f"  Windows:  Stop-Process -Id (netstat -ano | findstr :{port})")
            print(f"  Unix:     kill $(lsof -ti:{port})")
            sys.exit(1)

    httpd = build_server(host, port, certfile, keyfile)
    scheme = "https" if httpd.tls else "http"

    print(f"Starting server on {scheme}://{host}:{port} ...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()
