# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Rechnung zurückholen, deren Forderung schon in einem DATEV-Export stand: erst nach Rückfrage.

Das Zurückholen (versendet/überfällig → Entwurf/abgeschlossen) nimmt die Forderungsbuchung mit. Stand sie in einem DATEV-Stapel (``BookingExports``), bleibt sie dort stehen – die
Buchhaltung des Steuerbüros kennt sie dann, das Programm nicht mehr. Deshalb fragt ``Database.set_invoice_status`` zuerst nach: ``409`` mit dem Klartext ``REOPEN_EXPORTED_QUESTION`` und
``confirm == 'exported'``, nichts ist geändert. Erst mit ``confirm_exported: true`` geschieht es wie sonst. Es ist keine Sperre und ändert nichts, wo es keinen Export gab; die
bisherigen Ablehnungen (``REOPEN_*``: Import, Bankbewegung, fremde Forderung, Zahlungssätze) gehen der Rückfrage vor. ``confirm_exported`` ist ein Wahrheitswert, sonst ist die Anfrage ungültig.

Die Seite der Rechnung (Node, ``vm`` ohne Browser): ``setInvoiceStatus`` schickt ``confirm_exported`` und fragt bei der Antwort mit ``confirm`` über ``appConfirm`` nach.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import contextlib
import json
import re
import shutil
import sqlite3
import subprocess

import pytest

import server.handlers as handlers
from db.invoice_lock import (REOPEN_ATTACHED_MESSAGE, REOPEN_EXPORTED_QUESTION, REOPEN_FOREIGN_MESSAGE, REOPEN_IMPORTED_MESSAGE,
                             REOPEN_PAYMENT_MESSAGE, StatusChange)
from server.pages_invoice import PageInvoice
from tests.datev_helpers import confirmed_export
from tests.document_helpers import read_file, state
from tests.invoice_world import build_world, columns, make_invoice
from tests.test_invoice_status_receivable import attach_to_bank, hand_booked, mark_imported, receivables, sql

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die Prüfungen des Quelltexts decken das Wichtigste ab')
YEAR = ('2026-01-01', '2026-12-31')
JUNE = ('2026-06-01', '2026-06-30')


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return build_world(tmp_db)


def snapshot(db):
    """Alles, was das Zurückholen schreiben kann – samt der Merkmale „exportiert“, dem Protokoll und den abgelegten Fassungen."""
    rows = state(db, 'Invoices', 'InvoiceItems', 'InvoicePayments', 'Bookings', 'NumberRanges', 'InvoiceCorrections', 'InvoiceDocumentVersions')
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        rows['BookingExports'] = con.execute('SELECT * FROM BookingExports ORDER BY Booking_ID').fetchall()
    return rows


def request_status(invoice_id, status, **extra):
    code, body = handlers.handle_update_invoice_status(json.dumps({'invoice_id': invoice_id, 'status': status, **extra}).encode())
    return code, json.loads(body)


def send(w, number='26R100', **over):
    invoice_id = make_invoice(w, 'draft', number=number, **over)
    code, body = request_status(invoice_id, 'sent')
    assert (code, body['success']) == (200, True), body
    return invoice_id


def export(w, period=YEAR):
    result = confirmed_export(w.db, *period)
    assert isinstance(result[0], bytes), result


def exported_ids(w):
    with contextlib.closing(sqlite3.connect(w.db.db_name)) as con:
        return {row[0] for row in con.execute('SELECT Booking_ID FROM BookingExports')}


def sent_and_exported(w, number='26R100', **over):
    invoice_id = send(w, number, **over)
    export(w)
    assert {row[0] for row in receivables(w, invoice_id)} <= exported_ids(w) and receivables(w, invoice_id)
    return invoice_id


def asked(w, invoice_id, status='draft'):
    """Das Zurückholen ohne Bestätigung: die Antwort, und nichts hat sich geändert (Datenbank, Dokumente)."""
    before = snapshot(w.db)
    files = {path: read_file(path) for path in (columns(w.db, invoice_id)['PDFPath'], columns(w.db, invoice_id)['XMLPath']) if path}
    code, body = request_status(invoice_id, status)
    assert snapshot(w.db) == before
    assert files == {path: read_file(path) for path in files}
    return code, body


# ── die Rückfrage ────────────────────────────────────────────────────────────

def test_reopening_an_exported_invoice_asks_first_and_changes_nothing(world):
    invoice_id = sent_and_exported(world)
    code, body = asked(world, invoice_id)
    assert (code, body) == (409, {'success': False, 'error': REOPEN_EXPORTED_QUESTION, 'confirm': 'exported'})
    assert columns(world.db, invoice_id)['Status'] == 'sent' and len(receivables(world, invoice_id)) == 1


@pytest.mark.parametrize('status', ['draft', 'finalized'])
def test_both_targets_of_reopening_ask(world, status):
    invoice_id = sent_and_exported(world)
    assert asked(world, invoice_id, status)[1]['confirm'] == 'exported'


def test_an_overdue_invoice_asks_too(world):
    invoice_id = sent_and_exported(world)
    assert request_status(invoice_id, 'overdue')[0] == 200
    assert asked(world, invoice_id)[1] == {'success': False, 'error': REOPEN_EXPORTED_QUESTION, 'confirm': 'exported'}


def test_the_question_names_what_happens_and_what_stays(world):
    assert 'DATEV' in REOPEN_EXPORTED_QUESTION and 'entfernt' in REOPEN_EXPORTED_QUESTION and 'stehen' in REOPEN_EXPORTED_QUESTION
    assert REOPEN_EXPORTED_QUESTION.endswith('Trotzdem zurückholen?')


def test_with_the_confirmation_it_reopens_as_it_does_without_an_export(world):
    """Dieselbe Rechnung einmal mit, einmal ohne Export: mit ``confirm_exported: true`` kommt dasselbe heraus – Status, Forderung, abgelegte Fassung, Antwort."""
    exported = sent_and_exported(world, '26R100')
    plain = send(world, '26R101')
    code, body = request_status(exported, 'draft', confirm_exported=True)
    plain_code, plain_body = request_status(plain, 'draft')
    assert (code, plain_code) == (200, 200)
    assert {k: v for k, v in body.items() if k != 'invoice_id'} == {k: v for k, v in plain_body.items() if k != 'invoice_id'}
    assert body['success'] is True and 'Fassungen' in body['hint']
    for invoice_id in (exported, plain):
        stored = columns(world.db, invoice_id)
        assert (stored['Status'], stored['PDFPath'], stored['XMLPath'], stored['DocumentVersion']) == ('draft', None, None, 2)
        assert receivables(world, invoice_id) == []
    assert exported_ids(world) == set()                                       # mit der Forderung ist auch ihr Merkmal weg


def test_declining_keeps_the_way_open_to_confirm_later(world):
    invoice_id = sent_and_exported(world)
    assert asked(world, invoice_id)[0] == 409
    assert asked(world, invoice_id)[0] == 409                                  # nichts verbraucht
    assert request_status(invoice_id, 'draft', confirm_exported=True)[0] == 200


@pytest.mark.parametrize('value', ['1', 1, 'true', None, [], {}, 0, ''], ids=repr)
def test_confirm_exported_must_be_a_boolean(world, value):
    exported = sent_and_exported(world, '26R100')
    plain = send(world, '26R101')
    before = snapshot(world.db)
    for invoice_id in (exported, plain):
        with pytest.raises(handlers.InvalidRequest):
            request_status(invoice_id, 'draft', confirm_exported=value)
    assert snapshot(world.db) == before


def test_false_is_the_same_as_leaving_the_field_out(world):
    invoice_id = sent_and_exported(world)
    assert asked(world, invoice_id)[1] == request_status(invoice_id, 'draft', confirm_exported=False)[1]


def test_a_status_change_names_confirm_only_when_it_asks():
    """Die Schnappschüsse (``tests/single_rate_golden_2_6_6.json``) zeichnen ein Ergebnis über ``_asdict()`` auf: Ohne Rückfrage hat es genau die Felder von früher."""
    plain = StatusChange(True, None, 200, 'draft', 'sent', 'send')
    assert plain.confirm is None and tuple(plain._asdict()) == ('ok', 'message', 'code', 'old', 'new', 'kind', 'archived', 'moved', 'payment_hint')
    question = StatusChange(False, REOPEN_EXPORTED_QUESTION, 409, 'sent', 'draft', None, confirm='exported')
    assert question._asdict()['confirm'] == 'exported' and question.confirm == 'exported' and len(question) == 10


# ── ohne Export keine Rückfrage ──────────────────────────────────────────────

def test_without_an_export_there_is_no_question(world):
    invoice_id = send(world)
    assert exported_ids(world) == set()
    assert request_status(invoice_id, 'draft') == (200, {'success': True, 'invoice_id': invoice_id,
                                                       'hint': 'Die versendeten Dokumente (PDF/XML) liegen jetzt im Ordner „Fassungen“ dieser Rechnung und bleiben dort erhalten.'})
    assert receivables(world, invoice_id) == []


def test_a_confirmation_where_nothing_was_exported_changes_nothing(world):
    invoice_id = send(world)
    assert request_status(invoice_id, 'draft', confirm_exported=True)[0] == 200


def test_only_the_export_of_its_own_receivable_counts(world):
    """Zwei Rechnungen, ein Export nur für den Juni: die Rechnung vom August wird ohne Rückfrage zurückgeholt, die vom Juni nicht."""
    june = send(world, '26R100')
    august = send(world, '26R101', invoiceDate='2026-08-07', dueDate='2026-08-21')
    export(world, JUNE)
    assert exported_ids(world) == {row[0] for row in receivables(world, june)}
    assert request_status(august, 'draft')[0] == 200
    assert asked(world, june)[1]['confirm'] == 'exported'


def test_other_marks_do_not_matter_only_the_receivable_does(world):
    """Ein anderer Satz der Rechnung mit dem Merkmal (ein Zahlungssatz bliebe ohnehin vor) ändert nichts an der Frage: gefragt wird nach der Forderung, die verschwindet."""
    invoice_id = send(world)
    other = world.db.insert_booking('2026-06-08', 10.0, booking_type='entry', document_number='26R100', text='Anderer Satz')
    sql(world, 'INSERT INTO BookingExports (Booking_ID, ExportedAt) VALUES (?, CURRENT_TIMESTAMP)', other)
    assert request_status(invoice_id, 'draft')[0] == 200


# ── die bisherigen Ablehnungen gehen vor ─────────────────────────────────────

@pytest.mark.parametrize('prepare, message', [
    pytest.param(lambda w, invoice_id, booking: mark_imported(w, booking), REOPEN_IMPORTED_MESSAGE, id='Import'),
    pytest.param(lambda w, invoice_id, booking: attach_to_bank(w, booking), REOPEN_ATTACHED_MESSAGE, id='Bankbewegung'),
    pytest.param(lambda w, invoice_id, booking: hand_booked(w, booking), REOPEN_FOREIGN_MESSAGE, id='von-Hand-gebucht'),
])
def test_the_refusals_of_reopening_come_before_the_question(world, prepare, message):
    invoice_id = sent_and_exported(world)
    prepare(world, invoice_id, receivables(world, invoice_id)[0][0])
    assert exported_ids(world)                                                   # die Forderung trägt das Merkmal
    code, body = asked(world, invoice_id)
    assert (code, body) == (409, {'success': False, 'error': message})          # der alte Klartext, keine Rückfrage
    assert 'confirm' not in body


def test_leftover_payment_bookings_refuse_before_the_question(world):
    invoice_id = sent_and_exported(world)
    leftover = world.db.insert_booking('2026-07-01', 100.0, booking_type='entry', document_number='26R100', text='Zahlung')
    sql(world, "UPDATE Bookings SET Status = 'invoice_payment' WHERE ID = ?", leftover)
    code, body = asked(world, invoice_id)
    assert (code, body) == (409, {'success': False, 'error': REOPEN_PAYMENT_MESSAGE})


def test_a_refusal_stays_a_refusal_even_with_the_confirmation(world):
    invoice_id = sent_and_exported(world)
    mark_imported(world, receivables(world, invoice_id)[0][0])
    assert request_status(invoice_id, 'draft', confirm_exported=True) == (409, {'success': False, 'error': REOPEN_IMPORTED_MESSAGE})


def test_the_other_status_changes_never_ask(world):
    """Nur das Zurückholen fragt: ein anderer Wechsel (hier versendet → überfällig und zurück) bleibt, wie er ist."""
    invoice_id = sent_and_exported(world)
    assert request_status(invoice_id, 'overdue')[0] == 200 and request_status(invoice_id, 'sent')[0] == 200
    assert columns(world.db, invoice_id)['Status'] == 'sent' and exported_ids(world)


# ── die Seite ────────────────────────────────────────────────────────────────

def _status_script(db, invoice_id):
    html = PageInvoice(db, {}, invoice_id)
    (script,) = [s for s in re.findall(r'<script>(.*?)</script>', html, re.S) if 'function setInvoiceStatus(' in s]
    return script


def test_the_page_sends_confirm_exported_and_asks_on_the_answer_with_confirm(world):
    invoice_id = send(world)
    script = _status_script(world.db, invoice_id)
    assert 'confirm_exported: confirmed === true' in script and "data.confirm === 'exported'" in script
    assert 'appConfirm(data.error, function() { change(true); })' in script
    assert 'var change = function(confirmed)' in script
    assert 'appConfirm(einvoiceSendQuestion, change)' in script               # das Versenden fragt wie bisher nach der E-Rechnung


STATUS_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, scenarioFile] = process.argv;
const scenario = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));
const calls = [], confirms = [], messages = [];
let reloads = 0;
const sandbox = {
  document: {getElementById: id => ({value: scenario.status})},
  fetch: (url, init) => {
    calls.push({url, body: JSON.parse(init.body)});
    const answer = scenario.answers[calls.length - 1];
    return Promise.resolve({json: () => Promise.resolve(answer)});
  },
  appConfirm: (text, onOk) => { confirms.push(text); if (scenario.accept) onOk(undefined); },
  showMessage: (text, kind) => messages.push([text, kind]),
  location: {reload: () => { reloads++; }},
  setTimeout: (fn) => 0,
  JSON, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);
vm.runInContext('setInvoiceStatus(7)', sandbox);
setTimeout(() => { console.log(JSON.stringify({calls, confirms, messages, reloads})); }, 80);
"""


def _run_status(tmp_path, script, status, answers, accept=True):
    harness, source, data = (tmp_path / name for name in ('status_harness.js', 'status.js', 'status_scenario.json'))
    harness.write_text(STATUS_HARNESS, encoding='utf-8')
    source.write_text(script, encoding='utf-8')
    data.write_text(json.dumps({'status': status, 'answers': answers, 'accept': accept}), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(source), str(data)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


QUESTION = {'success': False, 'error': REOPEN_EXPORTED_QUESTION, 'confirm': 'exported'}


@NO_NODE
def test_confirming_the_question_sends_the_request_again_with_confirm_exported(tmp_path, world):
    script = _status_script(world.db, send(world))
    out = _run_status(tmp_path, script, 'draft', [QUESTION, {'success': True, 'invoice_id': 7}])
    assert [call['body'] for call in out['calls']] == [{'invoice_id': 7, 'status': 'draft', 'confirm_exported': False},
                                                       {'invoice_id': 7, 'status': 'draft', 'confirm_exported': True}]
    assert out['confirms'] == [REOPEN_EXPORTED_QUESTION] and out['reloads'] == 1 and out['messages'] == []


@NO_NODE
def test_declining_the_question_sends_nothing_more_and_shows_no_error(tmp_path, world):
    script = _status_script(world.db, send(world))
    out = _run_status(tmp_path, script, 'draft', [QUESTION], accept=False)
    assert len(out['calls']) == 1 and out['confirms'] == [REOPEN_EXPORTED_QUESTION] and out['reloads'] == 0 and out['messages'] == []


@NO_NODE
def test_a_refusal_without_confirm_is_shown_as_an_error_as_before(tmp_path, world):
    script = _status_script(world.db, send(world))
    out = _run_status(tmp_path, script, 'draft', [{'success': False, 'error': REOPEN_IMPORTED_MESSAGE}])
    assert out['confirms'] == [] and out['reloads'] == 0 and out['messages'] == [['Fehler: ' + REOPEN_IMPORTED_MESSAGE, 'error']]


@NO_NODE
def test_a_status_change_without_a_question_sends_confirm_exported_false_once(tmp_path, world):
    script = _status_script(world.db, send(world))
    out = _run_status(tmp_path, script, 'overdue', [{'success': True, 'invoice_id': 7}])
    assert [call['body'] for call in out['calls']] == [{'invoice_id': 7, 'status': 'overdue', 'confirm_exported': False}]
    assert out['confirms'] == [] and out['reloads'] == 1
