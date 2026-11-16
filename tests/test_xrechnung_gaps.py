# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Pflichtangaben der E-Rechnung (2.3.5): Fehlt dem UBL etwas, entsteht keines – und der Nutzer erfährt, was.

Bis 2.3.4 prüfte nichts, ob die Angaben einer Rechnung für die XRechnung reichen: Eine Rechnung mit Bestellnummer
bekam ein UBL ohne Käuferreferenz (BR-DE-15), eine ohne IBAN eines ohne Zahlungsanweisung (BR-DE-1), und das Portal des
Empfängers wies es ab. Jetzt prüft ``einvoice_gaps`` das Modell: Fehlt eine Angabe, schreibt ``generate_invoice_documents``
kein UBL (ein früher abgelegtes verschwindet), das PDF mit der eingebetteten CII entsteht immer, und der Text sagt, was
fehlt. Beim Versenden fragt die Maske vorher nach. Die Liste der Angaben ist an Mustang (XSD und Schematron der
XRechnung 3.0 und EN 16931) gemessen. Das UBL entsteht nur noch bei Leitweg-ID/Käuferreferenz, nicht mehr bei einer
Bestellnummer allein.

Dazu die Einordnung von Steuernummer und USt-IdNr. (BT-31/BT-32/BT-48): Eine Steuernummer ist keine USt-IdNr. und steht
in CII und UBL nicht als solche (BR-CO-09); der Käufer hat in EN 16931 kein Feld für eine Steuernummer. Die Tabelle der
Länder und die Bezeichnungen vor der Nummer prüft test_einvoice_model.py, Fuß/CII/UBL im Vergleich test_pdf_footer_tax_id.py.

Ein ``#`` im Zahlungshinweis ist ebenfalls eine Lücke (BR-DE-18: In der XRechnung ist es der Skonto-Angabe vorbehalten): Das
UBL entsteht nicht, das PDF mit der eingebetteten CII bleibt – dort ist der Text gültig.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import io
import json
import os
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1, stream_value

import server.handlers as handlers
import server.pages_invoice as pages_invoice
import userctx
from export.cii_invoice import NS, generate_cii
from export.einvoice_model import HASH_IN_PAYMENT_TERMS_GAP, Gap, build_einvoice, einvoice_gaps
from export.invoice_files import generate_invoice_documents, stored_invoice_path, wants_xrechnung
from export.xrechnung_invoice import XRechnungGenerator
from server.pages_invoice import PageInvoice
from tests.document_helpers import (CUSTOMER, INVOICE_NUMBER, add_contacts, add_document, make_books, post_action, row,
                                    set_column, write_file, XML_PATH)
from tests.http_client import get
from tests.invoice_world import save_open
from tests.test_invoice_storno import at, sent, world as storno_world       # noqa: F401  (Fixture)

USER = 'anna'
ROUTE_ID = '991-12345-67'
UBL = {'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
       'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'}
XRECHNUNG_SENTENCE = 'XRechnung (XML) nicht erstellt – es fehlt: '
PDF_SENTENCE = 'Die E-Rechnung im PDF ist unvollständig – es fehlt: '


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user(USER)
    own, customer = add_contacts(tmp_db)
    return SimpleNamespace(db=tmp_db, own=own, customer=customer)


def _invoice(w, number='26R001', **over):
    """Eine vollständige Rechnung: Leitweg-ID, Bankverbindung, Anschrift, Telefon und E-Mail beider Seiten (die des Kunden
    steht im Kontakt), USt-IdNr. in der Momentaufnahme. ``over`` überschreibt Felder von ``insert_invoice`` (auch
    ``own_id``/``customer_id``)."""
    fields = dict(own_id=w.own, customer_id=w.customer, buyer_route_id=ROUTE_ID, seller_vat_id='DE123456789',
                  bank_name='Testbank AG', bank_iban='DE89370400440532013000', bank_bic='TESTDE1X')
    fields.update(over)
    return add_document(w.db, number, **fields)


def _embedded_cii(pdf_path):
    with open(pdf_path, 'rb') as f:
        doc = PDFDocument(PDFParser(io.BytesIO(f.read())))
    spec = resolve1(resolve1(doc.catalog['AF'])[0])
    return ET.fromstring(stream_value(resolve1(spec['EF'])['F']).get_data())


# ── Eine vollständige Rechnung ───────────────────────────────────────────────

def test_a_complete_invoice_has_no_gap_and_gets_its_xrechnung(world):
    invoice_id = _invoice(world)
    model = build_einvoice(world.db, world.db.get_invoice_by_id(invoice_id), world.db.get_invoice_items(invoice_id))
    assert einvoice_gaps(model) == []

    result = generate_invoice_documents(world.db, invoice_id)

    assert result['error'] is None
    assert os.path.isfile(result['xml_path']) and os.path.isfile(result['pdf_path'])
    assert row(world.db, invoice_id)[XML_PATH] == result['xml_path']


# ── Jede Lücke einzeln: kein UBL, das PDF mit der CII bleibt, der Text nennt die Angabe ──

# (Text im Fehler, Änderung an der Rechnung). ``own_id=None``: ohne Kontakt gibt es keinen Rückgriff auf dessen Angaben.
XRECHNUNG_GAPS = {
    'Leitweg-ID': ('Leitweg-ID (Käuferreferenz)', dict(buyer_route_id='')),
    'IBAN': ('Bankverbindung (IBAN)', dict(bank_iban='')),
    'PLZ-eigene-Firma': ('PLZ der eigenen Firma', dict(seller_postal_code='')),
    'Ort-eigene-Firma': ('Ort der eigenen Firma', dict(seller_city='')),
    'Ansprechpartner': ('Ansprechpartner der eigenen Firma', dict(seller_name='')),
    'Telefon': ('Telefon der eigenen Firma', dict(seller_phone='')),
    'E-Mail-eigene-Firma': ('E-Mail der eigenen Firma', dict(seller_email='', own_id=None)),
    'PLZ-Kunde': ('PLZ des Kunden', dict(buyer_postal_code='')),
    'Ort-Kunde': ('Ort des Kunden', dict(buyer_city='')),
    'E-Mail-Kunde': ('E-Mail des Kunden', dict(customer_id=None)),
}
EN16931_GAPS = {
    'USt-IdNr': ('Steuernummer oder USt-IdNr. der eigenen Firma', dict(seller_vat_id='', own_id=None)),
}


@pytest.mark.parametrize('text, change', list(XRECHNUNG_GAPS.values()), ids=list(XRECHNUNG_GAPS))
def test_a_missing_detail_of_the_xrechnung_keeps_the_ubl_away_and_the_pdf_complete(world, text, change):
    invoice_id = _invoice(world, **change)

    result = generate_invoice_documents(world.db, invoice_id, with_xml=True)

    assert result['xml_path'] is None and result['xml_filename'] is None
    assert not row(world.db, invoice_id)[XML_PATH]
    assert os.path.isfile(result['pdf_path'])                                 # das PDF entsteht immer, mit CII
    assert _embedded_cii(result['pdf_path']).find('rsm:ExchangedDocument/ram:ID', NS).text == '26R001'
    assert result['error'] == f'{XRECHNUNG_SENTENCE}{text}.'                  # nur diese Angabe, kein zweiter Satz


@pytest.mark.parametrize('text, change', list(EN16931_GAPS.values()), ids=list(EN16931_GAPS))
def test_a_missing_detail_of_the_embedded_invoice_is_named_for_the_pdf_and_keeps_the_ubl_away(world, text, change):
    invoice_id = _invoice(world, **change)

    result = generate_invoice_documents(world.db, invoice_id)                  # mit Leitweg-ID: das UBL wäre gewollt

    assert result['xml_path'] is None and not row(world.db, invoice_id)[XML_PATH]
    assert os.path.isfile(result['pdf_path'])
    # Die Angabe steht genau einmal, im Satz über das PDF; davor steht ausdrücklich, dass das UBL nicht entstanden ist
    assert result['error'] == f'XRechnung (XML) nicht erstellt. {PDF_SENTENCE}{text}.' and result['error'].count(text) == 1


def test_a_detail_missing_for_the_pdf_alone_is_named_without_a_ubl_in_view(world):
    invoice_id = _invoice(world, buyer_route_id='', seller_vat_id='', own_id=None)

    result = generate_invoice_documents(world.db, invoice_id)                  # keine Leitweg-ID: kein UBL gewollt

    assert result['xml_path'] is None and os.path.isfile(result['pdf_path'])
    assert result['error'] == f'{PDF_SENTENCE}Steuernummer oder USt-IdNr. der eigenen Firma.'


def test_an_unreadable_skonto_is_a_gap_and_not_a_silent_loss(world):
    invoice_id = _invoice(world)
    set_column(world.db, invoice_id, 'SkontoDays', 7)
    set_column(world.db, invoice_id, 'SkontoPercent', 2.333)                   # per SQL-Konsole: mehr als zwei Stellen

    result = generate_invoice_documents(world.db, invoice_id)

    assert result['xml_path'] is None and os.path.isfile(result['pdf_path'])
    assert result['error'] == (f'XRechnung (XML) nicht erstellt. {PDF_SENTENCE}'
                               'Skonto (die gespeicherten Werte sind nicht lesbar).')


# Die drei Fälle der fachlichen Prüfung: jede mit „#“ beginnende Zeile muss der Skonto-Angabe gleichen (BR-DE-18)
HASH_CASES = {
    'Kundennummer-neben-der-Skonto-Zeile': dict(payment_terms='Bitte Kunden-Nr. #4711# angeben.', skonto_days=7,
                                                skonto_percent=2),
    'Freitext-beginnt-mit-Raute': dict(payment_terms='#4711 bitte angeben'),
    'Freitext-mit-eigener-Skonto-Zeile': dict(payment_terms='Zahlbar in 14 Tagen.\n#SKONTO#TAGE=7#PROZENT=2.00#'),
}


@pytest.mark.parametrize('change', list(HASH_CASES.values()), ids=list(HASH_CASES))
def test_a_hash_in_the_payment_terms_keeps_the_ubl_away_but_the_embedded_invoice_stays(world, change):
    invoice_id = _invoice(world, **change)

    result = generate_invoice_documents(world.db, invoice_id, with_xml=True)

    assert result['xml_path'] is None and not row(world.db, invoice_id)[XML_PATH]
    assert os.path.isfile(result['pdf_path'])
    assert result['error'] == f'{XRECHNUNG_SENTENCE}{HASH_IN_PAYMENT_TERMS_GAP}.' and result['incomplete'] is True
    # die CII im PDF trägt den Text (wie überall mit zusammengezogenem Leerraum), das „#“ bleibt: Dort ist er gültig – Mustang
    # meldet das PDF als gültig
    description = _embedded_cii(result['pdf_path']).find('.//ram:SpecifiedTradePaymentTerms/ram:Description', NS).text
    assert ' '.join(change['payment_terms'].split()) in description


def test_the_hash_gap_belongs_to_the_xrechnung_alone(world):
    invoice_id = _invoice(world, payment_terms='Kunden-Nr. #4711#')
    model = build_einvoice(world.db, world.db.get_invoice_by_id(invoice_id), world.db.get_invoice_items(invoice_id))

    assert einvoice_gaps(model) == [Gap(HASH_IN_PAYMENT_TERMS_GAP, 'xrechnung')]
    assert 'Skonto' in HASH_IN_PAYMENT_TERMS_GAP and '‚#‘' in HASH_IN_PAYMENT_TERMS_GAP


def test_a_hash_does_not_matter_when_no_ubl_is_in_view(world):
    invoice_id = _invoice(world, buyer_route_id=None, payment_terms='Kunden-Nr. #4711#')

    result = generate_invoice_documents(world.db, invoice_id)                  # keine Leitweg-ID: kein UBL gewollt

    assert result['error'] is None and result['xml_path'] is None and os.path.isfile(result['pdf_path'])


@pytest.mark.parametrize('skonto', [{}, dict(skonto_days=7, skonto_percent=2)], ids=['ohne-Skonto', 'mit-Skonto'])
def test_a_payment_text_without_a_hash_keeps_its_ubl_with_and_without_skonto(world, skonto):
    invoice_id = _invoice(world, payment_terms='Kunden-Nr. 4711 bitte angeben.', **skonto)

    result = generate_invoice_documents(world.db, invoice_id)

    assert result['error'] is None and os.path.isfile(result['xml_path'])


def test_an_old_ubl_is_removed_when_a_hash_is_added_to_the_payment_terms(world):
    invoice_id = _invoice(world)
    first = generate_invoice_documents(world.db, invoice_id)
    assert os.path.isfile(first['xml_path'])

    set_column(world.db, invoice_id, 'PaymentTerms', 'Kunden-Nr. #4711#')
    second = generate_invoice_documents(world.db, invoice_id)

    assert not os.path.exists(first['xml_path']) and not row(world.db, invoice_id)[XML_PATH]
    assert second['error'] == f'{XRECHNUNG_SENTENCE}{HASH_IN_PAYMENT_TERMS_GAP}.'


def test_the_page_names_the_hash_in_the_row_and_in_the_question(world):
    invoice_id = _invoice(world, payment_terms='Kunden-Nr. #4711#')

    html = PageInvoice(world.db, {}, invoice_id)

    assert f'<tr><td>E-Rechnung:</td><td>Für die XRechnung fehlt: {HASH_IN_PAYMENT_TERMS_GAP}.</td></tr>' in html
    assert (f'const einvoiceSendQuestion = "Die XRechnung wird nicht erstellt – es fehlt: {HASH_IN_PAYMENT_TERMS_GAP}. '
            'Trotzdem versenden?";') in html


def test_several_gaps_are_named_together_in_a_fixed_order_each_in_one_sentence(world):
    invoice_id = _invoice(world, buyer_route_id='', seller_phone='', customer_id=None, seller_vat_id='', own_id=None)

    result = generate_invoice_documents(world.db, invoice_id, with_xml=True)

    assert result['error'] == (
        f'{XRECHNUNG_SENTENCE}Leitweg-ID (Käuferreferenz), Telefon der eigenen Firma, E-Mail des Kunden. '
        f'{PDF_SENTENCE}Steuernummer oder USt-IdNr. der eigenen Firma.')


def test_without_a_ubl_in_view_the_first_sentence_is_left_out_altogether(world):
    invoice_id = _invoice(world, buyer_route_id='', seller_phone='', customer_id=None, seller_vat_id='', own_id=None)

    result = generate_invoice_documents(world.db, invoice_id)                  # keine Leitweg-ID: kein UBL gewollt

    assert result['error'] == f'{PDF_SENTENCE}Steuernummer oder USt-IdNr. der eigenen Firma.'      # Telefon: nur dem UBL wichtig


def test_the_gap_lists_name_the_scope_of_each_gap(world):
    invoice_id = _invoice(world, seller_phone='', seller_vat_id='', own_id=None)
    model = build_einvoice(world.db, world.db.get_invoice_by_id(invoice_id), world.db.get_invoice_items(invoice_id))

    assert einvoice_gaps(model) == [Gap('Telefon der eigenen Firma', 'xrechnung'),
                                    Gap('Steuernummer oder USt-IdNr. der eigenen Firma', 'en16931')]


# ── Wann ein UBL gewollt ist: Leitweg-ID oder Käuferreferenz, nicht eine Bestellnummer allein ──

@pytest.mark.parametrize('route_id, reference, order_number, wanted', [
    (ROUTE_ID, None, None, True),
    ('', 'REF-1', None, True),                     # die Käuferreferenz zählt wie die Leitweg-ID
    (None, None, 'B-4711', False),                 # eine Bestellnummer allein löst kein UBL mehr aus
    ('', '', 'B-4711', False),
    (None, None, None, False),
    ('  ', '  ', 'B-4711', False),                 # Leerraum ist keine Angabe
])
def test_a_ubl_is_wanted_for_a_route_id_or_a_buyer_reference_but_not_for_an_order_number_alone(
        world, route_id, reference, order_number, wanted):
    invoice_id = _invoice(world, buyer_route_id=route_id, buyer_reference=reference, order_number=order_number)

    assert wants_xrechnung(world.db.get_invoice_by_id(invoice_id)) is wanted
    result = generate_invoice_documents(world.db, invoice_id)
    assert (result['xml_path'] is not None) is wanted
    assert result['error'] is None


def test_an_order_number_alone_gives_the_pdf_with_its_cii_and_no_complaint(world):
    invoice_id = _invoice(world, buyer_route_id=None, order_number='B-4711', bank_iban='')

    result = generate_invoice_documents(world.db, invoice_id)

    assert result['xml_path'] is None and result['error'] is None             # kein UBL gewollt, also keine Lücke dafür
    cii = _embedded_cii(result['pdf_path'])
    reference = cii.find('.//ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID', NS)
    assert reference.text == 'B-4711'                                         # die Bestellnummer steht in der CII (BT-13)


# ── Ein früher abgelegtes UBL verschwindet, wenn das Dokument nicht mehr dazu passt ──

def test_an_old_ubl_of_a_draft_is_removed_when_a_detail_goes_missing(world):
    invoice_id = _invoice(world)
    first = generate_invoice_documents(world.db, invoice_id)
    assert os.path.isfile(first['xml_path'])

    set_column(world.db, invoice_id, 'BankIBAN', '')
    second = generate_invoice_documents(world.db, invoice_id)

    assert not os.path.exists(first['xml_path']) and not row(world.db, invoice_id)[XML_PATH]
    assert second['error'] == f'{XRECHNUNG_SENTENCE}Bankverbindung (IBAN).'
    assert os.path.isfile(second['pdf_path'])


def test_an_old_ubl_of_a_draft_is_removed_when_the_trigger_is_gone(world):
    invoice_id = _invoice(world)
    first = generate_invoice_documents(world.db, invoice_id)
    assert os.path.isfile(first['xml_path'])

    set_column(world.db, invoice_id, 'BuyerRouteID', None)
    second = generate_invoice_documents(world.db, invoice_id)

    assert not os.path.exists(first['xml_path']) and not row(world.db, invoice_id)[XML_PATH]
    assert second['error'] is None and second['xml_path'] is None


def test_an_old_ubl_is_removed_when_the_invoice_is_sent_and_a_detail_is_missing(world):
    invoice_id = _invoice(world)
    first = generate_invoice_documents(world.db, invoice_id)
    set_column(world.db, invoice_id, 'BankIBAN', '')

    result = generate_invoice_documents(world.db, invoice_id, overwrite_sent=True)

    assert not os.path.exists(first['xml_path']) and result['xml_path'] is None


def test_the_ubl_of_a_sent_invoice_stays_when_only_its_missing_pdf_is_made_again(world):
    """Eine versendete Rechnung ohne PDF darf ihr PDF neu bekommen – ihr abgelegtes UBL ist das, was der Kunde bekam, und
    verschwindet nie, auch wenn die Stammdaten sich inzwischen geändert haben."""
    invoice_id = _invoice(world)
    first = generate_invoice_documents(world.db, invoice_id)
    world.db.update_invoice_status(invoice_id, 'sent')
    os.remove(first['pdf_path'])
    set_column(world.db, invoice_id, 'CustomerId', None)                      # der Kontakt (E-Mail des Kunden) fehlt

    result = generate_invoice_documents(world.db, invoice_id)

    assert os.path.isfile(result['pdf_path'])
    assert os.path.isfile(first['xml_path']) and row(world.db, invoice_id)[XML_PATH] == first['xml_path']
    assert result['error'] == f'{XRECHNUNG_SENTENCE}E-Mail des Kunden.'


def test_a_ubl_in_the_versions_folder_is_never_removed(world, tmp_path):
    invoice_id = _invoice(world)
    kept = os.path.join(userctx.user_data_dir(), 'invoices', '2026', 'Fassungen', str(invoice_id), 'alt.xml')
    os.makedirs(os.path.dirname(kept))
    with open(kept, 'w', encoding='utf-8') as f:
        f.write('<Invoice/>')
    set_column(world.db, invoice_id, 'XMLPath', kept)
    set_column(world.db, invoice_id, 'BankIBAN', '')

    generate_invoice_documents(world.db, invoice_id)

    assert os.path.isfile(kept)                                               # eine versendete Fassung bleibt immer


def test_both_candidates_of_an_old_ubl_are_removed_and_nothing_is_found_afterwards(world):
    """Der gespeicherte Pfad kann veraltet sein, daneben liegt ein UBL am Namensschema: Fiele nur eines, fände der Download das
    andere über den Rückfall am Namensschema weiter."""
    invoice_id = _invoice(world)
    first = generate_invoice_documents(world.db, invoice_id)
    stale = write_file(os.path.join(os.path.dirname(first['xml_path']), 'veraltet.xml'), b'<Invoice/>')
    set_column(world.db, invoice_id, 'XMLPath', stale)                         # der gespeicherte Pfad zeigt auf etwas Altes
    set_column(world.db, invoice_id, 'BankIBAN', '')                           # dem UBL fehlt jetzt etwas

    generate_invoice_documents(world.db, invoice_id)

    assert not os.path.exists(stale) and not os.path.exists(first['xml_path'])
    assert not row(world.db, invoice_id)[XML_PATH]
    assert stored_invoice_path(world.db, invoice_id, 'xml')[0] is None


def test_the_download_answers_404_once_the_old_ubl_is_dropped(auto_login, server_port):
    books = make_books(auto_login)
    set_column(books.db, books.invoice, 'BuyerRouteID', ROUTE_ID)              # UBL gewollt; es fehlt die IBAN
    folder = os.path.join(books.invoices, '2026')
    by_name = write_file(os.path.join(folder, f'{INVOICE_NUMBER} {CUSTOMER}.xml'), b'<Invoice>Schema</Invoice>')
    stale = write_file(os.path.join(folder, 'veraltet.xml'), b'<Invoice>veraltet</Invoice>')
    books.db.update_invoice_xml_path(books.invoice, stale)
    assert get(server_port, f'/invoice/xml_download?id={books.invoice}').status == 200     # vorher liefert er etwas

    response = post_action(server_port, f'/invoice/pdf_generate?id={books.invoice}', books.csrf)

    assert response.status == 200 and json.loads(response.text)['xml_filename'] is None
    assert not os.path.exists(stale) and not os.path.exists(by_name)
    missing = get(server_port, f'/invoice/xml_download?id={books.invoice}')
    assert missing.status == 404 and missing.text == 'Kein XRechnung (XML) vorhanden.'


# ── Der Versand: Status gesetzt, aber … ──────────────────────────────────────

def _set_status(invoice_id, status):
    code, response = handlers.handle_update_invoice_status(
        json.dumps({'invoice_id': invoice_id, 'status': status}).encode())
    return code, json.loads(response)


def test_sending_with_a_gap_sets_the_status_and_warns_with_the_gap(world):
    invoice_id = _invoice(world, seller_phone='')

    code, payload = _set_status(invoice_id, 'sent')

    assert code == 200 and payload['success'] is True
    assert payload['warning'] == f'Status gesetzt, aber: {XRECHNUNG_SENTENCE}Telefon der eigenen Firma.'
    assert world.db.get_invoice_by_id(invoice_id)[40] == 'sent'
    assert os.path.isfile(world.db.get_invoice_by_id(invoice_id)[41])         # das PDF liegt vor
    assert not world.db.get_invoice_by_id(invoice_id)[42]                     # das UBL nicht


def test_sending_a_complete_invoice_warns_of_nothing(world):
    invoice_id = _invoice(world)

    code, payload = _set_status(invoice_id, 'sent')

    assert code == 200 and 'warning' not in payload
    assert os.path.isfile(world.db.get_invoice_by_id(invoice_id)[42])


def test_the_pdf_button_answers_200_with_the_gap_as_a_warning(auto_login, server_port):
    books = make_books(auto_login)
    set_column(books.db, books.invoice, 'BuyerRouteID', ROUTE_ID)             # UBL gewollt; es fehlt die IBAN

    response = post_action(server_port, f'/invoice/pdf_generate?id={books.invoice}', books.csrf)

    body = json.loads(response.text)
    assert response.status == 200 and body['success'] is True
    assert body['warning'] == f'{XRECHNUNG_SENTENCE}Bankverbindung (IBAN).'
    assert body['xml_filename'] is None and body['filename'].endswith('.pdf')


# ── Die Maske: die Zeile auf der Rechnungsseite und die Rückfrage vor dem Versenden ──

def test_the_page_names_what_the_xrechnung_and_the_pdf_lack_each_in_its_own_sentence(world):
    invoice_id = _invoice(world, seller_phone='', seller_vat_id='', own_id=None)

    html = PageInvoice(world.db, {}, invoice_id)

    assert ('<tr><td>E-Rechnung:</td><td>Für die XRechnung fehlt: Telefon der eigenen Firma. Für die E-Rechnung im PDF '
            'fehlt: Steuernummer oder USt-IdNr. der eigenen Firma.</td></tr>') in html


def test_the_page_names_a_gap_of_the_pdf_once_even_when_a_ubl_is_in_view(world):
    invoice_id = _invoice(world, seller_vat_id='', own_id=None)                # Leitweg-ID da: ein UBL wäre gewollt

    html = PageInvoice(world.db, {}, invoice_id)

    assert ('<tr><td>E-Rechnung:</td><td>Für die E-Rechnung im PDF fehlt: Steuernummer oder USt-IdNr. der eigenen '
            'Firma.</td></tr>') in html
    assert 'Für die XRechnung fehlt' not in html
    assert html.count('Steuernummer oder USt-IdNr. der eigenen Firma') == 2   # die Zeile und die Rückfrage, je einmal


def test_the_page_names_the_pdf_alone_when_no_ubl_is_in_view(world):
    invoice_id = _invoice(world, buyer_route_id=None, seller_phone='', seller_vat_id='', own_id=None)

    html = PageInvoice(world.db, {}, invoice_id)

    assert '<td>E-Rechnung:</td><td>Für die E-Rechnung im PDF fehlt: Steuernummer oder USt-IdNr. der eigenen Firma.</td>' in html
    assert 'Für die XRechnung fehlt' not in html and 'Telefon der eigenen Firma' not in html   # das Telefon gälte nur dem UBL


@pytest.mark.parametrize('status, shown', [
    ('draft', True), ('finalized', True),                                      # hier lässt sich noch etwas ändern
    ('sent', False), ('overdue', False), ('partial_payment', False), ('paid', False), ('cancelled', False)])
def test_the_page_shows_the_row_and_asks_only_while_the_invoice_can_still_be_changed(world, status, shown):
    invoice_id = _invoice(world, seller_phone='')
    world.db.update_invoice_status(invoice_id, status)

    html = PageInvoice(world.db, {}, invoice_id)

    assert ('E-Rechnung:' in html) is shown
    assert ('Telefon der eigenen Firma' in html) is shown
    assert ('const einvoiceSendQuestion = null;' in html) is not shown


def test_the_page_has_no_row_for_a_complete_invoice_a_new_one_and_a_credit_note(world):
    complete = _invoice(world)
    assert 'E-Rechnung:' not in PageInvoice(world.db, {}, complete)
    assert 'E-Rechnung:' not in PageInvoice(world.db, {}, None)                # neues Formular
    storno = _invoice(world, '26R002', seller_phone='')
    set_column(world.db, storno, 'InvoiceKind', 'storno')
    for status in ('draft', 'sent'):                                           # gleich, welchen Status die Zeile trägt
        world.db.update_invoice_status(storno, status)
        assert 'E-Rechnung:' not in PageInvoice(world.db, {}, storno)          # keine Zeile an der Stornorechnung


def test_the_page_masks_what_the_row_and_the_question_show(world, monkeypatch):
    invoice_id = _invoice(world)
    hostile = Gap('<img src=x onerror=alert(1)>', 'xrechnung')
    monkeypatch.setattr(pages_invoice, 'einvoice_gaps', lambda model: [hostile])

    html = PageInvoice(world.db, {}, invoice_id)

    assert '<img src=x onerror=alert(1)>' not in html
    assert '&lt;img src=x onerror=alert(1)&gt;' in html                        # in der Zeile: html.escape
    assert '\\u003cimg src=x onerror=alert(1)\\u003e' in html                  # in der Rückfrage: script_json


def test_a_draft_with_a_gap_asks_before_sending_and_a_complete_one_does_not(world):
    gapped = _invoice(world, seller_phone='')
    complete = _invoice(world, '26R002')

    gapped_page = PageInvoice(world.db, {}, gapped)
    assert ('const einvoiceSendQuestion = "Die XRechnung wird nicht erstellt – es fehlt: Telefon der eigenen Firma. '
            'Trotzdem versenden?";') in gapped_page
    assert 'if (ns === \'sent\' && einvoiceSendQuestion)' in gapped_page
    assert 'const einvoiceSendQuestion = null;' in PageInvoice(world.db, {}, complete)


def test_the_question_names_the_pdf_when_only_the_embedded_invoice_lacks_something(world):
    invoice_id = _invoice(world, buyer_route_id=None, seller_vat_id='', own_id=None)

    html = PageInvoice(world.db, {}, invoice_id)

    assert ('const einvoiceSendQuestion = "Die E-Rechnung im PDF ist unvollständig – es fehlt: Steuernummer oder '
            'USt-IdNr. der eigenen Firma. Trotzdem versenden?";') in html


def test_the_question_has_both_sentences_without_naming_a_gap_twice(world):
    invoice_id = _invoice(world, seller_phone='', seller_vat_id='', own_id=None)

    html = PageInvoice(world.db, {}, invoice_id)

    assert ('const einvoiceSendQuestion = "Die XRechnung wird nicht erstellt – es fehlt: Telefon der eigenen Firma. '
            'Die E-Rechnung im PDF ist unvollständig – es fehlt: Steuernummer oder USt-IdNr. der eigenen Firma. '
            'Trotzdem versenden?";') in html


# ── Steuernummer und USt-IdNr.: je an ihrer Stelle ────────────────────────────

def _cii_registrations(root, party):
    return {e.get('schemeID'): e.text for e in root.findall(
        f'.//ram:ApplicableHeaderTradeAgreement/ram:{party}/ram:SpecifiedTaxRegistration/ram:ID', NS)}


def _ubl_registrations(xml, party):
    root = ET.fromstring(xml.encode('utf-8'))
    return {e.find('cac:TaxScheme/cbc:ID', UBL).text: e.find('cbc:CompanyID', UBL).text
            for e in root.findall(f'.//cac:{party}/cac:Party/cac:PartyTaxScheme', UBL)}


def _documents(w, invoice_id):
    inv, items = w.db.get_invoice_by_id(invoice_id), w.db.get_invoice_items(invoice_id)
    return (ET.fromstring(generate_cii(inv, items, db=w.db)),
            XRechnungGenerator().generate_xml(inv, items, db=w.db))


@pytest.mark.parametrize('stored, scheme_cii, scheme_ubl, number', [
    ('DE123456789', 'VA', 'VAT', 'DE123456789'),
    ('USt-IdNr. DE123456789', 'VA', 'VAT', 'DE123456789'),      # die Bezeichnung davor fällt weg
    ('StNr. 1338150815', 'FC', 'TAX', '1338150815'),            # „ST“ ist kein Länderkürzel
    ('BY 123 456 78901', 'FC', 'TAX', 'BY 123 456 78901'),      # ein Bundesland auch nicht
    ('DE12345678', 'FC', 'TAX', 'DE12345678'),                  # zu wenige Ziffern für eine deutsche USt-IdNr.
    ('DE 123 456 789', 'VA', 'VAT', 'DE123456789'),             # Leerzeichen fallen weg
    ('DE-123 456 789', 'VA', 'VAT', 'DE123456789'),             # Bindestriche und Punkte ebenso
    ('de.123.456.789', 'VA', 'VAT', 'DE123456789'),             # in Großschreibung
    ('12/345/67890', 'FC', 'TAX', '12/345/67890'),              # eine Steuernummer ist keine USt-IdNr. (BR-CO-09)
    ('123 456 78901', 'FC', 'TAX', '123 456 78901'),            # und bleibt, wie sie eingegeben wurde
])
def test_the_snapshot_of_the_seller_is_filed_as_vat_id_or_tax_number(world, stored, scheme_cii, scheme_ubl, number):
    invoice_id = _invoice(world, seller_vat_id=stored, own_id=None)
    cii, ubl = _documents(world, invoice_id)

    assert _cii_registrations(cii, 'SellerTradeParty') == {scheme_cii: number}
    assert _ubl_registrations(ubl, 'AccountingSupplierParty') == {scheme_ubl: number}


@pytest.mark.parametrize('stored, scheme_cii, scheme_ubl, number', [
    ('DE-123 456 789', 'VA', 'VAT', 'DE123456789'),
    ('12/345/67890', 'FC', 'TAX', '12/345/67890'),
])
def test_the_contact_stands_in_for_an_empty_snapshot_with_the_same_classification(world, stored, scheme_cii,
                                                                                   scheme_ubl, number):
    invoice_id = _invoice(world, seller_vat_id='')
    with world.db._get_connection() as connection:
        connection.execute('UPDATE CompanyDetails SET TaxID = ? WHERE ContactID = ?', (stored, world.own))
    cii, ubl = _documents(world, invoice_id)

    assert _cii_registrations(cii, 'SellerTradeParty') == {scheme_cii: number}
    assert _ubl_registrations(ubl, 'AccountingSupplierParty') == {scheme_ubl: number}


def test_a_tax_number_in_the_snapshot_is_not_replaced_by_the_vat_id_of_the_contact(world):
    invoice_id = _invoice(world, seller_vat_id='12/345/67890')               # der Kontakt trägt DE123456789
    cii, ubl = _documents(world, invoice_id)

    assert _cii_registrations(cii, 'SellerTradeParty') == {'FC': '12/345/67890'}
    assert _ubl_registrations(ubl, 'AccountingSupplierParty') == {'TAX': '12/345/67890'}


def test_the_buyer_has_a_field_for_the_vat_id_only(world):
    with_vat = _invoice(world, buyer_vat_id='de-101.010 101')
    with_tax_number = _invoice(world, '26R002', buyer_vat_id='12/345/67890')

    cii, ubl = _documents(world, with_vat)
    assert _cii_registrations(cii, 'BuyerTradeParty') == {'VA': 'DE101010101'}
    assert _ubl_registrations(ubl, 'AccountingCustomerParty') == {'VAT': 'DE101010101'}

    cii, ubl = _documents(world, with_tax_number)
    assert _cii_registrations(cii, 'BuyerTradeParty') == {}                   # EN 16931 kennt keine Steuernummer des Käufers
    assert _ubl_registrations(ubl, 'AccountingCustomerParty') == {}


# ── Ohne USt-IdNr.: die Steuernummer ist zusätzlich die Verkäuferkennung (BT-29, BR-CO-26) ──

def _children(element):
    return [child.tag.split('}')[1] for child in element]


def _cii_seller(cii):
    return cii.find('.//ram:ApplicableHeaderTradeAgreement/ram:SellerTradeParty', NS)


def _ubl_party(xml, party):
    return ET.fromstring(xml.encode('utf-8')).find(f'.//cac:{party}/cac:Party', UBL)


def _ubl_identifiers(xml, party):
    return [e.text for e in _ubl_party(xml, party).findall('cac:PartyIdentification/cbc:ID', UBL)]


def test_a_tax_number_without_a_vat_id_is_also_the_identifier_of_the_seller(world):
    invoice_id = _invoice(world, seller_vat_id='12/345/67890', own_id=None)
    cii, ubl = _documents(world, invoice_id)

    seller = _cii_seller(cii)
    assert seller.find('ram:ID', NS).text == '12/345/67890'                    # BT-29 …
    assert _cii_registrations(cii, 'SellerTradeParty') == {'FC': '12/345/67890'}   # … neben BT-32
    assert _children(seller)[:2] == ['ID', 'Name']                             # im Schema der Partei vor dem Namen
    assert _ubl_identifiers(ubl, 'AccountingSupplierParty') == ['12/345/67890']
    assert _ubl_registrations(ubl, 'AccountingSupplierParty') == {'TAX': '12/345/67890'}
    party = _children(_ubl_party(ubl, 'AccountingSupplierParty'))
    assert party.index('EndpointID') < party.index('PartyIdentification') < party.index('PartyName')   # Schema-Reihenfolge


@pytest.mark.parametrize('stored, number', [
    ('  12 / 345 / 67890 ', '12 / 345 / 67890'),
    ('St.-Nr. 12 / 345 / 67890', '12 / 345 / 67890'),           # die Bezeichnung davor fällt weg, der Rest bleibt
    ('Steuernummer: 12/345/67890', '12/345/67890'),
])
def test_the_identifier_keeps_the_tax_number_as_it_was_entered(world, stored, number):
    invoice_id = _invoice(world, seller_vat_id=stored, own_id=None)
    cii, ubl = _documents(world, invoice_id)

    assert _cii_seller(cii).find('ram:ID', NS).text == number
    assert _ubl_identifiers(ubl, 'AccountingSupplierParty') == [number]


@pytest.mark.parametrize('stored', ['DE123456789', 'DE-123 456 789', 'ATU12345678', 'USt-IdNr. DE123456789',
                                    'CHE-123.456.789 MWST'])
def test_a_seller_with_a_vat_id_has_no_identifier(world, stored):
    invoice_id = _invoice(world, seller_vat_id=stored, own_id=None)
    cii, ubl = _documents(world, invoice_id)

    assert _cii_seller(cii).find('ram:ID', NS) is None and _children(_cii_seller(cii))[0] == 'Name'
    assert _ubl_identifiers(ubl, 'AccountingSupplierParty') == []
    assert 'PartyIdentification' not in ubl


def test_the_contact_with_a_tax_number_gives_the_identifier_too(world):
    invoice_id = _invoice(world, seller_vat_id='')
    with world.db._get_connection() as connection:
        connection.execute('UPDATE CompanyDetails SET TaxID = ? WHERE ContactID = ?', ('12/345/67890', world.own))
    cii, ubl = _documents(world, invoice_id)

    assert _cii_seller(cii).find('ram:ID', NS).text == '12/345/67890'
    assert _ubl_identifiers(ubl, 'AccountingSupplierParty') == ['12/345/67890']


def test_a_seller_with_neither_has_no_identifier_and_the_gap_stays(world):
    invoice_id = _invoice(world, seller_vat_id='', own_id=None)
    cii, ubl = _documents(world, invoice_id)

    assert _cii_seller(cii).find('ram:ID', NS) is None and 'PartyIdentification' not in ubl
    model = build_einvoice(world.db, world.db.get_invoice_by_id(invoice_id), world.db.get_invoice_items(invoice_id))
    assert [gap.text for gap in einvoice_gaps(model)] == ['Steuernummer oder USt-IdNr. der eigenen Firma']


def test_the_buyer_never_gets_an_identifier(world):
    invoice_id = _invoice(world, seller_vat_id='12/345/67890', own_id=None, buyer_vat_id='12/345/67890')
    cii, ubl = _documents(world, invoice_id)

    assert cii.find('.//ram:ApplicableHeaderTradeAgreement/ram:BuyerTradeParty/ram:ID', NS) is None
    assert _ubl_identifiers(ubl, 'AccountingCustomerParty') == []


def test_a_seller_with_only_a_tax_number_gets_a_ubl_when_nothing_else_is_missing(world):
    invoice_id = _invoice(world, seller_vat_id='12/345/67890', own_id=None)

    result = generate_invoice_documents(world.db, invoice_id)

    assert result['error'] is None and os.path.isfile(result['xml_path'])
    with open(result['xml_path'], encoding='utf-8') as f:
        assert '<cbc:ID>12/345/67890</cbc:ID>' in f.read()


# ── Die Meldung: ein Fehler beim Erzeugen ist etwas anderes als fehlende Angaben ──

def test_a_message_about_missing_details_is_marked_incomplete_and_nothing_else_is(world, monkeypatch):
    import export.invoice_files as files
    complete = _invoice(world)
    gapped = _invoice(world, '26R002', seller_phone='')

    assert generate_invoice_documents(world.db, complete)['incomplete'] is False
    assert generate_invoice_documents(world.db, complete)['error'] is None
    result = generate_invoice_documents(world.db, gapped)
    assert result['incomplete'] is True and result['error'] == f'{XRECHNUNG_SENTENCE}Telefon der eigenen Firma.'

    monkeypatch.setattr(files, '_write_xrechnung', lambda *a, **k: None)      # das Schreiben scheitert wirklich
    broken = generate_invoice_documents(world.db, complete)
    assert broken['error'] == 'XRechnung (XML) konnte nicht erstellt werden.' and broken['incomplete'] is False
    missing = generate_invoice_documents(world.db, 999)
    assert missing['error'] == 'Rechnung nicht gefunden.' and missing['incomplete'] is False


@pytest.mark.parametrize('result, retry', [
    ({'error': 'XRechnung (XML) nicht erstellt – es fehlt: Telefon der eigenen Firma.', 'incomplete': True}, ''),
    ({'error': 'Die E-Rechnung im PDF ist unvollständig – es fehlt: Skonto.', 'incomplete': True}, ''),
    ({'error': 'PDF konnte nicht erstellt werden.', 'incomplete': False}, ' Bitte „PDF + E-Rechnung“ ausführen.'),
    ({'error': 'XRechnung (XML) konnte nicht erstellt werden.'}, ' Bitte „PDF + E-Rechnung“ ausführen.'),
])
def test_the_retry_hint_is_left_out_when_only_details_are_missing(monkeypatch, result, retry):
    import export.invoice_files as files
    monkeypatch.setattr(files, 'generate_invoice_documents', lambda *a, **k: result)

    assert handlers._generate_storno_documents(None, 1) == f"Die Stornorechnung ist gespeichert, aber: {result['error']}{retry}"
    assert handlers._generate_corrected_documents(None, 1) == f"Die Berichtigung ist gespeichert, aber: {result['error']}{retry}"


def test_a_correction_that_lacks_details_names_them_without_the_retry_hint(storno_world):
    w = storno_world
    invoice_id = sent(w, 'T-100')                                              # versendet mit PDF und UBL (die Welt ist vollständig)
    with w.db._get_connection() as connection:
        connection.execute("UPDATE Contacts SET Email = '' WHERE ID = ?", (w.cust,))

    answer = save_open(w, invoice_id, paymentTerms='Zahlbar sofort')

    assert answer['success'] is True and answer['documentVersion'] == 2
    assert answer['warning'] == f'Die Berichtigung ist gespeichert, aber: {XRECHNUNG_SENTENCE}E-Mail des Kunden.'


def test_a_cancellation_that_lacks_details_names_them_without_the_retry_hint(storno_world, monkeypatch):
    w = storno_world
    invoice_id = sent(w, 'T-100')
    with w.db._get_connection() as connection:
        connection.execute("UPDATE Contacts SET Email = '' WHERE ID = ?", (w.cust,))
    at(monkeypatch, '2026-07-02')

    status, body = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': '2026-07-02'}).encode())

    payload = json.loads(body)
    assert status == 200 and payload['success'] is True
    assert payload['warning'] == f'Die Stornorechnung ist gespeichert, aber: {XRECHNUNG_SENTENCE}E-Mail des Kunden.'
