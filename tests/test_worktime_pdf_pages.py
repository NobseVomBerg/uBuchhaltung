# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Stundenzettel-PDF mit Seitenumbruch (T62) und Summen aus Minuten (T63).

* wenige Einträge: eine Seite, kein „Seite n von m“; das Layout unter der Tabelle (Summenzeile, Unterschriften)
  bleibt, wie es war
* viele Einträge: mehrere Seiten, Kopf und Tabellenkopf auf jeder Seite, „Seite n von m“ auf jeder; Summe und
  Unterschriften nur auf der letzten; keine Datenzeile unter y = 60 und kein Text unter y = 40
* Grenzfall: Passt die letzte Datenzeile noch, Summe und Unterschriften darunter aber nicht, bekommen sie eine
  eigene Seite (mit Kopf, ohne Datenzeilen)
* „Arbeitstage“ zählt Kalendertage mit Arbeit über 0 Minuten, die Summe entsteht aus Minuten (3 × 20 min = 1,00)
* „(+1)“ hinter der Endzeit eines Eintrags über Mitternacht, ohne dass die Spalte „Pause“ berührt wird
* das PDF bleibt PDF/A-3b mit eingebetteten Schriften, das Logo steht auf jeder Seite, ``store=False`` schreibt nichts

Gelesen werden die Content-Streams der Seiten (Textpositionen) und der Text, den pdfplumber extrahiert.
Alle Namen und Werte sind erfundene Testdaten; die Daten der Einträge sind feste Tage, „heute“ steht nur
im Unterschriftsfeld des PDF und wird nirgends erwartet.
"""
import datetime
import io
import os
import re
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pdfplumber
import pytest
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import resolve1, stream_value
from PIL import Image

import userctx
from export.pdf_document import text_width
from export.pdf_worktime import COL_X, PAGE_NUMBER_Y, ROW_SIZE, TABLE_TOP, generate_worktime_pdf, page_layout
from tests.test_pdfa import _doc, _xmp

YEAR = ('2026-01-01', '2026-12-31')           # über mehrere Monate: je Eintrag eine Zeile
MARCH = ('2026-03-01', '2026-03-31')          # ein Monat: dazu je Tag ohne Eintrag eine Zeile (31 Zeilen)
FIRST_DAY = datetime.date(2026, 1, 1)
HEADER = 'Tag Datum Start Ende Pause Std. Info'


# ── Aufbau der Welt ───────────────────────────────────────────────────────────
def _world(db, logo=''):
    db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Firma GmbH',
                      company_name='Eigene Firma GmbH', city='Musterstadt', logo=logo)
    person = db.insert_contact(contact_type='employee', entity_type='person', display_name='Erika Probe',
                               first_name='Erika', last_name='Probe')
    customer = db.insert_contact(contact_type='customer', entity_type='company', display_name='Kunde GmbH',
                                 company_name='Kunde GmbH', city='Kundenstadt')
    return SimpleNamespace(person=person, customer=customer)


def _fill(db, world, count, start='08:00', end='08:20', **fields):
    """``count`` Einträge an aufeinanderfolgenden Tagen ab dem 1. Januar 2026 (je ein Eintrag, je ein Tag)."""
    for i in range(count):
        day = (FIRST_DAY + datetime.timedelta(days=i)).isoformat()
        db.insert_worktime(world.person, day, customer_id=world.customer, start_time=start, end_time=end, **fields)


def _same_day(db, world, count, day='2026-03-10'):
    """``count`` Einträge an EINEM Tag (je 10 Minuten, zu verschiedenen Stunden)."""
    for hour in range(count):
        db.insert_worktime(world.person, day, customer_id=world.customer,
                           start_time=f'{hour:02d}:00', end_time=f'{hour:02d}:10')


def _pdf(db, world, period):
    pdf, _ = generate_worktime_pdf(db, world.person, *period, store=False)
    return pdf


# ── Lesen des PDF ─────────────────────────────────────────────────────────────
TM = re.compile(r'1 0 0 1 ([0-9.]+) (-?[0-9.]+) Tm \((.*)\) Tj')
LINE = re.compile(r'^(\S+) (\S+) m (\S+) (\S+) l S$', re.M)


def _streams(pdf):
    """Der Content-Stream jeder Seite als Text."""
    return [b''.join(stream_value(part).get_data() for part in page.contents).decode('cp1252')
            for page in PDFPage.create_pages(_doc(pdf))]


def _cells(stream):
    """Alle Textzellen eines Streams: ``(x, y, Text)`` – der Text ohne die Maskierung der Klammern."""
    return [(float(x), float(y), re.sub(r'\\([()\\])', r'\1', text)) for x, y, text in TM.findall(stream)]


def _texts(stream):
    return [text for _, _, text in _cells(stream)]


def _data_rows(cells):
    """y jeder Datenzeile (das Datum steht in der Spalte „Datum“)."""
    return [y for x, y, text in cells if x == COL_X['date'] and re.fullmatch(r'\d\d\.\d\d\.', text)]


def _sum_cells(cells):
    return [(x, y, text) for x, y, text in cells if text.startswith('Summe (')]


def _signed(cells):
    """Die Ort-und-Datum-Zeilen der Unterschriftenblöcke."""
    return [(x, y) for x, y, text in cells if re.fullmatch(r'[A-Za-zäöüß]+, den \d\d\.\d\d\.\d{4}', text)]


def _page_texts(pdf):
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        return [page.extract_text() or '' for page in document.pages]


# ── Aufteilung auf Seiten ─────────────────────────────────────────────────────
@pytest.mark.parametrize('rows, counts', [
    (0, [0]), (1, [1]), (31, [31]), (44, [44]),
    (45, [45, 0]), (52, [52, 0]), (53, [53, 0]),         # die Zeile passt noch, Summe und Unterschrift nicht mehr
    (54, [53, 1]), (97, [53, 44]),
    (98, [53, 45, 0]), (106, [53, 53, 0]), (107, [53, 53, 1]), (150, [53, 53, 44]), (151, [53, 53, 45, 0]),
])
def test_page_layout_puts_53_rows_on_a_page_and_keeps_sum_and_signature_together(rows, counts):
    assert page_layout(rows) == counts


# ── Eine Seite ────────────────────────────────────────────────────────────────
def test_a_few_entries_make_one_page_without_a_page_number(tmp_db):
    world = _world(tmp_db)
    _fill(tmp_db, world, 3)
    pdf = _pdf(tmp_db, world, YEAR)
    [text] = _page_texts(pdf)
    assert 'Seite' not in text
    assert HEADER in text and 'Summe (3 Arbeitstage):' in text
    assert len(_streams(pdf)) == 1


def test_a_month_on_one_page_keeps_its_layout_below_the_table(tmp_db):
    world = _world(tmp_db)
    tmp_db.insert_worktime(world.person, '2026-03-10', customer_id=world.customer, start_time='08:00',
                           end_time='16:00', pause_minutes=30)
    [stream] = _streams(_pdf(tmp_db, world, MARCH))
    cells = _cells(stream)
    assert len(_data_rows(cells)) == 31                                          # jeder Tag, auch ohne Eintrag
    assert _sum_cells(cells) == [(40.0, 332.0, 'Summe (1 Arbeitstag):')]        # 738 − 31 · 13 − 3
    assert sorted(_signed(cells)) == [(40.0, 298.0), (320.0, 298.0)]             # 34 unter der Summenzeile
    assert [(float(a), float(b), float(c), float(d)) for a, b, c, d in LINE.findall(stream)] == [
        (40.0, 260.0, 250.0, 260.0), (320.0, 260.0, 530.0, 260.0)]               # Linie 38 unter Ort und Datum
    names = {(x, y, text) for x, y, text in cells if y < 298}
    assert names == {(40.0, 248.0, 'Erika Probe'), (40.0, 236.0, 'Eigene Firma GmbH'), (320.0, 236.0, 'Kunde GmbH')}
    assert 'Seite' not in ' '.join(_texts(stream))


# ── Mehrere Seiten ────────────────────────────────────────────────────────────
@pytest.fixture
def many(tmp_db):
    """120 Einträge an den Tagen 1. Januar bis 30. April 2026 (je 20 Minuten) und ein zweiter am 2. Januar
    über Mitternacht (22:00–02:00, 30 Minuten Pause = 210 Minuten): 121 Zeilen auf 3 Seiten."""
    world = _world(tmp_db)
    _fill(tmp_db, world, 120)
    tmp_db.insert_worktime(world.person, '2026-01-02', customer_id=world.customer, start_time='22:00',
                           end_time='02:00', pause_minutes=30)
    return SimpleNamespace(db=tmp_db, world=world, pdf=_pdf(tmp_db, world, YEAR))


def test_many_entries_make_several_pages_with_a_page_number_on_each(many):
    assert len(_streams(many.pdf)) == 3 == len(page_layout(121))
    texts = _page_texts(many.pdf)
    for number, text in enumerate(texts, start=1):
        assert f'Seite {number} von 3' in text
        assert text.count('Seite ') == 1


def test_every_page_has_title_person_period_and_table_header(many):
    for text in _page_texts(many.pdf):
        assert text.startswith('Stundenzettel\nPerson: Erika Probe, Eigene Firma GmbH\nZeitraum: '
                               '01.01.2026 - 31.12.2026\n' + HEADER)


def test_sum_and_signatures_stand_only_on_the_last_page(many):
    streams = _streams(many.pdf)
    cells = [_cells(stream) for stream in streams]
    assert [len(_sum_cells(page)) for page in cells] == [0, 0, 1]
    assert [len(_signed(page)) for page in cells] == [0, 0, 2]
    assert [bool(LINE.findall(stream)) for stream in streams] == [False, False, True]
    [(_, _, label)] = _sum_cells(cells[-1])
    assert label == 'Summe (120 Arbeitstage):'                          # 121 Einträge an 120 Tagen
    assert '43,50' in _texts(streams[-1])                               # 120 · 20 + 210 Minuten = 2610 Minuten


def test_every_entry_stands_once_and_in_order_across_the_pages(many):
    dates = [text for stream in _streams(many.pdf) for x, _, text in _cells(stream)
             if x == COL_X['date'] and re.fullmatch(r'\d\d\.\d\d\.', text)]
    expected = [(FIRST_DAY + datetime.timedelta(days=i)).strftime('%d.%m.') for i in range(120)]
    expected.insert(2, '02.01.')                                       # der zweite Eintrag des 2. Januar
    assert dates == expected


@pytest.mark.parametrize('rows', [1, 44, 45, 53, 54, 97, 98, 106, 120, 151])
def test_no_data_row_below_60_and_no_text_below_40(tmp_db, rows):
    world = _world(tmp_db)
    _fill(tmp_db, world, rows)
    streams = _streams(_pdf(tmp_db, world, YEAR))
    assert len(streams) == len(page_layout(rows))
    for stream in streams:
        cells = _cells(stream)
        assert min(y for _, y, _ in cells) >= 40
        assert all(y >= 60 for y in _data_rows(cells))
    assert sum(len(_data_rows(_cells(stream))) for stream in streams) == rows


def test_the_page_number_stands_at_the_foot_right_of_the_text_column(many):
    for stream in _streams(many.pdf):
        [(x, y, text)] = [cell for cell in _cells(stream) if cell[2].startswith('Seite ')]
        assert y == PAGE_NUMBER_Y == 40
        assert 480 < x < 555 and re.fullmatch(r'Seite \d von 3', text)


# ── Der Grenzfall: Summe und Unterschrift passen nicht mehr unter die letzte Zeile ──
def test_44_rows_still_hold_sum_and_signature_on_the_first_page(tmp_db):
    world = _world(tmp_db)
    _fill(tmp_db, world, 44)
    [stream] = _streams(_pdf(tmp_db, world, YEAR))
    cells = _cells(stream)
    assert min(_data_rows(cells)) == 738 - 43 * 13 == 179
    assert _sum_cells(cells) == [(40.0, 163.0, 'Summe (44 Arbeitstage):')]
    assert min(y for _, y, _ in cells) == 67.0                                    # die Firma unter der Linie: 163 − 34 − 38 − 24


def test_45_rows_leave_no_room_so_sum_and_signature_get_a_page_of_their_own(tmp_db):
    world = _world(tmp_db)
    _fill(tmp_db, world, 45)
    pdf = _pdf(tmp_db, world, YEAR)
    first, second = _streams(pdf)
    first_cells, second_cells = _cells(first), _cells(second)
    assert len(_data_rows(first_cells)) == 45 and min(_data_rows(first_cells)) == 738 - 44 * 13 == 166
    assert _sum_cells(first_cells) == [] and _signed(first_cells) == [] and not LINE.findall(first)
    assert _data_rows(second_cells) == []                                          # ohne Datenzeilen
    assert _sum_cells(second_cells) == [(40.0, TABLE_TOP - 3.0, 'Summe (45 Arbeitstage):')]
    assert sorted(_signed(second_cells)) == [(40.0, TABLE_TOP - 3.0 - 34), (320.0, TABLE_TOP - 3.0 - 34)]
    first_text, second_text = _page_texts(pdf)
    assert first_text.count(HEADER) == second_text.count(HEADER) == 1               # Kopf und Tabellenkopf auf beiden
    assert 'Seite 1 von 2' in first_text and 'Seite 2 von 2' in second_text


def test_53_rows_fill_the_page_to_row_62_and_the_next_page_holds_only_the_footer(tmp_db):
    world = _world(tmp_db)
    _fill(tmp_db, world, 53)
    first, second = (_cells(stream) for stream in _streams(_pdf(tmp_db, world, YEAR)))
    assert min(_data_rows(first)) == 738 - 52 * 13 == 62
    assert _data_rows(second) == [] and len(_sum_cells(second)) == 1 and len(_signed(second)) == 2


def test_the_54th_row_starts_the_next_page_at_the_top(tmp_db):
    world = _world(tmp_db)
    _fill(tmp_db, world, 54)
    first, second = (_cells(stream) for stream in _streams(_pdf(tmp_db, world, YEAR)))
    assert len(_data_rows(first)) == 53 and _data_rows(second) == [TABLE_TOP]
    assert _sum_cells(first) == [] and len(_sum_cells(second)) == 1


@pytest.mark.parametrize('on_one_day, pages, total', [(14, 1, '2,33'), (15, 2, '2,50')])
def test_a_month_with_many_entries_on_one_day_breaks_the_page_by_the_same_rule(tmp_db, on_one_day, pages, total):
    """30 Tage ohne Eintrag und ein Tag mit Einträgen (je 10 Minuten): 44 Zeilen passen auf eine Seite, 45 nicht."""
    world = _world(tmp_db)
    _same_day(tmp_db, world, on_one_day)
    streams = _streams(_pdf(tmp_db, world, MARCH))
    assert len(streams) == pages
    assert sum(len(_data_rows(_cells(stream))) for stream in streams) == 30 + on_one_day
    last = _cells(streams[-1])
    assert [text for _, _, text in _sum_cells(last)] == ['Summe (1 Arbeitstag):']
    assert total in [text for _, _, text in last]


# ── Summe aus Minuten, Arbeitstage ────────────────────────────────────────────
def test_three_entries_of_twenty_minutes_sum_to_one_hour_while_each_row_shows_0_33(tmp_db):
    world = _world(tmp_db)
    _fill(tmp_db, world, 3)
    [stream] = _streams(_pdf(tmp_db, world, YEAR))
    texts = _texts(stream)
    assert texts.count('0,33') == 3 and '1,00' in texts and '0,99' not in texts
    assert 'Summe (3 Arbeitstage):' in texts


def test_workdays_are_calendar_days_with_work_over_zero_minutes(tmp_db):
    world = _world(tmp_db)
    tmp_db.insert_worktime(world.person, '2026-03-10', start_time='08:00', end_time='10:00')
    tmp_db.insert_worktime(world.person, '2026-03-10', start_time='11:00', end_time='12:00')    # derselbe Tag
    tmp_db.insert_worktime(world.person, '2026-03-11', start_time='08:00', end_time='08:00')    # 0 Minuten
    tmp_db.insert_worktime(world.person, '2026-03-12', kind='vacation')
    tmp_db.insert_worktime(world.person, '2026-03-13', start_time='22:00', end_time='06:00')
    for period in (MARCH, ('2026-03-01', '2026-04-30')):                  # als Monat und über mehrere Monate
        [stream] = _streams(_pdf(tmp_db, world, period))
        texts = _texts(stream)
        assert 'Summe (2 Arbeitstage):' in texts and '11,00' in texts


def test_an_entry_over_midnight_counts_to_its_start_day_and_is_marked(tmp_db):
    world = _world(tmp_db)
    tmp_db.insert_worktime(world.person, '2026-03-10', start_time='22:00', end_time='02:00', pause_minutes=30)
    tmp_db.insert_worktime(world.person, '2026-03-11', start_time='08:00', end_time='16:00')
    [stream] = _streams(_pdf(tmp_db, world, MARCH))
    texts = _texts(stream)
    assert [text for text in texts if '(+1)' in text] == ['02:00 (+1)'] and '16:00' in texts
    assert '3,50' in texts and '8,00' in texts and '11,50' in texts and 'Summe (2 Arbeitstage):' in texts


def test_the_marker_does_not_touch_the_pause_column(tmp_db):
    world = _world(tmp_db)
    tmp_db.insert_worktime(world.person, '2026-03-10', start_time='22:00', end_time='02:00', pause_minutes=30)
    pdf = _pdf(tmp_db, world, MARCH)
    assert COL_X['end'] + text_width('23:59 (+1)', ROW_SIZE) + 5 <= COL_X['pause']
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        words = document.pages[0].extract_words(x_tolerance=1)
    marker = next(word for word in words if word['text'] == '(+1)')
    pause = next(word for word in words if word['text'] == '30' and abs(word['top'] - marker['top']) < 1)
    assert pause['x0'] - marker['x1'] >= 5                                        # sichtbarer Abstand, keine Überlappung


# ── Das PDF bleibt, was es war ────────────────────────────────────────────────
def _assert_valid_pdfa(pdf, page_count):
    assert pdf.startswith(b'%PDF-1.7\n%')
    doc = _doc(pdf)
    pages = list(PDFPage.create_pages(doc))
    assert len(pages) == page_count
    for page in pages:
        fonts = resolve1(resolve1(page.resources)['Font'])
        assert fonts and set(fonts) <= {'F1', 'F2', 'F3'}
        for ref in fonts.values():
            assert 'FontFile2' in resolve1(resolve1(ref)['FontDescriptor'])      # eingebettet
    root = ET.fromstring(_xmp(pdf).split('?>', 1)[1].rsplit('<?xpacket', 1)[0])
    ns = {'pdfaid': 'http://www.aiim.org/pdfa/ns/id/'}
    assert root.find('.//pdfaid:part', ns).text == '3' and root.find('.//pdfaid:conformance', ns).text == 'B'
    assert resolve1(resolve1(doc.catalog['OutputIntents'])[0])['S'].name == 'GTS_PDFA1'
    start = int(re.search(rb'startxref\n(\d+)\n%%EOF', pdf).group(1))
    assert pdf[start:start + 4] == b'xref'
    for number, line in enumerate(pdf[start:].split(b'trailer')[0].splitlines()[3:], 1):
        assert pdf[int(line[:10]):].startswith(f'{number} 0 obj'.encode())


def test_a_one_page_pdf_is_valid_pdfa(tmp_db):
    world = _world(tmp_db)
    _fill(tmp_db, world, 3)
    _assert_valid_pdfa(_pdf(tmp_db, world, YEAR), 1)


def test_a_multi_page_pdf_is_valid_pdfa(many):
    _assert_valid_pdfa(many.pdf, 3)


def test_the_logo_stands_on_every_page(tmp_db):
    logos = userctx.user_subdir('logos', create=False)
    os.makedirs(logos, exist_ok=True)
    Image.new('RGB', (40, 20), 'red').save(os.path.join(logos, 'firma.png'))
    world = _world(tmp_db, logo='data/logos/firma.png')
    _fill(tmp_db, world, 120)
    pdf = _pdf(tmp_db, world, YEAR)
    streams = _streams(pdf)
    assert len(streams) == 3 and all('/Logo Do' in stream for stream in streams)
    assert pdf.count(b'/Subtype /Image') == 1                                    # ein Bild, auf jeder Seite gezeichnet
    _assert_valid_pdfa(pdf, 3)


def test_a_multi_page_pdf_with_store_false_writes_nothing_and_by_default_it_is_stored(tmp_db):
    world = _world(tmp_db)
    _fill(tmp_db, world, 120)
    pdf, path = generate_worktime_pdf(tmp_db, world.person, *YEAR, store=False)
    assert pdf.startswith(b'%PDF') and len(_streams(pdf)) == 3
    assert not os.path.exists(path) and not os.path.exists(os.path.dirname(path))
    pdf, path = generate_worktime_pdf(tmp_db, world.person, *YEAR)
    with open(path, 'rb') as stored:
        assert stored.read() == pdf
