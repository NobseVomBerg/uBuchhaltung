"""
HTML page generation functions
All functions return complete HTML strings
"""
from db import Database

def Header1(active_page=None):
    """Generate main header with navigation
    
    Args:
        active_page: Name of active page ('dashboard', 'receipts', 'transactions', 'skr', 'miscellaneous', 'about')
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
    
    # Stammdaten (Master Data)
    if active_page == 'masterdata':
        nav_items.append('<span id="ActivePage">Stammdaten</span>')
    else:
        nav_items.append('<a href="/masterdata">Stammdaten</a>')
    
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
    
    # Anlagen
    if active_page == 'assets':
        nav_items.append('<span id="ActivePage">Anlagen</span>')
    else:
        nav_items.append('<a href="/assets">Anlagen</a>')
    
    # Sonstiges
    if active_page == 'miscellaneous':
        nav_items.append('<span id="ActivePage">Sonstiges</span>')
    else:
        nav_items.append('<a href="/miscellaneous">Sonstiges</a>')
    
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

def PageAbout():
    """Generate about page"""
    s = Header1('about')
    s+= Header2()
    s+= Header3()
    s+= "<p>Einfache Buchführungssoftware.</p>"
    s+= Footer()
    return s

def PageSettings(db: Database):
    """Alias kept for backward compatibility – delegates to PageMiscellaneous."""
    from .pages_miscellaneous import PageMiscellaneous
    return PageMiscellaneous(db)

def PageReceipts(db: Database):
    """Generate receipts page with upload functionality"""
    import datetime
    current_year = datetime.datetime.now().year
    
    rows = db.fetch_receipts()
    
    # Get next receipt number from number ranges (company receipts)
    receipt_ranges = db.fetch_number_ranges('receipt_company')
    next_receipt_number = ''
    if receipt_ranges:
        # Find the range for current year, or use the first available
        current_range = None
        for r in receipt_ranges:
            if r[2] == current_year:
                current_range = r
                break
        if not current_range and receipt_ranges:
            current_range = receipt_ranges[0]
        
        if current_range:
            year = current_range[2]
            letter = current_range[3]
            prefix = current_range[4] or ''
            current_num = current_range[5] or 0
            next_num = current_num + 1
            year_short = str(year)[-2:]
            next_receipt_number = f"{year_short}{letter}{prefix}{next_num:03d}"
    
    s = Header1('receipts')
    s+= Header2()
    
    # Header3 with date filter
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
    '''
    s+= f'<tr><td>Nummer:</td><td><input type="text" name="number" value="{next_receipt_number}"></td></tr>'
    s+= '''
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
                    uploadStatus.innerHTML = '<p class="successColor">' + data + '</p>';
                    setTimeout(() => { 
                        uploadStatus.innerHTML = ''; 
                        location.reload();
                    }, 3000);
                }
            })
            .catch(error => {
                uploadStatus.innerHTML = '<p class="errorColor">Fehler beim Hochladen: ' + error + '</p>';
            });
        }
    </script>
    '''
    
    s+= "<h2>Vorhandene Belege</h2>"
    s+= "<table>"
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
        s+= "<table>"
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
            s+= "<table>"
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
                    uploadStatus.innerHTML = '<p class="successColor">' + data + '</p>';
                    setTimeout(() => { 
                        uploadStatus.innerHTML = ''; 
                        location.reload();
                    }, 3000);
                }
            })
            .catch(error => {
                uploadStatus.innerHTML = '<p class="errorColor">Fehler beim Hochladen: ' + error + '</p>';
            });
        }
    </script>
    '''
    
    s+= "<h2>Kontobewegungen</h2>"
    s+= "<table>"
    s+= "<tr><th>Datum</th><th>Empfänger/Auftragg.</th><th>Text</th><th>Betrag</th><th>Währung</th><th>Konto</th><th>Kunde</th><th>SKR</th><th>Beleg-Nr.</th><th>Aktionen</th></tr>"
    
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
        doc_number = booking[15] or "" if len(booking) > 15 else ""
        
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
        s+= f"<td>{doc_number}</td>"
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
                const rowCustomerId = row.getAttribute('data-contact-id');
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
    s+= "<table>"
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

def PageInvoice(db: Database, filters: dict = None):
    """Generate invoice list page with search and filter"""
    filters = filters or {}
    
    # Get filter parameters
    status_filter = filters.get('status', '')
    search_query = filters.get('search', '')
    date_from = filters.get('date_from', '')
    date_to = filters.get('date_to', '')
    
    # Fetch invoices with optional status filter
    if status_filter and status_filter != 'all':
        invoices = db.fetch_invoices(status=status_filter)
    else:
        invoices = db.fetch_invoices()
    
    # Apply additional filters in Python
    if search_query:
        search_lower = search_query.lower()
        invoices = [inv for inv in invoices 
                   if search_lower in str(inv[1]).lower()  # InvoiceNumber
                   or search_lower in str(inv[12]).lower()]  # BuyerName
    
    if date_from:
        invoices = [inv for inv in invoices if inv[2] >= date_from]  # InvoiceDate
    
    if date_to:
        invoices = [inv for inv in invoices if inv[2] <= date_to]
    
    s = Header1('invoice')
    submenu = '<span id="ActivePage">Liste</span> | <a href="/invoice/new">Neu</a>'
    s+= Header2(submenu)

    # Header3 with filters (date, status, search)
    import datetime
    current_year = datetime.datetime.now().year
    
    # Prepare status selection
    status_options = {
        'all': 'Alle',
        'draft': 'Entwurf',
        'finalized': 'Abgeschlossen',
        'sent': 'Versendet',
        'paid': 'Bezahlt',
        'cancelled': 'Storniert'
    }
    
    status_dropdown = '<select id="statusFilter" onchange="applyInvoiceFilters()" style="width: 120px;">'
    for value, label in status_options.items():
        selected = 'selected' if status_filter == value or (not status_filter and value == 'all') else ''
        status_dropdown += f'<option value="{value}" {selected}>{label}</option>'
    status_dropdown += '</select>'
    
    header3_content = f'''
        <div style="display: flex; gap: 15px; align-items: center; flex-wrap: wrap;">
            <div>
                <label>Von:</label> <input type="date" id="dateFrom" value="{date_from}" onchange="applyInvoiceFilters()"> 
                <label> Bis:</label> <input type="date" id="dateTo" value="{date_to}" onchange="applyInvoiceFilters()">
                <button onclick="setInvoiceYear({current_year})">{current_year}</button>
                <button onclick="setInvoiceYear({current_year-1})">{current_year-1}</button>
                <button onclick="setInvoiceYear({current_year-2})">{current_year-2}</button>
                <button onclick="setInvoiceYear({current_year-3})">{current_year-3}</button>
            </div>
            <div>
                <label>Status:</label> {status_dropdown}
            </div>
            <div>
                <label>🔍 Suche:</label> <input type="text" id="searchQuery" value="{search_query}" placeholder="RE-Nr. oder Kunde" onchange="applyInvoiceFilters()" style="width: 200px;">
            </div>
        </div>
    '''
    s+= Header3(header3_content)

    # Statistics summary
    total_count = len(invoices)
    total_sum = sum(inv[36] for inv in invoices if len(inv) > 36 and inv[36])  # SumGross is at index 36
    paid_sum = sum(inv[36] for inv in invoices if len(inv) > 36 and inv[36] and len(inv) > 38 and inv[38] == 'paid')  # Status at 38
    open_sum = sum(inv[36] for inv in invoices if len(inv) > 36 and inv[36] and len(inv) > 38 and inv[38] not in ['paid', 'cancelled'])
    
    s+= f'''
    <div class="rectRounded">
        <strong>Statistik:</strong> 
        {total_count} Rechnung(en) | 
        Gesamtsumme: {total_sum:.2f} € | 
        Bezahlt: {paid_sum:.2f} € | 
        Offen: {open_sum:.2f} €
    </div>
    '''
    
    if not invoices:
        s+= "<p><em>Keine Rechnungen gefunden.</em></p>"
    else:
        s+= "<table style='width: 100%;'>"
        s+= "<tr><th>RE-Nr.</th><th>Datum</th><th>Kunde</th><th>Netto</th><th>Brutto</th><th>Status</th><th>Aktionen</th></tr>"
        
        for invoice in invoices:
            # invoice: ID=0, InvoiceNumber=1, InvoiceDate=2, OwnCompanyId=3, SellerName=4, SellerCompany=5, ..., CustomerId=13, BuyerName=14, BuyerCompany=15, ..., TaxRate=35, SumNet=36, TaxAmount=37, SumGross=38, AmountDue=39, Status=40, PDFPath=41
            inv_id = invoice[0]
            inv_number = invoice[1]
            inv_date = invoice[2]
            buyer_name = invoice[14]  # BuyerName is now at index 14 (was 13)
            sum_net = invoice[36]  # SumNet is at index 36 (was 34)
            sum_gross = invoice[38]  # SumGross is at index 38 (was 36)
            status = invoice[40]  # Status is at index 40 (was 38)
            pdf_path = invoice[41] if len(invoice) > 41 else None  # PDFPath is at index 41 (was 39)
            
            # Status color
            status_colors = {
                'draft': '#888',
                'finalized': '#0066cc',
                'sent': '#ff9900',
                'paid': '#00aa00',
                'cancelled': '#cc0000'
            }
            status_text = {
                'draft': 'Entwurf',
                'finalized': 'Abgeschlossen',
                'sent': 'Versendet',
                'paid': 'Bezahlt',
                'cancelled': 'Storniert'
            }
            status_color = status_colors.get(status, '#888')
            status_label = status_text.get(status, status)
            
            s+= f"<tr>"
            s+= f"<td>{inv_number}</td>"
            s+= f"<td>{inv_date}</td>"
            s+= f"<td>{buyer_name[:30]}</td>"
            s+= f"<td style='text-align: right;'>{sum_net:.2f} €</td>"
            s+= f"<td style='text-align: right;'><strong>{sum_gross:.2f} €</strong></td>"
            s+= f"<td style='color: {status_color};'><strong>{status_label}</strong></td>"
            s+= f"<td>"
            # PDF button - check if PDF actually exists in filesystem
            import os
            pdf_exists = "true" if (pdf_path and os.path.exists(pdf_path)) else "false"
            s+= f'<a href="javascript:void(0);" onclick="handlePDF({inv_id}, {pdf_exists})">📄 PDF</a> | '
            s+= f"<a href='/invoice/view?id={inv_id}'>Ansicht</a>"
            s+= f"</td>"
            s+= f"</tr>"
        
        s+= "</table>"
    
    s+= '''
    <script>
        function handlePDF(invoiceId, pdfExists) {
            if (pdfExists) {
                // PDF exists - ask if user wants to regenerate
                if (confirm('PDF-Datei existiert bereits. Möchten Sie die Datei überschreiben und neu generieren?')) {
                    generatePDFInFilesystem(invoiceId);
                }
            } else {
                // PDF doesn't exist - generate it
                generatePDFInFilesystem(invoiceId);
            }
        }
        
        function generatePDFInFilesystem(invoiceId) {
            // Generate PDF in filesystem (no download)
            fetch('/invoice/pdf_generate?id=' + invoiceId)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        alert('PDF erfolgreich erstellt:\\n' + data.pdf_path);
                    } else {
                        alert('Fehler beim Erstellen der PDF: ' + (data.error || 'Unbekannter Fehler'));
                    }
                })
                .catch(err => {
                    alert('Fehler: ' + err.message);
                });
        }
        
        function setInvoiceYear(year) {
            document.getElementById('dateFrom').value = year + '-01-01';
            document.getElementById('dateTo').value = year + '-12-31';
            applyInvoiceFilters();
        }
        
        function applyInvoiceFilters() {
            const dateFrom = document.getElementById('dateFrom').value;
            const dateTo = document.getElementById('dateTo').value;
            const status = document.getElementById('statusFilter').value;
            const search = document.getElementById('searchQuery').value;
            
            // Build query string
            const params = new URLSearchParams();
            if (dateFrom) params.append('date_from', dateFrom);
            if (dateTo) params.append('date_to', dateTo);
            if (status && status !== 'all') params.append('status', status);
            if (search) params.append('search', search);
            
            // Reload page with filters
            window.location.href = '/invoice' + (params.toString() ? '?' + params.toString() : '');
        }
        
        function resetInvoiceFilters() {
            window.location.href = '/invoice';
        }
    </script>
    '''
    
    s+= Footer()
    return s

def PageInvoiceNew(db: Database, invoice_id=None):
    """Generate invoice creation/edit page"""
    import datetime
    import json
    current_year = datetime.datetime.now().year
    
    # Load existing invoice if invoice_id is provided
    existing_invoice = None
    existing_items = []
    if invoice_id:
        existing_invoice = db.get_invoice_by_id(invoice_id)
        if existing_invoice:
            existing_items = db.get_invoice_items(invoice_id)
    
    # Get company data (own contact)
    own_contacts = db.fetch_contacts(contact_type='own')
    own_contact = own_contacts[0] if own_contacts else None
    
    # Get customers for selection
    customers = db.fetch_contacts(contact_type='customer')
    
    # Get bank accounts for selection
    accounts = db.fetch_accounts()
    
    # Get only active articles for selection in invoices
    articles = db.fetch_articles(active_only=True)
    
    # Determine invoice number and initial values
    if existing_invoice:
        # Editing existing invoice - use existing data
        # existing_invoice structure: ID=0, InvoiceNumber=1, InvoiceDate=2, OwnCompanyId=3, SellerName=4, SellerCompany=5, ..., CustomerId=13, BuyerName=14, BuyerCompany=15, ..., Status=40, PDFPath=41
        invoice_number = existing_invoice[1]
        invoice_date = existing_invoice[2]
        selected_own_company_id = existing_invoice[3]
        selected_customer_id = existing_invoice[13]  # CustomerId is now at index 13 (was 12)
        invoice_status = existing_invoice[40]  # Status is now at index 40 (was 37)
        pdf_path = existing_invoice[41] if len(existing_invoice) > 41 else None  # PDFPath is now at index 41 (was 39)
        page_title = f"Rechnung {invoice_number} bearbeiten"
        is_edit_mode = True
    else:
        # Creating new invoice - generate next number
        invoice_ranges = db.fetch_number_ranges('invoice')
        invoice_number = ''
        if invoice_ranges:
            # Find the range for current year, or use the first available
            current_range = None
            for r in invoice_ranges:
                if r[2] == current_year:
                    current_range = r
                    break
            if not current_range and invoice_ranges:
                current_range = invoice_ranges[0]
            
            if current_range:
                # r: ID=0, Type=1, Year=2, Letter=3, Prefix=4, CurrentNumber=5, Description=6
                year = current_range[2]
                letter = current_range[3]
                prefix = current_range[4] or ''
                current_num = current_range[5] or 0
                next_num = current_num + 1
                year_short = str(year)[-2:]
                invoice_number = f"{year_short}{letter}{prefix}{next_num:03d}"
        
        invoice_date = ''
        selected_own_company_id = own_contact[0] if own_contact else None
        selected_customer_id = None
        invoice_status = 'draft'
        pdf_path = None
        page_title = "Neue Rechnung erstellen"
        is_edit_mode = False
    
    s = Header1('invoice')
    submenu = '<a href="/invoice">Liste</a> | <span id="ActivePage">Neu</span>'
    s+= Header2(submenu)
    s+= Header3()
    s += f'<input type="hidden" id="invoice_id" value="{invoice_id or ""}">'
    s += f'<input type="hidden" id="is_edit_mode" value="{str(is_edit_mode).lower()}">'
    # Check if PDF actually exists in filesystem
    import os
    pdf_file_exists = bool(pdf_path and os.path.exists(pdf_path))
    s += f'<input type="hidden" id="pdf_exists" value="{str(pdf_file_exists).lower()}">'
    s += f'<h2>{page_title}</h2>'
    s += '''<div class="invoice-container" id="invoice_container">
        <div class="invoice-header">
            <div class="invoice-logo">
                <img id="company_logo" src="" alt="Firmenlogo" style="max-width: 150px; max-height: 80px;" onerror="this.style.display='none';">
            </div>
            <div class="invoice-meta">
                <table class="invoice-meta-table">
                    <tr><td>Datum:</td><td><input type="date" id="invoice_date" value="'''
    s += invoice_date if invoice_date else ''
    s += '''" style="width: 150px;"></td></tr>
    '''
    s += f'<tr><td>Rechnungs-Nr.:</td><td><input type="text" id="invoice_number" value="{invoice_number}" style="width: 150px;"></td></tr>'
    s += '''
                    <tr><td>Kunden-Nr.:</td><td><input type="text" id="customer_number" readonly style="width: 150px; background-color: #f0f0f0;"></td></tr>
                </table>
            </div>
        </div>
        
        <div class="invoice-address-block">
            <div class="invoice-customer-address">
                <select id="own_company_select" onchange="updateOwnCompany()" style="margin-bottom: 10px; width: 100%;" class="no-pdf">
                    <option value="">-- Eigene Firma auswählen --</option>
    '''
    
    for own in own_contacts:
        own_name = own[3] or f"ID {own[0]}"  # display_name at index 3
        selected = 'selected' if (existing_invoice and own[0] == selected_own_company_id) or (not existing_invoice and own_contact and own[0] == own_contact[0]) else ''
        s += f'<option value="{own[0]}" {selected}>{own_name}</option>'
    
    s += '''
                </select>
            <div class="invoice-sender-line" id="sender_line">
    '''
    
    if own_contact:
        sender_street = own_contact[5] or ''
        sender_postal = own_contact[6] or ''
        sender_city   = own_contact[7] or ''
        sender_name   = own_contact[3] or ''  # display_name
        s += f'{sender_name} · {sender_street} · {sender_postal} {sender_city}'
    else:
        s += 'Eigene Adresse in Kontakte anlegen (Typ: own)'
    
    s += '''            </div>
                <select id="customer_select" onchange="updateCustomerAddress()" style="margin-bottom: 10px; width: 100%;" class="no-pdf">
                    <option value="">-- Kunde auswählen --</option>
    '''
    
    for customer in customers:
        cust_name = customer[3] or f"ID {customer[0]}"  # display_name at index 3
        selected = 'selected' if existing_invoice and customer[0] == selected_customer_id else ''
        s += f'<option value="{customer[0]}" {selected}>{cust_name}</option>'
    
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
            <tbody id="invoice_items_body">'''
    
    # Add existing items or one empty row
    if existing_items:
        for idx, item in enumerate(existing_items, 1):
            # item: ID=0, InvoiceId=1, Position=2, ArticleId=3, Description=4, Quantity=5, Unit=6, PricePerUnit=7, TotalNet=8, TaxCategory=9, TaxRate=10
            quantity = item[5]
            unit = item[6] or 'Stk.'
            description = item[4] or ''
            price = item[7]
            total = item[8]
            s += f'''
                <tr class="invoice-item-row" data-row="{idx}">
                    <td>{idx}</td>
                    <td><input type="number" class="item-quantity" value="{quantity}" min="0" step="0.01" style="width: 60px;"></td>
                    <td><input type="text" class="item-unit" value="{unit}" style="width: 60px;"></td>
                    <td><input type="text" class="item-description" value="{description}" style="width: 100%;"></td>
                    <td><input type="number" class="item-price" value="{price}" min="0" step="0.01" style="width: 80px;"> €</td>
                    <td class="item-total" style="text-align: right;">{total:.2f} €</td>
                    <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;">✕</button></td>
                </tr>'''
    else:
        s += '''
                <tr class="invoice-item-row" data-row="1">
                    <td>1</td>
                    <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width: 60px;"></td>
                    <td><input type="text" class="item-unit" value="Stk." style="width: 60px;"></td>
                    <td><input type="text" class="item-description" style="width: 100%;"></td>
                    <td><input type="number" class="item-price" value="0" min="0" step="0.01" style="width: 80px;"> €</td>
                    <td class="item-total" style="text-align: right;">0,00 €</td>
                    <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;">✕</button></td>
                </tr>'''
    
    s += '''
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
        
        <div style="margin: 10px 0;" class="no-pdf">
            <button type="button" onclick="addFreeRow()" style="margin-right: 10px;">+ Position frei editierbar hinzufügen</button>
            <button type="button" onclick="showArticleModal()">+ Position aus Artikelverzeichnis</button>
        </div>
        
        <!-- Article selection modal -->
        <div id="articleModal" class="modal-overlay no-pdf">
            <div class="modal-content">
                <h3>Artikel aus Verzeichnis auswählen</h3>
                <table border="1" style="width: 100%;">
                    <tr><th>Bezeichnung</th><th>Einheit</th><th>Preis (netto)</th><th>MwSt</th><th></th></tr>
    '''
    
    for article in articles:
        art_id = article[0]
        art_name = article[1] or ''
        art_unit = article[2] or 'Stk.'
        art_price = article[3] or 0
        art_tax = article[4] or 19
        s += f'''                    <tr>
                        <td>{art_name}</td>
                        <td>{art_unit}</td>
                        <td style="text-align: right;">{art_price:.2f} €</td>
                        <td>{art_tax:.0f}%</td>
                        <td><button type="button" class="modal-button-add" onclick="addArticleRow({art_id})">Hinzufügen</button></td>
                    </tr>
    '''
    
    s += '''                </table>
                <br>
                <button type="button" class="modal-button-close" onclick="hideArticleModal()">Schließen</button>
            </div>
        </div>
        
        <div class="invoice-payment-terms">'''
    
    # Payment terms text
    payment_terms_text = existing_invoice[26] if existing_invoice and existing_invoice[26] else 'Bitte überweisen Sie den Gesamtbetrag ohne jeden Abzug unter Angabe der Rechnungsnummer innerhalb von 14 Tagen ab Rechnungsdatum auf das unten angegebene Konto. Vielen Dank.'
    s += f'<textarea id="payment_terms" rows="3" style="width: 100%;">{payment_terms_text}</textarea>'
    
    s += '''
        </div>
        
        <div class="invoice-footer">
            <hr>
            <table class="footer-table">
                <tr>
                    <td class="footer-col-left">
                        <strong>Anschrift</strong><br>
                        <div id="footer_company_address">
    '''
    
    if own_contact:
        addr_line1_html = f'{own_contact[25]}<br>' if (len(own_contact) > 25 and own_contact[25]) else ''
        s += f'''                        {own_contact[3] or 'Firma'}<br>
                        {addr_line1_html}                        {own_contact[5] or 'Straße'}<br>
                        {own_contact[6] or 'PLZ'} {own_contact[7] or 'Ort'}'''
    else:
        s += '                        <em>Eigene Firmendaten in Kontakte anlegen</em>'
    
    s += '''                        </div>
                    </td>
                    <td class="footer-col-center">
                        <strong>Kontakt</strong><br>
                        <div id="footer_company_contact">
    '''
    
    if own_contact:
        s += f'''                        <span class="footer-label">Tel</span> {own_contact[10] or '-'}<br>
                        <span class="footer-label">E-Mail</span> {own_contact[9] or '-'}<br>
                        <span class="footer-label">UStIdNr</span> {own_contact[11] or '-'}'''
    else:
        s += '                        <em>Kontaktdaten fehlen</em>'
    
    s += '''                        </div>
                    </td>
                    <td class="footer-col-right">
                        <strong>Bankverbindung</strong><br>
                        <select id="bank_account_select" onchange="updateBankDetails()" style="width: 100%; margin-top: 5px;" class="no-pdf">
                            <option value="">-- Konto auswählen --</option>
    '''
    
    # BankAccountId is now at index 30 (was 28)
    selected_bank_id = existing_invoice[30] if existing_invoice and len(existing_invoice) > 30 and existing_invoice[30] else None
    for account in accounts:
        if not account[6]:  # Skip cash accounts (IsCash=1)
            selected = 'selected' if selected_bank_id and account[0] == selected_bank_id else ''
            s += f'<option value="{account[0]}" {selected}>{account[1]}</option>'
    
    s += '''                        </select>
                        <div id="bank_details" style="margin-top: 5px;">
                            
                        </div>
                    </td>
                </tr>
            </table>
        </div>
    </div>
    
    <!-- XRechnung / E-Rechnung Zusatzdaten (optional) -->
    <div class="xrechnung-section no-pdf" style="margin: 20px 0; padding: 20px; background-color: #f9f9f9; border: 1px solid #ddd; border-radius: 5px;">
        <h3 style="margin-top: 0; cursor: pointer;" onclick="toggleXRechnungFields()">
            ⚙️ XRechnung / E-Rechnung Zusatzdaten (optional) <span id="xrechnung_toggle">▼</span>
        </h3>
        <div id="xrechnung_fields" style="display: none;">
            <table style="width: 100%; border-collapse: collapse;">
                <tr>
                    <td style="padding: 8px; width: 200px; vertical-align: top;"><strong>Bestellnummer:</strong></td>
                    <td style="padding: 8px;">
                        <input type="text" id="order_number" style="width: 300px;" placeholder="z.B. PO-2026-12345" value="'''
    s += existing_invoice[23] if existing_invoice and len(existing_invoice) > 23 and existing_invoice[23] else ''  # OrderNumber is now at index 23 (was 21)
    s += '''">
                        <br><small style="color: #666;">Bestellnummer des Kunden (falls vorhanden)</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Leitweg-ID:</strong></td>
                    <td style="padding: 8px;">
                        <input type="text" id="buyer_route_id" style="width: 300px;" placeholder="z.B. 991-ABCDE-12" readonly value="'''
    s += existing_invoice[22] if existing_invoice and len(existing_invoice) > 22 and existing_invoice[22] else ''  # BuyerRouteID is now at index 22 (was 20)
    s += '''">
                        <br><small style="color: #666;">Wird automatisch vom Kunden übernommen (nur bei B2G-Rechnungen erforderlich)</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Lieferdatum:</strong></td>
                    <td style="padding: 8px;">
                        <input type="date" id="delivery_date" style="width: 200px;" value="'''
    s += existing_invoice[25] if existing_invoice and len(existing_invoice) > 25 and existing_invoice[25] else ''  # DeliveryDate is now at index 25 (was 23)
    s += '''">
                        <br><small style="color: #666;">Datum der Lieferung/Leistungserbringung (optional)</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Zahlungsart (Code):</strong></td>
                    <td style="padding: 8px;">
                        <select id="payment_means_code" style="width: 300px;">
                            <option value="58" selected>58 - SEPA Überweisung</option>
                            <option value="30">30 - Banküberweisung</option>
                            <option value="48">48 - Debitkarte</option>
                            <option value="54">54 - Kreditkarte</option>
                            <option value="49">49 - Lastschrift</option>
                            <option value="1">1 - Bargeld</option>
                        </select>
                        <br><small style="color: #666;">XRechnung Zahlungsart-Code (Standard: 58)</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Zahlungsart (Text):</strong></td>
                    <td style="padding: 8px;">
                        <input type="text" id="payment_means_text" value="SEPA Überweisung" style="width: 300px;">
                        <br><small style="color: #666;">Beschreibung der Zahlungsart</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Zahlungsziel (Tage):</strong></td>
                    <td style="padding: 8px;">
                        <input type="number" id="payment_term_days" value="'''
    # Calculate payment term days from due date and invoice date if available
    if existing_invoice and existing_invoice[27] and existing_invoice[2]:  # PaymentDueDate is now at index 27 (was 25)
        from datetime import datetime
        try:
            due_date = datetime.strptime(existing_invoice[27], '%Y-%m-%d')
            inv_date = datetime.strptime(existing_invoice[2], '%Y-%m-%d')
            term_days = (due_date - inv_date).days
            s += str(term_days)
        except:
            s += '14'
    else:
        s += '14'
    s += '''" min="0" style="width: 100px;"> Tage
                        <br><small style="color: #666;">Anzahl der Tage bis zur Fälligkeit (Standard: 14 Tage)</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Fälligkeitsdatum:</strong></td>
                    <td style="padding: 8px;">
                        <input type="date" id="payment_due_date" style="width: 200px;" value="'''
    s += existing_invoice[27] if existing_invoice and len(existing_invoice) > 27 and existing_invoice[27] else ''  # PaymentDueDate is now at index 27 (was 25)
    s += '''">
                        <br><small style="color: #666;">Wird automatisch berechnet oder kann manuell festgelegt werden</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Skonto:</strong></td>
                    <td style="padding: 8px;">
                        <input type="number" id="discount_percentage" min="0" max="100" step="0.1" style="width: 80px;" placeholder="z.B. 2" value="'''
    s += str(existing_invoice[29]) if existing_invoice and len(existing_invoice) > 29 and existing_invoice[29] else ''  # SkontoPercent is now at index 29 (was 27)
    s += '''"> %
                        bei Zahlung innerhalb von
                        <input type="number" id="discount_days" min="0" style="width: 80px;" placeholder="z.B. 7" value="'''
    s += str(existing_invoice[28]) if existing_invoice and len(existing_invoice) > 28 and existing_invoice[28] else ''  # SkontoDays is now at index 28 (was 26)
    s += '''"> Tagen
                        <br><small style="color: #666;">Skonto-Abzug bei frühzeitiger Zahlung (optional)</small>
                    </td>
                </tr>
            </table>
        </div>
    </div>
    
    <div style="text-align: center; margin: 20px 0;">
        <button type="button" onclick="saveInvoice('draft')" class="save-button" style="background-color: #4CAF50; color: white; padding: 12px 24px; margin: 0 10px; border: none; cursor: pointer; border-radius: 4px;">💾 Als Entwurf speichern</button>
        <button type="button" onclick="saveInvoice('finalized')" class="save-button" style="background-color: #2196F3; color: white; padding: 12px 24px; margin: 0 10px; border: none; cursor: pointer; border-radius: 4px;">✓ Finalisieren und speichern</button>
        <button type="button" onclick="generatePDF()" class="pdf-button no-pdf" style="background-color: #FF9800; color: white; padding: 12px 24px; margin: 0 10px; border: none; cursor: pointer; border-radius: 4px;">📄 Als PDF exportieren</button>
    </div>
    
    <script>
        // Own company data from server
        const ownCompaniesData = '''
    
    # Build JavaScript own companies data object
    own_companies_dict = {}
    for own in own_contacts:
        own_companies_dict[str(own[0])] = {
            'id':      own[0],
            'name':    own[3] or '',             # display_name
            'company': own[4] or own[3] or '',   # company_name fallback to display_name
            'address_line1': own[25] if len(own) > 25 and own[25] else '',
            'street':  own[5] or '',
            'postal':  own[6] or '',
            'city':    own[7] or '',
            'email':   own[9] or '',
            'phone':   own[10] or '',
            'tax_id':  own[11] or '',
            'logo':    own[13] if len(own) > 13 and own[13] else ''
        }
    
    s += json.dumps(own_companies_dict)
    
    s += ''';
        
        // Customer data from server
        const customersData = '''
    
    # Build JavaScript customer data object
    customers_dict = {}
    for customer in customers:
        customers_dict[str(customer[0])] = {
            'id':             customer[0],
            'customer_number':customer[2] or '',
            'name':           customer[3] or '',   # display_name
            'company':        customer[4] or '',   # company_name
            'address_line1':  customer[25] if len(customer) > 25 and customer[25] else '',
            'street':         customer[5] or '',
            'postal':         customer[6] or '',
            'city':           customer[7] or '',
            'buyer_route_id': customer[14] or ''
        }
    
    s += json.dumps(customers_dict)
    
    s += ''';
        
        // Bank accounts data from server
        const banksData = '''
    
    # Build JavaScript bank data object
    banks_dict = {}
    for account in accounts:
        if not account[6]:  # Skip cash accounts
            banks_dict[str(account[0])] = {
                'name': account[1],
                'bank_name': account[5] or '',
                'iban': account[3] or '',
                'bic': account[4] or ''
            }
    
    s += json.dumps(banks_dict)
    
    s += ''';
        
        // Articles data from server
        const articlesData = '''
    
    # Build JavaScript articles data object
    articles_dict = {}
    for article in articles:
        articles_dict[str(article[0])] = {
            'id': article[0],
            'name': article[1] or '',
            'unit': article[2] or 'Stk.',
            'price': article[3] or 0,
            'taxRate': article[4] or 19,
            'description': article[5] or ''
        }
    
    s += json.dumps(articles_dict)
    
    s += ''';
        
        // Set today's date only for new invoices
        const invoiceDate = document.getElementById('invoice_date').value;
        if (!invoiceDate) {
            document.getElementById('invoice_date').valueAsDate = new Date();
        }
        
        // Initialize own company and customer on page load for edit mode
        const isEditMode = document.getElementById('is_edit_mode').value === 'true';
        if (isEditMode) {
            // Trigger updates to populate address fields
            updateOwnCompany();
            updateCustomerAddress();
            updateBankDetails();
            // Recalculate totals
            calculateTotals();
        }
        
        // Update own company data (logo, sender line, footer)
        function updateOwnCompany() {
            const companyId = document.getElementById('own_company_select').value;
            const logoImg = document.getElementById('company_logo');
            const senderLine = document.getElementById('sender_line');
            const footerAddress = document.getElementById('footer_company_address');
            const footerContact = document.getElementById('footer_company_contact');
            
            if (!companyId) {
                // Reset to default
                logoImg.src = '';
                logoImg.style.display = 'none';
                senderLine.textContent = 'Eigene Adresse in Kontakte anlegen (Typ: own)';
                footerAddress.innerHTML = '<em>Eigene Firmendaten in Kontakte anlegen</em>';
                footerContact.innerHTML = '<em>Kontaktdaten fehlen</em>';
                return;
            }
            
            const company = ownCompaniesData[companyId];
            console.log('Selected company:', company);
            
            if (company) {
                // Update logo
                if (company.logo) {
                    console.log('Loading logo from:', company.logo);
                    logoImg.src = company.logo;
                    logoImg.style.display = '';
                    logoImg.onerror = function() {
                        console.error('Failed to load logo from:', this.src);
                        this.style.display = 'none';
                    };
                } else {
                    console.log('No logo specified for this company');
                    logoImg.src = '';
                    logoImg.style.display = 'none';
                }
                
                // Update sender line
                const displayName = company.company || company.name;
                senderLine.textContent = displayName + ' · ' + company.street + ' · ' + company.postal + ' ' + company.city;
                
                // Update footer address
                let addressHtml = displayName + '<br>';
                addressHtml += company.street + '<br>';
                addressHtml += company.postal + ' ' + company.city;
                footerAddress.innerHTML = addressHtml;
                
                // Update footer contact
                let contactHtml = '<span class="footer-label">Tel</span> ' + (company.phone || '-') + '<br>';
                contactHtml += '<span class="footer-label">E-Mail</span> ' + (company.email || '-') + '<br>';
                contactHtml += '<span class="footer-label">UStIdNr</span> ' + (company.tax_id || '-');
                footerContact.innerHTML = contactHtml;
            }
        }
        
        // Update customer address
        function updateCustomerAddress() {
            const customerId = document.getElementById('customer_select').value;
            const addressDisplay = document.getElementById('customer_address_display');
            const customerNumberField = document.getElementById('customer_number');
            const buyerRouteIdField = document.getElementById('buyer_route_id');
            
            console.log('updateCustomerAddress called, customerId:', customerId);
            console.log('customersData:', customersData);
            
            if (!customerId) {
                addressDisplay.innerHTML = '';
                customerNumberField.value = '';
                if (buyerRouteIdField) buyerRouteIdField.value = '';
                return;
            }
            
            const customer = customersData[customerId];
            console.log('Selected customer:', customer);
            if (customer) {
                const displayName = customer.company || customer.name;
                let address = displayName + '\\n';
                if (customer.street) address += customer.street + '\\n';
                if (customer.postal || customer.city) address += (customer.postal + ' ' + customer.city).trim();
                
                addressDisplay.textContent = address;
                customerNumberField.value = customer.customer_number;
                
                // Update Leitweg-ID from customer (for B2G invoices)
                if (buyerRouteIdField) {
                    buyerRouteIdField.value = customer.buyer_route_id || '';
                }
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
        
        // Toggle XRechnung fields visibility
        function toggleXRechnungFields() {
            const fields = document.getElementById('xrechnung_fields');
            const toggle = document.getElementById('xrechnung_toggle');
            if (fields.style.display === 'none') {
                fields.style.display = 'block';
                toggle.textContent = '▲';
            } else {
                fields.style.display = 'none';
                toggle.textContent = '▼';
            }
        }
        
        // Auto-calculate payment due date when invoice date or payment terms change
        function updatePaymentDueDate() {
            const invoiceDate = document.getElementById('invoice_date').value;
            const paymentTermDays = parseInt(document.getElementById('payment_term_days').value) || 14;
            
            if (invoiceDate) {
                const date = new Date(invoiceDate);
                date.setDate(date.getDate() + paymentTermDays);
                const year = date.getFullYear();
                const month = String(date.getMonth() + 1).padStart(2, '0');
                const day = String(date.getDate()).padStart(2, '0');
                document.getElementById('payment_due_date').value = year + '-' + month + '-' + day;
            }
        }
        
        // Attach listeners for automatic due date calculation
        document.getElementById('invoice_date').addEventListener('change', updatePaymentDueDate);
        document.getElementById('payment_term_days').addEventListener('input', updatePaymentDueDate);
        
        // Initial due date calculation
        updatePaymentDueDate();
        
        // Modal functions
        function showArticleModal() {
            document.getElementById('articleModal').style.display = 'block';
        }
        
        function hideArticleModal() {
            document.getElementById('articleModal').style.display = 'none';
        }
        
        // Close modal when clicking outside
        window.onclick = function(event) {
            const modal = document.getElementById('articleModal');
            if (event.target == modal) {
                hideArticleModal();
            }
        }
        
        let rowCounter = 1;
        
        // Add new free editable row
        function addFreeRow() {
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
        
        // Add row from article catalog (only quantity editable)
        function addArticleRow(articleId) {
            const article = articlesData[articleId];
            if (!article) return;
            
            rowCounter++;
            const tbody = document.getElementById('invoice_items_body');
            const newRow = document.createElement('tr');
            newRow.className = 'invoice-item-row';
            newRow.setAttribute('data-row', rowCounter);
            newRow.setAttribute('data-article-id', articleId);
            
            const displayName = article.description ? article.name + ' - ' + article.description : article.name;
            
            newRow.innerHTML = `
                <td>${rowCounter}</td>
                <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width: 60px;"></td>
                <td><span class="item-unit-display">${article.unit}</span><input type="hidden" class="item-unit" value="${article.unit}"></td>
                <td><span class="item-description-display">${displayName}</span><input type="hidden" class="item-description" value="${displayName}"></td>
                <td><span class="item-price-display">${article.price.toFixed(2).replace('.', ',')} €</span><input type="hidden" class="item-price" value="${article.price}"></td>
                <td class="item-total" style="text-align: right;">0,00 €</td>
                <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;">✕</button></td>
            `;
            tbody.appendChild(newRow);
            attachCalculationListeners(newRow);
            calculateTotals();
            hideArticleModal();
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
        
        // Initialize own company (load logo and data for preselected company)
        updateOwnCompany();
        
        // Save invoice to database
        function saveInvoice(status) {
            // Check if we're editing an existing invoice
            const invoiceId = document.getElementById('invoice_id').value;
            const isEdit = invoiceId !== '';
            
            // Get selected own company ID
            const ownCompanyId = document.getElementById('own_company_select').value;
            if (!ownCompanyId) {
                alert('Bitte wählen Sie eine eigene Firma aus.');
                return;
            }
            
            // Get selected customer
            const customerId = document.getElementById('customer_select').value;
            if (!customerId) {
                alert('Bitte wählen Sie einen Kunden aus.');
                return;
            }
            
            // Get invoice number
            const invoiceNumber = document.getElementById('invoice_number').value;
            if (!invoiceNumber) {
                alert('Bitte geben Sie eine Rechnungsnummer ein.');
                return;
            }
            
            // Get invoice date
            const invoiceDate = document.getElementById('invoice_date').value;
            if (!invoiceDate) {
                alert('Bitte wählen Sie ein Rechnungsdatum.');
                return;
            }
            
            // Collect items
            const items = [];
            document.querySelectorAll('.invoice-item-row').forEach((row, index) => {
                const description = row.querySelector('.item-description').value;
                if (description) {  // Only include rows with description
                    items.push({
                        position: index + 1,
                        quantity: parseFloat(row.querySelector('.item-quantity').value) || 1,
                        unit: row.querySelector('.item-unit').value,
                        description: description,
                        unitPrice: parseFloat(row.querySelector('.item-price').value) || 0,
                        totalPrice: parseFloat(row.querySelector('.item-total').textContent.replace('€', '').replace(',', '.').trim()) || 0,
                        taxRate: parseFloat(document.getElementById('tax_rate').value) || 19
                    });
                }
            });
            
            if (items.length === 0) {
                alert('Bitte fügen Sie mindestens eine Position hinzu.');
                return;
            }
            
            // Get bank account ID
            const bankAccountId = document.getElementById('bank_account_select').value || null;
            
            // Calculate amounts (replace comma with dot for German number format)
            const taxRate = parseFloat(document.getElementById('tax_rate').value) || 19;
            const netAmount = parseFloat(document.getElementById('sum_net').textContent.replace('€', '').replace(',', '.').trim());
            const taxAmount = parseFloat(document.getElementById('tax_amount').textContent.replace('€', '').replace(',', '.').trim());
            const grossAmount = parseFloat(document.getElementById('sum_gross').textContent.replace(/<[^>]*>/g, '').replace('€', '').replace(',', '.').trim());
            
            // Get payment terms
            const paymentTermDays = parseInt(document.getElementById('payment_term_days').value) || 14;
            const paymentTerms = document.getElementById('payment_terms').value;
            
            // Calculate due date
            const dueDateField = document.getElementById('payment_due_date').value;
            let dueDate = dueDateField;
            if (!dueDate && invoiceDate) {
                const d = new Date(invoiceDate);
                d.setDate(d.getDate() + paymentTermDays);
                dueDate = d.toISOString().split('T')[0];
            }
            
            // Collect invoice data
            const invoiceData = {
                invoiceNumber: invoiceNumber,
                invoiceDate: invoiceDate,
                customerId: parseInt(customerId),
                customerNumber: document.getElementById('customer_number').value,
                ownCompanyId: parseInt(ownCompanyId),
                buyerReference: document.getElementById('buyer_route_id').value || null,
                paymentTerms: paymentTerms,
                paymentTermsDays: paymentTermDays,
                dueDate: dueDate,
                bankAccountId: bankAccountId ? parseInt(bankAccountId) : null,
                netAmount: netAmount,
                taxRate: taxRate / 100,  // Convert percentage to decimal
                taxAmount: taxAmount,
                grossAmount: grossAmount,
                currency: 'EUR',
                status: status,
                paymentMeansCode: document.getElementById('payment_means_code').value || '58',
                paymentMeansText: document.getElementById('payment_means_text').value || 'SEPA Überweisung',
                items: items,
                // XRechnung optional fields
                orderNumber: document.getElementById('order_number').value || null,
                deliveryDate: document.getElementById('delivery_date').value || null,
                discountPercentage: parseFloat(document.getElementById('discount_percentage').value) || null,
                discountDays: parseInt(document.getElementById('discount_days').value) || null
            };
            
            // Add invoice ID if editing
            if (isEdit) {
                invoiceData.invoiceId = parseInt(invoiceId);
            }
            
            // Send to server
            fetch('/invoice/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(invoiceData)
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    const message = isEdit ? 'Rechnung erfolgreich aktualisiert!' : 'Rechnung erfolgreich gespeichert!\\nRechnungs-ID: ' + data.invoice_id;
                    alert(message);
                    // Redirect to invoice list
                    window.location.href = '/invoice';
                } else {
                    alert('Fehler beim Speichern: ' + (data.error || 'Unbekannter Fehler'));
                }
            })
            .catch(err => {
                alert('Fehler beim Speichern: ' + err);
                console.error('Save error:', err);
            });
        }
        
        // Generate PDF
        function generatePDF() {
            // Check if we're editing an existing invoice
            const invoiceId = document.getElementById('invoice_id').value;
            
            if (!invoiceId) {
                alert('Bitte speichern Sie die Rechnung zuerst, bevor Sie ein PDF erstellen.');
                return;
            }
            
            // Check if PDF already exists
            const pdfExists = document.getElementById('pdf_exists').value === 'true';
            
            if (pdfExists) {
                // PDF exists - ask if user wants to regenerate
                if (!confirm('PDF-Datei existiert bereits. Möchten Sie die Datei überschreiben und neu generieren?')) {
                    return;
                }
            }
            
            // Generate PDF in filesystem (no download)
            fetch('/invoice/pdf_generate?id=' + invoiceId)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        // Update pdf_exists flag
                        document.getElementById('pdf_exists').value = 'true';
                        alert('PDF erfolgreich erstellt:\\n' + data.pdf_path);
                    } else {
                        alert('Fehler beim Erstellen der PDF: ' + (data.error || 'Unbekannter Fehler'));
                    }
                })
                .catch(err => {
                    alert('Fehler: ' + err.message);
                });
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
                    <select name="contact_type" id="contact_type" onchange="toggleBuyerRouteID()">
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
                <tr><td>Land (ISO):</td><td>
                    <select name="country">
                        <option value="DE" selected>Deutschland (DE)</option>
                        <option value="AT">Österreich (AT)</option>
                        <option value="CH">Schweiz (CH)</option>
                        <option value="FR">Frankreich (FR)</option>
                        <option value="NL">Niederlande (NL)</option>
                        <option value="BE">Belgien (BE)</option>
                        <option value="PL">Polen (PL)</option>
                        <option value="IT">Italien (IT)</option>
                        <option value="ES">Spanien (ES)</option>
                        <option value="GB">Großbritannien (GB)</option>
                        <option value="US">USA (US)</option>
                    </select>
                </td></tr>
                <tr><td>E-Mail:</td><td><input type="email" name="email"></td></tr>
                <tr><td>Telefon:</td><td><input type="tel" name="phone"></td></tr>
                <tr><td>USt-IdNr / Steuer-Nr:</td><td><input type="text" name="tax_id" placeholder="z.B. DE123456789"></td></tr>
                <tr id="buyer_route_id_row" style="display: table-row;">
                    <td>Leitweg-ID (B2G):</td>
                    <td>
                        <input type="text" name="buyer_route_id" placeholder="Optional, nur für öffentliche Auftraggeber" style="width: 300px;">
                        <small style="color: #666; display: block;">Format: 12-stellige Nummer-5stellige Prüfziffer (z.B. 991-ABCDE-12)</small>
                    </td>
                </tr>
                <tr><td>Logo:</td><td>
                    <input type="text" id="logo_path" name="logo" placeholder="/static/logo.png oder URL" style="width: 300px;">
                    <button type="button" onclick="document.getElementById('logo_file_picker').click()">Datei wählen</button>
                    <input type="file" id="logo_file_picker" accept="image/*" style="display: none;" onchange="updateLogoPath(this)">
                    <div id="logo_preview" style="margin-top: 5px;"></div>
                </td></tr>
                <tr><td>Notizen:</td><td><textarea name="notes" rows="3" cols="40"></textarea></td></tr>
                <tr><td></td><td><input type="submit" value="Kontakt hinzufügen"></td></tr>
            </table>
        </form>
        
        <script>
            function toggleBuyerRouteID() {
                const contactType = document.getElementById('contact_type').value;
                const routeIdRow = document.getElementById('buyer_route_id_row');
                // Show Leitweg-ID only for customers (could be B2G)
                routeIdRow.style.display = (contactType === 'customer') ? 'table-row' : 'none';
            }
            // Initialize on page load
            toggleBuyerRouteID();
        </script>
    '''
    
    s+= "<h2>Kontakte</h2>"
    s+= "<table>"
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
    s+= '''<script>
        function updateLogoPath(input) {
            if (input.files && input.files[0]) {
                const file = input.files[0];
                const filePath = input.value;
                
                // Try to extract relative path if it's in project directory
                let displayPath = filePath;
                
                // For Windows paths, extract filename and try common patterns
                if (filePath.includes('\\\\')) {
                    const parts = filePath.split('\\\\');
                    const filename = parts[parts.length - 1];
                    
                    // Check if it contains common project folders
                    if (filePath.toLowerCase().includes('\\\\static\\\\')) {
                        displayPath = 'static/' + filename;
                    } else if (filePath.toLowerCase().includes('\\\\pybuch\\\\')) {
                        const idx = filePath.toLowerCase().indexOf('\\\\pybuch\\\\');
                        displayPath = filePath.substring(idx + 8).replace(/\\\\/g, '/');
                    } else {
                        displayPath = 'static/' + filename;
                    }
                }
                
                document.getElementById('logo_path').value = displayPath;
                
                // Show preview
                const reader = new FileReader();
                reader.onload = function(e) {
                    const preview = document.getElementById('logo_preview');
                    preview.innerHTML = '<img src="' + e.target.result + '" style="max-width: 150px; max-height: 80px; border: 1px solid #ccc;">';
                };
                reader.readAsDataURL(file);
            }
        }
    </script>
    '''
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
    
    # Extract contact data (ID=0, ContactType=1, CustomerNumber=2, Name=3, Company=4, Street=5, PostalCode=6, City=7, Country=8, Email=9, Phone=10, TaxID=11, Notes=12, Logo=13, BuyerRouteID=14)
    contact_type = contact[1] or 'customer'
    customer_number = contact[2] or ''
    name = contact[3]
    company = contact[4] or ''
    street = contact[5] or ''
    postal_code = contact[6] or ''
    city = contact[7] or ''
    country = contact[8] or 'DE'
    email = contact[9] or ''
    phone = contact[10] or ''
    tax_id = contact[11] or ''
    notes = contact[12] or ''
    logo = contact[13] or '' if len(contact) > 13 else ''
    buyer_route_id = contact[14] or '' if len(contact) > 14 else ''
    
    s+= "<h1>Kontakt bearbeiten</h1>"
    s+= f'''
        <form method="POST" action="/update_contact">
            <input type="hidden" name="contact_id" value="{contact_id}">
            <table>
                <tr><td>Typ:</td><td>
                    <select name="contact_type" id="contact_type_edit" onchange="toggleBuyerRouteIDEdit()">
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
                <tr><td>Land (ISO):</td><td>
                    <select name="country">
                        <option value="DE" {"selected" if country == "DE" else ""}>Deutschland (DE)</option>
                        <option value="AT" {"selected" if country == "AT" else ""}>Österreich (AT)</option>
                        <option value="CH" {"selected" if country == "CH" else ""}>Schweiz (CH)</option>
                        <option value="FR" {"selected" if country == "FR" else ""}>Frankreich (FR)</option>
                        <option value="NL" {"selected" if country == "NL" else ""}>Niederlande (NL)</option>
                        <option value="BE" {"selected" if country == "BE" else ""}>Belgien (BE)</option>
                        <option value="PL" {"selected" if country == "PL" else ""}>Polen (PL)</option>
                        <option value="IT" {"selected" if country == "IT" else ""}>Italien (IT)</option>
                        <option value="ES" {"selected" if country == "ES" else ""}>Spanien (ES)</option>
                        <option value="GB" {"selected" if country == "GB" else ""}>Großbritannien (GB)</option>
                        <option value="US" {"selected" if country == "US" else ""}>USA (US)</option>
                    </select>
                </td></tr>
                <tr><td>E-Mail:</td><td><input type="email" name="email" value="{email}"></td></tr>
                <tr><td>Telefon:</td><td><input type="tel" name="phone" value="{phone}"></td></tr>
                <tr><td>USt-IdNr / Steuer-Nr:</td><td><input type="text" name="tax_id" value="{tax_id}" placeholder="z.B. DE123456789"></td></tr>
                <tr id="buyer_route_id_row_edit" style="display: {"table-row" if contact_type == "customer" else "none"};">
                    <td>Leitweg-ID (B2G):</td>
                    <td>
                        <input type="text" name="buyer_route_id" value="{buyer_route_id}" placeholder="Optional, nur für öffentliche Auftraggeber" style="width: 300px;">
                        <small style="color: #666; display: block;">Format: 12-stellige Nummer-5stellige Prüfziffer (z.B. 991-ABCDE-12)</small>
                    </td>
                </tr>
                <tr><td>Logo:</td><td>
                    <input type="text" id="logo_path_edit" name="logo" value="{logo}" placeholder="/static/logo.png oder URL" style="width: 300px;">
                    <button type="button" onclick="document.getElementById('logo_file_picker_edit').click()">Datei wählen</button>
                    <input type="file" id="logo_file_picker_edit" accept="image/*" style="display: none;" onchange="updateLogoPathEdit(this)">
                    <div id="logo_preview_edit" style="margin-top: 5px;">
    '''
    
    if logo:
        s+= f'<img src="{logo}" style="max-width: 150px; max-height: 80px; border: 1px solid #ccc;" onerror="this.style.display=\'none\';">'
    
    s+= f'''                    </div>
                </td></tr>
                <tr><td>Notizen:</td><td><textarea name="notes" rows="3" cols="40">{notes}</textarea></td></tr>
                <tr><td></td><td><input type="submit" value="Kontakt aktualisieren"></td></tr>
            </table>
        </form>
        '''
    s+= '''        <script>
            function updateLogoPathEdit(input) {
                if (input.files && input.files[0]) {
                    const file = input.files[0];
                    const filePath = input.value;
                    
                    // Try to extract relative path if it's in project directory
                    let displayPath = filePath;
                    
                    // For Windows paths, extract filename and try common patterns
                    if (filePath.includes('\\\\')) {
                        const parts = filePath.split('\\\\');
                        const filename = parts[parts.length - 1];
                        
                        // Check if it contains common project folders
                        if (filePath.toLowerCase().includes('\\\\static\\\\')) {
                            displayPath = 'static/' + filename;
                        } else if (filePath.toLowerCase().includes('\\\\pybuch\\\\')) {
                            const idx = filePath.toLowerCase().indexOf('\\\\pybuch\\\\');
                            displayPath = filePath.substring(idx + 8).replace(/\\\\/g, '/');
                        } else {
                            displayPath = 'static/' + filename;
                        }
                    }
                    
                    document.getElementById('logo_path_edit').value = displayPath;
                    
                    // Show preview
                    const reader = new FileReader();
                    reader.onload = function(e) {
                        const preview = document.getElementById('logo_preview_edit');
                        preview.innerHTML = '<img src="' + e.target.result + '" style="max-width: 150px; max-height: 80px; border: 1px solid #ccc;">';
                    };
                    reader.readAsDataURL(file);
                }
            }
            
            function toggleBuyerRouteIDEdit() {
                const contactType = document.getElementById('contact_type_edit').value;
                const routeIdRow = document.getElementById('buyer_route_id_row_edit');
                routeIdRow.style.display = (contactType === 'customer') ? 'table-row' : 'none';
            }
            // Initialize on page load
            toggleBuyerRouteIDEdit();
        </script>
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
        s+= "<table>"
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
        s += "<table>"
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
            
            # Format expected amount
            expected_amount_str = f"{expected_amount:.2f}" if expected_amount else '-'
            
            s += f"<tr>"
            s += f"<td>{group_id}</td>"
            s += f"<td>{description}</td>"
            s += f"<td>{created_date}</td>"
            s += f"<td>{expected_amount_str}</td>"
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
        
        # Format expected amount
        expected_amount_str = f"{expected_amount:.2f}" if expected_amount else '-'
        
        s += f"<p><strong>Erwarteter Gesamtbetrag:</strong> {expected_amount_str} EUR</p>"
        s += f"<p><strong>Tatsächlicher Gesamtbetrag:</strong> {actual_amount:.2f} EUR</p>"
        
        if expected_amount and abs(expected_amount - actual_amount) > 0.01:
            diff = actual_amount - expected_amount
            s += f"<p style='color:red'><strong>Differenz:</strong> {diff:.2f} EUR</p>"
        
        s += "<h2>Buchungen in dieser Gruppe</h2>"
        s += "<table>"
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


def PageArticles(db: Database):
    """Generate articles management page"""
    articles = db.fetch_articles()
    s = Header1('articles')
    s+= Header2()
    s+= Header3()
    
    s+= '''
        <h2>Neuen Artikel anlegen</h2>
        <form method="POST" action="/articles/add">
            <table>
                <tr><td>Bezeichnung:</td><td><input type="text" name="name" required size="50"></td></tr>
                <tr><td>Einheit:</td><td>
                    <select name="unit">
                        <option value="Stk.">Stk. (Stück)</option>
                        <option value="Std.">Std. (Stunde)</option>
                        <option value="kg">kg (Kilogramm)</option>
                        <option value="g">g (Gramm)</option>
                        <option value="m">m (Meter)</option>
                        <option value="m²">m² (Quadratmeter)</option>
                        <option value="l">l (Liter)</option>
                        <option value="Psch.">Psch. (Pauschale)</option>
                    </select>
                </td></tr>
                <tr><td>Einzelpreis (netto):</td><td><input type="number" step="0.01" name="unit_price" value="0.00"> €</td></tr>
                <tr><td>MwSt (%):</td><td>
                    <select name="tax_rate">
                        <option value="19" selected>19%</option>
                        <option value="7">7%</option>
                        <option value="0">0%</option>
                    </select>
                </td></tr>
                <tr><td>Beschreibung:</td><td><textarea name="description" rows="2" cols="50"></textarea></td></tr>
                <tr><td>Aktiv:</td><td><input type="checkbox" name="active" value="1" checked> (nur aktive Artikel werden in Rechnungen angezeigt)</td></tr>
                <tr><td></td><td><input type="submit" value="Artikel hinzufügen"></td></tr>
            </table>
        </form>
    '''
    
    s+= "<h2>Artikelverzeichnis</h2>"
    s+= "<table>"
    s+= "<tr><th>ID</th><th>Bezeichnung</th><th>Einheit</th><th>Einzelpreis (netto)</th><th>MwSt</th><th>Beschreibung</th><th>Aktiv</th><th>Aktionen</th></tr>"
    
    for article in articles:
        article_id = article[0]
        name = article[1] or ''
        unit = article[2] or 'Stk.'
        unit_price = article[3] or 0
        tax_rate = article[4] or 19
        description = article[5] or ''
        active = article[6] if len(article) > 6 else 1
        active_display = "✓" if active else "✗"
        active_style = "color: green;" if active else "color: red;"
        
        s+= f"<tr>"
        s+= f"<td>{article_id}</td>"
        s+= f"<td>{name}</td>"
        s+= f"<td>{unit}</td>"
        s+= f"<td style='text-align: right;'>{unit_price:.2f} €</td>"
        s+= f"<td>{tax_rate:.0f}%</td>"
        s+= f"<td>{description[:50]}</td>"
        s+= f"<td style='text-align: center; {active_style}'>{active_display}</td>"
        s+= f"<td><a href='/articles/edit?id={article_id}'>Bearbeiten</a> | <a href='/articles/delete?id={article_id}' onclick='return confirm(\"Artikel wirklich löschen?\")'>Löschen</a></td>"
        s+= f"</tr>"
    
    s+= "</table>"
    s+= Footer()
    return s


def PageArticleEdit(db: Database, article_id):
    """Generate article edit page"""
    article = db.get_article_by_id(article_id)
    if not article:
        return "Artikel nicht gefunden."
    
    s = Header1('articles')
    submenu = '<a href="/articles">Artikel</a> -> <span id="ActivePage">Bearbeiten</span>'
    s+= Header2(submenu)
    s+= Header3()
    
    # Extract article data (ID=0, Name=1, Unit=2, UnitPrice=3, TaxRate=4, Description=5, Active=6)
    name = article[1] or ''
    unit = article[2] or 'Stk.'
    unit_price = article[3] or 0
    tax_rate = article[4] or 19
    description = article[5] or ''
    active = article[6] if len(article) > 6 else 1
    
    # Unit options with selection
    unit_options = ['Stk.', 'Std.', 'kg', 'g', 'm', 'm²', 'l', 'Psch.']
    unit_select = ""
    for u in unit_options:
        selected = 'selected' if u == unit else ''
        unit_select += f'<option value="{u}" {selected}>{u}</option>'
    
    # Tax rate options with selection
    tax_options = [(19, '19%'), (7, '7%'), (0, '0%')]
    tax_select = ""
    for rate, label in tax_options:
        selected = 'selected' if rate == tax_rate else ''
        tax_select += f'<option value="{rate}" {selected}>{label}</option>'
    
    active_checked = 'checked' if active else ''
    
    s+= f'''
        <h2>Artikel bearbeiten</h2>
        <form method="POST" action="/articles/update">
            <input type="hidden" name="id" value="{article_id}">
            <table>
                <tr><td>ID:</td><td>{article_id}</td></tr>
                <tr><td>Bezeichnung:</td><td><input type="text" name="name" value="{name}" required size="50"></td></tr>
                <tr><td>Einheit:</td><td><select name="unit">{unit_select}</select></td></tr>
                <tr><td>Einzelpreis (netto):</td><td><input type="number" step="0.01" name="unit_price" value="{unit_price:.2f}"> €</td></tr>
                <tr><td>MwSt (%):</td><td><select name="tax_rate">{tax_select}</select></td></tr>
                <tr><td>Beschreibung:</td><td><textarea name="description" rows="2" cols="50">{description}</textarea></td></tr>
                <tr><td>Aktiv:</td><td><input type="checkbox" name="active" value="1" {active_checked}> (nur aktive Artikel werden in Rechnungen angezeigt)</td></tr>
                <tr><td></td><td><input type="submit" value="Artikel aktualisieren"></td></tr>
            </table>
        </form>
        <p><a href="/articles">Zurück zum Artikelverzeichnis</a></p>
    '''
    s+= Footer()
    return s


def PageReminders(db: Database):
    """Generate reminders/dunning page for overdue invoices"""
    from datetime import date, timedelta
    
    overdue = db.get_overdue_invoices()
    due_soon = db.get_invoices_due_soon(days=7)
    
    s = Header1('invoice')
    submenu = '<a href="/invoice">Rechnungen</a> | <span id="ActivePage">Mahnwesen</span>'
    s += Header2(submenu)
    s += Header3()
    
    s += "<h2>Mahnwesen & Fälligkeiten</h2>"
    
    # Statistics
    total_overdue_amount = sum(inv[36] or inv[35] for inv in overdue if inv[35])
    overdue_count = len(overdue)
    
    s += f'''
    <div style="margin-bottom: 20px; padding: 15px; background-color: #fff3cd; border-left: 4px solid #ffc107; border-radius: 5px;">
        <h3 style="margin-top: 0; color: #856404;">⚠️ Überfällige Rechnungen</h3>
        <p style="margin: 0;"><strong>{overdue_count} Rechnung(en)</strong> überfällig | 
        Offener Betrag: <strong style="color: #dc3545;">{total_overdue_amount:.2f} €</strong></p>
    </div>
    '''
    
    if overdue:
        s += "<h3>Überfällige Rechnungen (Mahnung erforderlich)</h3>"
        s += "<table style='width: 100%;'>"
        s += "<tr><th>RE-Nr.</th><th>Datum</th><th>Fällig seit</th><th>Kunde</th><th>Betrag</th><th>Überfällig (Tage)</th><th>Aktionen</th></tr>"
        
        today = date.today()
        for inv in overdue:
            inv_id = inv[0]
            inv_number = inv[1]
            inv_date = inv[2]
            due_date = inv[20]  # PaymentDueDate
            buyer_name = inv[12]
            amount = inv[36] or inv[35]
            
            # Calculate days overdue
            if due_date:
                due = date.fromisoformat(due_date)
                days_overdue = (today - due).days
            else:
                days_overdue = 0
            
            # Color code by severity
            if days_overdue > 60:
                row_style = "background-color: #f8d7da;"  # Red - 3rd reminder
                severity = "🔴 Mahnstufe 3"
            elif days_overdue > 30:
                row_style = "background-color: #fff3cd;"  # Yellow - 2nd reminder
                severity = "🟡 Mahnstufe 2"
            else:
                row_style = "background-color: #d1ecf1;"  # Blue - 1st reminder
                severity = "🔵 Mahnstufe 1"
            
            s += f"<tr style='{row_style}'>"
            s += f"<td>{inv_number}</td>"
            s += f"<td>{inv_date}</td>"
            s += f"<td>{due_date}</td>"
            s += f"<td>{buyer_name[:30]}</td>"
            s += f"<td style='text-align: right;'><strong>{amount:.2f} €</strong></td>"
            s += f"<td>{days_overdue} Tage<br><small>{severity}</small></td>"
            s += f"<td>"
            s += f"<a href='/invoice/view?id={inv_id}'>Ansicht</a> | "
            s += f"<a href='/invoice/reminder?id={inv_id}' style='color: #dc3545;'>Mahnung erstellen</a>"
            s += f"</td>"
            s += f"</tr>"
        
        s += "</table>"
    else:
        s += "<p style='color: #28a745;'>✓ Keine überfälligen Rechnungen vorhanden.</p>"
    
    # Invoices due soon
    s += "<h3 style='margin-top: 30px;'>Rechnungen fällig in den nächsten 7 Tagen</h3>"
    
    if due_soon:
        s += "<table style='width: 100%;'>"
        s += "<tr><th>RE-Nr.</th><th>Datum</th><th>Fälligkeitsdatum</th><th>Kunde</th><th>Betrag</th><th>Verbleibende Tage</th><th>Aktionen</th></tr>"
        
        for inv in due_soon:
            inv_id = inv[0]
            inv_number = inv[1]
            inv_date = inv[2]
            due_date = inv[20]
            buyer_name = inv[12]
            amount = inv[36] or inv[35]
            
            if due_date:
                due = date.fromisoformat(due_date)
                days_remaining = (due - today).days
            else:
                days_remaining = 0
            
            s += f"<tr>"
            s += f"<td>{inv_number}</td>"
            s += f"<td>{inv_date}</td>"
            s += f"<td>{due_date}</td>"
            s += f"<td>{buyer_name[:30]}</td>"
            s += f"<td style='text-align: right;'>{amount:.2f} €</td>"
            s += f"<td>{days_remaining} Tage</td>"
            s += f"<td><a href='/invoice/view?id={inv_id}'>Ansicht</a></td>"
            s += f"</tr>"
        
        s += "</table>"
    else:
        s += "<p><em>Keine Rechnungen in den nächsten 7 Tagen fällig.</em></p>"
    
    s += Footer()
    return s


def PageDashboard(db: Database):
    """Generate dashboard with statistics and charts"""
    from datetime import date, timedelta, datetime
    
    # Get all invoices
    all_invoices = db.fetch_invoices()
    
    # Current year statistics
    current_year = datetime.now().year
    current_year_invoices = [inv for inv in all_invoices if inv[2] and inv[2].startswith(str(current_year))]
    
    # Status counts
    draft_count = len([inv for inv in all_invoices if inv[37] == 'draft'])
    finalized_count = len([inv for inv in all_invoices if inv[37] == 'finalized'])
    sent_count = len([inv for inv in all_invoices if inv[37] == 'sent'])
    paid_count = len([inv for inv in all_invoices if inv[37] == 'paid'])
    cancelled_count = len([inv for inv in all_invoices if inv[37] == 'cancelled'])
    
    # Financial statistics
    total_revenue = sum(inv[35] for inv in all_invoices if inv[35] and inv[37] in ['finalized', 'sent', 'paid'])
    paid_revenue = sum(inv[35] for inv in all_invoices if inv[35] and inv[37] == 'paid')
    open_amount = sum(inv[36] or inv[35] for inv in all_invoices if inv[35] and inv[37] in ['finalized', 'sent'])
    
    # Current year revenue
    year_revenue = sum(inv[35] for inv in current_year_invoices if inv[35] and inv[37] in ['finalized', 'sent', 'paid'])
    year_paid = sum(inv[35] for inv in current_year_invoices if inv[35] and inv[37] == 'paid')
    
    # Overdue
    overdue = db.get_overdue_invoices()
    overdue_amount = sum(inv[36] or inv[35] for inv in overdue if inv[35])
    
    # Monthly revenue for current year
    monthly_revenue = {}
    for month in range(1, 13):
        month_str = f"{current_year}-{month:02d}"
        month_invoices = [inv for inv in current_year_invoices 
                         if inv[2] and inv[2].startswith(month_str) 
                         and inv[37] in ['finalized', 'sent', 'paid']]
        monthly_revenue[month] = sum(inv[35] for inv in month_invoices if inv[35])
    
    s = Header1('dashboard')
    s += Header2()
    s += Header3()
    
    # Key metrics cards
    s += f'''
    <div class="grid-container">
        <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; opacity: 0.9;">Gesamtumsatz ({current_year})</div>
            <div style="font-size: 32px; font-weight: bold; margin: 10px 0;">{year_revenue:.2f} €</div>
            <div style="font-size: 12px; opacity: 0.8;">Davon bezahlt: {year_paid:.2f} €</div>
        </div>
        
        <div style="background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; opacity: 0.9;">Offene Forderungen</div>
            <div style="font-size: 32px; font-weight: bold; margin: 10px 0;">{open_amount:.2f} €</div>
            <div style="font-size: 12px; opacity: 0.8;">{sent_count + finalized_count} unbezahlte Rechnung(en)</div>
        </div>
        
        <div style="background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); color: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; opacity: 0.9;">Überfällige Rechnungen</div>
            <div style="font-size: 32px; font-weight: bold; margin: 10px 0;">{overdue_amount:.2f} €</div>
            <div style="font-size: 12px; opacity: 0.8;">{len(overdue)} Rechnung(en) überfällig</div>
        </div>
        
        <div style="background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); color: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; opacity: 0.9;">Gesamt Rechnungen</div>
            <div style="font-size: 32px; font-weight: bold; margin: 10px 0;">{len(all_invoices)}</div>
            <div style="font-size: 12px; opacity: 0.8;">{len(current_year_invoices)} in {current_year}</div>
        </div>
    </div>
    '''
    
    # Status distribution & Recent invoices
    s += '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 30px;">'
    
    # Left: Status pie chart (simple bar representation)
    s += '''
    <div class="rectRounded">
        <h3 style="margin-top: 0;">Status-Verteilung</h3>
        <div style="margin: 20px 0;">
    '''
    
    status_data = [
        ('Entwurf', draft_count, '#888'),
        ('Finalisiert', finalized_count, '#0066cc'),
        ('Versendet', sent_count, '#ff9900'),
        ('Bezahlt', paid_count, '#00aa00'),
        ('Storniert', cancelled_count, '#cc0000')
    ]
    
    total_status = sum(count for _, count, _ in status_data)
    for label, count, color in status_data:
        if total_status > 0:
            percentage = (count / total_status) * 100
        else:
            percentage = 0
        s += f'''
        <div style="margin-bottom: 15px;">
            <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                <span>{label}</span>
                <span><strong>{count}</strong> ({percentage:.1f}%)</span>
            </div>
            <div style="background: #f0f0f0; height: 20px; border-radius: 10px; overflow: hidden;">
                <div style="background: {color}; height: 100%; width: {percentage}%;"></div>
            </div>
        </div>
        '''
    
    s += '</div></div>'
    
    # Right: Recent invoices
    recent_invoices = sorted(all_invoices, key=lambda x: x[39] or '', reverse=True)[:5]  # CreatedAt
    s += '''
    <div class="rectRounded">
        <h3 style="margin-top: 0;">Letzte Rechnungen</h3>
        <table style="width: 100%; border-collapse: collapse;">
    '''
    
    for inv in recent_invoices:
        status_colors = {'draft': '#888', 'finalized': '#0066cc', 'sent': '#ff9900', 'paid': '#00aa00', 'cancelled': '#cc0000'}
        status_color = status_colors.get(inv[40], '#888')  # Status is now at index 40 (was 38)
        s += f'''
        <tr style="border-bottom: 1px solid #eee;">
            <td style="padding: 10px 0;">{inv[1]}</td>
            <td>{inv[14][:20] if inv[14] else ''}</td>
            <td style="text-align: right;">{inv[38]:.2f} €</td>
            <td><span style="color: {status_color};">●</span></td>
            <td><a href="/invoice/view?id={inv[0]}">Ansicht</a></td>
        </tr>
        '''
    
    s += '</table></div></div>'
    
    # Monthly revenue chart
    s += f'''
    <div class="rectRounded">
        <h3 style="margin-top: 0;">Monatlicher Umsatz {current_year}</h3>
        <div style="height: 250px; position: relative; margin-top: 20px;">
    '''
    
    # Simple bar chart
    max_revenue = max(monthly_revenue.values()) if monthly_revenue.values() else 1
    month_names = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']
    
    s += '<div style="display: flex; align-items: flex-end; justify-content: space-around; height: 200px; border-left: 2px solid #ddd; border-bottom: 2px solid #ddd; padding: 10px;">'
    
    for month in range(1, 13):
        revenue = monthly_revenue[month]
        height = (revenue / max_revenue * 180) if max_revenue > 0 else 0
        s += f'''
        <div style="display: flex; flex-direction: column; align-items: center; flex: 1;">
            <div style="font-size: 11px; margin-bottom: 5px;">{revenue:.0f}€</div>
            <div style="background: linear-gradient(180deg, #4facfe 0%, #00f2fe 100%); width: 80%; height: {height}px; border-radius: 5px 5px 0 0;"></div>
            <div style="font-size: 12px; margin-top: 5px;">{month_names[month-1]}</div>
        </div>
        '''
    
    s += '</div></div></div>'
    
    # Quick actions
    s += '''
    <div class="rectRounded">
        <h3 style="margin-top: 0;">Schnellzugriff</h3>
        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
            <a href="/invoice/new" class="coloredButton btn-green">+ Neue Rechnung</a>
            <a href="/invoice?status=sent" class="coloredButton btn-orange">Versendete Rechnungen</a>
            <a href="/invoice/reminders" class="coloredButton btn-red">⚠️ Mahnwesen</a>
            <a href="/invoice" class="coloredButton btn-blue">Alle Rechnungen</a>
        </div>
    </div>
    '''
    
    s += Footer()
    return s
