# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Kopfblock oben rechts im PDF folgt dem Eingabeformular (seit 2.5.2).

Das Formular nennt die beiden Daten zusammen und davor keine bloße „Datum“-Zeile: Rechnungsdatum, Leistungsdatum,
Rechnungs-Nr., Kunden-Nr. Das PDF schrieb „Datum“, dann die Nummern und das Leistungsdatum am Ende. Beim Angebot stand
„gültig bis“ hinter der Kundennummer; in Maske und PDF steht es jetzt direkt unter dem Angebotsdatum.

Gemessen wird am erzeugten PDF: die Zeilen von oben nach unten, und dass keine Bezeichnung in ihren Wert läuft
(„Rechnungsdatum:“ ist die längste und breiter als die frühere Spalte).
"""
import io
import re
from types import SimpleNamespace

import pdfplumber
import pytest

import userctx
from export import pdf_document
from export.pdf_invoice import generate_invoice_pdf
from export.pdf_quote import generate_quote_pdf
from tests.document_helpers import add_contacts, add_document, set_column

USER = 'anna'
LABELS = ('Rechnungsdatum:', 'Leistungsdatum:', 'Leistungszeitraum:', 'Rechnungs-Nr.:', 'Kunden-Nr.:', 'Angebots-Nr.:', 'Datum:', 'gültig bis:',
          'Fassung:')


@pytest.fixture
def world(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    userctx.set_user(USER)
    own, customer = add_contacts(tmp_db)
    return SimpleNamespace(db=tmp_db, own=own, customer=customer)


def header_rows(pdf):
    """Die Zeilen des Kopfblocks von oben nach unten: ``(Bezeichnung, Wert, rechter Rand der Bezeichnung, linker Rand des
    Werts)`` – alles rechts von der Bezeichnungsspalte, oberhalb des Titels. Eine Zeile ohne Bezeichnung (die zweite Zeile eines
    Leistungszeitraums: „bis …“) hat den Rand 0."""
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        page = document.pages[0]
        words = [w for w in page.extract_words() if w['x0'] >= pdf_document.META_LABEL_X - 1 and w['top'] < 150]
    lines = {}
    for word in words:
        lines.setdefault(round(word['top']), []).append(word)
    rows = []
    for top in sorted(lines):
        label = [w for w in lines[top] if w['x0'] < pdf_document.META_VALUE_X - 1]
        value = [w for w in lines[top] if w['x0'] >= pdf_document.META_VALUE_X - 1]
        rows.append((' '.join(w['text'] for w in label), ' '.join(w['text'] for w in value),
                     max((w['x1'] for w in label), default=0), min(w['x0'] for w in value)))
    return rows


def test_the_invoice_block_follows_the_form(world):
    iid = add_document(world.db, '26R001', own_id=world.own, customer_id=world.customer)
    set_column(world.db, iid, 'DeliveryDate', '2026-03-01')
    rows = header_rows(generate_invoice_pdf(world.db, iid)[0])
    assert [(label, value) for label, value, _, _ in rows] == [
        ('Rechnungsdatum:', '2026-03-04'), ('Leistungsdatum:', '2026-03-01'), ('Rechnungs-Nr.:', '26R001'),
        ('Kunden-Nr.:', 'K-100')]


def test_an_invoice_without_a_delivery_date_or_customer_number_leaves_no_gap(world):
    """Bestand aus einem Import: Was fehlt, bekommt keine leere Zeile."""
    iid = add_document(world.db, '26R001', own_id=world.own, customer_id=world.customer)
    set_column(world.db, iid, 'DeliveryDate', None)
    with world.db._get_connection() as connection:
        connection.execute("UPDATE Contacts SET CustomerNumber = '' WHERE ID = ?", (world.customer,))
    rows = header_rows(generate_invoice_pdf(world.db, iid)[0])
    assert [label for label, *_ in rows] == ['Rechnungsdatum:', 'Rechnungs-Nr.:']


def test_the_quote_block_follows_the_form(world):
    qid = add_document(world.db, '26A001', 'quote', own_id=world.own, customer_id=world.customer)
    set_column(world.db, qid, 'ValidUntil', '2026-04-03')
    rows = header_rows(generate_quote_pdf(world.db, qid)[0])
    assert [(label, value) for label, value, _, _ in rows] == [
        ('Datum:', '2026-03-04'), ('gültig bis:', '2026-04-03'), ('Angebots-Nr.:', '26A001'), ('Kunden-Nr.:', 'K-100')]


def test_the_forms_name_the_rows_in_the_same_order_as_the_pdf(world):
    """Die Maske ist der Maßstab: Wer dort eine Zeile umstellt, stellt sie auch im PDF um."""
    from server.pages_invoice import PageInvoice
    from server.pages_quote import PageQuote

    def form_labels(html):
        table = html.split('class="invoice-meta-table"')[1].split('</table>')[0]
        return [label.replace('&uuml;', 'ü') for label in re.findall(r'<tr><td[^>]*>([^<]+)</td>', table)]

    # „bis:“ ist das Ende des Leistungszeitraums; das PDF nennt es nur, wenn es eines gibt („Leistungszeitraum:“ mit dem Beginn, darunter „bis …“)
    assert form_labels(PageInvoice(world.db)) == ['Rechnungsdatum:', 'Leistungsdatum:', 'bis:', 'Rechnungs-Nr.:', 'Kunden-Nr.:']
    assert form_labels(PageQuote(world.db)) == ['Datum:', 'gültig bis:', 'Angebots-Nr.:', 'Kunden-Nr.:']


def test_no_label_runs_into_its_value(world):
    iid = add_document(world.db, '26R001', own_id=world.own, customer_id=world.customer)
    qid = add_document(world.db, '26A001', 'quote', own_id=world.own, customer_id=world.customer)
    set_column(world.db, qid, 'ValidUntil', '2026-04-03')
    for pdf in (generate_invoice_pdf(world.db, iid)[0], generate_quote_pdf(world.db, qid)[0]):
        for label, _, label_end, value_start in header_rows(pdf):
            assert value_start - label_end >= 5, label


def test_the_label_column_holds_the_longest_label():
    column = pdf_document.META_VALUE_X - pdf_document.META_LABEL_X
    for label in LABELS:
        assert pdf_document.text_width(label, 10) + 5 <= column, label
