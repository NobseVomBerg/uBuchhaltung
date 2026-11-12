# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Stornorechnung (2.2.1): ``Database.cancel_invoice`` und die Route ``POST /invoice/cancel``.

Eine versendete Rechnung wird über „Stornieren“ storniert: Es entsteht ein eigener Beleg mit eigener Nummer aus dem
Rechnungskreis (Jahr des Stornodatums), negativen Beträgen, ``InvoiceKind = 'storno'`` und ``CancelsInvoiceId``; die
Rechnung wird „storniert“, und – hat die App ihre Forderung gebucht – die Gegenbuchung hebt die Forderung auf. Die
Forderung der Ursprungsrechnung wird nie gelöscht. Zahlungen bleiben an der stornierten Rechnung; der Stornorechnung
lässt sich keine Zahlung zuordnen, und sie ist ganz gesperrt. Die Dokumente stehen in
``test_invoice_storno_documents.py``.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import datetime
import json
import re
import shutil
import sqlite3
import subprocess
import threading

import pytest

import server.handlers as handlers
import userctx
from db.invoice_lock import (CHANGED_MESSAGE, HAS_STORNO_MESSAGE, InvoiceLocked, STORNO_LABEL,
                             STORNO_LOCKED_MESSAGE, STORNO_STATUS_MESSAGE, allowed_targets)
from db.invoice_payments import PAYMENT_MARK, STORNO_PAYMENT_MESSAGE
from db.invoice_storno import (NO_COUNTER_BOOKING_HINT, STORNO_DATE_BEFORE_MESSAGE, STORNO_DATE_MESSAGE,
                               STORNO_FAILED_MESSAGE, STORNO_PAYMENT_TEXT, Storno)
from db.invoices import InvoiceNotDeletable
from server.pages import Footer, PageReminders
from server.pages_invoice import PageInvoice, STORNO_COPY_MESSAGE, cancel_question
from tests.document_helpers import post_json, state
from tests.dom_helpers import Dom, app_msg_arguments
from tests.http_client import FOREIGN_ORIGIN, get
from tests.invoice_world import (build_world, columns, force_status, make_invoice, make_payload, protocol,
                                 query_rows, run_sql, save, save_raw, set_invoice_status, snapshot)

USER = 'anna'
RANGE = ('2026-01-01', '2027-12-31')
ONE_ITEM = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung', 'unitPrice': 100.0,
             'taxRate': 19}]                                      # 100,00 netto + 19,00 USt = 119,00
DEBTORS, WAITING = 10000, 4405
TABLES = ('Invoices', 'InvoiceItems', 'InvoicePayments', 'Bookings', 'NumberRanges', 'InvoiceCorrections',
          'InvoiceDocumentVersions', 'ChartOfAccounts')


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user(USER)
    w = build_world(tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return w


# ── Hilfen ───────────────────────────────────────────────────────────────────

def sent(w, number='T-100', items=ONE_ITEM, **over):
    """Eine versendete Rechnung über 119,00 (eigene Nummer, damit der Zähler des Kreises für das Storno frei bleibt),
    versendet wie in der Oberfläche: Forderung gebucht, Dokument erzeugt."""
    invoice_id = make_invoice(w, 'draft', number=number, items=items, **over)
    code, body = set_invoice_status(invoice_id, 'sent')
    assert code == 200 and 'warning' not in body, body
    return invoice_id


def pay(w, invoice_id, amount):
    """Eine Zahlung zuordnen, wie die Oberfläche es tut (Zahlungs- und Umbuchungssatz unter der Bankbewegung)."""
    booking = w.db.insert_booking('2026-07-01', amount, booking_type='bank', account_id=w.bank,
                                  recipient_client='Malermeister Pinsel')
    ok, error = handlers.link_booking_to_invoice_capped(w.db, invoice_id, booking, amount)
    assert ok, error
    return booking


def cancel(w, invoice_id, day='2026-07-01'):
    storno = w.db.cancel_invoice(invoice_id, day, USER)
    assert storno.ok is True, storno.message
    return storno


def bookings_of(db, number):
    """Buchungssätze mit dieser Beleg-Nr.: (ID, Konto-Nr., Gegenkonto-Nr., Betrag, Steuer, Datum, Form, Text)."""
    return [(r['ID'], r['coa'], r['counter'], r['Amount'], r['TaxAmount'], r['DateBooking'], r['EntryForm'],
             r['Text']) for r in query_rows(
        db, 'SELECT b.ID, c.AccountNumber AS coa, g.AccountNumber AS counter, b.Amount, b.TaxAmount, '
            'b.DateBooking, b.EntryForm, b.Text FROM Bookings b '
            'LEFT JOIN ChartOfAccounts c ON c.ID = b.COA_ID LEFT JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID '
            "WHERE b.DocumentNumber = ? AND b.BookingType = 'entry' AND b.ParentBooking_ID IS NULL ORDER BY b.ID",
        number)]


def balance(db, number):
    """Saldo eines Kontos über alle Buchungssätze: Soll (Konto) minus Haben (Gegenkonto), in Minor Units."""
    (row,) = query_rows(
        db, "SELECT COALESCE(SUM(CASE WHEN b.COA_ID = c.ID THEN b.Amount END), 0) AS soll, "
            "COALESCE(SUM(CASE WHEN b.CounterCOA_ID = c.ID THEN b.Amount END), 0) AS haben "
            "FROM Bookings b, ChartOfAccounts c WHERE c.AccountNumber = ? AND b.BookingType = 'entry'", number)
    return row['soll'] - row['haben']


def euer(db):
    return {number: round(total, 2) for number, _name, total in db.get_euer_data(*RANGE)}


def items_of(db, invoice_id):
    return [(r['Position'], r['Description'], r['Quantity'], r['PricePerUnit'], r['TotalNet'], r['Unit'],
             r['TaxRate']) for r in query_rows(
        db, 'SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position, ID', invoice_id)]


def tables(db):
    return state(db, *TABLES)


def refused(w, invoice_id, day='2026-07-01', code=409, message=None):
    """Die Stornierung wird abgelehnt: Statuscode, Klartext, und nichts wurde geschrieben (auch kein Konto, kein
    Zähler)."""
    before = tables(w.db)
    storno = w.db.cancel_invoice(invoice_id, day, USER)
    assert (storno.ok, storno.code) == (False, code), storno
    assert storno.message and (message is None or storno.message == message), storno.message
    assert (storno.storno_id, storno.storno_number, storno.counter_booked) == (None, None, False)
    assert tables(w.db) == before
    return storno


# ── Eine unbezahlte Rechnung stornieren ──────────────────────────────────────

def test_cancelling_an_unpaid_invoice_creates_the_credit_note_and_the_counter_booking(world):
    db = world.db
    invoice_id = sent(world)
    original = columns(db, invoice_id)
    euer_before = euer(db)

    storno = cancel(world, invoice_id, '2026-07-01')

    assert storno == Storno(True, None, 200, storno.storno_id, '26R001', True)        # nächste Nummer des Kreises
    credit = columns(db, storno.storno_id)
    assert (credit['InvoiceNumber'], credit['InvoiceDate'], credit['Status']) == ('26R001', '2026-07-01', 'sent')
    assert (credit['InvoiceKind'], credit['CancelsInvoiceId'], credit['DocumentType']) == (
        'storno', invoice_id, 'invoice')
    assert credit['SentAt'] and credit['ReplacesInvoiceId'] is None and credit['DocumentVersion'] is None
    # Summen exakt negiert (Minor Units), Rest = Brutto der Stornorechnung
    assert (original['SumNet'], original['TaxAmount'], original['SumGross']) == (1000000, 190000, 1190000)
    assert (credit['SumNet'], credit['TaxAmount'], credit['SumGross']) == (-1000000, -190000, -1190000)
    assert credit['AmountDue'] == -1190000
    # Kopf: alles, was zur Rechnung gehört, kommt mit; Fälligkeit, Skonto, Texte und Dokumente nicht
    for column in ('OwnCompanyId', 'SellerName', 'SellerCompany', 'SellerStreet', 'SellerPostalCode', 'SellerCity',
                   'SellerCountry', 'SellerVATID', 'SellerEmail', 'SellerPhone', 'CustomerId', 'BuyerName',
                   'BuyerCompany', 'BuyerStreet', 'BuyerPostalCode', 'BuyerCity', 'BuyerCountry', 'BuyerVATID',
                   'BuyerReference', 'BuyerRouteID', 'OrderNumber', 'Currency', 'DeliveryDate', 'BankAccountId',
                   'BankName', 'BankIBAN', 'BankBIC', 'TaxCategory', 'TaxRate'):
        assert credit[column] == original[column], column
    assert credit['DeliveryDate'] == original['DeliveryDate'] == '2026-06-05'
    assert (credit['PaymentDueDate'], credit['SkontoDays'], credit['SkontoPercent']) == (None, None, None)
    assert credit['PaymentTerms'] == STORNO_PAYMENT_TEXT == 'Der Betrag wird verrechnet bzw. erstattet.'
    assert (credit['PDFPath'], credit['XMLPath'], credit['SourceQuoteId']) == (None, None, None)
    assert (credit['IntroText'], credit['ClosingText']) == (None, None)
    # Positionen: dieselbe Reihenfolge, Menge und Positionssumme negiert, Einzelpreis und der Rest gleich
    assert items_of(db, invoice_id) == [(1, 'Beratung', 1.0, 1000000, 1000000, 'C62', 19.0)]
    assert items_of(db, storno.storno_id) == [(1, 'Beratung', -1.0, 1000000, -1000000, 'C62', 19.0)]
    # die Rechnung ist storniert, sonst ändert sich an ihr nichts
    assert columns(db, invoice_id)['Status'] == 'cancelled'
    assert {k: v for k, v in columns(db, invoice_id).items() if k not in ('Status', 'UpdatedAt')} == \
        {k: v for k, v in original.items() if k not in ('Status', 'UpdatedAt')}
    # zwei Forderungsbuchungen: die der Rechnung bleibt, die Gegenbuchung hebt sie auf
    forderung, gegenbuchung = (bookings_of(db, 'T-100'), bookings_of(db, '26R001'))
    assert [(r[1:7]) for r in forderung] == [(DEBTORS, WAITING, 1190000, 190000, '2026-06-07', 'transfer')]
    assert [(r[1:7]) for r in gegenbuchung] == [(DEBTORS, WAITING, -1190000, -190000, '2026-07-01', 'transfer')]
    assert gegenbuchung[0][7] == 'Stornorechnung 26R001 Malermeister Pinsel'
    assert balance(db, DEBTORS) == 0 and balance(db, WAITING) == 0
    assert db.get_receivable_booking(invoice_id) == forderung[0][0]
    assert db.get_receivable_booking(storno.storno_id) == gegenbuchung[0][0]
    # keine Einnahme, kein Befund der Prüfliste
    assert euer(db) == euer_before
    assert db.find_payment_inconsistencies() == []


def test_the_protocol_has_a_row_for_each_of_the_two_invoices(world):
    invoice_id = sent(world)
    storno = cancel(world, invoice_id)
    rows = [(r['InvoiceNumber'], r['Version'], r['UserName'], r['Field'], r['OldValue'], r['NewValue'])
            for r in protocol(world.db, invoice_id, 'status')]
    assert rows == [('T-100', 1, USER, 'Status', 'draft', 'sent'), ('T-100', 1, USER, 'Status', 'sent', 'cancelled')]
    assert [(r['InvoiceNumber'], r['Version'], r['UserName'], r['OldValue'], r['NewValue'])
            for r in protocol(world.db, storno.storno_id)] == [('26R001', 1, USER, None, 'sent')]
    assert world.db.was_sent_before(storno.storno_id) is True        # SentAt: eine Stornorechnung war versendet


@pytest.mark.parametrize('status', ['sent', 'overdue', 'partial_payment', 'partial', 'paid'])
def test_every_locked_status_can_be_cancelled(world, status):
    invoice_id = sent(world)
    force_status(world.db, invoice_id, status)
    storno = cancel(world, invoice_id)
    assert columns(world.db, invoice_id)['Status'] == 'cancelled'
    assert [r['OldValue'] for r in protocol(world.db, invoice_id, 'status')][-1] == (
        'partial_payment' if status == 'partial' else status)             # der Alt-Wert wird kanonisch festgehalten
    assert columns(world.db, storno.storno_id)['Status'] == 'sent'


def test_the_number_comes_from_the_circle_of_the_year_of_the_storno_date(world):
    db = world.db
    invoice_id = sent(world, invoiceDate='2026-12-28', deliveryDate='2026-12-28')
    assert db.fetch_number_ranges('invoice')[0][2] == 2026 and len(db.fetch_number_ranges('invoice')) == 1

    storno = cancel(world, invoice_id, '2027-01-04')                     # Folgejahr: der Kreis fehlt, er entsteht

    assert storno.storno_number == '27R001'
    assert sorted((r[2], r[3], r[5]) for r in db.fetch_number_ranges('invoice')) == [(2026, 'R', 0), (2027, 'R', 1)]
    assert columns(db, storno.storno_id)['InvoiceDate'] == '2027-01-04'
    assert [r[5] for r in bookings_of(db, '27R001')] == ['2027-01-04']   # die Gegenbuchung trägt das Stornodatum
    second = sent(world, number='T-101', invoiceDate='2026-12-29', deliveryDate='2026-12-29')
    assert cancel(world, second, '2026-12-30').storno_number == '26R001'     # das alte Jahr zählt in seinem Kreis


def test_storno_numbers_follow_the_counter_and_skip_taken_numbers(world):
    db = world.db
    first = sent(world, number='26R002')                                     # belegt die Nummer, die als Nächste käme …
    run_sql(db, "UPDATE NumberRanges SET CurrentNumber = 0")                 # … ohne den Zähler zu heben
    storno = cancel(world, first)
    assert storno.storno_number == '26R001'
    assert cancel(world, sent(world, number='T-102')).storno_number == '26R003'      # 26R002 ist vergeben, wird übersprungen


# ── Teilbezahlt und bezahlt ──────────────────────────────────────────────────

@pytest.mark.parametrize('amount, status', [(50.0, 'partial_payment'), (119.0, 'paid')])
def test_a_partly_paid_and_a_paid_invoice_keep_their_payments_and_get_the_counter_booking(world, amount, status):
    db = world.db
    invoice_id = sent(world)
    booking = pay(world, invoice_id, amount)
    assert columns(db, invoice_id)['Status'] == status
    payments = query_rows(db, 'SELECT * FROM InvoicePayments ORDER BY ID')
    payment_rows = query_rows(db, 'SELECT * FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', booking)
    assert [r['Status'] for r in payment_rows] == [PAYMENT_MARK, PAYMENT_MARK]
    euer_before, due = euer(db), columns(db, invoice_id)['AmountDue']

    storno = cancel(world, invoice_id)

    assert query_rows(db, 'SELECT * FROM InvoicePayments ORDER BY ID') == payments            # unverändert
    assert query_rows(db, 'SELECT * FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', booking) == payment_rows
    original = columns(db, invoice_id)
    assert (original['Status'], original['AmountDue']) == ('cancelled', due)
    assert columns(db, storno.storno_id)['AmountDue'] == -1190000
    assert [r[3] for r in bookings_of(db, 'T-100')] == [1190000]               # die Forderung bleibt
    assert [r[3] for r in bookings_of(db, '26R001')] == [-1190000]             # die Gegenbuchung entsteht
    assert euer(db) == euer_before                                             # am Zahlungstag ändert sich nichts


@pytest.mark.parametrize('amount', [50.0, 119.0])
def test_removing_the_payment_afterwards_keeps_both_receivables(world, amount):
    db = world.db
    invoice_id = sent(world)
    pay(world, invoice_id, amount)
    storno = cancel(world, invoice_id)
    receivables = [r[0] for r in bookings_of(db, 'T-100')] + [r[0] for r in bookings_of(db, '26R001')]

    db.unassign_invoice_payment(db.get_invoice_payments(invoice_id)[0][0])

    assert [r[0] for r in bookings_of(db, 'T-100')] + [r[0] for r in bookings_of(db, '26R001')] == receivables
    assert columns(db, invoice_id)['Status'] == 'cancelled' and columns(db, invoice_id)['AmountDue'] == 1190000
    assert columns(db, storno.storno_id)['Status'] == 'sent'
    assert balance(db, DEBTORS) == 0


def test_what_the_check_list_says_about_a_paid_and_cancelled_invoice(world):
    """Bezahlt und storniert (Erstattung folgt mit 2.2.2): Die Prüfliste meldet dazu nichts Falsches – weder für die
    Rechnung noch für die Stornorechnung."""
    invoice_id = sent(world)
    pay(world, invoice_id, 119.0)
    storno = cancel(world, invoice_id)
    assert world.db.find_payment_inconsistencies() == []
    assert world.db.find_payment_inconsistencies(invoice_id) == []
    assert world.db.find_payment_inconsistencies(storno.storno_id) == []
    assert balance(world.db, DEBTORS) == -1190000          # das Guthaben des Kunden: noch nicht erstattet


# ── Ohne Forderung der App: keine Gegenbuchung ───────────────────────────────

def _imported_without_receivable(w):
    return make_invoice(w, 'sent', number='T-200', items=ONE_ITEM)         # ein Import: Status, aber keine Forderung


def _tax_rate_without_a_waiting_account(w):
    items = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Buch', 'unitPrice': 100.0,
              'taxRate': 16}]
    return sent(w, number='T-201', items=items, taxRate=0.16)


def _imported_receivable(w):
    invoice_id = sent(w, number='T-202')
    run_sql(w.db, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-1' WHERE DocumentNumber = 'T-202'")
    return invoice_id


def _hand_booked_receivable(w):
    invoice_id = sent(w, number='T-203')
    run_sql(w.db, "UPDATE Bookings SET CounterCOA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4400) "
                  "WHERE DocumentNumber = 'T-203'")
    return invoice_id


@pytest.mark.parametrize('prepare', [_imported_without_receivable, _tax_rate_without_a_waiting_account,
                                     _imported_receivable, _hand_booked_receivable],
                         ids=['Import-ohne-Forderung', 'Steuersatz-ohne-Wartekonto', 'importierte-Forderung',
                              'von-Hand-gebuchte-Forderung'])
def test_without_a_receivable_of_the_app_the_cancellation_books_no_counter_booking(world, prepare):
    db = world.db
    invoice_id = prepare(world)
    number = columns(db, invoice_id)['InvoiceNumber']
    receivables = bookings_of(db, number)

    storno = cancel(world, invoice_id)

    assert storno.counter_booked is False
    assert bookings_of(db, number) == receivables                            # die Forderung (falls es sie gibt) bleibt
    assert bookings_of(db, storno.storno_number) == []
    assert columns(db, invoice_id)['Status'] == 'cancelled'
    assert columns(db, storno.storno_id)['SumGross'] < 0
    assert db.find_payment_inconsistencies(storno.storno_id) == []


def test_a_small_business_invoice_gets_a_counter_booking_without_tax(world):
    """§ 19 UStG (Satz −1, keine Steuer): Wartekonto 4340, Gegenbuchung ohne Steuerbetrag und Steuersatz."""
    items = [dict(ONE_ITEM[0], taxRate=0)]
    invoice_id = sent(world, number='T-500', items=items, taxRate=0, showTax=False)
    assert columns(world.db, invoice_id)['TaxRate'] == -1 and columns(world.db, invoice_id)['TaxAmount'] == 0

    storno = cancel(world, invoice_id)

    assert storno.counter_booked is True
    assert [(r[1], r[2], r[3], r[4]) for r in bookings_of(world.db, 'T-500')] == [(DEBTORS, 4340, 1000000, None)]
    assert [(r[1], r[2], r[3], r[4]) for r in bookings_of(world.db, storno.storno_number)] == [
        (DEBTORS, 4340, -1000000, None)]
    credit = columns(world.db, storno.storno_id)
    assert (credit['TaxRate'], credit['TaxAmount'], credit['SumGross']) == (-1, 0, -1000000)
    assert balance(world.db, DEBTORS) == 0 and balance(world.db, 4340) == 0
    assert world.db.find_payment_inconsistencies() == []


def test_the_answer_carries_the_hint_only_without_a_counter_booking(world):
    with_receivable = sent(world)
    body = json.loads(handlers.handle_invoice_cancel(
        json.dumps({'invoiceId': with_receivable, 'date': '2026-07-01'}).encode())[1])
    assert body['success'] is True and 'hint' not in body and 'warning' not in body
    imported = _imported_without_receivable(world)
    code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': imported, 'date': '2026-07-01'}).encode())
    body = json.loads(text)
    assert (code, body['success'], body['hint']) == (200, True, NO_COUNTER_BOOKING_HINT)
    assert body['storno_number'] == columns(world.db, body['storno_id'])['InvoiceNumber']


# ── Ablehnungen: nichts wird geschrieben ─────────────────────────────────────

def test_a_second_cancellation_is_refused(world):
    invoice_id = sent(world)
    cancel(world, invoice_id)
    refused(world, invoice_id, message='Die Rechnung ist bereits storniert.')


def test_an_existing_credit_note_is_named_when_the_status_does_not_stop_it_first(world):
    """Steht die Rechnung nicht auf „storniert“ (hier von Hand zurückgesetzt), nennt die Ablehnung die vorhandene
    Stornorechnung; der eindeutige Index auf ``CancelsInvoiceId`` hielte es ohnehin auf."""
    invoice_id = sent(world)
    storno = cancel(world, invoice_id)
    force_status(world.db, invoice_id, 'sent')
    refused(world, invoice_id, message=f'Zu dieser Rechnung gibt es bereits die Stornorechnung {storno.storno_number}.')


def test_a_credit_note_cannot_be_cancelled(world):
    storno = cancel(world, sent(world))
    refused(world, storno.storno_id, message='Eine Stornorechnung lässt sich nicht stornieren.')


@pytest.mark.parametrize('status', ['draft', 'finalized'])
def test_a_draft_is_not_cancelled(world, status):
    invoice_id = make_invoice(world, status, number='T-300')
    refused(world, invoice_id, message='Ein Entwurf wird nicht storniert – er lässt sich löschen, solange er ein '
                                       'Entwurf ist.')


def test_an_already_cancelled_invoice_of_the_old_stock_is_refused(world):
    invoice_id = sent(world)
    world.db.update_invoice_status(invoice_id, 'cancelled')                  # Altbestand: von Hand storniert
    refused(world, invoice_id, message='Die Rechnung ist bereits storniert.')


def test_an_unknown_status_is_refused_not_cancelled(world):
    invoice_id = sent(world)
    force_status(world.db, invoice_id, 'seltsam')
    refused(world, invoice_id)


@pytest.mark.parametrize('gross', [0, -1190000])
def test_an_invoice_without_a_positive_amount_is_refused(world, gross):
    invoice_id = sent(world)
    run_sql(world.db, 'UPDATE Invoices SET SumGross = ? WHERE ID = ?', gross, invoice_id)
    refused(world, invoice_id, message='Eine Rechnung ohne positiven Betrag lässt sich nicht stornieren.')


def test_a_storno_date_before_the_invoice_date_is_refused(world):
    invoice_id = sent(world)                                                  # Rechnungsdatum 2026-06-07
    refused(world, invoice_id, day='2026-06-06', code=400, message=STORNO_DATE_BEFORE_MESSAGE)
    assert cancel(world, invoice_id, '2026-06-07').ok                         # derselbe Tag geht


@pytest.mark.parametrize('day', [None, '', ' ', '2026-7-1', '01.07.2026', '2026-02-30', ' 2026-07-01', '2026-07-01 ',
                                 '20260701', 20260701, '1899-12-31', '2201-01-01', '2026-07-01T00:00:00', [], True])
def test_a_storno_date_that_is_no_iso_date_is_refused(world, day):
    invoice_id = sent(world)
    refused(world, invoice_id, day=day, code=400, message=STORNO_DATE_MESSAGE)


def test_a_quote_an_unknown_id_and_a_foreign_type_are_not_found(world):
    quote = world.db.insert_invoice({
        'invoice_number': '26A001', 'invoice_date': '2026-06-09', 'seller_name': 'S', 'seller_company': 'S GmbH',
        'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19, 'sum_net': 100, 'tax_amount': 19,
        'sum_gross': 119, 'amount_due': 119, 'status': 'sent', 'document_type': 'quote'})
    refused(world, quote, code=404, message='Rechnung nicht gefunden.')
    refused(world, 99999, code=404, message='Rechnung nicht gefunden.')


def test_a_number_circle_without_a_usable_number_refuses_and_writes_nothing(world):
    invoice_id = sent(world)
    run_sql(world.db, "UPDATE NumberRanges SET NumberFormat = '{yy}{l}'")    # ohne Zähler
    storno = refused(world, invoice_id)
    assert 'Zähler' in storno.message


def test_the_cancellation_refuses_when_the_tax_rate_changed_after_the_accounts_were_resolved(world, monkeypatch):
    """Die Konten der Forderung gelten für den Steuersatz, mit dem sie aufgelöst wurden; ändert er sich bis zur
    Transaktion (eine zweite Anfrage, SQL-Konsole), wird abgelehnt statt auf das falsche Wartekonto zu buchen."""
    invoice_id = sent(world)
    original = type(world.db)._receivable_accounts

    def resolving_while_the_rate_changes(self, tax_rate):
        accounts = original(self, tax_rate)
        run_sql(world.db, 'UPDATE Invoices SET TaxRate = 0.07 WHERE ID = ?', invoice_id)
        return accounts
    monkeypatch.setattr(type(world.db), '_receivable_accounts', resolving_while_the_rate_changes)
    before = tables(world.db)
    storno = world.db.cancel_invoice(invoice_id, '2026-07-01', USER)
    assert (storno.ok, storno.code, storno.message) == (False, 409, CHANGED_MESSAGE)
    run_sql(world.db, 'UPDATE Invoices SET TaxRate = 0.19 WHERE ID = ?', invoice_id)
    assert tables(world.db) == before


# ── Ganz oder gar nicht ──────────────────────────────────────────────────────

@pytest.mark.parametrize('table, condition', [
    ('InvoiceItems', ''),                                   # beim Anlegen der Positionen
    ('Bookings', 'WHEN NEW.Amount < 0'),                    # bei der Gegenbuchung
    ('InvoiceCorrections', ''),                             # beim Protokoll
])
def test_a_failure_inside_the_transaction_changes_nothing_and_keeps_the_number(world, table, condition):
    invoice_id = sent(world)
    run_sql(world.db, f"CREATE TRIGGER boom BEFORE INSERT ON {table} {condition} "
                      "BEGIN SELECT RAISE(ABORT, 'Test: nicht erlaubt'); END")
    before, counter_before = tables(world.db), world.db.fetch_number_ranges('invoice')

    storno = world.db.cancel_invoice(invoice_id, '2026-07-01', USER)

    assert (storno.ok, storno.code, storno.message) == (False, 409, STORNO_FAILED_MESSAGE)
    assert tables(world.db) == before                       # auch die Rechnung ist nicht storniert
    assert world.db.fetch_number_ranges('invoice') == counter_before         # die Nummer ist nicht verbraucht
    run_sql(world.db, 'DROP TRIGGER boom')
    assert cancel(world, invoice_id).storno_number == '26R001'               # der nächste Versuch bekommt sie


def test_the_write_lock_is_held_from_the_check_to_the_commit(world, monkeypatch):
    invoice_id = sent(world)
    seen = []
    original = type(world.db)._book_receivable

    def watching(self, *args, **kwargs):
        other = sqlite3.connect(world.db.db_name, timeout=0.1)
        try:
            other.execute('BEGIN IMMEDIATE')
            other.rollback()
            seen.append(False)
        except sqlite3.OperationalError as exc:
            seen.append('locked' in str(exc))
        finally:
            other.close()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(type(world.db), '_book_receivable', watching)
    assert world.db.cancel_invoice(invoice_id, '2026-07-01').ok is True
    assert seen == [True]


def test_two_cancellations_racing_make_one_credit_note(world):
    invoice_id = sent(world)
    barrier = threading.Barrier(2)
    results = [None, None]

    def run(index):
        barrier.wait()
        results[index] = world.db.cancel_invoice(invoice_id, '2026-07-01', USER)
    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.ok for r in results) == [False, True]
    assert len(query_rows(world.db, "SELECT * FROM Invoices WHERE InvoiceKind = 'storno'")) == 1
    assert len(bookings_of(world.db, '26R001')) == 1 and balance(world.db, DEBTORS) == 0


def test_the_unique_index_catches_a_second_credit_note_with_the_same_message(world, monkeypatch):
    """Selbst wenn die Prüfung nichts findet (hier ausgetrickst), hält der eindeutige Index ein zweites Storno auf –
    mit derselben Meldung, ohne etwas zu hinterlassen."""
    invoice_id = sent(world)
    first = cancel(world, invoice_id)
    force_status(world.db, invoice_id, 'sent')
    before = tables(world.db)
    original = type(world.db)._storno_blocker
    monkeypatch.setattr(type(world.db), '_storno_blocker', lambda self, conn, row, day: None if row else original(
        self, conn, row, day))
    storno = world.db.cancel_invoice(invoice_id, '2026-07-02', USER)
    assert (storno.ok, storno.code) == (False, 409)
    assert storno.message == f'Zu dieser Rechnung gibt es bereits die Stornorechnung {first.storno_number}.'
    assert tables(world.db) == before


# ── Die Stornorechnung ist ganz gesperrt ─────────────────────────────────────

@pytest.fixture
def credit_note(world):
    invoice_id = sent(world)
    return invoice_id, cancel(world, invoice_id).storno_id


@pytest.mark.parametrize('status', ['draft', 'finalized', 'sent', 'overdue', 'partial_payment', 'paid', 'cancelled'])
def test_the_status_of_a_credit_note_never_changes(world, credit_note, status):
    before = snapshot(world.db)
    change = world.db.set_invoice_status(credit_note[1], status)
    assert (change.ok, change.code, change.message) == (False, 409, STORNO_STATUS_MESSAGE)
    assert snapshot(world.db) == before
    code, body = set_invoice_status(credit_note[1], status)
    assert (code, body['error']) == (409, STORNO_STATUS_MESSAGE)


def _full_form(world, invoice_id, **over):
    """Das Formular der Stornorechnung, wie die Maske es ungeändert zurückschickt (mit Positionen)."""
    row = columns(world.db, invoice_id)
    payload = make_payload(world, invoiceId=invoice_id, invoiceNumber=row['InvoiceNumber'],
                           invoiceDate=row['InvoiceDate'], paymentTerms=row['PaymentTerms'], dueDate=None,
                           discountPercentage=None, discountDays=None, orderNumber=row['OrderNumber'],
                           buyerReference=row['BuyerReference'], deliveryDate=row['DeliveryDate'],
                           items=[dict(ONE_ITEM[0], quantity=-1.0)])
    payload.update(over)
    return payload


OPEN_CHANGES = [{'orderNumber': 'B-99'}, {'paymentTerms': 'Zahlbar sofort'}, {'deliveryDate': '2026-06-01'},
                {'buyerReference': '99-X'}]                      # die Felder, die bei einer versendeten Rechnung offen sind
FORM_CHANGES = [{'invoiceNumber': '26R999'}, {'customerId': 4}, {'items': [dict(ONE_ITEM[0], quantity=-2.0)]},
                {'invoiceDate': '2026-06-08'}]


@pytest.mark.parametrize('change, full_form', [(c, False) for c in OPEN_CHANGES] + [(c, True) for c in FORM_CHANGES],
                         ids=[next(iter(c)) for c in OPEN_CHANGES + FORM_CHANGES])
def test_saving_a_change_to_a_credit_note_is_refused(world, credit_note, change, full_form):
    """Jede Abweichung – auch in den Feldern, die eine versendete Rechnung berichtigen darf – ergibt 409 mit
    demselben Text; geschrieben wird nichts."""
    storno_id = credit_note[1]
    before, documents = snapshot(world.db), query_rows(world.db, 'SELECT PDFPath FROM Invoices')
    payload = (_full_form(world, storno_id, **change) if full_form
               else {'invoiceId': storno_id, 'documentVersion': 1, **change})
    resp = save_raw(payload)
    assert resp.status == 409 and json.loads(resp) == {'success': False, 'error': STORNO_LOCKED_MESSAGE}
    assert snapshot(world.db) == before and query_rows(world.db, 'SELECT PDFPath FROM Invoices') == documents


def test_saving_the_credit_note_unchanged_changes_nothing(world, credit_note):
    storno_id = credit_note[1]
    before = snapshot(world.db)
    assert save({'invoiceId': storno_id, 'documentVersion': 1}) == {'success': True, 'invoice_id': storno_id}
    assert save(_full_form(world, storno_id)) == {'success': True, 'invoice_id': storno_id}
    assert snapshot(world.db) == before                                       # keine Spalte, keine Fassung, kein Protokoll


def test_the_database_layer_refuses_to_correct_or_update_a_credit_note(world, credit_note):
    storno_id = credit_note[1]
    before = snapshot(world.db)
    with pytest.raises(InvoiceLocked) as refused_correction:
        world.db.correct_invoice(storno_id, {'order_number': 'B-99'}, 1, USER)
    assert str(refused_correction.value) == STORNO_LOCKED_MESSAGE
    with pytest.raises(InvoiceLocked) as refused_update, world.db.transaction() as tx:
        world.db.update_invoice_fields(tx, storno_id, {'payment_terms': 'Zahlbar sofort'}, correcting=True)
    assert str(refused_update.value) == STORNO_LOCKED_MESSAGE
    assert world.db.correct_invoice(storno_id, {'order_number': columns(world.db, storno_id)['OrderNumber']},
                                    1, USER).corrected is False
    assert snapshot(world.db) == before


def test_a_credit_note_cannot_be_deleted(world, credit_note):
    storno_id = credit_note[1]
    before = snapshot(world.db)
    with pytest.raises(InvoiceNotDeletable):
        world.db.delete_draft_invoice(storno_id)
    assert handlers.handle_delete_invoice(world.db, storno_id)[0] == 303
    assert snapshot(world.db) == before and columns(world.db, storno_id)['InvoiceKind'] == 'storno'


def test_no_payment_can_be_assigned_to_a_credit_note(world, credit_note):
    storno_id = credit_note[1]
    booking = world.db.insert_booking('2026-07-02', 119.0, booking_type='bank', account_id=world.bank)
    before = snapshot(world.db)
    assigned = world.db.assign_invoice_payment(storno_id, booking)
    assert (assigned.ok, assigned.message) == (False, STORNO_PAYMENT_MESSAGE)
    assert world.db.payment_precheck(storno_id, world.bank) == STORNO_PAYMENT_MESSAGE
    ok, error = handlers.link_booking_to_invoice_capped(world.db, storno_id, booking, 119.0)
    assert (ok, error) == (False, STORNO_PAYMENT_MESSAGE)
    with pytest.raises(ValueError, match='Stornorechnung'):
        world.db.link_invoice_to_transaction(storno_id, booking, -119.0)
    assert snapshot(world.db) == before


def test_the_payment_logic_never_changes_the_status_of_a_credit_note(world, credit_note):
    storno_id = credit_note[1]
    world.db.recalc_invoice_payment_state(storno_id)                           # adjust_status=True
    assert (columns(world.db, storno_id)['Status'], columns(world.db, storno_id)['AmountDue']) == ('sent', -1190000)
    booking = world.db.insert_booking('2026-07-02', -119.0, booking_type='bank', account_id=world.bank)
    run_sql(world.db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) '
                      "VALUES (?, ?, -1190000, '2026-07-02')", storno_id, booking)   # wie ein Import es hinterließe
    world.db.recalc_invoice_payment_state(storno_id)
    assert columns(world.db, storno_id)['Status'] == 'sent'                    # nie „bezahlt“
    assert columns(world.db, storno_id)['AmountDue'] == 0                      # der Rest wird trotzdem gerechnet


def test_a_credit_note_is_no_candidate_for_a_payment_and_no_open_item(world):
    db = world.db
    invoice_id = sent(world, invoiceDate='2026-01-01', dueDate='2026-01-15')
    storno = cancel(world, invoice_id, '2026-01-02')
    # selbst ein verfälschter Stand (Fälligkeit, positiver Rest) macht sie nicht zum Kandidaten
    run_sql(db, 'UPDATE Invoices SET PaymentDueDate = ?, AmountDue = 1190000 WHERE ID = ?', '2026-01-15',
            storno.storno_id)
    assert storno.storno_id not in [r[0] for r in db.get_open_invoices()]
    assert storno.storno_id not in [r[0] for r in db.get_overdue_invoices()]
    assert storno.storno_id not in [r[0] for r in db.get_invoices_due_soon(days=100000)]
    assert [r[0] for r in db.get_open_items_for_contact(world.cust)['open_invoices']] == []


def test_the_original_of_a_credit_note_is_no_open_item_either(world):
    db = world.db
    invoice_id = sent(world, invoiceDate='2026-01-01', dueDate='2026-01-15')
    assert [r[0] for r in db.get_open_items_for_contact(world.cust)['open_invoices']] == [invoice_id]
    assert 'T-100' in PageReminders(db)                                        # überfällig: auf der Mahnseite
    storno = cancel(world, invoice_id, '2026-01-02')
    items = db.get_open_items_for_contact(world.cust)
    assert items['open_invoices'] == [] and items['saldo'] == 0
    assert db.get_open_invoices() == [] and db.get_overdue_invoices() == []
    reminders = PageReminders(db)
    assert 'T-100' not in reminders and storno.storno_number not in reminders      # weder Rechnung noch Stornorechnung


def test_the_receivables_of_a_credit_note_pair_are_never_removed(world):
    db = world.db
    invoice_id = sent(world)
    storno = cancel(world, invoice_id)
    receivables = bookings_of(db, 'T-100') + bookings_of(db, '26R001')
    assert db.remove_receivable_booking(invoice_id) is False
    assert db.remove_receivable_booking(storno.storno_id) is False
    with db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        assert db._drop_receivable(tx.conn, invoice_id, 'T-100') is False
        assert db._drop_receivable(tx.conn, storno.storno_id, '26R001') is False
    assert bookings_of(db, 'T-100') + bookings_of(db, '26R001') == receivables
    # was ohne Stornorechnung gilt, bleibt: Die stornierte Rechnung des Altbestands verliert ihre Forderung
    old = sent(world, number='T-101')
    db.update_invoice_status(old, 'cancelled')
    assert db.remove_receivable_booking(old) is True and bookings_of(db, 'T-101') == []


# ── Übergänge ────────────────────────────────────────────────────────────────

def test_a_cancelled_invoice_with_a_credit_note_cannot_be_revived(world, credit_note):
    invoice_id = credit_note[0]
    before = snapshot(world.db)
    for target in ('sent', 'overdue'):
        change = world.db.set_invoice_status(invoice_id, target)
        assert (change.ok, change.code, change.message) == (False, 409, HAS_STORNO_MESSAGE)
        assert set_invoice_status(invoice_id, target)[1]['error'] == 'Zu dieser Rechnung gibt es eine Stornorechnung – ' \
                                                                     'sie bleibt storniert.'
    assert snapshot(world.db) == before and allowed_targets('cancelled', 0, True) == []


def test_a_cancelled_invoice_without_a_credit_note_can_still_be_revived(world):
    invoice_id = sent(world)
    world.db.update_invoice_status(invoice_id, 'cancelled')                   # Altbestand
    assert allowed_targets('cancelled', 0, False) == ['sent', 'overdue']
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    assert columns(world.db, invoice_id)['Status'] == 'sent'


def _status_options(html):
    """Die Optionen der Statusauswahl einer Seite (leer, wenn die Seite keine hat)."""
    if 'id="statusChangeSelect"' not in html:
        return []
    return re.findall(r'value="(\w+)"', html.split('id="statusChangeSelect"')[1].split('</select>')[0])


def test_the_status_select_of_the_pages_never_offers_cancelled(world, credit_note):
    assert _status_options(PageInvoice(world.db, {}, credit_note[0])) == ['cancelled']      # bleibt storniert
    assert _status_options(PageInvoice(world.db, {}, credit_note[1])) == []                 # keine Auswahl
    open_invoice = sent(world, number='T-101')
    assert _status_options(PageInvoice(world.db, {}, open_invoice)) == ['sent', 'draft', 'finalized', 'overdue']


# ── Seiten und Liste ─────────────────────────────────────────────────────────

def test_the_pages_of_the_pair_link_to_each_other_and_are_locked(world, credit_note):
    invoice_id, storno_id = credit_note
    number = columns(world.db, storno_id)['InvoiceNumber']
    original_page = PageInvoice(world.db, {}, invoice_id)
    credit_page = PageInvoice(world.db, {}, storno_id)

    assert f'Storniert durch Stornorechnung <a href="/invoice?id={storno_id}">{number}</a> vom 01.07.2026' in original_page
    assert f'Stornorechnung zu Rechnung <a href="/invoice?id={invoice_id}">T-100</a> vom 07.06.2026' in credit_page
    assert 'Stornorechnung 26R001' in credit_page and STORNO_LOCKED_MESSAGE in credit_page
    dom = Dom(credit_page)
    assert dom.one('input', id='invoice_locked')['value'] == 'true'
    for element_id in ('invoice_date', 'invoice_number', 'customer_select', 'delivery_date', 'payment_terms',
                       'order_number', 'buyer_route_id'):
        assert 'disabled' in [a for t, a in dom.tags if a.get('id') == element_id][0], element_id
    buttons = [a.get('onclick') for t, a in dom.tags if t == 'button']
    assert 'saveInvoice()' not in buttons                                      # kein Berichtigen
    assert not any('copyInvoiceAsNew' in (b or '') or 'cancelInvoice' in (b or '') for b in buttons)
    assert 'setInvoiceStatus' not in ''.join(b or '' for b in buttons) and 'statusChangeSelect"' not in credit_page
    assert 'generatePDF()' in buttons and 'downloadPDF()' in buttons          # Erzeugen und Download funktionieren
    assert 'Einer Stornorechnung lässt sich keine Zahlung zuordnen.' in credit_page
    assert '/transactions?from_invoice=' not in credit_page
    # die Stornorechnung zeigt ihre negativen Beträge
    assert 'class="invoice-title">Stornorechnung</h1>' in credit_page                  # Überschrift der Seite
    assert 'class="invoice-title">Rechnung</h1>' in original_page
    items = [a['value'] for a in dom.all('input') if 'item-quantity' in (a.get('class') or '')]
    assert items == ['-1.0']                                                           # die negative Menge
    assert 'Noch offen' not in credit_page and 'Zahlung verbuchen' not in credit_page


def test_only_a_sent_invoice_offers_cancelling_and_the_dialog_has_a_date_field(world):
    draft = make_invoice(world, 'draft', number='T-400')
    assert 'cancelInvoice(' not in PageInvoice(world.db, {}, draft)
    invoice_id = sent(world)
    html = PageInvoice(world.db, {}, invoice_id)
    assert f'onclick="cancelInvoice({invoice_id})"' in html and 'Stornieren &hellip;' in html
    script = next(s for s in Dom(html).scripts if 'function cancelInvoice(' in s)
    assert "fetch('/invoice/cancel'" in script and 'invoiceId: invId, date: day' in script
    assert json.dumps(cancel_question('T-100'), ensure_ascii=False) in script        # die Rückfrage mit der Nummer
    assert '"min": "2026-06-07"' in script and '"label": "Stornodatum:"' in script        # nicht vor dem Rechnungsdatum
    assert cancel_question('T-100') == ('Rechnung T-100 stornieren? Es entsteht eine Stornorechnung mit eigener '
                                        'Nummer, die dem Kunden zugehen muss. Das lässt sich nicht rückgängig machen.')
    assert f'"value": "{max(datetime.date.today().isoformat(), "2026-06-07")}"' in script       # vorbelegt mit heute
    for status in ('overdue', 'partial_payment', 'paid'):
        force_status(world.db, invoice_id, status)
        assert 'cancelInvoice(' in PageInvoice(world.db, {}, invoice_id)
    force_status(world.db, invoice_id, 'cancelled')
    assert 'cancelInvoice(' not in PageInvoice(world.db, {}, invoice_id)


def test_the_list_marks_credit_notes_and_leaves_them_out_of_the_sums(world, credit_note):
    html = PageInvoice(world.db, {'date_from': '2000-01-01', 'date_to': '2100-12-31'})
    rows = [[cell for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)] for row in html.split('<tr>')[2:]]
    by_number = {re.sub('<.*?>', '', r[0]): r for r in rows if r}
    credit = by_number['26R001']
    assert STORNO_LABEL in credit[5] and 'Versendet' not in credit[5]          # statt des Status
    assert '-119,00 €' in re.sub('<.*?>', '', credit[4]) and '-100,00 €' in credit[3]
    assert 'Storniert' in by_number['T-100'][5]
    stats = ' '.join(re.search(r'<strong>Statistik:</strong>(.*?)</div>', html, re.S).group(1).split())
    assert stats == ('1 Rechnung(en) | Gesamtsumme: 119,00 € | Bezahlt: 0,00 € | Offen: 0,00 € | '
                     'Stornorechnungen (nicht mitgezählt): 1')


def test_a_customer_and_a_number_with_markup_are_escaped_everywhere(world):
    hostile = '<script>alert(1)</script>'
    invoice_id = sent(world, number='T-<b>')
    run_sql(world.db, 'UPDATE Invoices SET BuyerCompany = ?, BuyerName = ? WHERE ID = ?', hostile, hostile, invoice_id)
    storno = cancel(world, invoice_id)
    pages = {'Liste': PageInvoice(world.db, {'date_from': '2000-01-01', 'date_to': '2100-12-31'}),
             'Rechnung': PageInvoice(world.db, {}, invoice_id),
             'Stornorechnung': PageInvoice(world.db, {}, storno.storno_id)}
    for name, html in pages.items():
        assert hostile not in html and 'T-<b>' not in html, name
        assert 'alert(1)' not in ''.join(Dom(html).scripts).replace('alert(1)\\u003c', ''), name
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in pages['Liste']
    assert 'Storniert durch Stornorechnung' in pages['Rechnung']
    # die Rückfrage trägt die Nummer als Text im Skript
    other = sent(world, number='T-<i>x')
    script = next(s for s in Dom(PageInvoice(world.db, {}, other)).scripts if 'function cancelInvoice(' in s)
    assert '<i>' not in script and 'T-\\u003ci\\u003ex' in script


def test_copying_a_credit_note_gives_an_empty_form_with_a_hint(world, credit_note):
    storno_id = credit_note[1]
    page = PageInvoice(world.db, {}, None, copy_from=storno_id)
    dom = Dom(page)
    assert app_msg_arguments(dom) == [STORNO_COPY_MESSAGE, 'error']
    assert dom.one('input', id='is_edit_mode')['value'] == 'false' and dom.one('input', id='invoice_id')['value'] == ''
    assert 'Neue Rechnung erstellen' in page and 'Kopie von' not in page
    assert 'value="Beratung"' not in page                                      # keine Position der Stornorechnung
    address = re.search(r'<div id="customer_address_display"[^>]*>(.*?)</div>', page, re.S).group(1)
    assert address == ''                                                       # kein Kunde vorbelegt
    plain = PageInvoice(world.db, {}, None, copy_from=credit_note[0])           # eine gewöhnliche Rechnung lässt sich kopieren
    assert 'Kopie von T-100' in plain
    assert STORNO_COPY_MESSAGE == 'Eine Stornorechnung lässt sich nicht kopieren.'


# ── Die Skripte der Seiten ───────────────────────────────────────────────────

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die statischen Prüfungen oben decken '
                                                  'die Seite ab')


def _node(tmp_path, source, runner, scenario=None):
    """``runner`` (ein Node-Skript) führt ``source`` mit Attrappen für das DOM aus und gibt sein Protokoll als JSON aus."""
    (tmp_path / 'source.js').write_text(source, encoding='utf-8')
    (tmp_path / 'run.js').write_text(runner, encoding='utf-8')
    done = subprocess.run([NODE, str(tmp_path / 'run.js'), str(tmp_path / 'source.js'), json.dumps(scenario)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@NO_NODE
def test_every_script_block_of_the_pages_parses(world, credit_note, tmp_path):
    pages = [PageInvoice(world.db, {}, credit_note[0]), PageInvoice(world.db, {}, credit_note[1]),
             PageInvoice(world.db, {}, sent(world, number='T-101')),
             PageInvoice(world.db, {'date_from': '2000-01-01', 'date_to': '2100-12-31'}), Footer()]
    for number, html in enumerate(pages):
        for index, block in enumerate(Dom(html).scripts if number < 4 else [Footer().split('<script>')[1]]):
            source = tmp_path / f'page{number}_{index}.js'
            source.write_text(block.split('</script>')[0], encoding='utf-8')
            done = subprocess.run([NODE, '--check', str(source)], capture_output=True, text=True, timeout=60)
            assert done.returncode == 0, f'Seite {number}, Block {index}: {done.stderr}'


CANCEL_RUNNER = r"""
const fs = require('fs'), vm = require('vm');
const scenario = JSON.parse(process.argv[3]);
const log = {asked: [], requests: [], messages: [], redirects: [], timers: []};
const location = {};
Object.defineProperty(location, 'href', {set(v) { log.redirects.push(v); }});
const sandbox = {JSON, window: {location},
  appConfirm: (text, onOk, field) => { log.asked.push({text, field}); onOk(scenario.day); },
  fetch: (url, init) => { log.requests.push({url, init});
    return scenario.fail ? Promise.reject(new Error('Netz weg'))
                         : Promise.resolve({json: () => Promise.resolve(scenario.answer)}); },
  showMessage: (text, type) => log.messages.push([text, type]),
  setTimeout: (fn, ms) => { log.timers.push(ms); fn(); }};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8') + '\ncancelInvoice(7);', sandbox);
setTimeout(() => console.log(JSON.stringify(log)), 50);
"""


def _cancel_script(world, invoice_id):
    return next(s for s in Dom(PageInvoice(world.db, {}, invoice_id)).scripts if 'function cancelInvoice(' in s)


@NO_NODE
@pytest.mark.parametrize('answer, fail, redirects, messages', [
    ({'success': True, 'storno_id': 9, 'storno_number': '26R001'}, False, ['/invoice?id=9'], []),
    ({'success': True, 'storno_id': 9, 'hint': 'Hinweis'}, False, ['/invoice?id=9'], [['Hinweis', 'info']]),
    ({'success': True, 'storno_id': 9, 'warning': 'Warnung', 'hint': 'Hinweis'}, False, ['/invoice?id=9'],
     [['Warnung Hinweis', 'warn']]),
    ({'success': False, 'error': 'Die Rechnung ist bereits storniert.'}, False, [],
     [['Fehler: Die Rechnung ist bereits storniert.', 'error']]),
    (None, True, [], [['Fehler: Netz weg', 'error']]),
], ids=['Erfolg', 'Hinweis', 'Warnung-und-Hinweis', 'Ablehnung', 'Netzfehler'])
def test_the_cancel_script_asks_posts_and_follows_the_answer(world, tmp_path, answer, fail, redirects, messages):
    invoice_id = sent(world)
    log = _node(tmp_path, _cancel_script(world, invoice_id), CANCEL_RUNNER,
                {'day': '2026-07-02', 'answer': answer, 'fail': fail})
    today = datetime.date.today().isoformat()
    assert log['asked'] == [{'text': cancel_question('T-100'), 'field': {
        'label': 'Stornodatum:', 'min': '2026-06-07', 'value': max(today, '2026-06-07'),
        'error': 'Bitte ein Stornodatum angeben, das nicht vor dem Rechnungsdatum liegt.'}}]
    (request,) = log['requests']
    assert request['url'] == '/invoice/cancel' and request['init']['method'] == 'POST'
    assert json.loads(request['init']['body']) == {'invoiceId': 7, 'date': '2026-07-02'}
    assert log['redirects'] == redirects and log['messages'] == messages
    assert log['timers'] == ([3500] if redirects and messages else [])


APP_CONFIRM_RUNNER = r"""
const fs = require('fs'), vm = require('vm');
const scenario = JSON.parse(process.argv[3]);
const log = {ok: []};
const element = () => ({style: {}, hidden: true, value: '', min: '', textContent: ''});
const elements = {};
['app_confirm_text', 'app_confirm_date_row', 'app_confirm_date', 'app_confirm_date_label', 'app_confirm_date_error',
 'app_confirm_ok', 'app_confirm'].forEach(id => { elements[id] = element(); });
const sandbox = {document: {getElementById: id => elements[id]}};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
const onOk = (value) => log.ok.push(value === undefined ? null : value);
vm.runInContext('appConfirm', sandbox)('Frage', onOk, scenario.field);
log.row = {hidden: elements.app_confirm_date_row.hidden, label: elements.app_confirm_date_label.textContent,
           min: elements.app_confirm_date.min, value: elements.app_confirm_date.value};
elements.app_confirm_date.value = scenario.entered;
elements.app_confirm_ok.onclick();
log.display = elements.app_confirm.style.display;
log.problem = elements.app_confirm_date_error.textContent;
console.log(JSON.stringify(log));
"""


@NO_NODE
@pytest.mark.parametrize('field, entered, ok, display, problem', [
    (None, '', [None], 'none', ''),                                           # ohne Datumsfeld wie bisher
    ({'label': 'Stornodatum:', 'min': '2026-06-07', 'value': '2026-07-01', 'error': 'FEHLER'}, '2026-07-02',
     ['2026-07-02'], 'none', ''),
    ({'label': 'Stornodatum:', 'min': '2026-06-07', 'value': '2026-07-01', 'error': 'FEHLER'}, '2026-06-07',
     ['2026-06-07'], 'none', ''),                                             # der Tag des Rechnungsdatums geht
    ({'label': 'Stornodatum:', 'min': '2026-06-07', 'value': '2026-07-01', 'error': 'FEHLER'}, '2026-06-06', [],
     'flex', 'FEHLER'),                                                       # zu früh: der Dialog bleibt offen
    ({'label': 'Stornodatum:', 'min': '2026-06-07', 'value': '2026-07-01', 'error': 'FEHLER'}, '', [], 'flex',
     'FEHLER'),
], ids=['ohne-Datum', 'gueltig', 'am-Rechnungstag', 'zu-frueh', 'leer'])
def test_the_confirm_dialog_checks_the_date_before_it_closes(tmp_path, field, entered, ok, display, problem):
    footer = Footer().split('<script>')[1].split('</script>')[0]
    source = footer[footer.index('function appConfirm('):footer.index('function appConfirmHref(')]
    log = _node(tmp_path, source, APP_CONFIRM_RUNNER, {'field': field, 'entered': entered})
    assert log['ok'] == ok and log['problem'] == problem
    assert log['display'] == display
    if field:
        assert log['row'] == {'hidden': False, 'label': 'Stornodatum:', 'min': '2026-06-07', 'value': '2026-07-01'}
    else:
        assert log['row']['hidden'] is True


# ── Über den Server ──────────────────────────────────────────────────────────

@pytest.fixture
def books(auto_login):
    w = build_world(auto_login.db)
    w.csrf = auto_login.csrf
    w.db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return w


def sent_over_http(port, w, number='T-100'):
    resp = post_json(port, '/invoice/save', make_payload(w, invoiceNumber=number, items=ONE_ITEM), w.csrf)
    invoice_id = json.loads(resp.text)['invoice_id']
    status = post_json(port, '/invoice/status', {'invoice_id': invoice_id, 'status': 'sent'}, w.csrf)
    assert status.status == 200, status.text
    return invoice_id


def test_over_http_the_route_cancels_and_answers_with_the_new_credit_note(server_port, books):
    invoice_id = sent_over_http(server_port, books)
    resp = post_json(server_port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-01'}, books.csrf)
    body = json.loads(resp.text)
    assert resp.status == 200 and resp.headers['Content-Type'].startswith('application/json')
    assert body == {'success': True, 'storno_id': body['storno_id'], 'storno_number': '26R001'}
    assert columns(books.db, invoice_id)['Status'] == 'cancelled'
    credit = columns(books.db, body['storno_id'])
    assert (credit['InvoiceKind'], credit['SumGross'], credit['PDFPath'] is not None) == ('storno', -1190000, True)
    page = get(server_port, f"/invoice?id={body['storno_id']}")
    assert page.status == 200 and 'Stornorechnung zu Rechnung' in page.text
    assert [r['UserName'] for r in protocol(books.db, invoice_id, 'status')][-1] == 'admin'


def test_over_http_a_refusal_carries_its_status_and_text_and_writes_nothing(server_port, books):
    invoice_id = sent_over_http(server_port, books)
    before = tables(books.db)
    for payload, status, error in (
            ({'invoiceId': invoice_id, 'date': '2026-06-01'}, 400, STORNO_DATE_BEFORE_MESSAGE),
            ({'invoiceId': invoice_id, 'date': 'morgen'}, 400, STORNO_DATE_MESSAGE),
            ({'invoiceId': invoice_id}, 400, STORNO_DATE_MESSAGE),
            ({'invoiceId': 99999, 'date': '2026-07-01'}, 404, 'Rechnung nicht gefunden.')):
        resp = post_json(server_port, '/invoice/cancel', payload, books.csrf)
        assert (resp.status, json.loads(resp.text)) == (status, {'success': False, 'error': error}), payload
    assert tables(books.db) == before


@pytest.mark.parametrize('bad_id', ['"01"', '1.5', 'true', '"abc"', '[1]', '"1.0"', '" 1"', '""', '{"a": 1}'])
def test_over_http_the_invoice_id_must_be_a_whole_number(server_port, books, bad_id):
    sent_over_http(server_port, books)
    before = tables(books.db)
    body = '{"invoiceId": %s, "date": "2026-07-01"}' % bad_id
    resp = post_json(server_port, '/invoice/cancel', body, books.csrf)
    assert resp.status == 400 and json.loads(resp.text)['success'] is False
    assert tables(books.db) == before


@pytest.mark.parametrize('written', ['1', '1.0', '"1"'])
def test_over_http_a_whole_number_counts_in_every_notation(server_port, books, written):
    sent_over_http(server_port, books)
    resp = post_json(server_port, '/invoice/cancel', '{"invoiceId": %s, "date": "2026-07-01"}' % written, books.csrf)
    assert resp.status == 200 and json.loads(resp.text)['success'] is True


def test_over_http_get_is_405_and_cancels_nothing(server_port, books):
    invoice_id = sent_over_http(server_port, books)
    before = tables(books.db)
    for path in ('/invoice/cancel', f'/invoice/cancel?invoiceId={invoice_id}&date=2026-07-01',
                 f'/invoice/cancel?id={invoice_id}'):
        resp = get(server_port, path)
        assert resp.status == 405 and resp.headers['Allow'] == 'POST', path
    assert tables(books.db) == before


def test_over_http_the_route_needs_origin_and_token(server_port, books):
    invoice_id = sent_over_http(server_port, books)
    before = tables(books.db)
    payload = {'invoiceId': invoice_id, 'date': '2026-07-01'}
    assert post_json(server_port, '/invoice/cancel', payload, books.csrf,
                     headers={'Origin': FOREIGN_ORIGIN}).status == 403
    assert post_json(server_port, '/invoice/cancel', payload, 'falsch').status == 403
    assert post_json(server_port, '/invoice/cancel', payload, '').status == 403
    assert tables(books.db) == before


def test_over_http_a_failing_document_keeps_the_credit_note_and_warns(server_port, books, monkeypatch):
    import export.invoice_files as files
    invoice_id = sent_over_http(server_port, books)

    def broken(db, invoice_id, with_xml=None, overwrite_sent=False):
        raise RuntimeError('Datei nicht schreibbar /home/niemand')
    monkeypatch.setattr(files, 'generate_invoice_documents', broken)

    resp = post_json(server_port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-01'}, books.csrf)

    body = json.loads(resp.text)
    assert resp.status == 200 and body['success'] is True and 'PDF + E-Rechnung' in body['warning']
    assert 'niemand' not in resp.text                                          # nichts von innen nach außen
    assert columns(books.db, invoice_id)['Status'] == 'cancelled'
    assert columns(books.db, body['storno_id'])['PDFPath'] is None             # „PDF + E-Rechnung“ erzeugt es nach


def test_the_credit_note_can_be_generated_afterwards_and_downloaded(server_port, books, monkeypatch):
    import export.invoice_files as files
    from tests.document_helpers import post_action
    invoice_id = sent_over_http(server_port, books)
    real = files.generate_invoice_documents
    monkeypatch.setattr(files, 'generate_invoice_documents', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    storno_id = json.loads(post_json(server_port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-01'},
                                     books.csrf).text)['storno_id']
    assert get(server_port, f'/invoice/pdf_download?id={storno_id}').status == 404       # noch keine Datei
    monkeypatch.setattr(files, 'generate_invoice_documents', real)
    done = post_action(server_port, f'/invoice/pdf_generate?id={storno_id}', books.csrf)
    assert done.status == 200 and json.loads(done.text)['success'] is True
    download = get(server_port, f'/invoice/pdf_download?id={storno_id}')
    assert download.status == 200 and download.body.startswith(b'%PDF')
