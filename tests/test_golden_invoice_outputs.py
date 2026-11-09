# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Golden-Tests: UBL-XRechnung und Layout der Rechnungs-PDF dürfen sich nicht
unbemerkt ändern (Schutz beim Umbau auf das neutrale E-Rechnungsmodell und beim
Wechsel auf PDF/A-3 mit eingebetteten Schriften).

PDF: Wörter samt Position (auf 1 pt gerundet) – unabhängig von Schrift- und
Kodierungsdetails.
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
    _check(f'{case}.pdf.txt', pdf_layout_text(pdf))
