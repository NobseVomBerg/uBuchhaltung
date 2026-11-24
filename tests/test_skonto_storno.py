# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Skonto beim Stornieren, Erstatten, Umziehen und Neuaufbauen (2.9.0).

Eine Rechnung, die mit Skonto bezahlt ist (``assign_invoice_payment(…, skonto=True)``), trägt unter der Bankbewegung der Zahlung drei markierte Sätze: „Bank an Debitoren“ und „Wartekonto an
Erlöskonto“ über Geld plus Skonto (119,00 €) und „Skonto-Konto an Bank“ über −2,38 €. **Das Stornieren nimmt das Skonto zurück** (``cancel_invoice`` → ``_reverse_skonto_in``, in derselben
Transaktion): drei weitere markierte Sätze unter derselben Bewegung („Bank an Debitoren“ −2,38, „Wartekonto an Erlöskonto“ −2,38 mit −0,38 Steuer, „Skonto-Konto an Bank“ +2,38 mit +0,38 Steuer; ohne
Forderung zwei), mit der Nummer der stornierten Rechnung, Datum und Steuerdatum das spätere von Stornodatum und Datum der Bankbewegung; danach ist ``SkontoAmount`` NULL. Die stornierte Rechnung sieht
buchhalterisch aus wie eine Teilzahlung über das Geld (116,62 €): Das ist das Guthaben, und die Erstattung braucht keine eigene Regel. Geprüft wird:

* die Sätze der Rücknahme Spalte für Spalte (19 %, 7 %, 0 %, § 19, mit und ohne Forderung, Teilzahlung vor der schließenden Zahlung, Sammelzahlung, die Konten kommen aus den Sätzen der Zahlung);
* der Stand nach dem Stornieren und nach der Erstattung (ganz und in Teilen) – EÜR, Summen- und Saldenliste (Proben a–c in jedem Monat), Liquidität, Split-Summe und Prüfliste – auf den Minor-Wert, in
  der Testwelt wie in Zufallsbeständen (fester Seed); die benannten Kosten (``child_other_period`` in den Monaten zwischen Zahlung und Storno, zwei Bankzeilen ±2,38 im Stapel);
* das Datum der Rücknahme (das spätere von Stornodatum und Bankdatum), die Forderung (wieder offen, Stufe 7 löst sie nicht noch einmal auf), nichts lehrt 4736;
* eine geänderte Skonto-Buchung: 409 ``SKONTO_STORNO_EDITED`` ohne Schreiben, ohne Nummer, ohne Konto, ohne Replay-Log – nach dem Entfernen der Zahlung geht es;
* das Entfernen der Zahlung nach dem Stornieren (jede Zeile, jeder Weg: alle Sätze gehen mit, die Gegenbuchung bleibt, die Debitoren stehen auf 0);
* „Stornieren und neu ausstellen“ und das Versenden der Ersatzrechnung: Die Zahlung mit Skonto zieht nicht um, der Hinweis nennt den Grund – eine Teilzahlung derselben Rechnung zieht um;
* „Zuordnung neu aufbauen“: Grund ``skonto``, A–C laufen nicht, D rechnet den offenen Betrag aus Geld plus Skonto;
* eine Rechnung OHNE Skonto storniert, wie sie es immer tat (kein Satz mehr, kein Replay-Log-Eintrag mehr).

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da (``cancel_invoice(today=…)``).
"""
import datetime
import json
import os
from collections import namedtuple
from contextlib import closing
from decimal import Decimal

import pytest

import server.handlers as handlers
import userctx
from db.invoice_payments import MOVE_SKIP_REASONS, PAYMENT_MARK, SKONTO_STORNO_EDITED
from db.invoice_repair import REBUILD_SKIP_REASONS
from server.handlers import link_booking_to_invoice_capped
from tests import installation
from tests import susa_helpers as helpers
from tests.entry_form_roundtrip import differences, open_form, snapshot, submit
from tests.invoice_world import set_invoice_status
from tests.susa_helpers import account_id
from tests.test_skonto_reports import (RANGE, check_every_probe, datev_rows, ending, findings, invoice_of, minor, pay, raw, skonto_stock)
from tests.test_skonto_reports import db  # noqa: F401  (Fixture: Testdatenbank mit zwei Bankkonten)
from tests.test_invoice_storno import at, sent as world_sent, world  # noqa: F401  (world: Fixture mit den Routen der Oberfläche)

D = Decimal
MONEY = Decimal('0.0001')
USER = 'anna'
NUMBER = 'SK-100'
TODAY = datetime.date(2026, 12, 31)                    # der Tag, den die Tests dem Server als „heute“ geben (``cancel_invoice(today=…)``) – nie die Uhr
FIXED_TODAY = '2026-06-30'                             # ein fester Tag für den Status der Neuaufbau-Probeläufe
MOVE_SENTENCE = MOVE_SKIP_REASONS['skonto']


# ── Hilfen ───────────────────────────────────────────────────────────────────

# Ein Satz unter einer Bankbewegung, wie gespeichert: Form, Kontonummern, Betrag und Steuer in Euro, Satz, Text, Datum, Steuerdatum, Marke, Beleg-Nr., Kunde, Währung, Empfänger
Kid = namedtuple('Kid', 'id form coa counter amount tax rate text date date_tax status number contact currency recipient')


def kids(db, bank_id):
    numbers = db.get_coa_id_to_number_map()
    return [Kid(row[0], row[1], numbers.get(row[2]), numbers.get(row[3]), D(row[4]) * MONEY, None if row[5] is None else D(row[5]) * MONEY, *row[6:])
            for row in raw(db, 'SELECT ID, EntryForm, COA_ID, CounterCOA_ID, Amount, TaxAmount, TaxRate, Text, DateBooking, DateTax, Status, DocumentNumber, Contact_ID, Currency, '
                               'RecipientClient FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank_id)]


def shape(rows):
    """Form, Konto, Gegenkonto, Betrag und Steuer jedes Satzes – ohne den Rest."""
    return [(row.form, row.coa, row.counter, row.amount, row.tax) for row in rows]


def cancel(db, invoice_id, day='2025-06-03'):
    storno = db.cancel_invoice(invoice_id, day, USER, today=TODAY)
    assert storno.ok, storno.message
    return storno


def refund(db, storno_id, amount, day='2025-07-01'):
    """Eine ausgehende Bankbewegung über ``amount`` € und ihre Zuordnung als Erstattung der Stornorechnung."""
    bank = db.insert_booking(day, -D(amount), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    result = db.assign_storno_refund(storno_id, bank)
    assert result.ok, result.message
    return bank


def state(db, invoice_id):
    (status, due), = raw(db, 'SELECT Status, AmountDue FROM Invoices WHERE ID = ?', invoice_id)
    return status, D(due) * MONEY


def payments(db, invoice_id):
    """Die Zuordnungen einer Rechnung: (Geld, Skonto) in Minor Units, nach ID."""
    return raw(db, 'SELECT Amount, SkontoAmount FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID', invoice_id)


def paid_with_discount(db, receivable=True, number=NUMBER, gross=119.0, rate=0.19, paid='116.62', day='2025-05-05'):
    """Die Rechnung S (``SK-100``: 119,00 €, 19 %, Skonto 2 % = 2,38 €), bezahlt am 05.05.2025 mit 116,62 € und dem Skonto als Rest – ``(Rechnungs-ID, Bankbewegung)``."""
    invoice_id = invoice_of(db, number, gross, rate, receivable=receivable)
    return invoice_id, pay(db, invoice_id, paid, day, skonto=True)


def balances(db, month, numbers=(1800, 10000, 4405, 4400, 4736, 3806)):
    report = db.get_susa(2025, month)
    return [ending(report, number) for number in numbers]


def marked_tax(db):
    """Die Summe der Steuer aller markierten Sätze (Euro): die Steuer, die zum gezahlten Geld gehört – weniger die der Erstattungen."""
    return D(raw(db, 'SELECT COALESCE(SUM(TaxAmount), 0) FROM Bookings WHERE Status = ?', PAYMENT_MARK)[0][0]) * MONEY


def book_tables(db):
    """Die Buchungssätze und die Zuordnungen, Zeile für Zeile (jede Spalte)."""
    return [raw(db, f'SELECT * FROM {table} ORDER BY ID') for table in ('Bookings', 'InvoicePayments')]


def notices(report):
    return [(notice.key, notice.count) for notice in report.notices]


def replay_lines(prefix=''):
    path = os.path.join(userctx.user_data_dir(), 'sql_operations.sql')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as handle:
        return [line for line in handle.read().splitlines() if prefix in line]


# ═════════════════════════════════════════════════════════════════════════════
# Die Sätze der Rücknahme
# ═════════════════════════════════════════════════════════════════════════════

def test_cancelling_takes_the_discount_back_in_three_rows_under_the_movement_of_the_payment(db):
    customer = db.insert_contact(contact_type='customer', entity_type='company', display_name='Käufer AG', company_name='Käufer AG')
    invoice_id = invoice_of(db, NUMBER, 119.0)
    raw(db, 'UPDATE Invoices SET CustomerId = ? WHERE ID = ?', customer, invoice_id)
    bank = pay(db, invoice_id, '116.62', '2025-05-05', skonto=True)

    storno = cancel(db, invoice_id, '2025-06-03')

    rows = kids(db, bank)
    assert shape(rows) == [
        ('liquid_first', 1800, 10000, D('119'), None),                        # die Zahlung: Bank an Debitoren, Geld plus Skonto
        ('transfer', 4405, 4400, D('119'), D('19')),                          # Wartekonto an Erlöskonto, mit der Rechnungssteuer
        ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38')),                # Skonto-Konto an Bank
        ('liquid_first', 1800, 10000, D('-2.38'), None),                      # die Rücknahme: dieselben Konten, das Skonto mit umgekehrtem Vorzeichen
        ('transfer', 4405, 4400, D('-2.38'), D('-0.38')),
        ('purpose_first', 4736, 1800, D('2.38'), D('0.38'))]                  # der Skonto-Satz hebt den der Zahlung auf
    assert [row.id for row in rows] == sorted(row.id for row in rows)
    payment_rows, reversal = rows[:3], rows[3:]
    assert [row.date for row in payment_rows] == ['2025-05-05'] * 3 and [row.date_tax for row in payment_rows] == [None] * 3
    for row in reversal:                                                      # Datum und Steuerdatum: das Stornodatum (nach dem Bankdatum), Beleg-Nr.: die der stornierten Rechnung
        assert (row.date, row.date_tax, row.text, row.status, row.number) == ('2025-06-03', '2025-06-03', 'Skonto-Rücknahme zu Re. SK-100', PAYMENT_MARK, 'SK-100')
    assert [row.rate for row in reversal] == [None, 0.19, 0.19]
    for row in rows:                                                          # Kunde der Rechnung, Empfänger und Währung der Bankbewegung
        assert (row.contact, row.currency, row.recipient) == (customer, 'EUR', 'Käufer AG')
    assert raw(db, 'SELECT BookingType, AutoMirror, SourceGroup, Account_ID, Category_ID FROM Bookings WHERE ParentBooking_ID = ? AND ID > ?', bank, rows[2].id) == [
        ('entry', 0, None, None, None)] * 3
    assert payments(db, invoice_id) == [(1166200, None)]                      # Amount bleibt das Geld, SkontoAmount ist NULL
    assert db.get_invoice_skonto(invoice_id) == {}
    assert state(db, invoice_id) == ('cancelled', D('2.38'))                  # wie eine Teilzahlung: 119,00 − 116,62
    assert db.get_storno_credits() == {storno.storno_id: D('116.62')}         # das Guthaben ist das Geld, das geflossen ist
    assert findings(db) == []


def test_without_a_receivable_the_discount_comes_back_in_two_rows(db):
    invoice_id, bank = paid_with_discount(db, receivable=False)

    storno = cancel(db, invoice_id)

    assert storno.counter_booked is False                                     # ohne Forderung der App keine Gegenbuchung
    assert shape(kids(db, bank)) == [
        ('purpose_first', 4400, 1800, D('119'), D('19')),                     # Erlöskonto an Bank
        ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38')),
        ('purpose_first', 4400, 1800, D('-2.38'), D('-0.38')),                # die Rücknahme: Erlöskonto an Bank mit dem negativen Skonto …
        ('purpose_first', 4736, 1800, D('2.38'), D('0.38'))]                  # … und der Skonto-Satz mit dem positiven
    assert [row.text for row in kids(db, bank)[2:]] == ['Skonto-Rücknahme zu Re. SK-100'] * 2
    assert state(db, invoice_id) == ('cancelled', D('2.38')) and db.get_storno_credits() == {storno.storno_id: D('116.62')}
    assert balances(db, 6) == [minor('116.62'), 0, 0, minor('-98.00'), 0, minor('-18.62')]
    check_every_probe(db, (5, 6, 7))


@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
@pytest.mark.parametrize('case', [
    dict(rate=0.07, gross=107.0, paid='104.86', waiting=4345, revenue=4300, skonto=4731, amount='2.14', tax='0.14', euer={4300: '98.00', 3806: '6.86'}),
    dict(rate=0.0, gross=100.0, paid='98.00', waiting=4340, revenue=4185, skonto=4730, amount='2.00', tax=None, euer={4185: '98.00'}),
    dict(rate=-1, gross=100.0, paid='98.00', waiting=4340, revenue=4185, skonto=4730, amount='2.00', tax=None, euer={4185: '98.00'}),
], ids=['7-Prozent', '0-Prozent', 'par-19'])
def test_the_other_rates_take_the_discount_back_on_their_own_accounts(db, case, receivable):
    invoice_id, bank = paid_with_discount(db, receivable, gross=case['gross'], rate=case['rate'], paid=case['paid'])
    amount, tax = D(case['amount']), None if case['tax'] is None else D(case['tax'])

    storno = cancel(db, invoice_id)

    back = shape(kids(db, bank))[-3:] if receivable else shape(kids(db, bank))[-2:]
    assert back == ([('liquid_first', 1800, 10000, -amount, None), ('transfer', case['waiting'], case['revenue'], -amount, None if tax is None else -tax)]
                    if receivable else [('purpose_first', case['revenue'], 1800, -amount, None if tax is None else -tax)]) + [
        ('purpose_first', case['skonto'], 1800, amount, tax)]
    paid = minor(case['paid'])
    assert ending(db.get_susa(2025, 6), 10000) == (-paid if receivable else 0)
    assert ending(db.get_susa(2025, 6), case['waiting']) == (paid if receivable else 0) and ending(db.get_susa(2025, 6), case['skonto']) == 0
    assert helpers.euer_values(db, RANGE) == {number: minor(value) for number, value in case['euer'].items()}
    assert db.get_storno_credits() == {storno.storno_id: D(case['paid'])}
    check_every_probe(db, (5, 6, 7))
    refund(db, storno.storno_id, case['paid'])                                # die Erstattung nimmt alles zurück
    assert helpers.euer_values(db, RANGE) == {} and balances(db, 7, (1800, 10000, case['waiting'], case['revenue'], case['skonto'], 3806, 3801)) == [0] * 7
    check_every_probe(db, (7, 8))


def test_the_accounts_of_the_reversal_are_those_of_the_rows_of_the_payment_not_those_of_the_cancellation_day(db):
    """Ein Beleg nach § 19 bucht sein Erlöskonto nach dem Datum der Bankbewegung (bis 2025 auf 4185, ab 2026 auf 4184). Das Geld kommt im Dezember 2025, der Storno trägt den 01.02.2026: Die Rücknahme
    nimmt das Konto der Sätze der Zahlung (4185), nicht das, was ihr Datum hergäbe (4184) – sonst stünde der Erlös auf zwei Konten."""
    invoice_id = invoice_of(db, NUMBER, 100.0, rate=-1, day='2025-12-01')
    bank = pay(db, invoice_id, '98.00', '2025-12-15', skonto=True)

    storno = cancel(db, invoice_id, '2026-02-01')

    assert shape(kids(db, bank)) == [
        ('liquid_first', 1800, 10000, D('100'), None), ('transfer', 4340, 4185, D('100'), None), ('purpose_first', 4730, 1800, D('-2'), None),
        ('liquid_first', 1800, 10000, D('-2'), None), ('transfer', 4340, 4185, D('-2'), None), ('purpose_first', 4730, 1800, D('2'), None)]
    assert [row.date for row in kids(db, bank)] == ['2025-12-15'] * 3 + ['2026-02-01'] * 3
    refund(db, storno.storno_id, '98.00', day='2026-03-01')
    assert helpers.euer_values(db, ('2025-01-01', '2025-12-31')) == {4185: minor('100.00'), 4730: minor('-2.00')}        # 2025: wie nach der Zahlung
    assert helpers.euer_values(db, ('2026-01-01', '2026-12-31')) == {4185: minor('-100.00'), 4730: minor('2.00')}       # 2026: die Rücknahme und die Erstattung nehmen es auf demselben Konto zurück
    assert db.get_storno_credits() == {storno.storno_id: 0} and db.find_payment_inconsistencies() == []


# ═════════════════════════════════════════════════════════════════════════════
# Der Stand nach dem Stornieren und nach der Erstattung
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_after_the_cancellation_the_books_show_a_part_payment_of_the_money(db, receivable):
    invoice_id, _bank = paid_with_discount(db, receivable)

    cancel(db, invoice_id, '2025-06-03')

    # die Konten im Juni (Soll positiv): Bank 116,62, Debitoren 116,62 im Haben (= das Guthaben), das Wartekonto ebenso im Soll, Erlös 98,00, Skonto 0, Steuer 18,62
    assert balances(db, 6) == [minor('116.62'), minor('-116.62') if receivable else 0, minor('116.62') if receivable else 0, minor('-98.00'), 0, minor('-18.62')]
    assert helpers.euer_values(db, RANGE) == {4400: minor('98.00'), 3806: minor('18.62')}                    # das Skonto ist nach dem Jahr ganz verschwunden
    assert helpers.euer_values(db, ('2025-05-01', '2025-05-31')) == {4400: minor('100.00'), 4736: minor('-2.00'), 3806: minor('18.62')}    # im Mai wie nach der Zahlung
    june = helpers.euer_values(db, ('2025-06-01', '2025-06-30'))
    assert june == {4400: minor('-2.00'), 4736: minor('2.00')}                                                 # im Juni heben sich Erlös und Skonto auf, die Steuer bleibt 0
    assert [row for row in db.get_euer_data('2025-06-01', '2025-06-30') if row[0] == 3806 and round(row[2], 2)] == []
    check_every_probe(db, (3, 4, 5, 6, 7, 8))


def test_the_named_costs_of_the_reversal_a_notice_between_payment_and_cancellation_and_two_bank_rows_in_the_batch(db):
    """Die drei Sätze der Rücknahme hängen an einer Bewegung mit anderem Datum: Die Summen- und Saldenliste nennt es in den Monaten dazwischen (``child_other_period``, ein Hinweis, kein
    Fehler – die Proben gehen auf), und der Stapel des Stornomonats trägt zwei Bankzeilen ±2,38 (netto 0)."""
    invoice_id, _bank = paid_with_discount(db)
    cancel(db, invoice_id, '2025-06-03')

    assert [notices(db.get_susa(2025, month)) for month in (4, 5, 6, 7)] == [[], [('child_other_period', 3)], [], []]
    assert datev_rows(db, NUMBER) == [
        ('119,00', 'S', '10000', '4405', '0', '19,00', '03032025'),           # die Forderung
        ('119,00', 'S', '1800', '10000', '0', '', ''),                         # die Zahlung …
        ('119,00', 'S', '4405', '4400', '0', '19,00', ''),
        ('2,38', 'S', '4736', '1800', '101', '19,00', ''),
        ('2,38', 'H', '1800', '10000', '0', '', ''),                           # … und ihre Rücknahme (Bank an Debitoren −2,38 und der Skonto-Satz +2,38 auf 1800)
        ('2,38', 'H', '4405', '4400', '0', '19,00', ''),
        ('2,38', 'H', '4736', '1800', '101', '19,00', '')]


def test_the_refund_of_the_money_takes_every_account_back_to_zero(db):
    """Probe 3: nach dem Stornieren 10000 116,62 im Haben, 4405 116,62, 4400 98,00, 4736 0; die Erstattung über 116,62 € mit dem heutigen Code lässt alle Konten auf 0 und die EÜR leer."""
    invoice_id, _bank = paid_with_discount(db)
    storno = cancel(db, invoice_id, '2025-06-03')
    assert db.get_storno_credits() == {storno.storno_id: D('116.62')}

    bank = refund(db, storno.storno_id, '116.62', day='2025-07-01')

    assert shape(kids(db, bank)) == [('liquid_first', 1800, 10000, D('-116.62'), None), ('transfer', 4405, 4400, D('-116.62'), D('-18.62'))]      # die Steuer der Zahlungen: 18,62
    assert balances(db, 7) == [0] * 6 and balances(db, 8) == [0] * 6
    assert helpers.euer_values(db, RANGE) == {}
    assert db.get_storno_credits() == {storno.storno_id: 0} and state(db, invoice_id) == ('cancelled', D('2.38'))
    assert marked_tax(db) == 0
    check_every_probe(db, (5, 6, 7, 8, 12))


def test_the_refund_without_a_receivable_takes_every_account_back_to_zero_too(db):
    invoice_id, _bank = paid_with_discount(db, receivable=False)
    storno = cancel(db, invoice_id)

    bank = refund(db, storno.storno_id, '116.62')

    assert shape(kids(db, bank)) == [('purpose_first', 4400, 1800, D('-116.62'), D('-18.62'))]
    assert balances(db, 7) == [0] * 6 and helpers.euer_values(db, RANGE) == {}
    check_every_probe(db, (6, 7, 8))


def test_two_partial_refunds_split_the_tax_of_the_money_and_the_second_one_closes(db):
    invoice_id, _bank = paid_with_discount(db)
    storno = cancel(db, invoice_id)

    first = refund(db, storno.storno_id, '50.00', day='2025-07-01')

    assert shape(kids(db, first)) == [('liquid_first', 1800, 10000, D('-50'), None), ('transfer', 4405, 4400, D('-50'), D('-7.98'))]       # anteilig: 18,62 × 50 / 116,62
    assert db.get_storno_credits() == {storno.storno_id: D('66.62')} and balances(db, 7) == [minor('66.62'), minor('-66.62'), minor('66.62'), minor('-55.98'), 0, minor('-10.64')]
    assert helpers.euer_values(db, RANGE) == {4400: minor('55.98'), 3806: minor('10.64')}
    check_every_probe(db, (6, 7, 8))
    second = refund(db, storno.storno_id, '66.62', day='2025-07-02')

    assert shape(kids(db, second)) == [('liquid_first', 1800, 10000, D('-66.62'), None), ('transfer', 4405, 4400, D('-66.62'), D('-10.64'))]   # der Rest der Steuer: 18,62 − 7,98
    with closing(db._get_connection()) as conn:
        original_gross, original_tax = conn.execute('SELECT SumGross, TaxAmount FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()
        assert db._allocations_tax(conn, invoice_id, NUMBER, original_gross, original_tax) == 186200             # was die Zahlungen der stornierten Rechnung tragen: 19,00 − 0,38 (der Erlössatz der Rücknahme; die Skonto-Sätze zählen nicht)
    assert db.get_storno_credits() == {storno.storno_id: 0} and balances(db, 7) == [0] * 6 and helpers.euer_values(db, RANGE) == {}
    assert marked_tax(db) == 0
    check_every_probe(db, (7, 8, 12))


def test_a_part_payment_before_the_closing_one_with_a_discount_keeps_its_own_rows(db):
    invoice_id = invoice_of(db, NUMBER, 119.0)
    first = pay(db, invoice_id, '50.00', '2025-04-02')
    second = pay(db, invoice_id, '66.62', '2025-05-05', skonto=True)
    first_rows = kids(db, first)

    storno = cancel(db, invoice_id, '2025-06-03')

    assert kids(db, first) == first_rows                                      # die Teilzahlung bleibt, wie sie war
    assert shape(kids(db, second)) == [
        ('liquid_first', 1800, 10000, D('69'), None), ('transfer', 4405, 4400, D('69'), D('11.02')), ('purpose_first', 4736, 1800, D('-2.38'), D('-0.38')),
        ('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, 4400, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38'))]
    assert payments(db, invoice_id) == [(500000, None), (666200, None)] and state(db, invoice_id) == ('cancelled', D('2.38'))
    assert marked_tax(db) == D('18.62')                                       # 7,98 + 11,02 − 0,38 − 0,38 + 0,38
    assert balances(db, 6) == [minor('116.62'), minor('-116.62'), minor('116.62'), minor('-98.00'), 0, minor('-18.62')]
    check_every_probe(db, (4, 5, 6, 7))
    refund(db, storno.storno_id, '116.62')
    assert balances(db, 7) == [0] * 6 and helpers.euer_values(db, RANGE) == {}
    check_every_probe(db, (7, 8))


def test_a_collective_payment_keeps_the_other_invoice_as_it_was(db):
    first = invoice_of(db, 'SK-A', 119.0)
    second = invoice_of(db, 'SK-B', 98.0, day='2025-03-04')
    bank = db.insert_booking('2025-04-10', D('214.62'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    assert link_booking_to_invoice_capped(db, first, bank, D('116.62'), None, True) == (True, None)
    assert link_booking_to_invoice_capped(db, second, bank, D('98.00')) == (True, None)
    rows_of_b = [row for row in kids(db, bank) if row.number == 'SK-B']

    cancel(db, first, '2025-06-03')

    assert [row for row in kids(db, bank) if row.number == 'SK-B'] == rows_of_b
    assert [row.number for row in kids(db, bank)].count('SK-A') == 6          # drei Sätze der Zahlung und drei der Rücknahme
    assert state(db, second) == ('paid', 0) and payments(db, second) == [(980000, None)]
    check_every_probe(db, (4, 5, 6))


def test_a_cancellation_before_the_bank_date_takes_the_discount_back_at_the_bank_date(db):
    """Die Rechnung ist am 03.03. geschrieben, das Geld kommt am 10.04., der Storno trägt den 15.03. (er darf nicht nach heute liegen, aber vor dem Bankdatum): Die Rücknahme am 15.03. läge vor der
    Zahlung – sie trägt das spätere der beiden Daten."""
    invoice_id, bank = paid_with_discount(db, day='2025-04-10')

    cancel(db, invoice_id, '2025-03-15')

    for row in kids(db, bank)[3:]:
        assert (row.date, row.date_tax) == ('2025-04-10', '2025-04-10')
    assert [row.date for row in kids(db, bank)[:3]] == ['2025-04-10'] * 3
    assert balances(db, 4) == [minor('116.62'), minor('-116.62'), minor('116.62'), minor('-98.00'), 0, minor('-18.62')]
    check_every_probe(db, (3, 4, 5, 6))


# ═════════════════════════════════════════════════════════════════════════════
# Die Forderung und das gelernte Erlöskonto
# ═════════════════════════════════════════════════════════════════════════════

def test_the_receivable_is_open_again_after_the_cancellation_and_stage_7_does_not_resolve_it_again(db):
    invoice_id, _bank = paid_with_discount(db)
    assert db.link_bank_to_entries().get('resolved') == 1                    # Stufe 7: 119,00 „an 10000“ decken die Forderung
    assert raw(db, "SELECT Status FROM Bookings WHERE DocumentNumber = ? AND BookingType = 'entry' AND ParentBooking_ID IS NULL", NUMBER) == [('resolved',)]

    cancel(db, invoice_id)

    assert raw(db, "SELECT Status FROM Bookings WHERE DocumentNumber = ? AND BookingType = 'entry' AND ParentBooking_ID IS NULL", NUMBER) == [(None,)]      # 116,62 decken 119,00 nicht
    assert db.link_bank_to_entries().get('resolved') in (0, None)
    assert raw(db, "SELECT Status FROM Bookings WHERE DocumentNumber = ? AND BookingType = 'entry' AND ParentBooking_ID IS NULL", NUMBER) == [(None,)]
    check_every_probe(db, (5, 6, 7))


@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_after_the_reversal_nothing_teaches_the_discount_account_as_a_revenue_account(db, receivable):
    """Probe 4: Die Rücknahme ist ein positiver markierter Satz auf 4736 – ohne den Ausschluss lernte 4736 das Erlöskonto für 19 %, und die nächste Zahlung bucht ihren Erlös dorthin."""
    invoice_id, _bank = paid_with_discount(db, receivable)
    assert db.resolve_revenue_coa(0.19, create=False) == db.get_coa_id_by_account_number(4400)

    cancel(db, invoice_id)

    youngest = raw(db, 'SELECT Amount, COA_ID FROM Bookings WHERE Status = ? ORDER BY ID DESC LIMIT 1', PAYMENT_MARK)
    assert youngest == [(23800, db.get_coa_id_by_account_number(4736))]                                    # der jüngste Satz: +2,38 auf 4736 – er lehrt nichts
    assert db._learned_revenue_coa(db._revenue_rate_key(0.19)) == db.get_coa_id_by_account_number(4400)
    assert db.resolve_revenue_coa(0.19, create=False) == db.get_coa_id_by_account_number(4400)
    assert db.suggest_revenue_coa(0.19) in (None, db.get_coa_id_by_account_number(4400))
    pay(db, invoice_of(db, 'SK-200', 119.0, day='2025-03-05'), '119.00', '2025-06-10')                     # die nächste Zahlung bucht ihren Erlös auf 4400
    assert raw(db, "SELECT CounterCOA_ID FROM Bookings WHERE DocumentNumber = 'SK-200' AND Status = ? AND EntryForm = 'transfer'", PAYMENT_MARK) == [(db.get_coa_id_by_account_number(4400),)]


# ═════════════════════════════════════════════════════════════════════════════
# Zahlung entfernen, geänderte Skonto-Buchung, nachträgliche Stornorechnung
# ═════════════════════════════════════════════════════════════════════════════

def _after_the_cancellation(db, receivable=True):
    invoice_id, bank = paid_with_discount(db, receivable)
    storno = cancel(db, invoice_id)
    return invoice_id, bank, storno


@pytest.mark.parametrize('how', ['unassign', 'delete-the-movement', 'delete-the-discount-row', 'delete-a-row-of-the-reversal'])
@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_removing_the_payment_after_the_cancellation_takes_every_row_of_it_with_it(db, how, receivable):
    """Die Rücknahmesätze tragen dieselbe Marke und Nummer unter derselben Bewegung wie die Zahlung: Wer die Zahlung entfernt – auf jedem Weg –, nimmt sie alle mit. Die Forderung und die Gegenbuchung der
    Stornorechnung bleiben (die Forderung eines Storno-Paares geht nie), die Debitoren stehen auf 0."""
    invoice_id, bank, _storno = _after_the_cancellation(db, receivable)
    rows = kids(db, bank)
    receivable_rows = raw(db, "SELECT Amount FROM Bookings WHERE BookingType = 'entry' AND ParentBooking_ID IS NULL AND DocumentNumber IN (?, ?) ORDER BY ID", NUMBER, '25R001')

    if how == 'unassign':
        db.unassign_invoice_payment(raw(db, 'SELECT ID FROM InvoicePayments WHERE InvoiceID = ?', invoice_id)[0][0])
    elif how == 'delete-the-movement':
        db.delete_transaction(bank)
    elif how == 'delete-the-discount-row':
        db.delete_transaction(rows[2 if receivable else 1].id)
    else:
        db.delete_transaction(rows[-1].id)

    assert raw(db, 'SELECT COUNT(*) FROM Bookings WHERE Status = ?', PAYMENT_MARK) == [(0,)]
    assert payments(db, invoice_id) == [] and state(db, invoice_id) == ('cancelled', D('119'))
    assert raw(db, "SELECT Amount FROM Bookings WHERE BookingType = 'entry' AND ParentBooking_ID IS NULL AND DocumentNumber IN (?, ?) ORDER BY ID", NUMBER, '25R001') == receivable_rows == (
        [(1190000,), (-1190000,)] if receivable else [])
    assert ending(db.get_susa(2025, 6), 10000) == 0 and balances(db, 6, (4405, 4400, 4736, 3806)) == [0] * 4
    assert findings(db) == [] and db.find_unbalanced_splits(*RANGE) == []


def _break(db, bank, how):
    """Ändert die Skonto-Buchung der Zahlung so, wie es der Bank-Editor, die SQL-Konsole oder ein Import tun könnte."""
    rows = kids(db, bank)
    discount = rows[2]
    columns = 'DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, DocumentNumber, BookingType, ParentBooking_ID, EntryForm, Status, Currency'
    if how == 'discount-amount':
        raw(db, 'UPDATE Bookings SET Amount = -20000 WHERE ID = ?', discount.id)
    elif how == 'discount-row-deleted':
        raw(db, 'DELETE FROM Bookings WHERE ID = ?', discount.id)
    elif how == 'a-second-discount-row':
        raw(db, f'INSERT INTO Bookings ({columns}) SELECT {columns} FROM Bookings WHERE ID = ?', discount.id)
    elif how == 'revenue-row-deleted':
        raw(db, 'DELETE FROM Bookings WHERE ID = ?', rows[1].id)
    elif how == 'debtor-row-deleted':
        raw(db, 'DELETE FROM Bookings WHERE ID = ?', rows[0].id)
    elif how == 'a-row-more':
        raw(db, f'INSERT INTO Bookings ({columns}) SELECT {columns} FROM Bookings WHERE ID = ?', rows[1].id)
    elif how == 'discount-form':
        raw(db, "UPDATE Bookings SET EntryForm = 'transfer' WHERE ID = ?", discount.id)
    elif how == 'discount-counter-cleared':
        raw(db, 'UPDATE Bookings SET CounterCOA_ID = NULL WHERE ID = ?', discount.id)
    elif how == 'discount-tax-not-a-number':
        raw(db, "UPDATE Bookings SET TaxAmount = 'x' WHERE ID = ?", discount.id)
    elif how == 'skonto-column-not-a-number':
        raw(db, "UPDATE InvoicePayments SET SkontoAmount = 'abc'")
    elif how == 'movement-is-no-bank-row':
        raw(db, "UPDATE Bookings SET BookingType = 'entry' WHERE ID = ?", bank)
    else:
        raise AssertionError(how)


@pytest.mark.parametrize('how', ['discount-amount', 'discount-row-deleted', 'a-second-discount-row', 'revenue-row-deleted', 'debtor-row-deleted', 'a-row-more', 'discount-form',
                                 'discount-counter-cleared', 'discount-tax-not-a-number', 'skonto-column-not-a-number', 'movement-is-no-bank-row'])
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
    """Abgelehnt wird nur, was die Rücknahme nicht aufheben kann: Text und Datum des Skonto-Satzes, der Empfänger eines Satzes oder ein Eintrag im Replay-Log ändern daran nichts."""
    invoice_id, bank = paid_with_discount(db)
    rows = kids(db, bank)
    raw(db, "UPDATE Bookings SET Text = 'Skonto, vom Kunden abgezogen', DateBooking = '2025-05-06' WHERE ID = ?", rows[2].id)
    raw(db, "UPDATE Bookings SET RecipientClient = 'Käufer AG (Zentrale)' WHERE ID = ?", rows[0].id)

    cancel(db, invoice_id)

    assert shape(kids(db, bank))[3:] == [('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, 4400, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38'))]
    assert {row.recipient for row in kids(db, bank)[3:]} == {'Käufer AG'}                                    # Empfänger und Währung der Rücknahme sind die der Bankbewegung
    assert findings(db) == []


def test_the_tax_of_a_discount_row_changed_by_hand_is_mirrored_not_recalculated(db):
    """Der Server rechnet die Steuer einer Zeile nie nach: Hat der Nutzer sie im Bank-Editor auf 0,40 € gesetzt, nimmt die Rücknahme genau diese 0,40 € zurück – das Skonto-Konto und die Umsatzsteuer
    stehen danach ohne Rest da (18,60 € = 19,00 − 0,40)."""
    invoice_id, bank = paid_with_discount(db)
    raw(db, 'UPDATE Bookings SET TaxAmount = -4000 WHERE ID = ?', kids(db, bank)[2].id)

    cancel(db, invoice_id)

    assert shape(kids(db, bank))[3:] == [('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, 4400, D('-2.38'), D('-0.40')), ('purpose_first', 4736, 1800, D('2.38'), D('0.40'))]
    assert balances(db, 6) == [minor('116.62'), minor('-116.62'), minor('116.62'), minor('-98.02'), 0, minor('-18.60')]
    assert marked_tax(db) == D('18.60') and findings(db) == []


def test_a_refused_cancellation_creates_no_account(db):
    """Die Konten der Forderung (Debitoren, Wartekonto) legt ``cancel_invoice`` vor der Transaktion an, in eigenen Verbindungen – die Ablehnung steht davor: Fehlen sie, bleiben sie weg."""
    invoice_id, bank = paid_with_discount(db, receivable=False)
    raw(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (10000, 4405)')
    raw(db, 'DELETE FROM Bookings WHERE ID = ?', kids(db, bank)[1].id)                                # der Skonto-Satz fehlt
    before = installation.dump(db.db_name)

    storno = db.cancel_invoice(invoice_id, '2025-06-03', USER, today=TODAY)

    assert (storno.ok, storno.message, storno.code) == (False, SKONTO_STORNO_EDITED, 409)
    assert installation.dump(db.db_name) == before
    assert db.get_coa_id_by_account_number(10000) is None and db.get_coa_id_by_account_number(4405) is None


def test_after_the_refusal_the_payment_can_be_removed_and_the_invoice_cancelled_without_a_discount(db):
    """Der Satz sagt, was zu tun ist: die Zahlung entfernen (die Zuordnung samt ihren Sätzen) und neu zuordnen – oder die Rechnung ohne die Zahlung stornieren."""
    invoice_id, bank = paid_with_discount(db)
    raw(db, 'UPDATE Bookings SET Amount = -20000 WHERE ID = ?', kids(db, bank)[2].id)
    assert db.cancel_invoice(invoice_id, '2025-06-03', USER, today=TODAY).message == SKONTO_STORNO_EDITED
    assert 'erst entfernen und neu zuordnen' in SKONTO_STORNO_EDITED

    db.unassign_invoice_payment(raw(db, 'SELECT ID FROM InvoicePayments')[0][0])
    storno = cancel(db, invoice_id)

    assert kids(db, bank) == [] and state(db, invoice_id) == ('cancelled', D('119')) and storno.counter_booked is True
    assert db.get_storno_credits() == {storno.storno_id: 0} and findings(db) == []


def test_a_late_credit_note_for_an_invoice_cancelled_by_hand_takes_the_discount_back_too(db):
    """Eine von Hand stornierte Rechnung ohne Stornorechnung (Altbestand) bekommt sie nachträglich – mit derselben Transaktion, also auch derselben Rücknahme."""
    invoice_id, bank = paid_with_discount(db)
    raw(db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", invoice_id)

    storno = cancel(db, invoice_id, '2025-06-03')

    assert shape(kids(db, bank))[3:] == [('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, 4400, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38'))]
    assert payments(db, invoice_id) == [(1166200, None)] and state(db, invoice_id) == ('cancelled', D('2.38'))
    assert db.get_storno_credits() == {storno.storno_id: D('116.62')} and findings(db) == []
    check_every_probe(db, (5, 6, 7))


# ═════════════════════════════════════════════════════════════════════════════
# Eine Rechnung ohne Skonto storniert, wie sie es immer tat – und das Replay-Log
# ═════════════════════════════════════════════════════════════════════════════

def test_an_invoice_without_a_discount_is_cancelled_as_it_always_was(db):
    """Die Rechnung trägt Skonto-Bedingungen, wurde aber ohne Skonto bezahlt: Das Stornieren schreibt nichts mehr als früher – die Gegenbuchung der Forderung, sonst keinen Satz und keinen Eintrag zum Skonto im Replay-Log."""
    invoice_id = invoice_of(db, NUMBER, 119.0)
    bank = pay(db, invoice_id, '119.00', '2025-05-05')
    payments_before, kids_before = raw(db, 'SELECT * FROM InvoicePayments ORDER BY ID'), kids(db, bank)
    log_before = replay_lines()

    storno = cancel(db, invoice_id, '2025-06-03')

    assert kids(db, bank) == kids_before and len(kids_before) == 2
    assert raw(db, 'SELECT * FROM InvoicePayments ORDER BY ID') == payments_before
    assert raw(db, "SELECT COUNT(*) FROM Bookings WHERE Text LIKE '%Rücknahme%'") == [(0,)]
    assert not [line for line in replay_lines()[len(log_before):] if 'SkontoAmount' in line or 'Skonto-Rücknahme' in line]
    assert raw(db, "SELECT Amount FROM Bookings WHERE DocumentNumber = ? AND BookingType = 'entry' AND ParentBooking_ID IS NULL", storno.storno_number) == [(-1190000,)]      # die Gegenbuchung
    assert state(db, invoice_id) == ('cancelled', 0) and db.get_storno_credits() == {storno.storno_id: D('119')} and findings(db) == []


def test_the_replay_log_carries_the_rows_of_the_reversal_and_the_update_after_the_commit(db):
    invoice_id, bank = paid_with_discount(db)
    log_before = replay_lines()

    cancel(db, invoice_id)

    new = replay_lines()[len(log_before):]
    rows = [line for line in new if 'INSERT INTO Bookings' in line and 'Skonto-Rücknahme zu Re. SK-100' in line]
    assert len(rows) == 3 and sum("'invoice_payment'" in line for line in rows) == 3
    assert new[-1] == f'UPDATE InvoicePayments SET SkontoAmount = NULL WHERE InvoiceID = {invoice_id} AND BookingID = {bank};'     # zuletzt, nach den Sätzen – und nach dem Commit geschrieben
    assert [new.index(line) for line in rows] == [len(new) - 4, len(new) - 3, len(new) - 2]


def test_a_refused_cancellation_leaves_no_trace_in_the_replay_log(db):
    invoice_id, bank = paid_with_discount(db)
    raw(db, 'DELETE FROM Bookings WHERE ID = ?', kids(db, bank)[2].id)
    log_before = replay_lines()

    assert db.cancel_invoice(invoice_id, '2025-06-03', USER, today=TODAY).code == 409

    assert replay_lines() == log_before


# ═════════════════════════════════════════════════════════════════════════════
# Der Umzug auf die Ersatzrechnung
# ═════════════════════════════════════════════════════════════════════════════

def _reissue_and_send(db, invoice_id):
    draft = db.create_replacement_draft(invoice_id, USER, today=TODAY)
    assert draft.ok, draft.message
    change = db.set_invoice_status(draft.invoice_id, 'sent', USER)
    assert change.ok, change.message
    return draft.invoice_id, change


def test_a_payment_with_a_discount_stays_at_the_cancelled_invoice_when_the_replacement_is_sent(db):
    """„Stornieren und neu ausstellen“: Die Zahlung mit Skonto (nach der Rücknahme ist ``SkontoAmount`` NULL – ihre Skonto-Sätze verraten sie) zieht nicht um, die Rechnung geht trotzdem raus, und der Hinweis
    nennt den Grund."""
    invoice_id, bank = paid_with_discount(db)
    storno = cancel(db, invoice_id)
    rows_before = kids(db, bank)

    new_id, change = _reissue_and_send(db, invoice_id)

    assert change.moved == 0
    assert change.payment_hint == ('Es wurden keine Zahlungen der stornierten Rechnung SK-100 übernommen. 116,62 € ließen sich nicht automatisch übernehmen und bleiben an der '
                                   'stornierten Rechnung: die Zahlung wurde mit Skonto gebucht.')
    assert MOVE_SENTENCE == 'die Zahlung wurde mit Skonto gebucht' and list(MOVE_SKIP_REASONS).index('skonto') == list(MOVE_SKIP_REASONS).index('edited') + 1
    assert kids(db, bank) == rows_before and payments(db, invoice_id) == [(1166200, None)] and payments(db, new_id) == []
    assert state(db, new_id) == ('sent', D('119')) and db.get_storno_credits() == {storno.storno_id: D('116.62')}
    assert findings(db) == []                                                 # das Guthaben bleibt an der stornierten Rechnung: erstatten oder entfernen und neu zuordnen
    refund(db, storno.storno_id, '116.62', day='2025-07-01')
    assert db.get_storno_credits() == {storno.storno_id: 0} and findings(db) == []


def test_a_part_payment_of_the_same_invoice_moves_and_the_payment_with_the_discount_stays(db):
    invoice_id = invoice_of(db, NUMBER, 119.0)
    first = pay(db, invoice_id, '50.00', '2025-04-02')
    second = pay(db, invoice_id, '66.62', '2025-05-05', skonto=True)
    storno = cancel(db, invoice_id, '2025-06-03')
    second_rows = kids(db, second)

    new_id, change = _reissue_and_send(db, invoice_id)

    assert change.moved == 500000
    assert change.payment_hint == ('Zahlungen der stornierten Rechnung SK-100 über 50,00 € sind jetzt dieser Rechnung zugeordnet. 66,62 € ließen sich nicht automatisch übernehmen und '
                                   'bleiben an der stornierten Rechnung: die Zahlung wurde mit Skonto gebucht.')
    assert payments(db, new_id) == [(500000, None)] and payments(db, invoice_id) == [(666200, None)]
    assert kids(db, second) == second_rows and not [row for row in kids(db, first) if row.number == NUMBER]     # die Sätze der Teilzahlung gingen mit (neue Nummer), die mit Skonto blieben
    assert db.get_storno_credits() == {storno.storno_id: D('66.62')}
    assert findings(db) == []
    check_every_probe(db, (4, 5, 6, 7))
    refund(db, storno.storno_id, '66.62', day='2025-07-01')                                                   # der Rest des Guthabens: seine Steuer ist 11,02 − 0,38 (der Erlössatz der Rücknahme) = 10,64
    assert shape(kids(db, raw(db, 'SELECT MAX(ID) FROM Bookings WHERE BookingType = ?', 'bank')[0][0])) == [
        ('liquid_first', 1800, 10000, D('-66.62'), None), ('transfer', 4405, 4400, D('-66.62'), D('-10.64'))]
    assert db.get_storno_credits() == {storno.storno_id: 0} and findings(db) == []
    check_every_probe(db, (7, 8))


def test_the_whole_way_through_the_routes_pay_with_a_discount_cancel_and_reissue_send(world, monkeypatch):
    """Bezahlen mit Skonto, „Stornieren und neu ausstellen“ (``handle_invoice_cancel`` mit ``reissue``), die Ersatzrechnung versenden (``handle_update_invoice_status``): Die Antwort nennt, was mit der
    Zahlung geschah, die Zahlung bleibt mit allen Sätzen an der stornierten Rechnung, die Bücher gehen auf."""
    db = world.db
    r1 = world_sent(world, 'T-100')                                           # 119,00 €, 19 %, Skonto 2 % in 7 Tagen (Vorgabe der Maske der Tests)
    booking = db.insert_booking('2026-07-01', 116.62, booking_type='bank', account_id=world.bank, recipient_client='Malermeister Pinsel')
    assert handlers.link_booking_to_invoice_capped(db, r1, booking, 116.62, None, True) == (True, None)
    at(monkeypatch, '2026-07-10')

    code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': r1, 'date': '2026-07-05', 'reissue': True}).encode())

    body = json.loads(text)
    assert code == 200 and body['success'] is True and body['replacement_id'], body
    rows = kids(db, booking)
    assert shape(rows)[3:] == [('liquid_first', 1800, 10000, D('-2.38'), None), ('transfer', 4405, 4400, D('-2.38'), D('-0.38')), ('purpose_first', 4736, 1800, D('2.38'), D('0.38'))]
    assert {row.date for row in rows[3:]} == {'2026-07-05'} and {row.number for row in rows} == {'T-100'}
    assert payments(db, r1) == [(1166200, None)] and db.get_storno_credits() == {body['storno_id']: D('116.62')}

    code, answer = set_invoice_status(body['replacement_id'], 'sent')

    assert (code, answer['success']) == (200, True)
    assert answer['hint'] == ('Es wurden keine Zahlungen der stornierten Rechnung T-100 übernommen. 116,62 € ließen sich nicht automatisch übernehmen und bleiben an der '
                              'stornierten Rechnung: die Zahlung wurde mit Skonto gebucht.')
    assert kids(db, booking) == rows and payments(db, r1) == [(1166200, None)] and payments(db, body['replacement_id']) == []
    assert db.find_payment_inconsistencies() == [] and db.find_unbalanced_splits('2026-01-01', '2026-12-31') == []


def test_the_cancel_route_names_the_changed_discount_booking_and_cancels_nothing(world, monkeypatch):
    db = world.db
    r1 = world_sent(world, 'T-100')
    booking = db.insert_booking('2026-07-01', 116.62, booking_type='bank', account_id=world.bank, recipient_client='Malermeister Pinsel')
    assert handlers.link_booking_to_invoice_capped(db, r1, booking, 116.62, None, True) == (True, None)
    raw(db, 'UPDATE Bookings SET Amount = -20000 WHERE ID = ?', kids(db, booking)[2].id)
    at(monkeypatch, '2026-07-10')
    before = installation.dump(db.db_name)

    code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': r1, 'date': '2026-07-05'}).encode())

    assert (code, json.loads(text)) == (409, {'success': False, 'error': SKONTO_STORNO_EDITED})
    assert installation.dump(db.db_name) == before


def test_the_move_check_recognises_a_payment_with_a_discount_by_the_column_and_by_the_rows(db):
    invoice_id, bank = paid_with_discount(db)
    other = pay(db, invoice_of(db, 'SK-OTHER', 50.0, day='2025-03-05'), '10.00', '2025-04-11')                      # eine unbeteiligte Bewegung

    def booked(**kw):
        with closing(db._get_connection()) as conn:
            return db._skonto_booked(conn, invoice_id, NUMBER, **kw)

    assert booked() and booked(bank_id=bank) and not booked(bank_id=other)           # die Spalte
    raw(db, 'UPDATE InvoicePayments SET SkontoAmount = NULL WHERE InvoiceID = ?', invoice_id)
    assert booked() and booked(bank_id=bank) and not booked(bank_id=other)           # nach der Rücknahme beim Stornieren: nur die Sätze auf einem Skonto-Konto
    raw(db, 'DELETE FROM Bookings WHERE ParentBooking_ID = ? AND COA_ID = ?', bank, db.get_coa_id_by_account_number(4736))
    assert not booked() and not booked(bank_id=bank)                                  # weder Spalte noch Sätze
    raw(db, 'UPDATE InvoicePayments SET SkontoAmount = 23800 WHERE InvoiceID = ?', invoice_id)
    assert booked() and booked(bank_id=bank) and not booked(bank_id=other)           # die Spalte allein


# ═════════════════════════════════════════════════════════════════════════════
# „Zuordnung neu aufbauen“
# ═════════════════════════════════════════════════════════════════════════════

def test_the_rebuild_of_an_invoice_with_a_discount_rebuilds_nothing_and_recalculates_the_open_amount(db):
    """Grund ``skonto``: A–C laufen nicht (die Sätze der Zahlung stünden sonst ohne Skonto neu da), D rechnet den offenen Betrag aus Geld plus Skonto – Probelauf und Ausführung sind derselbe Code."""
    invoice_id, _bank = paid_with_discount(db)

    clean = db.rebuild_invoice_payments(invoice_id, today=FIXED_TODAY).plan
    assert (clean.actions, clean.rows, clean.skipped) == ([], [], [REBUILD_SKIP_REASONS['skonto']]) and (clean.due_before, clean.due_after) == (0, 0)
    assert REBUILD_SKIP_REASONS['skonto'] == ('Die Rechnung trägt eine Zahlung mit Skonto – ihre Zuordnungen werden in dieser Version nicht automatisch neu aufgebaut; '
                                              'neu gerechnet wird nur der offene Betrag.')
    assert list(REBUILD_SKIP_REASONS).index('skonto') == list(REBUILD_SKIP_REASONS).index('tax_entry') + 1

    raw(db, 'UPDATE Invoices SET AmountDue = 23800 WHERE ID = ?', invoice_id)                       # ein veralteter Rest
    before = book_tables(db)
    dry_run = db.rebuild_invoice_payments(invoice_id, today=FIXED_TODAY)
    assert [action.kind for action in dry_run.plan.actions] == ['recalc'] and dry_run.plan.skipped == [REBUILD_SKIP_REASONS['skonto']] and dry_run.plan.due_after == 0
    assert book_tables(db) == before                                                                # der Probelauf lässt alles, wie es war

    done = db.rebuild_invoice_payments(invoice_id, dry_run=False, confirmed_check=dry_run.plan.check, today=FIXED_TODAY)

    assert done.ok and done.executed
    assert book_tables(db) == before                                                                # kein Satz geschrieben, keine Zuordnung angefasst
    assert raw(db, 'SELECT AmountDue, Status FROM Invoices WHERE ID = ?', invoice_id) == [(0, 'paid')] and findings(db) == []


def test_the_rebuild_leaves_an_assignment_without_rows_alone_next_to_a_payment_with_a_discount(db):
    """Neben der Zahlung mit Skonto steht eine Zuordnung ohne Sätze (wie ein Import sie hinterlässt) – ohne den Grund bräuchte C sie neu zu bauen: Es läuft nicht, die Zuordnung bleibt."""
    invoice_id = invoice_of(db, NUMBER, 119.0)
    old_bank = db.insert_booking('2025-04-02', D('50.00'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    raw(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 500000, ?)', invoice_id, old_bank, '2025-04-02')
    raw(db, "UPDATE Invoices SET AmountDue = 690000, Status = 'partial_payment' WHERE ID = ?", invoice_id)
    pay(db, invoice_id, '66.62', '2025-05-05', skonto=True)
    before = book_tables(db)

    plan = db.rebuild_invoice_payments(invoice_id, today=FIXED_TODAY).plan

    assert plan.skipped == [REBUILD_SKIP_REASONS['skonto']] and [action.kind for action in plan.actions] == [] and plan.rows == []
    assert book_tables(db) == before


def test_the_rebuild_of_a_cancelled_invoice_names_the_discount_too(db):
    invoice_id, _bank, _storno = _after_the_cancellation(db)

    plan = db.rebuild_invoice_payments(invoice_id, today=FIXED_TODAY).plan

    assert plan.skipped == [REBUILD_SKIP_REASONS['skonto']] and plan.actions == [] and (plan.due_before, plan.due_after) == (23800, 23800)


# ═════════════════════════════════════════════════════════════════════════════
# Zufallsbestände: nur Wege der App, feste Seeds
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('seed', [21, 22, 23])
def test_a_random_stock_cancelled_and_refunded_balances_in_every_step(db, seed):
    """Rechnungen mit 19, 7, 0 % und § 19, mit und ohne Forderung, Teilzahlungen, schließende Zahlungen mit und ohne Skonto (``skonto_stock``): erst Stufe 7, dann jede Rechnung stornieren (die mit Skonto
    nehmen es zurück), dann jede Erstattung über das ganze Guthaben – nach jedem Schritt gehen alle Proben auf, am Ende steht nichts mehr in den Büchern."""
    stock = skonto_stock(db, seed)
    db.link_bank_to_entries()
    discounts = sum(1 for _id, _name, _gross, discount in stock if discount)
    assert discounts >= 1
    before = {name: payments(db, invoice_id) for invoice_id, name, _gross, _discount in stock}
    storno_ids = {}

    for invoice_id, name, _gross, _discount in stock:
        storno_ids[name] = cancel(db, invoice_id, '2025-09-15').storno_id
    check_every_probe(db, (3, 4, 5, 6, 7, 8, 9), seed)
    db.link_bank_to_entries()
    check_every_probe(db, (5, 9), seed)
    for invoice_id, name, gross, discount in stock:
        assert [row[0] for row in payments(db, invoice_id)] == [row[0] for row in before[name]]          # das Geld bleibt
        assert all(row[1] is None for row in payments(db, invoice_id))                                   # das Skonto ist zurückgenommen
        assert state(db, invoice_id) == ('cancelled', D(discount) * MONEY)                               # der Rest ist das Skonto: nur das Geld ist bezahlt
        assert db.get_storno_credits()[storno_ids[name]] == (D(gross) - D(discount)) * MONEY             # das Guthaben ist das Geld

    for invoice_id, name, gross, discount in stock:
        refund(db, storno_ids[name], str((D(gross) - D(discount)) * MONEY), day='2025-09-20')
    assert set(db.get_storno_credits().values()) == {0}
    check_every_probe(db, (9, 10, 12), seed)
    assert helpers.euer_values(db, RANGE) == {}


# ═════════════════════════════════════════════════════════════════════════════
# Der Bank-Editor
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_opening_and_saving_the_bank_editor_after_the_cancellation_changes_nothing(db, receivable):
    """Die Rücknahme legt sechs (ohne Forderung vier) markierte Sätze unter die Bewegung, darunter „Bank an Debitoren“ mit negativem Betrag: Der Editor liest keinen davon als Forderung oder Spiegel."""
    invoice_id, bank = paid_with_discount(db, receivable)
    cancel(db, invoice_id)
    before_rows = snapshot(db)
    before_tables = book_tables(db)

    post, _options = open_form(db, bank)
    submit(db, post)

    assert differences(before_rows, snapshot(db), db.get_coa_id_to_number_map()) == []
    assert book_tables(db) == before_tables
    assert payments(db, invoice_id) == [(1166200, None)] and findings(db) == []
