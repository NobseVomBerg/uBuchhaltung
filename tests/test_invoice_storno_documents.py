# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Dokumente einer Stornorechnung (2.2.1): PDF, eingebettete CII und UBL.

Die Datenbank führt die Stornorechnung mit negativen Beträgen, das PDF zeigt sie so. In den XML-Dokumenten stehen die
Beträge positiv (EN 16931, Typ 381): Das Modell (``export/einvoice_model.py``) negiert Menge, Zeilen-, Steuer- und
Gesamtsummen genau einmal. CII: ``TypeCode 381``, ``ram:InvoiceReferencedDocument`` (BT-25/BT-26) und BT-22; UBL: ein
``CreditNote`` mit ``cac:BillingReference``, ``cbc:CreditedQuantity`` und ohne ``cbc:DueDate``. Das Dokument der
Ursprungsrechnung bleibt, wie es war. Die Prüfung mit Mustang (XSD, Schematron, veraPDF) macht der CI-Job
``einvoice`` mit ``tools/make_einvoice_samples.py`` (``zugferd-storno.pdf``, ``xrechnung-storno.xml``).

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import datetime
import hashlib
import io
import json
import os
import xml.etree.ElementTree as ET

import pdfplumber
import pytest
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1, stream_value

import export.invoice_files as invoice_files
import server.handlers as handlers
import userctx
from db.invoice_storno import STORNO_PAYMENT_TEXT
from export.cii_invoice import NS, generate_cii
from export.einvoice_model import build_einvoice, storno_note_text
from export.invoice_files import generate_invoice_documents, stored_invoice_path
from export.xrechnung_invoice import XRechnungGenerator
from tests.document_helpers import fake_smtp, read_file, stored_documents
from tests.invoice_world import (archived_versions, build_world, columns, make_invoice, run_sql, save_open,
                                 set_invoice_status)

USER = 'anna'
ONE_ITEM = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung', 'unitPrice': 100.0,
             'taxRate': 19}]
UBL = {'': 'urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2',
       'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
       'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'}
CII = {**NS}
# Reihenfolge der Elemente auf Kopfebene laut UBL-2.1-XSD für CreditNote (nur, was das Dokument verwendet)
CREDIT_NOTE_ORDER = ['CustomizationID', 'ProfileID', 'ID', 'IssueDate', 'CreditNoteTypeCode', 'Note',
                     'DocumentCurrencyCode', 'BuyerReference', 'OrderReference', 'BillingReference',
                     'AccountingSupplierParty', 'AccountingCustomerParty', 'Delivery', 'PaymentMeans',
                     'PaymentTerms', 'TaxTotal', 'LegalMonetaryTotal', 'CreditNoteLine']


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user(USER)
    w = build_world(tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return w


def sent(w, number='T-100', **over):
    invoice_id = make_invoice(w, 'draft', number=number, items=ONE_ITEM, **over)
    code, body = set_invoice_status(invoice_id, 'sent')
    assert code == 200 and 'warning' not in body, body
    return invoice_id


def cancelled(w, number='T-100', day='2026-07-01', **over):
    """Eine versendete Rechnung stornieren, wie die Route es tut (Dokument der Stornorechnung inklusive)."""
    invoice_id = sent(w, number, **over)
    code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': day}).encode())
    body = json.loads(text)
    assert code == 200 and body['success'] is True and 'warning' not in body, body
    return invoice_id, body['storno_id']


def documents(w, invoice_id):
    """Zeile, Positionen und Beträge des Belegs, wie die Erzeuger sie lesen."""
    return w.db.get_invoice_by_id(invoice_id), w.db.get_invoice_items(invoice_id)


def sha256(path):
    return hashlib.sha256(read_file(path)).hexdigest()


def pdf_text(path):
    with pdfplumber.open(path) as pdf:
        return '\n'.join(page.extract_text() or '' for page in pdf.pages)


def embedded_xml(pdf_bytes):
    doc = PDFDocument(PDFParser(io.BytesIO(pdf_bytes)))
    spec = resolve1(resolve1(doc.catalog['AF'])[0])
    return stream_value(resolve1(spec['EF'])['F']).get_data()


def text_of(root, path, namespaces=CII):
    found = root.find(path, namespaces)
    assert found is not None, path
    return found.text


# ── Modell ───────────────────────────────────────────────────────────────────

def test_the_model_of_a_credit_note_is_a_381_with_positive_amounts_and_the_reference(world):
    invoice_id, storno_id = cancelled(world)
    original = build_einvoice(world.db, *documents(world, invoice_id))
    credit = build_einvoice(world.db, *documents(world, storno_id))

    assert (original.type_code, credit.type_code) == ('380', '381')
    assert credit.preceding_invoice_number == 'T-100' and credit.preceding_invoice_date == datetime.date(2026, 6, 7)
    assert credit.notes == ['Storno zu Rechnung T-100 vom 07.06.2026'] and original.notes == []
    assert (original.preceding_invoice_number, original.preceding_invoice_date) == ('', None)
    assert credit.number != original.number and credit.issue_date == datetime.date(2026, 7, 1)
    # dieselben Positionen und Summen wie die Rechnung – positiv, nicht neu gerechnet
    assert credit.lines == original.lines
    assert [(l.quantity, l.unit_price, l.net) for l in credit.lines] == [(1, 1000000, 1000000)]
    assert credit.tax_breakdown == original.tax_breakdown and credit.totals == original.totals
    assert (credit.totals.line_total, credit.totals.tax_total, credit.totals.grand_total, credit.totals.payable) == (
        1000000, 190000, 1190000, 1190000)
    assert credit.due_date is None and credit.payment_terms == STORNO_PAYMENT_TEXT and credit.warnings == []
    assert credit.delivery_date == original.delivery_date
    assert (credit.seller, credit.buyer, credit.iban) == (original.seller, original.buyer, original.iban)


def test_the_model_of_a_credit_note_without_a_database_has_no_reference(world):
    _invoice_id, storno_id = cancelled(world)
    model = build_einvoice(None, *documents(world, storno_id))
    assert model.type_code == '381' and model.preceding_invoice_number == '' and model.notes == []
    assert model.totals.grand_total == 1190000                                      # trotzdem positiv


def test_the_negation_happens_exactly_once_for_several_positions_and_rates(world):
    items = [{'position': 1, 'quantity': 2.5, 'unit': 'HUR', 'description': 'Beratung', 'unitPrice': 33.33,
              'taxRate': 19}, {'position': 2, 'quantity': 3, 'unit': 'C62', 'description': 'Material',
                               'unitPrice': 7.77, 'taxRate': 19}]
    invoice_id = make_invoice(world, 'draft', number='T-101', items=items)
    set_invoice_status(invoice_id, 'sent')
    storno_id = world.db.cancel_invoice(invoice_id, '2026-07-01', USER).storno_id
    original = build_einvoice(world.db, *documents(world, invoice_id))
    credit = build_einvoice(world.db, *documents(world, storno_id))
    assert credit.lines == original.lines and credit.totals == original.totals and credit.warnings == []
    assert credit.totals.grand_total > 0 and all(l.quantity > 0 and l.net > 0 for l in credit.lines)


def test_a_small_business_credit_note_keeps_its_exemption_in_both_formats(world):
    items = [dict(ONE_ITEM[0], taxRate=0)]
    invoice_id = make_invoice(world, 'draft', number='T-500', items=items, taxRate=0, showTax=False)
    set_invoice_status(invoice_id, 'sent')
    storno_id = world.db.cancel_invoice(invoice_id, '2026-07-01', USER).storno_id
    model = build_einvoice(world.db, *documents(world, storno_id))
    assert model.type_code == '381' and model.is_exempt and model.warnings == []
    assert [(t.category, t.basis, t.amount) for t in model.tax_breakdown] == [('E', 1000000, 0)]
    assert model.totals.grand_total == 1000000
    root = ET.fromstring(generate_cii(*documents(world, storno_id), db=world.db))
    assert text_of(root, './/ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax/ram:CategoryCode') == 'E'
    assert text_of(root, './/ram:SpecifiedTradeSettlementHeaderMonetarySummation/ram:GrandTotalAmount') == '100.00'
    ubl = ET.fromstring(XRechnungGenerator().generate_xml(*documents(world, storno_id), db=world.db))
    assert text_of(ubl, 'cac:LegalMonetaryTotal/cbc:PayableAmount', UBL) == '100.00' and ubl.tag.endswith('}CreditNote')


def test_the_note_text_of_a_credit_note():
    assert storno_note_text('R26001', datetime.date(2026, 1, 15)) == 'Storno zu Rechnung R26001 vom 15.01.2026'
    assert storno_note_text('R26001', None) == 'Storno zu Rechnung R26001'


# ── CII ──────────────────────────────────────────────────────────────────────

def test_the_cii_of_a_credit_note(world):
    invoice_id, storno_id = cancelled(world)
    original = ET.fromstring(generate_cii(*documents(world, invoice_id), db=world.db))
    root = ET.fromstring(generate_cii(*documents(world, storno_id), db=world.db))

    assert text_of(root, 'rsm:ExchangedDocument/ram:TypeCode') == '381'
    assert text_of(original, 'rsm:ExchangedDocument/ram:TypeCode') == '380'
    assert text_of(root, 'rsm:ExchangedDocument/ram:IncludedNote/ram:Content') == (
        'Storno zu Rechnung T-100 vom 07.06.2026')                                   # BT-22
    settle = root.find('.//ram:ApplicableHeaderTradeSettlement', CII)
    reference = settle.find('ram:InvoiceReferencedDocument', CII)
    assert text_of(reference, 'ram:IssuerAssignedID') == 'T-100'                     # BT-25
    stamp = reference.find('ram:FormattedIssueDateTime/qdt:DateTimeString', CII)
    assert (stamp.text, stamp.get('format')) == ('20260607', '102')                  # BT-26
    # im Schema nach der Summenbildung
    names = [child.tag.split('}')[1] for child in settle]
    assert names.index('InvoiceReferencedDocument') == names.index(
        'SpecifiedTradeSettlementHeaderMonetarySummation') + 1
    assert original.find('.//ram:InvoiceReferencedDocument', CII) is None
    # positive Beträge
    summ = settle.find('ram:SpecifiedTradeSettlementHeaderMonetarySummation', CII)
    assert [summ.find(f'ram:{tag}', CII).text for tag in (
        'LineTotalAmount', 'TaxBasisTotalAmount', 'TaxTotalAmount', 'GrandTotalAmount', 'DuePayableAmount')] == [
        '100.00', '100.00', '19.00', '119.00', '119.00']
    assert text_of(root, './/ram:SpecifiedLineTradeDelivery/ram:BilledQuantity') == '1'
    assert text_of(root, './/ram:SpecifiedLineTradeSettlement/ram:SpecifiedTradeSettlementLineMonetarySummation/'
                         'ram:LineTotalAmount') == '100.00'
    # keine Fälligkeit; der Zahlungstext steht als Zahlungsbedingung
    terms = settle.find('ram:SpecifiedTradePaymentTerms', CII)
    assert text_of(terms, 'ram:Description') == STORNO_PAYMENT_TEXT and terms.find('ram:DueDateDateTime', CII) is None
    assert text_of(settle, 'ram:InvoiceCurrencyCode') == 'EUR'


# ── UBL ──────────────────────────────────────────────────────────────────────

def test_the_ubl_of_a_credit_note_is_a_credit_note(world):
    invoice_id, storno_id = cancelled(world)
    xml = XRechnungGenerator().generate_xml(*documents(world, storno_id), db=world.db)
    root = ET.fromstring(xml)

    assert root.tag == '{%s}CreditNote' % UBL['']
    assert xml.count('xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"') == 1
    order = [child.tag.split('}')[1] for child in root]
    assert [name for name in order if name in CREDIT_NOTE_ORDER] == [
        name for name in CREDIT_NOTE_ORDER if name in order]                           # die Reihenfolge der XSD
    assert order.count('CreditNoteLine') == 1 and not {'InvoiceLine', 'DueDate', 'InvoiceTypeCode'} & set(order)
    assert text_of(root, 'cbc:CreditNoteTypeCode', UBL) == '381'
    assert text_of(root, 'cbc:Note', UBL) == 'Storno zu Rechnung T-100 vom 07.06.2026'
    reference = root.find('cac:BillingReference/cac:InvoiceDocumentReference', UBL)
    assert (text_of(reference, 'cbc:ID', UBL), text_of(reference, 'cbc:IssueDate', UBL)) == ('T-100', '2026-06-07')
    assert order.index('BillingReference') == order.index('OrderReference') + 1       # nach der Bestellreferenz
    line = root.find('cac:CreditNoteLine', UBL)
    quantity = line.find('cbc:CreditedQuantity', UBL)
    assert (quantity.text, quantity.get('unitCode')) == ('1', 'C62')                  # positiv
    assert text_of(line, 'cbc:LineExtensionAmount', UBL) == '100.00'
    totals = root.find('cac:LegalMonetaryTotal', UBL)
    assert [totals.find(f'cbc:{tag}', UBL).text for tag in (
        'LineExtensionAmount', 'TaxExclusiveAmount', 'TaxInclusiveAmount', 'PayableAmount')] == [
        '100.00', '100.00', '119.00', '119.00']
    assert text_of(root, 'cac:TaxTotal/cbc:TaxAmount', UBL) == '19.00'
    assert text_of(root, 'cac:PaymentTerms/cbc:Note', UBL) == STORNO_PAYMENT_TEXT


def test_the_ubl_of_the_original_stays_an_invoice(world):
    invoice_id, _storno_id = cancelled(world)
    xml = XRechnungGenerator().generate_xml(*documents(world, invoice_id), db=world.db)
    root = ET.fromstring(xml)
    assert root.tag == '{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice'
    names = [child.tag.split('}')[1] for child in root]
    assert 'DueDate' in names and 'InvoiceLine' in names and 'BillingReference' not in names
    assert 'CreditNote' not in xml


def test_generating_both_kinds_alternately_keeps_each_namespace(world):
    """Das Präfix des Standard-Namensraums gehört je Dokument dem Invoice- oder dem CreditNote-Namensraum."""
    invoice_id, storno_id = cancelled(world)
    for _ in range(2):
        for iid, tag in ((invoice_id, 'Invoice'), (storno_id, 'CreditNote')):
            xml = XRechnungGenerator().generate_xml(*documents(world, iid), db=world.db)
            assert xml.splitlines()[1].startswith(f'<{tag} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{tag}-2"')
            assert 'ns0:' not in xml


# ── PDF ──────────────────────────────────────────────────────────────────────

def test_the_pdf_of_a_credit_note(world):
    invoice_id, storno_id = cancelled(world)
    credit_path = stored_invoice_path(world.db, storno_id, 'pdf')[0]
    text = pdf_text(credit_path)

    assert 'Stornorechnung' in text
    assert 'zu Rechnung Nr. T-100 vom 07.06.2026' in text
    assert 'Rechnungs-Nr.: 26R001' in text                                           # die Stornonummer
    assert '-1,00' in text and '100,00 EUR' in text and '-100,00 EUR' in text        # Menge, Einzelpreis, Summe
    assert 'Summe netto: -100,00 EUR' in text and 'zzgl. 19% MwSt.: -19,00 EUR' in text
    assert 'Gesamtbetrag: -119,00 EUR' in text
    assert STORNO_PAYMENT_TEXT in text
    assert 'Berichtigte Fassung' not in text and 'Fassung:' not in text
    # die Ursprungsrechnung heißt weiter Rechnung und zeigt ihre positiven Beträge
    original = pdf_text(stored_invoice_path(world.db, invoice_id, 'pdf')[0])
    assert 'Stornorechnung' not in original and 'zu Rechnung Nr.' not in original
    assert 'Gesamtbetrag: 119,00 EUR' in original


def test_the_embedded_cii_of_the_credit_note_is_the_standalone_cii(world):
    _invoice_id, storno_id = cancelled(world)
    path = stored_invoice_path(world.db, storno_id, 'pdf')[0]
    assert embedded_xml(read_file(path)) == generate_cii(*documents(world, storno_id), db=world.db)
    root = ET.fromstring(embedded_xml(read_file(path)))
    assert text_of(root, 'rsm:ExchangedDocument/ram:TypeCode') == '381'


@pytest.mark.parametrize('number, one_line', [('T-100', True), ('RE-2026-GROSSKUNDE-000123', False)])
def test_the_reference_line_stays_inside_the_margin_even_for_a_long_number(world, number, one_line):
    """Die Zeile unter dem Titel wird verkleinert, und passt sie auch dann nicht, bricht sie um – nie über den Rand."""
    _invoice_id, storno_id = cancelled(world, number=number)
    with pdfplumber.open(stored_invoice_path(world.db, storno_id, 'pdf')[0]) as pdf:
        page = pdf.pages[0]
        text = page.extract_text()
        right_block = [w for w in page.extract_words() if 90 < w['top'] < 220 and w['x0'] >= 389]
    assert ('zu Rechnung Nr. %s vom 07.06.2026' % number in text) is one_line
    assert {'zu', 'Rechnung', 'Nr.', number, 'vom', '07.06.2026'} <= set(text.split())
    assert max(w['x1'] for w in right_block) <= 546                                     # der rechte Rand liegt bei 545 pt


# ── Ablage ───────────────────────────────────────────────────────────────────

def test_the_documents_of_the_credit_note_have_their_own_name_and_the_original_stays_untouched(world):
    invoice_id = sent(world)
    original_pdf = stored_invoice_path(world.db, invoice_id, 'pdf')[0]
    original_xml = stored_invoice_path(world.db, invoice_id, 'xml')[0]
    before = {path: sha256(path) for path in (original_pdf, original_xml)}
    stored_before = stored_documents(userctx.DATA_ROOT)
    row_before = (columns(world.db, invoice_id)['PDFPath'], columns(world.db, invoice_id)['XMLPath'])

    code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': '2026-07-01'}).encode())
    storno_id = json.loads(text)['storno_id']

    assert {path: sha256(path) for path in (original_pdf, original_xml)} == before     # Byte für Byte dieselben Dateien
    assert (columns(world.db, invoice_id)['PDFPath'], columns(world.db, invoice_id)['XMLPath']) == row_before
    credit = columns(world.db, storno_id)
    assert os.path.basename(credit['PDFPath']) == '26R001 Malermeister Pinsel.pdf'
    assert os.path.basename(credit['XMLPath']) == '26R001 Malermeister Pinsel.xml'     # die Bestellnummer: E-Rechnung
    assert os.path.dirname(credit['PDFPath']).endswith(os.path.join('invoices', '2026'))
    new_files = set(stored_documents(userctx.DATA_ROOT)) - set(stored_before)
    assert {os.path.basename(p) for p in new_files} == {'26R001 Malermeister Pinsel.pdf',
                                                        '26R001 Malermeister Pinsel.xml'}
    assert archived_versions(world.db, invoice_id) == [] and columns(world.db, invoice_id)['DocumentVersion'] is None
    assert not os.path.isdir(os.path.join(userctx.DATA_ROOT, 'invoices', '2026', 'Fassungen'))   # nichts „abgelegt“


def test_the_folder_is_the_year_of_the_storno_date(world):
    _invoice_id, storno_id = cancelled(world, day='2027-01-04', invoiceDate='2026-12-28', deliveryDate='2026-12-28')
    path = columns(world.db, storno_id)['PDFPath']
    assert os.path.dirname(path).endswith(os.path.join('invoices', '2027')) and os.path.isfile(path)
    assert os.path.basename(path) == '27R001 Malermeister Pinsel.pdf'


def test_without_e_invoice_data_only_the_pdf_is_created(world):
    _invoice_id, storno_id = cancelled(world, orderNumber=None, buyerReference=None)
    credit = columns(world.db, storno_id)
    assert credit['PDFPath'] and os.path.isfile(credit['PDFPath']) and credit['XMLPath'] is None


def test_generating_again_keeps_the_stored_document_of_the_credit_note(world):
    _invoice_id, storno_id = cancelled(world)
    path = stored_invoice_path(world.db, storno_id, 'pdf')[0]
    with open(path, 'wb') as f:
        f.write(b'%PDF-1.4 versendete Stornorechnung')
    result = generate_invoice_documents(world.db, storno_id)                            # „PDF + E-Rechnung“
    assert result['error'] is None and result['pdf_path'] == path
    assert read_file(path) == b'%PDF-1.4 versendete Stornorechnung'                    # eingefroren: nicht überschrieben


def test_a_credit_note_without_a_stored_document_is_generated_on_demand(world, monkeypatch):
    invoice_id = sent(world)
    real_generator = invoice_files.generate_invoice_documents
    monkeypatch.setattr(invoice_files, 'generate_invoice_documents',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('Platte voll')))
    code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': '2026-07-01'}).encode())
    body = json.loads(text)
    assert code == 200 and body['success'] is True and 'PDF + E-Rechnung' in body['warning']
    assert columns(world.db, body['storno_id'])['PDFPath'] is None
    assert columns(world.db, invoice_id)['Status'] == 'cancelled'                       # die Stornierung bleibt
    monkeypatch.setattr(invoice_files, 'generate_invoice_documents', real_generator)
    result = generate_invoice_documents(world.db, body['storno_id'])
    assert result['error'] is None and os.path.isfile(result['pdf_path'])


def test_an_error_text_of_the_generator_becomes_the_warning(world, monkeypatch):
    invoice_id = sent(world)
    monkeypatch.setattr(invoice_files, 'generate_invoice_documents',
                        lambda *a, **k: {'pdf_path': None, 'error': 'PDF konnte nicht erstellt werden.'})
    code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': '2026-07-01'}).encode())
    assert code == 200 and 'PDF konnte nicht erstellt werden.' in json.loads(text)['warning']


# ── E-Mail ───────────────────────────────────────────────────────────────────

def _mail(world, invoice_id):
    return json.loads(handlers.handle_send_invoice_email(json.dumps({
        'invoice_id': invoice_id, 'recipient_email': 'kunde@example.com', 'recipient_name': 'Kunde'}).encode()))


def test_the_mail_of_a_credit_note_names_it_and_asks_for_no_payment(world, monkeypatch):
    smtp = fake_smtp(monkeypatch)
    invoice_id, storno_id = cancelled(world)

    assert _mail(world, storno_id) == {'success': True}
    credit_mail = smtp.sent[-1]
    body = credit_mail.get_payload()[0].get_payload(decode=True).decode('utf-8')
    assert credit_mail['Subject'] == 'Stornorechnung 26R001'
    assert 'die Stornorechnung 26R001' in body and 'verrechnet bzw. erstattet' in body
    assert 'überweisen' not in body                                                      # sie fordert nichts an
    assert [name for name, _data in smtp.attachments()] == ['26R001 Malermeister Pinsel.pdf']

    assert _mail(world, invoice_id) == {'success': True}                               # die Rechnung bleibt, wie sie war
    mail = smtp.sent[-1]
    assert mail['Subject'] == 'Rechnung T-100'
    assert 'überweisen' in mail.get_payload()[0].get_payload(decode=True).decode('utf-8')


# ── Berichtigung und Stornorechnung ──────────────────────────────────────────

def test_a_credit_note_never_carries_the_correction_notice_of_its_invoice(world):
    invoice_id = sent(world)
    assert save_open(world, invoice_id, orderNumber='B-99')['documentVersion'] == 2        # Fassung 2 trägt den Hinweis
    assert 'Berichtigte Fassung' in ' '.join(build_einvoice(world.db, *documents(world, invoice_id)).notes)
    storno_id = world.db.cancel_invoice(invoice_id, '2026-07-01', USER).storno_id
    credit = build_einvoice(world.db, *documents(world, storno_id))
    assert credit.notes == ['Storno zu Rechnung T-100 vom 07.06.2026']
    assert generate_invoice_documents(world.db, storno_id, overwrite_sent=True)['error'] is None
    text = pdf_text(stored_invoice_path(world.db, storno_id, 'pdf')[0])
    assert 'Berichtigte Fassung' not in text and 'Fassung:' not in text
    assert 'zu Rechnung Nr. T-100' in text
    assert 'Fassung' not in ET.tostring(ET.fromstring(generate_cii(*documents(world, storno_id), db=world.db)),
                                        encoding='unicode')


def test_the_reference_survives_a_renamed_original_in_the_stored_documents_only_as_text(world):
    """Der Bezug steht im Dokument als Text der Ursprungsrechnung (Nummer und Datum zur Zeit der Erzeugung)."""
    invoice_id, storno_id = cancelled(world)
    run_sql(world.db, "UPDATE Invoices SET InvoiceNumber = 'T-100-X' WHERE ID = ?", invoice_id)
    model = build_einvoice(world.db, *documents(world, storno_id))
    assert model.preceding_invoice_number == 'T-100-X'                              # gelesen aus CancelsInvoiceId
    assert stored_invoice_path(world.db, storno_id, 'pdf')[0].endswith('26R001 Malermeister Pinsel.pdf')
