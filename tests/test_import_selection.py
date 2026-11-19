# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kontoauszug-Import: einzelne Umsätze auswählen (2.5.0) – nur auf einem gemischt genutzten Bankkonto.

* Die Vorschau nennt je Umsatz ``index`` (Position in ``server.import_preview.file_transactions``, dieselbe Liste, die der
  Import durchläuft) und ``importable`` (weder Duplikat noch unvollständig) und je Beleg ``selectable``: Konto gefunden UND
  ``Database.account_allows_selection`` (``Accounts.BusinessOnly`` genau 0, keine Kasse; jeder andere Wert gilt als reines
  Geschäftskonto).
* ``/confirm_transactions`` nimmt optional ``selection`` (JSON-Objekt ``{"<file_index>": [<index>, …]}``): ein Beleg mit Eintrag
  legt nur die genannten Umsätze an, einer ohne wird ganz importiert. Die Duplikat-Zählung bleibt exakt wie sonst; geprüft wird
  streng und vor dem ersten Schreiben (ungültig → 400 „Ungültige Auswahl“, Konto ohne Auswahl → 400 „Für ein reines
  Geschäftskonto …“, nichts ist importiert).
* Die Seite zeigt Kästchen, Suchfeld, „Alle“/„Keine“ und den Zähler nur bei ``selectable``; Datenwerte stehen maskiert.
* Das Bankkonto-Formular führt das Kennzeichen „Reines Geschäftskonto“ (Standard angehakt) mit einem versteckten Feld
  ``business_only_field``; ohne dieses Feld bleibt der gespeicherte Wert.

Namen, Beträge und IBANs sind erfundene Testwerte.
"""
import json
import re
import shutil
import subprocess
from contextlib import closing

import pytest

from server import handlers
from server.import_preview import build_import_preview, file_transactions, match_account
from server.pages_masterdata import BUSINESS_ONLY_HINT, PageBankAccounts
from tests.dom_helpers import Dom
from tests.http_client import own_origin, post, send
from tests.test_confirm_import import (IBAN_A, IBAN_B, _bank_bookings, _txn,  # noqa: F401
                                       db_two_accounts, pending_factory)
from tests.test_import_upload_http import (NO_NODE, Tree, _buttons, _camt, _multipart, _node, _preview_source, _text,
                                           _walk)

NODE = shutil.which('node')
IBAN_CASH = 'DE02200505501015871393'
INVALID = {'ok': False, 'error': 'Ungültige Auswahl'}
MIXED_ONLY = {'ok': False, 'error': 'Für ein reines Geschäftskonto wird der Auszug vollständig importiert. '
                                    'Bitte die Vorschau verwerfen und die Datei erneut hochladen.'}

ROWS = [_txn('2025-01-10', -10.0, 'Netflix'), _txn('2025-01-11', -20.0, 'Miete'), _txn('2025-01-12', -30.0, 'Privat Kino'),
        _txn('2025-01-13', -40.0, 'Strom'), _txn('2025-01-14', 50.0, 'Erstattung')]


def _account_id(db, iban):
    return match_account(db.fetch_accounts(), iban)[0]


def _sql(db, sql, params=()):
    with closing(db._get_connection()) as con:
        con.execute(sql, params)
        con.commit()


def _set_mixed(db, iban, mixed=True):
    """Das Kennzeichen eines Kontos über die DB-Schicht setzen (wie das Formular): ``mixed`` = gemischt genutzt."""
    account = db.get_account_by_id(_account_id(db, iban))
    db.update_account(account[0], account[1], account[2], account[3], account[4], account[5], account[7],
                      business_only=not mixed)


@pytest.fixture
def books(db_two_accounts):
    """Konto A ist gemischt genutzt (Auswahl möglich), Konto B ein reines Geschäftskonto (der Standard)."""
    _set_mixed(db_two_accounts, IBAN_A)
    return db_two_accounts


def _file(name, transactions, iban=IBAN_A):
    return {'filename': name, 'bank_code': 'CAMT', 'iban': iban, 'document_date': '2025-01-31',
            'transactions': transactions}


def _texts(db):
    return [row[2] for row in _bank_bookings(db)]


def _confirm(db, import_id, selection=None, file_index=None, **extra):
    form = {'import_id': [import_id], **extra}
    if selection is not None:
        form['selection'] = [selection if isinstance(selection, str) else json.dumps(selection)]
    if file_index is not None:
        form['file_index'] = [str(file_index)]
    status, body = handlers.handle_confirm_import(db, form)
    return status, json.loads(body)


# ══ Vorschau ═════════════════════════════════════════════════════════════════════════════════════════════════════

def test_the_preview_carries_index_importability_and_selectability(books):
    acc_a = _account_id(books, IBAN_A)
    books.insert_booking(date_booking='2025-01-10', amount=-10.0, account_id=acc_a, foreign_bank_account='DE00FOREIGN',
                         recipient_client='Partner', text='Netflix', booking_type='bank')
    incomplete = _txn(None, 5.0, 'ohne Datum')
    files = [_file('a.xml', [_txn('2025-01-10', -10.0, 'Netflix'), _txn('2025-01-11', -20.0, 'Miete'), incomplete,
                             _txn('2025-01-13', -40.0, 'Strom')]),
             _file('b.xml', [_txn('2025-01-11', -5.0, 'Neu')], iban=IBAN_B),
             _file('c.xml', [_txn('2025-01-11', -5.0, 'Fremd')], iban='DE00000000001000000001')]
    first, second, third = build_import_preview(books, {'files': files})['files']

    assert [t['index'] for t in first['transactions']] == [0, 1, 2, 3]
    assert [t['importable'] for t in first['transactions']] == [False, True, False, True]    # Duplikat, neu, unvollständig, neu
    assert [t['dup'] for t in first['transactions']] == [True, False, False, False]
    assert first['new_count'] == sum(t['importable'] for t in first['transactions']) == 2
    assert (first['selectable'], second['selectable'], third['selectable']) == (True, False, False)
    assert third['status'] == 'error' and second['transactions'][0]['index'] == 0


def test_an_empty_or_missing_transaction_list_is_an_empty_list_in_both_places(books):
    assert file_transactions({}) == [] and file_transactions({'transactions': None}) == []
    assert build_import_preview(books, {'files': [_file('a.xml', None)]})['files'][0]['transactions'] == []


@pytest.mark.parametrize('stored, expected', [
    pytest.param(0, True, id='0-gemischt'), pytest.param(1, False, id='1-rein'),
    pytest.param(2, False, id='2-Unsinn'), pytest.param(-1, False, id='minus-1'), pytest.param(0.5, False, id='Kommazahl'),
    pytest.param('x', False, id='Text'), pytest.param('', False, id='leerer-Text'), pytest.param(b'\x00', False, id='Blob'),
])
def test_only_a_stored_zero_means_mixed_use_everything_else_is_a_business_account(db_two_accounts, stored, expected):
    """Per SQL gesetzter Unsinn gilt als reines Geschäftskonto: im Zweifel wird der Auszug vollständig importiert."""
    account = _account_id(db_two_accounts, IBAN_A)
    _sql(db_two_accounts, 'UPDATE Accounts SET BusinessOnly = ? WHERE ID = ?', (stored, account))
    assert db_two_accounts.account_allows_selection(account) is expected
    (card,) = build_import_preview(db_two_accounts, {'files': [_file('a.xml', ROWS[:2])]})['files']
    assert card['selectable'] is expected


def test_a_cash_account_never_allows_a_selection_and_neither_does_an_unknown_one(db_two_accounts):
    db = db_two_accounts
    db.insert_account('Zweite Kasse', 'Owner', IBAN_CASH, '', '', is_cash=1, skr_account=1610, business_only=False)
    cash = _account_id(db, IBAN_CASH)
    assert db.get_account_by_id(cash)[8] == 0 and db.get_account_by_id(cash)[6] == 1      # „gemischt“ gekennzeichnet, aber Kasse
    assert db.account_allows_selection(cash) is False
    assert db.account_allows_selection(999_999) is False and db.account_allows_selection(None) is False
    (card,) = build_import_preview(db, {'files': [_file('a.xml', ROWS[:2], iban=IBAN_CASH)]})['files']
    assert card['selectable'] is False and card['account_id'] == cash


# ══ Auswahl beim Import ══════════════════════════════════════════════════════════════════════════════════════════

def test_only_the_chosen_transactions_are_booked(books, pending_factory):
    import_id = pending_factory([_file('a.xml', ROWS)])
    status, answer = _confirm(books, import_id, {'0': [1, 3]})
    (result,) = answer['results']
    assert status == 200 and answer['ok'] and answer['all_done'] is True
    assert (result['inserted'], result['skipped'], result['skipped_invalid'], result['skipped_unselected']) == (2, 0, 0, 3)
    assert result['account_found'] is True and result['error'] is None
    assert sorted(_texts(books)) == ['Miete', 'Strom']


def test_without_a_selection_everything_is_booked_as_before(books, pending_factory):
    status, answer = _confirm(books, pending_factory([_file('a.xml', ROWS)]))
    assert status == 200 and answer['results'][0]['inserted'] == 5 and answer['results'][0]['skipped_unselected'] == 0
    assert len(_texts(books)) == 5


@pytest.mark.parametrize('selection', ['', '{}'], ids=['leer', 'leeres-Objekt'])
def test_an_empty_selection_field_means_the_whole_statement(books, pending_factory, selection):
    status, answer = _confirm(books, pending_factory([_file('a.xml', ROWS)]), selection)
    assert status == 200 and answer['results'][0]['inserted'] == 5 and len(_texts(books)) == 5


def test_an_empty_list_for_a_statement_books_nothing_and_still_marks_it_imported(books, pending_factory):
    import_id = pending_factory([_file('a.xml', ROWS)])
    status, answer = _confirm(books, import_id, {'0': []})
    assert status == 200 and answer['results'][0]['inserted'] == 0 and answer['results'][0]['skipped_unselected'] == 5
    assert _bank_bookings(books) == [] and answer['all_done'] is True
    assert _confirm(books, import_id)[0] == 404                                       # verworfen, die Datei des Imports ist weg


def test_the_chosen_indices_are_the_positions_the_preview_names(books, pending_factory):
    """Vorschau und Import lesen dieselbe Liste in derselben Reihenfolge: Wer die ``index`` der Vorschau zurückschickt,
    bekommt genau diese Umsätze."""
    files = [_file('a.xml', ROWS)]
    preview = build_import_preview(books, {'files': files})['files'][0]
    wanted = [t['index'] for t in preview['transactions'] if t['reference'] in ('Miete', 'Strom', 'Erstattung')]
    status, answer = _confirm(books, pending_factory(files), {'0': wanted})
    assert status == 200 and answer['results'][0]['inserted'] == 3
    assert sorted(_texts(books)) == ['Erstattung', 'Miete', 'Strom']


def test_a_statement_without_an_entry_is_booked_whole_next_to_one_with_an_entry(books, pending_factory):
    _set_mixed(books, IBAN_B)
    import_id = pending_factory([_file('a.xml', ROWS), _file('b.xml', [_txn('2025-02-01', -1.0, 'B-1'),
                                                                        _txn('2025-02-02', -2.0, 'B-2')], iban=IBAN_B)])
    status, answer = _confirm(books, import_id, {'0': [0]})
    first, second = answer['results']
    assert status == 200 and answer['all_done'] is True
    assert (first['inserted'], first['skipped_unselected']) == (1, 4) and (second['inserted'], second['skipped_unselected']) == (2, 0)
    assert sorted(_texts(books)) == ['B-1', 'B-2', 'Netflix']


def test_a_single_statement_import_with_a_selection_leaves_the_others_open(books, pending_factory):
    _set_mixed(books, IBAN_B)
    import_id = pending_factory([_file('a.xml', ROWS), _file('b.xml', [_txn('2025-02-01', -1.0, 'B-1')], iban=IBAN_B)])
    status, answer = _confirm(books, import_id, {'0': [2], '1': [0]}, file_index=0)
    assert status == 200 and answer['all_done'] is False and [r['file_index'] for r in answer['results']] == [0]
    assert _texts(books) == ['Privat Kino']
    status, answer = _confirm(books, import_id, {'1': [0]}, file_index=1)
    assert answer['all_done'] is True and sorted(_texts(books)) == ['B-1', 'Privat Kino']


@pytest.mark.parametrize('selection, inserted, skipped, unselected', [
    pytest.param(None, 1, 1, 0, id='ohne-Auswahl'),
    pytest.param({'0': [0, 1]}, 1, 1, 0, id='beide-gewaehlt'),
    pytest.param({'0': [1]}, 1, 1, 0, id='nur-der-neue'),
    pytest.param({'0': [0]}, 0, 1, 1, id='nur-das-Duplikat-ein-gewaehltes-Duplikat-wird-nie-doppelt-angelegt'),
    pytest.param({'0': []}, 0, 1, 1, id='keiner'),
])
def test_the_duplicate_count_is_the_same_with_and_without_a_selection(books, pending_factory, selection, inserted, skipped,
                                                                      unselected):
    """Der Bestand hat schon einen von zwei gleichen Umsätzen: Der erste der Datei ist das Duplikat, der zweite neu – ob etwas
    gewählt ist oder nicht. Die Zählung rückt für jede Zeile vor, gewählt oder nicht."""
    books.insert_booking(date_booking='2025-01-10', amount=-10.0, account_id=_account_id(books, IBAN_A),
                         foreign_bank_account='DE00FOREIGN', recipient_client='Partner', text='Netflix', booking_type='bank')
    twice = [_txn('2025-01-10', -10.0, 'Netflix'), _txn('2025-01-10', -10.0, 'Netflix')]
    preview = build_import_preview(books, {'files': [_file('a.xml', twice)]})['files'][0]
    assert [t['dup'] for t in preview['transactions']] == [True, False]
    status, answer = _confirm(books, pending_factory([_file('a.xml', twice)]), selection)
    (result,) = answer['results']
    assert status == 200
    assert (result['inserted'], result['skipped'], result['skipped_unselected']) == (inserted, skipped, unselected)
    assert len(_bank_bookings(books)) == 1 + inserted


def test_an_unfinished_row_is_counted_as_invalid_whether_chosen_or_not(books, pending_factory):
    rows = [_txn(None, 5.0, 'ohne Datum'), _txn('2025-01-11', -20.0, 'Miete'), _txn('2025-01-12', None, 'ohne Betrag')]
    for selection, expected in (({'0': [0, 1, 2]}, (1, 2, 0)), ({'0': [1]}, (1, 2, 0)), ({'0': []}, (0, 2, 1))):
        db_state = len(_bank_bookings(books))
        status, answer = _confirm(books, pending_factory([_file('a.xml', rows)]), selection)
        (result,) = answer['results']
        assert status == 200
        assert (result['inserted'], result['skipped_invalid'], result['skipped_unselected']) == expected, selection
        assert len(_bank_bookings(books)) == db_state + expected[0]
        _sql(books, "DELETE FROM Bookings WHERE BookingType = 'bank'")


def test_the_text_backfill_for_duplicates_is_unchanged_by_a_selection(books, pending_factory):
    acc_a = _account_id(books, IBAN_A)
    booking = books.insert_booking(date_booking='2025-01-10', amount=-8.22, account_id=acc_a,
                                   foreign_bank_account='DE00FOREIGN', recipient_client='Shop', text='Bestellung ABC',
                                   booking_type='bank')
    full_text = 'Bestellung ABC123XYZ Teil 2'
    status, answer = _confirm(books, pending_factory([_file('a.xml', [_txn('2025-01-10', -8.22, full_text)])]), {'0': []})
    (result,) = answer['results']
    assert status == 200 and (result['inserted'], result['skipped'], result['text_updated']) == (0, 1, 1)
    assert books.get_booking_by_id(booking)[15] == full_text


# ══ Ungültige Auswahl: 400, nichts geschrieben ═══════════════════════════════════════════════════════════════════

BAD_SELECTIONS = {
    'kein-JSON': 'das ist kein json', 'abgeschnitten': '{"0": [1, 2', 'Liste-statt-Objekt': '[1, 2]', 'Text': '"abc"',
    'Zahl': '5', 'null': 'null', 'true': 'true', 'fremder-file_index': '{"5": [0]}', 'negativer-Schluessel': '{"-1": [0]}',
    'Schluessel-mit-fuehrender-Null': '{"00": [0]}', 'Schluessel-mit-Leerzeichen': '{" 0": [0]}', 'Schluessel-Text': '{"a": [0]}',
    'Schluessel-arabisch': '{"٠": [0]}', 'Schluessel-leer': '{"": [0]}', 'Schluessel-zu-gross': '{"1' + '0' * 40 + '": [0]}',
    'Index-ausserhalb': '{"0": [99]}', 'Index-gleich-Anzahl': '{"0": [5]}', 'Index-negativ': '{"0": [-1]}',
    'doppelt': '{"0": [1, 1]}', 'bool-true': '{"0": [true]}', 'bool-false': '{"0": [false]}', 'Kommazahl-1.0': '{"0": [1.0]}',
    'Kommazahl-1e0': '{"0": [1e0]}', 'Text-Index': '{"0": ["1"]}', 'NaN': '{"0": [NaN]}', 'null-Index': '{"0": [null]}',
    'Liste-in-Liste': '{"0": [[1]]}', 'Wert-Text': '{"0": "abc"}', 'Wert-Objekt': '{"0": {"a": 1}}', 'Wert-null': '{"0": null}',
    'Wert-Zahl': '{"0": 1}', 'zwei-Objekte': '{"0": [0]}{"0": [1]}', 'Rest-hinter-dem-Objekt': '{"0": [0]} x',
    'doppelter-Schluessel': '{"0": [0], "0": [1]}', 'riesige-Zahl': '{"0": [' + '9' * 5000 + ']}',
    'tief-verschachtelt': '[' * 200000, 'tief-im-Wert': '{"0": ' + '[' * 100000,
}


@pytest.mark.parametrize('selection', list(BAD_SELECTIONS.values()), ids=list(BAD_SELECTIONS))
def test_an_invalid_selection_is_a_400_and_nothing_is_imported(books, pending_factory, selection):
    import_id = pending_factory([_file('a.xml', ROWS)])
    status, answer = _confirm(books, import_id, selection)
    assert (status, answer) == (400, INVALID)
    assert _bank_bookings(books) == []
    status, answer = _confirm(books, import_id)                                        # die Datei des Imports ist unberührt
    assert status == 200 and answer['results'][0]['inserted'] == 5


def test_the_selection_field_given_twice_is_invalid(books, pending_factory):
    import_id = pending_factory([_file('a.xml', ROWS)])
    status, body = handlers.handle_confirm_import(books, {'import_id': [import_id], 'selection': ['{}', '{}']})
    assert (status, json.loads(body)) == (400, INVALID) and _bank_bookings(books) == []


def test_a_selection_with_one_bad_entry_imports_none_of_the_good_ones(books, pending_factory):
    _set_mixed(books, IBAN_B)
    import_id = pending_factory([_file('a.xml', ROWS), _file('b.xml', ROWS[:2], iban=IBAN_B)])
    status, answer = _confirm(books, import_id, '{"0": [1], "1": [7]}')
    assert (status, answer) == (400, INVALID) and _bank_bookings(books) == []


def test_a_selection_for_an_import_without_statements_is_invalid(books, pending_factory):
    import_id = pending_factory([])
    assert _confirm(books, import_id, {'0': [0]}) == (400, INVALID)


# ══ Nur beim gemischt genutzten Konto ════════════════════════════════════════════════════════════════════════════

def test_a_selection_for_a_business_account_is_refused_and_the_statement_stays_whole(books, pending_factory):
    import_id = pending_factory([_file('b.xml', ROWS, iban=IBAN_B)])
    assert _confirm(books, import_id, {'0': [0]}) == (400, MIXED_ONLY)
    assert _bank_bookings(books) == []
    status, answer = _confirm(books, import_id)                                        # ganz importieren geht weiter
    assert status == 200 and answer['results'][0]['inserted'] == 5


def test_a_selection_for_a_statement_without_an_account_is_refused(books, pending_factory):
    import_id = pending_factory([_file('x.xml', ROWS, iban='DE00000000001000000001')])
    assert _confirm(books, import_id, {'0': [0]}) == (400, MIXED_ONLY) and _bank_bookings(books) == []
    status, answer = _confirm(books, import_id)
    assert status == 200 and answer['results'][0]['account_found'] is False


def test_a_selection_for_a_cash_account_is_refused_even_if_marked_mixed(books, pending_factory):
    books.insert_account('Zweite Kasse', 'Owner', IBAN_CASH, '', '', is_cash=1, skr_account=1610, business_only=False)
    import_id = pending_factory([_file('k.xml', ROWS, iban=IBAN_CASH)])
    assert _confirm(books, import_id, {'0': [0]}) == (400, MIXED_ONLY) and _bank_bookings(books) == []


@pytest.mark.parametrize('stored', [2, -1, 0.5, 'x', ''])
def test_nonsense_in_the_flag_set_by_sql_counts_as_a_business_account(books, pending_factory, stored):
    _sql(books, 'UPDATE Accounts SET BusinessOnly = ? WHERE ID = ?', (stored, _account_id(books, IBAN_A)))
    import_id = pending_factory([_file('a.xml', ROWS)])
    assert _confirm(books, import_id, {'0': [0]}) == (400, MIXED_ONLY) and _bank_bookings(books) == []


def test_switching_the_account_back_to_a_business_account_takes_the_selection_away(books, pending_factory):
    import_id = pending_factory([_file('a.xml', ROWS)])
    _set_mixed(books, IBAN_A, mixed=False)
    assert _confirm(books, import_id, {'0': [0]}) == (400, MIXED_ONLY)
    _set_mixed(books, IBAN_A, mixed=True)
    status, answer = _confirm(books, import_id, {'0': [0]})
    assert status == 200 and answer['results'][0]['inserted'] == 1


def test_an_entry_for_another_statement_of_the_same_import_is_checked_too(books, pending_factory):
    """Importiert wird ein Beleg, genannt sind zwei – auch der andere muss eine Auswahl erlauben."""
    import_id = pending_factory([_file('a.xml', ROWS), _file('b.xml', ROWS[:2], iban=IBAN_B)])
    assert _confirm(books, import_id, {'0': [0], '1': [0]}, file_index=0) == (400, MIXED_ONLY)
    assert _bank_bookings(books) == []


def test_the_check_comes_before_any_write_even_for_a_later_statement(books, pending_factory):
    import_id = pending_factory([_file('a.xml', ROWS), _file('b.xml', ROWS[:2], iban=IBAN_B)])
    assert _confirm(books, import_id, {'0': [0], '1': [0]}) == (400, MIXED_ONLY)
    assert _bank_bookings(books) == []                                                 # auch Beleg 0 ist nicht importiert


# ══ Über HTTP ════════════════════════════════════════════════════════════════════════════════════════════════════

OWN_IBAN = 'DE89370400440532013000'
ENTRIES = [('10.00', 'DBIT', '2026-03-05', 'Alpha'), ('20.00', 'DBIT', '2026-03-06', 'Beta'),
           ('30.00', 'DBIT', '2026-03-07', 'Gamma')]


def _http_upload(port, login):
    content_type, body = _multipart([('auszug.xml', _camt('S1', ENTRIES))])
    response = send(port, 'POST', '/upload_receipts', {'Origin': own_origin(port), 'X-CSRF-Token': login.csrf,
                                                       'Content-Type': content_type}, body)
    assert response.status == 200, response.text
    return json.loads(response.text)


def _http_confirm(port, login, import_id, selection=None):
    form = {'import_id': import_id}
    if selection is not None:
        form['selection'] = selection if isinstance(selection, str) else json.dumps(selection)
    return post(port, '/confirm_transactions', headers={'Origin': own_origin(port)}, form=form, csrf=login.csrf)


@pytest.fixture
def mixed_giro(server_port, auto_login):
    auto_login.db.insert_account('Giro', 'Owner', OWN_IBAN, 'TESTDEFF', 'Testbank', skr_account=1800, business_only=False)
    return auto_login


def _http_texts(login):
    with closing(login.db._get_connection()) as con:
        return sorted(row[0] for row in con.execute("SELECT Text FROM Bookings WHERE BookingType = 'bank'"))


def test_over_http_the_preview_offers_the_selection_and_the_import_follows_it(server_port, mixed_giro):
    data = _http_upload(server_port, mixed_giro)
    (card,) = data['files']
    assert card['selectable'] is True and [t['index'] for t in card['transactions']] == [0, 1, 2]
    assert all(t['importable'] for t in card['transactions'])
    response = _http_confirm(server_port, mixed_giro, data['import_id'], {'0': [0, 2]})
    answer = json.loads(response.text)
    assert response.status == 200 and answer['results'][0]['inserted'] == 2 and answer['results'][0]['skipped_unselected'] == 1
    assert _http_texts(mixed_giro) == ['Alpha', 'Gamma']


def test_over_http_an_invalid_selection_is_a_400_with_the_fixed_sentence(server_port, mixed_giro):
    data = _http_upload(server_port, mixed_giro)
    for bad in ('kein json', {'3': [0]}, {'0': [0, 0]}, {'0': [True]}):
        response = _http_confirm(server_port, mixed_giro, data['import_id'], bad)
        assert response.status == 400 and json.loads(response.text) == INVALID, bad
    assert _http_texts(mixed_giro) == []
    assert json.loads(_http_confirm(server_port, mixed_giro, data['import_id']).text)['results'][0]['inserted'] == 3


def test_over_http_a_business_account_gets_no_selection(server_port, auto_login):
    auto_login.db.insert_account('Giro', 'Owner', OWN_IBAN, 'TESTDEFF', 'Testbank', skr_account=1800)
    data = _http_upload(server_port, auto_login)
    assert data['files'][0]['selectable'] is False
    response = _http_confirm(server_port, auto_login, data['import_id'], {'0': [0]})
    assert response.status == 400 and json.loads(response.text) == MIXED_ONLY
    assert json.loads(_http_confirm(server_port, auto_login, data['import_id']).text)['results'][0]['inserted'] == 3


DKB_CSV = ('"Girokonto";"DE89370400440532013000"\r\n"Zeitraum:";"01.01.2024 - 31.12.2024"\r\n\r\n'
           '"Buchungsdatum";"Wertstellung";"Status";"Zahlungspflichtige*r";"Zahlungsempfänger*in";"Verwendungszweck";'
           '"Umsatztyp";"IBAN";"Betrag (€)"\r\n'
           '"30.12.24";"30.12.24";"Gebucht";"Erika Probe";"Laden A";"Einkauf A";"Ausgang";"";"-15"\r\n'
           '"29.12.24";"29.12.24";"Gebucht";"Erika Probe";"Laden B";"Einkauf B";"Ausgang";"";"-1.000"\r\n'
           '"28.12.24";"28.12.24";"Gebucht";"Kunde C";"Erika Probe";"Rechnung C";"Eingang";"";"2.500,50"\r\n')


def test_over_http_a_dkb_csv_for_a_mixed_account_offers_the_selection_and_books_exact_amounts(server_port, mixed_giro):
    """Der ganze Weg: CSV hochladen (nur im Speicher), in der Vorschau zwei von drei Umsätzen wählen, importieren."""
    content_type, body = _multipart([('umsaetze.csv', DKB_CSV.encode('utf-8-sig'))])
    response = send(server_port, 'POST', '/upload_receipts', {'Origin': own_origin(server_port), 'X-CSRF-Token': mixed_giro.csrf,
                                                              'Content-Type': content_type}, body)
    assert response.status == 200, response.text
    data = json.loads(response.text)
    (card,) = data['files']
    assert data['other_files'] == [] and card['bank_code'] == 'DKB' and card['selectable'] is True
    assert [t['index'] for t in card['transactions']] == [0, 1, 2] and card['warnings'] == ['no_balance']

    answer = json.loads(_http_confirm(server_port, mixed_giro, data['import_id'], {'0': [1, 2]}).text)
    assert answer['results'][0]['inserted'] == 2 and answer['results'][0]['skipped_unselected'] == 1
    with closing(mixed_giro.db._get_connection()) as con:
        booked = con.execute("SELECT DateBooking, Amount, Text FROM Bookings WHERE BookingType = 'bank' "
                             "ORDER BY DateBooking").fetchall()
    assert booked == [('2024-12-28', 25005000, 'Rechnung C'), ('2024-12-29', -10000000, 'Einkauf B')]


# ══ Das Bankkonto-Formular ═══════════════════════════════════════════════════════════════════════════════════════

def _form_fields(page):
    """Die Attribute des Kästchens und des versteckten Felds im Formular (``Dom.all`` liefert die Attribute je Element)."""
    dom = Dom(page)
    boxes = [a for a in dom.all('input') if a.get('name') == 'business_only']
    hidden = [a for a in dom.all('input') if a.get('name') == 'business_only_field']
    return dom, boxes, hidden


def test_the_new_account_form_has_the_flag_checked_by_default_with_the_fixed_hint(books):
    page = PageBankAccounts(books)
    dom, boxes, hidden = _form_fields(page)
    (box,), (field,) = boxes, hidden
    assert box['type'] == 'checkbox' and box['value'] == '1' and 'checked' in box
    assert field['type'] == 'hidden' and field['value'] == '1'
    assert BUSINESS_ONLY_HINT in page and page.count(BUSINESS_ONLY_HINT) == 1
    assert BUSINESS_ONLY_HINT == ('Auf einem reinen Geschäftskonto werden Kontoauszüge vollständig importiert; einzelne private '
                                  'Umsätze bucht man als Privatentnahme oder -einlage. Ohne den Haken (gemischt genutztes Konto) '
                                  'lassen sich beim Import einzelne Umsätze auswählen.')
    assert 'Reines Geschäftskonto' in page


def test_the_edit_form_shows_the_stored_flag(books):
    mixed, business = _account_id(books, IBAN_A), _account_id(books, IBAN_B)
    assert 'checked' not in _form_fields(PageBankAccounts(books, edit_id=mixed))[1][0]
    assert 'checked' in _form_fields(PageBankAccounts(books, edit_id=business))[1][0]


@pytest.mark.parametrize('stored', [2, -1, 0.5, 'x'])
def test_a_nonsense_value_is_shown_as_a_business_account(books, stored):
    account = _account_id(books, IBAN_A)
    _sql(books, 'UPDATE Accounts SET BusinessOnly = ? WHERE ID = ?', (stored, account))
    assert 'checked' in _form_fields(PageBankAccounts(books, edit_id=account))[1][0]


def test_the_cash_form_has_no_flag(books):
    cash = next(row[0] for row in books.fetch_accounts() if row[6] == 1)
    page = PageBankAccounts(books, edit_id=cash)
    _dom, boxes, hidden = _form_fields(page)
    assert boxes == [] and hidden == [] and BUSINESS_ONLY_HINT not in page


def _form(**fields):
    return {key: [value] for key, value in fields.items()}


def _add_form(name='Neu', **extra):
    return _form(name=name, holder='Owner', iban='DE02100500000054540402', bic='X', bank_name='Bank', skr_account='1820', **extra)


def _stored_flag(db, name):
    with closing(db._get_connection()) as con:
        return con.execute('SELECT BusinessOnly FROM Accounts WHERE Name = ?', (name,)).fetchone()[0]


def test_a_new_account_without_the_hidden_field_is_a_business_account(books):
    assert handlers.handle_add_bankaccount(books, _add_form()) == (303, '/masterdata/bankaccounts')
    assert _stored_flag(books, 'Neu') == 1


@pytest.mark.parametrize('fields, stored', [
    pytest.param(dict(business_only_field='1', business_only='1'), 1, id='angehakt'),
    pytest.param(dict(business_only_field='1'), 0, id='abgehakt-schickt-nichts'),
    pytest.param(dict(business_only_field='1', business_only='ja'), 0, id='nur-genau-1-gilt'),
    pytest.param(dict(business_only_field='1', business_only='on'), 0, id='on-ist-nicht-1'),
    pytest.param(dict(business_only_field='1', business_only=''), 0, id='leer'),
    pytest.param(dict(business_only='1'), 1, id='ohne-verstecktes-Feld-bleibt-der-Standard'),
])
def test_the_add_handler_reads_the_flag_only_with_the_hidden_field(books, fields, stored):
    handlers.handle_add_bankaccount(books, _add_form(**fields))
    assert _stored_flag(books, 'Neu') == stored


@pytest.mark.parametrize('start, fields, stored', [
    pytest.param(1, dict(business_only_field='1'), 0, id='Haken-entfernt'),
    pytest.param(0, dict(business_only_field='1', business_only='1'), 1, id='Haken-gesetzt'),
    pytest.param(0, dict(), 0, id='ohne-Feld-bleibt-0'),
    pytest.param(1, dict(), 1, id='ohne-Feld-bleibt-1'),
    pytest.param(0, dict(business_only='1'), 0, id='Kaestchen-ohne-verstecktes-Feld-wird-ignoriert'),
    pytest.param(1, dict(business_only_field='1', business_only='0'), 0, id='0-ist-nicht-angehakt'),
])
def test_the_update_handler_leaves_the_flag_unless_the_form_carries_it(books, start, fields, stored):
    account = _account_id(books, IBAN_B)
    _set_mixed(books, IBAN_B, mixed=(start == 0))
    form = _form(id=str(account), name='Konto B', holder='Owner', iban=IBAN_B, bic='INGDDEFF', bank_name='Bank B',
                 skr_account='1210', **fields)
    assert handlers.handle_update_bankaccount(books, form) == (303, '/masterdata/bankaccounts')
    assert _stored_flag(books, 'Konto B') == stored


def test_a_form_without_the_field_still_changes_the_other_values(books):
    account = _account_id(books, IBAN_A)
    form = _form(id=str(account), name='Konto A neu', holder='Owner', iban=IBAN_A, bic='X', bank_name='Bank A', skr_account='1200')
    handlers.handle_update_bankaccount(books, form)
    assert _stored_flag(books, 'Konto A neu') == 0 and books.get_account_by_id(account)[1] == 'Konto A neu'


# ══ Die Seite: HTML der Karten ═══════════════════════════════════════════════════════════════════════════════════

SELECT_NOTE = 'Einzelne Umsätze auswählen: in den Stammdaten des Bankkontos den Haken „Reines Geschäftskonto“ entfernen.'
HOSTILE = '"><script>alert(1)</script>'
LONG_TEXT = 'Rechnung ' + 'lang ' * 30 + 'Ende'


@pytest.fixture
def preview(books):
    """Vorschau mit allem, was eine Karte zeigen kann: Karte 0 auf dem gemischt genutzten Konto (ein Duplikat, eine
    unvollständige Zeile, feindlicher Text, ein langer Verwendungszweck), Karte 1 auf dem reinen Geschäftskonto, Karte 2 ohne
    Konto."""
    books.insert_booking(date_booking='2025-01-10', amount=-10.0, account_id=_account_id(books, IBAN_A),
                         foreign_bank_account='DE00FOREIGN', recipient_client='Partner', text='Netflix', booking_type='bank')
    files = [_file('a.xml', [_txn('2025-01-10', -10.0, 'Netflix'), _txn('2025-01-11', -20.0, 'Miete', recipient=HOSTILE),
                             _txn(None, 5.0, 'ohne Datum'), _txn('2025-01-13', -40.0, LONG_TEXT),
                             _txn('2025-01-14', 1234.5, '</td><img src=x onerror=alert(1)>')]),
             _file('b.xml', [_txn('2025-01-11', -5.0, 'Neu'), _txn('2025-01-12', -6.0, 'Neu 2')], iban=IBAN_B),
             _file('c.xml', [_txn('2025-01-11', -5.0, 'Fremd')], iban='DE00000000001000000001')]
    return build_import_preview(books, {'files': files})


def _cards(out):
    return [n for n in Tree(out['html']).root['children'] if n['attrs'].get('id', '').startswith('beleg-')]


def _by_class(node, tag, css_class):
    return [n for n in _walk(node) if n['tag'] == tag and css_class in n['attrs'].get('class', '').split()]


def _render(tmp_path, db, preview_files):
    return _node(tmp_path, _preview_source(db), {'preview': {'files': preview_files, 'other_files': []}})


@NO_NODE
def test_a_selectable_card_has_a_checkbox_per_row_a_search_field_all_none_and_the_counter(tmp_path, books, preview):
    out = _render(tmp_path, books, preview['files'])
    assert Tree(out['html']).errors == []
    card = _cards(out)[0]

    boxes = _by_class(card, 'input', 'selBox')
    assert [b['attrs']['data-i'] for b in boxes] == ['0', '1', '2', '3', '4'] and {b['attrs']['data-idx'] for b in boxes} == {'0'}
    assert all(b['attrs']['type'] == 'checkbox' for b in boxes)
    # angehakt: jede importierbare Zeile; Duplikat (0) und unvollständige Zeile (2): deaktiviert und nicht angehakt
    assert ['checked' in b['attrs'] for b in boxes] == [False, True, False, True, True]
    assert ['disabled' in b['attrs'] for b in boxes] == [True, False, True, False, False]
    (search,) = _by_class(card, 'input', 'selSearch')
    assert search['attrs']['type'] == 'search' and search['attrs']['data-idx'] == '0'
    assert [_text(b) for b in _buttons(card, 'btnSelAll')] == ['Alle'] and [_text(b) for b in _buttons(card, 'btnSelNone')] == ['Keine']
    assert re.sub(r'\s+', ' ', _text(card)).count('3 von 3 ausgewählt') == 1
    (button,) = _buttons(card, 'btnImportBeleg')
    assert _text(button) == 'Importieren (3 ausgewählt)' and button['attrs']['id'] == 'btnimp-0' and button['attrs']['data-idx'] == '0'
    assert SELECT_NOTE not in _text(card)


@NO_NODE
def test_every_row_carries_its_full_text_so_one_can_find_four_among_four_hundred_fifty(tmp_path, books, preview):
    card = _cards(_render(tmp_path, books, preview['files']))[0]
    rows = _by_class(card, 'tr', 'selRow')
    assert len(rows) == 5
    assert LONG_TEXT in _text(rows[3])                                                  # ungekürzt in der Zeile
    assert '2025-01-13' in _text(rows[3]) and '-40,00' in _text(rows[3])
    haystack = rows[3]['attrs']['data-hay']
    assert haystack == haystack.lower() and LONG_TEXT.lower() in haystack and '2025-01-13' in haystack
    assert '-40,00' in haystack and '-40' in haystack                                   # der Betrag deutsch und als Zahl
    assert '1.234,50' in rows[4]['attrs']['data-hay']
    assert 'row-open' in rows[0]['attrs']['class'] and 'row-open' in rows[2]['attrs']['class']       # Duplikat, unvollständig
    assert 'row-open' not in rows[1]['attrs']['class']


@NO_NODE
def test_values_from_the_statement_never_reach_the_page_as_markup(tmp_path, books, preview):
    out = _render(tmp_path, books, preview['files'])
    html, tree = out['html'], Tree(out['html'])
    assert tree.errors == []
    assert not [n for n in _walk(tree.root) if n['tag'] in ('script', 'img')]
    assert '<script' not in html and '<img' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html and '&lt;img src=x onerror=alert(1)&gt;' in html
    row = _by_class(_cards(out)[0], 'tr', 'selRow')[1]
    assert HOSTILE in row['attrs']['data-hay']                                           # als Attributwert, nicht als Markup
    assert HOSTILE in _text(row)                                                         # als Text, nicht als Element


@NO_NODE
def test_a_business_account_card_looks_as_before_with_a_muted_note(tmp_path, books, preview):
    card = _cards(_render(tmp_path, books, preview['files']))[1]
    assert not _by_class(card, 'input', 'selBox') and not _by_class(card, 'input', 'selSearch')
    assert not _buttons(card, 'btnSelAll') and not _buttons(card, 'btnSelNone')
    assert 'ausgewählt' not in _text(card)
    (button,) = _buttons(card, 'btnImportBeleg')
    assert _text(button) == 'Importieren (2 neu)'                                         # wie bisher
    (note,) = [n for n in _walk(card) if n['tag'] == 'div' and 'hint' in n['attrs'].get('class', '').split()]
    assert 'muted' in note['attrs']['class'].split() and _text(note) == SELECT_NOTE
    assert not _by_class(card, 'table', 'selTable')


@NO_NODE
def test_a_card_without_an_account_gets_neither_the_selection_nor_the_note(tmp_path, books, preview):
    card = _cards(_render(tmp_path, books, preview['files']))[2]
    assert 'Import nicht möglich' in _text(card) and SELECT_NOTE not in _text(card)
    assert not _by_class(card, 'input', 'selBox') and not _buttons(card, 'btnImportBeleg')


@NO_NODE
def test_a_preview_without_selectable_renders_like_before(tmp_path, books, preview):
    """Alte Vorschau-Daten (kein ``selectable``) zeigen die Karte wie bisher – ohne Kästchen."""
    files = [{k: v for k, v in f.items() if k != 'selectable'} for f in preview['files']]
    out = _render(tmp_path, books, files)
    assert Tree(out['html']).errors == []
    assert not _by_class(Tree(out['html']).root, 'input', 'selBox')
    assert _text(_buttons(_cards(out)[0], 'btnImportBeleg')[0]) == 'Importieren (3 neu)'


def test_the_page_offers_csv_uploads_and_the_new_markup_adds_no_inline_style(tmp_db):
    from server.pages_transactions import PageTransactions
    from tests.test_import_upload_http import _page_script
    page = PageTransactions(tmp_db, date_from='2026-01-01', date_to='2026-12-31')
    assert 'CSV (Umsatzliste der DKB)' in page and '.csv' in page.split('id="fileInput"')[1].split('>')[0]
    script = _page_script(tmp_db)
    card = script[script.index('function renderCard('):script.index("document.addEventListener('click'")]
    new_markup = card[card.index('const fileNo'):card.index("h += '</table></div>'")]
    assert new_markup.count('style=') == 1                                               # nur der bisherige Einklapp-Container


# ══ Die Seite: Verhalten im Skript (Node, mit kleinem DOM) ═══════════════════════════════════════════════════════

DOM_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, scenarioFile] = process.argv;
const scenario = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));
const messages = [], calls = [], generic = {};
function element(id) {
  return generic[id] || (generic[id] = {id, innerHTML: '', style: {}, textContent: '', disabled: false, removed: false,
                                        remove() { this.removed = true; }});
}
// Karten wie sie renderCard baut: je Zeile ein Kästchen mit data-i, ein Suchtext (data-hay) und eine Klasse selHidden
const cards = {};
const buttons = [];
for (const [idx, card] of Object.entries(scenario.cards)) {
  cards[idx] = card.rows.map(r => {
    const row = {dataset: {hay: r.hay}, hidden: false,
                 classList: {toggle(name, on) { if (name === 'selHidden') row.hidden = !!on; }}};
    row.box = {checked: !!r.checked, disabled: !!r.disabled, dataset: {i: String(r.i), idx}};
    return row;
  });
  const button = element('btnimp-' + idx);
  button.dataset = {idx};
  buttons.push(button);
}
function select(sel) {
  let m;
  if ((m = /^#det-(\d+) \.selBox$/.exec(sel))) return (cards[m[1]] || []).map(r => r.box);
  if ((m = /^#det-(\d+) \.selRow$/.exec(sel))) return cards[m[1]] || [];
  if ((m = /^#det-(\d+) \.selRow:not\(\.selHidden\) \.selBox:not\(:disabled\)$/.exec(sel)))
    return (cards[m[1]] || []).filter(r => !r.hidden && !r.box.disabled).map(r => r.box);
  if (sel === '.btnImportBeleg' || sel === '.btnImportBeleg, .btnImportAll, .btnDismiss') return buttons;
  return [];
}
const sandbox = {
  document: {getElementById: element, querySelectorAll: select, addEventListener: () => {}},
  window: {location: {href: 'http://127.0.0.1/transactions'}},
  appMsg: (text, kind) => messages.push([text, kind]),
  fetch: (url, init) => {
    calls.push({url, body: init && init.body ? String(init.body) : ''});
    return Promise.resolve({json: () => Promise.resolve(scenario.response || {}), text: () => Promise.resolve('')});
  },
  DOMParser: class { parseFromString() { return {getElementById: () => null}; } },
  URLSearchParams, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);
(async () => {
  for (const step of [].concat(scenario.action)) {
    vm.runInContext(step, sandbox);
    await new Promise(resolve => setTimeout(resolve, 30));
  }
  const out = {messages, calls};
  out.cards = Object.fromEntries(Object.entries(cards).map(([idx, rows]) =>
    [idx, rows.map(r => ({i: r.box.dataset.i, checked: r.box.checked, disabled: r.box.disabled, hidden: r.hidden}))]));
  out.elements = Object.fromEntries(Object.entries(generic).map(([id, e]) =>
    [id, {text: e.textContent, disabled: e.disabled, removed: e.removed}]));
  console.log(JSON.stringify(out));
})();
"""


def _dom(tmp_path, db, cards, action, response=None):
    """Führt das Skript der Seite gegen ein kleines DOM aus: ``cards`` = ``{Karte: {rows: [{i, hay, checked, disabled}]}}``,
    ``action`` = Aufrufe (ein Text oder eine Liste), ``response`` = die Antwort des Servers."""
    harness, script, data = (tmp_path / name for name in ('dom_harness.js', 'preview.js', 'dom_scenario.json'))
    harness.write_text(DOM_HARNESS, encoding='utf-8')
    script.write_text(_preview_source(db), encoding='utf-8')
    data.write_text(json.dumps({'cards': cards, 'action': action, 'response': response or {}}), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(script), str(data)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _row(i, hay, checked=True, disabled=False):
    return {'i': i, 'hay': hay.lower(), 'checked': checked and not disabled, 'disabled': disabled}


FOUR = {'0': {'rows': [_row(0, '2025-01-10 netflix partner 10,00 € 10'), _row(1, '2025-01-11 miete vermieter 20,00 € 20'),
                       _row(2, '2025-01-12 privat kino 30,00 €', disabled=True), _row(3, '2025-01-13 stadtwerke strom miete')]}}


def _sent(call):
    from urllib.parse import parse_qs
    return {key: values[0] for key, values in parse_qs(call['body']).items()}


def _import_calls(out):
    return [call for call in out['calls'] if call['url'] == '/confirm_transactions']


@NO_NODE
def test_the_single_import_sends_the_checked_boxes_of_its_card(tmp_path, books):
    cards = {'0': {'rows': [_row(0, 'a', checked=True), _row(1, 'b', checked=False), _row(2, 'c', checked=True),
                            _row(3, 'd', disabled=True)]}}
    out = _dom(tmp_path, books, cards, "currentImportId = 'abc'; importBeleg('0')",
               {'ok': True, 'results': [{'file_index': 0, 'inserted': 2, 'skipped': 0, 'skipped_unselected': 1,
                                         'account_found': True}]})
    (call,) = _import_calls(out)
    sent = _sent(call)
    assert sent['import_id'] == 'abc' and sent['file_index'] == '0'
    assert json.loads(sent['selection']) == {'0': [0, 2]}


@NO_NODE
def test_a_card_without_boxes_sends_no_selection(tmp_path, books):
    out = _dom(tmp_path, books, {'1': {'rows': []}}, "currentImportId = 'abc'; importBeleg('1')",
               {'ok': True, 'results': [{'file_index': 1, 'inserted': 3, 'skipped': 0, 'account_found': True}]})
    (call,) = _import_calls(out)
    assert 'selection' not in _sent(call) and _sent(call)['file_index'] == '1'


@NO_NODE
def test_all_cards_import_sends_only_the_selections_of_cards_with_boxes(tmp_path, books):
    cards = {'0': {'rows': [_row(0, 'a'), _row(1, 'b', checked=False)]}, '1': {'rows': []},
             '2': {'rows': [_row(0, 'a', checked=False), _row(1, 'b', checked=False)]}}
    out = _dom(tmp_path, books, cards, "currentImportId = 'abc'; importAll()",
               {'ok': True, 'results': [{'file_index': 0, 'inserted': 1, 'skipped': 0, 'account_found': True}]})
    (call,) = _import_calls(out)
    sent = _sent(call)
    assert 'file_index' not in sent
    assert json.loads(sent['selection']) == {'0': [0], '2': []}                           # Karte 1 ohne Kästchen: ganz importieren


@NO_NODE
def test_all_cards_import_without_any_box_sends_no_selection_field(tmp_path, books):
    out = _dom(tmp_path, books, {'0': {'rows': []}}, "currentImportId = 'abc'; importAll()",
               {'ok': True, 'results': [{'file_index': 0, 'inserted': 1, 'skipped': 0, 'account_found': True}]})
    assert 'selection' not in _sent(_import_calls(out)[0])


@NO_NODE
def test_the_messages_name_the_unselected_rows_only_when_there_are_some(tmp_path, books):
    single = {'ok': True, 'results': [{'file_index': 0, 'inserted': 2, 'skipped': 1, 'skipped_upload': 0, 'skipped_invalid': 1,
                                       'skipped_unselected': 3, 'text_updated': 0, 'account_found': True}]}
    out = _dom(tmp_path, books, {'0': {'rows': []}}, "importBeleg('0')", single)
    assert out['messages'] == [['2 neu importiert, 1 Duplikate übersprungen, 1 ohne Datum/Betrag nicht importiert, '
                                '3 nicht ausgewählt', 'success']]
    many = {'ok': True, 'results': [
        {'file_index': 0, 'inserted': 1, 'skipped': 0, 'skipped_unselected': 4, 'account_found': True},
        {'file_index': 1, 'inserted': 2, 'skipped': 0, 'skipped_unselected': 1, 'skipped_invalid': 1, 'account_found': True},
        {'file_index': 2, 'inserted': 0, 'skipped': 0, 'account_found': False, 'error': 'kein Konto'}]}
    out = _dom(tmp_path, books, {'0': {'rows': []}}, 'importAll()', many)
    assert out['messages'] == [['3 neu importiert, 1 ohne Datum/Betrag nicht importiert, 5 nicht ausgewählt, 1 ohne Konto', 'warn']]
    quiet = {'ok': True, 'results': [{'file_index': 0, 'inserted': 3, 'skipped': 0, 'skipped_unselected': 0, 'account_found': True}]}
    assert _dom(tmp_path, books, {'0': {'rows': []}}, 'importAll()', quiet)['messages'] == [['3 neu importiert', 'success']]


@NO_NODE
def test_none_selects_nothing_all_selects_the_importable_rows_and_the_counter_follows(tmp_path, books):
    out = _dom(tmp_path, books, FOUR, ["setSelection('0', false)", "setSelection('0', true)"])
    assert [(c['i'], c['checked']) for c in out['cards']['0']] == [('0', True), ('1', True), ('2', False), ('3', True)]
    assert out['elements']['selcount-0']['text'] == '3 von 3 ausgewählt'                  # die deaktivierte Zeile zählt nicht mit
    assert out['elements']['btnimp-0']['text'] == 'Importieren (3 ausgewählt)' and out['elements']['btnimp-0']['disabled'] is False

    out = _dom(tmp_path, books, FOUR, "setSelection('0', false)")
    assert not any(c['checked'] for c in out['cards']['0'])
    assert out['elements']['selcount-0']['text'] == '0 von 3 ausgewählt'
    assert out['elements']['btnimp-0']['text'] == 'Importieren (0 ausgewählt)' and out['elements']['btnimp-0']['disabled'] is True


@NO_NODE
def test_the_search_filters_the_visible_rows_without_regard_to_case_and_all_acts_on_them_only(tmp_path, books):
    """Vier aus vierhundertfünfzig: erst „Keine“, dann suchen, dann „Alle“ – nur die sichtbaren Zeilen werden angehakt."""
    out = _dom(tmp_path, books, FOUR, ["setSelection('0', false)", "filterRows('0', 'MIETE')", "setSelection('0', true)"])
    rows = out['cards']['0']
    assert [(c['i'], c['hidden']) for c in rows] == [('0', True), ('1', False), ('2', True), ('3', False)]
    assert [(c['i'], c['checked']) for c in rows] == [('0', False), ('1', True), ('2', False), ('3', True)]
    assert out['elements']['selcount-0']['text'] == '2 von 3 ausgewählt'
    assert out['elements']['btnimp-0']['text'] == 'Importieren (2 ausgewählt)'


@NO_NODE
def test_several_words_must_all_match_and_an_empty_search_shows_every_row_again(tmp_path, books):
    out = _dom(tmp_path, books, FOUR, "filterRows('0', '  Miete   strom ')")
    assert [c['hidden'] for c in out['cards']['0']] == [True, True, True, False]
    out = _dom(tmp_path, books, FOUR, ["filterRows('0', 'miete')", "filterRows('0', '')"])
    assert [c['hidden'] for c in out['cards']['0']] == [False] * 4
    out = _dom(tmp_path, books, FOUR, "filterRows('0', '10,00 €')")                       # der Betrag deutsch formatiert
    assert [c['hidden'] for c in out['cards']['0']] == [False, True, True, True]
    out = _dom(tmp_path, books, FOUR, "filterRows('0', '2025-01-12')")                    # das Datum – auch einer deaktivierten Zeile
    assert [c['hidden'] for c in out['cards']['0']] == [True, True, False, True]


@NO_NODE
def test_a_filtered_out_row_keeps_its_checkmark_and_is_still_imported(tmp_path, books):
    out = _dom(tmp_path, books, FOUR, ["filterRows('0', 'kino')", "currentImportId = 'abc'; importBeleg('0')"],
               {'ok': True, 'results': [{'file_index': 0, 'inserted': 3, 'skipped': 0, 'account_found': True}]})
    assert json.loads(_sent(_import_calls(out)[0])['selection']) == {'0': [0, 1, 3]}       # das Suchfeld wählt nichts ab


@NO_NODE
def test_the_import_button_stays_disabled_at_zero_selected_even_after_an_import_ends(tmp_path, books):
    cards = {**FOUR, '1': {'rows': []}}
    out = _dom(tmp_path, books, cards, ["setSelection('0', false)", 'setImportBusy(true)', 'setImportBusy(false)'])
    assert out['elements']['btnimp-0']['disabled'] is True                                 # 0 gewählt: weiter gesperrt
    assert out['elements']['btnimp-1']['disabled'] is False                                # Karte ohne Auswahl: wieder frei
    out = _dom(tmp_path, books, cards, ['setImportBusy(true)'])
    assert out['elements']['btnimp-0']['disabled'] is True and out['elements']['btnimp-1']['disabled'] is True
    out = _dom(tmp_path, books, cards, ['setImportBusy(true)', 'setImportBusy(false)'])
    assert out['elements']['btnimp-0']['disabled'] is False and out['elements']['btnimp-1']['disabled'] is False


@NO_NODE
def test_a_checkbox_change_updates_the_counter_and_the_button(tmp_path, books):
    cards = {'0': {'rows': [_row(0, 'a'), _row(1, 'b'), _row(2, 'c')]}}
    out = _dom(tmp_path, books, cards, "updateSelection('0')")
    assert out['elements']['selcount-0']['text'] == '3 von 3 ausgewählt'
    cards['0']['rows'][1]['checked'] = False
    cards['0']['rows'][2]['checked'] = False
    out = _dom(tmp_path, books, cards, "updateSelection('0')")
    assert out['elements']['selcount-0']['text'] == '1 von 3 ausgewählt'
    assert out['elements']['btnimp-0']['text'] == 'Importieren (1 ausgewählt)' and out['elements']['btnimp-0']['disabled'] is False
