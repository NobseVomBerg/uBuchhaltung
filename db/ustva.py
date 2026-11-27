# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Umsatzsteuer-Voranmeldung (Stufe 1): die Kennzahlen 81, 86, 66 und 83 aus den Buchungen – ohne Datenbank, ohne Uhr.

Die Seite hat keine eigene Lesart der Buchungen. ``Database.get_ustva`` liest dieselben Sätze wie die Einnahmenüberschussrechnung
(``Database._euer_entries``: nach dem Datum der Bankbewegung bzw. der Kassenbuchung, auch die Vorsteuer zählt mit der Zahlung) und dazu
vier Ergänzungen, damit nichts still fehlt: Kinder einer Bankbewegung im Zeitraum, deren eigenes Datum außerhalb liegt; eigenständige Sätze
ohne Bankbewegung, die die EÜR nicht liest (nicht die Forderung einer Rechnung); Bankbewegungen ohne Satz; Bankbewegungen, deren Sätze nicht ihren
Betrag ergeben (die beiden Letzten nur gezählt). Hier steht nur, wohin ein Satz gehört (``assign``) und wie daraus der Bericht wird (``build_ustva``).

Jeder Satz geht in genau eine Kennzahl (81: Umsätze zu 19 %, 86: zu 7 %, 66: Vorsteuer zu 19 und 7 %), in genau einen Grund unter „Nicht zugeordnet“ oder in
„neutral“ (kein Umsatz, keine Steuer: Zahlung auf Debitoren, Spiegel, Privat, Aufwand ohne Steuer, Abschreibung …). Was sich nicht sicher
zuordnen lässt, bleibt in keiner Kennzahl und steht mit Anzahl, Netto und Steuer da. Die Wartekonten (4340, 4345, 4405) zählen nie: Die Umsatzsteuer
entsteht mit der Umbuchung auf das Erlöskonto bei der Zahlung (``reporting.NEUTRAL_ACCOUNT_NUMBERS``).

Gerechnet wird nur mit ganzen Zahlen (Minor Units, ``money``), nie mit ``float``. Die Steuer steht wie gebucht, auf den Cent; die Bemessungsgrundlage steht
genau und in vollen Euro ohne Cent. Die Richtung dieser Rundung ist fachlich nicht belegt (``docs/fachfragen.md``) und steht an EINER Stelle
(``BASE_ROUNDING``). Nicht abgebildet: Umsätze nach § 13b, innergemeinschaftliche Vorgänge, Einfuhrumsatzsteuer, Kennzahlen 39, 65 und 69 und alle
steuerfreien Umsätze – sie stehen, soweit buchbar, unter „Nicht zugeordnet“.
"""
import calendar
import datetime
import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from money import FACTOR, format_de
from . import datev_keys
from .bwa import format_amount, month_label
from .matching import FORM_PURPOSE_FIRST, FORM_TRANSFER, MIRROR_FORMS
from .reporting import NEUTRAL_ACCOUNT_NUMBERS
from .susa import TAX_ACCOUNTS

# ── Konstanten ────────────────────────────────────────────────────────────────

WAITING_NUMBERS = frozenset(number for number in NEUTRAL_ACCOUNT_NUMBERS if 4000 <= number <= 4999)     # 4340, 4345, 4405 – zählen nie
#: Die Steuerkonten der Summen- und Saldenliste (``susa.TAX_ACCOUNTS``: 1401, 1406, 3801, 3806) – nicht die Zahllast- und Finanzamtskonten (3820 ff.)
TAX_ACCOUNT_NUMBERS = frozenset(number for by_rate in TAX_ACCOUNTS.values() for number in by_rate.values())
PRIVATE_RANGE = (2100, 2199)
PL_RANGE = (4000, 7999)                       # wie matching.PL_ACCOUNT_RANGE
ACCOUNT_4185 = 4185
SMALL_BUSINESS_ACCOUNT = 4184
RATE_81, RATE_86 = 19.0, 7.0                  # die Prozentzahlen von datev_keys.percent
#: So weit darf die gebuchte Steuer bei 19 und 7 % von Netto × Satz abweichen (zwei Cent: die Steuer ist auf den Cent gerundet, auch bei Teilzahlungen); mehr ist
#: ``rate_tax_mismatch``. Minor Units, ganzzahlig.
TAX_TOLERANCE_MINOR = 2 * FACTOR // 100
KZ_81, KZ_86, KZ_66 = 'kz81', 'kz86', 'kz66'  # Schlüssel der Kennzahlen (auch im HTML)
NEUTRAL = 'neutral'
UNREADABLE = 'unreadable'
KIND_MONTH, KIND_QUARTER = 'monat', 'quartal'
USTVA_KINDS = (KIND_MONTH, KIND_QUARTER)
KIND_LABELS = {KIND_MONTH: 'Monat', KIND_QUARTER: 'Quartal'}
#: EINE Stelle für die Rundung der Bemessungsgrundlage auf volle Euro. Die Richtung ist nicht belegt (docs/fachfragen.md);
#: ROUND_DOWN lässt die Cent weg (zur Null hin, auch bei negativen Beträgen). Ändern nur hier, mit Test und Text USTVA_ROUNDING_TEXT.
BASE_ROUNDING = ROUND_DOWN

#: Die Quellen eines Satzes: ``euer`` (liest die EÜR), ``other_date`` (Kind außerhalb des Zeitraums, Bankbewegung im Zeitraum), ``standalone``
#: (eigenständig, ohne Bankbewegung, von der EÜR nicht gelesen)
SOURCE_EUER, SOURCE_OTHER_DATE, SOURCE_STANDALONE = 'euer', 'other_date', 'standalone'

#: Die Gründe für „Nicht zugeordnet“ in der Reihenfolge der Anzeige: Schlüssel → Bezeichnung
UNASSIGNED_REASONS = {
    'zero_rate': 'Erlöse ohne Steuer (0 %, steuerfrei, § 19 oder ohne Satz)',
    'other_rate': 'Erlöse zu einem anderen Steuersatz',
    'input_other_rate': 'Vorsteuer zu einem anderen Steuersatz',
    'account_4185': 'Erlöse auf Konto 4185',
    'tax_without_rate': 'Steuerbetrag ohne Steuersatz',
    'rate_tax_mismatch': 'Steuersatz und Steuerbetrag passen nicht zusammen',
    'private_tax': 'Vorsteuer auf Privatkonten',
    'other_date': 'Satz mit anderem Datum als seine Bankbewegung (anderer Zeitraum)',
    'private_transfer': 'Umbuchung Privat ↔ Erfolgskonto ohne Geldfluss',
    'no_bank': 'Satz ohne Bankbewegung, den die EÜR nicht zählt',
    'transfer_other': 'Umbuchung, die die Seite nicht zuordnet',
    'tax_other': 'Steuer auf einem Konto ohne Umsatz- oder Vorsteuer',
    'tax_account': 'Satz direkt auf einem Steuerkonto',
    'waiting': 'Satz auf einem Wartekonto (4340, 4345, 4405)',
    'no_account': 'Satz ohne Konto',
    UNREADABLE: 'Betrag nicht lesbar',
}
#: Bankbewegungen ohne Satz und Bankbewegungen, deren Sätze nicht ihren Betrag ergeben, sind kein Satz und stehen deshalb außerhalb der Invariante
UNBOOKED_LABEL = 'Bankbewegungen ohne Buchungssatz'
UNBALANCED_LABEL = 'Bankbewegungen, deren Buchungssätze nicht den Betrag ergeben'

# ── Texte (Seite und PDF) ─────────────────────────────────────────────────────

USTVA_TITLE = 'Umsatzsteuer-Voranmeldung'
USTVA_MENU_LABEL = 'USt-Voranmeldung'
USTVA_HINT_TEXT = ('Aus den Buchungen dieser App gerechnet, zum Übertragen in den Vordruck – die App übermittelt nichts. '
                   'Kein amtliches Formular; keine Steuerberatung, ersetzt nicht die Prüfung durch ein Steuerbüro.')
USTVA_DATE_TEXT = ('Die Seite rechnet nach dem Datum der Bankbewegung bzw. der Kassenbuchung. Auch die Vorsteuer zählt hier mit der Zahlung, '
                   'nicht mit dem Datum der Rechnung.')
USTVA_ROUNDING_TEXT = ('Bemessungsgrundlage genau und in vollen Euro ohne Cent; Steuer wie gebucht, auf den Cent. Rechnet das Formular die '
                       'Steuer selbst aus den vollen Euro, kann sie um Cent abweichen.')
USTVA_OPEN_PERIOD_TEXT = 'Der Zeitraum ist noch nicht abgelaufen.'
USTVA_UNASSIGNED_TEXT = 'Was die Seite keiner Kennzahl sicher zuordnet, steht unter „Nicht zugeordnet“ und zählt in keiner Kennzahl – bitte selbst prüfen.'
USTVA_NOT_SHOWN_TEXT = ('Nicht abgebildet: Umsätze nach § 13b, innergemeinschaftliche Lieferungen und Erwerbe, Einfuhrumsatzsteuer, die '
                        'Sondervorauszahlung (Kz 39) und die Kennzahlen 65 und 69. Steuerfreie Umsätze ordnet die Seite keiner Kennzahl zu.')
USTVA_SIGN_TEXT = 'Vorzeichen unter „Nicht zugeordnet“ wie gebucht.'
USTVA_RECON_TEXT = ('Die Steuer der Kennzahlen steht gegen Umsatzsteuer abzüglich Vorsteuer laut EÜR im selben Zeitraum. Eine Abweichung '
                    'entsteht durch die Posten unter „Nicht zugeordnet“ und durch Sätze mit anderem Datum als ihre Bankbewegung.')
USTVA_SMALL_BUSINESS_TEXT = 'Bei Kleinunternehmerregelung ist in der Regel keine Voranmeldung abzugeben.'
USTVA_SOLL_TEXT = ('Die eigene Firma ist auf Soll-Versteuerung gestellt. Diese Seite rechnet nur nach vereinnahmten Entgelten und zeigt '
                   'deshalb keine Zahlen.')
USTVA_UNSET_TEXT = ('Die Besteuerungsart ist nicht festgelegt. Zahlen zeigt die Seite nur bei Ist-Versteuerung – einzustellen in den '
                    'Stammdaten der eigenen Firma.')
USTVA_PDF_NOT_IST_TEXT = 'Die Umsatzsteuer-Voranmeldung gibt es nur bei Ist-Versteuerung.'
USTVA_KIND_LABEL = 'Anmeldezeitraum'
COLUMN_KZ, COLUMN_LABEL, COLUMN_BASE, COLUMN_WHOLE, COLUMN_TAX = 'Kz', 'Bezeichnung', 'Bemessungsgrundlage €', 'in vollen €', 'Steuer €'
LABEL_81 = 'Steuerpflichtige Umsätze 19 %'
LABEL_86 = 'Steuerpflichtige Umsätze 7 %'
LABEL_VAT_TOTAL = 'Umsatzsteuer (81 und 86)'
LABEL_66 = 'Vorsteuer aus Rechnungen anderer Unternehmer'
LABEL_83 = 'Verbleibende Vorauszahlung (Überschuss mit Minus)'
RECON_HEADING = 'Abstimmung'
RECON_COLUMNS = ('Steuer der Kennzahlen (81 + 86 − 66)', 'Umsatzsteuer abzüglich Vorsteuer laut EÜR', 'Abweichung')
RECON_UNKNOWN = '–'          # steht für einen Wert der Abstimmung, den die EÜR nicht als ganze Zahl liefert
UNASSIGNED_HEADING = 'Nicht zugeordnet'
UNASSIGNED_COLUMNS = ('Grund', 'Anzahl', 'Netto €', 'Steuer €')
UNASSIGNED_SUM_LABEL = 'Summe Nicht zugeordnet'
UNASSIGNED_NONE_TEXT = 'Keine Posten.'
USTVA_PERIOD_LINE = 'Zeitraum: {von} bis {bis} ({label})'
USTVA_FOOTER = ('Aus den Buchungen dieser App nach dem Datum der Zahlung gerechnet. Kein amtliches Formular; die App übermittelt nichts.',
                'Keine Steuerberatung; ersetzt nicht die Prüfung durch ein Steuerbüro.')

# ── Zeitraum, Bezeichnung, Dateiname ──────────────────────────────────────────

_ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_QUARTER_END_MONTHS = (3, 6, 9, 12)


def quarter_end_month(month: int) -> int:
    """Der letzte Monat des Kalenderquartals, in dem ``month`` (1 bis 12) liegt: 3, 6, 9 oder 12."""
    return (month - 1) // 3 * 3 + 3


def _iso_day(text):
    """Das Datum zu einem ISO-Text ``JJJJ-MM-TT`` (genau diese Form) – sonst ``None``."""
    if not isinstance(text, str) or not _ISO_DATE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def is_calendar_quarter(date_from: str, date_to: str) -> bool:
    """Genau ein Kalenderquartal: vom 1. Januar, 1. April, 1. Juli oder 1. Oktober bis zum letzten Tag des dritten Monats, im selben Jahr. Ein Wert,
    der kein ISO-Datum ``JJJJ-MM-TT`` ist, gilt als keines."""
    first, last = _iso_day(date_from), _iso_day(date_to)
    if first is None or last is None:
        return False
    return (first.year == last.year and first.day == 1 and first.month in (1, 4, 7, 10) and last.month == first.month + 2
            and last.day == calendar.monthrange(last.year, last.month)[1])


def ustva_period(year: int, month: int, kind: str) -> tuple[str, str]:
    """Der Zeitraum einer Voranmeldung als ISO-Datumspaar (von, bis): der Kalendermonat, bei ``quartal`` das Quartal, das mit ``month`` endet.
    ``ValueError`` bei einem Jahr außerhalb von 1 bis 9999, einem Monat außerhalb von 1 bis 12, einer unbekannten Art oder einem Quartal, dessen
    Monat nicht 3, 6, 9 oder 12 ist."""
    if not (isinstance(year, int) and isinstance(month, int) and 1 <= month <= 12 and 1 <= year <= 9999):
        raise ValueError(f'Ungültiger Zeitraum: Jahr {year!r}, Monat {month!r}')
    if kind == KIND_MONTH:
        first_month = month
    elif kind == KIND_QUARTER and month in _QUARTER_END_MONTHS:
        first_month = month - 2
    else:
        raise ValueError(f'Ungültiger Anmeldezeitraum: {kind!r} mit Monat {month!r}')
    return f'{year:04d}-{first_month:02d}-01', f'{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}'


def period_label(year: int, month: int, kind: str) -> str:
    """``März 2026`` bzw. ``1. Quartal 2026``. Gültigkeit wie bei ``ustva_period``."""
    ustva_period(year, month, kind)
    if kind == KIND_QUARTER:
        return f'{month // 3}. Quartal {year}'
    return month_label(year, month)


def ustva_filename(year: int, month: int, kind: str) -> str:
    """``USt-Voranmeldung_2026-03.pdf`` bzw. ``USt-Voranmeldung_2026-Q1.pdf``. Gültigkeit wie bei ``ustva_period``."""
    ustva_period(year, month, kind)
    suffix = f'Q{month // 3}' if kind == KIND_QUARTER else f'{month:02d}'
    return f'USt-Voranmeldung_{year:04d}-{suffix}.pdf'


# ── Rundung der Bemessungsgrundlage ───────────────────────────────────────────

def whole_euros(minor: int) -> int:
    """Der Betrag in vollen Euro (Minor Units mit 0 Cent), gerundet nach ``BASE_ROUNDING`` – exakt in ``Decimal``, nie über ``float``."""
    return int((Decimal(minor) / FACTOR).quantize(Decimal(1), rounding=BASE_ROUNDING) * FACTOR)


def format_whole(minor: int) -> str:
    """Der Betrag in vollen Euro, deutsch (``12.345``)."""
    return format_de(whole_euros(minor), 0)


# ── Die Zuordnung eines Satzes ────────────────────────────────────────────────

@dataclass(frozen=True)
class UstvaEntry:
    """Ein Satz, wie ``Database.get_ustva`` ihn aus der Datenbank liest. ``amount`` und ``tax`` sind Minor Units, ``None`` heißt nicht lesbar (der
    Steuerbetrag NULL ist 0). ``parent_date``: das Datum der Bankbewegung, nur bei einem Kind. ``asset_transfer``: Umbuchung Anlage ↔ Erfolgskonto
    (Sektion 3 der EÜR oder ``matching.is_asset_transfer``)."""
    booking_id: int
    source: str
    form: str
    coa_number: int | None
    counter_number: int | None
    amount: int | None
    tax: int | None
    rate: object
    entry_date: str | None
    parent_date: str | None
    asset_transfer: bool


@dataclass(frozen=True)
class Assignment:
    """Wohin ein Satz gehört: ``target`` ist ``KZ_81``, ``KZ_86``, ``KZ_66``, ein Schlüssel von ``UNASSIGNED_REASONS`` (auch ``UNREADABLE``) oder
    ``NEUTRAL``; ``account`` das Konto im Kontendetail (``None`` = keines). ``base`` und ``tax`` in Minor Units: bei 81 und 86 netto und Steuer wie
    gebucht, bei 66 das Negative davon (eine Ausgabe und ihre Vorsteuer sind positiv), sonst wie gebucht; ``UNREADABLE``: 0."""
    target: str
    account: int | None
    base: int
    tax: int


def _within(number, bounds) -> bool:
    return number is not None and bounds[0] <= number <= bounds[1]


def _is_sales(number) -> bool:
    """Ein Erlöskonto (4000–4999 und 6884–6892), das kein Wartekonto ist."""
    return number is not None and datev_keys.is_revenue_number(number) and number not in WAITING_NUMBERS


def _is_chain_side(number) -> bool:
    """Eine Seite der Rechnungskette: ein Wartekonto oder ein Debitor."""
    return number in WAITING_NUMBERS or datev_keys.is_debtor_number(number)


def _is_private_transfer(entry: UstvaEntry) -> bool:
    """Eine Umbuchung zwischen einem Privatkonto und einem Erfolgskonto (4000–7999)."""
    if entry.form != FORM_TRANSFER:
        return False
    coa, counter = entry.coa_number, entry.counter_number
    return ((_within(coa, PRIVATE_RANGE) and _within(counter, PL_RANGE)) or (_within(counter, PRIVATE_RANGE) and _within(coa, PL_RANGE)))


def _tax_fits(amount: int, tax: int, pct: float) -> bool:
    """Passt die gebuchte Steuer zu Betrag und Satz? Betrag und Steuer sind ≠ 0 und haben dasselbe Vorzeichen, und die Steuer weicht von Netto × Satz
    (Netto = Betrag − Steuer) höchstens um ``TAX_TOLERANCE_MINOR`` ab – gerechnet in ``Decimal``, nie mit ``float``."""
    if amount == 0 or tax == 0 or (amount > 0) != (tax > 0):
        return False
    expected = Decimal(amount - tax) * Decimal(str(pct)) / 100
    return abs(Decimal(tax) - expected) <= TAX_TOLERANCE_MINOR


def _revenue_target(number: int, pct: float, amount: int, tax: int) -> str:
    """Erlösregel R: wohin ein Satz auf dem Erlöskonto ``number`` gehört."""
    if number == ACCOUNT_4185:
        return 'account_4185'
    if pct == 0:
        return 'tax_without_rate' if tax != 0 else 'zero_rate'
    if pct not in (RATE_81, RATE_86):
        return 'other_rate'
    if not _tax_fits(amount, tax, pct):
        return 'rate_tax_mismatch'
    return KZ_81 if pct == RATE_81 else KZ_86


def _classify(entry: UstvaEntry) -> tuple[str, int | None]:
    """Schritt 2 der Zuordnung (``(Ziel, Konto)``) für einen lesbaren Satz mit Betrag oder Steuer ≠ 0: das Zweckkonto wie in ``get_euer_data`` (bei
    ``purpose_first`` das Konto, sonst das Gegenkonto; bei einer Umbuchung das Gegenkonto), danach die Regeln T1–T11 (Umbuchung, Spiegel), Z1–Z15
    (Zahlung) und R1–R7 (Erlöse) aus ``docs/regeln/auswertungen.md``."""
    coa, counter = entry.coa_number, entry.counter_number
    amount, tax = entry.amount, entry.tax
    pct = datev_keys.percent(entry.rate)

    if entry.form in MIRROR_FORMS:
        return (NEUTRAL, None) if tax == 0 else ('tax_other', counter)                                  # T1, T2
    if entry.form == FORM_TRANSFER:
        if _is_chain_side(coa) or _is_chain_side(counter):
            if _is_sales(counter):
                return _revenue_target(counter, pct, amount, tax), counter                              # T3 („4405 an 4400“)
            if _is_sales(coa):
                return 'transfer_other', coa                                                            # T4
            return (NEUTRAL, None) if tax == 0 else ('transfer_other', counter)                         # T5, T6
        if entry.asset_transfer:
            return (NEUTRAL, None) if tax == 0 else ('tax_other', counter)                              # T7, T8
        if _is_private_transfer(entry):
            return 'private_transfer', counter if _within(coa, PRIVATE_RANGE) else coa                  # T9 (die Erfolgsseite)
        if tax != 0 or _is_sales(coa) or _is_sales(counter):
            return 'transfer_other', counter if counter is not None else coa                            # T10
        return NEUTRAL, None                                                                            # T11

    purpose = coa if entry.form == FORM_PURPOSE_FIRST else counter
    if purpose is None:
        return 'no_account', None                                                                       # Z1
    if purpose in WAITING_NUMBERS:
        return 'waiting', purpose                                                                       # Z2
    if datev_keys.is_debtor_number(purpose):
        return (NEUTRAL, None) if tax == 0 else ('tax_other', purpose)                                  # Z3, Z4
    if _is_sales(purpose):
        return _revenue_target(purpose, pct, amount, tax), purpose                                      # Z5
    if _within(purpose, PRIVATE_RANGE):
        return (NEUTRAL, None) if tax == 0 else ('private_tax', purpose)                                # Z6, Z7
    if datev_keys.is_expense_or_asset_number(purpose):
        if tax == 0:
            return NEUTRAL, None                                                                        # Z8
        if pct == 0:
            return 'tax_without_rate', purpose                                                          # Z9
        if pct not in (RATE_81, RATE_86):
            return 'input_other_rate', purpose                                                          # Z10
        if not _tax_fits(amount, tax, pct):
            return 'rate_tax_mismatch', purpose                                                         # Z11
        return KZ_66, purpose                                                                           # Z12
    if purpose in TAX_ACCOUNT_NUMBERS and amount != 0:
        return 'tax_account', purpose                                                                   # Z13
    return (NEUTRAL, None) if tax == 0 else ('tax_other', purpose)                                      # Z14, Z15


def assign(entry: UstvaEntry, date_from: str, date_to: str) -> Assignment:
    """Wohin ein Satz im Zeitraum ``date_from`` bis ``date_to`` gehört: in genau eine Kennzahl, einen Grund unter „Nicht zugeordnet“ oder nach
    ``NEUTRAL`` (nie mit Steuer). Schritt 1: ein Betrag oder Steuerbetrag, der nicht lesbar ist, gibt ``UNREADABLE`` (Basis und Steuer 0); Betrag und
    Steuer 0 sind ``NEUTRAL``. Schritt 2: ``_classify``. Schritt 3 (nur bei einer Kennzahl oder einem Grund, ``NEUTRAL`` bleibt): ein eigenständiger Satz
    ist ``private_transfer`` (Umbuchung Privat ↔ Erfolgskonto) oder ``no_bank``; ein Satz, dessen eigenes Datum oder dessen Bankbewegung außerhalb
    des Zeitraums liegt, ist ``other_date`` – liegen beide Daten im Zeitraum, zählt der Satz normal."""
    if entry.amount is None or entry.tax is None:
        return Assignment(UNREADABLE, None, 0, 0)
    net = entry.amount - entry.tax
    if entry.amount == 0 and entry.tax == 0:
        return Assignment(NEUTRAL, None, net, entry.tax)
    target, account = _classify(entry)
    if target != NEUTRAL:
        if entry.source == SOURCE_STANDALONE:
            if _is_private_transfer(entry):
                target = 'private_transfer'
                account = entry.counter_number if _within(entry.coa_number, PRIVATE_RANGE) else entry.coa_number
            else:
                target = 'no_bank'
        elif entry.source == SOURCE_OTHER_DATE or (entry.parent_date is not None
                                                    and not date_from <= entry.parent_date <= date_to):
            target = 'other_date'
    if target == KZ_66:
        return Assignment(target, account, -net, -entry.tax)
    return Assignment(target, account, net, entry.tax)


# ── Der Bericht ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AccountLine:
    """Ein Konto im Kontendetail einer Kennzahl oder eines Grundes; ``number`` ``None`` = kein Konto."""
    number: int | None
    name: str
    count: int
    base: int
    tax: int


@dataclass(frozen=True)
class Figure:
    """Eine Kennzahl: ``base`` genau, ``base_whole`` in vollen Euro (aus der Summe gerechnet, nie je Konto; bei 66 unbenutzt: 0), ``tax`` wie gebucht."""
    key: str
    kz: str
    label: str
    base: int
    base_whole: int
    tax: int
    accounts: tuple[AccountLine, ...]


@dataclass(frozen=True)
class UnassignedGroup:
    """Ein Grund unter „Nicht zugeordnet“ mit Anzahl, Netto und Steuer (Vorzeichen wie gebucht: Einnahmen positiv, Ausgaben negativ)."""
    key: str
    label: str
    count: int
    net: int
    tax: int
    accounts: tuple[AccountLine, ...]


@dataclass(frozen=True)
class UstvaReport:
    """Die Voranmeldung eines Zeitraums. ``figures``: 81 und 86 (immer da, auch bei 0); ``input_tax``: 66 (positiv: Vorsteuer); ``prepayment``: Kz 83.
    ``unassigned``: nur die Gründe mit Sätzen, in der Reihenfolge von ``UNASSIGNED_REASONS`` – auch ``unreadable``. ``unbooked_*``: Bankbewegungen ohne
    Satz, ``unbalanced_*``: Bankbewegungen, deren Sätze nicht ihren Betrag ergeben (Zahl und Summe der Reste ohne Vorzeichen) – beide kein Satz und nicht in
    der Invariante. ``recon_*``: die Steuer der Kennzahlen (81 + 86 − 66) gegen Umsatzsteuer plus Vorsteuer laut EÜR; ``None`` bei ``recon_euer`` und
    ``recon_difference``, wenn die EÜR die Summe nicht als ganze Zahl liefert (eine Kommazahl im Bestand – nichts wird abgeschnitten).
    ``read_*`` sind alle gelesenen Sätze (``read_net`` und ``read_tax`` nur die lesbaren, wie gebucht); jeder Satz steht in genau einer Kennzahl, einem
    Grund (mit ``unreadable``) oder bei ``neutral_*`` – daraus die Invarianten in ``tests/test_ustva.py``."""
    year: int
    month: int
    kind: str
    date_from: str
    date_to: str
    figures: tuple[Figure, ...]
    vat_total: int
    input_tax: Figure
    prepayment: int
    unassigned: tuple[UnassignedGroup, ...]
    unassigned_net: int
    unassigned_tax: int
    unbooked_count: int
    unbooked_amount: int
    unbalanced_count: int
    unbalanced_amount: int
    recon_page: int
    recon_euer: int | None
    recon_difference: int | None
    read_count: int
    read_net: int
    read_tax: int
    neutral_count: int
    neutral_net: int
    neutral_tax: int
    unreadable_count: int
    has_small_business: bool


class _Sum:
    """Anzahl, Basis und Steuer eines Ziels, dazu je Konto."""
    __slots__ = ('count', 'base', 'tax', 'accounts')

    def __init__(self):
        self.count = self.base = self.tax = 0
        self.accounts: dict = {}

    def add(self, account, base, tax):
        self.count += 1
        self.base += base
        self.tax += tax
        line = self.accounts.setdefault(account, [0, 0, 0])
        line[0] += 1
        line[1] += base
        line[2] += tax

    def lines(self, names: dict) -> tuple[AccountLine, ...]:
        """Die Konten nach Nummer, ein Eintrag ohne Konto zuletzt."""
        return tuple(AccountLine(number, names.get(number, ''), *line)
                     for number, line in sorted(self.accounts.items(), key=lambda item: (item[0] is None, item[0] or 0)))


def account_label(line: AccountLine) -> str:
    """``4400 Erlöse`` – ohne Namen nur die Nummer, ohne Nummer ein Gedankenstrich."""
    if line.number is None:
        return '–'
    return f'{line.number} {line.name}' if line.name else str(line.number)


def format_recon(minor) -> str:
    """Ein Betrag der Abstimmung auf den Cent, deutsch; ``None`` (die EÜR liefert dort keine ganze Zahl) steht als ``RECON_UNKNOWN`` da."""
    return RECON_UNKNOWN if minor is None else format_amount(minor)


def build_ustva(year: int, month: int, kind: str, entries, names: dict, euer_vat_minor, euer_input_minor, unbooked: tuple[int, int],
                unbalanced: tuple[int, int] = (0, 0)) -> UstvaReport:
    """Der Bericht zu den Sätzen ``entries`` (``UstvaEntry``) des Zeitraums von ``ustva_period`` (``ValueError`` wie dort). ``names``: Name je
    Kontonummer. ``euer_vat_minor`` und ``euer_input_minor``: Umsatzsteuer und Vorsteuer laut EÜR im selben Zeitraum (``EuerTotals``, Minor Units,
    Vorzeichen wie dort) für die Abstimmung; ist einer keine ganze Zahl (eine Kommazahl im Bestand), bleiben ``recon_euer`` und ``recon_difference``
    ``None`` – sie werden nie abgeschnitten. ``unbooked``: ``(Summe, Zahl)`` der Bankbewegungen ohne Satz (``Database._liquidity_unbooked``),
    ``unbalanced``: ``(Summe der Reste, Zahl)`` der Bankbewegungen, deren Sätze nicht ihren Betrag ergeben (``Database._susa_unbalanced``). Gerechnet wird
    nur mit ganzen Zahlen."""
    date_from, date_to = ustva_period(year, month, kind)
    sums: dict[str, _Sum] = {}
    read_count = read_net = read_tax = 0
    for entry in entries:
        assignment = assign(entry, date_from, date_to)
        read_count += 1
        if entry.amount is not None and entry.tax is not None:
            read_net += entry.amount - entry.tax
            read_tax += entry.tax
        sums.setdefault(assignment.target, _Sum()).add(assignment.account, assignment.base, assignment.tax)

    def figure(key: str, kz: str, label: str, whole: bool) -> Figure:
        found = sums.get(key, _Sum())
        return Figure(key, kz, label, found.base, whole_euros(found.base) if whole else 0, found.tax, found.lines(names))

    figure_81 = figure(KZ_81, '81', LABEL_81, True)
    figure_86 = figure(KZ_86, '86', LABEL_86, True)
    input_tax = figure(KZ_66, '66', LABEL_66, False)
    groups = tuple(UnassignedGroup(key, label, sums[key].count, sums[key].base, sums[key].tax, sums[key].lines(names))
                   for key, label in UNASSIGNED_REASONS.items() if key in sums)
    neutral = sums.get(NEUTRAL, _Sum())
    vat_total = figure_81.tax + figure_86.tax
    recon_page = vat_total - input_tax.tax
    whole = all(isinstance(value, int) and not isinstance(value, bool) for value in (euer_vat_minor, euer_input_minor))
    recon_euer = euer_vat_minor + euer_input_minor if whole else None
    unbooked_amount, unbooked_count = unbooked
    unbalanced_amount, unbalanced_count = unbalanced
    return UstvaReport(
        year=year, month=month, kind=kind, date_from=date_from, date_to=date_to,
        figures=(figure_81, figure_86), vat_total=vat_total, input_tax=input_tax, prepayment=vat_total - input_tax.tax,
        unassigned=groups, unassigned_net=sum(group.net for group in groups), unassigned_tax=sum(group.tax for group in groups),
        unbooked_count=unbooked_count, unbooked_amount=unbooked_amount, unbalanced_count=unbalanced_count, unbalanced_amount=unbalanced_amount,
        recon_page=recon_page, recon_euer=recon_euer, recon_difference=recon_page - recon_euer if whole else None,
        read_count=read_count, read_net=read_net, read_tax=read_tax,
        neutral_count=neutral.count, neutral_net=neutral.base, neutral_tax=neutral.tax,
        unreadable_count=sums[UNREADABLE].count if UNREADABLE in sums else 0,
        has_small_business=any(line.number == SMALL_BUSINESS_ACCOUNT for group in groups if group.key == 'zero_rate' for line in group.accounts))
