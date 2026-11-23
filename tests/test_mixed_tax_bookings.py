# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Buchungen eines Belegs mit Steuersatz je Position (2.7.0, Etappe 2a): Forderung beim Versenden, Zahlung, Entfernen, Abgleich, Anzeigen.

Die feste Rechnung ist **M** (Nummer ``M-100``): 19 % × 100,00 und 7 % × 50,00 – Gruppe 19: netto 100,00 / Steuer 19,00 / brutto 119,00,
Gruppe 7: 50,00 / 3,50 / 53,50, zusammen 150,00 / 22,50 / 172,50. Beträge in den Vergleichen stehen in Cent (``cents``).

* **Versenden und Wiederbeleben** (``set_invoice_status``, die Route, ``ensure_receivable_booking``): je Satz eine Forderungszeile „10000 an
  Wartekonto“ (4405 für 19 %, 4345 für 7 %) mit dem Satz als Bruch (0,19 / 0,07) und der Steuer der Gruppe. Lässt sich der Beleg nicht buchen
  (Positionen und Summen passen nicht zusammen, eine Gruppe mit Brutto unter 0), lehnt der feste Klartext ab – vor dem ersten Schreiben, ohne dass
  ein Konto entsteht.
* **Zahlung zuordnen** (``assign_invoice_payment``, die Vorprüfung der Maske, die Route): „Bank an Debitoren“ über den ganzen Betrag und je Satz
  „Wartekonto an Erlöskonto“ (ohne Forderung je Satz „Erlöskonto an Bank“); die Teilzahlung verteilt ``tax_groups.split_payment``. Es gibt keine
  Wahl des Erlöskontos. Entfernen nimmt die ganze Zuordnung mit, auch über den Bank-Editor.
* **Lernen:** Das Erlöskonto je Satz lernt ``resolve_revenue_coa`` aus dem Satz des Buchungssatzes, nie aus dem Platzhalter im Kopf.
* Stornieren, Erstattung, Neuaufbau und der Umzug der Zahlungen beim Versenden einer Ersatzrechnung (Etappe 2b): ``test_mixed_tax_storno.py``; der eine Weg,
  der so einen Beleg noch ablehnt (``link_invoice_to_transaction``), und die Ein-Satz-Gegenstücke der Wege: ``test_mixed_tax_locks.py``.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import json
from decimal import Decimal

import pytest

import server.handlers as handlers
from db.invoice_lock import CHANGED_MESSAGE, REOPEN_PAYMENT_MESSAGE
from db.invoice_payments import (MIXED_ALLOCATION_UNREADABLE, MIXED_GROUP_NOT_POSITIVE, MIXED_GROUPS_MISMATCH, MIXED_NO_REVENUE_COA,
                                 MIXED_RECEIVABLE_MISMATCH, MOVE_MIXED_HINT, PAYMENT_CHANGED_MESSAGE, PaymentAssignment)
from server.pages_invoice import PageInvoice
from server.pages_transactions import PageTransactions
from tests.entry_form_roundtrip import drop_row, open_form, submit
from tests.invoice_world import columns, force_status, make_invoice, query_rows, run_sql, set_invoice_status
from tests.test_invoice_reissue import edit_draft, paid_scenario
from tests.test_invoice_storno import ONE_ITEM, USER, sent, tables, world   # noqa: F401  (``world`` ist die Fixture)
from tests.test_mixed_tax_locks import MIXED_ITEMS, chart, drop_accounts, flag, incoming, mixed_draft

NUMBER = 'M-100'
DATE = '2026-06-07'                                  # das Rechnungsdatum der Testwelt (``make_payload``)
TEXT = 'Rechnung M-100 Malermeister Pinsel'
RECEIVABLES = [(10000, 4405, 11900, 0.19, 1900, 'transfer', DATE, TEXT),
               (10000, 4345, 5350, 0.07, 350, 'transfer', DATE, TEXT)]
FULL = [(1800, 10000, 17250, None, None, 'liquid_first'),
        (4405, 4400, 11900, 1900, 0.19, 'transfer'),
        (4345, 4300, 5350, 350, 0.07, 'transfer')]
FIRST = [(1800, 10000, 10000, None, None, 'liquid_first'),                       # 100,00: 68,99 und 31,01
         (4405, 4400, 6899, 1102, 0.19, 'transfer'),
         (4345, 4300, 3101, 203, 0.07, 'transfer')]
SECOND = [(1800, 10000, 7250, None, None, 'liquid_first'),                       # die schließende Zahlung 72,50: 50,01 und 22,49
          (4405, 4400, 5001, 798, 0.19, 'transfer'),
          (4345, 4300, 2249, 147, 0.07, 'transfer')]


# ── Hilfen ───────────────────────────────────────────────────────────────────

def cents(minor):
    return None if minor is None else minor // 100


def mixed_sent(w, number=NUMBER, **over):
    """M, versendet wie in der Oberfläche (Route: Forderung gebucht, Dokument erzeugt). Fällig erst 2099: Der Status einer Rechnung nach dem
    Entfernen einer Zahlung (``sent`` oder ``overdue``) hängt sonst an der Uhr."""
    over.setdefault('dueDate', '2099-12-31')
    return sent(w, number, items=MIXED_ITEMS, taxMode='mixed', **over)


def _rows(db, where, *args):
    return query_rows(
        db, 'SELECT c.AccountNumber AS coa, g.AccountNumber AS counter, b.Amount, b.TaxRate, b.TaxAmount, b.EntryForm, b.DateBooking, b.Text, '
            'b.Status FROM Bookings b LEFT JOIN ChartOfAccounts c ON c.ID = b.COA_ID LEFT JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID '
            f'WHERE b.DocumentNumber = ? AND {where} ORDER BY b.ID', *args)


def receivables(db, number=NUMBER):
    """Die Forderungszeilen (eigenständige Sätze der Beleg-Nr.): (Konto, Gegenkonto, Betrag, Satz, Steuer, Form, Datum, Text) in Cent."""
    return [(r['coa'], r['counter'], cents(r['Amount']), r['TaxRate'], cents(r['TaxAmount']), r['EntryForm'], r['DateBooking'], r['Text'])
            for r in _rows(db, "b.BookingType = 'entry' AND b.ParentBooking_ID IS NULL", number)]


def marked(db, number=NUMBER):
    """Die Sätze der App zu den Zuordnungen der Nummer: (Konto, Gegenkonto, Betrag, Steuer, Satz, Form) in Cent."""
    return [(r['coa'], r['counter'], cents(r['Amount']), cents(r['TaxAmount']), r['TaxRate'], r['EntryForm'])
            for r in _rows(db, "b.Status = 'invoice_payment'", number)]


def standing(db, invoice_id):
    stored = columns(db, invoice_id)
    return stored['Status'], cents(stored['AmountDue'])


def pay_on(w, invoice_id, amount, day='2026-07-01'):
    """Eine Zahlung zuordnen, wie die Oberfläche es tut; liefert die Bankbewegung."""
    booking = w.db.insert_booking(day, amount, booking_type='bank', account_id=w.bank, recipient_client='Malermeister Pinsel')
    ok, error = handlers.link_booking_to_invoice_capped(w.db, invoice_id, booking, amount)
    assert ok, error
    return booking


def allocation_ids(db, invoice_id):
    return [r['ID'] for r in query_rows(db, 'SELECT ID FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID', invoice_id)]


def without_invoices(snapshot):
    return {table: rows for table, rows in snapshot.items() if table != 'Invoices'}


# ═════════════════════════════════════════════════════════════════════════════
# Versenden und Wiederbeleben: je Satz eine Forderung
# ═════════════════════════════════════════════════════════════════════════════

def test_sending_a_mixed_draft_books_one_receivable_per_rate_and_creates_the_accounts(world):
    invoice_id = mixed_draft(world)
    drop_accounts(world.db, 10000, 4405, 4345)                       # legte der Weg sie nicht an, fehlten sie danach

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (True, 200, None)
    assert standing(world.db, invoice_id) == ('sent', 17250)
    assert receivables(world.db) == RECEIVABLES                      # in der Reihenfolge der Gruppen: 19 %, dann 7 %
    assert {10000, 4405, 4345} <= {row['AccountNumber'] for row in chart(world.db)}


def test_the_receivable_rows_carry_what_the_row_of_an_invoice_with_one_rate_carries(world):
    """Dieselben Spalten wie die Forderung einer Rechnung mit einem Satz: Kunde, Empfänger, Währung, Art, kein Status, kein Elternsatz."""
    mixed = mixed_draft(world)
    single = make_invoice(world, 'draft', number='T-100', items=ONE_ITEM)
    for invoice_id in (mixed, single):
        assert world.db.set_invoice_status(invoice_id, 'sent', USER).ok
    rows = query_rows(world.db, "SELECT * FROM Bookings WHERE BookingType = 'entry' ORDER BY ID")
    ignored = {'ID', 'COA_ID', 'CounterCOA_ID', 'Amount', 'TaxRate', 'TaxAmount', 'DocumentNumber', 'Text'}

    assert len(rows) == 3
    for row in rows[1:]:
        assert {k: v for k, v in row.items() if k not in ignored} == {k: v for k, v in rows[0].items() if k not in ignored}
    assert rows[0]['Contact_ID'] == world.cust and rows[0]['RecipientClient'] == 'Malermeister Pinsel' and rows[0]['Currency'] == 'EUR'
    assert rows[0]['Status'] is None and rows[0]['ParentBooking_ID'] is None and rows[0]['AutoMirror'] == 0
    assert rows[2]['Text'] == 'Rechnung T-100 Malermeister Pinsel'      # der Text der Rechnung mit einem Satz, wie immer


def test_booking_the_receivable_twice_is_idempotent(world):
    invoice_id = mixed_draft(world)
    assert world.db.set_invoice_status(invoice_id, 'sent', USER).ok
    before = tables(world.db)

    first_id = world.db.ensure_receivable_booking(invoice_id)

    assert first_id == query_rows(world.db, "SELECT MIN(ID) AS id FROM Bookings WHERE DocumentNumber = ?", NUMBER)[0]['id']
    assert tables(world.db) == before and len(receivables(world.db)) == 2


def test_a_finalized_mixed_draft_is_sent_and_books_the_same(world):
    invoice_id = mixed_draft(world)
    assert world.db.set_invoice_status(invoice_id, 'finalized', USER).ok

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code) == (True, 200) and receivables(world.db) == RECEIVABLES


def test_the_route_sends_a_mixed_invoice_and_books_both_receivables(world):
    invoice_id = mixed_draft(world)

    code, body = set_invoice_status(invoice_id, 'sent')

    assert code == 200 and body['success'] is True and 'warning' not in body, body
    assert receivables(world.db) == RECEIVABLES and standing(world.db, invoice_id) == ('sent', 17250)


@pytest.mark.parametrize('target', ['sent', 'overdue'])
def test_a_cancelled_mixed_invoice_is_revived_and_books_per_rate(world, target):
    invoice_id = mixed_draft(world)
    force_status(world.db, invoice_id, 'cancelled')                  # von Hand storniert (ohne Stornorechnung)
    assert receivables(world.db) == []

    change = world.db.set_invoice_status(invoice_id, target, USER)

    assert (change.ok, change.code, change.kind) == (True, 200, 'revive')
    assert columns(world.db, invoice_id)['Status'] == target and receivables(world.db) == RECEIVABLES


def test_taking_a_mixed_invoice_back_to_draft_removes_both_receivables(world):
    invoice_id = mixed_draft(world)
    assert world.db.set_invoice_status(invoice_id, 'sent', USER).ok

    change = world.db.set_invoice_status(invoice_id, 'draft', USER)

    assert (change.ok, change.kind) == (True, 'reopen')
    assert receivables(world.db) == [] and columns(world.db, invoice_id)['Status'] == 'draft'


def test_taking_back_a_mixed_invoice_with_a_payment_is_refused_and_changes_nothing(world):
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'draft', USER)

    assert (change.ok, change.code) == (False, 409) and 'Zahlungen' in change.message       # „Teilzahlung“ geht nicht zurück auf Entwurf
    assert tables(world.db) == before


def test_orphaned_payment_rows_keep_a_mixed_invoice_from_being_taken_back(world):
    """Sätze der App ohne Zuordnung (die Zuordnung ist per SQL weg, der Status von Hand zurück auf „versendet“): ``REOPEN_PAYMENT_MESSAGE`` –
    die Forderungen bleiben."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    run_sql(world.db, 'DELETE FROM InvoicePayments WHERE InvoiceID = ?', invoice_id)
    force_status(world.db, invoice_id, 'sent')
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'draft', USER)

    assert (change.ok, change.code, change.message) == (False, 409, REOPEN_PAYMENT_MESSAGE) and tables(world.db) == before


def test_a_mixed_invoice_that_was_sent_keeps_one_receivable_per_rate_when_switched_to_overdue(world):
    invoice_id = mixed_sent(world)
    before = tables(world.db)

    assert world.db.set_invoice_status(invoice_id, 'overdue', USER).ok

    assert receivables(world.db) == RECEIVABLES and tables(world.db)['Bookings'] == before['Bookings']


# ── abgelehnt: vor dem ersten Schreiben, ohne ein Konto anzulegen ────────────

def test_a_group_with_a_negative_total_is_refused_with_its_rate_and_amount_and_nothing_is_written(world):
    """7 % × 50,00 und 7 % × −60,00: die Positionen dieses Satzes ergeben zusammen −10,70 € (netto −10,00, Steuer −0,70)."""
    items = MIXED_ITEMS + [{'position': 3, 'quantity': 1, 'unit': 'C62', 'description': 'Rabatt', 'unitPrice': -60.0, 'taxRate': 7}]
    invoice_id = make_invoice(world, 'draft', number='M-300', items=items, taxMode='mixed')
    assert columns(world.db, invoice_id)['MixedTaxRates'] == 1
    drop_accounts(world.db, 10000, 4405, 4345)
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code) == (False, 409)
    assert change.message == MIXED_GROUP_NOT_POSITIVE.format(rate='7', amount='-10,70 €')
    assert '(7 %: -10,70 €)' in change.message
    assert tables(world.db) == before                                # auch kein Konto angelegt


def test_positions_that_do_not_match_the_sums_are_refused_and_no_account_is_looked_up(world):
    invoice_id = mixed_draft(world)
    run_sql(world.db, 'UPDATE Invoices SET SumGross = SumGross + 10000 WHERE ID = ?', invoice_id)    # ein Eingriff an der App vorbei
    drop_accounts(world.db, 10000, 4405, 4345)
    before, accounts = tables(world.db), chart(world.db)

    assert world.db._receivable_accounts_for(invoice_id, 'sent') == (None, None)
    assert chart(world.db) == accounts                               # nichts nachgeschlagen, das ein Konto anlegte
    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, MIXED_GROUPS_MISMATCH)
    assert tables(world.db) == before and columns(world.db, invoice_id)['Status'] == 'draft'


def test_the_route_refuses_a_mixed_invoice_that_cannot_be_booked(world):
    invoice_id = mixed_draft(world)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 19 WHERE InvoiceId = ? AND Position = 2', invoice_id)    # die Summen stimmen nicht mehr
    before = tables(world.db)

    code, body = set_invoice_status(invoice_id, 'sent')

    assert code == 409 and body == {'success': False, 'error': MIXED_GROUPS_MISMATCH} and tables(world.db) == before


def test_a_position_without_a_rate_makes_the_invoice_unbookable(world):
    invoice_id = mixed_draft(world)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = -1 WHERE InvoiceId = ? AND Position = 2', invoice_id)       # der Platzhalter −1
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, MIXED_GROUPS_MISMATCH) and tables(world.db) == before


def test_the_flag_set_after_the_preparation_is_caught_in_the_transaction(world, monkeypatch):
    """Die Konten werden vor der Transaktion aufgelöst; wird der Beleg dazwischen gekennzeichnet, entscheidet der frisch gelesene Stand: Die Konten
    gelten für einen Satz, nicht für die Gruppen – „Die Rechnung wurde inzwischen geändert“."""
    invoice_id = make_invoice(world, 'draft', number='T-100', items=ONE_ITEM)
    real = world.db._receivable_accounts_for

    def prepared_then_flagged(invoice, new):
        found = real(invoice, new)
        flag(world.db, invoice)
        return found

    monkeypatch.setattr(world.db, '_receivable_accounts_for', prepared_then_flagged)
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, CHANGED_MESSAGE)
    assert without_invoices(tables(world.db)) == without_invoices(before)
    assert columns(world.db, invoice_id)['Status'] == 'draft'


def test_a_position_changed_after_the_preparation_is_caught_in_the_transaction(world, monkeypatch):
    """Die Gruppen ändern sich zwischen Vorbereitung und Transaktion (hier: die zweite Position, samt Summen, wie das Speichern sie schriebe)."""
    invoice_id = mixed_draft(world)
    real = world.db._receivable_accounts_for

    def prepared_then_changed(invoice, new):
        found = real(invoice, new)
        run_sql(world.db, 'UPDATE InvoiceItems SET TotalNet = 600000 WHERE InvoiceId = ? AND Position = 2', invoice)
        run_sql(world.db, 'UPDATE Invoices SET SumNet = 1600000, TaxAmount = 232000, SumGross = 1832000 WHERE ID = ?', invoice)
        return found

    monkeypatch.setattr(world.db, '_receivable_accounts_for', prepared_then_changed)
    before = tables(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (False, 409, CHANGED_MESSAGE)
    assert receivables(world.db) == [] and columns(world.db, invoice_id)['Status'] == 'draft'
    assert tables(world.db)['Bookings'] == before['Bookings'] and tables(world.db)['ChartOfAccounts'] == before['ChartOfAccounts']


# ── ``ensure_receivable_booking`` (Demodaten, Tests) ─────────────────────────

def test_ensure_receivable_booking_books_one_row_per_rate_and_returns_the_first(world):
    invoice_id = mixed_draft(world)

    first_id = world.db.ensure_receivable_booking(invoice_id)

    assert receivables(world.db) == RECEIVABLES and first_id == query_rows(world.db, 'SELECT MIN(ID) AS id FROM Bookings')[0]['id']
    assert world.db.ensure_receivable_booking(invoice_id) == first_id                   # idempotent


def test_ensure_receivable_booking_refuses_before_it_creates_an_account(world):
    invoice_id = mixed_draft(world)
    run_sql(world.db, 'UPDATE Invoices SET TaxAmount = TaxAmount + 100 WHERE ID = ?', invoice_id)
    drop_accounts(world.db, 10000, 4405, 4345)
    before = tables(world.db)

    with pytest.raises(ValueError) as refused:
        world.db.ensure_receivable_booking(invoice_id)

    assert str(refused.value) == MIXED_GROUPS_MISMATCH and tables(world.db) == before


def test_ensure_receivable_booking_checks_the_stored_state_in_the_transaction_too(world, monkeypatch):
    invoice_id = make_invoice(world, 'draft', number='T-100', items=ONE_ITEM)
    real = world.db._receivable_accounts

    def prepared_then_flagged(rate):
        found = real(rate)
        flag(world.db, invoice_id)
        return found

    monkeypatch.setattr(world.db, '_receivable_accounts', prepared_then_flagged)
    before = without_invoices(tables(world.db))

    with pytest.raises(ValueError) as refused:
        world.db.ensure_receivable_booking(invoice_id)

    assert str(refused.value) == CHANGED_MESSAGE and without_invoices(tables(world.db)) == before


def test_ensure_receivable_booking_catches_changed_groups_in_the_transaction(world, monkeypatch):
    invoice_id = mixed_draft(world)
    real = world.db._mixed_receivable_accounts

    def prepared_then_changed(groups):
        found = real(groups)
        run_sql(world.db, 'UPDATE InvoiceItems SET TotalNet = 600000 WHERE InvoiceId = ? AND Position = 2', invoice_id)
        run_sql(world.db, 'UPDATE Invoices SET SumNet = 1600000, TaxAmount = 232000, SumGross = 1832000 WHERE ID = ?', invoice_id)
        return found

    monkeypatch.setattr(world.db, '_mixed_receivable_accounts', prepared_then_changed)

    with pytest.raises(ValueError) as refused:
        world.db.ensure_receivable_booking(invoice_id)

    assert str(refused.value) == CHANGED_MESSAGE and receivables(world.db) == []


def test_the_waiting_account_of_a_rate_is_found_only_for_exactly_that_rate(world):
    """Nicht über ``_tax_rate_key`` (der rundet auf ganze Prozent): 5,5 % wäre dort 6, 7,4 % wäre 7."""
    assert world.db._group_waiting_entry(Decimal(19)) == (4405, 'Erlöse 19% USt noch offen')
    assert world.db._group_waiting_entry(Decimal(7)) == (4345, 'Umsatzerlöse 7% noch offen')
    assert world.db._group_waiting_entry(Decimal(0)) == (4340, 'Umsatzerlöse 0% noch offen')
    assert world.db._group_waiting_entry(Decimal('5.5')) is None
    assert world.db._group_waiting_entry(Decimal('7.4')) is None
    assert world.db._group_waiting_entry(Decimal('19.5')) is None


def test_a_group_with_0_percent_gets_its_waiting_account_but_no_revenue_account_so_the_payment_is_refused(world):
    """0 % (so ein Satz ist im Formular nicht wählbar, nur per SQL herstellbar) hat ein Wartekonto (4340, ohne Satz und ohne Steuer an der Zeile), aber
    bewusst kein Erlöskonto – nie 4184/4185, die Konten der Kleinunternehmer: Die Zahlung lehnt mit dem Klartext ab, bis das entschieden ist."""
    invoice_id = mixed_draft(world)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 0 WHERE InvoiceId = ? AND Position = 2', invoice_id)
    run_sql(world.db, 'UPDATE Invoices SET TaxAmount = 190000, SumGross = 1690000 WHERE ID = ?', invoice_id)          # 19 %: 19,00; 0 %: keine Steuer
    drop_accounts(world.db, 4340, 4184, 4185)                                                        # weder 4184 noch 4185 darf neu entstehen

    assert world.db.set_invoice_status(invoice_id, 'sent', USER).ok

    assert receivables(world.db) == [(10000, 4405, 11900, 0.19, 1900, 'transfer', DATE, TEXT), (10000, 4340, 5000, None, None, 'transfer', DATE, TEXT)]
    assignment = world.db.assign_invoice_payment(invoice_id, incoming(world, amount=100.0))
    assert assignment == PaymentAssignment(False, MIXED_NO_REVENUE_COA.format(rate='0'), None, None)
    assert world.db.get_coa_id_by_account_number(4184) is None and world.db.get_coa_id_by_account_number(4185) is None      # nie die Konten der Kleinunternehmer


def test_a_rate_without_a_waiting_account_gets_no_receivable_and_no_account_and_the_payment_names_its_revenue_account(world):
    """5,5 % hat kein Wartekonto: Der Beleg wird versendet, aber es wird gar keine Forderung gebucht (auch für 19 % nicht) und kein Konto angelegt.
    Die Zahlung lehnt ab, weil für 5,5 % kein Erlöskonto hinterlegt ist – in der Schreibweise des Satzes mit Komma."""
    invoice_id = mixed_draft(world)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 5.5 WHERE InvoiceId = ? AND Position = 2', invoice_id)
    run_sql(world.db, 'UPDATE Invoices SET TaxAmount = 217500, SumGross = 1717500 WHERE ID = ?', invoice_id)        # 19 %: 19,00, 5,5 %: 2,75
    drop_accounts(world.db, 10000, 4405, 4345)
    accounts = chart(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code) == (True, 200) and columns(world.db, invoice_id)['Status'] == 'sent'
    assert receivables(world.db) == [] and chart(world.db) == accounts
    booking = incoming(world, amount=100.0)
    assignment = world.db.assign_invoice_payment(invoice_id, booking)
    assert assignment == PaymentAssignment(False, MIXED_NO_REVENUE_COA.format(rate='5,5'), None, None)
    assert 'Für 5,5 % ist kein Erlöskonto hinterlegt' in assignment.message and chart(world.db) == accounts


# ═════════════════════════════════════════════════════════════════════════════
# Zahlung zuordnen
# ═════════════════════════════════════════════════════════════════════════════

def test_the_whole_amount_is_assigned_per_rate_and_the_invoice_is_paid(world):
    invoice_id = mixed_sent(world)
    booking = incoming(world, amount=172.50)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment.ok and assignment.message is None and assignment.revenue_coa_id is None        # kein einzelnes Erlöskonto
    assert marked(world.db) == FULL
    assert standing(world.db, invoice_id) == ('paid', 0)


def test_the_marked_rows_carry_date_recipient_text_and_marker_of_the_movement(world):
    invoice_id = mixed_sent(world)
    booking = incoming(world, amount=172.50, day='2026-07-02')
    assert world.db.assign_invoice_payment(invoice_id, booking).ok

    rows = query_rows(world.db, "SELECT * FROM Bookings WHERE Status = 'invoice_payment' ORDER BY ID")

    assert [r['Text'] for r in rows] == [f'Zahlung zu Re. {NUMBER}', f'Umb. zu Re. {NUMBER}', f'Umb. zu Re. {NUMBER}']
    assert {(r['DateBooking'], r['RecipientClient'], r['Contact_ID'], r['ParentBooking_ID'], r['Currency'], r['BookingType'])
            for r in rows} == {('2026-07-02', 'Malermeister Pinsel', world.cust, booking, 'EUR', 'entry')}
    assert [r['DocumentNumber'] for r in rows] == [NUMBER] * 3 and [r['EntryForm'] for r in rows] == ['liquid_first', 'transfer', 'transfer']


def test_a_part_payment_is_split_by_the_rule_and_the_closing_payment_takes_the_rest(world):
    invoice_id = mixed_sent(world)

    pay_on(world, invoice_id, 100.0, '2026-07-01')
    assert marked(world.db) == FIRST and standing(world.db, invoice_id) == ('partial_payment', 7250)

    pay_on(world, invoice_id, 72.50, '2026-07-05')
    assert marked(world.db) == FIRST + SECOND and standing(world.db, invoice_id) == ('paid', 0)
    assert [r['Amount'] for r in query_rows(world.db, 'SELECT Amount FROM InvoicePayments ORDER BY ID')] == [1000000, 725000]


def test_the_sums_per_rate_are_exactly_the_gross_and_the_tax_of_each_group(world):
    invoice_id = mixed_sent(world)
    for amount, day in ((50.0, '2026-07-01'), (50.0, '2026-07-02'), (72.50, '2026-07-03')):
        pay_on(world, invoice_id, amount, day)

    sums = {}
    for coa, counter, amount, tax, rate, _form in marked(world.db):
        if coa in (4405, 4345):
            sums[coa] = (sums.get(coa, (0, 0))[0] + amount, sums.get(coa, (0, 0))[1] + tax)
    assert sums == {4405: (11900, 1900), 4345: (5350, 350)}


def test_a_collective_payment_assigns_to_the_mixed_invoice_and_to_an_invoice_with_one_rate(world):
    mixed = mixed_sent(world)
    single = sent(world, 'E-100')                                    # 19 % × 100,00 = 119,00
    booking = incoming(world, amount=291.50)

    for invoice_id in (mixed, single):
        ok, error = handlers.link_booking_to_invoice_capped(world.db, invoice_id, booking)
        assert ok, error

    assert marked(world.db, NUMBER) == FULL
    assert marked(world.db, 'E-100') == [(1800, 10000, 11900, None, None, 'liquid_first'), (4405, 4400, 11900, 1900, 0.19, 'transfer')]
    assert world.db.get_booking_unallocated_amount(booking) == 0
    assert standing(world.db, mixed) == ('paid', 0) and standing(world.db, single) == ('paid', 0)


def test_an_overpayment_leaves_the_rest_as_the_free_amount_of_the_movement(world):
    invoice_id = mixed_sent(world)
    booking = incoming(world, amount=200.0)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment.ok and marked(world.db) == FULL
    assert world.db.get_booking_unallocated_amount(booking) == Decimal('27.50')


def test_the_requested_amount_is_split_too(world):
    invoice_id = mixed_sent(world)
    booking = incoming(world, amount=172.50)

    assert world.db.assign_invoice_payment(invoice_id, booking, amount=100.0).ok

    assert marked(world.db) == FIRST and standing(world.db, invoice_id) == ('partial_payment', 7250)


# ── Entfernen ────────────────────────────────────────────────────────────────

def test_removing_the_part_payment_removes_all_its_rows_and_the_invoice_is_open_again(world):
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    (payment,) = allocation_ids(world.db, invoice_id)

    world.db.unassign_invoice_payment(payment)

    assert marked(world.db) == [] and standing(world.db, invoice_id) == ('sent', 17250)
    assert receivables(world.db) == RECEIVABLES                       # die Forderungen bleiben


def test_removing_one_payment_leaves_the_other_one_as_it_is(world):
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0, '2026-07-01')
    pay_on(world, invoice_id, 40.0, '2026-07-02')
    first, second = allocation_ids(world.db, invoice_id)
    assert len(marked(world.db)) == 6

    world.db.unassign_invoice_payment(second)

    assert marked(world.db) == FIRST and standing(world.db, invoice_id) == ('partial_payment', 7250)
    assert allocation_ids(world.db, invoice_id) == [first]


def test_the_match_resolves_both_receivables_together_and_removing_a_payment_reopens_both(world):
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0, '2026-07-01')
    assert world.db.link_bank_to_entries()['resolved'] == 0                # nach der Teilzahlung ist nichts gedeckt
    pay_on(world, invoice_id, 72.50, '2026-07-05')
    assert world.db.link_bank_to_entries()['resolved'] == 2                # beide Zeilen, gemeinsam
    status = lambda: [r['Status'] for r in query_rows(world.db, "SELECT Status FROM Bookings WHERE DocumentNumber = ? AND BookingType = 'entry' "
                                                                 "AND ParentBooking_ID IS NULL ORDER BY ID", NUMBER)]
    assert status() == ['resolved', 'resolved']

    world.db.unassign_invoice_payment(allocation_ids(world.db, invoice_id)[-1])

    assert status() == [None, None]
    assert world.db.link_bank_to_entries()['resolved'] == 0


# ── Bank-Editor ──────────────────────────────────────────────────────────────

def test_removing_one_row_of_a_part_payment_in_the_editor_removes_the_whole_assignment(world):
    """Das ✕ an der Zeile „4345 an 4300“ nimmt die Zuordnung mit allen drei Zeilen weg – auch bei mehr als zwei Sätzen: Die Rechnung ist wieder offen."""
    invoice_id = mixed_sent(world)
    bank = pay_on(world, invoice_id, 100.0)
    children = [r['ID'] for r in query_rows(world.db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank)]
    assert len(children) == 3
    post, _options = open_form(world.db, bank)

    submit(world.db, drop_row(post, children[2]))                    # die Zeile des 7-%-Satzes

    assert query_rows(world.db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', bank) == []
    assert allocation_ids(world.db, invoice_id) == [] and standing(world.db, invoice_id) == ('sent', 17250)
    assert receivables(world.db) == RECEIVABLES


# ── Ohne Forderung ───────────────────────────────────────────────────────────

def test_without_a_receivable_each_rate_is_booked_as_revenue_to_bank(world):
    invoice_id = mixed_sent(world)
    run_sql(world.db, "DELETE FROM Bookings WHERE DocumentNumber = ? AND ParentBooking_ID IS NULL", NUMBER)    # die Forderungen sind weg

    pay_on(world, invoice_id, 100.0)

    assert marked(world.db) == [(4400, 1800, 6899, 1102, 0.19, 'purpose_first'), (4300, 1800, 3101, 203, 0.07, 'purpose_first')]
    assert standing(world.db, invoice_id) == ('partial_payment', 7250)


# ── Ablehnungen: nichts wird geschrieben ─────────────────────────────────────

def test_only_one_of_the_two_receivables_present_is_refused(world):
    invoice_id = mixed_sent(world)
    run_sql(world.db, 'DELETE FROM Bookings WHERE ID = (SELECT MAX(ID) FROM Bookings WHERE DocumentNumber = ? AND ParentBooking_ID IS NULL)', NUMBER)
    booking = incoming(world, amount=100.0)
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_RECEIVABLE_MISMATCH, None, None) and tables(world.db) == before


def test_a_receivable_with_a_rate_the_invoice_does_not_carry_is_refused(world):
    invoice_id = mixed_sent(world)
    run_sql(world.db, 'UPDATE Bookings SET TaxRate = 0.16 WHERE DocumentNumber = ? AND TaxRate = 0.07', NUMBER)
    booking = incoming(world, amount=100.0)
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_RECEIVABLE_MISMATCH, None, None) and tables(world.db) == before


def test_two_receivables_of_the_same_rate_are_refused(world):
    invoice_id = mixed_sent(world)
    run_sql(world.db, 'UPDATE Bookings SET TaxRate = 0.19 WHERE DocumentNumber = ? AND TaxRate = 0.07', NUMBER)
    booking = incoming(world, amount=100.0)
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_RECEIVABLE_MISMATCH, None, None) and tables(world.db) == before


def test_an_existing_allocation_without_the_rows_of_the_app_is_refused(world):
    """Eine Zuordnung ohne die Buchungssätze der App (so legt sie nur ein Eingriff an): Wie viel jeder Satz schon trägt, ist unbekannt."""
    invoice_id = mixed_sent(world)
    older = world.db.insert_booking('2026-06-30', 50.0, booking_type='bank', account_id=world.bank)
    run_sql(world.db, "INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 500000, '2026-06-30')", invoice_id, older)
    booking = incoming(world, amount=100.0)
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_ALLOCATION_UNREADABLE, None, None) and tables(world.db) == before


def test_an_allocation_whose_rows_were_changed_by_hand_is_refused(world):
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    run_sql(world.db, "UPDATE Bookings SET Amount = Amount + 100 WHERE Status = 'invoice_payment' AND TaxRate = 0.07")   # ein Cent mehr auf 7 %
    booking = incoming(world, amount=72.50, day='2026-07-05')
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_ALLOCATION_UNREADABLE, None, None) and tables(world.db) == before


def test_without_the_revenue_account_of_a_rate_the_payment_is_refused_and_no_account_is_created(world):
    invoice_id = mixed_sent(world)
    drop_accounts(world.db, 4300)
    booking = incoming(world, amount=100.0)
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking, revenue_coa_id=world.db.get_coa_id_by_account_number(4400))

    assert assignment == PaymentAssignment(False, MIXED_NO_REVENUE_COA.format(rate='7'), None, None)      # eine Wahl gibt es nicht
    assert '7 %' in assignment.message and tables(world.db) == before


def test_a_group_that_is_not_positive_refuses_the_payment_too(world):
    items = MIXED_ITEMS + [{'position': 3, 'quantity': 1, 'unit': 'C62', 'description': 'Rabatt', 'unitPrice': -60.0, 'taxRate': 7}]
    invoice_id = make_invoice(world, 'draft', number='M-300', items=items, taxMode='mixed')
    run_sql(world.db, "UPDATE Invoices SET Status = 'sent' WHERE ID = ?", invoice_id)             # gesendet ist so ein Beleg nicht zu bekommen
    booking = incoming(world, amount=100.0)
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_GROUP_NOT_POSITIVE.format(rate='7', amount='-10,70 €'), None, None)
    assert tables(world.db) == before


def test_positions_that_do_not_match_the_sums_refuse_the_payment(world):
    invoice_id = mixed_sent(world)
    run_sql(world.db, 'UPDATE Invoices SET SumNet = SumNet + 100 WHERE ID = ?', invoice_id)
    booking = incoming(world, amount=100.0)
    before = tables(world.db)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_GROUPS_MISMATCH, None, None) and tables(world.db) == before


# ── Vorprüfung der Maske, Route, Kontext ─────────────────────────────────────

def test_the_mask_of_a_new_bank_movement_finds_nothing_to_say_for_a_mixed_invoice(world):
    invoice_id = mixed_sent(world)
    before = tables(world.db)

    assert world.db.payment_precheck(invoice_id, world.bank) is None
    assert handlers._new_payment_problem(world.db, invoice_id, world.bank, '172.50') is None
    assert tables(world.db) == before


def test_the_precheck_names_the_rate_without_a_revenue_account_and_creates_none(world):
    invoice_id = mixed_sent(world)
    drop_accounts(world.db, 4300)
    before = tables(world.db)

    assert world.db.payment_precheck(invoice_id, world.bank) == MIXED_NO_REVENUE_COA.format(rate='7')
    assert handlers._new_payment_problem(world.db, invoice_id, world.bank, '172.50') == MIXED_NO_REVENUE_COA.format(rate='7')
    assert tables(world.db) == before


def test_the_route_assigns_a_payment_to_a_mixed_invoice(world):
    invoice_id = mixed_sent(world)
    booking = incoming(world, amount=172.50)

    code, text = handlers.handle_link_invoice_payment(json.dumps({'invoice_id': invoice_id, 'transaction_id': booking}).encode())

    assert (code, text) == (200, f'/invoice/view?id={invoice_id}') and marked(world.db) == FULL


def test_the_route_refuses_with_the_sentence_of_the_rate_without_a_revenue_account(world):
    invoice_id = mixed_sent(world)
    drop_accounts(world.db, 4300)
    booking = incoming(world, amount=172.50)
    before = tables(world.db)

    code, text = handlers.handle_link_invoice_payment(json.dumps({'invoice_id': invoice_id, 'transaction_id': booking,
                                                                  'revenue_coa_id': 1}).encode())

    assert (code, text) == (400, MIXED_NO_REVENUE_COA.format(rate='7')) and tables(world.db) == before


def test_the_bank_movement_gets_customer_and_number_but_no_account_rate_or_tax(world):
    """Der Anzeige-Backfill der Bankzeile: Kunde und Beleg-Nr. wie immer; ein Konto, ein Satz oder eine Steuer für die ganze Bewegung gäbe es bei
    mehreren Sätzen nicht (der Satz im Kopf ist nur ein Platzhalter)."""
    invoice_id = mixed_sent(world)

    booking = pay_on(world, invoice_id, 172.50)

    bank = query_rows(world.db, 'SELECT * FROM Bookings WHERE ID = ?', booking)[0]
    assert bank['Contact_ID'] == world.cust and bank['DocumentNumber'] == NUMBER
    assert (bank['COA_ID'], bank['TaxRate'], bank['TaxAmount']) == (None, None, None)


def test_the_bank_movement_of_an_invoice_with_one_rate_still_gets_account_rate_and_tax(world):
    invoice_id = sent(world, 'T-100')

    booking = pay_on(world, invoice_id, 119.0)

    bank = query_rows(world.db, 'SELECT * FROM Bookings WHERE ID = ?', booking)[0]
    assert (bank['COA_ID'], bank['TaxRate'], cents(bank['TaxAmount'])) == (world.db.get_coa_id_by_account_number(4400), 0.19, 1900)


def test_a_context_without_the_key_of_a_mixed_invoice_is_refused_and_nothing_is_written(world):
    """Neuaufbau und Umzug tragen keinen Schlüssel ``mixed`` in ihrem Kontext: Der Baustein in der Transaktion bucht einen gekennzeichneten Beleg
    dann nicht (mit einem Satz im Kopf buchte er falsch) und lehnt mit dem „geändert“-Satz ab."""
    invoice_id = mixed_sent(world)
    booking = incoming(world, amount=172.50)
    error, context = world.db._payment_prerequisites(invoice_id, world.bank)
    assert error is None and set(context['mixed']) == {'groups', 'revenue'}
    old_style = {key: value for key, value in context.items() if key != 'mixed'}
    before = tables(world.db)

    with world.db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        result = world.db._assign_in_tx(tx, invoice_id, booking, None, old_style)

    assert result == PaymentAssignment(False, PAYMENT_CHANGED_MESSAGE, None, None) and tables(world.db) == before


def test_a_context_of_a_mixed_invoice_is_refused_for_an_invoice_without_the_flag(world):
    invoice_id = mixed_sent(world)
    single = sent(world, 'T-100')
    booking = incoming(world, amount=119.0)
    error, context = world.db._payment_prerequisites(invoice_id, world.bank)
    assert error is None
    before = tables(world.db)

    with world.db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        result = world.db._assign_in_tx(tx, single, booking, None, {**context, 'tax_rate': 0.19})

    assert result == PaymentAssignment(False, PAYMENT_CHANGED_MESSAGE, None, None) and tables(world.db) == before


def test_the_groups_changed_after_the_precheck_are_caught_in_the_transaction(world):
    invoice_id = mixed_sent(world)
    booking = incoming(world, amount=172.50)
    error, context = world.db._payment_prerequisites(invoice_id, world.bank)
    assert error is None
    run_sql(world.db, 'UPDATE InvoiceItems SET TotalNet = 600000 WHERE InvoiceId = ? AND Position = 2', invoice_id)
    run_sql(world.db, 'UPDATE Invoices SET SumNet = 1600000, TaxAmount = 232000, SumGross = 1832000 WHERE ID = ?', invoice_id)
    before = tables(world.db)

    with world.db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        result = world.db._assign_in_tx(tx, invoice_id, booking, None, context)

    assert result == PaymentAssignment(False, PAYMENT_CHANGED_MESSAGE, None, None) and tables(world.db) == before


# ── Lernen des Erlöskontos je Satz ───────────────────────────────────────────

def test_the_revenue_account_learned_per_rate_comes_from_the_rate_of_the_row_not_from_the_head(world):
    """Nach einer Zahlung auf M ist die jüngste markierte Zeile „4345 an 4300“ (7 %). Der Satz im Kopf von M ist 0,19 (der höchste Satz): Am alten
    Code lernte 19 % deshalb das Konto der 7 % – ``resolve_revenue_coa(0.19)`` lieferte 4300."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 172.50)

    assert world.db.resolve_revenue_coa(0.19) == world.db.get_coa_id_by_account_number(4400)
    assert world.db.resolve_revenue_coa(0.07) == world.db.get_coa_id_by_account_number(4300)


def test_a_learned_account_for_one_rate_is_taken_for_that_rate_only(world):
    """Liegen die Sätze einer Zahlung für 7 % auf einem anderen Erlöskonto (hier von Hand auf 4301 gebucht), lernt genau dieser Satz es – für jede
    Rechnung mit 7 %, die mit einem Satz auch; 19 % behält 4400."""
    world.db.insert_chart_of_accounts(4, 4301, 'Umsatzerlöse 7% Ersatz', 'Betriebliche Erträge', is_standard=1)
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 172.50)
    run_sql(world.db, "UPDATE Bookings SET CounterCOA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4301) "
                      "WHERE Status = 'invoice_payment' AND TaxRate = 0.07 AND EntryForm = 'transfer'")

    assert world.db.resolve_revenue_coa(0.07) == world.db.get_coa_id_by_account_number(4301)
    assert world.db.resolve_revenue_coa(0.19) == world.db.get_coa_id_by_account_number(4400)


def test_the_learning_of_an_invoice_with_one_rate_is_unchanged(world):
    invoice_id = sent(world, 'T-100')
    pay_on(world, invoice_id, 119.0)

    assert world.db.resolve_revenue_coa(0.19) == world.db.get_coa_id_by_account_number(4400)
    assert world.db.resolve_revenue_coa(0.07) == world.db.get_coa_id_by_account_number(4300)


# ═════════════════════════════════════════════════════════════════════════════
# Anzeigen
# ═════════════════════════════════════════════════════════════════════════════

def test_the_payment_box_and_the_mask_offer_no_choice_of_the_revenue_account_even_if_4400_is_missing(world):
    mixed = mixed_sent(world)
    single = sent(world, 'T-200')
    drop_accounts(world.db, 4400)                                    # bei einer Rechnung mit einem Satz erschiene jetzt die Wahl
    before = tables(world.db)

    assert 'name="revenue_coa_id"' in PageInvoice(world.db, {}, single)
    assert 'name="revenue_coa_id"' in PageTransactions(world.db, from_invoice=single)
    box = PageInvoice(world.db, {}, mixed)
    mask = PageTransactions(world.db, from_invoice=mixed)

    assert 'name="revenue_coa_id"' not in box and 'kein Erlöskonto hinterlegt' not in box
    assert 'name="revenue_coa_id"' not in mask and 'Zahlung zu Rechnung M-100' in mask
    assert tables(world.db) == before                                # Anzeigen ändert nichts


# ═════════════════════════════════════════════════════════════════════════════
# Die Ersatzrechnung einer gemischten Rechnung lässt sich versenden: Forderung je Satz, die Zahlungen bleiben an der stornierten Rechnung
# ═════════════════════════════════════════════════════════════════════════════

def test_a_mixed_replacement_is_sent_with_one_receivable_per_rate_and_the_payments_stay_where_they_are(world, monkeypatch):
    s = paid_scenario(world, monkeypatch, paid=50.0)
    edit_draft(world, s.r2, MIXED_ITEMS, taxMode='mixed')              # die Ersatzrechnung trägt jetzt zwei Sätze
    number = columns(world.db, s.r2)['InvoiceNumber']
    assert columns(world.db, s.r2)['MixedTaxRates'] == 1

    change = world.db.set_invoice_status(s.r2, 'sent', USER)

    assert change.ok and change.moved == 0
    assert change.payment_hint == MOVE_MIXED_HINT.format(number='T-100', amount='50,00 €')
    assert [row[:6] for row in receivables(world.db, number)] == [(10000, 4405, 11900, 0.19, 1900, 'transfer'),
                                                                  (10000, 4345, 5350, 0.07, 350, 'transfer')]
    assert allocation_ids(world.db, s.r2) == [] and len(allocation_ids(world.db, s.r1)) == 1        # die Zahlung bleibt an der stornierten Rechnung


# ═════════════════════════════════════════════════════════════════════════════
# ``payment_rows(parts=…)``: die Sätze einer Zuordnung je Satz
# ═════════════════════════════════════════════════════════════════════════════

ROW_ARGUMENTS = dict(number='M-1', customer_id=3, bank_id=9, bank_date='2026-07-01', bank_tax_date='2026-07-02', recipient='Kunde', currency='EUR',
                     bank_coa=1, revenue_coa=None)


def shown_rows(rows):
    """Konto, Gegenkonto, Betrag, Satz, Steuer, Form, Text je Satz."""
    return [(r.values['COA_ID'], r.values['CounterCOA_ID'], r.values['Amount'], r.values['TaxRate'], r.values['TaxAmount'], r.values['EntryForm'],
             r.values['Text']) for r in rows]


def test_the_rows_with_a_receivable_are_the_settlement_over_the_whole_amount_and_one_transfer_per_part():
    from db.invoice_payments import PaymentPart, payment_rows
    parts = [PaymentPart(5, 6, 689900, 0.19, 110200), PaymentPart(7, 8, 310100, 0.07, 20300)]

    rows = payment_rows(**ROW_ARGUMENTS, amount=1000000, tax_rate=None, tax_value=None, debtor_side=2, parts=parts)

    assert shown_rows(rows) == [(1, 2, 1000000, None, None, 'liquid_first', 'Zahlung zu Re. M-1'),
                                (5, 6, 689900, 0.19, 110200, 'transfer', 'Umb. zu Re. M-1'),
                                (7, 8, 310100, 0.07, 20300, 'transfer', 'Umb. zu Re. M-1')]
    assert [r.description for r in rows] == ['Payment child (bank to debtor)', 'Payment child (open item to revenue)', 'Payment child (open item to revenue)']
    assert {(r.values['DateBooking'], r.values['DateTax'], r.values['RecipientClient'], r.values['Contact_ID'], r.values['ParentBooking_ID'],
             r.values['Status'], r.values['DocumentNumber'], r.values['Currency']) for r in rows} == {
        ('2026-07-01', '2026-07-02', 'Kunde', 3, 9, 'invoice_payment', 'M-1', 'EUR')}


def test_the_rows_without_a_receivable_are_one_revenue_to_bank_row_per_part():
    from db.invoice_payments import PaymentPart, payment_rows
    parts = [PaymentPart(None, 6, 689900, 0.19, 110200), PaymentPart(None, 8, 310100, None, None)]        # der zweite Satz ohne Steuersatz (0 %)

    rows = payment_rows(**ROW_ARGUMENTS, amount=1000000, tax_rate=None, tax_value=None, parts=parts)

    assert shown_rows(rows) == [(6, 1, 689900, 0.19, 110200, 'purpose_first', 'Zahlung zu Re. M-1'),
                                (8, 1, 310100, None, None, 'purpose_first', 'Zahlung zu Re. M-1')]


def test_a_refund_has_the_texts_of_a_refund_for_each_part():
    from db.invoice_payments import PaymentPart, payment_rows
    parts = [PaymentPart(5, 6, -2760, 0.19, -441)]

    rows = payment_rows(**ROW_ARGUMENTS, amount=-2760, tax_rate=None, tax_value=None, debtor_side=2, parts=parts)

    assert [r[6] for r in shown_rows(rows)] == ['Erstattung zu Re. M-1', 'Umb. Erstattung zu Re. M-1']


@pytest.mark.parametrize('parts, amount, debtor', [
    ([], 100, 2),                                                          # keine Teile
    ([(5, 6, 60), (7, 8, 30)], 100, 2),                                    # die Teile ergeben den Betrag nicht
    ([(5, 6, 100), (7, 8, 0)], 100, 2),                                    # ein Teil ohne Betrag: Dafür gibt es keine Zeile
    ([(None, 6, 100)], 100, 2),                                            # mit Forderung braucht jeder Teil ein Wartekonto
])
def test_inconsistent_parts_are_refused(parts, amount, debtor):
    from db.invoice_payments import PaymentPart, payment_rows
    with pytest.raises(ValueError):
        payment_rows(**ROW_ARGUMENTS, amount=amount, tax_rate=None, tax_value=None, debtor_side=debtor,
                     parts=[PaymentPart(waiting, revenue, value, 0.19, 1) for waiting, revenue, value in parts])


def test_without_parts_the_rows_are_the_ones_of_an_invoice_with_one_rate():
    from db.invoice_payments import payment_rows

    rows = payment_rows(**{**ROW_ARGUMENTS, 'revenue_coa': 4}, amount=1190000, tax_rate=0.19, tax_value=190000, debtor_side=2, waiting_side=5)
    without_receivable = payment_rows(**{**ROW_ARGUMENTS, 'revenue_coa': 4}, amount=1190000, tax_rate=0.19, tax_value=190000)

    assert shown_rows(rows) == [(1, 2, 1190000, None, None, 'liquid_first', 'Zahlung zu Re. M-1'),
                                (5, 4, 1190000, 0.19, 190000, 'transfer', 'Umb. zu Re. M-1')]
    assert shown_rows(without_receivable) == [(4, 1, 1190000, 0.19, 190000, 'purpose_first', 'Zahlung zu Re. M-1')]
