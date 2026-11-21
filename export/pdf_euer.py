# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
PDF der Einnahmenüberschussrechnung (A4 hoch, mehrseitig).

Die Seite (``server/pages_euer.py``), der Kasten des Dashboards und das PDF zeigen dieselbe Aufbereitung (``db.euer.EuerStatement``);
hier steht nur das Layout. Kein amtliches Formular: Die Zeilen sind die Konten der EÜR mit ihrem Saldo, in drei Abschnitten
(Einnahmen, Ausgaben, „Sonstige Ausgaben“ ohne Wirkung auf das Ergebnis); das Ergebnis steht zwischen den Ausgaben und den „Sonstigen“.

Aufbau: Kopf (Titel, Firma, Zeitraum, Spaltenköpfe) und Fuß (zwei Hinweiszeilen, „Seite n von m“) stehen auf jeder Seite. Ein Abschnitt
(Überschrift, Konten, Summe) bildet einen Block, das Ergebnis einen eigenen; ein Block wird nicht geteilt, solange er auf eine Seite
passt (``pdf_table.page_layout``). Es wird nichts auf die Platte geschrieben: ``generate_euer_pdf`` liefert die Bytes, wie das PDF der
betriebswirtschaftlichen Auswertung.

Text steht immer als ``1 0 0 1 x y Tm (…) Tj``, gemessen mit den Breiten der eingebetteten Schrift (``pdf_table``) – nur so liegen die
Beträge rechtsbündig auf einer Kante.
"""
import datetime
from typing import NamedTuple

from db.bwa import format_amount
from db.euer import (COLUMN_ACCOUNT, COLUMN_AMOUNT, COLUMN_NAME, EUER_FOOTER, EUER_PERIOD_LINE, EUER_TITLE, EXPENSE_HEADING,
                     EXPENSE_SUM_LABEL, INCOME_HEADING, INCOME_SUM_LABEL, OTHER_HEADING, OTHER_SUM_LABEL, RESULT_LABEL,
                     EuerStatement, german_date)
from . import pdf_table
from .pdf_core import build_multi_page_pdf

PAGE_SIZE = (595, 842)             # A4 hoch
LEFT_X = 50
RIGHT_X = 545
NAME_X = 100                       # die Bezeichnung beginnt hier, die Nummer steht bei LEFT_X
AMOUNT_RESERVE = 100               # Platz für den Betrag rechts: Namen werden auf RIGHT_X − AMOUNT_RESERVE − NAME_X pt gekürzt
NAME_WIDTH = RIGHT_X - AMOUNT_RESERVE - NAME_X
MAX_NAME_CHARS = 200               # so viele Zeichen eines Namens werden höchstens betrachtet

TITLE_Y = 800
COMPANY_Y = 784
PERIOD_Y = 771
HEADER_Y = 750                     # Spaltenköpfe
HEADER_LINE_Y = 746
TABLE_TOP = 732                    # Y der ersten Zeile
ROW_H = 12
ROW_SIZE = 8
BOTTOM_Y = 64                      # keine Zeile darunter
FOOTER_TOP_Y = 44                  # erste Hinweiszeile
FOOTER_Y = 35                      # zweite Hinweiszeile und „Seite n von m“
SUM_LINE_RISE = 9                  # die Linie über einer Summenzeile liegt so weit über deren Grundlinie
LINES_PER_PAGE = int((TABLE_TOP - BOTTOM_Y) // ROW_H) + 1


class Row(NamedTuple):
    """Eine Zeile der Tabelle: ``kind`` ist ``heading``, ``account`` oder ``sum`` (auch das Ergebnis); ``number`` und ``amount`` bleiben
    leer, wo es sie nicht gibt."""
    kind: str
    number: str
    label: str
    amount: str


def page_layout(block_heights) -> list[list[int]]:
    """Die Höhen (in Zeilen) der Blöcke auf die Seiten verteilen (``pdf_table.page_layout`` mit den Zeilen dieser Seite)."""
    return pdf_table.page_layout(block_heights, LINES_PER_PAGE)


def _section(heading: str, lines, sum_label: str, total: int) -> list[Row]:
    rows = [Row('heading', '', heading, '')]
    rows += [Row('account', str(line.number), line.name, format_amount(line.minor)) for line in lines]
    rows.append(Row('sum', '', sum_label, format_amount(total)))
    return rows


def _blocks(statement: EuerStatement) -> list[list[Row]]:
    """Die Blöcke der Tabelle: Einnahmen, Ausgaben, das Ergebnis und die „Sonstigen Ausgaben“, die nicht im Ergebnis stehen."""
    return [_section(INCOME_HEADING, statement.income, INCOME_SUM_LABEL, statement.income_sum),
            _section(EXPENSE_HEADING, statement.expense, EXPENSE_SUM_LABEL, statement.expense_sum),
            [Row('sum', '', RESULT_LABEL, format_amount(statement.result))],
            _section(OTHER_HEADING, statement.other, OTHER_SUM_LABEL, statement.other_sum)]


def _page_ops(rows: list[Row], number: int, total: int, company: str, date_from: str, date_to: str,
              created: datetime.date) -> list[str]:
    ops = ['BT']
    ops += pdf_table.cell(LEFT_X, TITLE_Y, EUER_TITLE, '/F2', 14)
    if company:
        ops += pdf_table.cell(LEFT_X, COMPANY_Y, pdf_table.fit(company, RIGHT_X - LEFT_X, 10, MAX_NAME_CHARS), '/F1', 10)
    ops += pdf_table.cell(LEFT_X, PERIOD_Y, EUER_PERIOD_LINE.format(von=german_date(date_from), bis=german_date(date_to)), '/F1', 9)
    ops += pdf_table.cell(RIGHT_X, PERIOD_Y, f'Erstellt am {created.strftime("%d.%m.%Y")}', '/F1', 9, right=True)
    ops += pdf_table.cell(LEFT_X, HEADER_Y, COLUMN_ACCOUNT, '/F2', ROW_SIZE)
    ops += pdf_table.cell(NAME_X, HEADER_Y, COLUMN_NAME, '/F2', ROW_SIZE)
    ops += pdf_table.cell(RIGHT_X, HEADER_Y, COLUMN_AMOUNT, '/F2', ROW_SIZE, right=True)
    lines = [pdf_table.hline(HEADER_LINE_Y, LEFT_X, RIGHT_X)]

    y = TABLE_TOP
    for row in rows:
        if row.kind == 'account':
            ops += pdf_table.cell(LEFT_X, y, row.number, '/F1', ROW_SIZE)
            name = pdf_table.fit(row.label, NAME_WIDTH, ROW_SIZE, MAX_NAME_CHARS)
            if name:                                                  # ein Konto ohne Namen druckt nur Nummer und Betrag
                ops += pdf_table.cell(NAME_X, y, name, '/F1', ROW_SIZE)
            ops += pdf_table.cell(RIGHT_X, y, row.amount, '/F1', ROW_SIZE, right=True)
        else:
            ops += pdf_table.cell(LEFT_X, y, row.label, '/F2', ROW_SIZE)
            if row.amount:
                ops += pdf_table.cell(RIGHT_X, y, row.amount, '/F2', ROW_SIZE, right=True)
            if row.kind == 'sum':
                lines.append(pdf_table.hline(y + SUM_LINE_RISE, LEFT_X, RIGHT_X))
        y -= ROW_H

    ops += pdf_table.cell(LEFT_X, FOOTER_TOP_Y, EUER_FOOTER[0], '/F1', 7, gray=True)
    ops += pdf_table.cell(LEFT_X, FOOTER_Y, EUER_FOOTER[1], '/F1', 7, gray=True)
    if total > 1:
        ops += pdf_table.cell(RIGHT_X, FOOTER_Y, f'Seite {number} von {total}', '/F1', 7, right=True)
    ops.append('ET')
    return ops + lines


def generate_euer_pdf(statement: EuerStatement, date_from: str, date_to: str, company: str, created: datetime.date) -> bytes:
    """Das PDF einer EÜR (PDF/A-3b, A4 hoch) als Bytes – nichts wird auf die Platte geschrieben. ``date_from`` und ``date_to`` sind
    geprüfte ISO-Daten; ``company`` (der erste eigene Kontakt, sonst leer) steht im Kopf und als Autor, ``created`` als „Erstellt am“."""
    blocks = _blocks(statement)
    layout = page_layout([len(block) for block in blocks])
    rows = [row for block in blocks for row in block]
    pages, first = [], 0
    for number, heights in enumerate(layout, start=1):
        count = sum(heights)
        pages.append(_page_ops(rows[first:first + count], number, len(layout), company or '', date_from, date_to, created))
        first += count
    return build_multi_page_pdf(pages, page_size=PAGE_SIZE, title=EUER_TITLE, author=company or '')
