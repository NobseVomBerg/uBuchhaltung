# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Im Demo-Modus nimmt die App gar keine Datei eines Besuchers mehr an: auch Logo und Textexport sind abgeschaltet.

Alle Besucher des Demo-Modus teilen ein Datenverzeichnis. Belege (``POST /receipts/upload``), Kontoauszüge (``POST /upload_receipts``) und
die WISO-Datenbank (``POST /wiso/fdb-upload``) waren schon gesperrt (``tests/test_receipt_upload.py``, ``tests/test_statement_upload_demo.py``,
``tests/test_wiso_fdb_upload.py``); hier kommen die zwei letzten Wege dazu: das Logo eines Kontakts (``POST /masterdata/contacts/upload-logo``)
und der Import der Textexporte (``POST /wiso/import``). Beide antworten im Demo-Modus mit 403, bevor ein Byte des Bodys gelesen wird; die
Seiten zeigen den Satz statt der Dateiauswahl. Das Token prüft das Logo davor (Header), der Textexport gar nicht (sein Token steckt im Body,
den niemand liest): Die Ablehnung gilt für jede Anfrage von eigener Herkunft. ``handle_wiso_import`` lehnt als zweite Schicht selbst ab.

Dazu die Aufzählung (``UPLOAD_WAYS``): jeder Weg, auf dem die App eine Datei annimmt, ist im Demo-Modus 403 und hinterlässt keine Spur
(Dateien und Zeilen aller Tabellen vorher und nachher gleich) – und ein Quelltext-Wächter, der anschlägt, sobald ein neuer Weg dazukommt.
Außerhalb des Demo-Modus bleibt alles, wie es war (Textexport: ``tests/test_upload_handlers.py``, ``tests/test_wiso_adoption_report.py``).

Echter Server (Fixture ``server_port``); das Skript des Kontaktformulars läuft in Node (``vm``, ohne Browser) – ohne Node entfallen nur diese
Läufe. Alle Namen und Dateien sind erfundene Testwerte.
"""
import ast
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

import auth
import demo
import userctx
from server import app as server_app
from server import handlers, wiso_upload
from server.app import MAX_UPLOAD_BODY
from server.handlers import LOGO_DEMO_REFUSED, MAX_LOGO_BYTES, WISO_TEXT_DEMO_REFUSED
from server.pages_contacts import PageContacts
from server.upload_handler import RECEIPTS_DEMO_REFUSED, STATEMENTS_DEMO_REFUSED
from tests import installation
from tests.http_client import get, own_origin, send
from tests.test_body_limits import assert_closed, exchange

LOGO = '/masterdata/contacts/upload-logo'
TEXT_IMPORT = '/wiso/import'
LOGO_SENTENCE = 'Im Demo-Modus ist das Hochladen von Logos abgeschaltet.'
TEXT_SENTENCE = 'Im Demo-Modus ist der Import aus Textexporten abgeschaltet.'
NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die Textprüfungen derselben Datei decken das Wichtigste ab')

#: Die Positivliste: jeder Weg, auf dem die App eine Datei annimmt – ``Adresse: (Satz der Ablehnung im Demo-Modus, Status der Annahme
#: sonst)``. ``_UPLOAD_PATHS`` in ``server/app.py`` plus die drei Wege, die dort als eigene Zweige stehen. Ein neuer Weg gehört hierher
#: UND braucht die Sperre im Demo-Modus.
UPLOAD_WAYS = {
    '/receipts/upload': (RECEIPTS_DEMO_REFUSED, 200),
    '/upload_receipts': (STATEMENTS_DEMO_REFUSED, 200),
    '/wiso/fdb-upload': (wiso_upload.DEMO_REFUSED, 200),
    TEXT_IMPORT: (WISO_TEXT_DEMO_REFUSED, 303),
    LOGO: (LOGO_DEMO_REFUSED, 200),
}
NEW_WAY = 'Neuer Upload-Weg: Er braucht die Sperre im Demo-Modus und einen Eintrag hier.'
#: Wie oft nennt der Code (ohne Docstrings und Kommentare) in jeder Datei von ``server/`` ``multipart/form-data``: die Stellen, an denen
#: die App eine Datei als Multipart annimmt oder ein Formular so abschickt.
MULTIPART_PLACES = {'handlers.py': 2, 'multipart.py': 1, 'pages_miscellaneous.py': 1, 'upload_handler.py': 1}


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


def png():
    """Ein kleines, echtes PNG."""
    buffer = io.BytesIO()
    Image.new('RGB', (8, 6), 'red').save(buffer, 'PNG')
    return buffer.getvalue()


def multipart(*parts):
    """Ein Multipart-Body aus ``(Feldname, Dateiname oder None, Inhalt)`` – ein Teil mit Dateiname ist eine Datei, sonst ein Textfeld."""
    boundary = 'GRENZE1234567890'
    body = b''
    for name, filename, content in parts:
        disposition = f'Content-Disposition: form-data; name="{name}"' + (f'; filename="{filename}"' if filename else '')
        kind = 'Content-Type: application/octet-stream\r\n' if filename else ''
        body += f'--{boundary}\r\n{disposition}\r\n{kind}\r\n'.encode('utf-8') + content + b'\r\n'
    return f'multipart/form-data; boundary={boundary}', body + f'--{boundary}--\r\n'.encode()


SMALL_FILE = 'Eine kleine Datei'.encode('utf-8')


def send_upload(port, route, csrf):
    """Eine kleine Datei an ``route`` schicken, wie die jeweilige Seite es tut: eigene Herkunft und das Token, dort wo die Seite es mitschickt
    (Header; beim klassischen Formular des Textexports als Feld im Body)."""
    headers = {'Origin': own_origin(port), 'X-CSRF-Token': csrf}
    if route == '/wiso/fdb-upload':                                # die WISO-Datenbank: roher Body
        headers['Content-Type'] = 'application/octet-stream'
        return send(port, 'POST', route + '?slot=client', headers, SMALL_FILE)
    if route == TEXT_IMPORT:                                       # das Token steckt im Body
        headers.pop('X-CSRF-Token')
        content_type, body = multipart(('csrf', None, csrf.encode('utf-8')), ('csvfile', 'export.csv', SMALL_FILE))
    elif route == LOGO:
        content_type, body = multipart(('logofile', 'logo.png', png()))
    else:                                                          # Belege und Kontoauszüge: das Feld ``files``
        content_type, body = multipart(('files', 'notiz.txt', SMALL_FILE))
    headers['Content-Type'] = content_type
    return send(port, 'POST', route, headers, body)


def refusal_text(response):
    """Der Satz einer Ablehnung: bei JSON das Feld ``error``, sonst der Absatz der kleinen Seite (``<h1>403</h1><p>…</p>``)."""
    if response.headers['Content-Type'] == 'application/json':
        return json.loads(response.text)['error']
    match = re.fullmatch(r'<h1>403</h1><p>(.*)</p>', response.text)
    assert match, response.text
    return match.group(1)


def row_counts(db):
    """Zeilen je Tabelle der Datenbank."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def new_contact(db):
    db.insert_contact(contact_type='customer', entity_type='company', company_name='Muster GmbH')
    return db.fetch_contacts()[0][0]


def query_of(location):
    """Die Parameter einer Weiterleitung als ``{Name: Wert}``."""
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


# ── Logo: Demo-Modus ─────────────────────────────────────────────────────────
def test_in_demo_mode_the_logo_upload_is_403_and_creates_nothing(server_port, demo_site):
    before = installation.tree(userctx.DATA_ROOT)
    response = send_upload(server_port, LOGO, demo_site.csrf)
    assert response.status == 403
    assert response.headers['Content-Type'] == 'application/json'
    assert json.loads(response.text) == {'success': False, 'error': LOGO_DEMO_REFUSED}
    assert LOGO_DEMO_REFUSED == LOGO_SENTENCE
    assert not os.path.exists(os.path.join(demo_site.folder, 'logos'))             # weder Datei noch Ordner
    assert installation.tree(userctx.DATA_ROOT) == before


@pytest.mark.parametrize('token', [None, 'falsch'], ids=['Token-fehlt', 'Token-falsch'])
def test_in_demo_mode_a_logo_request_without_a_valid_token_is_still_the_csrf_refusal(server_port, demo_site, token):
    before = installation.tree(userctx.DATA_ROOT)
    content_type, body = multipart(('logofile', 'logo.png', png()))
    headers = {'Origin': own_origin(server_port), 'Content-Type': content_type, **({} if token is None else {'X-CSRF-Token': token})}
    response = send(server_port, 'POST', LOGO, headers, body)
    assert response.status == 403 and 'Ungültiges oder fehlendes Sicherheits-Token' in response.text
    assert LOGO_SENTENCE not in response.text                                      # die Token-Prüfung kommt zuerst
    assert response.headers['Content-Type'] != 'application/json'                  # und antwortet wie vor der Änderung
    assert installation.tree(userctx.DATA_ROOT) == before


@pytest.mark.parametrize('length, content_type', [
    (MAX_LOGO_BYTES + 1, 'multipart/form-data; boundary=X'),       # über der Grenze der Logos: 403, nicht die Größenmeldung
    (5000, 'multipart/form-data; boundary=X'),                     # ein Body, der nie ankommt
    (5000, 'text/plain'),                                          # kein Multipart: 403, nicht „Kein Multipart-Upload“
], ids=['über-der-Grenze', 'kommt-nie-an', 'kein-Multipart'])
def test_in_demo_mode_the_logo_upload_is_refused_before_the_body_is_read(server_port, demo_site, length, content_type):
    before = installation.tree(userctx.DATA_ROOT)
    headers = {'Content-Type': content_type, 'X-CSRF-Token': demo_site.csrf, 'Origin': own_origin(server_port), 'Content-Length': str(length)}
    status, resp, body = exchange(server_port, LOGO, headers)
    assert status == 403 and json.loads(body) == {'success': False, 'error': LOGO_DEMO_REFUSED}
    assert_closed(resp)                                                            # die ungelesenen Bytes bleiben nie liegen
    assert installation.tree(userctx.DATA_ROOT) == before


# ── Logo: außerhalb des Demo-Modus ───────────────────────────────────────────
def test_outside_the_demo_the_same_logo_upload_is_taken_as_before(server_port, auto_login):
    response = send_upload(server_port, LOGO, auto_login.csrf)
    assert response.status == 200
    assert json.loads(response.text) == {'success': True, 'path': 'data/logos/logo.png'}
    logos = os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'logos')
    assert os.listdir(logos) == ['logo.png']


# ── Textexport: Demo-Modus ───────────────────────────────────────────────────
def test_in_demo_mode_the_text_import_is_403_and_imports_nothing(server_port, demo_site):
    before_all, rows = installation.tree(userctx.DATA_ROOT), row_counts(demo_site.db)
    response = send_upload(server_port, TEXT_IMPORT, demo_site.csrf)
    assert response.status == 403
    assert response.headers['Content-Type'] == 'text/html'
    assert response.text == f'<h1>403</h1><p>{WISO_TEXT_DEMO_REFUSED}</p>'
    assert WISO_TEXT_DEMO_REFUSED == TEXT_SENTENCE
    assert not os.path.exists(os.path.join(demo_site.folder, 'wiso_import_result.json'))
    assert row_counts(demo_site.db)['Bookings'] == 0 and row_counts(demo_site.db) == rows
    assert installation.tree(userctx.DATA_ROOT) == before_all


def test_in_demo_mode_the_text_import_needs_no_token_to_be_refused(server_port, demo_site):
    """Das Token dieses Formulars steckt im Body, den im Demo-Modus niemand liest: Auch ohne das Feld ``csrf`` (und mit falschem) ist es
    die Ablehnung des Demo-Modus, nicht die Weiterleitung mit dem Satz zum Token."""
    before = installation.tree(userctx.DATA_ROOT)
    for fields in ((), (('csrf', None, b'falsch'),)):
        content_type, body = multipart(*fields, ('csvfile', 'export.csv', SMALL_FILE))
        response = send(server_port, 'POST', TEXT_IMPORT, {'Origin': own_origin(server_port), 'Content-Type': content_type}, body)
        assert response.status == 403 and refusal_text(response) == WISO_TEXT_DEMO_REFUSED
    assert installation.tree(userctx.DATA_ROOT) == before


def test_in_demo_mode_the_text_import_is_refused_before_the_body_is_read_and_before_the_size_is_checked(server_port, demo_site):
    before = installation.tree(userctx.DATA_ROOT)
    headers = {'Content-Type': 'multipart/form-data; boundary=X', 'Origin': own_origin(server_port), 'Content-Length': '5000000'}   # der Body kommt nie an
    status, resp, body = exchange(server_port, TEXT_IMPORT, headers)
    assert status == 403 and body.decode('utf-8') == f'<h1>403</h1><p>{WISO_TEXT_DEMO_REFUSED}</p>'
    assert_closed(resp)
    # ein erklärter Body über der Grenze ist hier 403, nicht 413: die Demo-Prüfung steht vor der Größe
    status, resp, body = exchange(server_port, TEXT_IMPORT, {**headers, 'Content-Length': str(MAX_UPLOAD_BODY + 1)})
    assert status == 403 and body.decode('utf-8') == f'<h1>403</h1><p>{WISO_TEXT_DEMO_REFUSED}</p>'
    assert_closed(resp)
    assert installation.tree(userctx.DATA_ROOT) == before


class ExplodingRequest:
    """Ein Anfrage-Handler, dessen Body beim Lesen einen Fehler wirft: Im Demo-Modus darf ``handle_wiso_import`` nichts lesen."""

    def __init__(self):
        self.headers = {'Content-Type': 'multipart/form-data; boundary=X', 'Content-Length': '10'}
        self.rfile = self

    def read(self, *args):
        raise AssertionError('der Body darf im Demo-Modus nie gelesen werden')


def test_in_demo_mode_the_handler_itself_refuses_before_it_reads_anything(demo_site):
    rows = row_counts(demo_site.db)
    status, location = handlers.handle_wiso_import(ExplodingRequest(), demo_site.db)
    assert status == 303 and urlsplit(location).path == '/miscellaneous'
    assert query_of(location) == {'wiso_import': 'error', 'msg': WISO_TEXT_DEMO_REFUSED}
    assert row_counts(demo_site.db) == rows
    assert not os.path.exists(os.path.join(demo_site.folder, 'wiso_import_result.json'))


# ── Die Seiten ───────────────────────────────────────────────────────────────
def contact_pages(db):
    """Die Adressen des Kontaktformulars: das leere Formular, das neue einer Person und das eines vorhandenen Kontakts."""
    return ['/masterdata/contacts', '/masterdata/contacts/new?entity=person', f'/masterdata/contacts/edit?id={new_contact(db)}']


def test_in_demo_mode_the_contact_form_shows_the_sentence_instead_of_the_file_picker(server_port, demo_site):
    for path in contact_pages(demo_site.db):
        page = get(server_port, path).text
        assert f'<small class="muted">{LOGO_SENTENCE}</small>' in page, path
        assert 'id="logo_file_input"' not in page and 'type="file"' not in page, path
        assert 'name="logo" id="field_logo"' in page and 'id="logo_preview_div"' in page, path             # Textfeld und Vorschau bleiben
        assert page.index('name="logo" id="field_logo"') < page.index(LOGO_SENTENCE) < page.index('id="logo_preview_div"'), path


def test_outside_the_demo_the_contact_form_offers_the_file_picker_and_not_the_sentence(server_port, auto_login):
    for path in contact_pages(auto_login.db):
        page = get(server_port, path).text
        assert LOGO_SENTENCE not in page, path
        assert 'id="logo_file_input"' in page and 'Datei wählen</button>' in page, path
        assert page.index('name="logo" id="field_logo"') < page.index('id="logo_file_input"') < page.index('id="logo_preview_div"'), path


def test_in_demo_mode_the_miscellaneous_page_shows_the_sentence_instead_of_the_text_import_form(server_port, demo_site):
    page = get(server_port, '/miscellaneous').text
    assert f'<p class="hint">{TEXT_SENTENCE}</p>' in page
    assert 'action="/wiso/import"' not in page and 'name="csvfile"' not in page
    heading, explanation, database_heading = '<h2>WISO Mein Büro Import</h2>', 'Importiert Buchungen aus', 'Import aus der Datenbank</h2>'
    assert page.index(heading) < page.index(explanation) < page.index(TEXT_SENTENCE) < page.index(database_heading)   # im Abschnitt, Text davor bleibt
    assert wiso_upload.DEMO_REFUSED in page                                                                       # der Satz der Datenbank daneben


def test_outside_the_demo_the_miscellaneous_page_offers_the_text_import_form_and_not_the_sentence(server_port, auto_login):
    page = get(server_port, '/miscellaneous').text
    assert TEXT_SENTENCE not in page
    assert 'action="/wiso/import"' in page and 'name="csvfile"' in page and 'WISO Import starten' in page
    assert page.index('<h2>WISO Mein Büro Import</h2>') < page.index('action="/wiso/import"') < page.index('Import aus der Datenbank</h2>')


# ── Das Skript des Kontaktformulars ──────────────────────────────────────────
def contact_script(page):
    """Das Skript des Kontaktformulars (Logo-Auswahl, Anzeigename, Kürzel)."""
    (script,) = [text for text in re.findall(r'<script>(.*?)</script>', page, re.S) if 'function updateLogoPath(' in text]
    return script


def test_the_contact_script_hangs_no_listener_unguarded_on_a_file_picker_that_may_be_missing(demo_site):
    """Ohne ``#logo_file_input`` ist ``fi`` ``null``: Ein Hörer ohne Prüfung bräche das ganze Skript ab (und mit ihm das Kürzel). Die echte
    Prüfung ist der Lauf in Node (unten); dieser Text-Wächter gilt auch ohne Node."""
    script = contact_script(PageContacts(demo_site.db))
    assert script.count('fi.addEventListener') == 1
    assert script.index("getElementById('logo_file_input')") < script.index('if (fi)') < script.index('fi.addEventListener')


HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, variant] = process.argv;
const listeners = {logoFile: [], form: [], company: [], first: [], last: [], title: []};
const recorder = key => name => listeners[key].push(name);
const make = key => ({addEventListener: recorder(key), value: '', dataset: {}, style: {}});
const elements = {contact_form: make('form'), field_company_name: make('company'), field_first_name: make('first'),
                  field_last_name: make('last')};
if (variant === 'with') elements.logo_file_input = make('logoFile');
// ein fehlendes Element liefert null, wie im Browser
const sandbox = {document: {getElementById: id => elements[id] || null,
                            querySelector: selector => (selector === 'input[name="title"]' ? make('title') : null)}, console};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);
console.log(JSON.stringify({
  listeners,
  uploadPromise: vm.runInContext('_logoUploadPromise', sandbox),
  functions: ['updateLogoPath', 'autoFillAbbreviation', 'checkAbbreviationUnique'].map(name => vm.runInContext('typeof ' + name, sandbox)),
}));
"""


def run_script(tmp_path, page, variant):
    harness, source = tmp_path / 'harness.js', tmp_path / 'page.js'
    harness.write_text(HARNESS, encoding='utf-8')
    source.write_text(contact_script(page), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(source), variant], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr                                       # das Skript wirft nicht
    return json.loads(done.stdout)


@NO_NODE
def test_in_demo_mode_the_contact_script_runs_to_the_end_without_the_file_picker(tmp_path, demo_site):
    out = run_script(tmp_path, PageContacts(demo_site.db), 'without')
    # ``blur`` ist das Letzte, was das Skript hängt: Es lief ohne Fehler bis zum Ende
    assert out['listeners'] == {'logoFile': [], 'form': ['submit'], 'company': ['input', 'blur'], 'first': ['input', 'blur'],
                                'last': ['input', 'blur'], 'title': ['input']}
    assert out['uploadPromise'] is None and out['functions'] == ['function'] * 3


@NO_NODE
def test_outside_the_demo_the_contact_script_still_hangs_its_listener_on_the_file_picker(tmp_path, tmp_db):
    out = run_script(tmp_path, PageContacts(tmp_db), 'with')
    assert out['listeners'] == {'logoFile': ['change'], 'form': ['submit'], 'company': ['input', 'blur'], 'first': ['input', 'blur'],
                                'last': ['input', 'blur'], 'title': ['input']}
    assert out['uploadPromise'] is None and out['functions'] == ['function'] * 3


# ── Die Aufzählung aller Upload-Wege ─────────────────────────────────────────
@pytest.mark.parametrize('route', sorted(UPLOAD_WAYS))
def test_in_demo_mode_every_upload_way_is_403_and_leaves_no_trace(server_port, demo_site, route):
    """Eine kleine Datei mit gültigem Token an JEDEN Weg: 403 mit dem Satz des Wegs, keine neue Datei und keine neue Zeile."""
    before_files, before_rows = installation.tree(userctx.DATA_ROOT), row_counts(demo_site.db)
    response = send_upload(server_port, route, demo_site.csrf)
    assert response.status == 403
    assert refusal_text(response) == UPLOAD_WAYS[route][0]
    assert installation.tree(userctx.DATA_ROOT) == before_files
    assert row_counts(demo_site.db) == before_rows


@pytest.mark.parametrize('route', sorted(UPLOAD_WAYS))
def test_outside_the_demo_every_upload_way_still_takes_the_same_file(server_port, auto_login, route):
    """Die Gegenprobe: Dieselbe Anfrage wird außerhalb des Demo-Modus angenommen (sonst bewiese die Ablehnung oben nichts)."""
    response = send_upload(server_port, route, auto_login.csrf)
    assert response.status == UPLOAD_WAYS[route][1], response.text
    assert UPLOAD_WAYS[route][0] not in response.text


def test_every_upload_route_of_the_code_is_on_the_list():
    """Ein neuer Eintrag in ``_UPLOAD_PATHS``, der nicht in ``UPLOAD_WAYS`` steht, hat keine Sperre im Demo-Modus – und keinen Test dafür."""
    unknown = sorted(set(server_app._UPLOAD_PATHS) - set(UPLOAD_WAYS))
    assert not unknown, f'{NEW_WAY}\n(in _UPLOAD_PATHS, nicht in UPLOAD_WAYS: {unknown})'
    with open(server_app.__file__, encoding='utf-8') as handle:
        source = handle.read()
    gone = [route for route in UPLOAD_WAYS if f'"{route}"' not in source]
    assert not gone, f'Die Liste ist veraltet: server/app.py kennt diese Wege nicht mehr: {gone}'


def multipart_places():
    """Je Datei von ``server/``: wie viele Zeichenketten im Code (ohne Docstrings, ohne Kommentare) ``multipart/form-data`` nennen."""
    places, folder = {}, os.path.dirname(server_app.__file__)
    for name in sorted(os.listdir(folder)):
        if not name.endswith('.py'):
            continue
        with open(os.path.join(folder, name), encoding='utf-8') as handle:
            tree = ast.parse(handle.read())
        documents = {id(node.body[0].value) for node in ast.walk(tree)
                     if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body
                     and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
                     and isinstance(node.body[0].value.value, str)}
        count = sum(1 for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in documents and 'multipart/form-data' in node.value.lower())
        if count:
            places[name] = count
    return places


def test_no_new_place_takes_a_multipart_body_unnoticed():
    """Eine neue Stelle, die ``multipart/form-data`` liest oder ein Formular so abschickt, ist fast sicher ein neuer Upload-Weg."""
    assert multipart_places() == MULTIPART_PLACES, (
        f'{NEW_WAY}\n(Stellen mit multipart/form-data in server/: {multipart_places()}, erwartet: {MULTIPART_PLACES})')
