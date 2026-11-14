# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""„Vorhandene Buchung zuordnen“ im Buchungssatz-Editor: Steuer und eigene Stammdaten der Buchung.

* Die zugeordnete Buchung behält Steuersatz und Steuerbetrag, solange der Nutzer sie in der Zeile nicht
  ändert – auch wenn das Formular sie nicht mitbringt (ein Browser mit dem alten Skript der Seite). Ging
  die Steuer verloren, fehlte die Vorsteuer bzw. Umsatzsteuer in der EÜR und der BU-Schlüssel im DATEV-Export.
* Datum, Steuerdatum und Empfänger eines Satzes folgen der Bankbewegung nur, wenn der Satz sie vorher mit
  ihr geteilt hat. Ein eigener Wert (eine zugeordnete Bestandsbuchung) bleibt über jedes weitere Speichern.

Die Formulare stammen aus dem HTML der echten Maske (``tests/entry_form_roundtrip.py``) und gehen durch
``handle_add_transaction``. Das Skript der Seite läuft in der Suite nicht: Was es in die Zeile schreibt,
steht hier nur als Quelltext fest (``test_the_script_takes_the_tax_of_the_option_into_the_row``).

Alle Namen und Werte sind erfunden.
"""
import re
from decimal import Decimal

import pytest

from db.matching import FORM_PURPOSE_FIRST
from server.pages_transactions import PageTransactions
from tests.dom_helpers import Dom
from tests.entry_form_helpers import bank_account, coa, datev_rows, euer, execute, minor, query
from tests.entry_form_roundtrip import add_row, adopt_row, differences, open_form, snapshot, submit

BANK_DATE = '2026-08-01'
OWN = {'DateBooking': '2026-07-15', 'DateTax': '2026-07-20', 'RecipientClient': 'Lieferant Eins'}

# Die Buchung, die man zuordnet (Aufwand mit 19 %, Erlös mit 7 %) und was danach stimmen muss: EÜR
# (Zweckkonto netto, Vorsteuer 1406 bzw. Umsatzsteuer 3806) und DATEV-Zeile (Betrag, S/H, Konto,
# Gegenkonto, BU-Schlüssel)
EXPENSE_19 = dict(number=6815, counter=1800, amount='-119.00', rate=0.19, tax='-19.00', doc='ER-19')
REVENUE_7 = dict(number=4300, counter=1800, amount='107.00', rate=0.07, tax='7.00', doc='AR-7')
CASES = [
    pytest.param(EXPENSE_19, {6815: Decimal('-100.00'), 1406: Decimal('-19.00')},
                 ('119,00', 'S', '6815', '1800', '401'), id='aufwand-19'),
    pytest.param(REVENUE_7, {4300: Decimal('100.00'), 3806: Decimal('7.00')},
                 ('107,00', 'H', '4300', '1800', '102'), id='erloes-7'),
]


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Zuordnung-Bank', 1800)
    return tmp_db


def _movement(db, amount, date=BANK_DATE, recipient='Bank-Empfänger'):
    giro = next(a[0] for a in db.fetch_accounts() if a[1] == 'Zuordnung-Bank')
    return db.insert_booking(date, Decimal(amount), account_id=giro, booking_type='bank',
                             recipient_client=recipient, text='Zuordnungsziel')


def _own(db, case, **own):
    """Eine eigenständige Buchung (noch keiner Bankbewegung zugeordnet) mit eigenem Datum, Steuerdatum
    und Empfänger – so, wie die Maske oder ein Import sie anlegt."""
    values = dict(OWN, **own)
    return db.insert_booking(
        values['DateBooking'], Decimal(case['amount']), date_tax=values['DateTax'],
        recipient_client=values['RecipientClient'], coa_id=coa(db, case['number']),
        counter_coa_id=coa(db, case['counter']), tax_rate=case['rate'],
        tax_amount=Decimal(case['tax']), text=f"Beleg {case['doc']}", document_number=case['doc'],
        booking_type='entry')


def _option(options, booking_id):
    return next(o for o in options if o['value'] == str(booking_id))


def _assign(db, bank, booking_id, **row):
    """„Zuordnen“ und Speichern, wie das Skript der Seite es tut; ``row`` überschreibt Felder der Zeile."""
    post, options = open_form(db, bank)
    submit(db, adopt_row(post, _option(options, booking_id), **row))


def _assign_with_the_old_script(db, bank, booking_id, fields='empty'):
    """Dasselbe mit dem Skript vor 2.1.1: Es füllt Steuersatz und -betrag der Zeile nicht (``'empty'``);
    ``'missing'``: Das Formular bringt die beiden Felder gar nicht mit."""
    post, options = open_form(db, bank)
    option = _option(options, booking_id)
    row = dict(split_id=option['value'], split_amount=option['amount'], split_docnr=option['docnr'],
               split_text=option['text'])
    if option['coa']:
        row['split_coa'] = option['coa']
    post = add_row(post, **row)
    if fields == 'missing':
        post = _without_tax_fields(post)
    submit(db, post)


def _without_tax_fields(post):
    return {name: items for name, items in post.items()
            if name not in ('split_tax_rate', 'split_tax_amount')}


def _add_child(db, bank, amount, text):
    """Eine neue Zeile im Editor („+ Teilbuchung“), Zweckkonto 6815."""
    post, _options = open_form(db, bank)
    submit(db, add_row(post, split_amount=amount, split_coa=str(coa(db, 6815)), split_text=text))


def _save_again(db, bank, **changes):
    """Die Bankmaske unverändert speichern – oder mit geänderten Feldern der Bankbewegung."""
    post, _options = open_form(db, bank)
    for name, value in changes.items():
        post[name] = [value]
    submit(db, post)


def _tax(db, booking_id):
    return query(db, 'SELECT TaxRate, TaxAmount FROM Bookings WHERE ID = ?', (booking_id,))[0]


def _column(db, booking_id, column):
    return query(db, f'SELECT {column} FROM Bookings WHERE ID = ?', (booking_id,))[0][0]


def _children(db, bank):
    return [row[0] for row in query(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID',
                                    (bank,))]


# ── Fehler 1: Zuordnen verliert die Steuer ────────────────────────────────────

@pytest.mark.parametrize('case, expected_euer, expected_datev', CASES)
def test_assigning_keeps_the_tax_and_the_euer_and_datev_are_right(db, case, expected_euer,
                                                                  expected_datev):
    bank = _movement(db, case['amount'])
    booking = _own(db, case)
    tax = (case['rate'], minor(case['tax']))
    assert _tax(db, booking) == tax

    post, options = open_form(db, bank)
    option = _option(options, booking)
    assert option['rate'] and option['tax']                       # die Seite bietet die Steuer an
    submit(db, adopt_row(post, option))

    assert _column(db, booking, 'ParentBooking_ID') == bank
    assert _tax(db, booking) == tax                               # Satz und Betrag wie vorher
    assert euer(db) == expected_euer
    assert datev_rows(db)[case['doc']] == expected_datev

    settled = snapshot(db)
    for _ in range(2):                                            # zweimal unverändert speichern
        _save_again(db, bank)
        assert _tax(db, booking) == tax
        assert euer(db) == expected_euer
        assert datev_rows(db)[case['doc']] == expected_datev
        assert differences(settled, snapshot(db)) == []


@pytest.mark.parametrize('case, expected_euer, expected_datev', CASES)
@pytest.mark.parametrize('fields', ['empty', 'missing'])
def test_a_post_without_the_tax_of_the_assigned_row_keeps_the_tax(db, case, expected_euer,
                                                                  expected_datev, fields):
    """Der Browser mit dem alten Skript schickt Steuersatz und -betrag leer (oder gar nicht) mit."""
    bank = _movement(db, case['amount'])
    booking = _own(db, case)
    tax = _tax(db, booking)

    _assign_with_the_old_script(db, bank, booking, fields)

    assert _column(db, booking, 'ParentBooking_ID') == bank
    assert _tax(db, booking) == tax
    assert euer(db) == expected_euer
    assert datev_rows(db)[case['doc']] == expected_datev


def test_a_booking_under_the_bank_movement_keeps_its_tax_when_the_form_has_no_tax_fields(db):
    """Was das Formular nicht mitbringt, schreibt der Editor nicht – auch bei einem Satz, der schon
    unter der Bankbewegung hängt."""
    bank = _movement(db, EXPENSE_19['amount'])
    booking = _own(db, EXPENSE_19)
    tax = _tax(db, booking)
    _assign(db, bank, booking)
    post, _options = open_form(db, bank)

    submit(db, _without_tax_fields(post))

    assert _tax(db, booking) == tax == (0.19, minor('-19.00'))


def test_a_tax_changed_in_the_row_is_taken(db):
    bank = _movement(db, EXPENSE_19['amount'])
    booking = _own(db, EXPENSE_19)

    # Der Nutzer stellt den Satz in der Zeile auf 7 % (das Skript der Seite rechnet den Betrag nach)
    _assign(db, bank, booking, split_tax_rate='7', split_tax_amount='-7.79')

    assert _tax(db, booking) == (0.07, minor('-7.79'))
    assert euer(db) == {6815: Decimal('-111.21'), 1401: Decimal('-7.79')}
    assert datev_rows(db)['ER-19'][4] == '402'                    # 7 % Vorsteuer


def test_a_tax_cleared_in_the_row_of_a_saved_booking_is_cleared(db):
    """Beim Zuordnen gilt eine leere Steuer als „nicht mitgebracht“; danach löscht der Nutzer sie wie in
    jeder anderen Zeile, indem er beide Felder leert."""
    bank = _movement(db, EXPENSE_19['amount'])
    booking = _own(db, EXPENSE_19)
    _assign(db, bank, booking)
    post, _options = open_form(db, bank)
    index = post['split_id'].index(str(booking))
    post['split_tax_rate'][index] = ''
    post['split_tax_amount'][index] = ''

    submit(db, post)

    assert _tax(db, booking) == (None, None)
    assert euer(db) == {6815: Decimal('-119.00')}


def test_a_changed_amount_leaves_the_tax_as_the_row_shows_it(db):
    """Der Server rechnet die Steuer einer Zeile nie nach (das Skript der Seite tut es beim Ändern des
    Satzes): Ändert der Nutzer nur den Betrag der zugeordneten Zeile, gilt, was in den Steuerfeldern steht."""
    bank = _movement(db, '-100.00')
    booking = _own(db, EXPENSE_19)

    _assign(db, bank, booking, split_amount='-100.00')

    assert _column(db, booking, 'Amount') == minor('-100.00')
    assert _tax(db, booking) == (0.19, minor('-19.00'))


# ── die Seite: Steuer in der Zeile ─────────────────────────────────────────────

def _function(source, name):
    """Der Text der JavaScript-Funktion ``name`` (bis zur schließenden Klammer)."""
    start = source.index(f'function {name}(')
    depth, position = 0, source.index('{', start)
    while True:
        depth += {'{': 1, '}': -1}.get(source[position], 0)
        if depth == 0:
            return source[start:position + 1]
        position += 1


def test_the_page_offers_the_tax_of_each_candidate(db):
    expense, revenue = _own(db, EXPENSE_19), _own(db, REVENUE_7)
    plain = db.insert_booking('2026-07-01', Decimal('-10.00'), coa_id=coa(db, 6815),
                              counter_coa_id=coa(db, 1800), booking_type='entry')
    bank = _movement(db, '-119.00')

    dom = Dom(PageTransactions(db, edit_transaction_id=bank))

    options = {attrs['value']: attrs for attrs in dom.all('option')
               if attrs.get('value') in {str(expense), str(revenue), str(plain)}}
    assert (options[str(expense)]['data-rate'], options[str(expense)]['data-tax']) == ('19', '-19.00')
    assert (options[str(revenue)]['data-rate'], options[str(revenue)]['data-tax']) == ('7', '7.00')
    assert (options[str(plain)]['data-rate'], options[str(plain)]['data-tax']) == ('', '')


def test_the_candidates_carry_their_tax_columns(db):
    booking = _own(db, EXPENSE_19)

    (row,) = [r for r in db.get_unlinked_entry_bookings() if r[0] == booking]

    assert row[:9] == (booking, OWN['DateBooking'], Decimal('-119.0000'), 'ER-19', 'Beleg ER-19',
                       OWN['RecipientClient'], coa(db, 6815), coa(db, 1800), FORM_PURPOSE_FIRST)
    assert row[9] == 0.19 and row[10] == Decimal('-19.0000')      # TaxRate, TaxAmount


def test_the_script_takes_the_tax_of_the_option_into_the_row(db):
    """Nur der Quelltext: Das Skript der Seite läuft in der Suite nicht. Es schreibt Steuersatz und
    -betrag aus der gewählten Option in die neue Zeile – ohne innerHTML."""
    _own(db, EXPENSE_19)
    bank = _movement(db, '-119.00')
    dom = Dom(PageTransactions(db, edit_transaction_id=bank))
    (script,) = [text for text in dom.scripts if 'function splitAdopt(' in text]

    body = _function(script, 'splitAdopt')

    assert re.search(r"querySelector\('\[name=split_tax_rate\]'\)\.value\s*=\s*opt\.dataset\.rate", body)
    assert re.search(r"querySelector\('\[name=split_tax_amount\]'\)\.value\s*=\s*opt\.dataset\.tax", body)
    assert 'innerHTML' not in body


# ── Fehler 2: Datum, Steuerdatum und Empfänger folgen nur, wenn sie geteilt waren ────────

# (Feld der Bankmaske, Spalte, neuer Wert der Bankbewegung)
SHARED_FIELDS = [
    pytest.param('date', 'DateBooking', '2026-08-15', id='buchungsdatum'),
    pytest.param('date_tax', 'DateTax', '2026-08-20', id='steuerdatum'),
    pytest.param('recipient', 'RecipientClient', 'Neuer Empfänger', id='empfaenger'),
]


def test_an_assigned_booking_keeps_its_own_values_over_three_saves(db):
    bank = _movement(db, EXPENSE_19['amount'])
    booking = _own(db, EXPENSE_19)
    before = snapshot(db)

    _assign(db, bank, booking)                                    # 1. Speichern: zuordnen
    settled = snapshot(db)
    assert differences(before, settled, ignore=('ParentBooking_ID',)) == []
    for _ in range(2):                                            # 2. und 3. Speichern
        _save_again(db, bank)
        assert differences(settled, snapshot(db)) == []
    assert {column: _column(db, booking, column) for column in OWN} == OWN


def test_the_euer_period_of_an_assigned_booking_stays_over_repeated_saves(db):
    """Die EÜR zählt den Satz nach seinem eigenen Datum: Vom 20.12. bleibt er im Jahr 2026, auch wenn die
    Bankbewegung erst im Januar 2027 steht – das zweite Speichern schob ihn früher dorthin."""
    bank = _movement(db, EXPENSE_19['amount'], date='2027-01-05')
    booking = _own(db, EXPENSE_19, DateBooking='2026-12-20', DateTax=None)
    in_2026 = {6815: Decimal('-100.00'), 1406: Decimal('-19.00')}

    _assign(db, bank, booking)
    for _ in range(3):
        assert euer(db, '2026-01-01', '2026-12-31') == in_2026
        assert euer(db, '2027-01-01', '2027-12-31') == {}
        _save_again(db, bank)


@pytest.mark.parametrize('field, column, new_value', SHARED_FIELDS)
def test_a_child_with_the_same_value_follows_the_bank_movement(db, field, column, new_value):
    """Ein Satz, den der Editor selbst angelegt hat, teilt Datum, Steuerdatum und Empfänger mit der
    Bankbewegung – und folgt, wenn sie sich ändert."""
    bank = _movement(db, '-119.00')
    _add_child(db, bank, '-119.00', 'Neu')
    (child,) = _children(db, bank)
    assert _column(db, child, column) == _column(db, bank, column)    # geteilt (auch: beide leer)

    _save_again(db, bank, **{field: new_value})

    assert _column(db, bank, column) == new_value
    assert _column(db, child, column) == new_value


@pytest.mark.parametrize('field, column, new_value', SHARED_FIELDS)
def test_a_child_with_another_value_does_not_follow_the_bank_movement(db, field, column, new_value):
    bank = _movement(db, EXPENSE_19['amount'])
    booking = _own(db, EXPENSE_19)
    _assign(db, bank, booking)

    _save_again(db, bank, **{field: new_value})
    _save_again(db, bank)

    assert _column(db, bank, column) == new_value
    assert _column(db, booking, column) == OWN[column]            # behält seinen Wert


@pytest.mark.parametrize('field, column, new_value', SHARED_FIELDS)
def test_an_assigned_booking_that_shares_the_value_follows_the_bank_movement(db, field, column,
                                                                             new_value):
    """„Geteilt“ heißt gleicher Wert wie die Bankbewegung – gleich, woher der Satz kommt."""
    bank = _movement(db, EXPENSE_19['amount'])
    bank_values = {'DateBooking': BANK_DATE, 'DateTax': None, 'RecipientClient': 'Bank-Empfänger'}
    booking = _own(db, EXPENSE_19, **{column: bank_values[column]})
    _assign(db, bank, booking)
    assert _column(db, booking, column) == _column(db, bank, column)

    _save_again(db, bank, **{field: new_value})

    assert _column(db, booking, column) == new_value


def test_each_field_follows_on_its_own(db):
    """Ein Satz teilt das Buchungsdatum mit der Bankbewegung, hat aber einen eigenen Empfänger: Das Datum
    folgt, der Empfänger bleibt – bei dem anderen Satz umgekehrt."""
    bank = _movement(db, '-119.00')
    _add_child(db, bank, '-60.00', 'A')
    _add_child(db, bank, '-59.00', 'B')
    first, second = _children(db, bank)
    execute(db, 'UPDATE Bookings SET RecipientClient = ? WHERE ID = ?', ('Eigener Empfänger', first))
    execute(db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', ('2026-07-30', second))

    _save_again(db, bank, date='2026-08-15', recipient='Neuer Empfänger')

    assert query(db, 'SELECT DateBooking, RecipientClient FROM Bookings WHERE ID = ?', (first,)) == [
        ('2026-08-15', 'Eigener Empfänger')]
    assert query(db, 'SELECT DateBooking, RecipientClient FROM Bookings WHERE ID = ?', (second,)) == [
        ('2026-07-30', 'Neuer Empfänger')]


def test_a_new_row_gets_the_values_of_the_bank_movement(db):
    """Neue Zeilen bekommen wie bisher die Werte der Bankbewegung – auch die, die sie in demselben
    Speichern bekommt."""
    bank = _movement(db, '-119.00')
    post, _options = open_form(db, bank)
    post['date'], post['date_tax'], post['recipient'] = ['2026-08-15'], ['2026-08-20'], ['Neuer Empfänger']
    submit(db, add_row(post, split_amount='-119.00', split_coa=str(coa(db, 6815)), split_text='Neu'))

    (child,) = _children(db, bank)

    assert query(db, 'SELECT DateBooking, DateTax, RecipientClient FROM Bookings WHERE ID = ?',
                 (child,)) == [('2026-08-15', '2026-08-20', 'Neuer Empfänger')]
