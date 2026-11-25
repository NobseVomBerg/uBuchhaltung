# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zahlungsart einer Rechnung (2.10.0, Schema 2010): ``Invoices.PaymentMeans``.

Die E-Rechnung sagte bisher fest „58, SEPA-Überweisung“ und nur mit IBAN. Jetzt führt die Rechnung ihre Zahlungsart (Code nach UNTDID 4461): NULL = Überweisung (58, der
ganze Bestand), ``'10'`` bar, ``'48'`` Karte, ``'1'`` nicht festgelegt. Die 58 steht nie als Wert da (sie wird NULL); ein anderer Wert ist ein Fehler (400).

* **Dokumente:** Bei der Überweisung bleibt alles, wie es war (Code 58 mit der IBAN, ohne IBAN kein Element). Jeder andere Code nennt allein den Code
  (``PaymentMeansTypeCode``), ohne Konto. Was sich nicht lesen lässt, schreibt nichts – und ist eine Lücke.
* **Lücken:** Die IBAN fehlt nur bei der Überweisung. Eine Karte ist eine Lücke der XRechnung (BR-DE-24 verlangt die Ziffern der Kartennummer, die die App nicht führt): Das
  UBL entsteht nicht, die CII im PDF schon. Eine nicht lesbare Zahlungsart ist eine Lücke auch der CII.
* **Sperre:** Die Zahlungsart ist eine Finanzangabe und bei einer versendeten Rechnung gesperrt; NULL, ``''`` und ``'58'`` sind dasselbe und kein Verstoß.
* **Kopierwege:** Stornorechnung und Ersatzrechnung übernehmen sie; die Maske zeigt sie als Auswahl im Fuß unter der Bankverbindung.
* **Das PDF ändert sich nicht** (kein Satz, kein Hinweis), und mit NULL in allen drei neuen Spalten sind CII, UBL und PDF Byte für Byte wie vorher.

Alle Namen, Adressen, IBANs und Nummern sind erfundene Testwerte.
"""
import datetime
import json
import re
import xml.etree.ElementTree as ET

import pytest

import server.handlers as handlers
import userctx
from db import invoice_storno
from db.invoice_lock import InvoiceLocked
from db.invoices import PAYMENT_MEANS, PAYMENT_MEANS_INVALID
from export.cii_invoice import NS, generate_cii
from export.einvoice_model import (CARD_XRECHNUNG_GAP, PAYMENT_MEANS_UNREADABLE_GAP, SCOPE_EN16931, SCOPE_XRECHNUNG, Gap, build_einvoice,
                                   einvoice_gaps, gap_texts)
from export.invoice_files import generate_invoice_documents
from export.pdf_invoice import generate_invoice_pdf
from export.xrechnung_invoice import XRechnungGenerator
from server.pages_invoice import PageInvoice
from tests.document_helpers import set_column
from tests.dom_helpers import Dom
from tests.golden_cases import CASES, make_case
from tests.invoice_world import build_world, columns, make_invoice, make_payload, run_sql, save, save_raw, set_invoice_status, snapshot
from tests.test_golden_invoice_outputs import pdf_layout_text

USER = 'anna'
TODAY = '2026-11-12'
CAC = '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}'
CBC = '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}'
IBAN = 'DE89370400440532013000'


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
    """Versendet wie in der Oberfläche (Forderung gebucht, PDF der Fassung 1 abgelegt; eine Karte lässt das UBL aus und meldet es als Warnung)."""
    invoice_id = make_invoice(w, 'draft', number=number, **over)
    code, body = set_invoice_status(invoice_id, 'sent')
    assert code == 200, body
    return invoice_id


def stored_means(w, invoice_id):
    return columns(w.db, invoice_id)['PaymentMeans']


def model(w, invoice_id):
    return build_einvoice(w.db, w.db.get_invoice_by_id(invoice_id), w.db.get_invoice_items(invoice_id))


def cii(w, invoice_id):
    db = w.db
    return ET.fromstring(generate_cii(db.get_invoice_by_id(invoice_id), db.get_invoice_items(invoice_id), db))


def ubl(w, invoice_id):
    db = w.db
    text = XRechnungGenerator().generate_xml(db.get_invoice_by_id(invoice_id), db.get_invoice_items(invoice_id), db)
    return ET.fromstring(text.encode('utf-8'))


def names(element):
    return [child.tag.split('}')[1] for child in element]


# ── Speichern ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('given, stored', [('10', '10'), ('48', '48'), ('1', '1'), ('58', None), ('', None), (None, None)],
                         ids=['bar', 'Karte', 'nicht-festgelegt', 'Überweisung-58', 'leer', 'null'])
def test_the_payment_means_of_a_draft_is_stored_as_its_code_and_the_transfer_as_null(world, given, stored):
    assert stored_means(world, draft(world, paymentMeans=given)) == stored


def test_a_payload_without_the_key_stores_the_transfer(world):
    assert stored_means(world, draft(world)) is None


@pytest.mark.parametrize('given', ['59', ' 10', '10 ', '0', 'Bar', 5, 10, 58, True, ['10'], {'10': 1}])
def test_an_unknown_payment_means_is_refused_with_the_text_and_nothing_is_written(world, given):
    before = snapshot(world.db)

    body = save_raw(make_payload(world, paymentMeans=given))

    assert body.status == 400 and json.loads(body) == {'success': False, 'error': PAYMENT_MEANS_INVALID}
    assert snapshot(world.db) == before                                              # keine Nummer verbraucht


def test_the_payment_means_of_a_draft_can_be_changed_and_set_back_to_the_transfer(world):
    invoice_id = draft(world, paymentMeans='10')
    payload = make_payload(world, invoiceId=invoice_id)

    assert save({**payload, 'paymentMeans': '48'})['success'] is True and stored_means(world, invoice_id) == '48'
    assert save({**payload, 'paymentMeans': ''})['success'] is True and stored_means(world, invoice_id) is None


def test_the_question_mark_of_the_form_for_an_unreadable_value_is_refused(world):
    """Die Maske schickt ``'?'`` für „(gespeicherter Wert nicht lesbar)“: Der Server nimmt es nie an – erst eine neue Wahl speichert."""
    invoice_id = draft(world)
    run_sql(world.db, "UPDATE Invoices SET PaymentMeans = '59' WHERE ID = ?", invoice_id)

    body = save_raw(make_payload(world, invoiceId=invoice_id, paymentMeans='?'))

    assert body.status == 400 and json.loads(body)['error'] == PAYMENT_MEANS_INVALID and stored_means(world, invoice_id) == '59'


# ── Die Sperre einer versendeten Rechnung ────────────────────────────────────

def test_a_sent_invoice_does_not_take_another_payment_means(world):
    invoice_id = sent_invoice(world, paymentMeans='10')
    before = snapshot(world.db)

    body = save_raw(make_payload(world, invoiceId=invoice_id, paymentMeans='48', documentVersion=1))

    assert body.status == 409 and snapshot(world.db) == before
    with pytest.raises(InvoiceLocked) as locked:
        world.db.correct_invoice(invoice_id, {'payment_means': '48'}, 1, USER)
    assert locked.value.fields == ['Zahlungsart']


@pytest.mark.parametrize('sent', ['58', '', None])
@pytest.mark.parametrize('stored', [None, '58', ''], ids=['NULL', '58', 'leer'])
def test_the_transfer_written_in_any_form_is_no_change(world, stored, sent):
    invoice_id = sent_invoice(world)
    run_sql(world.db, 'UPDATE Invoices SET PaymentMeans = ? WHERE ID = ?', stored, invoice_id)
    before = snapshot(world.db)

    assert world.db.correct_invoice(invoice_id, {'payment_means': sent}, 1, USER).corrected is False
    assert snapshot(world.db) == before


def test_a_stored_code_is_no_change_when_sent_back_but_not_as_the_transfer(world):
    invoice_id = sent_invoice(world, paymentMeans='1')
    assert world.db.correct_invoice(invoice_id, {'payment_means': '1'}, 1, USER).corrected is False
    for other in ('', None, '58', '10'):
        with pytest.raises(InvoiceLocked):
            world.db.correct_invoice(invoice_id, {'payment_means': other}, 1, USER)


def test_an_unreadable_stored_value_stays_locked_too(world):
    invoice_id = sent_invoice(world)
    run_sql(world.db, "UPDATE Invoices SET PaymentMeans = '59' WHERE ID = ?", invoice_id)

    assert world.db.correct_invoice(invoice_id, {'payment_means': '59'}, 1, USER).corrected is False
    for other in ('', '58', '10'):
        with pytest.raises(InvoiceLocked):
            world.db.correct_invoice(invoice_id, {'payment_means': other}, 1, USER)


def test_the_full_form_of_a_sent_invoice_without_the_key_changes_nothing_for_a_transfer(world):
    invoice_id = sent_invoice(world)
    before = snapshot(world.db)

    answer = save(make_payload(world, invoiceId=invoice_id, documentVersion=1))

    assert answer == {'success': True, 'invoice_id': invoice_id} and snapshot(world.db) == before


# ── CII und UBL ──────────────────────────────────────────────────────────────

def cii_means(w, invoice_id):
    return cii(w, invoice_id).find('.//ram:ApplicableHeaderTradeSettlement/ram:SpecifiedTradeSettlementPaymentMeans', NS)


def ubl_means(w, invoice_id):
    return ubl(w, invoice_id).find(f'{CAC}PaymentMeans')


@pytest.mark.parametrize('given', [None, '58'], ids=['NULL', '58'])
def test_the_transfer_names_code_58_with_the_iban_as_before(world, given):
    invoice_id = draft(world, paymentMeans=given)

    means = cii_means(world, invoice_id)
    assert means.find('ram:TypeCode', NS).text == '58'
    assert means.find('ram:PayeePartyCreditorFinancialAccount/ram:IBANID', NS).text == IBAN
    assert means.find('ram:PayeeSpecifiedCreditorFinancialInstitution/ram:BICID', NS).text == 'TESTDE1X'
    pay = ubl_means(world, invoice_id)
    assert pay.find(f'{CBC}PaymentMeansCode').text == '58' and pay.find(f'{CAC}PayeeFinancialAccount/{CBC}ID').text == IBAN


def test_the_transfer_without_an_iban_writes_no_payment_means_as_before(world):
    invoice_id = draft(world, bankAccountId=None)
    assert cii_means(world, invoice_id) is None and ubl_means(world, invoice_id) is None


@pytest.mark.parametrize('code', ['10', '48', '1'])
def test_any_other_code_names_only_the_code_without_an_account(world, code):
    invoice_id = draft(world, paymentMeans=code)                     # die Rechnung hat eine Bankverbindung – sie bleibt außen vor

    means = cii_means(world, invoice_id)
    assert names(means) == ['TypeCode'] and means.find('ram:TypeCode', NS).text == code
    pay = ubl_means(world, invoice_id)
    assert names(pay) == ['PaymentMeansCode'] and pay.find(f'{CBC}PaymentMeansCode').text == code
    assert IBAN not in ET.tostring(means, encoding='unicode') + ET.tostring(pay, encoding='unicode')


@pytest.mark.parametrize('code', ['10', '1'])
def test_a_code_other_than_the_transfer_needs_no_bank_account(world, code):
    invoice_id = draft(world, paymentMeans=code, bankAccountId=None)

    assert cii_means(world, invoice_id).find('ram:TypeCode', NS).text == code
    assert ubl_means(world, invoice_id).find(f'{CBC}PaymentMeansCode').text == code


def test_an_unreadable_value_writes_no_payment_means_at_all(world):
    invoice_id = draft(world)
    set_column(world.db, invoice_id, 'PaymentMeans', '59')

    assert cii_means(world, invoice_id) is None and ubl_means(world, invoice_id) is None


def test_the_payment_means_stands_where_the_schema_wants_it(world):
    invoice_id = draft(world, paymentMeans='10')

    settle = names(cii(world, invoice_id).find('.//ram:ApplicableHeaderTradeSettlement', NS))
    assert settle.index('InvoiceCurrencyCode') < settle.index('SpecifiedTradeSettlementPaymentMeans') < settle.index('ApplicableTradeTax')
    root = names(ubl(world, invoice_id))
    assert root.index('AccountingCustomerParty') < root.index('PaymentMeans') < root.index('PaymentTerms')


# ── Lücken ───────────────────────────────────────────────────────────────────

def gaps(w, invoice_id):
    return einvoice_gaps(model(w, invoice_id))


IBAN_GAP = 'Bankverbindung (IBAN)'


def test_the_iban_is_missing_only_for_the_transfer(world):
    assert Gap(IBAN_GAP, SCOPE_XRECHNUNG) in gaps(world, draft(world, bankAccountId=None))
    assert Gap(IBAN_GAP, SCOPE_XRECHNUNG) in gaps(world, draft(world, number='26R101', bankAccountId=None, paymentMeans='58'))
    for number, code in (('26R102', '10'), ('26R103', '1')):
        assert gaps(world, draft(world, number=number, bankAccountId=None, paymentMeans=code)) == [], code


def test_a_card_is_a_gap_of_the_xrechnung_only(world):
    invoice_id = draft(world, paymentMeans='48')

    found = gaps(world, invoice_id)

    assert found == [Gap(CARD_XRECHNUNG_GAP, SCOPE_XRECHNUNG)] and gap_texts(found, SCOPE_EN16931) == []
    assert 'Kartenzahlung' in CARD_XRECHNUNG_GAP and 'Kartennummer' in CARD_XRECHNUNG_GAP


def test_a_card_makes_no_ubl_but_the_pdf_with_the_cii(world):
    invoice_id = draft(world, paymentMeans='48')                      # die Leitweg-ID des Formulars löst das UBL aus

    result = generate_invoice_documents(world.db, invoice_id)

    assert result['xml_path'] is None and result['pdf_path'] and result['incomplete'] is True
    assert CARD_XRECHNUNG_GAP in result['error'] and 'Die E-Rechnung im PDF' not in result['error']       # die CII im PDF ist vollständig
    assert cii_means(world, invoice_id).find('ram:TypeCode', NS).text == '48'                                # und nennt die Karte


@pytest.mark.parametrize('code', ['10', '1'])
def test_cash_and_not_specified_make_the_ubl_as_any_complete_invoice(world, code):
    invoice_id = draft(world, paymentMeans=code)

    result = generate_invoice_documents(world.db, invoice_id)

    assert result['error'] is None and result['xml_path'] and result['pdf_path']


def test_an_unreadable_payment_means_is_a_gap_of_the_cii_too(world):
    invoice_id = draft(world)
    set_column(world.db, invoice_id, 'PaymentMeans', '59')

    found = gaps(world, invoice_id)

    assert found == [Gap(PAYMENT_MEANS_UNREADABLE_GAP, SCOPE_EN16931)]              # und keine IBAN-Lücke dazu
    result = generate_invoice_documents(world.db, invoice_id)
    assert result['xml_path'] is None and result['pdf_path'] and PAYMENT_MEANS_UNREADABLE_GAP in result['error']
    assert 'Die E-Rechnung im PDF ist unvollständig' in result['error']


@pytest.mark.parametrize('stored', ['', ' 10', '10 ', '59', 'Bar'])
def test_every_unreadable_value_counts_as_unreadable_never_as_the_transfer(world, stored):
    invoice_id = draft(world)
    set_column(world.db, invoice_id, 'PaymentMeans', stored)
    assert model(world, invoice_id).payment_means is None and model(world, invoice_id).payment_means_unreadable is True


def test_the_model_reads_null_as_the_transfer_and_the_codes_as_they_are(world):
    assert model(world, draft(world)).payment_means == '58'
    for number, code in (('26R101', '10'), ('26R102', '48'), ('26R103', '1')):
        read = model(world, draft(world, number=number, paymentMeans=code))
        assert (read.payment_means, read.payment_means_unreadable) == (code, False)
    assert set(PAYMENT_MEANS) == {'58', '10', '48', '1'}


# ── Kopierwege ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('code', ['10', '48', '1', None])
def test_the_storno_and_the_replacement_invoice_take_the_payment_means(world, code):
    invoice_id = sent_invoice(world, paymentMeans=code)

    assert world.db.cancel_invoice(invoice_id, '2026-07-01', USER).ok
    storno_id = world.db.find_storno_invoice(invoice_id)[0]
    reissue = world.db.create_replacement_draft(invoice_id, USER)

    assert reissue.ok, reissue
    assert stored_means(world, storno_id) == stored_means(world, reissue.invoice_id) == code
    if code:
        assert cii_means(world, storno_id).find('ram:TypeCode', NS).text == code


# ── Die Maske ────────────────────────────────────────────────────────────────

def means_select(html):
    found = re.search(r'<select id="payment_means"[^>]*>.*?</select>', html, re.S)
    assert found, 'die Auswahl der Zahlungsart fehlt'
    return found.group(0)


def options(html):
    """Die Auswahl der Zahlungsart: ``[(Wert, Text, gewählt)]``."""
    return [(value, text, bool(chosen)) for value, chosen, text in re.findall(
        r'<option value="([^"]*)"( selected)?>([^<]*)</option>', means_select(html))]


LABELS = [('', 'Überweisung'), ('10', 'Bar'), ('48', 'Karte'), ('1', 'nicht festgelegt')]


def test_a_new_form_offers_the_four_means_with_the_transfer_chosen(world):
    html = PageInvoice(world.db, {}, None)

    assert [(value, text) for value, text, _ in options(html)] == LABELS
    assert [chosen for *_, chosen in options(html)] == [True, False, False, False]
    tag = means_select(html).split('>')[0]
    assert 'class="no-pdf"' in tag and 'disabled' not in tag
    assert html.index('id="bank_account_select"') < html.index('id="bank_details"') < html.index('id="payment_means"')    # im Fuß unter der Bankverbindung


def test_the_options_follow_the_means_of_the_invoice_module(world):
    shown = {value or '58': text for value, text in LABELS}
    assert shown == PAYMENT_MEANS


@pytest.mark.parametrize('stored, chosen', [(None, ''), ('10', '10'), ('48', '48'), ('1', '1')])
def test_the_form_chooses_the_stored_payment_means(world, stored, chosen):
    invoice_id = draft(world, paymentMeans=stored)
    assert [value for value, _text, picked in options(PageInvoice(world.db, {}, invoice_id)) if picked] == [chosen]


def test_a_copy_takes_the_payment_means_of_the_source(world):
    invoice_id = sent_invoice(world, paymentMeans='48')
    html = PageInvoice(world.db, {}, None, copy_from=invoice_id)
    assert [value for value, _text, picked in options(html) if picked] == ['48'] and 'disabled' not in means_select(html).split('>')[0]


def test_an_unreadable_value_shows_as_its_own_chosen_option_that_the_server_refuses(world):
    invoice_id = draft(world)
    set_column(world.db, invoice_id, 'PaymentMeans', '59')

    shown = options(PageInvoice(world.db, {}, invoice_id))

    assert shown[-1] == ('?', '(gespeicherter Wert nicht lesbar)', True)
    assert [chosen for *_, chosen in shown[:-1]] == [False, False, False, False]          # nichts wird still zur Überweisung


def test_a_sent_invoice_shows_the_means_locked(world):
    invoice_id = sent_invoice(world, paymentMeans='10')
    html = PageInvoice(world.db, {}, invoice_id)
    assert 'disabled' in means_select(html).split('>')[0] and [v for v, _t, picked in options(html) if picked] == ['10']


def test_the_script_sends_the_means_with_the_full_form_and_never_with_a_correction(world):
    invoice_id = sent_invoice(world, paymentMeans='10')
    scripts = Dom(PageInvoice(world.db, {}, invoice_id)).scripts
    script = next(s for s in scripts if 'function saveInvoice()' in s)

    assert script.count('paymentMeans') == 1                                          # nur im Vollformular, und dort nur, wenn eine Zahlungsart gewählt ist
    assert "if (document.getElementById('payment_means').value) invoiceData.paymentMeans = document.getElementById('payment_means').value;" in script
    locked_part = script.split('const current = {')[1].split('};')[0]
    assert 'paymentMeans' not in locked_part                                          # im gesperrten Pfad wird sie nicht mitgeschickt


# ── Das PDF ändert sich nicht ────────────────────────────────────────────────

def test_the_pdf_page_is_the_same_whatever_the_payment_means(world):
    invoice_id = draft(world)
    plain = pdf_layout_text(generate_invoice_pdf(world.db, invoice_id)[0])
    for code in ('10', '48', '1'):
        set_column(world.db, invoice_id, 'PaymentMeans', code)
        assert pdf_layout_text(generate_invoice_pdf(world.db, invoice_id)[0]) == plain, code


# ── Byte für Byte wie vorher ─────────────────────────────────────────────────

def outputs(db, invoice_id):
    return (generate_cii(db.get_invoice_by_id(invoice_id), db.get_invoice_items(invoice_id), db),
            XRechnungGenerator().generate_xml(db.get_invoice_by_id(invoice_id), db.get_invoice_items(invoice_id), db),
            pdf_layout_text(generate_invoice_pdf(db, invoice_id)[0]))


@pytest.mark.parametrize('case', sorted(CASES))
def test_with_null_in_the_three_new_columns_cii_ubl_and_pdf_are_exactly_as_before(tmp_db, monkeypatch, tmp_path, case):
    """Die Spalten werden gesetzt (die Dokumente ändern sich) und wieder auf NULL gesetzt: Alles ist Byte für Byte wie am Anfang."""
    monkeypatch.chdir(tmp_path)
    invoice_id = make_case(tmp_db, case)
    before = outputs(tmp_db, invoice_id)

    set_column(tmp_db, invoice_id, 'DeliveryDate', '2026-03-01')
    set_column(tmp_db, invoice_id, 'ServiceDateEnd', '2026-03-31')
    set_column(tmp_db, invoice_id, 'PaymentMeans', '10')
    during = outputs(tmp_db, invoice_id)
    assert [a != b for a, b in zip(during, before)] == [True, True, True]                       # sie wirken wirklich

    for column in ('DeliveryDate', 'ServiceDateEnd', 'PaymentMeans'):
        set_column(tmp_db, invoice_id, column, None)

    assert outputs(tmp_db, invoice_id) == before
