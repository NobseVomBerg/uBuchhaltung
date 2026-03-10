"""
Sonstiges page – database overview, SQL console, misc tools.
"""
from db import Database


def Header1(active_page=None):
    from server.pages import Header1 as _H
    return _H(active_page)

def Header2(content=""):
    from server.pages import Header2 as _H
    return _H(content)

def Header3(content=""):
    from server.pages import Header3 as _H
    return _H(content)

def Footer():
    from server.pages import Footer as _F
    return _F()


def PageMiscellaneous(db: Database):
    """Generate Sonstiges page (database overview, SQL console, misc tools)"""
    import urllib.parse
    from urllib.parse import parse_qs, urlparse
    s = Header1('miscellaneous')
    s += Header2()
    s += Header3()

    # Database statistics
    stats = db.get_table_statistics()
    s += "<h2>Datenbank-Übersicht</h2>"
    s += "<table>"
    s += "<tr><th>Tabelle</th><th>Anzahl Einträge</th></tr>"
    for table_name, count in stats:
        s += f"<tr><td>{table_name}</td><td style='text-align: right;'>{count}</td></tr>"
    s += "</table>"

    s += '''
    <h2>DB-Export</h2>
    <p>Exportiert alle Tabelleninhalte als INSERT-Statements nach <code>./data/db-export.sql</code>.
       Die Datei kann direkt im SQL-Konsolenbereich unten eingefügt werden.</p>
    <a href="/db_export" class="coloredButton btn-blue">&#x1F4BE; DB-Export</a>
    '''

    # Show export result message if redirected back with status
    s += '''
    <script>
    (function() {
        const p = new URLSearchParams(window.location.search);
        const status = p.get('export');
        if (status === 'ok') {
            const div = document.createElement('div');
            div.style = 'margin-top:10px; padding:8px 14px; background:#d4edda; color:#155724; border-radius:4px; display:inline-block;';
            div.textContent = '✅ Export erfolgreich: ' + p.get('tables') + ' Tabellen, ' + p.get('rows') + ' Zeilen.';
            document.currentScript.parentNode.insertBefore(div, document.currentScript);
        } else if (status === 'error') {
            const div = document.createElement('div');
            div.style = 'margin-top:10px; padding:8px 14px; background:#f8d7da; color:#721c24; border-radius:4px; display:inline-block;';
            div.textContent = '❌ Fehler beim Export: ' + p.get('msg');
            document.currentScript.parentNode.insertBefore(div, document.currentScript);
        }
    })();
    </script>
    '''

    # ── DATEV Export ──────────────────────────────────────────────────────────
    import datetime
    cur_year = datetime.date.today().year
    s += f'''
    <hr>
    <h2>DATEV Export</h2>
    <p>Exportiert Buchungen als <strong>DATEV Buchungsstapel-CSV</strong> (EXTF 700, Encoding CP1252).<br>
       Das Feld <em>Datum Zuord. Steuerperiode</em> (Spalte 115) wird für alle exportierten
       Buchungen auf das <strong>heutige Datum</strong> gesetzt.</p>
    <form method="POST" action="/datev/export">
        <label>Von:&nbsp;<input type="date" name="date_from" value="{cur_year}-01-01" required></label>
        &nbsp;&nbsp;
        <label>Bis:&nbsp;<input type="date" name="date_to" value="{cur_year}-12-31" required></label>
        &nbsp;&nbsp;
        <button type="submit" class="coloredButton btn-blue">&#x1F4E5; DATEV-CSV exportieren</button>
    </form>
    <script>
    (function() {{
        const p = new URLSearchParams(window.location.search);
        const status = p.get('datev_export');
        if (status === 'error') {{
            const div = document.createElement('div');
            div.style = 'margin-top:10px; padding:8px 14px; background:#f8d7da; color:#721c24; border-radius:4px; display:inline-block;';
            div.textContent = '❌ DATEV-Export Fehler: ' + decodeURIComponent(p.get('msg') || '');
            document.currentScript.parentNode.insertBefore(div, document.currentScript);
        }}
    }})();
    </script>
    '''

    s += '''
    <h2>SQL-Befehle ausführen</h2>
    <form method="POST" action="/execute_sql">
        <p>Geben Sie hier SQL-Befehle ein (mehrere Befehle durch Semikolon getrennt):</p>
        <textarea name="sql_commands" rows="15" cols="100" style="font-family: monospace; width: 100%; max-width: 1000px;" placeholder="INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard) VALUES (4, 1000, 'Kasse', 'Barkasse', 1);
INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard) VALUES (4, 1200, 'Bank', 'Bankguthaben', 1);"></textarea>
        <br>
        <input type="submit" value="SQL ausführen" class="coloredButton btn-green">
        <span style="color: red; margin-left: 20px;">⚠️ Vorsicht: SQL-Befehle werden direkt ausgeführt!</span>
    </form>

    <div id="sql_result" style="margin-top: 20px;"></div>
    '''
    s += Footer()
    return s
