# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Tests für die Kleinunternehmer-Option (§19 UStG): kein Steuerausweis.

Sentinel TaxRate = -1 bedeutet "keine USt-Zeile" (unterscheidbar von echten 0%).
Abgedeckt:
  - Formular-Vorbelegung der Checkbox aus dem Sentinel (Rechnung + Angebot)
  - PDF lässt Netto-/MwSt-Zeile weg, zeigt nur den Gesamtbetrag
"""
import zlib

from export.pdf_invoice import generate_invoice_pdf
from server.pages_invoice import PageInvoice
from server.pages_quote import PageQuote


def _make_doc(db, number, tax_rate, gross):
    return db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-06-01',
        'seller_name': 'S', 'seller_company': 'S GmbH',
        'seller_street': 'Str 1', 'seller_postal_code': '12345', 'seller_city': 'Stadt',
        'buyer_name': 'B', 'buyer_company': 'B GmbH',
        'tax_rate': tax_rate, 'sum_net': 100.0, 'tax_amount': 0.0 if tax_rate < 0 else 19.0,
        'sum_gross': gross, 'amount_due': gross, 'status': 'finalized',
    })


def _content(pdf_bytes):
    """Ersten FlateDecode-Stream dekomprimieren (Seiteninhalt, kein Logo)."""
    flat = pdf_bytes.find(b'/FlateDecode')
    start = pdf_bytes.find(b'stream\n', flat) + len(b'stream\n')
    end = pdf_bytes.find(b'\nendstream', start)
    return zlib.decompress(pdf_bytes[start:end]).decode('latin-1', errors='replace')


# ── Formular-Vorbelegung ─────────────────────────────────────────────────────

def test_invoice_new_has_tax_checkbox_checked(db_with_coa):
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    assert 'id="show_tax" checked' in html
    assert 'id="tax_row"' in html
    assert '__SHOW_TAX_CHECKED__' not in html


def test_invoice_exempt_checkbox_unchecked(db_with_coa):
    iid = _make_doc(db_with_coa, 'KU-INV', -1, 100.0)
    html = PageInvoice(db_with_coa, filters={}, invoice_id=iid)
    assert 'id="show_tax"' in html
    assert 'id="show_tax" checked' not in html      # §19: keine USt
    assert '__SHOW_TAX_CHECKED__' not in html


def test_quote_new_has_tax_checkbox_checked(db_with_coa):
    html = PageQuote(db_with_coa)
    assert 'id="show_tax" checked' in html
    assert 'id="tax_row"' in html


def test_quote_exempt_checkbox_unchecked(db_with_coa):
    qid = _make_doc(db_with_coa, 'KU-QUOTE', -1, 100.0)
    html = PageQuote(db_with_coa, {}, qid)
    assert 'id="show_tax"' in html
    assert 'id="show_tax" checked' not in html


# ── PDF ohne Steuerzeile ─────────────────────────────────────────────────────

def test_pdf_exempt_omits_tax_lines(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    iid = _make_doc(tmp_db, 'KU-PDF', -1, 100.0)
    tmp_db.insert_invoice_item({
        'invoice_id': iid, 'position': 1, 'description': 'Leistung',
        'quantity': 1.0, 'unit': 'C62', 'price_per_unit': 100.0,
        'total_net': 100.0, 'tax_category': 'S', 'tax_rate': 0.0,
    })
    pdf_bytes, _ = generate_invoice_pdf(tmp_db, iid)
    content = _content(pdf_bytes)
    assert 'MwSt' not in content          # keine Steuerzeile
    assert 'Summe netto' not in content   # keine Netto-Zwischenzeile
    assert 'Gesamtbetrag' in content      # nur der Gesamtbetrag


def test_pdf_with_tax_keeps_tax_line(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    iid = _make_doc(tmp_db, 'STD-PDF', 19.0, 119.0)
    tmp_db.insert_invoice_item({
        'invoice_id': iid, 'position': 1, 'description': 'Leistung',
        'quantity': 1.0, 'unit': 'C62', 'price_per_unit': 100.0,
        'total_net': 100.0, 'tax_category': 'S', 'tax_rate': 19.0,
    })
    pdf_bytes, _ = generate_invoice_pdf(tmp_db, iid)
    content = _content(pdf_bytes)
    assert 'MwSt' in content
    assert 'Summe netto' in content
