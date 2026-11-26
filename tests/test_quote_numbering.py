# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Angebotsnummern: Das erste Speichern – auch als Entwurf – vergibt die Nummer, wie bei der Rechnung.

Die Angebotsmaske schlug immer ``<JJ>A001`` vor: Sie las den Nummernkreis ``quote``, den es in keiner Installation gab (die Stammdaten
kennen die Art nicht), und ``handle_quote_save`` rückte nie einen Zähler vor – das zweite neue Angebot scheiterte mit „Diese Nummer ist
bereits vergeben.“. Jetzt vergibt ``Database.allocate_document_number`` die Nummer in der Transaktion des Speicherns
(``allocate_invoice_number`` ist dieselbe Funktion für die Art ``invoice``): Der Kreis ergibt sich aus dem Jahr des Angebotsdatums und
entsteht beim ersten Speichern (Buchstabe ``A``, Standardvorlage); der Vorschlag der Maske (``peek_next_number('quote', …)``) ist genau
die Nummer, die das Speichern vergibt – auch ohne Kreis und ohne eine schon belegte Nummer –, und eine eigene Nummer hebt den Zähler nur
nach oben. Ein gescheitertes Speichern verbraucht nichts, das Ändern eines Angebots fasst den Zähler nie an, das Löschen eines Angebots
lässt eine Lücke, und die Rechnungen bleiben unberührt. Alle Namen und Nummern sind erfundene Testwerte.
"""
import datetime
import json
import threading

import pytest

import server.handlers as handlers
from db import numbering
from db.numbering import NumberRangeError, number_format_error
from server.pages_quote import _quote_form_html
from tests.document_helpers import add_contacts, add_document
from tests.dom_helpers import Dom
from tests.test_invoice_numbering import _payload as _invoice_payload
from tests.test_invoice_recompute import _make_contacts
from tests.test_mixed_tax_mask import NO_NODE, run_mask, sent_payload

DEFAULT_TEMPLATE = '{yy}{l}{nnn}{s}'
TAKEN = 'Diese Nummer ist bereits vergeben.'


def _payload(own_id, cust_id, **over):
    p = {
        'quoteNumber': '26A100', 'quoteDate': '2026-06-07', 'validUntil': '2026-07-07', 'customerId': cust_id,
        'ownCompanyId': own_id, 'taxRate': 0.19, 'status': 'draft',
        'items': [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung', 'unitPrice': 100.0, 'taxRate': 19}],
    }
    p.update(over)
    return p


def _save(payload):
    """Speichern wie die Maske. Die Antwort nennt die Nummer nicht (die Maske lädt das Angebot danach neu): Für ein neues Angebot
    liest die Hilfe sie aus der Datenbank dazu (``quote_number``)."""
    resp = json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))
    if resp.get('success') and payload.get('quoteId') is None:
        assert set(resp) == {'success', 'quote_id'}
        resp['quote_number'] = handlers.Database().get_invoice_by_id(resp['quote_id'])[1]
    return resp


def _ranges(db):
    """Die Angebotskreise als ``(Jahr, Buchstabe, Zusatz, Zähler, Vorlage)``, jüngstes Jahr zuerst."""
    return [(r[2], r[3], r[4], r[5], r[7]) for r in db.fetch_number_ranges('quote')]


def _counter(db, year=2026, letter='A'):
    return [r[5] for r in db.fetch_number_ranges('quote') if r[2] == year and r[3] == letter][0]


def _number(db, quote_id):
    return db.get_invoice_by_id(quote_id)[1]


def _this_year():
    """Die Maske schlägt die Nummer für das laufende Jahr vor: gelesen wird es im Test, nie beim Import."""
    return datetime.date.today().year


@pytest.fixture
def world(tmp_db, monkeypatch):
    """Kontakte, aber KEIN Nummernkreis: So sieht jede Installation aus, die Stammdaten kennen die Art ``quote`` nicht."""
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return tmp_db, own, cust


def _new_quote(world, **over):
    """Neues Angebot, wie die Maske es anlegt: der Vorschlag des Nummernkreises, unverändert abgeschickt.
    Liefert die ID, die vergebene Nummer und den Vorschlag."""
    db, own, cust = world
    proposal = db.peek_next_number('quote', 2026)[0]
    resp = _save(_payload(own, cust, quoteNumber=proposal, numberSuggestion=proposal, **over))
    assert resp['success'] is True, resp
    return resp['quote_id'], resp['quote_number'], proposal


# ── das erste Speichern vergibt die Nummer (der Fehlfall) ────────────────────

def test_two_new_quotes_in_a_row_get_consecutive_numbers(world):
    db = world[0]
    first, first_number, first_proposal = _new_quote(world)
    assert (first_proposal, first_number) == ('26A001', '26A001')
    second, second_number, second_proposal = _new_quote(world)     # früher: wieder 26A001 und „Diese Nummer ist bereits vergeben.“
    assert (second_proposal, second_number) == ('26A002', '26A002')
    assert [_number(db, first), _number(db, second)] == ['26A001', '26A002']
    assert _counter(db) == 2


def test_the_proposal_is_the_number_the_save_hands_out(world):
    db = world[0]
    for _ in range(3):
        quote_id, number, proposal = _new_quote(world)
        assert _number(db, quote_id) == number == proposal         # keine Abweichung zwischen Maske und Speichern
        assert db.peek_next_number('quote', 2026)[0] != proposal


# ── Kreis nach dem Jahr des Datums, fehlender Kreis ──────────────────────────

def test_the_first_save_creates_the_range_of_the_year_of_the_quote_date(world):
    db, own, cust = world
    assert db.fetch_number_ranges() == []
    resp = _save(_payload(own, cust, quoteNumber='26A001', numberSuggestion='26A001'))
    assert (resp['success'], resp['quote_number']) == (True, '26A001')
    assert _ranges(db) == [(2026, 'A', '', 1, DEFAULT_TEMPLATE)]
    assert [r[1] for r in db.fetch_number_ranges()] == ['quote'] and db.fetch_number_ranges('invoice') == []


def test_a_new_year_gets_a_range_with_letter_suffix_and_template_of_the_newest(world):
    db, own, cust = world
    db.insert_number_range('quote', 2026, 'B', prefix='_X', current_number=9, number_format='{yyyy}/{l}{nnn}{s}')
    proposal = db.peek_next_number('quote', 2026)[0]
    assert proposal == '2026/B010_X'
    resp = _save(_payload(own, cust, quoteNumber=proposal, numberSuggestion=proposal, quoteDate='2027-01-04'))
    assert (resp['success'], resp['quote_number']) == (True, '2027/B001_X')       # der Kreis 2026 bleibt, wie er war
    assert _ranges(db) == [(2027, 'B', '_X', 1, '{yyyy}/{l}{nnn}{s}'), (2026, 'B', '_X', 9, '{yyyy}/{l}{nnn}{s}')]


def test_the_range_follows_the_year_of_the_quote_date(tmp_db):
    tmp_db.insert_number_range('quote', 2025, 'A', current_number=40)
    tmp_db.insert_number_range('quote', 2026, 'A', current_number=2)
    with tmp_db.transaction() as tx:
        assert tmp_db.allocate_document_number(tx.conn, 'quote', '2025-12-30') == '25A041'
        assert tmp_db.allocate_document_number(tx.conn, 'quote', '2026-01-02') == '26A003'
    assert [r[5] for r in tmp_db.fetch_number_ranges('quote')] == [3, 41]


def test_without_any_range_the_first_number_creates_one_per_type_with_its_letter(tmp_db):
    with tmp_db.transaction() as tx:
        assert tmp_db.allocate_document_number(tx.conn, 'quote', '2026-03-04') == '26A001'
        assert tmp_db.allocate_document_number(tx.conn, 'invoice', '2026-03-04') == '26R001'
        assert tmp_db.allocate_invoice_number(tx.conn, '2026-03-04') == '26R002'        # dieselbe Funktion für die Art ``invoice``
    assert sorted((r[1], r[3], r[5]) for r in tmp_db.fetch_number_ranges()) == [('invoice', 'R', 2), ('quote', 'A', 1)]


def test_an_unknown_document_type_is_refused_before_anything_is_written(tmp_db):
    with tmp_db.transaction() as tx:
        with pytest.raises(ValueError, match='Unbekannte Belegart'):
            tmp_db.allocate_document_number(tx.conn, 'receipt_company', '2026-03-04')
        assert not tx.conn.in_transaction                           # nicht einmal die Schreibsperre ist genommen
    assert tmp_db.fetch_number_ranges() == []


# ── der Vorschlag ────────────────────────────────────────────────────────────

def test_without_a_range_the_proposal_is_the_first_number_of_the_range_the_save_would_create(world):
    db = world[0]
    year = _this_year()
    assert db.peek_next_number('quote', 2026) == ('26A001', None)           # ein gedachter Kreis: ohne Kreis keine Kreis-ID
    assert db.peek_next_number('quote')[0] == f'{str(year)[2:]}A001'          # ohne Jahr das laufende
    assert db.fetch_number_ranges() == []                                    # angesehen, nichts angelegt
    db.insert_number_range('quote', 2026, 'B', prefix='_X', current_number=9, number_format='{yyyy}/{l}{nnn}{s}')
    range_id = db.fetch_number_ranges('quote')[0][0]
    assert db.peek_next_number('quote', 2026) == ('2026/B010_X', range_id)
    assert db.peek_next_number('quote', 2027) == ('2027/B001_X', None)      # Buchstabe, Zusatz und Vorlage des jüngsten Kreises
    assert _ranges(db) == [(2026, 'B', '_X', 9, '{yyyy}/{l}{nnn}{s}')]


def test_the_proposal_skips_hand_numbered_quotes_that_have_no_range(world):
    db, own, cust = world
    for number in ('26A001', '26A002', '26A003'):                           # von Hand vergeben: eine eigene Nummer legt keinen Kreis an
        assert _save(_payload(own, cust, quoteNumber=number))['success'] is True
    assert db.fetch_number_ranges('quote') == []
    assert db.peek_next_number('quote', 2026) == ('26A004', None)
    quote_id, number, proposal = _new_quote(world)
    assert (number, proposal) == ('26A004', '26A004')
    assert _number(db, quote_id) == '26A004' and _counter(db) == 4


def test_the_proposal_skips_hand_numbered_quotes_behind_the_counter(world):
    db, own, cust = world
    db.insert_number_range('quote', 2026, 'A', current_number=1)
    for number in ('26A002', '26A003'):                                     # eine eigene Nummer hebt den Zähler – hier von Hand zurückgesetzt
        assert _save(_payload(own, cust, quoteNumber=number))['success'] is True
    db.update_number_range(db.fetch_number_ranges('quote')[0][0], 2026, 'A', '', 1, '', None)
    assert db.peek_next_number('quote', 2026)[0] == '26A004'
    assert _new_quote(world)[1:] == ('26A004', '26A004') and _counter(db) == 4


def test_the_proposal_skips_a_number_an_invoice_carries(world):
    """Rechnungen und Angebote teilen sich die Tabelle und damit die eindeutige Nummer."""
    db, own, cust = world
    db.insert_number_range('quote', 2026, 'A', current_number=2)
    db.insert_invoice({'invoice_number': '26A003', 'invoice_date': '2026-01-02', 'seller_name': 'S', 'seller_company': 'S GmbH',
                       'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19, 'sum_net': 1, 'tax_amount': 0.19,
                       'sum_gross': 1.19, 'amount_due': 1.19, 'status': 'draft'})
    assert db.peek_next_number('quote', 2026)[0] == '26A004'
    assert _new_quote(world)[1:] == ('26A004', '26A004') and _counter(db) == 4


# ── eigene Nummern ───────────────────────────────────────────────────────────

def test_an_own_number_lifts_the_counter_but_never_lowers_it(world):
    db, own, cust = world
    db.insert_number_range('quote', 2026, 'A', current_number=0)
    assert _save(_payload(own, cust, quoteNumber='26A010'))['success'] is True
    assert _counter(db) == 10
    assert _save(_payload(own, cust, quoteNumber='26A004'))['success'] is True       # nach unten: nie
    assert _counter(db) == 10
    assert db.peek_next_number('quote', 2026)[0] == '26A011'


def test_an_own_number_outside_every_template_leaves_the_counter_alone(world):
    db, own, cust = world
    db.insert_number_range('quote', 2026, 'A', current_number=0)
    for number in ('ANG-17', '26X050', '25A050', '26A5x'):
        assert _save(_payload(own, cust, quoteNumber=number))['success'] is True
    assert _counter(db) == 0


def test_without_a_suggestion_the_number_is_an_own_number_as_before_and_creates_no_range(world):
    db, own, cust = world
    assert _save(_payload(own, cust, quoteNumber='26A001'))['quote_number'] == '26A001'
    assert db.fetch_number_ranges('quote') == []
    assert _save(_payload(own, cust, quoteNumber='26A001')) == {'success': False, 'error': TAKEN}


def test_a_number_the_user_typed_stays_an_own_number(world):
    db, own, cust = world
    db.insert_number_range('quote', 2026, 'A', current_number=0)
    proposal = db.peek_next_number('quote', 2026)[0]
    own_number = _save(_payload(own, cust, quoteNumber='26A050', numberSuggestion=proposal))
    assert (own_number['quote_number'], _counter(db)) == ('26A050', 50)
    taken = _save(_payload(own, cust, quoteNumber='26A050', numberSuggestion=proposal))
    assert taken == {'success': False, 'error': TAKEN}                      # eine eigene Nummer bleibt eigen
    assert _counter(db) == 50


# ── zwei Fenster ─────────────────────────────────────────────────────────────

def test_an_unchanged_stale_suggestion_gets_a_fresh_number(world):
    """Zwei Fenster zeigen denselben Vorschlag 26A001: Das zweite Speichern bekommt 26A002 statt „bereits vergeben“."""
    db, own, cust = world
    proposal = db.peek_next_number('quote', 2026)[0]
    first = _save(_payload(own, cust, quoteNumber=proposal, numberSuggestion=proposal))
    second = _save(_payload(own, cust, quoteNumber=proposal, numberSuggestion=proposal))
    assert (first['success'], first['quote_number']) == (True, '26A001')
    assert (second['success'], second['quote_number']) == (True, '26A002')          # die Nummer, die gilt
    assert _number(db, second['quote_id']) == '26A002' and _counter(db) == 2


def test_two_windows_with_ten_saves_each_hand_out_twenty_distinct_numbers(world):
    """Beide Fenster nehmen den Vorschlag, den sie gerade sehen – meist schon verbraucht: Keine Nummer doppelt, keine abgelehnt, keine Lücke."""
    db, own, cust = world
    results, errors = [], []

    def window():
        try:
            for _ in range(10):
                suggestion = db.peek_next_number('quote', 2026)[0]
                results.append(_save(_payload(own, cust, quoteNumber=suggestion, numberSuggestion=suggestion)))
        except Exception as exc:                      # pragma: no cover - nur bei einem Fehler
            errors.append(exc)

    threads = [threading.Thread(target=window) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and all(r['success'] for r in results), [r for r in results if not r['success']]
    assert sorted(r['quote_number'] for r in results) == [f'26A{n:03d}' for n in range(1, 21)]
    assert _counter(db) == 20


# ── ein gescheitertes Speichern verbraucht nichts ────────────────────────────

def test_a_failed_save_does_not_use_the_number(world):
    db, own, cust = world
    db.insert_number_range('quote', 2026, 'A', current_number=5)
    before = db.fetch_number_ranges('quote')
    bad = _payload(own, cust, quoteNumber='26A006', numberSuggestion='26A006')
    bad['items'].append({'position': 2, 'quantity': 1, 'unit': 'C62', 'description': None, 'unitPrice': 5.0, 'taxRate': 19})
    assert _save(bad)['success'] is False
    assert db.fetch_number_ranges('quote') == before and db.fetch_quotes() == []
    assert _new_quote(world)[1] == '26A006'


def test_a_failed_first_save_leaves_no_range_behind(world, monkeypatch):
    db, own, cust = world

    def fails(*args, **kwargs):
        raise RuntimeError('Position nicht geschrieben')

    with monkeypatch.context() as patch:
        patch.setattr(db, 'insert_invoice_item', fails)
        resp = _save(_payload(own, cust, quoteNumber='26A001', numberSuggestion='26A001'))
    assert resp['success'] is False and 'RuntimeError' not in json.dumps(resp)
    assert db.fetch_number_ranges() == [] and db.fetch_quotes() == []              # der Kreis entstand in der zurückgerollten Transaktion
    assert _new_quote(world)[1:] == ('26A001', '26A001') and _counter(db) == 1


# ── eine Formatvorlage ohne Zähler ───────────────────────────────────────────

def test_saving_with_a_template_without_a_counter_is_refused_in_plain_words(world):
    db, own, cust = world
    db.insert_number_range('quote', 2026, 'A', current_number=3, number_format='{yy}{l}')
    before = db.fetch_number_ranges('quote')
    assert db.peek_next_number('quote', 2026) == (None, before[0][0])              # kein Vorschlag – und kein Fehler
    resp = _save(_payload(own, cust, quoteNumber='26A', numberSuggestion='26A'))
    assert resp['success'] is False and 'Formatvorlage' in resp['error'] and 'Zähler' in resp['error']
    assert 'Traceback' not in json.dumps(resp) and db.fetch_number_ranges('quote') == before and db.fetch_quotes() == []
    assert _save(_payload(own, cust, quoteNumber='ANG-1'))['success'] is True       # eine eigene Nummer hindert die Vorlage nicht


# ── die Meldung eines Kreises ohne brauchbare Nummer nennt die Art des Belegs ─

NO_COUNTER_FORMAT = '{yy}{l}'


def test_saving_a_quote_with_a_template_without_a_counter_names_the_quote_number(world):
    """„Der Nummernkreis liefert keine Rechnungsnummer“ stand auch bei einem Angebot."""
    db, own, cust = world
    db.insert_number_range('quote', 2026, 'A', current_number=3, number_format=NO_COUNTER_FORMAT)
    resp = _save(_payload(own, cust, quoteNumber='26A', numberSuggestion='26A'))
    assert resp == {'success': False, 'error': 'Der Nummernkreis liefert keine Angebotsnummer. ' + number_format_error(NO_COUNTER_FORMAT)}


@pytest.mark.parametrize('range_type, noun', [('quote', 'Angebotsnummer'), ('invoice', 'Rechnungsnummer')])
def test_a_range_whose_template_has_no_counter_names_the_kind_of_document(tmp_db, range_type, noun):
    tmp_db.insert_number_range(range_type, 2026, 'X', current_number=3, number_format=NO_COUNTER_FORMAT)
    with tmp_db.transaction() as tx:
        with pytest.raises(NumberRangeError) as caught:
            tmp_db.allocate_document_number(tx.conn, range_type, '2026-03-04')
    assert str(caught.value) == f'Der Nummernkreis liefert keine {noun}. ' + number_format_error(NO_COUNTER_FORMAT)


@pytest.mark.parametrize('range_type, noun', [('quote', 'Angebotsnummer'), ('invoice', 'Rechnungsnummer')])
def test_too_many_taken_numbers_in_a_row_name_the_kind_of_document(tmp_db, monkeypatch, range_type, noun):
    """Rechnungen und Angebote teilen sich die Nummern: Hinter dem Zähler stehen ein Angebot und eine Rechnung, und der Kreis überspringt höchstens zwei."""
    own, cust = add_contacts(tmp_db)
    tmp_db.insert_number_range(range_type, 2026, 'X', current_number=0)
    add_document(tmp_db, '26X001', 'quote', own, cust)
    add_document(tmp_db, '26X002', 'invoice', own, cust)
    monkeypatch.setattr(numbering, '_MAX_SKIPPED_NUMBERS', 2)
    with tmp_db.transaction() as tx:
        with pytest.raises(NumberRangeError) as caught:
            tmp_db.allocate_document_number(tx.conn, range_type, '2026-03-04')
    assert str(caught.value) == f'Im Nummernkreis ist keine freie {noun} zu finden.'


# ── ändern und löschen ───────────────────────────────────────────────────────

def test_updating_a_quote_does_not_touch_the_counter_and_changes_the_number_only_when_the_user_does(world):
    db, own, cust = world
    quote_id, number, _ = _new_quote(world)
    db.update_number_range(db.fetch_number_ranges('quote')[0][0], 2026, 'A', '', 20, '', None)
    resp = _save(_payload(own, cust, quoteNumber=number, numberSuggestion=number, quoteId=quote_id, validUntil='2026-08-01'))
    assert resp == {'success': True, 'quote_id': quote_id}                          # die Antwort nennt keine Nummer
    assert _number(db, quote_id) == number and _counter(db) == 20
    resp = _save(_payload(own, cust, quoteNumber='26A777', quoteId=quote_id))      # eine Nummer, die der Nutzer ändert, gilt so
    assert resp == {'success': True, 'quote_id': quote_id}
    assert _number(db, quote_id) == '26A777' and _counter(db) == 20                 # auch wenn sie zur Vorlage passt: der Zähler bleibt


def test_deleting_a_quote_does_not_give_its_number_back(world):
    db = world[0]
    ids = [_new_quote(world)[0] for _ in range(3)]                                 # 26A001 … 26A003
    db.delete_quote(ids[2])                                                        # auch die letzte: eine Lücke, der Zähler bleibt
    assert _counter(db) == 3 and db.peek_next_number('quote', 2026)[0] == '26A004'
    db.delete_quote(ids[1])
    assert _counter(db) == 3 and db.peek_next_number('quote', 2026)[0] == '26A004'
    assert _new_quote(world)[1] == '26A004'


# ── die Seite ────────────────────────────────────────────────────────────────

def _form_numbers(db, quote_id=None):
    """Was die Maske zeigt: die Nummer im Feld und den Vorschlag im versteckten Feld."""
    dom = Dom(_quote_form_html(db, quote_id))
    return dom.one('input', id='quote_number')['value'], dom.one('input', id='number_suggestion')['value']


def test_the_form_shows_the_next_number_in_the_field_and_in_the_hidden_suggestion(world):
    db, own, cust = world
    year = _this_year()
    short = str(year)[2:]
    assert _form_numbers(db) == (f'{short}A001', f'{short}A001')
    shown, suggestion = _form_numbers(db)
    first = _save(_payload(own, cust, quoteNumber=shown, numberSuggestion=suggestion, quoteDate=f'{year}-06-07'))
    assert (first['success'], first['quote_number']) == (True, f'{short}A001')
    assert _form_numbers(db) == (f'{short}A002', f'{short}A002')                     # nach dem Speichern die NÄCHSTE Nummer (früher: immer wieder A001)
    assert _form_numbers(db, first['quote_id']) == (f'{short}A001', '')              # beim Bearbeiten: die eigene Nummer, kein Vorschlag


def test_the_suggestion_goes_into_the_hidden_field_escaped(world):
    db = world[0]
    year = _this_year()
    short = str(year)[2:]
    hostile = '"><zzz-evil>'
    db.insert_number_range('quote', year, 'A', prefix=hostile)
    dom = Dom(_quote_form_html(db))
    assert dom.one('input', id='number_suggestion')['value'] == f'{short}A001{hostile}'
    assert dom.one('input', id='quote_number')['value'] == f'{short}A001{hostile}'
    assert dom.all('zzz-evil') == []


def test_the_form_stays_usable_with_a_template_without_a_counter(world):
    db = world[0]
    db.insert_number_range('quote', _this_year(), 'A', current_number=3, number_format='{yy}{l}')
    assert _form_numbers(db) == ('', '')


# ── das Skript der Maske (Node) ──────────────────────────────────────────────

def _program(cust, year, before=''):
    """Was ein Nutzer vor dem Speichern tut: Kunde wählen, Datum eintragen, eine Position beschreiben (``before``: weiteres)."""
    return (f"$('customer_select').value = '{cust}'; $('quote_date').value = '{year}-06-07'; "
            f"rows()[0].querySelector('.item-description').value = 'Beratung'; {before}saveQuote();")


@NO_NODE
def test_two_new_masks_opened_before_either_saves_get_two_numbers(world, tmp_path):
    """Der Fehlfall mit der echten Maske: Beide Fenster zeigen die Nummer 1 und schicken sie unverändert ab."""
    db, own, cust = world
    year = _this_year()
    short = str(year)[2:]
    windows = [_quote_form_html(db), _quote_form_html(db)]
    payloads = [sent_payload(run_mask(tmp_path, html, 'quote', _program(cust, year)), 'quote') for html in windows]
    assert [(p['quoteNumber'], p['numberSuggestion']) for p in payloads] == [(f'{short}A001', f'{short}A001')] * 2
    assert [_save(p)['quote_number'] for p in payloads] == [f'{short}A001', f'{short}A002']
    assert _counter(db, year) == 2


@NO_NODE
def test_a_number_typed_into_the_mask_stays_an_own_number_and_the_edit_mask_sends_no_suggestion(world, tmp_path):
    db, own, cust = world
    year = _this_year()
    short = str(year)[2:]
    typed = sent_payload(run_mask(tmp_path, _quote_form_html(db), 'quote', _program(cust, year, "$('quote_number').value = 'ANG-1'; ")), 'quote')
    assert (typed['quoteNumber'], typed['numberSuggestion']) == ('ANG-1', f'{short}A001')     # der Vorschlag geht mit, die Nummer ist eine andere
    saved = _save(typed)
    assert (saved['success'], saved['quote_number']) == (True, 'ANG-1')
    assert db.fetch_number_ranges('quote') == []                                               # eine eigene Nummer legt keinen Kreis an
    edit = sent_payload(run_mask(tmp_path, _quote_form_html(db, saved['quote_id']), 'quote',
                                 "$('quote_number').value = 'ANG-2'; saveQuote();"), 'quote')
    assert edit['numberSuggestion'] is None and edit['quoteNumber'] == 'ANG-2' and edit['quoteId'] == saved['quote_id']


# ── Rechnungen bleiben unberührt ─────────────────────────────────────────────

def test_invoices_are_untouched_by_quotes_and_the_other_way_round(world):
    db, own, cust = world
    for _ in range(3):
        _new_quote(world)
    assert db.fetch_number_ranges('invoice') == [] and db.peek_next_number('invoice', 2026) == (None, None)    # die Angebote legen keinen Kreis an
    db.insert_number_range('invoice', 2026, 'R', current_number=7)
    invoice_ranges = db.fetch_number_ranges('invoice')
    assert db.peek_next_number('invoice', 2026)[0] == '26R008'
    _new_quote(world)
    assert db.fetch_number_ranges('invoice') == invoice_ranges and db.peek_next_number('invoice', 2026)[0] == '26R008'
    quote_ranges = db.fetch_number_ranges('quote')
    invoice = json.loads(handlers.handle_invoice_save(json.dumps(_invoice_payload(
        own, cust, invoiceNumber='26R008', numberSuggestion='26R008')).encode()))
    assert (invoice['success'], invoice['invoice_number']) == (True, '26R008')
    assert db.fetch_number_ranges('quote') == quote_ranges and _counter(db) == 4                         # und umgekehrt
