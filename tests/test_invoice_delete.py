# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Entwurf löschen (2.1.2, T91): ``POST /invoice/delete?id=``.

* Gelöscht wird nur ein Entwurf ohne zugeordnete Zahlung und ohne Forderungsbuchung – in EINER
  Transaktion samt Positionen; abgeschlossen, versendet, bezahlt und storniert nie. Eine Ablehnung
  geht mit dem Grund zurück auf die Rechnung (``err``), nichts ändert sich.
* Die Rechnungsnummer geht an den Nummernkreis zurück (die letzte; eine mittlere lässt eine Lücke –
  test_invoice_numbering.py), ein Quellangebot ist wieder umwandelbar.
* PDF und XML des Entwurfs werden nach dem Löschen entfernt – nur im Dokumentordner und nur, wenn kein
  anderer Beleg auf die Datei verweist.
* Aktion nur per POST (GET → 405, ohne Token 403); die ID eines Angebots ergibt 404.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import json
import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest

import server.handlers as handlers
import userctx
from db.invoice_lock import STATUS_LABELS
from export.invoice_files import generate_invoice_documents
from server.pages_invoice import PageInvoice
from tests.document_helpers import (document_files, post_action, post_json, read_file, row, state,
                                    stored_documents, user_dir, write_file)
from tests.dom_helpers import Dom, app_msg_arguments
from tests.http_client import FOREIGN_ORIGIN, get, own_origin, post
from tests.invoice_world import (add_payment, build_world, columns, force_status, make_invoice, make_payload,
                                 snapshot)

NOT_DRAFTS = [status for status in STATUS_LABELS if status != 'draft']


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return build_world(tmp_db)


@pytest.fixture
def books(auto_login):
    w = build_world(auto_login.db)
    w.csrf = auto_login.csrf
    return w


def with_documents(w, invoice_id):
    """PDF und XML des Entwurfs (die Bestellnummer macht es zur Rechnung mit E-Rechnungsdaten)."""
    result = generate_invoice_documents(w.db, invoice_id)
    assert result['error'] is None and result['xml_path']
    return result['pdf_path'], result['xml_path']


def delete(w, invoice_id):
    return handlers.handle_delete_invoice(w.db, invoice_id)


def counter(db):
    return db.fetch_number_ranges('invoice')[0][5]


def err_of(location):
    return parse_qs(urlsplit(location).query)['err'][0]


# ── Ein Entwurf geht ─────────────────────────────────────────────────────────

def test_a_draft_is_deleted_with_its_items_its_documents_and_its_number(world):
    db = world.db
    db.insert_number_range('invoice', 2026, 'R', current_number=0)
    invoice_id = make_invoice(world, 'draft', number=db.peek_next_number('invoice', 2026)[0])
    other = make_invoice(world, 'draft', number='RE-500', orderNumber=None)      # eigene Nummer, kein Kreis
    pdf, xml = with_documents(world, invoice_id)
    other_pdf = generate_invoice_documents(db, other)['pdf_path']
    assert counter(db) == 1
    assert delete(world, invoice_id) == (303, '/invoice')
    assert row(db, invoice_id) is None and db.get_invoice_items(invoice_id) == []
    assert not os.path.exists(pdf) and not os.path.exists(xml)
    assert counter(db) == 0                                         # die letzte Nummer ist wieder frei
    assert row(db, other) is not None and os.path.isfile(other_pdf)  # die andere Rechnung bleibt, samt Datei


def test_the_documents_of_a_draft_without_a_stored_path_go_too(world):
    """Der Pfad in der Datenbank fehlt (Altbestand): die Datei am Namensschema gehört trotzdem dazu."""
    invoice_id = make_invoice(world, 'draft')
    pdf, _xml = with_documents(world, invoice_id)
    world.db.update_invoice_pdf_path(invoice_id, None)
    assert delete(world, invoice_id) == (303, '/invoice')
    assert not os.path.exists(pdf)


def test_a_file_that_another_document_points_to_stays(world):
    db = world.db
    first = make_invoice(world, 'draft')
    pdf, xml = with_documents(world, first)
    second = make_invoice(world, 'sent', number='26R101')
    db.update_invoice_pdf_path(second, pdf)                          # die PDF gehört (auch) einem anderen Beleg
    assert delete(world, first) == (303, '/invoice')
    assert os.path.isfile(pdf) and not os.path.exists(xml)


def test_a_stored_path_outside_the_document_folder_is_never_deleted(world, tmp_path):
    invoice_id = make_invoice(world, 'draft')
    outside = write_file(str(tmp_path / 'fremd' / 'wichtig.pdf'), b'%PDF-1.4 nicht anfassen')
    world.db.update_invoice_pdf_path(invoice_id, outside)
    assert delete(world, invoice_id) == (303, '/invoice')
    assert read_file(outside) == b'%PDF-1.4 nicht anfassen'
    assert row(world.db, invoice_id) is None


# ── Alles andere bleibt ──────────────────────────────────────────────────────

@pytest.mark.parametrize('status', NOT_DRAFTS)
def test_only_a_draft_can_be_deleted(world, status):
    invoice_id = make_invoice(world, 'draft')
    pdf, xml = with_documents(world, invoice_id)
    force_status(world.db, invoice_id, status)
    before, files = snapshot(world.db), document_files(userctx.DATA_ROOT)
    code, location = delete(world, invoice_id)
    assert code == 303 and location.startswith(f'/invoice?id={invoice_id}&err=')
    assert err_of(location)
    assert ('Entwurf' in err_of(location)) and snapshot(world.db) == before
    assert document_files(userctx.DATA_ROOT) == files and os.path.isfile(pdf) and os.path.isfile(xml)


def test_a_finalized_invoice_says_to_set_it_back_to_draft_first(world):
    invoice_id = make_invoice(world, 'finalized')
    assert 'zurücksetzen' in err_of(delete(world, invoice_id)[1])


def test_a_draft_with_a_payment_is_kept(world):
    invoice_id = make_invoice(world, 'draft')
    add_payment(world, invoice_id)
    force_status(world.db, invoice_id, 'draft')           # die Zuordnung setzt den Status; so bliebe ein Bestand zurück
    before = snapshot(world.db)
    code, location = delete(world, invoice_id)
    assert code == 303 and err_of(location) == 'Zu dieser Rechnung sind Zahlungen zugeordnet.'
    assert snapshot(world.db) == before


def test_a_draft_with_a_receivable_is_kept(world):
    invoice_id = make_invoice(world, 'draft')
    assert world.db.ensure_receivable_booking(invoice_id) is not None
    before = snapshot(world.db)
    code, location = delete(world, invoice_id)
    assert code == 303 and err_of(location) == 'Zu dieser Rechnung gibt es eine Forderungsbuchung.'
    assert snapshot(world.db) == before


def test_a_failed_delete_leaves_the_number_range_alone(world):
    db = world.db
    db.insert_number_range('invoice', 2026, 'R', current_number=0)
    invoice_id = make_invoice(world, 'draft', number=db.peek_next_number('invoice', 2026)[0])
    force_status(db, invoice_id, 'sent')
    delete(world, invoice_id)
    assert counter(db) == 1 and row(db, invoice_id) is not None


def test_the_refusal_is_a_plain_message_on_the_invoice_page(world):
    invoice_id = make_invoice(world, 'sent')
    location = delete(world, invoice_id)[1]
    html = PageInvoice(world.db, {}, invoice_id, err=err_of(location))
    assert app_msg_arguments(Dom(html)) == [err_of(location), 'error']
    hostile = PageInvoice(world.db, {}, invoice_id, err='</script><img src=x onerror=alert(1)>')
    assert '</script><img' not in hostile and app_msg_arguments(Dom(hostile))[0] == '</script><img src=x onerror=alert(1)>'


# ── Angebot: zurück auf „angenommen“ ─────────────────────────────────────────

def quote_with_invoice(w):
    db = w.db
    quote_id = db.insert_invoice({
        'invoice_number': '26A001', 'invoice_date': '2026-06-09', 'seller_name': 'S',
        'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
        'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0, 'amount_due': 119.0,
        'status': 'sent', 'document_type': 'quote', 'customer_id': w.cust, 'own_company_id': w.own})
    db.insert_invoice_item({'invoice_id': quote_id, 'position': 1, 'description': 'Position', 'quantity': 1,
                            'unit': 'C62', 'price_per_unit': 100.0, 'total_net': 100.0, 'tax_rate': 0.19})
    return quote_id, db.convert_quote_to_invoice(quote_id)


def test_deleting_the_draft_of_a_quote_makes_the_quote_convertible_again(world):
    db = world.db
    quote_id, invoice_id = quote_with_invoice(world)
    assert columns(db, quote_id)['Status'] == 'converted'
    assert delete(world, invoice_id) == (303, '/invoice')
    assert columns(db, quote_id)['Status'] == 'accepted'
    again = db.convert_quote_to_invoice(quote_id)                    # und lässt sich wieder umwandeln
    assert again and again != invoice_id and columns(db, quote_id)['Status'] == 'converted'


def test_the_quote_stays_converted_while_another_invoice_comes_from_it(world):
    db = world.db
    quote_id, invoice_id = quote_with_invoice(world)
    other = make_invoice(world, 'sent', number='26R101')
    with db._get_connection() as con:
        con.execute('UPDATE Invoices SET SourceQuoteId = ? WHERE ID = ?', (quote_id, other))
    assert delete(world, invoice_id) == (303, '/invoice')
    assert columns(db, quote_id)['Status'] == 'converted'


def test_deleting_an_invoice_without_a_quote_leaves_quotes_alone(world):
    db = world.db
    quote_id, converted = quote_with_invoice(world)
    plain = make_invoice(world, 'draft', number='26R102')
    delete(world, plain)
    assert columns(db, quote_id)['Status'] == 'converted' and row(db, converted) is not None


# ── Der Server ───────────────────────────────────────────────────────────────

def test_post_deletes_the_draft_and_redirects_to_the_list(server_port, books):
    resp = post_json(server_port, '/invoice/save', make_payload(books), books.csrf)
    invoice_id = json.loads(resp.text)['invoice_id']
    generate = post_action(server_port, f'/invoice/pdf_generate?id={invoice_id}', books.csrf)
    assert generate.status == 200 and stored_documents()
    resp = post(server_port, f'/invoice/delete?id={invoice_id}', csrf=books.csrf,
                headers={'Origin': own_origin(server_port)})
    assert resp.status == 303 and resp.headers['Location'] == '/invoice'
    assert row(books.db, invoice_id) is None
    assert [p for p in stored_documents() if p.endswith(('.pdf', '.xml'))] == []
    again = post(server_port, f'/invoice/delete?id={invoice_id}', csrf=books.csrf)     # ein zweites Mal: weg
    assert again.status == 404 and again.text == 'Rechnung nicht gefunden.'


def test_post_on_a_sent_invoice_redirects_back_with_the_reason(server_port, books):
    resp = post_json(server_port, '/invoice/save', make_payload(books), books.csrf)
    invoice_id = json.loads(resp.text)['invoice_id']
    force_status(books.db, invoice_id, 'sent')
    before = state(books.db)
    resp = post(server_port, f'/invoice/delete?id={invoice_id}', csrf=books.csrf)
    assert resp.status == 303 and resp.headers['Location'].startswith(f'/invoice?id={invoice_id}&err=')
    assert state(books.db) == before
    page = get(server_port, resp.headers['Location'])
    assert page.status == 200 and app_msg_arguments(Dom(page.text)) == [err_of(resp.headers['Location']), 'error']


def test_get_is_405_and_deletes_nothing(server_port, books):
    resp = post_json(server_port, '/invoice/save', make_payload(books), books.csrf)
    invoice_id = json.loads(resp.text)['invoice_id']
    before = state(books.db)
    resp = get(server_port, f'/invoice/delete?id={invoice_id}')
    assert resp.status == 405 and resp.headers['Allow'] == 'POST'
    assert state(books.db) == before


@pytest.mark.parametrize('headers, with_token', [
    pytest.param({}, False, id='ohne-Token'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, True, id='fremde-Herkunft'),
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, True, id='fremde-Seite'),
])
def test_post_without_token_or_from_a_foreign_page_deletes_nothing(server_port, books, headers, with_token):
    resp = post_json(server_port, '/invoice/save', make_payload(books), books.csrf)
    invoice_id = json.loads(resp.text)['invoice_id']
    before = state(books.db)
    resp = post(server_port, f'/invoice/delete?id={invoice_id}', headers=headers,
                csrf=books.csrf if with_token else None)
    assert resp.status == 403
    assert state(books.db) == before


def test_the_id_of_a_quote_is_404_and_changes_nothing(server_port, books):
    quote_id, _invoice = quote_with_invoice(books)
    before, files = state(books.db, 'Invoices', 'InvoiceItems', 'NumberRanges'), document_files(user_dir())
    resp = post(server_port, f'/invoice/delete?id={quote_id}', csrf=books.csrf)
    assert resp.status == 404 and resp.text == 'Rechnung nicht gefunden.'
    assert state(books.db, 'Invoices', 'InvoiceItems', 'NumberRanges') == before
    assert document_files(user_dir()) == files


@pytest.mark.parametrize('query', ['', '?id=', '?id=abc', '?id=1.5', '?nummer=1'])
def test_a_missing_or_invalid_id_is_400(server_port, books, query):
    before = state(books.db)
    resp = post(server_port, '/invoice/delete' + query, csrf=books.csrf)
    assert resp.status == 400
    assert state(books.db) == before


def test_an_unknown_id_is_404(server_port, books):
    assert post(server_port, '/invoice/delete?id=99999', csrf=books.csrf).status == 404


# ── Die Liste und die Maske ──────────────────────────────────────────────────

def test_the_list_shows_the_trash_icon_for_drafts_only(world):
    ids = {status: make_invoice(world, status, number=f'26R{200 + i}')
           for i, status in enumerate(STATUS_LABELS)}
    html = PageInvoice(world.db, {}, None)
    list_part = html.split('Ende gridLeftCol')[0]
    shown = {int(i) for i in re.findall(r'onclick="deleteInvoiceDraft\((\d+)\)"', list_part)}
    assert shown == {ids['draft']}
    assert 'class="action-icon delete-icon" title="Entwurf löschen"' in list_part
    assert "appConfirmPost('/invoice/delete?id=' + invoiceId" in list_part         # POST nach Rückfrage, kein Link
    assert 'href="/invoice/delete' not in html and "href='/invoice/delete" not in html


def test_the_list_has_no_trash_icon_without_a_draft(world):
    make_invoice(world, 'sent')
    assert 'onclick="deleteInvoiceDraft(' not in PageInvoice(world.db, {}, None).split('Ende gridLeftCol')[0]
