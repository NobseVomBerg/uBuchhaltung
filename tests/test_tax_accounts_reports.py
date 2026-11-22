# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Steuerkonten der Stammdaten in den Auswertungen (2.8.0, Etappe E8b): die Vorsteuer jedes Satzes in der EÜR und die Steuerkonten je Satz in der Summen- und Saldenliste.

* **Die Auswahl** (``db.tax_rates.select_report_tax_accounts``, rein): Je Art (Umsatzsteuer, Vorsteuer) und Satz zählt der erste Eintrag nach (Reihenfolge, ID), der ein Konto dieser Art
  trägt – aktiv oder nicht; übergangen werden Zeilen, die kein Eintrag sind, Satz 0 und steuerfrei, die vier festen Sätze (19, 16, 7, 5 %) und ein Konto, das es nicht mehr gibt oder
  dessen Nummer heute nicht im Bereich liegt. Der Schlüssel ist die Prozentzahl der Buchungen (``datev_keys.percent``); ``Database._report_tax_accounts`` liest sie im Cursor des Aufrufers.
* **Die Regel der Summen- und Saldenliste** (``susa.tax_account`` und ``entry_postings`` mit den Konten der Stammdaten): Für die vier festen Sätze gilt allein die feste Tabelle, für jeden
  anderen Satz das Konto der Stammdaten, sonst „Steuer ohne Steuerkonto“ mit Hinweis. Der Stapel für das Steuerbüro kennt diese Konten nicht (Byte für Byte gleich).
* **Die EÜR:** Die Vorsteuer einer Ausgabe steht immer da – auf 1401/1406 wie bisher, zu jedem anderen Satz auf dem Vorsteuerkonto der Stammdaten, sonst (auch ein Steuerbetrag ohne Satz und
  19/16/7/5 % bei einem Kontenrahmen ohne 1401/1406) auf der virtuellen Zeile 1409 „Vorsteuer ohne Steuerkonto“. Die Umsatzsteuer bleibt auf der virtuellen 3806, gleich zu welchem Satz.
* **Die Abstimmung:** Probe (c) rechnet die Steuer gemeinsam (EÜR: 3806 und 1409; Liste: 3801, 3806, Umsatzsteuerkonten der Stammdaten und „Steuer ohne Steuerkonto“) und geht in jedem
  Monat auf; BWA (Ergebnis = EÜR-Ergebnis), Liquidität („Nicht zugeordnet“ 0) und Dashboard folgen der EÜR ohne eigene Regel – auch in Beständen aus Zufall mit Sätzen wie 5,5 und 10,7 %.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da (Stichtage sind feste Tage).
"""
import datetime
import html as html_lib
import random
import sqlite3
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal

import pytest

from db import datev_keys, susa
from db.euer import euer_statement
from db.matching import FORM_PURPOSE_FIRST, soll_side
from db.reporting import EUER_INPUT_TAX_UNASSIGNED, EUER_INPUT_TAX_UNASSIGNED_NAME, bank_skr_numbers
from db.tax_rates import FIXED_TAX_BPS, ReportTaxAccounts, select_report_tax_accounts
from money import to_minor
from server.pages_bwa import BwaQuery, PageBwa
from server.pages_dashboard import PageDashboard
from server.pages_euer import EuerQuery, PageEuer
from server.pages_susa import PageSusa, SusaQuery
from tests import liquidity_helpers as liq
from tests import susa_helpers as helpers
from tests.bwa_helpers import some_books
from tests.test_bwa import check_invariants, table_text
from tests.test_euer_view import table_rows
from tests.test_mixed_tax_reports import ending, pay
from tests.test_susa import export_of
from tests.test_tax_entries_bookings import REDUCED, account
from tests.test_tax_entries_bookings import entry as reduced_part
from tests.test_tax_entries_reports import invoice_of

D = Decimal
FIRST, LAST = '2026-03-01', '2026-03-31'                    # der Zeitraum der kleinen Welten


# ── Hilfen ───────────────────────────────────────────────────────────────────

def minor(euro):
    return helpers.minor(euro)


def new_account(db, number, name='Testkonto (erfunden)'):
    """Ein Konto, das der Kontenrahmen nicht führt (ein Nicht-Standardkonto); liefert seine COA-ID."""
    assert db.insert_chart_of_accounts(4, number, name, 'Testkonten', is_standard=0)
    return helpers.coa(db, number)


def rate_entry(db, bp, vat=None, input_tax=None, **more):
    """Ein Eintrag der Steuersätze über ``bp`` Hundertstel Prozent mit dem Erlöskonto 4400 und den genannten Steuerkonten (COA-IDs); liefert seine ID."""
    return db.insert_tax_rate(label=f'Satz {bp} (erfunden)', bp=bp, revenue_coa_id=helpers.coa(db, 4400), vat_coa_id=vat, input_tax_coa_id=input_tax, **more)


def two_tax_accounts(db):
    """Das Vorsteuerkonto 1430 und das Umsatzsteuerkonto 3845 (Testkonten im Bereich) – ``(COA-ID Vorsteuer, COA-ID Umsatzsteuer)``."""
    return new_account(db, 1430, 'Vorsteuer Testsatz'), new_account(db, 3845, 'Umsatzsteuer Testsatz')


def directory(db):
    """Die Steuerkonten der Stammdaten für die Auswertungen, gelesen im Cursor einer eigenen Verbindung."""
    with closing(sqlite3.connect(db.db_name)) as con:
        return db._report_tax_accounts(con.cursor())


def euer(db, span=(FIRST, LAST), accounts=None):
    return db.get_euer_data(*span, accounts) if accounts else db.get_euer_data(*span)


def numbers_of(rows):
    return [row[0] for row in rows]


def bank_with_child(db, date, amount, number, account_name='Probe-Giro', **extra):
    """Eine Bankbewegung mit EINEM Satz darunter (Zweckkonto ``number``, Gegenkonto 1800); ``amount`` in Euro als Text; liefert die ID des Satzes."""
    bank = db.insert_booking(date, D(amount), account_id=helpers.account_id(db, account_name), booking_type='bank')
    return db.insert_booking(date, D(amount), coa_id=helpers.coa(db, number), counter_coa_id=helpers.coa(db, 1800), booking_type='entry', parent_booking_id=bank,
                             entry_form='purpose_first', **extra)


@pytest.fixture
def db(tmp_db):
    liq.two_banks(tmp_db)
    return tmp_db


# ═════════════════════════════════════════════════════════════════════════════
# Die Auswahl (rein)
# ═════════════════════════════════════════════════════════════════════════════

#: Die Konten der reinen Tests: COA-ID → Kontonummer (eine Zahl, ein Text, keine)
CHART = {10: 1430, 11: 1431, 12: 3845, 13: 3846, 14: 4400, 15: 1406, 16: 3806, 17: 3801, 18: None, 19: 1440, 20: 3850, 21: '1432', 22: 1439, 23: 3800, 24: 1400, 25: 3849, 26: 1399}
EMPTY = ReportTaxAccounts({}, {})


def row(entry_id, bp=550, kind='standard', sort=10, vat=None, input_tax=None):
    """Eine Zeile, wie die Datenbank sie liefert: ``(ID, RateBp, Kind, SortOrder, VatCoaId, InputTaxCoaId)``."""
    return entry_id, bp, kind, sort, vat, input_tax


def test_the_first_entry_with_an_account_decides_by_sort_order_then_id():
    rows = [row(1, sort=20, vat=13, input_tax=11), row(2, sort=10, vat=12, input_tax=10)]
    assert select_report_tax_accounts(rows, CHART) == ReportTaxAccounts({5.5: 12}, {5.5: 10})
    same = [row(5, sort=10, vat=12, input_tax=10), row(3, sort=10, vat=13, input_tax=11)]
    assert select_report_tax_accounts(same, CHART) == ReportTaxAccounts({5.5: 13}, {5.5: 11})


def test_each_rate_has_its_own_accounts_and_each_kind_its_own_first_entry():
    rows = [row(1, 550, vat=12, input_tax=10), row(2, 1070, vat=13, input_tax=11), row(3, 1000, vat=None, input_tax=22)]
    assert select_report_tax_accounts(rows, CHART) == ReportTaxAccounts({5.5: 12, 10.7: 13}, {5.5: 10, 10.7: 11, 10.0: 22})


def test_an_earlier_entry_without_an_account_of_a_kind_blocks_no_later_one():
    rows = [row(1, sort=10), row(2, sort=20, vat=12), row(3, sort=30, input_tax=10), row(4, sort=40, vat=13, input_tax=11)]
    assert select_report_tax_accounts(rows, CHART) == ReportTaxAccounts({5.5: 12}, {5.5: 10})         # der vierte Eintrag kommt zu spät: beide Arten sind besetzt


def test_the_result_does_not_depend_on_the_order_of_the_rows():
    rows = [row(1, 550, sort=30, vat=12), row(2, 550, sort=10, vat=13, input_tax=10), row(3, 550, sort=10, input_tax=11), row(4, 1070, sort=20, vat=12),
            row(5, 0, 'exempt', 5, 12, 10), row(6, 1900, sort=1, vat=16, input_tax=15)]
    expected = select_report_tax_accounts(rows, CHART)
    assert expected == ReportTaxAccounts({5.5: 13, 10.7: 12}, {5.5: 10})
    rng = random.Random(2808)
    for _ in range(50):
        assert select_report_tax_accounts(rng.sample(rows, len(rows)), CHART) == expected


def test_the_sort_order_of_a_row_that_is_no_whole_number_counts_as_0_like_everywhere_else():
    rows = [row(1, sort=5, vat=12), row(2, sort=None, vat=13), row(3, sort='x', vat=16)]
    assert select_report_tax_accounts(rows, CHART) == ReportTaxAccounts({5.5: 13}, {})           # 0 vor 5, und bei gleicher Reihenfolge die kleinere ID


@pytest.mark.parametrize('bp', ['550', 550.0, True, None, 0, -550, 10001, 10 ** 9])
def test_a_row_whose_rate_is_not_a_whole_number_from_1_to_10000_is_no_entry(bp):
    assert select_report_tax_accounts([row(1, bp=bp, vat=12, input_tax=10)], CHART) == EMPTY


@pytest.mark.parametrize('kind', ['Standard', 'STANDARD', '', None, 1, 'other', b'standard'])
def test_a_row_of_another_kind_than_standard_or_exempt_is_no_entry(kind):
    assert select_report_tax_accounts([row(1, kind=kind, vat=12, input_tax=10)], CHART) == EMPTY


def test_a_tax_free_entry_and_the_rate_0_carry_no_account_into_the_reports_even_if_the_database_says_so():
    rows = [row(1, 0, 'standard', vat=12, input_tax=10), row(2, 0, 'exempt', vat=12, input_tax=10), row(3, 550, 'exempt', vat=12, input_tax=10)]
    assert select_report_tax_accounts(rows, CHART) == EMPTY


@pytest.mark.parametrize('bp', sorted(FIXED_TAX_BPS))
def test_the_four_fixed_rates_carry_no_account_into_the_reports_even_if_the_database_says_so(bp):
    """Ein per SQL gesetztes Konto an 19, 16, 7 oder 5 % wirkt nicht: Die feste Tabelle der Liste gilt zuerst (``susa.TAX_ACCOUNTS``)."""
    assert select_report_tax_accounts([row(1, bp, vat=12, input_tax=10)], CHART) == EMPTY
    assert select_report_tax_accounts([row(1, bp, vat=12, input_tax=10), row(2, 550, vat=13, input_tax=11)], CHART) == ReportTaxAccounts({5.5: 13}, {5.5: 11})


VAT_ACCOUNTS = (12, 13, 16, 17, 23, 25)                      # 3845, 3846, 3806, 3801, 3800, 3849
INPUT_ACCOUNTS = (10, 11, 15, 22, 24)                        # 1430, 1431, 1406, 1439, 1400
NOT_VAT = (10, 14, 18, 19, 20, 21, 26, 99, True, '12', 12.0)      # Vorsteuer-, Erlöskonto, ohne Nummer, 1440, 3850, eine Nummer als Text, 1399, unbekannt, ein Wahrheitswert, ID als Text/Kommazahl
NOT_INPUT = (12, 14, 18, 19, 20, 21, 26, 99, True, '10', 10.0)


@pytest.mark.parametrize('coa_id', VAT_ACCOUNTS)
def test_the_accounts_of_the_vat_groups_are_sales_tax_accounts_including_3806_and_the_edges(coa_id):
    assert select_report_tax_accounts([row(1, vat=coa_id)], CHART) == ReportTaxAccounts({5.5: coa_id}, {})


@pytest.mark.parametrize('coa_id', INPUT_ACCOUNTS)
def test_the_accounts_of_the_input_tax_group_are_input_tax_accounts_including_1406_and_the_edges(coa_id):
    assert select_report_tax_accounts([row(1, input_tax=coa_id)], CHART) == ReportTaxAccounts({}, {5.5: coa_id})


@pytest.mark.parametrize('coa_id', NOT_VAT)
def test_an_account_that_does_not_exist_has_no_whole_number_or_lies_outside_the_vat_groups_today_is_no_sales_tax_account(coa_id):
    assert select_report_tax_accounts([row(1, vat=coa_id)], CHART) == EMPTY


@pytest.mark.parametrize('coa_id', NOT_INPUT)
def test_an_account_that_does_not_exist_has_no_whole_number_or_lies_outside_the_input_tax_group_today_is_no_input_tax_account(coa_id):
    assert select_report_tax_accounts([row(1, input_tax=coa_id)], CHART) == EMPTY


@pytest.mark.parametrize('bp, stored', [(550, (0.055, 5.5)), (1070, (0.107, 10.7)), (1000, (0.1, 10, 10.0)), (1234, (0.1234, 12.34)), (100, (0.01,))])
def test_the_key_is_the_percent_of_the_bookings(bp, stored):
    """Eine Buchung findet ihren Satz mit derselben Normierung, die für ihren Satz gilt (``datev_keys.percent``): als Bruch und als Prozentzahl gespeichert."""
    found = select_report_tax_accounts([row(1, bp, vat=12, input_tax=10)], CHART)
    assert list(found.vat) == list(found.input) == [bp / 100]
    for value in stored:
        assert found.vat[datev_keys.percent(value)] == 12 and found.input[datev_keys.percent(value)] == 10


# ═════════════════════════════════════════════════════════════════════════════
# Die Datenbankschicht
# ═════════════════════════════════════════════════════════════════════════════

def test_a_fresh_database_has_no_tax_accounts_for_the_reports(tmp_db):
    assert directory(tmp_db) == EMPTY


def test_the_directory_maps_the_percent_of_an_entry_to_the_coa_ids_of_its_accounts(tmp_db):
    input_id, vat_id = two_tax_accounts(tmp_db)
    rate_entry(tmp_db, 550, vat=vat_id, input_tax=input_id)
    found = directory(tmp_db)
    assert found == ReportTaxAccounts({5.5: vat_id}, {5.5: input_id}) and (found.vat, found.input) == ({5.5: vat_id}, {5.5: input_id})


def test_an_inactive_entry_keeps_its_accounts_in_the_reports(tmp_db):
    input_id, vat_id = two_tax_accounts(tmp_db)
    entry_id = rate_entry(tmp_db, 550, vat=vat_id, input_tax=input_id)
    tmp_db.set_tax_rate_active(entry_id, False)
    assert directory(tmp_db) == ReportTaxAccounts({5.5: vat_id}, {5.5: input_id})


def test_an_entry_of_a_fixed_rate_with_accounts_written_around_the_rules_is_ignored(tmp_db):
    input_id, vat_id = two_tax_accounts(tmp_db)
    helpers.raw(tmp_db, 'UPDATE TaxRates SET VatCoaId = ?, InputTaxCoaId = ? WHERE RateBp IN (1900, 700)', vat_id, input_id)
    assert directory(tmp_db) == EMPTY


def test_an_account_whose_number_left_the_group_or_that_the_chart_lost_after_saving_counts_for_nothing(tmp_db):
    input_id, vat_id = two_tax_accounts(tmp_db)
    rate_entry(tmp_db, 550, vat=vat_id, input_tax=input_id)
    helpers.raw(tmp_db, 'UPDATE ChartOfAccounts SET AccountNumber = 9999 WHERE ID = ?', input_id)
    assert directory(tmp_db) == ReportTaxAccounts({5.5: vat_id}, {})
    helpers.raw(tmp_db, 'DELETE FROM ChartOfAccounts WHERE ID = ?', vat_id)
    assert directory(tmp_db) == EMPTY


def test_the_directory_reads_in_the_cursor_of_the_caller_and_writes_nothing(tmp_db):
    input_id, vat_id = two_tax_accounts(tmp_db)
    rate_entry(tmp_db, 550, vat=vat_id, input_tax=input_id)
    with closing(sqlite3.connect(tmp_db.db_name)) as con:
        before = con.execute('SELECT * FROM TaxRates').fetchall()
        found = tmp_db._report_tax_accounts(con.cursor())
        assert found == ReportTaxAccounts({5.5: vat_id}, {5.5: input_id})
        assert con.total_changes == 0 and not con.in_transaction and con.execute('SELECT * FROM TaxRates').fetchall() == before


# ═════════════════════════════════════════════════════════════════════════════
# Die Regel der Summen- und Saldenliste (rein)
# ═════════════════════════════════════════════════════════════════════════════

EXTRA = {susa.TAX_VAT: {5.5: 3845, 10.7: 3846, 19.0: 9999, 7.0: 9998}, susa.TAX_INPUT: {5.5: 1430, 10.7: 1431, 19.0: 9997, 5.0: 9996}}
RATES = ([0.19, 19, 19.0, 0.16, 16, 0.07, 7, 0.05, 5.0, 0.107, 10.7, 0, None, -1, 'abc', float('nan'), 0.2, 21, 0.055, 5.5, 0.0550004, 0.10700000000000001, '0.19', '5,5', 100, 0.01]
         + list(range(0, 101)) + [number / 100 for number in range(0, 101)])


@pytest.mark.parametrize('tax_type', [susa.TAX_VAT, susa.TAX_INPUT])
def test_the_accounts_of_the_master_data_change_nothing_for_a_fixed_rate_and_nothing_for_a_rate_without_an_entry(tax_type):
    """Eigenschaftsprüfung: Für jeden Satz der festen Tabelle (auch mit einem Konto in ``extra``) und jeden Satz ohne Eintrag ist das Ergebnis das von früher; nur ein Satz mit Eintrag
    und außerhalb der Tabelle findet sein Konto."""
    for rate in RATES:
        pct = datev_keys.percent(rate)
        plain = susa.tax_account(tax_type, rate)
        assert susa.tax_account(tax_type, rate, None) == plain == susa.tax_account(tax_type, rate, {}), rate
        if pct in susa.TAX_ACCOUNTS[tax_type] or pct not in EXTRA[tax_type]:
            assert susa.tax_account(tax_type, rate, EXTRA) == plain, rate
        else:
            assert plain == susa.TAX_UNASSIGNED and susa.tax_account(tax_type, rate, EXTRA) == EXTRA[tax_type][pct], rate


def test_a_rate_of_the_master_data_finds_its_account_for_the_kind_of_the_tax_and_no_other():
    for rate in (0.055, 5.5, 0.0550004):
        assert susa.tax_account('vat', rate, EXTRA) == 3845 and susa.tax_account('input', rate, EXTRA) == 1430, rate
    for rate in (0.107, 10.7):
        assert susa.tax_account('vat', rate, EXTRA) == 3846 and susa.tax_account('input', rate, EXTRA) == 1431, rate
    assert susa.tax_account('vat', 0.055, {'input': {5.5: 1430}}) == susa.TAX_UNASSIGNED          # ein Vorsteuerkonto ist kein Umsatzsteuerkonto
    assert susa.tax_account('input', 0.055, {'vat': {5.5: 3845}}) == susa.TAX_UNASSIGNED
    assert susa.tax_account('vat', 0.055, {'vat': {}}) == susa.tax_account('vat', 0.055, {}) == susa.TAX_UNASSIGNED


WAITING = (4340, 4345, 4405)


def SORT_KEY(item):                                           # noqa: N802  (die Schlüssel der Buchungen sind Zahlen und Text gemischt)
    return str(item[0]), item[1], item[2]



def postings_of(form, amount, coa, counter, tax, rate, extra=None):
    """``(Buchungen als (Konto, Soll, Haben), Hinweise)`` eines Satzes, mit der Soll-Seite und der Art, die der Export dafür liest."""
    kind = datev_keys.booking_kind(form, coa, counter, amount > 0, WAITING)
    found, notices = susa.entry_postings(form, soll_side(form), kind, coa, counter, amount, tax, rate, WAITING, extra_tax_accounts=extra)
    return sorted(((posting.key, posting.soll, posting.haben) for posting in found), key=SORT_KEY), notices


GROSS, NET, TAX = 1_055_000, 1_000_000, 55_000                  # 105,50 € brutto: 100,00 € netto und 5,50 € Steuer (5,5 %)


def test_an_expense_with_a_rate_of_the_master_data_books_its_tax_on_the_input_tax_account_without_a_notice():
    expected = sorted([(6815, NET, 0), (1430, TAX, 0), (1800, 0, GROSS)], key=SORT_KEY)
    assert postings_of(FORM_PURPOSE_FIRST, -GROSS, 6815, 1800, -TAX, 0.055, EXTRA) == (expected, set())
    unassigned = sorted([(6815, NET, 0), (susa.TAX_UNASSIGNED, TAX, 0), (1800, 0, GROSS)], key=SORT_KEY)
    for extra in (None, {}, {susa.TAX_INPUT: {10.7: 1431}}, {susa.TAX_VAT: {5.5: 3845}}):          # ohne Konto zu diesem Satz und Art: wie bisher
        assert postings_of(FORM_PURPOSE_FIRST, -GROSS, 6815, 1800, -TAX, 0.055, extra) == (unassigned, {'tax_unassigned'}), extra


def test_a_sale_with_a_rate_of_the_master_data_books_its_tax_on_the_sales_tax_account_without_a_notice():
    expected = sorted([(1800, GROSS, 0), (4400, 0, NET), (3845, 0, TAX)], key=SORT_KEY)
    assert postings_of(FORM_PURPOSE_FIRST, GROSS, 4400, 1800, TAX, 0.055, EXTRA) == (expected, set())
    unassigned = sorted([(1800, GROSS, 0), (4400, 0, NET), (susa.TAX_UNASSIGNED, 0, TAX)], key=SORT_KEY)
    assert postings_of(FORM_PURPOSE_FIRST, GROSS, 4400, 1800, TAX, 0.055) == (unassigned, {'tax_unassigned'})
    assert postings_of(FORM_PURPOSE_FIRST, GROSS, 4400, 1800, TAX, 0.055, {susa.TAX_INPUT: {5.5: 1430}}) == (unassigned, {'tax_unassigned'})


def test_a_fixed_rate_books_on_its_fixed_accounts_whatever_the_master_data_names():
    """19 % auf 1406/3806 und 7 % auf 1401/3801, auch wenn ``extra`` für den Satz Konten nennt – die feste Tabelle gilt zuerst."""
    expense = sorted([(6815, 1_000_000, 0), (1406, 190_000, 0), (1800, 0, 1_190_000)], key=SORT_KEY)
    assert postings_of(FORM_PURPOSE_FIRST, -1_190_000, 6815, 1800, -190_000, 0.19, EXTRA) == (expense, set())
    sale = sorted([(1800, 1_070_000, 0), (4300, 0, 1_000_000), (3801, 0, 70_000)], key=SORT_KEY)
    assert postings_of(FORM_PURPOSE_FIRST, 1_070_000, 4300, 1800, 70_000, 0.07, EXTRA) == (sale, set())


def test_the_accounts_of_the_master_data_are_read_by_the_group_book_and_the_context_default_changes_nothing():
    row_ = susa.GroupRow('purpose_first', 3, 2, False, 0.055, 'month', None, 2, -2 * GROSS, -2 * TAX)
    context = susa.SusaContext(frozenset({2}), {2: 1800, 3: 6815}, {}, WAITING)
    assert context.tax_accounts is None
    tally = susa.Tally()
    susa.book_group(tally, context, row_)
    assert tally.notices() == [susa.SusaNotice('tax_unassigned', 2, 2 * TAX)]
    tally = susa.Tally()
    susa.book_group(tally, susa.SusaContext(frozenset({2}), {2: 1800, 3: 6815}, {}, WAITING, EXTRA), row_)
    assert tally.notices() == [] and ('month', susa.Posting(1430, 2 * TAX, 0)) in tally.postings()


def test_the_notice_for_a_rate_without_a_tax_account_names_the_master_data():
    for count, text in ((1, '1 Steuerbetrag zu einem Steuersatz ohne Steuerkonto (5,50 €) steht unter „Steuer ohne Steuerkonto“'),
                        (2, '2 Steuerbeträge zu einem Steuersatz ohne Steuerkonto (5,50 €) stehen unter „Steuer ohne Steuerkonto“')):
        sentence = susa.notice_text(susa.SusaNotice(susa.TAX_UNASSIGNED_NOTICE, count, TAX))
        assert sentence == f'{text} – ein Steuerkonto je Satz lässt sich in den Stammdaten unter ‚Steuersätze‘ hinterlegen.'
        assert 'DATEV' not in sentence and 'WISO' not in sentence


# ═════════════════════════════════════════════════════════════════════════════
# Die Summen- und Saldenliste in der SuSa-Welt mit 5,5 %
# ═════════════════════════════════════════════════════════════════════════════

def susa_world_with_5_5(db):
    """Die SuSa-Welt (``helpers.build_susa_world``) und im März 2026 zwei Sätze zu 5,5 %: eine Ausgabe über 105,50 auf 5505 (Vorsteuer 5,50) und eine bezahlte Rechnung über 105,50
    (Umsatzsteuer 5,50, Erlös auf 4150)."""
    helpers.build_susa_world(db)
    helpers.mask(db, 'Probe-Giro', 5505, '-105.50', '2026-03-12', '5.5', '-5.50', document='ER-55')
    pay(db, invoice_of(db, 'H-55', [reduced_part('5.5', '100.00', account(db, REDUCED))], '2026-03-14'), '105.50', '2026-03-20')
    return db


MONTHS = [(2025, 12)] + helpers.every_month(2026)


def assert_every_probe_holds(db, where=''):
    """Die Abstimmungsproben (a), (b) und (c) in jedem Monat – (c) mit der gemeinsamen Steuer."""
    deviations = []
    for year, month in MONTHS:
        report = db.get_susa(year, month)
        helpers.assert_balanced(report, (where, year, month))
        deviations += helpers.check_bank_accounts(db, report, (where, year, month))
        deviations += helpers.check_erfolgskonten_against_euer(db, report, (where, year, month))
    assert deviations == []


def tax_notice(report):
    return [(notice.key, notice.count, notice.amount) for notice in report.notices if notice.key == susa.TAX_UNASSIGNED_NOTICE]


def test_without_accounts_the_tax_of_5_5_percent_stands_on_the_account_without_a_tax_account_with_the_notice(tmp_db):
    susa_world_with_5_5(tmp_db)
    report = tmp_db.get_susa(2026, 3)
    unassigned = helpers.accounts_of(report)[susa.TAX_UNASSIGNED]
    assert (unassigned.month_soll, unassigned.month_haben) == (minor('5.50'), minor('5.50'))             # die Vorsteuer der Ausgabe im Soll, die Umsatzsteuer der Rechnung im Haben
    assert tax_notice(report) == [(susa.TAX_UNASSIGNED_NOTICE, 2, minor('11.00'))]
    assert tax_notice(tmp_db.get_susa(2026, 2)) == []
    assert_every_probe_holds(tmp_db, 'ohne')


def test_with_accounts_the_tax_of_5_5_percent_stands_on_them_without_a_notice_and_without_the_suffix(tmp_db):
    susa_world_with_5_5(tmp_db)
    input_id, vat_id = two_tax_accounts(tmp_db)
    rate_entry(tmp_db, 550, vat=vat_id, input_tax=input_id)
    report = tmp_db.get_susa(2026, 3)
    accounts = helpers.accounts_of(report)
    assert (accounts[1430].month_soll, accounts[1430].month_haben) == (minor('5.50'), 0)                       # die Vorsteuer: Soll
    assert (accounts[3845].month_soll, accounts[3845].month_haben) == (0, minor('5.50'))                       # die Umsatzsteuer: Haben
    assert not susa.visible(accounts[susa.TAX_UNASSIGNED]) and tax_notice(report) == []
    assert [(accounts[number].name, accounts[number].outside_chart) for number in (1430, 3845)] == [('Vorsteuer Testsatz', False), ('Umsatzsteuer Testsatz', False)]
    assert not any(account.outside_chart for account in accounts.values())
    assert 'nicht im Kontenrahmen' not in ''.join(account.name for account in accounts.values())
    assert_every_probe_holds(tmp_db, 'mit')


def test_a_rate_with_only_one_of_the_two_accounts_moves_only_that_kind(tmp_db):
    susa_world_with_5_5(tmp_db)
    input_id, vat_id = two_tax_accounts(tmp_db)
    entry_id = rate_entry(tmp_db, 550, input_tax=input_id)
    accounts = helpers.accounts_of(tmp_db.get_susa(2026, 3))
    assert accounts[1430].month_soll == minor('5.50') and accounts[susa.TAX_UNASSIGNED].month_haben == minor('5.50') and accounts[susa.TAX_UNASSIGNED].month_soll == 0
    assert 3845 not in accounts
    helpers.raw(tmp_db, 'UPDATE TaxRates SET InputTaxCoaId = NULL, VatCoaId = ? WHERE ID = ?', vat_id, entry_id)
    accounts = helpers.accounts_of(tmp_db.get_susa(2026, 3))
    assert accounts[3845].month_haben == minor('5.50') and accounts[susa.TAX_UNASSIGNED].month_soll == minor('5.50') and accounts[susa.TAX_UNASSIGNED].month_haben == 0
    assert 1430 not in accounts or not susa.visible(accounts[1430])
    assert_every_probe_holds(tmp_db, 'eine Art')


def test_the_export_for_the_tax_office_is_byte_for_byte_the_same_with_and_without_accounts(tmp_db, monkeypatch):
    """Der Stapel kennt die Konten der Stammdaten nicht: Er liest ``tax_account`` und ``entry_postings`` nicht."""
    susa_world_with_5_5(tmp_db)
    span = ('2026-01-01', '2026-12-31')
    without = export_of(tmp_db, *span, monkeypatch)
    input_id, vat_id = two_tax_accounts(tmp_db)
    rate_entry(tmp_db, 550, vat=vat_id, input_tax=input_id)
    with_accounts = export_of(tmp_db, *span, monkeypatch)
    assert with_accounts[0] == without[0] and with_accounts[1] == without[1] and len(without[1]) > 20


@pytest.mark.parametrize('bp', sorted(FIXED_TAX_BPS))
def test_an_entry_of_a_fixed_rate_with_accounts_written_around_the_rules_changes_nothing_in_the_list_and_in_the_euer(tmp_db, bp):
    """19, 16, 7 und 5 % bleiben auf 1401/1406 und 3801/3806, auch wenn ein per SQL geschriebener Eintrag für den Satz Konten nennt."""
    susa_world_with_5_5(tmp_db)
    before = ([tmp_db.get_susa(2026, month) for month in range(1, 13)], tmp_db.get_euer_data('2026-01-01', '2026-12-31'))
    input_id, vat_id = two_tax_accounts(tmp_db)
    helpers.raw(tmp_db, 'INSERT INTO TaxRates (Label, RateBp, Kind, VatCoaId, InputTaxCoaId, Active, SortOrder) VALUES (?, ?, ?, ?, ?, 1, 5)', f'Satz {bp} (erfunden)', bp,
                'standard', vat_id, input_id)
    helpers.raw(tmp_db, 'UPDATE TaxRates SET VatCoaId = ?, InputTaxCoaId = ? WHERE RateBp = ?', vat_id, input_id, bp)
    assert ([tmp_db.get_susa(2026, month) for month in range(1, 13)], tmp_db.get_euer_data('2026-01-01', '2026-12-31')) == before
    assert before[1] != [] and 1430 not in numbers_of(before[1]) and 3845 not in numbers_of(before[1])


def test_the_reports_read_the_master_data_of_today_so_the_tax_of_earlier_months_moves_with_an_entry_but_no_sum_changes(tmp_db):
    """Die Rückwirkung ist gewollt: Wer einem Eintrag ein Konto gibt oder nimmt, verschiebt die Steuer früherer Monate zwischen dem Konto und „Steuer ohne Steuerkonto“ bzw.
    der Zeile 1409 – Summen und Ergebnis bleiben gleich."""
    susa_world_with_5_5(tmp_db)
    banks = bank_skr_numbers(tmp_db.fetch_accounts())
    result = lambda: euer_statement(tmp_db.get_euer_data('2026-03-01', '2026-03-31'), banks).result       # noqa: E731
    before_result, before_total = result(), tmp_db.get_susa(2026, 3).total
    before_unassigned = helpers.accounts_of(tmp_db.get_susa(2026, 3))[susa.TAX_UNASSIGNED].month_soll
    input_id, vat_id = two_tax_accounts(tmp_db)
    rate_entry(tmp_db, 550, vat=vat_id, input_tax=input_id)
    assert result() == before_result and tmp_db.get_susa(2026, 3).total == before_total
    assert before_unassigned == minor('5.50') and not susa.visible(helpers.accounts_of(tmp_db.get_susa(2026, 3))[susa.TAX_UNASSIGNED])


def test_the_list_follows_a_renumbered_account_and_falls_back_to_the_account_without_a_tax_account(tmp_db):
    susa_world_with_5_5(tmp_db)
    input_id, vat_id = two_tax_accounts(tmp_db)
    rate_entry(tmp_db, 550, vat=vat_id, input_tax=input_id)
    helpers.raw(tmp_db, 'UPDATE ChartOfAccounts SET AccountNumber = 9998 WHERE ID = ?', input_id)
    helpers.raw(tmp_db, 'UPDATE ChartOfAccounts SET AccountNumber = 9999 WHERE ID = ?', vat_id)
    report = tmp_db.get_susa(2026, 3)
    assert helpers.accounts_of(report)[susa.TAX_UNASSIGNED].month_soll == minor('5.50') and tax_notice(report) == [(susa.TAX_UNASSIGNED_NOTICE, 2, minor('11.00'))]
    assert_every_probe_holds(tmp_db, 'umbenannt')


def test_the_page_names_the_master_data_in_the_notice_and_shows_the_accounts_with_their_names(tmp_db):
    susa_world_with_5_5(tmp_db)
    notice = susa.notice_text(susa.SusaNotice(susa.TAX_UNASSIGNED_NOTICE, 2, minor('11.00')))
    page = str(PageSusa(tmp_db, SusaQuery(2026, 3)))
    assert f'<li>{html_lib.escape(notice, quote=True)}</li>' in page and 'unter ‚Steuersätze‘ hinterlegen' in page
    input_id, vat_id = two_tax_accounts(tmp_db)
    rate_entry(tmp_db, 550, vat=vat_id, input_tax=input_id)
    page = str(PageSusa(tmp_db, SusaQuery(2026, 3)))
    assert 'unter ‚Steuersätze‘' not in page
    rows = [line for line in table_rows(page) if line.startswith(('1430 |', '3845 |'))]
    assert [line.split(' | ')[:2] for line in rows] == [['1430', 'Vorsteuer Testsatz'], ['3845', 'Umsatzsteuer Testsatz']]


# ═════════════════════════════════════════════════════════════════════════════
# EÜR
# ═════════════════════════════════════════════════════════════════════════════

def test_an_expense_with_a_rate_outside_the_four_has_its_input_tax_on_the_virtual_line(db):
    helpers.mask(db, 'Probe-Giro', 6815, '-105.50', '2026-03-10', '5.5', '-5.50')
    rows = euer(db)
    assert [(number, total) for number, _name, total in rows] == [(1409, -5.5), (6815, -100.0)]
    assert rows[0][1] == EUER_INPUT_TAX_UNASSIGNED_NAME == 'Vorsteuer ohne Steuerkonto' and EUER_INPUT_TAX_UNASSIGNED == 1409
    statement = euer_statement(rows, bank_skr_numbers(db.fetch_accounts()))
    assert (statement.expense_sum, statement.result) == (-minor('105.50'), -minor('105.50')) and statement.income == ()       # das Konto hat 105,50 bezahlt


def test_the_input_tax_account_of_the_master_data_takes_the_tax_with_its_name_from_the_chart(db):
    input_id, _vat_id = two_tax_accounts(db)
    rate_entry(db, 550, input_tax=input_id)
    helpers.mask(db, 'Probe-Giro', 6815, '-105.50', '2026-03-10', '5.5', '-5.50')
    rows = euer(db)
    assert [(number, total) for number, _name, total in rows] == [(1430, -5.5), (6815, -100.0)] and rows[0][1] == 'Vorsteuer Testsatz'
    assert 1409 not in numbers_of(rows)


def test_a_tax_amount_without_a_rate_stands_on_the_virtual_line(db):
    bank_with_child(db, '2026-03-10', '-105.50', 6815, tax_amount=D('-5.50'))
    assert [(number, total) for number, _name, total in euer(db)] == [(1409, -5.5), (6815, -100.0)]


def test_input_tax_below_one_cent_gives_no_virtual_line_and_from_one_cent_it_does(db):
    """Wie bei der virtuellen 3806 und jeder anderen Zeile: Ein Saldo unter einem Cent steht nicht da. Gerechnet wird in Minor Units (ein Cent = 100)."""
    giro = helpers.account_id(db, 'Probe-Giro')
    for number, tax in ((6815, -40), (6850, -60)):
        bank = helpers.raw_bank(db, '2026-03-10', giro, -minor('1.00'))
        helpers.raw_entry(db, '2026-03-10', number, 1800, -minor('1.00'), parent=bank, rate=0.055, tax=tax)
        found = [row_ for row_ in euer(db) if row_[0] == 1409]
        assert found == ([] if tax == -40 else [(1409, EUER_INPUT_TAX_UNASSIGNED_NAME, -0.01)]), (number, tax)       # 0,004 € für sich, mit 0,006 € zusammen 0,01 €


@pytest.mark.parametrize('rate, gross, tax, number', [('19', '-119.00', '-19.00', 1406), ('7', '-107.00', '-7.00', 1401), ('16', '-116.00', '-16.00', 1406),
                                                       ('5', '-105.00', '-5.00', 1401)])
def test_the_four_fixed_rates_keep_their_input_tax_accounts(db, rate, gross, tax, number):
    helpers.mask(db, 'Probe-Giro', 6815, gross, '2026-03-10', rate, tax)
    rows = euer(db)
    assert numbers_of(rows) == [number, 6815] and rows[0][2] == float(tax) and 1409 not in numbers_of(rows)


@pytest.mark.parametrize('rate, gross, tax, number', [('19', '-119.00', '-19.00', 1406), ('7', '-107.00', '-7.00', 1401)])
def test_a_fixed_rate_whose_input_tax_account_the_chart_does_not_carry_stands_on_the_virtual_line_and_the_list_still_books_it_on_the_fixed_account(db, rate, gross, tax, number):
    """Ein Kontenrahmen ohne 1401/1406: Bis 2.7.0 fehlte die Vorsteuer in der EÜR. Jetzt steht sie auf der virtuellen Zeile; die Liste bucht sie auf das feste Konto (mit dem Zusatz
    „nicht im Kontenrahmen“) – die Probe (c) rechnet das zusammen."""
    helpers.raw(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = ?', number)
    helpers.mask(db, 'Probe-Giro', 6815, gross, '2026-03-10', rate, tax)
    assert [(n, t) for n, _name, t in euer(db)] == [(1409, float(tax)), (6815, float(D(gross) - D(tax)))]
    report = db.get_susa(2026, 3)
    assert helpers.accounts_of(report)[number].outside_chart and helpers.accounts_of(report)[number].month_soll == minor(str(-D(tax)))
    assert helpers.check_erfolgskonten_against_euer(db, report, 'ohne Konto') == []


def test_a_tax_rate_stored_as_a_percent_number_stands_on_the_fixed_account_like_in_the_list(db):
    """``TaxRate = 19`` (eine Prozentzahl statt 0,19 – kein Weg der App schreibt sie, die Liste liest sie so): auf 1406 und nicht auf der virtuellen Zeile."""
    bank = helpers.raw_bank(db, '2026-03-10', helpers.account_id(db, 'Probe-Giro'), -minor('119.00'))
    helpers.raw_entry(db, '2026-03-10', 6815, 1800, -minor('119.00'), parent=bank, rate=19, tax=-minor('19.00'))
    assert numbers_of(euer(db)) == [1406, 6815] and euer(db)[0][2] == -19.0
    report = db.get_susa(2026, 3)
    assert helpers.accounts_of(report)[1406].month_soll == minor('19.00') and helpers.check_erfolgskonten_against_euer(db, report) == []


@pytest.mark.parametrize('stored', [0.055, 5.5, 0.0550004])
def test_a_rate_is_read_with_the_normalisation_of_the_bookings_in_the_euer_and_in_the_list(db, stored):
    input_id, vat_id = two_tax_accounts(db)
    rate_entry(db, 550, vat=vat_id, input_tax=input_id)
    bank = helpers.raw_bank(db, '2026-03-10', helpers.account_id(db, 'Probe-Giro'), -minor('105.50'))
    helpers.raw_entry(db, '2026-03-10', 6815, 1800, -minor('105.50'), parent=bank, rate=stored, tax=-minor('5.50'))
    assert numbers_of(euer(db)) == [1430, 6815]
    report = db.get_susa(2026, 3)
    assert helpers.accounts_of(report)[1430].month_soll == minor('5.50') and helpers.check_erfolgskonten_against_euer(db, report) == []


def test_the_sales_tax_of_any_rate_stays_on_the_virtual_3806_even_with_a_sales_tax_account(db):
    """``VatCoaId`` wirkt nur in der Summen- und Saldenliste: Die EÜR kennt EINE Zeile „vereinnahmte Umsatzsteuer“, auch zu 5,5 %."""
    input_id, vat_id = two_tax_accounts(db)
    rate_entry(db, 550, vat=vat_id, input_tax=input_id)
    helpers.mask(db, 'Probe-Giro', 4400, '105.50', '2026-03-10', '5.5', '5.50')
    rows = euer(db)
    assert [(number, total) for number, _name, total in rows] == [(3806, 5.5), (4400, 100.0)] and 3845 not in numbers_of(rows) and 1409 not in numbers_of(rows)
    report = db.get_susa(2026, 3)
    assert ending(report, 3845) == -minor('5.50') and ending(report, susa.TAX_UNASSIGNED) == 0
    assert helpers.check_erfolgskonten_against_euer(db, report) == []


def test_the_result_is_the_money_that_moved_to_the_minor_unit(db):
    helpers.mask(db, 'Probe-Giro', 5505, '-105.50', '2026-03-10', '5.5', '-5.50')
    helpers.mask(db, 'Probe-Zweit', 6815, '-110.70', '2026-03-11', '10.7', '-10.70')
    helpers.mask(db, 'Probe-Giro', 4400, '105.50', '2026-03-12', '5.5', '5.50')
    statement = euer_statement(euer(db), bank_skr_numbers(db.fetch_accounts()))
    assert statement.result == statement.income_sum + statement.expense_sum == to_minor(db.get_dashboard(FIRST, LAST)[0]['balance']) == -minor('110.70')
    assert (statement.income_sum, statement.expense_sum) == (minor('105.50'), -minor('216.20'))          # 100,00 + 5,50 Umsatzsteuer; 200,00 und 16,20 Vorsteuer
    assert [line.number for line in statement.expense if line.number == 1409] == [1409]


def test_the_virtual_line_stands_by_number_between_the_fixed_accounts_and_the_expenses(db):
    helpers.mask(db, 'Probe-Giro', 6815, '-119.00', '2026-03-10', '19', '-19.00')
    helpers.mask(db, 'Probe-Giro', 6850, '-105.50', '2026-03-11', '5.5', '-5.50')
    assert numbers_of(euer(db)) == [1406, 1409, 6815, 6850]


def test_the_account_filter_follows_the_same_rule(db):
    giro, zweit = helpers.account_id(db, 'Probe-Giro'), helpers.account_id(db, 'Probe-Zweit')
    helpers.mask(db, 'Probe-Giro', 6815, '-105.50', '2026-03-10', '5.5', '-5.50')
    assert numbers_of(euer(db, accounts=[giro])) == [1409, 6815] and numbers_of(euer(db, accounts=[zweit])) == []
    assert euer(db, accounts=[giro, zweit]) == euer(db)


def test_the_first_entry_by_order_and_id_decides_the_input_tax_account_of_a_rate(db):
    low, high = new_account(db, 1430), new_account(db, 1431)
    rate_entry(db, 550, input_tax=high, sort_order=20)
    rate_entry(db, 550, input_tax=low, sort_order=10)
    helpers.mask(db, 'Probe-Giro', 6815, '-105.50', '2026-03-10', '5.5', '-5.50')
    assert numbers_of(euer(db)) == [1430, 6815]


def test_an_inactive_entry_keeps_its_input_tax_account_in_the_euer(db):
    entry_id = rate_entry(db, 550, input_tax=new_account(db, 1430))
    db.set_tax_rate_active(entry_id, False)
    helpers.mask(db, 'Probe-Giro', 6815, '-105.50', '2026-03-10', '5.5', '-5.50')
    assert numbers_of(euer(db)) == [1430, 6815]


def test_an_account_that_left_its_group_or_the_chart_returns_the_tax_to_the_virtual_line(db):
    input_id = new_account(db, 1430)
    rate_entry(db, 550, input_tax=input_id)
    helpers.mask(db, 'Probe-Giro', 6815, '-105.50', '2026-03-10', '5.5', '-5.50')
    assert numbers_of(euer(db)) == [1430, 6815]
    helpers.raw(db, 'UPDATE ChartOfAccounts SET AccountNumber = 9999 WHERE ID = ?', input_id)
    assert numbers_of(euer(db)) == [1409, 6815]
    helpers.raw(db, 'DELETE FROM ChartOfAccounts WHERE ID = ?', input_id)
    assert numbers_of(euer(db)) == [1409, 6815]


def test_a_real_account_1409_with_a_balance_and_the_virtual_line_stand_side_by_side(db):
    """Wie die echte und die virtuelle 3806: zwei Zeilen mit derselben Nummer, die echte zuerst; die BWA fasst sie unter dem zuerst gefundenen Namen zusammen."""
    new_account(db, 1409, 'Vorsteuer Testkonto 1409')
    helpers.mask(db, 'Probe-Giro', 1409, '-50.00', '2026-03-09')
    helpers.mask(db, 'Probe-Giro', 6815, '-105.50', '2026-03-10', '5.5', '-5.50')
    rows = euer(db)
    assert [row_ for row_ in rows if row_[0] == 1409] == [(1409, 'Vorsteuer Testkonto 1409', -50.0), (1409, EUER_INPUT_TAX_UNASSIGNED_NAME, -5.5)]
    statement = euer_statement(rows, bank_skr_numbers(db.fetch_accounts()))
    assert [(line.name, line.minor) for line in statement.expense if line.number == 1409] == [('Vorsteuer Testkonto 1409', -minor('50.00')),
                                                                                              (EUER_INPUT_TAX_UNASSIGNED_NAME, -minor('5.50'))]
    nachweis = [(found.number, found.name, found.values['month']) for found in db.get_bwa(2026, 3, 'jahr').line('input_vat').accounts]
    assert nachweis == [(1409, 'Vorsteuer Testkonto 1409', minor('55.50'))]
    check_invariants(db, db.get_bwa(2026, 3, 'jahr'))


def test_a_sale_of_an_asset_on_6885_with_5_5_percent_stands_as_input_tax_in_the_euer_and_as_sales_tax_in_the_list(db):
    """Bekannt und festgehalten (``EUER_DIFF_TEXT``): Auf 6884–6892 liest die EÜR den Steueranteil als Vorsteuer (kein Erlöskonto 4000–4999), die Liste als Umsatzsteuer (Erlös). Ohne Konten der
    Stammdaten rechnet die gemeinsame Probe beides zusammen; mit beiden Konten steht die Vorsteuer in der EÜR auf dem Vorsteuerkonto, die Liste bucht auf das Umsatzsteuerkonto – die Probe zeigt es."""
    helpers.mask(db, 'Probe-Giro', 6885, '105.50', '2026-03-10', '5.5', '5.50')
    assert [(number, total) for number, _name, total in euer(db)] == [(1409, 5.5), (6885, 100.0)]
    report = db.get_susa(2026, 3)
    assert helpers.accounts_of(report)[susa.TAX_UNASSIGNED].month_haben == minor('5.50') and helpers.check_erfolgskonten_against_euer(db, report) == []
    input_id, vat_id = two_tax_accounts(db)
    rate_entry(db, 550, vat=vat_id, input_tax=input_id)
    assert [(number, total) for number, _name, total in euer(db)] == [(1430, 5.5), (6885, 100.0)]
    report = db.get_susa(2026, 3)
    assert helpers.accounts_of(report)[3845].month_haben == minor('5.50') and ending(report, 1430) == 0
    assert helpers.check_erfolgskonten_against_euer(db, report, 'w') == [('w', 'month', 1430, minor('5.50'), 0), ('w', 'ytd', 1430, minor('5.50'), 0)]


# ═════════════════════════════════════════════════════════════════════════════
# BWA, Liquidität, Dashboard und die Seiten
# ═════════════════════════════════════════════════════════════════════════════

def books_of_unusual_rates(db):
    """Fünf Zahlungen zu 5,5, 10 und 10,7 % – Ausgaben und Einnahmen – im März und April 2026."""
    helpers.mask(db, 'Probe-Giro', 5505, '-105.50', '2026-03-12', '5.5', '-5.50')
    helpers.mask(db, 'Probe-Zweit', 6815, '-110.00', '2026-03-13', '10', '-10.00')
    helpers.mask(db, 'Probe-Giro', 4400, '105.50', '2026-03-14', '5.5', '5.50')
    helpers.mask(db, 'Probe-Giro', 4300, '110.70', '2026-04-02', '10.7', '10.70')
    helpers.mask(db, 'Probe-Zweit', 5170, '-110.70', '2026-04-03', '10.7', '-10.70')


def master_data_of_the_unusual_rates(db):
    """Die Steuerkonten: 5,5 % mit beiden, 10 % nur mit dem Vorsteuerkonto, 10,7 % nur mit dem Umsatzsteuerkonto."""
    input_id, vat_id = two_tax_accounts(db)
    other_input, other_vat = new_account(db, 1431, 'Vorsteuer Zehn'), new_account(db, 3846, 'Umsatzsteuer Zehn Komma Sieben')
    rate_entry(db, 550, vat=vat_id, input_tax=input_id)
    rate_entry(db, 1000, input_tax=other_input)
    rate_entry(db, 1070, vat=other_vat)


@pytest.mark.parametrize('with_accounts', [False, True])
def test_the_bwa_equals_the_euer_in_every_month_and_names_the_account_of_the_input_tax(db, with_accounts):
    books_of_unusual_rates(db)
    if with_accounts:
        master_data_of_the_unusual_rates(db)
    for month in (2, 3, 4, 5):
        for variant in ('jahr', 'vorjahr'):
            check_invariants(db, db.get_bwa(2026, month, variant))
    march = db.get_bwa(2026, 3, 'jahr').line('input_vat')
    names = {number: name for number, name in ((found.number, found.name) for found in march.accounts)}
    expected = {1430: 'Vorsteuer Testsatz', 1431: 'Vorsteuer Zehn', 1409: EUER_INPUT_TAX_UNASSIGNED_NAME}
    assert names == ({1409: expected[1409]} if not with_accounts else {1430: expected[1430], 1431: expected[1431]})
    assert march.values['month'] == minor('15.50')                                                       # 5,50 und 10,00 Vorsteuer des März


@pytest.mark.parametrize('with_accounts', [False, True])
def test_the_liquidity_has_nothing_unassigned_in_any_month_and_follows_its_sources(db, with_accounts):
    books_of_unusual_rates(db)
    if with_accounts:
        master_data_of_the_unusual_rates(db)
    for month in (2, 3, 4, 5):
        for variant in ('jahr', 'vorjahr'):
            report = db.get_liquidity(2026, month, variant)
            assert liq.unassigned_of(report) == dict.fromkeys(report.periods, 0), (month, variant)
            liq.check_against_sources(db, report, (with_accounts, month, variant))
    assert liq.values_of(db.get_liquidity(2026, 3, 'jahr'), 'result') == -minor('110.00')      # Ausgaben 215,50, Einnahme 105,50


def test_the_dashboard_counts_the_money_that_moved_and_the_euer_box_shows_the_virtual_line_escaped(db):
    books_of_unusual_rates(db)
    box = str(PageDashboard(db, FIRST, LAST))
    box = box[box.index('<h3>Einnahmen&uuml;berschussrechnung'):]
    assert '<td>1409</td><td>Vorsteuer ohne Steuerkonto</td>' in box
    totals, _monthly = db.get_dashboard(FIRST, LAST)
    statement = euer_statement(euer(db), bank_skr_numbers(db.fetch_accounts()))
    assert statement.result == to_minor(totals['balance']) == -minor('110.00')


def test_the_pages_show_the_account_name_of_the_master_data_escaped(db):
    hostile = '<b>Vorsteuer</b> & "x"'
    rate_entry(db, 550, input_tax=new_account(db, 1430, hostile))
    helpers.mask(db, 'Probe-Giro', 6815, '-105.50', '2026-03-10', '5.5', '-5.50')
    page = str(PageEuer(db, EuerQuery('2026-01-01', '2026-12-31'), datetime.date(2026, 11, 21)))
    assert '<b>Vorsteuer</b>' not in page and html_lib.escape(hostile, quote=True) in page
    box = str(PageDashboard(db, FIRST, LAST))
    assert '<b>Vorsteuer</b>' not in box and html_lib.escape(hostile, quote=True) in box
    assert [line for line in table_rows(page) if line.startswith('1430 | ')] == [f'1430 | {hostile} | -5,50']


def test_the_pages_show_the_virtual_line_of_the_euer_and_the_bwa(db):
    helpers.mask(db, 'Probe-Giro', 6815, '-105.50', '2026-03-10', '5.5', '-5.50')
    euer_page = str(PageEuer(db, EuerQuery('2026-01-01', '2026-12-31'), datetime.date(2026, 11, 21)))
    assert [line for line in table_rows(euer_page) if line.startswith('1409 | ')] == ['1409 | Vorsteuer ohne Steuerkonto | -5,50']
    bwa_page = str(PageBwa(db, BwaQuery(2026, 3, 'jahr')))
    assert any('Vorsteuer ohne Steuerkonto' in line for line in table_text(bwa_page))


def test_a_book_without_another_rate_shows_no_line_1409_anywhere(tmp_db):
    some_books(tmp_db)
    assert 1409 not in numbers_of(tmp_db.get_euer_data('2026-01-01', '2026-12-31'))
    euer_page = str(PageEuer(tmp_db, EuerQuery('2026-01-01', '2026-12-31'), datetime.date(2026, 11, 21)))
    assert not [line for line in table_rows(euer_page) if line.startswith('1409 | ')] and 'Vorsteuer ohne Steuerkonto' not in euer_page
    assert '<td>1409</td>' not in str(PageDashboard(tmp_db, '2026-01-01', '2026-12-31'))
    assert not any('Vorsteuer ohne Steuerkonto' in line for line in table_text(str(PageBwa(tmp_db, BwaQuery(2026, 3, 'jahr')))))


# ═════════════════════════════════════════════════════════════════════════════
# Bestände aus Zufall
# ═════════════════════════════════════════════════════════════════════════════

RANDOM_RATES = ['19', '7', '5.5', '10.7', '10', '16', '5', '5.5', '10.7']
SETUPS = {'keine Konten': (), 'beide Konten': ((550, True, True), (1070, True, True), (1000, True, True)),
          'nur Vorsteuerkonten': ((550, False, True), (1070, False, True), (1000, False, True)),
          'nur Umsatzsteuerkonten': ((550, True, False), (1070, True, False), (1000, True, False))}
SAMPLE_MONTHS = [(2025, 3), (2025, 9), (2025, 12), (2026, 1), (2026, 6), (2026, 12)]


def random_books(db, seed, size=45):
    """Ein Bestand aus Zufall (fester Seed) über 2025 und 2026, nur über die Buchungsmaske: Einnahmen und Ausgaben zu 19, 7, 16, 5, 5,5, 10 und 10,7 %."""
    rng = random.Random(seed)
    for _ in range(size):
        date = (datetime.date(2025, 1, 1) + datetime.timedelta(days=rng.randrange(0, 730))).isoformat()
        rate = rng.choice(RANDOM_RATES)
        net = D(rng.randint(500, 450_000)) / 100
        gross = (net * (100 + D(rate)) / 100).quantize(D('0.01'), rounding=ROUND_HALF_UP)
        tax = gross - net
        bank = rng.choice(['Probe-Giro', 'Probe-Giro', 'Probe-Zweit'])
        if rng.random() < 0.4:
            helpers.mask(db, bank, rng.choice([4400, 4300]), f'{gross}', date, rate, f'{tax}')
        else:
            helpers.mask(db, bank, rng.choice([6815, 6850, 5505, 5170]), f'-{gross}', date, rate, f'-{tax}')


def apply_setup(db, setup):
    """Die Einträge der Steuersätze auf den Stand ``setup`` bringen (die Vorbelegung 19 und 7 % bleibt): ``(Satz in Hundertstel Prozent, Umsatzsteuerkonto, Vorsteuerkonto)``."""
    helpers.raw(db, 'DELETE FROM TaxRates WHERE ID > 2')
    for number in (1430, 1431, 1432, 3845, 3846, 3847):
        helpers.raw(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = ?', number)
    for offset, (bp, vat, input_tax) in enumerate(setup):
        rate_entry(db, bp, vat=new_account(db, 3845 + offset) if vat else None, input_tax=new_account(db, 1430 + offset) if input_tax else None)


@pytest.mark.parametrize('seed', [2808, 2809])
def test_in_a_random_book_every_probe_holds_with_and_without_accounts_and_the_result_never_moves(db, seed):
    random_books(db, seed)
    banks = bank_skr_numbers(db.fetch_accounts())
    results = {}
    for name, setup in SETUPS.items():
        apply_setup(db, setup)
        deviations = []
        for year, month in helpers.every_month(2025) + helpers.every_month(2026):
            report = db.get_susa(year, month)
            helpers.assert_balanced(report, (seed, name, year, month))
            deviations += helpers.check_bank_accounts(db, report, (seed, name, year, month))
            deviations += helpers.check_erfolgskonten_against_euer(db, report, (seed, name, year, month))
        assert deviations == [], name
        for year, month in SAMPLE_MONTHS:
            check_invariants(db, db.get_bwa(year, month, 'vorjahr'))
            for variant in ('jahr', 'vorjahr'):
                report = db.get_liquidity(year, month, variant)
                assert liq.unassigned_of(report) == dict.fromkeys(report.periods, 0), (seed, name, year, month, variant)
        results[name] = [(span, euer_statement(db.get_euer_data(*span), banks).result) for span in (('2025-01-01', '2025-12-31'), ('2026-01-01', '2026-12-31'))]
    assert len({str(value) for value in results.values()}) == 1 and results['keine Konten'][0][1] != 0       # Konten verschieben die Steuer, nie das Ergebnis
