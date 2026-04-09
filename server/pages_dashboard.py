"""
Dashboard page – booking-based financial overview.

All figures come from Bookings (bank type), not from Invoices,
so that imported WISO/DATEV data is always reflected.
"""
from datetime import datetime
from db import Database
from .pages import Header1, Header2, Header3, Footer


def PageDashboard(db: Database, date_from: str = '', date_to: str = '',
                  account_ids: list | None = None):
    """Generate dashboard with statistics and charts.

    Args:
        db:          Database instance
        date_from:   Start date (YYYY-MM-DD). Default: Jan 1 of current year
        date_to:     End date   (YYYY-MM-DD). Default: Dec 31 of current year
        account_ids: Optional list of Account-IDs to include (None = all)
    """

    current_year = datetime.now().year

    # ── Defaults ──────────────────────────────────────────────────────
    if not date_from:
        date_from = f'{current_year}-01-01'
    if not date_to:
        date_to = f'{current_year}-12-31'

    # Display label
    if (date_from[5:] == '01-01' and date_to[5:] == '12-31'
            and date_from[:4] == date_to[:4]):
        range_label = date_from[:4]
    else:
        range_label = f'{date_from} &ndash; {date_to}'

    # ── Data ──────────────────────────────────────────────────────────
    totals  = db.get_dashboard_totals(date_from, date_to, account_ids)
    monthly = db.get_dashboard_monthly(date_from, date_to, account_ids)

    income   = totals['income']
    private  = totals['private']
    expense  = totals['expense']
    balance  = totals['balance']
    bank_cnt = totals['bank_count']
    unlinked = totals['unlinked_count']

    # ── Accounts (for Header2 checkboxes) ─────────────────────────────
    accounts = db.fetch_accounts()

    # Build a set of selected account IDs for checkbox state
    all_selected = account_ids is None
    sel_ids = set(account_ids) if account_ids else set()

    header2_content = (
        '<input type="checkbox" id="account_all"'
        + (' checked' if all_selected else '')
        + ' onchange="toggleAllDashAccounts()"> '
        '<label for="account_all"><strong>Alle</strong></label> &nbsp;|&nbsp; '
    )
    for acct in accounts:
        aid  = acct[0]
        name = acct[1]
        chk  = ' checked' if all_selected or aid in sel_ids else ''
        header2_content += (
            f'<input type="checkbox" id="dacct_{aid}" value="{aid}"{chk}'
            f' onchange="syncDashAccounts()"> '
            f'<label for="dacct_{aid}">{name}</label> &nbsp; '
        )

    # ── Year buttons ──────────────────────────────────────────────────
    year_buttons = ''
    for y in range(current_year, current_year - 5, -1):
        active_cls = " class='active'" if range_label == str(y) else ''
        year_buttons += (
            f'<button{active_cls} onclick="setDashYear({y})">{y}</button> ')

    header3_content = f'''
        <div style="display:flex;gap:15px;align-items:center;flex-wrap:wrap;">
            <div>
                <label>Von:</label>
                <input type="date" id="dateFrom" value="{date_from}"
                       onchange="applyDashFilter()">
                <label>Bis:</label>
                <input type="date" id="dateTo" value="{date_to}"
                       onchange="applyDashFilter()">
                {year_buttons}
            </div>
        </div>
    '''

    # ── Build page ────────────────────────────────────────────────────
    s = Header1('dashboard')
    s += Header2(header2_content)
    s += Header3(header3_content)

    # ── Key metric cards ──────────────────────────────────────────────
    s += f'''
    <div class="grid-1RowPrefered">
        <div style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
                    color:white;padding:20px;border-radius:10px;
                    box-shadow:0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size:14px;opacity:0.9;">
                Zahlungseing&auml;nge ({range_label})</div>
            <div style="font-size:32px;font-weight:bold;margin:10px 0;">
                {income:,.2f} &euro;</div>
            <div style="font-size:12px;opacity:0.8;">
                Rechnungen &amp; Erstattungen</div>
        </div>

        <div style="background:linear-gradient(135deg,#f093fb 0%,#f5576c 100%);
                    color:white;padding:20px;border-radius:10px;
                    box-shadow:0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size:14px;opacity:0.9;">
                Betriebsausgaben ({range_label})</div>
            <div style="font-size:32px;font-weight:bold;margin:10px 0;">
                {expense:,.2f} &euro;</div>
            <div style="font-size:12px;opacity:0.8;">
                ohne Privatentnahmen</div>
        </div>

        <div style="background:linear-gradient(135deg,#fa709a 0%,#fee140 100%);
                    color:white;padding:20px;border-radius:10px;
                    box-shadow:0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size:14px;opacity:0.9;">
                Privatentnahmen ({range_label})</div>
            <div style="font-size:32px;font-weight:bold;margin:10px 0;">
                {private:,.2f} &euro;</div>
            <div style="font-size:12px;opacity:0.8;">
                SKR 2100&ndash;2199</div>
        </div>

        <div style="background:linear-gradient(135deg,
                    {'#4facfe' if balance >= 0 else '#e53935'} 0%,
                    {'#00f2fe' if balance >= 0 else '#ff6f00'} 100%);
                    color:white;padding:20px;border-radius:10px;
                    box-shadow:0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size:14px;opacity:0.9;">
                Saldo ({range_label})</div>
            <div style="font-size:32px;font-weight:bold;margin:10px 0;">
                {balance:,.2f} &euro;</div>
            <div style="font-size:12px;opacity:0.8;">
                {bank_cnt} Bankbuchungen, {unlinked} offen</div>
        </div>
    </div>
    '''

    # ── Monthly 3-part bar chart ──────────────────────────────────────
    month_names = ['Jan', 'Feb', 'M&auml;r', 'Apr', 'Mai', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']

    # Determine scale: max absolute value across all three categories
    max_val = 1
    for m in range(1, 13):
        d = monthly[m]
        max_val = max(max_val, d['income'], abs(d['private']), abs(d['expense']))

    bar_height = 180  # max bar pixel height

    s += f'''
    <div class="grid-1RowPrefered">
    <div class="rectRounded">
        <h3 style="margin-top:0;">Monatlicher Umsatz {range_label}</h3>
        <div style="display:flex;gap:8px;margin-bottom:10px;font-size:12px;">
            <span style="color:#4facfe;">&#9632; Einnahmen</span>
            <span style="color:#ff6f00;">&#9632; Privatentnahmen</span>
            <span style="color:#e53935;">&#9632; Betriebsausgaben</span>
        </div>
        <div style="height:{bar_height + 80}px;position:relative;margin-top:10px;">
        <div style="display:flex;align-items:flex-end;justify-content:space-around;
                    height:{bar_height + 30}px;
                    border-left:2px solid #ddd;border-bottom:2px solid #ddd;
                    padding:10px 0 0 0;">
    '''

    for m in range(1, 13):
        d = monthly[m]
        h_inc  = (d['income']       / max_val * bar_height) if max_val else 0
        h_priv = (abs(d['private']) / max_val * bar_height) if max_val else 0
        h_exp  = (abs(d['expense']) / max_val * bar_height) if max_val else 0
        tip = (f"Einnahmen: {d['income']:,.2f}\\n"
               f"Privat: {d['private']:,.2f}\\n"
               f"Ausgaben: {d['expense']:,.2f}")
        s += f'''
        <div style="display:flex;flex-direction:column;align-items:center;flex:1;"
             title="{tip}">
            <div style="display:flex;gap:1px;align-items:flex-end;height:{bar_height}px;">
                <div style="background:#4facfe;width:8px;height:{h_inc:.0f}px;
                            border-radius:2px 2px 0 0;"></div>
                <div style="background:#ff6f00;width:8px;height:{h_priv:.0f}px;
                            border-radius:2px 2px 0 0;"></div>
                <div style="background:#e53935;width:8px;height:{h_exp:.0f}px;
                            border-radius:2px 2px 0 0;"></div>
            </div>
            <div style="font-size:11px;margin-top:4px;">{month_names[m-1]}</div>
        </div>
        '''

    s += '</div></div></div></div>'

    # ── Details table (annual summary by category) ────────────────────
    total_inc  = sum(monthly[m]['income']  for m in range(1, 13))
    total_priv = sum(monthly[m]['private'] for m in range(1, 13))
    total_exp  = sum(monthly[m]['expense'] for m in range(1, 13))
    total_bal  = total_inc + total_priv + total_exp

    s += f'''
    <div class="grid-1RowPrefered">
    <div class="rectRounded">
        <h3 style="margin-top:0;">Jahres&uuml;bersicht {range_label}</h3>
        <table style="width:100%;border-collapse:collapse;">
            <tr><th style="text-align:left;">Kategorie</th>
                <th style="text-align:right;">Betrag</th></tr>
            <tr><td>Zahlungseing&auml;nge</td>
                <td style="text-align:right;color:green;">
                    {total_inc:,.2f} &euro;</td></tr>
            <tr><td>Betriebsausgaben</td>
                <td style="text-align:right;color:red;">
                    {total_exp:,.2f} &euro;</td></tr>
            <tr><td>Privatentnahmen</td>
                <td style="text-align:right;color:#ff6f00;">
                    {total_priv:,.2f} &euro;</td></tr>
            <tr style="border-top:2px solid #666;">
                <td><strong>Saldo</strong></td>
                <td style="text-align:right;font-weight:bold;
                    color:{'green' if total_bal >= 0 else 'red'};">
                    {total_bal:,.2f} &euro;</td></tr>
        </table>
    </div>
    '''

    # ── Quick actions ─────────────────────────────────────────────────
    s += '''
    <div class="rectRounded">
        <h3 style="margin-top:0;">Schnellzugriff</h3>
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
            <a href="/invoice/new" class="coloredButton btn-green">
                + Neue Rechnung</a>
            <a href="/invoice?status=sent" class="coloredButton btn-orange">
                Versendete Rechnungen</a>
            <a href="/transactions" class="coloredButton btn-blue">
                Transaktionen</a>
        </div>
    </div>
    </div>
    '''

    # ── JavaScript ────────────────────────────────────────────────────
    s += '''
    <script>
        function _buildDashURL() {
            const f = document.getElementById('dateFrom').value;
            const t = document.getElementById('dateTo').value;
            let url = '/dashboard?from=' + f + '&to=' + t;
            const allCb = document.getElementById('account_all');
            if (!allCb.checked) {
                document.querySelectorAll(
                    'input[type="checkbox"][id^="dacct_"]:checked'
                ).forEach(cb => { url += '&acct=' + cb.value; });
            }
            return url;
        }
        function setDashYear(year) {
            document.getElementById('dateFrom').value = year + '-01-01';
            document.getElementById('dateTo').value   = year + '-12-31';
            window.location.href = _buildDashURL();
        }
        function applyDashFilter() {
            const f = document.getElementById('dateFrom').value;
            const t = document.getElementById('dateTo').value;
            if (f && t) window.location.href = _buildDashURL();
        }
        function toggleAllDashAccounts() {
            const allCb = document.getElementById('account_all');
            document.querySelectorAll('input[type="checkbox"][id^="dacct_"]')
                .forEach(cb => { cb.checked = allCb.checked; });
            window.location.href = _buildDashURL();
        }
        function syncDashAccounts() {
            const cbs = document.querySelectorAll(
                'input[type="checkbox"][id^="dacct_"]');
            const allCb = document.getElementById('account_all');
            allCb.checked = Array.from(cbs).every(cb => cb.checked);
            window.location.href = _buildDashURL();
        }
    </script>
    '''

    s += Footer()
    return s
