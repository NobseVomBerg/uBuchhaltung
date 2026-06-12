"""
HTML page generation functions
All functions return complete HTML strings
"""
import html as _html
from db import Database


def logo_url(logo):
    """Gespeicherten Logo-Pfad in eine im Browser ladbare URL umwandeln.

    - http(s)-URLs bleiben unverändert.
    - relative Pfade (z.B. 'data/logos/x.png', 'seed_data/private/logo.png')
      werden absolut ('/data/logos/x.png') – der Server liefert diese Verzeichnisse aus.
    Backslashes (Windows) werden zu Slashes normalisiert.
    """
    if not logo:
        return ''
    l = str(logo).strip().replace('\\', '/')
    if l.startswith(('http://', 'https://', '/')):
        return l
    return '/' + l


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

    # Zeiten (Arbeitszeiten, später auch Fahrten)
    if active_page == 'worktime':
        nav_items.append('<span id="ActivePage">Zeiten</span>')
    else:
        nav_items.append('<a href="/worktime">Zeiten</a>')

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
    """Generate page footer with shared confirm-modal and notification bar."""
    s = '''
<div id="app_confirm" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.45);z-index:9999;align-items:center;justify-content:center;">
  <div class="modalBox">
    <p id="app_confirm_text" style="margin:0 0 22px;font-size:15px;line-height:1.4;"></p>
    <button id="app_confirm_ok" class="coloredButton bg-green">OK</button>
    <button class="coloredButton bg-gray" style="margin-left:10px;"
            onclick="document.getElementById('app_confirm').style.display='none'">Abbrechen</button>
  </div>
</div>
<div id="app_msg" style="display:none;position:fixed;bottom:20px;right:20px;max-width:420px;padding:12px 18px;border-radius:5px;font-size:14px;box-shadow:0 2px 10px rgba(0,0,0,.2);z-index:10000;"></div>
<script>
function appConfirm(text, onOk) {
  document.getElementById('app_confirm_text').textContent = text;
  document.getElementById('app_confirm_ok').onclick = function() {
    document.getElementById('app_confirm').style.display = 'none';
    onOk();
  };
  document.getElementById('app_confirm').style.display = 'flex';
}
function appConfirmHref(url, text) {
  appConfirm(text, function() { window.location.href = url; });
}
function appMsg(text, type) {
  type = type || 'info';
  var styles = {
    success: 'background:#d4edda;color:#155724;border:1px solid #c3e6cb',
    error:   'background:#f8d7da;color:#721c24;border:1px solid #f5c6cb',
    warn:    'background:#fff3cd;color:#856404;border:1px solid #ffc107',
    info:    'background:#d1ecf1;color:#0c5460;border:1px solid #bee5eb'
  };
  var bar = document.getElementById('app_msg');
  bar.setAttribute('style', (styles[type] || styles.info)
    + ';display:block;position:fixed;bottom:20px;right:20px;max-width:420px;'
    + 'padding:12px 18px;border-radius:5px;font-size:14px;'
    + 'box-shadow:0 2px 10px rgba(0,0,0,.2);z-index:10000;');
  bar.textContent = text;
  if (type !== 'error') setTimeout(function() { bar.style.display = 'none'; }, 5000);
}
</script>
</body></html>'''
    return s

def PageAbout():
    """Generate about page"""
    from version import APP_VERSION
    s = Header1('about')
    s+= Header2()
    s+= Header3()
    s+= "<h1>PyBuch</h1>"
    s+= f'Version {APP_VERSION} <br>Deine einfache Buchführungssoftware, entwickelt von <a href="https://unsix.de">unsix</a>.<br><br>'
    s+= f'''Dowload auf GitHub: <a href="https://github.com/NobseVomBerg/PyBuch">PyBuch auf GitHub</a><br><br>
PyBuch ist ein Open-Source-Projekt unter der APGL-Lizenz, das mit Leidenschaft entwickelt wird, um Selbstständigen und kleinen
Unternehmen eine benutzerfreundliche Buchführungssoftware zu bieten, die die gängigsten Anforderungen direkt abdeckt und darüber
hinaus einfach erweiterbar ist. So kannst Du SKR-Konten nach Bedarf hinzufügen oder AFA-Abschreibungen anpassen.<br><br>
Wenn du Fragen hast, lies die MD-Files oder lass Dir das Projekt von einer KI Deiner Wahl kurz erklären.'''
    s+= Footer()
    return s

def PageReminders(db: Database):
    """Generate reminders/dunning page for overdue invoices"""
    from datetime import date, timedelta
    
    overdue = db.get_overdue_invoices()
    due_soon = db.get_invoices_due_soon(days=7)
    
    s = Header1('invoice')
    from .pages_invoice import document_submenu
    s += Header2(document_submenu('reminders'))
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
