# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Integration: Rechnungs-PDF ist hybride E-Rechnung (ZUGFeRD/Factur-X, EN 16931)."""
import io
import os
import xml.etree.ElementTree as ET

from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1, stream_value

from export.cii_invoice import NS
from export.invoice_files import generate_invoice_documents, stored_invoice_document
from tests.test_invoice_documents import inv_db, _make_invoice  # noqa: F401  (Fixture)


def embedded_xml(pdf_bytes):
    doc = PDFDocument(PDFParser(io.BytesIO(pdf_bytes)))
    spec = resolve1(resolve1(doc.catalog['AF'])[0])
    return stream_value(resolve1(spec['EF'])['F']).get_data()


def test_generated_pdf_embeds_cii(inv_db):
    iid = _make_invoice(inv_db, number='26R010')
    result = generate_invoice_documents(inv_db, iid)
    data, name, err = stored_invoice_document(inv_db, iid, 'pdf')
    assert err is None and name == '26R010 Malermeister Pinsel.pdf'
    root = ET.fromstring(embedded_xml(data))
    assert root.find('rsm:ExchangedDocument/ram:ID', NS).text == '26R010'
    summ = root.find('.//ram:SpecifiedTradeSettlementHeaderMonetarySummation', NS)
    assert summ.find('ram:GrandTotalAmount', NS).text == '119.00'
    assert result['error'] is None


def test_embedded_xml_matches_standalone_cii(inv_db):
    from export.cii_invoice import generate_cii
    iid = _make_invoice(inv_db)
    generate_invoice_documents(inv_db, iid)
    data, _, _ = stored_invoice_document(inv_db, iid, 'pdf')
    assert embedded_xml(data) == generate_cii(
        inv_db.get_invoice_by_id(iid), inv_db.get_invoice_items(iid), db=inv_db)


def _set_status(db, iid, status):
    with db._get_connection() as c:
        c.execute('UPDATE Invoices SET Status=? WHERE ID=?', (status, iid))


def test_sent_invoice_is_not_regenerated_by_button(inv_db):
    iid = _make_invoice(inv_db)
    generate_invoice_documents(inv_db, iid)
    path = inv_db.get_invoice_by_id(iid)[41]
    with open(path, 'wb') as f:
        f.write(b'%PDF-1.4 versendete Fassung')
    _set_status(inv_db, iid, 'sent')

    result = generate_invoice_documents(inv_db, iid)       # „PDF + E-Rechnung“

    assert result['pdf_path'] == path and result['error'] is None
    with open(path, 'rb') as f:
        assert f.read() == b'%PDF-1.4 versendete Fassung'


def test_overwrite_sent_regenerates(inv_db):
    iid = _make_invoice(inv_db)
    generate_invoice_documents(inv_db, iid)
    path = inv_db.get_invoice_by_id(iid)[41]
    with open(path, 'wb') as f:
        f.write(b'%PDF-1.4 alt')
    _set_status(inv_db, iid, 'sent')
    generate_invoice_documents(inv_db, iid, overwrite_sent=True)
    with open(path, 'rb') as f:
        assert f.read().startswith(b'%PDF-1.7')


def test_draft_is_regenerated(inv_db):
    iid = _make_invoice(inv_db)
    generate_invoice_documents(inv_db, iid)
    path = inv_db.get_invoice_by_id(iid)[41]
    with open(path, 'wb') as f:
        f.write(b'alt')
    generate_invoice_documents(inv_db, iid)
    assert os.path.getsize(path) > 1000
