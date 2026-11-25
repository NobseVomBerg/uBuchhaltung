# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Skonto bei der Zahlungszuordnung: Buchen (``assign_invoice_payment(…, skonto=True)``).

Eine Zahlung, die eine Rechnung mit Skonto schließt, bucht den Rest als Erlösschmälerung aus – drei markierte Sätze unter der Bankbewegung (mit Forderung):

1. „Bank an Debitoren“ über Geld plus Skonto (119,00), 2. „Wartekonto an Erlöskonto“ über dasselbe mit dem Rest der Rechnungssteuer, 3. „Skonto-Konto an Bank“ mit dem
negativen Skonto und seiner Steuer (−2,38 / −0,38); ohne Forderung zwei („Erlöskonto an Bank“, „Skonto-Konto an Bank“). ``InvoicePayments.Amount`` bleibt das Geld (116,62),
``SkontoAmount`` ist der Rest (2,38). Geprüft wird:

* jede Form Spalte für Spalte (19 %, 7 %, 0 %, § 19 vor und ab 2026, mit und ohne Forderung, Teilzahlung und schließende Zahlung, ein Rest unter dem Skonto);
* jede Ablehnung ohne Schreiben und ohne neues Konto (4730/4731/4736 fehlen vorher und nachher), die Reihenfolge der Sätze, das Netz in der Transaktion;
* Status „bezahlt“, offener Betrag 0, Stufe 7 des Abgleichs, das gelernte Erlöskonto, das Replay-Log, das lazy angelegte Skonto-Konto;
* Entfernen (Zuordnung, Bewegung, ✕ am Skonto-Satz) stellt jede Spalte wieder her; „Öffnen und unverändert speichern“ im Bank-Editor ändert nichts;
* ``payment_precheck`` und ``link_booking_to_invoice_capped`` mit Skonto.

Ohne ``skonto`` bleibt alles, wie es war (die Tests der Zahlungszuordnung und die Schnappschüsse); hier wird nur das Neue geprüft. Alle Namen und Beträge sind erfunden.
"""
import os
import sqlite3
from contextlib import closing
from decimal import Decimal

import pytest

import userctx
from db.invoice_payments import (NOTHING_TO_ASSIGN_MESSAGE, PAYMENT_CHANGED_MESSAGE, PAYMENT_MARK, SKONTO_ACCOUNTS, SKONTO_GROUP_RATE, SKONTO_NO_REST,
                                 SKONTO_NO_TERMS, SKONTO_RATE, SKONTO_TOO_LARGE, TAX_ENTRY_CONTRADICTION)
from server.handlers import link_booking_to_invoice_capped
from tests import installation
from tests.entry_form_roundtrip import differences, open_form, snapshot, submit

RANGE = ('2025-01-01', '2025-12-31')
SKONTO_NUMBERS = (4730, 4731, 4736)
MONEY = Decimal('0.0001')


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _raw(db, sql, *params):
    with closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql, params).fetchall()


def _exec(db, sql, *params):
    with closing(sqlite3.connect(db.db_name)) as con:
        con.execute(sql, params)
        con.commit()


def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _account(db, name='Probe-Giro', skr=1800):
    if not [a for a in db.fetch_accounts() if a[1] == name]:
        db.insert_account(name, 'Ich', 'DE02', 'BIC', 'Bank', is_cash=0, skr_account=skr)
    return [a for a in db.fetch_accounts() if a[1] == name][0][0]


def _invoice(db, number='SK-1', gross=119.0, rate=0.19, status='sent', day='2025-03-03', skonto=(14, 2.0), **over):
    """Eine Rechnung über ``gross`` € mit Skonto-Bedingungen ``(Tage, Prozent)`` (``None``: keine)."""
    net = round(gross / (1 + rate), 2) if rate > 0 else gross
    data = {'invoice_number': number, 'invoice_date': day, 'payment_due_date': '2099-12-31',
            'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH', 'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
            'tax_rate': rate, 'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross, 'amount_due': gross, 'status': status,
            'skonto_days': skonto[0] if skonto else None, 'skonto_percent': skonto[1] if skonto else None}
    data.update(over)
    return db.insert_invoice(data)


def _sent(db, *args, **kw):
    """Versendete Rechnung samt Forderungsbuchung (Debitoren an Wartekonto) wie beim Versenden."""
    invoice_id = _invoice(db, *args, **kw)
    db.ensure_receivable_booking(invoice_id)
    return invoice_id


def _mixed(db, number='SK-M', second_rate=0.07):
    """Ein Beleg mit Steuersatz je Position (100,00 € zu 19 % und 50,00 € zu ``second_rate``, 7 % oder 5,5 %) samt Forderungen, mit Skonto-Bedingungen."""
    tax, gross = (22.5, 172.5) if second_rate == 0.07 else (21.75, 171.75)
    invoice = _invoice(db, number, gross, rate=0.19, sum_net=150.0, tax_amount=tax, mixed_tax_rates=1)
    for position, (net, rate) in enumerate(((100.0, 0.19), (50.0, second_rate)), 1):
        db.insert_invoice_item({'invoice_id': invoice, 'position': position, 'description': 'Position', 'quantity': 1, 'unit': 'Stk.',
                                'price_per_unit': net, 'total_net': net, 'tax_category': 'S', 'tax_rate': rate})
    db.ensure_receivable_booking(invoice)
    return invoice


def _bank(db, account_id, amount, date='2025-04-10'):
    return db.insert_booking(date, Decimal(str(amount)), account_id=account_id, booking_type='bank', recipient_client='Käufer AG')


def _kids(db, bank_id):
    """Die Sätze unter einer Bankbewegung, nach ID: Form, Kontonummern, Betrag und Steuer in Euro, Satz, Text – wie gespeichert, mit der Marke."""
    numbers = db.get_coa_id_to_number_map()
    rows = _raw(db, 'SELECT EntryForm, COA_ID, CounterCOA_ID, Amount, TaxAmount, TaxRate, Text, Status FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank_id)
    return [(form, numbers.get(coa), numbers.get(counter), Decimal(amount) * MONEY, None if tax is None else Decimal(tax) * MONEY, rate, text, status)
            for form, coa, counter, amount, tax, rate, text, status in rows]


def _kid_ids(db, bank_id):
    return [row[0] for row in _raw(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank_id)]


def _payment(db, invoice_id):
    """Die Zuordnungen einer Rechnung: (Betrag, Skonto, Zahlungsdatum) in Minor Units, nach ID."""
    return _raw(db, 'SELECT Amount, SkontoAmount, PaymentDate FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID', invoice_id)


def _state(db, invoice_id):
    (status, due), = _raw(db, 'SELECT Status, AmountDue FROM Invoices WHERE ID = ?', invoice_id)
    return status, Decimal(due) * MONEY


def _euer(db):
    return {number: round(total, 2) for number, _name, total in db.get_euer_data(*RANGE)}


def _tables(db):
    """Bookings, Zuordnungen und Rechnungen, Spalte für Spalte – ohne die Zeitstempel, die jedes Schreiben setzt (``UpdatedAt``, ``CreatedAt``)."""
    result = {}
    with closing(sqlite3.connect(db.db_name)) as con:
        for table, skip in (('Bookings', ()), ('InvoicePayments', ('CreatedAt',)), ('Invoices', ('UpdatedAt',)), ('InvoiceItems', ())):
            cursor = con.execute(f'SELECT * FROM {table} ORDER BY ID')
            names = [column[0] for column in cursor.description]
            result[table] = [tuple(value for name, value in zip(names, row) if name not in skip) for row in cursor.fetchall()]
    return result


def _assign(db, invoice_id, bank_id, amount=None, skonto=True):
    result = db.assign_invoice_payment(invoice_id, bank_id, amount, None, skonto)
    assert result.ok, result.message
    return result


@pytest.fixture
def db(tmp_db):
    return tmp_db


@pytest.fixture
def acct(db):
    return _account(db)


# ═══ Die Form, Spalte für Spalte ═════════════════════════════════════════════

def test_19_percent_with_a_receivable_books_three_rows(db, acct):
    invoice = _sent(db, 'SK-1', 119.0)
    bank = _bank(db, acct, '116.62')

    result = _assign(db, invoice, bank)

    assert _kids(db, bank) == [
        ('liquid_first', 1800, 10000, Decimal('119'), None, None, 'Zahlung zu Re. SK-1', PAYMENT_MARK),                       # Bank an Debitoren: Geld plus Skonto
        ('transfer', 4405, 4400, Decimal('119'), Decimal('19'), 0.19, 'Umb. zu Re. SK-1', PAYMENT_MARK),                      # Wartekonto an Erlöskonto: mit der Rechnungssteuer
        ('purpose_first', 4736, 1800, Decimal('-2.38'), Decimal('-0.38'), 0.19, 'Skonto zu Re. SK-1', PAYMENT_MARK)]          # Skonto-Konto an Bank
    assert _payment(db, invoice) == [(1166200, 23800, '2025-04-10')]                                                         # Amount bleibt das Geld
    assert _state(db, invoice) == ('paid', 0)
    assert result.revenue_coa_id == _coa(db, 4400) and result.payment_id == _raw(db, 'SELECT ID FROM InvoicePayments')[0][0]
    for row in _raw(db, 'SELECT DocumentNumber, AutoMirror, DateBooking, DateTax, RecipientClient, Currency, BookingType, SourceGroup, Account_ID, Category_ID '
                        'FROM Bookings WHERE ParentBooking_ID = ?', bank):
        assert row == ('SK-1', 0, '2025-04-10', None, 'Käufer AG', 'EUR', 'entry', None, None, None)
    assert db.get_invoice_skonto(invoice) == {result.payment_id: Decimal('2.38')}


def test_19_percent_without_a_receivable_books_two_rows(db, acct):
    invoice = _invoice(db, 'SK-2', 119.0)                                                                                   # keine Forderung gebucht
    bank = _bank(db, acct, '116.62')

    _assign(db, invoice, bank)

    assert _kids(db, bank) == [
        ('purpose_first', 4400, 1800, Decimal('119'), Decimal('19'), 0.19, 'Zahlung zu Re. SK-2', PAYMENT_MARK),               # Erlöskonto an Bank
        ('purpose_first', 4736, 1800, Decimal('-2.38'), Decimal('-0.38'), 0.19, 'Skonto zu Re. SK-2', PAYMENT_MARK)]
    assert _payment(db, invoice) == [(1166200, 23800, '2025-04-10')] and _state(db, invoice) == ('paid', 0)


def test_7_percent_books_the_discount_on_4731(db, acct):
    invoice = _sent(db, 'SK-3', 107.0, rate=0.07)
    bank = _bank(db, acct, '104.86')

    _assign(db, invoice, bank)

    assert _kids(db, bank) == [
        ('liquid_first', 1800, 10000, Decimal('107'), None, None, 'Zahlung zu Re. SK-3', PAYMENT_MARK),
        ('transfer', 4345, 4300, Decimal('107'), Decimal('7'), 0.07, 'Umb. zu Re. SK-3', PAYMENT_MARK),
        ('purpose_first', 4731, 1800, Decimal('-2.14'), Decimal('-0.14'), 0.07, 'Skonto zu Re. SK-3', PAYMENT_MARK)]
    assert _payment(db, invoice) == [(1048600, 21400, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert _euer(db) == {4300: 100.0, 4731: -2.0, 3806: 6.86}                                                                 # die Umsatzsteuer steht auf einer Zeile


def test_0_percent_with_a_tax_statement_books_the_discount_on_4730_without_tax(db, acct):
    invoice = _sent(db, 'SK-4', 100.0, rate=0.0)
    bank = _bank(db, acct, '98.00')

    _assign(db, invoice, bank)

    assert _kids(db, bank) == [
        ('liquid_first', 1800, 10000, Decimal('100'), None, None, 'Zahlung zu Re. SK-4', PAYMENT_MARK),
        ('transfer', 4340, 4185, Decimal('100'), None, None, 'Umb. zu Re. SK-4', PAYMENT_MARK),                               # 0 % bleibt auf 4185
        ('purpose_first', 4730, 1800, Decimal('-2'), None, None, 'Skonto zu Re. SK-4', PAYMENT_MARK)]
    assert _payment(db, invoice) == [(980000, 20000, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert _euer(db) == {4185: 100.0, 4730: -2.0}                                                                             # Erlös und Skonto stehen getrennt, ohne Steuer


@pytest.mark.parametrize('day, revenue', [('2025-12-31', 4185), ('2026-01-01', 4184)], ids=['bis-2025-auf-4185', 'ab-2026-auf-4184'])
@pytest.mark.parametrize('with_receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_section_19_books_the_discount_on_4730_and_the_revenue_by_the_day_of_the_movement(db, acct, day, revenue, with_receivable):
    make = _sent if with_receivable else _invoice
    invoice = make(db, 'SK-5', 100.0, rate=-1)                                                                              # ohne Steuerausweis
    bank = _bank(db, acct, '98.00', date=day)

    _assign(db, invoice, bank)

    kids = _kids(db, bank)
    assert kids[-1] == ('purpose_first', 4730, 1800, Decimal('-2'), None, None, 'Skonto zu Re. SK-5', PAYMENT_MARK)
    if with_receivable:
        assert kids[:2] == [('liquid_first', 1800, 10000, Decimal('100'), None, None, 'Zahlung zu Re. SK-5', PAYMENT_MARK),
                            ('transfer', 4340, revenue, Decimal('100'), None, None, 'Umb. zu Re. SK-5', PAYMENT_MARK)]
    else:
        assert kids[0] == ('purpose_first', revenue, 1800, Decimal('100'), None, None, 'Zahlung zu Re. SK-5', PAYMENT_MARK)
    assert _payment(db, invoice) == [(980000, 20000, day)] and _state(db, invoice) == ('paid', 0)


def test_a_partial_payment_then_the_closing_one_with_a_discount(db, acct):
    invoice = _sent(db, 'SK-6', 119.0)
    first = _bank(db, acct, '50.00', date='2025-04-02')
    second = _bank(db, acct, '66.62', date='2025-05-05')

    _assign(db, invoice, first, skonto=False)
    assert _kids(db, first)[1][4] == Decimal('7.98')                                                                         # anteilige Steuer der Teilzahlung
    assert _state(db, invoice) == ('partial_payment', Decimal('69'))
    _assign(db, invoice, second)

    assert _kids(db, second) == [
        ('liquid_first', 1800, 10000, Decimal('69'), None, None, 'Zahlung zu Re. SK-6', PAYMENT_MARK),
        ('transfer', 4405, 4400, Decimal('69'), Decimal('11.02'), 0.19, 'Umb. zu Re. SK-6', PAYMENT_MARK),                    # der Rest der Rechnungssteuer
        ('purpose_first', 4736, 1800, Decimal('-2.38'), Decimal('-0.38'), 0.19, 'Skonto zu Re. SK-6', PAYMENT_MARK)]
    assert _payment(db, invoice) == [(500000, None, '2025-04-02'), (666200, 23800, '2025-05-05')]
    assert _state(db, invoice) == ('paid', 0)
    taxes = _raw(db, "SELECT SUM(TaxAmount) FROM Bookings WHERE Status = ? AND DocumentNumber = 'SK-6'", PAYMENT_MARK)[0][0]
    assert Decimal(taxes) * MONEY == Decimal('18.62')                                                                         # 7,98 + 11,02 − 0,38: die Steuer des gezahlten Geldes
    assert _euer(db) == {4400: 100.0, 4736: -2.0, 3806: 18.62}
    assert db.link_bank_to_entries().get('resolved') == 1                                                                    # Stufe 7: 119,00 „an 10000“ decken die Forderung


def test_a_payment_of_117_euro_books_two_euro_of_discount(db, acct):
    """Gebucht wird der tatsächliche Rest, nie mehr als der gedruckte Abzug: 119,00 − 117,00 = 2,00 (die Grenze wäre 2,38), die Steuer darin 0,32."""
    invoice = _sent(db, 'SK-7', 119.0)
    bank = _bank(db, acct, '117.00')

    _assign(db, invoice, bank)

    assert _kids(db, bank) == [
        ('liquid_first', 1800, 10000, Decimal('119'), None, None, 'Zahlung zu Re. SK-7', PAYMENT_MARK),
        ('transfer', 4405, 4400, Decimal('119'), Decimal('19'), 0.19, 'Umb. zu Re. SK-7', PAYMENT_MARK),
        ('purpose_first', 4736, 1800, Decimal('-2'), Decimal('-0.32'), 0.19, 'Skonto zu Re. SK-7', PAYMENT_MARK)]
    assert _payment(db, invoice) == [(1170000, 20000, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert _euer(db) == {4400: 100.0, 4736: -1.68, 3806: 18.68}


def test_a_movement_larger_than_the_payment_keeps_the_rest_free(db, acct):
    """Die Zahlung ist ``min(Rest der Rechnung, freier Rest, gewünschter Betrag)``; der Rest nach ihr ist das Skonto, der Rest der Bewegung bleibt frei."""
    invoice = _sent(db, 'SK-8', 119.0)
    bank = _bank(db, acct, '200.00')

    _assign(db, invoice, bank, amount='116.62')

    assert _payment(db, invoice) == [(1166200, 23800, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert db.get_booking_unallocated_amount(bank) == Decimal('83.38')                                                         # 200,00 − 116,62


def test_the_largest_rest_is_the_discount_the_invoice_provides_and_one_cent_more_is_too_much(db, acct):
    invoice = _sent(db, 'SK-9', 119.0)
    too_little = _bank(db, acct, '116.61')
    exact = _bank(db, acct, '116.62', date='2025-04-11')

    refused = db.assign_invoice_payment(invoice, too_little, None, None, True)
    assert (refused.ok, refused.message) == (False, SKONTO_TOO_LARGE.format(rest='2,39 €', skonto='2,38 €'))
    assert _payment(db, invoice) == []
    _assign(db, invoice, exact)
    assert _payment(db, invoice) == [(1166200, 23800, '2025-04-11')]


def test_the_discount_of_a_three_decimal_percent_is_rounded_once_to_the_cent(db, acct):
    """2,5 % von 119,00 sind 2,975 €: Das Skonto der Rechnung ist 2,98 € (``export.einvoice_model.skonto_amount``, wie das PDF), ein Rest bis dahin geht durch."""
    invoice = _sent(db, 'SK-10', 119.0, skonto=(10, 2.5))
    bank = _bank(db, acct, '116.02')                                                                                         # Rest 2,98 €

    _assign(db, invoice, bank)

    assert _payment(db, invoice) == [(1160200, 29800, '2025-04-10')]
    assert _kids(db, bank)[-1][3:5] == (Decimal('-2.98'), Decimal('-0.48'))                                                  # 2,98 × 19/119 = 0,4758 → 0,48


def test_the_deadline_is_never_checked(db, acct):
    """Die Frist (14 Tage ab dem 03.03.2025) ist am 10.04. lange vorbei – das Skonto bucht trotzdem: Der Kunde hat es abgezogen, die App bucht, was geflossen ist."""
    invoice = _sent(db, 'SK-11', 119.0, day='2025-03-03', skonto=(14, 2.0))
    bank = _bank(db, acct, '116.62', date='2025-04-10')

    _assign(db, invoice, bank)

    assert _payment(db, invoice) == [(1166200, 23800, '2025-04-10')]


# ═══ Die Reihenfolge der Sätze und das Erlöskonto ════════════════════════════

@pytest.mark.parametrize('with_receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_revenue_rows_come_before_the_discount_row_and_the_revenue_account_is_found(db, acct, with_receivable):
    """``_payment_revenue_coa`` (die Erstattung, das Lernen) nimmt den ERSTEN Satz der Form: Der Skonto-Satz steht zuletzt und ist keines."""
    make = _sent if with_receivable else _invoice
    invoice = make(db, 'SK-12', 119.0)
    bank = _bank(db, acct, '116.62')

    _assign(db, invoice, bank)

    ids = _kid_ids(db, bank)
    assert ids == sorted(ids) and len(ids) == (3 if with_receivable else 2)
    rows = _raw(db, 'SELECT ID, COA_ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank)
    assert rows[-1][1] == _coa(db, 4736) and rows[-1][0] == max(ids)
    with closing(db._get_connection()) as conn:
        assert db._payment_revenue_coa(conn, ids) == _coa(db, 4400)


def test_the_learned_revenue_account_stays_4400(db, acct):
    invoice = _sent(db, 'SK-13', 119.0)
    _assign(db, invoice, _bank(db, acct, '116.62'))

    assert db.resolve_revenue_coa(0.19, create=False) == _coa(db, 4400)


def test_a_positive_discount_row_teaches_nothing_and_an_older_row_still_does(db, acct):
    """Ein positiver Satz auf einem Skonto-Konto (die Rücknahme beim Stornieren, ein im Editor umgedrehter Satz) machte 4736 zum Erlöskonto für 19 %. Er fällt heraus,
    ein älterer lehrt weiter."""
    invoice = _sent(db, 'SK-14', 119.0)
    bank = _bank(db, acct, '116.62')
    _assign(db, invoice, bank)
    assert db._learned_revenue_coa(db._revenue_rate_key(0.19)) == _coa(db, 4400)
    marked = ("INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, DocumentNumber, BookingType, ParentBooking_ID, "
              "EntryForm, Status, Currency) VALUES ('2025-06-01', ?, ?, 23800, 0.19, 3800, 'SK-14', 'entry', ?, 'purpose_first', ?, 'EUR')")
    _exec(db, marked, _coa(db, 4736), _coa(db, 1800), bank, PAYMENT_MARK)                                                      # die jüngste Zeile: +2,38 auf 4736

    assert db._learned_revenue_coa(db._revenue_rate_key(0.19)) == _coa(db, 4400)
    assert db.resolve_revenue_coa(0.19, create=False) == _coa(db, 4400)


@pytest.mark.parametrize('number', SKONTO_NUMBERS)
def test_no_discount_account_is_ever_learned(db, acct, number):
    invoice = _invoice(db, 'SK-15', 119.0)
    bank = _bank(db, acct, '119.00')
    _exec(db, "INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate, DocumentNumber, BookingType, ParentBooking_ID, EntryForm, Status, "
              "Currency) VALUES ('2025-06-01', ?, ?, 10000, 0.19, 'SK-15', 'entry', ?, 'purpose_first', ?, 'EUR')", _coa(db, number), _coa(db, 1800), bank, PAYMENT_MARK)

    assert db._learned_revenue_coa(db._revenue_rate_key(0.19)) is None                                                         # nichts gelernt: es greift das Standardkonto
    assert db.resolve_revenue_coa(0.19, create=False) == _coa(db, 4400) and invoice


def test_the_suggestion_of_a_revenue_account_never_names_a_discount_account(db):
    for _ in range(3):                                                                                                        # 4736 ist das häufigste Konto mit 19 % – ohne Ausschluss die Vorauswahl
        db.insert_booking('2025-05-01', Decimal('-2.38'), coa_id=_coa(db, 4736), counter_coa_id=_coa(db, 1800), booking_type='entry', tax_rate=0.19,
                          tax_amount=Decimal('-0.38'), document_number='X-1')
    assert db.suggest_revenue_coa(0.19) is None                                                                               # kein Treffer unter den Erlöskonten

    db.insert_booking('2025-05-02', Decimal('119.00'), coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1800), booking_type='entry', tax_rate=0.19,
                      tax_amount=Decimal('19.00'), document_number='X-2')
    assert db.suggest_revenue_coa(0.19) == _coa(db, 4400)


# ═══ Ablehnungen: nichts geschrieben, kein Konto angelegt ═════════════════════

def _without_discount_accounts(db, *numbers):
    _exec(db, f"DELETE FROM ChartOfAccounts WHERE AccountNumber IN ({', '.join('?' * len(numbers))})", *numbers)
    assert all(_coa(db, number) is None for number in numbers)


def _refusal_world(db, acct, **kw):
    """Eine Rechnung (mit Forderung) und eine Bewegung über 116,62 €; Konten, die eine Zuordnung anlegen könnte, fehlen."""
    paid = kw.pop('paid', '116.62')
    invoice = _sent(db, 'SK-R', kw.pop('gross', 119.0), **kw)
    bank = _bank(db, acct, paid)
    _without_discount_accounts(db, *SKONTO_NUMBERS, 4184, 4185)
    return invoice, bank


def _assert_refused(db, invoice, bank, message, amount=None):
    before = installation.dump(db.db_name)
    result = db.assign_invoice_payment(invoice, bank, amount, None, True)
    assert (result.ok, result.message, result.payment_id) == (False, message, None)
    assert installation.dump(db.db_name) == before                                                                            # keine Zeile, kein Konto, kein Zähler
    assert all(_coa(db, number) is None for number in (*SKONTO_NUMBERS, 4184, 4185))


def test_an_invoice_without_discount_terms_refuses(db, acct):
    invoice, bank = _refusal_world(db, acct, skonto=None)
    _assert_refused(db, invoice, bank, SKONTO_NO_TERMS)


@pytest.mark.parametrize('days, percent', [(14, None), (None, 2.0), (0, 0), (400, 2.0), (14, 100.0), (14, 2.005)],
                         ids=['nur-Tage', 'nur-Prozent', 'null', 'Frist-zu-lang', 'hundert-Prozent', 'drei-Stellen'])
def test_unusable_terms_count_as_none(db, acct, days, percent):
    invoice, bank = _refusal_world(db, acct)
    _exec(db, 'UPDATE Invoices SET SkontoDays = ?, SkontoPercent = ? WHERE ID = ?', days, percent, invoice)
    _assert_refused(db, invoice, bank, SKONTO_NO_TERMS)


@pytest.mark.parametrize('day', ['2025-04-10', '2026-02-02'], ids=['bis-2025', 'ab-2026'])
def test_a_refused_discount_on_a_section_19_invoice_creates_no_account_at_all(db, acct, day):
    """Ohne Steuerausweis legt die Zuordnung 4185/4184 bei Bedarf an – die Regeln des Skontos kommen davor: Eine Ablehnung hinterlässt weder das Erlöskonto noch ein
    Skonto-Konto."""
    invoice = _sent(db, 'SK-R19', 100.0, rate=-1, skonto=None)
    bank = _bank(db, acct, '98.00', date=day)
    _without_discount_accounts(db, *SKONTO_NUMBERS, 4184, 4185)
    _assert_refused(db, invoice, bank, SKONTO_NO_TERMS)


def test_an_invoice_with_a_rate_per_position_and_a_group_without_a_discount_account_refuses(db, acct):
    """19 % und 5,5 %: Eine Gruppe ohne Skonto-Konto – der feste Satz nennt ihren Satz. (19 und 7 % gehen: ``tests/test_skonto_mixed_booking.py``.)"""
    invoice = _mixed(db, second_rate=0.055)
    bank = _bank(db, acct, '169.00')
    _without_discount_accounts(db, *SKONTO_NUMBERS, 4184, 4185)
    _assert_refused(db, invoice, bank, SKONTO_GROUP_RATE.format(rate='5,5'))


def test_an_invoice_with_half_a_snapshot_from_the_master_data_refuses(db, acct):
    """Ein Satz aus den Stammdaten geht jetzt (``tests/test_skonto_mixed_booking.py``); hier bleibt abgelehnt, was ein Widerspruch ist: ein Erlöskonto aus den Stammdaten ohne den Satz
    dazu (die halbe Momentaufnahme) – wie bei jeder Zahlung ``TAX_ENTRY_CONTRADICTION``."""
    invoice, bank = _refusal_world(db, acct)
    db.insert_invoice_item({'invoice_id': invoice, 'position': 1, 'description': 'Anstrich', 'quantity': 1, 'unit': 'Stk.', 'price_per_unit': 100.0,
                            'total_net': 100.0, 'tax_category': 'S', 'tax_rate': 0.19, 'tax_rate_bp': None, 'revenue_coa_id': 404400})
    _assert_refused(db, invoice, bank, TAX_ENTRY_CONTRADICTION)


@pytest.mark.parametrize('rate', [0.16, 0.05, 0.055], ids=['16-Prozent', '5-Prozent', '5,5-Prozent'])
def test_a_rate_without_a_discount_account_refuses(db, acct, rate):
    invoice, bank = _refusal_world(db, acct, rate=rate)
    _assert_refused(db, invoice, bank, SKONTO_RATE)


def test_a_movement_that_closes_the_invoice_has_no_rest_to_write_off(db, acct):
    invoice, bank = _refusal_world(db, acct, paid='119.00')
    _exec(db, 'UPDATE Bookings SET Amount = 1190000 WHERE ID = ?', bank)
    _assert_refused(db, invoice, bank, SKONTO_NO_REST)


def test_a_movement_larger_than_the_invoice_has_no_rest_either(db, acct):
    invoice, bank = _refusal_world(db, acct)
    _exec(db, 'UPDATE Bookings SET Amount = 1300000 WHERE ID = ?', bank)
    _assert_refused(db, invoice, bank, SKONTO_NO_REST)


def test_a_rest_above_the_discount_of_the_invoice_names_both_amounts(db, acct):
    invoice, bank = _refusal_world(db, acct)
    _exec(db, 'UPDATE Bookings SET Amount = 1000000 WHERE ID = ?', bank)                                                       # 100,00 €: Rest 19,00 €, Skonto 2,38 €
    _assert_refused(db, invoice, bank, SKONTO_TOO_LARGE.format(rest='19,00 €', skonto='2,38 €'))
    assert SKONTO_TOO_LARGE.format(rest='19,00 €', skonto='2,38 €') == 'Der Rest nach dieser Zahlung (19,00 €) ist größer als das Skonto der Rechnung (2,38 €).'


def test_a_requested_amount_below_the_movement_counts_not_the_movement(db, acct):
    invoice, bank = _refusal_world(db, acct)
    _exec(db, 'UPDATE Bookings SET Amount = 1190000 WHERE ID = ?', bank)
    _assert_refused(db, invoice, bank, SKONTO_TOO_LARGE.format(rest='19,00 €', skonto='2,38 €'), amount='100.00')


def test_an_invoice_that_cannot_be_paid_names_that_before_the_discount(db, acct):
    invoice, bank = _refusal_world(db, acct, status='draft')
    _assert_refused(db, invoice, bank, 'Die Rechnung ist noch nicht versendet – Zahlungen lassen sich erst nach dem Versenden zuordnen.')


def test_a_refused_discount_leaves_a_plain_payment_possible(db, acct):
    """Nach der Ablehnung gilt, was immer galt: dieselbe Zuordnung ohne Skonto gelingt."""
    invoice, bank = _refusal_world(db, acct, skonto=None)
    _assert_refused(db, invoice, bank, SKONTO_NO_TERMS)

    result = db.assign_invoice_payment(invoice, bank)

    assert result.ok and _payment(db, invoice) == [(1166200, None, '2025-04-10')] and _state(db, invoice) == ('partial_payment', Decimal('2.38'))


def test_the_rest_of_the_invoice_counts_the_discount_as_settled_for_the_next_assignment(db, acct):
    """Eine Zuordnung mit Skonto hat die Rechnung geschlossen. Auch wenn jemand den Status von Hand zurücksetzt, bleibt nichts zuzuordnen: Der Rest der Rechnung ist
    Brutto minus Geld minus Skonto – in der Vorprüfung mit Skonto wie in der Transaktion."""
    invoice = _sent(db, 'SK-40', 119.0)
    _assign(db, invoice, _bank(db, acct, '116.62'))
    _exec(db, "UPDATE Invoices SET Status = 'sent', AmountDue = 23800 WHERE ID = ?", invoice)                                    # von Hand: als stünden 2,38 € offen
    other = _bank(db, acct, '2.38', date='2025-04-11')

    for skonto in (False, True):
        result = db.assign_invoice_payment(invoice, other, None, None, skonto)
        assert (result.ok, result.message) == (False, NOTHING_TO_ASSIGN_MESSAGE), skonto
    assert _kid_ids(db, other) == []


# ═══ Das Skonto-Konto entsteht bei der ersten Zuordnung, wenn es fehlt ═══════

@pytest.mark.parametrize('rate, key', [(0.19, 19), (0.07, 7), (0.0, 0), (-1, 0)], ids=['19', '7', '0', 'par-19'])
def test_a_missing_discount_account_is_created_by_the_first_assignment(db, acct, rate, key):
    gross = 119.0 if rate == 0.19 else 107.0 if rate == 0.07 else 100.0
    paid = {19: '116.62', 7: '104.86', 0: '98.00'}[key]
    invoice = _sent(db, 'SK-16', gross, rate=rate)
    bank = _bank(db, acct, paid)
    number, name = SKONTO_ACCOUNTS[key]
    _without_discount_accounts(db, number)

    _assign(db, invoice, bank)

    created = _raw(db, 'SELECT Framework, Name, Description, IsStandard FROM ChartOfAccounts WHERE AccountNumber = ?', number)
    assert created == [(4, name, 'Betriebliche Erträge', 1)]                                                                  # wie die Seed-Datei es führt
    assert _kids(db, bank)[-1][1] == number


def test_an_older_stock_gets_the_discount_accounts_through_the_missing_standard_accounts(db):
    """Die Seed-Datei führt 4730, 4731 und 4736 als Standardkonten; ein Bestand ohne sie bekommt sie über „fehlende Standardkonten“ (nur auf Knopfdruck, wie jedes
    Standardkonto) – 4730 und 4731 neu seit 2.9.0."""
    _without_discount_accounts(db, *SKONTO_NUMBERS)

    assert [number for number, _name in db.missing_standard_accounts() if number in SKONTO_NUMBERS] == [4730, 4731, 4736]
    assert (4730, 'Gewährte Skonti') in db.missing_standard_accounts() and (4731, 'Gewährte Skonti 7 % USt') in db.missing_standard_accounts()
    db.add_missing_standard_accounts()

    assert [_raw(db, 'SELECT Name, IsStandard FROM ChartOfAccounts WHERE AccountNumber = ?', number) for number in SKONTO_NUMBERS] == [
        [('Gewährte Skonti', 1)], [('Gewährte Skonti 7 % USt', 1)], [('Gewährte Skonti 19 % USt', 1)]]


def test_a_present_discount_account_is_used_and_nothing_is_created(db, acct):
    invoice = _sent(db, 'SK-17', 119.0)
    bank = _bank(db, acct, '116.62')
    accounts = _raw(db, 'SELECT ID, AccountNumber FROM ChartOfAccounts ORDER BY ID')

    _assign(db, invoice, bank)

    assert _raw(db, 'SELECT ID, AccountNumber FROM ChartOfAccounts ORDER BY ID') == accounts


# ═══ Das Netz in der Transaktion ═════════════════════════════════════════════

def _change_after_the_precheck(monkeypatch, db, change):
    """``change()`` läuft, sobald die Vorprüfung durch ist – ein zweites Fenster ändert etwas, bevor die Transaktion beginnt."""
    original = type(db)._payment_prerequisites

    def moved(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        change()
        return result

    monkeypatch.setattr(type(db), '_payment_prerequisites', moved)


@pytest.mark.parametrize('name', ['terms-removed', 'discount-shrinks', 'discount-account-added', 'another-payment', 'rate-changes'])
def test_a_change_between_the_precheck_and_the_transaction_refuses_with_the_changed_sentence(db, acct, monkeypatch, name):
    invoice = _sent(db, 'SK-18', 119.0)
    bank = _bank(db, acct, '116.62')
    other = _bank(db, acct, '10.00', date='2025-04-11')
    changes = {
        'terms-removed': lambda: _exec(db, 'UPDATE Invoices SET SkontoDays = NULL, SkontoPercent = NULL WHERE ID = ?', invoice),
        'discount-shrinks': lambda: _exec(db, 'UPDATE Invoices SET SkontoPercent = 1.0 WHERE ID = ?', invoice),                  # Skonto jetzt 1,19 €, der Rest 2,38 €
        # ein Erlöskonto aus den Stammdaten, das selbst ein Skonto-Konto ist (ein Satz aus den Stammdaten allein lehnt nicht mehr ab)
        'discount-account-added': lambda: db.insert_invoice_item({'invoice_id': invoice, 'position': 1, 'description': 'x', 'quantity': 1, 'unit': 'Stk.',
                                                                  'price_per_unit': 100.0, 'total_net': 100.0, 'tax_rate': 0.19, 'tax_rate_bp': 1900,
                                                                  'revenue_coa_id': _coa(db, 4736)}),
        'another-payment': lambda: _exec(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 100000, ?)',
                                         invoice, other, '2025-04-11'),                                                          # der Rest der Rechnung sinkt um 10,00 €
        'rate-changes': lambda: _exec(db, 'UPDATE Invoices SET TaxRate = 0.07 WHERE ID = ?', invoice),
    }
    seen = {}

    def change():
        changes[name]()
        seen['tables'] = _tables(db)                                                                                          # der Stand nach der Änderung ist der Vergleich

    _change_after_the_precheck(monkeypatch, db, change)

    result = db.assign_invoice_payment(invoice, bank, None, None, True)

    assert (result.ok, result.message) == (False, PAYMENT_CHANGED_MESSAGE)
    assert _tables(db) == seen['tables'] and _kid_ids(db, bank) == []                                                          # nichts geschrieben
    assert all(row[1] is None for row in _payment(db, invoice))                                                                # kein Skonto


def test_a_context_without_the_key_books_as_always(db, acct):
    """Ein Kontext ohne ``skonto`` (Neuaufbau, Umzug, von Hand gebaut) bucht, wie die App immer gebucht hat – auch wenn die Rechnung Skonto-Bedingungen trägt."""
    invoice = _sent(db, 'SK-19', 119.0)
    bank = _bank(db, acct, '116.62')
    error, context = db._payment_prerequisites(invoice, acct, None, on='2025-04-10')
    assert error is None and 'skonto' not in context
    with db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        assert db._assign_in_tx(tx, invoice, bank, None, context).ok

    assert _payment(db, invoice) == [(1166200, None, '2025-04-10')]
    assert [row[3] for row in _kids(db, bank)] == [Decimal('116.62'), Decimal('116.62')]
    assert _state(db, invoice) == ('partial_payment', Decimal('2.38'))


# ═══ Entfernen stellt jede Spalte wieder her ═════════════════════════════════

def test_removing_the_assignment_restores_every_column(db, acct):
    invoice = _sent(db, 'SK-20', 119.0)
    bank = _bank(db, acct, '116.62')
    before = _tables(db)
    result = _assign(db, invoice, bank)
    assert db.link_bank_to_entries().get('resolved') == 1                                                                    # Stufe 7 hat die Forderung aufgelöst
    assert _tables(db) != before

    db.unassign_invoice_payment(result.payment_id)

    after = _tables(db)
    assert after == before                                                                                                    # Bookings, InvoicePayments, Invoices, Positionen: Spalte für Spalte
    assert _state(db, invoice) == ('sent', Decimal('119')) and _kids(db, bank) == []


def test_removing_the_assignment_without_a_receivable_restores_every_column(db, acct):
    invoice = _invoice(db, 'SK-21', 119.0)
    bank = _bank(db, acct, '116.62')
    before = _tables(db)
    result = _assign(db, invoice, bank)

    db.unassign_invoice_payment(result.payment_id)

    assert _tables(db) == before and _state(db, invoice) == ('sent', Decimal('119'))


def test_deleting_the_bank_movement_takes_the_whole_assignment_with_it(db, acct):
    invoice = _sent(db, 'SK-22', 119.0)
    before = _tables(db)
    bank = _bank(db, acct, '116.62')
    _assign(db, invoice, bank)

    db.delete_transaction(bank)

    assert _tables(db) == before and _state(db, invoice) == ('sent', Decimal('119'))


@pytest.mark.parametrize('which', [0, 1, 2], ids=['Bank-an-Debitoren', 'Wartekonto-an-Erlös', 'Skonto-Satz'])
def test_removing_any_row_of_the_assignment_removes_the_whole_assignment(db, acct, which):
    """Das ✕ im Bank-Editor an jeder markierten Zeile geht über ``remove_assignment_for_child``: die ganze Zuordnung, nie ein Satz allein – auch der Skonto-Satz."""
    invoice = _sent(db, 'SK-23', 119.0)
    bank = _bank(db, acct, '116.62')
    before = _tables(db)
    _assign(db, invoice, bank)

    db.delete_transaction(_kid_ids(db, bank)[which])

    assert _tables(db) == before
    assert _state(db, invoice) == ('sent', Decimal('119')) and _payment(db, invoice) == []


# ═══ Der Bank-Editor und das Replay-Log ══════════════════════════════════════

def test_opening_and_saving_the_bank_editor_without_a_change_changes_nothing(db, acct):
    invoice = _sent(db, 'SK-24', 119.0)
    bank = _bank(db, acct, '116.62')
    _assign(db, invoice, bank)
    before_rows, before_tables = snapshot(db), _tables(db)

    post, _options = open_form(db, bank)
    submit(db, post)

    assert differences(before_rows, snapshot(db), db.get_coa_id_to_number_map()) == []
    assert _tables(db) == before_tables
    assert _payment(db, invoice) == [(1166200, 23800, '2025-04-10')]


def _replay_lines(prefix):
    path = os.path.join(userctx.user_data_dir(), 'sql_operations.sql')
    with open(path, encoding='utf-8') as handle:
        return [line for line in handle.read().splitlines() if prefix in line]


def test_the_replay_log_carries_the_skonto_column_only_when_there_is_a_discount(db, acct):
    plain = _sent(db, 'SK-25', 119.0)
    plain_bank = _bank(db, acct, '119.00')
    with_discount = _sent(db, 'SK-26', 119.0)
    discount_bank = _bank(db, acct, '116.62', date='2025-04-11')

    _assign(db, plain, plain_bank, skonto=False)
    _assign(db, with_discount, discount_bank)

    inserts = _replay_lines('INSERT INTO InvoicePayments')
    assert any(f'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES ({plain}, {plain_bank}, 1190000, ' in line for line in inserts)
    assert not any('SkontoAmount' in line and f'VALUES ({plain}, ' in line for line in inserts)                                  # ohne Skonto Zeichen für Zeichen wie immer
    assert any(f"INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate, SkontoAmount) VALUES ({with_discount}, {discount_bank}, 1166200, "
               f"'2025-04-11', 23800)" in line for line in inserts)
    rows = [line for line in _replay_lines('INSERT INTO Bookings') if "'invoice_payment'" in line and 'Skonto zu Re. SK-26' in line]
    assert len(rows) == 1 and '-23800' in rows[0] and '-3800' in rows[0]


# ═══ payment_precheck und link_booking_to_invoice_capped ═════════════════════

def test_the_precheck_gives_the_same_sentences_before_a_movement_exists(db, acct):
    _without_discount_accounts(db, *SKONTO_NUMBERS)
    no_terms = _sent(db, 'SK-27', 119.0, skonto=None)
    mixed = _mixed(db, 'SK-28', second_rate=0.055)
    sixteen = _sent(db, 'SK-29', 116.0, rate=0.16)
    fine = _sent(db, 'SK-30', 119.0)
    before = installation.dump(db.db_name)

    assert db.payment_precheck(no_terms, acct, None, on='2025-04-10', amount='116.62', skonto=True) == SKONTO_NO_TERMS
    assert db.payment_precheck(mixed, acct, None, on='2025-04-10', amount='116.62', skonto=True) == SKONTO_GROUP_RATE.format(rate='5,5')
    assert db.payment_precheck(sixteen, acct, None, on='2025-04-10', amount='113.68', skonto=True) == SKONTO_RATE
    assert db.payment_precheck(fine, acct, None, on='2025-04-10', amount='119.00', skonto=True) == SKONTO_NO_REST
    assert db.payment_precheck(fine, acct, None, on='2025-04-10', skonto=True) == SKONTO_NO_REST                                # ohne Betrag: die ganze Rechnung
    assert db.payment_precheck(fine, acct, None, on='2025-04-10', amount='100.00', skonto=True) == SKONTO_TOO_LARGE.format(rest='19,00 €', skonto='2,38 €')
    assert installation.dump(db.db_name) == before                                                                              # eine Ablehnung legt kein Konto an
    assert db.payment_precheck(fine, acct, None, on='2025-04-10', amount='116.62', skonto=True) is None


def test_the_precheck_without_the_flag_is_what_it_was(db, acct):
    invoice = _sent(db, 'SK-31', 119.0)
    assert db.payment_precheck(invoice, acct) is None and db.payment_precheck(invoice, acct, None, '2025-04-10', '100.00') is None
    assert db.payment_precheck(999999, acct, skonto=True) == 'Rechnung nicht gefunden'
    assert db.payment_precheck(999999, acct) == 'Rechnung nicht gefunden'


def test_the_precheck_names_a_blocked_invoice_before_the_discount(db, acct):
    draft = _invoice(db, 'SK-32', 119.0, status='draft')
    assert db.payment_precheck(draft, acct, None, on='2025-04-10', amount='116.62', skonto=True) == db.payment_precheck(draft, acct)
    assert 'noch nicht versendet' in db.payment_precheck(draft, acct, None, on='2025-04-10', amount='116.62', skonto=True)


def test_link_booking_to_invoice_capped_passes_the_discount_through(db, acct):
    invoice = _sent(db, 'SK-33', 119.0)
    bank = _bank(db, acct, '116.62')

    assert link_booking_to_invoice_capped(db, invoice, bank, None, None, True) == (True, None)

    assert _payment(db, invoice) == [(1166200, 23800, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert _raw(db, 'SELECT COA_ID, DocumentNumber FROM Bookings WHERE ID = ?', bank) == [(_coa(db, 4400), 'SK-33')]        # der Anzeige-Backfill wie immer


def test_link_booking_to_invoice_capped_returns_the_sentence_of_a_refusal(db, acct):
    invoice = _sent(db, 'SK-34', 119.0, skonto=None)
    bank = _bank(db, acct, '116.62')

    assert link_booking_to_invoice_capped(db, invoice, bank, None, None, True) == (False, SKONTO_NO_TERMS)
    assert link_booking_to_invoice_capped(db, invoice, bank) == (True, None)                                                    # ohne Skonto geht es wie immer
    assert _payment(db, invoice) == [(1166200, None, '2025-04-10')]
