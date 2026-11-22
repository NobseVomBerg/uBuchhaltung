# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
CSV der Summen- und Saldenliste (``GET /susa/csv``).

Die Seite (``server/pages_susa.py``), das PDF und die CSV lesen denselben Bericht (``db.susa.SusaReport``) und dieselben Regeln für Sichtbarkeit und Format
(``db.susa``); hier steht nur die Form der Datei: eine Kopfzeile, je sichtbarer Gruppe ihre sichtbaren Konten und eine Summenzeile, am Ende die Gesamtsumme.
Es wird nichts gerechnet und nichts gerundet: Jeder Betrag ist der Anzeige-Text der Seite (``db.bwa.format_amount``) ohne Tausenderpunkte (``csv_table``).

Anders als die Seite, die Nullbeträge einer Kontozeile leer lässt, steht jeder Wert als Zahl da, auch null (``0,00``) – eine Tabellenkalkulation soll eine Spalte
ohne Lücken summieren können. Ein Saldo (``opening``, ``closing``; intern Soll positiv) steht als Betrag ohne Vorzeichen und daneben als Kennzeichen ``S`` oder
``H`` in einer eigenen Spalte; rundet der Betrag auf ``0,00``, bleibt das Kennzeichen leer – wie ``db.susa.format_balance``. Die Hinweise zu den Buchungen und der
Abgleich mit den Bankbewegungen stehen nur auf der Seite und im PDF.

Texte aus der Datenbank (die Namen der Konten) gehen nur über ``csv_table.text_cell`` (Schutz vor Formeln). Es wird nichts auf die Platte geschrieben:
``generate_susa_csv`` liefert die Bytes.
"""
from db.bwa import format_amount
from db.susa import SUM_LABEL, TOTAL_GROUP, SusaReport, block_headers, visible_accounts, visible_groups
from . import csv_table

ACCOUNT_KIND, SUM_KIND, TOTAL_KIND = 'Konto', 'Summe', 'Gesamt'


def _header(report: SusaReport) -> list[str]:
    """Die zwölf Köpfe: vier Textspalten, Anfangssaldo mit Kennzeichen, Soll und Haben im Monat und aufgelaufen (mit den Blocküberschriften der Seite), Endsaldo
    mit Kennzeichen."""
    month, accumulated = block_headers(report.year, report.month)
    return ['Art', 'Gruppe', 'Konto', 'Bezeichnung', 'Anfangssaldo', 'Anfangssaldo S/H', f'Soll {month}', f'Haben {month}',
            f'Soll {accumulated}', f'Haben {accumulated}', 'Endsaldo', 'Endsaldo S/H']


def _amount(minor: int) -> str:
    return csv_table.amount_cell(format_amount(minor))


def _balance(minor: int) -> list[str]:
    """Ein Saldo: der Betrag ohne Vorzeichen und das Kennzeichen ``S`` (Soll, positiv) oder ``H`` (Haben, negativ) – leer, wenn der Betrag auf ``0,00`` rundet."""
    amount = _amount(abs(minor))
    return [amount, '' if amount == '0,00' else 'S' if minor > 0 else 'H']


def _values(row) -> list[str]:
    """Die acht Wertzellen eines Kontos oder einer Gruppe: Anfangssaldo (mit Kennzeichen), Soll und Haben im Monat, Soll und Haben aufgelaufen, Endsaldo (mit
    Kennzeichen)."""
    return [*_balance(row.opening), _amount(row.month_soll), _amount(row.month_haben), _amount(row.ytd_soll), _amount(row.ytd_haben), *_balance(row.closing)]


def susa_csv_rows(report: SusaReport) -> list[list[str]]:
    """Die Zeilen der CSV: die Kopfzeile, dann je sichtbarer Gruppe (``db.susa.visible_groups``) ihre sichtbaren Konten (``Art`` ``Konto``, die Nummer – bei einem
    virtuellen Konto leer – und der Name) und eine Zeile ``Summe``; zuletzt eine Zeile ``Gesamt`` aus ``report.total``."""
    rows = [_header(report)]
    for group in visible_groups(report):
        label = csv_table.text_cell(group.group.label)
        for account in visible_accounts(group):
            number = '' if account.number is None else str(account.number)
            rows.append([ACCOUNT_KIND, label, number, csv_table.text_cell(account.name), *_values(account)])
        rows.append([SUM_KIND, label, '', csv_table.text_cell(SUM_LABEL.format(gruppe=group.group.label)), *_values(group)])
    rows.append([TOTAL_KIND, '', '', csv_table.text_cell(TOTAL_GROUP.label), *_values(report.total)])
    return rows


def generate_susa_csv(report: SusaReport) -> bytes:
    """Die CSV eines Berichts als Bytes (UTF-8 mit BOM, Semikolon, Dezimalkomma) – nichts wird auf die Platte geschrieben."""
    return csv_table.csv_bytes(susa_csv_rows(report))
