# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Buchungen eines Belegs mit Steuersatz je Position in den Auswertungen (2.7.0, Etappe 2a): EÜR, Summen- und Saldenliste, DATEV-Stapel,
Liquidität, Dashboard und Prüfliste – **ohne Änderung an den Auswertungen selbst**. Ihre Abstimmungsproben gehen für so einen Beleg auf den
Minor-Wert genauso auf wie für einen mit einem Satz.

Die feste Rechnung ist **M** (``M-100``, 03.03.2025): 19 % × 100,00 und 7 % × 50,00 – Gruppe 19: netto 100,00 / Steuer 19,00 / brutto 119,00,
Gruppe 7: 50,00 / 3,50 / 53,50, zusammen 172,50 – bezahlt mit 100,00 am 02.04.2025 und 72,50 am 05.05.2025 (die Verteilung: ``tax_groups.split_payment``,
68,99 + 31,01 und 50,01 + 22,49).

* **EÜR:** je Satz der Erlös netto, die Umsatzsteuer auf der virtuellen 3806 – nur die Zahlungen zählen, nicht die Forderung.
* **Summen- und Saldenliste:** Soll = Haben, die Konten der Forderung (10000, 4405, 4345) laufen auf 0, 3806 und 3801 tragen die Steuer je Satz.
* **DATEV:** die Rechnungskette je Zeile (Spalte 40 mit dem Satz, Spalte 114 an den Forderungen).
* **Additivität:** dieselben Zahlungen auf zwei Rechnungen mit je einem Satz ergeben je Konto dieselben Salden und dieselben DATEV-Zeilen.
* **Zufallsbestände** (fester Seed, nur Wege der App): Die Proben gehen in jedem Monat auf.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da.
"""
import csv
import datetime
import random
import sqlite3
import contextlib
from decimal import Decimal

import pytest

from db import Database
from export import datev
from money import from_minor, to_minor
from server.handlers import link_booking_to_invoice_capped
from tax_groups import rate_groups
from tests import liquidity_helpers as liq
from tests import susa_helpers as helpers
from tests.susa_helpers import account_id

D = Decimal
NUMBER = 'M-100'
INVOICE_DAY = '2025-03-03'
APRIL, MAY = '2025-04-02', '2025-05-05'
ITEMS = [(19, '100.00'), (7, '50.00')]
MONTHS = (3, 4, 5, 6)


# ── Welt und Hilfen ──────────────────────────────────────────────────────────

def with_banks(db):
    db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    return db


@pytest.fixture
def db(tmp_db):
    return with_banks(tmp_db)


def second_db(tmp_path):
    """Eine zweite, eigene Datenbank für eine Vergleichswelt."""
    return with_banks(Database(db_name=str(tmp_path / 'vergleich.db')))


def invoice_of(db, number, items, day, rate=None):
    """Eine versendete Rechnung aus ``(Satz in Prozent, Netto in Euro)``-Positionen mit ihrer Forderung – mit Steuersatz je Position (Kennzeichen 1), wenn die
    Positionen mehr als einen Satz tragen. Die Summen kommen aus ``tax_groups.rate_groups`` (so rechnet auch das Speichern der App)."""
    groups = rate_groups([(D(rate_), to_minor(D(net))) for rate_, net in items])
    net = sum(group.net for group in groups)
    tax = sum(group.tax for group in groups)
    head = float(max(group.pct for group in groups) / 100) if rate is None else rate
    invoice_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': day, 'delivery_date': day, 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH', 'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': head, 'sum_net': from_minor(net), 'tax_amount': from_minor(tax), 'sum_gross': from_minor(net + tax),
        'amount_due': from_minor(net + tax), 'status': 'sent', 'mixed_tax_rates': 1 if len(groups) > 1 else None})
    for position, (rate_, net_euro) in enumerate(items, 1):
        db.insert_invoice_item({'invoice_id': invoice_id, 'position': position, 'description': f'Position {position}', 'quantity': 1, 'unit': 'C62',
                                'price_per_unit': D(net_euro), 'total_net': D(net_euro), 'tax_rate': float(rate_)})
    assert db.ensure_receivable_booking(invoice_id) is not None
    return invoice_id


def pay(db, invoice_id, amount, day, account='Probe-Giro'):
    """Eine Bankbewegung ``amount`` € am Tag ``day`` und ihre Zuordnung an die Rechnung, wie die Oberfläche es tut; liefert die Bankbewegung."""
    bank = db.insert_booking(day, D(str(amount)), account_id=account_id(db, account), booking_type='bank', recipient_client='Käufer AG')
    ok, error = link_booking_to_invoice_capped(db, invoice_id, bank, D(str(amount)))
    assert ok, error
    return bank


def mixed_world(db, stop_after=2):
    """M mit Forderung (März) und den Zahlungen vom April und Mai; ``stop_after``: wie viele Zahlungen (0, 1 oder 2). Liefert die Rechnungs-ID."""
    invoice_id = invoice_of(db, NUMBER, ITEMS, INVOICE_DAY)
    for amount, day in [('100.00', APRIL), ('72.50', MAY)][:stop_after]:
        pay(db, invoice_id, amount, day)
    return invoice_id


def single_rate_world(db):
    """Dieselben Zahlungsbeträge als zwei Rechnungen mit einem Satz (19 % × 100,00 und 7 % × 50,00), bezahlt mit den Teilen je Satz: eine Bankbewegung, zwei
    Zuordnungen."""
    r19 = invoice_of(db, 'E-19', [(19, '100.00')], INVOICE_DAY, rate=0.19)
    r7 = invoice_of(db, 'E-07', [(7, '50.00')], INVOICE_DAY, rate=0.07)
    for first, second, day in (('68.99', '31.01', APRIL), ('50.01', '22.49', MAY)):
        bank = db.insert_booking(day, D(first) + D(second), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
        for invoice_id, amount in ((r19, first), (r7, second)):
            ok, error = link_booking_to_invoice_capped(db, invoice_id, bank, D(amount))
            assert ok, error


def ending(report, number):
    """Endsaldo (Soll minus Haben) in Minor Units; ein Konto, das die Liste nicht führt, steht auf 0."""
    account = helpers.accounts_of(report).get(number)
    return 0 if account is None else account.closing


def balances(report):
    return {key: account.closing for key, account in helpers.accounts_of(report).items() if isinstance(key, int)}


def minor(euro):
    return helpers.minor(euro)


# ═════════════════════════════════════════════════════════════════════════════
# EÜR
# ═════════════════════════════════════════════════════════════════════════════

def test_the_euer_shows_the_revenue_net_per_rate_and_the_tax_on_the_virtual_3806(db):
    mixed_world(db)

    assert helpers.euer_values(db, ('2025-04-01', '2025-04-30')) == {4400: minor('57.97'), 4300: minor('28.98'), 3806: minor('13.05')}
    assert helpers.euer_values(db, ('2025-05-01', '2025-05-31')) == {4400: minor('42.03'), 4300: minor('21.02'), 3806: minor('9.45')}
    assert helpers.euer_values(db, ('2025-01-01', '2025-12-31')) == {4400: minor('100.00'), 4300: minor('50.00'), 3806: minor('22.50')}


def test_the_forderung_alone_is_no_revenue(db):
    """Ist-Versteuerung: Erlös und Steuer entstehen erst mit der Zahlung – die Forderung (10000 an Wartekonto) steht nicht in der EÜR."""
    mixed_world(db, stop_after=0)

    assert helpers.euer_values(db, ('2025-01-01', '2025-12-31')) == {}


def test_the_accounts_of_the_chain_are_not_in_the_euer(db):
    mixed_world(db)

    numbers = {number for number, _name, _total in db.get_euer_data('2025-01-01', '2025-12-31')}

    assert not numbers & {10000, 4405, 4345} and {4400, 4300, 3806} <= numbers


# ═════════════════════════════════════════════════════════════════════════════
# Summen- und Saldenliste
# ═════════════════════════════════════════════════════════════════════════════

def test_the_susa_of_may_balances_and_carries_the_revenue_and_the_tax_per_rate(db):
    mixed_world(db)
    report = db.get_susa(2025, 5)

    helpers.assert_balanced(report, 'Mai')
    assert ending(report, 1800) == minor('172.50')                                  # 172,50 S = die Summe der Bankbewegungen
    assert [ending(report, number) for number in (10000, 4405, 4345)] == [0, 0, 0]
    assert ending(report, 4400) == -minor('100.00') and ending(report, 4300) == -minor('50.00')       # H
    assert ending(report, 3806) == -minor('19.00') and ending(report, 3801) == -minor('3.50')
    assert helpers.check_bank_accounts(db, report, 'Mai') == []
    assert helpers.check_erfolgskonten_against_euer(db, report, 'Mai') == []          # EÜR-Umsatzsteuer = 3801 + 3806, je Erlöskonto gleich


def test_after_the_part_payment_the_open_rest_stands_on_the_waiting_accounts_and_the_debtor(db):
    mixed_world(db, stop_after=1)
    report = db.get_susa(2025, 4)

    helpers.assert_balanced(report, 'April')
    assert ending(report, 4405) == -minor('50.01') and ending(report, 4345) == -minor('22.49')      # H: was die Zahlung noch nicht umgebucht hat
    assert ending(report, 10000) == minor('72.50')                                  # S: der offene Posten
    assert ending(report, 1800) == minor('100.00')
    assert ending(report, 4400) == -minor('57.97') and ending(report, 4300) == -minor('28.98')
    assert ending(report, 3806) == -minor('11.02') and ending(report, 3801) == -minor('2.03')
    assert helpers.check_bank_accounts(db, report, 'April') == [] and helpers.check_erfolgskonten_against_euer(db, report, 'April') == []


@pytest.mark.parametrize('month', MONTHS)
def test_every_probe_of_the_susa_holds_in_every_month(db, month):
    mixed_world(db)
    report = db.get_susa(2025, month)

    helpers.assert_balanced(report, month)
    assert helpers.check_bank_accounts(db, report, month) == []
    assert helpers.check_erfolgskonten_against_euer(db, report, month) == []


# ═════════════════════════════════════════════════════════════════════════════
# DATEV
# ═════════════════════════════════════════════════════════════════════════════

def datev_rows(db, *numbers, date_from='2025-01-01', date_to='2025-12-31'):
    """Die Zeilen des Stapels mit diesen Belegfeld-1-Nummern als Felder (Liste von Listen), in der Reihenfolge des Stapels."""
    omitted = db.duplicate_transfer_rows()
    rows = [row for row in db.fetch_bookings_range(date_from, date_to) if row[0] not in omitted]
    consultant_no, client_no = db.get_datev_numbers()
    csv_bytes, _ids = datev.export_to_datev(rows, db.get_coa_id_to_number_map(), date_from, date_to, liquid_coa_ids=db.get_payment_coa_ids(),
                                            waiting_numbers=db.get_waiting_account_numbers(), invoices=db.get_datev_invoice_data(),
                                            consultant_no=consultant_no, client_no=client_no)
    lines = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]
    fields = [next(csv.reader([line], delimiter=';', quotechar='"')) for line in lines]
    return [row for row in fields if row[10] in numbers]


def is_settlement(row):
    """„Bank an Debitoren“: Konto 1800 und Gegenkonto 10000 (in einer der beiden Schreibweisen)."""
    return {row[6], row[7]} == {'1800', '10000'}


def shown(rows):
    """Je Zeile: (Konto, Gegenkonto, S/H, Umsatz, Belegdatum, Spalte 40, Spalte 114)."""
    return [(row[6], row[7], row[1], row[0], row[9], row[40], row[114]) for row in rows]


def test_the_datev_batch_carries_the_chain_per_row_with_the_rate_in_column_40(db):
    mixed_world(db)

    rows = datev_rows(db, NUMBER)

    assert len(rows) == 8                                                            # 2 Forderungen, 2 × („Bank an Debitoren“ und 2 Umbuchungen)
    assert {row[8] for row in rows} == {'0'}                                         # die Rechnungskette trägt keinen BU-Schlüssel
    settlements = [row for row in rows if is_settlement(row)]
    assert sorted(row[0] for row in settlements) == ['100,00', '72,50'] and {(row[40], row[114]) for row in settlements} == {('', '')}
    chain = [row for row in rows if not is_settlement(row)]
    receivables = [row for row in chain if row[114]]
    assert sorted((row[40], row[114], row[0]) for row in receivables) == [('19,00', '03032025', '119,00'), ('7,00', '03032025', '53,50')]
    transfers = [row for row in chain if not row[114]]
    assert sorted((row[40], row[0]) for row in transfers) == sorted([('19,00', '68,99'), ('7,00', '31,01'), ('19,00', '50,01'), ('7,00', '22,49')])


def test_the_datev_rows_of_the_mixed_invoice_equal_those_of_two_invoices_with_one_rate(db, tmp_path):
    """Die Kette ist je Zeile die Summe zweier Belege mit einem Satz: dieselben Zeilen (Konten, S/H, Umsatz, Datum, Spalten 40 und 114) – bis auf „Bank an
    Debitoren“, das der Beleg mit einem Satz je Rechnung bucht (zusammen derselbe Betrag je Zahlung)."""
    other = second_db(tmp_path)
    mixed_world(db)
    single_rate_world(other)
    mixed_rows, single_rows = datev_rows(db, NUMBER), datev_rows(other, 'E-19', 'E-07')

    assert sorted(shown([row for row in mixed_rows if not is_settlement(row)])) == sorted(shown([row for row in single_rows if not is_settlement(row)]))
    paid = lambda rows, day: sum(to_minor(D(row[0].replace(',', '.'))) for row in rows if is_settlement(row) and row[9] == day)
    assert [paid(mixed_rows, day) for day in ('0204', '0505')] == [paid(single_rows, day) for day in ('0204', '0505')] == [minor('100.00'), minor('72.50')]


# ═════════════════════════════════════════════════════════════════════════════
# Liquidität, Dashboard, Prüfliste
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('month', MONTHS)
def test_the_liquidity_has_nothing_unassigned_in_any_month(db, month):
    mixed_world(db)

    for variant in ('vorjahr', 'jahr'):
        report = db.get_liquidity(2025, month, variant)

        assert set(liq.unassigned_of(report).values()) == {0}, (month, variant, liq.unassigned_of(report))
        liq.check_against_sources(db, report, (month, variant))


def test_the_dashboard_counts_the_two_payments_as_income_and_the_balance_is_the_money_received(db):
    mixed_world(db)

    april, _ = db.get_dashboard('2025-04-01', '2025-04-30')
    may, _ = db.get_dashboard('2025-05-01', '2025-05-31')
    year, monthly = db.get_dashboard('2025-01-01', '2025-12-31')

    assert (april['income'], april['transfers'], april['balance']) == (100.00, 0, 100.00)
    assert (may['income'], may['transfers'], may['balance']) == (72.50, 0, 72.50)
    assert (year['income'], year['expense'], year['private'], year['transfers'], year['balance']) == (172.50, 0, 0, 0, 172.50)
    assert year['unlinked_count'] == 0 and [month['income'] for month in monthly if month['income']] == [100.00, 72.50]


@pytest.mark.parametrize('steps', [0, 1, 2])
def test_the_payment_check_finds_nothing_after_every_step(db, steps):
    mixed_world(db, stop_after=steps)

    assert db.find_payment_inconsistencies() == []


def test_the_payment_check_finds_nothing_after_the_match_resolved_the_receivables(db):
    mixed_world(db)

    assert db.link_bank_to_entries()['resolved'] == 2
    assert db.find_payment_inconsistencies() == []


# ═════════════════════════════════════════════════════════════════════════════
# Additivität: dieselben Zahlungen auf zwei Rechnungen mit je einem Satz
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('month', MONTHS)
def test_the_balances_equal_those_of_the_same_payments_on_two_invoices_with_one_rate(db, tmp_path, month):
    other = second_db(tmp_path)
    mixed_world(db)
    single_rate_world(other)

    assert balances(db.get_susa(2025, month)) == balances(other.get_susa(2025, month))
    for span in (('2025-%02d-01' % month, '2025-%02d-28' % month), ('2025-01-01', '2025-12-31')):
        assert helpers.euer_values(db, span) == helpers.euer_values(other, span)


# ═════════════════════════════════════════════════════════════════════════════
# Zufallsbestände: nur Wege der App, feste Seeds
# ═════════════════════════════════════════════════════════════════════════════

def random_stock(db, seed, invoices=6):
    """``invoices`` Rechnungen mit 2 bis 3 Positionen aus 19 und 7 % (beide Sätze kommen vor), Rechnungstage im März und April 2025, je Rechnung 1 bis 3
    Zahlungen (Teilzahlungen, auch ein Cent; die letzte schließt, manchmal schon die erste) über ``assign_invoice_payment``, 5 bis 70 Tage nach dem
    Rechnungstag, auf beide Bankkonten. Liefert ``[(Rechnungs-ID, Nummer, Gruppen)]``."""
    rng = random.Random(seed)
    stock = []
    for number in range(invoices):
        items = [(19, f'{rng.randint(1, 4000)}.{rng.randint(0, 99):02d}'), (7, f'{rng.randint(1, 4000)}.{rng.randint(0, 99):02d}')]
        if rng.random() < 0.5:
            items.append((rng.choice([19, 7]), f'{rng.randint(1, 900)}.{rng.randint(0, 99):02d}'))
        day = datetime.date(2025, rng.choice([3, 4]), rng.randint(1, 28))
        name = f'Z{seed}-{number}'
        invoice_id = invoice_of(db, name, items, day.isoformat())
        left = to_minor(D(str(db.get_invoice_by_id(invoice_id)[38])))
        for step in range(rng.randint(1, 3)):
            if left <= 0:
                break
            if step == 2 or rng.random() < 0.4:
                amount = left                                                        # schließt die Rechnung
            elif rng.random() < 0.2:
                amount = 100                                                         # ein Cent
            else:
                amount = rng.randint(1, left // 100) * 100
            amount = min(amount, left)
            when = day + datetime.timedelta(days=rng.randint(5, 70))
            pay(db, invoice_id, from_minor(amount), when.isoformat(), account=rng.choice(['Probe-Giro', 'Probe-Zweit']))
            left -= amount
        stock.append((invoice_id, name, rate_groups([(D(rate), to_minor(D(net))) for rate, net in items])))
    return stock


def moved_per_waiting_account(db, number):
    """Was die „Wartekonto an Erlöskonto“-Sätze der Zuordnungen einer Rechnung je Wartekonto umbuchen: ``{Wartekonto: (Betrag, Steuer)}`` in Minor Units."""
    moved = {}
    with closing_connection(db) as con:
        for coa, amount, tax in con.execute(
                "SELECT c.AccountNumber, b.Amount, b.TaxAmount FROM Bookings b JOIN ChartOfAccounts c ON c.ID = b.COA_ID "
                "WHERE b.DocumentNumber = ? AND b.Status = 'invoice_payment' AND b.EntryForm = 'transfer' ORDER BY b.ID", (number,)):
            done = moved.get(coa, (0, 0))
            moved[coa] = (done[0] + amount, done[1] + (tax or 0))
    return moved


def closing_connection(db):
    return contextlib.closing(sqlite3.connect(db.db_name))


@pytest.mark.parametrize('seed', [11, 12, 13])
def test_a_random_stock_balances_in_every_month_and_the_probes_hold(db, seed):
    random_stock(db, seed)

    for month in (3, 4, 5, 6, 7, 8):
        report = db.get_susa(2025, month)
        helpers.assert_balanced(report, (seed, month))
        assert helpers.check_bank_accounts(db, report, (seed, month)) == []
        assert helpers.check_erfolgskonten_against_euer(db, report, (seed, month)) == []
        for variant in ('vorjahr', 'jahr'):
            liquidity = db.get_liquidity(2025, month, variant)
            assert set(liq.unassigned_of(liquidity).values()) == {0}, (seed, month, variant)
            liq.check_against_sources(db, liquidity, (seed, month, variant))
    assert db.find_payment_inconsistencies() == []


@pytest.mark.parametrize('seed', [11, 12, 13])
def test_every_fully_paid_invoice_has_its_gross_and_tax_per_rate_moved_to_the_revenue_accounts(db, seed):
    stock = random_stock(db, seed)
    checked = 0

    for invoice_id, name, groups in stock:
        if db.get_invoice_by_id(invoice_id)[40] != 'paid':
            continue
        wanted = {4405 if group.pct == 19 else 4345: (group.gross, group.tax) for group in groups}
        assert moved_per_waiting_account(db, name) == wanted, (seed, name)
        checked += 1

    assert checked >= 1                                                              # mit diesen Seeds ist mindestens eine Rechnung ganz bezahlt
