# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""PDF der Liquiditätsrechnung (2.6.3): A4 quer, mehrseitig, nichts auf der Platte.

* Querformat (842 × 595), Titel, Firma, Zeitraum und Hinweis stehen im Text; ohne Namen entfällt die Firmenzeile
* die Darstellung „Monat und Jahr“ hat zwei breite Wertspalten am rechten Rand, der Vorjahresvergleich acht
* viele Konten laufen über mehrere Seiten: Kopf und Fuß auf jeder, „Seite n von m“, keine Zeile unter dem Rand; ein Block (eine Zeile mit ihren
  Konten) wird nicht geteilt, solange er auf eine Seite passt – nur ein Block über eine Seite läuft weiter
* ohne Kontennachweis stehen nur die Zeilen da, hinter der Tabelle der Textblock mit dem Satz zur Abstimmung
* Beträge liegen rechtsbündig auf den Kanten der Spalten (gemessen mit pdfplumber, nicht mit den Funktionen des PDF-Baus); jede Bezeichnung passt in ihren Platz
* Klammern, Rückstriche, Umlaute und Steuerzeichen in Namen ergeben einen gültigen Text; überlange Namen werden mit „…“ gekürzt
* der Download (``GET /liquiditaet/pdf``): Anhang mit ``X-Filename``, schreibt nichts, bei einem Fehler 500 mit festem Satz

Der Aufbau des Berichts und die Zahlen prüft ``test_liquidity.py``. Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import datetime
import io
import re
from collections import Counter
from urllib.parse import quote

import pdfplumber
import pytest
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import stream_value

import userctx
from db import liquidity as liq
from db.liquidity import (BALANCED_TEXT, HINT_TEXT, METHOD_TEXT, ROUNDING_TEXT, SIGN_TEXT, TITLE, TRANSFER_TEXT, UNASSIGNED_TEXT, UNBALANCED_TEXT,
                          CashFigures, line_label, visible)
from export import pdf_liquidity, pdf_table
from export.pdf_liquidity import (ACCOUNT_RESERVE, ACCOUNT_X, BOTTOM_Y, COLUMN_COUNT, COMPANY_Y, FOOTER_Y, LABEL_GAP, LABEL_RIGHT, LEFT_X, LINES_PER_PAGE,
                                  MAX_LABEL_CHARS, PAGE_SIZE, RIGHT_X, ROW_H, TABLE_TOP, YEAR_COLUMN_WIDTH, column_edge, generate_liquidity_pdf, label_right,
                                  page_layout)
from export.pdf_worktime import info_width
from tests.http_client import get
from tests.installation import dump, tree
from tests.liquidity_helpers import liquidity_from, mask, two_banks
from tests.test_pdfa import _doc

CREATED = datetime.date(2026, 11, 21)          # „Erstellt am“: ein fester Tag, nie die Uhr
TM = re.compile(r'1 0 0 1 ([0-9.]+) (-?[0-9.]+) Tm \((.*)\) Tj')
NUMBER = re.compile(r'-?\d{1,3}(?:\.\d{3})*,\d\d')
BRANDS = ('DATEV', 'WISO', 'ELSTER')


# ── Lesen des PDF ─────────────────────────────────────────────────────────────
def _streams(pdf):
    """Der Content-Stream jeder Seite als Text."""
    return [b''.join(stream_value(part).get_data() for part in page.contents).decode('cp1252') for page in PDFPage.create_pages(_doc(pdf))]


def _cells(stream):
    """Alle Textzellen eines Streams: ``(x, y, Text)`` – der Text ohne die Maskierung der Klammern und Rückstriche."""
    return [(float(x), float(y), re.sub(r'\\([()\\])', r'\1', text)) for x, y, text in TM.findall(stream)]


def _texts(stream):
    return [text for _, _, text in _cells(stream)]


def _table(cells):
    """Die Zeilen der Tabelle einer Seite: ``(y, x, Text)`` der Bezeichnungen (links), von oben nach unten."""
    labels = [(y, x, text) for x, y, text in cells if BOTTOM_Y <= y <= TABLE_TOP and x in (LEFT_X, ACCOUNT_X)]
    return sorted(labels, key=lambda label: -label[0])


def _page_cells(pdf):
    return [_cells(stream) for stream in _streams(pdf)]


def _rows(number, count, first=1, prefix='Konto'):
    """``count`` Konten ab Nummer ``number + first`` mit einem Saldo."""
    return [(number + index, f'{prefix} {number + index}', -(index + 1) * 1.5) for index in range(first, first + count)]


def _wide_report(variant='vorjahr'):
    """Sieben Zeilen mit je zwölf Konten: Jeder Block (eine Zeile und zwölf Konten) ist 13 Zeilen hoch und passt auf eine Seite."""
    rows = (_rows(6200, 12)                              # Abschreibungen
            + _rows(6895, 3) + _rows(4855, 3)              # Restbuchwert (zwei Bereiche)
            + _rows(3000, 12)                              # Darlehen im Ergebnis und Darlehen (je zwölf Konten)
            + _rows(500, 12) + _rows(2100, 12) + _rows(2180, 12))
    return liquidity_from(rows, variant=variant, prev_month_rows=rows[:20], prev_ytd_rows=rows[:20])


# ── Geometrie ─────────────────────────────────────────────────────────────────
def test_the_lines_per_page_end_above_the_margin():
    assert LINES_PER_PAGE == 40
    assert TABLE_TOP - (LINES_PER_PAGE - 1) * ROW_H >= BOTTOM_Y > TABLE_TOP - LINES_PER_PAGE * ROW_H


def test_the_columns_end_on_the_right_margin_in_both_variants():
    assert label_right(COLUMN_COUNT) == LABEL_RIGHT == 290 and column_edge(COLUMN_COUNT, COLUMN_COUNT - 1) == RIGHT_X
    assert column_edge(COLUMN_COUNT, -1) == LABEL_RIGHT and column_edge(COLUMN_COUNT, 0) - LABEL_RIGHT == (RIGHT_X - LABEL_RIGHT) / COLUMN_COUNT
    assert label_right(2) == RIGHT_X - 2 * YEAR_COLUMN_WIDTH and column_edge(2, 1) == RIGHT_X
    assert column_edge(2, 1) - column_edge(2, 0) == column_edge(2, 0) - column_edge(2, -1) == YEAR_COLUMN_WIDTH
    assert LEFT_X < ACCOUNT_X < LABEL_RIGHT - ACCOUNT_RESERVE and PAGE_SIZE == (842, 595)


def test_page_layout_keeps_blocks_together():
    n = LINES_PER_PAGE
    assert page_layout([]) == [[]]
    assert page_layout([3, n - 2, 2]) == [[3], [n - 2, 2]]                  # der zweite Block passt nicht mehr: nächste Seite
    assert page_layout([3, n - 5, 2]) == [[3, n - 5, 2]]                    # passt genau
    assert page_layout([n]) == [[n]] and page_layout([n, 1]) == [[n], [1]]
    assert page_layout([3, 2 * n + 5, 2]) == [[3], [n], [n], [5, 2]]        # ein Block über eine Seite läuft über so viele, wie er braucht
    assert page_layout([0, -4]) == [[1, 1]]                                 # eine Zeile ist mindestens eine Zeile hoch


# ── Eine Seite ────────────────────────────────────────────────────────────────
CASH = {'month': CashFigures(1_000_000, -2_000_000, 0, 0), 'ytd': CashFigures(1_000_000, -2_000_000, 0, 0)}
ROWS = [(4400, 'Erlöse 19% USt', 1000.0), (3806, 'Umsatzsteuer 19%', 190.0), (6815, 'Bürobedarf', -100.0), (6220, 'Abschreibung', -250.0),
        (500, 'Anlage', -300.0), (3150, 'Darlehen', 400.0), (2100, 'Entnahme', -80.0)]


@pytest.mark.parametrize('variant', ['jahr', 'vorjahr'])
def test_the_pdf_is_landscape_and_names_the_company(variant):
    report = liquidity_from(ROWS, variant=variant, cash=CASH)
    pdf = generate_liquidity_pdf(report, True, 'Eigene Firma GmbH', CREATED)
    pages = list(PDFPage.create_pages(_doc(pdf)))
    assert [tuple(int(value) for value in page.mediabox) for page in pages] == [(0, 0) + PAGE_SIZE] == [(0, 0, 842, 595)]
    [stream] = _streams(pdf)
    texts = _texts(stream)
    assert TITLE in texts and 'Eigene Firma GmbH' in texts and HINT_TEXT in texts and 'Erstellt am 21.11.2026' in texts and 'Position' in texts
    if variant == 'vorjahr':
        assert 'März 2026 · aufgelaufen Januar bis März 2026 · Vergleich mit dem Vorjahr' in texts
        assert ['März', 'Januar bis März'] == [text for text in texts if text in ('März', 'Januar bis März')]
    else:
        assert 'März 2026 · aufgelaufen Januar bis März 2026' in texts and 'März 2026' in texts and 'Januar bis März 2026' in texts
    assert 'Seite' not in ' '.join(texts)                                    # eine Seite: keine Seitenzahl
    info = _doc(pdf).info[0]
    assert info['Title'] == b'\xfe\xff' + TITLE.encode('utf-16-be') and info['Author'] == b'Eigene Firma GmbH'       # ein Umlaut: UTF-16BE mit Kennung
    for fragment in (b'/F1 ', b'/F2 '):
        assert fragment in pdf


def test_without_a_company_name_the_company_line_is_left_out():
    [stream] = _streams(generate_liquidity_pdf(liquidity_from(ROWS), True, '', CREATED))
    assert all(y != COMPANY_Y for _x, y, _text in _cells(stream))
    [stream] = _streams(generate_liquidity_pdf(liquidity_from(ROWS), True, 'Eigene Firma GmbH', CREATED))
    assert any(y == COMPANY_Y for _x, y, _text in _cells(stream))


def _label_rows(pdf):
    return [text for _y, _x, text in _table(_cells(_streams(pdf)[0]))]


def test_without_the_account_list_only_the_lines_are_printed():
    report = liquidity_from(ROWS, cash=CASH)
    with_accounts = generate_liquidity_pdf(report, True, '', CREATED)
    without = generate_liquidity_pdf(report, False, '', CREATED)
    assert len(_streams(with_accounts)) == len(_streams(without)) == 1
    expected = [pdf_table.clean(line_label(report, line)) for line in report.lines if visible(report, line)]      # wie gedruckt: das Minuszeichen als „-“
    labels = _label_rows(without)
    assert labels[:len(expected)] == expected and all(not re.match(r'\d{3,4} ', label) for label in labels)
    with_labels = _label_rows(with_accounts)
    assert {'6220 Abschreibung', '500 Anlage', '3150 Darlehen', '2100 Entnahme'} <= set(with_labels)
    assert [label for label in with_labels if not re.match(r'\d{3,4} ', label)][:len(expected)] == expected       # dieselben Zeilen, dazu die Konten


def test_the_optional_lines_stand_only_with_a_value_and_the_unbooked_line_names_its_count():
    quiet = _label_rows(generate_liquidity_pdf(liquidity_from([(4400, 'Erlöse', 10.0)]), True, '', CREATED))
    assert not any(label.startswith(('Übrige', 'Nachrichtlich')) for label in quiet)
    assert 'Nicht zugeordnet' in quiet and 'Bankbewegungen ohne Buchungssatz' in quiet
    cash = {'month': CashFigures(0, -500_000, 1_190_000, 3), 'ytd': CashFigures(0, -500_000, 1_190_000, 12)}
    labels = _label_rows(generate_liquidity_pdf(liquidity_from([(1800, 'Bank', -5.0)], banks={1800}, cash=cash), True, '', CREATED))
    assert 'Bankbewegungen ohne Buchungssatz (3 im Monat, 12 aufgelaufen)' in labels and 'Übrige Konten außerhalb des Ergebnisses' in labels
    assert 'Nachrichtlich: Überträge zwischen eigenen Konten' in labels and 'Nachrichtlich: Veränderung laut Kontobewegungen' in labels


def test_the_text_block_after_the_table_names_the_reconciliation():
    def printed(report):
        pdf = generate_liquidity_pdf(report, False, '', CREATED)
        return ' '.join(text for stream in _streams(pdf) for _x, y, text in _cells(stream) if BOTTOM_Y <= y < TABLE_TOP or y == TABLE_TOP)
    balanced = liquidity_from([(4400, 'Erlöse', 100.0)], cash={'month': CashFigures(1_000_000, 0, 0, 0), 'ytd': CashFigures(1_000_000, 0, 0, 0)})
    text = printed(balanced)
    assert BALANCED_TEXT in text and SIGN_TEXT in text and ' '.join(METHOD_TEXT.split()) in ' '.join(text.split())
    assert UNBALANCED_TEXT not in text and UNASSIGNED_TEXT[:40] not in text and TRANSFER_TEXT not in text
    rest = liquidity_from([(4400, 'Erlöse', 100.0)], cash={'month': CashFigures(900_000, -100_000, 0, 0), 'ytd': CashFigures(1_000_000, 0, 0, 0)})
    text = ' '.join(printed(rest).split())
    assert UNBALANCED_TEXT in text and ' '.join(UNASSIGNED_TEXT.split()) in text and BALANCED_TEXT not in text and TRANSFER_TEXT in text
    assert ROUNDING_TEXT not in text
    cent = liquidity_from([(4400, 'Erlöse', 100.0)], cash={'month': CashFigures(1_000_000 - 49, 0, 0, 0), 'ytd': CashFigures(1_000_000, 0, 0, 0)})
    assert ROUNDING_TEXT in ' '.join(printed(cent).split())


def test_no_brand_name_stands_in_the_pdf():
    report = liquidity_from(ROWS, cash=CASH, variant='vorjahr', prev_month_rows=ROWS)
    text = ' '.join(text for stream in _streams(generate_liquidity_pdf(report, True, 'Eigene Firma GmbH', CREATED)) for text in _texts(stream)).lower()
    for brand in BRANDS:
        assert brand.lower() not in text, brand


# ── Mehrere Seiten ────────────────────────────────────────────────────────────
def test_many_accounts_run_over_several_pages_and_nothing_lies_below_the_margin():
    pdf = generate_liquidity_pdf(_wide_report(), True, 'Eigene Firma GmbH', CREATED)
    pages = _page_cells(pdf)
    total = len(pages)
    assert total >= 3
    for number, cells in enumerate(pages, start=1):
        below = [(y, text) for _x, y, text in cells if y < BOTTOM_Y]
        assert sorted(text for _y, text in below) == sorted([HINT_TEXT, f'Seite {number} von {total}']), number
        assert {y for y, _text in below} == {FOOTER_Y}
        texts = [text for _x, _y, text in cells]
        assert TITLE in texts and 'Eigene Firma GmbH' in texts and 'Position' in texts                  # Kopf auf jeder Seite
        assert 'März' in texts and 'Januar bis März' in texts
    printed = Counter(text for cells in pages for _y, _x, text in _table(cells) if re.match(r'\d{3,4} ', text))
    assert sum(printed.values()) == 78 == 12 + 6 + 12 + 12 + 12 + 12 + 12              # jedes Konto in jeder Zeile genau einmal, in der Reihenfolge der Tabelle
    assert all(count == 2 for text, count in printed.items() if text.startswith('30'))   # die Darlehen stehen im Ergebnis und als Finanzierung
    assert all(count == 1 for text, count in printed.items() if not text.startswith('30'))


def test_no_block_is_split_where_it_fits_on_a_page():
    """Die erste Zeile jeder Folgeseite ist die Zeile einer Gruppe (links), nie ein einzelnes Konto (eingerückt): Jeder Block, der auf eine Seite passt, steht
    ganz auf einer."""
    pages = _page_cells(generate_liquidity_pdf(_wide_report(), True, '', CREATED))
    assert len(pages) >= 3
    for cells in pages:
        [(first_y, first_x, _text)] = _table(cells)[:1]
        assert first_y == TABLE_TOP and first_x == LEFT_X


def test_one_block_longer_than_a_page_runs_over_and_starts_on_a_new_page():
    rows = [(4400, 'Erlöse', 1000.0)] + _rows(100, 120)                         # 120 Konten unter den Anlagenzugängen: der Block ist 121 Zeilen hoch
    pages = _page_cells(generate_liquidity_pdf(liquidity_from(rows), True, '', CREATED))
    assert len(pages) >= 5
    first_rows = [_table(cells)[0] for cells in pages]
    assert first_rows[1][1] == LEFT_X and first_rows[1][2] == 'Anlagenzugänge (Zahlungen auf Anlagekonten)'         # beginnt oben auf einer neuen Seite
    assert first_rows[2][1] == ACCOUNT_X and first_rows[3][1] == ACCOUNT_X                                          # und läuft auf den nächsten weiter
    printed = [text for cells in pages for _y, _x, text in _table(cells) if re.match(r'(1\d\d|2[0-2]\d) ', text)]
    assert len(printed) == 120 and printed[0].startswith('101 ') and printed[-1].startswith('220 ')
    for number, cells in enumerate(pages, start=1):
        assert min(y for _x, y, _text in cells) == FOOTER_Y, number
        assert not [text for _x, y, text in cells if y < BOTTOM_Y and text not in (HINT_TEXT, f'Seite {number} von {len(pages)}')]


def test_the_text_block_is_a_block_like_any_other_and_moves_to_the_next_page_when_it_does_not_fit():
    """Passt der Textblock nicht mehr auf die Restseite, beginnt er auf der nächsten – er wird nicht geteilt und liegt nie unter dem Rand."""
    for count in range(1, 60):                                        # so viele Konten unter den Anlagenzugängen, dass der Textblock gerade nicht mehr passt
        report = liquidity_from(_rows(100, count) + [(4400, 'Erlöse', 10.0)])
        blocks = pdf_liquidity._blocks(report, True)
        layout = page_layout([len(block) for block in blocks])
        if len(layout) >= 2 and layout[-1] == [len(blocks[-1])]:
            break
    else:
        pytest.fail('kein Bestand gefunden, in dem der Textblock auf eine eigene Seite rutscht')
    pages = _page_cells(generate_liquidity_pdf(report, True, '', CREATED))
    assert len(pages) == len(layout)
    for number, cells in enumerate(pages, start=1):
        assert not [text for _x, y, text in cells if y < BOTTOM_Y and text not in (HINT_TEXT, f'Seite {number} von {len(pages)}')]
    sentence = [number for number, cells in enumerate(pages, start=1) if any(text.startswith('Die Überleitung geht') for _x, _y, text in cells)]
    assert sentence == [len(pages)]                                   # der Satz zur Abstimmung steht nur auf der letzten Seite …
    last = [text for _x, _y, text in pages[-1]]
    assert 'Nicht zugeordnet' not in last and TITLE in last and 'Position' in last        # … die Tabelle endet davor, der Kopf steht auch dort


def test_the_hint_fits_into_the_footer_line_next_to_the_page_number():
    assert info_width(HINT_TEXT, 7) + info_width('Seite 10 von 10', 7) + 20 < RIGHT_X - LEFT_X


# ── Zahlen und Namen ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('variant', ['jahr', 'vorjahr'])
def test_every_amount_ends_on_the_edge_of_its_column(variant):
    """Gemessen mit pdfplumber (den Breiten der eingebetteten Schrift im PDF), nicht mit den Funktionen des PDF-Baus."""
    rows = ROWS + [(520, 'Fahrzeug', -1234567.89), (3160, 'Darlehen 1-5 Jahre', 98765432.1)]
    report = liquidity_from(rows, variant=variant, prev_month_rows=ROWS, prev_ytd_rows=ROWS, cash=CASH)
    pdf = generate_liquidity_pdf(report, True, '', CREATED)
    count = 2 if variant == 'jahr' else COLUMN_COUNT
    edges = [column_edge(count, index) for index in range(count)]
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        words = [word for page in document.pages for word in page.extract_words()]
    numbers = [word for word in words if NUMBER.fullmatch(word['text'])]
    assert len(numbers) > (30 if variant == 'jahr' else 100)
    for word in numbers:
        assert min(abs(word['x1'] - edge) for edge in edges) < 0.6, (word['text'], word['x1'])
    assert any(word['text'] == '-1.234.567,89' for word in numbers) and any(word['text'] == '98.765.432,10' for word in numbers)     # ein Abfluss steht negativ


@pytest.mark.parametrize('variant', ['jahr', 'vorjahr'])
def test_every_label_fits_into_its_place_and_is_never_cut(variant):
    """Keine Bezeichnung einer Zeile wird mit „…“ gekürzt – auch die längste Form der Bezeichnung „ohne Buchungssatz“ nicht (bis 999 Bewegungen im Monat und
    aufgelaufen). Gemessen mit den Breiten der Schrift (fett bei Summen und Ergebnis)."""
    cash = {key: CashFigures(0, -100, 100, 999) for key in ('month', 'ytd', 'prev_month', 'prev_ytd')}
    report = liquidity_from([(1800, 'Bank', -5.0)], banks={1800}, variant=variant, month=12, cash=cash)
    assert line_label(report, report.line('unbooked')) == 'Bankbewegungen ohne Buchungssatz (999 im Monat, 999 aufgelaufen)'
    available = label_right(2 if variant == 'jahr' else COLUMN_COUNT) - LABEL_GAP - LEFT_X
    for line in report.lines:
        text = pdf_table.clean(line_label(report, line))
        bold = line.kind in (liq.SUM, liq.RESULT)
        assert pdf_table.width(text, 7.5, '/F2' if bold else '/F1') <= available, (line.key, text)
        assert pdf_table.fit(text, available, 7.5, MAX_LABEL_CHARS) == text


def test_an_absurd_count_of_unbooked_movements_is_cut_before_the_first_column_and_never_runs_into_the_numbers():
    cash = {key: CashFigures(0, 0, 100, 9_999_999) for key in ('month', 'ytd', 'prev_month', 'prev_ytd')}
    report = liquidity_from([], variant='vorjahr', cash=cash)
    [label] = [text for _y, _x, text in _table(_cells(_streams(generate_liquidity_pdf(report, False, '', CREATED))[0])) if text.startswith('Bankbewegungen')]
    assert label.endswith('…') and info_width(label, 7.5) <= LABEL_RIGHT - LABEL_GAP - LEFT_X


def test_a_name_with_brackets_a_backslash_and_umlauts_prints_unchanged():
    name = '(Test) Anlage\\Ä – € ß'
    pdf = generate_liquidity_pdf(liquidity_from([(500, name, -10.0)]), True, 'Muster (Test) GmbH \\ Co. KG', CREATED)
    [stream] = _streams(pdf)
    texts = _texts(stream)
    assert f'500 {name}' in texts and 'Muster (Test) GmbH \\ Co. KG' in texts
    assert _doc(pdf).info[0]['Author'] == b'Muster (Test) GmbH \\ Co. KG'


def test_control_characters_in_a_name_do_not_break_the_stream():
    pdf = generate_liquidity_pdf(liquidity_from([(500, 'Zeile eins\nZeile zwei\tEnde\r\x00\x07 Schluss', -10.0)]), True, '', CREATED)
    [stream] = _streams(pdf)
    assert '500 Zeile eins Zeile zwei Ende Schluss' in _texts(stream)
    assert not re.search(r'[\x00-\x08\x0b-\x1f\x7f]', stream.replace('\n', ' '))


def test_an_account_without_a_name_prints_its_number_and_a_missing_name_never_prints_none():
    [stream] = _streams(generate_liquidity_pdf(liquidity_from([(500, None, -10.0), (510, '', -5.0)]), True, '', CREATED))
    texts = _texts(stream)
    assert '500' in texts and '510' in texts and not any('None' in text for text in texts)


@pytest.mark.parametrize('variant', ['jahr', 'vorjahr'])
def test_overlong_names_are_cut_with_an_ellipsis_to_the_width_of_their_place(variant):
    report = liquidity_from([(500, 'W' * 400, -10.0), (510, 'Wort ' * 300, -5.0), (520, 'ß' * (MAX_LABEL_CHARS - 10), -2.0)], variant=variant)
    pdf = generate_liquidity_pdf(report, True, 'Firma ' * 100, CREATED)
    [stream] = _streams(pdf)
    cells = _cells(stream)
    accounts = [text for x, y, text in cells if x == ACCOUNT_X and BOTTOM_Y <= y <= TABLE_TOP]
    assert len(accounts) == 3
    right = label_right(2 if variant == 'jahr' else COLUMN_COUNT)
    for text in accounts:
        assert info_width(text, 7) <= right - ACCOUNT_RESERVE, text
    assert all(text.endswith('…') for text in accounts)
    [company] = [text for x, y, text in cells if y == COMPANY_Y]
    assert company.endswith('…') and info_width(company, 10) <= RIGHT_X - LEFT_X


# ── Alle Monate ───────────────────────────────────────────────────────────────
def test_the_pdf_of_every_month_and_variant_is_built(tmp_db):
    two_banks(tmp_db)
    mask(tmp_db, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '19.00')
    mask(tmp_db, 'Probe-Giro', 500, '-1190.00', '2026-03-14', '19', '-190.00')
    mask(tmp_db, 'Probe-Giro', 3150, '5000.00', '2026-03-15')
    mask(tmp_db, 'Probe-Giro', 10000, '100.00', '2026-05-05')                       # ein Rest in „Nicht zugeordnet“
    for month in range(1, 13):
        for variant in ('jahr', 'vorjahr'):
            pdf = generate_liquidity_pdf(tmp_db.get_liquidity(2026, month, variant), True, 'Eigene Firma GmbH', CREATED)
            assert pdf.startswith(b'%PDF-') and _streams(pdf)
    assert generate_liquidity_pdf(tmp_db.get_liquidity(1900, 2, 'vorjahr'), True, '', CREATED).startswith(b'%PDF-')


# ── Download ──────────────────────────────────────────────────────────────────
def _books(db):
    db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Firma GmbH', company_name='Eigene Firma GmbH')
    two_banks(db)
    mask(db, 'Probe-Giro', 4400, '119.00', '2026-03-10', '19', '19.00')
    mask(db, 'Probe-Giro', 500, '-1190.00', '2026-03-14', '19', '-190.00')
    mask(db, 'Probe-Giro', 2100, '-80.00', '2026-03-20')


def test_the_pdf_route_answers_with_a_download_and_writes_nothing(server_port, auto_login):
    db = auto_login.db
    _books(db)
    before_db, before_tree = dump(db.db_name), tree(userctx.DATA_ROOT)

    default = get(server_port, '/liquiditaet/pdf?year=2026&month=3')
    assert default.status == 200 and default.headers['Content-Type'] == 'application/pdf'
    assert default.headers['Content-Disposition'].startswith('attachment;')
    assert default.headers['X-Filename'] == quote('Liquiditaet_2026-03.pdf', safe='') and default.body.startswith(b'%PDF-')
    assert default.headers.get_all('Cache-Control') == ['no-cache'] and default.headers.get('Set-Cookie') is None
    year = get(server_port, '/liquiditaet/pdf?year=2026&month=3&variant=jahr')
    assert year.status == 200 and year.headers['X-Filename'] == quote('Liquiditaet_2026-03_Monat_Jahr.pdf', safe='')
    assert 'Liquiditaet_2026-03_Monat_Jahr.pdf' in year.headers['Content-Disposition']

    with_accounts = _texts(_streams(get(server_port, '/liquiditaet/pdf?year=2026&month=3&accounts=1').body)[0])
    without = _texts(_streams(get(server_port, '/liquiditaet/pdf?year=2026&month=3&accounts=0').body)[0])
    unspecified = _texts(_streams(default.body)[0])
    assert '500 Andere Anlagen, Betriebs- und Geschäftsausstattung' in with_accounts and '500 Andere Anlagen, Betriebs- und Geschäftsausstattung' not in without
    assert unspecified == without                                                                      # Standard: ohne Kontennachweis
    assert 'Eigene Firma GmbH' in with_accounts and 'Eigene Firma GmbH' in without
    for switch in ('true', 'on'):
        assert any(text.startswith('500 ') for text in _texts(_streams(get(server_port, f'/liquiditaet/pdf?year=2026&month=3&accounts={switch}').body)[0]))
    assert not any(text.startswith('500 ') for text in _texts(_streams(get(server_port, '/liquiditaet/pdf?year=2026&month=3&accounts=nein').body)[0]))

    assert dump(db.db_name) == before_db and tree(userctx.DATA_ROOT) == before_tree          # nichts geschrieben, auch keine PDF-Datei


def test_the_pdf_route_refuses_what_it_cannot_read(server_port, auto_login):
    for path in ('/liquiditaet/pdf', '/liquiditaet/pdf?year=2026', '/liquiditaet/pdf?year=2026&month=13', '/liquiditaet/pdf?month=3',
                 '/liquiditaet/pdf?year=2026&year=2025&month=3'):
        response = get(server_port, path)
        assert response.status == 400 and response.text == 'Ungültiger Zeitraum.', path
        assert response.headers['Content-Type'] == 'text/plain; charset=utf-8'
    response = get(server_port, '/liquiditaet/pdf?year=2026&month=3&variant=x')
    assert response.status == 400 and response.text == 'Ungültige Darstellung.'


def test_a_failing_pdf_answers_with_a_fixed_sentence_and_no_exception_text(server_port, auto_login, monkeypatch, capsys):
    """Ein Fehler beim Erzeugen steht nur im Server-Log: Die Antwort ist 500 mit festem Satz, ohne Ausnahmetext oder Pfad."""
    from server.pages_liquidity import LIQUIDITY_PDF_FAILED_TEXT

    def failing(*args, **kwargs):
        raise RuntimeError('geheimer Pfad /data/users/admin')
    monkeypatch.setattr(pdf_liquidity, 'generate_liquidity_pdf', failing)
    response = get(server_port, '/liquiditaet/pdf?year=2026&month=3')
    assert response.status == 500 and response.text == LIQUIDITY_PDF_FAILED_TEXT == 'Die Liquiditätsrechnung ließ sich nicht als PDF erstellen.'
    assert 'geheim' not in response.text and response.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert 'geheimer Pfad' in capsys.readouterr().err                                              # der volle Fehler steht im Server-Log
