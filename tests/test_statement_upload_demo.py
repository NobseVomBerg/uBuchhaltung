# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kontoauszüge hochladen im Demo-Modus (2.6.7): abgeschaltet, wie das Hochladen von Belegen seit 2.5.11.

Im Demo-Modus teilen sich alle Besucher ein Datenverzeichnis. ``POST /upload_receipts`` – der Upload der Seite „Buchungen“, der Dateien
unter ``Documents/`` und Importe unter ``pending_imports/`` ablegt – ist dort 403, bevor ein Byte des Bodys gelesen wird: erst das CSRF-
Token (wie überall), dann der Demo-Modus, dann die Größe. Die Seite zeigt den Satz statt der Ablage, und ihr Skript wirft ohne die Ablage
keinen Fehler (sonst bräche es vor den Hörern der Vorschau ab). ``POST /confirm_transactions`` ist im Demo-Modus ebenfalls 403: Ein Import
aus der Zeit vor der Sperre, der noch in ``pending_imports/`` liegt, lässt sich dort nicht mehr bestätigen – gelöscht wird nichts.
Außerhalb des Demo-Modus bleibt alles, wie es war.

Echter Server (Fixture ``server_port``); das Skript der Seite läuft in Node (``vm``, ohne Browser) – ohne Node entfallen nur diese Läufe.
Alle Namen, IBANs und Beträge sind erfundene Testwerte.
"""
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

import auth
import demo
import userctx
from server.app import MAX_UPLOAD_BODY
from server.pages_transactions import PageTransactions
from server.upload_handler import STATEMENTS_DEMO_REFUSED
from tests import installation
from tests.http_client import get, own_origin, post, send
from tests.test_body_limits import assert_closed, exchange
from tests.test_dkb_csv_import import NEW_ROWS, OWN_IBAN, new_format

SENTENCE = 'Im Demo-Modus ist das Hochladen von Kontoauszügen abgeschaltet.'
NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die Textprüfungen derselben Datei decken das Wichtigste ab')


# ── Hilfen ───────────────────────────────────────────────────────────────────
@pytest.fixture
def demo_site(monkeypatch):
    """Nur Demodaten, Variable gesetzt: Jeder Besucher arbeitet als ``demo`` (kein Administrator, keine Anmeldung)."""
    monkeypatch.setenv(demo.ENV_VAR, '1')
    userctx.set_mode('multi')
    auth.create_user('demo', installation.PASSWORD, is_admin=False, allow_reserved=True)
    db = installation.open_db('demo')
    folder = os.path.join(userctx.DATA_ROOT, 'users', 'demo')
    with open(os.path.join(folder, demo.MARKER_FILE), 'w', encoding='utf-8') as handle:
        handle.write('x')
    return SimpleNamespace(db=db, folder=folder, csrf=auth.csrf_for('auto-login:demo'))


def multipart(*files):
    """Ein Multipart-Body mit je einem Teil ``files`` – so schickt ihn die Seite „Buchungen“."""
    boundary = 'GRENZE1234567890'
    body = b''
    for name, content in files:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                 'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8') + content + b'\r\n'
    return f'multipart/form-data; boundary={boundary}', body + f'--{boundary}--\r\n'.encode()


def statement_files():
    """Eine gültige Umsatzliste der DKB (wird ein Import unter ``pending_imports/``) und eine Textdatei (liegt dann unter ``Documents/``)."""
    return [('umsaetze.csv', new_format(*NEW_ROWS).encode('utf-8')), ('notiz.txt', 'Eine Notiz'.encode('utf-8'))]


def upload(port, csrf, *files, headers=None):
    """Dateien über die Route des Servers hochladen, wie es die Seite „Buchungen“ tut (eigene Herkunft, Token als Header)."""
    content_type, body = multipart(*files)
    sent = {'Origin': own_origin(port), 'Content-Type': content_type, **(headers or {})}
    if csrf is not None:
        sent['X-CSRF-Token'] = csrf
    return send(port, 'POST', '/upload_receipts', sent, body)


def confirm(port, csrf, **form):
    return post(port, '/confirm_transactions', headers={'Origin': own_origin(port)}, form=form, csrf=csrf)


def booking_count(db):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute('SELECT COUNT(*) FROM Bookings').fetchone()[0]


def pending_import(folder, import_id):
    """Ein Import aus der Zeit vor der Sperre: die Datei, die der Upload unter ``pending_imports/`` abgelegt hätte (ein Auszug mit
    einem Umsatz auf dem Konto mit ``OWN_IBAN``)."""
    pending = os.path.join(folder, 'pending_imports')
    os.makedirs(pending)
    path = os.path.join(pending, f'{import_id}_test.json')
    statement = {'filename': 'auszug.pdf', 'bank_code': 'VBR', 'iban': OWN_IBAN, 'document_date': '2025-01-31',
                 'transactions': [{'date': '2025-01-10', 'amount': -50.0, 'reference': 'A-1', 'recipient': 'Partner',
                                   'foreign_iban': 'DE00FOREIGN'}]}
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'files': [statement], 'import_id': import_id}, handle)
    return path


def read_text(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


# ── Die Route: Demo-Modus ────────────────────────────────────────────────────
def test_in_demo_mode_the_statement_upload_is_403_and_creates_nothing(server_port, demo_site):
    before_all = installation.tree(userctx.DATA_ROOT)
    bookings = booking_count(demo_site.db)
    response = upload(server_port, demo_site.csrf, *statement_files())
    assert response.status == 403
    assert response.headers['Content-Type'] == 'application/json'
    assert json.loads(response.text) == {'success': False, 'error': STATEMENTS_DEMO_REFUSED}
    assert STATEMENTS_DEMO_REFUSED == SENTENCE
    for folder in ('Documents', 'pending_imports'):                          # weder Datei noch Import, nicht einmal der Ordner
        assert not os.path.exists(os.path.join(demo_site.folder, folder)), folder
    assert booking_count(demo_site.db) == bookings
    assert installation.tree(userctx.DATA_ROOT) == before_all


def test_in_demo_mode_the_upload_is_refused_before_the_body_is_read_and_before_the_size_is_checked(server_port, demo_site):
    before = installation.tree(userctx.DATA_ROOT)
    headers = {'Content-Type': 'multipart/form-data; boundary=X', 'X-CSRF-Token': demo_site.csrf,
               'Origin': own_origin(server_port), 'Content-Length': '5000000'}                 # der Body kommt nie an
    status, resp, body = exchange(server_port, '/upload_receipts', headers)
    assert status == 403 and json.loads(body) == {'success': False, 'error': STATEMENTS_DEMO_REFUSED}
    assert_closed(resp)                                                                       # die ungelesenen Bytes bleiben nie liegen
    # ein erklärter Body über der Grenze ist hier 403, nicht 413: die Demo-Prüfung steht vor der Größe
    status, resp, body = exchange(server_port, '/upload_receipts', {**headers, 'Content-Length': str(MAX_UPLOAD_BODY + 1)})
    assert status == 403 and json.loads(body) == {'success': False, 'error': STATEMENTS_DEMO_REFUSED}
    assert_closed(resp)
    assert installation.tree(userctx.DATA_ROOT) == before


@pytest.mark.parametrize('token', [None, 'falsch'], ids=['Token-fehlt', 'Token-falsch'])
def test_in_demo_mode_a_request_without_a_valid_token_is_still_the_csrf_refusal(server_port, demo_site, token):
    before = installation.tree(userctx.DATA_ROOT)
    response = upload(server_port, token, *statement_files())
    assert response.status == 403 and 'Ungültiges oder fehlendes Sicherheits-Token' in response.text
    assert SENTENCE not in response.text                                                      # die Token-Prüfung kommt zuerst
    assert response.headers['Content-Type'] != 'application/json'                              # und antwortet wie vor der Änderung
    assert installation.tree(userctx.DATA_ROOT) == before


def test_in_demo_mode_an_older_pending_import_cannot_be_confirmed_and_stays_untouched(server_port, demo_site):
    demo_site.db.insert_account('Konto A', 'Inhaber', OWN_IBAN, 'GENODE1X', 'Bank A', skr_account=1200)
    path = pending_import(demo_site.folder, 'pytest_demo01')
    content, bookings = read_text(path), booking_count(demo_site.db)
    before = installation.tree(demo_site.folder)
    for form in ({'import_id': 'pytest_demo01'},                         # alle Belege
                 {'import_id': 'pytest_demo01', 'file_index': '0'},      # ein Beleg
                 {'import_id': 'gibt-es-nicht'},                         # auch kein Import: 403, nicht 404
                 {}):                                                    # und ohne ``import_id``: 403, nicht 400
        response = confirm(server_port, demo_site.csrf, **form)
        assert response.status == 403, form
        assert json.loads(response.text) == {'ok': False, 'error': STATEMENTS_DEMO_REFUSED}, form
    assert read_text(path) == content and booking_count(demo_site.db) == bookings    # nichts importiert, nichts gelöscht
    assert installation.tree(demo_site.folder) == before


def test_in_demo_mode_the_confirmation_needs_the_token_too(server_port, demo_site):
    path = pending_import(demo_site.folder, 'pytest_demo02')
    content = read_text(path)
    response = confirm(server_port, 'falsch', import_id='pytest_demo02')
    assert response.status == 403 and 'Sicherheits-Token' in response.text and SENTENCE not in response.text
    assert read_text(path) == content


# ── Die Route: außerhalb des Demo-Modus ──────────────────────────────────────
def test_outside_the_demo_the_same_upload_is_taken_as_before(server_port, auto_login):
    response = upload(server_port, auto_login.csrf, *statement_files())
    assert response.status == 200
    data = json.loads(response.text)
    assert set(data) == {'import_id', 'files', 'other_files'}
    assert data['import_id'] and [entry['filename'] for entry in data['files']] == ['umsaetze.csv']
    assert [(entry['filename'], entry['status']) for entry in data['other_files']] == [('notiz.txt', 'uploaded')]
    user_folder = os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN)
    assert os.listdir(os.path.join(user_folder, 'Documents')) == ['notiz.txt']
    assert [name for name in os.listdir(os.path.join(user_folder, 'pending_imports')) if name.startswith(data['import_id'])]


def test_outside_the_demo_an_unknown_import_is_still_404(server_port, auto_login):
    response = confirm(server_port, auto_login.csrf, import_id='gibt-es-nicht')
    assert response.status == 404 and json.loads(response.text) == {'ok': False, 'error': 'Import nicht gefunden'}
    response = confirm(server_port, auto_login.csrf)                                      # ohne ``import_id`` wie bisher 400
    assert response.status == 400 and json.loads(response.text) == {'ok': False, 'error': 'Fehlende import_id'}


# ── Die Seite ────────────────────────────────────────────────────────────────
def test_in_demo_mode_the_page_shows_the_sentence_instead_of_the_drop_zone(server_port, demo_site):
    page = get(server_port, '/transactions').text
    assert SENTENCE in page
    assert 'id="dropZone"' not in page and 'id="fileInput"' not in page
    assert page.index('<h2>Kontoauszüge hochladen</h2>') < page.index(SENTENCE) < page.index('id="uploadStatus"')   # im Kasten, unter der Überschrift
    assert 'id="importPreview"' in page                                                       # der Rest der Seite bleibt


def test_outside_the_demo_the_page_offers_the_drop_zone_and_not_the_sentence(server_port, auto_login):
    page = get(server_port, '/transactions').text
    assert SENTENCE not in page
    assert 'id="dropZone"' in page and 'id="fileInput"' in page and '<h2>Kontoauszüge hochladen</h2>' in page
    assert page.index('id="dropZone"') < page.index('id="fileInput"') < page.index('id="uploadStatus"')


def page_script(page):
    """Das Skript der Seite, das Vorschau und Import steuert (wie ``test_import_upload_http._page_script``)."""
    (script,) = [text for text in re.findall(r'<script>(.*?)</script>', page, re.S) if 'function renderCard(' in text]
    return script


def test_the_page_script_hangs_no_listener_unguarded_on_a_drop_zone_that_may_be_missing(demo_site):
    """Ohne ``#dropZone`` ist ``dropZone`` ``null``: Ein Hörer ohne Prüfung bräche das ganze Skript ab. Die echte Prüfung ist der Lauf in
    Node (unten); dieser Text-Wächter gilt auch ohne Node."""
    script = page_script(PageTransactions(demo_site.db, date_from='2026-01-01', date_to='2026-12-31'))
    guarded = script[script.index('if (dropZone)'):script.index('if (fileInput)')]
    assert script.count('dropZone.addEventListener') == guarded.count('dropZone.addEventListener') != 0    # keiner davor oder danach
    assert script.count('fileInput.addEventListener') == 1
    assert script.index('if (fileInput)') < script.index('fileInput.addEventListener')


HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, variant] = process.argv;
const listeners = {document: [], body: [], dropZone: [], fileInput: []};
const recorder = key => name => listeners[key].push(name);
const elements = {uploadStatus: {}};
if (variant === 'with') {
  elements.dropZone = {addEventListener: recorder('dropZone'), classList: {add() {}, remove() {}}};
  elements.fileInput = {addEventListener: recorder('fileInput')};
}
// ein fehlendes Element liefert null, wie im Browser
const sandbox = {document: {body: {addEventListener: recorder('body')}, getElementById: id => elements[id] || null,
                            addEventListener: recorder('document')}, console};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);
console.log(JSON.stringify({
  listeners,
  importBusy: vm.runInContext('importBusy', sandbox),     // ein let hinter dem Block: bräche das Skript früher ab, wäre es nicht gesetzt
  functions: ['renderPreview', 'importBeleg', 'importAll', 'openEditForm', 'uploadFiles'].map(name => vm.runInContext('typeof ' + name, sandbox)),
}));
"""

DOCUMENT_LISTENERS = ['click', 'change', 'input']                          # die der Vorschau: stehen hinter dem Block mit der Ablage
DRAG_EVENTS = ['dragenter', 'dragover', 'dragleave', 'drop']


def run_script(tmp_path, page, variant):
    harness, source = tmp_path / 'harness.js', tmp_path / 'page.js'
    harness.write_text(HARNESS, encoding='utf-8')
    source.write_text(page_script(page), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(source), variant], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr                               # das Skript wirft nicht
    return json.loads(done.stdout)


@NO_NODE
def test_in_demo_mode_the_page_script_runs_to_the_end_without_the_drop_zone(tmp_path, demo_site):
    out = run_script(tmp_path, PageTransactions(demo_site.db, date_from='2026-01-01', date_to='2026-12-31'), 'without')
    assert out['listeners'] == {'document': DOCUMENT_LISTENERS, 'body': DRAG_EVENTS, 'dropZone': [], 'fileInput': []}
    assert out['importBusy'] is False
    assert out['functions'] == ['function'] * 5                            # Vorschau, Import und Bearbeiten der Seite bleiben


@NO_NODE
def test_outside_the_demo_the_page_script_still_hangs_its_listeners_on_the_drop_zone(tmp_path, tmp_db):
    out = run_script(tmp_path, PageTransactions(tmp_db, date_from='2026-01-01', date_to='2026-12-31'), 'with')
    assert out['listeners'] == {'document': DOCUMENT_LISTENERS, 'body': DRAG_EVENTS, 'dropZone': DRAG_EVENTS, 'fileInput': ['change']}
    assert out['importBusy'] is False and out['functions'] == ['function'] * 5
