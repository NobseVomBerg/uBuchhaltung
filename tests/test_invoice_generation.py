"""
Tests for invoice PDF and XML generation.

Coverage:
- PDF: valid PDF structure, decompressible content stream, special-character escaping
- XML (XRechnung): well-formed XML, correct values, special-character escaping

Special characters tested:  ( ) / & < > \\
These characters require escaping in PDF string literals and in XML text nodes.
"""
import zlib
import xml.etree.ElementTree as ET

import pytest

from export.pdf_invoice import generate_invoice_pdf
from export.pdf_core import escape_pdf_string as _escape_pdf_string
from export.xrechnung_invoice import XRechnungGenerator

# ─────────────────────────────────────────────────────────────────────────────
# XML namespaces used by XRechnung / UBL
# ─────────────────────────────────────────────────────────────────────────────
_CBC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
_CAC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_invoice(db, suffix='001', seller_company='Muster GmbH',
                  buyer_company='Käufer AG', buyer_name='Max Mustermann',
                  items=None):
    """Insert a minimal test invoice into *db* and return its ID.

    *suffix* is appended to 'TEST-' to form the unique InvoiceNumber.
    Pass *items* as a list of dicts with keys: description, quantity,
    price_per_unit, total_net (and optionally unit).
    """
    invoice_id = db.insert_invoice({
        'invoice_number': f'TEST-{suffix}',
        'invoice_date': '2025-01-15',
        'seller_name': 'Anna Verkäufer',
        'seller_company': seller_company,
        'seller_street': 'Musterstraße 1',
        'seller_postal_code': '12345',
        'seller_city': 'Musterstadt',
        'seller_country': 'DE',
        'seller_vat_id': 'DE123456789',
        'seller_email': 'info@muster.de',
        'seller_phone': '+49 30 123456',
        'buyer_name': buyer_name,
        'buyer_company': buyer_company,
        'buyer_street': 'Kundenweg 5',
        'buyer_postal_code': '54321',
        'buyer_city': 'Kundenhausen',
        'buyer_country': 'DE',
        'currency': 'EUR',
        'bank_name': 'Testbank AG',
        'bank_iban': 'DE89370400440532013000',
        'bank_bic': 'TESTDE1X',
        'tax_category': 'S',
        'tax_rate': 19.0,   # percentage, as stored in production
        'sum_net': 100.00,
        'tax_amount': 19.00,
        'sum_gross': 119.00,
        'amount_due': 119.00,
        'status': 'finalized',
        'payment_terms': 'Zahlbar innerhalb von 14 Tagen.',
    })

    if items is None:
        items = [{'description': 'Standardartikel', 'quantity': 1.0,
                  'price_per_unit': 100.0, 'total_net': 100.0}]

    for i, item in enumerate(items, start=1):
        db.insert_invoice_item({
            'invoice_id': invoice_id,
            'position': i,
            'description': item['description'],
            'quantity': item['quantity'],
            'unit': item.get('unit', 'C62'),
            'price_per_unit': item['price_per_unit'],
            'total_net': item['total_net'],
            'tax_category': 'S',
            'tax_rate': 19.0,
        })

    return invoice_id


def _extract_content_stream(pdf_bytes: bytes) -> str:
    """Locate the FlateDecode content stream, decompress and return it as text.

    The helper finds the *first* ``/FlateDecode`` stream in the PDF, which is
    the page content stream in our test PDFs (no logo present).
    """
    flat_pos = pdf_bytes.find(b'/FlateDecode')
    assert flat_pos != -1, "No FlateDecode stream found in PDF"
    stream_start = pdf_bytes.find(b'stream\n', flat_pos) + len(b'stream\n')
    stream_end = pdf_bytes.find(b'\nendstream', stream_start)
    compressed = pdf_bytes[stream_start:stream_end]
    return zlib.decompress(compressed).decode('latin-1', errors='replace')


def _pdf_parens_balanced(stream: str) -> bool:
    """Return True when all parentheses in *stream* are balanced.

    In a PDF content stream every string literal is delimited by ``(`` and
    ``)``.  A ``\\(`` or ``\\)`` inside a literal is an escaped paren and does
    *not* affect nesting.  An unmatched ``)``, or any remaining open ``(`` at
    the end, signals a malformed stream.
    """
    depth = 0
    i = 0
    while i < len(stream):
        if stream[i] == '\\':
            i += 2          # skip backslash + next char
            continue
        if stream[i] == '(':
            depth += 1
        elif stream[i] == ')':
            if depth == 0:
                return False
            depth -= 1
        i += 1
    return depth == 0


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for the PDF string-escape helper itself
# ─────────────────────────────────────────────────────────────────────────────

class TestEscapePdfString:
    def test_no_special_chars_unchanged(self):
        assert _escape_pdf_string('Hello World') == 'Hello World'

    def test_open_paren_escaped(self):
        assert _escape_pdf_string('A(B') == r'A\(B'

    def test_close_paren_escaped(self):
        assert _escape_pdf_string('A)B') == r'A\)B'

    def test_both_parens_escaped(self):
        assert _escape_pdf_string('(test)') == r'\(test\)'

    def test_backslash_escaped(self):
        assert _escape_pdf_string('A\\B') == 'A\\\\B'

    def test_slash_not_escaped(self):
        # Forward slash is not special in PDF string literals
        assert _escape_pdf_string('Nord/Süd') == 'Nord/Süd'

    def test_combined_special_chars(self):
        result = _escape_pdf_string('Tech (Holding) GmbH & Co. KG')
        assert r'\(' in result
        assert r'\)' in result
        assert '\\(' in result   # same assertion, explicit form
        assert 'Tech' in result
        assert 'GmbH' in result


# ─────────────────────────────────────────────────────────────────────────────
# XRechnung XML generation tests
# ─────────────────────────────────────────────────────────────────────────────

class TestXRechnungXML:
    """Tests for XRechnungGenerator.generate_xml()."""

    def _xml(self, db, invoice_id):
        invoice = db.get_invoice_by_id(invoice_id)
        items = db.get_invoice_items(invoice_id)
        return XRechnungGenerator().generate_xml(invoice, items)

    # ── structural tests ──────────────────────────────────────────────────

    def test_xml_is_wellformed(self, tmp_db):
        iid = _make_invoice(tmp_db)
        root = ET.fromstring(self._xml(tmp_db, iid))
        assert root is not None

    def test_xml_contains_invoice_number(self, tmp_db):
        iid = _make_invoice(tmp_db, suffix='2025-042')
        xml = self._xml(tmp_db, iid)
        assert 'TEST-2025-042' in xml

    def test_xml_contains_seller_company(self, tmp_db):
        iid = _make_invoice(tmp_db, seller_company='Muster GmbH')
        root = ET.fromstring(self._xml(tmp_db, iid))
        names = [n.text for n in root.findall(f'.//{{{_CBC}}}Name')]
        assert 'Muster GmbH' in names

    def test_xml_contains_buyer_company(self, tmp_db):
        iid = _make_invoice(tmp_db, buyer_company='Einkauf AG')
        root = ET.fromstring(self._xml(tmp_db, iid))
        names = [n.text for n in root.findall(f'.//{{{_CBC}}}Name')]
        assert 'Einkauf AG' in names

    def test_xml_amounts(self, tmp_db):
        iid = _make_invoice(tmp_db, suffix='AMT-1')
        xml = self._xml(tmp_db, iid)
        assert '100.00' in xml   # net
        assert '19.00' in xml    # tax
        assert '119.00' in xml   # gross

    # ── special-character tests: values preserved correctly after parse ─────

    def test_xml_ampersand_in_seller_preserved(self, tmp_db):
        """& in company name: parsed XML must contain the literal ampersand."""
        company = 'Müller & Söhne GmbH'
        iid = _make_invoice(tmp_db, suffix='AMP-1', seller_company=company)
        xml = self._xml(tmp_db, iid)
        root = ET.fromstring(xml)   # must not raise
        names = [n.text for n in root.findall(f'.//{{{_CBC}}}Name')]
        assert company in names

    def test_xml_ampersand_escaped_in_raw_xml(self, tmp_db):
        """& must appear as &amp; in the serialised XML (not as a bare &)."""
        iid = _make_invoice(tmp_db, suffix='AMP-2', seller_company='A & B GmbH')
        xml = self._xml(tmp_db, iid)
        assert '&amp;' in xml

    def test_xml_angle_brackets_in_seller_preserved(self, tmp_db):
        """< and > in company name: parsed XML must round-trip correctly."""
        company = 'Entwicklung <Software> OHG'
        iid = _make_invoice(tmp_db, suffix='ANG-1', seller_company=company)
        xml = self._xml(tmp_db, iid)
        root = ET.fromstring(xml)
        names = [n.text for n in root.findall(f'.//{{{_CBC}}}Name')]
        assert company in names

    def test_xml_angle_brackets_escaped_in_raw_xml(self, tmp_db):
        """< and > must be escaped as &lt; / &gt; in the raw XML."""
        iid = _make_invoice(tmp_db, suffix='ANG-2',
                            seller_company='Dev <Core> GmbH')
        xml = self._xml(tmp_db, iid)
        assert '&lt;' in xml
        assert '&gt;' in xml

    def test_xml_parens_in_buyer_preserved(self, tmp_db):
        """Parentheses in company name are not special in XML."""
        company = 'Tech (Holding) GmbH & Co. KG'
        iid = _make_invoice(tmp_db, suffix='PAR-1', buyer_company=company)
        xml = self._xml(tmp_db, iid)
        root = ET.fromstring(xml)
        names = [n.text for n in root.findall(f'.//{{{_CBC}}}Name')]
        assert company in names

    def test_xml_slash_in_buyer_preserved(self, tmp_db):
        """Forward slash in company name is not special in XML."""
        company = 'Nord/Süd Logistik GmbH'
        iid = _make_invoice(tmp_db, suffix='SLA-1', buyer_company=company)
        xml = self._xml(tmp_db, iid)
        root = ET.fromstring(xml)
        names = [n.text for n in root.findall(f'.//{{{_CBC}}}Name')]
        assert company in names

    def test_xml_special_chars_in_article_description(self, tmp_db):
        """Article description with (, ), /, & and < > round-trips correctly."""
        desc = 'Service (Premium/Business) & Support <24/7>'
        iid = _make_invoice(tmp_db, suffix='ART-X',
                            items=[{'description': desc, 'quantity': 1.0,
                                    'price_per_unit': 100.0, 'total_net': 100.0}])
        xml = self._xml(tmp_db, iid)
        root = ET.fromstring(xml)
        item_names = [n.text for n in root.findall(f'.//{{{_CBC}}}Name')]
        assert desc in item_names

    def test_xml_all_special_chars_in_seller(self, tmp_db):
        """Company with (, ), /, & and < > in a single name."""
        company = 'A/B (C & D) <E> GmbH'
        iid = _make_invoice(tmp_db, suffix='ALL-1', seller_company=company)
        xml = self._xml(tmp_db, iid)
        root = ET.fromstring(xml)
        names = [n.text for n in root.findall(f'.//{{{_CBC}}}Name')]
        assert company in names


# ─────────────────────────────────────────────────────────────────────────────
# PDF generation tests
# ─────────────────────────────────────────────────────────────────────────────

class TestInvoicePDF:
    """Tests for generate_invoice_pdf().

    Each test changes the working directory to *tmp_path* (via monkeypatch) so
    that the PDF file written by the generator lands in an isolated temp folder.
    """

    def _generate(self, db, invoice_id, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        return generate_invoice_pdf(db, invoice_id)

    # ── structural tests ──────────────────────────────────────────────────

    def test_pdf_starts_with_header(self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db)
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        assert pdf_bytes is not None
        assert pdf_bytes.startswith(b'%PDF-1.4')

    def test_pdf_ends_with_eof(self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db, suffix='EOF-1')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        assert b'%%EOF' in pdf_bytes

    def test_pdf_file_is_written(self, tmp_db, monkeypatch, tmp_path):
        import os
        iid = _make_invoice(tmp_db, suffix='FILE-1')
        _, pdf_path = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        assert pdf_path is not None
        assert os.path.exists(pdf_path)

    def test_pdf_content_stream_decompressible(self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db, suffix='DEC-1')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        content = _extract_content_stream(pdf_bytes)
        assert len(content) > 0

    # ── content correctness ───────────────────────────────────────────────

    def test_pdf_contains_invoice_number(self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db, suffix='NUM-1')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        content = _extract_content_stream(pdf_bytes)
        assert 'TEST-NUM-1' in content

    def test_pdf_contains_buyer_company(self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db, suffix='BUY-1', buyer_company='Einkauf GmbH')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        content = _extract_content_stream(pdf_bytes)
        assert 'Einkauf GmbH' in content

    def test_pdf_contains_seller_company(self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db, suffix='SEL-1',
                            seller_company='Anbieter GmbH')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        content = _extract_content_stream(pdf_bytes)
        assert 'Anbieter GmbH' in content

    def test_pdf_amounts_in_content(self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db, suffix='AMT-2')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        content = _extract_content_stream(pdf_bytes)
        # Beträge im PDF im deutschen Format (Komma als Dezimaltrenner)
        assert '100,00' in content   # net
        assert '119,00' in content   # gross

    # ── parenthesis escaping ──────────────────────────────────────────────

    def test_pdf_parens_in_seller_company_stream_balanced(
            self, tmp_db, monkeypatch, tmp_path):
        """Unescaped ( ) in a company name would break the content stream."""
        iid = _make_invoice(tmp_db, suffix='PAR-S1',
                            seller_company='Tech (Holding) GmbH')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        assert _pdf_parens_balanced(_extract_content_stream(pdf_bytes))

    def test_pdf_parens_in_seller_company_text_in_stream(
            self, tmp_db, monkeypatch, tmp_path):
        """After escaping, the company name must appear as Tech \\(Holding\\) GmbH."""
        iid = _make_invoice(tmp_db, suffix='PAR-S2',
                            seller_company='Tech (Holding) GmbH')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        content = _extract_content_stream(pdf_bytes)
        assert r'Tech \(Holding\) GmbH' in content

    def test_pdf_parens_in_buyer_company_stream_balanced(
            self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db, suffix='PAR-B1',
                            buyer_company='Nord (Ost) Handel GmbH')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        assert _pdf_parens_balanced(_extract_content_stream(pdf_bytes))

    def test_pdf_parens_in_buyer_company_text_in_stream(
            self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db, suffix='PAR-B2',
                            buyer_company='Nord (Ost) Handel GmbH')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        content = _extract_content_stream(pdf_bytes)
        assert r'Nord \(Ost\) Handel GmbH' in content

    def test_pdf_parens_in_article_description_stream_balanced(
            self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(
            tmp_db, suffix='PAR-A1',
            items=[{'description': 'Beratung (remote/vor Ort)',
                    'quantity': 2.0, 'price_per_unit': 50.0, 'total_net': 100.0}])
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        assert _pdf_parens_balanced(_extract_content_stream(pdf_bytes))

    def test_pdf_parens_in_article_description_text_in_stream(
            self, tmp_db, monkeypatch, tmp_path):
        """Description is truncated to 35 chars; escaping must still work."""
        iid = _make_invoice(
            tmp_db, suffix='PAR-A2',
            items=[{'description': 'Service (Premium)',
                    'quantity': 1.0, 'price_per_unit': 100.0, 'total_net': 100.0}])
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        content = _extract_content_stream(pdf_bytes)
        assert r'Service \(Premium\)' in content

    # ── slash (no escaping needed, but must not corrupt the stream) ────────

    def test_pdf_slash_in_buyer_company_stream_balanced(
            self, tmp_db, monkeypatch, tmp_path):
        iid = _make_invoice(tmp_db, suffix='SLA-1',
                            buyer_company='Nord/Süd Logistik GmbH')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        assert _pdf_parens_balanced(_extract_content_stream(pdf_bytes))

    def test_pdf_slash_in_buyer_company_text_in_stream(
            self, tmp_db, monkeypatch, tmp_path):
        """/ is not special in PDF string literals; it must appear verbatim."""
        iid = _make_invoice(tmp_db, suffix='SLA-2',
                            buyer_company='Nord/Süd Logistik GmbH')
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        content = _extract_content_stream(pdf_bytes)
        assert 'Nord/S' in content   # ü is latin-1

    # ── combined special characters ───────────────────────────────────────

    def test_pdf_all_special_chars_stream_balanced(
            self, tmp_db, monkeypatch, tmp_path):
        """Company name with (, ), /, & all in one string."""
        iid = _make_invoice(
            tmp_db, suffix='ALL-2',
            seller_company='A/B (C & D) GmbH',
            buyer_company='X/Y (Z) AG',
            items=[{'description': 'Pos. (A/B) & mehr',
                    'quantity': 1.0, 'price_per_unit': 100.0, 'total_net': 100.0}])
        pdf_bytes, _ = self._generate(tmp_db, iid, monkeypatch, tmp_path)
        assert _pdf_parens_balanced(_extract_content_stream(pdf_bytes))
