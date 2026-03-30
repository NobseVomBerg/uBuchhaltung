"""
Transactions page (Kontobewegungen / Zahlungen)
"""
import datetime
from db import Database
from .pages import Header1, Header2, Header3, Footer

# Spaltenindizes in Bookings (SELECT *):
# [0]  ID            [1]  DateBooking   [2]  DateTax       [3]  BookingGroup_ID
# [4]  Account_ID    [5]  ForeignBankAccount               [6]  RecipientClient
# [7]  Contact_ID    [8]  COA_ID        [9]  CounterCOA_ID [10] Category_ID
# [11] Amount        [12] Currency      [13] TaxRate        [14] TaxAmount
# [15] Text          [16] DocumentNumber


def PageConfirmTransactions(import_id: str):
    """Display parsed transactions for confirmation before import"""
    import json
    import glob

    temp_file = f"./data/pending_imports/{import_id}_*.json"
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
                <label>Min. Betrag:</label> <input type="number" step="0.01" class="noButtons" id="minAmount" onchange="filterTransactions()" style="width: 80px;">
                <label>Max. Betrag:</label> <input type="number" step="0.01" class="noButtons" id="maxAmount" onchange="filterTransactions()" style="width: 80px;">
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
            edit_recipient = edit_trans[6] or ""  # RecipientClient
            edit_text = edit_trans[15] or ""      # Text

    # Get dropdown data
    customers = db.fetch_contacts(contact_type='customer')
    coa_accounts = db.fetch_chart_of_accounts()
    booking_groups = db.fetch_booking_groups()

    # Determine form title and button text
    form_title  = "Transaktion bearbeiten" if edit_trans else "Neue Transaktion"
    submit_text = "Transaktion aktualisieren" if edit_trans else "Transaktion hinzufügen"
    transaction_id = edit_trans[0] if edit_trans else 0

    id_display = (f'<tr><td>ID:</td><td style="color: #666;">{transaction_id}'
                  f'<input type="hidden" name="transaction_id" value="{transaction_id}"></td></tr>'
                  if edit_trans else '')

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
                    <tr><td>Verwendungszweck:</td><td><textarea name="text" rows="3" cols="42">{edit_text}</textarea></td></tr>
                    <tr><td>Bankkonto:</td><td><select name="account">
                        <option value="">-- Kein Konto --</option>
    '''
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

                    <tr><td>Betrag:</td><td><input type="number" step="0.01" class="noButtons" name="amount" id="amount" value="{edit_trans[11] if edit_trans else ""}" required></td></tr>
                    <tr><td>Währung:</td><td><input type="text" name="currency" value="{edit_trans[12] if edit_trans else "EUR"}" size="5"></td></tr>

                    <tr><td>Steuersatz (%):</td><td><input type="number" step="0.01" class="noButtons" name="tax_rate" id="tax_rate" value="{edit_trans[13]*100 if edit_trans and edit_trans[13] else ""}" placeholder="z.B. 19 für 19%"></td></tr>
                    <tr><td>Steuerbetrag:</td><td><input type="number" step="0.01" class="noButtons" name="tax_amount" id="tax_amount" value="{edit_trans[14] if edit_trans and edit_trans[14] else ""}"></td></tr>

                    <tr><td>Beleg-Nr.:</td><td><input type="text" name="document_nr" value="{edit_trans[16] if edit_trans and edit_trans[16] else ""}"></td></tr>
    '''

    neu_button = ('<a href="/transactions" style="margin-left: 10px; padding: 5px 10px; '
                  'background-color: #888; color: white; text-decoration: none; display: inline-block;">'
                  'Neu</a>') if edit_trans else ''

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
                doc_id        = doc[0]
                doc_number    = doc[1]
                doc_date      = doc[2]
                doc_filename  = doc[3]
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
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            document.body.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
            }, false);
        });

        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const uploadStatus = document.getElementById('uploadStatus');

        dropZone.addEventListener('dragenter', (e) => { e.preventDefault(); e.stopPropagation(); dropZone.classList.add('hover'); });
        dropZone.addEventListener('dragover',  (e) => { e.preventDefault(); e.stopPropagation(); });
        dropZone.addEventListener('dragleave', (e) => { e.preventDefault(); e.stopPropagation(); dropZone.classList.remove('hover'); });
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('hover');
            uploadFiles(e.dataTransfer.files);
        });
        fileInput.addEventListener('change', (e) => { uploadFiles(e.target.files); });

        function uploadFiles(files) {
            if (files.length === 0) return;
            uploadStatus.innerHTML = '<p>Uploading...</p>';
            const formData = new FormData();
            for (let i = 0; i < files.length; i++) { formData.append('files', files[i]); }
            fetch('/upload_receipts', { method: 'POST', body: formData })
                .then(response => response.text())
                .then(data => {
                    if (data.includes('confirm_transactions')) {
                        document.open(); document.write(data); document.close();
                    } else {
                        uploadStatus.innerHTML = '<p class="successColor">' + data + '</p>';
                        setTimeout(() => { uploadStatus.innerHTML = ''; location.reload(); }, 3000);
                    }
                })
                .catch(error => {
                    uploadStatus.innerHTML = '<p class="errorColor">Fehler beim Hochladen: ' + error + '</p>';
                });
        }
    </script>
    '''

    # ── Buchungstabelle ───────────────────────────────────────────────────
    s+= "<h2>Kontobewegungen</h2>"
    s+= "<table id='transactionsTable'>"
    s+= ("<tr><th>Datum</th><th>Empfänger/Auftragg.</th><th>Text</th>"
         "<th>Betrag</th><th>Währung</th><th>Konto</th><th>Kunde</th>"
         "<th>SKR</th><th>Beleg-Nr.</th><th>Aktionen</th></tr>")

    bookings = db.fetch_bookings_grouped()

    account_map  = {a[0]: a[1] for a in accounts}
    customers    = db.fetch_contacts(contact_type='customer')
    customer_map = {c[0]: c[2] or c[3] for c in customers}
    coa_accounts = db.fetch_chart_of_accounts()
    coa_map      = {c[0]: str(c[2]) for c in coa_accounts}

    for item in bookings:
        row_type = item['type']

        if row_type == 'group':
            # ── Split-Gruppen-Kopfzeile ───────────────────────────────────
            gid          = item['group_id']
            date_booking = item['date']
            amount       = item['amount'] or 0
            currency     = item['currency']
            description  = item['description'] or ''
            count        = item['count']
            account_id   = item['account_id']
            contact_id   = item['contact_id']
            account_name = account_map.get(account_id, '') if account_id else ''
            contact_name = customer_map.get(contact_id, '') if contact_id else ''
            amount_color = 'green' if amount > 0 else 'red'
            s+= (f"<tr class='transaction-row group-row' "
                 f"data-group-id='{gid}' "
                 f"data-account-id='{account_id or ''}' "
                 f"data-date='{date_booking}' "
                 f"data-contact-id='{contact_id or ''}' "
                 f"data-currency='{currency}' "
                 f"data-amount='{amount}' "
                 f"onclick='toggleGroup({gid})' "
                 f"title='Split-Buchung aufklappen/zuklappen'>")
            s+= f"<td>{date_booking}</td>"
            s+= f"<td></td>"
            s+= f"<td></td>"
            s+= f"<td style='color:{amount_color}'>{amount:.2f}</td>"
            s+= f"<td>{currency}</td>"
            s+= f"<td>{account_name[:20]}</td>"
            s+= f"<td>{contact_name[:20]}</td>"
            s+= f"<td><span class='split-badge'>Split {count}×</span></td>"
            s+= f"<td>{description[:35]}</td>"
            s+= f"<td><span class='split-toggle-icon' id='toggle-icon-{gid}'>▶</span></td>"
            s+= f"</tr>"

        elif row_type == 'child':
            # ── Teilbuchung einer Split-Gruppe ────────────────────────────
            gid     = item['group_id']
            booking = item['booking']
            booking_id   = booking[0]
            date_booking = booking[1]
            account_id   = booking[4]
            recipient    = booking[6] or ''
            contact_id   = booking[7]
            coa_id       = booking[8]
            amount       = booking[11]
            currency     = booking[12] or 'EUR'
            text         = booking[15] or ''
            doc_number   = booking[16] or '' if len(booking) > 16 else ''
            account_name = account_map.get(account_id, '') if account_id else ''
            contact_name = customer_map.get(contact_id, '') if contact_id else ''
            coa_number   = coa_map.get(coa_id, '') if coa_id else ''
            amount_color = 'green' if (amount or 0) > 0 else 'red'
            s+= (f"<tr class='child-row' data-parent-group='{gid}' style='display:none'>")
            s+= f"<td class='child-indent'>{date_booking}</td>"
            s+= f"<td>{recipient[:25]}</td>"
            s+= f"<td>{text[:35]}</td>"
            s+= f"<td style='color:{amount_color}'>{(amount or 0):.2f}</td>"
            s+= f"<td>{currency}</td>"
            s+= f"<td>{account_name[:20]}</td>"
            s+= f"<td>{contact_name[:20]}</td>"
            s+= f"<td>{coa_number}</td>"
            s+= f"<td>{doc_number}</td>"
            s+= f"<td><a href='/transactions/edit?id={booking_id}'>Bearbeiten</a></td>"
            s+= f"</tr>"

        else:
            # ── Normale Einzelbuchung ─────────────────────────────────────
            booking      = item['booking']
            booking_id   = booking[0]
            date_booking = booking[1]
            account_id   = booking[4]
            recipient    = booking[6] or ''
            contact_id   = booking[7]
            coa_id       = booking[8]
            amount       = booking[11]
            currency     = booking[12] or 'EUR'
            text         = booking[15] or ''
            doc_number   = booking[16] or '' if len(booking) > 16 else ''
            account_name = account_map.get(account_id, '') if account_id else ''
            contact_name = customer_map.get(contact_id, '') if contact_id else ''
            coa_number   = coa_map.get(coa_id, '') if coa_id else ''
            amount_color = 'green' if (amount or 0) > 0 else 'red'
            s+= (f"<tr class='transaction-row' "
                 f"data-account-id='{account_id or ''}' "
                 f"data-date='{date_booking}' "
                 f"data-contact-id='{contact_id or ''}' "
                 f"data-currency='{currency}' "
                 f"data-amount='{amount}'>")
            s+= f"<td>{date_booking}</td>"
            s+= f"<td>{recipient[:25]}</td>"
            s+= f"<td>{text[:35]}</td>"
            s+= f"<td style='color:{amount_color}'>{(amount or 0):.2f}</td>"
            s+= f"<td>{currency}</td>"
            s+= f"<td>{account_name[:20]}</td>"
            s+= f"<td>{contact_name[:20]}</td>"
            s+= f"<td>{coa_number}</td>"
            s+= f"<td>{doc_number}</td>"
            s+= f"<td><a href='/transactions/edit?id={booking_id}'>Bearbeiten</a></td>"
            s+= f"</tr>"

    s+= "</table>"

    s+= '''
    <script>
        function toggleGroup(groupId) {
            const children = document.querySelectorAll(`.child-row[data-parent-group='${groupId}']`);
            const icon = document.getElementById(`toggle-icon-${groupId}`);
            const isOpen = icon && icon.textContent === '▼';
            children.forEach(row => { row.style.display = isOpen ? 'none' : ''; });
            if (icon) icon.textContent = isOpen ? '▶' : '▼';
        }

        function setTransactionYear(year) {
            document.getElementById('dateFrom').value = year + '-01-01';
            document.getElementById('dateTo').value = year + '-12-31';
            filterTransactions();
        }

        function filterTransactions() {
            const dateFrom       = document.getElementById('dateFrom').value;
            const dateTo         = document.getElementById('dateTo').value;
            const customerFilter = document.getElementById('customerFilter').value;
            const currencyFilter = document.getElementById('currencyFilter').value;
            const minAmount      = parseFloat(document.getElementById('minAmount').value) || null;
            const maxAmount      = parseFloat(document.getElementById('maxAmount').value) || null;

            const checkedAccounts = new Set();
            document.querySelectorAll('input[type="checkbox"]:checked').forEach(cb => {
                checkedAccounts.add(cb.id.replace('account_', ''));
            });

            document.querySelectorAll('.transaction-row').forEach(row => {
                const rowDate     = row.getAttribute('data-date');
                const rowContact  = row.getAttribute('data-contact-id');
                const rowCurrency = row.getAttribute('data-currency');
                const rowAmount   = parseFloat(row.getAttribute('data-amount'));
                const rowAccount  = row.getAttribute('data-account-id');

                let show = true;
                if (dateFrom && rowDate < dateFrom) show = false;
                if (dateTo   && rowDate > dateTo)   show = false;
                if (customerFilter && rowContact !== customerFilter) show = false;
                if (currencyFilter && rowCurrency !== currencyFilter) show = false;
                if (minAmount !== null && rowAmount < minAmount) show = false;
                if (maxAmount !== null && rowAmount > maxAmount) show = false;
                if (checkedAccounts.size > 0 && !checkedAccounts.has(rowAccount)) show = false;

                row.style.display = show ? '' : 'none';

                const groupId = row.getAttribute('data-group-id');
                if (groupId && !show) {
                    document.querySelectorAll(`.child-row[data-parent-group='${groupId}']`)
                             .forEach(c => c.style.display = 'none');
                    const icon = document.getElementById(`toggle-icon-${groupId}`);
                    if (icon) icon.textContent = '▶';
                }
            });
        }

        function filterTransactionsByAccount() { filterTransactions(); }
        function filterTransactionsByDate()    { filterTransactions(); }
    </script>
    '''

    s+= Footer()
    return s
