# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belegtyp (2.0.7): Rechnungen und Angebote teilen sich die Tabelle ``Invoices``.

Jede Rechnungs- und jede Angebotsroute prüft den Typ (``Database.get_document``,
``db.invoices.document_type``, beim Schreiben ``… AND DocumentType = …``): Die ID eines Belegs der
anderen Art ergibt 404 bzw. „Rechnung/Angebot nicht gefunden.“ und ändert, versendet und erzeugt
nichts. Verglichen wird die ganze Tabelle vorher/nachher (jede Spalte jeder Zeile, die Positionen)
und der Inhalt des Datenordners.

Dazu der Wächter für ``InvoiceCol`` – die benannten Spaltenpositionen einer ``SELECT *``-Zeile.
Alle Namen und Nummern sind erfundene Testwerte.
"""
import contextlib
import json
import sqlite3
from decimal import Decimal

import pytest

from db.invoices import InvoiceCol, document_type
from export.invoice_files import generate_invoice_documents
from export.pdf_invoice import generate_invoice_pdf
from export.pdf_quote import generate_quote_pdf
from server import handlers
from server.pages_invoice import PageInvoice
from server.pages_quote import PageQuote
from tests.document_helpers import (INVOICE_NUMBER, QUOTE_NUMBER, STATUS, as_user, fake_smtp,
                                    make_books, post_action, post_json, row, state, store_pdf,
                                    stored_documents, write_file)
from tests.dom_helpers import Dom
from tests.http_client import get, post

MAIL = {'recipient_email': 'kunde@example.com', 'recipient_name': 'Erika Empfaenger'}


@pytest.fixture
def books(auto_login):
    """Rechnung und Angebot, beide mit abgelegter PDF (die Rechnung auch mit XML): Ein Zugriff über die
    falsche Route würde die Datei der anderen Belegart ausliefern. Die Nummernkreise 26R und 26A der
    Rechnungen sind vorhanden – ein Angebot, das als Rechnung behandelt würde, rückte einen davon vor."""
    books = make_books(auto_login)
    books.db.insert_number_range('invoice', 2026, 'R', current_number=1)
    books.db.insert_number_range('invoice', 2026, 'A', current_number=4)
    books.invoice_pdf = store_pdf(books, 'invoice', b'%PDF-1.4 Rechnung')
    books.quote_pdf = store_pdf(books, 'quote', b'%PDF-1.4 Angebot')
    books.invoice_xml = write_file(books.invoice_pdf[:-4] + '.xml', b'<Invoice>Rechnung</Invoice>')
    books.db.update_invoice_xml_path(books.invoice, books.invoice_xml)
    return books


def snapshot(books):
    """Alles, was eine Route verändern könnte: die Tabellen (auch Buchungen, Zahlungen und Nummernkreise)
    Spalte für Spalte und alle abgelegten Dokumente samt Inhalt."""
    return (state(books.db, 'Invoices', 'InvoiceItems', 'InvoicePayments', 'Bookings', 'NumberRanges'),
            stored_documents())


def valid_invoice_payload(books, **over):
    payload = {'invoiceNumber': '26R777', 'invoiceDate': '2026-06-07', 'deliveryDate': '2026-06-05',
               'customerId': books.customer, 'ownCompanyId': books.own, 'taxRate': 0.19, 'status': 'draft',
               'items': [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung',
                          'unitPrice': 100.0, 'taxRate': 19}]}
    payload.update(over)
    return payload


def valid_quote_payload(books, **over):
    payload = {'quoteNumber': '26A777', 'quoteDate': '2026-06-07', 'customerId': books.customer,
               'ownCompanyId': books.own, 'validUntil': '2026-07-07', 'taxRate': 0.19, 'status': 'draft',
               'introText': '<b>Hallo</b>', 'closingText': 'Gruß',
               'items': [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung',
                          'unitPrice': 100.0, 'taxRate': 19}]}
    payload.update(over)
    return payload


def _json(route, payload_for):
    return lambda port, b, doc: post_json(port, route, payload_for(b, doc), b.csrf)


def _expect_json(status, error):
    def check(resp, doc):
        assert resp.status == status
        payload = json.loads(resp.text)
        assert payload['success'] is False
        assert payload['error'].rstrip('.') == error
    return check


def _expect_plain(error):
    def check(resp, doc):
        assert resp.status == 404 and resp.text == error
    return check


def _expect_redirect(location_for):
    def check(resp, doc):
        assert resp.status == 303 and resp.headers['Location'] == location_for(doc)
    return check


WITH_QUOTE_ID = [         # Routen für Rechnungen, aufgerufen mit der ID eines Angebots
    pytest.param(_json('/invoice/save', lambda b, d: valid_invoice_payload(b, invoiceId=d)),
                 _expect_json(200, 'Rechnung nicht gefunden'), id='invoice-save'),
    pytest.param(_json('/invoice/status', lambda b, d: {'invoice_id': d, 'status': 'sent'}),
                 _expect_json(404, 'Rechnung nicht gefunden'), id='invoice-status-sent'),
    pytest.param(_json('/invoice/status', lambda b, d: {'invoice_id': d, 'status': 'finalized'}),
                 _expect_json(404, 'Rechnung nicht gefunden'), id='invoice-status-finalized'),
    pytest.param(lambda port, b, doc: post_action(port, f'/invoice/pdf_generate?id={doc}', b.csrf),
                 _expect_json(404, 'Rechnung nicht gefunden'), id='invoice-pdf_generate'),
    pytest.param(lambda port, b, doc: get(port, f'/invoice/pdf_download?id={doc}'),
                 _expect_plain('Rechnung nicht gefunden.'), id='invoice-pdf_download'),
    pytest.param(lambda port, b, doc: get(port, f'/invoice/pdf_download?id={doc}&inline=1'),
                 _expect_plain('Rechnung nicht gefunden.'), id='invoice-pdf_download-inline'),
    pytest.param(lambda port, b, doc: get(port, f'/invoice/xml_download?id={doc}'),
                 _expect_plain('Rechnung nicht gefunden.'), id='invoice-xml_download'),
    pytest.param(_json('/invoice/send-email', lambda b, d: {'invoice_id': d, **MAIL}),
                 _expect_json(200, 'Rechnung nicht gefunden'), id='invoice-send-email'),
    pytest.param(_json('/invoice/link-payment', lambda b, d: {'invoice_id': d, 'transaction_id': 1}),
                 _expect_json(400, 'Rechnung nicht gefunden'), id='invoice-link-payment'),
]
WITH_INVOICE_ID = [       # Routen für Angebote, aufgerufen mit der ID einer Rechnung
    pytest.param(_json('/quote/save', lambda b, d: valid_quote_payload(b, quoteId=d)),
                 _expect_json(200, 'Angebot nicht gefunden'), id='quote-save'),
    pytest.param(_json('/quote/status', lambda b, d: {'quote_id': d, 'status': 'accepted'}),
                 _expect_json(404, 'Angebot nicht gefunden'), id='quote-status'),
    pytest.param(lambda port, b, doc: post_action(port, f'/quote/pdf_generate?id={doc}', b.csrf),
                 _expect_json(404, 'Angebot nicht gefunden'), id='quote-pdf_generate'),
    pytest.param(lambda port, b, doc: get(port, f'/quote/pdf_download?id={doc}'),
                 _expect_plain('Angebot nicht gefunden.'), id='quote-pdf_download'),
    pytest.param(lambda port, b, doc: post_action(port, f'/quote/delete?id={doc}', b.csrf),
                 _expect_redirect(lambda doc: '/quote'), id='quote-delete'),
    pytest.param(lambda port, b, doc: post(port, '/quote/convert', form={'quote_id': str(doc)}, csrf=b.csrf),
                 _expect_redirect(lambda doc: f'/quote?id={doc}'), id='quote-convert'),
]


@pytest.mark.parametrize('send_request, check', WITH_QUOTE_ID)
def test_an_invoice_route_with_the_id_of_a_quote_is_rejected_and_changes_nothing(
        server_port, books, monkeypatch, send_request, check):
    smtp = fake_smtp(monkeypatch)
    before = snapshot(books)
    resp = send_request(server_port, books, books.quote)
    check(resp, books.quote)
    assert snapshot(books) == before                    # Tabellen Spalte für Spalte, Positionen, Dateien
    assert smtp.connections == []                        # und es geht nichts per E-Mail hinaus
    assert b'%PDF' not in resp.body and QUOTE_NUMBER.encode() not in resp.body


@pytest.mark.parametrize('send_request, check', WITH_INVOICE_ID)
def test_a_quote_route_with_the_id_of_an_invoice_is_rejected_and_changes_nothing(
        server_port, books, send_request, check):
    before = snapshot(books)
    resp = send_request(server_port, books, books.invoice)
    check(resp, books.invoice)
    assert snapshot(books) == before
    assert b'%PDF' not in resp.body and INVOICE_NUMBER.encode() not in resp.body


def test_a_download_with_the_wrong_route_never_delivers_the_other_documents_file(server_port, books):
    for route, doc in (('/invoice/pdf_download', books.quote), ('/invoice/xml_download', books.quote),
                       ('/quote/pdf_download', books.invoice)):
        resp = get(server_port, f'{route}?id={doc}')
        assert resp.status == 404
        assert b'%PDF' not in resp.body and b'<Invoice>' not in resp.body
    # die richtige Route liefert dieselben Dateien
    assert get(server_port, f'/invoice/pdf_download?id={books.invoice}').body == b'%PDF-1.4 Rechnung'
    assert get(server_port, f'/quote/pdf_download?id={books.quote}').body == b'%PDF-1.4 Angebot'
    assert get(server_port, f'/invoice/xml_download?id={books.invoice}').body == b'<Invoice>Rechnung</Invoice>'


def test_the_right_route_still_works_for_each_kind(server_port, books):
    """Gegenstück: Dieselben Anfragen mit der ID des passenden Belegs werden nicht abgewiesen."""
    status = post_json(server_port, '/invoice/status', {'invoice_id': books.invoice, 'status': 'finalized'},
                       books.csrf)
    assert status.status == 200 and row(books.db, books.invoice)[STATUS] == 'finalized'
    status = post_json(server_port, '/quote/status', {'quote_id': books.quote, 'status': 'sent'}, books.csrf)
    assert status.status == 200 and row(books.db, books.quote)[STATUS] == 'sent'
    saved = post_json(server_port, '/quote/save', valid_quote_payload(books, quoteId=books.quote,
                                                                         quoteNumber=QUOTE_NUMBER), books.csrf)
    assert json.loads(saved.text) == {'success': True, 'quote_id': books.quote}
    saved = post_json(server_port, '/invoice/save', valid_invoice_payload(books, invoiceId=books.invoice,
                                                                             invoiceNumber=INVOICE_NUMBER), books.csrf)
    assert json.loads(saved.text) == {'success': True, 'invoice_id': books.invoice}


# ── Formulare: die ID des anderen Belegs ergibt das leere Formular ───────────
def render(page, *args, **kwargs):
    """Seite als angemeldeter Benutzer erzeugen (seine Dokumente liegen unter users/<Name>)."""
    with as_user():
        return page(*args, **kwargs)


def _hidden(html, name):
    return Dom(html).one('input', id=name)['value']


def test_invoice_form_with_the_id_of_a_quote_is_the_empty_form_for_a_new_invoice(books):
    html = render(PageInvoice, books.db, {}, books.quote)
    assert _hidden(html, 'invoice_id') == '' and _hidden(html, 'is_edit_mode') == 'false'
    assert 'Neue Rechnung erstellen' in html
    assert QUOTE_NUMBER not in html and 'Guten Tag' not in html      # nichts vom Angebot im falschen Formular


def test_invoice_form_copy_of_a_quote_is_the_empty_form_too(books):
    html = render(PageInvoice, books.db, {}, None, copy_from=books.quote)
    assert _hidden(html, 'invoice_id') == '' and 'Kopie von' not in html
    assert QUOTE_NUMBER not in html and 'Anstrich' not in html


def test_quote_form_with_the_id_of_an_invoice_is_the_empty_form_for_a_new_quote(books):
    html = render(PageQuote, books.db, {}, books.invoice)
    assert _hidden(html, 'quote_id') == '' and _hidden(html, 'is_edit_mode') == 'false'
    assert 'Neues Angebot erstellen' in html
    assert 'Anstrich' not in html                        # die Position der Rechnung steht nicht im Formular


def test_forms_still_show_their_own_kind(books):
    invoice = render(PageInvoice, books.db, {}, books.invoice)
    quote = render(PageQuote, books.db, {}, books.quote)
    assert _hidden(invoice, 'invoice_id') == str(books.invoice) and _hidden(invoice, 'is_edit_mode') == 'true'
    assert _hidden(quote, 'quote_id') == str(books.quote) and _hidden(quote, 'is_edit_mode') == 'true'
    assert Dom(invoice).one('input', id='invoice_number')['value'] == INVOICE_NUMBER
    assert Dom(quote).one('input', id='quote_number')['value'] == QUOTE_NUMBER


@pytest.mark.parametrize('route, doc_attr, other_number', [
    pytest.param('/invoice/edit?id={}', 'quote', QUOTE_NUMBER, id='Rechnung-bearbeiten'),
    pytest.param('/invoice/view?id={}', 'quote', QUOTE_NUMBER, id='Rechnung-ansehen'),
    pytest.param('/invoice?id={}', 'quote', QUOTE_NUMBER, id='Rechnungsliste'),
    pytest.param('/quote/edit?id={}', 'invoice', INVOICE_NUMBER, id='Angebot-bearbeiten'),
    pytest.param('/quote?id={}', 'invoice', INVOICE_NUMBER, id='Angebotsliste'),
])
def test_forms_over_http_show_nothing_of_the_other_kind(server_port, books, route, doc_attr, other_number):
    resp = get(server_port, route.format(getattr(books, doc_attr)))
    assert resp.status == 200
    assert other_number not in resp.text


# ── Erzeugen: die falsche Belegart ergibt nichts ─────────────────────────────
def test_generating_with_the_wrong_kind_creates_nothing_and_keeps_the_stored_path(books):
    before = snapshot(books)
    assert generate_invoice_pdf(books.db, books.quote) == (None, None)
    assert generate_quote_pdf(books.db, books.invoice) == (None, None)
    result = generate_invoice_documents(books.db, books.quote, with_xml=True, overwrite_sent=True)
    assert result['error'] == 'Rechnung nicht gefunden.'
    assert result['pdf_path'] is None and result['xml_path'] is None
    assert snapshot(books) == before


def test_downloading_with_the_wrong_kind_reports_not_found(books):
    assert handlers.handle_invoice_document_download(books.quote, 'pdf', 'invoice') == (
        None, None, 'Rechnung nicht gefunden.')
    assert handlers.handle_invoice_document_download(books.invoice, 'pdf', 'quote') == (
        None, None, 'Angebot nicht gefunden.')
    assert handlers.handle_invoice_document_download(99999, 'pdf', 'invoice') == (
        None, None, 'Rechnung nicht gefunden.')


# ── get_document / document_type ─────────────────────────────────────────────
def test_get_document_returns_only_a_row_of_the_requested_kind(books):
    db = books.db
    assert db.get_document(books.invoice, 'invoice')[InvoiceCol.NUMBER] == INVOICE_NUMBER
    assert db.get_document(books.quote, 'quote')[InvoiceCol.NUMBER] == QUOTE_NUMBER
    assert db.get_document(books.quote, 'invoice') is None
    assert db.get_document(books.invoice, 'quote') is None
    assert db.get_document(99999, 'invoice') is None and db.get_document(99999, 'quote') is None
    assert db.get_document(books.invoice, 'gibt-es-nicht') is None


def test_document_type_of_a_row():
    row_of = lambda kind: tuple([None] * InvoiceCol.DOCUMENT_TYPE + [kind])
    assert document_type(row_of('quote')) == 'quote'
    assert document_type(row_of('invoice')) == 'invoice'
    # Altbestand: ohne Angabe (NULL, leer), ohne die Spalte (kurze Zeile) oder ohne Zeile gilt „Rechnung“
    assert document_type(row_of(None)) == 'invoice'
    assert document_type(row_of('')) == 'invoice'
    assert document_type(tuple(range(InvoiceCol.DOCUMENT_TYPE))) == 'invoice'
    assert document_type(()) == 'invoice'
    assert document_type(None) == 'invoice'


# ── InvoiceCol: Wächter ──────────────────────────────────────────────────────
# Spalte der Tabelle `Invoices`, auf die jede benannte Position zeigen muss
COLUMN_OF = {
    'ID': 'ID',
    'NUMBER': 'InvoiceNumber',
    'DATE': 'InvoiceDate',
    'BUYER_NAME': 'BuyerName',
    'BUYER_COMPANY': 'BuyerCompany',
    'DUE_DATE': 'PaymentDueDate',
    'SUM_GROSS': 'SumGross',
    'AMOUNT_DUE': 'AmountDue',
    'STATUS': 'Status',
    'DOCUMENT_TYPE': 'DocumentType',
}


def _constants():
    return {name: value for name, value in vars(InvoiceCol).items() if name.isupper()}


def test_every_invoicecol_constant_has_a_column_expectation():
    # Kommt eine Position hinzu, braucht sie hier eine Spalte – sonst bliebe sie ungeprüft
    assert set(_constants()) == set(COLUMN_OF)


def test_every_invoicecol_constant_points_to_its_column(tmp_db):
    with contextlib.closing(sqlite3.connect(tmp_db.db_name)) as con:
        names = [c[1] for c in con.execute('PRAGMA table_info(Invoices)')]       # Reihenfolge der Tabelle
        select_star = [c[0] for c in con.execute('SELECT * FROM Invoices').description]
    assert select_star == names                            # `SELECT *` liefert die Spalten in dieser Reihenfolge
    for constant, position in _constants().items():
        assert names[position] == COLUMN_OF[constant], f'InvoiceCol.{constant} = {position} -> {names[position]}'


def test_invoicecol_positions_read_the_right_values_from_a_real_row(books):
    db = books.db
    changes = {'Status': 'sent', 'PaymentDueDate': '2026-04-01', 'BuyerName': 'Erika Beispiel',
               'BuyerCompany': 'Beispiel AG', 'InvoiceDate': '2026-03-15'}
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        for column, value in changes.items():
            con.execute(f'UPDATE Invoices SET {column} = ? WHERE ID = ?', (value, books.invoice))
        con.commit()
    invoice = db.get_invoice_by_id(books.invoice)             # die Zeile, wie die Seiten sie bekommen
    assert invoice[InvoiceCol.ID] == books.invoice
    assert invoice[InvoiceCol.NUMBER] == INVOICE_NUMBER
    assert invoice[InvoiceCol.DATE] == '2026-03-15'
    assert invoice[InvoiceCol.BUYER_NAME] == 'Erika Beispiel'
    assert invoice[InvoiceCol.BUYER_COMPANY] == 'Beispiel AG'
    assert invoice[InvoiceCol.DUE_DATE] == '2026-04-01'
    assert invoice[InvoiceCol.STATUS] == 'sent'
    assert invoice[InvoiceCol.DOCUMENT_TYPE] == 'invoice'
    # Geldspalten sind in Euro (Decimal) umgerechnet: 100 netto + 19 % = 119
    assert invoice[InvoiceCol.SUM_GROSS] == Decimal('119')
    assert invoice[InvoiceCol.AMOUNT_DUE] == Decimal('119')
    assert books.db.get_invoice_by_id(books.quote)[InvoiceCol.DOCUMENT_TYPE] == 'quote'
