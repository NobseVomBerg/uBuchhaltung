# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Skonto bei mehreren Steuersätzen beim Stornieren, Erstatten, Umziehen und Neuaufbauen (Stufe 2).

Eine Rechnung mit Steuersatz je Position, die mit Skonto bezahlt ist (``tests/test_skonto_mixed_booking.py``: **M**, 19 % × 100,00 und 7 % × 50,00, 169,05 € gezahlt, Skonto 3,45 €), trägt unter der
Bankbewegung der Zahlung fünf markierte Sätze (mit Forderung): „Bank an Debitoren“ und je Satz „Wartekonto an Erlöskonto“ über Geld plus Skonto (172,50 / 119,00 / 53,50) und je Satz „Skonto-Konto an
Bank“ (−2,38 und −1,07). **Das Stornieren nimmt das Skonto je Satz zurück** (``_reverse_skonto_in``): je Skonto-Satz drei weitere markierte Sätze unter derselben Bewegung („Bank an Debitoren“ −2,38,
„Wartekonto an Erlöskonto“ −2,38 mit −0,38 Steuer, „Skonto-Konto an Bank“ +2,38 mit +0,38 – danach dasselbe für 7 %; ohne Forderung je Satz zwei), in der Reihenfolge der Skonto-Sätze der Zahlung,
mit der Nummer der stornierten Rechnung, Datum und Steuerdatum das spätere von Stornodatum und Bankdatum; danach ist ``SkontoAmount`` NULL (einmal je Bewegung). Die stornierte Rechnung sieht
buchhalterisch aus wie eine Teilzahlung über das Geld (169,05 €): Das ist das Guthaben. Geprüft wird:

* die Sätze der Rücknahme Spalte für Spalte (mit und ohne Forderung, drei Gruppen mit einer steuerfreien), die Konten kommen aus den Sätzen je Gruppe;
* der Stand nach dem Stornieren und nach der Erstattung (ganz und in Teilen: je Satz Betrag und Steuer) – EÜR, Summen- und Saldenliste (die Proben in jedem Monat), Liquidität, Split-Summe und
  Prüfliste – auf den Minor-Wert; der Topf je Satz ist das Geld, vor wie nach der Rücknahme;
* ``_refund_revenue_coas_by_rate``: das Erlöskonto je Satz bleibt eindeutig (die Skonto-Sätze der Zahlung und der Rücknahme zählen nicht);
* eine geänderte Skonto-Buchung (Betrag, Konto, Satz, doppelt, gelöscht, die Umbuchung eines Satzes gelöscht oder verdoppelt …): 409 ``SKONTO_STORNO_EDITED`` ohne Schreiben;
* das Entfernen der Zahlung (jede Zeile, jeder Weg), ``SKONTO_REMOVAL_REFUSED`` und der Schutz je Satz (``MIXED_REMOVAL_REFUSED``);
* der Umzug beim Versenden einer Ersatzrechnung (``MOVE_MIXED_HINT``: nichts zieht um) und der Neuaufbau (Grund ``mixed``, nur der offene Betrag).

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da (``cancel_invoice(today=…)``).
"""
from decimal import Decimal

import pytest

from db.invoice_payments import (MIXED_REMOVAL_REFUSED, MOVE_ENTRY_HINT, MOVE_MIXED_HINT, PAYMENT_MARK, SKONTO_REMOVAL_REFUSED, SKONTO_STORNO_EDITED, PaymentRemovalRefused)
from db.invoice_repair import REBUILD_SKIP_REASONS
from tests import installation
from tests import susa_helpers as helpers
from tests.susa_helpers import account_id
from tests.test_skonto_mixed_booking import FREE, NUMBER, PLAIN_19, PLAIN_7, account, entry, free, mixed_invoice
from tests.test_skonto_reports import RANGE, check_every_probe, datev_rows, ending, findings, minor, pay, raw
from tests.test_skonto_reports import db  # noqa: F401  (Fixture: Testdatenbank mit zwei Bankkonten)
from tests.test_skonto_storno import TODAY, USER, _reissue_and_send, book_tables, cancel, kids, notices, payments, refund, replay_lines, shape, state

D = Decimal
CHAIN = (1800, 10000, 4405, 4345, 4400, 4300, 4736, 4731, 3806, 3801)       # Bank, Debitoren, Wartekonten, Erlöse, Skonto-Konten, Umsatzsteuer 19 und 7 %


# ── Hilfen ───────────────────────────────────────────────────────────────────

def balances(db, month, numbers=CHAIN):
    report = db.get_susa(2025, month)
    return [ending(report, number) for number in numbers]


def paid_with_discount(db, receivable=True, parts=None, paid='169.05', day='2025-05-05'):
    """M (``M-100``: 19 % × 100,00 und 7 % × 50,00, Skonto 2 % = 3,45 €) mit dem Skonto als Rest der Zahlung vom 05.05.2025 – ``(Rechnungs-ID, Bankbewegung)``."""
    invoice_id = mixed_invoice(db, parts=parts, receivable=receivable)
    return invoice_id, pay(db, invoice_id, paid, day, skonto=True)


# ═════════════════════════════════════════════════════════════════════════════
# Die Sätze der Rücknahme
# ═════════════════════════════════════════════════════════════════════════════

def test_cancelling_takes_the_discount_back_per_rate_under_the_movement_of_the_payment(db):
    customer = db.insert_contact(contact_type='customer', entity_type='company', display_name='Käufer AG', company_name='Käufer AG')
    invoice_id = mixed_invoice(db)
    raw(db, 'UPDATE Invoices SET CustomerId = ? WHERE ID = ?', customer, invoice_id)
    bank = pay(db, invoice_id, '169.05', '2025-05-05', skonto=True)

    storno = cancel(db, invoice_id, '2025-06-03')

    rows = kids(db, bank)
    assert shape(rows) == [
        ('liquid_first', 1800, 10000, D('172.5'), None),                       # die Zahlung: Bank an Debitoren, Geld plus Skonto
        ('transfer', 4405, 4400, D('119'), D('19')), ('transfer', 4345, 4300, D('53.5'), D('3.5')),
        ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38')), ('purpose_first', 4731, 1800, D('-1.07'), D('-0.07')),          # je Satz der Skonto-Satz
        ('liquid_first', 1800, 10000, D('-2.38'), None),                      # die Rücknahme: je Satz dieselben drei Konten mit umgekehrtem Vorzeichen – erst 19 %, dann 7 %
        ('transfer', 4405, 4400, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38')),
        ('liquid_first', 1800, 10000, D('-1.07'), None),
        ('transfer', 4345, 4300, D('-1.07'), D('-0.07')), ('purpose_first', 4731, 1800, D('1.07'), D('0.07'))]
    assert [row.id for row in rows] == sorted(row.id for row in rows)
    payment_rows, taken_back = rows[:5], rows[5:]
    assert [row.date for row in payment_rows] == ['2025-05-05'] * 5 and [row.date_tax for row in payment_rows] == [None] * 5
    for row in taken_back:                                                    # Datum und Steuerdatum: das Stornodatum (nach dem Bankdatum), Beleg-Nr.: die der stornierten Rechnung
        assert (row.date, row.date_tax, row.text, row.status, row.number) == ('2025-06-03', '2025-06-03', 'Skonto-Rücknahme zu Re. M-100', PAYMENT_MARK, 'M-100')
    assert [row.rate for row in taken_back] == [None, 0.19, 0.19, None, 0.07, 0.07]
    for row in rows:                                                          # Kunde der Rechnung, Empfänger und Währung der Bankbewegung
        assert (row.contact, row.currency, row.recipient) == (customer, 'EUR', 'Käufer AG')
    assert raw(db, 'SELECT BookingType, AutoMirror, SourceGroup, Account_ID, Category_ID FROM Bookings WHERE ParentBooking_ID = ? AND ID > ?', bank, rows[4].id) == [
        ('entry', 0, None, None, None)] * 6
    assert payments(db, invoice_id) == [(1690500, None)]                      # Amount bleibt das Geld, SkontoAmount ist NULL
    assert db.get_invoice_skonto(invoice_id) == {}
    assert state(db, invoice_id) == ('cancelled', D('3.45'))                  # wie eine Teilzahlung: 172,50 − 169,05
    assert db.get_storno_credits() == {storno.storno_id: D('169.05')}         # das Guthaben ist das Geld, das geflossen ist
    assert findings(db) == []


def test_without_a_receivable_the_discount_comes_back_in_two_rows_per_rate(db):
    invoice_id, bank = paid_with_discount(db, receivable=False)

    storno = cancel(db, invoice_id)

    assert storno.counter_booked is False                                     # ohne Forderung der App keine Gegenbuchung
    assert shape(kids(db, bank)) == [
        ('purpose_first', 4400, 1800, D('119'), D('19')), ('purpose_first', 4300, 1800, D('53.5'), D('3.5')),
        ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38')), ('purpose_first', 4731, 1800, D('-1.07'), D('-0.07')),
        ('purpose_first', 4400, 1800, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38')),          # die Rücknahme: je Satz Erlöskonto an Bank mit dem negativen Skonto …
        ('purpose_first', 4300, 1800, D('-1.07'), D('-0.07')), ('purpose_first', 4731, 1800, D('1.07'), D('0.07'))]        # … und der Skonto-Satz mit dem positiven
    assert [row.text for row in kids(db, bank)[4:]] == ['Skonto-Rücknahme zu Re. M-100'] * 4
    assert state(db, invoice_id) == ('cancelled', D('3.45')) and db.get_storno_credits() == {storno.storno_id: D('169.05')}
    assert balances(db, 6) == [minor('169.05'), 0, 0, 0, minor('-98.00'), minor('-49.00'), 0, 0, minor('-18.62'), minor('-3.43')]
    check_every_probe(db, (5, 6, 7))


def test_three_groups_one_of_them_tax_free_take_the_discount_back_on_their_own_accounts(db):
    """Die steuerfreie Gruppe (Erlöskonto 4120 aus den Stammdaten) nimmt 2,00 € auf 4730 zurück – ohne Satz und ohne Steuer."""
    invoice_id, bank = paid_with_discount(db, parts=[PLAIN_19, PLAIN_7, free('100.00', account(db, FREE))], paid='267.05')

    storno = cancel(db, invoice_id)

    assert shape(kids(db, bank))[7:] == [
        ('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, 4400, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38')),
        ('liquid_first', 1800, 10000, D('-1.07'), None), ('transfer', 4345, 4300, D('-1.07'), D('-0.07')), ('purpose_first', 4731, 1800, D('1.07'), D('0.07')),
        ('liquid_first', 1800, 10000, D('-2'), None), ('transfer', 4340, FREE, D('-2'), None), ('purpose_first', 4730, 1800, D('2'), None)]
    assert state(db, invoice_id) == ('cancelled', D('5.45')) and db.get_storno_credits() == {storno.storno_id: D('267.05')}
    assert helpers.euer_values(db, RANGE) == {4400: minor('98.00'), 4300: minor('49.00'), FREE: minor('98.00'), 3806: minor('22.05')}
    check_every_probe(db, (5, 6, 7))
    refund(db, storno.storno_id, '267.05', day='2025-07-01')
    assert helpers.euer_values(db, RANGE) == {} and balances(db, 7, (*CHAIN, 4340, 4730, FREE)) == [0] * 13
    check_every_probe(db, (7, 8))


def test_the_pool_per_rate_is_the_money_before_and_after_the_cancellation(db):
    """Der Skonto-Satz zählt in seinem Satz: Betrag 119,00 − 2,38 und 53,50 − 1,07, Steuer 19,00 − 0,38 und 3,50 − 0,07 – das Geld je Satz. Nach der Rücknahme (Skonto-Satz der Zahlung, Skonto-Satz der
    Rücknahme und Umbuchung −2,38 / −1,07) steht dasselbe da."""
    invoice_id, _bank = paid_with_discount(db)
    expected = {D('19'): (1166200, 186200), D('7'): (524300, 34300)}

    def booked():
        connection = db._get_connection()
        try:
            return db._mixed_booked(connection, invoice_id, NUMBER, {D('19'), D('7')})
        finally:
            connection.close()
    assert booked() == expected
    cancel(db, invoice_id)
    assert booked() == expected


# ═════════════════════════════════════════════════════════════════════════════
# Der Stand nach dem Stornieren und nach der Erstattung
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_after_the_cancellation_the_books_show_a_part_payment_of_the_money(db, receivable):
    invoice_id, _bank = paid_with_discount(db, receivable)

    cancel(db, invoice_id, '2025-06-03')

    # die Konten im Juni (Soll positiv): Bank 169,05, Debitoren 169,05 im Haben (= das Guthaben), die Wartekonten im Soll (116,62 und 52,43), Erlöse 98,00 und 49,00, Skonto 0, Steuer 18,62 und 3,43
    assert balances(db, 6) == [minor('169.05'), minor('-169.05') if receivable else 0, minor('116.62') if receivable else 0, minor('52.43') if receivable else 0,
                               minor('-98.00'), minor('-49.00'), 0, 0, minor('-18.62'), minor('-3.43')]
    assert helpers.euer_values(db, RANGE) == {4400: minor('98.00'), 4300: minor('49.00'), 3806: minor('22.05')}              # das Skonto ist nach dem Jahr ganz verschwunden
    assert helpers.euer_values(db, ('2025-05-01', '2025-05-31')) == {4400: minor('100.00'), 4300: minor('50.00'), 4736: minor('-2.00'), 4731: minor('-1.00'), 3806: minor('22.05')}
    assert helpers.euer_values(db, ('2025-06-01', '2025-06-30')) == {4400: minor('-2.00'), 4300: minor('-1.00'), 4736: minor('2.00'), 4731: minor('1.00')}       # im Juni heben sich Erlös und Skonto auf
    assert [row for row in db.get_euer_data('2025-06-01', '2025-06-30') if row[0] == 3806 and round(row[2], 2)] == []
    check_every_probe(db, (3, 4, 5, 6, 7, 8))


def test_the_named_costs_of_the_reversal_a_notice_between_payment_and_cancellation_and_bank_rows_in_the_batch(db):
    """Die Sätze der Rücknahme hängen an einer Bewegung mit anderem Datum: Die Summen- und Saldenliste nennt es in den Monaten dazwischen (``child_other_period``, ein Hinweis, kein Fehler –
    die Proben gehen auf), und der Stapel des Stornomonats trägt je Satz zwei Bankzeilen ±."""
    invoice_id, _bank = paid_with_discount(db)
    cancel(db, invoice_id, '2025-06-03')

    assert [notices(db.get_susa(2025, month)) for month in (4, 5, 6, 7)] == [[], [('child_other_period', 6)], [], []]
    rows = datev_rows(db, NUMBER)
    assert [row for row in rows if row[3] == '1800' and row[2] == '4736'] == [('2,38', 'S', '4736', '1800', '101', '19,00', ''), ('2,38', 'H', '4736', '1800', '101', '19,00', '')]
    assert [row for row in rows if row[2] == '4731' and row[3] == '1800'] == [('1,07', 'S', '4731', '1800', '102', '7,00', ''), ('1,07', 'H', '4731', '1800', '102', '7,00', '')]


def test_the_refund_of_the_money_takes_every_account_back_to_zero(db):
    """Probe 3: nach dem Stornieren 10000 169,05 im Haben, die Wartekonten 116,62 und 52,43, 4400 98,00, 4300 49,00, 4736/4731 0; die Erstattung über 169,05 € nimmt je Satz Betrag und Steuer
    des Geldes zurück (116,62 / 18,62 und 52,43 / 3,43) und lässt alle Konten auf 0 und die EÜR leer."""
    invoice_id, _bank = paid_with_discount(db)
    storno = cancel(db, invoice_id, '2025-06-03')
    assert db.get_storno_credits() == {storno.storno_id: D('169.05')}

    bank = refund(db, storno.storno_id, '169.05', day='2025-07-01')

    assert shape(kids(db, bank)) == [('liquid_first', 1800, 10000, D('-169.05'), None), ('transfer', 4405, 4400, D('-116.62'), D('-18.62')),
                                     ('transfer', 4345, 4300, D('-52.43'), D('-3.43'))]
    assert balances(db, 7) == [0] * 10 and balances(db, 8) == [0] * 10
    assert helpers.euer_values(db, RANGE) == {}
    assert db.get_storno_credits() == {storno.storno_id: 0} and state(db, invoice_id) == ('cancelled', D('3.45'))
    assert raw(db, 'SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE Status = ?', PAYMENT_MARK) == [(0,)]
    check_every_probe(db, (5, 6, 7, 8, 12))


def test_the_refund_without_a_receivable_takes_every_account_back_to_zero_too(db):
    invoice_id, _bank = paid_with_discount(db, receivable=False)
    storno = cancel(db, invoice_id)

    bank = refund(db, storno.storno_id, '169.05')

    assert shape(kids(db, bank)) == [('purpose_first', 4400, 1800, D('-116.62'), D('-18.62')), ('purpose_first', 4300, 1800, D('-52.43'), D('-3.43'))]
    assert balances(db, 7) == [0] * 10 and helpers.euer_values(db, RANGE) == {}
    check_every_probe(db, (6, 7, 8))


def test_two_partial_refunds_split_amount_and_tax_per_rate_and_the_second_one_closes(db):
    invoice_id, _bank = paid_with_discount(db)
    storno = cancel(db, invoice_id)

    first = refund(db, storno.storno_id, '100.00', day='2025-07-01')

    assert shape(kids(db, first)) == [('liquid_first', 1800, 10000, D('-100'), None), ('transfer', 4405, 4400, D('-68.99'), D('-11.02')),
                                      ('transfer', 4345, 4300, D('-31.01'), D('-2.03'))]
    assert db.get_storno_credits() == {storno.storno_id: D('69.05')}
    assert helpers.euer_values(db, RANGE) == {4400: minor('98.00') - minor('57.97'), 4300: minor('49.00') - minor('28.98'), 3806: minor('22.05') - minor('13.05')}
    check_every_probe(db, (6, 7, 8))
    second = refund(db, storno.storno_id, '69.05', day='2025-07-02')

    assert shape(kids(db, second)) == [('liquid_first', 1800, 10000, D('-69.05'), None), ('transfer', 4405, 4400, D('-47.63'), D('-7.60')),
                                       ('transfer', 4345, 4300, D('-21.42'), D('-1.40'))]                                       # der Rest je Satz: Betrag 116,62 − 68,99, Steuer 18,62 − 11,02
    assert db.get_storno_credits() == {storno.storno_id: 0} and balances(db, 7) == [0] * 10 and helpers.euer_values(db, RANGE) == {}
    check_every_probe(db, (7, 8, 12))
    assert findings(db) == [] and db.find_unbalanced_splits(*RANGE) == []


def test_a_part_payment_before_the_closing_one_with_a_discount_keeps_its_own_rows(db):
    invoice_id = mixed_invoice(db)
    first = pay(db, invoice_id, '100.00', '2025-04-02')
    second = pay(db, invoice_id, '69.05', '2025-05-05', skonto=True)
    first_rows = kids(db, first)

    storno = cancel(db, invoice_id, '2025-06-03')

    assert kids(db, first) == first_rows                                      # die Teilzahlung bleibt, wie sie war
    assert shape(kids(db, second)) == [
        ('liquid_first', 1800, 10000, D('72.5'), None), ('transfer', 4405, 4400, D('50.01'), D('7.98')), ('transfer', 4345, 4300, D('22.49'), D('1.47')),
        ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38')), ('purpose_first', 4731, 1800, D('-1.07'), D('-0.07')),
        ('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, 4400, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38')),
        ('liquid_first', 1800, 10000, D('-1.07'), None), ('transfer', 4345, 4300, D('-1.07'), D('-0.07')), ('purpose_first', 4731, 1800, D('1.07'), D('0.07'))]
    assert payments(db, invoice_id) == [(1000000, None), (690500, None)] and state(db, invoice_id) == ('cancelled', D('3.45'))
    assert raw(db, 'SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE Status = ?', PAYMENT_MARK) == [(220500,)]         # 22,05: 11,02 + 2,03 + 7,98 + 1,47 − 0,45
    check_every_probe(db, (4, 5, 6, 7))
    refund(db, storno.storno_id, '169.05')
    assert balances(db, 7) == [0] * 10 and helpers.euer_values(db, RANGE) == {}
    check_every_probe(db, (7, 8))


def test_a_collective_payment_keeps_the_other_invoice_as_it_was(db):
    first = mixed_invoice(db, 'M-A')
    second = mixed_invoice(db, 'M-B', day='2025-03-04')
    bank = db.insert_booking('2025-04-10', D('338.10'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    assert db.assign_invoice_payment(first, bank, '169.05', None, True).ok and db.assign_invoice_payment(second, bank, '169.05').ok
    rows_of_b = [row for row in kids(db, bank) if row.number == 'M-B']

    cancel(db, first, '2025-06-03')

    assert [row for row in kids(db, bank) if row.number == 'M-B'] == rows_of_b
    assert [row.number for row in kids(db, bank)].count('M-A') == 11          # fünf Sätze der Zahlung und sechs der Rücknahme
    assert state(db, second) == ('partial_payment', D('3.45')) and payments(db, second) == [(1690500, None)]


def test_a_cancellation_before_the_bank_date_takes_the_discount_back_at_the_bank_date(db):
    """Die Rechnung ist am 03.03. geschrieben, das Geld kommt am 10.04., der Storno trägt den 15.03.: Die Rücknahme am 15.03. läge vor der Zahlung – sie trägt das spätere der beiden Daten."""
    invoice_id, bank = paid_with_discount(db, day='2025-04-10')

    cancel(db, invoice_id, '2025-03-15')

    for row in kids(db, bank)[5:]:
        assert (row.date, row.date_tax) == ('2025-04-10', '2025-04-10')
    assert [row.date for row in kids(db, bank)[:5]] == ['2025-04-10'] * 5
    check_every_probe(db, (3, 4, 5, 6))


# ═════════════════════════════════════════════════════════════════════════════
# Die Forderung, das gelernte Erlöskonto und das Erlöskonto der Erstattung
# ═════════════════════════════════════════════════════════════════════════════

def test_the_receivables_are_open_again_after_the_cancellation_and_stage_7_does_not_resolve_them_again(db):
    invoice_id, _bank = paid_with_discount(db)
    assert db.link_bank_to_entries().get('resolved') == 2                    # Stufe 7: 172,50 „an 10000“ decken beide Forderungen
    status = "SELECT Status FROM Bookings WHERE DocumentNumber = ? AND BookingType = 'entry' AND ParentBooking_ID IS NULL ORDER BY ID"
    assert raw(db, status, NUMBER) == [('resolved',), ('resolved',)]

    cancel(db, invoice_id)

    assert raw(db, status, NUMBER) == [(None,), (None,)]                     # 169,05 decken 172,50 nicht
    assert db.link_bank_to_entries().get('resolved') in (0, None) and raw(db, status, NUMBER) == [(None,), (None,)]
    check_every_probe(db, (5, 6, 7))


@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_after_the_reversal_nothing_teaches_a_discount_account_as_a_revenue_account(db, receivable):
    """Die Rücknahme ist ein positiver markierter Satz auf 4736 und 4731 – ohne den Ausschluss lernten sie das Erlöskonto für 19 und 7 %, und die nächste Zahlung bucht ihren Erlös dorthin."""
    invoice_id, _bank = paid_with_discount(db, receivable)
    cancel(db, invoice_id)

    assert db._learned_revenue_coa(db._revenue_rate_key(0.19)) == db.get_coa_id_by_account_number(4400)
    assert db._learned_revenue_coa(db._revenue_rate_key(0.07)) == db.get_coa_id_by_account_number(4300)
    assert db.resolve_revenue_coa(0.19, create=False) == db.get_coa_id_by_account_number(4400) and db.resolve_revenue_coa(0.07, create=False) == db.get_coa_id_by_account_number(4300)


@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_revenue_account_per_rate_of_the_refund_is_unambiguous_before_and_after_the_cancellation(db, receivable):
    """Die Sätze „Skonto-Konto an Bank“ (der Zahlung und der Rücknahme) sind keine Erlöse: Das Erlöskonto je Satz bleibt 4400 und 4300, nie ``MIXED_REFUND_REVENUE_AMBIGUOUS``."""
    invoice_id, _bank = paid_with_discount(db, receivable)
    coas = {D('19'): db.get_coa_id_by_account_number(4400), D('7'): db.get_coa_id_by_account_number(4300)}
    connection = db._get_connection()
    try:
        assert db._refund_revenue_coas_by_rate(connection, invoice_id, NUMBER) == coas
    finally:
        connection.close()

    cancel(db, invoice_id)

    connection = db._get_connection()
    try:
        assert db._refund_revenue_coas_by_rate(connection, invoice_id, NUMBER) == coas
    finally:
        connection.close()


# ═════════════════════════════════════════════════════════════════════════════
# Entfernen der Zahlung, geänderte Skonto-Buchung
# ═════════════════════════════════════════════════════════════════════════════

def _after_the_cancellation(db, receivable=True):
    invoice_id, bank = paid_with_discount(db, receivable)
    storno = cancel(db, invoice_id)
    return invoice_id, bank, storno


@pytest.mark.parametrize('how', ['unassign', 'delete-the-movement', 'delete-a-discount-row', 'delete-a-row-of-the-reversal'])
@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_removing_the_payment_after_the_cancellation_takes_every_row_of_it_with_it(db, how, receivable):
    """Die Rücknahmesätze tragen dieselbe Marke und Nummer unter derselben Bewegung wie die Zahlung: Wer die Zahlung entfernt – auf jedem Weg –, nimmt sie alle mit. Die Forderungen und die
    Gegenbuchungen der Stornorechnung bleiben (die Forderung eines Storno-Paares geht nie), die Debitoren stehen auf 0."""
    invoice_id, bank, _storno = _after_the_cancellation(db, receivable)
    rows = kids(db, bank)

    if how == 'unassign':
        db.unassign_invoice_payment(raw(db, 'SELECT ID FROM InvoicePayments WHERE InvoiceID = ?', invoice_id)[0][0])
    elif how == 'delete-the-movement':
        db.delete_transaction(bank)
    elif how == 'delete-a-discount-row':
        db.delete_transaction(rows[3 if receivable else 2].id)
    else:
        db.delete_transaction(rows[-1].id)

    assert raw(db, 'SELECT COUNT(*) FROM Bookings WHERE Status = ?', PAYMENT_MARK) == [(0,)]
    assert payments(db, invoice_id) == [] and state(db, invoice_id) == ('cancelled', D('172.5'))
    assert ending(db.get_susa(2025, 6), 10000) == 0 and balances(db, 6, (4405, 4345, 4400, 4300, 4736, 4731, 3806, 3801)) == [0] * 8
    assert findings(db) == [] and db.find_unbalanced_splits(*RANGE) == []


def _break(db, bank, how):
    """Ändert die Skonto-Buchung der Zahlung so, wie es der Bank-Editor, die SQL-Konsole oder ein Import tun könnte (mit Forderung: Sätze 0 Bank an Debitoren, 1/2 die Umbuchungen, 3/4 die Skonto-Sätze)."""
    rows = kids(db, bank)
    columns = 'DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, DocumentNumber, BookingType, ParentBooking_ID, EntryForm, Status, Currency'
    if how == 'discount-amount':
        raw(db, 'UPDATE Bookings SET Amount = -20000 WHERE ID = ?', rows[3].id)
    elif how == 'a-discount-row-deleted':
        raw(db, 'DELETE FROM Bookings WHERE ID = ?', rows[4].id)                                               # nur der Skonto-Satz der 7 % fehlt: Σ ≠ −Skonto
    elif how == 'a-second-discount-row':
        raw(db, f'INSERT INTO Bookings ({columns}) SELECT {columns} FROM Bookings WHERE ID = ?', rows[3].id)     # zweimal der Satz 19 %
    elif how == 'both-discount-rows-on-one-rate':
        raw(db, 'UPDATE Bookings SET TaxRate = 0.19 WHERE ID = ?', rows[4].id)                                  # zwei Skonto-Sätze mit demselben Satz
    elif how == 'a-discount-row-on-a-revenue-account':
        raw(db, 'UPDATE Bookings SET COA_ID = ? WHERE ID = ?', db.get_coa_id_by_account_number(4300), rows[4].id)  # kein Skonto-Konto mehr: Σ ≠ −Skonto
    elif how == 'revenue-row-of-a-rate-deleted':
        raw(db, 'DELETE FROM Bookings WHERE ID = ?', rows[2].id)                                                # zum Skonto-Satz der 7 % gibt es keine Umbuchung mehr
    elif how == 'revenue-row-of-a-rate-doubled':
        raw(db, f'INSERT INTO Bookings ({columns}) SELECT {columns} FROM Bookings WHERE ID = ?', rows[1].id)     # zum Skonto-Satz der 19 % gibt es zwei
    elif how == 'debtor-row-deleted':
        raw(db, 'DELETE FROM Bookings WHERE ID = ?', rows[0].id)
    elif how == 'a-discount-form':
        raw(db, "UPDATE Bookings SET EntryForm = 'transfer' WHERE ID = ?", rows[4].id)
    elif how == 'a-discount-counter-cleared':
        raw(db, 'UPDATE Bookings SET CounterCOA_ID = NULL WHERE ID = ?', rows[4].id)
    elif how == 'a-discount-tax-not-a-number':
        raw(db, "UPDATE Bookings SET TaxAmount = 'x' WHERE ID = ?", rows[3].id)
    elif how == 'a-discount-amount-not-a-number':
        raw(db, "UPDATE Bookings SET Amount = 'x' WHERE ID = ?", rows[4].id)
    elif how == 'a-discount-row-positive':
        raw(db, 'UPDATE Bookings SET Amount = 10700 WHERE ID = ?', rows[4].id)
    elif how == 'skonto-column-not-a-number':
        raw(db, "UPDATE InvoicePayments SET SkontoAmount = 'abc'")
    elif how == 'skonto-column-removed':
        raw(db, 'UPDATE InvoicePayments SET SkontoAmount = NULL')                                               # das Gegenstück: Der Skonto-Satz steht noch, die Spalte ist leer
    elif how == 'movement-is-no-bank-row':
        raw(db, "UPDATE Bookings SET BookingType = 'entry' WHERE ID = ?", bank)
    else:
        raise AssertionError(how)


BREAKS = ['discount-amount', 'a-discount-row-deleted', 'a-second-discount-row', 'both-discount-rows-on-one-rate', 'a-discount-row-on-a-revenue-account', 'revenue-row-of-a-rate-deleted',
          'revenue-row-of-a-rate-doubled', 'debtor-row-deleted', 'a-discount-form', 'a-discount-counter-cleared', 'a-discount-tax-not-a-number', 'a-discount-amount-not-a-number',
          'a-discount-row-positive', 'skonto-column-not-a-number', 'skonto-column-removed', 'movement-is-no-bank-row']


@pytest.mark.parametrize('how', BREAKS)
def test_a_changed_discount_booking_refuses_the_cancellation_and_writes_nothing(db, how):
    """Ist die Skonto-Buchung nicht mehr, wie die App sie geschrieben hat, weiß die Rücknahme nicht, was sie aufheben soll: 409, kein Beleg, keine Nummer, kein Konto, nichts im Replay-Log."""
    invoice_id, bank = paid_with_discount(db)
    _break(db, bank, how)
    before, log_before = installation.dump(db.db_name), replay_lines()

    storno = db.cancel_invoice(invoice_id, '2025-06-03', USER, today=TODAY)

    assert (storno.ok, storno.message, storno.code) == (False, SKONTO_STORNO_EDITED, 409)
    assert (storno.storno_id, storno.storno_number, storno.counter_booked) == (None, None, False)
    assert installation.dump(db.db_name) == before                            # keine Zeile, kein Zähler (die Nummer des Kreises ist nicht verbraucht), kein Konto
    assert replay_lines() == log_before
    assert db.find_storno_invoice(invoice_id) is None


def test_an_edit_that_changes_nothing_the_reversal_needs_does_not_refuse(db):
    """Abgelehnt wird nur, was die Rücknahme nicht aufheben kann: Text und Datum eines Skonto-Satzes, der Empfänger eines Satzes ändern daran nichts."""
    invoice_id, bank = paid_with_discount(db)
    rows = kids(db, bank)
    raw(db, "UPDATE Bookings SET Text = 'Skonto, vom Kunden abgezogen', DateBooking = '2025-05-06' WHERE ID IN (?, ?)", rows[3].id, rows[4].id)
    raw(db, "UPDATE Bookings SET RecipientClient = 'Käufer AG (Zentrale)' WHERE ID = ?", rows[0].id)

    cancel(db, invoice_id)

    assert shape(kids(db, bank))[5:] == [
        ('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, 4400, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38')),
        ('liquid_first', 1800, 10000, D('-1.07'), None), ('transfer', 4345, 4300, D('-1.07'), D('-0.07')), ('purpose_first', 4731, 1800, D('1.07'), D('0.07'))]
    assert {row.recipient for row in kids(db, bank)[5:]} == {'Käufer AG'} and findings(db) == []


def test_the_tax_of_a_discount_row_changed_by_hand_is_mirrored_not_recalculated(db):
    """Der Server rechnet die Steuer einer Zeile nie nach: Hat der Nutzer sie im Bank-Editor auf 0,40 € gesetzt, nimmt die Rücknahme genau diese 0,40 € zurück."""
    invoice_id, bank = paid_with_discount(db)
    raw(db, 'UPDATE Bookings SET TaxAmount = -4000 WHERE ID = ?', kids(db, bank)[3].id)

    cancel(db, invoice_id)

    assert shape(kids(db, bank))[5:8] == [('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, 4400, D('-2.38'), D('-0.40')), ('purpose_first', 4736, 1800, D('2.38'), D('0.40'))]
    assert raw(db, 'SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE Status = ?', PAYMENT_MARK) == [(220300,)] and findings(db) == []      # 22,05 − 0,02


def test_a_refused_cancellation_creates_no_account(db):
    invoice_id, bank = paid_with_discount(db, receivable=False)
    raw(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (10000, 4405, 4345)')
    raw(db, 'DELETE FROM Bookings WHERE ID = ?', kids(db, bank)[3].id)                                         # ein Skonto-Satz fehlt
    before = installation.dump(db.db_name)

    storno = db.cancel_invoice(invoice_id, '2025-06-03', USER, today=TODAY)

    assert (storno.ok, storno.message, storno.code) == (False, SKONTO_STORNO_EDITED, 409)
    assert installation.dump(db.db_name) == before
    assert db.get_coa_id_by_account_number(10000) is None and db.get_coa_id_by_account_number(4405) is None


def test_after_the_refusal_the_payment_can_be_removed_and_the_invoice_cancelled_without_a_discount(db):
    invoice_id, bank = paid_with_discount(db)
    raw(db, 'UPDATE Bookings SET Amount = -20000 WHERE ID = ?', kids(db, bank)[3].id)
    assert db.cancel_invoice(invoice_id, '2025-06-03', USER, today=TODAY).message == SKONTO_STORNO_EDITED

    db.unassign_invoice_payment(raw(db, 'SELECT ID FROM InvoicePayments')[0][0])
    storno = cancel(db, invoice_id)

    assert kids(db, bank) == [] and state(db, invoice_id) == ('cancelled', D('172.5')) and storno.counter_booked is True
    assert db.get_storno_credits() == {storno.storno_id: 0} and findings(db) == []


def test_a_late_credit_note_for_an_invoice_cancelled_by_hand_takes_the_discount_back_too(db):
    invoice_id, bank = paid_with_discount(db)
    raw(db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", invoice_id)

    storno = cancel(db, invoice_id, '2025-06-03')

    assert len(kids(db, bank)) == 11 and payments(db, invoice_id) == [(1690500, None)] and state(db, invoice_id) == ('cancelled', D('3.45'))
    assert db.get_storno_credits() == {storno.storno_id: D('169.05')} and findings(db) == []
    check_every_probe(db, (5, 6, 7))


def test_an_invoice_without_a_discount_is_cancelled_as_it_always_was(db):
    """M trägt Skonto-Bedingungen, wurde aber ohne Skonto bezahlt: Das Stornieren schreibt nichts mehr als früher – kein Satz zum Skonto, kein Eintrag im Replay-Log."""
    invoice_id = mixed_invoice(db)
    bank = pay(db, invoice_id, '172.50', '2025-05-05')
    rows, log_before = kids(db, bank), replay_lines('Cancel invoice: take back the discount')

    cancel(db, invoice_id)

    assert kids(db, bank) == rows and replay_lines('Cancel invoice: take back the discount') == log_before == []


def test_the_replay_log_carries_one_update_per_movement_and_the_rows_of_every_rate(db):
    invoice_id, _bank = paid_with_discount(db)

    cancel(db, invoice_id)

    assert len(replay_lines('Skonto-Rücknahme zu Re. M-100')) == 6                                              # sechs Sätze der Rücknahme
    assert len(replay_lines('UPDATE InvoicePayments SET SkontoAmount = NULL')) == 1                             # EIN Update für die Bewegung


# ═════════════════════════════════════════════════════════════════════════════
# Zahlung entfernen: SKONTO_REMOVAL_REFUSED und der Schutz je Satz
# ═════════════════════════════════════════════════════════════════════════════

def test_a_part_payment_cannot_be_removed_while_the_discount_stands_on_any_way(db):
    invoice_id = mixed_invoice(db)
    first = pay(db, invoice_id, '100.00', '2025-04-02')
    pay(db, invoice_id, '69.05', '2025-05-05', skonto=True)
    first_payment = raw(db, 'SELECT ID FROM InvoicePayments WHERE BookingID = ?', first)[0][0]
    before = book_tables(db)

    with pytest.raises(PaymentRemovalRefused) as refused:
        db.unassign_invoice_payment(first_payment)
    assert str(refused.value) == SKONTO_REMOVAL_REFUSED.format(number=NUMBER)
    with pytest.raises(PaymentRemovalRefused):
        db.delete_transaction(first)
    assert book_tables(db) == before


def test_removing_the_payment_with_the_discount_itself_stays_free(db):
    invoice_id = mixed_invoice(db)
    pay(db, invoice_id, '100.00', '2025-04-02')
    second = pay(db, invoice_id, '69.05', '2025-05-05', skonto=True)

    db.delete_transaction(second)

    assert payments(db, invoice_id) == [(1000000, None)] and state(db, invoice_id) == ('partial_payment', D('72.5'))
    assert findings(db) == []


def test_the_removal_of_a_payment_after_a_refund_is_refused_per_rate_with_the_rate_and_the_credit_note(db):
    """Zahlungen 100,00 (68,99 / 31,01) und 69,05 mit Skonto, storniert, 100,00 erstattet (68,99 / 31,01): Das Entfernen der zweiten Zahlung ginge in der Summe auf (100,00 gegen 100,00). Ein
    Eingriff im Editor hat 0,02 € von 19 % auf 7 % der Erstattung verschoben: Für 7 % wäre danach mehr erstattet (31,03) als gezahlt (31,01) – der Schutz je Satz (``_mixed_booked`` zählt auch den
    Skonto-Satz in seinem Satz) lehnt mit Satz und Nummer der Stornorechnung ab."""
    invoice_id = mixed_invoice(db)
    pay(db, invoice_id, '100.00', '2025-04-02')
    second = pay(db, invoice_id, '69.05', '2025-05-05', skonto=True)
    storno = cancel(db, invoice_id, '2025-06-03')
    out = refund(db, storno.storno_id, '100.00', day='2025-07-01')
    out_rows = kids(db, out)
    raw(db, 'UPDATE Bookings SET Amount = -6897 * 100 WHERE ID = ?', out_rows[1].id)
    raw(db, 'UPDATE Bookings SET Amount = -3103 * 100 WHERE ID = ?', out_rows[2].id)
    before = book_tables(db)

    with pytest.raises(PaymentRemovalRefused) as refused:
        db.delete_transaction(second)

    assert str(refused.value) == MIXED_REMOVAL_REFUSED.format(rate='7', number=storno.storno_number)
    assert book_tables(db) == before


# ═════════════════════════════════════════════════════════════════════════════
# Umzug beim Versenden einer Ersatzrechnung und Neuaufbau
# ═════════════════════════════════════════════════════════════════════════════

def test_the_payment_with_a_discount_stays_at_the_cancelled_invoice_when_the_replacement_is_sent(db):
    """„Stornieren und neu ausstellen“: Bei Steuersatz je Position zieht nichts um (``MOVE_MIXED_HINT`` – vor dem Grund ``skonto``); die Rechnung geht trotzdem raus, das Guthaben bleibt an der stornierten."""
    invoice_id, bank = paid_with_discount(db)
    storno = cancel(db, invoice_id)
    rows_before = kids(db, bank)

    new_id, change = _reissue_and_send(db, invoice_id)

    assert change.moved == 0
    assert change.payment_hint == MOVE_MIXED_HINT.format(number=NUMBER, amount='169,05 €')
    assert kids(db, bank) == rows_before and payments(db, invoice_id) == [(1690500, None)] and payments(db, new_id) == []
    assert state(db, new_id) == ('sent', D('172.5')) and db.get_storno_credits() == {storno.storno_id: D('169.05')}
    assert findings(db) == []                                                 # das Guthaben bleibt an der stornierten Rechnung: erstatten oder entfernen und neu zuordnen
    refund(db, storno.storno_id, '169.05', day='2025-07-01')
    assert db.get_storno_credits() == {storno.storno_id: 0} and findings(db) == []


def test_the_rebuild_counts_the_discount_as_settled_and_gives_the_reason_mixed(db):
    """Schritt D der Reparatur rechnet den offenen Betrag aus Geld plus Skonto; die Zuordnungen baut sie bei einem Beleg mit Steuersatz je Position nie neu auf (Grund ``mixed`` vor ``skonto``)."""
    invoice_id, _bank = paid_with_discount(db)

    plan = db.rebuild_invoice_payments(invoice_id, today='2025-12-31').plan

    assert plan.actions == [] and plan.skipped == [REBUILD_SKIP_REASONS['mixed']] and plan.rows == []
    assert (plan.due_before, plan.due_after, plan.status_before, plan.status_after) == (0, 0, 'paid', 'paid')
    assert plan.findings_before == plan.findings_after == []


def test_the_rebuild_brings_a_stale_open_amount_back_to_zero_and_keeps_the_status(db):
    invoice_id, _bank = paid_with_discount(db)
    raw(db, 'UPDATE Invoices SET AmountDue = 34500 WHERE ID = ?', invoice_id)                                  # ein veralteter Rest
    before = book_tables(db)

    result = db.rebuild_invoice_payments(invoice_id, today='2025-12-31')
    assert [action.kind for action in result.plan.actions] == ['recalc'] and result.plan.due_after == 0
    done = db.rebuild_invoice_payments(invoice_id, dry_run=False, confirmed_check=result.plan.check, today='2025-12-31')

    assert done.ok and done.executed
    assert raw(db, 'SELECT AmountDue, Status FROM Invoices WHERE ID = ?', invoice_id) == [(0, 'paid')]
    assert book_tables(db)[0] == before[0] and book_tables(db)[1] == before[1]                                 # kein Satz und keine Zuordnung geändert
    assert findings(db) == []


def test_the_rebuild_after_the_cancellation_names_the_reason_mixed_too(db):
    invoice_id, _bank = paid_with_discount(db)
    storno = cancel(db, invoice_id)

    for target in (invoice_id, storno.storno_id):
        plan = db.rebuild_invoice_payments(target, today='2025-12-31').plan
        assert plan.skipped == [REBUILD_SKIP_REASONS['mixed']] and plan.rows == [], target


# ═════════════════════════════════════════════════════════════════════════════
# Ein Beleg mit EINEM Satz aus den Stammdaten
# ═════════════════════════════════════════════════════════════════════════════

def test_an_invoice_with_a_rate_from_the_master_data_takes_the_discount_back_and_keeps_its_own_hints(db):
    """19 % aus den Stammdaten mit eigenem Erlöskonto: Stornieren nimmt das Skonto wie bei jedem Beleg mit einem Satz zurück (die Konten kommen aus den Sätzen der Zahlung, 4120 statt 4400);
    beim Umzug nennt der Hinweis das Erlöskonto aus den Stammdaten (``MOVE_ENTRY_HINT``, vor dem Grund ``skonto``), der Neuaufbau den Grund ``tax_entry``."""
    invoice_id = mixed_invoice(db, parts=[entry(19, '100.00', account(db, FREE))])
    bank = pay(db, invoice_id, '116.62', '2025-05-05', skonto=True)

    storno = cancel(db, invoice_id, '2025-06-03')

    assert shape(kids(db, bank)) == [
        ('liquid_first', 1800, 10000, D('119'), None), ('transfer', 4405, FREE, D('119'), D('19')), ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38')),
        ('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, FREE, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38'))]
    assert state(db, invoice_id) == ('cancelled', D('2.38')) and db.get_storno_credits() == {storno.storno_id: D('116.62')} and findings(db) == []
    plan = db.rebuild_invoice_payments(invoice_id, today='2025-12-31').plan
    assert plan.skipped == [REBUILD_SKIP_REASONS['tax_entry']] and plan.rows == []
    new_id, change = _reissue_and_send(db, invoice_id)
    assert change.moved == 0 and change.payment_hint == MOVE_ENTRY_HINT.format(number=NUMBER, amount='116,62 €')
    assert payments(db, new_id) == [] and kids(db, bank)[3:] and findings(db) == []
    refund(db, storno.storno_id, '116.62', day='2025-07-01')
    assert balances(db, 7, (1800, 10000, 4405, FREE, 4736, 3806)) == [0] * 6 and findings(db) == []
    check_every_probe(db, (5, 6, 7, 8))
