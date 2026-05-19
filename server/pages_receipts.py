"""
Receipts page (Belege: Upload, Anzeige und Bearbeitung)
"""
import datetime
import html as _html
from db import Database
from .pages import Header1, Header2, Header3, Footer


def PageReceipts(db: Database):
    """Generate receipts page with upload functionality"""
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
        <div class="rowWithObjects">
            <div>
                Von: <input type="date" id="dateFrom" onchange="filterReceipts()">
                Bis: <input type="date" id="dateTo" onchange="filterReceipts()"> &nbsp;
                <button onclick="setReceiptYear({current_year})">{current_year}</button>
                <button onclick="setReceiptYear({current_year-1})">{current_year-1}</button>
                <button onclick="setReceiptYear({current_year-2})">{current_year-2}</button>
                <button onclick="setReceiptYear({current_year-3})">{current_year-3}</button>
            </div>
            <div>
                <label>🔍 Suche:</label>
                <input type="text" id="receiptSearch" oninput="filterReceipts()" placeholder="Dateiname / Pfad / Info" style="width: 200px;">
            </div>
        </div>
    '''
    s+= Header3(header3_content)
    
    # ── Zwei-Spalten-Layout: links scrollbare Belege, rechts Upload + Formular ──
    s += '''
    <div class="grid2Rows">

        <!-- LINKS: scrollbare Belegliste -->
        <div class="gridLeftCol">
            <table>
                <tr><th>Nr.</th><th>Datum</th><th>Dateiname</th><th>Pfad</th><th>Info</th><th>Aktionen</th></tr>
        '''
    for row in rows:
        n   = _html.escape(str(row[1] or ''))
        fn  = _html.escape(str(row[3] or ''))
        pt  = _html.escape(str(row[4] or ''))
        inf = _html.escape(str(row[5] or ''))
        s += f'<tr class="receipt-row" data-date="{row[2]}" data-id="{row[0]}" data-number="{n}" data-filename="{fn}" data-path="{pt}" data-info="{inf}">'
        s += f'<td>{n}</td><td>{row[2]}</td><td>{fn}</td><td>{pt}</td><td>{inf}</td>'
        s += (f'<td>'
              f'<a href="#" class="action-icon" onclick="editReceiptFromRow(this); return false;" title="Bearbeiten">&#9998;</a>'
              f' <a href="javascript:void(0);" class="action-icon delete-icon" title="L\u00f6schen"'
              f' onclick="appConfirmHref(\'/receipts/delete?number={n}\', \'Beleg wirklich löschen?\')">&#128465;</a>'
              f'</td></tr>')
    s += '''
            </table>
        </div><!-- Ende linke Spalte -->

        <!-- RECHTS: Drag&Drop oben, Formular unten -->
        <div class="gridRightCol">

            <!-- Drag & Drop -->
            <div class="rectRounded">
                <h2>Belege hochladen</h2>
                <div id="dropZone">
                    <p>Dateien hier ablegen (Drag &amp; Drop)</p>
                    <input type="file" id="fileInput" multiple accept=".pdf,application/pdf">
                    <button onclick="document.getElementById('fileInput').click()">Oder Dateien ausw&auml;hlen</button>
                </div>
                <div id="uploadStatus"></div>
            </div>

            <!-- Neuer Beleg / Beleg bearbeiten -->
            <div class="rectRounded">
                <h2 id="receiptFormTitle">Neuer Beleg</h2>
                <form method="POST" action="/add_receipt" id="receiptForm">
                    <input type="hidden" name="id" id="editId" value="">
                    <table>
    '''
    s += f'<tr><td>Nummer:</td><td><input type="text" name="number" id="editNumber" value="{next_receipt_number}"></td></tr>'
    s += '''
                        <tr><td>Datum:</td><td><input type="date" name="date" id="editDate"></td></tr>
                        <tr><td>Dateiname:</td><td><input type="text" name="filename" id="editFilename" style="width:220px"></td></tr>
                        <tr><td>Pfad:</td><td><input type="text" name="path" id="editPath" style="width:220px"></td></tr>
                        <tr><td>Info:</td><td><input type="text" name="info" id="editInfo" style="width:220px"></td></tr>
                        <tr><td></td><td>
                            <div id="btnNew">
                                <input type="submit" value="Beleg hinzuf&uuml;gen" class="coloredButton btn-sm btn-green">
                            </div>
                            <div id="btnEdit" style="display:none">
                                <input type="submit" value="Aktualisieren" class="coloredButton btn-sm btn-green">
                                <a href="#" class="coloredButton btn-sm btn-gray" onclick="resetReceiptForm(); return false;">Abbrechen</a>
                            </div>
                        </td></tr>
                    </table>
                </form>
            </div>

        </div><!-- Ende rechte Spalte -->
    </div><!-- Ende Zwei-Spalten-Layout -->
    '''
    s += '''<script>
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
    
    s += '''
    <script>
        function editReceiptFromRow(el) {
            const row = el.closest('tr');
            document.getElementById('editId').value       = row.dataset.id;
            document.getElementById('editNumber').value   = row.dataset.number;
            document.getElementById('editDate').value     = row.dataset.date;
            document.getElementById('editFilename').value = row.dataset.filename;
            document.getElementById('editPath').value     = row.dataset.path;
            document.getElementById('editInfo').value     = row.dataset.info;
            document.getElementById('receiptForm').action           = '/update_receipt';
            document.getElementById('receiptFormTitle').textContent = 'Beleg bearbeiten';
            document.getElementById('btnNew').style.display  = 'none';
            document.getElementById('btnEdit').style.display = '';
            document.getElementById('receiptFormTitle').scrollIntoView({ behavior: 'smooth' });
        }

        function resetReceiptForm() {
            location.reload();
        }

        function setReceiptYear(year) {
            document.getElementById('dateFrom').value = year + '-01-01';
            document.getElementById('dateTo').value = year + '-12-31';
            filterReceipts();
        }

        function filterReceipts() {
            const dateFrom = document.getElementById('dateFrom').value;
            const dateTo   = document.getElementById('dateTo').value;
            const search   = document.getElementById('receiptSearch').value.toLowerCase();

            document.querySelectorAll('.receipt-row').forEach(row => {
                const rowDate = row.getAttribute('data-date') || '';
                const rowText = (
                    (row.getAttribute('data-filename') || '') + ' ' +
                    (row.getAttribute('data-path') || '') + ' ' +
                    (row.getAttribute('data-info') || '')
                ).toLowerCase();

                let show = true;
                if (dateFrom && rowDate < dateFrom) show = false;
                if (dateTo   && rowDate > dateTo)   show = false;
                if (search   && !rowText.includes(search)) show = false;

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
