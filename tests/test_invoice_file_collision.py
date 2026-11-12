# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Dateinamen der Rechnungsdokumente (2.1.2, T98): Die Datei eines anderen Belegs wird nie überschrieben.

Der Dateiname ist „<Nummer> <Kunde>“ ohne die Zeichen, die ein Dateiname nicht verträgt – zwei
verschiedene Nummern („26R/1“, „26R:1“) können so denselben Namen ergeben. Gehört der Zielpfad schon
der abgelegten Datei eines anderen Belegs, bekommt der Name die ID der Rechnung als Zusatz; Download
und Anhang finden auch ohne gespeicherten Pfad nie die Datei eines anderen Belegs. Alle Namen und
Nummern sind erfundene Testwerte.
"""
import os

import pytest

import userctx
from db import Database
from export.invoice_files import (generate_invoice_documents, invoice_file_path, stored_invoice_document,
                                  stored_invoice_path)
from export.pdf_invoice import generate_invoice_pdf
from tests.document_helpers import read_file, write_file
from tests.test_invoice_documents import _make_invoice


@pytest.fixture
def inv_db(tmp_path, monkeypatch):
    monkeypatch.setattr(userctx, 'DATA_ROOT', str(tmp_path))
    db = Database()
    db.load_test_seed_data()
    return db


def two_with_the_same_name(db):
    first = _make_invoice(db, number='26R/1', route_id='991-12345-67')
    second = _make_invoice(db, number='26R:1', route_id='991-12345-67')
    return first, second


def test_two_numbers_can_share_a_file_name(inv_db):
    first, second = two_with_the_same_name(inv_db)
    a, b = inv_db.get_invoice_by_id(first), inv_db.get_invoice_by_id(second)
    assert invoice_file_path(a, 'pdf') == invoice_file_path(b, 'pdf')            # ohne Prüfung: derselbe Pfad


def test_the_second_document_gets_the_id_in_its_name_and_the_first_stays(inv_db):
    first, second = two_with_the_same_name(inv_db)
    _, pdf_a = generate_invoice_pdf(inv_db, first)
    marked = write_file(pdf_a, b'%PDF-1.4 Rechnung A')
    _, pdf_b = generate_invoice_pdf(inv_db, second)
    assert os.path.basename(pdf_a) == '26R1 Malermeister Pinsel.pdf'
    assert os.path.basename(pdf_b) == f'26R1 Malermeister Pinsel (ID {second}).pdf'
    assert read_file(marked) == b'%PDF-1.4 Rechnung A'                        # nicht überschrieben
    assert inv_db.get_invoice_by_id(first)[41] == pdf_a and inv_db.get_invoice_by_id(second)[41] == pdf_b
    assert os.path.isfile(pdf_a) and os.path.isfile(pdf_b)


def test_the_xml_follows_and_downloads_deliver_each_its_own_file(inv_db):
    first, second = two_with_the_same_name(inv_db)
    a = generate_invoice_documents(inv_db, first)
    b = generate_invoice_documents(inv_db, second)
    assert a['xml_filename'] == '26R1 Malermeister Pinsel.xml'
    assert b['xml_filename'] == f'26R1 Malermeister Pinsel (ID {second}).xml'
    assert read_file(a['xml_path']) != read_file(b['xml_path'])                  # die Nummer steht im XML
    assert b'26R/1' in read_file(a['xml_path']) and b'26R:1' in read_file(b['xml_path'])
    assert stored_invoice_document(inv_db, first, 'pdf')[1] == '26R1 Malermeister Pinsel.pdf'
    assert stored_invoice_document(inv_db, second, 'pdf')[1] == f'26R1 Malermeister Pinsel (ID {second}).pdf'


def test_generating_again_keeps_each_name(inv_db):
    first, second = two_with_the_same_name(inv_db)
    generate_invoice_documents(inv_db, first)
    paths = generate_invoice_documents(inv_db, second)['pdf_path']
    for _ in range(2):
        assert generate_invoice_documents(inv_db, first)['pdf_path'].endswith('26R1 Malermeister Pinsel.pdf')
        assert generate_invoice_documents(inv_db, second)['pdf_path'] == paths


def test_a_leftover_file_that_belongs_to_no_document_is_still_overwritten(inv_db):
    """Die Prüfung gilt abgelegten Dateien anderer Belege, nicht jeder Datei im Ordner."""
    invoice_id = _make_invoice(inv_db)
    leftover = write_file(os.path.join(userctx.DATA_ROOT, 'invoices', '2026', '26R001 Malermeister Pinsel.pdf'),
                          b'%PDF-1.4 Rest')
    _, path = generate_invoice_pdf(inv_db, invoice_id)
    assert path == leftover and read_file(leftover).startswith(b'%PDF') and read_file(leftover) != b'%PDF-1.4 Rest'


def test_a_missing_stored_path_never_finds_the_file_of_another_document(inv_db):
    first, second = two_with_the_same_name(inv_db)
    _, pdf_a = generate_invoice_pdf(inv_db, first)
    inv_db.update_invoice_pdf_path(second, None)                                 # der Pfad fehlt (Altbestand, Umzug)
    assert stored_invoice_path(inv_db, second, 'pdf') == (None, 'Kein PDF vorhanden.')
    assert stored_invoice_document(inv_db, second, 'pdf')[0] is None
    assert stored_invoice_path(inv_db, first, 'pdf')[0] == pdf_a                 # die erste findet ihre Datei


def test_the_fallback_by_name_still_works_for_a_document_nobody_else_uses(inv_db):
    invoice_id = _make_invoice(inv_db)
    _, pdf = generate_invoice_pdf(inv_db, invoice_id)
    inv_db.update_invoice_pdf_path(invoice_id, None)
    assert stored_invoice_path(inv_db, invoice_id, 'pdf')[0] == pdf
