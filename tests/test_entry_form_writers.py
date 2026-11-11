# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Jeder Schreibweg setzt die Form des Buchungssatzes (``Bookings.EntryForm``).

Maske (neu, bearbeitet, Spiegel), Buchungssatz-Editor der Bankmaske (Zahlung, Umbuchung, Geldkonto
vorne, Spiegel), Rechnung (Forderung, Zahlung mit und ohne Forderung), AfA und die Schreibfunktionen der
Datenbank selbst (``insert_booking``, ``update_booking_fields``, ``sync_entry_child``,
``backfill_booking_fields``). Die Importe prüft test_entry_form_imports.py.

Die Form steht beim Schreiben fest: Sie wird mit den Zahlungskonten von damals bestimmt und danach
nicht mehr aus der Kontenliste von heute geraten. Alle Namen und Werte sind erfunden.
"""
from decimal import Decimal

import pytest

from db.matching import (
    FORM_LIQUID_FIRST, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY,
    FORM_PURPOSE_FIRST, FORM_TRANSFER,
)
from server.handlers import handle_add_transaction, link_booking_to_invoice_capped
from tests.entry_form_helpers import (
    bank_account, cash_account_id, children, coa, datev_rows, euer, execute, form_of, minor,
    numbers, query,
)


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Form-Bank-Eins', 1800)
    bank_account(tmp_db, 'Form-Bank-Zwei', 1810)
    return tmp_db


def _account(db, name='Form-Bank-Eins'):
    return next(a[0] for a in db.fetch_accounts() if a[1] == name)


def _post(db, coa_number, amount, account='Form-Bank-Eins', rate=None, tax='', transaction_id=0,
          date='2026-03-01', **extra):
    post = {'transaction_id': [str(transaction_id)], 'date': [date], 'amount': [amount],
            'currency': ['EUR'], 'coa_id': [str(coa(db, coa_number)) if coa_number else ''],
            'tax_rate': [f'{rate * 100:g}' if rate else ''], 'tax_amount': [tax]}
    if account:
        post['account'] = [str(_account(db, account))]
    post.update({key: [value] for key, value in extra.items()})
    return post


def _bank_rows(db):
    return [row[0] for row in query(db, "SELECT ID FROM Bookings WHERE BookingType = 'bank' "
                                        "ORDER BY ID")]


# ── Buchungsmaske ────────────────────────────────────────────────────────────

def test_mask_new_bank_booking_writes_a_purpose_first_child_and_no_form_on_the_bank_row(db):
    assert handle_add_transaction(db, _post(db, 6815, '-59.50', rate=0.19, tax='-9.50'))[0] == 303
    bank, = _bank_rows(db)
    assert form_of(db, bank) is None
    (child_id, konto, gegenkonto, amount, form), = children(db, bank)
    assert (konto, gegenkonto, amount, form) == (6815, 1800, minor('-59.50'), FORM_PURPOSE_FIRST)
    assert query(db, 'SELECT AutoMirror FROM Bookings WHERE ID = ?', (child_id,)) == [(1,)]


def test_mask_with_the_cash_account_as_purpose_writes_a_mirror_on_the_counter_account(db):
    """Abhebung von der Bank in die Kasse: Der Betrag ist der der Bankbewegung, die Richtung auf dem
    Gegenkonto (der Bank) – ``mirror_counter``, wie die Maske ihn immer geschrieben hat."""
    assert handle_add_transaction(db, _post(db, 1600, '-100.00'))[0] == 303
    bank, = _bank_rows(db)
    (_id, konto, gegenkonto, amount, form), = children(db, bank)
    assert (konto, gegenkonto, amount, form) == (1600, 1800, minor('-100.00'), FORM_MIRROR_COUNTER)


def test_mask_edit_writes_the_form_of_the_mirror_child_again(db):
    handle_add_transaction(db, _post(db, 6815, '-100.00'))
    bank, = _bank_rows(db)
    child_id = children(db, bank)[0][0]
    assert form_of(db, child_id) == FORM_PURPOSE_FIRST

    handle_add_transaction(db, _post(db, 1600, '-100.00', transaction_id=bank))
    assert (numbers(db, child_id), form_of(db, child_id)) == (
        (1600, 1800, minor('-100.00')), FORM_MIRROR_COUNTER)

    handle_add_transaction(db, _post(db, 4400, '-100.00', transaction_id=bank))
    assert (numbers(db, child_id), form_of(db, child_id)) == (
        (4400, 1800, minor('-100.00')), FORM_PURPOSE_FIRST)


def test_mask_standalone_entry_without_a_bank_account_has_the_purpose_first(db):
    assert handle_add_transaction(db, _post(db, 6815, '-12.00', account=None))[0] == 303
    entry, = [row[0] for row in query(db, "SELECT ID FROM Bookings WHERE BookingType = 'entry'")]
    assert (numbers(db, entry), form_of(db, entry)) == ((6815, None, minor('-12.00')),
                                                        FORM_PURPOSE_FIRST)


def test_mask_edit_of_a_money_first_entry_keeps_the_account_in_front_by_its_form(db):
    """Die Maske zeigt bei „Geldkonto vorne“ das Geldkonto als Konto; die Wahl des Nutzers ist das
    Zweckkonto und geht ins Gegenkonto. Ob der Satz so steht, sagt seine Form – hier ein Konto, das
    kein Geldkonto ist und trotzdem vorne steht (Altbestand)."""
    execute(db, "INSERT INTO ChartOfAccounts (ID, Framework, AccountNumber, Name, Description) "
                "VALUES (400050, 4, 50, 'Altkonto', '')")
    entry = db.insert_booking('2026-03-10', Decimal('119.00'), coa_id=coa(db, 50),
                              counter_coa_id=coa(db, 4400), booking_type='entry',
                              entry_form=FORM_LIQUID_FIRST)
    assert coa(db, 50) not in db.get_payment_coa_ids()

    handle_add_transaction(db, _post(db, 4300, '119.00', account=None, transaction_id=entry))

    assert (numbers(db, entry), form_of(db, entry)) == ((50, 4300, minor('119.00')),
                                                       FORM_LIQUID_FIRST)


def test_mask_edit_of_a_row_that_only_looks_money_first_does_not_swap_the_accounts(db):
    """Dasselbe Bild mit gespeicherter Form „Umbuchung“: Das Konto vorne ist keine Geldseite, die
    Wahl des Nutzers ersetzt es."""
    entry = db.insert_booking('2026-03-10', Decimal('119.00'), coa_id=coa(db, 1800),
                              counter_coa_id=coa(db, 4400), booking_type='entry',
                              entry_form=FORM_TRANSFER)

    handle_add_transaction(db, _post(db, 4300, '119.00', account=None, transaction_id=entry))

    assert numbers(db, entry)[:2] == (4300, 4400)
    assert form_of(db, entry) == FORM_TRANSFER


# ── Buchungssatz-Editor der Bankmaske ────────────────────────────────────────

def _editor(db, bank, rows):
    """Post einer bestehenden Bankbewegung mit den Zeilen des Editors; ``rows``:
    [(id, Zweckkonto, Betrag, Gegenkonto)]."""
    amount = query(db, 'SELECT Amount FROM Bookings WHERE ID = ?', (bank,))[0][0]
    post = {'transaction_id': [str(bank)], 'date': ['2026-03-01'],
            'amount': [str(Decimal(amount) / 10000)], 'currency': ['EUR'],
            'account': [str(_account(db))],
            'split_id': [str(r[0] or '') for r in rows],
            'split_coa': [str(coa(db, r[1]) or '') for r in rows],
            'split_amount': [r[2] for r in rows],
            'split_tax_rate': ['' for _ in rows], 'split_tax_amount': ['' for _ in rows],
            'split_docnr': ['' for _ in rows], 'split_text': ['' for _ in rows],
            'split_counter_coa': [str(coa(db, r[3]) or '') for r in rows]}
    return post


def test_editor_new_rows_are_payments_and_transfers(db):
    bank = db.insert_booking('2026-03-01', Decimal('-100.00'), account_id=_account(db),
                             booking_type='bank')
    post = _editor(db, bank, [(None, 6815, '-70.00', None), (None, 6850, '-30.00', None),
                              (None, 6805, '7.50', 2100)])
    assert handle_add_transaction(db, post)[0] == 303

    rows = children(db, bank)
    assert [(r[1], r[2], r[4]) for r in rows] == [
        (6815, 1800, FORM_PURPOSE_FIRST), (6850, 1800, FORM_PURPOSE_FIRST),
        (6805, 2100, FORM_TRANSFER)]                 # der Privatanteil ist eine Umbuchung


def test_editor_row_with_the_cash_account_is_a_mirror_on_the_bank(db):
    bank = db.insert_booking('2026-03-01', Decimal('-100.00'), account_id=_account(db),
                             booking_type='bank')
    handle_add_transaction(db, _editor(db, bank, [(None, 1600, '-100.00', None)]))

    (_id, konto, gegenkonto, amount, form), = children(db, bank)
    assert (konto, gegenkonto, amount, form) == (1600, 1800, minor('-100.00'), FORM_MIRROR_COUNTER)


def test_editor_keeps_the_orientation_and_the_form_of_a_money_first_row(db):
    """Geldkonto vorne (Einnahme aus dem WISO-Import): Der Editor schreibt das Zweckkonto ins
    Gegenkonto und lässt das Geldkonto vorne – die Form bleibt ``liquid_first``."""
    bank = db.insert_booking('2026-03-01', Decimal('119.00'), account_id=_account(db),
                             booking_type='bank')
    row = db.insert_booking('2026-03-01', Decimal('119.00'), coa_id=coa(db, 1800),
                            counter_coa_id=coa(db, 4400), booking_type='entry',
                            parent_booking_id=bank)
    assert form_of(db, row) == FORM_LIQUID_FIRST

    handle_add_transaction(db, _editor(db, bank, [(row, 4300, '119.00', None)]))

    assert (numbers(db, row), form_of(db, row)) == ((1800, 4300, minor('119.00')),
                                                   FORM_LIQUID_FIRST)


def test_editor_keeps_a_transfer_a_transfer(db):
    bank = db.insert_booking('2026-03-01', Decimal('-100.00'), account_id=_account(db),
                             booking_type='bank')
    handle_add_transaction(db, _editor(db, bank, [(None, 6815, '-100.00', None),
                                                  (None, 6805, '7.50', 2100)]))
    transfer = children(db, bank)[1][0]

    handle_add_transaction(db, _editor(db, bank, [(children(db, bank)[0][0], 6815, '-100.00', None),
                                                  (transfer, 6850, '7.50', 2100)]))

    assert (numbers(db, transfer), form_of(db, transfer)) == ((6850, 2100, minor('7.50')),
                                                             FORM_TRANSFER)


def test_editor_leaves_a_mirror_row_alone_and_does_not_delete_it(db):
    bank = db.insert_booking('2026-03-01', Decimal('-100.00'), account_id=_account(db),
                             booking_type='bank')
    mirror = db.insert_booking('2026-03-01', Decimal('-100.00'), coa_id=coa(db, 1600),
                               counter_coa_id=coa(db, 1800), booking_type='entry',
                               parent_booking_id=bank)
    assert form_of(db, mirror) == FORM_MIRROR_COUNTER

    handle_add_transaction(db, _editor(db, bank, [(None, 6815, '-50.00', None)]))

    assert form_of(db, mirror) == FORM_MIRROR_COUNTER
    assert (numbers(db, mirror), len(children(db, bank))) == ((1600, 1800, minor('-100.00')), 2)


def test_editor_reads_a_legacy_mirror_as_before_only_two_own_accounts_make_it_a_mirror(db):
    """``mirror_legacy`` heißt „Lesart unbekannt“ und wird in dieser Version nur so gelesen wie
    vorher: Ein Spiegel zweier eigener Konten gehört nicht in den Editor und bleibt stehen, einer
    mit dem Verrechnungskonto (kein eigenes Konto) ist für den Editor ein gewöhnlicher Satz."""
    bank = db.insert_booking('2026-03-01', Decimal('-100.00'), account_id=_account(db),
                             booking_type='bank')
    between_banks = db.insert_booking('2026-03-01', Decimal('-100.00'), coa_id=coa(db, 1810),
                                      counter_coa_id=coa(db, 1800), booking_type='entry',
                                      parent_booking_id=bank, entry_form=FORM_MIRROR_LEGACY)
    with_clearing = db.insert_booking('2026-03-01', Decimal('100.00'), coa_id=coa(db, 1460),
                                      counter_coa_id=coa(db, 1800), booking_type='entry',
                                      parent_booking_id=bank, entry_form=FORM_MIRROR_LEGACY)

    handle_add_transaction(db, _editor(db, bank, [(None, 6815, '-100.00', None)]))

    remaining = {row[0] for row in children(db, bank)}
    assert between_banks in remaining
    assert with_clearing not in remaining            # wie in 2.0.x: nicht im Editor, also entfernt
    assert form_of(db, between_banks) == FORM_MIRROR_LEGACY


# ── Rechnung und AfA ─────────────────────────────────────────────────────────

def _invoice(db, number, gross, rate=0.19, status='sent'):
    net = round(gross / (1 + rate), 2) if rate > 0 else gross
    return db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': rate,
        'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross,
        'amount_due': gross, 'status': status})


def test_the_receivable_is_a_transfer(db):
    invoice = _invoice(db, 'RE-1', 119.00)
    receivable = db.ensure_receivable_booking(invoice)
    assert (numbers(db, receivable), form_of(db, receivable)) == (
        (10000, 4405, minor('119.00')), FORM_TRANSFER)


def test_payment_with_a_receivable_writes_money_first_and_a_transfer(db):
    invoice = _invoice(db, 'RE-2', 119.00)
    db.ensure_receivable_booking(invoice)
    bank = db.insert_booking('2026-02-01', Decimal('119.00'), account_id=_account(db),
                             booking_type='bank', recipient_client='Käufer AG')

    ok, message = link_booking_to_invoice_capped(db, invoice, bank)

    assert ok, message
    assert [(r[1], r[2], r[4]) for r in children(db, bank)] == [
        (1800, 10000, FORM_LIQUID_FIRST),              # Bank an Debitoren
        (4405, 4400, FORM_TRANSFER)]                   # Wartekonto an Erlöskonto


def test_payment_without_a_receivable_writes_the_revenue_account_first(db):
    invoice = _invoice(db, 'RE-3', 105.00, rate=0.05)       # 5 %: kein Wartekonto, keine Forderung
    assert db.ensure_receivable_booking(invoice) is None
    bank = db.insert_booking('2026-02-01', Decimal('105.00'), account_id=_account(db),
                             booking_type='bank', recipient_client='Käufer AG')

    ok, message = link_booking_to_invoice_capped(db, invoice, bank, revenue_coa_id=coa(db, 4300))

    assert ok, message
    (_id, konto, gegenkonto, amount, form), = children(db, bank)
    assert (konto, gegenkonto, amount, form) == (4300, 1800, minor('105.00'), FORM_PURPOSE_FIRST)


def test_depreciation_is_a_transfer(db):
    asset = db.insert_asset('Rechner', '2026-01-02', 1200.0, 3)
    db.book_depreciation(asset, 2026, coa_id_expense=coa(db, 6220), coa_id_asset=coa(db, 520))
    row, = query(db, "SELECT ID FROM Bookings WHERE Status = 'posted'")
    assert (numbers(db, row[0])[:2], form_of(db, row[0])) == ((6220, 520), FORM_TRANSFER)


# ── die Schreibfunktionen der Datenbank ──────────────────────────────────────

def test_insert_booking_derives_the_form_from_the_payment_accounts_of_the_moment(db):
    """Ohne Angabe: Zweckkonto vorne, ein Spiegel mit Betrag auf dem Gegenkonto – wie die Maske."""
    for coa_nr, counter_nr, expected in [
            (6815, 1800, FORM_PURPOSE_FIRST), (1800, 4400, FORM_LIQUID_FIRST),
            (6815, 4400, FORM_TRANSFER), (1800, 1810, FORM_MIRROR_COUNTER),
            (6815, None, FORM_PURPOSE_FIRST), (None, None, FORM_PURPOSE_FIRST),
            (None, 6815, FORM_LIQUID_FIRST), (6815, 1460, FORM_PURPOSE_FIRST)]:
        booking = db.insert_booking('2026-03-01', Decimal('10.00'), coa_id=coa(db, coa_nr),
                                    counter_coa_id=coa(db, counter_nr), booking_type='entry')
        assert form_of(db, booking) == expected, (coa_nr, counter_nr)


def test_insert_booking_takes_an_explicit_form_as_it_is(db):
    booking = db.insert_booking('2026-03-01', Decimal('10.00'), coa_id=coa(db, 1800),
                                counter_coa_id=coa(db, 1810), booking_type='entry',
                                entry_form=FORM_MIRROR_COA)
    assert form_of(db, booking) == FORM_MIRROR_COA


def test_insert_booking_rejects_an_unknown_form_and_gives_a_bank_row_none(db):
    with pytest.raises(ValueError):
        db.insert_booking('2026-03-01', 1, coa_id=coa(db, 6815), booking_type='entry',
                          entry_form='mirror')
    bank = db.insert_booking('2026-03-01', 1, account_id=_account(db), booking_type='bank',
                             entry_form=FORM_TRANSFER)
    assert form_of(db, bank) is None


def _entry(db, coa_nr, counter_nr, amount, form=None):
    return db.insert_booking('2026-03-01', Decimal(amount), coa_id=coa(db, coa_nr),
                             counter_coa_id=coa(db, counter_nr), booking_type='entry',
                             entry_form=form)


def test_changing_the_accounts_keeps_the_soll_haben_of_the_amount(db):
    """Aufwand an Bank (−50: 6815 im Soll) wird zur Umbuchung 6815 / 4400: Eine Umbuchung zählt
    positiv als „Konto im Soll“, also kippt der Betrag – 6815 bleibt im Soll."""
    booking = _entry(db, 6815, 1800, '-50.00')
    assert form_of(db, booking) == FORM_PURPOSE_FIRST

    db.update_booking_fields(booking, counter_coa_id=coa(db, 4400))

    assert (numbers(db, booking), form_of(db, booking)) == ((6815, 4400, minor('50.00')),
                                                           FORM_TRANSFER)


def test_an_amount_given_with_the_new_accounts_is_not_negated(db):
    booking = _entry(db, 6815, 1800, '-50.00')

    db.update_booking_fields(booking, counter_coa_id=coa(db, 4400), amount=Decimal('50.00'))

    assert (numbers(db, booking), form_of(db, booking)) == ((6815, 4400, minor('50.00')),
                                                           FORM_TRANSFER)


def test_an_explicit_form_wins_over_the_derived_one(db):
    booking = _entry(db, 6815, 1800, '-50.00')

    db.update_booking_fields(booking, counter_coa_id=coa(db, 4400), entry_form=FORM_PURPOSE_FIRST)

    assert (numbers(db, booking), form_of(db, booking)) == ((6815, 4400, minor('-50.00')),
                                                           FORM_PURPOSE_FIRST)


def test_editing_other_fields_or_the_same_accounts_leaves_form_and_amount_alone(db):
    booking = _entry(db, 6815, 1800, '-50.00', form=FORM_LIQUID_FIRST)        # absichtlich anders

    db.update_booking_fields(booking, text='nur der Text')
    db.update_booking_fields(booking, coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800))

    assert (numbers(db, booking), form_of(db, booking)) == ((6815, 1800, minor('-50.00')),
                                                           FORM_LIQUID_FIRST)


def test_a_legacy_mirror_stays_a_legacy_mirror_when_its_money_accounts_change(db):
    booking = _entry(db, 1800, 1460, '100.00', form=FORM_MIRROR_LEGACY)

    db.update_booking_fields(booking, coa_id=coa(db, 1810))

    assert (numbers(db, booking), form_of(db, booking)) == ((1810, 1460, minor('100.00')),
                                                           FORM_MIRROR_LEGACY)


def test_a_bank_row_gets_no_form_when_its_account_changes(db):
    bank = db.insert_booking('2026-03-01', 1, account_id=_account(db), booking_type='bank')

    db.update_booking_fields(bank, coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800))

    assert form_of(db, bank) is None


def test_update_booking_recomputes_the_form_like_update_booking_fields(db):
    booking = _entry(db, 6815, 1800, '-50.00')

    db.update_booking(booking, '2026-03-01', 50.0, coa_id=coa(db, 6815),
                      counter_coa_id=coa(db, 4400))

    assert (numbers(db, booking), form_of(db, booking)) == ((6815, 4400, minor('50.00')),
                                                           FORM_TRANSFER)


def test_sync_entry_child_writes_the_form_of_the_mask(db):
    bank = db.insert_booking('2026-03-01', Decimal('-100.00'), account_id=_account(db),
                             booking_type='bank')
    child = db.insert_booking('2026-03-01', Decimal('-100.00'), coa_id=coa(db, 6815),
                              counter_coa_id=coa(db, 1800), booking_type='entry',
                              parent_booking_id=bank, auto_mirror=True)
    for coa_nr, expected in [(1600, FORM_MIRROR_COUNTER), (6815, FORM_PURPOSE_FIRST)]:
        db.sync_entry_child(child, '2026-03-01', None, Decimal('-100.00'), 'X', coa(db, coa_nr),
                            coa(db, 1800), 'EUR', None, None, 'Text', None)
        assert form_of(db, child) == expected


def test_backfill_gives_an_entry_without_a_front_account_its_form(db):
    entry = db.insert_booking('2026-03-01', Decimal('-50.00'), counter_coa_id=coa(db, 1800),
                              booking_type='entry')
    assert form_of(db, entry) == FORM_PURPOSE_FIRST                 # nichts kontiert

    db.backfill_booking_fields(entry, coa_id=coa(db, 6815))

    assert (numbers(db, entry), form_of(db, entry)) == ((6815, 1800, minor('-50.00')),
                                                       FORM_PURPOSE_FIRST)


def test_backfill_writes_the_account_the_form_and_the_negated_amount_into_the_sql_log(db):
    """Die ganze Änderung steht als EINE Anweisung im Protokoll: Konto, Form und Betrag – sonst ließe sich
    der Stand nicht nachspielen."""
    import os
    import userctx
    entry = db.insert_booking('2026-03-01', Decimal('50.00'), counter_coa_id=coa(db, 1800),
                              booking_type='entry', entry_form=FORM_MIRROR_COA)
    log = os.path.join(userctx.user_data_dir(), 'sql_operations.sql')
    before = len(open(log, encoding='utf-8').read().splitlines()) if os.path.exists(log) else 0

    db.backfill_booking_fields(entry, coa_id=coa(db, 6815))

    assert (numbers(db, entry), form_of(db, entry)) == ((6815, 1800, minor('-50.00')),
                                                       FORM_PURPOSE_FIRST)
    new = open(log, encoding='utf-8').read().splitlines()[before:]
    statement = ' '.join(' '.join(new).split())
    assert len(new) == 1 and statement.startswith('UPDATE Bookings SET COA_ID = COALESCE(COA_ID, ')
    assert "EntryForm = COALESCE('purpose_first', EntryForm)" in statement
    assert f'Amount = COALESCE({minor("-50.00")}, Amount) WHERE ID = {entry};' in statement


def test_backfill_that_changes_no_account_writes_nothing_into_the_sql_log(db):
    import os
    import userctx
    entry = db.insert_booking('2026-03-01', Decimal('-50.00'), coa_id=coa(db, 6815),
                              counter_coa_id=coa(db, 1800), booking_type='entry')
    log = os.path.join(userctx.user_data_dir(), 'sql_operations.sql')
    before = len(open(log, encoding='utf-8').read().splitlines()) if os.path.exists(log) else 0

    db.backfill_booking_fields(entry, coa_id=coa(db, 4400), document_number='X-1')

    assert (numbers(db, entry), form_of(db, entry)) == ((6815, 1800, minor('-50.00')),
                                                       FORM_PURPOSE_FIRST)
    after = len(open(log, encoding='utf-8').read().splitlines()) if os.path.exists(log) else 0
    assert after == before


def test_a_row_the_auto_match_turns_into_a_bank_row_loses_its_form(db):
    """Schritt 0 von ``link_bank_to_entries``: Ein ``entry`` mit Bankkonto und ohne jedes Konto ist in
    Wahrheit eine Bankbewegung. Eine Bankzeile hat keine Form."""
    row = db.insert_booking('2026-03-01', Decimal('-10.00'), account_id=_account(db),
                            booking_type='entry')
    assert form_of(db, row) == FORM_PURPOSE_FIRST                    # nichts kontiert

    db.link_bank_to_entries()

    assert query(db, 'SELECT BookingType, EntryForm FROM Bookings WHERE ID = ?', (row,)) == [
        ('bank', None)]


def test_backfill_on_a_bank_row_changes_no_form(db):
    bank = db.insert_booking('2026-03-01', 1, account_id=_account(db), booking_type='bank')
    db.backfill_booking_fields(bank, coa_id=coa(db, 4400))
    assert form_of(db, bank) is None


# ── Zeilen ohne Form (Roh-SQL) ───────────────────────────────────────────────

def test_a_row_without_a_form_is_read_with_the_payment_accounts_of_today(db):
    """Roh angelegt (``EntryForm`` leer): EÜR und DATEV ordnen mit der Kontenliste von jetzt ein –
    dieselbe Lesart wie bevor es die Form gab."""
    for number, (coa_nr, counter_nr) in enumerate([(6815, 1800), (1800, 4400), (1800, 1810)], 1):
        execute(db, "INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, "
                    "DocumentNumber, BookingType) VALUES ('2026-03-10', ?, ?, ?, ?, 'entry')",
                (coa(db, coa_nr), coa(db, counter_nr), minor('119.00'), f'RAW-{number}'))
    assert query(db, 'SELECT COUNT(*) FROM Bookings WHERE EntryForm IS NULL') == [(3,)]

    rows = datev_rows(db)

    assert rows['RAW-1'][1] == 'H'          # Zweckkonto vorne: positiv = Konto im Haben
    assert rows['RAW-2'][1] == 'S'          # Geldkonto vorne: positiv = Konto im Soll
    assert rows['RAW-3'][1] == 'H'          # Spiegel: wie vor der Form (Gegenkonto-Seite)
    assert euer(db) == {}                   # ohne Bankbewegung steht davon nichts in der EÜR
