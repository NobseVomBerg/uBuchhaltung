# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Betriebswirtschaftliche Auswertung (2.5.14): eine reine Umgruppierung der EÜR.

Drei Teile. Der reine Teil (``db/bwa.py``) prüft die Gruppentabelle, die Zeiträume, die Prozent- und Formatregeln und den Aufbau
des Berichts aus erfundenen EÜR-Zeilen. Der zweite Teil hält die Zusage fest, auf der die Auswertung steht: Sie hat keine eigene
Lesart der Buchungen. Einnahme, Ausgabe und „nicht im Ergebnis“ entscheidet ``reporting.euer_section`` – dieselbe Funktion wie auf der
EÜR-Seite des Dashboards (``test_euer_section_equals_the_former_page_rule`` schreibt deren frühere Regel ab und prüft sie für alle
Kontonummern) –, und in jedem Zeitraum gelten, auf den Minor-Wert genau, vier Invarianten gegen ``get_euer_data`` (Testwelt aus
``tests/entry_form_world.py`` und der Demobestand). Der dritte Teil prüft die Seite und die Adresse: strenges Lesen, Maskieren, nichts
schreiben, Menü.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie als Konstante da (die Adresse bekommt es als Argument).
"""
import calendar
import datetime
import html as html_lib
import json
import os
import random
import re
from decimal import Decimal

import pytest

import auth
import demo_seed
import userctx
from db import Database, bwa
from db.bwa import (COST_KEYS, GROUPS, LAYOUT, OPTIONAL_KEYS, PERCENT_TEXT, SIGN_TEXT, UNASSIGNED_TEXT,
                    bwa_filename, bwa_periods, block_headers, cells, change_percent, column_labels, columns, default_month,
                    format_amount, format_percent, group_of, percent, period_line, periods_for, visible, visible_accounts)
from db.reporting import (NEUTRAL_ACCOUNT_NUMBERS, OTHER_EXPENSE_ACCOUNTS, bank_skr_numbers, euer_section,
                          is_income_account)
from money import from_minor, to_minor
from server.pages import Header1
from server.pages_bwa import (INVALID_VARIANT_ERROR, BwaQuery, PageBwa, parse_month, parse_variant, parse_year,
                              read_pdf_query, read_variant)
from server.pages_dashboard import PageDashboard
from tests import entry_form_world
from tests.bwa_helpers import account_numbers, book, report_from, some_books, values_of
from tests.dom_helpers import Dom, assert_same_structure
from tests.http_client import get
from tests.installation import dump, tree

SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'seed_data', 'chart_of_accounts_skr04.json')
DEMO_TODAY = datetime.date(2026, 11, 3)         # ein fester Tag, nie die Uhr (tests/test_clock_at_import.py)


# ═══ Gruppentabelle ═══════════════════════════════════════════════════════════
def test_the_ranges_are_sorted_disjoint_and_name_known_groups():
    previous_high = 999
    for low, high, key in bwa.RANGES:
        assert 1000 <= low <= high <= 9999, (low, high, key)
        assert low > previous_high, (low, high, key)                     # aufsteigend und ohne Überschneidung
        previous_high = high
        assert key in GROUPS, key
    assert all(isinstance(number, int) and key in GROUPS for number, key in bwa.EXCEPTIONS.items())


@pytest.mark.parametrize('numbers, group', [
    ([7685], 'vehicles'),
    ([6495, 6845], 'maintenance_tools'),
    ([6220, 6222, 6260], 'depreciation'),
    ([6800, 6805, 6815, 6820, 6827, 6850, 6855, 6859], 'misc_costs'),
    ([6300], 'misc_costs'),
    ([6020, 6130, 6171], 'personnel'),
    ([6310, 6325, 6335, 6345], 'premises'),
    ([6430], 'taxes_insurance'),
    ([6520, 6530, 6532], 'vehicles'),
    ([6640, 6641, 6644], 'marketing_travel'),
    ([7326], 'interest_other'),
    ([1940], 'interest_other'),
    ([4400, 4300, 4185], 'revenue'),
    ([4639, 4646, 4972], 'other_revenue'),
    ([3806], 'vat_collected'),
    ([1401, 1406], 'input_vat'),
    ([3820, 3830, 3841], 'vat_settlement'),
    ([3150], 'liabilities'),
])
def test_the_owner_examples_land_in_their_groups(numbers, group):
    assert [group_of(number) for number in numbers] == [group] * len(numbers)


@pytest.mark.parametrize('value', [None, True, False, 'abc', '', 4400.0, ' 4400', '4400 ', '４４００', '٤٤٠٠', b'4400', [4400],
                                   '99999999999999999999999'])
def test_group_of_reads_only_whole_numbers(value):
    assert group_of(value) is None


def test_group_of_reads_a_whole_number_and_a_text_of_ascii_digits():
    assert group_of(4400) == group_of('4400') == 'revenue'
    assert group_of(0) is None and group_of(999) is None and group_of(8000) is None and group_of(-4400) is None
    assert group_of(2100) is None and group_of(1460) is None and group_of(1800) is None        # absichtlich ohne Gruppe


def test_no_standard_account_that_can_reach_the_euer_is_unassigned():
    """Jedes Standardkonto, das in der EÜR stehen kann, hat eine Gruppe – sonst stünde es unter „Nicht zugeordnet“. Die Geldkonten
    (1460, 1600–1699, 1800–1899) sind nie Zweckkonto und bleiben deshalb ausgenommen."""
    with open(SEED, encoding='utf-8') as f:
        accounts = json.load(f)['accounts']
    money = {1460} | set(range(1600, 1700)) | set(range(1800, 1900))
    checked = []
    for account in accounts:
        number = account['account_number']
        if not account.get('is_standard') or number in money:
            continue
        if euer_section(number, set()) in ('income', 'expense'):
            checked.append(number)
            assert group_of(number) is not None, number
    assert 1406 in checked and 3150 in checked and 6815 in checked and 4400 in checked          # die Auswahl ist nicht leer


def test_every_group_matches_the_euer_side():
    """Kein Einnahmenkonto landet in einer Ausgabengruppe und umgekehrt. Verglichen wird, was die EÜR zeigt: Die Wartekonten
    (4340, 4345, 4405) liegen im Bereich der Erlöse, erreichen die Auswertung aber nie."""
    for number in range(1000, 10000):
        group, section = group_of(number), euer_section(number, set())
        if group is None or section is None:
            continue
        side = GROUPS[group].side
        if section == 'income':
            assert side == 'income', (number, group)
        else:
            assert side in ('expense', 'sign'), (number, group)


def test_the_layout_names_each_line_once_and_a_sum_only_reads_lines_above_it():
    seen = []
    for spec in LAYOUT:
        assert spec.key not in seen, spec.key
        for part in (*spec.plus, *spec.minus):
            assert part in seen, (spec.key, part)                         # die Summen stehen hinter ihren Bestandteilen
        if spec.kind == 'group' and spec.key in GROUPS:
            assert GROUPS[spec.key].side in ('income', 'expense'), spec.key  # eine Gruppe mit eigener Zeile ist nie ``sign``
        seen.append(spec.key)
    assert 'outside' not in seen and set(COST_KEYS) <= set(seen) and OPTIONAL_KEYS - {'outside'} <= set(seen)
    assert all(re.fullmatch(r'[a-z_]+', key) for key in seen)             # die Schlüssel stehen in Adressen und Skript


# ═══ Einordnung wie die EÜR ═══════════════════════════════════════════════════
@pytest.mark.parametrize('number, banks, expected', [
    (4400, set(), 'income'), (3806, set(), 'income'), (4185, set(), 'income'), (4405, set(), None), (4340, set(), None),
    (10000, set(), None), (6815, set(), 'expense'), (1406, set(), 'expense'), (1050, set(), 'expense'),
    (3150, set(), 'expense'), (3820, set(), 'expense'), (3160, set(), 'other'), (3720, set(), 'other'), (3740, set(), 'other'),
    (2100, set(), None), (2199, set(), None), (2200, set(), 'expense'), (520, set(), None), (999, set(), None),
    (1000, set(), 'expense'), (1800, {1800}, None), (1800, set(), 'expense'), (4400, {4400}, None), (3806, {3806}, None),
    (3160, {3160}, 'other'),                  # „Sonstige Ausgaben“ stehen ohne weitere Prüfung dort, wie bisher auf der Seite
])
def test_euer_section_follows_the_page_rule(number, banks, expected):
    assert euer_section(number, banks) == expected


def _former_page_rule(number, bank_numbers):
    """Die Regel der EÜR-Seite vor 2.5.14 (``server/pages_dashboard.py``, damals als Inline-Code), abgeschrieben: die Listen, in
    denen ein Konto stand – einnahmen, ausgaben, sonstige."""
    other = {3160, 3720, 3740}
    exclude = set(NEUTRAL_ACCOUNT_NUMBERS) | set(bank_numbers)
    is_income_row = is_income_account(number) or number == 3806
    lists = []
    if is_income_row and number not in exclude:
        lists.append('income')
    if (not is_income_row and number not in other and number not in exclude
            and not (2100 <= number < 2200) and number >= 1000):
        lists.append('expense')
    if number in other:
        lists.append('other')
    return lists


@pytest.mark.parametrize('banks', [set(), {1800}, {1800, 1600}, {4400}, {3160}, {3806, 1406}],
                         ids=lambda banks: ','.join(map(str, sorted(banks))) or 'ohne')
def test_euer_section_equals_the_former_page_rule(banks):
    """``euer_section`` gibt die Regel der EÜR-Seite von vorher für ALLE Kontonummern wieder (0 bis 10999) – die Seite ruft sie
    nun statt ihres Inline-Codes. Weicht sie irgendwo ab, ist ``euer_section`` zu ändern, nicht dieser Test."""
    for number in range(0, 11000):
        lists = _former_page_rule(number, banks)
        assert len(lists) <= 1, number                          # früher stand ein Konto in höchstens einer Liste
        assert euer_section(number, banks) == (lists[0] if lists else None), number


def test_bank_skr_numbers_are_the_numbers_of_the_own_bank_and_cash_accounts():
    accounts = [(1, 'Kasse', '', '', '', '', 1, 1600, 1), (2, 'Giro', 'x', 'DE00', 'B', 'Bank', 0, 1800, 1),
                (3, 'Ohne', 'x', 'DE01', 'B', 'Bank', 0, None, 1), (4, 'Null', 'x', 'DE02', 'B', 'Bank', 0, 0, 1)]
    assert bank_skr_numbers(accounts) == {1600, 1800}
    assert bank_skr_numbers([]) == set()
    assert OTHER_EXPENSE_ACCOUNTS == frozenset({3160, 3720, 3740})


# ═══ Zeiträume ════════════════════════════════════════════════════════════════
def test_the_periods():
    assert bwa_periods(2024, 2) == {'month': ('2024-02-01', '2024-02-29'), 'ytd': ('2024-01-01', '2024-02-29'),
                                    'prev_month': ('2023-02-01', '2023-02-28'), 'prev_ytd': ('2023-01-01', '2023-02-28')}
    january = bwa_periods(2026, 1)
    assert january['month'] == january['ytd'] == ('2026-01-01', '2026-01-31')
    assert january['prev_month'] == january['prev_ytd'] == ('2025-01-01', '2025-01-31')
    december = bwa_periods(2026, 12)
    assert december['month'] == ('2026-12-01', '2026-12-31') and december['ytd'] == ('2026-01-01', '2026-12-31')
    assert bwa_periods(2001, 2)['prev_month'] == ('2000-02-01', '2000-02-29')           # das Vorjahr ist das Schaltjahr
    assert bwa_periods(2000, 2)['month'][1] == '2000-02-29'                                # durch 400 teilbar: Schaltjahr
    assert bwa_periods(1900, 2)['month'][1] == '1900-02-28' and bwa_periods(2100, 2)['month'][1] == '2100-02-28'
    for month in range(1, 13):
        last = bwa_periods(2026, month)['month'][1]
        assert last == f'2026-{month:02d}-{calendar.monthrange(2026, month)[1]:02d}'


@pytest.mark.parametrize('month', [0, 13, -1])
def test_a_month_outside_1_to_12_is_refused(month):
    with pytest.raises(ValueError):
        bwa_periods(2026, month)


def test_each_variant_shows_its_periods_and_an_unknown_one_is_refused():
    assert periods_for('jahr') == ('month', 'ytd')
    assert periods_for('vorjahr') == ('month', 'ytd', 'prev_month', 'prev_ytd')
    with pytest.raises(ValueError):
        periods_for('x')


def test_the_default_is_the_previous_month():
    assert default_month(datetime.date(2026, 1, 15)) == (2025, 12)
    assert default_month(datetime.date(2026, 11, 21)) == (2026, 10)
    assert default_month(datetime.date(2026, 3, 1)) == (2026, 2)
    assert default_month(datetime.date(2026, 12, 31)) == (2026, 11)


def test_the_filename_names_the_month_and_the_variant():
    assert bwa_filename(2026, 10, 'vorjahr') == 'BWA_2026-10.pdf'                    # der Standard: der Vorjahresvergleich
    assert bwa_filename(2026, 10, 'jahr') == 'BWA_2026-10_Anteile.pdf'
    assert bwa_filename(2026, 1, 'vorjahr') == 'BWA_2026-01.pdf'


# ═══ Prozent und Format ═══════════════════════════════════════════════════════
def test_a_ratio_beyond_the_decimal_context_is_an_empty_cell_and_no_error():
    """Ein Quotient ab 10^24 (winzige Bezugsgröße gegen einen absurden Betrag, nur per SQL-Konsole erreichbar) sprengt die 28 Stellen
    des Decimal-Kontexts: Die Zelle bleibt leer, die Seite scheitert nicht."""
    assert percent(10 ** 30, 1) is None and change_percent(10 ** 30, 1) is None and change_percent(-10 ** 30, -1) is None
    assert format_percent(percent(10 ** 30, 1)) == ''
    assert percent(10 ** 20, 1) == Decimal(10 ** 22)                                        # darunter wird weiter gerechnet


def test_percent_rules():
    assert percent(1, 0) is None and percent(0, 0) is None and percent(5, -100) is None      # Bezugsgröße null oder negativ: leer
    assert percent(1, 3) == Decimal('33.33') and percent(2, 3) == Decimal('66.67')
    assert percent(-50, 200) == Decimal('-25.00')                                           # das Vorzeichen folgt dem Zähler
    assert percent(1, 20000) == Decimal('0.01') and percent(-1, 20000) == Decimal('-0.01')  # 0,005 wird kaufmännisch gerundet
    assert percent(1, 40000) == Decimal('0.00') and str(percent(-1, 100000)) == '0.00'      # nie „-0,00“
    assert percent(0, 7) == Decimal('0.00') and percent(300, 100) == Decimal('300.00')


def test_change_percent_rules():
    assert change_percent(5, 0) is None and change_percent(0, 0) is None                    # Vorjahr null: leer
    assert change_percent(150, 100) == Decimal('50.00') and change_percent(50, 100) == Decimal('-50.00')
    assert change_percent(-50, -100) == Decimal('50.00')       # mit dem Betrag im Nenner: weniger Verlust heißt gestiegen
    assert change_percent(-100, -50) == Decimal('-100.00')
    assert change_percent(100, -100) == Decimal('200.00') and change_percent(0, 100) == Decimal('-100.00')
    assert change_percent(2, 3) == Decimal('-33.33')


def test_amounts_and_percents_are_formatted_german_and_empty_stays_empty():
    assert format_amount(None) == '' and format_percent(None) == ''
    assert format_amount(12345600) == '1.234,56' and format_amount(-12345600) == '-1.234,56'
    assert format_amount(0) == '0,00' and format_amount(-49) == '0,00' and format_amount(49) == '0,00'    # nie „-0,00“
    assert format_amount(50) == '0,01' and format_amount(-50) == '-0,01'                    # kaufmännisch auf den Cent
    assert format_amount(1234567890000) == '123.456.789,00'
    assert format_percent(Decimal('-12.34')) == '-12,34' and format_percent(Decimal('33.33')) == '33,33'
    assert format_percent(Decimal('1234.5')) == '1.234,50' and format_percent(Decimal('0.00')) == '0,00'
    assert format_percent(Decimal('-0.00')) == '0,00'


def test_the_round_trip_over_the_euro_float_keeps_every_minor_value():
    """Die EÜR liefert ihre Salden als Euro-``float`` (``get_euer_data``), die Auswertung rechnet in Minor Units: ``to_minor`` muss
    aus dem ``float`` jeden gespeicherten Minor-Wert exakt zurückgewinnen – sonst stimmten die Summen nicht auf den Cent mit der
    EÜR-Seite überein. Geprüft über Randwerte, Cent-Bruchteile und viele feste Zufallswerte bis 10^11 Euro."""
    values = [0, 1, -1, 49, 50, 51, 99, 100, 9999, 10000, -10000, 12345678, -12345678, 10 ** 15 - 1, -(10 ** 15 - 1)]
    generator = random.Random(2514)
    values += [generator.randint(-10 ** 15, 10 ** 15) for _ in range(5000)]
    values += [generator.randint(-10 ** 8, 10 ** 8) for _ in range(5000)]
    for minor in values:
        assert to_minor(float(from_minor(minor))) == minor, minor


# ═══ Spalten und Köpfe ════════════════════════════════════════════════════════
def test_the_columns_and_headers_of_both_variants():
    year = report_from([(6815, 'Bürobedarf', -100.0)], variant='jahr', month=10)
    assert [(column.period, column.kind) for column in columns(year)] == (
        [('month', kind) for kind in ('amount', 'pct_revenue', 'pct_costs', 'pct_income')]
        + [('ytd', kind) for kind in ('amount', 'pct_revenue', 'pct_costs', 'pct_income')])
    assert block_headers(year) == ('Oktober 2026', 'Januar bis Oktober 2026')
    assert column_labels(year) == ['Betrag €', '% der Erlöse', '% der Kosten', '% der Betriebseinnahmen'] * 2
    assert column_labels(year, short=True) == ['Betrag €', '% Erlöse', '% Kosten', '% Betr.-Einn.'] * 2
    previous = report_from([(6815, 'Bürobedarf', -100.0)], variant='vorjahr', month=10)
    assert [(column.period, column.kind) for column in columns(previous)] == [
        ('month', 'amount'), ('prev_month', 'amount'), ('month', 'delta'), ('month', 'delta_pct'),
        ('ytd', 'amount'), ('prev_ytd', 'amount'), ('ytd', 'delta'), ('ytd', 'delta_pct')]
    assert block_headers(previous) == ('Oktober', 'Januar bis Oktober')
    assert column_labels(previous) == column_labels(previous, short=True) == ['2026', '2025', 'Veränderung €', 'Veränderung %'] * 2


def test_in_january_the_month_and_the_accumulated_period_are_the_same():
    assert block_headers(report_from([], variant='jahr', month=1)) == ('Januar 2026', 'Januar 2026 (aufgelaufen)')
    assert block_headers(report_from([], variant='vorjahr', month=1)) == ('Januar', 'Januar (aufgelaufen)')


def test_the_period_line_of_the_pdf():
    assert period_line(report_from([], variant='jahr', month=10)) == 'Oktober 2026 · aufgelaufen Januar bis Oktober 2026'
    assert (period_line(report_from([], variant='vorjahr', month=10))
            == 'Oktober 2026 · aufgelaufen Januar bis Oktober 2026 · Vergleich mit dem Vorjahr')
    assert period_line(report_from([], variant='jahr', month=1)) == 'Januar 2026 · aufgelaufen Januar 2026'


# ═══ Aufbau des Berichts ══════════════════════════════════════════════════════
WORLD_ROWS = [(4400, 'Erlöse 19% USt', 1000.0), (3806, 'Umsatzsteuer 19%', 190.0), (6815, 'Bürobedarf', -100.0),
              (1406, 'Abziehbare Vorsteuer 19%', -19.0), (3820, 'Umsatzsteuer-Vorauszahlungen', -50.0),
              (3841, 'Umsatzsteuer-Sondervorauszahlung', 20.0), (1050, 'Altkonto', -5.0), (3160, 'Darlehen', -30.0),
              (520, 'PKW', -999.0), (2100, 'Privatentnahmen', -77.0)]


def test_build_report_regroups_and_balances():
    report = report_from(WORLD_ROWS)
    assert values_of(report, 'result') == to_minor(1036) and values_of(report, 'operating_income') == to_minor(1210)
    assert values_of(report, 'operating_expenses') == to_minor(174) and values_of(report, 'total_costs') == to_minor(100)
    assert values_of(report, 'total_revenue') == to_minor(1000) and values_of(report, 'vat_collected') == to_minor(190)
    assert values_of(report, 'vat_refund') == to_minor(20) and account_numbers(report, 'vat_refund') == [3841]
    assert values_of(report, 'vat_payment') == to_minor(50) and account_numbers(report, 'vat_payment') == [3820]
    assert values_of(report, 'unassigned_expense') == to_minor(5) and account_numbers(report, 'unassigned_expense') == [1050]
    assert report.outside.values['month'] == to_minor(30) and [a.number for a in report.outside.accounts] == [3160]
    assert account_numbers(report, 'input_vat') == [1406] and values_of(report, 'input_vat') == to_minor(19)
    everywhere = [account.number for line in report.lines for account in line.accounts] + [a.number for a in report.outside.accounts]
    assert 520 not in everywhere and 2100 not in everywhere                      # Anlagekonto und Privatkonto stehen nirgends
    assert sorted(everywhere) == sorted([4400, 3806, 6815, 1406, 3820, 3841, 1050, 3160])
    [account] = report.line('misc_costs').accounts
    assert account.number == 6815 and account.name == 'Bürobedarf'
    assert cells(report, report.line('misc_costs'), account.values)[:4] == ['100,00', '10,00', '100,00', '8,26']
    assert cells(report, report.line('total_costs'), report.line('total_costs').values)[:4] == ['100,00', '10,00', '100,00', '8,26']


def test_the_balance_of_a_report_holds_for_any_rows():
    """Ergebnis = Einnahmen + Ausgaben der EÜR; Betriebseinnahmen und -ausgaben weichen nur um die Erstattungen ab."""
    rows = WORLD_ROWS + [(3830, 'Umsatzsteuer 1/11', 7.5), (6850, 'Sonstiger Bedarf', 0.1), (4300, 'Erlöse 7%', 0.2)]
    report = report_from(rows)
    banks = set()
    income = sum(to_minor(t) for n, _, t in rows if euer_section(n, banks) == 'income')
    expense = sum(to_minor(t) for n, _, t in rows if euer_section(n, banks) == 'expense')
    assert values_of(report, 'result') == income + expense
    assert values_of(report, 'operating_income') - values_of(report, 'vat_refund') == income
    assert values_of(report, 'operating_expenses') - values_of(report, 'vat_refund') == -expense


def test_the_money_accounts_and_the_waiting_accounts_do_not_stand_in_the_report():
    rows = [(4400, 'Erlöse', 100.0), (1800, 'Bank', -10.0), (1600, 'Kasse', -20.0), (4405, 'Wartekonto', 5.0), (10000, 'Debitoren', 7.0)]
    report = report_from(rows, banks={1800, 1600})
    assert values_of(report, 'result') == to_minor(100)
    assert [a.number for line in report.lines for a in line.accounts] == [4400]


def test_a_settlement_account_changes_sides_per_period():
    """Ein Konto der Umsatzsteuer-Zahlungen steht je Zeitraum für sich unter Erstattungen (Saldo > 0) oder Zahlungen; die andere
    Zelle ist leer, nicht 0."""
    report = report_from([(3820, 'Vorauszahlungen', 20.0)], ytd_rows=[(3820, 'Vorauszahlungen', -30.0)])
    refund, payment = report.line('vat_refund'), report.line('vat_payment')
    assert refund.values == {'month': to_minor(20), 'ytd': 0} and payment.values == {'month': 0, 'ytd': to_minor(30)}
    [in_refund], [in_payment] = refund.accounts, payment.accounts
    assert in_refund.values == {'month': to_minor(20)} and in_payment.values == {'ytd': to_minor(30)}
    refund_cells, payment_cells = cells(report, refund, in_refund.values), cells(report, payment, in_payment.values)
    assert refund_cells[0] == '20,00' and refund_cells[4:] == [''] * 4                       # aufgelaufen: leer
    assert payment_cells[:4] == [''] * 4 and payment_cells[4] == '30,00'                      # Monat: leer
    assert values_of(report, 'result') == to_minor(20)                                        # das Ergebnis: der Monat
    assert values_of(report, 'result', 'ytd') == to_minor(-30)


def test_real_and_virtual_3806_are_added():
    report = report_from([(3806, 'Umsatzsteuer 19%', 100.0), (3806, 'Umsatzsteuer 19%', 90.0)])
    [account] = report.line('vat_collected').accounts
    assert account.number == 3806 and account.values['month'] == to_minor(190)
    assert values_of(report, 'vat_collected') == to_minor(190) and values_of(report, 'result') == to_minor(190)


def test_the_first_name_found_stays_and_a_zero_sum_account_is_listed_but_not_shown():
    report = report_from([(6815, 'Name im Monat', -10.0), (6815, 'Gleicher Name', 10.0)], ytd_rows=[(6815, 'Name aufgelaufen', -5.0)])
    [account] = report.line('misc_costs').accounts
    assert account.name == 'Name im Monat' and account.values == {'month': 0, 'ytd': to_minor(5)}
    only_zero = report_from([(6815, 'Bürobedarf', -10.0), (6815, 'Bürobedarf', 10.0)])
    assert account_numbers(only_zero, 'misc_costs') == [6815] and visible_accounts(only_zero, only_zero.line('misc_costs')) == []


def test_an_account_without_a_group_stands_under_not_assigned_and_counts(monkeypatch):
    """Ein Konto ohne Gruppe steht unter „Nicht zugeordnet“ und zählt im Ergebnis – es wird nie still in eine andere Gruppe gelegt."""
    monkeypatch.setattr(bwa, 'group_of', lambda number: None)
    report = report_from([(4400, 'Erlöse', 1000.0), (6815, 'Bürobedarf', -100.0), (3820, 'Vorauszahlungen', -50.0), (3150, 'Darlehen', -60.0)])
    assert account_numbers(report, 'unassigned_income') == [4400] and values_of(report, 'unassigned_income') == to_minor(1000)
    assert account_numbers(report, 'unassigned_expense') == [3150, 3820, 6815] and values_of(report, 'unassigned_expense') == to_minor(210)
    assert values_of(report, 'result') == to_minor(790)
    assert all(not report.line(key).accounts for key in ('revenue', 'misc_costs', 'vat_payment', 'liabilities', 'vat_refund'))


def test_an_unknown_account_stays_unassigned_in_the_real_table():
    report = report_from([(1050, 'Altkonto', -10.0), (6815, 'Bürobedarf', -100.0)])
    assert account_numbers(report, 'unassigned_expense') == [1050] and account_numbers(report, 'misc_costs') == [6815]
    assert values_of(report, 'result') == to_minor(-110)


def test_unassigned_and_outside_lines_show_only_when_they_have_something():
    plain = report_from([(4400, 'Erlöse', 100.0)])
    assert [visible(plain, plain.line(key)) for key in ('unassigned_income', 'unassigned_expense')] == [False, False]
    assert not visible(plain, plain.outside)
    assert all(visible(plain, line) for line in plain.lines if line.key not in OPTIONAL_KEYS)      # alle anderen immer, auch 0,00
    shown = report_from([(1050, 'Altkonto', -5.0), (3160, 'Darlehen', -30.0)])
    assert visible(shown, shown.line('unassigned_expense')) and visible(shown, shown.outside)
    cancelled = report_from([(1050, 'Altkonto', -5.0), (1050, 'Altkonto', 5.0)])
    assert visible(cancelled, cancelled.line('unassigned_expense'))              # es hat Konten, auch wenn sich ihr Wert aufhebt
    only_ytd = report_from([], ytd_rows=[(1050, 'Altkonto', -5.0)])
    assert only_ytd.line('unassigned_expense').values == {'month': 0, 'ytd': to_minor(5)}


def test_a_report_without_revenue_has_no_percent_and_no_inf_or_nan():
    report = report_from([(6815, 'Bürobedarf', -100.0)])
    line = report.line('misc_costs')
    assert cells(report, line, line.values) == ['100,00', '', '100,00', ''] * 2           # kein Anteil an Erlösen und Einnahmen: leer
    refund = report_from([(6815, 'Bürobedarf', 100.0), (4400, 'Erlöse', 100.0)])            # Erstattung: negative Kosten
    erstattet = refund.line('total_costs')
    assert values_of(refund, 'total_costs') == to_minor(-100)
    assert cells(refund, erstattet, erstattet.values)[:4] == ['-100,00', '-100,00', '', '-100,00']     # Nenner Kosten negativ: leer
    for text in (' '.join(cells(report, line, line.values)) for line in report.lines):
        assert 'inf' not in text.lower() and 'nan' not in text.lower()


def test_the_comparison_with_the_previous_year_shows_change_and_blanks():
    report = report_from(
        [(6815, 'Bürobedarf', -100.0), (6850, 'Sonstiges', -10.0), (4400, 'Erlöse', 500.0)],
        ytd_rows=[(6815, 'Bürobedarf', -300.0), (6850, 'Sonstiges', -10.0), (6805, 'Telefon', -4.0), (4400, 'Erlöse', 900.0)],
        variant='vorjahr', prev_month_rows=[(6815, 'Bürobedarf', -80.0), (4400, 'Erlöse', 250.0)],
        prev_ytd_rows=[(6815, 'Bürobedarf', -400.0), (4400, 'Erlöse', 900.0)])
    misc = report.line('misc_costs')
    by_number = {account.number: account for account in misc.accounts}
    assert cells(report, misc, by_number[6815].values) == ['100,00', '80,00', '20,00', '25,00', '300,00', '400,00', '-100,00', '-25,00']
    # 6850 steht im Vorjahr nirgends: leer, die Veränderung rechnet mit 0 und hat keinen Prozentwert
    assert cells(report, misc, by_number[6850].values) == ['10,00', '', '10,00', '', '10,00', '', '10,00', '']
    # 6805 steht nur aufgelaufen da: der Monat ist leer
    assert cells(report, misc, by_number[6805].values) == ['', '', '', '', '4,00', '', '4,00', '']
    assert cells(report, misc, misc.values) == ['110,00', '80,00', '30,00', '37,50', '314,00', '400,00', '-86,00', '-21,50']
    revenue = report.line('total_revenue')
    assert cells(report, revenue, revenue.values) == ['500,00', '250,00', '250,00', '100,00', '900,00', '900,00', '0,00', '0,00']
    result = report.line('result')
    assert cells(report, result, result.values)[:4] == ['390,00', '170,00', '220,00', '129,41']
    # eine Spalte des Vorjahres in einer Zeile, die dort gar nichts hat, bleibt bei der Gruppe 0,00 (und leer beim Prozentwert)
    other = report.line('other_revenue')
    assert cells(report, other, other.values) == ['0,00', '0,00', '0,00', '', '0,00', '0,00', '0,00', '']


def test_get_bwa_refuses_an_unknown_variant_and_an_invalid_month(tmp_db):
    with pytest.raises(ValueError):
        tmp_db.get_bwa(2026, 3, 'x')
    with pytest.raises(ValueError):
        tmp_db.get_bwa(2026, 13, 'jahr')


@pytest.mark.parametrize('month, variant, reads', [(3, 'jahr', 2), (3, 'vorjahr', 4), (1, 'jahr', 1), (1, 'vorjahr', 2)])
def test_get_bwa_reads_each_span_once(tmp_db, monkeypatch, month, variant, reads):
    """Im Januar sind Monat und aufgelaufen dieselbe Spanne: Sie wird nur einmal gelesen."""
    spans = []
    real = tmp_db.get_euer_data
    monkeypatch.setattr(tmp_db, 'get_euer_data', lambda *args, **kwargs: spans.append(args) or real(*args, **kwargs))
    report = tmp_db.get_bwa(2026, month, variant)
    assert len(spans) == reads and len(set(spans)) == reads
    assert set(report.periods) == set(periods_for(variant)) and set(report.line('result').values) == set(report.periods)


# ═══ Gegen die EÜR: Testwelt ══════════════════════════════════════════════════
@pytest.fixture(scope='module')
def world(tmp_path_factory):
    """Die Testwelt (``tests/entry_form_world.py``): jeder Schreibweg einmal, einmal gebaut. Die Datenwurzel liegt unter
    ``tmp_path_factory`` – nichts davon landet im echten data/ (``pytest.MonkeyPatch.context``, weil eine modulweite Fixture den
    ``monkeypatch`` je Test nicht nutzen darf)."""
    root = tmp_path_factory.mktemp('bwa_world')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, 'DATA_ROOT', str(root / 'data'))
        patch.setattr(auth, 'AUTH_DB', str(root / 'data' / 'auth.db'))
        userctx._mode_cache.clear()
        db = entry_form_world.build(str(root / 'world.db'))
    userctx._mode_cache.clear()
    return db


def check_invariants(db, report):
    """Die vier Invarianten je Zeitraum, auf den Minor-Wert genau: Die erwartete Seite entsteht aus ``get_euer_data`` über
    ``to_minor`` je Zeile, summiert als ``int`` (nie eine Summe von ``float``)."""
    banks = bank_skr_numbers(db.fetch_accounts())
    for period, span in report.periods.items():
        sums = {'income': 0, 'expense': 0, 'other': 0}
        for number, _name, total in db.get_euer_data(*span):
            section = euer_section(number, banks)
            if section is not None:
                sums[section] += to_minor(total)
        assert values_of(report, 'result', period) == sums['income'] + sums['expense'], (period, span)
        assert values_of(report, 'operating_income', period) - values_of(report, 'vat_refund', period) == sums['income'], (period, span)
        assert (values_of(report, 'operating_expenses', period) - values_of(report, 'vat_refund', period)
                == -sums['expense']), (period, span)
        assert report.outside.values[period] == -sums['other'], (period, span)
        assert values_of(report, 'total_revenue', period) == values_of(report, 'revenue', period) + values_of(report, 'other_revenue', period)
        assert values_of(report, 'total_costs', period) == sum(values_of(report, key, period) for key in COST_KEYS)


@pytest.mark.parametrize('variant', ['jahr', 'vorjahr'])
def test_the_bwa_equals_the_euer_to_the_cent_in_the_test_world(world, variant):
    for month in range(1, 13):
        check_invariants(world, world.get_bwa(2026, month, variant))


def test_the_bwa_equals_the_euer_with_payments_and_refunds_to_the_tax_office(tmp_db):
    """Zahlungen ans Finanzamt, eine Erstattung und Konten, die im Monat positiv und aufgelaufen negativ sind: Die Testwelt und der
    Demobestand kennen kein Konto 38xx – hier gelten die Invarianten dort, wo Betriebseinnahmen und -ausgaben von den EÜR-Summen
    um die Erstattungen abweichen."""
    some_books(tmp_db)
    book(tmp_db, 3820, '80.00', '2026-04-20')                        # März −50, April +80: im April eine Erstattung, aufgelaufen +30
    book(tmp_db, 3830, '-100.00', '2026-02-10', name='Umsatzsteuer 1/11')
    book(tmp_db, 3830, '30.00', '2026-04-25')                        # April +30, aufgelaufen −70: im Monat eine Erstattung, aufgelaufen eine Zahlung
    for month in (1, 2, 3, 4, 5, 12):
        for variant in ('jahr', 'vorjahr'):
            check_invariants(tmp_db, tmp_db.get_bwa(2026, month, variant))
    april = tmp_db.get_bwa(2026, 4, 'jahr')
    assert (values_of(april, 'vat_refund', 'month'), values_of(april, 'vat_payment', 'month')) == (to_minor(130), 0)
    assert (values_of(april, 'vat_refund', 'ytd'), values_of(april, 'vat_payment', 'ytd')) == (to_minor(50), to_minor(70))
    assert account_numbers(april, 'vat_refund') == [3820, 3830, 3841] and account_numbers(april, 'vat_payment') == [3830]
    march = tmp_db.get_bwa(2026, 3, 'jahr')
    assert (values_of(march, 'vat_refund'), values_of(march, 'vat_payment')) == (0, to_minor(50))


def _euer_page_sums(page):
    text = re.sub(r'\s+', ' ', html_lib.unescape(re.sub(r'<[^>]+>', ' ', page)))
    return {label: re.search(re.escape(label) + r' (-?[\d.]+,\d\d) €', text).group(1)
            for label in ('Summe Betriebseinnahmen', 'Summe Betriebsausgaben', 'Ergebnis EÜR')}


def _bwa_sums(report, period):
    refund = values_of(report, 'vat_refund', period)
    return {'Summe Betriebseinnahmen': format_amount(values_of(report, 'operating_income', period) - refund),
            'Summe Betriebsausgaben': format_amount(-(values_of(report, 'operating_expenses', period) - refund)),
            'Ergebnis EÜR': format_amount(values_of(report, 'result', period))}


def test_the_euer_page_shows_the_same_sums(world):
    """Was die EÜR-Seite als Summen und Ergebnis zeigt (Zahlen aus ``float``-Summen), zeigt die Auswertung aus den Minor-Werten
    genauso: Betriebseinnahmen − Erstattungen, −(Betriebsausgaben − Erstattungen) und das Ergebnis."""
    year_report, march_report = world.get_bwa(2026, 12, 'jahr'), world.get_bwa(2026, 3, 'jahr')
    cases = [(('2026-01-01', '2026-12-31'), year_report, 'ytd'), (('2026-03-01', '2026-03-31'), march_report, 'month'),
             (('2026-01-01', '2026-03-31'), march_report, 'ytd')]
    for (date_from, date_to), report, period in cases:
        assert report.periods[period] == (date_from, date_to)
        shown = _euer_page_sums(PageDashboard(world, date_from, date_to))
        assert shown == _bwa_sums(report, period), (date_from, date_to)
        assert shown['Ergebnis EÜR'] != '0,00'                              # die Welt zeigt in diesen Zeiträumen etwas


def test_the_test_world_shows_unassigned_accounts(world):
    """Das Konto 1050 der Testwelt hat keine Gruppe: Es steht, wo die Welt es bucht, unter „Nicht zugeordnet (Ausgaben)“ und zählt
    im Ergebnis wie in der EÜR."""
    report = world.get_bwa(2026, 12, 'jahr')
    unassigned = report.line('unassigned_expense')
    assert [account.number for account in unassigned.accounts] == [1050]
    euer = {number: total for number, _name, total in world.get_euer_data('2026-01-01', '2026-12-31')}
    assert unassigned.values['ytd'] == -to_minor(euer[1050]) and unassigned.values['ytd'] != 0
    banks = bank_skr_numbers(world.fetch_accounts())
    others = sum(to_minor(total) for number, total in euer.items()
                 if euer_section(number, banks) in ('income', 'expense') and number != 1050)
    assert values_of(report, 'result', 'ytd') == others + to_minor(euer[1050])
    assert values_of(report, 'result', 'ytd') != others                                       # 1050 zählt im Ergebnis mit
    assert visible(report, unassigned) and UNASSIGNED_TEXT in PageBwa(world, BwaQuery(2026, 12, 'jahr'))


def test_the_world_has_no_income_without_a_group_and_the_money_accounts_do_not_show(world):
    report = world.get_bwa(2026, 12, 'jahr')
    assert report.line('unassigned_income').accounts == []
    shown = {account.number for line in report.lines for account in line.accounts}
    assert not shown & {1600, 1800, 1810, 2100, 4340, 4345, 4405, 10000}


# ═══ Gegen die EÜR: Demobestand ═══════════════════════════════════════════════
@pytest.fixture(scope='module')
def demo_db(tmp_path_factory):
    """Der Demobestand, einmal geladen (Profil ``test``, fester Stichtag – wie ``loaded`` in ``test_demo_history.py``)."""
    root = tmp_path_factory.mktemp('bwa_demo')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, 'DATA_ROOT', str(root / 'data'))
        patch.setattr(auth, 'AUTH_DB', str(root / 'data' / 'auth.db'))
        userctx._mode_cache.clear()
        db = Database(str(root / 'demo.db'))
        db.load_test_seed_data(include_invoices=False, include_dated=False)
        demo_seed.load_demo_scenario(db, today=DEMO_TODAY, profile='test')
    userctx._mode_cache.clear()
    return db


@pytest.mark.parametrize('year, month, variant', [(2026, 10, 'vorjahr'), (2026, 1, 'vorjahr'), (2026, 1, 'jahr'),
                                                  (2025, 12, 'vorjahr'), (2025, 2, 'jahr'), (2026, 6, 'jahr'), (2026, 11, 'vorjahr')])
def test_the_bwa_equals_the_euer_in_the_demo_profile(demo_db, year, month, variant):
    check_invariants(demo_db, demo_db.get_bwa(year, month, variant))


def test_no_account_of_the_demo_data_stands_under_not_assigned(demo_db):
    for year in (2025, 2026):
        for month in range(1, 13):
            report = demo_db.get_bwa(year, month, 'vorjahr')
            assert report.line('unassigned_income').accounts == [] and report.line('unassigned_expense').accounts == [], (year, month)
    report = demo_db.get_bwa(2026, 12, 'jahr')
    assert values_of(report, 'result', 'ytd') != 0 and values_of(report, 'revenue', 'ytd') > 0


def test_the_demo_euer_page_and_the_bwa_show_the_same_result(demo_db):
    report = demo_db.get_bwa(2026, 10, 'jahr')
    for period, (date_from, date_to) in report.periods.items():
        assert _euer_page_sums(PageDashboard(demo_db, date_from, date_to)) == _bwa_sums(report, period), period


# ═══ Adresse ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize('text', ['02026', '26', '２０２６', ' 2026', '2026 ', '+2026', '1899', '3000', '', '20.26', '٢٠٢٦', '2026\n', '-2026'])
def test_a_year_is_read_strictly(text):
    assert parse_year(text) is None


@pytest.mark.parametrize('text', ['0', '13', '001', '1.0', '-1', '٣', '', ' 1', '1 ', '+1', '１', '00', '100'])
def test_a_month_is_read_strictly(text):
    assert parse_month(text) is None


def test_valid_years_months_and_variants_are_read():
    assert [parse_year(text) for text in ('1900', '2026', '2999')] == [1900, 2026, 2999]
    assert [parse_month(text) for text in ('1', '01', '9', '09', '10', '12')] == [1, 1, 9, 9, 10, 12]
    assert parse_variant('jahr') == 'jahr' and parse_variant('vorjahr') == 'vorjahr'
    assert all(parse_variant(text) is None for text in ('', 'Jahr', 'JAHR', ' jahr', 'x', 'jahr ', None, 1))
    assert parse_year(None) is None and parse_month(None) is None and parse_year(2026) is None


def test_the_page_reads_only_the_variant_from_its_address_and_an_unknown_one_is_the_comparison_without_a_note():
    """Jahr und Monat der Seite folgen dem gemeinsamen Zeitraum (``period_month``, ``test_report_period_menu.py``) – aus der Adresse liest die
    Seite nur noch die Darstellung."""
    assert read_variant({}) == 'vorjahr'
    assert read_variant({'variant': ['jahr']}) == 'jahr' and read_variant({'variant': ['vorjahr']}) == 'vorjahr'
    for qs in ({'variant': ['x']}, {'variant': ['JAHR']}, {'variant': [' jahr']}, {'variant': ['']}, {'variant': ['x', 'jahr']},
               {'variant': ['jahr', 'jahr']}, {'variant': ['jahr', 'vorjahr']}, {'year': ['2024'], 'month': ['2']}):
        assert read_variant(qs) == 'vorjahr', qs
    assert read_variant({'year': ['2024'], 'month': ['2'], 'variant': ['jahr']}) == 'jahr'


def test_the_pdf_address_needs_a_valid_period_and_refuses_an_invalid_variant():
    assert read_pdf_query({'year': ['2026'], 'month': ['3']}) == (BwaQuery(2026, 3, 'vorjahr'), None)           # fehlend: Vorjahresvergleich
    assert read_pdf_query({'year': ['2026'], 'month': ['3'], 'variant': ['jahr']}) == (BwaQuery(2026, 3, 'jahr'), None)
    assert read_pdf_query({'year': ['2026'], 'month': ['03'], 'variant': ['vorjahr']}) == (BwaQuery(2026, 3, 'vorjahr'), None)
    for qs in ({}, {'year': ['2026']}, {'month': ['3']}, {'year': ['2026'], 'month': ['13']}, {'year': ['26'], 'month': ['3']},
               {'year': ['2026', '2026'], 'month': ['3']}):
        assert read_pdf_query(qs) == (None, 'Ungültiger Zeitraum.'), qs
    for variant in (['x'], ['JAHR'], [' jahr'], ['jahr', 'jahr'], ['jahr', 'vorjahr']):
        assert read_pdf_query({'year': ['2026'], 'month': ['3'], 'variant': variant}) == (None, INVALID_VARIANT_ERROR), variant
    assert INVALID_VARIANT_ERROR == 'Ungültige Darstellung.'


# ═══ Seite ════════════════════════════════════════════════════════════════════
def table_text(page):
    """Der sichtbare Text der Tabelle: eine Zeile je ``tr``, die Zellen durch ``|`` getrennt."""
    body = page[page.index('<table class="bwaTable">'):page.index('</table>')]
    rows = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', body, flags=re.S):
        cells_ = [html_lib.unescape(re.sub(r'<[^>]+>', '', cell)).strip() for cell in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S)]
        rows.append(' | '.join(cells_))
    return rows


def test_the_page_shows_the_variant_form_the_lines_and_the_accounts(tmp_db):
    some_books(tmp_db)
    page = PageBwa(tmp_db, BwaQuery(2026, 3, 'jahr'))
    dom = Dom(page)
    assert dom.one('form', action='/bwa')['method'] == 'get'
    assert [attrs['name'] for attrs in dom.all('select')] == ['variant'] and dom.one('select', name='variant')['onchange'] == 'this.form.submit()'
    assert dom.one('input', type='hidden', name='from')['value'] == '2026-03-01' and dom.one('input', type='hidden', name='to')['value'] == '2026-03-31'
    assert not [attrs for attrs in dom.all('input') if attrs.get('name') == 'year'] and 'Anzeigen' not in page         # kein Monat, kein Jahr, kein Knopf
    options = [attrs for tag, attrs in dom.tags if tag == 'option']
    assert [attrs['value'] for attrs in options] == ['vorjahr', 'jahr']
    assert [attrs.get('value') for attrs in options if 'selected' in attrs] == ['jahr']
    rows = table_text(page)
    assert rows[0] == ' | März 2026 | Januar bis März 2026'
    assert rows[1].startswith('Position | Betrag € | % der Erlöse | % der Kosten | % der Betriebseinnahmen | Betrag €')
    assert any(row.startswith('▶ Umsatzerlöse | 100,00 | 100,00 | ') for row in rows)
    assert any(row.startswith('4400 Erlöse 19% USt | 100,00') for row in rows)
    assert any(row.startswith('Ergebnis | ') for row in rows) and any(row.startswith('Summe Kosten | ') for row in rows)
    assert 'Nicht im Ergebnis' in rows and any(row.startswith('3160 ') for row in rows)           # „Sonstige Ausgaben“ stehen darunter
    assert any(row.startswith('▶ Nicht zugeordnet (Ausgaben) | 5,00') for row in rows)
    assert 'Betriebswirtschaftliche Auswertung – März 2026' in page
    for text in (SIGN_TEXT, PERCENT_TEXT, bwa.HINT_TEXT, UNASSIGNED_TEXT):
        assert text in page
    assert 'warnColor' not in page


def test_the_accounts_are_closed_at_first_and_belong_to_their_line(tmp_db):
    some_books(tmp_db)
    dom = Dom(PageBwa(tmp_db, BwaQuery(2026, 3, 'vorjahr')))
    children = [attrs for tag, attrs in dom.tags if tag == 'tr' and 'child-row' in attrs.get('class', '')]
    assert children and all('hidden' in attrs and attrs['data-bwa-parent'] for attrs in children)
    parents = {attrs['data-bwa-parent'] for attrs in children}
    heads = {attrs['onclick'] for tag, attrs in dom.tags if tag == 'tr' and 'group-row' in attrs.get('class', '')}
    assert heads == {f"bwaToggle('{key}')" for key in parents}
    icons = {attrs['id'] for tag, attrs in dom.tags if tag == 'span' and attrs.get('id', '').startswith('bwa-icon-')}
    assert icons == {f'bwa-icon-{key}' for key in parents}


def test_the_variant_stays_in_the_row_of_the_pdf_button_and_not_in_the_menu_row(tmp_db):
    """Das Auswahlfeld „Darstellung“ steht im Inhalt, in der Zeile mit dem PDF-Knopf – in der Menüzeile (``header3``) ist es höher als die
    Knöpfe und ließe die Querlinie springen."""
    page = PageBwa(tmp_db, BwaQuery(2026, 3, 'jahr'), '2026-01-01', '2026-12-31', datetime.date(2026, 11, 21))
    menu_row = page[page.index("<div class='header3'>"):page.index('<div class="rectRounded">')]
    assert '<select' not in menu_row and '<form' not in menu_row and 'coloredButton' not in menu_row
    row = page[page.index('<div class="rowWithObjects"><button type="button" class="coloredButton bg-blue btn-sm" onclick="bwaPdf()">'):
               page.index('<div class="splitScroll">')]
    assert row.count('<form') == 1 and row.count('<select') == 1 and row.index('bwaPdf()') < row.index('<form')
    dom = Dom(row)
    assert dom.one('input', type='hidden', name='from')['value'] == '2026-01-01' and dom.one('input', type='hidden', name='to')['value'] == '2026-12-31'


def test_the_page_has_no_note_about_an_invalid_period(tmp_db):
    """Ein ungültiger Zeitraum der Adresse wird wie auf jeder Seite verworfen (``resolve_period``); die Seite hat dafür keinen Hinweis mehr."""
    page = PageBwa(tmp_db, BwaQuery(2026, 10, 'vorjahr'))
    assert 'warnColor' not in page and bwa.INVALID_PERIOD_TEXT not in page and 'ungültig' not in page


def test_the_page_of_an_empty_book_shows_every_line_with_zero_and_no_note_about_unassigned_accounts(tmp_db):
    page = PageBwa(tmp_db, BwaQuery(2026, 10, 'jahr'))
    rows = table_text(page)
    result = next(row for row in rows if row.startswith('Ergebnis | '))
    assert result.split(' | ')[1] == '0,00'
    assert UNASSIGNED_TEXT not in page and 'Nicht im Ergebnis' not in rows
    assert 'Nicht zugeordnet' not in ' '.join(rows)


def test_the_page_uses_no_inline_style_and_only_var_and_function(tmp_db):
    some_books(tmp_db)
    page = PageBwa(tmp_db, BwaQuery(2026, 3, 'vorjahr'))
    own = page[page.index('<div class="rectRounded">'):page.index('<div id="app_confirm"')]
    assert ' style=' not in own and 'innerHTML' not in own
    scripts = Dom(page).scripts
    script = scripts[1]                                                    # hinter dem Skript der Menüzeile (gotoPeriod)
    assert 'gotoPeriod' in scripts[0] and script.startswith('\nvar BWA_DATA = ')
    for forbidden in ('const ', 'let ', '=>', 'innerHTML', 'eval('):
        assert forbidden not in script, forbidden


def test_the_pdf_address_stands_only_in_the_script_block(tmp_db):
    some_books(tmp_db)
    page = PageBwa(tmp_db, BwaQuery(2026, 3, 'jahr'))
    assert '/bwa/pdf' not in re.sub(r'<script>.*?</script>', '', page, flags=re.S)
    data = re.search(r'var BWA_DATA = (.*?);\n', Dom(page).scripts[1], re.S).group(1)
    assert json.loads(data) == {'pdf': '/bwa/pdf?year=2026&month=3&variant=jahr'}


HOSTILE = '<script>alert(1)</script>"x'


def test_names_with_markup_are_escaped(tmp_db, tmp_path):
    benign = Database(db_name=str(tmp_path / 'benign.db'))
    for db, name in ((tmp_db, HOSTILE), (benign, 'Harmloser Name')):
        book(db, 4400, '119.00', '2026-03-10', tax_rate='19', tax_amount='19.00')
        book(db, 6890, '-12.00', '2026-03-11', name=name)
    hostile_page = PageBwa(tmp_db, BwaQuery(2026, 3, 'jahr'))
    benign_page = PageBwa(benign, BwaQuery(2026, 3, 'jahr'))
    assert HOSTILE not in hostile_page and '<script>alert(1)</script>' not in hostile_page
    assert html_lib.escape(HOSTILE, quote=True) in hostile_page
    assert_same_structure(hostile_page, benign_page)
    row = next(row for row in table_text(hostile_page) if row.startswith('6890 '))
    assert row.startswith('6890 ' + HOSTILE)                                     # als Text, nicht als Markup, an seiner Stelle


# ═══ Route: schreibt nichts ═══════════════════════════════════════════════════
PERIOD_COOKIE = 'period=2026-03-01..2026-03-31; Path=/; Max-Age=31536000; SameSite=Lax'


def test_the_page_writes_nothing_into_the_books_and_sets_only_the_cookie_of_the_period(server_port, auto_login):
    """Die Seite und das PDF sind GET-Routen, die nur lesen: Datenbank und Datenordner sind danach unverändert. Die Seite setzt – wie jede Seite
    mit Zeitraum – das Cookie des gemeinsamen Zeitraums, wenn der aus der Adresse kam; das PDF setzt nie eines. Das ist der Nachweis für die
    Positivliste des Diagnose-MCP (``/bwa``)."""
    some_books(auto_login.db)
    before_db, before_tree = dump(auto_login.db.db_name), tree(userctx.DATA_ROOT)
    for path, cookies in (('/bwa', []), ('/bwa?from=2026-03-01&to=2026-03-31', [PERIOD_COOKIE]),
                          ('/bwa?from=2026-03-31&to=2026-03-01&variant=jahr', [PERIOD_COOKIE]), ('/bwa?from=x', []), ('/bwa?year=2026&month=1', []),
                          ('/bwa?variant=jahr&acct=1', [])):
        response = get(server_port, path)
        assert response.status == 200, path
        assert (response.headers.get_all('Set-Cookie') or []) == cookies, path
    for path in ('/bwa/pdf?year=2026&month=3', '/bwa/pdf?year=2026&month=3&variant=jahr&accounts=1', '/bwa/pdf?year=bad',
                 '/bwa/pdf?year=2026&month=3&variant=x', '/bwa/pdf?from=2026-03-01&to=2026-03-31'):
        response = get(server_port, path)
        assert response.headers.get('Set-Cookie') is None, path
        assert response.status in (200, 400), path
    assert dump(auto_login.db.db_name) == before_db
    assert tree(userctx.DATA_ROOT) == before_tree


def test_the_page_answers_with_the_comparison_by_default(server_port, auto_login):
    some_books(auto_login.db)
    response = get(server_port, '/bwa?from=2026-03-01&to=2026-03-31')
    assert response.status == 200 and response.headers['Content-Type'].startswith('text/html')
    text = response.text
    assert 'Betriebswirtschaftliche Auswertung – März 2026' in text and 'Veränderung %' in text and '% der Erlöse' not in text
    assert '% der Erlöse' in get(server_port, '/bwa?from=2026-03-01&to=2026-03-31&variant=jahr').text
    assert '% der Erlöse' not in get(server_port, '/bwa?from=2026-03-01&to=2026-03-31&variant=unbekannt').text      # ungültig: der Standard, still
    bare = get(server_port, '/bwa')
    assert bare.status == 200 and 'Betriebswirtschaftliche Auswertung' in bare.text and 'warnColor' not in bare.text
    assert 'Der gewählte Zeitraum' not in get(server_port, '/bwa?from=2026-13-01&to=2026-03-31').text             # ungültig: wie überall verworfen, ohne Hinweis


def test_the_pdf_route_refuses_what_it_cannot_read(server_port, auto_login):
    for path in ('/bwa/pdf', '/bwa/pdf?year=2026', '/bwa/pdf?year=2026&month=13', '/bwa/pdf?month=3', '/bwa/pdf?year=2026&year=2025&month=3'):
        response = get(server_port, path)
        assert response.status == 400 and response.text == 'Ungültiger Zeitraum.', path
        assert response.headers['Content-Type'] == 'text/plain; charset=utf-8'
    response = get(server_port, '/bwa/pdf?year=2026&month=3&variant=x')
    assert response.status == 400 and response.text == 'Ungültige Darstellung.'
    assert response.headers['Content-Type'] == 'text/plain; charset=utf-8'


# ═══ Menü ═════════════════════════════════════════════════════════════════════
def test_the_navigation_names_the_page():
    assert '<span id="ActivePage">Auswertungen</span>' in Header1('bwa') and '<a href="/bwa">Auswertungen</a>' not in Header1('bwa')
    assert '<a href="/bwa">Auswertungen</a>' in Header1('dashboard') and '<span id="ActivePage">Dashboard</span>' in Header1('dashboard')
    assert Header1('bwa').index('Dashboard</a>') < Header1('bwa').index('Auswertungen</span>') < Header1('bwa').index('>Rechnung<')
    assert '<a href="/bwa">Auswertungen</a>' in Header1('invoice') and 'ActivePage">Auswertungen' not in Header1('invoice')


def test_the_page_shows_the_submenu_of_the_reports_with_the_bwa_active(tmp_db):
    """Seit 2.5.16 steht unter dem Menüpunkt „Auswertungen“ ein Untermenü (BWA, EÜR, seit 2.5.17 die Summen- und Saldenliste, seit 2.6.2 die
    Liquidität); die Seite der BWA ist darin die aktive."""
    page = PageBwa(tmp_db, BwaQuery(2026, 3, 'jahr'))
    assert ("<div class='header2'><span id=\"ActivePage\">BWA</span> | <a href=\"/euer\">EÜR</a> | "
            "<a href=\"/susa\">Summen- und Saldenliste</a> | <a href=\"/liquiditaet\">Liquidität</a></div>") in page
    assert page.index('Auswertungen</span>') < page.index('<span id="ActivePage">BWA</span>') < page.index("<div class='header3'>")
