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
    liegt global, und beide liefern nur Bilddateien der Positivliste
    (``export.pdf_core.LOGO_CONTENT_TYPES``); alles andere (``/static/…``, ein Pfad vom
    eigenen PC, eine SVG-Datei) liefert der Server nicht aus. Returns True/False,
    ``None`` bei http(s)-URLs – die kommen von woanders und lassen sich hier nicht prüfen.
    """
    import os
    from urllib.parse import unquote
    from export.pdf_core import logo_extension
    url = logo_url(logo)
    if not url:
        return False
    if url.startswith(('http://', 'https://')):
        return None
    path = unquote(url.split('?', 1)[0])
    if path.startswith('/data/logos/'):
        import userctx
        name = os.path.basename(path)
        return (bool(name) and logo_extension(name) is not None
                and os.path.isfile(os.path.join(userctx.user_subdir('logos', create=False), name)))
    if path.startswith('/seed_data/private/'):
        # Wie der Static-Handler: nur Dateien, die wirklich unter seed_data/private
        # liegen – sonst verriete die Antwort, ob es beliebige Dateien gibt.
        base = os.path.realpath('seed_data/private')
        try:
            target = os.path.realpath(path[1:])
        except ValueError:                                  # z. B. ein NUL im gespeicherten Wert: keine Datei
            return False
        return (target.startswith(base + os.sep) and logo_extension(path) is not None
                and logo_extension(target) is not None and os.path.isfile(target))
    return False


def Header1(active_page=None):
    """Generate main header with navigation
    
    Args:
        active_page: Name of active page ('dashboard', 'bwa', 'receipts', 'transactions', 'skr', 'miscellaneous', 'about')
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

    # Betriebswirtschaftliche Auswertung
    if active_page == 'bwa':
        nav_items.append('<span id="ActivePage">Auswertungen</span>')
    else:
        nav_items.append('<a href="/bwa">Auswertungen</a>')
    
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

    # Daten (Sicherung, Wiederherstellung, DATEV-Export, WISO-Import; die Adresse bleibt /miscellaneous)
    if active_page == 'miscellaneous':
        nav_items.append('<span id="ActivePage">Daten</span>')
    else:
        nav_items.append('<a href="/miscellaneous">Daten</a>')
    
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
    <p id="app_confirm_date_row" class="modalField" hidden><label><span id="app_confirm_date_label"></span>
      <input type="date" id="app_confirm_date"></label><br><span id="app_confirm_date_error" class="errorColor"></span></p>
    <button id="app_confirm_ok" class="coloredButton bg-green">OK</button>
    <button class="coloredButton bg-gray" style="margin-left:10px;"
            onclick="document.getElementById('app_confirm').style.display='none'">Abbrechen</button>
  </div>
</div>
<div id="app_msg" style="display:none;position:fixed;bottom:20px;right:20px;max-width:420px;padding:12px 18px;border-radius:5px;font-size:14px;box-shadow:0 2px 10px rgba(0,0,0,.2);z-index:10000;"></div>
<script>
/* Rückfrage; optional mit Datumsfeld (dateField: {label, value, min, max, error}; min und max sind optional): Der
   Dialog bleibt offen und nennt dateField.error, bis das Datum gültig ist (nicht leer, nicht vor min, nicht nach
   max); onOk bekommt es als Text JJJJ-MM-TT. */
function appConfirm(text, onOk, dateField) {
  document.getElementById('app_confirm_text').textContent = text;
  var row = document.getElementById('app_confirm_date_row');
  var date = document.getElementById('app_confirm_date');
  var problem = document.getElementById('app_confirm_date_error');
  row.hidden = !dateField;
  problem.textContent = '';
  if (dateField) {
    document.getElementById('app_confirm_date_label').textContent = dateField.label;
    date.min = dateField.min || '';
    date.max = dateField.max || '';
    date.value = dateField.value || '';
  }
  document.getElementById('app_confirm_ok').onclick = function() {
    if (dateField && (!date.value || (date.min && date.value < date.min) || (date.max && date.value > date.max))) {
      problem.textContent = dateField.error || 'Bitte ein gültiges Datum angeben.';
      return;
    }
    document.getElementById('app_confirm').style.display = 'none';
    onOk(dateField ? date.value : undefined);
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
function appConfirmPost(url, text, fields) {
  appConfirm(text, function() { appPost(url, fields); });
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

def _about_stats():
    """Der Abschnitt „Projekt in Zahlen“ der About-Seite aus ``project_stats.json`` (``server/project_stats.py``): streng gelesen, zur
    Laufzeit wird nichts gezählt. Fehlt die Datei oder ist sie ungültig, fehlt der Abschnitt – die Seite braucht ihn nicht. Jeder Wert
    aus der Datei steht maskiert im HTML."""
    from version import APP_VERSION
    from . import project_stats
    stats = project_stats.load_stats()
    if stats is None:
        return ''

    def count(number):
        return _html.escape(project_stats.format_count(number), quote=True)

    code, tests, data = stats['code'], stats['tests'], stats['data']
    rows = (
        ('Programmcode', f'{count(code["files"])} Dateien &middot; {count(code["lines"])} Zeilen &middot; '
                         f'davon {count(code["code_lines"])} ohne Leerzeilen und Kommentare'),
        ('Tests', f'{count(tests["files"])} Dateien &middot; {count(tests["lines"])} Zeilen &middot; '
                  f'davon {count(tests["code_lines"])} ohne Leerzeilen und Kommentare'),
        ('Tests (Anzahl)', f'{count(tests["functions"])} Testfunktionen &middot; {count(tests["cases"])} Testfälle'),
        ('Seed- und Beispieldaten', f'{count(data["files"])} Dateien &middot; {count(data["lines"])} Zeilen'),
    )
    version = _html.escape(stats['version'], quote=True)
    origin = f'Stand: Version {version}.' if stats['version'] == APP_VERSION else f'Die Zahlen stammen aus Version {version}.'
    s = '<br><br><div class="grid1Col700"><h2>Projekt in Zahlen</h2><table class="reportTable">'
    s += ''.join(f'<tr><td>{label}</td><td>{value}</td></tr>' for label, value in rows)
    s += f'</table><p class="hint">{origin} Gezählt mit tools/project_stats.py; Docstrings zählen als Kommentar.</p></div>'
    return s


#: Meldung der About-Seite, wenn der DB-Export gescheitert ist (``/about?export=error``): fest, der volle Fehler steht im Server-Log.
DB_EXPORT_FAILED_TEXT = 'Der DB-Export ist fehlgeschlagen – Einzelheiten stehen im Server-Log.'


def _digits(query, name):
    """Der Wert des Query-Parameters ``name`` als Zahl: genau ein Wert aus 1 bis 18 ASCII-Ziffern, sonst ``None``."""
    values = query.get(name, [])
    if len(values) == 1 and values[0].isascii() and values[0].isdigit() and len(values[0]) <= 18:
        return int(values[0])
    return None


def export_message(query):
    """Meldung zum DB-Export aus der Query der Weiterleitung (``Database.export_to_sql``, ``handlers.handle_db_export``): ``(Text, Art)``
    für ``appMsg`` bei ``?export=ok&tables=…&rows=…`` (beide Zahlen streng gelesen: der Text ist fest, die Zahlen kommen nur als Zahlen
    hinein) bzw. ``?export=error`` (``DB_EXPORT_FAILED_TEXT``); ``None``, wenn die Adresse keine nennt oder sie nicht streng passt."""
    status = query.get('export', [])
    if len(status) != 1:
        return None
    if status[0] == 'error':
        return DB_EXPORT_FAILED_TEXT, 'error'
    tables, rows = _digits(query, 'tables'), _digits(query, 'rows')
    if status[0] == 'ok' and tables is not None and rows is not None:
        return f'Export erfolgreich: {tables} Tabellen, {rows} Zeilen.', 'success'
    return None


def _about_database_overview(db):
    """Der Abschnitt „Datenbank-Übersicht“ der About-Seite (bis dahin auf der Seite Sonstiges): je Tabelle die Zahl der Einträge
    (``Database.get_table_statistics``), für jeden angemeldeten Benutzer. Jeder Wert steht maskiert da."""
    rows = ''.join(f'<tr><td>{_html.escape(str(name), quote=True)}</td><td class="num">{_html.escape(str(count), quote=True)}</td></tr>'
                   for name, count in db.get_table_statistics())
    return ('<br><br><div class="grid1Col700"><h2>Datenbank-Übersicht</h2><table class="reportTable">'
            f'<tr><th>Tabelle</th><th class="num">Anzahl Einträge</th></tr>{rows}</table></div>')


def _about_sql_console():
    """Der Kasten „SQL-Befehle ausführen“ samt der Vorlage „Zeitbereich löschen“ und seinem Skript – HTML und Skript wie bis dahin auf der
    Seite Sonstiges (gleiche IDs, gleiche Funktionen), nur ohne den Platz im Raster dort. Er geht an ``POST /execute_sql``
    (``handlers.handle_execute_sql``); die Seite zeigt ihn nur, wo ``auth.sql_console_allowed()`` gilt."""
    s = '<div class="rectRounded">'
    s += '''
        <h2>SQL-Befehle ausführen</h2>
        Gib hier SQL-Befehle ein (mehrere Befehle durch Semikolon getrennt). Eine Anfrage läuft höchstens 60 Sekunden, sehr große Ausgaben werden gekürzt:<br>
        <textarea id="sql_input" rows="15" cols="100" class="textareaSql" placeholder="SELECT * FROM ChartOfAccounts WHERE AccountNumber = 6805;"></textarea>
        <div class="rowWithObjects">
            <label><input type="checkbox" id="sql_write" autocomplete="off">&nbsp;Schreiben erlauben</label>
        </div>
        <p class="hint">Ohne Haken läuft die Konsole nur lesend. Mit Haken laufen alle Befehle in einer Transaktion: Scheitert einer, wird nichts geändert.</p>
        <div>
            <button type="button" onclick="executeSql()" class="coloredButton bg-orange">SQL ausführen</button>
            <span style="color: red; margin-left: 20px;">⚠️ Achtung: SQL-Befehle werden direkt ausgeführt!</span>
        </div>
        <br><br>
        <h3>Zeitbereich löschen</h3>
            <div>Erzeugt ein Lösch-Skript für Belege, Buchungen, Rechnungen und Angebote im Zeitraum und fügt es oben ins SQL-Feld ein.
                Zum Ausführen „Schreiben erlauben“ ankreuzen und obigen Button klicken. Dateien im Dateisystem bleiben erhalten.</div>
        <div>
            <label>Von:&nbsp;<input type="date" id="range_del_from"></label>
            <label>Bis:&nbsp;<input type="date" id="range_del_to"></label>
            <button type="button" onclick="buildRangeDeleteSql()" class="coloredButton bg-blue">SQL erzeugen</button>
        </div>
        <script>
        function buildRangeDeleteSql() {
            const from = document.getElementById('range_del_from').value;
            const to   = document.getElementById('range_del_to').value;
            if (!from || !to) { sqlStatus([['errorColor', 'Bitte Von- und Bis-Datum angeben.']]); return; }
            if (from > to)    { sqlStatus([['errorColor', 'Von-Datum liegt nach dem Bis-Datum.']]); return; }
            const b = "SELECT ID FROM Bookings  WHERE DateBooking BETWEEN '" + from + "' AND '" + to + "'";
            const d = "SELECT ID FROM Documents WHERE Date        BETWEEN '" + from + "' AND '" + to + "'";
            const i = "SELECT ID FROM Invoices  WHERE InvoiceDate BETWEEN '" + from + "' AND '" + to + "'";
            const sql =
                "-- Zeitbereich löschen: " + from + " bis " + to + "\\n" +
                "-- Reihenfolge beachtet Fremdschlüssel. Dateien im Dateisystem bleiben erhalten.\\n" +
                "DELETE FROM InvoicePayments WHERE InvoiceID IN (" + i + ") OR BookingID IN (" + b + ");\\n" +
                "DELETE FROM InvoiceItems WHERE InvoiceId IN (" + i + ");\\n" +
                "DELETE FROM InvoiceCorrections WHERE InvoiceID IN (" + i + ");\\n" +
                "DELETE FROM InvoiceDocumentVersions WHERE InvoiceID IN (" + i + ");\\n" +
                "UPDATE Invoices SET SourceQuoteId = NULL WHERE SourceQuoteId IN (" + i + ");\\n" +
                "UPDATE Invoices SET CancelsInvoiceId = NULL WHERE CancelsInvoiceId IN (" + i + ");\\n" +
                "UPDATE Invoices SET ReplacesInvoiceId = NULL WHERE ReplacesInvoiceId IN (" + i + ");\\n" +
                "DELETE FROM Invoices WHERE InvoiceDate BETWEEN '" + from + "' AND '" + to + "';  -- Rechnungen UND Angebote\\n" +
                "DELETE FROM BookingDocuments WHERE Booking_ID IN (" + b + ") OR Document_ID IN (" + d + ");\\n" +
                "UPDATE Assets SET Booking_ID = NULL WHERE Booking_ID IN (" + b + ");\\n" +
                "UPDATE Assets SET Document_ID = NULL WHERE Document_ID IN (" + d + ");\\n" +
                "UPDATE AssetDepreciations SET Booking_ID = NULL, Status = 'planned', BookedAt = NULL WHERE Booking_ID IN (" + b + ");\\n" +
                "UPDATE Trips SET DocumentID = NULL WHERE DocumentID IN (" + d + ");\\n" +
                "DELETE FROM DocumentReadings WHERE Document_ID IN (" + d + ");\\n" +
                "-- Sätze außerhalb des Zeitraums unter einer Bankbewegung des Zeitraums bleiben erhalten (abgehängt, wie im Buchungssatz-Editor).\\n" +
                "UPDATE Bookings SET ParentBooking_ID = NULL, Status = CASE WHEN Status = 'resolved' THEN NULL ELSE Status END WHERE ParentBooking_ID IN (" + b + ") AND DateBooking NOT BETWEEN '" + from + "' AND '" + to + "';\\n" +
                "DELETE FROM Bookings WHERE DateBooking BETWEEN '" + from + "' AND '" + to + "';\\n" +
                "DELETE FROM Documents WHERE Date BETWEEN '" + from + "' AND '" + to + "';";
            document.getElementById('sql_input').value = sql;
            sqlStatus([['', 'Lösch-Skript eingefügt — bitte prüfen, „Schreiben erlauben“ ankreuzen und „SQL ausführen“ klicken.']]);
        }
        </script>
        <div id="sql_status" style="margin-top: 10px;"></div>
        <textarea id="sql_output" rows="20" cols="100" readonly class="textareaSql" style="display: none;"></textarea>
        <script>
        // Meldungen nur über textContent: Fehlertexte kommen aus der Datenbank bzw. dem Server
        function sqlStatus(parts) {
            const status = document.getElementById('sql_status');
            status.textContent = '';
            parts.forEach(function(part) {
                const span = document.createElement('span');
                if (part[0]) span.className = part[0];
                span.textContent = part[1];
                status.appendChild(span);
                status.appendChild(document.createTextNode(' '));
            });
        }
        async function sendSql(input, write) {
            const output = document.getElementById('sql_output');
            sqlStatus([['', '⏳ Wird ausgeführt...']]);
            try {
                const resp = await fetch('/execute_sql', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'sql_commands=' + encodeURIComponent(input) + (write ? '&write=1' : '')
                });
                const data = await resp.json();
                const parts = [];
                if (data.success_count > 0) parts.push(['successColor', data.success_count + ' von ' + data.total + ' Befehlen erfolgreich.']);
                if (data.errors && data.errors.length) parts.push(['errorColor', data.errors.join('; ')]);
                if (data.warnings && data.warnings.length) parts.push(['warnColor', data.warnings.join('; ')]);
                sqlStatus(parts);
                if (data.output) {
                    output.style.display = 'block';
                    output.value = data.output;
                } else {
                    output.style.display = 'none';
                    output.value = '';
                }
            } catch(e) {
                sqlStatus([['errorColor', 'Fehler: ' + e.message]]);
            } finally {
                if (write) document.getElementById('sql_write').checked = false;
            }
        }
        function executeSql() {
            const input = document.getElementById('sql_input').value;
            if (!input.trim()) { sqlStatus([['errorColor', 'Keine SQL-Befehle eingegeben.']]); return; }
            if (!document.getElementById('sql_write').checked) return sendSql(input, false);
            appConfirm('Die Befehle können Daten ändern oder löschen. Vorher eine Datensicherung anlegen. Ausführen?',
                       function() { return sendSql(input, true); });
        }
        </script>
        '''
    s += '</div>'
    return s


def _about_db_export(query):
    """Der Kasten „DB-Export“ (``POST /db_export``) – er hängt an der SQL-Konsole und steht nur, wo sie steht. ``query``: die Query der Adresse; nennt
    sie das Ergebnis eines Exports (``export_message``), meldet ein Skript es nach dem Laden über ``appMsg`` (der Text als JSON in den Skriptblock)."""
    s = '<div class="rectRounded">'
    s += '''
        <h2>DB-Export</h2>
        <p>Exportiert alle Tabelleninhalte als INSERT-Statements in die Datei
        <code>db-export.sql</code> im Datenverzeichnis.
        Ein kleinerer Export lässt sich im SQL-Konsolenbereich einfügen (mit „Schreiben erlauben“);
        für ganze Bestände gibt es die Datensicherung.</p>
        <form method="POST" action="/db_export">
            <button type="submit" class="coloredButton bg-blue">&#x1F4BE; DB-Export</button>
        </form>
        '''
    message = export_message(query)
    if message:
        s += ("<script>document.addEventListener('DOMContentLoaded',function(){"
              f"appMsg({script_json(message[0])},{script_json(message[1])});}});</script>")
    s += '</div>'
    return s


def PageAbout(db=None, query=None):
    """Generate about page.

    Mit ``db`` (der Server übergibt sie immer) folgen auf „Projekt in Zahlen“ die Datenbank-Übersicht – für jeden angemeldeten Benutzer –
    und, nur für die, die die SQL-Konsole benutzen dürfen (``auth.sql_console_allowed``), ganz unten die SQL-Konsole und der DB-Export;
    wer sie nicht benutzen darf, sieht weder die Kästen noch ihre Skripte. Ohne ``db`` bleibt die Seite, wie sie war.
    ``query``: die Query der Adresse (``parse_qs``); sie trägt nur das Ergebnis eines DB-Exports (``export_message``)."""
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
    s+= _about_stats()
    if db is not None:
        import auth as _auth
        s+= _about_database_overview(db)
        if _auth.sql_console_allowed():
            s+= '<br><br><div class="grid1Col gridMiddle">' + _about_sql_console() + _about_db_export(query or {}) + '</div>'
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
