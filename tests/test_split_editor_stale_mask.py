# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine veraltete Buchungsmaske entfernt keine Buchungssätze, die sie nie gesehen hat.

Anlass: Die Maske einer Bankbewegung mit Buchungssatz-Editor ist geöffnet (Fenster 1). In einem zweiten Fenster wird der Bewegung eine
Rechnungszahlung zugeordnet – oder ein Import oder der Abgleich hängt einen Satz an. Speicherte Fenster 1 danach, galt jeder Satz, den es
nicht mitschickt, als vom Nutzer entfernt (``Database._split_rows_dropped``): Die Zahlung wurde samt Zuordnung gelöscht.

Entschieden ist: **ablehnen, nichts speichern.** Die Maske nennt mit dem versteckten Feld ``split_loaded`` die Sätze, die sie beim Öffnen
gezeigt hat (``server.pages_transactions``); steht beim Speichern ein Satz unter der Bewegung, den sie nicht kennt, lehnt
``handlers._plan_split_rows`` mit ``SPLIT_ROWS_CHANGED`` ab – unter der Schreibsperre, vor dem ersten Schreiben (400, der Klartext, nichts
gespeichert, nichts im Replay-Log). Was der Nutzer selbst entfernt, bleibt wie bisher (gelöscht bzw. abgehängt), ein Doppik-Spiegel zwischen
eigenen Konten (im Editor unsichtbar) löst nie eine Ablehnung aus, „vorhandene Buchung zuordnen“ geht weiter, und ein Formular ohne das Feld
verhält sich wie bisher. Ein ungültiges Feld ist eine ungültige Anfrage (400), auch bei einer neuen Bankbewegung.

Die Formulare stammen aus dem HTML der echten Maske (``tests/entry_form_roundtrip.py``). Alle Namen und Werte sind erfunden.
"""
from decimal import Decimal

import pytest

from db import Database
from server.handlers import handle_add_transaction, link_booking_to_invoice_capped
from server.pages_transactions import PageTransactions
from tests.document_helpers import state
from tests.entry_form_helpers import bank_account, coa, execute, query
from tests.entry_form_roundtrip import adopt_row, differences, drop_row, open_form, snapshot, submit

# Die Sätze stehen hier ausgeschrieben, nicht aus ``handlers`` gelesen: Ändert sich einer, schlägt der Test an.
CHANGED = ('Die Buchungssätze dieser Bankbewegung wurden inzwischen geändert (zum Beispiel in einem zweiten Fenster). '
           'Bitte die Buchung neu öffnen – nichts wurde gespeichert.')
INVALID = 'Ungültige Anfrage.'

STATE_TABLES = ('Bookings', 'InvoicePayments', 'Invoices')


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Fenster-Bank', 1800)
    return tmp_db


@pytest.fixture
def logged(monkeypatch):
    """Die Beschreibungen der Einträge, die ins Replay-Log gehen (``Database._log_sql``), in ihrer Reihenfolge."""
    entries = []
    monkeypatch.setattr(Database, '_log_sql', lambda self, sql, params, description: entries.append(description))
    return entries


def _giro(db):
    return next(a[0] for a in db.fetch_accounts() if a[1] == 'Fenster-Bank')


def _movement(db, amount='-100.00', date='2026-08-01', text='Zuordnungsziel'):
    return db.insert_booking(date, Decimal(amount), account_id=_giro(db), booking_type='bank',
                             recipient_client='Bank-Empfänger', text=text)


def _child(db, bank, amount, text, **over):
    values = dict(coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800), booking_type='entry', parent_booking_id=bank,
                  text=text, document_number=f'ER-{text}')
    values.update(over)
    return db.insert_booking('2026-08-01', Decimal(amount), **values)


def _mirror(db, bank):
    """Ein Spiegel zwischen zwei eigenen Konten (Bank und Kasse): Der Editor zeigt ihn nicht."""
    return db.insert_booking('2026-08-01', Decimal('-100.00'), coa_id=coa(db, 1800), counter_coa_id=coa(db, 1600),
                             booking_type='entry', parent_booking_id=bank, text='Spiegel')


def _free_entry(db, amount='-50.00'):
    """Ein Buchungssatz, wie das Zuordnen-Widget ihn anbietet: ohne Bankbewegung, ohne eigenes Konto."""
    return db.insert_booking('2026-07-15', Decimal(amount), coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800),
                             booking_type='entry', text='Freier Satz', document_number='ER-FREI')


def _invoice(db):
    return db.insert_invoice({
        'invoice_number': 'RE-FENSTER', 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19,
        'sum_net': 100.00, 'tax_amount': 19.00, 'sum_gross': 119.00, 'amount_due': 119.00,
        'status': 'sent'})


def _unpaid(db):
    """Eine versendete Rechnung über 119,00 mit ihrer Forderung und eine Bankbewegung über 119,00 – noch nicht zugeordnet."""
    invoice = _invoice(db)
    db.ensure_receivable_booking(invoice)
    bank = db.insert_booking('2026-08-01', Decimal('119.00'), account_id=_giro(db), booking_type='bank',
                             recipient_client='Käufer AG', text='Zuordnungsziel')
    return invoice, bank


def _everything(db):
    return state(db, *STATE_TABLES)


def _children(db, bank):
    return [row[0] for row in query(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', (bank,))]


def _new_post(db):
    """Eine neue Bankbewegung mit einer Zeile im Editor (von Hand gebaut: Die Maske für eine neue Buchung hat keinen Editor)."""
    return {'transaction_id': ['0'], 'date': ['2026-08-05'], 'amount': ['-60.00'], 'account': [str(_giro(db))],
            'currency': ['EUR'], 'recipient': ['Neue Bewegung'], 'text': ['Neuer Verwendungszweck'],
            'split_id': [''], 'split_text': ['Teil A'], 'split_counter_coa': [''], 'split_amount': ['-60.00'],
            'split_coa': [str(coa(db, 6815))], 'split_tax_rate': [''], 'split_tax_amount': [''], 'split_docnr': ['']}


# ── der Fehlfall: eine Zahlung, die ein zweites Fenster zugeordnet hat ──────────────────────────────────

def _stale_form_after_an_assignment(db):
    """Fenster 1 öffnet die Maske der Bewegung, Fenster 2 ordnet ihr die Rechnungszahlung zu; Fenster 1 hat danach einen Text geändert.
    Liefert (Rechnung, Bewegung, das Formular von Fenster 1)."""
    invoice, bank = _unpaid(db)
    stale, _options = open_form(db, bank)
    assert link_booking_to_invoice_capped(db, invoice, bank) == (True, None)
    stale['text'] = ['Im Fenster 1 geändert']
    return invoice, bank, stale


def test_a_stale_mask_does_not_delete_a_payment_that_a_second_window_assigned(db, logged):
    invoice, bank, stale = _stale_form_after_an_assignment(db)
    assert stale['split_loaded'] == ['']                                    # die Maske hat keine Zeile gezeigt
    before = _everything(db)
    assert query(db, 'SELECT COUNT(*) FROM InvoicePayments') == [(1,)] and len(_children(db, bank)) == 2
    logged.clear()

    assert handle_add_transaction(db, stale) == (400, CHANGED)

    assert _everything(db) == before                                        # Zuordnung, markierte Sätze, Rechnung, Bankzeile: wie vorher
    assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (bank,)) == [('Zuordnungsziel',)]   # auch der geänderte Text ist nicht gespeichert
    assert logged == []                                                     # und nichts davon im Replay-Log


def test_the_same_stale_form_without_the_field_still_deletes_the_payment(db):
    """Der Beleg dafür, dass der Test oben den Fehler trifft: Ohne das Feld (ein älterer Aufrufer) gilt alles wie bisher – die zwei Sätze der
    Zuordnung stehen nicht im Formular, gelten als entfernt, und mit ihnen geht die Zuordnung."""
    invoice, bank, stale = _stale_form_after_an_assignment(db)
    del stale['split_loaded']

    assert handle_add_transaction(db, stale) == (303, '/transactions')

    assert query(db, 'SELECT COUNT(*) FROM InvoicePayments') == [(0,)]
    assert _children(db, bank) == []
    assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (bank,)) == [('Im Fenster 1 geändert',)]


def test_a_mask_opened_after_the_assignment_saves_and_changes_nothing(db):
    invoice, bank, _stale = _stale_form_after_an_assignment(db)
    before, before_rows = _everything(db), snapshot(db)

    fresh, _options = open_form(db, bank)
    assert fresh['split_loaded'] == [','.join(str(child) for child in _children(db, bank))]     # die zwei markierten Sätze
    submit(db, fresh)

    assert _everything(db) == before
    assert differences(before_rows, snapshot(db)) == []


# ── ein anderer Satz, der in der Zwischenzeit unter die Bewegung kam ─────────────────────────────────────

@pytest.mark.parametrize('how', ['angehaengt', 'im-zweiten-Fenster-zugeordnet'])
def test_a_row_attached_in_the_meantime_makes_the_mask_stale(db, logged, how):
    """Der Import oder der Abgleich hängt einen Satz an – oder ein zweites Fenster ordnet eine vorhandene Buchung zu. Das veraltete Formular,
    das dazu eine Zeile gestrichen und einen Text geändert hat, wird ganz abgelehnt."""
    bank = _movement(db)
    first, second = _child(db, bank, '-60.00', 'Teil A'), _child(db, bank, '-40.00', 'Teil B')
    stale, _options = open_form(db, bank)
    if how == 'angehaengt':
        _child(db, bank, '-5.00', 'Später')
    else:
        free = _free_entry(db)
        other, options = open_form(db, bank)
        submit(db, adopt_row(other, next(o for o in options if o['value'] == str(free))))
    assert len(_children(db, bank)) == 3
    stale['text'] = ['Im Fenster 1 geändert']
    stale = drop_row(stale, second)
    before = _everything(db)
    logged.clear()

    assert handle_add_transaction(db, stale) == (400, CHANGED)

    assert _everything(db) == before and first in _children(db, bank) and second in _children(db, bank)
    assert logged == []


def test_a_stale_mask_is_refused_even_when_it_adds_a_new_row(db):
    bank = _movement(db)
    _child(db, bank, '-60.00', 'Teil A')
    stale, _options = open_form(db, bank)
    _child(db, bank, '-5.00', 'Später')
    stale['split_amount'][-1], stale['split_coa'][-1] = '-1.00', str(coa(db, 6815))        # die Vorlagenzeile wird zu einer neuen Zeile
    before = _everything(db)

    assert handle_add_transaction(db, stale) == (400, CHANGED)

    assert _everything(db) == before


# ── was bleibt, wie es war ──────────────────────────────────────────────────────────────────

def test_a_row_the_mask_showed_is_still_deleted_or_detached_when_the_user_removes_it(db):
    bank = _movement(db)
    kept, by_hand, imported = (_child(db, bank, '-10.00', 'Bleibt'), _child(db, bank, '-20.00', 'Handsatz'),
                               _child(db, bank, '-30.00', 'Importsatz'))
    execute(db, "UPDATE Bookings SET SourceGroup = 'FENSTER-1' WHERE ID = ?", (imported,))
    post_data, _options = open_form(db, bank)
    assert post_data['split_loaded'] == [f'{kept},{by_hand},{imported}']
    post_data = drop_row(drop_row(post_data, by_hand), imported)

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    assert query(db, 'SELECT 1 FROM Bookings WHERE ID = ?', (by_hand,)) == []                          # gelöscht
    assert query(db, 'SELECT ParentBooking_ID FROM Bookings WHERE ID = ?', (imported,)) == [(None,)]   # abgehängt
    assert _children(db, bank) == [kept]


def test_a_mirror_between_own_accounts_never_makes_the_mask_stale(db):
    """Der Spiegel steht nicht im Editor und nicht in ``split_loaded`` – er war schon da oder kam später, abgelehnt wird deshalb nie."""
    bank = _movement(db)
    first = _child(db, bank, '-60.00', 'Teil A')
    early = _mirror(db, bank)
    post_data, _options = open_form(db, bank)
    assert post_data['split_loaded'] == [str(first)]
    late = _mirror(db, bank)
    post_data['split_text'][0] = 'Geändert'

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    assert query(db, 'SELECT Text FROM Bookings WHERE ID = ?', (first,)) == [('Geändert',)]
    assert _children(db, bank) == [first, early, late]                      # beide Spiegel bleiben


def test_assigning_an_existing_booking_in_the_same_save_still_works(db):
    bank = _movement(db)
    first = _child(db, bank, '-60.00', 'Teil A')
    free = _free_entry(db)
    post_data, options = open_form(db, bank)

    submit(db, adopt_row(post_data, next(o for o in options if o['value'] == str(free))))

    assert _children(db, bank) == [first, free]


def test_a_form_without_the_field_behaves_as_before(db):
    """Ältere Aufrufer und Tests schicken das Feld nicht: Es wird nichts verglichen."""
    bank = _movement(db)
    _child(db, bank, '-60.00', 'Teil A')
    post_data, _options = open_form(db, bank)
    del post_data['split_loaded']
    before, before_rows = _everything(db), snapshot(db)

    submit(db, post_data)

    assert _everything(db) == before and differences(before_rows, snapshot(db)) == []


# ── ein ungültiges Feld ────────────────────────────────────────────────────────────────────────────

INVALID_VALUES = [
    pytest.param('1,x', id='kein-Zahl-dabei'),
    pytest.param('x', id='Text'),
    pytest.param('01', id='fuehrende-Null'),
    pytest.param('0', id='null'),
    pytest.param('-1', id='negativ'),
    pytest.param('+1', id='mit-Plus'),
    pytest.param(' 1', id='Leerraum-davor'),
    pytest.param('1 ', id='Leerraum-dahinter'),
    pytest.param('1 ,2', id='Leerraum-am-Komma'),
    pytest.param('1,,2', id='leerer-Eintrag'),
    pytest.param(',1', id='Komma-vorn'),
    pytest.param('1,', id='Komma-hinten'),
    pytest.param('1.0', id='Dezimalpunkt'),
    pytest.param(chr(0x661), id='arabisch-indische-Ziffer'),
    pytest.param(str(2 ** 63), id='nicht-in-64-Bit'),
    pytest.param('9' * 20, id='zwanzig-Ziffern'),
    pytest.param(','.join(str(n) for n in range(1, 5002)), id='5001-IDs'),
    pytest.param('1' * 100_001, id='mehr-als-100000-Zeichen'),
]


@pytest.mark.parametrize('value', INVALID_VALUES)
def test_an_invalid_split_loaded_is_a_bad_request_and_nothing_is_stored(db, logged, value):
    bank = _movement(db)
    _child(db, bank, '-60.00', 'Teil A')
    post_data, _options = open_form(db, bank)
    post_data['split_loaded'] = [value]
    post_data['text'] = ['Im Fenster geändert']
    before = _everything(db)
    logged.clear()

    assert handle_add_transaction(db, post_data) == (400, INVALID)

    assert _everything(db) == before and logged == []


@pytest.mark.parametrize('value', INVALID_VALUES)
def test_an_invalid_split_loaded_is_refused_for_a_new_bank_movement_too(db, value):
    post_data = _new_post(db)
    post_data['split_loaded'] = [value]
    before = _everything(db)

    assert handle_add_transaction(db, post_data) == (400, INVALID)

    assert _everything(db) == before                                        # auch keine Bankbewegung entstanden


@pytest.mark.parametrize('values', [['1', '1'], ['', ''], ['', '1']], ids=['doppelt', 'zweimal-leer', 'leer-und-eine-ID'])
def test_the_field_must_stand_exactly_once(db, values):
    bank = _movement(db)
    _child(db, bank, '-60.00', 'Teil A')
    post_data, _options = open_form(db, bank)
    post_data['split_loaded'] = values
    before = _everything(db)

    assert handle_add_transaction(db, post_data) == (400, INVALID)

    assert _everything(db) == before


@pytest.mark.parametrize('value', ['', '1', '1,2', '7,3,5', str(2 ** 63 - 1), ','.join(str(n) for n in range(1, 5001))],
                         ids=['leer', 'eine-ID', 'zwei-IDs', 'unsortiert', 'groesste-ID', '5000-IDs'])
def test_a_valid_split_loaded_is_accepted_for_a_new_bank_movement_and_changes_nothing_there(db, value):
    """Für eine Bewegung, die es noch nicht gibt, hat das Feld keine Wirkung – nur die Schreibweise zählt."""
    post_data = _new_post(db)
    post_data['split_loaded'] = [value]

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    (bank,) = [row[0] for row in query(db, "SELECT ID FROM Bookings WHERE BookingType = 'bank'")]
    assert query(db, 'SELECT Text FROM Bookings WHERE ParentBooking_ID = ?', (bank,)) == [('Teil A',)]


def test_an_id_in_split_loaded_that_hangs_under_no_movement_changes_nothing(db):
    """Das Feld sagt nur, was die Maske kannte: Eine ID darin, die nicht (mehr) unter der Bewegung hängt, ändert nichts."""
    bank = _movement(db)
    first = _child(db, bank, '-60.00', 'Teil A')
    post_data, _options = open_form(db, bank)
    post_data['split_loaded'] = [f'{first},999999']

    assert handle_add_transaction(db, post_data) == (303, '/transactions')

    assert _children(db, bank) == [first]


# ── die Seite ───────────────────────────────────────────────────────────────────────────────────────

def test_the_mask_names_exactly_the_rows_it_shows(db):
    bank = _movement(db)
    first = _child(db, bank, '-60.00', 'Teil A')
    _mirror(db, bank)                                                       # im Editor unsichtbar – und nicht in ``split_loaded``
    second = _child(db, bank, '-40.00', 'Teil B')

    page = PageTransactions(db, edit_transaction_id=bank)
    post_data, _options = open_form(db, bank)                              # nur Felder innerhalb des Formulars

    assert page.count('name="split_loaded"') == 1
    assert '<input type="hidden" name="split_loaded" value="' in page
    shown = [row_id for row_id in post_data['split_id'] if row_id]          # ohne die leere Vorlagenzeile
    assert shown == [str(first), str(second)]
    assert post_data['split_loaded'] == [','.join(shown)]


def test_the_mask_of_a_movement_without_rows_has_the_empty_value(db):
    bank = _movement(db)

    post_data, _options = open_form(db, bank)

    assert post_data['split_loaded'] == ['']
    assert '<input type="hidden" name="split_loaded" value="">' in PageTransactions(db, edit_transaction_id=bank)


def test_only_the_mask_with_an_editor_carries_the_field(db):
    """Der Editor gehört der Bankbewegung: Eine neue Buchung und ein Buchungssatz haben ihn – und das Feld – nicht."""
    entry = _free_entry(db)

    assert 'split_loaded' not in PageTransactions(db)
    assert 'split_loaded' not in PageTransactions(db, edit_transaction_id=entry)
