"""
Asset management pages (Anlagenverzeichnis)
"""
import datetime
from db import Database
from .pages import Header1, Header2, Header3, Footer


# ─── Helper ──────────────────────────────────────────────────────────────────

def _status_label(status):
    return {'active': 'Aktiv', 'sold': 'Verkauft', 'scrapped': 'Abgang'}.get(status, status)

def _method_label(method):
    return {'linear': 'Linear', 'degressive': 'Degressiv', 'both': 'Linear / Degressiv', 'GWG': 'GWG (Sofort)'}.get(method, method)

def _fmt(value):
    """Format number as German currency string."""
    if value is None:
        return '-'
    return f"{value:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.') + ' €'


# ─── PageAssets ──────────────────────────────────────────────────────────────

def PageAssets(db: Database, status_filter=''):
    """Asset list with statistics header"""
    assets = db.fetch_assets(status=status_filter if status_filter else None, parent_only=True)
    today = datetime.date.today()
    current_year = today.year

    # Pre-compute book values and depreciation info
    asset_rows = []
    total_purchase = 0.0
    total_book_value = 0.0
    total_depr_planned = 0.0
    total_depr_posted = 0.0

    for a in assets:
        asset_id = a[0]
        purchase_price = a[7]
        purchase_date = a[6]
        useful_life = a[8]
        method = a[9]
        status = a[17]

        plan = db.calculate_depreciation_plan(purchase_price, purchase_date, useful_life, method)
        book_value = db.get_book_value_at_date(asset_id)

        # Current year depreciation
        year_entry = next((e for e in plan if e['year'] == current_year), None)
        depr_this_year = year_entry['depreciation'] if year_entry else 0.0

        # Check if already booked
        existing = db.get_depreciations_for_asset(asset_id)
        booked_years = {e[2]: e[5] for e in existing}  # year → status
        depr_status = booked_years.get(current_year, 'planned')

        if status == 'active':
            total_purchase += purchase_price
            total_book_value += book_value
            if depr_status == 'posted':
                total_depr_posted += depr_this_year
            else:
                total_depr_planned += depr_this_year

        asset_rows.append({
            'id': asset_id,
            'inv': a[1],
            'name': a[2],
            'cat': a[22] if len(a) > 22 else '',   # CategoryName from JOIN
            'purchase_date': purchase_date,
            'purchase_price': purchase_price,
            'book_value': book_value,
            'depr_this_year': depr_this_year,
            'depr_status': depr_status,
            'status': status,
            'method': method,
        })

    s = Header1('assets')
    submenu = '<span id="ActivePage">Anlagen</span> | <a href="/assets/new">Neue Anlage</a> | <a href="/asset_categories">AfA-Kategorien</a>'
    s += Header2(submenu)

    # Filter bar
    filter_options = [('', 'Alle'), ('active', 'Aktiv'), ('sold', 'Verkauft'), ('scrapped', 'Abgang')]
    filter_html = '<div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">'
    filter_html += '<label>Status:</label>'
    for val, label in filter_options:
        active = 'font-weight:bold; text-decoration:underline;' if status_filter == val else ''
        filter_html += f'<a href="/assets?status={val}" style="{active}">{label}</a>'
    filter_html += '</div>'
    s += Header3(filter_html)

    # Statistics
    s += f'''
    <div class="rectRounded" style="display:flex; gap:30px; flex-wrap:wrap; margin-bottom:10px;">
        <div><strong>Anschaffungskosten (aktiv):</strong> {_fmt(total_purchase)}</div>
        <div><strong>Gesamtrestbuchwert:</strong> {_fmt(total_book_value)}</div>
        <div><strong>AfA {current_year} gebucht:</strong> {_fmt(total_depr_posted)}</div>
        <div><strong>AfA {current_year} geplant:</strong> {_fmt(total_depr_planned)}</div>
    </div>
    '''

    if not asset_rows:
        s += '<p><em>Keine Anlagen gefunden.</em></p>'
    else:
        s += '''<table border="1" style="width:100%; border-collapse:collapse;">
        <tr>
            <th>Inv.-Nr.</th>
            <th>Bezeichnung</th>
            <th>Kategorie</th>
            <th>Anschaff.-datum</th>
            <th style="text-align:right;">AK</th>
            <th style="text-align:right;">Restbuchwert</th>
            <th style="text-align:right;">AfA {}</th>
            <th>Status</th>
            <th>Aktionen</th>
        </tr>'''.format(current_year)

        for row in asset_rows:
            depr_badge = ''
            if row['depr_this_year'] > 0:
                if row['depr_status'] == 'posted':
                    depr_badge = f" <span style='color:green;font-size:0.8em;'>✓ gebucht</span>"
                else:
                    depr_badge = f" <span style='color:orange;font-size:0.8em;'>● offen</span>"
            status_color = {'active': 'green', 'sold': '#888', 'scrapped': '#cc0000'}.get(row['status'], '#000')
            s += f'''<tr>
                <td><a href="/assets/view?id={row['id']}">{row['inv'] or ''}</a></td>
                <td><a href="/assets/view?id={row['id']}">{row['name']}</a></td>
                <td>{row['cat'] or ''}</td>
                <td>{row['purchase_date'] or ''}</td>
                <td style="text-align:right;">{_fmt(row['purchase_price'])}</td>
                <td style="text-align:right;">{_fmt(row['book_value'])}</td>
                <td style="text-align:right;">{_fmt(row['depr_this_year'])}{depr_badge}</td>
                <td style="color:{status_color};">{_status_label(row['status'])}</td>
                <td>
                    <a href="/assets/view?id={row['id']}">Details</a> |
                    <a href="/assets/edit?id={row['id']}">Bearbeiten</a>
                </td>
            </tr>'''
        s += '</table>'

    s += Footer()
    return s


# ─── PageAssetView ────────────────────────────────────────────────────────────

def PageAssetView(db: Database, asset_id: int):
    """Detailed asset view with AfA plan table and extensions"""
    asset = db.get_asset_by_id(asset_id)
    if not asset:
        return Header1('assets') + Header2() + Header3() + '<p>Anlage nicht gefunden.</p>' + Footer()

    # asset fields: ID=0, InventoryNumber=1, Name=2, Description=3, AssetCategory_ID=4,
    #   COA_ID=5, PurchaseDate=6, PurchasePrice=7, UsefulLifeYears=8, DepreciationMethod=9,
    #   SerialNumber=10, Location=11, Supplier_ID=12, Document_ID=13, Booking_ID=14,
    #   SaleDate=15, SalePrice=16, Status=17, Notes=18, Parent_ID=19, CreatedAt=20,
    #   CategoryName=21, SupplierName=22
    inv_number = asset[1] or ''
    name = asset[2]
    description = asset[3] or ''
    purchase_date = asset[6]
    purchase_price = asset[7]
    useful_life = asset[8]
    method = asset[9]
    serial = asset[10] or ''
    location = asset[11] or ''
    sale_date = asset[15] or ''
    sale_price = asset[16]
    status = asset[17]
    notes = asset[18] or ''
    cat_name = asset[21] if len(asset) > 21 else ''
    supplier_name = asset[22] if len(asset) > 22 else ''

    # AfA plan
    plan = db.calculate_depreciation_plan(purchase_price, purchase_date, useful_life, method)
    booked = {e[2]: e for e in db.get_depreciations_for_asset(asset_id)}  # year → row
    today = datetime.date.today()
    current_year = today.year

    # Children / extensions
    children = db.get_asset_children(asset_id)

    # Accounts for AfA booking dialog
    accounts = db.fetch_accounts()
    coa_rows = db.fetch_chart_of_accounts()

    s = Header1('assets')
    submenu = f'<a href="/assets">Anlagen</a> → <span id="ActivePage">{name}</span>'
    s += Header2(submenu)
    s += Header3()

    s += f'<h2>{inv_number} – {name}</h2>'

    # Status badge
    status_color = {'active': 'green', 'sold': '#888', 'scrapped': '#cc0000'}.get(status, '#000')
    s += f'<p>Status: <strong style="color:{status_color};">{_status_label(status)}</strong></p>'

    # ── Stammdaten ──
    s += '<div class="rectRounded"><h3>Stammdaten</h3>'
    s += '<table style="border-collapse:collapse; width:100%;">'
    rows_data = [
        ('Bezeichnung', name),
        ('Beschreibung', description),
        ('Inventarnummer', inv_number),
        ('Kategorie', cat_name or '–'),
        ('Anschaffungsdatum', purchase_date),
        ('Anschaffungskosten (netto)', _fmt(purchase_price)),
        ('Nutzungsdauer', f'{useful_life} Jahre'),
        ('AfA-Methode', _method_label(method)),
        ('Seriennummer', serial or '–'),
        ('Standort', location or '–'),
        ('Lieferant', supplier_name or '–'),
        ('Notizen', notes or '–'),
    ]
    if status == 'sold':
        rows_data.append(('Verkaufsdatum', sale_date))
        rows_data.append(('Verkaufserlös', _fmt(sale_price)))
    for label, value in rows_data:
        s += f'<tr><td style="padding:4px 10px 4px 0; width:200px; color:#666;">{label}</td><td style="padding:4px 0;">{value}</td></tr>'
    s += '</table>'
    s += f'<p style="margin-top:8px;"><a href="/assets/edit?id={asset_id}">✏️ Bearbeiten</a>'
    if status == 'active':
        s += f' | <a href="#sell_form">💶 Anlage verkaufen/abgehen</a>'
    s += '</p></div>'

    # ── AfA-Plan ──
    s += f'<div class="rectRounded"><h3>AfA-Plan</h3>'
    s += f'<table border="1" style="width:100%; border-collapse:collapse;">'
    s += '<tr><th>Jahr</th><th style="text-align:right;">Buchwert Anfang</th><th style="text-align:right;">AfA</th><th style="text-align:right;">Buchwert Ende</th><th>Methode</th><th>Status</th><th>Aktion</th></tr>'

    for entry in plan:
        yr = entry['year']
        bv_start = entry['book_value_start']
        depr = entry['depreciation']
        bv_end = entry['book_value_end']
        meth = _method_label(entry['method'])
        booked_entry = booked.get(yr)
        if booked_entry:
            depr_status_label = '<span style="color:green;">✓ Gebucht</span>'
            action = f'<a href="/assets/depreciation/view?asset_id={asset_id}&year={yr}">Buchung ansehen</a>'
        elif yr < current_year:
            depr_status_label = '<span style="color:orange;">⚠ Nicht gebucht</span>'
            action = _book_depr_button(asset_id, yr, accounts, coa_rows)
        elif yr == current_year:
            depr_status_label = '<span style="color:#0066cc;">● Laufendes Jahr</span>'
            action = _book_depr_button(asset_id, yr, accounts, coa_rows)
        else:
            depr_status_label = '<span style="color:#aaa;">○ Geplant</span>'
            action = ''
        row_style = ' class="row-ok"' if booked_entry else ''
        s += f'<tr{row_style}><td>{yr}</td><td style="text-align:right;">{_fmt(bv_start)}</td><td style="text-align:right;">{_fmt(depr)}</td><td style="text-align:right;">{_fmt(bv_end)}</td><td>{meth}</td><td>{depr_status_label}</td><td>{action}</td></tr>'

    s += '</table></div>'

    # ── Erweiterungen / Sub-Anlagen ──
    s += '<div class="rectRounded"><h3>Erweiterungen / Nachkäufe</h3>'
    if children:
        s += '<table border="1" style="width:100%; border-collapse:collapse;">'
        s += '<tr><th>Inv.-Nr.</th><th>Bezeichnung</th><th>Datum</th><th style="text-align:right;">AK</th><th style="text-align:right;">Restbuchwert</th><th>Aktionen</th></tr>'
        for ch in children:
            ch_id = ch[0]
            ch_inv = ch[1] or ''
            ch_name = ch[2]
            ch_date = ch[6]
            ch_price = ch[7]
            ch_bv = db.get_book_value_at_date(ch_id)
            s += f'<tr><td>{ch_inv}</td><td>{ch_name}</td><td>{ch_date}</td><td style="text-align:right;">{_fmt(ch_price)}</td><td style="text-align:right;">{_fmt(ch_bv)}</td><td><a href="/assets/view?id={ch_id}">Details</a> | <a href="/assets/edit?id={ch_id}">Bearbeiten</a></td></tr>'
        s += '</table>'
    else:
        s += '<p><em>Keine Erweiterungen vorhanden.</em></p>'
    s += f'<p><a href="/assets/new?parent_id={asset_id}">+ Erweiterung hinzufügen</a></p>'
    s += '</div>'

    # ── Verkauf / Abgang ──
    if status == 'active':
        s += f'''<div class="rectRounded" id="sell_form"><h3>Anlage verkaufen / Abgang buchen</h3>
        <form method="POST" action="/assets/sell">
            <input type="hidden" name="asset_id" value="{asset_id}">
            <table class="form-table">
                <tr><td>Datum:</td><td><input type="date" name="sale_date" required></td></tr>
                <tr><td>Erlös (0 = Verschrottung):</td><td><input type="number" name="sale_price" value="0" min="0" step="0.01"> €</td></tr>
                <tr><td></td><td><input type="submit" value="Abgang buchen"></td></tr>
            </table>
        </form></div>'''

    s += Footer()
    return s


def _book_depr_button(asset_id, year, accounts, coa_rows):
    """Render inline form to book depreciation for a specific year."""
    form_id = f"depr_form_{asset_id}_{year}"
    account_options = ''.join(
        f'<option value="{a[0]}">{a[1]}</option>' for a in accounts
    )
    # Filter likely expense accounts (SKR 03/04 Abschreibungen ~ 4830)
    coa_options = ''.join(
        f'<option value="{c[0]}">{c[2]} {c[3]}</option>' for c in coa_rows
    )
    return f'''
        <button type="button" onclick="document.getElementById('{form_id}').style.display='block'; this.style.display='none';"
            style="font-size:0.85em; padding:2px 8px;">AfA buchen</button>
        <div id="{form_id}" class="rectRounded" style="display:none; margin-top:4px;">
            <form method="POST" action="/assets/depreciate">
                <input type="hidden" name="asset_id" value="{asset_id}">
                <input type="hidden" name="year" value="{year}">
                <table class="form-table" style="font-size:0.85em;">
                    <tr><td>Buchungskonto:</td><td>
                        <select name="account_id" style="width:180px;">{account_options}</select>
                    </td></tr>
                    <tr><td>Aufwandskonto (SKR):</td><td>
                        <select name="coa_id" style="width:180px;">{coa_options}</select>
                    </td></tr>
                    <tr><td>Beschreibung:</td><td>
                        <input type="text" name="description" placeholder="optional" style="width:180px;">
                    </td></tr>
                    <tr><td></td><td>
                        <input type="submit" value="✓ Jetzt buchen">
                        <button type="button" onclick="document.getElementById('{form_id}').style.display='none';">Abbrechen</button>
                    </td></tr>
                </table>
            </form>
        </div>'''


# ─── PageAssetEdit ────────────────────────────────────────────────────────────

def PageAssetEdit(db: Database, asset_id=None, parent_id=None):
    """Create or edit an asset"""
    asset = db.get_asset_by_id(asset_id) if asset_id else None
    categories = db.fetch_asset_categories()
    suppliers = db.fetch_contacts(contact_type='supplier') + db.fetch_contacts(contact_type='other')
    coa_rows = db.fetch_chart_of_accounts()
    documents = db.fetch_receipts()
    accounts = db.fetch_accounts()
    parent_asset = db.get_asset_by_id(parent_id) if parent_id else None

    is_edit = asset is not None
    page_title = f"Anlage bearbeiten" if is_edit else "Neue Anlage anlegen"
    action = "/assets/update" if is_edit else "/assets/add"

    # Pre-fill values
    v = {
        'name': asset[2] if asset else '',
        'description': asset[3] if asset else '',
        'cat': asset[4] if asset else '',
        'coa': asset[5] if asset else '',
        'purchase_date': asset[6] if asset else '',
        'purchase_price': asset[7] if asset else '',
        'useful_life': asset[8] if asset else '',
        'method': asset[9] if asset else 'linear',
        'serial': asset[10] if asset else '',
        'location': asset[11] if asset else '',
        'supplier': asset[12] if asset else '',
        'document': asset[13] if asset else '',
        'booking': asset[14] if asset else '',
        'notes': asset[18] if asset else '',
        'status': asset[17] if asset else 'active',
        'parent_id': asset[19] if asset else (parent_id or ''),
    }

    s = Header1('assets')
    submenu = f'<a href="/assets">Anlagen</a> → <span id="ActivePage">{page_title}</span>'
    s += Header2(submenu)
    s += Header3()

    s += f'<h2>{page_title}</h2>'

    if parent_asset:
        s += f'<p>🔗 Erweiterung von: <strong><a href="/assets/view?id={parent_asset[0]}">{parent_asset[2]}</a></strong></p>'

    s += f'<form method="POST" action="{action}">'
    if is_edit:
        s += f'<input type="hidden" name="asset_id" value="{asset_id}">'
    s += f'<input type="hidden" name="parent_id" value="{v["parent_id"]}">'

    # Category select – with JS prefill of useful_life and method
    cat_json = '{'
    for c in categories:
        method_val = 'degressive' if c[3] in ('degressive', 'both') else 'linear'
        cat_json += f'"{c[0]}": {{"years": {c[2]}, "method": "{method_val}"}}, '
    cat_json += '}'

    cat_options = '<option value="">-- keine Kategorie --</option>'
    for c in categories:
        sel = 'selected' if str(v['cat']) == str(c[0]) else ''
        cat_options += f'<option value="{c[0]}" {sel}>{c[1]} ({c[2]} J., {_method_label(c[3])})</option>'

    coa_options = '<option value="">-- keines --</option>'
    for c in coa_rows:
        sel = 'selected' if str(v['coa']) == str(c[0]) else ''
        coa_options += f'<option value="{c[0]}" {sel}>{c[2]} {c[3]}</option>'

    supplier_options = '<option value="">-- keiner --</option>'
    for c in suppliers:
        sel = 'selected' if str(v['supplier']) == str(c[0]) else ''
        supplier_options += f'<option value="{c[0]}" {sel}>{c[3] or c[4] or ""} ({c[1]})</option>'

    doc_options = '<option value="">-- kein Beleg --</option>'
    for d in documents:
        sel = 'selected' if str(v['document']) == str(d[0]) else ''
        doc_options += f'<option value="{d[0]}" {sel}>{d[1]} – {d[2]}</option>'

    method_lin = 'selected' if v['method'] == 'linear' else ''
    method_deg = 'selected' if v['method'] == 'degressive' else ''

    s += f'''
    <table class="form-table">
        <tr><td>Bezeichnung:*</td>
            <td><input type="text" name="name" value="{v['name']}" required></td></tr>
        <tr><td>Beschreibung:</td>
            <td><input type="text" name="description" value="{v['description']}"></td></tr>
        <tr><td>AfA-Kategorie:</td>
            <td><select name="asset_category_id" id="cat_select" onchange="prefillFromCategory()">{cat_options}</select></td></tr>
        <tr><td>SKR-Konto (Anlage):</td>
            <td><select name="coa_id">{coa_options}</select></td></tr>
        <tr><td>Anschaffungsdatum:*</td>
            <td><input type="date" name="purchase_date" value="{v['purchase_date']}" required onchange="updatePreview()"></td></tr>
        <tr><td>Anschaffungskosten (netto €):*</td>
            <td><input type="number" name="purchase_price" id="purchase_price" value="{v['purchase_price']}"
                min="0" step="0.01" required onchange="updatePreview()">
                <span class="muted"> ≤ 800 € → GWG-Sofortabschreibung</span></td></tr>
        <tr><td>Nutzungsdauer (Jahre):*</td>
            <td><input type="number" name="useful_life_years" id="useful_life" value="{v['useful_life']}"
                min="1" max="50" required onchange="updatePreview()"></td></tr>
        <tr><td>AfA-Methode:*</td>
            <td><select name="depreciation_method" id="depr_method" onchange="updatePreview()">
                <option value="linear" {method_lin}>Linear</option>
                <option value="degressive" {method_deg}>Degressiv (25%)</option>
            </select></td></tr>
        <tr><td>Seriennummer:</td>
            <td><input type="text" name="serial_number" value="{v['serial']}"></td></tr>
        <tr><td>Standort:</td>
            <td><input type="text" name="location" value="{v['location']}"></td></tr>
        <tr><td>Lieferant:</td>
            <td><select name="supplier_id">{supplier_options}</select></td></tr>
        <tr><td>Beleg (Eingangsrechnung):</td>
            <td><select name="document_id">{doc_options}</select></td></tr>
        <tr><td>Notizen:</td>
            <td><textarea name="notes" rows="3">{v['notes']}</textarea></td></tr>
    '''

    if is_edit:
        status_opts = ''
        for val, label in [('active', 'Aktiv'), ('sold', 'Verkauft'), ('scrapped', 'Abgang')]:
            sel = 'selected' if v['status'] == val else ''
            status_opts += f'<option value="{val}" {sel}>{label}</option>'
        s += f'<tr><td>Status:</td><td><select name="status">{status_opts}</select></td></tr>'

    s += f'''
        <tr><td></td><td>
            <input type="submit" value="{'Anlage aktualisieren' if is_edit else 'Anlage anlegen'}"
                class="coloredButton btn-sm btn-indigo">
            <a href="/assets" class="coloredButton btn-sm btn-gray">Abbrechen</a>
        </td></tr>
    </table>
    </form>
    '''

    # AfA preview table (JavaScript-rendered via server-side for simplicity)
    s += '''
    <div id="afa_preview" class="rectRounded" style="display:none;">
        <h3>AfA-Vorschau</h3>
        <p><em id="afa_gwg_hint" style="color:orange; display:none;">💡 Kaufpreis ≤ 800 € → Sofortabschreibung im Anschaffungsjahr (GWG)</em></p>
        <table border="1" style="border-collapse:collapse;" id="afa_table">
            <tr><th>Jahr</th><th style="text-align:right;">AK/Buchwert</th><th style="text-align:right;">AfA</th><th style="text-align:right;">Restbuchwert</th><th>Methode</th></tr>
        </table>
    </div>
    '''

    s += f'''
    <script>
        const catData = {cat_json};

        function prefillFromCategory() {{
            const catId = document.getElementById('cat_select').value;
            if (catId && catData[catId]) {{
                document.getElementById('useful_life').value = catData[catId].years;
                document.getElementById('depr_method').value = catData[catId].method;
                updatePreview();
            }}
        }}

        function updatePreview() {{
            const price = parseFloat(document.querySelector('[name="purchase_price"]').value) || 0;
            const dateStr = document.querySelector('[name="purchase_date"]').value;
            const years = parseInt(document.getElementById('useful_life').value) || 0;
            const method = document.getElementById('depr_method').value;

            if (!price || !dateStr || !years) {{
                document.getElementById('afa_preview').style.display = 'none';
                return;
            }}

            const plan = calcPlan(price, dateStr, years, method);
            renderPlan(plan);
            document.getElementById('afa_preview').style.display = 'block';
        }}

        function calcPlan(price, dateStr, years, method) {{
            const plan = [];
            const pd = new Date(dateStr);
            const purchaseYear = pd.getFullYear();
            const monthsFirst = 13 - (pd.getMonth() + 1);

            // GWG
            if (price <= 800) {{
                document.getElementById('afa_gwg_hint').style.display = '';
                plan.push({{ year: purchaseYear, bvStart: price, depr: price, bvEnd: 0, method: 'GWG' }});
                return plan;
            }}
            document.getElementById('afa_gwg_hint').style.display = 'none';

            if (method === 'linear') {{
                const annual = price / years;
                let remaining = price;
                let first = true;
                let year = purchaseYear;
                while (remaining > 0.005) {{
                    let depr = first ? round2(annual * monthsFirst / 12) : round2(Math.min(annual, remaining));
                    first = false;
                    const bvEnd = round2(Math.max(0, remaining - depr));
                    plan.push({{ year, bvStart: round2(remaining), depr, bvEnd, method: 'Linear' }});
                    remaining = bvEnd;
                    year++;
                }}
            }} else {{
                const degRate = 0.25;
                let remaining = price;
                let year = purchaseYear;
                let first = true;
                while (remaining > 0.005) {{
                    const yearsElapsed = year - purchaseYear;
                    const linDepr = remaining / Math.max(1, years - yearsElapsed);
                    const degDepr = remaining * degRate;
                    const useLin = linDepr >= degDepr;
                    let depr = useLin ? linDepr : degDepr;
                    if (first) {{ depr = depr * monthsFirst / 12; first = false; }}
                    depr = round2(Math.min(depr, remaining));
                    const bvEnd = round2(Math.max(0, remaining - depr));
                    plan.push({{ year, bvStart: round2(remaining), depr, bvEnd, method: useLin ? 'Linear' : 'Degressiv' }});
                    remaining = bvEnd;
                    year++;
                }}
            }}
            return plan;
        }}

        function renderPlan(plan) {{
            const tbody = document.getElementById('afa_table');
            // remove old rows except header
            while (tbody.rows.length > 1) tbody.deleteRow(1);
            plan.forEach(e => {{
                const tr = tbody.insertRow();
                tr.innerHTML = `<td>${{e.year}}</td>
                    <td style="text-align:right;">${{fmtEur(e.bvStart)}}</td>
                    <td style="text-align:right;">${{fmtEur(e.depr)}}</td>
                    <td style="text-align:right;">${{fmtEur(e.bvEnd)}}</td>
                    <td>${{e.method}}</td>`;
            }});
        }}

        function fmtEur(v) {{
            return v.toFixed(2).replace('.', ',') + ' €';
        }}
        function round2(v) {{ return Math.round(v * 100) / 100; }}

        // Initial preview if editing
        updatePreview();
    </script>
    '''

    s += Footer()
    return s


# ─── PageAssetCategories ──────────────────────────────────────────────────────

def PageAssetCategories(db: Database):
    """Manage AfA categories"""
    categories = db.fetch_asset_categories()
    coa_rows = db.fetch_chart_of_accounts()

    coa_map = {str(c[0]): f"{c[2]} {c[3]}" for c in coa_rows}

    s = Header1('assets')
    submenu = '<a href="/assets">Anlagen</a> → <span id="ActivePage">AfA-Kategorien</span>'
    s += Header2(submenu)
    s += Header3()

    s += '<h2>AfA-Kategorien verwalten</h2>'

    coa_options = '<option value="">-- keines --</option>'
    for c in coa_rows:
        coa_options += f'<option value="{c[0]}">{c[2]} {c[3]}</option>'

    s += f'''
    <h3>Neue Kategorie</h3>
    <form method="POST" action="/asset_categories/add">
        <table class="form-table">
            <tr><td>Bezeichnung:*</td><td><input type="text" name="name" required style="width:300px;"></td></tr>
            <tr><td>Nutzungsdauer (Jahre):*</td><td><input type="number" name="useful_life_years" min="1" max="50" required style="width:80px;"></td></tr>
            <tr><td>AfA-Methode:*</td><td>
                <select name="depreciation_method">
                    <option value="linear">Linear</option>
                    <option value="degressive">Degressiv</option>
                    <option value="both">Linear oder Degressiv</option>
                </select>
            </td></tr>
            <tr><td>SKR-Standardkonto:</td><td><select name="coa_id" style="width:304px;">{coa_options}</select></td></tr>
            <tr><td>Notizen:</td><td><input type="text" name="notes" style="width:300px;"></td></tr>
            <tr><td></td><td><input type="submit" value="Kategorie hinzufügen"></td></tr>
        </table>
    </form>
    '''

    s += '<h3>Bestehende Kategorien</h3>'
    s += '<table border="1" style="width:100%; border-collapse:collapse;">'
    s += '<tr><th>ID</th><th>Bezeichnung</th><th>Jahre</th><th>Methode</th><th>SKR-Konto</th><th>Notizen</th><th>Aktionen</th></tr>'

    for c in categories:
        coa_label = coa_map.get(str(c[3]), '–') if c[3] else '–'
        s += f'''<tr>
            <td>{c[0]}</td>
            <td>{c[1]}</td>
            <td style="text-align:center;">{c[2]}</td>
            <td>{_method_label(c[3])}</td>
            <td>{coa_label}</td>
            <td style="font-size:0.85em; color:#666;">{c[5] or ''}</td>
            <td>
                <a href="/asset_categories/edit?id={c[0]}">Bearbeiten</a> |
                <a href="javascript:void(0);"
                   onclick="appConfirmHref('/asset_categories/delete?id={c[0]}', 'Kategorie wirklich löschen?')">Löschen</a>
            </td>
        </tr>'''

    s += '</table>'
    s += Footer()
    return s


def PageAssetCategoryEdit(db: Database, category_id: int):
    """Edit an existing AfA category"""
    cat = db.get_asset_category_by_id(category_id)
    if not cat:
        return Header1('assets') + Header2() + Header3() + '<p>Kategorie nicht gefunden.</p>' + Footer()

    coa_rows = db.fetch_chart_of_accounts()
    coa_options = '<option value="">-- keines --</option>'
    for c in coa_rows:
        sel = 'selected' if cat[3] and str(cat[3]) == str(c[0]) else ''
        coa_options += f'<option value="{c[0]}" {sel}>{c[2]} {c[3]}</option>'

    method_opts = ''
    for val, label in [('linear', 'Linear'), ('degressive', 'Degressiv'), ('both', 'Linear oder Degressiv')]:
        sel = 'selected' if cat[3] == val else ''
        method_opts += f'<option value="{val}" {sel}>{label}</option>'

    s = Header1('assets')
    submenu = '<a href="/assets">Anlagen</a> → <a href="/asset_categories">AfA-Kategorien</a> → <span id="ActivePage">Bearbeiten</span>'
    s += Header2(submenu)
    s += Header3()
    s += f'''
    <h2>AfA-Kategorie bearbeiten</h2>
    <form method="POST" action="/asset_categories/update">
        <input type="hidden" name="category_id" value="{cat[0]}">
        <table class="form-table">
            <tr><td>Bezeichnung:*</td><td><input type="text" name="name" value="{cat[1]}" required style="width:300px;"></td></tr>
            <tr><td>Nutzungsdauer (Jahre):*</td><td><input type="number" name="useful_life_years" value="{cat[2]}" min="1" max="50" required style="width:80px;"></td></tr>
            <tr><td>AfA-Methode:*</td><td><select name="depreciation_method">{method_opts}</select></td></tr>
            <tr><td>SKR-Standardkonto:</td><td><select name="coa_id" style="width:304px;">{coa_options}</select></td></tr>
            <tr><td>Notizen:</td><td><input type="text" name="notes" value="{cat[4] or ''}" style="width:300px;"></td></tr>
            <tr><td></td><td><input type="submit" value="Aktualisieren"></td></tr>
        </table>
    </form>
    <p><a href="/asset_categories">Zurück zur Übersicht</a></p>
    '''
    s += Footer()
    return s
