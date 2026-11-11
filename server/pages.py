# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
HTML page generation functions
All functions return complete HTML strings
"""
import html as _html
import json
from db import Database
from db.invoices import InvoiceCol
from money import fmt_eur


def script_json(value) -> str:
    """JSON zum Einbetten in einen ``<script>``-Block.

    ``json.dumps`` lässt ``<``, ``>`` und ``&`` stehen: Ein ``</script>`` im Wert beendet
    den Block, und alles danach wäre HTML (XSS). Diese Zeichen und die Zeilentrenner
    U+2028/U+2029 (in älterem JavaScript ein Zeilenende innerhalb einer Zeichenkette)
    werden deshalb als ``\\uXXXX`` geschrieben; ``json.loads`` und JavaScript lesen daraus
    wieder den ursprünglichen Wert.

    Nur für ``<script>``-Blöcke. In einem HTML-Attribut (``onclick="…"``) beendete schon
    das ``"`` der JSON-Zeichenketten das Attribut: Dort das Ergebnis zusätzlich durch
    ``html.escape(…, quote=True)`` schicken.
    """
    return (json.dumps(value, ensure_ascii=False)
            .replace('<', '\\u003c')
            .replace('>', '\\u003e')
            .replace('&', '\\u0026')
            .replace('\N{LINE SEPARATOR}', '\\u2028')
            .replace('\N{PARAGRAPH SEPARATOR}', '\\u2029'))


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


def logo_on_server(logo):
    """Liegt das gespeicherte Logo als Datei dort, wo die URL aus logo_url()
    hinzeigt – kann der Browser es also von diesem Server laden?

    Gleiche Regeln wie der Static-Handler in ``server/app.py``: ``/data/logos/``
    löst den Dateinamen im logos-Verzeichnis des Nutzers auf, ``/seed_data/private/``
    liegt global; alles andere (``/static/…``, ein Pfad vom eigenen PC) liefert
    der Server nicht aus. Returns True/False, ``None`` bei http(s)-URLs – die
    kommen von woanders und lassen sich hier nicht prüfen.
    """
    import os
    from urllib.parse import unquote
    url = logo_url(logo)
    if not url:
        return False
    if url.startswith(('http://', 'https://')):
        return None
    path = unquote(url.split('?', 1)[0])
    if path.startswith('/data/logos/'):
        import userctx
        name = os.path.basename(path)
        return bool(name) and os.path.isfile(
            os.path.join(userctx.user_subdir('logos', create=False), name))
    if path.startswith('/seed_data/private/'):
        # Wie der Static-Handler: nur Dateien, die wirklich unter seed_data/private
        # liegen – sonst verriete die Antwort, ob es beliebige Dateien gibt.
        base = os.path.realpath('seed_data/private')
        target = os.path.realpath(path[1:])
        return target.startswith(base + os.sep) and os.path.isfile(target)
    return False


def Header1(active_page=None):
    """Generate main header with navigation
    
    Args:
        active_page: Name of active page ('dashboard', 'receipts', 'transactions', 'skr', 'miscellaneous', 'about')
                     Active page will be highlighted without link
    """
    from version import APP_VERSION
    import userctx as _userctx
    s = "<!DOCTYPE html>\n"
    s+= "<html>\n <head>\n  <meta charset='UTF-8'>\n"
    s+= "  <title>uBuchhaltung</title>\n"
    # CSRF-Token für Formulare/fetch (leer in der Ersteinrichtung: noch keine Sitzung)
    s+= f"  <meta name='csrf-token' content='{_userctx.get_csrf_token()}'>\n"
    # ?v=… als Cache-Buster: erlaubt langes Browser-Caching (kein Flackern durch
    # Revalidierung beim Seitenwechsel), Updates kommen per Versions-Bump an
    s+= f"  <link rel='stylesheet' href='/buch.css?v={APP_VERSION}'>\n"
    s+= "  <link rel='icon' sizes='32x32' href='favicon.ico'>\n"
    s+= " </head>\n <body>"
    import demo as _demo
    if _demo.is_active():
        s+= f"<div class='demoBanner'>{_html.escape(_demo.BANNER_TEXT)}</div>"
    
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

    # Angemeldeter Benutzer + Abmelden; bei automatischer Anmeldung gibt es keine
    # Sitzung, die man beenden könnte. Abmelden ist eine Aktion: POST, kein Link.
    import userctx
    _user = userctx.get_user()
    if userctx.auth_enabled() and _user:
        if userctx.auto_login_active():
            s += f'&nbsp;&nbsp;&nbsp; 👤 {_html.escape(_user)}'
        else:
            s += (f'&nbsp;&nbsp;&nbsp; 👤 {_html.escape(_user)} · '
                  '<a href="javascript:void(0);" onclick="appPost(\'/logout\')">Abmelden</a>')
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
/* Aktion mit Seiteneffekt (Löschen …) als POST abschicken – nie als Link/GET. */
function appPost(url, fields) {
  var f = document.createElement('form');
  f.method = 'post'; f.action = url; f.hidden = true;
  Object.keys(fields || {}).forEach(function (name) {
    var x = document.createElement('input');
    x.type = 'hidden'; x.name = name; x.value = fields[name];
    f.appendChild(x);
  });
  var meta = document.querySelector('meta[name="csrf-token"]');
  if (meta && meta.content) {
    var i = document.createElement('input');
    i.type = 'hidden'; i.name = 'csrf'; i.value = meta.content;
    f.appendChild(i);
  }
  document.body.appendChild(f);
  f.submit();
}
function appConfirmPost(url, text) {
  appConfirm(text, function() { appPost(url); });
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

/* Echter Datei-Download (todo #1): Datei holen und über einen temporaeren
   <a download> speichern. Ein blosser Link auf den Server waere ohne laufenden
   Server wertlos - so liegt die Datei anschliessend wirklich beim Nutzer.
   Der Dateiname kommt prozentkodiert im Header X-Filename.
   notify(text, typ) meldet Fehler (Standard: appMsg); erfuellt mit dem
   Dateinamen bzw. null. */
function appDownload(url, notify) {
  notify = notify || appMsg;
  return fetch(url).then(function(r) {
    if (!r.ok) {
      return r.text().then(function(t) {
        throw new Error((t || '').trim() || 'Datei konnte nicht geladen werden.');
      });
    }
    var header = r.headers.get('X-Filename');
    var name = header ? decodeURIComponent(header) : 'download';
    return r.blob().then(function(blob) {
      var objUrl = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = objUrl;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function() { URL.revokeObjectURL(objUrl); }, 60000);
      return name;
    });
  }).catch(function(err) {
    notify(err.message, 'error');
    return null;
  });
}

/* CSRF: Token aus dem Meta-Tag automatisch an alle POSTs anhängen.
   - Formulare: hidden-Feld 'csrf' beim Submit ergänzen (Capture-Phase,
     erfasst auch dynamisch nachgeladene Formulare)
   - fetch: X-CSRF-Token-Header für alle nicht-GET-Requests
   Ist der Token leer (Ersteinrichtung), passiert nichts. */
(function() {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta ? meta.content : '';
  if (!token) return;
  document.addEventListener('submit', function(e) {
    var f = e.target;
    if (!f || (f.method || '').toLowerCase() !== 'post') return;
    var i = f.querySelector('input[name="csrf"]');
    if (!i) {
      i = document.createElement('input');
      i.type = 'hidden'; i.name = 'csrf';
      f.appendChild(i);
    }
    i.value = token;
  }, true);
  var _fetch = window.fetch;
  window.fetch = function(input, init) {
    init = init || {};
    var method = (init.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      var h = new Headers(init.headers || {});
      if (!h.has('X-CSRF-Token')) h.set('X-CSRF-Token', token);
      init.headers = h;
    }
    return _fetch(input, init);
  };
})();
</script>
</body></html>'''
    return s

def PageAbout():
    """Generate about page"""
    from version import APP_VERSION
    s = Header1('about')
    s+= Header2()
    s+= Header3()
    s+= "<h1>uBuchhaltung</h1>"
    s+= f'Version {APP_VERSION} <br>Deine einfache Buchführungssoftware, entwickelt von <a href="https://unsix.de">unsix</a>.<br><br>'
    s+= f'''Download auf GitHub: <a href="https://github.com/NobseVomBerg/uBuchhaltung">uBuchhaltung auf GitHub</a><br><br>
uBuchhaltung ist ein Open-Source-Projekt unter der AGPL-Lizenz, das mit Leidenschaft entwickelt wird, um Selbstständigen und kleinen
Unternehmen eine benutzerfreundliche Buchführungssoftware zu bieten, die die gängigsten Anforderungen direkt abdeckt und darüber
hinaus einfach erweiterbar ist. So kannst Du SKR-Konten nach Bedarf hinzufügen oder AFA-Abschreibungen anpassen.<br><br>
Wenn du Fragen hast, lies die MD-Files oder lass Dir das Projekt von einer KI Deiner Wahl kurz erklären.<br><br>
Lizenz: <a href="https://www.gnu.org/licenses/agpl-3.0">AGPL-3.0-or-later</a> &middot;
Kommerzielle Lizenz ohne AGPL-Pflichten: <span id="licenseContact"><noscript>E-Mail-Anzeige ben&ouml;tigt JavaScript</noscript></span>'''
    # Spam-Schutz wie auf unsix.de: Adresse Base64-kodiert im Quelltext,
    # erst JavaScript schreibt den mailto-Link in den DOM (Kodierung:
    # btoa(unescape(encodeURIComponent(adresse)))).
    s+= '''
<script>
(function () {
  var m = decodeURIComponent(escape(atob("b2ZmaWNlQHVuc2l4LmNvbQ==")));
  var a = document.createElement("a");
  a.href = "mailto:" + m;
  a.textContent = m;
  var el = document.getElementById("licenseContact");
  el.textContent = "";
  el.appendChild(a);
}());
</script>'''
    s+= Footer()
    return s

def PageReminders(db: Database):
    """Generate reminders/dunning page for overdue invoices"""
    from datetime import date
    from decimal import Decimal

    today = date.today()

    def open_amount(inv):
        """Offener Betrag; Altbestand ohne AmountDue (NULL) gilt als voll offen."""
        due = inv[InvoiceCol.AMOUNT_DUE]
        return due if due is not None else (inv[InvoiceCol.SUM_GROSS] or Decimal(0))

    def due_date(inv):
        """Fälligkeitsdatum als date – None bei leerem oder ungültigem Eintrag."""
        try:
            return date.fromisoformat(str(inv[InvoiceCol.DUE_DATE]))
        except ValueError:
            return None

    overdue = db.get_overdue_invoices()
    due_soon = db.get_invoices_due_soon(days=7)
    
    s = Header1('invoice')
    from .pages_invoice import document_submenu
    s += Header2(document_submenu('reminders'))
    s += Header3()
    
    s += "<h2>Mahnwesen & Fälligkeiten</h2>"
    
    # Statistics
    total_overdue_amount = sum((open_amount(inv) for inv in overdue), Decimal(0))
    overdue_count = len(overdue)
    
    s += f'''
    <div style="margin-bottom: 20px; padding: 15px; background-color: #fff3cd; border-left: 4px solid #ffc107; border-radius: 5px;">
        <h3 style="margin-top: 0; color: #856404;">⚠️ Überfällige Rechnungen</h3>
        <p style="margin: 0;"><strong>{overdue_count} Rechnung(en)</strong> überfällig | 
        Offener Betrag: <strong style="color: #dc3545;">{fmt_eur(total_overdue_amount)}</strong></p>
    </div>
    '''
    
    if overdue:
        s += "<h3>Überfällige Rechnungen (Mahnung erforderlich)</h3>"
        s += "<table style='width: 100%;'>"
        s += "<tr><th>RE-Nr.</th><th>Datum</th><th>Fällig seit</th><th>Kunde</th><th>Betrag</th><th>Überfällig (Tage)</th><th>Aktionen</th></tr>"
        
        for inv in overdue:
            inv_id = inv[InvoiceCol.ID]
            inv_number = _html.escape(str(inv[InvoiceCol.NUMBER] or ''))
            inv_date = _html.escape(str(inv[InvoiceCol.DATE] or ''))
            due_text = _html.escape(str(inv[InvoiceCol.DUE_DATE] or ''))
            buyer_name = _html.escape(str(inv[InvoiceCol.BUYER_COMPANY]
                                          or inv[InvoiceCol.BUYER_NAME] or '')[:30])
            amount = open_amount(inv)
            due = due_date(inv)
            
            # Calculate days overdue; ohne gültiges Fälligkeitsdatum gibt es keine Verzugstage
            if due is None:
                row_style = ""
                days_text = "–"
            else:
                days_overdue = (today - due).days
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
                days_text = f"{days_overdue} Tage<br><small>{severity}</small>"
            
            s += f"<tr style='{row_style}'>"
            s += f"<td>{inv_number}</td>"
            s += f"<td>{inv_date}</td>"
            s += f"<td>{due_text}</td>"
            s += f"<td>{buyer_name}</td>"
            s += f"<td style='text-align: right;'><strong>{fmt_eur(amount)}</strong></td>"
            s += f"<td>{days_text}</td>"
            s += f"<td><a href='/invoice/view?id={inv_id}'>Ansicht</a></td>"
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
            inv_id = inv[InvoiceCol.ID]
            inv_number = _html.escape(str(inv[InvoiceCol.NUMBER] or ''))
            inv_date = _html.escape(str(inv[InvoiceCol.DATE] or ''))
            due_text = _html.escape(str(inv[InvoiceCol.DUE_DATE] or ''))
            buyer_name = _html.escape(str(inv[InvoiceCol.BUYER_COMPANY]
                                          or inv[InvoiceCol.BUYER_NAME] or '')[:30])
            amount = open_amount(inv)
            due = due_date(inv)
            days_text = f"{(due - today).days} Tage" if due is not None else "–"
            
            s += f"<tr>"
            s += f"<td>{inv_number}</td>"
            s += f"<td>{inv_date}</td>"
            s += f"<td>{due_text}</td>"
            s += f"<td>{buyer_name}</td>"
            s += f"<td style='text-align: right;'>{fmt_eur(amount)}</td>"
            s += f"<td>{days_text}</td>"
            s += f"<td><a href='/invoice/view?id={inv_id}'>Ansicht</a></td>"
            s += f"</tr>"
        
        s += "</table>"
    else:
        s += "<p><em>Keine Rechnungen in den nächsten 7 Tagen fällig.</em></p>"
    
    s += Footer()
    return s
