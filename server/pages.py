"""
HTML page generation functions
All functions return complete HTML strings
"""
import html as _html
from db import Database

def Header1(active_page=None):
    """Generate main header with navigation
    
    Args:
        active_page: Name of active page ('dashboard', 'receipts', 'transactions', 'skr', 'miscellaneous', 'about')
                     Active page will be highlighted without link
    """
    s = "<!DOCTYPE html>\n"
    s+= "<html>\n <head>\n  <meta charset='UTF-8'>\n"
    s+= "  <title>Contabilidad simple</title>\n"
    s+= "  <link rel='stylesheet' href='/buch.css'>\n"
    s+= "  <link rel='icon' sizes='32x32' href='favicon.ico'>\n"
    s+= " </head>\n <body>"
    
    # Build navigation menu with active page highlighting
    nav_items = []
    
    # Dashboard
    if active_page == 'dashboard':
        nav_items.append('<span id="ActivePage">Dashboard</span>')
    else:
        nav_items.append('<a href="/">Dashboard</a>')
    
    # Rechnung
    if active_page == 'invoice':
        nav_items.append('<span id="ActivePage">Rechnung</span>')
    else:
        nav_items.append('<a href="/invoice">Rechnung</a>')
    
    # Stammdaten (Master Data)
    if active_page == 'masterdata':
        nav_items.append('<span id="ActivePage">Stammdaten</span>')
    else:
        nav_items.append('<a href="/masterdata">Stammdaten</a>')
    
    # Belege
    if active_page == 'receipts':
        nav_items.append('<span id="ActivePage">Belege</span>')
    else:
        nav_items.append('<a href="/receipts">Belege</a>')
    
    # Buchungen
    if active_page == 'transactions':
        nav_items.append('<span id="ActivePage">Buchungen</span>')
    else:
        nav_items.append('<a href="/transactions">Buchungen</a>')
    
    # Split-Buchungen
    if active_page == 'bookinggroups':
        nav_items.append('<span id="ActivePage">Split-Buchungen</span>')
    else:
        nav_items.append('<a href="/bookinggroups">Split-Buchungen</a>')
    
    # Anlagen
    if active_page == 'assets':
        nav_items.append('<span id="ActivePage">Anlagen</span>')
    else:
        nav_items.append('<a href="/assets">Anlagen</a>')
    
    # Sonstiges
    if active_page == 'miscellaneous':
        nav_items.append('<span id="ActivePage">Sonstiges</span>')
    else:
        nav_items.append('<a href="/miscellaneous">Sonstiges</a>')
    
    # About
    if active_page == 'about':
        nav_items.append('<span id="ActivePage">About</span>')
    else:
        nav_items.append('<a href="/about">About</a>')
    
    s += ' | '.join(nav_items)
    return s

def Header2(content=""):
    """Generate secondary header/submenu"""
    s = "<div class='header2'>"
    if content:
        s += content
    else:
        s += "&nbsp;"
    s += "</div>"
    return s

def Header3(content=""):
    """Generate third header line for filters"""
    s = "<div class='header3'>"
    if content:
        s += content
    else:
        s += "&nbsp;"
    s += "</div>"
    return s

def Footer():
    """Generate page footer"""
    s = "</body></html>"
    return s

def PageAbout():
    """Generate about page"""
    s = Header1('about')
    s+= Header2()
    s+= Header3()
    s+= "<p>Einfache Buchführungssoftware.</p>"
    s+= Footer()
    return s

def PageReminders(db: Database):
    """Generate reminders/dunning page for overdue invoices"""
    from datetime import date, timedelta
    
    overdue = db.get_overdue_invoices()
    due_soon = db.get_invoices_due_soon(days=7)
    
    s = Header1('invoice')
    submenu = '<a href="/invoice">Rechnungen</a> | <span id="ActivePage">Mahnwesen</span>'
    s += Header2(submenu)
    s += Header3()
    
    s += "<h2>Mahnwesen & Fälligkeiten</h2>"
    
    # Statistics
    total_overdue_amount = sum(inv[36] or inv[35] for inv in overdue if inv[35])
    overdue_count = len(overdue)
    
    s += f'''
    <div style="margin-bottom: 20px; padding: 15px; background-color: #fff3cd; border-left: 4px solid #ffc107; border-radius: 5px;">
        <h3 style="margin-top: 0; color: #856404;">⚠️ Überfällige Rechnungen</h3>
        <p style="margin: 0;"><strong>{overdue_count} Rechnung(en)</strong> überfällig | 
        Offener Betrag: <strong style="color: #dc3545;">{total_overdue_amount:.2f} €</strong></p>
    </div>
    '''
    
    if overdue:
        s += "<h3>Überfällige Rechnungen (Mahnung erforderlich)</h3>"
        s += "<table style='width: 100%;'>"
        s += "<tr><th>RE-Nr.</th><th>Datum</th><th>Fällig seit</th><th>Kunde</th><th>Betrag</th><th>Überfällig (Tage)</th><th>Aktionen</th></tr>"
        
        today = date.today()
        for inv in overdue:
            inv_id = inv[0]
            inv_number = inv[1]
            inv_date = inv[2]
            due_date = inv[20]  # PaymentDueDate
            buyer_name = inv[12]
            amount = inv[36] or inv[35]
            
            # Calculate days overdue
            if due_date:
                due = date.fromisoformat(due_date)
                days_overdue = (today - due).days
            else:
                days_overdue = 0
            
            # Color code by severity
            if days_overdue > 60:
                row_style = "background-color: #f8d7da;"  # Red - 3rd reminder
                severity = "🔴 Mahnstufe 3"
            elif days_overdue > 30:
                row_style = "background-color: #fff3cd;"  # Yellow - 2nd reminder
                severity = "🟡 Mahnstufe 2"
            else:
                row_style = "background-color: #d1ecf1;"  # Blue - 1st reminder
                severity = "🔵 Mahnstufe 1"
            
            s += f"<tr style='{row_style}'>"
            s += f"<td>{inv_number}</td>"
            s += f"<td>{inv_date}</td>"
            s += f"<td>{due_date}</td>"
            s += f"<td>{buyer_name[:30]}</td>"
            s += f"<td style='text-align: right;'><strong>{amount:.2f} €</strong></td>"
            s += f"<td>{days_overdue} Tage<br><small>{severity}</small></td>"
            s += f"<td>"
            s += f"<a href='/invoice/view?id={inv_id}'>Ansicht</a> | "
            s += f"<a href='/invoice/reminder?id={inv_id}' style='color: #dc3545;'>Mahnung erstellen</a>"
            s += f"</td>"
            s += f"</tr>"
        
        s += "</table>"
    else:
        s += "<p style='color: #28a745;'>✓ Keine überfälligen Rechnungen vorhanden.</p>"
    
    # Invoices due soon
    s += "<h3 style='margin-top: 30px;'>Rechnungen fällig in den nächsten 7 Tagen</h3>"
    
    if due_soon:
        s += "<table style='width: 100%;'>"
        s += "<tr><th>RE-Nr.</th><th>Datum</th><th>Fälligkeitsdatum</th><th>Kunde</th><th>Betrag</th><th>Verbleibende Tage</th><th>Aktionen</th></tr>"
        
        for inv in due_soon:
            inv_id = inv[0]
            inv_number = inv[1]
            inv_date = inv[2]
            due_date = inv[20]
            buyer_name = inv[12]
            amount = inv[36] or inv[35]
            
            if due_date:
                due = date.fromisoformat(due_date)
                days_remaining = (due - today).days
            else:
                days_remaining = 0
            
            s += f"<tr>"
            s += f"<td>{inv_number}</td>"
            s += f"<td>{inv_date}</td>"
            s += f"<td>{due_date}</td>"
            s += f"<td>{buyer_name[:30]}</td>"
            s += f"<td style='text-align: right;'>{amount:.2f} €</td>"
            s += f"<td>{days_remaining} Tage</td>"
            s += f"<td><a href='/invoice/view?id={inv_id}'>Ansicht</a></td>"
            s += f"</tr>"
        
        s += "</table>"
    else:
        s += "<p><em>Keine Rechnungen in den nächsten 7 Tagen fällig.</em></p>"
    
    s += Footer()
    return s
