# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Angebot → Rechnung (2.1.2, T102): einmal je Angebot, in EINER Transaktion.

Umgewandelt wird nur ein Angebot im Status Entwurf, versendet oder angenommen; ein umgewandeltes oder
abgelehntes Angebot wird mit einem Klartext abgewiesen, ohne Rechnung, ohne verbrauchte Nummer. Der
Statuswechsel des Angebots ist ein ``UPDATE … WHERE Status IN (…)``: Zwei gleichzeitige Anfragen legen
nie zwei Rechnungen an, und scheitert ein Schritt, bleibt das Angebot, wie es war. Alle Namen und
Nummern sind erfundene Testwerte.
"""
import datetime
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

import server.handlers as handlers
from db.invoices import QuoteNotConvertible
from server.pages_quote import PageQuote
from tests.document_helpers import state
from tests.dom_helpers import Dom, app_msg_arguments
from tests.http_client import get, own_origin, post
from tests.invoice_world import build_world

YEAR = datetime.date.today().year
SHORT = str(YEAR)[2:]


def quote(db, number='26A001', status='accepted', **extra):
    quote_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-06-09', 'seller_name': 'S',
        'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
        'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0, 'amount_due': 119.0,
        'status': status, 'document_type': 'quote', 'valid_until': '2026-07-09', **extra})
    db.insert_invoice_item({'invoice_id': quote_id, 'position': 1, 'description': 'Position 1',
                            'quantity': 1, 'unit': 'C62', 'price_per_unit': 100.0, 'total_net': 100.0,
                            'tax_rate': 0.19})
    return quote_id


def invoices_of(db, quote_id):
    with db._get_connection() as con:
        return [r[0] for r in con.execute('SELECT ID FROM Invoices WHERE SourceQuoteId = ?', (quote_id,))]


def status_of(db, doc_id):
    return db.get_invoice_by_id(doc_id)[40]


def counter(db):
    return [r[5] for r in db.fetch_number_ranges('invoice') if r[2] == YEAR][0]


@pytest.fixture
def numbered(tmp_db):
    tmp_db.insert_number_range('invoice', YEAR, 'R', current_number=10)
    return tmp_db


@pytest.mark.parametrize('status', ['draft', 'sent', 'accepted'])
def test_a_quote_is_converted_once_from_every_convertible_status(numbered, status):
    quote_id = quote(numbered, status=status)
    new_id = numbered.convert_quote_to_invoice(quote_id)
    assert new_id and invoices_of(numbered, quote_id) == [new_id]
    assert numbered.get_invoice_by_id(new_id)[1] == f'{SHORT}R011' and counter(numbered) == 11
    assert status_of(numbered, quote_id) == 'converted' and status_of(numbered, new_id) == 'draft'


def test_the_second_conversion_is_refused_in_plain_words_and_changes_nothing(numbered):
    quote_id = quote(numbered)
    first = numbered.convert_quote_to_invoice(quote_id)
    before = state(numbered, 'Invoices', 'InvoiceItems', 'NumberRanges')
    with pytest.raises(QuoteNotConvertible) as refused:
        numbered.convert_quote_to_invoice(quote_id)
    assert str(refused.value) == 'Dieses Angebot wurde bereits in eine Rechnung umgewandelt.'
    assert invoices_of(numbered, quote_id) == [first]
    assert state(numbered, 'Invoices', 'InvoiceItems', 'NumberRanges') == before     # auch der Zähler


def test_a_rejected_quote_is_not_converted_until_it_is_accepted(numbered):
    quote_id = quote(numbered, status='rejected')
    before = state(numbered, 'Invoices', 'InvoiceItems', 'NumberRanges')
    with pytest.raises(QuoteNotConvertible) as refused:
        numbered.convert_quote_to_invoice(quote_id)
    assert 'abgelehnt' in str(refused.value) and 'Angenommen' in str(refused.value)
    assert state(numbered, 'Invoices', 'InvoiceItems', 'NumberRanges') == before
    numbered.update_invoice_status(quote_id, 'accepted')
    assert numbered.convert_quote_to_invoice(quote_id)


def test_a_document_that_is_no_quote_is_none_and_costs_no_number(numbered):
    invoice_id = numbered.insert_invoice({
        'invoice_number': '26R001', 'invoice_date': '2026-06-09', 'seller_name': 'S', 'seller_company': 'S',
        'buyer_name': 'B', 'buyer_company': 'B', 'tax_rate': 0.19, 'sum_net': 1, 'tax_amount': 0.19,
        'sum_gross': 1.19, 'amount_due': 1.19, 'status': 'draft'})
    before = state(numbered, 'Invoices', 'InvoiceItems', 'NumberRanges')
    assert numbered.convert_quote_to_invoice(invoice_id) is None
    assert numbered.convert_quote_to_invoice(99999) is None
    assert state(numbered, 'Invoices', 'InvoiceItems', 'NumberRanges') == before


def test_a_failing_step_leaves_the_quote_as_it_was(numbered, monkeypatch):
    quote_id = quote(numbered)
    before = state(numbered, 'Invoices', 'InvoiceItems', 'NumberRanges')

    def boom(*args, **kwargs):
        raise RuntimeError('Position nicht schreibbar')
    with monkeypatch.context() as patched:                  # nur diese Ersetzung zurücknehmen, nicht die der Fixtures
        patched.setattr(numbered, 'insert_invoice_item', boom)
        with pytest.raises(RuntimeError):
            numbered.convert_quote_to_invoice(quote_id)
    assert state(numbered, 'Invoices', 'InvoiceItems', 'NumberRanges') == before     # Status, Rechnung, Zähler
    assert numbered.convert_quote_to_invoice(quote_id)                              # und der nächste Versuch geht


def test_two_simultaneous_conversions_create_exactly_one_invoice(numbered):
    for round_ in range(6):
        quote_id = quote(numbered, number=f'26A{100 + round_}')
        results = [None, None]
        barrier = threading.Barrier(2)

        def convert(index):
            barrier.wait()
            try:
                results[index] = numbered.convert_quote_to_invoice(quote_id)
            except QuoteNotConvertible as refused:
                results[index] = refused

        threads = [threading.Thread(target=convert, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        created = [r for r in results if isinstance(r, int)]
        refused = [r for r in results if isinstance(r, QuoteNotConvertible)]
        assert len(created) == 1 and len(refused) == 1
        assert invoices_of(numbered, quote_id) == created
    assert counter(numbered) == 16                                                # sechs Rechnungen, sechs Nummern


def test_the_handler_sends_the_refusal_back_to_the_quote(numbered, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: numbered)
    quote_id = quote(numbered)
    code, location = handlers.handle_convert_quote_to_invoice({'quote_id': [str(quote_id)]})
    assert code == 303 and location == f'/invoice?id={invoices_of(numbered, quote_id)[0]}'
    code, location = handlers.handle_convert_quote_to_invoice({'quote_id': [str(quote_id)]})
    assert code == 303 and location.startswith(f'/quote?id={quote_id}&err=')
    assert parse_qs(urlsplit(location).query)['err'] == ['Dieses Angebot wurde bereits in eine Rechnung umgewandelt.']
    assert len(invoices_of(numbered, quote_id)) == 1
    # eine ID, die kein Angebot ist, bleibt eine stille Weiterleitung ohne Grund (wie bisher)
    assert handlers.handle_convert_quote_to_invoice({'quote_id': ['99999']}) == (303, '/quote?id=99999')


def test_the_quote_page_shows_the_reason(numbered):
    quote_id = quote(numbered)
    html = PageQuote(numbered, {}, quote_id, err='Dieses Angebot wurde bereits umgewandelt.')
    assert app_msg_arguments(Dom(html)) == ['Dieses Angebot wurde bereits umgewandelt.', 'error']
    assert 'appMsg(' not in PageQuote(numbered, {}, quote_id).split('Ende gridRightCol')[1].replace(
        'function appMsg(', '')


@pytest.fixture
def books(auto_login):
    w = build_world(auto_login.db)
    w.csrf = auto_login.csrf
    return w


def test_over_http_the_second_click_lands_on_the_quote_with_a_message(server_port, books):
    books.db.insert_number_range('invoice', YEAR, 'R', current_number=0)
    quote_id = quote(books.db)
    headers = {'Origin': own_origin(server_port)}
    first = post(server_port, '/quote/convert', form={'quote_id': str(quote_id)}, csrf=books.csrf,
                 headers=headers)
    assert first.status == 303 and first.headers['Location'].startswith('/invoice?id=')
    second = post(server_port, '/quote/convert', form={'quote_id': str(quote_id)}, csrf=books.csrf,
                  headers=headers)
    assert second.status == 303 and second.headers['Location'].startswith(f'/quote?id={quote_id}&err=')
    page = get(server_port, second.headers['Location'])
    assert page.status == 200
    assert app_msg_arguments(Dom(page.text)) == ['Dieses Angebot wurde bereits in eine Rechnung umgewandelt.',
                                                 'error']
    assert len(invoices_of(books.db, quote_id)) == 1
