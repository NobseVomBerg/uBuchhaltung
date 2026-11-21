# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen für die Tests der Liquiditätsrechnung (``test_liquidity.py``, ``test_liquidity_pdf.py``): ein Bericht aus erfundenen EÜR-Zeilen ohne
Datenbank, die Gegenrechnung der Zeilen aus den Quellen (``get_euer_data``, ``get_dashboard``) und ein Bestand aus Zufall, der nur über Wege der
App entsteht. Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da (Stichtage sind feste Tage).
"""
import datetime
import random
from decimal import ROUND_HALF_UP, Decimal

from db.bwa import build_report, bwa_periods, group_of, periods_for
from db.liquidity import CashFigures, build_liquidity
from db.reporting import bank_skr_numbers, euer_section
from money import to_minor
from tests.susa_helpers import account_id, coa, mask, paid_invoice

ZERO = CashFigures(0, 0, 0, 0)


def reports_from(month_rows, ytd_rows=None, variant='jahr', banks=(), year=2026, month=3, prev_month_rows=(), prev_ytd_rows=(), cash=None):
    """``(Bericht der betriebswirtschaftlichen Auswertung, Liquiditätsrechnung)`` aus erfundenen EÜR-Zeilen ``(Nummer, Name, Saldo in Euro)``; ohne
    ``ytd_rows`` gilt für den aufgelaufenen Zeitraum dieselbe Liste wie für den Monat. ``cash``: ``{Zeitraum: CashFigures}`` (fehlende: alles 0)."""
    rows = {'month': list(month_rows), 'ytd': list(month_rows if ytd_rows is None else ytd_rows),
            'prev_month': list(prev_month_rows), 'prev_ytd': list(prev_ytd_rows)}
    spans = bwa_periods(year, month)
    periods = {key: spans[key] for key in periods_for(variant)}
    banks = set(banks)
    bwa_report = build_report(year, month, variant, periods, rows, banks)
    figures = {key: (cash or {}).get(key, ZERO) for key in periods}
    return bwa_report, build_liquidity(year, month, variant, periods, rows, bwa_report, banks, figures)


def liquidity_from(*args, **kwargs):
    """Die Liquiditätsrechnung aus erfundenen EÜR-Zeilen (Argumente wie ``reports_from``)."""
    return reports_from(*args, **kwargs)[1]


def two_banks(db):
    """Die zwei Bankkonten „Probe-Giro“ (1800) und „Probe-Zweit“ (1810) neben der Kasse (1600); ``(Giro, Zweit)`` als ``Accounts.ID``."""
    db.insert_account('Probe-Giro', 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    db.insert_account('Probe-Zweit', 'Ich', 'DE03', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    return account_id(db, 'Probe-Giro'), account_id(db, 'Probe-Zweit')


def values_of(report, key, period='month'):
    return report.line(key).values[period]


def account_numbers(report, key):
    return [account.number for account in report.line(key).accounts]


def where_is(report, number, period='month'):
    """Die Zeilen (außer dem Ergebnis, das aus der BWA kommt), in deren Kontennachweis ``number`` im Zeitraum mit einem Wert steht – ``{Schlüssel: Wert}``."""
    found = {}
    for line in report.lines:
        if line.key == 'result':
            continue
        for account in line.accounts:
            if account.number == number and account.values.get(period) is not None:
                found[line.key] = account.values[period]
    return found


# ── Die Zeilen aus den Quellen, unabhängig vom Aufbau in ``db.liquidity`` ───────

def rows_by_number(db, span):
    """``{Kontonummer: Minor Units}`` aus ``get_euer_data`` (die zwei Zeilen 3806 addiert) – ``to_minor`` je Zeile."""
    rows = {}
    for number, _name, total in db.get_euer_data(*span):
        rows[number] = rows.get(number, 0) + to_minor(total)
    return rows


def expected_lines(db, span):
    """Die Werte der Zeilen für eine Spanne, noch einmal gerechnet aus den Zeilen der EÜR und den Bereichen – so, wie die Rechenregel sie nennt (nicht
    mit dem Code von ``build_liquidity``): Ergebnis, Abschreibungen, Restbuchwert, Darlehen im Ergebnis, Anlagen, Darlehen, Privat, Übrige."""
    banks = bank_skr_numbers(db.fetch_accounts())
    rows = rows_by_number(db, span)
    section = {number: euer_section(number, banks) for number in rows}
    in_result = lambda number: section[number] in ('income', 'expense')
    disposal = lambda number: 4855 <= number <= 4858 or 6895 <= number <= 6898
    private = lambda number: 2100 <= number <= 2199
    expected = {
        'result': sum(value for number, value in rows.items() if in_result(number)),
        'depreciation': -sum(value for number, value in rows.items() if in_result(number) and group_of(number) == 'depreciation'),
        'disposals': -sum(value for number, value in rows.items() if in_result(number) and disposal(number)),
        'financing_in_result': -sum(value for number, value in rows.items() if in_result(number) and group_of(number) == 'liabilities'),
        'assets': sum(value for number, value in rows.items() if section[number] is None and number < 1000),
        'financing': sum(value for number, value in rows.items() if 3000 <= number <= 3799 and section[number] in ('expense', 'other')),
        'withdrawals': sum(value for number, value in rows.items() if 2100 <= number <= 2179),
        'deposits': sum(value for number, value in rows.items() if 2180 <= number <= 2199),
    }
    expected['outside_other'] = sum(
        value for number, value in rows.items()
        if (section[number] is None and number >= 1000 and not private(number))
        or (section[number] == 'other' and not 3000 <= number <= 3799))
    return expected


def check_against_sources(db, report, where=''):
    """Gegen die Quellen, je Zeitraum und auf den Minor-Wert: Die Zeilen gleich ``expected_lines``; Ergebnis, Abschreibungen und Darlehen im Ergebnis gleich
    der BWA; Einnahme + privat + Ausgabe + Übertrag gleich der Karte ``get_dashboard`` derselben Spanne; die Bankbewegungen ohne Buchungssatz nie mehr als
    ``unlinked_count`` der Karte. Die Summen und ``unassigned`` sind aus ihren Bestandteilen gerechnet."""
    bwa = db.get_bwa(report.year, report.month, report.variant)
    for period, span in report.periods.items():
        for key, value in expected_lines(db, span).items():
            assert report.line(key).values[period] == value, (where, period, span, key)
        assert report.line('result').values[period] == bwa.line('result').values[period], (where, period)
        assert report.line('depreciation').values[period] == bwa.line('depreciation').values[period], (where, period)
        assert report.line('financing_in_result').values[period] == bwa.line('liabilities').values[period], (where, period)
        values = lambda key: report.line(key).values[period]
        assert values('operating') == values('result') + values('depreciation') + values('disposals') + values('financing_in_result')
        assert values('computed') == (values('operating') + values('assets') + values('financing') + values('withdrawals')
                                      + values('deposits') + values('outside_other')), (where, period)
        assert values('computed') + values('unbooked') + values('unassigned') == values('actual'), (where, period)
        totals, _monthly = db.get_dashboard(*span)
        assert values('actual') + values('memo_transfers') == to_minor(totals['balance']) == values('memo_balance'), (where, period, span)
        assert 0 <= report.line('unbooked').counts[period] <= totals['unlinked_count'], (where, period, span)


def unassigned_of(report):
    return dict(report.line('unassigned').values)


# ── Ein Bestand aus Zufall, nur über Wege der App ───────────────────────────────

def _cents(euro):
    return Decimal(euro).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _gross_and_tax(rng, rate_percent, sign):
    """Ein Bruttobetrag in Euro (Text) und der darin enthaltene Steuerbetrag (auf den Cent), beide mit dem Vorzeichen ``sign``."""
    net = Decimal(rng.randint(500, 450_000)) / 100
    gross = _cents(net * (100 + rate_percent) / 100)
    tax = _cents(gross - net)
    return f'{sign * gross}', f'{sign * tax}'


def random_books(db, seed, size=60):
    """Ein Bestand aus Zufall (fester Seed) über 2025 und 2026, **nur über Wege der App**: Zahlungen über die Buchungsmaske mit Erlös zu 7 und 19 %, mit
    Aufwand, Erstattung, Privatentnahme und -einlage, Anlage 0500 mit Vorsteuer, Darlehen 3150 und 3160, Überträge zwischen den Bankkonten (mit Spiegel
    auf beiden Bewegungen am selben Tag, mit satzloser Gegenseite), Kassensätze mit und ohne Bankbewegung, Rechnungen mit Forderung und Zahlung (im
    selben und im Folgemonat), Bargeldabhebung (der Übertrag steht nur auf der Bankseite), Abschreibung einer Anlage und Bankbewegungen ohne
    Buchungssatz. Nichts davon ist ein Fall, den die Rechenregel nicht kennt
    (kein Satz über 1460, keine Sofortabschreibung gegen die Bank, kein Steuersatz außer 7 und 19 %, kein unstimmiger Split, kein Satz ``resolved``)."""
    rng = random.Random(seed)
    giro, zweit = two_banks(db)
    bank = lambda: rng.choice(['Probe-Giro', 'Probe-Giro', 'Probe-Zweit'])

    def day():
        return (datetime.date(2025, 1, 1) + datetime.timedelta(days=rng.randrange(0, 730))).isoformat()

    def amount(low=500, high=450_000):
        return f'{Decimal(rng.randint(low, high)) / 100:.2f}'

    invoice_number = 0
    depreciated = set()
    for _ in range(size):
        kind = rng.choice(['income', 'income', 'expense', 'expense', 'refund', 'private_out', 'private_in', 'asset', 'loan_in', 'loan_out',
                           'other_liability', 'transfer_both', 'transfer_one', 'cash_withdrawal', 'cash', 'cash_standalone', 'invoice', 'invoice',
                           'depreciation', 'unbooked', 'no_tax_income', 'no_tax_expense'])
        date = day()
        if kind == 'income':
            number, rate = rng.choice([(4400, 19), (4300, 7)])
            gross, tax = _gross_and_tax(rng, rate, 1)
            mask(db, bank(), number, gross, date, str(rate), tax)
        elif kind == 'expense':
            number, rate = rng.choice([(6815, 19), (6850, 7), (6815, 7)])
            gross, tax = _gross_and_tax(rng, rate, -1)
            mask(db, bank(), number, gross, date, str(rate), tax)
        elif kind == 'refund':
            gross, tax = _gross_and_tax(rng, 19, 1)
            mask(db, bank(), 6815, gross, date, '19', tax)
        elif kind == 'no_tax_income':
            mask(db, bank(), 4185, amount(), date)
        elif kind == 'no_tax_expense':
            mask(db, bank(), rng.choice([6805, 6850]), f'-{amount()}', date)
        elif kind == 'private_out':
            mask(db, bank(), 2100, f'-{amount()}', date)
        elif kind == 'private_in':
            mask(db, bank(), 2180, amount(), date)
        elif kind == 'asset':
            gross, tax = _gross_and_tax(rng, 19, -1)
            mask(db, bank(), 500, gross, date, '19', tax)
        elif kind == 'loan_in':
            mask(db, bank(), 3150, amount(100_000, 2_000_000), date)
        elif kind == 'loan_out':
            mask(db, bank(), 3150, f'-{amount(1_000, 100_000)}', date)
        elif kind == 'other_liability':
            mask(db, bank(), 3160, f'-{amount(1_000, 60_000)}', date)
        elif kind == 'transfer_both':
            value = amount(1_000, 300_000)
            mask(db, 'Probe-Giro', 1810, f'-{value}', date)
            mask(db, 'Probe-Zweit', 1800, value, date)
        elif kind == 'transfer_one':
            value = Decimal(amount(1_000, 300_000))
            mask(db, 'Probe-Zweit', 1800, f'-{value}', date)
            db.insert_booking(date, value, account_id=giro, booking_type='bank')
        elif kind == 'cash_withdrawal':
            mask(db, 'Probe-Giro', 1600, f'-{amount(1_000, 60_000)}', date)           # die Kassenseite führt das Kassenbuch nicht selbst: ein Übertrag, einseitig
        elif kind == 'cash':
            gross, tax = _gross_and_tax(rng, 19, -1)
            mask(db, 'Kasse', 6805, gross, date, '19', tax)
        elif kind == 'cash_standalone':
            gross, tax = _gross_and_tax(rng, 19, -1)
            db.insert_booking(date, Decimal(gross), coa_id=coa(db, 6815), counter_coa_id=coa(db, 1600), booking_type='entry', tax_rate=0.19,
                              tax_amount=Decimal(tax), document_number='KS')
        elif kind == 'invoice':
            invoice_number += 1
            issued = datetime.date.fromisoformat(date)
            paid = issued + datetime.timedelta(days=rng.choice([3, 12, 25, 40]))
            rate = rng.choice([0.19, 0.07])
            paid_invoice(db, f'R-{seed}-{invoice_number}', float(_cents(Decimal(rng.randint(5_000, 400_000)) / 100)), issued.isoformat(),
                         paid.isoformat(), rng.choice(['Probe-Giro', 'Probe-Zweit']), rate=rate)
        elif kind == 'depreciation':
            year = rng.choice([2025, 2026])
            if year not in depreciated:
                depreciated.add(year)
                asset = db.insert_asset(f'Gerät {year}', f'{year}-01-02', 1200.0, 3)
                db.book_depreciation(asset, year, coa_id_expense=coa(db, 6220), coa_id_asset=coa(db, 520))
        elif kind == 'unbooked':
            db.insert_booking(date, Decimal(rng.choice([-1, 1]) * rng.randint(1_000, 90_000)) / 100, account_id=rng.choice([giro, zweit]),
                              booking_type='bank', recipient_client='Unbekannt')
