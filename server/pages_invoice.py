# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Invoice pages: list and create/edit – extracted from pages.py"""
import datetime
import json
import html as _html
import re
from urllib.parse import quote
from db import Database
from db.invoices import InvoiceCol
from db.invoice_lock import (CLOSED, EDITABLE, NUMBER_KEPT_MESSAGE, STATUS_LABELS, allowed_targets,
                             as_document_version, buyer_snapshot, status_group)
from db.invoice_payments import PAYABLE_STATUSES
from db.invoice_versions import DELIVERY_DATE_MESSAGE, PROTOCOL_LABELS, local_date, local_datetime
from money import fmt_eur
from export.einvoice_model import unit_display
from export.invoice_files import stored_invoice_path, version_files
from .pages import (
    Header1, Header2, Header3, Footer, logo_url, script_json,
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
INVOICE_STATUS_LABELS: dict = STATUS_LABELS     # eine Quelle: db/invoice_lock.py


# Löschen eines Entwurfs: Rückfrage, und – war die Rechnung schon einmal versendet – der Hinweis, was bleibt
DELETE_DRAFT_QUESTION = 'Entwurf wirklich löschen?'
DELETE_SENT_BEFORE_NOTE = ('Diese Rechnung war bereits versendet. Die versendete Fassung bleibt im Ordner '
                           '„Fassungen“ erhalten, die Nummer wird nicht neu vergeben.')

# Versendete Rechnung: was änderbar ist und was eine Änderung bewirkt (Berichtigen, db/invoice_versions.py)
SENT_HINT = ('Diese Rechnung ist versendet. Änderbar sind Zahlungshinweis, Anschrift, Bestellnummer, Leitweg-ID '
             'und Leistungsdatum. Jede Änderung wird protokolliert; die bisherige Fassung bleibt als Datei '
             'erhalten, und es entsteht eine neue Fassung, die dem Kunden erneut zugehen muss.')
CORRECT_QUESTION = ('Rechnung berichtigen? Die bisherige Fassung bleibt als Datei erhalten, die Änderung wird '
                    'protokolliert, und die neue Fassung muss dem Kunden erneut zugehen.')


def _address_text(snapshot, separator):
    """Anschrift aus ``buyer_snapshot``-Feldern als Text (Firma oder Name, Straße, PLZ Ort, Land
    außerhalb Deutschlands), getrennt durch ``separator``."""
    place = f"{snapshot.get('buyer_postal_code') or ''} {snapshot.get('buyer_city') or ''}".strip()
    country = snapshot.get('buyer_country') or 'DE'
    parts = [snapshot.get('buyer_company') or snapshot.get('buyer_name') or '',
             snapshot.get('buyer_street') or '', place, country if country != 'DE' else '']
    return separator.join(part for part in parts if part)


def delete_question(sent_before):
    return f'{DELETE_DRAFT_QUESTION} {DELETE_SENT_BEFORE_NOTE}' if sent_before else DELETE_DRAFT_QUESTION


def normalize_status(status):
    """Alt-Wert 'partial' (vor todo #2 geschrieben) auf den Kanon abbilden."""
    return 'partial_payment' if status == 'partial' else status


def revenue_coa_choice(db, tax_rate, selected=None):
    """Pflichtfeld „Erlöskonto“ für eine Rechnungszahlung – nur, wenn für den Steuersatz der
    Rechnung kein Erlöskonto hinterlegt ist (``Database.revenue_coa_choice_needed``, z. B. 5 %
    oder 16 %), sonst ``None``: Dann ergibt sich die Kontierung aus der Rechnung.

    Returns ``(select_html, hint_html)``. Vorgewählt ist ``selected`` (wenn es ein wählbares
    Erlöskonto ist), sonst der Vorschlag ``Database.suggest_revenue_coa`` – eine Vorauswahl, keine
    Entscheidung; ohne beides steht „bitte wählen“ da. Alle Werte sind maskiert."""
    if tax_rate is None or not db.revenue_coa_choice_needed(tax_rate):
        return None
    accounts = db.get_revenue_coas()
    if selected not in {coa_id for coa_id, _number, _name in accounts}:
        selected = db.suggest_revenue_coa(tax_rate)
    options = '<option value="">— bitte wählen —</option>'
    for coa_id, number, name in accounts:
        mark = ' selected' if coa_id == selected else ''
        label = _html.escape(f'{number} {name}')
        options += f'<option value="{int(coa_id)}"{mark}>{label}</option>'
    percent = tax_rate * 100 if tax_rate <= 1 else tax_rate
    percent_text = f'{percent:g}'.replace('.', ',')
    hint = (f'Für {percent_text} % ist kein Erlöskonto hinterlegt. Die Wahl gilt künftig als '
            'Vorgabe für diesen Steuersatz.')
    return (f'<select name="revenue_coa_id" id="revenueCoa" required>{options}</select>',
            _html.escape(hint))


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


def PageInvoice(db: Database, filters: dict = None, invoice_id=None, copy_from=None,
                payment_error=None, err=None):
    """Generate invoice list page with search and filter

    copy_from: ID einer Quellrechnung – das Formular öffnet sich dann als
    neue Rechnung mit deren Inhalten (todo #4).
    payment_error: Grund, warum eine Zahlung aus der Buchungsmaske nicht zugeordnet werden
    konnte (Klartext, wird maskiert ausgegeben).
    err: Grund, warum eine Aktion abgelehnt wurde (z. B. das Löschen einer versendeten Rechnung) –
    Klartext, kommt als Meldung auf die Seite.
    """
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
                   if search_lower in str(inv[InvoiceCol.NUMBER] or '').lower()
                   or search_lower in str(inv[InvoiceCol.BUYER_COMPANY] or '').lower()
                   or search_lower in str(inv[InvoiceCol.BUYER_NAME] or '').lower()]
    
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
    # Spalten laut Invoices-Schema: 38 = SumGross, 39 = AmountDue, 40 = Status
    def _gross(inv):
        return inv[38] or 0

    def _due(inv):
        return inv[39] if inv[39] is not None else _gross(inv)

    total_count = len(invoices)
    total_sum = sum(_gross(inv) for inv in invoices)
    paid_sum = sum(_gross(inv) - _due(inv) for inv in invoices if inv[40] != 'cancelled')
    open_sum = sum(_due(inv) for inv in invoices if inv[40] not in ('paid', 'cancelled'))

    s+= '<div class="grid2Cols gridMain"><div class="gridLeftCol" style="order:1">'
    s+= f'''
    <div class="rectRounded">
        <strong>Statistik:</strong> 
        {total_count} Rechnung(en) | 
        Gesamtsumme: {fmt_eur(total_sum)} | 
        Bezahlt: {fmt_eur(paid_sum)} | 
        Offen: {fmt_eur(open_sum)}
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
            inv_date = _html.escape(str(invoice[2] or ''))
            buyer_name = _html.escape(str(invoice[14] or '')[:30])  # BuyerName is now at index 14 (was 13)
            sum_net = invoice[36]  # SumNet is at index 36 (was 34)
            sum_gross = invoice[38]  # SumGross is at index 38 (was 36)
            status = normalize_status(invoice[InvoiceCol.STATUS])
            
            # Status color and label from shared module constants
            status_color = INVOICE_STATUS_COLORS.get(status, '#888')
            status_label = _html.escape(str(INVOICE_STATUS_LABELS.get(status, status)))
            
            s+= f"<tr>"
            s+= f"<td>{inv_number}</td>"
            s+= f"<td>{inv_date}</td>"
            s+= f"<td>{buyer_name}</td>"
            s+= f"<td style='text-align: right;'>{fmt_eur(sum_net)}</td>"
            s+= f"<td style='text-align: right;'><strong>{fmt_eur(sum_gross)}</strong></td>"
            s+= f"<td style='color: {status_color};'><strong>{status_label}</strong></td>"
            action_icon  = '&#9998;'    if status == 'draft' else '&#128065;'
            action_title = 'Bearbeiten' if status == 'draft' else 'Ansehen'
            s+= f"<td>"
            # PDF-Symbol laedt nur herunter, was abgelegt ist (todo #3) – gefragt wird dasselbe
            # wie beim Download (geprüfter Pfad mit Fallback auf das Namensschema)
            has_pdf = "true" if stored_invoice_path(db, inv_id, 'pdf', invoice)[0] else "false"
            has_xml = "true" if stored_invoice_path(db, inv_id, 'xml', invoice)[0] else "false"
            pdf_title = 'PDF herunterladen' if has_pdf == "true" else 'Kein PDF vorhanden'
            s+= (f'<a href="javascript:void(0);" onclick="downloadInvoiceDocs({inv_id}, '
                 f'{has_pdf}, {has_xml})" class="action-icon" title="{pdf_title}">&#128196;</a> ')
            s+= f"<a href='/invoice?id={inv_id}' class='action-icon' title='{action_title}'>{action_icon}</a>"
            if status == 'draft':
                sent_before = 'true' if db.was_sent_before(inv_id) else 'false'
                s+= (f' <a href="javascript:void(0);" onclick="deleteInvoiceDraft({int(inv_id)}, {sent_before})" '
                     'class="action-icon delete-icon" title="Entwurf löschen">&#128465;</a>')
            s+= f"</td>"
            s+= f"</tr>"
        
        s+= "</table>"
    
    s+= '''
    <script>
        // Vorhandene Dokumente herunterladen - hier wird nichts neu erzeugt
        // (todo #3). Erzeugt wird im Formular bzw. automatisch beim Versenden.
        function downloadInvoiceDocs(invoiceId, hasPdf, hasXml) {
            if (!hasPdf && !hasXml) {
                appMsg('Kein PDF vorhanden - bitte in der Rechnung erzeugen.', 'error');
                return;
            }
            if (hasPdf) appDownload('/invoice/pdf_download?id=' + invoiceId);
            if (hasXml) appDownload('/invoice/xml_download?id=' + invoiceId);
        }
        
        // Löschen gibt es nur für Entwürfe (POST, nach Rückfrage); war die Rechnung schon einmal versendet,
        // sagt die Rückfrage, was bleibt
        function deleteInvoiceDraft(invoiceId, sentBefore) {
            appConfirmPost('/invoice/delete?id=' + invoiceId,
                           sentBefore ? __DELETE_SENT_BEFORE__ : __DELETE_QUESTION__);
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
    '''.replace('__DELETE_QUESTION__', script_json(DELETE_DRAFT_QUESTION)
                ).replace('__DELETE_SENT_BEFORE__', script_json(delete_question(True)))
    s+= '</div><!-- Ende gridLeftCol -->'
    s+= '<div class="gridRightCol" style="order:2; min-width:820px;">'
    s+= _invoice_form_html(db, invoice_id, copy_from, payment_error)
    s+= '</div><!-- Ende gridRightCol --></div><!-- Ende grid2Cols -->'
    if err:
        s += ('<script>document.addEventListener("DOMContentLoaded",function(){'
              'appMsg(' + script_json(err) + ',"error");});</script>')
    s+= Footer()
    return s

def _invoice_form_html(db: Database, invoice_id=None, copy_from=None, payment_error=None):
    """Generate invoice form HTML block (no page wrapper).

    copy_from: ID einer Quellrechnung. Deren Inhalte füllen das Formular, es
    bleibt aber eine *neue* Rechnung – frisches Datum, frische Nummer, Entwurf
    (todo #4). payment_error: siehe PageInvoice.
    """
    import datetime
    import json
    current_year = datetime.datetime.now().year
    today_iso = datetime.date.today().isoformat()

    # Load existing invoice if invoice_id is provided
    existing_invoice = None
    existing_items = []
    existing_payments = []
    is_copy = bool(copy_from) and not invoice_id
    source_id = invoice_id or (copy_from if is_copy else None)
    if source_id:
        # Nur Rechnungen: die ID eines Angebots zeigt hier kein fremdes Dokument im falschen
        # Formular, sondern das leere Formular für eine neue Rechnung
        existing_invoice = db.get_document(source_id, 'invoice')
        if existing_invoice:
            existing_items = db.get_invoice_items(source_id)
            # Zahlungen gehören zur Quellrechnung und werden nicht mitkopiert
            existing_payments = [] if is_copy else db.get_invoice_payments(source_id)
        else:
            is_copy = False

    # Get company data (own contact)
    own_contacts = db.fetch_contacts(contact_type='own')
    own_contact = own_contacts[0] if own_contacts else None
    
    # Get customers for selection
    customers = db.fetch_contacts(contact_type='customer')
    
    # Get bank accounts for selection
    accounts = db.fetch_accounts()
    
    # Get only active articles for selection in invoices
    articles = db.fetch_articles(active_only=True)
    
    # Steuersatz der Quelle (Bearbeiten wie Kopieren): gespeichert als Dezimal
    # (0.19) bzw. 0; Sentinel -1 = kein USt-Ausweis (Kleinunternehmer).
    tax_pct, show_tax = 19, True
    if existing_invoice:
        _r = (existing_invoice[35] if len(existing_invoice) > 35
              and existing_invoice[35] is not None else None)
        show_tax = not (_r is not None and float(_r) < 0)
        if _r is not None and float(_r) >= 0:
            tax_pct = float(_r) * 100 if 0 <= float(_r) <= 1 else float(_r)

    # Determine invoice number and initial values
    number_hint = False
    if existing_invoice and not is_copy:
        # Editing existing invoice - use existing data
        # existing_invoice structure: ID=0, InvoiceNumber=1, InvoiceDate=2, OwnCompanyId=3, SellerName=4, SellerCompany=5, ..., CustomerId=13, BuyerName=14, BuyerCompany=15, ..., Status=40, PDFPath=41
        invoice_number = str(existing_invoice[1] or '')
        invoice_date = existing_invoice[2]
        selected_own_company_id = existing_invoice[3]
        selected_customer_id = existing_invoice[13]  # CustomerId is now at index 13 (was 12)
        invoice_status = normalize_status(existing_invoice[40])  # Status at index 40
        due_date_value = existing_invoice[27] if len(existing_invoice) > 27 else None
        page_title = f"Rechnung {_html.escape(invoice_number)} bearbeiten"
        is_edit_mode = True
    else:
        # Creating new invoice - Vorschlag aus dem Nummernkreis (Formatvorlage und Zusatz des Kreises,
        # eine schon belegte Nummer wird übersprungen); verbraucht wird die Nummer erst beim Speichern
        invoice_number, suggestion_range = db.peek_next_number('invoice', current_year)
        invoice_number = invoice_number or ''
        # Es gibt einen Kreis, aber keine Nummer daraus (Formatvorlage ohne Zähler): kein Vorschlag, dafür ein Hinweis
        number_hint = (suggestion_range is not None and not invoice_number)

        # Kopie: Datum von heute, alles andere aus der Quelle (todo #4)
        invoice_date = today_iso if is_copy else ''
        selected_own_company_id = (existing_invoice[3] if is_copy
                                   else (own_contact[0] if own_contact else None))
        selected_customer_id = existing_invoice[13] if is_copy else None
        invoice_status = 'draft'
        # Fälligkeitsdatum der Quelle ist verbraucht – wird aus dem neuen
        # Rechnungsdatum und dem Zahlungsziel neu berechnet
        due_date_value = None
        if is_copy:
            page_title = f"Neue Rechnung – Kopie von {_html.escape(str(existing_invoice[1] or ''))}"
        else:
            page_title = "Neue Rechnung erstellen"
        is_edit_mode = False
    
    # Versendet (gesperrt) oder storniert: Die Finanzangaben stehen im Formular nicht mehr offen
    # (db/invoice_lock.py; serverseitig entscheidet check_changes, das Formular zeigt es nur an).
    # Bei einer versendeten Rechnung bleiben Zahlungshinweis, Anschrift, Bestellnummer, Leitweg-ID und
    # Leistungsdatum änderbar (als Berichtigung), bei einer stornierten nichts.
    group = status_group(invoice_status) if is_edit_mode else EDITABLE
    locked = group != EDITABLE
    closed = group == CLOSED
    lock_attr = ' disabled' if locked else ''
    closed_attr = ' disabled' if closed else ''
    # Eine Rechnung, die schon einmal versendet war, behält ihre Nummer auch als Entwurf (der Kunde kennt sie)
    number_kept = is_edit_mode and group == EDITABLE and db.was_sent_before(invoice_id)
    number_attr = lock_attr or (' readonly' if number_kept else '')
    # Nummer der Fassung, die diese Maske geladen hat: Beim Berichtigen schickt sie sie mit (Schutz davor, eine
    # inzwischen anderweitig berichtigte Rechnung zu überschreiben)
    document_version = (as_document_version(existing_invoice[InvoiceCol.DOCUMENT_VERSION])
                        if is_edit_mode and len(existing_invoice) > InvoiceCol.DOCUMENT_VERSION else '')
    # Leitweg-ID: im Entwurf folgt sie dem Kunden (schreibgeschützt); bei einer versendeten Rechnung steht
    # der gespeicherte Wert da und ist ein normales Eingabefeld
    route_attr = closed_attr if locked else ' readonly'
    route_help = ('Gespeicherter Wert; bei einer versendeten Rechnung frei änderbar (nur bei B2G-Rechnungen erforderlich)'
                  if locked else
                  'Wird automatisch vom Kunden übernommen (nur bei B2G-Rechnungen erforderlich)')

    # Die ID gehört nur in den Bearbeiten-Modus: ein unbekannter oder fremder Beleg (Angebot)
    # ergibt ein leeres Formular, das beim Speichern eine neue Rechnung anlegt
    s = f'<input type="hidden" id="invoice_id" value="{invoice_id if is_edit_mode else ""}">'
    s += f'<input type="hidden" id="is_edit_mode" value="{str(is_edit_mode).lower()}">'
    s += f'<input type="hidden" id="invoice_status_value" value="{_html.escape(str(invoice_status))}">'
    s += f'<input type="hidden" id="invoice_locked" value="{str(locked).lower()}">'
    s += f'<input type="hidden" id="document_version" value="{document_version}">'
    # Die Nummer im Feld ist nur ein Vorschlag, solange sie unverändert bleibt: Dann vergibt der Server beim
    # Speichern frisch (ein zweites Fenster kann den Vorschlag inzwischen verbraucht haben)
    s += ('<input type="hidden" id="number_suggestion" value="'
          f'{"" if is_edit_mode else _html.escape(invoice_number)}">')
    # Check if PDF/XML actually exist – dieselbe Frage wie beim Download (geprüfter Pfad mit Fallback)
    pdf_file_exists = bool(is_edit_mode and stored_invoice_path(db, invoice_id, 'pdf', existing_invoice)[0])
    xml_file_exists = bool(is_edit_mode and stored_invoice_path(db, invoice_id, 'xml', existing_invoice)[0])
    s += f'<input type="hidden" id="pdf_exists" value="{str(pdf_file_exists).lower()}">'
    s += f'<input type="hidden" id="xml_exists" value="{str(xml_file_exists).lower()}">'
    s += '<div id="invoice_msg" class="no-pdf" style="display:none; margin-bottom:10px;"></div>'
    if is_edit_mode:
        payment_count = len(existing_payments)
        status_color  = INVOICE_STATUS_COLORS.get(invoice_status, '#888')
        status_label  = _html.escape(str(INVOICE_STATUS_LABELS.get(invoice_status, invoice_status)))
        # Angeboten werden nur die Status, auf die die Rechnung gesetzt werden darf (Übergangstabelle)
        targets = allowed_targets(invoice_status, payment_count)
        status_options_html = ''.join(
            '<option value="{k}"{sel}>{v}</option>'.format(
                k=_html.escape(k), v=_html.escape(str(INVOICE_STATUS_LABELS.get(k, k))),
                sel=' selected' if k == invoice_status else '')
            for k in [invoice_status, *targets]
        )
        set_status_attr = '' if targets else ' disabled'
        save_button = ('' if closed else
                       '<button onclick="saveInvoice()" class="coloredButton btn-sm bg-green">'
                       + ('✏ Berichtigen' if locked else '💾 Speichern') + '</button>')
        # Löschen gibt es nur für einen Entwurf (POST nach Rückfrage); sonst erst auf Entwurf zurücksetzen
        delete_button = ('' if invoice_status != 'draft' else
                         f'<button onclick="appConfirmPost(\'/invoice/delete?id={int(invoice_id)}\', '
                         + _html.escape(script_json(delete_question(db.was_sent_before(invoice_id))))
                         + ')" class="coloredButton btn-sm bg-red">🗑 Löschen</button>')
        s += '<div class="rectRounded no-pdf">' #right Column Headline, Status and Buttons
        s += f'<h2>{page_title}</h2>'
        s += f'''
        <table width="100%">
            <tr>
                <td>Status:</td>
                <td><div class="rowWithObjects">
                    '''
        if locked:
            s += '<div class="rectRounded bg-red">'
        s += f'<strong class="coloredButtonNoClick btn-smNoClick bg-back" style="color:{status_color}; hover:none;">{status_label}</strong>'
        if closed:
            s += ' &nbsp; &nbsp; &#9888; Storniert: Die Rechnung l&auml;sst sich nicht mehr &auml;ndern.</div>'
        elif locked:
            s += f' &nbsp; &nbsp; &#9888; {_html.escape(SENT_HINT)}</div>'
        s += f'''
                </div></td>
            </tr>
            <tr>
                <td>Neuer Status:</td>
                <td><div class="rowWithObjects">
                    <select id="statusChangeSelect">{status_options_html}</select>
                    <button onclick="setInvoiceStatus({invoice_id})" class="coloredButton btn-sm bg-blue"{set_status_attr}>&#9888; Status setzen</button>
                </div></td>
            </tr>
            <tr>
                <td>Rechnung:</td>
                <td><div class="rowWithObjects">
                    {save_button}
                    <button onclick="window.location.href='/invoice'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>
                    <button onclick="generatePDF()" class="coloredButton btn-sm bg-blue">📄 PDF + E-Rechnung</button>
                    <button onclick="downloadPDF()" class="coloredButton btn-sm bg-blue">⬇ Herunterladen</button>
                    <button onclick="copyInvoiceAsNew({invoice_id})" class="coloredButton btn-sm bg-gray">📋 Als neu kopieren</button>
                    {delete_button}
                </div></td>
            </tr>
        </table>
    </div>
<script>
function setInvoiceStatus(invId) {{
  var ns = document.getElementById('statusChangeSelect').value;
  fetch('/invoice/status', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{invoice_id: invId, status: ns}})}}).then(r => r.json())
  .then(data => {{
    if (!data.success) {{ showMessage('Fehler: ' + (data.error || '?'), 'error'); return; }}
    // "Versendet" erzeugt das PDF automatisch (todo #2) - Hinweis, falls das
    // nicht geklappt hat; sonst direkt neu laden.
    if (data.warning || data.hint) {{ showMessage(data.warning || data.hint, data.warning ? 'warn' : 'info'); setTimeout(function() {{ location.reload(); }}, 3500); }}
    else {{ location.reload(); }}
  }});
}}
// Identische Rechnung als neue Rechnung oeffnen - frisches Datum, neue
// Rechnungsnummer (todo #4)
function copyInvoiceAsNew(invId) {{
  window.location.href = '/invoice?copy_from=' + invId;
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
                    <tr><td>Rechnungsdatum:</td><td><input type="date" id="invoice_date" value="'''
    s += _html.escape(str(invoice_date)) if invoice_date else ''
    s += '" style="width: 150px;"' + lock_attr + '></td></tr>\n    '
    # Leistungsdatum direkt unter dem Rechnungsdatum: die beiden Daten gehören
    # zusammen und werden zusammen gepflegt
    _dd = existing_invoice[25] if existing_invoice and len(existing_invoice) > 25 and existing_invoice[25] else ''
    s += ('<tr><td title="Datum der Lieferung/Leistung bzw. Leistungszeitraum (Pflichtangabe nach § 14 UStG)">Leistungsdatum:</td>'
          f'<td><input type="date" id="delivery_date" required value="{_html.escape(str(_dd))}" style="width: 150px;"{closed_attr}></td></tr>')
    s += f'<tr><td>Rechnungs-Nr.:</td><td><input type="text" id="invoice_number" value="{_html.escape(invoice_number)}" style="width: 150px;"{number_attr}></td></tr>'
    if number_kept:
        s += f'<tr class="no-pdf"><td colspan="2" class="hint">{_html.escape(NUMBER_KEPT_MESSAGE)}</td></tr>'
    if number_hint:
        s += ('<tr class="no-pdf"><td colspan="2" class="hint">Der Nummernkreis liefert keine Nummer – bitte die '
              'Formatvorlage unter Stammdaten &rarr; Nummernkreise pr&uuml;fen (genau ein Z&auml;hler: '
              '{n}, {nn} oder {nnn}).</td></tr>')
    s += '''
                    <tr><td>Kunden-Nr.:</td><td><input type="text" id="customer_number" readonly style="width: 150px; background-color: #f0f0f0;"></td></tr>
                </table>
            </div>
        </div>
        
        <div class="invoice-address-block">
            <div class="invoice-customer-address">
                <select id="own_company_select" onchange="updateOwnCompany()" style="margin-bottom: 10px; width: 100%;" class="no-pdf"''' + lock_attr + '''>
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
                <select id="customer_select" onchange="updateCustomerAddress()" style="margin-bottom: 10px; width: 100%;" class="no-pdf"''' + lock_attr + '''>
                    <option value="">-- Kunde auswählen --</option>
    '''
    
    for customer in customers:
        cust_name = _html.escape(str(customer[3])) if customer[3] else f"ID {customer[0]}"  # display_name at index 3
        selected = 'selected' if existing_invoice and customer[0] == selected_customer_id else ''
        s += f'<option value="{customer[0]}" {selected}>{cust_name}</option>'
    
    # Eine versendete Rechnung zeigt ihre gespeicherte Anschrift (Momentaufnahme beim Erstellen), nicht die
    # heutigen Stammdaten; die Stammdaten übernimmt nur ein ausdrücklicher Wunsch (Kästchen unter der Anschrift)
    stored_address, refresh_html = '', ''
    if locked and existing_invoice:
        keys = ('buyer_name', 'buyer_company', 'buyer_street', 'buyer_postal_code', 'buyer_city', 'buyer_country')
        stored_snapshot = dict(zip(keys, existing_invoice[14:20]))
        stored_address = _html.escape(_address_text(stored_snapshot, '\n'))
        master = db.get_contact_by_id(existing_invoice[13]) if existing_invoice[13] and not closed else None
        if master:
            fresh = buyer_snapshot(master)
            if any((stored_snapshot[k] or ('DE' if k == 'buyer_country' else ''))
                   != (fresh[k] or '') for k in keys):
                refresh_html = ('<div class="no-pdf hint"><label><input type="checkbox" id="refresh_address"> '
                                'Anschrift aus den Stammdaten &uuml;bernehmen</label><br>'
                                + _html.escape(_address_text(stored_snapshot, ', ')) + ' &rarr; '
                                + _html.escape(_address_text(fresh, ', ')) + '</div>')
    s += '''                </select>
                <div id="customer_address_display" style="min-height: 80px; white-space: pre-line;">''' + stored_address + '''</div>''' + refresh_html + '''
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
            # Alles aus der Datenbank steht maskiert im Attribut: ein " im Text (Monitor 27")
            # schnitte ihn sonst ab, und der Rest ginge beim Speichern verloren
            quantity = _html.escape(str(item[5]))
            unit = _html.escape(str(unit_display(item[6])))
            description = _html.escape(str(item[4] or ''))
            price = _html.escape(str(item[7]))
            total = item[8]
            s += f'''
                <tr class="invoice-item-row" data-row="{idx}">
                    <td>{idx}</td>
                    <td><input type="number" class="item-quantity" value="{quantity}" min="0" step="0.01" style="width: 60px;"{lock_attr}></td>
                    <td><input type="text" class="item-unit" value="{unit}" style="width: 60px;"{lock_attr}></td>
                    <td><input type="text" class="item-description" value="{description}" style="width: 100%;"{lock_attr}></td>
                    <td><input type="number" class="item-price" value="{price}" min="0" step="0.01" style="width: 80px;"{lock_attr}> €</td>
                    <td class="item-total" style="text-align: right;">{fmt_eur(total)}</td>
                    <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;"{lock_attr}>✕</button></td>
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
                        <label><input type="checkbox" id="show_tax" __SHOW_TAX_CHECKED__''' + lock_attr + ''' onchange="calculateTotals()"> Umsatzsteuer ausweisen</label>
                        <small style="color:#888;">(aus = Kleinunternehmer §&nbsp;19 UStG; Hinweis ggf. unten im Text)</small>
                    </td>
                </tr>
                <tr class="totals-row" id="tax_row">
                    <td colspan="4" style="text-align: right; border: none;">Mehrwertsteuer</td>
                    <td style="text-align: right; border: none;"><input type="number" id="tax_rate" value="__TAX_PCT__" min="0" max="100" step="0.1" style="width: 50px;"''' + lock_attr + '''>% auf <span id="tax_base">0,00</span> € netto:</td>
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
            <button type="button" onclick="addFreeRow()" style="margin-right: 10px;"''' + lock_attr + '''>+ Position frei editierbar hinzufügen</button>
            <span style="position:relative; display:inline-block;">
                <input type="text" id="articleComboInput" placeholder="Artikel suchen …"
                       style="width:280px; padding:4px 8px;" autocomplete="off"
                       oninput="filterArticles(this.value)" onfocus="filterArticles(this.value)"
                       onblur="setTimeout(()=>document.getElementById('articleDropdown').style.display='none',150)"''' + lock_attr + '''>
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
    s += f'<textarea id="payment_terms" rows="3" style="width: 100%;"{closed_attr}>{payment_terms_text}</textarea>'
    
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
                        <select id="bank_account_select" onchange="updateBankDetails()" style="width: 100%; margin-top: 5px;" class="no-pdf"''' + lock_attr + '''>
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
    s += _html.escape(str(existing_invoice[23])) if existing_invoice and len(existing_invoice) > 23 and existing_invoice[23] else ''  # OrderNumber is now at index 23 (was 21)
    s += '"' + closed_attr + '''>
                        <br><small style="color: #666;">Bestellnummer des Kunden (falls vorhanden)</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Leitweg-ID:</strong></td>
                    <td style="padding: 8px;">
                        <input type="text" id="buyer_route_id" style="width: 300px;" placeholder="z.B. 991-ABCDE-12"''' + route_attr + ''' value="'''
    s += _html.escape(str(existing_invoice[22])) if existing_invoice and len(existing_invoice) > 22 and existing_invoice[22] else ''  # BuyerRouteID is now at index 22 (was 20)
    s += '''">
                        <br><small style="color: #666;">''' + route_help + '''</small>
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
    s += '" min="0" style="width: 100px;"' + lock_attr + '''> Tage
                        <br><small style="color: #666;">Anzahl der Tage bis zur Fälligkeit (Standard: 14 Tage)</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Fälligkeitsdatum:</strong></td>
                    <td style="padding: 8px;">
                        <input type="date" id="payment_due_date" style="width: 200px;" value="'''
    s += _html.escape(str(due_date_value or ''))  # PaymentDueDate (bei einer Kopie leer)
    s += '"' + lock_attr + '''>
                        <br><small style="color: #666;">Wird automatisch berechnet oder kann manuell festgelegt werden</small>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 8px; vertical-align: top;"><strong>Skonto:</strong></td>
                    <td style="padding: 8px;">
                        <input type="number" id="discount_percentage" min="0" max="100" step="0.1" style="width: 80px;" placeholder="z.B. 2" value="'''
    s += _html.escape(str(existing_invoice[29])) if existing_invoice and len(existing_invoice) > 29 and existing_invoice[29] else ''  # SkontoPercent is now at index 29 (was 27)
    s += '"' + lock_attr + '''> %
                        bei Zahlung innerhalb von
                        <input type="number" id="discount_days" min="0" style="width: 80px;" placeholder="z.B. 7" value="'''
    s += _html.escape(str(existing_invoice[28])) if existing_invoice and len(existing_invoice) > 28 and existing_invoice[28] else ''  # SkontoDays is now at index 28 (was 26)
    s += '"' + lock_attr + '''> Tagen
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
            'unit': unit_display(article[2]),
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

        // Leistungsdatum (Pflicht, § 14 UStG): mit dem Rechnungsdatum vorbelegen
        // und mitlaufen lassen, solange der Nutzer es nicht selbst angefasst hat.
        (function () {
            const inv = document.getElementById('invoice_date');
            const del = document.getElementById('delivery_date');
            let touched = !!del.value && del.value !== inv.value;
            // Eine versendete Rechnung ohne Leistungsdatum (Import) bleibt ohne: Vorbelegt nur im Entwurf
            if (!del.value && document.getElementById('invoice_locked').value !== 'true') del.value = inv.value;
            del.addEventListener('input', () => { touched = true; });
            inv.addEventListener('change', () => { if (!touched) del.value = inv.value; });
        })();
        
        // Vorausgewählte Werte beim Laden übernehmen – in JEDEM Modus: auch eine
        // neue Rechnung hat eine vorausgewählte eigene Firma (bisher fehlte dort
        // das Logo), eine Kopie zusätzlich Kunde und Bankverbindung.
        updateOwnCompany();
        updateCustomerAddress();
        updateBankDetails();
        calculateTotals();

        // Stand beim Laden: Eine versendete Rechnung schickt beim Speichern nur, was der Nutzer davon ändert
        const initialOpenFields = {
            paymentTerms: document.getElementById('payment_terms').value,
            orderNumber: document.getElementById('order_number').value,
            buyerReference: document.getElementById('buyer_route_id').value,
            deliveryDate: document.getElementById('delivery_date').value
        };
        
        // Kontakt- und Bankdaten stammen aus den Stammdaten und kommen als DOM-Knoten
        // in die Seite (Text bleibt Text, nie Markup). Eine Zeile ist ein Text oder
        // [Beschriftung, Wert]; zwischen den Zeilen steht je ein <br>-Knoten.
        function setLines(box, lines) {
            box.textContent = '';
            lines.forEach(function(line, i) {
                if (i > 0) box.appendChild(document.createElement('br'));
                if (Array.isArray(line)) {
                    const label = document.createElement('span');
                    label.className = 'footer-label';
                    label.textContent = line[0];
                    box.appendChild(label);
                    box.appendChild(document.createTextNode(' ' + line[1]));
                } else {
                    box.appendChild(document.createTextNode(line));
                }
            });
        }
        // Hinweistext (kursiv) statt Daten, z. B. wenn noch keine eigene Firma angelegt ist
        function setHint(box, text) {
            const hint = document.createElement('em');
            hint.textContent = text;
            box.textContent = '';
            box.appendChild(hint);
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
                setHint(footerAddress, 'Eigene Firmendaten in Kontakte anlegen');
                setHint(footerContact, 'Kontaktdaten fehlen');
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
                const addressLines = [displayName];
                if (company.address_line1) addressLines.push(company.address_line1);
                addressLines.push(company.street);
                addressLines.push(company.postal + ' ' + company.city);
                setLines(footerAddress, addressLines);
                
                // Update footer contact
                setLines(footerContact, [
                    ['Tel', company.phone || '-'],
                    ['E-Mail', company.email || '-'],
                    ['UStIdNr', company.tax_id || '-']
                ]);
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

            // Eine versendete Rechnung behält ihre gespeicherte Anschrift und Leitweg-ID: Das Formular
            // überschreibt beim Laden nichts aus den Stammdaten (nur die Kundennummer wird angezeigt)
            if (document.getElementById('invoice_locked').value === 'true') {
                const savedCustomer = customersData[customerId];
                customerNumberField.value = savedCustomer ? savedCustomer.customer_number : '';
                return;
            }
            
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
                bankDetails.textContent = '';
                return;
            }
            
            const bank = banksData[bankId];
            if (bank) {
                setLines(bankDetails, [
                    ['Bank', bank.bank_name],
                    ['IBAN', bank.iban],
                    ['BIC', bank.bic]
                ]);
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
        
        // Artikel-Combobox. Name und Einheit der Artikel stammen aus den Stammdaten: Sie kommen
        // als DOM-Knoten (textContent) in die Seite, nie als Markup.
        function filterArticles(query) {
            const dropdown = document.getElementById('articleDropdown');
            const q = query.toLowerCase();
            const entries = Object.entries(articlesData).filter(([id, a]) =>
                !q || a.name.toLowerCase().includes(q)
            );
            if (!entries.length) { dropdown.style.display = 'none'; return; }
            dropdown.textContent = '';
            entries.forEach(([id, a]) => {
                const option = document.createElement('div');
                option.className = 'article-option';
                option.addEventListener('mousedown', () => selectArticle(id));
                const name = document.createElement('strong');
                name.textContent = a.name;
                const meta = document.createElement('span');
                meta.className = 'article-option-meta';
                meta.textContent = a.unit + ' · ' + fmtDe(a.price) + ' € · ' + a.taxRate + '%';
                option.appendChild(name);
                option.appendChild(meta);
                dropdown.appendChild(option);
            });
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

            // Das Gerüst enthält keine Artikeldaten; Einheit, Text und Preis kommen als Wert in
            // die Felder (in das Markup eingesetzt, bräche ein " im Namen aus dem Attribut)
            newRow.innerHTML = `
                <td>${rowCounter}</td>
                <td><input type="number" class="item-quantity" value="1" min="0" step="0.01" style="width: 60px;"></td>
                <td><input type="text" class="item-unit" style="width: 60px;"></td>
                <td><input type="text" class="item-description" style="width: 100%;"></td>
                <td><input type="number" class="item-price" value="0" min="0" step="0.01" style="width: 80px;"> €</td>
                <td class="item-total" style="text-align: right;">0,00 €</td>
                <td class="no-pdf"><button type="button" onclick="removeRow(this)" style="color: red;">✕</button></td>
            `;
            newRow.querySelector('.item-unit').value = article.unit;
            newRow.querySelector('.item-description').value =
                article.description ? article.name + ' - ' + article.description : article.name;
            newRow.querySelector('.item-price').value = article.price;
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
        
        // Betrags-Anzeige/-Parsing im deutschen Format (Tausenderpunkt, Dezimalkomma).
        // Die Anzeigefelder werden von den Berechnungen zurückgelesen – daher immer
        // über fmtDe/parseDe, nie nur '.'/',' tauschen.
        function fmtDe(n) { return n.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
        function parseDe(t) { return parseFloat(String(t).replace(/<[^>]*>/g, '').replace('€', '').replace(/\\./g, '').replace(',', '.').trim()); }

        // Calculate row total
        function calculateRowTotal(row) {
            const quantity = parseFloat(row.querySelector('.item-quantity').value) || 0;
            const price = parseFloat(row.querySelector('.item-price').value) || 0;
            const total = quantity * price;
            row.querySelector('.item-total').textContent = fmtDe(total) + ' €';
        }
        
        // Calculate all totals
        function calculateTotals() {
            // Calculate sum of all items
            let sumNet = 0;
            document.querySelectorAll('.invoice-item-row').forEach(row => {
                calculateRowTotal(row);
                const totalText = row.querySelector('.item-total').textContent;
                const total = parseDe(totalText) || 0;
                sumNet += total;
            });
            
            // Update net sum
            document.getElementById('sum_net').textContent = fmtDe(sumNet) + ' €';
            document.getElementById('tax_base').textContent = fmtDe(sumNet);

            // USt nur ausweisen, wenn Checkbox aktiv (Kleinunternehmer §19: keine Zeile)
            const showTax = document.getElementById('show_tax').checked;
            const taxRow = document.getElementById('tax_row');
            if (taxRow) taxRow.style.display = showTax ? '' : 'none';

            // Calculate tax
            const taxRate = showTax ? (parseFloat(document.getElementById('tax_rate').value) || 0) : 0;
            const taxAmount = sumNet * (taxRate / 100);
            document.getElementById('tax_amount').textContent = fmtDe(taxAmount) + ' €';

            // Calculate gross sum
            const sumGross = sumNet + taxAmount;
            document.getElementById('sum_gross').innerHTML = '<strong>' + fmtDe(sumGross) + ' €</strong>';
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

            // Versendete Rechnung: Betraege, Positionen, Nummer, Datum und Kunde sind gesperrt (der Server
            // lehnt jede Aenderung daran ab) - geschickt wird nur, was sich noch aendern laesst UND was der
            // Nutzer geaendert hat; die Anschrift nur auf ausdruecklichen Wunsch (Kaestchen). Das Speichern
            // ist hier ein Berichtigen: nach Rueckfrage, mit der Fassung, die diese Maske geladen hat
            if (isEdit && document.getElementById('invoice_locked').value === 'true') {
                const current = {
                    paymentTerms: document.getElementById('payment_terms').value,
                    orderNumber: document.getElementById('order_number').value || null,
                    buyerReference: document.getElementById('buyer_route_id').value || null,
                    deliveryDate: document.getElementById('delivery_date').value || null
                };
                const changes = {invoiceId: parseInt(invoiceId),
                                 documentVersion: parseInt(document.getElementById('document_version').value)};
                Object.keys(current).forEach(function(key) {
                    if ((current[key] || '') !== (initialOpenFields[key] || '')) changes[key] = current[key];
                });
                const refresh = document.getElementById('refresh_address');
                if (refresh && refresh.checked) changes.refreshAddress = true;
                if (Object.keys(changes).length === 2) {         // nur die ID und die Fassung: nichts geändert
                    showMessage('Es wurde nichts geändert.', 'info');
                    return;
                }
                // Das Leistungsdatum ist Pflicht: geleert wird es nicht (ein leeres, unverändertes - Import - schon)
                if ('deliveryDate' in changes && !changes.deliveryDate) {
                    showMessage(__DELIVERY_DATE_MESSAGE__, 'error');
                    document.getElementById('delivery_date').focus();
                    return;
                }
                showConfirm(__CORRECT_QUESTION__, function() { sendInvoice(changes, isEdit, invoiceId); });
                return;
            }

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
            
            if (!document.getElementById('delivery_date').value) {
                showMessage('Bitte geben Sie das Leistungsdatum an (Pflichtangabe nach § 14 UStG).', 'error');
                document.getElementById('delivery_date').focus();
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
                        totalPrice: parseDe(row.querySelector('.item-total').textContent) || 0,
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
            const netAmount = parseDe(document.getElementById('sum_net').textContent);
            const taxAmount = parseDe(document.getElementById('tax_amount').textContent);
            const grossAmount = parseDe(document.getElementById('sum_gross').textContent);
            
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
                // Die vorgeschlagene Nummer: bleibt sie unverändert, vergibt der Server beim Speichern frisch
                numberSuggestion: isEdit ? null : (document.getElementById('number_suggestion').value || null),
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

            sendInvoice(invoiceData, isEdit, invoiceId);
        }

        // Send to server
        function sendInvoice(invoiceData, isEdit, invoiceId) {
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
                    const target = '/invoice?id=' + redirectId;
                    // Berichtigt: die Maske kennt jetzt die neue Fassung (die Seite wird gleich neu geladen)
                    if (data.documentVersion) document.getElementById('document_version').value = data.documentVersion;
                    // Gespeichert, aber mit Hinweis (z. B. Nummernkreis): kurz anzeigen, dann weiter
                    if (data.warning) {
                        showMessage(data.warning, 'warn');
                        setTimeout(function() { window.location.href = target; }, 4000);
                    } else if (!isEdit && data.invoice_number && data.invoice_number !== invoiceData.invoiceNumber) {
                        // Der Vorschlag war inzwischen vergeben: die Maske zeigt die Nummer, die gilt
                        document.getElementById('invoice_number').value = data.invoice_number;
                        showMessage('Gespeichert mit der Rechnungsnummer ' + data.invoice_number
                                    + ' (der Vorschlag ' + invoiceData.invoiceNumber + ' war inzwischen vergeben).', 'info');
                        setTimeout(function() { window.location.href = target; }, 3500);
                    } else if (data.documentVersion) {
                        showMessage('Berichtigt: Fassung ' + data.documentVersion
                                    + '. Die neue Fassung muss dem Kunden erneut zugehen.', 'success');
                        setTimeout(function() { window.location.href = target; }, 2500);
                    } else {
                        window.location.href = target;
                    }
                } else {
                    showMessage('Fehler beim Speichern: ' + (data.error || 'Unbekannter Fehler'), 'error');
                }
            })
            .catch(err => {
                showMessage('Fehler beim Speichern: ' + err, 'error');
                console.error('Save error:', err);
            });
        }
        
        // PDF (und E-Rechnung) erzeugen und anschliessend echt herunterladen
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
            fetch('/invoice/pdf_generate?id=' + invoiceId, {method: 'POST'})
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('pdf_exists').value = 'true';
                        showMessage('PDF erstellt: ' + data.filename
                                    + (data.warning ? ' (' + data.warning + ')' : ''), 'success');
                        downloadInvoiceFiles(invoiceId, !!data.xml_filename);
                    } else {
                        showMessage('Fehler beim Erstellen der PDF: ' + (data.error || 'Unbekannter Fehler'), 'error');
                    }
                })
                .catch(function(err) {
                    showMessage('Fehler: ' + err.message, 'error');
                });
        }
        // Echter Download der abgelegten Dateien (todo #1) - kein Server-Link,
        // sondern die Datei selbst.
        function downloadInvoiceFiles(invoiceId, withXml) {
            const notify = function(text, type) { showMessage(text, type); };
            appDownload('/invoice/pdf_download?id=' + invoiceId, notify);
            if (withXml) appDownload('/invoice/xml_download?id=' + invoiceId, notify);
        }
        // Herunterladen ohne Neuerzeugung (todo #3)
        function downloadPDF() {
            const invoiceId = document.getElementById('invoice_id').value;
            if (!invoiceId) {
                showMessage('Bitte speichern Sie die Rechnung zuerst.', 'warn');
                return;
            }
            if (document.getElementById('pdf_exists').value !== 'true') {
                showMessage('Kein PDF vorhanden - bitte zuerst erzeugen.', 'error');
                return;
            }
            downloadInvoiceFiles(invoiceId,
                document.getElementById('xml_exists')?.value === 'true');
        }
    </script>
    '''.replace('__CORRECT_QUESTION__', script_json(CORRECT_QUESTION)
                ).replace('__DELIVERY_DATE_MESSAGE__', script_json(DELIVERY_DATE_MESSAGE))

    # Payment history section (edit mode only)
    if is_edit_mode and invoice_id:
        amount_due = existing_invoice[39] if existing_invoice else 0
        s += f'''
    <div style="margin: 30px 0; padding: 20px; border: 1px solid #ddd; border-radius: 4px;">
        <h3>Zahlungsverknüpfungen</h3>
'''
        if payment_error:
            s += f'        <p class="errorColor">{_html.escape(str(payment_error))}</p>\n'
        findings = db.find_payment_inconsistencies(invoice_id)
        if findings:
            s += ('        <div class="warnColor"><strong>Zahlungszuordnung prüfen:</strong><ul>\n'
                  + ''.join(f'            <li>{_html.escape(f["text"])}</li>\n' for f in findings)
                  + '        </ul>Alle Rechnungen mit Unstimmigkeiten zeigt die Prüfung unter '
                  '<a href="/miscellaneous?check_payments=1">Sonstiges</a>.</div>\n')
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
                <td style="padding:6px 8px;">{_html.escape(str(pay_date or "–"))}</td>
                <td style="padding:6px 8px;">#{booking_id} {_html.escape(str(booking_ref or ""))}</td>
                <td style="padding:6px 8px; text-align:right;">{fmt_eur(amount)}</td>
                <td style="padding:6px 8px; text-align:center;">
                    <a href="javascript:void(0);" onclick="deletePayment({pid})" style="color:#c00;">✕ Entfernen</a>
                </td>
            </tr>
'''
            s += f'''            <tr style="font-weight:bold; border-top:2px solid #ccc;">
                <td colspan="2" style="padding:6px 8px;">Gezahlt gesamt</td>
                <td style="padding:6px 8px; text-align:right;">{fmt_eur(total_paid)}</td>
                <td></td>
            </tr>
            <tr style="font-weight:bold;">
                <td colspan="2" style="padding:6px 8px;">Noch offen</td>
                <td style="padding:6px 8px; text-align:right; color:{"#c00" if (amount_due or 0) > 0.01 else "#2a2"};">{fmt_eur(amount_due or 0)}</td>
                <td></td>
            </tr>
        </table>
'''
        else:
            s += '        <p><em>Keine Zahlungen verknüpft.</em></p>\n'

        # Zahlungs-Aktionen (todo #2): neue Zahlung verbuchen (vorbefüllte
        # Buchungsmaske) oder eine vorhandene Buchung mit freiem Rest zuordnen
        # (Import-Fall). "Bezahlt" ergibt sich automatisch aus der Zuordnung.
        is_invoice_doc = (len(existing_invoice) <= 45
                          or existing_invoice[45] != 'quote')
        # Zahlbar ist eine Rechnung erst ab „versendet“ (PAYABLE_STATUSES; 'partial' ist
        # schon auf 'partial_payment' abgebildet): Davor steht sie noch nicht in den Büchern.
        payable = (is_invoice_doc
                   and invoice_status in PAYABLE_STATUSES
                   and (amount_due is None or amount_due > 0))
        if is_invoice_doc and invoice_status in ('draft', 'finalized'):
            s += ('        <p class="hint">Zahlungen lassen sich erst zuordnen, wenn die '
                  'Rechnung versendet ist.</p>\n')
        if payable:
            open_due = (amount_due if amount_due is not None
                        else (existing_invoice[38] or 0))
            # Ist für den Steuersatz kein Erlöskonto hinterlegt (z. B. 5 %, 16 %), wählt der
            # Nutzer es hier – für „Verknüpfen“ und, per Adresse mitgegeben, für die Maske
            choice = revenue_coa_choice(db, existing_invoice[35])
            if choice:
                s += (f'        <p class="hint">{choice[1]}</p>\n'
                      f'        <p><label>Erlöskonto: {choice[0]}</label></p>\n')
            s += (f'        <p><a href="/transactions?from_invoice={invoice_id}" '
                  'class="coloredButton btn-sm bg-green" onclick="return bookPayment(this)">'
                  '💶 Zahlung verbuchen</a></p>\n')
            linked_booking_ids = {p[2] for p in existing_payments}
            candidates = [c for c in
                          db.get_unallocated_bank_bookings(existing_invoice[13])
                          if c[0] not in linked_booking_ids]
            if candidates:
                s += '''        <h4>Vorhandene Buchung verknüpfen</h4>
        <table style="width:100%; border-collapse:collapse;">
            <tr style="background:#f5f5f5;">
                <th style="padding:6px 8px; text-align:left;">Datum</th>
                <th style="padding:6px 8px; text-align:left;">Empfänger/Auftragg.</th>
                <th style="padding:6px 8px; text-align:right;">Betrag</th>
                <th style="padding:6px 8px; text-align:right;">Frei</th>
                <th style="padding:6px 8px; text-align:right;">Zuordnen</th>
                <th style="padding:6px 8px;"></th>
            </tr>
'''
                for c in candidates:
                    b_id, b_date, b_recipient, _, b_amount, b_free = c
                    default_alloc = min(open_due, b_free)
                    match_mark = ' ✓' if b_free == open_due else ''
                    s += f'''            <tr>
                <td style="padding:6px 8px;">{_html.escape(str(b_date or "–"))}</td>
                <td style="padding:6px 8px;">{_html.escape(str(b_recipient or ""))}</td>
                <td style="padding:6px 8px; text-align:right;">{fmt_eur(b_amount)}</td>
                <td style="padding:6px 8px; text-align:right;">{fmt_eur(b_free)}{match_mark}</td>
                <td style="padding:6px 8px; text-align:right;"><input type="number" step="0.01" class="noButtons" id="payAmount_{b_id}" value="{default_alloc:.2f}" style="width:90px; text-align:right;"></td>
                <td style="padding:6px 8px;"><button onclick="linkPayment({b_id}, this)" class="coloredButton btn-sm bg-blue">Verknüpfen</button></td>
            </tr>
'''
                s += '        </table>\n'

        s += f'''    </div>
    <script>
        // Das gewählte Erlöskonto ('' = es gibt kein Feld: die Kontierung ergibt sich aus der
        // Rechnung; null = Feld ohne Wahl, der Nutzer wurde darauf hingewiesen)
        function revenueCoaChoice() {{
            const field = document.getElementById('revenueCoa');
            if (!field) return '';
            if (!field.value) {{
                showMessage('Bitte ein Erlöskonto wählen.', 'error');
                return null;
            }}
            return field.value;
        }}
        function bookPayment(link) {{
            const coa = revenueCoaChoice();
            if (coa === null) return false;
            link.href = '/transactions?from_invoice={invoice_id}'
                + (coa ? '&revenue_coa_id=' + encodeURIComponent(coa) : '');
            return true;
        }}
        function linkPayment(bookingId, button) {{
            const amount = parseFloat(document.getElementById('payAmount_' + bookingId).value);
            if (!amount || amount <= 0) {{
                showMessage('Betrag muss größer 0 sein.', 'error');
                return;
            }}
            const coa = revenueCoaChoice();
            if (coa === null) return;
            const payload = {{invoice_id: {invoice_id}, transaction_id: bookingId,
                             amount_paid: amount}};
            if (coa) payload.revenue_coa_id = parseInt(coa, 10);
            button.disabled = true;   // ein Doppelklick ordnet nicht doppelt zu
            fetch('/invoice/link-payment', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify(payload)
            }})
            .then(r => r.json())
            .then(data => {{
                if (data.success) {{ location.reload(); }}
                else {{
                    button.disabled = false;
                    showMessage('Fehler: ' + (data.error || 'Unbekannt'), 'error');
                }}
            }})
            .catch(() => {{
                button.disabled = false;
                showMessage('Fehler: Die Zahlung konnte nicht verknüpft werden.', 'error');
            }});
        }}
        function deletePayment(paymentId) {{
            showConfirm('Zahlungsverkn\u00fcpfung wirklich entfernen? Die von der App daf\u00fcr angelegten Buchungss\u00e4tze werden mit entfernt.', function() {{
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
    # Der Kasten steht voller Daten aus der Datenbank: Er kommt erst nach den Ersetzungen dazu
    if is_edit_mode and invoice_id:
        s += _versions_box_html(db, invoice_id)
    return s


# ── Fassungen und Protokoll ──────────────────────────────────────────────────

_REASON_LABELS = {'corrected': 'berichtigt', 'reopened': 'zurückgeholt'}
_SENT_UNTIL = re.compile(r'versendet bis (\d{4})-(\d{2})-(\d{2})')


def _german_date(day):
    return day.strftime('%d.%m.%Y') if day else '–'


def _download_link(invoice_id, year, filename, label):
    """Link, der eine abgelegte Fassung über ``appDownload`` lädt (``/invoice/version_download``). Er trägt den
    Jahresordner: Dieselbe Datei kann in zwei Jahresordnern liegen, der Name allein träfe den ersten."""
    url = (f'/invoice/version_download?id={int(invoice_id)}&file={quote(filename, safe="")}'
           f'&year={quote(year, safe="")}')
    return (f'<a href="javascript:void(0);" onclick="{_html.escape("appDownload(" + script_json(url) + ")")}">'
            f'{_html.escape(label)}</a>')


def _version_rows(db, invoice_id):
    """Die abgelegten Fassungen als Zeilen ``(Fassung, versendet bis, Anlass, [(Art, Jahresordner, Dateiname,
    vorhanden)])``: die in ``InvoiceDocumentVersions`` festgehaltenen (der Jahresordner steht im ``RelPath``), dazu
    die Dateien in ``Fassungen/<ID>``, zu denen es keine Tabellenzeile gibt (Altbestand aus 2.1.2) – die erscheinen
    ohne Fassungsnummer, mit dem Jahresordner, in dem sie liegen. „Vorhanden“ heißt: im Jahresordner der Zeile."""
    present = {(year, name) for name, _path, year in version_files(invoice_id)}
    grouped, known = {}, set()
    for version, kind, rel_path, _sha, archived_at, reason in db.get_document_versions(invoice_id):
        year, filename = rel_path.split('/', 1)[0], rel_path.rsplit('/', 1)[-1]
        known.add((year, filename))
        row = grouped.setdefault((version, archived_at, reason), [])
        row.append((kind.upper(), year, filename, (year, filename) in present))
    rows = [(version, _german_date(local_date(archived_at)), _REASON_LABELS.get(reason, reason or '–'), files)
            for (version, archived_at, reason), files in sorted(
                grouped.items(), key=lambda kv: (kv[0][0], str(kv[0][1] or '')))]
    loose = {}
    for year, filename in sorted(present - known):
        stem, extension = filename.rsplit('.', 1) if '.' in filename else (filename, '')
        loose.setdefault((year, stem), []).append((extension.upper(), year, filename, True))
    for (_year, stem), files in loose.items():
        found = _SENT_UNTIL.search(stem)
        until = '.'.join(reversed(found.groups())) if found else '–'
        rows.append(('–', until, '–', files))
    return rows


def _protocol_value(field, value):
    if value is None or value == '':
        return '–'
    return STATUS_LABELS.get(value, value) if field == 'Status' else value


def _versions_box_html(db, invoice_id):
    """Kasten „Fassungen und Protokoll“ auf der Rechnungsseite: die abgelegten Fassungen (mit Download je
    Datei) und das Protokoll der Berichtigungen und Statuswechsel. Leer, wenn es nichts gibt. Jeder Wert aus
    der Datenbank ist maskiert, Dateinamen stehen prozentkodiert in der Adresse."""
    versions = _version_rows(db, invoice_id)
    protocol = db.get_invoice_protocol(invoice_id)
    if not versions and not protocol:
        return ''
    h = '    <div class="rectRounded no-pdf" id="invoice_versions">\n        <h3>Fassungen und Protokoll</h3>\n'
    if versions:
        h += ('        <h4>Abgelegte Fassungen</h4>\n        <table class="versionTable">\n'
              '            <tr><th>Fassung</th><th>Versendet bis</th><th>Anlass</th><th>Dateien</th></tr>\n')
        for number, until, reason, files in versions:
            links = ' '.join(_download_link(invoice_id, year, name, kind) if present
                             else f'<span class="muted" title="Datei nicht mehr vorhanden">{_html.escape(kind)}</span>'
                             for kind, year, name, present in files)
            h += (f'            <tr><td>{_html.escape(str(number))}</td><td>{_html.escape(until)}</td>'
                  f'<td>{_html.escape(reason)}</td><td>{links}</td></tr>\n')
        h += '        </table>\n'
    if protocol:
        h += ('        <h4>Protokoll</h4>\n        <table class="versionTable">\n'
              '            <tr><th>Datum</th><th>Benutzer</th><th>Feld</th><th>Alt &rarr; Neu</th></tr>\n')
        for changed_at, user, _action, field, old, new, _version in protocol:
            moment = local_datetime(changed_at)
            when = moment.strftime('%d.%m.%Y %H:%M') if moment else str(changed_at or '–')
            change = f'{_protocol_value(field, old)} \u2192 {_protocol_value(field, new)}'
            h += (f'            <tr><td>{_html.escape(when)}</td><td>{_html.escape(user or "–")}</td>'
                  f'<td>{_html.escape(PROTOCOL_LABELS.get(field, field or "–"))}</td>'
                  f'<td>{_html.escape(change)}</td></tr>\n')
        h += '        </table>\n'
    return h + '    </div>\n'
