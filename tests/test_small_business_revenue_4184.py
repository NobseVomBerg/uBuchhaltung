# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kleinunternehmer-Erlöse nach dem Datum der Bankbewegung (2.8.2): 4185 bis 31.12.2025, 4184 ab 1.1.2026.

Ein Beleg ohne Steuerausweis (§ 19 UStG, Kopfsatz −1) bucht den Erlös einer Zahlung auf das Kleinunternehmer-Konto, das für den Tag
der **Bankbewegung** gilt – nicht für den der Rechnung: Der Kontenrahmen ab 2026 kennt 4184 statt 4185. Hier steht, was dazugehört:

* die Regel selbst (``small_business_revenue_number``, ``uses_small_business_rule``) und ``resolve_revenue_coa`` – ohne Datum gilt heute,
  ein gelerntes eigenes Konto (4120) gewinnt wie immer, ein gelerntes 4184 oder 4185 zählt nicht als gelernt, Satz 0 mit Steuerausweis
  bleibt fest auf 4185;
* die Zahlung (vor und nach dem Stichtag, Rechnung 2025 – Zahlung 2026, Teilzahlungen über den Jahreswechsel), das Netz in der Transaktion
  und ein Datum, das keines ist;
* kein Konto entsteht, wo eine Aktion abgelehnt wird; ein Bestand ohne 4184 legt es bei Bedarf an; der Standard-Kontenrahmen führt es;
* die Erstattung, der Umzug der Zahlungen auf die Ersatzrechnung und „Zuordnung neu aufbauen“ über den Jahreswechsel;
* die Auswertungen (EÜR, BWA, Summen- und Saldenliste, Liquidität, DATEV, Dashboard): 4184 ist ein Erlöskonto wie 4185;
* der Demobestand.

Die Entscheidungen des Owners: Nur der Platzhalter −1 folgt der Datumsregel (``uses_small_business_rule``, Variante B – der EINE Ort, an dem sich das
ändert); die Erstattung bei Zahlungen auf beiden Konten ist die vorhandene Wahl des Nutzers; Zahlungen ab 2026, die 2.8.1 auf 4185 gebucht hat, bleiben,
wie sie sind.

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import csv
import datetime
import json
import os
from decimal import Decimal

import pytest

import db.invoice_payments as payments_module
import server.handlers as handlers
from db import datev_keys, susa
from db.bwa import group_of
from db.invoice_lock import CHANGED_MESSAGE
from db.invoice_payments import (MIXED_REVENUE_MESSAGE, PAYMENT_CHANGED_MESSAGE, PAYMENT_MARK, REVENUE_DATE_UNREADABLE,
                                 SMALL_BUSINESS_REVENUE_BEFORE, SMALL_BUSINESS_REVENUE_FROM, SMALL_BUSINESS_REVENUE_NAMES,
                                 SMALL_BUSINESS_REVENUE_NUMBERS, SMALL_BUSINESS_SWITCH, small_business_day,
                                 small_business_revenue_number, uses_small_business_rule)
from db.invoice_repair import REBUILD_SKIP_REASONS
from db.matching import FORM_PURPOSE_FIRST
from db.reporting import euer_section
from server.handlers import handle_add_transaction, handle_datev_export, link_booking_to_invoice_capped
from server.pages_dashboard import PageDashboard
from server.pages_invoice import PageInvoice
from server.pages_transactions import PageTransactions
from tests.bwa_helpers import account_numbers, values_of
from tests.invoice_world import query_rows, run_sql, set_invoice_status
from tests.liquidity_helpers import unassigned_of
from tests.test_invoice_payment_assignment import (_account, _assign, _bank, _children, _coa, _exec, _post, _raw, _revenue_account,
                                                   _sent)
from tests.test_invoice_reissue import allocations, payment_rows, reissue
from tests.test_invoice_storno import ONE_ITEM, cancel, sent, tables, world          # noqa: F401  (world: Fixture)


SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'seed_data', 'chart_of_accounts_skr04.json')


# ── Hilfen ───────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_db):
    return tmp_db


@pytest.fixture
def acct(db):
    return _account(db)


def section_19(db, number, gross=100.00, **kw):
    """Eine versendete Rechnung ohne Steuerausweis (§ 19, Kopfsatz −1) samt Forderung."""
    return _sent(db, number, gross, rate=-1, tax_amount=0, **kw)


def euer_between(db, first, last):
    return {number: round(total, 2) for number, _name, total in db.get_euer_data(first, last)}


def pay(db, acct, invoice_id, day, amount=None, **kw):
    """Eine Bankbewegung am ``day`` anlegen und der Rechnung zuordnen wie die Oberfläche (mit der Anzeige der Bankzeile); liefert die Bewegung."""
    gross = amount if amount is not None else 100.00
    bank = _bank(db, acct, gross, date=day)
    ok, error = link_booking_to_invoice_capped(db, invoice_id, bank, amount, **kw)
    assert ok, error
    return bank


def revenue_account_of(db, bank):
    """Die Kontonummer, auf der die Zahlung unter dieser Bewegung den Erlös bucht (Gegenkonto der Umbuchung „Wartekonto an Erlöskonto“)."""
    rows = [row for row in _children(db, bank) if row[0] == 4340]
    assert len(rows) == 1, rows
    return rows[0][1]


def count_accounts(db, *numbers):
    marks = ','.join('?' * len(numbers))
    return _raw(db, f'SELECT COUNT(*) FROM ChartOfAccounts WHERE AccountNumber IN ({marks})', *numbers)[0][0]


def drop_small_business_accounts(db):
    """Die Kontenrahmen älterer Bestände: weder 4184 noch 4185 (eine Bestandsdatenbank kennt 4184 nicht, und wer 4185 gelöscht hat, kennt es auch nicht mehr)."""
    _exec(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (4184, 4185)')
    assert count_accounts(db, 4184, 4185) == 0


def all_rows(db):
    """Alles, was ein Weg schreiben könnte – samt Kontenrahmen und Zählern."""
    result = {}
    for table in ('Bookings', 'InvoicePayments', 'Invoices', 'InvoiceItems', 'ChartOfAccounts', 'sqlite_sequence'):
        result[table] = _raw(db, f'SELECT * FROM {table} ORDER BY 1')
    return result


# ═══ Die Regel ═══════════════════════════════════════════════════════════════
@pytest.mark.parametrize('day, number', [('2025-12-30', 4185), ('2025-12-31', 4185), ('2026-01-01', 4184), ('2026-01-02', 4184),
                                         ('1999-01-01', 4185), ('2030-06-30', 4184), ('1900-01-01', 4185), ('2200-12-31', 4184)])
def test_the_day_of_the_bank_movement_decides_between_4185_and_4184(day, number):
    assert small_business_revenue_number(day) == number
    assert small_business_revenue_number(datetime.date.fromisoformat(day)) == number
    assert small_business_revenue_number(datetime.datetime.fromisoformat(day + 'T23:59:59')) == number       # die Uhrzeit zählt nie


def test_the_switch_and_the_accounts_are_named_in_one_place():
    assert SMALL_BUSINESS_SWITCH == datetime.date(2026, 1, 1)
    assert (SMALL_BUSINESS_REVENUE_BEFORE, SMALL_BUSINESS_REVENUE_FROM) == (4185, 4184)
    assert SMALL_BUSINESS_REVENUE_NUMBERS == {4184, 4185}
    assert SMALL_BUSINESS_REVENUE_NAMES == {4184: 'Steuerfreie Erlöse Kleinunternehmer nach § 19 Abs. 1 UStG',
                                            4185: 'Erlöse als Kleinunternehmer nach § 19 Abs. 1 UStG'}


@pytest.mark.parametrize('value', [None, '', ' ', ' 2026-01-01', '2026-01-01 ', '2026-1-1', '2026-02-30', '2026-13-01', '2026-01-01T00:00', '2026-01-01 00:00:00',
                                   '20260101', '2026/01/01', '２０２６-０１-０１', '1899-12-31', '2201-01-01', 20260101, 2026.0, True, b'2026-01-01', ['2026-01-01']])
def test_whatever_is_not_a_calendar_day_gives_no_account(value):
    """Ein Datum wird nie geraten: Davon hängt ein Konto ab."""
    assert small_business_revenue_number(value) is None


def test_only_the_placeholder_without_tax_follows_the_date_rule():
    """Variante B des Owners: Der Platzhalter −1 (§ 19) folgt der Datumsregel, ein Satz von 0 % mit Steuerausweis bleibt auf 4185. Wer das ändert, ändert diese
    EINE Funktion – und dann diese Zeilen (und die Tests mit ``rate=0``: ``test_invoice_payment_repair.py``, ``test_tax_entries_bookings.py``)."""
    assert uses_small_business_rule(-1) and uses_small_business_rule(-1.0)
    assert not any(uses_small_business_rule(rate) for rate in (0, 0.0, 0.19, 19, 0.07, None))


def test_each_account_has_a_day_on_which_the_rule_names_it():
    for number in SMALL_BUSINESS_REVENUE_NUMBERS:
        assert small_business_revenue_number(small_business_day(number)) == number
    assert small_business_day(4185) == datetime.date(2025, 12, 31) and small_business_day(4184) == datetime.date(2026, 1, 1)


# ═══ resolve_revenue_coa ═════════════════════════════════════════════════════
def test_resolve_follows_the_given_day(db):
    assert db.resolve_revenue_coa(-1, on='2025-12-31') == _coa(db, 4185)
    assert db.resolve_revenue_coa(-1, on='2026-01-01') == _coa(db, 4184)
    assert db.resolve_revenue_coa(-1, on=datetime.date(2025, 6, 30)) == _coa(db, 4185)
    assert db.resolve_revenue_coa(-1, on=datetime.date(2026, 6, 30)) == _coa(db, 4184)
    assert db.resolve_revenue_coa(-1, False, on='2026-06-30') == _coa(db, 4184)


@pytest.mark.parametrize('today, number', [(datetime.date(2025, 6, 30), 4185), (datetime.date(2026, 6, 30), 4184)])
def test_without_a_day_it_is_today(db, monkeypatch, today, number):
    """Nur für Anzeigen und Tests: Wer eine Bankbewegung hat, nennt ihr Datum. ``_revenue_today`` ist die EINE Stelle, die die Uhr liest."""
    monkeypatch.setattr(payments_module, '_revenue_today', lambda: today)
    assert db.resolve_revenue_coa(-1) == _coa(db, number)


def test_a_day_that_is_no_date_gives_no_account_and_creates_none(db):
    drop_small_business_accounts(db)
    for create in (True, False):
        assert db.resolve_revenue_coa(-1, create, on='kaputt') is None
        assert db.resolve_revenue_coa(-1, create, on='') is None
    assert count_accounts(db, 4184, 4185) == 0


def test_without_creating_it_only_looks_up(db):
    drop_small_business_accounts(db)
    assert db.resolve_revenue_coa(-1, False, on='2025-06-30') is None and db.resolve_revenue_coa(-1, False, on='2026-06-30') is None
    assert count_accounts(db, 4184, 4185) == 0


def test_a_missing_account_is_created_as_a_standard_account_with_its_name(db):
    drop_small_business_accounts(db)
    created = db.resolve_revenue_coa(-1, on='2026-03-01')
    assert created == _coa(db, 4184) and count_accounts(db, 4185) == 0                   # nur das, was gebraucht wird
    assert _raw(db, 'SELECT Framework, Name, Description, IsStandard FROM ChartOfAccounts WHERE AccountNumber = 4184') == [
        (4, 'Steuerfreie Erlöse Kleinunternehmer nach § 19 Abs. 1 UStG', 'Betriebliche Erträge', 1)]
    assert db.resolve_revenue_coa(-1, on='2025-03-01') == _coa(db, 4185)
    assert _raw(db, 'SELECT Name FROM ChartOfAccounts WHERE AccountNumber = 4185') == [('Erlöse als Kleinunternehmer nach § 19 Abs. 1 UStG',)]


def test_the_other_rates_do_not_depend_on_the_day(db):
    for day in ('2025-12-31', '2026-01-01'):
        assert db.resolve_revenue_coa(0.19, on=day) == _coa(db, 4400) and db.resolve_revenue_coa(0.07, on=day) == _coa(db, 4300)
        assert db.resolve_revenue_coa(0.05, on=day) is None


def test_a_rate_of_0_percent_with_tax_stays_on_4185_in_every_year(db):
    """Variante B: nur der Platzhalter −1 folgt dem Datum."""
    assert db.resolve_revenue_coa(0, on='2026-06-30') == _coa(db, 4185) and db.resolve_revenue_coa(0, on='2025-06-30') == _coa(db, 4185)
    assert db.resolve_revenue_coa(0.0) == _coa(db, 4185)


# ═══ Gelerntes Konto gegen die Datumsregel ═══════════════════════════════════
def teach(db, bank, number):
    """Der Nutzer ändert im Editor das Erlöskonto der Umbuchung „Wartekonto an Erlöskonto“ unter der Bewegung (das lernt die App)."""
    _exec(db, "UPDATE Bookings SET CounterCOA_ID = ? WHERE ParentBooking_ID = ? AND EntryForm = 'transfer'", _coa(db, number), bank)


def test_a_learned_account_of_the_user_stays_in_both_years(db, acct):
    """4120 steht dort, weil der Nutzer es gewählt hat – das gilt für 2025 wie für 2026."""
    _revenue_account(db, 4120, 'Erlöse eigener Art')
    teach(db, pay(db, acct, section_19(db, 'R-1'), '2025-12-20'), 4120)
    for number, day in (('R-2', '2025-12-28'), ('R-3', '2026-01-05'), ('R-4', '2026-08-05')):
        assert revenue_account_of(db, pay(db, acct, section_19(db, number), day)) == 4120, day
    assert db.resolve_revenue_coa(-1, on='2025-01-01') == db.resolve_revenue_coa(-1, on='2026-01-01') == _coa(db, 4120)


def test_a_learned_4185_does_not_stay_in_2026_and_a_learned_4184_does_not_go_back_to_2025(db, acct):
    """Für den Topf von „0 und § 19“ gibt es keine Wahl des Nutzers: 4184 und 4185 stehen dort, weil die App sie selbst als Standard gebucht hat."""
    assert revenue_account_of(db, pay(db, acct, section_19(db, 'R-1'), '2025-12-20')) == 4185
    assert revenue_account_of(db, pay(db, acct, section_19(db, 'R-2'), '2026-01-05')) == 4184           # gelernt war 4185
    assert revenue_account_of(db, pay(db, acct, section_19(db, 'R-3'), '2025-11-05')) == 4185           # gelernt ist jetzt 4184
    assert revenue_account_of(db, pay(db, acct, section_19(db, 'R-4'), '2026-02-05')) == 4184


def test_a_learned_4184_of_a_section_19_payment_does_not_color_a_rate_of_zero(db, acct):
    """Satz 0 mit Steuerausweis teilt den Topf der gelernten Konten, ist aber kein Beleg nach § 19: Ein gelerntes 4184 gilt für ihn nicht – er bleibt auf 4185."""
    assert revenue_account_of(db, pay(db, acct, section_19(db, 'R-1'), '2026-02-05')) == 4184
    zero = _sent(db, 'R-0', 100.00, rate=0)
    assert revenue_account_of(db, pay(db, acct, zero, '2026-03-05')) == 4185
    assert revenue_account_of(db, pay(db, acct, section_19(db, 'R-2'), '2026-04-05')) == 4184            # das gelernte 4185 des Satzes 0 gilt für § 19 nicht


def test_a_rate_of_zero_follows_a_learned_account_of_the_user_like_section_19(db, acct):
    _revenue_account(db, 4120, 'Erlöse eigener Art')
    teach(db, pay(db, acct, section_19(db, 'R-1'), '2026-02-05'), 4120)
    assert revenue_account_of(db, pay(db, acct, _sent(db, 'R-0', 100.00, rate=0), '2026-03-05')) == 4120


# ═══ Die Zahlung ═════════════════════════════════════════════════════════════
@pytest.mark.parametrize('day, number', [('2025-12-30', 4185), ('2025-12-31', 4185), ('2026-01-01', 4184), ('2026-01-02', 4184)])
def test_a_payment_books_on_the_account_of_the_day_of_the_bank_movement(db, acct, day, number):
    invoice = section_19(db, 'R-19')
    bank = _bank(db, acct, 100.00, date=day)
    result = db.assign_invoice_payment(invoice, bank)
    assert result.ok and result.revenue_coa_id == _coa(db, number)
    assert _children(db, bank) == [(1800, 10000, Decimal('100'), None, 'Zahlung zu Re. R-19', PAYMENT_MARK),
                                   (4340, number, Decimal('100'), None, 'Umb. zu Re. R-19', PAYMENT_MARK)]
    year = day[:4]
    assert euer_between(db, f'{year}-01-01', f'{year}-12-31') == {number: 100.0}
    assert db.find_payment_inconsistencies() == []


def test_the_bank_row_shows_the_account_of_the_day(db, acct):
    for day, number in (('2025-12-31', 4185), ('2026-01-01', 4184)):
        bank = pay(db, acct, section_19(db, f'R-{day}'), day)
        assert db.get_booking_by_id(bank)[8] == _coa(db, number)


def test_an_invoice_of_2025_paid_in_2026_books_on_4184(db, acct):
    invoice = section_19(db, 'R-DEC', invoice_date='2025-12-19')
    bank = pay(db, acct, invoice, '2026-01-14')
    assert revenue_account_of(db, bank) == 4184
    assert euer_between(db, '2025-01-01', '2025-12-31') == {} and euer_between(db, '2026-01-01', '2026-12-31') == {4184: 100.0}


def test_part_payments_across_the_new_year_use_both_accounts(db, acct):
    invoice = section_19(db, 'R-TP')
    first = pay(db, acct, invoice, '2025-12-20', 40.00)
    second = pay(db, acct, invoice, '2026-01-10', 60.00)
    assert (revenue_account_of(db, first), revenue_account_of(db, second)) == (4185, 4184)
    assert _raw(db, 'SELECT Status FROM Invoices WHERE ID = ?', invoice) == [('paid',)]
    assert db.find_payment_inconsistencies() == []
    assert euer_between(db, '2025-01-01', '2025-12-31') == {4185: 40.0} and euer_between(db, '2026-01-01', '2026-12-31') == {4184: 60.0}


def test_the_account_follows_the_date_of_the_movement_not_of_the_invoice_payment_date(db, acct):
    invoice = section_19(db, 'R-PD')
    bank = _bank(db, acct, 100.00, date='2025-12-31')
    _assign(db, invoice, bank)
    assert _raw(db, 'SELECT PaymentDate FROM InvoicePayments') == [('2025-12-31',)] and revenue_account_of(db, bank) == 4185


def test_a_payment_without_a_forderung_books_the_single_row_on_the_account_of_the_day(db, acct):
    invoice = _sent(db, 'R-NF', 100.00, rate=-1, tax_amount=0)
    _exec(db, 'DELETE FROM Bookings WHERE DocumentNumber = ?', 'R-NF')                          # Bestand vor den Forderungsbuchungen
    bank = _bank(db, acct, 100.00, date='2026-02-02')
    _assign(db, invoice, bank)
    assert [(row[0], row[1]) for row in _children(db, bank)] == [(4184, 1800)]


# ═══ Ein Datum, das keines ist ═══════════════════════════════════════════════
def test_an_unreadable_date_of_the_bank_movement_refuses_before_any_account_is_created(db, acct):
    invoice = section_19(db, 'R-BAD')
    bank = _bank(db, acct, 100.00, date='2026-02-02')
    _exec(db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', 'kaputt', bank)
    drop_small_business_accounts(db)
    before = all_rows(db)
    result = db.assign_invoice_payment(invoice, bank)
    assert (result.ok, result.message, result.payment_id) == (False, REVENUE_DATE_UNREADABLE, None)
    assert all_rows(db) == before and count_accounts(db, 4184, 4185) == 0


def test_an_unreadable_date_does_not_matter_for_a_rate_with_tax(db, acct):
    invoice = _sent(db, 'R-19P', 119.00)
    bank = _bank(db, acct, 119.00, date='2026-02-02')
    _exec(db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', 'kaputt', bank)
    assert db.assign_invoice_payment(invoice, bank).ok


def test_the_mask_checks_the_date_of_the_new_movement_before_it_creates_it(db, acct):
    invoice = section_19(db, 'R-MASK')
    drop_small_business_accounts(db)
    before = all_rows(db)
    status, message = handle_add_transaction(db, _post(invoice_id=invoice, account=acct, amount='100.00', date=''))
    assert (status, message) == (400, REVENUE_DATE_UNREADABLE)
    assert all_rows(db) == before


def test_the_mask_passes_the_date_of_the_new_movement_to_the_account(db, acct):
    drop_small_business_accounts(db)
    for number, day, expected in (('R-A', '2025-12-31', 4185), ('R-B', '2026-01-01', 4184)):
        invoice = section_19(db, number)
        status, location = handle_add_transaction(db, _post(invoice_id=invoice, account=acct, amount='100.00', date=day))
        assert status == 303 and location == f'/invoice/edit?id={invoice}', (status, location)
        assert revenue_account_of(db, _raw(db, 'SELECT ID FROM Bookings WHERE BookingType = ? AND DateBooking = ?', 'bank', day)[0][0]) == expected


def test_the_precheck_without_a_day_uses_today(db, acct, monkeypatch):
    """Ohne ``on`` (nur Tests) gilt heute; der Kontext bindet nichts."""
    invoice = section_19(db, 'R-TODAY')
    monkeypatch.setattr(payments_module, '_revenue_today', lambda: datetime.date(2025, 6, 30))
    error, context = db._payment_prerequisites(invoice, acct)
    assert error is None and context['revenue_coa'] == _coa(db, 4185) and 'revenue_date_number' not in context
    error, context = db._payment_prerequisites(invoice, acct, on='2026-02-02')
    assert error is None and context['revenue_coa'] == _coa(db, 4184) and context['revenue_date_number'] == 4184


# ═══ Das Netz in der Transaktion ═════════════════════════════════════════════
def test_a_date_that_moves_across_the_switch_between_the_precheck_and_the_transaction_refuses(db, acct, monkeypatch):
    invoice = section_19(db, 'R-NET')
    bank = _bank(db, acct, 100.00, date='2025-12-31')
    original = type(db)._payment_prerequisites

    def moved(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        _exec(self, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', '2026-01-01', bank)           # ein zweites Fenster ändert das Datum
        return result

    monkeypatch.setattr(type(db), '_payment_prerequisites', moved)
    before = all_rows(db)
    result = db.assign_invoice_payment(invoice, bank)
    assert (result.ok, result.message) == (False, PAYMENT_CHANGED_MESSAGE)
    assert {key: rows for key, rows in all_rows(db).items() if key != 'Bookings'} == {key: rows for key, rows in before.items() if key != 'Bookings'}
    assert _children(db, bank) == []


def test_a_context_built_by_hand_without_the_key_binds_nothing(db, acct):
    invoice = section_19(db, 'R-HAND')
    bank = _bank(db, acct, 100.00, date='2026-02-02')
    _error, context = db._payment_prerequisites(invoice, acct, on='2025-12-31')           # aufgelöst für 4185 …
    context = {key: value for key, value in context.items() if key != 'revenue_date_number'}
    with db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        assert db._assign_in_tx(tx, invoice, bank, None, context).ok                       # … und ohne den Schlüssel nicht geprüft
    assert revenue_account_of(db, bank) == 4185


# ═══ Der Standard-Kontenrahmen ═══════════════════════════════════════════════
def test_the_seed_file_names_4184_before_4185_as_a_standard_account():
    with open(SEED, encoding='utf-8') as handle:
        seed = json.load(handle)['accounts']
    numbers = [account['account_number'] for account in seed]
    assert numbers.index(4184) == numbers.index(4185) - 1 and numbers == sorted(numbers)
    entry = next(account for account in seed if account['account_number'] == 4184)
    assert (entry['name'], entry['description'], entry['is_standard']) == (
        'Steuerfreie Erlöse Kleinunternehmer nach § 19 Abs. 1 UStG', 'Betriebliche Erträge', True)
    assert next(account for account in seed if account['account_number'] == 4185)['name'] == SMALL_BUSINESS_REVENUE_NAMES[4185]


def test_a_new_database_has_both_accounts_with_the_ids_of_the_standard_frame(db):
    assert _coa(db, 4184) == 404184 and _coa(db, 4185) == 404185
    assert db.missing_standard_accounts() == []


def test_an_older_database_without_4184_names_it_and_adds_it_on_request(db):
    _exec(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4184')
    assert db.missing_standard_accounts() == [(4184, SMALL_BUSINESS_REVENUE_NAMES[4184])]
    assert db.add_missing_standard_accounts() == 1
    assert _coa(db, 4184) == 404184 and db.missing_standard_accounts() == []


def test_the_first_2026_payment_of_an_older_database_creates_4184_and_nothing_else(db, acct):
    _exec(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4184')
    accounts_before = _raw(db, 'SELECT COUNT(*) FROM ChartOfAccounts')[0][0]
    bank = pay(db, acct, section_19(db, 'R-OLD'), '2026-01-14')
    assert _raw(db, 'SELECT COUNT(*) FROM ChartOfAccounts')[0][0] == accounts_before + 1
    assert revenue_account_of(db, bank) == 4184 and db.missing_standard_accounts() == []
    assert _raw(db, 'SELECT IsStandard, Framework FROM ChartOfAccounts WHERE AccountNumber = 4184') == [(1, 4)]


def test_a_database_that_only_knows_2026_gets_no_4185(db, acct):
    drop_small_business_accounts(db)
    bank = pay(db, acct, section_19(db, 'R-NEW'), '2026-03-01')
    assert revenue_account_of(db, bank) == 4184 and count_accounts(db, 4185) == 0


# ═══ Abgelehnte Aktionen legen kein Konto an ═════════════════════════════════
def test_an_outgoing_or_an_exhausted_movement_is_refused_before_any_account_is_created(db, acct):
    """Beide Ablehnungen kamen bisher erst in der Transaktion – nach den Vorbedingungen, die das Erlöskonto anlegen."""
    from db.invoice_payments import INCOMING_ONLY_MESSAGE, NOTHING_TO_ASSIGN_MESSAGE
    invoice, other = section_19(db, 'R-OUT'), section_19(db, 'R-USED')
    outgoing = _bank(db, acct, -100.00, date='2026-02-04')
    used = pay(db, acct, other, '2025-11-05')                       # bucht auf 4185, die Bewegung ist danach ausgeschöpft
    _exec(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4184')
    before = all_rows(db)
    refused = [db.assign_invoice_payment(invoice, outgoing), db.assign_invoice_payment(invoice, used)]
    assert [result.ok for result in refused] == [False, False]
    assert [result.message for result in refused] == [INCOMING_ONLY_MESSAGE, NOTHING_TO_ASSIGN_MESSAGE]
    assert all_rows(db) == before and count_accounts(db, 4184) == 0


def test_a_refused_payment_creates_no_account(db, acct):
    drop_small_business_accounts(db)
    no_skr = _account(db, name='Ohne Konto im Rahmen', skr=None)
    draft = _sent(db, 'R-DRAFT', 100.00, rate=-1, tax_amount=0, status='draft')
    invoice = section_19(db, 'R-OK')
    linked = section_19(db, 'R-LINKED')
    bank = _bank(db, acct, 100.00, date='2026-02-02')
    other_bank = _bank(db, no_skr, 100.00, date='2026-02-03')
    db.link_invoice_to_transaction(linked, bank, 100.00)             # ein Weg ohne Sätze: Das Paar steht schon da
    before = all_rows(db)
    cases = [
        ('linked', db.assign_invoice_payment(linked, bank)),
        ('not sent', db.assign_invoice_payment(draft, other_bank)),
        ('no SKR number', db.assign_invoice_payment(invoice, other_bank)),
        ('amount zero', db.assign_invoice_payment(invoice, bank, 0)),
        ('amount negative', db.assign_invoice_payment(invoice, bank, -5)),
    ]
    for name, result in cases:
        assert result.ok is False and result.message, name
    assert [result.message for _name, result in cases][-2:] == ['Der Betrag muss größer 0 sein.'] * 2
    assert all_rows(db) == before and count_accounts(db, 4184, 4185) == 0


def test_the_page_of_a_payment_form_for_a_section_19_invoice_creates_no_account(db, acct):
    invoice = section_19(db, 'R-PAGE')
    drop_small_business_accounts(db)
    before = all_rows(db)
    html = PageTransactions(db, from_invoice=invoice)
    assert 'name="revenue_coa_id"' not in html and all_rows(db) == before


def test_a_precheck_that_refuses_creates_no_account(db, acct):
    drop_small_business_accounts(db)
    invoice = section_19(db, 'R-PRE')
    before = all_rows(db)
    assert db.payment_precheck(invoice, acct, on='kein Datum') == REVENUE_DATE_UNREADABLE
    assert db.payment_precheck(invoice, None, on='2026-01-01') == 'Für die Zahlung fehlt das Bankkonto.'
    assert all_rows(db) == before


def test_a_precheck_creates_the_account_of_the_day_like_the_payment_does(db, acct):
    """Wie bisher mit 4185: Die Vorprüfung löst auf und legt dabei an, was die Zahlung braucht (in einer eigenen Verbindung, nie in einer Transaktion)."""
    drop_small_business_accounts(db)
    invoice = section_19(db, 'R-PRE')
    assert db.payment_precheck(invoice, acct, on='2026-01-01') is None
    assert count_accounts(db, 4184) == 1 and count_accounts(db, 4185) == 0


# ═══ Die Welt der Stornorechnung: Erstattung und Umzug ═══════════════════════

SECTION_19_ITEMS = [dict(ONE_ITEM[0], taxRate=0)]              # 100,00 € ohne Steuer


def section_19_sent(w, number='T-300', **over):
    """Eine versendete Rechnung ohne Steuerausweis über 100,00 €, samt Forderung und Dokument."""
    return sent(w, number, items=SECTION_19_ITEMS, taxRate=0, showTax=False, **over)


def section_19_of_2025(w, number='T-300'):
    """Dieselbe Rechnung, ausgestellt im November 2025."""
    return section_19_sent(w, number, invoiceDate='2025-11-10', deliveryDate='2025-11-10', dueDate='2025-11-24')


def pay_on(w, invoice_id, amount, day):
    """Eine Zahlung am ``day`` zuordnen, wie die Oberfläche es tut; liefert die Bankbewegung."""
    booking = w.db.insert_booking(day, amount, booking_type='bank', account_id=w.bank, recipient_client='Malermeister Pinsel')
    ok, error = handlers.link_booking_to_invoice_capped(w.db, invoice_id, booking, amount)
    assert ok, error
    return booking


def outgoing_on(w, amount, day):
    return w.db.insert_booking(day, -amount, booking_type='bank', account_id=w.bank, recipient_client='Malermeister Pinsel')


def import_style_payment(w, invoice_id, amount, day):
    """Eine Zahlung ohne Buchungssätze (so ordnet der WISO-Import zu)."""
    booking = w.db.insert_booking(day, amount, booking_type='bank', account_id=w.bank, recipient_client='Malermeister Pinsel')
    w.db.link_invoice_to_transaction(invoice_id, booking, amount)
    return booking


def paid_in_2025_and_2026(w):
    """Die Rechnung vom November 2025, 40,00 € am 20.12.2025 (4185) und 60,00 € am 10.01.2026 (4184) bezahlt, am 20.01.2026 storniert."""
    r1 = section_19_of_2025(w)
    first, second = pay_on(w, r1, 40.0, '2025-12-20'), pay_on(w, r1, 60.0, '2026-01-10')
    return r1, cancel(w, r1, '2026-01-20').storno_id, first, second


def rows_under(db, bank):
    """Die Sätze unter der Bewegung: (Konto, Gegenkonto, Betrag in Minor Units)."""
    return [(row['coa'], row['counter'], row['Amount']) for row in query_rows(
        db, 'SELECT c.AccountNumber AS coa, g.AccountNumber AS counter, b.Amount FROM Bookings b '
            'LEFT JOIN ChartOfAccounts c ON c.ID = b.COA_ID LEFT JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID '
            'WHERE b.ParentBooking_ID = ? ORDER BY b.ID', bank)]


# ── Erstattung ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize('paid_on, refunded_on, number', [('2025-12-20', '2026-01-25', 4185), ('2026-01-05', '2026-01-25', 4184),
                                                         ('2025-11-20', '2025-12-28', 4185), ('2026-01-05', '2026-02-25', 4184)],
                         ids=['Zahlung-2025-Erstattung-2026', 'beides-2026', 'beides-2025', 'Zahlung-Januar-Erstattung-Februar'])
def test_a_refund_takes_the_revenue_back_where_the_payment_booked_it(world, paid_on, refunded_on, number):
    """Die Sätze der Zahlung nennen das Erlöskonto: Die Erstattung nimmt den Erlös dort zurück – egal, in welchem Jahr sie liegt."""
    db = world.db
    r1 = section_19_of_2025(world)
    booking = pay_on(world, r1, 100.0, paid_on)
    assert revenue_account_of(db, booking) == (4185 if paid_on < '2026' else 4184)
    storno = cancel(world, r1, max(paid_on, '2025-12-01')).storno_id
    out = outgoing_on(world, 100.0, refunded_on)

    result = db.assign_storno_refund(storno, out)

    assert result.ok and result.revenue_coa_id == _coa(db, number)
    assert rows_under(db, out) == [(1800, 10000, -1000000), (4340, number, -1000000)]
    assert euer_between(db, refunded_on, refunded_on) == {number: -100.0}
    assert db.find_payment_inconsistencies() == []


def test_payments_on_both_accounts_leave_the_choice_of_the_refund_to_the_user(world):
    """Eine Teilzahlung über den Jahreswechsel: zwei Erlöskonten – dann gilt die vorhandene Wahl des Nutzers, keine neue Regel."""
    db = world.db
    r1, storno, first, second = paid_in_2025_and_2026(world)
    out = outgoing_on(world, 100.0, '2026-02-02')
    assert db.refund_revenue_coas_differ(storno) is True
    before = tables(db)

    refused = db.assign_storno_refund(storno, out)
    assert (refused.ok, refused.message) == (False, MIXED_REVENUE_MESSAGE) and tables(db) == before
    chosen = db.assign_storno_refund(storno, out, None, _coa(db, 4184))

    assert chosen.ok and chosen.revenue_coa_id == _coa(db, 4184)
    assert rows_under(db, out) == [(1800, 10000, -1000000), (4340, 4184, -1000000)]
    assert db.find_payment_inconsistencies() == []


@pytest.mark.parametrize('refunded_on, number', [('2025-12-28', 4185), ('2026-01-25', 4184)])
def test_a_refund_without_marked_rows_follows_the_date_of_the_refund(world, refunded_on, number):
    """Hat keine Zuordnung markierte Sätze (Import), gilt das Erlöskonto für das Datum der Erstattung."""
    db = world.db
    r1 = section_19_of_2025(world)
    import_style_payment(world, r1, 100.0, '2025-11-20')
    storno = cancel(world, r1, '2025-12-01').storno_id
    out = outgoing_on(world, 100.0, refunded_on)

    result = db.assign_storno_refund(storno, out)

    assert result.ok and result.revenue_coa_id == _coa(db, number)
    assert rows_under(db, out) == [(1800, 10000, -1000000), (4340, number, -1000000)]


def test_a_refund_without_marked_rows_and_an_unreadable_date_refuses_and_creates_no_account(world):
    db = world.db
    r1 = section_19_of_2025(world)
    import_style_payment(world, r1, 100.0, '2025-11-20')
    storno = cancel(world, r1, '2025-12-01').storno_id
    out = outgoing_on(world, 100.0, '2026-01-25')
    run_sql(db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', 'kaputt', out)
    run_sql(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (4184, 4185)')
    before = tables(db)

    result = db.assign_storno_refund(storno, out)

    assert (result.ok, result.message) == (False, REVENUE_DATE_UNREADABLE) and tables(db) == before
    assert count_accounts(db, 4184, 4185) == 0


def test_a_refund_whose_date_crosses_the_switch_before_the_transaction_refuses(world, monkeypatch):
    db = world.db
    r1 = section_19_of_2025(world)
    import_style_payment(world, r1, 100.0, '2025-11-20')
    storno = cancel(world, r1, '2025-12-01').storno_id
    out = outgoing_on(world, 100.0, '2025-12-31')
    original = type(db)._document_revenue

    def moved(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        run_sql(self, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', '2026-01-01', out)
        return result

    monkeypatch.setattr(type(db), '_document_revenue', moved)
    before = tables(db)
    result = db.assign_storno_refund(storno, out)
    assert (result.ok, result.message) == (False, 'Rechnung oder Bankbewegung wurde inzwischen geändert – bitte erneut versuchen.')
    assert {key: rows for key, rows in tables(db).items() if key != 'Bookings'} == {key: rows for key, rows in before.items() if key != 'Bookings'}


def test_a_refused_refund_creates_no_account(world):
    db = world.db
    r1, storno, first, second = paid_in_2025_and_2026(world)
    run_sql(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (4184, 4185)')
    before = tables(db)
    inbound = db.insert_booking('2026-02-02', 100.0, booking_type='bank', account_id=world.bank)
    result = db.assign_storno_refund(storno, inbound)
    assert (result.ok, result.message) == (False, 'Nur Zahlungsausgänge lassen sich als Erstattung zuordnen.')
    assert count_accounts(db, 4184, 4185) == 0 and tables(db)['ChartOfAccounts'] == before['ChartOfAccounts']


# ── Umzug der Zahlungen auf die Ersatzrechnung ───────────────────────────────
USER = 'anna'
REASON_REVENUE_ACCOUNT = 'das Erlöskonto der Zahlung ist nicht das für Steuersatz und Zahlungsdatum hinterlegte'
REASON_REVENUE_DATE = 'zum Datum der Bankbewegung lässt sich kein Erlöskonto bestimmen'


def replaced_scenario(w, monkeypatch, *payments, invoice=section_19_of_2025):
    """Die Rechnung vom November 2025 ohne Steuerausweis, je (Betrag, Tag) bezahlt, am 20.01.2026 storniert, die Ersatzrechnung als Entwurf."""
    r1 = invoice(w)
    banks = [pay_on(w, r1, amount, day) for amount, day in payments]
    cancel(w, r1, '2026-01-20')
    return r1, reissue(w.db, r1, monkeypatch), banks


def number_of(db, invoice_id):
    return query_rows(db, 'SELECT InvoiceNumber FROM Invoices WHERE ID = ?', invoice_id)[0]['InvoiceNumber']


@pytest.mark.parametrize('day, number', [('2025-12-20', 4185), ('2026-01-14', 4184)])
def test_a_payment_moves_to_the_replacement_on_the_account_of_its_own_day(world, monkeypatch, day, number):
    """Zahlung 2025: zieht mit ihrem Datum auf 4185 um, die EÜR bleibt, wie sie war – Zahlung 2026: auf 4184."""
    db = world.db
    r1, r2, (bank,) = replaced_scenario(world, monkeypatch, (50.0, day))
    assert revenue_account_of(db, bank) == number
    euer_before = (euer_between(db, '2025-01-01', '2025-12-31'), euer_between(db, '2026-01-01', '2026-12-31'))

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200 and 'sind jetzt dieser Rechnung zugeordnet' in body['hint'], body
    assert allocations(db, r1) == [] and allocations(db, r2) == [(bank, 500000, day)]
    assert [(row[0], row[1], row[4]) for row in payment_rows(db, number_of(db, r2))] == [(1800, 10000, day), (4340, number, day)]
    assert (euer_between(db, '2025-01-01', '2025-12-31'), euer_between(db, '2026-01-01', '2026-12-31')) == euer_before
    assert db.find_payment_inconsistencies() == []


def test_a_payment_of_each_year_moves_on_its_own_account(world, monkeypatch):
    db = world.db
    r1, r2, (first, second) = replaced_scenario(world, monkeypatch, (40.0, '2025-12-20'), (60.0, '2026-01-10'))

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200 and allocations(db, r1) == [] and allocations(db, r2) == [(first, 400000, '2025-12-20'), (second, 600000, '2026-01-10')]
    assert [(row[0], row[1], row[4]) for row in payment_rows(db, number_of(db, r2))] == [
        (1800, 10000, '2025-12-20'), (4340, 4185, '2025-12-20'), (1800, 10000, '2026-01-10'), (4340, 4184, '2026-01-10')]
    assert euer_between(db, '2025-01-01', '2025-12-31') == {4185: 40.0} and euer_between(db, '2026-01-01', '2026-12-31') == {4184: 60.0}
    assert db.find_payment_inconsistencies() == []


def test_a_payment_of_2026_that_version_2_8_1_booked_on_4185_stays_at_the_cancelled_invoice(world, monkeypatch):
    """Der Umzug verschöbe sonst Erlös im vergangenen Zeitraum: Er läuft nur, wenn die Zahlung schon auf dem Konto liegt, das für ihr Datum gilt."""
    db = world.db
    r1, r2, (bank,) = replaced_scenario(world, monkeypatch, (50.0, '2026-01-14'))
    run_sql(db, "UPDATE Bookings SET CounterCOA_ID = ? WHERE ParentBooking_ID = ? AND EntryForm = 'transfer'", _coa(db, 4185), bank)    # der Stand von 2.8.1
    rows_before = rows_under(db, bank)

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200
    assert f'{REASON_REVENUE_ACCOUNT}.' in body['hint'] and 'ließen sich nicht automatisch übernehmen' in body['hint'], body
    assert allocations(db, r1) == [(bank, 500000, '2026-01-14')] and allocations(db, r2) == []
    assert rows_under(db, bank) == rows_before and euer_between(db, '2026-01-01', '2026-12-31') == {4185: 50.0}


def test_a_payment_with_an_unreadable_date_stays_with_its_own_reason(world, monkeypatch):
    db = world.db
    r1, r2, (bank,) = replaced_scenario(world, monkeypatch, (50.0, '2026-01-14'))
    run_sql(db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ? OR ParentBooking_ID = ?', 'kaputt', bank, bank)

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200 and f'{REASON_REVENUE_DATE}.' in body['hint'], body
    assert allocations(db, r1) == [(bank, 500000, '2026-01-14')] and allocations(db, r2) == []


def test_a_database_that_only_knows_2026_gets_no_4185_from_the_move(world, monkeypatch):
    """Angelegt wird nur, was eine Zahlung braucht: Der Umzugskontext löst je Konto auf, das eine Zahlung der stornierten Rechnung nennt."""
    db = world.db
    r1, r2, (bank,) = replaced_scenario(world, monkeypatch, (50.0, '2026-01-14'))
    run_sql(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4185')

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200 and allocations(db, r2) == [(bank, 500000, '2026-01-14')]
    assert count_accounts(db, 4185) == 0 and revenue_account_of(db, bank) == 4184


def test_the_move_creates_the_account_a_payment_of_2025_needs(world, monkeypatch):
    db = world.db
    r1, r2, (bank,) = replaced_scenario(world, monkeypatch, (50.0, '2025-12-20'))
    run_sql(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4184')

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200 and allocations(db, r2) == [(bank, 500000, '2025-12-20')]
    assert count_accounts(db, 4184) == 0                                  # eine Zahlung von 2025 braucht es nicht


def test_a_replacement_whose_head_turns_into_a_rate_of_zero_before_the_transaction_refuses(world, monkeypatch):
    """−1 und 0 haben denselben Schlüssel des Satzes – die vorbereitete Auflösung (nach Datum) gälte nicht mehr: ``CHANGED_MESSAGE``, nichts geschrieben."""
    db = world.db
    r1, r2, (bank,) = replaced_scenario(world, monkeypatch, (50.0, '2026-01-14'))
    original = type(db)._payment_move_context

    def flipped(self, invoice_id, new):
        context = original(self, invoice_id, new)
        run_sql(self, 'UPDATE Invoices SET TaxRate = 0 WHERE ID = ?', invoice_id)
        return context

    monkeypatch.setattr(type(db), '_payment_move_context', flipped)
    before = {key: rows for key, rows in tables(db).items() if key not in ('Invoices', 'InvoiceCorrections')}

    change = db.set_invoice_status(r2, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, CHANGED_MESSAGE)
    assert {key: rows for key, rows in tables(db).items() if key not in ('Invoices', 'InvoiceCorrections')} == before
    assert allocations(db, r1) == [(bank, 500000, '2026-01-14')] and allocations(db, r2) == []


def test_the_move_context_names_the_accounts_by_number_for_a_section_19_replacement_only(world, monkeypatch):
    db = world.db
    r1, r2, _banks = replaced_scenario(world, monkeypatch, (40.0, '2025-12-20'), (60.0, '2026-01-10'))
    context = db._payment_move_context(r2, 'sent')
    assert context['revenue_coa'] is None and context['revenue_by_number'] == {4184: _coa(db, 4184), 4185: _coa(db, 4185)}
    other = reissue_with_tax(world, monkeypatch)
    context = db._payment_move_context(other, 'sent')
    assert context['revenue_by_number'] is None and context['revenue_coa'] == _coa(db, 4400)


def test_a_refused_send_of_a_section_19_replacement_creates_no_account(world, monkeypatch):
    """Das Versenden scheitert an einem Widerspruch der Momentaufnahme (Kopf −1 gegen den Satz einer Position): Der Umzugskontext löst dann kein Erlöskonto auf –
    weder 4184 noch 4185 entsteht, kein Zähler rückt, alle Tabellen bleiben, wie sie waren."""
    db = world.db
    r1, r2, (bank,) = replaced_scenario(world, monkeypatch, (50.0, '2026-01-14'))
    run_sql(db, 'UPDATE InvoiceItems SET TaxRateBp = 1900 WHERE InvoiceId = ?', r2)
    run_sql(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (4184, 4185)')
    before = all_rows(db)

    change = db.set_invoice_status(r2, 'sent', USER)

    assert (change.ok, change.code) == (False, 409)
    assert all_rows(db) == before and count_accounts(db, 4184, 4185) == 0


def reissue_with_tax(w, monkeypatch):
    """Eine Ersatzrechnung mit 19 % zu einer bezahlten, stornierten Rechnung."""
    r1 = sent(w, 'T-400')
    pay_on(w, r1, 50.0, '2026-07-01')
    cancel(w, r1, '2026-07-02')
    return reissue(w.db, r1, monkeypatch)


# ═══ Zuordnung neu aufbauen ══════════════════════════════════════════════════

FIXED_TODAY = '2026-06-30'          # ein fester Tag für den Status – nie die Uhr


def linked_across_the_switch(db, acct, number='R-RB', rate=-1):
    """Eine versendete Rechnung über 100,00 € ohne Steuerausweis, 60,00 € am 20.12.2025 und 40,00 € am 10.01.2026 – wie der WISO-Import sie zuordnet: nur die Zeilen."""
    invoice = _sent(db, number, 100.00, rate=rate, tax_amount=0)
    first, second = _bank(db, acct, 60.00, date='2025-12-20'), _bank(db, acct, 40.00, date='2026-01-10')
    db.link_invoice_to_transaction(invoice, first, 60.00)
    db.link_invoice_to_transaction(invoice, second, 40.00)
    return invoice, first, second


def payment_ids(db):
    return [row[0] for row in _raw(db, 'SELECT ID FROM InvoicePayments ORDER BY ID')]


def rebuild(db, invoice, **kw):
    return db.rebuild_invoice_payments(invoice, today=FIXED_TODAY, **kw)


def test_the_rebuild_over_the_switch_names_both_accounts_and_books_each_assignment_on_its_own(db, acct):
    invoice, first, second = linked_across_the_switch(db, acct)
    before = all_rows(db)

    result = rebuild(db, invoice)
    plan = result.plan

    assert (result.ok, result.needs_revenue_coa, plan.skipped) == (True, False, [])
    assert all_rows(db) == before                                                          # der Probelauf lässt nichts zurück
    assert [(effect.date, effect.account, effect.net, effect.tax) for effect in plan.euer] == [
        ('2025-12-20', 4185, 600000, 0), ('2026-01-10', 4184, 400000, 0)]
    assert plan.revenue_coa == sorted([_coa(db, 4184), _coa(db, 4185)])                   # mehrere Konten: eine sortierte Liste

    done = rebuild(db, invoice, dry_run=False, confirmed_check=plan.check)
    assert (done.ok, done.executed) == (True, True)
    assert rows_under(db, first) == [(1800, 10000, 600000), (4340, 4185, 600000)]
    assert rows_under(db, second) == [(1800, 10000, 400000), (4340, 4184, 400000)]
    assert euer_between(db, '2025-01-01', '2025-12-31') == {4185: 60.0} and euer_between(db, '2026-01-01', '2026-12-31') == {4184: 40.0}
    assert db.find_payment_inconsistencies() == [] and _raw(db, 'SELECT Status FROM Invoices WHERE ID = ?', invoice) == [('paid',)]
    assert rebuild(db, invoice).plan.actions == []                                          # danach gibt es nichts mehr zu tun


@pytest.mark.parametrize('days, number', [(('2025-12-20', '2025-12-31'), 4185), (('2026-01-01', '2026-01-10'), 4184)])
def test_assignments_of_one_year_keep_the_plan_of_a_single_account(db, acct, days, number):
    """Nennt der Plan nur ein Konto, ist es dessen ID – wie bei einem festen Erlöskonto (die Prüfsumme der Pläne eines Jahres bleibt, wie sie war)."""
    invoice = _sent(db, 'R-ONE', 100.00, rate=-1, tax_amount=0)
    for day, amount in zip(days, (60.00, 40.00)):
        db.link_invoice_to_transaction(invoice, _bank(db, acct, amount, date=day), amount)
    plan = rebuild(db, invoice).plan
    assert plan.revenue_coa == _coa(db, number) and {effect.account for effect in plan.euer} == {number}


def test_a_missing_account_leaves_that_assignment_with_a_fixed_reason_and_asks_for_no_choice(db, acct):
    invoice, first, second = linked_across_the_switch(db, acct)
    _exec(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4184')
    before = all_rows(db)

    result = rebuild(db, invoice, revenue_coa_id=_coa(db, 4400))                           # eine Wahl gilt dort nie

    plan = result.plan
    assert (result.ok, result.needs_revenue_coa) == (True, False)
    assert plan.skipped == [REBUILD_SKIP_REASONS['revenue_missing'].format(payment=payment_ids(db)[1], number=4184)]
    assert plan.skipped == ['Zuordnung #%d: Das Erlöskonto 4184 fehlt im Kontenrahmen – bitte unter Stammdaten → Kontenrahmen „Standardkonten ergänzen“.' % payment_ids(db)[1]]
    assert [action.kind for action in plan.actions] == ['rebuild'] and plan.revenue_coa == _coa(db, 4185)
    assert all_rows(db) == before                                                          # der Probelauf hinterlässt nichts, auch kein Konto

    done = rebuild(db, invoice, dry_run=False, confirmed_check=plan.check)
    assert done.executed and rows_under(db, first) == [(1800, 10000, 600000), (4340, 4185, 600000)] and rows_under(db, second) == []
    assert count_accounts(db, 4184) == 0                                                   # die Reparatur legt nie ein Konto an


def test_without_both_accounts_nothing_is_rebuilt_and_no_choice_is_asked_for(db, acct):
    invoice, first, second = linked_across_the_switch(db, acct)
    drop_small_business_accounts(db)
    before = all_rows(db)

    result = rebuild(db, invoice)

    assert (result.ok, result.needs_revenue_coa) == (True, False) and result.plan.actions == [] and result.plan.revenue_coa is None
    assert [text.split(':')[0] for text in result.plan.skipped] == [f'Zuordnung #{pid}' for pid in payment_ids(db)]
    assert all_rows(db) == before and count_accounts(db, 4184, 4185) == 0


def test_an_unreadable_date_of_the_movement_leaves_that_assignment_with_a_fixed_reason(db, acct):
    invoice, first, second = linked_across_the_switch(db, acct)
    _exec(db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', 'kaputt', second)

    plan = rebuild(db, invoice).plan

    assert plan.skipped == [REBUILD_SKIP_REASONS['revenue_date'].format(payment=payment_ids(db)[1])]
    assert plan.skipped == [f'Zuordnung #{payment_ids(db)[1]}: Das Datum der Bankbewegung ist ungültig – davon hängt das Erlöskonto ab.']
    assert [action.kind for action in plan.actions] == ['rebuild']


def test_a_rate_of_zero_with_tax_still_asks_for_a_choice_when_4185_is_missing(db, acct):
    """Variante B: Nur der Platzhalter −1 folgt der Datumsregel – 0 % mit Steuerausweis hat ein festes Konto (4185), und fehlt es, wählt der Nutzer wie bisher."""
    invoice, first, second = linked_across_the_switch(db, acct, number='R-NULL', rate=0)
    _exec(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4185')

    result = rebuild(db, invoice)

    assert (result.ok, result.code, result.message, result.needs_revenue_coa) == (False, 400, payments_module.NO_REVENUE_COA, True)


def test_the_page_of_a_section_19_invoice_shows_no_choice_of_the_revenue_account_for_the_rebuild(db, acct):
    invoice, first, second = linked_across_the_switch(db, acct)
    drop_small_business_accounts(db)

    html = PageInvoice(db, {}, invoice)

    assert 'rebuildPayments(' in html and 'id="rebuildRevenueCoa"' not in html and 'Für den Neuaufbau ist kein Erlöskonto hinterlegt' not in html


def test_the_page_of_a_rate_of_zero_with_tax_shows_the_choice_when_4185_is_missing(db, acct):
    invoice, first, second = linked_across_the_switch(db, acct, number='R-NULL', rate=0)
    _exec(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4185')

    html = PageInvoice(db, {}, invoice)

    assert 'rebuildPayments(' in html and 'id="rebuildRevenueCoa"' in html


def test_a_wiso_style_link_without_rows_creates_no_account(db, acct):
    """Der Import ordnet ohne Sätze zu und löst kein Erlöskonto auf: Bestehendes bleibt, nichts entsteht."""
    drop_small_business_accounts(db)
    before = all_rows(db)
    invoice = section_19(db, 'R-WISO')
    bank = _bank(db, acct, 100.00, date='2026-02-02')
    db.link_invoice_to_transaction(invoice, bank, 100.00)
    assert count_accounts(db, 4184, 4185) == 0 and rows_under(db, bank) == []
    assert all_rows(db)['ChartOfAccounts'] == before['ChartOfAccounts']


# ═══ Die Auswertungen kennen 4184 wie 4185 ═══════════════════════════════════


def test_no_report_has_a_rule_of_its_own_for_the_account_numbers():
    """Die Konten stehen in Bereichen, nie als Aufzählung: 4184 liegt überall dort, wo 4185 liegt."""
    for number in (4184, 4185):
        assert euer_section(number, set()) == 'income'
        assert group_of(number) == 'revenue'
        assert susa.group_key(number) == 'revenue' and not susa.is_carried(number)
        assert datev_keys.is_revenue_number(number)
        assert datev_keys.booking_kind('purpose_first', number, 1800, True) == datev_keys.KIND_VAT                      # eine Zahlung auf das Erlöskonto
        assert datev_keys.booking_kind('transfer', 4340, number, True, (4340, 4345, 4405)) == datev_keys.KIND_INVOICE_CHAIN   # „Wartekonto an Erlöskonto“


def export_lines(db, first, last):
    status = handle_datev_export(db, {'date_from': [first], 'date_to': [last]})
    assert isinstance(status[0], bytes), status
    lines = [line for line in status[0].decode('cp1252').split('\r\n') if line][2:]
    return [next(csv.reader([line], delimiter=';', quotechar='"')) for line in lines]


def account_in(report, number):
    return next(account for group in report.groups for account in group.accounts if account.number == number)


def test_the_same_payment_the_day_before_and_the_day_after_the_switch_gives_the_same_figures_on_its_own_account(db, acct):
    pay(db, acct, section_19(db, 'R-A'), '2025-12-31')
    pay(db, acct, section_19(db, 'R-B'), '2026-01-01')

    # EÜR und Dashboard
    assert euer_between(db, '2025-01-01', '2025-12-31') == {4185: 100.0} and euer_between(db, '2026-01-01', '2026-12-31') == {4184: 100.0}
    html = PageDashboard(db, '2026-01-01', '2026-12-31')
    assert html.index('<td>4184</td>') < html.index('Summe Betriebseinnahmen') and '<td>4185</td>' not in html
    # BWA: dieselbe Gruppe, derselbe Betrag
    december, january = db.get_bwa(2025, 12, 'jahr'), db.get_bwa(2026, 1, 'jahr')
    assert account_numbers(december, 'revenue') == [4185] and account_numbers(january, 'revenue') == [4184]
    assert values_of(december, 'revenue', 'ytd') == values_of(january, 'revenue', 'month') == 1000000
    assert values_of(december, 'result', 'ytd') == values_of(january, 'result', 'month')
    # Summen- und Saldenliste: das Erlöskonto im Haben, nichts wird vorgetragen
    old, new = account_in(db.get_susa(2025, 12), 4185), account_in(db.get_susa(2026, 1), 4184)
    assert (old.month_haben, old.ytd_haben, old.opening) == (new.month_haben, new.ytd_haben, new.opening) == (1000000, 1000000, 0)
    # Liquidität: die Überleitung geht in beiden Monaten auf
    assert set(unassigned_of(db.get_liquidity(2025, 12, 'jahr')).values()) == set(unassigned_of(db.get_liquidity(2026, 1, 'jahr')).values()) == {0}
    # DATEV: dieselbe Zeile mit demselben Schlüssel, nur das Gegenkonto der Umbuchung ist ein anderes
    lines_2025, lines_2026 = export_lines(db, '2025-01-01', '2025-12-31'), export_lines(db, '2026-01-01', '2026-12-31')
    transfer = lambda lines: [(line[6], line[7], line[8], line[1], line[0]) for line in lines if line[6] == '4340']
    assert transfer(lines_2025) == [('4340', '4185', '0', 'S', '100,00')] and transfer(lines_2026) == [('4340', '4184', '0', 'S', '100,00')]
    assert db.find_payment_inconsistencies() == [] and db.find_unbalanced_splits('2025-01-01', '2026-12-31') == []


def test_a_payment_the_mask_books_directly_on_4184_is_income_like_one_on_4185(db, acct):
    """Eine Zahlung, die die Maske direkt auf 4184 bucht (ohne Rechnung), ist Einnahme wie die auf 4185."""
    handle_add_transaction(db, {'transaction_id': ['0'], 'date': ['2026-03-01'], 'amount': ['100.00'], 'account': [str(acct)], 'coa_id': [str(_coa(db, 4184))]})
    assert euer_between(db, '2026-01-01', '2026-12-31') == {4184: 100.0}
    assert FORM_PURPOSE_FIRST in {row[0] for row in _raw(db, 'SELECT EntryForm FROM Bookings WHERE EntryForm IS NOT NULL')}
