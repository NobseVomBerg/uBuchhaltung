# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Zahlung mit Skonto in den Auswertungen (2.9.0): EÜR, Summen- und Saldenliste, DATEV-Stapel, Liquidität, Dashboard, Split-Summe und Prüfliste – **ohne Änderung an
den Auswertungen selbst**. Ihre Abstimmungsproben gehen für eine Rechnung, die mit Skonto bezahlt ist, auf den Minor-Wert genauso auf wie für jede andere.

Die feste Rechnung ist **S** (``SK-100``, 03.03.2025, 119,00 € mit 19 % = 19,00 €, Skonto 2 % = 2,38 €), bezahlt mit 116,62 € am 10.04.2025 und dem Skonto als Rest:

* **EÜR:** Erlös 4400 100,00, die Erlösschmälerung 4736 −2,00, die Umsatzsteuer 3806 nur 18,62 (die Steuer des Skontos, 0,38, mindert sie).
* **Summen- und Saldenliste:** Soll = Haben, die Konten der Forderung (10000, 4405) laufen auf 0, 1800 trägt das Geld (116,62), 4736 steht im Soll mit 2,00.
* **DATEV:** „1800 an 10000“ über 119,00 und „4736 an 1800“ über 2,38 (Schlüssel 101, Spalte 40 mit 19,00) – ohne Forderung „4400 an 1800“ 119,00 und derselbe Skonto-Satz.
* **Dashboard, Liquidität, Split-Summe, Prüfliste:** das Geld (116,62) als Einnahme, „Nicht zugeordnet“ 0, kein unstimmiger Split, keine Befunde.

Dazu 7 %, 0 %, § 19, eine Teilzahlung vor der schließenden mit Skonto, eine Sammelzahlung und Zufallsbestände (fester Seed, nur Wege der App): Die Proben gehen in
jedem Monat auf. Die Prüfliste nennt ein Skonto, dessen Sätze nicht dazu passen (``skonto_mismatch``); der Neuaufbau einer Zuordnung rechnet den offenen Betrag mit Geld
plus Skonto.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da.
"""
import csv
import datetime
import random
import sqlite3
from contextlib import closing
from decimal import Decimal

import pytest

from db.invoice_payments import skonto_limit, skonto_tax
from export import datev
from money import to_minor
from server.handlers import link_booking_to_invoice_capped
from tests import liquidity_helpers as liq
from tests import susa_helpers as helpers
from tests.susa_helpers import account_id

D = Decimal
MONTHS = (3, 4, 5, 6, 7)
RANGE = ('2025-01-01', '2025-12-31')


# ── Welt und Hilfen ──────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_db):
    liq.two_banks(tmp_db)
    return tmp_db


def invoice_of(db, number, gross, rate=0.19, day='2025-03-03', skonto=(14, 2.0), receivable=True):
    """Eine versendete Rechnung über ``gross`` € (mit ``rate`` 0,19 / 0,07 / 0 oder −1 für § 19) mit Skonto-Bedingungen ``(Tage, Prozent)`` und, auf Wunsch, ihrer Forderung."""
    net = round(gross / (1 + rate), 2) if rate > 0 else gross
    invoice_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': day, 'delivery_date': day, 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH', 'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': rate, 'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross, 'amount_due': gross, 'status': 'sent',
        'skonto_days': skonto[0] if skonto else None, 'skonto_percent': skonto[1] if skonto else None})
    if receivable:
        assert db.ensure_receivable_booking(invoice_id) is not None
    return invoice_id


def pay(db, invoice_id, amount, day, skonto=False, account='Probe-Giro'):
    """Eine Bankbewegung ``amount`` € am Tag ``day`` und ihre Zuordnung an die Rechnung, wie die Oberfläche es tut (``skonto``: Der Rest wird als Skonto ausgebucht)."""
    bank = db.insert_booking(day, D(str(amount)), account_id=account_id(db, account), booking_type='bank', recipient_client='Käufer AG')
    ok, error = link_booking_to_invoice_capped(db, invoice_id, bank, D(str(amount)), None, skonto)
    assert ok, error
    return bank


def world(db, receivable=True, gross=119.0, rate=0.19, paid='116.62', day='2025-04-10'):
    """S mit dem Skonto als Rest der Zahlung."""
    invoice_id = invoice_of(db, 'SK-100', gross, rate, receivable=receivable)
    return invoice_id, pay(db, invoice_id, paid, day, skonto=True)


def minor(euro):
    return helpers.minor(euro)


def ending(report, number):
    account = helpers.accounts_of(report).get(number)
    return 0 if account is None else account.closing


def check_every_probe(db, months=MONTHS, where=''):
    """Die Abstimmungsproben der Summen- und Saldenliste, die Liquidität (beide Darstellungen, „Nicht zugeordnet“ 0) und die Gegenrechnung aus den Quellen – in jedem Monat."""
    for month in months:
        report = db.get_susa(2025, month)
        helpers.assert_balanced(report, (where, month))
        assert helpers.check_bank_accounts(db, report, (where, month)) == []
        assert helpers.check_erfolgskonten_against_euer(db, report, (where, month)) == []
        for variant in ('vorjahr', 'jahr'):
            liquidity = db.get_liquidity(2025, month, variant)
            assert set(liq.unassigned_of(liquidity).values()) == {0}, (where, month, variant, liq.unassigned_of(liquidity))
            liq.check_against_sources(db, liquidity, (where, month, variant))
    assert db.find_unbalanced_splits(*RANGE) == []
    assert db.find_payment_inconsistencies() == []


def datev_rows(db, number):
    """Die Zeilen des Stapels mit dieser Belegfeld-1-Nummer: (Umsatz, S/H, Konto, Gegenkonto, BU-Schlüssel, Spalte 40, Spalte 114), in der Reihenfolge des Stapels."""
    omitted = db.duplicate_transfer_rows()
    rows = [row for row in db.fetch_bookings_range(*RANGE) if row[0] not in omitted]
    consultant_no, client_no = db.get_datev_numbers()
    csv_bytes, _ids = datev.export_to_datev(rows, db.get_coa_id_to_number_map(), *RANGE, liquid_coa_ids=db.get_payment_coa_ids(),
                                            waiting_numbers=db.get_waiting_account_numbers(), invoices=db.get_datev_invoice_data(),
                                            consultant_no=consultant_no, client_no=client_no)
    lines = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]
    fields = [next(csv.reader([line], delimiter=';', quotechar='"')) for line in lines]
    return [(row[0], row[1], row[6], row[7], row[8], row[40], row[114]) for row in fields if row[10] == number]


def raw(db, sql, *params):
    with closing(sqlite3.connect(db.db_name)) as con:
        cursor = con.execute(sql, params)
        con.commit()
        return cursor.fetchall()


# ═════════════════════════════════════════════════════════════════════════════
# EÜR
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_euer_shows_the_revenue_the_discount_as_a_reduction_and_the_tax_less_the_tax_of_the_discount(db, receivable):
    world(db, receivable=receivable)

    assert helpers.euer_values(db, ('2025-04-01', '2025-04-30')) == {4400: minor('100.00'), 4736: minor('-2.00'), 3806: minor('18.62')}
    assert helpers.euer_values(db, RANGE) == {4400: minor('100.00'), 4736: minor('-2.00'), 3806: minor('18.62')}
    assert helpers.euer_values(db, ('2025-03-01', '2025-03-31')) == {}                                  # März: nur die Forderung – sie ist kein Erlös


def test_the_result_is_the_money_less_the_tax_the_customer_paid(db):
    """Ergebnis = Erlös − Skonto-Netto = 100,00 − 2,00 = 98,00; die Umsatzsteuer 18,62 ist das, was zum gezahlten Geld (116,62) gehört."""
    world(db)

    rows = {number: total for number, _name, total in db.get_euer_data(*RANGE)}

    assert D(str(rows[4400])) + D(str(rows[4736])) == D('98.00') and D(str(rows[4400])) + D(str(rows[4736])) + D(str(rows[3806])) == D('116.62')


def test_the_euer_of_a_payment_of_117_euro(db):
    invoice_id = invoice_of(db, 'SK-117', 119.0)
    pay(db, invoice_id, '117.00', '2025-04-10', skonto=True)

    assert helpers.euer_values(db, RANGE) == {4400: minor('100.00'), 4736: minor('-1.68'), 3806: minor('18.68')}             # Skonto 2,00 mit 0,32 Steuer
    check_every_probe(db, (4, 5))


# ═════════════════════════════════════════════════════════════════════════════
# Summen- und Saldenliste
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_susa_carries_the_money_and_closes_the_chain_of_the_receivable(db, receivable):
    world(db, receivable=receivable)

    report = db.get_susa(2025, 4)

    helpers.assert_balanced(report)
    assert [ending(report, number) for number in (1800, 10000, 4405, 4400, 4736, 3806)] == [
        minor('116.62'), 0, 0, minor('-100.00'), minor('2.00'), minor('-18.62')]                         # Soll positiv: 4736 im Soll, Erlös und Steuer im Haben
    assert [notice.key for notice in report.notices] == []                                              # kein Hinweis: jeder Satz ist buchbar


@pytest.mark.parametrize('month', MONTHS)
@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_every_probe_holds_in_every_month(db, month, receivable):
    world(db, receivable=receivable)

    check_every_probe(db, (month,))


def test_before_the_payment_the_receivable_stands_and_after_it_every_account_of_the_chain_is_zero(db):
    invoice_id = invoice_of(db, 'SK-100', 119.0)
    assert [ending(db.get_susa(2025, 3), number) for number in (10000, 4405)] == [minor('119.00'), minor('-119.00')]
    pay(db, invoice_id, '116.62', '2025-04-10', skonto=True)

    assert [ending(db.get_susa(2025, month), number) for month in (4, 5, 6) for number in (10000, 4405)] == [0] * 6


def test_a_part_payment_then_the_closing_one_with_a_discount(db):
    invoice_id = invoice_of(db, 'SK-100', 119.0)
    pay(db, invoice_id, '50.00', '2025-04-02')
    april = db.get_susa(2025, 4)
    assert [ending(april, number) for number in (1800, 10000, 4405)] == [minor('50.00'), minor('69.00'), minor('-69.00')]
    pay(db, invoice_id, '66.62', '2025-05-05', skonto=True)

    may = db.get_susa(2025, 5)

    assert [ending(may, number) for number in (1800, 10000, 4405, 4400, 4736, 3806)] == [
        minor('116.62'), 0, 0, minor('-100.00'), minor('2.00'), minor('-18.62')]
    assert helpers.euer_values(db, RANGE) == {4400: minor('100.00'), 4736: minor('-2.00'), 3806: minor('18.62')}
    check_every_probe(db)
    assert db.link_bank_to_entries().get('resolved') == 1                                              # Stufe 7: 50,00 + 119,00 „an 10000“ decken die Forderung
    check_every_probe(db)


@pytest.mark.parametrize('rate, gross, paid, skonto_number, euer', [
    (0.07, 107.0, '104.86', 4731, {4300: '100.00', 4731: '-2.00', 3806: '6.86'}),
    (0.0, 100.0, '98.00', 4730, {4185: '100.00', 4730: '-2.00'}),
    (-1, 100.0, '98.00', 4730, {4185: '100.00', 4730: '-2.00'}),
], ids=['7-Prozent', '0-Prozent', 'par-19'])
@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_other_rates_close_the_chain_and_every_probe_holds(db, rate, gross, paid, skonto_number, euer, receivable):
    world(db, receivable=receivable, gross=gross, rate=rate, paid=paid)

    assert helpers.euer_values(db, RANGE) == {number: minor(value) for number, value in euer.items()}
    assert ending(db.get_susa(2025, 4), skonto_number) == minor('2.00')                                 # die Erlösschmälerung (netto, ohne die Steuer des Skontos) steht im Soll
    assert ending(db.get_susa(2025, 4), 10000) == 0
    check_every_probe(db)


# ═════════════════════════════════════════════════════════════════════════════
# DATEV
# ═════════════════════════════════════════════════════════════════════════════

def test_the_datev_rows_of_a_payment_with_a_discount_and_a_receivable(db):
    world(db)

    assert datev_rows(db, 'SK-100') == [
        ('119,00', 'S', '10000', '4405', '0', '19,00', '03032025'),                                    # die Forderung: Leistungsdatum in Spalte 114
        ('119,00', 'S', '1800', '10000', '0', '', ''),                                                 # „Bank an Debitoren“ über Geld plus Skonto
        ('119,00', 'S', '4405', '4400', '0', '19,00', ''),
        ('2,38', 'S', '4736', '1800', '101', '19,00', '')]                                             # der Skonto-Satz: Umsatzsteuer-Art, Satz in Spalte 40


def test_the_datev_rows_of_a_payment_with_a_discount_without_a_receivable(db):
    world(db, receivable=False)

    assert datev_rows(db, 'SK-100') == [('119,00', 'H', '4400', '1800', '101', '19,00', ''),
                                        ('2,38', 'S', '4736', '1800', '101', '19,00', '')]


def test_the_datev_rows_of_the_other_rates(db):
    world(db, receivable=False, gross=107.0, rate=0.07, paid='104.86')
    seven = datev_rows(db, 'SK-100')
    assert seven == [('107,00', 'H', '4300', '1800', '102', '7,00', ''), ('2,14', 'S', '4731', '1800', '102', '7,00', '')]


def test_the_datev_row_of_a_discount_without_tax_has_no_key_and_no_rate(db):
    world(db, receivable=False, gross=100.0, rate=-1, paid='98.00')

    assert datev_rows(db, 'SK-100') == [('100,00', 'H', '4185', '1800', '0', '', ''), ('2,00', 'S', '4730', '1800', '0', '', '')]


def test_the_money_of_the_batch_is_the_money_of_the_statement(db):
    """Die Bankseite der Zeilen („1800 an 10000“ 119,00 und „4736 an 1800“ 2,38) ergibt zusammen den Auszug: 116,62."""
    world(db)

    bank = D(0)
    for amount, side, account, counter, _key, _rate, _date in datev_rows(db, 'SK-100'):
        if '1800' not in (account, counter):
            continue
        bank_side = side if account == '1800' else ('H' if side == 'S' else 'S')                          # auf welcher Seite steht 1800?
        bank += D(amount.replace(',', '.')) * (1 if bank_side == 'S' else -1)
    assert bank == D('116.62')


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard, Liquidität, Split-Summe, Prüfliste
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_dashboard_counts_the_money_received_as_income(db, receivable):
    world(db, receivable=receivable)

    totals, _monthly = db.get_dashboard('2025-04-01', '2025-04-30')

    assert (round(totals['income'], 2), totals['expense'], totals['private'], totals['transfers'], round(totals['balance'], 2), totals['unlinked_count']) == (
        116.62, 0, 0, 0, 116.62, 0)


def test_the_bank_movement_has_no_free_rest_and_the_rows_below_it_add_up_to_it(db):
    _invoice_id, bank = world(db)

    assert db.get_booking_unallocated_amount(bank) == 0
    assert db.find_unbalanced_splits(*RANGE) == []


def test_the_payment_check_finds_nothing_after_every_step(db):
    invoice_id = invoice_of(db, 'SK-100', 119.0)
    assert db.find_payment_inconsistencies() == []
    bank = pay(db, invoice_id, '116.62', '2025-04-10', skonto=True)
    assert db.find_payment_inconsistencies() == []
    db.link_bank_to_entries()
    assert db.find_payment_inconsistencies() == []
    db.unassign_invoice_payment(db.get_invoice_payments(invoice_id)[0][0])
    assert db.find_payment_inconsistencies() == [] and db.get_booking_allocations(bank) == []


# ═════════════════════════════════════════════════════════════════════════════
# skonto_mismatch und der Neuaufbau
# ═════════════════════════════════════════════════════════════════════════════

def findings(db, invoice_id=None):
    return [(f['category'], f['text']) for f in db.find_payment_inconsistencies(invoice_id)]


def test_an_untouched_assignment_has_no_skonto_finding(db):
    _invoice_id, _bank = world(db)
    assert not [f for f in findings(db) if f[0] == 'skonto_mismatch']


def test_a_deleted_discount_row_is_found(db):
    invoice_id, bank = world(db)
    raw(db, 'DELETE FROM Bookings WHERE ParentBooking_ID = ? AND COA_ID = ?', bank, db.get_coa_id_by_account_number(4736))

    assert findings(db, invoice_id) == [('skonto_mismatch', 'Zuordnung #1 trägt Skonto 2,38 €, gebucht sind 0,00 €.')]


def test_a_changed_discount_row_is_found(db):
    invoice_id, bank = world(db)
    raw(db, 'UPDATE Bookings SET Amount = -20000 WHERE ParentBooking_ID = ? AND COA_ID = ?', bank, db.get_coa_id_by_account_number(4736))

    assert findings(db, invoice_id) == [('skonto_mismatch', 'Zuordnung #1 trägt Skonto 2,38 €, gebucht sind 2,00 €.')]


def test_a_discount_row_without_a_discount_on_the_assignment_is_found_with_the_other_two_findings(db):
    """Ohne ``SkontoAmount`` an der Zuordnung zählt nur das Geld: Der offene Rest und die Umbuchung passen dann auch nicht mehr."""
    invoice_id, _bank = world(db)
    raw(db, 'UPDATE InvoicePayments SET SkontoAmount = NULL')

    assert [category for category, _text in findings(db, invoice_id)] == ['transfer_mismatch', 'amount_due_mismatch', 'skonto_mismatch']
    assert ('skonto_mismatch', 'Zuordnung #1 trägt Skonto 0,00 €, gebucht sind 2,38 €.') in findings(db, invoice_id)
    assert ('amount_due_mismatch', 'Offen sind 0,00 €, nach den Zuordnungen müssten es 2,38 € sein.') in findings(db, invoice_id)
    assert ('transfer_mismatch', 'Zugeordnet sind 116,62 €, auf das Erlöskonto umgebucht 119,00 €.') in findings(db, invoice_id)


def test_the_open_amount_and_the_transfer_count_the_discount_as_settled(db):
    """Dieselbe Rechnung, derselbe Stand – nur mit dem Skonto: kein Befund. Der Geldbegriff der Rechnung ist Geld plus Skonto."""
    invoice_id, _bank = world(db)

    assert findings(db, invoice_id) == [] and findings(db) == []
    assert raw(db, 'SELECT AmountDue, Status FROM Invoices WHERE ID = ?', invoice_id) == [(0, 'paid')]


def test_the_finding_is_per_assignment_and_filters_by_invoice(db):
    first, bank = world(db)
    second = invoice_of(db, 'SK-200', 119.0, day='2025-03-05')
    pay(db, second, '116.62', '2025-04-12', skonto=True)
    raw(db, 'DELETE FROM Bookings WHERE ParentBooking_ID = ? AND COA_ID = ?', bank, db.get_coa_id_by_account_number(4736))

    assert [text for category, text in findings(db) if category == 'skonto_mismatch'] == ['Zuordnung #1 trägt Skonto 2,38 €, gebucht sind 0,00 €.']
    assert findings(db, second) == []
    assert findings(db, first) == [('skonto_mismatch', 'Zuordnung #1 trägt Skonto 2,38 €, gebucht sind 0,00 €.')]


def test_a_collective_payment_keeps_each_invoice_apart(db):
    """Eine Bewegung, zwei Rechnungen: A schließt mit Skonto, B ohne. Die Sätze werden je Rechnung (Beleg-Nr.) gezählt: kein Befund, die Proben gehen auf."""
    first = invoice_of(db, 'SK-A', 119.0)
    second = invoice_of(db, 'SK-B', 98.0, rate=0.19, day='2025-03-04')
    bank = db.insert_booking('2025-04-10', D('214.62'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')

    assert link_booking_to_invoice_capped(db, first, bank, D('116.62'), None, True) == (True, None)
    assert link_booking_to_invoice_capped(db, second, bank, D('98.00')) == (True, None)

    assert findings(db) == []
    assert raw(db, 'SELECT SkontoAmount FROM InvoicePayments ORDER BY ID') == [(23800,), (None,)]
    assert db.get_booking_unallocated_amount(bank) == 0
    check_every_probe(db, (4, 5))


def test_the_rebuild_counts_the_discount_as_settled(db):
    """Schritt D der Reparatur rechnet den offenen Betrag mit Geld plus Skonto: Eine Rechnung, die mit Skonto bezahlt ist, bleibt 0 und „bezahlt“ – ohne eine Aktion."""
    invoice_id, _bank = world(db)

    plan = db.rebuild_invoice_payments(invoice_id, today='2025-12-31').plan

    assert plan.actions == [] and (plan.due_before, plan.due_after, plan.status_before, plan.status_after) == (0, 0, 'paid', 'paid')
    assert plan.findings_before == plan.findings_after == []


def test_the_rebuild_brings_a_stale_open_amount_back_to_zero_and_keeps_the_status(db):
    invoice_id, _bank = world(db)
    raw(db, 'UPDATE Invoices SET AmountDue = 23800 WHERE ID = ?', invoice_id)                          # ein veralteter Rest (als hätte ihn jemand von Hand gesetzt)
    assert [category for category, _text in findings(db, invoice_id)] == ['amount_due_mismatch']

    result = db.rebuild_invoice_payments(invoice_id, today='2025-12-31')
    assert [action[0] for action in result.plan.actions] == ['recalc'] and result.plan.due_after == 0
    done = db.rebuild_invoice_payments(invoice_id, dry_run=False, confirmed_check=result.plan.check, today='2025-12-31')

    assert done.ok and done.executed
    assert raw(db, 'SELECT AmountDue, Status FROM Invoices WHERE ID = ?', invoice_id) == [(0, 'paid')]
    assert raw(db, 'SELECT Amount, SkontoAmount FROM InvoicePayments') == [(1166200, 23800)]          # die Zuordnung und ihre Sätze bleiben, wie sie waren
    assert findings(db) == []


# ═════════════════════════════════════════════════════════════════════════════
# Zufallsbestände: nur Wege der App, feste Seeds
# ═════════════════════════════════════════════════════════════════════════════

def skonto_stock(db, seed, invoices=8):
    """``invoices`` Rechnungen mit 19, 7, 0 % oder § 19 (mit und ohne Forderung), 0 bis 2 Teilzahlungen und einer schließenden Zahlung – mit Skonto (der Rest liegt zwischen
    einem Cent und dem Skonto der Rechnung) oder ohne –, alles über ``link_booking_to_invoice_capped``. Liefert ``[(Rechnungs-ID, Nummer, Brutto, Skonto)]`` (Minor Units;
    Skonto 0, wenn die Rechnung ohne Skonto bezahlt ist)."""
    rng = random.Random(seed)
    stock = []
    for number in range(invoices):
        rate = rng.choice([0.19, 0.07, 0.0, -1])
        gross_cents = rng.randint(5_000, 400_000)
        percent = rng.choice([1.0, 2.0, 2.5, 3.0])
        day = datetime.date(2025, rng.choice([3, 4]), rng.randint(1, 28))
        name = f'Z{seed}-{number}'
        invoice_id = invoice_of(db, name, gross_cents / 100, rate, day.isoformat(), skonto=(rng.choice([7, 10, 14]), percent), receivable=rng.random() < 0.7)
        left, when = gross_cents, day
        for _ in range(rng.randint(0, 2)):                                                             # Teilzahlungen, jede höchstens 60 % des Rests
            part = rng.randint(1, max(1, left * 6 // 10))
            when += datetime.timedelta(days=rng.randint(5, 20))
            pay(db, invoice_id, f'{part / 100:.2f}', when.isoformat(), account=rng.choice(['Probe-Giro', 'Probe-Zweit']))
            left -= part
        limit = skonto_limit(to_minor(D(gross_cents) / 100), D(str(percent))) // 100                 # Cent
        discount = rng.randint(1, min(limit, left - 1)) if limit >= 1 and left > 1 and rng.random() < 0.75 else 0
        when += datetime.timedelta(days=rng.randint(5, 20))
        pay(db, invoice_id, f'{(left - discount) / 100:.2f}', when.isoformat(), skonto=bool(discount), account=rng.choice(['Probe-Giro', 'Probe-Zweit']))
        stock.append((invoice_id, name, to_minor(D(gross_cents) / 100), discount * 100))
    return stock


@pytest.mark.parametrize('seed', [21, 22, 23, 24])
def test_a_random_stock_with_discounts_balances_in_every_month_and_the_probes_hold(db, seed):
    skonto_stock(db, seed)

    check_every_probe(db, (3, 4, 5, 6, 7, 8, 9), seed)
    db.link_bank_to_entries()
    check_every_probe(db, (5, 9), seed)                                                                  # nach Stufe 7 des Abgleichs ebenso


@pytest.mark.parametrize('seed', [21, 22, 23, 24])
def test_every_invoice_of_the_random_stock_is_paid_and_carries_exactly_its_discount(db, seed):
    stock = skonto_stock(db, seed)
    with_discount = 0

    for invoice_id, name, gross, discount in stock:
        assert raw(db, 'SELECT Status, AmountDue FROM Invoices WHERE ID = ?', invoice_id) == [('paid', 0)], name
        money = raw(db, 'SELECT SUM(Amount), SUM(COALESCE(SkontoAmount, 0)) FROM InvoicePayments WHERE InvoiceID = ?', invoice_id)[0]
        assert money == (gross - discount, discount), (seed, name)                                       # das Geld plus das Skonto ist die Rechnung
        if discount:
            with_discount += 1
            head_tax, head_rate = raw(db, 'SELECT TaxAmount, TaxRate FROM Invoices WHERE ID = ?', invoice_id)[0]
            booked = raw(db, "SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE DocumentNumber = ? AND Status = 'invoice_payment'", name)[0][0]
            assert booked == (head_tax or 0) - skonto_tax(discount, {0.19: 19, 0.07: 7}.get(head_rate, 0)), (seed, name)       # die Steuer des gezahlten Geldes
    assert with_discount >= 1                                                                            # mit diesen Seeds gibt es mindestens eine Rechnung mit Skonto
