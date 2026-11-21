# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Betriebswirtschaftliche Auswertung (BWA): eine reine Umgruppierung der EÜR – ohne Datenbank, ohne Uhr.

Die Auswertung liest keine Buchungen. Sie nimmt die Zeilen von ``Database.get_euer_data`` (Saldo je SKR-Konto und Zeitraum,
netto, nach Zahlungsdatum) und ordnet sie in Gruppen. Ob ein Konto Einnahme, Ausgabe oder „nicht im Ergebnis“ ist,
entscheidet allein ``reporting.euer_section`` – dieselbe Funktion wie auf der EÜR-Seite des Dashboards. Deshalb stimmt das
Ergebnis auf den Minor-Wert mit dem EÜR-Ergebnis überein; die Summen der Einnahmen und Ausgaben weichen nur um die
Umsatzsteuer-Erstattungen ab (siehe ``vat_refund``).

Gerechnet wird in Minor Units (``int``, ``money``). Prozente entstehen erst bei der Ausgabe (``percent``, ``change_percent``).

Anzeige-Vorzeichen: Einnahmenzeilen wie in der EÜR (positiv = Einnahme), Ausgabenzeilen negiert (positiv = Ausgabe). Eine
Erstattung steht also negativ in ihrer Kostengruppe.

Die Gruppen stehen als Daten in ``RANGES`` (Kontonummernbereiche) und ``EXCEPTIONS`` (einzelne Konten). Ein Konto ohne
Gruppe steht unter „Nicht zugeordnet“ und zählt im Ergebnis wie in der EÜR – es wird nie still in eine andere Gruppe
gelegt. Gruppen, die der Nutzer pflegt, gibt es nicht (das wäre eine Schemaänderung).
"""
import bisect
import calendar
import datetime
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from money import format_de, to_minor
from .reporting import euer_section

# ── Gruppen ───────────────────────────────────────────────────────────────────

INCOME, EXPENSE, SIGN = 'income', 'expense', 'sign'     # Seite einer Gruppe; ``sign``: je Konto und Zeitraum nach dem Vorzeichen


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    side: str


GROUPS: dict[str, Group] = {group.key: group for group in (
    Group('revenue', 'Umsatzerlöse', INCOME),
    Group('other_revenue', 'Sonstige Erlöse', INCOME),
    Group('vat_collected', 'Vereinnahmte Umsatzsteuer', INCOME),
    # keine eigene Zeile: Zahlungen ans Finanzamt und Erstattungen von dort, je Konto und Zeitraum nach dem Vorzeichen
    Group('vat_settlement', 'Umsatzsteuer-Zahlungen und -Erstattungen', SIGN),
    Group('material', 'Material und Waren', EXPENSE),
    Group('services', 'Fremdleistungen', EXPENSE),
    Group('personnel', 'Personal', EXPENSE),
    Group('premises', 'Raumkosten', EXPENSE),
    Group('taxes_insurance', 'Steuern, Versicherungen, Beiträge', EXPENSE),
    Group('vehicles', 'Fahrzeugkosten', EXPENSE),
    Group('marketing_travel', 'Werbung und Reisen', EXPENSE),
    Group('distribution', 'Kosten der Warenabgabe', EXPENSE),
    Group('maintenance_tools', 'Instandhaltung und Werkzeuge', EXPENSE),
    Group('depreciation', 'Abschreibungen', EXPENSE),
    Group('misc_costs', 'Verschiedene Kosten', EXPENSE),
    Group('interest_other', 'Zinsen und sonstige Aufwendungen', EXPENSE),
    Group('liabilities', 'Tilgungen und Verbindlichkeiten', EXPENSE),
    Group('input_vat', 'Gezahlte Vorsteuer', EXPENSE),
)}

# Kontonummernbereiche (einschließlich), aufsteigend und ohne Überschneidung. Was in keinem Bereich liegt, hat keine Gruppe –
# absichtlich: 1000–1399, 1440–2999 (die Privatkonten 2100–2199 kennt die EÜR nie), 3850–3999 und ab 8000. Auch die Geldkonten
# (1460, 1600–1699, 1800–1899) gehören nicht dazu: Als Zweckkonto wären sie ein Buchungsfehler, der sichtbar bleiben soll.
RANGES: tuple[tuple[int, int, str], ...] = (
    (1400, 1439, 'input_vat'),
    (3000, 3799, 'liabilities'),
    (3800, 3849, 'vat_settlement'),
    (4000, 4599, 'revenue'),
    (4600, 4699, 'other_revenue'),
    (4700, 4799, 'revenue'),
    (4800, 4999, 'other_revenue'),
    (5000, 5899, 'material'),
    (5900, 5999, 'services'),
    (6000, 6199, 'personnel'),
    (6200, 6299, 'depreciation'),
    (6300, 6399, 'premises'),
    (6400, 6449, 'taxes_insurance'),
    (6450, 6499, 'maintenance_tools'),
    (6500, 6599, 'vehicles'),
    (6600, 6699, 'marketing_travel'),
    (6700, 6799, 'distribution'),
    (6800, 6879, 'misc_costs'),
    (6880, 7599, 'interest_other'),
    (7600, 7699, 'taxes_insurance'),
    (7700, 7999, 'interest_other'),
)

# Einzelne Konten, die vor den Bereichen gelten
EXCEPTIONS: dict[int, str] = {
    3806: 'vat_collected',        # virtuelle Zeile der EÜR für die Umsatzsteuer aus Einnahmen
    1940: 'interest_other',       # Damnum/Disagio
    6300: 'misc_costs',           # sonstige betriebliche Aufwendungen, nicht Raumkosten
    6845: 'maintenance_tools',    # Werkzeuge
    7685: 'vehicles',             # Kfz-Steuer
}

_RANGE_STARTS = [low for low, _high, _key in RANGES]
_MAX_DIGITS = 9                   # eine längere Ziffernfolge liegt in keinem Bereich (und int() hat eine Längengrenze)


def group_of(account_number) -> str | None:
    """Der Schlüssel der Gruppe einer Kontonummer oder ``None``. Gelesen werden eine Ganzzahl (kein ``bool``) und ein Text
    aus ASCII-Ziffern; alles andere – Kommazahl, Leerraum, Ziffern anderer Schriften – hat keine Gruppe. Zuerst
    ``EXCEPTIONS``, dann ``RANGES``."""
    if isinstance(account_number, bool):
        return None
    if isinstance(account_number, int):
        number = account_number
    elif (isinstance(account_number, str) and account_number.isascii() and account_number.isdigit()
          and len(account_number) <= _MAX_DIGITS):
        number = int(account_number)
    else:
        return None
    if number in EXCEPTIONS:
        return EXCEPTIONS[number]
    index = bisect.bisect_right(_RANGE_STARTS, number) - 1
    if index >= 0:
        low, high, key = RANGES[index]
        if low <= number <= high:
            return key
    return None


# ── Texte ─────────────────────────────────────────────────────────────────────

HINT_TEXT = ('Aus den Buchungen dieser App nach Zahlungsdatum gerechnet, wie die Einnahmenüberschussrechnung. '
             'Keine Steuerberatung; ersetzt nicht die Auswertung eines Steuerbüros.')
SIGN_TEXT = 'Kosten und Betriebsausgaben stehen positiv da; Erstattungen mindern sie.'
PERCENT_TEXT = ('Ein Anteil bleibt leer, wenn seine Bezugsgröße null oder negativ ist; die Veränderung in % bleibt leer, '
                'wenn der Vorjahreswert null ist.')
UNASSIGNED_TEXT = 'Konten ohne Gruppe stehen unter „Nicht zugeordnet“ und zählen im Ergebnis wie in der EÜR.'
INVALID_PERIOD_TEXT = 'Der gewählte Zeitraum ist ungültig – angezeigt wird der Vormonat.'
TITLE = 'Betriebswirtschaftliche Auswertung'
OUTSIDE_HEADING = 'Nicht im Ergebnis'

MONTH_NAMES = ('Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
               'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember')

# ── Darstellung und Zeiträume ─────────────────────────────────────────────────

VARIANT_YEAR = 'jahr'               # Monat und aufgelaufen mit Anteilen
VARIANT_PREVIOUS = 'vorjahr'        # Monat und aufgelaufen im Vergleich mit dem Vorjahr
VARIANTS = (VARIANT_PREVIOUS, VARIANT_YEAR)
DEFAULT_VARIANT = VARIANT_PREVIOUS
VARIANT_LABELS = {VARIANT_PREVIOUS: 'Vergleich mit dem Vorjahr', VARIANT_YEAR: 'Monat und Jahr mit Anteilen'}

PERIOD_KEYS = ('month', 'ytd', 'prev_month', 'prev_ytd')    # Monat, aufgelaufen ab 1. Januar, dieselben im Vorjahr
PREVIOUS_OF = {'month': 'prev_month', 'ytd': 'prev_ytd'}


def periods_for(variant) -> tuple[str, ...]:
    """Die Zeiträume, die eine Darstellung zeigt (und liest)."""
    if variant == VARIANT_YEAR:
        return PERIOD_KEYS[:2]
    if variant == VARIANT_PREVIOUS:
        return PERIOD_KEYS
    raise ValueError(f'Unbekannte Darstellung: {variant!r}')


def bwa_periods(year: int, month: int) -> dict[str, tuple[str, str]]:
    """Die vier Zeiträume als ISO-Datumspaare (von, bis): der Monat, aufgelaufen vom 1. Januar bis Monatsende und beide im Vorjahr.
    Der letzte Tag kommt aus dem Kalender (auch der 29. Februar; 1900 und 2100 sind keine Schaltjahre)."""
    def span(y, first_month, last_month):
        last_day = calendar.monthrange(y, last_month)[1]
        return f'{y:04d}-{first_month:02d}-01', f'{y:04d}-{last_month:02d}-{last_day:02d}'
    return {'month': span(year, month, month), 'ytd': span(year, 1, month),
            'prev_month': span(year - 1, month, month), 'prev_ytd': span(year - 1, 1, month)}


def default_month(today: datetime.date) -> tuple[int, int]:
    """(Jahr, Monat), den die Seite ohne Angabe zeigt: der Vormonat – im Januar der Dezember des Vorjahres."""
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def month_label(year: int, month: int) -> str:
    return f'{MONTH_NAMES[month - 1]} {year}'


# ── Der Bericht ───────────────────────────────────────────────────────────────

@dataclass
class BwaAccount:
    """Ein Konto in einer Zeile; ``values`` nennt nur die Zeiträume, in denen es in DIESER Zeile steht (Minor Units, Anzeige-Vorzeichen)."""
    number: int
    name: str
    values: dict[str, int] = field(default_factory=dict)


@dataclass
class BwaLine:
    """Eine Zeile: ``kind`` ist ``group`` (Konten einer Gruppe), ``sum`` oder ``result``; ``values`` hat für jeden angezeigten
    Zeitraum einen Wert (Standard 0). Die Konten stehen nach Nummer sortiert."""
    key: str
    label: str
    kind: str
    in_costs: bool
    values: dict[str, int]
    accounts: list[BwaAccount] = field(default_factory=list)


@dataclass
class BwaReport:
    year: int
    month: int
    variant: str
    periods: dict[str, tuple[str, str]]
    lines: list[BwaLine]
    outside: BwaLine

    def __post_init__(self):
        self._by_key = {line.key: line for line in [*self.lines, self.outside]}

    def line(self, key: str) -> BwaLine:
        return self._by_key[key]


@dataclass(frozen=True)
class LineSpec:
    """Eine Zeile des Aufbaus; bei ``sum`` und ``result`` sind ``plus`` und ``minus`` die Schlüssel der Zeilen, aus denen sie entsteht."""
    key: str
    kind: str
    label: str
    in_costs: bool = False
    plus: tuple[str, ...] = ()
    minus: tuple[str, ...] = ()


# Die Kostengruppen, in der Reihenfolge der Gruppentabelle; ihre Summe steht in „Summe Kosten“
COST_KEYS = ('material', 'services', 'personnel', 'premises', 'taxes_insurance', 'vehicles', 'marketing_travel',
             'distribution', 'maintenance_tools', 'depreciation', 'misc_costs')


def _group(key: str, in_costs: bool = False) -> LineSpec:
    return LineSpec(key, 'group', GROUPS[key].label, in_costs)


LAYOUT: tuple[LineSpec, ...] = (
    _group('revenue'),
    _group('other_revenue'),
    LineSpec('total_revenue', 'sum', 'Summe Erlöse', plus=('revenue', 'other_revenue')),
    _group('vat_collected'),
    LineSpec('vat_refund', 'group', 'Umsatzsteuer-Erstattungen'),       # Konten von ``vat_settlement`` mit Saldo > 0
    LineSpec('unassigned_income', 'group', 'Nicht zugeordnet (Einnahmen)'),
    LineSpec('operating_income', 'sum', 'Betriebseinnahmen',
             plus=('total_revenue', 'vat_collected', 'vat_refund', 'unassigned_income')),
    *(_group(key, in_costs=True) for key in COST_KEYS),
    LineSpec('total_costs', 'sum', 'Summe Kosten', in_costs=True, plus=COST_KEYS),
    _group('interest_other'),
    _group('liabilities'),
    LineSpec('unassigned_expense', 'group', 'Nicht zugeordnet (Ausgaben)'),
    _group('input_vat'),
    LineSpec('vat_payment', 'group', 'Umsatzsteuer-Zahlungen'),         # Konten von ``vat_settlement`` mit Saldo ≤ 0, negiert
    LineSpec('operating_expenses', 'sum', 'Betriebsausgaben',
             plus=('total_costs', 'interest_other', 'liabilities', 'unassigned_expense', 'input_vat', 'vat_payment')),
    LineSpec('result', 'result', 'Ergebnis', plus=('operating_income',), minus=('operating_expenses',)),
)
OUTSIDE = LineSpec('outside', 'group', 'Sonstige Ausgaben')              # nicht im Ergebnis, wie in der EÜR; eigener Block darunter

# Zeilen, die nur stehen, wenn sie etwas zeigen (ein Wert ≠ 0 in einem angezeigten Zeitraum oder Konten)
OPTIONAL_KEYS = frozenset({'unassigned_income', 'unassigned_expense', 'outside'})


def build_report(year: int, month: int, variant: str, periods: dict, euer_rows: dict, bank_numbers) -> BwaReport:
    """Den Bericht aus den Zeilen von ``get_euer_data`` bauen: ``euer_rows`` je Zeitraum (``periods_for(variant)``) die Liste
    ``(Kontonummer, Name, Saldo in Euro)``, ``bank_numbers`` die SKR-Nummern der Geldkonten (``reporting.bank_skr_numbers``).

    Je Konto und Zeitraum: Zwei Zeilen mit derselben Nummer werden addiert und stehen im Kontennachweis als EINE Zeile unter dem
    zuerst gefundenen Namen – die echte und die virtuelle 3806, aber auch zwei Konten gleicher Nummer aus verschiedenen
    Kontenrahmen (die EÜR führt sie getrennt; Summen und Ergebnis sind dieselben). Die Sektion
    (``euer_section``) entscheidet, ob die Zeile zählt. ``other`` → Block „nicht im Ergebnis“; ``income`` → seine Einnahmengruppe,
    sonst „Nicht zugeordnet (Einnahmen)“; ``expense`` → seine Ausgabengruppe (Wert negiert), sonst „Nicht zugeordnet
    (Ausgaben)“. Ein Konto von ``vat_settlement`` steht je Zeitraum für sich unter Erstattungen (Saldo > 0, wie in der EÜR) oder
    Zahlungen (Saldo ≤ 0, negiert).

    Invarianten, je Zeitraum (Test): Ergebnis = Summe der Einnahmen und Ausgaben der EÜR; Betriebseinnahmen − Erstattungen =
    Summe der Einnahmen; Betriebsausgaben − Erstattungen = −Summe der Ausgaben; „nicht im Ergebnis“ = −Summe von ``other``."""
    shown = periods_for(variant)
    sections: dict[int, str | None] = {}
    names: dict[int, str] = {}
    totals: dict[str, dict[int, int]] = {period: {} for period in shown}
    for period in shown:                                         # Reihenfolge: Monat, aufgelaufen, Vorjahre – der erste Name zählt
        for number, name, total in euer_rows.get(period, ()):
            if number not in sections:
                sections[number] = euer_section(number, bank_numbers)
            if sections[number] is None:
                continue
            names.setdefault(number, name)
            totals[period][number] = totals[period].get(number, 0) + to_minor(total)

    lines = {spec.key: BwaLine(spec.key, spec.label, spec.kind, spec.in_costs, dict.fromkeys(shown, 0)) for spec in LAYOUT}
    lines[OUTSIDE.key] = BwaLine(OUTSIDE.key, OUTSIDE.label, OUTSIDE.kind, False, dict.fromkeys(shown, 0))
    accounts: dict[str, dict[int, BwaAccount]] = {key: {} for key in lines}

    def place(key: str, number: int, period: str, amount: int):
        lines[key].values[period] += amount
        account = accounts[key].setdefault(number, BwaAccount(number, names[number]))
        account.values[period] = amount

    for period in shown:
        for number, minor in totals[period].items():
            section, group = sections[number], group_of(number)
            side = GROUPS[group].side if group else None
            if section == 'other':
                place(OUTSIDE.key, number, period, -minor)
            elif section == 'income':
                place(group if side == INCOME else 'unassigned_income', number, period, minor)
            elif side == SIGN:
                if minor > 0:
                    place('vat_refund', number, period, minor)
                else:
                    place('vat_payment', number, period, -minor)
            else:
                place(group if side == EXPENSE else 'unassigned_expense', number, period, -minor)

    for spec in LAYOUT:                                          # die Summen stehen hinter ihren Bestandteilen
        if spec.kind != 'group':
            for period in shown:
                lines[spec.key].values[period] = (sum(lines[key].values[period] for key in spec.plus)
                                                  - sum(lines[key].values[period] for key in spec.minus))
    for key, line in lines.items():
        line.accounts = sorted(accounts[key].values(), key=lambda account: account.number)
    return BwaReport(year, month, variant, dict(periods), [lines[spec.key] for spec in LAYOUT], lines[OUTSIDE.key])


# ── Spalten, Anteile und Format (gemeinsam für Seite und PDF) ─────────────────

@dataclass(frozen=True)
class Column:
    """Eine Spalte: ``kind`` ist ``amount``, ``pct_revenue``, ``pct_costs``, ``pct_income``, ``delta`` (Veränderung zum Vorjahr
    in Euro) oder ``delta_pct``; ``period`` der Zeitraum, bei ``delta`` und ``delta_pct`` der laufende (``PREVIOUS_OF``: der Vorjahreswert)."""
    period: str
    kind: str


# Bezugsgröße je Anteil: die Zeile, durch die geteilt wird (im selben Zeitraum, im Anzeige-Vorzeichen)
PERCENT_BASE = {'pct_revenue': 'total_revenue', 'pct_costs': 'total_costs', 'pct_income': 'operating_income'}


def columns(report: BwaReport) -> list[Column]:
    """Die acht Wertspalten: je Block (Monat, aufgelaufen) vier."""
    if report.variant == VARIANT_YEAR:
        return [Column(period, kind) for period in ('month', 'ytd')
                for kind in ('amount', 'pct_revenue', 'pct_costs', 'pct_income')]
    return [Column('month', 'amount'), Column('prev_month', 'amount'), Column('month', 'delta'), Column('month', 'delta_pct'),
            Column('ytd', 'amount'), Column('prev_ytd', 'amount'), Column('ytd', 'delta'), Column('ytd', 'delta_pct')]


def block_headers(report: BwaReport) -> tuple[str, str]:
    """Die Überschriften der beiden Blöcke: Monat und aufgelaufen (im Januar ist beides derselbe Zeitraum)."""
    name = MONTH_NAMES[report.month - 1]
    suffix = f' {report.year}' if report.variant == VARIANT_YEAR else ''
    if report.month == 1:
        return f'{name}{suffix}', f'{name}{suffix} (aufgelaufen)'
    return f'{name}{suffix}', f'Januar bis {name}{suffix}'


def period_line(report: BwaReport) -> str:
    """Die Zeile unter dem Titel des PDF: Monat, aufgelaufen und – im Vorjahresvergleich – die Darstellung."""
    name = MONTH_NAMES[report.month - 1]
    accumulated = f'Januar {report.year}' if report.month == 1 else f'Januar bis {name} {report.year}'
    text = f'{name} {report.year} · aufgelaufen {accumulated}'
    if report.variant == VARIANT_PREVIOUS:
        text += ' · Vergleich mit dem Vorjahr'
    return text


def column_labels(report: BwaReport, short: bool = False) -> list[str]:
    """Die Köpfe der acht Wertspalten; ``short`` für das PDF (schmale Spalten)."""
    if report.variant == VARIANT_YEAR:
        block = (['Betrag €', '% Erlöse', '% Kosten', '% Betr.-Einn.'] if short
                 else ['Betrag €', '% der Erlöse', '% der Kosten', '% der Betriebseinnahmen'])
    else:
        block = [str(report.year), str(report.year - 1), 'Veränderung €', 'Veränderung %']
    return block * 2


def percent(numerator: int, denominator: int) -> Decimal | None:
    """Anteil in Prozent auf 0,01 gerundet (kaufmännisch); leer (``None``), wenn die Bezugsgröße null oder negativ ist – ein Anteil
    an negativen Erlösen oder Kosten ließe sich nicht lesen. Das Vorzeichen folgt dem Zähler."""
    if denominator <= 0:
        return None
    return _hundredths(Decimal(numerator) * 100 / Decimal(denominator))


def change_percent(current: int, previous: int) -> Decimal | None:
    """Veränderung zum Vorjahr in Prozent, auf 0,01 gerundet; leer, wenn der Vorjahreswert null ist. Mit dem Betrag im Nenner heißt
    ein positiver Wert immer „gestiegen“, auch bei einem Verlust im Vorjahr."""
    if previous == 0:
        return None
    return _hundredths(Decimal(current - previous) * 100 / Decimal(abs(previous)))


def _hundredths(value: Decimal) -> Decimal | None:
    """Auf 0,01 gerundet (kaufmännisch). ``None`` (leere Zelle), wenn der Wert die Stellen des Decimal-Kontexts sprengt – ein Anteil
    ab 10^24 Prozent ist keine Aussage, und die Seite soll daran nicht scheitern."""
    try:
        rounded = value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return rounded if rounded else Decimal('0.00')               # nie „-0,00“


def format_amount(minor: int | None) -> str:
    """Ein Betrag auf den Cent, deutsch (``-1.234,56``); ``None`` bleibt leer."""
    return '' if minor is None else format_de(minor, 2)


_SWAP_SEPARATORS = str.maketrans(',.', '.,')       # '1,234.56' → '1.234,56'


def format_percent(value: Decimal | None) -> str:
    """Ein Anteil mit zwei Stellen und Komma (``-12,34``); ``None`` bleibt leer."""
    if value is None:
        return ''
    return format(value if value else Decimal('0.00'), ',.2f').translate(_SWAP_SEPARATORS)


def cells(report: BwaReport, line: BwaLine, values: dict[str, int]) -> list[str]:
    """Die acht Zellen einer Zeile (``values``: ``line.values`` oder die Werte eines ihrer Konten). Fehlt ein Zeitraum in ``values``
    (ein Konto, das dort nicht in dieser Zeile steht), bleiben Betrag und Anteile leer; für die Veränderung zählt ein fehlender
    Wert als 0, sind beide leer, bleibt sie leer."""
    return [_cell(report, line, values, column) for column in columns(report)]


def _cell(report: BwaReport, line: BwaLine, values: dict[str, int], column: Column) -> str:
    kind, period = column.kind, column.period
    if kind == 'amount':
        return format_amount(values.get(period))
    if kind in PERCENT_BASE:
        value = values.get(period)
        if value is None or (kind == 'pct_costs' and not line.in_costs):
            return ''
        return format_percent(percent(value, report.line(PERCENT_BASE[kind]).values[period]))
    current, previous = values.get(period), values.get(PREVIOUS_OF[period])
    if current is None and previous is None:
        return ''
    current, previous = current or 0, previous or 0
    if kind == 'delta':
        return format_amount(current - previous)
    return format_percent(change_percent(current, previous))


def visible(report: BwaReport, line: BwaLine) -> bool:
    """Gruppen-, Summen- und Ergebniszeilen stehen immer da (auch 0,00); „Nicht zugeordnet“ und „nicht im Ergebnis“ nur mit einem
    Wert ≠ 0 in einem angezeigten Zeitraum oder mit Konten."""
    if line.key not in OPTIONAL_KEYS:
        return True
    return bool(line.accounts) or any(line.values.values())


def visible_accounts(report: BwaReport, line: BwaLine) -> list[BwaAccount]:
    """Die Konten einer Zeile mit einem Wert ≠ 0 in einem angezeigten Zeitraum."""
    return [account for account in line.accounts if any(account.values.get(period) for period in line.values)]


def bwa_filename(year: int, month: int, variant: str) -> str:
    """``BWA_2026-10.pdf`` im Vorjahresvergleich (die Standarddarstellung), ``BWA_2026-10_Anteile.pdf`` mit Anteilen."""
    suffix = '' if variant == VARIANT_PREVIOUS else '_Anteile'
    return f'BWA_{year:04d}-{month:02d}{suffix}.pdf'
