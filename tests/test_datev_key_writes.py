# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der BU-Schlüssel am Satz (``Bookings.DatevKey``, seit 2.3.0) und die Schreibwege der App.

* Kein App-Schreibweg setzt ihn: ``insert_booking``, die Masken, der Editor, die Rechnungszahlung, die Forderung und
  die AfA lassen ihn leer – der Export leitet ihn dann ab. Nur die Importe tragen ihn ein
  (``test_datev_key_imports.py``).
* Er fällt (NULL), wenn sich Konto, Gegenkonto oder Steuersatz gegenüber dem gespeicherten Stand ändern, und das in
  demselben UPDATE (``update_booking_fields`` und ``update_booking``, EINE Stelle: ``_datev_key_stale``); jede
  andere Änderung lässt ihn stehen, ebenso ein unverändert zurückgeschickter Wert – auch in anderer Schreibweise des
  Satzes (0.19 = 19, kein Satz = 0).
* Nennt der Aufrufer ``datev_key`` selbst, gilt, was er nennt (nur eine ganze Zahl von 1 bis 9999 oder keiner).

Namen und Beträge sind erfunden.
"""
import os
import re
from contextlib import closing
from decimal import Decimal

import pytest

import userctx
from db import Database
from tests.entry_form_helpers import coa, execute, query

BOOKED = '2026-03-10'


def _key(db, booking_id):
    return query(db, 'SELECT DatevKey FROM Bookings WHERE ID = ?', (booking_id,))[0][0]


def _row(db, booking_id):
    return query(db, 'SELECT * FROM Bookings WHERE ID = ?', (booking_id,))[0]


@pytest.fixture
def db(tmp_db):
    return tmp_db


def _booking(db, key=401, rate=0.19, coa_number=6815, counter_number=1800, amount='-119.00', tax='-19.00', **extra):
    """Ein eigenständiger Satz mit einem mitgebrachten Schlüssel (wie ihn ein Import schreibt)."""
    booking_id = db.insert_booking(
        BOOKED, Decimal(amount), coa_id=coa(db, coa_number), counter_coa_id=coa(db, counter_number),
        tax_rate=rate, tax_amount=Decimal(tax) if tax is not None else None, document_number='ER-1',
        text='Bürobedarf', booking_type='entry', **extra)
    if key is not None:
        execute(db, 'UPDATE Bookings SET DatevKey = ? WHERE ID = ?', (key, booking_id))
    return booking_id


def _protocol(name):
    path = os.path.join(userctx.user_data_dir(), name)
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8') as f:
        return f.read()


# ── Kein App-Schreibweg setzt den Schlüssel ──────────────────────────────────

def test_insert_booking_has_no_key_parameter_and_leaves_the_key_empty(db):
    import inspect
    assert 'datev_key' not in inspect.signature(Database.insert_booking).parameters
    assert _key(db, _booking(db, key=None)) is None


def test_a_booking_the_mask_creates_has_no_key(tmp_db):
    from server.handlers import handle_add_transaction
    tmp_db.insert_account('Maske-Bank', 'Owner', 'DE00MASKE', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    account = next(a[0] for a in tmp_db.fetch_accounts() if a[1] == 'Maske-Bank')
    post = {'transaction_id': ['0'], 'date': [BOOKED], 'amount': ['-119.00'], 'currency': ['EUR'],
            'account': [str(account)], 'coa_id': [str(coa(tmp_db, 6815))], 'tax_rate': ['0.19'],
            'tax_amount': ['-19.00'], 'document_nr': ['MA-1']}
    assert handle_add_transaction(tmp_db, post)[0] == 303
    assert query(tmp_db, 'SELECT COUNT(*) FROM Bookings')[0][0] >= 1
    assert query(tmp_db, 'SELECT COUNT(*) FROM Bookings WHERE DatevKey IS NOT NULL')[0][0] == 0


def test_the_invoice_flow_writes_no_key(tmp_db, monkeypatch):
    """Forderung, Zahlung (mit und ohne Wartekonto) und Gegenbuchung der Stornorechnung: kein Schlüssel."""
    import server.handlers as handlers
    from tests.invoice_world import build_world, make_invoice, set_invoice_status
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user('anna')
    world = build_world(tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    items = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung', 'unitPrice': 100.0,
              'taxRate': 19}]
    invoice_id = make_invoice(world, 'draft', number='T-100', items=items, taxRate=0.19)
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    bank = tmp_db.insert_booking('2026-07-01', 50.0, booking_type='bank', account_id=world.bank)
    assert tmp_db.assign_invoice_payment(invoice_id, bank).ok
    assert tmp_db.cancel_invoice(invoice_id, '2026-07-02', 'anna').ok

    assert query(tmp_db, 'SELECT COUNT(*) FROM Bookings')[0][0] >= 4
    assert query(tmp_db, 'SELECT COUNT(*) FROM Bookings WHERE DatevKey IS NOT NULL')[0][0] == 0


# ── update_booking_fields: der Schlüssel fällt mit Konto, Gegenkonto und Satz ─

@pytest.mark.parametrize('change', [
    {'coa_id': 'other_coa'}, {'counter_coa_id': 'other_counter'}, {'tax_rate': 0.07}, {'tax_rate': None},
    {'tax_rate': 0.0}, {'tax_rate': 7}, {'tax_rate': 0.16}, {'coa_id': None}, {'counter_coa_id': None},
    {'coa_id': 'other_coa', 'tax_rate': 0.07}])
def test_the_key_falls_when_the_account_the_counter_account_or_the_rate_changes(db, change):
    booking_id = _booking(db)
    values = {name: (coa(db, 6850) if value == 'other_coa' else coa(db, 1810) if value == 'other_counter' else value)
              for name, value in change.items()}

    db.update_booking_fields(booking_id, **values)

    assert _key(db, booking_id) is None


@pytest.mark.parametrize('change', [
    {'text': 'Neuer Text'}, {'date_booking': '2026-04-01'}, {'date_tax': '2026-04-02'}, {'recipient_client': 'Neu'},
    {'document_number': 'ER-2'}, {'currency': 'EUR'}, {'foreign_bank_account': 'x'}, {'tax_amount': Decimal('-9.50')},
    {'amount': Decimal('-59.50')}, {'category_id': None}, {'contact_id': None}, {'parent_booking_id': None},
    {'account_id': None}])
def test_any_other_change_keeps_the_key(db, change):
    booking_id = _booking(db)

    db.update_booking_fields(booking_id, **change)

    assert _key(db, booking_id) == 401


@pytest.mark.parametrize('same', [
    {'coa_id': 'coa'}, {'counter_coa_id': 'counter'}, {'tax_rate': 0.19}, {'tax_rate': 19}, {'tax_rate': 19.0},
    {'tax_rate': 0.1900000001}, {'coa_id': 'coa', 'counter_coa_id': 'counter', 'tax_rate': 19}])
def test_an_unchanged_value_in_any_notation_keeps_the_key(db, same):
    """Die Masken schicken Konto, Gegenkonto und Satz mit zurück: Was sich nicht ändert, lässt den Schlüssel stehen
    (der Satz gilt als Prozentzahl: 0.19 und 19 sind derselbe)."""
    booking_id = _booking(db)
    values = {name: (coa(db, 6815) if value == 'coa' else coa(db, 1800) if value == 'counter' else value)
              for name, value in same.items()}

    db.update_booking_fields(booking_id, **values)

    assert _key(db, booking_id) == 401


@pytest.mark.parametrize('stored_rate, sent_rate', [(None, 0.0), (0.0, None), (None, 0), (None, -1), (0.0, 0)])
def test_no_rate_zero_and_the_paragraph_19_placeholder_are_the_same_rate(db, stored_rate, sent_rate):
    """Ein Satz „490“ (ohne Vorsteuerabzug) trägt keinen Satz oder 0: Beides schreibt dieselbe Zeile zurück."""
    booking_id = _booking(db, key=490, rate=stored_rate, tax=None)

    db.update_booking_fields(booking_id, tax_rate=sent_rate)

    assert _key(db, booking_id) == 490


def test_a_row_without_a_key_stays_without_one(db):
    booking_id = _booking(db, key=None)
    db.update_booking_fields(booking_id, coa_id=coa(db, 6850), tax_rate=0.07)
    assert _key(db, booking_id) is None


def test_the_key_falls_in_the_same_statement_and_the_protocol_replays_it(db):
    booking_id = _booking(db)

    db.update_booking_fields(booking_id, log_description='Konto geändert', coa_id=coa(db, 6850))

    statement = _protocol('sql_operations.sql').strip().splitlines()[-1]
    assert statement.startswith('UPDATE Bookings SET ') and 'DatevKey=NULL' in statement
    assert _key(db, booking_id) is None
    execute(db, 'UPDATE Bookings SET DatevKey = 401 WHERE ID = ?', (booking_id,))
    with closing(db._get_connection()) as conn:
        conn.execute(statement)
        conn.commit()
    assert _key(db, booking_id) is None               # das protokollierte Statement stellt den Stand wieder her


def test_a_change_of_the_account_that_rewrites_the_form_drops_the_key_too(db):
    """Konto, Form, negierter Betrag und Steuerbetrag stehen in einem UPDATE – der Schlüssel fällt darin mit."""
    booking_id = _booking(db, coa_number=6815, counter_number=1800)

    db.update_booking_fields(booking_id, coa_id=coa(db, 1810))             # beide Seiten Geldkonten: ein Spiegel

    assert _key(db, booking_id) is None
    assert query(db, 'SELECT EntryForm FROM Bookings WHERE ID = ?', (booking_id,))[0][0] is not None


# ── der Aufrufer nennt den Schlüssel selbst ──────────────────────────────────

def test_a_named_key_is_written_and_survives_a_change_of_the_account_in_the_same_call(db):
    booking_id = _booking(db)

    db.update_booking_fields(booking_id, coa_id=coa(db, 6850), datev_key=402)

    assert _key(db, booking_id) == 402


@pytest.mark.parametrize('value, stored', [(402, 402), ('402', 402), (9999, 9999), (1, 1), (None, None), (0, None),
                                           ('', None), ('0', None), (402.0, 402)])
def test_a_named_key_may_be_set_or_cleared_without_changing_the_accounts(db, value, stored):
    booking_id = _booking(db)

    db.update_booking_fields(booking_id, datev_key=value)

    assert _key(db, booking_id) == stored


@pytest.mark.parametrize('value', [-1, 10000, 'abc', '401;1', 401.5, True, [401], '0401', float('nan')])
def test_an_invalid_key_is_refused_and_nothing_is_written(db, value):
    booking_id = _booking(db)
    before = _row(db, booking_id)

    with pytest.raises(ValueError):
        db.update_booking_fields(booking_id, text='Nicht geschrieben', datev_key=value)

    assert _row(db, booking_id) == before


def test_a_key_can_be_named_on_a_row_without_one(db):
    booking_id = _booking(db, key=None)
    db.update_booking_fields(booking_id, datev_key=490)
    assert _key(db, booking_id) == 490


# ── update_booking (alle Spalten) ────────────────────────────────────────────

def _update_all(db, booking_id, **over):
    values = dict(date_booking=BOOKED, amount=Decimal('-119.00'), coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800),
                  tax_rate=0.19, tax_amount=Decimal('-19.00'), text='Bürobedarf', document_number='ER-1',
                  booking_type='entry')
    values.update(over)
    db.update_booking(booking_id, **values)


def test_update_booking_keeps_the_key_when_nothing_about_the_accounts_or_the_rate_changes(db):
    booking_id = _booking(db)
    _update_all(db, booking_id, text='Neuer Text', date_booking='2026-04-01', recipient_client='Neu',
                tax_rate=19, amount=Decimal('-120.00'))
    assert _key(db, booking_id) == 401


@pytest.mark.parametrize('over', [{'coa_id': 'other_coa'}, {'counter_coa_id': 'other_counter'}, {'tax_rate': 0.07},
                                  {'tax_rate': None}, {'coa_id': None}])
def test_update_booking_drops_the_key_when_the_accounts_or_the_rate_change(db, over):
    booking_id = _booking(db)
    over = {name: (coa(db, 6850) if value == 'other_coa' else coa(db, 1810) if value == 'other_counter' else value)
            for name, value in over.items()}

    _update_all(db, booking_id, **over)

    assert _key(db, booking_id) is None


def test_update_booking_has_no_key_parameter_and_the_key_goes_with_the_reform(db):
    import inspect
    assert 'datev_key' not in inspect.signature(Database.update_booking).parameters
    booking_id = _booking(db, coa_number=6815, counter_number=1800)
    _update_all(db, booking_id, coa_id=coa(db, 4400), counter_coa_id=coa(db, 1800))      # Aufwand → Erlös: Form kippt nicht
    assert _key(db, booking_id) is None


def test_update_booking_without_a_key_stays_without_one(db):
    booking_id = _booking(db, key=None)
    _update_all(db, booking_id, coa_id=coa(db, 6850))
    assert _key(db, booking_id) is None


def test_update_booking_replays_the_key_statement(db):
    booking_id = _booking(db)
    _update_all(db, booking_id, coa_id=coa(db, 6850))
    db.update_booking(booking_id, BOOKED, Decimal('-119.00'), coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800),
                      tax_rate=0.19, tax_amount=Decimal('-19.00'), booking_type='entry',
                      log_description='Alle Spalten')
    statement = _protocol('sql_operations.sql').strip().splitlines()[-1]
    assert 'DatevKey=CASE WHEN' in statement and statement.endswith(f'WHERE ID={booking_id};')


# ── Spalte 23 in der Zeile und die Maske für die Feldliste ───────────────────

def test_the_key_is_column_23_of_the_booking_row(db):
    booking_id = _booking(db, key=402)
    assert _row(db, booking_id)[23] == 402 and len(_row(db, booking_id)) == 24
    assert db.get_booking_by_id(booking_id)[23] == 402
    assert re.fullmatch(r'\d+', str(db.get_booking_by_id(booking_id)[23]))


def test_the_field_list_of_update_booking_fields_knows_the_key(db):
    assert Database._BOOKING_FIELD_COLUMNS['datev_key'] == 'DatevKey'
