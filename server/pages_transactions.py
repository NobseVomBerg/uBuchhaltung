# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Transactions page (Buchungen: Bankbewegungen + Buchungssätze)
"""
import html as _html
from db import Database
from .pages import Header1, Header2, Header3, Footer
from .period import period_filter_widget

# Spaltenindizes in Bookings (SELECT *):
# [0]  ID            [1]  DateBooking   [2]  DateTax       [3]  BookingGroup_ID
# [4]  Account_ID    [5]  ForeignBankAccount               [6]  RecipientClient
# [7]  Contact_ID    [8]  COA_ID        [9]  CounterCOA_ID [10] Category_ID
# [11] Amount        [12] Currency      [13] TaxRate        [14] TaxAmount
# [15] Text          [16] DocumentNumber


def PageTransactions(db: Database, edit_transaction_id=None, date_from=None, date_to=None):
    """Generate transactions page with edit functionality"""
    # Header2: Konten als kompaktes Dropdown (treibt weiter die clientseitige
    # filterTransactions-Logik) + Such-/Waehrungs-/Betrags-Filter daneben.
    accounts = db.fetch_accounts()
    account_checkboxes = (
        '<div><label><input type="checkbox" id="account_all" checked '
        'onchange="toggleAllAccounts()"> <strong>Alle</strong></label></div>'
        '<hr style="margin:5px 0;border:none;border-top:1px solid #888;">'
    )
    for account in accounts:
        account_id = account[0]
        account_name = _html.escape(str(account[1] or ''))
        account_iban = _html.escape(str(account[3] or ''))  # fuer Filterung
        account_checkboxes += (
            f'<div><label><input type="checkbox" id="account_{account_id}" '
            f'name="account_{account_id}" data-iban="{account_iban}" checked '
            f'onchange="syncAllAccountsAndFilter()"> {account_name}</label></div>'
        )

    # Panel als Overlay per Inline-Style (cache-unabhängig: erscheint über dem
    # Seiteninhalt, statt eine 2. Header-Zeile zu erzeugen). Hintergrund/Padding
    # kommen aus der bereits vorhandenen Klasse rectRounded.
    header2_content = f'''<div class="rowWithObjects">
        <div style="position:relative; display:inline-block;">
            <button type="button" id="acctMenuBtn" onclick="toggleAcctMenu(event)">🏦 Konten <span id="acctMenuLabel">(alle)</span> ▾</button>
            <div class="rectRounded" id="acctMenuPanel" style="display:none; position:absolute; top:100%; left:0; z-index:50; min-width:220px; max-height:340px; overflow-y:auto; white-space:nowrap; box-shadow:0 2px 8px rgba(0,0,0,.25);">{account_checkboxes}</div>
        </div>
        <div>🔍 <input type="text" id="txSearch" placeholder="Empfänger / Verwendungszweck" oninput="filterTransactions()" style="width: 240px;"></div>
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
    </div>'''

    s = Header1('transactions')
    s+= Header2(header2_content)

    # Header3: zentraler Zeitraum-Filter (Von/Bis + Jahr + Monat)
    s+= Header3(period_filter_widget(date_from, date_to, '/transactions'))

    # Load transaction for editing if ID provided
    edit_trans = None
    edit_recipient = ""
    edit_text = ""
    if edit_transaction_id:
        edit_trans = db.get_booking_by_id(edit_transaction_id)
        if edit_trans:
            edit_recipient = _html.escape(edit_trans[6] or "")  # RecipientClient
            edit_text = _html.escape(edit_trans[15] or "")      # Text

            # Für Bank-Buchungen: verknüpfte Entry-Daten laden
            if edit_trans[17] == 'bank':  # BookingType
                entry_data = db.get_linked_entry_for_bank(edit_transaction_id)
                if entry_data:
                    # Fehlende Felder aus dem Entry übernehmen
                    edit_trans = list(edit_trans)
                    if not edit_trans[8]:   edit_trans[8]  = entry_data[0]  # COA_ID
                    if not edit_trans[9]:   edit_trans[9]  = entry_data[1]  # CounterCOA_ID
                    if not edit_trans[13]:  edit_trans[13] = entry_data[2]  # TaxRate
                    if not edit_trans[14]:  edit_trans[14] = entry_data[3]  # TaxAmount
                    if not edit_trans[16]:  edit_trans[16] = entry_data[4]  # DocumentNumber
                    if not edit_trans[7]:   edit_trans[7]  = entry_data[5]  # Contact_ID
                    if not edit_trans[10]:  edit_trans[10] = entry_data[6]  # Category_ID
                    edit_trans = tuple(edit_trans)

    # Get dropdown data (einmalig laden – Maps für Tabelle gleich mitbauen)
    customers    = db.fetch_contacts(contact_type='customer')
    customer_map = {c[0]: _html.escape(str(c[2] or c[3] or '')) for c in customers}
    coa_accounts = db.fetch_chart_of_accounts()
    coa_map      = {c[0]: _html.escape(str(c[2])) for c in coa_accounts}
    private_coa_ids = {c[0] for c in coa_accounts if 2100 <= c[2] < 2200}
    booking_groups = db.fetch_booking_groups()

    # Determine form title and button text
    form_title  = "Transaktion bearbeiten" if edit_trans else "Neue Transaktion"
    submit_text = "Transaktion aktualisieren" if edit_trans else "Transaktion hinzufügen"
    transaction_id = edit_trans[0] if edit_trans else 0

    id_display = (f'<tr><td>ID:</td><td style="color: #666;">{transaction_id}'
                  f'<input type="hidden" name="transaction_id" value="{transaction_id}"></td></tr>'
                  if edit_trans else '')

    s+= f'''
    <div class="grid2Cols gridMain">
    <div class="gridRightCol gridMiddle" style="order:2">
        <div class="rectRounded" style="order:2">
        <h2>{form_title}</h2>
        <form method="POST" action="/transactions/add">
                <table class="form-table">
                    {id_display}
                    <tr><td>Buchungsdatum:</td><td><input type="date" name="date" value="{edit_trans[1] if edit_trans else ""}" required></td></tr>
                    <tr><td>Steuerdatum:</td><td><input type="date" name="date_tax" value="{edit_trans[2] if edit_trans and edit_trans[2] else ""}"></td></tr>
                    <tr><td>Empfänger/Auftragg.:</td><td><input type="text" name="recipient" value="{edit_recipient}" size="40"></td></tr>
                    <tr><td>Verwendungszweck:</td><td><textarea name="text" rows="3" cols="42">{edit_text}</textarea></td></tr>
                    <tr><td>Bankkonto:</td><td><select name="account">
                        <option value="">-- Kein Konto --</option>
    '''
    selected_account_id = edit_trans[4] if edit_trans else None
    # Konto über COA/CounterCOA ableiten wenn nicht direkt gesetzt
    if edit_trans and not selected_account_id:
        _edit_coa = edit_trans[8]
        _edit_ccoa = edit_trans[9]
        _skr_to_acct_edit = {}
        for _a in accounts:
            if _a[7]:  # SKRAccount
                _skr_to_acct_edit[_a[7]] = _a[0]
        for _c in coa_accounts:
            if _c[0] == _edit_coa and _c[2] in _skr_to_acct_edit:
                selected_account_id = _skr_to_acct_edit[_c[2]]
                break
            if _c[0] == _edit_ccoa and _c[2] in _skr_to_acct_edit:
                selected_account_id = _skr_to_acct_edit[_c[2]]
                break
    for account in accounts:
        selected = 'selected' if selected_account_id and account[0] == selected_account_id else ''
        s+= f'<option value="{account[0]}" {selected}>{_html.escape(str(account[1] or ""))}</option>'

    s+= f'''
                    </select></td></tr>
                    <tr><td>Fremdes Konto/IBAN:</td><td><input type="text" name="foreign_account" value="{_html.escape(str(edit_trans[5])) if edit_trans and edit_trans[5] else ""}" size="40"></td></tr>

                    <tr><td>Kunde:</td><td><select name="contact_id">
                        <option value="">-- Kein Kunde --</option>
    '''
    selected_contact_id = edit_trans[7] if edit_trans else None
    for contact in customers:
        selected = 'selected' if selected_contact_id and contact[0] == selected_contact_id else ''
        contact_display = f"{contact[2]} ({contact[3] or 'Privat'})" if contact[2] else contact[3] or f"ID {contact[0]}"
        s+= f'<option value="{contact[0]}" {selected}>{_html.escape(str(contact_display))}</option>'

    s+= f'''
                    </select></td></tr>

                    <tr><td>Split-Buchung:</td><td><select name="booking_group_id">
                        <option value="">-- Keine Gruppierung --</option>
    '''
    selected_booking_group_id = edit_trans[3] if edit_trans else None
    for bg in booking_groups:
        selected = 'selected' if selected_booking_group_id and bg[0] == selected_booking_group_id else ''
        bg_display = f"#{bg[0]} - {bg[1] or 'Ohne Beschreibung'}"
        s+= f'<option value="{bg[0]}" {selected}>{_html.escape(str(bg_display))}</option>'

    s+= f'''
                    </select></td></tr>

                    <tr><td>SKR-Konto:</td><td><select name="coa_id">
                        <option value="">-- Kein SKR-Konto --</option>
    '''
    selected_coa_id = edit_trans[8] if edit_trans else None
    for coa in coa_accounts:
        is_selected = bool(selected_coa_id and coa[0] == selected_coa_id)
        # Ausgeblendete Konten (ShowInMenu=0) nicht anzeigen – außer es ist das aktuell gesetzte
        if not (coa[7] if len(coa) > 7 else 1) and not is_selected:
            continue
        selected = 'selected' if is_selected else ''
        coa_display = f"{coa[2]} - {coa[3]}" if coa[3] else f"{coa[2]}"
        s+= f'<option value="{coa[0]}" {selected}>{_html.escape(str(coa_display))}</option>'

    s+= f'''
                    </select></td></tr>

                    <tr><td>Betrag:</td><td><input type="number" step="0.01" class="noButtons" name="amount" id="amount" value="{edit_trans[11] if edit_trans else ""}" required></td></tr>
                    <tr><td>Währung:</td><td><input type="text" name="currency" value="{_html.escape(str(edit_trans[12])) if edit_trans and edit_trans[12] else "EUR"}" size="5"></td></tr>

                    <tr><td>Steuersatz (%):</td><td><input type="number" step="0.01" class="noButtons" name="tax_rate" id="tax_rate" value="{edit_trans[13]*100 if edit_trans and edit_trans[13] else ""}" placeholder="z.B. 19 für 19%"></td></tr>
                    <tr><td>Steuerbetrag:</td><td><input type="number" step="0.01" class="noButtons" name="tax_amount" id="tax_amount" value="{edit_trans[14] if edit_trans and edit_trans[14] else ""}"></td></tr>

                    <tr><td>Beleg-Nr.:</td><td><input type="text" name="document_nr" value="{_html.escape(str(edit_trans[16])) if edit_trans and edit_trans[16] else ""}"></td></tr>
    '''

    if edit_trans:
        form_buttons = ('<input type="submit" value="Aktualisieren" class="coloredButton btn-sm bg-green">'
                        '<button type="button" onclick="window.location.href=\'/transactions\'" class="coloredButton btn-sm bg-gray">Abbrechen</button>')
    else:
        form_buttons = '<input type="submit" value="Transaktion hinzuf\u00fcgen" class="coloredButton btn-sm bg-green">'

    s+= f'''
                    <tr><td></td><td>
                        {form_buttons}
                    </td></tr>
                </table>
            </form>

            <script>
                // Automatische Berechnung des Steuerbetrags.
                // amount ist der BRUTTO-Betrag; berechnet wird die darin
                // enthaltene USt: tax = brutto * satz / (100 + satz).
                // Das Feld bleibt manuell überschreibbar (Skonto, Mischsätze).
                function calculateTax() {{
                    const amount = parseFloat(document.getElementById('amount').value) || 0;
                    const taxRate = parseFloat(document.getElementById('tax_rate').value) || 0;
                    if (amount !== 0 && taxRate !== 0) {{
                        const taxAmount = amount * taxRate / (100 + taxRate);
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

        s+= "<br><h3>Verknüpfte Dokumente</h3>"
        if linked_documents:
            s+= "<table>"
            s+= "<tr><th>ID</th><th>Nr.</th><th>Datum</th><th>Dateiname</th><th>Typ</th><th>Aktionen</th></tr>"
            for doc in linked_documents:
                doc_id        = doc[0]
                doc_number    = _html.escape(str(doc[1] or ''))
                doc_date      = _html.escape(str(doc[2] or ''))
                doc_filename  = _html.escape(str(doc[3] or ''))
                relation_type = _html.escape(str(doc[-1] or ''))  # RelationType from JOIN
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
        </div><!-- Ende rectRounded Formular -->
        <div class="rectRounded" style="order:1">
            <h2>Kontoauszüge hochladen</h2>
            <div id="dropZone">
                <p>Dateien hier ablegen (Drag & Drop)</p>
                <input type="file" id="fileInput" multiple accept=".pdf,application/pdf">
                <button onclick="document.getElementById('fileInput').click()">Oder Dateien auswählen</button>
            </div>
            <div id="uploadStatus"></div>
        </div><!-- Ende rectRounded Upload -->
    </div><!-- Ende gridRightCol -->

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

        let currentImportId = null;

        function esc(v) {
            if (v === null || v === undefined) return '';
            return String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        }
        function clip(v, n) { v = (v === null || v === undefined) ? '' : String(v); return v.length > n ? v.substring(0, n) : v; }
        function fmtAmount(a) {
            if (a === null || a === undefined || a === '') return '';
            var n = Number(a);
            if (isNaN(n)) return esc(a);
            return n.toFixed(2).replace('.', ',') + ' €';
        }

        // Doppel-Uploads verhindern: während Upload/Analyse läuft, weitere
        // Datei-Auswahl bzw. Drops ignorieren und den Auswahl-Button sperren.
        let uploadBusy = false;
        function uploadFiles(files) {
            if (files.length === 0) return;
            if (uploadBusy) { appMsg('Upload läuft bereits – bitte warten.', 'warn'); return; }
            uploadBusy = true;
            dropZone.querySelector('button').disabled = true;
            uploadStatus.innerHTML = '<p>Lade hoch & analysiere ...</p>';
            const formData = new FormData();
            for (let i = 0; i < files.length; i++) { formData.append('files', files[i]); }
            fetch('/upload_receipts', { method: 'POST', body: formData })
                .then(r => r.json())
                .then(data => {
                    uploadStatus.innerHTML = '';
                    if (data.error) { appMsg('Fehler: ' + data.error, 'error'); return; }
                    currentImportId = data.import_id;
                    renderPreview(data);
                })
                .catch(err => { uploadStatus.innerHTML = ''; appMsg('Fehler beim Hochladen: ' + err, 'error'); })
                .finally(() => {
                    uploadBusy = false;
                    dropZone.querySelector('button').disabled = false;
                    fileInput.value = '';   // gleiche Datei erneut wählbar
                });
        }

        function statusBadge(status) {
            if (status === 'error') return '<span class="badge bg-red">Konto nicht gefunden</span>';
            if (status === 'warn')  return '<span class="badge bg-orange">Hinweise</span>';
            return '<span class="badge bg-green">OK</span>';
        }

        function renderPreview(data) {
            const host = document.getElementById('importPreview');
            const files = data.files || [];
            const others = data.other_files || [];
            let html = '';

            if (others.length) {
                html += '<div class="rectRounded">';
                others.forEach(o => {
                    if (o.status === 'error' || o.status === 'warning')
                        html += '<div class="errorColor">⚠ ' + esc(o.filename) + ': ' + esc(o.error) + '</div>';
                    else if (o.status === 'organized')
                        html += '<div>✓ ' + esc(o.filename) + ' abgelegt (' + esc(o.path) + ')</div>';
                    else
                        html += '<div>✓ ' + esc(o.filename) + ' hochgeladen</div>';
                });
                html += '</div>';
            }

            if (!files.length) {
                host.innerHTML = html;
                if (others.length) appMsg(others.length + ' Datei(en) verarbeitet.', 'info');
                return;
            }

            const importable = files.filter(f => f.status !== 'error').length;
            html += '<div class="rectRounded rowWithObjects">';
            html += '<strong>' + files.length + ' Kontoauszug/-auszüge erkannt</strong> ';
            if (importable > 1) html += '<button type="button" class="coloredButton btn-sm bg-green btnImportAll">Alle importieren</button> ';
            html += '<button type="button" class="coloredButton btn-sm bg-gray btnDismiss">Verwerfen</button>';
            html += '</div>';

            files.forEach(f => { html += renderCard(f); });
            host.innerHTML = html;
        }

        function renderCard(f) {
            const detId = 'det-' + f.file_index;
            let h = '<div class="rectRounded" id="beleg-' + f.file_index + '">';

            h += '<div><strong>' + esc(f.filename) + '</strong> ' + statusBadge(f.status) + '</div>';
            h += '<div class="muted">' + esc(f.bank_code || '') + ' · ' + esc(f.iban || 'IBAN?') +
                 ' · ' + esc(f.document_date || '') + ' · Konto: ' +
                 (f.account_name ? esc(f.account_name) : '<span class="errorColor">nicht gefunden</span>') + '</div>';
            h += '<div class="muted">' + f.total + ' erkannt · ' + f.new_count + ' neu · ' +
                 f.dup_count + ' mögliche Duplikate</div>';

            if (f.problems && f.problems.length) {
                h += '<div class="muted"><em>Auffällige Buchungen:</em></div>';
                h += '<table>';
                f.problems.forEach(p => {
                    let tags = [];
                    if (p.dup) tags.push('Duplikat');
                    (p.warn || []).forEach(w => tags.push(w === 'amount' ? 'Betrag?' : (w === 'date' ? 'Datum?' : 'leer')));
                    h += '<tr class="row-open"><td>' + esc(clip(p.date,10)) + '</td><td>' + esc(clip(p.recipient,30)) +
                         '</td><td>' + esc(clip(p.reference,40)) + '</td><td>' + fmtAmount(p.amount) +
                         '</td><td>' + tags.join(', ') + '</td></tr>';
                });
                h += '</table></div>';
            }

            h += '<button type="button" class="coloredButton btn-sm bg-gray btnToggleDet" data-target="' + detId + '">Alle Einzelbuchungen</button>';
            h += '<div id="' + detId + '" style="display:none">';
            h += '<table><tr><th>Datum</th><th>Empfänger</th><th>Zweck</th><th>Betrag</th></tr>';
            (f.transactions || []).forEach(t => {
                const cls = t.dup ? ' class="row-open"' : '';
                h += '<tr' + cls + '><td>' + esc(clip(t.date,10)) + '</td><td>' + esc(clip(t.recipient,30)) +
                     '</td><td>' + esc(clip(t.reference,40)) + '</td><td>' + fmtAmount(t.amount) + '</td></tr>';
            });
            h += '</table></div>';

            if (f.status === 'error') {
                h += '<div class="errorColor">Import nicht möglich – Bankkonto zu dieser IBAN anlegen.</div>';
            } else {
                h += '<div><button type="button" class="coloredButton btn-sm bg-green btnImportBeleg" data-idx="' +
                     f.file_index + '">Importieren (' + f.new_count + ' neu)</button></div>';
            }
            h += '</div>';
            return h;
        }

        document.addEventListener('click', function(e) {
            const tgl = e.target.closest('.btnToggleDet');
            if (tgl) { const d = document.getElementById(tgl.dataset.target); if (d) d.style.display = (d.style.display === 'none' ? 'block' : 'none'); return; }
            const imp = e.target.closest('.btnImportBeleg');
            if (imp) { importBeleg(imp.dataset.idx); return; }
            if (e.target.closest('.btnImportAll')) { importAll(); return; }
            if (e.target.closest('.btnDismiss')) { dismissPreview(); return; }
        });

        // Während ein Import läuft, alle Import-/Verwerfen-Buttons sperren –
        // Doppelklicks lösen sonst parallele Requests und Fehlermeldungen aus.
        let importBusy = false;
        function setImportBusy(busy) {
            importBusy = busy;
            document.querySelectorAll('.btnImportBeleg, .btnImportAll, .btnDismiss')
                .forEach(b => { b.disabled = busy; });
        }

        function postImport(params) {
            const body = new URLSearchParams();
            body.append('import_id', currentImportId);
            if (params.file_index !== undefined) body.append('file_index', params.file_index);
            return fetch('/confirm_transactions', { method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: body }).then(r => r.json());
        }

        function importBeleg(idx) {
            if (importBusy) return;
            setImportBusy(true);
            postImport({ file_index: idx }).then(data => {
                if (!data.ok) { appMsg('Fehler: ' + (data.error || 'Import fehlgeschlagen'), 'error'); return; }
                const res = (data.results || [])[0] || {};
                if (res.account_found === false) { appMsg(res.error || 'Konto nicht gefunden', 'error'); return; }
                appMsg(res.inserted + ' neu importiert' + (res.skipped ? ', ' + res.skipped + ' Duplikate übersprungen' : ''), 'success');
                const card = document.getElementById('beleg-' + idx);
                if (card) card.remove();
                refreshTable();
            }).catch(err => appMsg('Fehler beim Import: ' + err, 'error'))
              .finally(() => setImportBusy(false));
        }

        function importAll() {
            if (importBusy) return;
            setImportBusy(true);
            postImport({}).then(data => {
                if (!data.ok) { appMsg('Fehler: ' + (data.error || 'Import fehlgeschlagen'), 'error'); return; }
                let ins = 0, skip = 0, failed = 0;
                (data.results || []).forEach(r => { ins += r.inserted || 0; skip += r.skipped || 0; if (r.account_found === false) failed++; });
                appMsg(ins + ' neu importiert' + (skip ? ', ' + skip + ' Duplikate' : '') + (failed ? ', ' + failed + ' ohne Konto' : ''), failed ? 'warn' : 'success');
                dismissPreview();
                refreshTable();
            }).catch(err => appMsg('Fehler beim Import: ' + err, 'error'))
              .finally(() => setImportBusy(false));
        }

        function dismissPreview() {
            document.getElementById('importPreview').innerHTML = '';
            currentImportId = null;
        }

        function refreshTable() {
            fetch(window.location.href).then(r => r.text()).then(html => {
                const doc = new DOMParser().parseFromString(html, 'text/html');
                const fresh = doc.getElementById('transactionsTable');
                const cur = document.getElementById('transactionsTable');
                if (fresh && cur) cur.innerHTML = fresh.innerHTML;
            });
        }

        function openEditForm(id) {
            fetch('/transactions/edit?id=' + id)
                .then(r => r.text())
                .then(html => {
                    const doc = new DOMParser().parseFromString(html, 'text/html');
                    const newForm = doc.querySelector('.gridRightCol');
                    const curForm = document.querySelector('.gridRightCol');
                    if (newForm && curForm) {
                        curForm.innerHTML = newForm.innerHTML;
                        curForm.querySelectorAll('script').forEach(s => {
                            const ns = document.createElement('script');
                            ns.textContent = s.textContent;
                            s.replaceWith(ns);
                        });
                        history.pushState({}, '', '/transactions/edit?id=' + id);
                    }
                })
                .catch(() => { window.location.href = '/transactions/edit?id=' + id; });
        }
    </script>
    '''
    s+= '<div class="gridLeftCol" style="order:1">'    # ── Buchungstabelle ───────────────────────────────────────────────────
    s+= '<div id="importPreview"></div>'              # Inline-Import-Vorschau (per Beleg)
    s+= "<table id='transactionsTable'>"
    s+= ("<tr><th>Datum</th><th>Empfänger/Auftragg.</th><th>Text</th>"
         "<th>Betrag</th><th>Währung</th><th>St.satz</th><th>Konto</th>"
         "<th>SKR</th><th>Beleg-Nr.</th><th>Aktionen</th></tr>")

    bookings = db.fetch_bookings_grouped(date_from, date_to)

    account_map = {a[0]: _html.escape(str(a[1] or '')) for a in accounts}

    # Reverse-Map: COA_ID → (Account_ID, Account_Name)
    # Damit Einträge ohne Account_ID über COA/CounterCOA dem Konto zugeordnet werden.
    _skr_to_acct = {}
    for a in accounts:
        if a[7]:  # SKRAccount
            _skr_to_acct[a[7]] = (a[0], a[1])
    coa_id_to_account = {}
    for c in coa_accounts:
        if c[2] in _skr_to_acct:
            coa_id_to_account[c[0]] = _skr_to_acct[c[2]]

    def _derive_account(account_id, coa_id, counter_coa_id):
        """Account-ID und -Name ableiten: direkt oder über COA/CounterCOA."""
        if account_id:
            return account_id, account_map.get(account_id, '')
        derived = coa_id_to_account.get(coa_id) or coa_id_to_account.get(counter_coa_id)
        if derived:
            return derived  # (account_id, account_name)
        return None, ''

    def _fmt_rate(r):
        """TaxRate (0.19) → '19%'; None/'' → ''."""
        if r is None or r == '':
            return ''
        try:
            return f"{round(float(r) * 100)}%"
        except (ValueError, TypeError):
            return ''

    for item in bookings:
        row_type = item['type']

        if row_type == 'group':
            # ── Split-Gruppen-Kopfzeile ───────────────────────────────────
            gid          = item['group_id']
            date_booking = item['date']
            amount       = item['amount'] or 0
            currency     = _html.escape(str(item['currency'] or ''))
            description  = _html.escape(item['description'] or '')
            count        = item['count']
            account_id   = item['account_id']
            contact_id   = item['contact_id']
            # Konto ableiten: direkt oder über COA/CounterCOA des ersten Kindes
            first_coa    = item.get('first_coa_id')
            first_ccoa   = item.get('first_ccoa_id')
            account_id, account_name = _derive_account(account_id, first_coa, first_ccoa)
            contact_name = customer_map.get(contact_id, '') if contact_id else ''
            first_recip  = _html.escape(item.get('first_recipient') or '')
            first_text   = _html.escape(item.get('first_text') or '')
            amount_color = 'green' if amount > 0 else 'red'
            # ✓-Badge: alle Kinder vollständig gebucht (COA_ID gesetzt)?
            # Entspricht dem gleichen Check wie bei Einzelbuchungen (booking[8] = COA_ID).
            children     = item.get('children', [])
            all_booked   = bool(children) and all(ch['booking'][8] is not None for ch in children)
            status_badge = ("<span class='badge bg-green' title='Buchung vollständig gebucht'>✓</span>"
                            if all_booked else "")
            row_class    = 'transaction-row group-row row-ok' if all_booked else 'transaction-row group-row'
            s+= (f"<tr class='{row_class}' "
                 f"data-group-id='{gid}' "
                 f"data-account-id='{account_id or ''}' "
                 f"data-date='{date_booking}' "
                 f"data-contact-id='{contact_id or ''}' "
                 f"data-currency='{currency}' "
                 f"data-amount='{amount}' "
                 f"data-recipient='{first_recip}' "
                 f"data-text='{first_text}' "
                 f"onclick='toggleGroup({gid})' "
                 f"title='Split-Buchung aufklappen/zuklappen'>")
            s+= f"<td>{date_booking}</td>"
            s+= f"<td>{first_recip[:25]}</td>"
            s+= f"<td>{first_text[:35]}</td>"
            s+= f"<td style='color:{amount_color}'>{amount:.2f}</td>"
            s+= f"<td>{currency}</td>"
            s+= f"<td></td>"
            s+= f"<td>{account_name[:20]}</td>"
            s+= f"<td><span class='badge bg-indigo'>Split {count}×</span></td>"
            s+= f"<td>{description[:35]}</td>"
            s+= f"<td>{status_badge}<span class='split-toggle-icon' id='toggle-icon-{gid}'>▶</span></td>"
            s+= f"</tr>"

        elif row_type == 'bank':
            # ── Bank-Buchung (merged mit Entry-Daten wenn verknüpft) ──────
            booking      = item['booking']
            bank_id      = booking[0]
            date_booking = booking[1]
            account_id   = booking[4]
            recipient    = _html.escape(booking[6] or '')
            amount       = booking[11]
            currency     = _html.escape(booking[12] or 'EUR')
            bank_text    = _html.escape(booking[15] or '')
            is_linked    = item.get('linked', False)
            children     = item.get('children', [])
            count        = len(children)
            bid          = f'b{bank_id}'
            account_name = account_map.get(account_id, '') if account_id else ''
            amount_color = 'green' if (amount or 0) > 0 else 'red'

            # Merged: Entry-Daten übernehmen (aus fetch_bookings_grouped)
            entry_text    = _html.escape(item.get('entry_text')) if item.get('entry_text') else bank_text
            entry_coa_id  = item.get('entry_coa_id')
            entry_docnr   = _html.escape(item.get('entry_docnr') or '')
            entry_contact = item.get('entry_contact_id')
            entry_coa_nr  = coa_map.get(entry_coa_id, '') if entry_coa_id else ''
            entry_contact_name = customer_map.get(entry_contact, '') if entry_contact else ''

            # Privatbuchungen markieren (COA 2100-2199)
            entry_counter_coa_id = item.get('entry_counter_coa_id')
            is_private = bool(
                (entry_coa_id and entry_coa_id in private_coa_ids)
                or (entry_counter_coa_id and entry_counter_coa_id in private_coa_ids))
            if is_private:
                entry_docnr = 'privat'

            # Für Split-Zeilen: alle Beleg-Nrn der Kinder zusammenführen
            if count > 1:
                seen_dns = set()
                parts = []
                for child_item in children:
                    cb = child_item['booking']
                    dn = _html.escape(cb[16] or '')
                    cb_coa = cb[8]
                    cb_ccoa = cb[9]
                    if (cb_coa and cb_coa in private_coa_ids) or (cb_ccoa and cb_ccoa in private_coa_ids):
                        dn = 'privat'
                    if dn and dn not in seen_dns:
                        seen_dns.add(dn)
                        parts.append(dn)
                if parts:
                    entry_docnr = ', '.join(parts)

            if is_linked:
                # Verknüpft: einzeilige Merged-Darstellung
                status_badge = "<span class='badge bg-green' title='Bank + Buchung verknüpft'>✓</span>"
                if count > 1:
                    # Split: aufklappbar
                    s+= (f"<tr class='transaction-row row-ok' "
                         f"data-group-id='{bid}' "
                         f"data-account-id='{account_id or ''}' "
                         f"data-date='{date_booking}' "
                         f"data-contact-id='{entry_contact or ''}' "
                         f"data-currency='{currency}' "
                         f"data-amount='{amount}' "                         f"data-recipient='{recipient}' "
                         f"data-text='{entry_text}' "                         f"onclick='toggleGroup(\"{bid}\")' "
                         f"title='Verknüpfte Split-Buchung aufklappen'>")
                    s+= f"<td>{date_booking}</td>"
                    s+= f"<td>{recipient[:25]}</td>"
                    s+= f"<td>{entry_text[:35]}</td>"
                    s+= f"<td style='color:{amount_color}'>{(amount or 0):.2f}</td>"
                    s+= f"<td>{currency}</td>"
                    s+= f"<td></td>"
                    s+= f"<td>{account_name[:20]}</td>"
                    s+= f"<td><span class='badge bg-indigo'>Split {count}×</span></td>"
                    s+= f"<td>{entry_docnr}</td>"
                    s+= f"<td>{status_badge} <span class='split-toggle-icon' id='toggle-icon-{bid}'>▶</span></td>"
                    s+= f"</tr>"
                else:
                    # Einzelne verknüpfte Buchung: eine Zeile, kein Toggle
                    s+= (f"<tr class='transaction-row row-ok' "
                         f"data-account-id='{account_id or ''}' "
                         f"data-date='{date_booking}' "
                         f"data-contact-id='{entry_contact or ''}' "
                         f"data-currency='{currency}' "
                         f"data-amount='{amount}' "
                         f"data-recipient='{recipient}' "
                         f"data-text='{entry_text}'>")
                    s+= f"<td>{date_booking}</td>"
                    s+= f"<td>{recipient[:25]}</td>"
                    s+= f"<td>{entry_text[:35]}</td>"
                    s+= f"<td style='color:{amount_color}'>{(amount or 0):.2f}</td>"
                    s+= f"<td>{currency}</td>"
                    s+= f"<td>{_fmt_rate(item.get('entry_tax_rate'))}</td>"
                    s+= f"<td>{account_name[:20]}</td>"
                    s+= f"<td>{entry_coa_nr}</td>"
                    s+= f"<td>{entry_docnr}</td>"
                    s+= (f"<td>{status_badge}"
                         f" <a href='javascript:void(0)' onclick='openEditForm({bank_id})' class='action-icon' title='Bearbeiten'>&#9998;</a>"
                         f" <a href='javascript:void(0);' class='action-icon delete-icon' title='L\u00f6schen'"
                         f" onclick='appConfirmHref(\"/transactions/delete?id={bank_id}\", \"Buchung #{bank_id} wirklich l\u00f6schen?\")'>&#128465;</a></td>")
                    s+= f"</tr>"
            else:
                # Nicht verknüpft: als offene Bank-Buchung anzeigen
                status_badge = "<span class='badge bg-orange' title='Noch nicht verbucht'>offen</span>"
                s+= (f"<tr class='transaction-row row-open' "
                     f"data-account-id='{account_id or ''}' "
                     f"data-date='{date_booking}' "
                     f"data-currency='{currency}' "
                     f"data-amount='{amount}' "
                     f"data-recipient='{recipient}' "
                     f"data-text='{bank_text}'>")
                s+= f"<td>{date_booking}</td>"
                s+= f"<td>{recipient[:25]}</td>"
                s+= f"<td>{bank_text[:35]}</td>"
                s+= f"<td style='color:{amount_color}'>{(amount or 0):.2f}</td>"
                s+= f"<td>{currency}</td>"
                s+= f"<td></td>"
                s+= f"<td>{account_name[:20]}</td>"
                s+= f"<td></td>"
                s+= f"<td></td>"
                s+= (f"<td>{status_badge}"
                     f" <a href='javascript:void(0)' onclick='openEditForm({bank_id})' class='action-icon' title='Bearbeiten'>&#9998;</a>"
                     f" <a href='javascript:void(0);' class='action-icon delete-icon' title='L\u00f6schen'"
                     f" onclick='appConfirmHref(\"/transactions/delete?id={bank_id}\", \"Buchung #{bank_id} wirklich l\u00f6schen?\")'>&#128465;</a></td>")
                s+= f"</tr>"

        elif row_type == 'child':
            # ── Teilbuchung einer Split-Gruppe ────────────────────────────
            gid     = item['group_id']
            booking = item['booking']
            booking_id   = booking[0]
            date_booking = booking[1]
            account_id   = booking[4]
            recipient    = _html.escape(booking[6] or '')
            contact_id   = booking[7]
            coa_id       = booking[8]
            amount       = booking[11]
            currency     = _html.escape(booking[12] or 'EUR')
            text         = _html.escape(booking[15] or '')
            doc_number   = _html.escape(booking[16] or '') if len(booking) > 16 else ''
            counter_coa_id = booking[9]
            account_id, account_name = _derive_account(account_id, coa_id, counter_coa_id)
            contact_name = customer_map.get(contact_id, '') if contact_id else ''
            coa_number   = coa_map.get(coa_id, '') if coa_id else ''
            # Privatbuchungen markieren
            if (coa_id and coa_id in private_coa_ids) or (counter_coa_id and counter_coa_id in private_coa_ids):
                doc_number = 'privat'
            amount_color = 'green' if (amount or 0) > 0 else 'red'
            status_badge = ("<span class='badge bg-green' title='Buchung vollst\u00e4ndig gebucht'>\u2713</span>"
                            if coa_id else "")
            s+= (f"<tr class='child-row' data-parent-group='{gid}' style='display:none'>")
            s+= f"<td class='child-indent'>{date_booking}</td>"
            s+= f"<td>{recipient[:25]}</td>"
            s+= f"<td>{text[:35]}</td>"
            s+= f"<td style='color:{amount_color}'>{(amount or 0):.2f}</td>"
            s+= f"<td>{currency}</td>"
            s+= f"<td>{_fmt_rate(booking[13])}</td>"
            s+= f"<td>{account_name[:20]}</td>"
            s+= f"<td>{coa_number}</td>"
            s+= f"<td>{doc_number}</td>"
            s+= (f"<td>{status_badge}"
                 f" <a href='javascript:void(0)' onclick='openEditForm({booking_id})' class='action-icon' title='Bearbeiten'>&#9998;</a>"
                 f" <a href='javascript:void(0);' class='action-icon delete-icon' title='L\u00f6schen'"
                 f" onclick='appConfirmHref(\"/transactions/delete?id={booking_id}\", \"Buchung #{booking_id} wirklich l\u00f6schen?\")'>&#128465;</a></td>")
            s+= f"</tr>"

        else:
            # ── Normale Einzelbuchung ─────────────────────────────────────
            booking      = item['booking']
            booking_id   = booking[0]
            date_booking = booking[1]
            account_id   = booking[4]
            recipient    = _html.escape(booking[6] or '')
            contact_id   = booking[7]
            coa_id       = booking[8]
            amount       = booking[11]
            currency     = _html.escape(booking[12] or 'EUR')
            text         = _html.escape(booking[15] or '')
            doc_number   = _html.escape(booking[16] or '') if len(booking) > 16 else ''
            counter_coa_id = booking[9]
            account_id, account_name = _derive_account(account_id, coa_id, counter_coa_id)
            contact_name = customer_map.get(contact_id, '') if contact_id else ''
            coa_number   = coa_map.get(coa_id, '') if coa_id else ''
            # Privatbuchungen markieren
            if (coa_id and coa_id in private_coa_ids) or (counter_coa_id and counter_coa_id in private_coa_ids):
                doc_number = 'privat'
            amount_color = 'green' if (amount or 0) > 0 else 'red'
            if coa_id:
                status_badge = "<span class='badge bg-green' title='Buchung vollst\u00e4ndig gebucht'>\u2713</span>"
            else:
                status_badge = ""
            s+= (f"<tr class='transaction-row' "
                 f"data-account-id='{account_id or ''}' "
                 f"data-date='{date_booking}' "
                 f"data-contact-id='{contact_id or ''}' "
                 f"data-currency='{currency}' "
                 f"data-amount='{amount}' "
                 f"data-recipient='{recipient}' "
                 f"data-text='{text}'>")
            s+= f"<td>{date_booking}</td>"
            s+= f"<td>{recipient[:25]}</td>"
            s+= f"<td>{text[:35]}</td>"
            s+= f"<td style='color:{amount_color}'>{(amount or 0):.2f}</td>"
            s+= f"<td>{currency}</td>"
            s+= f"<td>{_fmt_rate(booking[13])}</td>"
            s+= f"<td>{account_name[:20]}</td>"
            s+= f"<td>{coa_number}</td>"
            s+= f"<td>{doc_number}</td>"
            s+= (f"<td>{status_badge}"
                 f" <a href='javascript:void(0)' onclick='openEditForm({booking_id})' class='action-icon' title='Bearbeiten'>&#9998;</a>"
                 f" <a href='javascript:void(0);' class='action-icon delete-icon' title='L\u00f6schen'"
                 f" onclick='appConfirmHref(\"/transactions/delete?id={booking_id}\", \"Buchung #{booking_id} wirklich l\u00f6schen?\")'>&#128465;</a></td>")
            s+= f"</tr>"

    s+= "</table>"
    s+= '</div><!-- Ende gridLeftCol --></div><!-- Ende grid2Cols -->'

    s+= '''
    <script>
        function toggleGroup(groupId) {
            const children = document.querySelectorAll(`.child-row[data-parent-group='${groupId}']`);
            const icon = document.getElementById(`toggle-icon-${groupId}`);
            const isOpen = icon && icon.textContent === '▼';
            children.forEach(row => { row.style.display = isOpen ? 'none' : ''; });
            if (icon) icon.textContent = isOpen ? '▶' : '▼';
        }

        function filterTransactions() {
            const txSearch       = (document.getElementById('txSearch').value || '').toLowerCase();
            const currencyFilter = document.getElementById('currencyFilter').value;
            const minAmount      = parseFloat(document.getElementById('minAmount').value) || null;
            const maxAmount      = parseFloat(document.getElementById('maxAmount').value) || null;

            const allCb = document.getElementById('account_all');
            const showAllAccounts = allCb && allCb.checked;

            const checkedAccounts = new Set();
            if (!showAllAccounts) {
                document.querySelectorAll('input[type="checkbox"][id^="account_"]:not(#account_all):checked').forEach(cb => {
                    checkedAccounts.add(cb.id.replace('account_', ''));
                });
            }

            document.querySelectorAll('.transaction-row').forEach(row => {
                const rowCurrency = row.getAttribute('data-currency');
                const rowAmount   = parseFloat(row.getAttribute('data-amount'));
                const rowAccount  = row.getAttribute('data-account-id');

                let show = true;
                if (txSearch) {
                    const rowRecipient = (row.getAttribute('data-recipient') || '').toLowerCase();
                    const rowText = (row.getAttribute('data-text') || '').toLowerCase();
                    if (!rowRecipient.includes(txSearch) && !rowText.includes(txSearch)) show = false;
                }
                if (currencyFilter && rowCurrency !== currencyFilter) show = false;
                if (minAmount !== null && rowAmount < minAmount) show = false;
                if (maxAmount !== null && rowAmount > maxAmount) show = false;
                // Kontofilter: "Alle" = kein Filter; sonst nur gewählte Konten
                if (!showAllAccounts && !checkedAccounts.has(rowAccount)) show = false;

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

        function toggleAllAccounts() {
            const allCb = document.getElementById('account_all');
            const checked = allCb ? allCb.checked : true;
            document.querySelectorAll('input[type="checkbox"][id^="account_"]:not(#account_all)')
                .forEach(cb => { cb.checked = checked; });
            updateAcctMenuLabel();
            filterTransactions();
        }

        function syncAllAccountsAndFilter() {
            const accountCbs = document.querySelectorAll('input[type="checkbox"][id^="account_"]:not(#account_all)');
            const allCb = document.getElementById('account_all');
            if (allCb) {
                allCb.checked = Array.from(accountCbs).every(cb => cb.checked);
            }
            updateAcctMenuLabel();
            filterTransactions();
        }

        function filterTransactionsByAccount() { filterTransactions(); }
        function filterTransactionsByDate()    { filterTransactions(); }

        // ── Konten-Dropdown (kompaktes Overlay statt langer Checkbox-Leiste) ──
        function toggleAcctMenu(ev) {
            ev.stopPropagation();
            const p = document.getElementById('acctMenuPanel');
            p.style.display = (p.style.display === 'block') ? 'none' : 'block';
        }
        function updateAcctMenuLabel() {
            const all = document.getElementById('account_all');
            const cbs = document.querySelectorAll('input[type="checkbox"][id^="account_"]:not(#account_all)');
            const checked = Array.from(cbs).filter(cb => cb.checked).length;
            const label = document.getElementById('acctMenuLabel');
            if (!label) return;
            if (all && all.checked || checked === cbs.length) label.textContent = '(alle)';
            else if (checked === 0) label.textContent = '(keine)';
            else label.textContent = '(' + checked + '/' + cbs.length + ')';
        }
        // Panel schließen, wenn außerhalb des Buttons/Panels geklickt wird
        document.addEventListener('click', function(ev) {
            const btn = document.getElementById('acctMenuBtn');
            const panel = document.getElementById('acctMenuPanel');
            if (!panel) return;
            if (panel.contains(ev.target) || (btn && btn.contains(ev.target))) return;
            panel.style.display = 'none';
        });
        document.addEventListener('DOMContentLoaded', updateAcctMenuLabel);
    </script>
    '''

    s+= Footer()
    return s
