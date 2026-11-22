# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
CSV der betriebswirtschaftlichen Auswertung (``GET /bwa/csv``).

Die Seite (``server/pages_bwa.py``), das PDF und die CSV lesen denselben Bericht (``db.bwa.BwaReport``) und dieselben Regeln für Spalten, Anteile, Format und
Sichtbarkeit (``db.bwa``); hier steht nur die Form der Datei: eine Kopfzeile, dann je sichtbarer Zeile eine Datenzeile – auf Wunsch mit ihren Konten direkt
darunter. Es wird nichts gerechnet und nichts gerundet: Jede Zelle ist der Anzeige-Text der Seite (``db.bwa.cells``) ohne Tausenderpunkte und ohne
Prozentzeichen (``csv_table``). Der Zeitraum steht im Dateinamen (``db.bwa.bwa_filename``) und in den Köpfen der Wertspalten, Titel-, Leer- und Hinweiszeilen
gibt es nicht – eine Tabellenkalkulation soll die Datei als Tabelle lesen.

Texte aus der Datenbank (die Namen der Konten) gehen nur über ``csv_table.text_cell`` (Schutz vor Formeln). Es wird nichts auf die Platte geschrieben:
``generate_bwa_csv`` liefert die Bytes.
"""
from db.bwa import PERCENT_BASE, BwaReport, block_headers, cells, column_labels, columns, visible, visible_accounts
from . import csv_table

PERCENT_KINDS = frozenset({*PERCENT_BASE, 'delta_pct'})            # die Spalten mit einem Prozentwert (Anteile und Veränderung in %)
LINE_KINDS = {'group': 'Gruppe', 'sum': 'Summe', 'result': 'Ergebnis'}
OUTSIDE_KIND = 'Nicht im Ergebnis'                                  # die Zeile „Sonstige Ausgaben“ (``report.outside``)
ACCOUNT_KIND = 'Konto'


def _header(report: BwaReport, kinds: list[str]) -> list[str]:
    """Die Kopfzeile: ``Art``, ``Konto``, ``Bezeichnung`` und die acht Wertspalten als ``<Blocküberschrift>: <Spaltenbezeichnung>`` – der erste Block gilt für
    die erste Hälfte der Spalten, der zweite für die zweite. Eine Prozentspalte, deren Bezeichnung kein ``%`` trägt, bekommt ein `` in %``."""
    first_block, second_block = block_headers(report)
    heads = []
    for index, (kind, label) in enumerate(zip(kinds, column_labels(report))):
        if kind in PERCENT_KINDS and '%' not in label:
            label += ' in %'
        heads.append(f'{first_block if index < len(kinds) // 2 else second_block}: {label}')
    return ['Art', 'Konto', 'Bezeichnung', *heads]


def _values(report: BwaReport, line, values: dict[str, int], kinds: list[str]) -> list[str]:
    """Die acht Wertzellen einer Zeile oder eines Kontos: die Zellen der Seite (``db.bwa.cells``), Beträge ohne Tausenderpunkt, Prozentwerte als Zahl."""
    return [csv_table.percent_cell(text) if kind in PERCENT_KINDS else csv_table.amount_cell(text)
            for kind, text in zip(kinds, cells(report, line, values))]


def bwa_csv_rows(report: BwaReport, with_accounts: bool) -> list[list[str]]:
    """Die Zeilen der CSV: die Kopfzeile, dann je sichtbarer Zeile des Berichts (``db.bwa.visible``, in der Reihenfolge von ``report.lines``, zuletzt
    ``report.outside``) eine Datenzeile mit ``Art`` ``Gruppe``, ``Summe``, ``Ergebnis`` bzw. ``Nicht im Ergebnis``. Mit ``with_accounts`` folgen unter jeder
    Zeile ihre Konten (``db.bwa.visible_accounts``) mit ``Art`` ``Konto``, der Nummer in der Spalte ``Konto`` und dem Namen als Bezeichnung."""
    kinds = [column.kind for column in columns(report)]
    rows = [_header(report, kinds)]
    for line in (*report.lines, report.outside):
        if not visible(report, line):
            continue
        kind = OUTSIDE_KIND if line is report.outside else LINE_KINDS[line.kind]
        rows.append([kind, '', csv_table.text_cell(line.label), *_values(report, line, line.values, kinds)])
        if with_accounts:
            for account in visible_accounts(report, line):
                rows.append([ACCOUNT_KIND, csv_table.text_cell(account.number), csv_table.text_cell(account.name), *_values(report, line, account.values, kinds)])
    return rows


def generate_bwa_csv(report: BwaReport, with_accounts: bool) -> bytes:
    """Die CSV eines Berichts als Bytes (UTF-8 mit BOM, Semikolon, Dezimalkomma) – nichts wird auf die Platte geschrieben."""
    return csv_table.csv_bytes(bwa_csv_rows(report, with_accounts))
