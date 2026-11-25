# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Skonto bei der Zahlungszuordnung: die reinen Bausteine in ``db/invoice_payments.py`` (Konten, Steuer, Grenzen, feste Sätze, die Sätze einer Zahlung).

* ``skonto_tax``: die im Skonto enthaltene Steuer, EINMAL auf den Cent gerundet – mit dem Gegenbeispiel, an dem eine Doppelrundung (über die Minor Unit) einen Cent
  danebenläge, und einer Gegenrechnung mit Brüchen über viele Beträge.
* ``skonto_rate_key``: welcher Kopfsatz ein Skonto-Konto hat (19, 7, 0 und § 19), welcher keines.
* ``skonto_limit`` ist ``export.einvoice_model.skonto_amount``; ``skonto_terms`` und ``skonto_open_minor`` nennen die Ablehnungen in ihrer Reihenfolge (mit den Sätzen der Gruppen eines Belegs
  mit Steuersatz je Position: ``tests/test_skonto_mixed_pure.py``).
* Die festen Sätze, die Konten (gegen die Seed-Datei gehalten) und ``settled_sql``.
* ``payment_rows(skonto=…)``: dieselben Sätze wie immer, wenn es kein Skonto gibt; sonst drei (mit Forderung) bzw. zwei, der Skonto-Satz zuletzt.
* ``_recalc_invoice`` rechnet mit Geld plus Skonto: eine Rechnung mit von Hand gesetztem Skonto ist „bezahlt“, der Rest 0.

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import json
import os
import sqlite3
from contextlib import closing
from decimal import Decimal
from fractions import Fraction

import pytest

from db import invoice_payments as payments
from db.invoice_payments import (PAYMENT_MARK, SKONTO_ACCOUNT_NUMBERS, SKONTO_ACCOUNTS, SKONTO_ALREADY, SKONTO_NO_REST, SKONTO_NO_TERMS, SKONTO_NOT_CENTS,
                                 SKONTO_RATE, SKONTO_REFUND, SKONTO_REMOVAL_REFUSED, SKONTO_REVENUE_DISCOUNT, SKONTO_STORNO_EDITED, SKONTO_TOO_LARGE, SkontoPart, payment_rows,
                                 settled_sql, skonto_limit, skonto_open_minor, skonto_rate_key, skonto_tax, skonto_terms, valid_skonto_sql)
from export.einvoice_model import skonto_amount
from money import fmt_eur, from_minor, round_minor, tax_from_gross
from tests.document_helpers import add_contacts, add_document

SEED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'seed_data', 'chart_of_accounts_skr04.json')
CENT = 100                                  # eine Minor Unit ist 0,0001 €, ein Cent 100 Minor Units


# ── skonto_tax ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('skonto, rate_key, expected', [
    pytest.param(23800, 19, 3800, id='2,38 € bei 19 % → 0,38 €'),
    pytest.param(21400, 7, 1400, id='2,14 € bei 7 % → 0,14 €'),
    pytest.param(500, 19, 100, id='0,05 € → 0,01 € (0,0080 rundet auf)'),
    pytest.param(600, 19, 100, id='0,06 € → 0,01 €'),
    pytest.param(100, 19, 0, id='0,01 € → 0,00 € (0,0016 rundet ab)'),
    pytest.param(400, 19, 100, id='0,04 € → 0,01 € (0,0064)'),
    pytest.param(7200, 19, 1100, id='0,72 € → 0,11 €, nicht 0,12 € (Gegenbeispiel zur Doppelrundung)'),
    pytest.param(19100, 19, 3000, id='1,91 € → 0,30 €, nicht 0,31 €'),
    pytest.param(31000, 19, 4900, id='3,10 € → 0,49 €, nicht 0,50 €'),
    pytest.param(0, 19, 0, id='kein Skonto, keine Steuer'),
    pytest.param(23800, 0, 0, id='0 % und § 19: keine Steuer'),
    pytest.param(12345678, 19, 1971200, id='großer Betrag: 1.234,5678 € → 197,1158 € → 197,12 €'),
])
def test_skonto_tax_table(skonto, rate_key, expected):
    assert skonto_tax(skonto, rate_key) == expected


def test_skonto_tax_is_rounded_once_not_twice():
    """``money.tax_from_gross`` rundet auf die Minor Unit, danach die Rundung auf den Cent: zweimal. Bei 0,72 € Skonto zu 19 % (0,114958… €) wird so aus 0,11 € ein
    Cent zu viel – die Steuer eines Skontos rundet einmal, wie ``skonto_amount`` das Skonto selbst."""
    skonto = 7200
    twice = round_minor(tax_from_gross(skonto, 19), 2)
    assert tax_from_gross(skonto, 19) == 1150 and twice == 1200
    assert skonto_tax(skonto, 19) == 1100 != twice


@pytest.mark.parametrize('rate_key', [19, 7])
def test_skonto_tax_is_the_exact_value_rounded_half_up_to_the_cent(rate_key):
    """Gegenrechnung mit Brüchen (ohne Decimal) über jedes Skonto von einem Cent bis 50 €: ``Skonto × Satz / (100 + Satz)``, halb auf, in Cent."""
    for cents in range(1, 5001):
        exact = Fraction(cents * rate_key, 100 + rate_key)                 # in Cent
        expected = int((2 * exact.numerator + exact.denominator) // (2 * exact.denominator))    # halb auf
        assert skonto_tax(cents * CENT, rate_key) == expected * CENT, (cents, rate_key)


def test_skonto_tax_is_never_more_than_the_discount_and_always_whole_cents():
    for rate_key in (19, 7):
        for cents in range(0, 20001, 7):
            tax = skonto_tax(cents * CENT, rate_key)
            assert tax % CENT == 0 and 0 <= tax <= cents * CENT


# ── skonto_rate_key ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('tax_rate, expected', [
    pytest.param(0.19, 19, id='0,19'), pytest.param(19, 19, id='19'), pytest.param(19.0, 19, id='19,0'),
    pytest.param(0.07, 7, id='0,07'), pytest.param(7, 7, id='7'), pytest.param(7.0, 7, id='7,0'),
    pytest.param(0, 0, id='0'), pytest.param(0.0, 0, id='0,0'), pytest.param(None, 0, id='kein Satz'),
    pytest.param(-1, 0, id='§ 19: der Platzhalter −1'), pytest.param(-1.0, 0, id='§ 19: −1,0'),
    pytest.param(0.16, None, id='16 % hat kein Skonto-Konto'), pytest.param(16, None, id='16'),
    pytest.param(0.05, None, id='5 %'), pytest.param(5, None, id='5'), pytest.param(0.055, None, id='5,5 %'),
    pytest.param(1, None, id='1 %'), pytest.param(100, None, id='100 %'), pytest.param(0.1904, None, id='19,04 % ist nicht 19 %'),
])
def test_skonto_rate_key(tax_rate, expected):
    assert skonto_rate_key(tax_rate) == expected


# ── skonto_limit ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('gross, percent', [
    (1190000, Decimal('2.00')), (1070000, Decimal('2.00')), (990000, Decimal('0.50')), (9900, Decimal('0.50')), (1000000, Decimal('3.00')),
    (1190000, Decimal('1.50')), (1234567, Decimal('2.25')), (99, Decimal('99.99')), (0, Decimal('2.00')), (1190000, 2.5),
])
def test_skonto_limit_is_skonto_amount(gross, percent):
    assert skonto_limit(gross, percent) == skonto_amount(gross, percent)


def test_skonto_limit_values():
    assert skonto_limit(1190000, Decimal('2.00')) == 23800                  # 2 % von 119,00 € = 2,38 €
    assert skonto_limit(1070000, Decimal('2.00')) == 21400
    assert skonto_limit(9900, Decimal('0.50')) == 0                         # 0,99 € × 0,5 % sind 0,00 €, nicht 0,01 €


# ── skonto_terms ─────────────────────────────────────────────────────────────

def _terms(**over):
    values = dict(gross=1190000, days=14, percent=2.0, tax_rate=0.19, mixed=None)
    values.update(over)
    return skonto_terms(**values)


def test_skonto_terms_accepts_a_plain_invoice():
    assert _terms() == (None, (19, 23800))
    assert _terms(tax_rate=0.07, gross=1070000) == (None, (7, 21400))
    assert _terms(tax_rate=0, gross=1000000) == (None, (0, 20000))
    assert _terms(tax_rate=-1, gross=1000000) == (None, (0, 20000))        # § 19
    assert _terms(percent='2.50', days='7') == (None, (19, 29800))         # wie gespeichert: Text oder Zahl, gelesen wie überall


@pytest.mark.parametrize('over, expected', [
    pytest.param(dict(revenue_on_discount=True), SKONTO_REVENUE_DISCOUNT, id='Erlöskonto der Stammdaten ist ein Skonto-Konto'),
    pytest.param(dict(revenue_on_discount=True, tax_rate=0.16, days=None, percent=None), SKONTO_RATE, id='der Satz vor dem Erlöskonto'),
    pytest.param(dict(revenue_on_discount=True, days=None, percent=None), SKONTO_REVENUE_DISCOUNT, id='das Erlöskonto vor den Bedingungen'),
    pytest.param(dict(tax_rate=0.16), SKONTO_RATE, id='16 %'),
    pytest.param(dict(tax_rate=0.05), SKONTO_RATE, id='5 %'),
    pytest.param(dict(tax_rate=0.16, days=None, percent=None), SKONTO_RATE, id='der Satz vor den Bedingungen'),
    pytest.param(dict(days=None, percent=None), SKONTO_NO_TERMS, id='keine Bedingungen'),
    pytest.param(dict(days=14, percent=None), SKONTO_NO_TERMS, id='nur Tage'),
    pytest.param(dict(days=None, percent=2.0), SKONTO_NO_TERMS, id='nur Prozent'),
    pytest.param(dict(days=0, percent=0), SKONTO_NO_TERMS, id='0 und 0'),
    pytest.param(dict(days=400), SKONTO_NO_TERMS, id='Frist über 365 Tage'),
    pytest.param(dict(percent=100), SKONTO_NO_TERMS, id='100 %'),
    pytest.param(dict(percent=2.005), SKONTO_NO_TERMS, id='drei Nachkommastellen'),
    pytest.param(dict(days='x'), SKONTO_NO_TERMS, id='Text statt Tage'),
    pytest.param(dict(percent=float('nan')), SKONTO_NO_TERMS, id='keine Zahl'),
])
def test_skonto_terms_refusals_in_their_order(over, expected):
    assert _terms(**over) == (expected, None)


@pytest.mark.parametrize('mixed', [None, 0, True, 1.0, '1', 2])
def test_only_the_whole_number_one_marks_a_rate_per_position(mixed):
    """Das Kennzeichen wird streng gelesen (``has_mixed_rates``): wie überall zählt nur die ganze Zahl 1."""
    assert _terms(mixed=mixed) == (None, (19, 23800))


def test_skonto_terms_does_not_look_at_the_deadline():
    """Die Frist (``days``) bestimmt nur, bis wann es gedruckt Skonto gibt – geprüft wird sie nie, nicht einmal ein Datum kommt herein."""
    assert _terms(days=1) == _terms(days=365) == (None, (19, 23800))


# ── skonto_open_minor ────────────────────────────────────────────────────────

def _open(**over):
    values = dict(open_minor=1190000, gross=1190000, days=14, percent=2.0, tax_rate=0.19, mixed=None)
    values.update(over)
    return skonto_open_minor(**values)


def test_skonto_open_minor_is_the_open_amount_less_the_discount():
    assert _open() == 1166200                                               # 119,00 − 2,38 = 116,62
    assert _open(open_minor=690000) == 666200                               # nach einer Teilzahlung von 50,00: 69,00 − 2,38 = 66,62
    assert _open(tax_rate=0, gross=1000000, open_minor=1000000) == 980000


@pytest.mark.parametrize('over', [
    pytest.param(dict(mixed=1), id='gemischt ohne lesbare Gruppen'), pytest.param(dict(revenue_on_discount=True), id='Erlöskonto der Stammdaten ist ein Skonto-Konto'),
    pytest.param(dict(tax_rate=0.16), id='16 %'), pytest.param(dict(days=None, percent=None), id='ohne Bedingungen'),
    pytest.param(dict(open_minor=23800), id='der offene Betrag ist nicht größer als das Skonto'),
    pytest.param(dict(open_minor=10000), id='der offene Betrag ist kleiner als das Skonto'),
    pytest.param(dict(open_minor=0), id='nichts offen'),
    pytest.param(dict(gross=9900, percent=0.5, open_minor=9900), id='das Skonto ist 0,00 €'),
])
def test_skonto_open_minor_is_none_without_a_discount_to_take(over):
    assert _open(**over) is None


# ── Konten und feste Sätze ───────────────────────────────────────────────────

def test_the_discount_accounts_are_the_three_standard_accounts_of_the_seed_file():
    with open(SEED, encoding='utf-8') as handle:
        seed = {account['account_number']: account for account in json.load(handle)['accounts']}

    assert SKONTO_ACCOUNTS == {19: (4736, 'Gewährte Skonti 19 % USt'), 7: (4731, 'Gewährte Skonti 7 % USt'), 0: (4730, 'Gewährte Skonti')}
    assert SKONTO_ACCOUNT_NUMBERS == {4730, 4731, 4736}
    for number, name in SKONTO_ACCOUNTS.values():
        assert (seed[number]['name'], seed[number]['description'], seed[number]['is_standard']) == (name, 'Betriebliche Erträge', True)


def test_the_seed_file_stays_sorted_with_the_new_accounts():
    with open(SEED, encoding='utf-8') as handle:
        numbers = [account['account_number'] for account in json.load(handle)['accounts']]
    assert numbers == sorted(numbers) and len(numbers) == len(set(numbers))
    assert numbers.index(4731) == numbers.index(4730) + 1 and numbers.index(4736) == numbers.index(4731) + 1


def test_the_fixed_sentences():
    assert SKONTO_NO_TERMS == 'Diese Rechnung sieht kein Skonto vor.'
    assert SKONTO_RATE == 'Skonto lässt sich für den Steuersatz dieser Rechnung noch nicht ausbuchen.'
    assert SKONTO_NO_REST == 'Nach dieser Zahlung bleibt kein Rest – es gibt kein Skonto auszubuchen.'
    assert SKONTO_TOO_LARGE == 'Der Rest nach dieser Zahlung ({rest}) ist größer als das Skonto der Rechnung ({skonto}).'
    assert SKONTO_REFUND == 'Skonto gibt es nur bei einer Zahlung, nicht bei einer Erstattung.'
    assert SKONTO_STORNO_EDITED == ('Die Skonto-Buchung einer Zahlung dieser Rechnung wurde geändert – bitte die Zahlung erst entfernen und neu zuordnen.')
    assert SKONTO_NOT_CENTS == 'Skonto lässt sich nur für einen Rest in ganzen Cent ausbuchen.'
    assert SKONTO_ALREADY == 'An dieser Rechnung ist schon ein Skonto gebucht.'
    assert SKONTO_REMOVAL_REFUSED == 'An der Rechnung {number} ist eine Zahlung mit Skonto gebucht – bitte zuerst diese Zahlung entfernen.'


def test_the_sentences_are_short_and_carry_no_placeholder_but_their_own():
    sentences = [SKONTO_NO_TERMS, SKONTO_RATE, SKONTO_NO_REST, SKONTO_REFUND, SKONTO_STORNO_EDITED, SKONTO_NOT_CENTS, SKONTO_ALREADY]
    for text in sentences:
        assert text.endswith('.') and '{' not in text and '}' not in text and len(text) < 160, text
    assert SKONTO_REMOVAL_REFUSED.format(number='R26003') == 'An der Rechnung R26003 ist eine Zahlung mit Skonto gebucht – bitte zuerst diese Zahlung entfernen.'
    assert SKONTO_REMOVAL_REFUSED.endswith('.') and len(SKONTO_REMOVAL_REFUSED) < 160
    shown = SKONTO_TOO_LARGE.format(rest=fmt_eur(from_minor(23800)), skonto=fmt_eur(from_minor(10000)))
    assert shown == 'Der Rest nach dieser Zahlung (2,38 €) ist größer als das Skonto der Rechnung (1,00 €).'


def test_settled_sql_counts_the_money_and_the_valid_discount():
    assert valid_skonto_sql() == "(typeof(SkontoAmount) = 'integer' AND SkontoAmount > 0)"
    assert valid_skonto_sql('ip') == "(typeof(ip.SkontoAmount) = 'integer' AND ip.SkontoAmount > 0)"
    assert settled_sql() == "Amount + CASE WHEN (typeof(SkontoAmount) = 'integer' AND SkontoAmount > 0) THEN SkontoAmount ELSE 0 END"
    assert settled_sql('ip') == "ip.Amount + CASE WHEN (typeof(ip.SkontoAmount) = 'integer' AND ip.SkontoAmount > 0) THEN ip.SkontoAmount ELSE 0 END"
    with closing(sqlite3.connect(':memory:')) as con:
        con.execute('CREATE TABLE InvoicePayments (ID INTEGER PRIMARY KEY, InvoiceID INTEGER, Amount INTEGER NOT NULL, SkontoAmount INTEGER)')
        con.executemany('INSERT INTO InvoicePayments (InvoiceID, Amount, SkontoAmount) VALUES (?, ?, ?)',
                        [(1, 1166200, 23800), (2, 500000, None), (2, 690000, 0), (3, -100, None)])
        assert con.execute(f'SELECT InvoiceID, SUM({settled_sql()}) FROM InvoicePayments GROUP BY InvoiceID ORDER BY InvoiceID').fetchall() == [
            (1, 1190000), (2, 1190000), (3, -100)]
        assert con.execute(f'SELECT SUM({settled_sql("ip")}) FROM InvoicePayments ip').fetchone()[0] == 2379900


@pytest.mark.parametrize('literal, kind', [("'abc'", 'text'), ("'23800abc'", 'text'), ('23800.5', 'real'), ('0', 'integer'), ('-23800', 'integer'), ("X'4142'", 'blob')],
                         ids=['text', 'text-with-a-number', 'fraction', 'zero', 'negative', 'blob'])
def test_an_invalid_discount_value_is_never_settled_and_never_valid(literal, kind):
    """Nur eine ganze Zahl über 0 zählt als Skonto (Text, eine Kommazahl, 0, eine negative Zahl und ein Blob nur über die SQL-Konsole): Der Wert wird nie gerechnet – auch nicht über
    die Zahl, die SQLite einem Text vorn abliest (``'23800abc'`` ist in einer Rechnung 23800)."""
    with closing(sqlite3.connect(':memory:')) as con:
        con.execute('CREATE TABLE InvoicePayments (ID INTEGER PRIMARY KEY, InvoiceID INTEGER, Amount INTEGER NOT NULL, SkontoAmount INTEGER)')
        con.execute(f'INSERT INTO InvoicePayments (InvoiceID, Amount, SkontoAmount) VALUES (1, 1166200, {literal})')

        assert con.execute('SELECT typeof(SkontoAmount) FROM InvoicePayments').fetchone() == (kind,)
        assert con.execute(f'SELECT {settled_sql()} FROM InvoicePayments').fetchone() == (1166200,)
        assert con.execute(f'SELECT {valid_skonto_sql()} FROM InvoicePayments').fetchone() == (0,)
        assert con.execute(f'SELECT {settled_sql("ip")} FROM InvoicePayments ip').fetchone() == (1166200,)


# ── payment_rows(skonto=…) ───────────────────────────────────────────────────

BASE = dict(number='R1', customer_id=7, bank_id=11, bank_date='2026-02-01', bank_tax_date='2026-02-02', recipient='Käufer AG', currency='EUR',
            amount=1190000, tax_rate=0.19, tax_value=190000, bank_coa=100, revenue_coa=400, debtor_side=300, waiting_side=200)


def _values(rows):
    return [(row.values['EntryForm'], row.values['COA_ID'], row.values['CounterCOA_ID'], row.values['Amount'], row.values['TaxRate'],
             row.values['TaxAmount'], row.values['Text']) for row in rows]


def test_payment_rows_without_skonto_are_what_they_always_were():
    rows = payment_rows(**BASE)

    assert _values(rows) == [('liquid_first', 100, 300, 1190000, None, None, 'Zahlung zu Re. R1'),
                             ('transfer', 200, 400, 1190000, 0.19, 190000, 'Umb. zu Re. R1')]
    assert [row.description for row in rows] == ['Payment child (bank to debtor)', 'Payment child (open item to revenue)']
    assert payment_rows(**BASE, skonto=None) == rows
    assert _values(payment_rows(**{**BASE, 'debtor_side': None, 'waiting_side': None})) == [
        ('purpose_first', 400, 100, 1190000, 0.19, 190000, 'Zahlung zu Re. R1')]


def test_payment_rows_with_skonto_book_the_whole_rest_and_the_discount_last():
    skonto = SkontoPart(500, 23800, 0.19, 3800)
    rows = payment_rows(**{**BASE, 'amount': 1166200}, skonto=skonto)

    assert _values(rows) == [('liquid_first', 100, 300, 1190000, None, None, 'Zahlung zu Re. R1'),                 # Geld + Skonto
                             ('transfer', 200, 400, 1190000, 0.19, 190000, 'Umb. zu Re. R1'),                      # mit dem Rest der Rechnungssteuer
                             ('purpose_first', 500, 100, -23800, 0.19, -3800, 'Skonto zu Re. R1')]                  # zuletzt: Skonto-Konto an Bank
    assert [row.description for row in rows][-1] == 'Payment child (discount to bank)'
    for row in rows:                                                                                               # alle markiert, alle mit den Angaben der Bewegung
        assert row.values['Status'] == PAYMENT_MARK and row.values['DocumentNumber'] == 'R1' and row.values['ParentBooking_ID'] == 11
        assert (row.values['DateBooking'], row.values['DateTax'], row.values['RecipientClient'], row.values['Contact_ID'], row.values['Currency']) == (
            '2026-02-01', '2026-02-02', 'Käufer AG', 7, 'EUR')
    assert rows[0].values['Amount'] + rows[2].values['Amount'] == 1166200                                          # die Bankseite ergibt genau das Geld


def test_payment_rows_with_skonto_without_a_receivable_are_two():
    skonto = SkontoPart(500, 23800, 0.19, 3800)
    rows = payment_rows(**{**BASE, 'amount': 1166200, 'debtor_side': None, 'waiting_side': None}, skonto=skonto)

    assert _values(rows) == [('purpose_first', 400, 100, 1190000, 0.19, 190000, 'Zahlung zu Re. R1'),
                             ('purpose_first', 500, 100, -23800, 0.19, -3800, 'Skonto zu Re. R1')]
    assert rows[0].values['Amount'] + rows[1].values['Amount'] == 1166200


@pytest.mark.parametrize('with_receivable', [True, False])
def test_payment_rows_with_skonto_at_zero_percent_carry_no_tax(with_receivable):
    sides = {} if with_receivable else {'debtor_side': None, 'waiting_side': None}
    rows = payment_rows(**{**BASE, 'amount': 980000, 'tax_rate': None, 'tax_value': None, **sides}, skonto=SkontoPart(500, 20000, None, None))

    assert all(row.values['TaxRate'] is None and row.values['TaxAmount'] is None for row in rows)
    assert rows[-1].values['Amount'] == -20000 and rows[-1].values['EntryForm'] == 'purpose_first'
    assert sum(row.values['Amount'] for row in (rows[0], rows[-1])) == 980000


def test_payment_rows_refuse_a_discount_where_there_cannot_be_one():
    skonto = SkontoPart(500, 23800, 0.19, 3800)
    with pytest.raises(ValueError):
        payment_rows(**{**BASE, 'amount': 1166200}, skonto=skonto, parts=[payments.PaymentPart(200, 400, 1166200, 0.19, 180000)])
    with pytest.raises(ValueError):
        payment_rows(**{**BASE, 'amount': -1166200}, skonto=skonto)                          # eine Erstattung
    with pytest.raises(ValueError):
        payment_rows(**BASE, skonto=SkontoPart(500, 0, None, None))                          # ohne Betrag
    with pytest.raises(ValueError):
        payment_rows(**BASE, skonto=SkontoPart(500, -100, None, None))


# ── _recalc_invoice: Geld plus Skonto ────────────────────────────────────────

def _paid_with(db, amount_paid, skonto):
    own, customer = add_contacts(db)
    invoice = add_document(db, '26R100', 'invoice', own, customer, status='sent')
    bank = db.insert_booking('2026-07-01', amount_paid, booking_type='bank')
    db.link_invoice_to_transaction(invoice, bank, amount_paid)
    with closing(sqlite3.connect(db.db_name)) as con:
        con.execute('UPDATE InvoicePayments SET SkontoAmount = ?', (skonto,))
        con.commit()
    with closing(db._get_connection()) as conn:
        db._recalc_invoice(conn, invoice)
        conn.commit()
    with closing(sqlite3.connect(db.db_name)) as con:
        return con.execute('SELECT Status, AmountDue FROM Invoices WHERE ID = ?', (invoice,)).fetchone()


def test_an_invoice_with_a_discount_set_by_hand_is_paid_and_has_nothing_open(tmp_db):
    assert _paid_with(tmp_db, 116.62, 23800) == ('paid', 0)


def test_without_the_discount_the_same_payment_leaves_a_partial_payment(tmp_db):
    assert _paid_with(tmp_db, 116.62, None) == ('partial_payment', 23800)


def test_a_discount_that_does_not_close_the_invoice_leaves_the_rest_open(tmp_db):
    assert _paid_with(tmp_db, 116.62, 20000) == ('partial_payment', 3800)           # 119,00 − 116,62 − 2,00 = 0,38


def test_a_discount_of_zero_counts_nothing(tmp_db):
    assert _paid_with(tmp_db, 116.62, 0) == ('partial_payment', 23800)
