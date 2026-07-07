"""
Sonstiges page – database overview, SQL console, misc tools.
"""
import html as _html
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

    s += '<div class="grid3Cols gridMain">'
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
        Gib hier SQL-Befehle ein (mehrere Befehle durch Semikolon getrennt):<br>
        <textarea id="sql_input" rows="15" cols="100" class="textareaSql" placeholder="SELECT * FROM ChartOfAccounts WHERE AccountNumber = 6805;"></textarea>
        <div>
            <button type="button" onclick="executeSql()" class="coloredButton bg-orange">SQL ausführen</button>
            <span style="color: red; margin-left: 20px;">⚠️ Achtung: SQL-Befehle werden direkt ausgeführt!</span>
        </div>
        <br><br>
        <h3>Zeitbereich löschen</h3>
            <div>Erzeugt ein Lösch-Skript für Belege, Buchungen, Rechnungen und Angebote im Zeitraum und fügt es oben ins SQL-Feld ein.
                Ausführung erfolgt mit Klick auf obigen Button. Dateien im Dateisystem bleiben erhalten.</div>
        <div>
            <label>Von:&nbsp;<input type="date" id="range_del_from"></label>
            <label>Bis:&nbsp;<input type="date" id="range_del_to"></label>
            <button type="button" onclick="buildRangeDeleteSql()" class="coloredButton bg-blue">SQL erzeugen</button>
        </div>
        <script>
        function buildRangeDeleteSql() {
            const from = document.getElementById('range_del_from').value;
            const to   = document.getElementById('range_del_to').value;
            const status = document.getElementById('sql_status');
            if (!from || !to) { status.innerHTML = '<span class="errorColor">Bitte Von- und Bis-Datum angeben.</span>'; return; }
            if (from > to)    { status.innerHTML = '<span class="errorColor">Von-Datum liegt nach dem Bis-Datum.</span>'; return; }
            const b = "SELECT ID FROM Bookings  WHERE DateBooking BETWEEN '" + from + "' AND '" + to + "'";
            const d = "SELECT ID FROM Documents WHERE Date        BETWEEN '" + from + "' AND '" + to + "'";
            const i = "SELECT ID FROM Invoices  WHERE InvoiceDate BETWEEN '" + from + "' AND '" + to + "'";
            const sql =
                "-- Zeitbereich löschen: " + from + " bis " + to + "\\n" +
                "-- Reihenfolge beachtet Fremdschlüssel. Dateien im Dateisystem bleiben erhalten.\\n" +
                "DELETE FROM InvoicePayments WHERE InvoiceID IN (" + i + ") OR BookingID IN (" + b + ");\\n" +
                "DELETE FROM InvoiceItems WHERE InvoiceId IN (" + i + ");\\n" +
                "UPDATE Invoices SET SourceQuoteId = NULL WHERE SourceQuoteId IN (" + i + ");\\n" +
                "DELETE FROM Invoices WHERE InvoiceDate BETWEEN '" + from + "' AND '" + to + "';  -- Rechnungen UND Angebote\\n" +
                "DELETE FROM BookingDocuments WHERE Booking_ID IN (" + b + ") OR Document_ID IN (" + d + ");\\n" +
                "UPDATE Assets SET Booking_ID = NULL WHERE Booking_ID IN (" + b + ");\\n" +
                "UPDATE Assets SET Document_ID = NULL WHERE Document_ID IN (" + d + ");\\n" +
                "UPDATE AssetDepreciations SET Booking_ID = NULL WHERE Booking_ID IN (" + b + ");\\n" +
                "UPDATE Trips SET DocumentID = NULL WHERE DocumentID IN (" + d + ");\\n" +
                "DELETE FROM Bookings WHERE DateBooking BETWEEN '" + from + "' AND '" + to + "';\\n" +
                "DELETE FROM BookingGroups WHERE ID NOT IN (SELECT DISTINCT BookingGroup_ID FROM Bookings WHERE BookingGroup_ID IS NOT NULL);\\n" +
                "DELETE FROM Documents WHERE Date BETWEEN '" + from + "' AND '" + to + "';";
            document.getElementById('sql_input').value = sql;
            status.innerHTML = 'Lösch-Skript eingefügt — bitte prüfen und dann „SQL ausführen" klicken.';
        }
        </script>
        <div id="sql_status" style="margin-top: 10px;"></div>
        <textarea id="sql_output" rows="20" cols="100" readonly class="textareaSql" style="display: none;"></textarea>
        <script>
        async function executeSql() {
            const input = document.getElementById('sql_input').value;
            const output = document.getElementById('sql_output');
            const status = document.getElementById('sql_status');
            if (!input.trim()) { status.innerHTML = '<span class="errorColor">Keine SQL-Befehle eingegeben.</span>'; return; }
            status.innerHTML = '⏳ Wird ausgeführt...';
            try {
                const resp = await fetch('/execute_sql', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'sql_commands=' + encodeURIComponent(input)
                });
                const data = await resp.json();
                let statusHtml = '';
                if (data.success_count > 0) statusHtml += '<span class="successColor">' + data.success_count + ' von ' + data.total + ' Befehlen erfolgreich.</span> ';
                if (data.errors && data.errors.length) statusHtml += '<span class="errorColor">' + data.errors.join('; ') + '</span>';
                status.innerHTML = statusHtml;
                if (data.output) {
                    output.style.display = 'block';
                    output.value = data.output;
                } else {
                    output.style.display = 'none';
                    output.value = '';
                }
            } catch(e) {
                status.innerHTML = '<span class="errorColor">Fehler: ' + e.message + '</span>';
            }
        }
        </script>
        '''
    s += '\t</div>'

    # Both Exports together in one DIV
    s += '\t<div class="grid1Col gridMiddle">'
    # ── DB Export as SQL ──────────────────────────────────────────────────────
    s += '\t\t<div class="rectRounded">'
    s += '''
        <h2>DB-Export</h2>
        <p>Exportiert alle Tabelleninhalte als INSERT-Statements in die Datei
        <code>db-export.sql</code> im Datenverzeichnis.
        Die Datei kann direkt im SQL-Konsolenbereich eingefügt werden.</p>
        <form method="POST" action="/db_export">
            <button type="submit" class="coloredButton bg-blue">&#x1F4BE; DB-Export</button>
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
    s += '\t\t</div>'
    # ── DATEV Export ──────────────────────────────────────────────────────────
    s += '\t\t<div class="rectRounded">'
    import datetime
    cur_year = datetime.date.today().year
    s += f'''
        <h2>DATEV Export</h2>
        <p>Exportiert Buchungen als <strong>DATEV Buchungsstapel-CSV</strong> (EXTF 700, Encoding CP1252).<br>
        Das Feld <em>Datum Zuord. Steuerperiode</em> wird für alle exportierten Buchungen auf das <strong>heutige Datum</strong> gesetzt.</p>
        <form method="POST" action="/datev/export">
            <div class="rowWithObjects">
                <label>Von:&nbsp;<input type="date" name="date_from" value="{cur_year}-01-01" required></label>
                <label>Bis:&nbsp;<input type="date" name="date_to" value="{cur_year}-12-31" required></label>
            </div>
            <br>
            <button type="submit" class="coloredButton bg-blue">&#x1F4E5; DATEV-CSV exportieren</button>
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
    s += '\t\t</div>'
    s += '\t</div>'

    s += '\t<div class="rectRounded">'
    # ── WISO Mein Büro Import ─────────────────────────────────────────────────
    import json as _json, os as _os
    from urllib.parse import parse_qs as _parse_qs, urlparse as _urlparse

    # Letztes Import-Ergebnis einlesen (falls vorhanden)
    import userctx as _userctx
    _result_path = _os.path.join(_userctx.user_data_dir(), 'wiso_import_result.json')
    _last_result = None
    if _os.path.exists(_result_path):
        try:
            with open(_result_path, encoding='utf-8') as _f:
                _last_result = _json.load(_f)
        except Exception:
            _last_result = None

    s += '''
        <h2>WISO Mein Büro Import</h2>
        <p>Importiert Buchungen aus <strong>WISO Mein Büro</strong>.<br>
        1. Bewegungsdaten-Export
        (Datei &rarr; Export &rarr; &bdquo;Buchungsdaten als CSV&ldquo;).<br>
        2. Tabellen-Export (nur ergänzend, direkt aus der Tabellenansicht exportieren)<br>
        Voraussetzung: Jedes Konto in der <strong>Kontenverwaltung</strong>
        muss ein <em>SKR-Gegenkonto</em> hinterlegt haben (z.B. 1810 für 2. Bankkonto, 1460 für Kasse).<br>
        Duplikate (gleiche Referenznummer + Konto + Betrag) werden automatisch übersprungen.</p>
        <form method="POST" action="/wiso/import" enctype="multipart/form-data">
            <div class="rowWithObjects">CSV-Datei:&nbsp;<input type="file" name="csvfile" accept=".txt,.csv" required></div>
            <br>
            <button type="submit" class="coloredButton bg-green">&#x1F4C2; WISO Import starten</button>
        </form>
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('wiso_import');
            if (!status) return;
            const file = p.get('file');
            const div = document.createElement('div');
            div.style = 'margin-top:10px; padding:8px 14px; border-radius:4px; display:inline-block;';
            let msg;
            if (status === 'ok') {
                const ec  = parseInt(p.get('err_count')    || '0');
                const mc  = parseInt(p.get('missing_coa')  || '0');
                const ms  = parseInt(p.get('missing_skr')  || '0');
                const nf  = parseInt(p.get('not_found')    || '0');
                const warn = ec > 0 || mc > 0 || ms > 0 || nf > 0;
                msg = '\u2705 Import abgeschlossen: '
                        + p.get('imported') + ' importiert, '
                        + p.get('updated')  + ' aktualisiert, '
                        + p.get('skipped')  + ' \u00fcbersprungen';
                const lk = parseInt(p.get('linked') || '0');
                const rv = parseInt(p.get('resolved') || '0');
                if (lk > 0) msg += ', ' + lk + ' verkn\u00fcpft';
                if (rv > 0) msg += ', ' + rv + ' Debitoren aufgel\u00f6st';
                if (nf > 0) msg += ', ' + nf + ' nicht gefunden';
                if (mc > 0) msg += ', ' + mc + ' fehlende SKR-Konten';
                if (ms > 0) msg += ', ' + ms + ' fehlende Gegenkonten';
                if (ec > 0) msg += ', ' + ec + ' Fehler';
                msg += '. Siehe Details unten.';
                div.style.background = warn ? '#fff3cd' : '#d4edda';
                div.style.color      = warn ? '#856404' : '#155724';
            } else {
                div.style.background = '#f8d7da';
                div.style.color = '#721c24';
                msg = '\u274c Fehler: ' + decodeURIComponent(p.get('msg') || '');
            }
            // Dateiname als erste Zeile in derselben Box (append = Textknoten, kein HTML)
            if (file) {
                div.append('\U0001F4C4 Verarbeitete Datei: ' + file);
                div.appendChild(document.createElement('br'));
            }
            div.append(msg);
            document.currentScript.parentNode.insertBefore(div, document.currentScript);
        })();
        </script>
        '''

    # Detail-Tabellen aus letztem Ergebnis rendern (Dateiname steht in der
    # Import-abgeschlossen-Box; hier nicht erneut anzeigen)
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
            s += ', '.join(_html.escape(str(_n)) for _n in _missing_coa)
            s += '</p>'

        if _missing_skr:
            s += f'<h3>\u26a0\ufe0f Fehlende Gegenkonten (GEGENKONTO nicht in Kontenverwaltung) &ndash; {len(_missing_skr)} Einträge</h3>'
            s += '<p>Diese SKR-Nummern stehen in der GEGENKONTO-Spalte, sind aber keinem Konto in der <a href="/masterdata/bankaccounts">Kontenverwaltung</a> zugewiesen:</p>'
            s += '<p style="font-family:monospace;">'
            s += ', '.join(_html.escape(str(_n)) for _n in _missing_skr)
            s += '</p>'
        if _not_found:
            s += f'<h3>\u26a0\ufe0f Nicht eindeutig zuordenbar &ndash; {len(_not_found)} Eintr\u00e4ge</h3>'
            s += ('<p>F\u00fcr diese Zeilen wurde keine <em>eindeutige</em> bestehende Buchung gefunden '
                  '(kein Treffer oder mehrere Kandidaten \u00fcber Datum/Beleg-Nr./Betrag/Text). '
                  'Es wurde nichts ge\u00e4ndert &ndash; die Buchungen k\u00f6nnen bereits vollst\u00e4ndig '
                  'vorhanden sein; bei Bedarf manuell pr\u00fcfen.</p>')
            s += '<table><tr><th>CSV-Zeile</th><th>Datum</th><th>Beleg-Nr.</th><th>Betrag</th><th>Text</th></tr>'
            for _r in _not_found:
                s += (f"<tr><td>{_html.escape(str(_r.get('zeile','')))}</td><td>{_html.escape(str(_r.get('datum','')))}</td>"
                      f"<td>{_html.escape(str(_r.get('beleg','')))}</td><td>{_html.escape(str(_r.get('betrag','')))}</td>"
                      f"<td>{_html.escape(str(_r.get('text','')))}</td></tr>")
            s += '</table>'
        if _errors:
            s += f'<h3>\u274c Parse-Fehler &ndash; {len(_errors)} Einträge</h3><ul>'
            for _e in _errors:
                s += f'<li>{_html.escape(str(_e))}</li>'
            s += '</ul>'
    s += '\t</div>'
    # ── Testdaten ─────────────────────────────────────────────────────────────
    s += '\t<div class="rectRounded">'
    s += '''
        <h2>🧪 Testdaten</h2>
        <p>Lädt alle Testdaten aus <code>seed_data/test/</code> nach (Artikel, Kontakte, Anlagen, Rechnungen, ...).<br>
        Der Vorgang ist <strong>idempotent</strong> – bereits vorhandene Einträge werden übersprungen.</p>
        <form method="POST" action="/setup/load_testdata">
            <button type="submit" class="coloredButton bg-orange">🧪 Testdaten nachladen</button>
        </form>
        '''
    s += '\t</div>'

    # ── Datensicherung ────────────────────────────────────────────────────────
    from server.backup import list_backups as _list_backups
    _backups = _list_backups(_userctx.user_data_dir())

    s += '\t<div class="rectRounded">'
    s += '''
        <h2>&#x1F4BE; Datensicherung</h2>
        <p>Sichert die Daten des angemeldeten Benutzers als Archiv nach
        <code>backup/JJJJMMTT_Backup.7zip</code> (ohne installiertes 7-Zip als <code>.zip</code>).
        Die Datenbank wird dabei als konsistenter Snapshot gesichert.</p>
        <form method="POST" action="/backup/create">
            <div class="rowWithObjects">
                <label><input type="radio" name="scope" value="db"> nur DB</label>
                <label><input type="radio" name="scope" value="all" checked> alle Daten</label>
                <button type="submit" class="coloredButton bg-blue">&#x1F4BE; Backup erstellen</button>
            </div>
        </form>
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('backup');
            if (!status) return;
            let text, type;
            if (status === 'ok') {
                const kb = Math.round(parseInt(p.get('size') || '0') / 1024);
                text = 'Backup erstellt: ' + p.get('file') + ' (' + kb + ' KB)'; type = 'success';
            } else {
                text = 'Backup fehlgeschlagen: ' + decodeURIComponent(p.get('msg') || ''); type = 'error';
            }
            document.addEventListener('DOMContentLoaded', () => appMsg(text, type));
        })();
        </script>
        '''
    s += '\t</div>'

    # ── Wiederherstellung ─────────────────────────────────────────────────────
    s += '\t<div class="rectRounded">'
    s += '<h2>&#x267B;&#xFE0F; Wiederherstellung</h2>'
    if _backups:
        _opts = ''.join(f'<option value="{_html.escape(b)}">{_html.escape(b)}</option>' for b in _backups)
        s += f'''
        <p>Stellt ein Archiv aus <code>backup/</code> im Benutzerverzeichnis wieder her.</p>
        <form method="POST" action="/backup/restore"
              onsubmit="return confirm('Backup wirklich wiederherstellen? Aktuelle Daten werden ersetzt!')">
            <div class="rowWithObjects">Archiv:&nbsp;<select name="archive">{_opts}</select></div>
            <div class="rowWithObjects">
                <label><input type="radio" name="mode" value="wipe"> vorhandene Daten vorher löschen</label>
                <label><input type="radio" name="mode" value="overwrite" checked> vorhandene Daten überschreiben</label>
            </div>
            <br>
            <button type="submit" class="coloredButton bg-red">&#x267B;&#xFE0F; Wiederherstellen</button>
        </form>
        '''
    else:
        s += '<p class="muted">Noch keine Backups vorhanden.</p>'
    s += '''
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('restore');
            if (!status) return;
            let text, type;
            if (status === 'ok') {
                text = 'Backup wiederhergestellt: ' + p.get('file'); type = 'success';
            } else {
                text = 'Wiederherstellung fehlgeschlagen: ' + decodeURIComponent(p.get('msg') || ''); type = 'error';
            }
            document.addEventListener('DOMContentLoaded', () => appMsg(text, type));
        })();
        </script>
        '''
    s += '\t</div>'
    s += '</div>'

    s += Footer()
    return s
