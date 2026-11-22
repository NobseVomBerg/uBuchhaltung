# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Ablage für Kontoauszüge reagiert auch nach dem Öffnen einer Buchung: Die Hörer hängen am ``body`` und suchen sie bei jedem Ereignis neu.

Die Ablage (``#dropZone``, ``#fileInput``, ``#uploadStatus``) steht im rechten Bereich der Seite „Buchungen“ (``.gridRightCol``), und
``openEditForm`` ersetzt dessen Inhalt per ``innerHTML`` durch den der Seite der geöffneten Buchung. Das Skript – es läuft nur einmal beim Laden –
hängte seine Hörer damals direkt an die Elemente und hielt sie in Konstanten: Danach waren ``#dropZone`` und ``#fileInput`` neue Elemente ohne
Hörer, die Konstanten zeigten auf abgehängte Knoten. Eine Datei zu wählen oder abzulegen bewirkte nichts (keine Anfrage, kein Fehler), bis die
Seite neu geladen wurde. Jetzt hängen die Hörer am ``body`` (nicht an ``document``: die Schleife für die Drag-Ereignisse stoppt sie dort) und
suchen die Ablage bei jedem Ereignis neu; ``uploadFiles`` sucht Knopf, Statusbereich und Dateifeld bei jedem Gebrauch neu und überspringt, was
fehlt (Demo-Modus, Maske gerade getauscht).

Das Skript der Seite läuft in Node (``vm``, ohne Browser) gegen eine kleine Attrappe des DOM, die den Austausch nachahmt – gleiche Ids, neue
Objekte ohne Hörer – und Ereignisse wie der Browser nach oben weitergibt (``stopPropagation`` hält sie an); ohne Node entfallen nur diese Läufe,
der Text-Wächter bleibt. Alle Namen sind erfundene Testwerte.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

import demo
import userctx
from server.pages_transactions import PageTransactions

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – der Text-Wächter derselben Datei deckt das Wichtigste ab')
BUSY_MESSAGE = ['Upload läuft bereits – bitte warten.', 'warn']


# ── Hilfen ───────────────────────────────────────────────────────────────────
def page_script(page):
    """Das Skript der Seite, das Vorschau und Import steuert (wie ``test_statement_upload_demo.page_script``)."""
    (script,) = [text for text in re.findall(r'<script>(.*?)</script>', page, re.S) if 'function renderCard(' in text]
    return script


def build_page(db):
    return PageTransactions(db, date_from='2026-01-01', date_to='2026-12-31')


@pytest.fixture
def script(tmp_db):
    """Das Skript der Seite „Buchungen“ außerhalb des Demo-Modus: mit Ablage."""
    page = build_page(tmp_db)
    assert 'id="dropZone"' in page and 'id="fileInput"' in page
    return page_script(page)


@pytest.fixture
def demo_script(tmp_db, monkeypatch):
    """Das Skript der Seite im Demo-Modus: Dort steht der Satz statt der Ablage (die Variable gesetzt und die Markierung im Ordner ``demo``)."""
    monkeypatch.setenv(demo.ENV_VAR, '1')
    folder = os.path.join(userctx.DATA_ROOT, userctx.USERS_DIR, demo.USERNAME)
    os.makedirs(folder)
    with open(os.path.join(folder, demo.MARKER_FILE), 'w', encoding='utf-8') as handle:
        handle.write('x')
    page = build_page(tmp_db)
    assert 'id="dropZone"' not in page and 'id="fileInput"' not in page and 'id="uploadStatus"' in page
    return page_script(page)


HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, scenarioFile, variant] = process.argv;          // variant: 'with' = Ablage da, 'without' = Demo-Modus (keine Ablage)

// ── eine kleine Attrappe des DOM: Elemente mit Eltern, ``#id``-Selektor und Hörern ───────────────────────────────────────
class El {
  constructor(id, tag) {
    this.id = id; this.tag = tag; this.nodeType = 1; this.parentElement = null; this.children = [];
    this.listeners = {}; this.classes = new Set(); this.disabled = false; this.value = ''; this.innerHTML = ''; this.files = [];
    this.classList = {add: name => this.classes.add(name), remove: name => this.classes.delete(name), contains: name => this.classes.has(name)};
  }
  add(child) { child.parentElement = this; this.children.push(child); return child; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  closest(selector) {                                                    // nur ``#id`` – mehr braucht das Skript hier nicht
    for (let el = this; el; el = el.parentElement) if (selector === '#' + el.id) return el;
    return null;
  }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  querySelector(tag) { return this.all().find(el => el.tag === tag) || null; }
}

// Die Seite: links die (nie getauschte) Vorschau, rechts der Bereich ``.gridRightCol`` mit der Maske einer Buchung und der Ablage.
const body = new El('', 'body');
const right = body.add(new El('', 'div'));
body.add(new El('importPreview', 'div'));
// ``fill`` ersetzt den Inhalt des rechten Bereichs wie ``curForm.innerHTML = newForm.innerHTML``: gleiche Ids, NEUE Objekte ohne Hörer,
// die alten sind abgehängt
function fill({zone = variant === 'with', status = true} = {}) {
  right.children.forEach(child => { child.parentElement = null; });
  right.children = [];
  right.add(new El('formular', 'form')).add(new El('amount', 'input'));
  if (zone) {
    const box = right.add(new El('dropZone', 'div'));
    box.add(new El('', 'p'));
    box.add(new El('fileInput', 'input'));
    box.add(new El('', 'button'));
  }
  if (status) right.add(new El('uploadStatus', 'div'));
}
fill();

const documentNode = {
  listeners: {}, body,
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  getElementById: id => body.all().find(el => el.id === id) || null,     // nur, was im Baum hängt, wie im Browser
  querySelectorAll: () => [], querySelector: () => null,
};

const world = {
  body, calls: [], messages: [], errors: [], pending: [],
  get: id => documentNode.getElementById(id),
  swap: options => fill(options),
  // Ein Ereignis steigt vom Ziel über die Eltern bis ``body`` und ``document`` auf; ``stopPropagation`` hält es nach dem laufenden Element an.
  // Ein Textknoten als Ziel (``nodeType`` 3) beginnt bei seinem Element. Was ein Hörer wirft, steht in ``errors``.
  fire(type, target, extra = {}) {
    const event = {type, target, defaultPrevented: false, stopped: false, ...extra,
                   preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }};
    const path = [];
    for (let el = target.nodeType === 3 ? target.parentElement : target; el; el = el.parentElement) path.push(el);
    for (const holder of [...path, documentNode]) {
      for (const fn of holder.listeners[type] || []) {
        try { fn(event); } catch (error) { world.errors.push(type + ': ' + error); }
      }
      if (event.stopped) break;
    }
    return event;
  },
  // der Server antwortet auf die älteste offene Anfrage (Standard: ein Upload ohne Kontoauszug, die Vorschau bleibt leer); ist keine
  // offen, tut es nichts – die Anfragen des Szenarios stehen in ``calls`` und zeigen dann, dass die Ablage nicht reagiert hat
  respond(data = {import_id: 'abc', files: [], other_files: []}) {
    const next = world.pending.shift();
    if (next) next.resolve({json: () => Promise.resolve(data)});
    return !!next;
  },
  settle: () => new Promise(resolve => setTimeout(resolve, 0)),          // alle Zusagen sind abgearbeitet, bevor der Zeitgeber läuft
  read: name => vm.runInContext(name, sandbox),
};

const sandbox = {
  document: documentNode, window: {location: {href: 'http://127.0.0.1/transactions'}}, history: {pushState() {}},
  appMsg: (text, kind) => world.messages.push([text, kind]),
  FormData: class { constructor() { this.parts = []; } append(name, value) { this.parts.push([name, value]); } },
  fetch: (url, init) => {
    world.calls.push({url, method: init.method, files: init.body.parts.map(part => part[1].name)});
    return new Promise(resolve => world.pending.push({resolve}));
  },
  URLSearchParams, DOMParser: class {}, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);           // wirft das Skript beim Laden, endet der Lauf hier mit Fehler

const scenario = new vm.Script('(async (world) => {\n' + fs.readFileSync(scenarioFile, 'utf8') + '\n})').runInThisContext();
scenario(world)
  .then(result => console.log(JSON.stringify({result, calls: world.calls, messages: world.messages, errors: world.errors})))
  .catch(error => { console.error(error); process.exit(2); });
"""


def run(tmp_path, page_script_text, scenario, variant='with'):
    """Das Skript der Seite und ein Szenario in Node laufen lassen. Das Szenario ist der Rumpf einer ``async``-Funktion mit ``world`` und gibt ein
    JSON-fähiges Ergebnis zurück. Geprüft wird hier schon, dass weder das Skript noch ein Hörer noch ein Antwortzweig wirft (auch keine
    unbehandelte Zusage – der Lauf endete sonst mit Fehler)."""
    harness, source, steps = (tmp_path / name for name in ('harness.js', 'page.js', 'scenario.js'))
    harness.write_text(HARNESS, encoding='utf-8')
    source.write_text(page_script_text, encoding='utf-8')
    steps.write_text(scenario, encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(source), str(steps), variant], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out['errors'] == []
    return out


def uploads(*names):
    """Die erwarteten Anfragen: je Datei ein ``POST /upload_receipts``."""
    return [{'url': '/upload_receipts', 'method': 'POST', 'files': [name]} for name in names]


# ── Vor dem Austausch: wie bisher ────────────────────────────────────────────
BEFORE_SWAP = r"""
const zone = world.get('dropZone'), input = world.get('fileInput');
const sent = [];
input.files = [{name: 'auszug.csv'}];
world.fire('change', input);                                                                      // eine Datei gewählt
sent.push(world.calls.length);
world.respond(); await world.settle();
world.fire('drop', zone.children[0], {dataTransfer: {files: [{name: 'umsaetze.pdf'}]}});          // eine Datei auf die Ablage gelegt
sent.push(world.calls.length);
world.respond(); await world.settle();
const beside = world.fire('drop', world.get('formular'), {dataTransfer: {files: [{name: 'daneben.pdf'}]}});   // neben die Ablage
const field = world.get('amount');
field.files = [{name: 'anderes.csv'}];
world.fire('change', field);                                                                      // ein anderes Feld ändert sich
sent.push(world.calls.length);
return {sent, besidePrevented: beside.defaultPrevented, busy: world.read('uploadBusy')};
"""


@NO_NODE
def test_a_chosen_file_and_a_drop_on_the_zone_each_upload_once_and_a_drop_beside_it_does_nothing(tmp_path, script):
    out = run(tmp_path, script, BEFORE_SWAP)
    assert out['result'] == {'sent': [1, 2, 2], 'besidePrevented': True, 'busy': False}    # ein Ereignis, eine Anfrage; daneben keine
    assert out['calls'] == uploads('auszug.csv', 'umsaetze.pdf')
    assert out['messages'] == []                                                           # und der Browser öffnet die Datei auch daneben nicht


# ── Nach dem Austausch: der Fehlfall ─────────────────────────────────────────
AFTER_SWAP = r"""
const results = [];
for (let round = 1; round <= 2; round++) {                       // der Bereich wird mehrmals getauscht – jedes Mal muss es gehen
  const old = {zone: world.get('dropZone'), input: world.get('fileInput'), status: world.get('uploadStatus')};
  old.button = old.zone.querySelector('button');
  world.swap();
  const zone = world.get('dropZone'), input = world.get('fileInput'), status = world.get('uploadStatus');
  const button = zone.querySelector('button');
  const file = {name: 'auszug' + round + '.csv'};
  input.value = file.name;
  if (via === 'change') { input.files = [file]; world.fire('change', input); }
  else world.fire('drop', zone.children[0], {dataTransfer: {files: [file]}});
  const during = {calls: world.calls.length, blocked: button.disabled, status: status.innerHTML};
  world.respond(); await world.settle();
  results.push({replaced: zone !== old.zone && input !== old.input && button !== old.button && status !== old.status,
                during, after: {blocked: button.disabled, status: status.innerHTML, value: input.value, busy: world.read('uploadBusy')}});
}
return results;
"""


@NO_NODE
@pytest.mark.parametrize('via', ['change', 'drop'], ids=['Datei-gewählt', 'Datei-abgelegt'])
def test_after_the_swap_the_new_zone_uploads_once_and_only_its_own_button_is_blocked(tmp_path, script, via):
    out = run(tmp_path, script, f'const via = {json.dumps(via)};\n' + AFTER_SWAP)
    first, second = out['result']
    for round_, result in enumerate((first, second), start=1):
        assert result['replaced'] is True                                      # die Attrappe hat die Objekte wirklich ausgetauscht
        assert (result['during']['calls'], result['during']['blocked']) == (round_, True)     # eine Anfrage, der NEUE Knopf gesperrt
        assert 'Lade hoch' in result['during']['status']                       # im NEUEN Statusbereich
        assert result['after'] == {'blocked': False, 'status': '', 'value': '', 'busy': False}   # danach frei, Dateifeld leer
    assert out['calls'] == uploads('auszug1.csv', 'auszug2.csv')
    assert out['messages'] == []


DURING_UPLOAD = r"""
const input = world.get('fileInput');
input.files = [{name: 'erste.csv'}];
world.fire('change', input);                                      // der Upload läuft ...
world.swap();                                                     // ... und die Maske wechselt: neue Ablage, ihr Knopf ist noch frei
const input2 = world.get('fileInput'), button2 = world.get('dropZone').querySelector('button');
input2.files = [{name: 'zweite.csv'}];
world.fire('change', input2);                                     // ein zweites Auswählen, solange der erste Upload läuft
const meanwhile = {calls: world.calls.length, messages: world.messages.slice()};
world.respond(); await world.settle();                            // die Antwort kommt
const after = {busy: world.read('uploadBusy'), blocked: button2.disabled, status: world.get('uploadStatus').innerHTML};
input2.files = [{name: 'dritte.csv'}];
world.fire('change', input2);                                     // jetzt geht der nächste
return {meanwhile, after, next: world.calls.length};
"""


@NO_NODE
def test_a_swap_during_an_upload_keeps_the_guard_against_a_second_one_and_frees_everything_afterwards(tmp_path, script):
    out = run(tmp_path, script, DURING_UPLOAD)
    assert out['result'] == {'meanwhile': {'calls': 1, 'messages': [BUSY_MESSAGE]},         # das zweite wird abgewiesen, nicht doppelt hochgeladen
                             'after': {'busy': False, 'blocked': False, 'status': ''}, 'next': 2}
    assert out['calls'] == uploads('erste.csv', 'dritte.csv')


ZONE_GONE_DURING_UPLOAD = r"""
const input = world.get('fileInput');
input.files = [{name: 'erste.csv'}];
world.fire('change', input);                                      // der Upload läuft ...
world.swap({zone: false, status: false});                         // ... und die Maske hat weder Ablage noch Dateifeld noch Statusbereich
world.respond(); await world.settle();                            // die Antwort kommt trotzdem
const busy = world.read('uploadBusy');
world.swap();                                                     // später wieder eine Maske mit Ablage
const input2 = world.get('fileInput');
input2.files = [{name: 'zweite.csv'}];
world.fire('change', input2);
return {busy, calls: world.calls.length};
"""


@NO_NODE
def test_removing_the_elements_during_an_upload_throws_nothing_and_later_uploads_still_work(tmp_path, script):
    out = run(tmp_path, script, ZONE_GONE_DURING_UPLOAD)          # ``run`` prüft schon: kein Fehler im Hörer, keine unbehandelte Zusage
    assert out['result'] == {'busy': False, 'calls': 2}
    assert out['calls'] == uploads('erste.csv', 'zweite.csv')


# ── Ziehen über die neue Ablage ──────────────────────────────────────────────
HOVER = r"""
world.swap();
const zone = world.get('dropZone'), inside = zone.children[0], form = world.get('formular');
const classes = () => [...zone.classes];
const drop = {dataTransfer: {files: []}};
const trace = {};
world.fire('dragenter', inside);                    trace.enter = classes();
trace.over = world.fire('dragover', inside).defaultPrevented;
world.fire('dragleave', inside);                    trace.leave = classes();
world.fire('dragenter', zone);                      trace.enterZone = classes();
world.fire('drop', zone, drop);                     trace.dropped = classes();
world.fire('dragenter', form);                      trace.enterBeside = classes();
trace.overBeside = world.fire('dragover', form).defaultPrevented;
trace.dropBeside = world.fire('drop', form, {dataTransfer: {files: [{name: 'daneben.pdf'}]}}).defaultPrevented;
trace.calls = world.calls.length;
return trace;
"""


@NO_NODE
def test_dragging_over_the_new_zone_sets_and_clears_hover_there_and_beside_it_nothing_happens(tmp_path, script):
    out = run(tmp_path, script, HOVER)
    assert out['result'] == {'enter': ['hover'], 'over': True, 'leave': [], 'enterZone': ['hover'], 'dropped': [],
                             'enterBeside': [], 'overBeside': True, 'dropBeside': True, 'calls': 0}


TEXT_NODE = r"""
world.swap();
const inside = world.get('dropZone').children[0];
world.fire('drop', {nodeType: 3, parentElement: inside}, {dataTransfer: {files: [{name: 'umsaetze.pdf'}]}});   // das Ziel ist ein Textknoten
return world.calls.length;
"""


@NO_NODE
def test_a_text_node_as_the_target_inside_the_zone_counts_as_the_zone(tmp_path, script):
    out = run(tmp_path, script, TEXT_NODE)
    assert out['result'] == 1 and out['calls'] == uploads('umsaetze.pdf')


# ── Ohne die Elemente (Demo-Modus) ───────────────────────────────────────────
WITHOUT_ZONE = r"""
const targets = [world.body, world.get('formular'), world.get('amount'), world.get('uploadStatus')];
for (const target of targets) {
  target.files = [{name: 'auszug.csv'}];
  world.fire('change', target);
  for (const type of ['dragenter', 'dragover', 'dragleave', 'drop']) world.fire(type, target, {dataTransfer: {files: [{name: 'auszug.csv'}]}});
}
world.swap();                                                      // auch nach einem Austausch bleibt es dabei
for (const target of [world.body, world.get('formular')]) {
  world.fire('change', target);
  world.fire('drop', target, {dataTransfer: {files: [{name: 'auszug.csv'}]}});
}
return {zone: world.get('dropZone'), input: world.get('fileInput')};
"""


@NO_NODE
def test_without_the_drop_zone_nothing_is_sent_and_nothing_throws(tmp_path, demo_script):
    out = run(tmp_path, demo_script, WITHOUT_ZONE, variant='without')
    assert out['result'] == {'zone': None, 'input': None}                  # die Attrappe hat wie die Demo-Seite keine Ablage
    assert out['calls'] == [] and out['messages'] == []


# ── Text-Wächter (auch ohne Node) ────────────────────────────────────────────
def test_the_page_script_holds_no_element_of_the_drop_zone_and_hangs_no_listener_on_one(script):
    """Eine Modulkonstante wie ``const dropZone`` wäre nach dem Austausch des Bereichs ein abgehängter Knoten, ein Hörer an einem solchen Element
    ginge verloren – beides war der Fehler. Verboten ist jede Deklaration dieser drei Namen, auch eine lokale; ``dropZoneOf`` ist eine Funktion
    und kein Element und trifft das nicht."""
    for name in ('dropZone', 'fileInput', 'uploadStatus'):
        assert f'{name}.addEventListener' not in script, name
        assert not re.search(rf'\b(?:const|let|var)\s+{name}\b', script), name
