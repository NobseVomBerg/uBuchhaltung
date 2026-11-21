# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Einnahmenüberschussrechnung (EÜR): EINE Aufbereitung für den Kasten des Dashboards und die Seite ``/euer`` samt PDF – ohne
Datenbank, ohne Uhr.

Die Aufbereitung liest keine Buchungen. Sie nimmt die Zeilen von ``Database.get_euer_data`` (Saldo je SKR-Konto und Zeitraum, netto,
nach Zahlungsdatum) und ordnet sie nach ``reporting.euer_section`` – derselben Regel wie die betriebswirtschaftliche Auswertung
(``db.bwa``) – in Einnahmen, Ausgaben und „Sonstige Ausgaben“ (angezeigt, aber nicht im Ergebnis). Dashboard, Seite und PDF zeigen
deshalb dieselben Zahlen.

Gerechnet wird in Minor Units (``money``): je Zeile ``to_minor`` des Euro-Betrags, die Summen als ``int``, nie als Summe von
``float``. Gerundet wird erst bei der Ausgabe (``format_de``). Die Reihenfolge der Zeilen bleibt die von ``get_euer_data``; die echte
und die virtuelle Zeile 3806 (Umsatzsteuer aus Einnahmen) bleiben zwei Zeilen.
"""
import re
from dataclasses import dataclass

from money import to_minor
from .reporting import euer_section

# ── Texte (Seite und PDF) ─────────────────────────────────────────────────────

EUER_TITLE = 'Einnahmenüberschussrechnung'
EUER_HINT_TEXT = ('Aus den Buchungen dieser App nach Zahlungsdatum gerechnet – dieselben Zahlen wie auf dem Dashboard. '
                  'Kein amtliches Formular; keine Steuerberatung, ersetzt nicht die Auswertung eines Steuerbüros.')
EUER_OTHER_TEXT = '„Sonstige Ausgaben“ werden angezeigt, zählen aber nicht im Ergebnis.'
EUER_INVALID_TEXT = 'Der gewählte Zeitraum ist ungültig – angezeigt wird das laufende Jahr.'
EUER_LINK_TEXT = 'zur Auswertung'
EUER_FILTER_SUFFIX = ' (dort ohne Kontofilter)'

INCOME_HEADING = 'Einnahmen'
EXPENSE_HEADING = 'Ausgaben'
OTHER_HEADING = 'Sonstige Ausgaben (nicht im Ergebnis)'
INCOME_SUM_LABEL = 'Summe Betriebseinnahmen'
EXPENSE_SUM_LABEL = 'Summe Betriebsausgaben'
RESULT_LABEL = 'Ergebnis'
OTHER_SUM_LABEL = 'Summe Sonstige Ausgaben'
COLUMN_ACCOUNT, COLUMN_NAME, COLUMN_AMOUNT = 'Konto', 'Bezeichnung', 'Saldo €'

EUER_PERIOD_LINE = 'Zeitraum: {von} bis {bis}'
EUER_FOOTER = ('Aus den Buchungen dieser App nach Zahlungsdatum gerechnet. Kein amtliches Formular.',
               'Keine Steuerberatung; ersetzt nicht die Auswertung eines Steuerbüros.')


# ── Die Aufbereitung ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EuerLine:
    """Eine Zeile: Kontonummer, Name und Saldo in Minor Units (Einnahmen positiv, Ausgaben negativ wie in ``get_euer_data``)."""
    number: int
    name: str
    minor: int


@dataclass(frozen=True)
class EuerStatement:
    """Die EÜR eines Zeitraums. ``result`` = Summe der Einnahmen und der Ausgaben; die „Sonstigen Ausgaben“ (``other``) zählen nicht mit."""
    income: tuple[EuerLine, ...]
    expense: tuple[EuerLine, ...]
    other: tuple[EuerLine, ...]
    income_sum: int
    expense_sum: int
    other_sum: int
    result: int


def euer_statement(rows, bank_numbers) -> EuerStatement:
    """Die Zeilen von ``get_euer_data`` (``(Kontonummer, Name, Saldo in Euro)``) in die drei Abschnitte ordnen und summieren.
    ``bank_numbers`` sind die SKR-Nummern der Geldkonten (``reporting.bank_skr_numbers``). Eine Zeile, die ``euer_section`` nicht
    nennt (Geld-, Privat-, Anlage- und Wartekonten), steht nicht in der EÜR."""
    sections: dict[str, list[EuerLine]] = {'income': [], 'expense': [], 'other': []}
    for number, name, total in rows:
        section = euer_section(number, bank_numbers)
        if section is not None:
            sections[section].append(EuerLine(number, name, to_minor(total)))
    income, expense, other = (tuple(sections[key]) for key in ('income', 'expense', 'other'))
    income_sum = sum(line.minor for line in income)
    expense_sum = sum(line.minor for line in expense)
    other_sum = sum(line.minor for line in other)
    return EuerStatement(income, expense, other, income_sum, expense_sum, other_sum, income_sum + expense_sum)


# ── Zeitraum und Dateiname ────────────────────────────────────────────────────

_ISO_DATE = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})')


def _is_full_year(date_from: str, date_to: str) -> bool:
    """Ein volles Kalenderjahr: vom 1. Januar bis zum 31. Dezember desselben Jahres."""
    return date_from[5:] == '01-01' and date_to[5:] == '12-31' and date_from[:4] == date_to[:4]


def german_date(iso: str) -> str:
    """``2026-03-01`` → ``01.03.2026``. Erwartet ein geprüftes ISO-Datum; alles andere bleibt, wie es ist."""
    found = _ISO_DATE.fullmatch(iso)
    return f'{found[3]}.{found[2]}.{found[1]}' if found else iso


def range_label(date_from: str, date_to: str) -> str:
    """Die Überschrift des Zeitraums: ``2026`` für ein volles Kalenderjahr, sonst ``01.03.2026 – 31.03.2026``."""
    if _is_full_year(date_from, date_to):
        return date_from[:4]
    return f'{german_date(date_from)} – {german_date(date_to)}'


def euer_filename(date_from: str, date_to: str) -> str:
    """``EUER_2026.pdf`` für ein volles Kalenderjahr, sonst ``EUER_2026-01-01_2026-03-31.pdf``. Erwartet geprüfte ISO-Daten."""
    if _is_full_year(date_from, date_to):
        return f'EUER_{date_from[:4]}.pdf'
    return f'EUER_{date_from}_{date_to}.pdf'
