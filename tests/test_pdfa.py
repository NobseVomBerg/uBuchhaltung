# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Struktur von PDF/A-3b in ``pdf_core``: Schriften, XMP, OutputIntent, Anhang."""
import io
import re
import xml.etree.ElementTree as ET
import zlib

import pdfplumber
import pytest
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1, stream_value

from export.pdf_core import build_multi_page_pdf, build_single_page_pdf

OPS = ['BT', '/F1 10 Tf', '50 780 Td', '(Gr\\344\\337e \\200 und \\(Klammern\\)) Tj', 'ET',
       'BT', '/F2 12 Tf', '50 760 Td', '(Fett) Tj', 'ET',
       'BT', '/F3 9 Tf', '50 740 Td', '(Kursiv) Tj', 'ET']
OPS_UNICODE = ['BT', '/F1 10 Tf', '50 780 Td', '(Größe € – „Zitat“ • Fett) Tj', 'ET',
               'BT', '/F2 12 Tf', '50 760 Td', '(Fett) Tj', 'ET',
               'BT', '/F3 9 Tf', '50 740 Td', '(Kursiv) Tj', 'ET']
FX_XML = b"<?xml version='1.0' encoding='utf-8'?><rsm:x xmlns:rsm='urn:x'>\xc3\xa4</rsm:x>"


def _doc(pdf):
    return PDFDocument(PDFParser(io.BytesIO(pdf)))


def _xmp(pdf):
    doc = _doc(pdf)
    meta = stream_value(doc.catalog['Metadata'])
    return meta.get_data().decode('utf-8')


@pytest.fixture(scope='module')
def pdf_plain():
    return build_single_page_pdf(OPS_UNICODE, title='Rechnung A&B <1>', author='Müller (Söhne)')


def test_header_and_trailer(pdf_plain):
    assert pdf_plain.startswith(b'%PDF-1.7\n%')
    assert all(b > 127 for b in pdf_plain[10:14])          # Binär-Kommentar
    assert re.search(rb'/ID \[<[0-9a-f]{32}> <[0-9a-f]{32}>\]', pdf_plain)
    doc = _doc(pdf_plain)
    assert doc.info[0]['Title'].decode('latin-1') == 'Rechnung A&B <1>'


def test_all_fonts_embedded_subset(pdf_plain):
    doc = _doc(pdf_plain)
    pages = list(PDFPage.create_pages(doc))
    fonts = resolve1(resolve1(pages[0].resources)['Font'])
    assert set(fonts) == {'F1', 'F2', 'F3'}
    for ref in fonts.values():
        font = resolve1(ref)
        assert font['Subtype'].name == 'TrueType'
        assert re.match(r'^[A-Z]{6}\+LiberationSans', font['BaseFont'].name)
        desc = resolve1(font['FontDescriptor'])
        assert 'FontFile2' in desc                      # eingebettet
        data = stream_value(desc['FontFile2']).get_data()
        assert len(data) < 60_000                       # Untermenge, nicht die ganze Schrift
        assert font['Encoding'].name == 'WinAnsiEncoding'
    assert not re.search(rb'/BaseFont /Helvetica', pdf_plain)


def test_only_used_fonts_are_embedded():
    pdf = build_single_page_pdf(['BT', '/F1 10 Tf', '50 780 Td', '(nur Regular) Tj', 'ET'])
    page = list(PDFPage.create_pages(_doc(pdf)))[0]
    assert set(resolve1(resolve1(page.resources)['Font'])) == {'F1'}


def test_text_roundtrip_with_special_characters(pdf_plain):
    with pdfplumber.open(io.BytesIO(pdf_plain)) as pdf:
        text = pdf.pages[0].extract_text()
    assert 'Größe € – „Zitat“ • Fett' in text
    assert 'Fett' in text and 'Kursiv' in text


def test_xmp_pdfa_identification_and_escaping(pdf_plain):
    xmp = _xmp(pdf_plain)
    root = ET.fromstring(xmp.split('?>', 1)[1].rsplit('<?xpacket', 1)[0])   # wohlgeformt
    ns = {'pdfaid': 'http://www.aiim.org/pdfa/ns/id/', 'dc': 'http://purl.org/dc/elements/1.1/',
          'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'}
    assert root.find('.//pdfaid:part', ns).text == '3'
    assert root.find('.//pdfaid:conformance', ns).text == 'B'
    assert root.find('.//dc:title//rdf:li', ns).text == 'Rechnung A&B <1>'
    assert root.find('.//dc:creator//rdf:li', ns).text == 'Müller (Söhne)'
    assert 'factur' not in xmp.lower()                   # ohne Anhang kein Factur-X-Schema


def test_output_intent_with_icc_profile(pdf_plain):
    doc = _doc(pdf_plain)
    intent = resolve1(resolve1(doc.catalog['OutputIntents'])[0])
    assert intent['S'].name == 'GTS_PDFA1'
    icc = stream_value(intent['DestOutputProfile'])
    assert icc['N'] == 3
    data = icc.get_data()
    assert data[36:40] == b'acsp' and data[16:20] == b'RGB '
    assert int.from_bytes(data[0:4], 'big') == len(data)


def test_no_attachment_without_xml(pdf_plain):
    assert b'/EmbeddedFile' not in pdf_plain and b'/AF' not in pdf_plain


def test_multi_page():
    pdf = build_multi_page_pdf([OPS, OPS, OPS])
    assert len(list(PDFPage.create_pages(_doc(pdf)))) == 3


# ── Factur-X-Anhang ──────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def pdf_fx():
    return build_multi_page_pdf([OPS], title='Rechnung 1', author='A', facturx_xml=FX_XML)


def test_embedded_file_and_af(pdf_fx):
    doc = _doc(pdf_fx)
    cat = doc.catalog
    spec = resolve1(resolve1(cat['AF'])[0])
    assert spec['AFRelationship'].name == 'Alternative'
    assert spec['F'] == b'factur-x.xml' and spec['UF'] == b'factur-x.xml'
    ef = stream_value(resolve1(spec['EF'])['F'])
    assert ef['Subtype'].name == 'text/xml'
    assert ef['Params']['Size'] == len(FX_XML)
    assert ef['Params']['ModDate'].startswith(b'D:')
    assert ef.get_data() == FX_XML
    names = resolve1(resolve1(resolve1(cat['Names'])['EmbeddedFiles'])['Names'])
    assert names[0] == b'factur-x.xml'


def test_xmp_contains_facturx_extension_schema(pdf_fx):
    xmp = _xmp(pdf_fx)
    root = ET.fromstring(xmp.split('?>', 1)[1].rsplit('<?xpacket', 1)[0])
    fx = '{urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#}'
    assert root.find(f'.//{fx}DocumentType').text == 'INVOICE'
    assert root.find(f'.//{fx}DocumentFileName').text == 'factur-x.xml'
    assert root.find(f'.//{fx}Version').text == '1.0'
    assert root.find(f'.//{fx}ConformanceLevel').text == 'EN 16931'
    ext = 'http://www.aiim.org/pdfa/ns/schema#'
    assert root.find(f'.//{{{ext}}}namespaceURI').text == 'urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#'
    names = [e.text for e in root.iter('{http://www.aiim.org/pdfa/ns/property#}name')]
    assert names == ['DocumentFileName', 'DocumentType', 'Version', 'ConformanceLevel']


def test_xref_offsets_valid(pdf_fx):
    start = int(re.search(rb'startxref\n(\d+)\n%%EOF', pdf_fx).group(1))
    assert pdf_fx[start:start + 4] == b'xref'
    table = pdf_fx[start:].split(b'trailer')[0].splitlines()[2:]
    for num, line in enumerate(table[1:], 1):
        off = int(line[:10])
        assert pdf_fx[off:].startswith(f'{num} 0 obj'.encode())
