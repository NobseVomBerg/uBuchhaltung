# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Tests für den CII-Erzeuger (ZUGFeRD 2.x / Factur-X, Profil EN 16931)."""
import xml.etree.ElementTree as ET
from decimal import Decimal

import pytest

from export.cii_invoice import NS, build_cii, generate_cii, GUIDELINE_EN16931
from export.einvoice_model import build_einvoice
from tests.golden_cases import make_case, CASES

R = {k: f'{{{v}}}' for k, v in NS.items()}


def _root(db, case, **over):
    iid = make_case(db, case)
    return ET.fromstring(generate_cii(db.get_invoice_by_id(iid), db.get_invoice_items(iid), db=db))


def _t(root, path):
    el = root.find(path, NS)
    return None if el is None else el.text


@pytest.mark.parametrize('case', sorted(CASES))
def test_mandatory_fields(tmp_db, case):
    root = _root(tmp_db, case)
    assert _t(root, 'rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID') == GUIDELINE_EN16931
    assert _t(root, 'rsm:ExchangedDocument/ram:ID') == CASES[case]['invoice']['invoice_number']
    assert _t(root, 'rsm:ExchangedDocument/ram:TypeCode') == '380'
    assert _t(root, 'rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString') == '20260304'
    ag = 'rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeAgreement'
    assert _t(root, f'{ag}/ram:SellerTradeParty/ram:Name') == 'Muster & Söhne GmbH'
    assert _t(root, f'{ag}/ram:SellerTradeParty/ram:PostalTradeAddress/ram:CountryID') == 'DE'
    assert _t(root, f'{ag}/ram:BuyerTradeParty/ram:Name') == 'Malermeister Pinsel'
    st = 'rsm:SupplyChainTradeTransaction/ram:ApplicableHeaderTradeSettlement'
    assert _t(root, f'{st}/ram:InvoiceCurrencyCode') == 'EUR'
    assert _t(root, f'{st}/ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/ram:IBANID') == 'DE89370400440532013000'


@pytest.mark.parametrize('case', sorted(CASES))
def test_sums_consistent_exactly(tmp_db, case):
    root = _root(tmp_db, case)
    D = lambda s: Decimal(s)
    base = 'rsm:SupplyChainTradeTransaction/'
    lines = [D(e.text) for e in root.findall(
        f'{base}ram:IncludedSupplyChainTradeLineItem/ram:SpecifiedLineTradeSettlement/'
        'ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount', NS)]
    st = f'{base}ram:ApplicableHeaderTradeSettlement'
    summ = f'{st}/ram:SpecifiedTradeSettlementHeaderMonetarySummation/'
    line_total = D(_t(root, summ + 'ram:LineTotalAmount'))
    tax_basis = D(_t(root, summ + 'ram:TaxBasisTotalAmount'))
    tax_total = D(_t(root, summ + 'ram:TaxTotalAmount'))
    grand = D(_t(root, summ + 'ram:GrandTotalAmount'))
    due = D(_t(root, summ + 'ram:DuePayableAmount'))
    breakdown = root.findall(f'{st}/ram:ApplicableTradeTax', NS)
    assert sum(lines) == line_total == tax_basis
    assert sum(D(b.find('ram:BasisAmount', NS).text) for b in breakdown) == tax_basis
    assert sum(D(b.find('ram:CalculatedAmount', NS).text) for b in breakdown) == tax_total
    assert tax_basis + tax_total == grand == due
    # Gegenprobe zu den gespeicherten Rechnungssummen
    inv = CASES[case]['invoice']
    assert (line_total, tax_total, grand) == (D(str(inv['sum_net'])), D(str(inv['tax_amount'])).quantize(D('0.01')), D(str(inv['sum_gross'])))


def test_kleinunternehmer_exempt(tmp_db):
    root = _root(tmp_db, 'ku')
    tax = root.find('.//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax', NS)
    assert tax.find('ram:CategoryCode', NS).text == 'E'
    assert tax.find('ram:RateApplicablePercent', NS).text == '0.00'
    assert '§ 19 UStG' in tax.find('ram:ExemptionReason', NS).text


def test_order_and_buyer_reference(tmp_db):
    root = _root(tmp_db, 'std19')
    ag = root.find('.//ram:ApplicableHeaderTradeAgreement', NS)
    assert ag.find('ram:BuyerReference', NS).text == '04011000-1234512345-35'
    assert ag.find('ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID', NS).text == 'B-4711'


def test_units_are_unece(tmp_db):
    root = _root(tmp_db, 'std19')
    units = [e.get('unitCode') for e in root.iter(f'{R["ram"]}BilledQuantity')]
    assert units == ['C62', 'HUR']


def test_special_characters_escaped_and_roundtrip(tmp_db):
    iid = make_case(tmp_db, 'std19')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE Invoices SET BuyerCompany=?, PaymentTerms=? WHERE ID=?",
                  ('A & B <Test> "Q" \'x\' \x07Ü', ']]> <!-- x --> &amp;', iid))
    raw = generate_cii(tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid), db=tmp_db)
    assert b'<Test>' not in raw and b'<!--' not in raw and b']]>' not in raw
    root = ET.fromstring(raw)                                   # wohlgeformt
    assert _t(root, './/ram:BuyerTradeParty/ram:Name') == 'A & B <Test> "Q" \'x\' Ü'
    assert _t(root, './/ram:SpecifiedTradePaymentTerms/ram:Description') == ']]> <!-- x --> &amp;'


def test_xml_declaration_and_encoding(tmp_db):
    iid = make_case(tmp_db, 'std7')
    raw = build_cii(build_einvoice(tmp_db, tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid)))
    assert raw.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
