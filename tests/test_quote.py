# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Tests für Angebote (Quotes) – geteilte Invoices-Tabelle mit DocumentType.

Abdeckung:
- DB: insert/fetch von Angeboten, Trennung von Rechnungen, neue Spaltenindizes,
  convert_quote_to_invoice (Kopie + Verknüpfung + Statuswechsel),
  Statistik-Guard (overdue/due_soon ignorieren Angebote).
- Handler: handle_quote_save (neu), handle_convert_quote_to_invoice (303 → /invoice).
"""
import json
import zlib

import pytest

from server import handlers
from export.pdf_quote import generate_quote_pdf


def _all_content_bytes(pdf_bytes: bytes) -> bytes:
    """Alle FlateDecode-Streams dekomprimiert aneinanderhängen (alle Seiten)."""
    out = bytearray()
    pos = 0
    while True:
        flat = pdf_bytes.find(b'/FlateDecode', pos)
        if flat == -1:
            break
        start = pdf_bytes.find(b'stream\n', flat) + len(b'stream\n')
        end = pdf_bytes.find(b'\nendstream', start)
        try:
            out += zlib.decompress(pdf_bytes[start:end])
        except zlib.error:
            pass
        pos = end + 1
    return bytes(out)


# ─────────────────────────────────────────────────────────────────────────────
# Hilfen
# ─────────────────────────────────────────────────────────────────────────────

def _quote(db, number='26A001', status='sent', valid_until='2026-07-01',
           intro='<b>Hallo</b>', closing='Gruß', net=100.0):
    """Minimales Angebot anlegen, gibt die ID zurück."""
    qid = db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-06-09',
        'seller_name': 'S', 'seller_company': 'S GmbH',
        'buyer_name': 'B', 'buyer_company': 'B AG',
        'tax_rate': 0.19, 'sum_net': net, 'tax_amount': round(net * 0.19, 2),
        'sum_gross': round(net * 1.19, 2), 'amount_due': round(net * 1.19, 2),
        'status': status, 'document_type': 'quote',
        'valid_until': valid_until, 'intro_text': intro, 'closing_text': closing,
    })
    db.insert_invoice_item({
        'invoice_id': qid, 'position': 1, 'description': 'Position 1',
        'quantity': 2, 'unit': 'C62', 'price_per_unit': net / 2,
        'total_net': net, 'tax_rate': 0.19,
    })
    return qid


# ─────────────────────────────────────────────────────────────────────────────
# DB-Schicht
# ─────────────────────────────────────────────────────────────────────────────

class TestQuoteDB:
    def test_fetch_quotes_separates_from_invoices(self, tmp_db):
        _quote(tmp_db, '26A001')
        tmp_db.insert_invoice({
            'invoice_number': '26R001', 'invoice_date': '2026-06-09',
            'seller_name': 'S', 'seller_company': 'S GmbH',
            'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
            'sum_net': 10.0, 'tax_amount': 1.9, 'sum_gross': 11.9,
            'amount_due': 11.9, 'status': 'finalized',
        })
        quotes = [q[1] for q in tmp_db.fetch_quotes()]
        invoices = [i[1] for i in tmp_db.fetch_invoices()]
        assert quotes == ['26A001']
        assert invoices == ['26R001']

    def test_new_column_indices(self, tmp_db):
        qid = _quote(tmp_db, valid_until='2026-07-01', intro='<i>X</i>', closing='Y')
        row = tmp_db.get_invoice_by_id(qid)
        assert row[45] == 'quote',        f"DocumentType bei [45], war {row[45]!r}"
        assert row[46] == '2026-07-01',   f"ValidUntil bei [46], war {row[46]!r}"
        assert row[47] == '<i>X</i>',     f"IntroText bei [47], war {row[47]!r}"
        assert row[48] == 'Y',            f"ClosingText bei [48], war {row[48]!r}"
        assert row[49] is None,           f"SourceQuoteId bei [49], war {row[49]!r}"

    def test_default_document_type_is_invoice(self, tmp_db):
        iid = tmp_db.insert_invoice({
            'invoice_number': '26R009', 'invoice_date': '2026-06-09',
            'seller_name': 'S', 'seller_company': 'S GmbH',
            'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
            'sum_net': 10.0, 'tax_amount': 1.9, 'sum_gross': 11.9,
            'amount_due': 11.9, 'status': 'finalized',
        })
        assert tmp_db.get_invoice_by_id(iid)[45] == 'invoice'

    def test_convert_quote_to_invoice(self, tmp_db):
        qid = _quote(tmp_db, '26A001', net=200.0)
        new_id = tmp_db.convert_quote_to_invoice(qid)
        assert new_id is not None

        inv = tmp_db.get_invoice_by_id(new_id)
        assert inv[45] == 'invoice'            # DocumentType
        assert inv[49] == qid                  # SourceQuoteId
        assert inv[40] == 'draft'              # Status
        assert inv[36] == pytest.approx(200.0)  # SumNet kopiert
        assert len(tmp_db.get_invoice_items(new_id)) == 1

        # Angebot ist jetzt 'umgewandelt'
        assert tmp_db.get_invoice_by_id(qid)[40] == 'converted'
        # neue Rechnung erscheint in Rechnungsliste, nicht in Angebotsliste
        assert new_id in [i[0] for i in tmp_db.fetch_invoices()]
        assert new_id not in [q[0] for q in tmp_db.fetch_quotes()]

    def test_delete_quote(self, tmp_db):
        qid = _quote(tmp_db, '26A001')
        tmp_db.delete_quote(qid)
        assert tmp_db.get_invoice_by_id(qid) is None
        assert tmp_db.get_invoice_items(qid) == []

    def test_delete_converted_quote_keeps_invoice(self, tmp_db):
        qid = _quote(tmp_db, '26A002')
        inv_id = tmp_db.convert_quote_to_invoice(qid)
        tmp_db.delete_quote(qid)
        assert tmp_db.get_invoice_by_id(qid) is None          # Angebot weg
        inv = tmp_db.get_invoice_by_id(inv_id)
        assert inv is not None                                 # Rechnung bleibt
        assert inv[49] is None                                 # SourceQuoteId gelöst

    def test_convert_rejects_non_quote(self, tmp_db):
        iid = tmp_db.insert_invoice({
            'invoice_number': '26R010', 'invoice_date': '2026-06-09',
            'seller_name': 'S', 'seller_company': 'S GmbH',
            'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
            'sum_net': 10.0, 'tax_amount': 1.9, 'sum_gross': 11.9,
            'amount_due': 11.9, 'status': 'finalized',
        })
        assert tmp_db.convert_quote_to_invoice(iid) is None

    def test_overdue_ignores_quotes(self, tmp_db):
        # Angebot mit vergangenem Fälligkeitsdatum + offenem Betrag darf NICHT
        # als überfällige Rechnung auftauchen.
        tmp_db.insert_invoice({
            'invoice_number': '26A050', 'invoice_date': '2020-01-01',
            'seller_name': 'S', 'seller_company': 'S GmbH',
            'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
            'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0,
            'amount_due': 119.0, 'status': 'sent', 'document_type': 'quote',
            'payment_due_date': '2020-02-01',
        })
        assert tmp_db.get_overdue_invoices() == []
        assert tmp_db.get_invoices_due_soon(days=9999) == []


# ─────────────────────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────────────────────

class TestQuoteHandlers:
    @staticmethod
    def _make_contacts(db):
        db.insert_contact(contact_type='own', entity_type='company',
                          display_name='Eigene Firma', company_name='Eigene Firma GmbH',
                          street='Weg 1', postal_code='12345', city='Stadt',
                          email='info@example.com', phone='0123', tax_id='DE123')
        db.insert_contact(contact_type='customer', entity_type='company',
                          display_name='Kunde', company_name='Kunde AG',
                          customer_number='K900', street='Gasse 2',
                          postal_code='54321', city='Dorf')
        own_id = db.fetch_contacts(contact_type='own')[0][0]
        cust_id = db.fetch_contacts(contact_type='customer')[0][0]
        return own_id, cust_id

    def test_handle_quote_save_new(self, tmp_db, monkeypatch):
        monkeypatch.setattr('server.handlers.Database', lambda *a, **k: tmp_db)
        own_id, cust_id = self._make_contacts(tmp_db)
        body = json.dumps({
            'quoteNumber': '26A100', 'quoteDate': '2026-06-09',
            'customerId': cust_id, 'ownCompanyId': own_id,
            'validUntil': '2026-07-09',
            'introText': '<div>Guten Tag <b>Welt</b></div>',
            'closingText': '<i>Gruß</i>',
            'taxRate': 0.19, 'status': 'sent',
            'items': [{'position': 1, 'quantity': 3, 'unit': 'Std',
                       'description': 'Leistung', 'unitPrice': 100.0, 'taxRate': 19}],
        }).encode()
        resp = json.loads(handlers.handle_quote_save(body))
        assert resp['success'] is True
        qid = resp['quote_id']
        row = tmp_db.get_invoice_by_id(qid)
        assert row[45] == 'quote'
        assert row[46] == '2026-07-09'
        assert '<b>Welt</b>' in (row[47] or '')
        assert row[40] == 'sent'
        assert row[36] == pytest.approx(300.0)   # 3 × 100 netto, serverseitig
        assert len(tmp_db.get_invoice_items(qid)) == 1

    def test_handle_quote_save_strips_script(self, tmp_db, monkeypatch):
        monkeypatch.setattr('server.handlers.Database', lambda *a, **k: tmp_db)
        own_id, cust_id = self._make_contacts(tmp_db)
        body = json.dumps({
            'quoteNumber': '26A101', 'quoteDate': '2026-06-09',
            'customerId': cust_id, 'ownCompanyId': own_id,
            'introText': 'Hallo<script>alert(1)</script> Welt',
            'taxRate': 0.19, 'status': 'draft',
            'items': [{'position': 1, 'quantity': 1, 'description': 'X',
                       'unitPrice': 10.0, 'taxRate': 19}],
        }).encode()
        resp = json.loads(handlers.handle_quote_save(body))
        row = tmp_db.get_invoice_by_id(resp['quote_id'])
        assert '<script>' not in (row[47] or '')
        assert 'Hallo' in (row[47] or '')

    def test_handle_convert_redirects_to_invoice(self, tmp_db, monkeypatch):
        monkeypatch.setattr('server.handlers.Database', lambda *a, **k: tmp_db)
        qid = _quote(tmp_db, '26A200')
        status, location = handlers.handle_convert_quote_to_invoice({'quote_id': [str(qid)]})
        assert status == 303
        assert location.startswith('/invoice?id=')
        new_id = int(location.split('=')[1])
        assert tmp_db.get_invoice_by_id(new_id)[49] == qid


# ─────────────────────────────────────────────────────────────────────────────
# PDF-Erzeugung (WinAnsi-Encoding)
# ─────────────────────────────────────────────────────────────────────────────

class TestQuotePdf:
    def test_special_chars_use_winansi_not_questionmark(
            self, tmp_db, monkeypatch, tmp_path):
        """Aus Office kopierte Bulletpoints/Sonderzeichen landen als WinAnsi
        (cp1252) im Content-Stream – nicht als '?'."""
        monkeypatch.chdir(tmp_path)
        closing = (
            '<ul><li>Punkt eins</li><li>Punkt zwei</li></ul>'
            '<p>Preis: 10 € – „Angebot“, freibleibend.</p>'
        )
        qid = _quote(tmp_db, '26A300', closing=closing)
        pdf_bytes, _ = generate_quote_pdf(tmp_db, qid)
        assert pdf_bytes is not None
        content = _all_content_bytes(pdf_bytes)

        # cp1252/WinAnsi-Bytes müssen vorkommen (also nicht zu '?' geworden):
        assert b'\x95' in content   # • Bullet (aus <li> erzeugt)
        assert b'\x80' in content   # € Euro
        assert b'\x96' in content   # – Gedankenstrich
        assert b'\x84' in content   # „ (U+201E)
        assert b'\x93' in content   # " (U+201C)
