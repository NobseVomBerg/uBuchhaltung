# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Receipts page (Belege: Upload, Anzeige und Bearbeitung)
"""
import datetime
import html as _html
import traceback
from urllib.parse import quote
import demo
import receipt_reading
from db import Database
from . import receipt_files
from . import receipt_links
from . import receipt_reader
from .pages import Header1, Header2, Header3, Footer, script_json
from .period import period_filter_widget
from .upload_handler import RECEIPTS_DEMO_REFUSED

NO_FILE_TEXT = 'keine Datei'
FILE_OUTSIDE_TEXT = 'Datei nicht im Belegordner'
DELETE_QUESTION = "'Beleg wirklich löschen?'"          # als Argument im onclick (Anführungszeichen gehören dazu)
UNREAD_CELL_TEXT = 'ungelesen'                         # Spalte „Gelesen“: eine Datei ohne Lesung
READING_HINT = 'Gelesene Werte sind eine Lesung der Datei – bitte prüfen.'
SUGGESTIONS_HINT = 'Vorschläge beruhen auf der Lesung der Datei: Betrag, Rechnungsnummer, Name und Datum. Bitte prüfen.'
NO_SUGGESTIONS_TEXT = 'Keine passende Buchung gefunden.'
NO_AMOUNT_TEXT = 'Ohne gelesenen Betrag gibt es keine Vorschläge.'
LINKED_TEXT = 'Der Beleg ist bereits einer Buchung zugeordnet.'
MISMATCH_CHECKS = (receipt_reader.SUM_CHECK_TEXTS['mismatch'], receipt_reader.RATE_CHECK_TEXTS['mismatch'])   # Proben, die nicht stimmen: Warnfarbe


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


def _read_cell(info, summary):
    """Die Zelle „Gelesen“ der Liste (mit der Klasse ``readCell``: eine Mindestbreite, damit der Text in einem schmalen Fenster nicht
    auf viele Zeilen umbricht): ``–`` ohne Datei, ``ungelesen`` ohne Lesung, sonst der Text zur Lesung (``receipt_reader.summary_text``)
    – maskiert. ``summary``: die Zeile des Belegs aus ``document_reading_summaries``."""
    if info is None:
        text = '–'
    else:
        text = _esc(UNREAD_CELL_TEXT if summary is None else receipt_reader.summary_text(summary))
    return f'<td class="readCell">{text}</td>'


def suggestions_empty_text(linked, reading):
    """Der Satz zu einer leeren Liste der Vorschläge – die eine Entscheidung für die Seite des Belegs und die Route
    ``/receipts/suggestions``: ``LINKED_TEXT``, wenn der Beleg schon an einer Buchung hängt (``linked``; dann gibt es keine Vorschläge),
    ``NO_AMOUNT_TEXT``, wenn die Lesung keinen Brutto nennt (oder es keine gibt: ``reading`` ist ``None``), sonst ``NO_SUGGESTIONS_TEXT``."""
    if linked:
        return LINKED_TEXT
    gross = reading.get('GrossMinor') if isinstance(reading, dict) else None
    return NO_SUGGESTIONS_TEXT if receipt_reader.amount_text(gross) is not None else NO_AMOUNT_TEXT


def _unread_box(count, ids):
    """Der Kasten über der Liste: wie viele Belege mit Datei noch nicht (oder nur mit älteren Regeln) gelesen sind, und der Knopf, der
    die ``ids`` (höchstens ``READ_BATCH``) nacheinander liest (``readReceipts``). Ohne ungelesene Belege ein versteckter, leerer Platz:
    Dort zeigt das Skript nach einem Upload seinen Fortschritt. Die IDs gehen über ``script_json`` ins ``onclick``."""
    if not ids:
        return '<div id="readBox" class="rectRounded" hidden></div>\n'
    one = count == 1
    text = (f'{count} {"Beleg" if one else "Belege"} mit Datei {"ist" if one else "sind"} '
            'noch nicht (oder nur mit älteren Regeln) gelesen.')
    if count > len(ids):
        text += f' Gelesen werden die nächsten {len(ids)}.'
    label = '1 ungelesenen Beleg lesen' if len(ids) == 1 else f'{len(ids)} ungelesene Belege lesen'
    return (f'<div id="readBox" class="rectRounded"><p>{_esc(text)}</p>'
            f'<button type="button" class="coloredButton btn-sm bg-blue" onclick="readReceipts({_esc(script_json(ids))})">'
            f'{_esc(label)}</button></div>\n')


# Mehrere Belege nacheinander lesen: der Knopf über der Liste und der Upload (``showUploadResult``) rufen ``readReceipts`` auf. Das
# Skript steht im Skriptblock des Uploads – Lesen und Hochladen gibt es nur außerhalb des Demo-Modus. Nur ``var`` und ``function``;
# was auf der Seite erscheint, geht nur über ``textContent``.
READ_JS = r'''
        // Belege lesen (2.6.0): nacheinander je Beleg EINE Anfrage an /receipts/read – der Server liest immer nur einen zurzeit. Den
        // Fortschritt zeigt der Kasten #readBox, nur als Text. Eine Anfrage, die scheitert, zählt und hält die Schleife nicht an.
        var READ_BUSY_WAIT = 2000;       // Millisekunden, bis derselbe Beleg erneut angefragt wird, wenn der Server noch einen anderen liest
        var READ_MAX_ATTEMPTS = 30;      // so oft wird derselbe Beleg höchstens angefragt, danach geht es mit dem nächsten weiter
        var readRunning = false;

        function readOne(id) {
            // Die Antwort als Objekt – oder null (kein Netz, kein JSON): Dieses Versprechen wird nie abgelehnt.
            return fetch('/receipts/read', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                            body: JSON.stringify({documentId: id})})
                .then(function (response) { return response.text(); })
                .then(function (text) {
                    var data = null;
                    try { data = JSON.parse(text); } catch (e) { data = null; }
                    return (data && typeof data === 'object') ? data : null;
                })
                .catch(function () { return null; });
        }

        function readReceipts(ids, done) {
            if (readRunning) return;
            readRunning = true;
            var box = document.getElementById('readBox');
            var counts = {read: 0, unreadable: 0, failed: 0};
            var position = 0;
            var attempts = 0;
            var cancelled = false;
            var progress = document.createElement('p');
            var cancel = document.createElement('button');
            cancel.type = 'button';
            cancel.className = 'coloredButton btn-sm bg-gray';
            cancel.textContent = 'Abbrechen';
            cancel.addEventListener('click', function () { cancelled = true; });
            box.hidden = false;
            box.textContent = '';
            box.appendChild(progress);
            box.appendChild(cancel);

            function finish() {
                var text = counts.read + ' gelesen, ' + counts.unreadable + ' nicht lesbar';
                if (counts.failed) text += ', ' + counts.failed + ' fehlgeschlagen';
                if (cancelled) text += ' (abgebrochen)';
                var summary = document.createElement('p');
                summary.textContent = text + '.';
                box.textContent = '';
                box.appendChild(summary);
                readRunning = false;
                if (typeof done === 'function') done(text);
                else setTimeout(function () { location.reload(); }, 2000);
            }

            function next() {
                if (cancelled || position >= ids.length) { finish(); return; }
                attempts += 1;
                progress.textContent = 'Lese Beleg ' + (position + 1) + ' von ' + ids.length + ' …';
                readOne(ids[position]).then(function (data) {
                    if (data && data.busy && attempts < READ_MAX_ATTEMPTS) {
                        setTimeout(next, READ_BUSY_WAIT);
                        return;
                    }
                    if (data && data.success === true) {
                        if (data.reading && data.reading.status === 'ok') counts.read += 1; else counts.unreadable += 1;
                    } else {
                        counts.failed += 1;
                    }
                    position += 1;
                    attempts = 0;
                    next();
                });
            }
            next();
        }
'''


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
    # Die Lesung eines Belegs (Spalte „Gelesen“ aus EINER Abfrage); der Kasten zum Lesen der ungelesenen gibt es wie den Upload nur
    # außerhalb des Demo-Modus
    summaries = db.document_reading_summaries()
    read_box = ''
    if upload_allowed:
        version = receipt_reading.READER_VERSION
        unread = db.count_unread_receipts(version)
        read_box = _unread_box(unread, db.unread_receipt_ids(version, receipt_reader.READ_BATCH) if unread else [])

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
    '''
    s += read_box
    s += '''
            <table>
                <tr><th>Nr.</th><th>Datum</th><th>Dateiname</th><th>Datei</th><th class="readCell">Gelesen</th><th>Info</th><th>Buchungen</th><th>Aktionen</th></tr>
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
        s += (f'<td>{n}</td><td>{dt}</td><td>{fn}</td><td>{_file_cell(info, id_url)}</td>'
              f'{_read_cell(info, summaries.get(row[0]))}<td>{inf}</td>')
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

        // Das Ergebnis steht je Datei als Text da (Beleg angelegt, darunter ein Hinweis – oder der Fehler). Danach werden die angelegten
        // Belege gelesen (readReceipts, Fortschritt über der Liste; der Upload selbst liest nie); die Meldungen bleiben stehen. Die
        // Liste lädt nur neu, wenn es keinen Fehler und keinen Hinweis gab – sonst verschwände die Meldung, bevor jemand sie liest;
        // dann holt ein Knopf die Liste.
        function showUploadResult(response, data) {
            if (!response.ok || data === null || data.error || data.success === false) {
                showUploadStatus('Fehler beim Hochladen: ' + (data && data.error ? data.error
                    : 'Der Server hat die Anfrage abgelehnt (Status ' + response.status + ').'), 'errorColor');
                return;
            }
            uploadStatus.textContent = '';
            let problem = false;
            const created = [];
            (data.files || []).forEach(entry => {
                if (entry.status === 'created') {
                    addUploadNote('✓ ' + entry.filename + ' – Beleg ' + (entry.number ? entry.number + ' ' : '') + 'angelegt');
                    if (typeof entry.id === 'number') created.push(entry.id);
                    if (entry.hint) {
                        problem = true;
                        addUploadNote(entry.hint, 'warnColor');
                    }
                } else {
                    problem = true;
                    addUploadNote('✗ ' + entry.filename + ': ' + entry.error, 'errorColor');
                }
            });
            const afterwards = () => {
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
            };
            if (created.length) readReceipts(created, afterwards);
            else afterwards();
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
'''
        s += READ_JS + '''    </script>
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


# Das Skript der Seite eines Belegs für die verknüpften Buchungen und die Vorschläge (nach ``receipt_links.LINK_WIDGET_JS`` und den
# Daten ``RECEIPT_LINK_ID``/``RECEIPT_LINKED``/``RECEIPT_SUGGEST``). ``RECEIPT_SUGGEST`` ist ``null``, wenn die Seite keinen Bereich
# „Vorschläge“ hat (kein Beleg mit Lesung ``ok``), sonst ``{list, empty}``: die Vorschläge in der Form der Route
# ``/receipts/suggestions`` und der Satz für „keine Vorschläge“ (die Route nennt ihn bei jeder Antwort als ``empty``, das Skript übernimmt
# ihn vor dem Zeichnen). Werte aus der Datenbank nur über textContent/createElement, nie als Markup.
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
function receiptBookingRow(b) {
    /* eine Buchung als Zeile mit „Zuordnen“ (oder „zugeordnet“) – für die Treffer der Suche und die Vorschläge */
    return linkRow([b.date, linkWho(b), b.amount + ' ' + b.currency, b.documentNumber, b.kindLabel,
        receiptLinkedIds().indexOf(b.id) >= 0 ? linkEl('span', 'zugeordnet', 'muted')
            : linkButton('Zuordnen', 'bg-green', function () { receiptAttach(b.id); })]);
}
function receiptBookingSearch() {
    linkSearch('/transactions/search', document.getElementById('receiptBookingQuery').value,
        document.getElementById('receiptBookingResults'), ['Datum', 'Empfänger / Text', 'Betrag', 'Beleg-Nr.', 'Art', ''],
        receiptBookingRow);
}
function receiptSuggestionsDraw(list) {
    /* je Vorschlag die Zeile eines Treffers, in der Zelle „Empfänger / Text“ darunter klein die Begründungen */
    var rows = list.map(function (item) {
        var row = receiptBookingRow(item.booking);
        row.children[1].appendChild(linkEl('span', item.reasons.join(' · '), 'readingQuote muted'));
        return row;
    });
    linkTable(document.getElementById('receiptSuggestionList'),
        ['Datum', 'Empfänger / Text', 'Betrag', 'Beleg-Nr.', 'Art', ''], rows, RECEIPT_SUGGEST.empty);
}
function receiptSuggestionsLoad() {
    /* die Liste neu holen, nachdem zugeordnet oder gelöst wurde (dann ändert sie sich); ohne Bereich gibt es nichts zu tun */
    if (!RECEIPT_SUGGEST) return;
    fetch('/receipts/suggestions?id=' + encodeURIComponent(String(RECEIPT_LINK_ID)))
        .then(linkAnswer)
        .then(function (data) {
            /* der Satz für die leere Liste hängt vom Stand ab (zugeordnet / kein Betrag / nichts gefunden): vor dem Zeichnen übernehmen */
            if (typeof data.empty === 'string') RECEIPT_SUGGEST.empty = data.empty;
            receiptSuggestionsDraw(data.suggestions || []);
        })
        .catch(linkFail);
}
function receiptAttach(bookingId) {
    linkPost('/bookings/link-document', bookingId, RECEIPT_LINK_ID).then(function (data) {
        if (linkStale(data)) return;
        receiptLinksRender(data.bookings);
        appMsg(linkDoneText(data), data.numberInUse ? 'info' : (data.created ? 'success' : 'info'));
        /* die Treffer nur neu holen, wenn schon gesucht wurde – wer aus einem Vorschlag zuordnet, hat nicht gesucht */
        if (document.getElementById('receiptBookingResults').children.length) receiptBookingSearch();
        receiptSuggestionsLoad();
    }).catch(linkFail);
}
function receiptDetach(bookingId) {
    appConfirm('Verknüpfung lösen? Die Beleg-Nr. der Buchung bleibt.', function () {
        linkPost('/bookings/unlink-document', bookingId, RECEIPT_LINK_ID).then(function (data) {
            if (linkStale(data)) return;
            receiptLinksRender(data.bookings);
            appMsg(linkUndoneText(data), data.removed ? 'success' : 'info');
            receiptSuggestionsLoad();
        }).catch(linkFail);
    });
}
receiptLinksRender(RECEIPT_LINKED);
if (RECEIPT_SUGGEST) receiptSuggestionsDraw(RECEIPT_SUGGEST.list);
'''

# Das Skript des Kastens „Gelesen“ auf der Seite eines Belegs: „Lesen“/„Neu lesen“ (POST /receipts/read, danach lädt die Seite neu)
# und „Gelesenes Datum übernehmen“ (setzt nur den Wert des Datumsfelds – gespeichert wird über das Formular der Seite). Nur ``var`` und
# ``function``; was auf der Seite erscheint, geht nur über ``textContent`` oder ``.value``.
RECEIPT_READ_JS = r'''function receiptRead(button) {
    button.disabled = true;
    fetch('/receipts/read', {method: 'POST', headers: {'Content-Type': 'application/json'},
                             body: JSON.stringify({documentId: Number(button.dataset.id)})})
        .then(function (response) {
            return response.text().then(function (text) {
                var data = null;
                try { data = JSON.parse(text); } catch (e) { data = null; }
                if (data && typeof data === 'object' && data.success === true) { location.reload(); return; }
                button.disabled = false;
                appMsg(data && data.error ? data.error : 'Der Server hat die Anfrage abgelehnt (Status ' + response.status + ').', 'error');
            });
        })
        .catch(function () {
            button.disabled = false;
            appMsg('Die Verbindung zum Server ist fehlgeschlagen.', 'error');
        });
}
function receiptTakeDate(button) {
    document.getElementById('receiptDate').value = button.dataset.date;
    document.getElementById('receiptDateHint').textContent = 'Datum eingetragen – noch nicht gespeichert (oben „Beleg aktualisieren“ wählen).';
}
'''


def _reading_box(receipt, info, payload, read_allowed):
    """Der Kasten „Gelesen“ auf der Seite eines Belegs, serverseitig gebaut – jeder Wert maskiert. ``payload``: die Lesung als
    ``receipt_reader.reading_payload`` (``None``: noch nicht gelesen). Er nennt Status, Herkunft und Zeitpunkt, in einer Tabelle die
    gelesenen Felder (das Zitat aus dem Beleg bzw. der BT-Bezeichner der E-Rechnung klein darunter), die Proben (Summe, Steuer zum Satz;
    eine, die nicht stimmt, in Warnfarbe) und die Hinweise, dazu den festen Satz ``READING_HINT``. Den Knopf „Lesen“/„Neu lesen“ gibt es nur mit Datei und außerhalb des Demo-Modus; weicht
    das gelesene Datum vom Belegdatum ab, bietet „Gelesenes Datum übernehmen“ es für das Feld „Datum“ an – es ändert nur das Feld."""
    h = '    <div class="rectRounded" id="receiptReading">\n        <h2>Gelesen</h2>\n'
    if payload is None:
        h += f'        <p>{"Noch nicht gelesen." if info else "Ohne Datei gibt es nichts zu lesen."}</p>\n'
    else:
        parts = [payload['statusLabel'], payload['sourceLabel'], payload['readAt']]
        if payload['outdated']:
            parts.append('mit älteren Regeln gelesen')
        h += f'        <p>{_esc(" · ".join(part for part in parts if part))}</p>\n'
        if payload['fields']:
            h += '        <table class="form-table">\n'
            for field in payload['fields']:
                origin = f'„{field["quote"]}“' if field.get('quote') else field.get('ref')
                small = f'<span class="readingQuote muted">{_esc(origin)}</span>' if origin else ''
                h += f'            <tr><td>{_esc(field["label"])}</td><td>{_esc(field["value"])}{small}</td></tr>\n'
            h += '        </table>\n'
        for check in payload['checks']:
            css = ' class="warnColor"' if check in MISMATCH_CHECKS else ''
            h += f'        <p{css}>{_esc(check)}</p>\n'
        for note in payload['notes']:
            h += f'        <p class="warnColor">{_esc(note)}</p>\n'
        h += f'        <p class="hint">{_esc(READING_HINT)}</p>\n'
    controls = ''
    if info and read_allowed:
        label = 'Lesen' if payload is None else 'Neu lesen'
        controls += (f'<button type="button" class="coloredButton btn-sm bg-blue" data-id="{_esc(receipt[0])}" '
                     f'onclick="receiptRead(this)">{label}</button> ')
    day = payload['docDate'] if payload else None
    if day and day != str(receipt[2] or ''):
        german = datetime.date.fromisoformat(day).strftime('%d.%m.%Y')
        controls += (f'<button type="button" class="coloredButton btn-sm bg-green" data-date="{_esc(day)}" '
                     f'onclick="receiptTakeDate(this)">Gelesenes Datum übernehmen ({german})</button> '
                     '<span id="receiptDateHint" class="warnColor"></span>')
    if controls:
        h += f'        <p>{controls}</p>\n'
    return h + '    </div>\n'


def _suggestions_area(db, receipt_id, reading, payload):
    """Der Bereich „Vorschläge“ auf der Seite eines Belegs und seine Daten – ``(Markup, Daten)``. ``reading``: die gespeicherte Lesung
    (``Database.get_document_reading``), ``payload``: ihre Kurzform (``receipt_reader.reading_payload``).

    Ohne Lesung mit Status ``ok`` gibt es den Bereich gar nicht: ``('', None)``. Sonst steht im Markup die Überschrift, der Platz für die
    Liste (``#receiptSuggestionList``, das Skript zeichnet sie) und der feste Satz ``SUGGESTIONS_HINT``; die Daten sind ``{'list': die
    Vorschläge in der Form der Route ``/receipts/suggestions`` (``receipt_links.suggestions_payload``), 'empty': der Satz für „keine
    Vorschläge“}`` – ``suggestions_empty_text``: ``LINKED_TEXT``, wenn der Beleg schon an einer Buchung hängt (dann gibt es keine
    Vorschläge), ``NO_AMOUNT_TEXT``, wenn die Lesung keinen Brutto nennt (der Kasten „Gelesen“ zeigt dann keinen), sonst
    ``NO_SUGGESTIONS_TEXT``. Es wird nur vorgeschlagen; verknüpft wird erst auf Klick (``receiptAttach``)."""
    if payload is None or payload['status'] != 'ok':
        return '', None
    linked = False
    try:
        linked = db.receipt_is_linked(receipt_id)
        found = receipt_links.suggestions_payload(db.suggest_bookings_for_receipt(receipt_id))
    except Exception:                               # die Seite des Belegs hängt nicht an den Vorschlägen
        traceback.print_exc()                       # der volle Fehler nur ins Server-Log
        found = []
    data = {'list': found, 'empty': suggestions_empty_text(linked, reading)}
    area = ('<div id="receiptSuggestions">\n'
            '            <h3>Vorschläge</h3>\n'
            '            <div id="receiptSuggestionList"></div>\n'
            f'            <p class="hint">{_esc(SUGGESTIONS_HINT)}</p>\n'
            '        </div>')
    return area, data


def PageReceiptEdit(db: Database, receipt_id):
    """Generate receipt edit page. ``receipt_id``: die ID des Belegs (die Belegnummer ist nicht eindeutig); eine unbekannte ID
    zeigt „Beleg nicht gefunden.“

    Unter dem Formular steht der Kasten „Gelesen“ (die Lesung der Datei, ``_reading_box``), danach die verknüpften Buchungen, bei einer
    gelesenen Datei die Vorschläge (``_suggestions_area``) und eine Suche nach Buchungen zum Zuordnen (``receipt_links``): Die Daten
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
                <tr><td>Datum:</td><td><input type="date" name="date" id="receiptDate" value="{_esc(receipt[2])}"></td></tr>
                <tr><td>Dateiname:</td><td><input type="text" name="filename" value="{_esc(receipt[3])}"></td></tr>
                <tr><td>Datei:</td><td>{_esc(_file_text(info))}</td></tr>
                {file_links}
                <tr><td>Info:</td><td><input type="text" name="info" value="{_esc(receipt[5])}"></td></tr>
                <tr><td></td><td><input type="submit" value="Beleg aktualisieren"></td></tr>
            </table>
        </form>
    '''

    # Die Lesung der Datei (nur serverseitig gerendert; „Lesen“ nur mit Datei und außerhalb des Demo-Modus)
    reading = db.get_document_reading(receipt[0])
    payload = receipt_reader.reading_payload(reading)
    s+= _reading_box(receipt, info, payload, not demo.is_active())
    s+= '<script>' + RECEIPT_READ_JS + '</script>'

    # Die Vorschläge nur zu einer Datei, die sich lesen ließ (Lesung ``ok``): sonst gibt es den Bereich gar nicht
    suggestions_area, suggestions = _suggestions_area(db, receipt[0], reading, payload)

    # Verknüpfte Buchungen, Vorschläge und die Suche nach weiteren (Daten als JSON im Skriptblock, nur über ``script_json``)
    s+= f'''
        <h2>Verknüpfte Buchungen</h2>
        <div id="receiptLinks"></div>
        {suggestions_area}
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
         + f'var RECEIPT_SUGGEST = {script_json(suggestions)};\n'
         + RECEIPT_LINK_JS + '</script>')

    s+= Footer()
    return s
