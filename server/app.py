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

class SimpleWebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        db = Database()
        try:
            if self.path == "/":
                self.respond(200, pages.PageRoot(db))
            elif self.path == "/about":
                self.respond(200, pages.PageAbout())
            elif self.path == "/settings":
                self.respond(200, pages.PageSettings())
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
                self.respond(200, pages.PageTransactions(db))
            elif self.path.startswith("/transactions/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                transaction_id = int(query_components["id"][0])
                self.respond(200, pages.PageTransactions(db, edit_transaction_id=transaction_id))
            elif self.path.startswith("/transactions/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                transaction_id = int(query_components["id"][0])
                db.delete_transaction(transaction_id)
                self.respond(303, "", headers={"Location": "/transactions"})
            elif self.path == "/bookinggroups":
                self.respond(200, pages.PageBookingGroups(db))
            elif self.path.startswith("/bookinggroups/view"):
                query_components = parse_qs(self.path.split('?')[1])
                group_id = int(query_components["id"][0])
                self.respond(200, pages.PageBookingGroupDetails(db, group_id))
            elif self.path.startswith("/documents/unlink"):
                query_components = parse_qs(self.path.split('?')[1])
                doc_id = int(query_components["doc_id"][0])
                booking_id = int(query_components["booking_id"][0])
                db.unlink_booking_from_document(booking_id, doc_id)
                # Redirect back to referrer or transactions
                self.respond(303, "", headers={"Location": self.headers.get('Referer', '/transactions')})
            elif self.path == "/skr":
                self.respond(200, pages.PageSkr(db))
            elif self.path.startswith("/contacts"):
                if self.path == "/contacts" or self.path.startswith("/contacts?"):
                    self.respond(200, pages.PageContacts(db))
                elif self.path.startswith("/contacts/edit"):
                    query_components = parse_qs(self.path.split('?')[1])
                    contact_id = int(query_components["id"][0])
                    self.respond(200, pages.PageContactEdit(db, contact_id))
                elif self.path.startswith("/contacts/delete"):
                    query_components = parse_qs(self.path.split('?')[1])
                    contact_id = int(query_components["id"][0])
                    db.delete_contact(contact_id)
                    self.respond(303, "", headers={"Location": "/contacts"})
            elif self.path == "/settings/bankaccounts":
                self.respond(200, pages.PageSettingsBankAccounts(db))
            elif self.path.startswith("/settings/bankaccounts/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                account_id = int(query_components["id"][0])
                self.respond(200, pages.PageSettingsBankAccountEdit(db, account_id))
            elif self.path.startswith("/settings/bankaccounts/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                account_id = int(query_components["id"][0])
                db.delete_account(account_id)
                self.respond(303, "", headers={"Location": "/settings/bankaccounts"})
            elif self.path.startswith("/edit_skr"):
                query_components = parse_qs(self.path.split('?')[1])
                id = query_components["id"][0]
                self.respond(200, pages.PageSkrEdit(db, id))
            elif self.path.startswith("/confirm_transactions"):
                query_components = parse_qs(self.path.split('?')[1])
                import_id = query_components["import_id"][0]
                self.respond(200, pages.PageConfirmTransactions(import_id))
            elif self.path == "/buch.css":
                self.serve_static_file("buch.css", "text/css")
            elif self.path == "/favicon.ico":
                self.serve_static_file("favicon.ico", "image/x-icon")
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

    def do_POST(self):
        db = Database()
        
        # Handle file upload separately
        if self.path == "/upload_receipts":
            status_code, response = upload_handler.handle_file_upload(self)
            self.respond(status_code, response)
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
            elif self.path == "/documents/link":
                status_code, location = handlers.handle_link_document(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/settings/bankaccounts/add":
                status_code, location = handlers.handle_add_bankaccount(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/settings/bankaccounts/update":
                status_code, location = handlers.handle_update_bankaccount(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/add_skr":
                status_code, location = handlers.handle_add_skr(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/update_skr":
                status_code, location = handlers.handle_update_skr(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/add_contact":
                status_code, location = handlers.handle_add_contact(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/update_contact":
                status_code, location = handlers.handle_update_contact(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/init_content":
                status_code, location = handlers.handle_init_content(db, post_data)
                self.respond(status_code, "", headers={"Location": location})
            elif self.path == "/execute_sql":
                content = handlers.handle_execute_sql(db, post_data)
                self.respond(200, content)
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
    db = Database()
    server_address = (host, port)
    httpd = HTTPServer(server_address, SimpleWebServer)
    print(f"Starting server on {host}:{port}...")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()
