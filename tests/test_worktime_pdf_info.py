# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Info-Spalte des Stundenzettels: Notiz und Arbeitsort auf Wunsch, umbrochen statt abgeschnitten (seit 2.5.3).

Der Fehlfall: Die Spalte blieb bei gewöhnlichen Arbeitstagen leer, weil die Notiz nur mit einem Häkchen gedruckt wurde, das
beim Laden aus war; eine lange Notiz endete nach 70 Zeichen ohne ein Zeichen dafür, und den Arbeitsort, den die Liste der
Seite unter „Info“ zeigt, nannte das PDF nie.

* Beide Schalter der Seite („Mit Notiz“, „Mit Arbeitsort“) sind beim Laden an; die Adresse nennt immer beide.
* Der Server druckt nur, was die Adresse verlangt (``notes``, ``location``); ohne Angabe keines von beiden.
* ``info_lines`` bricht am Wortrand, ein überbreites Wort mitten im Wort, und endet nach ``MAX_INFO_LINES`` mit „…“ –
  gemessen mit den Breiten der eingebetteten Schrift, in einer Laufzeit, die nicht von der Länge des Textes abhängt.
* Eine umbrochene Zeile ist entsprechend höher: Die Seitenverteilung rechnet mit Textzeilen, keine Zeile steht unter
  dem unteren Rand, kein Eintrag wird über zwei Seiten geteilt.

Alle Namen und Texte sind erfunden.
"""
import io
import re
import time

import pdfplumber
import pytest

from export.pdf_worktime import (BOTTOM_Y, COL_X, FOOTER_HEIGHT, INFO_WIDTH, LINES_PER_PAGE, MAX_INFO_LINES, PAGE_RIGHT_X,
                                 ROW_H, TABLE_TOP, generate_worktime_pdf, info_lines, info_width, page_layout,
                                 printed_text)
from server.pages_worktime import PageWorkTimes, _WORKTIME_JS
from tests.http_client import get
from tests.test_worktime_pdf_pages import MARCH, YEAR, _cells, _data_rows, _fill, _page_texts, _streams, _world

LONG_NOTE = ('Einrichtung der neuen Arbeitsplätze im Büro, Übernahme der Daten vom alten Rechner, Test der Drucker und '
             'Scanner, Einweisung in die neue Zeiterfassung und Abstimmung der offenen Punkte mit der Geschäftsführung')


def _info_cells(stream):
    return [(y, text) for x, y, text in _cells(stream) if x == COL_X['info'] and y <= TABLE_TOP]


def _pdf(db, world, period, **flags):
    return generate_worktime_pdf(db, world.person, *period, store=False, **flags)[0]


# ── Zeilenumbruch ─────────────────────────────────────────────────────────────
def test_a_short_text_is_one_line_and_an_empty_one_is_none():
    assert info_lines('Wartung vor Ort') == ['Wartung vor Ort']
    assert info_lines('') == [] and info_lines(None) == [] and info_lines('  \n ') == []


def test_a_long_text_breaks_at_words_and_loses_nothing():
    lines = info_lines(LONG_NOTE)
    assert len(lines) > 1 and ' '.join(lines) == LONG_NOTE
    assert all(info_width(line) <= INFO_WIDTH for line in lines)


def test_a_word_wider_than_the_column_breaks_inside_the_word():
    lines = info_lines('kurz ' + 'N' * 120 + ' Ende')
    assert ''.join(lines).replace(' ', '') == 'kurz' + 'N' * 120 + 'Ende'
    assert all(info_width(line) <= INFO_WIDTH for line in lines) and len(lines) >= 3


def test_line_breaks_and_runs_of_blanks_count_as_one_blank():
    assert info_lines('eins\r\nzwei\t\tdrei    vier') == ['eins zwei drei vier']


def test_after_the_last_allowed_line_an_ellipsis_says_that_text_is_missing():
    lines = info_lines('wort ' * 400)
    assert len(lines) == MAX_INFO_LINES and lines[-1].endswith(' …')
    assert all(info_width(line) <= INFO_WIDTH for line in lines)
    exact = info_lines(' '.join(lines[:-1] + ['Ende']))
    assert exact[-1] == 'Ende' and not any(line.endswith('…') for line in exact)     # genau passend: kein „…“


def test_the_running_time_does_not_depend_on_the_length_of_the_text():
    """Die Notiz ist ein unbegrenztes Textfeld. Gemessen und geschnitten wird nur, was gedruckt wird – ein Megabyte ohne
    Leerzeichen kostete mit einem Umbruch, der jedes Teilstück neu misst, Stunden."""
    for text in ('N' * 2_000_000, 'wort ' * 400_000, '漢' * 200_000, ('x' * 300 + ' ') * 5000):
        started = time.perf_counter()
        lines = info_lines(text)
        assert time.perf_counter() - started < 2
        assert len(lines) == MAX_INFO_LINES and lines[-1].endswith(' …')
        assert all(info_width(line) <= INFO_WIDTH for line in lines)


def test_the_text_is_measured_as_it_is_printed_with_the_widths_of_the_embedded_font():
    """Ein Geviertstrich ist in der Schrift fast doppelt so breit wie der Ersatzwert der Helvetica-Tabelle, und ein
    Pfeil steht als drei Zeichen im PDF – nach der alten Rechnung liefe eine solche Zeile weit über den Rand."""
    assert printed_text('a ↔ b → c') == 'a <-> b -> c' and printed_text('漢') == '?' and printed_text(None) == ''
    assert info_width('—') > 1.7 * info_width('n') and info_width('ß') > info_width('s')
    for text in ('—' * 300, '↔' * 300, 'ß·' * 300, 'Œ™‰' * 100):
        lines = info_lines(text)
        assert all(info_width(line) <= INFO_WIDTH for line in lines) and len(lines) > 1
    assert info_lines('a ↔ b') == ['a <-> b']


# ── Seitenverteilung mit Zeilenhöhen ──────────────────────────────────────────
@pytest.mark.parametrize('count', [0, 1, 44, 45, 53, 54, 107, 151])
def test_a_number_and_a_list_of_single_lines_are_laid_out_alike(count):
    assert page_layout(count) == page_layout([1] * count)


def test_a_row_is_never_split_and_no_text_line_falls_below_the_margin():
    assert LINES_PER_PAGE == 53
    assert page_layout([8] * 10) == [6, 4]                      # 6 × 8 = 48 Textzeilen; die siebte passte nicht mehr
    assert page_layout([1] * 50 + [8]) == [50, 1]               # 3 Zeilen wären frei, 8 werden gebraucht
    assert page_layout([1] * 45 + [8]) == [46, 0]               # 45 + 8 = 53 passen genau; Summe und Unterschrift nicht mehr
    assert page_layout([1] * 46 + [8]) == [46, 1]
    assert page_layout([LINES_PER_PAGE + 20]) == [1, 0]         # nie höher als eine Seite


# ── Das PDF ───────────────────────────────────────────────────────────────────
def test_note_and_location_are_printed_only_when_asked_for(tmp_db):
    world = _world(tmp_db)
    tmp_db.insert_worktime(world.person, '2026-03-02', start_time='08:00', end_time='16:30', pause_minutes=30,
                           location_mode='other', location_city='Hamburg', note='Server getauscht',
                           pause_text='12:00-12:30')
    tmp_db.insert_worktime(world.person, '2026-03-05', kind='vacation', location_city='Hamburg', note='Brückentag')

    def infos(**flags):
        return [text for _, text in _info_cells(_streams(_pdf(tmp_db, world, MARCH, **flags))[0])]

    assert infos() == ['Pause 12:00-12:30', 'Urlaub']
    assert infos(with_notes=True) == ['Pause 12:00-12:30 · Server getauscht', 'Urlaub · Brückentag']
    assert infos(with_location=True) == ['Hamburg · Pause 12:00-12:30', 'Urlaub']      # der Ort gehört zum Arbeitstag
    assert infos(with_notes=True, with_location=True) == ['Hamburg · Pause 12:00-12:30 · Server getauscht',
                                                           'Urlaub · Brückentag']


def test_a_long_note_wraps_in_its_column_and_pushes_the_next_row_down(tmp_db):
    world = _world(tmp_db)
    tmp_db.insert_worktime(world.person, '2026-03-03', start_time='08:00', end_time='12:00', note=LONG_NOTE)
    stream = _streams(_pdf(tmp_db, world, MARCH, with_notes=True))[0]
    cells = _cells(stream)
    info = _info_cells(stream)
    lines = info_lines(LONG_NOTE)
    assert [text for _, text in info] == lines and len(lines) >= 2
    top = info[0][0]
    assert [y for y, _ in info] == [top - number * ROW_H for number in range(len(lines))]
    rows = sorted(_data_rows(cells), reverse=True)
    third, fourth = rows[2], rows[3]                             # 3. und 4. März
    assert third == top and fourth == top - len(lines) * ROW_H   # der nächste Tag steht unter der letzten Notizzeile
    assert len(rows) == 31 and min(y for _, y, _ in cells) >= BOTTOM_Y      # auch Summe und Unterschrift, eine Seite


def test_many_long_notes_spill_onto_further_pages_without_splitting_an_entry(tmp_db):
    world = _world(tmp_db)
    _fill(tmp_db, world, 40, note=LONG_NOTE)
    pdf = _pdf(tmp_db, world, YEAR, with_notes=True)
    streams = _streams(pdf)
    height = len(info_lines(LONG_NOTE))
    assert len(streams) == len(page_layout([height] * 40)) > 1
    for stream in streams:
        info = _info_cells(stream)
        assert len(info) % height == 0                           # nur ganze Einträge je Seite
        assert all(y >= BOTTOM_Y for y, _ in info)
    assert sum(len(_data_rows(_cells(stream))) for stream in streams) == 40
    assert 'Summe (' in _page_texts(pdf)[-1] and all('Summe (' not in text for text in _page_texts(pdf)[:-1])


def _right_edges(pdf):
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        return [word['x1'] for page in document.pages for word in page.extract_words()]


@pytest.mark.parametrize('note', ['—' * 400, 'Maßstäbe · Größe · ' * 30, '↔ ' * 200, LONG_NOTE])
def test_no_printed_line_runs_past_the_right_margin(tmp_db, note):
    world = _world(tmp_db)
    tmp_db.insert_worktime(world.person, '2026-03-03', start_time='08:00', end_time='12:00', note=note)
    assert max(_right_edges(_pdf(tmp_db, world, MARCH, with_notes=True))) <= PAGE_RIGHT_X + 1


def test_brackets_and_backslashes_stay_text_wherever_the_line_breaks(tmp_db):
    """Maskiert wird jede Zeile für sich, nach dem Umbruch: Keine Folge wird an der Umbruchstelle zerrissen."""
    world = _world(tmp_db)
    note = ' '.join('(Teil %d) \\ ) Tj ET BT (' % number for number in range(1, 40))
    tmp_db.insert_worktime(world.person, '2026-03-03', start_time='08:00', end_time='12:00', note=note)
    pdf = _pdf(tmp_db, world, MARCH, with_notes=True)
    info = [text for _, text in _info_cells(_streams(pdf)[0])]
    assert info == info_lines(note) and len(info) == MAX_INFO_LINES
    assert len(_data_rows(_cells(_streams(pdf)[0]))) == 31                  # der Rest der Seite steht, wo er stand


@pytest.mark.parametrize('entries, pages', [(11, 1), (12, 2)])
def test_sum_and_signature_keep_their_place_below_wrapped_rows(tmp_db, entries, pages):
    """Vier Textzeilen je Eintrag: Elf Einträge (44 Zeilen) lassen Platz für Summe und Unterschrift, zwölf nicht mehr –
    dann stehen beide zusammen auf einer weiteren Seite."""
    world = _world(tmp_db)
    note = ' '.join(['Wort'] * 150)
    lines = info_lines(note, max_lines=4)
    assert len(lines) == 4
    four = ' '.join(lines[:3]) + ' ' + lines[3][:-2]                       # genau vier Zeilen, ohne „…“
    assert len(info_lines(four)) == 4 and not info_lines(four)[-1].endswith('…')
    _fill(tmp_db, world, entries, note=four)
    pdf = _pdf(tmp_db, world, YEAR, with_notes=True)
    streams = _streams(pdf)
    assert len(streams) == pages == len(page_layout([4] * entries))
    assert TABLE_TOP - 44 * ROW_H - FOOTER_HEIGHT >= BOTTOM_Y > TABLE_TOP - 48 * ROW_H - FOOTER_HEIGHT
    last = _cells(streams[-1])
    assert any(text.startswith('Summe (') for _, _, text in last)
    assert min(y for _, y, text in last if not text.startswith('Seite ')) >= BOTTOM_Y


def test_without_notes_the_layout_is_the_one_before(tmp_db):
    """Ohne Schalter ändert sich für einen Zettel mit kurzen Pausenangaben nichts: jede Zeile eine Textzeile."""
    world = _world(tmp_db)
    _fill(tmp_db, world, 60, note=LONG_NOTE)
    assert len(_streams(_pdf(tmp_db, world, YEAR))) == len(page_layout(60)) == 2


# ── Seite und Route ───────────────────────────────────────────────────────────
def test_both_switches_start_checked_and_the_address_always_names_both(tmp_db):
    world = _world(tmp_db)
    html = PageWorkTimes(tmp_db, person_id=world.person, date_from=MARCH[0], date_to=MARCH[1])
    for element_id, label in (('wtPdfNotes', 'Mit Notiz'), ('wtPdfLocation', 'Mit Arbeitsort')):
        assert re.search(r'<input type="checkbox" id="%s" checked> %s</label>' % (element_id, label), html)
    assert "url += '&notes=' + (document.getElementById('wtPdfNotes').checked ? '1' : '0');" in _WORKTIME_JS
    assert "url += '&location=' + (document.getElementById('wtPdfLocation').checked ? '1' : '0');" in _WORKTIME_JS


def test_the_route_prints_what_the_address_asks_for(server_port, auto_login):
    world = _world(auto_login.db)
    auto_login.db.insert_worktime(world.person, '2026-03-02', start_time='08:00', end_time='16:30',
                                  location_mode='other', location_city='Hamburg', note='Server getauscht')
    base = f'/worktime/pdf?person={world.person}&from={MARCH[0]}&to={MARCH[1]}'

    def infos(query):
        response = get(server_port, base + query)
        assert response.status == 200 and response.headers['Content-Type'] == 'application/pdf'
        return [text for _, text in _info_cells(_streams(response.body)[0])]

    assert infos('') == [] == infos('&notes=0&location=0')
    assert infos('&notes=1&location=0') == ['Server getauscht']
    assert infos('&notes=0&location=1') == ['Hamburg']
    assert infos('&notes=1&location=1') == ['Hamburg · Server getauscht']
    assert infos('&notes=ja&location=2') == []                  # nur 1/true/on schalten ein
