# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""PDF der Summen- und Saldenliste (2.5.17): A4 quer, mehrseitig, nichts auf der Platte.

* Querformat (842 × 595), Titel, Firma, Zeitraum mit Daten und die Blockköpfe (Monat, aufgelaufen) stehen im Text; ohne Namen entfällt die Firmenzeile
* viele Konten laufen über mehrere Seiten: Kopf und Fuß auf jeder, „Seite n von m“, keine Zeile unter dem Rand; eine Gruppe (Überschrift, Konten,
  Summe) wird nicht geteilt, solange sie auf eine Seite passt – nur eine Gruppe über eine Seite läuft weiter
* Beträge liegen rechtsbündig auf den Kanten der Spalten (gemessen mit pdfplumber, nicht mit den Funktionen des PDF-Baus); Salden tragen ihr S oder H
* der Hinweisblock und die Erläuterungen sind auf die ganze Breite umbrochen: keine Zeile über der Breite, kein Hinweis fällt weg
* Klammern, Rückstriche, Umlaute und Steuerzeichen in Namen ergeben einen gültigen Text; überlange Namen werden mit „…“ gekürzt
* die Zahlen des PDF sind die der Seite; der Download (``GET /susa/pdf``) ist ein Anhang, schreibt nichts

Der Aufbau des Berichts und die Zahlen prüft ``test_susa.py``. Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import datetime
import io
import re
from urllib.parse import quote

import pdfplumber
import pytest
from pdfminer.pdfpage import PDFPage

import userctx
from db import susa
from export import pdf_susa
from export.pdf_susa import (BOTTOM_Y, COLUMN_WIDTH, COMPANY_Y, FOOTER_Y, LABEL_RIGHT, LEFT_X, LINES_PER_PAGE, MAX_LABEL_CHARS, NAME_RESERVE, NAME_X,
                             PAGE_SIZE, RIGHT_X, ROW_H, TABLE_TOP, generate_susa_pdf, page_layout)
from export.pdf_worktime import info_width
from tests import susa_helpers as helpers
from tests.http_client import get
from tests.installation import dump, tree
from tests.test_bwa import demo_db, world  # noqa: F401  (Fixtures: die Testwelt und der Demobestand)
from tests.test_bwa_pdf import _cells, _streams
from tests.test_pdfa import _doc
from tests.test_susa import P, table_rows

CREATED = datetime.date(2026, 11, 21)          # „Erstellt am“: ein fester Tag, nie die Uhr
EDGES = [LABEL_RIGHT + COLUMN_WIDTH * (index + 1) for index in range(6)]
BASES = {'fixed_assets': 100, 'current_assets': 1100, 'equity': 2200, 'liabilities': 3100, 'revenue': 4100, 'expenses': 6000, 'other': 8100,
         'personal': 10000}


def report_with(group_sizes, notices=(), year=2026, month=3, base=12_345):
    """Ein Bericht ohne Datenbank: je Gruppe so viele Konten, wie ``group_sizes`` nennt, jedes mit einem Betrag im Monat (die Gegenseite steht auf
    „Nicht kontiert“)."""
    postings, names = [], {}
    for key, count in group_sizes.items():
        for index in range(count):
            number = BASES[key] + index
            names[number] = (f'Konto {number}', False)
            postings += [('month', P(number, base * (index + 1))), ('month', P(susa.NOT_POSTED, 0, base * (index + 1)))]
    return susa.build_susa(year, month, postings, names, list(notices), [], [])


def pdf_of(report, company='Eigene Firma GmbH'):
    return generate_susa_pdf(report, company, CREATED)


def page_cells(pdf):
    return [_cells(stream) for stream in _streams(pdf)]


def table_lines(cells):
    """Die Zeilen der Tabelle einer Seite von oben nach unten: die Texte je Zeile, von links nach rechts."""
    by_line = {}
    for x, y, text in cells:
        if BOTTOM_Y <= y <= TABLE_TOP:
            by_line.setdefault(y, []).append((x, text))
    return [tuple(text for _x, text in sorted(by_line[y])) for y in sorted(by_line, reverse=True)]


def all_lines(pdf):
    return [row for cells in page_cells(pdf) for row in table_lines(cells)]


# ═══ Seitenverteilung ═════════════════════════════════════════════════════════
def test_the_lines_per_page_end_above_the_margin():
    assert LINES_PER_PAGE == 40
    assert TABLE_TOP - (LINES_PER_PAGE - 1) * ROW_H >= BOTTOM_Y > TABLE_TOP - LINES_PER_PAGE * ROW_H
    assert page_layout([]) == [[]] and page_layout([3, LINES_PER_PAGE - 2, 2]) == [[3], [LINES_PER_PAGE - 2, 2]]


def test_the_columns_start_right_of_the_labels_and_end_at_the_right_edge():
    assert LEFT_X < NAME_X < LABEL_RIGHT < RIGHT_X and abs(EDGES[-1] - RIGHT_X) < 1e-9 and COLUMN_WIDTH > 80


# ═══ Kopf, Fuß, Inhalt ════════════════════════════════════════════════════════
def test_the_pdf_is_landscape_and_names_the_company_the_period_and_the_blocks():
    pdf = pdf_of(report_with({'current_assets': 3, 'expenses': 2}))
    pages = list(PDFPage.create_pages(_doc(pdf)))
    assert [tuple(int(value) for value in page.mediabox) for page in pages] == [(0, 0) + PAGE_SIZE] == [(0, 0, 842, 595)]
    [cells] = page_cells(pdf)
    texts = [text for _x, _y, text in cells]
    assert susa.TITLE in texts and 'Eigene Firma GmbH' in texts and 'Erstellt am 21.11.2026' in texts
    assert 'Zeitraum: 01.03.2026 bis 31.03.2026 · aufgelaufen seit 01.01.2026' in texts
    assert 'März 2026' in texts and 'Januar bis März 2026' in texts
    assert [text for text in texts if text in susa.COLUMN_LABELS] == list(susa.COLUMN_LABELS)
    assert 'Konto' in texts and 'Bezeichnung' in texts and susa.SUSA_FOOTER in texts
    assert not [text for text in texts if text.startswith('Seite ')]                       # eine Seite: keine Seitenzahl
    info = _doc(pdf).info[0]
    assert info['Title'] == susa.TITLE.encode('ascii') and info['Author'] == b'Eigene Firma GmbH'                 # nur ASCII: ohne UTF-16-Kennung
    assert b'/F1 ' in pdf and b'/F2 ' in pdf


def test_january_names_the_month_once_and_the_accumulated_span_with_its_word():
    [cells] = page_cells(pdf_of(report_with({'expenses': 1}, month=1)))
    texts = [text for _x, _y, text in cells]
    assert 'Januar 2026' in texts and 'Januar 2026 (aufgelaufen)' in texts
    assert 'Zeitraum: 01.01.2026 bis 31.01.2026 · aufgelaufen seit 01.01.2026' in texts


def test_a_leap_year_february_ends_on_the_29th():
    [cells] = page_cells(pdf_of(report_with({'expenses': 1}, year=2028, month=2)))
    assert 'Zeitraum: 01.02.2028 bis 29.02.2028 · aufgelaufen seit 01.01.2028' in [text for _x, _y, text in cells]


def test_each_block_header_stands_centred_over_its_two_columns():
    [cells] = page_cells(pdf_of(report_with({'expenses': 1})))
    centres = {text: x + info_width(text, 8) / 2 for x, y, text in cells if text in ('März 2026', 'Januar bis März 2026')}
    assert abs(centres['März 2026'] - (EDGES[0] + EDGES[2]) / 2) < 8 and abs(centres['Januar bis März 2026'] - (EDGES[2] + EDGES[4]) / 2) < 8


def test_without_a_company_name_the_company_line_is_left_out():
    [cells] = page_cells(pdf_of(report_with({'expenses': 1}), company=''))
    assert all(y != COMPANY_Y for _x, y, _text in cells)
    [cells] = page_cells(pdf_of(report_with({'expenses': 1})))
    assert any(y == COMPANY_Y for _x, y, _text in cells)


def test_the_pdf_prints_the_groups_the_accounts_the_sums_and_the_total_in_order():
    rows = all_lines(pdf_of(report_with({'current_assets': 2, 'expenses': 1})))
    total = [row[0] for row in rows].index('Gesamtsumme')
    assert rows[:total + 1] == [
        ('Umlaufvermögen und Finanzkonten',), ('1100', 'Konto 1100', '1,23', '1,23', '1,23 S'), ('1101', 'Konto 1101', '2,47', '2,47', '2,47 S'),
        ('Summe Umlaufvermögen und Finanzkonten', '0,00', '3,70', '0,00', '3,70', '0,00', '3,70 S'),
        ('Aufwand und übrige Erfolgskonten',), ('6000', 'Konto 6000', '1,23', '1,23', '1,23 S'),
        ('Summe Aufwand und übrige Erfolgskonten', '0,00', '1,23', '0,00', '1,23', '0,00', '1,23 S'),
        ('Nicht kontiert',), ('Nicht kontiert (Gegenseite fehlt)', '4,94', '4,94', '4,94 H'),
        ('Summe Nicht kontiert', '0,00', '0,00', '4,94', '0,00', '4,94', '4,94 H'),
        ('Gesamtsumme', '0,00', '4,94', '4,94', '4,94', '4,94', '0,00')]


def test_the_virtual_account_prints_no_number_and_a_dimmed_name():
    pdf = pdf_of(report_with({'expenses': 1}))
    [stream] = _streams(pdf)
    lines = stream.split('\n')
    index = next(position for position, line in enumerate(lines) if 'Nicht kontiert \\(Gegenseite fehlt\\)' in line)
    assert lines[index - 2:index - 0] == ['0.5 g', '/F1 7 Tf'] and lines[index + 1] == '0 g'                # gedämpft: grauer Text
    assert re.match(r'1 0 0 1 80\.0 [0-9.]+ Tm', lines[index])                                          # bei NAME_X, ohne Nummer davor
    row = next(row for row in all_lines(pdf) if row[0] == 'Nicht kontiert (Gegenseite fehlt)')
    assert not re.fullmatch(r'\d+', row[0])


def test_the_total_row_prints_all_six_values_even_when_they_are_zero():
    assert all_lines(pdf_of(report_with({})))[0] == ('Gesamtsumme', '0,00', '0,00', '0,00', '0,00', '0,00', '0,00')


def test_the_pdf_of_every_month_of_the_test_world_and_of_months_of_the_demo_is_built(world, demo_db):
    for db, months in ((world, [(2026, month) for month in range(1, 13)]), (demo_db, [(2025, 1), (2025, 12), (2026, 6), (2026, 11)])):
        for year, month in months:
            assert pdf_of(db.get_susa(year, month)).startswith(b'%PDF-')


# ═══ Mehrere Seiten ═════════════════════════════════════════════════════════════
def test_many_accounts_run_over_several_pages_and_nothing_lies_below_the_margin_but_the_footer():
    report = report_with({'current_assets': 10, 'equity': 40, 'liabilities': 40, 'expenses': 130, 'personal': 4})
    pages = page_cells(pdf_of(report))
    total = len(pages)
    assert total >= 6
    for number, cells in enumerate(pages, start=1):
        below = sorted((y, text) for _x, y, text in cells if y < BOTTOM_Y)
        assert below == sorted([(FOOTER_Y, susa.SUSA_FOOTER), (FOOTER_Y, f'Seite {number} von {total}')]), number
        texts = [text for _x, _y, text in cells]
        assert susa.TITLE in texts and 'Eigene Firma GmbH' in texts and 'Anfangssaldo' in texts and 'Endsaldo' in texts
        assert 'Zeitraum: 01.03.2026 bis 31.03.2026 · aufgelaufen seit 01.01.2026' in texts
    printed = [row for row in all_lines(pdf_of(report)) if re.fullmatch(r'\d{4,5}', row[0])]
    expected = [str(account.number) for group in susa.visible_groups(report) for account in susa.visible_accounts(group) if not account.virtual]
    assert [row[0] for row in printed] == expected and len(expected) == 224                      # jedes Konto genau einmal, in der Reihenfolge


def test_no_group_is_split_where_it_fits_on_a_page():
    """Je Gruppe 30 Konten: 32 Zeilen hoch (Überschrift, Konten, Summe), eine je Seite – die erste Zeile jeder Seite ist die Überschrift einer Gruppe."""
    report = report_with({'current_assets': 30, 'equity': 30, 'liabilities': 30})
    pages = page_cells(pdf_of(report))
    first_rows = [table_lines(cells)[0] for cells in pages]
    assert len(pages) >= 3 and first_rows[:3] == [('Umlaufvermögen und Finanzkonten',), ('Eigenkapital und Privat',),
                                                  ('Rückstellungen, Verbindlichkeiten und Steuern',)]


def test_one_group_longer_than_a_page_starts_on_a_new_page_and_runs_over():
    pages = page_cells(pdf_of(report_with({'fixed_assets': 2, 'expenses': 130})))
    first_rows = [table_lines(cells)[0] for cells in pages]
    assert first_rows[0] == ('Anlagevermögen',) and first_rows[1] == ('Aufwand und übrige Erfolgskonten',)
    assert re.fullmatch(r'\d{4}', first_rows[2][0])                                           # und läuft auf der nächsten mit einem Konto weiter


def test_the_total_is_a_block_of_its_own_and_the_text_follows_it():
    rows = all_lines(pdf_of(report_with({'expenses': 3}, notices=[susa.SusaNotice('legacy_mirror', 2, 1_000_000)])))
    names = [row[0] for row in rows]
    assert names.index('Gesamtsumme') < names.index('Hinweise zu den Buchungen bis zum 31.03.2026')


# ═══ Beträge ════════════════════════════════════════════════════════════════
def test_every_amount_and_every_balance_ends_on_the_edge_of_its_column():
    """Gemessen mit pdfplumber (den Breiten der eingebetteten Schrift im PDF), nicht mit den Funktionen des PDF-Baus. Ein Saldo endet mit seinem S oder H
    an der Kante, die Zahl davor steht links daneben."""
    big, year, month = helpers.minor('987654321.00'), helpers.minor('123456789.00'), helpers.minor('1234567.80')
    postings = [('prior', P(1800, big)), ('prior', P(2100, 0, big)), ('year', P(6815, year)), ('year', P(1800, 0, year)),
                ('month', P(6850, month)), ('month', P(1800, 0, month))]
    names = {1800: ('Bank', False), 2100: ('Privat', False), 6815: ('Bürobedarf', False), 6850: ('Betriebsbedarf', False)}
    with pdfplumber.open(io.BytesIO(pdf_of(susa.build_susa(2026, 3, postings, names, [], [], [])))) as document:
        words = [word for page in document.pages for word in page.extract_words() if word['x0'] > LABEL_RIGHT - 1]
    letters = [word for word in words if word['text'] in ('S', 'H')]
    amounts = [word for word in words if re.fullmatch(r'-?\d{1,3}(?:\.\d{3})*,\d\d', word['text'])]
    assert len(letters) >= 6 and len(amounts) >= 12
    for word in letters:
        assert min(abs(word['x1'] - EDGES[0]), abs(word['x1'] - EDGES[5])) < 0.6, (word['text'], word['x1'])
    for word in amounts:
        followed = any(other['text'] in ('S', 'H') and abs(other['top'] - word['top']) < 1 and 0 < other['x0'] - word['x1'] < 6 for other in letters)
        if not followed:                                                  # ein Betrag, eine Null im Saldo (ohne S und H) oder ein Wert der Summenzeile
            assert min(abs(word['x1'] - edge) for edge in EDGES) < 0.6, (word['text'], word['x1'])
    texts = {word['text'] for word in amounts}
    assert {'987.654.321,00', '123.456.789,00', '1.234.567,80', '124.691.356,80', '862.962.964,20'} <= texts


# ═══ Text ═══════════════════════════════════════════════════════════════════
LONG_NOTICES = [susa.SusaNotice('unbooked', 3, None, 238_000_000, 11_115_000), susa.SusaNotice('missing_side', 5, 410_000),
                susa.SusaNotice('legacy_mirror', 4, 1_150_000), susa.SusaNotice('no_form', 11, 1_650_000), susa.SusaNotice('doubtful', 2)]


def test_the_notices_and_the_explanations_are_wrapped_to_the_whole_width_and_lose_nothing():
    pdf = pdf_of(report_with({'expenses': 2}, notices=LONG_NOTICES))
    names = [row[0] for row in all_lines(pdf)]
    block = names[names.index('Gesamtsumme') + 1:]
    assert block[0] == 'Hinweise zu den Buchungen bis zum 31.03.2026'
    joined = ' '.join(block[1:])
    for notice in LONG_NOTICES:
        assert '– ' + susa.notice_text(notice) in joined, notice.key
    for paragraph in (susa.SUSA_HINT_TEXT + ' ' + susa.SALDO_TEXT, susa.OPENING_TEXT, susa.NOT_POSTED_TEXT, susa.TRANSFER_TEXT, susa.TAX_TEXT,
                      susa.EUER_DIFF_TEXT):
        assert paragraph in joined, paragraph[:30]
    assert len(block) > 15 and all(info_width(line, pdf_susa.ROW_SIZE) <= RIGHT_X - LEFT_X + 0.01 for line in block)            # keine Zeile über der Breite


def test_the_wrapped_lines_of_every_text_read_back_as_the_text_itself():
    """Die Zeilen eines Absatzes, mit einem Leerzeichen verbunden, sind der Absatz."""
    rows = pdf_susa._text_rows(susa.OPENING_TEXT)
    assert len(rows) >= 2 and ' '.join(row.label for row in rows) == susa.OPENING_TEXT
    assert all(info_width(row.label, pdf_susa.ROW_SIZE) <= pdf_susa.TEXT_WIDTH for row in rows)
    assert pdf_susa._text_rows(susa.NOT_POSTED_TEXT, prefix='– ')[0].label.startswith('– „Nicht kontiert“ nimmt')


def test_a_very_long_notice_ends_with_an_ellipsis_instead_of_running_on_forever():
    rows = pdf_susa._text_rows('Wort ' * 5000)
    assert len(rows) == pdf_susa.MAX_TEXT_LINES and rows[-1].label.endswith('…')


def test_the_footer_lines_fit_next_to_the_page_number():
    assert info_width(susa.SUSA_FOOTER, 7) + info_width('Seite 99 von 99', 7) + 20 < RIGHT_X - LEFT_X


# ═══ Namen ══════════════════════════════════════════════════════════════════
def test_a_name_with_brackets_a_backslash_and_umlauts_prints_unchanged():
    name = '(Test) Bürobedarf\\Ä – € ß'
    report = susa.build_susa(2026, 3, [('month', P(6815, 100_000)), ('month', P(1800, 0, 100_000))], {6815: (name, False), 1800: ('Bank', False)},
                             [], [], [])
    pdf = pdf_of(report, company='Muster (Test) GmbH \\ Co. KG')
    [cells] = page_cells(pdf)
    texts = [text for _x, _y, text in cells]
    assert name in texts and 'Muster (Test) GmbH \\ Co. KG' in texts
    assert _doc(pdf).info[0]['Author'] == b'Muster (Test) GmbH \\ Co. KG'


def test_control_characters_in_a_name_do_not_break_the_stream():
    report = susa.build_susa(2026, 3, [('month', P(6815, 100_000)), ('month', P(1800, 0, 100_000))],
                             {6815: ('Zeile eins\nZeile zwei\tEnde\r\x00\x07 Schluss', False), 1800: ('Bank', False)}, [], [], [])
    [stream] = _streams(pdf_of(report))
    assert 'Zeile eins Zeile zwei Ende Schluss' in [text for _x, _y, text in _cells(stream)]
    assert not re.search(r'[\x00-\x08\x0b-\x1f\x7f]', stream.replace('\n', ' '))


def test_an_account_without_a_name_prints_number_and_amounts_and_never_none():
    report = susa.build_susa(2026, 3, [('month', P(6815, 100_000)), ('month', P(1800, 0, 100_000))], {6815: (None, False), 1800: ('', False)}, [], [], [])
    [cells] = page_cells(pdf_of(report))
    assert not any('None' in text for _x, _y, text in cells)
    assert any(row[0] == '6815' and row[-1] == '10,00 S' for row in table_lines(cells))


def test_overlong_names_are_cut_with_an_ellipsis_to_the_width_of_their_place():
    names = {6815: ('W' * 400, False), 6850: ('Wort ' * 300, False), 6805: ('ß' * 190, False), 1800: ('Bank', False)}
    postings = [('month', P(number, 100_000)) for number in (6815, 6850, 6805)] + [('month', P(1800, 0, 300_000))]
    pdf = pdf_of(susa.build_susa(2026, 3, postings, names, [], [], []), company='Firma ' * 100)
    [cells] = page_cells(pdf)
    cut = [text for x, y, text in cells if x == NAME_X and BOTTOM_Y <= y <= TABLE_TOP and text.endswith('…')]
    assert len(cut) == 3
    for name in cut:
        assert info_width(name, pdf_susa.ACCOUNT_SIZE) <= LABEL_RIGHT - NAME_RESERVE - NAME_X, name
    [company] = [text for x, y, text in cells if y == COMPANY_Y]
    assert company.endswith('…') and info_width(company, 10) <= RIGHT_X - LEFT_X and MAX_LABEL_CHARS == 200


# ═══ Die Zahlen sind die der Seite ══════════════════════════════════════════
@pytest.mark.parametrize('fixture, year, month', [('world', 2026, 12), ('world', 2026, 3), ('demo_db', 2026, 10), ('demo_db', 2025, 12)])
def test_the_numbers_of_the_pdf_are_those_of_the_page(fixture, year, month, request):
    """Dieselben Zeilen mit denselben Beträgen; ein Name, den das PDF auf seine Spalte kürzt, endet dort mit „…“ und beginnt wie der der Seite."""
    from server.pages_susa import PageSusa, SusaQuery
    db = request.getfixturevalue(fixture)
    printed = all_lines(pdf_of(db.get_susa(year, month)))
    printed = printed[:[row[0] for row in printed].index('Gesamtsumme') + 1]
    page_rows = [tuple(part.strip() for part in row.split(' | ') if part.strip()) for row in table_rows(PageSusa(db, SusaQuery(year, month)))[2:]]
    assert len(printed) == len(page_rows) and len(page_rows) > 10
    for found, expected in zip(printed, page_rows):
        assert len(found) == len(expected), (found, expected)
        for cell, shown in zip(found, expected):
            assert cell == shown or (cell.endswith('…') and shown.startswith(cell[:-1])), (found, expected)


# ═══ Der Download ═══════════════════════════════════════════════════════════
def test_the_pdf_route_answers_with_a_download_that_carries_company_and_total_and_writes_nothing(server_port, auto_login):
    db = auto_login.db
    db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Firma GmbH', company_name='Eigene Firma GmbH')
    helpers.build_susa_world(db)
    before_db, before_tree = dump(db.db_name), tree(userctx.DATA_ROOT)
    response = get(server_port, '/susa/pdf?year=2026&month=12')
    assert response.status == 200 and response.headers['Content-Type'] == 'application/pdf' and response.body.startswith(b'%PDF-')
    assert response.headers['Content-Disposition'].startswith('attachment;') and 'Summen_und_Salden_2026-12.pdf' in response.headers['Content-Disposition']
    assert response.headers['X-Filename'] == quote('Summen_und_Salden_2026-12.pdf', safe='')
    assert response.headers.get('Set-Cookie') is None
    texts = [text for stream in _streams(response.body) for _x, _y, text in _cells(stream)]
    assert 'Eigene Firma GmbH' in texts and 'Gesamtsumme' in texts and 'Zeitraum: 01.12.2026 bis 31.12.2026 · aufgelaufen seit 01.01.2026' in texts
    assert dump(db.db_name) == before_db and tree(userctx.DATA_ROOT) == before_tree
