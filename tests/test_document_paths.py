# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Gespeicherte Dokumentpfade (``Invoices.PDFPath``/``XMLPath``), 2.0.7.

Die Datenbank lässt sich per SQL-Konsole oder zurückgespielter Sicherung verändern. Ein dort
gespeicherter Pfad gilt deshalb nur im Dokumentordner des Benutzers (``<Datenordner>/invoices``, bei
Angeboten ``quotes``), aufgelöst mit ``realpath``: Download, E-Mail-Anhang und das Löschen eines
Vorgängers (``drop_stale_file``) gehen alle über ``stored_invoice_path``/``path_in_documents``.

* ein Pfad außerhalb (absolut, ``..``, Symlink, Ordner der anderen Belegart, Namensvetter-Ordner)
  gilt als nicht vorhanden – Fallback über das Namensschema, sonst 404 ohne den Pfad in der Antwort;
* ``drop_stale_file`` löscht nie außerhalb des Dokumentordners und nie die Datei eines anderen
  Belegs (``Database.document_path_in_use``);
* ein Symlink am Namensschema, der aus dem Dokumentordner hinausführt, zählt nicht als Dokument.

Alle Namen, Pfade und Inhalte sind erfundene Testwerte.
"""
import json
import os
import re
from types import SimpleNamespace

import pytest

import auth
import userctx
from export.invoice_files import (drop_stale_file, generate_invoice_documents, path_in_documents,
                                  stored_invoice_document, stored_invoice_path)
from export.pdf_invoice import generate_invoice_pdf
from server.pages_invoice import PageInvoice
from server.pages_quote import PageQuote
from tests.document_helpers import (CUSTOMER, INVOICE_NUMBER, PDF, PDF_PATH, QUOTE_NUMBER, XML_PATH,
                                    YEAR, add_contacts, add_document, fake_smtp, make_books, post_json,
                                    read_file, row, set_column, write_file)
from tests.dom_helpers import Dom
from tests.http_client import get

SECRET = b'%PDF-1.4 vertraulich - gehoert nicht in den Dokumentordner'
NOT_PRESENT = {'invoice': 'Kein PDF vorhanden.',
               'quote': 'Kein PDF vorhanden – bitte zuerst erzeugen.'}
STEM = {'invoice': f'{INVOICE_NUMBER} {CUSTOMER}', 'quote': f'{QUOTE_NUMBER} {CUSTOMER}'}


def symlink(target, link):
    """Symlink ``link`` -> ``target`` anlegen (ein vorhandener bleibt); ohne Symlink-Unterstützung wird übersprungen."""
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if not os.path.lexists(link):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):                 # z. B. Windows ohne Berechtigung
            pytest.skip('Symlinks sind auf diesem System nicht verfügbar')
    return link


# Wege, auf denen ein gespeicherter Pfad aus dem Dokumentordner hinausführt. Jeder Weg zeigt auf
# eine vorhandene Datei – nur deshalb wäre sie früher ausgeliefert oder gelöscht worden.
OUTSIDE = ['fremde-Datei', 'auth.db', 'Rückwärts-Pfad', 'Namensvetter-Ordner', 'Ordner-der-anderen-Belegart',
           'Ablage-eines-anderen-Benutzers', 'Symlink-auf-Datei', 'Symlink-auf-Ordner']


def outside_path(world, kind, label):
    """Pfad zu einer vorhandenen Datei, der für einen Beleg der Art ``kind`` außerhalb liegt."""
    own = world.invoices if kind == 'invoice' else world.quotes
    other = world.quotes if kind == 'invoice' else world.invoices
    if label == 'fremde-Datei':
        return world.foreign
    if label == 'auth.db':                                   # die echte Anmeldedatenbank, falls es sie gibt
        if not os.path.exists(auth.AUTH_DB):
            write_file(auth.AUTH_DB, b'SQLite format 3\x00 Zugangsdaten')
        return auth.AUTH_DB
    if label == 'Rückwärts-Pfad':
        write_file(os.path.join(os.path.dirname(own), 'buch-kopie.db'), SECRET)
        os.makedirs(os.path.join(own, '2026'), exist_ok=True)            # ohne den Ordner löst das Betriebssystem `..` nicht auf
        return os.path.join(own, '2026', '..', '..', 'buch-kopie.db')
    if label == 'Namensvetter-Ordner':                       # beginnt wie der Dokumentordner, ist aber ein anderer
        return write_file(own + '_alt/2026/x.pdf', SECRET)
    if label == 'Ordner-der-anderen-Belegart':
        return write_file(os.path.join(other, '2026', 'x.pdf'), SECRET)
    if label == 'Ablage-eines-anderen-Benutzers':            # users/bob/invoices bzw. quotes
        return write_file(os.path.join(userctx.DATA_ROOT, 'users', 'bob', os.path.basename(own), '2026', 'x.pdf'),
                          SECRET)
    if label == 'Symlink-auf-Datei':
        return symlink(world.foreign, os.path.join(own, '2026', 'link.pdf'))
    if label == 'Symlink-auf-Ordner':
        folder = symlink(os.path.dirname(world.foreign), os.path.join(own, '2027'))
        return os.path.join(folder, os.path.basename(world.foreign))
    raise AssertionError(label)


@pytest.fixture
def world(tmp_db, tmp_path):
    """Datenbank ohne Server: der Dokumentordner liegt unter ``userctx.DATA_ROOT`` (tmp_path)."""
    own, customer = add_contacts(tmp_db)
    return SimpleNamespace(
        db=tmp_db, own=own, customer=customer,
        invoice=add_document(tmp_db, INVOICE_NUMBER, 'invoice', own, customer),
        quote=add_document(tmp_db, QUOTE_NUMBER, 'quote', own, customer),
        invoices=os.path.join(userctx.DATA_ROOT, 'invoices'),
        quotes=os.path.join(userctx.DATA_ROOT, 'quotes'),
        foreign=write_file(str(tmp_path / 'fremd' / 'geheim.pdf'), SECRET))


def doc_of(world, kind):
    return world.invoice if kind == 'invoice' else world.quote


# ── path_in_documents / stored_invoice_path ──────────────────────────────────
@pytest.mark.parametrize('kind', ['invoice', 'quote'])
@pytest.mark.parametrize('label', OUTSIDE)
def test_path_outside_the_documents_folder_is_not_accepted(world, kind, label):
    path = outside_path(world, kind, label)
    assert os.path.exists(path)
    assert path_in_documents(path, world.db.get_invoice_by_id(doc_of(world, kind))) is False


@pytest.mark.parametrize('kind', ['invoice', 'quote'])
@pytest.mark.parametrize('label', OUTSIDE)
def test_stored_path_outside_counts_as_not_present_and_is_only_logged(world, kind, label, capsys):
    doc = doc_of(world, kind)
    path = outside_path(world, kind, label)
    set_column(world.db, doc, 'PDFPath', path)
    capsys.readouterr()
    found, error = stored_invoice_path(world.db, doc, 'pdf')
    assert found is None and error == NOT_PRESENT[kind]
    assert path in capsys.readouterr().out                   # der Pfad steht im Server-Log …
    assert path not in error                                 # … nie in der Antwort
    data, name, error = stored_invoice_document(world.db, doc, 'pdf')
    assert (data, name) == (None, None) and error == NOT_PRESENT[kind]


@pytest.mark.parametrize('label', OUTSIDE)
def test_stored_xml_path_outside_counts_as_not_present(world, label):
    path = outside_path(world, 'invoice', label)
    set_column(world.db, world.invoice, 'XMLPath', path)
    assert stored_invoice_path(world.db, world.invoice, 'xml') == (None, 'Kein XRechnung (XML) vorhanden.')


@pytest.mark.parametrize('kind', ['invoice', 'quote'])
def test_stored_path_outside_falls_back_to_the_file_at_the_naming_scheme(world, kind):
    doc = doc_of(world, kind)
    set_column(world.db, doc, 'PDFPath', world.foreign)
    folder = os.path.join(world.invoices if kind == 'invoice' else world.quotes,
                          '2026' if kind == 'invoice' else YEAR)
    stored = write_file(os.path.join(folder, STEM[kind] + '.pdf'), PDF)
    assert stored_invoice_path(world.db, doc, 'pdf') == (stored, None)
    assert stored_invoice_document(world.db, doc, 'pdf')[0] == PDF            # nicht die fremde Datei


def test_paths_inside_the_documents_folder_are_accepted(world):
    invoice = world.db.get_invoice_by_id(world.invoice)
    quote = world.db.get_invoice_by_id(world.quote)
    plain = write_file(os.path.join(world.invoices, '2026', 'a.pdf'))
    assert path_in_documents(plain, invoice) is True
    assert path_in_documents(plain) is True                                    # ohne Beleg gilt der Rechnungsordner
    assert path_in_documents(os.path.join(world.invoices, '2026', '..', '2025', 'b.pdf'), invoice) is True
    assert path_in_documents(write_file(os.path.join(world.quotes, YEAR, 'a.pdf')), quote) is True
    # ein Symlink im Ordner, der auf eine Datei im Ordner zeigt, bleibt im Ordner
    assert path_in_documents(symlink(plain, os.path.join(world.invoices, '2026', 'alias.pdf')), invoice) is True


@pytest.mark.parametrize('value', [None, '', 0, 5, b'/tmp/x.pdf', ['a.pdf'], 'x\x00y.pdf', 3.5],
                         ids=repr)
def test_something_that_is_not_a_path_counts_as_outside(world, value):
    # Eine Zahl läse `open` als Dateinummer, ein NUL-Byte lässt die Auflösung scheitern
    assert path_in_documents(value, world.db.get_invoice_by_id(world.invoice)) is False


def test_a_number_as_stored_path_is_never_opened(world):
    # Ein Altbestand ohne Textspalte könnte eine Zahl liefern; `os.path.isfile(1)`/`open(1)` meinten die
    # Standardausgabe. Die Zeile wird hier von Hand mit der Zahl 1 als Pfad gebaut.
    stored = list(world.db.get_invoice_by_id(world.invoice))
    stored[PDF_PATH] = 1
    assert stored_invoice_path(world.db, world.invoice, 'pdf', invoice=tuple(stored)) == (None, NOT_PRESENT['invoice'])


# ── Listen und Formulare bieten nur an, was auch heruntergeladen werden kann ──
def _offers(world):
    """(Rechnungsliste, Rechnungsformular, Angebotsliste, Angebotsformular) als ``(pdf, xml)``-Angebote."""
    invoice_list = re.findall(r'downloadInvoiceDocs\((\d+), (true|false), (true|false)\)', PageInvoice(world.db, {}))
    form = Dom(PageInvoice(world.db, {}, world.invoice))
    quote_list = re.findall(r'handleQuotePDF\((\d+), (true|false)\)', PageQuote(world.db, {}))
    quote_form = Dom(PageQuote(world.db, {}, world.quote))
    return (
        [(i, p, x) for i, p, x in invoice_list],
        (form.one('input', id='pdf_exists')['value'], form.one('input', id='xml_exists')['value']),
        quote_list,
        quote_form.one('input', id='pdf_exists')['value'])


@pytest.mark.parametrize('label', OUTSIDE)
def test_lists_and_forms_do_not_offer_a_document_outside_the_folder(world, label):
    invoice_path = outside_path(world, 'invoice', label)
    for column in ('PDFPath', 'XMLPath'):
        set_column(world.db, world.invoice, column, invoice_path)
    set_column(world.db, world.quote, 'PDFPath', outside_path(world, 'quote', label))
    invoice_list, invoice_form, quote_list, quote_form = _offers(world)
    assert invoice_list == [(str(world.invoice), 'false', 'false')]
    assert invoice_form == ('false', 'false')
    assert quote_list == [(str(world.quote), 'false')] and quote_form == 'false'


def test_lists_and_forms_offer_a_document_inside_the_folder(world):
    set_column(world.db, world.invoice, 'PDFPath',
               write_file(os.path.join(world.invoices, '2026', f'{STEM["invoice"]}.pdf'), PDF))
    set_column(world.db, world.invoice, 'XMLPath',
               write_file(os.path.join(world.invoices, '2026', f'{STEM["invoice"]}.xml'), b'<Invoice/>'))
    set_column(world.db, world.quote, 'PDFPath',
               write_file(os.path.join(world.quotes, YEAR, f'{STEM["quote"]}.pdf'), PDF))
    invoice_list, invoice_form, quote_list, quote_form = _offers(world)
    assert invoice_list == [(str(world.invoice), 'true', 'true')] and invoice_form == ('true', 'true')
    assert quote_list == [(str(world.quote), 'true')] and quote_form == 'true'


def test_lists_and_forms_offer_the_file_at_the_naming_scheme_when_the_stored_path_is_outside(world):
    set_column(world.db, world.invoice, 'PDFPath', world.foreign)
    write_file(os.path.join(world.invoices, '2026', f'{STEM["invoice"]}.pdf'), PDF)
    invoice_list, invoice_form, _, _ = _offers(world)
    assert invoice_list == [(str(world.invoice), 'true', 'false')] and invoice_form == ('true', 'false')


# ── Download über den Server ─────────────────────────────────────────────────
@pytest.fixture
def books(auto_login, tmp_path):
    books = make_books(auto_login)
    books.foreign = write_file(str(tmp_path / 'fremd' / 'geheim.pdf'), SECRET)
    return books


DOWNLOAD = {'invoice': '/invoice/pdf_download?id={}', 'quote': '/quote/pdf_download?id={}'}


@pytest.mark.parametrize('kind', ['invoice', 'quote'])
@pytest.mark.parametrize('label', OUTSIDE)
def test_download_with_a_path_outside_is_404_and_does_not_name_the_path(server_port, books, kind, label, capsys):
    doc = books.invoice if kind == 'invoice' else books.quote
    path = outside_path(books, kind, label)
    set_column(books.db, doc, 'PDFPath', path)
    capsys.readouterr()
    resp = get(server_port, DOWNLOAD[kind].format(doc))
    assert resp.status == 404
    assert resp.text == NOT_PRESENT[kind]
    assert SECRET not in resp.body and b'SQLite format' not in resp.body
    assert path not in resp.text and os.path.basename(path) not in resp.text
    assert path in capsys.readouterr().out                    # nur das Server-Log nennt ihn


def test_xml_download_with_a_path_outside_is_404(server_port, books):
    set_column(books.db, books.invoice, 'XMLPath', books.foreign)
    resp = get(server_port, f'/invoice/xml_download?id={books.invoice}')
    assert resp.status == 404 and resp.text == 'Kein XRechnung (XML) vorhanden.'
    assert SECRET not in resp.body and books.foreign not in resp.text


def test_download_with_a_path_outside_serves_the_stored_file_at_the_naming_scheme(server_port, books):
    set_column(books.db, books.invoice, 'PDFPath', books.foreign)
    write_file(os.path.join(books.invoices, '2026', f'{STEM["invoice"]}.pdf'), PDF)
    resp = get(server_port, DOWNLOAD['invoice'].format(books.invoice))
    assert resp.status == 200 and resp.body == PDF


# ── E-Mail-Anhang ────────────────────────────────────────────────────────────
MAIL = {'recipient_email': 'kunde@example.com', 'recipient_name': 'Erika Empfaenger'}


@pytest.mark.parametrize('label', OUTSIDE)
def test_email_attachment_never_comes_from_outside_the_documents_folder(server_port, books, monkeypatch, label):
    smtp = fake_smtp(monkeypatch)
    set_column(books.db, books.invoice, 'PDFPath', outside_path(books, 'invoice', label))
    resp = post_json(server_port, '/invoice/send-email', {'invoice_id': books.invoice, **MAIL}, books.csrf)
    assert resp.status == 200
    assert json.loads(resp.text) == {'success': False, 'error': 'Kein PDF vorhanden – bitte zuerst erzeugen.'}
    assert smtp.connections == [] and smtp.sent == []         # es wurde nichts verschickt


def test_email_attachment_is_the_stored_file_at_the_naming_scheme_when_the_path_is_outside(
        server_port, books, monkeypatch):
    smtp = fake_smtp(monkeypatch)
    set_column(books.db, books.invoice, 'PDFPath', books.foreign)
    write_file(os.path.join(books.invoices, '2026', f'{STEM["invoice"]}.pdf'), PDF)
    resp = post_json(server_port, '/invoice/send-email', {'invoice_id': books.invoice, **MAIL}, books.csrf)
    assert resp.status == 200 and json.loads(resp.text) == {'success': True}
    assert smtp.attachments() == [(f'{STEM["invoice"]}.pdf', PDF)]


# ── drop_stale_file ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('label', OUTSIDE)
def test_drop_stale_file_never_removes_a_file_outside_the_documents_folder(world, label):
    old = outside_path(world, 'invoice', label)
    before = read_file(old)
    new = os.path.join(world.invoices, '2026', 'neu.pdf')
    drop_stale_file(old, new)
    drop_stale_file(old, new, world.db, world.invoice)         # auch mit Datenbank
    assert os.path.lexists(old) and read_file(old) == before
    assert read_file(world.foreign) == SECRET                  # und kein Symlink-Ziel


def test_drop_stale_file_removes_the_predecessor_inside_the_folder(world):
    old = write_file(os.path.join(world.invoices, '2026', 'alt.pdf'))
    new = write_file(os.path.join(world.invoices, '2026', 'neu.pdf'))
    drop_stale_file(old, new)
    assert not os.path.exists(old) and os.path.exists(new)


@pytest.mark.parametrize('old', [None, '', 7, 'gibt-es-nicht.pdf', 'x\x00y.pdf'], ids=repr)
def test_drop_stale_file_ignores_what_is_not_a_file(world, old):
    new = write_file(os.path.join(world.invoices, '2026', 'neu.pdf'))
    drop_stale_file(old, new)                                   # kein Fehler
    drop_stale_file(old, new, world.db, world.invoice)
    assert os.path.exists(new)


def test_drop_stale_file_keeps_a_file_whose_name_did_not_change(world):
    path = write_file(os.path.join(world.invoices, '2026', 'gleich.pdf'))
    drop_stale_file(path, os.path.join(world.invoices, '2026', '..', '2026', 'gleich.pdf'))
    assert os.path.exists(path)


@pytest.mark.parametrize('label', OUTSIDE)
def test_regenerating_an_invoice_with_a_manipulated_pdf_path_keeps_the_foreign_file(world, label):
    old = outside_path(world, 'invoice', label)
    before = read_file(old)
    set_column(world.db, world.invoice, 'PDFPath', old)
    pdf, path = generate_invoice_pdf(world.db, world.invoice)
    assert pdf.startswith(b'%PDF') and path == os.path.join(world.invoices, '2026', f'{STEM["invoice"]}.pdf')
    assert os.path.isfile(path) and row(world.db, world.invoice)[PDF_PATH] == path
    assert os.path.lexists(old) and read_file(old) == before
    assert read_file(world.foreign) == SECRET


def test_regenerating_the_xml_with_a_manipulated_xml_path_keeps_the_foreign_file(world):
    set_column(world.db, world.invoice, 'XMLPath', world.foreign)
    result = generate_invoice_documents(world.db, world.invoice, with_xml=True)
    assert result['error'] is None and os.path.isfile(result['xml_path'])
    assert row(world.db, world.invoice)[XML_PATH] == result['xml_path']
    assert read_file(world.foreign) == SECRET


def test_a_renamed_invoice_still_loses_its_own_old_file(world):
    _, first = generate_invoice_pdf(world.db, world.invoice)
    set_column(world.db, world.invoice, 'BuyerCompany', 'Bau AG')
    _, second = generate_invoice_pdf(world.db, world.invoice)
    assert os.path.basename(second) == f'{INVOICE_NUMBER} Bau AG.pdf'
    assert os.path.isfile(second) and not os.path.exists(first)


# ── Datei eines anderen Belegs ───────────────────────────────────────────────
@pytest.fixture
def second_invoice(world):
    """Rechnung B neben Rechnung A (``world.invoice``), beide mit eigener abgelegter PDF."""
    other = add_document(world.db, '26R002', 'invoice', world.own, world.customer)
    _, a_path = generate_invoice_pdf(world.db, world.invoice)
    _, b_path = generate_invoice_pdf(world.db, other)
    return SimpleNamespace(id=other, a=a_path, b=b_path)


def test_regenerating_an_invoice_whose_path_points_to_another_invoices_pdf_keeps_that_pdf(world, second_invoice):
    a_bytes = read_file(second_invoice.a)
    set_column(world.db, second_invoice.id, 'PDFPath', second_invoice.a)    # per SQL auf die Datei von A umgebogen
    _, path = generate_invoice_pdf(world.db, second_invoice.id)
    assert path == second_invoice.b                                          # B bekommt wieder seine eigene Datei
    assert os.path.isfile(second_invoice.a) and read_file(second_invoice.a) == a_bytes
    assert row(world.db, world.invoice)[PDF_PATH] == second_invoice.a        # A kennt seine Datei weiter
    assert row(world.db, second_invoice.id)[PDF_PATH] == second_invoice.b


def test_regenerating_the_xml_of_an_invoice_whose_path_points_to_another_invoices_xml_keeps_that_xml(
        world, second_invoice):
    a_xml = generate_invoice_documents(world.db, world.invoice, with_xml=True)['xml_path']
    b_xml = generate_invoice_documents(world.db, second_invoice.id, with_xml=True)['xml_path']
    a_bytes = read_file(a_xml)
    set_column(world.db, second_invoice.id, 'XMLPath', a_xml)
    assert generate_invoice_documents(world.db, second_invoice.id, with_xml=True)['xml_path'] == b_xml
    assert os.path.isfile(a_xml) and read_file(a_xml) == a_bytes


def test_drop_stale_file_with_a_database_keeps_a_file_another_document_uses(world, second_invoice):
    new = os.path.join(world.invoices, '2026', 'neu.pdf')
    drop_stale_file(second_invoice.a, new, world.db, second_invoice.id)     # A gehört A, nicht B
    assert os.path.isfile(second_invoice.a)
    drop_stale_file(second_invoice.a, new, world.db, world.invoice)         # A darf seine Datei abräumen
    assert not os.path.exists(second_invoice.a)


class TestDocumentPathInUse:
    """``Database.document_path_in_use(path, exclude_id)``: verweist ein ANDERER Beleg auf die Datei?"""

    def test_exact_path_of_another_document(self, world, second_invoice):
        db = world.db
        assert db.document_path_in_use(second_invoice.a, second_invoice.id) is True
        assert db.document_path_in_use(second_invoice.a, world.invoice) is False       # der eigene zählt nicht
        assert db.document_path_in_use(second_invoice.a, None) is True                 # ohne Ausnahme zählt jeder
        assert db.document_path_in_use(os.path.join(world.invoices, 'nirgends.pdf'), None) is False

    def test_xml_path_counts_too(self, world, second_invoice):
        xml = generate_invoice_documents(world.db, world.invoice, with_xml=True)['xml_path']
        assert world.db.document_path_in_use(xml, second_invoice.id) is True
        assert world.db.document_path_in_use(xml, world.invoice) is False

    def test_the_same_file_in_another_notation_is_recognized(self, world, second_invoice, monkeypatch):
        db, folder = world.db, os.path.dirname(second_invoice.a)
        name = os.path.basename(second_invoice.a)
        monkeypatch.chdir(folder)
        set_column(db, world.invoice, 'PDFPath', name)                     # relativ gespeichert …
        assert db.document_path_in_use(second_invoice.a, second_invoice.id) is True      # … absolut gefragt
        set_column(db, world.invoice, 'PDFPath', second_invoice.a)         # absolut gespeichert …
        assert db.document_path_in_use(name, second_invoice.id) is True                  # … relativ gefragt
        dotted = os.path.join(folder, '..', os.path.basename(folder), name)
        assert db.document_path_in_use(dotted, second_invoice.id) is True                # mit `..`
        alias = symlink(second_invoice.a, os.path.join(folder, 'alias.pdf'))
        assert db.document_path_in_use(alias, second_invoice.id) is True                 # über einen Symlink

    @pytest.mark.parametrize('path', [None, '', 5, b'x', 'x\x00y'], ids=repr)
    def test_something_that_is_not_a_path_is_not_in_use(self, world, second_invoice, path):
        assert world.db.document_path_in_use(path, second_invoice.id) is False


# ── Symlink am Namensschema ──────────────────────────────────────────────────
def test_a_symlink_at_the_naming_scheme_leading_out_of_the_folder_is_not_a_document(world):
    symlink(world.foreign, os.path.join(world.invoices, '2026', f'{STEM["invoice"]}.pdf'))
    assert stored_invoice_path(world.db, world.invoice, 'pdf') == (None, NOT_PRESENT['invoice'])
    assert stored_invoice_document(world.db, world.invoice, 'pdf')[0] is None


def test_a_symlink_at_the_naming_scheme_of_a_quote_is_not_a_document(world):
    symlink(world.foreign, os.path.join(world.quotes, YEAR, f'{STEM["quote"]}.pdf'))
    assert stored_invoice_path(world.db, world.quote, 'pdf') == (None, NOT_PRESENT['quote'])


def test_a_year_folder_that_is_a_symlink_out_of_the_quote_folder_is_skipped(world):
    # Das neueste Jahr ist ein Link nach draußen, ein älteres Jahr ist echt: gefunden wird das echte
    symlink(os.path.dirname(world.foreign), os.path.join(world.quotes, '2030'))
    write_file(os.path.join(os.path.dirname(world.foreign), f'{STEM["quote"]}.pdf'), SECRET)
    real = write_file(os.path.join(world.quotes, '2025', f'{STEM["quote"]}.pdf'), PDF)
    assert stored_invoice_path(world.db, world.quote, 'pdf') == (real, None)


def test_symlink_at_the_naming_scheme_is_404_over_http(server_port, books):
    symlink(books.foreign, os.path.join(books.invoices, '2026', f'{STEM["invoice"]}.pdf'))
    symlink(books.foreign, os.path.join(books.quotes, YEAR, f'{STEM["quote"]}.pdf'))
    invoice = get(server_port, DOWNLOAD['invoice'].format(books.invoice))
    quote = get(server_port, DOWNLOAD['quote'].format(books.quote))
    assert (invoice.status, invoice.text) == (404, NOT_PRESENT['invoice'])
    assert (quote.status, quote.text) == (404, NOT_PRESENT['quote'])
    assert SECRET not in invoice.body + quote.body
