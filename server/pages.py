"""
HTML page generation functions
All functions return complete HTML strings
"""
from db import Database

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
    
    # Rechnung
    if active_page == 'invoice':
        nav_items.append('<span id="ActivePage">Rechnung</span>')
    else:
        nav_items.append('<a href="/invoice">Rechnung</a>')
    
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
    
    # Split-Buchungen
    if active_page == 'bookinggroups':
        nav_items.append('<span id="ActivePage">Split-Buchungen</span>')
    else:
        nav_items.append('<a href="/bookinggroups">Split-Buchungen</a>')
    
    # SKR
    if active_page == 'skr':
        nav_items.append('<span id="ActivePage">SKR</span>')
    else:
        nav_items.append('<a href="/skr">SKR</a>')
    
    # Kontakte
    if active_page == 'contacts':
        nav_items.append('<span id="ActivePage">Kontakte</span>')
    else:
        nav_items.append('<a href="/contacts">Kontakte</a>')
    
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

def PageRoot(db: Database):
    """Generate dashboard page"""
    s = Header1('dashboard')
    s+= Header2()
    s+= Header3()
    
    # Database statistics
    stats = db.get_table_statistics()
    s+= "<h2>Datenbank-Übersicht</h2>"
    s+= "<table border='1'>"
    s+= "<tr><th>Tabelle</th><th>Anzahl Einträge</th></tr>"
    for table_name, count in stats:
        s+= f"<tr><td>{table_name}</td><td style='text-align: right;'>{count}</td></tr>"
    s+= "</table>"
    
    s+= '''
    <h2>Datenbank mit einigen Testdaten befüllen</h2>
    <form method="POST" action="/init_content">
        <input type="submit" value="Datenbank initialisieren">
    </form>
    
    <h2>SQL-Befehle ausführen</h2>
    <form method="POST" action="/execute_sql">
        <p>Geben Sie hier SQL-Befehle ein (mehrere Befehle durch Semikolon getrennt):</p>
        <textarea name="sql_commands" rows="15" cols="100" style="font-family: monospace; width: 100%; max-width: 1000px;" placeholder="INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard) VALUES (4, 1000, 'Kasse', 'Barkasse', 1);
INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard) VALUES (4, 1200, 'Bank', 'Bankguthaben', 1);"></textarea>
        <br>
        <input type="submit" value="SQL ausführen" style="margin-top: 10px; padding: 8px 20px; background-color: #4CAF50; color: white; border: none; cursor: pointer;">
        <span style="color: red; margin-left: 20px;">⚠️ Vorsicht: SQL-Befehle werden direkt ausgeführt!</span>
    </form>
    
    <div id="sql_result" style="margin-top: 20px;"></div>
    '''
    s+= Footer()
    return s

def PageAbout():
    """Generate about page"""
    s = Header1('about')
    s+= Header2()
    s+= Header3()
    s+= "<p>Einfache Buchführungssoftware.</p>"
    s+= Footer()
    return s

def PageSettings():
    """Generate settings page"""
    s = Header1('settings')
    submenu = '<a href="/settings/bankaccounts">Bankkonten</a>'
    s+= Header2(submenu)
    s+= Header3()
    s+= "<p>Hier können Sie verschiedene Einstellungen vornehmen.</p>"
    s+= "<h2>Datenbankeinstellungen</h2>"
    s+= "<p>Datenbank: buch.db</p>"
    s+= "<h2>Systemeinstellungen</h2>"
    s+= "<p>Weitere Einstellungen folgen hier...</p>"
    s+= Footer()
    return s

def PageReceipts(db: Database):
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
                <input type="file" id="fileInput" multiple accept=".pdf,application/pdf">
                <button onclick="document.getElementById('fileInput').click()">Oder Dateien auswählen</button>
            </div>
            <div id="uploadStatus"></div>
        </div>
    </div>
    
    <script>
        // Prevent default browser behavior for drag and drop on entire page
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            document.body.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
            }, false);
        });
        
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const uploadStatus = document.getElementById('uploadStatus');
        
        // Drag & Drop Events
        dropZone.addEventListener('dragenter', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.add('hover');
            console.log('dragenter');
        });
        
        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('dragover');
        });
        
        dropZone.addEventListener('dragleave', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('hover');
            console.log('dragleave');
        });
        
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('hover');
            console.log('drop', e.dataTransfer.files);
            const files = e.dataTransfer.files;
            uploadFiles(files);
        });
        
        // File Input Change
        fileInput.addEventListener('change', (e) => {
            console.log('file input change', e.target.files);
            uploadFiles(e.target.files);
        });
        
        function uploadFiles(files) {
            console.log('uploadFiles called with', files.length, 'files');
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
        # Documents: ID(0), Number(1), Date(2), Filename(3), Path(4), Info(5)
        s+= f"<tr class='receipt-row' data-date='{row[2]}'><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td><td>{row[5]}</td>"
        s+= f"<td><a href='/receipts/edit?number={row[1]}'>Bearbeiten</a></td></tr>"
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

def PageReceiptEdit(db: Database, number):
    """Generate receipt edit page"""
    receipt = db.get_receipt_by_number(number)
    if not receipt:
        return "Beleg nicht gefunden."

    # Documents: ID(0), Number(1), Date(2), Filename(3), Path(4), Info(5)
    s = Header1()
    s+= Header2()
    s+= Header3()
    s+= "<h1>Beleg bearbeiten</h1>"
    s+= f'''
        <form method="POST" action="/update_receipt">
            <input type="hidden" name="id" value="{receipt[0]}">
            <table>
                <tr><td>Nummer:</td><td><input type="text" name="number" value="{receipt[1]}"></td></tr>
                <tr><td>Datum:</td><td><input type="date" name="date" value="{receipt[2]}"></td></tr>
                <tr><td>Dateiname:</td><td><input type="text" name="filename" value="{receipt[3]}"></td></tr>
                <tr><td>Pfad:</td><td><input type="text" name="path" value="{receipt[4]}"></td></tr>
                <tr><td>Info:</td><td><input type="text" name="info" value="{receipt[5]}"></td></tr>
                <tr><td></td><td><input type="submit" value="Beleg aktualisieren"></td></tr>
            </table>
        </form>
    '''
    
    # Show linked bookings
    document_id = receipt[0]  # ID is at index 0
    linked_bookings = db.get_bookings_for_document(document_id)
    
    s+= "<h2>Verknüpfte Buchungen</h2>"
    if linked_bookings:
        s+= "<table border='1'>"
        s+= "<tr><th>ID</th><th>Datum</th><th>Empfänger</th><th>Betrag</th><th>Typ</th><th>Aktionen</th></tr>"
        for booking in linked_bookings:
            booking_id = booking[0]
            date_booking = booking[1]
            recipient = booking[6] or ""
            amount = booking[10]
            relation_type = booking[-1]  # RelationType from JOIN
            
            amount_color = "green" if amount > 0 else "red"
            s+= f"<tr>"
            s+= f"<td>{booking_id}</td>"
            s+= f"<td>{date_booking}</td>"
            s+= f"<td>{recipient}</td>"
            s+= f"<td style='color:{amount_color}'>{amount:.2f}</td>"
            s+= f"<td>{relation_type or '-'}</td>"
            s+= f"<td><a href='/transactions/edit?id={booking_id}'>Bearbeiten</a> | "
            s+= f"<a href='/documents/unlink?doc_id={document_id}&booking_id={booking_id}'>Entfernen</a></td>"
            s+= f"</tr>"
        s+= "</table>"
    else:
        s+= "<p>Keine verknüpften Buchungen.</p>"
    
    # Form to link new booking
    s+= "<h3>Buchung verknüpfen</h3>"
    s+= f'''
        <form method="POST" action="/documents/link">
            <input type="hidden" name="document_id" value="{document_id}">
            <table>
                <tr><td>Buchungs-ID:</td><td><input type="number" name="booking_id" required></td></tr>
                <tr><td>Typ:</td><td>
                    <select name="relation_type">
                        <option value="receipt">Beleg</option>
                        <option value="invoice">Rechnung</option>
                        <option value="contract">Vertrag</option>
                        <option value="other">Sonstiges</option>
                    </select>
                </td></tr>
                <tr><td></td><td><input type="submit" value="Verknüpfung hinzufügen"></td></tr>
            </table>
        </form>
    '''
    
    s+= Footer()
    return s

def PageTransactions(db: Database, edit_transaction_id=None):
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
    
    # Header3 with filters
    import datetime
    current_year = datetime.datetime.now().year
    
    # Get filter data
    customers = db.fetch_contacts(contact_type='customer')
    coa_accounts = db.fetch_chart_of_accounts()
    
    header3_content = f'''
        <div style="display: flex; gap: 15px; align-items: center; flex-wrap: wrap;">
            <div>
                <label>Von:</label> <input type="date" id="dateFrom" onchange="filterTransactions()"> 
                <label>Bis:</label> <input type="date" id="dateTo" onchange="filterTransactions()">
                <button onclick="setTransactionYear({current_year})">{current_year}</button>
                <button onclick="setTransactionYear({current_year-1})">{current_year-1}</button>
            </div>
            <div>
                <label>Kunde:</label>
                <select id="customerFilter" onchange="filterTransactions()">
                    <option value="">Alle Kunden</option>
    '''
    for customer in customers:
        customer_display = f"{customer[2]} ({customer[3] or 'Privat'})" if customer[2] else customer[3] or f"ID {customer[0]}"
        header3_content += f'<option value="{customer[0]}">{customer_display}</option>'
    
    header3_content += '''
                </select>
            </div>
            <div>
                <label>Währung:</label>
                <select id="currencyFilter" onchange="filterTransactions()">
                    <option value="">Alle</option>
                    <option value="EUR" selected>EUR</option>
                    <option value="USD">USD</option>
                    <option value="GBP">GBP</option>
                    <option value="CHF">CHF</option>
                </select>
            </div>
            <div>
                <label>Min. Betrag:</label> <input type="number" step="0.01" id="minAmount" onchange="filterTransactions()" style="width: 80px;">
                <label>Max. Betrag:</label> <input type="number" step="0.01" id="maxAmount" onchange="filterTransactions()" style="width: 80px;">
            </div>
        </div>
    '''
    s+= Header3(header3_content)
    
    # Load transaction for editing if ID provided
    edit_trans = None
    edit_recipient = ""
    edit_text = ""
    if edit_transaction_id:
        edit_trans = db.get_booking_by_id(edit_transaction_id)
        if edit_trans:
            # Extract data from booking structure
            edit_recipient = edit_trans[6] or ""  # RecipientClient
            edit_text = edit_trans[14] or ""  # Text
    
    # Get dropdown data (reuse customers variable from above)
    customers = db.fetch_contacts(contact_type='customer')
    coa_accounts = db.fetch_chart_of_accounts()
    booking_groups = db.fetch_booking_groups()
    
    # Determine form title and button text
    form_title = "Transaktion bearbeiten" if edit_trans else "Neue Transaktion"
    submit_text = "Transaktion aktualisieren" if edit_trans else "Transaktion hinzufügen"
    transaction_id = edit_trans[0] if edit_trans else 0
    
    # Container for side-by-side areas
    id_display = f'<tr><td>ID:</td><td style="color: #666;">{transaction_id}<input type="hidden" name="transaction_id" value="{transaction_id}"></td></tr>' if edit_trans else ''
    
    s+= f'''
    <div class="accounts-container">
        <div>
            <h2>{form_title}</h2>
            <form method="POST" action="/transactions/add">
                <table>
                    {id_display}
                    <tr><td>Buchungsdatum:</td><td><input type="date" name="date" value="{edit_trans[1] if edit_trans else ""}" required></td></tr>
                    <tr><td>Steuerdatum:</td><td><input type="date" name="date_tax" value="{edit_trans[2] if edit_trans and edit_trans[2] else ""}"></td></tr>
                    
                    <tr><td>Empfänger/Auftragg.:</td><td><input type="text" name="recipient" value="{edit_recipient}" size="40"></td></tr>
                    <tr><td>Verwendungszweck:</td><td><textarea name="text" rows="3" cols="40">{edit_text}</textarea></td></tr>
                    
                    <tr><td>Bankkonto:</td><td><select name="account">
                        <option value="">-- Kein Konto --</option>
    '''
    # Get selected account_id for comparison
    selected_account_id = edit_trans[4] if edit_trans else None
    for account in accounts:
        selected = 'selected' if selected_account_id and account[0] == selected_account_id else ''
        s+= f'<option value="{account[0]}" {selected}>{account[1]}</option>'
    
    s+= f'''
                    </select></td></tr>
                    <tr><td>Fremdes Konto/IBAN:</td><td><input type="text" name="foreign_account" value="{edit_trans[5] if edit_trans and edit_trans[5] else ""}" size="40"></td></tr>
                    
                    <tr><td>Kunde:</td><td><select name="contact_id">
                        <option value="">-- Kein Kunde --</option>
    '''
    selected_contact_id = edit_trans[7] if edit_trans else None
    for contact in customers:
        selected = 'selected' if selected_contact_id and contact[0] == selected_contact_id else ''
        contact_display = f"{contact[2]} ({contact[3] or 'Privat'})" if contact[2] else contact[3] or f"ID {contact[0]}"
        s+= f'<option value="{contact[0]}" {selected}>{contact_display}</option>'
    
    s+= f'''
                    </select></td></tr>
                    
                    <tr><td>Split-Buchung:</td><td><select name="booking_group_id">
                        <option value="">-- Keine Gruppierung --</option>
    '''
    selected_booking_group_id = edit_trans[3] if edit_trans else None
    for bg in booking_groups:
        selected = 'selected' if selected_booking_group_id and bg[0] == selected_booking_group_id else ''
        bg_display = f"#{bg[0]} - {bg[1] or 'Ohne Beschreibung'}"
        s+= f'<option value="{bg[0]}" {selected}>{bg_display}</option>'
    
    s+= f'''
                    </select></td></tr>
                    
                    <tr><td>SKR-Konto:</td><td><select name="coa_id">
                        <option value="">-- Kein SKR-Konto --</option>
    '''
    selected_coa_id = edit_trans[8] if edit_trans else None
    for coa in coa_accounts:
        selected = 'selected' if selected_coa_id and coa[0] == selected_coa_id else ''
        coa_display = f"{coa[2]} - {coa[3]}" if coa[3] else f"{coa[2]}"
        s+= f'<option value="{coa[0]}" {selected}>{coa_display}</option>'
    
    s+= f'''
                    </select></td></tr>
                    
                    <tr><td>Betrag:</td><td><input type="number" step="0.01" name="amount" id="amount" value="{edit_trans[10] if edit_trans else ""}" required></td></tr>
                    <tr><td>Währung:</td><td><input type="text" name="currency" value="{edit_trans[11] if edit_trans else "EUR"}" size="5"></td></tr>
                    
                    <tr><td>Steuersatz (%):</td><td><input type="number" step="0.01" name="tax_rate" id="tax_rate" value="{edit_trans[12]*100 if edit_trans and edit_trans[12] else ""}" placeholder="z.B. 19 für 19%"></td></tr>
                    <tr><td>Steuerbetrag:</td><td><input type="number" step="0.01" name="tax_amount" id="tax_amount" value="{edit_trans[13] if edit_trans and edit_trans[13] else ""}"></td></tr>
                    
                    <tr><td>Beleg-Nr.:</td><td><input type="text" name="document_nr" value="{edit_trans[15] if edit_trans and edit_trans[15] else ""}"></td></tr>
                    
    '''
    
    neu_button = '<a href="/transactions" style="margin-left: 10px; padding: 5px 10px; background-color: #888; color: white; text-decoration: none; display: inline-block;">Neu</a>' if edit_trans else ''
    
    s+= f'''
                    <tr><td></td><td>
                        <input type="submit" value="{submit_text}">
                        {neu_button}
                    </td></tr>
                </table>
            </form>
            
            <script>
                // Automatische Berechnung des Steuerbetrags
                function calculateTax() {{
                    const amount = parseFloat(document.getElementById('amount').value) || 0;
                    const taxRate = parseFloat(document.getElementById('tax_rate').value) || 0;
                    
                    if (amount !== 0 && taxRate !== 0) {{
                        const taxAmount = amount * (taxRate / 100);
                        document.getElementById('tax_amount').value = taxAmount.toFixed(2);
                    }}
                }}
                
                // Event Listener für Betrag und Steuersatz
                document.getElementById('amount').addEventListener('input', calculateTax);
                document.getElementById('tax_rate').addEventListener('input', calculateTax);
            </script>
    '''
    
    # Show linked documents if editing
    if edit_trans:
        booking_id = edit_trans[0]
        linked_documents = db.get_documents_for_booking(booking_id)
        
        s+= "<h3>Verknüpfte Dokumente</h3>"
        if linked_documents:
            s+= "<table border='1'>"
            s+= "<tr><th>ID</th><th>Nr.</th><th>Datum</th><th>Dateiname</th><th>Typ</th><th>Aktionen</th></tr>"
            for doc in linked_documents:
                doc_id = doc[0]
                doc_number = doc[1]
                doc_date = doc[2]
                doc_filename = doc[3]
                relation_type = doc[-1]  # RelationType from JOIN
                
                s+= f"<tr>"
                s+= f"<td>{doc_id}</td>"
                s+= f"<td>{doc_number}</td>"
                s+= f"<td>{doc_date}</td>"
                s+= f"<td>{doc_filename}</td>"
                s+= f"<td>{relation_type or '-'}</td>"
                s+= f"<td><a href='/receipts/edit?number={doc_number}'>Ansehen</a> | "
                s+= f"<a href='/documents/unlink?doc_id={doc_id}&booking_id={booking_id}'>Entfernen</a></td>"
                s+= f"</tr>"
            s+= "</table>"
        else:
            s+= "<p>Keine verknüpften Dokumente.</p>"
    
    s+= '''
        </div>

        <div>
            <h2>Kontoauszüge hochladen</h2>
            <div id="dropZone">
                <p>Dateien hier ablegen (Drag & Drop)</p>
                <input type="file" id="fileInput" multiple accept=".pdf,application/pdf">
                <button onclick="document.getElementById('fileInput').click()">Oder Dateien auswählen</button>
            </div>
            <div id="uploadStatus"></div>
        </div>
    </div>
    
    <script>
        // Prevent default browser behavior for drag and drop on entire page
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            document.body.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
            }, false);
        });
        
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const uploadStatus = document.getElementById('uploadStatus');
        
        // Drag & Drop Events
        dropZone.addEventListener('dragenter', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.add('hover');
            console.log('dragenter');
        });
        
        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('dragover');
        });
        
        dropZone.addEventListener('dragleave', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('hover');
            console.log('dragleave');
        });
        
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('hover');
            console.log('drop', e.dataTransfer.files);
            const files = e.dataTransfer.files;
            uploadFiles(files);
        });
        
        // File Input Change
        fileInput.addEventListener('change', (e) => {
            console.log('file input change', e.target.files);
            uploadFiles(e.target.files);
        });
        
        function uploadFiles(files) {
            console.log('uploadFiles called with', files.length, 'files');
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
    
    s+= "<h2>Kontobewegungen</h2>"
    s+= "<table border='1'>"
    s+= "<tr><th>Datum</th><th>Empfänger/Auftragg.</th><th>Text</th><th>Betrag</th><th>Währung</th><th>Konto</th><th>Kunde</th><th>SKR</th><th>Aktionen</th></tr>"
    
    # Load bookings from database
    bookings = db.fetch_bookings()
    
    # Create account ID to account name mapping
    account_map = {}
    for account in accounts:
        account_map[account[0]] = account[1]  # ID -> Name
    
    # Create customer mapping
    customers = db.fetch_contacts(contact_type='customer')
    customer_map = {}
    for customer in customers:
        customer_map[customer[0]] = customer[2] or customer[3]  # Name or Company
    
    # Create COA mapping
    coa_accounts = db.fetch_chart_of_accounts()
    coa_map = {}
    for coa in coa_accounts:
        coa_map[coa[0]] = f"{coa[2]}"  # AccountNumber
    
    for booking in bookings:
        booking_id = booking[0]
        date_booking = booking[1]
        account_id = booking[4]
        recipient = booking[6] or ""
        contact_id = booking[7]
        coa_id = booking[8]
        amount = booking[10]
        currency = booking[11] or "EUR"
        text = booking[14] or ""
        
        # Get mapped names
        account_name = account_map.get(account_id, "") if account_id else ""
        contact_name = customer_map.get(contact_id, "") if contact_id else ""
        coa_number = coa_map.get(coa_id, "") if coa_id else ""
        
        # Color code amount
        amount_color = "green" if amount > 0 else "red"
        
        # Add data attributes for filtering
        account_id_str = account_id or ''
        contact_id_str = contact_id or ''
        s+= f"<tr class='transaction-row' data-account-id='{account_id_str}' data-date='{date_booking}' data-contact-id='{contact_id_str}' data-currency='{currency}' data-amount='{amount}'>"
        s+= f"<td>{date_booking}</td>"
        s+= f"<td>{recipient[:25]}</td>"
        s+= f"<td>{text[:35]}</td>"
        s+= f"<td style='color:{amount_color}'>{amount:.2f}</td>"
        s+= f"<td>{currency}</td>"
        s+= f"<td>{account_name[:20]}</td>"
        s+= f"<td>{contact_name[:20]}</td>"
        s+= f"<td>{coa_number}</td>"
        s+= f"<td><a href='/transactions/edit?id={booking_id}'>Bearbeiten</a></td>"
        s+= f"</tr>"
    
    s+= "</table>"
    
    # Add JavaScript for filtering
    s+= '''
    <script>
        function setTransactionYear(year) {
            document.getElementById('dateFrom').value = year + '-01-01';
            document.getElementById('dateTo').value = year + '-12-31';
            filterTransactions();
        }
        
        function filterTransactions() {
            const dateFrom = document.getElementById('dateFrom').value;
            const dateTo = document.getElementById('dateTo').value;
            const customerFilter = document.getElementById('customerFilter').value;
            const currencyFilter = document.getElementById('currencyFilter').value;
            const minAmount = parseFloat(document.getElementById('minAmount').value) || null;
            const maxAmount = parseFloat(document.getElementById('maxAmount').value) || null;
            
            const rows = document.querySelectorAll('.transaction-row');
            
            rows.forEach(row => {
                const rowDate = row.getAttribute('data-date');
                const rowCustomerId = row.getAttribute('data-customer-id');
                const rowCurrency = row.getAttribute('data-currency');
                const rowAmount = parseFloat(row.getAttribute('data-amount'));
                const rowAccountId = row.getAttribute('data-account-id');
                
                let show = true;
                
                // Check date filter
                if (dateFrom && rowDate < dateFrom) show = false;
                if (dateTo && rowDate > dateTo) show = false;
                
                // Check customer filter
                if (customerFilter && rowCustomerId !== customerFilter) show = false;
                
                // Check currency filter
                if (currencyFilter && rowCurrency !== currencyFilter) show = false;
                
                // Check amount range
                if (minAmount !== null && rowAmount < minAmount) show = false;
                if (maxAmount !== null && rowAmount > maxAmount) show = false;
                
                // Check account filter (existing checkboxes)
                if (show) {
                    const checkedAccounts = new Set();
                    document.querySelectorAll('input[type="checkbox"]:checked').forEach(cb => {
                        const accountId = cb.id.replace('account_', '');
                        checkedAccounts.add(accountId);
                    });
                    
                    if (checkedAccounts.size > 0 && !checkedAccounts.has(rowAccountId)) {
                        show = false;
                    }
                }
                
                row.style.display = show ? '' : 'none';
            });
        }
        
        // Also filter on account checkbox changes
        function filterTransactionsByAccount() {
            filterTransactions();
        }
        
        // Update old filter function name for compatibility
        function filterTransactionsByDate() {
            filterTransactions();
        }
    </script>
    '''
    
    s+= Footer()
    return s

def PageSettingsBankAccounts(db: Database):
    """Generate bank accounts settings page"""
    rows = db.fetch_accounts()
    s = Header1('settings')
    submenu = '<a href="/settings">Einstellungen</a> -> <span id="ActivePage">Bankkonten</span>'
    s+= Header2(submenu)
    s+= Header3()
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
    s+= "<h2>Vorhandene Konten</h2>"
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
    s+= Footer()
    return s

def PageSettingsBankAccountEdit(db: Database, account_id):
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

def PageSkr(db: Database):
    """Generate SKR (chart of accounts) page"""
    rows = db.fetch_chart_of_accounts()
    s = Header1('skr')
    s+= Header2()
    s+= Header3()
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
    s+= "<h2>Standardkontorahmen, definierte Konten</h2>"
    s+= "<table border='1'>"
    s+= "<tr><th>ID</th><th>SKR-Nr.</th><th>Konto</th><th>Name</th><th>Gruppe</th><th>Standard</th><th>Aktionen</th></tr>"
    for row in rows:
        is_standard = row[5] if len(row) > 5 else 0
        standard_text = "✓" if is_standard else ""
        edit_link = "<span style='color: #888;'>Standard (nicht bearbeitbar)</span>" if is_standard else f"<a href='/edit_skr?id={row[0]}'>Bearbeiten</a>"
        s+= f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td><td>{standard_text}</td>"
        s+= f"<td>{edit_link}</td></tr>"
    s+= "</table>"
    s+= Footer()
    return s

def PageSkrEdit(db: Database, id):
    """Generate SKR edit page"""
    rows = db.fetch_chart_of_accounts()
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

def PageInvoice(db: Database):
    """Generate invoice page"""
    # Get company data (own contact)
    own_contacts = db.fetch_contacts(contact_type='own')
    own_contact = own_contacts[0] if own_contacts else None
    
    # Get customers for selection
    customers = db.fetch_contacts(contact_type='customer')
    
    # Get bank accounts for selection
    accounts = db.fetch_accounts()
    
    s = Header1('invoice')
    s += Header2()
    s += '''
    <div class="invoice-container" id="invoice_container">
        <div class="invoice-header">
            <div class="invoice-logo">
                <img src="/static/logo.png" alt="Firmenlogo" style="max-width: 150px; max-height: 80px;" onerror="this.style.display='none';">
            </div>
            <div class="invoice-meta">
                <table class="invoice-meta-table">
                    <tr><td>Datum:</td><td><input type="date" id="invoice_date" value="" style="width: 150px;"></td></tr>
                    <tr><td>Rechnungs-Nr.:</td><td><input type="text" id="invoice_number" style="width: 150px;"></td></tr>
                    <tr><td>Kunden-Nr.:</td><td><input type="text" id="customer_number" readonly style="width: 150px; background-color: #f0f0f0;"></td></tr>
                </table>
            </div>
        </div>
        
        <div class="invoice-address-block">
            <div class="invoice-sender-line">
    '''
    
    if own_contact:
        sender_street = own_contact[5] or ''
        sender_postal = own_contact[6] or ''
        sender_city = own_contact[7] or ''
        sender_name = own_contact[4] or own_contact[3] or ''
        s += f'{sender_name} · {sender_street} · {sender_postal} {sender_city}'
    else:
        s += 'Eigene Adresse in Kontakte anlegen (Typ: own)'
    
    s += '''            </div>
            <div class="invoice-customer-address">
                <select id="customer_select" onchange="updateCustomerAddress()" style="margin-bottom: 10px; width: 100%;" class="no-pdf">
                    <option value="">-- Kunde auswählen --</option>
    '''
    
    for customer in customers:
        cust_name = customer[3] or customer[4] or f"ID {customer[0]}"
        s += f'<option value="{customer[0]}">{cust_name}</option>'
    
    s += '''                </select>
                <div id="customer_address_display" style="min-height: 80px; white-space: pre-line;">
                    
                </div>
            </div>
        </div>
        
        <h1 class="invoice-title">Rechnung</h1>
        
        <table class="invoice-items" id="invoice_table">
            <thead>
                <tr>
                    <th style="width: 40px;">Pos.</th>
                    <th style="width: 70px;">Menge</th>
                    <th style="width: 70px;">Einheit</th>
                    <th>Bezeichnung</th>
                    <th style="width: 100px;">Einzelpreis</th>
                    <th style="width: 100px;">Gesamt</th>
                    <th style="width: 30px;" class="no-pdf"></th>
                </tr>
            </thead>
            <tbody id="invoice_items_body">
                <tr class="invoice-item-row" data-row="1">
                    <td>1</td>
                    <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width: 60px;"></td>
                    <td><input type="text" class="item-unit" value="Stk." style="width: 60px;"></td>
                    <td><input type="text" class="item-description" style="width: 100%;"></td>
                    <td><input type="number" class="item-price" value="0" min="0" step="0.01" style="width: 80px;"> €</td>
                    <td class="item-total" style="text-align: right;">0,00 €</td>
                    <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;">✕</button></td>
                </tr>
            </tbody>
            <tfoot>
                <tr><td colspan="7" style="height: 10px; border: none;"></td></tr>
                <tr class="totals-row totals-row-border">
                    <td colspan="5" style="text-align: right; border: none;">Summe netto:</td>
                    <td id="sum_net" style="text-align: right; font-weight: bold;">0,00 €</td>
                    <td class="no-pdf" style="border: none;"></td>
                </tr>
                <tr class="totals-row">
                    <td colspan="4" style="text-align: right; border: none;">Mehrwertsteuer</td>
                    <td style="text-align: right; border: none;"><input type="number" id="tax_rate" value="19" min="0" max="100" step="0.1" style="width: 50px;">% auf <span id="tax_base">0,00</span> € netto:</td>
                    <td id="tax_amount" style="text-align: right; font-weight: bold;">0,00 €</td>
                    <td class="no-pdf" style="border: none;"></td>
                </tr>
                <tr class="totals-row totals-row-final">
                    <td colspan="5" style="text-align: right; border: none;"><strong>Gesamtbetrag:</strong></td>
                    <td id="sum_gross" style="text-align: right; font-weight: bold;"><strong>0,00 €</strong></td>
                    <td class="no-pdf" style="border: none;"></td>
                </tr>
            </tfoot>
        </table>
        
        <button type="button" onclick="addRow()" style="margin: 10px 0;" class="no-pdf">+ Position hinzufügen</button>
        
        <div class="invoice-payment-terms">
            <textarea id="payment_terms" rows="3" style="width: 100%;">Bitte überweisen Sie den Gesamtbetrag ohne jeden Abzug unter Angabe der Rechnungsnummer innerhalb von 14 Tagen ab Rechnungsdatum auf das unten angegebene Konto. Vielen Dank.</textarea>
        </div>
        
        <div class="invoice-footer">
            <hr>
            <table class="footer-table">
                <tr>
                    <td class="footer-col-left">
    '''
    
    if own_contact:
        s += f'''                        <strong>{own_contact[4] or own_contact[3] or 'Firmenname'}</strong><br>
                        {own_contact[3] or 'Ansprechpartner'}<br>
                        {own_contact[5] or 'Straße'}<br>
                        {own_contact[6] or 'PLZ'} {own_contact[7] or 'Ort'}'''
    else:
        s += '                        <em>Eigene Firmendaten in Kontakte anlegen</em>'
    
    s += '''                    </td>
                    <td class="footer-col-center">
    '''
    
    if own_contact:
        s += f'''                        <span class="footer-label">Tel</span> {own_contact[10] or '-'}<br>
                        <span class="footer-label">E-Mail</span> {own_contact[9] or '-'}<br>
                        <span class="footer-label">UStIdNr</span> {own_contact[11] or '-'}'''
    else:
        s += '                        <em>Kontaktdaten fehlen</em>'
    
    s += '''                    </td>
                    <td class="footer-col-right">
                        <strong>Bankverbindung</strong><br>
                        <select id="bank_account_select" onchange="updateBankDetails()" style="width: 100%; margin-top: 5px;" class="no-pdf">
                            <option value="">-- Konto auswählen --</option>
    '''
    
    for account in accounts:
        if not account[6]:  # Skip cash accounts (IsCash=1)
            s += f'<option value="{account[0]}">{account[1]}</option>'
    
    s += '''                        </select>
                        <div id="bank_details" style="margin-top: 5px;">
                            
                        </div>
                    </td>
                </tr>
            </table>
        </div>
    </div>
    
    <div style="text-align: center; margin: 20px 0;">
        <button type="button" onclick="generatePDF()" class="pdf-button no-pdf">📄 Als PDF exportieren</button>
    </div>
    
    <script>
        // Customer data from server
        const customersData = {'''
    
    # Build JavaScript customer data object
    customer_js_data = []
    for customer in customers:
        cust_data = {
            'id': customer[0],
            'customer_number': customer[2] or '',
            'name': customer[3] or '',
            'company': customer[4] or '',
            'street': customer[5] or '',
            'postal': customer[6] or '',
            'city': customer[7] or ''
        }
        customer_js_data.append(f'{customer[0]}: {cust_data}')
    
    s += ',\n            '.join(customer_js_data)
    
    s += '''
        };
        
        // Bank accounts data from server
        const banksData = {'''
    
    # Build JavaScript bank data object
    bank_js_data = []
    for account in accounts:
        if not account[6]:  # Skip cash accounts
            bank_data = {
                'name': account[1],
                'bank_name': account[5] or '',
                'iban': account[3] or '',
                'bic': account[4] or ''
            }
            bank_js_data.append(f'{account[0]}: {bank_data}')
    
    s += ',\n            '.join(bank_js_data)
    
    s += '''
        };
        
        // Set today's date
        document.getElementById('invoice_date').valueAsDate = new Date();
        
        // Update customer address
        function updateCustomerAddress() {
            const customerId = document.getElementById('customer_select').value;
            const addressDisplay = document.getElementById('customer_address_display');
            const customerNumberField = document.getElementById('customer_number');
            
            if (!customerId) {
                addressDisplay.innerHTML = '';
                customerNumberField.value = '';
                return;
            }
            
            const customer = customersData[customerId];
            if (customer) {
                const displayName = customer.company || customer.name;
                let address = displayName + '\\n';
                if (customer.street) address += customer.street + '\\n';
                if (customer.postal || customer.city) address += (customer.postal + ' ' + customer.city).trim();
                
                addressDisplay.textContent = address;
                customerNumberField.value = customer.customer_number;
            }
        }
        
        // Update bank details
        function updateBankDetails() {
            const bankId = document.getElementById('bank_account_select').value;
            const bankDetails = document.getElementById('bank_details');
            
            if (!bankId) {
                bankDetails.innerHTML = '';
                return;
            }
            
            const bank = banksData[bankId];
            if (bank) {
                let details = '<span class="footer-label">Bank</span> ' + bank.bank_name + '<br>';
                details += '<span class="footer-label">IBAN</span> ' + bank.iban + '<br>';
                details += '<span class="footer-label">BIC</span> ' + bank.bic;
                bankDetails.innerHTML = details;
            }
        }
        
        let rowCounter = 1;
        
        // Add new row
        function addRow() {
            rowCounter++;
            const tbody = document.getElementById('invoice_items_body');
            const newRow = document.createElement('tr');
            newRow.className = 'invoice-item-row';
            newRow.setAttribute('data-row', rowCounter);
            newRow.innerHTML = `
                <td>${rowCounter}</td>
                <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width: 60px;"></td>
                <td><input type="text" class="item-unit" value="Stk." style="width: 60px;"></td>
                <td><input type="text" class="item-description" style="width: 100%;"></td>
                <td><input type="number" class="item-price" value="0" min="0" step="0.01" style="width: 80px;"> €</td>
                <td class="item-total" style="text-align: right;">0,00 €</td>
                <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;">✕</button></td>
            `;
            tbody.appendChild(newRow);
            attachCalculationListeners(newRow);
            calculateTotals();
        }
        
        // Remove row
        function removeRow(button) {
            const row = button.closest('tr');
            row.remove();
            renumberRows();
            calculateTotals();
        }
        
        // Renumber rows
        function renumberRows() {
            const rows = document.querySelectorAll('.invoice-item-row');
            rows.forEach((row, index) => {
                row.querySelector('td:first-child').textContent = index + 1;
                row.setAttribute('data-row', index + 1);
            });
            rowCounter = rows.length;
        }
        
        // Calculate row total
        function calculateRowTotal(row) {
            const quantity = parseFloat(row.querySelector('.item-quantity').value) || 0;
            const price = parseFloat(row.querySelector('.item-price').value) || 0;
            const total = quantity * price;
            row.querySelector('.item-total').textContent = total.toFixed(2).replace('.', ',') + ' €';
        }
        
        // Calculate all totals
        function calculateTotals() {
            // Calculate sum of all items
            let sumNet = 0;
            document.querySelectorAll('.invoice-item-row').forEach(row => {
                calculateRowTotal(row);
                const totalText = row.querySelector('.item-total').textContent;
                const total = parseFloat(totalText.replace(' €', '').replace(',', '.')) || 0;
                sumNet += total;
            });
            
            // Update net sum
            document.getElementById('sum_net').textContent = sumNet.toFixed(2).replace('.', ',') + ' €';
            document.getElementById('tax_base').textContent = sumNet.toFixed(2).replace('.', ',');
            
            // Calculate tax
            const taxRate = parseFloat(document.getElementById('tax_rate').value) || 0;
            const taxAmount = sumNet * (taxRate / 100);
            document.getElementById('tax_amount').textContent = taxAmount.toFixed(2).replace('.', ',') + ' €';
            
            // Calculate gross sum
            const sumGross = sumNet + taxAmount;
            document.getElementById('sum_gross').innerHTML = '<strong>' + sumGross.toFixed(2).replace('.', ',') + ' €</strong>';
        }
        
        // Attach listeners to row inputs
        function attachCalculationListeners(row) {
            row.querySelector('.item-quantity').addEventListener('input', calculateTotals);
            row.querySelector('.item-price').addEventListener('input', calculateTotals);
        }
        
        // Attach listeners to tax rate
        document.getElementById('tax_rate').addEventListener('input', calculateTotals);
        
        // Attach listeners to initial row
        document.querySelectorAll('.invoice-item-row').forEach(attachCalculationListeners);
        
        // Initial calculation
        calculateTotals();
        
        // Generate PDF
        function generatePDF() {
            // Get selected customer name
            const customerId = document.getElementById('customer_select').value;
            let customerName = '';
            if (customerId && customersData[customerId]) {
                customerName = customersData[customerId].name || '';
            }
            
            // Collect invoice data
            const invoiceData = {
                date: document.getElementById('invoice_date').value,
                number: document.getElementById('invoice_number').value,
                customerNumber: document.getElementById('customer_number').value,
                customerAddress: document.getElementById('customer_address_display').textContent,
                customerName: customerName,
                items: [],
                taxRate: parseFloat(document.getElementById('tax_rate').value) || 19,
                sumNet: document.getElementById('sum_net').textContent,
                taxAmount: document.getElementById('tax_amount').textContent,
                sumGross: document.getElementById('sum_gross').textContent.replace(/<[^>]*>/g, ''),
                paymentTerms: document.getElementById('payment_terms').value,
                bankDetails: document.getElementById('bank_details').innerHTML
            };
            
            // Collect items
            document.querySelectorAll('.invoice-item-row').forEach(row => {
                invoiceData.items.push({
                    pos: row.querySelector('td:first-child').textContent,
                    quantity: row.querySelector('.item-quantity').value,
                    unit: row.querySelector('.item-unit').value,
                    description: row.querySelector('.item-description').value,
                    price: row.querySelector('.item-price').value,
                    total: row.querySelector('.item-total').textContent
                });
            });
            
            // Send to server for PDF generation
            fetch('/invoice/pdf', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(invoiceData)
            })
            .then(response => response.blob())
            .then(blob => {
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'Rechnung_' + (invoiceData.number || 'Entwurf') + '.pdf';
                document.body.appendChild(a);
                a.click();
                a.remove();
                window.URL.revokeObjectURL(url);
            })
            .catch(err => alert('Fehler beim PDF-Export: ' + err));
        }
    </script>
    '''
    
    s += Footer()
    return s

def PageContacts(db: Database):
    """Generate contacts management page"""
    contacts = db.fetch_contacts()
    s = Header1('contacts')
    s+= Header2()
    
    # Filter tabs for contact types
    header3_content = f'''
        <strong>Filter:</strong> 
        <a href="/contacts">Alle</a>
        <a href="/contacts?type=customer">Kunden</a>
        <a href="/contacts?type=supplier">Lieferanten</a>
        <a href="/contacts?type=own">Eigene Daten</a>
        <a href="/contacts?type=insurance">Versicherungen</a>
        <a href="/contacts?type=other">Sonstige</a>
    '''
    s+= Header3(header3_content)

    s+= '''
        <h2>Neuen Kontakt anlegen</h2>
        <form method="POST" action="/add_contact">
            <table>
                <tr><td>Typ:</td><td>
                    <select name="contact_type">
                        <option value="customer">Kunde</option>
                        <option value="supplier">Lieferant</option>
                        <option value="own">Eigene Daten</option>
                        <option value="insurance">Versicherung</option>
                        <option value="other">Sonstiges</option>
                    </select>
                </td></tr>
                <tr><td>Kundennummer:</td><td><input type="text" name="customer_number" placeholder="Optional, z.B. K-12345"></td></tr>
                <tr><td>Name:</td><td><input type="text" name="name" required></td></tr>
                <tr><td>Firma:</td><td><input type="text" name="company"></td></tr>
                <tr><td>Straße:</td><td><input type="text" name="street"></td></tr>
                <tr><td>PLZ:</td><td><input type="text" name="postal_code"></td></tr>
                <tr><td>Stadt:</td><td><input type="text" name="city"></td></tr>
                <tr><td>Land:</td><td><input type="text" name="country"></td></tr>
                <tr><td>E-Mail:</td><td><input type="email" name="email"></td></tr>
                <tr><td>Telefon:</td><td><input type="tel" name="phone"></td></tr>
                <tr><td>Steuernummer:</td><td><input type="text" name="tax_id"></td></tr>
                <tr><td>Notizen:</td><td><textarea name="notes" rows="3" cols="40"></textarea></td></tr>
                <tr><td></td><td><input type="submit" value="Kontakt hinzufügen"></td></tr>
            </table>
        </form>
    '''
    
    s+= "<h2>Kontakte</h2>"
    s+= "<table border='1'>"
    s+= "<tr><th>ID</th><th>Typ</th><th>Nr.</th><th>Name</th><th>Firma</th><th>E-Mail</th><th>Telefon</th><th>Aktionen</th></tr>"
    
    # Contact type labels in German
    type_labels = {
        'customer': 'Kunde',
        'supplier': 'Lieferant',
        'own': 'Eigene Daten',
        'insurance': 'Versicherung',
        'other': 'Sonstiges'
    }
    
    for contact in contacts:
        contact_id = contact[0]
        contact_type = contact[1] or 'customer'
        customer_number = contact[2] or ''
        name = contact[3]
        company = contact[4] or ''
        email = contact[8] or ''
        phone = contact[9] or ''
        
        type_label = type_labels.get(contact_type, contact_type)
        
        s+= f"<tr>"
        s+= f"<td>{contact_id}</td>"
        s+= f"<td>{type_label}</td>"
        s+= f"<td>{customer_number}</td>"
        s+= f"<td>{name}</td>"
        s+= f"<td>{company}</td>"
        s+= f"<td>{email}</td>"
        s+= f"<td>{phone}</td>"
        s+= f"<td><a href='/contacts/edit?id={contact_id}'>Bearbeiten</a> | <a href='/contacts/delete?id={contact_id}' onclick='return confirm(\"Wirklich löschen?\")'>Löschen</a></td>"
        s+= f"</tr>"
    
    s+= "</table>"
    s+= Footer()
    return s

def PageContactEdit(db: Database, contact_id):
    """Generate contact edit page"""
    contact = db.get_contact_by_id(contact_id)
    if not contact:
        return "Kontakt nicht gefunden."
    
    s = Header1('contacts')
    submenu = '<a href="/contacts">Kontakte</a> -> <span id="ActivePage">Bearbeiten</span>'
    s+= Header2(submenu)
    s+= Header3()
    
    # Extract contact data (ID=0, ContactType=1, CustomerNumber=2, Name=3, Company=4, Street=5, PostalCode=6, City=7, Country=8, Email=9, Phone=10, TaxID=11, Notes=12)
    contact_type = contact[1] or 'customer'
    customer_number = contact[2] or ''
    name = contact[3]
    company = contact[4] or ''
    street = contact[5] or ''
    postal_code = contact[6] or ''
    city = contact[7] or ''
    country = contact[8] or ''
    email = contact[9] or ''
    phone = contact[10] or ''
    tax_id = contact[11] or ''
    notes = contact[12] or ''
    
    s+= "<h1>Kontakt bearbeiten</h1>"
    s+= f'''
        <form method="POST" action="/update_contact">
            <input type="hidden" name="contact_id" value="{contact_id}">
            <table>
                <tr><td>Typ:</td><td>
                    <select name="contact_type">
                        <option value="customer" {"selected" if contact_type == "customer" else ""}>Kunde</option>
                        <option value="supplier" {"selected" if contact_type == "supplier" else ""}>Lieferant</option>
                        <option value="own" {"selected" if contact_type == "own" else ""}>Eigene Daten</option>
                        <option value="insurance" {"selected" if contact_type == "insurance" else ""}>Versicherung</option>
                        <option value="other" {"selected" if contact_type == "other" else ""}>Sonstiges</option>
                    </select>
                </td></tr>
                <tr><td>Kundennummer:</td><td><input type="text" name="customer_number" value="{customer_number}"></td></tr>
                <tr><td>Name:</td><td><input type="text" name="name" value="{name}" required></td></tr>
                <tr><td>Firma:</td><td><input type="text" name="company" value="{company}"></td></tr>
                <tr><td>Straße:</td><td><input type="text" name="street" value="{street}"></td></tr>
                <tr><td>PLZ:</td><td><input type="text" name="postal_code" value="{postal_code}"></td></tr>
                <tr><td>Stadt:</td><td><input type="text" name="city" value="{city}"></td></tr>
                <tr><td>Land:</td><td><input type="text" name="country" value="{country}"></td></tr>
                <tr><td>E-Mail:</td><td><input type="email" name="email" value="{email}"></td></tr>
                <tr><td>Telefon:</td><td><input type="tel" name="phone" value="{phone}"></td></tr>
                <tr><td>Steuernummer:</td><td><input type="text" name="tax_id" value="{tax_id}"></td></tr>
                <tr><td>Notizen:</td><td><textarea name="notes" rows="3" cols="40">{notes}</textarea></td></tr>
                <tr><td></td><td><input type="submit" value="Kontakt aktualisieren"></td></tr>
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

def PageBookingGroups(db: Database):
    """Page for managing split bookings (BookingGroups)"""
    s = Header1('bookinggroups')
    s += Header2()
    s += Header3()
    
    s += "<h1>Split-Buchungen verwalten</h1>"
    s += "<p>Hier können Sie zusammengehörige Buchungen gruppieren (z.B. Rechnungssplitting).</p>"
    
    # Form to create new booking group
    s += '''
    <h2>Neue Split-Buchung erstellen</h2>
    <form method="POST" action="/bookinggroups/create">
        <table>
            <tr><td>Beschreibung:</td><td><input type="text" name="description" size="60" placeholder="z.B. Rechnung XY123 aufgeteilt"></td></tr>
            <tr><td>Erwarteter Gesamtbetrag:</td><td><input type="number" step="0.01" name="total_amount" placeholder="Optional für Kontrolle"></td></tr>
            <tr><td></td><td><input type="submit" value="Gruppe erstellen"></td></tr>
        </table>
    </form>
    '''
    
    # List existing booking groups
    groups = db.fetch_booking_groups()
    
    s += "<h2>Vorhandene Split-Buchungen</h2>"
    if groups:
        s += "<table border='1'>"
        s += "<tr><th>ID</th><th>Beschreibung</th><th>Erstellt am</th><th>Erwarteter Betrag</th><th>Tatsächlicher Betrag</th><th>Anzahl Buchungen</th><th>Aktionen</th></tr>"
        
        for group in groups:
            group_id = group[0]
            description = group[1] or ""
            created_date = group[2] or ""
            expected_amount = group[3]
            
            # Get bookings in this group and calculate actual total
            bookings = db.get_bookings_in_group(group_id)
            actual_amount = sum(b[10] for b in bookings)  # Amount is at index 10
            booking_count = len(bookings)
            
            # Check if amounts match
            match_color = "green" if expected_amount and abs(expected_amount - actual_amount) < 0.01 else "black"
            
            s += f"<tr>"
            s += f"<td>{group_id}</td>"
            s += f"<td>{description}</td>"
            s += f"<td>{created_date}</td>"
            s += f"<td>{expected_amount:.2f if expected_amount else '-'}</td>"
            s += f"<td style='color:{match_color}'>{actual_amount:.2f}</td>"
            s += f"<td>{booking_count}</td>"
            s += f"<td><a href='/bookinggroups/view?id={group_id}'>Details</a></td>"
            s += f"</tr>"
        
        s += "</table>"
    else:
        s += "<p>Noch keine Split-Buchungen vorhanden.</p>"
    
    s += Footer()
    return s

def PageBookingGroupDetails(db: Database, group_id):
    """Page showing details of a specific booking group"""
    s = Header1('bookinggroups')
    s += Header2()
    s += Header3()
    
    # Get group info
    groups = db.fetch_booking_groups()
    group = None
    for g in groups:
        if g[0] == group_id:
            group = g
            break
    
    if not group:
        s += "<h1>Gruppe nicht gefunden</h1>"
        s += f"<p><a href='/bookinggroups'>Zurück zur Übersicht</a></p>"
        s += Footer()
        return s
    
    description = group[1] or "Ohne Beschreibung"
    created_date = group[2] or ""
    expected_amount = group[3]
    
    s += f"<h1>Split-Buchung: {description}</h1>"
    s += f"<p>Erstellt am: {created_date}</p>"
    
    # Get bookings in this group
    bookings = db.get_bookings_in_group(group_id)
    
    if bookings:
        actual_amount = sum(b[10] for b in bookings)
        
        s += f"<p><strong>Erwarteter Gesamtbetrag:</strong> {expected_amount:.2f if expected_amount else '-'} EUR</p>"
        s += f"<p><strong>Tatsächlicher Gesamtbetrag:</strong> {actual_amount:.2f} EUR</p>"
        
        if expected_amount and abs(expected_amount - actual_amount) > 0.01:
            diff = actual_amount - expected_amount
            s += f"<p style='color:red'><strong>Differenz:</strong> {diff:.2f} EUR</p>"
        
        s += "<h2>Buchungen in dieser Gruppe</h2>"
        s += "<table border='1'>"
        s += "<tr><th>ID</th><th>Datum</th><th>Empfänger</th><th>Text</th><th>Betrag</th><th>Konto</th><th>Aktionen</th></tr>"
        
        # Get account names
        accounts = db.fetch_accounts()
        account_map = {acc[0]: acc[1] for acc in accounts}
        
        for booking in bookings:
            booking_id = booking[0]
            date_booking = booking[1]
            account_id = booking[4]
            recipient = booking[6] or ""
            amount = booking[10]
            text = booking[14] or ""
            
            account_name = account_map.get(account_id, "") if account_id else ""
            amount_color = "green" if amount > 0 else "red"
            
            s += f"<tr>"
            s += f"<td>{booking_id}</td>"
            s += f"<td>{date_booking}</td>"
            s += f"<td>{recipient[:30]}</td>"
            s += f"<td>{text[:40]}</td>"
            s += f"<td style='color:{amount_color}'>{amount:.2f}</td>"
            s += f"<td>{account_name}</td>"
            s += f"<td><a href='/transactions/edit?id={booking_id}'>Bearbeiten</a></td>"
            s += f"</tr>"
        
        s += "</table>"
    else:
        s += "<p>Keine Buchungen in dieser Gruppe.</p>"
    
    s += "<p><a href='/bookinggroups'>Zurück zur Übersicht</a></p>"
    s += Footer()
    return s

