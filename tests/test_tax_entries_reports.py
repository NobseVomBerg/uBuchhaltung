# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Buchungen eines Belegs mit Steuersätzen aus den Stammdaten in den Auswertungen (2.8.0, Etappe 5): EÜR, Betriebswirtschaftliche Auswertung, Summen- und Saldenliste,
DATEV-Stapel, Liquidität, Dashboard und Prüfliste – **ohne Änderung an den Auswertungen selbst**. Ihre Abstimmungsproben gehen auf den Minor-Wert genauso auf wie für einen Beleg
mit 19 und 7 %; nur eine hat einen Zusatz.

Die Welt hat drei Belege (``3.3.2025``), bezahlt in zwei Monaten (``test_tax_entries_bookings.py`` beschreibt sie; die Positionen tragen die Momentaufnahme eines Steuersatzes aus den
Stammdaten – kein Weg der App schreibt sie in dieser Etappe, der Test legt sie direkt an):

* **F** (``F-100``): ganz steuerfrei, 100,00 – bezahlt mit 100,00 am 02.04.;
* **G** (``G-100``): 19 % × 100,00 und steuerfrei × 80,00, zusammen 199,00 – bezahlt mit 100,00 am 02.04. (59,80 für 19 %, 40,20 steuerfrei) und 99,00 am 05.05.;
* **H** (``H-100``): 19 % × 100,00 und 5,5 % × 200,00, zusammen 330,00, ohne Forderung (5,5 % hat kein Wartekonto) – bezahlt mit 100,00 am 02.04. (36,06 und 63,94) und 230,00 am 05.05.

* **EÜR:** je Satz der Erlös netto auf dem Erlöskonto der Momentaufnahme, die Umsatzsteuer aller Sätze auf der virtuellen 3806; steuerfreier Erlös ohne Steuer.
* **Summen- und Saldenliste:** Soll = Haben, die Wartekonten laufen auf 0. 19 % steht auf 3806, **5,5 % auf „Steuer ohne Steuerkonto“ mit Hinweis** (kein geratenes Steuerkonto): Die Probe
  „EÜR-Umsatzsteuer = 3801 + 3806“ gilt dort mit dem Zusatz dieser Steuer.
* **DATEV:** ein Satz außerhalb der vier hat den Schlüssel 0 und trägt den Satz in Spalte 40 („5,50“); eine steuerfreie Zeile trägt keinen Satz und keine Spalte 40.
* **Liquidität** (nichts „Nicht zugeordnet“), **Dashboard** (die Zahlungen) und **Prüfliste** (kein Befund).

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da.
"""
from decimal import Decimal

import pytest

from db import susa
from money import from_minor, to_minor
from tax_groups import item_groups
from tests import liquidity_helpers as liq
from tests import susa_helpers as helpers
from tests.test_bwa import check_invariants
from tests.test_mixed_tax_reports import datev_rows, ending, is_settlement, minor, pay, shown, with_banks
from tests.test_tax_entries_bookings import FREE, REASON, REDUCED, account, entry, free, plain

D = Decimal
INVOICE_DAY = '2025-03-03'
APRIL, MAY = '2025-04-02', '2025-05-05'
MONTHS = (3, 4, 5, 6)


# ── Welt und Hilfen ──────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_db):
    return with_banks(tmp_db)


def invoice_of(db, number, parts, day):
    """Eine versendete Rechnung aus ``parts`` mit ihrer Forderung (``ensure_receivable_booking``): die Summen aus ``tax_groups.item_groups``, der Kopf mit dem höchsten Satz und – bei mehr
    als einem Satz – dem Kennzeichen. Die Positionen tragen ihre Momentaufnahme (direkt geschrieben)."""
    groups = item_groups([(float(part.pct), to_minor(D(part.net)), None if part.entry is None else part.entry['tax_rate_bp']) for part in parts])
    net, tax = sum(group.net for group in groups), sum(group.tax for group in groups)
    invoice_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': day, 'delivery_date': day, 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH', 'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': float(max(group.pct for group in groups) / 100), 'sum_net': from_minor(net), 'tax_amount': from_minor(tax), 'sum_gross': from_minor(net + tax),
        'amount_due': from_minor(net + tax), 'status': 'sent', 'mixed_tax_rates': 1 if len(groups) > 1 else None})
    for position, part in enumerate(parts, 1):
        db.insert_invoice_item({'invoice_id': invoice_id, 'position': position, 'description': f'Position {position}', 'quantity': 1, 'unit': 'C62',
                                'price_per_unit': D(part.net), 'total_net': D(part.net), 'tax_rate': float(part.pct), **(part.entry or {})})
    db.ensure_receivable_booking(invoice_id)                      # ein Beleg ohne Wartekonto (5,5 %) bucht keine Forderung: Der Aufruf ändert dann nichts
    return invoice_id


def entry_world(db, stop_after=2):
    """F, G und H mit Forderung (März) und den Zahlungen vom April (``stop_after`` 1) und Mai (2); 0: nur die Forderungen. Liefert ``(F, G, H)`` als Rechnungs-IDs."""
    free_coa, reduced_coa = account(db, FREE), account(db, REDUCED)
    f = invoice_of(db, 'F-100', [free('100.00', free_coa)], INVOICE_DAY)
    g = invoice_of(db, 'G-100', [plain(19, '100.00'), free('80.00', free_coa)], INVOICE_DAY)
    h = invoice_of(db, 'H-100', [plain(19, '100.00'), entry('5.5', '200.00', reduced_coa)], INVOICE_DAY)
    for step, payments in enumerate((((f, '100.00'), (g, '100.00'), (h, '100.00')), ((g, '99.00'), (h, '230.00')))[:stop_after]):
        for invoice_id, amount in payments:
            pay(db, invoice_id, amount, (APRIL, MAY)[step])
    return f, g, h


def deviations_with_the_unassigned_tax(db, report, where=''):
    """Probe (c) der Summen- und Saldenliste mit dem Zusatz: Für jede EÜR-Zeile eines Abschnitts außer 3806 ist der Saldo der EÜR gleich (Haben − Soll) der Liste; die Umsatzsteuer der EÜR
    (3806, die echte und die virtuelle Zeile) ist (H − S) von 3801 plus 3806 **plus der Steuer auf „Steuer ohne Steuerkonto“** (``susa.TAX_UNASSIGNED``: ein Satz außerhalb von 5, 7, 16 und
    19 % hat kein Steuerkonto). Returns die Abweichungen (leer = alles stimmt)."""
    accounts = helpers.accounts_of(report)
    found = []
    for period, span in (('month', report.periods['month']), ('ytd', report.periods['ytd'])):
        def net(number):
            line = accounts.get(number)
            if line is None:
                return 0
            return line.month_haben - line.month_soll if period == 'month' else line.ytd_haben - line.ytd_soll
        for number, value in helpers.euer_values(db, span).items():
            got = net(3801) + net(3806) + net(susa.TAX_UNASSIGNED) if number == 3806 else net(number)
            if got != value:
                found.append((where, period, number, value, got))
    return found


# ═════════════════════════════════════════════════════════════════════════════
# EÜR
# ═════════════════════════════════════════════════════════════════════════════

def test_the_euer_shows_the_revenue_net_per_account_of_the_snapshot_and_the_tax_of_every_rate_on_3806(db):
    entry_world(db)

    assert helpers.euer_values(db, ('2025-04-01', '2025-04-30')) == {4400: minor('80.55'), 4120: minor('140.20'), 4150: minor('60.61'), 3806: minor('18.64')}
    assert helpers.euer_values(db, ('2025-05-01', '2025-05-31')) == {4400: minor('119.45'), 4120: minor('39.80'), 4150: minor('139.39'), 3806: minor('30.36')}
    # das Jahr: 19 % von G und H (100,00 netto je), steuerfrei von F und G (100,00 und 80,00), 5,5 % von H (200,00) – die Steuer 19,00 + 19,00 + 11,00 auf einer Zeile
    assert helpers.euer_values(db, ('2025-01-01', '2025-12-31')) == {4400: minor('200.00'), 4120: minor('180.00'), 4150: minor('200.00'), 3806: minor('49.00')}


def test_the_forderung_alone_is_no_revenue_and_the_accounts_of_the_chain_are_not_in_the_euer(db):
    entry_world(db, stop_after=0)

    assert helpers.euer_values(db, ('2025-01-01', '2025-12-31')) == {}
    entry_world_numbers = {number for number, _name, _total in db.get_euer_data('2025-01-01', '2025-12-31')}
    assert not entry_world_numbers & {10000, 4340, 4405}


# ═════════════════════════════════════════════════════════════════════════════
# Betriebswirtschaftliche Auswertung
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('month', MONTHS)
@pytest.mark.parametrize('variant', ['jahr', 'vorjahr'])
def test_the_bwa_equals_the_euer_to_the_minor_unit_in_every_month(db, month, variant):
    entry_world(db)

    check_invariants(db, db.get_bwa(2025, month, variant))


# ═════════════════════════════════════════════════════════════════════════════
# Summen- und Saldenliste
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('month', MONTHS)
def test_every_probe_of_the_susa_holds_in_every_month_with_the_addition_for_the_tax_without_an_account(db, month):
    entry_world(db)
    report = db.get_susa(2025, month)

    helpers.assert_balanced(report, month)
    assert helpers.check_bank_accounts(db, report, month) == []
    assert deviations_with_the_unassigned_tax(db, report, month) == []


def test_the_susa_of_may_books_19_percent_on_3806_and_5_5_percent_on_the_tax_without_an_account(db):
    entry_world(db)
    report = db.get_susa(2025, 5)

    helpers.assert_balanced(report, 'Mai')
    assert ending(report, 1800) == minor('629.00')                                                    # die Summe der Bankbewegungen
    assert [ending(report, number) for number in (10000, 4340, 4405)] == [0, 0, 0]                    # die Wartekonten laufen auf 0
    assert (ending(report, 4400), ending(report, 4120), ending(report, 4150)) == (-minor('200.00'), -minor('180.00'), -minor('200.00'))     # H
    assert ending(report, 3806) == -minor('38.00') and ending(report, 3801) == 0                       # 19 % von G und H
    assert ending(report, susa.TAX_UNASSIGNED) == -minor('11.00')                                      # 5,5 %: kein Steuerkonto, kein geratenes
    assert [(notice.key, notice.count) for notice in report.notices if notice.key == susa.TAX_UNASSIGNED_NOTICE] == [(susa.TAX_UNASSIGNED_NOTICE, 2)]


def test_the_notice_names_the_tax_without_an_account_from_the_month_of_the_first_5_5_percent_payment(db):
    entry_world(db)

    assert [n.key for n in db.get_susa(2025, 3).notices] == []
    april = [(n.key, n.count, n.amount) for n in db.get_susa(2025, 4).notices]
    assert april == [(susa.TAX_UNASSIGNED_NOTICE, 1, minor('3.33'))]                                  # die Steuer des Teils von April: 3,33


def test_the_plain_probe_of_the_susa_misses_exactly_the_tax_without_an_account(db):
    """Die Probe (c) der Liste (``helpers.check_erfolgskonten_against_euer``) rechnet die Umsatzsteuer als 3801 + 3806. Mit einem bezahlten Satz außerhalb der vier weicht sie genau um die Steuer
    auf „Steuer ohne Steuerkonto“ ab – so steht es im Kopf von ``tests/test_susa.py`` (deshalb zeigt der Demobestand 5,5 % nur versendet, nicht bezahlt)."""
    entry_world(db)
    for month in (4, 5, 6):
        report = db.get_susa(2025, month)
        accounts = helpers.accounts_of(report)
        plain_deviations = helpers.check_erfolgskonten_against_euer(db, report, month)

        assert plain_deviations, month
        for _where, period, number, expected, got in plain_deviations:
            line = accounts[susa.TAX_UNASSIGNED]
            unassigned = line.month_haben - line.month_soll if period == 'month' else line.ytd_haben - line.ytd_soll
            assert number == 3806 and expected - got == unassigned, (month, period)


def test_without_a_paid_5_5_percent_rate_the_plain_probe_holds(db):
    """Die Gegenprobe: F und G allein (steuerfrei und 19 %) lassen die Probe ohne Zusatz aufgehen."""
    free_coa = account(db, FREE)
    f = invoice_of(db, 'F-100', [free('100.00', free_coa)], INVOICE_DAY)
    g = invoice_of(db, 'G-100', [plain(19, '100.00'), free('80.00', free_coa)], INVOICE_DAY)
    pay(db, f, '100.00', APRIL)
    pay(db, g, '199.00', APRIL)

    for month in MONTHS:
        report = db.get_susa(2025, month)
        assert helpers.check_erfolgskonten_against_euer(db, report, month) == [] and not [n for n in report.notices if n.key == susa.TAX_UNASSIGNED_NOTICE]


# ═════════════════════════════════════════════════════════════════════════════
# DATEV
# ═════════════════════════════════════════════════════════════════════════════

def test_the_datev_rows_of_the_tax_free_invoice_carry_no_rate_in_column_40(db):
    entry_world(db)

    rows = datev_rows(db, 'F-100')

    assert shown(rows) == [('10000', '4340', 'S', '100,00', '0303', '', '03032025'),             # die Forderung: Leistungsdatum, kein Satz
                           ('1800', '10000', 'S', '100,00', '0204', '', ''),                     # Bank an Debitoren
                           ('4340', '4120', 'S', '100,00', '0204', '', '')]                      # Wartekonto an das Erlöskonto der Momentaufnahme
    assert {row[8] for row in rows} == {'0'}                                                      # die Rechnungskette trägt keinen BU-Schlüssel


def test_the_datev_rows_of_19_percent_and_tax_free_carry_the_rate_only_on_the_19_percent_rows(db):
    entry_world(db)

    rows = datev_rows(db, 'G-100')

    assert shown(rows) == [('10000', '4405', 'S', '119,00', '0303', '19,00', '03032025'), ('10000', '4340', 'S', '80,00', '0303', '', '03032025'),
                           ('1800', '10000', 'S', '100,00', '0204', '', ''), ('4405', '4400', 'S', '59,80', '0204', '19,00', ''), ('4340', '4120', 'S', '40,20', '0204', '', ''),
                           ('1800', '10000', 'S', '99,00', '0505', '', ''), ('4405', '4400', 'S', '59,20', '0505', '19,00', ''), ('4340', '4120', 'S', '39,80', '0505', '', '')]
    assert {row[8] for row in rows} == {'0'} and len([row for row in rows if is_settlement(row)]) == 2


def test_a_rate_outside_the_four_has_key_0_and_its_rate_in_column_40(db):
    """5,5 % hat keinen BU-Schlüssel (``datev_keys.derive_key``: 0) – der Satz steht allein in Spalte 40; der Schlüssel der 19 % bleibt 101."""
    entry_world(db)

    rows = datev_rows(db, 'H-100')

    assert shown(rows) == [('4400', '1800', 'H', '36,06', '0204', '19,00', ''), ('4150', '1800', 'H', '63,94', '0204', '5,50', ''),
                           ('4400', '1800', 'H', '82,94', '0505', '19,00', ''), ('4150', '1800', 'H', '147,06', '0505', '5,50', '')]
    assert [(row[6], row[8], row[40]) for row in rows] == [('4400', '101', '19,00'), ('4150', '0', '5,50'), ('4400', '101', '19,00'), ('4150', '0', '5,50')]   # Konto, Schlüssel, Spalte 40


# ═════════════════════════════════════════════════════════════════════════════
# Liquidität, Dashboard, Prüfliste
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('month', MONTHS)
def test_the_liquidity_has_nothing_unassigned_in_any_month(db, month):
    entry_world(db)

    for variant in ('vorjahr', 'jahr'):
        report = db.get_liquidity(2025, month, variant)

        assert set(liq.unassigned_of(report).values()) == {0}, (month, variant, liq.unassigned_of(report))
        liq.check_against_sources(db, report, (month, variant))


def test_the_dashboard_counts_the_payments_as_income_and_the_balance_is_the_money_received(db):
    entry_world(db)

    april, _ = db.get_dashboard('2025-04-01', '2025-04-30')
    may, _ = db.get_dashboard('2025-05-01', '2025-05-31')
    year, monthly = db.get_dashboard('2025-01-01', '2025-12-31')

    assert (april['income'], april['transfers'], april['balance']) == (300.00, 0, 300.00)           # F 100,00 + G 100,00 + H 100,00
    assert (may['income'], may['transfers'], may['balance']) == (329.00, 0, 329.00)                 # G 99,00 + H 230,00
    assert (year['income'], year['expense'], year['private'], year['transfers'], year['balance']) == (629.00, 0, 0, 0, 629.00)
    assert year['unlinked_count'] == 0 and [month['income'] for month in monthly if month['income']] == [300.00, 329.00]


@pytest.mark.parametrize('steps', [0, 1, 2])
def test_the_payment_check_finds_nothing_after_every_step(db, steps):
    entry_world(db, stop_after=steps)

    assert db.find_payment_inconsistencies() == []


def test_the_payment_check_finds_nothing_after_the_match_resolved_the_receivables(db):
    entry_world(db)

    assert db.link_bank_to_entries()['resolved'] == 3                  # F: eine Forderung, G: zwei – H hat keine
    assert db.find_payment_inconsistencies() == []


def test_the_world_carries_the_snapshot_and_the_rate_of_the_tax_free_group_is_the_one_of_the_snapshot(db):
    f, g, h = entry_world(db)

    stored = {row[0]: row[1:] for row in db._get_connection().execute(
        'SELECT InvoiceId, TaxCategory, TaxRateBp, TaxExemptReason FROM InvoiceItems WHERE Position = (SELECT MAX(Position) FROM InvoiceItems i WHERE i.InvoiceId = InvoiceItems.InvoiceId)')}

    assert stored == {f: ('E', 0, REASON), g: ('E', 0, REASON), h: ('S', 550, None)}


@pytest.mark.parametrize('month', MONTHS)
def test_every_account_of_the_chain_nets_to_zero_after_the_last_payment(db, month):
    """Debitoren und die Wartekonten laufen nach der letzten Zahlung auf 0 – in der Liste jedes Monats ab Mai."""
    entry_world(db)
    report = db.get_susa(2025, month)

    if month >= 5:
        assert [ending(report, number) for number in (10000, 4340, 4405)] == [0, 0, 0]
    else:
        assert (ending(report, 10000), ending(report, 4340), ending(report, 4405)) != (0, 0, 0)       # davor stehen offene Posten
