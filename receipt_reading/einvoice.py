# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""E-Rechnung lesen: XRechnung-XML (UBL ``Invoice`` und ``CreditNote``) und ZUGFeRD-/Factur-X-XML (CII
``CrossIndustryInvoice``), auch das XML aus einer hybriden PDF.

Die Felder stehen im Dokument (Begriffe nach EN 16931: BT-1 Rechnungsnummer, BT-2 Datum, BT-5 Währung, BT-27 Verkäufer,
BT-31 USt-IdNr., BT-109 Netto, BT-110 Steuer, BT-112 Brutto, BG-23 Steueraufschlüsselung). Gelesen wird nur, was streng passt –
Beträge mit höchstens zwei Nachkommastellen und zwölf Stellen davor, Sätze bis 100, ein Kalenderdatum, die Währung als drei
Großbuchstaben, nur ASCII-Ziffern –; was nicht passt, bleibt leer und gibt den Befund ``bad_<feld>``. Beträge stehen, wie das
Dokument sie nennt (eine Gutschrift trägt positive Beträge, ``kind`` sagt es). Geraten und abgeleitet wird nichts, mit einer
Ausnahme: Nennt das Dokument keinen Gesamtbetrag der Steuer, aber eine Steueraufschlüsselung, in der jeder Eintrag den Betrag 0
trägt (Beleg ohne Steuerausweis, § 19 UStG), ist die Steuer 0.

``raw['fields'][<Feld>]`` nennt je gelesenem Feld Wert, ``source: einvoice`` und den Begriff (``ref``); ``raw['checks']['sum']``
ist die Summenprobe BT-109 + BT-110 = BT-112 (``ok``, ``mismatch``, ``incomplete``).
"""
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import receipt_reading as package
from importers.safe_xml import UnsafeXml, parse_untrusted_xml
from money import MAX_PARSED_AMOUNT, to_minor

from . import NAME_LIMIT, NUMBER_LIMIT, RAW_LIST_LIMIT, VAT_ID_LIMIT, Reading, clean_text

_CII = {
    'rsm': 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100',
    'ram': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100',
    'udt': 'urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100',
}
_UBL = {
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
}
_CII_ROOT = '{%s}CrossIndustryInvoice' % _CII['rsm']
# Wurzelelement -> ist es eine Gutschrift?
_UBL_ROOTS = {'{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice': False,
              '{urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2}CreditNote': True}
CREDIT_NOTE_TYPE_CODE = '381'

# Nur ASCII ([0-9], [A-Z]): ``\d`` passt auch auf Ziffern anderer Schriften
_AMOUNT = re.compile(r'-?[0-9]{1,12}(?:\.[0-9]{1,2})?')
_RATE = re.compile(r'[0-9]{1,3}(?:\.[0-9]{1,4})?')
_CURRENCY = re.compile(r'[A-Z]{3}')
_CATEGORY = re.compile(r'[A-Z]{1,2}')
_CII_DATE = re.compile(r'[0-9]{8}')
_UBL_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_ONE_PERCENT_STEP = Decimal('0.01')

# Felder der Lesung (ohne ``kind``): Ist keines gelesen, ist das Dokument ``empty``
_DATA_FIELDS = ('doc_date', 'gross_minor', 'net_minor', 'tax_minor', 'tax_rate', 'currency', 'sender_name',
                'sender_vat_id', 'invoice_number')


# ── Werte, streng ────────────────────────────────────────────────────────────

def _raw(element) -> Optional[str]:
    """Der Text eines Elements ohne Leerraum am Rand; ``None``, wenn es fehlt oder leer ist."""
    if element is None:
        return None
    return (element.text or '').strip() or None


def _text(element, limit: int) -> Optional[str]:
    """Freitext eines Elements: Leerraum zusammengefasst, ohne Steuerzeichen, höchstens ``limit`` Zeichen."""
    text = _raw(element)
    return None if text is None else clean_text(' '.join(text[:limit * 4 + 64].split()), limit)


def _amount(text: str) -> Optional[int]:
    """Minor Units (``money.to_minor``) eines Betrags wie ``-12.5`` oder ``1234.56``; sonst ``None``."""
    if not _AMOUNT.fullmatch(text):
        return None
    value = Decimal(text)
    return to_minor(value) if abs(value) < MAX_PARSED_AMOUNT else None


def _rate(text: str) -> Optional[float]:
    """Ein Steuersatz als Anteil (``19`` -> 0.19), auf 0,01 Prozentpunkte gerundet, höchstens 100; sonst ``None``."""
    if not _RATE.fullmatch(text) or Decimal(text) > 100:
        return None
    return float(Decimal(text).quantize(_ONE_PERCENT_STEP, rounding=ROUND_HALF_UP) / 100)


def _calendar_date(pattern):
    """Parser für ein Datum aus ASCII-Ziffern (``pattern``, mit oder ohne ``-``): ein Kalenderdatum der Jahre 1900 bis 2200 als
    ISO-Text, sonst ``None``."""
    def parse(text: str) -> Optional[str]:
        if not pattern.fullmatch(text):
            return None
        digits = text.replace('-', '')
        year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:])
        try:
            return date(year, month, day).isoformat() if 1900 <= year <= 2200 else None
        except ValueError:
            return None
    return parse


def _currency(text: str) -> Optional[str]:
    return text if _CURRENCY.fullmatch(text) else None


def _category(text: str) -> Optional[str]:
    return text if _CATEGORY.fullmatch(text) else None


def _find(parent, path: str, namespaces: dict):
    return None if parent is None else parent.find(path, namespaces)


# ── Was ein Dokument nennt ───────────────────────────────────────────────────

class _Document:
    """Die Felder eines Dokuments, wie sie gelesen wurden: ``values[Feld] = (Wert, Begriff)``. Beide Syntaxen füllen es
    gleich; :meth:`finish` macht daraus die Lesung."""

    def __init__(self, source: str, default_kind_ref: str, credit_root: bool = False):
        self.source = source
        self.default_kind_ref = default_kind_ref
        self.credit_root = credit_root
        self.notes: list = []
        self.values: dict = {}
        self.type_code: Optional[str] = None
        self.tax_total_absent = False        # BT-110 fehlt ganz (nicht: steht da, passt aber nicht)
        self.breakdown: list = []            # BG-23, höchstens ``RAW_LIST_LIMIT`` Einträge
        self.breakdown_more = False          # es gab mehr Einträge

    def value(self, name: str):
        entry = self.values.get(name)
        return entry[0] if entry else None

    def bad(self, name: str) -> None:
        self.notes.append(f'bad_{name}')

    def read(self, name: str, element, parse, ref: str):
        text = _raw(element)
        if text is None:
            return None
        value = parse(text)
        if value is None:
            self.bad(name)
            return None
        self.values[name] = (value, ref)
        return value

    def read_text(self, name: str, element, limit: int, ref: str):
        value = _text(element, limit)
        if value is not None:
            self.values[name] = (value, ref)
        return value

    def read_amount(self, name: str, element, ref: str):
        """Ein Betrag; trägt das Element eine andere Währung als BT-5, gilt er nicht."""
        declared = None if element is None else element.get('currencyID')
        currency = self.value('currency')
        if _raw(element) is not None and declared is not None and currency is not None and declared != currency:
            self.bad(name)
            return None
        return self.read(name, element, _amount, ref)

    def read_tax_total(self, elements: list) -> Optional[int]:
        """BT-110 aus den Elementen, die einen Gesamtbetrag der Steuer nennen: das in der Belegwährung (BT-5), sonst das einzige.
        Gibt die Stelle des gewählten Elements zurück; mehrere passende oder keines unter mehreren ist ``bad_tax_minor``."""
        if not elements:
            self.tax_total_absent = True
            return None
        currency = self.value('currency')
        matching = [i for i, element in enumerate(elements) if currency is not None and element.get('currencyID') == currency]
        index = matching[0] if len(matching) == 1 else (0 if not matching and len(elements) == 1 else None)
        if index is None:
            self.bad('tax_minor')
            return None
        self.read_amount('tax_minor', elements[index], 'BT-110')
        return index

    def add_breakdown(self, basis, tax, category, rate) -> None:
        """Ein Eintrag der Steueraufschlüsselung (BG-23) aus den Elementen für Basis, Betrag, Kategorie und Satz."""
        if len(self.breakdown) >= RAW_LIST_LIMIT:
            self.breakdown_more = True
            return
        entry = {'rate': None, 'category': None, 'basis_minor': None, 'tax_minor': None}
        for key, element, parse in (('rate', rate, _rate), ('category', category, _category),
                                    ('basis_minor', basis, _amount), ('tax_minor', tax, _amount)):
            text = _raw(element)
            if text is not None:
                entry[key] = parse(text)
                if entry[key] is None:
                    self.bad('tax_breakdown')
        self.breakdown.append(entry)

    def finish(self) -> Reading:
        values = self.values
        breakdown = self.breakdown
        notes = list(self.notes)
        # Ohne Gesamtbetrag der Steuer und mit einer Aufschlüsselung, die überall 0 nennt, ist die Steuer 0
        if self.tax_total_absent and breakdown and all(entry['tax_minor'] == 0 for entry in breakdown):
            values['tax_minor'] = (0, 'BG-23')
        rates = {entry['rate'] for entry in breakdown}
        if breakdown and not self.breakdown_more and None not in rates and len(rates) == 1:
            values['tax_rate'] = (rates.pop(), 'BG-23')
        if not any(name in values for name in _DATA_FIELDS):
            return Reading(status='empty', source=self.source, notes=_unique(notes))

        if self.type_code:
            kind_ref = 'BT-3'
        else:
            kind_ref = 'root' if self.credit_root else self.default_kind_ref
        kind = 'credit_note' if self.credit_root or self.type_code == CREDIT_NOTE_TYPE_CODE else 'invoice'
        net, tax, gross = (self.value(name) for name in ('net_minor', 'tax_minor', 'gross_minor'))
        if None in (net, tax, gross):
            check = 'incomplete'
        else:
            check = 'ok' if net + tax == gross else 'mismatch'
        fields = {name: {'value': value, 'source': 'einvoice', 'ref': ref} for name, (value, ref) in values.items()}
        fields['kind'] = {'value': kind, 'source': 'einvoice', 'ref': kind_ref}
        raw = {'fields': fields, 'checks': {'sum': check}}
        if breakdown:
            raw['tax_breakdown'] = breakdown
        if self.breakdown_more:
            notes.append('raw_truncated')
        return Reading(
            status='ok', source=self.source, doc_date=self.value('doc_date'), gross_minor=gross, net_minor=net,
            tax_minor=tax, tax_rate=self.value('tax_rate'), currency=self.value('currency'),
            sender_name=self.value('sender_name'), sender_vat_id=self.value('sender_vat_id'),
            invoice_number=self.value('invoice_number'), kind=kind, raw=raw, notes=_unique(notes))


def _unique(notes: list) -> list:
    return list(dict.fromkeys(notes))


# ── CII (ZUGFeRD, Factur-X) ──────────────────────────────────────────────────

def _read_cii(root) -> Reading:
    ns = _CII
    doc = _Document('einvoice_cii', default_kind_ref='default')
    header = root.find('rsm:ExchangedDocument', ns)
    trade = root.find('rsm:SupplyChainTradeTransaction', ns)
    seller = _find(trade, 'ram:ApplicableHeaderTradeAgreement/ram:SellerTradeParty', ns)
    settlement = _find(trade, 'ram:ApplicableHeaderTradeSettlement', ns)
    sums = _find(settlement, 'ram:SpecifiedTradeSettlementHeaderMonetarySummation', ns)

    doc.read_text('invoice_number', _find(header, 'ram:ID', ns), NUMBER_LIMIT, 'BT-1')
    stamp = _find(header, 'ram:IssueDateTime/udt:DateTimeString', ns)
    if stamp is not None and stamp.get('format') != '102' and _raw(stamp) is not None:
        doc.bad('doc_date')                                # nur JJJJMMTT (Format 102) wird gelesen
    else:
        doc.read('doc_date', stamp, _calendar_date(_CII_DATE), 'BT-2')
    doc.type_code = _text(_find(header, 'ram:TypeCode', ns), 10)
    doc.read('currency', _find(settlement, 'ram:InvoiceCurrencyCode', ns), _currency, 'BT-5')
    doc.read_text('sender_name', _find(seller, 'ram:Name', ns), NAME_LIMIT, 'BT-27')
    for registration in ([] if seller is None else seller.findall('ram:SpecifiedTaxRegistration/ram:ID', ns)):
        if registration.get('schemeID') == 'VA' and doc.read_text('sender_vat_id', registration, VAT_ID_LIMIT, 'BT-31'):
            break
    doc.read_amount('net_minor', _find(sums, 'ram:TaxBasisTotalAmount', ns), 'BT-109')
    doc.read_amount('gross_minor', _find(sums, 'ram:GrandTotalAmount', ns), 'BT-112')
    doc.read_amount('due_payable_minor', _find(sums, 'ram:DuePayableAmount', ns), 'BT-115')
    doc.read_tax_total([] if sums is None else sums.findall('ram:TaxTotalAmount', ns))
    for entry in ([] if settlement is None else settlement.findall('ram:ApplicableTradeTax', ns)):
        if _text(_find(entry, 'ram:TypeCode', ns), 10) == 'VAT':
            doc.add_breakdown(basis=_find(entry, 'ram:BasisAmount', ns), tax=_find(entry, 'ram:CalculatedAmount', ns),
                              category=_find(entry, 'ram:CategoryCode', ns),
                              rate=_find(entry, 'ram:RateApplicablePercent', ns))
    return doc.finish()


# ── UBL (XRechnung) ──────────────────────────────────────────────────────────

def _read_ubl(root, credit_note: bool) -> Reading:
    ns = _UBL
    doc = _Document('einvoice_ubl', default_kind_ref='root', credit_root=credit_note)
    party = _find(root, 'cac:AccountingSupplierParty/cac:Party', ns)
    monetary = root.find('cac:LegalMonetaryTotal', ns)

    doc.read_text('invoice_number', root.find('cbc:ID', ns), NUMBER_LIMIT, 'BT-1')
    doc.read('doc_date', root.find('cbc:IssueDate', ns), _calendar_date(_UBL_DATE), 'BT-2')
    doc.type_code = _text(root.find('cbc:CreditNoteTypeCode' if credit_note else 'cbc:InvoiceTypeCode', ns), 10)
    doc.read('currency', root.find('cbc:DocumentCurrencyCode', ns), _currency, 'BT-5')
    if doc.read_text('sender_name', _find(party, 'cac:PartyLegalEntity/cbc:RegistrationName', ns), NAME_LIMIT,
                     'BT-27') is None:
        doc.read_text('sender_name', _find(party, 'cac:PartyName/cbc:Name', ns), NAME_LIMIT, 'BT-28')
    for scheme in ([] if party is None else party.findall('cac:PartyTaxScheme', ns)):
        if (_text(_find(scheme, 'cac:TaxScheme/cbc:ID', ns), 10) == 'VAT'
                and doc.read_text('sender_vat_id', _find(scheme, 'cbc:CompanyID', ns), VAT_ID_LIMIT, 'BT-31')):
            break
    doc.read_amount('net_minor', _find(monetary, 'cbc:TaxExclusiveAmount', ns), 'BT-109')
    doc.read_amount('gross_minor', _find(monetary, 'cbc:TaxInclusiveAmount', ns), 'BT-112')
    doc.read_amount('due_payable_minor', _find(monetary, 'cbc:PayableAmount', ns), 'BT-115')
    totals = [(total, total.find('cbc:TaxAmount', ns)) for total in root.findall('cac:TaxTotal', ns)]
    totals = [(total, amount) for total, amount in totals if amount is not None]
    index = doc.read_tax_total([amount for _total, amount in totals])
    if index is not None:                                  # die Aufschlüsselung (BG-23) steht im gewählten TaxTotal
        for subtotal in totals[index][0].findall('cac:TaxSubtotal', ns):
            category = _find(subtotal, 'cac:TaxCategory', ns)
            if _text(_find(category, 'cac:TaxScheme/cbc:ID', ns), 10) == 'VAT':
                doc.add_breakdown(basis=subtotal.find('cbc:TaxableAmount', ns), tax=subtotal.find('cbc:TaxAmount', ns),
                                  category=_find(category, 'cbc:ID', ns), rate=_find(category, 'cbc:Percent', ns))
    return doc.finish()


# ── Einstieg ─────────────────────────────────────────────────────────────────

def read_einvoice_xml(data: bytes) -> Reading:
    """Eine E-Rechnung aus den Bytes eines XML lesen. Der Wurzel-Namensraum entscheidet: CII (``einvoice_cii``), UBL
    ``Invoice`` und ``CreditNote`` (``einvoice_ubl``); jedes andere wohlgeformte XML ist ``unsupported`` mit dem Befund
    ``not_einvoice``. Über ``MAX_XML_BYTES`` ist es ``too_large``, mit DTD ``failed`` (``xml_dtd``), nicht wohlgeformt ``failed``
    (``xml_invalid``). Ein lesbares Dokument ohne lesbares Feld ist ``empty``."""
    try:
        root = parse_untrusted_xml(data, package.MAX_XML_BYTES)
    except UnsafeXml as error:
        if error.reason == 'too_large':
            return Reading(status='too_large', notes=['xml_too_large'])
        return Reading(status='failed', notes=['xml_dtd' if error.reason == 'dtd' else 'xml_invalid'])
    except Exception:            # z. B. ValueError von expat bei einer Kodierung, die es nicht kennt
        return Reading(status='failed', notes=['xml_invalid'])
    if root.tag == _CII_ROOT:
        return _read_cii(root)
    if root.tag in _UBL_ROOTS:
        return _read_ubl(root, _UBL_ROOTS[root.tag])
    return Reading(status='unsupported', notes=['not_einvoice'])
