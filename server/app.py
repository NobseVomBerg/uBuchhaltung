"""
Main HTTP server class with routing
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from db import Database
import os

from . import pages
from . import handlers
from . import upload_handler
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

class SimpleWebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        db = Database()
        try:
            if self.path == "/":
                self.respond(200, PageDashboard(db))
            elif self.path == "/dashboard":
                self.respond(200, PageDashboard(db))
            elif self.path == "/about":
                self.respond(200, pages.PageAbout())
            elif self.path == "/invoice" or self.path.startswith("/invoice?"):
                # Parse query parameters for filters
                filters = {}
                if '?' in self.path:
                    query_string = self.path.split('?', 1)[1]
                    query_components = parse_qs(query_string)
                    if 'search' in query_components:
                        filters['search'] = query_components['search'][0]
                    if 'status' in query_components:
                        filters['status'] = query_components['status'][0]
                    if 'date_from' in query_components:
                        filters['date_from'] = query_components['date_from'][0]
                    if 'date_to' in query_components:
                        filters['date_to'] = query_components['date_to'][0]
                self.respond(200, pages.PageInvoice(db, filters))
            elif self.path == "/invoice/new":
                self.respond(200, pages.PageInvoiceNew(db))
            elif self.path.startswith("/invoice/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                invoice_id = int(query_components["id"][0])
                self.respond(200, pages.PageInvoiceNew(db, invoice_id))
            elif self.path.startswith("/invoice/view"):
                # Redirect to edit page (view and edit are the same now)
                query_components = parse_qs(self.path.split('?')[1])
                invoice_id = int(query_components["id"][0])
                self.respond(200, pages.PageInvoiceNew(db, invoice_id))
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
                self.respond(200, pages.PageDashboard(db))
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
                id = query_components["id"][0]
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
            elif self.path == "/receipts":
                self.respond(200, pages.PageReceipts(db))
            elif self.path.startswith("/receipts/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                number = query_components["number"][0]
                self.respond(200, pages.PageReceiptEdit(db, number))
            elif self.path.startswith("/receipts/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                number = query_components["number"][0]
                db.delete_receipt(number)
                self.respond(303, "", headers={"Location": "/receipts"})
            elif self.path == "/transactions":
                self.respond(200, PageTransactions(db))
            elif self.path.startswith("/transactions/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                transaction_id = int(query_components["id"][0])
                self.respond(200, PageTransactions(db, edit_transaction_id=transaction_id))
            elif self.path.startswith("/transactions/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                transaction_id = int(query_components["id"][0])
                db.delete_transaction(transaction_id)
                self.respond(303, "", headers={"Location": "/transactions"})
            elif self.path == "/bookinggroups":
                self.respond(200, PageBookingGroups(db))
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
            elif self.path.startswith("/static/"):
                # Serve static files (e.g., logo)
                filename = self.path[1:]  # Remove leading /
                self.serve_static_file(filename, self.guess_content_type(filename))
            else:
                self.respond(404, "Seite nicht gefunden.")
        except Exception as e:
            import traceback
            traceback.print_exc()
            error_msg = f"<h1>Server Fehler</h1><pre>{str(e)}\n\n{traceback.format_exc()}</pre>"
            self.respond(500, error_msg)

    def serve_static_file(self, filename, content_type):
        try:
            with open(filename, 'rb') as file:
                self.send_response(200)
                self.send_header("Content-type", content_type)
                self.end_headers()
                self.wfile.write(file.read())
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

            response_data = handlers.handle_invoice_save(post_body)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
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
            self.end_headers()
            self.wfile.write(response_data)
            return
        
        # Handle invoice payment linking
        if self.path == "/invoice/link-payment":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            status_code, redirect_path = handlers.handle_link_invoice_payment(post_body)
            self.send_response(status_code)
            if status_code in [200, 303]:
                self.send_header("Content-type", "application/json")
            self.end_headers()
            if status_code == 200:
                self.wfile.write(b'{"success": true}')
            return
        
        # Handle invoice status updates
        if self.path == "/invoice/status":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            status_code, redirect_path = handlers.handle_update_invoice_status(post_body)
            self.send_response(status_code)
            if status_code == 303:
                self.send_header("Location", redirect_path)
            self.end_headers()
            return
        
        # Handle PDF generation (JSON body)
        if self.path == "/invoice/pdf":
            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
            pdf_data = handlers.handle_generate_invoice_pdf(post_body)
            self.send_response(200)
            self.send_header("Content-type", "application/pdf")
            self.send_header("Content-Disposition", "attachment; filename=invoice.pdf")
            self.send_header("Content-Length", str(len(pdf_data)))
            self.end_headers()
            self.wfile.write(pdf_data)
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
                self.respond(200, content)
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
            else:
                self.respond(404, "Seite nicht gefunden.")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.respond(500, f"Fehler: {str(e)}")

    def respond(self, status_code, content, headers=None):
        # Send HTTP status and headers
        self.send_response(status_code)
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        # Write content to response
        if content:
            self.wfile.write(content.encode("utf-8"))

# Start web server
def run_server(host="localhost", port=8080):
    #db = Database()
    server_address = (host, port)
    httpd = HTTPServer(server_address, SimpleWebServer)
    print(f"Starting server on {host}:{port}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()
