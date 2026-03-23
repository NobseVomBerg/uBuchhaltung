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

    s += '<div class="grid-1RowPrefered">'
    # ── Database statistics ───────────────────────────────────────────────────
    s += '\t<div class="rectRounded">'
    stats = db.get_table_statistics()
    s += "\t\t<h2>Datenbank-Übersicht</h2>"
    s += "\t\t<table>"
    s += "\t\t\t<tr><th>Tabelle</th><th>Anzahl Einträge</th></tr>"
    for table_name, count in stats:
        s += f"\t\t\t<tr><td>{table_name}</td><td style='text-align: right;'>{count}</td></tr>"
    s += "\t\t</table>"
    s += '\t</div>'
    # ── SQL Input Field ───────────────────────────────────────────────────────
    s += '\t<div class="rectRounded" style="grid-column: span 2">'
    s += '''
        <h2>SQL-Befehle ausführen</h2>
        <form method="POST" action="/execute_sql">
            <p>Gib hier SQL-Befehle ein (mehrere Befehle durch Semikolon getrennt):</p>
            <textarea name="sql_commands" rows="15" cols="100" style="font-family: monospace; width: 100%; max-width: 1000px;" placeholder="INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard) VALUES (4, 1000, 'Kasse', 'Barkasse', 1);
INSERT INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard) VALUES (4, 1200, 'Bank', 'Bankguthaben', 1);"></textarea>
            <br>
            <input type="submit" value="SQL ausführen" class="coloredButton btn-orange">
            <span style="color: red; margin-left: 20px;">⚠️ Vorsicht: SQL-Befehle werden direkt ausgeführt!</span>
        </form>

        <div id="sql_result" style="margin-top: 20px;"></div>
        '''
    s += '\t</div>'
    s += '</div>'

    s += '<div class="grid-1RowPrefered">'
    # ── DB Export as SQL ──────────────────────────────────────────────────────
    s += '\t<div class="rectRounded">'
    s += '''
        <h2>DB-Export</h2>
        <p>Exportiert alle Tabelleninhalte als INSERT-Statements nach <code>./data/db-export.sql</code>.
        Die Datei kann direkt im SQL-Konsolenbereich unten eingefügt werden.</p>
        <form method="POST" action="/db_export">
            <button type="submit" class="coloredButton btn-blue">&#x1F4BE; DB-Export</button>
        </form>
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
    s += '\t</div>'
    s += '\t<div class="rectRounded">'
    # ── DATEV Export ──────────────────────────────────────────────────────────
    import datetime
    cur_year = datetime.date.today().year
    s += f'''
        <h2>DATEV Export</h2>
        <p>Exportiert Buchungen als <strong>DATEV Buchungsstapel-CSV</strong> (EXTF 700, Encoding CP1252).<br>
        Das Feld <em>Datum Zuord. Steuerperiode</em> wird für alle exportierten Buchungen auf das <strong>heutige Datum</strong> gesetzt.</p>
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
    s += '\t</div>'
    s += '\t<div class="rectRounded">'
    # ── WISO Mein Büro Import ─────────────────────────────────────────────────
    import json as _json, os as _os
    from urllib.parse import parse_qs as _parse_qs, urlparse as _urlparse

    # Letztes Import-Ergebnis einlesen (falls vorhanden)
    _result_path = _os.path.join('data', 'wiso_import_result.json')
    _last_result = None
    if _os.path.exists(_result_path):
        try:
            with open(_result_path, encoding='utf-8') as _f:
                _last_result = _json.load(_f)
        except Exception:
            _last_result = None

    s += '''
        <h2>WISO Mein Büro Import</h2>
        <p>Importiert Buchungen aus dem <strong>WISO Mein Büro Bewegungsdaten-Export</strong>
        (Datei &rarr; Export &rarr; &bdquo;Buchungsdaten als CSV&ldquo;).<br>
        Voraussetzung: Jedes Konto in der <strong>Kontenverwaltung</strong>
        muss ein <em>SKR-Gegenkonto</em> hinterlegt haben (z.B. 1810 für 2. Bankkonto, 1460 für Kasse).<br>
        Duplikate (gleiche Referenznummer + Konto + Betrag) werden automatisch übersprungen.</p>
        <form method="POST" action="/wiso/import" enctype="multipart/form-data">
            <label>CSV-Datei:&nbsp;<input type="file" name="csvfile" accept=".txt,.csv" required></label>
            &nbsp;&nbsp;
            <button type="submit" class="coloredButton btn-green">&#x1F4C2; WISO Import starten</button>
        </form>
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('wiso_import');
            if (!status) return;
            const div = document.createElement('div');
            div.style = 'margin-top:10px; padding:8px 14px; border-radius:4px; display:inline-block;';
            if (status === 'ok') {
                const ec  = parseInt(p.get('err_count')    || '0');
                const mc  = parseInt(p.get('missing_coa')  || '0');
                const ms  = parseInt(p.get('missing_skr')  || '0');
                const nf  = parseInt(p.get('not_found')    || '0');
                const warn = ec > 0 || mc > 0 || ms > 0 || nf > 0;
                let msg = '\u2705 Import abgeschlossen: '
                        + p.get('imported') + ' importiert, '
                        + p.get('updated')  + ' aktualisiert, '
                        + p.get('skipped')  + ' \u00fcbersprungen';
                if (nf > 0) msg += ', ' + nf + ' nicht gefunden';
                if (mc > 0) msg += ', ' + mc + ' fehlende SKR-Konten';
                if (ms > 0) msg += ', ' + ms + ' fehlende Gegenkonten';
                if (ec > 0) msg += ', ' + ec + ' Fehler';
                msg += '. Siehe Details unten.';
                div.style.background = warn ? '#fff3cd' : '#d4edda';
                div.style.color      = warn ? '#856404' : '#155724';
                div.textContent = msg;
            } else {
                div.style.background = '#f8d7da';
                div.style.color = '#721c24';
                div.textContent = '\u274c Fehler: ' + decodeURIComponent(p.get('msg') || '');
            }
            document.currentScript.parentNode.insertBefore(div, document.currentScript);
        })();
        </script>
        '''

    # Detail-Tabellen aus letztem Ergebnis rendern
    if _last_result:
        _skipped_rows = _last_result.get('skipped_rows', [])
        _not_found    = _last_result.get('not_found', [])
        _missing_coa  = _last_result.get('missing_coa', [])
        _missing_skr  = _last_result.get('missing_skr', [])
        _errors       = _last_result.get('errors', [])

        if _skipped_rows:
            s += f'<h3>\u26a0\ufe0f Übersprungene Zeilen (Duplikate) &ndash; {len(_skipped_rows)} Einträge</h3>'
            s += '<table><tr><th>CSV-Zeile</th><th>Datum</th><th>Referenz</th><th>KONTO</th><th>Betrag</th><th>Text</th></tr>'
            for _r in _skipped_rows:
                s += (f"<tr><td>{_r.get('zeile','')}</td><td>{_r.get('datum','')}</td>"
                      f"<td>{_r.get('ref','')}</td><td>{_r.get('konto','')}</td>"
                      f"<td>{_r.get('betrag','')}</td><td>{_r.get('text','')}</td></tr>")
            s += '</table>'

        if _missing_coa:
            s += f'<h3>\u26a0\ufe0f Fehlende SKR-Konten (KONTO nicht in Kontenrahmen) &ndash; {len(_missing_coa)} Einträge</h3>'
            s += '<p>Diese Kontonummern wurden in den Buchungen verwendet, sind aber noch nicht im Kontenrahmen hinterlegt.<br>'
            s += 'Die Buchungen wurden trotzdem importiert (ohne COA-Zuweisung). Bitte <a href="/masterdata/skr">in der SKR-Verwaltung</a> ergänzen:</p>'
            s += '<p style="font-family:monospace;">'
            s += ', '.join(str(_n) for _n in _missing_coa)
            s += '</p>'

        if _missing_skr:
            s += f'<h3>\u26a0\ufe0f Fehlende Gegenkonten (GEGENKONTO nicht in Kontenverwaltung) &ndash; {len(_missing_skr)} Einträge</h3>'
            s += '<p>Diese SKR-Nummern stehen in der GEGENKONTO-Spalte, sind aber keinem Konto in der <a href="/masterdata/bankaccounts">Kontenverwaltung</a> zugewiesen:</p>'
            s += '<p style="font-family:monospace;">'
            s += ', '.join(str(_n) for _n in _missing_skr)
            s += '</p>'
        if _not_found:
            s += f'<h3>\u26a0\ufe0f Nicht gefunden (kein Treffer in DB) &ndash; {len(_not_found)} Eintr\u00e4ge</h3>'
            s += '<p>Diese Zeilen konnten keiner bestehenden Buchung zugeordnet werden (Datum + Belegr./Betrag stimmt nicht \u00fcberein).</p>'
            s += '<table><tr><th>CSV-Zeile</th><th>Datum</th><th>Beleg-Nr.</th><th>Betrag</th><th>Text</th></tr>'
            for _r in _not_found:
                s += (f"<tr><td>{_r.get('zeile','')}</td><td>{_r.get('datum','')}</td>"
                      f"<td>{_r.get('beleg','')}</td><td>{_r.get('betrag','')}</td>"
                      f"<td>{_r.get('text','')}</td></tr>")
            s += '</table>'
        if _errors:
            s += f'<h3>\u274c Parse-Fehler &ndash; {len(_errors)} Einträge</h3><ul>'
            for _e in _errors:
                s += f'<li>{_e}</li>'
            s += '</ul>'
    s += '\t</div>'
    s += '</div>'

    s += Footer()
    return s
