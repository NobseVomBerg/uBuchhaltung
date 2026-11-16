# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Skonto auf den Rechnungsdokumenten (2.3.5): PDF, CII, UBL und die Prüfung beim Speichern.

Bis 2.3.4 speicherte die Maske Skonto-Prozent und -Tage (``Invoices.SkontoPercent``/``SkontoDays``) und prüfte sie nicht;
kein Dokument trug sie. Seit 2.3.5 steht das Skonto
* im **PDF** als Absatz nach dem Zahlungshinweis: „Bei Zahlung bis zum 11.03.2026 (innerhalb von 7 Tagen ab Rechnungsdatum)
  gewähren wir 2 % Skonto: abzüglich 3,56 EUR sind dann 174,35 EUR zu zahlen. Ohne Abzug zahlbar bis zum 18.03.2026.“ –
  Grundlage ist das gedruckte Brutto, der Skontobetrag wird in EINEM Schritt auf den Cent gerundet;
* in **CII** (``ram:SpecifiedTradePaymentTerms/ram:Description``) und **UBL** (``cac:PaymentTerms/cbc:Note``) als erste
  Zeile ``#SKONTO#TAGE=7#PROZENT=2.00#`` (BR-DE-18 der XRechnung), danach ein Zeilenumbruch und der Freitext.
Eine Stornorechnung hat nie Skonto, ebenso kein Beleg über höchstens 0 (Gutschrift, 0-€-Rechnung). Beim Speichern einer
Rechnung, die noch nicht eingefroren ist, lehnt der Server unbrauchbare Angaben ab (``export.einvoice_model.parse_skonto``:
Tage als ganze Zahl von 1 bis 365, Prozent größer 0 und kleiner 100 mit höchstens zwei Stellen; geprüft, BEVOR etwas in eine
Zahl umgewandelt wird – ein Text wie ``1e9999999`` kehrt in Millisekunden zurück), ebenso Skonto bei einem Betrag von höchstens 0
und bei einem Rechnungsdatum, aus dem sich keine Frist rechnen lässt; eine versendete Rechnung mit einem Altwert wird nicht
geprüft. Ein unlesbarer gespeicherter Wert fällt nie still weg: Er steht in der Lückenliste (``test_xrechnung_gaps.py``).

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import datetime
import io
import json
import time
import xml.etree.ElementTree as ET
from decimal import Decimal

import pdfplumber
import pytest

import server.handlers as handlers
from export.cii_invoice import NS, generate_cii
from export.einvoice_model import build_einvoice, einvoice_gaps, payment_terms_text
from export.pdf_invoice import generate_invoice_pdf
from export.xrechnung_invoice import XRechnungGenerator
from server.pages_invoice import PageInvoice
from tests.golden_cases import make_case
from tests.invoice_world import columns, make_invoice, make_payload, run_sql, save, save_raw
from tests.test_invoice_storno import USER, at, cancel, sent, world  # noqa: F401  (world: Fixture)

CBC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
CAC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
SENTENCE = ('Bei Zahlung bis zum 11.03.2026 (innerhalb von 7 Tagen ab Rechnungsdatum) gewähren wir 2 % Skonto: '
            'abzüglich 3,56 EUR sind dann 174,35 EUR zu zahlen. Ohne Abzug zahlbar bis zum 18.03.2026.')
DEFAULT_WITHOUT_SKONTO = ('Bitte überweisen Sie den Gesamtbetrag ohne jeden Abzug unter Angabe der Rechnungsnummer '
                          'innerhalb von 14 Tagen ab Rechnungsdatum auf das unten angegebene Konto. Vielen Dank.')
DEFAULT_WITH_SKONTO = ('Bitte überweisen Sie den Betrag unter Angabe der Rechnungsnummer auf das unten angegebene '
                       'Konto. Vielen Dank.')


@pytest.fixture
def pdf_db(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return tmp_db


def _set(db, iid, **columns_):
    with db._get_connection() as connection:
        for column, value in columns_.items():
            connection.execute(f'UPDATE Invoices SET {column} = ? WHERE ID = ?', (value, iid))


def _pdf_text(db, iid):
    pdf, _ = generate_invoice_pdf(db, iid)
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        return ' '.join(' '.join((page.extract_text() or '').split()) for page in document.pages)


def _model(db, iid):
    return build_einvoice(db, db.get_invoice_by_id(iid), db.get_invoice_items(iid))


def _ubl(db, iid):
    xml = XRechnungGenerator().generate_xml(db.get_invoice_by_id(iid), db.get_invoice_items(iid), db=db)
    return ET.fromstring(xml.encode('utf-8'))


def _cii(db, iid):
    return ET.fromstring(generate_cii(db.get_invoice_by_id(iid), db.get_invoice_items(iid), db=db))


# ── PDF ──────────────────────────────────────────────────────────────────────

def test_the_pdf_states_the_skonto_with_the_amounts_and_the_due_date(pdf_db):
    iid = make_case(pdf_db, 'skonto')                      # 177,91 €, Rechnung 04.03.2026, 2 % in 7 Tagen, fällig 18.03.2026

    text = _pdf_text(pdf_db, iid)

    assert SENTENCE in text
    assert 'Zahlbar innerhalb von 14 Tagen.' in text        # der Zahlungshinweis bleibt, das Skonto steht dahinter
    assert text.index('Zahlbar innerhalb von 14 Tagen.') < text.index('Bei Zahlung bis zum')


def test_an_invoice_without_skonto_prints_no_paragraph(pdf_db):
    text = _pdf_text(pdf_db, make_case(pdf_db, 'std19'))

    assert 'Skonto' not in text and 'Bei Zahlung bis zum' not in text


def test_one_day_is_not_several_days(pdf_db):
    iid = make_case(pdf_db, 'std19')
    _set(pdf_db, iid, SkontoDays=1, SkontoPercent=3)

    text = _pdf_text(pdf_db, iid)

    assert ('Bei Zahlung bis zum 05.03.2026 (innerhalb von 1 Tag ab Rechnungsdatum) gewähren wir 3 % Skonto: '
            'abzüglich 5,34 EUR sind dann 172,57 EUR zu zahlen.') in text
    assert '1 Tagen' not in text


def test_the_percentage_is_printed_with_a_comma_and_without_trailing_zeros(pdf_db):
    iid = make_case(pdf_db, 'std19')
    _set(pdf_db, iid, SkontoDays=10, SkontoPercent=2.5)

    text = _pdf_text(pdf_db, iid)

    assert 'gewähren wir 2,5 % Skonto: abzüglich 4,45 EUR sind dann 173,46 EUR zu zahlen.' in text   # 4,44775 → 4,45


def test_the_last_sentence_is_left_out_without_a_due_date(pdf_db):
    iid = make_case(pdf_db, 'skonto')
    _set(pdf_db, iid, PaymentDueDate=None)

    text = _pdf_text(pdf_db, iid)

    assert 'abzüglich 3,56 EUR sind dann 174,35 EUR zu zahlen.' in text
    assert 'Ohne Abzug zahlbar bis zum' not in text


def test_the_last_sentence_is_left_out_when_the_period_ends_after_the_due_date(pdf_db):
    """Ein Altbestand kann eine Skontofrist nach der Fälligkeit tragen: Das Dokument widerspricht sich nicht."""
    iid = make_case(pdf_db, 'skonto')
    _set(pdf_db, iid, SkontoDays=30)                       # bis 03.04.2026, fällig 18.03.2026

    text = _pdf_text(pdf_db, iid)

    assert ('Bei Zahlung bis zum 03.04.2026 (innerhalb von 30 Tagen ab Rechnungsdatum) gewähren wir 2 % Skonto: '
            'abzüglich 3,56 EUR sind dann 174,35 EUR zu zahlen.') in text
    assert 'Ohne Abzug zahlbar bis zum' not in text


@pytest.mark.parametrize('factor', [-1, 0], ids=['credit', 'zero'])
def test_a_document_of_no_positive_amount_prints_no_skonto_paragraph_and_names_no_gap(pdf_db, factor):
    iid = make_case(pdf_db, 'skonto')
    with pdf_db._get_connection() as connection:
        connection.execute('UPDATE InvoiceItems SET TotalNet = TotalNet * ? WHERE InvoiceId = ?', (factor, iid))

    text = _pdf_text(pdf_db, iid)

    assert 'Bei Zahlung bis zum' not in text and 'Skonto' not in text
    assert not [gap for gap in einvoice_gaps(_model(pdf_db, iid)) if 'Skonto' in gap.text]


def test_the_skonto_is_taken_from_the_printed_gross_amount(pdf_db):
    iid = make_case(pdf_db, 'skonto')

    text = _pdf_text(pdf_db, iid)

    assert '177,91 EUR' in text and 'abzüglich 3,56 EUR' in text


def test_the_default_payment_text_does_not_contradict_the_skonto(pdf_db):
    with_skonto = make_case(pdf_db, 'skonto')
    _set(pdf_db, with_skonto, PaymentTerms='')
    without = make_case(pdf_db, 'std19')
    _set(pdf_db, without, PaymentTerms='', InvoiceNumber='G26099')

    text = _pdf_text(pdf_db, with_skonto)
    assert DEFAULT_WITH_SKONTO in text and 'ohne jeden Abzug' not in text and SENTENCE in text
    assert DEFAULT_WITHOUT_SKONTO in _pdf_text(pdf_db, without)             # ohne Skonto bleibt der alte Standardtext


def test_the_skonto_paragraph_and_the_note_for_small_businesses_both_stand_in_this_order(pdf_db):
    """Summenblock, Hinweis auf § 19 UStG, Zahlungshinweis, Skonto-Absatz – von oben nach unten."""
    iid = make_case(pdf_db, 'ku')
    _set(pdf_db, iid, SkontoDays=7, SkontoPercent=2)
    pdf, _ = generate_invoice_pdf(pdf_db, iid)

    with pdfplumber.open(io.BytesIO(pdf)) as document:
        words = document.pages[0].extract_words()

    def top(text):
        return next(word['top'] for word in words if word['text'] == text)

    assert top('Gesamtbetrag:') < top('Kleinunternehmer') < top('Zahlbar') < top('Skonto:')
    text = _pdf_text(pdf_db, iid)
    assert 'Es gilt die Steuerbefreiung für Kleinunternehmer nach § 19 UStG.' in text
    assert 'abzüglich 2,00 EUR sind dann 98,00 EUR zu zahlen.' in text


def test_a_skonto_that_cannot_be_read_prints_no_paragraph(pdf_db):
    iid = make_case(pdf_db, 'skonto')
    _set(pdf_db, iid, SkontoPercent=2.333)

    text = _pdf_text(pdf_db, iid)

    assert 'Skonto' not in text
    gaps = einvoice_gaps(_model(pdf_db, iid))
    assert 'Skonto (die gespeicherten Werte sind nicht lesbar)' in [gap.text for gap in gaps]


# ── CII und UBL ──────────────────────────────────────────────────────────────

def test_the_ubl_note_starts_with_the_skonto_line_and_keeps_its_linefeed(tmp_db):
    iid = make_case(tmp_db, 'skonto')

    note = _ubl(tmp_db, iid).find('cac:PaymentTerms/cbc:Note', {'cac': CAC, 'cbc': CBC}).text

    assert note == '#SKONTO#TAGE=7#PROZENT=2.00#\nZahlbar innerhalb von 14 Tagen.'
    assert len(_ubl(tmp_db, iid).findall('cac:PaymentTerms', {'cac': CAC})) == 1


def test_the_cii_description_starts_with_the_skonto_line_and_keeps_its_linefeed(tmp_db):
    iid = make_case(tmp_db, 'skonto')

    description = _cii(tmp_db, iid).find('.//ram:SpecifiedTradePaymentTerms/ram:Description', NS).text

    assert description == '#SKONTO#TAGE=7#PROZENT=2.00#\nZahlbar innerhalb von 14 Tagen.'


def test_without_a_free_text_only_the_skonto_line_and_its_linefeed_remain(tmp_db):
    iid = make_case(tmp_db, 'skonto')
    _set(tmp_db, iid, PaymentTerms='')

    assert _cii(tmp_db, iid).find('.//ram:SpecifiedTradePaymentTerms/ram:Description', NS).text == \
        '#SKONTO#TAGE=7#PROZENT=2.00#\n'
    assert _ubl(tmp_db, iid).find('cac:PaymentTerms/cbc:Note', {'cac': CAC, 'cbc': CBC}).text == \
        '#SKONTO#TAGE=7#PROZENT=2.00#\n'


def test_the_percentage_in_the_line_has_two_decimals_and_a_point(tmp_db):
    iid = make_case(tmp_db, 'std19')
    _set(tmp_db, iid, SkontoDays=14, SkontoPercent=2.5)

    assert payment_terms_text(_model(tmp_db, iid)).startswith('#SKONTO#TAGE=14#PROZENT=2.50#\n')


def test_without_skonto_the_free_text_stands_alone_as_before(tmp_db):
    iid = make_case(tmp_db, 'std19')

    assert payment_terms_text(_model(tmp_db, iid)) == 'Zahlbar innerhalb von 14 Tagen.'
    description = _cii(tmp_db, iid).find('.//ram:SpecifiedTradePaymentTerms/ram:Description', NS).text
    assert description == 'Zahlbar innerhalb von 14 Tagen.'


def test_a_credit_note_never_carries_a_skonto(tmp_db):
    iid = make_case(tmp_db, 'skonto')
    _set(tmp_db, iid, InvoiceKind='storno')                 # auch ein Altwert in der Zeile ändert daran nichts

    model = _model(tmp_db, iid)

    assert model.skonto is None and model.skonto_unreadable is False
    assert payment_terms_text(model) == 'Zahlbar innerhalb von 14 Tagen.'


# ── Die Frist rechnet ab dem eigenen Rechnungsdatum ───────────────────────────

def test_a_replacement_invoice_counts_the_skonto_period_from_its_own_date(world, monkeypatch):    # noqa: F811
    original = sent(world, 'T-100')                         # Rechnungsdatum 2026-06-07, 2 % bei Zahlung in 7 Tagen
    cancel(world, original, '2026-07-02')
    at(monkeypatch, '2026-11-12')

    draft = world.db.create_replacement_draft(original, USER)

    assert draft.ok is True
    model = _model(world.db, draft.invoice_id)
    assert model.issue_date == datetime.date(2026, 11, 12)
    assert (model.skonto.days, model.skonto.percent) == (7, Decimal('2.00'))
    assert model.skonto.until == datetime.date(2026, 11, 19)         # nicht der 14.06., der Tag der stornierten Rechnung


def test_a_corrected_version_of_an_older_invoice_carries_the_skonto_for_the_first_time(world):          # noqa: F811
    """Skonto und Rechnungsdatum sind bei einer versendeten Rechnung gesperrt – die neue Fassung rechnet also mit
    denselben Werten, trägt Absatz und Zeile aber auch dann, wenn die erste Fassung (vor 2.3.5) sie nicht trug."""
    from export.invoice_files import stored_invoice_path
    from tests.invoice_world import save_open
    invoice_id = sent(world, 'T-100')
    old_pdf = stored_invoice_path(world.db, invoice_id, 'pdf')[0]
    with open(old_pdf, 'wb') as f:
        f.write(b'%PDF-1.4 Fassung 1 ohne Skonto')                   # so sah das Dokument bis 2.3.4 aus (Platzhalter)

    answer = save_open(world, invoice_id, paymentTerms='Bitte überweisen Sie den Betrag auf das Konto unten.')

    assert answer == {'success': True, 'invoice_id': invoice_id, 'documentVersion': 2}
    new_pdf = stored_invoice_path(world.db, invoice_id, 'pdf')[0]
    with open(new_pdf, 'rb') as f:
        pdf = f.read()
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        text = ' '.join(' '.join((page.extract_text() or '').split()) for page in document.pages)
    assert ('Bei Zahlung bis zum 14.06.2026 (innerhalb von 7 Tagen ab Rechnungsdatum) gewähren wir 2 % Skonto: abzüglich '
            '2,38 EUR sind dann 116,62 EUR zu zahlen. Ohne Abzug zahlbar bis zum 21.06.2026.') in text
    assert _cii_description(pdf).startswith('#SKONTO#TAGE=7#PROZENT=2.00#\nBitte überweisen Sie den Betrag')


def _cii_description(pdf):
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import resolve1, stream_value
    document = PDFDocument(PDFParser(io.BytesIO(pdf)))
    spec = resolve1(resolve1(document.catalog['AF'])[0])
    root = ET.fromstring(stream_value(resolve1(spec['EF'])['F']).get_data())
    return root.find('.//ram:SpecifiedTradePaymentTerms/ram:Description', NS).text


# ── Speichern: unbrauchbare Angaben werden abgelehnt ──────────────────────────

PAIR = 'Skonto braucht Prozent und Tage.'
PERCENT = 'Skonto: Prozent größer 0 und kleiner 100, höchstens zwei Nachkommastellen.'
DAYS = 'Skonto: Tage als ganze Zahl von 1 bis 365.'
AFTER_DUE = 'Die Skontofrist endet nach der Fälligkeit.'
POSITIVE = 'Skonto nur bei einem positiven Rechnungsbetrag.'
INVOICE_DATE = 'Skonto braucht ein gültiges Rechnungsdatum.'


@pytest.mark.parametrize('percent, days, message', [
    (2, None, PAIR), (None, 7, PAIR), (2, 0, PAIR), (0, 7, PAIR),
    (2.333, 7, PERCENT), (100, 7, PERCENT), (150, 7, PERCENT), (-2, 7, PERCENT), ('zwei', 7, PERCENT),
    (2, 1.5, DAYS), (2, -3, DAYS), (2, 'sieben', DAYS), (2, 366, DAYS), (2, '366', DAYS), (2, '7.0', DAYS),
    (2, True, DAYS), (2, [7], DAYS), (2, {}, DAYS), ('2', '+7', DAYS),
    (True, 7, PERCENT), (['2'], 7, PERCENT), ('2,5', 7, PERCENT),
    (2, 15, AFTER_DUE),                                      # Rechnung 07.06., fällig 21.06. = 14 Tage
    (2, 365, AFTER_DUE),                                     # die Obergrenze der Tage ist erlaubt, die Fälligkeit zählt trotzdem
])
def test_an_unusable_skonto_is_refused_when_saving_and_nothing_is_written(world, percent, days, message):   # noqa: F811
    payload = make_payload(world, discountPercentage=percent, discountDays=days)

    answer = save(payload)

    assert answer == {'success': False, 'error': message}
    assert world.db.fetch_invoices() == []


@pytest.mark.parametrize('percent, days', [
    (2, 7), (2.5, 14), (0.5, 1), (99.99, 3),                # die Frist darf am Fälligkeitstag enden (14 Tage)
    (None, None),                                            # kein Skonto
])
def test_a_usable_skonto_or_none_is_saved(world, percent, days):                                         # noqa: F811
    answer = save(make_payload(world, discountPercentage=percent, discountDays=days))

    assert answer['success'] is True
    row = columns(world.db, answer['invoice_id'])
    assert (row['SkontoDays'], row['SkontoPercent']) == (days, percent)       # unverändert geschrieben


@pytest.mark.parametrize('percent, days', [(0, 0), ('', ''), (0, None), (None, 0)])
def test_empty_or_zero_values_mean_no_skonto_and_are_accepted(world, percent, days):                     # noqa: F811
    answer = save(make_payload(world, discountPercentage=percent, discountDays=days))

    assert answer['success'] is True


def test_the_skonto_of_a_form_without_a_due_date_is_not_compared_with_one(world):                       # noqa: F811
    answer = save(make_payload(world, dueDate=None, discountPercentage=2, discountDays=365))

    assert answer['success'] is True


def test_the_days_of_a_skonto_end_at_365_even_without_a_due_date(world):                               # noqa: F811
    answer = save(make_payload(world, dueDate=None, discountPercentage=2, discountDays=366))

    assert answer == {'success': False, 'error': DAYS}
    assert world.db.fetch_invoices() == []


# Werte, die ein Client schicken kann (``json.dumps`` schreibt ``Infinity``/``NaN``, ``json.loads`` liest sie): Sie kehren in
# Millisekunden mit der Ablehnung zurück. ``int()`` auf den Text ``1e9999999`` (über ``Decimal``) rechnete minutenlang.
@pytest.mark.parametrize('days', ['1e9999999', '1e400', 3650000, 10 ** 15, float('inf'), float('-inf'), float('nan'), '9' * 5000])
def test_hostile_days_are_refused_at_once(world, days):                                                 # noqa: F811
    started = time.perf_counter()
    answer = save(make_payload(world, discountDays=days))
    elapsed = time.perf_counter() - started

    assert answer == {'success': False, 'error': DAYS}
    assert elapsed < 1.0
    assert world.db.fetch_invoices() == []


@pytest.mark.parametrize('percent', ['1e9999999', '1e400', 10 ** 15, float('inf'), float('nan'), '9' * 5000])
def test_hostile_percentages_are_refused_at_once(world, percent):                                       # noqa: F811
    started = time.perf_counter()
    answer = save(make_payload(world, discountPercentage=percent))
    elapsed = time.perf_counter() - started

    assert answer == {'success': False, 'error': PERCENT}
    assert elapsed < 1.0


@pytest.mark.parametrize('field, error', [('discountDays', DAYS), ('discountPercentage', PERCENT)])
def test_a_huge_number_in_the_json_text_is_refused_at_once(world, field, error):                       # noqa: F811
    """``1e9999999`` als Zahl im JSON (nicht als Text): ``json.loads`` liest sie als ``inf``."""
    body = json.dumps(make_payload(world, **{field: '@@'})).replace('"@@"', '1e9999999').encode()
    started = time.perf_counter()
    answer = json.loads(handlers.handle_invoice_save(body))

    assert answer == {'success': False, 'error': error}
    assert time.perf_counter() - started < 1.0


# ── Nur bei einem positiven Betrag, nur mit einem Rechnungsdatum, aus dem sich eine Frist rechnen lässt ──

CREDIT_ITEM = {'position': 1, 'quantity': -1, 'unit': 'C62', 'description': 'Gutschrift', 'unitPrice': 100.0, 'taxRate': 19}
FREE_ITEM = {'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Gratisprobe', 'unitPrice': 0, 'taxRate': 19}


@pytest.mark.parametrize('item', [CREDIT_ITEM, FREE_ITEM], ids=['credit', 'zero'])
def test_a_skonto_is_refused_for_an_amount_of_no_more_than_zero(world, item):                          # noqa: F811
    answer = save(make_payload(world, items=[dict(item)]))                       # Formular mit 2 % in 7 Tagen

    assert answer == {'success': False, 'error': POSITIVE}
    assert world.db.fetch_invoices() == []


@pytest.mark.parametrize('item', [CREDIT_ITEM, FREE_ITEM], ids=['credit', 'zero'])
@pytest.mark.parametrize('percent, days', [(None, None), (0, 0), ('', '')])
def test_a_document_of_no_positive_amount_is_saved_without_skonto(world, item, percent, days):         # noqa: F811
    answer = save(make_payload(world, items=[dict(item)], discountPercentage=percent, discountDays=days))

    assert answer['success'] is True


@pytest.mark.parametrize('invoice_date', ['übermorgen', '07.06.2026', '2026-13-45', '2026/06/07'])
def test_a_skonto_needs_an_invoice_date_to_count_the_period_from(world, invoice_date):                 # noqa: F811
    answer = save(make_payload(world, invoiceDate=invoice_date, dueDate=None))

    assert answer == {'success': False, 'error': INVOICE_DATE}
    assert world.db.fetch_invoices() == []


@pytest.mark.parametrize('due_date', [None, '9999-12-31'])
def test_a_period_beyond_the_calendar_is_refused_not_a_server_error(world, due_date):                  # noqa: F811
    answer = save(make_payload(world, invoiceDate='9999-12-30', dueDate=due_date, discountDays=7, discountPercentage=2))

    assert answer == {'success': False, 'error': INVOICE_DATE}
    assert world.db.fetch_invoices() == []


def test_the_period_is_counted_even_without_a_due_date(world):                                          # noqa: F811
    answer = save(make_payload(world, invoiceDate='2026-12-30', dueDate=None, discountDays=14, discountPercentage=2))

    assert answer['success'] is True


def test_a_draft_is_checked_again_on_every_save(world):                                                  # noqa: F811
    invoice_id = make_invoice(world, 'draft')

    refused = save(make_payload(world, invoiceId=invoice_id, discountPercentage=2.333, discountDays=7))

    assert refused == {'success': False, 'error': PERCENT}
    assert columns(world.db, invoice_id)['SkontoPercent'] == 2


# ── Eine eingefrorene Rechnung mit Altwert wird nicht geprüft ──────────────────

@pytest.mark.parametrize('status', ['sent', 'overdue', 'paid'])
def test_a_sent_invoice_with_an_old_unusable_skonto_is_not_checked(world, status):                      # noqa: F811
    invoice_id = make_invoice(world, status)
    run_sql(world.db, 'UPDATE Invoices SET SkontoPercent = 2.333 WHERE ID = ?', invoice_id)    # Altbestand, SQL-Konsole

    # das Formular schickt den gespeicherten Wert unverändert zurück und ändert nur den Zahlungshinweis
    answer = save(make_payload(world, invoiceId=invoice_id, documentVersion=1, discountPercentage=2.333,
                               discountDays=7, paymentTerms='Zahlbar sofort'))

    assert answer == {'success': True, 'invoice_id': invoice_id, 'documentVersion': 2}
    row = columns(world.db, invoice_id)
    assert row['SkontoPercent'] == 2.333 and row['PaymentTerms'] == 'Zahlbar sofort'


def test_a_changed_skonto_of_a_sent_invoice_is_refused_by_the_lock_not_by_the_check(world):             # noqa: F811
    invoice_id = make_invoice(world, 'sent')

    refused = save_raw(make_payload(world, invoiceId=invoice_id, documentVersion=1, discountPercentage=2.333,
                                    discountDays=7))

    answer = json.loads(refused)
    assert refused.status == 409 and answer['success'] is False
    assert answer['error'].startswith('Versendete Rechnung: Beträge, Positionen') and PERCENT not in answer['error']


# ── Die Maske ────────────────────────────────────────────────────────────────

def test_the_form_warns_when_the_payment_text_says_without_deduction(world):                              # noqa: F811
    invoice_id = make_invoice(world, 'draft')

    html = PageInvoice(world.db, {}, invoice_id)

    assert '<small id="skonto_hint" class="warnColor" hidden></small>' in html
    assert 'function updateSkontoHint()' in html
    assert ('Der Zahlungshinweis sagt „ohne Abzug“ – bitte zum Skonto passend ändern.') in html
    assert "'ohne jeden abzug'" in html and "'ohne abzug'" in html
    assert "['discount_percentage', 'discount_days', 'payment_terms'].forEach(" in html
    assert "addEventListener('input', updateSkontoHint)" in html


def test_the_form_does_not_offer_the_payment_means_fields_any_more(world):                               # noqa: F811
    """Die Zahlungsart (Code und Text) hatte nie eine Spalte: Der Server las sie nicht, das XML sagt fest 58."""
    html = PageInvoice(world.db, {}, make_invoice(world, 'draft'))
    new_form = PageInvoice(world.db, {}, None)

    for page in (html, new_form):
        assert 'payment_means_code' not in page and 'payment_means_text' not in page
        assert 'paymentMeans' not in page and 'Zahlungsart' not in page
