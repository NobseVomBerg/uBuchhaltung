# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belegerkennung, Paket P2: E-Rechnung lesen (``receipt_reading``) – XRechnung-XML (UBL ``Invoice``/``CreditNote``),
ZUGFeRD-/Factur-X-XML (CII) und das XML, das in einer hybriden PDF steckt.

Die Erwartungen kommen aus dem neutralen Modell der App (``build_einvoice``), nicht aus festen Texten: Was die Erzeuger
schreiben, muss das Lesepaket als dasselbe Modell zurückliefern. Dazu die Grenzen der Eingabe (DTD, Entity-Bombe, Zip-Bombe im
Anhang, Größen, unpassende Werte) und die Form der Lesung (``to_row``, ``to_dict``/``from_dict``).

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import ast
import json
import pathlib
import time
import tracemalloc
import zlib
from xml.sax.saxutils import escape

import pytest
from pdfminer.pdftypes import PDFStream
from pdfminer.psparser import LIT

import receipt_reading
from export.cii_invoice import generate_cii
from export.einvoice_model import build_einvoice
from export.invoice_files import generate_invoice_documents, stored_invoice_document
from export.pdf_core import build_multi_page_pdf
from export.xrechnung_invoice import XRechnungGenerator
from importers import camt
from importers.safe_xml import UnsafeXml, parse_untrusted_xml
from receipt_reading import Reading, detect, einvoice, pdf_embedded, read_document
from tests.golden_cases import CASES, make_case
from tests.test_invoice_documents import _make_invoice, inv_db  # noqa: F401  (Fixture)
from tests.test_invoice_storno_documents import cancelled, documents, world  # noqa: F401  (Fixture)

OWN = ([], [])
ROOT = pathlib.Path(__file__).resolve().parent.parent
CAMT_FIXTURE = ROOT / 'tests' / 'fixtures' / 'camt' / 'camt053_v02.xml'
FIELDS = ('doc_date', 'gross_minor', 'net_minor', 'tax_minor', 'tax_rate', 'currency', 'sender_name', 'sender_vat_id',
          'invoice_number', 'kind')


# ── Dokumente von Hand ───────────────────────────────────────────────────────

def _el(tag, value, attributes=''):
    return '' if value is None else f'<{tag}{attributes}>{value}</{tag}>'


def cii(*, number='R-100', type_code='380', date='20260304', date_format='102', seller='Muster GmbH',
        vat_id='DE123456789', currency='EUR', net='100.00', tax='19.00', gross='119.00', due='119.00', rate='19.00',
        category='S', tax_total='default', breakdown='default', registrations=None) -> bytes:
    """Ein CII-Dokument; ``None`` lässt ein Element weg, ``tax_total`` und ``breakdown`` nehmen fertiges XML."""
    if tax_total == 'default':
        tax_total = _el('ram:TaxTotalAmount', tax, f' currencyID="{currency}"')
    if breakdown == 'default':
        breakdown = ('<ram:ApplicableTradeTax>' + _el('ram:CalculatedAmount', tax) + '<ram:TypeCode>VAT</ram:TypeCode>'
                     + _el('ram:BasisAmount', net) + _el('ram:CategoryCode', category)
                     + _el('ram:RateApplicablePercent', rate) + '</ram:ApplicableTradeTax>')
    if registrations is None:
        registrations = _el('ram:ID', vat_id, ' schemeID="VA"')
        registrations = f'<ram:SpecifiedTaxRegistration>{registrations}</ram:SpecifiedTaxRegistration>' if vat_id else ''
    stamp = '' if date is None else (
        f'<ram:IssueDateTime><udt:DateTimeString format="{date_format}">{date}</udt:DateTimeString></ram:IssueDateTime>')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>{_el('ram:ID', number)}{_el('ram:TypeCode', type_code)}{stamp}</rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>{_el('ram:Name', None if seller is None else escape(seller))}{registrations}</ram:SellerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeSettlement>
      {_el('ram:InvoiceCurrencyCode', currency)}
      {breakdown}
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        {_el('ram:TaxBasisTotalAmount', net)}{tax_total}{_el('ram:GrandTotalAmount', gross)}{_el('ram:DuePayableAmount', due)}
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>'''.encode('utf-8')


def ubl(*, root='Invoice', number='R-100', type_code='380', date='2026-03-04', seller='Muster GmbH', trade_name=None,
        vat_id='DE123456789', vat_scheme='VAT', currency='EUR', net='100.00', tax='19.00', gross='119.00',
        due='119.00', rate='19', category='S', tax_totals='default', amounts_in=None) -> bytes:
    """Ein UBL-Dokument (``Invoice`` oder ``CreditNote``); ``amounts_in``: ``currencyID`` der Summen (Standard: ``currency``)."""
    unit = f' currencyID="{amounts_in or currency}"'
    if tax_totals == 'default':
        tax_totals = (f'<cac:TaxTotal><cbc:TaxAmount currencyID="{currency}">{tax}</cbc:TaxAmount><cac:TaxSubtotal>'
                      f'<cbc:TaxableAmount currencyID="{currency}">{net}</cbc:TaxableAmount>'
                      f'<cbc:TaxAmount currencyID="{currency}">{tax}</cbc:TaxAmount><cac:TaxCategory>'
                      f'{_el("cbc:ID", category)}{_el("cbc:Percent", rate)}<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>'
                      '</cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>')
    code_tag = 'cbc:CreditNoteTypeCode' if root == 'CreditNote' else 'cbc:InvoiceTypeCode'
    scheme = (f'<cac:PartyTaxScheme><cbc:CompanyID>{vat_id}</cbc:CompanyID><cac:TaxScheme><cbc:ID>{vat_scheme}</cbc:ID>'
              '</cac:TaxScheme></cac:PartyTaxScheme>') if vat_id else ''
    legal = '' if seller is None else (
        f'<cac:PartyLegalEntity><cbc:RegistrationName>{escape(seller)}</cbc:RegistrationName></cac:PartyLegalEntity>')
    named = '' if trade_name is None else f'<cac:PartyName><cbc:Name>{escape(trade_name)}</cbc:Name></cac:PartyName>'
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{root}-2"
    xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
    xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  {_el('cbc:ID', number)}{_el('cbc:IssueDate', date)}{_el(code_tag, type_code)}{_el('cbc:DocumentCurrencyCode', currency)}
  <cac:AccountingSupplierParty><cac:Party>{named}{scheme}{legal}</cac:Party></cac:AccountingSupplierParty>
  {tax_totals}
  <cac:LegalMonetaryTotal>{_el('cbc:TaxExclusiveAmount', net, unit)}{_el('cbc:TaxInclusiveAmount', gross, unit)}{_el('cbc:PayableAmount', due, unit)}</cac:LegalMonetaryTotal>
</{root}>'''.encode('utf-8')


def read(data, name='beleg.xml'):
    return read_document(name, data, OWN)


def values(reading):
    return {name: getattr(reading, name) for name in FIELDS}


def expected(model):
    """Was die Lesung aus dem Modell der App haben muss (Beträge in Minor Units, Satz als Anteil – nur bei einem Satz)."""
    breakdown = model.tax_breakdown
    return {'doc_date': model.issue_date.isoformat(), 'gross_minor': model.totals.grand_total,
            'net_minor': model.totals.tax_basis, 'tax_minor': model.totals.tax_total,
            'tax_rate': float(breakdown[0].rate / 100) if len(breakdown) == 1 else None, 'currency': model.currency,
            'sender_name': model.seller.name, 'sender_vat_id': model.seller.vat_id or None,
            'invoice_number': model.number, 'kind': 'credit_note' if model.type_code == '381' else 'invoice'}


def expected_breakdown(model):
    return [{'rate': float(t.rate / 100), 'category': t.category, 'basis_minor': t.basis, 'tax_minor': t.amount}
            for t in model.tax_breakdown]


def both_syntaxes(db, invoice, items):
    """Dasselbe Dokument als CII und als UBL, wie die App es schreibt: ``(Quelle, Bytes)``."""
    return [('einvoice_cii', generate_cii(invoice, items, db=db)),
            ('einvoice_ubl', XRechnungGenerator().generate_xml(invoice, items, db=db).encode('utf-8'))]


# ── PDFs ─────────────────────────────────────────────────────────────────────

def pdf_with(xml):
    """Eine PDF der App (PDF/A-3b), die ``xml`` als ``factur-x.xml`` einbettet – beliebige Bytes."""
    return build_multi_page_pdf([[]], facturx_xml=xml)


def make_pdf(objects, catalog=b'', trailer=b''):
    """Eine kleine PDF mit klassischer Querverweistabelle von Hand: ``objects`` = ``{Nummer: Körper}`` (ab 3), der Katalog ist
    Objekt 1, ``catalog`` und ``trailer`` hängen sich an Katalog und Trailer."""
    bodies = {1: b'<< /Type /Catalog /Pages 2 0 R ' + catalog + b' >>', 2: b'<< /Type /Pages /Kids [] /Count 0 >>',
              **objects}
    out = bytearray(b'%PDF-1.7\n')
    offsets = {}
    for number in sorted(bodies):
        offsets[number] = len(out)
        out += b'%d 0 obj\n' % number + bodies[number] + b'\nendobj\n'
    size = max(bodies) + 1
    table = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % size
    for number in range(1, size):
        out += (b'%010d 00000 n \n' % offsets[number]) if number in offsets else b'0000000000 65535 f \n'
    out += b'trailer\n<< /Size %d /Root 1 0 R %s >>\nstartxref\n%d\n%%%%EOF\n' % (size, trailer, table)
    return bytes(out)


def data_stream(data, entries=b''):
    return b'<< ' + entries + b' /Length %d >>\nstream\n' % len(data) + data + b'\nendstream'


def flate_stream(xml, entries=b'/Type /EmbeddedFile'):
    return data_stream(zlib.compress(xml), entries + b' /Filter /FlateDecode')


def filespec(name=b'factur-x.xml', stream=5, keys=(b'F', b'UF'), files=b'F'):
    names = b' '.join(b'/' + key + b' (' + name + b')' for key in keys)
    return b'<< /Type /Filespec ' + names + b' /EF << /' + files + b' %d 0 R >> >>' % stream


def attached(xml, **spec):
    """Eine PDF, deren Katalog das XML unter ``AF`` führt (Flate, Name ``factur-x.xml``)."""
    return make_pdf({4: filespec(**spec), 5: flate_stream(xml)}, catalog=b'/AF [4 0 R]')


XML = cii()


# ── Rundlauf: was die App schreibt, liest das Paket als dasselbe Modell ──────

@pytest.mark.parametrize('case', sorted(CASES))
def test_the_documents_of_the_app_read_back_as_the_model(tmp_db, case):
    iid = make_case(tmp_db, case)
    invoice, items = tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid)
    model = build_einvoice(tmp_db, invoice, items)
    for source, data in both_syntaxes(tmp_db, invoice, items):
        reading = read(data)
        assert (reading.status, reading.source) == ('ok', source), source
        assert values(reading) == expected(model), source
        assert reading.raw['checks'] == {'sum': 'ok'} and reading.raw['tax_breakdown'] == expected_breakdown(model)
        assert reading.text is None and reading.notes == []
        assert reading.raw['fields']['gross_minor'] == {'value': model.totals.grand_total, 'source': 'einvoice',
                                                        'ref': 'BT-112'}
        assert reading.raw['fields']['due_payable_minor']['ref'] == 'BT-115'
        assert Reading.from_dict(reading.to_dict()) == reading


def test_the_amounts_are_minor_units_of_the_app_not_cent(tmp_db):
    iid = make_case(tmp_db, 'std19')
    invoice, items = tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid)
    for _source, data in both_syntaxes(tmp_db, invoice, items):
        reading = read(data)
        assert (reading.net_minor, reading.tax_minor, reading.gross_minor) == (1495000, 284100, 1779100)   # 149,50 €
        assert reading.tax_rate == 0.19 and reading.doc_date == '2026-03-04' and reading.invoice_number == 'G26001'


def test_without_vat_disclosure_the_tax_is_zero_and_the_sum_proof_holds(tmp_db):
    """§ 19 UStG: Die App schreibt Kategorie E, Satz 0, Gesamtbetrag der Steuer 0,00."""
    iid = make_case(tmp_db, 'ku')
    invoice, items = tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid)
    for source, data in both_syntaxes(tmp_db, invoice, items):
        reading = read(data)
        assert (reading.tax_minor, reading.tax_rate, reading.net_minor, reading.gross_minor) == (
            0, 0.0, 1000000, 1000000), source
        assert reading.raw['checks'] == {'sum': 'ok'}
        assert reading.raw['tax_breakdown'] == [{'rate': 0.0, 'category': 'E', 'basis_minor': 1000000, 'tax_minor': 0}]
        assert reading.raw['fields']['tax_minor']['ref'] == 'BT-110'


def test_the_pdf_the_app_makes_reads_through_its_embedded_cii(inv_db):
    iid = _make_invoice(inv_db, number='26R010')
    generate_invoice_documents(inv_db, iid)
    data, name, error = stored_invoice_document(inv_db, iid, 'pdf')
    assert error is None and name == '26R010 Malermeister Pinsel.pdf'
    invoice, items = inv_db.get_invoice_by_id(iid), inv_db.get_invoice_items(iid)
    reading = read_document(name, data, OWN)
    assert (reading.status, reading.source) == ('ok', 'einvoice_cii')
    assert values(reading) == expected(build_einvoice(inv_db, invoice, items))
    assert (reading.gross_minor, reading.invoice_number) == (1190000, '26R010')
    assert pdf_embedded.find_embedded_einvoice(data) == (generate_cii(invoice, items, db=inv_db), None)


def test_a_credit_note_is_a_credit_note_with_positive_amounts_in_both_syntaxes(world):
    _invoice_id, storno_id = cancelled(world)
    invoice, items = documents(world, storno_id)
    model = build_einvoice(world.db, invoice, items)
    assert model.type_code == '381'
    for source, data in both_syntaxes(world.db, invoice, items):
        reading = read(data)
        assert (reading.status, reading.source, reading.kind) == ('ok', source, 'credit_note')
        assert (reading.net_minor, reading.tax_minor, reading.gross_minor) == (1000000, 190000, 1190000)
        assert values(reading) == expected(model) and reading.raw['checks'] == {'sum': 'ok'}
        assert reading.raw['fields']['kind'] == {'value': 'credit_note', 'source': 'einvoice', 'ref': 'BT-3'}


def test_mixed_rates_leave_the_rate_empty_and_list_the_breakdown(tmp_db, monkeypatch):
    monkeypatch.setitem(CASES, 'mixed', dict(
        invoice={'invoice_number': 'G26009', 'tax_rate': 19.0, 'sum_net': 300.00, 'tax_amount': 33.00,
                 'sum_gross': 333.00, 'amount_due': 333.00},
        items=[('Beratung', 1, 'Std.', 100.00, 100.00, 19.0), ('Fachbuch', 2, 'Stk.', 100.00, 200.00, 7.0)]))
    iid = make_case(tmp_db, 'mixed')
    invoice, items = tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid)
    model = build_einvoice(tmp_db, invoice, items)
    assert [(t.category, t.rate) for t in model.tax_breakdown] == [('S', 7), ('S', 19)] and model.warnings == []
    for source, data in both_syntaxes(tmp_db, invoice, items):
        reading = read(data)
        assert reading.status == 'ok' and reading.tax_rate is None, source
        assert (reading.net_minor, reading.tax_minor, reading.gross_minor) == (3000000, 330000, 3330000)
        assert reading.raw['tax_breakdown'] == expected_breakdown(model) and len(reading.raw['tax_breakdown']) == 2
        assert 'tax_rate' not in reading.raw['fields'] and reading.raw['checks'] == {'sum': 'ok'}
        assert values(reading) == expected(model)


# ── Dokumente von Hand: Felder, Herkunft, Summenprobe ────────────────────────

def test_a_hand_written_document_is_read_with_its_origin_in_both_syntaxes():
    for data, source in ((cii(), 'einvoice_cii'), (ubl(), 'einvoice_ubl')):
        reading = read(data)
        assert (reading.status, reading.source, reading.kind) == ('ok', source, 'invoice')
        assert values(reading) == {
            'doc_date': '2026-03-04', 'gross_minor': 1190000, 'net_minor': 1000000, 'tax_minor': 190000,
            'tax_rate': 0.19, 'currency': 'EUR', 'sender_name': 'Muster GmbH', 'sender_vat_id': 'DE123456789',
            'invoice_number': 'R-100', 'kind': 'invoice'}
        refs = {name: entry['ref'] for name, entry in reading.raw['fields'].items()}
        assert refs == {'invoice_number': 'BT-1', 'doc_date': 'BT-2', 'kind': 'BT-3', 'currency': 'BT-5',
                        'sender_name': 'BT-27', 'sender_vat_id': 'BT-31', 'net_minor': 'BT-109', 'tax_minor': 'BT-110',
                        'gross_minor': 'BT-112', 'due_payable_minor': 'BT-115', 'tax_rate': 'BG-23'}
        assert all(entry['source'] == 'einvoice' for entry in reading.raw['fields'].values())


def test_the_sum_proof_says_ok_mismatch_or_incomplete():
    for build in (cii, ubl):
        assert read(build()).raw['checks'] == {'sum': 'ok'}
        assert read(build(gross='119.01')).raw['checks'] == {'sum': 'mismatch'}
        assert read(build(gross=None)).raw['checks'] == {'sum': 'incomplete'}
        assert read(build(net=None)).raw['checks'] == {'sum': 'incomplete'}


def test_a_credit_note_root_or_type_381_makes_a_credit_note():
    assert read(cii(type_code='381')).kind == 'credit_note'
    assert read(cii(type_code='380')).kind == 'invoice' and read(cii(type_code=None)).kind == 'invoice'
    assert read(ubl(type_code='381')).kind == 'credit_note'                       # Invoice mit Typ 381
    credit = read(ubl(root='CreditNote', type_code='380'))                        # die Wurzel gilt
    assert credit.kind == 'credit_note' and credit.source == 'einvoice_ubl'
    assert credit.raw['fields']['kind']['ref'] == 'BT-3'
    assert read(ubl(root='CreditNote', type_code=None)).raw['fields']['kind'] == {
        'value': 'credit_note', 'source': 'einvoice', 'ref': 'root'}
    assert read(cii(type_code=None)).raw['fields']['kind']['ref'] == 'default'
    assert read(ubl(root='CreditNote', gross='-5.00', net='-5.00', tax='0.00', rate='0')).gross_minor == -50000


def test_a_missing_total_of_tax_with_all_zero_breakdowns_has_tax_zero():
    reading = read(cii(net='100.00', gross='100.00', tax='0.00', rate='0.00', category='E', tax_total=''))
    assert (reading.tax_minor, reading.tax_rate, reading.gross_minor) == (0, 0.0, 1000000)
    assert reading.raw['fields']['tax_minor'] == {'value': 0, 'source': 'einvoice', 'ref': 'BG-23'}
    assert reading.raw['checks'] == {'sum': 'ok'} and reading.notes == []


def test_a_missing_total_of_tax_is_never_guessed_when_the_breakdown_shows_tax_or_is_missing():
    shown = read(cii(tax_total=''))                                       # 19 % mit 19,00 € Steuer
    assert shown.tax_minor is None and shown.tax_rate == 0.19 and shown.raw['checks'] == {'sum': 'incomplete'}
    bare = read(cii(tax_total='', breakdown=''))
    assert bare.tax_minor is None and bare.tax_rate is None and bare.raw['checks'] == {'sum': 'incomplete'}
    unreadable = read(cii(tax_total='', tax='0.0x', rate='0'))             # der Betrag der Aufschlüsselung passt nicht
    assert unreadable.tax_minor is None and 'bad_tax_breakdown' in unreadable.notes
    mixed = read(cii(tax_total='', breakdown=(
        '<ram:ApplicableTradeTax><ram:CalculatedAmount>0.00</ram:CalculatedAmount><ram:TypeCode>VAT</ram:TypeCode>'
        '<ram:CategoryCode>E</ram:CategoryCode><ram:RateApplicablePercent>0</ram:RateApplicablePercent></ram:ApplicableTradeTax>'
        '<ram:ApplicableTradeTax><ram:CalculatedAmount>7.00</ram:CalculatedAmount><ram:TypeCode>VAT</ram:TypeCode>'
        '<ram:CategoryCode>S</ram:CategoryCode><ram:RateApplicablePercent>7</ram:RateApplicablePercent></ram:ApplicableTradeTax>')))
    assert mixed.tax_minor is None and mixed.tax_rate is None


def test_a_total_of_tax_that_is_there_but_wrong_is_not_replaced_by_zero():
    reading = read(cii(net='100.00', gross='100.00', tax='0.00', rate='0.00', category='E',
                       tax_total='<ram:TaxTotalAmount currencyID="EUR">zero</ram:TaxTotalAmount>'))
    assert reading.tax_minor is None and 'bad_tax_minor' in reading.notes
    assert reading.raw['checks'] == {'sum': 'incomplete'}


def test_the_total_of_tax_in_the_document_currency_is_the_one_that_counts():
    both = ('<ram:TaxTotalAmount currencyID="USD">22.00</ram:TaxTotalAmount>'
            '<ram:TaxTotalAmount currencyID="EUR">19.00</ram:TaxTotalAmount>')
    assert read(cii(tax_total=both)).tax_minor == 190000
    ambiguous = read(cii(tax_total='<ram:TaxTotalAmount currencyID="EUR">19.00</ram:TaxTotalAmount>' * 2))
    assert ambiguous.tax_minor is None and 'bad_tax_minor' in ambiguous.notes
    elsewhere = read(cii(tax_total='<ram:TaxTotalAmount currencyID="USD">19.00</ram:TaxTotalAmount>'
                                   '<ram:TaxTotalAmount currencyID="CHF">19.00</ram:TaxTotalAmount>'))
    assert elsewhere.tax_minor is None and 'bad_tax_minor' in elsewhere.notes
    other = ('<cac:TaxTotal><cbc:TaxAmount currencyID="USD">22.00</cbc:TaxAmount></cac:TaxTotal>'
             '<cac:TaxTotal><cbc:TaxAmount currencyID="EUR">19.00</cbc:TaxAmount><cac:TaxSubtotal>'
             '<cbc:TaxableAmount currencyID="EUR">100.00</cbc:TaxableAmount><cbc:TaxAmount currencyID="EUR">19.00</cbc:TaxAmount>'
             '<cac:TaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>19</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>'
             '</cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>')
    chosen = read(ubl(tax_totals=other))
    assert chosen.tax_minor == 190000 and chosen.tax_rate == 0.19 and chosen.raw['checks'] == {'sum': 'ok'}


def test_an_amount_in_another_currency_than_the_document_is_not_read():
    reading = read(ubl(amounts_in='USD'))
    assert (reading.net_minor, reading.gross_minor) == (None, None) and reading.currency == 'EUR'
    assert {'bad_net_minor', 'bad_gross_minor'} <= set(reading.notes) and reading.tax_minor == 190000


def test_the_seller_is_read_by_its_registered_name_then_by_its_trade_name():
    assert read(ubl(seller='Muster GmbH', trade_name='Muster')).sender_name == 'Muster GmbH'
    trade = read(ubl(seller=None, trade_name='Muster'))
    assert trade.sender_name == 'Muster' and trade.raw['fields']['sender_name']['ref'] == 'BT-28'
    assert read(ubl(seller=None)).sender_name is None


def test_the_vat_id_is_the_vat_scheme_not_a_tax_number():
    assert read(ubl(vat_scheme='TAX')).sender_vat_id is None
    both = ('<ram:SpecifiedTaxRegistration><ram:ID schemeID="FC">12/345/67890</ram:ID></ram:SpecifiedTaxRegistration>'
            '<ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">DE123456789</ram:ID></ram:SpecifiedTaxRegistration>')
    assert read(cii(registrations=both)).sender_vat_id == 'DE123456789'
    assert read(cii(registrations=both.split('</ram:SpecifiedTaxRegistration>')[0]
                    + '</ram:SpecifiedTaxRegistration>')).sender_vat_id is None


def test_texts_are_stripped_collapsed_and_cleaned():
    reading = read(cii(seller='  Muster\n   GmbH​\x85 ', number=' R-\t100 '))
    assert reading.sender_name == 'Muster GmbH' and reading.invoice_number == 'R- 100'
    assert read(cii(seller='   ', number='\n')).sender_name is None


def test_an_amount_at_the_upper_bound_is_read_and_fits_the_checked_form():
    reading = read(cii(gross='999999999999.99', net='999999999999.99', tax='0.00', rate='0', category='E'))
    assert reading.gross_minor == 9999999999999900
    assert Reading.from_dict(reading.to_dict()) == reading


# ── Unpassende Werte: Feld leer, Befund gesetzt, nie geraten ─────────────────

BAD_AMOUNTS = ['1e5', '12.345', '١٢٣', '1,00', '1234567890123', '1234567890123.00', '+5', '1_000', '.5', '5.', '-',
               'nan', 'inf', '１２', '1 000', '0x10', '1e-2', '--5', '1.2.3', '€5']


@pytest.mark.parametrize('value', BAD_AMOUNTS)
def test_an_unfit_amount_leaves_the_field_empty_in_both_syntaxes(value):
    for build in (cii, ubl):
        reading = read(build(gross=value))
        assert reading.gross_minor is None, (build.__name__, value)
        assert reading.status == 'ok' and 'bad_gross_minor' in reading.notes
        assert 'gross_minor' not in reading.raw['fields'] and reading.raw['checks'] == {'sum': 'incomplete'}
        assert reading.net_minor == 1000000                                   # die anderen Felder bleiben
        assert Reading.from_dict(reading.to_dict()) == reading


@pytest.mark.parametrize('value', ['20261340', '2026-03-04', '2026030', '202603041', '18991231', '22010101', '٢٠٢٦٠٣٠٤',
                                   '20260230', '2026 0304', ''])
def test_an_unfit_cii_date_is_not_read(value):
    reading = read(cii(date=value))
    assert reading.doc_date is None and 'doc_date' not in reading.raw['fields']
    assert ('bad_doc_date' in reading.notes) is bool(value)                  # leer = nicht angegeben


@pytest.mark.parametrize('value', ['2026-13-40', '20260304', '2026-02-30', '1899-12-31', '2201-01-01', '2026-3-4',
                                   '2026-03-04Z', '2026/03/04', '٢٠٢٦-٠٣-٠٤', '2026-03-04T10:00:00'])
def test_an_unfit_ubl_date_is_not_read(value):
    reading = read(ubl(date=value))
    assert reading.doc_date is None and 'bad_doc_date' in reading.notes


def test_only_the_cii_date_format_102_is_read():
    reading = read(cii(date='202603041200', date_format='203'))
    assert reading.doc_date is None and 'bad_doc_date' in reading.notes
    assert read(cii(date='20260304', date_format='102')).doc_date == '2026-03-04'
    assert read(cii(date='19000101')).doc_date == '1900-01-01' and read(cii(date='22001231')).doc_date == '2200-12-31'
    assert read(cii(date='20240229')).doc_date == '2024-02-29'                  # Schaltjahr


@pytest.mark.parametrize('value', ['eur', 'EURO', '€', 'E1R', 'EU', '٣٣٣'])
def test_an_unfit_currency_is_not_read(value):
    reading = read(cii(currency=value))
    assert reading.currency is None and 'bad_currency' in reading.notes
    assert reading.gross_minor == 1190000                                      # ohne Währung ist kein Betrag unlesbar


@pytest.mark.parametrize('value', ['19,0', '101', '100.0001', '1e1', '-7', '19.00001', '١٩', '1000'])
def test_an_unfit_rate_is_not_read(value):
    reading = read(cii(rate=value))
    assert reading.tax_rate is None and 'bad_tax_breakdown' in reading.notes
    assert reading.raw['tax_breakdown'][0]['rate'] is None


def test_rates_are_normalised_to_a_hundredth_of_a_percentage_point():
    for value, rate in (('19', 0.19), ('19.00', 0.19), ('7.0', 0.07), ('5.5', 0.055), ('19.004', 0.19), ('19.005', 0.1901),
                        ('0', 0.0), ('100', 1.0), ('100.0000', 1.0)):
        assert read(cii(rate=value)).tax_rate == rate, value


def test_an_unknown_category_is_not_kept():
    reading = read(cii(category='s'))
    assert reading.raw['tax_breakdown'][0]['category'] is None and 'bad_tax_breakdown' in reading.notes


def test_more_than_twenty_breakdown_entries_are_cut_and_say_so():
    entry = ('<ram:ApplicableTradeTax><ram:CalculatedAmount>0.00</ram:CalculatedAmount><ram:TypeCode>VAT</ram:TypeCode>'
             '<ram:BasisAmount>1.00</ram:BasisAmount><ram:CategoryCode>Z</ram:CategoryCode>'
             '<ram:RateApplicablePercent>0</ram:RateApplicablePercent></ram:ApplicableTradeTax>')
    reading = read(cii(breakdown=entry * 25))
    assert len(reading.raw['tax_breakdown']) == 20 and 'raw_truncated' in reading.notes
    assert reading.tax_rate is None                          # nicht alle Einträge sind gesehen: kein einzelner Satz


# ── Keine E-Rechnung ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('data', [
    b'<root/>', b'<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"/>',
    b'<rsm:CrossIndustryDocument xmlns:rsm="urn:ferd:CrossIndustryDocument:invoice:1p0"/>',
    b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"/>',
    b'<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>',
    b'<x:Other xmlns:x="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"/>',
    b'<CrossIndustryInvoice/>'])
def test_other_xml_is_unsupported(data):
    reading = read(data)
    assert (reading.status, reading.source, reading.notes) == ('unsupported', None, ['not_einvoice'])
    assert (reading.gross_minor, reading.text, reading.raw) == (None, None, {})


def test_a_cii_root_in_the_right_namespace_with_nothing_in_it_is_empty_not_unsupported():
    reading = read(b'<CrossIndustryInvoice xmlns="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"/>')
    assert (reading.status, reading.source) == ('empty', 'einvoice_cii')


def test_a_camt_statement_is_not_an_einvoice():
    reading = read(CAMT_FIXTURE.read_bytes())
    assert (reading.status, reading.notes) == ('unsupported', ['not_einvoice'])


def test_a_document_without_any_readable_field_is_empty_and_carries_no_kind():
    reading = read(b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>')
    assert (reading.status, reading.source, reading.kind, reading.raw) == ('empty', 'einvoice_ubl', None, {})
    assert values(reading) == dict.fromkeys(FIELDS)
    assert read(cii(number=None, date=None, seller=None, vat_id=None, currency=None, net=None, gross=None, due=None,
                    breakdown='', tax_total='')).status == 'empty'


# ── Bösartig und kaputt ──────────────────────────────────────────────────────

DTD_XML = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>'
EXTERNAL_XML = b'<?xml version="1.0"?><!DOCTYPE a SYSTEM "file:///nonexistent/secret.dtd"><a/>'
PARAMETER_XML = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY % p SYSTEM "http://invalid.test/p">%p;]><a/>'


def entity_bomb():
    levels = ['<!ENTITY e0 "lol">'] + [f'<!ENTITY e{i} "' + f'&e{i - 1};' * 10 + '">' for i in range(1, 10)]
    return ('<?xml version="1.0"?><!DOCTYPE bomb [' + ''.join(levels) + ']><bomb>&e9;</bomb>').encode('utf-8')


@pytest.mark.parametrize('payload', [DTD_XML, EXTERNAL_XML, PARAMETER_XML, entity_bomb(),
                                     DTD_XML.decode().replace('<?xml version="1.0"?>',
                                                              '<?xml version="1.0" encoding="UTF-16"?>').encode('utf-16')],
                         ids=['entity', 'external', 'parameter', 'bomb', 'utf-16'])
def test_a_dtd_or_entity_is_refused_fast_and_never_expanded(payload):
    started = time.perf_counter()
    reading = read(payload)
    assert time.perf_counter() - started < 2
    assert (reading.status, reading.source, reading.notes) == ('failed', None, ['xml_dtd'])


def test_xml_that_is_not_well_formed_is_failed_without_an_error_text():
    for payload in (b'<a>', b'<a></b>', b'<a/><b/>', b'<?xml version="1.0" encoding="shift_jis"?><a/>', b'<'):
        reading = read(payload)
        assert (reading.status, reading.notes) == ('failed', ['xml_invalid']), payload
        assert reading.raw == {} and reading.text is None


def test_xml_over_the_limit_is_too_large(monkeypatch):
    monkeypatch.setattr(receipt_reading, 'MAX_XML_BYTES', 100)
    reading = read(cii())
    assert (reading.status, reading.source, reading.gross_minor) == ('too_large', None, None)
    assert einvoice.read_einvoice_xml(cii()).status == 'too_large'
    assert read(b'<a/>' + b' ' * 10).status == 'unsupported'                  # darunter wird gelesen


def test_a_file_over_the_read_limit_is_too_large_before_anything_is_looked_at(monkeypatch):
    monkeypatch.setattr(receipt_reading, 'MAX_READ_BYTES', 100)
    for name, data in (('a.xml', cii()), ('a.pdf', pdf_with(XML)), ('a.png', b'\x89PNG\r\n\x1a\n' + b'0' * 200),
                       ('a.txt', b'x' * 200)):
        assert read_document(name, data, OWN).status == 'too_large', name
    assert read_document('a.txt', b'x' * 100, OWN).status == 'unsupported'


def test_the_shared_xml_guard_names_its_reason():
    for data, limit, reason in ((b'<a/>', 3, 'too_large'), (DTD_XML, 1000, 'dtd'), (b'<a>', 1000, 'invalid')):
        with pytest.raises(UnsafeXml) as caught:
            parse_untrusted_xml(data, limit)
        assert caught.value.reason == reason
    assert parse_untrusted_xml(b'<a/>', 4).tag == 'a'
    with pytest.raises(UnsafeXml) as caught:
        parse_untrusted_xml(b'<a>', 1000)
    assert caught.value.detail                                        # der Befund des Parsers, für die CAMT-Meldung


def test_camt_keeps_its_messages_on_top_of_the_shared_guard(monkeypatch):
    with pytest.raises(camt.CamtError, match=r'^XML mit DTD/Entity-Deklaration wird aus Sicherheitsgründen nicht gelesen '
                                              r'\(CAMT enthält keine\)\.$'):
        camt.parse_xml(DTD_XML)
    with pytest.raises(camt.CamtError, match=r'^Kein gültiges XML: .+'):
        camt.parse_xml(b'<a>')
    monkeypatch.setattr(camt, 'MAX_XML_BYTES', 10)
    with pytest.raises(camt.CamtError, match=r'^XML-Datei ist zu groß\.$'):
        camt.parse_xml(b'<a>' + b'0' * 20)


def test_read_document_never_raises_and_gives_no_error_text(monkeypatch):
    def explode(_data):
        raise RuntimeError('Geheimnis im Ausnahmetext')
    monkeypatch.setattr(einvoice, 'read_einvoice_xml', explode)
    reading = read(cii())
    assert (reading.status, reading.notes) == ('failed', ['read_error'])
    assert 'Geheimnis' not in json.dumps(reading.to_dict()) and 'Geheimnis' not in str(reading.to_row())
    for name, data in ((None, None), ('a.pdf', 'text'), (5, b''), ('a.xml', None), ('a.pdf', 12)):
        assert read_document(name, data, OWN).status in ('failed', 'unsupported')


# ── PDF: eingebettetes XML ───────────────────────────────────────────────────

def test_the_embedded_xml_of_a_pdf_is_read_as_the_xml_is():
    reading = read(pdf_with(XML), 'rechnung.pdf')
    assert (reading.status, reading.source) == ('ok', 'einvoice_cii')
    assert values(reading) == values(read(XML))
    assert reading.raw == read(XML).raw and reading.notes == []


def test_a_pdf_without_an_attachment_goes_on_to_the_text_way():
    plain = build_multi_page_pdf([[]])
    reading = read(plain, 'plain.pdf')
    assert (reading.status, reading.source, reading.notes) == ('no_text', None, [])          # eine leere Seite: kein Text
    assert pdf_embedded.find_embedded_einvoice(plain) == (None, None)


def test_text_rules_get_the_name_the_bytes_the_own_data_the_deadline_and_the_notes(monkeypatch):
    seen = []

    def fake(name, data, own, deadline, notes):
        seen.append((name, data, own, deadline, list(notes)))
        return Reading(status='ok', source='text', gross_minor=5)
    monkeypatch.setattr(receipt_reading, '_read_pdf_text', fake)
    plain = build_multi_page_pdf([[]])
    reading = read_document('plain.pdf', plain, (['Muster GmbH'], ['DE123456789']), deadline=12.5)
    assert reading.source == 'text' and seen == [('plain.pdf', plain, (['Muster GmbH'], ['DE123456789']), 12.5, [])]
    broken = pdf_with(DTD_XML)
    read_document('broken.pdf', broken, OWN)
    assert seen[-1][4] == ['xml_dtd']                     # was der E-Rechnungs-Weg fand, geht an den Textweg weiter
    read_document('plain.pdf', plain, OWN)
    assert seen[-1][3] is None


def test_a_dtd_in_the_attachment_gives_no_einvoice_reading_but_a_note():
    reading = read(pdf_with(DTD_XML), 'a.pdf')
    assert (reading.status, reading.source, reading.gross_minor) == ('no_text', None, None)
    assert reading.notes == ['xml_dtd']
    assert pdf_embedded.find_embedded_einvoice(pdf_with(DTD_XML))[1] is None   # der Anhang ist gefunden, der Inhalt ungeprüft


def test_an_attachment_that_is_not_an_einvoice_goes_on_to_the_text_way():
    reading = read(pdf_with(CAMT_FIXTURE.read_bytes()), 'a.pdf')
    assert (reading.status, reading.source) == ('no_text', None) and reading.notes == ['not_einvoice']
    reading = read(pdf_with(b'<a>'), 'a.pdf')
    assert reading.notes == ['xml_invalid']


def test_an_einvoice_without_a_readable_field_goes_on_to_the_text_way_with_a_note():
    """Das XML im Anhang ist eine E-Rechnung, nennt aber kein lesbares Feld: Der Text der PDF wird gelesen (hier eine leere
    Seite), und der Befund sagt, warum der Anhang nicht gilt."""
    reading = read(pdf_with(b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'), 'a.pdf')
    assert (reading.status, reading.source, reading.notes) == ('no_text', None, ['einvoice_empty'])
    bad = read(pdf_with(ubl(gross='1e5', net=None, tax=None, tax_totals='', date=None, seller=None, vat_id=None, number=None,
                            currency=None, rate=None)), 'a.pdf')
    assert bad.notes == ['bad_gross_minor', 'einvoice_empty']                  # die Befunde des Anhangs gehen mit


def test_a_zip_bomb_in_the_attachment_is_refused_without_a_mountain_of_memory(monkeypatch):
    monkeypatch.setattr(receipt_reading, 'MAX_XML_BYTES', 64 * 1024)
    bomb = pdf_with(b'\x00' * (20 * 1024 * 1024))                              # wenige KB in der Datei, 20 MiB entpackt
    assert len(bomb) < 100 * 1024
    tracemalloc.start()
    try:
        found = pdf_embedded.find_embedded_einvoice(bomb)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert found == (None, 'too_large') and peak < 4 * 1024 * 1024
    reading = read(bomb, 'a.pdf')
    assert (reading.status, reading.source, reading.notes) == ('no_text', None, ['too_large'])


def test_an_attachment_right_at_the_limit_is_read_and_one_byte_more_is_too_large(monkeypatch):
    xml = cii()
    monkeypatch.setattr(receipt_reading, 'MAX_XML_BYTES', len(xml))
    assert pdf_embedded.find_embedded_einvoice(pdf_with(xml)) == (xml, None)
    assert read(pdf_with(xml), 'a.pdf').status == 'ok'
    monkeypatch.setattr(receipt_reading, 'MAX_XML_BYTES', len(xml) - 1)
    assert pdf_embedded.find_embedded_einvoice(pdf_with(xml)) == (None, 'too_large')


def _stream(raw, **attributes):
    return PDFStream(attributes, raw)


FLATE = LIT('FlateDecode')


def test_the_bounded_inflate_reads_plain_and_flate_streams_up_to_the_limit():
    assert pdf_embedded._bounded_inflate(_stream(b'<a/>'), 10) == b'<a/>'
    for filters in (FLATE, [FLATE], LIT('Fl')):
        assert pdf_embedded._bounded_inflate(_stream(zlib.compress(b'<a/>'), Filter=filters), 4) == b'<a/>'
    assert pdf_embedded._bounded_inflate(_stream(zlib.compress(b'<a/>'), Filter=FLATE, DecodeParms=None), 4) == b'<a/>'
    assert pdf_embedded._bounded_inflate(_stream(zlib.compress(b'<a/>'), Filter=FLATE, DecodeParms={}), 4) == b'<a/>'
    assert pdf_embedded._bounded_inflate(_stream(zlib.compress(b''), Filter=FLATE), 4) == b''
    assert pdf_embedded._bounded_inflate(_stream(b''), 0) == b''


def test_the_bounded_inflate_refuses_what_it_cannot_unpack_safely():
    packed = zlib.compress(b'<a>' + b'x' * 100 + b'</a>')
    chain = _stream(packed, Filter=[LIT('ASCIIHexDecode'), FLATE])
    assert pdf_embedded._bounded_inflate(chain, 1000) is None                  # eine Kette
    assert pdf_embedded._bounded_inflate(_stream(packed, Filter=[FLATE, FLATE]), 1000) is None
    for predictor in (12, 2, 1):
        stream = _stream(packed, Filter=FLATE, DecodeParms={'Predictor': predictor, 'Columns': 5})
        assert pdf_embedded._bounded_inflate(stream, 1000) is None, predictor
    assert pdf_embedded._bounded_inflate(_stream(packed, Filter=LIT('LZWDecode')), 1000) is None
    assert pdf_embedded._bounded_inflate(_stream(b'kein zlib', Filter=FLATE), 1000) is None
    assert pdf_embedded._bounded_inflate(_stream(packed[:-6], Filter=FLATE), 1000) is None   # abgeschnitten
    decoded = _stream(b'x')
    decoded.get_data()
    assert pdf_embedded._bounded_inflate(decoded, 1000) is None                # schon entpackt: Rohdaten sind weg


def test_the_bounded_inflate_stops_at_the_limit_and_never_decodes_the_whole_stream():
    bomb = zlib.compress(b'\x00' * 5_000_000)
    stream = _stream(bomb, Filter=FLATE)
    with pytest.raises(pdf_embedded._TooLarge):
        pdf_embedded._bounded_inflate(stream, 1000)
    assert stream.data is None and stream.rawdata == bomb                      # get_data() wurde nie gerufen
    with pytest.raises(pdf_embedded._TooLarge):
        pdf_embedded._bounded_inflate(_stream(b'x' * 11), 10)                  # ohne Filter zählt die Länge der Rohdaten
    exact = zlib.compress(b'0123456789')
    assert pdf_embedded._bounded_inflate(_stream(exact, Filter=FLATE), 10) == b'0123456789'
    with pytest.raises(pdf_embedded._TooLarge):
        pdf_embedded._bounded_inflate(_stream(exact, Filter=FLATE), 9)


def test_an_attachment_with_a_filter_chain_or_a_predictor_is_not_taken():
    stream = data_stream(zlib.compress(XML), b'/Filter [/ASCIIHexDecode /FlateDecode]')
    chain = make_pdf({4: filespec(), 5: stream}, catalog=b'/AF [4 0 R]')
    assert pdf_embedded.find_embedded_einvoice(chain) == (None, 'attachment_unreadable')
    predicted = make_pdf({4: filespec(), 5: data_stream(zlib.compress(XML), b'/Filter /FlateDecode '
                                                        b'/DecodeParms << /Predictor 12 /Columns 5 >>')},
                         catalog=b'/AF [4 0 R]')
    assert pdf_embedded.find_embedded_einvoice(predicted) == (None, 'attachment_unreadable')
    assert read(predicted, 'a.pdf').notes == ['attachment_unreadable']
    no_files = make_pdf({4: b'<< /Type /Filespec /F (factur-x.xml) >>'}, catalog=b'/AF [4 0 R]')
    assert pdf_embedded.find_embedded_einvoice(no_files) == (None, 'attachment_unreadable')
    assert pdf_embedded.find_embedded_einvoice(attached(b'<a/>')) == (b'<a/>', None)     # ohne Prüfung des Inhalts


def test_an_unfiltered_attachment_is_read_too():
    plain = make_pdf({4: filespec(), 5: data_stream(XML, b'/Type /EmbeddedFile')}, catalog=b'/AF [4 0 R]')
    assert pdf_embedded.find_embedded_einvoice(plain) == (XML, None)
    assert read(plain, 'a.pdf').source == 'einvoice_cii'


@pytest.mark.parametrize('name, keys, taken', [
    (b'factur-x.xml', (b'F', b'UF'), True), (b'Factur-X.XML', (b'F',), True), (b'ZUGFeRD-invoice.xml', (b'UF',), True),
    (b'xrechnung.xml', (b'F',), True), (b'rechnungen/2026/factur-x.xml', (b'F',), True),
    (b'rechnungen\\\\factur-x.xml', (b'UF',), True),             # im PDF-Text steht der Rückstrich doppelt: ein Pfadtrenner
    (b'other.xml', (b'F', b'UF'), False), (b'factur-x.xml.bak', (b'F',), False),
    (b'my-factur-x.xml', (b'F',), False), (b'', (b'F',), False), (b'ZUGFeRD-invoice.pdf', (b'F',), False)])
def test_an_attachment_is_taken_by_the_base_name_of_its_file(name, keys, taken):
    found = pdf_embedded.find_embedded_einvoice(attached(XML, name=name, keys=keys))
    assert found == ((XML, None) if taken else (None, None))


def test_the_unicode_name_wins_over_the_plain_one():
    spec = b'<< /Type /Filespec /UF (other.xml) /F (factur-x.xml) /EF << /F 5 0 R >> >>'
    assert pdf_embedded.find_embedded_einvoice(
        make_pdf({4: spec, 5: flate_stream(XML)}, catalog=b'/AF [4 0 R]')) == (None, None)
    spec = b'<< /Type /Filespec /UF (factur-x.xml) /F (other.xml) /EF << /F 5 0 R >> >>'
    assert pdf_embedded.find_embedded_einvoice(
        make_pdf({4: spec, 5: flate_stream(XML)}, catalog=b'/AF [4 0 R]')) == (XML, None)


def test_the_stream_comes_from_ef_f_and_else_from_ef_uf():
    assert pdf_embedded.find_embedded_einvoice(attached(XML, files=b'UF')) == (XML, None)
    spec = b'<< /Type /Filespec /F (factur-x.xml) /EF << /F 6 0 R /UF 5 0 R >> >>'
    both = make_pdf({4: spec, 5: flate_stream(b'<a/>'), 6: flate_stream(XML)}, catalog=b'/AF [4 0 R]')
    assert pdf_embedded.find_embedded_einvoice(both) == (XML, None)


def test_the_name_tree_is_searched_with_its_kids():
    flat = make_pdf({4: filespec(), 5: flate_stream(XML)}, catalog=b'/Names << /EmbeddedFiles << /Names [(a) 4 0 R] >> >>')
    assert pdf_embedded.find_embedded_einvoice(flat) == (XML, None)
    kids = make_pdf({4: filespec(), 5: flate_stream(XML), 6: b'<< /Kids [7 0 R 8 0 R] >>', 7: b'<< /Names [(a) 3 0 R] >>',
                     8: b'<< /Names [(b) 4 0 R] >>', 3: b'<< /Type /Filespec /F (other.xml) >>'},
                    catalog=b'/Names << /EmbeddedFiles 6 0 R >>')
    assert pdf_embedded.find_embedded_einvoice(kids) == (XML, None)


def _chain(depth):
    """Ein Namensbaum, dessen Blatt mit dem Anhang ``depth`` Ebenen unter der Wurzel (Objekt 10) liegt."""
    nodes = {10 + level: b'<< /Kids [%d 0 R] >>' % (11 + level) for level in range(depth)}
    nodes[10 + depth] = b'<< /Names [(a) 4 0 R] >>'
    return make_pdf({4: filespec(), 5: flate_stream(XML), **nodes}, catalog=b'/Names << /EmbeddedFiles 10 0 R >>')


def test_the_name_tree_is_followed_to_a_depth_of_eight_not_further():
    assert pdf_embedded.find_embedded_einvoice(_chain(0)) == (XML, None)
    assert pdf_embedded.find_embedded_einvoice(_chain(8)) == (XML, None)
    assert pdf_embedded.find_embedded_einvoice(_chain(9)) == (None, None)
    assert pdf_embedded.find_embedded_einvoice(_chain(400)) == (None, None)       # keine Rekursion bis zum Ende


def test_a_cycle_in_the_name_tree_ends_and_does_not_hide_an_attachment():
    loop = make_pdf({6: b'<< /Kids [7 0 R] >>', 7: b'<< /Kids [6 0 R] >>'}, catalog=b'/Names << /EmbeddedFiles 6 0 R >>')
    assert pdf_embedded.find_embedded_einvoice(loop) == (None, None)
    own = make_pdf({6: b'<< /Kids [6 0 R] >>'}, catalog=b'/Names << /EmbeddedFiles 6 0 R >>')
    assert pdf_embedded.find_embedded_einvoice(own) == (None, None)
    found = make_pdf({4: filespec(), 5: flate_stream(XML), 6: b'<< /Kids [7 0 R] >>',
                      7: b'<< /Names [(a) 4 0 R] /Kids [6 0 R] >>'}, catalog=b'/Names << /EmbeddedFiles 6 0 R >>')
    assert pdf_embedded.find_embedded_einvoice(found) == (XML, None)


def test_many_references_to_the_same_node_do_not_blow_up():
    """Jeder Knoten nennt den nächsten fünfzigmal: ohne die besuchten Knoten wären das 50 hoch 8 Besuche."""
    nodes = {10 + level: b'<< /Kids [' + b'%d 0 R ' % (11 + level) * 50 + b'] >>' for level in range(8)}
    nodes[18] = b'<< /Names [] >>'
    started = time.perf_counter()
    assert pdf_embedded.find_embedded_einvoice(
        make_pdf(nodes, catalog=b'/Names << /EmbeddedFiles 10 0 R >>')) == (None, None)
    assert time.perf_counter() - started < 2


def _wide(position):
    """Ein Namensbaum mit 150 Kindknoten unter der Wurzel (Objekt 10); der Anhang hängt am Knoten ``position``."""
    nodes = {11 + i: b'<< /Names [(a) 4 0 R] >>' if i == position else b'<< /Names [] >>' for i in range(150)}
    kids = b' '.join(b'%d 0 R' % (11 + i) for i in range(150))
    return make_pdf({4: filespec(), 5: flate_stream(XML), 10: b'<< /Kids [' + kids + b'] >>', **nodes},
                    catalog=b'/Names << /EmbeddedFiles 10 0 R >>')


def test_only_the_first_hundred_nodes_of_the_name_tree_are_visited():
    assert pdf_embedded.find_embedded_einvoice(_wide(99)) == (XML, None)
    assert pdf_embedded.find_embedded_einvoice(_wide(100)) == (None, None)


def _crowd(position):
    """Ein Namensbaum mit 150 Einträgen; der Anhang steht an der Stelle ``position`` (von 0 an)."""
    pairs = b' '.join(b'(k%d) %d 0 R' % (i, 4 if i == position else 3) for i in range(150))
    return make_pdf({3: b'<< /Type /Filespec /F (decoy.xml) >>', 4: filespec(), 5: flate_stream(XML),
                     6: b'<< /Names [' + pairs + b'] >>'}, catalog=b'/Names << /EmbeddedFiles 6 0 R >>')


def test_only_the_first_hundred_entries_are_looked_at():
    assert pdf_embedded.find_embedded_einvoice(_crowd(99)) == (XML, None)
    assert pdf_embedded.find_embedded_einvoice(_crowd(100)) == (None, None)


def test_the_af_entries_come_before_the_name_tree():
    spec = b'<< /Type /Filespec /F (factur-x.xml) /EF << /F 6 0 R >> >>'
    pdf = make_pdf({4: filespec(), 5: flate_stream(XML), 6: flate_stream(b'<a/>'), 7: spec},
                   catalog=b'/AF [7 0 R] /Names << /EmbeddedFiles << /Names [(a) 4 0 R] >> >>')
    assert pdf_embedded.find_embedded_einvoice(pdf) == (b'<a/>', None)


def test_an_attachment_that_cannot_be_read_does_not_hide_a_good_one_behind_it():
    bad = b'<< /Type /Filespec /F (factur-x.xml) >>'
    pdf = make_pdf({4: filespec(), 5: flate_stream(XML), 7: bad},
                   catalog=b'/AF [7 0 R] /Names << /EmbeddedFiles << /Names [(a) 4 0 R] >> >>')
    assert pdf_embedded.find_embedded_einvoice(pdf) == (XML, None)


BROKEN = data_stream(b'kein zlib', b'/Filter /FlateDecode')           # ein Anhang, der sich nicht entpacken lässt


def _counting(monkeypatch):
    """Die Datenströme (``objid``), die entpackt werden sollen: jeder Aufruf von ``_bounded_inflate``, in der Reihenfolge."""
    attempts = []
    real = pdf_embedded._bounded_inflate

    def counted(stream, limit):
        attempts.append(stream.objid)
        return real(stream, limit)

    monkeypatch.setattr(pdf_embedded, '_bounded_inflate', counted)
    return attempts


def _attachments(count, good=()):
    """Eine PDF mit ``count`` Anhängen namens ``factur-x.xml`` im Namensbaum, je mit eigener Filespec (Objekt 20 + 2n) und eigenem
    Datenstrom (21 + 2n); an den Stellen ``good`` (von 0 an) trägt der Datenstrom das XML, sonst lässt er sich nicht entpacken."""
    objects, pairs = {}, []
    for number in range(count):
        objects[20 + 2 * number] = filespec(stream=21 + 2 * number)
        objects[21 + 2 * number] = flate_stream(XML) if number in good else BROKEN
        pairs.append(b'(k%d) %d 0 R' % (number, 20 + 2 * number))
    return make_pdf(objects, catalog=b'/Names << /EmbeddedFiles << /Names [' + b' '.join(pairs) + b'] >> >>')


def test_at_most_three_attachments_are_unpacked_and_the_search_then_ends_with_the_note(monkeypatch):
    attempts = _counting(monkeypatch)
    pdf = _attachments(5)                                                      # fünf Anhänge mit dem Namen, keiner lässt sich entpacken
    assert pdf_embedded.MAX_ATTEMPTS == 3
    assert pdf_embedded.find_embedded_einvoice(pdf) == (None, 'attachment_unreadable')
    assert attempts == [21, 23, 25]                                            # genau drei Versuche, die ersten drei
    del attempts[:]
    assert read(pdf, 'a.pdf').notes == ['attachment_unreadable'] and len(attempts) == 3


def test_the_third_attachment_is_still_tried_and_the_fourth_never(monkeypatch):
    attempts = _counting(monkeypatch)
    assert pdf_embedded.find_embedded_einvoice(_attachments(5, good={2})) == (XML, None)                   # der dritte Versuch gelingt
    assert pdf_embedded.find_embedded_einvoice(_attachments(5, good={3})) == (None, 'attachment_unreadable')  # der vierte kommt nicht mehr dran
    assert pdf_embedded.find_embedded_einvoice(_attachments(3, good={0})) == (XML, None)
    assert attempts == [21, 23, 25, 21, 23, 25, 21]
    del attempts[:]
    assert pdf_embedded.find_embedded_einvoice(_attachments(2)) == (None, 'attachment_unreadable') and attempts == [21, 23]


def test_the_same_attachment_in_af_and_in_the_name_tree_counts_once(monkeypatch):
    attempts = _counting(monkeypatch)
    objects = {20: filespec(stream=21), 21: BROKEN, 22: filespec(stream=23), 23: BROKEN, 30: filespec(stream=31), 31: flate_stream(XML)}
    pdf = make_pdf(objects, catalog=b'/AF [20 0 R 22 0 R] /Names << /EmbeddedFiles << /Names [(a) 20 0 R (b) 22 0 R (c) 30 0 R] >> >>')
    assert pdf_embedded.find_embedded_einvoice(pdf) == (XML, None)            # ohne das Zählen nur einmal wären die drei Versuche vorher verbraucht
    assert attempts == [21, 23, 31]                                            # jede Datei einmal: der Namensbaum nennt die beiden ersten noch einmal
    del attempts[:]
    same_stream = make_pdf({20: filespec(stream=21), 22: filespec(stream=21), 21: BROKEN}, catalog=b'/AF [20 0 R 22 0 R]')
    assert pdf_embedded.find_embedded_einvoice(same_stream) == (None, 'attachment_unreadable') and attempts == [21]   # zwei Filespecs, ein Datenstrom


def test_an_attachment_without_a_data_stream_is_no_attempt(monkeypatch):
    attempts = _counting(monkeypatch)
    bare = b'<< /Type /Filespec /F (factur-x.xml) >>'
    objects = {20 + number: bare for number in range(6)}
    objects[30], objects[31] = filespec(stream=31), flate_stream(XML)
    pdf = make_pdf(objects, catalog=b'/AF [20 0 R 21 0 R 22 0 R 23 0 R 24 0 R 25 0 R 30 0 R]')
    assert pdf_embedded.find_embedded_einvoice(pdf) == (XML, None) and attempts == [31]


def test_the_pdf_of_the_app_is_found_with_one_attempt(monkeypatch):
    attempts = _counting(monkeypatch)
    pdf = pdf_with(XML)                                                        # das XML steht unter AF und im Namensbaum
    assert pdf_embedded.find_embedded_einvoice(pdf) == (XML, None) and len(attempts) == 1
    assert b'/AF [' in pdf and b'/EmbeddedFiles' in pdf
    assert read(pdf, 'a.pdf').source == 'einvoice_cii'


def test_the_app_makes_an_attachment_in_af_and_in_the_name_tree():
    pdf = pdf_with(XML)
    assert pdf_embedded.find_embedded_einvoice(pdf) == (XML, None)
    assert b'/AF [' in pdf and b'/EmbeddedFiles' in pdf
    renamed = pdf.replace(b'(factur-x.xml)', b'(abcdefgh.xml)')                # gleich lang: die Querverweise bleiben
    assert renamed != pdf and pdf_embedded.find_embedded_einvoice(renamed) == (None, None)
    assert read(renamed, 'a.pdf').notes == []


def test_broken_pdf_bytes_never_raise():
    good = pdf_with(XML)
    samples = [b'%PDF-', b'%PDF-1.7\n', b'%PDF-1.7\n' + b'\xff' * 500, good[:200], good[:len(good) // 2], good[::-1],
               b'%PDF-1.7\n1 0 obj\n<< /Type /Catalog /AF [1 0 R] /Names 1 0 R >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF',
               b'%PDF-1.7\n1 0 obj\n<< /Type /Catalog /AF 5 /Names << /EmbeddedFiles 7 >> >>\nendobj\n'
               b'trailer\n<< /Root 1 0 R >>\n%%EOF', b'\x00' * 100, b'', make_pdf({}, catalog=b'/AF 5 /Names 5')]
    for sample in samples:
        found = pdf_embedded.find_embedded_einvoice(sample)
        assert found[0] is None or found[0] == XML, sample[:20]
        reading = read(sample, 'kaputt.pdf')
        assert reading.status in ('empty', 'no_text', 'unsupported', 'ok', 'failed')
    assert pdf_embedded.find_embedded_einvoice(b'%PDF-1.7\n' + b'\xff' * 500) == (None, 'pdf_invalid')
    assert pdf_embedded.find_embedded_einvoice(b'') == (None, 'pdf_invalid')
    broken = read(b'%PDF-1.7\n' + b'\xff' * 500, 'kaputt.pdf')
    assert (broken.status, broken.notes) == ('failed', ['pdf_invalid'])      # der Anhang-Weg und der Textweg sagen dasselbe: einmal


def test_an_encrypted_pdf_is_not_opened():
    encrypt = (b'/Encrypt << /Filter /Standard /V 1 /R 2 /O (' + b'0' * 32 + b') /U (' + b'1' * 32 + b') /P -4 >> '
               b'/ID [(aaaaaaaaaaaaaaaa) (aaaaaaaaaaaaaaaa)]')
    pdf = make_pdf({4: filespec(), 5: flate_stream(XML)}, catalog=b'/AF [4 0 R]', trailer=encrypt)
    assert pdf_embedded.find_embedded_einvoice(pdf) == (None, 'encrypted')
    reading = read(pdf, 'a.pdf')                                               # ein Passwort ist nötig: auch kein Text
    assert (reading.status, reading.notes) == ('unsupported', ['encrypted'])


def test_a_pdf_that_opens_but_declares_encryption_is_not_read(monkeypatch):
    class Opened:
        encryption = ('id', {})
        catalog = {'AF': []}
    monkeypatch.setattr(pdf_embedded, 'PDFDocument', lambda _parser: Opened())
    assert pdf_embedded.find_embedded_einvoice(pdf_with(XML)) == (None, 'encrypted')


# ── detect.kind ──────────────────────────────────────────────────────────────

PNG = b'\x89PNG\r\n\x1a\n0000'
JPG = b'\xff\xd8\xff\xe0JFIF'
GIF = b'GIF89a0000'
WEBP = b'RIFF\x1a\x00\x00\x00WEBPVP8 '


@pytest.mark.parametrize('name, data, expected_kind', [
    ('a.pdf', b'%PDF-1.7\n', 'pdf'), ('A.PDF', b'%PDF-', 'pdf'), ('a.pdf', b'<?xml version="1.0"?><a/>', 'other'),
    ('a.pdf', b'PK\x03\x04', 'other'), ('a.pdf', b' %PDF-1.7', 'other'), ('a.pdf', b'', 'other'),
    ('a.xml', b'<?xml version="1.0"?><a/>', 'xml'), ('a.XML', b'<a/>', 'xml'), ('a.xml', b'\xef\xbb\xbf<a/>', 'xml'),
    ('a.xml', b' \t\r\n<a/>', 'xml'), ('a.xml', '<a/>'.encode('utf-16'), 'xml'), ('a.xml', '<a/>'.encode('utf-16-be'), 'other'),
    ('a.xml', b'\xfe\xff\x00<\x00a\x00/\x00>', 'xml'), ('a.xml', b'%PDF-1.7', 'other'), ('a.xml', b'hallo', 'other'),
    ('a.xml', b'', 'other'), ('a.xml', b'   ', 'other'), ('a.xml', b'\xef\xbb\xbf', 'other'),
    ('a.png', PNG, 'image'), ('a.jpg', JPG, 'image'), ('a.jpeg', JPG, 'image'), ('A.GIF', GIF, 'image'),
    ('a.webp', WEBP, 'image'), ('a.png', JPG, 'other'), ('a.jpg', PNG, 'other'), ('a.png', b'<svg/>', 'other'),
    ('a.png', b'%PDF-1.7', 'other'), ('a.webp', b'RIFF\x10\x00\x00\x00WAVEfmt ', 'other'),
    ('a.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>', 'other'), ('a.txt', b'text', 'other'),
    ('a', b'%PDF-1.7', 'other'), ('', b'%PDF-1.7', 'other'), (None, b'%PDF-1.7', 'other'), ('a.pdf.exe', b'%PDF-', 'other'),
    ('a.tiff', b'II*\x00', 'other'), ('a.html', b'<html/>', 'other')])
def test_detect_needs_the_extension_and_the_first_bytes_to_agree(name, data, expected_kind):
    assert detect.kind(name, data) == expected_kind


def test_an_image_has_no_text_and_anything_else_is_unsupported():
    assert read_document('a.png', PNG, OWN) == Reading(status='no_text')
    assert read_document('a.png', b'kein Bild', OWN) == Reading(status='unsupported')
    assert read_document('a.txt', b'Rechnung 119,00', OWN) == Reading(status='unsupported')
    assert read_document('a.xml', b'Rechnung', OWN) == Reading(status='unsupported')          # kein XML: Kennbytes passen nicht
    assert read_document('a.pdf', b'<a/>', OWN) == Reading(status='unsupported')


# ── Die Lesung: to_row, to_dict, from_dict ───────────────────────────────────

ROW_KEYS = {'Status', 'Source', 'ReaderVersion', 'DocDate', 'GrossMinor', 'NetMinor', 'TaxMinor', 'TaxRate', 'Currency',
            'SenderName', 'SenderVatId', 'InvoiceNumber', 'Kind', 'Raw', 'Text'}


def test_to_row_has_exactly_the_columns_and_json_for_raw():
    reading = read(cii())
    row = reading.to_row()
    assert set(row) == ROW_KEYS and 'ReadAt' not in row
    assert (row['Status'], row['Source'], row['ReaderVersion'], row['DocDate'], row['GrossMinor'], row['NetMinor'],
            row['TaxMinor'], row['TaxRate'], row['Currency'], row['SenderName'], row['SenderVatId'],
            row['InvoiceNumber'], row['Kind'], row['Text']) == (
        'ok', 'einvoice_cii', receipt_reading.READER_VERSION, '2026-03-04', 1190000, 1000000, 190000, 0.19, 'EUR',
        'Muster GmbH', 'DE123456789', 'R-100', 'invoice', None)
    assert receipt_reading.READER_VERSION == 1
    raw = json.loads(row['Raw'])
    assert raw['notes'] == [] and raw['checks'] == {'sum': 'ok'} and raw['fields'] == json.loads(
        json.dumps(reading.raw['fields']))
    assert row['Raw'] == json.dumps(raw, ensure_ascii=False, sort_keys=True)
    empty = Reading(status='unsupported').to_row()
    assert set(empty) == ROW_KEYS and empty['Raw'] == '{"notes": []}' and empty['GrossMinor'] is None


def test_to_row_notes_land_in_raw():
    row = Reading(status='failed', notes=['xml_dtd', 'x\x00y']).to_row()
    assert json.loads(row['Raw']) == {'notes': ['xml_dtd', 'xy']}


def test_to_row_cuts_the_texts_and_removes_control_characters():
    reading = Reading(
        status='ok', source='text', sender_name='N' * 300, sender_vat_id='D' * 30, invoice_number='R' * 150,
        text='a\x00b​c‮d\nz\t\x85\ud800' + 'x' * 30000,
        raw={'fields': {'sender_name': {'value': 'v\x07​', 'quote': 'q' * 500}}, 'tax_breakdown': [{'rate': 0.19}] * 30,
             'top': 'ü' * 300}, notes=['text_truncated', 'a\x00b'])
    row = reading.to_row()
    assert (len(row['SenderName']), len(row['SenderVatId']), len(row['InvoiceNumber'])) == (200, 20, 100)
    assert row['Text'].startswith('abcd\nz') and len(row['Text']) == receipt_reading.MAX_TEXT_CHARS
    assert '\x00' not in row['Text'] and '​' not in row['Text'] and '‮' not in row['Text'] and '\t' not in row['Text']
    row['Text'].encode('utf-8')                                          # lässt sich speichern: keine einzelnen Ersatzzeichen
    raw = json.loads(row['Raw'])
    assert raw['fields']['sender_name'] == {'value': 'v', 'quote': 'q' * 200} and len(raw['top']) == 200
    assert len(raw['tax_breakdown']) == 20 and raw['notes'] == ['text_truncated', 'ab', 'raw_truncated']
    assert Reading(status='ok', text='  \n\x00 ').to_row()['Text'] is None


def test_to_row_keeps_raw_within_its_size_and_says_so(monkeypatch):
    raw = {'fields': {'a': {'value': 'v' * 150, 'quote': 'q' * 150}, 'b': {'value': 'w' * 150, 'quote': 'r' * 150}},
           'tax_breakdown': [{'rate': 0.19, 'category': 'S', 'basis_minor': 1, 'tax_minor': 1}] * 20,
           'checks': {'sum': 'ok'}}
    reading = Reading(status='ok', raw=raw, notes=['one'])
    assert len(reading.to_row()['Raw'].encode('utf-8')) > 1000
    monkeypatch.setattr(receipt_reading, 'MAX_RAW_BYTES', 1000)
    row = reading.to_row()
    kept = json.loads(row['Raw'])
    assert len(row['Raw'].encode('utf-8')) <= 1000 and 'tax_breakdown' not in kept and kept['checks'] == {'sum': 'ok'}
    assert kept['notes'] == ['one', 'raw_truncated'] and 'quote' not in json.dumps(kept)
    assert kept['fields']['a']['value'] == 'v' * 150                      # nur die Zitate und die Aufschlüsselung fallen
    monkeypatch.setattr(receipt_reading, 'MAX_RAW_BYTES', 300)
    row = reading.to_row()
    assert json.loads(row['Raw']) == {'notes': ['one', 'raw_truncated']} and len(row['Raw'].encode('utf-8')) <= 300


def test_to_row_counts_bytes_not_characters_for_the_size_of_raw(monkeypatch):
    reading = Reading(status='ok', raw={'fields': {'a': {'value': 'ü' * 150}}}, notes=[])
    monkeypatch.setattr(receipt_reading, 'MAX_RAW_BYTES', 250)                 # 150 Zeichen sind 300 Bytes in UTF-8
    assert json.loads(reading.to_row()['Raw']) == {'notes': ['raw_truncated']}


def test_the_limits_are_module_constants_the_tests_can_lower(monkeypatch):
    assert (receipt_reading.MAX_READ_BYTES, receipt_reading.MAX_XML_BYTES) == (30 * 1024 * 1024, 20 * 1024 * 1024)
    assert (receipt_reading.MAX_TEXT_CHARS, receipt_reading.MAX_RAW_BYTES) == (20_000, 64 * 1024)
    assert receipt_reading.MAX_XML_BYTES == camt.MAX_XML_BYTES
    monkeypatch.setattr(receipt_reading, 'MAX_TEXT_CHARS', 10)
    assert Reading(status='ok', text='x' * 50).to_row()['Text'] == 'x' * 10


def test_a_reading_survives_the_way_through_json_as_a_dict():
    for reading in (read(cii()), read(ubl(root='CreditNote')), read(DTD_XML), read(b'<a/>'), Reading(status='no_text'),
                    Reading(status='ok', source='text', doc_date='2026-03-04', gross_minor=-5, tax_rate=0.0, text='Zeile\nZeile',
                            raw={'fields': {'x': {'value': 1.5, 'flag': True, 'none': None, 'list': [1, 'a']}}},
                            notes=['text_truncated'])):
        wire = json.loads(json.dumps(reading.to_dict()))
        assert Reading.from_dict(wire) == reading
        assert Reading.from_dict(reading.to_dict()) == reading


GOOD = {'status': 'ok', 'source': 'einvoice_cii', 'doc_date': '2026-03-04', 'gross_minor': 1190000, 'net_minor': 1000000,
        'tax_minor': 190000, 'tax_rate': 0.19, 'currency': 'EUR', 'sender_name': 'Muster GmbH',
        'sender_vat_id': 'DE123456789', 'invoice_number': 'R-100', 'kind': 'invoice',
        'raw': {'fields': {'a': {'value': 1}}}, 'text': 'Zeile', 'notes': ['bad_gross_minor']}


@pytest.mark.parametrize('change', [
    {'status': 'bogus'}, {'status': None}, {'status': 1}, {'source': 'x'}, {'source': 5}, {'kind': 'refund'}, {'kind': 1},
    {'gross_minor': 1.5}, {'gross_minor': True}, {'gross_minor': '5'}, {'gross_minor': 10 ** 16}, {'net_minor': -10 ** 16},
    {'tax_minor': [1]}, {'tax_rate': 1.5}, {'tax_rate': -0.1}, {'tax_rate': float('nan')}, {'tax_rate': float('inf')},
    {'tax_rate': 'x'}, {'tax_rate': True}, {'currency': 'eur'}, {'currency': 5}, {'currency': 'EURO'},
    {'doc_date': '2026-13-40'}, {'doc_date': '20260304'}, {'doc_date': '1800-01-01'}, {'doc_date': '2026-03-04 '},
    {'doc_date': 20260304}, {'sender_name': 'x' * 201}, {'sender_name': ''}, {'sender_name': ' x'}, {'sender_name': 'a\x00b'},
    {'sender_name': 5}, {'sender_vat_id': 'D' * 21}, {'invoice_number': 'N' * 101}, {'invoice_number': 'a‮b'},
    {'text': 'x' * 20001}, {'text': 'a\x00'}, {'text': 5}, {'raw': []}, {'raw': None}, {'raw': {'a': {1, 2}}},
    {'raw': {'a': 'x' * 201}}, {'raw': {'a': 'b\x00'}}, {'raw': {'notes': []}}, {'raw': {'a': [1] * 21}},
    {'raw': {'a': float('nan')}}, {'raw': {1: 'a'}}, {'notes': 'x'}, {'notes': None}, {'notes': [5]}, {'notes': ['a\x00']},
    {'notes': ['n'] * 51}, {'unknown': 1}, {'ReadAt': 'x'}])
def test_from_dict_refuses_anything_unfit(change):
    assert Reading.from_dict(GOOD).status == 'ok'
    with pytest.raises(ValueError):
        Reading.from_dict({**GOOD, **change})


def test_from_dict_refuses_what_is_not_a_dict_and_a_missing_status():
    for data in (None, [], 'x', 5, [GOOD], (('status', 'ok'),)):
        with pytest.raises(ValueError):
            Reading.from_dict(data)
    with pytest.raises(ValueError):
        Reading.from_dict({key: value for key, value in GOOD.items() if key != 'status'})
    assert Reading.from_dict({'status': 'failed'}) == Reading(status='failed')           # alles andere ist optional


def test_from_dict_does_not_echo_the_input_in_the_error():
    with pytest.raises(ValueError) as caught:
        Reading.from_dict({**GOOD, 'sender_name': 'Geheimnis' + 'x' * 300})
    assert 'Geheimnis' not in str(caught.value)


def test_from_dict_does_not_share_the_callers_structures():
    data = json.loads(json.dumps(GOOD))
    reading = Reading.from_dict(data)
    reading.raw['fields']['a']['value'] = 2
    reading.notes.append('x')
    assert data['raw']['fields']['a']['value'] == 1 and data['notes'] == ['bad_gross_minor']


# ── Das Paket bleibt ein reines Lesepaket ────────────────────────────────────

def test_the_package_imports_nothing_from_server_or_db_and_touches_no_files_or_network():
    forbidden = {'server', 'db', 'sqlite3', 'socket', 'subprocess', 'shutil', 'tempfile', 'urllib', 'http', 'ssl', 'ftplib',
                 'smtplib', 'pathlib', 'multiprocessing', 'threading'}
    sources = sorted((ROOT / 'receipt_reading').glob('*.py')) + [ROOT / 'importers' / 'safe_xml.py']
    assert len(sources) >= 5
    may_start_processes = {'isolated.py': {'subprocess'}}                    # der Starter des Kindprozesses, sonst niemand
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module or '']
            else:
                modules = []
            assert not {module.split('.')[0] for module in modules} & (
                forbidden - may_start_processes.get(path.name, set())), (path.name, modules)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ('open', 'exec', 'eval', 'print', '__import__'), (path.name, node.func.id)
