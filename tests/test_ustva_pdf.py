# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""PDF der Umsatzsteuer-Voranmeldung (2.10.5): A4 hoch, mehrseitig, nichts auf der Platte.

* Hochformat (595 × 842), PDF/A-3b, Titel, Firma, Zeitraum, „Erstellt am“ und die Spaltenköpfe stehen im Text; ohne Namen entfällt die Firmenzeile
* die Kennzahlen 81, 86, 66 und 83, die Abstimmung, „Nicht zugeordnet“ und die festen Erläuterungen stehen mit den Werten der Seite da – die Werte der
  SuSa-Welt (von Hand gerechnet: ``test_ustva.py``) im Text, die Beträge rechtsbündig auf den Kanten ihrer Spalten (gemessen mit pdfplumber)
* ein langer Kontoname wird mit „…“ gekürzt, eine lange Bezeichnung eines Grundes umbrochen (die Anzahl am Ende bleibt)
* viele Konten laufen über mehrere Seiten: Kopf und Fuß auf jeder, „Seite n von m“, keine Zeile unter dem Rand; ein Block wird nicht geteilt, solange er auf
  eine Seite passt
* der Download (``GET /ustva/pdf``): Anhang mit ``X-Filename``, streng gelesene Adresse, kein Cookie, schreibt nichts, bei einem Fehler 500 mit festem Satz

Der Aufbau des Berichts und die Zahlen prüft ``test_ustva.py``. Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import datetime
import io
import re
from urllib.parse import quote

import pdfplumber
import pytest
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import stream_value

import auth
import userctx
from db import Database
from db.ustva import (UNASSIGNED_REASONS, UNBALANCED_LABEL, USTVA_DATE_TEXT, USTVA_FOOTER, USTVA_NOT_SHOWN_TEXT, USTVA_RECON_TEXT, USTVA_ROUNDING_TEXT,
                      USTVA_SIGN_TEXT, USTVA_TITLE, build_ustva)
from export import pdf_table, pdf_ustva
from export.pdf_ustva import (ACCOUNT_INDENT, BASE_RIGHT, BOTTOM_Y, COMPANY_Y, FOOTER_TOP_Y, FOOTER_Y, HEADER_Y, LEFT_X, LINES_PER_PAGE, NAME_WIDTH, NAME_X, PAGE_SIZE,
                              ROW_H, TABLE_TOP, TAX_RIGHT, WHOLE_RIGHT, generate_ustva_pdf, page_layout)
from tests import susa_helpers as helpers
from tests.http_client import get
from tests.installation import dump, tree
from tests.liquidity_helpers import two_banks
from tests.susa_helpers import mask
from tests.test_pdfa import _doc, _xmp
from tests.test_ustva import entry, own_company

CREATED = datetime.date(2026, 11, 3)            # „Erstellt am“: ein fester Tag, nie die Uhr
TM = re.compile(r'1 0 0 1 ([0-9.]+) (-?[0-9.]+) Tm \((.*)\) Tj')
BRANDS = ('DATEV', 'WISO', 'ELSTER')


@pytest.fixture(scope='module')
def susa_db(tmp_path_factory):
    """Die SuSa-Welt (``tests/susa_helpers.py``), einmal gebaut; die Datenwurzel liegt unter ``tmp_path_factory`` – nichts landet im echten data/."""
    root = tmp_path_factory.mktemp('ustva_pdf_world')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, 'DATA_ROOT', str(root / 'data'))
        patch.setattr(auth, 'AUTH_DB', str(root / 'data' / 'auth.db'))
        userctx._mode_cache.clear()
        db = helpers.build_susa_world(Database(str(root / 'susa.db'))).db
    userctx._mode_cache.clear()
    return db


# ── Lesen des PDF ─────────────────────────────────────────────────────────────
def _streams(pdf):
    """Der Content-Stream jeder Seite als Text."""
    return [b''.join(stream_value(part).get_data() for part in page.contents).decode('cp1252') for page in PDFPage.create_pages(_doc(pdf))]


def _cells(stream):
    """Alle Textzellen eines Streams: ``(x, y, Text)`` – der Text ohne die Maskierung der Klammern und Rückstriche."""
    return [(float(x), float(y), re.sub(r'\\([()\\])', r'\1', text)) for x, y, text in TM.findall(stream)]


def _texts(stream):
    return [text for _, _, text in _cells(stream)]


def _lines(pdf):
    """Der Text jeder Seite als Zeilen (pdfplumber, wie ihn ein Leser sieht)."""
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        return [page.extract_text().split('\n') for page in document.pages]


def _words(pdf):
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        return [page.extract_words() for page in document.pages]


def _report(items, names=None, euer=(0, 0), unbooked=(0, 0), year=2026, month=3, kind='monat', unbalanced=(0, 0)):
    return build_ustva(year, month, kind, items, names or {}, *euer, unbooked, unbalanced)


def _rows(pdf, page=0):
    """Die Zeilen der Tabelle einer Seite: ``(y, x, Text)`` der Bezeichnungen und Kennzahlen (links), von oben nach unten."""
    labels = [(y, x, text) for x, y, text in _cells(_streams(pdf)[page]) if BOTTOM_Y <= y <= TABLE_TOP and x in (LEFT_X, NAME_X, NAME_X + ACCOUNT_INDENT)]
    return sorted(labels, key=lambda label: -label[0])


# ── Geometrie ─────────────────────────────────────────────────────────────────
def test_the_lines_per_page_end_above_the_margin():
    assert LINES_PER_PAGE == 56
    assert TABLE_TOP - (LINES_PER_PAGE - 1) * ROW_H >= BOTTOM_Y > TABLE_TOP - LINES_PER_PAGE * ROW_H and PAGE_SIZE == (595, 842)
    assert FOOTER_Y < FOOTER_TOP_Y < BOTTOM_Y < TABLE_TOP < HEADER_Y < COMPANY_Y


def test_the_columns_end_on_the_right_margin_and_the_names_leave_room_for_the_amounts():
    assert LEFT_X < NAME_X < BASE_RIGHT < WHOLE_RIGHT < TAX_RIGHT == pdf_ustva.RIGHT_X == 545
    assert NAME_X + NAME_WIDTH == BASE_RIGHT - 80 and pdf_ustva.KZ_X == LEFT_X == 50


def test_page_layout_keeps_blocks_together():
    n = LINES_PER_PAGE
    assert page_layout([]) == [[]]
    assert page_layout([3, n - 2, 2]) == [[3], [n - 2, 2]]                  # der zweite Block passt nicht mehr: nächste Seite
    assert page_layout([3, n - 5, 2]) == [[3, n - 5, 2]]                    # passt genau
    assert page_layout([n]) == [[n]] and page_layout([n, 1]) == [[n], [1]]
    assert page_layout([3, 2 * n + 5, 2]) == [[3], [n], [n], [5, 2]]        # ein Block über eine Seite läuft über so viele, wie er braucht


# ── Eine Seite ────────────────────────────────────────────────────────────────
def test_the_pdf_is_a_pdfa_document_in_a4_portrait_with_title_company_and_period(susa_db):
    pdf = generate_ustva_pdf(susa_db.get_ustva(2026, 3, 'quartal'), 'Eigene Firma GmbH', CREATED)
    assert pdf.startswith(b'%PDF-')
    pages = list(PDFPage.create_pages(_doc(pdf)))
    assert [tuple(int(value) for value in page.mediabox) for page in pages] == [(0, 0) + PAGE_SIZE] == [(0, 0, 595, 842)]
    [stream] = _streams(pdf)
    texts = _texts(stream)
    assert USTVA_TITLE in texts and 'Eigene Firma GmbH' in texts and 'Erstellt am 03.11.2026' in texts
    assert 'Zeitraum: 01.01.2026 bis 31.03.2026 (1. Quartal 2026)' in texts
    assert [text for text in texts if text in ('Kz', 'Bezeichnung', 'Bemessungsgrundlage €', 'in vollen €', 'Steuer €')] == [
        'Kz', 'Bezeichnung', 'Bemessungsgrundlage €', 'in vollen €', 'Steuer €']
    assert not re.search(r'Seite \d+ von \d+', ' '.join(texts))               # eine Seite: keine Seitenzahl
    info = _doc(pdf).info[0]
    assert info['Title'] == USTVA_TITLE.encode('ascii') and info['Author'] == b'Eigene Firma GmbH'
    assert 'pdfaid' in _xmp(pdf) and b'/F1 ' in pdf and b'/F2 ' in pdf


def test_without_a_company_name_the_company_line_is_left_out(susa_db):
    report = susa_db.get_ustva(2026, 3, 'monat')
    [stream] = _streams(generate_ustva_pdf(report, '', CREATED))
    assert all(y != COMPANY_Y for _x, y, _text in _cells(stream))
    [stream] = _streams(generate_ustva_pdf(report, 'Eigene Firma GmbH', CREATED))
    assert any(y == COMPANY_Y for _x, y, _text in _cells(stream))


def test_the_pdf_shows_the_figures_the_reconciliation_and_no_item_of_a_quarter(susa_db):
    pdf = generate_ustva_pdf(susa_db.get_ustva(2026, 3, 'quartal'), 'Eigene Firma GmbH', CREATED)
    [lines] = _lines(pdf)
    assert lines[:3] == ['Umsatzsteuer-Voranmeldung', 'Eigene Firma GmbH', 'Zeitraum: 01.01.2026 bis 31.03.2026 (1. Quartal 2026) Erstellt am 03.11.2026']
    assert lines[3:17] == [
        'Kz Bezeichnung Bemessungsgrundlage € in vollen € Steuer €',
        '81 Steuerpflichtige Umsätze 19 % 400,00 400 76,00', '4400 Erlöse 19% USt 400,00 76,00',
        '86 Steuerpflichtige Umsätze 7 % 100,00 100 7,00', '4300 Erlöse 7% USt 100,00 7,00',
        'Umsatzsteuer (81 und 86) 83,00',
        '66 Vorsteuer aus Rechnungen anderer Unternehmer 21,57', '6805 Telefon 1,92', '6815 Bürobedarf 16,15', '6850 Sonstiger Betriebsbedarf 3,50',
        '83 Verbleibende Vorauszahlung (Überschuss mit Minus) 61,43',
        'Abstimmung', 'Steuer der Kennzahlen (81 + 86 - 66) 61,43', 'Umsatzsteuer abzüglich Vorsteuer laut EÜR 61,43']
    assert lines[17:21] == ['Abweichung 0,00', 'Nicht zugeordnet', 'Keine Posten.', 'Summe Nicht zugeordnet 0,00 0,00']
    text = ' '.join(lines)
    for fixed in (USTVA_DATE_TEXT, USTVA_ROUNDING_TEXT, USTVA_NOT_SHOWN_TEXT, USTVA_SIGN_TEXT, USTVA_RECON_TEXT, *USTVA_FOOTER):
        assert ' '.join(pdf_table.clean(fixed).split()) in re.sub(r'\s+', ' ', text), fixed
    for brand in BRANDS:
        assert brand.lower() not in text.lower(), brand


def test_the_amounts_stand_on_the_right_edge_of_their_column(susa_db):
    """Gemessen am fertigen PDF (pdfplumber): die Bemessungsgrundlage auf 405, die vollen Euro auf 470, die Steuer auf 545 pt; ein Betrag wird nur dort gedruckt, wo er
    hingehört (bei Kz 66 steht nur die Steuer)."""
    [words] = _words(generate_ustva_pdf(susa_db.get_ustva(2026, 3, 'quartal'), '', CREATED))

    def edges(text):
        return sorted(round(word['x1'], 1) for word in words if word['text'] == text)
    assert edges('400,00') == [405.0, 405.0] and edges('400') == [470.0] and edges('100') == [470.0] and edges('100,00') == [405.0, 405.0]
    assert edges('76,00') == [545.0, 545.0] and edges('7,00') == [545.0, 545.0] and edges('83,00') == [545.0]
    assert edges('61,43') == [545.0] * 3                                         # die Vorauszahlung und die beiden Seiten der Abstimmung
    assert edges('21,57') == [545.0] and edges('3,50') == [545.0] and edges('1,92') == [545.0] and edges('0,00') == [405.0, 545.0, 545.0]   # Abweichung, Summe Netto, Summe Steuer


def test_the_pdf_prints_the_unassigned_items_with_their_count_net_and_tax():
    items = [entry('purpose_first', 4400, 1800, '116.00', '16.00', 0.16), entry('purpose_first', 4185, 1800, '50.00'), entry('purpose_first', 4184, 1800, '40.00'),
             entry('purpose_first', 4400, 1800, '116.00', '16.00', 0.16, booking_id=2)]
    report = _report(items, {4400: 'Erlöse 19% USt', 4185: 'Erlöse Kleinunternehmer', 4184: 'Steuerfreie Erlöse'}, unbooked=(12_340_000, 2))
    [lines] = _lines(generate_ustva_pdf(report, '', CREATED))
    start = lines.index('Nicht zugeordnet')
    assert lines[start:start + 9] == [
        'Nicht zugeordnet', 'Erlöse ohne Steuer (0 %, steuerfrei, § 19 oder ohne Satz) (1) 40,00 0,00', '4184 Steuerfreie Erlöse (1) 40,00 0,00',
        'Erlöse zu einem anderen Steuersatz (2) 200,00 32,00', '4400 Erlöse 19% USt (2) 200,00 32,00',
        'Erlöse auf Konto 4185 (1) 50,00 0,00', '4185 Erlöse Kleinunternehmer (1) 50,00 0,00',
        'Bankbewegungen ohne Buchungssatz (2) 1.234,00',                                     # in der Netto-Spalte, ohne Steuer; sie zählt nicht in der Summe
        'Summe Nicht zugeordnet 290,00 32,00']
    assert report.unbooked_amount == 12_340_000


def test_the_pdf_prints_the_unbalanced_bank_movements_after_the_ones_without_an_entry_and_outside_the_sum():
    """Bankbewegungen, deren Buchungssätze nicht den Betrag ergeben: Anzahl am Ende der (umbrochenen) Bezeichnung, die Summe der Reste in der Netto-Spalte, ohne Steuer –
    hinter den Bankbewegungen ohne Satz und nicht in der Summe."""
    items = [entry('purpose_first', 4400, 1800, '116.00', '16.00', 0.16)]
    report = _report(items, {4400: 'Erlöse 19% USt'}, unbooked=(12_340_000, 2), unbalanced=(1_190_000, 3))
    [lines] = _lines(generate_ustva_pdf(report, '', CREATED))
    start = lines.index('Nicht zugeordnet')
    assert lines[start:start + 7] == [
        'Nicht zugeordnet', 'Erlöse zu einem anderen Steuersatz (1) 100,00 16,00', '4400 Erlöse 19% USt (1) 100,00 16,00',
        'Bankbewegungen ohne Buchungssatz (2) 1.234,00', 'Bankbewegungen, deren Buchungssätze nicht den Betrag ergeben 119,00', '(3)',
        'Summe Nicht zugeordnet 100,00 16,00']
    assert UNBALANCED_LABEL == 'Bankbewegungen, deren Buchungssätze nicht den Betrag ergeben' and report.unbalanced_amount == 1_190_000
    [words] = _words(generate_ustva_pdf(report, '', CREATED))
    assert sorted(round(word['x1'], 1) for word in words if word['text'] == '119,00') == [BASE_RIGHT]


def test_a_bank_movement_that_does_not_add_up_alone_is_no_empty_list():
    """Auch wenn sonst nichts unter „Nicht zugeordnet“ steht, ist die Liste nicht leer: Die Zeile steht da, „Keine Posten.“ nicht."""
    [lines] = _lines(generate_ustva_pdf(_report([], unbalanced=(1_190_000, 1)), '', CREATED))
    start = lines.index('Nicht zugeordnet')
    assert lines[start:start + 4] == ['Nicht zugeordnet', 'Bankbewegungen, deren Buchungssätze nicht den Betrag ergeben 119,00', '(1)', 'Summe Nicht zugeordnet 0,00 0,00']
    assert 'Keine Posten.' not in lines


def test_the_reconciliation_shows_a_dash_where_the_euer_gives_no_whole_number():
    """Eine Kommazahl als Steuerbetrag im Bestand: ``recon_euer`` und ``recon_difference`` sind ``None`` – das PDF druckt „–“ (rechtsbündig auf der Kante der Steuer), die
    Steuer der Kennzahlen steht da."""
    items = [entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19)]
    pdf = generate_ustva_pdf(_report(items, euer=(190000.5, 0)), '', CREATED)
    [lines] = _lines(pdf)
    start = lines.index('Abstimmung')
    assert lines[start:start + 4] == ['Abstimmung', 'Steuer der Kennzahlen (81 + 86 - 66) 19,00', 'Umsatzsteuer abzüglich Vorsteuer laut EÜR –', 'Abweichung –']
    [words] = _words(pdf)
    assert [round(word['x1'], 1) for word in words if word['text'] == '–'] == [TAX_RIGHT, TAX_RIGHT]


def test_a_long_reason_is_wrapped_and_keeps_its_count():
    """„Satz mit anderem Datum als seine Bankbewegung (anderer Zeitraum)“ passt mit der Anzahl nicht in die Spalte: Sie läuft in die nächste Zeile, die Anzahl bleibt am
    Ende (die Werte stehen in der ersten Zeile)."""
    items = [entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19, source='other_date', booking_id=index) for index in range(12)]
    [lines] = _lines(generate_ustva_pdf(_report(items, {4400: 'Erlöse 19% USt'}), '', CREATED))
    start = lines.index('Nicht zugeordnet')
    assert lines[start:start + 5] == ['Nicht zugeordnet', 'Satz mit anderem Datum als seine Bankbewegung (anderer 1.200,00 228,00', 'Zeitraum) (12)',
                                      '4400 Erlöse 19% USt (12) 1.200,00 228,00', 'Summe Nicht zugeordnet 1.200,00 228,00']


def test_a_long_account_name_is_cut_with_an_ellipsis_before_the_amounts():
    items = [entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19)]
    pdf = generate_ustva_pdf(_report(items, {4400: 'X' * 300}), '', CREATED)
    [words] = _words(pdf)
    name = [word for word in words if word['text'].startswith(('4400', 'XXX')) and word['x0'] < BASE_RIGHT - 80]
    assert len(name) == 2 and name[1]['text'].endswith('…') and name[0]['x0'] == NAME_X + ACCOUNT_INDENT
    assert name[1]['x1'] <= NAME_X + NAME_WIDTH == BASE_RIGHT - 80                                  # vor dem Platz der Beträge


def test_special_characters_in_names_give_a_valid_text():
    items = [entry('purpose_first', 4400, 1800, '119.00', '19.00', 0.19)]
    for name in ('Müller (Söhne) \\ Co.', 'Zeile\nZwei\tTab', '<script>alert(1)</script>', '€ „Zitat“ – Strich', ''):
        pdf = generate_ustva_pdf(_report(items, {4400: name}), name or 'Firma', CREATED)
        assert pdf.startswith(b'%PDF-') and _streams(pdf)
    assert _doc(generate_ustva_pdf(_report(items), 'Muster (Test) GmbH \\ Co. KG', CREATED)).info[0]['Author'] == b'Muster (Test) GmbH \\ Co. KG'


def test_every_page_has_head_and_foot_and_many_accounts_run_over_several_pages():
    items = [entry('purpose_first', 4000 + number, 1800, '119.00', '19.00', 0.19, booking_id=number) for number in range(150)]
    pdf = generate_ustva_pdf(_report(items), 'Eigene Firma GmbH', CREATED)
    streams = _streams(pdf)
    assert len(streams) == 4                                                  # der Block 81 (151 Zeilen) läuft über drei Seiten, der Rest braucht eine vierte
    rows = []
    for number, stream in enumerate(streams, start=1):
        texts = _texts(stream)
        assert USTVA_TITLE in texts and 'Eigene Firma GmbH' in texts and 'Kz' in texts and f'Seite {number} von 4' in texts
        assert USTVA_FOOTER[0] in texts and USTVA_FOOTER[1] in texts
        table = {y for _x, y, _text in _cells(stream) if BOTTOM_Y <= y <= TABLE_TOP}
        assert min(table) >= BOTTOM_Y and max(table) <= TABLE_TOP and len(table) <= LINES_PER_PAGE
        rows.append(len(table))
    assert rows[:3] == [LINES_PER_PAGE, LINES_PER_PAGE, 50]                   # 56 + 56 + (41 Zeilen des Blocks 81, dazu 66/83, die Abstimmung, „Nicht zugeordnet“)
    assert 'Summe Nicht zugeordnet' in _texts(streams[2]) and 'Summe Nicht zugeordnet' not in _texts(streams[3])
    assert [text for text in _texts(streams[3]) if text.startswith(USTVA_DATE_TEXT[:20])] and 'Seite 4 von 4' in _texts(streams[3])


def test_a_block_that_fits_on_a_page_is_not_split():
    """Der Block „Nicht zugeordnet“ beginnt auf der nächsten Seite, wenn er nicht mehr auf die Restseite passt – er steht ganz auf einer Seite."""
    items = [entry('purpose_first', 4000 + number, 1800, '119.00', '19.00', 0.19, booking_id=number) for number in range(44)]
    items += [entry('purpose_first', 4400, 1800, '116.00', '16.00', 0.16, booking_id=1000 + number) for number in range(3)]
    pdf = generate_ustva_pdf(_report(items), '', CREATED)
    pages = [[text for _y, _x, text in _rows(pdf, index)] for index in range(len(_streams(pdf)))]
    holder = [index for index, labels in enumerate(pages) if 'Nicht zugeordnet' in labels]
    assert len(holder) == 1 and 'Summe Nicht zugeordnet' in pages[holder[0]]
    assert 'Abstimmung' in pages[0] or 'Abstimmung' in pages[1]


# ── Download ──────────────────────────────────────────────────────────────────
def _books(db):
    own_company(db)
    two_banks(db)
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '19.00')
    mask(db, 'Probe-Giro', 6815, '-59.50', '2026-03-12', '19', '-9.50')
    mask(db, 'Probe-Giro', 4300, '107.00', '2026-05-12', '7', '7.00')


def test_the_pdf_route_answers_with_a_download_and_writes_nothing(server_port, auto_login):
    db = auto_login.db
    _books(db)
    before_db, before_tree = dump(db.db_name), tree(userctx.DATA_ROOT)

    month = get(server_port, '/ustva/pdf?year=2026&month=3')
    assert month.status == 200 and month.headers['Content-Type'] == 'application/pdf' and month.headers['Content-Disposition'].startswith('attachment;')
    assert month.headers['X-Filename'] == quote('USt-Voranmeldung_2026-03.pdf', safe='') and month.body.startswith(b'%PDF-')
    assert month.headers.get_all('Cache-Control') == ['no-cache'] and month.headers.get('Set-Cookie') is None
    assert 'USt-Voranmeldung_2026-03.pdf' in month.headers['Content-Disposition']
    explicit = get(server_port, '/ustva/pdf?year=2026&month=3&art=monat')
    assert explicit.status == 200 and _lines(explicit.body) == _lines(month.body)                       # ``art`` fehlt = Monat
    quarter = get(server_port, '/ustva/pdf?year=2026&month=3&art=quartal')
    assert quarter.status == 200 and quarter.headers['X-Filename'] == quote('USt-Voranmeldung_2026-Q1.pdf', safe='') and 'USt-Voranmeldung_2026-Q1.pdf' in quarter.headers['Content-Disposition']
    second = get(server_port, '/ustva/pdf?year=2026&month=6&art=quartal')
    assert second.headers['X-Filename'] == quote('USt-Voranmeldung_2026-Q2.pdf', safe='')

    [month_lines] = _lines(month.body)
    assert 'Eigene Firma GmbH' in month_lines and 'Zeitraum: 01.03.2026 bis 31.03.2026 (März 2026) Erstellt am ' in ' '.join(month_lines)
    assert '81 Steuerpflichtige Umsätze 19 % 100,00 100 19,00' in month_lines and '86 Steuerpflichtige Umsätze 7 % 0,00 0 0,00' in month_lines
    [second_lines] = _lines(second.body)
    assert '86 Steuerpflichtige Umsätze 7 % 100,00 100 7,00' in second_lines and '81 Steuerpflichtige Umsätze 19 % 0,00 0 0,00' in second_lines

    assert dump(db.db_name) == before_db and tree(userctx.DATA_ROOT) == before_tree          # nichts geschrieben, auch keine PDF-Datei


@pytest.mark.parametrize('query', [
    '', 'year=2026', 'month=3', 'year=2026&month=13', 'year=2026&month=0', 'year=2026&year=2025&month=3', 'year=2026&month=3&month=4', 'year=02026&month=3',
    'year=2026&month=3&art=x', 'year=2026&month=3&art=monat&art=monat', 'year=2026&month=3&art=Quartal',
    'year=2026&month=2&art=quartal', 'year=2026&month=1&art=quartal', 'year=2026&month=11&art=quartal', 'from=2026-03-01&to=2026-03-31',
])
def test_the_pdf_route_refuses_what_it_cannot_read_and_sets_no_cookie(server_port, auto_login, query):
    """Streng gelesen, nichts getauscht oder korrigiert: ``art=quartal`` nur mit dem Monat 3, 6, 9 oder 12; ein doppelter Schlüssel ist ungültig."""
    _books(auto_login.db)
    response = get(server_port, f'/ustva/pdf?{query}')
    assert response.status == 400 and response.text == 'Ungültiger Zeitraum.', query
    assert response.headers['Content-Type'] == 'text/plain; charset=utf-8' and response.headers.get('Set-Cookie') is None


def test_a_failing_pdf_answers_with_a_fixed_sentence_and_no_exception_text(server_port, auto_login, monkeypatch, capsys):
    """Ein Fehler beim Erzeugen steht nur im Server-Log: Die Antwort ist 500 mit festem Satz, ohne Ausnahmetext oder Pfad."""
    from server.pages_ustva import USTVA_PDF_FAILED_TEXT
    _books(auto_login.db)

    def failing(*args, **kwargs):
        raise RuntimeError('geheimer Pfad /data/users/admin')
    monkeypatch.setattr(pdf_ustva, 'generate_ustva_pdf', failing)
    response = get(server_port, '/ustva/pdf?year=2026&month=3')
    assert response.status == 500 and response.text == USTVA_PDF_FAILED_TEXT == 'Die Umsatzsteuer-Voranmeldung ließ sich nicht als PDF erstellen.'
    assert 'geheim' not in response.text and response.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert 'geheimer Pfad' in capsys.readouterr().err                                              # der volle Fehler steht im Server-Log


def test_every_reason_fits_on_a_line_or_is_wrapped_without_losing_a_word():
    """Auch jede Bezeichnung eines Grundes mit einer dreistelligen Anzahl: Nach dem Umbruch stehen alle Wörter da."""
    for label in UNASSIGNED_REASONS.values():
        lines = pdf_table.wrap(f'{label} (123)', NAME_WIDTH, pdf_ustva.ROW_SIZE)
        assert ' '.join(lines).split() == pdf_table.clean(f'{label} (123)').split() and lines[-1].endswith('(123)')                      # ``↔`` druckt die Schrift als ``<->``
        assert all(pdf_table.width(line, pdf_ustva.ROW_SIZE, '/F1') <= NAME_WIDTH for line in lines)
