# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die IDs im Buchungssatz-Editor der Bankbewegung sind Eingaben des Browsers (T37).

Eine Zeile mit ``split_id`` ist entweder ein Satz, der schon unter der Bankbewegung hängt, oder „vorhandene Buchung
zuordnen“: Dann bekommt die Buchung ``ParentBooking_ID`` = die Bankbewegung. Der Server prüft deshalb jede ID, die kein eigener
Satz der Bewegung ist (``handlers._split_row_bookings``), bevor etwas geschrieben wird: Die Buchung muss es geben, ein
Buchungssatz sein, zu keiner Bankbewegung gehören und vom Editor zuordenbar sein (``Database.adoptable_entry_ids`` – dieselbe
Bedingung wie die Kandidaten des Zuordnen-Widgets, nur ohne dessen Grenze von 50). Abgelehnt wird mit 400 und Klartext; nichts
ist gespeichert, auch nicht, was das Formular sonst geändert hätte. Dasselbe gilt für eine Bankbewegung, die erst angelegt wird.

Dazu gehören die Schreibweise der ID (``'05'`` ist dieselbe ID, ``'+5'`` keine), ein Satz mit der Marke einer
Rechnungszahlung ohne Bankbewegung (nie zuordenbar), das Stückeln der Abfrage bei vielen IDs und die Grenze der Zeilen je
Anfrage (``handlers.MAX_SPLIT_ROWS``): Der Editor plant und schreibt unter der Schreibsperre der Datenbank.

Die Formulare stammen aus dem HTML der echten Maske (``tests/entry_form_roundtrip.py``). Alle Namen und Werte sind erfunden.
"""
from contextlib import closing
from decimal import Decimal

import pytest

from db.invoice_payments import PAYMENT_MARK
from db.matching import _ID_CHUNK
from server.handlers import MAX_SPLIT_ROWS, handle_add_transaction, link_booking_to_invoice_capped
from tests.document_helpers import state
from tests.entry_form_helpers import bank_account, coa, execute, minor, query
from tests.entry_form_roundtrip import add_row, adopt_row, differences, open_form, row_fields, snapshot, submit
from tests.http_client import post

STATE_TABLES = ('Bookings', 'InvoicePayments', 'Invoices')

# Die Klartexte; vorn steht „Zeile n: “
OTHER_MOVEMENT = 'Dieser Buchungssatz gehört schon zu einer anderen Bankbewegung. Bitte die Seite neu laden.'
A_BANK_MOVEMENT = 'Das ist eine Bankbewegung und kein Buchungssatz.'
GONE = 'Diese Buchung gibt es nicht (mehr). Bitte die Seite neu laden.'
NOT_ADOPTABLE = 'Dieser Buchungssatz lässt sich keiner Bankbewegung zuordnen.'
UNKNOWN = 'unbekannte Buchung.'
TWICE = 'Diese Buchung steht schon in einer anderen Zeile.'


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Wächter-Bank', 1800)
    return tmp_db


def _giro(db):
    return next(a[0] for a in db.fetch_accounts() if a[1] == 'Wächter-Bank')


def _movement(db, amount='-50.00', date='2026-08-01', text='Zuordnungsziel'):
    return db.insert_booking(date, Decimal(amount), account_id=_giro(db), booking_type='bank',
                             recipient_client='Bank-Empfänger', text=text)


def _free_entry(db, amount='-50.00', date='2026-07-15', **over):
    """Ein Buchungssatz, wie das Zuordnen-Widget ihn anbietet: ohne Bankbewegung, ohne eigenes Konto."""
    values = dict(coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800), booking_type='entry',
                  text='Freier Satz', document_number='ER-FREI')
    values.update(over)
    return db.insert_booking(date, Decimal(amount), **values)


def _invoice(db):
    return db.insert_invoice({
        'invoice_number': 'RE-GUARD', 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19,
        'sum_net': 100.00, 'tax_amount': 19.00, 'sum_gross': 119.00, 'amount_due': 119.00,
        'status': 'sent'})


# ── die eingeschleusten IDs: (Text der ID, Klartext der Ablehnung) ───────────────────────────────────────────────────

def _child_of_another_movement(db, bank):
    other = _movement(db, '-80.00', text='Andere Bewegung')
    child = _free_entry(db, '-80.00', parent_booking_id=other)
    return str(child), OTHER_MOVEMENT


def _payment_row_of_another_assignment(db, bank):
    """Ein markierter Zahlungssatz („Bank an Debitoren“) der Zuordnung einer Rechnung an eine andere Bankbewegung."""
    invoice = _invoice(db)
    db.ensure_receivable_booking(invoice)
    other = db.insert_booking('2026-02-01', Decimal('119.00'), account_id=_giro(db), booking_type='bank',
                              recipient_client='Käufer AG')
    assert link_booking_to_invoice_capped(db, invoice, other) == (True, None)
    marked = [row[0] for row in query(
        db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND Status = ? ORDER BY ID', (other, PAYMENT_MARK))]
    assert len(marked) == 2
    return str(marked[0]), OTHER_MOVEMENT


def _another_bank_movement(db, bank):
    return str(_movement(db, '-12.00', text='Eine zweite Bewegung')), A_BANK_MOVEMENT


def _the_movement_itself(db, bank):
    return str(bank), A_BANK_MOVEMENT


def _a_receivable(db, bank):
    """Die Forderung einer versendeten Rechnung: ein eigenständiger Satz, aber nie einer Bankbewegung zuzuordnen."""
    invoice = _invoice(db)
    db.ensure_receivable_booking(invoice)
    (receivable,) = [row[0] for row in query(
        db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'RE-GUARD' AND BookingType = 'entry'")]
    assert query(db, 'SELECT ParentBooking_ID, Account_ID, Status FROM Bookings WHERE ID = ?',
                 (receivable,)) == [(None, None, None)]                 # nur die Forderung selbst hält ihn fern
    return str(receivable), NOT_ADOPTABLE


def _no_such_booking(db, bank):
    return '999999', GONE


def _not_a_number(db, bank):
    return 'abc', UNKNOWN


def _an_entry_with_its_own_account(db, bank):
    return str(_free_entry(db, account_id=_giro(db))), NOT_ADOPTABLE


def _a_resolved_entry(db, bank):
    booking = _free_entry(db)
    execute(db, "UPDATE Bookings SET Status = 'resolved' WHERE ID = ?", (booking,))
    return str(booking), NOT_ADOPTABLE


def _a_free_payment_row(db, bank):
    """Ein Satz mit der Marke einer Rechnungszahlung, aber ohne Bankbewegung: frei gibt es ihn nur nach einem Eingriff an der
    App vorbei (SQL-Konsole) – er gehörte zu einer Zuordnung, nicht in den Editor."""
    booking = _free_entry(db)
    execute(db, 'UPDATE Bookings SET Status = ? WHERE ID = ?', (PAYMENT_MARK, booking))
    return str(booking), NOT_ADOPTABLE


def _a_payment_row_detached_by_sql(db, bank):
    """Der Satz „Bank an Debitoren“ einer echten Zuordnung, den ein Eingriff an der App vorbei von der Bankbewegung gelöst hat."""
    invoice = _invoice(db)
    db.ensure_receivable_booking(invoice)
    other = db.insert_booking('2026-02-01', Decimal('119.00'), account_id=_giro(db), booking_type='bank',
                              recipient_client='Käufer AG')
    assert link_booking_to_invoice_capped(db, invoice, other) == (True, None)
    marked = [row[0] for row in query(
        db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND Status = ? ORDER BY ID', (other, PAYMENT_MARK))]
    execute(db, 'UPDATE Bookings SET ParentBooking_ID = NULL WHERE ID = ?', (marked[0],))
    return str(marked[0]), NOT_ADOPTABLE


# Je ID: (Aufbau, gilt auch für eine neue Bankbewegung)
HOSTILE = {
    'kind-einer-anderen-bewegung': (_child_of_another_movement, True),
    'zahlungssatz-einer-anderen-zuordnung': (_payment_row_of_another_assignment, True),
    'andere-bankzeile': (_another_bank_movement, True),
    'die-bewegung-selbst': (_the_movement_itself, False),
    'forderung': (_a_receivable, True),
    'unbekannte-id': (_no_such_booking, True),
    'keine-zahl': (_not_a_number, True),
    'satz-mit-eigenem-konto': (_an_entry_with_its_own_account, True),
    'geloester-satz': (_a_resolved_entry, True),
    'freier-markierter-satz': (_a_free_payment_row, True),
    'vom-eingriff-geloester-zahlungssatz': (_a_payment_row_detached_by_sql, True),
}

# Andere Schreibweisen der ID eines Satzes einer anderen Bankbewegung: (Schreibweise, Klartext der Ablehnung). Was nach dem
# Abschneiden von Leerraum eine ASCII-Ganzzahl ist, meint dieselbe ID (``handlers._form_id``); alles andere – Vorzeichen,
# Dezimalpunkt, Ziffern einer anderen Schrift – ist keine ID, auch wenn ``int()`` es läse.
ARABIC_INDIC = str.maketrans('0123456789', ''.join(chr(0x0660 + n) for n in range(10)))
SPELLINGS = {
    'fuehrende-null': (lambda i: f'0{i}', OTHER_MOVEMENT),
    'leerzeichen-hinten': (lambda i: f'{i} ', OTHER_MOVEMENT),
    'leerzeichen-vorn': (lambda i: f' {i}', OTHER_MOVEMENT),
    'leerzeichen-beidseitig': (lambda i: f' {i} ', OTHER_MOVEMENT),
    'geschuetzte-leerzeichen': (lambda i: f'\xa0{i}\xa0', OTHER_MOVEMENT),
    'plus': (lambda i: f'+{i}', UNKNOWN),
    'dezimalpunkt': (lambda i: f'{i}.0', UNKNOWN),
    'arabisch-indische-ziffern': (lambda i: i.translate(ARABIC_INDIC), UNKNOWN),
}


# ── die Formulare ───────────────────────────────────────────────────────────────

def _update_post(db, bank, split_ids):
    """Die Maske der Bankbewegung, geöffnet und abgeschickt wie vom Browser – mit geändertem Verwendungszweck und je einer
    Zeile mit diesen IDs (Betrag und Konto sind gefüllt: eine ganz leere Zeile ignoriert der Server)."""
    post_data, _options = open_form(db, bank)
    post_data['text'] = ['Geänderter Verwendungszweck']
    for split_id in split_ids:
        post_data = add_row(post_data, split_id=split_id, split_amount='-50.00', split_coa=str(coa(db, 6815)),
                            split_text='Eingeschleust')
    return post_data


def _new_post(db, split_ids):
    """Eine neue Bankbewegung mit Editor-Zeilen (die Maske für eine neue Buchung hat keinen Editor: von Hand gebaut)."""
    names = ('split_id', 'split_text', 'split_counter_coa', 'split_amount', 'split_coa', 'split_tax_rate',
             'split_tax_amount', 'split_docnr')
    post_data = {'transaction_id': ['0'], 'date': ['2026-08-05'], 'amount': ['-50.00'], 'account': [str(_giro(db))],
                 'currency': ['EUR'], 'recipient': ['Neue Bewegung'], 'text': ['Neuer Verwendungszweck'],
                 **{name: [] for name in names}}
    for split_id in split_ids:
        row = {'split_id': split_id, 'split_text': 'Eingeschleust', 'split_amount': '-50.00',
               'split_coa': str(coa(db, 6815))}
        for name in names:
            post_data[name].append(row.get(name, ''))
    return post_data


def _everything(db):
    return state(db, *STATE_TABLES)


# ── jede eingeschleuste ID wird abgelehnt, nichts wird gespeichert ───────────────────────────────────────────────────

@pytest.mark.parametrize('name', HOSTILE)
def test_a_foreign_id_in_the_editor_is_refused_and_nothing_is_stored(db, name):
    build, _also_new = HOSTILE[name]
    bank = _movement(db)
    split_id, message = build(db, bank)
    post_data = _update_post(db, bank, [split_id])
    before = _everything(db)

    assert handle_add_transaction(db, post_data) == (400, f'Zeile 1: {message}')

    assert _everything(db) == before                      # auch der geänderte Verwendungszweck der Bankzeile nicht
    assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (bank,)) == [('Zuordnungsziel',)]


@pytest.mark.parametrize('name', [name for name, (_build, also_new) in HOSTILE.items() if also_new])
def test_the_same_ids_are_refused_for_a_new_bank_movement_and_no_movement_arises(db, name):
    build, _also_new = HOSTILE[name]
    split_id, message = build(db, None)
    post_data = _new_post(db, [split_id])
    before = _everything(db)

    assert handle_add_transaction(db, post_data) == (400, f'Zeile 1: {message}')

    assert _everything(db) == before                      # keine Bankzeile, kein Satz


def test_the_same_id_twice_is_refused(db):
    bank = _movement(db)
    free = _free_entry(db)
    before = _everything(db)

    assert handle_add_transaction(db, _update_post(db, bank, [str(free), str(free)])) == (400, f'Zeile 2: {TWICE}')

    assert _everything(db) == before
    assert handle_add_transaction(db, _new_post(db, [str(free), str(free)])) == (400, f'Zeile 2: {TWICE}')
    assert _everything(db) == before


def test_the_same_own_id_twice_is_refused_too(db):
    """Zwei Zeilen mit demselben Satz der Bankbewegung: Eine davon überschriebe sonst den Satz mit ihren Feldern."""
    bank = _movement(db)
    post_data, _options = open_form(db, bank)
    submit(db, add_row(post_data, split_amount='-50.00', split_coa=str(coa(db, 6815)), split_text='Eigener Satz'))
    (child,) = [row[0] for row in query(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', (bank,))]
    post_data, _options = open_form(db, bank)
    post_data = add_row(post_data, split_id=child, split_amount='-1.00', split_coa=str(coa(db, 6815)))
    before = _everything(db)

    assert handle_add_transaction(db, post_data) == (400, f'Zeile 2: {TWICE}')

    assert _everything(db) == before


@pytest.mark.parametrize('first, second', [
    pytest.param('{id}', '0{id}', id='mit-fuehrender-null'),
    pytest.param('00{id}', '{id}', id='fuehrende-nullen-zuerst'),
    pytest.param('{id}', ' {id}\xa0', id='mit-leerraum'),
])
def test_two_spellings_of_the_same_id_count_as_one_row_twice(db, first, second):
    """``'5'`` und ``'05'`` sind dieselbe ID (``_form_id``): Zwei Zeilen mit ihnen ordnen die Buchung nicht doppelt zu."""
    bank = _movement(db)
    free = _free_entry(db)
    split_ids = [first.format(id=free), second.format(id=free)]
    before = _everything(db)

    assert handle_add_transaction(db, _update_post(db, bank, split_ids)) == (400, f'Zeile 2: {TWICE}')
    assert _everything(db) == before
    assert handle_add_transaction(db, _new_post(db, split_ids)) == (400, f'Zeile 2: {TWICE}')
    assert _everything(db) == before


@pytest.mark.parametrize('text', ['0', '-5', '1.5', '１２', '99999999999999999999', ' abc ', '1e3', '٣'])
def test_an_id_that_is_no_ascii_integer_in_64_bits_is_unknown(db, text):
    bank = _movement(db)
    before = _everything(db)

    assert handle_add_transaction(db, _update_post(db, bank, [text])) == (400, f'Zeile 1: {UNKNOWN}')

    assert _everything(db) == before


@pytest.mark.parametrize('name', SPELLINGS)
def test_another_spelling_of_a_foreign_id_is_refused_like_the_id_or_as_unknown(db, name):
    """Die ID eines Satzes einer anderen Bewegung, anders geschrieben: Dieselbe ID wird wie ``'5'`` abgelehnt, keine ID als
    „unbekannt“ – nie ordnet eine Schreibweise den fremden Satz zu. Für eine vorhandene wie für eine neue Bankbewegung."""
    spell, message = SPELLINGS[name]
    bank = _movement(db)
    foreign, _message = _child_of_another_movement(db, bank)
    before = _everything(db)

    for post_data in (_update_post(db, bank, [spell(foreign)]), _new_post(db, [spell(foreign)])):
        assert handle_add_transaction(db, post_data) == (400, f'Zeile 1: {message}')

        assert _everything(db) == before                  # der fremde Satz hängt weiter an seiner Bewegung


def test_the_first_refused_row_names_its_number(db):
    """Die Zeilennummer ist die im Formular – hier die dritte (zwei gültige Zeilen davor)."""
    bank = _movement(db)
    first, second = _free_entry(db, document_number='ER-1'), _free_entry(db, document_number='ER-2')
    post_data = _update_post(db, bank, [str(first), str(second), '999999'])
    before = _everything(db)

    assert handle_add_transaction(db, post_data) == (400, f'Zeile 3: {GONE}')

    assert _everything(db) == before                      # auch die beiden gültigen Zeilen davor sind nicht zugeordnet


def test_a_row_without_amount_and_account_is_ignored_with_its_id(db):
    """Eine ganz leere Zeile ignoriert der Server – auch eine ID darin ordnet nichts zu."""
    bank = _movement(db)
    foreign, _message = _child_of_another_movement(db, bank)
    post_data, _options = open_form(db, bank)
    post_data = add_row(post_data, split_id=foreign)               # weder Betrag noch Konto
    before = snapshot(db)

    submit(db, post_data)

    assert differences(before, snapshot(db)) == []
    assert query(db, 'SELECT ParentBooking_ID FROM Bookings WHERE ID = ?', (int(foreign),)) != [(bank,)]


# ── was bleibt erlaubt ──────────────────────────────────────────────────────────────

def test_a_free_entry_beyond_the_listed_candidates_can_still_be_adopted(db):
    """Das Zuordnen-Widget bietet höchstens 50 Kandidaten an; die Prüfung kennt diese Grenze nicht."""
    bank = _movement(db)
    ids = [_free_entry(db, date=f'2026-{1 + n // 28:02d}-{1 + n % 28:02d}', document_number=f'ER-{n}')
           for n in range(55)]
    target = ids[0]                                                # der älteste: nach ``DateBooking DESC`` weit hinten
    assert target not in {row[0] for row in db.get_unlinked_entry_bookings()}
    assert db.adoptable_entry_ids([target]) == {target}
    post_data, _options = open_form(db, bank)

    submit(db, add_row(post_data, split_id=target, split_amount='-50.00', split_coa=str(coa(db, 6815))))

    assert query(db, 'SELECT ParentBooking_ID FROM Bookings WHERE ID = ?', (target,)) == [(bank,)]


def test_the_rows_under_the_bank_movement_stay_valid(db):
    """Die eigenen Sätze der Bewegung dürfen weiter in einer Zeile stehen – sie sind nie „zuzuordnen“."""
    bank = _movement(db, '-100.00')
    for amount, text in (('-60.00', 'Teil A'), ('-40.00', 'Teil B')):
        post_data, _options = open_form(db, bank)
        submit(db, add_row(post_data, split_amount=amount, split_coa=str(coa(db, 6815)), split_text=text))
    before = snapshot(db)

    post_data, _options = open_form(db, bank)
    assert len([i for i in post_data['split_id'] if i]) == 2
    submit(db, post_data)                                          # unverändert zurück

    assert differences(before, snapshot(db)) == []


def test_a_free_entry_is_adopted_with_the_option_of_the_widget(db):
    bank = _movement(db)
    free = _free_entry(db)
    post_data, options = open_form(db, bank)
    option = next(o for o in options if o['value'] == str(free))

    submit(db, adopt_row(post_data, option))

    assert query(db, 'SELECT ParentBooking_ID FROM Bookings WHERE ID = ?', (free,)) == [(bank,)]


# ── adoptable_entry_ids ─────────────────────────────────────────────────────────────

def test_adoptable_entry_ids_names_exactly_the_candidates_of_the_widget(db):
    free = _free_entry(db)
    unavailable = [int(build(db, None)[0]) for build in (
        _a_receivable, _a_resolved_entry, _an_entry_with_its_own_account, _child_of_another_movement,
        _another_bank_movement, _no_such_booking)]
    candidates = {row[0] for row in db.get_unlinked_entry_bookings(limit=500)}
    asked = [free, *unavailable]
    before = _everything(db)

    assert candidates & set(asked) == {free}                       # die Kandidaten des Widgets
    assert db.adoptable_entry_ids(asked) == {free}                 # dieselbe Bedingung, auch jenseits einer Grenze
    assert db.adoptable_entry_ids([]) == set()
    assert db.adoptable_entry_ids([str(free)]) == {free}           # IDs aus dem Formular sind Text
    assert _everything(db) == before                               # rein lesend


def test_a_free_payment_row_is_neither_a_candidate_nor_adoptable(db):
    """Ein Satz mit der Marke ``invoice_payment`` und ohne Bankbewegung (nach einem Eingriff an der App vorbei) ist kein Kandidat
    des Widgets und für den Editor nicht zuordenbar – allein die Marke trennt ihn vom zuordenbaren Satz."""
    booking = _free_entry(db)
    assert booking in {row[0] for row in db.get_unlinked_entry_bookings()}         # Gegenprobe: ohne Marke ein Kandidat
    assert db.adoptable_entry_ids([booking]) == {booking}
    execute(db, 'UPDATE Bookings SET Status = ? WHERE ID = ?', (PAYMENT_MARK, booking))
    before = _everything(db)

    assert booking not in {row[0] for row in db.get_unlinked_entry_bookings(limit=500)}
    assert db.adoptable_entry_ids([booking]) == set()
    assert _everything(db) == before


def _free_entries(db, count):
    """``count`` freie Buchungssätze auf einmal angelegt (``executemany``; je ``insert_booking`` dauerte das zu lange): ihre IDs."""
    with closing(db._get_connection()) as conn:
        conn.executemany(
            "INSERT INTO Bookings (DateBooking, Amount, COA_ID, CounterCOA_ID, BookingType, Text, EntryForm) "
            "VALUES ('2026-07-15', ?, ?, ?, 'entry', ?, 'purpose_first')",
            [(minor('-5.00'), coa(db, 6815), coa(db, 1800), f'Massensatz {n}') for n in range(count)])
        conn.commit()
    return [row[0] for row in query(db, "SELECT ID FROM Bookings WHERE Text LIKE 'Massensatz %' ORDER BY ID")]


def test_adoptable_entry_ids_over_several_chunks_names_exactly_the_adoptable_ones(db):
    """Mehr IDs, als eine Abfrage aufnimmt (``_ID_CHUNK``): Zuordenbare, Nicht-Zuordenbare und Unbekannte liegen über alle
    Stücke verteilt, das Ergebnis sind genau die zuordenbaren."""
    free = _free_entries(db, 2 * _ID_CHUNK + 200)
    unavailable = [int(build(db, None)[0]) for build in (
        _a_receivable, _a_resolved_entry, _an_entry_with_its_own_account, _child_of_another_movement,
        _another_bank_movement, _a_free_payment_row)]
    unknown = [900_000 + n for n in range(4)]
    asked = list(free)
    for position, other in enumerate(unavailable + unknown):
        asked.insert(position * (len(free) // 10), other)
    before = _everything(db)

    assert len(asked) == len(free) + 10
    assert db.adoptable_entry_ids(asked) == set(free)
    assert db.adoptable_entry_ids([str(booking) for booking in asked]) == set(free)     # aus dem Formular: Text
    assert _everything(db) == before


def test_adoptable_entry_ids_copes_with_more_ids_than_sqlite_takes_in_one_query(db):
    """Vierzigtausend IDs, fast alle unbekannt – mehr als die übliche Grenze von 32 766 Variablen je Abfrage: kein Fehler
    („too many SQL variables“), das Ergebnis stimmt."""
    free = [_free_entry(db, document_number=f'ER-{n}') for n in range(3)]
    asked = [free[0], *range(1_000_000, 1_040_000), free[1], free[2]]

    assert db.adoptable_entry_ids(asked) == set(free)


# ── die Zahl der Zeilen ─────────────────────────────────────────────────────────────

TOO_MANY_ROWS = f'Zu viele Buchungssätze in einer Bankbewegung (höchstens {MAX_SPLIT_ROWS}).'


def _with_rows(db, post_data, count, **row):
    """Das Formular mit ``count`` gleichen Zeilen im Editor statt der Zeilen, die dort standen: neue Zeilen über 0,10 € auf das
    Konto 6815, soweit ``row`` nichts anderes nennt."""
    row = {'split_amount': '-0.10', 'split_coa': str(coa(db, 6815)), **row}
    result = dict(post_data)
    for name in row_fields(post_data):
        result[name] = [row.get(name, '')] * count
    return result


@pytest.mark.parametrize('count', [MAX_SPLIT_ROWS + 1, 40_000], ids=['eine-zu-viel', 'vierzigtausend'])
def test_more_rows_than_the_limit_are_refused_and_nothing_is_stored(db, count):
    """Der Editor plant und schreibt unter der Schreibsperre: Eine Anfrage mit Tausenden Zeilen hielte sie fest. Abgelehnt wird,
    bevor eine Zeile gelesen wird – für eine vorhandene wie für eine neue Bankbewegung."""
    bank = _movement(db)
    before = _everything(db)

    assert handle_add_transaction(db, _with_rows(db, open_form(db, bank)[0], count)) == (400, TOO_MANY_ROWS)
    assert _everything(db) == before
    assert handle_add_transaction(db, _with_rows(db, _new_post(db, []), count)) == (400, TOO_MANY_ROWS)
    assert _everything(db) == before


def test_exactly_the_limit_of_rows_is_saved(db):
    bank = _movement(db)
    post_data = _with_rows(db, open_form(db, bank)[0], MAX_SPLIT_ROWS)

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    assert query(db, 'SELECT COUNT(*) FROM Bookings WHERE ParentBooking_ID = ?', (bank,)) == [(MAX_SPLIT_ROWS,)]


def test_empty_rows_do_not_count_towards_the_limit(db):
    """Eine Zeile ohne Betrag und Konto ignoriert der Server (so ist die Vorlagenzeile der Maske): Sie zählt nicht mit."""
    bank = _movement(db)
    post_data = _with_rows(db, open_form(db, bank)[0], MAX_SPLIT_ROWS + 100, split_amount='', split_coa='')
    for index in (0, 1):                                           # zwei echte Zeilen unter den leeren
        post_data['split_amount'][index], post_data['split_coa'][index] = '-5.00', str(coa(db, 6815))

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    assert query(db, 'SELECT COUNT(*) FROM Bookings WHERE ParentBooking_ID = ?', (bank,)) == [(2,)]


# ── über den Server ─────────────────────────────────────────────────────────────────

def test_over_http_the_editor_answers_400_with_the_text_and_stores_nothing(server_port, auto_login):
    db = auto_login.db
    bank_account(db, 'Wächter-Bank', 1800)
    bank = _movement(db)
    post_data = _update_post(db, bank, ['999999'])
    before = _everything(db)

    fields = [(name, value) for name, values in post_data.items() for value in values] + [('csrf', auto_login.csrf)]
    resp = post(server_port, '/transactions/add', form=fields)

    assert resp.status == 400 and resp.headers['Content-Type'].startswith('text/plain')
    assert resp.text == f'Zeile 1: {GONE}'
    assert _everything(db) == before
    assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (bank,)) == [('Zuordnungsziel',)]
