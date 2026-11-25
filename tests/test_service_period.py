# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Leistungszeitraum einer Rechnung (2.10.0, Schema 2010): ``Invoices.ServiceDateEnd``.

Statt eines einzelnen Leistungsdatums kann eine Rechnung einen Zeitraum nennen: Beginn ist das Leistungsdatum, das Ende steht in
``ServiceDateEnd`` (NULL = nur das Leistungsdatum, der ganze Bestand). Einen Zeitraum gibt es nur, wenn das Ende NACH dem Beginn liegt – ein Ende am Beginn
ist keiner und wird als NULL gespeichert, alles, was sich nicht als Datum lesen lässt, gilt in den Dokumenten als „kein Zeitraum“.

* **Speichern** (Entwurf): ein Ende vor dem Beginn oder kein Datum wird mit Klartext abgelehnt (400), nichts wird geschrieben und keine Nummer verbraucht.
* **Versendete Rechnung:** Das Ende ist änderbar – als Berichtigung (neue Fassung, Protokoll „Leistungszeitraum (bis)“, Hinweis „Leistungszeitraum“). Ein
  Leistungsdatum hinter dem gespeicherten Ende wird abgelehnt, bevor etwas verschoben ist.
* **Kopierwege:** Stornorechnung und Ersatzrechnung übernehmen den Zeitraum, „Als neu kopieren“ füllt das Feld der Maske.
* **Dokumente:** PDF-Kopf zweizeilig („Leistungszeitraum:“ mit dem Beginn, darunter „bis“ mit dem Ende), CII ``BillingSpecifiedPeriod`` (BG-14) statt
  ``ActualDeliverySupplyChainEvent``, UBL ``cac:InvoicePeriod`` nach ``cbc:BuyerReference`` statt ``cac:Delivery``; DATEV Spalte 114 = das Ende.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import csv
import datetime
import io
import json
import re
import xml.etree.ElementTree as ET

import pdfplumber
import pytest

import server.handlers as handlers
import userctx
from db import invoice_storno
from db.invoice_lock import CLOSED_MESSAGE
from db.invoice_versions import PROTOCOL_LABELS
from db.invoices import SERVICE_PERIOD_MESSAGE, SERVICE_PERIOD_START_MESSAGE
from export import pdf_document
from export.cii_invoice import NS, generate_cii
from export.einvoice_model import correction_note_text
from export.pdf_invoice import generate_invoice_pdf
from export.xrechnung_invoice import XRechnungGenerator
from server.pages import script_json
from server.pages_invoice import PageInvoice
from tests.document_helpers import set_column
from tests.dom_helpers import Dom, assert_same_structure
from tests.invoice_world import (build_world, columns, document_version, make_invoice, make_payload, protocol, run_sql, save, save_open, save_raw,
                                 set_invoice_status, snapshot)
from tests.test_invoice_correction import NO_NODE, _run_save_invoice, embedded_xml
from tests.test_pdf_header_block import header_rows

USER = 'anna'
START, END = '2026-06-05', '2026-06-30'                  # make_payload: Leistungsdatum 2026-06-05
TODAY = '2026-11-12'
LEISTUNG = 114                                           # Spalte „Leistungsdatum“ (0-basiert wie ``export.datev._C_LEISTUNG``)
CAC = '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}'
CBC = '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}'


@pytest.fixture
def world(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    monkeypatch.setattr(invoice_storno, '_today', lambda: datetime.date.fromisoformat(TODAY))       # der Tag, den der Server für „heute“ hält (Storno, Ersatzrechnung)
    monkeypatch.chdir(tmp_path)
    userctx.set_user(USER)
    return build_world(tmp_db)


def draft(w, number='26R100', **over):
    return make_invoice(w, 'draft', number=number, **over)


def sent_invoice(w, number='26R100', **over):
    """Versendet wie in der Oberfläche (Forderung gebucht, PDF und XML der Fassung 1 liegen am Namensschema)."""
    invoice_id = make_invoice(w, 'draft', number=number, **over)
    code, body = set_invoice_status(invoice_id, 'sent')
    assert code == 200 and 'warning' not in body, body
    return invoice_id


def stored_end(w, invoice_id):
    return columns(w.db, invoice_id)['ServiceDateEnd']


def error_of(body):
    """Der Klartext einer abgelehnten Antwort (``success`` falsch)."""
    answer = json.loads(body)
    assert answer['success'] is False, answer
    return answer['error']


def refused(payload, status=400):
    """Das Speichern lehnt ab: Statuscode und Klartext der Antwort."""
    body = save_raw(payload)
    assert body.status == status, (body.status, body)
    return error_of(body)


# ── Speichern eines Entwurfs ─────────────────────────────────────────────────

def test_a_valid_end_is_stored_with_the_draft(world):
    invoice_id = draft(world, serviceDateEnd=END)
    assert stored_end(world, invoice_id) == END and columns(world.db, invoice_id)['DeliveryDate'] == START


@pytest.mark.parametrize('given', [None, ''], ids=['null', 'leer'])
def test_no_end_is_stored_as_null(world, given):
    assert stored_end(world, draft(world, serviceDateEnd=given)) is None


def test_a_payload_without_the_key_stores_null(world):
    assert stored_end(world, draft(world)) is None


def test_an_end_on_the_start_day_is_no_period_and_stored_as_null(world):
    assert stored_end(world, draft(world, serviceDateEnd=START)) is None


def test_an_end_before_the_start_is_refused_with_the_text_and_nothing_is_written(world):
    before = snapshot(world.db)
    assert refused(make_payload(world, serviceDateEnd='2026-06-04')) == SERVICE_PERIOD_MESSAGE
    assert snapshot(world.db) == before                           # nichts gespeichert, keine Nummer verbraucht


@pytest.mark.parametrize('end', ['abc', '2026-02-30', '30.06.2026', '2026-6-30', ' 2026-06-30', '2026-06-30T00:00', 20260630, True, ['2026-06-30']])
def test_an_end_that_is_no_date_is_refused(world, end):
    before = snapshot(world.db)
    assert refused(make_payload(world, serviceDateEnd=end)) == SERVICE_PERIOD_MESSAGE
    assert snapshot(world.db) == before


def test_a_start_that_is_no_date_is_refused_with_its_own_text(world):
    assert refused(make_payload(world, deliveryDate='abc', serviceDateEnd=END)) == SERVICE_PERIOD_START_MESSAGE


def test_without_a_delivery_date_the_draft_is_refused_as_before(world):
    before = snapshot(world.db)
    body = save_raw(make_payload(world, deliveryDate=None, serviceDateEnd=END))
    assert 'Leistungsdatum fehlt' in error_of(body) and snapshot(world.db) == before


def test_the_end_of_a_draft_can_be_changed_and_cleared(world):
    invoice_id = draft(world, serviceDateEnd=END)
    payload = make_payload(world, invoiceId=invoice_id)

    assert save({**payload, 'serviceDateEnd': '2026-07-15'})['success'] is True
    assert stored_end(world, invoice_id) == '2026-07-15'
    assert save({**payload, 'serviceDateEnd': ''})['success'] is True
    assert stored_end(world, invoice_id) is None


def test_moving_the_start_behind_the_end_of_a_draft_is_refused(world):
    invoice_id = draft(world, serviceDateEnd=END)
    before = snapshot(world.db)
    assert refused(make_payload(world, invoiceId=invoice_id, deliveryDate='2026-07-01', serviceDateEnd=END)) == SERVICE_PERIOD_MESSAGE
    assert snapshot(world.db) == before


# ── Berichtigen einer versendeten Rechnung ───────────────────────────────────

def test_the_end_of_a_sent_invoice_is_corrected_with_a_protocol_row_and_a_notice(world):
    invoice_id = sent_invoice(world)

    answer = save_open(world, invoice_id, serviceDateEnd=END)

    assert answer['success'] is True and answer['documentVersion'] == 2 and stored_end(world, invoice_id) == END
    (row,) = protocol(world.db, invoice_id, 'correct')
    assert (row['Field'], row['OldValue'], row['NewValue'], row['Version']) == ('ServiceDateEnd', None, END, 2)
    notice = world.db.correction_notice(invoice_id)
    assert notice.fields == ('Leistungszeitraum',) and 'Leistungszeitraum' in correction_note_text(notice)
    assert PROTOCOL_LABELS['ServiceDateEnd'] == 'Leistungszeitraum (bis)'
    assert 'Leistungszeitraum (bis)' in PageInvoice(world.db, {}, invoice_id)             # das Protokoll der Seite nennt sie so


def test_start_and_end_corrected_together_name_both_in_the_notice_in_order(world):
    invoice_id = sent_invoice(world)

    assert save_open(world, invoice_id, deliveryDate='2026-07-01', serviceDateEnd='2026-07-31')['documentVersion'] == 2

    assert world.db.correction_notice(invoice_id).fields == ('Leistungsdatum', 'Leistungszeitraum')
    assert [row['Field'] for row in protocol(world.db, invoice_id, 'correct')] == ['DeliveryDate', 'ServiceDateEnd']


def test_the_end_can_be_removed_by_a_correction(world):
    invoice_id = sent_invoice(world, serviceDateEnd=END)

    assert save_open(world, invoice_id, serviceDateEnd='')['documentVersion'] == 2

    assert stored_end(world, invoice_id) is None
    (row,) = protocol(world.db, invoice_id, 'correct')
    assert (row['OldValue'], row['NewValue']) == (END, None)


def test_a_start_behind_the_stored_end_is_refused_and_nothing_is_moved(world):
    invoice_id = sent_invoice(world, serviceDateEnd=END)
    before = snapshot(world.db)

    answer = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'deliveryDate': '2026-07-15'})

    assert answer.status == 400 and error_of(answer) == SERVICE_PERIOD_MESSAGE
    assert snapshot(world.db) == before and document_version(world.db, invoice_id) == 1


def test_an_end_before_the_stored_start_is_refused_before_anything_is_moved(world):
    invoice_id = sent_invoice(world)
    before = snapshot(world.db)

    answer = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'serviceDateEnd': '2026-06-04'})

    assert answer.status == 400 and error_of(answer) == SERVICE_PERIOD_MESSAGE
    assert snapshot(world.db) == before


def test_an_end_without_a_stored_start_is_refused_with_its_own_text(world):
    invoice_id = sent_invoice(world)
    set_column(world.db, invoice_id, 'DeliveryDate', None)                    # ein Import ohne Leistungsdatum
    before = snapshot(world.db)

    answer = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'serviceDateEnd': END})

    assert answer.status == 400 and error_of(answer) == SERVICE_PERIOD_START_MESSAGE
    assert snapshot(world.db) == before


def test_an_end_on_the_start_day_is_no_change_and_makes_no_version(world):
    invoice_id = sent_invoice(world)
    before = snapshot(world.db)

    answer = save_open(world, invoice_id, serviceDateEnd=START)

    assert answer == {'success': True, 'invoice_id': invoice_id}                # keine neue Fassung
    assert snapshot(world.db) == before


def test_the_same_end_again_changes_nothing(world):
    invoice_id = sent_invoice(world, serviceDateEnd=END)
    before = snapshot(world.db)

    assert save_open(world, invoice_id, serviceDateEnd=END) == {'success': True, 'invoice_id': invoice_id}
    assert snapshot(world.db) == before


def test_a_cancelled_invoice_does_not_take_a_new_end(world):
    invoice_id = sent_invoice(world)
    run_sql(world.db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", invoice_id)
    before = snapshot(world.db)

    answer = save_raw({'invoiceId': invoice_id, 'documentVersion': 1, 'serviceDateEnd': END})

    assert answer.status == 409 and error_of(answer) == CLOSED_MESSAGE
    assert snapshot(world.db) == before


# ── Kopierwege ───────────────────────────────────────────────────────────────

def test_the_storno_invoice_takes_the_period_of_the_original(world):
    invoice_id = sent_invoice(world, serviceDateEnd=END)

    storno = world.db.cancel_invoice(invoice_id, '2026-07-01', USER)

    assert storno.ok, storno
    storno_id = world.db.find_storno_invoice(invoice_id)[0]
    assert stored_end(world, storno_id) == END and columns(world.db, storno_id)['DeliveryDate'] == START


def test_the_replacement_invoice_takes_the_period_of_the_cancelled_one(world):
    invoice_id = sent_invoice(world, serviceDateEnd=END)
    assert world.db.cancel_invoice(invoice_id, '2026-07-01', USER).ok

    reissue = world.db.create_replacement_draft(invoice_id, USER)

    assert reissue.ok, reissue
    assert stored_end(world, reissue.invoice_id) == END


def test_an_invoice_without_a_period_copies_none(world):
    invoice_id = sent_invoice(world)
    assert world.db.cancel_invoice(invoice_id, '2026-07-01', USER).ok
    assert stored_end(world, world.db.find_storno_invoice(invoice_id)[0]) is None


def test_the_storno_documents_carry_the_period_too(world):
    invoice_id = sent_invoice(world, serviceDateEnd=END)
    assert world.db.cancel_invoice(invoice_id, '2026-07-01', USER).ok
    storno_id = world.db.find_storno_invoice(invoice_id)[0]

    assert cii_period(world, storno_id) == ('20260605', '20260630')
    assert ubl_period(world, storno_id) == (START, END)                           # auch die CreditNote trägt ``cac:InvoicePeriod``
    assert ubl(world, storno_id).tag.endswith('}CreditNote')


# ── Die Maske ────────────────────────────────────────────────────────────────

def end_input(html):
    return Dom(html).one('input', id='service_date_end')


def test_the_form_has_the_end_field_under_the_delivery_date_and_empty_for_a_new_invoice(world):
    html = PageInvoice(world.db, {}, None)

    attributes = end_input(html)
    assert attributes['type'] == 'date' and attributes.get('value', '') == '' and 'disabled' not in attributes
    assert html.index('id="delivery_date"') < html.index('id="service_date_end"') < html.index('id="invoice_number"')


def test_the_form_shows_the_stored_end(world):
    invoice_id = draft(world, serviceDateEnd=END)
    assert end_input(PageInvoice(world.db, {}, invoice_id))['value'] == END


def test_a_sent_invoice_keeps_the_end_open_for_a_correction_and_a_cancelled_one_closes_it(world):
    invoice_id = sent_invoice(world, serviceDateEnd=END)
    attributes = end_input(PageInvoice(world.db, {}, invoice_id))
    assert attributes['value'] == END and 'disabled' not in attributes                     # wie das Leistungsdatum

    run_sql(world.db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", invoice_id)
    assert 'disabled' in end_input(PageInvoice(world.db, {}, invoice_id))


def test_copying_as_new_fills_the_end_field(world):
    invoice_id = sent_invoice(world, serviceDateEnd=END)
    html = PageInvoice(world.db, {}, None, copy_from=invoice_id)
    assert end_input(html)['value'] == END and 'disabled' not in end_input(html)


def test_a_hostile_stored_end_does_not_change_the_structure_of_the_form(world):
    invoice_id = draft(world, serviceDateEnd=END)
    benign = PageInvoice(world.db, {}, invoice_id)
    set_column(world.db, invoice_id, 'ServiceDateEnd', '"><img src=x onerror=alert(1)><script>alert(2)</script>')

    hostile = PageInvoice(world.db, {}, invoice_id)

    assert_same_structure(hostile, benign)
    assert '<img src=x' not in hostile and '<script>alert(2)' not in hostile


def test_the_script_names_the_end_in_every_payload_and_checks_it_with_the_same_text(world):
    invoice_id = sent_invoice(world, serviceDateEnd=END)
    scripts = Dom(PageInvoice(world.db, {}, invoice_id)).scripts
    script = next(s for s in scripts if 'function saveInvoice()' in s)

    read = "document.getElementById('service_date_end').value"
    assert script.count(f'serviceDateEnd: {read} || null') == 1                                       # gesperrter Pfad: nur, was geändert ist
    assert 'if (serviceDateEnd) invoiceData.serviceDateEnd = serviceDateEnd;' in script               # Vollformular: nur, wenn gesetzt (sonst bleibt der Payload wie vorher)
    assert re.search(r'const initialOpenFields = \{[^}]*serviceDateEnd: ' + re.escape(read) + r'[^}]*\}', '\n'.join(scripts))   # der Stand beim Laden
    assert script_json(SERVICE_PERIOD_MESSAGE) in script and '__SERVICE_PERIOD_MESSAGE__' not in script


@NO_NODE
def test_the_form_sends_only_a_changed_end(world, tmp_path):
    invoice_id = sent_invoice(world, serviceDateEnd=END)
    initial = {'paymentTerms': 'P', 'orderNumber': '', 'buyerReference': '', 'deliveryDate': START, 'serviceDateEnd': END}

    changed = _run_save_invoice(world, invoice_id, tmp_path, {'delivery_date': START, 'service_date_end': '2026-07-10'}, initial)
    same = _run_save_invoice(world, invoice_id, tmp_path, {'delivery_date': START, 'service_date_end': END, 'order_number': 'B-9'}, initial)

    assert changed['sent'] == [{'invoiceId': invoice_id, 'documentVersion': 1, 'serviceDateEnd': '2026-07-10'}]
    assert same['sent'] == [{'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-9'}]            # das unveränderte Ende bleibt zu Hause


@NO_NODE
def test_the_form_does_not_send_an_end_before_the_start(world, tmp_path):
    invoice_id = sent_invoice(world, serviceDateEnd=END)
    initial = {'paymentTerms': 'P', 'orderNumber': '', 'buyerReference': '', 'deliveryDate': START, 'serviceDateEnd': END}

    log = _run_save_invoice(world, invoice_id, tmp_path, {'delivery_date': START, 'service_date_end': '2026-06-01'}, initial)

    assert log['messages'] == [[SERVICE_PERIOD_MESSAGE, 'error']] and log['asked'] == [] and log['sent'] == []
    assert log['focused'] == ['service_date_end']


# ── Die Dokumente ────────────────────────────────────────────────────────────

def ubl(w, invoice_id):
    db = w.db
    text = XRechnungGenerator().generate_xml(db.get_invoice_by_id(invoice_id), db.get_invoice_items(invoice_id), db)
    return ET.fromstring(text.encode('utf-8'))


def cii(w, invoice_id):
    db = w.db
    return ET.fromstring(generate_cii(db.get_invoice_by_id(invoice_id), db.get_invoice_items(invoice_id), db))


def names(element):
    return [child.tag.split('}')[1] for child in element]


def cii_period(w, invoice_id):
    """(Beginn, Ende) der ``ram:BillingSpecifiedPeriod`` als Text – ``None`` ohne sie."""
    period = cii(w, invoice_id).find('.//ram:ApplicableHeaderTradeSettlement/ram:BillingSpecifiedPeriod', NS)
    if period is None:
        return None
    start, end = (period.find(f'ram:{tag}/udt:DateTimeString', NS) for tag in ('StartDateTime', 'EndDateTime'))
    assert start.get('format') == end.get('format') == '102'
    return start.text, end.text


def ubl_period(w, invoice_id):
    period = ubl(w, invoice_id).find(f'{CAC}InvoicePeriod')
    return None if period is None else (period.findtext(f'{CBC}StartDate'), period.findtext(f'{CBC}EndDate'))


def test_the_cii_names_the_period_in_billing_specified_period_instead_of_a_delivery_event(world):
    invoice_id = draft(world, serviceDateEnd=END)

    root = cii(world, invoice_id)

    assert cii_period(world, invoice_id) == ('20260605', '20260630')
    delivery = root.find('.//ram:ApplicableHeaderTradeDelivery', NS)
    assert delivery.find('ram:ActualDeliverySupplyChainEvent', NS) is None and root.find('.//ram:OccurrenceDateTime', NS) is None      # kein BT-72
    settle = names(root.find('.//ram:ApplicableHeaderTradeSettlement', NS))
    assert settle.index('BillingSpecifiedPeriod') == max(i for i, n in enumerate(settle) if n == 'ApplicableTradeTax') + 1       # Reihenfolge des Schemas
    assert settle.index('BillingSpecifiedPeriod') < settle.index('SpecifiedTradePaymentTerms')


def test_the_cii_without_a_period_is_as_before(world):
    root = cii(world, draft(world))

    assert root.find('.//ram:BillingSpecifiedPeriod', NS) is None
    assert root.find('.//ram:ActualDeliverySupplyChainEvent/ram:OccurrenceDateTime/udt:DateTimeString', NS).text == '20260605'


def test_the_ubl_names_the_period_in_invoice_period_after_the_buyer_reference_and_has_no_delivery(world):
    invoice_id = draft(world, serviceDateEnd=END)

    root = ubl(world, invoice_id)

    assert ubl_period(world, invoice_id) == (START, END)
    children = names(root)
    assert 'Delivery' not in children and root.find(f'.//{CBC}ActualDeliveryDate') is None
    assert children.index('InvoicePeriod') == children.index('BuyerReference') + 1 and children.index('InvoicePeriod') < children.index('OrderReference')
    assert names(root.find(f'{CAC}InvoicePeriod')) == ['StartDate', 'EndDate']


def test_the_ubl_without_a_period_is_as_before(world):
    root = ubl(world, draft(world))

    assert root.find(f'{CAC}InvoicePeriod') is None
    assert root.find(f'{CAC}Delivery/{CBC}ActualDeliveryDate').text == START


@pytest.mark.parametrize('stored', ['abc', '2026-02-30', '30.06.2026', START, '2026-06-04', ''], ids=['Text', 'kein-Tag', 'deutsch', 'gleicher-Tag', 'vor-Beginn', 'leer'])
def test_an_end_that_makes_no_period_is_ignored_by_every_document(world, stored):
    """Was der Server nie schreibt (die SQL-Konsole kann es): Es gilt das Leistungsdatum allein – PDF, CII, UBL und DATEV wie ohne Ende."""
    plain = draft(world)
    odd = draft(world, number='26R101')
    set_column(world.db, odd, 'ServiceDateEnd', stored)

    assert cii_period(world, odd) is None and ubl_period(world, odd) is None
    assert ET.tostring(strip_numbers(cii(world, odd))) == ET.tostring(strip_numbers(cii(world, plain)))
    assert ET.tostring(strip_numbers(ubl(world, odd))) == ET.tostring(strip_numbers(ubl(world, plain)))
    assert header_labels(pdf_of(world, odd)) == header_labels(pdf_of(world, plain))
    assert world.db.get_datev_invoice_data()['26R101'][0] == START


def strip_numbers(root):
    """Die Nummer der Rechnung ist das Einzige, was zwei sonst gleiche Rechnungen trennt."""
    for element in root.iter():
        if element.text and element.text.strip() in ('26R100', '26R101'):
            element.text = 'N'
    return root


def pdf_of(w, invoice_id):
    return generate_invoice_pdf(w.db, invoice_id)[0]


def header_labels(pdf):
    return [(label, value) for label, value, _end, _start in header_rows(pdf) if label != 'Rechnungs-Nr.:']


def test_the_pdf_header_names_start_and_end_in_two_rows_in_iso_format(world):
    invoice_id = draft(world, serviceDateEnd=END)

    rows = header_rows(pdf_of(world, invoice_id))

    assert [(label, value) for label, value, _, _ in rows] == [
        ('Rechnungsdatum:', '2026-06-07'), ('Leistungszeitraum:', START), ('', f'bis {END}'), ('Rechnungs-Nr.:', '26R100'), ('Kunden-Nr.:', 'K-100')]
    for label, _value, label_end, value_start in rows:
        assert label_end <= pdf_document.META_VALUE_X - 5, label                                  # keine Bezeichnung läuft in ihren Wert
        assert value_start >= pdf_document.META_VALUE_X - 1


def test_no_word_of_the_header_leaves_the_page_at_the_right(world):
    pdf = pdf_of(world, draft(world, serviceDateEnd='2026-12-31'))
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        words = [w for w in document.pages[0].extract_words() if w['x0'] >= pdf_document.META_LABEL_X - 1 and w['top'] < 150]
    assert words and max(w['x1'] for w in words) <= pdf_document.RIGHT


def test_the_pdf_without_a_period_has_the_one_row_as_before(world):
    rows = header_rows(pdf_of(world, draft(world)))
    assert [(label, value) for label, value, _, _ in rows][:2] == [('Rechnungsdatum:', '2026-06-07'), ('Leistungsdatum:', START)]
    assert 'Leistungszeitraum:' not in [label for label, *_ in rows]


def test_the_pdf_text_names_the_period_once_and_embeds_the_cii_with_it(world):
    pdf = pdf_of(world, draft(world, serviceDateEnd=END))
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        text = document.pages[0].extract_text()

    assert text.count('Leistungszeitraum:') == 1 and f'bis {END}' in text and 'Leistungsdatum:' not in text
    embedded = ET.fromstring(embedded_xml(pdf))
    assert embedded.find('.//ram:BillingSpecifiedPeriod/ram:EndDateTime/udt:DateTimeString', NS).text == '20260630'


# ── DATEV, Spalte 114 ────────────────────────────────────────────────────────

@pytest.fixture
def books(world):
    world.db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return world


def datev_service_dates(w):
    """Die Spalte 114 jeder Zeile des Stapels, die eine trägt (nur die Forderung einer Rechnung)."""
    csv_bytes, _name = handlers.handle_datev_export(w.db, {'date_from': ['2026-01-01'], 'date_to': ['2026-12-31']})
    assert isinstance(csv_bytes, bytes), csv_bytes
    lines = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]
    rows = [next(csv.reader([line], delimiter=';', quotechar='"')) for line in lines]
    return [row[LEISTUNG] for row in rows if row[LEISTUNG]]


def test_datev_column_114_is_the_end_of_the_period_else_the_service_date(books):
    with_period = sent_invoice(books, number='T-1', serviceDateEnd=END)
    sent_invoice(books, number='T-2', deliveryDate='2026-05-20')

    assert sorted(datev_service_dates(books)) == ['20052026', '30062026']
    assert books.db.get_datev_invoice_data()['T-1'] == (END, 'DE')                       # das Ende als ISO-Datum, das Land wie bisher
    assert books.db.get_datev_invoice_data()['T-2'] == ('2026-05-20', 'DE')
    assert with_period


def test_datev_column_114_of_the_counter_booking_of_a_storno_is_the_end_too(books):
    invoice_id = sent_invoice(books, number='T-1', serviceDateEnd=END)
    assert books.db.cancel_invoice(invoice_id, '2026-07-01', USER).ok

    assert datev_service_dates(books) == ['30062026', '30062026']
