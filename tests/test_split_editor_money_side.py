# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""„Vorhandene Buchung zuordnen“ überschreibt die Geldseite eines Satzes nicht.

Steht auf der Geldseite eines Satzes schon ein **anderes Zahlungskonto** als das der Bankbewegung (die Kasse, ein
anderes eigenes Bankkonto, das Verrechnungskonto 1460; ``Database.get_payment_coa_ids``), behält der Satz seine Konten
und seine Form: Er sagt, worüber er bezahlt wurde. Die Split-Summe zählt ihn, wie ``bank_effect`` ihn für diese
Bankbewegung zählt. Ein leeres Gegenkonto füllt das Konto der Bank wie bisher. Zuordnen und ✕ lassen einen Satz, der nur
gelöst wird (Import, AfA, Forderung), danach wie vorher dastehen – in jeder Spalte.

Die Formulare stammen aus dem HTML der echten Maske (``tests/entry_form_roundtrip.py``); die Eigenschaft für alle Geldseiten
und Kassenvarianten steht in ``test_entry_form_roundtrip.py``. Alle Namen und Werte sind erfunden.
"""
from decimal import Decimal

import pytest

from tests.entry_form_helpers import (
    bank_account, coa, datev_rows, euer, execute, query, wiso_database, wiso_row,
)
from tests.entry_form_roundtrip import adopt_row, differences, drop_row, editor_rest, open_form, snapshot, submit

RANGE = ('2026-01-01', '2026-12-31')


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Geldseite-Alpha', 1800)
    bank_account(tmp_db, 'Geldseite-Beta', 1810)
    return tmp_db


def _alpha(db):
    return next(a[0] for a in db.fetch_accounts() if a[1] == 'Geldseite-Alpha')


def _movement(db, amount, date='2026-05-04'):
    return db.insert_booking(date, Decimal(amount), account_id=_alpha(db), booking_type='bank',
                             recipient_client='Bank-Empfänger', text='Zuordnungsziel')


def _assign(db, bank, booking_id):
    post, options = open_form(db, bank)
    option = next(o for o in options if o['value'] == str(booking_id))
    submit(db, adopt_row(post, option))


def _remove_row(db, bank, booking_id):
    post, _options = open_form(db, bank)
    submit(db, drop_row(post, booking_id))


def _accounts(db, booking_id):
    names = db.get_coa_id_to_number_map()
    coa_id, counter_id, amount, form = query(
        db, 'SELECT COA_ID, CounterCOA_ID, Amount, EntryForm FROM Bookings WHERE ID = ?', (booking_id,))[0]
    return names.get(coa_id), names.get(counter_id), amount, form


# ── der WISO-Kassensatz ────────────────────────────────────────────────────────

def test_a_wiso_cash_booking_keeps_the_cash_account_when_assigned_and_comes_back_after_the_x(db, tmp_path):
    """„6850 an 1600“ (Ausgabe über die Kasse, mit Quellklammer) einer Bankbewegung auf Giro Alpha zugeordnet:
    Das Gegenkonto bleibt die Kasse, der Satz zählt in der EÜR einmal wie vorher – und nach dem ✕ ist er wieder genau,
    wie er war (er stand vorher in der Kassen-Sektion der EÜR)."""
    path = wiso_database(tmp_path / 'DB1.FDB', [
        wiso_row(4980, 1000, 21.40, 'FD-KA', AMOUNTNET=17.98, TAXRATE=19.0,
                 ACCOUNTING_DATE='2026-05-04 00:00:00')])
    assert db.import_wiso_fdb(path, with_assets=False, with_invoices=False)['imported'] == 1
    (booking,) = [row[0] for row in query(db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'FD-KA'")]
    bank = _movement(db, '-21.40')
    before = snapshot(db)
    euer_before, datev_before = euer(db), datev_rows(db)
    assert euer_before == {6850: Decimal('-17.98'), 1406: Decimal('-3.42')}      # die Kassen-Sektion
    accounts_before = _accounts(db, booking)
    assert accounts_before[:2] == (6850, 1600)

    _assign(db, bank, booking)

    assert _accounts(db, booking) == accounts_before                  # Kasse bleibt, Form bleibt
    assert euer(db) == euer_before and datev_rows(db) == datev_before     # einmal, nicht doppelt und nicht weg
    assert db.find_unbalanced_splits(*RANGE) == [] and editor_rest(db, bank) == 0.0

    _remove_row(db, bank, booking)

    assert differences(before, snapshot(db)) == []                    # jede Spalte jeder Buchung wie vorher
    assert euer(db) == euer_before and datev_rows(db) == datev_before


@pytest.mark.parametrize('counter, counted', [
    pytest.param(1600, True, id='kasse'),
    pytest.param(1810, True, id='andere-bank'),
    pytest.param(1460, False, id='verrechnungskonto'),
])
def test_the_split_sum_counts_a_kept_booking_the_way_bank_effect_does(db, counter, counted):
    """Eigene Konten (Kasse, andere Bank) bewegen Geld und zählen mit; das Verrechnungskonto ist kein eigenes Konto,
    der Satz dann nicht bankwirksam: Der Editor zeigt den vollen Rest, und die Prüfung meldet die Bewegung."""
    booking = db.insert_booking('2026-05-04', Decimal('-119.00'), coa_id=coa(db, 6815),
                                counter_coa_id=coa(db, counter), booking_type='entry')
    bank = _movement(db, '-119.00')

    _assign(db, bank, booking)

    assert _accounts(db, booking)[:2] == (6815, counter)
    expected_rest = 0.0 if counted else -119.0
    assert editor_rest(db, bank) == expected_rest
    unbalanced = db.find_unbalanced_splits(*RANGE)
    assert [row[0] for row in unbalanced] == ([] if counted else [bank])
    if not counted:
        assert unbalanced[0][3] == Decimal('-119.0000')


def test_a_second_save_keeps_the_other_payment_account(db):
    """Auch ein Satz, der schon unter der Bankbewegung hängt, behält sein Zahlungskonto beim Speichern."""
    booking = db.insert_booking('2026-05-04', Decimal('-119.00'), coa_id=coa(db, 6815),
                                counter_coa_id=coa(db, 1600), booking_type='entry')
    bank = _movement(db, '-119.00')
    _assign(db, bank, booking)

    for _ in range(2):
        post, _options = open_form(db, bank)
        submit(db, post)

    assert _accounts(db, booking)[:2] == (6815, 1600)


def test_an_empty_counter_account_is_filled_with_the_account_of_the_bank(db):
    """Wie bisher: Ein Satz ohne Gegenkonto bekommt das Konto der Bankbewegung."""
    booking = db.insert_booking('2026-05-04', Decimal('-119.00'), coa_id=coa(db, 6815),
                                booking_type='entry')
    bank = _movement(db, '-119.00')

    _assign(db, bank, booking)

    assert _accounts(db, booking)[:2] == (6815, 1800)


def test_the_bank_of_the_movement_as_counter_account_stays(db):
    booking = db.insert_booking('2026-05-04', Decimal('-119.00'), coa_id=coa(db, 6815),
                                counter_coa_id=coa(db, 1800), booking_type='entry')
    bank = _movement(db, '-119.00')
    before = snapshot(db)

    _assign(db, bank, booking)

    assert differences(before, snapshot(db), ignore=('ParentBooking_ID',)) == []


def test_assigning_and_the_x_leave_empty_columns_empty(db):
    """Auch leere Spalten bleiben, wie sie sind – NULL wird nicht zu '' und umgekehrt, eine fehlende Währung nicht zu
    EUR: Der Editor schreibt von den Stammdaten eines zugeordneten Satzes nichts, und von Text und Beleg-Nr. nur, was
    die Zeile geändert hat. Verglichen werden die rohen Spalten, ohne dass leer und NULL gleichgesetzt werden."""
    booking = db.insert_booking('2026-05-04', Decimal('-119.00'), coa_id=coa(db, 6815),
                                counter_coa_id=coa(db, 1800), booking_type='entry')
    execute(db, "UPDATE Bookings SET Text = NULL, DocumentNumber = '', Currency = NULL, RecipientClient = NULL, "
                "SourceGroup = 'sg-leer' WHERE ID = ?", (booking,))
    bank = _movement(db, '-119.00')
    before = query(db, 'SELECT * FROM Bookings WHERE ID = ?', (booking,))

    _assign(db, bank, booking)
    _remove_row(db, bank, booking)

    assert query(db, 'SELECT * FROM Bookings WHERE ID = ?', (booking,)) == before


# ── die Forderung ────────────────────────────────────────────────────────────────

def test_a_receivable_attached_by_an_old_matching_comes_back_as_it_was(db):
    """Altbestand: Die Forderung („10000 an Wartekonto“) hängt als aufgelöstes Kind an einer Bankbewegung. Der Editor
    lässt sie beim Speichern, wie sie ist; das ✕ löst sie, und sie steht wieder da wie vor dem Anhängen."""
    invoice = db.insert_invoice({
        'invoice_number': 'RE-GELD', 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19,
        'sum_net': 100.00, 'tax_amount': 19.00, 'sum_gross': 119.00, 'amount_due': 119.00,
        'status': 'sent'})
    db.ensure_receivable_booking(invoice)
    (receivable,) = [row[0] for row in query(
        db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'RE-GELD' AND BookingType = 'entry'")]
    bank = db.insert_booking('2026-02-01', Decimal('119.00'), account_id=_alpha(db), booking_type='bank',
                             recipient_client='Käufer AG')
    before = snapshot(db)
    execute(db, "UPDATE Bookings SET ParentBooking_ID = ?, Status = 'resolved' WHERE ID = ?",
            (bank, receivable))
    attached = snapshot(db)

    post, _options = open_form(db, bank)
    submit(db, post)

    assert differences({receivable: attached[receivable]}, {receivable: snapshot(db)[receivable]}) == []

    _remove_row(db, bank, receivable)

    assert differences({receivable: before[receivable]}, {receivable: snapshot(db)[receivable]}) == []
