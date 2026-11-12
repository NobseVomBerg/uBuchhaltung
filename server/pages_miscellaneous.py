# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Sonstiges page – database overview, SQL console, misc tools.
"""
import html as _html
from db import Database
from db.wiso_fdb_import import MAX_ADOPTED_ROWS
from money import fmt_eur


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


def _fdb_result_rows(rows):
    """Zeilen einer gespeicherten Ergebnisliste (``adopted_rows``, ``possible_duplicates``).

    Die Ergebnisdatei liegt im Datenordner und kann von Hand verändert sein: Was keine
    Liste ist, gilt als leer, Einträge, die kein Objekt sind, übergeht die Seite –
    statt mit einem Fehler abzubrechen (samt SQL-Konsole).
    """
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _fdb_rows_table(rows):
    """Datum, Betrag, Text und Buchungs-ID der Zeilen aus :func:`_fdb_result_rows` als Tabelle."""
    s = '<table><tr><th>Datum</th><th>Betrag</th><th>Text</th><th>Buchungs-ID</th></tr>'
    for row in rows:
        try:
            amount = fmt_eur(row.get('amount'))
        except Exception:                       # Ergebnisdatei von Hand verändert
            amount = str(row.get('amount'))
        s += (f'<tr><td>{_html.escape(str(row.get("date") or ""))}</td>'
              f'<td>{_html.escape(amount)}</td>'
              f'<td>{_html.escape(str(row.get("text") or ""))}</td>'
              f'<td>{_html.escape(str(row.get("id") or ""))}</td></tr>')
    return s + '</table>'


def _result_count(value):
    """Zähler aus einer gespeicherten Ergebnisdatei: eine ganze Zahl ab 1, sonst 0 (die Datei
    liegt im Datenordner und kann von Hand verändert sein)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _import_notices(result):
    """Hinweise zu Vorzeichen, Konten 1000–1099 und älteren Überträgen aus dem Ergebnis eines
    WISO-Imports (Text- und Datenbank-Import melden beide ``negative_gross``,
    ``stock_range_accounts``, ``legacy_stock_rows`` und ``legacy_mirrors``) als HTML."""
    s = ''
    negative = _result_count(result.get('negative_gross'))
    if negative:
        s += (f'<h3>\u26a0\ufe0f Negative Bruttobeträge &ndash; {negative} Sätze</h3>'
              '<p>Bei diesen Sätzen steht genau ein Zahlungskonto (Bank, Kasse oder '
              'Verrechnungskonto) auf einer Seite. Das Vorzeichen richtet sich dann nach der '
              'Geldbewegung, nicht nach dem negativen Bruttobetrag. Stornos und Gutschriften '
              'können dadurch als normale Buchung gelesen werden &ndash; eine Erstattung etwa als '
              'Ausgabe &ndash; bitte nachsehen.</p>')
    accounts = [row for row in (result.get('stock_range_accounts') or []) if isinstance(row, dict)]
    if accounts:
        s += (f'<h3>\u26a0\ufe0f Konten 1000&ndash;1099 &ndash; {len(accounts)}</h3>'
              '<p>Konten 1000&ndash;1099 sind im SKR04 Vorräte; ältere Versionen haben sie als '
              'Kasse gelesen &ndash; bitte prüfen, ob hier eine Kasse gemeint ist. Buchungen gegen '
              'diese Konten werden als Umbuchung gelesen und erscheinen nicht in der EÜR.</p>'
              '<table><tr><th>Konto</th><th>Bezeichnung</th><th>Buchungen</th></tr>')
        for row in accounts:
            s += (f'<tr><td>{_html.escape(str(row.get("nummer", "")))}</td>'
                  f'<td>{_html.escape(str(row.get("bezeichnung", "")))}</td>'
                  f'<td>{_html.escape(str(row.get("anzahl", "")))}</td></tr>')
        s += '</table>'
    stock_legacy = _result_count(result.get('legacy_stock_rows'))
    if stock_legacy:
        s += (f'<p>\u2139\ufe0f {stock_legacy} Buchungen auf Konten 1000&ndash;1099 stammen aus einer '
              'älteren Version und sind nach deren Lesart gespeichert; ihre Form und ihr Betrag '
              'wurden nicht verändert, und sie wurden nicht erneut angelegt.</p>')
    legacy = _result_count(result.get('legacy_mirrors'))
    if legacy:
        s += (f'<p>\u2139\ufe0f {legacy} ältere Überträge sind noch nach der alten Lesart '
              'gespeichert; ihre Form und ihr Betrag wurden nicht verändert.</p>')
    return s


def PageMiscellaneous(db: Database, check_payments: bool = False):
    """Generate Sonstiges page (database overview, SQL console, misc tools).

    ``check_payments``: die Prüfliste der Zahlungszuordnungen mit anzeigen. Sie läuft nur auf
    Klick (``?check_payments=1``), nicht bei jedem Laden der Seite – bei einem großen Bestand
    braucht sie einen Moment."""
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
    import auth as _auth
    if _auth.sql_console_allowed():
        s += '\t<div class="rectRounded" style="grid-column: span 2">'
        s += '''
        <h2>SQL-Befehle ausführen</h2>
        Gib hier SQL-Befehle ein (mehrere Befehle durch Semikolon getrennt). Eine Anfrage läuft höchstens 60 Sekunden, sehr große Ausgaben werden gekürzt:<br>
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
                "DELETE FROM Bookings WHERE DateBooking BETWEEN '" + from + "' AND '" + to + "';\\n" +
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
    else:
        s += '\t<div class="rectRounded" style="grid-column: span 2">'
        s += ('<h2>SQL-Befehle ausführen</h2>'
              '<p>Die SQL-Konsole steht nur Administratoren zur Verfügung.</p>')
        s += '\t</div>'

    # Both Exports together in one DIV
    s += '\t<div class="grid1Col gridMiddle">'
    # ── DB Export as SQL ──────────────────────────────────────────────────────
    s += '\t\t<div class="rectRounded">'
    s += '''
        <h2>DB-Export</h2>
        <p>Exportiert alle Tabelleninhalte als INSERT-Statements in die Datei
        <code>db-export.sql</code> im Datenverzeichnis.'''
    if _auth.sql_console_allowed():
        s += '''
        Die Datei kann direkt im SQL-Konsolenbereich eingefügt werden.'''
    s += '''</p>
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
    # ── Zahlungszuordnungen prüfen (nur Anzeige) ──────────────────────────────
    s += '\t\t<div class="rectRounded">'
    s += '<h2>Zahlungszuordnungen</h2>'
    s += ('<p>Prüft, ob Zuordnungen, Buchungssätze und offener Rest der Rechnungen '
          'zusammenpassen. Die Prüfung ändert nichts.</p>'
          '<form method="GET" action="/miscellaneous">'
          '<input type="hidden" name="check_payments" value="1">'
          '<button type="submit" class="coloredButton bg-blue">Zahlungszuordnungen prüfen</button>'
          '</form>')
    if check_payments:
        findings = db.find_payment_inconsistencies()
        if not findings:
            s += '<p class="successColor">Keine Unstimmigkeiten gefunden.</p>'
        else:
            shown = findings[:50]
            s += ('<p>Bei diesen Rechnungen passen Zuordnung, Buchungssätze oder offener Rest '
                  'nicht zusammen. Die Liste ändert nichts &ndash; die Rechnung öffnen und die '
                  'Buchungen prüfen.</p><table><tr><th>Rechnung</th><th>Befund</th></tr>')
            for finding in shown:
                number = _html.escape(finding['invoice_number'] or '–')
                if finding['invoice_id'] is not None:
                    number = f'<a href="/invoice/edit?id={int(finding["invoice_id"])}">{number}</a>'
                s += f'<tr><td>{number}</td><td>{_html.escape(finding["text"])}</td></tr>'
            s += '</table>'
            if len(findings) > len(shown):
                s += f'<p>und {len(findings) - len(shown)} weitere</p>'
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
        muss ein <em>SKR-Gegenkonto</em> hinterlegt haben (z.B. 1810 für 2. Bankkonto, 1600 für Kasse;
        das Verrechnungskonto 1460 gilt als Geldtransit).<br>
        Duplikate (gleiche Referenznummer + Datum + Konto + Betrag) werden übersprungen &ndash;
        auch, wenn die Buchungen schon aus dem Datenbank-Import stammen.</p>
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
        s += _import_notices(_last_result)
        if _errors:
            s += f'<h3>\u274c Parse-Fehler &ndash; {len(_errors)} Einträge</h3><ul>'
            for _e in _errors:
                s += f'<li>{_html.escape(str(_e))}</li>'
            s += '</ul>'
    s += '\t</div>'

    # ── WISO-Datenbank (Firebird) ─────────────────────────────────────────────
    _fdb_path = _os.path.join(_userctx.user_data_dir(), 'wiso_fdb_result.json')
    _fdb = None
    if _os.path.exists(_fdb_path):
        try:
            with open(_fdb_path, encoding='utf-8') as _f:
                _fdb = _json.load(_f)
        except Exception:
            _fdb = None

    s += '\t<div class="rectRounded">'
    s += '''
        <h2>WISO Mein Büro &ndash; Import aus der Datenbank</h2>
        <p>Genauer als die Textexporte: die Datenbank nennt mit einer eigenen
        Kennung, <strong>welche Teilbuchungen zusammengehören</strong>, bringt das
        <strong>Anlagenverzeichnis samt AfA-Plan</strong> mit und umfasst alle Jahre.
        Gelesen wird nur &ndash; an der WISO-Datenbank ändert sich nichts.</p>
        <p>Angeben lässt sich das <strong>DB-Verzeichnis</strong> von WISO Mein Büro
        oder direkt die Mandantendatei <code>DB1.FDB</code>.
        <code>DB0.FDB</code> (Standard-Kontenrahmen) wird daneben automatisch
        gesucht. WISO sollte dabei geschlossen sein.<br>
        Voraussetzung wie beim CSV-Import: jedes Konto in der
        <strong>Kontenverwaltung</strong> braucht ein hinterlegtes
        <em>SKR-Gegenkonto</em>. Ein zweiter Lauf verdoppelt nichts. Buchungen, die schon
        über den CSV-Import kamen, ergänzt der Import, statt sie neu anzulegen;
        wiedererkannt wird ein Satz nur, wenn Beleg-Nr., Datum, Konto, Betrag und Text übereinstimmen (und das Gegenkonto nicht widerspricht); alles andere wird neu angelegt und, wo eine ähnliche Buchung schon da ist, als möglicherweise doppelt gemeldet.</p>
        <form method="POST" action="/wiso/fdb-import">
            <div class="rowWithObjects">Pfad:&nbsp;
                <input type="text" name="fdb_path" style="min-width:34em"
                       placeholder="C:\\\\ProgramData\\\\...\\\\DB  oder  ...\\\\DB\\\\DB1.FDB" required>
            </div>
            <div class="rowWithObjects">
                <label><input type="checkbox" name="with_assets" value="1" checked>
                &nbsp;Anlagenverzeichnis und AfA-Plan mitnehmen</label>
            </div>
            <div class="rowWithObjects">
                <label><input type="checkbox" name="with_invoices" value="1" checked>
                &nbsp;Kunden und Rechnungen mitnehmen (Kundennummern bleiben erhalten)</label>
            </div>
            <br>
            <button type="submit" class="coloredButton bg-green">&#x1F5C4;&#xFE0F; Aus WISO-Datenbank importieren</button>
        </form>
        <p class="hint">Der Import ist auf den Stand abgestimmt, den WISO Mein Büro
        derzeit schreibt (Firebird ODS&nbsp;12). Weicht Ihre Version davon ab, meldet
        die Prüfung vor dem Schreiben, welche Tabellen oder Spalten fehlen &ndash; es
        wird dann <strong>nichts</strong> importiert. In dem Fall lässt sich das
        Importmodul unter <code>importers/wiso_fdb/</code> mit KI-Unterstützung an
        Ihre Version anpassen; die Meldung nennt genau die betroffenen Stellen.</p>
        <script>
        (function() {
            const p = new URLSearchParams(window.location.search);
            const status = p.get('wiso_fdb');
            if (!status) return;
            const div = document.createElement('div');
            div.style = 'margin-top:10px; padding:8px 14px; border-radius:4px; display:inline-block;';
            let msg;
            if (status === 'ok') {
                const missing = parseInt(p.get('missing_coa') || '0');
                const hints   = parseInt(p.get('hints') || '0');
                msg = '\\u2705 Import abgeschlossen: ' + p.get('imported')
                    + ' Buchungss\\u00e4tze, ' + p.get('skipped') + ' \\u00fcbersprungen, '
                    + p.get('assets') + ' Anlagen mit ' + p.get('afa') + ' AfA-Zeilen';
                const lk = parseInt(p.get('linked') || '0');
                const cc = parseInt(p.get('created_coa') || '0');
                const rn = parseInt(p.get('invoices') || '0');
                const kd = parseInt(p.get('customers') || '0');
                const zl = parseInt(p.get('payments') || '0');
                const ad = parseInt(p.get('adopted') || '0');
                const pd = parseInt(p.get('possible') || '0');
                if (ad > 0) msg += ', ' + ad + ' aus dem CSV-Import wiedererkannt';
                if (pd > 0) msg += ', ' + pd + ' vorhandene Buchungen m\\u00f6glicherweise doppelt \\u2013 siehe Liste unten';
                if (rn > 0) msg += ', ' + rn + ' Rechnungen mit '
                    + p.get('items') + ' Positionen';
                if (kd > 0) msg += ', ' + kd + ' Kunden';
                if (zl > 0) msg += ', ' + zl + ' Zahlungen zugeordnet';
                if (lk > 0) msg += ', ' + lk + ' Bankbewegungen verkn\\u00fcpft';
                if (cc > 0) msg += ', ' + cc + ' SKR-Konten angelegt';
                if (missing > 0) msg += ', ' + missing + ' Konten ohne Zuordnung';
                div.style.background = (missing || hints) ? '#fff3cd' : '#d4edda';
                div.style.color      = (missing || hints) ? '#856404' : '#155724';
            } else if (status === 'incompatible') {
                div.style.background = '#fff3cd';
                div.style.color = '#856404';
                msg = '\\u26a0\\ufe0f Diese WISO-Datenbank passt nicht zum Importmodul: '
                    + decodeURIComponent(p.get('msg') || '')
                    + '. Es wurde nichts importiert.';
            } else {
                div.style.background = '#f8d7da';
                div.style.color = '#721c24';
                msg = '\\u274c Fehler: ' + decodeURIComponent(p.get('msg') || '');
            }
            div.append(msg);
            document.currentScript.parentNode.insertBefore(div, document.currentScript);
        })();
        </script>
        '''

    if _fdb:
        _blocker = _fdb.get('blocker') or []
        _hints = _fdb.get('hints') or []
        _missing = _fdb.get('missing_coa') or {}
        _created = _fdb.get('created_coa') or {}
        if _fdb.get('file'):
            s += (f'<p>\U0001F4C4 Zuletzt gelesen: '
                  f'<code>{_html.escape(str(_fdb["file"]))}</code></p>')
        if _fdb.get('adopted'):
            s += (f'<p>✅ {_html.escape(str(_fdb["adopted"]))} Buchungssätze waren '
                  'schon aus dem CSV-Import da: Sie haben jetzt die WISO-Kennung; '
                  'leere Konten, Steuer und Empfänger wurden ergänzt, sie wurden aber nicht '
                  'doppelt angelegt.</p>')
            _adopted_rows = _fdb_result_rows(_fdb.get('adopted_rows'))
            if _adopted_rows:
                s += ('<details><summary>Welche Buchungen das waren</summary>'
                      + _fdb_rows_table(_adopted_rows))
                if len(_adopted_rows) >= MAX_ADOPTED_ROWS:
                    s += f'<p>Genannt werden die ersten {MAX_ADOPTED_ROWS}.</p>'
                s += '</details>'
        _possible_rows = _fdb_result_rows(_fdb.get('possible_duplicates'))
        if _possible_rows:
            _possible_count = _fdb.get('possible_duplicates_count', len(_possible_rows))
            s += ('<p>Diese Buchungen gleichen einem neu angelegten Satz in Datum, Konto und '
                  'Betrag, aber nicht im Text bzw. Gegenkonto. Bitte prüfen und eine von beiden '
                  'löschen, falls es derselbe Vorgang ist.</p>'
                  f'<details><summary>Möglicherweise doppelt ({_html.escape(str(_possible_count))})'
                  '</summary>' + _fdb_rows_table(_possible_rows))
            if len(_possible_rows) >= MAX_ADOPTED_ROWS:
                s += f'<p>Genannt werden die ersten {MAX_ADOPTED_ROWS}.</p>'
            s += '</details>'
        s += _import_notices(_fdb)
        _conflict_rows = _fdb_result_rows(_fdb.get('form_conflicts'))
        if _conflict_rows:
            _conflict_count = _result_count(_fdb.get('form_conflicts_count')) or len(_conflict_rows)
            s += ('<p>Diese schon importierten Sätze sind anders gespeichert, als dieser Import sie '
                  'schreiben würde (z.&nbsp;B. weil ein Konto inzwischen ein Geldkonto ist oder nicht '
                  'mehr). Sie wurden <strong>nicht</strong> geändert &ndash; bitte prüfen.</p>'
                  f'<details><summary>Abweichende Form ({_html.escape(str(_conflict_count))})'
                  '</summary><table><tr><th>Datum</th><th>Betrag</th><th>Text</th>'
                  '<th>Buchungs-ID</th><th>Gespeichert</th><th>Neu</th></tr>')
            for _row in _conflict_rows:
                try:
                    _amount = fmt_eur(_row.get('amount'))
                except Exception:                       # Ergebnisdatei von Hand verändert
                    _amount = str(_row.get('amount'))
                s += (f'<tr><td>{_html.escape(str(_row.get("date") or ""))}</td>'
                      f'<td>{_html.escape(_amount)}</td>'
                      f'<td>{_html.escape(str(_row.get("text") or ""))}</td>'
                      f'<td>{_html.escape(str(_row.get("id") or ""))}</td>'
                      f'<td>{_html.escape(str(_row.get("stored") or ""))}</td>'
                      f'<td>{_html.escape(str(_row.get("fresh") or ""))}</td></tr>')
            s += '</table>'
            if len(_conflict_rows) >= MAX_ADOPTED_ROWS:
                s += f'<p>Genannt werden die ersten {MAX_ADOPTED_ROWS}.</p>'
            s += '</details>'
        if _blocker:
            s += ('<h3>\u26a0\ufe0f Diese Datenbank passt nicht zum Importmodul</h3>'
                  '<p>Es wurde <strong>nichts</strong> importiert. Die folgenden '
                  'Tabellen oder Spalten erwartet <code>importers/wiso_fdb/</code>, '
                  'Ihre WISO-Version liefert sie aber nicht. Mit dieser Liste lässt '
                  'sich das Modul per KI anpassen:</p><ul>')
            for _b in _blocker:
                s += f'<li>{_html.escape(str(_b))}</li>'
            s += '</ul>'
        if _hints:
            s += (f'<h3>\u2139\ufe0f Teilweise übersprungen &ndash; {len(_hints)} Hinweise</h3>'
                  '<p>Diese Teile fehlen in Ihrer WISO-Datenbank; der Rest wurde '
                  'importiert.</p><ul>')
            for _h in _hints:
                s += f'<li>{_html.escape(str(_h))}</li>'
            s += '</ul>'
        _invoice_warnings = _fdb.get('invoice_warnings') or []
        if _invoice_warnings:
            s += (f'<h3>⚠️ Auffälligkeiten bei Rechnungen '
                  f'&ndash; {len(_invoice_warnings)}</h3>'
                  '<p>Die Rechnungen sind übernommen; diese Punkte lohnen '
                  'einen Blick:</p><ul>')
            for _w in _invoice_warnings[:40]:
                s += f'<li>{_html.escape(str(_w))}</li>'
            if len(_invoice_warnings) > 40:
                s += f'<li>… und {len(_invoice_warnings) - 40} weitere</li>'
            s += '</ul>'
        _asset_warnings = _fdb.get('asset_warnings') or []
        if _asset_warnings:
            s += (f'<h3>\u26a0\ufe0f Auff\u00e4lligkeiten im Anlagenverzeichnis '
                  f'&ndash; {len(_asset_warnings)}</h3>'
                  '<p>Diese Anlageg\u00fcter sind in WISO in sich nicht stimmig. '
                  '\u00dcbernommen wurde jeweils der gerechnete Wert; nachsehen '
                  'lohnt sich trotzdem:</p><ul>')
            for _w in _asset_warnings:
                s += f'<li>{_html.escape(str(_w))}</li>'
            s += '</ul>'
        if _missing:
            s += (f'<h3>\u26a0\ufe0f Konten ohne SKR04-Zuordnung &ndash; {len(_missing)}</h3>'
                  '<p>WISO führt diese Konten intern in SKR03, nennt dafür aber keine '
                  'SKR04-Entsprechung. Die Buchungen sind importiert, aber auf dieser '
                  'Seite unkontiert. Bitte <a href="/masterdata/skr">im Kontenrahmen</a> '
                  'anlegen und den Buchungen zuweisen:</p>')
            s += '<table><tr><th>SKR03</th><th>Bezeichnung in WISO</th><th>Buchungen</th></tr>'
            for _nr, _info in _missing.items():
                if isinstance(_info, dict):
                    _anz, _bez = _info.get('anzahl', ''), _info.get('bezeichnung', '')
                else:
                    _anz, _bez = _info, ''
                s += (f'<tr><td>{_html.escape(str(_nr))}</td>'
                      f'<td>{_html.escape(str(_bez))}</td>'
                      f'<td>{_html.escape(str(_anz))}</td></tr>')
            s += '</table>'
        if _created:
            s += (f'<h3>\u2705 Neu angelegte SKR-Konten &ndash; {len(_created)}</h3>'
                  '<p>Nummer und Bezeichnung stammen aus WISOs eigener '
                  'Umschlüsselung.</p><table><tr><th>SKR04</th><th>Bezeichnung</th></tr>')
            for _nr, _name in _created.items():
                s += (f'<tr><td>{_html.escape(str(_nr))}</td>'
                      f'<td>{_html.escape(str(_name))}</td></tr>')
            s += '</table>'
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
        <p>Stellt ein Archiv aus <code>backup/</code> im Benutzerverzeichnis wieder her.
        Sicherungen mit „nur DB“ lassen sich nur ohne „vorher löschen“ zurückspielen.</p>
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
