# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""CSV der betriebswirtschaftlichen Auswertung und der Summen- und Saldenliste (2.6.8): dieselben Zahlen wie Seite und PDF, in einer Form, die eine
Tabellenkalkulation mit deutscher Einstellung direkt öffnet – UTF-8 mit BOM, Semikolon, Dezimalkomma, Texte aus der Datenbank mit Schutz vor Formeln.

Vier Teile.

* Die Bausteine (``export/csv_table.py``): BOM, Trenner und Zeilenende, Anführung nur, wo nötig; der Schutz vor Formeln (``text_cell``); Beträge und
  Prozentwerte als Anzeige-Text ohne Tausenderpunkte (``amount_cell``, ``percent_cell``).
* Die BWA (``export/csv_bwa.py``): für die Testwelt und den Demobestand, beide Darstellungen, mit und ohne Konten – jede Zelle ist die der Seite (``cells``
  und das HTML der Seite); feindliche Namen stehen mit ``'`` davor bzw. ohne Zeilenumbruch da, und jede Zeile hat gleich viele Spalten.
* Die Summen- und Saldenliste (``export/csv_susa.py``): für die SuSa-Welt, die Testwelt und den Demobestand, mehrere Monate – jeder Wert ist eine Zahl
  (auch null), Salden tragen ihr Kennzeichen S/H, die Gesamtzeile geht auf.
* Routen, Seiten, Diagnose-MCP: streng gelesene Adresse, Anhang mit ``X-Filename``, kein Cookie, nichts geschrieben, kein Zugriff ohne Anmeldung, ein
  fester Satz bei einem Fehler; der Knopf „CSV“ auf den beiden Seiten und nirgends sonst; die Routen stehen in keiner Positivliste.

Der Aufbau der Berichte und die Zahlen prüfen ``test_bwa.py`` und ``test_susa.py``. Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht
nie als Konstante da.
"""
import csv
import html as html_lib
import io
import json
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote

import pytest

import userctx
from db import susa
from db.bwa import bwa_filename, cells, format_amount, format_percent, visible, visible_accounts
from db.susa import format_balance, susa_filename
from export import csv_bwa, csv_susa
from export.csv_bwa import bwa_csv_rows, generate_bwa_csv
from export.csv_susa import generate_susa_csv, susa_csv_rows
from export.csv_table import TEXT_LIMIT, amount_cell, csv_bytes, percent_cell, text_cell
from money import SCALE
from server import app as server_app
from server.pages_bwa import BWA_CSV_FAILED_TEXT, BwaQuery, PageBwa
from server.pages_liquidity import PageLiquidity
from server.pages_susa import SUSA_CSV_FAILED_TEXT, PageSusa, SusaQuery
from tests import installation
from tests import susa_helpers as helpers
from tests.bwa_helpers import book, report_from, some_books
from tests.dom_helpers import Dom
from tests.http_client import get
from tests.installation import dump, tree
from tests.test_bwa import demo_db, world  # noqa: F401  (Fixtures: die Testwelt und der Demobestand)
from tests.test_susa import susa_world  # noqa: F401  (Fixture: die SuSa-Welt)

BOM = b'\xef\xbb\xbf'
RLO, ZWSP, LSEP, NBSP = chr(0x202E), chr(0x200B), chr(0x2028), chr(0xA0)     # Schreibrichtung umkehren, Breite null, Zeilentrenner, geschütztes Leerzeichen
HOSTILE_NAMES = ['=HYPERLINK("http://example.invalid";"x")', 'Name; mit "Anführung"\nzweite Zeile', '+1+1', '-2+3', '@SUM(A1:A9)', '\t=1+1',
                 RLO + '=cmd']


def read_csv(data):
    """Die Bytes einer CSV zurückgelesen (Zeilen aus Zellen): Das BOM steht genau einmal vorn und gehört nicht zur ersten Zelle."""
    assert data.startswith(BOM) and data.count(BOM) == 1
    return list(csv.reader(io.StringIO(data[len(BOM):].decode('utf-8'), newline=''), delimiter=';'))


def assert_well_formed(data, columns):
    """Jede Zeile hat gleich viele Zellen, jede endet auf CRLF – und ein Zeilenumbruch steht nur am Ende einer Zeile, nie mitten in einer Zelle."""
    rows = read_csv(data)
    assert {len(row) for row in rows} == {columns}
    assert data.endswith(b'\r\n') and data.count(b'\r\n') == len(rows) and data.count(b'\n') == len(rows) and data.count(b'\r') == len(rows)
    return rows


def plain(shown):
    """Der Anzeige-Text einer Zelle der Seite ohne Tausenderpunkte, Prozentzeichen und Leerraum (auch das geschützte Leerzeichen)."""
    return re.sub(r'[.%\s]', '', shown)


def page_table(page, css):
    """Die erste Tabelle der Seite mit dieser Klasse: je ``tr`` die Zellen als Text (maskierte Zeichen aufgelöst)."""
    table = re.findall(rf'<table class="{css}">(.*?)</table>', page, flags=re.S)[0]
    return [[html_lib.unescape(re.sub(r'<[^>]+>', '', cell)).strip() for cell in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S)]
            for row in re.findall(r'<tr[^>]*>(.*?)</tr>', table, flags=re.S)]


def database_of(request, fixture):
    """Die Datenbank hinter einer Fixture: die SuSa-Welt trägt sie in ``db``, die Testwelt und der Demobestand sind selbst eine."""
    found = request.getfixturevalue(fixture)
    return found.db if fixture == 'susa_world' else found


# ═══ Die Bausteine ════════════════════════════════════════════════════════════
def test_csv_bytes_has_one_bom_semicolons_and_crlf_after_every_line():
    data = csv_bytes([['Art', 'Betrag'], ['Konto', '1234,56'], ['Summe', '']])
    assert data == BOM + b'Art;Betrag\r\nKonto;1234,56\r\nSumme;\r\n'
    assert csv_bytes([]) == BOM
    assert csv_bytes([['Büro Ä ß €', '12,50']]) == BOM + 'Büro Ä ß €;12,50\r\n'.encode('utf-8')          # UTF-8, nichts in Anführung


def test_a_text_with_a_separator_a_quote_and_a_line_break_reads_back_exactly():
    rows = [['a;b', 'sagt "hallo"', 'Zeile 1\nZeile 2'], ['x\r\ny', '', ';"\n', 'glatt']]
    data = csv_bytes(rows)
    assert read_csv(data) == rows
    assert b'"a;b"' in data and b'"sagt ""hallo"""' in data and b';glatt\r\n' in data          # nur, wo es nötig ist, in Anführung


@pytest.mark.parametrize('text, expected', [
    ('=1+1', "'=1+1"), ('+1', "'+1"), ('-1', "'-1"), ('@SUM(A1)', "'@SUM(A1)"), ('-', "'-"),
    ('\t=1+1', "'=1+1"), ('\r\n=1', "'=1"), (RLO + '=1', "'=1"), (ZWSP + '@x', "'@x"), ('  +49 30', "'+49 30"), ('\x00-5', "'-5"),
])
def test_a_text_that_starts_like_a_formula_gets_an_apostrophe_after_the_cleaning(text, expected):
    assert text_cell(text) == expected


@pytest.mark.parametrize('text', ['Bürobedarf', 'Erlöse 19% USt', 'Erlöse als Kleinunternehmer nach § 19 Abs. 1 UStG', 'Konto = Gegenkonto', 'a-b', 'x@y',
                                  '(=1)', "'=1", '3 + 4', '1-5 Jahre', 'Ä ß € – …'])
def test_an_ordinary_name_stays_as_it_is(text):
    assert text_cell(text) == text


def test_control_characters_and_line_breaks_inside_a_text_become_one_space():
    assert text_cell('Zeile eins\nZeile zwei\tEnde\r\x00\x07 Schluss') == 'Zeile eins Zeile zwei Ende Schluss'
    assert text_cell('a' + LSEP + 'b' + NBSP * 2 + 'c' + ZWSP + 'd' + RLO + 'e') == 'a b c d e'                # auch Zeilentrenner, geschütztes Leerzeichen, unsichtbare Zeichen
    assert text_cell('   ') == '' and text_cell('\n\t') == '' and text_cell(None) == '' and text_cell('') == ''
    assert text_cell(12345) == '12345'


def test_a_long_text_is_cut_to_200_characters_with_an_ellipsis():
    assert TEXT_LIMIT == 200
    assert text_cell('x' * 200) == 'x' * 200
    assert text_cell('x' * 201) == 'x' * 199 + '…' and len(text_cell('x' * 201)) == 200
    assert text_cell('Wort ' * 100) == ('Wort ' * 100)[:199] + '…'
    assert text_cell('abcdef', limit=4) == 'abc…'
    assert len(text_cell('ß' * 5000)) == 200


def test_the_protection_comes_after_the_cleaning_and_the_cutting():
    assert text_cell('=' + 'x' * 300) == "'=" + 'x' * 198 + '…'                         # erst gekürzt (200 Zeichen), dann der Schutz davor
    assert text_cell('\n' * 50 + '+1') == "'+1"                                         # nach dem Bereinigen ist der Anfang das Pluszeichen


@pytest.mark.parametrize('shown, expected', [
    ('1.234.567,89', '1234567,89'), ('-0,50', '-0,50'), ('', ''), ('0,00', '0,00'), ('-1.234,56', '-1234,56'), ('12,00', '12,00'),
    ('1.000.000.000.000,00', '1000000000000,00'),
])
def test_an_amount_loses_only_its_thousands_dots(shown, expected):
    assert amount_cell(shown) == expected


@pytest.mark.parametrize('shown', ['abc', '1,2,3', '12', '12,3', '1,234', '1.234', '--1,00', '1 234,56', '€ 1,00', '1,00 €', ' ', ',50', '1,', '١٢,٣٤', '1,00 S'])
def test_an_amount_cell_refuses_what_is_no_amount(shown):
    with pytest.raises(ValueError):
        amount_cell(shown)


@pytest.mark.parametrize('shown, expected', [
    ('12,34', '12,34'), ('-12,34', '-12,34'), ('1.234,56', '1234,56'), ('', ''), ('12,3 %', '12,3'), ('12,34' + NBSP + '%', '12,34'), ('-5,00 %', '-5,00'),
    ('0,00', '0,00'), ('  ', ''), ('1.234.567,89 %', '1234567,89'),
])
def test_a_percent_value_loses_its_thousands_dots_its_sign_and_its_blanks(shown, expected):
    assert percent_cell(shown) == expected


@pytest.mark.parametrize('shown', ['abc', '1,2,3', '12', '%', ',5', '5,', '--1,00', '1,00 S', '12,3x', '١٢,٣٤'])
def test_a_percent_cell_refuses_what_is_no_number(shown):
    with pytest.raises(ValueError):
        percent_cell(shown)


@pytest.mark.parametrize('minor', [0, 1, 49, 50, 51, 99, 100, 12345, 123450, -123450, -50, 9_999_999, 12_345_678_900_000, -12_345_678_900_000, 10 ** 17])
def test_the_amount_of_the_page_becomes_the_same_rounded_number(minor):
    """Die Rundung ist die der Seite (``format_amount``): kaufmännisch auf den Cent – ein Betrag, der auf 0,00 rundet, hat nie ein Minuszeichen."""
    value = Decimal(minor).scaleb(-SCALE).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    expected = f'{value if value else Decimal("0.00"):.2f}'.replace('.', ',')
    assert amount_cell(format_amount(minor)) == expected


@pytest.mark.parametrize('value', [None, Decimal('0.00'), Decimal('-0.01'), Decimal('5.5'), Decimal('12.345'), Decimal('-1234.56'), Decimal('1234567.89'),
                                   Decimal('-99999999.99')])
def test_the_percent_of_the_page_becomes_a_number(value):
    expected = '' if value is None else f'{value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):.2f}'.replace('.', ',')
    assert percent_cell(format_percent(None if value is None else value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))) == expected


# ═══ BWA: Zeilen und Zellen ═══════════════════════════════════════════════════
BWA_KINDS = {'Gruppe', 'Summe', 'Ergebnis', 'Nicht im Ergebnis', 'Konto'}


def bwa_lines(report, with_accounts):
    """Was die Seite zeigt, aus dem Bericht und ohne ``csv_bwa``: ``(Art, Nummer, Bezeichnung, Zellen der Seite)`` je sichtbarer Zeile (und Konto)."""
    kinds = {'group': 'Gruppe', 'sum': 'Summe', 'result': 'Ergebnis'}
    lines = []
    for line in [*report.lines, report.outside]:
        if not visible(report, line):
            continue
        lines.append(('Nicht im Ergebnis' if line is report.outside else kinds[line.kind], '', line.label, cells(report, line, line.values)))
        if with_accounts:
            lines += [('Konto', str(account.number), account.name or '', cells(report, line, account.values)) for account in visible_accounts(report, line)]
    return lines


BWA_CASES = [('world', 2026, 3), ('world', 2026, 12), ('demo_db', 2026, 10), ('demo_db', 2025, 12), ('demo_db', 2026, 1)]


@pytest.mark.parametrize('fixture, year, month', BWA_CASES)
def test_every_cell_of_the_bwa_csv_is_the_cell_of_the_page(fixture, year, month, request):
    """Beide Darstellungen, mit und ohne Konten: gleich viele Zeilen wie sichtbare Zeilen (und Konten), und jede Zelle ist die der Seite (``cells``) ohne
    Tausenderpunkte, Prozentzeichen und Leerraum."""
    db = database_of(request, fixture)
    for variant in ('jahr', 'vorjahr'):
        report = db.get_bwa(year, month, variant)
        for with_accounts in (False, True):
            rows = assert_well_formed(generate_bwa_csv(report, with_accounts), 11)
            head, body = rows[0], rows[1:]
            assert head[:3] == ['Art', 'Konto', 'Bezeichnung'] and len(set(head)) == 11                 # elf verschiedene Köpfe
            shown_lines = [line for line in [*report.lines, report.outside] if visible(report, line)]
            count = len(shown_lines) + (sum(len(visible_accounts(report, line)) for line in shown_lines) if with_accounts else 0)
            expected = bwa_lines(report, with_accounts)
            assert len(body) == len(expected) == count > 10, (fixture, year, month, variant, with_accounts)
            assert {row[0] for row in body} <= BWA_KINDS and [row[0] for row in body].count('Ergebnis') == 1
            assert ('Konto' in {row[0] for row in body}) == with_accounts
            for found, (kind, number, label, shown) in zip(body, expected):
                assert found[:3] == [kind, number, ' '.join(label.split())], (fixture, year, month, variant, found)
                assert found[3:] == [plain(cell) for cell in shown], (fixture, year, month, variant, found)
                assert all(re.fullmatch(r'(-?\d+,\d{2})?', cell) for cell in found[3:]), found


@pytest.mark.parametrize('fixture, year, month', BWA_CASES)
def test_the_bwa_csv_shows_the_rows_and_numbers_of_the_page(fixture, year, month, request):
    """Gegen das HTML der Seite (alle Konten, auch die zugeklappten): dieselben Zeilen in derselben Reihenfolge mit denselben Zahlen."""
    db = database_of(request, fixture)
    for variant in ('jahr', 'vorjahr'):
        body = read_csv(generate_bwa_csv(db.get_bwa(year, month, variant), True))[1:]
        page = [row for row in page_table(PageBwa(db, BwaQuery(year, month, variant)), 'bwaTable')[2:] if len(row) == 9]    # ohne die Überschrift „Nicht im Ergebnis“
        assert len(page) == len(body) > 10
        for shown, found in zip(page, body):
            label = shown[0].removeprefix('▶ ')
            assert label == (f'{found[1]} {found[2]}'.strip() if found[0] == 'Konto' else found[2]), (shown, found)
            assert [plain(cell) for cell in shown[1:]] == found[3:], (shown, found)


def test_the_bwa_csv_of_a_small_book_has_these_rows(tmp_db):
    some_books(tmp_db)
    compare = read_csv(generate_bwa_csv(tmp_db.get_bwa(2026, 3, 'vorjahr'), True))
    assert compare[0] == ['Art', 'Konto', 'Bezeichnung', 'März: 2026', 'März: 2025', 'März: Veränderung €', 'März: Veränderung %', 'Januar bis März: 2026',
                          'Januar bis März: 2025', 'Januar bis März: Veränderung €', 'Januar bis März: Veränderung %']
    rows = {(row[0], row[1], row[2]): row[3:] for row in compare[1:]}
    assert rows[('Ergebnis', '', 'Ergebnis')] == ['4,50', '119,00', '-114,50', '-96,22', '4,50', '119,00', '-114,50', '-96,22']
    assert rows[('Konto', '4400', 'Erlöse 19% USt')] == ['100,00', '100,00', '0,00', '0,00', '100,00', '100,00', '0,00', '0,00']
    assert rows[('Konto', '6815', 'Bürobedarf')] == ['50,00', '', '50,00', '', '50,00', '', '50,00', '']         # im Vorjahr nicht gebucht: leer
    assert rows[('Nicht im Ergebnis', '', 'Sonstige Ausgaben')] == ['30,00', '0,00', '30,00', '', '30,00', '0,00', '30,00', '']
    assert rows[('Konto', '3160', 'Verbindlichkeiten ggü. Kreditinstituten – 1-5 Jahre')][0] == '30,00'
    assert [row[0] for row in compare[1:]].count('Nicht im Ergebnis') == 1 and compare[-2][0] == 'Nicht im Ergebnis' and compare[-1][0] == 'Konto'
    shares = read_csv(generate_bwa_csv(tmp_db.get_bwa(2026, 3, 'jahr'), False))
    assert shares[0][3:] == ['März 2026: Betrag €', 'März 2026: % der Erlöse', 'März 2026: % der Kosten', 'März 2026: % der Betriebseinnahmen',
                             'Januar bis März 2026: Betrag €', 'Januar bis März 2026: % der Erlöse', 'Januar bis März 2026: % der Kosten',
                             'Januar bis März 2026: % der Betriebseinnahmen']
    result = next(row for row in shares if row[0] == 'Ergebnis')
    assert result[3:7] == ['4,50', '4,50', '', '3,78']                                                       # keine Kosten: der Anteil an den Kosten bleibt leer
    assert not [row for row in shares if row[0] == 'Konto']                                                  # ohne Kontennachweis keine Kontenzeilen


def test_a_percent_column_without_a_percent_sign_in_its_label_gets_in_percent(monkeypatch):
    report = report_from([(6815, 'Bürobedarf', -10.0)], variant='jahr')
    assert not [head for head in bwa_csv_rows(report, False)[0] if head.endswith(' in %')]                   # die Bezeichnungen tragen das Zeichen schon
    monkeypatch.setattr(csv_bwa, 'column_labels', lambda _report: ['Betrag', 'Anteil Erlöse', 'Anteil Kosten', 'Anteil Betriebseinnahmen'] * 2)
    head = bwa_csv_rows(report, False)[0]
    assert head[3:7] == ['März 2026: Betrag', 'März 2026: Anteil Erlöse in %', 'März 2026: Anteil Kosten in %', 'März 2026: Anteil Betriebseinnahmen in %']
    assert head[7] == 'Januar bis März 2026: Betrag' and head[8].endswith('Anteil Erlöse in %') and len(set(head)) == 11


def test_the_bwa_csv_of_an_empty_book_is_the_header_and_the_lines_with_zero():
    rows = assert_well_formed(generate_bwa_csv(report_from([], variant='vorjahr'), True), 11)
    assert rows[0][0] == 'Art' and [row[0] for row in rows[1:]].count('Ergebnis') == 1 and 'Konto' not in {row[0] for row in rows}
    assert next(row for row in rows if row[0] == 'Ergebnis')[3:] == ['0,00', '0,00', '0,00', '', '0,00', '0,00', '0,00', '']


# ═══ BWA: feindliche Namen ════════════════════════════════════════════════════
def test_a_formula_behind_a_comma_is_guarded_too():
    """Wer die Datei mit dem Komma als Trenner öffnet, bekommt das Stück hinter einem Komma als eigene Zelle: Auch dort steht das ``'``."""
    assert text_cell("x,=cmd|' /C calc'!A0,") == "x,'=cmd|' /C calc'!A0,"
    assert text_cell('Miete, -Nebenkosten') == "Miete, '-Nebenkosten"                   # hinter Leerzeichen: das Zeichen direkt vor der Formel
    assert text_cell('a,+1,b,@c,=d') == "a,'+1,b,'@c,'=d"
    assert text_cell('=a,=b') == "'=a,'=b"
    assert text_cell('Büro, Miete und Strom, 2. Halbjahr') == 'Büro, Miete und Strom, 2. Halbjahr'   # gewöhnliche Kommas bleiben, wie sie sind
    assert text_cell(',,') == ',,'
    row = next(csv.reader(io.StringIO("Konto,6890," + text_cell('x,=1+1,y')), delimiter=','))   # so läse es eine Tabellenkalkulation mit Komma
    assert not any(cell.lstrip().startswith(('=', '+', '-', '@')) for cell in row)


@pytest.mark.parametrize('code', [0xFF1D, 0xFF0B, 0xFF0D, 0xFF20, 0xFE62, 0xFE63, 0xFE66, 0xFE6B])
def test_wide_and_small_forms_of_the_formula_characters_are_guarded(code):
    """``＝``, ``＋``, ``－``, ``＠`` und ihre kleinen Formen: Die Grundform (NFKC) entscheidet – das Zeichen selbst bleibt stehen."""
    char = chr(code)
    assert unicodedata.normalize('NFKC', char) in ('=', '+', '-', '@')
    assert text_cell(char + '1+1') == "'" + char + '1+1'
    assert text_cell('x,' + char + '1') == "x,'" + char + '1'


def test_a_hostile_account_name_cannot_become_a_formula_or_break_the_table():
    report = report_from([(6800 + index, name, -10.0 - index) for index, name in enumerate(HOSTILE_NAMES)])
    data = generate_bwa_csv(report, True)
    rows = assert_well_formed(data, 11)                                                                      # jede Zeile hat dieselbe Spaltenzahl
    accounts = [row for row in rows if row[0] == 'Konto']
    assert [row[1] for row in accounts] == [str(6800 + index) for index in range(len(HOSTILE_NAMES))]
    assert [row[2] for row in accounts] == [
        '\'=HYPERLINK("http://example.invalid";"x")', 'Name; mit "Anführung" zweite Zeile', "'+1+1", "'-2+3", "'@SUM(A1:A9)", "'=1+1", "'=cmd"]
    for row in rows:
        assert not row[2].startswith(('=', '+', '-', '@')), row                                              # kein Text beginnt wie eine Formel
    for row in rows[1:]:
        assert all(re.fullmatch(r'(-?\d+,\d{2})?', cell) for cell in row[3:]), row                           # nur Zahlen dürfen mit einem Minus beginnen
    assert not any(cell.startswith(('=', '+', '@')) for row in rows for cell in row)
    assert accounts[1][2].count('\n') == 0 and b'\nzweite' not in data


def test_a_name_over_the_limit_is_cut_in_the_bwa_and_in_the_susa():
    report = report_from([(6815, 'W' * 400, -10.0), (6850, '= ' + 'Wort ' * 300, -5.0)])
    bwa_names = [row[2] for row in read_csv(generate_bwa_csv(report, True)) if row[0] == 'Konto']
    assert [len(name) for name in bwa_names] == [200, 201]                                                   # 200 Zeichen, beim zweiten der Schutz davor
    assert bwa_names[0].endswith('…') and bwa_names[1].startswith("'= Wort") and bwa_names[1].endswith('…')
    postings = [('month', susa.Posting(6815, 100_000, 0)), ('month', susa.Posting(susa.NOT_POSTED, 0, 100_000))]
    susa_rows = read_csv(generate_susa_csv(susa.build_susa(2026, 3, postings, {6815: ('x' * 400, False)}, [], [], [])))
    assert [len(row[3]) for row in susa_rows if row[0] == 'Konto' and row[2] == '6815'] == [200]


# ═══ Summen- und Saldenliste ══════════════════════════════════════════════════
SUSA_KINDS = {'Konto', 'Summe', 'Gesamt'}


def susa_lines(report):
    """Was die Seite zeigt, aus dem Bericht und ohne ``csv_susa``: ``(Art, Gruppe, Nummer, Bezeichnung, Konto oder Gruppe)``."""
    lines = []
    for group in susa.visible_groups(report):
        lines += [('Konto', group.group.label, '' if account.number is None else str(account.number), account.name, account)
                  for account in susa.visible_accounts(group)]
        lines.append(('Summe', group.group.label, '', f'Summe {group.group.label}', group))
    lines.append(('Gesamt', '', '', 'Gesamtsumme', report.total))
    return lines


def expected_values(row):
    """Die acht Wertzellen einer Zeile aus ihren Minor-Werten: Beträge wie die Seite (``format_amount``) ohne Tausenderpunkte, Salden ohne Vorzeichen und
    mit dem Kennzeichen, das ``format_balance`` der Seite nennt (leer bei 0,00)."""
    def amount(minor):
        return format_amount(minor).replace('.', '')

    def balance(minor):
        text, _, side = format_balance(minor).partition(' ')
        return [text.replace('.', ''), side]
    return [*balance(row.opening), amount(row.month_soll), amount(row.month_haben), amount(row.ytd_soll), amount(row.ytd_haben), *balance(row.closing)]


SUSA_CASES = [('susa_world', 2025, 12), ('susa_world', 2026, 1), ('susa_world', 2026, 3), ('susa_world', 2026, 7), ('susa_world', 2026, 12),
              ('world', 2026, 3), ('world', 2026, 12), ('demo_db', 2025, 12), ('demo_db', 2026, 10)]


@pytest.mark.parametrize('fixture, year, month', SUSA_CASES)
def test_every_value_of_the_susa_csv_is_a_number_that_agrees_with_the_report(fixture, year, month, request):
    db = database_of(request, fixture)
    report = db.get_susa(year, month)
    rows = assert_well_formed(generate_susa_csv(report), 12)
    head, body = rows[0], rows[1:]
    first, second = susa.block_headers(year, month)
    assert head == ['Art', 'Gruppe', 'Konto', 'Bezeichnung', 'Anfangssaldo', 'Anfangssaldo S/H', f'Soll {first}', f'Haben {first}',
                    f'Soll {second}', f'Haben {second}', 'Endsaldo', 'Endsaldo S/H'] and len(set(head)) == 12
    expected = susa_lines(report)
    assert len(body) == len(expected) > 5 and {row[0] for row in body} <= SUSA_KINDS
    for found, (kind, group, number, label, row) in zip(body, expected):
        assert found[:4] == [kind, group, number, ' '.join(label.split())], found
        assert found[4:] == expected_values(row), found
        for index in (4, 6, 7, 8, 9, 10):
            assert re.fullmatch(r'\d+,\d{2}', found[index]), found                                         # immer eine Zahl, auch null, nie leer, nie negativ
        for index, minor in ((5, row.opening), (11, row.closing)):
            assert found[index] == ('' if found[index - 1] == '0,00' else 'S' if minor > 0 else 'H'), found   # das Kennzeichen folgt dem Vorzeichen
        assert (found[2] == '') == (kind != 'Konto' or row.virtual), found                                  # nur ein virtuelles Konto hat keine Nummer
    kinds = [row[0] for row in body]
    assert kinds.count('Gesamt') == 1 and kinds[-1] == 'Gesamt'
    assert [row[1] for row in body if row[0] == 'Summe'] == [group.group.label for group in susa.visible_groups(report)]
    for group in susa.visible_groups(report):                                                              # je Gruppe ihre Konten, dann ihre Summe
        accounts = [row for row in body if row[0] == 'Konto' and row[1] == group.group.label]
        assert len(accounts) == len(susa.visible_accounts(group)) > 0
    total = body[-1]
    assert (total[6], total[8]) == (total[7], total[9]) and total[4:6] == ['0,00', ''] and total[10:12] == ['0,00', '']      # die Liste geht auf


@pytest.mark.parametrize('fixture, year, month', SUSA_CASES)
def test_the_susa_csv_shows_the_rows_and_numbers_of_the_page(fixture, year, month, request):
    """Gegen das HTML der Seite: dieselben Zeilen in derselben Reihenfolge; wo die Seite eine Null leer lässt, steht in der CSV ``0,00``."""
    db = database_of(request, fixture)
    body = read_csv(generate_susa_csv(db.get_susa(year, month)))[1:]
    page = [row for row in page_table(PageSusa(db, SusaQuery(year, month)), 'reportTable')[2:] if len(row) > 1]      # ohne die Gruppenüberschriften
    assert len(page) == len(body) > 5
    for shown, found in zip(page, body):
        if found[0] == 'Konto':
            assert shown[:2] == [found[2], found[3]], (shown, found)
            values = shown[2:]
        else:
            assert shown[0] == found[3], (shown, found)
            values = shown[1:]
        assert len(values) == 6
        opening, month_soll, month_haben, ytd_soll, ytd_haben, closing = values
        amount, _, side = (opening or '0,00').partition(' ')
        assert found[4:6] == [amount.replace('.', ''), side], (shown, found)
        assert found[6:10] == [(text or '0,00').replace('.', '') for text in (month_soll, month_haben, ytd_soll, ytd_haben)], (shown, found)
        amount, _, side = closing.partition(' ')
        assert found[10:12] == [amount.replace('.', ''), side], (shown, found)


def test_the_susa_world_shows_the_virtual_accounts_without_a_number(susa_world):
    rows = read_csv(generate_susa_csv(susa_world.db.get_susa(2026, 12)))
    virtual = [row for row in rows if row[0] == 'Konto' and row[2] == '']
    assert {row[3] for row in virtual} >= {susa.VIRTUAL_NAMES[susa.NOT_POSTED], susa.VIRTUAL_NAMES[susa.PRIOR_RESULT]}
    assert all(row[1] for row in virtual) and {row[1] for row in virtual} <= {group.label for group in susa.GROUPS}
    assert {row[2] for row in rows if row[0] == 'Konto' and row[2]} >= {'1800', '1810', '3806'}


def test_the_susa_csv_has_balances_with_a_flag_and_zero_as_a_number():
    def named(*numbers):
        return {number: (f'Konto {number}', False) for number in numbers}
    postings = [('prior', susa.Posting(1800, 12_345_678, 0)), ('prior', susa.Posting(1810, 0, 12_345_678)),               # 1.234,5678: S und H
                ('month', susa.Posting(6815, 40, 0)), ('month', susa.Posting(2100, 0, 40)),                              # 40 Minor Units: 0,004 – rundet auf 0,00
                ('month', susa.Posting(4400, 0, 5_000_000)), ('month', susa.Posting(1800, 5_000_000, 0))]
    rows = read_csv(generate_susa_csv(susa.build_susa(2026, 3, postings, named(1800, 1810, 6815, 2100, 4400), [], [], [])))
    by = {row[2]: row for row in rows if row[0] == 'Konto'}
    assert by['1800'][4:] == ['1234,57', 'S', '500,00', '0,00', '500,00', '0,00', '1734,57', 'S']
    assert by['1810'][4:] == ['1234,57', 'H', '0,00', '0,00', '0,00', '0,00', '1234,57', 'H']
    assert by['6815'][4:] == ['0,00', '', '0,00', '0,00', '0,00', '0,00', '0,00', '']                                       # rundet auf 0,00: kein Kennzeichen
    assert by['2100'][4:] == ['0,00', '', '0,00', '0,00', '0,00', '0,00', '0,00', '']
    assert by['4400'][4:] == ['0,00', '', '0,00', '500,00', '0,00', '500,00', '500,00', 'H']
    assert rows[-1][:4] == ['Gesamt', '', '', 'Gesamtsumme'] and rows[-1][4:] == ['0,00', '', '500,00', '500,00', '500,00', '500,00', '0,00', '']


def test_a_hostile_account_name_cannot_become_a_formula_in_the_susa():
    postings, names = [], {}
    for index, name in enumerate(HOSTILE_NAMES):
        number = 6800 + index
        names[number] = (name, False)
        postings += [('month', susa.Posting(number, 10_000 * (index + 1), 0)), ('month', susa.Posting(susa.NOT_POSTED, 0, 10_000 * (index + 1)))]
    data = generate_susa_csv(susa.build_susa(2026, 3, postings, names, [], [], []))
    rows = assert_well_formed(data, 12)
    accounts = [row for row in rows if row[0] == 'Konto' and row[2].startswith('68')]
    assert [row[3] for row in accounts] == [
        '\'=HYPERLINK("http://example.invalid";"x")', 'Name; mit "Anführung" zweite Zeile', "'+1+1", "'-2+3", "'@SUM(A1:A9)", "'=1+1", "'=cmd"]
    for row in rows:
        assert not row[3].startswith(('=', '+', '-', '@')) and not row[1].startswith(('=', '+', '-', '@')), row
    for row in rows[1:]:
        assert all(re.fullmatch(r'(\d+,\d{2}|[SH]?)', cell) for cell in row[4:]), row
    assert b'\nzweite' not in data


def test_the_susa_csv_leaves_out_the_notices_and_the_bank_check(susa_world):
    """Die Hinweise zu den Buchungen und der Abgleich mit den Bankbewegungen stehen auf der Seite und im PDF, nicht in der CSV."""
    report = susa_world.db.get_susa(2026, 12)
    assert report.notices and report.bank_checks
    text = generate_susa_csv(report).decode('utf-8')
    assert susa.BANK_HEADING not in text and 'Hinweise' not in text and 'Bankbewegung' not in text
    assert len(susa_csv_rows(report)) == len(susa_lines(report)) + 1


# ═══ Dateinamen ═══════════════════════════════════════════════════════════════
def test_the_filenames_without_an_extension_are_those_of_the_pdf():
    assert bwa_filename(2026, 10, 'vorjahr') == 'BWA_2026-10.pdf' and bwa_filename(2026, 10, 'jahr') == 'BWA_2026-10_Anteile.pdf'
    assert bwa_filename(2026, 1, 'vorjahr') == 'BWA_2026-01.pdf' and susa_filename(2026, 10) == 'Summen_und_Salden_2026-10.pdf'
    assert susa_filename(2026, 1) == 'Summen_und_Salden_2026-01.pdf'


def test_the_filenames_of_the_csv():
    assert bwa_filename(2026, 10, 'vorjahr', 'csv') == 'BWA_2026-10.csv' and bwa_filename(2026, 10, 'jahr', 'csv') == 'BWA_2026-10_Anteile.csv'
    assert bwa_filename(2026, 1, 'jahr', extension='csv') == 'BWA_2026-01_Anteile.csv'
    assert susa_filename(2026, 10, 'csv') == 'Summen_und_Salden_2026-10.csv' and susa_filename(2026, 1, extension='csv') == 'Summen_und_Salden_2026-01.csv'


# ═══ Routen ═══════════════════════════════════════════════════════════════════
def test_the_bwa_route_answers_with_a_download_and_writes_nothing(server_port, auto_login):
    db = auto_login.db
    some_books(db)
    before_db, before_tree = dump(db.db_name), tree(userctx.DATA_ROOT)

    default = get(server_port, '/bwa/csv?year=2026&month=3')
    assert default.status == 200 and default.headers['Content-Type'] == 'text/csv; charset=utf-8'
    assert default.headers['Content-Disposition'].startswith('attachment;') and 'BWA_2026-03.csv' in default.headers['Content-Disposition']
    assert default.headers['X-Filename'] == quote('BWA_2026-03.csv', safe='') and default.headers.get('Set-Cookie') is None
    assert default.headers.get_all('Cache-Control') == ['no-cache'] and default.headers['Content-Length'] == str(len(default.body))        # genau ein Cache-Control
    assert default.body == generate_bwa_csv(db.get_bwa(2026, 3, 'vorjahr'), False)                          # genau die Bytes des Erzeugers: die Zahlen von Seite und PDF
    shares = get(server_port, '/bwa/csv?year=2026&month=3&variant=jahr')
    assert shares.status == 200 and shares.headers['X-Filename'] == quote('BWA_2026-03_Anteile.csv', safe='')
    assert 'BWA_2026-03_Anteile.csv' in shares.headers['Content-Disposition'] and shares.headers.get('Set-Cookie') is None
    assert shares.body == generate_bwa_csv(db.get_bwa(2026, 3, 'jahr'), False)
    assert get(server_port, '/bwa/csv?year=2026&month=3&variant=vorjahr').body == default.body

    unspecified = {row[0] for row in read_csv(default.body)}
    assert 'Konto' not in unspecified and 'Gruppe' in unspecified                                              # Standard: ohne Konten
    for switch in ('1', 'true', 'on'):
        accounts = get(server_port, f'/bwa/csv?year=2026&month=3&accounts={switch}')
        assert accounts.body == generate_bwa_csv(db.get_bwa(2026, 3, 'vorjahr'), True) and 'Konto' in {row[0] for row in read_csv(accounts.body)}
    for switch in ('0', 'nein', 'false', 'ON', ''):
        assert get(server_port, f'/bwa/csv?year=2026&month=3&accounts={switch}').body == default.body

    assert dump(db.db_name) == before_db and tree(userctx.DATA_ROOT) == before_tree                         # nichts geschrieben, auch keine Datei


def test_the_susa_route_answers_with_a_download_and_writes_nothing(server_port, auto_login):
    db = auto_login.db
    helpers.build_susa_world(db)
    before_db, before_tree = dump(db.db_name), tree(userctx.DATA_ROOT)
    response = get(server_port, '/susa/csv?year=2026&month=8')
    assert response.status == 200 and response.headers['Content-Type'] == 'text/csv; charset=utf-8'
    assert response.headers['Content-Disposition'].startswith('attachment;') and 'Summen_und_Salden_2026-08.csv' in response.headers['Content-Disposition']
    assert response.headers['X-Filename'] == quote('Summen_und_Salden_2026-08.csv', safe='') and response.headers.get('Set-Cookie') is None
    assert response.headers.get_all('Cache-Control') == ['no-cache'] and response.headers['Content-Length'] == str(len(response.body))
    assert response.body == generate_susa_csv(db.get_susa(2026, 8))
    rows = assert_well_formed(response.body, 12)
    assert rows[-1][:4] == ['Gesamt', '', '', 'Gesamtsumme'] and any(row[2] == '1800' for row in rows)
    assert get(server_port, '/susa/csv?year=2025&month=12').body == generate_susa_csv(db.get_susa(2025, 12))
    assert dump(db.db_name) == before_db and tree(userctx.DATA_ROOT) == before_tree


def test_a_hostile_name_in_the_books_reaches_the_download_protected(server_port, auto_login):
    db = auto_login.db
    book(db, 4400, '119.00', '2026-03-10', tax_rate='19', tax_amount='19.00')
    book(db, 6890, '-12.00', '2026-03-11', name=HOSTILE_NAMES[0])
    book(db, 6891, '-13.00', '2026-03-12', name=HOSTILE_NAMES[1])
    bwa = assert_well_formed(get(server_port, '/bwa/csv?year=2026&month=3&accounts=1').body, 11)
    assert [row[2] for row in bwa if row[1] in ('6890', '6891')] == ['\'=HYPERLINK("http://example.invalid";"x")', 'Name; mit "Anführung" zweite Zeile']
    liste = assert_well_formed(get(server_port, '/susa/csv?year=2026&month=3').body, 12)
    assert [row[3] for row in liste if row[2] in ('6890', '6891')] == ['\'=HYPERLINK("http://example.invalid";"x")', 'Name; mit "Anführung" zweite Zeile']


@pytest.mark.parametrize('route', ['/bwa/csv', '/susa/csv'])
def test_the_csv_routes_refuse_what_they_cannot_read(server_port, auto_login, route):
    """Dieselben Fälle wie beim PDF: Jahr und Monat sind Pflicht und streng gelesen, ein doppelter Schlüssel ist ungültig – 400 mit Klartext, kein Cookie."""
    for query in ('', '?year=2026', '?month=3', '?year=2026&month=13', '?year=2026&month=0', '?year=26&month=3', '?year=%202026&month=3', '?year=2026&month=003',
                  '?year=2026&year=2025&month=3', '?year=2026&month=3&month=4', '?year=abcd&month=3', '?from=2026-03-01&to=2026-03-31'):
        response = get(server_port, route + query, headers={'Cookie': 'period=2026-03-01..2026-03-31'})
        assert response.status == 400 and response.text == 'Ungültiger Zeitraum.', (route, query)
        assert response.headers['Content-Type'] == 'text/plain; charset=utf-8' and response.headers.get('Set-Cookie') is None


def test_the_bwa_route_refuses_an_invalid_variant(server_port, auto_login):
    for variant in ('x', 'JAHR', '%20jahr', 'jahr&variant=jahr', 'jahr&variant=vorjahr'):
        response = get(server_port, f'/bwa/csv?year=2026&month=3&variant={variant}')
        assert response.status == 400 and response.text == 'Ungültige Darstellung.', variant
        assert response.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert get(server_port, '/bwa/csv?year=2026&month=3&variant=vorjahr').status == 200


@pytest.mark.parametrize('route', ['/bwa/csv?year=2026&month=3', '/susa/csv?year=2026&month=3'])
def test_without_a_login_the_csv_routes_go_to_the_login_page(server_port, route):
    installation.set_up(auto_login=False)
    for headers in (None, {'Cookie': 'session=gibtsnicht'}):
        response = get(server_port, route, headers=headers)
        assert response.status == 303 and response.headers['Location'] == '/login' and not response.body.startswith(BOM)


def test_the_failure_sentences_are_fixed_texts():
    assert BWA_CSV_FAILED_TEXT == 'Die Auswertung ließ sich nicht als CSV erstellen.'
    assert SUSA_CSV_FAILED_TEXT == 'Die Summen- und Saldenliste ließ sich nicht als CSV erstellen.'


@pytest.mark.parametrize('route, failed, module, target', [
    ('/bwa/csv?year=2026&month=3', BWA_CSV_FAILED_TEXT, csv_bwa, 'generate_bwa_csv'),
    ('/susa/csv?year=2026&month=3', SUSA_CSV_FAILED_TEXT, csv_susa, 'generate_susa_csv')])
def test_a_failing_csv_is_a_fixed_sentence_never_the_text_of_the_error(server_port, auto_login, monkeypatch, route, failed, module, target):
    def broken(*_args, **_kwargs):
        raise RuntimeError('Geheimer Pfad /srv/daten')
    monkeypatch.setattr(module, target, broken)
    response = get(server_port, route)
    assert response.status == 500 and response.text == failed and response.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert 'Geheimer Pfad' not in response.text and response.headers.get('Set-Cookie') is None


def test_there_is_no_csv_for_the_other_reports(server_port, auto_login):
    for route in ('/euer/csv?from=2026-01-01&to=2026-12-31', '/liquiditaet/csv?year=2026&month=3'):
        assert get(server_port, route).status == 404, route


def test_the_csv_routes_are_in_none_of_the_special_path_sets():
    for route in ('/bwa/csv', '/susa/csv'):
        for name in ('_ACTION_PATHS', '_GENERATE_PATHS', '_JSON_PATHS', '_POST_ONLY_JSON_PATHS', '_SEARCH_PATHS', '_UPLOAD_PATHS', '_LOCK_FREE_ROUTES',
                     '_JSON_ANSWER_ROUTES'):
            assert route not in getattr(server_app, name), (route, name)


def test_the_csv_routes_are_not_in_the_allowlist_of_the_diagnosis_tool():
    import tools.mcp_ubuchhaltung as mcp
    assert '/bwa/csv' not in mcp._PAGE_ALLOWLIST and '/susa/csv' not in mcp._PAGE_ALLOWLIST
    for path in ('/bwa/csv?year=2026&month=10', '/bwa/csv', '/susa/csv?year=2026&month=10', '/susa/csv'):
        with pytest.raises(ValueError, match='Seite nicht freigegeben'):
            mcp.check_page_path(path)


# ═══ Seiten ═══════════════════════════════════════════════════════════════════
def buttons_of(page):
    """Die Knöpfe der Zeile mit dem PDF-Knopf: ``(Klassen, onclick, Beschriftung)``."""
    row = page[page.index('<div class="rowWithObjects"><button type="button" class="coloredButton bg-blue btn-sm" onclick="'):page.index('<div class="splitScroll">')]
    return re.findall(r'<button type="button" class="([^"]*)" onclick="([^"]*)">([^<]*)</button>', row)


@pytest.mark.parametrize('variant', ['jahr', 'vorjahr'])
def test_the_bwa_page_has_a_csv_button_next_to_the_pdf_button(tmp_db, variant):
    some_books(tmp_db)
    page = PageBwa(tmp_db, BwaQuery(2026, 3, variant))
    buttons = buttons_of(page)
    assert [(onclick, label) for _css, onclick, label in buttons] == [('bwaPdf()', 'PDF'), ('bwaCsv()', 'CSV'), ('bwaToggleAll()', 'Alle Konten auf-/zuklappen')]
    assert buttons[0][0] == buttons[1][0] == 'coloredButton bg-blue btn-sm' and page.count('>CSV</button>') == 1       # dieselben Klassen, kein neues CSS
    assert ' style=' not in page[page.index('<div class="rectRounded">'):page.index('<div id="app_confirm"')]
    outside_script = re.sub(r'<script>.*?</script>', '', page, flags=re.S)
    assert '/bwa/csv' not in outside_script and 'bwaCsv' in outside_script and outside_script.count('bwaCsv') == 1      # die Adresse steht nur im Skriptblock
    script = Dom(page).scripts[1]                                                                             # hinter dem Skript der Menüzeile (gotoPeriod)
    data = re.search(r'var BWA_DATA = (.*?);\n', script, re.S).group(1)
    assert json.loads(data) == {'pdf': f'/bwa/pdf?year=2026&month=3&variant={variant}', 'csv': f'/bwa/csv?year=2026&month=3&variant={variant}'}
    csv_function = script[script.index('function bwaCsv()'):script.index('function bwaSetGroup')]
    assert "BWA_DATA.csv + '&accounts='" in csv_function and 'bwaAccounts' in csv_function                  # das Häkchen „mit Kontennachweis“ gilt für beide
    for forbidden in ('const ', 'let ', '=>', 'innerHTML', 'eval('):
        assert forbidden not in script, forbidden


def test_the_susa_page_has_a_csv_button_next_to_the_pdf_button(susa_world):
    page = PageSusa(susa_world.db, SusaQuery(2026, 3))
    buttons = buttons_of(page)
    assert [(onclick, label) for _css, onclick, label in buttons] == [('susaPdf()', 'PDF'), ('susaCsv()', 'CSV')]
    assert buttons[0][0] == buttons[1][0] == 'coloredButton bg-blue btn-sm' and page.count('>CSV</button>') == 1
    outside_script = re.sub(r'<script>.*?</script>', '', page, flags=re.S)
    assert '/susa/csv' not in outside_script and outside_script.count('susaCsv') == 1
    script = Dom(page).scripts[1]
    data = re.search(r'var SUSA_DATA = (.*?);\n', script, re.S).group(1)
    assert json.loads(data) == {'pdf': '/susa/pdf?year=2026&month=3', 'csv': '/susa/csv?year=2026&month=3'}
    assert 'function susaCsv() { appDownload(SUSA_DATA.csv); }' in script
    for forbidden in ('const ', 'let ', '=>', 'innerHTML', 'eval('):
        assert forbidden not in script, forbidden


def test_the_liquidity_page_has_no_csv_button(demo_db):
    page = PageLiquidity(demo_db, BwaQuery(2026, 3, 'vorjahr'))
    assert [label for _css, _onclick, label in buttons_of(page)] == ['PDF', 'Alle Konten auf-/zuklappen']
    assert '>CSV<' not in page and '/csv' not in page and 'Csv(' not in page and 'bwaCsv' not in page


def test_the_served_pages_carry_the_csv_address_of_the_month_they_show(server_port, auto_login):
    some_books(auto_login.db)
    for path, name, expected in (('/bwa?from=2026-03-01&to=2026-03-31&variant=jahr', 'BWA', '/bwa/csv?year=2026&month=3&variant=jahr'),
                                 ('/susa?from=2026-03-01&to=2026-03-31', 'SUSA', '/susa/csv?year=2026&month=3')):
        page = get(server_port, path).text
        data = re.search(rf'var {name}_DATA = (.*?);\n', Dom(page).scripts[1], re.S).group(1)
        assert json.loads(data)['csv'] == expected
        download = get(server_port, json.loads(data)['csv'])                                                  # der Knopf führt auf einen Download
        assert download.status == 200 and download.headers['Content-Type'] == 'text/csv; charset=utf-8' and download.body.startswith(BOM)
    liquidity = get(server_port, '/liquiditaet?from=2026-03-01&to=2026-03-31').text
    assert '>CSV<' not in liquidity and '/csv' not in liquidity
