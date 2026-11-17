# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Betrag, Bankkonto und Datum einer Bankbewegung mit zugeordneter Zahlung bleiben (T38).

Ist einer Bankbewegung eine Rechnungszahlung oder Erstattung zugeordnet (``InvoicePayments.BookingID``), hängen Betrag und
Status der Rechnung und die Buchungssätze der Zuordnung an Betrag, Bankkonto und Datum der Bewegung. Ändert die Maske eines
davon, bliebe die Zuordnung stehen und stimmte nicht mehr. Entschieden ist: **ablehnen** – nichts wird umgerechnet, der Nutzer
entfernt erst die Zuordnung. ``Database.save_bank_with_rows`` prüft das am Ende seiner Transaktion
(``_assigned_bank_change_blocker``, ``BankChangeRefused``): Abgelehnt wird mit 400 und Klartext, alles geht zurück – auch ein
sonst gültiges Speichern derselben Anfrage. Es zählen die Zuordnungen, die nach dem Schreiben noch übrig sind: Wer im Editor
alle Zeilen der Zahlung entfernt, darf den Betrag im selben Speichern ändern. Empfänger, Text, Währung und Steuerdatum sind
frei, und ein unverändert zurückgeschickter Wert (``'119'`` gegen gespeicherte 119,0000) ist nie ein Verstoß.

Zur Bewegung gehört auch eine Zuordnung, die an einem ihrer Buchungssätze hängt (``InvoicePayments.BookingID`` zeigt auf ein Kind:
So ordnet der Import eine Zahlung einem eigenständigen Satz zu, den der Abgleich oder der Editor später unter die Bankbewegung
hängt); hängt der Nutzer den Satz im selben Speichern ab, zählt sie nicht mehr.

Die Formulare stammen aus dem HTML der echten Maske (``tests/entry_form_roundtrip.py``). Alle Namen und Werte sind erfunden.
"""
from contextlib import closing

import pytest

import server.handlers as handlers
from db import Database
from server.handlers import handle_add_transaction
from tests.entry_form_helpers import coa
from tests.entry_form_roundtrip import adopt_row, drop_row, open_form, submit
from tests.invoice_world import add_payment, columns, query_rows, run_sql
from tests.test_invoice_refund import comparable, outgoing, paid_and_cancelled, refund
from tests.test_invoice_storno import pay, sent, world  # noqa: F401  (world ist eine Fixture)

PAYMENT_TEXT = ('Dieser Bankbewegung ist eine Rechnungszahlung zugeordnet (Rechnung {numbers}). '
                'Bitte zuerst die Zuordnung an der Rechnung entfernen, dann {changes} ändern.')
REFUND_TEXT = ('Dieser Bankbewegung ist eine Erstattung zugeordnet (Stornorechnung {numbers}). '
               'Bitte zuerst die Erstattung an der Stornorechnung entfernen, dann {changes} ändern.')

# Das gesperrte Feld der Maske → (neuer Wert, was der Klartext zu ändern nennt)
BLOCKED = {
    'amount': (lambda w: '120.00', 'den Betrag'),
    'account': (lambda w: str(w.bank2), 'das Bankkonto'),
    'date': (lambda w: '2026-07-02', 'das Datum'),
}


@pytest.fixture
def logged(monkeypatch):
    """Die Beschreibungen der Einträge, die ins Replay-Log gehen, in ihrer Reihenfolge."""
    entries = []
    monkeypatch.setattr(Database, '_log_sql', lambda self, sql, params, description: entries.append(description))
    return entries


def _paid(w, amount=119.0):
    """R1 (119,00 €) versendet und bezahlt: die Bankbewegung der Zahlung. Zahlungs- und Umbuchungssatz hängen darunter."""
    return pay(w, sent(w), amount)


def _without_editor(post_data):
    """Dasselbe Formular ohne den Buchungssatz-Editor (``split_*``): so schickt es eine Bankzeile ohne Sätze."""
    return {name: values for name, values in post_data.items() if not name.startswith('split_')}


def _what_stays(db, bank, with_editor):
    """Was ein unverändert zurückgeschicktes Formular nicht anfasst: mit Editor jede Spalte jeder Buchung, jede Zuordnung und
    jede Rechnung. Ohne Editor schreibt die Maske die Kontierung und Steuer der Bankzeile aus ihren (hier fehlenden) Feldern –
    das prüft dieser Test nicht; bleiben müssen Betrag, Bankkonto und Datum der Bewegung, die Zuordnungen und die Rechnungen."""
    everything = comparable(db)
    if with_editor:
        return everything
    return (query_rows(db, 'SELECT Amount, Account_ID, DateBooking FROM Bookings WHERE ID = ?', bank),
            everything['InvoicePayments'], everything['Invoices'])


def _refused(db, post_data, expected, logged):
    before = comparable(db)
    logged.clear()
    assert handle_add_transaction(db, post_data) == (400, expected)
    assert comparable(db) == before                          # auch die übrigen Änderungen der Anfrage sind nicht gespeichert
    assert logged == []


def _changed_form(db, bank, **fields):
    """Die Maske der Bankbewegung, abgeschickt mit geänderten Feldern – und mit einem geänderten Empfänger und Text, die für
    sich erlaubt wären."""
    post_data, _options = open_form(db, bank)
    post_data['recipient'], post_data['text'] = ['Neuer Empfänger'], ['Neuer Verwendungszweck']
    for name, value in fields.items():
        post_data[name] = [value]
    return post_data


# ── jedes gesperrte Feld wird abgelehnt ──────────────────────────────────────────────

@pytest.mark.parametrize('with_editor', [True, False], ids=['mit-editor', 'ohne-editor'])
@pytest.mark.parametrize('field', BLOCKED)
def test_changing_a_locked_field_of_a_bank_movement_with_a_payment_is_refused(world, logged, field, with_editor):
    db = world.db
    bank = _paid(world)
    value, what = BLOCKED[field]
    post_data = _changed_form(db, bank, **{field: value(world)})

    _refused(db, post_data if with_editor else _without_editor(post_data),
             PAYMENT_TEXT.format(numbers='T-100', changes=what), logged)


@pytest.mark.parametrize('fields, changes', [
    pytest.param(('amount', 'date'), 'den Betrag und das Datum', id='zwei'),
    pytest.param(('amount', 'account', 'date'), 'den Betrag, das Bankkonto und das Datum', id='drei'),
])
def test_several_changed_fields_are_named_together(world, logged, fields, changes):
    db = world.db
    bank = _paid(world)
    post_data = _changed_form(db, bank, **{field: BLOCKED[field][0](world) for field in fields})

    _refused(db, post_data, PAYMENT_TEXT.format(numbers='T-100', changes=changes), logged)


# ── was erlaubt bleibt ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('with_editor', [True, False], ids=['mit-editor', 'ohne-editor'])
def test_saving_the_unchanged_form_changes_nothing(world, with_editor):
    db = world.db
    bank = _paid(world)
    before = _what_stays(db, bank, with_editor)
    post_data, _options = open_form(db, bank)

    submit(db, post_data if with_editor else _without_editor(post_data))

    assert _what_stays(db, bank, with_editor) == before


@pytest.mark.parametrize('with_editor', [True, False], ids=['mit-editor', 'ohne-editor'])
def test_an_amount_sent_back_in_another_notation_is_no_change(world, with_editor):
    """Gespeichert sind 119,0000 €; das Formular schickt ``119.0000`` – ein anderer Nutzer oder eine Skript-Eingabe auch
    ``119`` oder ``119.00``. Es zählt der Betrag, nicht die Schreibweise."""
    db = world.db
    bank = _paid(world)
    before = _what_stays(db, bank, with_editor)
    for text in ('119', '119.00', '119.0000', '+119'):
        post_data, _options = open_form(db, bank)
        post_data['amount'] = [text]
        submit(db, post_data if with_editor else _without_editor(post_data))
        assert _what_stays(db, bank, with_editor) == before, text


@pytest.mark.parametrize('with_editor', [True, False], ids=['mit-editor', 'ohne-editor'])
def test_recipient_text_currency_and_tax_date_are_free(world, with_editor):
    db = world.db
    bank = _paid(world)
    post_data = _changed_form(db, bank, date_tax='2026-07-05', currency='USD')

    submit(db, post_data if with_editor else _without_editor(post_data))

    assert query_rows(db, 'SELECT RecipientClient, Text, DateTax, Currency, Amount, DateBooking, Account_ID FROM Bookings '
                          'WHERE ID = ?', bank) == [{
        'RecipientClient': 'Neuer Empfänger', 'Text': 'Neuer Verwendungszweck', 'DateTax': '2026-07-05', 'Currency': 'USD',
        'Amount': 1190000, 'DateBooking': '2026-07-01', 'Account_ID': world.bank}]
    assert len(db.get_booking_allocations(bank)) == 1         # die Zuordnung bleibt


@pytest.mark.parametrize('with_editor', [True, False], ids=['mit-editor', 'ohne-editor'])
def test_a_bank_movement_without_an_assignment_changes_freely(world, with_editor):
    db = world.db
    bank = db.insert_booking('2026-07-01', 119.0, booking_type='bank', account_id=world.bank, recipient_client='Frei')
    post_data = _changed_form(db, bank, amount='120.00', date='2026-07-02', account=str(world.bank2))

    submit(db, post_data if with_editor else _without_editor(post_data))

    assert query_rows(db, 'SELECT Amount, DateBooking, Account_ID FROM Bookings WHERE ID = ?', bank) == [{
        'Amount': 1200000, 'DateBooking': '2026-07-02', 'Account_ID': world.bank2}]


def test_after_the_assignment_is_removed_the_amount_can_be_changed(world):
    db = world.db
    bank = _paid(world)
    (payment,) = [row[0] for row in db.get_booking_allocations(bank)]
    assert handle_add_transaction(db, _changed_form(db, bank, amount='120.00'))[0] == 400

    db.unassign_invoice_payment(payment)

    assert handle_add_transaction(db, _changed_form(db, bank, amount='120.00'))[0] == 303
    assert query_rows(db, 'SELECT Amount FROM Bookings WHERE ID = ?', bank) == [{'Amount': 1200000}]


def test_removing_all_rows_of_the_payment_allows_the_new_amount_in_the_same_save(world):
    """✕ an allen Zeilen der Zahlung und ein neuer Betrag in einem Speichern: Es zählen die Zuordnungen, die nach dem
    Schreiben noch übrig sind – hier keine. Die Rechnung wird neu gerechnet."""
    db = world.db
    r1 = sent(world)
    bank = pay(world, r1, 119.0)
    settlement, transfer = [row['ID'] for row in query_rows(
        db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank)]
    assert columns(db, r1)['Status'] == 'paid'
    post_data = _changed_form(db, bank, amount='100.00')
    post_data = drop_row(drop_row(post_data, settlement), transfer)

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    assert query_rows(db, 'SELECT COUNT(*) AS n FROM InvoicePayments') == [{'n': 0}]
    assert query_rows(db, 'SELECT COUNT(*) AS n FROM Bookings WHERE ParentBooking_ID = ?', bank) == [{'n': 0}]
    invoice = columns(db, r1)
    assert (invoice['AmountDue'], invoice['Status'] != 'paid') == (invoice['SumGross'], True)    # neu gerechnet
    assert query_rows(db, 'SELECT Amount FROM Bookings WHERE ID = ?', bank) == [{'Amount': 1000000}]


def test_removing_only_one_row_of_the_payment_removes_the_assignment_and_allows_the_new_amount(world):
    """Ein ✕ an einer markierten Zeile entfernt die ganze Zuordnung (``remove_assignment_for_child``)."""
    db = world.db
    bank = _paid(world)
    settlement = query_rows(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', bank)[0]['ID']

    assert handle_add_transaction(db, drop_row(_changed_form(db, bank, amount='100.00'), settlement))[0] == 303

    assert query_rows(db, 'SELECT COUNT(*) AS n FROM InvoicePayments') == [{'n': 0}]


# ── Erstattung, Zuordnung ohne Sätze, mehrere Rechnungen ──────────────────────────────

def test_a_refund_is_named_as_a_refund(world, logged):
    db = world.db
    r1, s1, _paid_booking = paid_and_cancelled(world)
    out = outgoing(world, 20.0, '2026-07-10')
    refund(world, s1, out, 20.0)
    storno_number = columns(db, s1)['InvoiceNumber']

    _refused(db, _changed_form(db, out, amount='-25.00'), REFUND_TEXT.format(numbers=storno_number, changes='den Betrag'),
             logged)
    _refused(db, _changed_form(db, out, date='2026-07-11'),
             REFUND_TEXT.format(numbers=storno_number, changes='das Datum'), logged)


def test_the_payment_of_a_cancelled_invoice_is_still_a_payment(world, logged):
    db = world.db
    r1, _s1, paid_booking = paid_and_cancelled(world)

    _refused(db, _changed_form(db, paid_booking, amount='60.00'), PAYMENT_TEXT.format(numbers='T-100', changes='den Betrag'),
             logged)


def test_an_assignment_without_booking_rows_blocks_too(world, logged):
    """Der WISO-Import ordnet zu, ohne Sätze zu schreiben (``link_invoice_to_transaction``): Es bleibt trotzdem eine Zuordnung."""
    db = world.db
    r1 = sent(world)
    bank = add_payment(world, r1, 50.0)
    assert query_rows(db, 'SELECT COUNT(*) AS n FROM Bookings WHERE ParentBooking_ID = ?', bank) == [{'n': 0}]

    _refused(db, _changed_form(db, bank, amount='60.00'), PAYMENT_TEXT.format(numbers='T-100', changes='den Betrag'), logged)
    _refused(db, _without_editor(_changed_form(db, bank, date='2026-07-02')),
             PAYMENT_TEXT.format(numbers='T-100', changes='das Datum'), logged)


def test_a_collective_payment_names_all_its_invoices(world, logged):
    db = world.db
    r1, r3 = sent(world), sent(world, number='T-103')
    bank = db.insert_booking('2026-07-01', 100.0, booking_type='bank', account_id=world.bank,
                             recipient_client='Malermeister Pinsel')
    for invoice in (r1, r3):
        assert handlers.link_booking_to_invoice_capped(db, invoice, bank, 50.0) == (True, None)

    _refused(db, _changed_form(db, bank, date='2026-07-02'),
             PAYMENT_TEXT.format(numbers='T-100, T-103', changes='das Datum'), logged)


# ── die Zuordnung hängt an einem Kind der Bankbewegung ────────────────────────────────

IMPORT_GROUP = 'WISO-BANK-1'


def _imported_payment(w, invoice, amount=119.0):
    """Ein eigenständiger Satz „Bank an Debitoren“, wie der WISO-Import ihn schreibt (mit Quellklammer), der Rechnung
    zugeordnet – ohne Buchungssätze (``link_invoice_to_transaction``). Er hängt noch an keiner Bankbewegung."""
    db = w.db
    entry = db.insert_booking('2026-07-01', amount, coa_id=coa(db, 1800), counter_coa_id=coa(db, 10000),
                              booking_type='entry', entry_form='liquid_first', recipient_client='Malermeister Pinsel',
                              text='Zahlung aus dem Import', document_number='T-100')
    run_sql(db, 'UPDATE Bookings SET SourceGroup = ? WHERE ID = ?', IMPORT_GROUP, entry)
    db.link_invoice_to_transaction(invoice, entry, amount)
    return entry


def _bank_movement(w):
    """Eine Bankbewegung über 119,00 € auf das Giro-Konto, ohne Zuordnung und ohne Sätze."""
    return w.db.insert_booking('2026-07-01', 119.0, booking_type='bank', account_id=w.bank,
                               recipient_client='Malermeister Pinsel')


def _bank_with_assignment_on_a_child(w):
    """R1 (119,00 €) versendet, ihre Zahlung ein importierter Satz (``InvoicePayments.BookingID`` zeigt auf ihn), und der Satz
    hängt als Kind unter einer Bankbewegung über 119,00 € – dorthin hängt ihn der Abgleich oder der Editor. An der Bewegung
    selbst hängt keine Zuordnung. Liefert (Bankbewegung, Satz)."""
    db = w.db
    entry = _imported_payment(w, sent(w))
    bank = _bank_movement(w)
    run_sql(db, 'UPDATE Bookings SET ParentBooking_ID = ? WHERE ID = ?', bank, entry)
    assert db.get_booking_allocations(bank) == []
    assert query_rows(db, 'SELECT BookingID FROM InvoicePayments') == [{'BookingID': entry}]
    return bank, entry


@pytest.mark.parametrize('with_editor', [True, False], ids=['mit-editor', 'ohne-editor'])
@pytest.mark.parametrize('field', BLOCKED)
def test_changing_a_locked_field_is_refused_when_the_assignment_hangs_on_a_child(world, logged, field, with_editor):
    """Die Zuordnung eines Satzes unter der Bewegung (``InvoicePayments.BookingID`` = das Kind) zählt wie eine an der Bewegung."""
    db = world.db
    bank, _entry = _bank_with_assignment_on_a_child(world)
    value, what = BLOCKED[field]
    post_data = _changed_form(db, bank, **{field: value(world)})

    _refused(db, post_data if with_editor else _without_editor(post_data),
             PAYMENT_TEXT.format(numbers='T-100', changes=what), logged)


@pytest.mark.parametrize('with_editor', [True, False], ids=['mit-editor', 'ohne-editor'])
def test_saving_the_unchanged_form_with_an_assignment_on_a_child_changes_nothing(world, with_editor):
    db = world.db
    bank, _entry = _bank_with_assignment_on_a_child(world)
    before = _what_stays(db, bank, with_editor)
    post_data, _options = open_form(db, bank)

    submit(db, post_data if with_editor else _without_editor(post_data))

    assert _what_stays(db, bank, with_editor) == before


def test_removing_the_row_of_the_assigned_child_in_the_same_save_allows_the_new_amount(world):
    """✕ in der Zeile des importierten Satzes (``SourceGroup``) hängt ihn von der Bewegung ab, er wird nicht gelöscht: Die
    Zuordnung bleibt am Satz, gehört aber nicht mehr zur Bankbewegung – der Betrag lässt sich im selben Speichern ändern."""
    db = world.db
    bank, entry = _bank_with_assignment_on_a_child(world)
    payments = query_rows(db, 'SELECT * FROM InvoicePayments ORDER BY ID')
    post_data = drop_row(_changed_form(db, bank, amount='120.00'), entry)

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    assert query_rows(db, 'SELECT Amount FROM Bookings WHERE ID = ?', bank) == [{'Amount': 1200000}]
    assert query_rows(db, 'SELECT ParentBooking_ID, SourceGroup FROM Bookings WHERE ID = ?', entry) == [
        {'ParentBooking_ID': None, 'SourceGroup': IMPORT_GROUP}]            # abgehängt, nicht gelöscht
    assert query_rows(db, 'SELECT * FROM InvoicePayments ORDER BY ID') == payments      # die Zuordnung bleibt, wie sie war


def test_adopting_an_assigned_entry_and_changing_the_amount_in_the_same_save_is_refused(world, logged):
    """Geprüft wird nach den Schreibschritten: Ein Satz mit Zuordnung, den der Editor der Bewegung erst in diesem Speichern
    zuordnet, zählt schon."""
    db = world.db
    entry = _imported_payment(world, sent(world))
    bank = _bank_movement(world)
    _post_data, options = open_form(db, bank)
    option = next(o for o in options if o['value'] == str(entry))
    post_data = adopt_row(_changed_form(db, bank, amount='120.00'), option)

    _refused(db, post_data, PAYMENT_TEXT.format(numbers='T-100', changes='den Betrag'), logged)


def test_an_assigned_entry_can_be_adopted_when_amount_account_and_date_stay(world):
    db = world.db
    entry = _imported_payment(world, sent(world))
    bank = _bank_movement(world)
    post_data, options = open_form(db, bank)
    option = next(o for o in options if o['value'] == str(entry))

    submit(db, adopt_row(post_data, option))

    assert query_rows(db, 'SELECT ParentBooking_ID FROM Bookings WHERE ID = ?', entry) == [{'ParentBooking_ID': bank}]
    assert query_rows(db, 'SELECT Amount, Account_ID, DateBooking FROM Bookings WHERE ID = ?', bank) == [{
        'Amount': 1190000, 'Account_ID': world.bank, 'DateBooking': '2026-07-01'}]


@pytest.mark.parametrize('dropped', ['alle-zeilen', 'eine-zeile'])
def test_removing_the_payment_rows_of_one_invoice_does_not_free_a_collective_payment(world, logged, dropped):
    """Sammelzahlung über 100,00 € für T-100 und T-103: ✕ an den Zeilen der Zahlung von T-100 entfernt deren Zuordnung, die von
    T-103 bleibt – der neue Betrag wird abgelehnt, der Klartext nennt nur T-103, und alles geht zurück: auch die Zuordnung von
    T-100 und der Stand beider Rechnungen."""
    db = world.db
    r1, r3 = sent(world), sent(world, number='T-103')
    bank = db.insert_booking('2026-07-01', 100.0, booking_type='bank', account_id=world.bank,
                             recipient_client='Malermeister Pinsel')
    for invoice in (r1, r3):
        assert handlers.link_booking_to_invoice_capped(db, invoice, bank, 50.0) == (True, None)
    rows_of_r1 = [row['ID'] for row in query_rows(
        db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND DocumentNumber = ? ORDER BY ID', bank, 'T-100')]
    assert len(rows_of_r1) == 2                                          # Zahlungs- und Umbuchungssatz
    post_data = _changed_form(db, bank, amount='120.00')
    for row_id in rows_of_r1 if dropped == 'alle-zeilen' else rows_of_r1[:1]:
        post_data = drop_row(post_data, row_id)
    invoices = 'SELECT InvoiceNumber, Status, AmountDue FROM Invoices ORDER BY ID'
    invoices_before = query_rows(db, invoices)

    _refused(db, post_data, PAYMENT_TEXT.format(numbers='T-103', changes='den Betrag'), logged)

    assert [row[2] for row in db.get_invoice_payments(r1)] == [bank]      # die Zuordnung von T-100 steht noch
    assert query_rows(db, 'SELECT COUNT(*) AS n FROM Bookings WHERE ParentBooking_ID = ?', bank) == [{'n': 4}]
    assert query_rows(db, invoices) == invoices_before                    # Status und offener Betrag beider Rechnungen


# ── die Prüfung selbst ───────────────────────────────────────────────────────────────

def test_the_check_looks_only_at_the_fields_it_is_given(world):
    db = world.db
    bank = _paid(world)
    free = db.insert_booking('2026-07-03', 5.0, booking_type='bank', account_id=world.bank)
    with closing(db._get_connection()) as conn:
        stored = conn.execute('SELECT Amount, Account_ID, DateBooking FROM Bookings WHERE ID = ?', (bank,)).fetchone()
        blocker = db._assigned_bank_change_blocker
        text = PAYMENT_TEXT.format(numbers='T-100', changes='{}')

        assert blocker(conn, bank, stored, {}) is None
        assert blocker(conn, bank, stored, {'text': 'x', 'recipient_client': 'y', 'currency': 'USD', 'date_tax': '2026-08-01'}) is None
        assert blocker(conn, bank, stored, {'amount': 119.0, 'account_id': world.bank, 'date_booking': '2026-07-01'}) is None
        assert blocker(conn, bank, stored, {'amount': '119', 'tax_amount': 12.34}) is None
        assert blocker(conn, bank, stored, {'amount': 119.01}) == text.format('den Betrag')
        assert blocker(conn, bank, stored, {'account_id': None}) == text.format('das Bankkonto')
        assert blocker(conn, bank, stored, {'date_booking': ''}) == text.format('das Datum')
        assert blocker(conn, bank, stored, {'date_booking': None}) == text.format('das Datum')
        # ohne Zuordnung ist jede Änderung frei
        assert blocker(conn, free, (50000, world.bank, '2026-07-03'), {'amount': 6.0, 'date_booking': '2026-08-01'}) is None
