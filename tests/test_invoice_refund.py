# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Erstattung nach einem Storno (2.2.3): ``Database.assign_storno_refund``, Entfernen, Schutz des Guthabens.

Nach dem Storno einer (teil-)bezahlten Rechnung bleiben ihre Zahlungen an der stornierten Rechnung: Das ist das **Guthaben
des Kunden** (Σ Zuordnungen beider Belege des Paares, ``get_storno_credit``). Die **Erstattung** gleicht es aus – eine
ausgehende Bankbewegung, zugeordnet an der **Stornorechnung** mit negativem Betrag und mit eigenen Buchungssätzen, die den
Erlös am Tag der Erstattung zurücknehmen. Hier steht, was dazugehört:

* die Sätze Spalte für Spalte, die Steuer in allen Varianten (voll, in Teilen, in mehreren Zahlungen, ohne Forderung,
  § 19, nach dem Umzug eines Teils), Salden, EÜR, DATEV-Export und Dashboard;
* jede Ablehnung (nichts geschrieben), die Kappung auf Guthaben, freien Rest und gewünschten Betrag, eine Sammel-
  Überweisung, Bewegungen mit anderen Sätzen darunter; der freie Rest eines Ausgangs rechnet die Sätze nach ihrer Wirkung auf das
  Konto der Bewegung (``bank_effect``, auch ein Import-Spiegel) und in wenigen Abfragen; Zahlungen auf verschiedenen Erlöskonten;
* das Entfernen (der Stand ist Zeile für Zeile wieder da) und der Schutz des Guthabens auf jedem Weg: Rechnungsseite, Bank-
  Editor, Löschen eines Satzes oder der Bankbewegung, Liste (der Umzug nach einer Erstattung: ``test_invoice_reissue.py``);
* die Prüfliste (eine intakte Erstattung ergibt keinen Befund, ``refund_exceeds_credit``), die Seiten, die Liste, die offenen
  Posten des Kontakts, die Routen und der Bank-Editor.

Das Beispiel: Rechnung R1 über 119,00 € (19 %), 50,00 € am 01.07. gezahlt, am 02.07. storniert (Stornorechnung S1 = 26R001).

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import csv
import json
import re
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

import server.handlers as handlers
from db import Database
from db.invoice_payments import (ALREADY_REFUNDED_MESSAGE, MIXED_REVENUE_MESSAGE, NO_REVENUE_COA, NOT_A_REVENUE_COA,
                                 PAYMENT_MARK, REFUND_EXHAUSTED_MESSAGE, REFUND_NO_CREDIT_MESSAGE,
                                 REFUND_ONLY_OUTGOING_MESSAGE, REFUND_ONLY_STORNO_MESSAGE, STORNO_PAYMENT_MESSAGE,
                                 PaymentRemovalRefused)
from server.handlers import handle_add_transaction, handle_datev_export
from server.pages_contacts import PageContacts
from server.pages_invoice import PageInvoice, REFUND_CONFIRM_QUESTION, revenue_coa_choice
from server.pages_transactions import PageTransactions
from tests.document_helpers import post_json
from tests.dom_helpers import Dom, app_msg_arguments
from tests.entry_form_roundtrip import add_row, differences, drop_row, open_form, snapshot as bookings_snapshot, submit
from tests.http_client import FOREIGN_ORIGIN, get, post
from tests.invoice_world import add_payment, columns, make_invoice, query_rows, run_sql, set_invoice_status
from tests.test_invoice_reissue import (CUSTOMER_TEXT, _list_cells, _plain, allocations, edit_draft, findings, ledger,
                                        paid_scenario, payment_rows, standing)
from tests.test_invoice_storno import (DEBTORS, NO_NODE, NODE, ONE_ITEM, WAITING, _node, balance,  # noqa: F401
                                       bookings_of, books, cancel, euer, pay, sent, sent_over_http, tables, world)

RANGE = ('2026-01-01', '2027-12-31')
FORM_LIQUID, FORM_TRANSFER, FORM_PURPOSE = 'liquid_first', 'transfer', 'purpose_first'


# ── Hilfen ───────────────────────────────────────────────────────────────────

def outgoing(w, amount, date='2026-07-10', recipient=CUSTOMER_TEXT, **extra):
    """Eine ausgehende Bankbewegung (Zahlungsausgang) über ``amount`` € auf das Giro-Konto."""
    return w.db.insert_booking(date, -amount, booking_type='bank', account_id=extra.pop('account_id', w.bank),
                               recipient_client=recipient, **extra)


def refund(w, storno_id, booking, amount=None, **extra):
    """Eine Erstattung zuordnen; sie muss gelingen."""
    result = w.db.assign_storno_refund(storno_id, booking, amount, **extra)
    assert result.ok is True and result.message is None, result
    return result


def refused(w, storno_id, booking, message, amount=None, **extra):
    """Die Erstattung wird abgelehnt: der Klartext stimmt, und in keiner Tabelle hat sich etwas geändert (auch kein Konto,
    kein Zähler)."""
    before = tables(w.db)
    result = w.db.assign_storno_refund(storno_id, booking, amount, **extra)
    assert (result.ok, result.message) == (False, message), result
    assert (result.payment_id, result.revenue_coa_id) == (None, None)
    assert tables(w.db) == before
    return result


def paid_and_cancelled(w, paid=50.0, number='T-100', day='2026-07-02', **over):
    """R1 über 119,00 € versendet, ``paid`` am 01.07. gezahlt, am ``day`` storniert; liefert (R1, S1, Bankbewegung der Zahlung)."""
    r1 = sent(w, number, **over)
    booking = pay(w, r1, paid)
    return r1, cancel(w, r1, day).storno_id, booking


def coa(db, number):
    return db.get_coa_id_by_account_number(number)


def euer_on(db, day):
    """EÜR eines Tages: Kontonummer → Betrag."""
    return {number: round(total, 2) for number, _name, total in db.get_euer_data(day, day)}


def comparable(db):
    """Alles, was Entfernen wiederherstellen muss: Bookings, InvoicePayments und Invoices Zeile für Zeile, jede Spalte
    (``UpdatedAt`` ausgenommen – die Neuberechnung stempelt sie)."""
    result = {'Bookings': query_rows(db, 'SELECT * FROM Bookings ORDER BY ID'),
              'InvoicePayments': query_rows(db, 'SELECT * FROM InvoicePayments ORDER BY ID'),
              'Invoices': [{k: v for k, v in row.items() if k != 'UpdatedAt'}
                           for row in query_rows(db, 'SELECT * FROM Invoices ORDER BY ID')]}
    return result


def refund_rows(db, number='26R001'):
    """Die markierten Sätze der Erstattung (und jeder Zuordnung) zu dieser Beleg-Nr. – wie ``payment_rows``."""
    return payment_rows(db, number)


def unguarded(monkeypatch):
    """Die Prüfung vor dem Entfernen einer Zahlung (``_refund_removal_blocker``) abschalten: So entstand „mehr erstattet als
    gezahlt“ bis zu dieser Version (Löschen der Bankbewegung einer Zahlung nach der Erstattung), und so entsteht es weiter
    durch einen Eingriff an der App vorbei (SQL-Konsole, zurückgespielte Sicherung). Die Prüfliste meldet den Zustand."""
    monkeypatch.setattr(Database, '_refund_removal_blocker', staticmethod(lambda conn, payment_ids: None))


# ── Das durchgerechnete Beispiel ─────────────────────────────────────────────

def test_the_example_before_the_refund(world):
    db = world.db
    r1, s1, booking = paid_and_cancelled(world)
    assert ledger(db) == {DEBTORS: -500000, WAITING: 500000, 4345: 0, 4400: -500000, 4300: 0}
    assert euer_on(db, '2026-07-01') == {4400: 42.02, 3806: 7.98}
    assert db.get_storno_credit(r1) == db.get_storno_credit(s1) == 50
    assert db.get_storno_refunded(r1) == db.get_storno_refunded(s1) == 0
    assert db.find_payment_inconsistencies() == []


def test_a_full_refund_evens_out_debtors_waiting_account_and_revenue(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0, '2026-07-10')

    result = refund(world, s1, bank)

    assert result.revenue_coa_id == coa(db, 4400) and result.payment_id == db.get_invoice_payments(s1)[0][0]
    assert allocations(db, s1) == [(bank, -500000, '2026-07-10')]
    assert refund_rows(db, '26R001') == [(1800, DEBTORS, -500000, None, '2026-07-10', FORM_LIQUID),
                                         (WAITING, 4400, -500000, -79800, '2026-07-10', FORM_TRANSFER)]
    # Debitoren 0, Wartekonto 0, Erlös 0; die Bank steht wieder bei 0
    assert ledger(db) == {DEBTORS: 0, WAITING: 0, 4345: 0, 4400: 0, 4300: 0}
    assert balance(db, 1800) == 0
    # EÜR: am Tag der Erstattung negativer Erlös und negative Umsatzsteuer, gesamt 0
    assert euer_on(db, '2026-07-10') == {4400: -42.02, 3806: -7.98}
    assert euer_on(db, '2026-07-01') == {4400: 42.02, 3806: 7.98}
    assert euer(db) == {}
    assert db.get_storno_credit(r1) == db.get_storno_credit(s1) == 0
    assert db.get_storno_refunded(r1) == 50
    assert db.find_payment_inconsistencies() == []
    assert _plain(_list_cells(world)[0]['26R001'][5]) == 'Stornorechnung'
    # Forderung der Rechnung und Gegenbuchung der Stornorechnung bleiben stehen, die Stornorechnung bleibt „versendet“
    assert [r[3] for r in bookings_of(db, 'T-100')] == [1190000] and [r[3] for r in bookings_of(db, '26R001')] == [-1190000]
    assert standing(db, r1, s1) == [('cancelled', 690000), ('sent', -690000)]


# ── Teil-Erstattungen: die Steuer nach der Regel ─────────────────────────────

def test_two_partial_refunds_take_the_tax_by_the_rule_and_add_up_to_the_tax_of_the_payment(world):
    """20,00 € von 50,00 € Guthaben: Steuer anteilig 7,98 × 20/50 = 3,19; die Erstattung, die das Guthaben ausgleicht, nimmt den
    Rest 4,79 – zusammen genau die 7,98 der Zahlung."""
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    first, second = outgoing(world, 20.0, '2026-07-10'), outgoing(world, 30.0, '2026-07-12')

    refund(world, s1, first, 20.0)

    assert refund_rows(db) == [(1800, DEBTORS, -200000, None, '2026-07-10', FORM_LIQUID),
                               (WAITING, 4400, -200000, -31900, '2026-07-10', FORM_TRANSFER)]
    assert ledger(db) == {DEBTORS: -300000, WAITING: 300000, 4345: 0, 4400: -300000, 4300: 0}
    assert euer_on(db, '2026-07-10') == {4400: -16.81, 3806: -3.19}
    assert db.get_storno_credit(r1) == db.get_storno_credit(s1) == 30 and db.get_storno_refunded(s1) == 20
    assert 'Erstattung offen: 30,00 €' in _plain(_list_cells(world)[0]['26R001'][5])
    assert standing(db, r1, s1) == [('cancelled', 690000), ('sent', -990000)]
    assert db.find_payment_inconsistencies() == []

    refund(world, s1, second)                                       # ohne Betrag: so viel wie möglich = der Rest des Guthabens

    assert refund_rows(db)[2:] == [(1800, DEBTORS, -300000, None, '2026-07-12', FORM_LIQUID),
                                   (WAITING, 4400, -300000, -47900, '2026-07-12', FORM_TRANSFER)]
    assert sum(row[3] for row in refund_rows(db) if row[3]) == -79800          # Σ über alle Erstattungen = Steuer der Zahlung
    assert ledger(db) == {DEBTORS: 0, WAITING: 0, 4345: 0, 4400: 0, 4300: 0}
    assert euer(db) == {} and euer_on(db, '2026-07-12') == {4400: -25.21, 3806: -4.79}
    assert db.get_storno_credit(r1) == 0 and db.get_storno_refunded(s1) == 50
    assert standing(db, r1, s1) == [('cancelled', 690000), ('sent', -690000)]
    assert _plain(_list_cells(world)[0]['26R001'][5]) == 'Stornorechnung'
    assert db.find_payment_inconsistencies() == []


def test_a_rounding_drift_in_the_partial_refunds_never_takes_more_tax_than_the_payment_booked(world):
    """Die Steuer einer Teil-Erstattung wird auf den Cent gerundet (halb auf). Bei 0,50 € Steuer auf 100,00 € ergäbe jede
    Teil-Erstattung über 1,00 € genau 0,005 € → 0,01 €: hundert Teile nähmen 0,99 € zurück statt der 0,50 €, die die Zahlung
    gebucht hat. Die Summe bleibt bei der Steuer der Zahlung (nie mehr als der Rest)."""
    db = world.db
    r1 = make_invoice(world, 'draft', number='T-110')
    run_sql(db, 'UPDATE Invoices SET SumNet = 995000, TaxAmount = 5000, SumGross = 1000000, AmountDue = 1000000 '
                'WHERE ID = ?', r1)                                              # 100,00 € mit 0,50 € Steuer im Beleg
    assert set_invoice_status(r1, 'sent')[0] == 200
    pay(world, r1, 100.0)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    number = columns(db, s1)['InvoiceNumber']
    assert [row[3] for row in payment_rows(db, 'T-110') if row[3]] == [5000]

    for n in range(99):
        refund(world, s1, outgoing(world, 1.0, f'2026-08-{n % 28 + 1:02d}'), 1.0)
        assert -sum(row[3] for row in refund_rows(db, number) if row[3]) <= 5000
    assert -sum(row[3] for row in refund_rows(db, number) if row[3]) == 5000        # nach 50 Teilen ist die Steuer aufgebraucht
    refund(world, s1, outgoing(world, 1.0, '2026-09-01'))                           # die ausgleichende Erstattung
    assert -sum(row[3] for row in refund_rows(db, number) if row[3]) == 5000
    assert min(row[3] for row in refund_rows(db, number) if row[3]) == -100          # nie mehr als ein Cent je Teil
    assert db.get_storno_credit(s1) == 0 and euer(db) == {}


def test_two_payments_then_two_refunds_give_back_exactly_the_tax_both_payments_booked(world):
    """Zahlungen über 50,00 € (Steuer 7,98) und 30,00 € (4,79): zusammen 12,77. Die Teil-Erstattung über 40,00 € von 80,00 € nimmt
    12,77 × 40/80 = 6,385 → 6,39 zurück, die zweite, die ausgleicht, den Rest 6,38."""
    db = world.db
    r1 = sent(world)
    pay(world, r1, 50.0)
    pay(world, r1, 30.0)
    assert [row[3] for row in payment_rows(db, 'T-100') if row[3]] == [79800, 47900]
    s1 = cancel(world, r1, '2026-07-02').storno_id
    assert db.get_storno_credit(s1) == 80

    refund(world, s1, outgoing(world, 40.0, '2026-07-10'))
    refund(world, s1, outgoing(world, 40.0, '2026-07-12'))

    assert [row[3] for row in refund_rows(db) if row[3]] == [-63900, -63800]
    assert -sum(row[3] for row in refund_rows(db) if row[3]) == 127700
    assert ledger(db) == {DEBTORS: 0, WAITING: 0, 4345: 0, 4400: 0, 4300: 0}
    assert euer(db) == {} and euer_on(db, '2026-07-10') == {4400: -33.61, 3806: -6.39}
    assert euer_on(db, '2026-07-12') == {4400: -33.62, 3806: -6.38}
    assert db.find_payment_inconsistencies() == []


# ── Ohne Forderung: der Einzelsatz „Erlöskonto an Bank“ ──────────────────────

SIXTEEN = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Buch', 'unitPrice': 100.0, 'taxRate': 16}]


def sixteen_percent(w, number='T-201'):
    """Eine versendete Rechnung zu 16 % (116,00 €): Dafür gibt es kein Wartekonto, also keine Forderung der App."""
    return sent(w, number, items=SIXTEEN, taxRate=0.16)


def test_without_a_receivable_the_refund_is_the_single_row_revenue_account_to_bank(world):
    """16 %: keine Forderung, keine Gegenbuchung. Das Erlöskonto, das der Nutzer bei der Zahlung gewählt hat, trägt auch die
    Erstattung – eine andere Wahl bei der Erstattung zählt nicht."""
    db = world.db
    r1 = sixteen_percent(world)
    booking = db.insert_booking('2026-07-01', 116.0, booking_type='bank', account_id=world.bank,
                                recipient_client=CUSTOMER_TEXT)
    assert handlers.link_booking_to_invoice_capped(db, r1, booking, 116.0, revenue_coa_id=coa(db, 4300)) == (True, None)
    storno = cancel(world, r1, '2026-07-02')
    assert storno.counter_booked is False and bookings_of(db, '26R001') == []
    assert euer_on(db, '2026-07-01') == {4300: 100.0, 3806: 16.0}
    bank = outgoing(world, 116.0, '2026-07-10')

    result = refund(world, storno.storno_id, bank, revenue_coa_id=coa(db, 4400))

    assert result.revenue_coa_id == coa(db, 4300)
    assert refund_rows(db) == [(4300, 1800, -1160000, -160000, '2026-07-10', FORM_PURPOSE)]
    assert query_rows(db, 'SELECT TaxRate FROM Bookings WHERE ParentBooking_ID = ?', bank) == [{'TaxRate': 0.16}]
    assert euer_on(db, '2026-07-10') == {4300: -100.0, 3806: -16.0} and euer(db) == {}
    assert balance(db, 1800) == 0 and balance(db, 4300) == 0
    assert db.get_storno_credit(r1) == 0 and db.find_payment_inconsistencies() == []
    assert db.find_unbalanced_splits(*RANGE) == []


def test_without_a_receivable_a_partial_refund_takes_its_share_of_the_tax(world):
    db = world.db
    r1 = sixteen_percent(world)
    booking = db.insert_booking('2026-07-01', 116.0, booking_type='bank', account_id=world.bank,
                                recipient_client=CUSTOMER_TEXT)
    assert handlers.link_booking_to_invoice_capped(db, r1, booking, 116.0, revenue_coa_id=coa(db, 4300)) == (True, None)
    s1 = cancel(world, r1, '2026-07-02').storno_id

    refund(world, s1, outgoing(world, 58.0, '2026-07-10'), 58.0)
    refund(world, s1, outgoing(world, 58.0, '2026-07-12'))

    assert refund_rows(db) == [(4300, 1800, -580000, -80000, '2026-07-10', FORM_PURPOSE),
                               (4300, 1800, -580000, -80000, '2026-07-12', FORM_PURPOSE)]
    assert euer(db) == {} and db.find_payment_inconsistencies() == []


def test_an_imported_receivable_has_no_counter_booking_so_the_refund_is_the_single_row(world):
    """Eine importierte Forderung bekommt bei der Stornierung keine Gegenbuchung der App (``_is_app_receivable``). Die Erstattung
    folgt der Gegenbuchung, nicht der Zahlung: Es gibt den Einzelsatz „Erlöskonto an Bank“, die EÜR stimmt, und die Debitoren
    bleiben, wie sie vor der Erstattung waren (die Forderung ist nicht die der App)."""
    db = world.db
    r1 = sent(world)
    run_sql(db, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-1' WHERE DocumentNumber = 'T-100'")
    pay(world, r1, 50.0)
    storno = cancel(world, r1, '2026-07-02')
    assert storno.counter_booked is False and bookings_of(db, '26R001') == []
    debtors_before = balance(db, DEBTORS)
    bank = outgoing(world, 50.0, '2026-07-10')

    refund(world, storno.storno_id, bank)

    assert refund_rows(db) == [(4400, 1800, -500000, -79800, '2026-07-10', FORM_PURPOSE)]
    assert euer(db) == {} and euer_on(db, '2026-07-10') == {4400: -42.02, 3806: -7.98}
    assert balance(db, DEBTORS) == debtors_before and db.get_storno_credit(r1) == 0
    assert db.find_payment_inconsistencies() == []


def test_a_payment_without_rows_gives_no_revenue_account_so_the_user_chooses_one(world):
    """Ein Import (Zuordnung ohne Sätze) und kein Erlöskonto für 16 % in den Büchern: Die Erstattung braucht die Wahl des Nutzers –
    ein Erlöskonto, sonst Ablehnung. Die Wahl lehrt nichts: Sätze einer Erstattung (negativ) bestimmen kein künftiges Erlöskonto."""
    db = world.db
    r1 = make_invoice(world, 'sent', number='T-201', items=SIXTEEN, taxRate=0.16)       # ein Import: ohne Forderung
    add_payment(world, r1, 116.0)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    bank = outgoing(world, 116.0, '2026-07-10')
    assert db.get_storno_credit(s1) == 116 and db.revenue_coa_choice_needed(0.16) is True

    refused(world, s1, bank, NO_REVENUE_COA)
    refused(world, s1, bank, NOT_A_REVENUE_COA, revenue_coa_id=coa(db, WAITING))          # ein Wartekonto ist kein Erlöskonto
    refused(world, s1, bank, NOT_A_REVENUE_COA, revenue_coa_id=coa(db, 1800))
    refused(world, s1, bank, NOT_A_REVENUE_COA, revenue_coa_id='abc')
    refused(world, s1, bank, NO_REVENUE_COA, revenue_coa_id='')

    result = refund(world, s1, bank, revenue_coa_id=coa(db, 4300))

    assert result.revenue_coa_id == coa(db, 4300)
    assert refund_rows(db) == [(4300, 1800, -1160000, -160000, '2026-07-10', FORM_PURPOSE)]    # die Steuer 16,00 anteilig aus der Rechnung
    assert euer_on(db, '2026-07-10') == {4300: -100.0, 3806: -16.0}
    assert db._learned_revenue_coa(db._revenue_rate_key(0.16)) is None                      # die Erstattung lehrt nichts
    assert db.revenue_coa_choice_needed(0.16) is True and db.resolve_revenue_coa(0.16, create=False) is None


def test_a_refund_row_never_teaches_a_revenue_account_but_a_payment_row_still_does(world):
    """``_learned_revenue_coa`` liest nur Sätze mit positivem Betrag: Für den Bestand ändert das nichts (markierte Sätze sind
    positiv); ein negativer markierter Satz – eine Erstattung – lehrt nichts, auch wenn er jünger ist."""
    db = world.db
    key = db._revenue_rate_key(0.16)
    r1 = sixteen_percent(world)
    booking = db.insert_booking('2026-07-01', 116.0, booking_type='bank', account_id=world.bank)
    assert handlers.link_booking_to_invoice_capped(db, r1, booking, 116.0, revenue_coa_id=coa(db, 4300)) == (True, None)
    assert db._learned_revenue_coa(key) == coa(db, 4300)                                    # die Zahlung lehrt
    second = sixteen_percent(world, 'T-202')
    other = db.insert_booking('2026-07-02', 116.0, booking_type='bank', account_id=world.bank)
    assert db.assign_invoice_payment(second, other).ok
    assert db._learned_revenue_coa(key) == coa(db, 4300)
    # ein jüngerer, negativer markierter Satz auf einem anderen Erlöskonto (4400)
    out = outgoing(world, 5.0, '2026-07-03')
    run_sql(db, "INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, DocumentNumber, "
                "BookingType, ParentBooking_ID, Status, EntryForm) VALUES ('2026-07-03', ?, ?, -50000, 0.16, -6900, 'T-201', "
                "'entry', ?, 'invoice_payment', 'purpose_first')", coa(db, 4400), coa(db, 1800), out)
    assert db._learned_revenue_coa(key) == coa(db, 4300)
    run_sql(db, 'UPDATE Bookings SET Amount = 50000, TaxAmount = 6900 WHERE ParentBooking_ID = ?', out)      # derselbe Satz, positiv
    assert db._learned_revenue_coa(key) == coa(db, 4400)                                    # … lehrt sofort wieder


# ── Zahlungen auf verschiedenen Erlöskonten ──────────────────────────────────
# Tragen die markierten Sätze der Zahlungen mehr als ein Erlöskonto (ein Umbuchungssatz wurde im Editor geändert), buchte die
# Erstattung früher alles auf das Konto der jüngsten Zahlung zurück. Jetzt wählt der Nutzer das Erlöskonto der Erstattung.

def mixed_payments(w, second_account=4300):
    """R1: 50,00 € und 30,00 € gezahlt, der Umbuchungssatz der zweiten Zahlung von Hand auf ``second_account`` umkontiert
    (statt 4400), storniert, dazu ein Ausgang über 80,00 € – liefert (R1, S1, Ausgang)."""
    db = w.db
    r1 = sent(w)
    pay(w, r1, 50.0)
    second = pay(w, r1, 30.0)
    run_sql(db, "UPDATE Bookings SET CounterCOA_ID = ? WHERE ParentBooking_ID = ? AND EntryForm = 'transfer'",
            coa(db, second_account), second)
    s1 = cancel(w, r1, '2026-07-02').storno_id
    return r1, s1, outgoing(w, 80.0, '2026-07-10')


def test_payments_on_two_revenue_accounts_need_the_choice_of_the_user_and_nothing_is_written_without_it(world):
    db = world.db
    _r1, s1, out = mixed_payments(world)
    assert MIXED_REVENUE_MESSAGE == ('Die Zahlungen dieser Rechnung liegen auf verschiedenen Erlöskonten – bitte das Erlöskonto '
                                     'für die Erstattung wählen.')

    refused(world, s1, out, MIXED_REVENUE_MESSAGE)                                       # tables() vergleicht alle Tabellen
    refused(world, s1, out, MIXED_REVENUE_MESSAGE, revenue_coa_id='')
    refused(world, s1, out, MIXED_REVENUE_MESSAGE, revenue_coa_id=None)
    refused(world, s1, out, NOT_A_REVENUE_COA, revenue_coa_id=coa(db, WAITING))          # ein Wartekonto ist kein Erlöskonto
    refused(world, s1, out, NOT_A_REVENUE_COA, revenue_coa_id=coa(db, 1800))
    refused(world, s1, out, NOT_A_REVENUE_COA, revenue_coa_id='abc')
    assert db.refund_revenue_coas_differ(s1) is True


@pytest.mark.parametrize('chosen', [4300, 4400, 4185])
def test_the_chosen_revenue_account_carries_the_refund(world, chosen):
    db = world.db
    r1, s1, out = mixed_payments(world)

    result = refund(world, s1, out, revenue_coa_id=coa(db, chosen))

    assert result.revenue_coa_id == coa(db, chosen)
    assert refund_rows(db) == [(1800, DEBTORS, -800000, None, '2026-07-10', FORM_LIQUID),
                               (WAITING, chosen, -800000, -127700, '2026-07-10', FORM_TRANSFER)]      # Steuer der Zahlungen: 12,77
    assert db.get_storno_credit(r1) == 0 and db.find_payment_inconsistencies() == []
    assert ledger(db)[DEBTORS] == 0 and ledger(db)[WAITING] == 0
    assert euer_on(db, '2026-07-10').get(3806) == -12.77                                  # die Umsatzsteuer geht ganz zurück


def test_the_choice_is_asked_for_every_refund_while_the_payments_stay_on_two_accounts(world):
    db = world.db
    _r1, s1, _out = mixed_payments(world)
    first, second = outgoing(world, 30.0, '2026-07-11'), outgoing(world, 50.0, '2026-07-12')
    refund(world, s1, first, 30.0, revenue_coa_id=coa(db, 4400))
    refused(world, s1, second, MIXED_REVENUE_MESSAGE)
    refund(world, s1, second, revenue_coa_id=coa(db, 4300))
    assert [row[1] for row in refund_rows(db) if row[0] == WAITING] == [4400, 4300]


def test_a_non_revenue_account_among_the_payments_also_needs_the_choice(world):
    db = world.db
    _r1, s1, out = mixed_payments(world, second_account=6815)
    refused(world, s1, out, MIXED_REVENUE_MESSAGE)
    assert refund(world, s1, out, revenue_coa_id=coa(db, 4400)).revenue_coa_id == coa(db, 4400)


def test_payments_on_one_revenue_account_need_no_choice_and_a_given_one_is_ignored(world):
    """Alle Zahlungen auf demselben Erlöskonto (auch einem, das nicht das übliche ist): wie bisher dieses – eine Wahl des Nutzers
    zählt dann nicht."""
    db = world.db
    r1, s1, out = mixed_payments(world, second_account=4400)
    assert db.refund_revenue_coas_differ(s1) is False and db.refund_revenue_coas_differ(r1) is False
    assert refund(world, s1, out, revenue_coa_id=coa(db, 4300)).revenue_coa_id == coa(db, 4400)


def test_without_marked_rows_the_stored_account_decides_as_before_and_a_credit_note_only_asks_when_the_payments_differ(world):
    db = world.db
    r1 = sent(world)
    add_payment(world, r1, 50.0)                                                         # Import: keine markierten Sätze
    s1 = cancel(world, r1, '2026-07-02').storno_id
    assert db.refund_revenue_coas_differ(s1) is False
    assert refund(world, s1, outgoing(world, 50.0)).revenue_coa_id == coa(db, 4400)      # das hinterlegte Konto
    assert db.refund_revenue_coas_differ(99999) is False and db.refund_revenue_coas_differ(sent(world, number='T-200')) is False


def test_the_credit_note_page_offers_the_revenue_account_when_the_payments_differ(world):
    db = world.db
    r1, s1, _out = mixed_payments(world)
    html = PageInvoice(db, {}, s1)
    box = box_of(html)
    select = Dom(box).one('select', id='revenueCoa')
    assert 'required' in select and select['name'] == 'revenue_coa_id'
    assert f'<p class="hint">{MIXED_REVENUE_MESSAGE}</p>' in box                          # der Satz steht oben als Hinweis
    assert 'Die Wahl gilt künftig' not in box and 'kein Erlöskonto hinterlegt' not in box
    options = [(o.get('value'), 'selected' in o) for o in Dom(box).all('option')]
    assert ('', False) in options and str(coa(db, 4400)) in [value for value, _s in options]
    # die Seite der stornierten Rechnung hat den Kasten nicht; ohne verschiedene Konten fehlt die Auswahl
    assert box_of(PageInvoice(db, {}, r1)) is None
    other_world_r, other_s, _paid = paid_and_cancelled(world, number='T-300')
    assert 'id="revenueCoa"' not in box_of(PageInvoice(db, {}, other_s))


def test_the_hint_of_the_box_does_not_promise_a_default_but_the_payment_form_still_does(world):
    """Die Wahl einer Erstattung lehrt kein Erlöskonto (``_learned_revenue_coa`` liest nur positive Sätze) – der Kasten sagt
    es nicht. Bei der Zahlung bleibt der Satz."""
    db = world.db
    hint = 'Die Wahl gilt künftig als Vorgabe für diesen Steuersatz.'
    imported = make_invoice(world, 'sent', number='T-201', items=SIXTEEN, taxRate=0.16)
    add_payment(world, imported, 116.0)
    s1 = cancel(world, imported, '2026-07-02').storno_id
    box = box_of(PageInvoice(db, {}, s1))
    assert 'Für 16 % ist kein Erlöskonto hinterlegt.' in box and hint not in box
    open_16 = make_invoice(world, 'sent', number='T-202', items=SIXTEEN, taxRate=0.16)
    page = PageInvoice(db, {}, open_16)
    assert 'Für 16 % ist kein Erlöskonto hinterlegt. ' + hint in page
    assert revenue_coa_choice(db, 0.16)[1].endswith(hint) and not revenue_coa_choice(db, 0.16, teaches=False)[1].endswith(hint)


# ── § 19 und der Umzug eines Teils ───────────────────────────────────────────

def test_a_tax_free_invoice_refunds_without_tax(world):
    db = world.db
    items = [dict(ONE_ITEM[0], taxRate=0)]
    r1 = sent(world, 'T-300', items=items, taxRate=0, showTax=False)                   # § 19: 100,00 €, kein Steuersatz
    pay(world, r1, 100.0)
    assert euer_on(db, '2026-07-01') == {4185: 100.0}
    s1 = cancel(world, r1, '2026-07-02').storno_id
    bank = outgoing(world, 100.0, '2026-07-10')

    refund(world, s1, bank, 100.0)

    assert refund_rows(db, columns(db, s1)['InvoiceNumber']) == [(1800, DEBTORS, -1000000, None, '2026-07-10', FORM_LIQUID),
                                                                (4340, 4185, -1000000, None, '2026-07-10', FORM_TRANSFER)]
    assert query_rows(db, 'SELECT TaxRate, TaxAmount FROM Bookings WHERE ParentBooking_ID = ?', bank) == [
        {'TaxRate': None, 'TaxAmount': None}] * 2
    assert euer(db) == {} and euer_on(db, '2026-07-10') == {4185: -100.0}
    assert balance(db, DEBTORS) == 0 and balance(db, 4340) == 0
    assert db.find_payment_inconsistencies() == []


def test_after_a_part_moved_to_a_smaller_replacement_the_rest_is_refunded_and_everything_is_even(world, monkeypatch):
    """R1 (119,00 €, 50,00 € gezahlt) wird storniert, die Ersatzrechnung über 40,00 € nimmt 40,00 € der Zahlung; 10,00 € bleiben als
    Guthaben. Die Erstattung über 10,00 € nimmt die 1,60 € Steuer zurück, die der Rest trägt – danach ist alles ausgeglichen."""
    s = paid_scenario(world, monkeypatch)
    db = world.db
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], unitPrice=33.61)])
    assert set_invoice_status(s.r2, 'sent')[0] == 200
    assert db.get_storno_credit(s.s1) == 10
    bank = outgoing(world, 10.0, '2026-11-12')

    refund(world, s.s1, bank)

    assert refund_rows(db) == [(1800, DEBTORS, -100000, None, '2026-11-12', FORM_LIQUID),
                               (WAITING, 4400, -100000, -16000, '2026-11-12', FORM_TRANSFER)]
    assert ledger(db) == {DEBTORS: 0, WAITING: 0, 4345: 0, 4400: -400000, 4300: 0}          # nur die 40,00 € der neuen Rechnung
    assert euer(db) == {4400: 33.61, 3806: 6.39}
    assert db.get_storno_credit(s.s1) == 0 and db.find_payment_inconsistencies() == []
    assert standing(db, s.r1, s.s1, s.r2) == [('cancelled', 1090000), ('sent', -1090000), ('paid', 0)]


# ── Die Sätze, Spalte für Spalte ─────────────────────────────────────────────

def test_the_rows_of_a_refund_have_every_column_as_the_rows_of_a_payment_with_the_opposite_sign(world):
    db = world.db
    r1, s1, paid_booking = paid_and_cancelled(world)
    customer = columns(db, r1)['CustomerId']
    bank = outgoing(world, 50.0, '2026-07-10', date_tax='2026-07-11', recipient='Malermeister Pinsel & Söhne')

    refund(world, s1, bank)

    columns_of = ('DateBooking', 'DateTax', 'Account_ID', 'ForeignBankAccount', 'RecipientClient', 'Contact_ID', 'COA_ID',
                  'CounterCOA_ID', 'Category_ID', 'Amount', 'Currency', 'TaxRate', 'TaxAmount', 'Text', 'DocumentNumber',
                  'BookingType', 'ParentBooking_ID', 'Status', 'AutoMirror', 'SourceGroup', 'EntryForm')
    sql = f"SELECT {', '.join(columns_of)} FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID"
    given = [dict(row) for row in query_rows(db, sql, bank)]
    paid = [dict(row) for row in query_rows(db, sql, paid_booking)]
    gl = {n: coa(db, n) for n in (1800, DEBTORS, WAITING, 4400)}
    assert given == [
        dict(DateBooking='2026-07-10', DateTax='2026-07-11', Account_ID=None, ForeignBankAccount=None,
             RecipientClient='Malermeister Pinsel & Söhne', Contact_ID=customer, COA_ID=gl[1800], CounterCOA_ID=gl[DEBTORS],
             Category_ID=None, Amount=-500000, Currency='EUR', TaxRate=None, TaxAmount=None,
             Text='Erstattung zu Re. 26R001', DocumentNumber='26R001', BookingType='entry', ParentBooking_ID=bank,
             Status=PAYMENT_MARK, AutoMirror=0, SourceGroup=None, EntryForm=FORM_LIQUID),
        dict(DateBooking='2026-07-10', DateTax='2026-07-11', Account_ID=None, ForeignBankAccount=None,
             RecipientClient='Malermeister Pinsel & Söhne', Contact_ID=customer, COA_ID=gl[WAITING], CounterCOA_ID=gl[4400],
             Category_ID=None, Amount=-500000, Currency='EUR', TaxRate=0.19, TaxAmount=-79800,
             Text='Umb. Erstattung zu Re. 26R001', DocumentNumber='26R001', BookingType='entry', ParentBooking_ID=bank,
             Status=PAYMENT_MARK, AutoMirror=0, SourceGroup=None, EntryForm=FORM_TRANSFER)]
    # dieselben Konten, Formen und Steuersatz wie die Zahlung – nur Betrag, Steuer, Datum, Beleg-Nr. und Bewegung sind andere
    for refund_row, payment_row in zip(given, paid):
        same = ('COA_ID', 'CounterCOA_ID', 'EntryForm', 'TaxRate', 'Currency', 'Contact_ID', 'Status', 'BookingType', 'AutoMirror')
        assert {k: refund_row[k] for k in same} == {k: payment_row[k] for k in same}
        assert refund_row['Amount'] == -payment_row['Amount']
        assert refund_row['TaxAmount'] == (None if payment_row['TaxAmount'] is None else -payment_row['TaxAmount'])
    assert query_rows(db, 'SELECT * FROM InvoicePayments WHERE InvoiceID = ?', s1) == [
        {'ID': db.get_invoice_payments(s1)[0][0], 'InvoiceID': s1, 'BookingID': bank, 'Amount': -500000,
         'PaymentDate': '2026-07-10', 'Notes': None, 'CreatedAt': query_rows(db, 'SELECT CreatedAt FROM InvoicePayments '
                                                                                  'WHERE InvoiceID = ?', s1)[0]['CreatedAt']}]
    # die Bankbewegung selbst bleibt, wie sie war (kein Backfill, kein Kontakt, keine Beleg-Nr.)
    assert query_rows(db, 'SELECT COA_ID, CounterCOA_ID, TaxRate, TaxAmount, Contact_ID, DocumentNumber, Amount FROM Bookings '
                          'WHERE ID = ?', bank) == [dict(COA_ID=None, CounterCOA_ID=None, TaxRate=None, TaxAmount=None,
                                                         Contact_ID=None, DocumentNumber=None, Amount=-500000)]


def test_the_replay_log_carries_the_assignment_and_its_rows(world):
    import os
    import userctx
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0, '2026-07-10')
    refund(world, s1, bank)
    with open(os.path.join(userctx.user_data_dir(), 'sql_operations.sql'), encoding='utf-8') as handle:
        lines = [line for line in handle.read().splitlines() if f'-500000' in line]
    assert any('INSERT INTO InvoicePayments' in line and f'{s1}, {bank}, -500000' in line for line in lines)
    assert sum('INSERT INTO Bookings' in line and "'invoice_payment'" in line for line in lines) == 2
    assert db.get_invoice_payments(s1)


# ── Ablehnungen: Klartext, nichts geschrieben ────────────────────────────────

def test_only_a_credit_note_can_be_refunded(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0)
    quote = db.insert_invoice({'invoice_number': '26A001', 'invoice_date': '2026-06-09', 'seller_name': 'S',
                               'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
                               'sum_net': 100, 'tax_amount': 19, 'sum_gross': 119, 'amount_due': 119, 'status': 'sent',
                               'document_type': 'quote'})
    refused(world, 99999, bank, 'Rechnung nicht gefunden')
    refused(world, quote, bank, 'Rechnung nicht gefunden')
    refused(world, r1, bank, REFUND_ONLY_STORNO_MESSAGE)                                   # die stornierte Rechnung
    other = sent(world, number='T-200')
    refused(world, other, bank, REFUND_ONLY_STORNO_MESSAGE)                                # eine gewöhnliche Rechnung
    run_sql(db, 'UPDATE Invoices SET CancelsInvoiceId = NULL WHERE ID = ?', s1)           # ein Eingriff per SQL
    refused(world, s1, bank, REFUND_ONLY_STORNO_MESSAGE)
    run_sql(db, 'UPDATE Invoices SET CancelsInvoiceId = 9999 WHERE ID = ?', s1)
    refused(world, s1, bank, REFUND_ONLY_STORNO_MESSAGE)


def test_only_an_outgoing_bank_movement_can_be_assigned(world):
    db = world.db
    _r1, s1, paid_booking = paid_and_cancelled(world)
    refused(world, s1, 99999, 'Buchung nicht gefunden')
    refused(world, s1, paid_booking, REFUND_ONLY_OUTGOING_MESSAGE)                         # ein Eingang
    entry = db.insert_booking('2026-07-10', -50.0, coa_id=coa(db, 4400), booking_type='entry')
    refused(world, s1, entry, REFUND_ONLY_OUTGOING_MESSAGE)                                # keine Bankbewegung
    zero = db.insert_booking('2026-07-10', 0, booking_type='bank', account_id=world.bank)
    refused(world, s1, zero, REFUND_ONLY_OUTGOING_MESSAGE)


def test_a_movement_is_assigned_to_a_credit_note_only_once(world):
    _r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0)
    refund(world, s1, bank, 20.0)
    refused(world, s1, bank, 'Buchung ist bereits mit dieser Rechnung verknüpft')


def test_without_credit_there_is_nothing_to_refund(world, monkeypatch):
    db = world.db
    bank = outgoing(world, 50.0)
    unpaid = cancel(world, sent(world, number='T-200'), '2026-07-02').storno_id
    refused(world, unpaid, bank, REFUND_NO_CREDIT_MESSAGE)                                   # nie gezahlt
    _r1, s1, _paid = paid_and_cancelled(world)
    refund(world, s1, bank)
    refused(world, s1, outgoing(world, 5.0, '2026-07-12'), REFUND_NO_CREDIT_MESSAGE)        # schon ganz erstattet
    s = paid_scenario(world, monkeypatch, number='T-300')
    assert set_invoice_status(s.r2, 'sent')[0] == 200                                        # alles zog auf die Ersatzrechnung um
    assert db.get_storno_credit(s.s1) == 0
    refused(world, s.s1, outgoing(world, 5.0, '2026-07-13'), REFUND_NO_CREDIT_MESSAGE)


def test_a_refused_refund_creates_no_account(world):
    """Das Auflösen des Erlöskontos kann Konten anlegen (4185 bei § 19) – eine Erstattung, die abgelehnt wird, legt keines an."""
    db = world.db
    items = [dict(ONE_ITEM[0], taxRate=0)]
    s1 = cancel(world, sent(world, 'T-300', items=items, taxRate=0, showTax=False), '2026-07-02').storno_id
    run_sql(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4185')
    bank = outgoing(world, 50.0)
    refused(world, s1, bank, REFUND_NO_CREDIT_MESSAGE)                                       # tables() enthält ChartOfAccounts
    assert db.get_coa_id_by_account_number(4185) is None


def test_a_movement_that_is_used_up_has_no_free_rest(world):
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0)
    db.insert_booking('2026-07-10', -50.0, coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800), booking_type='entry',
                      parent_booking_id=bank, entry_form=FORM_PURPOSE)                       # bereits als Ausgabe kontiert
    refused(world, s1, bank, REFUND_EXHAUSTED_MESSAGE)
    assert bank not in [c[0] for c in db.get_unallocated_outgoing_bookings()]


@pytest.mark.parametrize('amount', [0, -5, 0.004, '0,00', 0.0049], ids=['null', 'negativ', 'rundet-auf-null', 'Text', 'knapp'])
def test_an_amount_that_is_not_positive_after_rounding_is_refused(world, amount):
    _r1, s1, _paid = paid_and_cancelled(world)
    refused(world, s1, outgoing(world, 50.0), 'Der Betrag muss größer 0 sein.', amount=amount)


def test_a_bank_account_without_a_skr_number_cannot_be_booked(world):
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    db.insert_account('Ohne Nummer', 'Eigene Firma GmbH', 'DE02120300000000202051', 'TESTDE2X', 'Testbank AG', is_cash=0,
                      skr_account=None)
    no_skr = {a[1]: a[0] for a in db.fetch_accounts()}['Ohne Nummer']
    refused(world, s1, outgoing(world, 50.0, account_id=no_skr),
            'Das Bankkonto hat keine SKR-Nummer – bitte in den Stammdaten ergänzen, sonst lässt sich die Erstattung nicht '
            'kontieren.')
    refused(world, s1, db.insert_booking('2026-07-10', -50.0, booking_type='bank'), 'Für die Erstattung fehlt das Bankkonto.')


def test_the_old_messages_of_the_payment_stay_as_they_were(world):
    """Das Bankkonto-Wort der Meldung ist nur bei der Erstattung ein anderes: Die Zahlung nennt weiter „Zahlung“."""
    db = world.db
    db.insert_account('Ohne Nummer', 'Eigene Firma GmbH', 'DE02120300000000202051', 'TESTDE2X', 'Testbank AG', is_cash=0,
                      skr_account=None)
    no_skr = {a[1]: a[0] for a in db.fetch_accounts()}['Ohne Nummer']
    invoice = sent(world)
    assert db.payment_precheck(invoice, no_skr) == ('Das Bankkonto hat keine SKR-Nummer – bitte in den Stammdaten ergänzen, '
                                                    'sonst lässt sich die Zahlung nicht kontieren.')
    assert db.payment_precheck(invoice, None) == 'Für die Zahlung fehlt das Bankkonto.'


# ── Kappung: Guthaben, freier Rest, gewünschter Betrag ───────────────────────

def test_the_refund_is_capped_by_the_credit(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 100.0)
    assert [(c[0], c[4], c[5]) for c in db.get_unallocated_outgoing_bookings()] == [(bank, -100, 100)]

    refund(world, s1, bank)

    assert allocations(db, s1) == [(bank, -500000, '2026-07-10')] and db.get_storno_credit(r1) == 0
    assert [(c[0], c[5]) for c in db.get_unallocated_outgoing_bookings()] == [(bank, 50)]       # 50,00 € der Bewegung bleiben frei


def test_the_refund_is_capped_by_the_free_rest_of_the_movement(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    refund(world, s1, outgoing(world, 30.0))
    assert allocations(db, s1)[0][1] == -300000 and db.get_storno_credit(r1) == 20
    assert db.get_unallocated_outgoing_bookings() == []


def test_the_refund_is_capped_by_the_wanted_amount_which_is_rounded_to_cents_first(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0)
    refund(world, s1, bank, 19.995)                                                           # halb auf: 20,00 €
    assert allocations(db, s1) == [(bank, -200000, '2026-07-10')] and db.get_storno_credit(r1) == 30


def test_a_wanted_amount_above_credit_and_movement_is_capped_to_the_smaller(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 40.0)
    refund(world, s1, bank, 1000)
    assert allocations(db, s1)[0][1] == -400000 and db.get_storno_credit(r1) == 10


def test_the_candidate_list_filters_by_contact_and_limit_and_the_refund_sum_is_only_for_the_pair(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    mine = outgoing(world, 10.0, '2026-07-10', contact_id=world.cust)
    others = [outgoing(world, 10.0, f'2026-08-{n:02d}', contact_id=world.cust2) for n in range(1, 4)]
    assert [c[0] for c in db.get_unallocated_outgoing_bookings(world.cust)] == [mine, *reversed(others)]
    assert [c[0] for c in db.get_unallocated_outgoing_bookings(world.cust, contact_only=True)] == [mine]
    assert [c[0] for c in db.get_unallocated_outgoing_bookings(world.cust, limit=2)] == [mine, others[-1]]
    assert [c[0] for c in db.get_unallocated_outgoing_bookings(None, limit=1)] == [others[-1]]    # ohne Kontakt: nach Datum
    assert db.get_storno_refunded(r1) == db.get_storno_refunded(s1) == 0
    assert db.get_storno_refunded(sent(world, number='T-200')) is None and db.get_storno_refunded(99999) is None


# ── Sammel-Überweisung und andere Sätze unter der Bewegung ───────────────────

def test_one_outgoing_movement_refunds_two_credit_notes(world):
    """Eine Überweisung über 100,00 € erstattet zwei Stornorechnungen (je 50,00 €): je Stornorechnung eine Zuordnung mit eigenen
    Sätzen und der eigenen Nummer; danach ist die Bewegung ausgeschöpft."""
    db = world.db
    r1, s1, _p1 = paid_and_cancelled(world)
    r2 = sent(world, number='T-101')
    pay(world, r2, 50.0)
    s2 = cancel(world, r2, '2026-07-03').storno_id
    bank = outgoing(world, 100.0)

    refund(world, s1, bank)
    assert [(c[0], c[5]) for c in db.get_unallocated_outgoing_bookings()] == [(bank, 50)]
    refund(world, s2, bank)

    assert db.get_unallocated_outgoing_bookings() == []
    numbers = {columns(db, s1)['InvoiceNumber'], columns(db, s2)['InvoiceNumber']}
    assert numbers == {'26R001', '26R002'}
    for number in numbers:
        assert [row[2:4] for row in refund_rows(db, number)] == [(-500000, None), (-500000, -79800)]
    assert sorted(a[3] for a in db.get_booking_allocations(bank)) == [-50, -50]
    assert db.get_storno_credit(r1) == db.get_storno_credit(r2) == 0
    assert euer(db) == {} and ledger(db)[DEBTORS] == 0 and db.find_payment_inconsistencies() == []
    refused(world, s1, bank, 'Buchung ist bereits mit dieser Rechnung verknüpft')
    assert db.find_unbalanced_splits(*RANGE) == []                                          # die Bewegung geht auf


def test_a_collective_movement_that_is_too_small_serves_the_second_credit_note_only_in_part(world):
    db = world.db
    _r1, s1, _p1 = paid_and_cancelled(world)
    r2 = sent(world, number='T-101')
    pay(world, r2, 50.0)
    s2 = cancel(world, r2, '2026-07-03').storno_id
    bank = outgoing(world, 70.0)
    refund(world, s1, bank)
    refund(world, s2, bank)
    assert db.get_storno_credit(s2) == 30 and db.get_unallocated_outgoing_bookings() == []


def test_other_rows_under_the_movement_reduce_its_free_rest(world):
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 100.0)
    db.insert_booking('2026-07-10', -30.0, coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800), booking_type='entry',
                      parent_booking_id=bank, entry_form=FORM_PURPOSE)                      # 30,00 € schon als Ausgabe kontiert
    assert [(c[0], c[5]) for c in db.get_unallocated_outgoing_bookings()] == [(bank, 70)]

    refund(world, s1, bank)                                                                  # das Guthaben (50) passt hinein
    assert [(c[0], c[5]) for c in db.get_unallocated_outgoing_bookings()] == [(bank, 20)]

    r2 = sent(world, number='T-101')
    pay(world, r2, 50.0)
    s2 = cancel(world, r2, '2026-07-03').storno_id
    refund(world, s2, bank)                                                                  # nur noch 20,00 € frei
    assert db.get_storno_credit(s2) == 30 and db.get_unallocated_outgoing_bookings() == []


def test_a_row_without_both_accounts_counts_against_the_free_rest_too(world):
    """Wie beim Eingang: Ein nicht ganz kontierter Satz unter der Bewegung ist bankwirksam."""
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 100.0)
    db.insert_booking('2026-07-10', -60.0, coa_id=coa(db, 6815), booking_type='entry', parent_booking_id=bank)
    refund(world, s1, bank)
    assert db.get_storno_credit(s1) == 10                                                     # 40,00 € frei → 40,00 € erstattet


def test_the_formula_of_the_free_rest_of_a_payment_is_unchanged():
    """``_free_minor_sql`` (die Formel des Zahlungseingangs) ist Zeichen für Zeichen die, die sie vor 2.2.3 war – Kandidatenlisten,
    Zuordnung und Kontakt-Guthaben rechnen weiter damit."""
    expr, params = Database._free_minor_sql([3, 5])
    assert expr == ('b.Amount - MAX(COALESCE((SELECT SUM(ip.Amount) FROM InvoicePayments ip WHERE ip.BookingID = b.ID), 0), '
                    'COALESCE((SELECT SUM(k.Amount) FROM Bookings k WHERE k.ParentBooking_ID = b.ID AND (k.COA_ID IS NULL '
                    'OR k.CounterCOA_ID IS NULL OR k.COA_ID IN (?,?) OR k.CounterCOA_ID IN (?,?))), 0))')
    assert params == [3, 5, 3, 5]
    assert Database._free_minor_sql([7], 'x')[0].startswith('x.Amount - MAX(COALESCE((SELECT SUM(ip.Amount) FROM '
                                                             'InvoicePayments ip WHERE ip.BookingID = x.ID), 0), ')


def test_the_condition_bank_effective_of_the_payment_formula_is_unchanged():
    """Die Bedingung „bankwirksam“ des Zahlungseingangs (``_bank_effective_sql``) ist Zeichen für Zeichen die, die sie war; der
    freie Rest eines Ausgangs rechnet nicht mehr damit (``_outgoing_free_minor``, über ``bank_effect``)."""
    condition = Database._bank_effective_sql([3, 5])
    assert condition == ('(k.COA_ID IS NULL OR k.CounterCOA_ID IS NULL OR k.COA_ID IN (?,?) OR k.CounterCOA_ID IN (?,?))',
                         [3, 5, 3, 5])
    assert condition[0] in Database._free_minor_sql([3, 5])[0]
    assert not hasattr(Database, '_free_outgoing_minor_sql')


# ── Freier Rest eines Ausgangs: die Sätze nach ihrer Wirkung auf das Konto der Bewegung ──
# Der freie Rest ist ``|Betrag| − MAX(−Σ Zuordnungen, −Σ Wirkung der Sätze)``; die Wirkung eines Satzes ist die, mit der die
# Split-Summe rechnet (``bank_effect``). Ein Spiegel aus dem Import (``mirror_coa``, Rohbetrag, positiv = Konto im Soll) unter
# einem Ausgang „Bank → Tagesgeld“ steht als „1810 an 1800“ mit **+50**: Roh gerechnet sähe er nicht verbraucht aus.

def savings_account(w):
    """Ein zweites eigenes Bankkonto „Tagesgeld“ (SKR 1810): Eine Überweisung dorthin ist ein Ausgang mit einem Spiegel darunter."""
    w.db.insert_account('Tagesgeld', 'Eigene Firma GmbH', 'DE02120300000000202051', 'TESTDE2X', 'Testbank AG', is_cash=0,
                        skr_account=1810)


def out_with(w, *rows, amount=50.0, source_group=None, allocated=None, no_form=False):
    """Ein Ausgang über ``amount`` € mit Sätzen darunter, je ``(Konto-Nr., Gegenkonto-Nr., Betrag in €, Form)``; ``allocated``:
    eine Zuordnung (Betrag in €, negativ) an der Bewegung wie der WISO-Import sie anlegt; ``no_form``: die Sätze ohne gespeicherte
    Form (per Roh-SQL angelegt)."""
    db = w.db
    bank = outgoing(w, amount, '2026-07-10')
    for number, counter, value, form in rows:
        row = db.insert_booking('2026-07-10', value, coa_id=coa(db, number), counter_coa_id=coa(db, counter),
                                booking_type='entry', parent_booking_id=bank, entry_form=form)
        if source_group:
            run_sql(db, 'UPDATE Bookings SET SourceGroup = ? WHERE ID = ?', source_group, row)
        if no_form:
            run_sql(db, 'UPDATE Bookings SET EntryForm = NULL WHERE ID = ?', row)
    if allocated is not None:
        target = query_rows(db, "SELECT ID FROM Invoices WHERE InvoiceNumber = 'T-190'")
        other = target[0]['ID'] if target else sent(w, number='T-190')
        run_sql(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)',
                other, bank, int(round(allocated * 10000)), '2026-07-10')
    return bank


def split_sum_rest(db, bank):
    """Was die Split-Summe zur Bewegung sagt: ihr Rest in € (Betrag minus Wirkung der Sätze); 0, wenn sie aufgeht."""
    return {row[0]: row[3] for row in db.find_unbalanced_splits(*RANGE)}.get(bank, 0)


MIRROR_COA, MIRROR_COUNTER, MIRROR_LEGACY, PURPOSE = 'mirror_coa', 'mirror_counter', 'mirror_legacy', 'purpose_first'

#: (Sätze, Optionen von ``out_with``, freier Rest in €) unter einem Ausgang über 50,00 € auf das Giro-Konto 1800
FREE_REST_CASES = {
    'Import-Spiegel-mirror_coa-+50': ([(1810, 1800, 50.0, MIRROR_COA)], {}, 0),
    'mirror_coa-in-die-andere-Richtung': ([(1800, 1810, 50.0, MIRROR_COA)], {}, 50),
    'mirror_counter-Betrag-auf-dem-Gegenkonto': ([(1810, 1800, -50.0, MIRROR_COUNTER)], {}, 0),
    'mirror_counter-Bank-vorne': ([(1800, 1810, 50.0, MIRROR_COUNTER)], {}, 0),
    'mirror_legacy-roh-negativ': ([(1810, 1800, -50.0, MIRROR_LEGACY)], {}, 0),
    'mirror_legacy-roh-positiv': ([(1810, 1800, 50.0, MIRROR_LEGACY)], {}, 50),
    'ohne-Form-roh-negativ': ([(1810, 1800, -50.0, MIRROR_LEGACY)], {'no_form': True}, 0),
    'ohne-Form-roh-positiv': ([(1810, 1800, 50.0, MIRROR_LEGACY)], {'no_form': True}, 50),
    'teilweise-kontiert': ([(6815, 1800, -20.0, PURPOSE)], {}, 30),
    'WISO-Spiegel': ([(1810, 1800, 50.0, MIRROR_COA)], {'source_group': 'WISO-TEST-1'}, 0),
    'WISO-Ausgabe-teilweise': ([(6815, 1800, -30.0, PURPOSE)], {'source_group': 'WISO-TEST-2'}, 20),
    'negative-Import-Zuordnung': ([], {'allocated': -30.0}, 20),
    'Zuordnung-größer-als-der-Satz': ([(6815, 1800, -20.0, PURPOSE)], {'allocated': -30.0}, 20),
    'Satz-größer-als-die-Zuordnung': ([(6815, 1800, -20.0, PURPOSE)], {'allocated': -10.0}, 30),
    'positiver-Satz-unter-negativer-Bewegung': ([(6815, 1800, 20.0, PURPOSE)], {}, 50),
    'Umbuchung-ohne-Geldkonto': ([(6815, 4400, -20.0, 'transfer')], {}, 50),
    'ohne-Sätze': ([], {}, 50),
}


@pytest.mark.parametrize('rows, options, free', list(FREE_REST_CASES.values()), ids=list(FREE_REST_CASES))
def test_the_free_rest_of_an_outgoing_movement_counts_the_rows_by_their_effect_on_its_account(world, rows, options, free):
    db = world.db
    savings_account(world)
    r1, s1, _paid = paid_and_cancelled(world)                                  # 50,00 € Guthaben
    bank = out_with(world, *rows, **options)
    candidates = {c[0]: c[5] for c in db.get_unallocated_outgoing_bookings()}
    if rows and not options.get('allocated'):
        # so sieht es auch die Split-Summe: frei ist, was ihr Rest noch fasst – nie mehr als der Betrag der Bewegung selbst
        assert free == min(50, -split_sum_rest(db, bank))

    if free == 0:
        assert bank not in candidates                                          # die Umbuchung ist keine Erstattung
        refused(world, s1, bank, REFUND_EXHAUSTED_MESSAGE)
    else:
        assert candidates[bank] == free
        refund(world, s1, bank)
        assert allocations(db, s1)[0][1] == -free * 10000                      # erstattet wird höchstens der freie Rest
        assert db.get_storno_credit(r1) == 50 - free


def test_the_import_mirror_to_the_savings_account_is_no_candidate_and_no_refund(world):
    """Die Überweisung Giro → Tagesgeld, wie der Import sie schreibt („1810 an 1800“ mit +50): kein Kandidat, und die Erstattung
    lehnt mit dem Klartext ab – nichts geschrieben. Ohne den Spiegel (oder mit einem Betrag, der die Bewegung nicht verbraucht)
    wäre sie ein Kandidat mit „Frei 50,00“."""
    db = world.db
    savings_account(world)
    _r1, s1, _paid = paid_and_cancelled(world)
    transfer = out_with(world, (1810, 1800, 50.0, MIRROR_COA))
    free_one = outgoing(world, 50.0, '2026-07-09')

    assert [(c[0], c[5]) for c in db.get_unallocated_outgoing_bookings()] == [(free_one, 50)]
    assert refused(world, s1, transfer, REFUND_EXHAUSTED_MESSAGE).message == 'Kein offener Betrag zuordenbar (Guthaben oder Buchung ausgeschöpft)'
    assert db.find_unbalanced_splits(*RANGE) == []                              # für die Split-Summe geht der Spiegel auf


def test_the_free_rest_of_a_payment_still_counts_a_mirror_raw(world):
    """Beim Eingang bleibt es bewusst bei der Rohrechnung (``_free_minor_sql``): Der Spiegel „1800 an 1810“ mit +50 unter einem
    Eingang über 50 gilt dort als verbraucht – wie bisher."""
    db = world.db
    savings_account(world)
    incoming = db.insert_booking('2026-07-09', 50.0, booking_type='bank', account_id=world.bank, recipient_client=CUSTOMER_TEXT)
    db.insert_booking('2026-07-09', 50.0, coa_id=coa(db, 1800), counter_coa_id=coa(db, 1810), booking_type='entry',
                      parent_booking_id=incoming, entry_form=MIRROR_COA)
    assert db.get_booking_unallocated_amount(incoming) == 0 and incoming not in [c[0] for c in db.get_unallocated_bank_bookings()]


def counting_connections(db, monkeypatch, *, steps=None, statements=None):
    """Jede Verbindung der Datenbankschicht zählt mit: ``statements`` sammelt die SQL-Anweisungen, ``steps`` die Schritte der
    SQLite-Maschine (alle 100 Befehle ein Eintrag) – ein Maß für die Arbeit, das nicht an der Uhr hängt."""
    original = type(db)._get_connection

    def counting(self):
        conn = original(self)
        if statements is not None:
            conn.set_trace_callback(statements.append)
        if steps is not None:
            conn.set_progress_handler(lambda: (steps.append(1), 0)[1], 100)
        return conn
    monkeypatch.setattr(type(db), '_get_connection', counting)


def test_the_free_rest_of_an_outgoing_movement_is_never_more_than_its_amount(world):
    """Eine positive Zuordnung unter einem Ausgang (die Bewegung wurde nach der Zahlung umgedreht) machte ihn „freier“, als er ist:
    50,00 € Ausgang mit +30,00 € Zuordnung und ihrem Satz „Bank an Debitoren“ ergäben 80,00 € frei. Frei ist nie mehr als der
    Betrag der Bewegung."""
    db = world.db
    bank = out_with(world, (1800, DEBTORS, 30.0, FORM_LIQUID), allocated=30.0)
    with closing(db._get_connection()) as conn:
        assert db._booking_free_outgoing_minor(conn, bank, db.get_bank_coa_ids()) == 500000
    assert [(row[0], row[-1]) for row in db.get_unallocated_outgoing_bookings(None, limit=100) if row[0] == bank] in (
        [(bank, 50.0)], [(bank, 500000)])


def test_the_free_rest_of_an_outgoing_movement_is_asked_in_few_statements(world, monkeypatch):
    """Der freie Rest aller Ausgänge kommt aus wenigen gesammelten Abfragen: Die Zahl der Anweisungen ist bei 3 und bei 30 Ausgängen
    – mit Sätzen und Zuordnungen darunter – dieselbe (kein „eine Abfrage je Bewegung“)."""
    db = world.db
    savings_account(world)
    statements = []
    counting_connections(db, monkeypatch, statements=statements)
    counts, made = {}, 0
    for total in (3, 30):
        while made < total:
            made += 1
            out_with(world, (6815, 1800, -10.0, PURPOSE), (1810, 1800, 5.0, MIRROR_COA), allocated=-1.0 if made % 2 else None)
        statements.clear()
        assert len(db.get_unallocated_outgoing_bookings(None, limit=100)) == total
        counts[total] = len(statements)
    assert counts[3] == counts[30], counts
    assert counts[30] <= 12, counts


def test_the_candidate_list_does_not_grow_with_outgoing_movements_times_allocations(world, monkeypatch):
    """Vorher las die Kandidatenliste je Ausgang die Zuordnungen aller Bewegungen (``InvoicePayments`` hat keinen Index auf
    ``BookingID`` allein): 20 000 Ausgänge × 5 000 Zuordnungen brauchten gut 5 s je Seitenaufruf. Gemessen an den Schritten der
    SQLite-Maschine – deterministisch, nicht an der Uhr: Das Vierfache an Ausgängen und Zuordnungen kostet etwa das Vierfache, nicht
    das Sechzehnfache."""
    db = world.db
    invoice = sent(world)
    steps = []
    counting_connections(db, monkeypatch, steps=steps)
    cost, made = {}, 0
    for total in (50, 200):
        with closing(sqlite3.connect(db.db_name)) as con:
            con.executemany("INSERT INTO Bookings (DateBooking, Amount, Account_ID, BookingType) VALUES ('2026-01-01', ?, ?, 'bank')",
                            [(-10000 - n, world.bank) for n in range(made, total)])
            con.executemany("INSERT INTO Bookings (DateBooking, Amount, Account_ID, BookingType) VALUES ('2026-01-01', ?, ?, 'bank')",
                            [(10000 + n, world.bank) for n in range(made, total)])
            incoming = [row[0] for row in con.execute("SELECT ID FROM Bookings WHERE Amount > 0 AND ID NOT IN "
                                                      "(SELECT BookingID FROM InvoicePayments)")]
            con.executemany("INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 5, '2026-01-01')",
                            [(invoice, booking) for booking in incoming])
            con.commit()
        made = total
        steps.clear()
        assert len(db.get_unallocated_outgoing_bookings(None, limit=1000)) == total
        cost[total] = len(steps)
    assert cost[200] < 8 * cost[50], cost
# ── Entfernen ────────────────────────────────────────────────────────────────

def test_removing_a_refund_restores_every_row_of_every_table(world):
    """Zuordnung und markierte Sätze gehen, Forderung und Gegenbuchung bleiben, das Guthaben ist wieder da – und Bookings,
    InvoicePayments und Invoices stehen Zeile für Zeile wie vor der Erstattung (jede Spalte)."""
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0)
    before, ledger_before, euer_before = comparable(db), ledger(db), euer(db)
    refund(world, s1, bank, 20.0)
    assert comparable(db) != before and db.get_storno_credit(r1) == 30

    assert db.unassign_invoice_payment(db.get_invoice_payments(s1)[0][0]) == s1

    assert comparable(db) == before
    assert ledger(db) == ledger_before and euer(db) == euer_before
    assert db.get_storno_credit(r1) == db.get_storno_credit(s1) == 50 and db.get_storno_refunded(s1) == 0
    assert [r[3] for r in bookings_of(db, 'T-100')] == [1190000] and [r[3] for r in bookings_of(db, '26R001')] == [-1190000]
    assert standing(db, r1, s1) == [('cancelled', 690000), ('sent', -1190000)]
    assert [(c[0], c[5]) for c in db.get_unallocated_outgoing_bookings()] == [(bank, 50)]
    assert db.find_payment_inconsistencies() == []
    refund(world, s1, bank)                                                                   # und lässt sich neu zuordnen
    assert db.get_storno_credit(r1) == 0


def test_removing_the_refund_without_a_receivable_leaves_the_books_as_before(world):
    db = world.db
    r1 = sixteen_percent(world)
    booking = db.insert_booking('2026-07-01', 116.0, booking_type='bank', account_id=world.bank)
    assert handlers.link_booking_to_invoice_capped(db, r1, booking, 116.0, revenue_coa_id=coa(db, 4300)) == (True, None)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    bank = outgoing(world, 116.0)
    before = comparable(db)
    refund(world, s1, bank)
    db.unassign_invoice_payment(db.get_invoice_payments(s1)[0][0])
    assert comparable(db) == before and db.get_storno_credit(s1) == 116


def test_removing_the_last_refund_resets_a_counter_booking_the_matching_had_resolved(world):
    """Stufe 7 des Auto-Abgleichs setzt eine Forderung auf ``resolved``, sobald die Sätze „… an Debitoren“ sie ausgleichen – bei der
    Gegenbuchung der Stornorechnung also nach einer vollen Erstattung. Entfernen setzt sie zurück."""
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world, paid=119.0)
    refund(world, s1, outgoing(world, 119.0))
    db.link_bank_to_entries()
    assert query_rows(db, "SELECT Status FROM Bookings WHERE DocumentNumber = '26R001' AND ParentBooking_ID IS NULL") == [
        {'Status': 'resolved'}]
    assert db.find_payment_inconsistencies() == []

    db.unassign_invoice_payment(db.get_invoice_payments(s1)[0][0])

    assert query_rows(db, "SELECT Status FROM Bookings WHERE DocumentNumber = '26R001' AND ParentBooking_ID IS NULL") == [
        {'Status': None}]
    assert [r[3] for r in bookings_of(db, '26R001')] == [-1190000]
    assert db.find_payment_inconsistencies() == []


def test_the_matching_never_attaches_the_counter_booking_to_the_refund_movement(world):
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world, paid=119.0)
    bank = outgoing(world, 119.0)
    refund(world, s1, bank)
    before = comparable(db)
    db.link_bank_to_entries()
    after = comparable(db)
    assert {r['ID'] for r in after['Bookings'] if r['ParentBooking_ID'] == bank} == \
        {r['ID'] for r in before['Bookings'] if r['ParentBooking_ID'] == bank}
    assert [r[3] for r in bookings_of(db, '26R001')] == [-1190000]                           # die Gegenbuchung hängt an nichts


# ── Schutz des Guthabens ─────────────────────────────────────────────────────

def test_a_payment_cannot_be_removed_while_it_would_leave_more_refunded_than_paid(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 20.0)
    refund(world, s1, bank)
    payment_id = db.get_invoice_payments(r1)[0][0]
    before = comparable(db)

    with pytest.raises(PaymentRemovalRefused) as raised:
        db.unassign_invoice_payment(payment_id)

    assert str(raised.value) == ALREADY_REFUNDED_MESSAGE == (
        'Zu dieser Rechnung wurde bereits erstattet – erst die Erstattung an der Stornorechnung entfernen.')
    assert isinstance(raised.value, ValueError)
    assert comparable(db) == before and db.get_storno_credit(r1) == 30

    db.unassign_invoice_payment(db.get_invoice_payments(s1)[0][0])                          # erst die Erstattung entfernen …
    db.unassign_invoice_payment(payment_id)                                                 # … dann geht die Zahlung
    assert db.get_invoice_payments(r1) == [] and db.get_storno_credit(r1) == 0
    assert [r[3] for r in bookings_of(db, 'T-100')] == [1190000]                            # die Forderung bleibt (Storno-Paar)


def test_the_credit_is_what_counts_so_a_payment_the_refunds_do_not_need_can_go(world):
    """Zahlungen über 50,00 € und 30,00 €, erstattet 40,00 €: Die 30,00 € dürfen gehen (50 − 40 ≥ 0), die 50,00 € nicht
    (30 − 40 < 0)."""
    db = world.db
    r1 = sent(world)
    pay(world, r1, 50.0)
    pay(world, r1, 30.0)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    refund(world, s1, outgoing(world, 40.0))
    first, second = [p[0] for p in db.get_invoice_payments(r1)]
    with pytest.raises(PaymentRemovalRefused):
        db.unassign_invoice_payment(first)
    db.unassign_invoice_payment(second)
    assert db.get_storno_credit(r1) == 10 and db.find_payment_inconsistencies() == []
    with pytest.raises(PaymentRemovalRefused):
        db.unassign_invoice_payment(first)


def test_a_payment_of_an_invoice_without_credit_note_is_removed_as_before(world):
    db = world.db
    r1 = sent(world)
    pay(world, r1, 50.0)
    db.unassign_invoice_payment(db.get_invoice_payments(r1)[0][0])
    assert db.get_invoice_payments(r1) == []
    with pytest.raises(ValueError, match='not found'):
        db.unassign_invoice_payment(99999)


def test_the_check_list_names_a_refund_beyond_the_payments_when_the_guard_was_bypassed(world, monkeypatch):
    """Die Prüfliste bleibt für Eingriffe an der App vorbei (und Bestand aus älteren Versionen): Der Zustand „mehr erstattet als
    gezahlt“ erscheint in der Prüfliste – auf beiden Seiten des Paares. Die App selbst lässt ihn nicht mehr entstehen
    (``test_no_way_removes_a_payment_the_refund_needs``)."""
    db = world.db
    r1, s1, paid_booking = paid_and_cancelled(world)
    refund(world, s1, outgoing(world, 50.0))
    assert db.find_payment_inconsistencies() == []

    unguarded(monkeypatch)
    db.delete_transaction(paid_booking)

    expected = [('refund_exceeds_credit', s1, '26R001',
                 'Zur Stornorechnung 26R001 ist mehr erstattet (50,00 €) als zur Rechnung T-100 gezahlt wurde (0,00 €).')]
    assert findings(db) == expected
    assert findings(db, r1) == expected and findings(db, s1) == expected
    assert findings(db, sent(world, number='T-200')) == []
    assert db.get_storno_credit(r1) == -50 and 'Erstattung offen' not in _list_cells(world)[1]
    page = PageInvoice(db, {}, s1)
    assert 'Zur Stornorechnung 26R001 ist mehr erstattet (50,00 €)' in page
    assert 'Guthaben des Kunden: ' not in page and 'vollständig erstattet' not in page      # kein Guthaben, aber auch nicht „ganz erstattet“


def test_the_handler_answers_a_refused_removal_with_409_and_the_text(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    refund(world, s1, outgoing(world, 20.0))
    payment_id = db.get_invoice_payments(r1)[0][0]
    status, message = handlers.handle_delete_invoice_payment(json.dumps({'payment_id': payment_id}).encode())
    assert (status, message) == (409, ALREADY_REFUNDED_MESSAGE) and len(db.get_invoice_payments(r1)) == 1
    status, message = handlers.handle_delete_invoice_payment(
        json.dumps({'payment_id': db.get_invoice_payments(s1)[0][0]}).encode())
    assert (status, message) == (200, 'ok') and db.get_storno_credit(r1) == 50
    assert handlers.handle_delete_invoice_payment(json.dumps({'payment_id': 99999}).encode())[0] == 500


# ── Schutz des Guthabens auf jedem Weg ───────────────────────────────────────
# Das Guthaben eines Storno-Paares wird auf keinem Weg der Oberfläche negativ: die Rechnungsseite, der Bank-Editor (✕ an einer
# Zeile der Zahlung), das Löschen eines Satzes und das Löschen der Bankbewegung fragen vor dem ersten Schreiben dieselbe Prüfung
# (``_refund_removal_blocker``) und lehnen mit dem Klartext ab – nichts ist dann geändert. Das Entfernen der Erstattung bleibt frei.

def refunded_pair(w, refunded=20.0):
    """R1 (50,00 € gezahlt) storniert, ``refunded`` € erstattet – die Namen, die die Wege brauchen: die Zahlung (ID der Zuordnung,
    Bankbewegung, ihre beiden Sätze) und die Erstattung (dasselbe)."""
    db = w.db
    r1, s1, paid_booking = paid_and_cancelled(w)
    out = outgoing(w, refunded, '2026-07-10')
    refund(w, s1, out, refunded)

    def children(bank):
        return [row['ID'] for row in query_rows(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank)]
    return SimpleNamespace(w=w, db=db, r1=r1, s1=s1, bank=paid_booking, kids=children(paid_booking), out=out,
                           refund_kids=children(out), payment_id=db.get_invoice_payments(r1)[0][0],
                           refund_id=db.get_invoice_payments(s1)[0][0])


def _refusal(call):
    """Der Klartext, mit dem ``call`` das Entfernen ablehnt – ``None``, wenn es gelingt."""
    try:
        call()
    except PaymentRemovalRefused as exc:
        return str(exc)
    return None


def _invoice_page(c, payment_id=None):
    status, message = handlers.handle_delete_invoice_payment(json.dumps({'payment_id': payment_id or c.payment_id}).encode())
    if status == 409:
        return message
    assert (status, message) == (200, 'ok'), (status, message)
    return None


def _editor(c, *dropped, bank=None):
    """Die Maske der Bankbewegung öffnen, die Zeilen dieser Sätze entfernen (✕) und speichern, wie der Browser es schickt:
    der Klartext der Ablehnung (400), ``None``, wenn es gespeichert wurde."""
    post_data, _options = open_form(c.db, bank or c.bank)
    for row_id in dropped:
        post_data = drop_row(post_data, row_id)
    status, text = handle_add_transaction(c.db, post_data)
    if status == 400:
        return text
    assert status == 303, (status, text)
    return None


def _delete_route(c, booking_id):
    """``POST /transactions/delete``: der Grund aus der Weiterleitung auf die Liste – ``None`` bei einer Löschung."""
    status, location = handlers.handle_delete_transaction(c.db, booking_id)
    assert status == 303
    if location == '/transactions':
        return None
    assert location.startswith('/transactions?err='), location
    return unquote(location[len('/transactions?err='):])


PAYMENT_WAYS = {
    'Rechnungsseite': _invoice_page,
    'unassign_invoice_payment': lambda c: _refusal(lambda: c.db.unassign_invoice_payment(c.payment_id)),
    'Editor-Satz-1': lambda c: _editor(c, c.kids[0]),
    'Editor-Satz-2': lambda c: _editor(c, c.kids[1]),
    'Editor-beide-Sätze': lambda c: _editor(c, *c.kids),
    'remove_split_row-1': lambda c: _refusal(lambda: c.db.remove_split_row(c.kids[0])),
    'remove_split_row-2': lambda c: _refusal(lambda: c.db.remove_split_row(c.kids[1])),
    'delete_transaction-Satz-1': lambda c: _refusal(lambda: c.db.delete_transaction(c.kids[0])),
    'delete_transaction-Satz-2': lambda c: _refusal(lambda: c.db.delete_transaction(c.kids[1])),
    'delete_transaction-Bankbewegung': lambda c: _refusal(lambda: c.db.delete_transaction(c.bank)),
    'Löschroute-Satz': lambda c: _delete_route(c, c.kids[0]),
    'Löschroute-Bankbewegung': lambda c: _delete_route(c, c.bank),
}


@pytest.mark.parametrize('way', list(PAYMENT_WAYS.values()), ids=list(PAYMENT_WAYS))
def test_no_way_removes_a_payment_the_refund_needs(world, way):
    db = world.db
    c = refunded_pair(world)
    before = comparable(db)

    assert way(c) == ALREADY_REFUNDED_MESSAGE == (
        'Zu dieser Rechnung wurde bereits erstattet – erst die Erstattung an der Stornorechnung entfernen.')

    assert comparable(db) == before                                          # Bookings, InvoicePayments, Invoices: Zeile für Zeile
    assert db.get_storno_credit(c.r1) == 30 and len(db.get_invoice_payments(c.r1)) == 1
    # nach dem Entfernen der Erstattung geht derselbe Weg wieder
    db.unassign_invoice_payment(c.refund_id)
    assert way(c) is None
    assert db.get_invoice_payments(c.r1) == [] and db.get_storno_credit(c.r1) == 0
    assert db.find_payment_inconsistencies() == []


REFUND_WAYS = {
    'Rechnungsseite': lambda c: _invoice_page(c, c.refund_id),
    'unassign_invoice_payment': lambda c: _refusal(lambda: c.db.unassign_invoice_payment(c.refund_id)),
    'Editor-Satz-1': lambda c: _editor(c, c.refund_kids[0], bank=c.out),
    'Editor-Satz-2': lambda c: _editor(c, c.refund_kids[1], bank=c.out),
    'remove_split_row': lambda c: _refusal(lambda: c.db.remove_split_row(c.refund_kids[1])),
    'delete_transaction-Satz': lambda c: _refusal(lambda: c.db.delete_transaction(c.refund_kids[0])),
    'delete_transaction-Bankbewegung': lambda c: _refusal(lambda: c.db.delete_transaction(c.out)),
    'Löschroute-Satz': lambda c: _delete_route(c, c.refund_kids[0]),
    'Löschroute-Bankbewegung': lambda c: _delete_route(c, c.out),
}


@pytest.mark.parametrize('way', list(REFUND_WAYS.values()), ids=list(REFUND_WAYS))
def test_removing_a_refund_stays_free_on_every_way(world, way):
    db = world.db
    c = refunded_pair(world)
    assert way(c) is None
    assert db.get_invoice_payments(c.s1) == [] and db.get_storno_credit(c.r1) == 50       # das Guthaben ist wieder da
    assert len(db.get_invoice_payments(c.r1)) == 1                                        # die Zahlung bleibt
    assert db.find_payment_inconsistencies() == []


def test_a_save_with_several_changes_stores_nothing_when_one_row_would_remove_the_payment(world):
    """Alles oder nichts: Wer im Editor die Bankzeile ändert, eine Zeile ergänzt UND die Zeile der geschützten Zahlung entfernt,
    bekommt die Ablehnung – gespeichert ist nichts, auch nicht die Änderung der Bankzeile und die neue Zeile."""
    db = world.db
    c = refunded_pair(world)
    before = comparable(db)
    post_data, _options = open_form(db, c.bank)
    post_data['recipient'] = ['Geändert & Söhne']
    post_data = add_row(post_data, split_amount='5.00', split_coa=str(coa(db, 6815)), split_text='neue Zeile')
    post_data = drop_row(post_data, c.kids[1])

    assert handle_add_transaction(db, post_data) == (400, ALREADY_REFUNDED_MESSAGE)

    assert comparable(db) == before
    assert query_rows(db, 'SELECT RecipientClient FROM Bookings WHERE ID = ?', c.bank) == [
        {'RecipientClient': CUSTOMER_TEXT}]
    assert [r['ID'] for r in query_rows(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', c.bank)] == c.kids
    # ohne die Erstattung gelingt dasselbe Speichern – alle Änderungen auf einmal
    db.unassign_invoice_payment(c.refund_id)
    assert handle_add_transaction(db, post_data)[0] == 303
    assert query_rows(db, 'SELECT RecipientClient FROM Bookings WHERE ID = ?', c.bank) == [
        {'RecipientClient': 'Geändert & Söhne'}]
    assert [r['Text'] for r in query_rows(db, 'SELECT Text FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', c.bank)] == [
        'neue Zeile']


def test_a_collective_payment_loses_only_the_row_of_the_invoice_without_credit_note(world):
    """Eine Überweisung über 100,00 € zahlt R1 (50,00 €, später storniert und teilweise erstattet) und R3 (50,00 €): Im Editor geht
    die Zeile von R3 – die von R1 nicht. Beide stehen unter derselben Bankbewegung."""
    db = world.db
    r1, r3 = sent(world), sent(world, number='T-103')
    bank = db.insert_booking('2026-07-01', 100.0, booking_type='bank', account_id=world.bank, recipient_client=CUSTOMER_TEXT)
    for invoice in (r1, r3):
        assert handlers.link_booking_to_invoice_capped(db, invoice, bank, 50.0) == (True, None)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    refund(world, s1, outgoing(world, 20.0, '2026-07-10'), 20.0)
    rows_of = {number: [r['ID'] for r in query_rows(
        db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND DocumentNumber = ? ORDER BY ID', bank, number)]
        for number in ('T-100', 'T-103')}
    c = SimpleNamespace(db=db, bank=bank)
    before = comparable(db)

    assert _editor(c, *rows_of['T-100']) == ALREADY_REFUNDED_MESSAGE
    assert _editor(c, rows_of['T-103'][0], *rows_of['T-100']) == ALREADY_REFUNDED_MESSAGE        # auch zusammen mit der erlaubten
    assert comparable(db) == before

    assert _editor(c, *rows_of['T-103']) is None                                                  # R3 allein geht
    assert [(a[1], a[3]) for a in db.get_booking_allocations(bank)] == [(r1, 50)]
    assert db.get_invoice_payments(r3) == [] and db.get_storno_credit(r1) == 30


def test_the_prevention_looks_at_all_removals_of_one_save_together(world):
    """Zwei Zuordnungen desselben Paares (Doppel aus einem Altbestand: ein eindeutiger Index auf (Rechnung, Bewegung) fehlt dort),
    von denen jede allein gehen dürfte – 50,00 € gezahlt, 40,00 € erstattet: jede allein lässt 50 ≥ 40 –, aber zusammen nicht."""
    db = world.db
    r1 = sent(world)
    pay(world, r1, 50.0)
    pay(world, r1, 50.0)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    refund(world, s1, outgoing(world, 40.0, '2026-07-10'), 40.0)
    first, second = [p[0] for p in db.get_invoice_payments(r1)]
    with closing(sqlite3.connect(db.db_name)) as con:
        blocker = Database._refund_removal_blocker
        assert blocker(con, [first]) is None and blocker(con, [second]) is None
        assert blocker(con, [first, second]) == ALREADY_REFUNDED_MESSAGE
        assert blocker(con, [first, second, db.get_invoice_payments(s1)[0][0]]) is None          # mit der Erstattung geht alles
        assert blocker(con, []) is None and blocker(con, [db.get_invoice_payments(s1)[0][0]]) is None


def test_the_check_of_the_editor_is_read_only(world):
    db = world.db
    c = refunded_pair(world)
    before = comparable(db)
    assert db.split_removal_blocker([]) is None
    assert db.split_removal_blocker(c.kids) == db.split_removal_blocker([c.kids[0]]) == ALREADY_REFUNDED_MESSAGE
    assert db.split_removal_blocker(c.refund_kids) is None and db.split_removal_blocker([99999]) is None
    assert db.split_removal_blocker([str(c.kids[1])]) == ALREADY_REFUNDED_MESSAGE                    # IDs aus dem Formular sind Text
    assert comparable(db) == before


def test_a_row_the_editor_only_detaches_takes_no_assignment_with_it(world):
    """Ein importierter Satz (Quellklammer) wird beim ✕ nur abgehängt, nicht gelöscht – hängt eine Zahlung (Altbestand: eine
    Zuordnung an einem Buchungssatz statt an der Bankbewegung) an ihm, bleibt sie, und die Prüfung hat nichts dagegen."""
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    other = db.insert_booking('2026-07-05', 30.0, booking_type='bank', account_id=world.bank, recipient_client=CUSTOMER_TEXT)
    entry = db.insert_booking('2026-07-05', 30.0, coa_id=coa(db, 4400), counter_coa_id=coa(db, 1800), booking_type='entry',
                              parent_booking_id=other, entry_form=FORM_PURPOSE)
    run_sql(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, 300000, ?)',
            r1, entry, '2026-07-05')
    refund(world, s1, outgoing(world, 60.0, '2026-07-10'), 60.0)                  # 80,00 € gezahlt, 60,00 € erstattet
    before = comparable(db)

    run_sql(db, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-9' WHERE ID = ?", entry)
    assert db.split_removal_blocker([entry]) is None and _delete_route(SimpleNamespace(db=db), entry) == ALREADY_REFUNDED_MESSAGE
    assert db.detachable_split_rows([entry]) == {entry}
    run_sql(db, "UPDATE Bookings SET SourceGroup = NULL WHERE ID = ?", entry)
    assert db.split_removal_blocker([entry]) == ALREADY_REFUNDED_MESSAGE                              # gelöscht würde sie mitgehen
    assert comparable(db)['InvoicePayments'] == before['InvoicePayments']


def test_a_state_that_changed_between_planning_and_writing_is_a_refusal_and_not_a_500(world, monkeypatch):
    """Die Prüfung im Plan ist die Vorab-Prüfung; maßgeblich bleibt die in der Transaktion des Löschens. Ändert sich der Bestand
    dazwischen (zwei Fenster), antwortet der Handler mit dem Klartext statt mit einem Fehler – und die Zahlung bleibt."""
    db = world.db
    c = refunded_pair(world)
    monkeypatch.setattr(Database, 'split_removal_blocker', lambda self, ids: None)        # der Plan sah noch nichts
    assert _editor(c, c.kids[1]) == ALREADY_REFUNDED_MESSAGE
    assert len(db.get_invoice_payments(c.r1)) == 1 and db.get_storno_credit(c.r1) == 30


# ── Über den Server: Liste und Editor ────────────────────────────────────────

def test_over_http_the_list_route_redirects_with_the_reason_and_the_list_shows_it(server_port, books):
    db = books.db
    r1, s1, paid_booking = _http_pair(server_port, books)
    refund(books, s1, outgoing(books, 20.0, '2026-07-10'), 20.0)
    kids = [r['ID'] for r in query_rows(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', paid_booking)]
    before = comparable(db)

    for booking in (kids[0], paid_booking):
        resp = post(server_port, f'/transactions/delete?id={booking}', csrf=books.csrf)
        assert resp.status == 303, booking
        assert unquote(resp.headers['Location']) == f'/transactions?err={ALREADY_REFUNDED_MESSAGE}'
        assert comparable(db) == before
        page = get(server_port, resp.headers['Location'])
        assert page.status == 200 and app_msg_arguments(Dom(page.text)) == [ALREADY_REFUNDED_MESSAGE, 'error']

    free = db.insert_booking('2026-08-01', 5.0, booking_type='entry')                   # eine gewöhnliche Löschung bleibt, wie sie war
    ok = post(server_port, f'/transactions/delete?id={free}', csrf=books.csrf)
    assert (ok.status, ok.headers['Location']) == (303, '/transactions') and db.get_booking_by_id(free) is None


def test_over_http_the_editor_answers_with_the_text_and_stores_nothing(server_port, books):
    db = books.db
    r1, s1, paid_booking = _http_pair(server_port, books)
    refund(books, s1, outgoing(books, 20.0, '2026-07-10'), 20.0)
    kids = [r['ID'] for r in query_rows(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', paid_booking)]
    post_data, _options = open_form(db, paid_booking)
    post_data = drop_row(post_data, kids[0])
    before = comparable(db)

    fields = [(name, value) for name, values in post_data.items() for value in values] + [('csrf', books.csrf)]
    resp = post(server_port, '/transactions/add', form=fields)

    assert resp.status == 400 and resp.headers['Content-Type'].startswith('text/plain')
    assert resp.text == ALREADY_REFUNDED_MESSAGE and comparable(db) == before


LIST_RUNNER = r"""
const fs = require('fs'), vm = require('vm');
const log = {messages: []};
const sandbox = {document: {addEventListener: (name, handler) => { if (name === 'DOMContentLoaded') handler(); }},
  appMsg: (text, type) => log.messages.push([text, type])};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
console.log(JSON.stringify(log));
"""


@NO_NODE
def test_the_script_block_of_the_reason_on_the_list_parses_and_shows_it_as_an_error(world, tmp_path):
    reason = ALREADY_REFUNDED_MESSAGE + ' </script> "\'x'
    block = next(s for s in Dom(PageTransactions(world.db, err=reason)).scripts if re.search(r'function\(\)\{appMsg\(', s))
    log = _node(tmp_path, block.split('</script>')[0], LIST_RUNNER)
    assert log == {'messages': [[reason, 'error']]}


def test_the_list_page_shows_a_hostile_reason_only_as_text(world):
    hostile = '</script><img src=x onerror=alert(1)>'
    html = PageTransactions(world.db, err=hostile)
    assert '<img' not in html and app_msg_arguments(Dom(html)) == [hostile, 'error']
    assert app_msg_arguments(Dom(PageTransactions(world.db, err=ALREADY_REFUNDED_MESSAGE))) == [ALREADY_REFUNDED_MESSAGE, 'error']
    assert 'DOMContentLoaded",function(){appMsg(' not in PageTransactions(world.db)               # ohne Grund keine Meldung


# ── Umzug und Erstattung ─────────────────────────────────────────────────────
# Nach einer Erstattung zieht höchstens das verbleibende Guthaben auf die Ersatzrechnung um (``test_invoice_reissue.py``,
# „Umzug nach einer Erstattung“). Hier die Gegenrichtung und die Paare, die nichts miteinander zu tun haben.

def test_without_a_refund_the_payments_move_as_before_and_the_remaining_credit_can_be_refunded(world, monkeypatch):
    """Die Gegenrichtung braucht keine Sonderregel: Nach dem Umzug ist das Guthaben entsprechend kleiner."""
    s = paid_scenario(world, monkeypatch)
    edit_draft(world, s.r2, [dict(ONE_ITEM[0], unitPrice=33.61)])
    code, body = set_invoice_status(s.r2, 'sent')
    assert code == 200 and 'MOVE' not in body['hint'] and 'bereits erstattet' not in body['hint']
    assert world.db.get_storno_credit(s.s1) == 10
    refund(world, s.s1, outgoing(world, 10.0))
    assert world.db.get_storno_credit(s.s1) == 0


def test_a_refund_to_another_pair_does_not_stop_the_move_of_this_one(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    other_r, other_s, _paid = paid_and_cancelled(world, number='T-200', day='2026-07-03')
    refund(world, other_s, outgoing(world, 50.0))
    code, body = set_invoice_status(s.r2, 'sent')
    assert code == 200 and body['hint'].startswith('Zahlungen der stornierten Rechnung T-100 über 50,00 €')
    assert world.db.get_storno_credit(other_r) == 0 and world.db.get_storno_credit(s.s1) == 0


# ── Prüfliste ────────────────────────────────────────────────────────────────

def _forderung_full(w, monkeypatch):
    r1, s1, _paid = paid_and_cancelled(w)
    refund(w, s1, outgoing(w, 50.0))
    return r1, s1


def _forderung_partial(w, monkeypatch):
    r1, s1, _paid = paid_and_cancelled(w)
    refund(w, s1, outgoing(w, 20.0), 20.0)
    return r1, s1


def _single_row(w, monkeypatch):
    r1 = sixteen_percent(w)
    booking = w.db.insert_booking('2026-07-01', 116.0, booking_type='bank', account_id=w.bank)
    assert handlers.link_booking_to_invoice_capped(w.db, r1, booking, 116.0, revenue_coa_id=coa(w.db, 4300)) == (True, None)
    s1 = cancel(w, r1, '2026-07-02').storno_id
    refund(w, s1, outgoing(w, 58.0), 58.0)
    return r1, s1


def _tax_free(w, monkeypatch):
    r1 = sent(w, 'T-300', items=[dict(ONE_ITEM[0], taxRate=0)], taxRate=0, showTax=False)
    pay(w, r1, 100.0)
    s1 = cancel(w, r1, '2026-07-02').storno_id
    refund(w, s1, outgoing(w, 100.0))
    return r1, s1


def _after_the_move(w, monkeypatch):
    s = paid_scenario(w, monkeypatch)
    edit_draft(w, s.r2, [dict(ONE_ITEM[0], unitPrice=33.61)])
    assert set_invoice_status(s.r2, 'sent')[0] == 200
    refund(w, s.s1, outgoing(w, 10.0))
    return s.r1, s.s1


def _collective(w, monkeypatch):
    r1, s1, _p = paid_and_cancelled(w)
    r2 = sent(w, number='T-101')
    pay(w, r2, 50.0)
    s2 = cancel(w, r2, '2026-07-03').storno_id
    bank = outgoing(w, 100.0)
    refund(w, s1, bank)
    refund(w, s2, bank)
    return r1, s1


def _import_with_choice(w, monkeypatch):
    r1 = make_invoice(w, 'sent', number='T-201', items=SIXTEEN, taxRate=0.16)
    add_payment(w, r1, 116.0)
    s1 = cancel(w, r1, '2026-07-02').storno_id
    refund(w, s1, outgoing(w, 116.0), revenue_coa_id=coa(w.db, 4300))
    return r1, s1


@pytest.mark.parametrize('build', [_forderung_full, _forderung_partial, _single_row, _tax_free, _after_the_move, _collective,
                                   _import_with_choice],
                         ids=['Forderung-ganz', 'Forderung-teil', 'Einzelsatz', 'ohne-Steuer', 'nach-Umzug', 'Sammel',
                              'Import-mit-Wahl'])
def test_an_intact_refund_is_no_finding_in_any_category(world, monkeypatch, build):
    """Die Prüfliste ist lesend und linear; eine Erstattung (Zuordnung an der Stornorechnung mit ihren Sätzen, negativer Betrag
    auf einer ausgehenden Bewegung) löst in keiner Kategorie einen Befund aus – für das ganze Buch und je Beleg."""
    db = world.db
    r1, s1 = build(world, monkeypatch)
    assert findings(db) == [] and findings(db, r1) == [] and findings(db, s1) == []
    assert db.find_unbalanced_splits(*RANGE) == []
    assert PageInvoice(db, {}, s1).count('Zahlungszuordnung prüfen') == 0 == PageInvoice(db, {}, r1).count(
        'Zahlungszuordnung prüfen')


def test_the_existing_categories_stay_armed_for_a_refund_whose_rows_are_gone(world):
    """Die bestehenden Kategorien bleiben scharf: Eine Erstattung ohne ihre Sätze (per SQL entfernt) ist weiter ein Befund
    (``transfer_mismatch`` zeigt, dass nichts abgeschaltet wurde)."""
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0)
    refund(world, s1, bank)
    run_sql(db, 'DELETE FROM Bookings WHERE ParentBooking_ID = ?', bank)
    found = {(f['category']) for f in db.find_payment_inconsistencies()}
    assert 'transfer_mismatch' in found                                                     # 0 umgebucht, −50 zugeordnet


def test_a_refund_row_without_its_assignment_is_an_orphan_payment_row(world):
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0)
    refund(world, s1, bank)
    run_sql(db, 'DELETE FROM InvoicePayments WHERE InvoiceID = ?', s1)
    assert {f['category'] for f in db.find_payment_inconsistencies()} >= {'orphan_payment_rows'}


def test_the_new_category_is_found_for_the_pair_and_for_no_other_invoice(world, monkeypatch):
    db = world.db
    r1, s1, paid_booking = paid_and_cancelled(world)
    other = sent(world, number='T-200')
    refund(world, s1, outgoing(world, 30.0))
    pay(world, other, 10.0)
    unguarded(monkeypatch)
    db.delete_transaction(paid_booking)

    expected = [('refund_exceeds_credit', s1, '26R001',
                 'Zur Stornorechnung 26R001 ist mehr erstattet (30,00 €) als zur Rechnung T-100 gezahlt wurde (0,00 €).')]
    assert findings(db) == expected
    assert findings(db, other) == []
    # nach dem Entfernen der Erstattung ist das Buch in Ordnung
    db.unassign_invoice_payment(db.get_invoice_payments(s1)[0][0])
    assert findings(db) == [] and db.get_storno_credit(r1) == 0


def test_the_check_list_stays_read_only_with_refunds(world, monkeypatch):
    db = world.db
    r1, s1, paid_booking = paid_and_cancelled(world)
    refund(world, s1, outgoing(world, 50.0))
    unguarded(monkeypatch)
    db.delete_transaction(paid_booking)
    before = comparable(db)
    assert findings(db) != []
    assert comparable(db) == before


def test_the_check_list_needs_no_more_statements_for_more_pairs(world, monkeypatch):
    """Die Summen der Zuordnungen je Rechnung werden vorab zusammengefasst: Bei 2 und bei 8 Paaren mit Erstattung läuft dieselbe
    Zahl von Anweisungen."""
    db = world.db
    statements = []
    original = type(db)._get_connection

    def counting(self):
        conn = original(self)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(type(db), '_get_connection', counting)
    counts = {}
    made = 0
    for total in (2, 8):
        while made < total:
            made += 1
            r, s, _p = paid_and_cancelled(world, number=f'T-{500 + made}', day='2026-07-03')
            refund(world, s, outgoing(world, 50.0, f'2026-08-{made:02d}'))
        statements.clear()
        assert db.find_payment_inconsistencies() == []
        counts[total] = len(statements)
    assert counts[2] == counts[8], counts


# ── Nebenläufigkeit ──────────────────────────────────────────────────────────

def test_a_second_call_sees_the_state_the_first_one_left(world):
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    first, second = outgoing(world, 50.0), outgoing(world, 50.0, '2026-07-11')
    refund(world, s1, first)
    refused(world, s1, first, 'Buchung ist bereits mit dieser Rechnung verknüpft')           # dasselbe Paar
    refused(world, s1, second, REFUND_NO_CREDIT_MESSAGE)                                      # das Guthaben ist weg
    assert len(db.get_invoice_payments(s1)) == 1


def _race(world, s1, bookings):
    """Zwei Anfragen gleichzeitig, je mit eigener Datenbankverbindung; liefert ihre Ergebnisse (ok) sortiert."""
    path = world.db.db_name
    barrier = threading.Barrier(2)
    outcomes, errors = [], []

    def click(booking):
        try:
            local = Database(db_name=path)
            barrier.wait(timeout=10)
            outcomes.append(local.assign_storno_refund(s1, booking).ok)
        except Exception as exc:                                     # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=click, args=(booking,)) for booking in bookings]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    return sorted(outcomes)


def test_two_simultaneous_requests_never_refund_more_than_the_credit(world):
    """Zwei Anfragen, zwei Bewegungen über je 50,00 € und 50,00 € Guthaben: Genau eine gelingt, die andere findet das Guthaben
    verbraucht – es wird nie mehr erstattet als gezahlt."""
    for run in range(8):
        _r1, s1, _paid = paid_and_cancelled(world, number=f'T-{700 + run}')
        first, second = outgoing(world, 50.0, '2026-07-10'), outgoing(world, 50.0, '2026-07-11')
        assert _race(world, s1, (first, second)) == [False, True], f'Lauf {run}'
        assert query_rows(world.db, 'SELECT SUM(Amount) AS total FROM InvoicePayments WHERE InvoiceID = ?', s1) == [
            {'total': -500000}]
        assert world.db.get_storno_credit(s1) == 0 and len(world.db.get_invoice_payments(s1)) == 1


def test_two_simultaneous_requests_for_the_same_pair_assign_it_once(world):
    for run in range(8):
        _r1, s1, _paid = paid_and_cancelled(world, number=f'T-{800 + run}')
        bank = outgoing(world, 100.0, '2026-07-10')
        assert _race(world, s1, (bank, bank)) == [False, True], f'Lauf {run}'
        assert len(world.db.get_invoice_payments(s1)) == 1 and world.db.get_storno_credit(s1) == 0
        assert len(query_rows(world.db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', bank)) == 2


# ── DATEV-Export und Dashboard ───────────────────────────────────────────────

def _datev(db):
    """Die Zeilen des Exports als Dicts mit den Feldern, die zählen; der Export darf nicht gesperrt sein."""
    result = handle_datev_export(db, {'date_from': [RANGE[0]], 'date_to': [RANGE[1]]})
    assert isinstance(result[0], bytes), f'DATEV-Export gesperrt: {result}'
    lines = [ln for ln in result[0].decode('cp1252').split('\r\n') if ln]
    rows = [next(csv.reader([ln], delimiter=';', quotechar='"')) for ln in lines[2:]]
    return [{'amount': f[0], 'sh': f[1], 'konto': f[6], 'gegen': f[7], 'bu': f[8], 'date': f[9], 'beleg': f[10]} for f in rows]


def test_the_export_carries_the_refund_rows_with_the_opposite_debit_credit_mark_and_the_same_tax_key(world):
    """Zahlung und Erstattung: gleicher Betrag, entgegengesetztes S/H, derselbe BU-Schlüssel (die Umbuchung mit Erlöskonto
    schlüsselt immer nach der Umsatzsteuer, ``_bu_direction``) – das Konto bleibt, der Stapel geht auf."""
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    refund(world, s1, outgoing(world, 50.0, '2026-07-10'))

    rows = {(r['beleg'], r['konto'], r['gegen']): r for r in _datev(db)}
    paid_bank, refund_bank = rows[('T-100', '1800', '10000')], rows[('26R001', '1800', '10000')]
    paid_tax, refund_tax = rows[('T-100', '4405', '4400')], rows[('26R001', '4405', '4400')]
    assert (paid_bank['amount'], paid_bank['sh'], paid_bank['bu'], paid_bank['date']) == ('50,00', 'S', '0', '0107')
    assert (refund_bank['amount'], refund_bank['sh'], refund_bank['bu'], refund_bank['date']) == ('50,00', 'H', '0', '1007')
    assert (paid_tax['amount'], paid_tax['sh'], paid_tax['bu']) == ('50,00', 'S', '3')
    assert (refund_tax['amount'], refund_tax['sh'], refund_tax['bu']) == ('50,00', 'H', '3')           # BU 3: 19 % USt
    assert db.find_unbalanced_splits(*RANGE) == []


def test_the_export_gives_the_single_refund_row_the_opposite_mark_and_the_input_tax_key_of_its_direction(world):
    """Der Einzelsatz „Erlöskonto an Bank“ ist keine Umbuchung: Sein BU-Schlüssel folgt der Richtung der Geldbewegung – die
    Erstattung (Ausgang) bekommt den Schlüssel der Vorsteuer-Richtung (16 %: 7), die Zahlung den der Umsatzsteuer (16 %: 5).
    Das ist die Regel des Exports (``_bu_direction``); sie bleibt hier unberührt (``docs/aufgaben.md``)."""
    db = world.db
    r1 = sixteen_percent(world)
    booking = db.insert_booking('2026-07-01', 116.0, booking_type='bank', account_id=world.bank)
    assert handlers.link_booking_to_invoice_capped(db, r1, booking, 116.0, revenue_coa_id=coa(db, 4300)) == (True, None)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    refund(world, s1, outgoing(world, 116.0, '2026-07-10'))

    rows = {r['beleg']: r for r in _datev(db)}
    assert (rows['T-201']['amount'], rows['T-201']['sh'], rows['T-201']['bu']) == ('116,00', 'H', '5')
    assert (rows['26R001']['amount'], rows['26R001']['sh'], rows['26R001']['bu']) == ('116,00', 'S', '7')
    assert (rows['26R001']['konto'], rows['26R001']['gegen']) == ('4300', '1800')


def test_the_dashboard_counts_the_refund_once_as_an_outgoing_movement_and_the_euer_once_as_negative_revenue(world):
    """Das Dashboard folgt den Kontobewegungen: Die Zahlung ist eine Einnahme (+50,00 €), die Erstattung eine Ausgabe (−50,00 €) –
    die Sätze darunter zählen dort nicht noch einmal. Die EÜR nimmt den Erlös am Tag der Erstattung zurück (−42,02 € / −7,98 €)
    und führt sie nicht als Ausgabe."""
    from server.pages_dashboard import PageDashboard
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    refund(world, s1, outgoing(world, 50.0, '2026-07-10'))

    assert db.get_dashboard_totals('2026-01-01', '2026-12-31') == {
        'income': 50.0, 'private': 0.0, 'expense': -50.0, 'balance': 0.0, 'bank_count': 2, 'unlinked_count': 0}
    assert [m for m in db.get_dashboard_monthly('2026-06-01', '2026-08-31') if m['income'] or m['expense']] == [
        {'year_month': '2026-07', 'label': 'Jul', 'income': 50.0, 'private': 0.0, 'expense': -50.0}]
    assert db.get_dashboard_totals('2026-07-10', '2026-07-10')['expense'] == -50.0
    assert db.get_dashboard_totals('2026-07-10', '2026-07-10')['income'] == 0
    assert euer_on(db, '2026-07-10') == {4400: -42.02, 3806: -7.98} and euer(db) == {}

    def plain(html):
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html))

    day = plain(PageDashboard(db, '2026-07-10', '2026-07-10'))
    assert '3806 Umsatzsteuer 19% -7,98 €' in day and '4400 Erlöse 19% USt -42,02 €' in day
    assert 'Summe Betriebseinnahmen -50,00 €' in day and 'Summe Betriebsausgaben 0,00 €' in day
    year = plain(PageDashboard(db, '2026-01-01', '2026-12-31'))
    assert 'Summe Betriebseinnahmen 0,00 €' in year and 'Ergebnis EÜR 0,00 €' in year
    assert 'Zahlungseingänge 50,00 €' in year.replace('&auml;', 'ä') and 'Saldo 0,00 €' in year


# ── Bank-Editor ──────────────────────────────────────────────────────────────

def refund_ui(w, storno_id, booking, amount=None, **extra):
    """Eine Erstattung, wie die Rechnungsseite sie zuordnet: über die Route ``/invoice/link-payment`` (Handler) – mit dem Kunden
    auf der Bankzeile."""
    body = {'invoice_id': storno_id, 'transaction_id': booking, **extra}
    if amount is not None:
        body['amount_paid'] = amount
    status, message = handlers.handle_link_invoice_payment(json.dumps(body).encode())
    assert (status, message) == (200, f'/invoice/view?id={storno_id}'), (status, message)


def _ui_forderung(w):
    r1, s1, _paid = paid_and_cancelled(w)
    refund_ui(w, s1, outgoing(w, 50.0), 50.0)


def _ui_single_row(w):
    r1 = sixteen_percent(w)
    booking = w.db.insert_booking('2026-07-01', 116.0, booking_type='bank', account_id=w.bank)
    assert handlers.link_booking_to_invoice_capped(w.db, r1, booking, 116.0, revenue_coa_id=coa(w.db, 4300)) == (True, None)
    refund_ui(w, cancel(w, r1, '2026-07-02').storno_id, outgoing(w, 58.0), 58.0)


def _ui_collective(w):
    _r1, s1, _p1 = paid_and_cancelled(w)
    r2 = sent(w, number='T-101')
    pay(w, r2, 50.0)
    s2 = cancel(w, r2, '2026-07-03').storno_id
    bank = outgoing(w, 100.0)
    refund_ui(w, s1, bank)
    refund_ui(w, s2, bank)


def _ui_import(w):
    r1 = make_invoice(w, 'sent', number='T-201', items=SIXTEEN, taxRate=0.16)
    add_payment(w, r1, 116.0)
    refund_ui(w, cancel(w, r1, '2026-07-02').storno_id, outgoing(w, 116.0), revenue_coa_id=coa(w.db, 4300))


@pytest.mark.parametrize('build', [_ui_forderung, _ui_single_row, _ui_collective, _ui_import],
                         ids=['Forderung', 'Einzelsatz', 'Sammel', 'Import-mit-Wahl'])
def test_the_bank_editor_opens_and_saves_a_movement_with_refund_rows_without_changing_anything(world, build):
    """Öffnen und unverändert speichern ändert nichts (jede Spalte jeder Buchung): auch bei einer ausgehenden Bewegung mit den
    Sätzen einer Erstattung. Die Summenzeile des Editors geht auf."""
    from tests.entry_form_roundtrip import editor_rest
    db = world.db
    build(world)
    movements = _refund_movements(db)
    assert movements
    names = {coa_id: number for coa_id, number in db.get_coa_id_to_number_map().items()}
    for bank in movements:
        before = bookings_snapshot(db)
        for _round in range(2):                                   # und ein zweites Mal
            post, _options = open_form(db, bank)
            submit(db, post)
            assert differences(before, bookings_snapshot(db), names) == [], f'Bewegung {bank}'
        assert editor_rest(db, bank) == 0.0, f'Bewegung {bank}'
    assert db.find_payment_inconsistencies() == []


def _refund_movements(db):
    return [row['ParentBooking_ID'] for row in query_rows(
        db, 'SELECT DISTINCT b.ParentBooking_ID FROM Bookings b JOIN Bookings p ON p.ID = b.ParentBooking_ID '
            'WHERE b.Status = ? AND p.Amount < 0 ORDER BY b.ParentBooking_ID', PAYMENT_MARK)]


def test_the_route_fills_in_the_customer_of_the_movement_but_nothing_else(world):
    """Von dem, was die Zahlung der Bankzeile nachträgt (``_backfill_booking_skr``), bleibt bei der Erstattung der Kunde: Ohne
    ihn trüge ihn die Bankmaske beim ersten Speichern ein. Eine Bewegung, die schon einen Kunden hat, behält ihn."""
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    r2, s2, _p2 = paid_and_cancelled(world, number='T-101', day='2026-07-03')
    free, owned = outgoing(world, 50.0), outgoing(world, 50.0, '2026-07-11', contact_id=world.cust2)
    refund_ui(world, s1, free)
    refund_ui(world, s2, owned)
    row = 'SELECT Contact_ID, COA_ID, CounterCOA_ID, TaxRate, TaxAmount, DocumentNumber, Amount FROM Bookings WHERE ID = ?'
    assert query_rows(db, row, free) == [dict(Contact_ID=world.cust, COA_ID=None, CounterCOA_ID=None, TaxRate=None,
                                               TaxAmount=None, DocumentNumber=None, Amount=-500000)]
    assert query_rows(db, row, owned)[0]['Contact_ID'] == world.cust2
    # direkt über die Datenbank-Schicht (ohne Route) schreibt die Zuordnung die Bankzeile nie
    third = outgoing(world, 10.0, '2026-07-12')
    cancel_extra = cancel(world, sent(world, number='T-102'), '2026-07-04').storno_id
    pay_extra = db.insert_booking('2026-07-01', 10.0, booking_type='bank', account_id=world.bank)
    run_sql(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES ((SELECT CancelsInvoiceId FROM '
                'Invoices WHERE ID = ?), ?, 100000, ?)', cancel_extra, pay_extra, '2026-07-01')
    refund(world, cancel_extra, third)
    assert query_rows(db, 'SELECT Contact_ID FROM Bookings WHERE ID = ?', third) == [{'Contact_ID': None}]


def test_removing_one_refund_row_in_the_bank_editor_removes_the_whole_assignment(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0)
    before = comparable(db)
    refund(world, s1, bank)
    ids = [row['ID'] for row in query_rows(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank)]
    assert len(ids) == 2
    post, _options = open_form(db, bank)

    submit(db, drop_row(post, ids[1]))                          # die Umbuchung „Wartekonto an Erlöskonto“ entfernen

    assert query_rows(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', bank) == []
    assert db.get_invoice_payments(s1) == [] and db.get_storno_credit(r1) == 50
    after = comparable(db)
    assert after['InvoicePayments'] == before['InvoicePayments']
    assert [r for r in after['Bookings'] if r['ID'] not in {x['ID'] for x in before['Bookings']}] == []
    assert after['Invoices'] == before['Invoices']
    assert [r[3] for r in bookings_of(db, '26R001')] == [-1190000]            # die Gegenbuchung der Stornorechnung bleibt
    assert db.find_payment_inconsistencies() == []


def test_the_bank_editor_offers_no_new_way_to_assign_a_credit_note(world):
    """Die Zeile „Rechnung“ der Bankmaske kennt nur offene Rechnungen mit freiem Eingang; auch ein per Hand geschickter
    ``invoice_id`` einer Stornorechnung ordnet über die Maske nichts zu."""
    from server.pages_transactions import PageTransactions
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0)
    page = PageTransactions(db, edit_transaction_id=bank)
    assert 'name="invoice_id"' not in page                      # keine Rechnungsauswahl an einem Ausgang
    before = comparable(db)
    post = {'transaction_id': [str(bank)], 'date': ['2026-07-10'], 'amount': ['-50.00'], 'account': [str(world.bank)],
            'invoice_id': [str(s1)]}
    status, location = handle_add_transaction(db, post)
    assert status == 303 and location.startswith(f'/invoice/edit?id={s1}&payment_error=')
    assert 'Stornorechnung' in location
    after = comparable(db)
    assert after['InvoicePayments'] == before['InvoicePayments'] and len(after['Bookings']) == len(before['Bookings'])


# ── Unverändert: die Zahlung auf eine Stornorechnung bleibt abgelehnt ────────

def test_the_payment_ways_still_refuse_a_credit_note(world):
    db = world.db
    r1, s1, paid_booking = paid_and_cancelled(world)
    out = outgoing(world, 50.0)
    incoming = db.insert_booking('2026-07-05', 50.0, booking_type='bank', account_id=world.bank)
    before = comparable(db)
    for booking in (out, incoming):
        assigned = db.assign_invoice_payment(s1, booking)
        assert (assigned.ok, assigned.message) == (False, STORNO_PAYMENT_MESSAGE)
        assert handlers.link_booking_to_invoice_capped(db, s1, booking, 10.0) == (False, STORNO_PAYMENT_MESSAGE)
    assert db.payment_precheck(s1, world.bank) == STORNO_PAYMENT_MESSAGE
    with pytest.raises(ValueError, match='Stornorechnung'):
        db.link_invoice_to_transaction(s1, out, -50.0)
    assert comparable(db) == before
    assert s1 not in [row[0] for row in db.get_open_invoices()]


def test_the_refund_takes_neither_an_incoming_payment_nor_an_invoice(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    out = outgoing(world, 50.0)
    incoming = db.insert_booking('2026-07-05', 50.0, booking_type='bank', account_id=world.bank)
    assert db.assign_storno_refund(r1, out).message == REFUND_ONLY_STORNO_MESSAGE
    assert db.assign_storno_refund(s1, incoming).message == REFUND_ONLY_OUTGOING_MESSAGE


# ── Die Seiten ───────────────────────────────────────────────────────────────

CREDIT_HINT = 'Einer Stornorechnung lässt sich keine Zahlung zuordnen.'


def box_of(html):
    """Der Kasten „Erstattung“ der Seite (ohne sein Skript); ``None``, wenn die Seite keinen hat."""
    if 'id="refund_box"' not in html:
        return None
    start = html.index('<div class="rectRounded no-pdf" id="refund_box">')
    return html[start:html.index('<script>', start)]


def refund_buttons(html):
    """(Bewegung, vorbelegter Betrag) je Knopf „Als Erstattung zuordnen“, in der Reihenfolge der Seite."""
    dom = Dom(box_of(html) or '')
    amounts = {a['id']: a['value'] for a in dom.all('input') if (a.get('id') or '').startswith('refundAmount_')}
    found = []
    for button in dom.all('button'):
        match = re.fullmatch(r'linkRefund\((\d+), this\)', button.get('onclick') or '')
        if match:
            found.append((int(match.group(1)), amounts[f'refundAmount_{match.group(1)}']))
    return found


def test_the_credit_note_page_offers_the_outgoing_movements_of_the_customer_first_with_the_amount_filled_in(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    older = outgoing(world, 50.0, '2026-07-10', contact_id=world.cust)
    newer_other = outgoing(world, 30.0, '2026-07-20', contact_id=world.cust2)
    big_other = outgoing(world, 70.0, '2026-07-21', contact_id=world.cust2)
    incoming = db.insert_booking('2026-07-22', 20.0, booking_type='bank', account_id=world.bank)
    used = outgoing(world, 40.0, '2026-07-23')
    db.insert_booking('2026-07-23', -40.0, coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800), booking_type='entry',
                      parent_booking_id=used, entry_form=FORM_PURPOSE)

    html = PageInvoice(db, {}, s1)

    box = box_of(html)
    assert '<h3>Erstattung</h3>' in box and CREDIT_HINT not in html
    assert 'Zahlung verbuchen' not in html and '/transactions?from_invoice=' not in html
    buttons = refund_buttons(html)
    assert buttons[0][0] == older                                                              # die Bewegung des Kunden zuerst
    assert dict(buttons) == {older: '50.00', newer_other: '30.00', big_other: '50.00'}        # min(Guthaben, freier Rest)
    assert incoming not in dict(buttons) and used not in dict(buttons)                         # nur Ausgänge mit freiem Rest
    assert 'Guthaben des Kunden: 50,00 € – Zahlungen auf die stornierte Rechnung' in html
    assert '✓' in box                                                                          # Frei = Guthaben
    dom = Dom(html)
    assert dom.one('input', id='invoice_locked')['value'] == 'true'                           # die Seite bleibt schreibgeschützt
    assert not any('saveInvoice' in (b.get('onclick') or '') for b in dom.all('button'))


def test_the_credit_note_page_without_credit_and_without_refunds_keeps_the_old_hint(world, monkeypatch):
    db = world.db
    unpaid = cancel(world, sent(world, number='T-200'), '2026-07-02').storno_id
    outgoing(world, 50.0)
    html = PageInvoice(db, {}, unpaid)
    assert box_of(html) is None and CREDIT_HINT in html and 'Guthaben vollständig' not in html
    assert '<h3>Zahlungsverknüpfungen</h3>' in html
    s = paid_scenario(world, monkeypatch, number='T-300')
    assert set_invoice_status(s.r2, 'sent')[0] == 200                                         # alles zog um: Guthaben 0
    html = PageInvoice(db, {}, s.s1)
    assert box_of(html) is None and CREDIT_HINT in html and 'Guthaben vollständig' not in html


def test_after_a_partial_refund_the_page_lists_it_with_removal_and_offers_the_rest(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    first = outgoing(world, 50.0, '2026-07-10', document_number='RÜ-1')
    second = outgoing(world, 30.0, '2026-07-20')
    refund(world, s1, first, 20.0)
    payment_id = db.get_invoice_payments(s1)[0][0]

    html = PageInvoice(db, {}, s1)

    dom = Dom(box_of(html))
    assert (f'<td>2026-07-10</td><td><a href="/transactions/edit?id={first}">#{first} RÜ-1</a></td>'
            '<td class="num">-20,00 €</td>') in box_of(html)
    assert any(a.get('onclick') == f'deleteRefund({payment_id})' for a in dom.all('a'))
    assert refund_buttons(html) == [(second, '30.00')]                                       # die schon zugeordnete fehlt
    row = '<tr><td>Storno:</td><td>Guthaben des Kunden: 30,00 € – Zahlungen auf die stornierte Rechnung</td></tr>'
    assert row in html and row in PageInvoice(db, {}, r1)
    assert 'Guthaben vollständig erstattet' not in html


def test_the_credit_note_page_says_so_when_no_outgoing_movement_has_a_free_rest(world):
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    html = PageInvoice(db, {}, s1)
    box = box_of(html)
    assert refund_buttons(html) == [] and 'Es gibt keine ausgehende Bankbewegung mit freiem Rest' in box
    assert 'Guthaben des Kunden: 50,00 €' in html and 'Ausgehende Bankbewegung als Erstattung zuordnen' not in box


def test_after_a_full_refund_both_pages_say_so_and_the_credit_note_offers_only_the_removal(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    bank = outgoing(world, 50.0, '2026-07-10')
    other = outgoing(world, 20.0, '2026-07-15')
    refund(world, s1, bank)
    payment_id = db.get_invoice_payments(s1)[0][0]

    for invoice_id in (r1, s1):
        html = PageInvoice(db, {}, invoice_id)
        assert '<tr><td>Storno:</td><td>Guthaben vollständig erstattet.</td></tr>' in html, invoice_id
        assert 'Guthaben des Kunden: ' not in html
    html = PageInvoice(db, {}, s1)
    box = box_of(html)
    assert 'Das Guthaben ist vollständig erstattet.' in box and refund_buttons(html) == []
    assert 'Ausgehende Bankbewegung als Erstattung zuordnen' not in box
    assert f'deleteRefund({payment_id})' in box and other not in [b for b, _a in refund_buttons(html)]
    assert box_of(PageInvoice(db, {}, r1)) is None                                           # die stornierte Rechnung hat keinen
    # eine nie erstattete Rechnung zeigt die Zeile nicht
    assert 'Guthaben vollständig' not in PageInvoice(db, {}, sent(world, number='T-400'))


def test_the_page_names_the_revenue_account_choice_only_where_nothing_decides_it(world):
    """Ein Import auf 16 % ohne Historie: Die Seite fragt das Erlöskonto ab, wie bei der Zahlung (Feld ``revenueCoa``,
    Pflichtfeld, Vorauswahl nur ein Vorschlag). Ist eines bekannt, fehlt das Feld."""
    db = world.db
    r1 = make_invoice(world, 'sent', number='T-201', items=SIXTEEN, taxRate=0.16)
    add_payment(world, r1, 116.0)
    s1 = cancel(world, r1, '2026-07-02').storno_id
    outgoing(world, 116.0)

    html = PageInvoice(db, {}, s1)

    select = Dom(box_of(html)).one('select', id='revenueCoa')
    assert 'required' in select and select['name'] == 'revenue_coa_id'
    assert 'Für 16 % ist kein Erlöskonto hinterlegt.' in box_of(html)
    # mit bekanntem Konto (die Zahlung eines anderen Beleges lehrte es) braucht die Seite keine Wahl
    other = sixteen_percent(world, 'T-202')
    other_bank = db.insert_booking('2026-07-02', 116.0, booking_type='bank', account_id=world.bank)
    assert handlers.link_booking_to_invoice_capped(db, other, other_bank, 116.0, revenue_coa_id=coa(db, 4300)) == (True, None)
    assert 'id="revenueCoa"' not in box_of(PageInvoice(db, {}, s1))


def test_the_page_escapes_what_comes_from_the_books(world):
    db = world.db
    hostile = '<img src=x onerror=alert(1)>"\'</script>'
    r1, s1, _paid = paid_and_cancelled(world)
    first = outgoing(world, 50.0, '2026-07-10', recipient=hostile, text=hostile, document_number=hostile)
    second = outgoing(world, 30.0, '2026-07-20', recipient=hostile)
    refund(world, s1, first, 20.0)
    html = PageInvoice(db, {}, s1)
    box = box_of(html)
    assert hostile not in html and '<img' not in box and '&lt;img src=x onerror=alert(1)&gt;' in box
    dom = Dom(html)
    assert not any(name.startswith('on') and 'alert' in value for _tag, attrs in dom.tags for name, value in attrs.items())
    assert refund_buttons(html) == [(second, '30.00')]
    benign = PageInvoice(db, {}, s1).count('<script>')
    run_sql(db, 'UPDATE Bookings SET RecipientClient = ?, Text = ?, DocumentNumber = ? WHERE ID = ?', 'x', 'x', 'x', first)
    assert PageInvoice(db, {}, s1).count('<script>') == benign                               # Aufbau hängt nicht vom Text ab


def test_the_removal_question_reaches_the_script_as_a_json_string(world):
    db = world.db
    _r1, s1, _paid = paid_and_cancelled(world)
    refund(world, s1, outgoing(world, 50.0))
    script = next(s for s in Dom(PageInvoice(db, {}, s1)).scripts if 'function deleteRefund(' in s)
    assert json.dumps(REFUND_CONFIRM_QUESTION, ensure_ascii=False) in script.replace('\\u00e4', 'ä')
    assert REFUND_CONFIRM_QUESTION == ('Erstattung wirklich entfernen? Die von der App dafür angelegten Buchungssätze werden '
                                       'mit entfernt; das Guthaben des Kunden steht danach wieder aus.')
    assert f'invoice_id: {s1},' in script and '__' not in script


# ── Rechnungsliste und offene Posten ─────────────────────────────────────────

def test_the_list_follows_the_remaining_credit(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    cells, _html = _list_cells(world)
    assert 'Erstattung offen: 50,00 €' in _plain(cells['26R001'][5])
    refund(world, s1, outgoing(world, 20.0), 20.0)
    cells, html = _list_cells(world)
    assert 'Erstattung offen: 30,00 €' in _plain(cells['26R001'][5]) and '-119,00 €' in _plain(cells['26R001'][4])
    refund(world, s1, outgoing(world, 30.0, '2026-07-12'))
    cells, html = _list_cells(world)
    assert _plain(cells['26R001'][5]) == 'Stornorechnung' and 'Erstattung offen' not in html
    assert _plain(cells['T-100'][5]) == 'Storniert'
    db.unassign_invoice_payment(db.get_invoice_payments(s1)[1][0])                               # eine Erstattung entfernen
    assert 'Erstattung offen: 30,00 €' in _plain(_list_cells(world)[0]['26R001'][5])


def test_the_open_items_of_the_contact_show_only_the_remaining_credit(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    open_invoice = sent(world, number='T-101')                                                  # 119,00 € offen
    assert db.get_open_items_for_contact(world.cust)['saldo'] == 69                              # 119 − 50

    refund(world, s1, outgoing(world, 20.0), 20.0)
    items = db.get_open_items_for_contact(world.cust)
    assert items['storno_credits'] == [(s1, '26R001', '2026-07-02', r1, 'T-100', 30)] and items['saldo'] == 89
    assert [r[0] for r in items['open_invoices']] == [open_invoice] and items['credits'] == []   # der Ausgang ist kein Guthaben
    html = PageContacts(db, edit_contact_id=world.cust)
    assert '<h4>Guthaben aus stornierten Rechnungen</h4>' in html and '30,00 €' in html

    refund(world, s1, outgoing(world, 30.0, '2026-07-12'))
    items = db.get_open_items_for_contact(world.cust)
    assert items['storno_credits'] == [] and items['saldo'] == 119
    assert 'Guthaben aus stornierten Rechnungen</h4>' not in PageContacts(db, edit_contact_id=world.cust)


# ── Die Skripte der Seite ────────────────────────────────────────────────────

def _pages_with_a_box(world):
    db = world.db
    r1, s1, _paid = paid_and_cancelled(world)
    outgoing(world, 50.0, '2026-07-10')
    pages = [PageInvoice(db, {}, s1)]                                                          # Guthaben, Bewegungen
    refund(world, s1, outgoing(world, 20.0, '2026-07-11'), 20.0)
    pages.append(PageInvoice(db, {}, s1))                                                      # Teil-Erstattung
    refund(world, s1, outgoing(world, 30.0, '2026-07-12'))
    pages.append(PageInvoice(db, {}, s1))                                                      # ganz erstattet
    imported = make_invoice(world, 'sent', number='T-201', items=SIXTEEN, taxRate=0.16)
    add_payment(world, imported, 116.0)
    pages.append(PageInvoice(db, {}, cancel(world, imported, '2026-07-02').storno_id))        # mit Auswahl des Erlöskontos
    return pages


@NO_NODE
def test_every_script_block_of_the_refund_pages_parses(world, tmp_path):
    import subprocess
    for number, html in enumerate(_pages_with_a_box(world)):
        assert box_of(html) is not None
        for index, block in enumerate(Dom(html).scripts):
            source = tmp_path / f'page{number}_{index}.js'
            source.write_text(block.split('</script>')[0], encoding='utf-8')
            done = subprocess.run([NODE, '--check', str(source)], capture_output=True, text=True, timeout=60)
            assert done.returncode == 0, f'Seite {number}, Block {index}: {done.stderr}'


REFUND_RUNNER = r"""
const fs = require('fs'), vm = require('vm');
const scenario = JSON.parse(process.argv[3]);
const log = {requests: [], messages: [], asked: [], reloads: 0, disabled: []};
const button = {};
Object.defineProperty(button, 'disabled', {set(v) { log.disabled.push(v); }, get() { return false; }});
const sandbox = {JSON, parseInt, parseFloat, Promise, button,
  document: {getElementById: id => (id in scenario.fields) ? {value: scenario.fields[id]} : null},
  location: {reload() { log.reloads += 1; }},
  fetch: (url, init) => { log.requests.push({url, init});
    return scenario.fail ? Promise.reject(new Error('Netz weg'))
                         : Promise.resolve({json: () => Promise.resolve(scenario.answer)}); },
  showMessage: (text, type) => log.messages.push([text, type]),
  showConfirm: (text, onOk) => { log.asked.push(text); onOk(); }};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8') + '\n' + scenario.call + ';', sandbox);
setTimeout(() => console.log(JSON.stringify(log)), 50);
"""


def _run(world, tmp_path, call, fields=None, answer=None, fail=False, number='T-100'):
    _r1, s1, _paid = paid_and_cancelled(world, number=number)
    outgoing(world, 50.0, '2026-07-10')
    refund(world, s1, outgoing(world, 5.0, '2026-07-11'), 5.0)                                 # damit deleteRefund da ist
    script = next(s for s in Dom(PageInvoice(world.db, {}, s1)).scripts if 'function linkRefund(' in s)
    return s1, _node(tmp_path, script, REFUND_RUNNER, {'call': call, 'fields': fields or {}, 'answer': answer, 'fail': fail})


@NO_NODE
def test_the_link_script_posts_the_amount_and_reloads(world, tmp_path):
    s1, log = _run(world, tmp_path, 'linkRefund(7, button)', {'refundAmount_7': '20.00'}, {'success': True})
    (request,) = log['requests']
    assert request['url'] == '/invoice/link-payment' and request['init']['method'] == 'POST'
    assert json.loads(request['init']['body']) == {'invoice_id': s1, 'transaction_id': 7, 'amount_paid': 20}
    assert log['reloads'] == 1 and log['disabled'] == [True] and log['messages'] == []


@NO_NODE
def test_the_link_script_sends_the_chosen_revenue_account(world, tmp_path):
    s1, log = _run(world, tmp_path, 'linkRefund(7, button)', {'refundAmount_7': '12.5', 'revenueCoa': '33'}, {'success': True})
    assert json.loads(log['requests'][0]['init']['body']) == {
        'invoice_id': s1, 'transaction_id': 7, 'amount_paid': 12.5, 'revenue_coa_id': 33}


@NO_NODE
@pytest.mark.parametrize('fields, message', [
    ({'refundAmount_7': '0'}, 'Betrag muss größer 0 sein.'), ({'refundAmount_7': ''}, 'Betrag muss größer 0 sein.'),
    ({'refundAmount_7': '-3'}, 'Betrag muss größer 0 sein.'),
    ({'refundAmount_7': '10', 'revenueCoa': ''}, 'Bitte ein Erlöskonto wählen.')],
    ids=['null', 'leer', 'negativ', 'ohne-Erlöskonto'])
def test_the_link_script_stops_before_asking_the_server_when_the_input_is_unusable(world, tmp_path, fields, message):
    _s1, log = _run(world, tmp_path, 'linkRefund(7, button)', fields, {'success': True})
    assert log['requests'] == [] and log['messages'] == [[message, 'error']] and log['disabled'] == []


@NO_NODE
def test_the_link_script_shows_a_refusal_and_enables_the_button_again(world, tmp_path):
    _s1, log = _run(world, tmp_path, 'linkRefund(7, button)', {'refundAmount_7': '20'},
                    {'success': False, 'error': REFUND_NO_CREDIT_MESSAGE})
    assert log['messages'] == [[f'Fehler: {REFUND_NO_CREDIT_MESSAGE}', 'error']] and log['disabled'] == [True, False]
    assert log['reloads'] == 0
    _s1, log = _run(world, tmp_path, 'linkRefund(7, button)', {'refundAmount_7': '20'}, fail=True, number='T-101')
    assert log['messages'] == [['Fehler: Die Erstattung konnte nicht zugeordnet werden.', 'error']]
    assert log['disabled'] == [True, False]


@NO_NODE
def test_the_delete_script_asks_posts_and_shows_the_refusal(world, tmp_path):
    _s1, log = _run(world, tmp_path, 'deleteRefund(5)', {}, {'success': True})
    assert log['asked'] == [REFUND_CONFIRM_QUESTION]
    (request,) = log['requests']
    assert request['url'] == '/invoice/delete-payment' and json.loads(request['init']['body']) == {'payment_id': 5}
    assert log['reloads'] == 1
    _s1, log = _run(world, tmp_path, 'deleteRefund(5)', {}, {'success': False, 'error': ALREADY_REFUNDED_MESSAGE},
                    number='T-101')
    assert log['reloads'] == 0 and log['messages'] == [[f'Fehler: {ALREADY_REFUNDED_MESSAGE}', 'error']]


# ── Über den Server: dieselben Routen, dieselben Prüfungen ───────────────────

def _http_pair(port, w):
    """Eine versendete Rechnung über den Server, 50,00 € gezahlt, storniert; liefert (R1, S1, Zahlung der Bewegung)."""
    invoice_id = sent_over_http(port, w)
    booking = w.db.insert_booking('2026-07-01', 50.0, booking_type='bank', account_id=w.bank, recipient_client=CUSTOMER_TEXT)
    assert handlers.link_booking_to_invoice_capped(w.db, invoice_id, booking, 50.0) == (True, None)
    body = json.loads(post_json(port, '/invoice/cancel', {'invoiceId': invoice_id, 'date': '2026-07-02'}, w.csrf).text)
    return invoice_id, body['storno_id'], booking


def test_over_http_the_link_route_refunds_a_credit_note_and_the_delete_route_takes_it_back(server_port, books):
    db = books.db
    r1, s1, _paid = _http_pair(server_port, books)
    bank = outgoing(books, 50.0, '2026-07-10')

    resp = post_json(server_port, '/invoice/link-payment', {'invoice_id': s1, 'transaction_id': bank, 'amount_paid': 20.0},
                     books.csrf)

    assert resp.status == 200 and json.loads(resp.text) == {'success': True}
    assert allocations(db, s1) == [(bank, -200000, '2026-07-10')] and db.get_storno_credit(r1) == 30
    assert [r[2:4] for r in payment_rows(db, '26R001')] == [(-200000, None), (-200000, -31900)]
    assert query_rows(db, 'SELECT Contact_ID FROM Bookings WHERE ID = ?', bank) == [{'Contact_ID': books.cust}]
    page = get(server_port, f'/invoice?id={s1}').text
    assert 'id="refund_box"' in page and 'Guthaben des Kunden: 30,00 €' in page and 'deleteRefund(' in page
    payment_id = db.get_invoice_payments(s1)[0][0]

    # die Zahlung lässt sich nicht entfernen, solange erstattet ist
    blocked = post_json(server_port, '/invoice/delete-payment', {'payment_id': db.get_invoice_payments(r1)[0][0]}, books.csrf)
    assert blocked.status == 409 and json.loads(blocked.text) == {'success': False, 'error': ALREADY_REFUNDED_MESSAGE}
    assert len(db.get_invoice_payments(r1)) == 1

    resp = post_json(server_port, '/invoice/delete-payment', {'payment_id': payment_id}, books.csrf)
    assert resp.status == 200 and json.loads(resp.text) == {'success': True}
    assert db.get_invoice_payments(s1) == [] and db.get_storno_credit(r1) == 50


def test_over_http_a_refused_refund_is_400_with_the_text_and_writes_nothing(server_port, books):
    db = books.db
    r1, s1, paid = _http_pair(server_port, books)
    bank = outgoing(books, 50.0, '2026-07-10')
    other = sent_over_http(server_port, books, number='T-200')
    before = tables(db)
    for payload, error in (
            ({'invoice_id': s1, 'transaction_id': paid}, REFUND_ONLY_OUTGOING_MESSAGE),
            ({'invoice_id': s1, 'transaction_id': 99999}, 'Buchung nicht gefunden'),
            ({'invoice_id': s1, 'transaction_id': bank, 'amount_paid': 0}, 'Der Betrag muss größer 0 sein.'),
            ({'invoice_id': s1, 'transaction_id': bank, 'amount_paid': -4}, 'Der Betrag muss größer 0 sein.')):
        resp = post_json(server_port, '/invoice/link-payment', payload, books.csrf)
        assert resp.status == 400 and json.loads(resp.text) == {'success': False, 'error': error}, payload
    assert tables(db) == before
    # eine gewöhnliche Rechnung nimmt keinen Ausgang an – die Route für Zahlungen bleibt, wie sie war
    resp = post_json(server_port, '/invoice/link-payment', {'invoice_id': other, 'transaction_id': bank}, books.csrf)
    assert resp.status == 400 and json.loads(resp.text)['error'] == 'Nur Zahlungseingänge lassen sich einer Rechnung zuordnen.'
    assert tables(db) == before


def test_over_http_the_checks_of_the_route_are_the_same_for_a_refund(server_port, books):
    db = books.db
    _r1, s1, _paid = _http_pair(server_port, books)
    bank = outgoing(books, 50.0, '2026-07-10')
    quote = db.insert_invoice({'invoice_number': '26A001', 'invoice_date': '2026-06-09', 'seller_name': 'S',
                               'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG',
                               'tax_rate': 0.19, 'sum_net': 100, 'tax_amount': 19, 'sum_gross': 119,
                               'amount_due': 119, 'status': 'sent', 'document_type': 'quote'})
    before = tables(db)
    body = {'invoice_id': s1, 'transaction_id': bank, 'amount_paid': 10.0}
    assert get(server_port, '/invoice/link-payment').status in (404, 405)                           # nie per GET
    assert post_json(server_port, '/invoice/link-payment', body, books.csrf, headers={'Origin': FOREIGN_ORIGIN}).status == 403
    assert post_json(server_port, '/invoice/link-payment', body, 'falsch').status == 403            # CSRF-Token
    assert post_json(server_port, '/invoice/link-payment', b'{kaputt', books.csrf).status == 400
    assert post_json(server_port, '/invoice/link-payment', {'invoice_id': 'x', 'transaction_id': bank}, books.csrf).status == 400
    resp = post_json(server_port, '/invoice/link-payment', {'invoice_id': quote, 'transaction_id': bank}, books.csrf)
    assert resp.status == 400 and json.loads(resp.text)['error'] == 'Rechnung nicht gefunden'     # der Belegtyp zählt
    assert tables(db) == before
    ok = post_json(server_port, '/invoice/link-payment', body, books.csrf)
    assert ok.status == 200 and len(db.get_invoice_payments(s1)) == 1
