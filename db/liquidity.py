# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Liquiditätsrechnung: vom Ergebnis zur Veränderung der flüssigen Mittel – ohne Datenbank, ohne Uhr.

Die Rechnung hat keine eigene Lesart der Buchungen. Sie nimmt drei Quellen und sonst nichts:

* die Zeilen von ``Database.get_euer_data`` (Saldo je SKR-Konto und Zeitraum, netto, nach Zahlungsdatum; das Vorzeichen ist das der
  Geldbewegung: Zufluss +), eingeordnet von ``reporting.euer_section`` – derselben Funktion wie auf der EÜR-Seite und in der
  betriebswirtschaftlichen Auswertung;
* den Bericht der betriebswirtschaftlichen Auswertung (``db.bwa.build_report``) über dieselben Zeilen: sein Ergebnis steht in der ersten Zeile,
  seine Gruppen „Abschreibungen“ und „Tilgungen und Verbindlichkeiten“ in der zweiten und vierten;
* die Töpfe des Dashboards (``Database._dashboard_movements``: Einnahme, privat, Ausgabe, Übertrag), die ``CashFigures`` hier in Minor Units
  mitbringen, dazu die Bankbewegungen ohne Buchungssatz.

Überleitung (alle Werte mit ihrer Wirkung auf die flüssigen Mittel, Zufluss positiv):

    Ergebnis (wie BWA)
  + Abschreibungen                                  kein Geldabfluss: BWA-Gruppe ``depreciation``
  + Anlagenabgänge zum Restbuchwert                 kein Geldzufluss: die Konten von ``DISPOSAL_RANGES``
  + Darlehen und Verbindlichkeiten im Ergebnis      herausgerechnet: BWA-Gruppe ``liabilities`` (3000–3799 außer 3160, 3720, 3740)
  = Ergebnis vor Abschreibungen und Darlehen
  + Anlagenzugänge                                  Zahlungen auf Anlagekonten (unter 1000), nicht im Ergebnis
  + Darlehen und Verbindlichkeiten                  alle Konten 3000–3799 (Aufnahme +, Tilgung −)
  + Privatentnahmen, + Privateinlagen               2100–2179 bzw. 2180–2199
  + übrige Konten außerhalb des Ergebnisses         erwartet 0, steht nur bei einem Wert
  = Veränderung der flüssigen Mittel laut Überleitung
    Bankbewegungen ohne Buchungssatz                nicht gedeckt von einem Übertrag; stehen im Ist, aber in keinem Satz
    Nicht zugeordnet                                Ist − (Überleitung + Bankbewegungen ohne Buchungssatz); steht immer da, auch bei 0,00
  = Veränderung der flüssigen Mittel (Bank und Kasse, ohne Überträge zwischen eigenen Konten) – das „Ist“

„Nicht zugeordnet“ ist per Definition die Differenz, also keine dritte Lesart: Was die Überleitung aus den Buchungen nicht erklärt (eine
Zahlung steht in der EÜR in einem anderen Monat oder mit einem anderen Betrag als auf dem Konto), bleibt dort sichtbar und wird nie einer
anderen Zeile zugeschlagen. „Geht auf“ heißt exakt 0 Minor Units.

Gerechnet wird in Minor Units (``int``, ``money``), gerundet erst bei der Anzeige (``cells``). Die Formate (``format_amount``,
``format_percent``, ``change_percent``) und die Zeiträume gehören der betriebswirtschaftlichen Auswertung (``db.bwa``).
"""
from dataclasses import dataclass, field

from money import to_minor
from .bwa import (VARIANT_PREVIOUS, VARIANT_YEAR, Column, PREVIOUS_OF, change_percent, format_amount, format_percent, periods_for)
from .reporting import euer_section

# ── Bereiche als Daten ────────────────────────────────────────────────────────

DISPOSAL_RANGES = ((4855, 4858), (6895, 6898))       # Restbuchwert beim Abgang (Buchgewinn, Buchverlust): Ertrag bzw. Aufwand ohne Geldfluss
FINANCING_RANGE = (3000, 3799)                       # Darlehen und Verbindlichkeiten (die BWA-Gruppe ``liabilities`` plus 3160, 3720, 3740)
WITHDRAWAL_RANGE = (2100, 2179)                      # Privatentnahmen
DEPOSIT_RANGE = (2180, 2199)                         # Privateinlagen
ASSET_LIMIT = 1000                                   # darunter liegen die Anlagekonten (dieselbe Grenze wie in ``euer_section``)


def _within(number, bounds: tuple[int, int]) -> bool:
    """Die Kontonummer liegt im Bereich. Nur eine ganze Zahl (kein ``bool``) hat einen Bereich – wie in ``bwa.group_of``: Eine Kommazahl (per
    SQL-Konsole schreibbar) führt die betriebswirtschaftliche Auswertung unter „Nicht zugeordnet“, sie gehört dann auch hier in keine eigene Zeile."""
    return _whole(number) and bounds[0] <= number <= bounds[1]


def _whole(number) -> bool:
    return isinstance(number, int) and not isinstance(number, bool)


# ── Texte ─────────────────────────────────────────────────────────────────────

TITLE = 'Liquiditätsrechnung'
HINT_TEXT = 'Aus den Buchungen dieser App gerechnet. Keine Steuerberatung; ersetzt nicht die Auswertung eines Steuerbüros.'
SIGN_TEXT = 'Jeder Wert steht mit seiner Wirkung auf die flüssigen Mittel (Bank und Kasse): ein Zufluss positiv, ein Abfluss negativ.'
METHOD_TEXT = ('Ausgangspunkt ist das Ergebnis der betriebswirtschaftlichen Auswertung. Abschreibungen und Anlagenabgänge zum Restbuchwert '
               'bewegen kein Geld und werden zurückgerechnet; Darlehen und Verbindlichkeiten, die schon im Ergebnis stehen, werden herausgenommen '
               'und als eigene Zeile gezeigt. Dazu kommen Anlagenzugänge, Privatentnahmen und Privateinlagen. Die Veränderung der flüssigen Mittel '
               'selbst entsteht aus den Bewegungen auf den Bank- und Kassenkonten, ohne Überträge zwischen eigenen Konten.')
UNASSIGNED_TEXT = ('Diesen Betrag erklärt die Überleitung nicht: Eine Zahlung steht in der Einnahmenüberschussrechnung in einem anderen Monat oder '
                   'mit einem anderen Betrag als auf dem Konto – etwa bei Buchungssätzen mit einem anderen Datum als ihre Bankbewegung, über ein '
                   'Verrechnungskonto oder bei einer Sofortabschreibung, die direkt bezahlt wurde.')
BALANCED_TEXT = 'Die Überleitung geht auf.'
UNBALANCED_TEXT = 'Die Überleitung geht nicht auf: Die Zeile „Nicht zugeordnet“ nennt den Unterschied zur Veränderung laut Kontobewegungen.'
ROUNDING_TEXT = 'Ein Rest unter einem halben Cent ist ein Rundungsrest; er steht als 0,00 da.'
TRANSFER_TEXT = 'Überträge zwischen eigenen Konten ändern die flüssigen Mittel nicht und stehen deshalb nur nachrichtlich da.'

# ── Darstellung ───────────────────────────────────────────────────────────────

VARIANT_LABELS = {VARIANT_PREVIOUS: 'Vergleich mit dem Vorjahr', VARIANT_YEAR: 'Monat und Jahr'}

# ── Aufbau ────────────────────────────────────────────────────────────────────

LINE, SUM, RESULT, MEMO = 'line', 'sum', 'result', 'memo'


@dataclass(frozen=True)
class LineSpec:
    """Eine Zeile des Aufbaus: ``kind`` ist ``line`` (ein Wert, auf Wunsch mit Konten), ``sum`` (Zwischensumme), ``result`` (das Ergebnis der
    Rechnung) oder ``memo`` (nachrichtlich, in keiner Summe); bei ``sum`` sind ``plus`` die Schlüssel der Zeilen, aus denen sie entsteht.
    ``optional``: Die Zeile steht nur, wenn sie in einem angezeigten Zeitraum einen Wert ≠ 0 hat."""
    key: str
    kind: str
    label: str
    optional: bool = False
    plus: tuple[str, ...] = ()


LAYOUT: tuple[LineSpec, ...] = (
    LineSpec('result', LINE, 'Ergebnis (wie BWA)'),
    LineSpec('depreciation', LINE, 'Abschreibungen'),
    LineSpec('disposals', LINE, 'Anlagenabgänge zum Restbuchwert'),
    LineSpec('financing_in_result', LINE, 'Darlehen und Verbindlichkeiten im Ergebnis (herausgerechnet)'),
    LineSpec('operating', SUM, 'Ergebnis vor Abschreibungen und Darlehen',
             plus=('result', 'depreciation', 'disposals', 'financing_in_result')),
    LineSpec('assets', LINE, 'Anlagenzugänge (Zahlungen auf Anlagekonten)'),
    LineSpec('financing', LINE, 'Darlehen und Verbindlichkeiten (Aufnahme +, Tilgung −)'),
    LineSpec('withdrawals', LINE, 'Privatentnahmen'),
    LineSpec('deposits', LINE, 'Privateinlagen'),
    LineSpec('outside_other', LINE, 'Übrige Konten außerhalb des Ergebnisses', optional=True),
    LineSpec('computed', SUM, 'Veränderung der flüssigen Mittel laut Überleitung',
             plus=('operating', 'assets', 'financing', 'withdrawals', 'deposits', 'outside_other')),
    LineSpec('unbooked', LINE, 'Bankbewegungen ohne Buchungssatz'),
    LineSpec('unassigned', LINE, 'Nicht zugeordnet'),
    LineSpec('actual', RESULT, 'Veränderung der flüssigen Mittel (ohne Überträge)'),
    LineSpec('memo_transfers', MEMO, 'Nachrichtlich: Überträge zwischen eigenen Konten', optional=True),
    LineSpec('memo_balance', MEMO, 'Nachrichtlich: Veränderung laut Kontobewegungen', optional=True),
)

# Die Zeilen, unter denen sich die Konten aufklappen lassen (die Konten von ``depreciation`` und ``financing_in_result`` kommen aus der BWA)
ACCOUNT_KEYS = ('depreciation', 'disposals', 'financing_in_result', 'assets', 'financing', 'withdrawals', 'deposits', 'outside_other')

# Die Zeilen, die ihre Werte aus dem Bericht der betriebswirtschaftlichen Auswertung nehmen: Schlüssel hier → Zeile dort
BWA_LINES = {'result': 'result', 'depreciation': 'depreciation', 'financing_in_result': 'liabilities'}


@dataclass(frozen=True)
class CashFigures:
    """Die Kontobewegungen eines Zeitraums in Minor Units, aus den Töpfen des Dashboards (ohne Kontofilter): ``actual`` = Einnahme + privat +
    Ausgabe, ``transfers`` = Überträge zwischen eigenen Konten (nicht in ``actual``); ``unbooked`` und ``unbooked_count`` die Summe und Zahl
    der Bankbewegungen ohne Buchungssatz, die kein Übertrag deckt (sie stehen in ``actual``)."""
    actual: int
    transfers: int
    unbooked: int
    unbooked_count: int


@dataclass
class LiquidityAccount:
    """Ein Konto in einer Zeile; ``values`` nennt nur die Zeiträume, in denen es in DIESER Zeile steht (Minor Units, Vorzeichen der Zeile)."""
    number: int
    name: str
    values: dict[str, int] = field(default_factory=dict)


@dataclass
class LiquidityLine:
    """Eine Zeile; ``values`` hat für jeden angezeigten Zeitraum einen Wert (Standard 0), ``counts`` bei ``unbooked`` die Zahl der Bewegungen je
    Zeitraum. Die Konten stehen nach Nummer sortiert."""
    key: str
    label: str
    kind: str
    optional: bool
    values: dict[str, int]
    accounts: list[LiquidityAccount] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class LiquidityReport:
    year: int
    month: int
    variant: str
    periods: dict[str, tuple[str, str]]
    lines: list[LiquidityLine]

    def __post_init__(self):
        self._by_key = {line.key: line for line in self.lines}

    def line(self, key: str) -> LiquidityLine:
        return self._by_key[key]


def build_liquidity(year: int, month: int, variant: str, periods: dict, euer_rows: dict, bwa_report, bank_numbers,
                    cash: dict) -> LiquidityReport:
    """Den Bericht bauen. ``euer_rows`` je Zeitraum (``periods_for(variant)``) die Liste ``(Kontonummer, Name, Saldo in Euro)`` von
    ``get_euer_data``, ``bwa_report`` der Bericht der betriebswirtschaftlichen Auswertung über dieselben Zeilen (``build_report``),
    ``bank_numbers`` die SKR-Nummern der Geldkonten (``reporting.bank_skr_numbers``), ``cash`` je Zeitraum die ``CashFigures``.

    Jede Zeile von ``get_euer_data`` steht an genau einer Stelle: Eine Zeile im Ergebnis (``euer_section`` ``income``/``expense``) steckt in der
    ersten Zeile; ``depreciation`` und ``financing_in_result`` heben nur ihren Anteil aus der BWA heraus, ``disposals`` den Restbuchwert
    (``DISPOSAL_RANGES``). Eine Zeile außerhalb des Ergebnisses steht nach ihrer Nummer unter Anlagenzugängen (unter ``ASSET_LIMIT``),
    Privatentnahmen, Privateinlagen oder – wenn sie in keinen dieser Bereiche passt – unter „Übrige Konten außerhalb des Ergebnisses“; die Konten
    3000–3799 (auch 3160, 3720, 3740, die nicht im Ergebnis stehen) stehen unter ``financing``. Zwei Zeilen mit derselben Nummer (die echte und die
    virtuelle 3806, zwei Konten gleicher Nummer aus verschiedenen Kontenrahmen) werden je Zeitraum addiert und stehen im Kontennachweis als EINE Zeile
    unter dem zuerst gefundenen Namen."""
    shown = periods_for(variant)
    sections: dict[int, str | None] = {}
    names: dict[int, str] = {}
    totals: dict[str, dict[int, int]] = {period: {} for period in shown}
    for period in shown:                                         # Reihenfolge: Monat, aufgelaufen, Vorjahre – der erste Name zählt
        for number, name, total in euer_rows.get(period, ()):
            if number not in sections:
                sections[number] = euer_section(number, bank_numbers)
            names.setdefault(number, name)
            totals[period][number] = totals[period].get(number, 0) + to_minor(total)

    lines = {spec.key: LiquidityLine(spec.key, spec.label, spec.kind, spec.optional, dict.fromkeys(shown, 0)) for spec in LAYOUT}
    accounts: dict[str, dict[int, LiquidityAccount]] = {key: {} for key in lines}

    def place(key: str, number: int, period: str, amount: int):
        lines[key].values[period] += amount
        account = accounts[key].setdefault(number, LiquidityAccount(number, names[number]))
        account.values[period] = amount

    for period in shown:
        for number, minor in totals[period].items():
            section = sections[number]
            if section in ('income', 'expense'):                 # im Ergebnis: die erste Zeile; hier nur die Teile, die ein anderes Gesicht haben
                if any(_within(number, bounds) for bounds in DISPOSAL_RANGES):
                    place('disposals', number, period, -minor)
                elif section == 'expense' and _within(number, FINANCING_RANGE):
                    place('financing', number, period, minor)
            elif section == 'other' and _within(number, FINANCING_RANGE):
                place('financing', number, period, minor)
            elif section is None and _whole(number) and number < ASSET_LIMIT:
                place('assets', number, period, minor)
            elif section is None and _within(number, WITHDRAWAL_RANGE):
                place('withdrawals', number, period, minor)
            elif section is None and _within(number, DEPOSIT_RANGE):
                place('deposits', number, period, minor)
            else:
                place('outside_other', number, period, minor)

    # Ergebnis, Abschreibungen und Darlehen im Ergebnis: die Werte und Konten der betriebswirtschaftlichen Auswertung, unverändert
    for key, bwa_key in BWA_LINES.items():
        source = bwa_report.line(bwa_key)
        for period in shown:
            lines[key].values[period] = source.values[period]
        for account in source.accounts:
            accounts[key][account.number] = LiquidityAccount(account.number, account.name, dict(account.values))

    for spec in LAYOUT:                                          # die Zwischensummen stehen hinter ihren Bestandteilen
        if spec.kind == SUM:
            for period in shown:
                lines[spec.key].values[period] = sum(lines[key].values[period] for key in spec.plus)

    for period in shown:
        figures = cash[period]
        lines['unbooked'].values[period] = figures.unbooked
        lines['unbooked'].counts[period] = figures.unbooked_count
        lines['actual'].values[period] = figures.actual
        lines['unassigned'].values[period] = figures.actual - lines['computed'].values[period] - figures.unbooked
        lines['memo_transfers'].values[period] = figures.transfers
        lines['memo_balance'].values[period] = figures.actual + figures.transfers

    for key, line in lines.items():
        line.accounts = sorted(accounts[key].values(), key=lambda account: account.number)
    return LiquidityReport(year, month, variant, dict(periods), [lines[spec.key] for spec in LAYOUT])


# ── Abstimmung ────────────────────────────────────────────────────────────────

def balanced(report: LiquidityReport) -> bool:
    """Die Überleitung geht auf: „Nicht zugeordnet“ ist in jedem angezeigten Zeitraum exakt 0 Minor Units."""
    return not any(report.line('unassigned').values.values())


def rounding_remainder(report: LiquidityReport) -> bool:
    """Ein Rest in „Nicht zugeordnet“, der als 0,00 dasteht (unter einem halben Cent): Er ist nicht 0 und wird nicht geglättet, die Seite nennt ihn."""
    return any(value and format_amount(value) == '0,00' for value in report.line('unassigned').values.values())


# ── Spalten und Format (gemeinsam für Seite und PDF) ──────────────────────────

def columns(report: LiquidityReport) -> list[Column]:
    """Die Wertspalten: im Vorjahresvergleich acht (je Block Jahr, Vorjahr, Veränderung in Euro und in Prozent), in der Darstellung „Monat und
    Jahr“ zwei – ein Anteil an Erlösen ergäbe bei Liquidität keinen Sinn."""
    if report.variant == VARIANT_YEAR:
        return [Column('month', 'amount'), Column('ytd', 'amount')]
    return [Column('month', 'amount'), Column('prev_month', 'amount'), Column('month', 'delta'), Column('month', 'delta_pct'),
            Column('ytd', 'amount'), Column('prev_ytd', 'amount'), Column('ytd', 'delta'), Column('ytd', 'delta_pct')]


def block_size(report: LiquidityReport) -> int:
    """Die Zahl der Wertspalten je Block (Monat, aufgelaufen): vier im Vorjahresvergleich, eine sonst."""
    return len(columns(report)) // 2


def column_labels(report: LiquidityReport) -> list[str]:
    """Die Köpfe der Wertspalten."""
    if report.variant == VARIANT_YEAR:
        return ['Betrag €', 'Betrag €']
    return [str(report.year), str(report.year - 1), 'Veränderung €', 'Veränderung %'] * 2


def cells(report: LiquidityReport, values: dict[str, int]) -> list[str]:
    """Die Zellen einer Zeile (``values``: ``line.values`` oder die Werte eines ihrer Konten). Fehlt ein Zeitraum in ``values`` (ein Konto, das dort
    nicht in dieser Zeile steht), bleibt der Betrag leer; für die Veränderung zählt ein fehlender Wert als 0, sind beide leer, bleibt sie leer."""
    return [_cell(values, column) for column in columns(report)]


def _cell(values: dict[str, int], column: Column) -> str:
    kind, period = column.kind, column.period
    if kind == 'amount':
        return format_amount(values.get(period))
    current, previous = values.get(period), values.get(PREVIOUS_OF[period])
    if current is None and previous is None:
        return ''
    current, previous = current or 0, previous or 0
    if kind == 'delta':
        return format_amount(current - previous)
    return format_percent(change_percent(current, previous))


def visible(report: LiquidityReport, line: LiquidityLine) -> bool:
    """Jede Zeile steht da (auch mit 0,00), außer den optionalen: „Übrige Konten außerhalb des Ergebnisses“ und die nachrichtlichen Zeilen nur mit
    einem Wert ≠ 0 in einem angezeigten Zeitraum. Die Veränderung laut Kontobewegungen steht nur neben den Überträgen – ohne sie ist sie das Ist."""
    shown = report.line('memo_transfers') if line.key == 'memo_balance' else line
    return not shown.optional or any(shown.values.values())


def visible_accounts(report: LiquidityReport, line: LiquidityLine) -> list[LiquidityAccount]:
    """Die Konten einer Zeile mit einem Wert ≠ 0 in einem angezeigten Zeitraum."""
    return [account for account in line.accounts if any(account.values.get(period) for period in line.values)]


def line_label(report: LiquidityReport, line: LiquidityLine) -> str:
    """Die Bezeichnung einer Zeile; bei den Bankbewegungen ohne Buchungssatz mit ihrer Zahl im Monat und aufgelaufen (im Januar ist das derselbe
    Zeitraum, dort steht die Zahl nur einmal). Ohne eine solche Bewegung steht die Bezeichnung allein."""
    month, accumulated = line.counts.get('month', 0), line.counts.get('ytd', 0)
    if line.key != 'unbooked' or not (month or accumulated):
        return line.label
    if report.month == 1:
        return f'{line.label} ({month})'
    return f'{line.label} ({month} im Monat, {accumulated} aufgelaufen)'


def account_label(account: LiquidityAccount) -> str:
    return f'{account.number} {account.name}' if account.name else str(account.number)


def liquidity_filename(year: int, month: int, variant: str) -> str:
    """``Liquiditaet_2026-10.pdf`` im Vorjahresvergleich (die Standarddarstellung), ``Liquiditaet_2026-10_Monat_Jahr.pdf`` in der Darstellung
    „Monat und Jahr“."""
    suffix = '' if variant == VARIANT_PREVIOUS else '_Monat_Jahr'
    return f'Liquiditaet_{year:04d}-{month:02d}{suffix}.pdf'
