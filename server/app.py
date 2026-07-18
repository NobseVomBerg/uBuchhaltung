# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Main HTTP server class with routing
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote
from http.cookies import SimpleCookie
import unicodedata
from db import Database
import os
import auth
import userctx

from . import pages
from . import handlers
from . import upload_handler
from .pages_login import PageLogin, PageSetupAdmin
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
from .pages_booking_groups import PageBookingGroups, PageBookingGroupDetails
from .pages_transactions import PageTransactions
from .pages_dashboard import PageDashboard
from .pages_receipts import PageReceipts, PageReceiptEdit
from .pages_setup import PageSetup, PageSetupModeChoice
from .pages_invoice import PageInvoice
from .pages_quote import PageQuote
from .pages_worktime import PageWorkTimes
from .pages_trips import PageTrips

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


class SimpleWebServer(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def end_headers(self):
        if self.path.endswith('.css') or self.path.endswith('.js') or self.path.endswith('.png'):
            self.send_header('Cache-Control', 'public, max-age=86400')  # Cache this File for 1d
        else:
            self.send_header('Cache-Control', 'no-cache')               # HTML etc no caching
            
        super().end_headers()                                           # Call the Base

    # ── Authentifizierung (TODO #4) ──────────────────────────────────────────
    # Pfade, die ohne Anmeldung erreichbar sein müssen.
    _PUBLIC_PATHS = ('/login', '/logout', '/setup-admin', '/buch.css', '/favicon.ico')

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
        """Database() nur erzeugen, wenn sie auch genutzt werden darf:
        - unkonfiguriert (Modus noch nicht gewählt): nie (kein verwaistes data/buch.db),
        - Mehrbenutzer-Modus: erst nach Anmeldung,
        - Einzelbenutzer-Modus: immer.
        """
        mode = userctx.get_mode()
        if mode is None:
            return None
        if mode == 'multi' and userctx.get_user() is None:
            return None
        return Database()

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
        """True, wenn der angemeldete Nutzer Administrator im Mehrbenutzer-Modus
        ist; sonst wird bereits eine Antwort (404/403) gesendet."""
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
        ``X-CSRF-Token`` (fetch-Aufrufe, siehe Footer-JS). Nur im Mehrbenutzer-
        Modus wirksam – der Token ist an die Session gebunden; im Einzelmodus
        ohne Sessions gibt es nichts zu binden.
        """
        if not userctx.auth_enabled():
            return True
        token = post_data.get('csrf', [''])[0] if post_data else ''
        if not token:
            token = self.headers.get('X-CSRF-Token', '')
        if not auth.check_csrf(self._session_token(), token):
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
        path = self.path.split('?', 1)[0]
        if path in ('/buch.css', '/favicon.ico'):
            return True
        mode = userctx.get_mode()
        if mode is None:
            # Noch nicht eingerichtet: ausschließlich die Modus-Auswahl zulassen –
            # sonst entstünde (im Einzelmodus-Fallback) ein verwaistes data/buch.db,
            # falls anschließend „Mehrbenutzer" gewählt wird.
            if path in ('/setup', '/setup/mode'):
                return True
            self._drain_body()
            self.respond(303, "", headers={"Location": "/setup"})
            return False
        if mode != 'multi':
            return True                                  # Einzelbenutzer-Modus
        # Bootstrap: ohne jeden Benutzer nur die Admin-Ersteinrichtung zulassen
        if not auth.has_any_user():
            if path == '/setup-admin':
                return True
            self._drain_body()
            self.respond(303, "", headers={"Location": "/setup-admin"})
            return False
        user = auth.get_session_user(self._session_token())
        if user:
            userctx.set_user(user)
            userctx.set_csrf_token(auth.csrf_for(self._session_token()))
            return True
        if path in ('/login', '/logout'):
            return True                                  # Anmeldeseite / Logout
        self._drain_body()
        self.respond(303, "", headers={"Location": "/login"})
        return False

    def _auth_routes_get(self):
        """Behandelt GET /login, /logout, /setup-admin. True, wenn erledigt."""
        if not userctx.auth_enabled():
            return False                                 # Login-UI nur im Mehrbenutzer-Modus
        path = self.path.split('?', 1)[0]
        if path == '/login':
            self.respond(200, PageLogin())
            return True
        if path == '/setup-admin':
            self.respond(200, PageSetupAdmin())
            return True
        if path == '/logout':
            auth.delete_session(self._session_token())
            self.respond(303, "", headers={"Location": "/login",
                                            "Set-Cookie": self._logout_cookie_header()})
            return True
        return False

    def _auth_routes_post(self):
        """Behandelt POST /login, /setup-admin. True, wenn erledigt."""
        if not userctx.auth_enabled():
            return False                                 # Login-UI nur im Mehrbenutzer-Modus
        path = self.path.split('?', 1)[0]
        if path not in ('/login', '/setup-admin'):
            return False
        length = int(self.headers.get('Content-Length', 0) or 0)
        post_data = parse_qs(self.rfile.read(length).decode('utf-8')) if length else {}
        if path == '/login':
            ok, username, err = handlers.handle_login(post_data)
            if ok:
                token = auth.create_session(username)
                self.respond(303, "", headers={"Location": "/",
                             "Set-Cookie": self._session_cookie_header(token)})
            else:
                self.respond(200, PageLogin(error_msg=err, username=username))
            return True
        # /setup-admin
        ok, username, err = handlers.handle_setup_admin(post_data)
        if ok:
            token = auth.create_session(username)
            self.respond(303, "", headers={"Location": "/",
                         "Set-Cookie": self._session_cookie_header(token)})
        else:
            self.respond(200, PageSetupAdmin(error_msg=err, username=username))
        return True

    def do_GET(self):
        if not self._gate():
            return
        if self._auth_routes_get():
            userctx.clear()
            return
        # Im Mehrbenutzer-Modus ohne angemeldeten Nutzer keine (globale) DB anlegen –
        # hier sind nur public/statische Routen erreichbar (z. B. /buch.css). Sonst
        # entstünde fälschlich ein data/buch.db.
        db = self._maybe_db()
        try:
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
                # Erststart: solange der Betriebsmodus noch nicht gewählt wurde,
                # die Modus-Auswahl zeigen; danach das gewohnte Kontaktformular.
                if userctx.get_mode() is None:
                    self.respond(200, PageSetupModeChoice())
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
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageInvoice(db, filters, invoice_id), headers=hdrs)
            elif self.path.startswith("/invoice/view") or self.path.startswith("/invoice/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                invoice_id = int(query_components["id"][0])
                self.respond(200, PageInvoice(db, {}, invoice_id))
            elif self.path.startswith("/invoice/xml"):
                # Generate and download XRechnung XML
                query_components = parse_qs(self.path.split('?')[1])
                invoice_id = int(query_components["id"][0])
                
                invoice = db.get_invoice_by_id(invoice_id)
                if invoice:
                    from export.xrechnung_invoice import XRechnungGenerator
                    items = db.get_invoice_items(invoice_id)
                    generator = XRechnungGenerator()
                    xml_content = generator.generate_xml(invoice, items)
                    
                    # Namenskonvention "[Rechnungsnummer] [Kundenname]" (todo #1)
                    from export.pdf_core import safe_filename_component
                    invoice_number = invoice[1] or 'DRAFT'
                    customer = safe_filename_component(invoice[15] or invoice[14])
                    parts = [safe_filename_component(invoice_number), customer]
                    filename = " ".join(p for p in parts if p) + ".xml"

                    self.send_response(200)
                    self.send_header("Content-type", "application/xml")
                    self.send_header("Content-Disposition", content_disposition('attachment', filename))
                    self.send_header("Content-Length", str(len(xml_content.encode('utf-8'))))
                    self.end_headers()
                    self.wfile.write(xml_content.encode('utf-8'))
                else:
                    self.send_response(404)
                    self.end_headers()
                return
            elif self.path.startswith("/invoice/pdf_download"):
                # PDF einer Rechnung ausliefern. inline=1 -> im Browser anzeigen
                # (neuer Tab), sonst Download.
                query_components = parse_qs(self.path.split('?')[1])
                invoice_id = int(query_components["id"][0])
                disposition = 'inline' if query_components.get('inline') else 'attachment'

                pdf_bytes, filename = handlers.handle_invoice_pdf_by_id(invoice_id)

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
                    self.wfile.write(b"Error generating PDF")
                return
            elif self.path.startswith("/invoice/pdf_generate"):
                # Generate PDF in filesystem only (no download)
                query_components = parse_qs(self.path.split('?')[1])
                invoice_id = int(query_components["id"][0])
                
                from export.pdf_invoice import generate_invoice_pdf
                
                pdf_bytes, pdf_path = generate_invoice_pdf(db, invoice_id)
                
                if pdf_bytes and pdf_path:
                    import json
                    response = json.dumps({
                        'success': True,
                        'pdf_path': pdf_path,
                        'message': 'PDF erfolgreich erstellt'
                    })
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.send_header("Content-Length", str(len(response.encode())))
                    self.end_headers()
                    self.wfile.write(response.encode())
                else:
                    import json
                    response = json.dumps({
                        'success': False,
                        'error': 'Fehler beim Erstellen der PDF'
                    })
                    self.send_response(500)
                    self.send_header("Content-type", "application/json")
                    self.send_header("Content-Length", str(len(response.encode())))
                    self.end_headers()
                    self.wfile.write(response.encode())
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
                self.respond(200, PageQuote(db, filters, quote_id))
            elif self.path.startswith("/quote/edit"):
                qs = parse_qs(self.path.split('?')[1])
                self.respond(200, PageQuote(db, {}, int(qs["id"][0])))
            elif self.path.startswith("/quote/delete"):
                qs = parse_qs(self.path.split('?')[1])
                db.delete_quote(int(qs["id"][0]))
                self.respond(303, "", headers={"Location": "/quote"})
            elif self.path.startswith("/quote/pdf_download"):
                qs = parse_qs(self.path.split('?')[1])
                disposition = 'inline' if qs.get('inline') else 'attachment'
                pdf_bytes, filename = handlers.handle_quote_pdf_by_id(int(qs["id"][0]))
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
                    self.wfile.write(b"Error generating PDF")
                return
            elif self.path.startswith("/quote/pdf_generate"):
                import json as _json
                qs = parse_qs(self.path.split('?')[1])
                from export.pdf_quote import generate_quote_pdf
                pdf_bytes, pdf_path = generate_quote_pdf(db, int(qs["id"][0]))
                if pdf_bytes and pdf_path:
                    response = _json.dumps({'success': True, 'pdf_path': pdf_path})
                    self.send_response(200)
                else:
                    response = _json.dumps({'success': False, 'error': 'Fehler beim Erstellen der PDF'})
                    self.send_response(500)
                self.send_header("Content-type", "application/json")
                self.send_header("Content-Length", str(len(response.encode())))
                self.end_headers()
                self.wfile.write(response.encode())
                return
            elif self.path == "/dashboard":
                self.respond(200, PageDashboard(db))
            # ── Master Data (Stammdaten) ──────────────────────────────────
            elif self.path == "/masterdata":
                self.respond(200, PageMasterData(db))
            # Articles
            elif self.path == "/masterdata/articles":
                self.respond(200, PageArticles(db))
            elif self.path.startswith("/masterdata/articles/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                article_id = int(query_components["id"][0])
                self.respond(200, PageArticleEdit(db, article_id))
            elif self.path.startswith("/masterdata/articles/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                article_id = int(query_components["id"][0])
                db.delete_article(article_id)
                self.respond(303, "", headers={"Location": "/masterdata/articles"})
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
            elif self.path.startswith("/masterdata/contacts/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                contact_id = int(query_components["id"][0])
                db.delete_contact(contact_id)
                self.respond(303, "", headers={"Location": "/masterdata/contacts"})
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
            elif self.path.startswith("/masterdata/skr/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                id = int(query_components["id"][0])
                status_code, location = handlers.handle_delete_skr(db, id)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path.startswith("/masterdata/skr/togglemenu"):
                query_components = parse_qs(self.path.split('?')[1])
                id = int(query_components["id"][0])
                status_code, location = handlers.handle_toggle_skr_menu(db, id)
                self.respond(status_code, "", headers={"Location": location})
            # Bank Accounts
            elif self.path == "/masterdata/bankaccounts":
                self.respond(200, PageBankAccounts(db))
            elif self.path.startswith("/masterdata/bankaccounts/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                account_id = int(query_components["id"][0])
                self.respond(200, PageBankAccountEdit(db, account_id))
            elif self.path.startswith("/masterdata/bankaccounts/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                account_id = int(query_components["id"][0])
                db.delete_account(account_id)
                self.respond(303, "", headers={"Location": "/masterdata/bankaccounts"})
            # ──────────────────────────────────────────────────────────────
            elif self.path == "/miscellaneous" or self.path.startswith("/miscellaneous?"):
                self.respond(200, PageMiscellaneous(db))
            elif self.path == "/receipts" or self.path.startswith("/receipts?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageReceipts(db, date_from, date_to), headers=hdrs)
            elif self.path.startswith("/receipts/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                number = query_components["number"][0]
                self.respond(200, PageReceiptEdit(db, number))
            elif self.path.startswith("/receipts/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                number = query_components["number"][0]
                db.delete_receipt(number)
                self.respond(303, "", headers={"Location": "/receipts"})
            elif self.path == "/transactions" or self.path.startswith("/transactions?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageTransactions(db, date_from=date_from, date_to=date_to), headers=hdrs)
            elif self.path.startswith("/transactions/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                transaction_id = int(query_components["id"][0])
                date_from, date_to, _ = resolve_period(query_components, self.headers.get('Cookie'))
                self.respond(200, PageTransactions(db, edit_transaction_id=transaction_id,
                                                   date_from=date_from, date_to=date_to))
            elif self.path.startswith("/transactions/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                transaction_id = int(query_components["id"][0])
                db.delete_transaction(transaction_id)
                self.respond(303, "", headers={"Location": "/transactions"})
            elif self.path == "/bookinggroups" or self.path.startswith("/bookinggroups?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageBookingGroups(db, date_from, date_to), headers=hdrs)
            elif self.path.startswith("/bookinggroups/view"):
                query_components = parse_qs(self.path.split('?')[1])
                group_id = int(query_components["id"][0])
                self.respond(200, PageBookingGroupDetails(db, group_id))
            elif self.path.startswith("/bookinggroups/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                group_id = int(query_components["id"][0])
                status, location = handlers.handle_delete_booking_group(db, group_id)
                self.respond(status, "", headers={"Location": location})
            elif self.path.startswith("/bookinggroups/unlink_booking"):
                query_components = parse_qs(self.path.split('?')[1])
                booking_id = int(query_components["booking_id"][0])
                grp_id     = int(query_components["group_id"][0])
                status, location = handlers.handle_unlink_booking_from_group(db, booking_id, grp_id)
                self.respond(status, "", headers={"Location": location})
            elif self.path.startswith("/documents/unlink"):
                query_components = parse_qs(self.path.split('?')[1])
                doc_id = int(query_components["doc_id"][0])
                booking_id = int(query_components["booking_id"][0])
                db.unlink_booking_from_document(booking_id, doc_id)
                # Redirect back to referrer or transactions
                self.respond(303, "", headers={"Location": self.headers.get('Referer', '/transactions')})
            elif self.path == "/masterdata/numberranges":
                self.respond(200, PageNumberRanges(db))
            elif self.path.startswith("/masterdata/numberranges/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                range_id = int(query_components["id"][0])
                self.respond(200, PageNumberRangesEdit(db, range_id))
            elif self.path.startswith("/masterdata/numberranges/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                range_id = int(query_components["id"][0])
                db.delete_number_range(range_id)
                self.respond(303, "", headers={"Location": "/masterdata/numberranges"})
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
                self.respond(200, PageAssetView(db, asset_id))
            elif self.path.startswith("/assets/delete"):
                qc = parse_qs(self.path.split('?', 1)[1])
                asset_id = int(qc["id"][0])
                db.delete_asset(asset_id)
                self.respond(303, "", headers={"Location": "/assets"})
            elif self.path == "/asset_categories" or self.path.startswith("/asset_categories?"):
                self.respond(200, PageAssetCategories(db))
            elif self.path.startswith("/asset_categories/edit"):
                qc = parse_qs(self.path.split('?', 1)[1])
                cat_id = int(qc["id"][0])
                self.respond(200, PageAssetCategoryEdit(db, cat_id))
            elif self.path.startswith("/asset_categories/delete"):
                qc = parse_qs(self.path.split('?', 1)[1])
                cat_id = int(qc["id"][0])
                db.delete_asset_category(cat_id)
                self.respond(303, "", headers={"Location": "/asset_categories"})
            # ── Zeiten / Arbeitszeiten ────────────────────────────────────
            elif self.path == "/worktime" or self.path.startswith("/worktime?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                person_id = int(qs['person'][0]) if qs.get('person') else None
                error_msg = qs['error'][0] if qs.get('error') else None
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageWorkTimes(db, person_id=person_id,
                                                date_from=date_from, date_to=date_to,
                                                error_msg=error_msg), headers=hdrs)
            elif self.path.startswith("/worktime/edit"):
                qs = parse_qs(self.path.split('?')[1])
                edit_id = int(qs["id"][0])
                person_id = int(qs['person'][0]) if qs.get('person') else None
                date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
                self.respond(200, PageWorkTimes(db, person_id=person_id, date_from=date_from,
                                                date_to=date_to, edit_id=edit_id))
            elif self.path.startswith("/worktime/delete"):
                qs = parse_qs(self.path.split('?')[1])
                worktime_id = int(qs["id"][0])
                db.delete_worktime(worktime_id)
                person_id = qs['person'][0] if qs.get('person') else ''
                date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
                self.respond(303, "", headers={"Location":
                    f"/worktime?person={person_id}&from={date_from}&to={date_to}"})
            elif self.path.startswith("/worktime/pdf"):
                qs = parse_qs(self.path.split('?')[1])
                person_id = qs["person"][0]
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
            # ── Benutzerverwaltung (nur Admin, Mehrbenutzer-Modus) ────────
            elif self.path == "/users" or self.path.startswith("/users?"):
                if not self._require_admin():
                    return
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                info = qs['info'][0] if qs.get('info') else None
                err = qs['err'][0] if qs.get('err') else None
                csrf = auth.csrf_for(self._session_token())
                self.respond(200, PageUsers(userctx.get_user(), auth.list_users(),
                                            csrf, error_msg=err, info_msg=info))
            # ── Zeiten / Fahrten ──────────────────────────────────────────
            elif self.path == "/trips" or self.path.startswith("/trips?"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                date_from, date_to, set_cookie = resolve_period(qs, self.headers.get('Cookie'))
                person_id = int(qs['person'][0]) if qs.get('person') else None
                error_msg = qs['error'][0] if qs.get('error') else None
                hdrs = {"Set-Cookie": period_cookie_header(date_from, date_to)} if set_cookie else None
                self.respond(200, PageTrips(db, person_id=person_id,
                                            date_from=date_from, date_to=date_to,
                                            error_msg=error_msg), headers=hdrs)
            elif self.path.startswith("/trips/edit"):
                qs = parse_qs(self.path.split('?')[1])
                edit_id = int(qs["id"][0])
                person_id = int(qs['person'][0]) if qs.get('person') else None
                date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
                self.respond(200, PageTrips(db, person_id=person_id, date_from=date_from,
                                            date_to=date_to, edit_id=edit_id))
            elif self.path.startswith("/trips/delete"):
                qs = parse_qs(self.path.split('?')[1])
                trip_id = int(qs["id"][0])
                db.delete_trip(trip_id)
                person_id = qs['person'][0] if qs.get('person') else ''
                date_from, date_to, _ = resolve_period(qs, self.headers.get('Cookie'))
                self.respond(303, "", headers={"Location":
                    f"/trips?person={person_id}&from={date_from}&to={date_to}"})
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
                # (Single-User: ./data/logos). Es wird nur der Dateiname genutzt,
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
        db = self._maybe_db()

        # ── CSRF für fetch-Routen (Body wird erst im Handler gelesen): Token
        # kommt als X-CSRF-Token-Header (Footer-JS umhüllt window.fetch).
        # /wiso/import ist ein klassisches Multipart-Formular und prüft das
        # csrf-Feld selbst im Handler; alle übrigen Formular-Routen werden
        # unten nach parse_qs über das Feld 'csrf' geprüft.
        if self.path in ("/upload_receipts", "/masterdata/contacts/upload-logo",
                         "/invoice/save", "/invoice/send-email", "/invoice/link-payment",
                         "/invoice/delete-payment", "/invoice/status",
                         "/quote/save", "/quote/status"):
            if not self._check_csrf():
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

        # Handle invoice save / update
        if self.path == "/invoice/save":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            response_data = handlers.handle_invoice_save(post_body)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", str(len(response_data)))
            self.end_headers()
            self.wfile.write(response_data)
            return

        # Handle invoice email sending
        if self.path == "/invoice/send-email":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            response_data = handlers.handle_send_invoice_email(post_body)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", str(len(response_data)))
            self.end_headers()
            self.wfile.write(response_data)
            return
        
        # Handle invoice payment linking
        if self.path == "/invoice/link-payment":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            status_code, redirect_path = handlers.handle_link_invoice_payment(post_body)
            body = b'{"success": true}' if status_code == 200 else f'{{"error": "status {status_code}"}}'.encode()
            self.send_response(status_code)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Handle invoice payment deletion
        if self.path == "/invoice/delete-payment":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            status_code, msg = handlers.handle_delete_invoice_payment(post_body)
            body = b'{"success": true}' if status_code == 200 else f'{{"error": "{msg}"}}'.encode()
            self.send_response(status_code)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        
        # Handle invoice status updates
        if self.path == "/invoice/status":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            status_code, response_body = handlers.handle_update_invoice_status(post_body)
            self.respond(status_code, response_body, content_type="application/json")
            return

        # Handle quote save / update
        if self.path == "/quote/save":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            response_data = handlers.handle_quote_save(post_body)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Content-Length", str(len(response_data)))
            self.end_headers()
            self.wfile.write(response_data)
            return

        # Handle quote status updates
        if self.path == "/quote/status":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            status_code, response_body = handlers.handle_update_quote_status(post_body)
            self.respond(status_code, response_body, content_type="application/json")
            return

        # Parse regular form data
        content_length = int(self.headers['Content-Length'])
        post_body = self.rfile.read(content_length).decode('utf-8')
        post_data = parse_qs(post_body)

        # ── CSRF für alle Formular-Routen (Feld 'csrf' oder Header) ───────────
        if not self._check_csrf(post_data):
            return

        # Route to appropriate handler
        try:
            # ── Benutzerverwaltung (nur Admin) ────────────────────────────
            if self.path in ("/users/create", "/users/delete",
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
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/bookinggroups/create":
                status_code, location = handlers.handle_create_booking_group(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/bookinggroups/update":
                status_code, location = handlers.handle_update_booking_group(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/documents/link":
                status_code, location = handlers.handle_link_document(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/masterdata/bankaccounts/add":
                status_code, location = handlers.handle_add_bankaccount(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/bankaccounts"})
            elif self.path == "/masterdata/bankaccounts/update":
                status_code, location = handlers.handle_update_bankaccount(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/bankaccounts"})
            elif self.path == "/masterdata/numberranges/add":
                status_code, location = handlers.handle_add_number_range(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/numberranges"})
            elif self.path == "/masterdata/numberranges/update":
                status_code, location = handlers.handle_update_number_range(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/numberranges"})
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
                self.respond(status_code, "", headers={"Location": "/masterdata/articles"})
            elif self.path == "/masterdata/articles/update":
                status_code, location = handlers.handle_update_article(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/articles"})
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
                self.respond(status_code, "", headers={"Location": "/masterdata/articles"})
            elif self.path == "/articles/update":
                status_code, location = handlers.handle_update_article(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/articles"})
            # ──────────────────────────────────────────────────────────────
            elif self.path == "/execute_sql":
                content = handlers.handle_execute_sql(db, post_data)
                self.respond(200, content, content_type="application/json")
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
            elif self.path == "/setup/mode":
                location = handlers.handle_setup_mode(post_data)
                self.respond(303, "", headers={"Location": location})
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

# Im Einzelmodus benötigte Ausgabe-Unterordner (Jahres-Unterordner werden bei
# Bedarf von den jeweiligen Generatoren erzeugt). Wer bei laufendem Server Ordner
# löscht, muss nicht abgefangen werden.
_SINGLE_SUBDIRS = ("logos", "invoices", "quotes", "worktime")


def _ensure_directories():
    """Fehlende Ausgabe-Verzeichnisse anlegen (idempotent).

    Im Mehrbenutzer-Modus werden keine globalen Typ-Verzeichnisse angelegt – die
    Dateien jedes Nutzers entstehen bei Bedarf unter data/users/<user>/.
    """
    import os
    root = userctx.DATA_ROOT
    os.makedirs(root, exist_ok=True)
    mode = userctx.get_mode()
    if mode == "multi":
        os.makedirs(os.path.join(root, "users"), exist_ok=True)
    elif mode == "single":
        for sub in _SINGLE_SUBDIRS:
            os.makedirs(os.path.join(root, sub), exist_ok=True)
    # mode is None (noch nicht eingerichtet): nur die Datenwurzel anlegen –
    # die Typ-Verzeichnisse entstehen nach der Modus-Wahl bei Bedarf.


# Start web server
def run_server(host=None, port=None, certfile=None, keyfile=None):
    """Web-Server starten.

    Defaults kommen aus Umgebungsvariablen (für Deployment im LAN/VM):
      UBUCHHALTUNG_HOST (Default 'localhost'; '0.0.0.0' für Netzzugriff),
      UBUCHHALTUNG_PORT (Default 8080),
      UBUCHHALTUNG_CERT / UBUCHHALTUNG_KEY (PEM-Dateien ⇒ HTTPS, self-signed möglich).

    Der Betriebsmodus (Einzel-/Mehrbenutzer) wird bei der Ersteinrichtung gewählt
    und in data/config.json gespeichert (für Headless-Setup ggf. vorab anlegen).
    """
    import socket, sys
    host = host or os.environ.get("UBUCHHALTUNG_HOST", "localhost")
    port = int(port or os.environ.get("UBUCHHALTUNG_PORT", "8080"))
    certfile = certfile or os.environ.get("UBUCHHALTUNG_CERT")
    keyfile = keyfile or os.environ.get("UBUCHHALTUNG_KEY")

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
