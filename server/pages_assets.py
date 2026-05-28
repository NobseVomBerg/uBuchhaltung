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

def PageAssets(db: Database, status_filter='', edit_id=None, new_parent_id=None):
    """Anlagenverzeichnis (grid2Cols): Liste links, Formular + Details rechts.

    Rechts: ohne Auswahl ein 'Neue Anlage'-Formular; mit Auswahl das
    Bearbeiten-Formular plus Detail-Blöcke (AfA-Plan, Erweiterungen, Verkauf).
    """
    assets = db.fetch_assets(status=status_filter if status_filter else None, parent_only=True)
    current_year = datetime.date.today().year

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

        year_entry = next((e for e in plan if e['year'] == current_year), None)
        depr_this_year = year_entry['depreciation'] if year_entry else 0.0

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
            'id': asset_id, 'inv': a[1], 'name': a[2],
            'cat': a[22] if len(a) > 22 else '',   # CategoryName from JOIN
            'purchase_date': purchase_date, 'purchase_price': purchase_price,
            'book_value': book_value, 'depr_this_year': depr_this_year,
            'depr_status': depr_status, 'status': status, 'method': method,
        })

    s = Header1('assets')
    s += Header2()

    # Filter bar
    filter_options = [('', 'Alle'), ('active', 'Aktiv'), ('sold', 'Verkauft'), ('scrapped', 'Abgang')]
    filter_html = '<div class="rowWithObjects"><label>Status:</label>'
    for val, label in filter_options:
        active = 'font-weight:bold; text-decoration:underline;' if status_filter == val else ''
        filter_html += f'<a href="/assets?status={val}" style="{active}">{label}</a>'
    filter_html += '</div>'
    s += Header3(filter_html)

    # ── Rechte Spalte: Formular (+ Detail-Blöcke bei Auswahl) ──
    edit_asset = db.get_asset_by_id(edit_id) if edit_id is not None else None
    parent_asset = db.get_asset_by_id(new_parent_id) if new_parent_id is not None else None

    s += '<div class="grid2Cols gridMain">'
    s += '<div class="gridRightCol gridMiddle" style="order:2">'
    s += _asset_form(db, edit_asset, parent_asset)
    if edit_asset:
        s += _asset_details(db, edit_asset)
    s += '</div><!-- Ende gridRightCol -->'

    # ── Linke Spalte: Liste ──
    s += '<div class="gridLeftCol" style="order:1">'
    s += (f'<div class="rectRounded">'
          f'Anschaffungskosten (aktiv): {_fmt(total_purchase)} | '
          f'Gesamtrestbuchwert: {_fmt(total_book_value)} | '
          f'AfA {current_year} gebucht: {_fmt(total_depr_posted)} | '
          f'AfA {current_year} geplant: {_fmt(total_depr_planned)}</div>')
    if not asset_rows:
        s += '<p><em>Keine Anlagen gefunden.</em></p>'
    else:
        s += "<table>"
        s += ("<tr><th>Inv.-Nr.</th><th>Bezeichnung</th><th>Kategorie</th><th>Anschaff.-datum</th>"
              "<th style='text-align:right;'>AK</th><th style='text-align:right;'>Restbuchwert</th>"
              f"<th style='text-align:right;'>AfA {current_year}</th><th>Status</th><th>Aktionen</th></tr>")
        for row in asset_rows:
            aid = row['id']
            depr_badge = ''
            if row['depr_this_year'] > 0:
                if row['depr_status'] == 'posted':
                    depr_badge = " <span style='color:green;font-size:0.8em;'>✓ gebucht</span>"
                else:
                    depr_badge = " <span style='color:orange;font-size:0.8em;'>● offen</span>"
            status_color = {'active': 'green', 'sold': '#888', 'scrapped': '#cc0000'}.get(row['status'], '#000')
            s += (f"<tr><td><a href='javascript:void(0)' onclick='openEditForm(\"/assets/edit?id={aid}\")'>{row['inv'] or ''}</a></td>"
                  f"<td><a href='javascript:void(0)' onclick='openEditForm(\"/assets/edit?id={aid}\")'>{row['name']}</a></td>"
                  f"<td>{row['cat'] or ''}</td><td>{row['purchase_date'] or ''}</td>"
                  f"<td style='text-align:right;'>{_fmt(row['purchase_price'])}</td>"
                  f"<td style='text-align:right;'>{_fmt(row['book_value'])}</td>"
                  f"<td style='text-align:right;'>{_fmt(row['depr_this_year'])}{depr_badge}</td>"
                  f"<td style='color:{status_color};'>{_status_label(row['status'])}</td>"
                  f"<td><a href='javascript:void(0)' onclick='openEditForm(\"/assets/edit?id={aid}\")' class='action-icon' title='Öffnen / Bearbeiten'>&#9998;</a></td></tr>")
        s += "</table>"
    s += '</div><!-- Ende gridLeftCol --></div><!-- Ende grid2Cols -->'
    s += '''
    <script>
        function openEditForm(url) {
            fetch(url)
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
                        history.pushState({}, '', url);
                    }
                })
                .catch(() => { window.location.href = url; });
        }
    </script>
    '''
    s += Footer()
    return s


def PageAssetEdit(db: Database, asset_id=None, parent_id=None):
    """Thin-Wrapper – Anlage anlegen/bearbeiten in der kombinierten Seite."""
    return PageAssets(db, edit_id=asset_id, new_parent_id=parent_id)


def PageAssetView(db: Database, asset_id):
    """Thin-Wrapper – Detail/Bearbeiten in der kombinierten Seite (rechte Spalte)."""
    return PageAssets(db, edit_id=asset_id)


# ─── PageAssetView ────────────────────────────────────────────────────────────

def _asset_details(db: Database, asset):
    """Render-Block: Detail-Inhalte einer Anlage (AfA-Plan inkl. Buchen,
    Erweiterungen/Nachkäufe, Verkauf/Abgang) für die rechte Spalte.
    Die Stammdaten kommen aus dem Bearbeiten-Formular darüber."""
    asset_id = asset[0]
    purchase_date = asset[6]
    purchase_price = asset[7]
    useful_life = asset[8]
    method = asset[9]
    status = asset[17]

    plan = db.calculate_depreciation_plan(purchase_price, purchase_date, useful_life, method)
    booked = {e[2]: e for e in db.get_depreciations_for_asset(asset_id)}  # year → row
    current_year = datetime.date.today().year
    children = db.get_asset_children(asset_id)
    accounts = db.fetch_accounts()
    coa_rows = db.fetch_chart_of_accounts()

    # ── AfA-Plan ──
    s = '<div class="rectRounded"><h3>AfA-Plan</h3>'
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
            s += f'<tr><td>{ch_inv}</td><td>{ch_name}</td><td>{ch_date}</td><td style="text-align:right;">{_fmt(ch_price)}</td><td style="text-align:right;">{_fmt(ch_bv)}</td><td><a href="/assets/edit?id={ch_id}">Öffnen</a></td></tr>'
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
                <tr><td>Erlös in € (0 = Verschrottung):</td><td><input type="number" name="sale_price" value="0" min="0" step="0.01"></td></tr>
                <tr><td></td><td><input type="submit" value="Abgang buchen" class="coloredButton btn-sm bg-indigo"></td></tr>
            </table>
        </form></div>'''

    return s


def _book_depr_button(asset_id, year, accounts, coa_rows):
    """Render inline form to book depreciation for a specific year."""
    form_id = f"depr_form_{asset_id}_{year}"
    account_options = ''.join(
        f'<option value="{a[0]}">{a[1]}</option>' for a in accounts
    )
    # Filter likely expense accounts (SKR 03/04 Abschreibungen ~ 4830)
    coa_options = ''.join(
        f'<option value="{c[0]}">{c[2]} {c[3]}</option>'
        for c in coa_rows if (c[7] if len(c) > 7 else 1)
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

def _asset_form(db: Database, asset=None, parent_asset=None):
    """Render-Block: Anlage-Formular (Neu/Bearbeiten) für die rechte Spalte."""
    categories = db.fetch_asset_categories()
    suppliers = db.fetch_contacts(contact_type='supplier') + db.fetch_contacts(contact_type='other')
    coa_rows = db.fetch_chart_of_accounts()
    documents = db.fetch_receipts()

    is_edit = asset is not None
    asset_id = asset[0] if asset else None
    page_title = "Anlage bearbeiten" if is_edit else "Neue Anlage"
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
        'parent_id': asset[19] if asset else (parent_asset[0] if parent_asset else ''),
    }

    s = '<div class="rectRounded">'
    s += f'<h2>{page_title}</h2>'

    if parent_asset:
        s += f'<p>🔗 Erweiterung von: <strong>{parent_asset[2]}</strong></p>'

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
        if not (c[7] if len(c) > 7 else 1) and not sel:
            continue
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
                class="coloredButton btn-sm bg-indigo">
            <button type="button" onclick="window.location.href='/assets'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>
        </td></tr>
    </table>
    </form>
    </div>
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

    return s


# ─── PageAssetCategories ──────────────────────────────────────────────────────

def PageAssetCategories(db: Database, edit_category_id=None):
    """AfA-Kategorien (Stammdaten): Übersicht links, Inline-Formular rechts.

    Bearbeiten lädt die Kategorie ins rechte Formular (wie PageArticles);
    ohne edit_category_id ist es ein leeres 'Neue Kategorie'-Formular.
    """
    categories = db.fetch_asset_categories()
    coa_rows = db.fetch_chart_of_accounts()
    coa_map = {str(c[0]): f"{c[2]} {c[3]}" for c in coa_rows}

    # AssetCategories (SELECT *): [0]ID [1]Name [2]UsefulLifeYears
    #                             [3]DepreciationMethod [4]COA_ID [5]Notes
    edit_cat = db.get_asset_category_by_id(edit_category_id) if edit_category_id else None

    s = Header1('masterdata')
    submenu = '<a href="/masterdata">Stammdaten</a> → <span id="ActivePage">📂 AfA-Kategorien</span>'
    s += Header2(submenu)
    s += Header3()

    # Formular-Werte (Bearbeiten oder Neu)
    form_title = "Kategorie bearbeiten" if edit_cat else "Neue Kategorie"
    ec_name   = edit_cat[1] if edit_cat else ''
    ec_years  = edit_cat[2] if edit_cat else ''
    ec_method = edit_cat[3] if edit_cat else 'linear'
    ec_coa    = edit_cat[4] if edit_cat else ''
    ec_notes  = (edit_cat[5] or '') if edit_cat else ''

    method_opts = ''
    for val, label in [('linear', 'Linear'), ('degressive', 'Degressiv'), ('both', 'Linear oder Degressiv')]:
        sel = 'selected' if ec_method == val else ''
        method_opts += f'<option value="{val}" {sel}>{label}</option>'

    coa_options = '<option value="">-- keines --</option>'
    for c in coa_rows:
        sel = 'selected' if ec_coa and str(ec_coa) == str(c[0]) else ''
        if not (c[7] if len(c) > 7 else 1) and not sel:
            continue
        coa_options += f'<option value="{c[0]}" {sel}>{c[2]} {c[3]}</option>'

    id_row = (f'<input type="hidden" name="category_id" value="{edit_cat[0]}">'
              if edit_cat else '')

    if edit_cat:
        action_buttons = (
            '<input type="submit" value="Aktualisieren" class="coloredButton btn-sm bg-green">'
            '<button type="button" onclick="window.location.href=\'/asset_categories\'" class="coloredButton btn-sm bg-gray">← Abbrechen</button>'
        )
    else:
        action_buttons = ('<input type="submit" value="Kategorie hinzufügen" '
                          'formaction="/asset_categories/add" class="coloredButton btn-sm bg-green">')

    s += f'''
    <div class="grid2Cols gridMain">
    <div class="gridRightCol" style="order:2">
        <div class="rectRounded">
        <h2>{form_title}</h2>
        <form method="POST" action="/asset_categories/update">
            {id_row}
            <table class="form-table">
                <tr><td>Bezeichnung:*</td><td><input type="text" name="name" value="{ec_name}" required></td></tr>
                <tr><td>Nutzungsdauer (Jahre):*</td><td><input type="number" name="useful_life_years" value="{ec_years}" min="1" max="50" required></td></tr>
                <tr><td>AfA-Methode:*</td><td><select name="depreciation_method">{method_opts}</select></td></tr>
                <tr><td>SKR-Standardkonto:</td><td><select name="coa_id">{coa_options}</select></td></tr>
                <tr><td>Notizen:</td><td><input type="text" name="notes" value="{ec_notes}"></td></tr>
                <tr><td></td><td>{action_buttons}</td></tr>
            </table>
        </form>
        </div>
    </div>
    <div class="gridLeftCol" style="order:1">
        <table>
            <tr><th>ID</th><th>Bezeichnung</th><th>Jahre</th><th>Methode</th><th>SKR-Konto</th><th>Notizen</th><th>Aktionen</th></tr>
    '''
    for c in categories:
        coa_label = coa_map.get(str(c[4]), '–') if c[4] else '–'
        s += f'''<tr>
            <td>{c[0]}</td>
            <td>{c[1]}</td>
            <td style="text-align:center;">{c[2]}</td>
            <td>{_method_label(c[3])}</td>
            <td>{coa_label}</td>
            <td class="muted">{c[5] or ''}</td>
            <td>
                <a href="javascript:void(0)" onclick="openEditForm('/asset_categories/edit?id={c[0]}')" class="action-icon" title="Bearbeiten">&#9998;</a>
                <a href="javascript:void(0);" class="action-icon delete-icon" title="Löschen"
                   onclick="appConfirmHref('/asset_categories/delete?id={c[0]}', 'Kategorie wirklich löschen?')">&#128465;</a>
            </td>
        </tr>'''

    s += '</table>'
    s += '</div><!-- Ende gridLeftCol --></div><!-- Ende grid2Cols -->'
    s += '''
    <script>
        function openEditForm(url) {
            fetch(url)
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
                        history.pushState({}, '', url);
                    }
                })
                .catch(() => { window.location.href = url; });
        }
    </script>
    '''
    s += Footer()
    return s


def PageAssetCategoryEdit(db: Database, category_id: int):
    """Thin-Wrapper – Inline-Bearbeiten in der kombinierten Kategorie-Seite."""
    return PageAssetCategories(db, edit_category_id=category_id)
