# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Gemeinsame Bausteine der Tabellen-PDFs (2.5.16, ``export/pdf_table.py``) – und die Zusage, dass das PDF der betriebswirtschaftlichen
Auswertung dabei dasselbe geblieben ist.

``pdf_bwa`` hat seine Bausteine (Text bereinigen, messen, kürzen, Zelle, Linie, Seitenverteilung) an ``pdf_table`` abgegeben und ruft sie über
dünne Hüllen auf. Die SHA-256-Schnappschüsse (``OPS_SNAPSHOTS``) halten die Operationen aller Seiten von zehn Berichten fest, genommen am
Stand 2.5.14 VOR dem Herausziehen – darunter ein mehrseitiger mit 120 Konten, Beträge an der Spaltenkante, Namen mit Klammern, Rückstrichen,
Steuerzeichen und überlange Namen. Weicht ein Zeichen ab, ist der Code zu ändern, nie der Schnappschuss.

Dazu die Bausteine selbst: Seitenverteilung mit beliebiger Zeilenzahl (nichts geht verloren, kein Block wird geteilt, wo er passt), Umbruch
(``wrap``: keine Zeile breiter als die Spalte, Grenze von 200 Zeilen), Kürzen, Zelle und Linie.

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import datetime
import hashlib
import random

import pytest

from export import pdf_bwa, pdf_table
from export.pdf_worktime import info_width
from tests.bwa_helpers import report_from, some_books

CREATED = datetime.date(2026, 11, 21)          # „Erstellt am“: ein fester Tag, nie die Uhr
COMPANY = 'Eigene Firma GmbH'


# ═══ Schnappschuss: pdf_bwa gibt dasselbe aus ═════════════════════════════════
def _ops_of(report, with_accounts, company):
    """Alle Operationen aller Seiten, verteilt wie in ``generate_bwa_pdf``."""
    blocks = pdf_bwa._rows(report, with_accounts)
    layout = pdf_bwa.page_layout([len(block) for block in blocks])
    rows = [row for block in blocks for row in block]
    ops, first = [], 0
    for number, heights in enumerate(layout, start=1):
        count = sum(heights)
        ops.append(f'--- Seite {number} von {len(layout)}')
        ops += pdf_bwa._page_ops(report, rows[first:first + count], number, len(layout), company, CREATED)
        first += count
    return '\n'.join(ops)


def _rows(number, count, first=1, prefix='Konto'):
    return [(number + index, f'{prefix} {number + index}', -(index + 1) * 1.5) for index in range(first, first + count)]


EIGHT_GROUPS = [row for base in (5000, 5900, 6000, 6300, 6400, 6500, 6600, 6700) for row in _rows(base, 12)]
ROWS = [(4400, 'Erlöse 19% USt', 1000.0), (3806, 'Umsatzsteuer 19%', 190.0), (6815, 'Bürobedarf', -100.0),
        (1406, 'Abziehbare Vorsteuer 19%', -19.0), (3820, 'Umsatzsteuer-Vorauszahlungen', -50.0),
        (1050, 'Altkonto', -5.0), (3160, 'Darlehen', -30.0)]
BIG_AMOUNTS = ROWS + [(6850, 'Sonstiger Betriebsbedarf', -1234567.89), (4300, 'Erlöse 7% USt', 98765432.1)]
NAME_EDGE_CASES = [(6815, '(Test) Bürobedarf\\Ä – € ß', -10.0), (6850, 'Zeile eins\nZeile zwei\tEnde\r\x00\x07 Schluss', -5.0),
                   (6805, None, -2.0), (6820, '', -3.0), (6830, 'W' * 400, -10.0), (6840, 'Wort ' * 300, -5.0), (6860, 'ß' * 190, -2.0)]

# Fall → (Bericht aus der Datenbank oder aus Zeilen, mit Konten, Firma)
OPS_CASES = {
    'some_books_jahr': (lambda db: db.get_bwa(2026, 3, 'jahr'), True, COMPANY),
    'some_books_vorjahr': (lambda db: db.get_bwa(2026, 3, 'vorjahr'), True, COMPANY),
    'some_books_vorjahr_lines': (lambda db: db.get_bwa(2026, 3, 'vorjahr'), False, COMPANY),
    'some_books_january': (lambda db: db.get_bwa(2026, 1, 'jahr'), True, ''),
    'report_120_accounts': (lambda db: report_from([(4400, 'Erlöse 19% USt', 1000.0)] + _rows(6900, 120)), True, COMPANY),
    'report_eight_groups': (lambda db: report_from(EIGHT_GROUPS), True, COMPANY),
    'report_rows_vorjahr': (lambda db: report_from(ROWS, variant='vorjahr', prev_month_rows=ROWS, prev_ytd_rows=ROWS), True, COMPANY),
    'report_bigamounts': (lambda db: report_from(BIG_AMOUNTS, variant='vorjahr', prev_month_rows=ROWS, prev_ytd_rows=ROWS), True, COMPANY),
    'report_name_edge_cases': (lambda db: report_from(NAME_EDGE_CASES), True, 'Firma ' * 100),
    'report_company_special': (lambda db: report_from([(6815, 'x', -10.0)]), True, 'Muster (Test) GmbH \\ Co. KG'),
}
# SHA-256 der Operationen, genommen am Stand 2.5.14 vor dem Herausziehen der Bausteine
OPS_SNAPSHOTS = {
    'some_books_jahr': '27372a9d894d9275fe4942b90b696037450e37142819067a8b72e497a19f7ac9',
    'some_books_vorjahr': '050154b2d11432e2bef926f14749d25d640614fafb6804173f0e4909567e8aca',
    'some_books_vorjahr_lines': '73d0941a0cc687df527872ce41855576a79e9ba3e677174495c9b982f0f12c25',
    'some_books_january': '284ecdf7efb8f818154955987a36b081de5aafdfc904fd57df836f320d137cfb',
    'report_120_accounts': 'e0ef1fe439cc471cc8e57f29d93874f8665babbccb37e1ea43034e7518d2188d',
    'report_eight_groups': '2b6a09208459358d2b5b14525de92ba807d1884944a3d87956390af0c119f480',
    'report_rows_vorjahr': '9e20fcac4baca26859e1bc276b8c51af574ce683b92a1c5b6f59ab513928eb51',
    'report_bigamounts': 'dc1c4a14230060750191ce04822912e0b2e0cf2db6e9af05164539c7dfa8e195',
    'report_name_edge_cases': '2f5532e59aae7986788b4f1d4131c800a99cee04f860af4269eec47c635a7fe3',
    'report_company_special': '236f80b1cf852f15df0e5fab24cbd93eccf24605a00ab9a22eb7e899bd8202fe',
}


def test_the_cases_and_the_snapshots_name_the_same_reports():
    assert set(OPS_CASES) == set(OPS_SNAPSHOTS)


@pytest.mark.parametrize('case', sorted(OPS_CASES))
def test_the_bwa_pdf_prints_what_it_printed_before_the_blocks_moved_to_pdf_table(case, tmp_db):
    some_books(tmp_db)
    build, with_accounts, company = OPS_CASES[case]
    text = _ops_of(build(tmp_db), with_accounts, company)
    assert hashlib.sha256(text.encode('utf-8')).hexdigest() == OPS_SNAPSHOTS[case], case


def test_the_bwa_wrappers_call_the_shared_blocks_with_their_own_geometry():
    assert pdf_bwa._clean('a\nb\x00c') == pdf_table.clean('a\nb\x00c') == 'a b c'
    assert pdf_bwa._width('Summe', 7.5, '/F2') == pdf_table.width('Summe', 7.5, '/F2')
    assert pdf_bwa._width('Summe', 7.5, '/F1') == pdf_table.width('Summe', 7.5, '/F1')
    assert pdf_bwa._fit('W' * 400, 100, 7) == pdf_table.fit('W' * 400, 100, 7, pdf_bwa.MAX_LABEL_CHARS)
    assert pdf_bwa._cell(100, 50, 'x', right=True)[-1] == pdf_table.cell(100, 50, 'x', '/F1', pdf_bwa.ROW_SIZE, right=True)[-1]
    assert pdf_bwa._line(40) == pdf_table.hline(40, pdf_bwa.LEFT_X, pdf_bwa.RIGHT_X)
    assert pdf_bwa.page_layout([3, 50, 7, 90]) == pdf_table.page_layout([3, 50, 7, 90], pdf_bwa.LINES_PER_PAGE)


# ═══ page_layout ══════════════════════════════════════════════════════════════
def test_page_layout_with_any_number_of_lines_keeps_every_row_and_splits_no_block_that_fits():
    """Eigenschaftsprüfung mit festem Seed: Keine Seite hat mehr Zeilen als erlaubt, keine Zeile geht verloren, keine Seite ist leer (außer ohne
    Blöcke), und ein Block, der auf eine Seite passt, steht ganz auf einer – nur ein längerer läuft über, beginnt aber oben auf einer neuen."""
    rng = random.Random(2516)
    for _ in range(500):
        per_page = rng.randint(1, 60)
        heights = [rng.choice((1, 2, 3, 5, 8, 13, 30, 59, 61, 130)) for _ in range(rng.randint(0, 12))]
        layout = pdf_table.page_layout(heights, per_page)
        assert all(sum(page) <= per_page for page in layout), (per_page, heights, layout)
        assert sum(map(sum, layout)) == sum(heights)
        assert all(page for page in layout) or layout == [[]]
        pieces = [piece for page in layout for piece in page]
        index = 0
        for height in heights:                                            # die Stücke jedes Blocks folgen aufeinander, in der Summe seine Höhe
            taken, count = 0, 0
            while taken < height:
                taken += pieces[index + count]
                count += 1
            assert taken == height, (heights, layout)
            if height <= per_page:
                assert count == 1, (per_page, heights, layout)               # passt auf eine Seite: nicht geteilt
            index += count
        assert index == len(pieces)


def test_page_layout_treats_a_block_of_no_height_as_one_line():
    assert pdf_table.page_layout([0, -4], 10) == [[1, 1]] and pdf_table.page_layout([], 10) == [[]]


def test_a_block_longer_than_a_page_starts_on_a_new_one_and_runs_over():
    assert pdf_table.page_layout([3, 25, 2], 10) == [[3], [10], [10], [5, 2]]
    assert pdf_table.page_layout([20], 10) == [[10], [10]]
    assert pdf_table.page_layout([10, 1], 10) == [[10], [1]]


# ═══ Text: clean, fit, wrap ═══════════════════════════════════════════════════
def test_clean_turns_control_characters_and_runs_of_blanks_into_single_spaces():
    assert pdf_table.clean('Zeile eins\nZeile zwei\tEnde\r\x00\x07 Schluss') == 'Zeile eins Zeile zwei Ende Schluss'
    assert pdf_table.clean(None) == '' and pdf_table.clean('  a   b  ') == 'a b'
    assert pdf_table.clean('→') == '->' and pdf_table.clean('€ ß Ä') == '€ ß Ä'          # Ersatz nach der Tabelle des PDF-Baus, WinAnsi bleibt
    assert pdf_table.clean('日本') == '??'                                                 # was die Schrift nicht kennt, steht als ? da


def test_fit_keeps_what_fits_and_cuts_the_rest_with_an_ellipsis():
    assert pdf_table.fit('Bürobedarf', 200, 8) == 'Bürobedarf' and pdf_table.fit(None, 200, 8) == ''
    for text in ('W' * 400, 'Wort ' * 300, 'ß' * 190):
        cut = pdf_table.fit(text, 120, 8)
        assert cut.endswith('…') and info_width(cut, 8) <= 120
    assert pdf_table.fit('a' * 50, 1000, 8, max_chars=10) == 'a' * 10 + '…'                  # nur so viele Zeichen werden betrachtet
    assert pdf_table.fit('x' * 10, 1000, 8, max_chars=10) == 'x' * 10                      # genau so viele Zeichen wie erlaubt: nicht gekürzt


def test_wrap_breaks_at_word_boundaries_and_no_line_is_wider_than_the_column():
    text = 'Aus den Buchungen dieser App nach Zahlungsdatum gerechnet. Kein amtliches Formular; keine Steuerberatung, ersetzt nicht ' * 3
    for width in (60, 120, 250, 495):
        lines = pdf_table.wrap(text, width, 7)
        assert lines and all(info_width(line, 7) <= width for line in lines), width
        assert ' '.join(lines) == ' '.join(text.split())                                  # nichts fällt weg, nichts kommt dazu


def test_wrap_splits_a_word_that_is_wider_than_the_column_and_keeps_every_character():
    text = 'kurz ' + 'Donaudampfschifffahrtsgesellschaft' * 3 + ' Ende'
    lines = pdf_table.wrap(text, 60, 8)
    assert len(lines) > 3 and all(info_width(line, 8) <= 60 for line in lines)
    assert ''.join(lines).replace(' ', '') == text.replace(' ', '')


def test_wrap_reads_whitespace_and_control_characters_as_one_space_and_an_empty_text_has_no_line():
    assert pdf_table.wrap('a\nb\t c\x00d', 500, 8) == ['a b c d']
    assert pdf_table.wrap('', 100, 8) == [] and pdf_table.wrap(None, 100, 8) == [] and pdf_table.wrap('  \n ', 100, 8) == []


def test_wrap_stops_after_the_limit_with_an_ellipsis_on_the_last_line():
    lines = pdf_table.wrap('wort ' * 5000, 50, 8)
    assert len(lines) == 200 and lines[-1].endswith('…') and all(info_width(line, 8) <= 50 for line in lines)
    assert not any(line.endswith('…') for line in lines[:-1])
    few = pdf_table.wrap('wort ' * 5000, 50, 8, max_lines=3)
    assert len(few) == 3 and few[-1].endswith('…')
    assert len(pdf_table.wrap('x' * 100_000, 100, 8)) == 200                                  # ein einziges langes Wort: dieselbe Grenze
    exact = pdf_table.wrap(' '.join(['ab'] * 40), 14, 8, max_lines=200)
    assert not any(line.endswith('…') for line in exact)                                      # was in die Grenze passt, bleibt unmarkiert


# ═══ Zelle und Linie ══════════════════════════════════════════════════════════
def test_a_cell_is_a_font_selection_and_a_text_matrix_and_gray_text_is_wrapped_in_color_operators():
    assert pdf_table.cell(50, 700, 'Konto', '/F1', 8) == ['/F1 8 Tf', '1 0 0 1 50.0 700.0 Tm (Konto) Tj']
    assert pdf_table.cell(50, 700, 'x', '/F2', 7.5, gray=True) == ['0.5 g', '/F2 7.5 Tf', '1 0 0 1 50.0 700.0 Tm (x) Tj', '0 g']
    assert pdf_table.cell(50, 700, '(a\\b)', '/F1', 8)[-1] == '1 0 0 1 50.0 700.0 Tm (\\(a\\\\b\\)) Tj'           # Klammern und Rückstrich maskiert


def test_a_right_aligned_cell_ends_on_the_given_edge():
    for font in ('/F1', '/F2'):
        for text in ('1,00', '-1.234.567,89', '98.765.432,10'):
            [_font, matrix] = pdf_table.cell(500, 700, text, font, 8, right=True)
            x = float(matrix.split()[4])
            assert abs(x + pdf_table.width(text, 8, font) - 500) < 0.06, (font, text)           # auf 0,1 pt gerundet


def test_a_line_runs_from_edge_to_edge():
    assert pdf_table.hline(746, 50, 545) == 'q 0.5 w 50.0 746.0 m 545.0 746.0 l S Q'
