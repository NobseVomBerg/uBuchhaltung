# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belegerkennung, Paket P5b (2.6.0): Vorschläge zu einem gelesenen Beleg zeigen – ``GET /receipts/suggestions?id=`` und der Bereich
„Vorschläge“ auf der Seite eines Belegs (``/receipts/edit?id=``). Vorgeschlagen wird nur; verknüpft wird allein auf Klick, über den
bestehenden Weg (``POST /bookings/link-document``).

* Die Route (``app._SEARCH_PATHS``, wie die Suchen der Belege und Buchungen): GET, rein lesend, nur von den eigenen Seiten (sonst 403), nie
  aus einem Cache, die 503 während einer Wiederherstellung als JSON (``test_dirlock.py``), die ID über ``_id_param`` (fehlt sie oder ist sie
  ungültig, antwortet die Route wie auf jede unbekannte ID: 404 mit festem Satz). Antwort: ``{"success": true, "suggestions": […],
  "empty": "…"}``, je Vorschlag ``booking`` (dieselbe Form wie ein Suchtreffer derselben Buchung), ``reasons`` (feste Sätze) und ``linked`` –
  in der Rangfolge von ``db/receipt_matching.py`` (die Zahlungsrichtung nach der Belegart: eine Rechnung erwartet einen Ausgang, eine
  Gutschrift einen Eingang; die falsche Richtung steht hinter der richtigen, mit eigener Begründung). ``empty`` ist der Satz für die leere
  Liste – derselbe wie auf der Seite des Belegs. **Hängt der Beleg an einer Buchung, gibt es keine Vorschläge** (die Liste ist leer, der Satz
  nennt es); nach dem Lösen der letzten Verknüpfung kommen sie wieder. Nie ein Pfad, ein Text des Belegs oder ein Ausnahmetext; ein
  unerwarteter Fehler ist ein fester Satz (500). Der Diagnose-MCP kennt die Route nicht (``test_mcp_ubuchhaltung.py``).
* Die Seite eines Belegs: Den Bereich gibt es nur zu einer Lesung mit Status ``ok``; die Vorschläge stehen als JSON im Skriptblock (nur über
  ``script_json``), in derselben Form wie die Route; die festen Sätze; der Kasten „Gelesen“ zeigt neben der Summenprobe die Satzprobe.
* Das Skript der Seite (Node-Läufe unten; ohne Node entfallen nur diese): Zeilen wie Suchtreffer, die Begründungen klein darunter, „Zuordnen“
  ruft den bestehenden Weg, danach – und nach dem Lösen – werden die Vorschläge neu geholt (die Treffer der Suche nur, wenn schon gesucht
  wurde: wer aus einem Vorschlag zuordnet, bekommt keine Trefferliste); ohne den Bereich wird nichts gezeichnet und nichts geholt; alles
  Fremde nur als Text.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; Daten stehen fest im Test (kein „heute“).
"""
import json
import os
import re
import sqlite3
from types import SimpleNamespace

import pytest

import receipt_reading
import userctx
from db import Database
from db.document_readings import READING_COLUMNS
from server import app as server_app
from server import handlers, receipt_files, receipt_links
from server.pages_receipts import (
    LINKED_TEXT, NO_AMOUNT_TEXT, NO_SUGGESTIONS_TEXT, RECEIPT_LINK_JS, SUGGESTIONS_HINT, PageReceiptEdit, suggestions_empty_text)
from tests import installation
from tests.document_helpers import post_json
from tests.dom_helpers import Dom, assert_same_structure
from tests.http_client import FOREIGN_ORIGIN, get, own_origin
from tests.test_receipt_links import NO_NODE, answer, rows_of, run_page_script, script_data, script_of, walk
from tests.test_receipt_upload import demo_site

DAY = '2026-03-04'                          # das Belegdatum der Tests
GROSS = 1190000                             # 119,00 EUR in Minor Units (money.SCALE = 4)
FILE = 'Belege/2026/B-1_beleg.pdf'
NOT_FOUND = {'success': False, 'error': 'Diesen Beleg gibt es nicht.'}
SECRET = '/srv/geheim/buch.db sqlite3 Traceback'
SECRET_TEXT = 'GEHEIMER-TEXT-DER-DATEI Rechnung 4711'
IMG = '<img src=x onerror=1>'
HOSTILE = '</script><img src=x onerror=1>'
BOOKING_KEYS = {'id', 'date', 'amount', 'currency', 'recipient', 'text', 'documentNumber', 'kind', 'kindLabel'}


# ── Hilfen ───────────────────────────────────────────────────────────────────
def reading(**over):
    """Eine Lesung (Schlüssel = Spalten von ``DocumentReadings``) über 119,00 EUR; ``over`` überschreibt Felder."""
    row = {'Status': 'ok', 'Source': 'text', 'ReaderVersion': receipt_reading.READER_VERSION, 'ReadAt': '2026-03-05 10:00:00',
           'DocDate': DAY, 'GrossMinor': GROSS, 'NetMinor': None, 'TaxMinor': None, 'TaxRate': None, 'Currency': 'EUR',
           'SenderName': None, 'SenderVatId': None, 'InvoiceNumber': None, 'Kind': 'invoice', 'Raw': None, 'Text': None}
    row.update(over)
    return row


def add_receipt(db, number='B-1', date=DAY, path=FILE, filename='beleg.pdf', info=''):
    """Einen Beleg anlegen; liefert seine ID."""
    db.insert_receipt(number, date, filename, path, info)
    return db.fetch_receipts()[-1][0]


def save_reading(db, receipt, **over):
    assert db.save_document_reading(receipt, FILE, reading(**over))


def add_booking(db, date_booking=DAY, amount='-119.00', **fields):
    """Eine Buchung (ein eigenständiger Satz, wenn nicht anders genannt); liefert ihre ID."""
    return db.insert_booking(date_booking, amount, **fields)


def add_bank(db, date_booking=DAY, amount='-119.00', **fields):
    return db.insert_booking(date_booking, amount, booking_type='bank', **fields)


def a_world(db):
    """Ein Beleg vom 04.03.2026 über 119,00 EUR mit Lesung (eine Rechnung: erwartet wird ein Ausgang) und fünf Buchungen: Bank mit
    Rechnungsnummer und Namen, ein Satz am Folgetag, einer am Rand des Fensters, zwei außerhalb (Betrag, Datum). Liefert ``(Beleg, [IDs
    in der erwarteten Reihenfolge])``."""
    receipt = add_receipt(db)
    save_reading(db, receipt, SenderName='Lieferant Probe GmbH', InvoiceNumber='RE-2026-17')
    bank = add_bank(db, date_booking='2026-03-10', recipient_client='Lieferant Probe GmbH', text='Zahlung RE-2026-17')
    entry = add_booking(db, date_booking='2026-03-05', amount='-119.00', text='Sonstiges')
    edge = add_booking(db, date_booking='2026-02-25', text='Rand')
    add_booking(db, date_booking='2026-03-05', amount='-118.00', text='falscher Betrag')
    add_bank(db, date_booking='2026-07-01', text='zu spät')
    add_bank(db, date_booking='2026-02-24', text='zu früh')
    return receipt, [bank, entry, edge]


def ask(port, receipt, headers=None, extra=''):
    return get(port, f'/receipts/suggestions?id={receipt}{extra}', headers)


def listed(response):
    """Die Vorschläge einer gelungenen Antwort (dazu immer ``empty``: der Satz für die leere Liste, siehe ``sentence``)."""
    assert response.status == 200 and response.headers['Content-Type'] == 'application/json', response.text
    data = json.loads(response.text)
    assert data['success'] is True and set(data) == {'success', 'suggestions', 'empty'}
    assert data['empty'] in (LINKED_TEXT, NO_AMOUNT_TEXT, NO_SUGGESTIONS_TEXT)
    return data['suggestions']


def sentence(response):
    """Der Satz für die leere Liste (``empty``) einer gelungenen Antwort."""
    assert response.status == 200
    return json.loads(response.text)['empty']


def ids(found):
    return [item['booking']['id'] for item in found]


def edit_page(port, receipt):
    page = get(port, f'/receipts/edit?id={receipt}')
    assert page.status == 200
    return page.text


def test_the_reading_of_the_tests_has_exactly_the_columns_of_the_table():
    assert set(reading()) == set(READING_COLUMNS)


# ── Die Route: Aufbau, Einordnung ────────────────────────────────────────────
def test_the_route_is_a_search_route_and_not_in_the_diagnosis_server():
    path = '/receipts/suggestions'
    assert path in server_app._SEARCH_PATHS and path in server_app._JSON_ANSWER_ROUTES      # auch die 503 während einer Wiederherstellung ist JSON
    for other in (server_app._JSON_PATHS, server_app._POST_ONLY_JSON_PATHS, server_app._ACTION_PATHS, server_app._GENERATE_PATHS,
                  server_app._UPLOAD_PATHS):
        assert path not in other                                                              # nichts davon ist ein POST
    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'mcp_ubuchhaltung.py')
    with open(tool, encoding='utf-8') as handle:
        assert path not in handle.read()                                                      # die Positivliste des Diagnose-MCP bleibt, wie sie war


def test_the_fixed_sentences_are_the_planned_ones():
    assert SUGGESTIONS_HINT == 'Vorschläge beruhen auf der Lesung der Datei: Betrag, Rechnungsnummer, Name und Datum. Bitte prüfen.'
    assert NO_SUGGESTIONS_TEXT == 'Keine passende Buchung gefunden.'
    assert NO_AMOUNT_TEXT == 'Ohne gelesenen Betrag gibt es keine Vorschläge.'
    assert LINKED_TEXT == 'Der Beleg ist bereits einer Buchung zugeordnet.'
    assert receipt_files.NOT_FOUND == NOT_FOUND['error']
    assert receipt_links.SUGGEST_FAILED == 'Die Vorschläge konnten nicht gelesen werden – Einzelheiten stehen im Server-Log.'
    assert receipt_links.SUGGEST_FOREIGN == 'Die Vorschläge sind nur über die Seiten der App erreichbar.'


# ── Die Route: Vorschläge ────────────────────────────────────────────────────
def test_the_suggestions_come_in_the_ranking_with_their_reasons(server_port, auto_login):
    receipt, (bank, entry, edge) = a_world(auto_login.db)
    found = listed(ask(server_port, receipt))
    # die Bankbewegung mit Rechnungsnummer und Namen kommt vor dem Satz, der nur den Betrag trägt – obwohl er einen Tag näher am Belegdatum liegt
    assert ids(found) == [bank, entry, edge]
    assert [item['reasons'] for item in found] == [
        ['Betrag stimmt', 'Rechnungsnummer im Buchungstext', 'Name passt', '6 Tage nach dem Belegdatum'],
        ['Betrag stimmt', '1 Tag nach dem Belegdatum'],
        ['Betrag stimmt', '7 Tage vor dem Belegdatum']]
    assert [item['linked'] for item in found] == [False, False, False]
    assert all(set(item) == {'booking', 'reasons', 'linked'} for item in found)


def test_a_booking_has_the_form_of_a_search_hit_of_the_same_booking(server_port, auto_login):
    receipt, _expected = a_world(auto_login.db)
    found = listed(ask(server_port, receipt))
    hits = {hit['id']: hit for hit in json.loads(get(server_port, '/transactions/search?q=').text)['results']}
    assert len(found) == 3 and len(hits) >= 6
    for item in found:
        assert item['booking'] == hits[item['booking']['id']]                  # Zeile für Zeile dasselbe Objekt wie in der Suche
    assert set(found[0]['booking']) == BOOKING_KEYS
    assert (found[0]['booking']['kind'], found[0]['booking']['kindLabel'], found[0]['booking']['amount']) == ('bank', 'Bankbewegung', '-119,00')
    assert found[0]['booking']['recipient'] == 'Lieferant Probe GmbH' and found[0]['booking']['text'] == 'Zahlung RE-2026-17'


def test_a_booking_that_already_carries_another_receipt_comes_last_and_is_marked(server_port, auto_login):
    db = auto_login.db
    receipt, (bank, entry, edge) = a_world(db)
    other = add_receipt(db, 'B-2', path='Belege/2026/B-2_beleg.pdf')
    db.link_receipt_to_booking(bank, other)                                    # der beste Treffer trägt schon einen anderen Beleg
    found = listed(ask(server_port, receipt))
    assert ids(found) == [entry, edge, bank]
    assert [item['linked'] for item in found] == [False, False, True]
    assert found[-1]['reasons'][-1] == 'bereits mit einem Beleg verknüpft'


def test_at_most_five_suggestions_come_back(server_port, auto_login):
    db = auto_login.db
    receipt = add_receipt(db)
    save_reading(db, receipt)
    booked = [add_booking(db, date_booking=f'2026-03-{4 + number:02d}') for number in range(1, 9)]
    assert ids(listed(ask(server_port, receipt))) == booked[:5]


def test_a_wrong_direction_for_the_kind_comes_after_the_right_one_with_its_reason(server_port, auto_login):
    db = auto_login.db
    receipt = add_receipt(db)
    save_reading(db, receipt)                                                  # eine Rechnung: erwartet wird ein Ausgang
    income = add_booking(db, date_booking=DAY, amount='119.00', text='Einnahme')                      # am Belegdatum, aber falsche Richtung
    expense = add_booking(db, date_booking='2026-03-24', text='Ausgabe')                              # drei Wochen später, richtige Richtung
    found = listed(ask(server_port, receipt))
    assert ids(found) == [expense, income] and [item['reasons'] for item in found] == [
        ['Betrag stimmt', '20 Tage nach dem Belegdatum'],
        ['Betrag stimmt', 'am Belegdatum', 'Zahlungsrichtung passt nicht zur Belegart']]
    assert all(set(item) == {'booking', 'reasons', 'linked'} for item in found)        # die JSON-Form bleibt: kein neues Feld
    credit = add_receipt(db, 'B-2')
    save_reading(db, credit, Kind='credit_note')                               # eine Gutschrift: erwartet wird ein Eingang
    assert ids(listed(ask(server_port, credit))) == [income, expense]
    assert listed(ask(server_port, credit))[1]['reasons'][-1] == 'Zahlungsrichtung passt nicht zur Belegart'


def test_a_receipt_that_was_linked_gets_no_suggestions_and_the_sentence_and_gets_them_back_after_unlinking(server_port, auto_login):
    db = auto_login.db
    receipt, (bank, entry, edge) = a_world(db)
    assert ids(listed(ask(server_port, receipt))) == [bank, entry, edge] and sentence(ask(server_port, receipt)) == NO_SUGGESTIONS_TEXT
    linked = post_json(server_port, '/bookings/link-document', {'bookingId': bank, 'documentId': receipt}, auto_login.csrf)
    assert linked.status == 200 and json.loads(linked.text)['created'] is True
    resp = ask(server_port, receipt)
    assert (resp.status, json.loads(resp.text)) == (200, {'success': True, 'suggestions': [], 'empty': LINKED_TEXT})   # keine Vorschläge mehr
    other = post_json(server_port, '/bookings/link-document', {'bookingId': entry, 'documentId': receipt}, auto_login.csrf)
    assert other.status == 200 and ask(server_port, receipt).text == resp.text                         # noch eine Verknüpfung: dieselbe Antwort
    unlinked = post_json(server_port, '/bookings/unlink-document', {'bookingId': bank, 'documentId': receipt}, auto_login.csrf)
    assert unlinked.status == 200 and json.loads(unlinked.text)['removed'] is True
    assert ask(server_port, receipt).text == resp.text                         # eine bleibt: weiter keine Vorschläge
    unlinked = post_json(server_port, '/bookings/unlink-document', {'bookingId': entry, 'documentId': receipt}, auto_login.csrf)
    assert unlinked.status == 200 and json.loads(unlinked.text)['removed'] is True
    assert ids(listed(ask(server_port, receipt))) == [bank, entry, edge]       # die letzte gelöst: die Vorschläge sind wieder da


@pytest.mark.parametrize('over, sentence_expected', [
    pytest.param(None, NO_AMOUNT_TEXT, id='keine-Lesung'),
    pytest.param({'Status': 'empty'}, NO_SUGGESTIONS_TEXT, id='nichts-gefunden'),
    pytest.param({'Status': 'no_text'}, NO_SUGGESTIONS_TEXT, id='kein-Text'),
    pytest.param({'Status': 'failed'}, NO_SUGGESTIONS_TEXT, id='fehlgeschlagen'),
    pytest.param({'GrossMinor': None}, NO_AMOUNT_TEXT, id='ohne-Brutto'),
    pytest.param({'GrossMinor': 0}, NO_SUGGESTIONS_TEXT, id='Brutto-null'),
    pytest.param({'Currency': 'USD'}, NO_SUGGESTIONS_TEXT, id='fremde-Waehrung'),
])
def test_without_a_usable_reading_the_list_is_empty(server_port, auto_login, over, sentence_expected):
    db = auto_login.db
    receipt = add_receipt(db)
    add_booking(db)
    add_bank(db)
    if over is not None:
        save_reading(db, receipt, **over)
    resp = ask(server_port, receipt)
    assert (resp.status, json.loads(resp.text)) == (200, {'success': True, 'suggestions': [], 'empty': sentence_expected})


def test_the_sentence_for_the_empty_list_is_the_one_of_the_page_in_every_state(server_port, auto_login):
    """Seite und Route nennen denselben Satz: Es entscheidet dieselbe Funktion (``suggestions_empty_text``)."""
    db = auto_login.db
    plain = add_receipt(db, 'B-1')
    save_reading(db, plain)
    no_amount = add_receipt(db, 'B-2')
    save_reading(db, no_amount, GrossMinor=None)
    linked = add_receipt(db, 'B-3')
    save_reading(db, linked)
    db.link_receipt_to_booking(add_booking(db, amount='-5.00'), linked)
    linked_no_amount = add_receipt(db, 'B-4')
    save_reading(db, linked_no_amount, GrossMinor=None)
    db.link_receipt_to_booking(add_booking(db, amount='-6.00'), linked_no_amount)
    expected = {plain: NO_SUGGESTIONS_TEXT, no_amount: NO_AMOUNT_TEXT, linked: LINKED_TEXT, linked_no_amount: LINKED_TEXT}
    for receipt, text in expected.items():
        assert sentence(ask(server_port, receipt)) == text, receipt
        assert script_data(edit_page(server_port, receipt), 'RECEIPT_SUGGEST') == {'list': [], 'empty': text}, receipt


@pytest.mark.parametrize('linked, reading_row, text', [
    (True, None, LINKED_TEXT), (True, reading(), LINKED_TEXT), (True, reading(GrossMinor=None), LINKED_TEXT),
    (False, None, NO_AMOUNT_TEXT), (False, 'text', NO_AMOUNT_TEXT), (False, reading(GrossMinor=None), NO_AMOUNT_TEXT),
    (False, reading(GrossMinor='119'), NO_AMOUNT_TEXT), (False, reading(), NO_SUGGESTIONS_TEXT),
    (False, reading(GrossMinor=-GROSS), NO_SUGGESTIONS_TEXT)])
def test_the_sentence_for_the_empty_list(linked, reading_row, text):
    assert suggestions_empty_text(linked, reading_row) == text


# ── Die Route: unbekannter Beleg, unbrauchbare ID ────────────────────────────
def test_an_unknown_receipt_is_404_with_the_fixed_sentence(server_port, auto_login):
    resp = ask(server_port, 999)
    assert (resp.status, json.loads(resp.text)) == (404, NOT_FOUND)
    assert resp.headers['Content-Type'] == 'application/json'


@pytest.mark.parametrize('query', ['', '?id=', '?id=abc', '?id=1.5', '?id=0', '?id=-1', f'?id={2 ** 63}', f'?id={"9" * 30}', '?id=%00',
                                   '?other=1'])
def test_a_missing_or_unusable_id_answers_like_an_unknown_one(server_port, auto_login, query):
    """``_id_param``: Fehlt die ID oder ist sie keine Ganzzahl in 64 Bit, gilt ``-1`` – nie ein 500 und nie ein Rückfall auf einen vorhandenen Beleg
    (es gibt hier einen mit der ID 1)."""
    receipt, _expected = a_world(auto_login.db)
    assert receipt == 1
    resp = get(server_port, '/receipts/suggestions' + query)
    assert (resp.status, json.loads(resp.text)) == (404, NOT_FOUND)
    assert resp.headers['Content-Type'] == 'application/json'


# ── Die Route: Herkunft, Cache ───────────────────────────────────────────────
@pytest.mark.parametrize('headers', [
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, id='cross-site'),
    pytest.param({'Sec-Fetch-Site': 'same-site'}, id='same-site'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, id='fremder-Origin'),
])
def test_a_request_from_a_foreign_page_is_403_and_names_no_data(server_port, auto_login, headers):
    receipt, _expected = a_world(auto_login.db)
    resp = ask(server_port, receipt, headers)
    assert (resp.status, json.loads(resp.text)) == (403, {'success': False, 'error': receipt_links.SUGGEST_FOREIGN})
    assert resp.headers['Content-Type'] == 'application/json'
    for text in ('Zahlung', 'RE-2026-17', 'Lieferant', 'suggestions'):
        assert text not in resp.text, text


def test_the_own_page_may_ask(server_port, auto_login):
    receipt, (bank, entry, edge) = a_world(auto_login.db)
    for headers in ({'Sec-Fetch-Site': 'same-origin'}, {'Sec-Fetch-Site': 'none'}, {'Origin': own_origin(server_port)}, {}):
        assert ids(listed(ask(server_port, receipt, headers))) == [bank, entry, edge], headers


def test_the_answers_are_never_cached_even_when_the_address_ends_like_a_static_file(server_port, auto_login):
    receipt, _expected = a_world(auto_login.db)
    answers = [ask(server_port, receipt), ask(server_port, 999), ask(server_port, receipt, {'Origin': FOREIGN_ORIGIN}),
               get(server_port, f'/receipts/suggestions?id={receipt}&x=.png'), get(server_port, f'/receipts/suggestions?id={receipt}&x=.css'),
               get(server_port, '/receipts/suggestions?id=abc')]
    assert [resp.status for resp in answers] == [200, 404, 403, 200, 200, 404]
    for resp in answers:
        assert resp.headers['Cache-Control'] == 'no-store'


# ── Die Route: rein lesend, nie Pfad oder Text ───────────────────────────────
def test_the_route_writes_nothing_and_only_selects(server_port, auto_login, monkeypatch):
    db = auto_login.db
    receipt, _expected = a_world(db)
    before = (installation.dump(db.db_name), installation.tree(os.path.dirname(db.db_name)))
    statements = []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    found = listed(ask(server_port, receipt))
    listed(ask(server_port, receipt))
    assert ask(server_port, 999).status == 404
    monkeypatch.setattr(Database, '_get_connection', real)
    assert found and statements and all(statement.lstrip().upper().startswith('SELECT') for statement in statements), statements
    assert (installation.dump(db.db_name), installation.tree(os.path.dirname(db.db_name))) == before       # kein Eintrag, keine Datei


def test_the_answer_never_names_a_path_a_file_name_the_info_or_the_text_of_the_file(server_port, auto_login):
    db = auto_login.db
    receipt = add_receipt(db, number='26B001', filename='geheime-ablage.pdf', info='GEHEIME INFO')
    raw_text = json.dumps({'fields': {'gross_minor': {'value': GROSS, 'source': 'rule', 'quote': SECRET_TEXT, 'page': 1}},
                           'checks': {'sum': 'ok'}, 'notes': ['by_checksum']})
    save_reading(db, receipt, Text=SECRET_TEXT, Raw=raw_text, SenderName='Lieferant Probe GmbH', InvoiceNumber='RE-2026-17')
    add_bank(db, recipient_client='Lieferant Probe GmbH', text='Zahlung RE-2026-17')
    resp = ask(server_port, receipt)
    assert len(listed(resp)) == 1
    for text in (SECRET_TEXT, 'GEHEIMER', 'geheime-ablage', 'GEHEIME INFO', 'Belege', 'B-1_beleg', '26B001', userctx.DATA_ROOT, 'Traceback',
                 'Path', 'Filename', 'fields', 'quote', 'by_checksum', 'Source'):
        assert text not in resp.text, text
    assert '"text"' in resp.text and '"Text"' not in resp.text                  # nur der Buchungstext der Buchung, nie ein Text des Belegs


def test_in_demo_mode_the_route_answers_like_everywhere_and_without_a_reading_the_list_is_empty(server_port, monkeypatch):
    demo_site(monkeypatch)
    db = installation.open_db('demo')
    receipt = add_receipt(db)
    booking = add_booking(db)
    resp = ask(server_port, receipt)
    assert (resp.status, json.loads(resp.text)) == (200, {'success': True, 'suggestions': [], 'empty': NO_AMOUNT_TEXT})
    save_reading(db, receipt)
    assert ids(listed(ask(server_port, receipt))) == [booking]
    assert ask(server_port, 999).status == 404


@pytest.mark.parametrize('broken', ['suggest_bookings_for_receipt', 'get_receipt_by_id'])
def test_an_unexpected_error_is_a_fixed_sentence_and_its_text_stays_in_the_server_log(server_port, auto_login, monkeypatch, capsys, broken):
    receipt, (bank, entry, edge) = a_world(auto_login.db)
    real = getattr(Database, broken)

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(Database, broken, boom)
    resp = ask(server_port, receipt)
    assert (resp.status, json.loads(resp.text)) == (500, {'success': False, 'error': receipt_links.SUGGEST_FAILED})
    assert resp.headers['Content-Type'] == 'application/json'
    for text in ('geheim', '/srv', 'sqlite3', 'RuntimeError', 'Traceback'):
        assert text not in resp.text, text
    assert SECRET in capsys.readouterr().err                                   # der volle Fehler steht im Server-Log
    monkeypatch.setattr(Database, broken, real)
    assert ids(listed(ask(server_port, receipt))) == [bank, entry, edge]       # der Server antwortet weiter


# ── Die Seite eines Belegs: der Bereich ──────────────────────────────────────
@pytest.mark.parametrize('over', [
    pytest.param(None, id='keine-Lesung'),
    pytest.param({'Status': 'empty'}, id='nichts-gefunden'),
    pytest.param({'Status': 'no_text'}, id='kein-Text'),
    pytest.param({'Status': 'unsupported'}, id='Dateityp'),
    pytest.param({'Status': 'too_large'}, id='zu-gross'),
    pytest.param({'Status': 'failed'}, id='fehlgeschlagen'),
])
def test_without_a_reading_with_status_ok_there_is_no_area_at_all(tmp_db, over):
    receipt = add_receipt(tmp_db)
    add_bank(tmp_db)
    if over is not None:
        save_reading(tmp_db, receipt, **over)
    html = PageReceiptEdit(tmp_db, receipt)
    assert [tag for tag in Dom(html).all('div') if tag.get('id') in ('receiptSuggestions', 'receiptSuggestionList')] == []
    assert '<h3>Vorschläge</h3>' not in html and SUGGESTIONS_HINT not in html and NO_SUGGESTIONS_TEXT not in html
    assert script_data(html, 'RECEIPT_SUGGEST') is None                         # das Skript weiß es: nichts zu zeichnen, nichts zu holen


def test_with_a_reading_ok_the_area_sits_between_the_linked_bookings_and_the_search(tmp_db):
    receipt, (bank, entry, edge) = a_world(tmp_db)
    html = PageReceiptEdit(tmp_db, receipt)
    dom = Dom(html)
    assert dom.one('div', id='receiptSuggestions') == {'id': 'receiptSuggestions'}
    assert dom.one('div', id='receiptSuggestionList') == {'id': 'receiptSuggestionList'}
    assert html.index('id="receiptLinks"') < html.index('id="receiptSuggestions"') < html.index('<h3>Buchung suchen</h3>')
    start = html.index('id="receiptSuggestions"')
    markup = html[start:html.index('<h3>Buchung suchen</h3>', start)]
    assert '<h3>Vorschläge</h3>' in markup and 'id="receiptSuggestionList"' in markup
    assert markup.index('<h3>Vorschläge</h3>') < markup.index('id="receiptSuggestionList"') < markup.index(f'<p class="hint">{SUGGESTIONS_HINT}</p>')
    data = script_data(html, 'RECEIPT_SUGGEST')
    assert set(data) == {'list', 'empty'} and ids(data['list']) == [bank, entry, edge] and data['empty'] == NO_SUGGESTIONS_TEXT


def test_the_data_of_the_page_have_the_form_of_the_route(server_port, auto_login):
    receipt, _expected = a_world(auto_login.db)
    page = edit_page(server_port, receipt)
    assert script_data(page, 'RECEIPT_SUGGEST')['list'] == listed(ask(server_port, receipt))
    assert script_data(page, 'RECEIPT_LINK_ID') == receipt


def test_a_failing_suggestion_query_leaves_the_page_of_the_receipt_standing(server_port, auto_login, monkeypatch, capsys):
    """Die Seite des Belegs hängt nicht an den Vorschlägen (``_suggestions_area`` fängt jeden Fehler ab): Wirft ihre Abfrage, antwortet
    ``/receipts/edit`` trotzdem mit 200, der Bereich „Vorschläge“ steht da, die Liste ist leer – und der Ausnahmetext steht nur im Server-Log."""
    receipt, (bank, entry, edge) = a_world(auto_login.db)
    real = Database.suggest_bookings_for_receipt

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(Database, 'suggest_bookings_for_receipt', boom)
    page = get(server_port, f'/receipts/edit?id={receipt}')
    assert page.status == 200
    html = page.text
    assert Dom(html).one('div', id='receiptSuggestions') == {'id': 'receiptSuggestions'}
    assert Dom(html).one('div', id='receiptSuggestionList') == {'id': 'receiptSuggestionList'}
    assert '<h3>Vorschläge</h3>' in html
    assert script_data(html, 'RECEIPT_SUGGEST') == {'list': [], 'empty': NO_SUGGESTIONS_TEXT}
    for text in ('geheim', '/srv', 'sqlite3', 'RuntimeError', 'Traceback'):
        assert text not in html, text
    assert SECRET in capsys.readouterr().err                                   # der volle Fehler steht im Server-Log
    monkeypatch.setattr(Database, 'suggest_bookings_for_receipt', real)
    assert ids(script_data(edit_page(server_port, receipt), 'RECEIPT_SUGGEST')['list']) == [bank, entry, edge]       # der Server antwortet weiter


@pytest.mark.parametrize('over, sentence', [
    pytest.param({}, NO_SUGGESTIONS_TEXT, id='kein-Treffer'),
    pytest.param({'GrossMinor': None}, NO_AMOUNT_TEXT, id='ohne-Brutto'),
])
def test_without_suggestions_the_page_hands_the_script_the_fixed_sentence(tmp_db, over, sentence):
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt, **over)
    add_booking(tmp_db, amount='-5.00')                                         # ein Satz mit anderem Betrag ist kein Vorschlag
    html = PageReceiptEdit(tmp_db, receipt)
    assert script_data(html, 'RECEIPT_SUGGEST') == {'list': [], 'empty': sentence}
    assert f'<p class="hint">{SUGGESTIONS_HINT}</p>' in html                    # der Hinweis steht immer da – auch ohne Vorschläge


def test_hostile_values_of_a_suggested_booking_stay_data_in_the_script_block(tmp_db):
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt)
    add_booking(tmp_db, recipient_client=HOSTILE, text=IMG, document_number='"><b>x</b>')
    html = PageReceiptEdit(tmp_db, receipt)
    assert HOSTILE not in html and IMG not in html and '"><b>x</b>' not in html          # roh steht nichts davon im HTML
    assert Dom(html).all('img') == []
    (item,) = script_data(html, 'RECEIPT_SUGGEST')['list']
    assert (item['booking']['recipient'], item['booking']['text'], item['booking']['documentNumber']) == (HOSTILE, IMG, '"><b>x</b>')
    connection = sqlite3.connect(tmp_db.db_name)
    try:
        connection.execute("UPDATE Bookings SET RecipientClient = 'Muster GmbH', Text = 'Zahlung', DocumentNumber = 'ER-1'")
        connection.commit()
    finally:
        connection.close()
    assert_same_structure(html, PageReceiptEdit(tmp_db, receipt))              # der Aufbau der Seite hängt nicht vom feindlichen Wert ab


@pytest.mark.parametrize('checks, shown, warned', [
    pytest.param({'sum': 'ok', 'rate': 'ok'}, ['Netto + Steuer = Brutto: stimmt', 'Steuer passt zum Satz: stimmt'], [], id='beide-stimmen'),
    pytest.param({'sum': 'ok', 'rate': 'mismatch'}, ['Netto + Steuer = Brutto: stimmt', 'Steuer passt zum Satz: stimmt nicht'],
                 ['Steuer passt zum Satz: stimmt nicht'], id='Satz-stimmt-nicht'),
    pytest.param({'sum': 'mismatch', 'rate': 'ok'}, ['Netto + Steuer = Brutto: stimmt nicht', 'Steuer passt zum Satz: stimmt'],
                 ['Netto + Steuer = Brutto: stimmt nicht'], id='Summe-stimmt-nicht'),
    pytest.param({'sum': 'mismatch', 'rate': 'mismatch'}, ['Netto + Steuer = Brutto: stimmt nicht', 'Steuer passt zum Satz: stimmt nicht'],
                 ['Netto + Steuer = Brutto: stimmt nicht', 'Steuer passt zum Satz: stimmt nicht'], id='beide-stimmen-nicht'),
    pytest.param({'sum': 'ok', 'rate': 'incomplete'}, ['Netto + Steuer = Brutto: stimmt'], [], id='Satzprobe-unvollstaendig'),
    pytest.param({'sum': 'incomplete', 'rate': 'ok'}, ['Steuer passt zum Satz: stimmt'], [], id='Summenprobe-unvollstaendig'),
    pytest.param({'sum': 'ok'}, ['Netto + Steuer = Brutto: stimmt'], [], id='ohne-Satzprobe-wie-bei-der-E-Rechnung'),
    pytest.param({'rate': 'weiss-nicht', 'sum': ['ok']}, [], [], id='Unbekanntes-erscheint-nie'),
])
def test_the_box_shows_the_sum_proof_and_the_rate_proof_as_fixed_sentences(tmp_db, checks, shown, warned):
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt, Raw=json.dumps({'checks': checks}))
    html = PageReceiptEdit(tmp_db, receipt)
    box = html[html.index('id="receiptReading"'):html.index('<script>', html.index('id="receiptReading"'))]
    proofs = re.findall(r'<p( class="warnColor")?>((?:Netto \+ Steuer|Steuer passt)[^<]*)</p>', box)
    assert proofs == [(' class="warnColor"' if text in warned else '', text) for text in shown]


# ── Das Skript der Seite ─────────────────────────────────────────────────────
def test_the_script_builds_the_rows_like_the_hits_of_the_search_and_asks_the_route_after_linking_and_unlinking():
    # die Funktion, die Treffer zeichnet: Suche und Vorschläge teilen sie
    assert 'function receiptBookingRow(b) {' in RECEIPT_LINK_JS and 'receiptBookingRow);' in RECEIPT_LINK_JS
    assert 'var row = receiptBookingRow(item.booking);' in RECEIPT_LINK_JS
    assert "fetch('/receipts/suggestions?id=' + encodeURIComponent(String(RECEIPT_LINK_ID)))" in RECEIPT_LINK_JS
    assert 'if (!RECEIPT_SUGGEST) return;' in RECEIPT_LINK_JS and 'if (RECEIPT_SUGGEST) receiptSuggestionsDraw(RECEIPT_SUGGEST.list);' in RECEIPT_LINK_JS
    attach = RECEIPT_LINK_JS[RECEIPT_LINK_JS.index('function receiptAttach('):RECEIPT_LINK_JS.index('function receiptDetach(')]
    detach = RECEIPT_LINK_JS[RECEIPT_LINK_JS.index('function receiptDetach('):RECEIPT_LINK_JS.index('receiptLinksRender(RECEIPT_LINKED);\nif')]
    assert 'receiptSuggestionsLoad();' in attach and 'receiptSuggestionsLoad();' in detach
    assert "'/bookings/link-document'" in attach and 'receiptAttach(b.id)' in RECEIPT_LINK_JS       # „Zuordnen“ nimmt den bestehenden Weg
    assert 'link-document' not in RECEIPT_LINK_JS[RECEIPT_LINK_JS.index('function receiptSuggestionsDraw('):RECEIPT_LINK_JS.index('function receiptAttach(')]
    load = RECEIPT_LINK_JS[RECEIPT_LINK_JS.index('function receiptSuggestionsLoad('):RECEIPT_LINK_JS.index('function receiptAttach(')]
    assert "if (typeof data.empty === 'string') RECEIPT_SUGGEST.empty = data.empty;" in load        # der Satz der Antwort (zugeordnet, kein Betrag …)
    assert load.index('RECEIPT_SUGGEST.empty = data.empty') < load.index('receiptSuggestionsDraw(')   # … gilt, bevor gezeichnet wird


@pytest.fixture
def suggest_page(tmp_db):
    """Ein Beleg mit Lesung und zwei vorgeschlagenen Buchungen (feindliche Werte); dazu die Seite des Belegs und ihr Skript."""
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt, InvoiceNumber='RE-2026-17')
    first = add_bank(tmp_db, date_booking='2026-03-06', recipient_client=IMG, text='<b>x</b> RE-2026-17')
    second = add_booking(tmp_db, date_booking=DAY, recipient_client="O'Brien & Söhne", text=HOSTILE)
    html = PageReceiptEdit(tmp_db, receipt)
    return SimpleNamespace(db=tmp_db, receipt=receipt, first=first, second=second, html=html, script=script_of(html, 'receiptLinksRender'))


@NO_NODE
def test_the_suggestions_are_drawn_as_text_with_their_reasons_below_and_a_button_each(tmp_path, suggest_page):
    out = run_page_script(tmp_path, suggest_page.script)
    tags = {node['tag'] for root in out['tree'].values() for node in walk(root)}
    assert tags <= {'div', 'table', 'tr', 'th', 'td', 'p', 'button', 'span', 'a', '#text'}, tags         # kein img, script, b …
    first, second = rows_of(out['tree']['receiptSuggestionList'])
    assert [cell['text'] for cell in first[:5]] == ['2026-03-06', f'{IMG} · <b>x</b> RE-2026-17', '-119,00 EUR', '', 'Bankbewegung']
    assert [cell['text'] for cell in second[:5]] == [DAY, f"O'Brien & Söhne · {HOSTILE}", '-119,00 EUR', '', 'Buchungssatz']
    reasons = [cell['children'][0] for cell in (first[1], second[1])]
    assert [(node['tag'], node['className'], node['text']) for node in reasons] == [
        ('span', 'readingQuote muted', 'Betrag stimmt · Rechnungsnummer im Buchungstext · 2 Tage nach dem Belegdatum'),
        ('span', 'readingQuote muted', 'Betrag stimmt · am Belegdatum')]
    for row in (first, second):
        action = row[5]['children'][0]
        assert (action['tag'], action['text'], action['className']) == ('button', 'Zuordnen', 'coloredButton btn-sm bg-green')
    header = [cell['text'] for cell in next(node for node in walk(out['tree']['receiptSuggestionList']) if node['tag'] == 'table')['children'][0]['children']]
    assert header == ['Datum', 'Empfänger / Text', 'Betrag', 'Beleg-Nr.', 'Art', '']                     # dieselbe Kopfzeile wie bei der Suche
    assert out['fetches'] == [] and out['msgs'] == []                                                       # Zeichnen allein fragt nichts an


@NO_NODE
def test_attaching_a_suggestion_takes_the_existing_route_and_refreshes_only_the_suggestions_when_nothing_was_searched(tmp_path, suggest_page):
    db = suggest_page.db
    status, body = handlers.handle_booking_link_document(
        db, json.dumps({'bookingId': suggest_page.first, 'documentId': suggest_page.receipt}).encode())      # so antwortet der Server
    assert status == 200 and json.loads(body)['created'] is True
    refreshed = handlers.handle_receipt_suggestions(db, suggest_page.receipt)                              # nach dem Verknüpfen: keine Vorschläge mehr
    assert refreshed[0] == 200 and json.loads(refreshed[1]) == {'success': True, 'suggestions': [], 'empty': LINKED_TEXT}
    out = run_page_script(tmp_path, suggest_page.script, clicks=['Zuordnen'], queryField='receiptBookingQuery', queryValue='',
                          responses=[answer(status, body), answer(*refreshed)])
    # keine Suche davor, also keine danach: Wer aus einem Vorschlag zuordnet, bekommt keine Trefferliste der jüngsten Buchungen
    assert [(fetch['method'], fetch['url']) for fetch in out['fetches']] == [
        ('POST', '/bookings/link-document'), ('GET', f'/receipts/suggestions?id={suggest_page.receipt}')]
    assert json.loads(out['fetches'][0]['body']) == {'bookingId': suggest_page.first, 'documentId': suggest_page.receipt}   # der erste Vorschlag
    assert out['msgs'] == [['Zugeordnet – die Buchung trägt jetzt die Beleg-Nr. B-1.', 'success']]
    assert [(node['tag'], node['className'], node['text']) for node in out['tree']['receiptSuggestionList']['children']] == [
        ('p', 'muted', LINKED_TEXT)]                                                                         # die Liste kommt aus der Antwort: der Satz
    (linked,) = rows_of(out['tree']['receiptLinks'])
    assert linked[0]['text'] == '2026-03-06'
    assert 'receiptBookingResults' not in out['tree'] or out['tree']['receiptBookingResults']['children'] == []


@NO_NODE
def test_attaching_a_suggestion_after_a_search_refreshes_the_hits_too(tmp_path, suggest_page):
    db = suggest_page.db
    search_all = handlers.handle_booking_search(db, {'q': ['']})
    status, body = handlers.handle_booking_link_document(
        db, json.dumps({'bookingId': suggest_page.first, 'documentId': suggest_page.receipt}).encode())
    refreshed = handlers.handle_receipt_suggestions(db, suggest_page.receipt)
    out = run_page_script(tmp_path, suggest_page.script, call='receiptBookingSearch()', clicks=['Zuordnen'], queryField='receiptBookingQuery',
                          queryValue='', responses=[answer(*search_all), answer(status, body), answer(*search_all), answer(*refreshed)])
    # die Suche war schon da: Ihre Treffer zeigen danach „zugeordnet“ – wie immer nach dem Zuordnen aus der Liste der Treffer
    assert [(fetch['method'], fetch['url']) for fetch in out['fetches']] == [
        ('GET', '/transactions/search?q='), ('POST', '/bookings/link-document'), ('GET', '/transactions/search?q='),
        ('GET', f'/receipts/suggestions?id={suggest_page.receipt}')]
    assert json.loads(out['fetches'][1]['body']) == {'bookingId': suggest_page.first, 'documentId': suggest_page.receipt}
    hits = rows_of(out['tree']['receiptBookingResults'])
    assert [row[5]['children'][0]['text'] for row in hits] == ['zugeordnet', 'Zuordnen']                      # der erste Treffer ist jetzt verknüpft
    assert [node['text'] for node in out['tree']['receiptSuggestionList']['children']] == [LINKED_TEXT]       # und die Vorschläge sind weg


@NO_NODE
def test_detaching_refreshes_the_suggestions_and_a_stale_answer_changes_nothing(tmp_path, tmp_db):
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt)
    first = add_bank(tmp_db, date_booking='2026-03-06', text='Eins')
    second = add_booking(tmp_db, date_booking=DAY, text='Zwei')
    tmp_db.link_receipt_to_booking(first, receipt)                                                           # beim Laden der Seite schon verknüpft
    script = script_of(PageReceiptEdit(tmp_db, receipt), 'receiptLinksRender')
    status, body = handlers.handle_booking_unlink_document(tmp_db, json.dumps({'bookingId': first, 'documentId': receipt}).encode())
    refreshed = handlers.handle_receipt_suggestions(tmp_db, receipt)                                         # gelöst: beide sind Kandidaten
    out = run_page_script(tmp_path, script, clicks=['Lösen'], responses=[answer(status, body), answer(*refreshed)])
    assert out['confirms'] == ['Verknüpfung lösen? Die Beleg-Nr. der Buchung bleibt.']
    assert [(fetch['method'], fetch['url']) for fetch in out['fetches']] == [
        ('POST', '/bookings/unlink-document'), ('GET', f'/receipts/suggestions?id={receipt}')]
    assert out['msgs'] == [['Verknüpfung gelöst – die Beleg-Nr. der Buchung bleibt.', 'success']]
    assert [row[1]['text'] for row in rows_of(out['tree']['receiptSuggestionList'])] == ['Zwei', 'Eins']      # gleiche Punkte: der nähere Tag zuerst
    stale = {'success': True, 'removed': True, 'documents': [], 'bookings': [], 'stale': True}
    out = run_page_script(tmp_path, script, clicks=['Lösen'], responses=[answer(200, stale)])
    assert [fetch['url'] for fetch in out['fetches']] == ['/bookings/unlink-document']                      # nichts neu holen
    assert out['msgs'] == [['Gespeichert – bitte die Seite neu laden.', 'info']]
    assert [node['text'] for node in out['tree']['receiptSuggestionList']['children']] == [LINKED_TEXT]     # die Liste der Seite bleibt: der Beleg war verknüpft


@NO_NODE
def test_an_empty_list_shows_the_sentence_the_page_handed_over(tmp_path, tmp_db):
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt, GrossMinor=None)
    out = run_page_script(tmp_path, script_of(PageReceiptEdit(tmp_db, receipt), 'receiptLinksRender'))
    assert [(node['tag'], node['className'], node['text']) for node in out['tree']['receiptSuggestionList']['children']] == [
        ('p', 'muted', 'Ohne gelesenen Betrag gibt es keine Vorschläge.')]
    again = run_page_script(tmp_path, script_of(PageReceiptEdit(tmp_db, receipt), 'receiptLinksRender'), call='receiptSuggestionsLoad()',
                            responses=[answer(200, {'success': True, 'suggestions': []})])
    assert [node['text'] for node in again['tree']['receiptSuggestionList']['children']] == ['Ohne gelesenen Betrag gibt es keine Vorschläge.']


@NO_NODE
def test_the_sentence_of_a_refreshed_answer_replaces_the_one_of_the_page_before_drawing(tmp_path, tmp_db):
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt)                                                                            # mit Betrag, ohne Treffer
    script = script_of(PageReceiptEdit(tmp_db, receipt), 'receiptLinksRender')

    def shown(out):
        return [(node['tag'], node['className'], node['text']) for node in out['tree']['receiptSuggestionList']['children']]

    assert shown(run_page_script(tmp_path, script)) == [('p', 'muted', NO_SUGGESTIONS_TEXT)]                # der Satz der Seite
    out = run_page_script(tmp_path, script, call='receiptSuggestionsLoad()',
                          responses=[answer(200, {'success': True, 'suggestions': [], 'empty': LINKED_TEXT})])
    assert shown(out) == [('p', 'muted', LINKED_TEXT)]                                                       # der Satz der Antwort gilt
    for odd in (None, 5, ['x'], {}):                                                                         # was kein Text ist, bleibt unbeachtet
        out = run_page_script(tmp_path, script, call='receiptSuggestionsLoad()',
                              responses=[answer(200, {'success': True, 'suggestions': [], 'empty': odd})])
        assert shown(out) == [('p', 'muted', NO_SUGGESTIONS_TEXT)], odd


@NO_NODE
def test_without_the_area_nothing_is_drawn_and_nothing_is_fetched(tmp_path, tmp_db):
    receipt = add_receipt(tmp_db)                                                                            # keine Lesung: gar kein Bereich
    booking = add_booking(tmp_db)
    script = script_of(PageReceiptEdit(tmp_db, receipt), 'receiptLinksRender')
    search_all = handlers.handle_booking_search(tmp_db, {'q': ['']})
    status, body = handlers.handle_booking_link_document(tmp_db, json.dumps({'bookingId': booking, 'documentId': receipt}).encode())
    out = run_page_script(tmp_path, script, call='receiptBookingSearch(); receiptSuggestionsLoad()', clicks=['Zuordnen'],
                          queryField='receiptBookingQuery', responses=[answer(*search_all), answer(status, body), answer(*search_all)])
    assert [(fetch['method'], fetch['url']) for fetch in out['fetches']] == [
        ('GET', '/transactions/search?q='), ('POST', '/bookings/link-document'), ('GET', '/transactions/search?q=')]   # nur die Wege von vorher
    assert 'receiptSuggestionList' not in out['tree'] and 'receiptSuggestions' not in out['tree']


@NO_NODE
@pytest.mark.parametrize('response, message', [
    pytest.param(answer(500, {'success': False, 'error': receipt_links.SUGGEST_FAILED}), receipt_links.SUGGEST_FAILED, id='500-mit-festem-Satz'),
    pytest.param(answer(404, {'success': False, 'error': receipt_files.NOT_FOUND}), receipt_files.NOT_FOUND, id='404-mit-Klartext'),
    pytest.param(answer(403, '<h1>403</h1>'), 'Der Server hat die Anfrage abgelehnt (Status 403).', id='kein-JSON'),
    pytest.param(answer(503, {'success': False, 'ok': False, 'error': 'Bitte später.', 'errors': ['Bitte später.']}), 'Bitte später.',
                 id='503-waehrend-der-Wiederherstellung'),
])
def test_a_refusal_while_refreshing_is_shown_and_the_list_stays_as_it_was(tmp_path, suggest_page, response, message):
    out = run_page_script(tmp_path, suggest_page.script, call='receiptSuggestionsLoad()', responses=[response])
    assert out['msgs'] == [[message, 'error']]
    assert len(rows_of(out['tree']['receiptSuggestionList'])) == 2


@NO_NODE
def test_a_lost_connection_while_refreshing_is_shown_in_words(tmp_path, suggest_page):
    out = run_page_script(tmp_path, suggest_page.script, call='receiptSuggestionsLoad()', networkError=True)
    assert out['msgs'] == [['Die Verbindung zum Server ist fehlgeschlagen.', 'error']]
    assert len(rows_of(out['tree']['receiptSuggestionList'])) == 2
