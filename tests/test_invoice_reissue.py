# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Storno-Folgen (2.2.2): Neu ausstellen, Umzug der Zahlungen, Guthaben, nachträgliche Stornorechnung, Prüfliste.

* **Neu ausstellen** (``Database.create_replacement_draft``, ``POST /invoice/reissue``, ``reissue`` in
  ``/invoice/cancel``): Zu einer stornierten Rechnung mit Stornorechnung legt die App den Entwurf einer neuen Rechnung an
  (``ReplacesInvoiceId``) – höchstens einen, mit neuer Nummer, heutigem Datum und verschobener Fälligkeit.
* **Zahlungen ziehen um**: Beim Versenden der Ersatzrechnung gehen die Zahlungen der stornierten Rechnung in der
  Transaktion des Statuswechsels auf sie über (``_move_replaced_payments``); was sich nicht eindeutig umschreiben lässt,
  bleibt, und der Hinweis nennt es.
* **Hinweis im Dokument**, **Guthaben** des Storno-Paares (Zahlungen auf die stornierte Rechnung), die **nachträgliche
  Stornorechnung** für eine von Hand stornierte Rechnung und zwei neue Kategorien der **Prüfliste**.

Das Beispiel: Rechnung R1 über 119,00 € (19 %), 50,00 € gezahlt, storniert (Stornorechnung S1), Ersatzrechnung R2.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import datetime
import json
import re
import sqlite3
import threading
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

import export.invoice_files as invoice_files
import server.handlers as handlers
from db.invoice_lock import CHANGED_MESSAGE
from db.invoice_payments import STORNO_PAYMENT_MESSAGE, PaymentAssignment
from db.invoice_storno import (NO_COUNTER_BOOKING_HINT, REISSUE_FAILED_MESSAGE, REISSUE_NO_STORNO_MESSAGE,
                               REISSUE_NOT_CANCELLED_MESSAGE, REISSUE_STORNO_MESSAGE, Reissue)
from db.numbering import NumberRangeError
from export.cii_invoice import generate_cii
from export.einvoice_model import build_einvoice, replacement_note, replacement_note_text
from export.xrechnung_invoice import XRechnungGenerator
from server.pages_contacts import PageContacts
from server.pages_invoice import (PageInvoice, cancel_question, late_cancel_question, new_draft_question,
                                  reissue_question)
from tests.document_helpers import post_json, read_file
from tests.dom_helpers import Dom
from tests.http_client import FOREIGN_ORIGIN, get
from tests.invoice_world import (add_payment, columns, make_invoice, make_payload, protocol, query_rows, run_sql, save,
                                 save_open, set_invoice_status, snapshot)
from tests.test_invoice_storno import (DEBTORS, NO_NODE, ONE_ITEM, USER, WAITING, _node, at, balance,  # noqa: F401
                                       bookings_of, books, cancel, euer, items_of, pay, sent, sent_over_http,
                                       tables, world)
from tests.test_invoice_storno_documents import CII, UBL, embedded_xml, pdf_text, text_of

TODAY = '2026-11-12'              # der Tag, den die Tests dem Server als „heute“ geben
CUSTOMER_TEXT = 'Malermeister Pinsel'


# ── Hilfen ───────────────────────────────────────────────────────────────────

def reissue(db, original_id, monkeypatch, day=TODAY):
    """Zur stornierten Rechnung die Ersatzrechnung anlegen; liefert die ID des Entwurfs."""
    at(monkeypatch, day)
    result = db.create_replacement_draft(original_id, USER)
    assert result.ok is True, result
    return result.invoice_id


def edit_draft(w, invoice_id, items, **over):
    """Positionen (und Kopf) eines Entwurfs ändern, wie die Maske es tut."""
    row = columns(w.db, invoice_id)
    payload = make_payload(w, invoiceId=invoice_id, invoiceNumber=row['InvoiceNumber'], invoiceDate=row['InvoiceDate'],
                           deliveryDate=row['DeliveryDate'], dueDate=row['PaymentDueDate'], items=items, **over)
    assert save(payload)['success'] is True


def paid_scenario(w, monkeypatch, paid=50.0, number='T-100', **over):
    """R1 über 119,00 € versendet, ``paid`` gezahlt (am 01.07.), storniert (02.07.) und die Ersatzrechnung R2 als Entwurf."""
    r1 = sent(w, number, **over)
    booking = pay(w, r1, paid) if paid else None
    s1 = cancel(w, r1, '2026-07-02')
    r2 = reissue(w.db, r1, monkeypatch)
    return SimpleNamespace(r1=r1, s1=s1.storno_id, r2=r2, booking=booking, w=w)


def pay_after_cancel(w, invoice_id, amount):
    """Eine Zahlung auf die schon stornierte Rechnung (wie sie ein Altbestand oder der Rest eines Umzugs hinterlässt)."""
    booking = w.db.insert_booking('2026-07-05', amount, booking_type='bank', account_id=w.bank,
                                  recipient_client=CUSTOMER_TEXT)
    run_sql(w.db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, '
                  "'2026-07-05')", invoice_id, booking, int(amount * 10000))
    return booking


def ledger(db):
    """Salden der Konten, die ein Fall berührt (Minor Units, Soll minus Haben)."""
    return {number: balance(db, number) for number in (DEBTORS, WAITING, 4345, 4400, 4300)}


def standing(db, *ids):
    """(Status, offener Rest) je Beleg, in Minor Units."""
    return [(columns(db, i)['Status'], columns(db, i)['AmountDue']) for i in ids]


def allocations(db, invoice_id):
    """Die Zuordnungen einer Rechnung als ``[(Bewegung, Betrag, Datum)]``."""
    return [(r['BookingID'], r['Amount'], r['PaymentDate']) for r in query_rows(
        db, 'SELECT * FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID', invoice_id)]


def payment_rows(db, invoice_number):
    """Die markierten Sätze einer Zuordnung zur Rechnungsnummer: (Konto, Gegenkonto, Betrag, Steuer, Datum, Form)."""
    return [(r['coa'], r['counter'], r['Amount'], r['TaxAmount'], r['DateBooking'], r['EntryForm']) for r in query_rows(
        db, 'SELECT c.AccountNumber AS coa, g.AccountNumber AS counter, b.Amount, b.TaxAmount, b.DateBooking, '
            'b.EntryForm FROM Bookings b LEFT JOIN ChartOfAccounts c ON c.ID = b.COA_ID '
            'LEFT JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID '
            "WHERE b.DocumentNumber = ? AND b.Status = 'invoice_payment' ORDER BY b.ID", invoice_number)]


# ═════════════════════════════════════════════════════════════════════════════
# Neu ausstellen
# ═════════════════════════════════════════════════════════════════════════════

def test_reissue_creates_a_draft_with_the_data_of_the_cancelled_invoice(world, monkeypatch):
    db = world.db
    r1 = sent(world)
    s1 = cancel(world, r1, '2026-07-02')
    original = columns(db, r1)
    before_original = snapshot(db)
    at(monkeypatch, '2026-09-10')

    result = db.create_replacement_draft(r1, USER)

    assert result == Reissue(True, None, 200, result.invoice_id, '26R002')            # nächste Nummer des Kreises
    draft = columns(db, result.invoice_id)
    assert (draft['InvoiceNumber'], draft['InvoiceDate'], draft['Status']) == ('26R002', '2026-09-10', 'draft')
    assert (draft['ReplacesInvoiceId'], draft['InvoiceKind'], draft['CancelsInvoiceId'], draft['DocumentType']) == (
        r1, None, None, 'invoice')
    assert (draft['SentAt'], draft['DocumentVersion'], draft['PDFPath'], draft['XMLPath']) == (None, None, None, None)
    # Kopf: alles, was zur Rechnung gehört, kommt mit – das Leistungsdatum unverändert
    for column in ('OwnCompanyId', 'SellerName', 'SellerCompany', 'SellerStreet', 'SellerPostalCode', 'SellerCity',
                   'SellerCountry', 'SellerVATID', 'SellerEmail', 'SellerPhone', 'CustomerId', 'BuyerName',
                   'BuyerCompany', 'BuyerStreet', 'BuyerPostalCode', 'BuyerCity', 'BuyerCountry', 'BuyerVATID',
                   'BuyerReference', 'BuyerRouteID', 'OrderNumber', 'Currency', 'DeliveryDate', 'BankAccountId',
                   'BankName', 'BankIBAN', 'BankBIC', 'TaxCategory', 'TaxRate', 'PaymentTerms', 'SkontoDays',
                   'SkontoPercent'):
        assert draft[column] == original[column], column
    assert draft['DeliveryDate'] == '2026-06-05' and draft['PaymentTerms'] == 'Zahlbar in 14 Tagen'
    assert (draft['SkontoDays'], draft['SkontoPercent']) == (7, 2)
    # das Zahlungsziel (14 Tage) läuft ab dem neuen Datum
    assert original['InvoiceDate'] == '2026-06-07' and original['PaymentDueDate'] == '2026-06-21'
    assert draft['PaymentDueDate'] == '2026-09-24'
    # Summen positiv und unverändert, Rest = Brutto
    assert (draft['SumNet'], draft['TaxAmount'], draft['SumGross']) == (1000000, 190000, 1190000)
    assert draft['AmountDue'] == 1190000
    assert items_of(db, result.invoice_id) == items_of(db, r1) == [(1, 'Beratung', 1.0, 1000000, 1000000, 'C62', 19.0)]
    # die stornierte Rechnung und ihre Stornorechnung bleiben, wie sie sind; ein Entwurf wird nicht protokolliert
    after = snapshot(db)
    assert {t: rows for t, rows in after.items() if t not in ('Invoices', 'InvoiceItems', 'NumberRanges')} == \
        {t: rows for t, rows in before_original.items() if t not in ('Invoices', 'InvoiceItems', 'NumberRanges')}
    assert columns(db, r1) == original and columns(db, s1.storno_id)['CancelsInvoiceId'] == r1
    assert protocol(db, result.invoice_id) == []
    assert db.find_replacement_invoice(r1) == (result.invoice_id, '26R002', 'draft')
    assert db.find_replaced_invoice(result.invoice_id) == (r1, 'T-100', s1.storno_id, '26R001')


def test_reissue_takes_the_number_from_the_circle_of_todays_year(world, monkeypatch):
    db = world.db
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    assert [r[2] for r in db.fetch_number_ranges('invoice')] == [2026]

    result = db.create_replacement_draft(r1, USER, today=datetime.date(2027, 1, 4))      # ein anderes Jahr

    assert result.ok and result.invoice_number == '27R001'
    assert sorted((r[2], r[5]) for r in db.fetch_number_ranges('invoice')) == [(2026, 1), (2027, 1)]
    assert columns(db, result.invoice_id)['InvoiceDate'] == '2027-01-04'


@pytest.mark.parametrize('term', ['2026-06-07', '2026-07-05', None, ''], ids=['gleich', 'spaeter', 'ohne', 'leer'])
def test_the_due_date_moves_with_the_payment_term(world, monkeypatch, term):
    """Fälligkeit = neues Datum + (Fälligkeit − Rechnungsdatum) Tage; ist eines der Daten nicht lesbar, bleibt sie leer."""
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    run_sql(world.db, 'UPDATE Invoices SET PaymentDueDate = ? WHERE ID = ?', term, r1)
    at(monkeypatch, '2026-09-10')
    draft = world.db.create_replacement_draft(r1).invoice_id
    expected = {'2026-06-07': '2026-09-10', '2026-07-05': '2026-10-08', None: None, '': None}[term]
    assert columns(world.db, draft)['PaymentDueDate'] == expected


@pytest.mark.parametrize('invoice_date, due', [('2026-06-07', '2026-06-01'), ('kein Datum', '2026-06-21'),
                                               ('2026-06-07', '9999-12-31')],
                         ids=['Ziel-negativ', 'Rechnungsdatum-kaputt', 'Ziel-ueber-das-Jahr-9999'])
def test_an_unusable_payment_term_leaves_the_due_date_empty(world, monkeypatch, invoice_date, due):
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    run_sql(world.db, 'UPDATE Invoices SET InvoiceDate = ?, PaymentDueDate = ? WHERE ID = ?', invoice_date, due, r1)
    at(monkeypatch, '2026-09-10')
    result = world.db.create_replacement_draft(r1)
    assert result.ok and columns(world.db, result.invoice_id)['PaymentDueDate'] is None


def test_the_positions_are_copied_one_to_one_in_their_order(world, monkeypatch):
    items = [{'position': 1, 'quantity': 2.5, 'unit': 'HUR', 'description': 'Beratung', 'unitPrice': 33.33,
              'taxRate': 19}, {'position': 2, 'quantity': 3, 'unit': 'C62', 'description': 'Material',
                               'unitPrice': 7.77, 'taxRate': 19}]
    r1 = sent(world, items=items)
    cancel(world, r1, '2026-07-02')
    draft = reissue(world.db, r1, monkeypatch)
    assert items_of(world.db, draft) == items_of(world.db, r1) and len(items_of(world.db, draft)) == 2
    assert (columns(world.db, draft)['SumNet'], columns(world.db, draft)['SumGross']) == (
        columns(world.db, r1)['SumNet'], columns(world.db, r1)['SumGross'])


def test_the_copy_over_the_form_never_sets_the_link(world, monkeypatch):
    """„Als neu kopieren“ ist reine Maske: Die Rechnung daraus ersetzt keine andere."""
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    page = PageInvoice(world.db, {}, None, copy_from=r1)
    assert 'Kopie von T-100' in page
    created = save(make_payload(world, invoiceNumber='26R050'))
    assert columns(world.db, created['invoice_id'])['ReplacesInvoiceId'] is None
    assert world.db.find_replaced_invoice(created['invoice_id']) is None and world.db.find_replacement_invoice(r1) is None


# ── Ablehnungen ──────────────────────────────────────────────────────────────

def _quote(w):
    return w.db.insert_invoice({
        'invoice_number': '26A001', 'invoice_date': '2026-06-09', 'seller_name': 'S', 'seller_company': 'S GmbH',
        'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19, 'sum_net': 100, 'tax_amount': 19,
        'sum_gross': 119, 'amount_due': 119, 'status': 'sent', 'document_type': 'quote'})


def _unknown(w, monkeypatch):
    return 99999, 404, 'Rechnung nicht gefunden.'


def _a_quote(w, monkeypatch):
    return _quote(w), 404, 'Rechnung nicht gefunden.'


def _a_credit_note(w, monkeypatch):
    return cancel(w, sent(w)).storno_id, 409, REISSUE_STORNO_MESSAGE


def _a_sent_invoice(w, monkeypatch):
    return sent(w), 409, REISSUE_NOT_CANCELLED_MESSAGE


def _a_draft(w, monkeypatch):
    return make_invoice(w, 'draft', number='T-300'), 409, REISSUE_NOT_CANCELLED_MESSAGE


def _cancelled_by_hand(w, monkeypatch):
    invoice_id = sent(w)
    w.db.update_invoice_status(invoice_id, 'cancelled')                      # Altbestand: ohne Stornorechnung
    return invoice_id, 409, REISSUE_NO_STORNO_MESSAGE


def _already_replaced(w, monkeypatch):
    invoice_id = sent(w)
    cancel(w, invoice_id, '2026-07-02')
    reissue(w.db, invoice_id, monkeypatch)
    return invoice_id, 409, 'Zu dieser Rechnung gibt es bereits die neue Rechnung 26R002.'


def _no_usable_number(w, monkeypatch):
    invoice_id = sent(w)
    cancel(w, invoice_id, '2026-07-02')
    run_sql(w.db, "UPDATE NumberRanges SET NumberFormat = '{yy}{l}'")       # ohne Zähler
    return invoice_id, 409, None


def _dangling_customer(w, monkeypatch):
    invoice_id = sent(w)
    cancel(w, invoice_id, '2026-07-02')
    run_sql(w.db, 'DELETE FROM Contacts WHERE ID = ?', w.cust)
    return invoice_id, 409, ('Die neue Rechnung konnte nicht angelegt werden: Verweis ins Leere – '
                             f'Kunde mit ID {w.cust} gibt es nicht (Tabelle Contacts)')


def _failing_write(w, monkeypatch):
    invoice_id = sent(w)
    cancel(w, invoice_id, '2026-07-02')
    run_sql(w.db, "CREATE TRIGGER boom BEFORE INSERT ON InvoiceItems "
                  "BEGIN SELECT RAISE(ABORT, 'Test: nicht erlaubt'); END")
    return invoice_id, 409, REISSUE_FAILED_MESSAGE


REFUSALS = [pytest.param(_unknown, id='unbekannte-ID'), pytest.param(_a_quote, id='Angebot'),
            pytest.param(_a_credit_note, id='Stornorechnung'), pytest.param(_a_sent_invoice, id='versendet'),
            pytest.param(_a_draft, id='Entwurf'), pytest.param(_cancelled_by_hand, id='ohne-Stornorechnung'),
            pytest.param(_already_replaced, id='schon-ersetzt'), pytest.param(_no_usable_number, id='Nummernkreis'),
            pytest.param(_dangling_customer, id='Verweis-ins-Leere'),
            pytest.param(_failing_write, id='Fehler-beim-Speichern')]


@pytest.mark.parametrize('prepare', REFUSALS)
def test_every_refusal_names_its_reason_and_writes_nothing(world, monkeypatch, prepare):
    invoice_id, code, message = prepare(world, monkeypatch)
    at(monkeypatch, TODAY)
    before, circles = tables(world.db), world.db.fetch_number_ranges('invoice')

    result = world.db.create_replacement_draft(invoice_id, USER)

    assert (result.ok, result.code) == (False, code), result
    assert result.message and (message is None or result.message == message), result.message
    assert (result.invoice_id, result.invoice_number) == (None, None)
    assert tables(world.db) == before and world.db.fetch_number_ranges('invoice') == circles   # auch kein Zähler


def test_a_circle_without_a_usable_number_names_the_counter(world, monkeypatch):
    invoice_id, _code, _message = _no_usable_number(world, monkeypatch)
    at(monkeypatch, TODAY)
    assert 'Zähler' in world.db.create_replacement_draft(invoice_id).message


def test_the_refusal_after_a_failure_leaves_the_number_for_the_next_try(world, monkeypatch):
    invoice_id, _code, _message = _failing_write(world, monkeypatch)
    at(monkeypatch, TODAY)
    circles = world.db.fetch_number_ranges('invoice')
    assert world.db.create_replacement_draft(invoice_id).ok is False
    assert world.db.fetch_number_ranges('invoice') == circles
    run_sql(world.db, 'DROP TRIGGER boom')
    assert world.db.create_replacement_draft(invoice_id).invoice_number == '26R002'      # der nächste Versuch bekommt sie


def test_after_deleting_the_draft_the_invoice_can_be_reissued_again_with_the_same_number(world, monkeypatch):
    db = world.db
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    first = reissue(db, r1, monkeypatch)
    assert db.create_replacement_draft(r1).code == 409                       # einmal je stornierter Rechnung

    db.delete_draft_invoice(first)                                           # der Entwurf war nie versendet: Nummer frei

    assert db.find_replacement_invoice(r1) is None
    second = db.create_replacement_draft(r1)
    assert second.ok and second.invoice_number == '26R002' and second.invoice_id != first


def test_two_reissues_racing_make_one_draft(world, monkeypatch):
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    at(monkeypatch, TODAY)
    barrier, results = threading.Barrier(2), [None, None]

    def run(index):
        barrier.wait()
        results[index] = world.db.create_replacement_draft(r1, USER)
    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.ok for r in results) == [False, True]
    assert len(query_rows(world.db, 'SELECT * FROM Invoices WHERE ReplacesInvoiceId IS NOT NULL')) == 1
    assert world.db.fetch_number_ranges('invoice')[0][5] == 2                # genau eine Nummer mehr


# ── Die Routen ───────────────────────────────────────────────────────────────

def _cancel_body(invoice_id, **extra):
    return json.dumps({'invoiceId': invoice_id, 'date': '2026-07-01', **extra}).encode()


def test_cancelling_with_reissue_creates_the_credit_note_and_the_draft(world, monkeypatch):
    db = world.db
    r1 = sent(world)
    at(monkeypatch, TODAY)

    code, text = handlers.handle_invoice_cancel(_cancel_body(r1, reissue=True))

    body = json.loads(text)
    assert code == 200 and set(body) == {'success', 'storno_id', 'storno_number', 'replacement_id'}, body
    assert columns(db, r1)['Status'] == 'cancelled' and columns(db, body['storno_id'])['InvoiceKind'] == 'storno'
    draft = columns(db, body['replacement_id'])
    assert (draft['Status'], draft['ReplacesInvoiceId'], draft['InvoiceNumber']) == ('draft', r1, '26R002')
    assert draft['InvoiceDate'] == TODAY


@pytest.mark.parametrize('extra', [{}, {'reissue': False}], ids=['ohne-Angabe', 'false'])
def test_cancelling_without_reissue_creates_no_draft(world, extra):
    r1 = sent(world)
    body = json.loads(handlers.handle_invoice_cancel(_cancel_body(r1, **extra))[1])
    assert 'replacement_id' not in body and world.db.find_replacement_invoice(r1) is None


@pytest.mark.parametrize('value', ['ja', 'true', 1, 0, None, [], {}, 'false'])
def test_a_reissue_that_is_no_boolean_is_an_invalid_request_before_anything_happens(world, value):
    r1 = sent(world)
    before = tables(world.db)
    with pytest.raises(handlers.InvalidRequest):
        handlers.handle_invoice_cancel(_cancel_body(r1, reissue=value))
    assert tables(world.db) == before and columns(world.db, r1)['Status'] == 'sent'


def test_a_failing_draft_keeps_the_cancellation_and_warns(world, monkeypatch):
    db = world.db
    r1 = sent(world)
    original, calls = type(db).allocate_invoice_number, []

    def second_call_fails(self, conn, invoice_date, requested=None):
        calls.append(invoice_date)
        if len(calls) > 1:
            raise NumberRangeError('Im Nummernkreis ist keine freie Rechnungsnummer zu finden.')
        return original(self, conn, invoice_date, requested)
    monkeypatch.setattr(type(db), 'allocate_invoice_number', second_call_fails)

    code, text = handlers.handle_invoice_cancel(_cancel_body(r1, reissue=True))

    body = json.loads(text)
    assert code == 200 and body['success'] is True and 'replacement_id' not in body
    assert body['warning'] == ('Der Entwurf der neuen Rechnung wurde nicht angelegt: Im Nummernkreis ist keine freie '
                               'Rechnungsnummer zu finden. – „Neu ausstellen“ auf der Seite der stornierten Rechnung '
                               'holt es nach.')
    assert columns(db, r1)['Status'] == 'cancelled' and db.find_storno_invoice(r1) is not None   # die Stornierung steht
    assert db.find_replacement_invoice(r1) is None and len(calls) == 2


def test_the_warning_of_the_draft_is_appended_to_the_one_of_the_document(world, monkeypatch):
    db = world.db
    r1 = sent(world)

    def broken_documents(db_, invoice_id, with_xml=None, overwrite_sent=False):
        raise RuntimeError('Datei nicht schreibbar /home/niemand')
    monkeypatch.setattr(invoice_files, 'generate_invoice_documents', broken_documents)
    original, calls = type(db).allocate_invoice_number, []

    def second_call_fails(self, conn, invoice_date, requested=None):
        calls.append(invoice_date)
        if len(calls) > 1:
            raise NumberRangeError('Kein Nummernkreis.')
        return original(self, conn, invoice_date, requested)
    monkeypatch.setattr(type(db), 'allocate_invoice_number', second_call_fails)

    body = json.loads(handlers.handle_invoice_cancel(_cancel_body(r1, reissue=True))[1])

    assert body['warning'].startswith('Die Stornorechnung ist gespeichert, aber das Dokument konnte nicht erstellt '
                                      'werden – bitte „PDF + E-Rechnung“ ausführen. Der Entwurf der neuen Rechnung '
                                      'wurde nicht angelegt: Kein Nummernkreis. – ')
    assert 'niemand' not in body['warning']


def test_the_reissue_handler_answers_with_the_new_draft(world, monkeypatch):
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    at(monkeypatch, TODAY)

    code, text = handlers.handle_invoice_reissue(json.dumps({'invoiceId': r1}).encode())

    body = json.loads(text)
    assert code == 200 and body == {'success': True, 'replacement_id': body['replacement_id'],
                                    'replacement_number': '26R002'}
    assert columns(world.db, body['replacement_id'])['ReplacesInvoiceId'] == r1


def test_the_reissue_handler_passes_a_refusal_on_with_its_status_and_text(world):
    r1 = sent(world)
    before = tables(world.db)
    code, text = handlers.handle_invoice_reissue(json.dumps({'invoiceId': r1}).encode())
    assert (code, json.loads(text)) == (409, {'success': False, 'error': REISSUE_NOT_CANCELLED_MESSAGE})
    assert handlers.handle_invoice_reissue(json.dumps({'invoiceId': 99999}).encode())[0] == 404
    assert tables(world.db) == before


@pytest.mark.parametrize('body', [b'{}', b'{"invoiceId": null}', b'[]', b'{'])
def test_the_reissue_handler_needs_an_invoice_id(world, body):
    with pytest.raises(handlers.InvalidRequest):
        handlers.handle_invoice_reissue(body)


def test_a_malformed_invoice_id_is_refused_by_the_reissue_handler(world):
    code, text = handlers.handle_invoice_reissue(json.dumps({'invoiceId': '01'}).encode())
    assert code == 400 and json.loads(text)['success'] is False


def test_over_http_reissue_creates_the_draft_and_a_second_call_is_refused(server_port, books):
    invoice_id = sent_over_http(server_port, books)
    resp = post_json(server_port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-01'}, books.csrf)
    assert resp.status == 200

    resp = post_json(server_port, '/invoice/reissue', {'invoiceId': invoice_id}, books.csrf)

    body = json.loads(resp.text)
    assert resp.status == 200 and resp.headers['Content-Type'].startswith('application/json')
    assert body['success'] is True and columns(books.db, body['replacement_id'])['ReplacesInvoiceId'] == invoice_id
    assert body['replacement_number'] == columns(books.db, body['replacement_id'])['InvoiceNumber']
    again = post_json(server_port, '/invoice/reissue', {'invoiceId': invoice_id}, books.csrf)
    assert again.status == 409 and json.loads(again.text)['error'].startswith(
        'Zu dieser Rechnung gibt es bereits die neue Rechnung')


def test_over_http_cancel_with_reissue_answers_with_the_draft(server_port, books):
    invoice_id = sent_over_http(server_port, books)
    resp = post_json(server_port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-01', 'reissue': True},
                     books.csrf)
    body = json.loads(resp.text)
    assert resp.status == 200 and columns(books.db, body['replacement_id'])['Status'] == 'draft'
    page = get(server_port, f"/invoice?id={body['replacement_id']}")
    assert page.status == 200 and 'Ersetzt:' in page.text


def test_over_http_the_whole_way_from_the_payment_to_the_sent_replacement(server_port, books):
    """Bezahlen, „Stornieren und neu ausstellen“, die Ersatzrechnung versenden: Die Antwort nennt, was mit der Zahlung
    geschah, und die Zahlung hängt danach an der neuen Rechnung."""
    db = books.db
    invoice_id = sent_over_http(server_port, books)
    booking = db.insert_booking('2026-07-01', 50.0, booking_type='bank', account_id=books.bank,
                                recipient_client=CUSTOMER_TEXT)
    assert handlers.link_booking_to_invoice_capped(db, invoice_id, booking, 50.0)[0]
    cancelled = json.loads(post_json(server_port, '/invoice/cancel', {
        'invoiceId': invoice_id, 'date': '2026-07-02', 'reissue': True}, books.csrf).text)
    draft = cancelled['replacement_id']
    page = get(server_port, f'/invoice?id={invoice_id}').text
    assert 'Guthaben des Kunden: 50,00 €' in page and 'Erstattung offen: 50,00 €' in page   # Seite und Liste daneben

    resp = post_json(server_port, '/invoice/status', {'invoice_id': draft, 'status': 'sent'}, books.csrf)

    body = json.loads(resp.text)
    assert resp.status == 200 and body['success'] is True
    assert body['hint'] == 'Zahlungen der stornierten Rechnung T-100 über 50,00 € sind jetzt dieser Rechnung zugeordnet.'
    assert allocations(db, invoice_id) == [] and allocations(db, draft) == [(booking, 500000, '2026-07-01')]
    assert columns(db, draft)['Status'] == 'partial_payment' and db.find_payment_inconsistencies() == []
    page = get(server_port, f'/invoice?id={invoice_id}').text
    assert 'Guthaben des Kunden' not in page and 'Erstattung offen' not in page


def test_over_http_a_reissue_that_is_no_boolean_is_400_and_cancels_nothing(server_port, books):
    invoice_id = sent_over_http(server_port, books)
    before = tables(books.db)
    resp = post_json(server_port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-01', 'reissue': 'ja'},
                     books.csrf)
    assert resp.status == 400 and json.loads(resp.text) == {'success': False, 'error': 'Ungültige Anfrage.'}
    assert tables(books.db) == before and columns(books.db, invoice_id)['Status'] == 'sent'


def test_over_http_get_is_405_and_reissues_nothing(server_port, books):
    invoice_id = sent_over_http(server_port, books)
    post_json(server_port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-01'}, books.csrf)
    before = tables(books.db)
    for path in ('/invoice/reissue', f'/invoice/reissue?invoiceId={invoice_id}', f'/invoice/reissue?id={invoice_id}'):
        resp = get(server_port, path)
        assert resp.status == 405 and resp.headers['Allow'] == 'POST', path
    assert tables(books.db) == before


def test_over_http_the_reissue_route_needs_origin_and_token(server_port, books):
    invoice_id = sent_over_http(server_port, books)
    post_json(server_port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-01'}, books.csrf)
    before = tables(books.db)
    payload = {'invoiceId': invoice_id}
    assert post_json(server_port, '/invoice/reissue', payload, books.csrf,
                     headers={'Origin': FOREIGN_ORIGIN}).status == 403
    assert post_json(server_port, '/invoice/reissue', payload, 'falsch').status == 403
    assert post_json(server_port, '/invoice/reissue', payload, '').status == 403
    assert tables(books.db) == before                                           # nichts angelegt …
    assert post_json(server_port, '/invoice/reissue', payload, books.csrf).status == 200    # … erst mit beidem geht es
    assert books.db.find_replacement_invoice(invoice_id) is not None


@pytest.mark.parametrize('bad_id', ['"01"', '1.5', 'true', '"abc"', '[1]', '{"a": 1}', '""'])
def test_over_http_the_reissue_id_must_be_a_whole_number(server_port, books, bad_id):
    invoice_id = sent_over_http(server_port, books)
    post_json(server_port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-01'}, books.csrf)
    before = tables(books.db)
    resp = post_json(server_port, '/invoice/reissue', '{"invoiceId": %s}' % bad_id, books.csrf)
    assert resp.status == 400 and json.loads(resp.text)['success'] is False
    assert tables(books.db) == before


# ── Die Seiten ───────────────────────────────────────────────────────────────

def _buttons(html):
    return [a.get('onclick') for tag, a in Dom(html).tags if tag == 'button']


def test_a_sent_invoice_offers_both_ways_of_cancelling(world):
    r1 = sent(world)
    html = PageInvoice(world.db, {}, r1)
    assert f'cancelInvoice({r1})' in _buttons(html) and f'cancelInvoice({r1}, true)' in _buttons(html)
    assert 'Stornieren &hellip;' in html and 'Stornieren und neu ausstellen &hellip;' in html
    script = next(s for s in Dom(html).scripts if 'function cancelInvoice(' in s)
    assert json.dumps(reissue_question('T-100'), ensure_ascii=False) in script
    assert json.dumps(cancel_question('T-100'), ensure_ascii=False) in script
    assert 'invoiceId: invId, date: day' in script and 'reissue: true' in script
    assert 'function reissueInvoice(' not in html
    # ein Entwurf und eine Stornorechnung bieten weder das eine noch das andere an
    credit = cancel(world, sent(world, number='T-401')).storno_id
    for page in (PageInvoice(world.db, {}, make_invoice(world, 'draft', number='T-400')),
                 PageInvoice(world.db, {}, credit)):
        assert 'cancelInvoice' not in page and 'reissueInvoice' not in page


def test_a_cancelled_invoice_with_a_credit_note_offers_reissuing_until_there_is_a_draft(world, monkeypatch):
    db = world.db
    r1 = sent(world)
    s1 = cancel(world, r1, '2026-07-02')
    html = PageInvoice(db, {}, r1)
    assert f'reissueInvoice({r1})' in _buttons(html) and 'Neu ausstellen &hellip;' in html
    assert not any('cancelInvoice' in (b or '') for b in _buttons(html)) and 'Neue Rechnung:' not in html
    script = next(s for s in Dom(html).scripts if 'function reissueInvoice(' in s)
    assert "fetch('/invoice/reissue'" in script and json.dumps(new_draft_question('T-100'), ensure_ascii=False) in script
    assert 'reissueInvoice' not in PageInvoice(db, {}, s1.storno_id)         # die Stornorechnung bietet es nie an

    draft = reissue(db, r1, monkeypatch)

    html = PageInvoice(db, {}, r1)
    assert 'reissueInvoice' not in html and 'Neu ausstellen' not in html
    assert f'<tr><td>Neue Rechnung:</td><td><a href="/invoice?id={draft}">26R002</a></td></tr>' in html


def test_the_replacement_names_the_cancelled_invoice_and_its_credit_note(world, monkeypatch):
    r1 = sent(world)
    s1 = cancel(world, r1, '2026-07-02')
    draft = reissue(world.db, r1, monkeypatch)
    html = PageInvoice(world.db, {}, draft)
    assert (f'<tr><td>Ersetzt:</td><td>stornierte Rechnung <a href="/invoice?id={r1}">T-100</a> '
            f'(Stornorechnung <a href="/invoice?id={s1.storno_id}">26R001</a>)</td></tr>') in html
    assert 'Neue Rechnung:' not in html and 'Ersetzt:' not in PageInvoice(world.db, {}, r1)
    assert 'Ersetzt:' not in PageInvoice(world.db, {}, s1.storno_id)
    assert f'saveInvoice()' in _buttons(html) and 'reissueInvoice' not in html     # ein Entwurf bleibt ein Entwurf


def test_a_replacement_without_its_credit_note_names_only_the_invoice(world, monkeypatch):
    r1 = sent(world)
    s1 = cancel(world, r1, '2026-07-02')
    draft = reissue(world.db, r1, monkeypatch)
    run_sql(world.db, 'UPDATE Invoices SET CancelsInvoiceId = NULL WHERE ID = ?', s1.storno_id)    # Eingriff per SQL
    html = PageInvoice(world.db, {}, draft)
    assert f'<td>stornierte Rechnung <a href="/invoice?id={r1}">T-100</a></td>' in html and 'Stornorechnung <a' not in html


def test_the_pages_escape_numbers_and_never_offer_actions_for_the_wrong_documents(world, monkeypatch):
    hostile = 'T-<b>x</b>'
    r1 = sent(world, number=hostile)
    cancel(world, r1, '2026-07-02')
    draft = reissue(world.db, r1, monkeypatch)
    pages = {'stornierte': PageInvoice(world.db, {}, r1), 'Ersatz': PageInvoice(world.db, {}, draft)}
    for name, html in pages.items():
        assert hostile not in html, name
        assert 'T-&lt;b&gt;x&lt;/b&gt;' in html, name
    other = sent(world, number='T-<i>y')
    cancel(world, other, '2026-07-03')
    script = next(s for s in Dom(PageInvoice(world.db, {}, other)).scripts if 'function reissueInvoice(' in s)
    assert '<i>' not in script and 'T-\\u003ci\\u003ey' in script
    sent_page = PageInvoice(world.db, {}, sent(world, number='T-<u>z'))
    script = next(s for s in Dom(sent_page).scripts if 'function cancelInvoice(' in s)
    assert '<u>' not in script and 'T-\\u003cu\\u003ez' in script


@NO_NODE
def test_every_new_script_block_parses(world, monkeypatch, tmp_path):
    import subprocess
    from tests.test_invoice_storno import NODE
    r1 = sent(world)
    s1 = cancel(world, r1, '2026-07-02')
    draft = reissue(world.db, r1, monkeypatch)
    alt = sent(world, number='T-101')
    world.db.update_invoice_status(alt, 'cancelled')
    pages = [PageInvoice(world.db, {}, i) for i in (r1, s1.storno_id, draft, alt, sent(world, number='T-102'))]
    for number, html in enumerate(pages):
        for index, block in enumerate(Dom(html).scripts):
            source = tmp_path / f'page{number}_{index}.js'
            source.write_text(block.split('</script>')[0], encoding='utf-8')
            done = subprocess.run([NODE, '--check', str(source)], capture_output=True, text=True, timeout=60)
            assert done.returncode == 0, f'Seite {number}, Block {index}: {done.stderr}'


SCRIPT_RUNNER = r"""
const fs = require('fs'), vm = require('vm');
const scenario = JSON.parse(process.argv[3]);
const log = {asked: [], requests: [], messages: [], redirects: []};
const location = {};
Object.defineProperty(location, 'href', {set(v) { log.redirects.push(v); }});
const sandbox = {JSON, window: {location},
  appConfirm: (text, onOk, field) => { log.asked.push({text, field: field === undefined ? null : field}); onOk(scenario.day); },
  fetch: (url, init) => { log.requests.push({url, init});
    return scenario.fail ? Promise.reject(new Error('Netz weg'))
                         : Promise.resolve({json: () => Promise.resolve(scenario.answer)}); },
  showMessage: (text, type) => log.messages.push([text, type]),
  setTimeout: (fn, ms) => { fn(); }};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8') + '\n' + scenario.call + ';', sandbox);
setTimeout(() => console.log(JSON.stringify(log)), 50);
"""


def _script(world, invoice_id, function):
    return next(s for s in Dom(PageInvoice(world.db, {}, invoice_id)).scripts if f'function {function}(' in s)


@NO_NODE
@pytest.mark.parametrize('call, answer, body, question, redirects', [
    ('cancelInvoice(7, true)', {'success': True, 'storno_id': 9, 'replacement_id': 12},
     {'invoiceId': 7, 'date': '2026-07-02', 'reissue': True}, 'reissue', ['/invoice?id=12']),
    ('cancelInvoice(7, true)', {'success': True, 'storno_id': 9, 'warning': 'Warnung'},
     {'invoiceId': 7, 'date': '2026-07-02', 'reissue': True}, 'reissue', ['/invoice?id=9']),
    ('cancelInvoice(7)', {'success': True, 'storno_id': 9},
     {'invoiceId': 7, 'date': '2026-07-02'}, 'cancel', ['/invoice?id=9']),
    ('cancelInvoice(7, false)', {'success': True, 'storno_id': 9},
     {'invoiceId': 7, 'date': '2026-07-02'}, 'cancel', ['/invoice?id=9']),
], ids=['neu-ausstellen', 'Entwurf-gescheitert', 'ohne-reissue-bleibt-der-Body', 'reissue-false'])
def test_the_cancel_script_sends_reissue_only_when_asked_and_follows_the_draft(world, tmp_path, call, answer, body,
                                                                               question, redirects):
    invoice_id = sent(world)
    log = _node(tmp_path, _script(world, invoice_id, 'cancelInvoice'), SCRIPT_RUNNER,
                {'day': '2026-07-02', 'answer': answer, 'fail': False, 'call': call})
    asked = {'reissue': reissue_question('T-100'), 'cancel': cancel_question('T-100')}[question]
    assert [a['text'] for a in log['asked']] == [asked] and log['asked'][0]['field']['label'] == 'Stornodatum:'
    (request,) = log['requests']
    assert request['url'] == '/invoice/cancel' and request['init']['method'] == 'POST'
    assert json.loads(request['init']['body']) == body
    assert log['redirects'] == redirects


@NO_NODE
@pytest.mark.parametrize('answer, fail, redirects, messages', [
    ({'success': True, 'replacement_id': 12, 'replacement_number': '26R002'}, False, ['/invoice?id=12'], []),
    ({'success': False, 'error': 'Zu dieser Rechnung gibt es bereits die neue Rechnung 26R002.'}, False, [],
     [['Fehler: Zu dieser Rechnung gibt es bereits die neue Rechnung 26R002.', 'error']]),
    (None, True, [], [['Fehler: Netz weg', 'error']]),
], ids=['Erfolg', 'Ablehnung', 'Netzfehler'])
def test_the_reissue_script_asks_posts_and_opens_the_draft(world, monkeypatch, tmp_path, answer, fail, redirects,
                                                           messages):
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    log = _node(tmp_path, _script(world, r1, 'reissueInvoice'), SCRIPT_RUNNER,
                {'day': None, 'answer': answer, 'fail': fail, 'call': 'reissueInvoice(7)'})
    assert log['asked'] == [{'text': new_draft_question('T-100'), 'field': None}]
    (request,) = log['requests']
    assert request['url'] == '/invoice/reissue' and request['init']['method'] == 'POST'
    assert json.loads(request['init']['body']) == {'invoiceId': 7}
    assert log['redirects'] == redirects and log['messages'] == messages


# ═════════════════════════════════════════════════════════════════════════════
# Die Zahlungen ziehen beim Versenden der Ersatzrechnung um
# ═════════════════════════════════════════════════════════════════════════════

BEFORE = {DEBTORS: -500000, WAITING: 500000, 4345: 0, 4400: -500000, 4300: 0}     # F, F′, 50,00 € auf R1
HINT_ALL = 'Zahlungen der stornierten Rechnung T-100 über 50,00 € sind jetzt dieser Rechnung zugeordnet.'


def test_the_example_before_the_replacement_is_sent(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    db = world.db
    assert ledger(db) == BEFORE
    assert euer(db) == {4400: 42.02, 3806: 7.98}
    assert standing(db, s.r1, s.s1, s.r2) == [('cancelled', 690000), ('sent', -1190000), ('draft', 1190000)]
    assert db.get_storno_credits() == {s.s1: 50}
    assert db.find_payment_inconsistencies() == []


def test_case_a_same_amount_and_rate_moves_the_whole_payment_and_changes_no_figure(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    db = world.db
    r2_number = columns(db, s.r2)['InvoiceNumber']

    code, body = set_invoice_status(s.r2, 'sent')

    assert (code, body) == (200, {'success': True, 'invoice_id': s.r2, 'hint': HINT_ALL})
    assert ledger(db) == {DEBTORS: 690000, WAITING: -690000, 4345: 0, 4400: -500000, 4300: 0}
    assert euer(db) == {4400: 42.02, 3806: 7.98}                              # unverändert: gleicher Betrag, gleicher Satz
    assert balance(db, 1800) == 500000                                        # die Bank bleibt, wie sie war
    assert standing(db, s.r1, s.s1, s.r2) == [('cancelled', 1190000), ('sent', -1190000), ('partial_payment', 690000)]
    assert allocations(db, s.r1) == [] and allocations(db, s.r2) == [(s.booking, 500000, '2026-07-01')]
    assert payment_rows(db, 'T-100') == []                                    # die Sätze der alten Rechnung sind weg …
    assert payment_rows(db, r2_number) == [(1800, DEBTORS, 500000, None, '2026-07-01', 'liquid_first'),
                                           (WAITING, 4400, 500000, 79800, '2026-07-01', 'transfer')]   # … neu, mit dem Bankdatum
    assert [r[3] for r in bookings_of(db, 'T-100')] == [1190000]               # die Forderung der stornierten Rechnung bleibt
    assert [r[3] for r in bookings_of(db, '26R001')] == [-1190000]             # samt Gegenbuchung der Stornorechnung
    assert [r[3] for r in bookings_of(db, r2_number)] == [1190000]             # die neue Forderung
    assert db.get_storno_credits() == {s.s1: 0}
    assert db.find_payment_inconsistencies() == []
    assert columns(db, s.r2)['SentAt'] and columns(db, s.r2)['Status'] == 'partial_payment'
    assert [(r['OldValue'], r['NewValue']) for r in protocol(db, s.r2, 'status')] == [('draft', 'sent')]


def test_case_b_a_smaller_replacement_takes_what_it_needs_and_the_rest_stays_with_the_cancelled_invoice(
        world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    db = world.db
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], unitPrice=33.61)])              # 33,61 netto + 6,39 USt = 40,00
    assert columns(db, s.r2)['SumGross'] == 400000
    r2_number = columns(db, s.r2)['InvoiceNumber']

    code, body = set_invoice_status(s.r2, 'sent')

    assert code == 200 and body['hint'] == (HINT_ALL.replace('50,00', '40,00')
                                            + ' 10,00 € bleiben als Guthaben des Kunden an der stornierten Rechnung.')
    assert ledger(db) == {DEBTORS: -100000, WAITING: 100000, 4345: 0, 4400: -500000, 4300: 0}
    assert euer(db) == {4400: 42.01, 3806: 7.99}                              # 6,39 + 1,60 statt 7,98: der Betrag ändert sich
    assert standing(db, s.r1, s.s1, s.r2) == [('cancelled', 1090000), ('sent', -1190000), ('paid', 0)]
    assert allocations(db, s.r2) == [(s.booking, 400000, '2026-07-01')]
    assert allocations(db, s.r1) == [(s.booking, 100000, '2026-07-01')]
    assert payment_rows(db, r2_number) == [(1800, DEBTORS, 400000, None, '2026-07-01', 'liquid_first'),
                                           (WAITING, 4400, 400000, 63900, '2026-07-01', 'transfer')]   # schließt: 6,39
    assert payment_rows(db, 'T-100') == [(1800, DEBTORS, 100000, None, '2026-07-01', 'liquid_first'),
                                         (WAITING, 4400, 100000, 16000, '2026-07-01', 'transfer')]     # anteilig: 1,60
    assert db.get_storno_credits() == {s.s1: 10}
    assert db.get_booking_unallocated_amount(s.booking) == 0
    assert db.find_payment_inconsistencies() == []


def test_case_c_another_tax_rate_books_on_its_own_accounts_and_says_so(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    db = world.db
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], unitPrice=111.21, taxRate=7)], taxRate=0.07)   # 111,21 + 7,78 = 118,99
    assert (columns(db, s.r2)['SumGross'], columns(db, s.r2)['TaxAmount']) == (1189900, 77800)
    r2_number = columns(db, s.r2)['InvoiceNumber']

    code, body = set_invoice_status(s.r2, 'sent')

    assert code == 200 and body['hint'] == (HINT_ALL + ' Der Steuersatz weicht ab – die Umsatzsteuer am Zahlungstag '
                                                       'ändert sich.')
    assert ledger(db) == {DEBTORS: 689900, WAITING: 0, 4345: -689900, 4400: 0, 4300: -500000}
    assert euer(db) == {4300: 46.73, 3806: 3.27}                              # die USt des Zahlungstags ist jetzt die zu 7 %
    assert standing(db, s.r1, s.s1, s.r2) == [('cancelled', 1190000), ('sent', -1190000), ('partial_payment', 689900)]
    assert payment_rows(db, r2_number) == [(1800, DEBTORS, 500000, None, '2026-07-01', 'liquid_first'),
                                           (4345, 4300, 500000, 32700, '2026-07-01', 'transfer')]
    assert [r[1:3] for r in bookings_of(db, r2_number)] == [(DEBTORS, 4345)]    # die Forderung auf das Wartekonto zu 7 %
    assert db.find_payment_inconsistencies() == []


def test_the_rest_stays_on_the_old_account_and_the_part_goes_to_the_new_one(world, monkeypatch):
    """Die neue Rechnung (7 %, 40,00 €) nimmt 40,00 € auf 4300, der Rest von 10,00 € bleibt zu 19 % auf 4400."""
    s = paid_scenario(world, monkeypatch)
    db = world.db
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], unitPrice=37.38, taxRate=7)], taxRate=0.07)       # 37,38 + 2,62 = 40,00
    assert columns(db, s.r2)['SumGross'] == 400000

    code, body = set_invoice_status(s.r2, 'sent')

    assert code == 200 and 'Der Steuersatz weicht ab' in body['hint'] and 'Guthaben des Kunden' in body['hint']
    assert ledger(db) == {DEBTORS: -100000, WAITING: 100000, 4345: 0, 4400: -100000, 4300: -400000}
    assert [r[0:2] for r in payment_rows(db, 'T-100')] == [(1800, DEBTORS), (WAITING, 4400)]   # der Rest bleibt zu 19 %
    assert [r[2:4] for r in payment_rows(db, 'T-100')][1] == (100000, 16000)
    assert db.find_payment_inconsistencies() == []


def test_an_older_payment_moves_first_and_the_newer_one_stays_when_the_replacement_is_smaller(world, monkeypatch):
    r1 = sent(world)
    first = pay(world, r1, 30.0)                                              # 01.07.
    second_day = world.db.insert_booking('2026-07-15', 50.0, booking_type='bank', account_id=world.bank,
                                         recipient_client=CUSTOMER_TEXT)
    ok, error = handlers.link_booking_to_invoice_capped(world.db, r1, second_day, 50.0)
    assert ok, error
    cancel(world, r1, '2026-07-20')
    r2 = reissue(world.db, r1, monkeypatch)
    edit_draft(world, r2, [dict(ONE_ITEM[0], unitPrice=50.0)])                 # 50 netto + 9,50 USt = 59,50

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200
    assert allocations(world.db, r2) == [(first, 300000, '2026-07-01'), (second_day, 295000, '2026-07-15')]
    assert allocations(world.db, r1) == [(second_day, 205000, '2026-07-15')]
    assert columns(world.db, r2)['Status'] == 'paid' and world.db.get_storno_credits() != {}
    assert world.db.find_payment_inconsistencies() == []


def test_a_tax_rate_without_a_waiting_account_moves_with_the_single_posting_form(world, monkeypatch):
    """16 %: kein Wartekonto, also keine Forderung – die Zuordnung besteht aus dem Einzelsatz „Erlöskonto an Bank“, der
    mit dem Erlöskonto, das der Nutzer gewählt hat (und das die App seither für 16 % kennt), umzieht."""
    db = world.db
    items = [dict(ONE_ITEM[0], taxRate=16)]
    r1 = sent(world, items=items, taxRate=0.16)                                  # 100 netto + 16 USt = 116
    booking = db.insert_booking('2026-07-01', 50.0, booking_type='bank', account_id=world.bank,
                                recipient_client=CUSTOMER_TEXT)
    chosen = db.get_coa_id_by_account_number(4400)
    assert handlers.link_booking_to_invoice_capped(db, r1, booking, 50.0, revenue_coa_id=chosen)[0]
    assert payment_rows(db, 'T-100') == [(4400, 1800, 500000, 69000, '2026-07-01', 'purpose_first')]
    storno = cancel(world, r1, '2026-07-02')
    assert storno.counter_booked is False and bookings_of(db, 'T-100') == []     # keine Forderung, keine Gegenbuchung
    r2 = reissue(db, r1, monkeypatch)
    assert db.revenue_coa_choice_needed(0.16) is False                            # gelernt aus den Sätzen der Zuordnung

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200 and body['hint'] == HINT_ALL
    assert allocations(db, r1) == [] and allocations(db, r2) == [(booking, 500000, '2026-07-01')]
    assert payment_rows(db, 'T-100') == []
    assert payment_rows(db, columns(db, r2)['InvoiceNumber']) == [(4400, 1800, 500000, 69000, '2026-07-01',
                                                                  'purpose_first')]
    assert bookings_of(db, columns(db, r2)['InvoiceNumber']) == []               # auch die neue Rechnung hat keine Forderung
    assert standing(db, r1, r2) == [('cancelled', 1160000), ('partial_payment', 660000)]
    assert db.find_payment_inconsistencies() == []


# ── Was sich nicht umziehen lässt, bleibt – mit Hinweis ──────────────────────

def _send_without_move(s, world, expected_hint):
    """R2 wird versendet; die Zahlungen bleiben an R1, und der Hinweis sagt es."""
    db = world.db
    before_r1 = (allocations(db, s.r1), payment_rows(db, 'T-100'))
    code, body = set_invoice_status(s.r2, 'sent')
    assert code == 200 and body['hint'] == expected_hint, body
    assert (allocations(db, s.r1), payment_rows(db, 'T-100')) == before_r1       # nichts von R1 angefasst
    assert allocations(db, s.r2) == [] and payment_rows(db, columns(db, s.r2)['InvoiceNumber']) == []
    assert columns(db, s.r1)['Status'] == 'cancelled'
    return body


def test_a_tax_rate_without_a_revenue_account_moves_nothing_and_says_so(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], taxRate=16)], taxRate=0.16)       # für 16 % ist kein Erlöskonto hinterlegt
    assert world.db.revenue_coa_choice_needed(0.16) is True
    _send_without_move(s, world, 'Die Zahlungen der stornierten Rechnung T-100 (50,00 €) wurden nicht übernommen: Für den '
                                 'Steuersatz dieser Rechnung ist kein Erlöskonto hinterlegt. Bitte die Zuordnung dort '
                                 'entfernen und hier mit Erlöskonto zuordnen.')
    assert columns(world.db, s.r2)['Status'] == 'sent' and ledger(world.db) == BEFORE   # wie vor dem Versand (16 %: keine Forderung)
    assert world.db.find_payment_inconsistencies() == []


@pytest.mark.parametrize('over', [{'customerId': 'cust2'}, {'currency': 'USD'}], ids=['anderer-Kunde', 'andere-Waehrung'])
def test_another_customer_or_currency_moves_nothing(world, monkeypatch, over):
    s = paid_scenario(world, monkeypatch)
    over = {key: (getattr(world, value) if value == 'cust2' else value) for key, value in over.items()}
    edit_draft(world, s.r2, ONE_ITEM, **over)
    _send_without_move(s, world, 'Die Zahlungen der stornierten Rechnung T-100 (50,00 €) bleiben dort: Kunde oder Währung '
                                 'dieser Rechnung weichen ab.')
    assert world.db.find_payment_inconsistencies() == []


NOT_AUTOMATIC = ('Es wurden keine Zahlungen der stornierten Rechnung T-100 übernommen. 50,00 € ließen sich nicht '
                 'automatisch übernehmen (Import oder Sammelbuchung) und bleiben an der stornierten Rechnung.')


def test_an_import_allocation_without_postings_stays(world, monkeypatch):
    """Eine Zuordnung ohne Sätze (WISO-Import, ``link_invoice_to_transaction``) lässt sich nicht sauber entfernen."""
    r1 = sent(world)
    add_payment(world, r1, 50.0)
    cancel(world, r1, '2026-07-02')
    s = SimpleNamespace(r1=r1, r2=reissue(world.db, r1, monkeypatch))
    _send_without_move(s, world, NOT_AUTOMATIC)
    assert columns(world.db, s.r2)['Status'] == 'sent'


def test_a_payment_with_postings_of_an_import_stays(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    run_sql(world.db, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-1' WHERE ParentBooking_ID = ? "
                      "AND Status = 'invoice_payment'", s.booking)
    _send_without_move(s, world, NOT_AUTOMATIC)


def test_one_extra_posting_with_a_source_group_under_the_movement_keeps_the_payment(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    world.db.insert_booking('2026-07-01', 1.0, booking_type='entry', parent_booking_id=s.booking, coa_id=world.db.get_coa_id_by_account_number(4400),
                            counter_coa_id=world.db.get_coa_id_by_account_number(1800))
    run_sql(world.db, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-1' WHERE ParentBooking_ID = ? "
                      "AND Status IS NOT 'invoice_payment'", s.booking)
    _send_without_move(s, world, NOT_AUTOMATIC)


def test_a_posting_edited_by_hand_keeps_the_payment_where_it_is(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    run_sql(world.db, "UPDATE Bookings SET Amount = 400000 WHERE ParentBooking_ID = ? AND EntryForm = 'liquid_first'",
            s.booking)                                                        # ein Satz von Hand geändert
    _send_without_move(s, world, NOT_AUTOMATIC)


def test_an_overbooked_movement_stays(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    other = sent(world, number='T-200')
    run_sql(world.db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 500000, '
                      "'2026-07-01')", other, s.booking)                       # 100,00 € zugeordnet, 50,00 € gebucht
    assert world.db.get_booking_unallocated_amount(s.booking) == 0
    _send_without_move(s, world, NOT_AUTOMATIC)
    assert allocations(world.db, other) == [(s.booking, 500000, '2026-07-01')]


def test_a_bank_account_without_an_skr_number_stays(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    run_sql(world.db, 'UPDATE Accounts SET SKRAccount = NULL WHERE ID = ?', world.bank)
    _send_without_move(s, world, NOT_AUTOMATIC)


def test_a_mix_moves_what_it_can_and_names_the_rest(world, monkeypatch):
    r1 = sent(world)
    app_booking = pay(world, r1, 30.0)
    imported = add_payment(world, r1, 20.0)                                   # ohne Sätze: bleibt
    cancel(world, r1, '2026-07-02')
    r2 = reissue(world.db, r1, monkeypatch)
    findings_before = [(f['category'], f['invoice_number']) for f in world.db.find_payment_inconsistencies()]

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200 and body['hint'] == ('Zahlungen der stornierten Rechnung T-100 über 30,00 € sind jetzt dieser '
                                            'Rechnung zugeordnet. 20,00 € ließen sich nicht automatisch übernehmen '
                                            '(Import oder Sammelbuchung) und bleiben an der stornierten Rechnung.')
    assert allocations(world.db, r2) == [(app_booking, 300000, '2026-07-01')]
    assert allocations(world.db, r1) == [(imported, 200000, '2026-07-01')]
    assert world.db.get_storno_credits() == {world.db.find_storno_invoice(r1)[0]: 20}
    # die Prüfliste kennt die Zuordnung ohne Sätze (so liefert sie ein Import ohne Quellklammer) auch ohne den Umzug:
    # Der Umzug fügt keinen Befund hinzu und nimmt keinen weg
    assert findings_before and findings_before == [(f['category'], f['invoice_number'])
                                                   for f in world.db.find_payment_inconsistencies()]


def test_a_negative_allocation_stays_and_is_not_counted_in_the_hint(world, monkeypatch):
    """Negative Zuordnungen kennt nur der WISO-Import (``link_invoice_to_transaction``): Sie bleiben liegen, und der
    Hinweis rechnet mit den positiven Zahlungen."""
    r1 = sent(world)
    app_booking = pay(world, r1, 30.0)
    refund = add_payment(world, r1, -10.0)
    cancel(world, r1, '2026-07-02')
    r2 = reissue(world.db, r1, monkeypatch)

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200 and body['hint'] == ('Zahlungen der stornierten Rechnung T-100 über 30,00 € sind jetzt dieser '
                                            'Rechnung zugeordnet.')
    assert allocations(world.db, r2) == [(app_booking, 300000, '2026-07-01')]
    assert allocations(world.db, r1) == [(refund, -100000, '2026-07-01')]


def test_a_collective_payment_moves_only_its_share(world, monkeypatch):
    """100,00 € überwiesen: 50,00 € für R1, 50,00 € für eine andere Rechnung X – nur der Teil von R1 zieht um."""
    db = world.db
    r1, other = sent(world), sent(world, number='T-200')
    booking = db.insert_booking('2026-07-01', 100.0, booking_type='bank', account_id=world.bank,
                                recipient_client=CUSTOMER_TEXT)
    assert handlers.link_booking_to_invoice_capped(db, r1, booking, 50.0)[0]
    assert handlers.link_booking_to_invoice_capped(db, other, booking, 50.0)[0]
    other_before = (allocations(db, other), payment_rows(db, 'T-200'))
    cancel(world, r1, '2026-07-02')
    r2 = reissue(db, r1, monkeypatch)

    code, body = set_invoice_status(r2, 'sent')

    assert code == 200 and body['hint'] == HINT_ALL
    assert allocations(db, r2) == [(booking, 500000, '2026-07-01')] and allocations(db, r1) == []
    assert (allocations(db, other), payment_rows(db, 'T-200')) == other_before    # die andere Zuordnung bleibt, wie sie ist
    assert db.get_booking_unallocated_amount(booking) == 0                       # frei bleibt nichts
    assert db.find_payment_inconsistencies() == []


def test_a_replacement_that_is_already_covered_takes_nothing(world, monkeypatch):
    """Hat die neue Rechnung keinen offenen Betrag (hier 0 €), bleibt alles an der stornierten."""
    s = paid_scenario(world, monkeypatch)
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], unitPrice=0.0)])
    code, body = set_invoice_status(s.r2, 'sent')
    assert code == 200 and body['hint'].startswith('Es wurden keine Zahlungen der stornierten Rechnung T-100 übernommen.')
    assert 'bleiben als Guthaben des Kunden' in body['hint'] and allocations(world.db, s.r2) == []
    assert allocations(world.db, s.r1) == [(s.booking, 500000, '2026-07-01')]


@pytest.mark.parametrize('paid', [None, 0.0], ids=['nie-bezahlt', 'ohne-Zahlung'])
def test_without_payments_nothing_moves_and_there_is_no_hint(world, monkeypatch, paid):
    s = paid_scenario(world, monkeypatch, paid=paid)
    code, body = set_invoice_status(s.r2, 'sent')
    assert (code, body) == (200, {'success': True, 'invoice_id': s.r2})
    assert allocations(world.db, s.r2) == [] and columns(world.db, s.r2)['Status'] == 'sent'
    # eine gewöhnliche Rechnung (ohne ``ReplacesInvoiceId``) zieht ohnehin nichts um
    other = make_invoice(world, 'draft', number='T-200')
    assert set_invoice_status(other, 'sent') == (200, {'success': True, 'invoice_id': other})


def test_only_the_send_moves_not_the_other_transitions(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    assert set_invoice_status(s.r2, 'finalized')[0] == 200                    # Entwurf → abgeschlossen: nichts
    assert allocations(world.db, s.r1) != [] and allocations(world.db, s.r2) == []
    code, body = set_invoice_status(s.r2, 'sent')                             # abgeschlossen → versendet: Umzug
    assert code == 200 and body['hint'] == HINT_ALL
    assert columns(world.db, s.r2)['Status'] == 'partial_payment'
    assert set_invoice_status(s.r2, 'overdue')[0] == 409                      # Teilzahlung → überfällig gibt es nicht
    before = snapshot(world.db)
    assert set_invoice_status(s.r2, 'partial_payment') == (200, {'success': True, 'invoice_id': s.r2})   # derselbe Status
    assert snapshot(world.db) == before


def test_update_invoice_status_the_unchecked_building_block_never_moves(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    world.db.update_invoice_status(s.r2, 'sent')
    assert allocations(world.db, s.r1) != [] and allocations(world.db, s.r2) == []


@pytest.mark.parametrize('what', ['rate', 'replaced'], ids=['Steuersatz', 'ersetzte-Rechnung'])
def test_the_stale_context_is_refused(world, monkeypatch, what):
    """Ändert sich der Steuersatz der neuen Rechnung oder die ersetzte Rechnung zwischen dem Auflösen der Konten und der
    Transaktion, wird abgelehnt (409, nichts geschrieben): Das aufgelöste Erlöskonto gälte nicht mehr."""
    s = paid_scenario(world, monkeypatch)
    original = type(world.db)._payment_move_context
    rate_before = columns(world.db, s.r2)['TaxRate']

    def resolving_while_it_changes(self, invoice_id, new):
        context = original(self, invoice_id, new)
        if what == 'rate':
            run_sql(world.db, 'UPDATE Invoices SET TaxRate = 0.07 WHERE ID = ?', invoice_id)
        else:
            context = {**context, 'replaced_id': context['replaced_id'] + 1}
        return context
    monkeypatch.setattr(type(world.db), '_payment_move_context', resolving_while_it_changes)
    before = snapshot(world.db)

    change = world.db.set_invoice_status(s.r2, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, CHANGED_MESSAGE)
    run_sql(world.db, 'UPDATE Invoices SET TaxRate = ? WHERE ID = ?', rate_before, s.r2)
    assert snapshot(world.db) == before


# ── Ganz oder gar nicht ──────────────────────────────────────────────────────

@pytest.mark.parametrize('failing_call', [1, 2], ids=['neue-Zuordnung', 'Rest-an-der-stornierten'])
def test_a_failing_assignment_rolls_the_whole_send_back(world, monkeypatch, failing_call):
    s = paid_scenario(world, monkeypatch)
    db = world.db
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], unitPrice=33.61)])              # 40,00: es gibt eine zweite Zuordnung (Rest)
    before = snapshot(db)
    original, calls = type(db)._assign_in_tx, []

    def failing(self, tx, *args, **kwargs):
        calls.append(args[0])
        if len(calls) == failing_call:
            return PaymentAssignment(False, 'Kein offener Betrag zuordenbar (Rechnung oder Buchung ausgeschöpft)', None, None)
        return original(self, tx, *args, **kwargs)
    monkeypatch.setattr(type(db), '_assign_in_tx', failing)

    code, body = set_invoice_status(s.r2, 'sent')

    assert code == 409 and body['success'] is False
    assert body['error'] == ('Die Zahlungen der stornierten Rechnung T-100 ließen sich nicht übernehmen: Kein offener Betrag '
                             'zuordenbar (Rechnung oder Buchung ausgeschöpft). Die Rechnung wurde nicht versendet.')
    assert len(calls) == failing_call
    assert snapshot(db) == before                                             # Zuordnungen, Sätze, Protokoll: wie vorher
    row = columns(db, s.r2)
    assert (row['Status'], row['SentAt']) == ('draft', None) and bookings_of(db, row['InvoiceNumber']) == []
    assert allocations(db, s.r1) == [(s.booking, 500000, '2026-07-01')]
    monkeypatch.setattr(type(db), '_assign_in_tx', original)
    assert set_invoice_status(s.r2, 'sent')[0] == 200                         # ohne den Fehler geht es


def test_a_wrong_amount_rolls_the_whole_send_back_too(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    db = world.db
    before = snapshot(db)
    original = type(db)._assign_in_tx

    def writes_less(self, tx, invoice_id, booking_id, requested, ctx, allow_cancelled=False):
        return original(self, tx, invoice_id, booking_id, requested - 10000, ctx, allow_cancelled)
    monkeypatch.setattr(type(db), '_assign_in_tx', writes_less)

    code, body = set_invoice_status(s.r2, 'sent')

    assert code == 409 and 'Der zugeordnete Betrag weicht ab.' in body['error'] and snapshot(db) == before


def test_the_write_lock_is_held_while_the_payments_move(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    seen = []
    original = type(world.db)._move_replaced_payments

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
    monkeypatch.setattr(type(world.db), '_move_replaced_payments', watching)
    assert set_invoice_status(s.r2, 'sent')[0] == 200
    assert seen == [True]


# ── Nach dem Umzug ───────────────────────────────────────────────────────────

def test_after_the_move_the_replacement_stays_sent_and_a_removed_payment_does_not_go_back(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    db = world.db
    assert set_invoice_status(s.r2, 'sent')[0] == 200
    r2_number = columns(db, s.r2)['InvoiceNumber']

    code, body = set_invoice_status(s.r2, 'draft')                            # mit Zahlung: nicht zurückholbar
    assert code == 409 and body['error'] == ('Eine Rechnung mit Zahlungen lässt sich nicht zurück auf Entwurf oder '
                                             'Abgeschlossen setzen.')
    code, body = set_invoice_status(s.r2, 'finalized')
    assert code == 409

    db.unassign_invoice_payment(allocations_ids(db, s.r2)[0])                 # von Hand entfernen
    assert allocations(db, s.r1) == [] and allocations(db, s.r2) == []        # nichts wandert zurück
    assert db.get_booking_unallocated_amount(s.booking) == 50                 # der Betrag ist freier Rest der Bewegung …
    assert db.get_open_items_for_contact(world.cust)['credits'][0][5] == 50   # … und Guthaben in den offenen Posten
    assert columns(db, s.r2)['Status'] == 'sent' and db.find_payment_inconsistencies() == []
    assert ledger(db)[DEBTORS] == 1190000 and payment_rows(db, r2_number) == []

    # zurückholen und noch einmal versenden: Es hängt nichts mehr an R1, also zieht nichts um
    assert set_invoice_status(s.r2, 'draft')[0] == 200
    code, body = set_invoice_status(s.r2, 'sent')
    assert (code, body) == (200, {'success': True, 'invoice_id': s.r2})
    assert allocations(db, s.r1) == [] and allocations(db, s.r2) == []
    assert db.find_payment_inconsistencies() == []


def allocations_ids(db, invoice_id):
    return [r['ID'] for r in query_rows(db, 'SELECT ID FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID', invoice_id)]


def test_a_second_send_after_taking_the_payment_back_moves_what_still_hangs_on_the_cancelled_invoice(world, monkeypatch):
    """Im Fall b bleiben 10,00 € an R1. Wird die Zahlung an R2 entfernt und R2 erneut versendet, zieht der Rest um –
    einmal, nichts doppelt."""
    s = paid_scenario(world, monkeypatch)
    db = world.db
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], unitPrice=33.61)])
    assert set_invoice_status(s.r2, 'sent')[0] == 200
    db.unassign_invoice_payment(allocations_ids(db, s.r2)[0])
    assert allocations(db, s.r1) == [(s.booking, 100000, '2026-07-01')]
    assert set_invoice_status(s.r2, 'draft')[0] == 200

    code, body = set_invoice_status(s.r2, 'sent')

    assert code == 200 and body['hint'] == 'Zahlungen der stornierten Rechnung T-100 über 10,00 € sind jetzt dieser Rechnung zugeordnet.'
    assert allocations(db, s.r1) == [] and allocations(db, s.r2) == [(s.booking, 100000, '2026-07-01')]
    assert db.get_booking_unallocated_amount(s.booking) == 40                 # 50 − 10: nichts doppelt
    assert db.find_payment_inconsistencies() == []


def test_the_remaining_payment_of_the_cancelled_invoice_cannot_be_assigned_by_hand(world, monkeypatch):
    """Der Rest an der stornierten Rechnung bleibt für das normale Zuordnen gesperrt (``payment_blocker``)."""
    s = paid_scenario(world, monkeypatch)
    booking = world.db.insert_booking('2026-07-05', 20.0, booking_type='bank', account_id=world.bank)
    assigned = world.db.assign_invoice_payment(s.r1, booking)
    assert (assigned.ok, assigned.message) == (False, 'Die Rechnung ist storniert.')


def test_allow_cancelled_opens_only_a_cancelled_positive_invoice_that_is_no_credit_note(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    db = world.db
    booking = db.insert_booking('2026-07-05', 20.0, booking_type='bank', account_id=world.bank)
    account = db.get_account_by_id(world.bank)
    bank_coa = db.get_coa_id_by_account_number(account[7])

    def assign(invoice_id, allow):
        row = columns(db, invoice_id)
        ctx = {'tax_rate': row['TaxRate'], 'account_id': world.bank, 'bank_coa': bank_coa,
               'revenue_coa': db.get_coa_id_by_account_number(4400), 'bank_coa_ids': db.get_bank_coa_ids()}
        with db.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            return db._assign_in_tx(tx, invoice_id, booking, 100000, ctx, allow_cancelled=allow)

    assert assign(s.s1, True).message == STORNO_PAYMENT_MESSAGE                # die Stornorechnung nie
    assert assign(s.r1, False).message == 'Die Rechnung ist storniert.'
    assert assign(s.r2, True).message.startswith('Die Rechnung ist noch nicht versendet')    # Entwurf: auch nicht
    run_sql(db, 'UPDATE Invoices SET SumGross = 0 WHERE ID = ?', s.r1)
    assert assign(s.r1, True).message == 'Die Rechnung ist storniert.'          # ohne positiven Betrag: nicht
    run_sql(db, 'UPDATE Invoices SET SumGross = 1190000 WHERE ID = ?', s.r1)
    result = assign(s.r1, True)                                               # die stornierte Rechnung mit positivem Betrag
    assert result.ok is True and allocations(db, s.r1)[-1][1] == 100000


def test_the_revenue_account_of_a_payment_is_read_from_its_postings(world, monkeypatch):
    """Das Erlöskonto einer Zuordnung: Gegenkonto der Umbuchung „Wartekonto an Erlöskonto“, sonst Konto des Satzes
    „Erlöskonto an Bank“ (ohne Forderung); ohne eine der beiden Formen gibt es keines."""
    db = world.db
    with_receivable = sent(world)
    booking = pay(world, with_receivable, 50.0)
    items = [dict(ONE_ITEM[0], taxRate=0)]
    without = make_invoice(world, 'draft', number='T-500', items=items, taxRate=0, showTax=False)
    db.update_invoice_status(without, 'sent')                                  # ohne Forderung der App
    second = db.insert_booking('2026-07-02', 100.0, booking_type='bank', account_id=world.bank)
    assert handlers.link_booking_to_invoice_capped(db, without, second, 100.0)[0]
    coa = db.get_coa_id_by_account_number
    conn = db._get_connection()
    try:
        children = db._payment_children(conn, booking, 'T-100')
        assert db._payment_revenue_coa(conn, children) == coa(4400)
        assert db._payment_revenue_coa(conn, db._payment_children(conn, second, 'T-500')) == coa(4185)
        assert db._payment_revenue_coa(conn, []) is None
        liquid_only = [r[0] for r in conn.execute("SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND EntryForm = "
                                                  "'liquid_first'", (booking,))]
        assert db._payment_revenue_coa(conn, liquid_only) is None              # „Bank an Debitoren“ allein nennt keines
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Der Hinweis im Dokument
# ═════════════════════════════════════════════════════════════════════════════

NOTE = 'Ersetzt die stornierte Rechnung T-100 (Stornorechnung 26R001)'


def documents(w, invoice_id):
    return w.db.get_invoice_by_id(invoice_id), w.db.get_invoice_items(invoice_id)


def test_the_note_text_of_a_replacement():
    assert replacement_note_text('R26001', 'R26002') == 'Ersetzt die stornierte Rechnung R26001 (Stornorechnung R26002)'
    assert replacement_note_text('R26001', '') == 'Ersetzt die stornierte Rechnung R26001'
    assert replacement_note_text('R26001', None) == 'Ersetzt die stornierte Rechnung R26001'


def test_the_model_of_a_replacement_carries_the_note_and_stays_an_invoice(world, monkeypatch):
    r1 = sent(world)
    s1 = cancel(world, r1, '2026-07-02')
    r2 = reissue(world.db, r1, monkeypatch)

    model = build_einvoice(world.db, *documents(world, r2))

    assert (model.type_code, model.notes, model.warnings) == ('380', [NOTE], [])
    assert model.preceding_invoice_number == '' and model.preceding_invoice_date is None
    assert build_einvoice(None, *documents(world, r2)).notes == []              # ohne Datenbank gibt es keinen Hinweis
    assert build_einvoice(world.db, *documents(world, r1)).notes == []           # weder die stornierte Rechnung …
    assert build_einvoice(world.db, *documents(world, s1.storno_id)).notes == [  # … noch die Stornorechnung
        'Storno zu Rechnung T-100 vom 07.06.2026']


@pytest.mark.parametrize('value', [None, 0, -3, True, 'abc', 99999, 3.5], ids=str)
def test_only_a_reference_to_an_existing_invoice_makes_a_note(world, monkeypatch, value):
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    r2 = reissue(world.db, r1, monkeypatch)
    row = list(world.db.get_invoice_by_id(r2))
    assert replacement_note(world.db, tuple(row)) == NOTE
    row[52] = value
    assert replacement_note(world.db, tuple(row)) is None
    assert replacement_note(world.db, tuple(row[:52])) is None                  # eine kurze Zeile (alter Zuschnitt)


def test_a_replacement_without_its_credit_note_names_only_the_invoice_in_the_note(world, monkeypatch):
    r1 = sent(world)
    s1 = cancel(world, r1, '2026-07-02')
    r2 = reissue(world.db, r1, monkeypatch)
    run_sql(world.db, 'UPDATE Invoices SET CancelsInvoiceId = NULL WHERE ID = ?', s1.storno_id)
    assert replacement_note(world.db, world.db.get_invoice_by_id(r2)) == 'Ersetzt die stornierte Rechnung T-100'


def test_the_sent_replacement_carries_the_note_in_pdf_cii_and_ubl(world, monkeypatch):
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    r2 = reissue(world.db, r1, monkeypatch)
    assert set_invoice_status(r2, 'sent')[0] == 200
    row = columns(world.db, r2)

    page = pdf_text(row['PDFPath'])
    assert NOTE in page.replace('\n', ' ') and page.index('Ersetzt die stornierte Rechnung') < page.index('Pos. Menge')
    assert 'Berichtigte Fassung' not in page and 'Fassung:' not in page
    cii = ET.fromstring(embedded_xml(read_file(row['PDFPath'])))
    assert text_of(cii, 'rsm:ExchangedDocument/ram:IncludedNote/ram:Content') == NOTE           # BT-22
    assert text_of(cii, 'rsm:ExchangedDocument/ram:TypeCode') == '380'
    ubl = ET.fromstring(read_file(row['XMLPath']))
    ns = {'cbc': UBL['cbc']}
    assert ubl.find('cbc:Note', ns).text == NOTE and ubl.find('cbc:InvoiceTypeCode', ns).text == '380'
    names = [child.tag.split('}')[1] for child in ubl]
    assert names.index('Note') == names.index('InvoiceTypeCode') + 1 and names.index('Note') < names.index(
        'DocumentCurrencyCode')                                                # Reihenfolge laut UBL-Schema
    # die stornierte Rechnung (eine gewöhnliche Rechnung) trägt keinen Hinweis
    old = columns(world.db, r1)
    assert 'Ersetzt' not in pdf_text(old['PDFPath']) and b'IncludedNote' not in embedded_xml(read_file(old['PDFPath']))
    assert ET.fromstring(read_file(old['XMLPath'])).findall('cbc:Note', ns) == []


def test_a_document_generated_later_carries_the_note_from_the_database_state(world, monkeypatch):
    """Auch „PDF + E-Rechnung“ für den Entwurf schreibt den Hinweis: Er ergibt sich aus dem Stand, nicht aus einem Parameter."""
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    r2 = reissue(world.db, r1, monkeypatch)
    result = invoice_files.generate_invoice_documents(world.db, r2)
    assert result['error'] is None
    assert NOTE in pdf_text(result['pdf_path']).replace('\n', ' ')
    assert NOTE.encode() in read_file(result['xml_path'])


def test_a_corrected_replacement_carries_both_notes_the_replacement_first(world, monkeypatch):
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    r2 = reissue(world.db, r1, monkeypatch)
    assert set_invoice_status(r2, 'sent')[0] == 200
    save_open(world, r2, deliveryDate='2026-06-01')                              # Berichtigung: Fassung 2
    row = columns(world.db, r2)
    correction = world.db.correction_notice(r2)
    assert correction is not None

    model = build_einvoice(world.db, *documents(world, r2))
    assert len(model.notes) == 2 and model.notes[0] == NOTE and model.notes[1].startswith('Berichtigte Fassung vom')
    page = pdf_text(row['PDFPath']).replace('\n', ' ')
    assert NOTE in page and 'Fassung: 2' in page
    assert page.index(NOTE) < page.index('Berichtigte Fassung vom') < page.replace('\n', ' ').index('Pos. Menge')
    cii = ET.fromstring(embedded_xml(read_file(row['PDFPath'])))
    contents = [n.text for n in cii.findall('rsm:ExchangedDocument/ram:IncludedNote/ram:Content', CII)]
    assert contents == model.notes
    ubl = ET.fromstring(read_file(row['XMLPath']))
    assert [n.text for n in ubl.findall('cbc:Note', {'cbc': UBL['cbc']})] == model.notes
    assert text_of(cii, 'rsm:ExchangedDocument/ram:TypeCode') == '380' and row['InvoiceNumber'] == '26R002'


def test_the_notes_of_a_replacement_stand_where_the_schemas_want_them(world, monkeypatch):
    """Zwei Hinweise (BT-22) stehen im Schema direkt nach dem Ausstellungsdatum."""
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    r2 = reissue(world.db, r1, monkeypatch)
    root = ET.fromstring(generate_cii(*documents(world, r2), db=world.db))
    document = root.find('rsm:ExchangedDocument', CII)
    assert [child.tag.split('}')[1] for child in document] == ['ID', 'TypeCode', 'IssueDateTime', 'IncludedNote']
    ubl = ET.fromstring(XRechnungGenerator().generate_xml(*documents(world, r2), db=world.db))
    assert [n.text for n in ubl.findall('cbc:Note', {'cbc': UBL['cbc']})] == [NOTE]      # der Zahlungshinweis ist ein anderes Note


# ═════════════════════════════════════════════════════════════════════════════
# Das Guthaben eines Storno-Paares
# ═════════════════════════════════════════════════════════════════════════════

def _list_cells(world):
    """Die Zellen der Rechnungsliste: Nummer → Zellen der Zeile."""
    html = PageInvoice(world.db, {'date_from': '2000-01-01', 'date_to': '2100-12-31'})
    rows = [[cell for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
            for row in html.split('<tr>')[2:]]
    return {re.sub('<.*?>', '', r[0]): r for r in rows if r}, html


def _plain(cell):
    return re.sub('<.*?>', '', cell)


def test_the_credit_of_a_pair_is_the_sum_of_the_payments_on_both_documents(world, monkeypatch):
    db = world.db
    r1 = sent(world)
    other = sent(world, number='T-200')
    assert db.get_storno_credits() == {} and db.get_storno_credit(r1) is None
    s1 = cancel(world, r1, '2026-07-02').storno_id
    assert db.get_storno_credits() == {s1: 0} and db.get_storno_credit(r1) == db.get_storno_credit(s1) == 0
    assert db.get_storno_credit(other) is None and db.get_storno_credit(99999) is None

    booking = pay_after_cancel(world, r1, 50.0)

    assert db.get_storno_credits() == {s1: 50} and db.get_storno_credit(r1) == db.get_storno_credit(s1) == 50
    assert db.get_storno_credit(other) is None
    # alle Zuordnungen beider Belege zählen – auch eine spätere Gegenbuchung an der Stornorechnung (negativ)
    run_sql(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, -200000, '
                "'2026-07-09')", s1, booking)
    assert db.get_storno_credits() == {s1: 30} and db.get_storno_credit(s1) == 30


def test_the_list_says_what_is_still_to_be_refunded(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    cells, _html = _list_cells(world)
    credit_row = cells['26R001']
    assert 'Erstattung offen: 50,00 €' in _plain(credit_row[5]) and 'Stornorechnung' not in _plain(credit_row[5])
    assert _plain(cells['T-100'][5]) == 'Storniert' and _plain(cells['26R002'][5]) == 'Entwurf'
    assert '-119,00 €' in _plain(credit_row[4])                                 # die Beträge bleiben, wie sie waren

    assert set_invoice_status(s.r2, 'sent')[0] == 200                           # alles zieht um: nichts mehr offen
    assert _plain(_list_cells(world)[0]['26R001'][5]) == 'Stornorechnung'
    # ein Paar, auf das nie gezahlt wurde, trägt die Kennzeichnung ohnehin
    unpaid = cancel(world, sent(world, number='T-200'), '2026-07-10')
    cells, html = _list_cells(world)
    assert _plain(cells[unpaid.storno_number][5]) == 'Stornorechnung' and 'Erstattung offen' not in html


def test_a_partly_moved_payment_leaves_the_rest_as_the_refund(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], unitPrice=33.61)])
    assert set_invoice_status(s.r2, 'sent')[0] == 200
    assert 'Erstattung offen: 10,00 €' in _plain(_list_cells(world)[0]['26R001'][5])


def test_both_pages_name_the_credit_and_neither_does_without_payments(world, monkeypatch):
    db = world.db
    s = paid_scenario(world, monkeypatch)
    row = 'Guthaben des Kunden: 50,00 € – Zahlungen auf die stornierte Rechnung'
    for invoice_id in (s.r1, s.s1):
        html = PageInvoice(db, {}, invoice_id)
        assert f'<tr><td>Storno:</td><td>{row}</td></tr>' in html, invoice_id
    assert 'Guthaben des Kunden' not in PageInvoice(db, {}, s.r2)                # die Ersatzrechnung hat keines
    assert 'Guthaben des Kunden' not in PageInvoice(db, {}, sent(world, number='T-200'))

    assert set_invoice_status(s.r2, 'sent')[0] == 200
    assert 'Guthaben des Kunden' not in PageInvoice(db, {}, s.r1) and 'Guthaben des Kunden' not in PageInvoice(
        db, {}, s.s1)

    clean = sent(world, number='T-300')
    cancel(world, clean, '2026-07-03')
    assert 'Guthaben des Kunden' not in PageInvoice(db, {}, clean)


def test_the_open_items_of_the_contact_carry_the_credits_of_its_cancelled_invoices(world, monkeypatch):
    db = world.db
    s = paid_scenario(world, monkeypatch)
    open_invoice = sent(world, number='T-101')                                  # 119,00 € offen
    other_customer = sent(world, number='T-102', customerId=world.cust2)
    other_storno = cancel(world, other_customer, '2026-07-10').storno_id
    pay_after_cancel(world, other_customer, 20.0)

    items = db.get_open_items_for_contact(world.cust)

    assert items['storno_credits'] == [(s.s1, '26R001', '2026-07-02', s.r1, 'T-100', 50)]    # nur der Kunde selbst
    assert [r[0] for r in items['open_invoices']] == [open_invoice]
    assert items['saldo'] == 69                                                  # 119 offen − 50 Guthaben aus der Stornierung
    assert db.get_open_items_for_contact(world.cust2)['storno_credits'][0][0] == other_storno
    assert db.get_open_items_for_contact(world.cust2)['saldo'] == -20

    assert set_invoice_status(s.r2, 'sent')[0] == 200                           # das Guthaben zieht um, die Forderung wächst
    items = db.get_open_items_for_contact(world.cust)
    assert items['storno_credits'] == [] and items['saldo'] == 119 + 69 - 0     # R2 mit 69,00 offen, T-101 mit 119,00


def test_several_credits_are_listed_with_the_newest_credit_note_first_and_empty_ones_stay_out(world):
    db = world.db
    first, second, third = (sent(world, number=f'T-{n}') for n in (110, 111, 112))
    s_first = cancel(world, first, '2026-07-02').storno_id
    s_second = cancel(world, second, '2026-07-10').storno_id
    cancel(world, third, '2026-07-20')                                          # ohne Zahlung: kein Guthaben
    pay_after_cancel(world, first, 10.0)
    pay_after_cancel(world, second, 5.0)
    items = db.get_open_items_for_contact(world.cust)
    assert [(r[0], r[5]) for r in items['storno_credits']] == [(s_second, 5), (s_first, 10)]
    assert items['saldo'] == -15


def test_the_contact_page_shows_the_credits_and_the_balance_text(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    html = PageContacts(world.db, edit_contact_id=world.cust)
    assert '<h4>Guthaben aus stornierten Rechnungen</h4>' in html
    assert (f'<a href="/invoice/edit?id={s.s1}">Stornorechnung 26R001</a> zu Rechnung '
            f'<a href="/invoice/edit?id={s.r1}">T-100</a>') in html
    assert '50,00 €' in html and '2026-07-02' in html
    assert '− Guthaben aus nicht kontierten Zahlungen und stornierten Rechnungen)' in html and 'Saldo:' in html
    assert 'Guthaben aus stornierten Rechnungen' not in PageContacts(world.db, edit_contact_id=world.cust2)

    assert set_invoice_status(s.r2, 'sent')[0] == 200
    assert 'Guthaben aus stornierten Rechnungen</h4>' not in PageContacts(world.db, edit_contact_id=world.cust)


def test_the_contact_page_escapes_the_numbers_of_the_credit(world, monkeypatch):
    r1 = sent(world, number='T-<b>x</b>')
    cancel(world, r1, '2026-07-02')
    pay_after_cancel(world, r1, 10.0)
    html = PageContacts(world.db, edit_contact_id=world.cust)
    assert 'T-<b>x</b>' not in html and 'T-&lt;b&gt;x&lt;/b&gt;' in html


# ═════════════════════════════════════════════════════════════════════════════
# Die Stornorechnung nachträglich
# ═════════════════════════════════════════════════════════════════════════════

def _cancelled_by_hand_with_receivable(w, paid=50.0):
    """Altbestand: von Hand storniert (``update_invoice_status``), die Forderung steht noch – mit einer Zahlung dran."""
    invoice_id = sent(w)
    if paid:
        pay(w, invoice_id, paid)
    w.db.update_invoice_status(invoice_id, 'cancelled')
    return invoice_id


def test_a_hand_cancelled_invoice_with_its_receivable_gets_the_credit_note_and_the_counter_booking(world):
    db = world.db
    r1 = _cancelled_by_hand_with_receivable(world)
    log_before = protocol(db, r1)
    row_before = {k: v for k, v in columns(db, r1).items() if k != 'UpdatedAt'}
    assert balance(db, DEBTORS) == 690000 and [r[3] for r in bookings_of(db, 'T-100')] == [1190000]   # F (119) − Zahlung (50)

    storno = cancel(world, r1, '2026-07-02')

    assert storno.counter_booked is True and storno.storno_number == '26R001'
    assert {k: v for k, v in columns(db, r1).items() if k != 'UpdatedAt'} == row_before     # die Rechnung bleibt, wie sie ist
    assert protocol(db, r1) == log_before                                                   # keine Statuszeile der Rechnung
    assert [(r['OldValue'], r['NewValue']) for r in protocol(db, storno.storno_id, 'status')] == [(None, 'sent')]
    assert [r[1:5] for r in bookings_of(db, '26R001')] == [(DEBTORS, WAITING, -1190000, -190000)]    # die Gegenbuchung
    assert balance(db, DEBTORS) == -500000 and balance(db, WAITING) == 500000               # nur noch die Zahlung offen
    assert columns(db, storno.storno_id)['CancelsInvoiceId'] == r1 and db.find_storno_invoice(r1)[0] == storno.storno_id
    assert db.get_storno_credits() == {storno.storno_id: 50}
    assert db.find_payment_inconsistencies() == []


def test_a_revived_hand_cancelled_invoice_is_not_possible_after_the_credit_note(world):
    r1 = _cancelled_by_hand_with_receivable(world)
    from db.invoice_lock import HAS_STORNO_MESSAGE, allowed_targets
    assert allowed_targets('cancelled', 1, False, 1190000) == ['sent', 'overdue']      # vorher: wiederbeleben
    cancel(world, r1, '2026-07-02')
    before = snapshot(world.db)
    for target in ('sent', 'overdue'):
        change = world.db.set_invoice_status(r1, target)
        assert (change.ok, change.code, change.message) == (False, 409, HAS_STORNO_MESSAGE)
    assert snapshot(world.db) == before and allowed_targets('cancelled', 1, True, 1190000) == []


def test_without_a_receivable_there_is_no_counter_booking_and_the_answer_says_so(world):
    db = world.db
    r1 = sent(world)
    db.update_invoice_status(r1, 'cancelled')
    assert db.remove_receivable_booking(r1) is True                                  # wie der Altbestand ohne Forderung
    assert bookings_of(db, 'T-100') == []

    code, text = handlers.handle_invoice_cancel(_cancel_body(r1))

    body = json.loads(text)
    assert code == 200 and body['success'] is True and body['hint'] == NO_COUNTER_BOOKING_HINT
    assert bookings_of(db, body['storno_number']) == [] and columns(db, r1)['Status'] == 'cancelled'
    assert columns(db, body['storno_id'])['SumGross'] == -1190000 and balance(db, DEBTORS) == 0
    assert db.find_payment_inconsistencies() == []


def test_the_late_credit_note_can_be_asked_for_together_with_a_new_draft(world, monkeypatch):
    r1 = _cancelled_by_hand_with_receivable(world, paid=None)
    at(monkeypatch, TODAY)
    code, text = handlers.handle_invoice_cancel(_cancel_body(r1, reissue=True))
    body = json.loads(text)
    assert code == 200 and columns(world.db, body['replacement_id'])['ReplacesInvoiceId'] == r1


@pytest.mark.parametrize('gross', [0, -1190000], ids=['null', 'negativ'])
def test_a_hand_cancelled_invoice_without_a_positive_amount_gets_no_credit_note(world, gross):
    from db.invoice_storno import NO_POSITIVE_AMOUNT_MESSAGE
    r1 = sent(world)
    world.db.update_invoice_status(r1, 'cancelled')
    run_sql(world.db, 'UPDATE Invoices SET SumGross = ? WHERE ID = ?', gross, r1)
    before = tables(world.db)
    storno = world.db.cancel_invoice(r1, '2026-07-02', USER)
    assert (storno.ok, storno.code, storno.message) == (False, 409, NO_POSITIVE_AMOUNT_MESSAGE)
    assert tables(world.db) == before


def test_the_late_credit_note_is_not_dated_before_the_invoice(world):
    from db.invoice_storno import STORNO_DATE_BEFORE_MESSAGE
    r1 = _cancelled_by_hand_with_receivable(world, paid=None)
    before = tables(world.db)
    storno = world.db.cancel_invoice(r1, '2026-06-06', USER)
    assert (storno.ok, storno.code, storno.message) == (False, 400, STORNO_DATE_BEFORE_MESSAGE)
    assert tables(world.db) == before


def test_the_late_credit_note_is_made_once_only(world):
    r1 = _cancelled_by_hand_with_receivable(world, paid=None)
    first = cancel(world, r1, '2026-07-02')
    before = tables(world.db)
    again = world.db.cancel_invoice(r1, '2026-07-03', USER)
    assert (again.ok, again.code, again.message) == (False, 409, 'Die Rechnung ist bereits storniert.')
    assert tables(world.db) == before and world.db.find_storno_invoice(r1)[0] == first.storno_id


def test_the_page_offers_the_late_credit_note_only_where_it_applies(world):
    db = world.db
    alt = _cancelled_by_hand_with_receivable(world, paid=None)
    html = PageInvoice(db, {}, alt)
    assert f'cancelInvoice({alt})' in _buttons(html) and 'Stornorechnung nachträglich erstellen &hellip;' in html
    assert 'cancelInvoice(' + str(alt) + ', true)' not in html and 'reissueInvoice' not in html
    script = next(s for s in Dom(html).scripts if 'function cancelInvoice(' in s)
    assert json.dumps(late_cancel_question('T-100'), ensure_ascii=False) in script
    assert '"label": "Stornodatum:"' in script and '"min": "2026-06-07"' in script
    assert late_cancel_question('T-100') == ('Zur stornierten Rechnung T-100 eine Stornorechnung erstellen? Sie bekommt '
                                             'eine eigene Nummer und muss dem Kunden zugehen. Steht die Forderung noch '
                                             'in den Büchern, wird sie ausgeglichen. Das lässt sich nicht rückgängig '
                                             'machen.')
    # mit Stornorechnung: kein Knopf mehr zum Stornieren, dafür „Neu ausstellen“
    cancel(world, alt, '2026-07-02')
    html = PageInvoice(db, {}, alt)
    assert 'nachträglich' not in html and f'reissueInvoice({alt})' in _buttons(html)
    # eine Gutschrift (kein positiver Betrag) wird über den Status storniert
    credit = sent(world, number='G-1', items=[dict(ONE_ITEM[0], unitPrice=-100.0)])
    set_invoice_status(credit, 'cancelled')
    html = PageInvoice(db, {}, credit)
    assert 'cancelInvoice(' not in html and 'reissueInvoice' not in html and 'nachträglich' not in html
    # ein Entwurf und eine versendete Rechnung bieten es nicht an
    assert 'nachträglich' not in PageInvoice(db, {}, sent(world, number='T-201'))


def test_the_status_select_of_a_hand_cancelled_invoice_still_offers_reviving_until_the_credit_note_exists(world):
    alt = _cancelled_by_hand_with_receivable(world, paid=None)
    html = PageInvoice(world.db, {}, alt)
    options = re.findall(r'value="(\w+)"', html.split('id="statusChangeSelect"')[1].split('</select>')[0])
    assert options == ['cancelled', 'sent', 'overdue']
    cancel(world, alt, '2026-07-02')
    html = PageInvoice(world.db, {}, alt)
    options = re.findall(r'value="(\w+)"', html.split('id="statusChangeSelect"')[1].split('</select>')[0])
    assert options == ['cancelled']


# ═════════════════════════════════════════════════════════════════════════════
# Die Prüfliste
# ═════════════════════════════════════════════════════════════════════════════

def findings(db, invoice_id=None):
    return [(f['category'], f['invoice_id'], f['invoice_number'], f['text'])
            for f in db.find_payment_inconsistencies(invoice_id)]


def test_a_missing_counter_booking_is_found(world):
    db = world.db
    r1 = sent(world)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    other = sent(world, number='T-200')
    assert findings(db) == []                                                    # ein intaktes Paar ergibt keinen Befund
    run_sql(db, 'DELETE FROM Bookings WHERE ID = ?', bookings_of(db, '26R001')[0][0])

    expected = [('storno_counter_mismatch', r1, 'T-100',
                 'Zur Rechnung steht die Forderung, aber die Gegenbuchung der Stornorechnung 26R001 fehlt – auf dem '
                 'Debitorenkonto bleibt der Betrag stehen.')]
    assert findings(db) == expected
    assert findings(db, r1) == expected and findings(db, s1) == expected         # beide Belege des Paares zeigen es
    assert findings(db, other) == []                                             # eine andere Rechnung nicht


def test_a_missing_receivable_is_found(world):
    db = world.db
    r1 = sent(world)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    run_sql(db, 'DELETE FROM Bookings WHERE ID = ?', bookings_of(db, 'T-100')[0][0])
    expected = [('storno_counter_mismatch', r1, 'T-100',
                 'Die Stornorechnung 26R001 hat eine Gegenbuchung, aber zur Rechnung steht keine Forderung der App – '
                 'auf dem Debitorenkonto steht ein Haben-Überhang.')]
    assert findings(db) == expected and findings(db, r1) == expected and findings(db, s1) == expected


def test_only_receivables_of_the_app_count_so_an_import_raises_no_false_alarm(world):
    """Ein Import (Quellklammer) hat weder Forderung noch Gegenbuchung der App: Sind beide Seiten importiert oder fehlen
    beide, gibt es keinen Befund; steht nur die Forderung der App, schon."""
    db = world.db
    r1 = sent(world)
    cancel(world, r1, '2026-07-02')
    imported_pair = sent(world, number='T-201')
    cancel(world, imported_pair, '2026-07-02')
    no_receivable = make_invoice(world, 'sent', number='T-200')                  # ein Import: Status, aber keine Forderung
    cancel(world, no_receivable, '2026-07-02')
    run_sql(db, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-2' WHERE DocumentNumber IN ('T-201', '27R001', '26R002')")
    run_sql(db, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-2' WHERE DocumentNumber = ?",
            db.find_storno_invoice(imported_pair)[1])
    assert findings(db) == [] and bookings_of(db, 'T-200') == []               # intakt, beide importiert, keine Forderung

    run_sql(db, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-1' WHERE DocumentNumber = 'T-100'")     # nur die Forderung
    assert [f[0] for f in findings(db)] == ['storno_counter_mismatch']


def test_an_intact_pair_with_payments_and_after_the_move_is_no_finding(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    assert findings(world.db) == []
    assert set_invoice_status(s.r2, 'sent')[0] == 200
    assert findings(world.db) == []


def test_a_hand_cancelled_invoice_whose_receivable_still_stands_is_found_until_the_credit_note_exists(world):
    db = world.db
    r1 = _cancelled_by_hand_with_receivable(world, paid=None)
    other = sent(world, number='T-200')
    # dagegen kein Befund: ohne Forderung, mit importierter Forderung und ohne positiven Betrag (Gutschrift)
    gone = sent(world, number='T-201')
    db.update_invoice_status(gone, 'cancelled')
    assert db.remove_receivable_booking(gone) is True                            # keine Forderung mehr
    imported = sent(world, number='T-202')
    run_sql(db, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-1' WHERE DocumentNumber = 'T-202'")
    db.update_invoice_status(imported, 'cancelled')
    credit = sent(world, number='G-1', items=[dict(ONE_ITEM[0], unitPrice=-100.0)])
    assert set_invoice_status(credit, 'cancelled')[0] == 200                     # die Forderung einer Gutschrift geht mit
    paid_credit = sent(world, number='G-2', items=[dict(ONE_ITEM[0], unitPrice=-100.0)])
    add_payment(world, paid_credit, -119.0)                                      # … außer eine Zahlung hängt dran
    assert set_invoice_status(paid_credit, 'cancelled')[0] == 200
    expected = [('legacy_cancelled_open', r1, 'T-100',
                 'Die Rechnung ist storniert, ihre Forderung steht aber noch in den Büchern (119,00 €). Abhilfe: '
                 '„Stornorechnung nachträglich erstellen“.')]
    assert [f for f in findings(db) if f[0] == 'legacy_cancelled_open'] == expected
    assert [f for f in findings(db, r1) if f[0] == 'legacy_cancelled_open'] == expected
    assert [f for f in findings(db, other) if f[0] == 'legacy_cancelled_open'] == []

    cancel(world, r1, '2026-07-02')

    assert [f for f in findings(db) if f[0] == 'legacy_cancelled_open'] == []   # das Paar ist vollständig


def test_the_check_page_lists_the_new_categories_escaped(world):
    from server.pages_miscellaneous import PageMiscellaneous
    r1 = _cancelled_by_hand_with_receivable(world, paid=None)
    html = PageMiscellaneous(world.db, check_payments=True)
    assert f'<a href="/invoice/edit?id={r1}">T-100</a>' in html
    assert 'Abhilfe: „Stornorechnung nachträglich erstellen“.' in html and 'Keine Unstimmigkeiten' not in html
    hostile = sent(world, number='T-<b>x</b>')
    storno_number = cancel(world, hostile, '2026-07-02').storno_number
    run_sql(world.db, 'DELETE FROM Bookings WHERE ID = ?', bookings_of(world.db, storno_number)[0][0])   # die Gegenbuchung fehlt
    page = PageMiscellaneous(world.db, check_payments=True)
    assert 'T-<b>x</b>' not in page and 'T-&lt;b&gt;x&lt;/b&gt;' in page
