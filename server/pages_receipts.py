# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Receipts page (Belege: Upload, Anzeige und Bearbeitung)
"""
import datetime
import html as _html
from urllib.parse import quote
import demo
from db import Database
from . import receipt_files
from . import receipt_links
from .pages import Header1, Header2, Header3, Footer, script_json
from .period import period_filter_widget
from .upload_handler import RECEIPTS_DEMO_REFUSED

NO_FILE_TEXT = 'keine Datei'
FILE_OUTSIDE_TEXT = 'Datei nicht im Belegordner'
DELETE_QUESTION = "'Beleg wirklich löschen?'"          # als Argument im onclick (Anführungszeichen gehören dazu)


def _esc(value):
    """Wert aus Datenbank oder Anfrage für HTML-Text und -Attribute maskieren (NULL = leer)."""
    return _html.escape(str(value if value is not None else ''), quote=True)


def _file_text(info):
    """Der Text zur Datei eines Belegs (Formular und Seite zum Bearbeiten): ihr Name, ``keine Datei`` oder der Hinweis, dass ein
    gespeicherter Pfad nicht gilt."""
    if info is None:
        return NO_FILE_TEXT
    return FILE_OUTSIDE_TEXT if info is False else info.name


def _file_cell(info, id_url):
    """Die Zelle „Datei“ der Liste: ``–`` ohne Datei, der Hinweis bei einem Pfad, der nicht gilt, sonst „Ansehen“ (nur, wenn die Datei
    im Browser angezeigt werden darf: ``inline_type``) und „Herunterladen“. ``id_url`` ist die prozentkodierte ID des Belegs."""
    if info is None:
        return '–'
    if info is False:
        return f'<span class="muted">{FILE_OUTSIDE_TEXT}</span>'
    links = []
    if info.inline:
        links.append(f'<a href="/receipts/file?id={id_url}&amp;inline=1" target="_blank" rel="noopener">Ansehen</a>')
    links.append(f'<a href="javascript:void(0);" onclick="appDownload(\'/receipts/file?id={id_url}\')">Herunterladen</a>')
    return ' | '.join(links)


def _delete_question(number, info):
    """Das zweite Argument des ``appConfirmPost`` zum Löschen. Wird die Datei mitgelöscht (sie liegt in ``Belege/``), sagt die
    Rückfrage es und nennt die Nummer; die Nummer ist ein Wert aus der Datenbank: als JSON-Zeichenkette, im Attribut maskiert."""
    if not info or not info.removable:
        return DELETE_QUESTION
    label = f"Beleg {str(number or '').strip()}".strip()
    return _esc(script_json(f'{label} und seine Datei wirklich löschen?'))


def PageReceipts(db: Database, date_from=None, date_to=None, err=None):
    """Generate receipts page with upload functionality

    ``err``: Klartext, warum eine Aktion abgelehnt wurde (z. B. ein Beleg, auf den Buchungen verweisen, lässt sich
    nicht löschen); er kommt aus der Adresse und erscheint nur als Meldung am Seitenrand (``appMsg``)."""
    current_year = datetime.datetime.now().year

    rows = db.fetch_receipts()
    # Serverseitiger Zeitraum-Filter (Belegmenge klein → Python-Filter genügt)
    if date_from and date_to:
        rows = [r for r in rows if r[2] and date_from <= r[2] <= date_to]
    counts = db.receipt_link_counts()

    # Nächste Belegnummer vorschlagen, ohne den Zähler zu verbrauchen.
    # peek_next_number berücksichtigt das eingestellte NumberFormat – die
    # frühere Handrechnung hier hängte den Prefix immer vor die Nummer.
    next_receipt_number = db.peek_next_number('receipt_company', current_year)[0] or ''
    # Im Demo-Modus legt der Server keine Dateien eines Besuchers ab: statt der Ablage steht dort der Satz (und kein Skript)
    upload_allowed = not demo.is_active()

    s = Header1('receipts')
    # Suche nach Header2; Zeitraum (Von/Bis + Jahr + Monat) zentral in Header3.
    s += Header2(
        '<div class="rowWithObjects"><div>'
        '<label>🔍 Suche:</label> '
        '<input type="text" id="receiptSearch" oninput="filterReceipts()" '
        'placeholder="Nummer / Dateiname / Info" style="width: 200px;">'
        '</div></div>'
    )
    s += Header3(period_filter_widget(date_from, date_to, '/receipts'))
    
    # ── Zwei-Spalten-Layout: links scrollbare Belege, rechts Upload + Formular ──
    s += '''
    <div class="grid2Cols gridMain">

        <!-- LINKS: scrollbare Belegliste -->
        <div class="gridLeftCol">
            <table>
                <tr><th>Nr.</th><th>Datum</th><th>Dateiname</th><th>Datei</th><th>Info</th><th>Buchungen</th><th>Aktionen</th></tr>
        '''
    for row in rows:
        rid = _esc(row[0])
        n   = _esc(row[1])
        # Gelöscht wird über die ID des Belegs (die Nummer ist nicht eindeutig). In der Adresse innerhalb des onclick
        # steht sie prozentkodiert: Ein nur HTML-maskiertes Hochkomma löst der Browser dort wieder auf (gespeichertes Skript).
        id_url = quote(str(row[0]), safe='')
        dt  = _esc(row[2])
        fn  = _esc(row[3])
        inf = _esc(row[5])
        info = receipt_files.file_info(row[4])
        s += (f'<tr class="receipt-row" data-date="{dt}" data-id="{rid}" data-number="{n}" data-filename="{fn}" '
              f'data-file="{_esc(_file_text(info))}" data-info="{inf}">')
        s += f'<td>{n}</td><td>{dt}</td><td>{fn}</td><td>{_file_cell(info, id_url)}</td><td>{inf}</td>'
        s += (f'<td><a href="/receipts/edit?id={id_url}" title="Verknüpfte Buchungen ansehen und zuordnen">'
              f'{counts.get(row[0], 0)}</a></td>')
        s += (f'<td>'
              f'<a href="#" class="action-icon" onclick="editReceiptFromRow(this); return false;" title="Bearbeiten">&#9998;</a>'
              f' <a href="javascript:void(0);" class="action-icon delete-icon" title="L\u00f6schen"'
              f' onclick="appConfirmPost(\'/receipts/delete?id={id_url}\', {_delete_question(row[1], info)})">&#128465;</a>'
              f'</td></tr>')
    s += '''
            </table>
        </div><!-- Ende linke Spalte -->

        <!-- RECHTS: Drag&Drop oben, Formular unten -->
        <div class="gridRightCol">

            <!-- Drag & Drop -->
            <div class="rectRounded">
                <h2>Belege hochladen</h2>
    '''
    if upload_allowed:
        s += '''
                <div id="dropZone">
                    <p>Dateien hier ablegen (Drag &amp; Drop)</p>
                    <input type="file" id="fileInput" multiple>
                    <button onclick="document.getElementById('fileInput').click()">Oder Dateien ausw&auml;hlen</button>
                </div>
                <div id="uploadStatus"></div>
        '''
    else:
        s += f'<p>{_esc(RECEIPTS_DEMO_REFUSED)}</p>'
    s += '''
            </div>

            <!-- Neuer Beleg / Beleg bearbeiten -->
            <div class="rectRounded">
                <h2 id="receiptFormTitle">Neuer Beleg</h2>
                <form method="POST" action="/add_receipt" id="receiptForm">
                    <input type="hidden" name="id" id="editId" value="">
    '''
    # Der Vorschlag folgt der Formatvorlage des Nummernkreises (frei eintippbar): maskieren. Das versteckte Feld hält ihn fest:
    # Ist die Nummer beim Absenden noch der Vorschlag, vergibt der Server frisch (ein zweites Fenster kann ihn verbraucht haben).
    s += f'<input type="hidden" name="number_suggestion" value="{_esc(next_receipt_number)}">'
    s += '''
                    <table class="form-table">
    '''
    s += f'<tr><td>Nummer:</td><td><input type="text" name="number" id="editNumber" value="{_esc(next_receipt_number)}"></td></tr>'
    s += '''
                        <tr><td>Datum:</td><td><input type="date" name="date" id="editDate"></td></tr>
                        <tr><td>Dateiname:</td><td><input type="text" name="filename" id="editFilename"
                            placeholder="JJJJMMTT Name (was gekauft wurde)"
                            title="Namenskonvention f&uuml;r eingehende Belege: JJJJMMTT Kunden-/Lieferantenname (kurze Liste der K&auml;ufe)"></td></tr>
                        <tr id="editFileRow" hidden><td>Datei:</td><td id="editFileText"></td></tr>
                        <tr id="editLinksRow" hidden><td>Buchungen:</td><td><a id="editLinks" href="/receipts">Verknüpfungen ansehen und zuordnen</a></td></tr>
                        <tr><td>Info:</td><td><input type="text" name="info" id="editInfo"></td></tr>
                        <tr><td></td><td>
                            <div id="btnNew">
                                <input type="submit" value="Beleg hinzuf&uuml;gen" class="coloredButton btn-sm bg-green">
                            </div>
                            <div id="btnEdit" style="display:none">
                                <input type="submit" value="Aktualisieren" class="coloredButton btn-sm bg-green">
                                <button type="button" class="coloredButton btn-sm bg-gray" onclick="resetReceiptForm()">Abbrechen</button>
                            </div>
                        </td></tr>
                    </table>
                </form>
            </div>

        </div><!-- Ende rechte Spalte -->
    </div><!-- Ende Zwei-Spalten-Layout -->
    '''
    if upload_allowed:
        s += '''<script>
        // Prevent default browser behavior for drag and drop on entire page
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            document.body.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
            }, false);
        });
        
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const uploadStatus = document.getElementById('uploadStatus');
        
        // Drag & Drop Events
        dropZone.addEventListener('dragenter', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.add('hover');
            console.log('dragenter');
        });
        
        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.stopPropagation();
            console.log('dragover');
        });
        
        dropZone.addEventListener('dragleave', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('hover');
            console.log('dragleave');
        });
        
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            e.stopPropagation();
            dropZone.classList.remove('hover');
            console.log('drop', e.dataTransfer.files);
            const files = e.dataTransfer.files;
            uploadFiles(files);
        });
        
        // File Input Change
        fileInput.addEventListener('change', (e) => {
            console.log('file input change', e.target.files);
            uploadFiles(e.target.files);
        });
        
        // Was der Server antwortet (Dateinamen, Fehlertexte), sind Daten und kein HTML:
        // nur als Text einsetzen, nie als Markup.
        function addUploadNote(text, cssClass) {
            const note = document.createElement('p');
            if (cssClass) note.className = cssClass;
            note.textContent = text;
            uploadStatus.appendChild(note);
        }

        function showUploadStatus(text, cssClass) {
            uploadStatus.textContent = '';
            addUploadNote(text, cssClass);
        }

        // Das Ergebnis steht je Datei als Text da (Beleg angelegt, darunter ein Hinweis – oder der Fehler). Die Liste lädt nur neu,
        // wenn es keinen Fehler und keinen Hinweis gab – sonst verschwände die Meldung, bevor jemand sie liest; dann holt ein Knopf
        // die Liste.
        function showUploadResult(response, data) {
            if (!response.ok || data === null || data.error || data.success === false) {
                showUploadStatus('Fehler beim Hochladen: ' + (data && data.error ? data.error
                    : 'Der Server hat die Anfrage abgelehnt (Status ' + response.status + ').'), 'errorColor');
                return;
            }
            uploadStatus.textContent = '';
            let problem = false;
            (data.files || []).forEach(entry => {
                if (entry.status === 'created') {
                    addUploadNote('✓ ' + entry.filename + ' – Beleg ' + (entry.number ? entry.number + ' ' : '') + 'angelegt');
                    if (entry.hint) {
                        problem = true;
                        addUploadNote(entry.hint, 'warnColor');
                    }
                } else {
                    problem = true;
                    addUploadNote('✗ ' + entry.filename + ': ' + entry.error, 'errorColor');
                }
            });
            if (problem) {
                const refresh = document.createElement('button');
                refresh.type = 'button';
                refresh.className = 'coloredButton btn-sm bg-gray';
                refresh.textContent = 'Liste aktualisieren';
                refresh.addEventListener('click', () => location.reload());
                uploadStatus.appendChild(refresh);
            } else {
                setTimeout(() => location.reload(), 3000);
            }
        }

        function uploadFiles(files) {
            console.log('uploadFiles called with', files.length, 'files');
            if (files.length === 0) return;
            
            showUploadStatus('Uploading...');
            
            const formData = new FormData();
            for (let i = 0; i < files.length; i++) {
                formData.append('files', files[i]);
            }
            
            fetch('/receipts/upload', {
                method: 'POST',
                body: formData
            })
            .then(response => response.text().then(text => {
                let data = null;
                try {
                    const parsed = JSON.parse(text);
                    if (parsed && typeof parsed === 'object') data = parsed;
                } catch (e) {
                    data = null;
                }
                showUploadResult(response, data);
            }))
            .catch(error => {
                showUploadStatus('Fehler beim Hochladen: ' + error, 'errorColor');
            });
        }
    </script>
    '''
    
    s += '''
    <script>
        function editReceiptFromRow(el) {
            const row = el.closest('tr');
            document.getElementById('editId').value       = row.dataset.id;
            document.getElementById('editNumber').value   = row.dataset.number;
            document.getElementById('editDate').value     = row.dataset.date;
            document.getElementById('editFilename').value = row.dataset.filename;
            document.getElementById('editInfo').value     = row.dataset.info;
            document.getElementById('editFileText').textContent = row.dataset.file;
            document.getElementById('editFileRow').hidden = false;
            document.getElementById('editLinks').href = '/receipts/edit?id=' + encodeURIComponent(row.dataset.id);
            document.getElementById('editLinksRow').hidden = false;
            document.getElementById('receiptForm').action           = '/update_receipt';
            document.getElementById('receiptFormTitle').textContent = 'Beleg bearbeiten';
            document.getElementById('btnNew').style.display  = 'none';
            document.getElementById('btnEdit').style.display = '';
            document.getElementById('receiptFormTitle').scrollIntoView({ behavior: 'smooth' });
        }

        function resetReceiptForm() {
            location.reload();
        }

        function filterReceipts() {
            const search = document.getElementById('receiptSearch').value.toLowerCase();

            document.querySelectorAll('.receipt-row').forEach(row => {
                const rowText = (
                    (row.getAttribute('data-number') || '') + ' ' +
                    (row.getAttribute('data-filename') || '') + ' ' +
                    (row.getAttribute('data-info') || '')
                ).toLowerCase();

                let show = true;
                if (search && !rowText.includes(search)) show = false;

                row.style.display = show ? '' : 'none';
            });
        }
    </script>
    '''
    if err:
        s += ('<script>document.addEventListener("DOMContentLoaded",function(){'
              'appMsg(' + script_json(err) + ',"error");});</script>')
    s+= Footer()
    return s


# Das Skript der Seite eines Belegs für die verknüpften Buchungen (nach ``receipt_links.LINK_WIDGET_JS`` und den Daten
# ``RECEIPT_LINK_ID``/``RECEIPT_LINKED``). Werte aus der Datenbank nur über textContent/createElement, nie als Markup.
RECEIPT_LINK_JS = r'''function receiptLinkedIds() { return RECEIPT_LINKED.map(function (b) { return b.id; }); }
function receiptLinksRender(bookings) {
    RECEIPT_LINKED = bookings;
    linkTable(document.getElementById('receiptLinks'),
        ['Datum', 'Empfänger / Text', 'Betrag', 'Beleg-Nr.', 'Art', 'Verknüpfung', ''],
        bookings.map(function (b) {
            return linkRow([b.date, linkWho(b), b.amount + ' ' + b.currency, b.documentNumber, b.kindLabel, b.relationLabel,
                linkActions([linkAnchor('Öffnen', '/transactions/edit?id=' + encodeURIComponent(String(b.id)), false),
                             linkButton('Lösen', 'bg-gray', function () { receiptDetach(b.id); })])]);
        }), 'Keine verknüpften Buchungen.');
}
function receiptBookingSearch() {
    linkSearch('/transactions/search', document.getElementById('receiptBookingQuery').value,
        document.getElementById('receiptBookingResults'), ['Datum', 'Empfänger / Text', 'Betrag', 'Beleg-Nr.', 'Art', ''],
        function (b) {
            return linkRow([b.date, linkWho(b), b.amount + ' ' + b.currency, b.documentNumber, b.kindLabel,
                receiptLinkedIds().indexOf(b.id) >= 0 ? linkEl('span', 'zugeordnet', 'muted')
                    : linkButton('Zuordnen', 'bg-green', function () { receiptAttach(b.id); })]);
        });
}
function receiptAttach(bookingId) {
    linkPost('/bookings/link-document', bookingId, RECEIPT_LINK_ID).then(function (data) {
        if (linkStale(data)) return;
        receiptLinksRender(data.bookings);
        appMsg(linkDoneText(data), data.numberInUse ? 'info' : (data.created ? 'success' : 'info'));
        receiptBookingSearch();
    }).catch(linkFail);
}
function receiptDetach(bookingId) {
    appConfirm('Verknüpfung lösen? Die Beleg-Nr. der Buchung bleibt.', function () {
        linkPost('/bookings/unlink-document', bookingId, RECEIPT_LINK_ID).then(function (data) {
            if (linkStale(data)) return;
            receiptLinksRender(data.bookings);
            appMsg(linkUndoneText(data), data.removed ? 'success' : 'info');
        }).catch(linkFail);
    });
}
receiptLinksRender(RECEIPT_LINKED);
'''


def PageReceiptEdit(db: Database, receipt_id):
    """Generate receipt edit page. ``receipt_id``: die ID des Belegs (die Belegnummer ist nicht eindeutig); eine unbekannte ID
    zeigt „Beleg nicht gefunden.“

    Unter dem Formular stehen die verknüpften Buchungen und eine Suche nach Buchungen zum Zuordnen (``receipt_links``): Die Daten
    kommen als JSON in den Skriptblock, das Skript baut die Tabellen nur über ``textContent``/``createElement``."""
    receipt = db.get_receipt_by_id(receipt_id)
    if not receipt:
        return "Beleg nicht gefunden."

    # Documents: ID(0), Number(1), Date(2), Filename(3), Path(4), Info(5)
    s = Header1('receipts')
    s+= Header2()
    s+= Header3()
    s+= "<h1>Beleg bearbeiten</h1>"
    info = receipt_files.file_info(receipt[4])
    id_url = quote(str(receipt[0]), safe='')
    # Ansehen und Herunterladen nur bei einer gültigen Datei (wie in der Liste); der Pfad steht nie auf der Seite
    file_links = f'<tr><td></td><td>{_file_cell(info, id_url)}</td></tr>' if info else ''
    # Jeder Wert stammt aus der Datenbank: in HTML maskiert, in den Adressen des onclick
    # prozentkodiert (ein nur HTML-maskiertes Hochkomma löst der Browser dort wieder auf).
    s+= f'''
        <form method="POST" action="/update_receipt">
            <input type="hidden" name="id" value="{_esc(receipt[0])}">
            <table class="form-table">
                <tr><td>Nummer:</td><td><input type="text" name="number" value="{_esc(receipt[1])}"></td></tr>
                <tr><td>Datum:</td><td><input type="date" name="date" value="{_esc(receipt[2])}"></td></tr>
                <tr><td>Dateiname:</td><td><input type="text" name="filename" value="{_esc(receipt[3])}"></td></tr>
                <tr><td>Datei:</td><td>{_esc(_file_text(info))}</td></tr>
                {file_links}
                <tr><td>Info:</td><td><input type="text" name="info" value="{_esc(receipt[5])}"></td></tr>
                <tr><td></td><td><input type="submit" value="Beleg aktualisieren"></td></tr>
            </table>
        </form>
    '''

    # Verknüpfte Buchungen und die Suche nach weiteren (Daten als JSON im Skriptblock, nur über ``script_json``)
    s+= f'''
        <h2>Verknüpfte Buchungen</h2>
        <div id="receiptLinks"></div>
        <h3>Buchung suchen</h3>
        <div class="rowWithObjects">
            <input type="search" id="receiptBookingQuery" maxlength="{receipt_links.SEARCH_MAX_LENGTH}" placeholder="Empfänger, Text, Beleg-Nr., Betrag oder Datum"
                   onkeydown="if (event.key === 'Enter') {{ event.preventDefault(); receiptBookingSearch(); }}">
            <button type="button" class="coloredButton btn-sm bg-blue" onclick="receiptBookingSearch()">Buchung suchen</button>
        </div>
        <div id="receiptBookingResults"></div>
    '''
    s+= ('<script>' + receipt_links.LINK_WIDGET_JS
         + f'var RECEIPT_LINK_ID = {int(receipt[0])};\n'
         + f'var RECEIPT_LINKED = {script_json(receipt_links.linked_bookings(db, receipt[0]))};\n'
         + RECEIPT_LINK_JS + '</script>')

    s+= Footer()
    return s
