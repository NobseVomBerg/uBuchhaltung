# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Ablage, Download und Kopie von Rechnungen.

Deckt ab:
* Namensschema "<Rechnungs-Nr.> <Kundenname>" für PDF und XRechnung (todo #2)
* automatische PDF-Erzeugung beim Wechsel auf „versendet“ (todo #2)
* Download liefert die *abgelegte* Datei, erzeugt nichts neu (todo #3)
* echter Datei-Download im Frontend statt Server-Link (todo #1)
* „Als neu kopieren“: gleiche Inhalte, frisches Datum, neue Nummer (todo #4)
"""
import datetime
import json
import os

import pytest

import userctx
from db import Database
from export.invoice_files import (generate_invoice_documents, invoice_file_stem,
                                  stored_invoice_document)
from export.pdf_invoice import generate_invoice_pdf
from server import handlers
from server.pages_invoice import PageInvoice


@pytest.fixture
def inv_db(tmp_path, monkeypatch):
    """Datenbank samt Datenverzeichnis im tmp_path – auch für handlers.Database()."""
    monkeypatch.setattr(userctx, "DATA_ROOT", str(tmp_path))
    db = Database()
    db.load_test_seed_data()
    return db


def _make_invoice(db, number='26R001', buyer_company='Malermeister Pinsel',
                  route_id=None, invoice_date='2026-03-04'):
    invoice_id = db.insert_invoice({
        'invoice_number': number,
        'invoice_date': invoice_date,
        'seller_name': 'Anna Verkäufer',
        'seller_company': 'Muster GmbH',
        'seller_street': 'Musterstraße 1',
        'seller_postal_code': '12345',
        'seller_city': 'Musterstadt',
        'buyer_name': 'Max Mustermann',
        'buyer_company': buyer_company,
        'buyer_street': 'Kundenweg 5',
        'buyer_postal_code': '54321',
        'buyer_city': 'Kundenhausen',
        'buyer_route_id': route_id,
        'currency': 'EUR',
        'payment_terms': 'Zahlbar in 14 Tagen',
        'payment_due_date': '2026-03-18',
        'bank_name': 'Testbank AG',
        'bank_iban': 'DE89370400440532013000',
        'bank_bic': 'TESTDE1X',
        'tax_category': 'S',
        'tax_rate': 0.19,
        'sum_net': 100.00,
        'tax_amount': 19.00,
        'sum_gross': 119.00,
        'amount_due': 119.00,
        'status': 'draft',
    })
    db.insert_invoice_item({
        'invoice_id': invoice_id, 'position': 1, 'description': 'Anstrich',
        'quantity': 1, 'unit': 'Stk.', 'price_per_unit': 100.00,
        'total_net': 100.00, 'tax_category': 'S', 'tax_rate': 0.19,
    })
    return invoice_id


# ── Namensschema (todo #2) ───────────────────────────────────────────────────

def test_pdf_filename_is_number_and_customer(inv_db):
    invoice_id = _make_invoice(inv_db)
    _, pdf_path = generate_invoice_pdf(inv_db, invoice_id)

    assert os.path.basename(pdf_path) == '26R001 Malermeister Pinsel.pdf'
    # Ablage im Jahresordner des Rechnungsdatums
    assert os.path.basename(os.path.dirname(pdf_path)) == '2026'
    assert os.path.isfile(pdf_path)
    # Pfad steht auch in der Datenbank
    assert inv_db.get_invoice_by_id(invoice_id)[41] == pdf_path


def test_file_stem_falls_back_to_buyer_name(inv_db):
    invoice_id = _make_invoice(inv_db, number='26R009', buyer_company='')
    stem = invoice_file_stem(inv_db.get_invoice_by_id(invoice_id))
    assert stem == '26R009 Max Mustermann'


def test_rename_removes_stale_pdf(inv_db):
    """Nach einer Umbenennung darf die alte Datei nicht liegen bleiben."""
    invoice_id = _make_invoice(inv_db)
    _, first = generate_invoice_pdf(inv_db, invoice_id)

    conn = inv_db._get_connection()
    conn.execute("UPDATE Invoices SET BuyerCompany = 'Bau AG' WHERE ID = ?", (invoice_id,))
    conn.commit()
    conn.close()

    _, second = generate_invoice_pdf(inv_db, invoice_id)
    assert os.path.basename(second) == '26R001 Bau AG.pdf'
    assert os.path.isfile(second)
    assert not os.path.exists(first)


# ── XRechnung neben dem PDF (todo #2/#3) ─────────────────────────────────────

def test_xrechnung_written_next_to_pdf(inv_db):
    invoice_id = _make_invoice(inv_db, route_id='991-12345-67')
    result = generate_invoice_documents(inv_db, invoice_id)

    assert result['error'] is None
    assert result['xml_filename'] == '26R001 Malermeister Pinsel.xml'
    assert os.path.isfile(result['xml_path'])
    assert inv_db.get_invoice_by_id(invoice_id)[42] == result['xml_path']


def test_no_xrechnung_without_erechnung_fields(inv_db):
    invoice_id = _make_invoice(inv_db)
    result = generate_invoice_documents(inv_db, invoice_id)

    assert result['xml_path'] is None
    assert result['error'] is None


# ── Download liefert die abgelegte Datei (todo #3) ───────────────────────────

def test_download_reads_stored_file_without_regenerating(inv_db):
    invoice_id = _make_invoice(inv_db)
    _, pdf_path = generate_invoice_pdf(inv_db, invoice_id)
    with open(pdf_path, 'wb') as f:          # Markierung: würde neu erzeugt, wäre sie weg
        f.write(b'%PDF-1.4 abgelegte Fassung')

    data, filename, error = stored_invoice_document(inv_db, invoice_id, 'pdf')

    assert error is None
    assert data == b'%PDF-1.4 abgelegte Fassung'
    assert filename == '26R001 Malermeister Pinsel.pdf'


def test_download_without_file_reports_short_error(inv_db):
    invoice_id = _make_invoice(inv_db)

    data, filename, error = stored_invoice_document(inv_db, invoice_id, 'pdf')

    assert (data, filename) == (None, None)
    assert error == 'Kein PDF vorhanden.'


def test_download_finds_file_when_db_path_is_stale(inv_db):
    """Fehlt der DB-Pfad, greift das Namensschema als Fallback."""
    invoice_id = _make_invoice(inv_db)
    generate_invoice_pdf(inv_db, invoice_id)
    inv_db.update_invoice_pdf_path(invoice_id, None)

    data, filename, error = stored_invoice_document(inv_db, invoice_id, 'pdf')
    assert error is None
    assert filename == '26R001 Malermeister Pinsel.pdf'
    assert data.startswith(b'%PDF')


def test_download_xml_reports_missing_file(inv_db):
    invoice_id = _make_invoice(inv_db)
    _, _, error = stored_invoice_document(inv_db, invoice_id, 'xml')
    assert error == 'Kein XRechnung (XML) vorhanden.'


# ── Automatisches PDF beim Versenden (todo #2) ───────────────────────────────

def _set_status(invoice_id, status):
    body = json.dumps({'invoice_id': invoice_id, 'status': status}).encode()
    code, response = handlers.handle_update_invoice_status(body)
    return code, json.loads(response)


def test_status_sent_generates_pdf(inv_db):
    invoice_id = _make_invoice(inv_db)
    code, payload = _set_status(invoice_id, 'sent')

    assert code == 200 and payload['success'] is True
    assert 'warning' not in payload
    pdf_path = inv_db.get_invoice_by_id(invoice_id)[41]
    assert pdf_path and os.path.isfile(pdf_path)
    assert os.path.basename(pdf_path) == '26R001 Malermeister Pinsel.pdf'


def test_status_sent_generates_xrechnung_too(inv_db):
    invoice_id = _make_invoice(inv_db, route_id='991-12345-67')
    _set_status(invoice_id, 'sent')

    xml_path = inv_db.get_invoice_by_id(invoice_id)[42]
    assert xml_path and os.path.isfile(xml_path)


def test_status_sent_twice_keeps_the_sent_document(inv_db):
    """Eine bereits versendete Rechnung wird nicht überschrieben."""
    invoice_id = _make_invoice(inv_db)
    _set_status(invoice_id, 'sent')
    pdf_path = inv_db.get_invoice_by_id(invoice_id)[41]
    with open(pdf_path, 'wb') as f:
        f.write(b'%PDF-1.4 versendete Fassung')

    _set_status(invoice_id, 'sent')

    with open(pdf_path, 'rb') as f:
        assert f.read() == b'%PDF-1.4 versendete Fassung'


def test_other_status_changes_do_not_generate(inv_db):
    invoice_id = _make_invoice(inv_db)
    _set_status(invoice_id, 'finalized')
    assert not inv_db.get_invoice_by_id(invoice_id)[41]


def test_status_sent_reports_warning_when_pdf_fails(inv_db):
    """Ohne Positionen lässt sich kein PDF bauen – der Status wird trotzdem
    gesetzt, der Nutzer aber gewarnt."""
    invoice_id = _make_invoice(inv_db)
    conn = inv_db._get_connection()
    conn.execute('DELETE FROM InvoiceItems WHERE InvoiceId = ?', (invoice_id,))
    conn.commit()
    conn.close()

    code, payload = _set_status(invoice_id, 'sent')

    assert code == 200 and payload['success'] is True
    assert 'warning' in payload
    assert inv_db.get_invoice_by_id(invoice_id)[40] == 'sent'


# ── Frontend: echter Download statt Server-Link (todo #1/#3) ─────────────────

def test_list_icon_only_downloads(inv_db):
    _make_invoice(inv_db)
    html = PageInvoice(inv_db, {}, None)

    assert 'downloadInvoiceDocs(' in html
    # Die Liste erzeugt nichts mehr neu ...
    assert '/invoice/pdf_generate' not in html.split('Ende gridLeftCol')[0]
    # ... und lädt PDF wie XRechnung als echte Datei herunter
    assert "appDownload('/invoice/pdf_download?id='" in html
    assert "appDownload('/invoice/xml_download?id='" in html


def test_list_icon_flags_missing_pdf(inv_db):
    _make_invoice(inv_db)
    html = PageInvoice(inv_db, {}, None)
    assert 'downloadInvoiceDocs(1, false, false)' in html
    assert 'Kein PDF vorhanden' in html


def test_list_icon_offers_existing_pdf(inv_db):
    invoice_id = _make_invoice(inv_db)
    generate_invoice_pdf(inv_db, invoice_id)
    html = PageInvoice(inv_db, {}, None)
    assert f'downloadInvoiceDocs({invoice_id}, true, false)' in html


def test_form_downloads_file_instead_of_opening_a_tab(inv_db):
    invoice_id = _make_invoice(inv_db)
    html = PageInvoice(inv_db, {}, invoice_id)

    assert 'function downloadInvoiceFiles(' in html
    assert 'downloadPDF()' in html
    # kein Anzeigen-im-Tab mehr, sondern echter Download
    assert 'inline=1' not in html


# ── Als neu kopieren (todo #4) ───────────────────────────────────────────────

def test_form_offers_copy_button(inv_db):
    invoice_id = _make_invoice(inv_db)
    html = PageInvoice(inv_db, {}, invoice_id)

    assert 'Als neu kopieren' in html
    assert f'copyInvoiceAsNew({invoice_id})' in html
    assert "'/invoice?copy_from='" in html


def test_copy_opens_as_new_invoice_with_fresh_number_and_date(inv_db):
    inv_db.insert_number_range('invoice', 2026, 'R', current_number=1)
    invoice_id = _make_invoice(inv_db)
    inv_db.update_invoice_status(invoice_id, 'sent')

    html = PageInvoice(inv_db, {}, None, copy_from=invoice_id)

    # neue, leere Rechnung: keine ID, kein Bearbeiten-Modus
    assert '<input type="hidden" id="invoice_id" value="">' in html
    assert '<input type="hidden" id="is_edit_mode" value="false">' in html
    assert '<input type="hidden" id="invoice_status_value" value="draft">' in html
    # frische Nummer aus dem Nummernkreis, nicht die der Quelle
    assert 'id="invoice_number" value="26R002"' in html
    # frisches Datum
    today = datetime.date.today().isoformat()
    assert f'id="invoice_date" value="{today}"' in html
    # Kopie ist als solche erkennbar
    assert 'Kopie von 26R001' in html


def test_copy_keeps_customer_items_and_terms(inv_db):
    inv_db.insert_number_range('invoice', 2026, 'R', current_number=1)
    customer_id = inv_db.insert_contact(
        contact_type='customer', display_name='Malermeister Pinsel',
        company_name='Malermeister Pinsel')
    invoice_id = _make_invoice(inv_db)
    conn = inv_db._get_connection()
    conn.execute('UPDATE Invoices SET CustomerId = ? WHERE ID = ?', (customer_id, invoice_id))
    conn.commit()
    conn.close()

    html = PageInvoice(inv_db, {}, None, copy_from=invoice_id)

    assert f'<option value="{customer_id}" selected>' in html
    assert 'Anstrich' in html                      # Position übernommen
    assert 'Zahlbar in 14 Tagen' in html           # Konditionen übernommen
    # Fälligkeitsdatum der Quelle wird NICHT mitkopiert
    assert 'id="payment_due_date" style="width: 200px;" value=""' in html
    # keine Zahlungsverknüpfungen der Quelle
    assert 'Zahlungsverknüpfungen' not in html


def test_copy_of_unknown_invoice_falls_back_to_empty_form(inv_db):
    html = PageInvoice(inv_db, {}, None, copy_from=999)
    assert 'Neue Rechnung erstellen' in html
