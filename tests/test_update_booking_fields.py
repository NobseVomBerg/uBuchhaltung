# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""``Database.update_booking_fields``: nur die genannten Spalten einer Buchung ändern.

Masken führen nicht jede Spalte einer Buchung (Gegenkonto, Kategorie und fremde
Kontakte haben dort kein Feld). ``update_booking`` setzt ALLE Spalten und leerte beim
Speichern, was die Maske nicht kennt; ``update_booking_fields`` schreibt nur, was
übergeben wird. Feldnamen wie bei ``insert_booking``; ``amount``/``tax_amount`` in
Euro, gespeichert in Minor Units; ein ausdrückliches ``None`` setzt NULL, ein
weggelassenes Feld bleibt. Feldnamen sind nie SQL: Unbekanntes ist ein ``TypeError``.

Verglichen wird die ganze Zeile aus SQLite samt Typ der Werte, nicht nur die
geänderte Spalte.
"""
import inspect
import os
import re
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace

import pytest

import userctx
from db import Database


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _row(db, booking_id):
    """Ganze Zeile aus Bookings: {Spalte: (Wert, Typ)} – so, wie SQLite sie hält."""
    with closing(db._get_connection()) as conn:
        cursor = conn.execute('SELECT * FROM Bookings WHERE ID=?', (booking_id,))
        names = [d[0] for d in cursor.description]
        values = cursor.fetchone()
    return {name: (value, type(value)) for name, value in zip(names, values)}


def _without(row, column):
    return {name: cell for name, cell in row.items() if name != column}


def _plain_booking(db, **kw):
    kw.setdefault('text', 'Alter Text')
    return db.insert_booking('2026-03-01', Decimal('-59.50'), booking_type='entry', **kw)


def _protocol(name):
    path = os.path.join(userctx.user_data_dir(), name)
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8') as f:
        return f.read()


@pytest.fixture
def world(db_with_coa):
    """Seed-Daten (Testbank, Lieferant GmbH, Beispiel Kunde AG) plus zwei Kategorien und zwei
    Bankbewegungen als Ziel für ``parent_booking_id``."""
    db = db_with_coa
    accounts = {a[1]: a[0] for a in db.fetch_accounts()}
    contacts = {c[2]: c[0] for c in db.fetch_contacts()}        # Kundennummer -> ID
    with closing(db._get_connection()) as conn:
        conn.execute("INSERT INTO Categories (Name, Text) VALUES ('Büro', 'Kategorie A')")
        conn.execute("INSERT INTO Categories (Name, Text) VALUES ('Reise', 'Kategorie B')")
        conn.commit()
        categories = [row[0] for row in conn.execute('SELECT ID FROM Categories ORDER BY ID')]
    coa = db.get_coa_id_by_account_number
    return SimpleNamespace(
        db=db,
        bank_one=accounts['Testbank'], bank_two=accounts['Testkonto 2'],
        supplier=contacts['L001'], customer=contacts['K001'],
        category_one=categories[0], category_two=categories[1],
        coa_expense=coa(6815), coa_other=coa(6850), coa_bank=coa(1800), coa_bank_two=coa(1810),
        parent_one=db.insert_booking('2026-03-01', Decimal('-59.50'), account_id=accounts['Testbank'],
                                     booking_type='bank'),
        parent_two=db.insert_booking('2026-03-02', Decimal('-10.00'), account_id=accounts['Testbank'],
                                     booking_type='bank'),
    )


def _full_booking(w):
    """Eine Buchung, in der jede Spalte außer dem veralteten BookingGroup_ID belegt ist."""
    db = w.db
    bid = db.insert_booking(
        '2026-03-01', Decimal('-59.50'), date_tax='2026-03-02', account_id=w.bank_one,
        foreign_bank_account='DE89370400440532013000', recipient_client='Alt Empfänger',
        contact_id=w.supplier, coa_id=w.coa_expense, counter_coa_id=w.coa_bank,
        category_id=w.category_one, currency='EUR', tax_rate=0.19, tax_amount=Decimal('-9.50'),
        text='Alter Text', document_number='ALT-1', booking_type='entry',
        parent_booking_id=w.parent_one, auto_mirror=True)
    with closing(db._get_connection()) as conn:
        conn.execute("UPDATE Bookings SET Status='offen', SourceGroup='SG-1' WHERE ID=?", (bid,))
        conn.commit()
    empty = [name for name, (value, _type) in _row(db, bid).items() if value is None]
    assert empty == ['BookingGroup_ID']
    return bid


def _new_values(w):
    """Feld -> (Spalte, neuer Wert, erwarteter Rohwert in SQLite); jeder Wert weicht vom Alten ab."""
    return {
        'date_booking': ('DateBooking', '2026-05-06', '2026-05-06'),
        'date_tax': ('DateTax', '2026-05-07', '2026-05-07'),
        'account_id': ('Account_ID', w.bank_two, w.bank_two),
        'foreign_bank_account': ('ForeignBankAccount', 'Neues Fremdkonto', 'Neues Fremdkonto'),
        'recipient_client': ('RecipientClient', 'Neuer Empfänger', 'Neuer Empfänger'),
        'contact_id': ('Contact_ID', w.customer, w.customer),
        'coa_id': ('COA_ID', w.coa_other, w.coa_other),
        'counter_coa_id': ('CounterCOA_ID', w.coa_bank_two, w.coa_bank_two),
        'category_id': ('Category_ID', w.category_two, w.category_two),
        'amount': ('Amount', Decimal('-12.34'), -123400),
        'currency': ('Currency', 'USD', 'USD'),
        'tax_rate': ('TaxRate', 0.07, 0.07),
        'tax_amount': ('TaxAmount', Decimal('-0.81'), -8100),
        'text': ('Text', 'Neuer Text', 'Neuer Text'),
        'document_number': ('DocumentNumber', 'NEU-1', 'NEU-1'),
        'parent_booking_id': ('ParentBooking_ID', w.parent_two, w.parent_two),
        'entry_form': ('EntryForm', 'transfer', 'transfer'),
    }


# ── nur die genannten Spalten ändern sich ────────────────────────────────────

def test_field_names_are_the_ones_of_insert_booking(world):
    """Alle Felder von ``insert_booking`` (ohne Protokoll, Typ und Spiegel-Kennzeichen)."""
    insert_fields = set(inspect.signature(Database.insert_booking).parameters) - {
        'self', 'log_description', 'booking_type', 'auto_mirror'}
    assert set(_new_values(world)) == insert_fields


def test_each_field_changes_only_its_own_column(world):
    for field, (column, value, stored) in _new_values(world).items():
        bid = _full_booking(world)
        before = _row(world.db, bid)

        world.db.update_booking_fields(bid, **{field: value})

        after = _row(world.db, bid)
        assert after[column] == (stored, type(stored)), field
        assert after[column] != before[column], field          # sonst bewiese der Fall nichts
        assert _without(after, column) == _without(before, column), field


def test_all_fields_at_once_in_any_order(world):
    new_values = _new_values(world)
    forward = {field: value for field, (_column, value, _stored) in new_values.items()}
    backward = dict(reversed(list(forward.items())))

    rows = []
    for kwargs in (forward, backward):
        bid = _full_booking(world)
        world.db.update_booking_fields(bid, **kwargs)
        rows.append(_row(world.db, bid))

    for row in rows:
        for field, (column, _value, stored) in new_values.items():
            assert row[column] == (stored, type(stored)), field
        # was keine Maske führt, bleibt
        assert row['BookingType'] == ('entry', str)
        assert row['AutoMirror'] == (1, int)
        assert row['Status'] == ('offen', str)
        assert row['SourceGroup'] == ('SG-1', str)
    assert _without(rows[0], 'ID') == _without(rows[1], 'ID')


def test_other_bookings_are_not_touched(world):
    first = _full_booking(world)
    second = _full_booking(world)
    before_second = _row(world.db, second)

    world.db.update_booking_fields(first, text='Nur die erste', amount=Decimal('-1.00'))

    assert _row(world.db, second) == before_second
    assert _row(world.db, first)['Text'] == ('Nur die erste', str)


def test_none_sets_null_while_an_omitted_field_stays(world):
    """Der Unterschied zu ``update_booking``: Was nicht genannt ist, bleibt – ein
    ausdrückliches None leert die Spalte."""
    bid = _full_booking(world)
    before = _row(world.db, bid)

    world.db.update_booking_fields(bid, tax_amount=None, tax_rate=None, contact_id=None,
                                   category_id=None)

    after = _row(world.db, bid)
    for column in ('TaxAmount', 'TaxRate', 'Contact_ID', 'Category_ID'):
        assert after[column] == (None, type(None)), column
    changed = {'TaxAmount', 'TaxRate', 'Contact_ID', 'Category_ID'}
    assert {c: v for c, v in after.items() if c not in changed} == \
           {c: v for c, v in before.items() if c not in changed}


# ── Geld in Minor Units ──────────────────────────────────────────────────────

@pytest.mark.parametrize('value', [
    pytest.param(Decimal('-59.50'), id='decimal'),
    pytest.param(-59.5, id='float'),
    pytest.param('-59.50', id='text'),
    pytest.param('-59,50', id='text-deutsch'),
])
def test_amount_and_tax_amount_are_stored_in_minor_units(tmp_db, value):
    bid = _plain_booking(tmp_db)

    tmp_db.update_booking_fields(bid, amount=value, tax_amount=value)

    row = _row(tmp_db, bid)
    assert row['Amount'] == (-595000, int)
    assert row['TaxAmount'] == (-595000, int)
    assert tmp_db.get_booking_by_id(bid)[11] == Decimal('-59.5000')     # nach außen wieder Euro


def test_money_keeps_four_decimal_places(tmp_db):
    bid = _plain_booking(tmp_db)

    tmp_db.update_booking_fields(bid, amount=Decimal('0.0001'), tax_amount=Decimal('-0.0001'))

    row = _row(tmp_db, bid)
    assert row['Amount'] == (1, int)
    assert row['TaxAmount'] == (-1, int)


@pytest.mark.parametrize('zero', [0, 0.0, Decimal('0.00')])
def test_zero_tax_amount_is_zero_and_not_null(tmp_db, zero):
    bid = _plain_booking(tmp_db, tax_amount=Decimal('-9.50'))

    tmp_db.update_booking_fields(bid, tax_amount=zero)

    assert _row(tmp_db, bid)['TaxAmount'] == (0, int)


# ── leerer Aufruf, unbekannte Felder, nie SQL aus Feldnamen ──────────────────

def test_call_without_fields_does_nothing(world):
    bid = _full_booking(world)
    before = _row(world.db, bid)
    log_before, replay_before = _protocol('sql_operations.log'), _protocol('sql_operations.sql')

    assert world.db.update_booking_fields(bid) is None
    assert world.db.update_booking_fields(bid, log_description='Leerer Aufruf') is None

    assert _row(world.db, bid) == before
    # auch kein (ungültiges) UPDATE ohne Zuweisung im Protokoll
    assert _protocol('sql_operations.log') == log_before
    assert _protocol('sql_operations.sql') == replay_before


def test_unknown_field_raises_type_error_and_writes_nothing(tmp_db):
    bid = _plain_booking(tmp_db)
    before = _row(tmp_db, bid)

    with pytest.raises(TypeError, match='bogus'):
        tmp_db.update_booking_fields(bid, text='Neuer Text', bogus=1)

    assert _row(tmp_db, bid) == before             # auch das gültige Feld wurde nicht geschrieben


@pytest.mark.parametrize('name', [
    pytest.param('Amount', id='spaltenname-Amount'),
    pytest.param('DateBooking', id='spaltenname-DateBooking'),
    pytest.param('Text', id='spaltenname-Text'),
    pytest.param('COA_ID', id='spaltenname-COA_ID'),
    pytest.param('ID', id='spaltenname-ID'),
    pytest.param('text=?, Amount', id='zuweisung-eingeschmuggelt'),
    pytest.param('Amount=?, Text', id='zuweisung-eingeschmuggelt-2'),
    pytest.param('text=1 WHERE 1=1; --', id='where-ausgehebelt'),
    pytest.param('text; DROP TABLE Bookings', id='drop-table'),
    pytest.param("x' OR '1'='1", id='anfuehrungszeichen'),
    pytest.param('', id='leerer-name'),
])
def test_field_names_are_never_taken_as_sql(tmp_db, name):
    bid = _plain_booking(tmp_db)
    other = _plain_booking(tmp_db, text='Zweite')
    before, before_other = _row(tmp_db, bid), _row(tmp_db, other)

    with pytest.raises(TypeError):
        tmp_db.update_booking_fields(bid, **{name: 1})

    assert _row(tmp_db, bid) == before
    assert _row(tmp_db, other) == before_other
    with closing(tmp_db._get_connection()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM Bookings').fetchone()[0] == 2


# ── SQL-Protokoll ────────────────────────────────────────────────────────────

def test_partial_update_is_logged_and_replayable(tmp_db):
    bid = _plain_booking(tmp_db, contact_id=None)

    tmp_db.update_booking_fields(bid, log_description='Teil-UPDATE im Protokoll',
                                 text='Neuer Text', tax_amount=Decimal('-9.50'), contact_id=None)

    assert 'Description: Teil-UPDATE im Protokoll' in _protocol('sql_operations.log')
    statement = _protocol('sql_operations.sql').strip().splitlines()[-1]
    assert statement.startswith('UPDATE Bookings SET ') and statement.endswith(f'WHERE ID={bid};')
    assigned = re.findall(r'(\w+)=', statement[len('UPDATE Bookings SET '):statement.rindex(' WHERE')])
    assert assigned == ['Text', 'TaxAmount', 'Contact_ID']        # nur die genannten Spalten
    assert "Text='Neuer Text'" in statement and 'TaxAmount=-95000' in statement

    # Das protokollierte Statement stellt das Ergebnis wieder her
    expected = _row(tmp_db, bid)
    tmp_db.update_booking_fields(bid, text='Zwischenstand', tax_amount=None)
    assert _row(tmp_db, bid) != expected
    with closing(tmp_db._get_connection()) as conn:
        conn.execute(statement)
        conn.commit()
    assert _row(tmp_db, bid) == expected


def test_update_without_description_is_not_logged(tmp_db):
    bid = _plain_booking(tmp_db)
    log_before, replay_before = _protocol('sql_operations.log'), _protocol('sql_operations.sql')

    tmp_db.update_booking_fields(bid, text='Ohne Protokoll')

    assert _protocol('sql_operations.log') == log_before
    assert _protocol('sql_operations.sql') == replay_before
    assert _row(tmp_db, bid)['Text'] == ('Ohne Protokoll', str)
