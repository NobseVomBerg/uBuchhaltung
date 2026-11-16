# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""„Testdaten laden“ gibt es nur für einen leeren Bestand, mit Rückfrage (2.3.8).

Vorher lud der Knopf auf *Sonstiges* („Testdaten nachladen“) in jeden Bestand Beispielkontakte, -rechnungen, -buchungen und
-anlagen dazu – ein Klick zu viel mitten in den echten Büchern.

* ``Database.stock_is_empty``: keine Buchung, keine Rechnung oder Angebot, keine Anlage – Stammdaten zählen nicht.
* ``handlers.handle_load_testdata``: nur in einen leeren Bestand; sonst wird nichts geladen und die Weiterleitung nennt
  ``?testdata=refused``. ``Database.load_test_seed_data`` selbst prüft nichts (Tests, Demo und Ersteinrichtung).
* Die Seite *Sonstiges*: Knopf mit Rückfrage nur bei leerem Bestand, sonst EIN Satz (``TESTDATA_NOTICE``: der Satz der
  Ablehnung ``db.seed.TESTDATA_REFUSED`` samt Klammer, was ein leerer Bestand ist) – nach einer Ablehnung derselbe Absatz in
  Rot statt als Randbemerkung; aus der Adresse kommt nur das Kennzeichen ``refused``, nie Text.

Alle Namen sind erfundene Testwerte.
"""
import contextlib
import re
import sqlite3
import urllib.parse

import pytest

from db.seed import TESTDATA_REFUSED
from server import handlers
from server.pages_miscellaneous import TESTDATA_NOTICE, PageMiscellaneous
from tests import installation
from tests.document_helpers import add_contacts, add_document, make_books
from tests.dom_helpers import Dom
from tests.http_client import get, own_origin, post

DAY = '2026-03-04'
HINT = ('Der Bestand ist nicht leer – Testdaten lassen sich nur in einen leeren Bestand laden '
        '(keine Buchungen, Rechnungen, Angebote und Anlagen).')
SENTENCE_START = 'Testdaten lassen sich nur in einen leeren Bestand laden'
CONFIRM_ONCLICK = ("appConfirmPost('/setup/load_testdata', 'Testdaten laden? Es entstehen Beispiel-Kontakte, "
                   "-Rechnungen, -Buchungen und -Anlagen in diesem Bestand.')")
WRITTEN = ('Contacts', 'Invoices', 'InvoiceItems', 'Bookings', 'Assets', 'Articles', 'Accounts', 'Documents',
           'WorkTimes')


def counts(db, *tables):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return {table: con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables}


def sentences(html):
    """Alle Absätze ``<p …>…</p>`` der Seite, die den Satz „Testdaten lassen sich nur in einen leeren Bestand laden“
    tragen, als (Klasse, Text) – ``Dom`` kennt nur Starttags."""
    found = re.findall(r'<p(?: class="([^"]*)")?>(.*?)</p>', html, re.S)
    return [(cls, body) for cls, body in found if SENTENCE_START in body]


def load_buttons(html):
    """Alle Schaltflächen der Seite, die „Testdaten laden“ auslösen, und alle Formulare, die dorthin senden."""
    buttons = [a for a in Dom(html).all('button') if 'load_testdata' in (a.get('onclick') or '')]
    forms = [a for a in Dom(html).all('form') if a.get('action') == '/setup/load_testdata']
    return buttons, forms


# ── Database.stock_is_empty ──────────────────────────────────────────────────
def test_a_fresh_stock_is_empty(tmp_db):
    assert tmp_db.stock_is_empty() is True


def test_master_data_alone_does_not_fill_the_stock(tmp_db):
    add_contacts(tmp_db)
    tmp_db.insert_article(name='Probeartikel')
    tmp_db.insert_account('Probekonto', 'Inhaber', 'DE89370400440532013000', 'COBADEFFXXX', 'Probebank', is_cash=0,
                          skr_account=1810)
    tmp_db.insert_number_range('invoice', 2031, 'Z')
    tmp_db.insert_asset_category('Probekategorie', 3)
    tmp_db.insert_receipt('B-100', DAY, 'beleg.pdf', 'pfad/beleg.pdf', 'Info')
    person = tmp_db.insert_contact(contact_type='employee', entity_type='person', first_name='Eva', last_name='Probe')
    tmp_db.insert_worktime(person, DAY)
    tmp_db.insert_trip(person, DAY, destination='Ziel')
    assert tmp_db.stock_is_empty() is True


def _booking(db):
    db.insert_booking(DAY, 10)


def _bank_booking(db):
    db.insert_booking(DAY, 10, booking_type='bank')


def _invoice(db):
    add_document(db, 'R-1', 'invoice')


def _quote(db):
    add_document(db, 'A-1', 'quote')


def _asset(db):
    db.insert_asset('Rechner', DAY, 100, 3)


@pytest.mark.parametrize('add', [_booking, _bank_booking, _invoice, _quote, _asset],
                         ids=['Buchung', 'Bankbewegung', 'Rechnung', 'Angebot', 'Anlage'])
def test_a_booking_an_invoice_a_quote_or_an_asset_makes_the_stock_non_empty(tmp_db, add):
    add(tmp_db)
    assert tmp_db.stock_is_empty() is False


# ── handlers.handle_load_testdata ────────────────────────────────────────────
def test_the_handler_loads_the_test_data_into_an_empty_stock(tmp_db):
    assert handlers.handle_load_testdata(tmp_db) == (303, '/')
    after = counts(tmp_db, *WRITTEN)
    for table in ('Contacts', 'Invoices', 'InvoiceItems', 'Bookings', 'Assets', 'Articles'):
        assert after[table] > 0, table
    assert tmp_db.stock_is_empty() is False


@pytest.mark.parametrize('add', [_booking, _invoice, _quote, _asset], ids=['Buchung', 'Rechnung', 'Angebot', 'Anlage'])
def test_the_handler_refuses_a_non_empty_stock_and_loads_nothing(tmp_db, add):
    add(tmp_db)
    before = installation.dump(tmp_db.db_name)
    assert handlers.handle_load_testdata(tmp_db) == (303, '/miscellaneous?testdata=refused')
    assert installation.dump(tmp_db.db_name) == before


def test_a_second_click_after_loading_is_refused(tmp_db):
    handlers.handle_load_testdata(tmp_db)
    loaded = counts(tmp_db, *WRITTEN)
    assert handlers.handle_load_testdata(tmp_db) == (303, '/miscellaneous?testdata=refused')
    assert counts(tmp_db, *WRITTEN) == loaded


def test_the_database_method_itself_checks_nothing(tmp_db):
    """Tests, Demodaten und die Ersteinrichtung rufen ``load_test_seed_data`` direkt auf."""
    _booking(tmp_db)
    before = counts(tmp_db, 'Contacts')['Contacts']
    tmp_db.load_test_seed_data()
    assert counts(tmp_db, 'Contacts')['Contacts'] > before


# ── die Seite „Sonstiges“ ────────────────────────────────────────────────────
def test_an_empty_stock_offers_the_button_with_a_confirmation(tmp_db):
    html = PageMiscellaneous(tmp_db)
    buttons, forms = load_buttons(html)
    assert forms == []                                         # kein Formular, das ohne Rückfrage absendet
    assert len(buttons) == 1
    assert buttons[0]['type'] == 'button' and buttons[0]['onclick'] == CONFIRM_ONCLICK
    assert 'Testdaten laden' in html
    assert HINT not in html and TESTDATA_REFUSED not in html
    assert 'nachladen' not in html and 'idempotent' not in html          # die alte Zusage entfällt


@pytest.mark.parametrize('add', [_booking, _invoice, _quote, _asset], ids=['Buchung', 'Rechnung', 'Angebot', 'Anlage'])
def test_a_non_empty_stock_shows_the_hint_instead_of_the_button(tmp_db, add):
    add(tmp_db)
    html = PageMiscellaneous(tmp_db)
    assert load_buttons(html) == ([], [])
    assert f'<p class="hint">{HINT}</p>' in html
    assert TESTDATA_REFUSED not in html                        # der Satz allein (mit Punkt) steht nur bei leerem Bestand
    assert 'nachladen' not in html


def test_the_hint_is_the_sentence_of_the_refusal_with_the_explanation_of_an_empty_stock():
    assert TESTDATA_NOTICE == HINT
    assert TESTDATA_NOTICE.startswith(TESTDATA_REFUSED[:-1])
    assert TESTDATA_REFUSED == 'Der Bestand ist nicht leer – Testdaten lassen sich nur in einen leeren Bestand laden.'


@pytest.mark.parametrize('add', [_booking, _invoice, _quote, _asset], ids=['Buchung', 'Rechnung', 'Angebot', 'Anlage'])
def test_a_non_empty_stock_shows_exactly_one_sentence_without_a_refusal(tmp_db, add):
    add(tmp_db)
    found = sentences(PageMiscellaneous(tmp_db))
    assert len(found) == 1 and found[0][0] == 'hint'


@pytest.mark.parametrize('add', [_booking, _invoice, _quote, _asset], ids=['Buchung', 'Rechnung', 'Angebot', 'Anlage'])
def test_after_a_refusal_the_same_paragraph_turns_red_and_nothing_stands_twice(tmp_db, add):
    add(tmp_db)
    html = PageMiscellaneous(tmp_db, testdata_refused=True)
    assert f'<p class="errorColor">{HINT}</p>' in html
    assert '<p class="hint">' + HINT not in html              # derselbe Absatz, nicht zusätzlich als Randbemerkung
    found = sentences(html)
    assert len(found) == 1 and found[0][0] == 'errorColor'
    assert TESTDATA_REFUSED not in html                        # nicht zusätzlich der Satz allein
    assert load_buttons(html) == ([], [])


def test_the_fixed_sentence_stands_even_where_the_stock_is_empty_again(tmp_db):
    """Die Ablehnung kam, der Bestand wurde seither geleert: Der Satz der Ablehnung steht in Rot, der Knopf ist wieder da."""
    html = PageMiscellaneous(tmp_db, testdata_refused=True)
    assert f'<p class="errorColor">{TESTDATA_REFUSED}</p>' in html
    assert len(load_buttons(html)[0]) == 1


def test_the_sentence_is_not_shown_by_default(tmp_db):
    assert TESTDATA_REFUSED not in PageMiscellaneous(tmp_db)
    assert sentences(PageMiscellaneous(tmp_db)) == []


# ── über HTTP ────────────────────────────────────────────────────────────────
@pytest.fixture
def books(auto_login):
    return make_books(auto_login)


def load_over_http(port, auto_login):
    return post(port, '/setup/load_testdata', headers={'Origin': own_origin(port)}, csrf=auto_login.csrf)


def test_the_route_loads_into_an_empty_stock(server_port, auto_login):
    assert auto_login.db.stock_is_empty()
    resp = load_over_http(server_port, auto_login)
    assert resp.status == 303 and resp.headers['Location'] == '/'
    after = counts(auto_login.db, 'Contacts', 'Invoices', 'Bookings', 'Assets', 'Articles')
    assert all(after.values()), after


def test_the_route_refuses_a_non_empty_stock_and_loads_nothing(server_port, auto_login, books):
    before = counts(books.db, *WRITTEN)
    resp = load_over_http(server_port, auto_login)
    assert resp.status == 303 and resp.headers['Location'] == '/miscellaneous?testdata=refused'
    assert counts(books.db, *WRITTEN) == before
    page = get(server_port, resp.headers['Location'])
    assert page.status == 200
    assert f'<p class="errorColor">{HINT}</p>' in page.text
    found = sentences(page.text)
    assert len(found) == 1 and found[0][0] == 'errorColor'
    assert TESTDATA_REFUSED not in page.text
    assert load_buttons(page.text) == ([], [])


def test_the_page_offers_the_button_only_for_an_empty_stock_over_http(server_port, auto_login):
    empty = get(server_port, '/miscellaneous')
    assert empty.status == 200 and len(load_buttons(empty.text)[0]) == 1
    assert TESTDATA_REFUSED not in empty.text
    add_document(auto_login.db, 'R-1', 'invoice')
    full = get(server_port, '/miscellaneous')
    assert full.status == 200 and load_buttons(full.text) == ([], []) and f'<p class="hint">{HINT}</p>' in full.text


@pytest.mark.parametrize('value', ['', 'REFUSED', 'refused ', 'refusedX', 'true', '1'])
def test_only_exactly_refused_sets_the_flag(server_port, auto_login, value):
    resp = get(server_port, '/miscellaneous?' + urllib.parse.urlencode({'testdata': value}))
    assert resp.status == 200
    assert TESTDATA_REFUSED not in resp.text


@pytest.mark.parametrize('value', ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "';alert(1);//"])
def test_no_text_of_the_address_reaches_the_page(server_port, auto_login, value):
    resp = get(server_port, '/miscellaneous?' + urllib.parse.urlencode({'testdata': value}))
    assert resp.status == 200
    assert TESTDATA_REFUSED not in resp.text and value not in resp.text


def test_the_flag_over_http_shows_the_fixed_sentence(server_port, auto_login):
    resp = get(server_port, '/miscellaneous?testdata=refused')
    assert resp.status == 200 and f'<p class="errorColor">{TESTDATA_REFUSED}</p>' in resp.text


def test_the_first_set_up_page_still_offers_the_test_data(tmp_db):
    """Die Ersteinrichtung bleibt, wie sie ist: Dort ist der Bestand leer."""
    from server.pages_setup import PageSetup
    assert [a for a in Dom(PageSetup(tmp_db)).all('form') if a.get('action') == '/setup/load_testdata']
