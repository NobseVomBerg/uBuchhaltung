# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""„Als Artikel speichern“ in den Masken von Rechnung und Angebot: ``POST /articles/quick-add`` (``server/handlers.py``, ``db/articles.py``).

Der Fehlfall hinter der Funktion: Eine kopierte Rechnung trug eine Position, deren Einzelpreis versehentlich mit der Stundenzahl überschrieben war – niemand sah, dass sie vom
Artikel abwich. Die Maske markiert so eine Position (``tests/test_article_marks.py``) und bietet bei einer Position, die zu keinem Artikel gehört oder vom Artikel abweicht, ein „+“:
Es legt aus der Position einen NEUEN Artikel an. **Ein vorhandener Artikel wird nie geändert.**

Geprüft wird die Route (``_JSON_PATHS`` + ``_POST_ONLY_JSON_PATHS``: GET ergibt 405, Herkunft und CSRF-Token wie bei jeder JSON-Route), jede Fehlform des Bodys (400 mit festem
Satz, nichts angelegt), die Dublettenprüfung (409, auch gegen einen inaktiven Artikel und eine andere Schreibweise; Prüfung und Anlegen in EINER Transaktion), das Geld (``money``:
auf den Cent gerundet, kein Gleitkomma-Rechnen), die Antwortform (dieselbe wie ein Eintrag von ``articlesData``) und dass nie ein Ausnahmetext in der Antwort steht.

Alle Namen, Einheiten und Preise sind erfundene Testwerte.
"""
import json
import threading
from types import SimpleNamespace

import pytest

from db import Database
from db.articles import ArticleExists, article_key
from server import app as server_app
from server.handlers import (ARTICLE_EXISTS, ARTICLE_NAME_INVALID, ARTICLE_PRICE_INVALID, ARTICLE_RATE_INVALID, ARTICLE_SAVE_FAILED,
                             ARTICLE_UNIT_INVALID)
from tests import installation
from tests.dom_helpers import Dom, data_block
from tests.http_client import FOREIGN_ORIGIN, get, own_origin, send
from tests.invoice_world import query_rows
from tests.test_receipt_upload import demo_site

ROUTE = '/articles/quick-add'
GOOD = {'name': 'Beratung Pro', 'unit': 'Std.', 'price': 85, 'taxRate': 19}
MISSING = object()


def body_of(**over):
    """Ein brauchbarer Body; ``MISSING`` lässt den Schlüssel weg."""
    return {key: value for key, value in {**GOOD, **over}.items() if value is not MISSING}


def quick_add(port, login, payload, headers=None):
    """``POST /articles/quick-add`` wie die Maske: eigene Herkunft, Token als Header; ``payload`` ein Objekt oder fertiger Text; ein Header mit ``None`` entfällt."""
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    sent = {'Origin': own_origin(port), 'Content-Type': 'application/json', 'X-CSRF-Token': login.csrf, **(headers or {})}
    return send(port, 'POST', ROUTE, {name: value for name, value in sent.items() if value is not None}, body)


def answer(response):
    assert response.headers['Content-Type'].startswith('application/json')
    return json.loads(response.text)


def articles(db):
    return db.fetch_articles()


# ── Die Route: Methode, Herkunft, Token ──────────────────────────────────────

def test_the_route_is_a_post_only_json_route_and_nothing_else():
    assert ROUTE in server_app._JSON_PATHS and ROUTE in server_app._POST_ONLY_JSON_PATHS
    assert ROUTE in server_app._JSON_ANSWER_ROUTES                            # auch die 503 während einer Wiederherstellung ist JSON
    for other in (server_app._ACTION_PATHS, server_app._GENERATE_PATHS, server_app._SEARCH_PATHS, server_app._UPLOAD_PATHS, server_app._LOCK_FREE_ROUTES):
        assert ROUTE not in other


def test_a_get_is_405_and_creates_nothing(server_port, auto_login):
    response = get(server_port, ROUTE)
    assert response.status == 405 and response.headers['Allow'] == 'POST'
    response = get(server_port, ROUTE + '?name=Beratung&unit=Std.&price=1&taxRate=19')
    assert response.status == 405
    assert articles(auto_login.db) == []


@pytest.mark.parametrize('headers, status, text', [
    pytest.param({'Origin': FOREIGN_ORIGIN}, 403, 'fremder Herkunft', id='fremder-Origin'),
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, 403, 'fremder Herkunft', id='cross-site'),
    pytest.param({'X-CSRF-Token': None}, 403, 'Sicherheits-Token', id='Token-fehlt'),
    pytest.param({'X-CSRF-Token': 'falsch'}, 403, 'Sicherheits-Token', id='Token-falsch'),
])
def test_a_request_from_a_foreign_page_or_without_a_token_creates_nothing(server_port, auto_login, headers, status, text):
    response = quick_add(server_port, auto_login, GOOD, headers)
    assert response.status == status and text in response.text
    assert articles(auto_login.db) == []


@pytest.mark.parametrize('payload', [b'{', b'', b'[]', b'[1, 2]', b'"text"', b'null', b'42', b'\xff\xfe\x00{}'])
def test_a_body_that_is_no_json_object_is_the_400_of_every_json_route(server_port, auto_login, payload):
    response = quick_add(server_port, auto_login, payload)
    assert (response.status, answer(response)) == (400, {'success': False, 'error': 'Ungültige Anfrage.'})
    assert articles(auto_login.db) == []


# ── Ein gültiger Body legt genau einen Artikel an ────────────────────────────

def test_a_good_request_creates_one_active_article_and_answers_like_an_entry_of_articlesData(server_port, auto_login):
    response = quick_add(server_port, auto_login, GOOD)

    assert response.status == 200
    body = answer(response)
    assert body['success'] is True and list(body) == ['success', 'article']
    (stored,) = articles(auto_login.db)
    assert stored[1:] == ('Beratung Pro', 'Std.', stored[3], 19.0, '', 1) and str(stored[3]) == '85.0000'
    article = body['article']
    assert list(article) == ['id', 'name', 'unit', 'price', 'taxRate', 'mixRate', 'description']          # dieselbe Form und Reihenfolge wie ein Eintrag der Masken
    assert article == {'id': stored[0], 'name': 'Beratung Pro', 'unit': 'Std.', 'price': 85.0, 'taxRate': 19.0, 'mixRate': '19', 'description': ''}
    for path in ('/invoice', '/quote'):                                       # und der Eintrag, den beide Masken danach für ihn schreiben
        listed = data_block(Dom(get(server_port, path).text), 'articlesData')
        assert listed == {str(stored[0]): article}, path


def test_the_name_and_the_unit_are_stored_trimmed_and_a_description_in_the_request_is_ignored(server_port, auto_login):
    response = quick_add(server_port, auto_login, body_of(name='  Beratung Pro \t', unit=' Std. ', description='nicht gewollt', active=0, id=99))

    assert response.status == 200
    (stored,) = articles(auto_login.db)
    assert (stored[1], stored[2], stored[5], stored[6]) == ('Beratung Pro', 'Std.', '', 1)
    assert stored[0] != 99 and answer(response)['article']['description'] == ''


@pytest.mark.parametrize('name', ['A', 'x' * 200, 'Rückbau – Straße 5 (ß, ä, ö, ü)'])
def test_a_name_of_one_to_200_printable_characters_is_accepted(server_port, auto_login, name):
    assert quick_add(server_port, auto_login, body_of(name=name)).status == 200
    assert [row[1] for row in articles(auto_login.db)] == [name]


@pytest.mark.parametrize('unit', ['m', 'x' * 20])
def test_a_unit_of_one_to_20_printable_characters_is_accepted(server_port, auto_login, unit):
    assert quick_add(server_port, auto_login, body_of(unit=unit)).status == 200
    assert [row[2] for row in articles(auto_login.db)] == [unit]


@pytest.mark.parametrize('price, minor', [
    pytest.param(85, 850000, id='ganze-Zahl'),
    pytest.param(85.0, 850000, id='Gleitkomma-ganz'),
    pytest.param(0.3, 3000, id='0,3'),
    pytest.param(85.005, 850100, id='halber-Cent-wird-aufgerundet'),
    pytest.param(0.005, 100, id='halber-Cent-klein'),
    pytest.param(0.004, 0, id='unter-einem-halben-Cent'),
    pytest.param(1.005, 10100, id='1,005-kaufmännisch-gerundet'),
    pytest.param(12.3456, 123500, id='vier-Stellen-auf-den-Cent'),
    pytest.param(0, 0, id='null'),
    pytest.param(-0.0, 0, id='minus-null'),
    pytest.param(999999999.99, 9999999999900, id='knapp-unter-der-Grenze'),
])
def test_the_price_is_rounded_to_the_cent_through_money_and_stored_in_minor_units(server_port, auto_login, price, minor):
    """Kein Gleitkomma-Rechnen: ``85.005`` ist als Text 85,005 und ergibt 85,01 (``1.005 * 100`` als Gleitkomma ergäbe 100,49999…)."""
    assert quick_add(server_port, auto_login, body_of(price=price)).status == 200
    assert query_rows(auto_login.db, 'SELECT UnitPrice FROM Articles') == [{'UnitPrice': minor}]


@pytest.mark.parametrize('rate', [0, 7, 19, 100, 5.5, 0.5, 100.0])
def test_a_tax_rate_from_0_to_100_is_stored_as_a_percentage(server_port, auto_login, rate):
    assert quick_add(server_port, auto_login, body_of(taxRate=rate)).status == 200
    assert [row[4] for row in articles(auto_login.db)] == [float(rate)]


# ── Jede Fehlform ergibt 400 mit festem Satz, nichts wird angelegt ───────────

BAD_BODIES = [
    pytest.param(body_of(name=MISSING), ARTICLE_NAME_INVALID, id='Name-fehlt'),
    pytest.param(body_of(name=None), ARTICLE_NAME_INVALID, id='Name-null'),
    pytest.param(body_of(name=''), ARTICLE_NAME_INVALID, id='Name-leer'),
    pytest.param(body_of(name='   '), ARTICLE_NAME_INVALID, id='Name-nur-Leerraum'),
    pytest.param(body_of(name=5), ARTICLE_NAME_INVALID, id='Name-Zahl'),
    pytest.param(body_of(name=['Beratung']), ARTICLE_NAME_INVALID, id='Name-Liste'),
    pytest.param(body_of(name={'a': 1}), ARTICLE_NAME_INVALID, id='Name-Objekt'),
    pytest.param(body_of(name=True), ARTICLE_NAME_INVALID, id='Name-Wahrheitswert'),
    pytest.param(body_of(name='x' * 201), ARTICLE_NAME_INVALID, id='Name-201-Zeichen'),
    pytest.param(body_of(name='Zeile\nUmbruch'), ARTICLE_NAME_INVALID, id='Name-Zeilenumbruch'),
    pytest.param(body_of(name='Tab\tulator'), ARTICLE_NAME_INVALID, id='Name-Tabulator'),
    pytest.param(body_of(name='Steuer\x00zeichen'), ARTICLE_NAME_INVALID, id='Name-NUL'),
    pytest.param(body_of(name='Geschütztes\u00a0Leerzeichen'), ARTICLE_NAME_INVALID, id='Name-nicht-druckbar'),
    pytest.param(body_of(unit=MISSING), ARTICLE_UNIT_INVALID, id='Einheit-fehlt'),
    pytest.param(body_of(unit=None), ARTICLE_UNIT_INVALID, id='Einheit-null'),
    pytest.param(body_of(unit=''), ARTICLE_UNIT_INVALID, id='Einheit-leer'),
    pytest.param(body_of(unit='  '), ARTICLE_UNIT_INVALID, id='Einheit-nur-Leerraum'),
    pytest.param(body_of(unit=1), ARTICLE_UNIT_INVALID, id='Einheit-Zahl'),
    pytest.param(body_of(unit='x' * 21), ARTICLE_UNIT_INVALID, id='Einheit-21-Zeichen'),
    pytest.param(body_of(unit='a\tb'), ARTICLE_UNIT_INVALID, id='Einheit-Tabulator'),
    pytest.param(body_of(price=MISSING), ARTICLE_PRICE_INVALID, id='Preis-fehlt'),
    pytest.param(body_of(price=None), ARTICLE_PRICE_INVALID, id='Preis-null'),
    pytest.param(body_of(price='85'), ARTICLE_PRICE_INVALID, id='Preis-Text'),
    pytest.param(body_of(price=''), ARTICLE_PRICE_INVALID, id='Preis-leer'),
    pytest.param(body_of(price=True), ARTICLE_PRICE_INVALID, id='Preis-wahr'),
    pytest.param(body_of(price=False), ARTICLE_PRICE_INVALID, id='Preis-falsch'),
    pytest.param(body_of(price=[85]), ARTICLE_PRICE_INVALID, id='Preis-Liste'),
    pytest.param(body_of(price=-0.01), ARTICLE_PRICE_INVALID, id='Preis-negativ'),
    pytest.param(body_of(price=-1), ARTICLE_PRICE_INVALID, id='Preis-minus-eins'),
    pytest.param(body_of(price=1000000000), ARTICLE_PRICE_INVALID, id='Preis-eine-Milliarde'),
    pytest.param(body_of(price=1e9), ARTICLE_PRICE_INVALID, id='Preis-eine-Milliarde-Gleitkomma'),
    pytest.param(body_of(price=10 ** 400), ARTICLE_PRICE_INVALID, id='Preis-riesige-Ganzzahl'),
    pytest.param(body_of(price=1e308), ARTICLE_PRICE_INVALID, id='Preis-1e308'),
    pytest.param(body_of(taxRate=MISSING), ARTICLE_RATE_INVALID, id='Satz-fehlt'),
    pytest.param(body_of(taxRate=None), ARTICLE_RATE_INVALID, id='Satz-null'),
    pytest.param(body_of(taxRate='19'), ARTICLE_RATE_INVALID, id='Satz-Text'),
    pytest.param(body_of(taxRate=True), ARTICLE_RATE_INVALID, id='Satz-wahr'),
    pytest.param(body_of(taxRate=-1), ARTICLE_RATE_INVALID, id='Satz-negativ'),
    pytest.param(body_of(taxRate=100.01), ARTICLE_RATE_INVALID, id='Satz-über-100'),
    pytest.param(body_of(taxRate=101), ARTICLE_RATE_INVALID, id='Satz-101'),
    pytest.param(body_of(taxRate=10 ** 400), ARTICLE_RATE_INVALID, id='Satz-riesige-Ganzzahl'),
    pytest.param(body_of(taxRate=[19]), ARTICLE_RATE_INVALID, id='Satz-Liste'),
]


@pytest.mark.parametrize('payload, sentence', BAD_BODIES)
def test_every_unusable_field_is_a_400_with_its_fixed_sentence_and_nothing_is_created(server_port, auto_login, payload, sentence):
    response = quick_add(server_port, auto_login, payload)

    assert (response.status, answer(response)) == (400, {'success': False, 'error': sentence})
    assert articles(auto_login.db) == []


@pytest.mark.parametrize('literal, sentence', [
    pytest.param('NaN', ARTICLE_PRICE_INVALID, id='NaN'),
    pytest.param('Infinity', ARTICLE_PRICE_INVALID, id='Infinity'),
    pytest.param('-Infinity', ARTICLE_PRICE_INVALID, id='minus-Infinity'),
    pytest.param('1e999', ARTICLE_PRICE_INVALID, id='1e999'),
])
def test_a_price_that_python_reads_from_non_standard_json_is_refused_too(server_port, auto_login, literal, sentence):
    """``json.loads`` nimmt ``NaN`` und ``Infinity`` an, und ``1e999`` wird zu ``inf``: Ein Preis ist es nie."""
    response = quick_add(server_port, auto_login, '{"name": "Beratung Pro", "unit": "Std.", "price": %s, "taxRate": 19}' % literal)
    assert (response.status, answer(response)) == (400, {'success': False, 'error': sentence})
    response = quick_add(server_port, auto_login, '{"name": "Beratung Pro", "unit": "Std.", "price": 1, "taxRate": %s}' % literal)
    assert (response.status, answer(response)) == (400, {'success': False, 'error': ARTICLE_RATE_INVALID})
    assert articles(auto_login.db) == []


def test_the_sentences_are_plain_text_without_exception_text_or_path():
    for sentence in (ARTICLE_NAME_INVALID, ARTICLE_UNIT_INVALID, ARTICLE_PRICE_INVALID, ARTICLE_RATE_INVALID, ARTICLE_EXISTS, ARTICLE_SAVE_FAILED):
        assert sentence and '/' not in sentence and 'Traceback' not in sentence and '<' not in sentence
    assert ARTICLE_EXISTS == 'Einen Artikel mit dieser Bezeichnung gibt es schon.'


# ── Die Dublette: 409, nichts angelegt, nichts geändert ──────────────────────

@pytest.mark.parametrize('name', ['Beratung Pro', 'beratung pro', 'BERATUNG PRO', '  Beratung Pro  ', 'Beratung   Pro', '\tBeratung Pro\n'])
def test_an_article_with_the_same_name_in_any_spelling_is_a_409_and_the_existing_one_stays_as_it_is(server_port, auto_login, name):
    auto_login.db.insert_article('Beratung Pro', 'Std.', 85.0, 19, 'Beschreibung')
    before = articles(auto_login.db)

    response = quick_add(server_port, auto_login, body_of(name=name, unit='Stk.', price=1, taxRate=7))

    assert (response.status, answer(response)) == (409, {'success': False, 'error': ARTICLE_EXISTS})
    assert articles(auto_login.db) == before


def test_an_inactive_article_counts_too(server_port, auto_login):
    auto_login.db.insert_article('Alter Artikel', 'Stk.', 10.0, 19, '', active=0)
    before = articles(auto_login.db)

    response = quick_add(server_port, auto_login, body_of(name='ALTER ARTIKEL'))

    assert (response.status, answer(response)) == (409, {'success': False, 'error': ARTICLE_EXISTS})
    assert articles(auto_login.db) == before


def test_the_comparison_knows_umlauts_which_sql_lower_does_not(server_port, auto_login):
    """``LOWER()`` von SQLite kennt nur ASCII: Die Prüfung vergleicht in Python."""
    auto_login.db.insert_article('Überprüfung', 'Stk.', 10.0, 19)

    response = quick_add(server_port, auto_login, body_of(name='ÜBERPRÜFUNG'))

    assert (response.status, answer(response)) == (409, {'success': False, 'error': ARTICLE_EXISTS})
    assert len(articles(auto_login.db)) == 1


def test_another_name_is_no_duplicate_even_when_it_starts_with_an_existing_one(server_port, auto_login):
    auto_login.db.insert_article('Beratung', 'Std.', 85.0, 19)

    assert quick_add(server_port, auto_login, body_of(name='Beratung Pro')).status == 200
    assert quick_add(server_port, auto_login, body_of(name='Beratung')).status == 409
    assert sorted(row[1] for row in articles(auto_login.db)) == ['Beratung', 'Beratung Pro']


def test_the_same_request_twice_creates_one_article_and_the_second_answer_is_409(server_port, auto_login):
    first = quick_add(server_port, auto_login, GOOD)
    second = quick_add(server_port, auto_login, GOOD)

    assert (first.status, second.status) == (200, 409)
    assert len(articles(auto_login.db)) == 1


def test_the_name_key_is_the_rule_of_the_mask():
    """Dieselbe Regel wie ``artNorm`` der Maske (Leerraum zusammengefasst, außen entfernt, Kleinbuchstaben): Zwei Artikel, die die Maske nicht auseinanderhält, legt die Route nie an."""
    assert article_key('  Beratung \t  PRO ') == 'beratung pro'
    assert article_key('ÄÖÜ') == 'äöü' and article_key(None) == '' and article_key('') == ''


# ── Prüfung und Anlegen in EINER Transaktion ─────────────────────────────────

def test_the_database_method_refuses_a_duplicate_and_returns_the_stored_article_otherwise(tmp_db):
    first = tmp_db.insert_article_unique('Beratung Pro', 'Std.', 85, 19)

    assert first[1:] == ('Beratung Pro', 'Std.', first[3], 19, '', 1) and str(first[3]) == '85.0000'
    assert tmp_db.get_article_by_id(first[0]) == first
    with pytest.raises(ArticleExists):
        tmp_db.insert_article_unique(' beratung PRO', 'Stk.', 1, 7)
    assert [row[1] for row in tmp_db.fetch_articles()] == ['Beratung Pro']


def test_two_requests_at_the_same_time_create_one_article(tmp_db):
    """Prüfung und Anlegen laufen unter ``BEGIN IMMEDIATE``: Wer als Zweiter prüft, sieht den Artikel des Ersten."""
    gate = threading.Barrier(8)
    outcome = []

    def attempt(index):
        try:
            gate.wait(timeout=10)
            tmp_db.insert_article_unique('Beratung Pro' if index % 2 else 'BERATUNG pro ', 'Std.', 85, 19)
            outcome.append('angelegt')
        except ArticleExists:
            outcome.append('vorhanden')

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcome) == ['angelegt'] + ['vorhanden'] * 7
    assert len(tmp_db.fetch_articles()) == 1


def test_a_failing_insert_rolls_back_and_releases_the_lock(tmp_db):
    """Ein Name ohne Wert lehnt die Datenbank beim INSERT ab (``NOT NULL``) – mitten in der Transaktion: nichts bleibt liegen, die Schreibsperre ist frei."""
    with pytest.raises(Exception):
        tmp_db.insert_article_unique(None, 'Std.', 85, 19)

    assert tmp_db.fetch_articles() == []
    assert tmp_db.insert_article_unique('Beratung Pro', 'Std.', 85, 19)[1] == 'Beratung Pro'


# ── Nie ein Ausnahmetext ─────────────────────────────────────────────────────

def test_an_unexpected_error_is_a_500_with_the_fixed_sentence_and_the_full_error_goes_to_the_log(server_port, auto_login, monkeypatch, capsys):
    def broken(self, *args, **kwargs):
        raise RuntimeError('geheimer Pfad /srv/ubuch/buch.db ist kaputt')

    monkeypatch.setattr(Database, 'insert_article_unique', broken)

    response = quick_add(server_port, auto_login, GOOD)

    assert (response.status, answer(response)) == (500, {'success': False, 'error': ARTICLE_SAVE_FAILED})
    assert 'geheimer Pfad' not in response.text and '/srv/' not in response.text and 'RuntimeError' not in response.text
    assert 'geheimer Pfad /srv/ubuch/buch.db ist kaputt' in capsys.readouterr().err           # der volle Fehler steht im Server-Log
    assert articles(auto_login.db) == []


# ── Der Demo-Modus: keine Datei, also erlaubt ────────────────────────────────

def test_in_demo_mode_the_route_works_like_anywhere_since_no_file_is_involved(server_port, monkeypatch):
    login = SimpleNamespace(csrf=demo_site(monkeypatch))
    db = installation.open_db('demo')

    response = quick_add(server_port, login, GOOD)

    assert response.status == 200 and answer(response)['success'] is True
    assert [row[1] for row in articles(db)] == ['Beratung Pro']
