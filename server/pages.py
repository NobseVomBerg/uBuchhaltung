"""
HTML page generation functions
All functions return complete HTML strings
"""
from db import Database

# Shared invoice status constants – single source of truth used across pages
INVOICE_STATUS_COLORS: dict = {
    'draft':     '#888',
    'finalized': '#0066cc',
    'sent':      '#ff9900',
    'paid':      '#00aa00',
    'cancelled': '#cc0000',
}
INVOICE_STATUS_LABELS: dict = {
    'draft':     'Entwurf',
    'finalized': 'Abgeschlossen',
    'sent':      'Versendet',
    'paid':      'Bezahlt',
    'cancelled': 'Storniert',
}

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
    
    # Buchungen
    if active_page == 'transactions':
        nav_items.append('<span id="ActivePage">Buchungen</span>')
    else:
        nav_items.append('<a href="/transactions">Buchungen</a>')
    
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
    """Alias kept for backward compatibility â€“ delegates to PageMiscellaneous."""
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
            amount = booking[11]
            relation_type = booking[-1]  # RelationType from JOIN
            
            amount_color = "green" if (amount or 0) > 0 else "red"
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
    """Delegiert an pages_transactions.PageTransactions â€“ ausgelagert."""
    from .pages_transactions import PageTransactions as _PT
    return _PT(db, edit_transaction_id)


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
    """Rechnungsliste – ausgelagert nach pages_invoice."""
    from .pages_invoice import PageInvoice as _PI
    return _PI(db, filters)


def PageInvoiceNew(db: Database, invoice_id=None):
    """Rechnung erstellen/bearbeiten – ausgelagert nach pages_invoice."""
    from .pages_invoice import PageInvoiceNew as _PIN
    return _PIN(db, invoice_id)


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
                    <input type="text" id="logo_path" name="logo" placeholder="seed_data/private/logo.png oder URL" style="width: 300px;">
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
                    if (filePath.toLowerCase().includes('\\\\private\\\\')) {
                        displayPath = 'seed_data/private/' + filename;
                    } else if (filePath.toLowerCase().includes('\\\\pybuch\\\\')) {
                        const idx = filePath.toLowerCase().indexOf('\\\\pybuch\\\\');
                        displayPath = filePath.substring(idx + 8).replace(/\\\\/g, '/');
                    } else {
                        displayPath = 'seed_data/private/' + filename;
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
                    <input type="text" id="logo_path_edit" name="logo" value="{logo}" placeholder="seed_data/private/logo.png oder URL" style="width: 300px;">
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
                        if (filePath.toLowerCase().includes('\\\\private\\\\')) {
                            displayPath = 'seed_data/private/' + filename;
                        } else if (filePath.toLowerCase().includes('\\\\pybuch\\\\')) {
                            const idx = filePath.toLowerCase().indexOf('\\\\pybuch\\\\');
                            displayPath = filePath.substring(idx + 8).replace(/\\\\/g, '/');
                        } else {
                            displayPath = 'seed_data/private/' + filename;
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
    from .pages_transactions import PageConfirmTransactions as _PCT
    return _PCT(import_id)

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
    from .pages_dashboard import PageDashboard as _PD
    return _PD(db)
