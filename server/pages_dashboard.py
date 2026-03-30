"""
Dashboard page
"""
from datetime import datetime
from db import Database
from .pages import Header1, Header2, Header3, Footer, INVOICE_STATUS_COLORS, INVOICE_STATUS_LABELS


def PageDashboard(db: Database):
    """Generate dashboard with statistics and charts"""

    # Get all invoices
    all_invoices = db.fetch_invoices()

    # Current year statistics
    current_year = datetime.now().year
    current_year_invoices = [inv for inv in all_invoices if inv[2] and inv[2].startswith(str(current_year))]

    # Status counts – keyed to match INVOICE_STATUS_COLORS order
    counts = {
        'draft':     len([inv for inv in all_invoices if inv[37] == 'draft']),
        'finalized': len([inv for inv in all_invoices if inv[37] == 'finalized']),
        'sent':      len([inv for inv in all_invoices if inv[37] == 'sent']),
        'paid':      len([inv for inv in all_invoices if inv[37] == 'paid']),
        'cancelled': len([inv for inv in all_invoices if inv[37] == 'cancelled']),
    }
    draft_count     = counts['draft']
    finalized_count = counts['finalized']
    sent_count      = counts['sent']
    paid_count      = counts['paid']
    cancelled_count = counts['cancelled']

    # Financial statistics
    total_revenue = sum(inv[35] for inv in all_invoices if inv[35] and inv[37] in ['finalized', 'sent', 'paid'])
    paid_revenue  = sum(inv[35] for inv in all_invoices if inv[35] and inv[37] == 'paid')
    open_amount   = sum(inv[36] or inv[35] for inv in all_invoices if inv[35] and inv[37] in ['finalized', 'sent'])

    # Current year revenue
    year_revenue = sum(inv[35] for inv in current_year_invoices if inv[35] and inv[37] in ['finalized', 'sent', 'paid'])
    year_paid    = sum(inv[35] for inv in current_year_invoices if inv[35] and inv[37] == 'paid')

    # Overdue
    overdue        = db.get_overdue_invoices()
    overdue_amount = sum(inv[36] or inv[35] for inv in overdue if inv[35])

    # Monthly revenue for current year
    monthly_revenue = {}
    for month in range(1, 13):
        month_str = f"{current_year}-{month:02d}"
        month_invoices = [inv for inv in current_year_invoices
                         if inv[2] and inv[2].startswith(month_str)
                         and inv[37] in ['finalized', 'sent', 'paid']]
        monthly_revenue[month] = sum(inv[35] for inv in month_invoices if inv[35])

    s = Header1('dashboard')
    s += Header2()
    s += Header3()

    # Key metrics cards
    s += f'''
    <div class="grid-1RowPrefered">
        <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; opacity: 0.9;">Gesamtumsatz ({current_year})</div>
            <div style="font-size: 32px; font-weight: bold; margin: 10px 0;">{year_revenue:.2f} €</div>
            <div style="font-size: 12px; opacity: 0.8;">Davon bezahlt: {year_paid:.2f} €</div>
        </div>

        <div style="background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; opacity: 0.9;">Offene Forderungen</div>
            <div style="font-size: 32px; font-weight: bold; margin: 10px 0;">{open_amount:.2f} €</div>
            <div style="font-size: 12px; opacity: 0.8;">{sent_count + finalized_count} unbezahlte Rechnung(en)</div>
        </div>

        <div style="background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); color: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; opacity: 0.9;">Überfällige Rechnungen</div>
            <div style="font-size: 32px; font-weight: bold; margin: 10px 0;">{overdue_amount:.2f} €</div>
            <div style="font-size: 12px; opacity: 0.8;">{len(overdue)} Rechnung(en) überfällig</div>
        </div>

        <div style="background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); color: white; padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="font-size: 14px; opacity: 0.9;">Gesamt Rechnungen</div>
            <div style="font-size: 32px; font-weight: bold; margin: 10px 0;">{len(all_invoices)}</div>
            <div style="font-size: 12px; opacity: 0.8;">{len(current_year_invoices)} in {current_year}</div>
        </div>
    </div>
    '''

    # Status distribution & Recent invoices
    s += '<div class="grid-1RowPrefered">'

    # Left: Status bar chart – built from shared constants, no duplicate color definitions
    s += '''
    <div class="rectRounded">
        <h3 style="margin-top: 0;">Status-Verteilung</h3>
        <div style="margin: 20px 0;">
    '''

    status_data = [
        (INVOICE_STATUS_LABELS[k], counts[k], INVOICE_STATUS_COLORS[k])
        for k in INVOICE_STATUS_COLORS
    ]
    total_status = sum(c for _, c, _ in status_data)
    for label, count, color in status_data:
        percentage = (count / total_status * 100) if total_status > 0 else 0
        s += f'''
        <div style="margin-bottom: 15px;">
            <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                <span>{label}</span>
                <span><strong>{count}</strong> ({percentage:.1f}%)</span>
            </div>
            <div style="background: #f0f0f0; height: 20px; border-radius: 10px; overflow: hidden;">
                <div style="background: {color}; height: 100%; width: {percentage}%;"></div>
            </div>
        </div>
        '''

    s += '</div></div>'

    # Right: Recent invoices  (color dot from shared constant too)
    recent_invoices = sorted(all_invoices, key=lambda x: x[39] or '', reverse=True)[:5]  # CreatedAt
    s += '''
    <div class="rectRounded">
        <h3 style="margin-top: 0;">Letzte Rechnungen</h3>
        <table style="width: 100%; border-collapse: collapse;">
    '''

    for inv in recent_invoices:
        status_color = INVOICE_STATUS_COLORS.get(inv[40], '#888')  # Status at index 40
        s += f'''
        <tr style="border-bottom: 1px solid #eee;">
            <td style="padding: 10px 0;">{inv[1]}</td>
            <td>{inv[14][:20] if inv[14] else ''}</td>
            <td style="text-align: right;">{inv[38]:.2f} €</td>
            <td><span style="color: {status_color};">●</span></td>
            <td><a href="/invoice/view?id={inv[0]}">Ansicht</a></td>
        </tr>
        '''

    s += '</table></div></div>'

    # Monthly revenue chart
    s += f'''
    <div class="grid-1RowPrefered">
    <div class="rectRounded">
        <h3 style="margin-top: 0;">Monatlicher Umsatz {current_year}</h3>
        <div style="height: 250px; position: relative; margin-top: 20px;">
    '''

    max_revenue  = max(monthly_revenue.values()) if any(monthly_revenue.values()) else 1
    month_names  = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']

    s += '<div style="display: flex; align-items: flex-end; justify-content: space-around; height: 200px; border-left: 2px solid #ddd; border-bottom: 2px solid #ddd; padding: 10px;">'

    for month in range(1, 13):
        revenue = monthly_revenue[month]
        height  = (revenue / max_revenue * 180) if max_revenue > 0 else 0
        s += f'''
        <div style="display: flex; flex-direction: column; align-items: center; flex: 1;">
            <div style="font-size: 11px; margin-bottom: 5px;">{revenue:.0f}€</div>
            <div style="background: linear-gradient(180deg, #4facfe 0%, #00f2fe 100%); width: 80%; height: {height}px; border-radius: 5px 5px 0 0;"></div>
            <div style="font-size: 12px; margin-top: 5px;">{month_names[month-1]}</div>
        </div>
        '''

    s += '</div></div></div></div>'

    # Quick actions
    s += '''
    <div class="grid-1RowPrefered">
    <div class="rectRounded">
        <h3 style="margin-top: 0;">Schnellzugriff</h3>
        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
            <a href="/invoice/new" class="coloredButton btn-green">+ Neue Rechnung</a>
            <a href="/invoice?status=sent" class="coloredButton btn-orange">Versendete Rechnungen</a>
            <a href="/invoice/reminders" class="coloredButton btn-red">⚠️ Mahnwesen</a>
            <a href="/invoice" class="coloredButton btn-blue">Alle Rechnungen</a>
        </div>
    </div>
    </div>
    '''

    s += Footer()
    return s
