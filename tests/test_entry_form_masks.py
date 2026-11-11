# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Masken und die Form eines Buchungssatzes (``Bookings.EntryForm``): Befunde der Finanzprüfung.

Der allgemeine Beweis, dass „öffnen und unverändert speichern“ nichts ändert, steht in
``test_entry_form_roundtrip.py``. Hier die einzelnen Fälle:

* Buchungssatz-Editor: „Bank an 1460“ (ein Spiegel des Bestands) zeigt das Zweckkonto 1460 und bleibt
  beim Speichern, wie er ist; ebenso der Kontovorschlag. Ein Spiegel mit 1460 bleibt sichtbar und
  bearbeitbar, die Summenzeile des Editors rechnet wie die Prüfung auf dem Server.
* Buchungsmaske eines Einzelsatzes: Ein Konto, das kein Zahlungskonto mehr ist, wird nicht zum
  Gegenkonto; ein unverändert zurückgeschickter Betrag erhält Soll/Haben beim Wechsel des Zweckkontos.
* „Vorhandene Buchung zuordnen“ nimmt bei einer Buchung mit Geldkonto vorne das Zweckkonto vor.
* Kein Schreibweg der Masken schreibt dasselbe Konto auf beide Seiten.

Alle Namen und Werte sind erfunden.
"""
from decimal import Decimal

import pytest

from db.matching import (
    FORM_LIQUID_FIRST, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY, FORM_PURPOSE_FIRST,
)
from server.handlers import handle_add_transaction
from tests.entry_form_helpers import (
    D1, D2, bank_account, coa, csv_export, datev_rows, execute, minor, query,
)
from tests.entry_form_roundtrip import (
    add_row, adopt_row, editor_rest, editor_rows, open_form, snapshot, submit,
)


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Maske-Bank-Eins', 1800)
    for number, name in ((1050, 'Altkonto Vorräte'),):
        tmp_db.insert_chart_of_accounts(4, number, name, 'Test')
    return tmp_db


def _alpha(db):
    return next(a[0] for a in db.fetch_accounts() if a[1] == 'Maske-Bank-Eins')


def _movement(db, amount, date='2026-03-01'):
    return db.insert_booking(date, Decimal(amount), account_id=_alpha(db), booking_type='bank',
                             text='Bewegung')


def _child(db, parent, coa_number, counter_number, amount, form, document='K-1'):
    """Ein Satz mit der Form ``form``; ``None``: eine Zeile ohne Form, wie Roh-SQL sie anlegt."""
    booking_id = db.insert_booking('2026-03-01', Decimal(amount), coa_id=coa(db, coa_number),
                                   counter_coa_id=coa(db, counter_number), document_number=document,
                                   text=f'Posten {document}', booking_type='entry',
                                   parent_booking_id=parent, entry_form=form)
    if form is None:
        execute(db, 'UPDATE Bookings SET EntryForm = NULL WHERE ID = ?', (booking_id,))
    return booking_id


def _entry(db, coa_number, counter_number, amount, form, document='E-1'):
    return _child(db, None, coa_number, counter_number, amount, form, document)


def _accounts_of(db, booking_id):
    return query(db, 'SELECT COA_ID, CounterCOA_ID, Amount, EntryForm FROM Bookings WHERE ID = ?',
                 (booking_id,))[0]


def _mask_post(db, booking_id, **changes):
    """Die Buchungsmaske eines Einzelsatzes, wie der Browser sie abschickt, mit geänderten Feldern."""
    post, _options = open_form(db, booking_id)
    for name, value in changes.items():
        post[name] = [str(value)]
    return post


# ── A1: Bank an 1460 im Editor ────────────────────────────────────────────────

@pytest.mark.parametrize('form', [FORM_MIRROR_LEGACY, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, None])
@pytest.mark.parametrize('other', [1460, 1050])
def test_the_editor_shows_the_other_account_of_a_bank_to_1460_row_and_keeps_it(db, form, other):
    """Konto = die Bank, Gegenkonto = 1460 (oder ein Konto, das früher als Kasse galt): Das Feld „SKR-Konto“
    zeigt das Zweckkonto 1460 – nicht die Bank. Unverändert gespeichert wird daraus nicht „Bank an Bank“."""
    bank = _movement(db, '70.00')
    row = _child(db, bank, 1800, other, '70.00', form)
    before = snapshot(db)

    rows, _amount = editor_rows(db, bank)
    assert [(r['id'], r['coa']) for r in rows] == [(str(row), coa(db, other))]
    post, _ = open_form(db, bank)
    submit(db, post)

    assert _accounts_of(db, row) == (coa(db, 1800), coa(db, other), minor('70.00'), form)
    assert snapshot(db) == before


@pytest.mark.parametrize('form', [FORM_MIRROR_LEGACY, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, None])
def test_the_editor_shows_the_account_in_front_of_a_clearing_to_bank_row(db, form):
    bank = _movement(db, '-70.00')
    row = _child(db, bank, 1460, 1800, '70.00', form)

    rows, _amount = editor_rows(db, bank)
    post, _ = open_form(db, bank)
    submit(db, post)

    assert [r['coa'] for r in rows] == [coa(db, 1460)]
    assert _accounts_of(db, row)[:2] == (coa(db, 1460), coa(db, 1800))


def test_the_suggestion_takes_the_other_account_of_a_bank_to_1460_row(db):
    """Der Kontovorschlag kopiert die Kontierung der letzten Buchung desselben Empfängers: bei „Bank an
    1460“ ist das Zweckkonto 1460."""
    bank = db.insert_booking('2026-03-01', Decimal('70.00'), account_id=_alpha(db), booking_type='bank',
                             recipient_client='Verrechnung GmbH')
    _child(db, bank, 1800, 1460, '70.00', FORM_MIRROR_LEGACY)

    hit = db.find_history_suggestion('Verrechnung GmbH')

    assert [row['coa_id'] for row in hit['rows']] == [coa(db, 1460)]


# ── A4: ein Spiegel mit 1460 bleibt sichtbar, die Summenzeile stimmt ───────────

@pytest.mark.parametrize('form', [FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY])
def test_a_mirror_between_two_own_accounts_is_hidden_in_the_list_and_in_the_editor(db, form):
    bank_account(db, 'Maske-Bank-Zwei', 1810)
    bank = _movement(db, '-50.00')
    _child(db, bank, 1810, 1800, '50.00', form)
    solo = _entry(db, 1810, 1800, '5.00', form, document='S-1')

    assert editor_rows(db, bank)[0] == []
    assert solo not in [item['booking'][0] for item in db.fetch_bookings_grouped(D1, D2)]


@pytest.mark.parametrize('form', [FORM_MIRROR_COA, FORM_MIRROR_COUNTER])
def test_a_mirror_with_the_clearing_account_stays_in_the_list(db, form):
    solo = _entry(db, 1460, 1800, '100.00', form, document='S-1')

    assert solo in [item['booking'][0] for item in db.fetch_bookings_grouped(D1, D2)]


def test_a_mirror_with_the_clearing_account_is_in_the_editor_and_the_editor_sum_matches_the_server(db):
    """1460 an 1800 (``mirror_coa``, +100) unter einer Bankbewegung von −100: Der Satz steht im Editor,
    und die Summenzeile rechnet ihn wie die Prüfung auf dem Server (gegenläufig: −100) – Rest 0 hier wie
    dort. Mit dem Rohbetrag allein käme der Editor auf einen Rest von −200."""
    bank = _movement(db, '-100.00')
    row = _child(db, bank, 1460, 1800, '100.00', FORM_MIRROR_COA)

    rows, bank_amount = editor_rows(db, bank)
    assert [(r['id'], r['amount'], r['sign']) for r in rows] == [(str(row), 100.0, -1.0)]
    assert bank_amount == -100.0
    assert editor_rest(db, bank) == 0
    assert db.find_unbalanced_splits(D1, D2) == []


@pytest.mark.parametrize('coa_number, counter_number, form, bank_amount', [
    (1800, 1460, FORM_MIRROR_COA, '70.00'),         # Bank vorne, der Betrag steht auf dem Konto: dieselbe Richtung
    (1460, 1800, FORM_MIRROR_COA, '-70.00'),        # Bank hinten: das Gegenteil
    (1460, 1800, FORM_MIRROR_COUNTER, '70.00'),     # der Betrag steht auf dem Gegenkonto, das ist die Bank
    (1800, 1460, FORM_MIRROR_COUNTER, '-70.00'),
    (1460, 1810, FORM_MIRROR_COA, '5.00'),          # berührt das Konto der Bank nicht: zählt nicht
    (1800, 1460, FORM_MIRROR_LEGACY, '70.00'),      # unbekannte Lesart: wie bisher der Rohbetrag
])
def test_the_editor_sum_matches_the_server_for_every_position_of_a_mirror(
        db, coa_number, counter_number, form, bank_amount):
    """Editor (``splitRecalc``) und Prüfung auf dem Server (``find_unbalanced_splits``) kommen auf
    denselben Rest, ob er nun null ist oder nicht."""
    bank_account(db, 'Maske-Bank-Zwei', 1810)
    bank = _movement(db, bank_amount)
    _child(db, bank, coa_number, counter_number, '70.00', form)

    server = [row[3] for row in db.find_unbalanced_splits(D1, D2) if row[0] == bank]

    assert editor_rest(db, bank) == (float(server[0]) if server else 0)


# ── A2: Buchungsmaske eines Einzelsatzes ──────────────────────────────────────

@pytest.mark.parametrize('number', [1050, 1810])
def test_the_mask_keeps_a_cash_account_in_front_that_is_no_payment_account_any_more(db, number):
    """„1050 an 4400“ (früher Kasse) und „Bankkonto an 4400“, dessen Bankkonto gelöscht ist: Das Feld
    „SKR-Konto“ zeigt das Konto vorne. Kommt es unverändert zurück, bleibt der Satz, wie er ist – es wird
    nicht zum Gegenkonto (früher „1050 an 1050“)."""
    entry = _entry(db, number, 4400, '20.00', FORM_LIQUID_FIRST)      # 1810: kein eigenes Konto (mehr)
    before = _accounts_of(db, entry)

    submit(db, _mask_post(db, entry))

    assert _accounts_of(db, entry) == before


def test_the_mask_still_turns_a_chosen_purpose_account_into_the_counter_account(db):
    """Wählt der Nutzer ein anderes Konto, meint er das Zweckkonto: Das Geldkonto bleibt vorne."""
    entry = _entry(db, 1050, 4400, '20.00', FORM_LIQUID_FIRST)

    submit(db, _mask_post(db, entry, coa_id=coa(db, 4300)))

    assert _accounts_of(db, entry) == (coa(db, 1050), coa(db, 4300), minor('20.00'), FORM_LIQUID_FIRST)


def test_the_mask_takes_the_purpose_account_back_when_no_account_is_chosen(db):
    entry = _entry(db, 1050, 4400, '20.00', FORM_LIQUID_FIRST)

    submit(db, _mask_post(db, entry, coa_id=''))

    assert _accounts_of(db, entry)[:2] == (coa(db, 1050), None)


# ── A3: vorhandene Buchung zuordnen ───────────────────────────────────────────

@pytest.mark.parametrize('number', [1800, 1460, 1050])
def test_the_adopt_widget_offers_the_purpose_account_of_a_cash_account_in_front(db, number):
    candidate = _entry(db, number, 4400, '11.00', FORM_LIQUID_FIRST)
    bank = _movement(db, '1.00')
    post, options = open_form(db, bank)
    option = next(o for o in options if o['value'] == str(candidate))

    assert option['coa'] == str(coa(db, 4400))
    submit(db, adopt_row(post, option))

    assert _accounts_of(db, candidate) == (coa(db, number), coa(db, 4400), minor('11.00'),
                                            FORM_LIQUID_FIRST)
    assert query(db, 'SELECT ParentBooking_ID FROM Bookings WHERE ID = ?', (candidate,)) == [(bank,)]


def test_the_adopt_widget_offers_the_account_in_front_of_a_purpose_first_candidate(db):
    candidate = _entry(db, 6815, 1800, '-11.00', FORM_PURPOSE_FIRST)
    bank = _movement(db, '-11.00')
    post, options = open_form(db, bank)
    option = next(o for o in options if o['value'] == str(candidate))

    assert option['coa'] == str(coa(db, 6815))


@pytest.mark.parametrize('number', [1800, 1460])
def test_assigning_with_the_cash_account_itself_as_the_purpose_changes_nothing(db, number):
    """Ein Client, der noch das Konto der Buchung als „SKR-Konto“ schickt (so wie die Seite es früher
    vorbelegte), macht aus „1460 an 4400“ nicht „1460 an 1460“: Das Konto vorne ist kein Zweckkonto."""
    candidate = _entry(db, number, 4400, '11.00', FORM_LIQUID_FIRST)
    bank = _movement(db, '1.00')
    post, options = open_form(db, bank)
    option = dict(next(o for o in options if o['value'] == str(candidate)), coa=str(coa(db, number)))

    submit(db, adopt_row(post, option))

    assert _accounts_of(db, candidate)[:2] == (coa(db, number), coa(db, 4400))


def test_assigning_a_mirror_between_two_own_accounts_keeps_its_accounts(db):
    bank_account(db, 'Maske-Bank-Zwei', 1810)
    candidate = _entry(db, 1800, 1810, '30.00', FORM_MIRROR_COA)
    bank = _movement(db, '1.00')
    post, options = open_form(db, bank)
    option = next(o for o in options if o['value'] == str(candidate))

    submit(db, adopt_row(post, option))

    assert _accounts_of(db, candidate)[:2] == (coa(db, 1800), coa(db, 1810))


# ── A5: Wechsel des Zweckkontos in der Maske erhält Soll/Haben ────────────────

def test_changing_the_account_of_an_imported_transfer_in_the_mask_keeps_the_debit_side(db):
    """CSV „1600;1800;100“ (Abhebung, ``mirror_coa``, DATEV S 1600). Die Maske schickt den gespeicherten
    Betrag mit; wählt der Nutzer dort 6815, bleibt das Konto im Soll: danach S 6815."""
    db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'AB-1', '')))
    (entry,) = [row[0] for row in query(db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'AB-1'")]
    assert datev_rows(db)['AB-1'][:3] == ('100,00', 'S', '1600')

    submit(db, _mask_post(db, entry, coa_id=coa(db, 6815)))

    assert _accounts_of(db, entry) == (coa(db, 6815), coa(db, 1800), minor('-100.00'), FORM_PURPOSE_FIRST)
    assert datev_rows(db)['AB-1'][:3] == ('100,00', 'S', '6815')


def test_an_amount_the_user_changed_is_the_amount_of_the_new_posting(db):
    db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'AB-1', '')))
    (entry,) = [row[0] for row in query(db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'AB-1'")]

    submit(db, _mask_post(db, entry, coa_id=coa(db, 6815), amount='-80.00'))

    assert _accounts_of(db, entry)[2:] == (minor('-80.00'), FORM_PURPOSE_FIRST)


def test_update_booking_fields_negates_an_amount_that_comes_back_unchanged(db):
    entry = _entry(db, 1600, 1800, '100.00', FORM_MIRROR_COA)

    db.update_booking_fields(entry, coa_id=coa(db, 6815), amount=Decimal('100.00'))

    assert _accounts_of(db, entry)[2:] == (minor('-100.00'), FORM_PURPOSE_FIRST)


def test_update_booking_fields_keeps_an_amount_the_caller_changed(db):
    entry = _entry(db, 1600, 1800, '100.00', FORM_MIRROR_COA)

    db.update_booking_fields(entry, coa_id=coa(db, 6815), amount=Decimal('90.00'))

    assert _accounts_of(db, entry)[2:] == (minor('90.00'), FORM_PURPOSE_FIRST)


# ── kein Satz mit demselben Konto auf beiden Seiten ───────────────────────────

def test_the_editor_refuses_a_new_row_with_the_account_of_the_bank_itself(db):
    bank = _movement(db, '-10.00')
    post, _ = open_form(db, bank)
    before = snapshot(db)

    status, message = handle_add_transaction(db, add_row(post, split_amount='-10.00',
                                                        split_coa=coa(db, 1800)))

    assert status == 400 and 'dasselbe Konto (1800)' in message and 'Zeile 1' in message
    assert snapshot(db) == before


def test_the_editor_refuses_to_turn_an_existing_row_into_the_bank_to_the_bank(db):
    bank = _movement(db, '-10.00')
    row = _child(db, bank, 6815, 1800, '-10.00', FORM_PURPOSE_FIRST)
    post, _ = open_form(db, bank)
    post['split_coa'][post['split_id'].index(str(row))] = str(coa(db, 1800))
    before = snapshot(db)

    status, message = handle_add_transaction(db, post)

    assert status == 400 and 'dasselbe Konto' in message
    assert snapshot(db) == before


def test_the_editor_leaves_a_row_alone_that_is_stored_with_the_same_account_on_both_sides(db):
    """Ein Satz, den ein Fehler früherer Versionen so hinterlassen hat, blockiert das Speichern nicht –
    und wird auch nicht noch weiter verändert."""
    bank = _movement(db, '-10.00')
    _child(db, bank, 1800, 1800, '-10.00', FORM_PURPOSE_FIRST)
    before = snapshot(db)
    post, _ = open_form(db, bank)

    submit(db, post)

    assert snapshot(db) == before


def test_the_mask_refuses_to_put_the_counter_account_into_the_account_field(db):
    entry = _entry(db, 6815, 1800, '-10.00', FORM_PURPOSE_FIRST)
    before = snapshot(db)

    status, message = handle_add_transaction(db, _mask_post(db, entry, coa_id=coa(db, 1800)))

    assert status == 400 and 'dasselbe Konto (1800)' in message
    assert snapshot(db) == before


def test_a_new_bank_movement_with_the_account_of_the_bank_as_the_posting_is_refused(db):
    post = {'transaction_id': ['0'], 'date': ['2026-03-01'], 'amount': ['-10.00'], 'currency': ['EUR'],
            'account': [str(_alpha(db))], 'coa_id': [str(coa(db, 1800))]}
    before = snapshot(db)

    status, message = handle_add_transaction(db, post)

    assert status == 400 and 'dasselbe Konto' in message
    assert snapshot(db) == before


def test_a_bank_movement_without_a_posting_yet_refuses_the_account_of_the_bank_too(db):
    bank = _movement(db, '-10.00')
    post = {'transaction_id': [str(bank)], 'date': ['2026-03-01'], 'amount': ['-10.00'],
            'currency': ['EUR'], 'account': [str(_alpha(db))], 'coa_id': [str(coa(db, 1800))]}
    before = snapshot(db)

    status, message = handle_add_transaction(db, post)

    assert status == 400 and 'dasselbe Konto' in message
    assert snapshot(db) == before
