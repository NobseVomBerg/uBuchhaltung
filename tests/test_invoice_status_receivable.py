# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Status und Forderungsbuchung gehören zusammen (2.1.2).

* **Eine Transaktion:** ``Database.set_invoice_status`` prüft, setzt den Status und bucht bzw. entfernt die
  Forderung („Debitoren an Wartekonto“) – gelingt eines nicht, ändert sich nichts. Die Schreibsperre
  liegt von der Prüfung bis zum Commit; kein zweiter Schreiber kommt dazwischen.
* **Nie ein Satz mit Quellklammer:** Die App entfernt nur die Forderung, die sie selbst gebucht hat (ohne
  ``SourceGroup``, Wartekonto als Gegenkonto, keiner Bankbewegung zugeordnet) – beim Zurückholen, beim Storno
  von Hand (nur eine Rechnung ohne positiven Betrag: Gutschrift, 0-€-Rechnung) und beim Entfernen der letzten
  Zahlung einer von Hand stornierten Rechnung (Altbestand ohne Stornorechnung). Das Zurückholen einer Rechnung
  mit anderer Forderung wird mit Klartext abgelehnt, nichts ändert sich. Eine Stornorechnung
  (``cancel_invoice``) löscht nie eine Forderung (test_invoice_storno.py).

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import contextlib
import os
import sqlite3

import pytest

import server.handlers as handlers
from db.invoice_lock import (REOPEN_ATTACHED_MESSAGE, REOPEN_FOREIGN_MESSAGE, REOPEN_IMPORTED_MESSAGE,
                             REOPEN_PAYMENT_MESSAGE)
from tests.document_helpers import read_file
from tests.invoice_world import (add_payment, build_world, columns, force_status, make_invoice, make_payload,
                                 save, set_invoice_status, snapshot)


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return build_world(tmp_db)


def sql(w, statement, *args):
    with contextlib.closing(sqlite3.connect(w.db.db_name)) as con:
        cursor = con.execute(statement, args)
        con.commit()
        return cursor.fetchall()


def receivables(w, invoice_id):
    """Die Forderungsbuchungen der Rechnung: (ID, Elternbuchung, Quellklammer, Gegenkonto-Nr.)."""
    number = columns(w.db, invoice_id)['InvoiceNumber']
    return sql(w, "SELECT b.ID, b.ParentBooking_ID, b.SourceGroup, c.AccountNumber FROM Bookings b "
                  "LEFT JOIN ChartOfAccounts c ON c.ID = b.CounterCOA_ID "
                  "WHERE b.DocumentNumber = ? AND b.COA_ID = (SELECT ID FROM ChartOfAccounts "
                  "WHERE AccountNumber = 10000) ORDER BY b.ID", number)


def send(w, number='26R100', **over):
    invoice_id = make_invoice(w, 'draft', number=number, **over)
    code, body = set_invoice_status(invoice_id, 'sent')
    assert (code, body['success']) == (200, True), body
    return invoice_id


def mark_imported(w, booking_id):
    sql(w, "UPDATE Bookings SET SourceGroup = 'WISO-TEST-1' WHERE ID = ?", booking_id)


def hand_booked(w, booking_id):
    """Die Forderung wurde von Hand gebucht: ein Erlöskonto statt des Wartekontos als Gegenkonto."""
    sql(w, "UPDATE Bookings SET CounterCOA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4400) "
           "WHERE ID = ?", booking_id)


def attach_to_bank(w, booking_id):
    bank = w.db.insert_booking('2026-07-01', 298.10, booking_type='bank', account_id=w.bank)
    sql(w, 'UPDATE Bookings SET ParentBooking_ID = ? WHERE ID = ?', bank, booking_id)
    return bank


def refused(w, invoice_id, status='draft'):
    before = snapshot(w.db)
    code, body = set_invoice_status(invoice_id, status)
    assert snapshot(w.db) == before                       # nichts hat sich geändert
    return code, body


# ── Zurückholen: nur die Forderung der App ───────────────────────────────────

def test_reopening_removes_the_receivable_the_app_booked(world):
    invoice_id = send(world)
    assert len(receivables(world, invoice_id)) == 1
    assert set_invoice_status(invoice_id, 'draft')[0] == 200
    assert receivables(world, invoice_id) == []
    assert columns(world.db, invoice_id)['Status'] == 'draft'


def test_reopening_refuses_an_imported_receivable(world):
    invoice_id = send(world)
    pdf = columns(world.db, invoice_id)['PDFPath']
    mark_imported(world, receivables(world, invoice_id)[0][0])
    pdf_bytes = read_file(pdf)
    code, body = refused(world, invoice_id)
    assert (code, body['success'], body['error']) == (409, False, REOPEN_IMPORTED_MESSAGE)
    assert columns(world.db, invoice_id)['Status'] == 'sent' and len(receivables(world, invoice_id)) == 1
    assert read_file(pdf) == pdf_bytes and columns(world.db, invoice_id)['PDFPath'] == pdf    # auch das Dokument bleibt


def test_reopening_to_finalized_refuses_the_same_way(world):
    invoice_id = send(world)
    mark_imported(world, receivables(world, invoice_id)[0][0])
    assert refused(world, invoice_id, 'finalized')[1]['error'] == REOPEN_IMPORTED_MESSAGE


def test_reopening_refuses_a_receivable_attached_to_a_bank_movement(world):
    invoice_id = send(world)
    attach_to_bank(world, receivables(world, invoice_id)[0][0])
    code, body = refused(world, invoice_id)
    assert (code, body['error']) == (409, REOPEN_ATTACHED_MESSAGE)


def test_reopening_refuses_a_receivable_the_user_booked_by_hand(world):
    invoice_id = send(world)
    hand_booked(world, receivables(world, invoice_id)[0][0])
    code, body = refused(world, invoice_id)
    assert (code, body['error']) == (409, REOPEN_FOREIGN_MESSAGE)


def test_reopening_refuses_when_payment_bookings_are_left_over(world):
    """Keine Zuordnung mehr, aber noch ein markierter Zahlungssatz der Rechnung (Rest eines Altbestands)."""
    invoice_id = send(world)
    leftover = world.db.insert_booking('2026-07-01', 100.0, booking_type='entry',
                                       document_number='26R100', text='Zahlung')
    sql(world, "UPDATE Bookings SET Status = 'invoice_payment' WHERE ID = ?", leftover)
    code, body = refused(world, invoice_id)
    assert (code, body['error']) == (409, REOPEN_PAYMENT_MESSAGE)


def test_an_imported_receivable_blocks_even_next_to_one_the_app_booked(world):
    invoice_id = send(world)
    app_receivable = receivables(world, invoice_id)[0][0]
    imported = world.db.insert_booking('2026-06-07', 298.10, booking_type='entry', document_number='26R100',
                                       coa_id=world.db.get_coa_id_by_account_number(10000),
                                       counter_coa_id=world.db.get_coa_id_by_account_number(4405),
                                       entry_form='transfer')
    mark_imported(world, imported)
    code, body = refused(world, invoice_id)
    assert (code, body['error']) == (409, REOPEN_IMPORTED_MESSAGE)
    assert {r[0] for r in receivables(world, invoice_id)} == {app_receivable, imported}


def test_an_invoice_without_any_receivable_can_be_reopened(world):
    """Steuersatz ohne Wartekonto (5 %): Beim Versenden wird nichts gebucht, also hängt nichts am Zurückholen."""
    items = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Buch', 'unitPrice': 40.0,
              'taxRate': 5}]
    invoice_id = send(world, taxRate=0.05, items=items)
    assert receivables(world, invoice_id) == []
    assert set_invoice_status(invoice_id, 'draft')[0] == 200


# ── Storno und letzte Zahlung: nie ein Satz mit Quellklammer ──────────────────────

def test_the_status_does_not_cancel_and_removes_no_receivable(world):
    """Auf „storniert“ setzt der Status nicht mehr (409): nichts wird geändert, die Forderung bleibt."""
    invoice_id = send(world)
    code, body = refused(world, invoice_id, 'cancelled')
    assert (code, body['success']) == (409, False)
    assert columns(world.db, invoice_id)['Status'] == 'sent' and len(receivables(world, invoice_id)) == 1


CREDIT = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Gutschrift', 'unitPrice': -100.0,
           'taxRate': 19}]                                               # eine Rechnung über −119,00 brutto


def send_credit(w, number='G-100'):
    """Eine versendete Rechnung ohne positiven Betrag (Gutschrift): Sie lässt sich über den Status stornieren."""
    return send(w, number=number, items=CREDIT)


def test_cancelling_an_invoice_without_a_positive_amount_by_hand_removes_the_app_receivable(world):
    invoice_id = send_credit(world)
    assert len(receivables(world, invoice_id)) == 1
    assert set_invoice_status(invoice_id, 'cancelled')[0] == 200
    assert receivables(world, invoice_id) == [] and columns(world.db, invoice_id)['Status'] == 'cancelled'


def test_cancelling_by_hand_never_deletes_an_imported_receivable(world):
    invoice_id = send_credit(world)
    booking = receivables(world, invoice_id)[0][0]
    mark_imported(world, booking)
    assert set_invoice_status(invoice_id, 'cancelled')[0] == 200
    assert columns(world.db, invoice_id)['Status'] == 'cancelled'
    assert [r[0] for r in receivables(world, invoice_id)] == [booking]


def test_cancelling_by_hand_never_deletes_a_receivable_booked_by_hand_or_attached(world):
    first, second = send_credit(world, 'G-101'), send_credit(world, 'G-102')
    hand_booked(world, receivables(world, first)[0][0])
    attach_to_bank(world, receivables(world, second)[0][0])
    assert set_invoice_status(first, 'cancelled')[0] == 200
    assert set_invoice_status(second, 'cancelled')[0] == 200
    assert len(receivables(world, first)) == 1 and len(receivables(world, second)) == 1


def test_cancelling_by_hand_keeps_the_receivable_while_a_payment_hangs_on_it(world):
    invoice_id = send_credit(world)
    add_payment(world, invoice_id, -119.0)                               # wie der WISO-Import sie zuordnet
    assert set_invoice_status(invoice_id, 'cancelled')[0] == 200
    assert len(receivables(world, invoice_id)) == 1


def test_a_failing_cancel_removal_leaves_the_status(world, monkeypatch):
    invoice_id = send_credit(world)
    before = snapshot(world.db)

    def broken(self, conn, invoice_id, number):
        raise RuntimeError('Löschen nicht möglich')
    monkeypatch.setattr(type(world.db), '_drop_receivable', broken)
    with pytest.raises(RuntimeError):
        world.db.set_invoice_status(invoice_id, 'cancelled')
    assert snapshot(world.db) == before and columns(world.db, invoice_id)['Status'] == 'sent'


def test_a_credit_note_keeps_every_kind_of_receivable(world):
    """Das Stornieren entfernt nie eine Forderung – die der App nicht (``test_invoice_storno.py`` zeigt die Gegenbuchung),
    eine importierte, von Hand gebuchte oder an eine Bankbewegung gehängte erst recht nicht."""
    for number, change in (('T-1', None), ('T-2', mark_imported), ('T-3', hand_booked), ('T-4', attach_to_bank)):
        invoice_id = send(world, number=number)        # eigene Nummern: nie die des Kreises, aus dem das Storno zieht
        if change:
            change(world, receivables(world, invoice_id)[0][0])
        before = [r[0] for r in receivables(world, invoice_id)]
        assert world.db.cancel_invoice(invoice_id, '2026-07-01').ok
        assert before[0] in [r[0] for r in receivables(world, invoice_id)], number
        assert columns(world.db, invoice_id)['Status'] == 'cancelled'


def cancelled_with_a_payment(w, mutate=None):
    """Eine von Hand stornierte Rechnung (Altbestand: ohne Stornorechnung) mit einer Zahlung."""
    invoice_id = send(w)
    if mutate:
        mutate(w, receivables(w, invoice_id)[0][0])
    add_payment(w, invoice_id)
    w.db.update_invoice_status(invoice_id, 'cancelled')
    assert len(receivables(w, invoice_id)) == 1                       # mit Zahlung bleibt die Forderung stehen
    return invoice_id


def test_removing_the_last_payment_of_a_cancelled_invoice_takes_the_app_receivable_along(world):
    invoice_id = cancelled_with_a_payment(world)
    world.db.unassign_invoice_payment(world.db.get_invoice_payments(invoice_id)[0][0])
    assert receivables(world, invoice_id) == []                       # die Reihenfolge ändert den Endzustand nicht


@pytest.mark.parametrize('kind, mutate', [('importiert', mark_imported), ('von-Hand-gebucht', hand_booked)])
def test_removing_the_last_payment_never_deletes_a_receivable_that_is_not_the_apps(world, kind, mutate):
    invoice_id = cancelled_with_a_payment(world, mutate)
    world.db.unassign_invoice_payment(world.db.get_invoice_payments(invoice_id)[0][0])
    assert len(receivables(world, invoice_id)) == 1


# ── Eine Transaktion ─────────────────────────────────────────────────────────

def test_the_status_change_itself_books_the_receivable(world):
    invoice_id = make_invoice(world, 'draft', number='26R100')
    assert receivables(world, invoice_id) == []
    change = world.db.set_invoice_status(invoice_id, 'sent')
    assert (change.ok, change.kind) == (True, 'send')
    ((booking_id, parent, group, counter),) = receivables(world, invoice_id)
    assert (parent, group, counter) == (None, None, 4405)             # Debitoren an „Erlöse 19 % USt noch offen“
    amount, tax, form = sql(world, 'SELECT Amount, TaxAmount, EntryForm FROM Bookings WHERE ID = ?',
                            booking_id)[0]
    assert (amount, tax, form) == (columns(world.db, invoice_id)['SumGross'],
                                   columns(world.db, invoice_id)['TaxAmount'], 'transfer')


def test_a_failing_booking_leaves_the_status_as_it_was(world, monkeypatch):
    invoice_id = make_invoice(world, 'finalized', number='26R100')
    before = snapshot(world.db)

    def broken(self, tx, row, accounts):
        raise RuntimeError('Buchung nicht möglich')
    monkeypatch.setattr(type(world.db), '_book_receivable', broken)
    with pytest.raises(RuntimeError):
        world.db.set_invoice_status(invoice_id, 'sent')
    assert snapshot(world.db) == before and columns(world.db, invoice_id)['Status'] == 'finalized'


def test_the_handler_does_not_swallow_a_failing_booking(world, monkeypatch):
    """Früher blieb nach einem Fehler beim Buchen die Rechnung „versendet“, ohne Forderung."""
    invoice_id = make_invoice(world, 'draft', number='26R100')

    def broken(self, tx, row, accounts):
        raise RuntimeError('Buchung nicht möglich')
    monkeypatch.setattr(type(world.db), '_book_receivable', broken)
    with pytest.raises(RuntimeError):
        set_invoice_status(invoice_id, 'sent')
    assert columns(world.db, invoice_id)['Status'] == 'draft'


def test_a_failing_removal_leaves_status_receivable_and_documents(world, monkeypatch):
    invoice_id = send(world)
    pdf = columns(world.db, invoice_id)['PDFPath']
    before = snapshot(world.db)

    def broken(self, conn, invoice_id, number):
        raise RuntimeError('Löschen nicht möglich')
    monkeypatch.setattr(type(world.db), '_drop_receivable', broken)
    with pytest.raises(RuntimeError):
        world.db.set_invoice_status(invoice_id, 'draft')
    assert snapshot(world.db) == before and os.path.isfile(pdf)


def locked_for_others(world):
    """Versucht, von einer zweiten Verbindung zu schreiben: Läuft die Prüfung in der Transaktion, ist die
    Datenbank dafür gesperrt."""
    other = sqlite3.connect(world.db.db_name, timeout=0.1)
    try:
        other.execute('BEGIN IMMEDIATE')
        other.rollback()
        return False
    except sqlite3.OperationalError as exc:
        return 'locked' in str(exc)
    finally:
        other.close()


@pytest.mark.parametrize('step, status, start', [('_reopen_blocker', 'draft', 'sent'),
                                                 ('_book_receivable', 'sent', 'draft')])
def test_the_write_lock_is_held_from_the_check_to_the_commit(world, monkeypatch, step, status, start):
    """Zwischen Prüfung und Schreiben kann niemand eine Zahlung zuordnen oder die Forderung anhängen."""
    invoice_id = send(world) if start == 'sent' else make_invoice(world, 'draft', number='26R100')
    seen = []
    original = getattr(type(world.db), step)

    def watching(self, *args, **kwargs):
        seen.append(locked_for_others(world))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(type(world.db), step, watching)
    assert world.db.set_invoice_status(invoice_id, status).ok is True
    assert seen == [True]


def test_the_write_lock_is_held_from_the_check_to_the_commit_when_cancelling_by_hand(world, monkeypatch):
    """Auch hier (Rechnung ohne positiven Betrag): Zwischen Prüfung des Betrags und Schreiben kommt niemand dazwischen."""
    invoice_id = send_credit(world)
    seen = []
    original = type(world.db)._drop_receivable

    def watching(self, *args, **kwargs):
        seen.append(locked_for_others(world))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(type(world.db), '_drop_receivable', watching)
    assert world.db.set_invoice_status(invoice_id, 'cancelled').ok is True
    assert seen == [True]


def test_a_refused_change_writes_nothing_and_leaves_the_lock_free(world):
    invoice_id = make_invoice(world, 'draft', number='26R100')
    assert set_invoice_status(invoice_id, 'paid')[0] == 409
    assert locked_for_others(world) is False
