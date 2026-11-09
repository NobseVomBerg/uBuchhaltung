# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""CII-XML (UN/CEFACT Cross Industry Invoice) für ZUGFeRD 2.x / Factur-X.

Profil EN 16931 (Guideline ``urn:cen.eu:en16931:2017``). Liest nur das
neutrale Modell aus :mod:`export.einvoice_model`. Kundendaten werden vom
XML-Serializer der Standardbibliothek escaped; Steuerzeichen, die in XML 1.0
nicht vorkommen dürfen, werden vorher entfernt.
"""
import re
import xml.etree.ElementTree as ET
from decimal import Decimal

from money import format_plain
from .einvoice_model import EInvoice, Party, build_einvoice

NS = {
    'rsm': 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100',
    'ram': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100',
    'udt': 'urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100',
    'qdt': 'urn:un:unece:uncefact:data:standard:QualifiedDataType:100',
}
GUIDELINE_EN16931 = 'urn:cen.eu:en16931:2017'

_RSM, _RAM, _UDT = (f'{{{NS[k]}}}' for k in ('rsm', 'ram', 'udt'))
_ILLEGAL_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')


def _clean(text) -> str:
    return _ILLEGAL_XML.sub('', str(text))


def _sub(parent, ns, tag, text=None, **attrib):
    el = ET.SubElement(parent, f'{ns}{tag}', attrib)
    if text is not None:
        el.text = _clean(text)
    return el


def _amt(minor, dp=2):
    return format_plain(minor, dp)


def _price(minor):
    """Einzelpreis: mindestens 2, höchstens 4 Nachkommastellen."""
    whole, _, frac = format_plain(minor, 4).rstrip('0').partition('.')
    return f'{whole}.{frac.ljust(2, "0")}'


def _qty(q: Decimal) -> str:
    return f'{q:f}' if q != q.to_integral() else f'{int(q)}'


def _rate(r: Decimal) -> str:
    return f'{r.quantize(Decimal("0.01")):f}'


def _date(parent, tag, d):
    wrapper = _sub(parent, _RAM, tag)
    _sub(wrapper, _UDT, 'DateTimeString', d.strftime('%Y%m%d'), format='102')


def _tax_ids(party: Party):
    """(schemeID, Nummer)-Paare: ``VA`` = USt-IdNr., ``FC`` = Steuernummer."""
    return [(s, n) for s, n in (('VA', party.vat_id), ('FC', party.tax_number)) if n]


def _party(parent, tag, party: Party, seller: bool):
    p = _sub(parent, _RAM, tag)
    _sub(p, _RAM, 'Name', party.name)
    if seller and (party.contact_name or party.phone or party.email):
        contact = _sub(p, _RAM, 'DefinedTradeContact')
        if party.contact_name:
            _sub(contact, _RAM, 'PersonName', party.contact_name)
        if party.phone:
            _sub(_sub(contact, _RAM, 'TelephoneUniversalCommunication'),
                 _RAM, 'CompleteNumber', party.phone)
        if party.email:
            _sub(_sub(contact, _RAM, 'EmailURIUniversalCommunication'),
                 _RAM, 'URIID', party.email)
    addr = _sub(p, _RAM, 'PostalTradeAddress')
    if party.postal_code:
        _sub(addr, _RAM, 'PostcodeCode', party.postal_code)
    if party.street:
        _sub(addr, _RAM, 'LineOne', party.street)
    if party.city:
        _sub(addr, _RAM, 'CityName', party.city)
    _sub(addr, _RAM, 'CountryID', party.country)
    if party.email:
        _sub(_sub(p, _RAM, 'URIUniversalCommunication'), _RAM, 'URIID',
             party.email, schemeID='EM')
    for scheme, number in _tax_ids(party):
        _sub(_sub(p, _RAM, 'SpecifiedTaxRegistration'), _RAM, 'ID', number, schemeID=scheme)


def build_cii(inv: EInvoice) -> bytes:
    """CII-XML (UTF-8, mit XML-Deklaration) für das Profil EN 16931 erzeugen."""
    for prefix, uri in NS.items():
        ET.register_namespace(prefix, uri)
    root = ET.Element(f'{_RSM}CrossIndustryInvoice')
    cur = inv.currency

    ctx = _sub(root, _RSM, 'ExchangedDocumentContext')
    _sub(_sub(ctx, _RAM, 'GuidelineSpecifiedDocumentContextParameter'),
         _RAM, 'ID', GUIDELINE_EN16931)

    doc = _sub(root, _RSM, 'ExchangedDocument')
    _sub(doc, _RAM, 'ID', inv.number)
    _sub(doc, _RAM, 'TypeCode', inv.type_code)
    _date(doc, 'IssueDateTime', inv.issue_date)

    trade = _sub(root, _RSM, 'SupplyChainTradeTransaction')

    for ln in inv.lines:
        item = _sub(trade, _RAM, 'IncludedSupplyChainTradeLineItem')
        _sub(_sub(item, _RAM, 'AssociatedDocumentLineDocument'), _RAM, 'LineID', str(ln.position))
        _sub(_sub(item, _RAM, 'SpecifiedTradeProduct'), _RAM, 'Name', ln.name)
        agreement = _sub(item, _RAM, 'SpecifiedLineTradeAgreement')
        _sub(_sub(agreement, _RAM, 'NetPriceProductTradePrice'),
             _RAM, 'ChargeAmount', _price(ln.unit_price))
        delivery = _sub(item, _RAM, 'SpecifiedLineTradeDelivery')
        _sub(delivery, _RAM, 'BilledQuantity', _qty(ln.quantity), unitCode=ln.unit_code)
        settlement = _sub(item, _RAM, 'SpecifiedLineTradeSettlement')
        tax = _sub(settlement, _RAM, 'ApplicableTradeTax')
        _sub(tax, _RAM, 'TypeCode', 'VAT')
        _sub(tax, _RAM, 'CategoryCode', ln.tax_category)
        _sub(tax, _RAM, 'RateApplicablePercent', _rate(ln.tax_rate))
        _sub(_sub(settlement, _RAM, 'SpecifiedTradeSettlementLineMonetarySummation'),
             _RAM, 'LineTotalAmount', _amt(ln.net))

    agreement = _sub(trade, _RAM, 'ApplicableHeaderTradeAgreement')
    if inv.buyer_reference:
        _sub(agreement, _RAM, 'BuyerReference', inv.buyer_reference)
    _party(agreement, 'SellerTradeParty', inv.seller, seller=True)
    _party(agreement, 'BuyerTradeParty', inv.buyer, seller=False)
    if inv.order_reference:
        _sub(_sub(agreement, _RAM, 'BuyerOrderReferencedDocument'),
             _RAM, 'IssuerAssignedID', inv.order_reference)

    delivery = _sub(trade, _RAM, 'ApplicableHeaderTradeDelivery')
    if inv.delivery_date:
        event = _sub(delivery, _RAM, 'ActualDeliverySupplyChainEvent')
        _date(event, 'OccurrenceDateTime', inv.delivery_date)

    settle = _sub(trade, _RAM, 'ApplicableHeaderTradeSettlement')
    _sub(settle, _RAM, 'InvoiceCurrencyCode', cur)
    if inv.iban:
        means = _sub(settle, _RAM, 'SpecifiedTradeSettlementPaymentMeans')
        _sub(means, _RAM, 'TypeCode', '58')                    # SEPA-Überweisung
        account = _sub(means, _RAM, 'PayeePartyCreditorFinancialAccount')
        _sub(account, _RAM, 'IBANID', inv.iban)
        if inv.bank_name:
            _sub(account, _RAM, 'AccountName', inv.bank_name)
        if inv.bic:
            _sub(_sub(means, _RAM, 'PayeeSpecifiedCreditorFinancialInstitution'),
                 _RAM, 'BICID', inv.bic)
    for t in inv.tax_breakdown:
        tax = _sub(settle, _RAM, 'ApplicableTradeTax')
        _sub(tax, _RAM, 'CalculatedAmount', _amt(t.amount))
        _sub(tax, _RAM, 'TypeCode', 'VAT')
        if t.exemption_reason:
            _sub(tax, _RAM, 'ExemptionReason', t.exemption_reason)
        _sub(tax, _RAM, 'BasisAmount', _amt(t.basis))
        _sub(tax, _RAM, 'CategoryCode', t.category)
        _sub(tax, _RAM, 'RateApplicablePercent', _rate(t.rate))
    if inv.payment_terms or inv.due_date:
        terms = _sub(settle, _RAM, 'SpecifiedTradePaymentTerms')
        if inv.payment_terms:
            _sub(terms, _RAM, 'Description', inv.payment_terms)
        if inv.due_date:
            _date(terms, 'DueDateDateTime', inv.due_date)
    t = inv.totals
    summ = _sub(settle, _RAM, 'SpecifiedTradeSettlementHeaderMonetarySummation')
    _sub(summ, _RAM, 'LineTotalAmount', _amt(t.line_total))
    _sub(summ, _RAM, 'TaxBasisTotalAmount', _amt(t.tax_basis))
    _sub(summ, _RAM, 'TaxTotalAmount', _amt(t.tax_total), currencyID=cur)
    _sub(summ, _RAM, 'GrandTotalAmount', _amt(t.grand_total))
    if t.prepaid:
        _sub(summ, _RAM, 'TotalPrepaidAmount', _amt(t.prepaid))
    _sub(summ, _RAM, 'DuePayableAmount', _amt(t.payable))

    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def generate_cii(invoice, items, db=None) -> bytes:
    """CII-XML direkt aus Rechnungs- und Positionszeilen der Datenbank."""
    return build_cii(build_einvoice(db, invoice, items))
