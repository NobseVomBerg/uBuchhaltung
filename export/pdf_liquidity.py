# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
PDF der Liquiditätsrechnung (A4 quer, mehrseitig).

Die Seite (``server/pages_liquidity.py``) und das PDF lesen denselben Bericht (``db.liquidity.LiquidityReport``) und dieselben Regeln für Spalten,
Format und Sichtbarkeit (``db.liquidity``); hier steht nur das Layout. Quer, weil im Vorjahresvergleich neun Spalten – die Bezeichnung und je Block vier
Werte – nur so lesbar nebeneinander stehen; in der Darstellung „Monat und Jahr“ sind es zwei breite Wertspalten am rechten Rand.

Aufbau: Kopf (Titel, Firma, Zeitraum, Blockköpfe, Spaltenköpfe) und Fuß (Hinweis, „Seite n von m“) stehen auf jeder Seite. Eine Zeile und ihre Konten
bilden einen Block; ein Block wird nicht geteilt, solange er auf eine Seite passt (``pdf_table.page_layout``). Hinter der Tabelle steht ein Textblock mit
dem Satz zur Abstimmung und den festen Erläuterungen, umbrochen auf die ganze Breite. Es wird nichts auf die Platte geschrieben:
``generate_liquidity_pdf`` liefert die Bytes, wie das PDF der betriebswirtschaftlichen Auswertung.

Text steht immer als ``1 0 0 1 x y Tm (…) Tj``, gemessen mit den Breiten der eingebetteten Schrift (``pdf_table``) – nur so liegen die Beträge
rechtsbündig auf einer Kante.
"""
import datetime
from typing import NamedTuple

from db.bwa import block_headers, period_line
from db.liquidity import (BALANCED_TEXT, HINT_TEXT, MEMO, METHOD_TEXT, RESULT, ROUNDING_TEXT, SIGN_TEXT, SUM, TITLE, TRANSFER_TEXT,
                          UNASSIGNED_TEXT, UNBALANCED_TEXT, LiquidityReport, account_label, balanced, block_size, cells, column_labels,
                          columns, line_label, rounding_remainder, visible, visible_accounts)
from . import pdf_table
from .pdf_core import build_multi_page_pdf

PAGE_SIZE = (842, 595)             # A4 quer
LEFT_X = 36
RIGHT_X = 806
LABEL_RIGHT = 290                  # Vorjahresvergleich: rechts davon beginnen die acht Wertspalten
COLUMN_COUNT = 8
YEAR_COLUMN_WIDTH = 120            # „Monat und Jahr“: zwei Wertspalten dieser Breite am rechten Rand
LABEL_GAP = 8                      # zwischen dem Ende einer Bezeichnung und der ersten Wertspalte bleibt mindestens so viel frei
ACCOUNT_X = 48                     # Konten sind eingerückt
ACCOUNT_RESERVE = 52               # Konten werden auf label_right − ACCOUNT_RESERVE pt gekürzt
MAX_LABEL_CHARS = 200              # so viele Zeichen eines Namens werden höchstens betrachtet
MAX_TEXT_LINES = 40                # so viele Zeilen druckt ein einzelner Erläuterungstext höchstens (danach „…“)
TEXT_WIDTH = RIGHT_X - LEFT_X

TITLE_Y = 560
COMPANY_Y = 544
PERIOD_Y = 531
BLOCK_HEADER_Y = 510
COLUMN_HEADER_Y = 499
HEADER_LINE_Y = 495
TABLE_TOP = 484                    # Y der ersten Zeile
ROW_H = 11
ROW_SIZE = 7.5
ACCOUNT_SIZE = 7
BOTTOM_Y = 48                      # keine Zeile darunter
FOOTER_Y = 28                      # Hinweis und „Seite n von m“
SUM_LINE_RISE = 8                  # die Linie über einer Summenzeile liegt so weit über deren Grundlinie
LINES_PER_PAGE = int((TABLE_TOP - BOTTOM_Y) // ROW_H) + 1


class Row(NamedTuple):
    """Eine Zeile: ``kind`` ist ``group`` (eine Zeile mit Wert), ``sum`` (Zwischensumme und Ergebnis), ``memo`` (nachrichtlich, grau), ``account``,
    ``bold`` und ``text`` (Zeilen des Textblocks: der Satz zur Abstimmung bzw. die grauen Erläuterungen)."""
    kind: str
    label: str
    values: list


def label_right(count: int) -> float:
    """Rechts von dieser Kante beginnen die ``count`` Wertspalten: bei acht ``LABEL_RIGHT``, sonst am rechten Rand ``YEAR_COLUMN_WIDTH`` breit."""
    return LABEL_RIGHT if count == COLUMN_COUNT else RIGHT_X - count * YEAR_COLUMN_WIDTH


def column_edge(count: int, index: int) -> float:
    """Rechte Kante der Wertspalte ``index`` (bei ``index`` −1 die linke Kante der ersten Spalte)."""
    left = label_right(count)
    return left + (RIGHT_X - left) / count * (index + 1)


def page_layout(block_heights) -> list[list[int]]:
    """Die Höhen (in Zeilen) der Blöcke auf die Seiten verteilen (``pdf_table.page_layout`` mit den Zeilen dieser Seite)."""
    return pdf_table.page_layout(block_heights, LINES_PER_PAGE)


def _text_rows(text: str, kind: str = 'text') -> list[Row]:
    """Ein Text, umbrochen auf die ganze Breite: eine Zeile je Umbruch (``pdf_table.wrap``)."""
    return [Row(kind, line, []) for line in pdf_table.wrap(text, TEXT_WIDTH, ROW_SIZE, MAX_TEXT_LINES)]


def _text_block(report: LiquidityReport) -> list[Row]:
    """Der Textblock hinter der Tabelle: der Satz zur Abstimmung (die Überleitung geht auf – oder nicht, mit der Erläuterung von „Nicht zugeordnet“ und
    bei Bedarf dem Rundungsrest), dann die festen Erläuterungen zum Rechenweg und zu den Überträgen."""
    block = [Row('text', '', [])]
    if balanced(report):
        block += _text_rows(BALANCED_TEXT, 'bold')
    else:
        block += _text_rows(UNBALANCED_TEXT, 'bold') + _text_rows(UNASSIGNED_TEXT)
        if rounding_remainder(report):
            block += _text_rows(ROUNDING_TEXT)
    block += _text_rows(SIGN_TEXT) + _text_rows(METHOD_TEXT)
    if visible(report, report.line('memo_transfers')):
        block += _text_rows(TRANSFER_TEXT)
    return block


def _blocks(report: LiquidityReport, with_accounts: bool) -> list[list[Row]]:
    """Die Blöcke der Tabelle: eine Zeile mit ihren Konten (auf Wunsch), danach der Textblock."""
    blocks = []
    for line in report.lines:
        if not visible(report, line):
            continue
        kind = 'sum' if line.kind in (SUM, RESULT) else 'memo' if line.kind == MEMO else 'group'
        block = [Row(kind, line_label(report, line), cells(report, line.values))]
        if with_accounts:
            for account in visible_accounts(report, line):
                block.append(Row('account', account_label(account), cells(report, account.values)))
        blocks.append(block)
    blocks.append(_text_block(report))
    return blocks


def _page_ops(report: LiquidityReport, rows: list[Row], number: int, total: int, company: str, created: datetime.date) -> list[str]:
    count = len(columns(report))
    per_block = block_size(report)
    right = label_right(count)
    ops = ['BT']
    ops += pdf_table.cell(LEFT_X, TITLE_Y, TITLE, '/F2', 14)
    if company:
        ops += pdf_table.cell(LEFT_X, COMPANY_Y, pdf_table.fit(company, RIGHT_X - LEFT_X, 10, MAX_LABEL_CHARS), '/F1', 10)
    ops += pdf_table.cell(LEFT_X, PERIOD_Y, period_line(report), '/F1', 9)
    ops += pdf_table.cell(RIGHT_X, PERIOD_Y, f'Erstellt am {created.strftime("%d.%m.%Y")}', '/F1', 9, right=True)

    first_block, second_block = block_headers(report)
    # Die Blocküberschrift steht mittig über den Wertspalten ihres Blocks (rechtsbündig stünde sie nur über der letzten)
    for block, heading in enumerate((first_block, second_block)):
        printed = pdf_table.clean(heading)
        middle = (column_edge(count, per_block * block - 1) + column_edge(count, per_block * block + per_block - 1)) / 2
        ops += pdf_table.cell(middle - pdf_table.width(printed, 8, '/F2') / 2, BLOCK_HEADER_Y, printed, '/F2', 8)
    ops += pdf_table.cell(LEFT_X, COLUMN_HEADER_Y, 'Position', '/F2', 7)
    for index, label in enumerate(column_labels(report)):
        ops += pdf_table.cell(column_edge(count, index), COLUMN_HEADER_Y, label, '/F2', 7, right=True)
    lines = [pdf_table.hline(HEADER_LINE_Y, LEFT_X, RIGHT_X)]

    y = TABLE_TOP
    for row in rows:
        if row.kind in ('text', 'bold'):
            if row.label:
                ops += pdf_table.cell(LEFT_X, y, row.label, '/F2' if row.kind == 'bold' else '/F1', ROW_SIZE, gray=row.kind == 'text')
            y -= ROW_H
            continue
        if row.kind == 'account':
            ops += pdf_table.cell(ACCOUNT_X, y, pdf_table.fit(row.label, right - ACCOUNT_RESERVE, ACCOUNT_SIZE, MAX_LABEL_CHARS), '/F1', ACCOUNT_SIZE,
                                  gray=True)
            font, size, gray = '/F1', ACCOUNT_SIZE, True
        else:
            bold = row.kind == 'sum'
            font, size, gray = ('/F2' if bold else '/F1'), ROW_SIZE, row.kind == 'memo'
            ops += pdf_table.cell(LEFT_X, y, pdf_table.fit(row.label, right - LABEL_GAP - LEFT_X, ROW_SIZE, MAX_LABEL_CHARS), font, size, gray=gray)
            if bold:
                lines.append(pdf_table.hline(y + SUM_LINE_RISE, LEFT_X, RIGHT_X))
        for index, text in enumerate(row.values):
            if text:
                ops += pdf_table.cell(column_edge(count, index), y, text, font, size, right=True, gray=gray)
        y -= ROW_H

    ops += pdf_table.cell(LEFT_X, FOOTER_Y, HINT_TEXT, '/F1', 7, gray=True)
    if total > 1:
        ops += pdf_table.cell(RIGHT_X, FOOTER_Y, f'Seite {number} von {total}', '/F1', 7, right=True)
    ops.append('ET')
    return ops + lines


def generate_liquidity_pdf(report: LiquidityReport, with_accounts: bool, company: str, created: datetime.date) -> bytes:
    """Das PDF eines Berichts (PDF/A-3b, A4 quer) als Bytes – nichts wird auf die Platte geschrieben. ``with_accounts`` druckt unter jeder Zeile ihre
    Konten; ``company`` (der erste eigene Kontakt, sonst leer) steht im Kopf und als Autor, ``created`` als „Erstellt am“."""
    blocks = _blocks(report, with_accounts)
    layout = page_layout([len(block) for block in blocks])
    rows = [row for block in blocks for row in block]
    pages, first = [], 0
    for number, heights in enumerate(layout, start=1):
        count = sum(heights)
        pages.append(_page_ops(report, rows[first:first + count], number, len(layout), company or '', created))
        first += count
    return build_multi_page_pdf(pages, page_size=PAGE_SIZE, title=TITLE, author=company or '')
