# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Berichtigen einer versendeten Rechnung (2.2.0): neue Fassung und Protokoll.

Bei einer versendeten Rechnung sind Zahlungshinweis, Anschrift, Bestellnummer, Leitweg-ID und Leistungsdatum
änderbar – als **Berichtigung** (``Database.correct_invoice``, der Weg ist weiter ``/invoice/save`` mit dem
Teil-Payload):

* Je geändertem Feld steht eine Zeile im Protokoll (wer, wann, alt, neu, neue Fassung); ein gesperrtes Feld,
  eine veraltete oder fehlende ``documentVersion`` und eine stornierte Rechnung werden mit 409 abgelehnt, nichts
  wird geschrieben. Ohne Abweichung vom Gespeicherten passiert nichts (keine Fassung, kein Protokoll).
* Die abgelegten Dokumente der bisherigen Fassung wandern byte-gleich nach ``Fassungen/<ID>/`` (mit Zeile,
  Pfad und SHA-256 in ``InvoiceDocumentVersions``); scheitert das Verschieben oder der Commit, ist alles wie
  vorher. Danach entsteht das Dokument der neuen Fassung am Namensschema – mit Hinweis (PDF-Zeile und
  „Fassung: n“, CII BT-22, UBL ``cbc:Note``), TypeCode 380 und gleicher Nummer. Scheitert das Erzeugen, bleibt die
  Berichtigung gespeichert und „PDF + E-Rechnung“ erzeugt die fehlende Datei (auch sie trägt den Hinweis).
* Ein Import ohne abgelegtes Dokument bekommt nur Felder und Protokoll. Nach dem Zurückholen und erneuten
  Versenden trägt das Dokument keinen Hinweis mehr.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import datetime
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

import pdfplumber
import pytest
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1, stream_value

import server.handlers as handlers
import userctx
from db.invoice_lock import CHANGED_MESSAGE, CLOSED_MESSAGE, LOCKED_MESSAGE, document_version as version_of
from db.invoice_versions import CORRECTION_ARCHIVE_FAILED_MESSAGE, DELIVERY_DATE_MESSAGE, CorrectionInvalid
from export.einvoice_model import correction_note_text
from server.pages import script_json
from server.pages_invoice import CORRECT_QUESTION, PageInvoice
from tests.document_helpers import read_file, stored_documents, write_file
from tests.dom_helpers import Dom
from tests.invoice_world import (archived_versions, build_world, columns, document_version, finance,
                                 make_invoice, make_payload, protocol, run_sql, save, save_open,
                                 save_raw, set_invoice_status, snapshot)

TODAY = datetime.date.today()
TODAY_DE = TODAY.strftime('%d.%m.%Y')
USER = 'anna'


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user(USER)                       # der angemeldete Benutzer dieser Anfragen
    return build_world(tmp_db)


def sent_invoice(w, number='26R100', **over):
    """Eine Rechnung, versendet wie in der Oberfläche: PDF und XML (die Bestellnummer macht sie zur E-Rechnung)
    der Fassung 1 liegen am Namensschema."""
    invoice_id = make_invoice(w, 'draft', number=number, **over)
    code, body = set_invoice_status(invoice_id, 'sent')
    assert code == 200 and 'warning' not in body, body
    return invoice_id


def versions_dir(invoice_id, year='2026'):
    return os.path.join(userctx.DATA_ROOT, 'invoices', year, 'Fassungen', str(invoice_id))


def archived_files(invoice_id):
    folder = versions_dir(invoice_id)
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


def sha256(path):
    return hashlib.sha256(read_file(path)).hexdigest()


def embedded_xml(pdf_bytes):
    doc = PDFDocument(PDFParser(io.BytesIO(pdf_bytes)))
    spec = resolve1(resolve1(doc.catalog['AF'])[0])
    return stream_value(resolve1(spec['EF'])['F']).get_data()


def root_children(xml_path):
    """Die Elemente direkt unter der Wurzel der UBL-Rechnung (``cbc:Note`` steht auch im Zahlungsziel)."""
    return [child.tag.split('}')[1] for child in ET.fromstring(read_file(xml_path))]


def pdf_text(path):
    with pdfplumber.open(path) as pdf:
        return '\n'.join(page.extract_text() or '' for page in pdf.pages)


NOTICE = 'Berichtigte Fassung vom {day} – ersetzt die Fassung vom {day} (geändert: {fields})'


def notice(fields, day=TODAY_DE):
    return NOTICE.format(day=day, fields=fields)


# ── Je Feld eine Protokollzeile ──────────────────────────────────────────────

OPEN_CHANGES = {
    'Zahlungshinweis': ({'paymentTerms': 'Zahlbar sofort'},
                        [('PaymentTerms', 'Zahlbar in 14 Tagen', 'Zahlbar sofort')]),
    'Bestellnummer': ({'orderNumber': 'B-99'}, [('OrderNumber', 'B-17', 'B-99')]),
    'Leitweg-ID': ({'buyerReference': '991-99999-99'},
                   [('BuyerRouteID', '04011000-1234512345-35', '991-99999-99')]),
    'Leistungsdatum': ({'deliveryDate': '2026-06-01'}, [('DeliveryDate', '2026-06-05', '2026-06-01')]),
    'Bestellnummer-entfernt': ({'orderNumber': None}, [('OrderNumber', 'B-17', None)]),
}


@pytest.mark.parametrize('group', OPEN_CHANGES)
def test_each_open_field_is_one_protocol_row_with_old_and_new_value_user_and_version(world, group):
    invoice_id = make_invoice(world, 'sent')
    locked_before = finance(world.db, invoice_id)
    form, expected = OPEN_CHANGES[group]

    answer = save_open(world, invoice_id, **form)

    assert answer == {'success': True, 'invoice_id': invoice_id, 'documentVersion': 2}
    rows = protocol(world.db, invoice_id, 'correct')
    assert [(r['Field'], r['OldValue'], r['NewValue']) for r in rows] == expected
    for r in rows:
        assert (r['UserName'], r['Version'], r['InvoiceNumber'], r['Action']) == (USER, 2, '26R100', 'correct')
        assert r['ChangedAt'] and r['InvoiceID'] == invoice_id
    assert document_version(world.db, invoice_id) == 2
    assert finance(world.db, invoice_id) == locked_before


def test_several_fields_in_one_request_are_one_new_version_with_a_row_each(world):
    invoice_id = make_invoice(world, 'sent')
    answer = save_open(world, invoice_id, paymentTerms='Zahlbar sofort', orderNumber='B-99',
                       deliveryDate='2026-06-01', buyerReference='991-99999-99')
    assert answer['documentVersion'] == 2 and document_version(world.db, invoice_id) == 2
    rows = protocol(world.db, invoice_id, 'correct')
    assert [r['Field'] for r in rows] == ['PaymentTerms', 'OrderNumber', 'BuyerRouteID', 'DeliveryDate']
    assert {r['Version'] for r in rows} == {2} and len({r['ChangedAt'] for r in rows}) == 1


def test_the_leitweg_id_is_one_protocol_row_although_two_columns_carry_it(world):
    invoice_id = make_invoice(world, 'sent')
    save_open(world, invoice_id, buyerReference='991-99999-99')
    stored = columns(world.db, invoice_id)
    assert stored['BuyerReference'] == stored['BuyerRouteID'] == '991-99999-99'
    assert [r['Field'] for r in protocol(world.db, invoice_id, 'correct')] == ['BuyerRouteID']


def _rename_customer(world):
    world.db.update_contact(world.cust, contact_type='customer', entity_type='company',
                            display_name='Malermeister Pinsel', company_name='Malermeister Pinsel GmbH',
                            customer_number='K-100', street='Pinselallee 7', postal_code='12345',
                            city='Farbstadt', email='info@example.com')


def test_the_address_request_writes_one_row_per_changed_column(world):
    invoice_id = make_invoice(world, 'sent')
    _rename_customer(world)

    answer = save_open(world, invoice_id, refreshAddress=True)

    assert answer['documentVersion'] == 2
    rows = protocol(world.db, invoice_id, 'correct')
    assert [(r['Field'], r['OldValue'], r['NewValue']) for r in rows] == [
        ('BuyerName', 'Malermeister Pinsel', 'Malermeister Pinsel GmbH'),
        ('BuyerCompany', 'Malermeister Pinsel', 'Malermeister Pinsel GmbH'),
        ('BuyerStreet', 'Kundenweg 5', 'Pinselallee 7'),
        ('BuyerPostalCode', '54321', '12345'),
        ('BuyerCity', 'Kundenhausen', 'Farbstadt')]               # das Land blieb DE: keine Zeile


def test_an_address_request_without_a_difference_changes_nothing(world):
    invoice_id = make_invoice(world, 'sent')
    before = snapshot(world.db)
    assert save_open(world, invoice_id, refreshAddress=True) == {'success': True, 'invoice_id': invoice_id}
    assert snapshot(world.db) == before


def test_the_user_is_the_logged_in_user_of_the_request_and_empty_without_one(world):
    first = make_invoice(world, 'sent')
    userctx.clear()
    save_open(world, first, orderNumber='B-1')
    userctx.set_user('Zweite Person')
    second = make_invoice(world, 'sent', number='26R101')
    save_open(world, second, orderNumber='B-2')
    assert protocol(world.db, first, 'correct')[0]['UserName'] is None
    assert protocol(world.db, second, 'correct')[0]['UserName'] == 'Zweite Person'


def test_an_editable_invoice_is_saved_as_before_and_makes_no_version(world):
    invoice_id = make_invoice(world, 'draft')
    payload = make_payload(world, invoiceId=invoice_id, orderNumber='B-99', paymentTerms='Zahlbar sofort')
    assert save(payload) == {'success': True, 'invoice_id': invoice_id}
    assert columns(world.db, invoice_id)['OrderNumber'] == 'B-99'
    assert protocol(world.db, invoice_id) == [] and columns(world.db, invoice_id)['DocumentVersion'] is None


# ── Abgelehnt, ohne Spur ─────────────────────────────────────────────────────

def _price(w, p):
    p['items'][0]['unitPrice'] = 101.0


LOCKED_FORMS = {
    'Rechnungsnummer': lambda w, p: p.update(invoiceNumber='26R999'),
    'Rechnungsdatum': lambda w, p: p.update(invoiceDate='2026-06-08'),
    'Kunde': lambda w, p: p.update(customerId=w.cust2),
    'Preis': _price,
}


@pytest.mark.parametrize('key', LOCKED_FORMS)
def test_a_locked_field_is_refused_and_no_version_is_made(world, key):
    invoice_id = sent_invoice(world)
    before, documents = snapshot(world.db), stored_documents(userctx.DATA_ROOT)
    payload = make_payload(world, invoiceId=invoice_id, documentVersion=1,
                           paymentTerms='Zahlbar sofort', orderNumber='B-99', deliveryDate='2026-06-01')
    LOCKED_FORMS[key](world, payload)                                      # die änderbaren Felder gehen mit unter

    resp = save_raw(payload)

    assert resp.status == 409 and json.loads(resp)['error'].startswith(LOCKED_MESSAGE)
    assert snapshot(world.db) == before                                    # auch kein Protokoll, keine Fassung
    assert stored_documents(userctx.DATA_ROOT) == documents and archived_files(invoice_id) == []


@pytest.mark.parametrize('shape', ['nur-ID-und-Fassung', 'Leerwerte-fuer-NULL', 'gespeicherte-Werte',
                                   'komplettes-Formular'])
def test_an_unchanged_save_makes_no_version_and_no_protocol(world, shape):
    invoice_id = sent_invoice(world)
    before, documents = snapshot(world.db), stored_documents(userctx.DATA_ROOT)
    stored = columns(world.db, invoice_id)
    forms = {
        'nur-ID-und-Fassung': {'invoiceId': invoice_id, 'documentVersion': 1},
        'Leerwerte-fuer-NULL': {'invoiceId': invoice_id, 'documentVersion': 1, 'refreshAddress': False},
        'gespeicherte-Werte': {'invoiceId': invoice_id, 'documentVersion': 1,
                               'paymentTerms': stored['PaymentTerms'], 'orderNumber': stored['OrderNumber'],
                               'buyerReference': stored['BuyerRouteID'], 'deliveryDate': stored['DeliveryDate']},
        'komplettes-Formular': make_payload(world, invoiceId=invoice_id, documentVersion=1),
    }

    assert save(forms[shape]) == {'success': True, 'invoice_id': invoice_id}

    assert snapshot(world.db) == before and stored_documents(userctx.DATA_ROOT) == documents
    assert archived_files(invoice_id) == []


@pytest.mark.parametrize('given', [None, 0, 2, 3, '1.5', 'zwei', [], True])
def test_a_stale_or_missing_document_version_is_refused_and_nothing_is_written(world, given):
    invoice_id = sent_invoice(world)
    before, documents = snapshot(world.db), stored_documents(userctx.DATA_ROOT)
    payload = {'invoiceId': invoice_id, 'orderNumber': 'B-99'}
    if given is not None:
        payload['documentVersion'] = given

    resp = save_raw(payload)

    assert resp.status == 409 and json.loads(resp) == {'success': False, 'error': CHANGED_MESSAGE}
    assert snapshot(world.db) == before and stored_documents(userctx.DATA_ROOT) == documents


def test_the_version_the_form_loaded_is_stale_after_another_correction(world):
    invoice_id = sent_invoice(world)
    assert save_open(world, invoice_id, orderNumber='B-2')['documentVersion'] == 2
    before = snapshot(world.db)
    resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'paymentTerms': 'Zahlbar sofort'})
    assert resp.status == 409 and json.loads(resp)['error'] == CHANGED_MESSAGE
    assert snapshot(world.db) == before
    assert save_open(world, invoice_id, version=2, paymentTerms='Zahlbar sofort')['documentVersion'] == 3


@pytest.mark.parametrize('how', ['credit-note', 'by-hand'])
def test_a_cancelled_invoice_cannot_be_corrected(world, how):
    """Eine stornierte Rechnung – mit Stornorechnung oder (Altbestand) von Hand storniert – ist ganz gesperrt."""
    invoice_id = sent_invoice(world)
    if how == 'credit-note':
        assert world.db.cancel_invoice(invoice_id, '2026-07-01', USER).ok
    else:
        world.db.update_invoice_status(invoice_id, 'cancelled')
    before, documents = snapshot(world.db), stored_documents(userctx.DATA_ROOT)
    resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'})
    assert resp.status == 409 and json.loads(resp)['error'] == CLOSED_MESSAGE
    assert snapshot(world.db) == before and stored_documents(userctx.DATA_ROOT) == documents


def test_the_database_layer_rejects_unknown_fields_and_a_missing_invoice(world):
    invoice_id = sent_invoice(world)
    with pytest.raises(ValueError):
        world.db.correct_invoice(invoice_id, {'status': 'paid'}, 1, USER)
    with pytest.raises(ValueError):
        world.db.correct_invoice(invoice_id, {'pdf_path': '/tmp/x.pdf'}, 1, USER)
    with pytest.raises(handlers.DocumentNotFound):
        world.db.correct_invoice(99999, {'order_number': 'B-99'}, 1, USER)
    assert protocol(world.db, invoice_id, 'correct') == []


def test_a_form_loaded_before_the_invoice_was_taken_back_writes_nothing(world):
    invoice_id = sent_invoice(world)
    assert set_invoice_status(invoice_id, 'draft')[0] == 200
    before = snapshot(world.db)
    resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'})
    assert resp.status == 200                                  # ein Entwurf: das Speichern des Teil-Payloads …
    assert json.loads(resp) == {'success': False, 'error': 'Pflichtfelder fehlen'}   # … gilt dort nicht
    assert snapshot(world.db) == before
    # und das Berichtigen selbst lehnt einen Entwurf ab (die Maske ist veraltet)
    with pytest.raises(handlers.InvoiceLocked) as refused:
        world.db.correct_invoice(invoice_id, {'order_number': 'B-99'}, 1, USER)
    assert str(refused.value) == CHANGED_MESSAGE and snapshot(world.db) == before


# ── Das Leistungsdatum bleibt ein Datum ──────────────────────────────────────

OTHER_CHANGES = {'orderNumber': 'B-99', 'paymentTerms': 'Zahlbar sofort'}
BAD_DELIVERY_DATES = [(None, {}), ('', {}), ('31.12.2026', {}), ('irgendwann', {}), ('2026-13-01', {}),
                      (' 2026-06-01', {}), ('2026-06-01 ', {}), (20260601, {}), (['2026-06-01'], {}),
                      (None, OTHER_CHANGES), ('irgendwann', OTHER_CHANGES)]


@pytest.mark.parametrize('value, others', BAD_DELIVERY_DATES)
def test_a_correction_cannot_empty_the_delivery_date_or_fill_it_with_text(world, value, others):
    """Eine Rechnung braucht ihr Leistungsdatum (§ 14 UStG): Die Berichtigung lehnt ab (400), bevor sie ein
    Dokument verschiebt – auch die übrigen Änderungen der Anfrage werden nicht geschrieben."""
    invoice_id = sent_invoice(world)
    before, documents = snapshot(world.db), stored_documents(userctx.DATA_ROOT)

    resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'deliveryDate': value, **others})

    assert resp.status == 400 and json.loads(resp) == {'success': False, 'error': DELIVERY_DATE_MESSAGE}
    assert snapshot(world.db) == before and stored_documents(userctx.DATA_ROOT) == documents
    assert archived_files(invoice_id) == [] and not os.path.exists(versions_dir(invoice_id))


def test_a_valid_iso_date_is_a_correction_and_the_unchanged_date_is_none(world):
    invoice_id = sent_invoice(world)
    before = snapshot(world.db)
    assert save_open(world, invoice_id, deliveryDate='2026-06-05') == {'success': True, 'invoice_id': invoice_id}
    assert snapshot(world.db) == before                                    # unverändert: keine Fassung
    assert save_open(world, invoice_id, deliveryDate='2026-06-01')['documentVersion'] == 2
    assert columns(world.db, invoice_id)['DeliveryDate'] == '2026-06-01'


@pytest.mark.parametrize('stored', [None, ''])
def test_an_imported_invoice_without_a_delivery_date_can_still_be_corrected_in_the_other_fields(world, stored):
    """Was die Maske unverändert zurückschickt – auch ein leeres Leistungsdatum –, ist kein Verstoß."""
    invoice_id = make_invoice(world, 'sent')
    run_sql(world.db, 'UPDATE Invoices SET DeliveryDate = ? WHERE ID = ?', stored, invoice_id)
    for shown in (None, ''):
        before = snapshot(world.db)
        assert save_open(world, invoice_id, deliveryDate=shown) == {'success': True, 'invoice_id': invoice_id}
        assert snapshot(world.db) == before

    answer = save_open(world, invoice_id, orderNumber='B-99', deliveryDate=None)

    assert answer == {'success': True, 'invoice_id': invoice_id, 'documentVersion': 2}
    row = columns(world.db, invoice_id)
    assert row['OrderNumber'] == 'B-99' and not row['DeliveryDate']
    resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 2, 'deliveryDate': 'irgendwann'})
    assert resp.status == 400                                              # Text statt eines Datums geht auch hier nicht
    assert save_open(world, invoice_id, deliveryDate='2026-06-01')['documentVersion'] == 3    # ein Datum lässt sich nachtragen


def test_a_stored_delivery_date_that_is_not_iso_is_no_violation_while_it_stays(world):
    invoice_id = make_invoice(world, 'sent')
    run_sql(world.db, "UPDATE Invoices SET DeliveryDate = '31.12.2026' WHERE ID = ?", invoice_id)

    answer = save_open(world, invoice_id, deliveryDate='31.12.2026', orderNumber='B-99')

    assert answer['documentVersion'] == 2 and columns(world.db, invoice_id)['DeliveryDate'] == '31.12.2026'


def test_the_database_layer_refuses_an_invalid_delivery_date_before_it_moves_anything(world):
    invoice_id = sent_invoice(world)
    before, documents = snapshot(world.db), stored_documents(userctx.DATA_ROOT)
    with pytest.raises(CorrectionInvalid) as refused:
        world.db.correct_invoice(invoice_id, {'delivery_date': None, 'order_number': 'B-99'}, 1, USER)
    assert str(refused.value) == DELIVERY_DATE_MESSAGE and isinstance(refused.value, ValueError)
    assert snapshot(world.db) == before and stored_documents(userctx.DATA_ROOT) == documents
    assert archived_files(invoice_id) == []


# ── Die Maske: ein geleertes Leistungsdatum wird nicht abgeschickt ───────────

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die statische Prüfung '
                                                  '(test_the_form_checks_the_delivery_date_before_it_asks) deckt es ab')


def _form_script(world, invoice_id):
    return next(s for s in Dom(PageInvoice(world.db, {}, invoice_id)).scripts if 'function saveInvoice()' in s)


def test_the_form_checks_the_delivery_date_before_it_asks_and_names_the_same_text(world):
    script = _form_script(world, sent_invoice(world))
    guard = script.index("if ('deliveryDate' in changes && !changes.deliveryDate)")
    assert guard < script.index(f'showConfirm({script_json(CORRECT_QUESTION)}')       # vor der Rückfrage „Berichtigen“
    assert f'showMessage({script_json(DELIVERY_DATE_MESSAGE)}, \'error\')' in script
    assert '__CORRECT_QUESTION__' not in script and '__DELIVERY_DATE_MESSAGE__' not in script


def _run_save_invoice(world, invoice_id, tmp_path, fields, initial):
    """``saveInvoice()`` der Seite einer versendeten Rechnung in Node ausführen, mit Attrappen für das DOM und die
    Meldungen: liefert, was gemeldet, gefragt und abgeschickt wurde."""
    script = _form_script(world, invoice_id)
    start = script.index('function saveInvoice()')
    source = tmp_path / 'save_invoice.js'
    source.write_text(script[start:script.index('// Send to server', start)], encoding='utf-8')
    runner = tmp_path / 'run.js'
    runner.write_text(
        "const fs = require('fs'), vm = require('vm');\n"
        "const scenario = JSON.parse(process.argv[3]);\n"
        "const log = {messages: [], asked: [], sent: [], focused: []};\n"
        "const elements = {};\n"
        "for (const [id, value] of Object.entries(scenario.fields)) {\n"
        "  elements[id] = {value: value, focus() { log.focused.push(id); }};\n"
        "}\n"
        "const sandbox = {document: {getElementById: id => elements[id] || null}, Object, parseInt,\n"
        "  initialOpenFields: scenario.initial,\n"
        "  showMessage: (text, type) => log.messages.push([text, type]),\n"
        "  showConfirm: (text, ok) => { log.asked.push(text); ok(); },\n"
        "  sendInvoice: changes => log.sent.push(changes)};\n"
        "vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8') + '\\nsaveInvoice();', sandbox);\n"
        "console.log(JSON.stringify(log));\n", encoding='utf-8')
    base = {'invoice_id': str(invoice_id), 'invoice_locked': 'true', 'document_version': '1', 'payment_terms': 'P',
            'order_number': '', 'buyer_route_id': '', 'delivery_date': ''}
    done = subprocess.run([NODE, str(runner), str(source), json.dumps({'fields': {**base, **fields},
                                                                         'initial': initial})],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@NO_NODE
def test_the_form_does_not_send_a_cleared_delivery_date(world, tmp_path):
    invoice_id = sent_invoice(world)
    initial = {'paymentTerms': 'P', 'orderNumber': 'B-17', 'buyerReference': '', 'deliveryDate': '2026-06-05'}

    log = _run_save_invoice(world, invoice_id, tmp_path, {'order_number': 'B-99', 'delivery_date': ''}, initial)

    assert log['messages'] == [[DELIVERY_DATE_MESSAGE, 'error']]
    assert log['asked'] == [] and log['sent'] == [] and log['focused'] == ['delivery_date']


@NO_NODE
def test_the_form_sends_an_unchanged_empty_delivery_date_of_an_import_no_more_and_a_new_date(world, tmp_path):
    invoice_id = sent_invoice(world)
    imported = {'paymentTerms': 'P', 'orderNumber': '', 'buyerReference': '', 'deliveryDate': ''}

    log = _run_save_invoice(world, invoice_id, tmp_path, {'order_number': 'B-99'}, imported)

    assert log['messages'] == [] and log['asked'] == [CORRECT_QUESTION]            # nur die Bestellnummer ist geändert
    assert log['sent'] == [{'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'}]

    log = _run_save_invoice(world, invoice_id, tmp_path, {'delivery_date': '2026-06-01'}, imported)

    assert log['messages'] == [] and log['sent'] == [
        {'invoiceId': invoice_id, 'documentVersion': 1, 'deliveryDate': '2026-06-01'}]


# ── Die Nummer der Fassung ist immer eine Zahl ───────────────────────────────

@pytest.mark.parametrize('stored, expected', [(None, 1), (0, 1), (-3, 1), (True, 1), (2.5, 1), ('abc', 1),
                                              ('2"><img src=x onerror=alert(1)>', 1), (1, 1), (2, 2), (17, 17)])
def test_only_a_positive_whole_number_counts_as_the_version(stored, expected):
    assert version_of({'DocumentVersion': stored}) == expected
    assert version_of({}) == 1


def test_a_version_that_is_no_number_counts_as_version_1_for_the_notice_and_the_documents(world):
    from export.invoice_files import generate_invoice_documents
    invoice_id = sent_invoice(world)
    run_sql(world.db, 'UPDATE Invoices SET DocumentVersion = ? WHERE ID = ?', 'abc', invoice_id)

    assert world.db.correction_notice(invoice_id) is None
    result = generate_invoice_documents(world.db, invoice_id, overwrite_sent=True)

    assert result['error'] is None
    page = pdf_text(result['pdf_path'])
    assert 'Fassung:' not in page and 'Berichtigte Fassung' not in page


# ── Die Dokumente der bisherigen Fassung bleiben ─────────────────────────────

def test_the_sent_documents_move_byte_for_byte_with_their_hash_and_a_row(world):
    invoice_id = sent_invoice(world)
    cols = columns(world.db, invoice_id)
    pdf, xml = cols['PDFPath'], cols['XMLPath']
    sent_pdf, sent_xml = read_file(pdf), read_file(xml)
    stem = os.path.splitext(os.path.basename(pdf))[0]

    save_open(world, invoice_id, deliveryDate='2026-06-01')

    names = [f'{stem} – versendet bis {TODAY.isoformat()}.pdf', f'{stem} – versendet bis {TODAY.isoformat()}.xml']
    assert archived_files(invoice_id) == sorted(names)
    assert read_file(os.path.join(versions_dir(invoice_id), names[0])) == sent_pdf
    assert read_file(os.path.join(versions_dir(invoice_id), names[1])) == sent_xml
    rows = archived_versions(world.db, invoice_id)
    assert [(r['Version'], r['Kind'], r['Reason']) for r in rows] == [(1, 'pdf', 'corrected'),
                                                                      (1, 'xml', 'corrected')]
    by_kind = {r['Kind']: r for r in rows}
    assert by_kind['pdf']['Sha256'] == hashlib.sha256(sent_pdf).hexdigest()
    assert by_kind['xml']['Sha256'] == hashlib.sha256(sent_xml).hexdigest()
    for kind, name in (('pdf', names[0]), ('xml', names[1])):
        assert by_kind[kind]['RelPath'] == f'2026/Fassungen/{invoice_id}/{name}'     # relativ zu invoices/, mit /
        assert by_kind[kind]['ArchivedAt'] == rows[0]['ArchivedAt']
        assert sha256(os.path.join(userctx.DATA_ROOT, 'invoices', *by_kind[kind]['RelPath'].split('/'))) == \
            by_kind[kind]['Sha256']


def test_the_new_documents_lie_at_the_name_scheme_and_the_old_path_is_replaced(world):
    invoice_id = sent_invoice(world)
    cols = columns(world.db, invoice_id)
    old_pdf, old_xml = cols['PDFPath'], cols['XMLPath']
    sent_pdf = read_file(old_pdf)

    save_open(world, invoice_id, deliveryDate='2026-06-01')

    new = columns(world.db, invoice_id)
    assert new['PDFPath'] == old_pdf and new['XMLPath'] == old_xml          # derselbe Name, neue Dateien
    assert os.path.isfile(old_pdf) and os.path.isfile(old_xml)
    assert read_file(old_pdf) != sent_pdf and read_file(old_pdf).startswith(b'%PDF')
    assert b'2026-06-01' in read_file(old_xml)


def test_the_new_version_carries_the_notice_in_pdf_cii_and_ubl_and_the_type_stays_380(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, deliveryDate='2026-06-01')
    cols = columns(world.db, invoice_id)
    text = notice('Leistungsdatum')

    page = pdf_text(cols['PDFPath'])
    assert 'Fassung: 2' in page and text in page.replace('\n', ' ')
    assert page.index('Fassung: 2') < page.index('Pos. Menge')              # im Kopf, die Zeile vor der Tabelle
    assert page.index('Berichtigte Fassung') < page.index('Pos. Menge')

    cii = ET.fromstring(embedded_xml(read_file(cols['PDFPath'])))
    ns = {'rsm': 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100',
          'ram': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100'}
    document = cii.find('rsm:ExchangedDocument', ns)
    assert [child.tag.split('}')[1] for child in document] == ['ID', 'TypeCode', 'IssueDateTime', 'IncludedNote']
    assert document.find('ram:IncludedNote/ram:Content', ns).text == text
    assert document.find('ram:TypeCode', ns).text == '380' and document.find('ram:ID', ns).text == '26R100'

    ubl = ET.fromstring(read_file(cols['XMLPath']))
    tags = root_children(cols['XMLPath'])
    assert tags.index('Note') == tags.index('InvoiceTypeCode') + 1 and tags.index('Note') < tags.index(
        'DocumentCurrencyCode')                                              # Reihenfolge laut UBL-Schema
    assert ubl.find('{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Note').text == text
    assert ubl.find('{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}InvoiceTypeCode'
                    ).text == '380'


def test_the_first_version_carries_no_notice(world):
    invoice_id = sent_invoice(world)
    page = pdf_text(columns(world.db, invoice_id)['PDFPath'])
    assert 'Berichtigte Fassung' not in page and 'Fassung:' not in page
    assert world.db.correction_notice(invoice_id) is None
    assert b'IncludedNote' not in embedded_xml(read_file(columns(world.db, invoice_id)['PDFPath']))
    assert 'Note' not in root_children(columns(world.db, invoice_id)['XMLPath'])


def test_the_totals_of_the_new_version_are_those_of_the_old_one(world):
    invoice_id = sent_invoice(world)
    gross = columns(world.db, invoice_id)['SumGross']
    save_open(world, invoice_id, orderNumber='B-99')
    assert columns(world.db, invoice_id)['SumGross'] == gross
    assert '298,10 EUR' in pdf_text(columns(world.db, invoice_id)['PDFPath'])


def test_the_second_correction_is_version_3_and_keeps_both_older_versions(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, orderNumber='B-2')
    first_correction = read_file(columns(world.db, invoice_id)['PDFPath'])

    answer = save_open(world, invoice_id, deliveryDate='2026-06-01')

    assert answer['documentVersion'] == 3 and document_version(world.db, invoice_id) == 3
    rows = archived_versions(world.db, invoice_id)
    assert [(r['Version'], r['Kind']) for r in rows] == [(1, 'pdf'), (1, 'xml'), (2, 'pdf'), (2, 'xml')]
    assert len(archived_files(invoice_id)) == 4
    second = next(r for r in rows if (r['Version'], r['Kind']) == (2, 'pdf'))
    assert second['Sha256'] == hashlib.sha256(first_correction).hexdigest()           # Fassung 2 ist erhalten
    page = pdf_text(columns(world.db, invoice_id)['PDFPath'])
    assert 'Fassung: 3' in page and notice('Leistungsdatum') in page.replace('\n', ' ')
    assert [(r['Field'], r['Version']) for r in protocol(world.db, invoice_id, 'correct')] == [
        ('OrderNumber', 2), ('DeliveryDate', 3)]


def test_a_taken_name_gets_a_counter_for_the_second_correction_of_the_day(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, orderNumber='B-2')
    save_open(world, invoice_id, orderNumber='B-3')
    stems = sorted(archived_files(invoice_id))
    assert any(f'versendet bis {TODAY.isoformat()} (2).pdf' in name for name in stems)
    assert len(stems) == 4


def test_the_name_of_the_new_file_follows_the_address_and_the_old_one_is_kept_under_its_name(world):
    invoice_id = sent_invoice(world)
    old_pdf = columns(world.db, invoice_id)['PDFPath']
    sent_pdf = read_file(old_pdf)
    _rename_customer(world)

    save_open(world, invoice_id, refreshAddress=True)

    new_pdf = columns(world.db, invoice_id)['PDFPath']
    assert os.path.basename(new_pdf) == '26R100 Malermeister Pinsel GmbH.pdf' and os.path.isfile(new_pdf)
    assert not os.path.exists(old_pdf)                                      # unter dem alten Namen liegt nichts mehr
    kept = [n for n in archived_files(invoice_id) if n.endswith('.pdf')]
    assert kept == [f'26R100 Malermeister Pinsel – versendet bis {TODAY.isoformat()}.pdf']
    assert read_file(os.path.join(versions_dir(invoice_id), kept[0])) == sent_pdf
    assert notice('Anschrift') in pdf_text(new_pdf).replace('\n', ' ')


# ── Wenn etwas schiefgeht ────────────────────────────────────────────────────

def test_a_failing_move_changes_nothing(world, monkeypatch):
    invoice_id = sent_invoice(world)
    cols = columns(world.db, invoice_id)
    before, files = snapshot(world.db), (read_file(cols['PDFPath']), read_file(cols['XMLPath']))
    real_move, calls = shutil.move, []

    def flaky(src, dst, *args, **kwargs):
        calls.append(src)
        if len(calls) == 2:
            raise OSError('Platte voll')
        return real_move(src, dst, *args, **kwargs)
    monkeypatch.setattr('export.invoice_files.shutil.move', flaky)

    resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'})

    answer = json.loads(resp)
    assert resp.status == 409 and answer['success'] is False and 'nicht berichtigt' in answer['error']
    assert 'Platte' not in str(answer)                                     # keine Ausnahmetexte in der Antwort
    assert snapshot(world.db) == before                                    # Felder, Protokoll, Fassung, Pfade
    assert (read_file(cols['PDFPath']), read_file(cols['XMLPath'])) == files
    assert archived_files(invoice_id) == [] and not os.path.exists(versions_dir(invoice_id))


def test_a_failing_commit_puts_the_documents_back_and_changes_nothing(world, monkeypatch):
    invoice_id = sent_invoice(world)
    cols = columns(world.db, invoice_id)
    before, files = snapshot(world.db), (read_file(cols['PDFPath']), read_file(cols['XMLPath']))
    from db.core import Transaction

    def broken_commit(self, *exc):
        self.conn.rollback()
        self.conn.close()
        raise sqlite3.OperationalError('disk I/O error')
    with monkeypatch.context() as patched:
        patched.setattr(Transaction, '__exit__', broken_commit)
        resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'})

    answer = json.loads(resp)
    assert resp.status == 409 and answer['success'] is False and 'nicht gespeichert' in answer['error']
    assert 'disk' not in str(answer)
    assert (read_file(cols['PDFPath']), read_file(cols['XMLPath'])) == files
    assert archived_files(invoice_id) == []
    assert snapshot(world.db) == before


def test_a_failure_while_writing_the_rows_puts_the_documents_back_too(world, monkeypatch):
    invoice_id = sent_invoice(world)
    cols = columns(world.db, invoice_id)
    before, files = snapshot(world.db), (read_file(cols['PDFPath']), read_file(cols['XMLPath']))

    def broken(*args, **kwargs):
        raise RuntimeError('Programmfehler')
    monkeypatch.setattr(type(world.db), '_log_invoice_change', staticmethod(broken))

    resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'})

    assert json.loads(resp)['success'] is False and 'Programmfehler' not in resp.decode()
    assert (read_file(cols['PDFPath']), read_file(cols['XMLPath'])) == files and archived_files(invoice_id) == []
    assert snapshot(world.db) == before


def test_a_failing_version_row_puts_the_documents_back_and_refuses_the_correction(world):
    """Das Verschieben gelang, das Festhalten in ``InvoiceDocumentVersions`` nicht (hier weist ein Trigger das
    ``INSERT`` ab): Die Dokumente liegen wieder am gespeicherten Pfad, die Antwort ist die des gescheiterten Ablegens."""
    invoice_id = sent_invoice(world)
    cols = columns(world.db, invoice_id)
    before, files = snapshot(world.db), (read_file(cols['PDFPath']), read_file(cols['XMLPath']))
    run_sql(world.db, "CREATE TRIGGER boom BEFORE INSERT ON InvoiceDocumentVersions "
                      "BEGIN SELECT RAISE(ABORT, 'boom'); END")

    resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'})

    assert resp.status == 409
    assert json.loads(resp) == {'success': False, 'error': CORRECTION_ARCHIVE_FAILED_MESSAGE}
    assert (read_file(cols['PDFPath']), read_file(cols['XMLPath'])) == files
    assert archived_files(invoice_id) == [] and not os.path.exists(versions_dir(invoice_id))
    assert snapshot(world.db) == before                     # Felder, Fassung, Pfade, Protokoll, Fassungstabelle
    assert columns(world.db, invoice_id)['DocumentVersion'] is None


def test_an_unexpected_error_while_holding_the_documents_puts_them_back_too(world, monkeypatch):
    invoice_id = sent_invoice(world)
    cols = columns(world.db, invoice_id)
    before, files = snapshot(world.db), (read_file(cols['PDFPath']), read_file(cols['XMLPath']))

    def broken(path):
        raise RuntimeError('Programmfehler')
    monkeypatch.setattr('db.invoice_versions._sha256', broken)

    resp = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'})

    assert json.loads(resp)['success'] is False and 'Programmfehler' not in resp.decode()
    assert (read_file(cols['PDFPath']), read_file(cols['XMLPath'])) == files and archived_files(invoice_id) == []
    assert snapshot(world.db) == before


def test_a_failing_generation_keeps_the_correction_and_warns(world, monkeypatch):
    invoice_id = sent_invoice(world)

    def broken(*args, **kwargs):
        raise RuntimeError('PDF kaputt')
    monkeypatch.setattr('export.invoice_files.generate_invoice_documents', broken)

    answer = save_open(world, invoice_id, deliveryDate='2026-06-01')

    assert answer['success'] is True and answer['documentVersion'] == 2
    assert 'Berichtigung ist gespeichert' in answer['warning'] and 'PDF + E-Rechnung' in answer['warning']
    assert 'PDF kaputt' not in answer['warning']
    stored = columns(world.db, invoice_id)
    assert stored['DeliveryDate'] == '2026-06-01' and stored['PDFPath'] is None and stored['XMLPath'] is None
    assert len(archived_versions(world.db, invoice_id)) == 2 and len(protocol(world.db, invoice_id, 'correct')) == 1


def test_a_reported_error_of_the_generation_is_a_warning_too(world, monkeypatch):
    invoice_id = sent_invoice(world)
    monkeypatch.setattr('export.invoice_files.generate_invoice_documents',
                        lambda *a, **k: {'pdf_path': None, 'error': 'PDF konnte nicht erstellt werden.'})
    answer = save_open(world, invoice_id, orderNumber='B-9')
    assert answer['documentVersion'] == 2 and 'PDF konnte nicht erstellt werden.' in answer['warning']


def test_the_button_makes_the_missing_file_after_a_failed_generation_and_it_carries_the_notice(world, monkeypatch):
    """Die Regel „eingefroren, aber keine Datei vorhanden“ greift: „PDF + E-Rechnung“ erzeugt die Datei der
    neuen Fassung – mit dem Hinweis, den der Stand der Datenbank vorgibt."""
    invoice_id = sent_invoice(world)
    with monkeypatch.context() as patched:
        patched.setattr('export.invoice_files.generate_invoice_documents',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('PDF kaputt')))
        assert 'warning' in save_open(world, invoice_id, deliveryDate='2026-06-01')
    assert columns(world.db, invoice_id)['PDFPath'] is None

    result = handlers.handle_invoice_pdf_generate(invoice_id)                     # „PDF + E-Rechnung“

    assert result['error'] is None and result['pdf_path'] and result['xml_path']
    stored = columns(world.db, invoice_id)
    assert stored['PDFPath'] == result['pdf_path'] and os.path.isfile(stored['PDFPath'])
    page = pdf_text(stored['PDFPath']).replace('\n', ' ')
    assert 'Fassung: 2' in page and notice('Leistungsdatum') in page
    assert notice('Leistungsdatum') in read_file(stored['XMLPath']).decode('utf-8')
    assert len(archived_versions(world.db, invoice_id)) == 2                      # die Fassung 1 blieb, wo sie war


def test_the_button_does_not_overwrite_a_new_version_that_exists(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, deliveryDate='2026-06-01')
    path = columns(world.db, invoice_id)['PDFPath']
    marked = write_file(path, b'%PDF-1.4 berichtigte Fassung')
    assert handlers.handle_invoice_pdf_generate(invoice_id)['pdf_path'] == marked
    assert read_file(path) == b'%PDF-1.4 berichtigte Fassung'


# ── Ohne abgelegtes Dokument (Import) ────────────────────────────────────────

def test_an_import_without_documents_gets_only_fields_and_protocol(world):
    invoice_id = make_invoice(world, 'sent')
    assert stored_documents(userctx.DATA_ROOT) == {}

    answer = save_open(world, invoice_id, deliveryDate='2026-06-01')

    assert answer == {'success': True, 'invoice_id': invoice_id, 'documentVersion': 2}      # keine Warnung
    stored = columns(world.db, invoice_id)
    assert (stored['DeliveryDate'], stored['DocumentVersion'], stored['PDFPath']) == ('2026-06-01', 2, None)
    assert archived_versions(world.db, invoice_id) == [] and stored_documents(userctx.DATA_ROOT) == {}
    assert len(protocol(world.db, invoice_id, 'correct')) == 1


def test_a_document_made_later_for_an_import_carries_the_notice_and_dates_by_the_correction(world):
    invoice_id = make_invoice(world, 'sent')
    save_open(world, invoice_id, orderNumber='B-9')

    result = handlers.handle_invoice_pdf_generate(invoice_id)

    assert result['error'] is None
    page = pdf_text(result['pdf_path']).replace('\n', ' ')
    assert 'Fassung: 2' in page
    # der Import hat keinen Versandzeitpunkt (SentAt NULL): Der Teil „ersetzt die Fassung vom …“ entfällt
    assert f'Berichtigte Fassung vom {TODAY_DE} (geändert: Bestellnummer)' in page
    assert 'ersetzt die Fassung' not in page


# ── Zurückholen und erneut versenden ─────────────────────────────────────────

def test_after_taking_back_and_sending_again_the_document_has_no_notice(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, deliveryDate='2026-06-01')
    assert world.db.correction_notice(invoice_id) is not None
    assert set_invoice_status(invoice_id, 'draft')[0] == 200
    assert world.db.correction_notice(invoice_id) is None          # ein Entwurf ist keine berichtigte Fassung mehr

    code, body = set_invoice_status(invoice_id, 'sent')

    assert code == 200 and 'warning' not in body
    cols = columns(world.db, invoice_id)
    assert cols['DocumentVersion'] == 3
    page = pdf_text(cols['PDFPath'])
    assert 'Berichtigte Fassung' not in page and 'Fassung:' not in page
    assert b'IncludedNote' not in embedded_xml(read_file(cols['PDFPath']))
    assert 'Note' not in root_children(cols['XMLPath'])
    rows = archived_versions(world.db, invoice_id)
    assert [(r['Version'], r['Kind'], r['Reason']) for r in rows] == [
        (1, 'pdf', 'corrected'), (1, 'xml', 'corrected'), (2, 'pdf', 'reopened'), (2, 'xml', 'reopened')]


def test_taking_back_an_imported_corrected_invoice_without_files_leaves_no_notice(world):
    """Das Zurückholen zählt die Fassung nur hoch, wenn eine Datei abgelegt wurde – der Hinweis fällt trotzdem
    weg: Eine Rechnung, die nach dem Zurückholen neu versendet wird, trägt keinen."""
    invoice_id = make_invoice(world, 'sent')
    save_open(world, invoice_id, orderNumber='B-9')
    assert world.db.correction_notice(invoice_id) is not None
    assert set_invoice_status(invoice_id, 'draft')[0] == 200
    assert document_version(world.db, invoice_id) == 2                       # keine Datei: keine neue Fassung
    assert world.db.correction_notice(invoice_id) is None
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    assert 'Berichtigte Fassung' not in pdf_text(columns(world.db, invoice_id)['PDFPath'])


def test_a_correction_after_the_new_sending_dates_the_replaced_version_by_the_sending(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, orderNumber='B-2')
    set_invoice_status(invoice_id, 'draft')
    set_invoice_status(invoice_id, 'sent')

    save_open(world, invoice_id, orderNumber='B-3')

    notice_ = world.db.correction_notice(invoice_id)
    assert notice_.version == 4 and notice_.fields == ('Bestellnummer',)
    assert notice_.corrected_on == TODAY and notice_.replaces_on == TODAY


# ── Der Hinweis ──────────────────────────────────────────────────────────────

def test_the_notice_names_the_changed_fields_in_the_agreed_order(world):
    invoice_id = make_invoice(world, 'sent')
    _rename_customer(world)
    save_open(world, invoice_id, paymentTerms='Zahlbar sofort', orderNumber='B-99', deliveryDate='2026-06-01',
              buyerReference='991-99999-99', refreshAddress=True)

    result = handlers.handle_invoice_pdf_generate(invoice_id)

    page = pdf_text(result['pdf_path']).replace('\n', ' ')
    assert ('(geändert: Leistungsdatum, Anschrift, Zahlungshinweis, Bestellnummer, Leitweg-ID)') in page
    from export.einvoice_model import correction_note_text
    assert correction_note_text(world.db.correction_notice(invoice_id)) == (
        f'Berichtigte Fassung vom {TODAY_DE} (geändert: Leistungsdatum, Anschrift, Zahlungshinweis, '
        'Bestellnummer, Leitweg-ID)')


def test_the_replaced_version_is_dated_by_the_earlier_correction_else_by_the_sending(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, orderNumber='B-2')
    save_open(world, invoice_id, orderNumber='B-3')
    run_sql(world.db, "UPDATE InvoiceCorrections SET ChangedAt = '2026-03-04 10:00:00' "      # die erste war im März
                  "WHERE InvoiceID = ? AND Version = 2", invoice_id)
    run_sql(world.db, "UPDATE InvoiceCorrections SET ChangedAt = '2026-01-16 10:00:00' "      # der Versand im Januar
                  "WHERE InvoiceID = ? AND Action = 'status'", invoice_id)
    run_sql(world.db, "UPDATE Invoices SET SentAt = '2026-01-15 10:00:00' WHERE ID = ?", invoice_id)   # SentAt einen Tag früher
    third = world.db.correction_notice(invoice_id)
    assert (third.version, third.replaces_on) == (3, datetime.date(2026, 3, 4))

    run_sql(world.db, "DELETE FROM InvoiceCorrections WHERE InvoiceID = ? AND Version = 2", invoice_id)
    assert world.db.correction_notice(invoice_id).replaces_on == datetime.date(2026, 1, 16)   # ohne sie: der Versand im Protokoll

    run_sql(world.db, "DELETE FROM InvoiceCorrections WHERE InvoiceID = ? AND Action = 'status'", invoice_id)
    assert world.db.correction_notice(invoice_id).replaces_on == datetime.date(2026, 1, 15)   # ohne ihn: SentAt (Bestand)

    run_sql(world.db, "UPDATE Invoices SET SentAt = NULL WHERE ID = ?", invoice_id)               # ohne alles entfällt es
    unknown = world.db.correction_notice(invoice_id)
    assert unknown.replaces_on is None
    assert correction_note_text(unknown) == f'Berichtigte Fassung vom {TODAY_DE} (geändert: Bestellnummer)'


def _date_the_newest_row(world, invoice_id, stamp):
    """Die jüngste Protokollzeile auf einen Zeitpunkt in der Vergangenheit legen – so lässt sich prüfen, aus
    welchem Eintrag ein Datum stammt."""
    run_sql(world.db, "UPDATE InvoiceCorrections SET ChangedAt = ? WHERE ID = "
                      "(SELECT MAX(ID) FROM InvoiceCorrections WHERE InvoiceID = ?)", stamp, invoice_id)


def test_after_sending_again_the_replaced_version_is_dated_by_that_sending_not_the_first_one(world):
    """Versendet (Januar), zurückgeholt, neu versendet (heute), berichtigt: Ersetzt wird die Fassung, die heute an
    den Kunden ging – nicht die des ersten Versands."""
    invoice_id = sent_invoice(world)
    run_sql(world.db, "UPDATE InvoiceCorrections SET ChangedAt = '2026-01-05 10:00:00' WHERE InvoiceID = ?", invoice_id)
    run_sql(world.db, "UPDATE Invoices SET SentAt = '2026-01-05 10:00:00' WHERE ID = ?", invoice_id)
    assert set_invoice_status(invoice_id, 'draft')[0] == 200
    assert set_invoice_status(invoice_id, 'sent')[0] == 200                    # Fassung 2 geht heute an den Kunden

    assert save_open(world, invoice_id, orderNumber='B-99')['documentVersion'] == 3

    notice_ = world.db.correction_notice(invoice_id)
    assert (notice_.version, notice_.corrected_on, notice_.replaces_on) == (3, TODAY, TODAY)
    assert correction_note_text(notice_) == (
        f'Berichtigte Fassung vom {TODAY_DE} – ersetzt die Fassung vom {TODAY_DE} (geändert: Bestellnummer)')
    assert notice('Bestellnummer') in pdf_text(columns(world.db, invoice_id)['PDFPath']).replace('\n', ' ')


def test_after_a_correction_reopening_and_sending_the_replaced_version_is_dated_by_the_second_sending(world):
    invoice_id = sent_invoice(world)
    _date_the_newest_row(world, invoice_id, '2026-01-05 10:00:00')                  # erster Versand
    save_open(world, invoice_id, orderNumber='B-2')                                 # Fassung 2
    _date_the_newest_row(world, invoice_id, '2026-02-10 10:00:00')                  # … im Februar berichtigt
    assert set_invoice_status(invoice_id, 'draft')[0] == 200                         # zurückgeholt: Fassung 3
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    _date_the_newest_row(world, invoice_id, '2026-03-20 10:00:00')                  # zweiter Versand im März

    assert save_open(world, invoice_id, orderNumber='B-3')['documentVersion'] == 4

    notice_ = world.db.correction_notice(invoice_id)
    assert (notice_.version, notice_.corrected_on) == (4, TODAY)
    assert notice_.replaces_on == datetime.date(2026, 3, 20)                         # nicht SentAt (heute) und nicht Januar


def test_two_corrections_in_a_row_the_second_replaces_the_version_of_the_first(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, orderNumber='B-2', paymentTerms='Zahlbar sofort')    # eine Berichtigung, zwei Zeilen
    run_sql(world.db, "UPDATE InvoiceCorrections SET ChangedAt = '2026-03-04 10:00:00' "
                      "WHERE InvoiceID = ? AND Action = 'correct'", invoice_id)

    save_open(world, invoice_id, orderNumber='B-3')

    notice_ = world.db.correction_notice(invoice_id)
    assert (notice_.version, notice_.replaces_on) == (3, datetime.date(2026, 3, 4))


def test_a_taking_back_without_files_does_not_hide_the_second_sending_behind_the_first_correction(world):
    """Ein Import hat keine Dateien: Das Zurückholen zählt die Fassung nicht hoch. Die Berichtigung danach ersetzt
    trotzdem die Fassung, die beim zweiten Versand an den Kunden ging – nicht die der ersten Berichtigung."""
    invoice_id = make_invoice(world, 'sent')
    save_open(world, invoice_id, orderNumber='B-2')                                 # Fassung 2, ohne Datei
    _date_the_newest_row(world, invoice_id, '2026-02-10 10:00:00')
    assert set_invoice_status(invoice_id, 'draft')[0] == 200
    assert document_version(world.db, invoice_id) == 2                              # keine Datei: keine neue Fassung
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    _date_the_newest_row(world, invoice_id, '2026-03-20 10:00:00')                  # zweiter Versand im März

    assert save_open(world, invoice_id, orderNumber='B-3')['documentVersion'] == 3

    assert world.db.correction_notice(invoice_id).replaces_on == datetime.date(2026, 3, 20)


def test_an_import_without_a_sending_time_and_a_protocol_names_no_replaced_version(world):
    invoice_id = make_invoice(world, 'sent')
    assert columns(world.db, invoice_id)['SentAt'] is None and protocol(world.db, invoice_id) == []

    save_open(world, invoice_id, orderNumber='B-9')

    notice_ = world.db.correction_notice(invoice_id)
    assert notice_.replaces_on is None                                  # die eigene Berichtigung ersetzt nichts
    assert correction_note_text(notice_) == f'Berichtigte Fassung vom {TODAY_DE} (geändert: Bestellnummer)'


@pytest.mark.skipif(not hasattr(time, 'tzset'), reason='Zeitzone lässt sich nur unter Unix umstellen')
def test_stored_times_are_utc_and_shown_in_local_time(world, monkeypatch):
    from db.invoice_versions import local_date, local_datetime
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, orderNumber='B-2')
    run_sql(world.db, "UPDATE InvoiceCorrections SET ChangedAt = '2026-11-12 23:30:00' "
                  "WHERE InvoiceID = ? AND Action = 'correct'", invoice_id)
    try:
        with monkeypatch.context() as zone:
            zone.setenv('TZ', 'Europe/Berlin')
            time.tzset()
            assert local_date('2026-11-12 23:30:00') == datetime.date(2026, 11, 13)       # UTC+1 im November
            assert local_datetime('2026-11-12 23:30:00') == datetime.datetime(2026, 11, 13, 0, 30)
            assert world.db.correction_notice(invoice_id).corrected_on == datetime.date(2026, 11, 13)
            zone.setenv('TZ', 'America/Los_Angeles')
            time.tzset()
            assert local_date('2026-11-12 23:30:00') == datetime.date(2026, 11, 12)       # UTC-8
            assert world.db.correction_notice(invoice_id).corrected_on == datetime.date(2026, 11, 12)
            assert local_date('2026-03-04') == datetime.date(2026, 3, 4)                    # reines Datum bleibt
            assert local_date('kein Datum') is None and local_datetime(None) is None
    finally:
        time.tzset()


# ── Gleichzeitige Berichtigungen ─────────────────────────────────────────────

def test_two_simultaneous_corrections_run_one_after_the_other(world):
    invoice_id = sent_invoice(world)
    barrier = threading.Barrier(2)
    results = [None, None]

    def correct(index, **fields):
        barrier.wait()
        results[index] = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, **fields})

    threads = [threading.Thread(target=correct, args=(0,), kwargs={'orderNumber': 'B-A'}),
               threading.Thread(target=correct, args=(1,), kwargs={'paymentTerms': 'Zahlbar sofort'})]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    answers = [json.loads(r) for r in results]
    assert sorted(a['success'] for a in answers) == [False, True]
    loser = next(r for r, a in zip(results, answers) if not a['success'])
    assert loser.status == 409 and json.loads(loser)['error'] == CHANGED_MESSAGE
    assert document_version(world.db, invoice_id) == 2
    assert len(protocol(world.db, invoice_id, 'correct')) == 1               # nur die des Gewinners
    assert len(archived_versions(world.db, invoice_id)) == 2
    stored = columns(world.db, invoice_id)
    changed = [(stored['OrderNumber'] != 'B-17'), (stored['PaymentTerms'] != 'Zahlbar in 14 Tagen')]
    assert sorted(changed) == [False, True]                                    # genau eine der beiden Änderungen
