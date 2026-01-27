"""
HTML page generation functions
All functions return complete HTML strings
"""

def Header1(active_page=None):
    """Generate main header with navigation
    
    Args:
        active_page: Name of active page ('dashboard', 'receipts', 'transactions', 'skr', 'settings', 'about')
                     Active page will be highlighted without link
    """
    s = "<!DOCTYPE html>\n"
    s+= "<html>\n <head>\n  <meta charset='UTF-8'>\n"
    s+= "  <title>Contabilidad simple</title>\n"
    s+= "  <link rel='stylesheet' href='/buch.css'>\n"
    s+= "  <link rel='icon' sizes='32x32' href='favicon.ico'>\n"
    s+= " </head>\n <body>"
    
    # Build navigation menu with active page highlighting
    nav_items = []
    
    # Dashboard
    if active_page == 'dashboard':
        nav_items.append('<span id="ActivePage">Dashboard</span>')
    else:
        nav_items.append('<a href="/">Dashboard</a>')
    
    # Belege
    if active_page == 'receipts':
        nav_items.append('<span id="ActivePage">Belege</span>')
    else:
        nav_items.append('<a href="/receipts">Belege</a>')
    
    # Zahlungen
    if active_page == 'transactions':
        nav_items.append('<span id="ActivePage">Zahlungen</span>')
    else:
        nav_items.append('<a href="/transactions">Zahlungen</a>')
    
    # SKR
    if active_page == 'skr':
        nav_items.append('<span id="ActivePage">SKR</span>')
    else:
        nav_items.append('<a href="/skr">SKR</a>')
    
    # Einstellungen
    if active_page == 'settings':
        nav_items.append('<span id="ActivePage">Einstellungen</span>')
    else:
        nav_items.append('<a href="/settings">Einstellungen</a>')
    
    # About
    if active_page == 'about':
        nav_items.append('<span id="ActivePage">About</span>')
    else:
        nav_items.append('<a href="/about">About</a>')
    
    s += ' | '.join(nav_items)
    return s

def Header2(content=""):
    """Generate secondary header/submenu"""
    s = "<div class='header2'>"
    if content:
        s += content
    else:
        s += "&nbsp;"
    s += "</div>"
    return s

def Header3(content=""):
    """Generate third header line for filters"""
    s = "<div class='header3'>"  # Use header3 styling (no border)
    if content:
        s += content
    else:
        s += "&nbsp;"
    s += "</div>"
    return s

def Footer():
    """Generate page footer"""
    s = "</body></html>"
    return s

def PageRoot():
    """Generate dashboard page"""
    s = Header1('dashboard')
    s+= Header2()
    s+= Header3()
    s+= "<h1>Dashboard</h1>"
    s+= "<p>Hier fehlen noch ein paar Dinge.</p>"
    s+= '''
    <form method="POST" action="/init_content">
        <input type="submit" value="Datenbank initialisieren">
    </form>
    '''
    s+= Footer()
    return s

def PageAbout():
    """Generate about page"""
    s = Header1('about')
    s+= Header2()
    s+= Header3()
    s+= "<h1>About</h1>"
    s+= "<p>Einfache Buchführungssoftware.</p>"
    s+= Footer()
    return s

def PageSettings():
    """Generate settings page"""
    s = Header1('settings')
    submenu = '<a href="/settings/bankaccounts">Bankkonten</a>'
    s+= Header2(submenu)
    s+= Header3()
    s+= "<h1>Einstellungen</h1>"
    s+= "<p>Hier können Sie verschiedene Einstellungen vornehmen.</p>"
    s+= "<h2>Datenbankeinstellungen</h2>"
    s+= "<p>Datenbank: buch.db</p>"
    s+= "<h2>Systemeinstellungen</h2>"
    s+= "<p>Weitere Einstellungen folgen hier...</p>"
    s+= Footer()
    return s

def PageReceipts(db):
    """Generate receipts page with upload functionality"""
    rows = db.fetch_receipts()
    s = Header1('receipts')
    s+= Header2()
    
    # Header3 with date filter
    import datetime
    current_year = datetime.datetime.now().year
    header3_content = f'''
        Von: <input type="date" id="dateFrom" onchange="filterReceiptsByDate()"> 
        Bis: <input type="date" id="dateTo" onchange="filterReceiptsByDate()"> &nbsp;
        <button onclick="setReceiptYear({current_year})">{current_year}</button>
        <button onclick="setReceiptYear({current_year-1})">{current_year-1}</button>
        <button onclick="setReceiptYear({current_year-2})">{current_year-2}</button>
        <button onclick="setReceiptYear({current_year-3})">{current_year-3}</button>
    '''
    s+= Header3(header3_content)
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
        s+= f"<tr class='receipt-row' data-date='{row[2]}'><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td><td>{row[5]}</td>"
        s+= f"<td><a href='/edit_receipt?number={row[1]}'>Bearbeiten</a></td></tr>"
    s+= "</table>"
    
    # Add date filter JavaScript
    s+= '''
    <script>
        function setReceiptYear(year) {
            document.getElementById('dateFrom').value = year + '-01-01';
            document.getElementById('dateTo').value = year + '-12-31';
            filterReceiptsByDate();
        }
        
        function filterReceiptsByDate() {
            const dateFrom = document.getElementById('dateFrom').value;
            const dateTo = document.getElementById('dateTo').value;
            const rows = document.querySelectorAll('.receipt-row');
            
            rows.forEach(row => {
                const rowDate = row.getAttribute('data-date');
                let show = true;
                
                if (dateFrom && rowDate < dateFrom) {
                    show = false;
                }
                if (dateTo && rowDate > dateTo) {
                    show = false;
                }
                
                row.style.display = show ? '' : 'none';
            });
        }
    </script>
    '''
    s+= Footer()
    return s

def PageReceiptEdit(db, number):
    """Generate receipt edit page"""
    rows = db.fetch_receipts()
    receipt = None
    for row in rows:
        if row[0] == number:
            receipt = row
            break
    if not receipt:
        return "Beleg nicht gefunden."

    s = Header1()
    s+= Header2()
    s+= Header3()
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
    s+= Footer()
    return s

def PageTransactions(db, edit_transaction_id=None):
    """Generate transactions page with edit functionality"""
    # Generate Header2 with account checkboxes
    accounts = db.fetch_accounts()
    header2_content = ""
    for account in accounts:
        account_id = account[0]
        account_name = account[1]
        account_iban = account[3]  # Store IBAN for filtering
        header2_content += f'<input type="checkbox" id="account_{account_id}" name="account_{account_id}" data-iban="{account_iban}" checked onchange="filterTransactions()"> '
        header2_content += f'<label for="account_{account_id}">{account_name}</label> &nbsp; '
    
    s = Header1('transactions')
    s+= Header2(header2_content)
    
    # Header3 with date filter
    import datetime
    current_year = datetime.datetime.now().year
    header3_content = f'''
        Von: <input type="date" id="dateFrom" onchange="filterTransactionsByDate()"> 
        Bis: <input type="date" id="dateTo" onchange="filterTransactionsByDate()"> &nbsp;
        <button onclick="setTransactionYear({current_year})">{current_year}</button>
        <button onclick="setTransactionYear({current_year-1})">{current_year-1}</button>
        <button onclick="setTransactionYear({current_year-2})">{current_year-2}</button>
        <button onclick="setTransactionYear({current_year-3})">{current_year-3}</button>
    '''
    s+= Header3(header3_content)
    s+= "<h1>Zahlungen</h1>"
    
    # Load transaction for editing if ID provided
    edit_trans = None
    edit_recipient = ""
    edit_reference = ""
    if edit_transaction_id:
        edit_trans = db.get_transaction_by_id(edit_transaction_id)
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
    s+= "<tr><th>Buchungs-Datum</th><th>Empfänger/Auftragg.</th><th>Verwendungszweck</th><th>Betrag</th><th>Fremdkonto</th><th>SKR-Kto</th><th>Beleg-Nr.</th><th>Aktionen</th></tr>"
    
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
        
        # Add data-iban and data-date attributes for filtering
        s+= f"<tr class='transaction-row' data-iban='{bank_eigen}' data-date='{date}'>"
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
    
    # Add JavaScript for filtering
    s+= '''
    <script>
        function setTransactionYear(year) {
            document.getElementById('dateFrom').value = year + '-01-01';
            document.getElementById('dateTo').value = year + '-12-31';
            filterTransactionsByDate();
        }
        
        function filterTransactionsByDate() {
            const dateFrom = document.getElementById('dateFrom').value;
            const dateTo = document.getElementById('dateTo').value;
            const rows = document.querySelectorAll('.transaction-row');
            
            rows.forEach(row => {
                const rowDate = row.getAttribute('data-date');
                let show = true;
                
                // Check date filter
                if (dateFrom && rowDate < dateFrom) {
                    show = false;
                }
                if (dateTo && rowDate > dateTo) {
                    show = false;
                }
                
                // Check IBAN filter (existing functionality)
                if (show) {
                    const checkedIbans = new Set();
                    const checkboxes = document.querySelectorAll('input[type="checkbox"][data-iban]');
                    checkboxes.forEach(cb => {
                        if (cb.checked) {
                            checkedIbans.add(cb.getAttribute('data-iban'));
                        }
                    });
                    const iban = row.getAttribute('data-iban');
                    if (!checkedIbans.has(iban)) {
                        show = false;
                    }
                }
                
                row.style.display = show ? '' : 'none';
            });
        }
        
        function filterTransactions() {
            // Combined filter that also considers date range
            filterTransactionsByDate();
        }
    </script>
    '''
    
    s+= Footer()
    return s

def PageSettingsBankAccounts(db):
    """Generate bank accounts settings page"""
    rows = db.fetch_accounts()
    s = Header1('settings')
    submenu = '<a href="/settings">Einstellungen</a> | <a href="/settings/bankaccounts">Bankkonten</a>'
    s+= Header2(submenu)
    s+= Header3()
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
    s+= Footer()
    return s

def PageSettingsBankAccountEdit(db, account_id):
    """Generate bank account edit page"""
    account = db.get_account_by_id(account_id)
    if not account:
        return "Konto nicht gefunden."

    s = Header1()
    submenu = '<a href="/settings">Einstellungen</a> | <a href="/settings/bankaccounts">Bankkonten</a>'
    s+= Header2(submenu)
    s+= Header3()
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
    s+= Footer()
    return s

def PageSkr(db):
    """Generate SKR (chart of accounts) page"""
    rows = db.fetch_skr()
    s = Header1('skr')
    s+= Header2()
    s+= Header3()
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
    s+= Footer()
    return s

def PageSkrEdit(db, id):
    """Generate SKR edit page"""
    rows = db.fetch_skr()
    skr = None
    for row in rows:
        if row[0] == id:
            skr = row
            break
    if not skr:
        return "SKR-Konto nicht gefunden."

    s = Header1()
    s+= Header2()
    s+= Header3()
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
    s+= Footer()
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
    
    s = Header1()
    s+= Header2()
    s+= Header3()
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
            <form method="POST" action="/confirm_transactions">
                <input type="hidden" name="import_id" value="{import_id}">
                <p>
                    <input type="submit" name="action" value="Importieren" style="background-color: green; color: white; padding: 10px 20px; font-size: 16px;">
                    <input type="submit" name="action" value="Abbrechen" style="background-color: red; color: white; padding: 10px 20px; font-size: 16px;">
                </p>
            </form>
        '''
    else:
        s+= "<p>Keine Transaktionen gefunden.</p>"
    
    s+= Footer()
    return s
