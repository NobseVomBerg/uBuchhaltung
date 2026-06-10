# -*- coding: utf-8 -*-
"""Angebots-Seite (Quotes): Liste + Anlegen/Bearbeiten.

Teilt sich die `Invoices`-Tabelle mit den Rechnungen (Diskriminator DocumentType),
ist aber eine eigene Seite unter /quote (Unter-Tab bei „Rechnung"). Reine
Phase-1-Umsetzung: Fließtext vor/nach den Positionen mit einfacher Formatierung
(fett/kursiv) – mehrseitiger Rich-Text folgt in Phase 2.
"""
import datetime
import os
import html as _html
from db import Database
from .pages import Header1, Header2, Header3, Footer, logo_url
from .pages_invoice import _json_for_script, document_submenu


# Angebots-Status
QUOTE_STATUS_COLORS = {
    'draft':     '#888',
    'sent':      '#ff9900',
    'accepted':  '#00aa00',
    'rejected':  '#cc0000',
    'converted': '#0066cc',
}
QUOTE_STATUS_LABELS = {
    'draft':     'Entwurf',
    'sent':      'Versendet',
    'accepted':  'Angenommen',
    'rejected':  'Abgelehnt',
    'converted': 'Umgewandelt',
}


def PageQuote(db: Database, filters: dict = None, quote_id=None):
    """Angebotsliste + Formular."""
    filters = filters or {}
    status_filter = filters.get('status', '')
    search_query = filters.get('search', '')

    if status_filter and status_filter != 'all':
        quotes = db.fetch_quotes(status=status_filter)
    else:
        quotes = db.fetch_quotes()

    if search_query:
        q = search_query.lower()
        quotes = [x for x in quotes
                  if q in str(x[1]).lower() or q in str(x[14]).lower()]

    s = Header1('invoice')

    # Submenü + Such-/Statusfilter gemeinsam einzeilig in Header2
    status_options = {'all': 'Alle', **QUOTE_STATUS_LABELS}
    status_dropdown = '<select id="statusFilter" onchange="applyQuoteFilters()">'
    for value, label in status_options.items():
        sel = 'selected' if status_filter == value or (not status_filter and value == 'all') else ''
        status_dropdown += f'<option value="{value}" {sel}>{label}</option>'
    status_dropdown += '</select>'
    s += Header2(
        '<div class="rowWithObjects">'
        f'<div>{document_submenu("quote")}</div>'
        f'<div><label>🔍 Suche:</label> <input type="text" id="searchQuery" '
        f'value="{_html.escape(search_query)}" placeholder="A-Nr. oder Kunde" '
        'onchange="applyQuoteFilters()" style="width: 200px;"></div>'
        f'<div><label>Status:</label> {status_dropdown}</div>'
        '</div>'
    )
    s += Header3()

    # Statistik
    total_count = len(quotes)
    total_sum = sum(x[38] for x in quotes if len(x) > 38 and x[38])
    open_sum = sum(x[38] for x in quotes if len(x) > 38 and x[38]
                   and len(x) > 40 and x[40] in ('draft', 'sent'))

    s += '<div class="grid2Cols gridMain"><div class="gridLeftCol" style="order:1">'
    s += f'''
    <div class="rectRounded">
        <strong>Statistik:</strong>
        {total_count} Angebot(e) |
        Gesamtsumme: {total_sum:.2f} € |
        Offen: {open_sum:.2f} €
    </div>
    '''

    if not quotes:
        s += "<p><em>Keine Angebote gefunden.</em></p>"
    else:
        s += "<table style='width: 100%;'>"
        s += ("<tr><th>A-Nr.</th><th>Datum</th><th>Kunde</th><th>Netto</th>"
              "<th>Brutto</th><th>Status</th><th>g&uuml;ltig bis</th><th>Aktionen</th></tr>")
        for q in quotes:
            qid = q[0]
            q_number = _html.escape(str(q[1] or ''))
            q_date = q[2]
            buyer = _html.escape(str(q[14] or ''))
            sum_net = q[36]
            sum_gross = q[38]
            status = q[40]
            valid_until = q[46] if len(q) > 46 else ''
            color = QUOTE_STATUS_COLORS.get(status, '#888')
            label = QUOTE_STATUS_LABELS.get(status, status)
            pdf_path = q[41] if len(q) > 41 else None
            pdf_exists = "true" if (pdf_path and os.path.exists(pdf_path)) else "false"
            s += "<tr>"
            s += f"<td>{q_number}</td><td>{q_date}</td><td>{buyer[:30]}</td>"
            s += f"<td style='text-align:right;'>{sum_net:.2f} €</td>"
            s += f"<td style='text-align:right;'><strong>{sum_gross:.2f} €</strong></td>"
            s += f"<td style='color:{color};'><strong>{label}</strong></td>"
            s += f"<td>{valid_until or '–'}</td>"
            s += "<td>"
            s += (f'<a href="javascript:void(0);" onclick="handleQuotePDF({qid}, {pdf_exists})" '
                  'class="action-icon" title="PDF">&#128196;</a> ')
            s += f"<a href='/quote?id={qid}' class='action-icon' title='Bearbeiten'>&#9998;</a> "
            if status != 'converted':
                s += (f'<a href="javascript:void(0);" onclick="convertQuote({qid})" '
                      'class="action-icon" title="In Rechnung umwandeln">&#10138;</a> ')
            s += (f'<a href="javascript:void(0);" onclick="deleteQuote({qid})" '
                  'class="action-icon" title="Löschen" style="color:#c00;">&#128465;</a>')
            s += "</td></tr>"
        s += "</table>"

    s += '''
    <script>
        function handleQuotePDF(quoteId, pdfExists) {
            if (!pdfExists || confirm('PDF existiert bereits. Überschreiben und neu generieren?')) {
                fetch('/quote/pdf_generate?id=' + quoteId)
                    .then(r => r.json())
                    .then(d => { alert(d.success ? ('PDF erstellt:\\n' + d.pdf_path)
                                                  : ('Fehler: ' + (d.error || '?'))); });
            }
        }
        function convertQuote(quoteId) {
            if (!confirm('Aus diesem Angebot eine Rechnung erstellen? Das Angebot wird als "Umgewandelt" markiert.')) return;
            const f = document.createElement('form');
            f.method = 'POST'; f.action = '/quote/convert';
            const i = document.createElement('input');
            i.type = 'hidden'; i.name = 'quote_id'; i.value = quoteId;
            f.appendChild(i); document.body.appendChild(f); f.submit();
        }
        function deleteQuote(quoteId) {
            if (!confirm('Angebot wirklich löschen?')) return;
            window.location.href = '/quote/delete?id=' + quoteId;
        }
        function applyQuoteFilters() {
            const status = document.getElementById('statusFilter').value;
            const search = document.getElementById('searchQuery').value;
            const p = new URLSearchParams();
            if (status && status !== 'all') p.append('status', status);
            if (search) p.append('search', search);
            window.location.href = '/quote' + (p.toString() ? '?' + p.toString() : '');
        }
    </script>
    '''
    s += '</div><!-- Ende gridLeftCol -->'
    s += '<div class="gridRightCol" style="order:2; min-width:820px;">'
    s += _quote_form_html(db, quote_id)
    s += '</div><!-- Ende gridRightCol --></div><!-- Ende grid2Cols -->'
    s += Footer()
    return s


def _quote_form_html(db: Database, quote_id=None):
    """Angebotsformular (ohne Seiten-Wrapper)."""
    current_year = datetime.datetime.now().year

    existing = None
    existing_items = []
    if quote_id:
        existing = db.get_invoice_by_id(quote_id)
        if existing:
            existing_items = db.get_invoice_items(quote_id)

    own_contacts = db.fetch_contacts(contact_type='own')
    own_contact = own_contacts[0] if own_contacts else None
    customers = db.fetch_contacts(contact_type='customer')
    articles = db.fetch_articles(active_only=True)

    if existing:
        quote_number = _html.escape(str(existing[1] or ''))
        quote_date = existing[2] or ''
        selected_own_id = existing[3]
        selected_customer_id = existing[13]
        quote_status = existing[40]
        valid_until = existing[46] if len(existing) > 46 else ''
        intro_text = existing[47] if len(existing) > 47 else ''
        closing_text = existing[48] if len(existing) > 48 else ''
        pdf_path = existing[41] if len(existing) > 41 else None
        page_title = f"Angebot {quote_number} bearbeiten"
        is_edit = True
    else:
        # nächste Angebotsnummer ermitteln (Letter 'A')
        quote_number = ''
        ranges = db.fetch_number_ranges('quote')
        current_range = None
        for r in ranges:
            if r[2] == current_year:
                current_range = r
                break
        if not current_range and ranges:
            current_range = ranges[0]
        if current_range:
            year = current_range[2]; letter = current_range[3]
            prefix = current_range[4] or ''; current_num = current_range[5] or 0
            quote_number = f"{str(year)[-2:]}{letter}{prefix}{current_num + 1:03d}"
        else:
            quote_number = f"{str(current_year)[-2:]}A001"
        quote_date = ''
        selected_own_id = own_contact[0] if own_contact else None
        selected_customer_id = None
        quote_status = 'draft'
        valid_until = ''
        intro_text = ''
        closing_text = ''
        pdf_path = None
        page_title = "Neues Angebot erstellen"
        is_edit = False

    pdf_file_exists = bool(pdf_path and os.path.exists(pdf_path))

    s = f'<input type="hidden" id="quote_id" value="{quote_id or ""}">'
    s += f'<input type="hidden" id="is_edit_mode" value="{str(is_edit).lower()}">'
    s += f'<input type="hidden" id="quote_status_value" value="{quote_status}">'
    s += f'<input type="hidden" id="pdf_exists" value="{str(pdf_file_exists).lower()}">'
    s += '<div id="quote_msg" class="no-pdf" style="display:none; margin-bottom:10px;"></div>'

    # Kopf-Box mit Titel, Status, Buttons
    s += '<div class="rectRounded no-pdf">'
    s += f'<h2>{page_title}</h2>'
    if is_edit:
        color = QUOTE_STATUS_COLORS.get(quote_status, '#888')
        label = QUOTE_STATUS_LABELS.get(quote_status, quote_status)
        status_opts = ''.join(
            f'<option value="{k}"{" selected" if k == quote_status else ""}>{v}</option>'
            for k, v in QUOTE_STATUS_LABELS.items())
        s += f'''
        <table width="100%">
            <tr><td>Status:</td><td>
                <strong class="coloredButtonNoClick btn-smNoClick bg-back" style="color:{color};">{label}</strong>
            </td></tr>
            <tr><td>Neuer Status:</td><td><div class="rowWithObjects">
                <select id="statusChangeSelect">{status_opts}</select>
                <button onclick="setQuoteStatus({quote_id})" class="coloredButton btn-sm bg-blue">Status setzen</button>
            </div></td></tr>
            <tr><td>Angebot:</td><td><div class="rowWithObjects">
                <button onclick="saveQuote()" class="coloredButton btn-sm bg-green">💾 Speichern</button>
                <button onclick="window.location.href='/quote'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>
                <button onclick="generateQuotePDF()" class="coloredButton btn-sm bg-blue">📄 PDF</button>
                <button onclick="convertQuote({quote_id})" class="coloredButton btn-sm bg-orange">➔ In Rechnung umwandeln</button>
                <button onclick="if(confirm('Angebot wirklich löschen?')) window.location.href='/quote/delete?id={quote_id}'" class="coloredButton btn-sm bg-red">🗑 Löschen</button>
            </div></td></tr>
        </table>'''
    else:
        s += '''
        <table width="100%">
            <tr><td>Angebot:</td><td><div class="rowWithObjects">
                <button onclick="saveQuote()" class="coloredButton btn-sm bg-green">💾 Speichern</button>
                <button onclick="window.location.href='/quote'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>
            </div></td></tr>
        </table>'''
    s += '</div>'

    # WYSIWYG-Dokument (gleiche .invoice-* Optik)
    s += '<div class="invoice-container" id="quote_container">'
    s += '''
        <div class="invoice-header">
            <div class="invoice-logo">
                <img id="company_logo" src="" alt="Firmenlogo" style="max-width:150px; max-height:80px;" onerror="this.style.display='none';">
            </div>
            <div class="invoice-meta">
                <table class="invoice-meta-table">'''
    s += f'<tr><td>Datum:</td><td><input type="date" id="quote_date" value="{quote_date}" style="width:150px;"></td></tr>'
    s += f'<tr><td>Angebots-Nr.:</td><td><input type="text" id="quote_number" value="{quote_number}" style="width:150px;"></td></tr>'
    s += f'<tr><td>g&uuml;ltig bis:</td><td><input type="date" id="valid_until" value="{valid_until or ""}" style="width:150px;"></td></tr>'
    s += '''
                    <tr><td>Kunden-Nr.:</td><td><input type="text" id="customer_number" readonly style="width:150px; background-color:#f0f0f0;"></td></tr>
                </table>
            </div>
        </div>
        <div class="invoice-address-block">
            <div class="invoice-customer-address">
                <select id="own_company_select" onchange="updateOwnCompany()" style="margin-bottom:10px; width:100%;" class="no-pdf">
                    <option value="">-- Eigene Firma auswählen --</option>'''
    for own in own_contacts:
        nm = _html.escape(str(own[3])) if own[3] else f"ID {own[0]}"
        sel = 'selected' if (existing and own[0] == selected_own_id) or (not existing and own_contact and own[0] == own_contact[0]) else ''
        s += f'<option value="{own[0]}" {sel}>{nm}</option>'
    s += '</select><div class="invoice-sender-line" id="sender_line">'
    if own_contact:
        s += (f'{_html.escape(str(own_contact[3] or ""))} · {_html.escape(str(own_contact[5] or ""))} · '
              f'{_html.escape(str(own_contact[6] or ""))} {_html.escape(str(own_contact[7] or ""))}')
    s += '''</div>
                <select id="customer_select" onchange="updateCustomerAddress()" style="margin-bottom:10px; width:100%;" class="no-pdf">
                    <option value="">-- Kunde auswählen --</option>'''
    for c in customers:
        nm = _html.escape(str(c[3])) if c[3] else f"ID {c[0]}"
        sel = 'selected' if existing and c[0] == selected_customer_id else ''
        s += f'<option value="{c[0]}" {sel}>{nm}</option>'
    s += '''</select>
                <div id="customer_address_display" style="min-height:80px; white-space:pre-line;"></div>
            </div>
        </div>

        <h1 class="invoice-title">Angebot</h1>

        <!-- Einleitungstext -->
        <div class="invoice-richtext-toolbar no-pdf">
            <button type="button" onmousedown="event.preventDefault()" onclick="document.execCommand('bold')"><b>F</b></button>
            <button type="button" onmousedown="event.preventDefault()" onclick="document.execCommand('italic')"><i>K</i></button>
            <span class="rt-label">Einleitungstext</span>
        </div>'''
    s += f'<div class="invoice-richtext" id="intro_text_editor" contenteditable="true">{intro_text or ""}</div>'
    s += '''
        <table class="invoice-items" id="quote_table">
            <thead>
                <tr>
                    <th style="width:40px;">Pos.</th>
                    <th style="width:70px;">Menge</th>
                    <th style="width:70px;">Einheit</th>
                    <th>Bezeichnung</th>
                    <th style="width:100px;">Einzelpreis</th>
                    <th style="width:100px;">Gesamt</th>
                    <th style="width:30px;" class="no-pdf"></th>
                </tr>
            </thead>
            <tbody id="quote_items_body">'''
    if existing_items:
        for idx, item in enumerate(existing_items, 1):
            quantity = item[5]; unit = item[6] or 'Stk.'
            description = _html.escape(str(item[4] or '')); price = item[7]; total = item[8]
            s += f'''
                <tr class="invoice-item-row" data-row="{idx}">
                    <td>{idx}</td>
                    <td><input type="number" class="item-quantity" value="{quantity}" min="0" step="0.01" style="width:60px;"></td>
                    <td><input type="text" class="item-unit" value="{unit}" style="width:60px;"></td>
                    <td><input type="text" class="item-description" value="{description}" style="width:100%;"></td>
                    <td><input type="number" class="item-price" value="{price}" min="0" step="0.01" style="width:80px;"> €</td>
                    <td class="item-total" style="text-align:right;">{total:.2f} €</td>
                    <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color:red;">✕</button></td>
                </tr>'''
    else:
        s += '''
                <tr class="invoice-item-row" data-row="1">
                    <td>1</td>
                    <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width:60px;"></td>
                    <td><input type="text" class="item-unit" value="Stk." style="width:60px;"></td>
                    <td><input type="text" class="item-description" style="width:100%;"></td>
                    <td><input type="number" class="item-price" value="0" min="0" step="0.01" style="width:80px;"> €</td>
                    <td class="item-total" style="text-align:right;">0,00 €</td>
                    <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color:red;">✕</button></td>
                </tr>'''
    s += '''
            </tbody>
            <tfoot>
                <tr><td colspan="7" style="height:10px; border:none;"></td></tr>
                <tr class="totals-row totals-row-border">
                    <td colspan="5" style="text-align:right; border:none;">Summe netto:</td>
                    <td id="sum_net" style="text-align:right; font-weight:bold;">0,00 €</td>
                    <td class="no-pdf" style="border:none;"></td>
                </tr>
                <tr class="totals-row">
                    <td colspan="4" style="text-align:right; border:none;">Mehrwertsteuer</td>
                    <td style="text-align:right; border:none;"><input type="number" id="tax_rate" value="19" min="0" max="100" step="0.1" style="width:50px;">% auf <span id="tax_base">0,00</span> € netto:</td>
                    <td id="tax_amount" style="text-align:right; font-weight:bold;">0,00 €</td>
                    <td class="no-pdf" style="border:none;"></td>
                </tr>
                <tr class="totals-row totals-row-final">
                    <td colspan="5" style="text-align:right; border:none;"><strong>Gesamtbetrag:</strong></td>
                    <td id="sum_gross" style="text-align:right; font-weight:bold;"><strong>0,00 €</strong></td>
                    <td class="no-pdf" style="border:none;"></td>
                </tr>
            </tfoot>
        </table>

        <div style="margin:10px 0;" class="no-pdf">
            <button type="button" onclick="addFreeRow()" style="margin-right:10px;">+ Position frei editierbar hinzufügen</button>
            <button type="button" onclick="showArticleModal()">+ Position aus Artikelverzeichnis</button>
        </div>

        <!-- Schlusstext -->
        <div class="invoice-richtext-toolbar no-pdf">
            <button type="button" onmousedown="event.preventDefault()" onclick="document.execCommand('bold')"><b>F</b></button>
            <button type="button" onmousedown="event.preventDefault()" onclick="document.execCommand('italic')"><i>K</i></button>
            <span class="rt-label">Schlusstext</span>
        </div>'''
    s += f'<div class="invoice-richtext" id="closing_text_editor" contenteditable="true">{closing_text or ""}</div>'

    # Artikel-Modal
    s += '''
        <div id="articleModal" class="modal-overlay no-pdf">
            <div class="modal-content">
                <h3>Artikel aus Verzeichnis auswählen</h3>
                <table border="1" style="width:100%;">
                    <tr><th>Bezeichnung</th><th>Einheit</th><th>Preis (netto)</th><th>MwSt</th><th></th></tr>'''
    for a in articles:
        s += (f'<tr><td>{_html.escape(str(a[1] or ""))}</td><td>{_html.escape(str(a[2] or "Stk."))}</td>'
              f'<td style="text-align:right;">{(a[3] or 0):.2f} €</td><td>{(a[4] or 19):.0f}%</td>'
              f'<td><button type="button" class="modal-button-add" onclick="addArticleRow({a[0]})">Hinzufügen</button></td></tr>')
    s += '''                </table><br>
                <button type="button" class="modal-button-close" onclick="hideArticleModal()">Schließen</button>
            </div>
        </div>
    </div><!-- Ende invoice-container -->'''

    # ── Daten + JS ────────────────────────────────────────────────────────────
    own_dict = {str(o[0]): {
        'name': o[3] or '', 'company': o[4] or o[3] or '',
        'street': o[5] or '', 'postal': o[6] or '', 'city': o[7] or '',
        'email': o[9] or '', 'phone': o[10] or '', 'tax_id': o[11] or '',
        'logo': logo_url(o[13]) if len(o) > 13 and o[13] else ''} for o in own_contacts}
    cust_dict = {str(c[0]): {
        'customer_number': c[2] or '', 'name': c[3] or '', 'company': c[4] or '',
        'street': c[5] or '', 'postal': c[6] or '', 'city': c[7] or ''} for c in customers}
    art_dict = {str(a[0]): {
        'name': a[1] or '', 'unit': a[2] or 'Stk.',
        'price': float(a[3]) if a[3] is not None else 0,
        'taxRate': a[4] or 19, 'description': a[5] or ''} for a in articles}

    s += '<script>\n'
    s += 'const ownCompaniesData = ' + _json_for_script(own_dict) + ';\n'
    s += 'const customersData = ' + _json_for_script(cust_dict) + ';\n'
    s += 'const articlesData = ' + _json_for_script(art_dict) + ';\n'
    s += '''
        if (!document.getElementById('quote_date').value) {
            document.getElementById('quote_date').valueAsDate = new Date();
        }
        const isEditMode = document.getElementById('is_edit_mode').value === 'true';

        function updateOwnCompany() {
            const id = document.getElementById('own_company_select').value;
            const logo = document.getElementById('company_logo');
            const sender = document.getElementById('sender_line');
            if (!id) { logo.src=''; logo.style.display='none'; sender.textContent='Eigene Adresse in Kontakte anlegen (Typ: own)'; return; }
            const c = ownCompaniesData[id];
            if (!c) return;
            if (c.logo) { logo.src=c.logo; logo.style.display=''; } else { logo.src=''; logo.style.display='none'; }
            const dn = c.company || c.name;
            sender.textContent = dn + ' · ' + c.street + ' · ' + c.postal + ' ' + c.city;
        }
        function updateCustomerAddress() {
            const id = document.getElementById('customer_select').value;
            const disp = document.getElementById('customer_address_display');
            const num = document.getElementById('customer_number');
            if (!id) { disp.innerHTML=''; num.value=''; return; }
            const c = customersData[id];
            if (!c) return;
            const dn = c.company || c.name;
            let a = dn + '\\n';
            if (c.street) a += c.street + '\\n';
            if (c.postal || c.city) a += (c.postal + ' ' + c.city).trim();
            disp.textContent = a; num.value = c.customer_number;
        }

        function showArticleModal(){ document.getElementById('articleModal').style.display='block'; }
        function hideArticleModal(){ document.getElementById('articleModal').style.display='none'; }
        window.onclick = function(e){ const m=document.getElementById('articleModal'); if(e.target==m) hideArticleModal(); };

        let rowCounter = 1;
        function addFreeRow() {
            rowCounter++;
            const tb = document.getElementById('quote_items_body');
            const r = document.createElement('tr');
            r.className='invoice-item-row'; r.setAttribute('data-row', rowCounter);
            r.innerHTML = `
                <td>${rowCounter}</td>
                <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width:60px;"></td>
                <td><input type="text" class="item-unit" value="Stk." style="width:60px;"></td>
                <td><input type="text" class="item-description" style="width:100%;"></td>
                <td><input type="number" class="item-price" value="0" min="0" step="0.01" style="width:80px;"> €</td>
                <td class="item-total" style="text-align:right;">0,00 €</td>
                <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color:red;">✕</button></td>`;
            tb.appendChild(r); attachCalculationListeners(r); calculateTotals();
        }
        function addArticleRow(articleId) {
            const a = articlesData[articleId]; if (!a) return;
            rowCounter++;
            const tb = document.getElementById('quote_items_body');
            const r = document.createElement('tr');
            r.className='invoice-item-row'; r.setAttribute('data-row', rowCounter); r.setAttribute('data-article-id', articleId);
            const dn = a.description ? a.name + ' - ' + a.description : a.name;
            r.innerHTML = `
                <td>${rowCounter}</td>
                <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width:60px;"></td>
                <td><span class="item-unit-display">${a.unit}</span><input type="hidden" class="item-unit" value="${a.unit}"></td>
                <td><span class="item-description-display">${dn}</span><input type="hidden" class="item-description" value="${dn}"></td>
                <td><span class="item-price-display">${a.price.toFixed(2).replace('.', ',')} €</span><input type="hidden" class="item-price" value="${a.price}"></td>
                <td class="item-total" style="text-align:right;">0,00 €</td>
                <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color:red;">✕</button></td>`;
            tb.appendChild(r); attachCalculationListeners(r); calculateTotals(); hideArticleModal();
        }
        function removeRow(b){ b.closest('tr').remove(); renumberRows(); calculateTotals(); }
        function renumberRows(){
            const rows = document.querySelectorAll('.invoice-item-row');
            rows.forEach((row,i)=>{ row.querySelector('td:first-child').textContent=i+1; row.setAttribute('data-row', i+1); });
            rowCounter = rows.length;
        }
        function calculateRowTotal(row){
            const q = parseFloat(row.querySelector('.item-quantity').value)||0;
            const p = parseFloat(row.querySelector('.item-price').value)||0;
            row.querySelector('.item-total').textContent = (q*p).toFixed(2).replace('.', ',') + ' €';
        }
        function calculateTotals(){
            let net = 0;
            document.querySelectorAll('.invoice-item-row').forEach(row=>{
                calculateRowTotal(row);
                net += parseFloat(row.querySelector('.item-total').textContent.replace(' €','').replace(',','.'))||0;
            });
            document.getElementById('sum_net').textContent = net.toFixed(2).replace('.', ',') + ' €';
            document.getElementById('tax_base').textContent = net.toFixed(2).replace('.', ',');
            const tr = parseFloat(document.getElementById('tax_rate').value)||0;
            const tax = net*(tr/100);
            document.getElementById('tax_amount').textContent = tax.toFixed(2).replace('.', ',') + ' €';
            document.getElementById('sum_gross').innerHTML = '<strong>' + (net+tax).toFixed(2).replace('.', ',') + ' €</strong>';
        }
        function attachCalculationListeners(row){
            row.querySelector('.item-quantity').addEventListener('input', calculateTotals);
            row.querySelector('.item-price').addEventListener('input', calculateTotals);
        }
        document.getElementById('tax_rate').addEventListener('input', calculateTotals);
        document.querySelectorAll('.invoice-item-row').forEach(attachCalculationListeners);
        calculateTotals();
        updateOwnCompany();
        if (isEditMode) { updateCustomerAddress(); }

        function showMessage(text, type){
            type = type || 'success';
            const bar = document.getElementById('quote_msg'); if(!bar) return;
            const st = {success:'background:#d4edda;color:#155724;border:1px solid #c3e6cb',
                        error:'background:#f8d7da;color:#721c24;border:1px solid #f5c6cb',
                        warn:'background:#fff3cd;color:#856404;border:1px solid #ffc107'};
            bar.setAttribute('style', (st[type]||st.success) + ';display:block;padding:10px 16px;border-radius:4px;margin-bottom:10px;');
            bar.textContent = text;
            if (type !== 'error') setTimeout(()=>{ bar.style.display='none'; }, 6000);
        }

        function saveQuote(){
            const quoteId = document.getElementById('quote_id').value;
            const isEdit = quoteId !== '';
            const status = document.getElementById('quote_status_value')?.value || 'draft';
            const ownId = document.getElementById('own_company_select').value;
            if (!ownId) { showMessage('Bitte eigene Firma auswählen.', 'error'); return; }
            const custId = document.getElementById('customer_select').value;
            if (!custId) { showMessage('Bitte Kunde auswählen.', 'error'); return; }
            const number = document.getElementById('quote_number').value;
            if (!number) { showMessage('Bitte Angebotsnummer eingeben.', 'error'); return; }
            const date = document.getElementById('quote_date').value;
            if (!date) { showMessage('Bitte Angebotsdatum wählen.', 'error'); return; }
            const items = [];
            document.querySelectorAll('.invoice-item-row').forEach((row,i)=>{
                const desc = row.querySelector('.item-description').value;
                if (desc) items.push({
                    position: i+1,
                    quantity: parseFloat(row.querySelector('.item-quantity').value)||1,
                    unit: row.querySelector('.item-unit').value,
                    description: desc,
                    unitPrice: parseFloat(row.querySelector('.item-price').value)||0,
                    taxRate: parseFloat(document.getElementById('tax_rate').value)||19
                });
            });
            if (!items.length) { showMessage('Bitte mindestens eine Position hinzufügen.', 'error'); return; }
            const taxRate = parseFloat(document.getElementById('tax_rate').value)||19;
            const data = {
                quoteNumber: number, quoteDate: date,
                customerId: parseInt(custId), ownCompanyId: parseInt(ownId),
                validUntil: document.getElementById('valid_until').value || null,
                introText: document.getElementById('intro_text_editor').innerHTML || null,
                closingText: document.getElementById('closing_text_editor').innerHTML || null,
                taxRate: taxRate/100, status: status, items: items
            };
            if (isEdit) data.quoteId = parseInt(quoteId);
            fetch('/quote/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)})
                .then(r=>r.json())
                .then(d=>{ if(d.success){ window.location.href='/quote?id='+(isEdit?quoteId:String(d.quote_id)); }
                          else showMessage('Fehler beim Speichern: '+(d.error||'?'), 'error'); })
                .catch(e=>showMessage('Fehler: '+e, 'error'));
        }

        function generateQuotePDF(){
            const id = document.getElementById('quote_id').value;
            if (!id) { showMessage('Bitte Angebot zuerst speichern.', 'warn'); return; }
            const exists = document.getElementById('pdf_exists').value === 'true';
            if (exists && !confirm('PDF existiert bereits. Überschreiben?')) return;
            fetch('/quote/pdf_generate?id='+id).then(r=>r.json())
                .then(d=>{ if(d.success){ document.getElementById('pdf_exists').value='true'; showMessage('PDF erstellt: '+d.pdf_path, 'success'); }
                          else showMessage('Fehler: '+(d.error||'?'), 'error'); });
        }
        function setQuoteStatus(id){
            const ns = document.getElementById('statusChangeSelect').value;
            fetch('/quote/status', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({quote_id:id, status:ns})})
                .then(r=>r.json()).then(d=>{ if(d.success) location.reload(); else showMessage('Fehler: '+(d.error||'?'), 'error'); });
        }
        function convertQuote(id){
            if (!confirm('Aus diesem Angebot eine Rechnung erstellen? Das Angebot wird als "Umgewandelt" markiert.')) return;
            const f=document.createElement('form'); f.method='POST'; f.action='/quote/convert';
            const i=document.createElement('input'); i.type='hidden'; i.name='quote_id'; i.value=id;
            f.appendChild(i); document.body.appendChild(f); f.submit();
        }
    </script>
    '''
    return s
