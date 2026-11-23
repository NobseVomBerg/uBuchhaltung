# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Buchungssatz-Editor der Bankbewegung: Verwendungszweck und Beleg-Nr. der Bankbewegung im Skriptblock (2.8.1).

``SPLIT_BANK_DOCNR`` und ``SPLIT_BANK_TEXT`` stehen als JSON (``pages.script_json``) im Skriptblock des Editors – nie nur maskiert in Anführungszeichen. Ein Zeilenumbruch im
Verwendungszweck (der Import eines PDF-Auszugs verbindet dessen Zeilen damit) oder ein Backslash am Ende beendete sonst die Zeichenkette, und der ganze Block des Editors fiel
mit einem Syntaxfehler aus (weder „+ Teilbuchung“ noch die Summenzeile liefen); ein ``&`` stand als ``&amp;`` in der neuen Zeile.

Geprüft wird, was die Seite baut und was das Skript damit tut:

* die Maske lässt sich bauen (``PageTransactions``, und über den Server: ``GET /transactions/edit?id=``), der Aufbau der Seite hängt nicht vom Wert ab (kein Element mehr, kein
  Skriptblock mehr), und kein ``</script>`` aus dem Wert beendet den Block des Editors;
* der Skriptblock ist gültiges JavaScript (``node --check``);
* die beiden Variablen tragen genau den gespeicherten Text (das Skript läuft in Node, mit einer kleinen Attrappe der Seite), und „+ Teilbuchung“ (``splitAdd``) legt eine Zeile
  mit genau diesem Verwendungszweck und der Beleg-Nr. mit Buchstabe an.

Alle Werte sind erfundene Testdaten.
"""
import json
import shutil
import sqlite3
import subprocess
from contextlib import closing

import pytest

from db import Database
from server.pages import script_json
from server.pages_transactions import PageTransactions
from tests.dom_helpers import Dom, assert_same_structure
from tests.http_client import get

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – nur die Läufe des Skripts entfallen')

# (Verwendungszweck, Beleg-Nr.) – was in einer Bankbewegung stehen kann
CASES = {
    'Zeilenumbruch': ('Zeile eins\nZeile zwei', 'B-1\nB-2'),
    'Windows-Zeilenumbruch': ('Zeile eins\r\nZeile zwei', 'B-1\r\nB-2'),
    'Backslash-am-Ende': ('Ende\\', 'B-1\\'),
    'Backslash-mitten': ('C:\\Pfad\\datei und \\n kein Umbruch', 'B\\n1'),
    'Skriptende': ('</script><b>fett</b>', '</script><i>'),
    'Kaufmannsund': ('A & B', 'A & B'),
    'Entität-als-Text': ('Fish &amp; Chips &lt;b&gt;', '&quot;1&quot;'),
    'Anführungszeichen': ('Er sagte "Hallo" und \'Tschüss\' und `mehr`', '"1"\'2\''),
    'Zeilentrenner': ('Vor\u2028Nach\u2029Ende', 'B\u20281'),
    'Kommentar': ('<!-- Kommentar', '-->'),
    'Umlaute': ('Größe 10 € für Müller – ä ö ü ß 日本', 'Ä-1'),
    'Platzhalter': ('${x} {text} {bank_text} %s __TAX_PCT__', '{bank_docnr}'),
    'leer': ('', ''),
    'alles-zusammen': ('Zeile\n</script>\\ & "x" \'y\' <!--', '</script>\\\n&'),
}


def bank_booking(db, text, docnumber, amount=119.00):
    """Eine Bankbewegung (Zahlungseingang) mit diesem Verwendungszweck und dieser Beleg-Nr.; das Konto gibt es je Datenbank nur einmal."""
    if not [a for a in db.fetch_accounts() if a[1] == 'Editor-Bank']:
        db.insert_account('Editor-Bank', 'Ich', 'DE00EDITOR', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    account = [a for a in db.fetch_accounts() if a[1] == 'Editor-Bank'][0][0]
    return db.insert_booking('2026-03-01', amount, account_id=account, booking_type='bank', recipient_client='Käufer AG', text=text, document_number=docnumber)


def editor_block(page):
    """Der Skriptblock des Editors aus einer Seite, so wie ein Browser die Blöcke zerlegt (ein ``</script>`` im Wert beendet dort den Block): genau EINER trägt die Variablen,
    und er ist vollständig – bis zur Summenzeile am Ende."""
    blocks = [block for block in Dom(page).scripts if 'SPLIT_BANK_TEXT' in block]
    assert len(blocks) == 1, f'{len(blocks)} Blöcke mit SPLIT_BANK_TEXT'
    block = blocks[0]
    assert 'function splitAdopt()' in block and block.rstrip().endswith('splitRecalc();'), 'der Block des Editors ist abgeschnitten'
    return block


HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
// eine kleine Attrappe des Editors: die Tabelle mit der Vorlagenzeile und die Zeilen, die ``splitAdd`` einfügt
const rows = [];
const table = {insertBefore: row => { rows.push(row); }};
function makeRow() {
  const fields = {'.splitAmount': {value: ''}, '[name=split_docnr]': {value: ''}, '[name=split_text]': {value: ''}};
  return {className: '', style: {}, dataset: {}, parentNode: table, fields, querySelector: selector => fields[selector], cloneNode: () => makeRow()};
}
const template = makeRow();
const summary = {textContent: '', className: ''};
const sandbox = {
  document: {
    querySelector: selector => (selector === '#splitTable tr.splitTemplate' ? template : null),
    querySelectorAll: selector => (selector === '#splitTable tr.splitRow' ? rows : []),
    getElementById: id => (id === 'splitSummary' ? summary : null),
  },
  parseFloat, Math,
};
vm.createContext(sandbox);
vm.runInContext(input.source, sandbox);                       // läuft bis zur Summenzeile am Ende des Blocks
const result = {docnr: sandbox.SPLIT_BANK_DOCNR, text: sandbox.SPLIT_BANK_TEXT, amount: sandbox.SPLIT_BANK_AMOUNT, summary: summary.textContent};
vm.runInContext('splitAdd()', sandbox);                       // „+ Teilbuchung“
const added = rows[0].fields;
result.added = {text: added['[name=split_text]'].value, docnr: added['[name=split_docnr]'].value, amount: added['.splitAmount'].value};
console.log(JSON.stringify(result));
"""


def run_editor(block):
    done = subprocess.run([NODE, '-e', HARNESS], input=json.dumps({'source': block}), capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# ── Die Seite ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('name', list(CASES))
def test_the_mask_is_built_and_the_values_stand_as_json_in_the_block(tmp_db, name):
    text, docnumber = CASES[name]
    booking = bank_booking(tmp_db, text, docnumber)
    page = PageTransactions(tmp_db, edit_transaction_id=booking)
    block = editor_block(page)
    assert f'var SPLIT_BANK_DOCNR = {script_json(docnumber)};' in block
    assert f'var SPLIT_BANK_TEXT = {script_json(text)};' in block
    assert 'var SPLIT_BANK_AMOUNT = 119.0;' in block
    assert '</script' not in block.lower() and '<!--' not in block                      # nichts aus dem Wert kann den Block beenden oder verstecken


def test_the_value_does_not_change_the_structure_of_the_page(tmp_db):
    """Ein ``</script><b>`` im Verwendungszweck und in der Beleg-Nr. fügt kein Element und keinen Skriptblock hinzu."""
    hostile = bank_booking(tmp_db, '</script><b>fett</b>\\', '</script><i>\n&')
    benign_db = Database(db_name=tmp_db.db_name + '.benign')
    benign = bank_booking(benign_db, 'Büromaterial', 'B-1')
    assert_same_structure(PageTransactions(tmp_db, edit_transaction_id=hostile), PageTransactions(benign_db, edit_transaction_id=benign))


# ── Gültiges JavaScript und der gespeicherte Text ────────────────────────────

@NO_NODE
@pytest.mark.parametrize('name', list(CASES))
def test_the_block_of_the_editor_is_valid_javascript(tmp_db, tmp_path, name):
    text, docnumber = CASES[name]
    page = PageTransactions(tmp_db, edit_transaction_id=bank_booking(tmp_db, text, docnumber))
    for index, block in enumerate(Dom(page).scripts):                                   # jeder Block der Seite, nicht nur der des Editors
        source = tmp_path / f'block{index}.js'
        source.write_text(block, encoding='utf-8')
        done = subprocess.run([NODE, '--check', str(source)], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, f'Block {index}: {done.stderr}'


@NO_NODE
@pytest.mark.parametrize('name', list(CASES))
def test_the_variables_carry_exactly_the_stored_text_and_a_new_row_gets_it(tmp_db, name):
    text, docnumber = CASES[name]
    page = PageTransactions(tmp_db, edit_transaction_id=bank_booking(tmp_db, text, docnumber))
    result = run_editor(editor_block(page))
    assert result['text'] == text and result['docnr'] == docnumber and result['amount'] == 119
    assert result['added']['text'] == text                                               # „+ Teilbuchung“ übernimmt den Verwendungszweck unverändert
    assert result['added']['docnr'] == (f'{docnumber}_A' if docnumber else '')           # … und die Beleg-Nr. mit dem Buchstaben der ersten Zeile
    assert result['added']['amount'] == '119.00'
    assert result['summary'] == ''                                                       # die Summenzeile lief (ohne Zeilen bleibt sie leer)


@NO_NODE
def test_a_booking_without_text_and_number_gives_empty_strings(tmp_db):
    """NULL in der Datenbank ist ein leerer Text, nie ``None`` oder ``null`` im Skript."""
    booking = bank_booking(tmp_db, 'x', 'y')
    with closing(sqlite3.connect(tmp_db.db_name)) as conn:
        conn.execute('UPDATE Bookings SET Text = NULL, DocumentNumber = NULL WHERE ID = ?', (booking,))
        conn.commit()
    page = PageTransactions(tmp_db, edit_transaction_id=booking)
    assert 'var SPLIT_BANK_DOCNR = "";' in editor_block(page) and 'var SPLIT_BANK_TEXT = "";' in editor_block(page)
    result = run_editor(editor_block(page))
    assert (result['text'], result['docnr'], result['added']['text'], result['added']['docnr']) == ('', '', '', '')


# ── Über den Server ──────────────────────────────────────────────────────────

@NO_NODE
def test_the_mask_over_the_server_carries_every_value_as_valid_json(server_port, auto_login):
    """``GET /transactions/edit?id=``: Die Maske jeder Bankbewegung wird gebaut (200), der Block des Editors ist vollständig und trägt genau den gespeicherten Text."""
    db = auto_login.db
    bookings = {name: bank_booking(db, *CASES[name]) for name in CASES}
    for name, booking in bookings.items():
        answer = get(server_port, f'/transactions/edit?id={booking}')
        assert answer.status == 200, name
        result = run_editor(editor_block(answer.text))
        assert (result['text'], result['docnr']) == CASES[name], name
