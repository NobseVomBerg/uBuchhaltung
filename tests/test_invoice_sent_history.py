# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Versandzeitpunkt, Statusprotokoll und „war schon versendet“ (2.2.0).

* ``Invoices.SentAt`` setzt der Übergang ``send`` einmal (``COALESCE``); ein Import, ``insert_invoice`` und jeder
  andere Status lassen es leer. Der Baustein ``update_invoice_status`` (Seed, Tests) setzt es bei „versendet“.
* ``set_invoice_status`` schreibt je gelungenem Wechsel (außer demselben Status) eine Zeile ins Protokoll
  (``Action='status'``, alt/neu, Benutzer, Fassung) – in derselben Transaktion, also nicht bei einer Ablehnung.
  Was die Zahlungslogik am Status ändert, steht nicht darin.
* ``Database.was_sent_before``: ``SentAt`` oder ein Statuswechsel aus einem versendeten Status im Protokoll oder
  (Bestand aus 2.1.2) eine Datei in ``Fassungen/<ID>``. Folge: Eine versendete Rechnung ohne abgelegte Datei, die
  zurückgeholt wird, gibt ihre Nummer beim Löschen nicht mehr frei; die Nummer ist im zurückgeholten Entwurf
  gesperrt (409). Ein Entwurf ohne Vorgeschichte gibt sie weiter frei und ist umnummerierbar.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import json
import os

import pytest

import server.handlers as handlers
import userctx
from db.invoice_lock import InvoiceLocked
from server.pages_invoice import PageInvoice, delete_question
from tests.document_helpers import write_file
from tests.dom_helpers import Dom
from tests.invoice_world import (add_payment, build_world, columns, document_version, force_status,
                                 make_invoice, make_payload, protocol, query_rows, run_sql, save,
                                 save_raw, set_invoice_status, snapshot)

USER = 'anna'
NUMBER_KEPT_MESSAGE = 'Die Rechnung war bereits versendet – ihre Nummer bleibt.'


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user(USER)
    w = build_world(tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return w


def new_invoice(w, **over):
    """Eine neue Rechnung mit der Nummer, die der Kreis vorschlägt (wie das Formular)."""
    suggestion = w.db.peek_next_number('invoice', 2026)[0]
    resp = save(make_payload(w, invoiceNumber=suggestion, numberSuggestion=suggestion, **over))
    assert resp['success'] is True, resp
    return resp['invoice_id']


def counter(db):
    return db.fetch_number_ranges('invoice')[0][5]


def status_rows(w, invoice_id):
    return [(r['OldValue'], r['NewValue'], r['Version'], r['UserName']) for r in
            protocol(w.db, invoice_id, 'status')]


# ── Versandzeitpunkt ─────────────────────────────────────────────────────────

def test_sending_sets_the_time_once_and_later_changes_keep_it(world):
    invoice_id = new_invoice(world)
    assert columns(world.db, invoice_id)['SentAt'] is None
    assert set_invoice_status(invoice_id, 'finalized')[0] == 200
    assert columns(world.db, invoice_id)['SentAt'] is None                  # Abschließen ist kein Versand
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    sent_at = columns(world.db, invoice_id)['SentAt']
    assert sent_at and len(sent_at) == 19                                    # JJJJ-MM-TT HH:MM:SS (UTC)

    run_sql(world.db, "UPDATE Invoices SET SentAt = '2026-01-15 08:00:00' WHERE ID = ?", invoice_id)
    for status in ('overdue', 'sent', 'draft', 'sent'):
        assert set_invoice_status(invoice_id, status)[0] == 200
        assert columns(world.db, invoice_id)['SentAt'] == '2026-01-15 08:00:00'


def test_an_import_and_the_other_creators_leave_the_time_empty(world):
    imported = make_invoice(world, 'sent')                                   # Status von Hand, wie ein Import
    assert columns(world.db, imported)['SentAt'] is None
    assert set_invoice_status(imported, 'overdue')[0] == 200                 # versendet ↔ überfällig ist kein Versand
    assert set_invoice_status(imported, 'sent')[0] == 200
    assert columns(world.db, imported)['SentAt'] is None
    draft = make_invoice(world, 'draft', number='26R101')
    assert columns(world.db, draft)['SentAt'] is None and columns(world.db, draft)['DocumentVersion'] is None


def test_the_seed_building_block_sets_the_time_only_for_sent(world):
    invoice_id = make_invoice(world, 'draft')
    world.db.update_invoice_status(invoice_id, 'finalized')
    assert columns(world.db, invoice_id)['SentAt'] is None
    world.db.update_invoice_status(invoice_id, 'sent')
    first = columns(world.db, invoice_id)['SentAt']
    assert first
    run_sql(world.db, "UPDATE Invoices SET SentAt = '2026-02-02 10:00:00' WHERE ID = ?", invoice_id)
    world.db.update_invoice_status(invoice_id, 'sent')
    assert columns(world.db, invoice_id)['SentAt'] == '2026-02-02 10:00:00'
    other = make_invoice(world, 'draft', number='26R101')
    world.db.update_invoice_status(other, 'paid')
    assert columns(world.db, other)['SentAt'] is None


# ── Statusprotokoll ──────────────────────────────────────────────────────────

def test_every_successful_change_is_one_protocol_row_and_nothing_else_is(world):
    invoice_id = new_invoice(world)
    assert protocol(world.db, invoice_id) == []
    steps = [('finalized', ('draft', 'finalized')), ('draft', ('finalized', 'draft')),
             ('sent', ('draft', 'sent')), ('overdue', ('sent', 'overdue')), ('sent', ('overdue', 'sent')),
             ('draft', ('sent', 'draft'))]
    expected = []
    for target, (old, new) in steps:
        assert set_invoice_status(invoice_id, target)[0] == 200
        expected.append((old, new))
        assert [(o, n) for o, n, _v, _u in status_rows(world, invoice_id)] == expected
    rows = protocol(world.db, invoice_id)
    assert {r['Action'] for r in rows} == {'status'} and {r['Field'] for r in rows} == {'Status'}
    assert {r['UserName'] for r in rows} == {USER} and {r['InvoiceNumber'] for r in rows} == {'26R001'}
    assert all(r['ChangedAt'] for r in rows)


def test_the_same_status_and_a_refused_change_write_nothing(world):
    invoice_id = new_invoice(world)
    set_invoice_status(invoice_id, 'sent')
    before = snapshot(world.db)
    assert set_invoice_status(invoice_id, 'sent')[0] == 200                   # derselbe Status
    assert set_invoice_status(invoice_id, 'partial_payment')[0] == 409        # nicht erlaubt
    assert set_invoice_status(invoice_id, 'gibt-es-nicht')[0] == 400
    assert snapshot(world.db) == before
    add_payment(world, invoice_id)
    before = snapshot(world.db)
    assert set_invoice_status(invoice_id, 'draft')[0] == 409                  # mit Zahlung kein Zurückholen
    assert snapshot(world.db) == before


def test_the_payment_logic_does_not_write_the_protocol(world):
    invoice_id = new_invoice(world)
    set_invoice_status(invoice_id, 'sent')
    rows = status_rows(world, invoice_id)
    booking = world.db.insert_booking('2026-07-01', 100.0, booking_type='bank', account_id=world.bank)
    ok, err = handlers.link_booking_to_invoice_capped(world.db, invoice_id, booking, 100.0)
    assert ok, err
    assert columns(world.db, invoice_id)['Status'] == 'partial_payment'       # die Zahlungslogik setzt ihn
    assert status_rows(world, invoice_id) == rows                              # … ohne Protokollzeile
    assert set_invoice_status(invoice_id, 'cancelled')[0] == 200               # der Klick dagegen steht drin
    assert status_rows(world, invoice_id)[-1][:2] == ('partial_payment', 'cancelled')


def test_the_protocol_row_carries_the_new_version_when_taking_back_made_one(world):
    with_files = new_invoice(world)
    set_invoice_status(with_files, 'sent')
    assert document_version(world.db, with_files) == 1
    assert set_invoice_status(with_files, 'draft')[0] == 200
    assert document_version(world.db, with_files) == 2
    assert status_rows(world, with_files) == [('draft', 'sent', 1, USER), ('sent', 'draft', 2, USER)]

    imported = make_invoice(world, 'sent', number='26R101')                   # keine Datei: keine neue Fassung
    assert set_invoice_status(imported, 'draft')[0] == 200
    assert document_version(world.db, imported) == 1
    assert status_rows(world, imported) == [('sent', 'draft', 1, USER)]


def test_a_user_free_request_writes_an_empty_user_name(world):
    userctx.clear()
    invoice_id = make_invoice(world, 'draft')
    set_invoice_status(invoice_id, 'finalized')
    assert protocol(world.db, invoice_id)[0]['UserName'] is None


# ── Die Frage „war schon versendet“ ──────────────────────────────────────────

def test_was_sent_before_by_sent_at_by_the_protocol_and_by_the_old_folder(world):
    db = world.db
    never = new_invoice(world)
    assert db.was_sent_before(never) is False
    set_invoice_status(never, 'finalized')
    set_invoice_status(never, 'draft')
    assert db.was_sent_before(never) is False                                  # Abschließen/Entwurf ist kein Versand

    sent = new_invoice(world, customerId=world.cust2)
    set_invoice_status(sent, 'sent')
    assert db.was_sent_before(sent) is True                                    # SentAt

    imported = make_invoice(world, 'sent', number='26R150')
    assert columns(db, imported)['SentAt'] is None
    assert db.was_sent_before(imported) is False                               # ein Import trägt keine Vorgeschichte
    set_invoice_status(imported, 'draft')
    assert db.was_sent_before(imported) is True                                # das Protokoll kennt den Statuswechsel

    legacy = make_invoice(world, 'draft', number='26R151')                     # Bestand aus 2.1.2: nur der Ordner
    assert db.was_sent_before(legacy) is False
    write_file(os.path.join(userctx.DATA_ROOT, 'invoices', '2026', 'Fassungen', str(legacy), 'alt.pdf'))
    assert db.was_sent_before(legacy) is True

    assert db.was_sent_before(99999) is False


@pytest.mark.parametrize('old', ['sent', 'overdue', 'partial_payment', 'partial', 'paid', 'cancelled'])
def test_every_sent_status_in_the_protocol_counts(world, old):
    invoice_id = make_invoice(world, 'draft')
    assert world.db.was_sent_before(invoice_id) is False
    run_sql(world.db, "INSERT INTO InvoiceCorrections (InvoiceID, Version, Action, Field, OldValue, NewValue) "
                      "VALUES (?, 1, 'status', 'Status', ?, 'draft')", invoice_id, old)
    assert world.db.was_sent_before(invoice_id) is True


@pytest.mark.parametrize('old', ['draft', 'finalized'])
def test_a_change_between_editable_statuses_and_a_correction_row_do_not_count(world, old):
    invoice_id = make_invoice(world, 'draft')
    run_sql(world.db, "INSERT INTO InvoiceCorrections (InvoiceID, Version, Action, Field, OldValue, NewValue) "
                      "VALUES (?, 1, 'status', 'Status', ?, 'sent')", invoice_id, old)
    run_sql(world.db, "INSERT INTO InvoiceCorrections (InvoiceID, Version, Action, Field, OldValue, NewValue) "
                      "VALUES (?, 2, 'correct', 'Status', 'sent', 'x')", invoice_id)
    assert world.db.was_sent_before(invoice_id) is False


def test_the_question_can_be_asked_inside_a_transaction(world):
    invoice_id = new_invoice(world)
    force_status(world.db, invoice_id, 'sent')                                 # ein Import: weder Datei noch SentAt
    set_invoice_status(invoice_id, 'draft')
    with world.db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        assert world.db.was_sent_before(invoice_id, tx.conn) is True           # das Protokoll
        tx.conn.execute('DELETE FROM InvoiceCorrections WHERE InvoiceID = ?', (invoice_id,))
        assert world.db.was_sent_before(invoice_id, tx.conn) is False          # die Transaktion sieht ihren Stand
        tx.conn.execute("UPDATE Invoices SET SentAt = '2026-01-01 00:00:00' WHERE ID = ?", (invoice_id,))
        assert world.db.was_sent_before(invoice_id, tx.conn) is True           # SentAt


# ── Löschen ──────────────────────────────────────────────────────────────────

def test_a_sent_invoice_without_a_stored_file_keeps_its_number_when_the_draft_is_deleted(world):
    """Früher gab es nur die Dateiprobe: Eine versendete Rechnung ohne abgelegte Datei hinterließ keine Spur, und die
    Nummer ging beim Löschen des zurückgeholten Entwurfs zurück."""
    invoice_id = new_invoice(world)
    assert counter(world.db) == 1
    force_status(world.db, invoice_id, 'sent')                                 # kein Dokument, keine Forderung
    assert set_invoice_status(invoice_id, 'draft')[0] == 200
    assert not os.path.isdir(os.path.join(userctx.DATA_ROOT, 'invoices', '2026', 'Fassungen'))

    assert handlers.handle_delete_invoice(world.db, invoice_id) == (303, '/invoice')

    assert counter(world.db) == 1                                              # die Nummer kommt nicht zurück
    assert columns_exist(world, invoice_id) is False
    assert columns(world.db, new_invoice(world, customerId=world.cust2))['InvoiceNumber'] == '26R002'


def columns_exist(w, invoice_id):
    return bool(query_rows(w.db, 'SELECT 1 FROM Invoices WHERE ID = ?', invoice_id))


def test_a_draft_without_a_history_still_gives_its_number_back_and_a_sent_one_never(world):
    plain = new_invoice(world)
    set_invoice_status(plain, 'finalized')
    set_invoice_status(plain, 'draft')                                         # nur hin und her, nie versendet
    assert handlers.handle_delete_invoice(world.db, plain) == (303, '/invoice')
    assert counter(world.db) == 0

    sent = new_invoice(world)
    set_invoice_status(sent, 'sent')
    set_invoice_status(sent, 'draft')
    assert handlers.handle_delete_invoice(world.db, sent) == (303, '/invoice')
    assert counter(world.db) == 1 and handlers.handle_delete_invoice(world.db, sent)[0] == 404


def test_the_protocol_of_a_deleted_draft_stays(world):
    invoice_id = new_invoice(world)
    set_invoice_status(invoice_id, 'sent')
    set_invoice_status(invoice_id, 'draft')
    rows = protocol(world.db, invoice_id)
    assert len(rows) == 2
    assert handlers.handle_delete_invoice(world.db, invoice_id) == (303, '/invoice')
    assert protocol(world.db, invoice_id) == rows                              # kein Fremdschlüssel, kein Kaskadieren


def test_the_questions_of_the_form_and_the_list_use_the_new_answer(world):
    reopened = new_invoice(world)
    force_status(world.db, reopened, 'sent')
    set_invoice_status(reopened, 'draft')                                      # ohne Datei – früher: „unverdächtig“
    plain = new_invoice(world, customerId=world.cust2)
    list_part = PageInvoice(world.db, {}, None).split('Ende gridLeftCol')[0]
    assert f'onclick="deleteInvoiceDraft({reopened}, true)"' in list_part
    assert f'onclick="deleteInvoiceDraft({plain}, false)"' in list_part
    page = PageInvoice(world.db, {}, reopened)
    assert any(f"/invoice/delete?id={reopened}'" in a.get('onclick', '') and
               delete_question(True) in a['onclick'] for a in Dom(page).all('button'))


# ── Die Nummer eines zurückgeholten Entwurfs ─────────────────────────────────

def test_the_number_of_a_draft_that_was_sent_cannot_be_changed(world):
    invoice_id = new_invoice(world)
    set_invoice_status(invoice_id, 'sent')
    set_invoice_status(invoice_id, 'draft')
    before = snapshot(world.db)
    payload = make_payload(world, invoiceId=invoice_id, invoiceNumber='26R999', orderNumber='B-18')

    resp = save_raw(payload)

    assert resp.status == 409 and json.loads(resp) == {'success': False, 'error': NUMBER_KEPT_MESSAGE}
    assert snapshot(world.db) == before                                        # auch die anderen Felder nicht

    same = make_payload(world, invoiceId=invoice_id, invoiceNumber='26R001', orderNumber='B-18',
                        dueDate='2026-07-01')
    assert save(same) == {'success': True, 'invoice_id': invoice_id}           # die Nummer unverändert: geht
    stored = columns(world.db, invoice_id)
    assert (stored['InvoiceNumber'], stored['OrderNumber'], stored['PaymentDueDate']) == (
        '26R001', 'B-18', '2026-07-01')


def test_the_database_layer_refuses_the_number_too(world):
    invoice_id = new_invoice(world)
    set_invoice_status(invoice_id, 'sent')
    set_invoice_status(invoice_id, 'finalized')
    with pytest.raises(InvoiceLocked) as refused:
        with world.db.transaction() as tx:
            world.db.update_invoice_fields(tx, invoice_id, {'invoice_number': '26R999', 'order_number': 'B-99'})
    assert str(refused.value) == NUMBER_KEPT_MESSAGE and refused.value.fields == ['Rechnungsnummer']
    assert columns(world.db, invoice_id)['OrderNumber'] == 'B-17'
    with world.db.transaction() as tx:                                         # ohne die Nummer geht alles
        assert world.db.update_invoice_fields(tx, invoice_id, {'order_number': 'B-99'}) == 'edited'


def test_a_draft_without_a_history_can_still_be_renumbered(world):
    invoice_id = make_invoice(world, 'draft')
    payload = make_payload(world, invoiceId=invoice_id, invoiceNumber='26R777')
    assert save(payload) == {'success': True, 'invoice_id': invoice_id}
    assert columns(world.db, invoice_id)['InvoiceNumber'] == '26R777'


def test_the_form_shows_the_number_of_a_draft_that_was_sent_read_only_with_a_hint(world):
    reopened = new_invoice(world)
    set_invoice_status(reopened, 'sent')
    set_invoice_status(reopened, 'draft')
    plain = new_invoice(world, customerId=world.cust2)

    kept = PageInvoice(world.db, {}, reopened)
    field = Dom(kept).one('input', id='invoice_number')
    assert 'readonly' in field and 'disabled' not in field
    assert NUMBER_KEPT_MESSAGE in kept
    free = PageInvoice(world.db, {}, plain)
    assert 'readonly' not in Dom(free).one('input', id='invoice_number') and NUMBER_KEPT_MESSAGE not in free
    sent = new_invoice(world, customerId=world.cust2)
    set_invoice_status(sent, 'sent')
    locked = Dom(PageInvoice(world.db, {}, sent)).one('input', id='invoice_number')
    assert 'disabled' in locked                                                # versendet: wie bisher gesperrt


def test_the_form_of_a_new_invoice_and_of_a_copy_has_an_editable_number(world):
    sent = new_invoice(world)
    set_invoice_status(sent, 'sent')
    set_invoice_status(sent, 'draft')
    for html in (PageInvoice(world.db, {}, None), PageInvoice(world.db, {}, None, copy_from=sent)):
        field = Dom(html).one('input', id='invoice_number')
        assert 'readonly' not in field and 'disabled' not in field
