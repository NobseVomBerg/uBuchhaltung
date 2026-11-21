# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
PDF der Summen- und Saldenliste (A4 quer, mehrseitig).

Die Seite (``server/pages_susa.py``) und das PDF lesen denselben Bericht (``db.susa.SusaReport``) und dieselben Regeln für Spalten, Format und
Sichtbarkeit (``db.susa``); hier steht nur das Layout. Quer, weil acht Spalten – Konto, Bezeichnung und sechs Werte – nur so lesbar nebeneinander
stehen. Der Abgleich mit den Bankbewegungen steht nur auf der Seite.

Aufbau: Kopf (Titel, Firma, Zeitraum, Blockköpfe, Spaltenköpfe) und Fuß (Hinweis, „Seite n von m“) stehen auf jeder Seite. Eine Gruppe (Überschrift,
Konten, Summe) bildet einen Block, die Gesamtsumme einen eigenen, danach folgt ein Textblock mit den Hinweisen zu den Buchungen und den festen
Erläuterungen, umbrochen auf die ganze Breite. Ein Block wird nicht geteilt, solange er auf eine Seite passt (``pdf_table.page_layout``). Es wird
nichts auf die Platte geschrieben: ``generate_susa_pdf`` liefert die Bytes, wie das PDF der betriebswirtschaftlichen Auswertung.

Text steht immer als ``1 0 0 1 x y Tm (…) Tj``, gemessen mit den Breiten der eingebetteten Schrift (``pdf_table``) – nur so liegen die Beträge
rechtsbündig auf einer Kante.
"""
import datetime
from typing import NamedTuple

from db.euer import german_date
from db.susa import (COLUMN_ACCOUNT, COLUMN_LABELS, COLUMN_NAME, EUER_DIFF_TEXT, NOT_POSTED_TEXT, NOTICES_HEADING, OPENING_TEXT, SALDO_TEXT,
                     SUM_LABEL, SUSA_FOOTER, SUSA_HINT_TEXT, TAX_TEXT, TITLE, TOTAL_GROUP, TRANSFER_TEXT, SusaReport, block_headers, cells,
                     notice_text, visible_accounts, visible_groups)
from . import pdf_table
from .pdf_core import build_multi_page_pdf

PAGE_SIZE = (842, 595)             # A4 quer
LEFT_X = 36
RIGHT_X = 806
NAME_X = 80                        # die Bezeichnung beginnt hier, die Nummer steht bei LEFT_X
LABEL_RIGHT = 300                  # rechts davon beginnen die Wertspalten
COLUMN_COUNT = 6
COLUMN_WIDTH = (RIGHT_X - LABEL_RIGHT) / COLUMN_COUNT
NAME_RESERVE = 6                   # Bezeichnungen werden auf LABEL_RIGHT − NAME_RESERVE − NAME_X pt gekürzt
MAX_LABEL_CHARS = 200              # so viele Zeichen eines Namens werden höchstens betrachtet
MAX_TEXT_LINES = 40                # so viele Zeilen druckt ein einzelner Hinweis oder Erläuterungstext höchstens (danach „…“)

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
TEXT_WIDTH = RIGHT_X - LEFT_X


class Row(NamedTuple):
    """Eine Zeile der Tabelle: ``kind`` ist ``heading`` (Gruppenüberschrift), ``account``, ``virtual`` (ein Konto ohne Nummer), ``sum`` (auch die
    Gesamtsumme) oder ``text`` (eine Zeile des Textblocks, ``bold``: die Überschrift)."""
    kind: str
    number: str
    label: str
    values: list


def page_layout(block_heights) -> list[list[int]]:
    """Die Höhen (in Zeilen) der Blöcke auf die Seiten verteilen (``pdf_table.page_layout`` mit den Zeilen dieser Seite)."""
    return pdf_table.page_layout(block_heights, LINES_PER_PAGE)


def _column_edge(index: int) -> float:
    """Rechte Kante der Wertspalte ``index`` (0 bis 5)."""
    return LABEL_RIGHT + COLUMN_WIDTH * (index + 1)


def _text_rows(text: str, kind: str = 'text', prefix: str = '') -> list[Row]:
    """Ein Text, umbrochen auf die ganze Breite: eine Zeile je Umbruch (``pdf_table.wrap``), die erste mit ``prefix``."""
    lines = pdf_table.wrap(prefix + text, TEXT_WIDTH, ROW_SIZE, MAX_TEXT_LINES)
    return [Row(kind, '', line, []) for line in lines]


def _blocks(report: SusaReport) -> list[list[Row]]:
    """Die Blöcke der Tabelle: eine Gruppe mit ihren Konten und ihrer Summe, die Gesamtsumme und der Textblock."""
    empty = [''] * COLUMN_COUNT
    blocks = []
    for group in visible_groups(report):
        block = [Row('heading', '', group.group.label, empty)]
        for account in visible_accounts(group):
            block.append(Row('virtual' if account.virtual else 'account', '' if account.virtual else str(account.number), account.name,
                             cells(account, False)))
        block.append(Row('sum', '', SUM_LABEL.format(gruppe=group.group.label), cells(group, True)))
        blocks.append(block)
    blocks.append([Row('sum', '', TOTAL_GROUP.label, cells(report.total, True))])

    until = german_date(report.periods['month'][1])
    text: list[Row] = []
    if report.notices:
        text.append(Row('bold', '', NOTICES_HEADING.format(datum=until), []))
        for notice in report.notices:
            text += _text_rows(notice_text(notice), prefix='– ')
        text.append(Row('text', '', '', []))
    for paragraph in (SUSA_HINT_TEXT + ' ' + SALDO_TEXT, OPENING_TEXT, NOT_POSTED_TEXT, TRANSFER_TEXT, TAX_TEXT, EUER_DIFF_TEXT):
        text += _text_rows(paragraph)
    blocks.append(text)
    return blocks


def period_line(report: SusaReport) -> str:
    """Die Zeile unter dem Titel: der Monat und aufgelaufen, mit den Daten."""
    month_from, month_to = report.periods['month']
    year_from = report.periods['ytd'][0]
    return f'Zeitraum: {german_date(month_from)} bis {german_date(month_to)} · aufgelaufen seit {german_date(year_from)}'


def _page_ops(report: SusaReport, rows: list[Row], number: int, total: int, company: str, created: datetime.date) -> list[str]:
    ops = ['BT']
    ops += pdf_table.cell(LEFT_X, TITLE_Y, TITLE, '/F2', 14)
    if company:
        ops += pdf_table.cell(LEFT_X, COMPANY_Y, pdf_table.fit(company, RIGHT_X - LEFT_X, 10, MAX_LABEL_CHARS), '/F1', 10)
    ops += pdf_table.cell(LEFT_X, PERIOD_Y, period_line(report), '/F1', 9)
    ops += pdf_table.cell(RIGHT_X, PERIOD_Y, f'Erstellt am {created.strftime("%d.%m.%Y")}', '/F1', 9, right=True)

    first_block, second_block = block_headers(report.year, report.month)
    # Die Blocküberschrift steht mittig über ihren zwei Wertspalten (Soll und Haben)
    for block, heading in enumerate((first_block, second_block)):
        printed = pdf_table.clean(heading)
        middle = (_column_edge(2 * block) + _column_edge(2 * block + 2)) / 2
        ops += pdf_table.cell(middle - pdf_table.width(printed, 8, '/F2') / 2, BLOCK_HEADER_Y, printed, '/F2', 8)
    ops += pdf_table.cell(LEFT_X, COLUMN_HEADER_Y, COLUMN_ACCOUNT, '/F2', 7)
    ops += pdf_table.cell(NAME_X, COLUMN_HEADER_Y, COLUMN_NAME, '/F2', 7)
    for index, label in enumerate(COLUMN_LABELS):
        ops += pdf_table.cell(_column_edge(index), COLUMN_HEADER_Y, label, '/F2', 7, right=True)
    lines = [pdf_table.hline(HEADER_LINE_Y, LEFT_X, RIGHT_X)]

    y = TABLE_TOP
    for row in rows:
        if row.kind == 'text' or row.kind == 'bold':
            if row.label:
                ops += pdf_table.cell(LEFT_X, y, row.label, '/F2' if row.kind == 'bold' else '/F1', ROW_SIZE, gray=row.kind == 'text')
            y -= ROW_H
            continue
        if row.kind in ('account', 'virtual'):
            gray = row.kind == 'virtual'
            if row.number:
                ops += pdf_table.cell(LEFT_X, y, row.number, '/F1', ACCOUNT_SIZE)
            name = pdf_table.fit(row.label, LABEL_RIGHT - NAME_RESERVE - NAME_X, ACCOUNT_SIZE, MAX_LABEL_CHARS)
            if name:
                ops += pdf_table.cell(NAME_X, y, name, '/F1', ACCOUNT_SIZE, gray=gray)
            font, size = '/F1', ACCOUNT_SIZE
        else:
            ops += pdf_table.cell(LEFT_X, y, pdf_table.fit(row.label, LABEL_RIGHT - NAME_RESERVE - LEFT_X, ROW_SIZE, MAX_LABEL_CHARS), '/F2', ROW_SIZE)
            font, size = '/F2', ROW_SIZE
            if row.kind == 'sum':
                lines.append(pdf_table.hline(y + SUM_LINE_RISE, LEFT_X, RIGHT_X))
        for index, text in enumerate(row.values):
            if text:
                ops += pdf_table.cell(_column_edge(index), y, text, font, size, right=True)
        y -= ROW_H

    ops += pdf_table.cell(LEFT_X, FOOTER_Y, SUSA_FOOTER, '/F1', 7, gray=True)
    if total > 1:
        ops += pdf_table.cell(RIGHT_X, FOOTER_Y, f'Seite {number} von {total}', '/F1', 7, right=True)
    ops.append('ET')
    return ops + lines


def generate_susa_pdf(report: SusaReport, company: str, created: datetime.date) -> bytes:
    """Das PDF einer Summen- und Saldenliste (PDF/A-3b, A4 quer) als Bytes – nichts wird auf die Platte geschrieben. ``company`` (der erste eigene
    Kontakt, sonst leer) steht im Kopf und als Autor, ``created`` als „Erstellt am“."""
    blocks = _blocks(report)
    layout = page_layout([len(block) for block in blocks])
    rows = [row for block in blocks for row in block]
    pages, first = [], 0
    for number, heights in enumerate(layout, start=1):
        count = sum(heights)
        pages.append(_page_ops(report, rows[first:first + count], number, len(layout), company or '', created))
        first += count
    return build_multi_page_pdf(pages, page_size=PAGE_SIZE, title=TITLE, author=company or '')
