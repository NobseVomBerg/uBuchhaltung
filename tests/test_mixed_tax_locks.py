# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die eine Sperre, die für einen Beleg mit Steuersatz je Position in dieser Version bleibt (2.7.0), und die Ein-Satz-Gegenstücke der Wege, die inzwischen
je Satz buchen.

Versenden, Wiederbeleben und Zahlung zuordnen (Etappe 2a: ``test_mixed_tax_bookings.py``) sowie Stornieren, Erstattung, Neuaufbau und der Umzug der Zahlungen
(Etappe 2b: ``test_mixed_tax_storno.py``) buchen je Satz oder rechnen, wie der Neuaufbau, nur den offenen Betrag. Abgelehnt wird allein
``link_invoice_to_transaction`` (der WISO-Import): Der Weg schreibt eine Zuordnung ohne Buchungssätze, deren Anteil je Satz keine spätere Zahlung, Erstattung
oder Prüfung kennen könnte – er lehnt so einen Beleg mit EINEM festen Satz ab (``MIXED_LINK_NOT_BOOKABLE``), vor dem ersten Schreiben, am frisch in der
Transaktion gelesenen Beleg: nichts geändert. Der Satz im Kopf ist bei so einem Beleg nur ein Platzhalter (der höchste Satz), und jede Buchung, die ihn
nähme, buchte falsch. Dazu stehen hier die Ein-Satz-Gegenstücke der Wege: Sie gehen wie vorher.

Ein Beleg mit dem Kennzeichen entsteht im Formular nur als Entwurf (``taxMode``); die übrigen Zustände – versendet, mit Zahlung, storniert –
stellen die Tests her, wie ein Eingriff über die SQL-Konsole sie hinterließe: Eine normale Rechnung wird gebaut und danach mit
``MixedTaxRates = 1`` gekennzeichnet. ``update_invoice_status`` (der ungeprüfte Baustein für Seed und Tests) bleibt, wie er ist, und ``create_replacement_draft``
bucht nichts und bleibt erlaubt.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import pytest

from db.invoices import MIXED_LINK_NOT_BOOKABLE
from db.invoice_payments import PAYMENT_CHANGED_MESSAGE, PaymentAssignment
from tests.invoice_world import columns, make_invoice, query_rows, run_sql
from tests.test_invoice_reissue import allocations, paid_scenario, payment_rows, reissue
from tests.test_invoice_storno import ONE_ITEM, USER, cancel, pay, sent, tables, world   # noqa: F401  (``world`` ist die Fixture)

SENTENCE = 'Einer Rechnung mit mehreren Steuersätzen lässt sich eine Zahlung nur mit ihren Buchungssätzen zuordnen.'
MIXED_ITEMS = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung', 'unitPrice': 100.0, 'taxRate': 19},
               {'position': 2, 'quantity': 1, 'unit': 'C62', 'description': 'Fachbuch', 'unitPrice': 50.0, 'taxRate': 7}]


def flag(db, *invoice_ids):
    """Die Rechnungen als „Steuersatz je Position“ kennzeichnen, wie ein Eingriff an der App vorbei es täte."""
    for invoice_id in invoice_ids:
        run_sql(db, 'UPDATE Invoices SET MixedTaxRates = 1 WHERE ID = ?', invoice_id)


def drop_accounts(db, *numbers):
    """Konten aus dem Kontenrahmen nehmen (solange nichts darauf bucht): Legte ein Weg sie neu an, zeigte es der Bestand."""
    for number in numbers:
        run_sql(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = ?', number)


def mixed_draft(w, number='M-100'):
    """Ein Entwurf mit 19 % und 7 %, wie das Formular ihn speichert."""
    invoice_id = make_invoice(w, 'draft', number=number, items=MIXED_ITEMS, taxMode='mixed')
    assert columns(w.db, invoice_id)['MixedTaxRates'] == 1
    return invoice_id


def incoming(w, amount=119.0, day='2026-07-01'):
    return w.db.insert_booking(day, amount, booking_type='bank', account_id=w.bank, recipient_client='Malermeister Pinsel')


def outgoing(w, amount=-50.0, day='2026-07-10'):
    return w.db.insert_booking(day, amount, booking_type='bank', account_id=w.bank, recipient_client='Malermeister Pinsel')


def chart(db):
    return query_rows(db, 'SELECT AccountNumber FROM ChartOfAccounts ORDER BY ID')


def test_the_sentence_of_the_one_refused_way_is_the_one_of_the_plan():
    assert MIXED_LINK_NOT_BOOKABLE == SENTENCE


# ═════════════════════════════════════════════════════════════════════════════
# Versenden und Wiederbeleben
# ═════════════════════════════════════════════════════════════════════════════

def test_the_same_path_with_one_rate_sends_and_books_the_receivable_as_before(world):
    invoice_id = make_invoice(world, 'draft', number='T-100', items=ONE_ITEM)
    drop_accounts(world.db, 10000, 4405)
    before = chart(world.db)

    change = world.db.set_invoice_status(invoice_id, 'sent', USER)

    assert (change.ok, change.code, change.message) == (True, 200, None)
    assert columns(world.db, invoice_id)['Status'] == 'sent' and chart(world.db) != before       # die Konten entstehen


def test_the_free_changes_between_draft_and_finalized_stay_allowed_for_a_mixed_invoice(world):
    invoice_id = mixed_draft(world)

    assert world.db.set_invoice_status(invoice_id, 'finalized', USER).ok
    assert world.db.set_invoice_status(invoice_id, 'draft', USER).ok
    assert columns(world.db, invoice_id)['MixedTaxRates'] == 1


def test_the_unchecked_building_block_for_seeds_and_tests_stays_as_it_is(world):
    """``update_invoice_status`` setzt den Status ohne Prüfung und bucht nichts – so bleibt es, auch für einen gekennzeichneten Beleg."""
    invoice_id = mixed_draft(world)
    before_bookings = query_rows(world.db, 'SELECT * FROM Bookings ORDER BY ID')

    world.db.update_invoice_status(invoice_id, 'sent')

    stored = columns(world.db, invoice_id)
    assert stored['Status'] == 'sent' and stored['SentAt'] is not None and stored['MixedTaxRates'] == 1
    assert query_rows(world.db, 'SELECT * FROM Bookings ORDER BY ID') == before_bookings


# ═════════════════════════════════════════════════════════════════════════════
# Die Forderung außerhalb des Statuswechsels
# ═════════════════════════════════════════════════════════════════════════════

def test_ensure_receivable_booking_books_the_receivable_of_an_invoice_with_one_rate_as_before(world):
    invoice_id = make_invoice(world, 'draft', number='T-100', items=ONE_ITEM)

    booking_id = world.db.ensure_receivable_booking(invoice_id)

    assert booking_id is not None and world.db.ensure_receivable_booking(invoice_id) == booking_id     # idempotent


# ═════════════════════════════════════════════════════════════════════════════
# Zahlung zuordnen
# ═════════════════════════════════════════════════════════════════════════════

def test_the_same_payment_on_an_invoice_with_one_rate_is_assigned_as_before(world):
    invoice_id = sent(world, 'T-100')
    booking = incoming(world)

    assignment = world.db.assign_invoice_payment(invoice_id, booking)

    assert assignment.ok and assignment.message is None
    assert [(r[0], r[1]) for r in payment_rows(world.db, 'T-100')] == [(1800, 10000), (4405, 4400)]


def test_the_precheck_of_an_invoice_with_one_rate_finds_nothing_to_say(world):
    invoice_id = sent(world, 'T-100')

    assert world.db.payment_precheck(invoice_id, world.bank) is None


def test_the_building_block_in_the_transaction_reads_the_stored_flag_itself(world):
    """Die Vorprüfung (``_payment_prerequisites``) läuft vor der Transaktion; der Baustein in der Transaktion liest den Beleg frisch: Wird er
    danach gekennzeichnet, passt der Kontext einer Rechnung mit einem Satz nicht mehr – er lehnt mit dem „geändert“-Satz ab, nichts geschrieben.
    (Der Weg des gekennzeichneten Belegs selbst: ``test_mixed_tax_bookings.py``.)"""
    invoice_id = sent(world, 'T-100')
    booking = incoming(world)
    error, context = world.db._payment_prerequisites(invoice_id, world.bank)
    assert error is None
    flag(world.db, invoice_id)
    before = tables(world.db)

    with world.db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        result = world.db._assign_in_tx(tx, invoice_id, booking, None, context)

    assert result == PaymentAssignment(False, PAYMENT_CHANGED_MESSAGE, None, None) and tables(world.db) == before


def test_linking_without_booking_sets_is_refused_for_a_mixed_invoice(world):
    """Der WISO-Import ordnet zu, ohne Sätze zu schreiben (``link_invoice_to_transaction``): auch das nimmt der Beleg nicht an – der eine Weg, der in
    dieser Version noch ablehnt, mit seinem eigenen Satz."""
    invoice_id = sent(world, 'T-100')
    booking = incoming(world)
    flag(world.db, invoice_id)
    before = tables(world.db)

    with pytest.raises(ValueError) as refused:
        world.db.link_invoice_to_transaction(invoice_id, booking, 119.0)

    assert str(refused.value) == SENTENCE and tables(world.db) == before


def test_linking_without_booking_sets_works_for_an_invoice_with_one_rate_as_before(world):
    invoice_id = sent(world, 'T-100')
    booking = incoming(world)

    world.db.link_invoice_to_transaction(invoice_id, booking, 119.0)

    assert [row[1] for row in allocations(world.db, invoice_id)] == [1190000]


# ═════════════════════════════════════════════════════════════════════════════
# Stornieren, Neu ausstellen, Erstattung, Neuaufbau, Umzug: die Gegenstücke mit EINEM Satz
# (der Beleg mit Steuersatz je Position: ``test_mixed_tax_storno.py``)
# ═════════════════════════════════════════════════════════════════════════════

def test_the_cancellation_of_an_invoice_with_one_rate_works_as_before(world):
    invoice_id = sent(world, 'T-100')

    storno = world.db.cancel_invoice(invoice_id, '2026-07-01', USER)

    assert storno.ok and storno.counter_booked is True


def test_a_replacement_draft_stays_allowed_because_it_books_nothing(world, monkeypatch):
    original = sent(world, 'T-100')
    cancel(world, original, '2026-07-02')
    credit = world.db.find_storno_invoice(original)[0]
    flag(world.db, original, credit)                                     # ein Eingriff, nachdem die App storniert hat
    bookings_before = query_rows(world.db, 'SELECT * FROM Bookings ORDER BY ID')

    replacement = reissue(world.db, original, monkeypatch)

    assert columns(world.db, replacement)['MixedTaxRates'] == 1 and columns(world.db, replacement)['Status'] == 'draft'
    assert query_rows(world.db, 'SELECT * FROM Bookings ORDER BY ID') == bookings_before


def test_the_same_refund_with_one_rate_is_assigned_as_before(world, monkeypatch):
    s = paid_scenario(world, monkeypatch, paid=50.0)
    refund = outgoing(world)

    assignment = world.db.assign_storno_refund(s.s1, refund)

    assert assignment.ok and assignment.message is None


def test_the_dry_run_of_an_invoice_with_one_rate_works_as_before(world):
    invoice_id = sent(world, 'T-100')
    pay(world, invoice_id, 50.0)

    result = world.db.rebuild_invoice_payments(invoice_id)

    assert result.ok and result.message is None and result.plan is not None


def test_the_same_scenario_with_one_rate_moves_the_payment_as_before(world, monkeypatch):
    s = paid_scenario(world, monkeypatch, paid=50.0)

    change = world.db.set_invoice_status(s.r2, 'sent', USER)

    assert change.ok and change.moved == 500000 and 'mehrere Steuersätze' not in (change.payment_hint or '')
    assert allocations(world.db, s.r1) == [] and [row[1] for row in allocations(world.db, s.r2)] == [500000]
