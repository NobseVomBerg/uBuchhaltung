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

Eine Stornorechnung (``Invoices.InvoiceKind = 'storno'``) ist eine Gutschrift (Typ 381) mit Bezug auf die
Ursprungsrechnung (BT-25/BT-26). Die Datenbank führt sie mit negativer Menge und negativen Summen (so zeigt sie das
PDF); im Modell – und damit in CII und UBL – stehen die Beträge positiv, wie EN 16931 es für 381 verlangt: Das Modell
negiert Mengen, Zeilen-, Steuer- und Gesamtsummen genau einmal.
"""
import html
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional, Tuple

from db.invoice_lock import STORNO_KIND
from money import SCALE, format_de, to_minor
from richtext import sanitize_richtext
from tax_groups import rate_groups

# Spaltenindizes der Invoices-Zeile (``SELECT *``), siehe db/schema.py
_INV = dict(
    id=0, number=1, date=2, own_company_id=3, seller_name=4, seller_company=5,
    seller_street=6, seller_postal=7, seller_city=8, seller_country=9,
    seller_vat_id=10, seller_email=11, seller_phone=12, customer_id=13,
    buyer_name=14, buyer_company=15, buyer_street=16, buyer_postal=17,
    buyer_city=18, buyer_country=19, buyer_vat_id=20, buyer_reference=21,
    buyer_route_id=22, order_number=23, currency=24, delivery_date=25,
    payment_terms=26, payment_due_date=27, skonto_days=28, skonto_percent=29, bank_name=31, bank_iban=32,
    bank_bic=33, tax_category=34, tax_rate=35, sum_net=36, tax_amount=37,
    sum_gross=38, amount_due=39, invoice_kind=50, cancels_invoice_id=51, replaces_invoice_id=52, document_version=54,
)
# Spaltenindizes der InvoiceItems-Zeile
_ITM = dict(position=2, description=4, quantity=5, unit=6, price=7, total_net=8,
            tax_category=9, tax_rate=10)

# Ein Wortlaut für alle Ausgaben: BT-120 (CII), ``cbc:TaxExemptionReason`` (UBL) und der Hinweis im PDF
EXEMPT_REASON_KLEINUNTERNEHMER = (
    'Es gilt die Steuerbefreiung für Kleinunternehmer nach § 19 UStG. Es wird keine Umsatzsteuer berechnet.')

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


# Ein Freitext trägt die Steuerbefreiung schon, wenn er das Gesetz ausdrücklich zitiert: „§ 19“ (auch „§19“ und „§ 19a“,
# nicht „§ 190“), höchstens gefolgt von „Abs. n“ / „Absatz n“ und danach „Satz n“ / „S. n“, und unmittelbar danach „UStG“
# oder „Umsatzsteuergesetz“ – dazwischen nur Leerraum (auch ein geschütztes). Jedes Füllwort („des“, „gem.“ …), „§ 19 BGB“,
# „§ 19 der AGB“ oder nur das Wort „Kleinunternehmer“ genügt nicht: Dann steht der Hinweis eben doppelt. Gesucht wird im
# casefold-Text.
_LAW_CITATION = re.compile(
    r'§\s*19a?(?![0-9])'
    r'(?:\s*(?:abs\.?|absatz)\s*[0-9]+(?:\s*(?:satz|s\.)\s*[0-9]+)?)?'
    r'\s*(?:ustg|umsatzsteuergesetz)')
# die Tags der Positivliste, die ``sanitize_richtext`` stehen lässt (ein loses „<“ ist dort längst als „&lt;“ maskiert)
_HTML_TAG = re.compile(r'<[^<>]*>')


def _printed_text(richtext) -> str:
    """Der Text eines Richtext-Feldes, wie das PDF ihn setzt (``export.pdf_document.flow_richtext``): durch die
    Positivliste (``sanitize_richtext``: ein ``<style>``-Block, Kommentare und Attribute fallen weg), Tags entfernt,
    Entitäten einmal aufgelöst."""
    return html.unescape(_HTML_TAG.sub('', sanitize_richtext(richtext) or ''))


def small_business_note(tax_rate, *richtexts) -> Optional[str]:
    """Der Hinweis auf § 19 UStG, den das PDF eines Belegs ohne Umsatzsteuerausweis druckt – sonst ``None``.

    Ohne Ausweis steht in ``Invoices.TaxRate`` der Platzhalter −1 (negativ; kein Satz gilt wie im PDF als 19 %,
    0 % ist ein anderer Fall). ``richtexts`` sind die Freitexte des Belegs, die das PDF als Richtext setzt (Rechnung:
    der Zahlungshinweis, Angebot: Einleitung und Schluss); die Positionstexte gehören nicht dazu. Zitiert einer
    dieser Texte das Gesetz ausdrücklich (``_LAW_CITATION``, je Text einzeln, so wie das PDF ihn druckt), bleibt es
    beim Text des Nutzers und es gibt keinen zweiten. Im Zweifel steht der Hinweis da – ein fehlender Pflichthinweis
    wiegt schwerer als ein doppelter. Was kein Text ist, wird übergangen."""
    if tax_rate is None or tax_rate >= 0:
        return None
    if any(isinstance(value, str) and _LAW_CITATION.search(_printed_text(value).casefold()) for value in richtexts):
        return None
    return EXEMPT_REASON_KLEINUNTERNEHMER


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
    identifier: str = ''      # Kennung des Verkäufers (BT-29); ohne USt-IdNr. die Steuernummer (sonst verletzt BR-CO-26)
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
class Skonto:
    """Das Skonto einer Rechnung: ``percent`` Prozent bei Zahlung bis zum Tag ``until``."""
    days: int
    percent: Decimal           # Prozent, auf zwei Nachkommastellen
    until: date                # letzter Tag der Skontofrist: Rechnungsdatum plus ``days``


@dataclass
class EInvoice:
    number: str
    issue_date: date
    type_code: str                       # 380 = Rechnung, 381 = Gutschrift (Stornorechnung)
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
    preceding_invoice_number: str = ''                 # BT-25: Nummer der Ursprungsrechnung (Stornorechnung)
    preceding_invoice_date: Optional[date] = None      # BT-26: deren Rechnungsdatum
    warnings: List[str] = field(default_factory=list)
    skonto: Optional[Skonto] = None                    # nie bei einer Stornorechnung
    skonto_unreadable: bool = False                    # gespeicherte Werte unbrauchbar: kein Skonto, dafür eine Lücke

    @property
    def is_exempt(self) -> bool:
        return any(t.category == 'E' for t in self.tax_breakdown)


def _german_date(day) -> str:
    return day.strftime('%d.%m.%Y')


# ── Skonto ───────────────────────────────────────────────────────────────────
# Die Spalten sind ``SkontoDays INTEGER`` und ``SkontoPercent REAL``, das Formular schickt Zahlen – ein Client oder die
# SQL-Konsole kann aber alles hineinlegen. Geprüft wird deshalb, BEVOR etwas in eine Zahl umgewandelt wird: Ein Text wie
# ``1e9999999`` bliebe als ``Decimal`` harmlos, ``int()`` darauf aber rechnete minutenlang. Leer und 0 heißen beide
# „kein Skonto“ (so schickt es das Formular).

SKONTO_PAIR_MESSAGE = 'Skonto braucht Prozent und Tage.'
SKONTO_PERCENT_MESSAGE = 'Skonto: Prozent größer 0 und kleiner 100, höchstens zwei Nachkommastellen.'
SKONTO_DAYS_MESSAGE = 'Skonto: Tage als ganze Zahl von 1 bis 365.'
SKONTO_AFTER_DUE_MESSAGE = 'Die Skontofrist endet nach der Fälligkeit.'
SKONTO_DATE_MESSAGE = 'Skonto braucht ein gültiges Rechnungsdatum.'
SKONTO_POSITIVE_MESSAGE = 'Skonto nur bei einem positiven Rechnungsbetrag.'
MAX_SKONTO_DAYS = 365
_CENT = Decimal('0.01')
_PERCENT_TEXT = re.compile(r'[0-9]{1,2}(?:\.[0-9]{1,2})?')


def _skonto_days(value) -> Optional[int]:
    """Die Skonto-Tage: ``None``, wenn leer (nichts, Leerraum oder 0), sonst eine ganze Zahl von 1 bis 365 – sonst
    ``ValueError``. Angenommen werden ein ``int`` (kein ``bool``), ein endlicher, ganzzahliger ``float`` und ein Text aus
    1 bis 3 ASCII-Ziffern; alles andere (Exponent, Vorzeichen, Komma, länger, ein anderer Typ) ist ungültig."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if len(text) > 3 or not (text.isascii() and text.isdigit()):
            raise ValueError(SKONTO_DAYS_MESSAGE)
        number = int(text)
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(SKONTO_DAYS_MESSAGE)
    elif isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
        raise ValueError(SKONTO_DAYS_MESSAGE)
    else:
        number = value
    if number == 0:
        return None
    if not 1 <= number <= MAX_SKONTO_DAYS:
        raise ValueError(SKONTO_DAYS_MESSAGE)
    return int(number)


def _skonto_percent(value) -> Optional[Decimal]:
    """Das Skonto in Prozent: ``None``, wenn leer (nichts, Leerraum oder 0), sonst eine Zahl größer 0 und kleiner 100 –
    sonst ``ValueError``. Angenommen werden eine endliche Zahl (``int`` ohne ``bool``, ``float`` – über ihre Textform gelesen,
    2.5 ist 2,5 –, ``Decimal``) und ein Text der Form ``12`` oder ``2.5`` (höchstens zwei Stellen davor und danach); die
    Anzahl der Nachkommastellen prüft :func:`parse_skonto`."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if not _PERCENT_TEXT.fullmatch(text):
            raise ValueError(SKONTO_PERCENT_MESSAGE)
        number = Decimal(text)
    elif isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(SKONTO_PERCENT_MESSAGE)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(SKONTO_PERCENT_MESSAGE)
        number = value
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(SKONTO_PERCENT_MESSAGE)
    else:
        number = value
    if number == 0:
        return None
    if not 0 < number < 100:
        raise ValueError(SKONTO_PERCENT_MESSAGE)
    return number if isinstance(number, Decimal) else Decimal(str(number))


def parse_skonto(days, percent) -> Optional[Tuple[int, Decimal]]:
    """Das Skonto aus den gespeicherten (oder im Formular angegebenen) Werten: ``None`` ohne Skonto (beide leer oder 0),
    sonst ``(Tage, Prozent)``. Gültig sind Tage als ganze Zahl von 1 bis 365 und Prozent größer 0 und kleiner 100 mit
    höchstens zwei Nachkommastellen; eine Angabe allein genügt nicht. Alles andere ist ein ``ValueError`` mit deutschem
    Klartext – und kommt in Millisekunden zurück, gleich was geschickt wird (siehe oben)."""
    n_days = _skonto_days(days)
    n_percent = _skonto_percent(percent)
    if n_days is None and n_percent is None:
        return None
    if n_days is None or n_percent is None:
        raise ValueError(SKONTO_PAIR_MESSAGE)
    if n_percent != n_percent.quantize(_CENT):
        raise ValueError(SKONTO_PERCENT_MESSAGE)
    return n_days, n_percent.quantize(_CENT)


def skonto_until(issue_date, days) -> Optional[date]:
    """Der letzte Tag der Skontofrist: Rechnungsdatum plus ``days``; ``None``, wenn das Datum nicht darstellbar ist."""
    try:
        return issue_date + timedelta(days=days)
    except OverflowError:
        return None


def skonto_amount(gross_minor, percent) -> int:
    """Der Skontobetrag in Minor Units: ``percent`` Prozent des Bruttos, in EINEM Schritt kaufmännisch auf den Cent
    gerundet (nicht erst auf die Minor Unit und danach auf den Cent: 0,99 € × 0,5 % sind 0,00 €, nicht 0,01 €)."""
    cent = 10 ** (SCALE - 2)
    exact = Decimal(int(gross_minor)) * Decimal(str(percent)) / 100 / cent
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP)) * cent


def skonto_terms_line(skonto: Skonto) -> str:
    """Die Skonto-Zeile der Zahlungsbedingungen, wie BR-DE-18 der XRechnung sie verlangt:
    ``#SKONTO#TAGE=7#PROZENT=2.00#`` (Prozent mit zwei Nachkommastellen und Punkt, ohne ``BASISBETRAG``)."""
    return f'#SKONTO#TAGE={skonto.days}#PROZENT={skonto.percent:.2f}#'


def payment_terms_text(inv: EInvoice) -> str:
    """Die Zahlungsbedingungen (BT-20) für CII und UBL: die Skonto-Zeile, ein Zeilenumbruch und der Freitext der Rechnung.
    Ohne Skonto steht der Freitext allein. Die Zeile steht vorn: BR-DE-18 prüft jede Zeile für sich."""
    if inv.skonto is None:
        return inv.payment_terms
    return f'{skonto_terms_line(inv.skonto)}\n{inv.payment_terms}'


def skonto_sentence(skonto: Skonto, gross_minor, due_date=None) -> str:
    """Der Absatz im PDF: „Bei Zahlung bis zum 11.03.2026 (innerhalb von 7 Tagen ab Rechnungsdatum) gewähren wir 2 %
    Skonto: abzüglich 3,56 EUR sind dann 174,35 EUR zu zahlen. Ohne Abzug zahlbar bis zum 18.03.2026.“ Grundlage ist das
    gedruckte Brutto ``gross_minor``; der letzte Satz steht nur mit Fälligkeit – und nur, wenn die Skontofrist nicht nach ihr
    endet (ein Altbestand kann das tragen; das Dokument widerspricht sich dann nicht)."""
    amount = skonto_amount(gross_minor, skonto.percent)
    days = f'{skonto.days} Tag' if skonto.days == 1 else f'{skonto.days} Tagen'
    percent = f'{skonto.percent.normalize():f}'.replace('.', ',')
    text = (f'Bei Zahlung bis zum {_german_date(skonto.until)} (innerhalb von {days} ab Rechnungsdatum) gewähren wir '
            f'{percent} % Skonto: abzüglich {format_de(amount)} EUR sind dann {format_de(gross_minor - amount)} EUR '
            'zu zahlen.')
    if due_date and skonto.until <= due_date:
        text += f' Ohne Abzug zahlbar bis zum {_german_date(due_date)}.'
    return text


def _read_skonto(days, percent, issue_date) -> Tuple[Optional[Skonto], bool]:
    """``(Skonto, unlesbar)`` aus den gespeicherten Werten: Sind sie unbrauchbar (Altbestand, SQL-Konsole), gibt es kein
    Skonto im Modell, aber ``unlesbar`` – der Verlust bleibt nicht still (:func:`einvoice_gaps`)."""
    try:
        parsed = parse_skonto(days, percent)
    except ValueError:
        return None, True
    if parsed is None:
        return None, False
    until = skonto_until(issue_date, parsed[0])
    if until is None:
        return None, True
    return Skonto(parsed[0], parsed[1], until), False


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
    # nur eine Ganzzahl über 1 ist eine berichtigte Fassung (die Spalte lässt sich per SQL beschreiben: ein Text
    # oder 0 gilt als Fassung 1, wie ``db.invoice_lock.as_document_version`` es liest)
    if db is None or isinstance(version, bool) or not isinstance(version, int) or version <= 1:
        return None
    return db.correction_notice(invoice[_INV['id']])


def storno_note_text(number, day) -> str:
    """Der Hinweis einer Stornorechnung im Klartext (BT-22): „Storno zu Rechnung R26001 vom 15.01.2026“; ohne
    bekanntes Datum der Ursprungsrechnung entfällt „vom …“."""
    text = f'Storno zu Rechnung {number}'
    return f'{text} vom {_german_date(day)}' if day else text


def replacement_note_text(number, storno_number) -> str:
    """Der Hinweis einer Ersatzrechnung im Klartext, für PDF, CII (BT-22) und UBL (``cbc:Note``) derselbe: „Ersetzt die
    stornierte Rechnung R26001 (Stornorechnung R26002)“; ohne bekannte Stornorechnung entfällt die Klammer."""
    text = f'Ersetzt die stornierte Rechnung {number}'
    return f'{text} (Stornorechnung {storno_number})' if storno_number else text


def replacement_note(db, invoice):
    """Der Hinweis, den die Rechnungszeile ``invoice`` (aus ``db.get_invoice_by_id``) trägt, wenn sie eine stornierte
    Rechnung ersetzt (``Invoices.ReplacesInvoiceId``) – sonst ``None``. Er ergibt sich aus dem Stand der Datenbank, nicht
    aus einem Parameter: Auch ein später über „PDF + E-Rechnung“ erzeugtes Dokument trägt ihn. Ohne Datenbank, ohne
    Verweis oder ohne die ersetzte Rechnung gibt es keinen Hinweis; die Nummer der Stornorechnung nennt
    ``db.find_storno_invoice``."""
    column = _INV['replaces_invoice_id']
    replaced_id = invoice[column] if len(invoice) > column else None
    # nur eine positive Ganzzahl verweist auf eine Rechnung (die Spalte lässt sich per SQL beschreiben)
    if db is None or isinstance(replaced_id, bool) or not isinstance(replaced_id, int) or replaced_id <= 0:
        return None
    original = db.get_invoice_by_id(replaced_id)
    if original is None:
        return None
    storno = db.find_storno_invoice(replaced_id)
    return replacement_note_text(_text(original[_INV['number']]), _text(storno[1]) if storno else '')


def _party(name, street, postal, city, country, vat_id='', email='', phone='',
           contact='', tax_number='', identifier=''):
    return Party(name=_text(name), street=_text(street), postal_code=_text(postal),
                 city=_text(city), country=(_text(country) or 'DE').upper(),
                 vat_id=_text(vat_id).replace(' ', ''), tax_number=_text(tax_number),
                 identifier=_text(identifier), email=_text(email), phone=_text(phone), contact_name=_text(contact))


# Die USt-IdNr. je Land: das Länderkürzel der E-Rechnung (BR-CO-09: ISO 3166-1, Griechenland ``EL``, Nordirland ``XI``) und der Rest als
# Muster (``fullmatch``, ASCII). Gesucht wird nach dem Entfernen von Leerzeichen, Bindestrichen und Punkten, in Großschreibung.
# ``CHE`` (Schweiz) und ``NO`` (Norwegen) tragen ihr Suffix (MWST/TVA/IVA, MVA) hinten.
_VAT_ID_RULES = {
    'AT': r'U\d{8}', 'BE': r'\d{9,10}', 'BG': r'\d{9,10}', 'CY': r'\d{8}[A-Z]', 'CZ': r'\d{8,10}', 'DE': r'\d{9}',
    'DK': r'\d{8}', 'EE': r'\d{9}', 'EL': r'\d{9}', 'ES': r'[A-Z0-9]\d{7}[A-Z0-9]', 'FI': r'\d{8}',
    'FR': r'[A-HJ-NP-Z0-9]{2}\d{9}', 'HR': r'\d{11}', 'HU': r'\d{8}', 'IE': r'\d{7}[A-Z]{1,2}|\d[A-Z0-9+*]\d{5}[A-Z]',
    'IT': r'\d{11}', 'LT': r'\d{9}|\d{12}', 'LU': r'\d{8}', 'LV': r'\d{11}', 'MT': r'\d{8}', 'NL': r'\d{9}B\d{2}',
    'PL': r'\d{10}', 'PT': r'\d{9}', 'RO': r'\d{2,10}', 'SE': r'\d{12}', 'SI': r'\d{8}', 'SK': r'\d{10}',
    'XI': r'\d{9}|\d{12}|GD\d{3}|HA\d{3}', 'GB': r'\d{9}|\d{12}|GD\d{3}|HA\d{3}',
    'CHE': r'\d{9}(?:MWST|TVA|IVA)?', 'NO': r'\d{9}(?:MVA)?',
}
_VAT_ID_PATTERNS = {prefix: re.compile(rest, re.ASCII) for prefix, rest in _VAT_ID_RULES.items()}

# Eine Bezeichnung vor der Nummer („USt-IdNr. DE123456789“, „St.-Nr.: 12/345/67890“): ohne Groß-/Kleinschreibung, mit oder ohne
# Punkte und Bindestriche, gefolgt von Doppelpunkt und Leerraum. „VAT No“ gilt nur, wenn danach kein Buchstabe und keine Ziffer
# folgt – sonst wäre das „NO“ von „NO123456789MVA“ (Norwegen) eine Bezeichnung.
_LABEL_GAP = r'[\s.\-]*'
_LABEL = re.compile(
    r'^\s*(?:'
    r'U\s*St' + _LABEL_GAP + r'Id(?:' + _LABEL_GAP + r'Nr)?'                       # USt-IdNr, USt-Id-Nr, USt-ID, UStID
    r'|UID(?:' + _LABEL_GAP + r'Nr)?'                                               # UID
    r'|VAT(?:' + _LABEL_GAP + r'(?:ID|Nr|Number|No(?![A-Za-z0-9])))?'               # VAT, VAT-ID, VAT No
    r'|Umsatzsteuer' + _LABEL_GAP + r'(?:Identifikationsnummer|IdNr|ID)'            # Umsatzsteuer-ID, -Identifikationsnummer
    r'|St(?:euer)?' + _LABEL_GAP + r'N(?:r|ummer)'                                  # St.-Nr, StNr, Steuer-Nr, Steuernummer, Steuernr
    r')(?![A-Za-z])[\s.:\-]*', re.IGNORECASE)


def tax_id_kind(text) -> Tuple[str, str]:
    """Ist die Angabe eine USt-IdNr. (``'vat'``) oder eine Steuernummer (``'tax'``)? Liefert ``(Art, Wert)``; leer ergibt
    ``('', '')``. Das Feld der Kontakte und die Momentaufnahme der Rechnung (``SellerVATID``) tragen beides in einer Spalte –
    EN 16931 trennt die USt-IdNr. (BT-31, CII ``VA``, UBL ``VAT``) von der Steuernummer (BT-32, ``FC``, ``TAX``), und
    eine Steuernummer als USt-IdNr. verletzt BR-CO-09.

    Eine vorangestellte Bezeichnung („USt-IdNr.“, „VAT-ID“, „St.-Nr.“, „Steuernummer“ …) wird zuerst abgeschnitten. Eine
    USt-IdNr. ist die Angabe nur, wenn sie – ohne Leerzeichen, Bindestriche und Punkte, in Großschreibung – mit dem Länderkürzel
    eines Landes beginnt und der Rest zu den Nummern dieses Landes passt (``DE`` und neun Ziffern, ``ATU`` und acht …; die Tabelle
    ``_VAT_ID_RULES``); ihr Wert ist genau dieser bereinigte Text (``DE-123 456 789`` ergibt ``DE123456789``). Alles andere ist
    eine Steuernummer – auch ``BY 123 456 78901``, ``HRB 12345`` oder ein ``DE12345678`` mit zu wenigen Ziffern – und bleibt, wie
    sie eingegeben wurde: ohne die Bezeichnung, außen getrimmt, Leerraum in der Mitte auf ein Leerzeichen."""
    value = _LABEL.sub('', _text(text), count=1).strip()
    if not value:
        return '', ''
    compact = re.sub(r'[\s.\-]', '', value).upper()
    prefix = 'CHE' if compact.startswith('CHE') else compact[:2]
    rule = _VAT_ID_PATTERNS.get(prefix)
    if rule is not None and rule.fullmatch(compact[len(prefix):]):
        return 'vat', compact
    return 'tax', value


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
    # Stornorechnung: gespeichert negativ, im Modell (CII/UBL, Typ 381) positiv – einmal negiert, hier
    storno = col('invoice_kind') == STORNO_KIND
    sign = -1 if storno else 1

    lines: List[Line] = []
    unknown_units: List[str] = []
    for it in items:
        qty = Decimal(str(it[_ITM['quantity']] if it[_ITM['quantity']] is not None else 1)) * sign
        price = to_minor(it[_ITM['price']] or 0)
        net = to_minor(it[_ITM['total_net']] or 0) * sign
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

    # Steueraufschlüsselung je (Kategorie, Satz); Steuer je Gruppe auf Cent gerundet. Die Gruppe des Dokuments bleibt
    # (Kategorie, Satz) – rate_groups gruppiert nur nach dem Satz –, die Steuer einer Gruppe rechnet dieselbe Regel wie die
    # Summen der Rechnung (tax_groups.rate_groups: Steuer aus der Nettosumme der Gruppe)
    groups = {}
    for ln in lines:
        groups[(ln.tax_category, ln.tax_rate)] = groups.get(
            (ln.tax_category, ln.tax_rate), 0) + ln.net
    breakdown = []
    for (category, rate), basis in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        amount = 0 if category in ('E', 'Z') else rate_groups([(rate, basis)])[0].tax
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
        if stored is not None and to_minor(stored) * sign != derived:
            warnings.append(f'{label} gespeichert {stored} ≠ aus Positionen {derived}')

    # Kontakt-Fallbacks (E-Mail/Steuernummer der Beteiligten). Steuernummer und USt-IdNr. stehen in der Momentaufnahme
    # wie im Kontakt in EINER Spalte; je Art kommen sie an ihre Stelle (BT-31 / BT-32). Der Kontakt springt nur ein,
    # wenn die Momentaufnahme leer ist (Import)
    own_email = col('seller_email')
    seller_kind, seller_number = tax_id_kind(col('seller_vat_id'))
    buyer_email = ''
    if db is not None:
        try:
            own_id = col('own_company_id')
            own = db.get_contact_by_id(own_id) if own_id else None
            if own is not None:
                own_email = own_email or own[9]
                if not seller_number:
                    seller_kind, seller_number = tax_id_kind(own[11])
            cust_id = col('customer_id')
            cust = db.get_contact_by_id(cust_id) if cust_id else None
            if cust is not None:
                buyer_email = cust[9] or ''
        except Exception as e:                       # Fallbacks sind optional
            warnings.append(f'Kontaktdaten nicht lesbar: {e}')

    # Ohne USt-IdNr. verlangt BR-CO-26 eine Kennung (BT-29) oder Registerkennung (BT-30): Die Steuernummer steht dann zusätzlich als
    # Verkäuferkennung da (BT-29), neben BT-32; mit USt-IdNr. gibt es kein BT-29
    seller = _party(col('seller_company') or col('seller_name') or 'Verkäufer',
                    col('seller_street'), col('seller_postal'), col('seller_city'),
                    col('seller_country'), vat_id=seller_number if seller_kind == 'vat' else '', email=own_email,
                    phone=col('seller_phone'), contact=col('seller_name'),
                    tax_number=seller_number if seller_kind == 'tax' else '',
                    identifier=seller_number if seller_kind == 'tax' else '')
    # Der Käufer hat in EN 16931 nur ein Feld für die USt-IdNr. (BT-48), keines für eine Steuernummer: Sie fällt weg
    buyer_kind, buyer_number = tax_id_kind(col('buyer_vat_id'))
    buyer = _party(col('buyer_company') or col('buyer_name') or 'Käufer',
                   col('buyer_street'), col('buyer_postal'), col('buyer_city'),
                   col('buyer_country'), vat_id=buyer_number if buyer_kind == 'vat' else '',
                   email=buyer_email, contact=col('buyer_name'))

    preceding_number, preceding_date, notes = '', None, []
    if storno:
        # Bezug auf die Ursprungsrechnung (BT-25/BT-26) und der Hinweis dazu (BT-22); eine Stornorechnung trägt nie
        # den Hinweis einer Berichtigung
        original = db.get_invoice_by_id(col('cancels_invoice_id')) if db is not None and col('cancels_invoice_id') else None
        if original is not None:
            preceding_number = _text(original[_INV['number']])
            preceding_date = _iso_date(original[_INV['date']])
            notes = [storno_note_text(preceding_number, preceding_date)]
    else:
        # Eine Ersatzrechnung nennt die stornierte Rechnung; eine berichtigte Fassung dazu ihren Hinweis
        notice = correction_notice(db, invoice)
        notes = [note for note in (replacement_note(db, invoice), correction_note_text(notice) if notice else None)
                 if note]
    issue_date = _iso_date(col('date')) or date.today()
    # Eine Stornorechnung hat nie Skonto, auch nicht, wenn in ihrer Zeile ein Altwert steht – ebenso kein Beleg über höchstens
    # 0 (Gutschrift, 0-€-Rechnung): Dort gibt es nichts abzuziehen, und das ist keine Lücke
    skonto, skonto_unreadable = ((None, False) if storno or grand <= 0
                                 else _read_skonto(col('skonto_days'), col('skonto_percent'), issue_date))
    return EInvoice(
        number=_text(col('number')) or 'DRAFT',
        issue_date=issue_date,
        type_code='381' if storno else '380',
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
        notes=notes, preceding_invoice_number=preceding_number, preceding_invoice_date=preceding_date,
        warnings=warnings, skonto=skonto, skonto_unreadable=skonto_unreadable)


# ── Pflichtangaben: Was fehlt, damit die E-Rechnung gültig ist? ──────────────
# Gemessen an Mustang (XSD und Schematron: EN 16931, XRechnung 3.0). Jede Lücke ist an dem Fehler erkannt, den ihr Fehlen
# dort auslöst; die Regel steht dabei.

# Eine Zeile der Zahlungsbedingungen (BT-20), die mit ``#`` beginnt, gilt der XRechnung als Skonto-Angabe, und mit der Skonto-Zeile
# der App (vorn) darf kein ``#…#`` im Freitext stehen (BR-DE-18). Verweigert wird jedes ``#``: leicht zu sagen, und Mustang
# prüft den Fall mit Zeilenanfang oder mit Skonto-Zeile ohnehin als Fehler. Die CII im PDF bleibt davon unberührt.
HASH_IN_PAYMENT_TERMS_GAP = ('Zahlungshinweis ohne ‚#‘ (das Zeichen ist in der XRechnung der Skonto-Angabe vorbehalten – '
                             'Skonto bitte in die Skonto-Felder eintragen)')
SCOPE_XRECHNUNG = 'xrechnung'     # nur das UBL der XRechnung ist betroffen (die Regeln BR-DE und PEPPOL)
SCOPE_EN16931 = 'en16931'         # auch die CII im PDF (ZUGFeRD, Profil EN 16931) ist betroffen


@dataclass
class Gap:
    """Eine fehlende Angabe der E-Rechnung: ``text`` nennt sie für den Nutzer, ``scope`` wen es betrifft."""
    text: str
    scope: str


def einvoice_gaps(inv: EInvoice) -> List[Gap]:
    """Die Angaben, die der E-Rechnung fehlen – in fester Reihenfolge, leer, wenn nichts fehlt.

    Fehlt eine Angabe vom Bereich ``xrechnung``, wäre das UBL ungültig (das Portal des Empfängers wiese es ab); fehlt eine
    vom Bereich ``en16931``, ist es auch die CII im PDF. Ein UBL entsteht nur ohne jede Lücke
    (``export.invoice_files.generate_invoice_documents``)."""
    gaps: List[Gap] = []

    def need(value, text, scope=SCOPE_XRECHNUNG):
        if not value:
            gaps.append(Gap(text, scope))

    seller, buyer = inv.seller, inv.buyer
    need(inv.buyer_reference, 'Leitweg-ID (Käuferreferenz)')                        # BR-DE-15
    need(inv.iban, 'Bankverbindung (IBAN)')                                         # BR-DE-1
    need(seller.postal_code, 'PLZ der eigenen Firma')                               # BR-DE-4
    need(seller.city, 'Ort der eigenen Firma')                                      # BR-DE-3
    need(seller.contact_name, 'Ansprechpartner der eigenen Firma')                  # BR-DE-2, BR-DE-5
    need(seller.phone, 'Telefon der eigenen Firma')                                 # BR-DE-6
    need(seller.email, 'E-Mail der eigenen Firma')                                  # BR-DE-7, PEPPOL-EN16931-R020 (BT-34)
    need(buyer.postal_code, 'PLZ des Kunden')                                       # BR-DE-9
    need(buyer.city, 'Ort des Kunden')                                              # BR-DE-8
    need(buyer.email, 'E-Mail des Kunden')                                          # PEPPOL-EN16931-R010 (BT-49)
    need('#' not in inv.payment_terms, HASH_IN_PAYMENT_TERMS_GAP)                   # BR-DE-18
    need(seller.vat_id or seller.tax_number,
         'Steuernummer oder USt-IdNr. der eigenen Firma', SCOPE_EN16931)            # BR-S-02, BR-Z-02, BR-E-02
    if inv.skonto_unreadable:
        gaps.append(Gap('Skonto (die gespeicherten Werte sind nicht lesbar)', SCOPE_EN16931))
    return gaps


def gap_texts(gaps: List[Gap], scope: Optional[str] = None) -> List[str]:
    """Die Texte der Lücken – mit ``scope`` nur die dieses Bereichs."""
    return [gap.text for gap in gaps if scope is None or gap.scope == scope]
