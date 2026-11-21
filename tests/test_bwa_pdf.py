# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""PDF der betriebswirtschaftlichen Auswertung (2.5.14): A4 quer, mehrseitig, nichts auf der Platte.

* Querformat (842 × 595), Titel, Firma, Zeitraum und Hinweis stehen im Text; ohne Namen entfällt die Firmenzeile
* viele Konten laufen über mehrere Seiten: Kopf und Fuß auf jeder, „Seite n von m“, keine Zeile unter dem Rand; ein Block (eine Zeile
  mit ihren Konten) wird nicht geteilt, solange er auf eine Seite passt – nur ein Block über eine Seite läuft weiter
* ohne Kontennachweis stehen nur die Zeilen da
* Beträge liegen rechtsbündig auf den Kanten der Spalten (gemessen mit pdfplumber, nicht mit den Funktionen des PDF-Baus)
* Klammern, Rückstriche, Umlaute und Steuerzeichen in Namen ergeben einen gültigen Text; überlange Namen werden mit „…“ gekürzt
* der Download (``GET /bwa/pdf``): Anhang mit ``X-Filename``, schreibt nichts

Der Aufbau des Berichts und die Zahlen prüft ``test_bwa.py``. Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import datetime
import io
import re
from urllib.parse import quote

import pdfplumber
import pytest
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import stream_value

import userctx
from db.bwa import HINT_TEXT, OUTSIDE_HEADING, TITLE, visible
from export.pdf_bwa import (ACCOUNT_X, ACCOUNT_RESERVE, BOTTOM_Y, COLUMN_WIDTH, COMPANY_Y, FOOTER_Y, LABEL_RIGHT, LEFT_X,
                            LINES_PER_PAGE, MAX_LABEL_CHARS, PAGE_SIZE, RIGHT_X, ROW_H, TABLE_TOP, generate_bwa_pdf, page_layout)
from export.pdf_worktime import info_width
from tests.bwa_helpers import report_from, some_books
from tests.http_client import get
from tests.installation import dump, tree
from tests.test_pdfa import _doc

CREATED = datetime.date(2026, 11, 21)          # „Erstellt am“: ein fester Tag, nie die Uhr
TM = re.compile(r'1 0 0 1 ([0-9.]+) (-?[0-9.]+) Tm \((.*)\) Tj')
NUMBER = re.compile(r'-?\d{1,3}(?:\.\d{3})*,\d\d')
EDGES = [LABEL_RIGHT + COLUMN_WIDTH * (index + 1) for index in range(8)]


# ── Lesen des PDF ─────────────────────────────────────────────────────────────
def _streams(pdf):
    """Der Content-Stream jeder Seite als Text."""
    return [b''.join(stream_value(part).get_data() for part in page.contents).decode('cp1252')
            for page in PDFPage.create_pages(_doc(pdf))]


def _cells(stream):
    """Alle Textzellen eines Streams: ``(x, y, Text)`` – der Text ohne die Maskierung der Klammern und Rückstriche."""
    return [(float(x), float(y), re.sub(r'\\([()\\])', r'\1', text)) for x, y, text in TM.findall(stream)]


def _texts(stream):
    return [text for _, _, text in _cells(stream)]


def _table(cells):
    """Die Zeilen der Tabelle einer Seite: ``(y, x, Text)`` der Bezeichnungen (links), von oben nach unten."""
    labels = [(y, x, text) for x, y, text in cells if BOTTOM_Y <= y <= TABLE_TOP and x in (LEFT_X, ACCOUNT_X)]
    return sorted(labels, key=lambda label: -label[0])


def _rows(number, count, first=1, prefix='Konto'):
    """``count`` Aufwandskonten ab Nummer ``number + first`` mit einem Saldo (alle in derselben Gruppe, wenn ``number`` es ist)."""
    return [(number + index, f'{prefix} {number + index}', -(index + 1) * 1.5) for index in range(first, first + count)]


# Acht Gruppen mit je zwölf Konten: Jeder Block (eine Zeile und zwölf Konten) ist 13 Zeilen hoch und passt auf eine Seite
EIGHT_GROUPS = [row for base in (5000, 5900, 6000, 6300, 6400, 6500, 6600, 6700) for row in _rows(base, 12)]


# ── Seitenverteilung ──────────────────────────────────────────────────────────
def test_the_lines_per_page_end_above_the_margin():
    assert LINES_PER_PAGE == 40
    assert TABLE_TOP - (LINES_PER_PAGE - 1) * ROW_H >= BOTTOM_Y > TABLE_TOP - LINES_PER_PAGE * ROW_H


def test_page_layout_keeps_blocks_together():
    n = LINES_PER_PAGE
    assert page_layout([]) == [[]]
    assert page_layout([3, n - 2, 2]) == [[3], [n - 2, 2]]                  # der zweite Block passt nicht mehr: nächste Seite
    assert page_layout([3, n - 5, 2]) == [[3, n - 5, 2]]                    # passt genau
    assert page_layout([n]) == [[n]] and page_layout([n, 1]) == [[n], [1]]
    assert page_layout([1] * (n + 1)) == [[1] * n, [1]]
    # Ein Block über eine Seite beginnt auf einer neuen Seite und läuft über so viele, wie er braucht
    assert page_layout([3, 2 * n + 5, 2]) == [[3], [n], [n], [5, 2]]
    assert page_layout([2 * n]) == [[n], [n]]
    assert page_layout([0, -4]) == [[1, 1]]                                 # eine Zeile ist mindestens eine Zeile hoch


def test_page_layout_never_exceeds_a_page_and_loses_no_row():
    heights = [1, 13, 40, 41, 7, 1, 120, 2, 39, 3, 80, 1]
    layout = page_layout(heights)
    assert all(sum(page) <= LINES_PER_PAGE for page in layout) and sum(map(sum, layout)) == sum(heights)
    assert all(page for page in layout)                                      # keine leere Seite mitten im Dokument


# ── Eine Seite ────────────────────────────────────────────────────────────────
ROWS = [(4400, 'Erlöse 19% USt', 1000.0), (3806, 'Umsatzsteuer 19%', 190.0), (6815, 'Bürobedarf', -100.0),
        (1406, 'Abziehbare Vorsteuer 19%', -19.0), (3820, 'Umsatzsteuer-Vorauszahlungen', -50.0),
        (1050, 'Altkonto', -5.0), (3160, 'Darlehen', -30.0)]


def test_the_pdf_is_landscape_and_names_the_company():
    pdf = generate_bwa_pdf(report_from(ROWS, variant='vorjahr'), True, 'Eigene Firma GmbH', CREATED)
    pages = list(PDFPage.create_pages(_doc(pdf)))
    assert [tuple(int(value) for value in page.mediabox) for page in pages] == [(0, 0) + PAGE_SIZE] == [(0, 0, 842, 595)]
    [stream] = _streams(pdf)
    texts = _texts(stream)
    assert TITLE in texts and 'Eigene Firma GmbH' in texts and HINT_TEXT in texts
    assert 'März 2026 · aufgelaufen Januar bis März 2026 · Vergleich mit dem Vorjahr' in texts
    assert 'Erstellt am 21.11.2026' in texts and 'Position' in texts
    assert ['März', 'Januar bis März'] == [text for text in texts if text in ('März', 'Januar bis März')]
    assert 'Seite' not in ' '.join(texts)                                    # eine Seite: keine Seitenzahl
    document = _doc(pdf)
    info = document.info[0]
    assert info['Title'] == b'Betriebswirtschaftliche Auswertung' and info['Author'] == b'Eigene Firma GmbH'
    for fragment in (b'/F1 ', b'/F2 '):
        assert fragment in pdf


def test_without_a_company_name_the_company_line_is_left_out():
    [stream] = _streams(generate_bwa_pdf(report_from(ROWS), True, '', CREATED))
    assert all(y != COMPANY_Y for _x, y, _text in _cells(stream))
    [stream] = _streams(generate_bwa_pdf(report_from(ROWS), True, 'Eigene Firma GmbH', CREATED))
    assert any(y == COMPANY_Y for _x, y, _text in _cells(stream))


def test_without_the_account_list_only_the_lines_are_printed():
    report = report_from(ROWS)
    with_accounts = _streams(generate_bwa_pdf(report, True, '', CREATED))
    without = _streams(generate_bwa_pdf(report, False, '', CREATED))
    assert len(with_accounts) == len(without) == 1
    labels = [text for _y, _x, text in _table(_cells(without[0]))]
    expected = [line.label for line in report.lines if visible(report, line)]
    expected.insert(expected.index('Ergebnis') + 1, OUTSIDE_HEADING)
    expected.insert(expected.index(OUTSIDE_HEADING) + 1, report.outside.label)
    assert labels == expected
    assert all(not re.match(r'\d{4} ', label) for label in labels)
    with_labels = [text for _y, _x, text in _table(_cells(with_accounts[0]))]
    assert {'4400 Erlöse 19% USt', '6815 Bürobedarf', '1050 Altkonto', '3160 Darlehen'} <= set(with_labels)
    assert [label for label in with_labels if not re.match(r'\d{4} ', label)] == expected      # dieselben Zeilen, dazu die Konten


def test_the_block_under_the_result_starts_with_its_heading_and_only_when_it_has_something():
    labels = [text for _y, _x, text in _table(_cells(_streams(generate_bwa_pdf(report_from(ROWS), True, '', CREATED))[0]))]
    assert labels[labels.index('Ergebnis') + 1:labels.index('Ergebnis') + 4] == [OUTSIDE_HEADING, 'Sonstige Ausgaben', '3160 Darlehen']
    quiet = [text for _y, _x, text in _table(_cells(_streams(generate_bwa_pdf(report_from(ROWS[:3]), True, '', CREATED))[0]))]
    assert OUTSIDE_HEADING not in quiet and 'Sonstige Ausgaben' not in quiet and 'Nicht zugeordnet (Ausgaben)' not in quiet


# ── Mehrere Seiten ────────────────────────────────────────────────────────────
def _page_cells(pdf):
    return [_cells(stream) for stream in _streams(pdf)]


def test_many_accounts_run_over_several_pages_and_nothing_lies_below_the_margin():
    pdf = generate_bwa_pdf(report_from(EIGHT_GROUPS), True, 'Eigene Firma GmbH', CREATED)
    pages = _page_cells(pdf)
    total = len(pages)
    assert total >= 3
    for number, cells in enumerate(pages, start=1):
        below = [(y, text) for _x, y, text in cells if y < BOTTOM_Y]
        assert sorted(text for _y, text in below) == sorted([HINT_TEXT, f'Seite {number} von {total}']), number
        assert {y for y, _text in below} == {FOOTER_Y}
        texts = [text for _x, _y, text in cells]
        assert TITLE in texts and 'Eigene Firma GmbH' in texts and 'Position' in texts          # Kopf auf jeder Seite
        assert 'März 2026' in texts and 'Januar bis März 2026' in texts
    # Jedes Konto steht genau einmal da, in der Reihenfolge der Tabelle
    printed = [text for cells in pages for _y, _x, text in _table(cells) if re.match(r'\d{4} ', text)]
    assert printed == [f'{number} Konto {number}' for number, _name, _total in EIGHT_GROUPS]


def test_no_block_is_split_where_it_fits_on_a_page():
    """Die erste Zeile jeder Folgeseite ist die Zeile einer Gruppe (links), nie ein einzelnes Konto (eingerückt): Jeder Block,
    der auf eine Seite passt, steht ganz auf einer."""
    pages = _page_cells(generate_bwa_pdf(report_from(EIGHT_GROUPS), True, '', CREATED))
    assert len(pages) >= 3
    for cells in pages:
        [(first_y, first_x, _text)] = _table(cells)[:1]
        assert first_y == TABLE_TOP and first_x == LEFT_X


def test_one_block_longer_than_a_page_runs_over_and_starts_on_a_new_page():
    rows = [(4400, 'Erlöse 19% USt', 1000.0)] + _rows(6900, 120)               # 120 Konten einer Gruppe: der Block ist 121 Zeilen hoch
    pages = _page_cells(generate_bwa_pdf(report_from(rows), True, '', CREATED))
    assert len(pages) >= 5
    first_rows = [_table(cells)[0] for cells in pages]
    assert first_rows[1][1] == LEFT_X and first_rows[1][2] == 'Zinsen und sonstige Aufwendungen'   # beginnt oben auf einer neuen Seite
    assert first_rows[2][1] == ACCOUNT_X and first_rows[3][1] == ACCOUNT_X                       # und läuft auf den nächsten weiter
    printed = [text for cells in pages for _y, _x, text in _table(cells) if re.match(r'(69|70)\d\d ', text)]
    assert len(printed) == 120 and printed[0].startswith('6901 ') and printed[-1].startswith('7020 ')
    for number, cells in enumerate(pages, start=1):
        assert min(y for _x, y, _text in cells) == FOOTER_Y, number
        assert not [text for _x, y, text in cells if y < BOTTOM_Y and text not in (HINT_TEXT, f'Seite {number} von {len(pages)}')]


def test_the_hint_fits_into_the_footer_line_next_to_the_page_number():
    assert info_width(HINT_TEXT, 7) + info_width('Seite 10 von 10', 7) + 20 < RIGHT_X - LEFT_X


# ── Zahlen und Namen ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('variant', ['jahr', 'vorjahr'])
def test_every_amount_ends_on_the_edge_of_its_column(variant):
    """Gemessen mit pdfplumber (den Breiten der eingebetteten Schrift im PDF), nicht mit den Funktionen des PDF-Baus."""
    rows = ROWS + [(6850, 'Sonstiger Betriebsbedarf', -1234567.89), (4300, 'Erlöse 7% USt', 98765432.1)]
    report = report_from(rows, variant=variant, prev_month_rows=ROWS, prev_ytd_rows=ROWS)
    pdf = generate_bwa_pdf(report, True, '', CREATED)
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        words = [word for page in document.pages for word in page.extract_words()]
    numbers = [word for word in words if NUMBER.fullmatch(word['text'])]
    assert len(numbers) > 100
    for word in numbers:
        assert min(abs(word['x1'] - edge) for edge in EDGES) < 0.6, (word['text'], word['x1'])
    assert any(word['text'] == '98.765.432,10' for word in numbers)
    assert any(word['text'] == '1.234.567,89' for word in numbers)                 # ein Aufwand steht positiv da


def test_a_name_with_brackets_a_backslash_and_umlauts_prints_unchanged():
    name = '(Test) Bürobedarf\\Ä – € ß'
    pdf = generate_bwa_pdf(report_from([(6815, name, -10.0)]), True, 'Muster (Test) GmbH \\ Co. KG', CREATED)
    [stream] = _streams(pdf)
    texts = _texts(stream)
    assert f'6815 {name}' in texts and 'Muster (Test) GmbH \\ Co. KG' in texts
    assert _doc(pdf).info[0]['Author'] == b'Muster (Test) GmbH \\ Co. KG'


def test_control_characters_in_a_name_do_not_break_the_stream():
    pdf = generate_bwa_pdf(report_from([(6815, 'Zeile eins\nZeile zwei\tEnde\r\x00\x07 Schluss', -10.0)]), True, '', CREATED)
    [stream] = _streams(pdf)
    assert '6815 Zeile eins Zeile zwei Ende Schluss' in _texts(stream)
    assert not re.search(r'[\x00-\x08\x0b-\x1f\x7f]', stream.replace('\n', ' '))


def test_an_account_without_a_name_prints_its_number_and_a_missing_name_never_prints_none():
    [stream] = _streams(generate_bwa_pdf(report_from([(6815, None, -10.0), (6850, '', -5.0)]), True, '', CREATED))
    texts = _texts(stream)
    assert '6815' in texts and '6850' in texts and not any('None' in text for text in texts)


def test_overlong_names_are_cut_with_an_ellipsis_to_the_width_of_their_place():
    report = report_from([(6815, 'W' * 400, -10.0), (6850, 'Wort ' * 300, -5.0), (6805, 'ß' * (MAX_LABEL_CHARS - 10), -2.0)])
    pdf = generate_bwa_pdf(report, True, 'Firma ' * 100, CREATED)
    [stream] = _streams(pdf)
    cells = _cells(stream)
    accounts = [text for x, y, text in cells if x == ACCOUNT_X and BOTTOM_Y <= y <= TABLE_TOP]
    assert len(accounts) == 3
    for text in accounts:
        assert info_width(text, 7) <= LABEL_RIGHT - ACCOUNT_RESERVE, text
    assert all(text.endswith('…') for text in accounts)
    [company] = [text for x, y, text in cells if y == COMPANY_Y]
    assert company.endswith('…') and info_width(company, 10) <= RIGHT_X - LEFT_X


# ── Download ──────────────────────────────────────────────────────────────────
def test_the_pdf_route_answers_with_a_download_and_writes_nothing(server_port, auto_login):
    db = auto_login.db
    db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Firma GmbH', company_name='Eigene Firma GmbH')
    some_books(db)
    before_db, before_tree = dump(db.db_name), tree(userctx.DATA_ROOT)

    default = get(server_port, '/bwa/pdf?year=2026&month=3')
    assert default.status == 200 and default.headers['Content-Type'] == 'application/pdf'
    assert default.headers['Content-Disposition'].startswith('attachment;')
    assert default.headers['X-Filename'] == quote('BWA_2026-03.pdf', safe='') and default.body.startswith(b'%PDF-')
    shares = get(server_port, '/bwa/pdf?year=2026&month=3&variant=jahr')
    assert shares.status == 200 and shares.headers['X-Filename'] == quote('BWA_2026-03_Anteile.pdf', safe='')
    assert 'BWA_2026-03_Anteile.pdf' in shares.headers['Content-Disposition']

    with_accounts = _texts(_streams(get(server_port, '/bwa/pdf?year=2026&month=3&accounts=1').body)[0])
    without = _texts(_streams(get(server_port, '/bwa/pdf?year=2026&month=3&accounts=0').body)[0])
    unspecified = _texts(_streams(default.body)[0])
    assert '4400 Erlöse 19% USt' in with_accounts and '4400 Erlöse 19% USt' not in without and unspecified == without   # Standard: ohne
    assert 'Eigene Firma GmbH' in with_accounts and 'Eigene Firma GmbH' in without
    for switch in ('true', 'on'):
        assert '4400 Erlöse 19% USt' in _texts(_streams(get(server_port, f'/bwa/pdf?year=2026&month=3&accounts={switch}').body)[0])
    assert '4400 Erlöse 19% USt' not in _texts(_streams(get(server_port, '/bwa/pdf?year=2026&month=3&accounts=nein').body)[0])

    assert dump(db.db_name) == before_db and tree(userctx.DATA_ROOT) == before_tree          # nichts geschrieben, auch keine PDF-Datei


def test_the_pdf_of_a_very_early_year_and_of_every_month_is_built(tmp_db):
    some_books(tmp_db)
    for month in range(1, 13):
        for variant in ('jahr', 'vorjahr'):
            pdf = generate_bwa_pdf(tmp_db.get_bwa(2026, month, variant), True, 'Eigene Firma GmbH', CREATED)
            assert pdf.startswith(b'%PDF-') and _streams(pdf)
    assert generate_bwa_pdf(tmp_db.get_bwa(1900, 2, 'vorjahr'), True, '', CREATED).startswith(b'%PDF-')
