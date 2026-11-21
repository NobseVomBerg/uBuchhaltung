# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Dashboard page – booking-based financial overview.

All figures come from Bookings (bank type), not from Invoices,
so that imported WISO/DATEV data is always reflected.
"""
from datetime import datetime
import html as _html
from db import Database
from db.reporting import bank_skr_numbers, euer_section
from money import fmt_eur, round_minor, to_minor
from .pages import Header1, Header2, Header3, Footer


def _esc(value):
    """Wert aus der Datenbank für HTML-Text und -Attribute maskieren (NULL = leer)."""
    return _html.escape(str(value if value is not None else ''), quote=True)


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

    # Der Zeitraum kommt aus Adresse oder Cookie: nur maskiert in HTML einbetten
    h_from = _html.escape(date_from, quote=True)
    h_to = _html.escape(date_to, quote=True)

    # Display label
    if (date_from[5:] == '01-01' and date_to[5:] == '12-31'
            and date_from[:4] == date_to[:4]):
        range_label = _html.escape(date_from[:4], quote=True)
    else:
        range_label = f'{h_from} &ndash; {h_to}'

    # ── Data ──────────────────────────────────────────────────────────
    totals, monthly = db.get_dashboard(date_from, date_to, account_ids)

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
            f'<label for="dacct_{aid}">{_html.escape(str(name))}</label> &nbsp; '
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
                <input type="date" id="dateFrom" value="{h_from}"
                       onchange="applyDashFilter()">
                <label>Bis:</label>
                <input type="date" id="dateTo" value="{h_to}"
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
    <div class="grid4Cols gridMain">
        <div style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
                    color:white;padding:20px;border-radius:10px;
                    box-shadow:0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size:14px;opacity:0.9;">
                Zahlungseing&auml;nge ({range_label})</div>
            <div style="font-size:32px;font-weight:bold;margin:10px 0;">
                {fmt_eur(income)}</div>
            <div style="font-size:12px;opacity:0.8;">
                Rechnungen &amp; Erstattungen</div>
        </div>

        <div style="background:linear-gradient(135deg,#f093fb 0%,#f5576c 100%);
                    color:white;padding:20px;border-radius:10px;
                    box-shadow:0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size:14px;opacity:0.9;">
                Betriebsausgaben ({range_label})</div>
            <div style="font-size:32px;font-weight:bold;margin:10px 0;">
                {fmt_eur(expense)}</div>
            <div style="font-size:12px;opacity:0.8;">
                ohne Privatentnahmen</div>
        </div>

        <div style="background:linear-gradient(135deg,#fa709a 0%,#fee140 100%);
                    color:white;padding:20px;border-radius:10px;
                    box-shadow:0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size:14px;opacity:0.9;">
                Privatentnahmen ({range_label})</div>
            <div style="font-size:32px;font-weight:bold;margin:10px 0;">
                {fmt_eur(private)}</div>
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
                {fmt_eur(balance)}</div>
            <div style="font-size:12px;opacity:0.8;">
                {bank_cnt} Bankbuchungen, {unlinked} offen</div>
        </div>
    '''

    # ── Monthly 3-part bar chart ──────────────────────────────────────

    # Determine scale: max absolute value across all three categories
    max_val = 1
    for d in monthly:
        max_val = max(max_val, d['income'], abs(d['private']), abs(d['expense']))

    bar_height = 180  # max bar pixel height

    s += f'''
    <div class="rectRounded box4">
        <h3>Monatlicher Umsatz {range_label}</h3>
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

    for entry in monthly:
        d = entry
        h_inc  = (d['income']       / max_val * bar_height) if max_val else 0
        h_priv = (abs(d['private']) / max_val * bar_height) if max_val else 0
        h_exp  = (abs(d['expense']) / max_val * bar_height) if max_val else 0
        tip = (f"Einnahmen: {fmt_eur(d['income'], symbol=False)}\\n"
               f"Privat: {fmt_eur(d['private'], symbol=False)}\\n"
               f"Ausgaben: {fmt_eur(d['expense'], symbol=False)}")
        if d['transfers']:
            tip += f"\\nÜberträge: {_esc(fmt_eur(d['transfers'], symbol=False))}"
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
            <div style="font-size:11px;margin-top:4px;">{entry['label']}</div>
        </div>
        '''

    s += '</div></div></div>'

    # ── Details table (annual summary by category) ────────────────────
    total_inc  = sum(entry['income']  for entry in monthly)
    total_priv = sum(entry['private'] for entry in monthly)
    total_exp  = sum(entry['expense'] for entry in monthly)
    total_tr   = sum(entry['transfers'] for entry in monthly)
    total_bal  = total_inc + total_priv + total_exp + total_tr

    # Überträge zwischen eigenen Konten sind weder Einnahme noch Ausgabe: eigene Zeile nur, wenn netto etwas bleibt
    # (auf den Cent – eine Summe von Kommazahlen ist selten genau 0)
    transfers_row = ''
    if round_minor(to_minor(total_tr), 2):       # ab einem Cent – sonst stünde dort „0,00 €“
        transfers_row = ('<tr class="dashTransfers"><td class="hint">&Uuml;bertr&auml;ge zwischen eigenen Konten</td>'
                         f'<td class="hint">{_esc(fmt_eur(total_tr))}</td></tr>')

    s += f'''
    <div class="rectRounded box2">
        <h3>&Uuml;bersicht {range_label}</h3>
        <table style="width:100%;border-collapse:collapse;">
            <tr><th style="text-align:left;">Kategorie</th>
                <th style="text-align:right;">Betrag</th></tr>
            <tr><td>Zahlungseing&auml;nge</td>
                <td style="text-align:right;color:green;">
                    {fmt_eur(total_inc)}</td></tr>
            <tr><td>Betriebsausgaben</td>
                <td style="text-align:right;color:red;">
                    {fmt_eur(total_exp)}</td></tr>
            <tr><td>Privatentnahmen</td>
                <td style="text-align:right;color:#ff6f00;">
                    {fmt_eur(total_priv)}</td></tr>
            {transfers_row}
            <tr style="border-top:2px solid #666;">
                <td><strong>Saldo</strong></td>
                <td style="text-align:right;font-weight:bold;
                    color:{'green' if total_bal >= 0 else 'red'};">
                    {fmt_eur(total_bal)}</td></tr>
        </table>
    </div>
    '''

    # ── EÜR-Auswertung (ersetzt Schnellzugriff) ─────────────────────
    euer_data = db.get_euer_data(date_from, date_to, account_ids)

    # Einnahme, Ausgabe oder „Sonstige Ausgabe“ entscheidet db.reporting.euer_section – dieselbe Regel wie in der
    # betriebswirtschaftlichen Auswertung (Einnahmen = SKR04-Ertragsklasse plus virtuelles USt-Konto 3806;
    # Aufzählungen einzelner Erlöskonten hatten z. B. 4185 (§19) und 4300 (7%) übersehen).
    bank_numbers = bank_skr_numbers(accounts)
    income_rows = [row for row in euer_data if euer_section(row[0], bank_numbers) == 'income']
    expense_rows = [row for row in euer_data if euer_section(row[0], bank_numbers) == 'expense']
    other_expense_rows = [row for row in euer_data if euer_section(row[0], bank_numbers) == 'other']

    income_sum = sum(t for _, _, t in income_rows)
    expense_sum = sum(t for _, _, t in expense_rows)
    other_expense_sum = sum(t for _, _, t in other_expense_rows)
    euer_result = income_sum + expense_sum

    s += f'''
    <div class="rectRounded box2">
        <h3>Einnahmen&uuml;berschussrechnung {range_label}</h3>
        <h4 style="margin-bottom:6px;">Einnahmen</h4>
        <table style="width:100%;border-collapse:collapse;">
            <tr><th style="text-align:left;">SKR-Konto</th>
                <th style="text-align:left;">Bezeichnung</th>
                <th style="text-align:right;">Saldo</th></tr>
    '''
    for nr, name, total in income_rows:
        color = 'green' if total >= 0 else 'red'
        s += (f"<tr><td>{_esc(nr)}</td><td>{_esc(name)}</td>"
              f"<td style='text-align:right;color:{color};'>"
              f"{fmt_eur(total)}</td></tr>")
    s += f'''
            <tr style="border-top:2px solid #666;">
                <td colspan="2"><strong>Summe Betriebseinnahmen</strong></td>
                <td style="text-align:right;font-weight:bold;color:green;">
                    {fmt_eur(income_sum)}</td></tr>
        </table>

        <h4 style="margin-bottom:6px;margin-top:18px;">Ausgaben</h4>
        <table style="width:100%;border-collapse:collapse;">
            <tr><th style="text-align:left;">SKR-Konto</th>
                <th style="text-align:left;">Bezeichnung</th>
                <th style="text-align:right;">Saldo</th></tr>
    '''
    for nr, name, total in expense_rows:
        color = 'green' if total >= 0 else 'red'
        s += (f"<tr><td>{_esc(nr)}</td><td>{_esc(name)}</td>"
              f"<td style='text-align:right;color:{color};'>"
              f"{fmt_eur(total)}</td></tr>")
    euer_color = 'green' if euer_result >= 0 else 'red'
    s += f'''
            <tr style="border-top:2px solid #666;">
                <td colspan="2"><strong>Summe Betriebsausgaben</strong></td>
                <td style="text-align:right;font-weight:bold;color:red;">
                    {fmt_eur(expense_sum)}</td></tr>
        </table>

        <table style="width:100%;border-collapse:collapse;">
            <tr><td><strong>Ergebnis EÜR</strong></td>
                <td style="text-align:right;font-weight:bold;font-size:18px;
                    color:{euer_color};">
                    {fmt_eur(euer_result)}</td></tr>
        </table>

        <h4 style="margin-bottom:6px;margin-top:18px;">Sonstige Ausgaben</h4>
        <table style="width:100%;border-collapse:collapse;">
            <tr><th style="text-align:left;">SKR-Konto</th>
                <th style="text-align:left;">Bezeichnung</th>
                <th style="text-align:right;">Saldo</th></tr>
    '''
    for nr, name, total in other_expense_rows:
        color = 'green' if total >= 0 else 'red'
        s += (f"<tr><td>{_esc(nr)}</td><td>{_esc(name)}</td>"
              f"<td style='text-align:right;color:{color};'>"
              f"{fmt_eur(total)}</td></tr>")
    s += f'''
            <tr style="border-top:2px solid #666;">
                <td colspan="2"><strong>Summe Sonstige Ausgaben</strong></td>
                <td style="text-align:right;font-weight:bold;color:red;">
                    {fmt_eur(other_expense_sum)}</td></tr>
        </table>
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
