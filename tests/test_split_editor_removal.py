# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Zeile im Buchungssatz-Editor der Bankbewegung entfernen (✕ und speichern).

Was nachweislich nicht im Editor entstanden ist, wird **abgehängt statt gelöscht** (``Database.remove_split_row``):
ein importierter Satz (``SourceGroup``) und eine AfA-Buchung. ``ParentBooking_ID`` wird leer, ein vom Abgleich
gesetzter Status ``resolved`` zurückgesetzt, jede andere Spalte bleibt. Der Satz ist danach wieder eine
eigenständige Buchung – in der Liste, als Kandidat des Zuordnen-Widgets, in der EÜR und im DATEV-Export wie vor
dem Zuordnen; das AfA-Jahr bleibt gebucht, und der nächste Abgleich findet den Importsatz wieder.

Alles andere wird wie bisher gelöscht: eine im Editor angelegte Zeile, eine von Hand angelegte und später
zugeordnete Buchung (von im Editor angelegten Zeilen nicht zu unterscheiden, ``docs/aufgaben.md``) und ein markierter
Zahlungssatz mit seiner ganzen Zuordnung. ``delete_transaction`` – eine Buchung ausdrücklich aus der Liste löschen –
bleibt unverändert.

Die Formulare stammen aus dem HTML der echten Maske (``tests/entry_form_roundtrip.py``). Alle Namen und Werte sind erfunden.
"""
from decimal import Decimal

import pytest

from server.handlers import link_booking_to_invoice_capped
from server.pages_transactions import PageTransactions
from tests.dom_helpers import Dom
from tests.entry_form_helpers import (
    bank_account, coa, datev_rows, euer, execute, query, wiso_database, wiso_row,
)
from tests.entry_form_roundtrip import (
    add_row, adopt_row, differences, drop_row, editor_rest, open_form, snapshot, submit,
)

RANGE = ('2026-01-01', '2026-12-31')


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Entfernen-Bank', 1800)
    return tmp_db


def _giro(db):
    return next(a[0] for a in db.fetch_accounts() if a[1] == 'Entfernen-Bank')


def _movement(db, amount, date='2026-08-01'):
    return db.insert_booking(date, Decimal(amount), account_id=_giro(db), booking_type='bank',
                             recipient_client='Bank-Empfänger', text='Zuordnungsziel')


def _wiso_booking(db, tmp_path, number='FD-BA', date='2026-05-06', gross=77.0, net=64.71):
    """Ein Satz „6850 an 1800“ aus der WISO-Datenbank (mit Quellklammer), 19 %."""
    path = wiso_database(tmp_path / f'{number}.FDB', [
        wiso_row(4980, 1200, gross, number, AMOUNTNET=net, TAXRATE=19.0,
                 ACCOUNTING_DATE=f'{date} 00:00:00')])
    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)
    assert result['imported'] == 1 and not result['errors'], result
    (booking,) = [row[0] for row in query(db, 'SELECT ID FROM Bookings WHERE DocumentNumber = ?',
                                          (number,))]
    assert query(db, 'SELECT SourceGroup FROM Bookings WHERE ID = ?', (booking,))[0][0]
    return booking


def _depreciation_booking(db):
    """Die AfA-Buchung eines Jahres (31.12., Umbuchung „6220 an 520“) und die Anlage dazu."""
    asset = db.insert_asset('Rechner', '2026-01-02', 1200.0, 3)
    booking = db.book_depreciation(asset, 2026, coa_id_expense=coa(db, 6220), coa_id_asset=coa(db, 520))
    return asset, booking


def _assign(db, bank, booking_id):
    post, options = open_form(db, bank)
    option = next(o for o in options if o['value'] == str(booking_id))
    submit(db, adopt_row(post, option))


def _remove_row(db, bank, booking_id):
    """✕ in der Zeile und speichern."""
    post, _options = open_form(db, bank)
    submit(db, drop_row(post, booking_id))


def _add_child(db, bank, amount, text):
    post, _options = open_form(db, bank)
    submit(db, add_row(post, split_amount=amount, split_coa=str(coa(db, 6815)), split_text=text))


def _children(db, bank):
    return [row[0] for row in query(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID',
                                    (bank,))]


def _row(db, booking_id):
    return query(db, 'SELECT ParentBooking_ID, Status FROM Bookings WHERE ID = ?', (booking_id,))


def _exists(db, booking_id):
    return bool(query(db, 'SELECT 1 FROM Bookings WHERE ID = ?', (booking_id,)))


def _in_the_list(db, booking_id):
    return any(item['type'] == 'normal' and item['booking'][0] == booking_id
               for item in db.fetch_bookings_grouped(*RANGE))


def _is_candidate(db, booking_id):
    return booking_id in {row[0] for row in db.get_unlinked_entry_bookings(limit=500)}


# ── abgehängt: Import und AfA ─────────────────────────────────────────────────

@pytest.mark.parametrize('kind', ['wiso', 'afa'])
def test_removing_the_row_of_an_assigned_import_or_depreciation_booking_detaches_it(db, tmp_path, kind):
    if kind == 'wiso':
        booking = _wiso_booking(db, tmp_path)
        bank = _movement(db, '-77.00', date='2026-05-06')
    else:
        asset, booking = _depreciation_booking(db)
        bank = _movement(db, '-100.00')
    before = snapshot(db)
    euer_before, datev_before = euer(db), datev_rows(db)
    plan_before = query(db, 'SELECT * FROM AssetDepreciations ORDER BY ID')
    assert _is_candidate(db, booking) and _in_the_list(db, booking)

    _assign(db, bank, booking)
    assert _row(db, booking)[0][0] == bank and not _is_candidate(db, booking)

    _remove_row(db, bank, booking)

    assert _exists(db, booking)                                          # nicht gelöscht
    assert differences(before, snapshot(db)) == []                       # jede Spalte jeder Buchung wie vorher
    assert _children(db, bank) == []
    assert _is_candidate(db, booking) and _in_the_list(db, booking)      # wieder eigenständig
    _post, options = open_form(db, bank)
    assert str(booking) in [option['value'] for option in options]       # und im Zuordnen-Widget wählbar
    assert euer(db) == euer_before and datev_rows(db) == datev_before
    assert query(db, 'SELECT * FROM AssetDepreciations ORDER BY ID') == plan_before
    if kind == 'afa':
        assert 2026 in db.get_booked_depreciation_years(asset)           # das Jahr bleibt gebucht
        with pytest.raises(ValueError, match='bereits gebucht'):
            db.book_depreciation(asset, 2026, coa_id_expense=coa(db, 6220), coa_id_asset=coa(db, 520))


def test_the_split_sum_of_the_bank_movement_is_without_a_detached_row(db, tmp_path):
    booking = _wiso_booking(db, tmp_path, date='2026-08-01')
    bank = _movement(db, '-100.00')
    _add_child(db, bank, '-23.00', 'Versand')
    _assign(db, bank, booking)
    assert editor_rest(db, bank) == 0.0                                  # -77 und -23 ergeben die -100

    _remove_row(db, bank, booking)

    assert _exists(db, booking) and len(_children(db, bank)) == 1
    assert editor_rest(db, bank) == -77.0                                # offen: die -77 sind nicht mehr dabei


# ── gelöscht: alles, was im Editor entstanden sein kann ───────────────────────

def test_a_row_created_in_the_editor_is_deleted_when_removed(db):
    bank = _movement(db, '-119.00')
    _add_child(db, bank, '-119.00', 'Neu')
    (child,) = _children(db, bank)

    _remove_row(db, bank, child)

    assert not _exists(db, child) and _children(db, bank) == []


def test_a_hand_made_booking_assigned_later_is_deleted_when_removed(db):
    """Von Hand angelegt und später zugeordnet: nicht von einer im Editor angelegten Zeile zu unterscheiden –
    wird gelöscht (offener Punkt in ``docs/aufgaben.md``)."""
    booking = db.insert_booking('2026-07-15', Decimal('-119.00'), coa_id=coa(db, 6815),
                                counter_coa_id=coa(db, 1800), document_number='ER-1',
                                booking_type='entry')
    bank = _movement(db, '-119.00')
    _assign(db, bank, booking)

    _remove_row(db, bank, booking)

    assert not _exists(db, booking)


def _invoice(db):
    return db.insert_invoice({
        'invoice_number': 'RE-ENTF', 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19,
        'sum_net': 100.00, 'tax_amount': 19.00, 'sum_gross': 119.00, 'amount_due': 119.00,
        'status': 'sent'})


def test_a_receivable_hanging_under_a_bank_movement_is_detached_not_deleted(db):
    """Altbestand: Eine Forderung („10000 an Wartekonto“) hängt als Kind an einer Bankbewegung. Das ✕ ihrer
    Zeile löst sie nur – sie gehört zur Rechnung, wie beim Löschen der Bankbewegung (``delete_transaction``)."""
    invoice = _invoice(db)
    db.ensure_receivable_booking(invoice)
    (receivable,) = [row[0] for row in query(
        db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'RE-ENTF' AND BookingType = 'entry'")]
    bank = db.insert_booking('2026-02-01', Decimal('119.00'), account_id=_giro(db), booking_type='bank',
                             recipient_client='Käufer AG')
    execute(db, "UPDATE Bookings SET ParentBooking_ID = ?, Status = 'resolved' WHERE ID = ?",
            (bank, receivable))
    before = query(db, 'SELECT * FROM Bookings WHERE ID = ?', (receivable,))[0]

    _remove_row(db, bank, receivable)

    after = query(db, 'SELECT * FROM Bookings WHERE ID = ?', (receivable,))[0]
    assert _row(db, receivable) == [(None, None)]                        # gelöst, Status zurückgesetzt
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    names = [d[1] for d in query(db, 'PRAGMA table_info(Bookings)')]
    assert sorted(names[i] for i in changed) == ['ParentBooking_ID', 'Status']
    assert db.get_receivable_booking(invoice) is not None                # die Rechnung findet sie weiter


def test_removing_a_payment_row_removes_the_whole_assignment(db):
    invoice = _invoice(db)
    db.ensure_receivable_booking(invoice)
    bank = db.insert_booking('2026-02-01', Decimal('119.00'), account_id=_giro(db), booking_type='bank',
                             recipient_client='Käufer AG')
    link_booking_to_invoice_capped(db, invoice, bank)
    settlement, transfer = _children(db, bank)

    _remove_row(db, bank, settlement)

    assert _children(db, bank) == [] and not _exists(db, transfer)       # Zahlungs- und Umbuchungssatz
    assert query(db, 'SELECT COUNT(*) FROM InvoicePayments') == [(0,)]
    assert _exists(db, bank)


def test_delete_transaction_from_the_list_still_deletes_an_import_row(db, tmp_path):
    booking = _wiso_booking(db, tmp_path)

    db.delete_transaction(booking)

    assert not _exists(db, booking)


# ── der Abgleich ────────────────────────────────────────────────────────────────

def test_an_import_row_linked_by_the_matching_is_detached_and_found_again(db, tmp_path):
    booking = _wiso_booking(db, tmp_path)
    bank = _movement(db, '-77.00', date='2026-05-06')
    assert db.link_bank_to_entries()['linked'] == 1                       # Stufe 0: Quellklammer
    assert _children(db, bank) == [booking]

    _remove_row(db, bank, booking)

    assert _exists(db, booking) and _children(db, bank) == []
    assert _is_candidate(db, booking)
    assert db.link_bank_to_entries()['linked'] == 1                       # der nächste Abgleich findet ihn wieder
    assert _children(db, bank) == [booking]


@pytest.mark.parametrize('status, expected', [('resolved', None), ('offen', 'offen')])
def test_detaching_resets_only_the_status_resolved(db, tmp_path, status, expected):
    booking = _wiso_booking(db, tmp_path)
    bank = _movement(db, '-77.00', date='2026-05-06')
    db.link_bank_to_entries()
    execute(db, 'UPDATE Bookings SET Status = ? WHERE ID = ?', (status, booking))

    _remove_row(db, bank, booking)

    assert _row(db, booking) == [(None, expected)]
    assert _is_candidate(db, booking) and _in_the_list(db, booking)


# ── der Hinweis am ✕ ────────────────────────────────────────────────────────────

def test_the_remove_link_says_whether_the_booking_is_kept_or_deleted(db, tmp_path):
    imported = _wiso_booking(db, tmp_path, date='2026-08-01')
    bank = _movement(db, '-100.00')
    _add_child(db, bank, '-23.00', 'Versand')
    _assign(db, bank, imported)
    (made_here,) = [child for child in _children(db, bank) if child != imported]

    titles, current = {}, None
    for tag, attrs in Dom(PageTransactions(db, edit_transaction_id=bank)).tags:
        if tag == 'input' and attrs.get('name') == 'split_id':
            current = attrs.get('value')
        elif tag == 'a' and attrs.get('onclick') == 'splitRemove(this)':
            titles[current] = attrs['title']

    assert 'lösen' in titles[str(imported)] and 'bleibt bestehen' in titles[str(imported)]
    assert 'gelöscht' in titles[str(made_here)]
    assert 'gelöscht' not in titles[''] and 'lösen' not in titles['']      # die Vorlage für neue Zeilen
