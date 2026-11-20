# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Beleg-Upload: lesbare Rückmeldung und feste Fehlersätze, dazu zwei kleine Reste (2.5.7).

Vorher gab der Upload bei einem Fehler den Text der Ausnahme weiter (``str(e)``) – bei einer ``OSError`` des Betriebssystems mit
dem Serverpfad –, und die Belege-Seite zeigte die rohe JSON-Antwort in Erfolgsfarbe und lud nach drei Sekunden neu, auch nach
einem Fehler: Eine Warnung war nie zu lesen, ein Fehler sah aus wie ein Erfolg.

* ``server/upload_handler.py``: Je Datei steht in der Antwort nur der Klartext einer eigenen Klasse (``DocumentExists``: Datei
  gleichen Namens mit anderem Inhalt; ``CamtError``, ``DkbCsvError``), sonst der feste Satz ``UPLOAD_FAILED`` – der volle Fehler
  steht im Server-Log. ``DocumentExists`` ist eine ``FileExistsError``, aber nur sie: Eine nackte ``FileExistsError`` des
  Betriebssystems nennt einen Pfad. Ein statischer Wächter (``tests/test_form_error_paths.py``) hält fest, dass kein breites
  ``except … as e`` seine Ausnahme benutzt.
* ``server/pages_receipts.py``: Die Seite liest die Antwort als Text, zeigt je Datei eine Zeile (nur über ``textContent``), einen
  Fehler in Fehlerfarbe und lädt nur ohne Warnung, Fehler und erkannten Kontoauszug neu – sonst bleibt die Meldung stehen, mit dem
  Knopf „Liste aktualisieren“. Das Skript läuft in Node (``vm``, ohne Browser); ohne Node entfallen nur diese Läufe.
* ``openEditForm`` der Listenseiten: Ein Fehlerstatus oder eine Antwort ohne Formular (Anmeldeseite nach abgelaufener Sitzung)
  öffnet die Adresse, statt still nichts zu tun.
* ``server/backup.py``: ``_next_archive_path`` vergleicht Archivnamen ohne Groß-/Kleinschreibung.

Seit 2.5.11 postet die Belege-Seite an ``/receipts/upload`` (jede Datei wird ein Beleg, Antwort ``{"success", "files": [{"filename",
"status": "created"|"error", "id", "number", "date", "hint", "error"}]}``); die Tests zum Skript der Seite lesen diese Antwort. Die
ersten vier Tests prüfen weiter ``/upload_receipts`` – den Import der Seite „Buchungen“ (``handle_file_upload``), der unverändert bleibt.

Alle Namen und Inhalte sind erfundene Testwerte.
"""
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime

import pytest

from document_parser import DocumentExists, DocumentParser
from server import backup as backup_mod
from server.upload_handler import RECEIPT_UPLOAD_FAILED, RECEIPTS_DEMO_REFUSED, UPLOAD_FAILED
from tests.dom_helpers import Dom
from tests.http_client import get, own_origin, send
from tests.test_statement_import_guards import CLOSING_LINE, OPENING_LINE, make_pdf, one_page

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die Textprüfungen derselben Datei decken das '
                                                  'Wichtigste ab')

SECRET_PATH = '/srv/geheim/beleg.pdf'
PDF_BYTES = b'%PDF-1.4\n% ein Beleg\n'
KNOWN_SENTENCE = ("Datei 'leer.pdf' existiert bereits mit unterschiedlichem Inhalt. "
                  "Bitte umbenennen oder vorhandene Datei prüfen.")


# ── Hilfen ───────────────────────────────────────────────────────────────────
def multipart(files):
    boundary = 'GRENZE' + uuid.uuid4().hex
    body = b''
    for name, content in files:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                 'Content-Type: application/pdf\r\n\r\n').encode() + content + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    return f'multipart/form-data; boundary={boundary}', body


def upload(port, login, *files):
    """Dateien über die Route des Servers hochladen, wie es die Belege-Seite tut; liefert die Antwort."""
    content_type, body = multipart(files)
    return send(port, 'POST', '/upload_receipts', {'Origin': own_origin(port), 'X-CSRF-Token': login.csrf,
                                                    'Content-Type': content_type}, body)


def other_files(response):
    assert response.status == 200, response.text
    return json.loads(response.text)['other_files']


def failing_with(monkeypatch, raise_it):
    """Das Ablegen scheitert: ``DocumentParser.process_and_organize`` wirft, was ``raise_it()`` liefert."""
    def process_and_organize(self, filepath):
        raise raise_it()
    monkeypatch.setattr(DocumentParser, 'process_and_organize', process_and_organize)


# ── Der Server: kein Ausnahmetext in der Antwort ─────────────────────────────
@pytest.mark.parametrize('kind, args, in_log', [
    pytest.param(OSError, (13, 'Permission denied', SECRET_PATH), 'Permission denied', id='Zugriff-verweigert'),
    pytest.param(FileExistsError, (17, 'File exists', SECRET_PATH), 'File exists', id='FileExistsError-des-Betriebssystems'),
    pytest.param(RuntimeError, ('interner Fehler bei ' + SECRET_PATH,), 'interner Fehler', id='anderer-Fehler'),
])
def test_an_unexpected_error_is_a_fixed_sentence_and_its_text_stays_in_the_server_log(server_port, auto_login, monkeypatch,
                                                                                     capsys, kind, args, in_log):
    failing_with(monkeypatch, lambda: kind(*args))
    response = upload(server_port, auto_login, ('beleg.pdf', PDF_BYTES))
    assert response.status == 200
    data = json.loads(response.text)
    assert data['other_files'] == [{'filename': 'beleg.pdf', 'status': 'error', 'error': UPLOAD_FAILED}]
    assert data['files'] == [] and data['import_id'] is None
    for text in (SECRET_PATH, 'geheim', in_log, 'Errno'):
        assert text not in response.text, text                                   # nirgends in der Antwort
    log = capsys.readouterr().err
    assert SECRET_PATH in log and in_log in log and 'Traceback' in log           # aber im Server-Log


def test_the_clear_refusal_of_the_parser_is_passed_on_as_a_warning(server_port, auto_login, monkeypatch, capsys):
    failing_with(monkeypatch, lambda: DocumentExists(KNOWN_SENTENCE))
    assert other_files(upload(server_port, auto_login, ('leer.pdf', PDF_BYTES))) == [
        {'filename': 'leer.pdf', 'status': 'warning', 'error': KNOWN_SENTENCE}]
    assert 'Traceback' not in capsys.readouterr().err                            # eine bekannte Ablehnung ist kein Serverfehler


def test_the_refusal_is_a_file_exists_error_but_not_every_file_exists_error_is_the_refusal():
    assert issubclass(DocumentExists, FileExistsError)
    assert not isinstance(FileExistsError(17, 'File exists', SECRET_PATH), DocumentExists)


def test_the_same_name_with_other_content_is_a_warning_with_the_known_sentence(server_port, auto_login, capsys):
    """Der echte Weg über den Parser: ein Auszug ohne Umsätze wird unter ``Belege/<Jahr>/Konten/…`` abgelegt; ein zweiter
    Auszug gleichen Namens, Jahres und Kontos, aber mit anderem Inhalt, trifft die vorhandene Datei."""
    first = make_pdf(one_page(OPENING_LINE, CLOSING_LINE))
    other = make_pdf(one_page(OPENING_LINE, CLOSING_LINE, number='2/2025'))
    assert first != other
    filed = other_files(upload(server_port, auto_login, ('leer.pdf', first)))
    assert [entry['status'] for entry in filed] == ['warning']
    assert filed[0]['error'] == 'Kontoauszug erkannt, aber keine Umsätze gelesen.'     # abgelegt, wie bisher
    assert other_files(upload(server_port, auto_login, ('leer.pdf', first))) == filed   # dieselbe Datei erneut: kein Fehler
    assert other_files(upload(server_port, auto_login, ('leer.pdf', other))) == [
        {'filename': 'leer.pdf', 'status': 'warning', 'error': KNOWN_SENTENCE}]
    assert 'Traceback' not in capsys.readouterr().err


# ── Die Belege-Seite ─────────────────────────────────────────────────────────
def receipts_script(port):
    page = get(port, '/receipts')
    assert page.status == 200
    (script,) = [text for text in Dom(page.text).scripts if 'uploadFiles' in text]
    return script


def test_the_receipts_page_reads_the_status_and_shows_one_text_line_per_file(server_port, auto_login):
    script = receipts_script(server_port)
    assert 'response.ok' in script                                               # ein Fehlerstatus ist ein Fehler
    assert "showUploadStatus(data, 'successColor')" not in script                # nicht mehr die rohe Antwort als Erfolg
    for forbidden in ('innerHTML', 'outerHTML', 'document.write'):
        assert forbidden not in script, forbidden                                # nur Text, nie Markup
    assert 'Liste aktualisieren' in script
    assert "fetch('/receipts/upload'" in script and '/upload_receipts' not in script   # seit 2.5.11: jede Datei wird ein Beleg
    assert 'importiert wird auf der Seite' not in script                         # der Hinweis auf den Import kommt vom Server
    assert re.search(r'if \(problem\) \{.*?\} else \{\s*setTimeout\(\(\) => location\.reload\(\), 3000\);', script, re.S)


HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, scenarioFile] = process.argv;
const scenario = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));

function element(tag) {
  const node = {tagName: tag, className: '', type: '', children: [], listeners: {},
                classList: {add() {}, remove() {}},
                addEventListener(name, handler) { this.listeners[name] = handler; },
                appendChild(child) { this.children.push(child); return child; }};
  let text = '';
  Object.defineProperty(node, 'textContent', {
    get() { return text; },
    set(value) { text = value; this.children = []; },
  });
  return node;
}

const byId = {};
const timers = [];
let reloads = 0;
const sandbox = {
  document: {body: element('body'),
             getElementById: id => byId[id] || (byId[id] = element('div')),
             createElement: tag => element(tag)},
  location: {reload() { reloads += 1; }},
  setTimeout: (fn, delay) => { timers.push({fn, delay}); return timers.length; },
  FormData: class { append() {} },
  fetch: () => scenario.network_error ? Promise.reject(new Error('offline')) : Promise.resolve({
    ok: scenario.ok, status: scenario.status, text: () => Promise.resolve(scenario.body)}),
  console: {log() {}},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);
(async () => {
  vm.runInContext('uploadFiles([{name: "a.pdf"}])', sandbox);
  await new Promise(resolve => setTimeout(resolve, 20));
  const shown = byId.uploadStatus.children;
  const out = {
    notes: shown.map(child => ({tag: child.tagName, text: child.textContent, className: child.className, type: child.type})),
    delays: timers.map(timer => timer.delay),
  };
  timers.forEach(timer => timer.fn());
  out.reloadsByTimer = reloads;
  reloads = 0;
  const button = shown.find(child => child.tagName === 'button');
  if (button) button.listeners.click();
  out.reloadsByButton = reloads;
  console.log(JSON.stringify(out));
})();
"""

STATEMENT_HINT = ('Kontoauszug erkannt und als Beleg abgelegt – Umsätze werden auf der Seite „Buchungen“ importiert.')
NO_NUMBER_HINT = 'Kein brauchbarer Nummernkreis „Belegnummern Firma“ – der Beleg hat keine Nummer.'
CREATED = {'filename': '<b>beleg-1</b>.pdf', 'status': 'created', 'id': 7, 'number': '26B001', 'date': '2026-03-04'}
CREATED_2 = {'filename': 'liste.xlsx', 'status': 'created', 'id': 8, 'number': '26B002', 'date': '2026-03-04'}
WITHOUT_NUMBER = {'filename': 'ohne.pdf', 'status': 'created', 'id': 9, 'number': '', 'date': '2026-03-04',
                  'hint': NO_NUMBER_HINT}
STATEMENT = {'filename': 'auszug.pdf', 'status': 'created', 'id': 10, 'number': '26B003', 'date': '2026-01-03',
             'hint': STATEMENT_HINT}
BROKEN = {'filename': 'kaputt.pdf', 'status': 'error', 'error': RECEIPT_UPLOAD_FAILED}
EMPTY = {'filename': 'leer.pdf', 'status': 'error', 'error': 'Die Datei ist leer.'}
REFUSED = 'Fehler beim Hochladen: Der Server hat die Anfrage abgelehnt (Status {}).'
REFRESH = {'tag': 'button', 'text': 'Liste aktualisieren', 'className': 'coloredButton btn-sm bg-gray', 'type': 'button'}


def answer(*files):
    return json.dumps({'success': True, 'files': list(files)})


# (ok, Status, Antwort, erwartete Zeilen (Text, Klasse), danach: 'reload' = lädt nach 3 s neu, 'button' = Knopf, None = nichts)
UPLOAD_CASES = [
    pytest.param(True, 200, answer(CREATED, CREATED_2),
                 [('✓ <b>beleg-1</b>.pdf – Beleg 26B001 angelegt', ''), ('✓ liste.xlsx – Beleg 26B002 angelegt', '')],
                 'reload', id='glatt'),
    pytest.param(True, 200, answer(), [], 'reload', id='nichts-zu-melden'),
    pytest.param(True, 200, answer(CREATED, STATEMENT),
                 [('✓ <b>beleg-1</b>.pdf – Beleg 26B001 angelegt', ''), ('✓ auszug.pdf – Beleg 26B003 angelegt', ''),
                  (STATEMENT_HINT, 'warnColor')], 'button', id='Kontoauszug-erkannt-Hinweis'),
    pytest.param(True, 200, answer(WITHOUT_NUMBER),
                 [('✓ ohne.pdf – Beleg angelegt', ''), (NO_NUMBER_HINT, 'warnColor')], 'button', id='ohne-Nummer-Hinweis'),
    pytest.param(True, 200, answer(CREATED, BROKEN),
                 [('✓ <b>beleg-1</b>.pdf – Beleg 26B001 angelegt', ''), ('✗ kaputt.pdf: ' + RECEIPT_UPLOAD_FAILED, 'errorColor')],
                 'button', id='Fehler-je-Datei'),
    pytest.param(True, 200, answer(EMPTY), [('✗ leer.pdf: Die Datei ist leer.', 'errorColor')], 'button', id='leere-Datei'),
    pytest.param(False, 413, json.dumps({'success': False, 'error': 'Die Anfrage ist zu groß (höchstens 200 MB).'}),
                 [('Fehler beim Hochladen: Die Anfrage ist zu groß (höchstens 200 MB).', 'errorColor')], None,
                 id='Fehlerstatus-mit-Text'),
    pytest.param(False, 403, json.dumps({'success': False, 'error': RECEIPTS_DEMO_REFUSED}),
                 [('Fehler beim Hochladen: ' + RECEIPTS_DEMO_REFUSED, 'errorColor')], None, id='Demo-Modus-abgelehnt'),
    pytest.param(False, 403, '<h1>403</h1>', [(REFUSED.format(403), 'errorColor')], None, id='Fehlerstatus-ohne-JSON'),
    pytest.param(True, 200, 'keine Antwort', [(REFUSED.format(200), 'errorColor')], None, id='Antwort-ist-kein-JSON'),
    pytest.param(True, 200, 'null', [(REFUSED.format(200), 'errorColor')], None, id='Antwort-ist-JSON-null'),
    pytest.param(True, 200, json.dumps({'success': False}), [(REFUSED.format(200), 'errorColor')], None,
                 id='success-false-ohne-Text'),
    pytest.param(True, 200, json.dumps({'error': 'Ungültige Anfrage.'}),
                 [('Fehler beim Hochladen: Ungültige Anfrage.', 'errorColor')], None, id='Fehler-im-JSON'),
]


def run_upload_script(tmp_path, script, scenario):
    harness, source, data = (tmp_path / name for name in ('harness.js', 'upload.js', 'scenario.json'))
    harness.write_text(HARNESS, encoding='utf-8')
    source.write_text(script, encoding='utf-8')
    data.write_text(json.dumps(scenario), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(source), str(data)], capture_output=True, text=True, encoding='utf-8',
                          timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@NO_NODE
@pytest.mark.parametrize('ok, status, body, lines, after', UPLOAD_CASES)
def test_the_upload_script_shows_the_result_and_reloads_only_when_nothing_needs_reading(server_port, auto_login, tmp_path, ok,
                                                                                         status, body, lines, after):
    out = run_upload_script(tmp_path, receipts_script(server_port), {'ok': ok, 'status': status, 'body': body})
    assert {note['tag'] for note in out['notes']} <= {'p', 'button'}
    assert [(note['text'], note['className']) for note in out['notes'] if note['tag'] == 'p'] == lines
    buttons = [note for note in out['notes'] if note['tag'] == 'button']
    if after == 'reload':
        assert buttons == [] and out['delays'] == [3000] and out['reloadsByTimer'] == 1
    else:
        assert out['delays'] == [] and out['reloadsByTimer'] == 0                # die Meldung bleibt stehen
        assert buttons == ([REFRESH] if after == 'button' else [])
        assert out['reloadsByButton'] == (1 if after == 'button' else 0)         # der Knopf lädt die Liste neu


@NO_NODE
def test_a_network_error_is_shown_and_nothing_reloads(server_port, auto_login, tmp_path):
    out = run_upload_script(tmp_path, receipts_script(server_port), {'network_error': True})
    assert [(note['text'], note['className']) for note in out['notes']] == [
        ('Fehler beim Hochladen: Error: offline', 'errorColor')]
    assert out['delays'] == []


# ── openEditForm der Listenseiten ────────────────────────────────────────────
@pytest.mark.parametrize('path', [
    pytest.param('/transactions', id='Buchungen'),
    pytest.param('/assets', id='Anlagen'),
    pytest.param('/asset_categories', id='AfA-Kategorien'),
    pytest.param('/masterdata/contacts', id='Kontakte'),
    pytest.param('/masterdata/articles', id='Artikel'),
    pytest.param('/masterdata/skr', id='Kontenrahmen'),
    pytest.param('/masterdata/bankaccounts', id='Bankkonten'),
    pytest.param('/masterdata/numberranges', id='Nummernkreise'),
])
def test_open_edit_form_opens_the_address_on_an_error_status_and_on_an_answer_without_a_form(server_port, auto_login, path):
    page = get(server_port, path)
    assert page.status == 200
    (script,) = [text for text in Dom(page.text).scripts if 'function openEditForm(' in text]
    assert "if (!r.ok) throw new Error('status ' + r.status);" in script           # der Fehlerstatus geht in den .catch
    assert re.search(r'if \(newForm && curForm\) \{.*?\} else \{\s*window\.location\.href = ', script, re.S)
    assert re.search(r'\.catch\(\(\) => \{ window\.location\.href = ', script)     # … der die Adresse öffnet


# ── Archivname ───────────────────────────────────────────────────────────────
def test_a_backup_name_that_differs_only_in_case_counts_as_taken(tmp_path, monkeypatch):
    class Day(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 5, 9, 30)

    monkeypatch.setattr(backup_mod, 'datetime', Day)
    folder = tmp_path / 'backup'
    folder.mkdir()

    def next_name():
        return os.path.basename(backup_mod._next_archive_path(str(tmp_path), '.zip'))

    assert next_name() == '20260105_Backup.zip'                                  # ohne vorhandene Datei wie bisher
    (folder / '20260105_BACKUP.ZIP').write_bytes(b'eine vorhandene Sicherung')
    assert next_name() == '20260105_Backup_2.zip'                                # sonst träfe der Name die vorhandene Datei
    (folder / '20260105_backup_2.7zip').write_bytes(b'noch eine')
    assert next_name() == '20260105_Backup_3.zip'
    (folder / '20260104_Backup.zip').write_bytes(b'ein anderer Tag')
    (folder / '20260105_BACKUP.ZIP').unlink()
    (folder / '20260105_backup_2.7zip').unlink()
    assert next_name() == '20260105_Backup.zip'                                  # ein anderer Tag zählt nicht
