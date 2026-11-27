# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
PDF der Umsatzsteuer-Voranmeldung (A4 hoch, mehrseitig).

Die Seite (``server/pages_ustva.py``) und das PDF lesen denselben Bericht (``db.ustva.UstvaReport``); hier steht nur das Layout. Kein amtliches Formular: Die
Zeilen sind die Kennzahlen 81, 86, 66 und 83 mit Bemessungsgrundlage, vollen Euro und Steuer, darunter die Abstimmung mit der EÜR und alles, was die Seite
keiner Kennzahl sicher zuordnet („Nicht zugeordnet“); hinter der Tabelle stehen die festen Erläuterungen.

Aufbau: Kopf (Titel, Firma, Zeitraum, Spaltenköpfe) und Fuß (zwei Hinweiszeilen, „Seite n von m“) stehen auf jeder Seite. Eine Kennzahl mit ihren Konten bildet
einen Block, ebenso die Abstimmung und „Nicht zugeordnet“; ein Block wird nicht geteilt, solange er auf eine Seite passt (``pdf_table.page_layout``). Es wird
nichts auf die Platte geschrieben: ``generate_ustva_pdf`` liefert die Bytes, wie das PDF der EÜR.

Text steht immer als ``1 0 0 1 x y Tm (…) Tj``, gemessen mit den Breiten der eingebetteten Schrift (``pdf_table``) – nur so liegen die Beträge rechtsbündig auf
einer Kante.
"""
import datetime
from typing import NamedTuple

from db.bwa import format_amount
from db.euer import german_date
from db.ustva import (COLUMN_BASE, COLUMN_KZ, COLUMN_LABEL, COLUMN_TAX, COLUMN_WHOLE, LABEL_83, LABEL_VAT_TOTAL, RECON_COLUMNS, RECON_HEADING,
                      UNASSIGNED_HEADING, UNASSIGNED_NONE_TEXT, UNASSIGNED_SUM_LABEL, UNBOOKED_LABEL, USTVA_DATE_TEXT, USTVA_FOOTER, USTVA_NOT_SHOWN_TEXT,
                      USTVA_PERIOD_LINE, USTVA_RECON_TEXT, USTVA_ROUNDING_TEXT, USTVA_SIGN_TEXT, USTVA_TITLE, AccountLine, Figure, UstvaReport,
                      account_label, format_whole, period_label)
from . import pdf_table
from .pdf_core import build_multi_page_pdf

PAGE_SIZE = (595, 842)             # A4 hoch
LEFT_X = 50
KZ_X = 50                          # die Kennzahl steht hier, die Bezeichnung bei NAME_X
NAME_X = 78
BASE_RIGHT = 405                   # rechte Kante der Bemessungsgrundlage (unter „Nicht zugeordnet“: Netto)
WHOLE_RIGHT = 470                  # rechte Kante der vollen Euro
TAX_RIGHT = 545                    # rechte Kante der Steuer
RIGHT_X = TAX_RIGHT
ACCOUNT_INDENT = 8                 # Konten sind eingerückt
NAME_WIDTH = BASE_RIGHT - 80 - NAME_X          # Bezeichnungen werden auf diese Breite gekürzt (Konten) bzw. umbrochen (Gründe)
MAX_NAME_CHARS = 200               # so viele Zeichen eines Namens werden höchstens betrachtet
MAX_TEXT_LINES = 40                # so viele Zeilen druckt ein einzelner Erläuterungstext höchstens (danach „…“)
TEXT_SIZE = 7

TITLE_Y = 800
COMPANY_Y = 784
PERIOD_Y = 771
HEADER_Y = 750                     # Spaltenköpfe
HEADER_LINE_Y = 746
TABLE_TOP = 732                    # Y der ersten Zeile
ROW_H = 12
ROW_SIZE = 8
ACCOUNT_SIZE = 7
BOTTOM_Y = 64                      # keine Zeile darunter
FOOTER_TOP_Y = 44                  # erste Hinweiszeile
FOOTER_Y = 35                      # zweite Hinweiszeile und „Seite n von m“
SUM_LINE_RISE = 9                  # die Linie über einer Summenzeile liegt so weit über deren Grundlinie
LINES_PER_PAGE = int((TABLE_TOP - BOTTOM_Y) // ROW_H) + 1


class Row(NamedTuple):
    """Eine Zeile der Tabelle: ``kind`` ist ``heading``, ``figure`` (eine Kennzahl oder ein Grund), ``cont`` (weitere Zeile einer umbrochenen Bezeichnung),
    ``account``, ``sum`` oder ``text`` (Erläuterung, grau, über die ganze Breite); ``kz``, ``base``, ``whole`` und ``tax`` bleiben leer, wo es sie nicht gibt."""
    kind: str
    kz: str
    label: str
    base: str
    whole: str
    tax: str


def page_layout(block_heights) -> list[list[int]]:
    """Die Höhen (in Zeilen) der Blöcke auf die Seiten verteilen (``pdf_table.page_layout`` mit den Zeilen dieser Seite)."""
    return pdf_table.page_layout(block_heights, LINES_PER_PAGE)


def _account_rows(lines: tuple[AccountLine, ...], with_base: bool, with_count: bool = False) -> list[Row]:
    rows = []
    for line in lines:
        label = f'{account_label(line)} ({line.count})' if with_count else account_label(line)
        rows.append(Row('account', '', label, format_amount(line.base) if with_base else '', '', format_amount(line.tax)))
    return rows


def _figure_block(figure: Figure, with_base: bool) -> list[Row]:
    """Eine Kennzahl und ihre Konten; ohne ``with_base`` (Kz 66) steht nur die Steuer da."""
    head = Row('figure', figure.kz, figure.label, format_amount(figure.base) if with_base else '', format_whole(figure.base) if with_base else '',
               format_amount(figure.tax))
    return [head] + _account_rows(figure.accounts, with_base)


def _text_rows(text: str) -> list[Row]:
    """Ein Text, umbrochen auf die ganze Breite: eine Zeile je Umbruch (``pdf_table.wrap``)."""
    return [Row('text', '', line, '', '', '') for line in pdf_table.wrap(text, RIGHT_X - LEFT_X, TEXT_SIZE, MAX_TEXT_LINES)]


def _group_rows(label: str, base: str, whole: str, tax: str) -> list[Row]:
    """Eine Zeile mit einer Bezeichnung, die auf mehrere Zeilen laufen darf (sie kürzen hieße, die Anzahl am Ende zu verlieren)."""
    parts = pdf_table.wrap(label, NAME_WIDTH, ROW_SIZE) or ['']
    return [Row('figure', '', parts[0], base, whole, tax)] + [Row('cont', '', part, '', '', '') for part in parts[1:]]


def _blocks(report: UstvaReport) -> list[list[Row]]:
    """Die Blöcke der Tabelle: 81 und 86 mit der Umsatzsteuer, 66 mit der Vorauszahlung (Kz 83), die Abstimmung, „Nicht zugeordnet“ und die Erläuterungen."""
    figure_81, figure_86 = report.figures
    first = _figure_block(figure_81, True) + _figure_block(figure_86, True) + [Row('sum', '', LABEL_VAT_TOTAL, '', '', format_amount(report.vat_total))]
    second = _figure_block(report.input_tax, False) + [Row('sum', '83', LABEL_83, '', '', format_amount(report.prepayment))]
    recon = [Row('heading', '', RECON_HEADING, '', '', ''),
             Row('figure', '', RECON_COLUMNS[0], '', '', format_amount(report.recon_page)),
             Row('figure', '', RECON_COLUMNS[1], '', '', format_amount(report.recon_euer)),
             Row('sum', '', RECON_COLUMNS[2], '', '', format_amount(report.recon_difference))]
    unassigned = [Row('heading', '', UNASSIGNED_HEADING, '', '', '')]
    for group in report.unassigned:
        unassigned += _group_rows(f'{group.label} ({group.count})', format_amount(group.net), '', format_amount(group.tax))
        unassigned += _account_rows(group.accounts, True, True)
    if report.unbooked_count:
        unassigned += _group_rows(f'{UNBOOKED_LABEL} ({report.unbooked_count})', format_amount(report.unbooked_amount), '', '')
    if not report.unassigned and not report.unbooked_count:
        unassigned.append(Row('figure', '', UNASSIGNED_NONE_TEXT, '', '', ''))
    unassigned.append(Row('sum', '', UNASSIGNED_SUM_LABEL, format_amount(report.unassigned_net), '', format_amount(report.unassigned_tax)))
    notes = [Row('text', '', '', '', '', '')]
    for text in (USTVA_DATE_TEXT, USTVA_ROUNDING_TEXT, USTVA_NOT_SHOWN_TEXT, USTVA_SIGN_TEXT, USTVA_RECON_TEXT):
        notes += _text_rows(text)
    return [first, second, recon, unassigned, notes]


def _page_ops(rows: list[Row], number: int, total: int, report: UstvaReport, company: str, created: datetime.date) -> list[str]:
    ops = ['BT']
    ops += pdf_table.cell(LEFT_X, TITLE_Y, USTVA_TITLE, '/F2', 14)
    if company:
        ops += pdf_table.cell(LEFT_X, COMPANY_Y, pdf_table.fit(company, RIGHT_X - LEFT_X, 10, MAX_NAME_CHARS), '/F1', 10)
    period = USTVA_PERIOD_LINE.format(von=german_date(report.date_from), bis=german_date(report.date_to),
                                      label=period_label(report.year, report.month, report.kind))
    ops += pdf_table.cell(LEFT_X, PERIOD_Y, period, '/F1', 9)
    ops += pdf_table.cell(RIGHT_X, PERIOD_Y, f'Erstellt am {created.strftime("%d.%m.%Y")}', '/F1', 9, right=True)
    ops += pdf_table.cell(KZ_X, HEADER_Y, COLUMN_KZ, '/F2', ROW_SIZE)
    ops += pdf_table.cell(NAME_X, HEADER_Y, COLUMN_LABEL, '/F2', ROW_SIZE)
    ops += pdf_table.cell(BASE_RIGHT, HEADER_Y, COLUMN_BASE, '/F2', ROW_SIZE, right=True)
    ops += pdf_table.cell(WHOLE_RIGHT, HEADER_Y, COLUMN_WHOLE, '/F2', ROW_SIZE, right=True)
    ops += pdf_table.cell(TAX_RIGHT, HEADER_Y, COLUMN_TAX, '/F2', ROW_SIZE, right=True)
    lines = [pdf_table.hline(HEADER_LINE_Y, LEFT_X, RIGHT_X)]

    y = TABLE_TOP
    for row in rows:
        if row.kind == 'text':
            if row.label:
                ops += pdf_table.cell(LEFT_X, y, row.label, '/F1', TEXT_SIZE, gray=True)
            y -= ROW_H
            continue
        if row.kind == 'heading':
            ops += pdf_table.cell(KZ_X, y, row.label, '/F2', ROW_SIZE)
            y -= ROW_H
            continue
        if row.kind == 'account':
            font, size, gray, x = '/F1', ACCOUNT_SIZE, True, NAME_X + ACCOUNT_INDENT
            label = pdf_table.fit(row.label, NAME_WIDTH - ACCOUNT_INDENT, size, MAX_NAME_CHARS)
        else:
            bold = row.kind == 'sum'
            font, size, gray, x = ('/F2' if bold else '/F1'), ROW_SIZE, False, NAME_X
            label = row.label if row.kind == 'cont' else pdf_table.fit(row.label, NAME_WIDTH, size, MAX_NAME_CHARS)
            if row.kz:
                ops += pdf_table.cell(KZ_X, y, row.kz, font, size)
            if bold:
                lines.append(pdf_table.hline(y + SUM_LINE_RISE, LEFT_X, RIGHT_X))
        if label:                                                  # ein Konto ohne Namen druckt nur Nummer und Beträge
            ops += pdf_table.cell(x, y, label, font, size, gray=gray)
        for right, text in ((BASE_RIGHT, row.base), (WHOLE_RIGHT, row.whole), (TAX_RIGHT, row.tax)):
            if text:
                ops += pdf_table.cell(right, y, text, font, size, right=True, gray=gray)
        y -= ROW_H

    ops += pdf_table.cell(LEFT_X, FOOTER_TOP_Y, USTVA_FOOTER[0], '/F1', 7, gray=True)
    ops += pdf_table.cell(LEFT_X, FOOTER_Y, USTVA_FOOTER[1], '/F1', 7, gray=True)
    if total > 1:
        ops += pdf_table.cell(RIGHT_X, FOOTER_Y, f'Seite {number} von {total}', '/F1', 7, right=True)
    ops.append('ET')
    return ops + lines


def generate_ustva_pdf(report: UstvaReport, company: str, created: datetime.date) -> bytes:
    """Das PDF einer Voranmeldung (PDF/A-3b, A4 hoch) als Bytes – nichts wird auf die Platte geschrieben. ``company`` (der erste eigene Kontakt, sonst leer)
    steht im Kopf und als Autor, ``created`` als „Erstellt am“."""
    blocks = _blocks(report)
    layout = page_layout([len(block) for block in blocks])
    rows = [row for block in blocks for row in block]
    pages, first = [], 0
    for number, heights in enumerate(layout, start=1):
        count = sum(heights)
        pages.append(_page_ops(rows[first:first + count], number, len(layout), report, company or '', created))
        first += count
    return build_multi_page_pdf(pages, page_size=PAGE_SIZE, title=USTVA_TITLE, author=company or '')
