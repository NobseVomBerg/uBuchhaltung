# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Rechnungs- und Angebotsrouten des HTTP-Servers (2.0.7), gegen einen echten Server.

* Aktionen mit Seiteneffekt gibt es nur per POST (``/quote/delete``, ``/invoice/pdf_generate``,
  ``/quote/pdf_generate``): GET ergibt 405 und ändert nichts, POST braucht Herkunft und Token.
* Heruntergeladen wird nur, was abgelegt ist (``/quote/pdf_download``); ``/invoice/xml`` gibt es nicht
  mehr, ``/invoice/xml_download`` liefert die abgelegte Datei.
* Die acht JSON-Routen (``app._JSON_PATHS``) antworten auf unbrauchbare Bodys mit 400
  „Ungültige Anfrage.“ und danach weiter.
* Jede Antwort trägt ``X-Content-Type-Options: nosniff`` und ``X-Frame-Options: DENY`` – auch die
  Fehlerantwort auf eine kaputte Anfragezeile.
* „In Rechnung umwandeln“ (``POST /quote/convert``) schickt das CSRF-Token mit; ohne Token 403.
* ``/worktime/pdf`` (GET) schreibt nichts auf die Platte, ``/transactions?from_invoice=`` (GET) legt
  kein Konto an.

Abmelden (``POST /logout``) steht in test_http_guard.py, Belegtyp und Dokumentpfade in
test_document_type.py und test_document_paths.py. Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import datetime
import json
import os
import re
import socket
import sqlite3

import pytest

from db.invoices import InvoiceCol
from export.pdf_worktime import generate_worktime_pdf
from server import app as server_app
from server import handlers
from server.pages_quote import PageQuote
from server.pages_transactions import PageTransactions
from tests import installation
from tests.document_helpers import (CUSTOMER, INVOICE_NUMBER, PDF, PDF_PATH, QUOTE_NUMBER, STATUS,
                                    add_document, document_files, make_books, post_action, post_json,
                                    read_file, row, scalar, set_column, state, store_pdf, user_dir,
                                    write_file)
from tests.http_client import FOREIGN_ORIGIN, TIMEOUT, KeepAlive, get, own_origin, post, send

XML = b'<?xml version="1.0"?><Invoice>abgelegt</Invoice>'
WRITE_TABLES = ('Invoices', 'InvoiceItems', 'InvoicePayments', 'Bookings')


@pytest.fixture
def books(auto_login):
    return make_books(auto_login)


# ── Aktionen nur per POST ────────────────────────────────────────────────────
def test_get_on_quote_delete_is_405_and_keeps_the_quote(server_port, books):
    before = state(books.db)
    resp = get(server_port, f'/quote/delete?id={books.quote}')
    assert resp.status == 405
    assert resp.headers['Allow'] == 'POST'
    assert state(books.db) == before                    # Angebot, Rechnung und Positionen, Spalte für Spalte


def test_post_on_quote_delete_removes_that_quote_and_its_items_only(server_port, books):
    resp = post(server_port, f'/quote/delete?id={books.quote}', csrf=books.csrf,
                headers={'Origin': own_origin(server_port)})
    assert resp.status == 303 and resp.headers['Location'] == '/quote'
    now = state(books.db)
    assert [r[0] for r in now['Invoices']] == [books.invoice]
    assert [r[1] for r in now['InvoiceItems']] == [books.invoice]     # Spalte 1 = InvoiceId


@pytest.mark.parametrize('headers, with_token', [
    pytest.param({}, False, id='ohne-Token'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, True, id='fremde-Herkunft'),
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, True, id='fremde-Seite'),
])
def test_post_on_quote_delete_without_token_or_from_a_foreign_page_keeps_the_quote(
        server_port, books, headers, with_token):
    before = state(books.db)
    resp = post(server_port, f'/quote/delete?id={books.quote}', headers=headers,
                csrf=books.csrf if with_token else None)
    assert resp.status == 403
    assert state(books.db) == before


GENERATE = [
    pytest.param('/invoice/pdf_generate', 'invoice', INVOICE_NUMBER, 'invoices', id='Rechnung'),
    pytest.param('/quote/pdf_generate', 'quote', QUOTE_NUMBER, 'quotes', id='Angebot'),
]


@pytest.mark.parametrize('route, kind, number, folder', GENERATE)
def test_get_on_generate_path_is_405_and_creates_nothing(server_port, books, route, kind, number, folder):
    files, before = document_files(user_dir()), state(books.db)
    resp = get(server_port, f'{route}?id={getattr(books, kind)}')
    assert resp.status == 405
    assert resp.headers['Allow'] == 'POST'
    assert document_files(user_dir()) == files          # keine Datei, kein Ordner
    assert not os.path.exists(os.path.join(user_dir(), folder))
    assert state(books.db) == before                    # auch der Pfad in der Datenbank bleibt leer


@pytest.mark.parametrize('route, kind, number, folder', GENERATE)
def test_post_on_generate_path_stores_the_document_and_answers_with_json(
        server_port, books, route, kind, number, folder):
    doc = getattr(books, kind)
    resp = post_action(server_port, f'{route}?id={doc}', books.csrf,
                       headers={'Origin': own_origin(server_port)})
    assert resp.status == 200
    assert resp.headers['Content-Type'].startswith('application/json')
    payload = json.loads(resp.text)
    assert payload['success'] is True
    path = payload['pdf_path']
    assert os.path.basename(path) == f'{number} {CUSTOMER}.pdf'
    assert os.path.dirname(os.path.dirname(path)) == os.path.join(user_dir(), folder)
    assert read_file(path).startswith(b'%PDF')
    assert row(books.db, doc)[PDF_PATH] == path                           # die Datenbank kennt die Datei


@pytest.mark.parametrize('route, kind, number, folder', GENERATE)
@pytest.mark.parametrize('headers, with_token', [
    pytest.param({}, False, id='ohne-Token'),
    pytest.param({'X-CSRF-Token': 'falsch'}, False, id='falsches-Token'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, True, id='fremde-Herkunft'),
    pytest.param({'Sec-Fetch-Site': 'same-site'}, True, id='fremde-Seite'),
])
def test_post_on_generate_path_without_token_or_from_a_foreign_page_creates_nothing(
        server_port, books, route, kind, number, folder, headers, with_token):
    files, before = document_files(user_dir()), state(books.db)
    hdrs = {**headers, **({'X-CSRF-Token': books.csrf} if with_token else {})}
    resp = send(server_port, 'POST', f'{route}?id={getattr(books, kind)}', hdrs, b'')
    assert resp.status == 403
    assert document_files(user_dir()) == files
    assert state(books.db) == before


@pytest.mark.parametrize('route, kind, number, folder', GENERATE)
def test_generate_without_a_valid_id_is_400(server_port, books, route, kind, number, folder):
    files = document_files(user_dir())
    for query in ('', '?id=', '?id=abc', '?id=1.5', '?nummer=1'):
        resp = post_action(server_port, route + query, books.csrf)
        assert resp.status == 400, query
        assert json.loads(resp.text) == {'success': False, 'error': 'Ungültige oder fehlende ID'}, query
    assert document_files(user_dir()) == files


@pytest.mark.parametrize('route, kind, number, folder', GENERATE)
def test_generate_for_an_unknown_id_is_404_and_creates_nothing(
        server_port, books, route, kind, number, folder):
    files = document_files(user_dir())
    resp = post_action(server_port, f'{route}?id=99999', books.csrf)
    assert resp.status == 404
    expected = 'Angebot nicht gefunden.' if kind == 'quote' else 'Rechnung nicht gefunden.'
    assert json.loads(resp.text) == {'success': False, 'error': expected}
    assert document_files(user_dir()) == files


# ── Angebots-PDF: nur die abgelegte Datei ────────────────────────────────────
def test_quote_download_without_a_stored_pdf_is_404_with_plain_text_and_creates_nothing(server_port, books):
    files = document_files(user_dir())
    resp = get(server_port, f'/quote/pdf_download?id={books.quote}')
    assert resp.status == 404
    assert resp.headers['Content-Type'].startswith('text/plain')
    assert resp.text == 'Kein PDF vorhanden – bitte zuerst erzeugen.'
    assert document_files(user_dir()) == files                       # nichts erzeugt, nicht einmal ein Ordner
    assert not os.path.exists(books.quotes)


def test_quote_download_delivers_the_stored_file_unchanged(server_port, books):
    path = store_pdf(books, 'quote')
    resp = get(server_port, f'/quote/pdf_download?id={books.quote}')
    assert resp.status == 200
    assert resp.headers['Content-Type'] == 'application/pdf'
    assert resp.body == PDF                                           # genau die abgelegte Fassung
    assert resp.headers['Content-Disposition'].startswith('attachment;')
    assert f'{QUOTE_NUMBER} {CUSTOMER}.pdf' in resp.headers['Content-Disposition']
    assert resp.headers['X-Filename'] == f'{QUOTE_NUMBER}%20{CUSTOMER.replace(" ", "%20")}.pdf'
    assert read_file(path) == PDF                                     # und wurde nicht neu geschrieben


def test_quote_download_with_inline_opens_in_the_browser(server_port, books):
    store_pdf(books, 'quote')
    resp = get(server_port, f'/quote/pdf_download?id={books.quote}&inline=1')
    assert resp.status == 200 and resp.headers['Content-Disposition'].startswith('inline;')


def test_quote_download_finds_the_newest_file_by_name_when_the_database_path_is_missing(server_port, books):
    # Das Jahr im Ordnernamen ist das der Erzeugung, das der Datensatz nicht kennt: der neueste Treffer zählt
    name = f'{QUOTE_NUMBER} {CUSTOMER}.pdf'
    write_file(os.path.join(books.quotes, '2024', name), b'%PDF-1.4 alt')
    write_file(os.path.join(books.quotes, '2025', name), b'%PDF-1.4 neu')
    resp = get(server_port, f'/quote/pdf_download?id={books.quote}')
    assert resp.status == 200 and resp.body == b'%PDF-1.4 neu'


# ── /invoice/xml gibt es nicht mehr, /invoice/xml_download bleibt ───────────
def test_invoice_xml_route_is_gone_and_generates_nothing(server_port, books):
    # E-Rechnungsdaten sind da: die frühere Route hätte das XML frisch gebaut und ausgeliefert
    set_column(books.db, books.invoice, 'BuyerRouteID', '991-12345-67')
    files = document_files(user_dir())
    resp = get(server_port, f'/invoice/xml?id={books.invoice}')
    assert resp.status == 404
    assert 'xml' not in resp.headers['Content-Type'] and b'<Invoice' not in resp.body
    assert document_files(user_dir()) == files


def test_invoice_xml_download_delivers_the_stored_file_only(server_port, books):
    missing = get(server_port, f'/invoice/xml_download?id={books.invoice}')
    assert missing.status == 404 and missing.text == 'Kein XRechnung (XML) vorhanden.'
    path = write_file(os.path.join(books.invoices, '2026', f'{INVOICE_NUMBER} {CUSTOMER}.xml'), XML)
    books.db.update_invoice_xml_path(books.invoice, path)
    resp = get(server_port, f'/invoice/xml_download?id={books.invoice}')
    assert resp.status == 200
    assert resp.headers['Content-Type'] == 'application/xml'
    assert resp.body == XML
    assert read_file(path) == XML


# ── Angebot in Rechnung umwandeln: POST mit CSRF-Token ───────────────────────
def test_convert_with_token_creates_the_invoice_and_marks_the_quote(server_port, books):
    year = datetime.date.today().year
    books.db.insert_number_range('invoice', year, 'R', current_number=40)
    resp = post(server_port, '/quote/convert', form={'quote_id': str(books.quote)}, csrf=books.csrf,
                headers={'Origin': own_origin(server_port)})
    assert resp.status == 303
    new_id = int(resp.headers['Location'].rsplit('id=', 1)[1])
    assert resp.headers['Location'] == f'/invoice?id={new_id}'
    new = books.db.get_invoice_by_id(new_id)
    assert new[InvoiceCol.DOCUMENT_TYPE] == 'invoice' and new[49] == books.quote      # Spalte 49 = SourceQuoteId
    assert new[InvoiceCol.NUMBER] == f'{str(year)[2:]}R041'                           # nächste Nummer des Kreises
    assert row(books.db, books.quote)[STATUS] == 'converted'                       # Status des Angebots


@pytest.mark.parametrize('headers, with_token', [
    pytest.param({}, False, id='ohne-Token'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, True, id='fremde-Herkunft'),
])
def test_convert_without_token_or_from_a_foreign_origin_is_403_and_changes_nothing(
        server_port, books, headers, with_token):
    before = state(books.db)
    resp = post(server_port, '/quote/convert', form={'quote_id': str(books.quote)}, headers=headers,
                csrf=books.csrf if with_token else None)
    assert resp.status == 403
    assert state(books.db) == before


def test_convert_buttons_post_through_the_helper_that_adds_the_token(books):
    # Beide Skripte (Liste und Formular) rufen appPost mit dem Feld auf – kein selbst gebautes
    # Formular ohne CSRF-Feld
    html = PageQuote(books.db, {}, books.quote)
    assert re.findall(r"appPost\('/quote/convert', \{quote_id: (\w+)\}\)", html) == ['quoteId', 'id']
    assert "f.action = '/quote/convert'" not in html and "f.action='/quote/convert'" not in html
    # Der Helfer im Seitenfuß hängt die Felder und das Token (Meta-Tag) an das Formular
    assert 'function appPost(url, fields)' in html
    assert "meta[name=\"csrf-token\"]" in html


# ── Zahlungszuordnung entfernen: keine Interna in der Antwort ────────────────
def test_deleting_an_unknown_payment_does_not_leak_the_exception_text(server_port, books, capsys):
    before = state(books.db, *WRITE_TABLES)
    resp = post_json(server_port, '/invoice/delete-payment', {'payment_id': 99999}, books.csrf)
    assert resp.status == 500
    assert json.loads(resp.text) == {'success': False,
                                     'error': 'Die Zahlungszuordnung konnte nicht entfernt werden.'}
    assert 'InvoicePayment' not in resp.text and 'not found' not in resp.text
    assert 'InvoicePayment 99999 not found' in capsys.readouterr().out         # steht im Server-Log
    assert state(books.db, *WRITE_TABLES) == before


def _raw_exchange(port, data):
    """Rohdaten an den Server schicken und alles lesen, bis er die Verbindung schließt."""
    with socket.create_connection(('127.0.0.1', port), timeout=TIMEOUT) as sock:
        sock.sendall(data)
        raw = b''
        while chunk := sock.recv(65536):
            raw += chunk
    head, _, body = raw.partition(b'\r\n\r\n')
    status_line, *header_lines = head.split(b'\r\n')
    headers = [tuple(line.decode('latin-1').split(': ', 1)) for line in header_lines]
    return int(status_line.split()[1]), headers, body


# ── JSON-Routen: unbrauchbare Bodys ergeben 400, der Server antwortet weiter ──
JSON_ROUTES = sorted(server_app._JSON_PATHS)
HANDLER = {                     # Route -> Handler, den `_post_json_route` aufruft
    '/invoice/save': 'handle_invoice_save',
    '/invoice/send-email': 'handle_send_invoice_email',
    '/invoice/link-payment': 'handle_link_invoice_payment',
    '/invoice/delete-payment': 'handle_delete_invoice_payment',
    '/invoice/status': 'handle_update_invoice_status',
    '/invoice/cancel': 'handle_invoice_cancel',
    '/quote/save': 'handle_quote_save',
    '/quote/status': 'handle_update_quote_status',
}
# Feld, das die Beleg-/Zahlungs-ID trägt (die Speichern-Routen lesen invoiceId/quoteId optional)
ID_FIELD = {'/invoice/send-email': 'invoice_id', '/invoice/link-payment': 'invoice_id',
            '/invoice/delete-payment': 'payment_id', '/invoice/status': 'invoice_id',
            '/quote/status': 'quote_id'}
INVALID_REQUEST = {'success': False, 'error': 'Ungültige Anfrage.'}


def test_json_route_expectations_cover_all_json_paths():
    # Kommt eine JSON-Route hinzu, braucht sie hier einen Handler und eine Erwartung
    assert set(HANDLER) == set(server_app._JSON_PATHS)
    assert len(JSON_ROUTES) == 8


UNUSABLE_BODIES = {
    'kaputtes JSON': b'{',
    'leer': b'',
    'leeres Array': b'[]',
    'Array': b'[1, 2]',
    'Zeichenkette': b'"text"',
    'null': b'null',
    'Zahl': b'42',
    'kein UTF-8': b'\xff\xfe\x00{}',
    'tief verschachtelt': b'[' * 100000 + b']' * 100000,
}


@pytest.mark.parametrize('route', JSON_ROUTES)
def test_unusable_json_body_is_400_and_changes_nothing(server_port, books, route):
    before = state(books.db, *WRITE_TABLES)
    for name, body in UNUSABLE_BODIES.items():
        resp = post_json(server_port, route, body, books.csrf)
        assert resp.status == 400, name
        assert resp.headers['Content-Type'].startswith('application/json'), name
        assert json.loads(resp.text) == INVALID_REQUEST, name
        assert get(server_port, '/about').status == 200, name              # der Server antwortet weiter
    assert state(books.db, *WRITE_TABLES) == before


# `{}` ist ein gültiges JSON-Objekt: Routen mit Pflicht-ID lehnen es als unbrauchbar ab (400
# „Ungültige Anfrage.“), die Speichern-Routen melden die fehlenden Formularfelder wie ein Formular
# (200, Fehlertext für die Anzeige), link-payment nennt „Ungültige Parameter“.
EMPTY_OBJECT = {
    '/invoice/status': (400, 'Ungültige Anfrage.'),
    '/invoice/send-email': (400, 'Ungültige Anfrage.'),
    '/invoice/delete-payment': (400, 'Ungültige Anfrage.'),
    '/invoice/cancel': (400, 'Ungültige Anfrage.'),
    '/quote/status': (400, 'Ungültige Anfrage.'),
    '/invoice/link-payment': (400, 'Ungültige Parameter'),
    '/invoice/save': (200, 'Pflichtfelder fehlen'),
    '/quote/save': (200, 'Pflichtfelder fehlen'),
}


@pytest.mark.parametrize('route', JSON_ROUTES)
def test_empty_json_object_is_answered_with_a_json_error_and_changes_nothing(server_port, books, route):
    before = state(books.db, *WRITE_TABLES)
    resp = post_json(server_port, route, {}, books.csrf)
    status, error = EMPTY_OBJECT[route]
    assert (resp.status, json.loads(resp.text)) == (status, {'success': False, 'error': error})
    assert state(books.db, *WRITE_TABLES) == before


NON_NUMERIC_IDS = ['"abc"', '"1.5x"', '[1]', '{"a": 1}', 'true', 'NaN', 'Infinity']


@pytest.mark.parametrize('route', sorted(ID_FIELD))
def test_non_numeric_id_is_400(server_port, books, route):
    before = state(books.db, *WRITE_TABLES)
    for bad_id in NON_NUMERIC_IDS:
        # Alle übrigen Felder sind in Ordnung – nur die ID ist keine Ganzzahl
        body = ('{"%s": %s, "status": "sent", "recipient_email": "kunde@example.com", '
                '"transaction_id": 1}' % (ID_FIELD[route], bad_id))
        resp = post_json(server_port, route, body, books.csrf)
        assert (resp.status, json.loads(resp.text)) == (400, INVALID_REQUEST), bad_id
    assert state(books.db, *WRITE_TABLES) == before


@pytest.mark.parametrize('route, field, kind', [
    pytest.param('/invoice/status', 'invoice_id', 'invoice', id='Rechnung'),
    pytest.param('/quote/status', 'quote_id', 'quote', id='Angebot'),
])
def test_status_route_without_a_usable_status_is_rejected(server_port, books, route, field, kind):
    before = state(books.db)
    for status in (None, 5, ['sent'], {'a': 1}):                 # kein Text: unbrauchbare Anfrage
        resp = post_json(server_port, route, {field: getattr(books, kind), 'status': status}, books.csrf)
        assert (resp.status, json.loads(resp.text)) == (400, INVALID_REQUEST), status
    resp = post_json(server_port, route, {field: getattr(books, kind), 'status': 'gibt-es-nicht'}, books.csrf)
    assert resp.status == 400 and json.loads(resp.text)['success'] is False     # unbekannter Status
    assert state(books.db) == before


def test_numeric_ids_in_text_form_are_accepted(server_port, books):
    # Das Frontend schickt IDs als Zahl, ein Skript als Text: beides ist eine gültige ID
    resp = post_json(server_port, '/quote/status', {'quote_id': str(books.quote), 'status': 'accepted'},
                     books.csrf)
    assert resp.status == 200 and json.loads(resp.text)['success'] is True
    assert row(books.db, books.quote)[STATUS] == 'accepted'


@pytest.mark.parametrize('length', ['abc', '-1', '1.5', ''])
def test_invalid_content_length_is_400_and_closes_the_connection(server_port, books, length):
    resp = send(server_port, 'POST', '/invoice/status',
                {'X-CSRF-Token': books.csrf, 'Content-Length': length}, None)
    assert resp.status == 400
    assert json.loads(resp.text) == INVALID_REQUEST
    assert resp.headers['Connection'] == 'close'                      # ein etwaiger Body bliebe ungelesen
    assert get(server_port, '/about').status == 200


def test_missing_content_length_is_400(server_port, books):
    request = (f'POST /invoice/status HTTP/1.1\r\nHost: 127.0.0.1:{server_port}\r\n'
               f'X-CSRF-Token: {books.csrf}\r\nConnection: close\r\n\r\n').encode()
    code, headers, body = _raw_exchange(server_port, request)
    assert code == 400 and json.loads(body) == INVALID_REQUEST
    assert get(server_port, '/about').status == 200


def test_connection_stays_usable_after_an_unusable_body(server_port, books):
    # Der Body wird vollständig gelesen: die nächste Anfrage auf derselben Verbindung geht durch
    headers = {'Content-Type': 'application/json', 'X-CSRF-Token': books.csrf}
    with KeepAlive(server_port) as conn:
        bad = conn.request('POST', '/invoice/save', headers, body=b'{ "invoiceNumber": ' + b'x' * 5000)
        assert bad.status == 400 and json.loads(bad.text) == INVALID_REQUEST
        good = conn.get('/about')
        assert good.status == 200 and 'uBuchhaltung' in good.text


@pytest.mark.parametrize('route', JSON_ROUTES)
def test_unexpected_error_is_500_with_a_short_json_message_and_the_server_goes_on(
        server_port, books, monkeypatch, capsys, route):
    def boom(*args, **kwargs):
        raise RuntimeError('geheimer Interna-Text /home/niemand/buch.db')
    monkeypatch.setattr(handlers, HANDLER[route], boom)
    resp = post_json(server_port, route, {'invoice_id': books.invoice}, books.csrf)
    assert resp.status == 500
    payload = json.loads(resp.text)
    assert payload['success'] is False
    assert payload['error'] == 'Interner Fehler – Einzelheiten stehen im Server-Log.'
    assert 'geheimer' not in resp.text and 'Traceback' not in resp.text and 'niemand' not in resp.text
    assert 'geheimer Interna-Text' in capsys.readouterr().err        # der Traceback steht im Server-Log
    assert get(server_port, '/about').status == 200


# ── Jede Antwort trägt die Schutz-Header ─────────────────────────────────────
def _pdf_download(port, books):
    store_pdf(books, 'invoice')
    return get(port, f'/invoice/pdf_download?id={books.invoice}')


RESPONSES = [
    pytest.param(lambda p, b: get(p, '/about'), 200, id='HTML-Seite'),
    pytest.param(lambda p, b: get(p, '/invoice'), 200, id='Rechnungsliste'),
    pytest.param(lambda p, b: get(p, '/buch.css'), 200, id='Stylesheet'),
    pytest.param(lambda p, b: get(p, '/gibt-es-nicht'), 404, id='404-Seite'),
    pytest.param(lambda p, b: get(p, f'/invoice/pdf_download?id={b.invoice}'), 404,
                 id='404-Klartext-ohne-PDF'),
    pytest.param(_pdf_download, 200, id='PDF'),
    pytest.param(lambda p, b: get(p, f'/invoice/xml?id={b.invoice}'), 404, id='entfernte-Route'),
    pytest.param(lambda p, b: post_json(p, '/invoice/status', {'invoice_id': 99999, 'status': 'sent'},
                                        b.csrf), 404, id='JSON-404'),
    pytest.param(lambda p, b: post_json(p, '/invoice/status', b'{', b.csrf), 400, id='JSON-400'),
    pytest.param(lambda p, b: post_action(p, f'/invoice/pdf_generate?id={b.invoice}', b.csrf), 200,
                 id='JSON-200'),
    pytest.param(lambda p, b: post_action(p, f'/quote/delete?id={b.quote}', b.csrf), 303,
                 id='Weiterleitung'),
    pytest.param(lambda p, b: get(p, f'/quote/delete?id={b.quote}'), 405, id='405'),
    pytest.param(lambda p, b: post(p, f'/quote/delete?id={b.quote}', csrf=b.csrf,
                                   headers={'Origin': FOREIGN_ORIGIN}), 403, id='403-fremde-Herkunft'),
    pytest.param(lambda p, b: post(p, f'/quote/delete?id={b.quote}'), 403, id='403-ohne-Token'),
    pytest.param(lambda p, b: get(p, '/invoice/reminders'), 200, id='Mahnwesen'),
]


@pytest.mark.parametrize('make_request, status', RESPONSES)
def test_every_response_carries_the_protection_headers(server_port, books, make_request, status):
    resp = make_request(server_port, books)
    assert resp.status == status
    # genau einmal: kein Weg schreibt sie zusätzlich selbst
    assert resp.headers.get_all('X-Content-Type-Options') == ['nosniff']
    assert resp.headers.get_all('X-Frame-Options') == ['DENY']


# Die Anfragezeile wird mit CRLF, aber ohne Leerzeile geschickt, und die überlange ist genau 65537 Bytes
# lang (die Grenze des Servers): So bleibt im Server nichts ungelesen, er schließt mit FIN statt mit RST, und
# die Antwort geht nie verloren.
OVERLONG_LINE = b'GET /' + b'a' * (65537 - len(b'GET / HTTP/1.1\r\n')) + b' HTTP/1.1'


@pytest.mark.parametrize('request_line, status', [
    pytest.param(b'XYZ', 400, id='ein-Wort'),
    pytest.param(b'GET', 400, id='nur-Methode'),
    pytest.param(b'GET / HTTP/1.1 extra', 400, id='vier-Wörter'),
    pytest.param(b'GET / HTTP/x.y', 400, id='kaputte-Version'),
    pytest.param(b'GET / HTTP/9.9', 505, id='unbekannte-Version'),
    pytest.param(b'\x00\x01\x02\xff', 400, id='Binärmüll'),
    pytest.param(OVERLONG_LINE, 414, id='überlange-Zeile'),
])
def test_a_broken_request_line_gets_an_answer_with_the_protection_headers(server_port, books,
                                                                           request_line, status):
    # Die Anfrage scheitert, bevor `path` gesetzt ist: früher brach `end_headers` ohne Antwort ab
    code, headers, _ = _raw_exchange(server_port, request_line + b'\r\n')
    assert code == status
    assert ('X-Frame-Options', 'DENY') in headers
    assert ('X-Content-Type-Options', 'nosniff') in headers
    assert ('Connection', 'close') in headers
    # und der Server bedient danach weitere Anfragen
    assert get(server_port, '/about').status == 200


# ── /worktime/pdf (GET) schreibt nichts ──────────────────────────────────────
@pytest.fixture
def person(auto_login):
    db = auto_login.db
    pid = db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                            first_name='Erika', last_name='Probe')
    db.insert_worktime(pid, '2026-03-10', start_time='08:00', end_time='16:00')
    return pid


def test_worktime_pdf_download_writes_nothing_to_disk(server_port, person):
    files = document_files(user_dir())
    resp = get(server_port, f'/worktime/pdf?person={person}&from=2026-03-01&to=2026-03-31')
    assert resp.status == 200
    assert resp.headers['Content-Type'] == 'application/pdf' and resp.body.startswith(b'%PDF')
    assert 'Stundenzettel_Erika_Probe_2026-03.pdf' in resp.headers['Content-Disposition']
    assert document_files(user_dir()) == files                       # keine PDF, kein Ordner `worktime`
    assert not os.path.exists(os.path.join(user_dir(), 'worktime'))


def test_worktime_pdf_with_store_false_names_the_file_but_does_not_write_it(tmp_db):
    pid = tmp_db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                                first_name='Erika', last_name='Probe')
    pdf, path = generate_worktime_pdf(tmp_db, pid, '2026-03-01', '2026-03-31', store=False)
    assert pdf.startswith(b'%PDF')
    assert os.path.basename(path) == 'Stundenzettel_Erika_Probe_2026-03.pdf'   # daraus wird der Dateiname
    assert not os.path.exists(path) and not os.path.exists(os.path.dirname(path))


def test_worktime_pdf_is_stored_by_default(tmp_db):
    # Gegenprobe zum Test davor: Ohne `store=False` legt die Funktion die Datei weiterhin ab
    pid = tmp_db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                                first_name='Erika', last_name='Probe')
    pdf, path = generate_worktime_pdf(tmp_db, pid, '2026-03-01', '2026-03-31')
    assert os.path.isfile(path) and read_file(path) == pdf


# ── /transactions?from_invoice= (GET) legt kein Konto an ─────────────────────
def _without_tax_free_revenue_account(db):
    """Datenbank ohne das Standardkonto 4185 (wie eine ältere Datenbank, die es noch nicht kennt)."""
    with sqlite3.connect(db.db_name) as con:
        con.execute('DELETE FROM ChartOfAccounts WHERE AccountNumber = 4185')
    assert db.get_coa_id_by_account_number(4185) is None


def _tax_free_open_invoice(db):
    return add_document(db, '26R050', 'invoice', status='sent', tax_rate=-1, tax_amount=0,
                        sum_gross=100.0, amount_due=100.0)


def test_showing_the_payment_form_for_an_invoice_does_not_create_an_account(server_port, books):
    invoice = _tax_free_open_invoice(books.db)
    _without_tax_free_revenue_account(books.db)
    accounts = scalar(books.db, 'SELECT COUNT(*) FROM ChartOfAccounts')
    dump = installation.dump(books.db.db_name)
    resp = get(server_port, f'/transactions?from_invoice={invoice}')
    assert resp.status == 200 and 'Zahlung zu Rechnung 26R050' in resp.text     # die Vorbelegung steht
    assert scalar(books.db, 'SELECT COUNT(*) FROM ChartOfAccounts') == accounts
    assert books.db.get_coa_id_by_account_number(4185) is None
    assert installation.dump(books.db.db_name) == dump                           # Anzeigen ändert nichts


def test_payment_form_page_does_not_create_an_account(tmp_db):
    invoice = _tax_free_open_invoice(tmp_db)
    _without_tax_free_revenue_account(tmp_db)
    accounts = scalar(tmp_db, 'SELECT COUNT(*) FROM ChartOfAccounts')
    html = PageTransactions(tmp_db, from_invoice=invoice)
    assert 'Zahlung zu Rechnung 26R050' in html
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM ChartOfAccounts') == accounts


def test_resolve_revenue_coa_creates_the_account_only_when_asked_to(tmp_db):
    _without_tax_free_revenue_account(tmp_db)
    accounts = scalar(tmp_db, 'SELECT COUNT(*) FROM ChartOfAccounts')
    assert tmp_db.resolve_revenue_coa(0, create=False) is None           # steuerfrei: nachschlagen, nichts anlegen
    assert tmp_db.resolve_revenue_coa(-1, create=False) is None          # Kleinunternehmer (§ 19) genauso
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM ChartOfAccounts') == accounts
    created = tmp_db.resolve_revenue_coa(-1)                             # Vorgabe: legt es an (Speichern der Zuordnung)
    assert created == tmp_db.get_coa_id_by_account_number(4185) and created is not None
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM ChartOfAccounts') == accounts + 1
    assert tmp_db.resolve_revenue_coa(0, create=False) == created        # jetzt gibt es das Konto zum Nachschlagen


@pytest.mark.parametrize('rate, number', [(0.19, 4400), (19, 4400), (0.07, 4300)])
def test_resolve_revenue_coa_without_create_still_finds_the_standard_accounts(tmp_db, rate, number):
    assert tmp_db.resolve_revenue_coa(rate, create=False) == tmp_db.get_coa_id_by_account_number(number)
