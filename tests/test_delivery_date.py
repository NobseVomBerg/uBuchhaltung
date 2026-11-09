# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Leistungsdatum als Pflichtfeld (§ 14 UStG): Formular, Server, PDF, XML."""
import io
import json
import xml.etree.ElementTree as ET

import pdfplumber

import server.handlers as handlers
from export.cii_invoice import NS, generate_cii
from export.pdf_invoice import generate_invoice_pdf
from export.xrechnung_invoice import XRechnungGenerator
from server.handlers import handle_invoice_save
from server.pages_invoice import PageInvoice
from tests.golden_cases import make_case
from tests.test_invoice_recompute import _make_contacts
from tests.test_quote import _quote


def _payload(own_id, cust_id, **over):
    p = {
        'invoiceNumber': '26R100', 'invoiceDate': '2026-06-07',
        'customerId': cust_id, 'ownCompanyId': own_id, 'taxRate': 0.19,
        'status': 'draft',
        'items': [{'position': 1, 'quantity': 1, 'unit': 'C62',
                   'description': 'X', 'unitPrice': 100.0, 'taxRate': 19}],
    }
    p.update(over)
    return p


def _save(payload):
    return json.loads(handle_invoice_save(json.dumps(payload).encode()))


def test_form_marks_delivery_date_required_and_prefills(db_with_coa):
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    assert 'id="delivery_date" required' in html
    assert 'Leistungsdatum' in html and '(optional)' not in html.split('Leistungsdatum')[1][:300]
    # Vorbelegung mit dem Rechnungsdatum + Mitlaufen
    assert "del.value = inv.value" in html


def test_form_keeps_existing_delivery_date(tmp_db):
    iid = _case_with_delivery(tmp_db)
    html = PageInvoice(tmp_db, filters={}, invoice_id=iid)
    assert 'value="2026-03-01"' in html.split('id="delivery_date"')[1][:80]


def test_server_rejects_missing_delivery_date(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    resp = _save(_payload(own, cust))
    assert resp['success'] is False
    assert 'Leistungsdatum' in resp['error']
    resp = _save(_payload(own, cust, deliveryDate='2026-06-05'))
    assert resp['success'] is True
    assert tmp_db.get_invoice_by_id(resp['invoice_id'])[25] == '2026-06-05'


def test_sent_invoice_is_not_checked_afterwards(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    iid = _save(_payload(own, cust, deliveryDate='2026-06-05'))['invoice_id']
    import sqlite3
    con = sqlite3.connect(tmp_db.db_name)
    con.execute("UPDATE Invoices SET Status='sent', DeliveryDate=NULL WHERE ID=?", (iid,))
    con.commit()
    con.close()
    resp = _save(_payload(own, cust, invoiceId=iid, status='sent'))
    assert resp['success'] is True


def test_quote_to_invoice_prefills_delivery_date(tmp_db):
    qid = _quote(tmp_db, '26A001')
    new_id = tmp_db.convert_quote_to_invoice(qid)
    inv = tmp_db.get_invoice_by_id(new_id)
    assert inv[25] == inv[2]


def test_pdf_shows_delivery_date(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    iid = _case_with_delivery(tmp_db)
    pdf, _ = generate_invoice_pdf(tmp_db, iid)
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        text = p.pages[0].extract_text()
    assert 'Leistungsdatum:' in text and '2026-03-01' in text


def _case_with_delivery(db):
    from tests.golden_cases import CASES
    iid = make_case(db, sorted(CASES)[0])
    import sqlite3
    con = sqlite3.connect(db.db_name)
    con.execute("UPDATE Invoices SET DeliveryDate='2026-03-01' WHERE ID=?", (iid,))
    con.commit()
    con.close()
    return iid


def test_delivery_date_in_cii_and_ubl(tmp_db):
    iid = _case_with_delivery(tmp_db)
    inv, items = tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid)
    cii = ET.fromstring(generate_cii(inv, items, db=tmp_db))
    ev = cii.find('.//ram:ActualDeliverySupplyChainEvent/ram:OccurrenceDateTime', NS)
    assert ev is not None and '20260301' in ''.join(ev.itertext()).replace('-', '')
    ubl = XRechnungGenerator().generate_xml(inv, items)
    assert '<cbc:ActualDeliveryDate>2026-03-01</cbc:ActualDeliveryDate>' in ubl
