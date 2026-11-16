# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Steuernummer und USt-IdNr. im Fuß von Rechnungs- und Angebots-PDF (2.3.5).

Bis 2.3.4 las der Fuß die Nummer aus dem **Kontakt der eigenen Firma** (live) und druckte sie immer als „USt-IdNr:“ –
auch eine Steuernummer, und nur, wenn die Rechnung eine eigene Firma hatte. Die Momentaufnahme der Rechnung
(``Invoices.SellerVATID``) las der Fuß nie. Jetzt gilt die Momentaufnahme: Eine USt-IdNr. druckt „USt-IdNr.: …“, eine
Steuernummer „Steuer-Nr.: …“ (``export.einvoice_model.tax_id_kind``), und ein später geänderter Kontakt ändert das PDF nicht.
Der Kontakt springt nur ein, wenn die Momentaufnahme leer ist (Import). Beim Angebot gilt dasselbe. Fuß, CII und UBL nennen
denselben Wert in derselben Art – auch für Angaben, die bis 2.3.5 falsch eingeordnet wurden („StNr. 1338150815“ galt als
USt-IdNr., „USt-IdNr. DE123456789“ als Steuernummer).

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import io
from types import SimpleNamespace

import pdfplumber
import pytest

import userctx
from export.pdf_invoice import generate_invoice_pdf
from export.pdf_quote import generate_quote_pdf
from tests.document_helpers import add_contacts, add_document
from tests.test_xrechnung_gaps import _cii_registrations, _documents, _ubl_registrations

USER = 'anna'


@pytest.fixture
def world(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    userctx.set_user(USER)
    own, customer = add_contacts(tmp_db)             # die eigene Firma trägt DE123456789
    return SimpleNamespace(db=tmp_db, own=own, customer=customer)


def _text(pdf):
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        return ' '.join(' '.join((page.extract_text() or '').split()) for page in document.pages)


def _invoice_text(w, **over):
    iid = add_document(w.db, '26R001', own_id=w.own, customer_id=w.customer, **over)
    return iid, _text(generate_invoice_pdf(w.db, iid)[0])


def _quote_text(w, **over):
    qid = add_document(w.db, '26A001', 'quote', own_id=w.own, customer_id=w.customer, **over)
    return qid, _text(generate_quote_pdf(w.db, qid)[0])


def _set_contact_tax_id(w, value):
    with w.db._get_connection() as connection:
        connection.execute('UPDATE CompanyDetails SET TaxID = ? WHERE ContactID = ?', (value, w.own))


STAMPED = [
    ('DE123456789', 'USt-IdNr.: DE123456789'),
    ('DE 123 456 789', 'USt-IdNr.: DE123456789'),          # Leerzeichen fallen weg, wie in der E-Rechnung
    ('de-123.456 789', 'USt-IdNr.: DE123456789'),          # Bindestriche und Punkte ebenso, in Großschreibung
    ('USt-IdNr. DE123456789', 'USt-IdNr.: DE123456789'),   # eine Bezeichnung davor fällt weg (galt früher als Steuernummer)
    ('CHE-123.456.789 MWST', 'USt-IdNr.: CHE123456789MWST'),
    ('12/345/67890', 'Steuer-Nr.: 12/345/67890'),
    ('123 456 78901', 'Steuer-Nr.: 123 456 78901'),        # eine Steuernummer bleibt, wie sie eingegeben wurde
    ('StNr. 1338150815', 'Steuer-Nr.: 1338150815'),        # galt früher als USt-IdNr. („ST“ als Länderkürzel)
    ('Steuernummer: 12/345/67890', 'Steuer-Nr.: 12/345/67890'),
    ('BY 123 456 78901', 'Steuer-Nr.: BY 123 456 78901'),  # das Bundesland ist kein Länderkürzel
    ('NW 133 8150 8150', 'Steuer-Nr.: NW 133 8150 8150'),
    ('HRB 12345', 'Steuer-Nr.: HRB 12345'),
    ('DE12345678', 'Steuer-Nr.: DE12345678'),              # eine Ziffer zu wenig: keine USt-IdNr.
]


@pytest.mark.parametrize('stored, line', STAMPED)
@pytest.mark.parametrize('document', [_invoice_text, _quote_text])
def test_the_footer_prints_the_snapshot_with_the_label_of_its_kind(world, document, stored, line):
    _, text = document(world, seller_vat_id=stored)

    assert line in text
    assert 'USt-IdNr:' not in text                          # die Schreibweise bis 2.3.4 (ohne Punkt)


@pytest.mark.parametrize('stored, line', STAMPED)
@pytest.mark.parametrize('document', [_invoice_text, _quote_text])
def test_a_changed_contact_does_not_change_the_pdf(world, document, stored, line):
    _set_contact_tax_id(world, 'DE987654321')                # der Kontakt trägt zuerst eine andere Nummer

    _, text = document(world, seller_vat_id=stored)

    assert line in text and 'DE987654321' not in text


def test_a_regenerated_invoice_keeps_its_stamped_number_after_the_contact_changed(world):
    iid, first = _invoice_text(world, seller_vat_id='DE123456789')
    _set_contact_tax_id(world, '12/345/67890')               # später wechselt der Kontakt auf eine Steuernummer

    second = _text(generate_invoice_pdf(world.db, iid)[0])

    assert 'USt-IdNr.: DE123456789' in first and 'USt-IdNr.: DE123456789' in second
    assert 'Steuer-Nr.' not in second and '12/345/67890' not in second


@pytest.mark.parametrize('contact, line', [
    ('DE-123 456 789', 'USt-IdNr.: DE123456789'),
    ('12/345/67890', 'Steuer-Nr.: 12/345/67890'),
])
@pytest.mark.parametrize('document', [_invoice_text, _quote_text])
def test_the_contact_stands_in_for_an_empty_snapshot(world, document, contact, line):
    _set_contact_tax_id(world, contact)

    _, text = document(world, seller_vat_id='')

    assert line in text


@pytest.mark.parametrize('document', [_invoice_text, _quote_text])
def test_without_a_number_anywhere_the_footer_prints_no_line(world, document):
    _set_contact_tax_id(world, '')

    _, text = document(world, seller_vat_id='')

    assert 'USt-IdNr' not in text and 'Steuer-Nr' not in text


def test_an_invoice_without_an_own_company_still_prints_its_snapshot(world):
    """Bis 2.3.4 druckte der Fuß ohne eigene Firma gar keine Nummer, obwohl die Momentaufnahme eine trug."""
    iid = add_document(world.db, '26R002', own_id=None, customer_id=world.customer, seller_vat_id='DE123456789')

    assert 'USt-IdNr.: DE123456789' in _text(generate_invoice_pdf(world.db, iid)[0])


# Fuß, CII (BT-31/BT-32) und UBL (``cac:PartyTaxScheme``) nennen denselben Wert in derselben Art: (gespeichert, Art, Wert)
AGREED = [(stored, 'vat' if line.startswith('USt-IdNr.') else 'tax', line.split(': ', 1)[1]) for stored, line in STAMPED]
SCHEMES = {'vat': ('VA', 'VAT'), 'tax': ('FC', 'TAX')}


@pytest.mark.parametrize('stored, kind, number', AGREED)
def test_footer_cii_and_ubl_name_the_same_number_in_the_same_kind(world, stored, kind, number):
    iid, text = _invoice_text(world, seller_vat_id=stored)           # die Momentaufnahme gilt, der Kontakt (DE123456789) schweigt
    cii, ubl = _documents(world, iid)
    scheme_cii, scheme_ubl = SCHEMES[kind]
    other = SCHEMES['tax' if kind == 'vat' else 'vat']

    label = 'USt-IdNr.' if kind == 'vat' else 'Steuer-Nr.'
    assert f'{label}: {number}' in text
    assert _cii_registrations(cii, 'SellerTradeParty') == {scheme_cii: number}
    assert _ubl_registrations(ubl, 'AccountingSupplierParty') == {scheme_ubl: number}
    assert other[0] not in _cii_registrations(cii, 'SellerTradeParty')
    assert other[1] not in _ubl_registrations(ubl, 'AccountingSupplierParty')
