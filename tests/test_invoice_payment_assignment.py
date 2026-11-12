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
import time
from decimal import Decimal

import pytest

from db import Database
from db.invoice_lock import status_group
from db.invoice_payments import PAYMENT_MARK, next_payment_status, payment_blocker
from db.matching import REVENUE_ACCOUNT_RANGE, receivable_condition
from db.reporting import INCOME_ACCOUNT_RANGE
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


def _exec(db, sql, *params):
    """Roh schreiben (und committen) – für Zustände, die die App selbst nie erzeugt."""
    con = sqlite3.connect(db.db_name)
    try:
        con.execute(sql, params)
        con.commit()
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


def _datev_fields(db):
    """Alle Felder der exportierten Zeilen; der Export darf nicht gesperrt sein."""
    result = handle_datev_export(db, {'date_from': [RANGE[0]], 'date_to': [RANGE[1]]})
    assert isinstance(result[0], bytes), f'DATEV-Export gesperrt: {result}'
    lines = [ln for ln in result[0].decode('cp1252').split('\r\n') if ln]
    return [next(csv.reader([ln], delimiter=';', quotechar='"')) for ln in lines[2:]]


def _datev(db):
    """Exportierte Zeilen als (Betrag, Konto, Gegenkonto); der Export darf nicht gesperrt sein."""
    return [(f[0], f[6], f[7]) for f in _datev_fields(db)]


def _datev_amount(db, number):
    """Summe der Beträge aller DATEV-Zeilen, die das Konto als Konto oder Gegenkonto führen."""
    return sum(Decimal(amount.replace(',', '.')) for amount, konto, gegen in _datev(db)
               if str(number) in (konto, gegen))


def _assign(db, invoice_id, bank_id, amount=None, revenue_coa_id=None):
    result = db.assign_invoice_payment(invoice_id, bank_id, amount, revenue_coa_id)
    assert result.ok, result.message
    return result.payment_id


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
    # Für 5 % gibt es kein Standard-Erlöskonto; der Nutzer wählt eines (4400)
    inv = _sent(db, 'R-5B', 210.00, rate=0.05)                   # kein Wartekonto → keine Forderung
    assert db.get_receivable_booking(inv) is None
    bank = _bank(db, acct, 400.00, date='2026-02-02')

    _assign(db, inv, bank, amount=100.00, revenue_coa_id=_coa(db, 4400))

    assert _children(db, bank) == [
        (4400, 1800, Decimal('100'), Decimal('4.76'), 'Zahlung zu Re. R-5B', PAYMENT_MARK)]
    assert _invoice_state(db, inv) == ('partial_payment', Decimal('110'))
    assert db.get_booking_unallocated_amount(bank) == Decimal('300')
    assert _euer(db) == {4400: 95.24, 3806: 4.76}


# ── Erlöskonto wählen, wenn für den Steuersatz keines hinterlegt ist ─────────

NO_REVENUE_COA = ('Für den Steuersatz dieser Rechnung ist kein Erlöskonto hinterlegt – bitte ein '
                  'Erlöskonto wählen.')
NOT_A_REVENUE_COA = 'Das gewählte Konto ist kein Erlöskonto.'


def _revenue_account(db, number, name):
    """Erlöskonto anlegen (der Seed kennt 4400 und 4300, aber keines für 16 % oder 5 %)."""
    if db.get_coa_id_by_account_number(number) is None:
        db.insert_chart_of_accounts(4, number, name, 'Betriebliche Erträge')
    return db.get_coa_id_by_account_number(number)


# (Satz, Brutto, Erlöskonto) – die Sätze aus der zweiten Jahreshälfte 2020 bzw. ermäßigt alt
UNLISTED_RATES = [
    pytest.param(0.16, 116.00, 4401, id='16-Prozent'),
    pytest.param(0.05, 105.00, 4402, id='5-Prozent'),
]


@pytest.mark.parametrize('rate, gross, number', UNLISTED_RATES)
def test_rate_without_a_revenue_account_is_refused_without_a_choice(db, acct, rate, gross, number):
    """Die App rät nicht: ohne hinterlegtes Konto und ohne Wahl wird nichts geschrieben."""
    inv = _sent(db, 'R-NOCOA', gross, rate=rate)
    bank = _bank(db, acct, gross)
    before = _count(db)

    for choice in (None, ''):
        result = db.assign_invoice_payment(inv, bank, revenue_coa_id=choice)
        assert not result.ok and result.payment_id is None and result.message == NO_REVENUE_COA

    assert db.payment_precheck(inv, acct) == NO_REVENUE_COA
    assert _count(db) == before and _count(db, 'InvoicePayments') == 0
    assert _invoice_state(db, inv) == ('sent', Decimal(str(gross)))


@pytest.mark.parametrize('rate, gross, number', UNLISTED_RATES)
def test_chosen_revenue_account_books_one_row_with_the_tax_share(db, acct, rate, gross, number):
    coa = _revenue_account(db, number, 'Erlöse zum Satz')
    inv = _sent(db, 'R-CHOICE', gross, rate=rate)
    assert db.get_receivable_booking(inv) is None                # kein Wartekonto zu diesem Satz
    bank = _bank(db, acct, gross)
    tax = Decimal(str(round(gross - 100.00, 2)))                 # 100,00 netto

    result = db.assign_invoice_payment(inv, bank, revenue_coa_id=coa)

    assert result.ok and result.revenue_coa_id == coa
    assert _children(db, bank) == [
        (number, 1800, Decimal(str(gross)), tax, 'Zahlung zu Re. R-CHOICE', PAYMENT_MARK)]
    assert _count(db) == 2                                       # Bankbewegung + ein Satz, nichts sonst
    assert _invoice_state(db, inv) == ('paid', 0)
    assert _euer(db) == {number: 100.0, 3806: float(tax)}        # netto und USt, einmal
    assert _datev(db) == [(f'{gross:.2f}'.replace('.', ','), str(number), '1800')]
    assert db.find_unbalanced_splits(*RANGE) == []
    assert db.find_payment_inconsistencies() == []


@pytest.mark.parametrize('rate, gross, number, bu', [
    pytest.param(0.16, 116.00, 4401, '5', id='16-Prozent'),
    pytest.param(0.05, 105.00, 4402, '4', id='5-Prozent'),
])
def test_payment_on_a_temporary_rate_exports_its_own_tax_key(db, acct, rate, gross, number, bu):
    """Die DATEV-Zeile einer Einnahme zu 16 % bzw. 5 % trägt den Schlüssel dieses Satzes (früher den
    für 7 % bzw. gar keinen). Wie alle BU-Schlüssel ist er noch nicht gegen einen echten Stapel
    geprüft."""
    coa = _revenue_account(db, number, 'Erlöse zum Satz')
    inv = _sent(db, 'R-BU', gross, rate=rate)

    _assign(db, inv, _bank(db, acct, gross), revenue_coa_id=coa)

    rows = _datev_fields(db)
    assert [(f[0], f[6], f[7], f[8]) for f in rows] == [
        (f'{gross:.2f}'.replace('.', ','), str(number), '1800', bu)]


def test_partial_payments_at_sixteen_percent_split_the_tax_exactly(db, acct):
    coa = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    inv = _sent(db, 'R-16T', 116.00, rate=0.16)
    first, second = _bank(db, acct, 58.00), _bank(db, acct, 58.00, date='2026-02-02')

    _assign(db, inv, first, revenue_coa_id=coa)
    assert _invoice_state(db, inv) == ('partial_payment', Decimal('58'))
    _assign(db, inv, second, revenue_coa_id=coa)

    assert [c[3] for b in (first, second) for c in _children(db, b)] == \
        [Decimal('8.00'), Decimal('8.00')]
    assert _invoice_state(db, inv) == ('paid', 0)
    assert _euer(db) == {4401: 100.0, 3806: 16.0}
    assert _datev_amount(db, 4401) == Decimal('116.00')


@pytest.mark.parametrize('bad, label', [
    pytest.param(lambda db: db.get_coa_id_by_account_number(6300), 'Aufwandskonto', id='6300'),
    pytest.param(lambda db: db.get_coa_id_by_account_number(1800), 'Bankkonto', id='1800'),
    pytest.param(lambda db: db.get_coa_id_by_account_number(4405), 'Wartekonto', id='4405'),
    pytest.param(lambda db: 999999, 'unbekannte ID', id='unbekannt'),
    pytest.param(lambda db: -1, 'negative ID', id='negativ'),
    pytest.param(lambda db: 'abc', 'keine Zahl', id='Text'),
])
def test_invalid_revenue_choice_is_refused_and_writes_nothing(db, acct, bad, label):
    inv = _sent(db, 'R-BAD16', 116.00, rate=0.16)
    bank = _bank(db, acct, 116.00)
    before = _count(db)

    result = db.assign_invoice_payment(inv, bank, revenue_coa_id=bad(db))

    assert not result.ok and result.message == NOT_A_REVENUE_COA, label
    assert db.payment_precheck(inv, acct, bad(db)) == NOT_A_REVENUE_COA
    assert _count(db) == before and _count(db, 'InvoicePayments') == 0


def test_choice_is_ignored_when_the_rate_has_a_revenue_account(db, acct):
    """Bei 19 % ergibt sich das Konto aus der Rechnung – eine Wahl (auch eine falsche) zählt nicht."""
    other = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    inv = _sent(db, 'R-19W', 119.00)
    bank = _bank(db, acct, 119.00)

    result = db.assign_invoice_payment(inv, bank, revenue_coa_id=other)
    assert result.ok and result.revenue_coa_id == _coa(db, 4400)

    inv2 = _sent(db, 'R-19X', 119.00)
    bank2 = _bank(db, acct, 119.00, date='2026-02-02')
    result = db.assign_invoice_payment(inv2, bank2, revenue_coa_id=_coa(db, 6300))   # ungültig, egal
    assert result.ok and result.revenue_coa_id == _coa(db, 4400)
    assert _euer(db) == {4400: 200.0, 3806: 38.0}


def test_chosen_revenue_account_is_learned_for_the_rate(db, acct):
    """Nach der Zuordnung mit gewähltem Konto liefert ``resolve_revenue_coa`` es für den Satz:
    die nächste 16-%-Rechnung braucht keine Wahl mehr und landet auf demselben Konto."""
    coa = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    first = _sent(db, 'R-L1', 116.00, rate=0.16)
    second = _sent(db, 'R-L2', 116.00, rate=0.16)
    b1, b2 = _bank(db, acct, 116.00), _bank(db, acct, 116.00, date='2026-02-02')
    assert db.revenue_coa_choice_needed(0.16) and db.resolve_revenue_coa(0.16) is None

    assert link_booking_to_invoice_capped(db, first, b1, revenue_coa_id=coa) == (True, None)

    assert db.get_booking_by_id(b1)[8] == coa                    # Anzeige der Bankzeile = die Quelle
    assert db.resolve_revenue_coa(0.16) == coa and not db.revenue_coa_choice_needed(0.16)
    assert link_booking_to_invoice_capped(db, second, b2) == (True, None)        # ohne Wahl
    assert [c[0] for c in _children(db, b2)] == [4401]
    assert _euer(db) == {4401: 200.0, 3806: 32.0}
    assert db.resolve_revenue_coa(0.05) is None                  # andere Sätze lernen nicht mit


def test_a_collective_payment_over_different_rates_leaves_each_rate_on_its_own_account(db, acct):
    """Gelernt wird aus den Sätzen der Zuordnung, nicht aus dem einen Konto der Bankzeile: Nach
    19 % + 16 % auf einer Bewegung führt jeder Satz sein eigenes Konto (früher lernte 16 % das
    Konto 4400 der Bankzeile – und umgekehrt 19 % das gewählte)."""
    chosen = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    bank = _bank(db, acct, 235.00)                                        # 119 + 116
    i19, i16 = _sent(db, 'R-S19', 119.00), _sent(db, 'R-S16', 116.00, rate=0.16)

    assert link_booking_to_invoice_capped(db, i16, bank, revenue_coa_id=chosen) == (True, None)
    assert db.resolve_revenue_coa(0.16) == chosen
    assert link_booking_to_invoice_capped(db, i19, bank) == (True, None)

    assert db.get_booking_by_id(bank)[8] == chosen                         # Anzeige: das eine Konto der Bankzeile
    assert db.resolve_revenue_coa(0.19) == _coa(db, 4400)                  # 19 % bleibt bei seinem Konto
    assert db.resolve_revenue_coa(0.16) == chosen                          # 16 % bei seinem
    assert not db.revenue_coa_choice_needed(0.16)
    assert _euer(db) == {4400: 100.0, 4401: 100.0, 3806: 35.0}


def test_a_collective_payment_over_one_rate_still_teaches_it(db, acct):
    coa = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    bank = _bank(db, acct, 232.00)
    first, second = _sent(db, 'R-T1', 116.00, rate=0.16), _sent(db, 'R-T2', 116.00, rate=0.16)

    assert link_booking_to_invoice_capped(db, first, bank, revenue_coa_id=coa) == (True, None)
    assert link_booking_to_invoice_capped(db, second, bank, revenue_coa_id=coa) == (True, None)

    assert db.resolve_revenue_coa(0.16) == coa


def test_a_bank_row_that_already_has_an_account_keeps_it(db, acct):
    """Trägt die Bankzeile schon ein Konto, bleibt es (Backfill überschreibt nie)."""
    coa = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    inv = _sent(db, 'R-KEEP16', 116.00, rate=0.16)
    bank = _bank(db, acct, 116.00, coa_id=_coa(db, 4400))

    assert link_booking_to_invoice_capped(db, inv, bank, revenue_coa_id=coa) == (True, None)

    assert db.get_booking_by_id(bank)[8] == _coa(db, 4400)
    assert [c[0] for c in _children(db, bank)] == [4401]         # gebucht wird auf die Wahl


# ── Erlöskonto lernen: nur aus den Sätzen der Zuordnung ──────────────────────

def _set_row_account(db, row_id, column, coa):
    con = sqlite3.connect(db.db_name)
    con.execute(f'UPDATE Bookings SET {column} = ? WHERE ID = ?', (coa, row_id))
    con.commit()
    con.close()


def test_learning_reads_the_counter_account_of_the_waiting_account_row_and_skips_the_settlement_row(
        db, acct):
    """Mit Forderung steht das Erlöskonto im Satz „Wartekonto an Erlöskonto“ als Gegenkonto. Der
    Satz „Bank an Debitoren“ trägt das Bankkonto und lehrt nichts – auch nicht, wenn er der
    jüngste Satz ist."""
    special = _revenue_account(db, 4401, 'Erlöse Sonderfall')
    first, second = _sent(db, 'R-U1', 119.00), _sent(db, 'R-U2', 119.00)
    b1, b2 = _bank(db, acct, 119.00), _bank(db, acct, 119.00, date='2026-02-02')
    _assign(db, first, b1)
    transfer = _raw(db, "SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND COA_ID = ?",
                    b1, _coa(db, 4405))[0][0]
    _set_row_account(db, transfer, 'CounterCOA_ID', special)           # der Altbestand lehrt 4401
    assert db.resolve_revenue_coa(0.19) == special

    _assign(db, second, b2)                                            # nimmt 4401 (gelernt)
    assert [c[:2] for c in _children(db, b2)] == [(1800, 10000), (4405, 4401)]
    # Der jüngste markierte Satz ist jetzt „Bank an Debitoren“ (der Umbuchungssatz ist weg)
    _raw_delete = sqlite3.connect(db.db_name)
    _raw_delete.execute("DELETE FROM Bookings WHERE ParentBooking_ID = ? AND COA_ID = ?",
                        (b2, _coa(db, 4405)))
    _raw_delete.commit()
    _raw_delete.close()
    assert db.resolve_revenue_coa(0.19) == special                     # Satz 1 zählt, nicht das Bankkonto


def test_a_wiso_assignment_on_a_bank_account_teaches_nothing(db, acct):
    """Der WISO-Weg ordnet einen Satz „1800 an 10000“ zu: Sein Konto ist das Bankkonto. Früher
    lernte der Steuersatz daraus das Bankkonto – die nächste Zahlung wurde „4405 an 1800“."""
    old = _invoice(db, 'R-W1', 119.00, status='paid')
    wiso_payment = db.insert_booking('2026-01-15', 119.00, coa_id=_coa(db, 1800),
                                     counter_coa_id=_coa(db, 10000), booking_type='entry',
                                     document_number='R-W1')
    db.link_invoice_to_transaction(old, wiso_payment, 119.00)
    assert db.resolve_revenue_coa(0.19) == _coa(db, 4400)

    inv, bank = _sent(db, 'R-W2', 119.00), _bank(db, acct, 119.00)
    _assign(db, inv, bank)

    assert [c[:2] for c in _children(db, bank)] == [(1800, 10000), (4405, 4400)]
    assert _euer(db) == {4400: 100.0, 3806: 19.0}


def test_a_bank_row_with_an_expense_account_teaches_nothing(db, acct):
    """Die Bankzeile war schon kontiert (6815 = Aufwand): Das Konto bleibt Anzeige und wird weder
    gebucht noch für den Steuersatz gelernt."""
    first, second = _sent(db, 'R-E1', 119.00), _sent(db, 'R-E2', 119.00)
    booked = _bank(db, acct, 119.00, coa_id=_coa(db, 6815))
    _assign(db, first, booked)
    assert db.get_booking_by_id(booked)[8] == _coa(db, 6815)
    assert db.resolve_revenue_coa(0.19) == _coa(db, 4400)

    bank = _bank(db, acct, 119.00, date='2026-02-02')
    _assign(db, second, bank)

    assert [c[:2] for c in _children(db, bank)] == [(1800, 10000), (4405, 4400)]
    assert _euer(db) == {4400: 200.0, 3806: 38.0}


def test_a_tax_free_invoice_whose_bank_row_sits_on_a_waiting_account_teaches_nothing(db, acct):
    first = _sent(db, 'R-F1', 100.00, rate=-1, tax_amount=0)
    _assign(db, first, _bank(db, acct, 100.00, coa_id=_coa(db, 4405)))
    assert db.resolve_revenue_coa(-1) == _coa(db, 4185)

    second, bank = _sent(db, 'R-F2', 50.00, rate=-1, tax_amount=0), _bank(
        db, acct, 50.00, date='2026-02-02')
    _assign(db, second, bank)

    assert [c[:2] for c in _children(db, bank)] == [(1800, 10000), (4340, 4185)]
    assert _euer(db) == {4185: 150.0}


def test_a_collective_payment_over_19_and_7_percent_leaves_every_later_invoice_on_its_own_account(
        db, acct):
    """Sammelzahlung 19 % + 7 % auf einer Bewegung; danach weitere 7-%-, 19-%- und §-19-Rechnungen:
    jede landet auf dem Konto ihres Satzes (4400/4300/4185), die EÜR stimmt."""
    def link(invoice, movement):                       # wie die Oberfläche: samt Anzeige der Bankzeile
        assert link_booking_to_invoice_capped(db, invoice, movement) == (True, None)

    bank = _bank(db, acct, 226.00)                                      # 119 + 107
    link(_sent(db, 'R-C19', 119.00), bank)
    link(_sent(db, 'R-C7', 107.00, rate=0.07), bank)
    later7, later19, free = (_sent(db, 'R-C7B', 107.00, rate=0.07), _sent(db, 'R-C19B', 119.00),
                             _sent(db, 'R-C0', 100.00, rate=-1, tax_amount=0))
    banks = [_bank(db, acct, amount, date='2026-02-02') for amount in (107.00, 119.00, 100.00)]
    for invoice, movement in zip((later7, later19, free), banks):
        link(invoice, movement)

    assert [c[:2] for c in _children(db, bank)] == [(1800, 10000), (4405, 4400),
                                                    (1800, 10000), (4345, 4300)]
    assert [c[:2] for c in _children(db, banks[0])] == [(1800, 10000), (4345, 4300)]
    assert [c[:2] for c in _children(db, banks[1])] == [(1800, 10000), (4405, 4400)]
    assert [c[:2] for c in _children(db, banks[2])] == [(1800, 10000), (4340, 4185)]
    assert _euer(db) == {4400: 200.0, 4300: 200.0, 4185: 100.0, 3806: 52.0}
    assert db.find_payment_inconsistencies() == []


def _marked_row(db, bank, number, coa, counter):
    """Ein Satz mit der Marke einer Zuordnung, in einer Form nach Wahl – auch einer, die die App
    selbst nie schreibt (aus dem Editor verbogen)."""
    _exec(db, "INSERT INTO Bookings (DateBooking, Amount, Currency, COA_ID, CounterCOA_ID, "
              "DocumentNumber, BookingType, ParentBooking_ID, AutoMirror, Status) "
              "VALUES ('2026-02-01', 1190000, 'EUR', ?, ?, ?, 'entry', ?, 0, ?)",
          coa, counter, number, bank, PAYMENT_MARK)


@pytest.mark.parametrize('coa, counter, learned', [
    pytest.param(4405, 'special', True, id='Wartekonto-an-Erlös'),
    pytest.param(4345, 'special', True, id='anderes-Wartekonto-an-Erlös'),
    pytest.param('special', 1800, True, id='Erlös-an-Bank'),
    pytest.param('special', 1460, True, id='Erlös-an-Verrechnungskonto'),
    pytest.param(1800, 10000, False, id='Bank-an-Debitoren'),
    pytest.param(4736, 'special', False, id='verbogen-Erlös-an-Erlös'),
    pytest.param('special', 4736, False, id='verbogen-umgekehrt'),
    pytest.param('special', 10000, False, id='Erlös-an-Debitoren'),
    pytest.param('special', 6815, False, id='Erlös-an-Aufwand'),
    pytest.param(6815, 'special', False, id='Aufwand-an-Erlös'),
    pytest.param(4405, None, False, id='Wartekonto-ohne-Gegenkonto'),
])
def test_only_rows_in_the_form_the_app_writes_teach_a_revenue_account(db, acct, coa, counter, learned):
    """Eine Umbuchung lehrt nur, wenn ihr Konto ein Wartekonto ist (gelernt wird das Gegenkonto), ein
    Einzelsatz nur, wenn sein Gegenkonto ein Zahlungskonto ist (gelernt wird das Konto). Was ein
    Nutzer im Split-Editor aus einem markierten Satz gemacht hat, lehrt nichts: Früher wurde aus
    „4405 an 4400“, im Editor zu „4736 an 4400“ geändert, das Konto 4736 für 19 % gelernt."""
    special = _revenue_account(db, 4401, 'Erlöse Sonderfall')
    _sent(db, 'R-N2', 119.00)
    bank = _bank(db, acct, 119.00)
    number = lambda value: special if value == 'special' else (None if value is None else _coa(db, value))

    _marked_row(db, bank, 'R-N2', number(coa), number(counter))

    expected = special if learned else _coa(db, 4400)
    assert db.resolve_revenue_coa(0.19) == expected


def test_a_marked_row_changed_in_the_editor_does_not_steer_the_next_invoice(db, acct):
    first, second = _sent(db, 'R-ED1', 119.00), _sent(db, 'R-ED2', 119.00)
    b1, b2 = _bank(db, acct, 119.00), _bank(db, acct, 119.00, date='2026-02-02')
    _assign(db, first, b1)
    transfer = _raw(db, "SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND COA_ID = ?",
                    b1, _coa(db, 4405))[0][0]
    _set_row_account(db, transfer, 'COA_ID', _coa(db, 4736))             # „4405 an 4400“ → „4736 an 4400“

    assert db.resolve_revenue_coa(0.19) == _coa(db, 4400)
    _assign(db, second, b2)

    assert [c[:2] for c in _children(db, b2)] == [(1800, 10000), (4405, 4400)]   # nicht auf 4736


def test_get_revenue_coas_lists_revenue_accounts_only(db):
    _revenue_account(db, 4401, 'Erlöse 16 % USt')
    numbers = [number for _id, number, _name in db.get_revenue_coas()]
    assert numbers == sorted(numbers) and 4400 in numbers and 4401 in numbers and 4185 in numbers
    assert not {4340, 4345, 4405} & set(numbers)                 # Wartekonten sind keine Erlöse
    assert not {number for number in numbers if not 4000 <= number <= 4999}
    assert REVENUE_ACCOUNT_RANGE == INCOME_ACCOUNT_RANGE == (4000, 4999)


def test_revenue_coa_choice_is_needed_only_for_rates_without_an_account(db):
    assert db.revenue_coa_choice_needed(0.16) and db.revenue_coa_choice_needed(0.05)
    assert db.revenue_coa_choice_needed(5) and db.revenue_coa_choice_needed(16)    # auch als 16
    assert not db.revenue_coa_choice_needed(0.19) and not db.revenue_coa_choice_needed(0.07)
    assert not db.revenue_coa_choice_needed(0) and not db.revenue_coa_choice_needed(-1)


# ── Vorschlag (nur Vorauswahl) ───────────────────────────────────────────────

def _booking_with_rate(db, rate, coa=None, counter=None):
    return db.insert_booking('2026-02-01', 116.00, coa_id=coa, counter_coa_id=counter,
                             tax_rate=rate, booking_type='entry')


def test_suggestion_is_the_most_frequent_revenue_account_of_the_rate(db):
    a, b = _revenue_account(db, 4401, 'Erlöse A'), _revenue_account(db, 4402, 'Erlöse B')
    bank, expense = _coa(db, 1800), _coa(db, 6300)
    assert db.suggest_revenue_coa(0.16) is None                  # nichts gefunden

    _booking_with_rate(db, 0.16, coa=b, counter=bank)            # als Konto
    _booking_with_rate(db, 0.16, coa=bank, counter=b)            # als Gegenkonto
    _booking_with_rate(db, 0.16, coa=a, counter=bank)
    assert db.suggest_revenue_coa(0.16) == b                     # 2 gegen 1

    # Nicht zählen: andere Sätze, Aufwandskonten, Wartekonten
    for _ in range(5):
        _booking_with_rate(db, 0.19, coa=a, counter=bank)
        _booking_with_rate(db, 0.16, coa=expense, counter=bank)
        _booking_with_rate(db, 0.16, coa=_coa(db, 4405), counter=bank)
    assert db.suggest_revenue_coa(0.16) == b
    assert db.suggest_revenue_coa(0.05) is None


def test_suggestion_on_a_tie_takes_the_smallest_account_number(db):
    a, b = _revenue_account(db, 4401, 'Erlöse A'), _revenue_account(db, 4402, 'Erlöse B')
    for coa in (b, a, b, a):
        _booking_with_rate(db, 0.16, coa=coa, counter=_coa(db, 1800))
    assert db.suggest_revenue_coa(0.16) == a


def test_suggestion_compares_the_rate_like_the_resolution_does(db):
    """Der Satz steht mal als 0.16, mal als 16 – beides ist derselbe Steuersatz."""
    a = _revenue_account(db, 4401, 'Erlöse A')
    _booking_with_rate(db, 16, coa=a, counter=_coa(db, 1800))
    assert db.suggest_revenue_coa(0.16) == a and db.suggest_revenue_coa(16) == a


def test_suggestion_changes_nothing(db):
    a = _revenue_account(db, 4401, 'Erlöse A')
    _booking_with_rate(db, 0.16, coa=a, counter=_coa(db, 1800))
    before = {t: _raw(db, f'SELECT * FROM {t} ORDER BY 1') for t in
              ('Bookings', 'ChartOfAccounts', 'InvoicePayments', 'Invoices')}
    db.suggest_revenue_coa(0.16)
    db.get_revenue_coas()
    db.revenue_coa_choice_needed(0.16)
    assert before == {t: _raw(db, f'SELECT * FROM {t} ORDER BY 1') for t in before}


# ── Formular „Zahlung verbuchen“ und Verknüpfen: Erlöskonto ──────────────────

def test_payment_form_without_a_choice_is_refused_when_no_revenue_account_exists(db, acct):
    _revenue_account(db, 4401, 'Erlöse 16 % USt')
    inv = _sent(db, 'R-F16', 116.00, rate=0.16)
    before = _count(db)

    for fields, expected in (({}, NO_REVENUE_COA), ({'revenue_coa_id': ''}, NO_REVENUE_COA),
                             ({'revenue_coa_id': 'abc'}, NOT_A_REVENUE_COA),
                             ({'revenue_coa_id': _coa(db, 6300)}, NOT_A_REVENUE_COA)):
        status, message = handle_add_transaction(
            db, _post(invoice_id=inv, account=acct, amount='116.00', **fields))
        assert (status, message) == (400, expected)

    assert _count(db) == before and _count(db, 'InvoicePayments') == 0     # nichts gespeichert


def test_payment_form_with_a_choice_books_on_that_account_and_the_next_one_needs_none(db, acct):
    coa = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    first = _sent(db, 'R-F16A', 116.00, rate=0.16)
    second = _sent(db, 'R-F16B', 116.00, rate=0.16)

    status, location = handle_add_transaction(
        db, _post(invoice_id=first, account=acct, amount='116.00', revenue_coa_id=coa))
    assert (status, location) == (303, f'/invoice/edit?id={first}')
    status, location = handle_add_transaction(
        db, _post(invoice_id=second, account=acct, amount='116.00', date='2026-02-02'))
    assert (status, location) == (303, f'/invoice/edit?id={second}')      # ohne Wahl: gelernt

    assert _euer(db) == {4401: 200.0, 3806: 32.0}
    assert _raw(db, 'SELECT COUNT(*) FROM Bookings WHERE AutoMirror = 1') == [(0,)]
    assert [_invoice_state(db, i) for i in (first, second)] == [('paid', 0), ('paid', 0)]


def test_update_path_names_the_missing_revenue_account(db, acct):
    inv = _sent(db, 'R-U16', 116.00, rate=0.16)
    bank = _bank(db, acct, 116.00)

    status, location = handle_add_transaction(
        db, _post(transaction_id=bank, invoice_id=inv, account=acct, amount='116.00'))

    assert status == 303 and location.startswith(f'/invoice/edit?id={inv}&payment_error=')
    assert db.get_invoice_payments(inv) == []


# ── Seiten: das Feld erscheint nur, wenn es gebraucht wird ───────────────────

FIELD = 'id="revenueCoa"'


def test_invoice_page_offers_the_revenue_account_only_without_a_stored_one(db, acct):
    _revenue_account(db, 4401, 'Erlöse 16 % USt')
    sixteen = _sent(db, 'R-P16', 116.00, rate=0.16)
    nineteen = _sent(db, 'R-P19', 119.00)
    free = _sent(db, 'R-P00', 100.00, rate=-1, tax_amount=0)
    _bank(db, acct, 116.00)

    page = PageInvoice(db, {}, sixteen)
    assert FIELD in page and 'name="revenue_coa_id"' in page and ' required>' in page
    assert ('Für 16 % ist kein Erlöskonto hinterlegt. Die Wahl gilt künftig als Vorgabe für '
            'diesen Steuersatz.') in page
    assert '<option value="">— bitte wählen —</option>' in page
    assert '4401 Erlöse 16 % USt</option>' in page
    assert '4405 ' not in page.split(FIELD)[1].split('</select>')[0]       # kein Wartekonto
    assert 'revenueCoaChoice()' in page and 'bookPayment(this)' in page

    assert FIELD not in PageInvoice(db, {}, nineteen)
    assert FIELD not in PageInvoice(db, {}, free)


def test_invoice_page_preselects_the_suggestion_and_stops_asking_once_learned(db, acct):
    a, b = _revenue_account(db, 4401, 'Erlöse A'), _revenue_account(db, 4402, 'Erlöse B')
    for coa in (b, b, a):
        _booking_with_rate(db, 0.16, coa=coa, counter=_coa(db, 1800))
    inv = _sent(db, 'R-PRE16', 116.00, rate=0.16)
    bank = _bank(db, acct, 116.00)

    field = PageInvoice(db, {}, inv).split(FIELD)[1].split('</select>')[0]
    assert f'<option value="{b}" selected>' in field and f'<option value="{a}" selected>' not in field

    assert link_booking_to_invoice_capped(db, inv, bank, revenue_coa_id=b) == (True, None)
    again = _sent(db, 'R-PRE16B', 116.00, rate=0.16)
    assert FIELD not in PageInvoice(db, {}, again)


def test_invoice_page_escapes_account_names(db, acct):
    _revenue_account(db, 4401, '<b>Erlöse</b> & mehr')
    inv = _sent(db, 'R-ESC', 116.00, rate=0.16)
    page = PageInvoice(db, {}, inv)
    assert '4401 &lt;b&gt;Erlöse&lt;/b&gt; &amp; mehr' in page and '<b>Erlöse</b> & mehr' not in page


def test_payment_mask_offers_the_revenue_account_in_the_same_case_only(db, acct):
    _revenue_account(db, 4401, 'Erlöse 16 % USt')
    sixteen = _sent(db, 'R-M16', 116.00, rate=0.16)
    nineteen = _sent(db, 'R-M19', 119.00)

    page = PageTransactions(db, from_invoice=sixteen)
    assert FIELD in page and 'name="revenue_coa_id"' in page and ' required>' in page
    assert 'Für 16 % ist kein Erlöskonto hinterlegt.' in page
    assert 'name="coa_id"' not in page and 'name="tax_rate"' not in page    # sonst bleibt die Maske schlank

    plain = PageTransactions(db, from_invoice=nineteen)
    assert FIELD not in plain and 'revenue_coa_id' not in plain
    assert 'revenue_coa_id' not in PageTransactions(db)                     # neue Buchung
    bank = _bank(db, acct, 116.00)
    assert 'revenue_coa_id' not in PageTransactions(db, edit_transaction_id=bank)


def test_payment_mask_takes_a_valid_preselection_from_the_invoice_page(db, acct):
    a, b = _revenue_account(db, 4401, 'Erlöse A'), _revenue_account(db, 4402, 'Erlöse B')
    _booking_with_rate(db, 0.16, coa=a, counter=_coa(db, 1800))              # Vorschlag wäre a
    inv = _sent(db, 'R-M16B', 116.00, rate=0.16)

    chosen = PageTransactions(db, from_invoice=inv, revenue_coa_id=b)
    assert f'<option value="{b}" selected>' in chosen and f'<option value="{a}" selected>' not in chosen
    ignored = PageTransactions(db, from_invoice=inv, revenue_coa_id=_coa(db, 6300))   # kein Erlöskonto
    assert f'<option value="{a}" selected>' in ignored                       # dann der Vorschlag


# ── HTTP: /invoice/link-payment mit und ohne Erlöskonto ──────────────────────

def test_link_payment_route_takes_the_revenue_account(server_port, auto_login):
    books = make_books(auto_login)
    db = books.db
    coa = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    inv = _sent(db, 'R-H16', 116.00, rate=0.16)
    bank = _bank(db, _account(db), 116.00)
    headers = {'Origin': own_origin(server_port)}
    before = _count(db)

    def link(**extra):
        return post_json(server_port, '/invoice/link-payment',
                         {'invoice_id': inv, 'transaction_id': bank, **extra}, books.csrf, headers)

    without = link()                                                    # kein Konto hinterlegt, keine Wahl
    assert without.status == 400
    assert json.loads(without.text) == {'success': False, 'error': NO_REVENUE_COA}
    invalid = link(revenue_coa_id=_coa(db, 6300))
    assert invalid.status == 400 and json.loads(invalid.text)['error'] == NOT_A_REVENUE_COA
    assert _count(db) == before and _count(db, 'InvoicePayments') == 0

    not_a_number = link(revenue_coa_id='abc')                           # keine Ganzzahl: 400 wie bei den anderen Feldern
    assert not_a_number.status == 400
    assert json.loads(not_a_number.text) == {'success': False, 'error': 'Ungültige Anfrage.'}
    assert _count(db) == before

    ok = link(revenue_coa_id=coa)
    assert ok.status == 200 and json.loads(ok.text) == {'success': True}
    assert [c[0] for c in _children(db, bank)] == [4401] and _invoice_state(db, inv) == ('paid', 0)
    assert db.get_booking_by_id(bank)[8] == coa                         # gelernt: Bankzeile trägt das Konto


def test_link_payment_route_ignores_the_choice_for_19_percent(server_port, auto_login):
    books = make_books(auto_login)
    db = books.db
    other = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    db.update_invoice_status(books.invoice, 'sent')
    db.ensure_receivable_booking(books.invoice)
    bank = _bank(db, _account(db), 119.00)

    resp = post_json(server_port, '/invoice/link-payment',
                     {'invoice_id': books.invoice, 'transaction_id': bank, 'revenue_coa_id': other},
                     books.csrf, {'Origin': own_origin(server_port)})

    assert resp.status == 200
    assert _euer(db) == {4400: 100.0, 3806: 19.0}


def test_rejected_payment_form_over_http_names_the_missing_revenue_account(server_port, auto_login):
    books = make_books(auto_login)
    db = books.db
    inv = _sent(db, 'R-H16F', 116.00, rate=0.16)
    before = _count(db)

    resp = post(server_port, '/transactions/add', csrf=books.csrf,
                form={'transaction_id': '0', 'date': '2026-02-01', 'amount': '116.00',
                      'account': str(_account(db)), 'invoice_id': str(inv)},
                headers={'Origin': own_origin(server_port)})

    assert resp.status == 400 and resp.text == NO_REVENUE_COA
    assert _count(db) == before


# ── 5. Ablehnungen ───────────────────────────────────────────────────────────

def _no_change(db, call, expected_in_message):
    bookings, payments = _count(db), _count(db, 'InvoicePayments')
    result = call()
    assert not result.ok and result.payment_id is None and result.revenue_coa_id is None
    assert expected_in_message in result.message, result.message
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
    assert status_group(_invoice_state(db, inv)[0]) != 'editable'  # eingefroren, nicht mehr änderbar
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
    # Dubletten gibt es nur in einem Bestand aus der Zeit vor dem eindeutigen Index (2.1.0)
    con.execute('DROP INDEX IF EXISTS ux_invoicepayments_invoice_booking')
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
        'transfer_mismatch': {'R-A', 'R-E', 'R-F'},        # nicht R-D: keine Bankbewegung, nur payment_on_entry
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

    misc = PageMiscellaneous(db, check_payments=True)
    assert 'Zahlungszuordnungen' in misc
    assert 'R-&lt;b&gt;' in misc and 'R-<b>' not in misc
    assert f'/invoice/edit?id={inv}' in misc


def test_miscellaneous_page_reports_a_clean_state(db, acct):
    assert 'Keine Unstimmigkeiten gefunden' in PageMiscellaneous(db, check_payments=True)


# ── Seiten ───────────────────────────────────────────────────────────────────

def test_invoice_page_offers_payments_only_after_sending(db, acct):
    finalized = _invoice(db, 'R-FIN', 119.00, status='finalized')
    sent = _sent(db, 'R-SENT', 119.00)
    _bank(db, acct, 119.00)

    assert 'erst zuordnen, wenn die Rechnung versendet ist' in PageInvoice(db, {}, finalized)
    assert f'href="/transactions?from_invoice={finalized}"' not in PageInvoice(db, {}, finalized)
    page = PageInvoice(db, {}, sent)
    assert f'href="/transactions?from_invoice={sent}"' in page
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


# ═════════════════════════════════════════════════════════════════════════════
# Zweiter Nachtrag: Befunde der Prüfung (A2–A6, B1–B3)
# ═════════════════════════════════════════════════════════════════════════════

# ── A2: Forderung nach dem Entfernen der letzten Zahlung ─────────────────────

def _cancel(db, invoice_id):
    """Stornieren wie die Oberfläche: Status setzen, Forderungsbuchung ausrichten."""
    db.update_invoice_status(invoice_id, 'cancelled')
    handlers._sync_receivable_booking(db, invoice_id, 'cancelled')


def _end_state(db, invoice_id, number):
    return (_invoice_state(db, invoice_id), db.get_receivable_booking(invoice_id),
            _raw(db, 'SELECT COUNT(*) FROM Bookings WHERE DocumentNumber = ?', number)[0][0],
            _raw(db, 'SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ?', invoice_id)[0][0])


def test_cancelling_and_removing_the_last_payment_end_in_the_same_state_in_every_order(db, acct):
    """Storniert mit Teilzahlung, dann Zuordnung entfernt: Die Forderung blieb stehen (10000 +119,
    DATEV-Zeile) – in umgekehrter Reihenfolge wurde sie gelöscht. Jetzt gleicher Endzustand."""
    a, b, c = (_sent(db, number, 119.00) for number in ('R-ORD-A', 'R-ORD-B', 'R-ORD-C'))
    ba, bb, bc = (_bank(db, acct, 50.00, date=day)
                  for day in ('2026-02-01', '2026-02-02', '2026-02-03'))
    pa, pb = _assign(db, a, ba), _assign(db, b, bb)
    _assign(db, c, bc)

    _cancel(db, a)                                   # A: erst stornieren …
    assert db.get_receivable_booking(a) is not None  # … die Forderung bleibt (Teilzahlung)
    db.unassign_invoice_payment(pa)                  # … dann die Zahlung entfernen
    db.unassign_invoice_payment(pb)                  # B: erst die Zahlung entfernen …
    assert db.get_receivable_booking(b) is not None  # … die Forderung bleibt (offen)
    _cancel(db, b)                                   # … dann stornieren
    _cancel(db, c)                                   # C: stornieren, dann die Bewegung löschen
    db.delete_transaction(bc)

    expected = (('cancelled', Decimal('119')), None, 0, 0)
    assert [_end_state(db, inv, number) for inv, number in
            ((a, 'R-ORD-A'), (b, 'R-ORD-B'), (c, 'R-ORD-C'))] == [expected] * 3
    assert _balance(db, 10000) == 0 and _balance(db, 4405) == 0
    assert _euer(db) == {}
    exported = handle_datev_export(db, {'date_from': [RANGE[0]], 'date_to': [RANGE[1]]})
    assert exported[0] == 303 and 'Keine+Buchungen' in exported[1]       # kein Satz mehr im Stapel
    assert db.find_payment_inconsistencies() == []


def test_the_receivable_of_a_cancelled_invoice_stays_while_another_payment_remains(db, acct):
    inv = _sent(db, 'R-ORD-D', 119.00)
    first, second = _bank(db, acct, 60.00), _bank(db, acct, 59.00, date='2026-02-02')
    p1, p2 = _assign(db, inv, first), _assign(db, inv, second)
    _cancel(db, inv)

    db.unassign_invoice_payment(p1)
    assert db.get_receivable_booking(inv) is not None            # eine Zahlung bleibt
    db.unassign_invoice_payment(p2)

    assert _end_state(db, inv, 'R-ORD-D') == (('cancelled', Decimal('119')), None, 0, 0)


@pytest.mark.parametrize('status', ['draft', 'finalized'])
def test_the_receivable_goes_with_the_last_payment_of_an_invoice_that_is_not_sent(db, acct, status):
    """Dieselbe Regel wie beim Statuswechsel: Nur ab „versendet“ gibt es eine Forderung."""
    inv = _sent(db, 'R-ORD-E', 119.00)
    payment = _assign(db, inv, _bank(db, acct, 50.00))
    _exec(db, 'UPDATE Invoices SET Status = ? WHERE ID = ?', status, inv)

    db.unassign_invoice_payment(payment)

    assert db.get_receivable_booking(inv) is None


def test_the_receivable_of_a_sent_invoice_survives_removing_its_last_payment(db, acct):
    inv = _sent(db, 'R-ORD-F', 119.00)
    payment = _assign(db, inv, _bank(db, acct, 50.00))

    db.unassign_invoice_payment(payment)

    assert db.get_receivable_booking(inv) is not None and _invoice_state(db, inv)[0] == 'sent'


# ── A4: Steuer des schließenden Teils bei Altbestand ─────────────────────────

def _legacy_partial(db, acct, number, document_number, two_rows=True):
    """Eine Teilzahlung über 50 € aus dem Altbestand: Zuordnung ohne Marke, Sätze mit der
    Steuer 7,98 (der anteiligen von 19,00). Returns die Rechnung."""
    inv = _sent(db, number, 119.00)                                    # 19,00 USt
    bank = _bank(db, acct, 50.00)
    db.link_invoice_to_transaction(inv, bank, 50.00)
    rows = ([(1800, 10000, None), (4405, 4400, 7.98)] if two_rows else [(4400, 1800, 7.98)])
    for coa, counter, tax in rows:
        db.insert_booking('2026-02-01', 50.00, coa_id=_coa(db, coa), counter_coa_id=_coa(db, counter),
                          booking_type='entry', parent_booking_id=bank, tax_rate=0.19 if tax else None,
                          tax_amount=tax, document_number=document_number)
    return inv


@pytest.mark.parametrize('document_number, two_rows', [
    pytest.param('R-A4', False, id='ein-Satz'),
    pytest.param('FREMDE-NR', True, id='fremde-Beleg-Nr'),
    pytest.param(None, True, id='ohne-Beleg-Nr'),
])
def test_closing_part_takes_the_prorated_tax_of_a_legacy_partial_payment(
        db, acct, document_number, two_rows):
    """Die Altzahlung hat keine markierten Sätze: ihre Steuer ist der anteilige Betrag, auch wenn
    ihre Sätze eine andere oder keine Beleg-Nr. tragen (früher wurde sie nicht gefunden und die
    Rechnungssteuer doppelt gebucht)."""
    inv = _legacy_partial(db, acct, 'R-A4', document_number, two_rows)

    bank = _bank(db, acct, 69.00, date='2026-02-02')
    _assign(db, inv, bank)

    assert [c[3] for c in _children(db, bank)] == [None, Decimal('11.02')]   # 19,00 − 7,98
    assert _invoice_state(db, inv) == ('paid', 0)
    assert _euer(db) == {4400: 100.0, 3806: 19.0}                             # 7,98 + 11,02


def test_closing_part_takes_the_booked_tax_of_marked_rows_not_a_recomputed_one(db, acct):
    """Hat eine Zuordnung markierte Sätze, zählt deren tatsächlich gebuchte Steuer."""
    inv = _sent(db, 'R-A4M', 119.00)
    first = _bank(db, acct, 50.00)
    _assign(db, inv, first)
    _exec(db, "UPDATE Bookings SET TaxAmount = 80000 WHERE ParentBooking_ID = ? "
              "AND TaxAmount IS NOT NULL", first)                              # 8,00 statt 7,98

    last = _bank(db, acct, 69.00, date='2026-02-02')
    _assign(db, inv, last)

    assert [c[3] for c in _children(db, last)] == [None, Decimal('11.00')]    # 19,00 − 8,00
    assert _euer(db) == {4400: 100.0, 3806: 19.0}


# ── A5: Die Forderung wird wie überall gesucht ───────────────────────────────

def test_a_wiso_reversal_with_the_invoice_number_is_not_taken_for_the_receivable(db, acct):
    """Ein Storno „10000 an 1800“ mit der Rechnungsnummer ist keine Forderung: Gäbe es nur ihn,
    bucht die Zuordnung den Erlös ohne Forderung – nicht „Bank an 10000“ mit dem Bankkonto als
    Gegenseite."""
    inv = _invoice(db, 'R-STO', 119.00)                                # versendet, ohne Forderung
    db.insert_booking('2026-02-01', 119.00, coa_id=_coa(db, 10000), counter_coa_id=_coa(db, 1800),
                      booking_type='entry', document_number='R-STO')
    assert db.get_receivable_booking(inv) is None
    bank = _bank(db, acct, 119.00)

    _assign(db, inv, bank)

    assert [c[:4] for c in _children(db, bank)] == [(4400, 1800, Decimal('119'), Decimal('19'))]


def test_the_real_receivable_is_found_even_behind_a_reversal_with_the_same_number(db, acct):
    inv = _invoice(db, 'R-STO2', 119.00)
    storno = db.insert_booking('2026-02-01', 119.00, coa_id=_coa(db, 10000),
                               counter_coa_id=_coa(db, 1800), booking_type='entry',
                               document_number='R-STO2')               # die kleinere ID
    receivable = db.ensure_receivable_booking(inv)
    assert receivable is not None and receivable != storno
    assert db.get_receivable_booking(inv) == receivable
    bank = _bank(db, acct, 119.00)

    _assign(db, inv, bank)

    assert [c[:2] for c in _children(db, bank)] == [(1800, 10000), (4405, 4400)]
    db.remove_receivable_booking(inv)                                  # lässt den Storno stehen
    assert db.get_booking_by_id(storno) is not None


# ── A6: Betrag auf Cent runden ───────────────────────────────────────────────

def test_the_requested_amount_is_rounded_to_cents_before_it_is_capped(db, acct):
    inv = _sent(db, 'R-RND', 119.00)
    bank = _bank(db, acct, 119.00)

    _assign(db, inv, bank, amount=118.995)                              # 118,995 → 119,00

    assert db.get_invoice_payments(inv)[0][3] == Decimal('119')
    assert _invoice_state(db, inv) == ('paid', 0)
    assert _balance(db, 10000) == 0                                     # kein Bruchteil eines Cents bleibt
    amounts = [amount for amount, _konto, _gegen in _datev(db)]         # der Export ist nicht gesperrt
    assert len(amounts) == 3 and all(len(a.split(',')[-1]) == 2 for a in amounts)
    assert db.find_payment_inconsistencies() == []


@pytest.mark.parametrize('amount, allocated', [(33.335, '33.34'), (33.334, '33.33'),
                                               ('0.005', '0.01'), (0.01, '0.01')])
def test_rounding_half_up_to_the_cent(db, acct, amount, allocated):
    inv = _sent(db, 'R-RND2', 119.00)
    bank = _bank(db, acct, 119.00)

    _assign(db, inv, bank, amount=amount)

    assert db.get_invoice_payments(inv)[0][3] == Decimal(allocated)


@pytest.mark.parametrize('amount', [0.004, 0.0049, -0.005])
def test_an_amount_that_rounds_to_zero_is_refused(db, acct, amount):
    inv = _sent(db, 'R-RND3', 119.00)
    bank = _bank(db, acct, 119.00)
    _no_change(db, lambda: db.assign_invoice_payment(inv, bank, amount=amount), 'größer 0')


# ── B2: Forderung = 10000 im Soll ────────────────────────────────────────────

_DEBTOR, _WAITING, _BANK_ID, _SALES = 10, 20, 9, 30


@pytest.mark.parametrize('coa, counter, amount, document, expected', [
    pytest.param(_DEBTOR, _WAITING, 100, None, True, id='10000-an-Wartekonto'),
    pytest.param(_WAITING, _DEBTOR, -100, None, True, id='negativ-gebucht'),
    pytest.param(_DEBTOR, None, 100, None, True, id='ohne-Gegenkonto'),
    pytest.param(_WAITING, _DEBTOR, 100, None, False, id='Erlösminderung-an-10000'),
    pytest.param(_SALES, _DEBTOR, 100, None, False, id='Skonto-an-10000'),
    pytest.param(_DEBTOR, _WAITING, -100, None, False, id='10000-im-Haben'),
    pytest.param(_BANK_ID, _DEBTOR, 100, None, False, id='Bank-an-10000'),
    pytest.param(_DEBTOR, _BANK_ID, 100, None, False, id='Storno-10000-an-Bank'),
    pytest.param(None, None, 100, None, False, id='ohne-Konten'),
    pytest.param(_DEBTOR, _WAITING, 0, None, False, id='Betrag-0'),
    pytest.param(_DEBTOR, _WAITING, None, None, False, id='ohne-Betrag'),
    # Gutschrift: Die Forderung einer Rechnung mit negativem Brutto ist „10000 an Wartekonto“ mit
    # negativem Betrag – 10000 steht im Soll, nur das Vorzeichen ist umgekehrt
    pytest.param(_DEBTOR, _WAITING, -100, 'GS-1', True, id='Gutschrift'),
    pytest.param(_DEBTOR, None, -100, 'GS-1', True, id='Gutschrift-ohne-Gegenkonto'),
    pytest.param(_DEBTOR, _WAITING, -100, 'R-1', False, id='negativ-zu-positiver-Rechnung'),
    pytest.param(_DEBTOR, _WAITING, -100, 'R-0', False, id='negativ-zu-Rechnung-ueber-0'),
    pytest.param(_DEBTOR, _WAITING, -100, 'UNBEKANNT', False, id='negativ-ohne-Rechnung'),
    pytest.param(_DEBTOR, _WAITING, -100, None, False, id='negativ-ohne-Beleg-Nr'),
    pytest.param(_DEBTOR, _BANK_ID, -100, 'GS-1', False, id='Gutschrift-Zahlungskonto-Gegenseite'),
    pytest.param(_BANK_ID, _DEBTOR, -100, 'GS-1', False, id='Gutschrift-Zahlungskonto-vorne'),
    pytest.param(_DEBTOR, _WAITING, 0, 'GS-1', False, id='Gutschrift-Betrag-0'),
    pytest.param(_DEBTOR, _WAITING, None, 'GS-1', False, id='Gutschrift-ohne-Betrag'),
    pytest.param(_WAITING, _DEBTOR, 100, 'GS-1', False, id='Erlösminderung-zur-Gutschrift'),
])
def test_receivable_condition_means_10000_on_the_debit_side(coa, counter, amount, document, expected):
    con = sqlite3.connect(':memory:')
    try:
        con.execute('CREATE TABLE Bookings (COA_ID INTEGER, CounterCOA_ID INTEGER, Amount INTEGER, '
                    'DocumentNumber TEXT)')
        con.execute('CREATE TABLE Invoices (InvoiceNumber TEXT, SumGross INTEGER)')
        con.executemany('INSERT INTO Invoices VALUES (?, ?)',
                        [('GS-1', -1190000), ('R-1', 1190000), ('R-0', 0)])
        con.execute('INSERT INTO Bookings VALUES (?, ?, ?, ?)', (coa, counter, amount, document))
        sql, params = receivable_condition([_DEBTOR], [_BANK_ID])
        matches = con.execute(f'SELECT COUNT(*) FROM Bookings WHERE {sql}', params).fetchone()[0]
        others = con.execute(f'SELECT COUNT(*) FROM Bookings WHERE NOT {sql}', params).fetchone()[0]
        aliased_sql, aliased_params = receivable_condition([_DEBTOR], [_BANK_ID], alias='r')
        aliased = con.execute(f'SELECT COUNT(*) FROM Bookings r WHERE {aliased_sql}',
                              aliased_params).fetchone()[0]
    finally:
        con.close()
    assert (matches, others) == ((1, 0) if expected else (0, 1))        # nie NULL
    assert aliased == matches                                           # mit und ohne Alias dasselbe


# ── Gutschrift: Forderung mit negativem Betrag ───────────────────────────────

def _receivable_rows(db, number):
    return _raw(db, "SELECT COA_ID, CounterCOA_ID, Amount FROM Bookings WHERE DocumentNumber = ? "
                    "AND BookingType = 'entry'", number)


def test_a_credit_note_keeps_exactly_one_receivable_over_every_status_change(db):
    """Die Forderung einer Rechnung über −119 € ist „10000 an Wartekonto“ mit negativem Betrag. Sie
    muss als Forderung erkannt werden: Sonst bucht jeder Statuswechsel eine weitere, und zurück auf
    Entwurf entfernt keine."""
    inv = _sent(db, 'GS-1', -119.00)
    assert _receivable_rows(db, 'GS-1') == [(_coa(db, 10000), _coa(db, 4405), -1190000)]
    assert db.get_receivable_booking(inv) is not None

    for status in ('overdue', 'sent', 'paid', 'sent', 'overdue', 'sent'):
        db.update_invoice_status(inv, status)
        handlers._sync_receivable_booking(db, inv, status)
        assert len(_receivable_rows(db, 'GS-1')) == 1, status

    for status in ('draft', 'finalized', 'cancelled'):
        db.update_invoice_status(inv, 'sent')
        handlers._sync_receivable_booking(db, inv, 'sent')
        assert len(_receivable_rows(db, 'GS-1')) == 1, status
        db.update_invoice_status(inv, status)
        handlers._sync_receivable_booking(db, inv, status)
        assert _receivable_rows(db, 'GS-1') == [], status               # zurück: keine Forderung mehr
    assert _balance(db, 10000) == 0


def test_auto_link_never_adopts_the_receivable_of_a_credit_note(db, acct):
    inv = _sent(db, 'GS-AUTO', -119.00, invoice_date='2026-03-20')
    receivable = db.get_receivable_booking(inv)
    assert receivable is not None
    refund = _bank(db, acct, -119.00, date='2026-03-20', recipient='Käufer AG')

    result = db.link_bank_to_entries()

    assert result['linked'] == 0
    assert db.get_booking_by_id(receivable)[18] is None                 # eigenständig geblieben
    assert db.get_child_bookings_for_bank(refund) == []
    assert receivable not in [c[0] for c in db.get_unlinked_entry_bookings()]


def test_deleting_a_movement_keeps_the_receivable_of_a_credit_note(db, acct):
    inv = _sent(db, 'GS-DEL', -119.00)
    receivable = db.get_receivable_booking(inv)
    refund = _bank(db, acct, -119.00)
    _adopt(db, receivable, refund)
    assert [f['category'] for f in db.find_payment_inconsistencies(inv)
            if f['category'] == 'receivable_attached'] == ['receivable_attached']   # sie ist erkannt

    db.delete_transaction(refund)

    assert db.get_booking_by_id(refund) is None
    kept = db.get_booking_by_id(receivable)
    assert kept is not None and kept[18] is None and kept[19] is None   # abgehängt und wieder offen
    assert _receivable_rows(db, 'GS-DEL') == [(_coa(db, 10000), _coa(db, 4405), -1190000)]


def test_a_negative_10000_row_without_a_matching_credit_note_is_not_a_receivable(db, acct):
    """„10000 an Erlöse“ mit negativem Betrag zu einer Rechnung mit positivem Brutto oder ohne
    Rechnung ist keine Forderung (10000 steht dort im Haben): Sie geht mit ihrer Bewegung und ist
    ein Editor-Kandidat."""
    inv = _sent(db, 'R-POSITIV', 119.00)
    real = db.get_receivable_booking(inv)
    bank = _bank(db, acct, 100.00)
    attached = db.insert_booking('2026-02-01', -100.00, coa_id=_coa(db, 10000),
                                 counter_coa_id=_coa(db, 4400), booking_type='entry',
                                 parent_booking_id=bank, document_number='R-POSITIV')
    loose = db.insert_booking('2026-02-02', -50.00, coa_id=_coa(db, 10000),
                              counter_coa_id=_coa(db, 4400), booking_type='entry',
                              document_number='OHNE-RECHNUNG')
    assert db.get_receivable_booking(inv) == real                        # die echte, nicht die verirrte
    assert loose in [c[0] for c in db.get_unlinked_entry_bookings()]
    assert [f for f in db.find_payment_inconsistencies() if f['category'] == 'receivable_attached'] == []

    db.delete_transaction(bank)

    assert db.get_booking_by_id(attached) is None                        # ging mit der Bewegung
    assert db.get_booking_by_id(real) is not None


def _discount_case(db, acct):
    """Rechnung über 119 €; die Zahlung über 117 € und 2 € Skonto („Erlösminderung an 10000“)
    hängen unter der Bankbewegung."""
    inv = _sent(db, 'R-SKO', 119.00)
    bank = _bank(db, acct, 117.00)
    pay = db.insert_booking('2026-02-01', 117.00, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000),
                            booking_type='entry', parent_booking_id=bank, document_number='R-SKO',
                            text='Zahlung zu Re. R-SKO')
    discount = db.insert_booking('2026-02-01', 2.00, coa_id=_coa(db, 4736),
                                 counter_coa_id=_coa(db, 10000), booking_type='entry',
                                 parent_booking_id=bank, document_number='R-SKO', text='Skonto')
    return inv, bank, pay, discount


def test_a_discount_row_is_deleted_with_its_movement_and_the_receivable_stays(db, acct):
    inv, bank, pay, discount = _discount_case(db, acct)
    receivable = db.get_receivable_booking(inv)

    db.delete_transaction(bank)

    assert db.get_booking_by_id(bank) is None
    assert db.get_booking_by_id(pay) is None and db.get_booking_by_id(discount) is None
    kept = db.get_booking_by_id(receivable)
    assert kept is not None and kept[18] is None                         # die Forderung, eigenständig


def test_the_check_list_does_not_report_a_discount_row_as_an_attached_receivable(db, acct):
    inv, bank, _pay, _discount = _discount_case(db, acct)
    assert [f for f in db.find_payment_inconsistencies() if f['category'] == 'receivable_attached'] == []

    # Die echte Forderung unter einer Bewegung wird dagegen weiter gemeldet
    _adopt(db, db.get_receivable_booking(inv), bank)
    assert [f['category'] for f in db.find_payment_inconsistencies(inv)
            if f['category'] == 'receivable_attached'] == ['receivable_attached']


def test_a_standalone_discount_row_is_a_candidate_for_linking_but_the_receivable_is_not(db, acct):
    inv = _sent(db, 'R-SKO2', 119.00)
    discount = db.insert_booking('2026-02-01', 2.00, coa_id=_coa(db, 4736),
                                 counter_coa_id=_coa(db, 10000), booking_type='entry',
                                 document_number='R-SKO2', text='Skonto')
    candidates = [c[0] for c in db.get_unlinked_entry_bookings()]
    assert discount in candidates and db.get_receivable_booking(inv) not in candidates


# ── Stufe 7: erst auflösen, wenn die Zahlungen die Forderung ausgleichen ─────

def test_auto_link_resolves_the_receivable_only_when_the_payments_cover_it(db, acct):
    inv = _sent(db, 'R-S7', 119.00)
    receivable = db.get_receivable_booking(inv)
    first, second = _bank(db, acct, 50.00), _bank(db, acct, 69.00, date='2026-02-02')

    _assign(db, inv, first)
    assert db.link_bank_to_entries()['resolved'] == 0                    # nach der Teilzahlung noch offen
    assert db.get_booking_by_id(receivable)[19] is None

    last = _assign(db, inv, second)
    assert db.link_bank_to_entries()['resolved'] == 1
    assert db.get_booking_by_id(receivable)[19] == 'resolved'
    assert db.link_bank_to_entries()['resolved'] == 0                    # nichts mehr zu tun

    db.unassign_invoice_payment(last)                                    # die letzte Zahlung geht
    assert db.get_booking_by_id(receivable)[19] is None                  # wieder offen


def test_removing_a_payment_keeps_the_receivable_resolved_while_the_rest_still_covers_it(db, acct):
    """Eine Überzahlung über zwei Zuordnungen: Fällt die kleinere weg, deckt die andere weiter."""
    inv = _sent(db, 'R-S7B', 119.00)
    receivable = db.get_receivable_booking(inv)
    big = _bank(db, acct, 119.00)
    _assign(db, inv, big)
    db.link_bank_to_entries()
    assert db.get_booking_by_id(receivable)[19] == 'resolved'
    small = db.insert_booking('2026-02-02', 5.00, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000),
                              booking_type='entry', parent_booking_id=_bank(db, acct, 5.00),
                              document_number='R-S7B')                     # eine weitere Zahlung (Altbestand)

    other = _assign(db, _sent(db, 'R-S7C', 119.00), _bank(db, acct, 119.00, date='2026-02-03'))
    db.unassign_invoice_payment(other)                                   # irgendeine andere Rechnung

    assert db.get_booking_by_id(receivable)[19] == 'resolved'            # unberührt
    assert db.get_booking_by_id(small) is not None


# ── WISO-Bestand: die Prüfliste bleibt leer ──────────────────────────────────

def _wiso_like_books(db, acct):
    """Das, was der WISO-Import hinterlässt: Sätze mit Quellklammer (``SourceGroup``) und
    Zuordnungen über ``link_invoice_to_transaction`` – eine Überweisung und eine Barzahlung ohne
    Bankbewegung. Alles bezahlt, nichts davon hat die App selbst gebucht."""
    banked = _invoice(db, 'W-1', 119.00, status='paid')
    db.insert_booking('2026-01-01', 119.00, coa_id=_coa(db, 10000), counter_coa_id=_coa(db, 4400),
                      booking_type='entry', document_number='W-1', tax_rate=0.19, tax_amount=19.00)
    bank = _bank(db, acct, 119.00, date='2026-01-15')
    settlement = db.insert_booking('2026-01-15', 119.00, coa_id=_coa(db, 1800),
                                   counter_coa_id=_coa(db, 10000), booking_type='entry',
                                   parent_booking_id=bank, document_number='W-1')
    transfer = db.insert_booking('2026-01-15', 119.00, coa_id=_coa(db, 4405),
                                 counter_coa_id=_coa(db, 4400), booking_type='entry',
                                 parent_booking_id=bank, document_number='W-1')
    db.link_invoice_to_transaction(banked, bank, 119.00)

    cash = _invoice(db, 'W-2', 50.00, status='paid')
    db.insert_booking('2026-01-02', 50.00, coa_id=_coa(db, 10000), counter_coa_id=_coa(db, 4400),
                      booking_type='entry', document_number='W-2', tax_rate=0.19, tax_amount=7.98)
    cash_payment = db.insert_booking('2026-01-20', 50.00, coa_id=_coa(db, 1600),
                                     counter_coa_id=_coa(db, 10000), booking_type='entry',
                                     document_number='W-2')
    db.link_invoice_to_transaction(cash, cash_payment, 50.00)
    # Eine Überweisung, deren Zahlungssatz keine Beleg-Nr. trägt: Den „Satz Bank an Debitoren“
    # findet die Prüfung nicht über die Nummer – die Quellklammer sagt, dass er dem Import gehört
    nameless = _invoice(db, 'W-3', 80.00, status='paid')
    db.insert_booking('2026-01-03', 80.00, coa_id=_coa(db, 10000), counter_coa_id=_coa(db, 4400),
                      booking_type='entry', document_number='W-3', tax_rate=0.19, tax_amount=12.77)
    bank3 = _bank(db, acct, 80.00, date='2026-01-25')
    nameless_payment = db.insert_booking('2026-01-25', 80.00, coa_id=_coa(db, 1800),
                                         counter_coa_id=_coa(db, 10000), booking_type='entry',
                                         parent_booking_id=bank3)
    db.link_invoice_to_transaction(nameless, bank3, 80.00)
    for row in (settlement, transfer, cash_payment, nameless_payment):
        _exec(db, "UPDATE Bookings SET SourceGroup = 'WISO-1' WHERE ID = ?", row)
    return banked, cash, nameless, nameless_payment


def test_a_wiso_like_stock_has_an_empty_check_list(db, acct):
    banked, cash, nameless, _row = _wiso_like_books(db, acct)
    assert all(_invoice_state(db, inv) == ('paid', 0) for inv in (banked, cash, nameless))
    assert _count(db, 'InvoicePayments') == 3

    assert db.find_payment_inconsistencies() == []
    for inv in (banked, cash, nameless):
        assert db.find_payment_inconsistencies(inv) == []


def test_the_wiso_exemption_is_the_source_group_not_a_blanket_silence(db, acct):
    """Ohne Quellklammer wäre die Überweisung ohne Beleg-Nr. eine App-Zuordnung und fiele auf: kein
    Satz „Bank an Debitoren“ mit der Rechnungsnummer, nichts auf das Erlöskonto umgebucht."""
    banked, cash, nameless, row = _wiso_like_books(db, acct)
    _exec(db, 'UPDATE Bookings SET SourceGroup = NULL WHERE ID = ?', row)

    assert {f['category'] for f in db.find_payment_inconsistencies(nameless)} == {
        'no_settlement', 'transfer_mismatch'}
    assert db.find_payment_inconsistencies(banked) == []
    assert db.find_payment_inconsistencies(cash) == []             # Barzahlung: keine Bankbewegung


# ── Neue Kategorie: „Bank an Debitoren“ ohne Forderung ───────────────────────

def test_settlement_rows_without_a_receivable_are_reported(db, acct):
    inv = _sent(db, 'R-NOREC', 119.00)
    bank = _bank(db, acct, 119.00)
    _assign(db, inv, bank)
    assert db.find_payment_inconsistencies() == []

    db.delete_transaction(db.get_receivable_booking(inv))               # die Forderung geht von Hand

    findings = db.find_payment_inconsistencies()
    assert [(f['category'], f['invoice_number']) for f in findings] == [
        ('settlement_without_receivable', 'R-NOREC')]
    assert 'Haben-Überhang' in findings[0]['text'] and '119' in findings[0]['text']
    assert db.find_payment_inconsistencies(inv) == findings


def test_a_receivable_that_is_only_a_reversal_does_not_count_as_a_receivable(db, acct):
    inv = _sent(db, 'R-NOREC2', 119.00)
    _assign(db, inv, _bank(db, acct, 119.00))
    db.delete_transaction(db.get_receivable_booking(inv))
    db.insert_booking('2026-02-02', 119.00, coa_id=_coa(db, 10000), counter_coa_id=_coa(db, 1800),
                      booking_type='entry', document_number='R-NOREC2')  # Storno: 10000 im Soll, aber Bank

    assert [f['category'] for f in db.find_payment_inconsistencies()] == ['settlement_without_receivable']


# ── A3: bankwirksame Erlössätze ohne Zuordnung ───────────────────────────────

def _legacy_revenue_case(db, acct, variant):
    inv = _sent(db, 'R-LEG', 119.00)
    bank = _bank(db, acct, 119.00)
    coa, counter, number = _coa(db, 4400), _coa(db, 1800), 'R-LEG'
    if variant == 'bank_first':
        coa, counter = counter, coa
    elif variant == 'waiting_account':
        coa, counter = _coa(db, 4405), _coa(db, 4400)
    elif variant == 'expense':
        coa = _coa(db, 6815)
    elif variant == 'other_number':
        number = 'KEINE-RECHNUNG'
    row = db.insert_booking('2026-02-01', 119.00, coa_id=coa, counter_coa_id=counter,
                            booking_type='entry', parent_booking_id=bank,
                            text='Frei gewählter Text', document_number=number)
    if variant == 'source_group':
        _exec(db, "UPDATE Bookings SET SourceGroup = 'G-1' WHERE ID = ?", row)
    if variant == 'with_assignment':
        db.link_invoice_to_transaction(inv, bank, 119.00)
    if variant == 'quote_number':
        _exec(db, "UPDATE Invoices SET DocumentType = 'quote' WHERE ID = ?", inv)
    return inv, row


@pytest.mark.parametrize('variant, reported', [
    pytest.param('revenue_first', True, id='Erlös-an-Bank'),
    pytest.param('bank_first', True, id='Bank-an-Erlös'),
    pytest.param('with_assignment', False, id='mit-Zuordnung'),
    pytest.param('source_group', False, id='mit-Quellklammer'),
    pytest.param('waiting_account', False, id='Wartekonto-an-Erlös'),
    pytest.param('expense', False, id='Aufwand-an-Bank'),
    pytest.param('other_number', False, id='keine-Rechnungsnummer'),
    pytest.param('quote_number', False, id='Nummer-eines-Angebots'),
])
def test_check_reports_bank_effective_revenue_rows_that_have_no_assignment(db, acct, variant, reported):
    """Altbestand „Zahlung verbuchen mit Konto“ nach dem Entfernen der Zuordnung: der Erlössatz
    steht noch unter der Bewegung, ohne Marke und mit beliebigem Text."""
    inv, row = _legacy_revenue_case(db, acct, variant)

    orphans = [f for f in db.find_payment_inconsistencies() if f['category'] == 'orphan_payment_rows']

    assert bool(orphans) == reported
    if reported:
        assert [f['invoice_number'] for f in orphans] == ['R-LEG'] and f'#{row}' in orphans[0]['text']
        assert [f['category'] for f in db.find_payment_inconsistencies(inv)] == ['orphan_payment_rows']


def test_a_row_the_check_reports_once_is_not_reported_twice(db, acct):
    """Ein markierter Satz ohne Zuordnung trifft zugleich die Marke, den Text und die Form
    „Erlöskonto an Bank“ – er steht trotzdem nur einmal in der Liste."""
    inv = _sent(db, 'R-ONCE', 119.00, rate=0.16)
    bank = _bank(db, acct, 116.00)
    coa = _revenue_account(db, 4401, 'Erlöse 16 % USt')
    _assign(db, inv, bank, revenue_coa_id=coa)
    _exec(db, 'DELETE FROM InvoicePayments')

    assert [f['category'] for f in db.find_payment_inconsistencies()
            if f['category'] == 'orphan_payment_rows'] == ['orphan_payment_rows']


# ── B1: Die Prüfliste bleibt auf einem großen Bestand schnell ────────────────

def _synthetic_books(db, acct, invoices, banks, assigned):
    """Ein Bestand ohne Unstimmigkeit: ``invoices`` versendete Rechnungen mit Forderung, ``banks``
    Bankbewegungen, die ersten ``assigned`` Rechnungen sind ganz bezahlt (Zuordnung, Satz „Bank an
    Debitoren“ und Umbuchung)."""
    debtor, waiting, revenue, bank_coa = (_coa(db, n) for n in (10000, 4405, 4400, 1800))
    con = sqlite3.connect(db.db_name)
    try:
        con.executemany(
            'INSERT INTO Invoices (InvoiceNumber, InvoiceDate, SellerName, SellerCompany, BuyerName, '
            'BuyerCompany, TaxRate, SumNet, TaxAmount, SumGross, AmountDue, Status) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [(f'R{n:05d}', '2026-01-01', 'Vera Verkäufer', 'Verkäufer GmbH', 'Kim Käufer', 'Käufer AG',
              0.19, 1000000, 190000, 1190000, 0 if n < assigned else 1190000,
              'paid' if n < assigned else 'sent') for n in range(invoices)])
        con.executemany(
            "INSERT INTO Bookings (DateBooking, DateTax, RecipientClient, COA_ID, CounterCOA_ID, Amount, "
            "Currency, TaxRate, TaxAmount, Text, DocumentNumber, BookingType, AutoMirror) "
            "VALUES ('2026-01-01', '2026-01-01', 'Käufer AG', ?, ?, 1190000, 'EUR', 0.19, 190000, ?, ?, "
            "'entry', 0)", [(debtor, waiting, f'Rechnung R{n:05d}', f'R{n:05d}') for n in range(invoices)])
        con.executemany(
            "INSERT INTO Bookings (DateBooking, DateTax, RecipientClient, Account_ID, Amount, Currency, "
            "Text, BookingType) VALUES ('2026-02-01', '2026-02-01', 'Käufer AG', ?, 1190000, 'EUR', ?, "
            "'bank')", [(acct, f'Zahlung {n}') for n in range(banks)])
        bank_ids = [r[0] for r in con.execute("SELECT ID FROM Bookings WHERE BookingType = 'bank' "
                                              "ORDER BY ID")]
        invoice_ids = [r[0] for r in con.execute('SELECT ID FROM Invoices ORDER BY ID')]
        con.executemany('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) '
                        "VALUES (?, ?, 1190000, '2026-02-01')",
                        [(invoice_ids[n], bank_ids[n]) for n in range(assigned)])
        child = ("INSERT INTO Bookings (DateBooking, DateTax, RecipientClient, COA_ID, CounterCOA_ID, "
                 "Amount, Currency, TaxRate, TaxAmount, Text, DocumentNumber, BookingType, "
                 "ParentBooking_ID, AutoMirror, Status) VALUES ('2026-02-01', '2026-02-01', 'Käufer AG', "
                 "?, ?, 1190000, 'EUR', ?, ?, ?, ?, 'entry', ?, 0, 'invoice_payment')")
        con.executemany(child, [(bank_coa, debtor, None, None, f'Zahlung zu Re. R{n:05d}',
                                 f'R{n:05d}', bank_ids[n]) for n in range(assigned)])
        con.executemany(child, [(waiting, revenue, 0.19, 190000, f'Umb. zu Re. R{n:05d}',
                                 f'R{n:05d}', bank_ids[n]) for n in range(assigned)])
        con.commit()
    finally:
        con.close()
    return bank_ids


def test_the_check_list_stays_fast_on_a_large_stock_and_still_finds_a_defect(db, acct):
    """1500 Rechnungen mit Forderung, 1500 Bewegungen, 400 Zuordnungen. Die Abfrage „Zuordnung ohne
    Ausgleichssatz“ verband früher alle Forderungen mit allen Bewegungen und brauchte hier über
    20 Sekunden (bei 4000/4000/1000 rund 100); jetzt Bruchteile einer Sekunde. Die Grenze von
    10 Sekunden ist großzügig, damit der Test auf einem langsamen Rechner nicht flackert."""
    _synthetic_books(db, acct, invoices=1500, banks=1500, assigned=400)

    started = time.monotonic()
    assert db.find_payment_inconsistencies() == []
    clean_run = time.monotonic() - started

    _exec(db, "DELETE FROM Bookings WHERE Text = 'Zahlung zu Re. R00007'")          # ein Defekt
    started = time.monotonic()
    findings = db.find_payment_inconsistencies()
    defect_run = time.monotonic() - started
    assert [(f['category'], f['invoice_number']) for f in findings] == [('no_settlement', 'R00007')]

    started = time.monotonic()
    assert [f['category'] for f in db.find_payment_inconsistencies(
        _raw(db, "SELECT ID FROM Invoices WHERE InvoiceNumber = 'R00007'")[0][0])] == ['no_settlement']
    single_run = time.monotonic() - started

    assert clean_run < 10 and defect_run < 10 and single_run < 10, (clean_run, defect_run, single_run)


def test_the_check_list_stays_fast_when_most_of_the_stock_is_defective(db, acct):
    """Der schlechte Fall: Fast jede Zuordnung ohne Ausgleichssatz und jeder Rest falsch."""
    _synthetic_books(db, acct, invoices=1500, banks=1500, assigned=400)
    _exec(db, "DELETE FROM Bookings WHERE Text LIKE 'Zahlung zu Re. %'")
    _exec(db, 'UPDATE Invoices SET AmountDue = 5')

    started = time.monotonic()
    findings = db.find_payment_inconsistencies()
    elapsed = time.monotonic() - started

    counts = {c: sum(1 for f in findings if f['category'] == c) for c in
              ('no_settlement', 'amount_due_mismatch')}
    assert counts == {'no_settlement': 400, 'amount_due_mismatch': 1500}
    assert elapsed < 10, elapsed


# ── Sonstiges: die Prüfliste läuft nur auf Klick ─────────────────────────────

def test_miscellaneous_page_runs_the_check_only_on_request(db, acct, monkeypatch):
    calls = []
    monkeypatch.setattr(Database, 'find_payment_inconsistencies',
                        lambda self, *args, **kwargs: calls.append(1) or [])

    page = PageMiscellaneous(db)
    assert calls == []
    assert 'Zahlungszuordnungen prüfen' in page and 'name="check_payments"' in page
    assert 'method="GET" action="/miscellaneous"' in page
    assert 'Keine Unstimmigkeiten' not in page

    page = PageMiscellaneous(db, check_payments=True)
    assert calls == [1] and 'Keine Unstimmigkeiten gefunden' in page


def test_miscellaneous_route_checks_only_with_the_query_flag(server_port, auto_login, monkeypatch):
    calls = []
    monkeypatch.setattr(Database, 'find_payment_inconsistencies',
                        lambda self, *args, **kwargs: calls.append(1) or [])

    assert get(server_port, '/miscellaneous').status == 200 and calls == []
    resp = get(server_port, '/miscellaneous?check_payments=1')
    assert resp.status == 200 and calls == [1] and 'Keine Unstimmigkeiten gefunden' in resp.text
    resp = get(server_port, '/miscellaneous?check_payments=0&export=ok')
    assert resp.status == 200 and calls == [1]                          # kein „1“: keine Prüfung


# ── Seite der Rechnung: Bestätigungstext beim Entfernen ──────────────────────

def test_removal_confirmation_says_the_app_removes_the_rows_it_created(db, acct):
    inv = _sent(db, 'R-CONF', 119.00)
    _assign(db, inv, _bank(db, acct, 50.00))

    page = PageInvoice(db, {}, inv)

    assert ('Zahlungsverknüpfung wirklich entfernen? Die von der App dafür angelegten '
            'Buchungssätze werden mit entfernt.') in page


# ── B3: unbrauchbarer Betrag im Buchungsformular ─────────────────────────────

BAD_AMOUNTS = ['inf', '-inf', 'Infinity', 'nan', 'NaN', '1e30', '1e12', '1e999', '', 'abc']


@pytest.mark.parametrize('amount', BAD_AMOUNTS)
@pytest.mark.parametrize('with_invoice', [True, False], ids=['Zahlung', 'Buchung'])
def test_an_unusable_amount_is_answered_with_a_400_in_plain_text(db, acct, amount, with_invoice):
    inv = _sent(db, 'R-INF', 119.00)
    fields = {'account': acct, 'amount': amount}
    if with_invoice:
        fields['invoice_id'] = inv
    before = _count(db)

    status, message = handle_add_transaction(db, _post(**fields))

    assert (status, message) == (400, 'Der Betrag ist keine gültige Zahl.')
    assert _count(db) == before and _count(db, 'InvoicePayments') == 0


@pytest.mark.parametrize('amount', ['inf', 'nan', '1e30'])
def test_an_unusable_amount_on_an_existing_movement_changes_nothing(db, acct, amount):
    inv = _sent(db, 'R-INF2', 119.00)
    bank = _bank(db, acct, 119.00)
    before = _raw(db, 'SELECT * FROM Bookings ORDER BY ID')

    status, message = handle_add_transaction(
        db, _post(transaction_id=bank, invoice_id=inv, account=acct, amount=amount))

    assert (status, message) == (400, 'Der Betrag ist keine gültige Zahl.')
    assert _raw(db, 'SELECT * FROM Bookings ORDER BY ID') == before


@pytest.mark.parametrize('amount', ['inf', 'nan', '1e30'])
def test_an_unusable_amount_over_http_is_400_without_the_name_of_an_exception(
        server_port, auto_login, amount):
    books = make_books(auto_login)
    resp = post(server_port, '/transactions/add', csrf=books.csrf,
                form={'transaction_id': '0', 'date': '2026-02-01', 'amount': amount,
                      'invoice_id': str(books.invoice)},
                headers={'Origin': own_origin(server_port)})

    assert resp.status == 400 and resp.headers['Content-type'].startswith('text/plain')
    assert resp.text == 'Der Betrag ist keine gültige Zahl.'
    assert 'Decimal' not in resp.text and 'class' not in resp.text
