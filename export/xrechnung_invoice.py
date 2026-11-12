# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""XRechnung als UBL-XML (EN 16931, CIUS XRechnung 3.0).

Liest ausschließlich das neutrale Modell aus :mod:`export.einvoice_model`; die
Datenbank-Zeile wird nur noch dort übersetzt. Für ZUGFeRD/Factur-X (CII) siehe
:mod:`export.cii_invoice`.
"""
import xml.etree.ElementTree as ET
from decimal import Decimal
from xml.dom import minidom

from money import format_plain
from .einvoice_model import EInvoice, Party, build_einvoice

_NS = {
    '': 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
}
_INVOICE = f'{{{_NS[""]}}}'
_CAC = f'{{{_NS["cac"]}}}'
_CBC = f'{{{_NS["cbc"]}}}'

CUSTOMIZATION_ID = 'urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0'
PROFILE_ID = 'urn:fdc:peppol.eu:2017:poacc:billing:01:1.0'


def _amount(minor, dp=2):
    return format_plain(minor, dp)


def _price(minor):
    """Einzelpreis: mindestens 2, höchstens 4 Nachkommastellen (BT-146)."""
    text = format_plain(minor, 4).rstrip('0')
    whole, _, frac = text.partition('.')
    return f'{whole}.{frac.ljust(2, "0")}'


def _qty(q: Decimal) -> str:
    return f'{q:f}' if q != q.to_integral() else f'{int(q)}'


def _rate(r: Decimal) -> str:
    return f'{r.normalize():f}'


def _sub(parent, ns, tag, text=None, **attrib):
    el = ET.SubElement(parent, f'{ns}{tag}', attrib)
    if text is not None:
        el.text = text
    return el


class XRechnungGenerator:
    """UBL-XRechnung aus einer Rechnung erzeugen."""

    def generate_xml(self, invoice, invoice_items, db=None) -> str:
        """XML-String aus Rechnungszeile und Positionen (DB-Zeilen) erzeugen.

        ``db`` ist optional und liefert nur Kontakt-Fallbacks (E-Mail,
        Steuernummer), falls die Rechnung sie nicht selbst trägt.
        """
        return self.generate_from_model(build_einvoice(db, invoice, invoice_items))

    def generate_from_model(self, inv: EInvoice) -> str:
        # Präfixe (cac/cbc) und Default-Namespace für die Serialisierung
        for prefix, uri in _NS.items():
            ET.register_namespace(prefix, uri)
        root = ET.Element(f'{_INVOICE}Invoice')
        cur = inv.currency

        _sub(root, _CBC, 'CustomizationID', CUSTOMIZATION_ID)
        _sub(root, _CBC, 'ProfileID', PROFILE_ID)
        _sub(root, _CBC, 'ID', inv.number)
        _sub(root, _CBC, 'IssueDate', inv.issue_date.isoformat())
        if inv.due_date:
            _sub(root, _CBC, 'DueDate', inv.due_date.isoformat())
        _sub(root, _CBC, 'InvoiceTypeCode', inv.type_code)
        for note in inv.notes:                                 # BT-22; im Schema zwischen Typ und Währung
            _sub(root, _CBC, 'Note', note)
        _sub(root, _CBC, 'DocumentCurrencyCode', cur)
        if inv.buyer_reference:
            _sub(root, _CBC, 'BuyerReference', inv.buyer_reference)
        if inv.order_reference:
            order = _sub(root, _CAC, 'OrderReference')
            _sub(order, _CBC, 'ID', inv.order_reference)

        self._party(root, 'AccountingSupplierParty', inv.seller, seller=True)
        self._party(root, 'AccountingCustomerParty', inv.buyer, seller=False)

        if inv.delivery_date:
            delivery = _sub(root, _CAC, 'Delivery')
            _sub(delivery, _CBC, 'ActualDeliveryDate', inv.delivery_date.isoformat())

        if inv.iban:
            pay = _sub(root, _CAC, 'PaymentMeans')
            _sub(pay, _CBC, 'PaymentMeansCode', '58')          # SEPA-Überweisung
            account = _sub(pay, _CAC, 'PayeeFinancialAccount')
            _sub(account, _CBC, 'ID', inv.iban)
            if inv.bank_name:
                _sub(account, _CBC, 'Name', inv.bank_name)
            if inv.bic:
                branch = _sub(account, _CAC, 'FinancialInstitutionBranch')
                _sub(branch, _CBC, 'ID', inv.bic)

        if inv.payment_terms:
            terms = _sub(root, _CAC, 'PaymentTerms')
            _sub(terms, _CBC, 'Note', inv.payment_terms)

        tax_total = _sub(root, _CAC, 'TaxTotal')
        _sub(tax_total, _CBC, 'TaxAmount', _amount(inv.totals.tax_total), currencyID=cur)
        for t in inv.tax_breakdown:
            sub = _sub(tax_total, _CAC, 'TaxSubtotal')
            _sub(sub, _CBC, 'TaxableAmount', _amount(t.basis), currencyID=cur)
            _sub(sub, _CBC, 'TaxAmount', _amount(t.amount), currencyID=cur)
            cat = _sub(sub, _CAC, 'TaxCategory')
            _sub(cat, _CBC, 'ID', t.category)
            _sub(cat, _CBC, 'Percent', _rate(t.rate))
            if t.exemption_reason:
                _sub(cat, _CBC, 'TaxExemptionReason', t.exemption_reason)
            scheme = _sub(cat, _CAC, 'TaxScheme')
            _sub(scheme, _CBC, 'ID', 'VAT')

        mon = _sub(root, _CAC, 'LegalMonetaryTotal')
        _sub(mon, _CBC, 'LineExtensionAmount', _amount(inv.totals.line_total), currencyID=cur)
        _sub(mon, _CBC, 'TaxExclusiveAmount', _amount(inv.totals.tax_basis), currencyID=cur)
        _sub(mon, _CBC, 'TaxInclusiveAmount', _amount(inv.totals.grand_total), currencyID=cur)
        if inv.totals.prepaid:
            _sub(mon, _CBC, 'PrepaidAmount', _amount(inv.totals.prepaid), currencyID=cur)
        _sub(mon, _CBC, 'PayableAmount', _amount(inv.totals.payable), currencyID=cur)

        for ln in inv.lines:
            line = _sub(root, _CAC, 'InvoiceLine')
            _sub(line, _CBC, 'ID', str(ln.position))
            _sub(line, _CBC, 'InvoicedQuantity', _qty(ln.quantity), unitCode=ln.unit_code)
            _sub(line, _CBC, 'LineExtensionAmount', _amount(ln.net), currencyID=cur)
            item = _sub(line, _CAC, 'Item')
            _sub(item, _CBC, 'Name', ln.name)
            cat = _sub(item, _CAC, 'ClassifiedTaxCategory')
            _sub(cat, _CBC, 'ID', ln.tax_category)
            _sub(cat, _CBC, 'Percent', _rate(ln.tax_rate))
            scheme = _sub(cat, _CAC, 'TaxScheme')
            _sub(scheme, _CBC, 'ID', 'VAT')
            price = _sub(line, _CAC, 'Price')
            _sub(price, _CBC, 'PriceAmount', _price(ln.unit_price), currencyID=cur)

        xml_string = ET.tostring(root, encoding='unicode')
        return minidom.parseString(xml_string).toprettyxml(indent='  ')

    def _party(self, root, tag, party: Party, seller: bool):
        wrapper = _sub(root, _CAC, tag)
        p = _sub(wrapper, _CAC, 'Party')
        if party.email:
            _sub(p, _CBC, 'EndpointID', party.email, schemeID='EM')
        name = _sub(p, _CAC, 'PartyName')
        _sub(name, _CBC, 'Name', party.name)
        postal = _sub(p, _CAC, 'PostalAddress')
        for tag_name, value in (('StreetName', party.street), ('CityName', party.city),
                                ('PostalZone', party.postal_code)):
            if value:
                _sub(postal, _CBC, tag_name, value)
        country = _sub(postal, _CAC, 'Country')
        _sub(country, _CBC, 'IdentificationCode', party.country)
        for number, scheme_id in ((party.vat_id, 'VAT'), (party.tax_number, 'TAX')):
            if number:
                tax = _sub(p, _CAC, 'PartyTaxScheme')
                _sub(tax, _CBC, 'CompanyID', number)
                scheme = _sub(tax, _CAC, 'TaxScheme')
                _sub(scheme, _CBC, 'ID', scheme_id)
        legal = _sub(p, _CAC, 'PartyLegalEntity')
        _sub(legal, _CBC, 'RegistrationName', party.name)
        if seller and (party.contact_name or party.phone or party.email):
            contact = _sub(p, _CAC, 'Contact')
            if party.contact_name:
                _sub(contact, _CBC, 'Name', party.contact_name)
            if party.phone:
                _sub(contact, _CBC, 'Telephone', party.phone)
            if party.email:
                _sub(contact, _CBC, 'ElectronicMail', party.email)
