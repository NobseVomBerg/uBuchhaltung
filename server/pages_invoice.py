# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Invoice pages: list and create/edit – extracted from pages.py"""
import datetime
import json
import os
import html as _html
from db import Database
from .pages import (
    Header1, Header2, Header3, Footer, logo_url,
)
from .period import period_filter_widget


def _json_for_script(obj) -> str:
    """json.dumps, aber sicher zum Einbetten in <script>: neutralisiert
    </script> sowie HTML-Sonderzeichen (json.dumps escaped '<' '>' '&' nicht)."""
    return (json.dumps(obj)
            .replace('<', '\\u003c')
            .replace('>', '\\u003e')
            .replace('&', '\\u0026'))

# Invoice status constants
INVOICE_STATUS_COLORS: dict = {
    'draft':           '#888',
    'finalized':       '#0066cc',
    'sent':            '#ff9900',
    'partial_payment': '#e69500',
    'overdue':         '#b71c1c',
    'paid':            '#00aa00',
    'cancelled':       '#cc0000',
}
INVOICE_STATUS_LABELS: dict = {
    'draft':           'Entwurf',
    'finalized':       'Abgeschlossen',
    'sent':            'Versendet',
    'partial_payment': 'Teilzahlung',
    'overdue':         'Überfällig',
    'paid':            'Bezahlt',
    'cancelled':       'Storniert',
}

def document_submenu(active):
    """Gemeinsames Header2-Submenü für Rechnung / Angebot / Mahnwesen.

    active: 'invoice' | 'quote' | 'reminders'
    """
    def part(href, label, key):
        if key == active:
            return f'<span id="ActivePage">{label}</span>'
        return f'<a href="{href}">{label}</a>'
    return ' | '.join([
        part('/invoice', 'Rechnungen', 'invoice'),
        part('/quote', 'Angebote', 'quote'),
        part('/invoice/reminders', 'Mahnwesen', 'reminders'),
    ])


def PageInvoice(db: Database, filters: dict = None, invoice_id=None):
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

    # Status- und Such-Filter wandern nach Header2; der Zeitraum (Von/Bis + Jahr +
    # Monat) wird zentral in Header3 über period_filter_widget angeboten.
    status_options = {
        'all': 'Alle',
        'draft': 'Entwurf',
        'finalized': 'Abgeschlossen',
        'sent': 'Versendet',
        'paid': 'Bezahlt',
        'cancelled': 'Storniert'
    }

    status_dropdown = '<select id="statusFilter" onchange="applyInvoiceFilters()">'
    for value, label in status_options.items():
        selected = 'selected' if status_filter == value or (not status_filter and value == 'all') else ''
        status_dropdown += f'<option value="{value}" {selected}>{label}</option>'
    status_dropdown += '</select>'

    header2_content = (
        '<div class="rowWithObjects">'
        f'<div>{document_submenu("invoice")}</div>'
        f'<div><label>🔍 Suche:</label> <input type="text" id="searchQuery" '
        f'value="{_html.escape(search_query)}" placeholder="RNr. oder Kunde" '
        'onchange="applyInvoiceFilters()" style="width: 200px;"></div>'
        f'<div><label>Status:</label> {status_dropdown}</div>'
        '</div>'
    )
    s += Header2(header2_content)

    # Header3: zentraler Zeitraum-Filter (erhält Status/Suche/ausgewählte Rechnung)
    s += Header3(period_filter_widget(
        date_from, date_to, '/invoice',
        extra_params={'status': status_filter, 'search': search_query,
                      'id': invoice_id if invoice_id else ''}))

    # Statistics summary
    total_count = len(invoices)
    total_sum = sum(inv[36] for inv in invoices if len(inv) > 36 and inv[36])  # SumGross is at index 36
    paid_sum = sum(inv[36] for inv in invoices if len(inv) > 36 and inv[36] and len(inv) > 38 and inv[38] == 'paid')  # Status at 38
    open_sum = sum(inv[36] for inv in invoices if len(inv) > 36 and inv[36] and len(inv) > 38 and inv[38] not in ['paid', 'cancelled'])
    
    s+= '<div class="grid2Cols gridMain"><div class="gridLeftCol" style="order:1">'
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
            inv_number = _html.escape(str(invoice[1] or ''))
            inv_date = invoice[2]
            buyer_name = _html.escape(str(invoice[14] or ''))  # BuyerName is now at index 14 (was 13)
            sum_net = invoice[36]  # SumNet is at index 36 (was 34)
            sum_gross = invoice[38]  # SumGross is at index 38 (was 36)
            status = invoice[40]  # Status is at index 40 (was 38)
            pdf_path = invoice[41] if len(invoice) > 41 else None  # PDFPath is at index 41 (was 39)
            
            # Status color and label from shared module constants
            status_color = INVOICE_STATUS_COLORS.get(status, '#888')
            status_label = INVOICE_STATUS_LABELS.get(status, status)
            
            s+= f"<tr>"
            s+= f"<td>{inv_number}</td>"
            s+= f"<td>{inv_date}</td>"
            s+= f"<td>{buyer_name[:30]}</td>"
            s+= f"<td style='text-align: right;'>{sum_net:.2f} €</td>"
            s+= f"<td style='text-align: right;'><strong>{sum_gross:.2f} €</strong></td>"
            s+= f"<td style='color: {status_color};'><strong>{status_label}</strong></td>"
            action_icon  = '&#9998;'    if status == 'draft' else '&#128065;'
            action_title = 'Bearbeiten' if status == 'draft' else 'Ansehen'
            s+= f"<td>"
            # PDF button - check if PDF actually exists in filesystem
            pdf_exists = "true" if (pdf_path and os.path.exists(pdf_path)) else "false"
            s+= f'<a href="javascript:void(0);" onclick="handlePDF({inv_id}, {pdf_exists})" class="action-icon" title="PDF">&#128196;</a> '
            s+= f"<a href='/invoice?id={inv_id}' class='action-icon' title='{action_title}'>{action_icon}</a>"
            s+= f"</td>"
            s+= f"</tr>"
        
        s+= "</table>"
    
    s+= '''
    <script>
        function handlePDF(invoiceId, pdfExists) {
            if (pdfExists) {
                appConfirm('PDF-Datei existiert bereits. Überschreiben und neu generieren?', function() {
                    generatePDFInFilesystem(invoiceId);
                });
            } else {
                generatePDFInFilesystem(invoiceId);
            }
        }

        function generatePDFInFilesystem(invoiceId) {
            // Tab im Klick-Kontext oeffnen (kein Popup-Blocker), spaeter befuellen
            const viewer = window.open('about:blank', '_blank');
            fetch('/invoice/pdf_generate?id=' + invoiceId)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        appMsg('PDF erstellt: ' + data.pdf_path, 'success');
                        if (viewer) viewer.location = '/invoice/pdf_download?id=' + invoiceId + '&inline=1';
                    } else {
                        if (viewer) viewer.close();
                        appMsg('Fehler: ' + (data.error || 'Unbekannter Fehler'), 'error');
                    }
                })
                .catch(err => {
                    if (viewer) viewer.close();
                    appMsg('Fehler: ' + err.message, 'error');
                });
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
            
            // Preserve currently selected invoice in grid view
            const currentId = new URLSearchParams(window.location.search).get('id');
            if (currentId) params.append('id', currentId);
            // Reload page with filters
            window.location.href = '/invoice' + (params.toString() ? '?' + params.toString() : '');
        }
        
        function resetInvoiceFilters() {
            window.location.href = '/invoice';
        }
    </script>
    '''
    s+= '</div><!-- Ende gridLeftCol -->'
    s+= '<div class="gridRightCol" style="order:2; min-width:820px;">'
    s+= _invoice_form_html(db, invoice_id)
    s+= '</div><!-- Ende gridRightCol --></div><!-- Ende grid2Cols -->'
    s+= Footer()
    return s

def _invoice_form_html(db: Database, invoice_id=None):
    """Generate invoice form HTML block (no page wrapper)."""
    import datetime
    import json
    current_year = datetime.datetime.now().year

    # Load existing invoice if invoice_id is provided
    existing_invoice = None
    existing_items = []
    existing_payments = []
    if invoice_id:
        existing_invoice = db.get_invoice_by_id(invoice_id)
        if existing_invoice:
            existing_items = db.get_invoice_items(invoice_id)
            existing_payments = db.get_invoice_payments(invoice_id)

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
        invoice_number = _html.escape(str(existing_invoice[1] or ''))
        invoice_date = existing_invoice[2]
        selected_own_company_id = existing_invoice[3]
        selected_customer_id = existing_invoice[13]  # CustomerId is now at index 13 (was 12)
        invoice_status = existing_invoice[40]  # Status is now at index 40 (was 37)
        pdf_path = existing_invoice[41] if len(existing_invoice) > 41 else None  # PDFPath is now at index 41 (was 39)
        # Steuersatz aus der gespeicherten Rechnung vorbelegen. Gespeichert als
        # Dezimal (0.19) bzw. 0; Sentinel -1 = kein USt-Ausweis (Kleinunternehmer).
        _r = existing_invoice[35] if len(existing_invoice) > 35 and existing_invoice[35] is not None else None
        show_tax = not (_r is not None and float(_r) < 0)
        if _r is None or float(_r) < 0:
            tax_pct = 19
        else:
            tax_pct = float(_r) * 100 if 0 <= float(_r) <= 1 else float(_r)
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
        tax_pct = 19
        show_tax = True
        page_title = "Neue Rechnung erstellen"
        is_edit_mode = False
    
    s = f'<input type="hidden" id="invoice_id" value="{invoice_id or ""}">'
    s += f'<input type="hidden" id="is_edit_mode" value="{str(is_edit_mode).lower()}">'
    s += f'<input type="hidden" id="invoice_status_value" value="{invoice_status}">'
    # Check if PDF actually exists in filesystem
    import os
    pdf_file_exists = bool(pdf_path and os.path.exists(pdf_path))
    s += f'<input type="hidden" id="pdf_exists" value="{str(pdf_file_exists).lower()}">'
    s += '<div id="invoice_msg" class="no-pdf" style="display:none; margin-bottom:10px;"></div>'
    if is_edit_mode:
        payment_count = len(existing_payments)
        status_color  = INVOICE_STATUS_COLORS.get(invoice_status, '#888')
        status_label  = INVOICE_STATUS_LABELS.get(invoice_status, invoice_status)
        status_options_html = ''.join(
            '<option value="{k}"{sel}{dis}>{v}</option>'.format(
                k=k, v=v,
                sel=' selected' if k == invoice_status else '',
                dis=' disabled' if k == 'paid' and payment_count == 0 else '')
            for k, v in INVOICE_STATUS_LABELS.items()
        )
        s += '<div class="rectRounded no-pdf">' #right Column Headline, Status and Buttons
        s += f'<h2>{page_title}</h2>'
        s += f'''
        <table width="100%">
            <tr>
                <td>Status:</td>
                <td><div class="rowWithObjects">
                    '''
        if invoice_status not in ('draft', 'finalized'):
            s += '<div class="rectRounded bg-red">'
        s += f'<strong class="coloredButtonNoClick btn-smNoClick bg-back" style="color:{status_color}; hover:none;">{status_label}</strong>'
        if invoice_status not in ('draft', 'finalized'):
            s += ' &nbsp; &nbsp; &#9888; Nicht im Entwurfsstatus! &Auml;nderungen k&ouml;nnen trotzdem gespeichert werden.</div>'
        s += f'''
                </div></td>
            </tr>
            <tr>
                <td>Neuer Status:</td>
                <td><div class="rowWithObjects">
                    <select id="statusChangeSelect">{status_options_html}</select>
                    <button onclick="setInvoiceStatus({invoice_id})" class="coloredButton btn-sm bg-blue">&#9888; Status setzen</button>
                </div></td>
            </tr>
            <tr>
                <td>Rechnung:</td>
                <td><div class="rowWithObjects">
                    <button onclick="saveInvoice()" class="coloredButton btn-sm bg-green">💾 Speichern</button>
                    <button onclick="window.location.href='/invoice'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>
                    <button onclick="generatePDF()" class="coloredButton btn-sm bg-blue">📄 PDF + E-Rechnung</button>
                </div></td>
            </tr>
        </table>
    </div>
<script>
const _payCount = {payment_count};
function setInvoiceStatus(invId) {{
  var ns = document.getElementById('statusChangeSelect').value;
  if (ns === 'paid' && _payCount === 0) {{
    showMessage('Bezahlt nur m\u00f6glich, wenn mindestens eine Zahlung verkn\u00fcpft ist.', 'error');
    return;
  }}
  fetch('/invoice/status', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{invoice_id: invId, status: ns}})}}).then(r => r.json())
  .then(data => {{ if (data.success) location.reload(); else showMessage('Fehler: ' + (data.error || '?'), 'error'); }});
}}
</script>'''
    else:
        s += '<div class="rectRounded no-pdf">'
        s += f'<h2>{page_title}</h2>'
        s += '''
        <table width="100%">
            <tr>
                <td>Rechnung:</td>
                <td><div class="rowWithObjects">
                    <button onclick="saveInvoice()" class="coloredButton btn-sm bg-green">💾 Speichern</button>
                    <button onclick="window.location.href=\'/invoice\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>
                </div></td>
            </tr>
        </table>
    </div>'''
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
        own_name = _html.escape(str(own[3])) if own[3] else f"ID {own[0]}"  # display_name at index 3
        selected = 'selected' if (existing_invoice and own[0] == selected_own_company_id) or (not existing_invoice and own_contact and own[0] == own_contact[0]) else ''
        s += f'<option value="{own[0]}" {selected}>{own_name}</option>'
    
    s += '''
                </select>
            <div class="invoice-sender-line" id="sender_line">
    '''
    
    if own_contact:
        sender_street = _html.escape(str(own_contact[5] or ''))
        sender_postal = _html.escape(str(own_contact[6] or ''))
        sender_city   = _html.escape(str(own_contact[7] or ''))
        sender_name   = _html.escape(str(own_contact[3] or ''))  # display_name
        s += f'{sender_name} · {sender_street} · {sender_postal} {sender_city}'
    else:
        s += 'Eigene Adresse in Kontakte anlegen (Typ: own)'
    
    s += '''            </div>
                <select id="customer_select" onchange="updateCustomerAddress()" style="margin-bottom: 10px; width: 100%;" class="no-pdf">
                    <option value="">-- Kunde auswählen --</option>
    '''
    
    for customer in customers:
        cust_name = _html.escape(str(customer[3])) if customer[3] else f"ID {customer[0]}"  # display_name at index 3
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
                <tr class="no-pdf">
                    <td colspan="7" style="text-align: right; border: none;">
                        <label><input type="checkbox" id="show_tax" __SHOW_TAX_CHECKED__ onchange="calculateTotals()"> Umsatzsteuer ausweisen</label>
                        <small style="color:#888;">(aus = Kleinunternehmer §&nbsp;19 UStG; Hinweis ggf. unten im Text)</small>
                    </td>
                </tr>
                <tr class="totals-row" id="tax_row">
                    <td colspan="4" style="text-align: right; border: none;">Mehrwertsteuer</td>
                    <td style="text-align: right; border: none;"><input type="number" id="tax_rate" value="__TAX_PCT__" min="0" max="100" step="0.1" style="width: 50px;">% auf <span id="tax_base">0,00</span> € netto:</td>
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
            <span style="position:relative; display:inline-block;">
                <input type="text" id="articleComboInput" placeholder="Artikel suchen …"
                       style="width:280px; padding:4px 8px;" autocomplete="off"
                       oninput="filterArticles(this.value)" onfocus="filterArticles(this.value)"
                       onblur="setTimeout(()=>document.getElementById('articleDropdown').style.display='none',150)">
                <div id="articleDropdown" style="display:none; position:absolute; top:100%; left:0;
                     width:440px; background:#fff; border:1px solid #ccc; border-radius:4px;
                     max-height:280px; overflow-y:auto; z-index:999;
                     box-shadow:0 2px 8px rgba(0,0,0,0.18);"></div>
            </span>
        </div>
    '''

    s += '''
        
        <div class="invoice-payment-terms">'''
    
    # Payment terms text
    payment_terms_text = _html.escape(str(existing_invoice[26])) if existing_invoice and existing_invoice[26] else 'Bitte überweisen Sie den Gesamtbetrag ohne jeden Abzug unter Angabe der Rechnungsnummer innerhalb von 14 Tagen ab Rechnungsdatum auf das unten angegebene Konto. Vielen Dank.'
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
        addr_line1_html = f'{_html.escape(str(own_contact[25]))}<br>' if (len(own_contact) > 25 and own_contact[25]) else ''
        s += f'''                        {_html.escape(str(own_contact[3])) if own_contact[3] else 'Firma'}<br>
                        {addr_line1_html}                        {_html.escape(str(own_contact[5])) if own_contact[5] else 'Straße'}<br>
                        {_html.escape(str(own_contact[6])) if own_contact[6] else 'PLZ'} {_html.escape(str(own_contact[7])) if own_contact[7] else 'Ort'}'''
    else:
        s += '                        <em>Eigene Firmendaten in Kontakte anlegen</em>'
    
    s += '''                        </div>
                    </td>
                    <td class="footer-col-center">
                        <strong>Kontakt</strong><br>
                        <div id="footer_company_contact">
    '''
    
    if own_contact:
        s += f'''                        <span class="footer-label">Tel</span> {_html.escape(str(own_contact[10])) if own_contact[10] else '-'}<br>
                        <span class="footer-label">E-Mail</span> {_html.escape(str(own_contact[9])) if own_contact[9] else '-'}<br>
                        <span class="footer-label">UStIdNr</span> {_html.escape(str(own_contact[11])) if own_contact[11] else '-'}'''
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
            s += f'<option value="{account[0]}" {selected}>{_html.escape(str(account[1] or ""))}</option>'
    
    s += '''                        </select>
                        <div id="bank_details" style="margin-top: 5px;">
                            
                        </div>
                    </td>
                </tr>
            </table>
        </div>
    </div>
    
    <!-- XRechnung / E-Rechnung Zusatzdaten (optional) -->
    <div class="rectRounded no-pdf">
        <button type="button" onclick="toggleXRechnungFields()" class="coloredButton">⚙️ XRechnung / E-Rechnung Zusatzdaten (optional) <span id="xrechnung_toggle">&#9654;</span></button>
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
            'logo':    logo_url(own[13]) if len(own) > 13 and own[13] else ''
        }
    
    s += _json_for_script(own_companies_dict)
    
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
    
    s += _json_for_script(customers_dict)
    
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
    
    s += _json_for_script(banks_dict)
    
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
            'price': float(article[3]) if article[3] is not None else 0,  # Decimal -> JSON-fähig
            'taxRate': article[4] if article[4] is not None else 19,  # 0% nicht zu 19% verfälschen
            'description': article[5] or ''
        }
    
    s += _json_for_script(articles_dict)
    
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
                
                // Update sender line (OHNE Zusatzzeile – aus Platzgründen)
                const displayName = company.company || company.name;
                senderLine.textContent = displayName + ' · ' + company.street + ' · ' + company.postal + ' ' + company.city;

                // Update footer address (MIT Zusatzzeile, falls vorhanden)
                let addressHtml = displayName + '<br>';
                if (company.address_line1) addressHtml += company.address_line1 + '<br>';
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
                if (customer.address_line1) address += customer.address_line1 + '\\n';
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
                toggle.textContent = '\u25BC';
            } else {
                fields.style.display = 'none';
                toggle.textContent = '\u25B6';
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
        
        // Artikel-Combobox
        function filterArticles(query) {
            const dropdown = document.getElementById('articleDropdown');
            const q = query.toLowerCase();
            const entries = Object.entries(articlesData).filter(([id, a]) =>
                !q || a.name.toLowerCase().includes(q)
            );
            if (!entries.length) { dropdown.style.display = 'none'; return; }
            dropdown.innerHTML = entries.map(([id, a]) =>
                '<div class="article-option" onmousedown="selectArticle(' + id + ')">' +
                '<strong>' + a.name + '</strong>' +
                '<span class="article-option-meta">' + a.unit + ' · ' + a.price.toFixed(2) + ' € · ' + a.taxRate + '%</span>' +
                '</div>'
            ).join('');
            dropdown.style.display = 'block';
        }
        function selectArticle(articleId) {
            addArticleRow(articleId);
            document.getElementById('articleComboInput').value = '';
            document.getElementById('articleDropdown').style.display = 'none';
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
            const safeName = displayName.replace(/"/g, '&quot;');

            newRow.innerHTML = `
                <td>${rowCounter}</td>
                <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width: 60px;"></td>
                <td><input type="text" class="item-unit" value="${article.unit}" style="width: 60px;"></td>
                <td><input type="text" class="item-description" value="${safeName}" style="width: 100%;"></td>
                <td><input type="number" class="item-price" value="${article.price}" min="0" step="0.01" style="width: 80px;"> €</td>
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

            // USt nur ausweisen, wenn Checkbox aktiv (Kleinunternehmer §19: keine Zeile)
            const showTax = document.getElementById('show_tax').checked;
            const taxRow = document.getElementById('tax_row');
            if (taxRow) taxRow.style.display = showTax ? '' : 'none';

            // Calculate tax
            const taxRate = showTax ? (parseFloat(document.getElementById('tax_rate').value) || 0) : 0;
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
        
        // Notification helpers
        function showMessage(text, type) {
            type = type || 'success';
            var bar = document.getElementById('invoice_msg');
            if (!bar) return;
            var styles = {
                success: 'background:#d4edda;color:#155724;border:1px solid #c3e6cb',
                error:   'background:#f8d7da;color:#721c24;border:1px solid #f5c6cb',
                warn:    'background:#fff3cd;color:#856404;border:1px solid #ffc107',
                info:    'background:#d1ecf1;color:#0c5460;border:1px solid #bee5eb'
            };
            bar.setAttribute('style', (styles[type] || styles.info)
                + ';display:block;padding:10px 16px;border-radius:4px;margin-bottom:10px;font-size:14px;');
            bar.textContent = text;
            if (type !== 'error') setTimeout(function() { bar.style.display = 'none'; }, 6000);
        }
        function showConfirm(text, onOk) { appConfirm(text, onOk); }
        
        // Save invoice to database
        function saveInvoice() {
            // Check if we're editing an existing invoice
            const invoiceId = document.getElementById('invoice_id').value;
            const isEdit = invoiceId !== '';
            const currentStatus = document.getElementById('invoice_status_value')?.value || 'draft';
            
            // Get selected own company ID
            const ownCompanyId = document.getElementById('own_company_select').value;
            if (!ownCompanyId) {
                showMessage('Bitte wählen Sie eine eigene Firma aus.', 'error');
                return;
            }
            
            // Get selected customer
            const customerId = document.getElementById('customer_select').value;
            if (!customerId) {
                showMessage('Bitte wählen Sie einen Kunden aus.', 'error');
                return;
            }
            
            // Get invoice number
            const invoiceNumber = document.getElementById('invoice_number').value;
            if (!invoiceNumber) {
                showMessage('Bitte geben Sie eine Rechnungsnummer ein.', 'error');
                return;
            }
            
            // Get invoice date
            const invoiceDate = document.getElementById('invoice_date').value;
            if (!invoiceDate) {
                showMessage('Bitte wählen Sie ein Rechnungsdatum.', 'error');
                return;
            }
            
            // Steuersatz NaN-sicher lesen (0% darf nicht zu 19% werden).
            // Kleinunternehmer (§19): keine USt -> effektiver Satz 0, Flag showTax=false.
            const showTax = document.getElementById('show_tax').checked;
            const _tr = parseFloat(document.getElementById('tax_rate').value);
            const taxRate = showTax ? (isNaN(_tr) ? 19 : _tr) : 0;

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
                        taxRate: taxRate
                    });
                }
            });
            
            if (items.length === 0) {
                showMessage('Bitte fügen Sie mindestens eine Position hinzu.', 'error');
                return;
            }
            
            // Get bank account ID
            const bankAccountId = document.getElementById('bank_account_select').value || null;
            
            // Calculate amounts (replace comma with dot for German number format)
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
                showTax: showTax,        // false = Kleinunternehmer (§19), keine USt-Zeile
                taxAmount: taxAmount,
                grossAmount: grossAmount,
                currency: 'EUR',
                status: currentStatus,
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
                    // Stay in grid view with this invoice selected
                    const redirectId = isEdit ? invoiceId : String(data.invoice_id);
                    window.location.href = '/invoice?id=' + redirectId;
                } else {
                    showMessage('Fehler beim Speichern: ' + (data.error || 'Unbekannter Fehler'), 'error');
                }
            })
            .catch(err => {
                showMessage('Fehler beim Speichern: ' + err, 'error');
                console.error('Save error:', err);
            });
        }
        
        // Generate PDF (and optionally XML)
        function generatePDF() {
            const invoiceId = document.getElementById('invoice_id').value;
            if (!invoiceId) {
                showMessage('Bitte speichern Sie die Rechnung zuerst, bevor Sie ein PDF erstellen.', 'warn');
                return;
            }
            const pdfExists = document.getElementById('pdf_exists').value === 'true';
            if (pdfExists) {
                showConfirm('PDF-Datei existiert bereits. Überschreiben und neu generieren?', function() {
                    _runPDFGeneration(invoiceId);
                });
            } else {
                _runPDFGeneration(invoiceId);
            }
        }
        function _runPDFGeneration(invoiceId) {
            // Tab im Klick-Kontext oeffnen (kein Popup-Blocker), spaeter befuellen
            const viewer = window.open('about:blank', '_blank');
            fetch('/invoice/pdf_generate?id=' + invoiceId)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('pdf_exists').value = 'true';
                        showMessage('PDF erstellt: ' + data.pdf_path, 'success');
                        // PDF inline im neuen Tab anzeigen
                        if (viewer) viewer.location = '/invoice/pdf_download?id=' + invoiceId + '&inline=1';
                        // Auch XML (E-Rechnung) downloaden wenn XRechnung-Felder gefüllt
                        const buyerRouteId = document.getElementById('buyer_route_id')?.value || '';
                        const orderNumber = document.getElementById('order_number')?.value || '';
                        if (buyerRouteId || orderNumber) {
                            window.open('/invoice/xml?id=' + invoiceId, '_blank');
                        }
                    } else {
                        if (viewer) viewer.close();
                        showMessage('Fehler beim Erstellen der PDF: ' + (data.error || 'Unbekannter Fehler'), 'error');
                    }
                })
                .catch(function(err) {
                    if (viewer) viewer.close();
                    showMessage('Fehler: ' + err.message, 'error');
                });
        }
    </script>
    '''

    # Payment history section (edit mode only)
    if is_edit_mode and invoice_id:
        amount_due = existing_invoice[39] if existing_invoice else 0
        s += f'''
    <div style="margin: 30px 0; padding: 20px; border: 1px solid #ddd; border-radius: 4px;">
        <h3>Zahlungsverknüpfungen</h3>
'''
        if existing_payments:
            total_paid = sum(p[3] for p in existing_payments)
            s += '''        <table style="width:100%; border-collapse:collapse;">
            <tr style="background:#f5f5f5;">
                <th style="padding:6px 8px; text-align:left;">Datum</th>
                <th style="padding:6px 8px; text-align:left;">Buchung</th>
                <th style="padding:6px 8px; text-align:right;">Betrag</th>
                <th style="padding:6px 8px;"></th>
            </tr>
'''
            for p in existing_payments:
                pid, _, booking_id, amount, pay_date, notes, booking_ref = p
                s += f'''            <tr>
                <td style="padding:6px 8px;">{pay_date or "–"}</td>
                <td style="padding:6px 8px;">#{booking_id} {booking_ref}</td>
                <td style="padding:6px 8px; text-align:right;">{amount:.2f} €</td>
                <td style="padding:6px 8px; text-align:center;">
                    <a href="javascript:void(0);" onclick="deletePayment({pid})" style="color:#c00;">✕ Entfernen</a>
                </td>
            </tr>
'''
            s += f'''            <tr style="font-weight:bold; border-top:2px solid #ccc;">
                <td colspan="2" style="padding:6px 8px;">Gezahlt gesamt</td>
                <td style="padding:6px 8px; text-align:right;">{total_paid:.2f} €</td>
                <td></td>
            </tr>
            <tr style="font-weight:bold;">
                <td colspan="2" style="padding:6px 8px;">Noch offen</td>
                <td style="padding:6px 8px; text-align:right; color:{"#c00" if (amount_due or 0) > 0.01 else "#2a2"};">{(amount_due or 0):.2f} €</td>
                <td></td>
            </tr>
        </table>
'''
        else:
            s += '        <p><em>Keine Zahlungen verknüpft.</em></p>\n'

        s += f'''    </div>
    <script>
        function deletePayment(paymentId) {{
            showConfirm('Zahlungsverkn\u00fcpfung wirklich entfernen?', function() {{
                fetch('/invoice/delete-payment', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{payment_id: paymentId}})
                }})
                .then(r => r.json())
                .then(data => {{
                    if (data.success) {{ location.reload(); }}
                    else {{ showMessage('Fehler: ' + (data.error || 'Unbekannt'), 'error'); }}
                }});
            }});
        }}
    </script>
'''

    # Steuersatz-Feld + USt-Checkbox serverseitig vorbelegen (Platzhalter im Template)
    s = s.replace('__TAX_PCT__', f'{tax_pct:g}')
    s = s.replace('__SHOW_TAX_CHECKED__', 'checked' if show_tax else '')
    return s

