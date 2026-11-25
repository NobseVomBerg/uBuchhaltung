# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Skonto bei einer Rechnung mit mehreren Steuersätzen: Buchen (``assign_invoice_payment(…, skonto=True)``, Stufe 2).

Die feste Rechnung ist **M** (``M-100``): 19 % × 100,00 (Steuer 19,00, brutto 119,00) und 7 % × 50,00 (3,50 / 53,50), zusammen 172,50, Skonto 2 % in 14 Tagen = 3,45 €. Die Zahlung, die M
schließt, bucht das Skonto je Satz – mit Forderung fünf markierte Sätze unter der Bankbewegung (ohne Forderung vier):

1. „Bank an Debitoren“ über Geld plus Skonto (172,50), 2./3. je Satz „Wartekonto an Erlöskonto“ (119,00 / 19,00 und 53,50 / 3,50 – jede Gruppe mit ihrem Rest und dem Rest ihrer Steuer),
4./5. je Satz „Skonto-Konto an Bank“ (4736: −2,38 / −0,38, 4731: −1,07 / −0,07), in der Reihenfolge der Gruppen (Satz absteigend). ``InvoicePayments.Amount`` bleibt das Geld (169,05),
``SkontoAmount`` ist der Rest (3,45). Das Skonto verteilt sich im Verhältnis zum Brutto der Gruppen (``tax_groups.split_skonto``), die Steuer je Gruppe rechnet ``skonto_tax`` einmal auf den Cent.

Geprüft wird: die Beispiele a (genau), b (ein Cent mehr, ein Cent weniger), c (Teilzahlung vor der schließenden), d (drei Gruppen, eine steuerfreie aus den Stammdaten) Spalte für Spalte,
mit und ohne Forderung; der Beleg mit EINEM Satz aus den Stammdaten (19 % mit eigenem Erlöskonto, steuerfrei → 4730 ohne Steuer); jede Ablehnung ohne Schreiben und ohne neues Konto
(``SKONTO_GROUP_RATE``, ``SKONTO_REVENUE_DISCOUNT``, ``SKONTO_MIXED_SPLIT`` – auch nach Teilzahlungen, die die App selbst gebucht hat –, ``SKONTO_RATE``, ``MIXED_*``, die Forderungen schon in der
Vorprüfung, ``SKONTO_NO_TERMS`` vor jedem anderen Grund); die Konten, die eine Zuordnung anlegt (nur die der Sätze des Belegs); das Netz in der Transaktion (bei EINEM Satz auch die
Momentaufnahme der Stammdaten); Entfernen, der Bank-Editor, ``payment_precheck`` und die Route.

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import json
from decimal import Decimal

import pytest

import server.handlers as handlers
from db.invoice_payments import (MIXED_ALLOCATION_UNREADABLE, MIXED_GROUPS_MISMATCH, MIXED_RECEIVABLE_MISMATCH, PAYMENT_CHANGED_MESSAGE, PAYMENT_MARK, SKONTO_GROUP_RATE, SKONTO_MIXED_SPLIT,
                                 SKONTO_NO_TERMS, SKONTO_NOT_CENTS, SKONTO_RATE, SKONTO_REVENUE_DISCOUNT, SKONTO_TOO_LARGE, Snapshot, TAX_ENTRY_CONTRADICTION)
from money import from_minor, to_minor
from server.pages_invoice import PageInvoice
from server.pages_transactions import PageTransactions
from tax_groups import item_groups
from tests import installation
from tests import liquidity_helpers as liq
from tests.entry_form_roundtrip import differences, open_form, snapshot, submit
from tests.susa_helpers import account_id
from tests.test_skonto_booking import (SKONTO_NUMBERS, _assert_refused, _bank, _change_after_the_precheck, _coa, _euer, _exec, _kid_ids, _kids, _payment, _raw, _state, _tables,
                                       _without_discount_accounts)

D = Decimal
NUMBER = 'M-100'
DAY = '2025-03-03'
REASON = 'Steuerfreie Leistung (erfundener Testgrund)'
FREE, REDUCED = 4120, 4150                          # Erlöskonten, die der Standard-Kontenrahmen nicht führt
PLAIN_19, PLAIN_7 = (19, '100.00', None), (7, '50.00', None)
ITEMS = [PLAIN_19, PLAIN_7]
M_ROWS = [                                           # Beispiel a mit Forderung: Form, Konto, Gegenkonto, Betrag, Steuer, Satz
    ('liquid_first', 1800, 10000, D('172.5'), None, None),
    ('transfer', 4405, 4400, D('119'), D('19'), 0.19),
    ('transfer', 4345, 4300, D('53.5'), D('3.5'), 0.07),
    ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38'), 0.19),
    ('purpose_first', 4731, 1800, D('-1.07'), D('-0.07'), 0.07)]


# ── Welt und Hilfen ──────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_db):
    liq.two_banks(tmp_db)
    return tmp_db


@pytest.fixture
def acct(db):
    return account_id(db, 'Probe-Giro')


def account(db, number):
    """Das Erlöskonto ``number`` (COA-ID); gibt es das Konto nicht, entsteht es im Standard-Kontenrahmen."""
    if db.get_coa_id_by_account_number(number) is None:
        db.insert_chart_of_accounts(4, number, f'Erlöse (erfundenes Testkonto {number})', 'Betriebliche Erträge')
    return db.get_coa_id_by_account_number(number)


def free(net, coa, reason=REASON):
    """Eine steuerfreie Position mit der Momentaufnahme ihres Eintrags: Art ``'E'``, Satz 0, Grund, Erlöskonto – ``(Satz, Netto, Momentaufnahme)``."""
    return (0, net, {'tax_category': 'E', 'tax_rate_bp': 0, 'tax_exempt_reason': reason, 'revenue_coa_id': coa})


def entry(pct, net, coa):
    """Eine Position mit der Momentaufnahme eines Eintrags mit Satz ``pct`` (Art ``'S'``, Satz in Hundertstel Prozent, Erlöskonto ``coa`` oder ``None``)."""
    return (D(str(pct)), net, {'tax_category': 'S', 'tax_rate_bp': int(D(str(pct)) * 100), 'tax_exempt_reason': None, 'revenue_coa_id': coa})


def mixed_invoice(db, number=NUMBER, parts=None, day=DAY, skonto=(14, 2.0), receivable=True, status='sent', **over):
    """Eine Rechnung aus ``(Satz in Prozent, Netto in Euro, Momentaufnahme oder None)``-Positionen mit Skonto-Bedingungen ``(Tage, Prozent)`` – mit Steuersatz je Position (Kennzeichen 1), wenn die
    Positionen mehr als einen Satz tragen; ihre Summen rechnet ``tax_groups.item_groups`` (so rechnet auch das Speichern der App). Mit ``receivable`` und ``status`` ``sent`` samt
    Forderung (je Satz eine, ``ensure_receivable_booking``; ein Beleg mit einem Satz ohne Wartekonto, etwa 5,5 %, bucht keine). Die Momentaufnahme schreibt kein Weg der App außer
    dem Speichern mit einem Eintrag der Stammdaten – hier über ``insert_invoice_item``."""
    parts = ITEMS if parts is None else parts
    groups = item_groups([(float(pct), to_minor(D(net)), None if shot is None else shot['tax_rate_bp']) for pct, net, shot in parts])
    net, tax = sum(group.net for group in groups), sum(group.tax for group in groups)
    data = {'invoice_number': number, 'invoice_date': day, 'delivery_date': day, 'payment_due_date': '2099-12-31', 'seller_name': 'Vera Verkäufer',
            'seller_company': 'Verkäufer GmbH', 'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': float(max(group.pct for group in groups) / 100),
            'sum_net': from_minor(net), 'tax_amount': from_minor(tax), 'sum_gross': from_minor(net + tax), 'amount_due': from_minor(net + tax), 'status': status,
            'mixed_tax_rates': 1 if len(groups) > 1 else None,
            'skonto_days': skonto[0] if skonto else None, 'skonto_percent': skonto[1] if skonto else None}
    data.update(over)
    invoice_id = db.insert_invoice(data)
    for position, (pct, net_euro, shot) in enumerate(parts, 1):
        db.insert_invoice_item({'invoice_id': invoice_id, 'position': position, 'description': f'Position {position}', 'quantity': 1, 'unit': 'C62',
                                'price_per_unit': D(net_euro), 'total_net': D(net_euro), 'tax_rate': float(pct), **(shot or {})})
    if receivable and status == 'sent':
        db.ensure_receivable_booking(invoice_id)
    return invoice_id


def no_accounts(db):
    """Die Konten, die eine Zuordnung anlegen könnte (die drei Skonto-Konten und 4184/4185), fehlen – eine abgelehnte Zuordnung legt keines an (``_assert_refused`` prüft es)."""
    _without_discount_accounts(db, *SKONTO_NUMBERS, 4184, 4185)


def assign(db, invoice_id, bank_id, amount=None, skonto=True):
    result = db.assign_invoice_payment(invoice_id, bank_id, amount, None, skonto)
    assert result.ok, result.message
    return result


def shape(rows):
    """Form, Konten, Betrag, Steuer und Satz jedes Satzes – ohne Text und Marke."""
    return [(row[0], row[1], row[2], row[3], row[4], row[5]) for row in rows]


def tax_of(db, number=NUMBER):
    """Die Summe der Steuer aller markierten Sätze der Nummer (Euro): die Steuer des gezahlten Geldes."""
    return D(_raw(db, 'SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE Status = ? AND DocumentNumber = ?', PAYMENT_MARK, number)[0][0]) * D('0.0001')


# ═══ Die Beispiele, Spalte für Spalte ════════════════════════════════════════

def test_example_a_with_a_receivable_books_five_rows_and_the_discount_per_rate(db, acct):
    """169,05 € gezahlt, 3,45 € Skonto: Verteilung 7 % 1,07 (3,45 × 53,50 / 172,50 – glatt), 19 % der Rest 2,38; Steuer 0,38 und 0,07."""
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')

    result = assign(db, invoice, bank)

    kids = _kids(db, bank)
    assert shape(kids) == M_ROWS
    assert [row[6] for row in kids] == ['Zahlung zu Re. M-100', 'Umb. zu Re. M-100', 'Umb. zu Re. M-100', 'Skonto zu Re. M-100', 'Skonto zu Re. M-100']
    assert {row[7] for row in kids} == {PAYMENT_MARK}
    assert _payment(db, invoice) == [(1690500, 34500, '2025-04-10')]                                                         # Amount bleibt das Geld
    assert _state(db, invoice) == ('paid', 0)
    assert result.revenue_coa_id is None and result.payment_id == _raw(db, 'SELECT ID FROM InvoicePayments')[0][0]           # je Satz ein Erlöskonto: keines für die Zuordnung
    assert _euer(db) == {4400: 100.0, 4300: 50.0, 4736: -2.0, 4731: -1.0, 3806: 22.05}                                       # die Steuer des gezahlten Geldes: 22,50 − 0,38 − 0,07
    assert db.get_invoice_skonto(invoice) == {result.payment_id: D('3.45')}
    for row in _raw(db, 'SELECT DocumentNumber, AutoMirror, DateBooking, DateTax, RecipientClient, Currency, BookingType, SourceGroup, Account_ID, Category_ID '
                        'FROM Bookings WHERE ParentBooking_ID = ?', bank):
        assert row == (NUMBER, 0, '2025-04-10', None, 'Käufer AG', 'EUR', 'entry', None, None, None)


def test_example_a_without_a_receivable_books_four_rows(db, acct):
    invoice = mixed_invoice(db, receivable=False)
    bank = _bank(db, acct, '169.05')

    assign(db, invoice, bank)

    assert shape(_kids(db, bank)) == [
        ('purpose_first', 4400, 1800, D('119'), D('19'), 0.19), ('purpose_first', 4300, 1800, D('53.5'), D('3.5'), 0.07),             # je Satz Erlöskonto an Bank
        ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38'), 0.19), ('purpose_first', 4731, 1800, D('-1.07'), D('-0.07'), 0.07)]
    assert _payment(db, invoice) == [(1690500, 34500, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert _euer(db) == {4400: 100.0, 4300: 50.0, 4736: -2.0, 4731: -1.0, 3806: 22.05}


def test_example_b_a_cent_less_gives_the_discount_one_cent_more_per_the_rounding_of_each_share(db, acct):
    """169,06 € gezahlt, Skonto 3,44 €: 7 % 3,44 × 53,50 / 172,50 = 1,0669 → 1,07, 19 % der Rest 2,37; Steuer 2,37 × 19/119 = 0,3784 → 0,38."""
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.06')

    assign(db, invoice, bank)

    assert shape(_kids(db, bank))[3:] == [('purpose_first', 4736, 1800, D('-2.37'), D('-0.38'), 0.19), ('purpose_first', 4731, 1800, D('-1.07'), D('-0.07'), 0.07)]
    assert shape(_kids(db, bank))[:3] == M_ROWS[:3] and _payment(db, invoice) == [(1690600, 34400, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert _euer(db) == {4400: 100.0, 4300: 50.0, 4736: -1.99, 4731: -1.0, 3806: 22.05}


def test_example_b_one_cent_more_money_takes_the_cent_from_the_7_percent_share(db, acct):
    """169,07 € gezahlt, Skonto 3,43 €: 7 % 1,0638 → 1,06, 19 % 2,37; Steuer 1,06 × 7/107 = 0,0693 → 0,07."""
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.07')

    assign(db, invoice, bank)

    assert shape(_kids(db, bank))[3:] == [('purpose_first', 4736, 1800, D('-2.37'), D('-0.38'), 0.19), ('purpose_first', 4731, 1800, D('-1.06'), D('-0.07'), 0.07)]
    assert _payment(db, invoice) == [(1690700, 34300, '2025-04-10')] and _euer(db)[4731] == -0.99


def test_example_b_one_cent_less_money_is_more_than_the_discount_of_the_invoice(db, acct):
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.04')                                                                                           # Rest 3,46 € – das Skonto der Rechnung sind 3,45 €
    no_accounts(db)

    _assert_refused(db, invoice, bank, SKONTO_TOO_LARGE.format(rest='3,46 €', skonto='3,45 €'))
    assert SKONTO_TOO_LARGE.format(rest='3,46 €', skonto='3,45 €') == 'Der Rest nach dieser Zahlung (3,46 €) ist größer als das Skonto der Rechnung (3,45 €).'
    assert _payment(db, invoice) == [] and _kid_ids(db, bank) == []


def test_example_c_a_part_payment_before_the_closing_one_with_the_discount(db, acct):
    """Erst 100,00 € (68,99 + 31,01, Steuer 11,02 + 2,03), dann 69,05 € mit Skonto: Die schließende Zahlung nimmt den Rest jeder Gruppe und ihrer Steuer (50,01 / 7,98 und 22,49 / 1,47)."""
    invoice = mixed_invoice(db)
    first, second = _bank(db, acct, '100.00', date='2025-04-02'), _bank(db, acct, '69.05', date='2025-05-05')

    assign(db, invoice, first, skonto=False)
    assert shape(_kids(db, first)) == [('liquid_first', 1800, 10000, D('100'), None, None), ('transfer', 4405, 4400, D('68.99'), D('11.02'), 0.19),
                                       ('transfer', 4345, 4300, D('31.01'), D('2.03'), 0.07)]
    assert _state(db, invoice) == ('partial_payment', D('72.5'))
    assign(db, invoice, second)

    assert shape(_kids(db, second)) == [
        ('liquid_first', 1800, 10000, D('72.5'), None, None), ('transfer', 4405, 4400, D('50.01'), D('7.98'), 0.19), ('transfer', 4345, 4300, D('22.49'), D('1.47'), 0.07),
        ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38'), 0.19), ('purpose_first', 4731, 1800, D('-1.07'), D('-0.07'), 0.07)]
    assert _payment(db, invoice) == [(1000000, None, '2025-04-02'), (690500, 34500, '2025-05-05')] and _state(db, invoice) == ('paid', 0)
    assert tax_of(db) == D('22.05')                                                                                          # 11,02 + 2,03 + 7,98 + 1,47 − 0,38 − 0,07
    assert _euer(db) == {4400: 100.0, 4300: 50.0, 4736: -2.0, 4731: -1.0, 3806: 22.05}
    assert db.link_bank_to_entries().get('resolved') == 2                                                                    # Stufe 7: 100,00 + 172,50 „an 10000“ decken beide Forderungen


def test_example_d_three_groups_one_of_them_tax_free_from_the_master_data(db, acct):
    """19 % (119,00), 7 % (53,50) und steuerfrei (100,00, Momentaufnahme mit Erlöskonto 4120): brutto 272,50, Skonto 5,45 € – 7 % 1,07, steuerfrei 2,00, 19 % der Rest 2,38."""
    invoice = mixed_invoice(db, parts=[PLAIN_19, PLAIN_7, free('100.00', account(db, FREE))])
    bank = _bank(db, acct, '267.05')

    assign(db, invoice, bank)

    assert shape(_kids(db, bank)) == [
        ('liquid_first', 1800, 10000, D('272.5'), None, None), ('transfer', 4405, 4400, D('119'), D('19'), 0.19), ('transfer', 4345, 4300, D('53.5'), D('3.5'), 0.07),
        ('transfer', 4340, FREE, D('100'), None, None),                                                                      # steuerfrei: Wartekonto 4340, das Erlöskonto der Momentaufnahme
        ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38'), 0.19), ('purpose_first', 4731, 1800, D('-1.07'), D('-0.07'), 0.07),
        ('purpose_first', 4730, 1800, D('-2'), None, None)]                                                                  # 4730 ohne Satz und ohne Steuer
    assert _payment(db, invoice) == [(2670500, 54500, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert _euer(db) == {4400: 100.0, 4300: 50.0, FREE: 100.0, 4736: -2.0, 4731: -1.0, 4730: -2.0, 3806: 22.05}


def test_the_same_assignment_without_a_receivable_for_the_three_groups(db, acct):
    invoice = mixed_invoice(db, parts=[PLAIN_19, PLAIN_7, free('100.00', account(db, FREE))], receivable=False)
    bank = _bank(db, acct, '267.05')

    assign(db, invoice, bank)

    assert shape(_kids(db, bank)) == [
        ('purpose_first', 4400, 1800, D('119'), D('19'), 0.19), ('purpose_first', 4300, 1800, D('53.5'), D('3.5'), 0.07), ('purpose_first', FREE, 1800, D('100'), None, None),
        ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38'), 0.19), ('purpose_first', 4731, 1800, D('-1.07'), D('-0.07'), 0.07), ('purpose_first', 4730, 1800, D('-2'), None, None)]


def test_a_group_the_discount_leaves_out_gets_no_discount_row(db, acct):
    """Ist der Anteil einer Gruppe 0 (ein Skonto von einem Cent), gibt es für sie keinen Skonto-Satz – nur ihre Umbuchung."""
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '172.49')                                                                                           # Rest 0,01 €

    assign(db, invoice, bank)

    assert shape(_kids(db, bank)) == [('liquid_first', 1800, 10000, D('172.5'), None, None), ('transfer', 4405, 4400, D('119'), D('19'), 0.19),
                                      ('transfer', 4345, 4300, D('53.5'), D('3.5'), 0.07), ('purpose_first', 4736, 1800, D('-0.01'), D('0'), 0.19)]
    assert _payment(db, invoice) == [(1724900, 100, '2025-04-10')]


def test_the_discount_accounts_are_the_ones_of_the_rates_of_the_invoice_only(db, acct):
    """Eine Zuordnung legt nur die Skonto-Konten der Sätze des Belegs an, die fehlen: 19 und 7 % ergeben 4736 und 4731, nie 4730; mit einer steuerfreien Gruppe auch 4730."""
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')
    _without_discount_accounts(db, *SKONTO_NUMBERS)

    assign(db, invoice, bank)

    assert [(number, _coa(db, number) is not None) for number in SKONTO_NUMBERS] == [(4730, False), (4731, True), (4736, True)]
    assert _raw(db, 'SELECT Framework, Name, Description, IsStandard FROM ChartOfAccounts WHERE AccountNumber = 4731') == [(4, 'Gewährte Skonti 7 % USt', 'Betriebliche Erträge', 1)]


def test_a_tax_free_group_creates_4730_too(db, acct):
    invoice = mixed_invoice(db, parts=[PLAIN_19, free('100.00', account(db, FREE))])
    bank = _bank(db, acct, '214.62')                                                                                           # 219,00 − 4,38 (2 %)
    _without_discount_accounts(db, *SKONTO_NUMBERS)

    assign(db, invoice, bank)

    assert [(number, _coa(db, number) is not None) for number in SKONTO_NUMBERS] == [(4730, True), (4731, False), (4736, True)]


def test_a_missing_discount_account_of_a_group_does_not_stop_the_others(db, acct):
    """Fehlt von zwei Konten nur eines, entsteht nur dieses; das vorhandene bleibt, wie es ist."""
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')
    _without_discount_accounts(db, 4731)
    present = _raw(db, 'SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4736')

    assign(db, invoice, bank)

    assert _raw(db, 'SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4736') == present and _coa(db, 4731) is not None


# ═══ Der Beleg mit EINEM Satz aus den Stammdaten ═════════════════════════════

def test_one_rate_from_the_master_data_with_its_own_revenue_account_books_the_discount_as_for_any_19_percent(db, acct):
    invoice = mixed_invoice(db, 'S-1', parts=[entry(19, '100.00', account(db, FREE))])
    bank = _bank(db, acct, '116.62')

    assign(db, invoice, bank)

    assert shape(_kids(db, bank)) == [('liquid_first', 1800, 10000, D('119'), None, None), ('transfer', 4405, FREE, D('119'), D('19'), 0.19),
                                      ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38'), 0.19)]
    assert _payment(db, invoice) == [(1166200, 23800, '2025-04-10')] and _state(db, invoice) == ('paid', 0)


def test_a_tax_free_invoice_from_the_master_data_books_the_discount_on_4730_without_tax(db, acct):
    invoice = mixed_invoice(db, 'S-2', parts=[free('100.00', account(db, FREE))])
    bank = _bank(db, acct, '98.00')

    assign(db, invoice, bank)

    assert shape(_kids(db, bank)) == [('liquid_first', 1800, 10000, D('100'), None, None), ('transfer', 4340, FREE, D('100'), None, None),
                                      ('purpose_first', 4730, 1800, D('-2'), None, None)]
    assert _payment(db, invoice) == [(980000, 20000, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert _euer(db) == {FREE: 100.0, 4730: -2.0}


def test_one_rate_with_a_snapshot_but_no_revenue_account_books_as_before(db, acct):
    """19 % aus den Stammdaten ohne eigenes Erlöskonto (die Vorbelegung): das hinterlegte Erlöskonto 4400."""
    invoice = mixed_invoice(db, 'S-3', parts=[entry(19, '100.00', None)])
    bank = _bank(db, acct, '116.62')

    assign(db, invoice, bank)

    assert shape(_kids(db, bank))[1] == ('transfer', 4405, 4400, D('119'), D('19'), 0.19)
    assert _payment(db, invoice) == [(1166200, 23800, '2025-04-10')]


# ═══ Ablehnungen: nichts geschrieben, kein Konto angelegt ════════════════════

def _world(db, acct, parts, paid, **kw):
    """Eine Rechnung aus ``parts`` und eine Bewegung über ``paid`` €; die Konten, die eine Zuordnung anlegen könnte, fehlen."""
    invoice = mixed_invoice(db, parts=parts, **kw)
    bank = _bank(db, acct, paid)
    no_accounts(db)
    return invoice, bank


def test_a_group_with_5_5_percent_refuses_and_names_its_rate(db, acct):
    invoice, bank = _world(db, acct, [PLAIN_19, entry('5.5', '200.00', account(db, REDUCED))], '323.40', receivable=False)         # brutto 330,00, Skonto 6,60
    _assert_refused(db, invoice, bank, SKONTO_GROUP_RATE.format(rate='5,5'))
    assert SKONTO_GROUP_RATE.format(rate='5,5') == 'Skonto lässt sich für 5,5 % dieser Rechnung noch nicht ausbuchen.'


@pytest.mark.parametrize('rate, shown', [(16, '16'), (5, '5')], ids=['16-Prozent', '5-Prozent'])
def test_a_plain_group_of_another_rate_refuses_too(db, acct, rate, shown):
    invoice, bank = _world(db, acct, [PLAIN_19, (rate, '100.00', None)], '230.00', receivable=False)
    _exec(db, 'UPDATE Invoices SET SkontoPercent = 5.0 WHERE ID = ?', invoice)
    _assert_refused(db, invoice, bank, SKONTO_GROUP_RATE.format(rate=shown))


def test_the_first_group_without_a_discount_account_is_named_descending(db, acct):
    invoice, bank = _world(db, acct, [PLAIN_19, (16, '100.00', None), entry('5.5', '100.00', account(db, REDUCED))], '300.00', receivable=False)
    _assert_refused(db, invoice, bank, SKONTO_GROUP_RATE.format(rate='16'))


@pytest.mark.parametrize('number', SKONTO_NUMBERS)
def test_a_revenue_account_of_the_master_data_that_is_a_discount_account_refuses_a_mixed_invoice(db, acct, number):
    """Nur ohne so ein Konto ist in einer Zuordnung mit Skonto jeder Satz auf einem Skonto-Konto ein Skonto-Satz (die Erstattung und die Rücknahme verlassen sich darauf)."""
    invoice = mixed_invoice(db, parts=[PLAIN_19, entry(7, '50.00', _coa(db, number))])
    bank = _bank(db, acct, '169.05')
    before = installation.dump(db.db_name)

    refused = db.assign_invoice_payment(invoice, bank, None, None, True)

    assert (refused.ok, refused.message, refused.payment_id) == (False, SKONTO_REVENUE_DISCOUNT, None)
    assert installation.dump(db.db_name) == before


@pytest.mark.parametrize('number', SKONTO_NUMBERS)
def test_a_revenue_account_of_the_master_data_that_is_a_discount_account_refuses_an_invoice_with_one_rate(db, acct, number):
    invoice = mixed_invoice(db, 'S-4', parts=[entry(19, '100.00', account(db, number))])
    bank = _bank(db, acct, '116.62')
    refused = db.assign_invoice_payment(invoice, bank, None, None, True)

    assert (refused.ok, refused.message) == (False, SKONTO_REVENUE_DISCOUNT)
    assert SKONTO_REVENUE_DISCOUNT == 'Skonto lässt sich nicht ausbuchen: Ein Erlöskonto dieser Rechnung ist selbst ein Skonto-Konto.'
    assert _kid_ids(db, bank) == [] and _payment(db, invoice) == []


def test_the_head_rate_of_a_5_5_percent_invoice_refuses_with_the_rate_sentence(db, acct):
    invoice = mixed_invoice(db, 'S-5', parts=[entry('5.5', '200.00', account(db, REDUCED))], receivable=False)
    bank = _bank(db, acct, '206.78')                                                                                           # 211,00 € − 4,22 €
    no_accounts(db)
    _assert_refused(db, invoice, bank, SKONTO_RATE)


def test_a_mixed_invoice_without_discount_terms_refuses(db, acct):
    invoice, bank = _world(db, acct, ITEMS, '169.05', skonto=None)
    _assert_refused(db, invoice, bank, SKONTO_NO_TERMS)


def test_a_mixed_invoice_whose_positions_do_not_match_its_sums_refuses_with_the_fixed_sentence(db, acct):
    invoice, bank = _world(db, acct, ITEMS, '169.05')
    _exec(db, 'UPDATE Invoices SET SumNet = 9990000 WHERE ID = ?', invoice)
    _assert_refused(db, invoice, bank, MIXED_GROUPS_MISMATCH)


def test_a_mixed_invoice_with_contradictory_snapshots_refuses_with_the_fixed_sentence(db, acct):
    invoice, bank = _world(db, acct, ITEMS, '169.05')
    _exec(db, "UPDATE InvoiceItems SET TaxExemptReason = 'Grund ohne Satz' WHERE InvoiceId = ? AND TaxRate = 7", invoice)           # die halbe Momentaufnahme
    _assert_refused(db, invoice, bank, TAX_ENTRY_CONTRADICTION)


def test_a_rate_flag_without_positions_refuses_like_any_unreadable_mixed_invoice(db, acct):
    invoice = mixed_invoice(db, 'S-6', parts=[PLAIN_19], receivable=False)
    _exec(db, 'DELETE FROM InvoiceItems WHERE InvoiceId = ?', invoice)
    _exec(db, 'UPDATE Invoices SET MixedTaxRates = 1 WHERE ID = ?', invoice)
    bank = _bank(db, acct, '116.62')
    no_accounts(db)
    _assert_refused(db, invoice, bank, MIXED_GROUPS_MISMATCH)


def _break_the_split(db, invoice, first):
    """Fall f: Die Teilzahlung über 119,00 € steht – per SQL – ganz auf 19 % (119,00 / 19,00), der Satz der 7 % ist weg."""
    _exec(db, "UPDATE Bookings SET Amount = 1190000, TaxAmount = 190000 WHERE ParentBooking_ID = ? AND EntryForm = 'transfer' AND TaxRate = 0.19", first)
    _exec(db, "DELETE FROM Bookings WHERE ParentBooking_ID = ? AND EntryForm = 'transfer' AND TaxRate = 0.07", first)


@pytest.mark.parametrize('paid, rest', [('50.05', '3,45'), ('52.43', '1,07')], ids=['volles-Skonto', 'Rest-1,07'])
def test_part_payments_that_do_not_fit_the_split_refuse_with_the_fixed_sentence_and_create_no_account(db, acct, paid, rest):
    """Fall f: Die 19 % wären bei einem Skonto von 3,45 € mit 2,38 € dabei (bei 1,07 € Skonto mit 0,74 €) – ihr Rest ist 0. Hier hat ein Eingriff die Teilzahlung geändert; dass es auch ohne
    einen geht, zeigt der nächste Test."""
    invoice = mixed_invoice(db)
    first, second = _bank(db, acct, '119.00', date='2025-04-02'), _bank(db, acct, paid, date='2025-05-05')
    assert db.assign_invoice_payment(invoice, first).ok
    _break_the_split(db, invoice, first)
    no_accounts(db)

    _assert_refused(db, invoice, second, SKONTO_MIXED_SPLIT)
    assert SKONTO_MIXED_SPLIT == 'Das Skonto lässt sich nicht auf die Steuersätze dieser Rechnung aufteilen – die bisherigen Zahlungen lassen es nicht zu.' and rest


def test_part_payments_the_app_booked_itself_can_leave_a_small_group_no_tax_for_its_share_of_the_discount(db, acct):
    """Fall E der Prüfung, ohne jeden Eingriff: 19 % × 1078,00 und 7 % × 4,00 (brutto 1287,10, Skonto 2 % = 25,74 €). Die Teilzahlungen über 675,00 und 582,00 € buchen die Steuer je Satz auf den
    Cent gerundet; bei den 7 % ist die ganze Steuer (0,28 €) weg, bevor ihr Betrag bezahlt ist (Rest 0,10 €). Die schließende Zahlung über 4,36 € mit dem Skonto von 25,74 € trüge davon 0,09 € mit 0,01 €
    Steuer – mehr, als dort noch steht: Das Skonto lässt sich nicht auf die Sätze aufteilen. Nichts wird geschrieben, kein Konto angelegt; die Zahlung ohne Skonto geht."""
    invoice = mixed_invoice(db, 'M-E', parts=[(19, '1078.00', None), (7, '4.00', None)])
    for paid in ('675.00', '582.00'):
        assign(db, invoice, _bank(db, acct, paid), skonto=False)
    assert _raw(db, "SELECT TaxRate, SUM(Amount), SUM(TaxAmount) FROM Bookings WHERE Status = ? AND DocumentNumber = 'M-E' AND EntryForm = 'transfer' GROUP BY TaxRate ORDER BY TaxRate",
                PAYMENT_MARK) == [(0.07, 41800, 2800), (0.19, 12528200, 2000300)]                                               # 7 %: 4,18 € von 4,28 € bezahlt, 0,28 € von 0,28 € Steuer
    closing = _bank(db, acct, '4.36')
    no_accounts(db)

    _assert_refused(db, invoice, closing, SKONTO_MIXED_SPLIT)
    assert db.payment_precheck(invoice, acct, None, on='2025-04-10', amount='4.36', skonto=True) == SKONTO_MIXED_SPLIT          # die Vorprüfung der Maske sagt dasselbe

    assert db.assign_invoice_payment(invoice, closing).ok and _state(db, invoice) == ('partial_payment', D('25.74'))           # ohne Skonto: eine gewöhnliche Teilzahlung


def test_the_same_sum_in_one_part_payment_leaves_the_tax_of_the_small_group_and_the_discount_goes(db, acct):
    """Gegenprobe zu Fall E: Dieselben 1257,00 € in EINER Teilzahlung – die Steuer der 7 % wird nur einmal gerundet und reicht für ihren Anteil am Skonto."""
    invoice = mixed_invoice(db, 'M-E', parts=[(19, '1078.00', None), (7, '4.00', None)])
    assign(db, invoice, _bank(db, acct, '1257.00'), skonto=False)
    closing = _bank(db, acct, '4.36')

    result = assign(db, invoice, closing)

    assert _payment(db, invoice)[-1] == (43600, 257400, '2025-04-10') and _state(db, invoice) == ('paid', 0) and result.payment_id


def test_an_earlier_payment_without_the_rows_of_the_app_refuses_before_the_split(db, acct):
    """Eine Zuordnung ohne die Sätze der App (Import, von Hand): Die Verteilung je Satz ließe sich nur raten – ``MIXED_ALLOCATION_UNREADABLE``."""
    invoice = mixed_invoice(db)
    first, second = _bank(db, acct, '119.00', date='2025-04-02'), _bank(db, acct, '50.05', date='2025-05-05')
    _exec(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 1190000, ?)', invoice, first, '2025-04-02')
    no_accounts(db)

    _assert_refused(db, invoice, second, MIXED_ALLOCATION_UNREADABLE)


def test_a_rest_that_is_no_whole_cent_amount_refuses(db, acct):
    invoice, bank = _world(db, acct, ITEMS, '169.05')
    _exec(db, 'UPDATE Bookings SET Amount = 16905000 / 10 + 50 WHERE ID = ?', bank)                                         # 169,055 €: Rest 3,445 €
    _assert_refused(db, invoice, bank, SKONTO_NOT_CENTS)


@pytest.mark.parametrize('damage', [
    "DELETE FROM Bookings WHERE DocumentNumber = ? AND TaxRate = 0.07",
    "UPDATE Bookings SET TaxRate = 0.16 WHERE DocumentNumber = ? AND TaxRate = 0.07",
    "UPDATE Bookings SET TaxRate = 0.19 WHERE DocumentNumber = ? AND TaxRate = 0.07",
], ids=['eine-Forderung-fehlt', 'Forderung-mit-fremdem-Satz', 'zwei-Forderungen-desselben-Satzes'])
def test_receivables_that_do_not_carry_the_rates_of_the_invoice_refuse_before_any_account_is_created(db, acct, damage):
    """Fall A der Prüfung: ``MIXED_RECEIVABLE_MISMATCH`` ist lesend zu prüfen – die Vorprüfung sagt es, BEVOR sie die Skonto-Konten (4736, 4731) anlegt, und die Vorprüfung der Maske ebenso."""
    invoice = mixed_invoice(db)
    _exec(db, damage, NUMBER)
    bank = _bank(db, acct, '169.05')
    no_accounts(db)

    _assert_refused(db, invoice, bank, MIXED_RECEIVABLE_MISMATCH)

    assert db.payment_precheck(invoice, acct, None, on='2025-04-10', amount='169.05', skonto=True) == MIXED_RECEIVABLE_MISMATCH
    assert all(_coa(db, number) is None for number in (*SKONTO_NUMBERS, 4184, 4185))


def test_a_mixed_invoice_without_receivables_has_nothing_to_mismatch(db, acct):
    invoice = mixed_invoice(db, receivable=False)
    assert db.payment_precheck(invoice, acct, None, on='2025-04-10', amount='169.05', skonto=True) is None


def test_a_refused_discount_leaves_a_plain_payment_possible(db, acct):
    invoice, bank = _world(db, acct, [PLAIN_19, entry('5.5', '200.00', account(db, REDUCED))], '323.40', receivable=False)
    _assert_refused(db, invoice, bank, SKONTO_GROUP_RATE.format(rate='5,5'))

    result = db.assign_invoice_payment(invoice, bank)

    assert result.ok and _payment(db, invoice) == [(3234000, None, '2025-04-10')]


# Eine Rechnung ohne Skonto-Bedingungen sagt NIE etwas über Steuersätze oder Skonto-Konten: ``SKONTO_NO_TERMS`` kommt vor jedem anderen Grund
def _sixteen_percent(db):
    return mixed_invoice(db, 'N-1', parts=[(16, '100.00', None)], skonto=None, receivable=False)


def _group_without_account(db):
    return mixed_invoice(db, 'N-2', parts=[PLAIN_19, entry('5.5', '200.00', account(db, REDUCED))], skonto=None, receivable=False)


def _discount_account_as_revenue(db):
    return mixed_invoice(db, 'N-3', parts=[PLAIN_19, entry(7, '50.00', _coa(db, 4731))], skonto=None)


def _sums_do_not_match(db):
    invoice = mixed_invoice(db, 'N-4', skonto=None)
    _exec(db, 'UPDATE Invoices SET SumNet = 9990000 WHERE ID = ?', invoice)
    return invoice


def _snapshots_contradict(db):
    invoice = mixed_invoice(db, 'N-5', skonto=None)
    _exec(db, "UPDATE InvoiceItems SET TaxExemptReason = 'Grund ohne Satz' WHERE InvoiceId = ? AND TaxRate = 7", invoice)
    return invoice


def _rate_flag_without_positions(db):
    invoice = mixed_invoice(db, 'N-6', parts=[PLAIN_19], skonto=None, receivable=False)
    _exec(db, 'DELETE FROM InvoiceItems WHERE InvoiceId = ?', invoice)
    _exec(db, 'UPDATE Invoices SET MixedTaxRates = 1 WHERE ID = ?', invoice)
    return invoice


DEFECTS = [(_sixteen_percent, SKONTO_RATE), (_group_without_account, SKONTO_GROUP_RATE.format(rate='5,5')), (_discount_account_as_revenue, SKONTO_REVENUE_DISCOUNT),
           (_sums_do_not_match, MIXED_GROUPS_MISMATCH), (_snapshots_contradict, TAX_ENTRY_CONTRADICTION), (_rate_flag_without_positions, MIXED_GROUPS_MISMATCH)]
DEFECT_IDS = ['16-Prozent', 'Gruppe-5,5-Prozent', 'Skonto-Konto-als-Erlöskonto', 'Summen-passen-nicht', 'Momentaufnahme-widerspricht', 'Kennzeichen-ohne-Positionen']


@pytest.mark.parametrize('world', [world for world, _sentence in DEFECTS], ids=DEFECT_IDS)
def test_an_invoice_without_discount_terms_names_only_that_whatever_else_is_wrong_with_it(db, acct, world):
    invoice = world(db)
    bank = _bank(db, acct, '100.00')
    before = installation.dump(db.db_name)

    refused = db.assign_invoice_payment(invoice, bank, None, None, True)

    assert (refused.ok, refused.message) == (False, SKONTO_NO_TERMS) and installation.dump(db.db_name) == before
    assert db.payment_precheck(invoice, acct, None, on='2025-04-10', amount='100.00', skonto=True) == SKONTO_NO_TERMS
    assert db.skonto_offer(invoice).error == SKONTO_NO_TERMS                                                                    # die Seite sagt dazu nichts (``_skonto_info_html``)


@pytest.mark.parametrize('world, sentence', DEFECTS, ids=DEFECT_IDS)
def test_the_same_defects_with_discount_terms_keep_their_own_sentences(db, acct, world, sentence):
    """Nichts ändert sich für eine Rechnung MIT Bedingungen: Die Gründe stehen in ihrer Reihenfolge."""
    invoice = world(db)
    _exec(db, 'UPDATE Invoices SET SkontoDays = 14, SkontoPercent = 2.0 WHERE ID = ?', invoice)
    bank = _bank(db, acct, '100.00')
    before = installation.dump(db.db_name)

    refused = db.assign_invoice_payment(invoice, bank, None, None, True)

    assert (refused.ok, refused.message) == (False, sentence) and installation.dump(db.db_name) == before
    assert db.payment_precheck(invoice, acct, None, on='2025-04-10', amount='100.00', skonto=True) == sentence


# ═══ Das Netz in der Transaktion ═════════════════════════════════════════════

@pytest.mark.parametrize('name', ['terms-removed', 'discount-shrinks', 'group-changes', 'another-payment', 'snapshot-contradicts'])
def test_a_change_between_the_precheck_and_the_transaction_refuses_with_the_changed_sentence(db, acct, monkeypatch, name):
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')
    other = _bank(db, acct, '10.00', date='2025-04-11')
    changes = {
        'terms-removed': lambda: _exec(db, 'UPDATE Invoices SET SkontoDays = NULL, SkontoPercent = NULL WHERE ID = ?', invoice),
        'discount-shrinks': lambda: _exec(db, 'UPDATE Invoices SET SkontoPercent = 1.0 WHERE ID = ?', invoice),                  # Skonto jetzt 1,73 €, der Rest 3,45 €
        'group-changes': lambda: _exec(db, 'UPDATE InvoiceItems SET TaxRate = 5.5 WHERE InvoiceId = ? AND TaxRate = 7', invoice),
        'another-payment': lambda: _exec(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 100000, ?)', invoice, other, '2025-04-11'),
        'snapshot-contradicts': lambda: _exec(db, "UPDATE InvoiceItems SET TaxExemptReason = 'x' WHERE InvoiceId = ? AND TaxRate = 7", invoice),
    }
    seen = {}

    def change():
        changes[name]()
        seen['tables'] = _tables(db)

    _change_after_the_precheck(monkeypatch, db, change)

    result = db.assign_invoice_payment(invoice, bank, None, None, True)

    assert (result.ok, result.message) == (False, PAYMENT_CHANGED_MESSAGE)
    assert _tables(db) == seen['tables'] and _kid_ids(db, bank) == []
    assert all(row[1] is None for row in _payment(db, invoice))


@pytest.mark.parametrize('name', ['snapshot-contradicts-the-head', 'snapshot-with-a-revenue-account-added', 'revenue-account-changes', 'snapshot-removed'])
def test_a_change_of_the_snapshot_of_an_invoice_with_one_rate_after_the_precheck_refuses_with_the_changed_sentence(db, acct, monkeypatch, name):
    """Fall D der Prüfung: Die Vorprüfung merkt sich, ob die Rechnung mit EINEM Satz die Momentaufnahme eines Satzes aus den Stammdaten trägt und welche; die Transaktion liest sie frisch und lehnt bei jeder
    Abweichung ab. Sonst bucht sie auf dem Erlöskonto, das die Vorprüfung aufgelöst hat, obwohl die Rechnung inzwischen ein anderes (oder keines) trägt – oder trotz eines Widerspruchs."""
    own, other = account(db, FREE), account(db, REDUCED)
    invoice = mixed_invoice(db, 'S-7', parts=[entry(19, '100.00', own)] if name in ('revenue-account-changes', 'snapshot-removed') else [PLAIN_19])
    bank = _bank(db, acct, '116.62')
    changes = {
        'snapshot-contradicts-the-head': lambda: db.insert_invoice_item({'invoice_id': invoice, 'position': 2, 'description': 'x', 'quantity': 1, 'unit': 'C62', 'price_per_unit': 100.0,
                                                                         'total_net': 100.0, 'tax_rate': 0.07, 'tax_rate_bp': 700, 'revenue_coa_id': own}),
        'snapshot-with-a-revenue-account-added': lambda: _exec(db, 'UPDATE InvoiceItems SET TaxRateBp = 1900, RevenueCoaId = ? WHERE InvoiceId = ?', own, invoice),
        'revenue-account-changes': lambda: _exec(db, 'UPDATE InvoiceItems SET RevenueCoaId = ? WHERE InvoiceId = ?', other, invoice),
        'snapshot-removed': lambda: _exec(db, 'UPDATE InvoiceItems SET TaxRateBp = NULL, RevenueCoaId = NULL WHERE InvoiceId = ?', invoice),
    }
    seen = {}

    def change():
        changes[name]()
        seen['tables'] = _tables(db)

    _change_after_the_precheck(monkeypatch, db, change)

    result = db.assign_invoice_payment(invoice, bank, None, None, True)

    assert (result.ok, result.message) == (False, PAYMENT_CHANGED_MESSAGE)
    assert _tables(db) == seen['tables'] and _kid_ids(db, bank) == []
    assert all(row[1] is None for row in _payment(db, invoice))


def test_without_a_change_the_precheck_and_the_transaction_agree_on_the_snapshot(db, acct, monkeypatch):
    """Gegenprobe: Ändert sich nichts, bucht die Zuordnung – auch mit einer Momentaufnahme samt eigenem Erlöskonto."""
    invoice = mixed_invoice(db, 'S-8', parts=[entry(19, '100.00', account(db, FREE))])
    bank = _bank(db, acct, '116.62')
    _change_after_the_precheck(monkeypatch, db, lambda: None)

    assign(db, invoice, bank)

    assert shape(_kids(db, bank))[1] == ('transfer', 4405, FREE, D('119'), D('19'), 0.19) and _state(db, invoice) == ('paid', 0)


def test_the_context_of_an_invoice_with_one_rate_carries_the_snapshot_the_precheck_saw(db, acct):
    own = account(db, FREE)
    plain, entered = mixed_invoice(db, 'S-9', parts=[PLAIN_19]), mixed_invoice(db, 'S-10', parts=[entry(19, '100.00', own)])
    for invoice, expected in ((plain, None), (entered, (D(19), Snapshot(False, None, own)))):
        error, context = db._payment_prerequisites(invoice, acct, None, on='2025-04-10', skonto=(1166200, 1166200))
        assert error is None and context['skonto']['entry'] == expected and context['skonto']['rate_key'] == 19
    error, context = db._payment_prerequisites(plain, acct, None, on='2025-04-10')
    assert error is None and 'skonto' not in context                                                                            # ohne Skonto bindet die Vorprüfung nichts


def test_a_context_without_the_key_books_as_always_even_with_discount_terms(db, acct):
    """Ein Kontext ohne ``skonto`` (Neuaufbau, Umzug, von Hand gebaut) bucht je Satz wie immer – das Geld ist dann eine Teilzahlung."""
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')
    error, context = db._payment_prerequisites(invoice, acct, None, on='2025-04-10')
    assert error is None and 'skonto' not in context
    with db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        assert db._assign_in_tx(tx, invoice, bank, None, context).ok

    assert _payment(db, invoice) == [(1690500, None, '2025-04-10')] and _state(db, invoice) == ('partial_payment', D('3.45'))
    assert sum(row[3] for row in _kids(db, bank)[1:]) == D('169.05')


def test_the_context_of_a_mixed_invoice_carries_the_accounts_of_its_rates(db, acct):
    invoice = mixed_invoice(db, parts=[PLAIN_19, PLAIN_7, free('100.00', account(db, FREE))])
    error, context = db._payment_prerequisites(invoice, acct, None, on='2025-04-10', skonto=(2670500, 2670500))                # 267,05 € von 272,50 €: der Rest ist das Skonto
    assert error is None
    assert context['skonto'] == {'coas': {19: _coa(db, 4736), 7: _coa(db, 4731), 0: _coa(db, 4730)}, 'rate_key': (19, 7, 0)}


# ═══ Entfernen stellt jede Spalte wieder her ═════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_removing_the_assignment_restores_every_column(db, acct, receivable):
    invoice = mixed_invoice(db, receivable=receivable)
    bank = _bank(db, acct, '169.05')
    before = _tables(db)
    result = assign(db, invoice, bank)
    assert _tables(db) != before

    db.unassign_invoice_payment(result.payment_id)

    assert _tables(db) == before and _state(db, invoice) == ('sent', D('172.5')) and _kids(db, bank) == []


def test_deleting_the_bank_movement_takes_the_whole_assignment_with_it(db, acct):
    invoice = mixed_invoice(db)
    before = _tables(db)
    bank = _bank(db, acct, '169.05')
    assign(db, invoice, bank)

    db.delete_transaction(bank)

    assert _tables(db) == before and _state(db, invoice) == ('sent', D('172.5'))


@pytest.mark.parametrize('which', range(5), ids=['Bank-an-Debitoren', 'Umbuchung-19', 'Umbuchung-7', 'Skonto-19', 'Skonto-7'])
def test_removing_any_row_of_the_assignment_removes_the_whole_assignment(db, acct, which):
    """Das ✕ im Bank-Editor an jeder markierten Zeile geht über ``remove_assignment_for_child``: die ganze Zuordnung, nie ein Satz allein – auch ein Skonto-Satz je Gruppe."""
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')
    before = _tables(db)
    assign(db, invoice, bank)

    db.delete_transaction(_kid_ids(db, bank)[which])

    assert _tables(db) == before and _payment(db, invoice) == []


def test_a_part_payment_cannot_be_removed_while_the_discount_stands(db, acct):
    invoice = mixed_invoice(db)
    first, second = _bank(db, acct, '100.00', date='2025-04-02'), _bank(db, acct, '69.05', date='2025-05-05')
    assign(db, invoice, first, skonto=False)
    assign(db, invoice, second)
    first_payment = _raw(db, 'SELECT ID FROM InvoicePayments WHERE BookingID = ?', first)[0][0]
    before = _tables(db)

    with pytest.raises(ValueError, match='M-100'):
        db.unassign_invoice_payment(first_payment)
    assert _tables(db) == before                                                                                               # nichts verändert


# ═══ Der Bank-Editor, das Lernen und die Auswahl des Erlöskontos ═════════════

def test_opening_and_saving_the_bank_editor_without_a_change_changes_nothing(db, acct):
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')
    assign(db, invoice, bank)
    before_rows, before_tables = snapshot(db), _tables(db)

    post, _options = open_form(db, bank)
    submit(db, post)

    assert differences(before_rows, snapshot(db), db.get_coa_id_to_number_map()) == []
    assert _tables(db) == before_tables and _payment(db, invoice) == [(1690500, 34500, '2025-04-10')]


def test_no_discount_account_is_learned_as_a_revenue_account_of_a_rate(db, acct):
    invoice = mixed_invoice(db)
    assign(db, invoice, _bank(db, acct, '169.05'))

    assert db._learned_revenue_coa(db._revenue_rate_key(0.19)) == _coa(db, 4400) and db._learned_revenue_coa(db._revenue_rate_key(0.07)) == _coa(db, 4300)
    assert db.resolve_revenue_coa(0.19, create=False) == _coa(db, 4400) and db.resolve_revenue_coa(0.07, create=False) == _coa(db, 4300)


def test_the_suggestion_of_a_revenue_account_never_names_a_discount_account_after_a_mixed_discount(db, acct):
    invoice = mixed_invoice(db)
    assign(db, invoice, _bank(db, acct, '169.05'))
    assert db.suggest_revenue_coa(0.19) == _coa(db, 4400) and db.suggest_revenue_coa(0.07) == _coa(db, 4300)


# ═══ payment_precheck und die Route ══════════════════════════════════════════

def test_the_precheck_gives_the_same_sentences_before_a_movement_exists(db, acct):
    ok = mixed_invoice(db, 'P-1')
    group = mixed_invoice(db, 'P-2', parts=[PLAIN_19, entry('5.5', '200.00', account(db, REDUCED))], receivable=False)
    free_terms = mixed_invoice(db, 'P-3', skonto=None)
    no_accounts(db)
    before = installation.dump(db.db_name)

    assert db.payment_precheck(group, acct, None, on='2025-04-10', amount='323.40', skonto=True) == SKONTO_GROUP_RATE.format(rate='5,5')
    assert db.payment_precheck(free_terms, acct, None, on='2025-04-10', amount='169.05', skonto=True) == SKONTO_NO_TERMS
    assert db.payment_precheck(ok, acct, None, on='2025-04-10', amount='169.04', skonto=True) == SKONTO_TOO_LARGE.format(rest='3,46 €', skonto='3,45 €')
    assert installation.dump(db.db_name) == before                                                                              # eine Ablehnung legt kein Konto an
    assert db.payment_precheck(ok, acct, None, on='2025-04-10', amount='169.05', skonto=True) is None


def test_link_booking_to_invoice_capped_passes_the_discount_through(db, acct):
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')

    assert handlers.link_booking_to_invoice_capped(db, invoice, bank, None, None, True) == (True, None)

    assert _payment(db, invoice) == [(1690500, 34500, '2025-04-10')] and _state(db, invoice) == ('paid', 0)
    assert _raw(db, 'SELECT DocumentNumber FROM Bookings WHERE ID = ?', bank) == [(NUMBER,)]                                    # der Anzeige-Backfill: Kunde und Beleg-Nr.


def test_link_booking_to_invoice_capped_returns_the_sentence_of_a_refusal(db, acct):
    invoice = mixed_invoice(db, parts=[PLAIN_19, entry('5.5', '200.00', account(db, REDUCED))], receivable=False)
    bank = _bank(db, acct, '323.40')

    assert handlers.link_booking_to_invoice_capped(db, invoice, bank, None, None, True) == (False, SKONTO_GROUP_RATE.format(rate='5,5'))
    assert handlers.link_booking_to_invoice_capped(db, invoice, bank) == (True, None)                                           # ohne Skonto geht es wie immer


@pytest.fixture
def routes(db, monkeypatch):
    """Die Handler arbeiten auf der Testdatenbank (``handlers.Database()``) – wie die Routen im Betrieb."""
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: db)
    return db


def link(payload):
    """``POST /invoice/link-payment`` ohne HTTP: Statuscode und Text des Handlers."""
    return handlers.handle_link_invoice_payment(json.dumps(payload).encode())


def test_the_route_books_the_discount_per_rate(routes, acct):
    invoice = mixed_invoice(routes)
    bank = _bank(routes, acct, '169.05')

    code, text = link({'invoice_id': invoice, 'transaction_id': bank, 'skonto': True})

    assert (code, text) == (200, f'/invoice/view?id={invoice}')
    assert shape(_kids(routes, bank)) == M_ROWS and _state(routes, invoice) == ('paid', 0)


def test_the_route_comes_back_with_the_fixed_sentence_of_a_refusal_and_writes_nothing(routes, acct):
    invoice = mixed_invoice(routes, parts=[PLAIN_19, entry('5.5', '200.00', account(routes, REDUCED))], receivable=False)
    bank = _bank(routes, acct, '323.40')
    before = installation.dump(routes.db_name)

    assert link({'invoice_id': invoice, 'transaction_id': bank, 'skonto': True}) == (400, SKONTO_GROUP_RATE.format(rate='5,5'))
    assert installation.dump(routes.db_name) == before


def test_the_mask_books_a_new_movement_with_the_discount_of_a_mixed_invoice(routes, acct):
    invoice = mixed_invoice(routes)
    form = {'transaction_id': ['0'], 'date': ['2025-04-10'], 'amount': ['169.05'], 'account': [str(acct)], 'recipient': ['Käufer AG'], 'text': ['Danke'],
            'invoice_id': [str(invoice)], 'currency': ['EUR'], 'skonto': ['1']}

    status, location = handlers.handle_add_transaction(routes, form)

    assert (status, location) == (303, f'/invoice/edit?id={invoice}') and _state(routes, invoice) == ('paid', 0)
    assert _payment(routes, invoice) == [(1690500, 34500, '2025-04-10')]


def test_the_mask_refuses_a_new_movement_before_anything_is_written(routes, acct):
    invoice = mixed_invoice(routes, parts=[PLAIN_19, entry('5.5', '200.00', account(routes, REDUCED))], receivable=False)
    before = installation.dump(routes.db_name)
    form = {'transaction_id': ['0'], 'date': ['2025-04-10'], 'amount': ['323.40'], 'account': [str(acct)], 'recipient': ['Käufer AG'], 'text': ['Danke'],
            'invoice_id': [str(invoice)], 'currency': ['EUR'], 'skonto': ['1']}

    assert handlers.handle_add_transaction(routes, form) == (400, SKONTO_GROUP_RATE.format(rate='5,5'))
    assert installation.dump(routes.db_name) == before


def test_the_mask_refuses_a_new_movement_for_broken_receivables_before_anything_is_written(routes, acct):
    """Die Vorprüfung sieht die Forderungen: Es entsteht keine Bankbewegung, die die Zuordnung dann ablehnte – und kein Skonto-Konto."""
    invoice = mixed_invoice(routes)
    _exec(routes, "DELETE FROM Bookings WHERE DocumentNumber = ? AND TaxRate = 0.07", NUMBER)
    no_accounts(routes)
    before = installation.dump(routes.db_name)
    form = {'transaction_id': ['0'], 'date': ['2025-04-10'], 'amount': ['169.05'], 'account': [str(acct)], 'recipient': ['Käufer AG'], 'text': ['Danke'],
            'invoice_id': [str(invoice)], 'currency': ['EUR'], 'skonto': ['1']}

    assert handlers.handle_add_transaction(routes, form) == (400, MIXED_RECEIVABLE_MISMATCH)

    assert installation.dump(routes.db_name) == before and _raw(routes, "SELECT COUNT(*) FROM Bookings WHERE BookingType = 'bank'") == [(0,)]
    assert all(_coa(routes, number) is None for number in (*SKONTO_NUMBERS, 4184, 4185))


# ═══ Die Seiten und die Auswertungen nehmen den Skonto-Satz je Satz mit ═════

def test_the_pages_show_a_mixed_invoice_paid_with_a_discount(db, acct):
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')
    assign(db, invoice, bank)

    page = PageInvoice(db, {}, invoice)

    assert '169,05 €<div class="hint">+ Skonto 3,45 €</div>' in page and 'Gezahlt gesamt' in page and 'Noch offen' in page
    mask = PageTransactions(db, edit_transaction_id=bank)
    assert 'Skonto zu Re. M-100' in mask                                                                                       # die Skonto-Sätze unter der Bewegung stehen im Editor
    assert 'Skonto zu Re. M-100' in PageTransactions(db, date_from='2025-01-01', date_to='2025-12-31')


def test_each_discount_row_is_marked_and_carries_the_number_of_the_invoice(db, acct):
    invoice = mixed_invoice(db)
    bank = _bank(db, acct, '169.05')
    assign(db, invoice, bank)

    rows = _raw(db, "SELECT c.AccountNumber, k.DocumentNumber, k.Status, k.Text FROM Bookings k JOIN ChartOfAccounts c ON c.ID = k.COA_ID WHERE k.ParentBooking_ID = ? AND k.EntryForm = 'purpose_first' "
                    'ORDER BY k.ID', bank)
    assert rows == [(4736, NUMBER, PAYMENT_MARK, 'Skonto zu Re. M-100'), (4731, NUMBER, PAYMENT_MARK, 'Skonto zu Re. M-100')]
    assert db.find_payment_inconsistencies() == []

