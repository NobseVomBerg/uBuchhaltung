# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Main HTTP server class with routing
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlsplit
from http.cookies import SimpleCookie
import ipaddress
import json
import socket
import unicodedata
from db import Database, MigrationBackupError, SchemaTooNewError
import os
import auth
import demo
import userctx

from . import pages
from . import handlers
from . import upload_handler
from .pages_login import PageLogin
from .pages_users import PageUsers
from .period import resolve_period, period_cookie_header
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
from .pages_receipts import PageReceipts, PageReceiptEdit
from .pages_setup import PageSetup, PageSetupStart
from .pages_invoice import PageInvoice
from .pages_quote import PageQuote
from .pages_worktime import PageWorkTimes
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


def _int_param(qs, name):
    """Ganzzahliger Query-Parameter (``qs`` aus parse_qs) oder ``None``, wenn er fehlt
    oder keine Ganzzahl ist."""
    try:
        return int(qs[name][0])
    except (KeyError, IndexError, ValueError):
        return None


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
# Parameter bleiben in der Query.
_ACTION_PATHS = frozenset({
    "/masterdata/articles/delete",
    "/masterdata/contacts/delete",
    "/masterdata/skr/delete",
    "/masterdata/skr/togglemenu",
    "/masterdata/bankaccounts/delete",
    "/receipts/delete",
    "/transactions/delete",
    "/documents/unlink",
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

# Rechnungen und Angebote per fetch: JSON hinein, JSON hinaus, siehe `_post_json_route`.
_JSON_PATHS = frozenset({
    "/invoice/save",
    "/invoice/send-email",
    "/invoice/link-payment",
    "/invoice/delete-payment",
    "/invoice/status",
    "/invoice/cancel",
    "/invoice/reissue",
    "/quote/save",
    "/quote/status",
})

# Aktionen mit Seiteneffekt unter den JSON-Routen: GET ergibt 405 (wie bei `_ACTION_PATHS`/`_GENERATE_PATHS`).
# Stornieren und Neu ausstellen legen einen Beleg an, siehe `handlers.handle_invoice_cancel` und
# `handlers.handle_invoice_reissue`.
_POST_ONLY_JSON_PATHS = frozenset({
    "/invoice/cancel",
    "/invoice/reissue",
})


class SimpleWebServer(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def end_headers(self):
        path = getattr(self, 'path', '')           # fehlt bei einer kaputten Anfragezeile
        if path.endswith('.css') or path.endswith('.js') or path.endswith('.png'):
            self.send_header('Cache-Control', 'public, max-age=86400')  # Cache this File for 1d
        else:
            self.send_header('Cache-Control', 'no-cache')               # HTML etc no caching
        # Für ALLE Antworten: kein MIME-Sniffing und kein Einrahmen durch fremde Seiten
        # (Clickjacking, etwa auf „Status setzen“ bei automatischer Anmeldung). Die App rahmt
        # sich nicht selbst ein – PDFs öffnen sich als eigene Seite in einem neuen Tab –, daher
        # DENY statt SAMEORIGIN.
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')

        super().end_headers()                                           # Call the Base

    def send_header(self, keyword, value):
        # Letzte Sicherung gegen Header-Injection, egal woher ein Wert stammt (Query,
        # Cookie, Formular, Dateiname): CR/LF/NUL kommen nie in einen Header.
        super().send_header(_header_safe(keyword), _header_safe(value))

    # ── Authentifizierung (TODO #4) ──────────────────────────────────────────
    def _drain_body(self):
        """Ungelesenen Request-Body verwerfen (verhindert Keep-Alive-Desync bei
        POST-Redirects ohne vorherige Body-Verarbeitung)."""
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            if length > 0:
                self.rfile.read(length)
        except Exception:
            pass

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
        return "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"

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
        userctx.set_tls(getattr(self.server, 'tls', False))
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
        length = int(self.headers.get('Content-Length', 0) or 0)
        post_data = parse_qs(self.rfile.read(length).decode('utf-8')) if length else {}
        if path == '/logout':
            # Mit gültiger Sitzung nur mit CSRF-Token. Ohne (abgelaufen, anderes Fenster) gibt
            # es nichts zu schützen und kein Token zu prüfen: nur zur Anmeldeseite.
            if userctx.get_user() and not self._check_csrf(post_data):
                return True
            auth.delete_session(self._session_token())
            self.respond(303, "", headers={"Location": "/login",
                                            "Set-Cookie": self._logout_cookie_header()})
            return True
        ok, username, err = handlers.handle_login(post_data)
        if ok:
            token = auth.create_session(username)
            self.respond(303, "", headers={"Location": "/",
                         "Set-Cookie": self._session_cookie_header(token)})
        else:
            self.respond(200, PageLogin(error_msg=err, username=username))
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

    def _local_referer(self, default):
        """Pfad (+ Query) des Referer – nie eine fremde Adresse (kein offener Redirect)."""
        try:
            parts = urlsplit(self.headers.get('Referer') or '')
        except ValueError:
            return default
        # Browser lesen "/\\host" wie "//host" – beides wäre eine fremde Adresse.
        if (not parts.path.startswith('/') or parts.path.startswith('//')
                or '\\' in parts.path):
            return default
        return parts.path + ('?' + parts.query if parts.query else '')

    def _post_action_routes(self, db):
        """Aktionen mit Seiteneffekt (Löschen, Umschalten, Lösen). Parameter stehen
        in der Query, der Body trägt nur den CSRF-Token. Returns True, wenn die
        Anfrage hier beantwortet wurde."""
        path = self.path.split('?', 1)[0]
        if path not in _ACTION_PATHS:
            return False
        qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
        if path == "/masterdata/articles/delete":
            db.delete_article(int(qs["id"][0]))
            self.respond(303, "", headers={"Location": "/masterdata/articles"})
        elif path == "/masterdata/contacts/delete":
            db.delete_contact(int(qs["id"][0]))
            self.respond(303, "", headers={"Location": "/masterdata/contacts"})
        elif path == "/masterdata/skr/delete":
            status_code, location = handlers.handle_delete_skr(db, int(qs["id"][0]))
            self.respond(status_code, "", headers={"Location": location})
        elif path == "/masterdata/skr/togglemenu":
            status_code, location = handlers.handle_toggle_skr_menu(db, int(qs["id"][0]))
            self.respond(status_code, "", headers={"Location": location})
        elif path == "/masterdata/bankaccounts/delete":
            db.delete_account(int(qs["id"][0]))
            self.respond(303, "", headers={"Location": "/masterdata/bankaccounts"})
        elif path == "/receipts/delete":
            db.delete_receipt(qs["number"][0])
            self.respond(303, "", headers={"Location": "/receipts"})
        elif path == "/transactions/delete":
            db.delete_transaction(int(qs["id"][0]))
            self.respond(303, "", headers={"Location": "/transactions"})
        elif path == "/documents/unlink":
            doc_id = int(qs["doc_id"][0])
            booking_id = int(qs["booking_id"][0])
            db.unlink_booking_from_document(booking_id, doc_id)
            self.respond(303, "", headers={"Location": self._local_referer('/transactions')})
        elif path == "/masterdata/numberranges/delete":
            db.delete_number_range(int(qs["id"][0]))
            self.respond(303, "", headers={"Location": "/masterdata/numberranges"})
        elif path == "/assets/delete":
            asset_id = int(qs["id"][0])
            try:
                db.delete_asset(asset_id)
                location = "/assets"
            except ValueError as exc:      # gebuchte AfA oder Unteranlagen: nicht löschen
                location = f"/assets/view?id={asset_id}&err={quote(str(exc), safe='')}"
            self.respond(303, "", headers={"Location": location})
        elif path == "/asset_categories/delete":
            db.delete_asset_category(int(qs["id"][0]))
            self.respond(303, "", headers={"Location": "/asset_categories"})
        elif path == "/worktime/delete":
            db.delete_worktime(int(qs["id"][0]))
            person_id = qs['person'][0] if qs.get('person') else ''
            date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
            self.respond(303, "", headers={"Location":
                f"/worktime?person={quote(str(person_id), safe='')}&from={quote(date_from, safe='')}&to={quote(date_to, safe='')}"})
        elif path == "/trips/delete":
            db.delete_trip(int(qs["id"][0]))
            person_id = qs['person'][0] if qs.get('person') else ''
            date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
            self.respond(303, "", headers={"Location":
                f"/trips?person={quote(str(person_id), safe='')}&from={quote(date_from, safe='')}&to={quote(date_to, safe='')}"})
        elif path == "/quote/delete":
            db.delete_quote(int(qs["id"][0]))
            self.respond(303, "", headers={"Location": "/quote"})
        elif path == "/invoice/delete":
            invoice_id = _int_param(qs, "id")
            if invoice_id is None:
                self.respond(400, "Ungültige oder fehlende ID.", content_type="text/plain; charset=utf-8")
            else:
                status_code, location = handlers.handle_delete_invoice(db, invoice_id)
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

    def _post_json_route(self, path):
        """POST-Routen der Rechnungen und Angebote, die JSON lesen und JSON antworten (fetch).

        Ein unbrauchbarer Body – fehlende oder ungültige Content-Length, kein JSON-Objekt,
        fehlende Pflichtfelder (``handlers.InvalidRequest``) – wird mit 400 „Ungültige Anfrage.“
        beantwortet, ein unerwarteter Fehler mit 500. Der Traceback bleibt im Server-Log; die
        Verbindung bricht nie ohne Antwort ab."""
        try:
            length = int(self.headers['Content-Length'])
            if length < 0:
                raise ValueError('negative Länge')
        except (TypeError, ValueError):
            # Ein etwaiger Body bleibt ungelesen: Verbindung danach nicht weiterverwenden
            self._respond_json(400, {'success': False, 'error': 'Ungültige Anfrage.'},
                               headers={"Connection": "close"})
            return
        post_body = self.rfile.read(length)
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
            import traceback
            traceback.print_exc()
            self._respond_json(500, {'success': False, 'error':
                                     'Interner Fehler – Einzelheiten stehen im Server-Log.'})
            return
        self.respond(status_code, response, content_type="application/json")

    def do_GET(self):
        if not self._gate():
            return
        if self._auth_routes_get():
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
            if route in _ACTION_PATHS or route in _GENERATE_PATHS or route in _POST_ONLY_JSON_PATHS:
                self._respond_post_only()
                return
            if self.path == "/" or self.path == "/dashboard" or self.path.startswith("/dashboard?"):
                if db.is_first_run():
                    self.respond(303, "", headers={"Location": "/setup"})
                    return
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                acct_raw  = qs.get('acct', [])
                account_ids = [int(a) for a in acct_raw] if acct_raw else None
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageDashboard(db, date_from, date_to, account_ids), headers=hdrs)
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
                query_components = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                # Zeitraum aus Query/Cookie/Default – gilt seitenübergreifend
                date_from, date_to, set_cookie = resolve_period(query_components, self.headers.get('Cookie'))
                filters = {'date_from': date_from, 'date_to': date_to}
                invoice_id = None
                if 'search' in query_components:
                    filters['search'] = query_components['search'][0]
                if 'status' in query_components:
                    filters['status'] = query_components['status'][0]
                if 'id' in query_components:
                    invoice_id = int(query_components['id'][0])
                # "Als neu kopieren": Formular mit den Daten der Quellrechnung,
                # aber als neue Rechnung (todo #4)
                copy_from = (int(query_components['copy_from'][0])
                             if 'copy_from' in query_components else None)
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageInvoice(db, filters, invoice_id, copy_from=copy_from,
                                              err=query_components.get('err', [None])[0]),
                             headers=hdrs)
            elif self.path.startswith("/invoice/view") or self.path.startswith("/invoice/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                invoice_id = int(query_components["id"][0])
                self.respond(200, PageInvoice(
                    db, {}, invoice_id,
                    payment_error=query_components.get("payment_error", [None])[0]))
            elif self.path.startswith("/invoice/xml_download"):
                # Abgelegte XRechnung ausliefern – nur herunterladen, nie frisch erzeugen
                # (todo #3); eine Route, die das XML neu baute, gibt es nicht.
                query_components = parse_qs(self.path.split('?')[1])
                self._send_invoice_document(int(query_components["id"][0]), 'xml',
                                            "application/xml")
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
                query_components = parse_qs(self.path.split('?')[1])
                invoice_id = int(query_components["id"][0])
                disposition = 'inline' if query_components.get('inline') else 'attachment'
                self._send_invoice_document(invoice_id, 'pdf', "application/pdf",
                                            disposition=disposition)
                return
            elif self.path == "/invoice/reminders":
                self.respond(200, pages.PageReminders(db))
            # ── Angebote (Quotes) ─────────────────────────────────────────
            elif self.path == "/quote" or self.path.startswith("/quote?"):
                qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                filters = {}
                if 'search' in qs:
                    filters['search'] = qs['search'][0]
                if 'status' in qs:
                    filters['status'] = qs['status'][0]
                quote_id = int(qs['id'][0]) if 'id' in qs else None
                self.respond(200, PageQuote(db, filters, quote_id, err=qs.get('err', [None])[0]))
            elif self.path.startswith("/quote/edit"):
                qs = parse_qs(self.path.split('?')[1])
                self.respond(200, PageQuote(db, {}, int(qs["id"][0])))
            elif self.path.startswith("/quote/pdf_download"):
                # Abgelegte Angebots-PDF ausliefern – wie bei Rechnungen nie neu erzeugt.
                # inline=1 zeigt sie im Browser an (Viewer-Tab nach dem Erzeugen).
                qs = parse_qs(self.path.split('?')[1])
                disposition = 'inline' if qs.get('inline') else 'attachment'
                self._send_invoice_document(int(qs["id"][0]), 'pdf', "application/pdf",
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
                query_components = parse_qs(self.path.split('?')[1])
                article_id = int(query_components["id"][0])
                self.respond(200, PageArticleEdit(db, article_id))
            # Contacts
            elif self.path == "/masterdata/contacts" or self.path.startswith("/masterdata/contacts?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                c_type   = qs.get('type',   [None])[0]
                c_entity = qs.get('entity', [None])[0]
                c_error  = qs.get('error',  [None])[0]
                self.respond(200, PageContacts(db, contact_type_filter=c_type,
                                               entity_type_filter=c_entity, error_msg=c_error))
            elif self.path.startswith("/masterdata/contacts/new"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                entity  = qs.get('entity', ['company'])[0]
                c_error = qs.get('error',  [None])[0]
                self.respond(200, PageContactNew(db, entity_type=entity, error_msg=c_error))
            elif self.path.startswith("/masterdata/contacts/edit"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                contact_id = int(qs["id"][0])
                c_error    = qs.get('error', [None])[0]
                self.respond(200, PageContactEdit(db, contact_id, error_msg=c_error))
            elif self.path.startswith("/masterdata/contacts/check-abbreviation"):
                import json as _json
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
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
                self.respond(200, PageSkr(db, msg=msg, msg_type=msg_type))
            elif self.path.startswith("/masterdata/skr/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                id = int(query_components["id"][0])
                self.respond(200, PageSkrEdit(db, id))
            elif self.path.startswith("/masterdata/skr/copy"):
                query_components = parse_qs(self.path.split('?')[1])
                id = int(query_components["id"][0])
                self.respond(200, PageSkr(db, copy_from_id=id))
            # Bank Accounts
            elif self.path == "/masterdata/bankaccounts":
                self.respond(200, PageBankAccounts(db))
            elif self.path.startswith("/masterdata/bankaccounts/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                account_id = int(query_components["id"][0])
                self.respond(200, PageBankAccountEdit(
                    db, account_id, error=query_components.get("error", [None])[0]))
            # ──────────────────────────────────────────────────────────────
            elif self.path == "/miscellaneous" or self.path.startswith("/miscellaneous?"):
                qs = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                self.respond(200, PageMiscellaneous(
                    db, check_payments=qs.get("check_payments", [""])[0] == "1"))
            elif self.path == "/receipts" or self.path.startswith("/receipts?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageReceipts(db, date_from, date_to), headers=hdrs)
            elif self.path.startswith("/receipts/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                number = query_components["number"][0]
                self.respond(200, PageReceiptEdit(db, number))
            elif self.path == "/transactions" or self.path.startswith("/transactions?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                from_invoice = int(qs["from_invoice"][0]) if qs.get("from_invoice") else None
                self.respond(200, PageTransactions(db, date_from=date_from, date_to=date_to,
                                                   from_invoice=from_invoice,
                                                   revenue_coa_id=_int_param(qs, "revenue_coa_id")),
                             headers=hdrs)
            elif self.path.startswith("/transactions/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                transaction_id = int(query_components["id"][0])
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
                query_components = parse_qs(self.path.split('?')[1])
                range_id = int(query_components["id"][0])
                self.respond(200, PageNumberRangesEdit(db, range_id, err=query_components.get('err', [None])[0]))
            # ── Assets / Anlagenverzeichnis ───────────────────────────────
            elif self.path == "/assets" or self.path.startswith("/assets?"):
                status_filter = ''
                if '?' in self.path:
                    qc = parse_qs(self.path.split('?', 1)[1])
                    status_filter = qc.get('status', [''])[0]
                self.respond(200, PageAssets(db, status_filter=status_filter))
            elif self.path == "/assets/new" or self.path.startswith("/assets/new?"):
                parent_id = None
                if '?' in self.path:
                    qc = parse_qs(self.path.split('?', 1)[1])
                    pid = qc.get('parent_id', [None])[0]
                    parent_id = int(pid) if pid else None
                self.respond(200, PageAssetEdit(db, asset_id=None, parent_id=parent_id))
            elif self.path.startswith("/assets/edit"):
                qc = parse_qs(self.path.split('?', 1)[1])
                asset_id = int(qc["id"][0])
                self.respond(200, PageAssetEdit(db, asset_id=asset_id))
            elif self.path.startswith("/assets/view"):
                qc = parse_qs(self.path.split('?', 1)[1])
                asset_id = int(qc["id"][0])
                self.respond(200, PageAssetView(db, asset_id, err=qc.get("err", [None])[0]))
            elif self.path == "/asset_categories" or self.path.startswith("/asset_categories?"):
                self.respond(200, PageAssetCategories(db))
            elif self.path.startswith("/asset_categories/edit"):
                qc = parse_qs(self.path.split('?', 1)[1])
                cat_id = int(qc["id"][0])
                self.respond(200, PageAssetCategoryEdit(db, cat_id))
            # ── Zeiten / Arbeitszeiten ────────────────────────────────────
            elif self.path == "/worktime" or self.path.startswith("/worktime?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                person_id = _int_param(qs, 'person')
                error_msg = qs['error'][0] if qs.get('error') else None
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageWorkTimes(db, person_id=person_id,
                                                date_from=date_from, date_to=date_to,
                                                error_msg=error_msg), headers=hdrs)
            elif self.path.startswith("/worktime/edit"):
                qs = parse_qs(self.path.split('?')[1])
                edit_id = int(qs["id"][0])
                person_id = _int_param(qs, 'person')
                date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
                self.respond(200, PageWorkTimes(db, person_id=person_id, date_from=date_from,
                                                date_to=date_to, edit_id=edit_id))
            elif self.path.startswith("/worktime/pdf"):
                qs = parse_qs(self.path.split('?')[1])
                person_id = _int_param(qs, "person")
                if person_id is None:
                    self.respond(400, "Ungültige oder fehlende Person.",
                                 content_type="text/plain; charset=utf-8")
                    return
                date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
                with_notes = qs.get('notes', ['0'])[0] in ('1', 'true', 'on')
                disposition = 'inline' if qs.get('inline') else 'attachment'
                pdf_bytes, filename = handlers.handle_worktime_pdf(db, person_id, date_from, date_to,
                                                                   with_notes=with_notes)
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
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                info = qs['info'][0] if qs.get('info') else None
                err = qs['err'][0] if qs.get('err') else None
                csrf = auth.csrf_for(self._csrf_key())
                self.respond(200, PageUsers(userctx.get_user(), auth.list_users(),
                                            csrf, error_msg=err, info_msg=info,
                                            auto_login_user=auth.auto_login_user()))
            # ── Zeiten / Fahrten ──────────────────────────────────────────
            elif self.path == "/trips" or self.path.startswith("/trips?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                person_id = _int_param(qs, 'person')
                error_msg = qs['error'][0] if qs.get('error') else None
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageTrips(db, person_id=person_id,
                                            date_from=date_from, date_to=date_to,
                                            error_msg=error_msg), headers=hdrs)
            elif self.path.startswith("/trips/edit"):
                qs = parse_qs(self.path.split('?')[1])
                edit_id = int(qs["id"][0])
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
                # kein Path-Traversal.
                from urllib.parse import unquote
                fname = os.path.basename(unquote(self.path.split('?', 1)[0]))
                base = os.path.realpath(userctx.user_subdir('logos', create=False))
                target = os.path.realpath(os.path.join(base, fname))
                if (target == base or target.startswith(base + os.sep)) and os.path.isfile(target):
                    self.serve_static_file(target, self.guess_content_type(target))
                else:
                    self.respond(404, "Datei nicht gefunden.")
            elif self.path.startswith("/seed_data/private/"):
                # Geteilte Privat-Seed-Logos (nur Eigentümer-Setup) – global,
                # mit Path-Traversal-Schutz.
                from urllib.parse import unquote
                rel_path = unquote(self.path.split('?', 1)[0])[1:]
                base = os.path.realpath("seed_data/private")
                target = os.path.realpath(rel_path)
                if (target == base or target.startswith(base + os.sep)) and os.path.isfile(target):
                    self.serve_static_file(target, self.guess_content_type(target))
                else:
                    self.respond(404, "Datei nicht gefunden.")
            else:
                self.respond(404, "Seite nicht gefunden.")
        except Exception:
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
        if filename.endswith('.png'):
            return 'image/png'
        elif filename.endswith('.jpg') or filename.endswith('.jpeg'):
            return 'image/jpeg'
        elif filename.endswith('.gif'):
            return 'image/gif'
        elif filename.endswith('.svg'):
            return 'image/svg+xml'
        elif filename.endswith('.css'):
            return 'text/css'
        elif filename.endswith('.js'):
            return 'application/javascript'
        else:
            return 'application/octet-stream'

    def do_POST(self):
        if not self._gate():
            return
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

        # Handle file upload separately
        if self.path == "/upload_receipts":
            status_code, response = upload_handler.handle_file_upload(self, db)
            self.respond(status_code, response, content_type="application/json")
            return

        # Handle WISO CSV import (multipart file upload)
        if self.path == "/wiso/import":
            status_code, location = handlers.handle_wiso_import(self, db)
            self.respond(status_code, "", headers={"Location": location})
            return

        # Handle contact logo upload (multipart file upload)
        if self.path == "/masterdata/contacts/upload-logo":
            response_data = handlers.handle_logo_upload(self)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", str(len(response_data)))
            self.end_headers()
            self.wfile.write(response_data)
            return

        # Rechnungen und Angebote per fetch (JSON)
        if self.path in _JSON_PATHS:
            self._post_json_route(self.path)
            return

        # Parse regular form data
        content_length = int(self.headers['Content-Length'])
        post_body = self.rfile.read(content_length).decode('utf-8')
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
                    "/users/reset-password": handlers.handle_user_reset_password,
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
                status_code, location = handlers.handle_update_receipt(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/confirm_transactions":
                status_code, response = handlers.handle_confirm_import(db, post_data)
                self.respond(status_code, response, content_type="application/json")
            elif self.path == "/transactions/add":
                status_code, location = handlers.handle_add_transaction(db, post_data)
                if status_code == 400:           # abgelehnt, nichts gespeichert: Grund als Klartext
                    self.respond(400, location, content_type="text/plain; charset=utf-8")
                else:
                    self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/documents/link":
                status_code, location = handlers.handle_link_document(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/masterdata/bankaccounts/add":
                status_code, location = handlers.handle_add_bankaccount(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/bankaccounts"})
            elif self.path == "/masterdata/bankaccounts/update":
                status_code, location = handlers.handle_update_bankaccount(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/masterdata/numberranges/add":
                status_code, location = handlers.handle_add_number_range(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/masterdata/numberranges/update":
                status_code, location = handlers.handle_update_number_range(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            # ── Master Data POST Routes ───────────────────────────────────
            # SKR
            elif self.path == "/masterdata/skr/add":
                status_code, location = handlers.handle_add_skr(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/masterdata/skr/update":
                status_code, location = handlers.handle_update_skr(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            # Contacts
            elif self.path == "/masterdata/contacts/add":
                status_code, location = handlers.handle_add_contact(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/masterdata/contacts/update":
                status_code, location = handlers.handle_update_contact(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            # Zeiten / Arbeitszeiten
            elif self.path == "/worktime/add":
                status_code, location = handlers.handle_add_worktime(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/worktime/update":
                status_code, location = handlers.handle_update_worktime(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            # Zeiten / Fahrten
            elif self.path == "/trips/add":
                status_code, location = handlers.handle_add_trip(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/trips/update":
                status_code, location = handlers.handle_update_trip(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            # Articles
            elif self.path == "/masterdata/articles/add":
                status_code, location = handlers.handle_add_article(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/masterdata/articles/update":
                status_code, location = handlers.handle_update_article(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            # ──────────────────────────────────────────────────────────────
            # ── Assets ────────────────────────────────────────────────────
            elif self.path == "/assets/add":
                status_code, location = handlers.handle_add_asset(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/assets/update":
                status_code, location = handlers.handle_update_asset(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/assets/depreciate":
                status_code, location = handlers.handle_book_depreciation(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/assets/sell":
                status_code, location = handlers.handle_asset_sale(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/asset_categories/add":
                status_code, location = handlers.handle_add_asset_category(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/asset_categories/update":
                status_code, location = handlers.handle_update_asset_category(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
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
                status_code, location = handlers.handle_add_skr(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/skr"})
            elif self.path == "/update_skr":
                status_code, location = handlers.handle_update_skr(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/skr"})
            elif self.path == "/add_contact":
                status_code, location = handlers.handle_add_contact(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/contacts"})
            elif self.path == "/update_contact":
                status_code, location = handlers.handle_update_contact(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/contacts"})
            elif self.path == "/articles/add":
                status_code, location = handlers.handle_add_article(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/articles/update":
                status_code, location = handlers.handle_update_article(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
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
            elif self.path == "/setup/load_testdata":
                status_code, location = handlers.handle_load_testdata(db)
                self.respond(status_code, "", headers={"Location": location})
            # ─────────────────────────────────────────────────────────────
            else:
                self.respond(404, "Seite nicht gefunden.")
        except Exception:
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

    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, SimpleWebServer)

    scheme = "http"
    httpd.tls = False
    if certfile and keyfile:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        httpd.tls = True                         # aktiviert das Secure-Flag der Cookies
        scheme = "https"

    print(f"Starting server on {scheme}://{host}:{port} ...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()
