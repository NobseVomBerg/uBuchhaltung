# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Neutrales E-Rechnungsmodell nach EN-16931-Begriffen.

Die Rechnung wird einmal aus der Datenbank in dieses Modell übersetzt; die
Ausgabeformate (UBL-XRechnung in :mod:`export.xrechnung_invoice`, CII/ZUGFeRD in
:mod:`export.cii_invoice`) lesen nur noch das Modell – keine Spaltenindizes und
keine eigene Rechenlogik in den Erzeugern.

Beträge sind durchgehend ganzzahlige Minor Units (:mod:`money`), nie float. Die
Steueraufschlüsselung ist eine Liste (Vorbereitung für gemischte Steuersätze);
Summen werden aus den Positionen abgeleitet und sind damit exakt konsistent
(BR-CO-10 … BR-CO-16).
"""
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import List, Optional

from money import multiply, round_minor, tax_from_net, to_minor

# Spaltenindizes der Invoices-Zeile (``SELECT *``), siehe db/schema.py
_INV = dict(
    id=0, number=1, date=2, own_company_id=3, seller_name=4, seller_company=5,
    seller_street=6, seller_postal=7, seller_city=8, seller_country=9,
    seller_vat_id=10, seller_email=11, seller_phone=12, customer_id=13,
    buyer_name=14, buyer_company=15, buyer_street=16, buyer_postal=17,
    buyer_city=18, buyer_country=19, buyer_vat_id=20, buyer_reference=21,
    buyer_route_id=22, order_number=23, currency=24, delivery_date=25,
    payment_terms=26, payment_due_date=27, bank_name=31, bank_iban=32,
    bank_bic=33, tax_category=34, tax_rate=35, sum_net=36, tax_amount=37,
    sum_gross=38, amount_due=39, document_version=54,
)
# Spaltenindizes der InvoiceItems-Zeile
_ITM = dict(position=2, description=4, quantity=5, unit=6, price=7, total_net=8,
            tax_category=9, tax_rate=10)

EXEMPT_REASON_KLEINUNTERNEHMER = (
    'Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.')

# Freitext-Einheiten der Rechnungsmaske -> UN/ECE Recommendation 20
_UNIT_CODES = {
    'stk': 'C62', 'stk.': 'C62', 'stück': 'C62', 'stueck': 'C62', 'st': 'C62',
    'st.': 'C62', 'x': 'C62', 'pausch.': 'LS', 'pauschal': 'LS',
    'pauschale': 'LS', 'psch': 'LS', 'psch.': 'LS', 'einheit': 'C62',
    'std': 'HUR', 'std.': 'HUR', 'stunde': 'HUR', 'stunden': 'HUR', 'h': 'HUR',
    'tag': 'DAY', 'tage': 'DAY', 'tg': 'DAY', 'tg.': 'DAY', 'd': 'DAY',
    'woche': 'WEE', 'wochen': 'WEE', 'monat': 'MON', 'monate': 'MON',
    'mon': 'MON', 'jahr': 'ANN', 'jahre': 'ANN', 'min': 'MIN', 'min.': 'MIN',
    'km': 'KMT', 'm': 'MTR', 'cm': 'CMT', 'mm': 'MMT', 'm²': 'MTK',
    'qm': 'MTK', 'm2': 'MTK', 'm³': 'MTQ', 'cbm': 'MTQ', 'm3': 'MTQ',
    'l': 'LTR', 'ltr': 'LTR', 'liter': 'LTR', 'kg': 'KGM', 'g': 'GRM',
    't': 'TNE', 'pkt': 'XPK', 'pkt.': 'XPK', 'packung': 'XPK', 'set': 'SET',
    'paar': 'PR', 'satz': 'SET', 'karton': 'XCT', 'flasche': 'XBO',
    'kwh': 'KWH', 'seite': 'C62', 'lizenz': 'C62',
}
# Anzeige (deutsche Kurzform) je UN/ECE-Code – Rückrichtung für Menschen
_UNIT_LABELS = {
    'C62': 'Stk.', 'HUR': 'Std.', 'DAY': 'Tag', 'WEE': 'Woche', 'MON': 'Monat',
    'ANN': 'Jahr', 'MIN': 'Min.', 'KMT': 'km', 'MTR': 'm', 'CMT': 'cm',
    'MMT': 'mm', 'MTK': 'm²', 'MTQ': 'm³', 'LTR': 'l', 'KGM': 'kg', 'GRM': 'g',
    'TNE': 't', 'XPK': 'Pkt.', 'SET': 'Satz', 'PR': 'Paar', 'KWH': 'kWh',
    'LS': 'pausch.',
}
_UNECE_RE = re.compile(r'^[A-Z0-9]{2,3}$')


def unit_code(text, unknown=None) -> str:
    """Einheit der Rechnungsposition als UN/ECE-Code (Fallback ``C62`` = Stück).

    Groß-/Kleinschreibung und Punkt sind egal; echte UN/ECE-Codes (z. B. aus dem
    WISO-Import) werden durchgereicht. Eine unbekannte Einheit ergibt ``C62``;
    ``unknown`` (Liste) sammelt sie, damit der Fallback sichtbar wird.
    """
    s = str(text or '').strip()
    if not s:
        return 'C62'
    if _UNECE_RE.match(s) and s == s.upper():
        return s                      # bereits ein Code (HUR, C62, DAY, …)
    code = _UNIT_CODES.get(s.lower())
    if code is None:
        if unknown is not None:
            unknown.append(s)
        return 'C62'
    return code


def unit_display(text, default='Stk.') -> str:
    """Lesbare Einheit für Mensch (PDF, Formulare).

    Gespeicherter Freitext bleibt unverändert („Std.“ → „Std.“). Ein gespeicherter
    UN/ECE-Code (Demodaten, WISO-Import) wird zur deutschen Kurzform; ein
    unbekannter Code wird als Code angezeigt. Leer → ``default``.
    """
    s = str(text or '').strip()
    if not s:
        return default
    if _UNECE_RE.match(s) and s == s.upper():
        return _UNIT_LABELS.get(s, s)
    return s


def rate_to_pct(value, default=Decimal('19')) -> Decimal:
    """Steuersatz als Prozentzahl (DB speichert teils 0.19, teils 19)."""
    if value is None or value == '':
        return default
    r = Decimal(str(value))
    if 0 < r < 1:
        r = r * 100
    return r.quantize(Decimal('0.01')).normalize() if r else Decimal('0')


_ILLEGAL_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]')


def _text(value) -> str:
    """Freitext für beide Ausgabeformate: in XML 1.0 verbotene Zeichen entfernen,
    Whitespace normalisieren."""
    if value in (None, ''):
        return ''
    return ' '.join(_ILLEGAL_XML.sub('', str(value)).split())


def _iso_date(value) -> Optional[date]:
    s = str(value or '')[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


@dataclass
class Party:
    name: str
    street: str = ''
    postal_code: str = ''
    city: str = ''
    country: str = 'DE'
    vat_id: str = ''          # USt-IdNr. (BT-31 / BT-48)
    tax_number: str = ''      # nationale Steuernummer (BT-32)
    email: str = ''
    phone: str = ''
    contact_name: str = ''


@dataclass
class Line:
    position: int
    name: str
    quantity: Decimal
    unit_code: str
    unit_price: int            # Minor Units, netto je Einheit
    net: int                   # Minor Units, Positions-Nettosumme
    tax_category: str          # S, Z, E …
    tax_rate: Decimal          # Prozent


@dataclass
class TaxBreakdown:
    category: str
    rate: Decimal
    basis: int                 # Minor Units
    amount: int                # Minor Units
    exemption_reason: str = ''


@dataclass
class Totals:
    line_total: int
    tax_basis: int
    tax_total: int
    grand_total: int
    prepaid: int
    payable: int


@dataclass
class EInvoice:
    number: str
    issue_date: date
    type_code: str                       # 380 = Rechnung
    currency: str
    seller: Party
    buyer: Party
    lines: List[Line]
    tax_breakdown: List[TaxBreakdown]
    totals: Totals
    buyer_reference: str = ''            # BT-10 (Leitweg-ID)
    order_reference: str = ''            # BT-13
    delivery_date: Optional[date] = None
    due_date: Optional[date] = None
    payment_terms: str = ''
    bank_name: str = ''
    iban: str = ''
    bic: str = ''
    notes: List[str] = field(default_factory=list)     # BT-22: Freitext zur Rechnung (Hinweis einer Berichtigung)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_exempt(self) -> bool:
        return any(t.category == 'E' for t in self.tax_breakdown)


def _german_date(day) -> str:
    return day.strftime('%d.%m.%Y')


def correction_note_text(notice) -> str:
    """Der Hinweis einer berichtigten Fassung im Klartext, für PDF, CII (BT-22) und UBL (``cbc:Note``) derselbe:
    „Berichtigte Fassung vom 12.11.2026 – ersetzt die Fassung vom 04.03.2026 (geändert: Leistungsdatum)“.
    ``notice``: :class:`db.invoice_versions.CorrectionNotice`. Ist das Datum der ersetzten Fassung unbekannt,
    entfällt dieser Teil."""
    text = f'Berichtigte Fassung vom {_german_date(notice.corrected_on)}'
    if notice.replaces_on:
        text += f' – ersetzt die Fassung vom {_german_date(notice.replaces_on)}'
    if notice.fields:
        text += f' (geändert: {", ".join(notice.fields)})'
    return text


def correction_notice(db, invoice):
    """Der Hinweis, den die Rechnungszeile ``invoice`` (aus ``db.get_invoice_by_id``) trägt, wenn ihre
    aktuelle Fassung eine Berichtigung ist (``Database.correction_notice``) – sonst ``None``. Ohne Datenbank
    oder bei einer Rechnung in Fassung 1 gibt es keinen und keine Abfrage."""
    version = invoice[_INV['document_version']] if len(invoice) > _INV['document_version'] else None
    if db is None or not version or version <= 1:
        return None
    return db.correction_notice(invoice[_INV['id']])


def _party(name, street, postal, city, country, vat_id='', email='', phone='',
           contact='', tax_number=''):
    return Party(name=_text(name), street=_text(street), postal_code=_text(postal),
                 city=_text(city), country=(_text(country) or 'DE').upper(),
                 vat_id=_text(vat_id).replace(' ', ''), tax_number=_text(tax_number),
                 email=_text(email), phone=_text(phone), contact_name=_text(contact))


def _is_vat_id(s: str) -> bool:
    return bool(re.match(r'^[A-Za-z]{2}[A-Za-z0-9+*.]{2,12}$', s or ''))


def build_einvoice(db, invoice, items) -> EInvoice:
    """Rechnungszeile + Positionen aus der DB in das neutrale Modell übersetzen.

    Args:
        db: Datenbank (für Kontakt-Fallbacks: E-Mail, Steuernummer); darf ``None`` sein.
        invoice: Zeile aus ``db.get_invoice_by_id`` (Beträge als Euro-Decimal).
        items: Zeilen aus ``db.get_invoice_items``.
    """
    def col(name):
        idx = _INV[name]
        return invoice[idx] if len(invoice) > idx else None

    warnings: List[str] = []
    raw_rate = col('tax_rate')
    # Sentinel -1 = Kleinunternehmer (§ 19 UStG): keine Umsatzsteuer
    exempt = raw_rate is not None and Decimal(str(raw_rate)) < 0
    default_rate = Decimal('0') if exempt else rate_to_pct(raw_rate, Decimal('19'))

    lines: List[Line] = []
    unknown_units: List[str] = []
    for it in items:
        qty = Decimal(str(it[_ITM['quantity']] if it[_ITM['quantity']] is not None else 1))
        price = to_minor(it[_ITM['price']] or 0)
        net = to_minor(it[_ITM['total_net']] or 0)
        if exempt:
            category, rate = 'E', Decimal('0')
        else:
            item_rate = it[_ITM['tax_rate']] if len(it) > _ITM['tax_rate'] else None
            rate = rate_to_pct(item_rate, default_rate)
            category = 'S' if rate > 0 else (it[_ITM['tax_category']] or 'Z')
            if rate == 0 and category == 'S':
                category = 'Z'
        lines.append(Line(
            position=int(it[_ITM['position']]), name=_text(it[_ITM['description']]) or 'Position',
            quantity=qty.normalize() if qty == qty.to_integral() else qty,
            unit_code=unit_code(it[_ITM['unit']], unknown_units), unit_price=price, net=net,
            tax_category=category, tax_rate=rate))

    for u in sorted(set(unknown_units)):
        msg = f"Unbekannte Einheit '{u}' – als C62 (Stück) ausgegeben"
        warnings.append(msg)
        print(f"E-Rechnung {_text(col('number'))}: {msg}")

    # Steueraufschlüsselung je (Kategorie, Satz); Steuer je Gruppe auf Cent gerundet
    groups = {}
    for ln in lines:
        groups[(ln.tax_category, ln.tax_rate)] = groups.get(
            (ln.tax_category, ln.tax_rate), 0) + ln.net
    breakdown = []
    for (category, rate), basis in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        amount = 0 if category in ('E', 'Z') else round_minor(tax_from_net(basis, rate), 2)
        breakdown.append(TaxBreakdown(
            category=category, rate=rate, basis=basis, amount=amount,
            exemption_reason=EXEMPT_REASON_KLEINUNTERNEHMER if category == 'E' else ''))

    line_total = sum(ln.net for ln in lines)
    tax_total = sum(t.amount for t in breakdown)
    grand = line_total + tax_total
    # Das Dokument zeigt den Betrag der Rechnung, nie den Stand der Zahlungen: Ein Dokument ist die
    # Rechnung, die der Kunde bekommen hat, nicht der offene Posten von heute (AmountDue).
    totals = Totals(line_total=line_total, tax_basis=line_total, tax_total=tax_total,
                    grand_total=grand, prepaid=0, payable=grand)

    for label, stored, derived in (('Netto', col('sum_net'), line_total),
                                   ('Steuer', col('tax_amount'), tax_total),
                                   ('Brutto', col('sum_gross'), grand)):
        if stored is not None and to_minor(stored) != derived:
            warnings.append(f'{label} gespeichert {stored} ≠ aus Positionen {derived}')

    # Kontakt-Fallbacks (E-Mail/Steuernummer der Beteiligten)
    own_email = col('seller_email')
    seller_tax = ''
    seller_vat = _text(col('seller_vat_id')).replace(' ', '')
    buyer_email = ''
    if db is not None:
        try:
            own_id = col('own_company_id')
            own = db.get_contact_by_id(own_id) if own_id else None
            if own is not None:
                own_email = own_email or own[9]
                contact_tax = _text(own[11]).replace(' ', '')
                if contact_tax and not seller_vat:
                    if _is_vat_id(contact_tax) and contact_tax[:2].isalpha():
                        seller_vat = contact_tax
                    else:
                        seller_tax = contact_tax
            cust_id = col('customer_id')
            cust = db.get_contact_by_id(cust_id) if cust_id else None
            if cust is not None:
                buyer_email = cust[9] or ''
        except Exception as e:                       # Fallbacks sind optional
            warnings.append(f'Kontaktdaten nicht lesbar: {e}')

    seller = _party(col('seller_company') or col('seller_name') or 'Verkäufer',
                    col('seller_street'), col('seller_postal'), col('seller_city'),
                    col('seller_country'), vat_id=seller_vat, email=own_email,
                    phone=col('seller_phone'), contact=col('seller_name'),
                    tax_number=seller_tax)
    buyer = _party(col('buyer_company') or col('buyer_name') or 'Käufer',
                   col('buyer_street'), col('buyer_postal'), col('buyer_city'),
                   col('buyer_country'), vat_id=col('buyer_vat_id') or '',
                   email=buyer_email, contact=col('buyer_name'))

    notice = correction_notice(db, invoice)
    return EInvoice(
        number=_text(col('number')) or 'DRAFT',
        issue_date=_iso_date(col('date')) or date.today(),
        type_code='380',
        currency=_text(col('currency')) or 'EUR',
        seller=seller, buyer=buyer, lines=lines, tax_breakdown=breakdown,
        totals=totals,
        buyer_reference=_text(col('buyer_route_id')) or _text(col('buyer_reference')),
        order_reference=_text(col('order_number')),
        delivery_date=_iso_date(col('delivery_date')),
        due_date=_iso_date(col('payment_due_date')),
        payment_terms=_text(col('payment_terms')),
        bank_name=_text(col('bank_name')),
        iban=_text(col('bank_iban')).replace(' ', ''),
        bic=_text(col('bank_bic')).replace(' ', ''),
        notes=[correction_note_text(notice)] if notice else [],
        warnings=warnings)
