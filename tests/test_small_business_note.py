# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Hinweis auf § 19 UStG im PDF (2.3.3) und das Häkchen „Umsatzsteuer ausweisen“ eines neuen Belegs.

Ein Beleg ohne Umsatzsteuerausweis (Kleinunternehmer: ``Invoices.TaxRate`` = Platzhalter −1) druckt den Hinweis
selbst, in einer Zeile ab dem linken Seitenrand unter dem Summenblock – bis 2.3.2 stand er nur im XML. Ein Wortlaut für
PDF, CII und UBL (``export.einvoice_model.EXEMPT_REASON_KLEINUNTERNEHMER``). Unterdrückt wird der Hinweis im PDF nur,
wenn ein Freitext des Belegs das Gesetz ausdrücklich zitiert: „§ 19“ (auch „§ 19a“), höchstens „Abs. n“ und „Satz n“
dazwischen, unmittelbar gefolgt von „UStG“ oder „Umsatzsteuergesetz“ – je Text einzeln und so, wie das PDF ihn druckt
(Richtext erst durch die Positivliste). Geprüft werden bei der Rechnung der Zahlungshinweis, beim Angebot Einleitung und
Schluss; alle drei setzt das PDF als Richtext. Die Positionstexte, das Wort „Kleinunternehmer“ allein, „§ 19 BGB“ oder ein
Füllwort zwischen „§ 19“ und „UStG“ unterdrücken nichts: Ein fehlender Pflichthinweis wiegt schwerer als ein doppelter.
Ein Betreff-Feld gibt es nicht.

Das Häkchen eines NEUEN Belegs ohne Quelle (Rechnung wie Angebot) übernimmt den Stand der jüngsten gewöhnlichen Rechnung
(``Database.last_document_shows_tax``: kein Angebot, keine Stornorechnung, keine Ersatzrechnung); startet es abgehakt, sagt
die Maske es dazu. Bearbeiten und Kopieren folgen weiter der Quelle.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1, stream_value

import server.handlers as handlers
import userctx
from export import pdf_document as D
from export.cii_invoice import NS, generate_cii
from export.einvoice_model import EXEMPT_REASON_KLEINUNTERNEHMER as NOTE, small_business_note
from export.invoice_files import stored_invoice_path
from export.pdf_invoice import generate_invoice_pdf
from export.pdf_quote import generate_quote_pdf
from export.xrechnung_invoice import XRechnungGenerator
from server.pages_invoice import PageInvoice
from server.pages_quote import PageQuote
from tests.golden_cases import make_case
from tests.invoice_world import build_world, columns, make_invoice, run_sql, set_invoice_status

USER = 'anna'
NBSP = chr(0xA0)                                              # geschütztes Leerzeichen
ONE_ITEM = {'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung', 'unitPrice': 100.0, 'taxRate': 19}
NO_TAX_ITEM = dict(ONE_ITEM, taxRate=0)                      # so schickt das Formular die Positionen ohne Steuerausweis
FORM_HINT = '(aus = Kleinunternehmer §&nbsp;19 UStG; der Hinweis darauf wird automatisch gedruckt)'
OLD_FORM_HINT = 'Hinweis ggf. unten im Text'
PREFILL_NOTE = ('<p class="warnColor" id="tax_prefill_note">Vorbelegt wie die letzte Rechnung: ohne Umsatzsteuer '
               '(Kleinunternehmer, §&nbsp;19 UStG).</p>')


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user(USER)
    w = build_world(tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return w


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _document(db, number, tax_rate, *, kind='invoice', terms='Zahlbar innerhalb von 14 Tagen.',
              descriptions=('Beratung',), intro=None, closing=None):
    """Rechnung oder Angebot mit einer Position je Beschreibung (100,00 € netto). ``tax_rate``: der Platzhalter −1
    (ohne Umsatzsteuerausweis), 0 oder ein Satz in Prozent."""
    net = 100.0 * len(descriptions)
    item_rate = max(tax_rate, 0.0)
    tax = round(net * item_rate / 100, 2)
    doc_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-06-01', 'delivery_date': '2026-06-01',
        'seller_name': 'Anna Verkäufer', 'seller_company': 'Muster & Söhne GmbH', 'seller_street': 'Musterstraße 1',
        'seller_postal_code': '12345', 'seller_city': 'Musterstadt', 'buyer_name': 'Max Mustermann',
        'buyer_company': 'Malermeister Pinsel', 'buyer_street': 'Kundenweg 5', 'buyer_postal_code': '54321',
        'buyer_city': 'Kundenhausen', 'payment_terms': terms, 'tax_rate': tax_rate, 'sum_net': net,
        'tax_amount': tax, 'sum_gross': net + tax, 'amount_due': net + tax,
        'status': 'finalized' if kind == 'invoice' else 'draft', 'document_type': kind,
        'intro_text': intro, 'closing_text': closing})
    for position, description in enumerate(descriptions, 1):
        db.insert_invoice_item({
            'invoice_id': doc_id, 'position': position, 'description': description, 'quantity': 1.0, 'unit': 'C62',
            'price_per_unit': 100.0, 'total_net': 100.0, 'tax_category': 'S', 'tax_rate': item_rate})
    return doc_id


def _invoice_pdf(db, number, tax_rate, **kwargs):
    return generate_invoice_pdf(db, _document(db, number, tax_rate, **kwargs))[0]


def _quote_pdf(db, number, tax_rate, **kwargs):
    return generate_quote_pdf(db, _document(db, number, tax_rate, kind='quote', **kwargs))[0]


def _page_texts(pdf_bytes):
    """Der Text je Seite, Leerraum zu einem Leerzeichen zusammengezogen (ein umbrochener Satz bleibt auffindbar)."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [' '.join((page.extract_text() or '').split()) for page in pdf.pages]


def _text(pdf_bytes):
    return ' '.join(_page_texts(pdf_bytes))


def _embedded_xml(pdf_bytes):
    doc = PDFDocument(PDFParser(io.BytesIO(pdf_bytes)))
    spec = resolve1(resolve1(doc.catalog['AF'])[0])
    return stream_value(resolve1(spec['EF'])['F']).get_data()


def _text_lines(page):
    """Die Textzeilen einer Seite als ``(Grundlinie, linker Rand, Text)`` – Grundlinie in PDF-Koordinaten (von unten)."""
    lines = {}
    for char in page.chars:
        lines.setdefault(round(char['matrix'][5], 1), []).append(char)
    return [(baseline, min(c['x0'] for c in chars), ''.join(c['text'] for c in sorted(chars, key=lambda c: c['x0'])))
            for baseline, chars in sorted(lines.items())]


def _baselines(page, *snippets):
    """Grundlinien der Textzeilen einer Seite, die einen der Ausschnitte enthalten."""
    return [baseline for baseline, _, line in _text_lines(page) if any(snippet in line for snippet in snippets)]


def _box_checked(html):
    return 'id="show_tax" checked' in html


# ── Die Funktion: wann der Hinweis gesetzt wird ─────────────────────────────

def test_the_wording_is_one_text_for_pdf_and_xml():
    assert NOTE == ('Es gilt die Steuerbefreiung für Kleinunternehmer nach § 19 UStG. '
                    'Es wird keine Umsatzsteuer berechnet.')


@pytest.mark.parametrize('rate', [-1, -1.0, Decimal('-1'), -0.5])
def test_a_negative_rate_is_the_placeholder_and_gets_the_note(rate):
    assert small_business_note(rate) == NOTE


@pytest.mark.parametrize('rate', [0, 0.0, 0.19, 19, 19.0, 7, Decimal('0.19'), None])
def test_every_other_rate_gets_no_note(rate):
    """0 % ohne Platzhalter ist ein anderer Fall (Kategorie Z), kein Satz gilt wie im PDF als 19 %."""
    assert small_business_note(rate) is None
    assert small_business_note(rate, 'Zahlbar in 14 Tagen.') is None


@pytest.mark.parametrize('text', [
    'Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.',
    '§19UStG', 'Gemäß §19UStG', 'gemäß §  19   UStG',
    'nach § 19 Abs. 1 UStG', 'nach §19 Abs. 1 UStG', 'nach § 19 Abs.1 UStG', 'nach § 19 Absatz 1 UStG',
    'nach § 19 Abs. 1 Satz 1 UStG', 'nach § 19 Absatz 1 S. 1 UStG', 'nach § 19 Abs. 1 S.2 UStG',
    'Steuerfrei nach § 19 Umsatzsteuergesetz', 'nach § 19 Abs. 1 Umsatzsteuergesetz',
    'Gemäß § 19a UStG', 'Gemäß § 19a Abs. 2 UStG',               # die Sonderregel für Kleinunternehmer der EU
    'GEMÄSS § 19 USTG',
    'Gemäß §&nbsp;19&nbsp;UStG', f'Gemäß §{NBSP}19{NBSP}UStG', f'nach § 19 Abs.{NBSP}1 UStG',   # geschütztes Leerzeichen
    'Gemäß &sect; 19 UStG', 'Gemäß &#167; 19 UStG',              # das PDF löst die Entität auf und druckt „§“
    '<p>Gemäß <b>§ 19</b> UStG</p>', '<p>§&nbsp;19 <i>UStG</i></p>', '<p>§ 19</p><p>UStG</p>',           # Tags dazwischen
    'Preise: 10 EUR. Gemäß § 19 UStG wird keine Umsatzsteuer berechnet. Zahlbar in 14 Tagen.',          # mitten im Text
])
def test_a_text_that_cites_the_law_suppresses_the_note(text):
    assert small_business_note(-1, text) is None
    assert small_business_note(-1, 'Zahlbar in 14 Tagen.', None, text) is None      # irgendeiner der Texte genügt


@pytest.mark.parametrize('text', [
    # kein ausdrückliches Zitat des Gesetzes: das Wort allein, ein anderes Gesetz, die eigenen Bedingungen
    'Es gelten § 19 unserer AGB. USt-IdNr. folgt', 'Kleinunternehmerregelung', 'Kleinunternehmer',
    'Gemäß Kleinunternehmerregelung', '§ 19 BGB', 'Rücktritt nach § 19 BGB', '§ 19', '§ 19a', '<p>§&nbsp;19</p>',
    # nicht „UStG“ unmittelbar dahinter: dann steht der Hinweis eben doppelt
    'Gem. § 19 wird keine Umsatzsteuer berechnet', 'Gemäß § 19 des Umsatzsteuergesetzes',
    '§ 19 Absatz 1 Satz 1 des Umsatzsteuergesetzes', 'nach § 19 Abs. 1 des UStG', 'nach § 19, UStG',
    'nach § 19 (UStG)', 'nach § 19 Abs. 1, UStG', 'nach § 19 Abs. UStG', 'nach § 19 Abs. 1 Nr. 2 UStG',
    # nur ein Bezug zur Umsatzsteuer, kein Zitat
    'Umsatzsteuer entfällt', 'Preise ohne MwSt.', 'Gemäß UStG', 'Paragraph 19 UStG',
    # nicht „§ 19“
    '§ 190 UStG', '§ 191 Umsatzsteuergesetz', '§ 1 UStG', '§ 9 UStG', '§ 119 UStG', '§ 119a UStG',
    '', '   ', '<p></p>'])
def test_a_text_without_that_citation_does_not_suppress_the_note(text):
    assert small_business_note(-1, text) == NOTE


@pytest.mark.parametrize('text', [
    # was die Positivliste (``richtext.sanitize_richtext``) verwirft, druckt das PDF nicht – es unterdrückt nichts
    '<style>Kleinunternehmer § 19 UStG</style>', '<script>§ 19 UStG</script>', '<iframe>§ 19 UStG</iframe>',
    '<svg><text>§ 19 UStG</text></svg>', '<math>§ 19 UStG</math>',
    '<!-- § 19 UStG -->', 'x<!-- Gemäß § 19 UStG -->y',
    '<span title="§ 19 UStG">x</span>', '<a href="§ 19 UStG">Link</a>', '<b data-x="§ 19 UStG">fett</b>',
    # die Entität steht in einem Wert, den das PDF nur einmal auflöst: gedruckt wird „&#167; 19 UStG“
    '&amp;#167; 19 UStG', '&amp;sect; 19 UStG',
    '<style>.a { }</style> Rücktritt nach § 19 BGB'])
def test_what_the_pdf_does_not_print_cannot_suppress_the_note(text):
    assert small_business_note(-1, text) == NOTE


def test_each_text_is_judged_on_its_own():
    """Das Zitat muss in EINEM Text stehen: „§ 19“ im einen und „UStG“ im anderen zählen nicht zusammen."""
    assert small_business_note(-1, 'Leistung nach § 19', 'Gemäß UStG') == NOTE
    assert small_business_note(-1, 'nach § 19', 'UStG') == NOTE
    assert small_business_note(-1, 'nach § 19 Abs. 1', 'UStG') == NOTE


def test_what_is_no_text_is_skipped():
    assert small_business_note(-1) == NOTE
    assert small_business_note(-1, None, 19, 1.5, b'\xc2\xa7 19 UStG', ['§ 19 UStG'], ('Kleinunternehmer',)) == NOTE


@pytest.mark.parametrize('text', [
    '<' * 2_000_000,                                  # „<“ ohne „>“ dahinter liest nicht bis zum Textende
    '§' + ' ' * 2_000_000 + 'x',                      # lange Leerraum-Strecken hinter „§“
    '§ 19 Abs. 1' + ' ' * 2_000_000,                  # … und hinter jedem Teil des Zitats
    '§ 19 Abs. 1 Satz 1 ' * 100_000,                  # viele angefangene Zitate
])
def test_a_text_of_two_megabytes_is_read_in_linear_time(text):
    """Der Freitext kommt vom Nutzer (die SQL-Konsole lässt Werte bis 2 MB zu): Sekunden, nicht Stunden."""
    started = time.monotonic()
    assert small_business_note(-1, text) == NOTE
    assert time.monotonic() - started < 30


# ── Der Summenblock: Lage des Hinweises und Seitenumbruch ───────────────────

_TM = re.compile(r'^1 0 0 1 (-?[\d.]+) (-?[\d.]+) Tm \((.*)\) Tj$')
# ein Hinweis über mehrere Zeilen: dieselbe Lage und derselbe Umbruch wie beim Standardsatz, nur länger
LONG_NOTE = ' '.join([NOTE] * 3)
NOTES = pytest.mark.parametrize('note', [NOTE, LONG_NOTE], ids=['standard-sentence', 'several-lines'])


def _draw(rows, tax_note, start=600):
    """Positionstabelle mit ``rows`` einzeiligen Positionen, ohne Steuerausweis, in einem leeren Seitenfluss."""
    flow = D.PageFlow([[], [], []])
    flow.y = start
    items = [{'pos': n, 'quantity': 1, 'unit': 'Stk.', 'description': f'Position {n}', 'price': 10.0, 'total': 10.0}
             for n in range(1, rows + 1)]
    D.draw_item_table(flow, items, tax_rate_pct=0, sum_net=10.0 * rows, tax_amount=0, sum_gross=10.0 * rows,
                      show_tax=False, tax_note=tax_note)
    return flow


def _drawn(flow):
    """Alle gesetzten Textzeilen des Flusses als ``(Seite, x, y, Text)`` – die abgeschlossenen Seiten, dann die laufende."""
    drawn = []
    for page, ops in enumerate([*flow.pages, flow.ops]):
        for op in ops:
            match = _TM.match(op)
            if match:
                drawn.append((page, float(match.group(1)), float(match.group(2)), match.group(3)))
    return drawn


def _note_lines(flow):
    """Die Zeilen des Hinweises als ``(Seite, x, y, Text)``: links bei ``LEFT``, unterhalb von „Gesamtbetrag:“ derselben Seite."""
    drawn = _drawn(flow)
    total = {page: y for page, _, y, text in drawn if text == 'Gesamtbetrag:'}
    return [(page, x, y, text) for page, x, y, text in drawn if x == D.LEFT and page in total and y < total[page]]


def test_the_standard_sentence_takes_exactly_one_line():
    """Der Standardsatz passt zwischen die Seitenränder (gut 420 von 495 pt) und steht in einer Zeile; ein längerer
    Hinweis bricht um."""
    assert D.text_width(NOTE, 9) < D.RIGHT - D.LEFT
    assert [text for _, _, _, text in _note_lines(_draw(1, NOTE))] == [NOTE]
    assert len(_note_lines(_draw(1, LONG_NOTE))) > 1


@NOTES
def test_the_note_stands_under_the_closing_line_and_the_flow_continues_below_it(note):
    with_note, without = _draw(1, note), _draw(1, None)
    total = next(y for _, _, y, text in _drawn(with_note) if text == 'Gesamtbetrag:')
    lines = _note_lines(with_note)
    # ab dem linken Seitenrand, Größe 9: die erste Zeile 14 pt unter der Schlusslinie (die liegt 6 pt unter der Grundlinie
    # von „Gesamtbetrag:“), dann 12 pt Zeilenabstand; danach geht es mit den bisherigen 16 pt weiter
    assert ' '.join(text for _, _, _, text in lines) == note
    assert all(x == D.LEFT for _, x, _, _ in lines)
    assert [y for _, _, y, _ in lines] == [total - 6 - 14 - 12 * n for n in range(len(lines))]
    assert with_note.y == total - 6 - 14 - 12 * (len(lines) - 1) - 16
    assert without.y == total - 6 - 16
    assert _note_lines(without) == []


def test_without_a_note_the_layout_is_the_old_one():
    assert _draw(5, None).ops == _draw(5, '').ops
    assert _draw(5, None).pages == _draw(5, '').pages
    assert _note_lines(_draw(5, None)) == []


@NOTES
def test_the_note_never_falls_below_the_bottom_and_stays_with_the_totals(note):
    for rows in range(1, 70):
        flow = _draw(rows, note)
        lines = _note_lines(flow)
        total_pages = {page for page, _, _, text in _drawn(flow) if text == 'Gesamtbetrag:'}
        assert ' '.join(text for _, _, _, text in lines) == note, rows
        assert {page for page, _, _, _ in lines} == total_pages, rows             # Hinweis und Summe auf einer Seite
        assert all(y >= flow.bottom for _, _, y, _ in lines), rows


@NOTES
def test_a_note_that_does_not_fit_takes_the_totals_to_the_next_page(note):
    """Der Block gerade noch ohne Hinweis auf der Seite: Mit Hinweis bricht er samt Summe um, nichts bleibt zurück."""
    crossings = [rows for rows in range(1, 70)
                 if len(_draw(rows, None).pages) == 0 and len(_draw(rows, note).pages) == 1]
    assert crossings, 'es gibt eine Positionszahl, bei der erst der Hinweis den Umbruch auslöst'
    for rows in crossings:
        flow = _draw(rows, note)
        drawn = _drawn(flow)
        assert [page for page, _, _, text in drawn if text == 'Gesamtbetrag:'] == [1], rows
        assert {page for page, _, _, _ in _note_lines(flow)} == {1}, rows
        assert [text for page, _, _, text in drawn if page == 0 and 'Kleinunternehmer' in text] == [], rows


# ── Rechnung: Hinweis im PDF ────────────────────────────────────────────────

def test_an_invoice_without_tax_carries_the_note_once_under_the_totals(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pdf = _invoice_pdf(tmp_db, 'KU-1', -1)
    text = _text(pdf)
    assert text.count(NOTE) == 1
    assert text.index('Gesamtbetrag:') < text.index(NOTE) < text.index('Zahlbar innerhalb von 14 Tagen.')
    assert 'MwSt' not in text and 'Summe netto' not in text                   # sonst unverändert: keine Steuerzeile
    with pdfplumber.open(io.BytesIO(pdf)) as parsed:                           # eine Zeile, ab dem linken Seitenrand
        lines = [(left, line) for _, left, line in _text_lines(parsed.pages[0]) if 'Steuerbefreiung' in line]
    assert len(lines) == 1 and lines[0][1] == NOTE and abs(lines[0][0] - D.LEFT) < 1


@pytest.mark.parametrize('terms, own_text', [
    ('Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.', 'Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.'),
    ('Steuerfrei nach § 19 Abs. 1 UStG. Zahlbar in 14 Tagen.', 'Steuerfrei nach § 19 Abs. 1 UStG. Zahlbar in 14 Tagen.'),
    ('<p>Gemäß <b>§&nbsp;19</b> UStG keine Umsatzsteuer.</p>', 'Gemäß § 19 UStG keine Umsatzsteuer.'),
    ('Gemäß &#167; 19 UStG keine Umsatzsteuer.', 'Gemäß § 19 UStG keine Umsatzsteuer.'),   # das PDF löst die Entität auf
])
def test_a_payment_text_that_cites_the_law_stays_alone(tmp_db, monkeypatch, tmp_path, terms, own_text):
    monkeypatch.chdir(tmp_path)
    text = _text(_invoice_pdf(tmp_db, 'KU-2', -1, terms=terms))
    assert text.count(own_text) == 1                                          # der eigene Text bleibt
    assert 'Steuerbefreiung' not in text and NOTE not in text                 # kein zweiter Hinweis daneben


@pytest.mark.parametrize('descriptions', [
    ('Beratung', 'Leistung nach § 19 UStG'), ('Prüfung nach § 19 Abs. 1 UStG',), ('Workshop für Kleinunternehmer',)])
def test_a_position_text_never_suppresses_the_note(tmp_db, monkeypatch, tmp_path, descriptions):
    """Die Positionen sind Leistungen, kein Hinweis des Belegs: Auch ein Zitat darin lässt den Hinweis stehen."""
    monkeypatch.chdir(tmp_path)
    text = _text(_invoice_pdf(tmp_db, 'KU-3', -1, descriptions=descriptions))
    assert text.count(NOTE) == 1 and descriptions[-1] in text


def test_the_word_kleinunternehmer_alone_does_not_suppress_the_note(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    text = _text(_invoice_pdf(tmp_db, 'KU-3b', -1, terms='Kleinunternehmer – Zahlbar in 14 Tagen.'))
    assert text.count(NOTE) == 1 and 'Kleinunternehmer – Zahlbar in 14 Tagen.' in text


@pytest.mark.parametrize('terms', [
    '<style>Kleinunternehmer § 19 UStG</style>Zahlbar in 14 Tagen.',
    '<!-- § 19 UStG -->Zahlbar in 14 Tagen.',
    '<span title="§ 19 UStG">Zahlbar in 14 Tagen.</span>',
    'Zahlbar in 14 Tagen. &amp;#167; 19 UStG',
])
def test_what_the_pdf_does_not_print_does_not_suppress_the_note(tmp_db, monkeypatch, tmp_path, terms):
    """Geprüft wird der Text, den das PDF druckt: Was die Positivliste verwirft (Style-Block, Kommentar, Attribut) oder
    nur als Zeichenfolge „&#167;“ erscheint, zitiert das Gesetz nicht."""
    monkeypatch.chdir(tmp_path)
    text = _text(_invoice_pdf(tmp_db, 'KU-3c', -1, terms=terms))
    assert text.count(NOTE) == 1 and 'Zahlbar in 14 Tagen.' in text
    assert '§ 19 UStG' not in text.replace(NOTE, '')                          # nichts davon steht im Dokument


@pytest.mark.parametrize('terms, descriptions', [
    ('Rücktritt nach § 19 BGB möglich.', ('Beratung',)),
    ('Es gilt § 19 unserer Bedingungen.', ('Beratung',)),
    ('Gem. § 19 wird keine Umsatzsteuer berechnet.', ('Beratung',)),         # kein „UStG“ dahinter: der Hinweis steht doppelt
    ('Preise inklusive Umsatzsteuer.', ('Beratung', 'Leistung nach § 19 BGB')),
])
def test_paragraph_19_without_the_act_does_not_replace_the_note(tmp_db, monkeypatch, tmp_path, terms, descriptions):
    """Ein fehlender Pflichthinweis wiegt schwerer als ein doppelter: Der Hinweis steht da, der eigene Text bleibt."""
    monkeypatch.chdir(tmp_path)
    text = _text(_invoice_pdf(tmp_db, 'KU-7', -1, terms=terms, descriptions=descriptions))
    assert text.count(NOTE) == 1
    assert terms in text and descriptions[-1] in text


def test_the_standard_payment_text_is_part_of_the_check(tmp_db, monkeypatch, tmp_path):
    """Ohne Zahlungshinweis druckt das PDF den Standardtext; er trägt den Hinweis nicht, also steht der Hinweis da."""
    monkeypatch.chdir(tmp_path)
    text = _text(_invoice_pdf(tmp_db, 'KU-4', -1, terms=None))
    assert text.count(NOTE) == 1 and 'innerhalb von 14 Tagen ab Rechnungsdatum' in text


def test_an_invoice_does_not_check_the_texts_it_does_not_print(tmp_db, monkeypatch, tmp_path):
    """Einleitung und Schluss einer Rechnung (aus einem Angebot übernommen) druckt das PDF nicht – sie zählen nicht."""
    monkeypatch.chdir(tmp_path)
    text = _text(_invoice_pdf(tmp_db, 'KU-5', -1, intro='Gemäß § 19 UStG', closing='Kleinunternehmer'))
    assert text.count(NOTE) == 1


@pytest.mark.parametrize('rate', [19.0, 7.0, 0])
def test_an_invoice_with_a_rate_or_with_zero_percent_carries_no_note(tmp_db, monkeypatch, tmp_path, rate):
    monkeypatch.chdir(tmp_path)
    text = _text(_invoice_pdf(tmp_db, 'ST-1', rate))
    assert 'Steuerbefreiung' not in text and 'Kleinunternehmer' not in text


def test_the_golden_cases_agree(tmp_db, monkeypatch, tmp_path):
    """Die festen Fälle der Golden-Tests: nur der Kleinunternehmer trägt den Hinweis."""
    monkeypatch.chdir(tmp_path)
    texts = {case: _text(generate_invoice_pdf(tmp_db, make_case(tmp_db, case))[0]) for case in ('std19', 'std7', 'ku')}
    assert [NOTE in texts[case] for case in ('std19', 'std7', 'ku')] == [False, False, True]


# ── Angebot ──────────────────────────────────────────────────────────────────

def test_a_quote_without_tax_carries_the_note_once_between_the_totals_and_the_closing_text(tmp_db, monkeypatch,
                                                                                            tmp_path):
    monkeypatch.chdir(tmp_path)
    text = _text(_quote_pdf(tmp_db, '26A001', -1, intro='Vielen Dank für Ihre Anfrage.', closing='Wir freuen uns.'))
    assert text.count(NOTE) == 1
    assert text.index('Vielen Dank') < text.index('Gesamtbetrag:') < text.index(NOTE) < text.index('Wir freuen uns.')


@pytest.mark.parametrize('intro, closing, own_text', [
    ('Gemäß § 19 UStG keine Umsatzsteuer.', 'Gruß', 'Gemäß § 19 UStG keine Umsatzsteuer.'),
    ('Hallo', '<p>Steuerfrei nach <b>§ 19</b> Abs. 1 <i>UStG</i> – danke.</p>', 'Steuerfrei nach § 19 Abs. 1 UStG – danke.'),
    ('Hallo', 'Gruß – Kein Ausweis nach §19UStG.', 'Kein Ausweis nach §19UStG.'),
])
def test_a_quote_text_that_cites_the_law_stays_alone(tmp_db, monkeypatch, tmp_path, intro, closing, own_text):
    monkeypatch.chdir(tmp_path)
    text = _text(_quote_pdf(tmp_db, '26A002', -1, intro=intro, closing=closing))
    assert own_text in text and 'Steuerbefreiung' not in text


@pytest.mark.parametrize('intro, closing, descriptions', [
    ('Hallo', 'Gruß', ('Beratung', 'Leistung nach § 19 UStG')),                       # Positionen zählen nicht
    ('Hallo', 'Als Kleinunternehmer ohne USt.', ('Beratung',)),                        # das Wort allein genügt nicht
    ('Rücktritt nach § 19 BGB möglich.', 'Gruß', ('Beratung', 'Leistung nach § 19 der AGB')),
    ('<style>Kleinunternehmer § 19 UStG</style>Hallo', '<!-- § 19 UStG -->Gruß', ('Beratung',)),   # druckt das PDF nicht
])
def test_a_quote_keeps_the_note_unless_intro_or_closing_cite_the_law(tmp_db, monkeypatch, tmp_path, intro, closing,
                                                                     descriptions):
    monkeypatch.chdir(tmp_path)
    text = _text(_quote_pdf(tmp_db, '26A005', -1, intro=intro, closing=closing, descriptions=descriptions))
    assert text.count(NOTE) == 1 and descriptions[-1] in text
    assert '§ 19 UStG' not in text.replace(NOTE, '').replace('Leistung nach § 19 UStG', '')


def test_a_quote_does_not_check_the_texts_it_does_not_print(tmp_db, monkeypatch, tmp_path):
    """Das Angebots-PDF druckt keinen Zahlungshinweis: Steht der Hinweis nur dort, fehlt er im Dokument."""
    monkeypatch.chdir(tmp_path)
    text = _text(_quote_pdf(tmp_db, '26A003', -1, terms='Gemäß § 19 UStG'))
    assert text.count(NOTE) == 1


@pytest.mark.parametrize('rate', [19.0, 0])
def test_a_quote_with_a_rate_or_with_zero_percent_carries_no_note(tmp_db, monkeypatch, tmp_path, rate):
    monkeypatch.chdir(tmp_path)
    assert 'Steuerbefreiung' not in _text(_quote_pdf(tmp_db, '26A004', rate))


# ── Stornorechnung (derselbe Code wie die Rechnung) ─────────────────────────

def _cancelled(world, number, **over):
    """Eine Rechnung versenden und stornieren, wie die Oberfläche es tut: ``(Rechnung, Stornorechnung)``."""
    invoice_id = make_invoice(world, 'draft', number=number, **over)
    code, body = set_invoice_status(invoice_id, 'sent')
    assert code == 200 and 'warning' not in body, body
    code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': '2026-07-01'}).encode())
    body = json.loads(text)
    assert code == 200 and body['success'] is True and 'warning' not in body, body
    return invoice_id, body['storno_id']


def _stored_text(world, document_id):
    path, _ = stored_invoice_path(world.db, document_id, 'pdf')
    assert path, 'das Dokument ist abgelegt'
    return _text(Path(path).read_bytes())


def test_the_credit_note_of_an_invoice_without_tax_carries_the_note_once_like_the_invoice(world):
    invoice_id, storno_id = _cancelled(world, 'T-500', items=[NO_TAX_ITEM], taxRate=0, showTax=False)
    assert columns(world.db, storno_id)['TaxRate'] == -1
    for document_id in (invoice_id, storno_id):
        assert _stored_text(world, document_id).count(NOTE) == 1, document_id
    assert 'Stornorechnung' in _stored_text(world, storno_id)


def test_the_credit_note_of_an_invoice_with_tax_carries_no_note(world):
    invoice_id, storno_id = _cancelled(world, 'T-501', items=[ONE_ITEM])
    for document_id in (invoice_id, storno_id):
        assert 'Steuerbefreiung' not in _stored_text(world, document_id), document_id


# ── Umbruch im fertigen PDF ──────────────────────────────────────────────────

def test_a_note_that_does_not_fit_moves_with_the_totals_in_the_finished_pdf(tmp_db, monkeypatch, tmp_path):
    """Der Summenblock passt gerade noch auf die erste Seite, mit Hinweis nicht mehr: Beide stehen auf Seite 2, der
    Hinweis (eine Zeile) endet nie unter dem unteren Rand (``PageFlow.bottom``), Seite 1 trägt keinen Rest."""
    monkeypatch.chdir(tmp_path)

    def totals_page(rows, terms):
        pages = _page_texts(_invoice_pdf(tmp_db, f'KU-{rows}-{len(terms)}', -1, terms=terms,
                                         descriptions=[f'Position {n}' for n in range(1, rows + 1)]))
        return next(index for index, text in enumerate(pages) if 'Gesamtbetrag:' in text)

    own = 'Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.'                 # trägt den Hinweis: nichts kommt dazu
    crossing = next(rows for rows in range(1, 70) if totals_page(rows, 'Zahlbar in 14 Tagen.') == 1)
    assert totals_page(crossing, own) == 0, 'ohne den Hinweis passt der Summenblock noch auf die erste Seite'

    for rows in range(crossing - 2, crossing + 3):
        pdf = _invoice_pdf(tmp_db, f'KU-U{rows}', -1, descriptions=[f'Position {n}' for n in range(1, rows + 1)])
        texts = _page_texts(pdf)
        holder = [index for index, text in enumerate(texts) if NOTE in text]
        assert len(holder) == 1 and sum(text.count(NOTE) for text in texts) == 1, rows
        assert 'Gesamtbetrag:' in texts[holder[0]], rows                       # Hinweis und Summe auf einer Seite
        assert holder[0] == (0 if rows < crossing else 1), rows
        with pdfplumber.open(io.BytesIO(pdf)) as parsed:
            baselines = _baselines(parsed.pages[holder[0]], 'Steuerbefreiung')
        assert len(baselines) == 1 and min(baselines) >= D.PageFlow([[], [], []]).bottom, (rows, baselines)


# ── XML: derselbe Wortlaut in CII, UBL und im eingebetteten Dokument ────────

def _models(db, document_id):
    return db.get_invoice_by_id(document_id), db.get_invoice_items(document_id)


def test_cii_and_ubl_carry_the_wording_of_the_pdf(tmp_db):
    invoice, items = _models(tmp_db, make_case(tmp_db, 'ku'))
    cii = ET.fromstring(generate_cii(invoice, items, db=tmp_db))
    reasons = cii.findall('.//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax/ram:ExemptionReason', NS)
    assert [r.text for r in reasons] == [NOTE]
    ubl = ET.fromstring(XRechnungGenerator().generate_xml(invoice, items, db=tmp_db).encode('utf-8'))
    assert [e.text for e in ubl.iter() if e.tag.endswith('}TaxExemptionReason')] == [NOTE]


def test_the_credit_note_documents_carry_the_wording_too(world):
    _, storno_id = _cancelled(world, 'T-502', items=[NO_TAX_ITEM], taxRate=0, showTax=False)
    invoice, items = _models(world.db, storno_id)
    cii = ET.fromstring(generate_cii(invoice, items, db=world.db))
    assert [r.text for r in cii.iter(f'{{{NS["ram"]}}}ExemptionReason')] == [NOTE]
    ubl = ET.fromstring(XRechnungGenerator().generate_xml(invoice, items, db=world.db).encode('utf-8'))
    assert ubl.tag.endswith('}CreditNote')
    assert [e.text for e in ubl.iter() if e.tag.endswith('}TaxExemptionReason')] == [NOTE]


def test_the_own_text_does_not_remove_the_reason_from_the_xml(tmp_db, monkeypatch, tmp_path):
    """BT-120 ist für die Kategorie E vorgeschrieben: Der Nutzertext verdrängt nur den Hinweis im PDF, nie den im XML."""
    monkeypatch.chdir(tmp_path)
    document_id = _document(tmp_db, 'KU-6', -1, terms='Gemäß § 19 UStG keine USt.')
    pdf = generate_invoice_pdf(tmp_db, document_id)[0]
    assert NOTE not in _text(pdf)
    reason = ET.fromstring(_embedded_xml(pdf)).find('.//ram:ApplicableTradeTax/ram:ExemptionReason', NS)
    assert reason is not None and reason.text == NOTE


def test_the_pdf_and_its_embedded_e_invoice_name_the_same_sentence(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pdf = generate_invoice_pdf(tmp_db, make_case(tmp_db, 'ku'))[0]
    reason = ET.fromstring(_embedded_xml(pdf)).find(
        './/ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax/ram:ExemptionReason', NS).text
    assert reason == NOTE and _text(pdf).count(reason) == 1


# ── Das Häkchen „Umsatzsteuer ausweisen“ eines neuen Belegs ─────────────────

def _sent(world, number, **over):
    """Eine Rechnung über das Formular anlegen und versenden (Dokument und Forderung entstehen wie in der Oberfläche)."""
    invoice_id = make_invoice(world, 'draft', number=number, **over)
    code, body = set_invoice_status(invoice_id, 'sent')
    assert code == 200 and 'warning' not in body, body
    return invoice_id


NO_TAX = dict(items=[NO_TAX_ITEM], taxRate=0, showTax=False)        # was das Formular ohne Häkchen schickt
WITH_TAX = dict(items=[ONE_ITEM])                                    # 19 %


def _new_forms(db):
    """Die beiden Formulare für einen neuen Beleg ohne Quelle."""
    return PageInvoice(db, filters={}, invoice_id=None), PageQuote(db)


def test_the_newest_ordinary_invoice_decides_the_state_for_a_new_document(tmp_db):
    db = tmp_db
    assert db.last_document_shows_tax() is True                    # noch keine Rechnung
    _document(db, 'R-1', -1)
    assert db.last_document_shows_tax() is False                   # Platzhalter: ohne Steuer abgerechnet
    _document(db, 'R-2', 0)
    assert db.last_document_shows_tax() is True                    # 0 % ist kein Platzhalter
    _document(db, 'R-3', -1)
    assert db.last_document_shows_tax() is False
    _document(db, 'R-4', 7.0)
    assert db.last_document_shows_tax() is True


def test_a_quote_stays_out_of_the_state(tmp_db):
    _document(tmp_db, 'A-0', -1, kind='quote')
    assert tmp_db.last_document_shows_tax() is True                # nur ein Angebot: ohne Rechnung ist das Häkchen an
    _document(tmp_db, 'R-1', 19.0)
    _document(tmp_db, 'A-1', -1, kind='quote')                     # das jüngste Angebot ist ohne Steuer
    assert tmp_db.last_document_shows_tax() is True
    _document(tmp_db, 'R-2', -1)
    _document(tmp_db, 'A-2', 19.0, kind='quote')                   # das jüngste Angebot trägt Steuer
    assert tmp_db.last_document_shows_tax() is False


def test_anything_that_is_no_negative_rate_shows_the_tax(tmp_db):
    document_id = _document(tmp_db, 'R-1', -1)
    assert tmp_db.last_document_shows_tax() is False
    run_sql(tmp_db, 'UPDATE Invoices SET TaxRate = ? WHERE ID = ?', 'unlesbar', document_id)   # von Hand geschrieben
    assert tmp_db.last_document_shows_tax() is True


def test_the_state_reading_writes_nothing(tmp_db):
    document_id = _document(tmp_db, 'R-1', -1)
    before = tmp_db.get_invoice_by_id(document_id)
    tmp_db.last_document_shows_tax()
    assert tmp_db.get_invoice_by_id(document_id) == before and len(tmp_db.fetch_invoices()) == 1


def test_a_credit_note_stays_out_of_the_state(world):
    """Die Stornorechnung erbt den Satz der stornierten Rechnung: Sie sagt nichts über die nächste."""
    db = world.db
    plain = _sent(world, 'T-1', **NO_TAX)                           # § 19, alt
    taxed = _sent(world, 'T-2', **WITH_TAX)                         # 19 %, jünger
    credit_note = db.cancel_invoice(plain, '2026-07-01', USER).storno_id
    assert (columns(db, credit_note)['InvoiceKind'], columns(db, credit_note)['TaxRate']) == ('storno', -1)
    assert max(row[0] for row in db.fetch_invoices()) == credit_note       # die jüngste Zeile ist die Stornorechnung
    assert db.last_document_shows_tax() is True
    assert all(_box_checked(html) and PREFILL_NOTE not in html for html in _new_forms(db))
    # umgekehrt: Die jüngste gewöhnliche Rechnung ist ohne Steuer, danach folgt eine Stornorechnung mit Steuer
    _sent(world, 'T-3', **NO_TAX)
    credit_note_with_tax = db.cancel_invoice(taxed, '2026-07-02', USER).storno_id
    assert columns(db, credit_note_with_tax)['TaxRate'] == 0.19
    assert db.last_document_shows_tax() is False
    assert all(not _box_checked(html) and PREFILL_NOTE in html for html in _new_forms(db))


def test_a_replacement_invoice_stays_out_of_the_state(world):
    """„Neu ausstellen“ legt einen Entwurf mit dem Satz der stornierten Rechnung an – auch er sagt nichts über die nächste."""
    db = world.db
    plain = _sent(world, 'T-1', **NO_TAX)
    taxed = _sent(world, 'T-2', **WITH_TAX)
    db.cancel_invoice(plain, '2026-07-01', USER)
    draft = db.create_replacement_draft(plain, USER)
    assert draft.ok and columns(db, draft.invoice_id)['ReplacesInvoiceId'] == plain
    assert columns(db, draft.invoice_id)['TaxRate'] == -1                   # der Entwurf trägt den Platzhalter
    assert db.last_document_shows_tax() is True                             # die jüngste gewöhnliche Rechnung trägt Steuer
    assert all(_box_checked(html) and PREFILL_NOTE not in html for html in _new_forms(db))
    # umgekehrt: Die jüngste gewöhnliche Rechnung ist ohne Steuer, die Ersatzrechnung (jüngste Zeile) trägt Steuer
    _sent(world, 'T-3', **NO_TAX)
    db.cancel_invoice(taxed, '2026-07-02', USER)
    replacement = db.create_replacement_draft(taxed, USER)
    assert replacement.ok and columns(db, replacement.invoice_id)['TaxRate'] == 0.19
    assert db.last_document_shows_tax() is False
    assert all(not _box_checked(html) and PREFILL_NOTE in html for html in _new_forms(db))


def test_a_quote_without_tax_does_not_switch_the_box_off(tmp_db):
    _document(tmp_db, 'R-1', 19.0)
    _document(tmp_db, 'A-1', -1, kind='quote')
    for html in _new_forms(tmp_db):
        assert _box_checked(html) and PREFILL_NOTE not in html


def test_a_new_document_without_a_predecessor_shows_the_tax_and_no_note(tmp_db):
    for html in _new_forms(tmp_db):
        assert _box_checked(html) and PREFILL_NOTE not in html


def test_a_new_document_after_an_invoice_without_tax_has_the_box_off_keeps_19_percent_and_says_so(tmp_db):
    _document(tmp_db, 'R-1', -1)
    for html in _new_forms(tmp_db):
        assert 'id="show_tax"' in html and not _box_checked(html)
        assert 'id="tax_rate" value="19"' in html                   # der Satz im Feld bleibt die Vorbelegung
        assert '__SHOW_TAX_CHECKED__' not in html and '__TAX_PREFILL_NOTE__' not in html
        assert html.count(PREFILL_NOTE) == 1
        # direkt beim Häkchen: nach dem Kästchen, vor der Zeile mit dem Steuersatz
        assert html.index('id="show_tax"') < html.index(PREFILL_NOTE) < html.index('id="tax_row"')


def test_a_new_document_after_an_invoice_with_tax_has_the_box_on_again(tmp_db):
    _document(tmp_db, 'R-1', -1)
    _document(tmp_db, 'R-2', 19.0)
    for html in _new_forms(tmp_db):
        assert _box_checked(html) and PREFILL_NOTE not in html


def test_the_note_uses_an_existing_class_and_no_inline_style(tmp_db):
    """Vorhandene Klasse für Warnhinweise (buch.css: ``.warnColor``), keine neue Klasse und kein Inline-Style."""
    _document(tmp_db, 'R-1', -1)
    assert '.warnColor' in (Path(__file__).resolve().parent.parent / 'buch.css').read_text(encoding='utf-8')
    for html in _new_forms(tmp_db):
        paragraph = html[html.index('<p class="warnColor" id="tax_prefill_note">Vorbelegt'):]
        paragraph = paragraph[:paragraph.index('</p>') + 4]
        assert paragraph == PREFILL_NOTE and 'style=' not in paragraph


def test_editing_and_copying_follow_the_source_and_carry_no_note(tmp_db):
    db = tmp_db
    taxed = _document(db, 'R-1', 19.0)
    plain = _document(db, 'R-2', -1)
    quote_taxed = _document(db, 'A-1', 19.0, kind='quote')
    quote_plain = _document(db, 'A-2', -1, kind='quote')
    # die jüngste gewöhnliche Rechnung ist ohne Steuer: Ein neuer Beleg folgt ihr (mit Hinweis), alles mit Quelle seiner Quelle
    assert all(not _box_checked(html) and PREFILL_NOTE in html for html in _new_forms(db))
    assert _box_checked(PageInvoice(db, filters={}, invoice_id=taxed))
    assert _box_checked(PageInvoice(db, filters={}, invoice_id=None, copy_from=taxed))
    assert _box_checked(PageQuote(db, {}, quote_taxed))
    for html in (PageInvoice(db, filters={}, invoice_id=plain), PageInvoice(db, filters={}, invoice_id=None, copy_from=plain),
                 PageQuote(db, {}, quote_plain)):
        assert not _box_checked(html) and PREFILL_NOTE not in html          # ohne Steuer, aber kein neuer Beleg ohne Quelle
    # und umgekehrt: Die jüngste gewöhnliche Rechnung trägt Steuer
    _document(db, 'R-3', 19.0)
    assert all(_box_checked(html) and PREFILL_NOTE not in html for html in _new_forms(db))
    for html in (PageInvoice(db, filters={}, invoice_id=plain), PageInvoice(db, filters={}, invoice_id=None, copy_from=plain),
                 PageQuote(db, {}, quote_plain)):
        assert not _box_checked(html) and PREFILL_NOTE not in html
    assert _box_checked(PageQuote(db, {}, quote_taxed))


def test_the_id_of_the_other_kind_gives_the_empty_form_with_the_state_of_the_newest_invoice(tmp_db):
    """Die ID eines Angebots im Rechnungsformular (und umgekehrt) ergibt das leere Formular – ohne Quelle."""
    quote = _document(tmp_db, 'A-1', 19.0, kind='quote')
    older_invoice = _document(tmp_db, 'R-1', -1)
    _document(tmp_db, 'R-2', 19.0)
    # die jüngste gewöhnliche Rechnung trägt Steuer: Die leeren Formulare folgen ihr, nicht der fremden ID
    assert _box_checked(PageQuote(tmp_db, {}, older_invoice))
    _document(tmp_db, 'R-3', -1)
    html = PageInvoice(tmp_db, filters={}, invoice_id=quote)                # das Angebot trägt Steuer, die Rechnung nicht
    assert not _box_checked(html) and PREFILL_NOTE in html


def test_the_hint_next_to_the_box_says_the_note_is_printed_automatically(tmp_db):
    document_id = _document(tmp_db, 'R-1', 19.0)
    for html in (PageInvoice(tmp_db, filters={}, invoice_id=None),
                 PageInvoice(tmp_db, filters={}, invoice_id=document_id),
                 PageQuote(tmp_db)):
        assert FORM_HINT in html and OLD_FORM_HINT not in html


# ── Speichern eines so vorbelegten neuen Belegs ─────────────────────────────

def test_saving_a_new_invoice_with_the_box_prefilled_off_writes_the_placeholder(world):
    first = make_invoice(world, 'draft', number='R-1', items=[NO_TAX_ITEM], taxRate=0, showTax=False)
    assert columns(world.db, first)['TaxRate'] == -1
    assert not _box_checked(PageInvoice(world.db, filters={}, invoice_id=None))          # die Vorbelegung
    # was das Formular mit dem Häkchen aus schickt: Satz 0, showTax false, Positionen ohne Satz
    second = make_invoice(world, 'draft', number='R-2', items=[NO_TAX_ITEM], taxRate=0, showTax=False)
    row = columns(world.db, second)
    assert row['TaxRate'] == -1 and row['TaxAmount'] == 0 and row['SumGross'] == row['SumNet']
    assert world.db.last_document_shows_tax() is False


def test_saving_a_new_quote_with_the_box_prefilled_off_writes_the_placeholder(world):
    make_invoice(world, 'draft', number='R-1', items=[NO_TAX_ITEM], taxRate=0, showTax=False)
    assert not _box_checked(PageQuote(world.db))
    payload = {'quoteNumber': '26A001', 'quoteDate': '2026-06-07', 'customerId': world.cust, 'ownCompanyId': world.own,
               'validUntil': None, 'introText': None, 'closingText': None, 'taxRate': 0, 'showTax': False,
               'status': 'draft', 'items': [NO_TAX_ITEM]}
    body = json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))
    assert body['success'] is True, body
    row = columns(world.db, body['quote_id'])
    assert row['TaxRate'] == -1 and row['TaxAmount'] == 0 and row['SumGross'] == row['SumNet']
    assert world.db.last_document_shows_tax() is False


@pytest.mark.parametrize("text", [
    "Brustgurt nach § 19 der Norm",
    "Lustgarten, § 19 der Hausordnung",
])
def test_ustg_inside_another_word_is_no_vat_reference(text):
    assert small_business_note(-1, text) == NOTE


@pytest.mark.parametrize("text", ["§19UStG", "gem. § 19 UStG", "§ 19 Abs. 1 UStG: keine Steuer"])
def test_ustg_after_a_digit_or_space_is_a_vat_reference(text):
    assert small_business_note(-1, text) is None
