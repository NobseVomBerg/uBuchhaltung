from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from db import Database
import os
import json
try:
    from document_parser import DocumentParser
    PARSER_AVAILABLE = True
except ImportError:
    PARSER_AVAILABLE = False
    print("Warning: document_parser not available. Install pdfplumber to enable document parsing.")

class SimpleWebServer(BaseHTTPRequestHandler):
    def Header1():
        s = "<!DOCTYPE html>\n"
        s+= "<html>\n <head>\n  <meta charset='UTF-8'>\n"
        s+= "  <title>Contabilidad simple</title>\n"
        s+= "  <link rel='stylesheet' href='/buch.css'>\n"
        s+= "  <link rel='icon' sizes='32x32' href='favicon.ico'>\n"
        s+= " </head>\n <body>"
        s+= '<a href="/">Dashboard</a> | <a href="/receipts">Belege</a> | <a href="/transactions">Zahlungen</a> | <a href="/skr">SKR</a> | <a href="/settings">Einstellungen</a> | <a href="/about">About</a>'
        return s
    
    def Header2(content=""):
        s = "<div class='header2'>"
        if content:
            s += content
        else:
            s += "&nbsp;"
        s += "</div>"
        return s
    
    def Footer():
        s = "</body></html>"
        return s

    def PageRoot():
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>Dashboard</h1>"
        s+= "<p>Hier fehlen noch ein paar Dinge.</p>"
        s+= '''
        <form method="POST" action="/init_content">
            <input type="submit" value="Datenbank initialisieren">
        </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    def PageAbout():
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>About</h1>"
        s+= "<p>Einfache Buchführungssoftware.</p>"
        s+= SimpleWebServer.Footer()
        return s

    def PageSettings():
        s = SimpleWebServer.Header1()
        submenu = '<a href="/settings/bankaccounts">Bankkonten</a>'
        s+= SimpleWebServer.Header2(submenu)
        s+= "<h1>Einstellungen</h1>"
        s+= "<p>Hier können Sie verschiedene Einstellungen vornehmen.</p>"
        s+= "<h2>Datenbankeinstellungen</h2>"
        s+= "<p>Datenbank: buch.db</p>"
        s+= "<h2>Systemeinstellungen</h2>"
        s+= "<p>Weitere Einstellungen folgen hier...</p>"
        s+= SimpleWebServer.Footer()
        return s

    # Receipts (Belege)
    def PageReceipts(db):
        rows = db.fetch_receipts()
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>Belege</h1>"
        
        # Container for side-by-side areas
        s+= '''
        <div class="accounts-container">
            <div>
                <h2>Neuen Beleg anlegen</h2>
                <form method="POST" action="/add_receipt">
                    <table>
                        <tr><td>Nummer:</td><td><input type="text" name="number"></td></tr>
                        <tr><td>Datum:</td><td><input type="date" name="date"></td></tr>
                        <tr><td>Dateiname:</td><td><input type="text" name="filename"></td></tr>
                        <tr><td>Pfad:</td><td><input type="text" name="path"></td></tr>
                        <tr><td>Info:</td><td><input type="text" name="info"></td></tr>
                        <tr><td></td><td><input type="submit" value="Beleg hinzufügen"></td></tr>
                    </table>
                </form>
            </div>
            
            <div>
                <h2>Belege hochladen</h2>
                <div id="dropZone">
                    <p>Dateien hier ablegen (Drag & Drop)</p>
                    <input type="file" id="fileInput" multiple>
                    <button onclick="document.getElementById('fileInput').click()">Oder Dateien auswählen</button>
                </div>
                <div id="uploadStatus"></div>
            </div>
        </div>
        
        <script>
            const dropZone = document.getElementById('dropZone');
            const fileInput = document.getElementById('fileInput');
            const uploadStatus = document.getElementById('uploadStatus');
            
            // Drag & Drop Events
            dropZone.addEventListener('dragover', (e) => {
                e.preventDefault();
                dropZone.classList.add('hover');
            });
            
            dropZone.addEventListener('dragleave', () => {
                dropZone.classList.remove('hover');
            });
            
            dropZone.addEventListener('drop', (e) => {
                e.preventDefault();
                dropZone.classList.remove('hover');
                const files = e.dataTransfer.files;
                uploadFiles(files);
            });
            
            // File Input Change
            fileInput.addEventListener('change', (e) => {
                uploadFiles(e.target.files);
            });
            
            function uploadFiles(files) {
                if (files.length === 0) return;
                
                uploadStatus.innerHTML = '<p>Uploading...</p>';
                
                const formData = new FormData();
                for (let i = 0; i < files.length; i++) {
                    formData.append('files', files[i]);
                }
                
                fetch('/upload_receipts', {
                    method: 'POST',
                    body: formData
                })
                .then(response => response.text())
                .then(data => {
                    // Check if response contains confirmation link
                    if (data.includes('confirm_transactions')) {
                        // Replace entire page with response
                        document.open();
                        document.write(data);
                        document.close();
                    } else {
                        // Show temporary success message
                        uploadStatus.innerHTML = '<p class="upload-success">' + data + '</p>';
                        setTimeout(() => { 
                            uploadStatus.innerHTML = ''; 
                            location.reload();
                        }, 3000);
                    }
                })
                .catch(error => {
                    uploadStatus.innerHTML = '<p class="upload-error">Fehler beim Hochladen: ' + error + '</p>';
                });
            }
        </script>
        '''
        
        s+= "<h2>Vorhandene Belege</h2>"
        s+= "<table border='1'>"
        s+= "<tr><th>Nr.</th><th>Datum</th><th>Dateiname</th><th>Pfad</th><th>Info</th><th>Aktionen</th></tr>"
        for row in rows:
            s+= f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td>"
            s+= f"<td><a href='/edit_receipt?number={row[0]}'>Bearbeiten</a></td></tr>"
        s+= "</table>"
        s+= SimpleWebServer.Footer()
        return s

    def PageReceiptEdit(db, number):
        rows = db.fetch_receipts()
        receipt = None
        for row in rows:
            if row[0] == number:
                receipt = row
                break
        if not receipt:
            return "Beleg nicht gefunden."

        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>Beleg bearbeiten</h1>"
        s+= f'''
            <form method="POST" action="/update_receipt">
                <table>
                    <tr><td>Nummer:</td><td><input type="text" name="number" value="{receipt[0]}" readonly></td></tr>
                    <tr><td>Datum:</td><td><input type="date" name="date" value="{receipt[1]}"></td></tr>
                    <tr><td>Dateiname:</td><td><input type="text" name="filename" value="{receipt[2]}"></td></tr>
                    <tr><td>Pfad:</td><td><input type="text" name="path" value="{receipt[3]}"></td></tr>
                    <tr><td>Info:</td><td><input type="text" name="info" value="{receipt[4]}"></td></tr>
                    <tr><td></td><td><input type="submit" value="Beleg aktualisieren"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    # Transactions (Zahlungen)
    def PageTransactions(db, edit_id=None):
        # Generate Header2 with account checkboxes
        accounts = db.fetch_accounts()
        header2_content = ""
        for account in accounts:
            account_id = account[0]
            account_name = account[1]
            header2_content += f'<input type="checkbox" id="account_{account_id}" name="account_{account_id}" checked> '
            header2_content += f'<label for="account_{account_id}">{account_name}</label> &nbsp; '
        
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2(header2_content)
        s+= "<h1>Zahlungen</h1>"
        
        # Load transaction for editing if ID provided
        edit_trans = None
        edit_recipient = ""
        edit_reference = ""
        if edit_id:
            edit_trans = db.get_transaction_by_id(edit_id)
            if edit_trans:
                # Split note into recipient and reference
                note = edit_trans[5] or ""
                note_lines = note.split('\n', 1)
                edit_recipient = note_lines[0] if len(note_lines) > 0 else ""
                edit_reference = note_lines[1] if len(note_lines) > 1 else ""
        
        # Determine form title and button text
        form_title = "Transaktion bearbeiten" if edit_trans else "Neue Transaktion"
        submit_text = "Transaktion aktualisieren" if edit_trans else "Transaktion hinzufügen"
        transaction_id = edit_trans[0] if edit_trans else 0
        
        # Container for side-by-side areas
        s+= f'''
        <div class="accounts-container">
            <div>
                <h2>{form_title}</h2>
                <form method="POST" action="/transactions/add">
                    <table>
                        <tr><td>ID:</td><td><input type="text" name="transaction_id" value="{transaction_id}" readonly style="background-color: #f0f0f0;"></td></tr>
                        <tr><td>Datum:</td><td><input type="date" name="date" value="{edit_trans[1] if edit_trans else ""}" required></td></tr>
                        <tr><td>Empfänger/Auftragg.:</td><td><input type="text" name="recipient" value="{edit_recipient}"></td></tr>
                        <tr><td>Verwendungszweck:</td><td><input type="text" name="reference" value="{edit_reference}"></td></tr>
                        <tr><td>Betrag:</td><td><input type="number" step="0.01" name="amount" value="{edit_trans[7] if edit_trans else ""}"></td></tr>
                        <tr><td>Bankkonto:</td><td><select name="account">
        '''
        # Get selected IBAN for comparison
        selected_iban = edit_trans[3] if edit_trans else None
        for account in accounts:
            selected = 'selected' if selected_iban and account[3] == selected_iban else ''
            s+= f'<option value="{account[0]}" {selected}>{account[1]}</option>'
        
        neu_button = '<a href="/transactions" style="margin-left: 10px; padding: 5px 10px; background-color: #888; color: white; text-decoration: none; display: inline-block;">Neu</a>' if edit_trans else ''
        skr_value = edit_trans[8] if edit_trans and edit_trans[8] else ''
        receipt_value = edit_trans[6] if edit_trans and edit_trans[6] else ''
        
        s+= f'''
                        </select></td></tr>
                        <tr><td>SKR-Konto:</td><td><input type="text" name="skr_account" value="{skr_value}"></td></tr>
                        <tr><td>Beleg-Nr.:</td><td><input type="text" name="receipt_nr" value="{receipt_value}"></td></tr>
                        <tr><td></td><td>
                            <input type="submit" value="{submit_text}">
                            {neu_button}
                        </td></tr>
                    </table>
                </form>
            </div>

            <div>
                <h2>Kontoauszüge hochladen</h2>
                <div id="dropZone">
                    <p>Dateien hier ablegen (Drag & Drop)</p>
                    <input type="file" id="fileInput" multiple>
                    <button onclick="document.getElementById(\'fileInput\').click()">Oder Dateien auswählen</button>
                </div>
                <div id="uploadStatus"></div>
            </div>
        </div>
        
        <script>
            const dropZone = document.getElementById('dropZone');
            const fileInput = document.getElementById('fileInput');
            const uploadStatus = document.getElementById('uploadStatus');
            
            // Drag & Drop Events
            dropZone.addEventListener('dragover', (e) => {{
                e.preventDefault();
                dropZone.classList.add('hover');
            }});
            
            dropZone.addEventListener('dragleave', () => {{
                dropZone.classList.remove('hover');
            }});
            
            dropZone.addEventListener('drop', (e) => {{
                e.preventDefault();
                dropZone.classList.remove('hover');
                const files = e.dataTransfer.files;
                uploadFiles(files);
            }});
            
            // File Input Change
            fileInput.addEventListener('change', (e) => {{
                uploadFiles(e.target.files);
            }});
            
            function uploadFiles(files) {{
                if (files.length === 0) return;
                
                uploadStatus.innerHTML = '<p>Uploading...</p>';
                
                const formData = new FormData();
                for (let i = 0; i < files.length; i++) {{
                    formData.append('files', files[i]);
                }}
                
                fetch('/upload_receipts', {{
                    method: 'POST',
                    body: formData
                }})
                .then(response => response.text())
                .then(data => {{
                    // Check if response contains confirmation link
                    if (data.includes('confirm_transactions')) {{
                        // Replace entire page with response
                        document.open();
                        document.write(data);
                        document.close();
                    }} else {{
                        // Show temporary success message
                        uploadStatus.innerHTML = '<p class="upload-success">' + data + '</p>';
                        setTimeout(() => {{ 
                            uploadStatus.innerHTML = ''; 
                            location.reload();
                        }}, 3000);
                    }}
                }})
                .catch(error => {{
                    uploadStatus.innerHTML = '<p class="upload-error">Fehler beim Hochladen: ' + error + '</p>';
                }});
            }}
        </script>
        '''
        
        s+= "<h2>Kontobewegungen</h2>"
        s+= "<table border='1'>"
        s+= "<tr><th>Datum</th><th>Empfänger/Auftragg.</th><th>Verwendungszw.</th><th>Betrag</th><th>Bankkonto</th><th>SKR-Kto</th><th>Beleg-Nr.</th><th>Aktionen</th></tr>"
        
        # Load transactions from database
        transactions = db.fetch_zahlung()
        
        # Create IBAN to account name mapping
        account_map = {}
        for account in accounts:
            account_map[account[3]] = account[1]  # IBAN -> Name
        
        for trans in transactions:
            trans_id = trans[0]
            date = trans[1]
            bank_eigen = trans[3]
            note = trans[5] or ""
            receipt_nr = trans[6] or ""
            amount = trans[7]
            skr_join_id = trans[8] or ""
            
            # Split note into recipient and reference
            note_lines = note.split('\n', 1)
            recipient = note_lines[0] if len(note_lines) > 0 else ""
            reference = note_lines[1] if len(note_lines) > 1 else ""
            
            # Get account name from IBAN
            account_name = account_map.get(bank_eigen, bank_eigen[:10] + "..." if bank_eigen else "")
            
            # Color code amount
            amount_color = "green" if amount > 0 else "red"
            
            s+= f"<tr>"
            s+= f"<td>{date}</td>"
            s+= f"<td>{recipient[:30]}</td>"
            s+= f"<td>{reference[:40]}</td>"
            s+= f"<td style='color:{amount_color}'>{amount:.2f} €</td>"
            s+= f"<td>{account_name}</td>"
            s+= f"<td>{skr_join_id}</td>"
            s+= f"<td>{receipt_nr}</td>"
            s+= f"<td><a href='/transactions/edit?id={trans_id}'>Bearbeiten</a></td>"
            s+= f"</tr>"
        
        s+= "</table>"
        s+= SimpleWebServer.Footer()
        return s

    # Settings - Bank Accounts
    def PageSettingsBankAccounts(db):
        rows = db.fetch_accounts()
        s = SimpleWebServer.Header1()
        submenu = '<a href="/settings">Einstellungen</a> | <a href="/settings/bankaccounts">Bankkonten</a>'
        s+= SimpleWebServer.Header2(submenu)
        s+= "<h1>Bankkonten</h1>"
        s+= "<table border='1'>"
        s+= "<tr><th>ID</th><th>Bezeichnung</th><th>Inhaber</th><th>IBAN</th><th>BIC</th><th>Bank</th><th>Typ</th><th>Aktionen</th></tr>"
        for row in rows:
            account_type = "Kasse" if row[6] == 1 else "Bank"
            s+= f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td><td>{row[5]}</td><td>{account_type}</td>"
            if row[6] == 1:  # is_cash
                s+= f"<td><span style='color:gray;'>Kann nicht gelöscht werden</span></td></tr>"
            else:
                s+= f"<td><a href='/settings/bankaccounts/edit?id={row[0]}'>Bearbeiten</a> | <a href='/settings/bankaccounts/delete?id={row[0]}'>Löschen</a></td></tr>"
        s+= "</table>"
        s+= '''
            <h2>Neues Bankkonto anlegen</h2>
            <form method="POST" action="/settings/bankaccounts/add">
                <table>
                    <tr><td>Bezeichnung:</td><td><input type="text" name="name" required></td></tr>
                    <tr><td>Inhaber:</td><td><input type="text" name="holder"></td></tr>
                    <tr><td>IBAN:</td><td><input type="text" name="iban"></td></tr>
                    <tr><td>BIC:</td><td><input type="text" name="bic"></td></tr>
                    <tr><td>Bank:</td><td><input type="text" name="bank_name"></td></tr>
                    <tr><td></td><td><input type="submit" value="Konto hinzufügen"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s

    def PageSettingsBankAccountEdit(db, account_id):
        account = db.get_account_by_id(account_id)
        if not account:
            return "Konto nicht gefunden."

        s = SimpleWebServer.Header1()
        submenu = '<a href="/settings">Einstellungen</a> | <a href="/settings/bankaccounts">Bankkonten</a>'
        s+= SimpleWebServer.Header2(submenu)
        s+= "<h1>Bankkonto bearbeiten</h1>"
        
        if account[6] == 1:  # is_cash
            s+= "<p style='color:red;'>Hinweis: Das Kasse-Konto kann nicht bearbeitet werden.</p>"
            s+= "<a href='/settings/bankaccounts'>Zurück zur Kontenübersicht</a>"
        else:
            s+= f'''
                <form method="POST" action="/settings/bankaccounts/update">
                    <table>
                        <tr><td>ID:</td><td><input type="text" name="id" value="{account[0]}" readonly></td></tr>
                        <tr><td>Bezeichnung:</td><td><input type="text" name="name" value="{account[1]}" required></td></tr>
                        <tr><td>Inhaber:</td><td><input type="text" name="holder" value="{account[2]}"></td></tr>
                        <tr><td>IBAN:</td><td><input type="text" name="iban" value="{account[3]}"></td></tr>
                        <tr><td>BIC:</td><td><input type="text" name="bic" value="{account[4]}"></td></tr>
                        <tr><td>Bank:</td><td><input type="text" name="bank_name" value="{account[5]}"></td></tr>
                        <tr><td></td><td><input type="submit" value="Konto aktualisieren"></td></tr>
                    </table>
                </form>
            '''
        s+= SimpleWebServer.Footer()
        return s

    # Chart of Accounts (Standardkontorahmen)
    def PageSkr(db):
        rows = db.fetch_skr()
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>SKR (Standardkontorahmen)</h1>"
        s+= "<table border='1'>"
        s+= "<tr><th>ID</th><th>SKR-Nr.</th><th>Konto</th><th>Name</th><th>Gruppe</th><th>Aktionen</th></tr>"
        for row in rows:
            s+= f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td>"
            s+= f"<td><a href='/edit_skr?id={row[0]}'>Bearbeiten</a></td></tr>"
        s+= "</table>"
        s+= '''
            <h2>Neues SKR-Konto anlegen</h2>
            <form method="POST" action="/add_skr">
                <table>
                    <tr><td>Rahmen-Nr.:</td><td><input type="text" name="framework_nr"></td></tr>
                    <tr><td>Konto:</td><td><input type="text" name="account"></td></tr>
                    <tr><td>Name:</td><td><input type="text" name="name"></td></tr>
                    <tr><td>Gruppe:</td><td><input type="text" name="group"></td></tr>
                    <tr><td></td><td><input type="submit" value="SKR-Konto hinzufügen"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s
    
    def PageSkrEdit(db, id):
        rows = db.fetch_skr()
        skr = None
        for row in rows:
            if row[0] == id:
                skr = row
                break
        if not skr:
            return "SKR-Konto nicht gefunden."

        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>SKR-Konto bearbeiten</h1>"
        s+= f'''
            <form method="POST" action="/update_skr">
                <table>
                    <tr><td>ID:</td><td><input type="text" name="id" value="{skr[0]}" readonly></td></tr>
                    <tr><td>Rahmen-Nr.:</td><td><input type="text" name="framework_nr" value="{skr[1]}"></td></tr>
                    <tr><td>Konto:</td><td><input type="text" name="account" value="{skr[2]}"></td></tr>
                    <tr><td>Name:</td><td><input type="text" name="name" value="{skr[3]}"></td></tr>
                    <tr><td>Gruppe:</td><td><input type="text" name="group" value="{skr[4]}"></td></tr>
                    <tr><td></td><td><input type="submit" value="SKR-Konto aktualisieren"></td></tr>
                </table>
            </form>
        '''
        s+= SimpleWebServer.Footer()
        return s
    
    def PageConfirmTransactions(import_id: str):
        """Display parsed transactions for confirmation before import"""
        import json
        
        temp_file = f"./data/pending_imports/{import_id}_*.json"
        import glob
        json_files = glob.glob(temp_file)
        
        if not json_files:
            return "Import-Daten nicht gefunden."
        
        with open(json_files[0], 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        s = SimpleWebServer.Header1()
        s+= SimpleWebServer.Header2()
        s+= "<h1>Transaktionen bestätigen</h1>"
        s+= f"<p><strong>Datei:</strong> {data.get('original_filename', 'Unbekannt')}</p>"
        s+= f"<p><strong>IBAN:</strong> {data.get('iban', 'Nicht erkannt')}</p>"
        s+= f"<p><strong>Belegdatum:</strong> {data.get('document_date', 'Nicht erkannt')}</p>"
        s+= f"<p><strong>Bank:</strong> {data.get('bank_code', 'Unbekannt')}</p>"
        
        if data.get('transactions'):
            s+= f"<h2>Gefundene Transaktionen: {len(data['transactions'])}</h2>"
            s+= "<table border='1'>"
            s+= "<tr><th>Datum</th><th>Empfänger/Auftragg.</th><th>Verwendungszweck</th><th>Betrag</th><th>Fremd-IBAN</th></tr>"
            
            for trans in data['transactions']:
                date_str = trans['date'][:10] if isinstance(trans['date'], str) else trans['date']
                amount_color = "green" if trans['amount'] > 0 else "red"
                s+= f"<tr>"
                s+= f"<td>{date_str}</td>"
                s+= f"<td>{trans['recipient']}</td>"
                s+= f"<td>{trans['reference'][:50]}...</td>"
                s+= f"<td style='color:{amount_color}'>{trans['amount']:.2f} €</td>"
                s+= f"<td>{trans.get('foreign_iban', '')[:10]}...</td>"
                s+= f"</tr>"
            
            s+= "</table>"
            s+= f'''
                <form method="POST" action="/confirm_import">
                    <input type="hidden" name="import_id" value="{import_id}">
                    <p>
                        <input type="submit" name="action" value="Importieren" style="background-color: green; color: white; padding: 10px 20px; font-size: 16px;">
                        <input type="submit" name="action" value="Abbrechen" style="background-color: red; color: white; padding: 10px 20px; font-size: 16px;">
                    </p>
                </form>
            '''
        else:
            s+= "<p>Keine Transaktionen gefunden.</p>"
        
        s+= SimpleWebServer.Footer()
        return s

    def do_GET(self):
        try:
            db = Database()
            # URL routing
            if self.path == "/":
                self.respond(200, SimpleWebServer.PageRoot())
            elif self.path == "/about":
                self.respond(200, SimpleWebServer.PageAbout())
            elif self.path == "/settings":
                self.respond(200, SimpleWebServer.PageSettings())
            elif self.path == "/settings/bankaccounts":
                self.respond(200, SimpleWebServer.PageSettingsBankAccounts(db))
            elif self.path == "/receipts" or self.path == "/belege":
                self.respond(200, SimpleWebServer.PageReceipts(db))
            elif self.path == "/transactions":
                self.respond(200, SimpleWebServer.PageTransactions(db))
            elif self.path.startswith("/transactions/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                edit_id = int(query_components["id"][0])
                self.respond(200, SimpleWebServer.PageTransactions(db, edit_id=edit_id))
            elif self.path == "/skr":
                self.respond(200, SimpleWebServer.PageSkr(db))
            elif self.path.startswith("/edit_receipt"):
                query_components = parse_qs(self.path.split('?')[1])
                number = query_components["number"][0]
                self.respond(200, SimpleWebServer.PageReceiptEdit(db, number))
            elif self.path.startswith("/settings/bankaccounts/edit"):
                query_components = parse_qs(self.path.split('?')[1])
                account_id = int(query_components["id"][0])
                self.respond(200, SimpleWebServer.PageSettingsBankAccountEdit(db, account_id))
            elif self.path.startswith("/settings/bankaccounts/delete"):
                query_components = parse_qs(self.path.split('?')[1])
                account_id = int(query_components["id"][0])
                db.delete_account(account_id)
                self.respond(303, "", headers={"Location": "/settings/bankaccounts"})
            elif self.path.startswith("/edit_skr"):
                query_components = parse_qs(self.path.split('?')[1])
                id = query_components["id"][0]
                self.respond(200, SimpleWebServer.PageSkrEdit(db, id))
            elif self.path.startswith("/confirm_transactions"):
                query_components = parse_qs(self.path.split('?')[1])
                import_id = query_components["import_id"][0]
                self.respond(200, SimpleWebServer.PageConfirmTransactions(import_id))
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
            from email.parser import BytesParser
            
            # Create directory if it doesn't exist
            upload_dir = "./data/Belege"
            os.makedirs(upload_dir, exist_ok=True)
            
            # Parse multipart/form-data
            content_type = self.headers['Content-Type']
            if 'multipart/form-data' not in content_type:
                self.respond(400, "Ungültiger Content-Type")
                return
            
            # Get boundary
            boundary = content_type.split("boundary=")[1].encode()
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            # Parse multipart data
            parts = post_data.split(b'--' + boundary)
            uploaded_files = []
            
            for part in parts:
                if b'Content-Disposition' in part:
                    # Extract filename
                    if b'filename=' in part:
                        header_end = part.find(b'\r\n\r\n')
                        if header_end == -1:
                            continue
                        
                        header = part[:header_end].decode('utf-8', errors='ignore')
                        content = part[header_end + 4:]
                        
                        # Remove trailing boundary markers
                        if content.endswith(b'\r\n'):
                            content = content[:-2]
                        
                        # Extract filename from header
                        filename_start = header.find('filename="') + 10
                        filename_end = header.find('"', filename_start)
                        filename = header[filename_start:filename_end]
                        
                        if filename:
                            # Save file temporarily
                            filepath = os.path.join(upload_dir, filename)
                            with open(filepath, 'wb') as f:
                                f.write(content)
                            
                            # Try to parse and organize document
                            if PARSER_AVAILABLE and filename.lower().endswith('.pdf'):
                                try:
                                    parser = DocumentParser()
                                    new_path, parsed_data = parser.process_and_organize(filepath)
                                    
                                    # If bank statement with transactions, save for confirmation
                                    if parsed_data.get('transactions') and len(parsed_data['transactions']) > 0:
                                        import_id = parser.save_parsed_data(filename, parsed_data)
                                        uploaded_files.append({
                                            'filename': filename,
                                            'status': 'parsed',
                                            'import_id': import_id,
                                            'transaction_count': len(parsed_data['transactions'])
                                        })
                                    else:
                                        uploaded_files.append({
                                            'filename': filename,
                                            'status': 'organized',
                                            'path': os.path.relpath(new_path, upload_dir)
                                        })
                                except FileExistsError as e:
                                    # File exists with different content
                                    uploaded_files.append({
                                        'filename': filename,
                                        'status': 'warning',
                                        'error': str(e)
                                    })
                                except Exception as e:
                                    print(f"Error parsing {filename}: {e}")
                                    import traceback
                                    traceback.print_exc()
                                    uploaded_files.append({
                                        'filename': filename,
                                        'status': 'error',
                                        'error': str(e)
                                    })
                            else:
                                uploaded_files.append({
                                    'filename': filename,
                                    'status': 'uploaded'
                                })
            
            if uploaded_files:
                # Build response HTML
                s = SimpleWebServer.Header1()
                s+= SimpleWebServer.Header2()
                s+= "<h1>Upload erfolgreich</h1>"
                
                for file_info in uploaded_files:
                    if isinstance(file_info, dict):
                        if file_info['status'] == 'parsed':
                            s+= f"<p>✓ <strong>{file_info['filename']}</strong>: {file_info['transaction_count']} Transaktionen erkannt</p>"
                            s+= f"<p><a href='/confirm_transactions?import_id={file_info['import_id']}' style='background-color: green; color: white; padding: 10px 20px; text-decoration: none; display: inline-block;'>Transaktionen bestätigen</a></p>"
                        elif file_info['status'] == 'organized':
                            s+= f"<p>✓ <strong>{file_info['filename']}</strong> verschoben nach {file_info['path']}</p>"
                        elif file_info['status'] == 'warning':
                            s+= f"<p style='color: orange;'>⚠ <strong>{file_info['filename']}</strong>: {file_info['error']}</p>"
                        elif file_info['status'] == 'error':
                            s+= f"<p>⚠ <strong>{file_info['filename']}</strong>: Fehler beim Parsen - {file_info['error']}</p>"
                        else:
                            s+= f"<p>✓ <strong>{file_info['filename']}</strong> hochgeladen</p>"
                    else:
                        s+= f"<p>✓ {file_info}</p>"
                
                s+= "<p><a href='/receipts'>Zurück zu Belegen</a></p>"
                s+= SimpleWebServer.Footer()
                message = s
            else:
                message = "Keine Dateien hochgeladen."
            
            self.respond(200, message)
            return
        
        # Handle regular form data
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')
        post_data = parse_qs(post_data)

        if self.path == "/add_receipt":
            number = post_data["number"][0]
            date = post_data["date"][0]
            filename = post_data["filename"][0]
            path = post_data["path"][0]
            info = post_data["info"][0]
            db.insert_receipt(number, date, filename, path, info)
            self.respond(303, "", headers={"Location": "/receipts"})
        elif self.path == "/update_receipt":
            number = post_data["number"][0]
            date = post_data["date"][0]
            filename = post_data["filename"][0]
            path = post_data["path"][0]
            info = post_data["info"][0]
            db.update_receipt(number, date, filename, path, info)
            self.respond(303, "", headers={"Location": "/receipts"})
        elif self.path == "/confirm_import":
            # Handle confirmation of parsed transactions
            import_id = post_data.get("import_id", [""])[0]
            action = post_data.get("action", [""])[0]
            
            if not import_id:
                self.respond(400, "Fehlende import_id")
                return
            
            if PARSER_AVAILABLE:
                parser = DocumentParser()
                
                # Load pending import data
                import_dir = os.path.join('data', 'pending_imports')
                import_files = [f for f in os.listdir(import_dir) if f.startswith(import_id)]
                
                if not import_files:
                    self.respond(404, "Import nicht gefunden")
                    return
                
                import_file = os.path.join(import_dir, import_files[0])
                
                # Handle cancel action
                if action == "Abbrechen":
                    try:
                        os.remove(import_file)
                        s = SimpleWebServer.Header1()
                        s += SimpleWebServer.Header2()
                        s += f"<h1>Import abgebrochen</h1>"
                        s += f"<p>Die Transaktionen wurden nicht importiert.</p>"
                        s += "<p><a href='/receipts'>Zurück zu Belegen</a></p>"
                        s += SimpleWebServer.Footer()
                        self.respond(200, s)
                    except Exception as e:
                        self.respond(500, f"Fehler beim Löschen: {str(e)}")
                    return
                
                # Handle import action
                try:
                    with open(import_file, 'r', encoding='utf-8') as f:
                        import_data = json.load(f)
                    
                    # Get account ID from IBAN
                    account_iban = import_data.get('iban', '')
                    accounts = db.fetch_accounts()
                    account_id = None
                    for acc in accounts:
                        if acc[3] == account_iban:  # IBAN is at index 3
                            account_id = acc[0]
                            break
                    
                    if not account_id:
                        self.respond(400, f"Kein Konto gefunden für IBAN: {account_iban}")
                        return
                    
                    # Insert transactions
                    inserted_count = 0
                    skipped_count = 0
                    skipped_transactions = []
                    
                    # Get own IBAN for duplicate check
                    own_iban = ""
                    for acc in accounts:
                        if acc[0] == account_id:
                            own_iban = acc[3]
                            break
                    
                    for trans in import_data.get('transactions', []):
                        # Build note
                        note = f"{trans['recipient']}\n{trans['reference']}"
                        foreign_iban = trans.get('foreign_iban', '')
                        
                        # Check if transaction already exists (including note for uniqueness)
                        if db.check_transaction_exists(trans['date'], trans['amount'], own_iban, foreign_iban, note):
                            skipped_count += 1
                            skipped_transactions.append(trans)
                            continue
                        
                        # Prepare parameters tuple for logging
                        parameters = (account_id, trans['date'], trans['amount'], None, note, foreign_iban)
                        
                        # Prepare SQL statement for logging
                        sql_statement = f"""INSERT INTO Zahlung (KontoId, Datum, Betrag, Beleg, Notiz, FremdIban) 
                                          VALUES ({account_id}, '{trans['date']}', {trans['amount']}, 
                                          NULL, '{note.replace("'", "''")}', '{foreign_iban}')"""
                        
                        # Log SQL before execution
                        parser.log_sql(sql_statement, parameters, "VBR bank statement import")
                        
                        # Execute insert
                        db.insert_transaction(
                            date=trans['date'],
                            amount=trans['amount'],
                            own_iban=own_iban,
                            foreign_iban=foreign_iban,
                            note=note,
                            receipt_number=None
                        )
                        inserted_count += 1
                    
                    # Delete pending import file
                    os.remove(import_file)
                    
                    # Redirect with success message
                    s = SimpleWebServer.Header1()
                    s += SimpleWebServer.Header2()
                    s += f"<h1>Import erfolgreich</h1>"
                    s += f"<p>{inserted_count} Transaktionen wurden importiert.</p>"
                    
                    if skipped_count > 0:
                        s += f"<p style='color: orange;'>{skipped_count} Duplikate wurden übersprungen:</p>"
                        s += "<table border='1'>"
                        s += "<tr><th>Datum</th><th>Empfänger</th><th>Verwendungszweck</th><th>Betrag</th><th>Fremd-IBAN</th></tr>"
                        for trans in skipped_transactions:
                            date_str = trans['date'][:10] if isinstance(trans['date'], str) else trans['date']
                            amount_color = "green" if trans['amount'] > 0 else "red"
                            s += f"<tr>"
                            s += f"<td>{date_str}</td>"
                            s += f"<td>{trans['recipient'][:30]}</td>"
                            s += f"<td>{trans['reference'][:40]}...</td>"
                            s += f"<td style='color:{amount_color}'>{trans['amount']:.2f} €</td>"
                            s += f"<td>{trans.get('foreign_iban', '')[:10]}...</td>"
                            s += f"</tr>"
                        s += "</table>"
                    
                    s += "<p><a href='/transactions'>Zu Zahlungen</a></p>"
                    s += SimpleWebServer.Footer()
                    self.respond(200, s)
                    
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    self.respond(500, f"Fehler beim Import: {str(e)}")
            else:
                self.respond(500, "Parser nicht verfügbar")
            return
        elif self.path == "/transactions/add":
            # Handle manual transaction entry (insert or update)
            transaction_id = int(post_data.get("transaction_id", ["0"])[0])
            date = post_data.get("date", [""])[0]
            recipient = post_data.get("recipient", [""])[0]
            reference = post_data.get("reference", [""])[0]
            amount = post_data.get("amount", ["0"])[0]
            account_id = post_data.get("account", [""])[0]
            skr_account = post_data.get("skr_account", [""])[0]
            receipt_nr = post_data.get("receipt_nr", [""])[0]
            
            try:
                # Get account IBAN
                accounts = db.fetch_accounts()
                own_iban = None
                for acc in accounts:
                    if str(acc[0]) == account_id:
                        own_iban = acc[3]  # IBAN is at index 3
                        break
                
                # Build note
                note = f"{recipient}\n{reference}" if recipient and reference else (recipient or reference or "")
                
                # Update or insert transaction
                if transaction_id > 0:
                    # Update existing transaction
                    db.update_transaction(
                        transaction_id=transaction_id,
                        date=date,
                        amount=float(amount),
                        own_iban=own_iban or "",
                        foreign_iban="",
                        note=note,
                        receipt_number=receipt_nr or None
                    )
                    log_desc = "Manual transaction update"
                    sql_statement = f'''UPDATE Zahlung SET Datum1='{date}', Datum2=NULL, BankEigen='{own_iban or ""}', BankFremd='', Zweck='{note.replace("'", "''")}', BelegNummer={f"'{receipt_nr}'" if receipt_nr else "NULL"}, Betrag={amount}, SkrBuchJoinId=NULL WHERE ID={transaction_id}'''
                else:
                    # Insert new transaction
                    transaction_id = db.insert_transaction(
                        date=date,
                        amount=float(amount),
                        own_iban=own_iban or "",
                        foreign_iban="",
                        note=note,
                        receipt_number=receipt_nr or None
                    )
                    log_desc = "Manual transaction entry"
                    sql_statement = f'''INSERT INTO Zahlung (Datum1, Datum2, BankEigen, BankFremd, Zweck, BelegNummer, Betrag, SkrBuchJoinId)
VALUES ('{date}', NULL, '{own_iban or ""}', '', '{note.replace("'", "''")}', {f"'{receipt_nr}'" if receipt_nr else "NULL"}, {amount}, NULL)'''
                
                # Log the operation
                if PARSER_AVAILABLE:
                    parser = DocumentParser()
                    parser.log_sql(sql_statement, (date, None, own_iban or "", "", note, receipt_nr, float(amount), None), log_desc)
                
                self.respond(303, "", headers={"Location": "/transactions"})
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.respond(500, f"Fehler beim Speichern: {str(e)}")
            return
        elif self.path == "/settings/bankaccounts/add":
            name = post_data["name"][0]
            holder = post_data.get("holder", [""])[0]
            iban = post_data.get("iban", [""])[0]
            bic = post_data.get("bic", [""])[0]
            bank_name = post_data.get("bank_name", [""])[0]
            db.insert_account(name, holder, iban, bic, bank_name)
            self.respond(303, "", headers={"Location": "/settings/bankaccounts"})
        elif self.path == "/settings/bankaccounts/update":
            account_id = int(post_data["id"][0])
            name = post_data["name"][0]
            holder = post_data.get("holder", [""])[0]
            iban = post_data.get("iban", [""])[0]
            bic = post_data.get("bic", [""])[0]
            bank_name = post_data.get("bank_name", [""])[0]
            db.update_account(account_id, name, holder, iban, bic, bank_name)
            self.respond(303, "", headers={"Location": "/settings/bankaccounts"})
        elif self.path == "/add_skr":
            framework_nr = post_data["framework_nr"][0]
            account = post_data["account"][0]
            name = post_data["name"][0]
            group = post_data["group"][0]
            db.insert_skr(framework_nr, account, name, group)
            self.respond(303, "", headers={"Location": "/skr"})
        elif self.path == "/update_skr":
            id = post_data["id"][0]
            framework_nr = post_data["framework_nr"][0]
            account = post_data["account"][0]
            name = post_data["name"][0]
            group = post_data["group"][0]
            db.update_skr(id, framework_nr, account, name, group)
            self.respond(303, "", headers={"Location": "/skr"})
        if self.path == "/init_content":
            db.init_content()
            self.respond(303, "", headers={"Location": "/"})
        else:
            self.respond(404, "Seite nicht gefunden.")

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

if __name__ == "__main__":
    run_server()
