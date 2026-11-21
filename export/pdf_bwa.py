# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
PDF der betriebswirtschaftlichen Auswertung (A4 quer, mehrseitig).

Die Seite (``server/pages_bwa.py``) und das PDF lesen denselben Bericht (``db.bwa.BwaReport``) und dieselben Regeln für
Spalten, Anteile, Format und Sichtbarkeit (``db.bwa``); hier steht nur das Layout. Quer, weil neun Spalten – die Bezeichnung und
je Block vier Werte – nur so lesbar nebeneinander stehen.

Aufbau: Kopf (Titel, Firma, Zeitraum, Blockköpfe, Spaltenköpfe) und Fuß (Hinweis, „Seite n von m“) stehen auf jeder Seite. Eine
Zeile und ihre Konten bilden einen Block; ein Block wird nicht geteilt, solange er auf eine Seite passt (``page_layout``). Es wird
nichts auf die Platte geschrieben: ``generate_bwa_pdf`` liefert die Bytes, wie der Stundenzettel mit ``store=False``.

Text steht immer als ``1 0 0 1 x y Tm (…) Tj``, gemessen mit den Breiten der eingebetteten Schrift (``info_width``, fetter Text
mit denen der Helvetica-Bold-Tabelle) – nur so liegen die Beträge rechtsbündig auf einer Kante. Die Bausteine dafür (Text bereinigen,
kürzen, Zelle, Linie, Seitenverteilung) stehen in ``pdf_table`` und gelten für alle Tabellen-PDFs; die Funktionen mit Unterstrich
hier sind dünne Hüllen darum mit der Geometrie dieser Seite.
"""
import datetime
from typing import NamedTuple

from db.bwa import (HINT_TEXT, OUTSIDE_HEADING, TITLE, BwaReport, block_headers, cells, column_labels, period_line,
                    visible, visible_accounts)
from . import pdf_table
from .pdf_core import build_multi_page_pdf

PAGE_SIZE = (842, 595)             # A4 quer
LEFT_X = 36
RIGHT_X = 806
LABEL_RIGHT = 290                  # rechts davon beginnen die Wertspalten
COLUMN_COUNT = 8
COLUMN_WIDTH = (RIGHT_X - LABEL_RIGHT) / COLUMN_COUNT
ACCOUNT_X = 48                     # Konten sind eingerückt
ACCOUNT_RESERVE = 52               # Konten werden auf LABEL_RIGHT − ACCOUNT_RESERVE pt gekürzt
MAX_LABEL_CHARS = 200              # so viele Zeichen eines Namens werden höchstens betrachtet

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
    """Eine Zeile der Tabelle: ``kind`` ist ``group``, ``sum`` (auch das Ergebnis), ``account`` oder ``heading``."""
    kind: str
    label: str
    values: list


def page_layout(block_heights) -> list[list[int]]:
    """Die Höhen (in Zeilen) der Blöcke auf die Seiten verteilen: je Seite die Zeilenzahlen der Blockstücke, die auf ihr stehen
    (mindestens eine Seite). Ein Block wird nicht geteilt, solange er auf eine Seite passt; passt er auf die Restseite nicht
    mehr, beginnt er auf der nächsten. Ein Block, der länger ist als eine Seite, beginnt auf einer neuen Seite und läuft dann
    über so viele, wie er braucht. Keine Zeile steht unter ``BOTTOM_Y``."""
    return pdf_table.page_layout(block_heights, LINES_PER_PAGE)


def _clean(text) -> str:
    """Ein Text, wie er im PDF steht, ohne Zeilenumbrüche, Tabulatoren und andere Steuerzeichen (``pdf_table.clean``)."""
    return pdf_table.clean(text)


def _width(text: str, size: float, font: str) -> float:
    return pdf_table.width(text, size, font)


def _fit(text, max_width: float, size: float) -> str:
    """Der Text auf ``max_width`` pt gekürzt (mit „…“); betrachtet werden höchstens ``MAX_LABEL_CHARS`` Zeichen (``pdf_table.fit``)."""
    return pdf_table.fit(text, max_width, size, MAX_LABEL_CHARS)


def _cell(x: float, y: float, text, font: str = '/F1', size: float = ROW_SIZE, right: bool = False, gray: bool = False) -> list[str]:
    """Eine Textzelle an absoluter Position; ``right``: ``x`` ist die rechte Kante des Textes (``pdf_table.cell``)."""
    return pdf_table.cell(x, y, text, font, size, right, gray)


def _column_edge(index: int) -> float:
    """Rechte Kante der Wertspalte ``index`` (0 bis 7)."""
    return LABEL_RIGHT + COLUMN_WIDTH * (index + 1)


def _line(y: float) -> str:
    """Eine dünne Linie über die ganze Breite – wird nach dem Textobjekt gezeichnet."""
    return pdf_table.hline(y, LEFT_X, RIGHT_X)


def _rows(report: BwaReport, with_accounts: bool) -> list[list[Row]]:
    """Die Blöcke der Tabelle: eine Zeile mit ihren Konten (auf Wunsch); der Block „nicht im Ergebnis“ beginnt mit seiner Überschrift."""
    blocks = []
    for line in report.lines + [report.outside]:
        if not visible(report, line):
            continue
        kind = 'group' if line.kind == 'group' else 'sum'
        block = [Row('heading', OUTSIDE_HEADING, [''] * COLUMN_COUNT)] if line is report.outside else []
        block.append(Row(kind, line.label, cells(report, line, line.values)))
        if with_accounts:
            for account in visible_accounts(report, line):
                label = f'{account.number} {account.name}' if account.name else str(account.number)
                block.append(Row('account', label, cells(report, line, account.values)))
        blocks.append(block)
    return blocks


def _page_ops(report: BwaReport, rows: list[Row], number: int, total: int, company: str,
              created: datetime.date) -> list[str]:
    ops = ['BT']
    ops += _cell(LEFT_X, TITLE_Y, TITLE, font='/F2', size=14)
    if company:
        ops += _cell(LEFT_X, COMPANY_Y, _fit(company, RIGHT_X - LEFT_X, 10), size=10)
    ops += _cell(LEFT_X, PERIOD_Y, period_line(report), size=9)
    ops += _cell(RIGHT_X, PERIOD_Y, f'Erstellt am {created.strftime("%d.%m.%Y")}', size=9, right=True)

    first_block, second_block = block_headers(report)
    # Die Blocküberschrift steht mittig über ihren vier Wertspalten (rechtsbündig stünde sie nur über der letzten)
    for block, heading in enumerate((first_block, second_block)):
        printed = _clean(heading)
        middle = (_column_edge(4 * block - 1) + _column_edge(4 * block + 3)) / 2
        ops += _cell(middle - _width(printed, 8, '/F2') / 2, BLOCK_HEADER_Y, printed, font='/F2', size=8)
    ops += _cell(LEFT_X, COLUMN_HEADER_Y, 'Position', font='/F2', size=7)
    for index, label in enumerate(column_labels(report, short=True)):
        ops += _cell(_column_edge(index), COLUMN_HEADER_Y, label, font='/F2', size=7, right=True)
    lines = [_line(HEADER_LINE_Y)]

    y = TABLE_TOP
    for row in rows:
        if row.kind == 'account':
            ops += _cell(ACCOUNT_X, y, _fit(row.label, LABEL_RIGHT - ACCOUNT_RESERVE, ACCOUNT_SIZE), size=ACCOUNT_SIZE, gray=True)
            font, size, gray = '/F1', ACCOUNT_SIZE, True
        else:
            bold = row.kind in ('sum', 'heading')
            font, size, gray = ('/F2' if bold else '/F1'), ROW_SIZE, False
            ops += _cell(LEFT_X, y, row.label, font=font, size=size)
            if row.kind == 'sum':
                lines.append(_line(y + SUM_LINE_RISE))
        for index, text in enumerate(row.values):
            if text:
                ops += _cell(_column_edge(index), y, text, font=font, size=size, right=True, gray=gray)
        y -= ROW_H

    ops += _cell(LEFT_X, FOOTER_Y, HINT_TEXT, size=7, gray=True)
    if total > 1:
        ops += _cell(RIGHT_X, FOOTER_Y, f'Seite {number} von {total}', size=7, right=True)
    ops.append('ET')
    return ops + lines


def generate_bwa_pdf(report: BwaReport, with_accounts: bool, company: str, created: datetime.date) -> bytes:
    """Das PDF eines Berichts (PDF/A-3b, A4 quer) als Bytes – nichts wird auf die Platte geschrieben. ``with_accounts`` druckt unter jeder
    Zeile ihre Konten; ``company`` (der erste eigene Kontakt, sonst leer) steht im Kopf und als Autor, ``created`` als „Erstellt am“."""
    blocks = _rows(report, with_accounts)
    layout = page_layout([len(block) for block in blocks])
    rows = [row for block in blocks for row in block]
    pages, first = [], 0
    for number, heights in enumerate(layout, start=1):
        count = sum(heights)
        pages.append(_page_ops(report, rows[first:first + count], number, len(layout), company or '', created))
        first += count
    return build_multi_page_pdf(pages, page_size=PAGE_SIZE, title=TITLE, author=company or '')
