# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Das Storno-Paar eines Belegs mit Steuersatz je Position (2.7.0, Etappe 2b): Stornieren, Erstattung, „Stornieren und neu ausstellen“, Neuaufbau der
Zuordnung, Prüfliste und Anzeigen.

Die feste Rechnung ist **M** (Nummer ``M-100``): 19 % × 100,00 und 7 % × 50,00 – Gruppe 19: netto 100,00 / Steuer 19,00 / brutto 119,00, Gruppe 7: 50,00 /
3,50 / 53,50, zusammen 150,00 / 22,50 / 172,50. Eine Teilzahlung von 100,00 verteilt ``tax_groups.split_payment`` auf 68,99 (Steuer 11,02) und 31,01
(2,03). Beträge in den Vergleichen stehen in Cent.

* **Stornieren** (``cancel_invoice``, die Route): Die Stornorechnung trägt das Kennzeichen und die negierten Summen und Positionen, und die Gegenbuchung
  der Forderung bucht je Satz eine Zeile „10000 an Wartekonto“ mit negativem Betrag, negativer Steuer und dem Satz der Gruppe (Nummer und Datum der
  Stornorechnung). Positionen, die nicht zu den Summen passen, und ein Satz mit Brutto unter 0 lehnen mit einem festen Satz ab – vor dem ersten Schreiben,
  ohne ein Konto anzulegen.
* **Erstattung** (``assign_storno_refund``, die Route): Der Betrag verteilt sich nach derselben Regel wie eine Zahlung, gerechnet an dem, was die Zahlungen
  je Satz getragen haben, abzüglich der früheren Erstattungen; „Bank an Debitoren“ und je Satz „Wartekonto an Erlöskonto“ (negativ). Das Erlöskonto je Satz
  ist das der Zahlungen – der Nutzer wählt keines. Teilerstattung 40,00: 27,60 (Steuer 4,41) und 12,40 (0,81); der Rest 60,00: 41,39 (6,61) und 18,61
  (1,22); danach ist das Guthaben 0.
* **Entfernen einer Zahlung nach einer Erstattung:** Die Prüfung des Guthabens (``_refund_removal_blocker``, jeder Weg) gilt bei so einem Paar zusätzlich je Satz –
  die Summe kann aufgehen, und doch hätte ein Satz danach mehr zurückbekommen, als er trug (``MIXED_REMOVAL_REFUSED``); ein Stand, der sich nicht je Satz lesen lässt,
  bleibt bei der Summe.
* **Neu ausstellen und Umzug:** Die Ersatzrechnung trägt das Kennzeichen, die Zahlungen ziehen nicht um (der Hinweis ist ``MOVE_MIXED_HINT``, nach einer Erstattung
  mit dem Satz dazu; ist alles erstattet, sind es die Sätze des Belegs mit einem Satz).
* **Zuordnung neu aufbauen:** Nur der offene Betrag wird neu gerechnet (Grund ``mixed``), kein Satz wird geschrieben, ein Erlöskonto braucht es nicht.
* **Prüfliste:** ``rate_split_mismatch`` für Belege mit dem Kennzeichen, ``rate_refund_mismatch`` für Storno-Paare mit dem Kennzeichen, bei denen je Satz mehr erstattet
  ist als gezahlt, ``unflagged_rate_mix`` für Belege mit verschiedenen Sätzen ohne es.

Alle Namen, Adressen und Nummern sind erfundene Testwerte; ein „heute“ steht nie da (``CLOCK`` ist der festgesetzte Tag des Servers).
"""
import datetime
import json
import random
import sqlite3
from collections import Counter
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace

import pytest

import server.handlers as handlers
from db import Database
from db.invoice_lock import CHANGED_MESSAGE
from db.invoice_payments import (ALREADY_REFUNDED_MESSAGE, MIXED_ALLOCATION_UNREADABLE, MIXED_GROUP_NOT_POSITIVE, MIXED_GROUPS_MISMATCH,
                                 MIXED_RECEIVABLE_MISMATCH, MIXED_REFUND_REVENUE_AMBIGUOUS, MIXED_REFUND_SPLIT, MIXED_REMOVAL_REFUSED, MOVE_MIXED_HINT,
                                 MOVE_NONE_HINT, MOVE_REFUNDED_HINT, PAYMENT_CHANGED_MESSAGE, PaymentAssignment, PaymentRemovalRefused)
from db.invoice_repair import REBUILD_NOTHING_MESSAGE, REBUILD_SKIP_REASONS
from db.invoice_storno import NO_COUNTER_BOOKING_HINT
from money import fmt_eur, from_minor, to_minor
from server.pages_invoice import PageInvoice, revenue_coa_choice
from tests.entry_form_roundtrip import drop_row, open_form
from tests.invoice_world import columns, force_status, make_invoice, query_rows, run_sql
from tests.susa_helpers import account_id
from tests.test_invoice_reissue import allocations, book_tables, edit_draft, reissue
from tests.test_invoice_refund import _delete_route, _editor, _invoice_page, _refusal, comparable, mixed_payments
from tests.test_invoice_storno import ONE_ITEM, USER, at, sent, tables, world   # noqa: F401  (``world`` ist die Fixture)
from tests.test_mixed_tax_reports import cancel_on, invoice_of, pay as pay_fast, with_banks
from tests.test_mixed_tax_bookings import NUMBER, RECEIVABLES, allocation_ids, marked, mixed_sent, pay_on, receivables, standing
from tests.test_mixed_tax_locks import MIXED_ITEMS, drop_accounts, flag, mixed_draft, outgoing

STORNO_DAY = '2026-07-02'
CLOCK = datetime.date(2026, 7, 31)                  # der Tag, den der Server für „heute“ hält (``today=`` von ``cancel_invoice``)
REFUND_DAY = '2026-07-10'

# Die Zahlungssätze der Teilzahlung 100,00 (``test_mixed_tax_bookings.FIRST``): 68,99 und 31,01
PART_PAYMENT = [(1800, 10000, 10000, None, None, 'liquid_first'), (4405, 4400, 6899, 1102, 0.19, 'transfer'), (4345, 4300, 3101, 203, 0.07, 'transfer')]
# Die Teilerstattung 40,00: 27,60 (Steuer 4,41) und 12,40 (0,81) – ``Bank an Debitoren`` über 40,00 ohne Steuer
FIRST_REFUND = [(1800, 10000, -4000, None, None, 'liquid_first'), (4405, 4400, -2760, -441, 0.19, 'transfer'), (4345, 4300, -1240, -81, 0.07, 'transfer')]
# Der Rest 60,00: 41,39 (6,61) und 18,61 (1,22)
SECOND_REFUND = [(1800, 10000, -6000, None, None, 'liquid_first'), (4405, 4400, -4139, -661, 0.19, 'transfer'), (4345, 4300, -1861, -122, 0.07, 'transfer')]


# ── Hilfen ───────────────────────────────────────────────────────────────────

def cancel_mixed(w, invoice_id, day=STORNO_DAY):
    """Die Rechnung stornieren (Datenbankschicht, der Tag des Servers ist ``CLOCK``): liefert das Ergebnis, das ``ok`` ist."""
    storno = w.db.cancel_invoice(invoice_id, day, USER, today=CLOCK)
    assert storno.ok is True, storno.message
    return storno


def paid_and_cancelled(w, paid=100.0):
    """M, versendet, mit der Zahlung ``paid`` (am 01.07.) und storniert (02.07.): ``(ID der Rechnung, ID der Stornorechnung, Nummer der Stornorechnung)``."""
    invoice_id = mixed_sent(w)
    if paid:
        pay_on(w, invoice_id, paid)
    storno = cancel_mixed(w, invoice_id)
    return invoice_id, storno.storno_id, storno.storno_number


def refund_of(w, storno_id, amount, day=REFUND_DAY, **kwargs):
    """Eine ausgehende Bankbewegung über ``amount`` € (positiv) als Erstattung zuordnen; liefert ``(Bewegung, Ergebnis)``."""
    booking = outgoing(w, -amount, day)
    return booking, w.db.assign_storno_refund(storno_id, booking, **kwargs)


def rows_of(db, number):
    """Die Zahlungssätze zur Nummer: (Konto, Gegenkonto, Betrag, Steuer, Satz, Form) in Cent – ``tests.test_mixed_tax_bookings.marked``."""
    return marked(db, number)


def findings(db, invoice_id=None, category=None):
    found = db.find_payment_inconsistencies(invoice_id)
    return [f for f in found if category is None or f['category'] == category]


def count_statements(world, monkeypatch):
    """Die Anweisungen zählen, die die Datenbankschicht an SQLite schickt (``set_trace_callback`` an jeder neuen Verbindung); liefert die Liste."""
    statements = []
    original = type(world.db)._get_connection

    def counting(self):
        conn = original(self)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(type(world.db), '_get_connection', counting)
    return statements


# ═════════════════════════════════════════════════════════════════════════════
# Stornieren: die Stornorechnung und die Gegenbuchung je Satz
# ═════════════════════════════════════════════════════════════════════════════

def test_cancelling_a_mixed_invoice_books_the_counter_receivable_per_rate(world):
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)

    storno = world.db.cancel_invoice(invoice_id, STORNO_DAY, USER, today=CLOCK)

    assert (storno.ok, storno.code, storno.message, storno.counter_booked) == (True, 200, None, True)
    text = f'Stornorechnung {storno.storno_number} Malermeister Pinsel'
    assert receivables(world.db, storno.storno_number) == [(10000, 4405, -11900, 0.19, -1900, 'transfer', STORNO_DAY, text),
                                                           (10000, 4345, -5350, 0.07, -350, 'transfer', STORNO_DAY, text)]
    assert receivables(world.db) == RECEIVABLES                     # die Forderung der Rechnung bleibt in jedem Fall stehen
    assert standing(world.db, invoice_id) == ('cancelled', 7250)
    assert world.db.get_storno_credit(invoice_id) == Decimal('100.00')


def test_the_credit_note_carries_the_flag_and_the_negated_sums_and_positions(world):
    invoice_id = mixed_sent(world)

    storno = cancel_mixed(world, invoice_id)

    stored = columns(world.db, storno.storno_id)
    assert (stored['InvoiceKind'], stored['CancelsInvoiceId'], stored['MixedTaxRates'], stored['Status']) == ('storno', invoice_id, 1, 'sent')
    assert (stored['SumNet'], stored['TaxAmount'], stored['SumGross'], stored['AmountDue']) == (-1500000, -225000, -1725000, -1725000)
    assert stored['TaxRate'] == columns(world.db, invoice_id)['TaxRate']                      # der Platzhalter im Kopf: der höchste Satz
    positions = query_rows(world.db, 'SELECT Position, Quantity, PricePerUnit, TotalNet, TaxRate FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position',
                           storno.storno_id)
    assert [(p['Position'], p['Quantity'], p['PricePerUnit'], p['TotalNet'], p['TaxRate']) for p in positions] == [
        (1, -1, 1000000, -1000000, 19), (2, -1, 500000, -500000, 7)]


def test_the_counter_receivable_and_the_receivable_cancel_each_other_per_account(world):
    """Auf jedem Konto der Kette heben sich Forderung und Gegenbuchung auf: Debitoren, 4405 und 4345 laufen auf 0."""
    invoice_id = mixed_sent(world)
    cancel_mixed(world, invoice_id)

    rows = query_rows(world.db, "SELECT c.AccountNumber AS coa, g.AccountNumber AS counter, b.Amount FROM Bookings b "
                                "JOIN ChartOfAccounts c ON c.ID = b.COA_ID JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID WHERE b.BookingType = 'entry'")
    balance = {}
    for row in rows:
        balance[row['coa']] = balance.get(row['coa'], 0) + row['Amount']
        balance[row['counter']] = balance.get(row['counter'], 0) - row['Amount']

    assert balance == {10000: 0, 4405: 0, 4345: 0}


def test_the_route_cancels_a_mixed_invoice_without_a_hint_about_a_missing_counter_booking(world, monkeypatch):
    invoice_id = mixed_sent(world)
    at(monkeypatch, '2026-07-31')

    code, body = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': STORNO_DAY}).encode())

    answer = json.loads(body)
    assert code == 200 and answer['success'] is True and 'hint' not in answer, answer
    assert columns(world.db, answer['storno_id'])['InvoiceNumber'] == answer['storno_number']
    assert len(receivables(world.db, answer['storno_number'])) == 2


def test_a_hand_cancelled_mixed_invoice_gets_its_credit_note_afterwards_with_the_counter_booking(world):
    """Von Hand storniert (Altbestand): Die Forderungen der App stehen noch in den Büchern – dann folgt die Gegenbuchung je Satz; Status und Protokoll der
    Rechnung bleiben, wie sie sind."""
    invoice_id = mixed_sent(world)
    force_status(world.db, invoice_id, 'cancelled')

    storno = world.db.cancel_invoice(invoice_id, STORNO_DAY, USER, today=CLOCK)

    assert (storno.ok, storno.counter_booked) == (True, True)
    assert [row[:5] for row in receivables(world.db, storno.storno_number)] == [(10000, 4405, -11900, 0.19, -1900), (10000, 4345, -5350, 0.07, -350)]
    assert columns(world.db, invoice_id)['Status'] == 'cancelled'


def test_without_the_receivables_of_the_app_there_is_no_counter_booking_and_the_hint_says_so(world, monkeypatch):
    invoice_id = mixed_sent(world)
    run_sql(world.db, 'DELETE FROM Bookings WHERE DocumentNumber = ?', NUMBER)               # die Forderungen sind weg (Import, von Hand gelöscht)
    at(monkeypatch, '2026-07-31')

    code, body = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': STORNO_DAY}).encode())

    answer = json.loads(body)
    assert code == 200 and answer['success'] is True and answer['hint'] == NO_COUNTER_BOOKING_HINT
    assert receivables(world.db, answer['storno_number']) == []


def test_a_group_that_is_not_positive_refuses_the_cancellation_with_rate_and_amount_and_creates_no_account(world):
    items = MIXED_ITEMS + [{'position': 3, 'quantity': 1, 'unit': 'C62', 'description': 'Rabatt', 'unitPrice': -60.0, 'taxRate': 7}]
    invoice_id = make_invoice(world, 'draft', number='M-300', items=items, taxMode='mixed')
    run_sql(world.db, "UPDATE Invoices SET Status = 'sent' WHERE ID = ?", invoice_id)       # versendet ist so ein Beleg nicht zu bekommen
    drop_accounts(world.db, 10000, 4405, 4345)
    before = tables(world.db)

    storno = world.db.cancel_invoice(invoice_id, STORNO_DAY, USER, today=CLOCK)

    assert (storno.ok, storno.code) == (False, 409)
    assert storno.message == MIXED_GROUP_NOT_POSITIVE.format(rate='7', amount=fmt_eur(from_minor(-107000))) and '(7 %: -10,70 €)' in storno.message
    assert (storno.storno_id, storno.storno_number, storno.counter_booked) == (None, None, False)
    assert tables(world.db) == before                                                        # auch kein Konto angelegt, keine Nummer verbraucht


def test_positions_that_do_not_match_the_sums_refuse_the_cancellation_and_nothing_is_written(world):
    invoice_id = mixed_sent(world)
    run_sql(world.db, 'UPDATE Invoices SET SumGross = SumGross + 10000 WHERE ID = ?', invoice_id)      # ein Eingriff an der App vorbei
    before = tables(world.db)

    storno = world.db.cancel_invoice(invoice_id, STORNO_DAY, USER, today=CLOCK)

    assert (storno.ok, storno.code, storno.message) == (False, 409, MIXED_GROUPS_MISMATCH) and tables(world.db) == before


def test_the_route_refuses_what_cannot_be_booked_and_creates_no_replacement(world, monkeypatch):
    invoice_id = mixed_sent(world)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 19 WHERE InvoiceId = ? AND Position = 2', invoice_id)      # die Summen stimmen nicht mehr
    before = tables(world.db)
    at(monkeypatch, '2026-07-31')

    code, body = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': STORNO_DAY, 'reissue': True}).encode())

    assert code == 409 and json.loads(body) == {'success': False, 'error': MIXED_GROUPS_MISMATCH}
    assert tables(world.db) == before                                                        # auch keine Ersatzrechnung


def test_the_flag_removed_after_the_preparation_is_caught_in_the_transaction(world, monkeypatch):
    """Die Konten werden vor der Transaktion aufgelöst; verliert der Beleg das Kennzeichen dazwischen, gelten sie für Satzgruppen, nicht für den Satz im
    Kopf – der frisch gelesene Stand entscheidet: „geändert“, nichts geschrieben."""
    invoice_id = mixed_sent(world)
    real = world.db._mixed_receivable_accounts

    def prepared_then_unflagged(groups):
        found = real(groups)
        run_sql(world.db, 'UPDATE Invoices SET MixedTaxRates = NULL WHERE ID = ?', invoice_id)
        return found

    monkeypatch.setattr(world.db, '_mixed_receivable_accounts', prepared_then_unflagged)
    before = tables(world.db)

    storno = world.db.cancel_invoice(invoice_id, STORNO_DAY, USER, today=CLOCK)

    assert (storno.ok, storno.code, storno.message) == (False, 409, CHANGED_MESSAGE)
    assert without_invoices(tables(world.db)) == without_invoices(before) and world.db.find_storno_invoice(invoice_id) is None


def test_the_flag_set_after_the_preparation_is_caught_in_the_transaction(world, monkeypatch):
    """Die andere Richtung: Eine Rechnung mit einem Satz wird nach der Vorbereitung gekennzeichnet – ihre Konten gelten für einen Satz, nicht für Gruppen."""
    invoice_id = sent(world, 'T-100')
    real = world.db._receivable_accounts

    def prepared_then_flagged(rate):
        found = real(rate)
        flag(world.db, invoice_id)
        return found

    monkeypatch.setattr(world.db, '_receivable_accounts', prepared_then_flagged)
    before = tables(world.db)

    storno = world.db.cancel_invoice(invoice_id, STORNO_DAY, USER, today=CLOCK)

    assert (storno.ok, storno.code, storno.message) == (False, 409, CHANGED_MESSAGE)
    assert without_invoices(tables(world.db)) == without_invoices(before) and world.db.find_storno_invoice(invoice_id) is None


def test_a_position_changed_after_the_preparation_is_caught_in_the_transaction(world, monkeypatch):
    invoice_id = mixed_sent(world)
    real = world.db._mixed_receivable_accounts

    def prepared_then_changed(groups):
        found = real(groups)
        run_sql(world.db, 'UPDATE InvoiceItems SET TotalNet = 600000 WHERE InvoiceId = ? AND Position = 2', invoice_id)
        run_sql(world.db, 'UPDATE Invoices SET SumNet = 1600000, TaxAmount = 232000, SumGross = 1832000 WHERE ID = ?', invoice_id)
        return found

    monkeypatch.setattr(world.db, '_mixed_receivable_accounts', prepared_then_changed)
    before = tables(world.db)

    storno = world.db.cancel_invoice(invoice_id, STORNO_DAY, USER, today=CLOCK)

    assert (storno.ok, storno.code, storno.message) == (False, 409, CHANGED_MESSAGE)
    assert tables(world.db)['Bookings'] == before['Bookings'] and tables(world.db)['NumberRanges'] == before['NumberRanges']
    assert columns(world.db, invoice_id)['Status'] == 'sent' and world.db.find_storno_invoice(invoice_id) is None


def test_the_credit_note_whose_positions_do_not_add_up_rolls_everything_back_and_uses_no_number(world, monkeypatch):
    """Ein Eingriff an der Stornorechnung (hier: Die Positionen werden beim Anlegen verfälscht) lässt die Gegenbuchung nicht zu – ganz oder gar nicht."""
    invoice_id = mixed_sent(world)
    real = world.db._copy_items

    def tampered(conn, tx, source_id, target_id, negate=False):
        real(conn, tx, source_id, target_id, negate=negate)
        if negate:
            conn.execute('UPDATE InvoiceItems SET TotalNet = TotalNet - 10000 WHERE InvoiceId = ? AND Position = 1', (target_id,))

    monkeypatch.setattr(world.db, '_copy_items', tampered)
    before = tables(world.db)

    storno = world.db.cancel_invoice(invoice_id, STORNO_DAY, USER, today=CLOCK)

    assert (storno.ok, storno.code, storno.message) == (False, 409, MIXED_GROUPS_MISMATCH)
    assert tables(world.db) == before                                                        # keine Nummer verbraucht, kein Beleg, keine Buchung


def without_invoices(snapshot):
    return {table: rows for table, rows in snapshot.items() if table != 'Invoices'}


# ═════════════════════════════════════════════════════════════════════════════
# Erstattung: je Satz
# ═════════════════════════════════════════════════════════════════════════════

def test_a_part_refund_is_split_by_rate_from_what_each_rate_has_been_paid(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)

    booking, assignment = refund_of(world, storno_id, 40.0)

    assert assignment.ok and assignment.message is None and assignment.revenue_coa_id is None        # es gibt kein einzelnes Erlöskonto
    assert rows_of(world.db, number) == FIRST_REFUND
    assert [(r['BookingID'], r['Amount']) for r in query_rows(world.db, 'SELECT * FROM InvoicePayments WHERE InvoiceID = ?', storno_id)] == [(booking, -400000)]
    assert world.db.get_storno_credit(invoice_id) == Decimal('60.00')
    assert standing(world.db, storno_id)[1] == -17250 + 4000                                          # AmountDue = Brutto − Zuordnungen (negativ)


def test_the_rest_closes_both_rates_with_their_remaining_amount_and_tax_and_the_credit_is_gone(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    refund_of(world, storno_id, 40.0)

    _, assignment = refund_of(world, storno_id, 60.0, '2026-07-11')

    assert assignment.ok
    assert rows_of(world.db, number) == FIRST_REFUND + SECOND_REFUND
    assert world.db.get_storno_credit(invoice_id) == Decimal('0.00') and world.db.get_storno_refunded(invoice_id) == Decimal('100.00')
    refunded = {account: (sum(r[2] for r in rows_of(world.db, number) if r[1] == account), sum(r[3] for r in rows_of(world.db, number) if r[1] == account))
                for account in (4400, 4300)}
    assert refunded == {4400: (-6899, -1102), 4300: (-3101, -203)}                                    # je Satz Σ Erstattung = Σ Zahlung, Betrag und Steuer


def test_the_refund_rows_carry_date_recipient_text_and_marker_of_the_movement(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    booking, _ = refund_of(world, storno_id, 40.0)

    rows = query_rows(world.db, 'SELECT * FROM Bookings WHERE ParentBooking_ID = ? AND DocumentNumber = ? ORDER BY ID', booking, number)

    assert [r['Text'] for r in rows] == [f'Erstattung zu Re. {number}', f'Umb. Erstattung zu Re. {number}', f'Umb. Erstattung zu Re. {number}']
    assert {(r['DateBooking'], r['Status'], r['RecipientClient'], r['Currency'], r['Contact_ID'], r['BookingType']) for r in rows} == {
        (REFUND_DAY, 'invoice_payment', 'Malermeister Pinsel', 'EUR', world.cust, 'entry')}


def test_the_refund_of_the_whole_credit_in_one_step_takes_back_each_rate_with_its_tax(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)

    _, assignment = refund_of(world, storno_id, 100.0)

    assert assignment.ok
    assert rows_of(world.db, number) == [(1800, 10000, -10000, None, None, 'liquid_first'), (4405, 4400, -6899, -1102, 0.19, 'transfer'),
                                         (4345, 4300, -3101, -203, 0.07, 'transfer')]


def test_the_requested_amount_is_split_too_and_the_rest_of_the_movement_stays_free(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    booking = outgoing(world, -80.0, REFUND_DAY)

    assignment = world.db.assign_storno_refund(storno_id, booking, amount=40.0)

    assert assignment.ok and rows_of(world.db, number) == FIRST_REFUND
    assert [row[5] for row in world.db.get_unallocated_outgoing_bookings() if row[0] == booking] == [Decimal('40.00')]


def test_a_choice_of_the_revenue_account_is_ignored_for_a_mixed_invoice(world):
    """Der Nutzer wählt für so einen Beleg kein Erlöskonto: Ein mitgeschicktes Konto ändert nichts – gebucht wird auf die Konten der Zahlungen."""
    invoice_id, storno_id, number = paid_and_cancelled(world)

    _, assignment = refund_of(world, storno_id, 40.0, revenue_coa_id=world.db.get_coa_id_by_account_number(4400))

    assert assignment.ok and rows_of(world.db, number) == FIRST_REFUND


def test_the_route_assigns_the_refund_of_a_mixed_credit_note(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    booking = outgoing(world, -40.0, REFUND_DAY)

    code, text = handlers.handle_link_invoice_payment(json.dumps({'invoice_id': storno_id, 'transaction_id': booking, 'amount_paid': 40.0}).encode())

    assert (code, text) == (200, f'/invoice/view?id={storno_id}')
    assert rows_of(world.db, number) == FIRST_REFUND


def test_the_bank_movement_of_a_refund_gets_only_the_customer(world):
    """Wie bei einem Beleg mit einem Satz: Konto, Satz, Steuer und Beleg-Nr. gehören zur Rechnung, nicht zur Bewegung."""
    invoice_id, storno_id, number = paid_and_cancelled(world)
    booking = outgoing(world, -40.0, REFUND_DAY)
    run_sql(world.db, 'UPDATE Bookings SET Contact_ID = NULL WHERE ID = ?', booking)

    code, _text = handlers.handle_link_invoice_payment(json.dumps({'invoice_id': storno_id, 'transaction_id': booking}).encode())

    stored = query_rows(world.db, 'SELECT Contact_ID, COA_ID, TaxRate, TaxAmount, DocumentNumber FROM Bookings WHERE ID = ?', booking)[0]
    assert code == 200 and stored['Contact_ID'] == world.cust
    assert (stored['COA_ID'], stored['TaxRate'], stored['TaxAmount']) == (None, None, None)


def test_without_a_counter_booking_each_rate_is_booked_as_revenue_to_bank(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    run_sql(world.db, 'DELETE FROM Bookings WHERE DocumentNumber = ? AND ParentBooking_ID IS NULL', number)     # die Gegenbuchung ist weg

    _, assignment = refund_of(world, storno_id, 40.0)

    assert assignment.ok
    assert rows_of(world.db, number) == [(4400, 1800, -2760, -441, 0.19, 'purpose_first'), (4300, 1800, -1240, -81, 0.07, 'purpose_first')]


def test_only_one_of_the_two_counter_rows_present_is_refused_and_nothing_is_written(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    run_sql(world.db, 'DELETE FROM Bookings WHERE ID = (SELECT MAX(ID) FROM Bookings WHERE DocumentNumber = ? AND ParentBooking_ID IS NULL)', number)
    booking = outgoing(world, -40.0, REFUND_DAY)
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_RECEIVABLE_MISMATCH, None, None) and tables(world.db) == before


def test_removing_the_refund_removes_all_its_rows_and_the_credit_is_back(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    refund_of(world, storno_id, 40.0)
    payment_id = allocation_ids(world.db, storno_id)[0]

    world.db.unassign_invoice_payment(payment_id)

    assert rows_of(world.db, number) == [] and allocation_ids(world.db, storno_id) == []
    assert world.db.get_storno_credit(invoice_id) == Decimal('100.00')
    assert [row[:5] for row in receivables(world.db, number)] != []                                   # die Gegenbuchung bleibt


def test_a_payment_the_refund_needs_cannot_be_removed(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    refund_of(world, storno_id, 100.0)

    with pytest.raises(PaymentRemovalRefused) as refused:
        world.db.unassign_invoice_payment(allocation_ids(world.db, invoice_id)[0])

    assert str(refused.value) == ALREADY_REFUNDED_MESSAGE
    assert len(allocation_ids(world.db, invoice_id)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# Entfernen einer Zahlung nach einer Erstattung: Die Prüfung gilt auch je Satz
# ═════════════════════════════════════════════════════════════════════════════
#
# Die Summe der Zuordnungen beider Belege (``ALREADY_REFUNDED_MESSAGE``) reicht bei zwei Sätzen nicht. Zahlungen 10,01 und 10,00 auf M, storniert, 10,01 erstattet:
# Die Summe nach dem Entfernen der Zahlung 10,00 ist 0,00 und geht auf – aber 7 % hätte dann 3,11 zurückbekommen, nachdem es 3,10 trug (19 %: gezahlt 6,91, erstattet
# 6,90). Jeder Weg, der eine Zahlung entfernt, lehnt das mit ``MIXED_REMOVAL_REFUSED`` ab, vor dem ersten Schreiben.

def per_rate(db, number):
    """Was die Sätze „Wartekonto an Erlöskonto“ der Zuordnungen einer Beleg-Nr. je Satz umbuchen: ``{Satz in Prozent: (Betrag, Steuer)}`` in Cent (Erstattungen
    negativ) – aus den Zeilen gelesen, unabhängig vom Code unter Test."""
    return {round(row['TaxRate'] * 100): (row['amount'] // 100, row['tax'] // 100) for row in query_rows(
        db, "SELECT TaxRate, SUM(Amount) AS amount, SUM(COALESCE(TaxAmount, 0)) AS tax FROM Bookings WHERE DocumentNumber = ? AND Status = 'invoice_payment' "
            "AND EntryForm = 'transfer' GROUP BY TaxRate", number)}


def over_refunded(w, payments=('10.01', '10.00'), refunded='10.01'):
    """M mit den Zahlungen ``payments`` (am 01.07., 02.07. …), storniert, ``refunded`` € erstattet. Liefert die Namen, die die Wege brauchen: die zweite Zahlung
    (Bankbewegung ``bank``, ihre Sätze ``kids``, die ID ihrer Zuordnung ``payment_id``), alle Zuordnungen der Rechnung (``payment_ids``) und die Erstattung."""
    invoice_id = mixed_sent(w)
    banks = [pay_on(w, invoice_id, Decimal(amount), f'2026-07-{day:02d}') for day, amount in enumerate(payments, 1)]
    storno = cancel_mixed(w, invoice_id, f'2026-07-{len(payments) + 1:02d}')                   # am Tag nach der letzten Zahlung
    out, assignment = refund_of(w, storno.storno_id, Decimal(refunded))
    assert assignment.ok, assignment.message

    def children(bank):
        return [row['ID'] for row in query_rows(w.db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank)]
    payment_ids = allocation_ids(w.db, invoice_id)
    return SimpleNamespace(w=w, db=w.db, invoice_id=invoice_id, storno_id=storno.storno_id, storno_number=storno.storno_number, banks=banks, bank=banks[-1],
                           kids=children(banks[-1]), payment_ids=payment_ids, payment_id=payment_ids[-1], out=out,
                           refund_id=allocation_ids(w.db, storno.storno_id)[0])


REFUSAL_7 = 'Zu dieser Rechnung wurde bereits erstattet – für 7 % wäre danach mehr erstattet als gezahlt (oder Betrag und Steuer gingen nicht auf). Erst die Erstattung an der Stornorechnung {number} entfernen.'


def test_the_sentence_of_the_refusal_names_the_rate_and_the_credit_note():
    assert MIXED_REMOVAL_REFUSED.format(rate='7', number='26R001') == REFUSAL_7.format(number='26R001')
    assert MIXED_REMOVAL_REFUSED.format(rate='5,5', number='26R002').startswith('Zu dieser Rechnung wurde bereits erstattet – für 5,5 % ')   # der Satz mit Komma
    assert ALREADY_REFUNDED_MESSAGE.split(' – ')[0] == MIXED_REMOVAL_REFUSED.split(' – ')[0]                  # gebaut wie die Summenprüfung


MIXED_WAYS = {
    'Rechnungsseite': _invoice_page,
    'unassign_invoice_payment': lambda c: _refusal(lambda: c.db.unassign_invoice_payment(c.payment_id)),
    'Editor-Satz-1': lambda c: _editor(c, c.kids[0]),
    'Editor-Satz-2': lambda c: _editor(c, c.kids[1]),
    'Editor-Satz-3': lambda c: _editor(c, c.kids[2]),
    'Editor-alle-Sätze': lambda c: _editor(c, *c.kids),
    'remove_split_row': lambda c: _refusal(lambda: c.db.remove_split_row(c.kids[2])),
    'delete_transaction-Satz': lambda c: _refusal(lambda: c.db.delete_transaction(c.kids[1])),
    'delete_transaction-Bankbewegung': lambda c: _refusal(lambda: c.db.delete_transaction(c.bank)),
    'Löschroute-Satz': lambda c: _delete_route(c, c.kids[2]),
    'Löschroute-Bankbewegung': lambda c: _delete_route(c, c.bank),
}


@pytest.mark.parametrize('way', list(MIXED_WAYS.values()), ids=list(MIXED_WAYS))
def test_no_way_removes_a_payment_that_would_leave_a_rate_refunded_beyond_what_it_paid(world, way):
    c = over_refunded(world)
    db, before = c.db, comparable(c.db)
    assert findings(db) == []
    assert per_rate(db, NUMBER) == {19: (1380, 220), 7: (621, 40)} and per_rate(db, c.storno_number) == {19: (-690, -110), 7: (-311, -20)}   # 7 %: 3,11 gegen 3,10

    assert way(c) == REFUSAL_7.format(number=c.storno_number)

    assert comparable(db) == before                                          # Bookings, InvoicePayments, Invoices: Zeile für Zeile
    assert db.get_storno_credit(c.invoice_id) == Decimal('10.00') and len(allocation_ids(db, c.invoice_id)) == 2
    # der Weg, den der Satz nennt: die Erstattung entfernen, die Zahlung entfernen, erneut erstatten
    db.unassign_invoice_payment(c.refund_id)
    assert way(c) is None
    assert len(allocation_ids(db, c.invoice_id)) == 1 and db.get_storno_credit(c.invoice_id) == Decimal('10.01')
    _out, assignment = refund_of(world, c.storno_id, Decimal('10.01'), '2026-07-12')
    assert assignment.ok and db.get_storno_credit(c.invoice_id) == 0 and findings(db) == []
    assert per_rate(db, NUMBER) == {19: (691, 110), 7: (310, 20)}
    assert per_rate(db, c.storno_number) == {19: (-691, -110), 7: (-310, -20)}               # je Satz gezahlt = erstattet, Betrag und Steuer


def test_the_removal_of_the_first_payment_is_still_refused_by_the_sum_with_its_old_sentence(world):
    """Die Summe lehnt zuerst ab (10,00 gezahlt gegen 10,01 erstattet): Der Satz bleibt der von immer, die Prüfung je Satz kommt nur dazu."""
    c = over_refunded(world)

    with pytest.raises(PaymentRemovalRefused) as refused:
        c.db.unassign_invoice_payment(c.payment_ids[0])

    assert str(refused.value) == ALREADY_REFUNDED_MESSAGE


def test_the_check_in_the_transaction_of_the_editor_refuses_what_the_plan_did_not_see(world, monkeypatch):
    """Die Prüfung im Plan ist die Vorab-Prüfung; maßgeblich bleibt die in der Transaktion. Ändert sich der Bestand dazwischen, antwortet der Editor mit dem
    Klartext statt mit einem Fehler – und nichts ist gespeichert, auch die geänderte Bankzeile nicht."""
    c = over_refunded(world)
    monkeypatch.setattr(Database, 'split_removal_blocker', lambda self, ids: None)        # der Plan sah noch nichts
    before = comparable(c.db)
    post_data, _options = open_form(c.db, c.bank)
    post_data['recipient'] = ['Geändert & Söhne']
    post_data = drop_row(post_data, c.kids[2])

    assert handlers.handle_add_transaction(c.db, post_data) == (400, REFUSAL_7.format(number=c.storno_number))

    assert comparable(c.db) == before
    assert query_rows(c.db, 'SELECT RecipientClient FROM Bookings WHERE ID = ?', c.bank) == [{'RecipientClient': 'Malermeister Pinsel'}]


def test_the_editor_checks_all_rows_it_removes_together_per_rate_too(world):
    """Zahlungen 0,23, 0,23, 0,10 und 0,29, storniert, 0,38 erstattet: Die erste Zahlung darf allein gehen, die zweite allein auch (je Satz bleibt genug) – beide
    zusammen nicht: Die Summe geht auf (0,39 gegen 0,38), aber 7 % hätte mehr zurückbekommen, als es trug. Der Editor prüft alle Zeilen, die er entfernt, zusammen."""
    c = over_refunded(world, payments=('0.23', '0.23', '0.10', '0.29'), refunded='0.38')
    rows = {bank: [row['ID'] for row in query_rows(c.db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank)] for bank in c.banks}
    before = comparable(c.db)

    assert c.db.split_removal_blocker(rows[c.banks[0]]) is None and c.db.split_removal_blocker(rows[c.banks[1]]) is None
    assert c.db.split_removal_blocker(rows[c.banks[0]] + rows[c.banks[1]]) == REFUSAL_7.format(number=c.storno_number)
    assert comparable(c.db) == before                                                    # die Prüfung liest nur


def test_a_credit_that_could_not_be_refunded_any_more_is_never_left_behind(world):
    """Zahlungen 0,01, 10,01 und 0,02, storniert, 0,02 erstattet. Ohne die Zahlung 10,01 hätte 7 % (0,01) erstattet, ohne etwas getragen zu haben: Es bliebe ein
    Guthaben (0,01), das sich nicht mehr erstatten ließe (``MIXED_REFUND_SPLIT``). Das Entfernen wird abgelehnt – das Guthaben (10,02) lässt sich in einem Zug erstatten."""
    invoice_id = mixed_sent(world)
    for day, amount in enumerate(['0.01', '10.01', '0.02'], 1):
        pay_on(world, invoice_id, Decimal(amount), f'2026-07-{day:02d}')
    storno = cancel_mixed(world, invoice_id)
    refund_of(world, storno.storno_id, Decimal('0.02'))
    big = allocation_ids(world.db, invoice_id)[1]
    before = tables(world.db)

    with pytest.raises(PaymentRemovalRefused) as refused:
        world.db.unassign_invoice_payment(big)

    assert str(refused.value) == REFUSAL_7.format(number=storno.storno_number)
    assert tables(world.db) == before
    _out, assignment = refund_of(world, storno.storno_id, Decimal('10.02'), '2026-07-11')
    assert assignment.ok and world.db.get_storno_credit(invoice_id) == 0 and findings(world.db) == []


def test_removing_a_payment_stays_allowed_while_nothing_was_refunded(world):
    invoice_id = mixed_sent(world)
    for day, amount in enumerate(['10.01', '10.00'], 1):
        pay_on(world, invoice_id, Decimal(amount), f'2026-07-{day:02d}')
    cancel_mixed(world, invoice_id)

    world.db.unassign_invoice_payment(allocation_ids(world.db, invoice_id)[1])

    assert world.db.get_storno_credit(invoice_id) == Decimal('10.01') and findings(world.db) == []


def test_a_payment_that_each_rate_still_covers_may_go_after_a_refund(world):
    """Zahlungen 100,00 (19 %: 68,99, Steuer 11,02; 7 %: 31,01, 2,03) und 72,50, storniert, 10,00 erstattet (19 %: 6,90, Steuer 1,10; 7 %: 3,10, 0,20): Ohne die
    Zahlung 72,50 trägt jeder Satz noch mehr, als er zurückbekam – das Entfernen bleibt erlaubt. Das Guthaben ist danach 90,00 und lässt sich ganz erstatten; je
    Satz ist dann gezahlt = erstattet."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, Decimal('100.00'), '2026-07-01')
    pay_on(world, invoice_id, Decimal('72.50'), '2026-07-02')
    storno = cancel_mixed(world, invoice_id)
    refund_of(world, storno.storno_id, Decimal('10.00'))
    assert per_rate(world.db, storno.storno_number) == {19: (-690, -110), 7: (-310, -20)}

    world.db.unassign_invoice_payment(allocation_ids(world.db, invoice_id)[1])

    assert per_rate(world.db, NUMBER) == {19: (6899, 1102), 7: (3101, 203)}
    assert world.db.get_storno_credit(invoice_id) == Decimal('90.00') and findings(world.db) == []
    _out, assignment = refund_of(world, storno.storno_id, Decimal('90.00'), '2026-07-12')
    assert assignment.ok and world.db.get_storno_credit(invoice_id) == 0 and findings(world.db) == []
    assert per_rate(world.db, storno.storno_number) == {19: (-6899, -1102), 7: (-3101, -203)}


def test_a_state_that_cannot_be_read_per_rate_keeps_the_check_of_the_sum_and_never_locks_the_removal(world):
    """Eine Zuordnung ohne die Sätze der App (per SQL: ein Import) macht den Stand je Satz unlesbar. Dann gilt, wie bisher, nur die Summe: Das Entfernen ist dort der
    Weg, den Zustand zu bereinigen – solange sie nicht unter 0 fällt."""
    c = over_refunded(world)
    older = world.db.insert_booking('2026-06-30', Decimal('5.00'), booking_type='bank', account_id=world.bank)
    run_sql(world.db, "INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 50000, '2026-06-30')", c.invoice_id, older)

    with closing(sqlite3.connect(c.db.db_name)) as con:
        blocker = Database._refund_removal_blocker
        assert blocker(con, [c.payment_id]) is None                                          # Summe 5,00 ≥ 0; je Satz ist nichts zu lesen
        assert blocker(con, c.payment_ids) == ALREADY_REFUNDED_MESSAGE                       # beide Zahlungen: 5,00 − 10,01 < 0, die Summe lehnt ab
    assert c.db.unassign_invoice_payment(c.payment_id) == c.invoice_id
    assert len(allocation_ids(c.db, c.invoice_id)) == 2


@pytest.mark.parametrize('flagged, refused', [('beide', True), ('Ursprungsrechnung', True), ('Stornorechnung', True), ('keine', False)])
def test_the_check_per_rate_asks_for_the_flag_of_either_document_of_the_pair(world, flagged, refused):
    """„Die stornierte Rechnung ODER ihre Stornorechnung trägt das Kennzeichen“: Ohne es bei beiden gilt nur die Summe, die hier aufgeht."""
    c = over_refunded(world)
    if flagged in ('Stornorechnung', 'keine'):
        run_sql(world.db, 'UPDATE Invoices SET MixedTaxRates = NULL WHERE ID = ?', c.invoice_id)
    if flagged in ('Ursprungsrechnung', 'keine'):
        run_sql(world.db, 'UPDATE Invoices SET MixedTaxRates = NULL WHERE ID = ?', c.storno_id)

    with closing(sqlite3.connect(c.db.db_name)) as con:
        assert Database._refund_removal_blocker(con, [c.payment_id]) == (REFUSAL_7.format(number=c.storno_number) if refused else None)


def test_the_one_check_runs_without_an_instance_on_a_raw_connection_and_the_removal_of_the_refund_is_free(world):
    c = over_refunded(world)

    with closing(sqlite3.connect(c.db.db_name)) as con:
        blocker = Database._refund_removal_blocker
        assert blocker(con, [c.payment_id]) == REFUSAL_7.format(number=c.storno_number)
        assert blocker(con, [c.payment_id, c.refund_id]) is None                             # mit der Erstattung geht alles
        assert blocker(con, [c.refund_id]) is None and blocker(con, []) is None


def test_the_check_per_rate_reads_the_single_rows_of_a_pair_without_a_counter_booking_too(world):
    """Ohne Forderung bucht jede Zahlung je Satz „Erlöskonto an Bank“ (``purpose_first``) statt „Wartekonto an Erlöskonto“: dieselbe Lesart, derselbe Schutz."""
    invoice_id = mixed_sent(world)
    run_sql(world.db, 'DELETE FROM Bookings WHERE DocumentNumber = ? AND ParentBooking_ID IS NULL', NUMBER)               # keine Forderung, keine Gegenbuchung
    for day, amount in enumerate(['10.01', '10.00'], 1):
        pay_on(world, invoice_id, Decimal(amount), f'2026-07-{day:02d}')
    storno = cancel_mixed(world, invoice_id)
    refund_of(world, storno.storno_id, Decimal('10.01'))
    assert {row['EntryForm'] for row in query_rows(world.db, "SELECT EntryForm FROM Bookings WHERE Status = 'invoice_payment'")} == {'purpose_first'}
    before = comparable(world.db)

    with pytest.raises(PaymentRemovalRefused) as refused:
        world.db.unassign_invoice_payment(allocation_ids(world.db, invoice_id)[1])

    assert str(refused.value) == REFUSAL_7.format(number=storno.storno_number) and comparable(world.db) == before


def test_the_same_removal_at_a_pair_with_one_rate_is_allowed_as_before(world):
    """Festhalte-Test: Ein Paar ohne das Kennzeichen hat nur die Summe als Prüfung. Dieselben Beträge wie bei M (zahlen 10,01 und 10,00, stornieren, 10,01
    erstatten, 10,00 entfernen), aber an einer Rechnung mit EINEM Satz: Die Summe geht auf, das Entfernen wird zugelassen – wie vor dieser Version."""
    invoice_id = sent(world, 'T-100')
    assert columns(world.db, invoice_id)['MixedTaxRates'] is None
    for day, amount in enumerate(['10.01', '10.00'], 1):
        pay_on(world, invoice_id, Decimal(amount), f'2026-07-{day:02d}')
    storno = cancel_mixed(world, invoice_id)
    refund_of(world, storno.storno_id, Decimal('10.01'))

    world.db.unassign_invoice_payment(allocation_ids(world.db, invoice_id)[1])

    assert world.db.get_storno_credit(invoice_id) == 0 and findings(world.db) == []
    assert len(allocation_ids(world.db, invoice_id)) == 1


# ── Ablehnungen: nichts wird geschrieben ─────────────────────────────────────

def test_payments_of_one_rate_on_two_revenue_accounts_refuse_the_refund_and_name_the_rate(world):
    """Das Erlöskonto eines 7-%-Zahlungssatzes wurde von Hand auf ein anderes Erlöskonto gelegt, eine zweite Zuordnung liegt auf 4300: Für 7 % gibt es
    kein EINES Konto – der Nutzer wählt keines, die Erstattung lehnt mit dem Satz ab."""
    invoice_id = mixed_sent(world)
    first = pay_on(world, invoice_id, 100.0)
    pay_on(world, invoice_id, 72.50, '2026-07-05')                                                    # die zweite Zuordnung liegt auf 4300
    world.db.insert_chart_of_accounts(4, 4301, 'Erlöse 7 % (Sonderkonto)', 'Betriebliche Erträge', is_standard=1)
    run_sql(world.db, "UPDATE Bookings SET CounterCOA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4301) "
                      "WHERE ParentBooking_ID = ? AND Status = 'invoice_payment' AND TaxRate = 0.07 "
                      "AND COA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4345)", first)     # die erste auf einem anderen Erlöskonto
    storno = cancel_mixed(world, invoice_id)
    booking = outgoing(world, -40.0, REFUND_DAY)
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno.storno_id, booking, revenue_coa_id=world.db.get_coa_id_by_account_number(4400))

    assert assignment == PaymentAssignment(False, MIXED_REFUND_REVENUE_AMBIGUOUS.format(rate='7'), None, None)
    assert '7 %' in assignment.message and tables(world.db) == before


def test_a_revenue_account_that_is_no_revenue_account_refuses_the_refund_too(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    run_sql(world.db, "UPDATE Bookings SET CounterCOA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 1800) "
                      "WHERE Status = 'invoice_payment' AND TaxRate = 0.19 AND COA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4405) "
                      "AND DocumentNumber = ?", NUMBER)
    booking = outgoing(world, -40.0, REFUND_DAY)
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_REFUND_REVENUE_AMBIGUOUS.format(rate='19'), None, None) and tables(world.db) == before


def test_refunds_that_no_longer_fit_the_payments_per_rate_are_refused(world):
    """Eine frühere Erstattung (von Hand geändert: der ganze Betrag auf 7 %) trägt für diesen Satz mehr, als die Zahlungen hergeben – die Aufteilung ginge
    nicht auf, die App rät nicht."""
    invoice_id, storno_id, number = paid_and_cancelled(world)
    refund_of(world, storno_id, 40.0)
    run_sql(world.db, "UPDATE Bookings SET Amount = 0, TaxAmount = NULL WHERE DocumentNumber = ? AND Status = 'invoice_payment' AND TaxRate = 0.19 "
                      "AND Amount < 0 AND COA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4405)", number)
    run_sql(world.db, "UPDATE Bookings SET Amount = -400000 WHERE DocumentNumber = ? AND Status = 'invoice_payment' AND TaxRate = 0.07", number)
    booking = outgoing(world, -20.0, '2026-07-11')
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_REFUND_SPLIT, None, None) and tables(world.db) == before


def test_an_allocation_without_the_rows_of_the_app_refuses_the_refund(world):
    invoice_id = mixed_sent(world)
    older = world.db.insert_booking('2026-06-30', 50.0, booking_type='bank', account_id=world.bank)
    run_sql(world.db, "INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 500000, '2026-06-30')", invoice_id, older)
    storno = cancel_mixed(world, invoice_id)
    booking = outgoing(world, -20.0, REFUND_DAY)
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno.storno_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_ALLOCATION_UNREADABLE, None, None) and tables(world.db) == before


def test_a_refund_whose_rows_were_changed_by_hand_is_refused(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    refund_of(world, storno_id, 40.0)
    run_sql(world.db, "UPDATE Bookings SET Amount = Amount - 100 WHERE DocumentNumber = ? AND Status = 'invoice_payment' AND TaxRate = 0.07", number)
    booking = outgoing(world, -20.0, '2026-07-11')
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_ALLOCATION_UNREADABLE, None, None) and tables(world.db) == before


@pytest.mark.parametrize('which', ['Ursprungsrechnung', 'Stornorechnung'])
def test_a_refund_is_refused_when_only_one_of_the_pair_carries_the_flag(world, which):
    """Die Stornorechnung übernimmt das Kennzeichen der stornierten Rechnung: Trägt nur eine von beiden es, hat jemand an der App vorbei etwas geändert."""
    invoice_id = sent(world, 'T-100')
    pay_on(world, invoice_id, 50.0)
    storno = cancel_mixed(world, invoice_id)
    booking = outgoing(world, -20.0, REFUND_DAY)
    flag(world.db, invoice_id if which == 'Ursprungsrechnung' else storno.storno_id)
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno.storno_id, booking)

    assert assignment == PaymentAssignment(False, MIXED_GROUPS_MISMATCH, None, None) and tables(world.db) == before


def test_the_state_changed_between_the_precheck_and_the_transaction_is_caught(world, monkeypatch):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    booking = outgoing(world, -40.0, REFUND_DAY)
    real = world.db._bank_coa_of

    def prepared_then_changed(account_id, what='Zahlung'):
        found = real(account_id, what)
        run_sql(world.db, 'UPDATE Bookings SET Account_ID = ? WHERE ID = ?', world.bank2, booking)      # die Bewegung liegt dann auf einem anderen Konto
        return found

    monkeypatch.setattr(world.db, '_bank_coa_of', prepared_then_changed)
    before = tables(world.db)

    assignment = world.db.assign_storno_refund(storno_id, booking)

    assert assignment == PaymentAssignment(False, PAYMENT_CHANGED_MESSAGE, None, None)
    assert tables(world.db)['InvoicePayments'] == before['InvoicePayments'] and rows_of(world.db, number) == []


# ═════════════════════════════════════════════════════════════════════════════
# Stornieren und neu ausstellen: Die Zahlungen ziehen nicht um
# ═════════════════════════════════════════════════════════════════════════════

def test_cancel_and_reissue_of_a_mixed_invoice_keeps_the_payment_at_the_cancelled_invoice_and_names_the_reason(world, monkeypatch):
    invoice_id = mixed_sent(world)
    payment_booking = pay_on(world, invoice_id, 100.0)
    at(monkeypatch, '2026-07-31')

    code, body = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': STORNO_DAY, 'reissue': True}).encode())

    answer = json.loads(body)
    assert code == 200 and answer['success'] is True, answer
    replacement = answer['replacement_id']
    assert columns(world.db, replacement)['MixedTaxRates'] == 1 and columns(world.db, replacement)['Status'] == 'draft'
    number = columns(world.db, replacement)['InvoiceNumber']

    change = world.db.set_invoice_status(replacement, 'sent', USER)

    assert change.ok and change.moved == 0
    assert change.payment_hint == MOVE_MIXED_HINT.format(number=NUMBER, amount='100,00 €')
    assert [row[:6] for row in receivables(world.db, number)] == [(10000, 4405, 11900, 0.19, 1900, 'transfer'), (10000, 4345, 5350, 0.07, 350, 'transfer')]
    assert allocation_ids(world.db, replacement) == [] and len(allocation_ids(world.db, invoice_id)) == 1
    assert world.db.get_storno_credit(invoice_id) == Decimal('100.00')
    # Wer die Zahlung hier haben will: dort entfernen und an der Ersatzrechnung neu zuordnen – sie verteilt sich wie eine erste Zahlung
    world.db.unassign_invoice_payment(allocation_ids(world.db, invoice_id)[0])
    assert world.db.assign_invoice_payment(replacement, payment_booking).ok
    assert marked(world.db, number) == PART_PAYMENT


def test_a_replacement_without_the_flag_for_a_mixed_original_does_not_take_the_payments_either(world, monkeypatch):
    """Die Regel gilt, wenn die alte ODER die neue Rechnung mehrere Sätze trägt: Hier wird die Ersatzrechnung zu einer mit einem Satz umgestellt."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 50.0)
    cancel_mixed(world, invoice_id)
    replacement = reissue(world.db, invoice_id, monkeypatch)
    edit_draft(world, replacement, ONE_ITEM)                                                  # ein Satz, kein Kennzeichen
    assert columns(world.db, replacement)['MixedTaxRates'] is None
    number = columns(world.db, replacement)['InvoiceNumber']
    before = book_tables(world.db, number)

    change = world.db.set_invoice_status(replacement, 'sent', USER)

    assert change.ok and change.moved == 0 and change.payment_hint == MOVE_MIXED_HINT.format(number=NUMBER, amount='50,00 €')
    assert book_tables(world.db, number) == before                                            # keine Zuordnung und kein Satz anders
    assert allocations(world.db, replacement) == [] and [row[1] for row in allocations(world.db, invoice_id)] == [500000]


MIXED_HINT_WITH_CREDIT = ('Die Zahlungen der stornierten Rechnung M-100 ({amount}) bleiben dort als Guthaben des Kunden: Eine der beiden Rechnungen trägt mehrere '
                          'Steuersätze, die Zahlungen ziehen in dieser Version nicht automatisch um. Sie lassen sich dort erstatten oder entfernen und hier neu zuordnen.')


@pytest.mark.parametrize('refunded, hint', [
    (None, MIXED_HINT_WITH_CREDIT.format(amount='100,00 €')),                           # nichts erstattet: wie bisher
    (40.0, MIXED_HINT_WITH_CREDIT.format(amount='60,00 €') + ' 40,00 € sind bereits erstattet und bleiben an der stornierten Rechnung.'),
    (100.0, 'Es wurden keine Zahlungen der stornierten Rechnung M-100 übernommen. 100,00 € sind bereits erstattet und bleiben an der stornierten Rechnung.'),
])
def test_the_hint_names_what_is_left_after_a_refund(world, monkeypatch, refunded, hint):
    """Drei Lagen mit festen Beträgen: nichts erstattet (der Hinweis von immer), teilweise erstattet (dazu der Satz „… sind bereits erstattet …“) und ganz erstattet
    (dieselben Sätze wie beim Beleg mit einem Satz – ohne den Halbsatz zum Erstatten und Neu-Zuordnen, denn es gibt nichts mehr)."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    storno = cancel_mixed(world, invoice_id)
    if refunded:
        refund_of(world, storno.storno_id, refunded)
    replacement = reissue(world.db, invoice_id, monkeypatch)

    change = world.db.set_invoice_status(replacement, 'sent', USER)

    assert change.ok and change.moved == 0 and change.payment_hint == hint
    assert ('erstatten oder entfernen und hier neu zuordnen' in change.payment_hint) == (refunded != 100.0)       # ist alles erstattet, gibt es nichts mehr zu tun
    assert MOVE_MIXED_HINT.format(number=NUMBER, amount='100,00 €') == MIXED_HINT_WITH_CREDIT.format(amount='100,00 €')   # die Hilfskonstante gibt den Satz wieder


def test_the_hint_after_a_full_refund_is_built_from_the_sentences_of_a_credit_note_with_one_rate(world, monkeypatch):
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    storno = cancel_mixed(world, invoice_id)
    refund_of(world, storno.storno_id, 100.0)
    replacement = reissue(world.db, invoice_id, monkeypatch)

    change = world.db.set_invoice_status(replacement, 'sent', USER)

    assert change.payment_hint == f'{MOVE_NONE_HINT.format(number=NUMBER)} {MOVE_REFUNDED_HINT.format(amount="100,00 €")}'


def test_without_any_payment_a_mixed_pair_gets_no_hint_as_a_pair_with_one_rate_gets_none(world, monkeypatch):
    mixed = mixed_sent(world)
    cancel_mixed(world, mixed)
    single = sent(world, 'T-100')
    cancel_mixed(world, single, '2026-07-03')

    hints = [world.db.set_invoice_status(reissue(world.db, original, monkeypatch), 'sent', USER).payment_hint for original in (mixed, single)]

    assert hints == [None, None]


@pytest.mark.parametrize('refunded, hint', [
    (None, 'Zahlungen der stornierten Rechnung T-100 über 100,00 € sind jetzt dieser Rechnung zugeordnet.'),
    (40.0, 'Zahlungen der stornierten Rechnung T-100 über 60,00 € sind jetzt dieser Rechnung zugeordnet. '
           '40,00 € sind bereits erstattet und bleiben an der stornierten Rechnung.'),
    (100.0, 'Es wurden keine Zahlungen der stornierten Rechnung T-100 übernommen. 100,00 € sind bereits erstattet und bleiben an der stornierten Rechnung.'),
])
def test_the_hint_of_a_pair_with_one_rate_stays_character_for_character_the_same(world, monkeypatch, refunded, hint):
    """Festhalte-Test: Die Sätze, die jetzt auch der Hinweis des Belegs mit Steuersatz je Position benutzt (``MOVE_NONE_HINT``, ``MOVE_REFUNDED_HINT``), lauten
    beim Beleg mit einem Satz Zeichen für Zeichen wie vorher."""
    invoice_id = sent(world, 'T-100')
    pay_on(world, invoice_id, 100.0)
    storno = cancel_mixed(world, invoice_id)
    if refunded:
        refund_of(world, storno.storno_id, refunded)
    replacement = reissue(world.db, invoice_id, monkeypatch)

    change = world.db.set_invoice_status(replacement, 'sent', USER)

    assert change.ok and change.payment_hint == hint


def test_no_revenue_account_is_looked_up_for_the_placeholder_rate_of_a_mixed_replacement(world, monkeypatch):
    """Der Kopfsatz einer Rechnung mit Steuersatz je Position ist nur ein Platzhalter: ``_payment_move_context`` löst für ihn nie ein Erlöskonto auf –
    bei einer Rechnung mit einem Satz ist es das hinterlegte."""
    mixed = mixed_sent(world)
    pay_on(world, mixed, 100.0)
    cancel_mixed(world, mixed)
    mixed_replacement = reissue(world.db, mixed, monkeypatch)
    single = sent(world, 'T-100')
    cancel_mixed(world, single, '2026-07-03')
    single_replacement = reissue(world.db, single, monkeypatch)

    assert world.db._payment_move_context(mixed_replacement, 'sent')['revenue_coa'] is None
    assert world.db._payment_move_context(single_replacement, 'sent')['revenue_coa'] == world.db.get_coa_id_by_account_number(4400)


# ═════════════════════════════════════════════════════════════════════════════
# Zuordnung neu aufbauen: nur der offene Betrag
# ═════════════════════════════════════════════════════════════════════════════

def wrong_amount_due(world, amount_due=999999):
    """M mit der Zahlung 100,00 und einem offenen Betrag, der nicht zu den Zuordnungen passt (ein Eingriff an der App vorbei)."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    run_sql(world.db, 'UPDATE Invoices SET AmountDue = ? WHERE ID = ?', amount_due, invoice_id)
    return invoice_id


def test_the_rebuild_of_a_mixed_invoice_only_recalculates_the_open_amount_and_writes_no_row(world):
    invoice_id = wrong_amount_due(world)
    assert [f['category'] for f in findings(world.db, invoice_id)] == ['amount_due_mismatch']
    before = tables(world.db)

    dry = world.db.rebuild_invoice_payments(invoice_id)

    assert (dry.ok, dry.code, dry.message, dry.executed, dry.needs_revenue_coa) == (True, 200, None, False, False)
    plan = dry.plan
    assert [action.kind for action in plan.actions] == ['recalc'] and plan.rows == [] and plan.euer == []
    assert plan.skipped == [REBUILD_SKIP_REASONS['mixed']]
    assert (plan.status_before, plan.status_after, plan.due_before, plan.due_after) == ('partial_payment', 'partial_payment', 999999, 725000)
    assert tables(world.db) == before                                                         # der Probelauf hinterlässt nichts

    done = world.db.rebuild_invoice_payments(invoice_id, dry_run=False, confirmed_check=plan.check)

    assert (done.ok, done.code, done.executed) == (True, 200, True)
    assert standing(world.db, invoice_id) == ('partial_payment', 7250)
    assert tables(world.db)['Bookings'] == before['Bookings']                                 # kein Satz geschrieben, keiner geändert
    assert findings(world.db, invoice_id) == []


def test_the_dry_run_names_the_reason_and_the_execution_needs_the_confirmation(world):
    invoice_id = wrong_amount_due(world)
    plan = world.db.rebuild_invoice_payments(invoice_id).plan

    without_check = world.db.rebuild_invoice_payments(invoice_id, dry_run=False)
    wrong_check = world.db.rebuild_invoice_payments(invoice_id, dry_run=False, confirmed_check='0' * 64)

    assert (without_check.ok, without_check.code, without_check.executed) == (False, 409, False)
    assert (wrong_check.ok, wrong_check.code, wrong_check.executed) == (False, 409, False)
    assert columns(world.db, invoice_id)['AmountDue'] == 999999 and plan.skipped == [REBUILD_SKIP_REASONS['mixed']]


def test_a_mixed_invoice_without_a_finding_has_nothing_to_rebuild(world):
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)

    dry = world.db.rebuild_invoice_payments(invoice_id)
    done = world.db.rebuild_invoice_payments(invoice_id, dry_run=False, confirmed_check=dry.plan.check)

    assert dry.ok and dry.plan.actions == [] and dry.plan.skipped == [REBUILD_SKIP_REASONS['mixed']]
    assert (done.ok, done.message, done.executed) == (True, REBUILD_NOTHING_MESSAGE, False)


def test_the_rebuild_of_a_mixed_storno_pair_gives_the_reason_mixed_not_storno_pair(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    refund_of(world, storno_id, 40.0)

    for target in (invoice_id, storno_id):
        plan = world.db.rebuild_invoice_payments(target).plan
        assert plan.skipped == [REBUILD_SKIP_REASONS['mixed']] and plan.rows == [], target


def test_the_rebuild_of_a_mixed_invoice_asks_for_no_revenue_account_and_ignores_a_chosen_one(world):
    invoice_id = mixed_sent(world)
    drop_accounts(world.db, 4400, 4300)                                                       # nichts bucht darauf: Es gibt kein Erlöskonto
    run_sql(world.db, 'UPDATE Invoices SET AmountDue = 999999 WHERE ID = ?', invoice_id)

    asked = world.db.rebuild_invoice_payments(invoice_id)
    chosen = world.db.rebuild_invoice_payments(invoice_id, revenue_coa_id=999999)               # ein Konto, das es nicht gibt, wird nicht einmal gelesen

    assert asked.ok and not asked.needs_revenue_coa and asked.plan.revenue_coa is None
    assert chosen.ok and chosen.plan.check == asked.plan.check


def test_the_route_of_the_rebuild_answers_200_with_the_reason_for_a_mixed_invoice(world):
    invoice_id = wrong_amount_due(world)

    code, body = handlers.handle_invoice_rebuild_payments(world.db, json.dumps({'invoiceId': invoice_id}).encode())

    answer = json.loads(body)
    assert code == 200 and answer['success'] is True and answer['executed'] is False and answer['needsRevenueCoa'] is False
    assert answer['plan']['skipped'] == [REBUILD_SKIP_REASONS['mixed']] and answer['plan']['rows'] == [] and answer['plan']['nothingToDo'] is False
    assert [action['kind'] for action in answer['plan']['actions']] == ['recalc']

    code, body = handlers.handle_invoice_rebuild_payments(
        world.db, json.dumps({'invoiceId': invoice_id, 'execute': True, 'check': answer['plan']['check']}).encode())

    assert code == 200 and json.loads(body)['executed'] is True and standing(world.db, invoice_id) == ('partial_payment', 7250)


# ═════════════════════════════════════════════════════════════════════════════
# Prüfliste: rate_split_mismatch
# ═════════════════════════════════════════════════════════════════════════════

def test_an_intact_mixed_pair_has_no_finding_after_every_step(world):
    invoice_id = mixed_sent(world)
    assert findings(world.db) == []
    pay_on(world, invoice_id, 100.0)
    assert findings(world.db) == []
    storno = cancel_mixed(world, invoice_id)
    assert findings(world.db) == []
    refund_of(world, storno.storno_id, 40.0)
    assert findings(world.db) == []
    refund_of(world, storno.storno_id, 60.0, '2026-07-11')
    assert findings(world.db) == []


def test_a_fully_paid_and_fully_refunded_pair_has_no_finding_either(world):
    """Zuordnungen = Brutto an beiden Belegen: Die Probe der Umbuchungen je Satz gilt für die Rechnung wie für die Stornorechnung."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    pay_on(world, invoice_id, 72.50, '2026-07-05')
    storno = cancel_mixed(world, invoice_id)

    _, assignment = refund_of(world, storno.storno_id, 172.50)

    assert assignment.ok and findings(world.db) == []
    assert marked(world.db, storno.storno_number) == [(1800, 10000, -17250, None, None, 'liquid_first'), (4405, 4400, -11900, -1900, 0.19, 'transfer'),
                                                       (4345, 4300, -5350, -350, 0.07, 'transfer')]


def test_a_receivable_with_another_amount_is_one_finding_with_the_expected_and_the_booked_amounts(world):
    invoice_id = mixed_sent(world)
    run_sql(world.db, 'UPDATE Bookings SET Amount = 500000 WHERE DocumentNumber = ? AND TaxRate = 0.07 AND ParentBooking_ID IS NULL', NUMBER)

    found = findings(world.db, invoice_id)

    assert [f['category'] for f in found] == ['rate_split_mismatch']
    assert (found[0]['invoice_id'], found[0]['invoice_number']) == (invoice_id, NUMBER)
    assert found[0]['text'] == ('Die Forderungsbuchungen der App passen nicht zu den Steuersätzen der Rechnung: '
                                'erwartet 19 %: 119,00 € (Steuer 19,00 €); 7 %: 53,50 € (Steuer 3,50 €), '
                                'gebucht 19 %: 119,00 € (Steuer 19,00 €); 7 %: 50,00 € (Steuer 3,50 €).')


def test_a_missing_or_a_doubled_receivable_row_is_a_finding_too(world):
    first = mixed_sent(world)
    run_sql(world.db, 'DELETE FROM Bookings WHERE DocumentNumber = ? AND TaxRate = 0.07 AND ParentBooking_ID IS NULL', NUMBER)
    second = mixed_sent(world, 'M-200')
    run_sql(world.db, "INSERT INTO Bookings (DateBooking, Amount, COA_ID, CounterCOA_ID, TaxRate, TaxAmount, Text, DocumentNumber, BookingType, EntryForm) "
                      "SELECT DateBooking, Amount, COA_ID, CounterCOA_ID, TaxRate, TaxAmount, Text, DocumentNumber, BookingType, EntryForm FROM Bookings "
                      "WHERE DocumentNumber = 'M-200' AND TaxRate = 0.07 AND ParentBooking_ID IS NULL")

    missing, doubled = findings(world.db, first, 'rate_split_mismatch'), findings(world.db, second, 'rate_split_mismatch')

    assert [f['text'] for f in missing] == ['Die Forderungsbuchungen der App passen nicht zu den Steuersätzen der Rechnung: '
                                            'erwartet 19 %: 119,00 € (Steuer 19,00 €); 7 %: 53,50 € (Steuer 3,50 €), '
                                            'gebucht 19 %: 119,00 € (Steuer 19,00 €).']
    assert [f['text'] for f in doubled] == ['Die Forderungsbuchungen der App passen nicht zu den Steuersätzen der Rechnung: '
                                            'erwartet 19 %: 119,00 € (Steuer 19,00 €); 7 %: 53,50 € (Steuer 3,50 €), '
                                            'gebucht 19 %: 119,00 € (Steuer 19,00 €); 7 %: 53,50 € (Steuer 3,50 €); 7 %: 53,50 € (Steuer 3,50 €).']


def test_the_counter_receivable_of_the_credit_note_is_checked_against_its_own_negated_groups(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    run_sql(world.db, 'UPDATE Bookings SET TaxAmount = -30000 WHERE DocumentNumber = ? AND TaxRate = 0.07 AND ParentBooking_ID IS NULL', number)

    found = findings(world.db, storno_id, 'rate_split_mismatch')

    assert [f['text'] for f in found] == ['Die Forderungsbuchungen der App passen nicht zu den Steuersätzen der Rechnung: '
                                          'erwartet 19 %: -119,00 € (Steuer -19,00 €); 7 %: -53,50 € (Steuer -3,50 €), '
                                          'gebucht 19 %: -119,00 € (Steuer -19,00 €); 7 %: -53,50 € (Steuer -3,00 €).']
    assert findings(world.db, invoice_id, 'rate_split_mismatch') == []                         # die Rechnung selbst ist in Ordnung


def test_a_fully_paid_invoice_whose_transfers_were_shifted_between_the_rates_is_a_finding(world):
    """Eine Zuordnung geht für sich auf (Σ Umbuchungen = Betrag), aber 1,00 € liegen auf dem falschen Satz: je Satz stimmt die Summe nicht mehr."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    second = pay_on(world, invoice_id, 72.50, '2026-07-05')
    run_sql(world.db, "UPDATE Bookings SET Amount = Amount - 10000 WHERE ParentBooking_ID = ? AND EntryForm = 'transfer' AND TaxRate = 0.19", second)
    run_sql(world.db, "UPDATE Bookings SET Amount = Amount + 10000 WHERE ParentBooking_ID = ? AND EntryForm = 'transfer' AND TaxRate = 0.07", second)

    found = findings(world.db, invoice_id)

    assert [f['category'] for f in found] == ['rate_split_mismatch']
    assert found[0]['text'] == ('Die Rechnung ist ganz bezahlt, aber auf die Erlöskonten ist je Steuersatz nicht ihr Betrag umgebucht: '
                                'erwartet 19 %: 119,00 € (Steuer 19,00 €); 7 %: 53,50 € (Steuer 3,50 €), '
                                'umgebucht 19 %: 118,00 € (Steuer 19,00 €); 7 %: 54,50 € (Steuer 3,50 €).')


def test_a_fully_paid_invoice_without_any_transfer_rows_names_nothing_booked(world):
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    pay_on(world, invoice_id, 72.50, '2026-07-05')
    run_sql(world.db, "DELETE FROM Bookings WHERE DocumentNumber = ? AND EntryForm = 'transfer' AND Status = 'invoice_payment'", NUMBER)

    texts = [f['text'] for f in findings(world.db, invoice_id, 'rate_split_mismatch')]

    assert texts == ['Die Rechnung ist ganz bezahlt, aber auf die Erlöskonten ist je Steuersatz nicht ihr Betrag umgebucht: '
                     'erwartet 19 %: 119,00 € (Steuer 19,00 €); 7 %: 53,50 € (Steuer 3,50 €), umgebucht nichts.']


def test_a_part_payment_is_not_checked_for_the_transfers_per_rate(world):
    """Nur eine ganz bezahlte Rechnung muss je Satz ihr Brutto umgebucht haben; eine Teilzahlung ist noch unterwegs."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    run_sql(world.db, "UPDATE Bookings SET Amount = Amount + 100 WHERE DocumentNumber = ? AND EntryForm = 'transfer' AND Status = 'invoice_payment' "
                      "AND TaxRate = 0.07", NUMBER)

    assert findings(world.db, invoice_id, 'rate_split_mismatch') == []


def test_positions_that_do_not_match_the_sums_are_a_finding_for_a_flagged_invoice_in_any_status(world):
    invoice_id = mixed_draft(world)
    run_sql(world.db, 'UPDATE Invoices SET SumGross = SumGross + 10000 WHERE ID = ?', invoice_id)

    found = findings(world.db, invoice_id, 'rate_split_mismatch')

    assert [f['text'] for f in found] == ['Die Positionen der Rechnung passen nicht zu ihren Summen – die Steuersätze lassen sich nicht prüfen.']
    assert found[0]['invoice_number'] == NUMBER


def test_an_invoice_without_positions_is_a_finding_too(world):
    invoice_id = mixed_draft(world)
    run_sql(world.db, 'DELETE FROM InvoiceItems WHERE InvoiceId = ?', invoice_id)

    assert [f['category'] for f in findings(world.db, invoice_id, 'rate_split_mismatch')] == ['rate_split_mismatch']


def test_the_findings_of_the_rates_never_touch_an_invoice_with_one_rate_or_a_quote(world):
    single = sent(world, 'T-100')
    pay_on(world, single, 119.0)
    quote = mixed_draft(world, 'AG-1')
    run_sql(world.db, "UPDATE Invoices SET DocumentType = 'quote', MixedTaxRates = 1 WHERE ID = ?", quote)
    run_sql(world.db, 'UPDATE Invoices SET SumGross = SumGross + 10000 WHERE ID = ?', quote)           # ein Angebot ist nie ein Beleg dieser Prüfung

    assert [f for f in findings(world.db) if f['category'] in ('rate_split_mismatch', 'unflagged_rate_mix')] == []


def test_a_flag_that_is_not_the_whole_number_one_does_not_count(world):
    """Wie überall liest nur die ganze Zahl 1 das Kennzeichen (``has_mixed_rates``): jede andere Zahl heißt „ein Satz“ – und der Beleg trägt dann
    verschiedene Sätze ohne Kennzeichen."""
    invoice_id = mixed_draft(world)
    run_sql(world.db, 'UPDATE Invoices SET MixedTaxRates = 2 WHERE ID = ?', invoice_id)

    assert [f['category'] for f in findings(world.db, invoice_id)] == ['unflagged_rate_mix']


# ═════════════════════════════════════════════════════════════════════════════
# Prüfliste: rate_refund_mismatch
# ═════════════════════════════════════════════════════════════════════════════
#
# Die App lässt den Zustand nicht entstehen (``test_no_way_removes_a_payment_that_would_leave_a_rate_refunded_beyond_what_it_paid``); die Kategorie meldet, was
# daran vorbei geschieht: Hier wird er per SQL hergestellt – die Zuordnung der Zahlung 10,00 samt ihren Sätzen gelöscht, ein Eingriff an der App vorbei.

RATE_REFUND_TEXT = ('Je Steuersatz ist mehr erstattet als gezahlt (oder Betrag und Steuer gehen nicht auf): '
                    'gezahlt 19 %: 6,91 € (Steuer 1,10 €); 7 %: 3,10 € (Steuer 0,20 €) – erstattet 19 %: 6,90 € (Steuer 1,10 €); 7 %: 3,11 € (Steuer 0,20 €).')


def drop_second_payment(w, c):
    """Die Zuordnung der zweiten Zahlung samt ihren Sätzen löschen, wie die SQL-Konsole es täte (nichts rechnet neu)."""
    run_sql(w.db, "DELETE FROM Bookings WHERE ParentBooking_ID = ? AND Status = 'invoice_payment'", c.bank)
    run_sql(w.db, 'DELETE FROM InvoicePayments WHERE ID = ?', c.payment_id)


def unflag(w, c, flagged):
    """Das Kennzeichen so setzen, wie ``flagged`` es nennt: ``'beide'`` (wie es ist), ``'Ursprungsrechnung'`` (nur sie trägt es), ``'Stornorechnung'``, ``'keine'``."""
    if flagged in ('Stornorechnung', 'keine'):
        run_sql(w.db, 'UPDATE Invoices SET MixedTaxRates = NULL WHERE ID = ?', c.invoice_id)
    if flagged in ('Ursprungsrechnung', 'keine'):
        run_sql(w.db, 'UPDATE Invoices SET MixedTaxRates = NULL WHERE ID = ?', c.storno_id)


def test_a_pair_refunded_beyond_one_rate_is_one_finding_with_the_values_per_rate(world):
    c = over_refunded(world)
    drop_second_payment(world, c)

    found = findings(world.db, category='rate_refund_mismatch')

    assert [(f['category'], f['invoice_id'], f['invoice_number'], f['text']) for f in found] == [('rate_refund_mismatch', c.invoice_id, NUMBER, RATE_REFUND_TEXT)]
    assert findings(world.db, category='refund_exceeds_credit') == []          # die Summe geht auf (10,01 gezahlt, 10,01 erstattet): nur die Sätze melden es


def test_the_finding_is_given_for_either_invoice_of_the_pair_and_for_no_other(world):
    c = over_refunded(world)
    drop_second_payment(world, c)
    other = mixed_sent(world, 'M-200')

    assert [f['invoice_id'] for f in findings(world.db, c.invoice_id, 'rate_refund_mismatch')] == [c.invoice_id]
    assert [f['invoice_id'] for f in findings(world.db, c.storno_id, 'rate_refund_mismatch')] == [c.invoice_id]       # auch am Beleg der Erstattung, an der stornierten Rechnung
    assert findings(world.db, other, 'rate_refund_mismatch') == []


@pytest.mark.parametrize('flagged, named', [('beide', 1), ('Ursprungsrechnung', 1), ('Stornorechnung', 1), ('keine', 0)])
def test_the_finding_asks_for_the_flag_of_either_document_of_the_pair(world, flagged, named):
    c = over_refunded(world)
    drop_second_payment(world, c)
    unflag(world, c, flagged)

    assert len(findings(world.db, category='rate_refund_mismatch')) == named


def test_the_same_lay_at_a_pair_with_one_rate_is_only_the_old_finding(world):
    """Eine Rechnung mit einem Satz: 20,01 gezahlt und erstattet, dann die Zuordnung einer Zahlung per SQL gelöscht – mehr erstattet als gezahlt, die Summe zeigt es
    (``refund_exceeds_credit``). Je Satz gibt es dort nichts zu melden."""
    invoice_id = sent(world, 'T-100')
    banks = [pay_on(world, invoice_id, Decimal(amount), f'2026-07-0{day}') for day, amount in enumerate(['10.01', '10.00'], 1)]
    storno = cancel_mixed(world, invoice_id)
    refund_of(world, storno.storno_id, Decimal('20.01'))
    run_sql(world.db, "DELETE FROM Bookings WHERE ParentBooking_ID = ? AND Status = 'invoice_payment'", banks[1])
    run_sql(world.db, 'DELETE FROM InvoicePayments WHERE BookingID = ? AND InvoiceID = ?', banks[1], invoice_id)

    categories = [f['category'] for f in findings(world.db)]

    assert 'refund_exceeds_credit' in categories and 'rate_refund_mismatch' not in categories


def test_a_rate_refunded_in_full_but_with_another_tax_is_a_finding_too(world):
    """Der dritte Fall der Bedingung: Betrag gleich, Steuer nicht – ein Rest von Cent zwischen den Sätzen. Alles bezahlt (172,50) und ganz erstattet, dann die Steuer
    der Erstattung für 7 % per SQL um einen Cent gekürzt: Die Summe geht auf (das Guthaben ist 0), je Satz ist 7 % mit 3,49 statt 3,50 Steuer zurückgenommen."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    pay_on(world, invoice_id, 72.50, '2026-07-05')
    storno = cancel_mixed(world, invoice_id)
    refund_of(world, storno.storno_id, 172.50)
    assert findings(world.db) == []
    run_sql(world.db, "UPDATE Bookings SET TaxAmount = TaxAmount + 100 WHERE DocumentNumber = ? AND Status = 'invoice_payment' AND EntryForm = 'transfer' "
                      'AND TaxRate = 0.07', storno.storno_number)

    found = findings(world.db, category='rate_refund_mismatch')

    assert [f['text'] for f in found] == ['Je Steuersatz ist mehr erstattet als gezahlt (oder Betrag und Steuer gehen nicht auf): '
                                          'gezahlt 19 %: 119,00 € (Steuer 19,00 €); 7 %: 53,50 € (Steuer 3,50 €) – '
                                          'erstattet 19 %: 119,00 € (Steuer 19,00 €); 7 %: 53,50 € (Steuer 3,49 €).']
    assert world.db.get_storno_credit(invoice_id) == 0 and findings(world.db, category='refund_exceeds_credit') == []


def test_a_state_that_cannot_be_read_per_rate_is_no_finding_of_this_category(world):
    c = over_refunded(world)
    drop_second_payment(world, c)
    older = world.db.insert_booking('2026-06-30', Decimal('5.00'), booking_type='bank', account_id=world.bank)
    run_sql(world.db, "INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 50000, '2026-06-30')", c.invoice_id, older)

    assert findings(world.db, category='rate_refund_mismatch') == []


def test_positions_that_do_not_match_the_sums_leave_the_finding_of_the_rates_to_rate_split_mismatch(world):
    c = over_refunded(world)
    drop_second_payment(world, c)
    run_sql(world.db, 'UPDATE Invoices SET SumGross = SumGross + 10000 WHERE ID = ?', c.invoice_id)

    categories = [f['category'] for f in findings(world.db, c.invoice_id)]

    assert 'rate_split_mismatch' in categories and 'rate_refund_mismatch' not in categories


def test_the_finding_lists_a_rate_that_was_refunded_without_any_payment_carrying_it(world):
    """Zahlungen 0,01 und 10,01, 10,01 erstattet, die zweite Zahlung per SQL gelöscht: Gezahlt steht allein der Cent der ersten Zahlung da (auf 19 %), die
    Erstattung hat 19 % und 7 % zurückgebucht – 7 % hat erstattet, ohne dass eine Zahlung es trägt. (Die Summe ist hier auch negativ: ``refund_exceeds_credit``
    meldet es mit.)"""
    c = over_refunded(world, payments=('0.01', '10.01'), refunded='10.01')
    drop_second_payment(world, c)

    found = findings(world.db, category='rate_refund_mismatch')

    assert [f['text'] for f in found] == ['Je Steuersatz ist mehr erstattet als gezahlt (oder Betrag und Steuer gehen nicht auf): '
                                          'gezahlt 19 %: 0,01 € (Steuer 0,00 €) – erstattet 19 %: 6,90 € (Steuer 1,10 €); 7 %: 3,11 € (Steuer 0,20 €).']
    assert len(findings(world.db, category='refund_exceeds_credit')) == 1


def test_the_check_list_needs_no_more_statements_for_more_mixed_credit_notes(world, monkeypatch):
    """Die Befunde je Satz lesen alle Storno-Paare mit dem Kennzeichen in je einer Abfrage: Bei 2 und bei 6 Paaren (mit Zahlung und Erstattung) läuft dieselbe Zahl
    von Anweisungen – und mit Paaren mehr als ohne."""
    statements = count_statements(world, monkeypatch)
    counts = {}
    made = 0
    for total in (0, 2, 6):
        while made < total:
            made += 1
            invoice_id = mixed_sent(world, f'M-{600 + made}')
            pay_on(world, invoice_id, 100.0, f'2026-07-{made:02d}')
            refund_of(world, cancel_mixed(world, invoice_id).storno_id, 30.0, f'2026-07-{made + 10:02d}')
        statements.clear()
        assert findings(world.db) == []
        counts[total] = len(statements)

    assert counts[2] == counts[6] and counts[0] < counts[2], counts


# ═════════════════════════════════════════════════════════════════════════════
# Prüfliste: unflagged_rate_mix
# ═════════════════════════════════════════════════════════════════════════════

UNFLAGGED_TEXT = ('Die Positionen der Rechnung tragen verschiedene Steuersätze, die Rechnung wird aber mit einem Satz geführt – '
                  'ihre Buchungen rechnen mit dem Satz im Kopf.')


def unflagged_mix(world, number='U-100'):
    """Eine Rechnung, deren Positionen 19 % und 7 % tragen, aber ohne Kennzeichen – so hinterließe sie ein Bestand aus einer Version vor 2.7.0."""
    invoice_id = mixed_draft(world, number)
    run_sql(world.db, 'UPDATE Invoices SET MixedTaxRates = NULL WHERE ID = ?', invoice_id)
    return invoice_id


def test_positions_with_different_rates_without_the_flag_are_one_finding_with_a_fixed_sentence(world):
    invoice_id = unflagged_mix(world)

    found = findings(world.db, invoice_id)

    assert [(f['category'], f['text'], f['invoice_id'], f['invoice_number']) for f in found] == [
        ('unflagged_rate_mix', UNFLAGGED_TEXT, invoice_id, 'U-100')]


def test_the_flagged_invoice_and_the_invoice_with_one_rate_are_never_named(world):
    mixed_draft(world)
    make_invoice(world, 'draft', number='T-100', items=ONE_ITEM)

    assert findings(world.db, category='unflagged_rate_mix') == []


def test_only_the_rate_counts_not_its_spelling_and_a_position_without_a_rate_does_not_count(world):
    """0,19 und 19 sind derselbe Satz (``rate_pct``); eine Position ohne Satz (ein leerer Text – die Spalte nimmt kein NULL) zählt nicht."""
    items = [ONE_ITEM[0], dict(ONE_ITEM[0], position=2, description='Zweite'), dict(ONE_ITEM[0], position=3, description='Dritte')]
    invoice_id = make_invoice(world, 'draft', number='T-300', items=items)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 0.19 WHERE InvoiceId = ? AND Position = 1', invoice_id)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 19 WHERE InvoiceId = ? AND Position = 2', invoice_id)
    run_sql(world.db, "UPDATE InvoiceItems SET TaxRate = '' WHERE InvoiceId = ? AND Position = 3", invoice_id)

    assert findings(world.db, invoice_id, 'unflagged_rate_mix') == []

    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 0 WHERE InvoiceId = ? AND Position = 3', invoice_id)      # 0 % ist ein Satz

    assert [f['category'] for f in findings(world.db, invoice_id, 'unflagged_rate_mix')] == ['unflagged_rate_mix']


def test_an_invoice_without_tax_is_never_named_whatever_its_positions_carry(world):
    """Kopf unter 0 (der Platzhalter für „ohne Steuerausweis“, § 19): Die Sätze der Positionen sind dort nie Grundlage einer Buchung."""
    invoice_id = make_invoice(world, 'draft', number='K-100', items=MIXED_ITEMS, showTax=False)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 19 WHERE InvoiceId = ? AND Position = 1', invoice_id)
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = 7 WHERE InvoiceId = ? AND Position = 2', invoice_id)
    assert columns(world.db, invoice_id)['TaxRate'] < 0

    assert findings(world.db, invoice_id, 'unflagged_rate_mix') == []


def test_a_quote_with_different_rates_is_never_named(world):
    invoice_id = unflagged_mix(world, 'AG-1')
    run_sql(world.db, "UPDATE Invoices SET DocumentType = 'quote' WHERE ID = ?", invoice_id)

    assert findings(world.db, category='unflagged_rate_mix') == []


def test_a_credit_note_counts_like_an_invoice(world):
    invoice_id, storno_id, number = paid_and_cancelled(world)
    run_sql(world.db, 'UPDATE Invoices SET MixedTaxRates = NULL WHERE ID IN (?, ?)', invoice_id, storno_id)

    found = findings(world.db, category='unflagged_rate_mix')

    assert sorted((f['invoice_id'], f['invoice_number']) for f in found) == sorted([(invoice_id, NUMBER), (storno_id, number)])


def test_the_filter_by_invoice_limits_both_kinds_of_finding(world):
    first = unflagged_mix(world, 'U-100')
    second = mixed_draft(world, 'M-100')
    run_sql(world.db, 'UPDATE Invoices SET SumGross = SumGross + 10000 WHERE ID = ?', second)

    assert [f['category'] for f in findings(world.db, first, 'unflagged_rate_mix')] == ['unflagged_rate_mix']
    assert findings(world.db, second, 'unflagged_rate_mix') == [] and len(findings(world.db, second, 'rate_split_mismatch')) == 1
    assert [f['invoice_id'] for f in findings(world.db) if f['category'] in ('unflagged_rate_mix', 'rate_split_mismatch')] == [second, first]


def test_the_check_list_needs_no_more_statements_for_more_mixed_invoices(world, monkeypatch):
    """Die Befunde der Sätze lesen alle Belege mit dem Kennzeichen in je einer Abfrage: Bei 2 und bei 6 gekennzeichneten Rechnungen (mit Zahlungen)
    läuft dieselbe Zahl von Anweisungen."""
    statements = count_statements(world, monkeypatch)
    counts = {}
    made = 0
    for total in (2, 6):
        while made < total:
            made += 1
            invoice_id = mixed_sent(world, f'M-{500 + made}')
            pay_on(world, invoice_id, 100.0, f'2026-07-{made:02d}')
        statements.clear()
        findings(world.db)
        counts[total] = len(statements)

    assert counts[2] == counts[6], counts


# ═════════════════════════════════════════════════════════════════════════════
# Eigenschaft: Zufallsfolgen über die Wege der App (fester Seed)
# ═════════════════════════════════════════════════════════════════════════════
#
# Eine Rechnung mit zwei bis drei Positionen (19 und 7 %), ein bis drei Teilzahlungen (auch ein Cent), storniert – dann zufällige Schritte: teilweise und ganz erstatten,
# Zahlungen und Erstattungen entfernen (``unassign_invoice_payment``) und die Bankbewegung einer Zahlung löschen (``delete_transaction``), in beliebiger Reihenfolge.
# Eine Ablehnung (``PaymentRemovalRefused``) ist erlaubt und wird übergangen – aber nur eine, die stimmt: Eine unabhängige Rechnung (Summe, je Satz) sagt für jedes
# Entfernen, ob es gehen müsste. Nach JEDEM Schritt: Prüfliste leer, je Satz erstattet ≤ gezahlt (Betrag und Steuer), und solange ein Guthaben besteht, ließe es sich in
# einem Zug ganz erstatten (Probelauf ``_refund_plan``) – nie ``MIXED_REFUND_SPLIT``. Am Ende wird das Guthaben wirklich erstattet: je Satz gezahlt = erstattet.

SEQUENCE_GROUPS, SEQUENCES_PER_GROUP = 20, 10


def violating_rates(paid, refunded):
    """Die Sätze, für die mehr erstattet ist als gezahlt (Betrag oder Steuer) oder Betrag und Steuer nicht aufgehen – ``paid`` und ``refunded`` wie ``per_rate``
    (``refunded`` steht negativ). Unabhängig vom Code unter Test geschrieben: eine Menge, keine Reihenfolge."""
    bad = set()
    for rate, (amount, tax) in refunded.items():
        paid_amount, paid_tax = paid.get(rate, (0, 0))
        if -amount > paid_amount or -tax > paid_tax or (-amount == paid_amount and -tax != paid_tax):
            bad.add(rate)
    return bad


def rows_per_rate(db, number, bank):
    """``per_rate`` für die Sätze einer Bankbewegung."""
    return {round(row['TaxRate'] * 100): (row['amount'] // 100, row['tax'] // 100) for row in query_rows(
        db, "SELECT TaxRate, SUM(Amount) AS amount, SUM(COALESCE(TaxAmount, 0)) AS tax FROM Bookings WHERE DocumentNumber = ? AND ParentBooking_ID = ? "
            "AND Status = 'invoice_payment' AND EntryForm = 'transfer' GROUP BY TaxRate", number, bank)}


def run_sequence(db, rng, number, stats):
    """Eine Folge wie oben beschrieben auf der Datenbank ``db`` (Rechnung ``number``); ``stats`` zählt, was geschah."""
    items = [(19, f'{rng.randint(1, 600)}.{rng.randint(0, 99):02d}'), (7, f'{rng.randint(1, 600)}.{rng.randint(0, 99):02d}')]
    if rng.random() < 0.4:
        items.append((rng.choice([19, 7]), f'{rng.randint(1, 300)}.{rng.randint(0, 99):02d}'))
    invoice_id = invoice_of(db, number, items, '2025-03-03')
    left = to_minor(Decimal(str(db.get_invoice_by_id(invoice_id)[38])))
    for step in range(rng.randint(1, 3)):
        if left <= 0:
            break
        amount = 100 if rng.random() < 0.2 else (left if step == 2 or rng.random() < 0.3 else rng.randint(1, max(left // 100, 1)) * 100)
        amount = min(amount, left)
        pay_fast(db, invoice_id, from_minor(amount), f'2025-04-{step + 1:02d}')
        left -= amount
    storno = cancel_on(db, invoice_id, '2025-06-01')
    storno_number = storno.storno_number
    probe = db.insert_booking('2025-06-02', -Decimal('1000000.00'), account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')

    def allocations_of(invoice):
        with closing(sqlite3.connect(db.db_name)) as con:
            return con.execute('SELECT ID, BookingID, Amount FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID', (invoice,)).fetchall()

    def credit_cents():
        return to_minor(db.get_storno_credit(invoice_id)) // 100

    def check(where):
        assert db.find_payment_inconsistencies() == [], where                               # die ganze Prüfliste, nicht nur diese Rechnung
        paid, refunded = per_rate(db, number), per_rate(db, storno_number)
        assert violating_rates(paid, refunded) == set(), (where, paid, refunded)             # erstattet ≤ gezahlt je Satz, Betrag und Steuer
        if credit_cents() > 0:
            conn = db._get_connection()
            try:
                refusal, plan = db._refund_plan(conn, storno.storno_id, probe, None, db.get_bank_coa_ids())
            finally:
                conn.close()
            assert refusal is None and plan.amount == to_minor(db.get_storno_credit(invoice_id)), (where, refusal)     # ganz erstattbar, nie ``MIXED_REFUND_SPLIT``
            stats['plans'] += 1

    def refund(cents, day):
        bank = db.insert_booking(day, -Decimal(cents) / 100, account_id=account_id(db, 'Probe-Giro'), booking_type='bank', recipient_client='Käufer AG')
        result = db.assign_storno_refund(storno.storno_id, bank)
        assert result.ok, result.message

    def remove(kind, allocation):
        pay_id, bank, amount = allocation
        paid, refunded = per_rate(db, number), per_rate(db, storno_number)
        gone = rows_per_rate(db, number, bank)
        paid_after = {rate: (value[0] - gone.get(rate, (0, 0))[0], value[1] - gone.get(rate, (0, 0))[1]) for rate, value in paid.items()}
        total_after = sum(row[2] for row in allocations_of(invoice_id) + allocations_of(storno.storno_id)) - amount
        before = tables(db)
        try:
            if kind == 'payment':
                db.unassign_invoice_payment(pay_id)
            else:
                db.delete_transaction(bank)                                                # die Bankbewegung der Zahlung mit allem, was daran hängt
            refused = None
        except PaymentRemovalRefused as exc:
            refused = str(exc)
        bad = violating_rates(paid_after, refunded)
        if total_after < 0:
            assert refused == ALREADY_REFUNDED_MESSAGE, (kind, refused, total_after)
            stats['refused_sum'] += 1
        elif bad:
            assert refused in {MIXED_REMOVAL_REFUSED.format(rate=rate, number=storno_number) for rate in bad}, (kind, refused, paid_after, refunded)
            stats['refused_rate'] += 1
        else:
            assert refused is None, (kind, refused, paid_after, refunded)
            stats['removed'] += 1
        if refused:
            assert tables(db) == before                                                     # abgelehnt: nichts geändert

    check('nach dem Stornieren')
    for step in range(rng.randint(3, 9)):
        payments, refunds, credit = allocations_of(invoice_id), allocations_of(storno.storno_id), credit_cents()
        what = rng.choice(['refund_part', 'refund_part', 'refund_tight', 'refund_tight', 'refund_all', 'remove_payment', 'remove_payment', 'delete_bank',
                           'remove_refund', 'remove_refund'])
        day = f'2025-07-{step + 1:02d}'
        if what == 'refund_part' and credit > 0:
            refund(1 if rng.random() < 0.2 else rng.randint(1, credit), day)
            stats['refunds'] += 1
        elif what == 'refund_tight' and credit > 0 and payments:
            # knapp: so viel erstatten, dass nach dem Entfernen einer Zahlung nur 0 bis 2 Cent Guthaben blieben (die Summe geht auf) – und gleich versuchen, genau
            # diese Zahlung zu entfernen: Dort entscheidet erst die Prüfung je Satz
            chosen = payments[rng.randrange(len(payments))]
            target = credit - chosen[2] // 100 - rng.randint(0, 2)
            if 1 <= target <= credit:
                refund(target, day)
                stats['refunds'] += 1
                stats['tight'] += 1
                remove('payment', chosen)
        elif what == 'refund_all' and credit > 0:
            refund(credit, day)
            stats['refunds'] += 1
        elif what in ('remove_payment', 'delete_bank') and payments:
            remove('payment' if what == 'remove_payment' else 'bank', rng.choice(payments))
        elif what == 'remove_refund' and refunds:
            db.unassign_invoice_payment(rng.choice(refunds)[0])                                # das Entfernen einer Erstattung ist immer frei
            stats['refunds_removed'] += 1
        check((number, step, what))

    credit = credit_cents()
    if credit > 0:                                                                          # am Ende wirklich ganz erstatten
        result = db.assign_storno_refund(storno.storno_id, probe)
        assert result.ok and db.get_storno_credit(invoice_id) == 0, result.message
        assert db.find_payment_inconsistencies(invoice_id) == [] and db.find_payment_inconsistencies(storno.storno_id) == []
        paid, refunded = per_rate(db, number), per_rate(db, storno_number)
        assert {rate: (-value[0], -value[1]) for rate, value in refunded.items()} == {rate: value for rate, value in paid.items() if value[0]}, (number, paid, refunded)
        stats['closed'] += 1


def test_random_sequences_never_leave_a_rate_refunded_beyond_what_it_paid_or_a_credit_that_cannot_be_refunded(tmp_path):
    stats = Counter()
    for group in range(SEQUENCE_GROUPS):
        db = with_banks(Database(db_name=str(tmp_path / f'sequences{group}.db')))
        rng = random.Random(7000 + group)
        for index in range(SEQUENCES_PER_GROUP):
            run_sequence(db, rng, f'Z{group}-{index}', stats)
            stats['sequences'] += 1

    assert stats['sequences'] == SEQUENCE_GROUPS * SEQUENCES_PER_GROUP == 200
    # die Eigenschaft bleibt nicht leer (gemessen mit diesen Seeds: 7 / 119 / 162 / 259 / 709 / 66): abgelehnte Entfernungen je Satz und nach der Summe, erlaubte,
    # Erstattungen, Probeläufe der ganzen Erstattung und am Ende wirklich ganz erstattete Folgen
    assert (stats['refused_rate'] >= 3 and stats['refused_sum'] >= 30 and stats['removed'] >= 80 and stats['refunds'] >= 130 and stats['plans'] >= 350
            and stats['closed'] >= 30), dict(stats)


# ═════════════════════════════════════════════════════════════════════════════
# Anzeigen: nie eine Wahl des Erlöskontos
# ═════════════════════════════════════════════════════════════════════════════

def test_the_credit_note_page_offers_no_choice_of_the_revenue_account_even_when_the_payments_differ(world):
    """Bei einem Beleg mit Steuersatz je Position gibt es nie eine Auswahl, auch wenn die Zahlungen eines Satzes auf verschiedenen Erlöskonten liegen:
    Die Ablehnung sagt, für welchen Satz es nicht geht."""
    invoice_id = mixed_sent(world)
    pay_on(world, invoice_id, 100.0)
    second = pay_on(world, invoice_id, 72.50, '2026-07-05')
    run_sql(world.db, "UPDATE Bookings SET CounterCOA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4300) "
                      "WHERE ParentBooking_ID = ? AND EntryForm = 'transfer' AND TaxRate = 0.19", second)       # 19 % auf zwei Erlöskonten
    storno = cancel_mixed(world, invoice_id)
    booking = outgoing(world, -40.0, REFUND_DAY)

    assert world.db.refund_revenue_coas_differ(storno.storno_id) is False
    html = PageInvoice(world.db, {}, storno.storno_id)
    box = html[html.index('id="refund_box"'):html.index('<script>', html.index('id="refund_box"'))]
    assert 'name="revenue_coa_id"' not in box and 'revenueCoa' not in box and 'Als Erstattung zuordnen' in box
    assert world.db.assign_storno_refund(storno.storno_id, booking) == PaymentAssignment(
        False, MIXED_REFUND_REVENUE_AMBIGUOUS.format(rate='19'), None, None)


def test_the_same_lay_of_the_payments_with_one_rate_still_offers_the_choice(world):
    """Die Gegenprobe: Bei einem Beleg mit einem Satz zeigt die Seite die Auswahl, wenn die Zahlungen auf verschiedenen Erlöskonten liegen."""
    _original, storno_id, _out = mixed_payments(world)

    assert world.db.refund_revenue_coas_differ(storno_id) is True
    assert 'name="revenue_coa_id"' in PageInvoice(world.db, {}, storno_id)


def test_the_choice_of_the_revenue_account_is_never_offered_for_a_mixed_invoice(world):
    assert revenue_coa_choice(world.db, 0.19, mixed=True) is None and revenue_coa_choice(world.db, 0.16, teaches=False, mixed=True) is None
    assert revenue_coa_choice(world.db, 0.16) is not None                                         # ein Satz ohne hinterlegtes Konto: die Wahl des Nutzers


def test_the_rebuild_box_of_a_mixed_invoice_has_the_button_but_no_revenue_account_field(world):
    invoice_id = mixed_sent(world)
    drop_accounts(world.db, 4400, 4300)
    run_sql(world.db, 'UPDATE Invoices SET AmountDue = 999999 WHERE ID = ?', invoice_id)                # ein Befund: Die Seite zeigt den Neuaufbau
    single = sent(world, 'T-100')
    run_sql(world.db, 'UPDATE Invoices SET AmountDue = 999999 WHERE ID = ?', single)

    mixed_page, single_page = PageInvoice(world.db, {}, invoice_id), PageInvoice(world.db, {}, single)

    assert 'rebuildPayments(%d, false)' % invoice_id in mixed_page and 'id="rebuildRevenueCoa"' not in mixed_page
    assert 'rebuildPayments(%d, false)' % single in single_page and 'id="rebuildRevenueCoa"' in single_page      # die Wahl bleibt bei einem Satz
