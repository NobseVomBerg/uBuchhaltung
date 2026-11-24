# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Schutz des Skontos (2.9.0): fünf Befunde der Prüfung der Buchungslogik.

1. **Die Steuer der anderen Zuordnungen zählt keine Skonto-Sätze** (``_allocations_tax``): Wird nach einer Teilzahlung A und der schließenden Zahlung B mit Skonto die Teilzahlung entfernt und
   eine Zahlung über denselben Betrag neu zugeordnet, bekam sie 8,36 € statt 7,98 € Steuer (Σ 19,00 € statt 18,62 €), weil der Skonto-Satz (−0,38 €) von B als „gezahlte Steuer“ galt.
2. **Eine Zahlung mit Skonto bleibt, solange eine andere Zuordnung der Rechnung geht**: Jeder Weg, der eine Zahlung entfernt, lehnt ab (``SKONTO_REMOVAL_REFUSED``), und ein zweites
   Skonto an derselben Rechnung wird nie ausgebucht (``SKONTO_ALREADY``).
3. **Ein ungültiger Wert in ``InvoicePayments.SkontoAmount``** (Text, Kommazahl, 0, negativ, Blob – nur über die SQL-Konsole möglich) bricht weder die Prüfliste noch die Rechnungsseite noch den
   Probelauf des Neuaufbaus ab und zählt nie als erledigter Betrag; die Prüfliste nennt ihn mit einem festen Satz ohne den Wert.
4. **Das Stornieren lehnt ab, wenn die Spalte leer ist, die Skonto-Sätze aber stehen** (nach einer regulären Rücknahme ergeben sie 0) und wenn die Spalte einen ungültigen Wert trägt.
5. **Ein Rest, der kein ganzer Cent-Betrag ist**, wird nicht als Skonto ausgebucht (``SKONTO_NOT_CENTS``).

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie da.
"""
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

import server.handlers as handlers
from db import Database
from db.invoice_payments import (SKONTO_ALREADY, SKONTO_NOT_CENTS, SKONTO_NO_REST, SKONTO_REMOVAL_REFUSED, SKONTO_STORNO_EDITED, SKONTO_TOO_LARGE, PaymentRemovalRefused,
                                 settled_sql)
from db.invoice_repair import REBUILD_SKIP_REASONS
from server.handlers import handle_add_transaction, link_booking_to_invoice_capped
from server.pages_invoice import PageInvoice
from tests import installation
from tests.document_helpers import post_json
from tests.dom_helpers import Dom, app_msg_arguments
from tests.entry_form_roundtrip import drop_row, open_form
from tests.http_client import get, post
from tests.susa_helpers import account_id
from tests.test_invoice_storno import books, sent_over_http  # noqa: F401  (books: Fixture mit dem Server der Oberfläche)
from tests.test_skonto_reports import check_every_probe, findings, invoice_of, pay, raw
from tests.test_skonto_reports import db  # noqa: F401  (Fixture: Testdatenbank mit zwei Bankkonten)
from tests.test_skonto_storno import (FIXED_TODAY, NUMBER, TODAY, USER, _reissue_and_send, book_tables, cancel, kids, marked_tax, paid_with_discount, payments, refund, replay_lines,
                                      shape, state)

D = Decimal
CUSTOMER = 'Malermeister Pinsel'
REFUSED = SKONTO_REMOVAL_REFUSED.format(number=NUMBER)


# ── Hilfen ───────────────────────────────────────────────────────────────────

@pytest.fixture
def routes(db, monkeypatch):                                                    # noqa: F811
    """Die Handler der Oberfläche (``handlers.Database()``) arbeiten auf der Testdatenbank."""
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: db)
    return db


def two_payments(db, receivable=True):                                          # noqa: F811
    """Die Rechnung SK-100 (119,00 €, 19 %, Skonto 2 % = 2,38 €): A = Teilzahlung 50,00 € (02.04.), B = 66,62 € mit dem Skonto als Rest (05.05.)."""
    invoice_id = invoice_of(db, NUMBER, 119.0, receivable=receivable)
    a = pay(db, invoice_id, '50.00', '2025-04-02')
    b = pay(db, invoice_id, '66.62', '2025-05-05', skonto=True)
    ids = dict(raw(db, 'SELECT BookingID, ID FROM InvoicePayments WHERE InvoiceID = ?', invoice_id))
    return SimpleNamespace(db=db, invoice=invoice_id, a=a, b=b, pay_a=ids[a], pay_b=ids[b], kids_a=[row.id for row in kids(db, a)], kids_b=[row.id for row in kids(db, b)])


def drop_directly(db, payment_id):                                              # noqa: F811
    """Eine Zuordnung über den Baustein entfernen – am Schutz von ``_refund_removal_blocker`` vorbei: Kein Weg der App lässt es zu (der Umzug und der Neuaufbau rufen ``_unassign`` selbst, und
    ein Eingriff an der App vorbei – SQL-Konsole, Sicherung – hinterließe denselben Stand)."""
    with db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        db._unassign(tx.conn, payment_id)


def taxes_of_the_others(db, invoice_id, number=NUMBER):                         # noqa: F811
    """``(Steuer der Zuordnungen, Rest der Rechnungssteuer)`` in Minor Units – was ``_assign_in_tx`` für den schließenden Teil und ``_refund_plan`` für die Erstattung liest."""
    with closing(db._get_connection()) as conn:
        gross, tax = conn.execute('SELECT SumGross, TaxAmount FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()
        return db._allocations_tax(conn, invoice_id, number, gross, tax), db._remaining_tax(conn, invoice_id, number, gross, tax)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Die Steuer der anderen Zuordnungen zählt keine Skonto-Sätze
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_the_closing_part_after_the_partial_payment_went_takes_the_rest_of_the_invoice_tax(db, receivable):
    """Probe 1: A (7,98 €) und B mit Skonto (11,02 €, Skonto-Satz −0,38 €); A geht, eine Zahlung über 50,00 € schließt die Rechnung – sie bekommt 19,00 − 11,02 = 7,98 €, nicht 8,36 €."""
    c = two_payments(db, receivable)
    assert marked_tax(db) == D('18.62')                                           # 7,98 + 11,02 − 0,38
    drop_directly(db, c.pay_a)

    assert state(db, c.invoice) == ('partial_payment', D('50'))
    assert taxes_of_the_others(db, c.invoice) == (110200, 79800)                  # B trägt 11,02 (sein Skonto-Satz zählt nicht), der Rest ist 7,98
    closing_bank = pay(db, c.invoice, '50.00', '2025-06-02')

    assert [row.tax for row in kids(db, closing_bank)] == ([None, D('7.98')] if receivable else [D('7.98')])
    assert marked_tax(db) == D('18.62')                                           # Σ Steuer der Zahlungssätze: die Rechnungssteuer 19,00 minus die Steuer des Skontos 0,38
    assert state(db, c.invoice) == ('paid', 0) and payments(db, c.invoice) == [(666200, 23800), (500000, None)]
    assert findings(db) == []
    check_every_probe(db, (4, 5, 6, 7))


def test_the_tax_of_the_other_payments_does_not_count_the_discount_rows(db):
    """Der Skonto-Satz einer Zahlung (−0,38 €) trägt keine „gezahlte Steuer“: Er nimmt von der Steuer der Zahlung selbst ab, nicht von der der anderen."""
    c = two_payments(db)

    assert taxes_of_the_others(db, c.invoice) == (190000, 0)                      # A (7,98) und B (11,02) tragen die ganze Rechnungssteuer – ohne den Skonto-Satz
    first_only = invoice_of(db, 'SK-ONE', 119.0, day='2025-03-05')
    pay(db, first_only, '116.62', '2025-04-12', skonto=True)
    assert taxes_of_the_others(db, first_only, 'SK-ONE') == (190000, 0)           # die Zahlung, die allein schließt: 19,00 (vorher 18,62)


def test_the_tax_of_a_payment_without_a_discount_is_what_it_always_was(db):
    """Ohne Skonto an der Rechnung ändert sich nichts: die Teilzahlung anteilig (7,98 €), die schließende der Rest (11,02 €)."""
    invoice_id = invoice_of(db, NUMBER, 119.0)
    pay(db, invoice_id, '50.00', '2025-04-02')

    assert taxes_of_the_others(db, invoice_id) == (79800, 110200)
    closing_bank = pay(db, invoice_id, '69.00', '2025-05-05')

    assert [row.tax for row in kids(db, closing_bank)] == [None, D('11.02')]
    assert marked_tax(db) == D('19.00') and findings(db) == []


def test_the_skonto_payment_itself_takes_the_rest_of_the_invoice_tax_after_a_partial_payment(db):
    """B mit Skonto nach A: Der Erlössatz trägt den Rest der Rechnungssteuer (19,00 − 7,98 = 11,02 €), der Skonto-Satz die Steuer des Skontos – unverändert."""
    c = two_payments(db)

    assert [(row.form, row.amount, row.tax) for row in kids(db, c.b)] == [
        ('liquid_first', D('69'), None), ('transfer', D('69'), D('11.02')), ('purpose_first', D('-2.38'), D('-0.38'))]


def test_the_tax_after_the_cancellation_is_what_the_payments_carried_without_the_discount_rows(db):
    """Nach dem Stornieren (Rücknahme: 3 weitere Sätze): Der Skonto-Satz der Zahlung (−0,38) und der der Rücknahme (+0,38) zählen beide nicht, der Erlössatz der Rücknahme (−0,38) schon:
    19,00 − 0,38 = 18,62 € – dieselbe Zahl wie mit allen Sätzen, und genau die, die die Erstattung zurücknimmt."""
    c = two_payments(db)
    only_b = invoice_of(db, 'SK-ONE', 119.0, day='2025-03-05')
    pay(db, only_b, '116.62', '2025-04-12', skonto=True)
    storno, storno_b = cancel(db, c.invoice, '2025-06-03'), cancel(db, only_b, '2025-06-03')

    assert taxes_of_the_others(db, c.invoice) == (186200, 3800) and taxes_of_the_others(db, only_b, 'SK-ONE') == (186200, 3800)
    assert marked_tax(db) == D('37.24')                                           # zweimal 18,62

    refund_bank = refund(db, storno.storno_id, '116.62')                          # die Erstattung nimmt die ganze Steuer des gezahlten Geldes zurück
    refund_b = refund(db, storno_b.storno_id, '116.62')

    for bank in (refund_bank, refund_b):
        assert shape(kids(db, bank)) == [('liquid_first', 1800, 10000, D('-116.62'), None), ('transfer', 4405, 4400, D('-116.62'), D('-18.62'))]
    assert db.get_storno_credits() == {storno.storno_id: 0, storno_b.storno_id: 0} and findings(db) == []


def test_two_partial_refunds_take_the_tax_by_the_same_rule_as_before(db):
    """Teilerstattungen: 50,00 € anteilig 18,62 × 50 / 116,62 = 7,98 €, die schließende 66,62 € den Rest 10,64 € – unverändert."""
    c = two_payments(db)
    storno = cancel(db, c.invoice, '2025-06-03')

    first = refund(db, storno.storno_id, '50.00', day='2025-07-01')
    second = refund(db, storno.storno_id, '66.62', day='2025-07-02')

    assert [row.tax for row in kids(db, first)] == [None, D('-7.98')]
    assert [row.tax for row in kids(db, second)] == [None, D('-10.64')]
    assert db.get_storno_credits() == {storno.storno_id: 0} and findings(db) == []


def test_the_move_to_the_replacement_invoice_takes_the_prorated_tax_of_the_part_payment(db):
    """Der Umzug einer Teilzahlung, neben der eine Zahlung mit Skonto an der stornierten Rechnung bleibt: dieselbe Steuer wie vorher (7,98 €), und das Guthaben danach trägt 10,64 €."""
    c = two_payments(db)
    storno = cancel(db, c.invoice, '2025-06-03')

    new_id, change = _reissue_and_send(db, c.invoice)

    assert change.moved == 500000
    assert [row.tax for row in kids(db, c.a) if row.number != NUMBER] == [None, D('7.98')]
    assert taxes_of_the_others(db, c.invoice) == (106400, 83600)                  # nur B bleibt: 11,02 − 0,38 (der Erlössatz der Rücknahme)
    refund_bank = refund(db, storno.storno_id, '66.62', day='2025-07-01')
    assert [row.tax for row in kids(db, refund_bank)] == [None, D('-10.64')] and payments(db, new_id) == [(500000, None)]


# ═════════════════════════════════════════════════════════════════════════════
# 2. Das Skonto bleibt an der Zahlung, die die Rechnung schließt
# ═════════════════════════════════════════════════════════════════════════════

def _refusal(call):
    """Der Klartext, mit dem ``call`` das Entfernen ablehnt – ``None``, wenn es gelingt."""
    try:
        call()
    except PaymentRemovalRefused as exc:
        return str(exc)
    return None


def _unlink_route(c):
    """``POST /invoice/delete-payment`` (``handle_delete_invoice_payment``): der Klartext der 409 – ``None``, wenn die Zuordnung entfernt wurde."""
    status, message = handlers.handle_delete_invoice_payment(json.dumps({'payment_id': c.pay_a}).encode())
    if status == 409:
        return message
    assert (status, message) == (200, 'ok'), (status, message)
    return None


def _delete_route(c, booking_id):
    """``POST /transactions/delete`` (``handle_delete_transaction``): der Grund aus der Weiterleitung auf die Liste – ``None`` bei einer Löschung."""
    status, location = handlers.handle_delete_transaction(c.db, booking_id)
    assert status == 303
    if location == '/transactions':
        return None
    assert location.startswith('/transactions?err='), location
    return unquote(location[len('/transactions?err='):])


def _editor(c, row_id):
    """Die Maske der Bankbewegung der Teilzahlung öffnen, die Zeile dieses Satzes entfernen (✕) und speichern: der Klartext der Ablehnung (400), ``None``, wenn es gespeichert wurde."""
    post_data, _options = open_form(c.db, c.a)
    status, text = handle_add_transaction(c.db, drop_row(post_data, row_id))
    if status == 400:
        return text
    assert status == 303, (status, text)
    return None


WAYS = {
    'unassign_invoice_payment': lambda c: _refusal(lambda: c.db.unassign_invoice_payment(c.pay_a)),
    'Rechnungsseite-Route': _unlink_route,
    'remove_split_row': lambda c: _refusal(lambda: c.db.remove_split_row(c.kids_a[0])),
    'delete_transaction-Satz': lambda c: _refusal(lambda: c.db.delete_transaction(c.kids_a[-1])),
    'delete_transaction-Bankbewegung': lambda c: _refusal(lambda: c.db.delete_transaction(c.a)),
    'Löschroute-Satz': lambda c: _delete_route(c, c.kids_a[0]),
    'Löschroute-Bankbewegung': lambda c: _delete_route(c, c.a),
    'Editor-Satz': lambda c: _editor(c, c.kids_a[-1]),
    'split_removal_blocker': lambda c: c.db.split_removal_blocker(c.kids_a),
}


@pytest.mark.parametrize('way', list(WAYS.values()), ids=list(WAYS))
@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_no_way_removes_the_other_payment_while_a_payment_with_a_discount_stands(routes, way, receivable):
    """Die Rechnungsseite, der Baustein, der Bank-Editor (✕), das Löschen eines Satzes oder der Bankbewegung und die Liste fragen dieselbe Prüfung (``_refund_removal_blocker``): Solange B das Skonto
    trägt, geht A nicht – der Klartext nennt die Nummer, nichts ist geändert. Entfernt der Nutzer erst B, geht derselbe Weg."""
    c = two_payments(routes, receivable)
    before = installation.dump(routes.db_name)

    assert way(c) == REFUSED == 'An der Rechnung SK-100 ist eine Zahlung mit Skonto gebucht – bitte zuerst diese Zahlung entfernen.'

    assert installation.dump(routes.db_name) == before                           # Bookings, InvoicePayments, Invoices: Zeile für Zeile (jede Spalte)
    assert payments(routes, c.invoice) == [(500000, None), (666200, 23800)] and state(routes, c.invoice) == ('paid', 0)
    routes.unassign_invoice_payment(c.pay_b)                                      # erst die Zahlung mit dem Skonto …
    assert way(c) is None                                                         # … dann geht derselbe Weg
    assert payments(routes, c.invoice) in ([], [(500000, None)])                  # (die Prüfung des Editors und der Liste entfernt nichts)


def test_the_payment_with_the_discount_goes_first_and_then_the_other(db):
    c = two_payments(db)

    db.unassign_invoice_payment(c.pay_b)

    assert payments(db, c.invoice) == [(500000, None)] and state(db, c.invoice) == ('partial_payment', D('69'))
    assert kids(db, c.b) == [] and findings(db) == []
    db.unassign_invoice_payment(c.pay_a)
    assert payments(db, c.invoice) == [] and state(db, c.invoice) == ('sent', D('119')) and findings(db) == []


def test_both_payments_together_go(db):
    """Dieselbe Prüfung, mit beiden Zuordnungen zusammen (der Bank-Editor und das Löschen einer Bewegung sammeln alle): Das Skonto geht mit seiner Zahlung."""
    c = two_payments(db)
    with closing(sqlite3.connect(db.db_name)) as con:
        blocker = Database._refund_removal_blocker
        assert blocker(con, [c.pay_a]) == REFUSED
        assert blocker(con, [c.pay_a, c.pay_b]) is None and blocker(con, [c.pay_b, c.pay_a, c.pay_a]) is None
        assert blocker(con, [c.pay_b]) is None and blocker(con, []) is None and blocker(con, [99999]) is None


def test_a_movement_that_carries_both_assignments_is_deleted_whole_and_a_row_of_the_other_is_not(db):
    """Die Teilzahlung hängt – wie ein Import sie hinterlässt – an einem Satz UNTER der Bewegung der Zahlung mit Skonto. Die Bewegung zu löschen nimmt beide Zuordnungen mit (geht), den Satz
    allein zu löschen nur die Teilzahlung (abgelehnt, solange das Skonto steht)."""
    invoice_id = invoice_of(db, NUMBER, 119.0)
    b = db.insert_booking('2025-05-05', D('66.62'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    child = db.insert_booking('2025-04-02', D('50.00'), coa_id=db.get_coa_id_by_account_number(4400), counter_coa_id=db.get_coa_id_by_account_number(4405),
                              booking_type='entry', parent_booking_id=b, entry_form='transfer')                  # kein Geld auf einem Bankkonto: er verbraucht den freien Rest der Bewegung nicht
    raw(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 500000, ?)', invoice_id, child, '2025-04-02')
    assert link_booking_to_invoice_capped(db, invoice_id, b, D('66.62'), None, True) == (True, None)
    everything = [row[0] for row in raw(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', b)]
    before = installation.dump(db.db_name)

    assert db.split_removal_blocker([child]) == REFUSED and _refusal(lambda: db.delete_transaction(child)) == REFUSED
    assert db.split_removal_blocker(everything) is None
    assert installation.dump(db.db_name) == before
    db.delete_transaction(b)

    assert payments(db, invoice_id) == [] and raw(db, 'SELECT COUNT(*) FROM Bookings WHERE ID IN (?, ?)', b, child) == [(0,)]
    assert state(db, invoice_id)[0] != 'paid'


def test_after_the_cancellation_the_other_payment_goes_as_before(db):
    """Nach dem Stornieren ist ``SkontoAmount`` NULL (die Rücknahme): Es gibt keine Zahlung mit Skonto mehr, die der Entfernung im Weg stünde."""
    c = two_payments(db)
    cancel(db, c.invoice, '2025-06-03')
    assert payments(db, c.invoice) == [(500000, None), (666200, None)]

    db.unassign_invoice_payment(c.pay_a)

    assert payments(db, c.invoice) == [(666200, None)] and findings(db) == []


def test_a_second_discount_is_refused_before_anything_is_written(db):
    """Probe 2: B trägt das Skonto, die Teilzahlung A ging (über den Baustein), eine weitere Zahlung über 49,00 € ließe 1,00 € Rest – ein zweites Skonto, zusammen mehr, als die Rechnung vorsieht."""
    c = two_payments(db)
    drop_directly(db, c.pay_a)
    other = db.insert_booking('2025-05-20', D('49.00'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    before = installation.dump(db.db_name)

    result = db.assign_invoice_payment(c.invoice, other, None, None, True)

    assert (result.ok, result.message, result.payment_id) == (False, SKONTO_ALREADY, None) and SKONTO_ALREADY == 'An dieser Rechnung ist schon ein Skonto gebucht.'
    assert installation.dump(db.db_name) == before and payments(db, c.invoice) == [(666200, 23800)]
    assert link_booking_to_invoice_capped(db, c.invoice, other, D('49.00'), None, True) == (False, SKONTO_ALREADY)
    plain = db.assign_invoice_payment(c.invoice, other)                                                             # ohne Skonto gilt, was immer galt
    assert plain.ok and payments(db, c.invoice) == [(666200, 23800), (490000, None)] and state(db, c.invoice) == ('partial_payment', D('1'))


def test_a_second_discount_creates_no_account(db):
    """Die Ablehnung kommt in der Vorprüfung, vor dem Anlegen des Skonto-Kontos: Ein Konto, das fehlt, bleibt weg (die Maske fragt dieselbe Prüfung)."""
    invoice_id = invoice_of(db, NUMBER, 119.0)
    pay(db, invoice_id, '50.00', '2025-04-02')
    raw(db, 'UPDATE InvoicePayments SET SkontoAmount = 10000')                                                      # von Hand: 1,00 € Skonto an der Teilzahlung
    raw(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (4730, 4731, 4736)')
    other = db.insert_booking('2025-05-20', D('67.00'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    before = installation.dump(db.db_name)

    result = db.assign_invoice_payment(invoice_id, other, None, None, True)

    assert (result.ok, result.message) == (False, SKONTO_ALREADY)                                                  # der Rest (1,00 €) wäre für sich in Ordnung
    assert installation.dump(db.db_name) == before and db.get_coa_id_by_account_number(4736) is None
    assert db.payment_precheck(invoice_id, account_id(db, 'Probe-Giro'), amount=D('67.00'), skonto=True) == SKONTO_ALREADY
    assert db.payment_precheck(invoice_id, account_id(db, 'Probe-Giro'), amount=D('67.00')) is None


# ── Über den Server: dieselben Routen, derselbe Satz ─────────────────────────

def _http_scene(port, w):
    """T-100 über den Server (119,00 €, 19 %, Skonto 2 % in 7 Tagen); A = 50,00 € am 01.07., B = 66,62 € mit dem Skonto am 02.07.; liefert (Rechnung, A, B, Zuordnung A, Sätze von A)."""
    invoice_id = sent_over_http(port, w, number='T-100')
    a = w.db.insert_booking('2026-07-01', 50.0, booking_type='bank', account_id=w.bank, recipient_client=CUSTOMER)
    assert handlers.link_booking_to_invoice_capped(w.db, invoice_id, a, 50.0) == (True, None)
    b = w.db.insert_booking('2026-07-02', 66.62, booking_type='bank', account_id=w.bank, recipient_client=CUSTOMER)
    assert handlers.link_booking_to_invoice_capped(w.db, invoice_id, b, 66.62, None, True) == (True, None)
    pay_a = raw(w.db, 'SELECT ID FROM InvoicePayments WHERE BookingID = ?', a)[0][0]
    return invoice_id, a, b, pay_a, [row.id for row in kids(w.db, a)]


def test_over_http_the_delete_route_answers_409_with_the_sentence(server_port, books):
    db = books.db
    invoice_id, a, b, pay_a, _kids = _http_scene(server_port, books)
    before = book_tables(db)

    resp = post_json(server_port, '/invoice/delete-payment', {'payment_id': pay_a}, books.csrf)

    sentence = SKONTO_REMOVAL_REFUSED.format(number='T-100')
    assert resp.status == 409 and json.loads(resp.text) == {'success': False, 'error': sentence}
    assert book_tables(db) == before and len(payments(db, invoice_id)) == 2
    pay_b = raw(db, 'SELECT ID FROM InvoicePayments WHERE BookingID = ?', b)[0][0]                                 # erst die Zahlung mit dem Skonto, dann die Teilzahlung
    assert post_json(server_port, '/invoice/delete-payment', {'payment_id': pay_b}, books.csrf).status == 200
    resp = post_json(server_port, '/invoice/delete-payment', {'payment_id': pay_a}, books.csrf)
    assert resp.status == 200 and json.loads(resp.text) == {'success': True} and payments(db, invoice_id) == []


def test_over_http_the_list_route_redirects_with_the_sentence_and_the_list_shows_it(server_port, books):
    db = books.db
    invoice_id, a, _b, _pay_a, kids_a = _http_scene(server_port, books)
    before = book_tables(db)
    sentence = SKONTO_REMOVAL_REFUSED.format(number='T-100')

    for booking in (kids_a[0], a):
        resp = post(server_port, f'/transactions/delete?id={booking}', csrf=books.csrf)
        assert resp.status == 303, booking
        assert unquote(resp.headers['Location']) == f'/transactions?err={sentence}'
        assert book_tables(db) == before
        page = get(server_port, resp.headers['Location'])
        assert page.status == 200 and app_msg_arguments(Dom(page.text)) == [sentence, 'error']
    assert len(payments(db, invoice_id)) == 2


def test_over_http_the_editor_answers_400_with_the_sentence_and_stores_nothing(server_port, books):
    db = books.db
    _invoice_id, a, _b, _pay_a, kids_a = _http_scene(server_port, books)
    post_data, _options = open_form(db, a)
    post_data = drop_row(post_data, kids_a[-1])
    before = book_tables(db)

    fields = [(name, value) for name, values in post_data.items() for value in values] + [('csrf', books.csrf)]
    resp = post(server_port, '/transactions/add', form=fields)

    assert resp.status == 400 and resp.headers['Content-Type'].startswith('text/plain')
    assert resp.text == SKONTO_REMOVAL_REFUSED.format(number='T-100') and book_tables(db) == before


# ═════════════════════════════════════════════════════════════════════════════
# 3. Ein ungültiger Wert in SkontoAmount
# ═════════════════════════════════════════════════════════════════════════════

# Was die SQL-Konsole in die Spalte schreiben kann: Text (auch mit einer Zahl vorn), eine Kommazahl, 0, eine negative Zahl, ein Blob
INVALID = {'text': ("'abc'", 'text'), 'text-with-a-number': ("'23800abc'", 'text'), 'fraction': ('23800.5', 'real'), 'zero': ('0', 'integer'),
           'negative': ('-23800', 'integer'), 'blob': ("X'4142'", 'blob')}
INVALID_SENTENCE = 'Zuordnung #1 trägt einen ungültigen Skonto-Wert.'


def _break_the_column(db, how):
    literal, kind = INVALID[how]
    raw(db, f'UPDATE InvoicePayments SET SkontoAmount = {literal}')
    assert raw(db, 'SELECT typeof(SkontoAmount) FROM InvoicePayments') == [(kind,)]


@pytest.mark.parametrize('how', list(INVALID))
def test_an_invalid_discount_value_is_a_finding_and_breaks_nothing(db, how):
    """Probe 5: Die Prüfliste formatierte den rohen Wert und brach bei Text und Blob ab – und mit ihr die Rechnungsseite und der Probelauf des Neuaufbaus. Jetzt steht ein fester Satz ohne den Wert da."""
    invoice_id, _bank = paid_with_discount(db)
    _break_the_column(db, how)

    mismatches = [text for category, text in findings(db, invoice_id) if category == 'skonto_mismatch']
    assert mismatches == [INVALID_SENTENCE] == [text for category, text in findings(db) if category == 'skonto_mismatch']
    page = PageInvoice(db, {}, invoice_id)
    assert INVALID_SENTENCE in page and 'Zahlungszuordnung prüfen' in page
    plan = db.rebuild_invoice_payments(invoice_id, today=FIXED_TODAY).plan
    assert plan.skipped == [REBUILD_SKIP_REASONS['skonto']] and [action.kind for action in plan.actions] == ['recalc']      # A–C laufen nicht, D rechnet den Rest ohne den ungültigen Wert neu
    assert (plan.due_before, plan.due_after) == (0, 23800)


@pytest.mark.parametrize('how', list(INVALID))
def test_an_invalid_discount_value_is_never_counted_as_settled(db, how):
    invoice_id, _bank = paid_with_discount(db)
    _break_the_column(db, how)

    with db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        db._recalc_invoice(tx.conn, invoice_id, today='2025-12-31')

    assert state(db, invoice_id) == ('partial_payment', D('2.38'))                                                # nur das Geld (116,62 €) ist bezahlt
    assert raw(db, f'SELECT SUM({settled_sql()}) FROM InvoicePayments') == [(1166200,)]
    assert 'amount_due_mismatch' not in [category for category, _text in findings(db, invoice_id)]               # der Rest passt zu dem, was zählt


def test_a_valid_value_is_still_counted_and_formatted(db):
    invoice_id, _bank = paid_with_discount(db)

    assert raw(db, f'SELECT SUM({settled_sql()}) FROM InvoicePayments') == [(1190000,)]
    assert findings(db, invoice_id) == [] and state(db, invoice_id) == ('paid', 0)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Stornieren: leere Spalte, aber die Skonto-Sätze stehen
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_a_cancellation_refuses_when_the_column_is_empty_but_the_discount_rows_stand(db, receivable):
    """Ohne den Wert in der Spalte wüsste das Stornieren nichts vom Skonto und ließe seine Sätze stehen: das Skonto-Konto bliebe belastet, die Debitoren mit 2,38 € im Soll."""
    invoice_id, _bank = paid_with_discount(db, receivable)
    raw(db, 'UPDATE InvoicePayments SET SkontoAmount = NULL')
    before, log_before = installation.dump(db.db_name), replay_lines()

    storno = db.cancel_invoice(invoice_id, '2025-06-03', USER, today=TODAY)

    assert (storno.ok, storno.message, storno.code) == (False, SKONTO_STORNO_EDITED, 409)
    assert (storno.storno_id, storno.storno_number, storno.counter_booked) == (None, None, False)
    assert installation.dump(db.db_name) == before and replay_lines() == log_before                               # keine Zeile, keine Nummer, kein Konto, kein Eintrag im Replay-Log
    db.unassign_invoice_payment(raw(db, 'SELECT ID FROM InvoicePayments')[0][0])                                   # der Satz sagt, was zu tun ist: die Zahlung entfernen …
    assert cancel(db, invoice_id).ok                                                                              # … dann geht es


@pytest.mark.parametrize('how', list(INVALID))
def test_a_cancellation_refuses_an_invalid_value_in_the_column(db, how):
    invoice_id, _bank = paid_with_discount(db)
    _break_the_column(db, how)
    before, log_before = installation.dump(db.db_name), replay_lines()

    storno = db.cancel_invoice(invoice_id, '2025-06-03', USER, today=TODAY)

    assert (storno.ok, storno.message, storno.code) == (False, SKONTO_STORNO_EDITED, 409)
    assert installation.dump(db.db_name) == before and replay_lines() == log_before


@pytest.mark.parametrize('receivable', [True, False], ids=['mit-Forderung', 'ohne-Forderung'])
def test_after_a_regular_reversal_the_discount_rows_add_up_to_nothing_and_nothing_refuses_again(db, receivable):
    """Nach der Rücknahme beim Stornieren ist die Spalte NULL, die Skonto-Sätze (−2,38 der Zahlung, +2,38 der Rücknahme) ergeben 0: ein zweiter Lauf der Prüfung lehnt nicht ab und
    findet nichts zurückzunehmen."""
    invoice_id, _bank = paid_with_discount(db, receivable)
    cancel(db, invoice_id)

    with closing(db._get_connection()) as conn:
        assert db._skonto_reversals(conn, invoice_id, NUMBER) == (None, [])
        assert db._skonto_reversals(conn, invoice_id, NUMBER) == (None, [])
    assert payments(db, invoice_id) == [(1166200, None)] and findings(db) == []
    again = db.cancel_invoice(invoice_id, '2025-06-04', USER, today=TODAY)                                          # ein zweites Stornieren lehnt aus dem Grund ab, der immer galt – nicht wegen des Skontos
    assert (again.ok, again.code) == (False, 409) and again.message != SKONTO_STORNO_EDITED


def test_a_late_credit_note_for_an_invoice_with_only_plain_payments_is_not_refused(db):
    """„Stornorechnung nachträglich erstellen“: Eine Rechnung von Hand storniert, mit einer gewöhnlichen Zahlung und ohne Skonto – keine Skonto-Sätze, nichts zu prüfen."""
    invoice_id = invoice_of(db, NUMBER, 119.0)
    pay(db, invoice_id, '50.00', '2025-04-02')
    raw(db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", invoice_id)

    storno = db.cancel_invoice(invoice_id, '2025-06-03', USER, today=TODAY)

    assert storno.ok, storno.message
    assert payments(db, invoice_id) == [(500000, None)] and findings(db) == []


def test_a_collective_payment_with_a_discount_on_another_invoice_does_not_count_for_this_one(db):
    """Eine Bewegung, zwei Rechnungen: Die Skonto-Sätze der einen tragen ihre Nummer – die andere (ohne Skonto, Spalte NULL) storniert ungehindert."""
    first = invoice_of(db, 'SK-A', 119.0)
    second = invoice_of(db, 'SK-B', 98.0, day='2025-03-04')
    bank = db.insert_booking('2025-04-10', D('214.62'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    assert link_booking_to_invoice_capped(db, first, bank, D('116.62'), None, True) == (True, None)
    assert link_booking_to_invoice_capped(db, second, bank, D('98.00')) == (True, None)

    assert cancel(db, second, '2025-06-03').ok and cancel(db, first, '2025-06-04').ok
    assert payments(db, second) == [(980000, None)] and payments(db, first) == [(1166200, None)]


# ═════════════════════════════════════════════════════════════════════════════
# 5. Ein Rest, der kein ganzer Cent-Betrag ist
# ═════════════════════════════════════════════════════════════════════════════

def test_a_rest_that_is_not_whole_cents_refuses_and_writes_nothing(db):
    """Probe 4: Eine Bewegung über 116,6250 € lässt 2,3750 € Rest – die Sätze des Skontos rechnen auf den Cent (Steuer, Grenze), der Rest stünde mit Bruchteilen da."""
    invoice_id = invoice_of(db, NUMBER, 119.0)
    bank = db.insert_booking('2025-04-10', D('116.6250'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    raw(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (4730, 4731, 4736)')
    before = installation.dump(db.db_name)

    result = db.assign_invoice_payment(invoice_id, bank, None, None, True)

    assert (result.ok, result.message, result.payment_id) == (False, SKONTO_NOT_CENTS, None) and SKONTO_NOT_CENTS == 'Skonto lässt sich nur für einen Rest in ganzen Cent ausbuchen.'
    assert installation.dump(db.db_name) == before and db.get_coa_id_by_account_number(4736) is None
    assert link_booking_to_invoice_capped(db, invoice_id, bank, None, None, True) == (False, SKONTO_NOT_CENTS)
    plain = db.assign_invoice_payment(invoice_id, bank)                                                             # ohne Skonto bucht die Zuordnung, wie sie es immer tat
    assert plain.ok and state(db, invoice_id) == ('partial_payment', D('2.375'))


def test_a_rest_with_fractions_of_a_cent_because_of_the_invoice_refuses_too(db):
    invoice_id = invoice_of(db, NUMBER, 119.0)
    raw(db, 'UPDATE Invoices SET SumGross = 1190050, AmountDue = 1190050 WHERE ID = ?', invoice_id)              # 119,0050 €: ein Rest von 2,3850 € nach 116,62 €
    bank = db.insert_booking('2025-04-10', D('116.62'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
    before = installation.dump(db.db_name)

    result = db.assign_invoice_payment(invoice_id, bank, None, None, True)

    assert (result.ok, result.message) == (False, SKONTO_NOT_CENTS) and installation.dump(db.db_name) == before


@pytest.mark.parametrize('paid, message', [
    ('119.0050', SKONTO_NO_REST),                                                                              # über dem Rest: kein Rest, das kommt zuerst
    ('100.0050', SKONTO_NOT_CENTS),                                                                            # 18,9950 € Rest: kein ganzer Cent, vor der Grenze (2,38 €)
    ('100.0000', SKONTO_TOO_LARGE.format(rest='19,00 €', skonto='2,38 €')),                                    # ein ganzer Cent-Betrag über der Grenze: die Grenze
])
def test_the_whole_cent_rule_comes_after_no_rest_and_before_the_limit(db, paid, message):
    invoice_id = invoice_of(db, NUMBER, 119.0)
    bank = db.insert_booking('2025-04-10', D(paid), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')

    result = db.assign_invoice_payment(invoice_id, bank, None, None, True)

    assert (result.ok, result.message) == (False, message)


def test_a_whole_cent_rest_is_still_booked_with_the_discount(db):
    invoice_id = invoice_of(db, NUMBER, 119.0)

    bank = pay(db, invoice_id, '116.62', '2025-04-10', skonto=True)

    assert payments(db, invoice_id) == [(1166200, 23800)] and [row.amount for row in kids(db, bank)][-1] == D('-2.38')
    assert findings(db) == []


# ── ein Skonto-Konto ist kein Erlöskonto einer Zahlung ───────────────────────

def test_a_discount_account_is_no_choice_for_the_revenue_account_of_a_payment(tmp_db):
    """Die Wahl des Erlöskontos einer Zahlung führt die Skonto-Konten nicht (``payment_revenue_choices``), und eine Anfrage, die
    eines nennt, wird abgelehnt – sein Satz zählte sonst nicht in die Steuer der Zuordnungen und gälte beim Stornieren als
    geändertes Skonto. Die allgemeine Liste der Erlöskonten (``get_revenue_coas``) bleibt, wie sie war."""
    from db.invoice_payments import NOT_A_REVENUE_COA, SKONTO_ACCOUNT_NUMBERS
    from server.pages_invoice import _revenue_coa_select

    numbers = {row[1] for row in tmp_db.get_revenue_coas()}
    assert 4736 in numbers and 4400 in numbers                        # der Standard-Kontenrahmen führt beide
    choices = tmp_db.payment_revenue_choices()
    assert not {row[1] for row in choices} & set(SKONTO_ACCOUNT_NUMBERS)
    assert {row[1] for row in choices} == numbers - set(SKONTO_ACCOUNT_NUMBERS)
    skonto_coa = tmp_db.get_coa_id_by_account_number(4736)
    assert tmp_db._chosen_revenue_coa(skonto_coa) == (None, NOT_A_REVENUE_COA)
    assert tmp_db._chosen_revenue_coa(tmp_db.get_coa_id_by_account_number(4400))[1] is None
    select = _revenue_coa_select(tmp_db, 0.16)
    assert f'value="{skonto_coa}"' not in select and 'Gewährte Skonti' not in select

