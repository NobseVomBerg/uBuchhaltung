# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Rechnungsnummern (2.1.2): Das erste Speichern – auch als Entwurf – vergibt die Nummer.

Der Zähler des Nummernkreises rückt in der Transaktion des Speicherns vor
(``Database.allocate_invoice_number``): Scheitert das Speichern, ist die Nummer nicht verbraucht;
Statuswechsel zählen nie mit (Entwurf ↔ abgeschlossen verbrauchte früher eine Nummer je Wechsel).
Der Vorschlag der Maske (``peek_next_number``) ist genau die Nummer, die das Speichern vergibt – mit
Formatvorlage und Zusatz des Kreises, ohne eine schon belegte Nummer. Eine eigene Nummer hebt den
Zähler nur nach oben. Das Löschen eines Entwurfs gibt die letzte Nummer zurück, eine mittlere lässt
eine Lücke (``release_invoice_number``). Alle Namen und Nummern sind erfundene Testwerte.
"""
import json
import sqlite3
import threading

import pytest

import server.handlers as handlers
from tests.test_invoice_recompute import _make_contacts


def _payload(own_id, cust_id, **over):
    p = {
        'invoiceNumber': '26R100', 'invoiceDate': '2026-06-07', 'deliveryDate': '2026-06-05',
        'customerId': cust_id, 'ownCompanyId': own_id, 'taxRate': 0.19, 'status': 'draft',
        'items': [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung',
                   'unitPrice': 100.0, 'taxRate': 19}],
    }
    p.update(over)
    return p


def _save(payload):
    return json.loads(handlers.handle_invoice_save(json.dumps(payload).encode()))


def _set_status(db, invoice_id, status):
    code, body = handlers.handle_update_invoice_status(
        json.dumps({'invoice_id': invoice_id, 'status': status}).encode())
    return code, json.loads(body)


def _counter(db, letter='R', year=2026):
    return [r[5] for r in db.fetch_number_ranges('invoice') if r[2] == year and r[3] == letter][0]


def _number(db, invoice_id):
    return db.get_invoice_by_id(invoice_id)[1]


@pytest.fixture
def world(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return tmp_db, own, cust


def _new_invoice(world, **over):
    """Neue Rechnung, wie die Maske sie anlegt: der Vorschlag des Nummernkreises, gespeichert als Entwurf."""
    db, own, cust = world
    proposal = db.peek_next_number('invoice', 2026)[0]
    resp = _save(_payload(own, cust, invoiceNumber=proposal, **over))
    assert resp['success'] is True, resp
    return resp['invoice_id'], proposal


# ── das erste Speichern vergibt die Nummer ───────────────────────────────────

def test_two_new_drafts_in_a_row_get_consecutive_numbers(world):
    db = world[0]
    first, first_number = _new_invoice(world)
    assert first_number == '26R001' and _counter(db) == 1
    second, second_number = _new_invoice(world)                    # früher: wieder 26R001 und „bereits vergeben“
    assert second_number == '26R002' and _counter(db) == 2
    assert [_number(db, first), _number(db, second)] == ['26R001', '26R002']


def test_the_proposal_is_the_number_the_save_hands_out(world):
    db = world[0]
    for _ in range(3):
        invoice_id, proposal = _new_invoice(world)
        assert _number(db, invoice_id) == proposal                 # keine Abweichung zwischen Maske und Speichern
        assert db.peek_next_number('invoice', 2026)[0] != proposal


def test_changing_the_status_never_counts(world):
    db, own, cust = world
    invoice_id, _ = _new_invoice(world)
    assert _counter(db) == 1
    for status in ('finalized', 'draft', 'finalized', 'draft', 'finalized'):
        assert _set_status(db, invoice_id, status)[0] == 200
        assert _counter(db) == 1                                   # früher: +1 bei jedem Wechsel nach „abgeschlossen“
    assert _set_status(db, invoice_id, 'sent')[0] == 200           # und Versenden zählt nicht mit
    assert _counter(db) == 1


def test_updating_an_invoice_does_not_touch_the_counter(world):
    db, own, cust = world
    invoice_id, number = _new_invoice(world)
    db.update_number_range(db.fetch_number_ranges('invoice')[0][0], 2026, 'R', '', 20, '', None)
    resp = _save(_payload(own, cust, invoiceNumber=number, invoiceId=invoice_id, deliveryDate='2026-06-06'))
    assert resp['success'] is True and _counter(db) == 20


def test_a_failed_save_does_not_use_the_number(world):
    db, own, cust = world
    bad = _payload(own, cust, invoiceNumber=db.peek_next_number('invoice', 2026)[0])
    bad['items'].append({'position': 2, 'quantity': 1, 'unit': 'C62', 'description': None,
                         'unitPrice': 5.0, 'taxRate': 19})
    assert _save(bad)['success'] is False
    assert _counter(db) == 0
    assert _new_invoice(world)[1] == '26R001'


# ── eigene Nummern ───────────────────────────────────────────────────────────

def test_an_own_number_lifts_the_counter_but_never_lowers_it(world):
    db, own, cust = world
    assert _save(_payload(own, cust, invoiceNumber='26R010'))['success'] is True
    assert _counter(db) == 10
    assert _save(_payload(own, cust, invoiceNumber='26R004'))['success'] is True    # nach unten: nie
    assert _counter(db) == 10
    assert db.peek_next_number('invoice', 2026)[0] == '26R011'


def test_an_own_number_outside_every_template_leaves_the_counters_alone(world):
    db, own, cust = world
    for number in ('RE-17', '26X050', '25R050', '26R5x'):
        assert _save(_payload(own, cust, invoiceNumber=number))['success'] is True
    assert _counter(db) == 0


def test_a_taken_own_number_is_reported_and_leaves_the_counter_alone(world):
    db, own, cust = world
    assert _save(_payload(own, cust, invoiceNumber='26R003'))['success'] is True
    again = _save(_payload(own, cust, invoiceNumber='26R003'))
    assert again == {'success': False, 'error': 'Diese Nummer ist bereits vergeben.'}
    assert _counter(db) == 3


# ── Formatvorlage und Zusatz ─────────────────────────────────────────────────

def test_template_and_suffix_of_the_range_are_used(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', prefix='_A', current_number=6,
                               number_format='{yyyy}-{l}{nn}{s}')
    assert tmp_db.peek_next_number('invoice', 2026)[0] == '2026-R07_A'      # früher: „26R_A007“
    resp = _save(_payload(own, cust, invoiceNumber='2026-R07_A'))
    assert resp['success'] is True and _counter(tmp_db) == 7
    assert tmp_db.peek_next_number('invoice', 2026)[0] == '2026-R08_A'
    # eine eigene Nummer in der Vorlage hebt den Zähler, eine ohne den Zusatz passt nicht
    assert _save(_payload(own, cust, invoiceNumber='2026-R15_A'))['success'] is True
    assert _counter(tmp_db) == 15
    assert _save(_payload(own, cust, invoiceNumber='2026-R30'))['success'] is True
    assert _counter(tmp_db) == 15


def test_the_proposal_skips_numbers_that_are_already_taken(world):
    db, own, cust = world
    db.update_number_range(db.fetch_number_ranges('invoice')[0][0], 2026, 'R', '', 1, '', None)
    _save(_payload(own, cust, invoiceNumber='26R002'))             # eigene Nummer: hebt den Zähler auf 2
    db.update_number_range(db.fetch_number_ranges('invoice')[0][0], 2026, 'R', '', 1, '', None)
    # Zähler (von Hand) zurückgesetzt: 26R002 ist belegt, der Vorschlag nimmt 26R003
    assert db.peek_next_number('invoice', 2026)[0] == '26R003'
    invoice_id, number = _new_invoice(world)
    assert number == '26R003' and _counter(db) == 3


def test_a_quote_number_counts_as_taken_too(world):
    db, own, cust = world
    db.insert_invoice({'invoice_number': '26R001', 'invoice_date': '2026-01-02', 'seller_name': 'S',
                       'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG',
                       'tax_rate': 0.19, 'sum_net': 1, 'tax_amount': 0.19, 'sum_gross': 1.19,
                       'amount_due': 1.19, 'status': 'draft', 'document_type': 'quote'})
    assert db.peek_next_number('invoice', 2026)[0] == '26R002'


# ── Kreis nach dem Jahr des Datums, fehlender Kreis ──────────────────────────

def test_the_range_follows_the_year_of_the_invoice_date(tmp_db):
    tmp_db.insert_number_range('invoice', 2025, 'R', current_number=40)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=2)
    with tmp_db.transaction() as tx:
        assert tmp_db.allocate_invoice_number(tx.conn, '2025-12-30') == '25R041'
        assert tmp_db.allocate_invoice_number(tx.conn, '2026-01-02') == '26R003'
    assert (_counter(tmp_db, year=2025), _counter(tmp_db, year=2026)) == (41, 3)


def test_without_any_range_the_first_number_creates_one(tmp_db):
    with tmp_db.transaction() as tx:
        assert tmp_db.allocate_invoice_number(tx.conn, '2026-03-04') == '26R001'
    assert _counter(tmp_db) == 1


def test_a_new_year_gets_a_range_with_letter_suffix_and_template_of_the_newest(tmp_db):
    tmp_db.insert_number_range('invoice', 2025, 'B', prefix='_X', current_number=9,
                               number_format='{yyyy}/{l}{nnn}{s}')
    with tmp_db.transaction() as tx:
        assert tmp_db.allocate_invoice_number(tx.conn, '2026-01-05') == '2026/B001_X'
    assert _counter(tmp_db, 'B', 2026) == 1 and _counter(tmp_db, 'B', 2025) == 9


def test_a_rolled_back_allocation_leaves_the_counter(tmp_db):
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    with pytest.raises(RuntimeError):
        with tmp_db.transaction() as tx:
            assert tmp_db.allocate_invoice_number(tx.conn, '2026-02-02') == '26R006'
            raise RuntimeError('Speichern scheitert')
    assert _counter(tmp_db) == 5


def test_concurrent_saves_never_hand_out_a_number_twice(tmp_db):
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    numbers, errors = [], []

    def save():
        try:
            with tmp_db.transaction() as tx:
                number = tmp_db.allocate_invoice_number(tx.conn, '2026-05-05')
                tmp_db.insert_invoice({
                    'invoice_number': number, 'invoice_date': '2026-05-05', 'seller_name': 'S',
                    'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG',
                    'tax_rate': 0.19, 'sum_net': 1, 'tax_amount': 0.19, 'sum_gross': 1.19,
                    'amount_due': 1.19, 'status': 'draft'}, tx=tx)
            numbers.append(number)
        except Exception as exc:                      # pragma: no cover - nur bei einem Fehler
            errors.append(exc)

    threads = [threading.Thread(target=save) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(numbers) == [f'26R{n:03d}' for n in range(1, 9)]
    assert _counter(tmp_db) == 8


# ── Angebot → Rechnung ───────────────────────────────────────────────────────

def _quote(db, number='26A001', status='accepted'):
    quote_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-06-09', 'seller_name': 'S',
        'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
        'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0, 'amount_due': 119.0,
        'status': status, 'document_type': 'quote', 'valid_until': '2026-07-09'})
    db.insert_invoice_item({'invoice_id': quote_id, 'position': 1, 'description': 'Position 1',
                            'quantity': 1, 'unit': 'C62', 'price_per_unit': 100.0, 'total_net': 100.0,
                            'tax_rate': 0.19})
    return quote_id


def test_converting_a_quote_takes_the_next_invoice_number(tmp_db):
    import datetime
    year = datetime.date.today().year
    tmp_db.insert_number_range('invoice', year, 'R', current_number=40)
    tmp_db.insert_number_range('quote', year, 'A', current_number=3)
    quote_id = _quote(tmp_db)
    new_id = tmp_db.convert_quote_to_invoice(quote_id)
    assert _number(tmp_db, new_id) == f'{str(year)[2:]}R041'
    assert _counter(tmp_db, year=year) == 41
    assert [r[5] for r in tmp_db.fetch_number_ranges('quote')] == [3]      # der Angebotskreis bleibt
    assert _number(tmp_db, quote_id) == '26A001'


def test_converting_a_quote_after_a_draft_continues_the_sequence(tmp_db, monkeypatch):
    import datetime
    year = datetime.date.today().year
    short = str(year)[2:]
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', year, 'R', current_number=0)
    draft = _save(_payload(own, cust, invoiceNumber=tmp_db.peek_next_number('invoice', year)[0],
                           invoiceDate=f'{year}-06-07'))
    assert _number(tmp_db, draft['invoice_id']) == f'{short}R001'
    new_id = tmp_db.convert_quote_to_invoice(_quote(tmp_db))
    assert _number(tmp_db, new_id) == f'{short}R002'
    assert tmp_db.peek_next_number('invoice', year)[0] == f'{short}R003'


# ── Löschen gibt die letzte Nummer frei ──────────────────────────────────────

def test_deleting_the_last_draft_gives_its_number_back_a_middle_one_leaves_a_gap(world):
    db = world[0]
    ids = [_new_invoice(world)[0] for _ in range(3)]               # 26R001 … 26R003
    db.delete_draft_invoice(ids[1])                                # eine mittlere: Lücke, Zähler bleibt
    assert _counter(db) == 3 and db.peek_next_number('invoice', 2026)[0] == '26R004'
    db.delete_draft_invoice(ids[2])                                # die letzte: der Zähler geht zurück
    assert _counter(db) == 2 and db.peek_next_number('invoice', 2026)[0] == '26R003'
    assert _new_invoice(world)[1] == '26R003'


def test_releasing_a_number_needs_the_counter_to_stand_on_it(tmp_db):
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    con = sqlite3.connect(tmp_db.db_name)
    try:
        con.execute('BEGIN IMMEDIATE')
        assert tmp_db.release_invoice_number(con, '26R004') is False       # darunter: Lücke
        assert tmp_db.release_invoice_number(con, '26R007') is False       # darüber: nichts zu tun
        assert tmp_db.release_invoice_number(con, 'RE-5') is False         # nicht aus dem Kreis
        assert tmp_db.release_invoice_number(con, '26R005') is True
        con.commit()
    finally:
        con.close()
    assert _counter(tmp_db) == 4


# ── Eine Formatvorlage ohne Zähler sperrt nichts ─────────────────────────────

def _break_the_template(db, fmt='{yy}{l}'):
    """Eine Vorlage, wie sie ein Altbestand oder die SQL-Konsole hinterlässt (die Maske lehnt sie ab)."""
    con = sqlite3.connect(db.db_name)
    con.execute("UPDATE NumberRanges SET NumberFormat = ? WHERE Type = 'invoice'", (fmt,))
    con.commit()
    con.close()


@pytest.mark.parametrize('fmt', ['{yy}{l}', '{yy}{l}{nnn}{nn}', '{yyyy}-{s}'])
def test_a_template_without_one_counter_gives_no_proposal_instead_of_an_error(world, fmt):
    db, own, cust = world
    _break_the_template(db, fmt)
    number, range_id = db.peek_next_number('invoice', 2026)
    assert number is None and range_id is not None


def test_the_invoice_page_stays_usable_with_a_template_without_a_counter(world):
    """Früher war nach der ersten Rechnung mit dieser Vorlage (Nummer „26R“) jede neue belegt: das Ansehen
    des Vorschlags lief 10 000 Nummern lang und warf – die Rechnungsseite kam nicht mehr."""
    from server.pages_invoice import PageInvoice
    from tests.dom_helpers import Dom
    db, own, cust = world
    _break_the_template(db, '{yy}{l}')
    assert _save(_payload(own, cust, invoiceNumber='26R'))['success'] is True       # eigene Nummer
    html = PageInvoice(db, {}, None)
    dom = Dom(html)
    assert dom.one('input', id='invoice_number')['value'] == ''
    assert 'Der Nummernkreis liefert keine Nummer' in html and '{nnn}' in html
    assert dom.one('input', id='number_suggestion')['value'] == ''


def test_saving_with_such_a_template_is_refused_in_plain_words(world):
    db, own, cust = world
    _break_the_template(db, '{yy}{l}')
    before = db.fetch_number_ranges('invoice')
    resp = _save(_payload(own, cust, invoiceNumber='26R', numberSuggestion='26R'))
    assert resp['success'] is False
    assert 'Formatvorlage' in resp['error'] and 'Zähler' in resp['error']
    assert 'Traceback' not in json.dumps(resp) and db.fetch_number_ranges('invoice') == before
    assert db.get_invoice_by_id(1) is None


@pytest.mark.parametrize('fmt', ['{yy}{l}', '{yy}{l}{nnn}{nn}', '{n}{n}', 'Rechnung'])
def test_a_template_without_one_counter_is_an_error_in_plain_words(fmt):
    from db.numbering import number_format_error
    assert number_format_error(fmt) and 'genau einen Zähler' in number_format_error(fmt)


@pytest.mark.parametrize('fmt', ['{yy}{l}{nnn}{s}', '{yyyy}-{nn}', 'RE{n}', '{l}/{yy}/{nnn}'])
def test_templates_with_exactly_one_counter_are_accepted(tmp_db, fmt):
    from db.numbering import number_format_error
    assert number_format_error(fmt) is None
    tmp_db.insert_number_range('invoice', 2026, 'R', number_format=fmt)
    assert len(tmp_db.fetch_number_ranges('invoice')) == 1


def test_the_masterdata_form_refuses_a_template_without_a_counter(tmp_db):
    from urllib.parse import parse_qs, urlsplit
    form = {'type': ['invoice'], 'year': ['2026'], 'letter': ['R'], 'prefix': [''],
            'current_number': ['0'], 'description': [''], 'number_format': ['{yy}{l}']}
    code, location = handlers.handle_add_number_range(tmp_db, form)
    assert code == 303 and location.startswith('/masterdata/numberranges?err=')
    assert 'genau einen Zähler' in parse_qs(urlsplit(location).query)['err'][0]
    assert tmp_db.fetch_number_ranges('invoice') == []
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=3)
    range_id = tmp_db.fetch_number_ranges('invoice')[0][0]
    code, location = handlers.handle_update_number_range(tmp_db, {**form, 'id': [str(range_id)]})
    assert code == 303 and location.startswith(f'/masterdata/numberranges/edit?id={range_id}&err=')
    assert tmp_db.fetch_number_ranges('invoice')[0][7] == '{yy}{l}{nnn}{s}'             # nichts überschrieben
    ok = {**form, 'id': [str(range_id)], 'number_format': ['{yy}{l}{nn}']}
    assert handlers.handle_update_number_range(tmp_db, ok) == (303, '/masterdata/numberranges')
    assert tmp_db.fetch_number_ranges('invoice')[0][7] == '{yy}{l}{nn}'


def test_the_number_range_page_shows_the_refusal(tmp_db):
    from server.pages_masterdata import PageNumberRanges
    from tests.dom_helpers import app_msg_arguments, Dom
    html = PageNumberRanges(tmp_db, err='Die Formatvorlage braucht genau einen Zähler: {n}.')
    assert app_msg_arguments(Dom(html)) == ['Die Formatvorlage braucht genau einen Zähler: {n}.', 'error']
    assert 'function(){appMsg(' not in PageNumberRanges(tmp_db)             # ohne Fehler keine Meldung


# ── Ein veralteter Vorschlag zählt nicht als eigene Nummer ──────────────────────

def test_an_unchanged_stale_suggestion_gets_a_fresh_number(world):
    """Zwei Fenster haben denselben Vorschlag 26R001: Das zweite Speichern bekommt 26R002 statt „bereits vergeben“."""
    db, own, cust = world
    proposal = db.peek_next_number('invoice', 2026)[0]
    first = _save(_payload(own, cust, invoiceNumber=proposal, numberSuggestion=proposal))
    second = _save(_payload(own, cust, invoiceNumber=proposal, numberSuggestion=proposal))
    assert (first['success'], first['invoice_number']) == (True, '26R001')
    assert (second['success'], second['invoice_number']) == (True, '26R002')      # die Nummer, die gilt
    assert _number(db, second['invoice_id']) == '26R002' and _counter(db) == 2


def test_a_number_the_user_typed_stays_an_own_number(world):
    db, own, cust = world
    proposal = db.peek_next_number('invoice', 2026)[0]
    own_number = _save(_payload(own, cust, invoiceNumber='26R050', numberSuggestion=proposal))
    assert (own_number['invoice_number'], _counter(db)) == ('26R050', 50)
    taken = _save(_payload(own, cust, invoiceNumber='26R050', numberSuggestion=proposal))
    assert taken == {'success': False, 'error': 'Diese Nummer ist bereits vergeben.'}   # eine eigene Nummer bleibt eigen


def test_without_a_suggestion_the_number_is_an_own_number_as_before(world):
    db, own, cust = world
    assert _save(_payload(own, cust, invoiceNumber='26R001'))['invoice_number'] == '26R001'
    assert _save(_payload(own, cust, invoiceNumber='26R001')) == {
        'success': False, 'error': 'Diese Nummer ist bereits vergeben.'}


def test_the_response_names_the_number_for_a_new_invoice_only(world):
    db, own, cust = world
    created = _save(_payload(own, cust, invoiceNumber='26R001'))
    updated = _save(_payload(own, cust, invoiceNumber='26R001', invoiceId=created['invoice_id']))
    assert 'invoice_number' in created and 'invoice_number' not in updated


def test_two_windows_with_twenty_saves_each_hand_out_forty_distinct_numbers(world):
    """Beide Fenster nehmen den Vorschlag, den sie gerade sehen – meist schon verbraucht: Keine Nummer
    doppelt, keine abgelehnt, keine Lücke."""
    db, own, cust = world
    results, errors = [], []

    def window():
        try:
            for _ in range(20):
                suggestion = db.peek_next_number('invoice', 2026)[0]
                results.append(_save(_payload(own, cust, invoiceNumber=suggestion, numberSuggestion=suggestion)))
        except Exception as exc:                      # pragma: no cover - nur bei einem Fehler
            errors.append(exc)

    threads = [threading.Thread(target=window) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and all(r['success'] for r in results), [r for r in results if not r['success']]
    numbers = sorted(r['invoice_number'] for r in results)
    assert numbers == [f'26R{n:03d}' for n in range(1, 41)]
    assert _counter(db) == 40


# ── Jahreswechsel ────────────────────────────────────────────────────────────

def test_an_invoice_of_next_year_takes_next_years_number_although_the_form_suggested_this_years(world):
    """Rechnung vom 2027-01-04 bei vorhandenem Kreis 2026, Vorschlag unverändert → 27R001, der Kreis 2026 bleibt."""
    db, own, cust = world
    db.update_number_range(db.fetch_number_ranges('invoice')[0][0], 2026, 'R', current_number=5)
    proposal = db.peek_next_number('invoice', 2026)[0]
    assert proposal == '26R006'
    resp = _save(_payload(own, cust, invoiceNumber=proposal, numberSuggestion=proposal,
                          invoiceDate='2027-01-04', deliveryDate='2027-01-04'))
    assert (resp['success'], resp['invoice_number']) == (True, '27R001')
    assert (_counter(db, year=2026), _counter(db, year=2027)) == (5, 1)
    assert _number(db, resp['invoice_id']) == '27R001'


def test_an_invoice_of_the_previous_year_continues_that_years_range(tmp_db, monkeypatch):
    from tests.test_invoice_recompute import _make_contacts
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2025, 'R', current_number=40)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=2)
    resp = _save(_payload(own, cust, invoiceNumber='26R003', numberSuggestion='26R003',
                          invoiceDate='2025-12-30', deliveryDate='2025-12-30'))
    assert (resp['success'], resp['invoice_number']) == (True, '25R041')
    assert (_counter(tmp_db, year=2025), _counter(tmp_db, year=2026)) == (41, 2)


def test_an_own_number_for_another_year_is_not_replaced(world):
    db, own, cust = world
    resp = _save(_payload(own, cust, invoiceNumber='27R777', numberSuggestion='26R001',
                          invoiceDate='2027-01-04', deliveryDate='2027-01-04'))
    assert (resp['success'], resp['invoice_number']) == (True, '27R777')


# ── Kein RETURNING ───────────────────────────────────────────────────────────

def test_the_counters_are_incremented_without_returning():
    """``UPDATE … RETURNING`` kennt SQLite erst seit 3.35; ältere Versionen in verbreiteten Distributionen
    würden die Nummernvergabe mit einem Syntaxfehler beenden."""
    import inspect
    import db.numbering as numbering
    assert 'RETURNING' not in inspect.getsource(numbering).replace('kein RETURNING', '').replace(
        'ohne RETURNING', '')


def test_get_next_number_still_counts_up_and_creates_a_range(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'B', current_number=4)
    assert tmp_db.get_next_number('receipt_company', 2026, 'B') == '26B005'
    assert tmp_db.get_next_number('receipt_company', 2026, 'B') == '26B006'
    assert tmp_db.get_next_number('receipt_company', 2026, 'C') == '26C001'            # neuer Kreis
