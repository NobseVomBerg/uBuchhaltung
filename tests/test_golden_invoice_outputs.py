# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Golden-Tests: UBL-XRechnung und Layout der Rechnungs-PDF dürfen sich nicht
unbemerkt ändern (Schutz beim Umbau auf das neutrale E-Rechnungsmodell und beim
Wechsel auf PDF/A-3 mit eingebetteten Schriften).

PDF: Wörter samt Position – unabhängig von Schrift- und Kodierungsdetails. Die
eingebettete Liberation Sans hat auf 1/1000 gerundete Zeichenbreiten; innerhalb
einer Textzeile weichen Wortanfänge daher um höchstens ``X_TOLERANCE`` pt von
den Helvetica-Referenzwerten ab, vertikal und im Text gibt es keine Toleranz.
"""
import io
import os

import pdfplumber
import pytest

from export.pdf_invoice import generate_invoice_pdf
from export.xrechnung_invoice import XRechnungGenerator
from tests.golden_cases import CASES, make_case

GOLDEN = os.path.join(os.path.dirname(__file__), 'fixtures', 'golden')
UPDATE = os.environ.get('UPDATE_GOLDEN') == '1'
X_TOLERANCE = 2


def _check(name, actual):
    path = os.path.join(GOLDEN, name)
    if UPDATE:
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(actual)
    with open(path, encoding='utf-8') as f:
        assert actual == f.read(), f'Abweichung von {name}'


def pdf_layout_text(pdf_bytes: bytes) -> str:
    lines = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for n, page in enumerate(pdf.pages, 1):
            lines.append(f'# Seite {n}')
            for w in page.extract_words(keep_blank_chars=False):
                lines.append(f"{round(w['x0'])}\t{round(w['top'])}\t{w['text']}")
    return '\n'.join(lines) + '\n'


@pytest.mark.parametrize('case', sorted(CASES))
def test_ubl_xml_golden(tmp_db, case):
    iid = make_case(tmp_db, case)
    xml = XRechnungGenerator().generate_xml(
        tmp_db.get_invoice_by_id(iid), tmp_db.get_invoice_items(iid))
    _check(f'{case}.ubl.xml', xml)


@pytest.mark.parametrize('case', sorted(CASES))
def test_pdf_layout_golden(tmp_db, monkeypatch, tmp_path, case):
    monkeypatch.chdir(tmp_path)
    iid = make_case(tmp_db, case)
    pdf, _ = generate_invoice_pdf(tmp_db, iid)
    actual = pdf_layout_text(pdf)
    path = os.path.join(GOLDEN, f'{case}.pdf.txt')
    if UPDATE:
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(actual)
    with open(path, encoding='utf-8') as f:
        expected = f.read()
    a, e = actual.splitlines(), expected.splitlines()
    assert len(a) == len(e), 'andere Wortanzahl'
    for la, le in zip(a, e):
        if la.startswith('#'):
            assert la == le
            continue
        xa, ya, ta = la.split('\t', 2)
        xe, ye, te = le.split('\t', 2)
        assert (ta, ya) == (te, ye), f'{la!r} ≠ {le!r}'
        assert abs(int(xa) - int(xe)) <= X_TOLERANCE, f'{la!r} ≠ {le!r}'
