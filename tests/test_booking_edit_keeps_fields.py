# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Bearbeiten über die Masken (``/transactions/add``) erhält, was die Maske nicht führt.

T25: Die Buchungsmaske und der Editor der Bankmaske schreiben nur noch die Felder, die sie
führen (``update_booking_fields``). Gegenkonto, Kategorie, fremde Kontakte und das fehlende
``Account_ID`` eines Buchungssatzes bleiben beim Speichern; der Editor liest und prüft erst
ALLE Zeilen und schreibt dann – auch die Bankzeile.

Die Formulare stammen aus dem HTML der echten Maske (``PageTransactions``): Alle ``name``/
``value``/``selected``-Werte werden ausgelesen, wie ein Browser sie schickt (samt der
verborgenen Vorlagenzeile des Editors), über denselben ``parse_form`` wie der Server
geschickt und an ``handle_add_transaction`` gegeben – unverändert oder mit gezielter
Änderung. Verglichen wird die ganze Zeile aus SQLite samt Typ der Werte.

Geldkonto vorne (COA = Bank/Kasse/Verrechnungskonto, Gegenkonto = Zweckkonto, v. a. aus dem
WISO-Import): Die Maske zeigt das Geldkonto als „SKR-Konto“. Ein dort gewähltes Zweckkonto
meint das Gegenkonto, „Kein SKR-Konto“ nimmt es zurück, ein anderes Geldkonto ersetzt das
vordere. Maßgeblich sind die Zahlungskonten (``get_payment_coa_ids``, auch 1460), nicht nur
die eigenen Bankkonten – die Tests dazu laufen auf einer frischen Datenbank, deren
Standard-Kasse 1600 führt (1460 ist darin kein eigenes Konto).
"""
from contextlib import closing
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from server.app import parse_form
from server.handlers import (
    _plan_split_rows, _save_split_rows, _write_split_rows, handle_add_transaction,
)
from server.pages_transactions import PageTransactions

IBAN_ONE = 'DE89370400440532013000'      # Test-IBANs aus tests/check_anonymized.py
IBAN_TWO = 'DE02200505501015871393'
IBAN_THREE = 'DE12500105170648489890'


# ── Formular wie ein Browser ─────────────────────────────────────────────────

class _FormReader(HTMLParser):
    """Liest das Formular ``/transactions/add``: ``fields`` ist, was ein Browser abschickt
    (ohne Schaltflächen und ``disabled``; bei ``<select>`` die gewählte Option, sonst die
    erste), ``selects`` die Optionen jeder Auswahl als (Wert, Text, vorgewählt)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = []
        self.selects = {}
        self._in_form = False
        self._select = None
        self._option = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self._in_form = a.get('action') == '/transactions/add'
        elif not self._in_form:
            return
        elif tag == 'input':
            kind = (a.get('type') or 'text').lower()
            if not a.get('name') or 'disabled' in a or kind in ('submit', 'button', 'reset', 'image', 'file'):
                return
            if kind in ('checkbox', 'radio') and 'checked' not in a:
                return
            self.fields.append((a['name'], a.get('value') or ''))
        elif tag == 'select':
            self._select = {'name': a.get('name'), 'options': [], 'disabled': 'disabled' in a}
        elif tag == 'option' and self._select is not None:
            self._option = {'value': a.get('value'), 'text': '', 'selected': 'selected' in a}
            self._select['options'].append(self._option)
        elif tag == 'textarea':
            self._textarea = {'name': a.get('name'), 'text': ''}

    def handle_endtag(self, tag):
        if tag == 'form':
            self._in_form = False
        elif tag == 'option':
            self._option = None
        elif tag == 'select' and self._select is not None:
            select, self._select = self._select, None
            options = select['options']
            self.selects[select['name']] = [
                (o['value'] if o['value'] is not None else o['text'], o['text'], o['selected'])
                for o in options]
            if select['name'] and not select['disabled'] and options:
                chosen = [o for o in options if o['selected']] or options[:1]
                value = chosen[-1]['value']
                self.fields.append((select['name'], value if value is not None else chosen[-1]['text']))
        elif tag == 'textarea' and self._textarea is not None:
            self.fields.append((self._textarea['name'], self._textarea['text']))
            self._textarea = None

    def handle_data(self, data):
        if self._option is not None:
            self._option['text'] += data
        if self._textarea is not None:
            self._textarea['text'] += data


def _read_form(db, booking_id):
    reader = _FormReader()
    reader.feed(PageTransactions(db, edit_transaction_id=booking_id))
    reader.close()
    assert reader.fields, 'Formular /transactions/add nicht gefunden'
    return reader


def _form_post(db, booking_id):
    """Was der Browser beim Speichern der Maske schickt – als {Feld: [Werte]} wie im Server."""
    return parse_form(urlencode(_read_form(db, booking_id).fields))


# ── Zeilen aus SQLite ────────────────────────────────────────────────────────

def _row(db, booking_id):
    """Ganze Zeile aus Bookings: {Spalte: (Wert, Typ)} – so, wie SQLite sie hält."""
    with closing(db._get_connection()) as conn:
        cursor = conn.execute('SELECT * FROM Bookings WHERE ID=?', (booking_id,))
        names = [d[0] for d in cursor.description]
        values = cursor.fetchone()
    return {name: (value, type(value)) for name, value in zip(names, values)}


def _table(db):
    """Alle Zeilen aus Bookings (nach ID) – für 'es hat sich nichts geändert'."""
    with closing(db._get_connection()) as conn:
        cursor = conn.execute('SELECT * FROM Bookings ORDER BY ID')
        names = [d[0] for d in cursor.description]
        return [{name: (value, type(value)) for name, value in zip(names, values)}
                for values in cursor.fetchall()]


def _changed(before, after):
    """Spalten, in denen sich zwei Zeilen unterscheiden."""
    assert before.keys() == after.keys()
    return {column for column in before if before[column] != after[column]}


def _euer(db):
    return {nr: Decimal(str(total))
            for nr, _name, total in db.get_euer_data('2026-01-01', '2026-12-31')}


# ── Testdaten ────────────────────────────────────────────────────────────────

@pytest.fixture
def world(db_with_coa):
    """Seed-Daten (Testbank 1800, Testkonto 2 1810, Testkasse, Lieferant GmbH, Beispiel Kunde AG)
    plus zwei Kategorien."""
    db = db_with_coa
    accounts = {a[1]: a[0] for a in db.fetch_accounts()}
    contacts = {c[2]: c[0] for c in db.fetch_contacts()}            # Kundennummer -> ID
    with closing(db._get_connection()) as conn:
        conn.execute("INSERT INTO Categories (Name, Text) VALUES ('Büro', 'Kategorie A')")
        conn.execute("INSERT INTO Categories (Name, Text) VALUES ('Reise', 'Kategorie B')")
        conn.commit()
        categories = [row[0] for row in conn.execute('SELECT ID FROM Categories ORDER BY ID')]
    return SimpleNamespace(
        db=db, coa=db.get_coa_id_by_account_number,
        bank=accounts['Testbank'], bank_two=accounts['Testkonto 2'], cash=accounts['Testkasse'],
        supplier=contacts['L001'], customer=contacts['K001'],
        category_one=categories[0], category_two=categories[1])


def _mark(db, booking_id):
    """Spalten, die keine Maske führt, mit einem Wert belegen."""
    with closing(db._get_connection()) as conn:
        conn.execute("UPDATE Bookings SET Status='offen', SourceGroup='SG-1' WHERE ID=?",
                     (booking_id,))
        conn.commit()


def _bank_row(w, **kw):
    """Bankbewegung auf der Testbank; jede Spalte belegt, die die Bankmaske führt."""
    values = dict(
        date='2026-03-01', amount=Decimal('-238.00'), account_id=w.bank,
        foreign_bank_account=IBAN_ONE, recipient_client='Lieferant GmbH', contact_id=w.supplier,
        category_id=w.category_one, counter_coa_id=w.coa(1800), currency='EUR',
        text='Sammelrechnung Büro', document_number='26F123', booking_type='bank')
    values.update(kw)
    date = values.pop('date')
    amount = values.pop('amount')
    bank = w.db.insert_booking(date, amount, **values)
    _mark(w.db, bank)
    return bank


def _child_row(w, bank, coa, counter, amount, tax_rate=0.19, **kw):
    """Buchungssatz zu einer Bankbewegung; Datum, Empfänger und Währung wie die Bankzeile
    (der Editor gleicht sie an), alles andere eigen. Der Steuerbetrag ist im Brutto enthalten."""
    rate = Decimal(str(tax_rate))
    values = dict(
        date_tax=None, foreign_bank_account=IBAN_TWO, recipient_client='Lieferant GmbH',
        contact_id=w.customer, coa_id=coa, counter_coa_id=counter, category_id=w.category_two,
        currency='EUR', tax_rate=tax_rate,
        tax_amount=(Decimal(amount) * rate / (1 + rate)).quantize(Decimal('0.01')),
        text='Eigener Zeilentext', document_number='ER-2026-014', booking_type='entry',
        parent_booking_id=bank)
    values.update(kw)
    child = w.db.insert_booking('2026-03-01', Decimal(amount), **values)
    _mark(w.db, child)
    return child


# ── Kindsatz einer Bankbewegung über die Buchungsmaske ───────────────────────

# (Zweckkonto, Betrag, Satz) – die Form ergibt sich aus (COA, Gegenkonto) im Test
SAETZE = [
    pytest.param(4400, '119.00', 0.19, id='einnahme-19'),
    pytest.param(6815, '-59.50', 0.19, id='ausgabe-19'),
    pytest.param(6815, '-107.00', 0.07, id='ausgabe-7'),
]
# (COA, Gegenkonto) zum Zweckkonto z und der Bank-COA b
FORMEN = {
    'zweckkonto-vorne': lambda z, b: (z, b),
    'geldkonto-vorne': lambda z, b: (b, z),
    'gegenkonto-leer': lambda z, b: (z, None),
}


@pytest.mark.parametrize('form', FORMEN)
@pytest.mark.parametrize('purpose, amount, rate', SAETZE)
def test_child_saved_unchanged_keeps_the_whole_row(world, form, purpose, amount, rate):
    """Kindsatz mit Kategorie und Lieferanten-Kontakt: Speichern ohne Änderung lässt die ganze
    Zeile, wie sie ist – auch das Bankkonto, das die Maske nur zur Anzeige vorwählt."""
    w = world
    coa, counter = FORMEN[form](w.coa(purpose), w.coa(1800))
    bank = _bank_row(w, amount=Decimal(amount))
    child = _child_row(w, bank, coa, counter, amount, tax_rate=rate, contact_id=w.supplier,
                       foreign_bank_account=IBAN_THREE)
    before = _row(w.db, child)
    assert before['Account_ID'][0] is None and before['Category_ID'][0] == w.category_two
    assert before['Contact_ID'][0] == w.supplier
    post = _form_post(w.db, child)
    # die Maske zeigt das Bankkonto und den Lieferanten vorgewählt an und schickt beides mit
    assert post['account'] == ([''] if form == 'gegenkonto-leer' else [str(w.bank)])
    assert post['contact_id'] == [str(w.supplier)]

    status, _location = handle_add_transaction(w.db, post)

    assert status == 303
    assert _row(w.db, child) == before


def test_child_purpose_first_account_choice_changes_coa_and_keeps_counter(world):
    w = world
    bank = _bank_row(w, amount=Decimal('119.00'))
    child = _child_row(w, bank, w.coa(4400), w.coa(1800), '119.00', contact_id=w.supplier)
    before = _row(w.db, child)
    post = _form_post(w.db, child)
    assert post['coa_id'] == [str(w.coa(4400))]
    post['coa_id'] = [str(w.coa(4300))]

    assert handle_add_transaction(w.db, post)[0] == 303

    after = _row(w.db, child)
    assert _changed(before, after) == {'COA_ID'}
    assert after['COA_ID'][0] == w.coa(4300)
    assert after['CounterCOA_ID'][0] == w.coa(1800)          # Gegenkonto bleibt
    assert _euer(w.db) == {4300: Decimal('100.00'), 3806: Decimal('19.00')}


@pytest.mark.parametrize('target, euer_after', [
    pytest.param(4300, {4300: Decimal('100.00'), 3806: Decimal('19.00')}, id='erloes-7'),
    pytest.param(4405, {}, id='wartekonto-4405'),
])
def test_child_liquid_first_account_choice_goes_to_the_counter_account(world, target, euer_after):
    """Geldkonto vorne (COA = Bank, Gegenkonto = Erlöse): Die Maske zeigt die Bank als
    „SKR-Konto“. Ein dort gewähltes Zweckkonto meint das Gegenkonto; die Bank bleibt vorne."""
    w = world
    bank = _bank_row(w, amount=Decimal('119.00'))
    child = _child_row(w, bank, w.coa(1800), w.coa(4400), '119.00', contact_id=w.supplier)
    before = _row(w.db, child)
    assert _euer(w.db) == {4400: Decimal('100.00'), 3806: Decimal('19.00')}
    post = _form_post(w.db, child)
    assert post['coa_id'] == [str(w.coa(1800))]              # das Geldkonto steht im „SKR-Konto“
    post['coa_id'] = [str(w.coa(target))]

    assert handle_add_transaction(w.db, post)[0] == 303

    after = _row(w.db, child)
    assert _changed(before, after) == {'CounterCOA_ID'}
    assert after['COA_ID'][0] == w.coa(1800)                  # Geldkonto bleibt vorne
    assert after['CounterCOA_ID'][0] == w.coa(target)
    assert _euer(w.db) == euer_after                          # nichts mehr auf 4400


def test_child_liquid_first_other_money_account_replaces_the_front_account(world):
    """Wählt der Nutzer ein anderes Geldkonto, ersetzt es das vordere – das Gegenkonto
    (Erlöse) bleibt und die Einnahme bleibt in der EÜR."""
    w = world
    bank = _bank_row(w, amount=Decimal('119.00'))
    child = _child_row(w, bank, w.coa(1800), w.coa(4400), '119.00')
    before = _row(w.db, child)
    post = _form_post(w.db, child)
    post['coa_id'] = [str(w.coa(1810))]

    assert handle_add_transaction(w.db, post)[0] == 303

    after = _row(w.db, child)
    assert _changed(before, after) == {'COA_ID'}
    assert after['COA_ID'][0] == w.coa(1810)
    assert after['CounterCOA_ID'][0] == w.coa(4400)
    assert _euer(w.db) == {4400: Decimal('100.00'), 3806: Decimal('19.00')}


# ── Geldkonto vorne, bemessen an den Zahlungskonten (auch 1460) ──────────────
# Frische Datenbank: Die Standard-Kasse führt 1600, 1460 ist also kein eigenes Konto –
# nur ``get_payment_coa_ids`` kennt es als Geldkonto (wie die Importe, die das Vorzeichen
# eines Satzes daran ausrichten).

@pytest.fixture
def fresh(tmp_db):
    db = tmp_db
    for name, skr in (('Frische-Bank-Eins', 1800), ('Frische-Bank-Zwei', 1810)):
        db.insert_account(name, 'Owner', 'DE00FRESHBANK', 'BIC', 'Bank', is_cash=0, skr_account=skr)
    accounts = {a[1]: a[0] for a in db.fetch_accounts()}

    def coa(number):
        return None if number is None else db.get_coa_id_by_account_number(number)

    assert db.get_bank_coa_ids() == {coa(1800), coa(1810), coa(1600)}   # 1460 ist KEIN eigenes Konto …
    assert coa(1460) in db.get_payment_coa_ids()                    # … aber ein Zahlungskonto
    return SimpleNamespace(db=db, coa=coa, bank=accounts['Frische-Bank-Eins'])


def _front_row(f, kind, front, back=4400, amount='119.00', rate=0.19):
    """Satz „front / back“ über 119,00 (19 %). ``child`` hängt unter einer Bankbewegung,
    ``standalone`` steht ohne Bankbewegung (mit dem Bankkonto als Konto, damit die Maske
    kein Konto ableitet und ein Speichern ohne Änderung die Zeile nicht anfasst)."""
    tax = None
    if rate is not None:
        r = Decimal(str(rate))
        tax = (Decimal(amount) * r / (1 + r)).quantize(Decimal('0.01'))
    values = dict(
        recipient_client='Testmarkt', coa_id=f.coa(front), counter_coa_id=f.coa(back),
        tax_rate=rate, tax_amount=tax, text='Kopierpapier', document_number='ER-2026-021',
        booking_type='entry')
    if kind == 'child':
        values['parent_booking_id'] = f.db.insert_booking(
            '2026-03-05', Decimal(amount), account_id=f.bank, booking_type='bank')
    else:
        values['account_id'] = f.bank
    return f.db.insert_booking('2026-03-05', Decimal(amount), **values)


ZAHLUNGSKONTEN = [pytest.param(1800, id='bank'), pytest.param(1460, id='verrechnungskonto')]
ARTEN = [pytest.param('child', id='kind'), pytest.param('standalone', id='eigenstaendig')]


@pytest.mark.parametrize('front', ZAHLUNGSKONTEN)
@pytest.mark.parametrize('kind', ARTEN)
def test_front_money_account_saved_unchanged_keeps_the_whole_row(fresh, kind, front):
    row = _front_row(fresh, kind, front)
    before = _row(fresh.db, row)
    post = _form_post(fresh.db, row)
    assert post['coa_id'] == [str(fresh.coa(front))]         # die Maske zeigt das Geldkonto als „SKR-Konto“

    assert handle_add_transaction(fresh.db, post)[0] == 303

    assert _row(fresh.db, row) == before


@pytest.mark.parametrize('front', ZAHLUNGSKONTEN)
@pytest.mark.parametrize('kind', ARTEN)
def test_front_money_account_purpose_choice_goes_to_the_counter_account(fresh, kind, front):
    """„1460 / 4400“ auf einer frischen Datenbank, SKR-Konto auf 4300: Das Verrechnungskonto
    bleibt vorne, 4300 wird das Gegenkonto. Früher (Regel nur an den eigenen Bankkonten) wurde
    bei 1460 das SKR-Konto 4300 zum vorderen Konto und 4400 blieb stehen – eine stille
    Erlösumbuchung."""
    row = _front_row(fresh, kind, front)
    before = _row(fresh.db, row)
    post = _form_post(fresh.db, row)
    post['coa_id'] = [str(fresh.coa(4300))]

    assert handle_add_transaction(fresh.db, post)[0] == 303

    after = _row(fresh.db, row)
    assert _changed(before, after) == {'CounterCOA_ID'}
    assert after['COA_ID'][0] == fresh.coa(front)
    assert after['CounterCOA_ID'][0] == fresh.coa(4300)


@pytest.mark.parametrize('front', ZAHLUNGSKONTEN)
@pytest.mark.parametrize('kind', ARTEN)
def test_no_skr_account_keeps_the_money_account_and_clears_the_counter_account(fresh, kind, front):
    """„Kein SKR-Konto“ nimmt die Kontierung zurück: Das Geldkonto bleibt vorne, das Zweckkonto
    geht (nicht umgekehrt)."""
    row = _front_row(fresh, kind, front)
    before = _row(fresh.db, row)
    post = _form_post(fresh.db, row)
    post['coa_id'] = ['']

    assert handle_add_transaction(fresh.db, post)[0] == 303

    after = _row(fresh.db, row)
    assert _changed(before, after) == {'CounterCOA_ID'}
    assert after['COA_ID'][0] == fresh.coa(front)
    assert after['CounterCOA_ID'] == (None, type(None))


def test_no_skr_account_on_a_bank_front_row_takes_it_out_of_the_euer(fresh):
    row = _front_row(fresh, 'child', 1800)
    assert _euer(fresh.db) == {4400: Decimal('100.00'), 3806: Decimal('19.00')}
    post = _form_post(fresh.db, row)
    post['coa_id'] = ['']

    assert handle_add_transaction(fresh.db, post)[0] == 303

    assert _euer(fresh.db) == {}


def test_no_skr_account_on_a_clearing_account_front_row_takes_the_revenue_out_of_the_euer(fresh):
    """Die EÜR liest „1460 / 4400“ als Zahlung (das Verrechnungskonto ist ein Zahlungskonto)
    und bucht den Erlös. Nach „Kein SKR-Konto“ zählt der Erlös (4400 samt USt 3806) nicht
    mehr."""
    row = _front_row(fresh, 'child', 1460)
    before = _euer(fresh.db)
    assert before[4400] == Decimal('100.00') and before[3806] == Decimal('19.00')
    post = _form_post(fresh.db, row)
    post['coa_id'] = ['']

    assert handle_add_transaction(fresh.db, post)[0] == 303

    after = _euer(fresh.db)
    assert 4400 not in after and 3806 not in after


@pytest.mark.parametrize('front, choice', [
    pytest.param(1800, 1810, id='bank-wird-andere-bank'),
    pytest.param(1800, 1460, id='bank-wird-verrechnungskonto'),
    pytest.param(1460, 1800, id='verrechnungskonto-wird-bank'),
])
def test_front_money_account_choice_of_another_money_account_replaces_it(fresh, front, choice):
    """Ein anderes Geldkonto ist kein Zweckkonto: Es ersetzt das vordere, das Gegenkonto bleibt."""
    row = _front_row(fresh, 'child', front)
    before = _row(fresh.db, row)
    post = _form_post(fresh.db, row)
    post['coa_id'] = [str(fresh.coa(choice))]

    assert handle_add_transaction(fresh.db, post)[0] == 303

    after = _row(fresh.db, row)
    assert _changed(before, after) == {'COA_ID'}
    assert after['COA_ID'][0] == fresh.coa(choice)
    assert after['CounterCOA_ID'][0] == fresh.coa(4400)


@pytest.mark.parametrize('front, back', [
    pytest.param(4400, 1800, id='zweckkonto-vorne'),
    pytest.param(4400, None, id='gegenkonto-leer'),
    pytest.param(4405, 4400, id='umbuchung'),
])
@pytest.mark.parametrize('choice', [
    pytest.param(4300, id='anderes-zweckkonto'),
    pytest.param(None, id='kein-skr-konto'),
])
def test_purpose_front_row_takes_the_choice_as_coa_and_keeps_the_counter_account(fresh, front, back,
                                                                                 choice):
    """Zweckkonto vorne (und Umbuchung): Die Maske schreibt das SKR-Konto, wie gewählt – auch
    leer – und lässt das Gegenkonto stehen."""
    row = _front_row(fresh, 'child', front, back)
    before = _row(fresh.db, row)
    post = _form_post(fresh.db, row)
    assert post['coa_id'] == [str(fresh.coa(front))]
    post['coa_id'] = [str(fresh.coa(choice)) if choice else '']

    assert handle_add_transaction(fresh.db, post)[0] == 303

    after = _row(fresh.db, row)
    assert _changed(before, after) == {'COA_ID'}
    assert after['COA_ID'][0] == fresh.coa(choice)
    assert after['CounterCOA_ID'] == before['CounterCOA_ID']


def test_mirror_row_takes_the_choice_as_coa(fresh):
    """Spiegel (Geldkonto an Geldkonto) hat hinten kein Zweckkonto: keine Umkehr. Die Wahl ersetzt
    das vordere Konto, aus dem Spiegel wird eine Zahlung (Zweckkonto vorne, Geldkonto hinten)."""
    row = _front_row(fresh, 'child', 1800, 1810, amount='500.00', rate=None)
    assert _euer(fresh.db) == {}
    before = _row(fresh.db, row)
    post = _form_post(fresh.db, row)
    post['coa_id'] = [str(fresh.coa(4300))]

    assert handle_add_transaction(fresh.db, post)[0] == 303

    after = _row(fresh.db, row)
    assert _changed(before, after) == {'COA_ID'}
    assert (after['COA_ID'][0], after['CounterCOA_ID'][0]) == (fresh.coa(4300), fresh.coa(1810))
    assert _euer(fresh.db) == {4300: Decimal('500.00')}


@pytest.mark.parametrize('choice, counter', [
    pytest.param('same', 4400, id='unveraendert'),
    pytest.param('4300', 4300, id='zweckkonto-4300'),
    pytest.param('none', None, id='kein-skr-konto'),
])
@pytest.mark.parametrize('account', [
    pytest.param(None, id='ohne-konto'),
    pytest.param('Kasse', id='mit-standard-kasse'),
])
def test_clearing_account_front_row_on_a_database_without_any_bank_account(tmp_db, account, choice,
                                                                           counter):
    """Ganz frische Datenbank: kein Bankkonto, nur die Standard-Kasse mit 1600. Ein
    eigenständiger Satz „1460 / 4400“ (+119,00, 19 %) behält das Verrechnungskonto vorne."""
    db = tmp_db
    coa = db.get_coa_id_by_account_number
    assert db.get_bank_coa_ids() == {coa(1600)}
    account_id = next(a[0] for a in db.fetch_accounts() if a[1] == account) if account else None
    row = db.insert_booking(
        '2026-03-05', Decimal('119.00'), account_id=account_id, recipient_client='Testmarkt',
        coa_id=coa(1460), counter_coa_id=coa(4400), tax_rate=0.19, tax_amount=Decimal('19.00'),
        text='Kopierpapier', document_number='ER-2026-021', booking_type='entry')
    before = _row(db, row)
    post = _form_post(db, row)
    assert post['coa_id'] == [str(coa(1460))]
    if choice != 'same':
        post['coa_id'] = [str(coa(int(choice))) if choice != 'none' else '']

    assert handle_add_transaction(db, post)[0] == 303

    after = _row(db, row)
    assert after['COA_ID'][0] == coa(1460)
    assert after['CounterCOA_ID'][0] == (coa(counter) if counter else None)
    assert _changed(before, after) == (set() if choice == 'same' else {'CounterCOA_ID'})


# ── eigenständige Buchung ohne Bankbewegung: Account_ID kommt aus dem Formular ──

def _standalone_cash_entry(w):
    entry = w.db.insert_booking(
        '2026-03-02', Decimal('-18.35'), date_tax='2026-03-03', account_id=w.cash,
        foreign_bank_account=IBAN_THREE, recipient_client='Testmarkt', contact_id=w.supplier,
        coa_id=w.coa(6815), counter_coa_id=w.coa(1460), category_id=w.category_one,
        currency='EUR', tax_rate=0.19, tax_amount=Decimal('-2.93'), text='Kopierpapier',
        document_number='KASSE-2026-01', booking_type='entry')
    _mark(w.db, entry)
    return entry


def test_standalone_entry_saved_unchanged_keeps_the_whole_row(world):
    w = world
    entry = _standalone_cash_entry(w)
    before = _row(w.db, entry)
    post = _form_post(w.db, entry)
    assert post['account'] == [str(w.cash)]

    assert handle_add_transaction(w.db, post)[0] == 303

    assert _row(w.db, entry) == before
    assert before['Account_ID'][0] == w.cash


@pytest.mark.parametrize('account', ['bank', 'none'])
def test_standalone_entry_takes_account_id_from_the_form(world, account):
    """Ohne Bankbewegung (kein Parent) führt die Maske das Konto: Was dort steht, wird übernommen."""
    w = world
    entry = _standalone_cash_entry(w)
    before = _row(w.db, entry)
    post = _form_post(w.db, entry)
    expected = w.bank if account == 'bank' else None
    post['account'] = [str(expected) if expected else '']

    assert handle_add_transaction(w.db, post)[0] == 303

    after = _row(w.db, entry)
    assert _changed(before, after) == {'Account_ID'}
    assert after['Account_ID'][0] == expected


# ── Bankbewegung mit Buchungssätzen über die Bankmaske ───────────────────────

def _bank_with_two_children(w):
    bank = _bank_row(w, amount=Decimal('-238.00'))
    first = _child_row(w, bank, w.coa(6815), w.coa(1800), '-200.00', contact_id=w.supplier,
                       category_id=w.category_one, foreign_bank_account=IBAN_ONE,
                       text='Büromaterial', document_number='26F123_A')
    second = _child_row(w, bank, w.coa(6300), w.coa(1800), '-38.00', contact_id=w.customer,
                        category_id=w.category_two, foreign_bank_account=IBAN_TWO,
                        text='Versand', document_number='26F123_B')
    return SimpleNamespace(bank=bank, first=first, second=second,
                           ids=(bank, first, second))


def _snapshot(w, booking):
    return {i: _row(w.db, i) for i in booking.ids}


def test_bank_mask_saved_unchanged_keeps_bank_row_and_both_children(world):
    """Zwei Teilbuchungen mit eigenem Kontakt, eigener Kategorie und eigenem fremden Konto:
    Speichern der Bankmaske ohne Änderung lässt alle drei Zeilen und die ganze Tabelle."""
    w = world
    booking = _bank_with_two_children(w)
    before = _snapshot(w, booking)
    table_before = _table(w.db)
    post = _form_post(w.db, booking.bank)
    # Editor mit beiden Zeilen und der verborgenen Vorlagenzeile
    assert post['split_id'] == [str(booking.first), str(booking.second), '']
    assert post['contact_id'] == [str(w.supplier)]

    status, _location = handle_add_transaction(w.db, post)

    assert status == 303
    assert _snapshot(w, booking) == before
    assert _table(w.db) == table_before


def test_bank_mask_edit_changes_only_what_the_editor_leads(world):
    w = world
    booking = _bank_with_two_children(w)
    before = _snapshot(w, booking)
    post = _form_post(w.db, booking.bank)
    post['text'] = ['Neuer Verwendungszweck der Bank']
    post['split_amount'][0] = '-190.00'
    post['split_text'][0] = 'Neuer Zeilentext'
    post['split_docnr'][0] = '26F123_A2'

    assert handle_add_transaction(w.db, post)[0] == 303

    after = _snapshot(w, booking)
    assert _changed(before[booking.bank], after[booking.bank]) == {'Text'}
    assert _changed(before[booking.first], after[booking.first]) == {'Amount', 'Text', 'DocumentNumber'}
    assert after[booking.first]['Amount'] == (-1900000, int)
    assert after[booking.second] == before[booking.second]


def test_bank_mask_adopting_an_existing_entry_keeps_its_own_fields(world):
    """„Vorhandene Buchung zuordnen“: Die zugeordnete Buchung behält Kontakt, Kategorie, Konto
    und fremdes Konto; geschrieben werden nur die Verknüpfung und die Gegenseite."""
    w = world
    booking = _bank_with_two_children(w)
    adoptee = w.db.insert_booking(
        '2026-02-20', Decimal('-50.00'), account_id=w.cash, foreign_bank_account=IBAN_THREE,
        recipient_client='Eigener Lieferant', contact_id=w.customer, coa_id=w.coa(6815),
        category_id=w.category_one, tax_rate=0.19, tax_amount=Decimal('-7.98'),
        text='Eigener Text', document_number='ER-ADOPT', booking_type='entry')
    _mark(w.db, adoptee)
    before = _row(w.db, adoptee)
    post = _form_post(w.db, booking.bank)
    # die Zeile, die „Zuordnen“ im Browser einfügt: vor der Vorlagenzeile, mit der ID der Buchung
    _add_editor_row(post, split_id=str(adoptee), split_text='Eigener Text', split_amount='-50.00',
                    split_coa=str(w.coa(6815)), split_tax_rate='19', split_tax_amount='-7.98',
                    split_docnr='ER-ADOPT')

    assert handle_add_transaction(w.db, post)[0] == 303

    after = _row(w.db, adoptee)
    assert _changed(before, after) == {'ParentBooking_ID', 'CounterCOA_ID'}
    assert after['ParentBooking_ID'][0] == booking.bank
    assert after['CounterCOA_ID'][0] == w.coa(1800)
    for column in ('Contact_ID', 'Category_ID', 'Account_ID', 'ForeignBankAccount',
                   'DateBooking', 'RecipientClient'):
        assert after[column] == before[column], column


def _add_editor_row(post, **values):
    """Fügt dem Editor eine Zeile hinzu – vor die verborgene Vorlagenzeile, wie „+ Teilbuchung“."""
    row = {'split_id': '', 'split_text': 'Neue Zeile', 'split_counter_coa': '', 'split_amount': '',
           'split_coa': '', 'split_tax_rate': '', 'split_tax_amount': '', 'split_docnr': ''}
    assert set(values) <= set(row), values
    row.update(values)
    for name, value in row.items():
        assert post[name][-1] == '', 'letzte Zeile ist nicht die leere Vorlagenzeile'
        post[name].insert(len(post[name]) - 1, value)


# ── ungültige Zahl im Editor: erst prüfen, dann schreiben ────────────────────

def _edit_everything(post):
    """Änderungen, die ohne den Fehler gespeichert würden: Bankzeile und erste Teilbuchung."""
    post['text'] = ['Geänderter Verwendungszweck der Bank']
    post['split_amount'][0] = '-190.00'
    post['split_text'][0] = 'Geänderter Zeilentext'


def test_valid_edit_of_bank_row_and_first_split_is_saved(world):
    """Gegenprobe zu den Fehlerfällen: Dieselben Änderungen ohne ungültige Zahl kommen an."""
    w = world
    booking = _bank_with_two_children(w)
    before = _snapshot(w, booking)
    post = _form_post(w.db, booking.bank)
    _edit_everything(post)

    assert handle_add_transaction(w.db, post)[0] == 303

    after = _snapshot(w, booking)
    assert after[booking.bank]['Text'] == ('Geänderter Verwendungszweck der Bank', str)
    assert after[booking.first]['Amount'] == (-1900000, int)
    assert after[booking.first]['Text'] == ('Geänderter Zeilentext', str)
    assert after[booking.second] == before[booking.second]


@pytest.mark.parametrize('field, value', [
    pytest.param('split_amount', 'abc', id='betrag'),
    pytest.param('split_tax_rate', 'neunzehn', id='steuersatz'),
    pytest.param('split_tax_amount', 'x', id='steuerbetrag'),
    pytest.param('split_coa', 'abc', id='konto'),
])
def test_invalid_number_in_last_editor_row_leaves_everything_unchanged(world, field, value):
    w = world
    booking = _bank_with_two_children(w)
    table_before = _table(w.db)
    post = _form_post(w.db, booking.bank)
    _edit_everything(post)
    post[field][1] = value                                   # letzte Zeile des Editors

    status, message = handle_add_transaction(w.db, post)

    assert status == 500 and message.startswith('Fehler beim Speichern')
    assert _table(w.db) == table_before                      # Bankzeile UND alle Teilbuchungen


def test_invalid_number_in_a_new_editor_row_leaves_everything_unchanged(world):
    """Eine neue Zeile mit Dezimalkomma (statt -Punkt) als letzte Zeile des Editors."""
    w = world
    booking = _bank_with_two_children(w)
    table_before = _table(w.db)
    post = _form_post(w.db, booking.bank)
    _edit_everything(post)
    _add_editor_row(post, split_amount='-12,5', split_coa=str(w.coa(6850)))

    status, _message = handle_add_transaction(w.db, post)

    assert status == 500
    assert _table(w.db) == table_before


def _new_movement_post(w, rows):
    """Neue Bankbewegung mit Editor-Zeilen (ohne Maske: Die Maske für eine neue Buchung hat
    keinen Editor, das Formular wird von Hand gebaut)."""
    post = {'transaction_id': ['0'], 'date': ['2026-03-01'], 'amount': ['-238.00'],
            'account': [str(w.bank)], 'currency': ['EUR'], 'recipient': ['Lieferant GmbH'],
            'text': ['Sammelrechnung'], 'split_id': [], 'split_text': [], 'split_counter_coa': [],
            'split_amount': [], 'split_coa': [], 'split_tax_rate': [], 'split_tax_amount': [],
            'split_docnr': []}
    for row in rows:
        post['split_id'].append('')
        post['split_text'].append(row.get('text', 'Teil'))
        post['split_counter_coa'].append('')
        post['split_amount'].append(row['amount'])
        post['split_coa'].append(row.get('coa', ''))
        post['split_tax_rate'].append(row.get('rate', ''))
        post['split_tax_amount'].append(row.get('tax', ''))
        post['split_docnr'].append(row.get('docnr', ''))
    return post


def test_new_bank_movement_with_valid_editor_rows_is_created(world):
    """Gegenprobe: Ohne ungültige Zahl entstehen die Bankbewegung und ihre zwei Sätze."""
    w = world
    count_before = len(_table(w.db))
    post = _new_movement_post(w, [
        {'amount': '-200.00', 'coa': str(w.coa(6815)), 'rate': '19', 'tax': '-31.93'},
        {'amount': '-38.00', 'coa': str(w.coa(6300))},
    ])

    status, _location = handle_add_transaction(w.db, post)

    assert status == 303
    table = _table(w.db)
    assert len(table) == count_before + 3
    bank = table[-3]
    assert bank['BookingType'] == ('bank', str)
    assert [row['ParentBooking_ID'][0] for row in table[-2:]] == [bank['ID'][0]] * 2


@pytest.mark.parametrize('field, value', [
    pytest.param('split_amount', 'abc', id='betrag'),
    pytest.param('split_tax_rate', 'x', id='steuersatz'),
    pytest.param('split_coa', 'abc', id='konto'),
])
def test_invalid_number_in_new_bank_movement_creates_nothing(world, field, value):
    w = world
    table_before = _table(w.db)
    post = _new_movement_post(w, [
        {'amount': '-200.00', 'coa': str(w.coa(6815)), 'rate': '19', 'tax': '-31.93'},
        {'amount': '-38.00', 'coa': str(w.coa(6300))},
    ])
    post[field][1] = value

    status, message = handle_add_transaction(w.db, post)

    assert status == 500 and message.startswith('Fehler beim Speichern')
    assert _table(w.db) == table_before                      # keine Bankbewegung, keine Sätze


# ── Editor der Bankmaske: planen, schreiben, speichern ───────────────────────

SPLIT_FIELDS = ('split_id', 'split_text', 'split_counter_coa', 'split_amount', 'split_coa',
                'split_tax_rate', 'split_tax_amount', 'split_docnr')


def _plan_args(w, booking, post):
    """Argumente von ``_plan_split_rows``, wie ``handle_add_transaction`` sie für die Bankzeile bildet."""
    return (w.db, booking.bank, post, w.coa(1800), '2026-03-01', None, 'Lieferant GmbH', 'EUR')


def _drop_editor_row(post, index):
    """Entfernt eine Zeile des Editors (wie das ✕ im Browser)."""
    for name in SPLIT_FIELDS:
        del post[name][index]


def _children_by_id(w, booking):
    return {c[0]: c for c in w.db.get_child_bookings_for_bank(booking.bank)}


def test_plan_split_rows_reads_all_rows_and_writes_nothing(world):
    w = world
    booking = _bank_with_two_children(w)
    post = _form_post(w.db, booking.bank)
    _edit_everything(post)
    _add_editor_row(post, split_amount='-12.00', split_coa=str(w.coa(6850)))
    table_before = _table(w.db)

    planned = _plan_split_rows(*_plan_args(w, booking, post))

    assert _table(w.db) == table_before                     # planen schreibt nichts
    # zwei bestehende Zeilen und die neue; die leere Vorlagenzeile fällt weg
    assert [rid for rid, _fields, _adopt in planned] == [booking.first, booking.second, None]
    assert [adopt for _rid, _fields, adopt in planned] == [False, False, False]


def test_plan_split_rows_marks_an_unlinked_booking_as_adopted(world):
    w = world
    booking = _bank_with_two_children(w)
    free = w.db.insert_booking('2026-02-20', Decimal('-50.00'), coa_id=w.coa(6815),
                               booking_type='entry', text='Frei')
    post = _form_post(w.db, booking.bank)
    _add_editor_row(post, split_id=str(free), split_amount='-50.00', split_coa=str(w.coa(6815)))

    planned = _plan_split_rows(*_plan_args(w, booking, post))

    assert [(rid, adopt) for rid, _fields, adopt in planned] == [
        (booking.first, False), (booking.second, False), (free, True)]


@pytest.mark.parametrize('field, value', [
    pytest.param('split_amount', 'abc', id='betrag'),
    pytest.param('split_tax_rate', 'neunzehn', id='steuersatz'),
    pytest.param('split_coa', 'abc', id='konto'),
])
def test_plan_split_rows_with_an_invalid_number_raises_and_writes_nothing(world, field, value):
    w = world
    booking = _bank_with_two_children(w)
    post = _form_post(w.db, booking.bank)
    _edit_everything(post)
    post[field][1] = value
    table_before = _table(w.db)

    with pytest.raises(ValueError):
        _plan_split_rows(*_plan_args(w, booking, post))

    assert _table(w.db) == table_before


def _edited_post_with_a_new_row_and_the_second_row_removed(w, booking):
    post = _form_post(w.db, booking.bank)
    _edit_everything(post)                                   # erste Zeile geändert
    _drop_editor_row(post, 1)                                # zweite Zeile entfernt
    _add_editor_row(post, split_amount='-12.00', split_coa=str(w.coa(6850)), split_text='Neue Zeile')
    return post


def _assert_plan_was_written(w, booking):
    children = _children_by_id(w, booking)
    assert booking.second not in children                    # entfernte Zeile ist gelöscht
    assert w.db.get_booking_by_id(booking.second) is None
    assert children[booking.first][4] == Decimal('-190.0000')           # geänderte Zeile
    (new_id,) = set(children) - {booking.first}
    new = w.db.get_booking_by_id(new_id)
    assert (new[8], new[9]) == (w.coa(6850), w.coa(1800))   # Zweckkonto vorne, Bank hinten
    assert new[11] == Decimal('-12.0000') and new[18] == booking.bank
    assert new[17] == 'entry' and new[15] == 'Neue Zeile'


def test_write_split_rows_writes_the_plan_and_removes_dropped_rows(world):
    w = world
    booking = _bank_with_two_children(w)
    post = _edited_post_with_a_new_row_and_the_second_row_removed(w, booking)
    planned = _plan_split_rows(*_plan_args(w, booking, post))
    table_before = _table(w.db)

    _write_split_rows(w.db, booking.bank, planned)

    assert _table(w.db) != table_before
    _assert_plan_was_written(w, booking)


def test_save_split_rows_plans_and_writes(world):
    w = world
    booking = _bank_with_two_children(w)
    post = _edited_post_with_a_new_row_and_the_second_row_removed(w, booking)

    _save_split_rows(*_plan_args(w, booking, post))

    _assert_plan_was_written(w, booking)


def test_save_split_rows_with_an_invalid_number_writes_nothing(world):
    w = world
    booking = _bank_with_two_children(w)
    post = _edited_post_with_a_new_row_and_the_second_row_removed(w, booking)
    post['split_amount'][1] = 'abc'                          # die neue, letzte Zeile
    table_before = _table(w.db)

    with pytest.raises(ValueError):
        _save_split_rows(*_plan_args(w, booking, post))

    assert _table(w.db) == table_before


def test_plan_split_rows_for_a_new_bank_movement_has_no_existing_rows(world):
    """``bank_id`` ist None, solange die Bankbewegung erst noch angelegt wird."""
    w = world
    post = _new_movement_post(w, [
        {'amount': '-200.00', 'coa': str(w.coa(6815))},
        {'amount': '-38.00', 'coa': str(w.coa(6300))},
    ])
    table_before = _table(w.db)

    planned = _plan_split_rows(w.db, None, post, w.coa(1800), '2026-03-01', None, 'Lieferant GmbH', 'EUR')

    assert _table(w.db) == table_before
    assert [(rid, adopt) for rid, _fields, adopt in planned] == [(None, False), (None, False)]


# ── Auswahl „Kunde“ ──────────────────────────────────────────────────────────

def _entry_with_contact(w, contact_id):
    entry = w.db.insert_booking('2026-03-02', Decimal('-18.35'), account_id=w.cash,
                                contact_id=contact_id, coa_id=w.coa(6815),
                                counter_coa_id=w.coa(1460), booking_type='entry', text='Beleg')
    return entry


def test_customer_select_holds_a_supplier_once_and_preselected(world):
    """Ein Lieferant ist kein Kunde. Er steht trotzdem in der Auswahl und ist vorgewählt –
    sonst schickte das Formular einen leeren Kontakt und löschte ihn aus der Buchung."""
    w = world
    entry = _entry_with_contact(w, w.supplier)
    reader = _read_form(w.db, entry)

    options = reader.selects['contact_id']
    supplier = [(value, selected) for value, _text, selected in options if value == str(w.supplier)]
    assert supplier == [(str(w.supplier), True)]              # genau einmal, vorgewählt
    assert [o[0] for o in options if o[2]] == [str(w.supplier)]
    assert str(w.customer) in [o[0] for o in options]         # die Kunden bleiben in der Auswahl
    assert options[0][0] == ''                                # „-- Kein Kunde --“
    assert ('contact_id', str(w.supplier)) in reader.fields


def test_customer_select_holds_a_customer_exactly_once(world):
    w = world
    entry = _entry_with_contact(w, w.customer)

    options = _read_form(w.db, entry).selects['contact_id']

    customer = [(value, selected) for value, _text, selected in options if value == str(w.customer)]
    assert customer == [(str(w.customer), True)]              # genau einmal, vorgewählt
    assert [o[0] for o in options if o[2]] == [str(w.customer)]


def test_customer_select_without_contact_offers_no_supplier(world):
    w = world
    entry = _entry_with_contact(w, None)
    reader = _read_form(w.db, entry)

    options = reader.selects['contact_id']

    assert str(w.supplier) not in [o[0] for o in options]    # Lieferanten nur, wenn gespeichert
    assert not any(o[2] for o in options)
    assert ('contact_id', '') in reader.fields


def test_customer_select_survives_a_contact_that_no_longer_exists(world):
    """Ein Kontakt, den es nicht mehr gibt (Altbestand, Fremdschlüssel nicht erzwungen),
    lässt die Maske nicht abstürzen."""
    w = world
    entry = _entry_with_contact(w, w.supplier)
    with closing(w.db._get_connection()) as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('UPDATE Bookings SET Contact_ID=? WHERE ID=?', (999999, entry))
        conn.commit()

    reader = _read_form(w.db, entry)

    options = reader.selects['contact_id']
    assert '999999' not in [o[0] for o in options]
    assert not any(o[2] for o in options)
    assert ('contact_id', '') in reader.fields
