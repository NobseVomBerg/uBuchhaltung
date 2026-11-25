# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Zahlung mit Skonto bei mehreren Steuersätzen in den Auswertungen (Stufe 2): EÜR, Summen- und Saldenliste, DATEV-Stapel, Liquidität, Dashboard, Split-Summe und Prüfliste – **ohne
Änderung an den Auswertungen selbst**. Ihre Abstimmungsproben gehen für so eine Rechnung auf den Minor-Wert genauso auf wie für jede andere.

Die feste Rechnung ist **M** (``M-100``, 03.03.2025): 19 % × 100,00 und 7 % × 50,00 (brutto 119,00 und 53,50, zusammen 172,50), Skonto 2 % = 3,45 €, bezahlt mit 169,05 € am 10.04.2025:

* **EÜR:** Erlöse 4400 100,00 und 4300 50,00, die Erlösschmälerungen 4736 −2,00 und 4731 −1,00, die Umsatzsteuer 3806 nur 22,05 (die Steuer der Skonti, 0,38 und 0,07, mindert sie).
* **Summen- und Saldenliste:** Soll = Haben, die Konten der Forderungen (10000, 4405, 4345) laufen auf 0, 1800 trägt das Geld (169,05), 4736 und 4731 stehen im Soll mit 2,00 und 1,00.
* **DATEV:** „1800 an 10000“ über 172,50 und je Satz „4736 an 1800“ (Schlüssel 101, Spalte 40 mit 19,00) und „4731 an 1800“ (102, 7,00); steuerfrei „4730 an 1800“ mit Schlüssel 0 ohne Satz.
* **Additivität:** dieselben Salden, dieselbe EÜR und dieselben DATEV-Zeilen (bis auf „Bank an Debitoren“) wie zwei Rechnungen mit einem Satz (119,00 mit 2,38 und 53,50 mit 1,07).
* **Dashboard, Liquidität, Split-Summe, Prüfliste:** das Geld als Einnahme, „Nicht zugeordnet“ 0, kein freier Rest, keine Befunde – auch nach dem Stornieren und Erstatten.
* **Prüfliste:** ein Skonto-Satz, den jemand je Satz verschoben hat (die Summe bleibt), nennt ``rate_split_mismatch``.
* **Zufallsbestände** (fester Seed, nur Wege der App): Die Proben gehen in jedem Monat auf, jede bezahlte Rechnung trägt je Satz genau ``skonto_moved_by_rate``.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da.
"""
import datetime
import random
from decimal import Decimal

import pytest

from db import Database
from db.invoice_payments import skonto_limit, skonto_moved_by_rate
from tax_groups import item_groups
from tests import liquidity_helpers as liq
from tests import susa_helpers as helpers
from tests.test_skonto_mixed_booking import FREE, NUMBER, PLAIN_19, PLAIN_7, REDUCED, account, entry, free, mixed_invoice
from tests.test_skonto_reports import RANGE, check_every_probe, datev_rows, ending, findings, invoice_of, minor, pay, raw
from tests.test_skonto_reports import db  # noqa: F401  (Fixture: Testdatenbank mit zwei Bankkonten)
from tests.test_skonto_storno import cancel, refund

D = Decimal
MONTHS = (3, 4, 5, 6, 7)
CHAIN = (1800, 10000, 4405, 4345, 4400, 4300, 4736, 4731, 3806, 3801)


# ── Welt und Hilfen ──────────────────────────────────────────────────────────

def world(db, receivable=True, parts=None, paid='169.05', day='2025-04-10'):
    """M mit dem Skonto als Rest der Zahlung – ``(Rechnungs-ID, Bankbewegung)``."""
    invoice_id = mixed_invoice(db, parts=parts, receivable=receivable)
    return invoice_id, pay(db, invoice_id, paid, day, skonto=True)


def balances(report, numbers=CHAIN):
    return [ending(report, number) for number in numbers]


def closings(report):
    """Der Endsaldo jedes Kontos der Liste: ``{Kontonummer: Minor Units}``."""
    return {key: account.closing for key, account in helpers.accounts_of(report).items() if isinstance(key, int)}


def second_db(tmp_path):
    """Eine zweite, eigene Datenbank für eine Vergleichswelt – mit denselben zwei Bankkonten."""
    other = Database(db_name=str(tmp_path / 'vergleich.db'))
    liq.two_banks(other)
    return other


def single_rate_world(db, partial=False):
    """Dieselben Zahlungen als zwei Rechnungen mit je einem Satz (19 % × 100,00 und 7 % × 50,00, je Skonto 2 %): eine Bankbewegung, zwei Zuordnungen – mit den Anteilen je Satz, die die
    Rechnung M mit Steuersatz je Position verteilt (Geld 116,62 und 52,43, Skonto 2,38 und 1,07)."""
    r19 = invoice_of(db, 'E-19', 119.0, 0.19)
    r7 = invoice_of(db, 'E-07', 53.5, 0.07)
    plan = ([('68.99', '31.01', '2025-04-02', False), ('47.63', '21.42', '2025-05-05', True)] if partial else [('116.62', '52.43', '2025-04-10', True)])
    for first, second, day, skonto in plan:
        bank = db.insert_booking(day, D(first) + D(second), account_id=helpers.account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
        for invoice_id, amount in ((r19, first), (r7, second)):
            assert db.assign_invoice_payment(invoice_id, bank, amount, None, skonto).ok


def settlement(row):
    """„Bank an Debitoren“: Konto 1800 und Gegenkonto 10000 (in einer der beiden Schreibweisen)."""
    return {row[2], row[3]} == {'1800', '10000'}


def per_rate_moved(db, number):
    """Was die markierten Umbuchungen und Einzelsätze der Zuordnungen einer Beleg-Nr. je Satz tragen – aus den Zeilen gelesen, unabhängig vom Code unter Test: ``{Satz in Prozent: (Betrag, Steuer)}``
    in Minor Units (0 % ohne Satz)."""
    waiting = [row[0] for row in raw(db, 'SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN (4405, 4345, 4340)')]
    marks = ','.join('?' * len(waiting))
    result = {}
    for rate, amount, tax in raw(
            db, "SELECT TaxRate, SUM(Amount), SUM(COALESCE(TaxAmount, 0)) FROM Bookings WHERE DocumentNumber = ? AND Status = 'invoice_payment' "
                f"AND ((EntryForm = 'transfer' AND COA_ID IN ({marks})) OR EntryForm = 'purpose_first') GROUP BY TaxRate", number, *waiting):
        result[D(0) if rate is None else D(str(round(rate * 100, 2))).normalize()] = (amount, tax)
    return result


# ═════════════════════════════════════════════════════════════════════════════
# EÜR
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_euer_shows_the_revenue_the_discounts_as_reductions_and_the_tax_less_the_tax_of_the_discounts(db, receivable):
    world(db, receivable=receivable)

    expected = {4400: minor('100.00'), 4300: minor('50.00'), 4736: minor('-2.00'), 4731: minor('-1.00'), 3806: minor('22.05')}
    assert helpers.euer_values(db, ('2025-04-01', '2025-04-30')) == expected
    assert helpers.euer_values(db, RANGE) == expected
    assert helpers.euer_values(db, ('2025-03-01', '2025-03-31')) == {}                                  # März: nur die Forderungen – sie sind kein Erlös


def test_the_result_is_the_money_less_the_tax_the_customer_paid(db):
    """Ergebnis = Erlöse − Skonto-Netto = 150,00 − 3,00 = 147,00; die Umsatzsteuer 22,05 ist das, was zum gezahlten Geld (169,05) gehört."""
    world(db)

    rows = {number: D(str(total)) for number, _name, total in db.get_euer_data(*RANGE)}

    assert rows[4400] + rows[4300] + rows[4736] + rows[4731] == D('147.00') and sum(rows.values()) == D('169.05')


def test_the_euer_of_a_payment_of_169_06_euro(db):
    """169,06 € gezahlt: Das Skonto ist 3,44 € (7 %: 1,07 – 1,0669 gerundet, 19 %: der Rest 2,37), die Steuer darin 0,38 und 0,07: Die EÜR trägt 4736 mit −1,99 und 3806 mit 22,05."""
    invoice_id = mixed_invoice(db)
    bank = db.insert_booking('2025-04-10', D('169.06'), account_id=helpers.account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    assert db.assign_invoice_payment(invoice_id, bank, None, None, True).ok

    assert helpers.euer_values(db, RANGE) == {4400: minor('100.00'), 4300: minor('50.00'), 4736: minor('-1.99'), 4731: minor('-1.00'), 3806: minor('22.05')}
    check_every_probe(db, (4, 5))


# ═════════════════════════════════════════════════════════════════════════════
# Summen- und Saldenliste
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_susa_carries_the_money_and_closes_the_chain_of_the_receivables(db, receivable):
    world(db, receivable=receivable)

    report = db.get_susa(2025, 4)

    helpers.assert_balanced(report)
    assert balances(report) == [minor('169.05'), 0, 0, 0, minor('-100.00'), minor('-50.00'), minor('2.00'), minor('1.00'), minor('-18.62'), minor('-3.43')]
    assert [notice.key for notice in report.notices] == []                                              # kein Hinweis: jeder Satz ist buchbar


@pytest.mark.parametrize('month', MONTHS)
@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_every_probe_holds_in_every_month(db, month, receivable):
    world(db, receivable=receivable)

    check_every_probe(db, (month,))


def test_before_the_payment_the_receivables_stand_and_after_it_every_account_of_the_chain_is_zero(db):
    invoice_id = mixed_invoice(db)
    assert balances(db.get_susa(2025, 3), (10000, 4405, 4345)) == [minor('172.50'), minor('-119.00'), minor('-53.50')]
    pay(db, invoice_id, '169.05', '2025-04-10', skonto=True)

    assert [ending(db.get_susa(2025, month), number) for month in (4, 5, 6) for number in (10000, 4405, 4345)] == [0] * 9


def test_a_part_payment_then_the_closing_one_with_a_discount(db):
    invoice_id = mixed_invoice(db)
    pay(db, invoice_id, '100.00', '2025-04-02')
    april = db.get_susa(2025, 4)
    assert balances(april, (1800, 10000, 4405, 4345)) == [minor('100.00'), minor('72.50'), minor('-50.01'), minor('-22.49')]
    pay(db, invoice_id, '69.05', '2025-05-05', skonto=True)

    may = db.get_susa(2025, 5)

    assert balances(may) == [minor('169.05'), 0, 0, 0, minor('-100.00'), minor('-50.00'), minor('2.00'), minor('1.00'), minor('-18.62'), minor('-3.43')]
    assert helpers.euer_values(db, RANGE) == {4400: minor('100.00'), 4300: minor('50.00'), 4736: minor('-2.00'), 4731: minor('-1.00'), 3806: minor('22.05')}
    check_every_probe(db)
    assert db.link_bank_to_entries().get('resolved') == 2                                              # Stufe 7: 100,00 + 172,50 „an 10000“ decken beide Forderungen
    check_every_probe(db)


@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_three_groups_with_a_tax_free_one_close_the_chain_and_every_probe_holds(db, receivable):
    world(db, receivable=receivable, parts=[PLAIN_19, PLAIN_7, free('100.00', account(db, FREE))], paid='267.05')

    assert helpers.euer_values(db, RANGE) == {4400: minor('100.00'), 4300: minor('50.00'), FREE: minor('100.00'), 4736: minor('-2.00'), 4731: minor('-1.00'), 4730: minor('-2.00'),
                                              3806: minor('22.05')}
    assert ending(db.get_susa(2025, 4), 4730) == minor('2.00') and ending(db.get_susa(2025, 4), 10000) == 0
    check_every_probe(db)


# ═════════════════════════════════════════════════════════════════════════════
# DATEV
# ═════════════════════════════════════════════════════════════════════════════

def test_the_datev_rows_of_a_payment_with_discounts_and_receivables(db):
    world(db)

    assert datev_rows(db, NUMBER) == [
        ('119,00', 'S', '10000', '4405', '0', '19,00', '03032025'),                                    # die Forderungen: Leistungsdatum in Spalte 114
        ('53,50', 'S', '10000', '4345', '0', '7,00', '03032025'),
        ('172,50', 'S', '1800', '10000', '0', '', ''),                                                 # „Bank an Debitoren“ über Geld plus Skonto
        ('119,00', 'S', '4405', '4400', '0', '19,00', ''), ('53,50', 'S', '4345', '4300', '0', '7,00', ''),
        ('2,38', 'S', '4736', '1800', '101', '19,00', ''),                                             # je Satz der Skonto-Satz: Umsatzsteuer-Art, Satz in Spalte 40
        ('1,07', 'S', '4731', '1800', '102', '7,00', '')]


def test_the_datev_rows_of_a_payment_with_discounts_without_receivables(db):
    world(db, receivable=False)

    assert datev_rows(db, NUMBER) == [('119,00', 'H', '4400', '1800', '101', '19,00', ''), ('53,50', 'H', '4300', '1800', '102', '7,00', ''),
                                      ('2,38', 'S', '4736', '1800', '101', '19,00', ''), ('1,07', 'S', '4731', '1800', '102', '7,00', '')]


def test_the_datev_row_of_a_tax_free_group_has_no_key_and_no_rate(db):
    world(db, receivable=False, parts=[PLAIN_19, free('100.00', account(db, FREE))], paid='214.62')

    assert datev_rows(db, NUMBER)[-1] == ('2,00', 'S', '4730', '1800', '0', '', '')
    assert datev_rows(db, NUMBER)[:2] == [('119,00', 'H', '4400', '1800', '101', '19,00', ''), ('100,00', 'H', str(FREE), '1800', '0', '', '')]


def test_the_money_of_the_batch_is_the_money_of_the_statement(db):
    """Die Bankseite der Zeilen („1800 an 10000“ 172,50 und die Skonto-Sätze 2,38 und 1,07) ergibt zusammen den Auszug: 169,05."""
    world(db)

    bank = D(0)
    for amount, side, account_no, counter, _key, _rate, _date in datev_rows(db, NUMBER):
        if '1800' not in (account_no, counter):
            continue
        bank_side = side if account_no == '1800' else ('H' if side == 'S' else 'S')                      # auf welcher Seite steht 1800?
        bank += D(amount.replace(',', '.')) * (1 if bank_side == 'S' else -1)
    assert bank == D('169.05')


# ═════════════════════════════════════════════════════════════════════════════
# Additivität: dieselben Bücher wie zwei Rechnungen mit einem Satz
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('partial', [False, True], ids=['eine-Zahlung', 'Teilzahlung-und-Skonto'])
def test_the_books_of_the_mixed_invoice_equal_those_of_two_invoices_with_one_rate(db, tmp_path, partial):
    """Je Konto und Monat dieselben Salden, dieselbe EÜR und – bis auf „Bank an Debitoren“, das der Beleg mit einem Satz je Rechnung bucht – dieselben DATEV-Zeilen (Konten, S/H, Umsatz, BU-Schlüssel,
    Spalten 40 und 114) wie zwei Rechnungen mit einem Satz (119,00 mit 2,38 und 53,50 mit 1,07 Skonto)."""
    other = second_db(tmp_path)
    invoice_id = mixed_invoice(db)
    if partial:
        pay(db, invoice_id, '100.00', '2025-04-02')
        pay(db, invoice_id, '69.05', '2025-05-05', skonto=True)
    else:
        pay(db, invoice_id, '169.05', '2025-04-10', skonto=True)
    single_rate_world(other, partial)

    for month in MONTHS:
        assert closings(db.get_susa(2025, month)) == closings(other.get_susa(2025, month)), month
    assert helpers.euer_values(db, RANGE) == helpers.euer_values(other, RANGE)
    mixed_rows = [row for row in datev_rows(db, NUMBER) if not settlement(row)]
    single_rows = [row for row in datev_rows(other, 'E-19') + datev_rows(other, 'E-07') if not settlement(row)]
    assert sorted(mixed_rows) == sorted(single_rows)
    check_every_probe(other, (4, 5, 6))


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard, Liquidität, Split-Summe, Prüfliste
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_dashboard_counts_the_money_received_as_income(db, receivable):
    world(db, receivable=receivable)

    totals, _monthly = db.get_dashboard('2025-04-01', '2025-04-30')

    assert (round(totals['income'], 2), totals['expense'], totals['private'], totals['transfers'], round(totals['balance'], 2), totals['unlinked_count']) == (
        169.05, 0, 0, 0, 169.05, 0)


def test_the_bank_movement_has_no_free_rest_and_the_rows_below_it_add_up_to_it(db):
    _invoice_id, bank = world(db)

    assert db.get_booking_unallocated_amount(bank) == 0
    assert db.find_unbalanced_splits(*RANGE) == []


def test_the_payment_check_finds_nothing_after_every_step(db):
    invoice_id = mixed_invoice(db)
    assert db.find_payment_inconsistencies() == []
    bank = pay(db, invoice_id, '169.05', '2025-04-10', skonto=True)
    assert db.find_payment_inconsistencies() == []
    db.link_bank_to_entries()
    assert db.find_payment_inconsistencies() == []
    storno = cancel(db, invoice_id, '2025-06-03')
    assert db.find_payment_inconsistencies() == []
    refund(db, storno.storno_id, '100.00', day='2025-07-01')
    assert db.find_payment_inconsistencies() == []
    refund(db, storno.storno_id, '69.05', day='2025-07-02')
    assert db.find_payment_inconsistencies() == [] and db.find_unbalanced_splits(*RANGE) == []
    assert db.get_booking_unallocated_amount(bank) == 0


def test_the_payment_check_finds_nothing_after_removing_the_payment(db):
    invoice_id = mixed_invoice(db)
    bank = pay(db, invoice_id, '169.05', '2025-04-10', skonto=True)

    db.unassign_invoice_payment(db.get_invoice_payments(invoice_id)[0][0])

    assert db.find_payment_inconsistencies() == [] and db.get_booking_allocations(bank) == []


def test_the_liquidity_has_nothing_unassigned_after_the_cancellation_and_the_refund_either(db):
    invoice_id, _bank = world(db)
    storno = cancel(db, invoice_id, '2025-06-03')
    refund(db, storno.storno_id, '169.05', day='2025-07-01')

    check_every_probe(db, (4, 5, 6, 7, 8))


# ═════════════════════════════════════════════════════════════════════════════
# Prüfliste: ein Skonto-Satz je Satz geändert
# ═════════════════════════════════════════════════════════════════════════════

def categories(db, invoice_id=None):
    return [category for category, _text in findings(db, invoice_id)]


def test_an_untouched_assignment_has_no_finding(db):
    invoice_id, _bank = world(db)
    assert findings(db, invoice_id) == [] and findings(db) == []


def test_a_discount_row_moved_between_the_rates_keeps_the_sum_and_is_found_per_rate(db):
    """19 % trägt 2,37 und 7 % 1,08 statt 2,38 und 1,07: Die Summe (3,45) stimmt – ``skonto_mismatch`` und ``transfer_mismatch`` schweigen, ``rate_split_mismatch`` nennt, was je Satz erwartet
    ist und was umgebucht ist."""
    invoice_id, bank = world(db)
    raw(db, 'UPDATE Bookings SET Amount = -23700 WHERE ParentBooking_ID = ? AND COA_ID = ?', bank, db.get_coa_id_by_account_number(4736))
    raw(db, 'UPDATE Bookings SET Amount = -10800 WHERE ParentBooking_ID = ? AND COA_ID = ?', bank, db.get_coa_id_by_account_number(4731))

    assert findings(db, invoice_id) == [('rate_split_mismatch', 'Die Rechnung ist ganz bezahlt, aber auf die Erlöskonten ist je Steuersatz nicht ihr Betrag umgebucht: '
                                                                  'erwartet 19 %: 116,62 € (Steuer 18,62 €); 7 %: 52,43 € (Steuer 3,43 €), '
                                                                  'umgebucht 19 %: 116,63 € (Steuer 18,62 €); 7 %: 52,42 € (Steuer 3,43 €).')]


def test_a_discount_row_with_the_tax_changed_is_found_per_rate(db):
    invoice_id, bank = world(db)
    raw(db, 'UPDATE Bookings SET TaxAmount = -4000 WHERE ParentBooking_ID = ? AND COA_ID = ?', bank, db.get_coa_id_by_account_number(4736))

    assert categories(db, invoice_id) == ['rate_split_mismatch']


def test_a_deleted_discount_row_is_found_by_the_sum_and_by_the_rates(db):
    invoice_id, bank = world(db)
    raw(db, 'DELETE FROM Bookings WHERE ParentBooking_ID = ? AND COA_ID = ?', bank, db.get_coa_id_by_account_number(4731))

    found = findings(db, invoice_id)
    assert [category for category, _text in found] == ['skonto_mismatch', 'rate_split_mismatch']
    assert found[0][1] == 'Zuordnung #1 trägt Skonto 3,45 €, gebucht sind 2,38 €.'


def test_a_discount_that_cannot_be_split_over_the_rates_is_named_with_a_fixed_sentence(db):
    """Ein Satz ohne Skonto-Konto (5,5 %) im Beleg, ganz bezahlt mit Skonto (von Hand gesetzt): Die Verteilung lässt sich nicht nachrechnen – der Befund sagt es, ohne eine Zahl zu raten."""
    invoice_id = mixed_invoice(db, parts=[PLAIN_19, entry('5.5', '200.00', account(db, REDUCED))], receivable=False)
    bank = db.insert_booking('2025-04-10', D('323.40'), account_id=helpers.account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    assert db.assign_invoice_payment(invoice_id, bank).ok
    raw(db, 'UPDATE InvoicePayments SET SkontoAmount = 66000')                                                  # von Hand: 6,60 € Skonto – das Brutto ist 330,00 €
    raw(db, "UPDATE Invoices SET Status = 'paid', AmountDue = 0")

    assert ('rate_split_mismatch', 'Die Rechnung ist mit Skonto bezahlt, das sich nicht auf ihre Steuersätze aufteilen lässt.') in findings(db, invoice_id)


def test_the_check_after_the_cancellation_and_a_refund_has_no_refund_finding(db):
    """Nach dem Stornieren und Erstatten (je Satz Betrag und Steuer des Geldes) schweigt ``rate_refund_mismatch``; mehr erstattet als gezahlt je Satz nennt er."""
    invoice_id, _bank = world(db)
    storno = cancel(db, invoice_id, '2025-06-03')
    out = refund(db, storno.storno_id, '169.05', day='2025-07-01')

    assert findings(db) == []
    raw(db, 'UPDATE Bookings SET Amount = -1166300 WHERE ParentBooking_ID = ? AND EntryForm = ? AND TaxRate = 0.19', out, 'transfer')           # 116,63 statt 116,62 …
    raw(db, 'UPDATE Bookings SET Amount = -524200 WHERE ParentBooking_ID = ? AND EntryForm = ? AND TaxRate = 0.07', out, 'transfer')            # … und 52,42 statt 52,43: Die Summe bleibt, 19 % ist mehr erstattet als gezahlt
    assert 'rate_refund_mismatch' in categories(db)


# ═════════════════════════════════════════════════════════════════════════════
# Zufallsbestände: nur Wege der App, feste Seeds
# ═════════════════════════════════════════════════════════════════════════════

def discount_stock(db, seed, invoices=6):
    """``invoices`` Rechnungen mit 19 und 7 % und, mal, einer steuerfreien Gruppe (mit und ohne Forderung), 0 bis 2 Teilzahlungen und einer schließenden Zahlung – mit Skonto (der Rest liegt
    zwischen einem Cent und dem Skonto der Rechnung, wo die Vorprüfung es zulässt) oder ohne –, alles über die Wege der App. Liefert ``[(Rechnungs-ID, Nummer, Brutto, Skonto)]`` (Minor Units;
    Skonto 0, wenn die Rechnung ohne Skonto bezahlt ist)."""
    rng = random.Random(seed)
    free_account = account(db, FREE)
    stock = []
    for number in range(invoices):
        parts = [(19, f'{rng.randint(1_000, 150_000) / 100:.2f}', None), (7, f'{rng.randint(1_000, 150_000) / 100:.2f}', None)]
        if rng.random() < 0.4:
            parts.append(free(f'{rng.randint(1_000, 150_000) / 100:.2f}', free_account))
        percent = rng.choice([1.0, 2.0, 2.5, 3.0])
        day = datetime.date(2025, rng.choice([3, 4]), rng.randint(1, 28))
        name = f'Z{seed}-{number}'
        invoice_id = mixed_invoice(db, name, parts=parts, day=day.isoformat(), skonto=(rng.choice([7, 10, 14]), percent), receivable=rng.random() < 0.7)
        gross = raw(db, 'SELECT SumGross FROM Invoices WHERE ID = ?', invoice_id)[0][0]
        gross_cents = gross // 100
        left, when = gross_cents, day
        for _ in range(rng.randint(0, 2)):                                                             # Teilzahlungen, jede höchstens 60 % des Rests
            part = rng.randint(1, max(1, left * 6 // 10))
            when += datetime.timedelta(days=rng.randint(5, 20))
            pay(db, invoice_id, f'{part / 100:.2f}', when.isoformat(), account=rng.choice(['Probe-Giro', 'Probe-Zweit']))
            left -= part
        limit = skonto_limit(gross, D(str(percent))) // 100                                           # Cent
        discount = rng.randint(1, min(limit, left - 1)) if limit >= 1 and left > 1 and rng.random() < 0.75 else 0
        if discount:
            minor_amount = (left - discount) * 100
            error, _state = db._skonto_precheck(invoice_id, minor_amount, minor_amount)                # nur, wo die Zuordnung es zulässt (die Aufteilung auf die Sätze geht auf)
            if error:
                discount = 0
        when += datetime.timedelta(days=rng.randint(5, 20))
        pay(db, invoice_id, f'{(left - discount) / 100:.2f}', when.isoformat(), skonto=bool(discount), account=rng.choice(['Probe-Giro', 'Probe-Zweit']))
        stock.append((invoice_id, name, gross, discount * 100))
    return stock


@pytest.mark.parametrize('seed', [31, 32, 33, 34])
def test_a_random_stock_with_discounts_balances_in_every_month_and_the_probes_hold(db, seed):
    discount_stock(db, seed)

    check_every_probe(db, (3, 4, 5, 6, 7, 8, 9), seed)
    db.link_bank_to_entries()
    check_every_probe(db, (5, 9), seed)                                                                  # nach Stufe 7 des Abgleichs ebenso


@pytest.mark.parametrize('seed', [31, 32, 33, 34])
def test_every_invoice_of_the_random_stock_is_paid_and_carries_exactly_the_discount_per_rate(db, seed):
    stock = discount_stock(db, seed)
    with_discount = 0

    for invoice_id, name, gross, discount in stock:
        assert raw(db, 'SELECT Status, AmountDue FROM Invoices WHERE ID = ?', invoice_id) == [('paid', 0)], name
        money = raw(db, 'SELECT SUM(Amount), SUM(COALESCE(SkontoAmount, 0)) FROM InvoicePayments WHERE InvoiceID = ?', invoice_id)[0]
        assert money == (gross - discount, discount), (seed, name)                                       # das Geld plus das Skonto ist die Rechnung
        items = raw(db, 'SELECT TaxRate, TotalNet, TaxRateBp FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', invoice_id)
        groups = item_groups([(rate, net, bp) for rate, net, bp in items])
        assert per_rate_moved(db, name) == skonto_moved_by_rate(groups, discount), (seed, name)
        if discount:
            with_discount += 1
    assert with_discount >= 1                                                                            # mit diesen Seeds gibt es mindestens eine Rechnung mit Skonto
    assert findings(db) == []


@pytest.mark.parametrize('seed', [31, 32, 33, 34])
def test_a_random_stock_cancelled_and_refunded_keeps_every_probe(db, seed):
    """Die Hälfte der Rechnungen wird storniert (nach der letzten Zahlung) und – die eine ganz, die andere in zwei Teilen – erstattet: Die Proben gehen in jedem Monat auf, die Prüfliste ist leer,
    ``rate_refund_mismatch`` eingeschlossen."""
    stock = discount_stock(db, seed)
    rng = random.Random(seed + 1000)
    for invoice_id, _name, _gross, _discount in stock[::2]:
        storno = cancel(db, invoice_id, '2025-09-15')
        credit = db.get_storno_credit(storno.storno_id)
        if credit and rng.random() < 0.5:
            refund(db, storno.storno_id, f'{credit}', day='2025-10-01')
        elif credit:
            first = (D(credit) / 3).quantize(D('0.01'))
            refund(db, storno.storno_id, f'{first}', day='2025-10-01')
            refund(db, storno.storno_id, f'{D(credit) - first}', day='2025-10-02')

    check_every_probe(db, (3, 4, 5, 6, 7, 8, 9, 10, 11), seed)
    assert findings(db) == []
