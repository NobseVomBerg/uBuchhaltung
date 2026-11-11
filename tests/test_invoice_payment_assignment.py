# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zahlungszuordnung Rechnung ↔ Bankbewegung.

Eine Zuordnung ist genau eine InvoicePayments-Zeile plus die Buchungssätze, die die App für sie
anlegt (Marke ``Status='invoice_payment'``): „Bank an Debitoren“ und „Wartekonto an Erlöskonto“ –
bzw. „Erlöskonto an Bank“, wenn es keine Forderung gibt. ``assign_invoice_payment`` legt sie an,
``unassign_invoice_payment`` und ``delete_transaction`` nehmen sie samt Sätzen wieder weg.

Geprüft wird an dem, was am Ende zählt: EÜR (``get_euer_data``), DATEV-Export, die Prüfung auf
unstimmige Splits und der offene Posten auf Debitoren (10000). Beträge, Namen und Nummern sind
erfunden.
"""
import csv
import datetime
import json
import sqlite3
import threading
from decimal import Decimal

import pytest

from db import Database
from db.invoice_payments import PAYMENT_MARK, next_payment_status, payment_blocker
from money import prorate
from server import handlers
from server.handlers import (handle_add_transaction, handle_datev_export,
                             link_booking_to_invoice_capped)
from server.pages_contacts import PageContacts
from server.pages_invoice import PageInvoice
from server.pages_miscellaneous import PageMiscellaneous
from server.pages_transactions import PageTransactions
from tests.document_helpers import make_books, post_json
from tests.http_client import get, own_origin, post

TODAY = datetime.date.today()
YESTERDAY = (TODAY - datetime.timedelta(days=1)).isoformat()
RANGE = ('2026-01-01', '2026-12-31')


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _account(db, name='Zuordnungs-Bank', skr=1800):
    if not [a for a in db.fetch_accounts() if a[1] == name]:
        db.insert_account(name, 'Ich', 'DE00ZUORD', 'BIC', 'Bank', is_cash=0, skr_account=skr)
    return [a for a in db.fetch_accounts() if a[1] == name][0][0]


def _customer(db, name='Käufer AG'):
    db.insert_contact(display_name=name, company_name=name)
    return _raw(db, 'SELECT ID FROM Contacts WHERE DisplayName = ?', name)[0][0]


def _invoice(db, number, gross, rate=0.19, status='sent', due='2099-12-31', **kw):
    net = round(gross / (1 + rate), 2) if rate > 0 else gross
    data = {
        'invoice_number': number, 'invoice_date': '2026-01-01', 'payment_due_date': due,
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': rate, 'sum_net': net, 'tax_amount': round(gross - net, 2),
        'sum_gross': gross, 'amount_due': gross, 'status': status,
    }
    data.update(kw)
    return db.insert_invoice(data)


def _sent(db, number, gross, **kw):
    """Versendete Rechnung samt Forderungsbuchung (Debitoren an Wartekonto) wie beim Versenden."""
    invoice_id = _invoice(db, number, gross, **kw)
    db.ensure_receivable_booking(invoice_id)
    return invoice_id


def _bank(db, account_id, amount, date='2026-02-01', **kw):
    return db.insert_booking(date, amount, account_id=account_id, booking_type='bank',
                             recipient_client=kw.pop('recipient', 'Käufer AG'), **kw)


def _raw(db, sql, *params):
    con = sqlite3.connect(db.db_name)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _count(db, table='Bookings'):
    return _raw(db, f'SELECT COUNT(*) FROM {table}')[0][0]


def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _invoice_state(db, invoice_id):
    (status, due), = _raw(db, 'SELECT Status, AmountDue FROM Invoices WHERE ID = ?', invoice_id)
    return status, Decimal(due) / 10000


def _children(db, bank_id):
    """Buchungssätze unter einer Bankbewegung als (Konto, Gegenkonto, Betrag, Steuer, Text,
    Status), nach ID."""
    numbers = db.get_coa_id_to_number_map()
    return [(numbers.get(coa), numbers.get(counter), Decimal(amount) / 10000,
             None if tax is None else Decimal(tax) / 10000, text, status)
            for coa, counter, amount, tax, text, status in _raw(
                db, 'SELECT COA_ID, CounterCOA_ID, Amount, TaxAmount, Text, Status FROM Bookings '
                    'WHERE ParentBooking_ID = ? ORDER BY ID', bank_id)]


def _euer(db):
    return {number: round(total, 2) for number, _name, total in db.get_euer_data(*RANGE)}


def _balance(db, number):
    """Saldo eines Kontos über alle Buchungssätze: Soll (COA) minus Haben (Gegenkonto)."""
    (soll, haben), = _raw(
        db, "SELECT COALESCE(SUM(CASE WHEN b.COA_ID = c.ID THEN b.Amount END), 0), "
            "COALESCE(SUM(CASE WHEN b.CounterCOA_ID = c.ID THEN b.Amount END), 0) "
            "FROM Bookings b, ChartOfAccounts c WHERE c.AccountNumber = ? "
            "AND b.BookingType = 'entry'", number)
    return Decimal(soll - haben) / 10000


def _datev(db):
    """Exportierte Zeilen als (Betrag, Konto, Gegenkonto); der Export darf nicht gesperrt sein."""
    result = handle_datev_export(db, {'date_from': [RANGE[0]], 'date_to': [RANGE[1]]})
    assert isinstance(result[0], bytes), f'DATEV-Export gesperrt: {result}'
    lines = [ln for ln in result[0].decode('cp1252').split('\r\n') if ln]
    fields = [next(csv.reader([ln], delimiter=';', quotechar='"')) for ln in lines[2:]]
    return [(f[0], f[6], f[7]) for f in fields]


def _datev_amount(db, number):
    """Summe der Beträge aller DATEV-Zeilen, die das Konto als Konto oder Gegenkonto führen."""
    return sum(Decimal(amount.replace(',', '.')) for amount, konto, gegen in _datev(db)
               if str(number) in (konto, gegen))


def _assign(db, invoice_id, bank_id, amount=None):
    ok, message, payment_id = db.assign_invoice_payment(invoice_id, bank_id, amount)
    assert ok, message
    return payment_id


@pytest.fixture
def db(tmp_db):
    return tmp_db


@pytest.fixture
def acct(db):
    return _account(db)


# ── Normalfall: Forderung, Zahlung, Umbuchung ────────────────────────────────

def test_normal_payment_books_settlement_and_transfer_once(db, acct):
    inv = _sent(db, 'R001', 119.00)
    bank = _bank(db, acct, 119.00)

    payment_id = _assign(db, inv, bank)

    assert payment_id == db.get_invoice_payments(inv)[0][0]
    assert _children(db, bank) == [
        (1800, 10000, Decimal('119'), None, 'Zahlung zu Re. R001', PAYMENT_MARK),
        (4405, 4400, Decimal('119'), Decimal('19'), 'Umb. zu Re. R001', PAYMENT_MARK),
    ]
    kids = _raw(db, 'SELECT DocumentNumber, AutoMirror, DateBooking, RecipientClient '
                    'FROM Bookings WHERE ParentBooking_ID = ?', bank)
    assert kids == [('R001', 0, '2026-02-01', 'Käufer AG')] * 2
    assert _invoice_state(db, inv) == ('paid', 0)
    assert _euer(db) == {4400: 100.0, 3806: 19.0}
    assert sorted((k, g) for _b, k, g in _datev(db)) == [('10000', '4405'), ('1800', '10000'),
                                                          ('4405', '4400')]
    assert _datev_amount(db, 4400) == Decimal('119.00')           # der Erlös steht genau einmal im Stapel
    assert db.find_unbalanced_splits(*RANGE) == []
    assert _balance(db, 10000) == 0 and _balance(db, 4405) == 0
    assert db.find_payment_inconsistencies() == []


def _normal_case(db, acct):
    inv = _sent(db, 'R001', 119.00)
    _assign(db, inv, _bank(db, acct, 119.00))
    return Decimal('119.00'), 100.0, 19.0, 3


def _collective_case(db, acct):
    bank = _bank(db, acct, 357.00)
    for number, gross in (('R001', 119.00), ('R002', 238.00)):
        _assign(db, _sent(db, number, gross), bank)
    return Decimal('357.00'), 300.0, 57.0, 6


def _partial_case(db, acct):
    inv = _sent(db, 'R-TP', 100.00)
    for n, amount in enumerate((33.33, 33.33, 33.34), start=1):
        _assign(db, inv, _bank(db, acct, amount, date=f'2026-02-0{n}'))
    return Decimal('100.00'), 84.03, 15.97, 7


@pytest.mark.parametrize('build', [_normal_case, _collective_case, _partial_case],
                         ids=['Normalfall', 'Sammelzahlung', 'Teilzahlungen'])
def test_datev_and_euer_hold_the_revenue_exactly_once(db, acct, build):
    """Dieselbe Rechnung darf weder in der EÜR noch im DATEV-Stapel doppelt stehen: Der Erlös
    erscheint einmal (EÜR: netto und USt; DATEV: der Bruttobetrag auf dem Erlöskonto), und die
    Bankbewegungen gehen auf – der Export bleibt frei."""
    gross, net, tax, datev_rows = build(db, acct)

    assert _euer(db) == {4400: net, 3806: tax}
    assert _datev_amount(db, 4400) == gross
    assert len(_datev(db)) == datev_rows
    assert db.find_unbalanced_splits(*RANGE) == []


def test_the_payment_is_dated_like_the_bank_movement(db, acct):
    inv = _sent(db, 'R002', 119.00)
    bank = _bank(db, acct, 119.00, date='2026-03-17', date_tax='2026-03-18')
    _assign(db, inv, bank)
    assert _raw(db, 'SELECT PaymentDate FROM InvoicePayments') == [('2026-03-17',)]
    assert set(_raw(db, 'SELECT DateBooking, DateTax FROM Bookings WHERE ParentBooking_ID = ?',
                    bank)) == {('2026-03-17', '2026-03-18')}


# ── 1. Sammelzahlung ─────────────────────────────────────────────────────────

def test_collective_payment_gets_one_pair_per_invoice(db, acct):
    r1 = _sent(db, 'R001', 119.00)
    r2 = _sent(db, 'R002', 238.00)
    bank = _bank(db, acct, 357.00)

    _assign(db, r1, bank)
    _assign(db, r2, bank)

    assert len(_children(db, bank)) == 4                         # je Rechnung Zahlung + Umbuchung
    assert _invoice_state(db, r1) == ('paid', 0) and _invoice_state(db, r2) == ('paid', 0)
    assert _euer(db) == {4400: 300.0, 3806: 57.0}
    assert len(_datev(db)) == 6                                  # 2 Forderungen + 2 × (Zahlung, Umbuchung)
    assert _datev_amount(db, 4400) == Decimal('357.00')          # Erlös genau einmal, je Rechnung ein Satz
    assert db.find_unbalanced_splits(*RANGE) == []
    assert _balance(db, 10000) == 0 and _balance(db, 4405) == 0
    assert db.get_booking_unallocated_amount(bank) == 0


def test_collective_payment_covering_only_one_invoice_leaves_the_rest_free(db, acct):
    r1 = _sent(db, 'R001', 119.00)
    r2 = _sent(db, 'R002', 238.00)
    bank = _bank(db, acct, 200.00)

    _assign(db, r1, bank)                                        # 119 von 200
    _assign(db, r2, bank)                                        # die restlichen 81 von 238

    allocations = {a[2]: a[3] for a in db.get_booking_allocations(bank)}
    assert allocations == {'R001': Decimal('119'), 'R002': Decimal('81')}
    assert _invoice_state(db, r2) == ('partial_payment', Decimal('157'))
    assert db.get_booking_unallocated_amount(bank) == 0
    assert db.find_unbalanced_splits(*RANGE) == []


# ── 2. Teilzahlungen: die Steuer geht exakt auf ──────────────────────────────

def test_partial_payments_add_up_to_the_exact_invoice_tax(db, acct):
    inv = _sent(db, 'R-TP', 100.00)                              # 84,03 netto + 15,97 USt
    banks = [_bank(db, acct, amount, date=f'2026-02-0{n}')
             for n, amount in enumerate((33.33, 33.33, 33.34), start=1)]

    for bank in banks:
        _assign(db, inv, bank)

    taxes = [next(c[3] for c in _children(db, bank) if c[0] == 4405) for bank in banks]
    assert taxes == [Decimal('5.32'), Decimal('5.32'), Decimal('5.33')]    # der letzte gleicht aus
    assert sum(taxes) == Decimal('15.97')
    assert _invoice_state(db, inv) == ('paid', 0)
    assert _euer(db) == {4400: 84.03, 3806: 15.97}
    assert len(_datev(db)) == 7                                  # Forderung + 3 × (Zahlung, Umbuchung)
    assert _datev_amount(db, 4400) == Decimal('100.00')          # die Teile ergeben den Erlös einmal
    assert db.find_unbalanced_splits(*RANGE) == []
    assert _balance(db, 10000) == 0 and _balance(db, 4405) == 0


def test_partial_payments_at_seven_percent(db, acct):
    inv = _sent(db, 'R-7', 107.00, rate=0.07)                    # 100,00 netto + 7,00 USt
    first, second = _bank(db, acct, 50.00), _bank(db, acct, 57.00, date='2026-02-02')

    _assign(db, inv, first)
    assert _invoice_state(db, inv) == ('partial_payment', Decimal('57'))
    _assign(db, inv, second)

    assert [c[3] for b in (first, second) for c in _children(db, b) if c[0] == 4345] == \
        [Decimal('3.27'), Decimal('3.73')]
    assert _euer(db) == {4300: 100.0, 3806: 7.0}
    assert _invoice_state(db, inv) == ('paid', 0)
    assert _balance(db, 10000) == 0 and _balance(db, 4345) == 0


def test_closing_part_after_a_legacy_partial_payment_takes_only_the_remaining_tax(db, acct):
    """Eine Teilzahlung aus dem Altbestand (Sätze ohne Marke) hat ihre Steuer schon gebucht;
    der Rest über die neue Funktion bekommt nur, was an der Rechnungssteuer noch fehlt."""
    inv = _sent(db, 'R-ALT', 119.00)                                    # 19,00 USt
    old_bank = _bank(db, acct, 50.00)
    db.link_invoice_to_transaction(inv, old_bank, 50.00)
    for coa, counter, tax in ((1800, 10000, None), (4405, 4400, 7.98)):
        db.insert_booking('2026-02-01', 50.00, coa_id=_coa(db, coa), counter_coa_id=_coa(db, counter),
                          booking_type='entry', parent_booking_id=old_bank, tax_rate=0.19 if tax else None,
                          tax_amount=tax, document_number='R-ALT')

    _assign(db, inv, _bank(db, acct, 69.00, date='2026-02-02'))

    assert _invoice_state(db, inv) == ('paid', 0)
    assert _euer(db) == {4400: 100.0, 3806: 19.0}                       # 7,98 + 11,02, nicht 26,98
    assert _balance(db, 10000) == 0 and _balance(db, 4405) == 0


def test_tax_free_invoice_books_without_tax(db, acct):
    inv = _sent(db, 'R-19', 100.00, rate=-1, tax_amount=0)       # § 19 UStG
    bank = _bank(db, acct, 100.00)

    _assign(db, inv, bank)

    assert _children(db, bank) == [
        (1800, 10000, Decimal('100'), None, 'Zahlung zu Re. R-19', PAYMENT_MARK),
        (4340, 4185, Decimal('100'), None, 'Umb. zu Re. R-19', PAYMENT_MARK),
    ]
    assert _euer(db) == {4185: 100.0}
    assert _balance(db, 10000) == 0 and _balance(db, 4340) == 0


# ── 3. Überzahlung ───────────────────────────────────────────────────────────

def test_overpayment_leaves_the_rest_free_as_credit(db, acct):
    customer = _customer(db)
    inv = _sent(db, 'R-OVER', 119.00, customer_id=customer)
    bank = _bank(db, acct, 150.00, contact_id=customer)

    _assign(db, inv, bank)

    assert db.get_invoice_payments(inv)[0][3] == Decimal('119')
    assert _children(db, bank)[0][2] == Decimal('119')
    assert db.get_booking_unallocated_amount(bank) == Decimal('31')
    items = db.get_open_items_for_contact(customer)
    assert [(c[0], c[5]) for c in items['credits']] == [(bank, Decimal('31'))]
    assert items['open_invoices'] == [] and items['saldo'] == Decimal('-31')
    # Der Rest ist nicht bankwirksam gebucht: Der Export bleibt gesperrt, bis er kontiert ist
    assert db.find_unbalanced_splits(*RANGE) != []


# ── 4. Satz ohne Wartekonto ──────────────────────────────────────────────────

def test_rate_without_waiting_account_books_one_revenue_row_over_the_allocation(db, acct):
    # Für 5 % gibt es kein Standard-Erlöskonto; die Historie liefert es (4400)
    old = _invoice(db, 'R-5A', 105.00, rate=0.05)
    db.link_invoice_to_transaction(old, _bank(db, acct, 105.00, coa_id=_coa(db, 4400)), 105.00)
    inv = _sent(db, 'R-5B', 210.00, rate=0.05)                   # kein Wartekonto → keine Forderung
    assert db.get_receivable_booking(inv) is None
    bank = _bank(db, acct, 400.00, date='2026-02-02')

    _assign(db, inv, bank, amount=100.00)

    assert _children(db, bank) == [
        (4400, 1800, Decimal('100'), Decimal('4.76'), 'Zahlung zu Re. R-5B', PAYMENT_MARK)]
    assert _invoice_state(db, inv) == ('partial_payment', Decimal('110'))
    assert db.get_booking_unallocated_amount(bank) == Decimal('300')
    assert _euer(db) == {4400: 95.24, 3806: 4.76}


def test_rate_without_any_revenue_account_is_rejected_not_guessed(db, acct):
    inv = _sent(db, 'R-5C', 105.00, rate=0.05)
    bank = _bank(db, acct, 105.00)
    before = _count(db)

    ok, message, payment_id = db.assign_invoice_payment(inv, bank)

    assert not ok and payment_id is None and 'Erlöskonto' in message
    assert _count(db) == before and _count(db, 'InvoicePayments') == 0


# ── 5. Ablehnungen ───────────────────────────────────────────────────────────

def _no_change(db, call, expected_in_message):
    bookings, payments = _count(db), _count(db, 'InvoicePayments')
    ok, message, payment_id = call()
    assert not ok and payment_id is None
    assert expected_in_message in message, message
    assert (_count(db), _count(db, 'InvoicePayments')) == (bookings, payments)


def test_outgoing_movement_is_rejected(db, acct):
    inv = _sent(db, 'R-OUT', 119.00)
    bank = _bank(db, acct, -119.00)
    _no_change(db, lambda: db.assign_invoice_payment(inv, bank), 'Zahlungseingänge')
    assert _invoice_state(db, inv) == ('sent', 119)


@pytest.mark.parametrize('status, expected', [
    ('draft', 'noch nicht versendet'), ('finalized', 'noch nicht versendet'),
    ('cancelled', 'storniert'), ('paid', 'bereits bezahlt'),
])
def test_invoice_that_is_not_payable_is_rejected(db, acct, status, expected):
    inv = _invoice(db, 'R-NP', 119.00, status=status)
    bank = _bank(db, acct, 119.00)
    _no_change(db, lambda: db.assign_invoice_payment(inv, bank), expected)
    assert _invoice_state(db, inv)[0] == status


def test_quote_is_rejected(db, acct):
    quote = _invoice(db, 'A-1', 119.00, document_type='quote')
    bank = _bank(db, acct, 119.00)
    _no_change(db, lambda: db.assign_invoice_payment(quote, bank), 'Rechnung nicht gefunden')


def test_bank_account_without_skr_number_is_rejected(db):
    db.insert_account('Ohne-SKR', 'Ich', 'DE00OHNE', 'BIC', 'Bank', is_cash=0, skr_account=None)
    no_skr = [a[0] for a in db.fetch_accounts() if a[1] == 'Ohne-SKR'][0]
    inv = _sent(db, 'R-NS', 119.00)
    bank = _bank(db, no_skr, 119.00)
    _no_change(db, lambda: db.assign_invoice_payment(inv, bank), 'SKR-Nummer')


def test_movement_without_a_bank_account_is_rejected(db):
    inv = _sent(db, 'R-NA', 119.00)
    bank = db.insert_booking('2026-02-01', 119.00, booking_type='bank')
    _no_change(db, lambda: db.assign_invoice_payment(inv, bank), 'Bankkonto')


def test_assignment_to_a_booking_that_is_not_a_bank_movement_is_rejected(db, acct):
    inv = _sent(db, 'R-EN', 119.00)
    entry = db.insert_booking('2026-02-01', 119.00, booking_type='entry', coa_id=_coa(db, 4400))
    _no_change(db, lambda: db.assign_invoice_payment(inv, entry), 'Bankbewegungen')
    _no_change(db, lambda: db.assign_invoice_payment(inv, 99999), 'nicht gefunden')


def test_an_assigned_pair_is_not_assigned_twice(db, acct):
    inv = _sent(db, 'R-DUP', 238.00)
    bank = _bank(db, acct, 300.00)
    _assign(db, inv, bank, amount=100.00)
    _no_change(db, lambda: db.assign_invoice_payment(inv, bank), 'bereits')
    assert len(db.get_invoice_payments(inv)) == 1


def test_bad_inputs_are_rejected(db, acct):
    inv = _sent(db, 'R-BAD', 119.00)
    bank = _bank(db, acct, 119.00)
    _no_change(db, lambda: db.assign_invoice_payment(inv, bank, amount=0), 'größer 0')
    _no_change(db, lambda: db.assign_invoice_payment(inv, bank, amount=-5), 'größer 0')
    _no_change(db, lambda: db.assign_invoice_payment(99999, bank), 'nicht gefunden')
    credit_note = _sent(db, 'R-GS', -119.00)
    _no_change(db, lambda: db.assign_invoice_payment(credit_note, bank), 'positivem')


def test_a_booked_movement_cannot_be_assigned_again(db, acct):
    """Eine schon als Erlös kontierte Bewegung ist keine Rechnungszahlung mehr – der Fall, der
    früher den Erlös doppelt buchte."""
    inv = _sent(db, 'R-BK', 119.00)
    bank = _bank(db, acct, 119.00)
    db.insert_booking('2026-02-01', 119.00, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1800),
                      booking_type='entry', parent_booking_id=bank, auto_mirror=True)
    assert db.get_booking_unallocated_amount(bank) == 0
    assert bank not in [c[0] for c in db.get_unallocated_bank_bookings()]
    _no_change(db, lambda: db.assign_invoice_payment(inv, bank), 'ausgeschöpft')


# ── 6. Nebenläufigkeit ───────────────────────────────────────────────────────

def test_two_simultaneous_requests_assign_the_pair_exactly_once(tmp_path):
    path = str(tmp_path / 'race.db')
    setup = Database(db_name=path)
    acct = _account(setup)

    for run in range(20):
        inv = _sent(setup, f'R-RACE{run}', 119.00)
        bank = _bank(setup, acct, 119.00, date='2026-02-01')
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def click():
            try:
                local = Database(db_name=path)                   # eigene Verbindung je Anfrage
                barrier.wait(timeout=10)
                outcomes.append(local.assign_invoice_payment(inv, bank)[0])
            except Exception as exc:                             # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=click) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert sorted(outcomes) == [False, True], f'Lauf {run}: {outcomes}'
        assert _raw(setup, 'SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ?',
                    inv) == [(1,)]
        assert len(_children(setup, bank)) == 2
        assert _invoice_state(setup, inv) == ('paid', 0)


def test_double_click_through_the_link_route_assigns_once(db, acct):
    inv = _sent(db, 'R-DBL', 119.00)
    bank = _bank(db, acct, 119.00)
    assert link_booking_to_invoice_capped(db, inv, bank) == (True, None)
    ok, message = link_booking_to_invoice_capped(db, inv, bank)
    assert not ok and 'bereits mit dieser Rechnung verknüpft' in message
    assert len(db.get_invoice_payments(inv)) == 1 and _invoice_state(db, inv) == ('paid', 0)


# ── 7. Entfernen ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('due, expected_status', [('2099-12-31', 'sent'), (YESTERDAY, 'overdue')])
def test_removing_a_payment_removes_its_rows_and_reopens_the_invoice(db, acct, due, expected_status):
    inv = _sent(db, 'R-RM', 119.00, due=due)
    bank = _bank(db, acct, 119.00)
    payment_id = _assign(db, inv, bank)
    receivable = db.get_receivable_booking(inv)

    db.unassign_invoice_payment(payment_id)

    assert _children(db, bank) == [] and db.get_invoice_payments(inv) == []
    assert db.get_receivable_booking(inv) == receivable                  # die Forderung bleibt
    assert _invoice_state(db, inv) == (expected_status, 119)
    assert _euer(db) == {} and _balance(db, 10000) == 119
    assert db.get_booking_unallocated_amount(bank) == 119
    assert db.find_payment_inconsistencies() == []

    _assign(db, inv, bank)                                               # erneut zuordnen
    assert _euer(db) == {4400: 100.0, 3806: 19.0}                        # Erlös genau einmal
    assert _balance(db, 10000) == 0 and len(_children(db, bank)) == 2


def test_removing_one_of_two_assignments_keeps_the_other_untouched(db, acct):
    r1, r2 = _sent(db, 'R001', 119.00), _sent(db, 'R002', 238.00)
    bank = _bank(db, acct, 357.00)
    first, _second = _assign(db, r1, bank), _assign(db, r2, bank)

    db.unassign_invoice_payment(first)

    assert [c[4] for c in _children(db, bank)] == ['Zahlung zu Re. R002', 'Umb. zu Re. R002']
    assert _invoice_state(db, r1)[0] == 'sent' and _invoice_state(db, r2) == ('paid', 0)
    assert _euer(db) == {4400: 200.0, 3806: 38.0}


def test_reassigning_to_another_movement_leaves_nothing_behind(db, acct):
    """Zuordnung zur Bewegung B1 entfernen, B2 zuordnen: die Sätze von B1 sind weg, der Erlös
    steht genau einmal in den Büchern."""
    inv = _sent(db, 'R-B1B2', 119.00)
    b1 = _bank(db, acct, 119.00)
    b2 = _bank(db, acct, 119.00, date='2026-02-02')
    db.unassign_invoice_payment(_assign(db, inv, b1))

    _assign(db, inv, b2)

    assert _children(db, b1) == [] and len(_children(db, b2)) == 2
    assert _euer(db) == {4400: 100.0, 3806: 19.0}
    assert len(_datev(db)) == 3                                   # Forderung, Zahlung, Umbuchung
    assert _balance(db, 10000) == 0 and _balance(db, 4405) == 0   # vorher: −119 im Haben
    assert db.find_unbalanced_splits(*RANGE) == []
    assert db.find_payment_inconsistencies() == []


def test_partial_payment_whose_movement_is_deleted_stays_sent_and_frozen(db, acct, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda: db)
    inv = _sent(db, 'R-TEIL', 119.00)
    bank = _bank(db, acct, 50.00)
    _assign(db, inv, bank)
    assert _invoice_state(db, inv) == ('partial_payment', Decimal('69'))

    db.delete_transaction(bank)

    assert _invoice_state(db, inv) == ('sent', 119)               # vorher: „abgeschlossen“
    assert handlers._invoice_is_frozen(inv) is True               # eingefroren, nicht mehr änderbar
    assert _balance(db, 10000) == 119 and _euer(db) == {}         # die Forderung steht offen


def test_removing_an_unknown_assignment_raises(db):
    with pytest.raises(ValueError):
        db.unassign_invoice_payment(99999)


def test_legacy_rows_without_the_mark_are_never_deleted_with_an_assignment(db, acct):
    """Altbestand und WISO-Sätze tragen die Marke nicht: Beim Entfernen der Zuordnung bleiben
    sie stehen – nichts wird gelöscht, was nicht eindeutig zur Zuordnung gehört."""
    inv = _sent(db, 'R-OLD', 119.00)
    bank = _bank(db, acct, 119.00)
    legacy = db.insert_booking('2026-02-01', 119.00, coa_id=_coa(db, 1800),
                               counter_coa_id=_coa(db, 10000), booking_type='entry',
                               parent_booking_id=bank, text='Zahlung zu Re. R-OLD',
                               document_number='R-OLD')
    db.link_invoice_to_transaction(inv, bank, 119.00)
    payment_id = db.get_invoice_payments(inv)[0][0]

    db.unassign_invoice_payment(payment_id)

    assert db.get_invoice_payments(inv) == []
    assert db.get_booking_by_id(legacy) is not None


# ── 8. Storniert ─────────────────────────────────────────────────────────────

def test_a_cancelled_invoice_stays_cancelled_when_its_payments_change(db, acct):
    customer = _customer(db)
    inv = _sent(db, 'R-CAN', 119.00, customer_id=customer)
    b1, b2 = _bank(db, acct, 69.00, contact_id=customer), _bank(db, acct, 50.00, date='2026-02-02',
                                                                contact_id=customer)
    _assign(db, inv, b1)
    second = _assign(db, inv, b2)
    db.update_invoice_status(inv, 'cancelled')

    db.unassign_invoice_payment(second)
    assert _invoice_state(db, inv) == ('cancelled', Decimal('50'))
    assert _children(db, b2) == []
    assert db.get_booking_unallocated_amount(b2) == 50                  # die 50 € sind wieder frei …
    assert [c[0] for c in db.get_open_items_for_contact(customer)['credits']] == [b2]   # … Guthaben

    db.delete_transaction(b1)                                           # eine andere Zahlung
    assert _invoice_state(db, inv) == ('cancelled', Decimal('119'))
    assert _euer(db) == {} and db.find_unbalanced_splits(*RANGE) == []


# ── 9. Bankbewegung löschen ──────────────────────────────────────────────────

def _adopt(db, receivable_id, bank_id):
    """Der Zustand, den der frühere Auto-Abgleich hinterließ: Die Forderung hängt als Kind an der
    Bankbewegung (und ist aufgelöst)."""
    con = sqlite3.connect(db.db_name)
    con.execute("UPDATE Bookings SET ParentBooking_ID = ?, Status = 'resolved' WHERE ID = ?",
                (bank_id, receivable_id))
    con.commit()
    con.close()


def test_delete_route_handler_removes_the_assignment_with_its_rows(db, acct, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda: db)
    inv = _sent(db, 'R-HDL', 119.00)
    bank = _bank(db, acct, 119.00)
    payment_id = _assign(db, inv, bank)

    status, _ = handlers.handle_delete_invoice_payment(
        ('{"payment_id": %d}' % payment_id).encode())

    assert status == 200
    assert _children(db, bank) == [] and _invoice_state(db, inv) == ('sent', 119)
    assert handlers.handle_delete_invoice_payment(b'{"payment_id": 99999}')[0] == 500


def test_deleting_the_bank_movement_keeps_an_adopted_receivable(db, acct):
    inv = _sent(db, 'R-ADOPT', 119.00)
    receivable = db.get_receivable_booking(inv)
    bank = _bank(db, acct, 119.00)
    _adopt(db, receivable, bank)

    db.delete_transaction(bank)

    assert db.get_booking_by_id(bank) is None
    kept = db.get_booking_by_id(receivable)
    assert kept is not None and kept[18] is None and kept[19] is None   # abgehängt, nicht mehr aufgelöst
    assert _invoice_state(db, inv) == ('sent', 119)


def test_deleting_the_bank_movement_removes_the_assignment_and_reopens_the_receivable(db, acct):
    inv = _sent(db, 'R-DEL', 119.00, due=YESTERDAY)
    receivable = db.get_receivable_booking(inv)
    bank = _bank(db, acct, 119.00)
    _assign(db, inv, bank)
    _adopt(db, receivable, bank)                    # zusätzlich der Altbestand-Zustand

    db.delete_transaction(bank)

    assert _count(db, 'InvoicePayments') == 0
    assert _raw(db, 'SELECT ID, ParentBooking_ID, Status FROM Bookings') == [(receivable, None, None)]
    assert _invoice_state(db, inv) == ('overdue', 119)
    assert _balance(db, 10000) == 119 and _euer(db) == {}


def test_deleting_a_payment_row_removes_the_whole_assignment(db, acct):
    inv = _sent(db, 'R-ROW', 119.00)
    bank = _bank(db, acct, 119.00)
    _assign(db, inv, bank)
    settlement = _raw(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID',
                      bank)[0][0]

    db.delete_transaction(settlement)

    assert _children(db, bank) == [] and db.get_invoice_payments(inv) == []
    assert db.get_booking_by_id(bank) is not None                       # die Bewegung bleibt
    assert _invoice_state(db, inv) == ('sent', 119) and _euer(db) == {}
    db.delete_transaction(settlement)                                   # nochmal: nichts mehr da


def test_deleting_an_entry_with_its_own_assignment_recalculates_the_invoice(db):
    """Altbestand „Zahlung verbuchen“ ohne Konto: ein Satz mit eigener Zuordnung."""
    inv = _sent(db, 'R-ENT', 119.00)
    entry = db.insert_booking('2026-02-01', 119.00, coa_id=_coa(db, 4400), booking_type='entry')
    db.link_invoice_to_transaction(inv, entry, 119.00)
    assert _invoice_state(db, inv) == ('paid', 0)

    db.delete_transaction(entry)

    assert _count(db, 'InvoicePayments') == 0 and _invoice_state(db, inv) == ('sent', 119)


# ── 10. Auto-Abgleich ────────────────────────────────────────────────────────

def test_auto_link_never_adopts_the_receivable(db, acct):
    inv = _sent(db, 'R-AUTO', 119.00, invoice_date='2026-03-20')
    receivable = db.get_receivable_booking(inv)
    bank = _bank(db, acct, 119.00, date='2026-03-20', recipient='Käufer AG')

    result = db.link_bank_to_entries()

    assert result['linked'] == 0
    assert db.get_booking_by_id(receivable)[18] is None                 # eigenständig geblieben
    assert db.get_child_bookings_for_bank(bank) == []
    assert receivable not in [c[0] for c in db.get_unlinked_entry_bookings()]

    _assign(db, inv, bank)
    assert _euer(db) == {4400: 100.0, 3806: 19.0}
    assert len(_datev(db)) == 3                                         # Forderung, Zahlung, Umbuchung
    assert db.find_unbalanced_splits(*RANGE) == []
    assert _balance(db, 10000) == 0


def test_auto_link_never_adopts_the_receivable_as_part_of_a_document_group(db, acct):
    """Forderung und Zahlungssätze desselben Belegs am selben Tag (WISO): die Sätze werden
    verknüpft, die Forderung nicht."""
    inv = _sent(db, 'R-GRP', 119.00, invoice_date='2026-03-20')
    receivable = db.get_receivable_booking(inv)
    bank = _bank(db, acct, 119.00, date='2026-03-20', recipient='Anderer Name')
    for coa, counter, text in ((1800, 10000, 'Zahlung zu Re. R-GRP'),
                               (4405, 4400, 'Umb. zu Re. R-GRP')):
        db.insert_booking('2026-03-20', 119.00, coa_id=_coa(db, coa), counter_coa_id=_coa(db, counter),
                          booking_type='entry', text=text, document_number='R-GRP')
    con = sqlite3.connect(db.db_name)
    con.execute("UPDATE Bookings SET DateBooking = '2026-03-20' WHERE ID = ?", (receivable,))
    con.commit()
    con.close()

    db.link_bank_to_entries()

    assert len(db.get_child_bookings_for_bank(bank)) == 2
    assert db.get_booking_by_id(receivable)[18] is None


def test_auto_link_still_links_document_groups_of_entries_without_accounts(db, acct):
    """Die Regel „Forderung“ ist nie NULL: Sätze ohne Konto (NULL) sind keine Forderung und
    bleiben für die Gruppenstufen und die Editor-Kandidaten sichtbar."""
    bank = _bank(db, acct, 100.00, date='2026-04-01', recipient='Anderer Name')
    parts = [db.insert_booking('2026-04-01', amount, booking_type='entry', text='Teil',
                               document_number='S-GRP')
             for amount in (60.00, 40.00)]
    loose = db.insert_booking('2026-04-02', 7.00, booking_type='entry', text='ohne Konto')
    assert loose in [c[0] for c in db.get_unlinked_entry_bookings()]

    assert db.link_bank_to_entries()['linked'] == 1

    assert sorted(c[0] for c in db.get_child_bookings_for_bank(bank)) == sorted(parts)


def test_wiso_payment_rows_stay_linkable(db, acct):
    """„1800 an 10000“ (Zahlung) und „10000 an 1800“ (Storno) haben ein Zahlungskonto auf einer
    Seite – sie gehören zur Bankbewegung."""
    pay_bank = _bank(db, acct, 4.10, date='2026-05-20', recipient='Dauerkunde')
    storno_bank = _bank(db, acct, -4.10, date='2026-05-21', recipient='Dauerkunde')
    pay = db.insert_booking('2026-05-20', 4.10, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000),
                            booking_type='entry', recipient_client='Dauerkunde',
                            text='Zahlung zu Re. W1', document_number='W1')
    storno = db.insert_booking('2026-05-21', -4.10, coa_id=_coa(db, 10000),
                               counter_coa_id=_coa(db, 1800), booking_type='entry',
                               recipient_client='Dauerkunde', text='Zahlung zu Re. W1',
                               document_number='W1')

    assert db.link_bank_to_entries()['linked'] == 2

    assert db.get_booking_by_id(pay)[18] == pay_bank
    assert db.get_booking_by_id(storno)[18] == storno_bank


# ── 11. „Zahlung verbuchen“ ──────────────────────────────────────────────────

def _post(**kw):
    base = {'transaction_id': ['0'], 'date': ['2026-02-01'], 'amount': ['119.00']}
    base.update({k: [str(v)] for k, v in kw.items()})
    return base


def test_payment_form_without_a_bank_account_is_rejected_and_saves_nothing(db, acct):
    inv = _sent(db, 'R-FORM', 119.00)
    before = _count(db)

    status, message = handle_add_transaction(db, _post(invoice_id=inv))

    assert status == 400 and 'Bankkonto' in message
    assert _count(db) == before and _count(db, 'InvoicePayments') == 0


@pytest.mark.parametrize('overrides, expected', [
    ({'amount': '0'}, 'Betrag'), ({'amount': '-119.00'}, 'Betrag'), ({'amount': 'abc'}, 'Betrag'),
])
def test_payment_form_with_an_unusable_amount_is_rejected(db, acct, overrides, expected):
    inv = _sent(db, 'R-FORM', 119.00)
    before = _count(db)
    status, message = handle_add_transaction(db, _post(invoice_id=inv, account=acct, **overrides))
    assert status == 400 and expected in message and _count(db) == before


def test_payment_form_for_an_invoice_that_is_not_sent_is_rejected(db, acct):
    inv = _invoice(db, 'R-FIN', 119.00, status='finalized')
    before = _count(db)
    status, message = handle_add_transaction(db, _post(invoice_id=inv, account=acct))
    assert status == 400 and 'versendet' in message and _count(db) == before


def test_payment_form_with_a_bank_account_without_skr_is_rejected(db):
    db.insert_account('Ohne-SKR', 'Ich', 'DE00OHNE', 'BIC', 'Bank', is_cash=0, skr_account=None)
    no_skr = [a[0] for a in db.fetch_accounts() if a[1] == 'Ohne-SKR'][0]
    inv = _sent(db, 'R-FORM', 119.00)
    before = _count(db)
    status, message = handle_add_transaction(db, _post(invoice_id=inv, account=no_skr))
    assert status == 400 and 'SKR-Nummer' in message and _count(db) == before


def test_payment_form_books_settlement_and_transfer_and_ignores_the_posted_revenue_account(db, acct):
    inv = _sent(db, 'R-FORM', 119.00)
    wrong = _coa(db, 4300)                                              # 7 %-Erlöskonto, falsch für 19 %

    status, location = handle_add_transaction(
        db, _post(invoice_id=inv, account=acct, coa_id=wrong, tax_rate=7, tax_amount=7.79))

    assert (status, location) == (303, f'/invoice/edit?id={inv}')
    bank, = [r[0] for r in _raw(db, "SELECT ID FROM Bookings WHERE BookingType = 'bank'")]
    assert _children(db, bank) == [
        (1800, 10000, Decimal('119'), None, 'Zahlung zu Re. R-FORM', PAYMENT_MARK),
        (4405, 4400, Decimal('119'), Decimal('19'), 'Umb. zu Re. R-FORM', PAYMENT_MARK),
    ]
    assert _raw(db, 'SELECT COUNT(*) FROM Bookings WHERE AutoMirror = 1') == [(0,)]
    assert db.get_booking_by_id(bank)[8] == _coa(db, 4400)              # Anzeige: aus der Rechnung
    assert len(_datev(db)) == 3                                         # Forderung, Zahlung, Umbuchung
    assert _euer(db) == {4400: 100.0, 3806: 19.0}                       # Erlös genau einmal
    assert db.find_unbalanced_splits(*RANGE) == []
    assert _balance(db, 10000) == 0


def test_payment_form_before_the_invoice_is_sent_is_refused_and_afterwards_booked_in_parts(db, acct):
    """Teilzahlung vor dem Versand wird abgelehnt („erst versenden“); danach laufen zwei Teile
    sauber durch."""
    inv = _invoice(db, 'R-VOR', 119.00, status='finalized')
    status, message = handle_add_transaction(db, _post(invoice_id=inv, account=acct, amount='50.00'))
    assert status == 400 and 'versendet' in message

    db.update_invoice_status(inv, 'sent')
    db.ensure_receivable_booking(inv)
    assert handle_add_transaction(db, _post(invoice_id=inv, account=acct, amount='50.00'))[0] == 303
    assert handle_add_transaction(db, _post(invoice_id=inv, account=acct, amount='69.00',
                                            date='2026-02-02'))[0] == 303

    assert _invoice_state(db, inv) == ('paid', 0)
    assert _euer(db) == {4400: 100.0, 3806: 19.0}
    assert _balance(db, 10000) == 0 and _balance(db, 4405) == 0
    assert db.find_unbalanced_splits(*RANGE) == []


def test_update_path_names_the_reason_when_the_assignment_fails(db, acct):
    inv = _invoice(db, 'R-NOSEND', 119.00, status='finalized')
    bank = _bank(db, acct, 119.00)

    status, location = handle_add_transaction(
        db, _post(transaction_id=bank, invoice_id=inv, account=acct))

    assert status == 303 and location.startswith(f'/invoice/edit?id={inv}&payment_error=')
    assert 'versendet' in location                       # der Grund steht in der Meldung
    assert db.get_invoice_payments(inv) == []


# ── 12. Kontakt-Saldo ────────────────────────────────────────────────────────

def test_contact_balance_counts_only_sent_invoices_and_ignores_booked_cash_sales(db, acct):
    customer = _customer(db)
    finalized = _invoice(db, 'R-FIN', 119.00, status='finalized', customer_id=customer)
    # Barverkauf über 50 €: schon als Erlös kontiert – kein Guthaben
    cash = _bank(db, acct, 50.00, contact_id=customer)
    db.insert_booking('2026-02-01', 50.00, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1800),
                      booking_type='entry', parent_booking_id=cash, auto_mirror=True)

    items = db.get_open_items_for_contact(customer)

    assert items['open_invoices'] == [] and items['credits'] == []
    assert [r[0] for r in items['unsent_invoices']] == [finalized]
    assert items['saldo'] == 0                                           # vorher: 69

    sent = _sent(db, 'R-SENT', 80.00, customer_id=customer)
    items = db.get_open_items_for_contact(customer)
    assert [r[0] for r in items['open_invoices']] == [sent] and items['saldo'] == Decimal('80')


def test_contact_page_shows_unsent_invoices_apart_from_the_balance(db, acct):
    customer = _customer(db)
    _invoice(db, 'R-FIN', 119.00, status='finalized', customer_id=customer)
    _sent(db, 'R-SENT', 80.00, customer_id=customer)

    html = PageContacts(db, edit_contact_id=customer)

    assert 'Noch nicht versendet (nicht im Saldo)' in html
    assert 'Forderungen aus versendeten Rechnungen' in html


# ── 14. Prüfung ──────────────────────────────────────────────────────────────

def test_inconsistency_check_reports_every_category(db, acct):
    """Je Zustand, den die alten Wege hinterlassen haben, die erwartete Kategorie – und nur
    für die Rechnung, die ihn trägt. Die Zustände entstehen per Roh-SQL bzw. über den
    WISO-Weg (``link_invoice_to_transaction``), nicht über die neue Zuordnung."""
    # R-A: Zuordnung ohne „Bank an Debitoren“; nichts auf das Erlöskonto umgebucht
    a = _sent(db, 'R-A', 119.00)
    db.link_invoice_to_transaction(a, _bank(db, acct, 119.00), 119.00)
    # R-B: die Forderung hängt an einer Bankbewegung
    b = _sent(db, 'R-B', 119.00)
    bank_b = _bank(db, acct, 119.00, date='2026-02-02')
    _adopt(db, db.get_receivable_booking(b), bank_b)
    # R-C: Zahlungssatz unter einer Bankbewegung, zu dem es keine Zuordnung gibt
    _sent(db, 'R-C', 119.00)
    bank_c = _bank(db, acct, 119.00, date='2026-02-03')
    db.insert_booking('2026-02-03', 119.00, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000),
                      booking_type='entry', parent_booking_id=bank_c, text='Zahlung zu Re. R-C',
                      document_number='R-C')
    # R-D: Zuordnung hängt an einem Buchungssatz statt an einer Bankbewegung
    d = _sent(db, 'R-D', 119.00)
    entry = db.insert_booking('2026-02-04', 119.00, coa_id=_coa(db, 4400), booking_type='entry')
    db.link_invoice_to_transaction(d, entry, 119.00)
    # R-E: dasselbe Paar doppelt zugeordnet (und AmountDue nicht nachgezogen)
    e = _sent(db, 'R-E', 238.00)
    bank_e = _bank(db, acct, 238.00, date='2026-02-05')
    con = sqlite3.connect(db.db_name)
    for _ in range(2):
        con.execute('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) '
                    'VALUES (?, ?, 1190000, ?)', (e, bank_e, '2026-02-05'))
    con.commit()
    con.close()
    # R-F: Zuordnung auf einen Ausgang
    f = _sent(db, 'R-F', 119.00)
    db.link_invoice_to_transaction(f, _bank(db, acct, -119.00, date='2026-02-06'), 119.00)
    # R-G: offener Rest passt nicht zu den Zuordnungen
    g = _sent(db, 'R-G', 119.00)
    con = sqlite3.connect(db.db_name)
    con.execute('UPDATE Invoices SET AmountDue = 5 WHERE ID = ?', (g,))
    con.commit()
    con.close()
    # R-H: als Forderung gebucht, Status aber „abgeschlossen“
    _sent(db, 'R-H', 119.00, status='finalized')
    # R-OK: sauber zugeordnet über die neue Funktion
    clean = _sent(db, 'R-OK', 119.00)
    _assign(db, clean, _bank(db, acct, 119.00, date='2026-02-07'))

    findings = db.find_payment_inconsistencies()
    by_category = {}
    for finding in findings:
        by_category.setdefault(finding['category'], set()).add(finding['invoice_number'])

    assert by_category == {
        'no_settlement': {'R-A', 'R-E', 'R-F'},
        'receivable_attached': {'R-B'},
        'orphan_payment_rows': {'R-C'},
        'transfer_mismatch': {'R-A', 'R-D', 'R-E', 'R-F'},
        'payment_on_entry': {'R-D'},
        'duplicate_payment': {'R-E'},
        'outgoing_payment': {'R-F'},
        'status_unsent': {'R-H'},
        'amount_due_mismatch': {'R-E', 'R-G'},
    }
    assert all(f['text'] for f in findings)
    assert all(f['invoice_id'] is not None for f in findings)

    # Auf eine Rechnung eingegrenzt
    only_e = db.find_payment_inconsistencies(e)
    assert {f['category'] for f in only_e} == {'no_settlement', 'transfer_mismatch',
                                               'duplicate_payment', 'amount_due_mismatch'}
    assert all(f['invoice_id'] == e for f in only_e)
    assert db.find_payment_inconsistencies(clean) == []


def test_inconsistency_check_changes_nothing(db, acct):
    a = _sent(db, 'R-A', 119.00, status='finalized')
    db.link_invoice_to_transaction(a, _bank(db, acct, 119.00), 119.00)
    tables = ('Bookings', 'InvoicePayments', 'Invoices')

    def snapshot():
        return {t: _raw(db, f'SELECT * FROM {t} ORDER BY ID') for t in tables}

    before = snapshot()
    assert db.find_payment_inconsistencies() != []
    assert snapshot() == before


def test_inconsistency_hint_and_list_are_shown_and_escaped(db, acct):
    inv = _sent(db, 'R-<b>', 119.00, status='finalized')
    db.link_invoice_to_transaction(inv, _bank(db, acct, 119.00), 119.00)

    page = PageInvoice(db, {}, inv, payment_error='<script>alert(1)</script>')
    assert 'Zahlungszuordnung prüfen' in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page and '<script>alert(1)</script>' not in page

    misc = PageMiscellaneous(db)
    assert 'Zahlungszuordnungen' in misc
    assert 'R-&lt;b&gt;' in misc and 'R-<b>' not in misc
    assert f'/invoice/edit?id={inv}' in misc


def test_miscellaneous_page_reports_a_clean_state(db, acct):
    assert 'Keine Unstimmigkeiten gefunden' in PageMiscellaneous(db)


# ── Seiten ───────────────────────────────────────────────────────────────────

def test_invoice_page_offers_payments_only_after_sending(db, acct):
    finalized = _invoice(db, 'R-FIN', 119.00, status='finalized')
    sent = _sent(db, 'R-SENT', 119.00)
    _bank(db, acct, 119.00)

    assert 'erst zuordnen, wenn die Rechnung versendet ist' in PageInvoice(db, {}, finalized)
    assert f'/transactions?from_invoice={finalized}' not in PageInvoice(db, {}, finalized)
    page = PageInvoice(db, {}, sent)
    assert f'/transactions?from_invoice={sent}' in page
    assert 'linkPayment(' in page and 'button.disabled = true' in page


def test_transactions_page_offers_the_invoice_dropdown_only_for_incoming_bank_movements(db, acct):
    _sent(db, 'R-SENT', 119.00)
    incoming = _bank(db, acct, 119.00)
    outgoing = _bank(db, acct, -119.00, date='2026-02-02')
    cash_sale = _bank(db, acct, 50.00, date='2026-02-03')
    db.insert_booking('2026-02-03', 50.00, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1800),
                      booking_type='entry', parent_booking_id=cash_sale, auto_mirror=True)

    assert 'name="invoice_id"' in PageTransactions(db, edit_transaction_id=incoming)
    assert 'name="invoice_id"' not in PageTransactions(db, edit_transaction_id=outgoing)
    assert 'name="invoice_id"' not in PageTransactions(db, edit_transaction_id=cash_sale)
    assert 'name="invoice_id"' in PageTransactions(db)                  # neue Buchung: Betrag noch offen


def test_payment_form_takes_the_invoice_bank_account(db, acct):
    inv = _sent(db, 'R-ACC', 119.00, bank_account_id=acct)
    html = PageTransactions(db, from_invoice=inv)
    assert f'<option value="{acct}" selected>' in html


# ── HTTP: Ablehnung als Klartext, Meldung maskiert ───────────────────────────

def test_rejected_payment_form_over_http_is_400_with_plain_text(server_port, auto_login):
    books = make_books(auto_login)
    db = books.db
    db.update_invoice_status(books.invoice, 'sent')
    before = _count(db)

    resp = post(server_port, '/transactions/add', csrf=books.csrf,
                form={'transaction_id': '0', 'date': '2026-02-01', 'amount': '119.00',
                      'invoice_id': str(books.invoice)},
                headers={'Origin': own_origin(server_port)})

    assert resp.status == 400
    assert resp.headers['Content-type'].startswith('text/plain')
    assert 'Bankkonto' in resp.text and 'Location' not in resp.headers
    assert _count(db) == before


def test_link_and_delete_routes_over_http_book_and_unbook_the_payment(server_port, auto_login):
    books = make_books(auto_login)
    db = books.db
    db.update_invoice_status(books.invoice, 'sent')
    db.ensure_receivable_booking(books.invoice)
    bank = _bank(db, _account(db), 119.00)

    resp = post_json(server_port, '/invoice/link-payment',
                     {'invoice_id': books.invoice, 'transaction_id': bank}, books.csrf,
                     headers={'Origin': own_origin(server_port)})
    assert resp.status == 200 and json.loads(resp.text) == {'success': True}
    assert len(_children(db, bank)) == 2 and _invoice_state(db, books.invoice) == ('paid', 0)

    again = post_json(server_port, '/invoice/link-payment',
                      {'invoice_id': books.invoice, 'transaction_id': bank}, books.csrf,
                      headers={'Origin': own_origin(server_port)})
    assert again.status == 400 and json.loads(again.text)['success'] is False
    assert len(db.get_invoice_payments(books.invoice)) == 1

    payment_id = db.get_invoice_payments(books.invoice)[0][0]
    resp = post_json(server_port, '/invoice/delete-payment', {'payment_id': payment_id}, books.csrf,
                     headers={'Origin': own_origin(server_port)})
    assert resp.status == 200 and json.loads(resp.text) == {'success': True}
    assert _children(db, bank) == [] and _invoice_state(db, books.invoice)[0] in ('sent', 'overdue')


def test_invoice_page_over_http_shows_the_payment_error_escaped(server_port, auto_login):
    books = make_books(auto_login)
    resp = get(server_port, f'/invoice/edit?id={books.invoice}&payment_error='
                            '%3Cscript%3Ealert(1)%3C%2Fscript%3E')
    assert resp.status == 200
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in resp.text
    assert '<script>alert(1)</script>' not in resp.text


# ── Bausteine ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('current, gross, paid, due, expected', [
    ('sent', 1190000, 1190000, None, 'paid'),
    ('sent', 1190000, 1500000, None, 'paid'),                       # Überzahlung gilt als bezahlt
    ('sent', 1190000, 1189950, None, 'paid'),                       # Rest unter einem Cent
    ('sent', 1190000, 500000, None, 'partial_payment'),
    ('overdue', 1190000, 500000, '2020-01-01', 'partial_payment'),
    ('partial', 1190000, 500000, None, 'partial_payment'),
    ('paid', 1190000, 0, '2099-01-01', 'sent'),
    ('paid', 1190000, 0, '2020-01-01', 'overdue'),
    ('partial_payment', 1190000, 0, None, 'sent'),
    ('sent', 1190000, 0, '2020-01-01', 'sent'),                     # nichts bezahlt, nichts geändert
    ('overdue', 1190000, 0, '2020-01-01', 'overdue'),
    ('finalized', 1190000, 0, None, 'finalized'),                   # setzt nie abgeschlossen/Entwurf
    ('draft', 1190000, 0, None, 'draft'),
    ('cancelled', 1190000, 1190000, None, 'cancelled'),             # verlässt nie storniert
    ('cancelled', 1190000, 0, None, 'cancelled'),
    ('paid', -1190000, -1190000, None, 'paid'),                     # WISO-Gutschrift (Vorzeichen)
    ('sent', -1190000, -500000, None, 'partial_payment'),
])
def test_next_payment_status(current, gross, paid, due, expected):
    assert next_payment_status(current, gross, paid, due, '2026-06-01') == expected


def test_payment_blocker_names_the_reason():
    assert payment_blocker('invoice', 'sent', 1) is None
    assert payment_blocker(None, 'partial', 1) is None             # Altbestand: ohne Typ, Alt-Status
    assert 'nicht gefunden' in payment_blocker('quote', 'sent', 1)
    assert 'versendet' in payment_blocker('invoice', 'finalized', 1)
    assert 'versendet' in payment_blocker('invoice', None, 1)
    assert 'positivem' in payment_blocker('invoice', 'sent', 0)


def test_prorate_rounds_half_up_exactly():
    assert prorate(159700, 333300, 1000000) == 53228
    assert prorate(1, 1, 2) == 1                                    # 0,5 → 1
    assert prorate(-1, 1, 2) == -1                                  # kaufmännisch: weg von 0
    assert prorate(70000, 500000, 1070000) == 32710


def test_link_invoice_to_transaction_still_takes_signed_amounts_without_children(db, acct):
    """Der WISO-Import ordnet mit Vorzeichen zu und legt keine Sätze an."""
    inv = _invoice(db, 'W-1', 119.00, status='paid')
    entry = db.insert_booking('2026-02-01', 119.00, coa_id=_coa(db, 1800),
                              counter_coa_id=_coa(db, 10000), booking_type='entry')
    db.link_invoice_to_transaction(inv, entry, 119.00)
    db.link_invoice_to_transaction(inv, db.insert_booking('2026-02-02', -119.00, booking_type='entry'),
                                   -119.00)
    assert _invoice_state(db, inv) == ('sent', 119)           # Zahlung und Storno heben sich auf
    assert _count(db) == 2                                     # und es entstehen keine neuen Sätze
    with pytest.raises(ValueError):
        db.link_invoice_to_transaction(inv, entry, 1.00)           # dasselbe Paar nicht zweimal
    assert len(db.get_invoice_payments(inv)) == 2
