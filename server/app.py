"""
Main HTTP server class with routing
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pydoc import html
from urllib.parse import parse_qs
from db import Database
import os

from . import pages
from . import handlers
from . import upload_handler
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
from .pages_transactions import PageTransactions, PageConfirmTransactions
from .pages_dashboard import PageDashboard
from .pages_receipts import PageReceipts, PageReceiptEdit
from .pages_setup import PageSetup
from .pages_invoice import PageInvoice

class SimpleWebServer(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def end_headers(self):
        if self.path.endswith('.css') or self.path.endswith('.js') or self.path.endswith('.png'):
            self.send_header('Cache-Control', 'public, max-age=86400')  # Cache this File for 1d
        else:
            self.send_header('Cache-Control', 'no-cache')               # HTML etc no caching
            
        super().end_headers()                                           # Call the Base

    def do_GET(self):
        db = Database()
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
                    from xrechnung_generator import XRechnungGenerator
                    items = db.get_invoice_items(invoice_id)
                    generator = XRechnungGenerator()
                    xml_content = generator.generate_xml(invoice, items)
                    
                    invoice_number = invoice[1] or 'DRAFT'
                    filename = f"XRechnung_{invoice_number}.xml"
                    
                    self.send_response(200)
                    self.send_header("Content-type", "application/xml")
                    self.send_header("Content-Disposition", f"attachment; filename={filename}")
                    self.send_header("Content-Length", str(len(xml_content.encode('utf-8'))))
                    self.end_headers()
                    self.wfile.write(xml_content.encode('utf-8'))
                else:
                    self.send_response(404)
                    self.end_headers()
                return
            elif self.path.startswith("/invoice/pdf_download"):
                # Generate and download PDF for existing invoice
                query_components = parse_qs(self.path.split('?')[1])
                invoice_id = int(query_components["id"][0])
                
                pdf_bytes, filename = handlers.handle_invoice_pdf_by_id(invoice_id)
                
                if pdf_bytes:
                    self.send_response(200)
                    self.send_header("Content-type", "application/pdf")
                    self.send_header("Content-Disposition", f"attachment; filename={filename}")
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
                
                from pdf_generator import generate_invoice_pdf
                
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
                c_type  = qs.get('type',   [None])[0]
                c_entity= qs.get('entity', [None])[0]
                self.respond(200, PageContacts(db, contact_type_filter=c_type, entity_type_filter=c_entity))
            elif self.path.startswith("/masterdata/contacts/new"):
                qs = parse_qs(self.path.split('?')[1]) if '?' in self.path else {}
                entity = qs.get('entity', ['company'])[0]
                self.respond(200, PageContactNew(db, entity_type=entity))
            elif self.path.startswith("/masterdata/contacts/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                contact_id = int(query_components["id"][0])
                self.respond(200, PageContactEdit(db, contact_id))
            elif self.path.startswith("/masterdata/contacts/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                contact_id = int(query_components["id"][0])
                db.delete_contact(contact_id)
                self.respond(303, "", headers={"Location": "/masterdata/contacts"})
            # SKR (Chart of Accounts)
            elif self.path == "/masterdata/skr":
                self.respond(200, PageSkr(db))
            elif self.path.startswith("/masterdata/skr/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                id = int(query_components["id"][0])
                self.respond(200, PageSkrEdit(db, id))
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
            elif self.path.startswith("/confirm_transactions"):
                query_components = parse_qs(self.path.split('?')[1])
                import_id = query_components["import_id"][0]
                self.respond(200, PageConfirmTransactions(import_id))
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
            elif self.path == "/buch.css":
                self.serve_static_file("buch.css", "text/css")
            elif self.path == "/favicon.ico":
                self.serve_static_file("favicon.ico", "image/x-icon")
            elif self.path.startswith("/seed_data/private/"):
                # Serve private static files (e.g., logo)
                filename = self.path[1:]  # Remove leading /
                self.serve_static_file(filename, self.guess_content_type(filename))
            else:
                self.respond(404, "Seite nicht gefunden.")
        except Exception as e:
            import traceback
            traceback.print_exc()
            error_msg = f"<h1>Server Fehler</h1><pre>{html.escape(str(e))}\n\n{html.escape(traceback.format_exc())}</pre>"
            self.respond(500, error_msg)

    def serve_static_file(self, filename, content_type):
        try:
            file_mtime = os.path.getmtime(filename)
            file_mtime_string  = self.date_time_string(file_mtime)

            if_modified_since = self.headers.get('If-Modified-Since')
            #print(f"[CACHE] {filename}: IMS={if_modified_since!r}  LM={file_mtime_string!r}  match={if_modified_since is not None and if_modified_since.strip() == file_mtime_string}")
            if if_modified_since and if_modified_since.strip() == file_mtime_string:
                self.send_response(304)
                self.end_headers()
                return

            with open(filename, 'rb') as file:
                data = file.read()
                self.send_response(200)
                self.send_header("Content-type", content_type)
                self.send_header("Last-Modified", file_mtime_string)
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
        db = Database()
        
        # Handle file upload separately
        if self.path == "/upload_receipts":
            status_code, response = upload_handler.handle_file_upload(self)
            self.respond(status_code, response)
            return

        # Handle WISO CSV import (multipart file upload)
        if self.path == "/wiso/import":
            status_code, location = handlers.handle_wiso_import(self, db)
            self.respond(status_code, "", headers={"Location": location})
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
        
        # Parse regular form data
        content_length = int(self.headers['Content-Length'])
        post_body = self.rfile.read(content_length).decode('utf-8')
        post_data = parse_qs(post_body)
        
        # Route to appropriate handler
        try:
            if self.path == "/add_receipt":
                status_code, location = handlers.handle_add_receipt(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/update_receipt":
                status_code, location = handlers.handle_update_receipt(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/confirm_transactions":
                status_code, response = handlers.handle_confirm_import(db, post_data)
                if status_code == 303:
                    self.respond(status_code, "", headers={"Location": response})
                else:
                    self.respond(status_code, response)
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
                self.respond(status_code, "", headers={"Location": "/masterdata/skr"})
            elif self.path == "/masterdata/skr/update":
                status_code, location = handlers.handle_update_skr(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/skr"})
            # Contacts
            elif self.path == "/masterdata/contacts/add":
                status_code, location = handlers.handle_add_contact(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/contacts"})
            elif self.path == "/masterdata/contacts/update":
                status_code, location = handlers.handle_update_contact(db, post_data)
                self.respond(status_code, "", headers={"Location": "/masterdata/contacts"})
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
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.respond(500, f"Fehler: {str(e)}")

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

# Start web server
def run_server(host="localhost", port=8080):
    import socket, sys
    # Prüfen ob der Port schon belegt ist
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        if _s.connect_ex((host, port)) == 0:
            print(f"\nFEHLER: Port {port} ist bereits belegt – läuft noch eine andere Server-Instanz?")
            print(f"  Windows:  Stop-Process -Id (netstat -ano | findstr :{port})")
            print(f"  Unix:     kill $(lsof -ti:{port})")
            sys.exit(1)

    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, SimpleWebServer)
    print(f"Starting server on {host}:{port}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()
