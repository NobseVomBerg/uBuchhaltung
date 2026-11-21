# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Liquiditätsrechnung (2.6.3): vom Ergebnis der betriebswirtschaftlichen Auswertung zur Veränderung der flüssigen Mittel.

Vier Teile. Der reine Teil (``db/liquidity.py``) prüft den Aufbau aus erfundenen EÜR-Zeilen: Jede Zeile steht an genau einer Stelle (eine Nummer je Grenze der
Bereiche), Ergebnis, Abschreibungen und Darlehen im Ergebnis sind die der BWA, die Überleitung und „Nicht zugeordnet“ sind aus ihren Bestandteilen gerechnet
(auch über Zufall mit festem Seed), Spalten, Sichtbarkeit, Texte. Der zweite Teil ist die **Abstimmung** an echten Beständen – die Zusage, auf der die Rechnung
steht: Sie hat keine eigene Lesart der Buchungen und rechnet nur mit den Zeilen von ``get_euer_data``, dem Bericht der BWA und den Töpfen des Dashboards.
„Nicht zugeordnet“ (Ist − Überleitung − Bankbewegungen ohne Buchungssatz) ist im Demobestand und in Beständen aus Zufall über Wege der App in jedem Monat und
aufgelaufen exakt 0 Minor Units; in der SuSa-Welt und der Testwelt stehen feste Werte (der Maßstab, an dem sich nichts verschieben darf), und für jeden
benannten Fall ist festgehalten, was in „Nicht zugeordnet“ landet und warum. Der dritte Teil prüft Seite und Adresse (strenges Lesen, Maskieren, nichts
schreiben, Menü, Diagnose-MCP), der PDF-Teil steht in ``test_liquidity_pdf.py``.

Seit 2.6.4 rechnet die Überleitung nur zurück, was die EÜR als Umbuchung zwischen Anlage- und Erfolgskonto liest (``get_euer_data(…, only_asset_transfers=True)``,
die Sätze von Sektion 3): Eine Abschreibung, die die Bank bezahlt hat („6260 an Bank“), bleibt im Ergebnis. Der reine Teil bekommt dafür die Zeilen der
Umbuchungen (``transfer_rows``) zusätzlich zu den EÜR-Zeilen – eine EÜR-Zeile allein füllt weder „Abschreibungen“ noch „Anlagenabgänge“ –, und ein eigener Teil
prüft ``get_euer_data`` mit diesem Schalter.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie als Konstante da (die Adresse bekommt es als Argument oder der Test liest es frisch).
"""
import datetime
import html as html_lib
import importlib.util
import json
import os
import random
import re
import sqlite3
import sys
from contextlib import closing
from decimal import Decimal

import pytest

import auth
import demo_seed
import userctx
from db import Database, bwa
from db import liquidity as liq
from db.bwa import block_headers, default_month, format_amount, period_line, periods_for
from db.reporting import euer_section
from db.liquidity import (ACCOUNT_KEYS, BALANCED_TEXT, BWA_LINES, HINT_TEXT, LAYOUT, METHOD_TEXT, ROUNDING_TEXT, SIGN_TEXT, SUM, TRANSFER_TEXT,
                          UNASSIGNED_TEXT, UNBALANCED_TEXT, VARIANT_LABELS, CashFigures, account_label, balanced, block_size, cells, column_labels,
                          columns, line_label, liquidity_filename, rounding_remainder, visible, visible_accounts)
from money import to_minor
from server.pages import Header1
from server.pages_bwa import INVALID_VARIANT_ERROR, BwaQuery, reports_submenu
from server.pages_liquidity import LIQUIDITY_JS, PageLiquidity
from tests import entry_form_world
from tests import liquidity_helpers as helpers
from tests.bwa_helpers import book
from tests.dom_helpers import Dom, assert_same_structure
from tests.http_client import get
from tests.installation import dump, tree
from tests.liquidity_helpers import (account_numbers, check_against_sources, liquidity_from, reports_from, rows_by_number, two_banks, unassigned_of, values_of,
                                    where_is)
from tests.susa_helpers import account_id, build_susa_world, coa, mask, raw, raw_entry

DEMO_TODAY = datetime.date(2026, 11, 3)         # ein fester Tag, nie die Uhr (tests/test_clock_at_import.py)
BRANDS = ('DATEV', 'WISO', 'ELSTER')
TOOL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'mcp_ubuchhaltung.py')


def eur(text):
    """Ein Betrag in Euro (Text) in Minor Units."""
    return to_minor(Decimal(text))


# ═══ Aufbau ═══════════════════════════════════════════════════════════════════
def test_the_layout_names_each_line_once_and_a_sum_only_reads_lines_above_it():
    seen = []
    for spec in LAYOUT:
        assert spec.key not in seen, spec.key
        for part in spec.plus:
            assert part in seen, (spec.key, part)                         # die Zwischensummen stehen hinter ihren Bestandteilen
        assert (spec.kind == SUM) == bool(spec.plus), spec.key
        seen.append(spec.key)
    assert all(re.fullmatch(r'[a-z_]+', key) for key in seen)             # die Schlüssel stehen in Adressen und Skript
    assert seen == ['result', 'depreciation', 'disposals', 'noncash_other', 'financing_in_result', 'operating', 'assets', 'financing', 'withdrawals',
                    'deposits', 'outside_other', 'computed', 'unbooked', 'unassigned', 'actual', 'memo_transfers', 'memo_balance']
    assert set(ACCOUNT_KEYS) <= set(seen) and not {'result', 'operating', 'computed', 'unbooked', 'unassigned', 'actual'} & set(ACCOUNT_KEYS)
    assert {spec.key for spec in LAYOUT if spec.optional} == {'noncash_other', 'outside_other', 'memo_transfers', 'memo_balance'}
    assert BWA_LINES == {'result': 'result', 'financing_in_result': 'liabilities'}         # Abschreibungen kommen nicht mehr aus der BWA
    assert dict(operating=('result', 'depreciation', 'disposals', 'noncash_other', 'financing_in_result'),
                computed=('operating', 'assets', 'financing', 'withdrawals', 'deposits', 'outside_other')) == {
        spec.key: spec.plus for spec in LAYOUT if spec.kind == SUM}


def test_the_ranges_are_data_in_one_place():
    assert liq.DISPOSAL_RANGES == ((4855, 4858), (6895, 6898)) and liq.FINANCING_RANGE == (3000, 3799)
    assert liq.WITHDRAWAL_RANGE == (2100, 2179) and liq.DEPOSIT_RANGE == (2180, 2199) and liq.ASSET_LIMIT == 1000
    assert liq.WITHDRAWAL_RANGE[1] + 1 == liq.DEPOSIT_RANGE[0]            # die Privatkonten 2100–2199 sind lückenlos geteilt
    assert [(low, high) for low, high, key in bwa.RANGES if key == 'liabilities'] == [liq.FINANCING_RANGE]      # dieselben Konten wie die BWA-Gruppe
    assert [(low, high) for low, high, key in bwa.RANGES if key == 'depreciation'] == [(6200, 6299)]           # und die Zeile Abschreibungen


# Jede Zeile von ``get_euer_data`` steht an genau einer Stelle: (Nummer, Saldo, im Ergebnis?, {Zeile: Wert in Euro}) – die Zeilen ohne das Ergebnis, das die
# BWA liefert. ``financing_in_result`` hebt seinen Anteil aus dem Ergebnis heraus (die BWA-Gruppe). ``depreciation``, ``disposals`` und ``noncash_other`` füllt eine
# EÜR-Zeile allein nie, nur eine Umbuchung zwischen Anlage- und Erfolgskonto (``TRANSFER_PLACEMENT``): Die Konten 4855–4858, 6200–6299 und 6895–6898 stehen hier im Ergebnis
# und in keiner anderen Zeile.
PLACEMENT = [
    (999, -10.0, False, {'assets': -10}), (1000, -10.0, True, {}), (2099, -10.0, True, {}),
    (2100, -10.0, False, {'withdrawals': -10}), (2179, -10.0, False, {'withdrawals': -10}),
    (2180, 10.0, False, {'deposits': 10}), (2199, 10.0, False, {'deposits': 10}), (2200, -10.0, True, {}), (2999, -10.0, True, {}),
    (3000, 10.0, True, {'financing': 10, 'financing_in_result': -10}), (3150, -10.0, True, {'financing': -10, 'financing_in_result': 10}),
    (3159, -10.0, True, {'financing': -10, 'financing_in_result': 10}), (3160, -10.0, False, {'financing': -10}),
    (3161, 10.0, True, {'financing': 10, 'financing_in_result': -10}), (3719, 10.0, True, {'financing': 10, 'financing_in_result': -10}),
    (3720, -10.0, False, {'financing': -10}), (3739, 10.0, True, {'financing': 10, 'financing_in_result': -10}),
    (3740, -10.0, False, {'financing': -10}), (3799, 10.0, True, {'financing': 10, 'financing_in_result': -10}), (3800, -10.0, True, {}),
    (4400, 10.0, True, {}), (3806, 10.0, True, {}),
    (4854, 10.0, True, {}), (4855, 10.0, True, {}), (4858, 10.0, True, {}), (4859, 10.0, True, {}),
    (6199, -10.0, True, {}), (6200, -10.0, True, {}), (6299, -10.0, True, {}), (6300, -10.0, True, {}),
    (6894, -10.0, True, {}), (6895, -10.0, True, {}), (6898, -10.0, True, {}), (6899, -10.0, True, {}),
    (1401, -10.0, True, {}), (520, -10.0, False, {'assets': -10}),
]

# Eine Umbuchung zwischen Anlage- und Erfolgskonto steht in ``euer_rows`` UND in ``transfer_rows`` (sie ist Teil des Saldos der EÜR): (Nummer, Saldo, Zeile, in der sie
# mit −Saldo zurückkommt). Die Prüfung geht erst nach ``DISPOSAL_RANGES``, dann nach der BWA-Gruppe ``depreciation``, sonst steht sie unter „Übrige Umbuchungen“.
TRANSFER_PLACEMENT = [
    (4854, 10.0, 'noncash_other'), (4855, 10.0, 'disposals'), (4858, 10.0, 'disposals'), (4859, 10.0, 'noncash_other'),
    (6199, -10.0, 'noncash_other'), (6200, -10.0, 'depreciation'), (6299, -10.0, 'depreciation'), (6300, -10.0, 'noncash_other'),
    (6894, -10.0, 'noncash_other'), (6895, -10.0, 'disposals'), (6898, -10.0, 'disposals'), (6899, -10.0, 'noncash_other'),
    (6815, -10.0, 'noncash_other'), (4400, 10.0, 'noncash_other'),
]


@pytest.mark.parametrize('flip', [1, -1], ids=['wie_gebucht', 'umgekehrt'])
@pytest.mark.parametrize('number, amount, in_result, expected', PLACEMENT, ids=[str(case[0]) for case in PLACEMENT])
def test_every_euer_row_lands_in_exactly_one_place(number, amount, in_result, expected, flip):
    """Eine Zeile an jeder Grenze der Bereiche (999/1000, 2099/2100, 2179/2180, 2199/2200, 2999/3000, 3799/3800, 4854/4855, 4858/4859, 6894/6895, 6898/6899,
    3160/3720/3740): Sie steht im Ergebnis oder nicht, hebt nur ihren Anteil heraus und steht sonst in keiner Zeile – mit dem Vorzeichen ihrer Wirkung auf die
    flüssigen Mittel (auch mit umgekehrtem Vorzeichen des Saldos)."""
    bwa_report, report = reports_from([(number, f'Konto {number}', amount * flip)])
    assert where_is(report, number) == {key: eur(str(value * flip)) for key, value in expected.items()}
    assert values_of(report, 'result') == (eur(str(amount * flip)) if in_result else 0) == values_of(bwa_report, 'result')
    others = {line.key: line.values['month'] for line in report.lines if line.kind == 'line' and line.key not in ('result', 'unbooked', 'unassigned')}
    assert all(value == 0 for key, value in others.items() if key not in expected), others


@pytest.mark.parametrize('flip', [1, -1], ids=['wie_gebucht', 'umgekehrt'])
@pytest.mark.parametrize('number, amount, line', TRANSFER_PLACEMENT, ids=[str(case[0]) for case in TRANSFER_PLACEMENT])
def test_every_transfer_row_lands_in_exactly_one_of_the_three_lines(number, amount, line, flip):
    """Eine Umbuchung ohne Geldfluss an jeder Grenze der Bereiche (4854/4855, 4858/4859, 6199/6200, 6299/6300, 6894/6895, 6898/6899) und auf einem anderen Konto: Sie steht
    im Ergebnis (die EÜR-Zeile) und kommt in genau EINER der Zeilen Abschreibungen, Anlagenabgänge und „Übrige Umbuchungen“ mit −Saldo zurück – auch mit umgekehrtem
    Vorzeichen des Saldos; die Zwischensumme vor Abschreibungen und Darlehen ist dann 0."""
    row = [(number, f'Konto {number}', amount * flip)]
    bwa_report, report = reports_from(row, month_transfers=row)
    assert where_is(report, number) == {line: eur(str(-amount * flip))}
    assert values_of(report, 'result') == eur(str(amount * flip)) == values_of(bwa_report, 'result')
    assert values_of(report, 'operating') == 0 and values_of(report, 'computed') == 0
    others = {other.key: other.values['month'] for other in report.lines if other.kind == 'line' and other.key not in ('result', 'unbooked', 'unassigned')}
    assert all(value == 0 for key, value in others.items() if key != line), others


def test_a_row_of_a_money_account_or_a_waiting_account_stands_under_the_other_accounts():
    """Eine Zeile, die weder im Ergebnis noch Anlage, Privat oder Darlehen ist (ein Geldkonto als Zweckkonto, ein Bestandskonto), steht unter „Übrige Konten
    außerhalb des Ergebnisses“ – nie in einer anderen Zeile und nie im Ergebnis."""
    report = liquidity_from([(1800, 'Bank', -5.0), (4400, 'Erlöse', 7.0), (10000, 'Debitoren', 3.0)], banks={1800, 4400})
    assert account_numbers(report, 'outside_other') == [1800, 4400, 10000]
    assert values_of(report, 'outside_other') == eur('5')
    assert values_of(report, 'result') == 0 and values_of(report, 'computed') == eur('5')
    assert where_is(report, 4400) == {'outside_other': eur('7')} and where_is(report, 1800) == {'outside_other': eur('-5')}


def test_a_row_of_the_other_expenses_outside_the_financing_range_stands_under_the_other_accounts(monkeypatch):
    """„Sonstige Ausgaben“ (``OTHER_EXPENSE_ACCOUNTS``) liegen heute alle in 3000–3799; würde ein Konto außerhalb dazukommen, fiele es in „Übrige“, nicht in
    die Darlehen."""
    from db import reporting
    monkeypatch.setattr(reporting, 'OTHER_EXPENSE_ACCOUNTS', frozenset({3160, 3720, 3740, 6999}))
    report = liquidity_from([(6999, 'Sonstiges', -4.0), (3160, 'Darlehen', -6.0)])
    assert where_is(report, 6999) == {'outside_other': eur('-4')} and where_is(report, 3160) == {'financing': eur('-6')}


def test_results_depreciation_and_loans_in_the_result_are_the_lines_of_the_bwa():
    rows = [(4400, 'Erlöse', 1000.0), (3806, 'Umsatzsteuer', 190.0), (6815, 'Bürobedarf', -100.0), (6220, 'AfA', -250.0), (6260, 'GWG', -40.0),
            (3150, 'Darlehen', 900.0), (3160, 'Darlehen 2', -30.0), (4855, 'Restbuchwert', 20.0), (6895, 'Restbuchwert 2', -15.0), (500, 'Anlage', -300.0)]
    moved = lambda part: [row for row in part if row[0] in (6220, 6230, 6260, 4855, 6895)]           # hier ist jedes Abschreibungs- und Restbuchwertkonto eine Umbuchung
    bwa_report, report = reports_from(rows, ytd_rows=rows + [(6230, 'AfA 2', -5.0)], variant='vorjahr', prev_month_rows=rows[:4], prev_ytd_rows=rows[2:6],
                                      month_transfers=moved(rows), ytd_transfers=moved(rows + [(6230, 'AfA 2', -5.0)]), prev_month_transfers=moved(rows[:4]),
                                      prev_ytd_transfers=moved(rows[2:6]))
    for period in report.periods:
        assert report.line('result').values[period] == bwa_report.line('result').values[period]
        assert report.line('depreciation').values[period] == bwa_report.line('depreciation').values[period]
        assert report.line('financing_in_result').values[period] == bwa_report.line('liabilities').values[period]
    assert account_numbers(report, 'depreciation') == [6220, 6230, 6260] and account_numbers(report, 'financing_in_result') == [3150]
    assert [(a.number, a.name, a.values) for a in report.line('depreciation').accounts] == [
        (a.number, a.name, a.values) for a in bwa_report.line('depreciation').accounts]
    assert [(a.number, a.values) for a in report.line('financing_in_result').accounts] == [
        (a.number, a.values) for a in bwa_report.line('liabilities').accounts]
    assert values_of(report, 'result') == eur('1000') + eur('190') - eur('100') - eur('250') - eur('40') + eur('900') + eur('20') - eur('15')
    assert values_of(report, 'depreciation') == eur('290') and values_of(report, 'financing_in_result') == eur('-900')
    assert values_of(report, 'disposals') == eur('-20') + eur('15')                                            # der Restbuchwert fällt wieder heraus
    assert values_of(report, 'financing') == eur('900') - eur('30')                                           # alle Konten 3000–3799, auch 3160
    assert values_of(report, 'assets') == eur('-300')


def test_a_number_that_is_no_whole_number_has_no_line_of_its_own():
    """Eine Kontonummer als Kommazahl (per SQL-Konsole schreibbar) hat in der betriebswirtschaftlichen Auswertung keine Gruppe – sie steht dort unter „Nicht
    zugeordnet“ und im Ergebnis. Dann gehört sie auch hier in keinen Bereich: weder Darlehen (3150,5) noch Restbuchwert (4855,5); außerhalb des Ergebnisses
    (500,5, 2100,5) steht sie unter den übrigen Konten. Die Überleitung bleibt die Summe aller Zeilen."""
    rows = [(3150.5, 'Darlehen?', 10000.0), (4855.5, 'Restbuchwert?', -3000.0), (500.5, 'Anlage?', -200.0), (2100.5, 'Privat?', -50.0)]
    bwa_report, report = reports_from(rows)
    result = bwa_report.line('result').values['month']
    assert result == eur('7000')                                                                    # 3150,5 und 4855,5 stehen im Ergebnis
    for key in ('financing', 'financing_in_result', 'disposals', 'assets', 'withdrawals', 'deposits'):
        assert values_of(report, key) == 0 and account_numbers(report, key) == [], key
    assert values_of(report, 'outside_other') == eur('-250') and account_numbers(report, 'outside_other') == [500.5, 2100.5]
    assert values_of(report, 'computed') == eur('6750')
    # dieselbe Kommazahl 4855,5 auch als Umbuchung ohne Geldfluss: kein Bereich, also unter „Übrige Umbuchungen“ – nie unter dem Restbuchwert
    _bwa_report, moved = reports_from(rows, month_transfers=[(4855.5, 'Restbuchwert?', -3000.0)])
    assert values_of(moved, 'disposals') == 0 and account_numbers(moved, 'disposals') == [] and values_of(moved, 'depreciation') == 0
    assert values_of(moved, 'noncash_other') == eur('3000') and account_numbers(moved, 'noncash_other') == [4855.5]
    assert values_of(moved, 'computed') == eur('9750') and values_of(moved, 'operating') == eur('10000')


def test_two_rows_of_one_number_are_added_per_period_and_the_first_name_stays():
    """Die echte und die virtuelle 3806 (im Ergebnis) und zwei Konten gleicher Nummer aus verschiedenen Kontenrahmen (hier 3150, 500 und 2100) stehen je
    Zeitraum als EINE Zeile unter dem zuerst gefundenen Namen – Monat vor aufgelaufen."""
    report = liquidity_from([(3150, 'Erster Name', 100.0), (3150, 'Zweiter Name', -30.0), (3806, 'USt 19%', 5.0), (3806, 'USt 19%', 7.0),
                             (500, 'Anlage A', -10.0), (500, 'Anlage B', -5.0)],
                            ytd_rows=[(3150, 'Name im Jahr', 50.0), (500, 'Anlage C', -1.0), (2100, 'Entnahme', -3.0), (2100, 'Entnahme 2', -4.0)])
    [loan] = report.line('financing').accounts
    assert (loan.number, loan.name, loan.values) == (3150, 'Erster Name', {'month': eur('70'), 'ytd': eur('50')})
    [asset] = report.line('assets').accounts
    assert (asset.name, asset.values) == ('Anlage A', {'month': eur('-15'), 'ytd': eur('-1')})
    [private] = report.line('withdrawals').accounts
    assert (private.name, private.values) == ('Entnahme', {'ytd': eur('-7')})                              # im Monat steht es nicht da
    assert values_of(report, 'result') == eur('70') + eur('12')


def test_a_zero_sum_account_is_listed_but_not_shown():
    report = liquidity_from([(500, 'Anlage', -10.0), (500, 'Anlage', 10.0)])
    assert account_numbers(report, 'assets') == [500] and visible_accounts(report, report.line('assets')) == []


# ═══ Überleitung und „Nicht zugeordnet“ ═══════════════════════════════════════
def test_the_overview_is_the_sum_of_its_parts_and_not_assigned_is_the_difference():
    cash = {'month': CashFigures(eur('1234.56'), eur('-200'), eur('50'), 2), 'ytd': CashFigures(eur('-80'), 0, eur('-10'), 1)}
    rows = [(4400, 'Erlöse', 100.0), (6220, 'AfA', -30.0), (3150, 'Darlehen', 500.0), (500, 'Anlage', -250.0), (2100, 'Entnahme', -40.0),
            (2180, 'Einlage', 15.0), (4855, 'Restbuchwert', 8.0), (1800, 'Bank', 1.0)]
    report = liquidity_from(rows, banks={1800}, cash=cash, month_transfers=[(6220, 'AfA', -30.0), (4855, 'Restbuchwert', 8.0)])      # AfA und Restbuchwert als Umbuchung
    for period in ('month', 'ytd'):
        values = lambda key: report.line(key).values[period]
        assert values('operating') == (values('result') + values('depreciation') + values('disposals') + values('noncash_other')
                                       + values('financing_in_result'))
        assert values('computed') == (values('operating') + values('assets') + values('financing') + values('withdrawals') + values('deposits')
                                      + values('outside_other'))
        assert values('unassigned') == values('actual') - values('computed') - values('unbooked')
        assert values('memo_balance') == values('actual') + values('memo_transfers')
    # Ergebnis 100 − 30 + 500 + 8 = 578; zurück: Abschreibung 30, Restbuchwert −8, Darlehen im Ergebnis −500 → 100; dazu Anlage −250, Darlehen 500, Entnahme
    # −40, Einlage 15 und das Geldkonto unter „Übrige“ 1 → 326
    assert [values_of(report, key) for key in ('result', 'depreciation', 'disposals', 'financing_in_result', 'operating')] == [
        eur('578'), eur('30'), eur('-8'), eur('-500'), eur('100')]
    assert [values_of(report, key) for key in ('assets', 'financing', 'withdrawals', 'deposits', 'outside_other', 'computed')] == [
        eur('-250'), eur('500'), eur('-40'), eur('15'), eur('1'), eur('326')]
    assert values_of(report, 'unbooked') == eur('50') and report.line('unbooked').counts == {'month': 2, 'ytd': 1}
    assert values_of(report, 'actual') == eur('1234.56') and values_of(report, 'unassigned') == eur('858.56')
    assert values_of(report, 'memo_transfers') == eur('-200') and values_of(report, 'memo_balance') == eur('1034.56')
    assert values_of(report, 'unassigned', 'ytd') == eur('-80') - eur('326') + eur('10')


def test_the_overview_holds_for_any_rows_and_figures():
    """Eigenschaftsprüfung mit festem Seed über Zeilen aus allen Bereichen (auch Geldkonten und Zeilen ohne Gruppe), positive und negative Salden und zufällige
    Kontobewegungen, dazu zufällige Umbuchungen ohne Geldfluss (ein Teil des Saldos einer gezogenen Zeile, auch auf einem Konto, das keine EÜR-Zeile hat):
    Zwischensummen und „Nicht zugeordnet“ sind aus ihren Bestandteilen gerechnet, Ergebnis und Darlehen im Ergebnis sind die der BWA, Abschreibungen,
    Anlagenabgänge und übrige Umbuchungen sind −Saldo der Umbuchungen nach ihren Bereichen (nie aus einer EÜR-Zeile allein), und jede andere Zeile steht in genau
    einer Zeile außer dem Ergebnis (oder gar nicht, wenn sie nur im Ergebnis zählt)."""
    rng = random.Random(262)
    banks = {1800, 1600}
    pool = [4400, 4300, 3806, 6815, 6850, 6220, 6260, 1406, 1401, 3820, 3841, 1050, 520, 500, 2100, 2150, 2180, 2199, 3150, 3160, 3720, 3740, 3000, 3799, 4855,
            4858, 6895, 6898, 1800, 1600, 10000, 4405, 8000]
    transfer_pool = [6220, 6222, 6260, 4855, 4858, 6895, 6898, 6815, 6850, 4400, 6199, 6300]

    def draw():
        return [(number, f'Konto {number}', round(rng.choice([-1, 1]) * rng.randint(1, 900_000) / 100, 2)) for number in rng.sample(pool, rng.randint(0, 14))]

    def draw_transfers(rows):
        """Ein Teil der gezogenen Zeilen (der ganze Saldo oder ein Teil) als Umbuchung; dazu ein Konto, das in diesem Zeitraum keine EÜR-Zeile hat."""
        moved = [(number, name, round(total * rng.choice([1, 1, 0.5, 0.25]), 2)) for number, name, total in rows if number in transfer_pool and rng.random() < 0.6]
        if rng.random() < 0.3:
            moved.append((rng.choice(transfer_pool), 'Konto ohne EÜR-Zeile', rng.choice([-1, 1]) * rng.randint(1, 90_000) / 100))
        return moved

    expected_line = lambda number: ('disposals' if 4855 <= number <= 4858 or 6895 <= number <= 6898
                                    else 'depreciation' if 6200 <= number <= 6299 else 'noncash_other')
    for _round in range(200):
        rows = {'month': draw(), 'ytd': draw(), 'prev_month': draw(), 'prev_ytd': draw()}
        transfers = {key: draw_transfers(rows[key]) for key in rows}
        cash = {key: CashFigures(rng.randint(-10 ** 7, 10 ** 7), rng.randint(-10 ** 6, 10 ** 6), rng.randint(-10 ** 6, 10 ** 6), rng.randint(0, 9))
                for key in rows}
        bwa_report, report = reports_from(rows['month'], rows['ytd'], 'vorjahr', banks=banks, prev_month_rows=rows['prev_month'],
                                          prev_ytd_rows=rows['prev_ytd'], cash=cash, month_transfers=transfers['month'], ytd_transfers=transfers['ytd'],
                                          prev_month_transfers=transfers['prev_month'], prev_ytd_transfers=transfers['prev_ytd'])
        for period in report.periods:
            values = lambda key: report.line(key).values[period]
            assert values('result') == bwa_report.line('result').values[period]
            assert values('financing_in_result') == bwa_report.line('liabilities').values[period]
            moved = dict.fromkeys(('depreciation', 'disposals', 'noncash_other'), 0)
            for number, _name, total in transfers[period]:
                moved[expected_line(number)] -= to_minor(total)
            assert {key: values(key) for key in moved} == moved, (period, transfers[period])
            assert values('operating') == (values('result') + values('depreciation') + values('disposals') + values('noncash_other')
                                           + values('financing_in_result'))
            assert values('computed') == (values('operating') + values('assets') + values('financing') + values('withdrawals') + values('deposits')
                                          + values('outside_other'))
            assert values('unassigned') == cash[period].actual - values('computed') - cash[period].unbooked
            assert values('computed') + values('unbooked') + values('unassigned') == values('actual') == cash[period].actual
            assert values('memo_balance') == cash[period].actual + cash[period].transfers
            # Jede Zeile steht an einer Stelle: Was im Ergebnis zählt, ist das Ergebnis; alles andere steht in genau einer der Zeilen darunter (die
            # Darlehen im Ergebnis, die Zeile 4 herausnimmt und Zeile 7 zurückbringt, heben sich auf); die Umbuchungen sind nur ein Rückrechnen des Ergebnisses
            sums = {'result': 0, 'outside': 0}
            for number, _name, total in rows[period]:
                sums['result' if euer_section(number, banks) in ('income', 'expense') else 'outside'] += to_minor(total)
            assert sums['result'] == values('result')
            assert sums['outside'] == (values('assets') + values('withdrawals') + values('deposits') + values('outside_other')
                                       + values('financing') + values('financing_in_result')), (period, rows[period])


def test_the_loan_lines_net_out_to_what_is_not_in_the_result():
    """Zeile 4 nimmt die Darlehen aus dem Ergebnis, Zeile 7 bringt alle Konten 3000–3799 zurück: Netto bleibt genau der Anteil, der nicht im Ergebnis steht
    (3160, 3720, 3740) – so ist die Überleitung gleich der Veränderung der flüssigen Mittel, wo Ergebnis und Konten übereinstimmen."""
    report = liquidity_from([(3150, 'Darlehen', 10000.0), (3150, 'Darlehen', -500.0), (3160, 'Darlehen 1-5 Jahre', -30.0)])
    assert values_of(report, 'financing_in_result') == eur('-9500') and values_of(report, 'financing') == eur('9470')
    assert values_of(report, 'financing') + values_of(report, 'financing_in_result') == eur('-30')
    assert values_of(report, 'result') == eur('9500')                                                              # das Ergebnis enthält 3150, wie die BWA
    assert values_of(report, 'computed') == eur('9470')


# ═══ Spalten, Köpfe, Zellen ═══════════════════════════════════════════════════
def test_the_columns_and_headers_of_both_variants():
    year = liquidity_from([(500, 'Anlage', -100.0)], variant='jahr', month=10)
    assert [(column.period, column.kind) for column in columns(year)] == [('month', 'amount'), ('ytd', 'amount')]
    assert block_size(year) == 1 and column_labels(year) == ['Betrag €', 'Betrag €']
    assert block_headers(year) == ('Oktober 2026', 'Januar bis Oktober 2026')
    previous = liquidity_from([(500, 'Anlage', -100.0)], variant='vorjahr', month=10)
    assert [(column.period, column.kind) for column in columns(previous)] == [
        ('month', 'amount'), ('prev_month', 'amount'), ('month', 'delta'), ('month', 'delta_pct'),
        ('ytd', 'amount'), ('prev_ytd', 'amount'), ('ytd', 'delta'), ('ytd', 'delta_pct')]
    assert block_size(previous) == 4 and column_labels(previous) == ['2026', '2025', 'Veränderung €', 'Veränderung %'] * 2
    assert block_headers(previous) == ('Oktober', 'Januar bis Oktober')
    assert period_line(previous) == 'Oktober 2026 · aufgelaufen Januar bis Oktober 2026 · Vergleich mit dem Vorjahr'
    assert period_line(year) == 'Oktober 2026 · aufgelaufen Januar bis Oktober 2026'
    assert block_headers(liquidity_from([], variant='jahr', month=1)) == ('Januar 2026', 'Januar 2026 (aufgelaufen)')


def test_the_amount_columns_of_the_year_variant_have_no_shares():
    report = liquidity_from([(4400, 'Erlöse', 1000.0), (500, 'Anlage', -100.0)])
    line = report.line('assets')
    assert cells(report, line.values) == ['-100,00', '-100,00'] and len(cells(report, report.line('result').values)) == 2
    assert all('%' not in label for label in column_labels(report))
    assert {key for key in VARIANT_LABELS} == {'jahr', 'vorjahr'} and VARIANT_LABELS['jahr'] == 'Monat und Jahr'


def test_the_comparison_shows_change_in_euro_and_percent_like_the_bwa_and_blanks():
    report = liquidity_from([(500, 'Anlage', -1000.0)], ytd_rows=[(500, 'Anlage', -1500.0), (520, 'Fahrzeug', -300.0)], variant='vorjahr',
                            prev_month_rows=[(500, 'Anlage', -400.0)], prev_ytd_rows=[])
    line = report.line('assets')
    assert cells(report, line.values) == ['-1.000,00', '-400,00', '-600,00', '-150,00', '-1.800,00', '0,00', '-1.800,00', '']        # Vorjahr 0: kein Prozent
    by_number = {account.number: account for account in line.accounts}
    assert cells(report, by_number[500].values) == ['-1.000,00', '-400,00', '-600,00', '-150,00', '-1.500,00', '', '-1.500,00', '']
    assert cells(report, by_number[520].values) == ['', '', '', '', '-300,00', '', '-300,00', '']                # nur aufgelaufen: der Monat ist leer


def test_a_value_below_half_a_cent_never_shows_minus_zero():
    previous = liquidity_from([], variant='vorjahr')
    for values in ({'month': -49, 'ytd': 49, 'prev_month': 0, 'prev_ytd': 0}, {'month': 0, 'ytd': 0, 'prev_month': 49, 'prev_ytd': -49}):
        text = ' '.join(cells(previous, values))
        assert '-0,00' not in text and '0,00' in text
    assert cells(liquidity_from([]), {'month': -49, 'ytd': -1}) == ['0,00', '0,00']


# ═══ Sichtbarkeit ═════════════════════════════════════════════════════════════
def test_unassigned_and_unbooked_always_stand_there_and_the_optional_lines_only_with_a_value():
    quiet = liquidity_from([(4400, 'Erlöse', 100.0)])
    shown = {line.key: visible(quiet, line) for line in quiet.lines}
    assert shown['unassigned'] and shown['unbooked'] and shown['result'] and shown['computed'] and shown['actual']        # auch mit 0,00
    assert not shown['noncash_other'] and not shown['outside_other'] and not shown['memo_transfers'] and not shown['memo_balance']
    assert sum(shown.values()) == len(LAYOUT) - 4
    rest = liquidity_from([(1800, 'Bank', -5.0)], banks={1800})
    assert visible(rest, rest.line('outside_other'))
    only_ytd = liquidity_from([], ytd_rows=[(1800, 'Bank', -5.0)], banks={1800})
    assert only_ytd.line('outside_other').values == {'month': 0, 'ytd': eur('-5')} and visible(only_ytd, only_ytd.line('outside_other'))
    cancelled = liquidity_from([(1800, 'Bank', -5.0), (1800, 'Bank', 5.0)], banks={1800})
    assert not visible(cancelled, cancelled.line('outside_other'))                              # nur bei einem Wert ≠ 0, auch wenn es Konten gibt


def test_the_line_of_the_other_transfers_without_cash_stands_only_with_a_value():
    """„Übrige Umbuchungen ohne Geldfluss“ ist optional wie „Übrige Konten“: Ohne eine solche Umbuchung steht die Zeile nicht da (auch nicht, wenn Abschreibung und
    Restbuchwert Werte haben); mit einem Wert in einem der angezeigten Zeiträume steht sie da, auch nur im aufgelaufenen, mit ihrem Konto – und hebt sich in
    einem Zeitraum, in dem Umbuchung und Gegenbuchung sich aufheben, wieder auf."""
    plain = liquidity_from([(6815, 'Bürobedarf', -300.0), (6220, 'AfA', -40.0), (6895, 'Restbuchwert', -5.0)],
                           month_transfers=[(6220, 'AfA', -40.0), (6895, 'Restbuchwert', -5.0)])
    assert not visible(plain, plain.line('noncash_other')) and visible(plain, plain.line('depreciation')) and visible(plain, plain.line('disposals'))
    month = liquidity_from([(6815, 'Bürobedarf', -300.0)], month_transfers=[(6815, 'Bürobedarf', -300.0)])
    assert visible(month, month.line('noncash_other')) and account_numbers(month, 'noncash_other') == [6815]
    assert [account_label(account) for account in visible_accounts(month, month.line('noncash_other'))] == ['6815 Bürobedarf']
    only_ytd = liquidity_from([], ytd_rows=[(6815, 'Bürobedarf', -300.0)], ytd_transfers=[(6815, 'Bürobedarf', -300.0)])
    assert only_ytd.line('noncash_other').values == {'month': 0, 'ytd': eur('300')} and visible(only_ytd, only_ytd.line('noncash_other'))
    netted = liquidity_from([(6815, 'Bürobedarf', -300.0)], month_transfers=[(6815, 'Bürobedarf', -300.0), (6815, 'Bürobedarf', 300.0)])
    assert not visible(netted, netted.line('noncash_other')) and visible_accounts(netted, netted.line('noncash_other')) == []


def test_the_transfers_and_the_balance_of_the_movements_stand_only_beside_each_other():
    """Die nachrichtlichen Zeilen stehen nur, wenn Überträge dastehen – ohne sie wäre „Veränderung laut Kontobewegungen“ eine Wiederholung des Ist."""
    plain = liquidity_from([], cash={'month': CashFigures(eur('5'), 0, 0, 0), 'ytd': CashFigures(eur('5'), 0, 0, 0)})
    assert not visible(plain, plain.line('memo_transfers')) and not visible(plain, plain.line('memo_balance'))
    with_transfers = liquidity_from([], cash={'ytd': CashFigures(eur('5'), eur('-200'), 0, 0)})
    assert visible(with_transfers, with_transfers.line('memo_transfers')) and visible(with_transfers, with_transfers.line('memo_balance'))
    assert with_transfers.line('memo_balance').values == {'month': 0, 'ytd': eur('-195')}
    netted = liquidity_from([], cash={'month': CashFigures(0, eur('-200'), 0, 0), 'ytd': CashFigures(0, eur('200'), 0, 0)})
    assert visible(netted, netted.line('memo_transfers'))


def test_visible_accounts_are_those_with_a_value_in_a_shown_period():
    report = liquidity_from([(500, 'Anlage', -10.0), (520, 'Fahrzeug', 0.0)], ytd_rows=[(500, 'Anlage', -10.0), (510, 'Gerät', -3.0)])
    assert account_numbers(report, 'assets') == [500, 510, 520]
    assert [account.number for account in visible_accounts(report, report.line('assets'))] == [500, 510]


def test_account_labels_name_the_number_and_a_missing_name_never_shows_none():
    report = liquidity_from([(500, None, -10.0), (520, '', -5.0), (510, 'Gerät', -1.0)])
    assert [account_label(account) for account in report.line('assets').accounts] == ['500', '510 Gerät', '520']


# ═══ Abstimmung als Satz ══════════════════════════════════════════════════════
def test_balanced_means_exactly_zero_minor_units_and_a_rest_under_half_a_cent_is_named():
    cash = {'month': CashFigures(eur('100'), 0, 0, 0), 'ytd': CashFigures(eur('100'), 0, 0, 0)}
    balanced_report = liquidity_from([(4400, 'Erlöse', 100.0)], cash=cash)
    assert balanced(balanced_report) and not rounding_remainder(balanced_report)
    assert unassigned_of(balanced_report) == {'month': 0, 'ytd': 0}
    cent = {'month': CashFigures(eur('100') - 49, 0, 0, 0), 'ytd': CashFigures(eur('100'), 0, 0, 0)}
    rounding = liquidity_from([(4400, 'Erlöse', 100.0)], cash=cent)
    assert not balanced(rounding) and rounding_remainder(rounding)
    assert cells(rounding, rounding.line('unassigned').values) == ['0,00', '0,00']                      # der Rest steht als 0,00 da – und wird genannt
    real = liquidity_from([(4400, 'Erlöse', 100.0)], cash={'month': CashFigures(eur('90'), 0, 0, 0), 'ytd': CashFigures(eur('100'), 0, 0, 0)})
    assert not balanced(real) and not rounding_remainder(real)
    half = liquidity_from([(4400, 'Erlöse', 100.0)], cash={'month': CashFigures(eur('100') - 50, 0, 0, 0), 'ytd': CashFigures(eur('100'), 0, 0, 0)})
    assert not rounding_remainder(half) and cells(half, half.line('unassigned').values)[0] == '-0,01'     # ab einem halben Cent steht ein Wert da


def test_the_variant_decides_which_periods_must_balance():
    """„Geht auf“ gilt in jedem angezeigten Zeitraum – im Vorjahresvergleich auch in den Vorjahren, in der Darstellung „Monat und Jahr“ nur in den zweien."""
    cash = {'month': CashFigures(0, 0, 0, 0), 'ytd': CashFigures(0, 0, 0, 0), 'prev_month': CashFigures(eur('7'), 0, 0, 0), 'prev_ytd': CashFigures(0, 0, 0, 0)}
    assert balanced(liquidity_from([], variant='jahr', cash=cash)) and not balanced(liquidity_from([], variant='vorjahr', cash=cash))


def test_the_unbooked_line_names_its_count_in_the_month_and_accumulated():
    cash = {'month': CashFigures(eur('119'), 0, eur('119'), 1), 'ytd': CashFigures(eur('150'), 0, eur('150'), 3)}
    report = liquidity_from([], cash=cash)
    assert line_label(report, report.line('unbooked')) == 'Bankbewegungen ohne Buchungssatz (1 im Monat, 3 aufgelaufen)'
    january = liquidity_from([], month=1, cash={'month': CashFigures(eur('119'), 0, eur('119'), 2), 'ytd': CashFigures(eur('119'), 0, eur('119'), 2)})
    assert line_label(january, january.line('unbooked')) == 'Bankbewegungen ohne Buchungssatz (2)'
    none = liquidity_from([])
    assert line_label(none, none.line('unbooked')) == 'Bankbewegungen ohne Buchungssatz'
    assert all(line_label(report, line) == line.label for line in report.lines if line.key != 'unbooked')


def test_the_filename_names_the_month_and_the_variant():
    assert liquidity_filename(2026, 10, 'vorjahr') == 'Liquiditaet_2026-10.pdf'                    # der Standard: der Vorjahresvergleich
    assert liquidity_filename(2026, 10, 'jahr') == 'Liquiditaet_2026-10_Monat_Jahr.pdf'
    assert liquidity_filename(2026, 1, 'vorjahr') == 'Liquiditaet_2026-01.pdf'


def test_the_texts_keep_the_decided_wording_and_name_no_brand():
    assert HINT_TEXT == 'Aus den Buchungen dieser App gerechnet. Keine Steuerberatung; ersetzt nicht die Auswertung eines Steuerbüros.'
    assert UNASSIGNED_TEXT == ('Diesen Betrag erklärt die Überleitung nicht: Eine Zahlung steht in der Einnahmenüberschussrechnung in einem anderen Monat oder '
                               'mit einem anderen Betrag als auf dem Konto – etwa bei Buchungssätzen mit einem anderen Datum als ihre Bankbewegung oder über '
                               'ein Verrechnungskonto.')
    assert 'gleicht sich' not in UNASSIGNED_TEXT                                                  # keine Zusage, dass der Rest wieder verschwindet
    assert 'Sofortabschreibung' not in UNASSIGNED_TEXT                                            # eine direkt bezahlte Abschreibung geht auf, sie bleibt im Ergebnis
    assert 'Umbuchung zwischen Anlage- und Erfolgskonto' in METHOD_TEXT and 'direkt bezahlte Abschreibung bleibt im Ergebnis' in METHOD_TEXT
    assert BALANCED_TEXT == 'Die Überleitung geht auf.' and liq.TITLE == 'Liquiditätsrechnung'
    texts = [HINT_TEXT, SIGN_TEXT, METHOD_TEXT, UNASSIGNED_TEXT, BALANCED_TEXT, UNBALANCED_TEXT, ROUNDING_TEXT, TRANSFER_TEXT, *(spec.label for spec in LAYOUT),
             *VARIANT_LABELS.values()]
    for brand in BRANDS:
        assert not any(brand.lower() in text.lower() for text in texts), brand


# ═══ Lesen: get_liquidity ═════════════════════════════════════════════════════
#: Die Zeilen, die ein Fall zeigt (ohne die Zwischensumme ``operating``, die dem Ergebnis folgt)
SHOWN = ('result', 'depreciation', 'disposals', 'noncash_other', 'financing_in_result', 'assets', 'financing', 'withdrawals', 'deposits', 'outside_other',
         'computed', 'unbooked', 'unassigned', 'actual', 'memo_transfers')


def shown(report, period='month'):
    """Die Zeilen ≠ 0 eines Zeitraums: ``{Schlüssel: Minor Units}`` – so liest sich ein Fall als Tabelle."""
    return {key: report.line(key).values[period] for key in SHOWN if report.line(key).values[period]}


def at(db, year, month, variant='jahr'):
    return db.get_liquidity(year, month, variant)


@pytest.mark.parametrize('arguments', [(2026, 3, 'x'), (2026, 13, 'jahr'), (2026, 0, 'jahr'), (2026, -1, 'jahr'), (0, 3, 'jahr'), (10000, 3, 'jahr'),
                                       ('2026', 3, 'jahr'), (2026, '3', 'jahr'), (2026.0, 3, 'jahr'), (None, 3, 'jahr'), (2026, 3, None), (2026, 3, 'Jahr')])
def test_get_liquidity_refuses_an_unknown_variant_and_an_invalid_period(tmp_db, arguments):
    with pytest.raises(ValueError):
        tmp_db.get_liquidity(*arguments)


@pytest.mark.parametrize('month, variant, reads', [(3, 'jahr', 2), (3, 'vorjahr', 4), (1, 'jahr', 1), (1, 'vorjahr', 2)])
def test_get_liquidity_reads_each_span_once(tmp_db, monkeypatch, month, variant, reads):
    """Im Januar sind Monat und aufgelaufen dieselbe Spanne: Sie wird nur einmal gelesen – die Zeilen der EÜR (voll und, für die Umbuchungen ohne Geldfluss,
    mit ``only_asset_transfers=True``) und die Töpfe des Dashboards je Spanne."""
    euer, moved, dashboard = [], [], []
    real_euer, real_dashboard = tmp_db.get_euer_data, tmp_db._dashboard_movements

    def counting_euer(*args, **kwargs):
        (moved if kwargs.get('only_asset_transfers') else euer).append(args)
        return real_euer(*args, **kwargs)
    monkeypatch.setattr(tmp_db, 'get_euer_data', counting_euer)
    monkeypatch.setattr(tmp_db, '_dashboard_movements',
                        lambda cursor, date_from, date_to, scope: dashboard.append((date_from, date_to)) or real_dashboard(cursor, date_from, date_to, scope))
    report = at(tmp_db, 2026, month, variant)
    assert len(euer) == len(set(euer)) == len(moved) == len(set(moved)) == reads and len(dashboard) == len(set(dashboard)) == reads
    assert set(euer) == set(moved) == set(dashboard) == set(report.periods.values())
    assert set(report.periods) == set(periods_for(variant)) and set(report.line('result').values) == set(report.periods)


def test_an_empty_book_has_zero_everywhere_and_balances(tmp_db):
    report = at(tmp_db, 2026, 3, 'vorjahr')
    assert all(value == 0 for line in report.lines for value in line.values.values()) and balanced(report)
    assert all(not line.accounts for line in report.lines)


def test_a_book_without_any_account_has_zero_everywhere_and_raises_nothing(tmp_db):
    """Ohne ein einziges Konto gibt es nichts zu zählen (wie ``get_dashboard``): alles 0, kein Fehler."""
    raw(tmp_db, 'DELETE FROM Accounts')
    assert tmp_db.fetch_accounts() == []
    report = at(tmp_db, 2026, 3, 'vorjahr')
    assert all(value == 0 for line in report.lines for value in line.values.values()) and balanced(report)
    assert PageLiquidity(tmp_db, BwaQuery(2026, 3, 'jahr')).count('<tr') > 10


def test_the_unbooked_movements_use_the_cover_of_the_caller_and_the_transfer_calculation_only_when_needed(susa_db, monkeypatch):
    """``_liquidity_unbooked`` rechnet die Überträge (``_settle_stock``, sie liest den ganzen Bestand) nur, wenn eine Bewegung ohne Satz einen ganzzahligen Betrag
    ≠ 0 hat, und nimmt die gedeckten IDs, die der Aufrufer mitgibt – so läuft die Rechnung bei vier Zeiträumen nur einmal."""
    conn = susa_db._get_connection()
    try:
        cursor = conn.cursor()
        scope = susa_db._dashboard_scope(cursor, None)
        acct_ids = scope[0]
        july = ('2026-07-01', '2026-07-31')
        assert susa_db._liquidity_unbooked(cursor, *july, acct_ids) == (0, 0)                      # die Gegenseite eines Übertrags: gedeckt
        assert susa_db._liquidity_unbooked(cursor, *july, acct_ids, covered=lambda: frozenset()) == (eur('300'), 1)
        calls = []
        monkeypatch.setattr(susa_db, '_settle_stock', lambda cursor: calls.append(1) or pytest.fail('keine Übertragsrechnung nötig'))
        assert susa_db._liquidity_unbooked(cursor, '2026-09-01', '2026-09-30', acct_ids) == (0, 0)  # keine Bewegung ohne Satz: nichts zu decken
        assert susa_db._liquidity_unbooked(cursor, '2026-08-01', '2026-08-31', acct_ids, covered=lambda: frozenset()) == (eur('119'), 1)
        assert not calls
    finally:
        conn.close()


# ═══ Abstimmung: Fixtures ═════════════════════════════════════════════════════
def _module_db(tmp_path_factory, name, build):
    """Eine Datenbank, einmal gebaut; die Datenwurzel liegt unter ``tmp_path_factory`` – nichts landet im echten data/ (``pytest.MonkeyPatch.context``, weil
    eine modulweite Fixture den ``monkeypatch`` je Test nicht nutzen darf)."""
    root = tmp_path_factory.mktemp(name)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, 'DATA_ROOT', str(root / 'data'))
        patch.setattr(auth, 'AUTH_DB', str(root / 'data' / 'auth.db'))
        userctx._mode_cache.clear()
        db = build(str(root / f'{name}.db'))
    userctx._mode_cache.clear()
    return db


@pytest.fixture(scope='module')
def susa_db(tmp_path_factory):
    """Die SuSa-Welt (``tests/susa_helpers.py``): jeder Satz entsteht über einen Weg der App."""
    def build(path):
        db = Database(path)
        build_susa_world(db)
        return db
    return _module_db(tmp_path_factory, 'liq_susa', build)


@pytest.fixture(scope='module')
def world_db(tmp_path_factory):
    """Die Testwelt (``tests/entry_form_world.py``): jeder Schreibweg einmal, dazu Zeilen, wie 2.0.x sie schrieb."""
    return _module_db(tmp_path_factory, 'liq_world', entry_form_world.build)


@pytest.fixture(scope='module')
def demo_db(tmp_path_factory):
    """Der Demobestand, einmal geladen (Profil ``test``, fester Stichtag – wie in ``test_bwa.py``)."""
    def build(path):
        db = Database(path)
        db.load_test_seed_data(include_invoices=False, include_dated=False)
        demo_seed.load_demo_scenario(db, today=DEMO_TODAY, profile='test')
        return db
    return _module_db(tmp_path_factory, 'liq_demo', build)


# ═══ Abstimmung: Demobestand ══════════════════════════════════════════════════
def months_of(*years):
    return [(year, month) for year in years for month in range(1, 13)]


def test_not_assigned_is_zero_in_every_month_and_accumulated_in_the_demo(demo_db):
    """Die Zusage: Im Demobestand geht die Überleitung in allen 23 Monaten und aufgelaufen exakt auf – „Nicht zugeordnet“ ist 0 Minor Units, nicht nur 0,00."""
    checked = 0
    for year, month in months_of(2025, 2026):
        if (year, month) > (2026, 11):
            break
        report = at(demo_db, year, month, 'vorjahr')
        assert unassigned_of(report) == dict.fromkeys(report.periods, 0), (year, month)
        assert balanced(report)
        checked += 1
    assert checked == 23


@pytest.mark.parametrize('year, month, period, expected', [
    # Februar 2025: Ergebnis, Entnahmen, Einlagen
    (2025, 2, 'month', {'result': '5.710,70', 'withdrawals': '-1.500,00', 'deposits': '2.500,00', 'computed': '6.710,70', 'unassigned': '0,00',
                        'actual': '6.710,70'}),
    # Dezember 2025: die Abschreibung geht zurück, ein Übertrag steht nachrichtlich da
    (2025, 12, 'month', {'result': '976,07', 'depreciation': '652,23', 'withdrawals': '-1.500,00', 'computed': '128,30', 'unassigned': '0,00',
                         'actual': '128,30', 'memo_transfers': '-250,00'}),
    (2025, 12, 'ytd', {'result': '46.275,94', 'depreciation': '652,23', 'assets': '-3.879,00', 'withdrawals': '-18.023,97', 'deposits': '2.500,00',
                       'computed': '27.525,20', 'unassigned': '0,00', 'actual': '27.525,20', 'memo_transfers': '-1.100,00'}),
    # März 2026: eine Anlage für 23.800 €, die Portokasse bekommt 200 € – ihre Kassenseite führt das Kassenbuch nicht
    (2026, 3, 'month', {'result': '14.423,92', 'assets': '-23.800,00', 'withdrawals': '-1.500,00', 'computed': '-10.876,08', 'unassigned': '0,00',
                        'actual': '-10.876,08', 'memo_transfers': '-200,00'}),
    # November 2026: drei Bankbewegungen ohne Buchungssatz
    (2026, 11, 'month', {'result': '862,80', 'computed': '862,80', 'unbooked': '2.268,85', 'unassigned': '0,00', 'actual': '3.131,65'}),
])
def test_the_demo_shows_these_values(demo_db, year, month, period, expected):
    report = at(demo_db, year, month)
    assert {key: format_amount(report.line(key).values[period]) for key in expected} == expected
    assert {key for key in SHOWN if report.line(key).values[period]} <= set(expected)                 # und sonst nichts


def test_the_demo_loans_and_disposals_are_empty(demo_db):
    """Restbuchwert und Darlehen kommen im Demobestand nicht vor (der Verkauf einer Anlage bucht keinen Restbuchwert)."""
    for year, month in [(2025, 6), (2025, 12), (2026, 3), (2026, 11)]:
        report = at(demo_db, year, month)
        assert all(report.line(key).values[period] == 0 for key in ('disposals', 'financing', 'financing_in_result', 'outside_other')
                   for period in ('month', 'ytd'))


def test_the_demo_unbooked_movements_are_counted(demo_db):
    report = at(demo_db, 2026, 11)
    assert report.line('unbooked').counts == {'month': 3, 'ytd': 3}
    assert line_label(report, report.line('unbooked')) == 'Bankbewegungen ohne Buchungssatz (3 im Monat, 3 aufgelaufen)'


@pytest.mark.parametrize('variant', ['jahr', 'vorjahr'])
def test_the_demo_lines_equal_the_sources(demo_db, variant):
    """Die Zeilen gleich ihrer Gegenrechnung aus ``get_euer_data`` und den Bereichen, Ergebnis, Abschreibungen und Darlehen im Ergebnis gleich der BWA, Ist +
    Überträge gleich der Karte ``get_dashboard`` derselben Spanne, die Bewegungen ohne Buchungssatz nie mehr als ihre ``unlinked_count`` – in jedem Monat."""
    for year, month in months_of(2025, 2026):
        if (year, month) <= (2026, 11):
            check_against_sources(demo_db, at(demo_db, year, month, variant), (variant, year, month))


# ═══ Abstimmung: SuSa-Welt und Testwelt ═══════════════════════════════════════
def test_the_susa_world_balances_but_for_the_clearing_account(susa_db):
    """In der SuSa-Welt steht „Nicht zugeordnet“ nur im März (und aufgelaufen ab März) mit +20,00: der Satz „Aufwand an Verrechnung“ (1460) vom 4. März, den die
    EÜR mit seinem Datum zählt und der kein Geld bewegt. Alles andere geht in jedem Monat 2025 und 2026 auf."""
    for year, month in months_of(2025, 2026):
        report = at(susa_db, year, month, 'vorjahr')
        expected = {'month': eur('20') if (year, month) == (2026, 3) else 0, 'ytd': eur('20') if year == 2026 and month >= 3 else 0,
                    'prev_month': 0, 'prev_ytd': 0}
        assert unassigned_of(report) == expected, (year, month)
        check_against_sources(susa_db, report, (year, month))


@pytest.mark.parametrize('month, period, expected', [
    (3, 'month', {'result': '93,05', 'computed': '93,05', 'unassigned': '20,00', 'actual': '113,05'}),
    (3, 'ytd', {'result': '396,35', 'withdrawals': '-80,00', 'computed': '316,35', 'unassigned': '20,00', 'actual': '336,35'}),
    (8, 'month', {'unbooked': '119,00', 'actual': '119,00'}),                                  # eine Bankbewegung ohne Buchungssatz, nicht gedeckt
    (8, 'ytd', {'result': '503,35', 'withdrawals': '-80,00', 'computed': '423,35', 'unbooked': '119,00', 'unassigned': '20,00', 'actual': '562,35'}),
    (12, 'month', {'result': '-400,00', 'depreciation': '400,00'}),                           # die Abschreibung bewegt kein Geld: Ist 0
    (12, 'ytd', {'result': '103,35', 'depreciation': '400,00', 'withdrawals': '-80,00', 'computed': '423,35', 'unbooked': '119,00', 'unassigned': '20,00',
                 'actual': '562,35'}),
])
def test_the_susa_world_shows_these_values(susa_db, month, period, expected):
    report = at(susa_db, 2026, month)
    got = {key: format_amount(report.line(key).values[period]) for key in SHOWN if report.line(key).values[period]}
    assert got == expected


def test_the_susa_world_transfers_leave_the_actual_unchanged(susa_db):
    """Ein Übertrag mit Spiegel auf beiden Bewegungen am selben Tag und einer mit satzloser Gegenseite (gedeckt) ändern das Ist nicht und heben sich in der
    Anmerkung auf (Juli, sie steht dort nicht); die Bewegung ohne Satz im August ist offen und gezählt."""
    july = at(susa_db, 2026, 7)
    assert shown(july) == {} and july.line('memo_transfers').values == {'month': 0, 'ytd': 0} and not visible(july, july.line('memo_transfers'))
    assert july.line('unbooked').counts == {'month': 0, 'ytd': 0}
    assert at(susa_db, 2026, 8).line('unbooked').counts == {'month': 1, 'ytd': 1}


def test_the_test_world_keeps_its_rests_as_the_yardstick(world_db):
    """Die Testwelt ist ein Bestand aus 2.0.x mit Spiegeln alter Art, unstimmigen Splits und Sätzen ohne Konto: Die Reste in „Nicht zugeordnet“ sind festgehalten,
    damit sich an ihnen nichts verschiebt. April −70,00 und Mai −55,00: eine Bankbewegung, deren Satz „Verrechnungskonto an Bank“ (1460) heißt – das
    Dashboard zählt sie als Ausgabe, die EÜR nichts; Juni −1,00: ein Satz „Aufwand an Verrechnung“ (+9,00, die EÜR zählt ihn, kein Geld) und eine Bankbewegung
    über −40,00, deren Sätze nur −30,00 ergeben (−10,00)."""
    rests = {month: unassigned_of(at(world_db, 2026, month)) for month in range(1, 13)}
    assert {month: value for month, value in rests.items() if value['month']} == {
        4: {'month': eur('-70'), 'ytd': eur('-70')}, 5: {'month': eur('-55'), 'ytd': eur('-125')}, 6: {'month': eur('-1'), 'ytd': eur('-126')}}
    assert [rests[month]['ytd'] for month in range(1, 13)] == [0, 0, 0, eur('-70'), eur('-125')] + [eur('-126')] * 7


@pytest.mark.parametrize('month, expected', [
    (4, {'result': '-1,35', 'computed': '-1,35', 'unbooked': '119,00', 'unassigned': '-70,00', 'actual': '47,65', 'memo_transfers': '-40,00'}),
    (5, {'result': '-38,40', 'computed': '-38,40', 'unassigned': '-55,00', 'actual': '-93,40', 'memo_transfers': '45,00'}),
    (6, {'result': '-39,00', 'computed': '-39,00', 'unassigned': '-1,00', 'actual': '-40,00'}),
])
def test_the_test_world_shows_these_values(world_db, month, expected):
    report = at(world_db, 2026, month)
    assert {key: format_amount(report.line(key).values['month']) for key in SHOWN if report.line(key).values['month']} == expected


def test_the_test_world_equals_the_sources_in_every_month(world_db):
    for variant in ('jahr', 'vorjahr'):
        for year, month in months_of(2026):
            check_against_sources(world_db, at(world_db, year, month, variant), (variant, year, month))


# ═══ Abstimmung: Bestände aus Zufall, nur über Wege der App ═══════════════════
@pytest.mark.parametrize('seed', [1, 2, 3, 4, 5, 6])
def test_not_assigned_is_zero_in_every_month_of_a_random_stock_made_with_the_ways_of_the_app(tmp_db, seed):
    """Eigenschaftsprüfung mit festem Seed über Bestände aus Erlös zu 7 und 19 %, Aufwand, Privat, Anlage mit Vorsteuer, Darlehen, Überträgen, Kassensätzen,
    Rechnungen mit Forderung und Zahlung und AfA: „Nicht zugeordnet“ ist in jedem Monat und aufgelaufen 0 Minor Units, und die Zeilen gleich ihrer
    Gegenrechnung aus den Quellen."""
    helpers.random_books(tmp_db, seed)
    for year, month in months_of(2025, 2026):
        report = at(tmp_db, year, month, 'vorjahr')
        assert unassigned_of(report) == dict.fromkeys(report.periods, 0), (seed, year, month)
        check_against_sources(tmp_db, report, (seed, year, month))


def test_the_random_stocks_use_nearly_every_kind_of_line(tmp_path):
    """Die Zufallsbestände decken zusammen die Zeilen ab, die ein Weg der App erreicht (nicht jede in jedem Bestand); „Übrige“, die übrigen Umbuchungen und der
    Restbuchwert bleiben leer – kein Weg der App erreicht sie hier."""
    seen = set()
    for seed in (1, 2, 3):
        db = Database(str(tmp_path / f'random{seed}.db'))
        helpers.random_books(db, seed)
        for year, month in months_of(2025, 2026):
            report = at(db, year, month, 'jahr')
            seen |= {key for key in SHOWN if report.line(key).values['month'] or report.line(key).values['ytd']}
    assert {'depreciation', 'assets', 'financing', 'financing_in_result', 'withdrawals', 'deposits', 'unbooked', 'memo_transfers'} <= seen
    assert 'outside_other' not in seen and 'disposals' not in seen and 'noncash_other' not in seen


# ═══ „Nicht zugeordnet“ und die eigenen Zeilen: je ein kleiner Bestand ═════════
def standalone(db, date, number, counter, amount, form, **extra):
    """Ein eigenständiger Satz ``number`` an ``counter`` (Kontonummern; ``amount`` Euro als Text) in der angegebenen Form."""
    return db.insert_booking(date, Decimal(amount), coa_id=coa(db, number), counter_coa_id=coa(db, counter), booking_type='entry', entry_form=form, **extra)


def bank_with_child(db, bank_date, bank_amount, child_date, child_amount, number, counter=1800, form='purpose_first', account='Probe-Giro', **extra):
    """Eine Bankbewegung auf ``account`` mit EINEM Satz darunter (Kontonummern; Beträge in Euro als Text); ``(Bankbewegung, Satz)`` als IDs."""
    bank = db.insert_booking(bank_date, Decimal(bank_amount), account_id=account_id(db, account), booking_type='bank')
    child = db.insert_booking(child_date, Decimal(child_amount), coa_id=coa(db, number), counter_coa_id=coa(db, counter), booking_type='entry',
                              parent_booking_id=bank, entry_form=form, **extra)
    return bank, child


def test_clearing_account_entry_and_its_bank_side_in_another_month(tmp_db):
    """1460: Der Satz „Aufwand an Verrechnung“ steht in der EÜR mit seinem Datum (März), die Bankseite („Verrechnung an Bank“) im Dashboard als Ausgabe nach dem
    Datum der Bewegung (April): März +x, April −x, aufgelaufen ab April 0."""
    two_banks(tmp_db)
    standalone(tmp_db, '2026-03-04', 6815, 1460, '-20.00', 'purpose_first', document_number='VR-1')
    mask(tmp_db, 'Probe-Giro', 1460, '-20.00', '2026-04-10')
    march, april = at(tmp_db, 2026, 3), at(tmp_db, 2026, 4)
    assert shown(march) == shown(march, 'ytd') == {'result': eur('-20'), 'computed': eur('-20'), 'unassigned': eur('20')}
    assert shown(april) == {'unassigned': eur('-20'), 'actual': eur('-20')}
    assert shown(april, 'ytd') == {'result': eur('-20'), 'computed': eur('-20'), 'actual': eur('-20')}
    for report in (march, april):
        check_against_sources(tmp_db, report)


def test_a_child_with_another_date_than_its_bank_movement_across_a_month_boundary(tmp_db):
    """Sektion 1 der EÜR zählt den Satz mit SEINEM Datum, das Dashboard die Bewegung mit ihrem: zwei Monate mit ±x, aufgelaufen ab April 0."""
    two_banks(tmp_db)
    bank_with_child(tmp_db, '2026-03-31', '-119.00', '2026-04-01', '-119.00', 6815, tax_rate=0.19, tax_amount=Decimal('-19.00'))
    march, april = at(tmp_db, 2026, 3), at(tmp_db, 2026, 4)
    assert shown(march) == shown(march, 'ytd') == {'unassigned': eur('-119'), 'actual': eur('-119')}
    assert shown(april) == {'result': eur('-119'), 'computed': eur('-119'), 'unassigned': eur('119')}
    assert shown(april, 'ytd') == {'result': eur('-119'), 'computed': eur('-119'), 'actual': eur('-119')}
    for report in (march, april):
        check_against_sources(tmp_db, report)


def test_an_unbalanced_split_leaves_its_rest_in_not_assigned(tmp_db):
    """Eine Bankbewegung über −100 mit einem Satz über −80: Das Dashboard zählt die ganze Bewegung, die EÜR nur den Satz – −20."""
    two_banks(tmp_db)
    bank_with_child(tmp_db, '2026-03-10', '-100.00', '2026-03-10', '-80.00', 6850)
    report = at(tmp_db, 2026, 3)
    assert shown(report) == shown(report, 'ytd') == {'result': eur('-80'), 'computed': eur('-80'), 'unassigned': eur('-20'), 'actual': eur('-100')}
    assert not balanced(report)
    check_against_sources(tmp_db, report)


def test_an_unbalanced_split_next_to_a_transfer_stands_under_the_transfers(tmp_db):
    """Die festgehaltene Grenze der Kontobewegungen: Trägt eine Bankbewegung einen Spiegel zwischen eigenen Konten, gehört ihr Betrag ohne die Wirkung der
    übrigen Sätze zum Übertrag – auch der Teil, den kein Satz erklärt. Eine Bewegung über −100 mit dem Spiegel über −60 (Gegenseite +60): Der Rest von −40 steht
    nachrichtlich unter den Überträgen, nicht in „Nicht zugeordnet“; die Prüfung der Splits nennt die Bewegung. Zu viel aufgeteilt (Spiegel −100 und ein Aufwand
    über −20): Der Aufwand steht im Ergebnis und in der Veränderung, die Überträge gleichen ihn nachrichtlich aus (+20)."""
    giro, _zweit = two_banks(tmp_db)
    mask(tmp_db, 'Probe-Giro', 1810, '-60.00', '2026-03-08')
    mask(tmp_db, 'Probe-Zweit', 1800, '60.00', '2026-03-08')
    raw(tmp_db, "UPDATE Bookings SET Amount = ? WHERE BookingType = 'bank' AND Account_ID = ?", eur('-100'), giro)
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'memo_transfers': eur('-40')} and balanced(report)
    assert len(tmp_db.find_unbalanced_splits('2026-03-01', '2026-03-31')) == 1
    check_against_sources(tmp_db, report)

    other = Database(str(tmp_db.db_name) + '.2')
    giro, _zweit = two_banks(other)
    mask(other, 'Probe-Giro', 1810, '-100.00', '2026-03-08')
    mask(other, 'Probe-Zweit', 1800, '100.00', '2026-03-08')
    with closing(sqlite3.connect(other.db_name)) as con:
        bank = con.execute("SELECT ID FROM Bookings WHERE BookingType = 'bank' AND Account_ID = ?", (giro,)).fetchone()[0]
    other.insert_booking('2026-03-08', Decimal('-20.00'), coa_id=coa(other, 6805), counter_coa_id=coa(other, 1800), booking_type='entry',
                         parent_booking_id=bank, entry_form='purpose_first')
    report = at(other, 2026, 3)
    assert shown(report) == {'result': eur('-20'), 'computed': eur('-20'), 'actual': eur('-20'), 'memo_transfers': eur('20')} and balanced(report)
    check_against_sources(other, report)


def test_a_tax_rate_the_euer_does_not_know_leaves_its_tax_in_not_assigned(tmp_db):
    """Ein Satz mit 10 % Steuer: Die Zeilen der EÜR führen Vorsteuer nur zu den Sätzen 5, 7, 16 und 19 % – seine Steuer steht dort nicht, das Konto hat sie
    bezahlt: −10."""
    two_banks(tmp_db)
    mask(tmp_db, 'Probe-Giro', 6815, '-110.00', '2026-03-08', '10', '-10.00')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('-100'), 'computed': eur('-100'), 'unassigned': eur('-10'), 'actual': eur('-110')}
    check_against_sources(tmp_db, report)


def test_a_direct_depreciation_against_the_bank_stays_in_the_result(tmp_db):
    """„6260 an Bank“ (Sofortabschreibung, in einem Zug bezahlt und abgeschrieben): Das Geld ist geflossen. Die EÜR liest den Satz als Zahlung, nicht als Umbuchung zwischen
    Anlage- und Erfolgskonto – die Zeile Abschreibungen rechnet nichts zurück, der Betrag bleibt im Ergebnis und steht im Ist, „Nicht zugeordnet“ ist 0."""
    two_banks(tmp_db)
    mask(tmp_db, 'Probe-Giro', 6260, '-400.00', '2026-03-05')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('-400'), 'computed': eur('-400'), 'actual': eur('-400')} and balanced(report)
    assert account_numbers(report, 'depreciation') == [] and values_of(report, 'depreciation', 'ytd') == 0
    check_against_sources(tmp_db, report)


def test_a_child_with_the_status_resolved_stands_in_not_assigned(tmp_db):
    """Ein Satz mit dem Status ``resolved`` steht nicht in den Zeilen der EÜR, in der Kontobewegung schon: −50."""
    two_banks(tmp_db)
    mask(tmp_db, 'Probe-Giro', 6815, '-50.00', '2026-03-05')
    raw(tmp_db, "UPDATE Bookings SET Status = 'resolved' WHERE BookingType = 'entry'")
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'unassigned': eur('-50'), 'actual': eur('-50')}
    check_against_sources(tmp_db, report)


def test_a_payment_to_a_balance_sheet_account_without_its_counter_booking_stands_in_not_assigned(tmp_db):
    """„Bank an 10000“ (Forderung, ein Bestandskonto) allein: Die Zeilen der EÜR enthalten keinen Betrag, die Kontobewegung zählt die Einnahme: +100."""
    two_banks(tmp_db)
    mask(tmp_db, 'Probe-Giro', 10000, '100.00', '2026-03-05')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'unassigned': eur('100'), 'actual': eur('100')}
    check_against_sources(tmp_db, report)


def test_bank_movements_without_a_booking_have_their_own_line_and_not_assigned_stays_zero(tmp_db):
    giro, _zweit = two_banks(tmp_db)
    tmp_db.insert_booking('2026-03-05', Decimal('77.00'), account_id=giro, booking_type='bank')
    tmp_db.insert_booking('2026-03-06', Decimal('-12.50'), account_id=giro, booking_type='bank')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == shown(report, 'ytd') == {'unbooked': eur('64.50'), 'actual': eur('64.50')}
    assert report.line('unbooked').counts == {'month': 2, 'ytd': 2} and balanced(report)
    assert line_label(report, report.line('unbooked')) == 'Bankbewegungen ohne Buchungssatz (2 im Monat, 2 aufgelaufen)'
    check_against_sources(tmp_db, report)


def test_a_covered_transfer_without_a_booking_and_a_cash_withdrawal_stand_only_as_a_note(tmp_db):
    """Die satzlose Gegenseite eines Übertrags (gedeckt) und eine Bargeldabhebung, deren Kassenseite das Kassenbuch nicht führt: nur in den nachrichtlichen
    Überträgen – „Nicht zugeordnet“ 0, das Ist ohne sie, die Bewegung zählt nicht als „ohne Buchungssatz“."""
    giro, zweit = two_banks(tmp_db)
    mask(tmp_db, 'Probe-Giro', 1810, '-300.00', '2026-03-08')
    tmp_db.insert_booking('2026-03-08', Decimal('300.00'), account_id=zweit, booking_type='bank')
    mask(tmp_db, 'Probe-Giro', 1600, '-200.00', '2026-03-12')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == shown(report, 'ytd') == {'memo_transfers': eur('-200')}
    assert report.line('unbooked').counts == {'month': 0, 'ytd': 0} and report.line('actual').values['month'] == 0
    assert report.line('memo_balance').values['month'] == eur('-200') and visible(report, report.line('memo_balance'))
    assert balanced(report)
    check_against_sources(tmp_db, report)


def test_loans_leave_the_result_and_come_back_as_financing(tmp_db):
    """Darlehen 3150 +10.000 und Tilgung −500 stehen im Ergebnis der BWA (Gruppe „Tilgungen und Verbindlichkeiten“); 3160 (−30) nicht. Zeile 4 nimmt 3150 heraus,
    Zeile 7 bringt alle Konten 3000–3799 zurück – die Überleitung geht auf."""
    two_banks(tmp_db)
    mask(tmp_db, 'Probe-Giro', 3150, '10000.00', '2026-03-02')
    mask(tmp_db, 'Probe-Giro', 3150, '-500.00', '2026-03-03')
    mask(tmp_db, 'Probe-Giro', 3160, '-30.00', '2026-03-04')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('9500'), 'financing_in_result': eur('-9500'), 'financing': eur('9470'), 'computed': eur('9470'),
                             'actual': eur('9470')}
    assert tmp_db.get_bwa(2026, 3, 'jahr').line('result').values['month'] == eur('9500')                       # das Ergebnis enthält 3150, wie die BWA
    assert [(a.number, a.values['month']) for a in report.line('financing').accounts] == [(3150, eur('9500')), (3160, eur('-30'))]
    assert [(a.number, a.values['month']) for a in report.line('financing_in_result').accounts] == [(3150, eur('-9500'))]
    assert balanced(report)
    check_against_sources(tmp_db, report)


def test_the_residual_book_value_of_an_asset_goes_back(tmp_db):
    """„6895 an 0520“ als Umbuchung (Restbuchwert beim Abgang): Aufwand ohne Geldfluss – Zeile 3 rechnet ihn zurück, „Nicht zugeordnet“ 0."""
    two_banks(tmp_db)
    standalone(tmp_db, '2026-03-31', 6895, 520, '300.00', 'transfer')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('-300'), 'disposals': eur('300')} and balanced(report)
    assert [(a.number, a.values['month']) for a in report.line('disposals').accounts] == [(6895, eur('300'))]
    check_against_sources(tmp_db, report)


def test_a_private_withdrawal_with_tax_is_net_and_its_tax_stands_in_the_result(tmp_db):
    """2100 mit 19 %: Die Entnahme steht netto da, die Vorsteuer als „Gezahlte Vorsteuer“ im Ergebnis – zusammen der Betrag der Bewegung."""
    two_banks(tmp_db)
    mask(tmp_db, 'Probe-Giro', 2100, '-119.00', '2026-03-05', '19', '-19.00')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('-19'), 'withdrawals': eur('-100'), 'computed': eur('-119'), 'actual': eur('-119')} and balanced(report)
    check_against_sources(tmp_db, report)


def test_a_depreciation_booked_with_the_cash_account_as_its_account_stands_in_not_assigned(tmp_db):
    """Eine Umbuchung „6220 an 0520“ mit der Kasse als ``Account_ID``: In der EÜR ist sie Abschreibung ohne Geldfluss (Sektion 3), im Dashboard ein Satz der
    Kasse (+300) – das Ergebnis geht zurück, das Ist hat den Betrag: +300. Ebenso der Restbuchwert."""
    two_banks(tmp_db)
    kasse = account_id(tmp_db, 'Kasse')
    standalone(tmp_db, '2026-03-31', 6220, 520, '300.00', 'transfer', account_id=kasse)
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('-300'), 'depreciation': eur('300'), 'unassigned': eur('300'), 'actual': eur('300')}
    check_against_sources(tmp_db, report)
    other = Database(str(tmp_db.db_name) + '.2')
    two_banks(other)
    standalone(other, '2026-03-31', 6895, 520, '300.00', 'transfer', account_id=account_id(other, 'Kasse'))
    report = at(other, 2026, 3)
    assert shown(report) == {'result': eur('-300'), 'disposals': eur('300'), 'unassigned': eur('300'), 'actual': eur('300')}
    check_against_sources(other, report)


def test_a_legacy_mirror_under_a_movement_is_read_by_the_dashboard_but_not_by_the_euer(tmp_db):
    """Ein Satz in Spiegel-Form alter Art (``mirror_legacy``, Lesart unbekannt) unter einer Bankbewegung: Zwischen eigenen Konten ist er ein Übertrag (nur die
    Anmerkung, „Nicht zugeordnet“ 0); mit dem Verrechnungskonto 1460 auf einer Seite zählt die Kontobewegung als Ausgabe, die Zeilen der EÜR enthalten keinen
    Spiegel – −50."""
    two_banks(tmp_db)
    bank_with_child(tmp_db, '2026-03-10', '-50.00', '2026-03-10', '-50.00', 1800, 1810, form='mirror_legacy')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'memo_transfers': eur('-50')} and balanced(report)
    check_against_sources(tmp_db, report)
    other = Database(str(tmp_db.db_name) + '.2')
    two_banks(other)
    bank_with_child(other, '2026-03-10', '-50.00', '2026-03-10', '-50.00', 1460, 1800, form='mirror_legacy')
    report = at(other, 2026, 3)
    assert shown(report) == {'unassigned': eur('-50'), 'actual': eur('-50')}
    check_against_sources(other, report)


def test_a_rest_below_one_cent_is_not_zero_and_stands_there_as_a_rounding_rest(tmp_db):
    """Ein Satz unter einem Cent (−0,0049 €, per SQL-Konsole schreibbar): Die Zeilen der EÜR führen Salden unter 0,01 € nicht, die Kontobewegung zählt ihn – der Rest
    ist nicht 0, steht als 0,00 da und wird als Rundungsrest genannt, nicht geglättet."""
    giro, _zweit = two_banks(tmp_db)
    bank = raw(tmp_db, "INSERT INTO Bookings (DateBooking, Account_ID, Amount, BookingType, Currency) VALUES ('2026-03-10', ?, -49, 'bank', 'EUR')", giro)
    raw_entry(tmp_db, '2026-03-10', 6815, 1800, -49, 'purpose_first', parent=bank)
    report = at(tmp_db, 2026, 3)
    assert unassigned_of(report) == {'month': -49, 'ytd': -49} and not balanced(report) and rounding_remainder(report)
    assert cells(report, report.line('unassigned').values) == ['0,00', '0,00']
    check_against_sources(tmp_db, report)


def test_a_transfer_between_an_asset_account_and_another_expense_account_goes_back_as_another_transfer(tmp_db):
    """„6815 an 0520“ (Umbuchung ohne Geldfluss auf einem Konto, das keine Abschreibung und kein Restbuchwert ist): Die EÜR zählt sie als Aufwand, im Ist steht nichts – die
    Zeile „Übrige Umbuchungen ohne Geldfluss“ rechnet sie zurück, „Nicht zugeordnet“ ist 0."""
    two_banks(tmp_db)
    standalone(tmp_db, '2026-03-31', 6815, 520, '300.00', 'transfer')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('-300'), 'noncash_other': eur('300')} and balanced(report)        # computed und actual sind 0
    assert [(a.number, a.values['month']) for a in report.line('noncash_other').accounts] == [(6815, eur('300'))]
    assert values_of(report, 'depreciation') == 0 and values_of(report, 'disposals') == 0 and visible(report, report.line('noncash_other'))
    check_against_sources(tmp_db, report)


def test_payments_the_euer_counts_as_expense_are_not_a_rest(tmp_db):
    """Zahlungen auf 1000–1399 (hier ein Altkonto), die Umsatzsteuer-Zahlung und der Verkauf einer Anlage (4845, mit Geldfluss) stehen im Ergebnis und im Ist –
    sie brauchen keine eigene Zeile und lassen keinen Rest."""
    two_banks(tmp_db)
    book(tmp_db, 1050, '-250.00', '2026-03-20', name='Altkonto')
    book(tmp_db, 3820, '-80.00', '2026-03-21')
    book(tmp_db, 4845, '595.00', '2026-03-22', tax_rate='19', tax_amount='95.00')
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('-250') - eur('80') + eur('595'), 'computed': eur('265'), 'actual': eur('265')} and balanced(report)
    check_against_sources(tmp_db, report)


# ═══ Umbuchungen ohne Geldfluss: get_euer_data(only_asset_transfers=True) und die Zeilen der Überleitung ═══
def old_form_depreciation(db, date, number, amount, account=None):
    """Eine AfA-Buchung alter Form (bis 2.0.4): Aufwandskonto ohne Gegenkonto, von der Planzeile einer Anlage referenziert (``Booking_ID`` zeigt auf sie); ``Buchung`` als ID."""
    asset = db.insert_asset('Altlaptop', f'{date[:4]}-01-10', 1800.0, 3, coa_id=coa(db, 520))
    booking = db.insert_booking(date, Decimal(amount), account_id=account, coa_id=coa(db, number), booking_type='entry')
    raw(db, "UPDATE Bookings SET Status = 'posted' WHERE ID = ?", booking)
    raw(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Booking_ID, Status, BookedAt) "
            "VALUES (?, ?, 6000000, 12000000, ?, 'posted', CURRENT_TIMESTAMP)", asset, int(date[:4]), booking)
    return booking


def mixed_books(db):
    """Ein kleiner Bestand von Hand, in dem der Test die Umbuchungen selbst anlegt und ihre IDs kennt – daneben Zahlungen auf dieselben Konten.

    Umbuchungen zwischen Anlage- und Erfolgskonto (ihre IDs stehen im Ergebnis): die AfA der App „6220 an 0520“ (31.12., 400,00), der Restbuchwert „6895 an 0520“ (300,00),
    „6815 an 0520“ (120,00) und eine AfA alter Form auf 6222 (Aufwandskonto ohne Gegenkonto, von einer Planzeile referenziert, −600,00). Zahlungen, die NICHT dazugehören:
    „6260 an Bank“ (−400,00), „6220 an Bank“ (−150,00) und „6220 an Kasse“ (−80,00, ein Kassensatz ohne Bankbewegung)."""
    two_banks(db)
    asset = db.insert_asset('Rechner', '2026-01-02', 1200.0, 3)
    ids = {'afa': db.book_depreciation(asset, 2026, coa_id_expense=coa(db, 6220), coa_id_asset=coa(db, 520)),
           'book_value': standalone(db, '2026-03-31', 6895, 520, '300.00', 'transfer'),
           'other': standalone(db, '2026-04-30', 6815, 520, '120.00', 'transfer'),
           'old_form': old_form_depreciation(db, '2026-06-30', 6222, '-600.00')}
    mask(db, 'Probe-Giro', 6260, '-400.00', '2026-03-05')
    mask(db, 'Probe-Giro', 6220, '-150.00', '2026-03-08')
    db.insert_booking('2026-03-06', Decimal('-80.00'), coa_id=coa(db, 6220), counter_coa_id=coa(db, 1600), booking_type='entry', document_number='KS-1')
    return ids


def copy_without(db, path, booking_ids):
    """Eine Kopie der Datenbank (SQLite-Sicherung), in der genau die Buchungen ``booking_ids`` gelöscht sind – so liest ``get_euer_data`` den Bestand ohne sie."""
    booking_ids = list(booking_ids)
    with closing(sqlite3.connect(db.db_name)) as source, closing(sqlite3.connect(str(path))) as target:
        source.backup(target)
    with closing(sqlite3.connect(str(path))) as con:
        con.execute(f"DELETE FROM Bookings WHERE ID IN ({','.join('?' * len(booking_ids))})", booking_ids)
        con.commit()
    return Database(str(path))


def test_the_default_of_get_euer_data_is_the_same_with_the_switch_off(susa_db, world_db, demo_db):
    """Der Schalter ändert den Standard nicht: ohne ihn und mit ``only_asset_transfers=False`` sind es dieselben Zeilen – in jedem Monat, aufgelaufen und im Vorjahr der
    SuSa-Welt, der Testwelt und des Demobestands, auch mit einem Kontofilter (die Schnappschüsse von EÜR-Kasten, BWA-PDF und Export prüfen ``test_euer_view.py``,
    ``test_bwa_pdf.py`` und ``test_susa.py``)."""
    for name, db in (('susa', susa_db), ('test', world_db), ('demo', demo_db)):
        accounts = [[account[0]] for account in db.fetch_accounts()][:2]
        for year, month in months_of(2025, 2026):
            for span in bwa.bwa_periods(year, month).values():
                assert db.get_euer_data(*span) == db.get_euer_data(*span, only_asset_transfers=False), (name, span)
                for chosen in accounts:
                    assert db.get_euer_data(*span, chosen) == db.get_euer_data(*span, chosen, only_asset_transfers=False), (name, span, chosen)


def test_the_euer_rows_split_into_the_transfers_and_the_rest(tmp_db, tmp_path):
    """Die Zerlegung, auf den Minor-Wert und je Konto: Saldo(alle Zeilen) − Saldo(nur Umbuchungen) = Saldo eines Bestands, in dem genau die Umbuchungen gelöscht sind
    (eine Kopie der Datenbank ohne ihre IDs). Im Bestand von Hand stehen die AfA der App, der Restbuchwert „6895 an 0520“, „6815 an 0520“ und eine AfA alter Form
    neben „6260 an Bank“, „6220 an Bank“ und „6220 an Kasse“ – die Zahlungen gehören nicht zu den Umbuchungen, auch nicht auf demselben Konto 6220."""
    ids = mixed_books(tmp_db)
    rest = copy_without(tmp_db, tmp_path / 'ohne_umbuchungen.db', ids.values())
    year = ('2026-01-01', '2026-12-31')
    assert rows_by_number(rest, year) == {6220: eur('-230'), 6260: eur('-400')}                                    # was übrig bleibt: nur die Zahlungen
    assert rows_by_number(tmp_db, year, only_asset_transfers=True) == {6220: eur('-400'), 6222: eur('-600'), 6895: eur('-300'), 6815: eur('-120')}
    assert rows_by_number(tmp_db, year) == {6220: eur('-630'), 6222: eur('-600'), 6260: eur('-400'), 6895: eur('-300'), 6815: eur('-120')}
    months = {month: bwa.bwa_periods(2026, month)['month'] for month in range(1, 13)}
    for span in [year, ('2025-01-01', '2025-12-31'), *months.values()]:
        whole, moved, others = rows_by_number(tmp_db, span), rows_by_number(tmp_db, span, only_asset_transfers=True), rows_by_number(rest, span)
        for number in set(whole) | set(moved) | set(others):
            assert whole.get(number, 0) - moved.get(number, 0) == others.get(number, 0), (span, number)
    by_month = {month: rows_by_number(tmp_db, span, only_asset_transfers=True) for month, span in months.items()}
    assert {month: rows for month, rows in by_month.items() if rows} == {3: {6895: eur('-300')}, 4: {6815: eur('-120')}, 6: {6222: eur('-600')},
                                                                         12: {6220: eur('-400')}}                       # und in keinem anderen Monat eine Zeile


def test_the_transfers_are_empty_with_an_account_filter_and_not_with_all_accounts(tmp_db):
    """Mit Kontofilter läuft Sektion 3 nicht (die eigenständigen Umbuchungen hängen an keinem Konto): In diesem Bestand – keine Umbuchung steht unter einer Bankbewegung –
    ist das Ergebnis leer, auch wenn der Filter nur Konten nennt, deren Sätze die EÜR zählt. Sind alle Konten gewählt, gilt der Filter nicht: dieselben Zeilen wie ohne."""
    mixed_books(tmp_db)
    giro = account_id(tmp_db, 'Probe-Giro')
    year = ('2026-01-01', '2026-12-31')
    assert tmp_db.get_euer_data(*year, [giro]) and tmp_db.get_euer_data(*year, [giro], only_asset_transfers=True) == []
    every = [account[0] for account in tmp_db.fetch_accounts()]
    assert tmp_db.get_euer_data(*year, every, only_asset_transfers=True) == tmp_db.get_euer_data(*year, only_asset_transfers=True) != []
    assert tmp_db.get_euer_data(*year, every) == tmp_db.get_euer_data(*year)


def child(db, bank, date, amount, number, counter, form, **extra):
    """Ein weiterer Satz ``number`` an ``counter`` unter der Bankbewegung ``bank`` (Kontonummern; ``amount`` Euro als Text)."""
    return db.insert_booking(date, Decimal(amount), coa_id=coa(db, number), counter_coa_id=coa(db, counter), booking_type='entry',
                             parent_booking_id=bank, entry_form=form, **extra)


def test_a_transfer_under_a_bank_movement_moves_no_money_either(tmp_db):
    """Ein Satz „6220 an 0520“ unter einer Bankbewegung (``ParentBooking_ID`` gesetzt) findet die EÜR in Sektion 1 – er bleibt eine Umbuchung zwischen Anlage- und
    Erfolgskonto, seine Wirkung auf das Konto der Bewegung ist 0, und er gehört zu den Umbuchungen ohne Geldfluss. Trägt die Bewegung (−300) NUR diesen Satz, ist ihr
    Betrag unerklärt: ein unstimmiger Split, dessen Rest in „Nicht zugeordnet“ steht (die Prüfung der Splits nennt die Bewegung) – er hebt sich nicht still mit der
    Abschreibung auf."""
    two_banks(tmp_db)
    bank_with_child(tmp_db, '2026-03-10', '-300.00', '2026-03-10', '300.00', 6220, 520, form='transfer')
    span = ('2026-03-01', '2026-03-31')
    assert rows_by_number(tmp_db, span) == rows_by_number(tmp_db, span, only_asset_transfers=True) == {6220: eur('-300')}
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('-300'), 'depreciation': eur('300'), 'unassigned': eur('-300'), 'actual': eur('-300')}
    assert not balanced(report) and len(tmp_db.find_unbalanced_splits(*span)) == 1
    check_against_sources(tmp_db, report)


def test_with_an_account_filter_only_the_transfers_under_its_bank_movements_remain(tmp_db):
    """Mit Kontofilter bleiben nur die Umbuchungen unter den Bankbewegungen der gewählten Konten (Sektion 1); die eigenständigen (Sektion 3) hängen an keinem Konto."""
    giro, zweit = two_banks(tmp_db)
    bank, _cost = bank_with_child(tmp_db, '2026-03-10', '-50.00', '2026-03-10', '-50.00', 6855)
    child(tmp_db, bank, '2026-03-10', '500.00', 6895, 520, 'transfer')
    standalone(tmp_db, '2026-03-31', 6220, 520, '400.00', 'transfer')
    span = ('2026-03-01', '2026-03-31')
    assert rows_by_number(tmp_db, span, only_asset_transfers=True) == {6220: eur('-400'), 6895: eur('-500')}
    assert {number: to_minor(total) for number, _name, total in tmp_db.get_euer_data(*span, [giro], only_asset_transfers=True)} == {6895: eur('-500')}
    assert tmp_db.get_euer_data(*span, [zweit], only_asset_transfers=True) == []


def test_the_book_value_in_the_split_of_a_sale_goes_back(tmp_db):
    """Der Verkauf einer Anlage in EINEM Beleg: Die Bank bekommt 1.190,00, darunter der Erlös „6885 an Bank“ (19 %) und – als Umbuchung ohne Geldfluss im selben Split –
    der Restbuchwert „6895 an 0520“ über 500,00. Der Split ist stimmig; der Restbuchwert steht im Ergebnis und kommt in der Zeile Anlagenabgänge zurück, die Überleitung
    geht auf."""
    two_banks(tmp_db)
    bank, _sale = bank_with_child(tmp_db, '2026-03-10', '1190.00', '2026-03-10', '1190.00', 6885, tax_rate=0.19, tax_amount=Decimal('190.00'))
    child(tmp_db, bank, '2026-03-10', '500.00', 6895, 520, 'transfer')
    span = ('2026-03-01', '2026-03-31')
    assert tmp_db.find_unbalanced_splits(*span) == []
    assert rows_by_number(tmp_db, span, only_asset_transfers=True) == {6895: eur('-500')}
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('690'), 'disposals': eur('500'), 'computed': eur('1190'), 'actual': eur('1190')} and balanced(report)
    check_against_sources(tmp_db, report)


def test_a_small_asset_bought_and_written_off_in_one_split_goes_back(tmp_db):
    """Ein geringwertiges Wirtschaftsgut in einem Beleg: Die Bank zahlt 800,00 auf das Anlagekonto („0670 an Bank“), und im selben Split steht die Sofortabschreibung als
    Umbuchung „6260 an 0670“. Geld ist nur einmal geflossen – als Anlagenzugang; die Abschreibung bewegt keines und kommt zurück."""
    two_banks(tmp_db)
    bank, _asset = bank_with_child(tmp_db, '2026-03-10', '-800.00', '2026-03-10', '-800.00', 670)
    child(tmp_db, bank, '2026-03-10', '800.00', 6260, 670, 'transfer')
    span = ('2026-03-01', '2026-03-31')
    assert tmp_db.find_unbalanced_splits(*span) == []
    assert rows_by_number(tmp_db, span, only_asset_transfers=True) == {6260: eur('-800')}
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {'result': eur('-800'), 'depreciation': eur('800'), 'assets': eur('-800'), 'computed': eur('-800'), 'actual': eur('-800')}
    assert balanced(report)
    check_against_sources(tmp_db, report)


def test_a_depreciation_assigned_to_a_bank_movement_still_goes_back(tmp_db):
    """Eine eigenständige Abschreibung „6220 an 0520“ (400,00), im Bank-Editor unter eine Bewegung gehängt, die ihr eigener Satz „6855 an Bank“ (−50,00) schon ganz erklärt:
    Sie zählt nicht in die Summe der Bewegung und bewegt weiter kein Geld – vor und nach dem Zuordnen dieselbe Überleitung."""
    two_banks(tmp_db)
    bank, _cost = bank_with_child(tmp_db, '2026-03-10', '-50.00', '2026-03-10', '-50.00', 6855)
    depreciation = standalone(tmp_db, '2026-03-31', 6220, 520, '400.00', 'transfer')
    expected = {'result': eur('-450'), 'depreciation': eur('400'), 'computed': eur('-50'), 'actual': eur('-50')}
    report = at(tmp_db, 2026, 3)
    assert shown(report) == expected and balanced(report)
    raw(tmp_db, 'UPDATE Bookings SET ParentBooking_ID = ? WHERE ID = ?', bank, depreciation)
    assert tmp_db.find_unbalanced_splits('2026-03-01', '2026-03-31') == []
    report = at(tmp_db, 2026, 3)
    assert shown(report) == expected and balanced(report)
    check_against_sources(tmp_db, report)


def test_a_resolved_transfer_is_in_no_section(tmp_db):
    """Ein Satz mit dem Status ``resolved`` steht in keiner Sektion – auch nicht in den Umbuchungen."""
    two_banks(tmp_db)
    standalone(tmp_db, '2026-03-31', 6220, 520, '300.00', 'transfer')
    span = ('2026-03-01', '2026-03-31')
    assert rows_by_number(tmp_db, span, only_asset_transfers=True) == {6220: eur('-300')}
    raw(tmp_db, "UPDATE Bookings SET Status = 'resolved' WHERE BookingType = 'entry'")
    assert rows_by_number(tmp_db, span) == {} and rows_by_number(tmp_db, span, only_asset_transfers=True) == {}
    report = at(tmp_db, 2026, 3)
    assert shown(report) == {} and balanced(report)


def test_a_transfer_that_an_earlier_section_finds_first_is_not_included(tmp_db):
    """Jede Buchung zählt höchstens einmal, die erste Sektion gewinnt: Eine AfA-Buchung alter Form über das Verrechnungskonto („6220 an 1460“, von einer Planzeile
    referenziert) findet Sektion 2 über das Konto, bevor Sektion 3 sie über die Planzeile findet – die EÜR zählt sie einmal, zu den Umbuchungen ohne Geldfluss gehört sie
    nicht (das Geld verlässt das Konto erst mit der Bankbewegung „Verrechnung an Bank“)."""
    two_banks(tmp_db)
    asset = tmp_db.insert_asset('Altlaptop', '2026-01-10', 1800.0, 3, coa_id=coa(tmp_db, 520))
    booking = standalone(tmp_db, '2026-12-31', 6220, 1460, '-600.00', 'purpose_first')
    raw(tmp_db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Booking_ID, Status, BookedAt) "
                "VALUES (?, 2026, 6000000, 12000000, ?, 'posted', CURRENT_TIMESTAMP)", asset, booking)
    year = ('2026-01-01', '2026-12-31')
    assert rows_by_number(tmp_db, year) == {6220: eur('-600')} and rows_by_number(tmp_db, year, only_asset_transfers=True) == {}


def test_a_direct_depreciation_and_one_of_the_app_in_the_same_month_only_the_second_goes_back(tmp_db):
    """Im Dezember die AfA der App (eine Umbuchung ohne Geldfluss) und „6260 an Bank“ (−400): Die Zeile Abschreibungen rechnet nur die AfA zurück, die Zahlung bleibt im
    Ergebnis und im Ist – „Nicht zugeordnet“ ist 0, und im Kontennachweis steht nur das Konto der AfA."""
    two_banks(tmp_db)
    asset = tmp_db.insert_asset('Rechner', '2026-01-02', 1200.0, 3)
    tmp_db.book_depreciation(asset, 2026, coa_id_expense=coa(tmp_db, 6220), coa_id_asset=coa(tmp_db, 520))
    mask(tmp_db, 'Probe-Giro', 6260, '-400.00', '2026-12-05')
    report = at(tmp_db, 2026, 12)
    assert shown(report) == {'result': eur('-800'), 'depreciation': eur('400'), 'computed': eur('-400'), 'actual': eur('-400')} and balanced(report)
    assert [(a.number, a.values['month']) for a in report.line('depreciation').accounts] == [(6220, eur('400'))]
    check_against_sources(tmp_db, report)


def test_the_account_list_of_a_line_shows_only_the_transfer_part_of_an_account(tmp_db):
    """„6220 an Bank“ (direkt bezahlt, −150) und dieselbe Kontonummer mit einer Umbuchung (die AfA der App, 400): Der Saldo der EÜR-Zeile 6220 ist −550, der Kontennachweis der
    Zeile Abschreibungen nennt nur den Umbuchungsbetrag (400) – die 150 bleiben im Ergebnis, die Überleitung geht auf."""
    two_banks(tmp_db)
    asset = tmp_db.insert_asset('Rechner', '2026-01-02', 1200.0, 3)
    tmp_db.book_depreciation(asset, 2026, coa_id_expense=coa(tmp_db, 6220), coa_id_asset=coa(tmp_db, 520))
    mask(tmp_db, 'Probe-Giro', 6220, '-150.00', '2026-12-05')
    report = at(tmp_db, 2026, 12)
    assert rows_by_number(tmp_db, ('2026-12-01', '2026-12-31')) == {6220: eur('-550')}
    assert shown(report) == {'result': eur('-550'), 'depreciation': eur('400'), 'computed': eur('-150'), 'actual': eur('-150')} and balanced(report)
    assert [(a.number, a.name, a.values['month']) for a in report.line('depreciation').accounts] == [
        (6220, 'Abschreibungen auf Sachanlagen (ohne AfA auf Fahrzeuge und Gebäude)', eur('400'))]
    check_against_sources(tmp_db, report)


def test_a_disposal_through_the_clearing_account_is_no_transfer_without_cash(tmp_db):
    """Zwei eigenständige Sätze am selben Tag: „6895 an 1460“ (−300, ``purpose_first``: der Restbuchwert als Aufwand über das Verrechnungskonto) und „1460 an 0520“ (+300,
    ``liquid_first``: der Anlagenabgang gegen das Anlagekonto). Keiner ist eine Umbuchung zwischen Anlage- und Erfolgskonto – die EÜR zählt den Aufwand im Ergebnis und die
    Gegenseite auf dem Anlagekonto, die Zeile Anlagenabgänge rechnet nichts zurück: Der Aufwand (−300) und die Anlagenzugänge (+300) heben sich auf, das Ist ist 0 und die
    Überleitung geht auf."""
    two_banks(tmp_db)
    standalone(tmp_db, '2026-03-31', 6895, 1460, '-300.00', 'purpose_first')
    standalone(tmp_db, '2026-03-31', 1460, 520, '300.00', 'liquid_first')
    report = at(tmp_db, 2026, 3)
    assert rows_by_number(tmp_db, ('2026-03-01', '2026-03-31'), only_asset_transfers=True) == {}
    assert shown(report) == {'result': eur('-300'), 'assets': eur('300')} and balanced(report)
    assert values_of(report, 'disposals') == 0 and account_numbers(report, 'disposals') == []
    check_against_sources(tmp_db, report)


def test_the_mixed_books_balance_in_every_month(tmp_db):
    """Der Bestand von Hand (Umbuchungen und Zahlungen auf denselben Konten) geht in jedem Monat und aufgelaufen auf; die Zeilen gleich ihrer Gegenrechnung aus den
    Quellen – die AfA der App und die alter Form stehen unter Abschreibungen, der Restbuchwert unter den Anlagenabgängen, „6815 an 0520“ unter den übrigen Umbuchungen."""
    mixed_books(tmp_db)
    for month in range(1, 13):
        report = at(tmp_db, 2026, month, 'vorjahr')
        assert unassigned_of(report) == dict.fromkeys(report.periods, 0), month
        check_against_sources(tmp_db, report, month)
    december = at(tmp_db, 2026, 12)
    assert [(a.number, a.values['ytd']) for a in december.line('depreciation').accounts] == [(6220, eur('400')), (6222, eur('600'))]
    assert [(a.number, a.values['ytd']) for a in december.line('disposals').accounts] == [(6895, eur('300'))]
    assert [(a.number, a.values['ytd']) for a in december.line('noncash_other').accounts] == [(6815, eur('120'))]


# ═══ Nur lesend ═══════════════════════════════════════════════════════════════
def test_get_liquidity_and_the_page_change_nothing(susa_db):
    before = dump(susa_db.db_name)
    for variant in ('jahr', 'vorjahr'):
        for month in (1, 3, 8, 12):
            at(susa_db, 2026, month, variant)
            PageLiquidity(susa_db, BwaQuery(2026, month, variant))
    assert dump(susa_db.db_name) == before


# ═══ Seite ════════════════════════════════════════════════════════════════════
def table_text(page):
    """Der sichtbare Text der Tabelle: eine Zeile je ``tr``, die Zellen durch ``|`` getrennt."""
    body = page[page.index('<table class="bwaTable">'):page.index('</table>')]
    rows = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', body, flags=re.S):
        found = [html_lib.unescape(re.sub(r'<[^>]+>', '', cell)).strip() for cell in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S)]
        rows.append(' | '.join(found))
    return rows


def visible_text(page):
    return html_lib.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'<script>.*?</script>', '', page, flags=re.S)))


def test_the_page_shows_the_overview_of_a_month_with_a_rest(susa_db):
    page = PageLiquidity(susa_db, BwaQuery(2026, 3, 'jahr'))
    assert table_text(page) == [
        ' | März 2026 | Januar bis März 2026', 'Position | Betrag € | Betrag €',
        '▶ Ergebnis (wie BWA) | 93,05 | 396,35', '▶ Abschreibungen | 0,00 | 0,00', '▶ Anlagenabgänge zum Restbuchwert | 0,00 | 0,00',
        '▶ Darlehen und Verbindlichkeiten im Ergebnis (herausgerechnet) | 0,00 | 0,00',
        'Ergebnis vor Abschreibungen und Darlehen | 93,05 | 396,35',
        '▶ Anlagenzugänge (Zahlungen auf Anlagekonten) | 0,00 | 0,00', '▶ Darlehen und Verbindlichkeiten (Aufnahme +, Tilgung −) | 0,00 | 0,00',
        '▶ Privatentnahmen | 0,00 | -80,00', '2100 Privatentnahmen allgemein |  | -80,00', '▶ Privateinlagen | 0,00 | 0,00',
        'Veränderung der flüssigen Mittel laut Überleitung | 93,05 | 316,35',
        '▶ Bankbewegungen ohne Buchungssatz | 0,00 | 0,00', '▶ Nicht zugeordnet | 20,00 | 20,00',
        'Veränderung der flüssigen Mittel (ohne Überträge) | 113,05 | 336,35']
    assert 'Liquiditätsrechnung – März 2026' in page
    for text in (HINT_TEXT, SIGN_TEXT, METHOD_TEXT, UNBALANCED_TEXT, UNASSIGNED_TEXT):
        assert html_lib.escape(text, quote=True) in page, text
    assert BALANCED_TEXT not in page and ROUNDING_TEXT not in page and TRANSFER_TEXT not in page


def test_the_row_not_assigned_is_colored_only_when_it_has_a_value(susa_db):
    colored = PageLiquidity(susa_db, BwaQuery(2026, 3, 'jahr'))
    assert re.search(r'<tr class="warnColor"><td><span class="split-toggle-icon bwaNoIcon">▶</span> Nicht zugeordnet</td>', colored)
    quiet = PageLiquidity(susa_db, BwaQuery(2026, 2, 'jahr'))
    assert 'warnColor' not in quiet and BALANCED_TEXT in quiet and UNBALANCED_TEXT not in quiet and UNASSIGNED_TEXT not in quiet
    assert 'Nicht zugeordnet | 0,00 | 0,00' in table_text(quiet)[-2]


def test_the_comparison_page_shows_the_previous_year_and_the_count_of_unbooked_movements(susa_db):
    rows = table_text(PageLiquidity(susa_db, BwaQuery(2026, 8, 'vorjahr')))
    assert rows[0] == ' | August | Januar bis August'
    assert rows[1] == 'Position | 2026 | 2025 | Veränderung € | Veränderung % | 2026 | 2025 | Veränderung € | Veränderung %'
    assert '▶ Bankbewegungen ohne Buchungssatz (1 im Monat, 1 aufgelaufen) | 119,00 | 0,00 | 119,00 |  | 119,00 | 0,00 | 119,00 | ' in rows
    assert '▶ Ergebnis (wie BWA) | 0,00 | 0,00 | 0,00 |  | 503,35 | 59,50 | 443,85 | 745,97' in rows


def test_the_transfers_stand_below_the_table_as_a_note_with_their_sentence(demo_db, tmp_db):
    page = PageLiquidity(demo_db, BwaQuery(2026, 3, 'jahr'))
    rows = table_text(page)
    assert rows[-2] == 'Nachrichtlich: Überträge zwischen eigenen Konten | -200,00 | -200,00'
    assert rows[-1] == 'Nachrichtlich: Veränderung laut Kontobewegungen | -11.076,08 | 2.045,61'
    assert TRANSFER_TEXT in page and BALANCED_TEXT in page
    quiet = PageLiquidity(tmp_db, BwaQuery(2026, 3, 'jahr'))
    assert TRANSFER_TEXT not in quiet and not any(row.startswith('Nachrichtlich') for row in table_text(quiet))


def test_the_page_names_a_rounding_rest_and_not_a_real_one(tmp_db):
    giro, _zweit = two_banks(tmp_db)
    bank = raw(tmp_db, "INSERT INTO Bookings (DateBooking, Account_ID, Amount, BookingType, Currency) VALUES ('2026-03-10', ?, -49, 'bank', 'EUR')", giro)
    raw_entry(tmp_db, '2026-03-10', 6815, 1800, -49, 'purpose_first', parent=bank)
    page = PageLiquidity(tmp_db, BwaQuery(2026, 3, 'jahr'))
    assert 'Nicht zugeordnet | 0,00 | 0,00' in table_text(page)[-2] and UNBALANCED_TEXT in page and ROUNDING_TEXT in page and BALANCED_TEXT not in page
    mask(tmp_db, 'Probe-Giro', 10000, '100.00', '2026-03-05')
    assert ROUNDING_TEXT not in PageLiquidity(tmp_db, BwaQuery(2026, 3, 'jahr')) and UNBALANCED_TEXT in PageLiquidity(tmp_db, BwaQuery(2026, 3, 'jahr'))


def test_the_row_of_the_other_transfers_without_cash_stands_on_the_page_only_with_a_value(tmp_db):
    """„Übrige Umbuchungen ohne Geldfluss“ steht auf der Seite nur mit einem Wert ≠ 0 – mit ihrem Konto zum Aufklappen (``noncash_other``) –, sonst fehlt die Zeile; eine
    Abschreibung als Umbuchung (Zeile „Abschreibungen“) ruft sie nicht hervor."""
    two_banks(tmp_db)
    label = 'Übrige Umbuchungen ohne Geldfluss'
    quiet = PageLiquidity(tmp_db, BwaQuery(2026, 3, 'jahr'))
    assert label not in quiet and 'noncash_other' not in quiet
    asset = tmp_db.insert_asset('Rechner', '2026-01-02', 1200.0, 3)
    tmp_db.book_depreciation(asset, 2026, coa_id_expense=coa(tmp_db, 6220), coa_id_asset=coa(tmp_db, 520))
    standalone(tmp_db, '2026-12-31', 6815, 520, '300.00', 'transfer')
    depreciation_only = PageLiquidity(tmp_db, BwaQuery(2026, 11, 'jahr'))
    assert label not in depreciation_only
    page = PageLiquidity(tmp_db, BwaQuery(2026, 12, 'jahr'))
    rows = table_text(page)
    assert rows.index(f'▶ {label} | 300,00 | 300,00') == rows.index('▶ Anlagenabgänge zum Restbuchwert | 0,00 | 0,00') + 1          # direkt hinter den Anlagenabgängen
    assert rows[rows.index(f'▶ {label} | 300,00 | 300,00') + 1] == '6815 Bürobedarf | 300,00 | 300,00'
    assert 'data-liq-parent="noncash_other"' in page and "liqToggle('noncash_other')" in page and BALANCED_TEXT in page
    assert 'Abschreibungen | 400,00 | 400,00' in ' '.join(rows)


def test_the_page_shows_the_form_with_the_chosen_values(tmp_db):
    page = PageLiquidity(tmp_db, BwaQuery(2026, 3, 'jahr'))
    dom = Dom(page)
    assert dom.one('form', action='/liquiditaet')['method'] == 'get'
    assert dom.one('select', name='month') == {'name': 'month'} and dom.one('input', name='year')['value'] == '2026'
    options = [attrs for tag, attrs in dom.tags if tag == 'option']
    assert [attrs['value'] for attrs in options] == [str(n) for n in range(1, 13)] + ['vorjahr', 'jahr']
    assert [attrs.get('value') for attrs in options if 'selected' in attrs] == ['3', 'jahr']
    assert 'Monat und Jahr</option>' in page and 'Vergleich mit dem Vorjahr</option>' in page and 'Anteilen' not in page


def test_the_accounts_are_closed_at_first_and_belong_to_their_line(demo_db):
    dom = Dom(PageLiquidity(demo_db, BwaQuery(2026, 3, 'vorjahr')))
    children = [attrs for tag, attrs in dom.tags if tag == 'tr' and 'child-row' in attrs.get('class', '')]
    assert children and all('hidden' in attrs and attrs['data-liq-parent'] for attrs in children)
    parents = {attrs['data-liq-parent'] for attrs in children}
    assert parents == {'assets', 'withdrawals', 'deposits'}                          # die Zeilen mit Konten in einem der vier Zeiträume
    heads = {attrs['onclick'] for tag, attrs in dom.tags if tag == 'tr' and 'group-row' in attrs.get('class', '')}
    assert heads == {f"liqToggle('{key}')" for key in parents}
    icons = {attrs['id'] for tag, attrs in dom.tags if tag == 'span' and attrs.get('id', '').startswith('liq-icon-')}
    assert icons == {f'liq-icon-{key}' for key in parents}
    assert not [attrs for tag, attrs in dom.tags if tag == 'tr' and 'bwa-parent' in str(attrs)]          # nichts vom Skript der BWA


def test_the_invalid_period_note_stands_only_for_an_invalid_period(tmp_db):
    page = PageLiquidity(tmp_db, BwaQuery(2026, 10, 'vorjahr', invalid=True))
    assert '<p class="warnColor">Der gewählte Zeitraum ist ungültig – angezeigt wird der Vormonat.</p>' in page
    assert 'Der gewählte Zeitraum' not in PageLiquidity(tmp_db, BwaQuery(2026, 10, 'vorjahr'))


def test_the_page_of_an_empty_book_shows_every_line_with_zero(tmp_db):
    page = PageLiquidity(tmp_db, BwaQuery(2026, 10, 'jahr'))
    rows = table_text(page)
    assert len(rows) == 2 + len(LAYOUT) - 4                                         # alle Zeilen, ohne die „Übrigen“ (Umbuchungen, Konten) und die nachrichtlichen
    assert all(row.endswith('| 0,00 | 0,00') for row in rows[2:]) and BALANCED_TEXT in page and 'warnColor' not in page


def test_the_page_uses_no_inline_style_and_only_var_and_function(demo_db):
    page = PageLiquidity(demo_db, BwaQuery(2026, 3, 'vorjahr'))
    own = page[page.index('<div class="rectRounded">'):page.index('<div id="app_confirm"')]
    assert ' style=' not in own and 'innerHTML' not in own
    script = Dom(page).scripts[0]
    assert script.startswith('\nvar LIQUIDITY_DATA = ')
    for forbidden in ('const ', 'let ', '=>', 'innerHTML', 'eval('):
        assert forbidden not in script, forbidden
    assert 'BWA_DATA' not in page and LIQUIDITY_JS in script


def test_the_pdf_address_stands_only_in_the_script_block(demo_db):
    page = PageLiquidity(demo_db, BwaQuery(2026, 3, 'jahr'))
    assert '/liquiditaet/pdf' not in re.sub(r'<script>.*?</script>', '', page, flags=re.S)
    data = re.search(r'var LIQUIDITY_DATA = (.*?);\n', Dom(page).scripts[0], re.S).group(1)
    assert json.loads(data) == {'pdf': '/liquiditaet/pdf?year=2026&month=3&variant=jahr'}


HOSTILE = '<script>alert(1)</script>"x'


def test_names_with_markup_are_escaped(tmp_path):
    """Ein Kontoname mit Markup steht als Text im Kontennachweis (hier unter den Anlagenzugängen) – und ändert den Aufbau der Seite nicht."""
    pages = {}
    for kind, name in (('hostile', HOSTILE), ('benign', 'Harmloser Name')):
        db = Database(db_name=str(tmp_path / f'{kind}.db'))
        book(db, 4400, '119.00', '2026-03-10', tax_rate='19', tax_amount='19.00')
        book(db, 790, '-12.00', '2026-03-11', name=name)
        pages[kind] = PageLiquidity(db, BwaQuery(2026, 3, 'jahr'))
    assert HOSTILE not in pages['hostile'] and '<script>alert(1)</script>' not in pages['hostile']
    assert html_lib.escape(HOSTILE, quote=True) in pages['hostile']
    assert_same_structure(pages['hostile'], pages['benign'])
    rows = table_text(pages['hostile'])
    assert '▶ Anlagenzugänge (Zahlungen auf Anlagekonten) | -12,00 | -12,00' in rows and f'790 {HOSTILE} | -12,00 | -12,00' in rows      # als Text, an seiner Stelle


def test_the_page_names_no_brand(demo_db):
    for variant in ('jahr', 'vorjahr'):
        text = visible_text(PageLiquidity(demo_db, BwaQuery(2026, 3, variant))).lower()
        for brand in BRANDS:
            assert brand.lower() not in text, brand


def test_the_page_shows_the_submenu_of_the_reports_with_the_liquidity_active(tmp_db):
    page = PageLiquidity(tmp_db, BwaQuery(2026, 3, 'jahr'))
    assert ("<div class='header2'><a href=\"/bwa\">BWA</a> | <a href=\"/euer\">EÜR</a> | <a href=\"/susa\">Summen- und Saldenliste</a> | "
            "<span id=\"ActivePage\">Liquidität</span></div>") in page
    assert reports_submenu('liquiditaet') in page and '<span id="ActivePage">Auswertungen</span>' in Header1('bwa')
    assert page.index('Auswertungen</span>') < page.index('<span id="ActivePage">Liquidität</span>') < page.index("<div class='header3'>")
    assert '<a href="/bwa">Auswertungen</a>' not in page


# ═══ Adresse und Route ════════════════════════════════════════════════════════
INVALID_ADDRESSES = [
    'year=02026&month=3', 'year=26&month=3', 'year=%EF%BC%92%EF%BC%90%EF%BC%92%EF%BC%96&month=3', 'year=%202026&month=3', 'year=%2B2026&month=3',
    'year=1899&month=3', 'year=3000&month=3', 'year=&month=3', 'year=2026&month=0', 'year=2026&month=13', 'year=2026&month=001', 'year=2026&month=1.0',
    'year=2026&month=-1', 'year=2026&month=%D9%A3', 'year=2026&year=2025&month=3', 'year=2026&month=3&month=4', 'year=2026', 'month=3',
]


@pytest.mark.parametrize('query', INVALID_ADDRESSES)
def test_an_invalid_period_gives_the_previous_month_with_a_note_and_the_pdf_refuses_it(server_port, auto_login, query):
    """Dieselbe strenge Lesart wie die BWA (``pages_bwa.read_page_query``/``read_pdf_query``): Die Seite zeigt den Vormonat mit Hinweis, das PDF antwortet 400.
    Der Vormonat hängt vom heutigen Datum ab – der Test liest es frisch."""
    year, month = default_month(datetime.date.today())
    page = get(server_port, f'/liquiditaet?{query}')
    assert page.status == 200 and 'Der gewählte Zeitraum ist ungültig' in page.text
    assert f'Liquiditätsrechnung – {bwa.month_label(year, month)}' in page.text
    pdf = get(server_port, f'/liquiditaet/pdf?{query}')
    assert pdf.status == 400 and pdf.text == 'Ungültiger Zeitraum.' and pdf.headers['Content-Type'] == 'text/plain; charset=utf-8'


def test_an_unknown_variant_gives_the_default_on_the_page_and_400_on_the_pdf(server_port, auto_login):
    some_books(auto_login.db)
    page = get(server_port, '/liquiditaet?year=2026&month=3&variant=unbekannt')
    assert page.status == 200 and 'Der gewählte Zeitraum' not in page.text and 'Veränderung %' in page.text         # der Standard, still
    for variant in ('x', 'JAHR', '%20jahr', 'jahr&variant=jahr', 'jahr&variant=vorjahr'):
        response = get(server_port, f'/liquiditaet/pdf?year=2026&month=3&variant={variant}')
        assert response.status == 400 and response.text == INVALID_VARIANT_ERROR, variant
    assert INVALID_VARIANT_ERROR == 'Ungültige Darstellung.'
    assert get(server_port, '/liquiditaet/pdf').status == 400


def some_books(db):
    two_banks(db)
    book(db, 4400, '119.00', '2026-03-10', tax_rate='19', tax_amount='19.00')
    book(db, 6815, '-59.50', '2026-03-12', tax_rate='19', tax_amount='-9.50')
    book(db, 500, '-1190.00', '2026-03-14', tax_rate='19', tax_amount='-190.00')
    book(db, 3150, '5000.00', '2026-03-15')
    book(db, 2100, '-80.00', '2026-03-20')
    book(db, 4400, '119.00', '2025-03-10', tax_rate='19', tax_amount='19.00')


def test_the_page_answers_with_the_comparison_by_default_and_never_sets_a_cookie(server_port, auto_login):
    some_books(auto_login.db)
    response = get(server_port, '/liquiditaet?year=2026&month=3')
    assert response.status == 200 and response.headers['Content-Type'].startswith('text/html') and response.headers.get('Set-Cookie') is None
    assert response.headers.get_all('Cache-Control') == ['no-cache']
    text = response.text
    assert 'Liquiditätsrechnung – März 2026' in text and 'Veränderung %' in text and 'Betrag €' not in text
    assert 'Betrag €' in get(server_port, '/liquiditaet?year=2026&month=3&variant=jahr').text
    bare = get(server_port, '/liquiditaet')
    assert bare.status == 200 and 'Liquiditätsrechnung – ' in bare.text and 'Der gewählte Zeitraum' not in bare.text


def test_the_page_and_the_pdf_write_nothing(server_port, auto_login):
    """Die Seite und das PDF sind GET-Routen, die nur lesen: Datenbank und Datenordner sind danach unverändert, es gibt kein Cookie. Das ist der Nachweis für die
    Positivliste des Diagnose-MCP (``/liquiditaet``)."""
    some_books(auto_login.db)
    before_db, before_tree = dump(auto_login.db.db_name), tree(userctx.DATA_ROOT)
    for path in ('/liquiditaet', '/liquiditaet?year=2026&month=3', '/liquiditaet?year=2026&month=3&variant=jahr', '/liquiditaet?year=x',
                 '/liquiditaet?year=2026&month=1', '/liquiditaet/pdf?year=2026&month=3', '/liquiditaet/pdf?year=2026&month=3&variant=jahr&accounts=1',
                 '/liquiditaet/pdf?year=bad', '/liquiditaet/pdf?year=2026&month=3&variant=x'):
        response = get(server_port, path)
        assert response.headers.get('Set-Cookie') is None, path
        assert response.status in (200, 400), path
    assert dump(auto_login.db.db_name) == before_db
    assert tree(userctx.DATA_ROOT) == before_tree


def test_the_routes_are_plain_get_pages_without_an_action_or_json_entry():
    from server import app
    for name in ('_ACTION_PATHS', '_GENERATE_PATHS', '_JSON_PATHS', '_POST_ONLY_JSON_PATHS', '_SEARCH_PATHS', '_UPLOAD_PATHS', '_LOCK_FREE_ROUTES'):
        for route in ('/liquiditaet', '/liquiditaet/pdf'):
            assert route not in getattr(app, name), (name, route)


# ═══ Diagnose-MCP ═════════════════════════════════════════════════════════════
def _mcp():
    spec = importlib.util.spec_from_file_location('mcp_ubuchhaltung', TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_mcp_may_read_the_page_but_never_the_pdf():
    """Die Seite schreibt nichts (Nachweis: ``test_the_page_and_the_pdf_write_nothing``), das PDF ist ein Dokument: nur die Seite steht in der Positivliste."""
    mcp = _mcp()
    for path in ('/liquiditaet', '/liquiditaet?year=2026&month=10&variant=jahr', '/liquiditaet?year=2026&month=10&variant=vorjahr'):
        assert mcp.check_page_path(path) == path
    assert mcp._PAGE_ALLOWLIST['/liquiditaet'] == frozenset({'year', 'month', 'variant'}) and '/liquiditaet/pdf' not in mcp._PAGE_ALLOWLIST
    for path in ('/liquiditaet/pdf?year=2026&month=10', '/liquiditaet/pdf', '/Liquiditaet', '/liquiditaet/'):
        with pytest.raises(ValueError, match='Seite nicht freigegeben'):
            mcp.check_page_path(path)
    for path, key in (('/liquiditaet?from=2026-01-01', 'from'), ('/liquiditaet?accounts=1', 'accounts'), ('/liquiditaet?year=2026&acct=1', 'acct')):
        with pytest.raises(ValueError, match=f'Query-Parameter nicht freigegeben: {key}'):
            mcp.check_page_path(path)
