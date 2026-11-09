# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Tests für das neutrale E-Rechnungsmodell (Summen, Steuer, Einheiten)."""
from decimal import Decimal

import pytest

from export.einvoice_model import build_einvoice, unit_code, rate_to_pct
from tests.golden_cases import make_case


def _model(db, case):
    iid = make_case(db, case)
    return build_einvoice(db, db.get_invoice_by_id(iid), db.get_invoice_items(iid))


def test_totals_are_exactly_consistent(tmp_db):
    m = _model(tmp_db, 'std19')
    t = m.totals
    assert t.line_total == sum(l.net for l in m.lines)
    assert t.tax_total == sum(b.amount for b in m.tax_breakdown)
    assert t.grand_total == t.tax_basis + t.tax_total
    assert t.payable == t.grand_total - t.prepaid
    assert m.warnings == []                      # gespeicherte Summen stimmen


def test_tax_breakdown_is_list_per_rate(tmp_db):
    m = _model(tmp_db, 'std7')
    assert [(b.category, b.rate) for b in m.tax_breakdown] == [('S', Decimal('7'))]


def test_kleinunternehmer_is_exempt_with_reason(tmp_db):
    m = _model(tmp_db, 'ku')
    assert m.is_exempt
    b = m.tax_breakdown[0]
    assert (b.category, b.rate, b.amount) == ('E', Decimal('0'), 0)
    assert '§ 19 UStG' in b.exemption_reason
    assert all(l.tax_category == 'E' for l in m.lines)


@pytest.mark.parametrize('text, code', [
    ('Stk.', 'C62'), ('Std.', 'HUR'), ('HUR', 'HUR'), ('C62', 'C62'),
    ('Tag', 'DAY'), ('pauschal', 'LS'), ('STD.', 'HUR'), ('m²', 'MTK'), ('kg', 'KGM'), ('', 'C62'), (None, 'C62'), ('unbekannt', 'C62')])
def test_unit_code(text, code):
    assert unit_code(text) == code


@pytest.mark.parametrize('value, expected', [
    (19, Decimal('19')), (0.19, Decimal('19')), (7, Decimal('7')),
    (0, Decimal('0')), (None, Decimal('19'))])
def test_rate_to_pct(value, expected):
    assert rate_to_pct(value) == expected


def test_unknown_unit_is_visible(tmp_db, capsys):
    iid = make_case(tmp_db, 'std7')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE InvoiceItems SET Unit='Zauberstab' WHERE InvoiceId=?", (iid,))
    m = build_einvoice(tmp_db, tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid))
    assert m.lines[0].unit_code == 'C62'
    assert any('Zauberstab' in w for w in m.warnings)
    assert 'Zauberstab' in capsys.readouterr().out


def test_control_characters_do_not_break_ubl(tmp_db):
    import xml.etree.ElementTree as ET
    from export.xrechnung_invoice import XRechnungGenerator
    iid = make_case(tmp_db, 'std7')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE Invoices SET BuyerCompany=? WHERE ID=?", ('Muster\x07 AG\x00', iid))
        c.execute("UPDATE InvoiceItems SET Description=? WHERE InvoiceId=?", ('Buch\x1b', iid))
    xml = XRechnungGenerator().generate_xml(
        tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid), db=tmp_db)
    assert ET.fromstring(xml.encode('utf-8')) is not None
    assert 'Muster AG' in xml and '>Buch<' in xml


@pytest.mark.parametrize('stored, shown', [
    ('Std.', 'Std.'), ('Stk.', 'Stk.'), ('Tag', 'Tag'), ('h', 'h'),       # Freitext bleibt
    ('HUR', 'Std.'), ('C62', 'Stk.'), ('DAY', 'Tag'), ('KMT', 'km'), ('KGM', 'kg'),
    ('MTR', 'm'), ('MTK', 'm²'), ('LTR', 'l'), ('LS', 'pausch.'),
    ('ZZZ', 'ZZZ'),                                                        # unbekannter Code
    ('', 'Stk.'), (None, 'Stk.')])
def test_unit_display(stored, shown):
    from export.einvoice_model import unit_display
    assert unit_display(stored) == shown


def test_pdf_shows_readable_unit_but_xml_keeps_code(tmp_db, monkeypatch, tmp_path):
    import io
    import pdfplumber
    from export.pdf_invoice import generate_invoice_pdf
    from export.cii_invoice import generate_cii
    monkeypatch.chdir(tmp_path)
    iid = make_case(tmp_db, 'std7')
    with tmp_db._get_connection() as c:
        c.execute("UPDATE InvoiceItems SET Unit='HUR' WHERE InvoiceId=?", (iid,))
    pdf, _ = generate_invoice_pdf(tmp_db, iid)
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        words = p.pages[0].extract_text().split()
    assert 'Std.' in words and 'HUR' not in words
    xml = generate_cii(tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid), db=tmp_db)
    assert b'unitCode="HUR"' in xml
