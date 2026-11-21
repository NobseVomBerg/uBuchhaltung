# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""EÜR-Ansicht (2.5.16): EINE Aufbereitung (``db/euer.py``) für den Kasten des Dashboards und die Seite ``/euer`` samt PDF.

Fünf Teile.

* Die Aufbereitung (rein): Abschnitte nach ``reporting.euer_section``, Reihenfolge von ``get_euer_data``, Summen aus Minor Units, die echte
  und die virtuelle 3806 als zwei Zeilen; Zeitraumtext und Dateiname.
* Der Kasten des Dashboards bleibt zeichengleich – bis auf die eine neue Zeile mit dem Link. Die SHA-256-Schnappschüsse (``BOX_SNAPSHOTS``) sind
  am Stand 2.5.14 VOR der Umstellung genommen: Testwelt, Demobestand, ein Bestand mit „Sonstigen Ausgaben“ (3160, 3720, 3740) und einer echten UND
  der virtuellen 3806 samt einem Namen mit Markup, einer mit Beträgen mit Bruchteilen eines Cents. Der Kasten rechnete damals die Summen als
  ``float``, die Aufbereitung rechnet in Minor Units: Die gedruckten Beträge sind dabei in jedem geprüften Fall dieselben (Eigenschaftsprüfung gegen
  eine Abschrift der früheren Rechnung). Die Abweichung im geprüften Bereich: Ein Ergebnis von genau 0 war rot, wenn die float-Summe einen Rest von −5,55e-17
  ließ (0,3 gegen −0,1 und −0,2); jetzt ist es grün (``test_a_result_of_exactly_zero_is_green_…``).
  Außerhalb des Geprüften: Bei Summen ab rund 6·10¹¹ € kann der letzte Cent anders – richtig – gerundet sein, weil die float-Summe dort Stellen verlor.
* Die Adresse (``from``, ``to``) wird streng gelesen, ohne stillen Tausch und ohne Cookie.
* Die Seite: Untermenü, Formular, Tabelle gleich dem Kasten, Maskierung, kein Inline-Style, kein Markenname.
* Das PDF und die Routen: hochkant, mehrseitig, Beträge an der Spaltenkante, nichts auf der Platte, Anhang mit Dateiname.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie als Konstante da (die Seite bekommt es als Argument).
"""
import datetime
import hashlib
import html as html_lib
import io
import json
import random
import re
from urllib.parse import quote

import pdfplumber
import pytest
from pdfminer.pdfpage import PDFPage

import userctx
from db import Database
from db.bwa import format_amount
from db.euer import (COLUMN_ACCOUNT, COLUMN_AMOUNT, COLUMN_NAME, EUER_FILTER_SUFFIX, EUER_FOOTER, EUER_HINT_TEXT, EUER_INVALID_TEXT,
                     EUER_LINK_TEXT, EUER_OTHER_TEXT, EUER_TITLE, OTHER_HEADING, euer_filename, euer_statement, german_date,
                     range_label)
from db.reporting import bank_skr_numbers, euer_section
from export import pdf_euer
from export.pdf_euer import (BOTTOM_Y, COMPANY_Y, FOOTER_TOP_Y, FOOTER_Y, LEFT_X, LINES_PER_PAGE, NAME_WIDTH, NAME_X, PAGE_SIZE, RIGHT_X,
                             ROW_H, ROW_SIZE, TABLE_TOP, generate_euer_pdf, page_layout)
from export.pdf_worktime import info_width
from money import fmt_eur, from_minor, to_minor
from server.pages import Header1
from server.pages_bwa import REPORT_PAGES, reports_submenu
from server.pages_dashboard import PageDashboard
from server.pages_euer import (EUER_PDF_FAILED_TEXT, INVALID_PERIOD_ERROR, YEAR_LINKS, EuerQuery, PageEuer, read_euer_page_query,
                               read_euer_pdf_query)
from tests.bwa_helpers import book, some_books
from tests.dom_helpers import Dom, assert_same_structure
from tests.http_client import get
from tests.installation import dump, tree
from tests.test_bwa import demo_db, world  # noqa: F401  (Fixtures: die Testwelt und der Demobestand)
from tests.test_bwa_pdf import _cells, _streams
from tests.test_pdfa import _doc

BANKS = {1600, 1800, 1810}
HOSTILE = '<script>alert(1)</script>"x'
BRANDS = ('DATEV', 'WISO', 'ELSTER')


def created():
    """„Erstellt am“ der PDFs: ein fester Tag, nie die Uhr."""
    return datetime.date(2026, 11, 21)


# ═══ Die Aufbereitung ═════════════════════════════════════════════════════════
ROWS = [(520, 'Rechner', -3.0), (1406, 'Abziehbare Vorsteuer 19%', -9.5), (1800, 'Bank', 5.0), (2100, 'Privatentnahmen', -5.0),
        (3160, 'Darlehen', -30.0), (3806, 'Umsatzsteuer 19%', 40.0), (3806, 'Umsatzsteuer 19%', 19.0), (4400, 'Erlöse 19% USt', 100.0),
        (4405, 'Wartekonto', 7.0), (6815, 'Bürobedarf', -50.0), (10000, 'Forderungen', 3.0)]


def _triples(lines):
    return [(line.number, line.name, line.minor) for line in lines]


def test_the_statement_orders_the_rows_into_three_sections():
    statement = euer_statement(ROWS, BANKS)
    assert _triples(statement.income) == [(3806, 'Umsatzsteuer 19%', 400000), (3806, 'Umsatzsteuer 19%', 190000),
                                          (4400, 'Erlöse 19% USt', 1000000)]          # die echte und die virtuelle 3806: zwei Zeilen
    assert _triples(statement.expense) == [(1406, 'Abziehbare Vorsteuer 19%', -95000), (6815, 'Bürobedarf', -500000)]
    assert _triples(statement.other) == [(3160, 'Darlehen', -300000)]
    assert (statement.income_sum, statement.expense_sum, statement.other_sum) == (1590000, -595000, -300000)
    assert statement.result == 1590000 - 595000                                       # die „Sonstigen Ausgaben“ zählen nicht mit
    shown = {line.number for line in statement.income + statement.expense + statement.other}
    assert not shown & {520, 1800, 2100, 4405, 10000}                                 # Anlage, Bank, Privat, Wartekonto, Forderung


def test_an_empty_statement_is_zero_in_every_section():
    statement = euer_statement([], BANKS)
    assert statement.income == statement.expense == statement.other == ()
    assert (statement.income_sum, statement.expense_sum, statement.other_sum, statement.result) == (0, 0, 0, 0)


def test_the_statement_is_frozen():
    statement = euer_statement(ROWS, BANKS)
    with pytest.raises(AttributeError):
        statement.result = 0
    with pytest.raises(AttributeError):
        statement.income[0].minor = 0


def test_every_line_is_the_minor_value_of_its_row_and_the_sums_are_integers():
    """Eigenschaftsprüfung mit festem Seed: Jede Zeile ist ``to_minor`` ihres Betrags, die Reihenfolge je Abschnitt bleibt die der
    Zeilen, ``result`` ist die Summe der Einnahmen und der Ausgaben – nie die der „Sonstigen Ausgaben“."""
    rng = random.Random(2516)
    numbers = [520, 1406, 1800, 2100, 3160, 3720, 3740, 3806, 3806, 4185, 4300, 4400, 4405, 6815, 6850, 10000]
    for _ in range(300):
        rows = [(rng.choice(numbers), f'Konto {index}', float(from_minor(rng.choice((-1, 1)) * rng.randint(100, 50_000_000))))
                for index in range(rng.randint(0, 14))]
        statement = euer_statement(rows, BANKS)
        for key in ('income', 'expense', 'other'):
            expected = [(number, name, to_minor(total)) for number, name, total in rows if euer_section(number, BANKS) == key]
            assert _triples(getattr(statement, key)) == expected
            assert getattr(statement, f'{key}_sum') == sum(minor for _n, _m, minor in expected)
            assert isinstance(getattr(statement, f'{key}_sum'), int)
        assert statement.result == statement.income_sum + statement.expense_sum


def test_the_period_label_names_a_full_year_by_its_number_and_any_other_period_by_its_days():
    assert range_label('2026-01-01', '2026-12-31') == '2026'
    assert range_label('2026-03-01', '2026-03-31') == '01.03.2026 – 31.03.2026'
    assert range_label('2025-01-01', '2026-12-31') == '01.01.2025 – 31.12.2026'
    assert range_label('2026-01-01', '2026-12-30') == '01.01.2026 – 30.12.2026'
    assert range_label('2026-02-01', '2026-12-31') == '01.02.2026 – 31.12.2026'
    assert german_date('2026-03-01') == '01.03.2026' and german_date('nicht lesbar') == 'nicht lesbar'


def test_the_file_name_follows_the_period():
    assert euer_filename('2026-01-01', '2026-12-31') == 'EUER_2026.pdf'
    assert euer_filename('2026-03-01', '2026-03-31') == 'EUER_2026-03-01_2026-03-31.pdf'
    assert euer_filename('2025-01-01', '2026-12-31') == 'EUER_2025-01-01_2026-12-31.pdf'


# ═══ Der Kasten des Dashboards ════════════════════════════════════════════════
def _box(page):
    """Der Kasten der EÜR im Dashboard: von der Überschrift bis vor das Skript."""
    start = page.index('<h3>Einnahmen&uuml;berschussrechnung')
    return page[start:page.index('<script>', start)]


def _link_line(date_from, date_to, suffix=''):
    return f'        <p class="hint"><a href="/euer?from={date_from}&amp;to={date_to}">zur Auswertung</a>{suffix}</p>\n'


def _without_link(box, date_from, date_to, suffix=''):
    line = _link_line(date_from, date_to, suffix)
    assert box.count(line) == 1, line
    return box.replace(line, '')


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _extra_books(db):
    """„Sonstige Ausgaben“ (3160, 3720, 3740), eine echte UND die virtuelle 3806 (Umsatzsteuer aus Einnahmen), ein Name mit Markup."""
    book(db, 4400, '119.00', '2026-03-10', tax_rate='19', tax_amount='19.00')
    book(db, 4300, '107.00', '2026-03-11', tax_rate='7', tax_amount='7.00')
    book(db, 3806, '40.00', '2026-03-12')
    book(db, 6815, '-59.50', '2026-03-13', tax_rate='19', tax_amount='-9.50')
    book(db, 3160, '-30.00', '2026-03-14')
    book(db, 3720, '-12.00', '2026-03-15')
    book(db, 3740, '-8.00', '2026-03-16')
    book(db, 6890, '-20.00', '2026-03-17', name='<b>Posten</b> & "x" \'y\'')
    book(db, 6850, '-20.00', '2026-03-17')
    book(db, 4400, '50.00', '2026-04-01')


def _fraction_books(db):
    """Beträge mit Bruchteilen eines Cents (die Maske hält sie auf 1/10000 €; die EÜR zeigt Konten ab einem Cent)."""
    for number, amount in ((4400, '0.015'), (4300, '0.015'), (4185, '0.005'), (6815, '-0.015'), (6850, '-0.025'), (6805, '-0.0125'),
                           (3160, '-0.015'), (3720, '-0.015')):
        book(db, number, amount, '2026-03-10')


def _noise_books(db):
    """Das Ergebnis ist genau 0 – die float-Summe der Zeilen nicht: 0,3 gegen −0,1 und −0,2 ergab −5,55e-17."""
    book(db, 4400, '0.30', '2026-03-10')
    book(db, 6815, '-0.10', '2026-03-10')
    book(db, 6850, '-0.20', '2026-03-10')


def _positive_noise_books(db):
    """Das Spiegelbild: 0,1 und 0,2 gegen −0,3 (die float-Summe der Einnahmen war 0,30000000000000004)."""
    book(db, 4400, '0.10', '2026-03-10')
    book(db, 4300, '0.20', '2026-03-10')
    book(db, 6815, '-0.30', '2026-03-10')


@pytest.fixture
def extra(tmp_db):
    _extra_books(tmp_db)
    return tmp_db


@pytest.fixture
def fractions(tmp_db):
    _fraction_books(tmp_db)
    return tmp_db


@pytest.fixture
def noise(tmp_db):
    _noise_books(tmp_db)
    return tmp_db


@pytest.fixture
def positive_noise(tmp_db):
    _positive_noise_books(tmp_db)
    return tmp_db


# Die Fälle: (Bestand, von, bis, Konto) – und der SHA-256 des Kastens VOR der Umstellung (Stand 2.5.14), ohne die Zeile mit dem Link
BOX_CASES = {
    'world_year': ('world', '2026-01-01', '2026-12-31', None),
    'world_march': ('world', '2026-03-01', '2026-03-31', None),
    'world_q1': ('world', '2026-01-01', '2026-03-31', None),
    'world_alpha': ('world', '2026-01-01', '2026-12-31', 'Giro Alpha'),
    'demo_2025': ('demo_db', '2025-01-01', '2025-12-31', None),
    'demo_2026': ('demo_db', '2026-01-01', '2026-10-31', None),
    'demo_2026_full': ('demo_db', '2026-01-01', '2026-12-31', None),
    'empty': ('tmp_db', '2026-01-01', '2026-12-31', None),
    'extra_year': ('extra', '2026-01-01', '2026-12-31', None),
    'extra_march': ('extra', '2026-03-01', '2026-03-31', None),
    'fractions': ('fractions', '2026-01-01', '2026-12-31', None),
    'positive_noise': ('positive_noise', '2026-01-01', '2026-12-31', None),
}
BOX_SNAPSHOTS = {
    'world_year': 'd3ed0c244aa6c17ec39ce997391445f6dad6faee313104b88ee0e68c60357255',
    'world_march': '09f3f03b1f9e266c58a8a898245d9fc170132ce42a3d0c0f39c942a217b691df',
    'world_q1': '0e9b9777663b8f97d7e7e2d31c1c8a52d885f19e284a68f91bebb7f5ad717b4b',
    'world_alpha': '4959539b496131b129c98f44d5442af8e648f3c979c338e3722e8824aec6cbe6',
    'demo_2025': '70ae1dc736f4d2ea35d2aeeb59ef2c358590549a477f13fb0481444639691bc6',
    'demo_2026': '828ead12a26bd8b67e2180a655911fc7b3c26cb301671c94e9981b61c93c552b',
    'demo_2026_full': '5a906a4a565d161f4ab4d87c7cca32c80c125b4313850ba18e704b43008a5bb1',
    'empty': 'af2aef7cbfa1355d765687ce5932b1ff0f3296d54414b625dc2562eae44e58da',
    'extra_year': '0453235a501f42d4c6ed3f4f477e5e54a838a4d75e2972e1c4a31dc2eef3d3b1',
    'extra_march': '0108876b788084ee6363dbb9044a5ebb15b82f086d01e4dfe7e0aa461fb3aac3',
    'fractions': '8990d98625e3ce9a221a1fe587e8a3abc06f6fbf4aaef4b6957cd1503ce0d4e9',
    'positive_noise': '7dd1c52d940acfd386c03c227e94aa7207b8ba2cb288a2ae4808a64d6e77b12a',
}
NOISE_SNAPSHOT = '9c24870324434e5cebc08fdcc97e594cb91652343bf107cb605e571cdfdfaa38'      # mit dem roten „0,00 €“ des Ergebnisses
ZERO_RESULT = 'font-size:18px;\n                    color:green;">\n                    0,00 €</td></tr>'      # die Zeile „Ergebnis EÜR“
OLD_ZERO_RESULT = 'font-size:18px;\n                    color:red;">\n                    0,00 €</td></tr>'


def _account_id(db, name):
    return next(account[0] for account in db.fetch_accounts() if account[1] == name)


def test_the_cases_and_the_snapshots_name_the_same_boxes():
    assert set(BOX_CASES) == set(BOX_SNAPSHOTS)


@pytest.mark.parametrize('case', sorted(BOX_CASES))
def test_the_dashboard_box_is_unchanged_but_for_the_link(case, request):
    """Zeichen für Zeichen derselbe Kasten wie vor der Umstellung auf ``db.euer``: Nach Abzug der einen Zeile mit dem Link ergibt der
    Text denselben SHA-256 wie am Stand 2.5.14. Weicht ein Zeichen ab, ist der Code zu ändern, nie der Schnappschuss."""
    fixture, date_from, date_to, account = BOX_CASES[case]
    db = request.getfixturevalue(fixture)
    account_ids = [_account_id(db, account)] if account else None
    box = _box(PageDashboard(db, date_from, date_to, account_ids))
    suffix = EUER_FILTER_SUFFIX if account else ''
    assert _sha(_without_link(box, date_from, date_to, suffix)) == BOX_SNAPSHOTS[case], case


def _legacy_numbers(rows, bank_numbers):
    """Die Zahlen des Kastens bis 2.5.14 (abgeschrieben): je Zeile ``fmt_eur`` des float, die Summen als float-Summe, die Farbe des
    Ergebnisses am float."""
    parts = {key: [row for row in rows if euer_section(row[0], bank_numbers) == key] for key in ('income', 'expense', 'other')}
    sums = {key: sum(total for _number, _name, total in part) for key, part in parts.items()}
    result = sums['income'] + sums['expense']
    return {'lines': {key: [fmt_eur(total) for _n, _m, total in part] for key, part in parts.items()},
            'sums': {key: fmt_eur(value) for key, value in sums.items()}, 'result': fmt_eur(result),
            'color': 'green' if result >= 0 else 'red'}


def _new_numbers(statement):
    return {'lines': {key: [fmt_eur(from_minor(line.minor)) for line in getattr(statement, key)] for key in ('income', 'expense', 'other')},
            'sums': {key: fmt_eur(from_minor(getattr(statement, f'{key}_sum'))) for key in ('income', 'expense', 'other')},
            'result': fmt_eur(from_minor(statement.result)), 'color': 'green' if statement.result >= 0 else 'red'}


def test_a_result_of_exactly_zero_is_green_even_where_a_float_sum_left_a_residue(noise):
    """Die einzige Abweichung vom Kasten bis 2.5.14: Er rechnete die Summen als ``float`` – 0,3 gegen −0,1 und −0,2 ergab −5,55e-17, und
    das „0,00 €“ des Ergebnisses stand rot da. Die Aufbereitung rechnet in Minor Units: genau 0 ist grün. Sonst ist der Kasten derselbe."""
    box = _without_link(_box(PageDashboard(noise, '2026-01-01', '2026-12-31')), '2026-01-01', '2026-12-31')
    assert box.count(ZERO_RESULT) == 1 and OLD_ZERO_RESULT not in box
    assert _sha(box.replace(ZERO_RESULT, OLD_ZERO_RESULT)) == NOISE_SNAPSHOT                 # nur diese eine Farbe weicht ab
    rows = noise.get_euer_data('2026-01-01', '2026-12-31')
    legacy = _legacy_numbers(rows, bank_skr_numbers(noise.fetch_accounts()))
    assert legacy['color'] == 'red' and legacy['result'] == '0,00 €'                          # so rechnete der Kasten bis 2.5.14
    assert euer_statement(rows, set()).result == 0


def test_the_printed_amounts_equal_the_former_float_sums_also_with_fractions_of_a_cent():
    """Eigenschaftsprüfung mit festem Seed gegen eine Abschrift der früheren Rechnung: Zeilen und Summen werden gleich gedruckt, auch bei
    Beträgen mit Bruchteilen eines Cents (zwei Zeilen zu 0,015 € ergeben 0,03 €, nicht 0,04 €) – dort rundet die Anzeige auf den Cent, die
    Summe wird aus den ungerundeten Werten gebildet, früher wie jetzt. Die Farbe stimmt auch, ausgenommen ein Ergebnis von genau 0."""
    rng = random.Random(25160)
    numbers = [1406, 3160, 3720, 3740, 3806, 3806, 4185, 4300, 4400, 6805, 6815, 6850]
    zero_results = 0
    for round_number in range(2000):
        if round_number % 4 == 0:                                   # Einnahmen und Ausgaben, die sich zu genau 0 aufheben
            first, second = rng.randint(100, 99_999), rng.randint(100, 99_999)
            rows = [(4300, 'Einnahme B', float(from_minor(second))), (4400, 'Einnahme A', float(from_minor(first))),
                    (6815, 'Ausgabe', float(from_minor(-(first + second)))), (3160, 'Sonstiges', float(from_minor(-rng.randint(100, 9999))))]
        else:
            rows = []
            for index in range(rng.randint(0, 8)):
                minor = max(rng.randint(1, 3_000_000) * 100 + rng.choice((0, 5, 25, 50, 75, 95)), 100)
                rows.append((rng.choice(numbers), f'Konto {index}', float(from_minor(rng.choice((-1, 1)) * minor))))
        rows.sort(key=lambda row: row[0])
        statement = euer_statement(rows, BANKS)
        old, new = _legacy_numbers(rows, BANKS), _new_numbers(statement)
        assert old['lines'] == new['lines'] and old['sums'] == new['sums'] and old['result'] == new['result'], rows
        if statement.result == 0:
            zero_results += 1
            assert new['color'] == 'green'
        else:
            assert old['color'] == new['color'], rows
    assert zero_results >= 500


def test_two_lines_of_a_fraction_of_a_cent_print_alike_and_sum_unrounded(fractions):
    """Zwei Konten zu 0,015 € stehen je mit „0,02 €“ da, ihre Summe mit „0,03 €“ (nicht „0,04 €“): Es wird erst bei der Anzeige gerundet."""
    box = _box(PageDashboard(fractions, '2026-01-01', '2026-12-31'))
    text = re.sub(r'\s+', ' ', html_lib.unescape(re.sub(r'<[^>]+>', ' ', box)))
    assert '4300 Erlöse 7% USt 0,02 € 4400 Erlöse 19% USt 0,02 € Summe Betriebseinnahmen 0,03 €' in text
    assert 'Summe Betriebsausgaben -0,05 €' in text and 'Ergebnis EÜR -0,02 €' in text
    assert 'Summe Sonstige Ausgaben -0,03 €' in text


def test_the_box_links_to_the_page_for_its_period_and_names_the_missing_account_filter(world):
    box = _box(PageDashboard(world, '2026-03-01', '2026-03-31'))
    links = re.findall(r'<a href="(/euer\?[^"]*)">([^<]*)</a>(.*?)</p>', box)
    assert links == [('/euer?from=2026-03-01&amp;to=2026-03-31', EUER_LINK_TEXT, '')]
    assert EUER_FILTER_SUFFIX not in box
    assert box.index('</h3>') < box.index('/euer?from=') < box.index('<h4')                 # direkt unter der Überschrift
    filtered = _box(PageDashboard(world, '2026-03-01', '2026-03-31', [_account_id(world, 'Giro Alpha')]))
    assert re.findall(r'<a href="(/euer\?[^"]*)">([^<]*)</a>(.*?)</p>', filtered) == [
        ('/euer?from=2026-03-01&amp;to=2026-03-31', EUER_LINK_TEXT, EUER_FILTER_SUFFIX)]
    assert EUER_FILTER_SUFFIX == ' (dort ohne Kontofilter)'
    href = html_lib.unescape(links[0][0])                                                    # die Adresse des Links liest die Seite wieder
    query = dict(part.split('=') for part in href.split('?')[1].split('&'))
    assert read_euer_page_query({key: [value] for key, value in query.items()}, created()) == EuerQuery('2026-03-01', '2026-03-31')


# ═══ Adresse ══════════════════════════════════════════════════════════════════
def _qs(**values):
    return {key: value if isinstance(value, list) else [value] for key, value in values.items()}


def test_the_page_without_an_address_shows_the_current_year_without_a_note():
    assert read_euer_page_query({}, datetime.date(2026, 11, 21)) == EuerQuery('2026-01-01', '2026-12-31', invalid=False)
    assert read_euer_page_query({}, datetime.date(2025, 1, 1)) == EuerQuery('2025-01-01', '2025-12-31')
    assert read_euer_page_query({'unbekannt': ['x']}, datetime.date(2026, 11, 21)) == EuerQuery('2026-01-01', '2026-12-31')


def test_the_page_reads_a_valid_period():
    today = datetime.date(2026, 11, 21)
    assert read_euer_page_query(_qs(**{'from': '2025-03-01', 'to': '2025-03-31'}), today) == EuerQuery('2025-03-01', '2025-03-31')
    assert read_euer_page_query(_qs(**{'from': '2026-05-05', 'to': '2026-05-05'}), today) == EuerQuery('2026-05-05', '2026-05-05')
    assert read_euer_page_query(_qs(**{'from': '1900-01-01', 'to': '2200-12-31'}), today) == EuerQuery('1900-01-01', '2200-12-31')


INVALID_PERIODS = [
    {'from': ['2026-02-30'], 'to': ['2026-12-31']},                       # ein Tag, den es nicht gibt
    {'from': [' 2026-01-01'], 'to': ['2026-12-31']}, {'from': ['2026-01-01'], 'to': ['2026-12-31 ']},   # Leerraum wird nicht getrimmt
    {'from': ['26-01-01'], 'to': ['2026-12-31']}, {'from': ['2026-1-1'], 'to': ['2026-12-31']},
    {'from': ['２０２６-01-01'], 'to': ['2026-12-31']},                      # Ziffern anderer Schriften
    {'from': ['1899-12-31'], 'to': ['2026-12-31']}, {'from': ['2026-01-01'], 'to': ['2201-01-01']},
    {'from': ['2026-01-01']}, {'to': ['2026-12-31']},                      # nur einer von beiden
    {'from': [''], 'to': ['2026-12-31']}, {'from': ['2026-01-01'], 'to': ['']},
    {'from': ['2026-01-01', '2026-02-01'], 'to': ['2026-12-31']},          # doppelt: ungültig, kein erster Wert
    {'from': ['2026-01-01'], 'to': ['2026-12-31', '2026-12-30']},
    {'from': ['2026-12-31'], 'to': ['2026-01-01']},                        # von nach bis: kein stiller Tausch
]


@pytest.mark.parametrize('qs', INVALID_PERIODS)
def test_an_invalid_period_gives_the_current_year_with_a_note(qs):
    assert read_euer_page_query(qs, datetime.date(2026, 11, 21)) == EuerQuery('2026-01-01', '2026-12-31', invalid=True)


@pytest.mark.parametrize('qs', [{}, *INVALID_PERIODS])
def test_the_pdf_address_needs_a_valid_period(qs):
    assert read_euer_pdf_query(qs) == (None, 'Ungültiger Zeitraum.') and INVALID_PERIOD_ERROR == 'Ungültiger Zeitraum.'


def test_the_pdf_address_reads_a_valid_period():
    assert read_euer_pdf_query(_qs(**{'from': '2026-01-01', 'to': '2026-12-31'})) == (EuerQuery('2026-01-01', '2026-12-31'), None)
    assert read_euer_pdf_query(_qs(**{'from': '2026-03-05', 'to': '2026-03-05'})) == (EuerQuery('2026-03-05', '2026-03-05'), None)


# ═══ Menü ═════════════════════════════════════════════════════════════════════
def test_the_submenu_lists_the_pages_and_marks_the_active_one():
    assert [key for key, _path, _label in REPORT_PAGES] == ['bwa', 'euer']
    assert reports_submenu('euer') == '<a href="/bwa">BWA</a> | <span id="ActivePage">EÜR</span>'
    assert reports_submenu('bwa') == '<span id="ActivePage">BWA</span> | <a href="/euer">EÜR</a>'
    assert reports_submenu('susa') == '<a href="/bwa">BWA</a> | <a href="/euer">EÜR</a>'          # eine unbekannte Seite markiert keine


# ═══ Seite ════════════════════════════════════════════════════════════════════
def _page(db, query=None, today=None):
    return PageEuer(db, query or EuerQuery('2026-01-01', '2026-12-31'), today or created())


def table_rows(page):
    """Der sichtbare Text der Tabelle: eine Zeile je ``tr``, die Zellen durch ``|`` getrennt."""
    body = page[page.index('<table class="reportTable">'):page.index('</table>')]
    rows = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', body, flags=re.S):
        found = [html_lib.unescape(re.sub(r'<[^>]+>', '', cell)).strip() for cell in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S)]
        rows.append(' | '.join(found))
    return rows


def test_the_page_shows_the_submenu_the_form_and_the_table(tmp_db):
    some_books(tmp_db)
    book(tmp_db, 3806, '40.00', '2026-03-12')
    page = _page(tmp_db)
    assert reports_submenu('euer') in page and '<span id="ActivePage">Auswertungen</span>' in Header1('bwa')
    dom = Dom(page)
    assert dom.one('form', action='/euer')['method'] == 'get'
    assert dom.one('input', name='from')['value'] == '2026-01-01' and 'required' in dom.one('input', name='from')
    assert dom.one('input', name='to')['value'] == '2026-12-31' and 'required' in dom.one('input', name='to')
    assert [attrs['type'] for tag, attrs in dom.tags if tag == 'input' and attrs.get('name') in ('from', 'to')] == ['date', 'date']
    links = [attrs['href'] for tag, attrs in dom.tags if tag == 'a' and attrs.get('href', '').startswith('/euer?')]
    assert links == [f'/euer?from={year}-01-01&to={year}-12-31' for year in (2026, 2025, 2024, 2023, 2022)] and YEAR_LINKS == 5
    assert f'{EUER_TITLE} – 2026</h3>' in page and EUER_HINT_TEXT in page and EUER_OTHER_TEXT in page and 'warnColor' not in page
    assert 'onclick="euerPdf()"' in page
    rows = table_rows(page)
    assert rows[0] == f'{COLUMN_ACCOUNT} | {COLUMN_NAME} | {COLUMN_AMOUNT}' == 'Konto | Bezeichnung | Saldo €'
    assert rows[1:6] == ['Einnahmen', '3806 | Umsatzsteuer 19% | 40,00', '3806 | Umsatzsteuer 19% | 19,00',
                         '4400 | Erlöse 19% USt | 100,00', 'Summe Betriebseinnahmen | 159,00']
    assert rows[6] == 'Ausgaben' and 'Summe Betriebsausgaben | -94,50' in rows
    assert rows[rows.index('Summe Betriebsausgaben | -94,50') + 1:] == [
        'Ergebnis | 64,50', OTHER_HEADING, '3160 | Verbindlichkeiten ggü. Kreditinstituten – 1-5 Jahre | -30,00', 'Summe Sonstige Ausgaben | -30,00']


def test_the_page_names_a_month_by_its_days_and_counts_only_that_month(tmp_db):
    some_books(tmp_db)
    page = _page(tmp_db, EuerQuery('2026-03-01', '2026-03-31'))
    assert f'{EUER_TITLE} – 01.03.2026 – 31.03.2026</h3>' in page
    assert Dom(page).one('input', name='from')['value'] == '2026-03-01'
    rows = table_rows(page)
    assert 'Summe Betriebseinnahmen | 119,00' in rows and 'Ergebnis | 4,50' in rows          # die Erstattung vom April steht nicht darin
    assert not any(row.startswith('3841 ') for row in rows)


def test_the_invalid_period_note_stands_only_for_an_invalid_period(tmp_db):
    assert f'<p class="warnColor">{EUER_INVALID_TEXT}</p>' in _page(tmp_db, EuerQuery('2026-01-01', '2026-12-31', invalid=True))
    assert 'warnColor' not in _page(tmp_db)


def test_the_page_of_an_empty_book_shows_every_section_with_zero(tmp_db):
    assert table_rows(_page(tmp_db)) == [
        'Konto | Bezeichnung | Saldo €', 'Einnahmen', 'Summe Betriebseinnahmen | 0,00', 'Ausgaben', 'Summe Betriebsausgaben | 0,00',
        'Ergebnis | 0,00', OTHER_HEADING, 'Summe Sonstige Ausgaben | 0,00']


def test_the_year_links_count_from_the_day_the_route_passes_in(tmp_db):
    dom = Dom(_page(tmp_db, today=datetime.date(2031, 6, 30)))
    links = [attrs['href'] for tag, attrs in dom.tags if tag == 'a' and attrs.get('href', '').startswith('/euer?')]
    assert [link.split('=')[1][:4] for link in links] == ['2031', '2030', '2029', '2028', '2027']


def _dashboard_lines(box):
    """Die Zeilen des Kastens im Dashboard, so geschrieben wie die der Seite: Abschnitt, Konten, Summen."""
    token = re.compile(r"<h4[^>]*>(?P<heading>.*?)</h4>"
                       r"|<tr><td>(?P<number>[^<]*)</td><td>(?P<name>.*?)</td><td style='[^']*'>(?P<amount>.*?)</td></tr>"
                       r"|<td[^>]*><strong>(?P<label>.*?)</strong></td>\s*<td[^>]*>\s*(?P<total>.*?)</td></tr>", re.S)
    renamed = {'Sonstige Ausgaben': OTHER_HEADING, 'Ergebnis EÜR': 'Ergebnis'}
    lines = []
    for found in token.finditer(box):
        if found['heading']:
            lines.append(renamed.get(found['heading'], found['heading']))
        elif found['number'] is not None:
            lines.append(f"{found['number']} | {html_lib.unescape(found['name'])} | {found['amount'].removesuffix(' €')}")
        else:
            lines.append(f"{renamed.get(found['label'], found['label'])} | {found['total'].strip().removesuffix(' €')}")
    return lines


@pytest.mark.parametrize('case', ['world_year', 'world_march', 'demo_2025', 'demo_2026', 'extra_year', 'extra_march', 'fractions', 'empty'])
def test_the_page_shows_what_the_dashboard_box_shows(case, request):
    """Dasselbe Konto mit demselben Betrag, dieselben Summen und dasselbe Ergebnis: Kasten und Seite haben EINE Aufbereitung."""
    fixture, date_from, date_to, _account = BOX_CASES[case]
    db = request.getfixturevalue(fixture)
    box = _box(PageDashboard(db, date_from, date_to))
    shown = table_rows(_page(db, EuerQuery(date_from, date_to)))[1:]                    # ohne die Spaltenköpfe
    assert shown == _dashboard_lines(box), case
    assert len(shown) >= 7


def test_the_page_uses_no_inline_style_and_only_var_and_function(tmp_db):
    some_books(tmp_db)
    page = _page(tmp_db)
    own = page[page.index("<div class='header2'>"):page.index('<div id="app_confirm"')]          # Untermenü, Formular und Karte
    assert ' style=' not in own and 'innerHTML' not in own
    script = Dom(page).scripts[0]
    assert script.startswith('\nvar EUER_DATA = ')
    for forbidden in ('const ', 'let ', '=>', 'innerHTML', 'eval('):
        assert forbidden not in script, forbidden


def test_the_pdf_address_stands_only_in_the_script_block(tmp_db):
    some_books(tmp_db)
    page = _page(tmp_db, EuerQuery('2026-03-01', '2026-03-31'))
    assert '/euer/pdf' not in re.sub(r'<script>.*?</script>', '', page, flags=re.S)
    data = re.search(r'var EUER_DATA = (.*?);\n', Dom(page).scripts[0], re.S).group(1)
    assert json.loads(data) == {'pdf': '/euer/pdf?from=2026-03-01&to=2026-03-31'}


def test_names_with_markup_are_escaped_on_the_page_and_in_the_box(tmp_db, tmp_path):
    benign = Database(db_name=str(tmp_path / 'benign.db'))
    for db, name in ((tmp_db, HOSTILE), (benign, 'Harmloser Name')):
        book(db, 4400, '119.00', '2026-03-10', tax_rate='19', tax_amount='19.00')
        book(db, 6890, '-12.00', '2026-03-11', name=name)
    hostile_page, benign_page = _page(tmp_db), _page(benign)
    assert HOSTILE not in hostile_page and '<script>alert(1)</script>' not in hostile_page
    assert html_lib.escape(HOSTILE, quote=True) in hostile_page
    assert_same_structure(hostile_page, benign_page)
    assert any(row.startswith('6890 | ' + HOSTILE) for row in table_rows(hostile_page))             # als Text, nicht als Markup
    box = _box(PageDashboard(tmp_db, '2026-01-01', '2026-12-31'))
    assert HOSTILE not in box and html_lib.escape(HOSTILE, quote=True) in box


def test_no_brand_name_stands_on_the_page_or_in_the_pdf(tmp_db):
    some_books(tmp_db)
    page = _page(tmp_db)
    visible = html_lib.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'<script>.*?</script>', '', page, flags=re.S)))
    statement = euer_statement(tmp_db.get_euer_data('2026-01-01', '2026-12-31'), bank_skr_numbers(tmp_db.fetch_accounts()))
    pdf = generate_euer_pdf(statement, '2026-01-01', '2026-12-31', 'Eigene Firma GmbH', created())
    pdf_text = ' '.join(text for stream in _streams(pdf) for _x, _y, text in _cells(stream))
    for brand in BRANDS:
        assert brand.lower() not in visible.lower() and brand.lower() not in pdf_text.lower(), brand
    assert 'Kein amtliches Formular' in EUER_HINT_TEXT and 'Kein amtliches Formular' in EUER_FOOTER[0]


def test_building_the_page_writes_nothing(tmp_db):
    some_books(tmp_db)
    before = dump(tmp_db.db_name)
    _page(tmp_db)
    assert dump(tmp_db.db_name) == before


# ═══ PDF ══════════════════════════════════════════════════════════════════════
def _pdf(rows=ROWS, company='Eigene Firma GmbH', date_from='2026-01-01', date_to='2026-12-31'):
    return generate_euer_pdf(euer_statement(rows, BANKS), date_from, date_to, company, created())


def _page_cells(pdf):
    return [_cells(stream) for stream in _streams(pdf)]


def _table_rows(cells):
    """Die Zeilen der Tabelle einer Seite von oben nach unten: die Texte je Zeile, von links nach rechts."""
    by_line = {}
    for x, y, text in cells:
        if BOTTOM_Y <= y <= TABLE_TOP:
            by_line.setdefault(y, []).append((x, text))
    return [tuple(text for _x, text in sorted(by_line[y])) for y in sorted(by_line, reverse=True)]


def test_the_lines_per_page_end_above_the_margin():
    assert LINES_PER_PAGE == 56
    assert TABLE_TOP - (LINES_PER_PAGE - 1) * ROW_H >= BOTTOM_Y > TABLE_TOP - LINES_PER_PAGE * ROW_H
    assert page_layout([]) == [[]] and page_layout([3, LINES_PER_PAGE - 2, 2]) == [[3], [LINES_PER_PAGE - 2, 2]]


def test_the_pdf_is_portrait_and_names_the_company_and_the_period():
    pdf = _pdf()
    pages = list(PDFPage.create_pages(_doc(pdf)))
    assert [tuple(int(value) for value in page.mediabox) for page in pages] == [(0, 0) + PAGE_SIZE] == [(0, 0, 595, 842)]
    [cells] = _page_cells(pdf)
    texts = [text for _x, _y, text in cells]
    assert EUER_TITLE in texts and 'Eigene Firma GmbH' in texts and 'Zeitraum: 01.01.2026 bis 31.12.2026' in texts
    assert 'Erstellt am 21.11.2026' in texts and 'Konto' in texts and 'Bezeichnung' in texts and 'Saldo €' in texts
    assert list(EUER_FOOTER) == [text for text in texts if text in EUER_FOOTER]
    assert not [text for text in texts if text.startswith('Seite ')]                       # eine Seite: keine Seitenzahl
    info = _doc(pdf).info[0]
    assert info['Title'] == b'\xfe\xff' + EUER_TITLE.encode('utf-16-be') and info['Author'] == b'Eigene Firma GmbH'
    assert b'/F1 ' in pdf and b'/F2 ' in pdf


def test_the_pdf_names_a_month_by_its_days():
    [cells] = _page_cells(_pdf(date_from='2026-03-01', date_to='2026-03-31'))
    assert 'Zeitraum: 01.03.2026 bis 31.03.2026' in [text for _x, _y, text in cells]


def test_without_a_company_name_the_company_line_is_left_out():
    [cells] = _page_cells(_pdf(company=''))
    assert all(y != COMPANY_Y for _x, y, _text in cells)
    [cells] = _page_cells(_pdf())
    assert any(y == COMPANY_Y for _x, y, _text in cells)


def test_the_pdf_prints_the_sections_the_accounts_and_the_sums_in_order():
    [cells] = _page_cells(_pdf())
    assert _table_rows(cells) == [
        ('Einnahmen',), ('3806', 'Umsatzsteuer 19%', '40,00'), ('3806', 'Umsatzsteuer 19%', '19,00'), ('4400', 'Erlöse 19% USt', '100,00'),
        ('Summe Betriebseinnahmen', '159,00'),
        ('Ausgaben',), ('1406', 'Abziehbare Vorsteuer 19%', '-9,50'), ('6815', 'Bürobedarf', '-50,00'), ('Summe Betriebsausgaben', '-59,50'),
        ('Ergebnis', '99,50'),
        (OTHER_HEADING,), ('3160', 'Darlehen', '-30,00'), ('Summe Sonstige Ausgaben', '-30,00')]


def test_the_pdf_of_an_empty_book_has_every_section():
    [cells] = _page_cells(_pdf([]))
    assert _table_rows(cells) == [('Einnahmen',), ('Summe Betriebseinnahmen', '0,00'), ('Ausgaben',), ('Summe Betriebsausgaben', '0,00'),
                                  ('Ergebnis', '0,00'), (OTHER_HEADING,), ('Summe Sonstige Ausgaben', '0,00')]


def _many(income, expense, other):
    """Zeilen für ``income`` Einnahmen-, ``expense`` Ausgaben- und ``other`` „Sonstige“ Konten (dieselbe Nummer 3160, verschiedene Namen)."""
    rows = [(4000 + index, f'Erlös {index}', 10.0 + index) for index in range(income)]
    rows += [(6000 + index, f'Aufwand {index}', -(1.0 + index)) for index in range(expense)]
    rows += [(3160, f'Sonstiges {index}', -2.0 - index) for index in range(other)]
    return rows


def test_many_accounts_run_over_several_pages_and_nothing_lies_below_the_margin():
    rows = _many(10, 130, 3)                                   # der Block der Ausgaben ist 132 Zeilen hoch: länger als eine Seite
    statement = euer_statement(rows, BANKS)
    pages = _page_cells(_pdf(rows))
    total = len(pages)
    assert total >= 4
    for number, cells in enumerate(pages, start=1):
        below = sorted((y, text) for _x, y, text in cells if y < BOTTOM_Y)
        assert below == sorted([(FOOTER_TOP_Y, EUER_FOOTER[0]), (FOOTER_Y, EUER_FOOTER[1]), (FOOTER_Y, f'Seite {number} von {total}')]), number
        texts = [text for _x, _y, text in cells]
        assert EUER_TITLE in texts and 'Eigene Firma GmbH' in texts and 'Saldo €' in texts and 'Zeitraum: 01.01.2026 bis 31.12.2026' in texts
    printed = [row for cells in pages for row in _table_rows(cells)]
    assert [row[0] for row in printed if re.fullmatch(r'\d{4}', row[0])] == [
        str(line.number) for line in statement.income + statement.expense + statement.other]           # jedes Konto genau einmal, in der Reihenfolge
    assert [row for row in printed if row[0] == 'Ergebnis'] == [('Ergebnis', format_amount(statement.result))]


def test_no_block_is_split_where_it_fits_on_a_page():
    """Jeder Abschnitt, der auf eine Seite passt, steht ganz auf einer: Die erste Zeile jeder Seite ist die Überschrift eines Abschnitts
    (bei 30 Konten je Abschnitt 32 Zeilen hoch: einer je Seite, das Ergebnis hinter den Ausgaben)."""
    pages = _page_cells(_pdf(_many(30, 30, 30)))
    assert len(pages) == 3
    assert [_table_rows(cells)[0] for cells in pages] == [('Einnahmen',), ('Ausgaben',), (OTHER_HEADING,)]
    assert _table_rows(pages[1])[-1][0] == 'Ergebnis'


def test_one_block_longer_than_a_page_starts_on_a_new_page_and_runs_over():
    pages = _page_cells(_pdf(_many(2, 130, 0)))
    first_rows = [_table_rows(cells)[0] for cells in pages]
    assert first_rows[0] == ('Einnahmen',) and first_rows[1] == ('Ausgaben',)               # der lange Block beginnt oben auf einer neuen Seite
    assert re.fullmatch(r'\d{4}', first_rows[2][0])                                          # und läuft auf der nächsten mit einem Konto weiter


@pytest.mark.parametrize('date_from, date_to', [('2026-01-01', '2026-12-31'), ('2026-03-01', '2026-03-31')])
def test_every_amount_ends_on_the_right_edge(date_from, date_to):
    """Gemessen mit pdfplumber (den Breiten der eingebetteten Schrift im PDF), nicht mit den Funktionen des PDF-Baus."""
    rows = ROWS + [(6850, 'Sonstiger Betriebsbedarf', -1234567.89), (4300, 'Erlöse 7% USt', 98765432.1)]
    with pdfplumber.open(io.BytesIO(_pdf(rows, date_from=date_from, date_to=date_to))) as document:
        words = [word for page in document.pages for word in page.extract_words()]
    numbers = [word for word in words if re.fullmatch(r'-?\d{1,3}(?:\.\d{3})*,\d\d', word['text'])]
    assert len(numbers) >= 9
    for word in numbers:
        assert abs(word['x1'] - RIGHT_X) < 0.6, (word['text'], word['x1'])
    assert any(word['text'] == '98.765.432,10' for word in numbers) and any(word['text'] == '-1.234.567,89' for word in numbers)


def test_the_footer_lines_fit_next_to_the_page_number():
    assert info_width(EUER_FOOTER[1], 7) + info_width('Seite 99 von 99', 7) + 20 < RIGHT_X - LEFT_X
    assert info_width(EUER_FOOTER[0], 7) < RIGHT_X - LEFT_X


def test_a_name_with_brackets_a_backslash_and_umlauts_prints_unchanged():
    name = '(Test) Bürobedarf\\Ä – € ß'
    pdf = _pdf([(6815, name, -10.0)], company='Muster (Test) GmbH \\ Co. KG')
    [cells] = _page_cells(pdf)
    texts = [text for _x, _y, text in cells]
    assert name in texts and 'Muster (Test) GmbH \\ Co. KG' in texts
    assert _doc(pdf).info[0]['Author'] == b'Muster (Test) GmbH \\ Co. KG'


def test_control_characters_in_a_name_do_not_break_the_stream():
    [stream] = _streams(_pdf([(6815, 'Zeile eins\nZeile zwei\tEnde\r\x00\x07 Schluss', -10.0)]))
    assert 'Zeile eins Zeile zwei Ende Schluss' in [text for _x, _y, text in _cells(stream)]
    assert not re.search(r'[\x00-\x08\x0b-\x1f\x7f]', stream.replace('\n', ' '))


def test_an_account_without_a_name_prints_number_and_amount_and_never_none():
    [cells] = _page_cells(_pdf([(6815, None, -10.0), (6850, '', -5.0)]))
    assert not any('None' in text for _x, _y, text in cells)
    rows = _table_rows(cells)
    assert ('6815', '-10,00') in rows and ('6850', '-5,00') in rows


def test_overlong_names_are_cut_with_an_ellipsis_to_the_width_of_their_place():
    pdf = _pdf([(6815, 'W' * 400, -10.0), (6850, 'Wort ' * 300, -5.0), (6805, 'ß' * 190, -2.0)], company='Firma ' * 100)
    [cells] = _page_cells(pdf)
    names = [text for x, y, text in cells if x == NAME_X and BOTTOM_Y <= y <= TABLE_TOP]
    assert len(names) == 3 and all(name.endswith('…') for name in names)
    for name in names:
        assert info_width(name, ROW_SIZE) <= NAME_WIDTH, name
    [company] = [text for x, y, text in cells if y == COMPANY_Y]
    assert company.endswith('…') and info_width(company, 10) <= RIGHT_X - LEFT_X


def test_the_pdf_of_every_period_of_a_book_is_built(tmp_db):
    some_books(tmp_db)
    for date_from, date_to in (('2026-01-01', '2026-12-31'), ('2026-03-01', '2026-03-31'), ('2025-01-01', '2026-12-31'),
                               ('1900-01-01', '1900-01-01'), ('2200-12-31', '2200-12-31')):
        statement = euer_statement(tmp_db.get_euer_data(date_from, date_to), bank_skr_numbers(tmp_db.fetch_accounts()))
        assert generate_euer_pdf(statement, date_from, date_to, '', created()).startswith(b'%PDF-')


@pytest.mark.parametrize('fixture, date_from, date_to', [('world', '2026-01-01', '2026-12-31'), ('demo_db', '2025-01-01', '2025-12-31'),
                                                         ('demo_db', '2026-01-01', '2026-10-31')])
def test_the_numbers_of_the_pdf_are_those_of_the_page(fixture, date_from, date_to, request):
    db = request.getfixturevalue(fixture)
    statement = euer_statement(db.get_euer_data(date_from, date_to), bank_skr_numbers(db.fetch_accounts()))
    pages = _page_cells(generate_euer_pdf(statement, date_from, date_to, '', created()))
    printed = [row for cells in pages for row in _table_rows(cells)]
    page_rows = [tuple(part.strip() for part in row.split(' | ')) for row in table_rows(_page(db, EuerQuery(date_from, date_to)))[1:]]
    assert printed == page_rows and len(printed) > 10


# ═══ Routen ═══════════════════════════════════════════════════════════════════
def test_the_page_and_the_pdf_are_get_routes_that_write_nothing(server_port, auto_login):
    """Der Nachweis für die Positivliste des Diagnose-MCP (``/euer``): Datenbank und Datenordner sind nach jedem Aufruf unverändert, es gibt kein
    Cookie. ``/euer/pdf`` steht nicht in der Liste (ein Dokument), schreibt aber auch nichts."""
    some_books(auto_login.db)
    before_db, before_tree = dump(auto_login.db.db_name), tree(userctx.DATA_ROOT)
    for path in ('/euer', '/euer?from=2026-01-01&to=2026-12-31', '/euer?from=2026-03-01&to=2026-03-31', '/euer?from=x',
                 '/euer?from=2026-12-31&to=2026-01-01', '/euer?from=2026-01-01', '/euer?to=2026-12-31',
                 '/euer?from=2026-01-01&from=2026-02-01&to=2026-12-31', '/euer?from=&to=', '/euer?acct=1',
                 '/euer/pdf?from=2026-01-01&to=2026-12-31', '/euer/pdf?from=2026-03-01&to=2026-03-31', '/euer/pdf', '/euer/pdf?from=bad'):
        response = get(server_port, path)
        assert response.headers.get('Set-Cookie') is None, path
        assert response.status in (200, 400), path
    assert dump(auto_login.db.db_name) == before_db
    assert tree(userctx.DATA_ROOT) == before_tree


def test_the_page_answers_for_the_period_in_the_address(server_port, auto_login):
    some_books(auto_login.db)
    response = get(server_port, '/euer?from=2026-03-01&to=2026-03-31')
    assert response.status == 200 and response.headers['Content-Type'].startswith('text/html')
    assert 'Einnahmenüberschussrechnung – 01.03.2026 – 31.03.2026' in response.text and 'warnColor' not in response.text
    rows = table_rows(response.text)
    assert reports_submenu('euer') in response.text
    assert 'Summe Betriebseinnahmen | 119,00' in rows and 'Summe Betriebsausgaben | -114,50' in rows and 'Ergebnis | 4,50' in rows
    bare = get(server_port, '/euer')
    year = re.search(r'Einnahmenüberschussrechnung – (\d{4})</h3>', bare.text).group(1)         # das laufende Jahr, ohne Hinweis
    assert bare.status == 200 and 'warnColor' not in bare.text
    assert Dom(bare.text).one('input', name='from')['value'] == f'{year}-01-01' and Dom(bare.text).one('input', name='to')['value'] == f'{year}-12-31'
    assert 'warnColor' in get(server_port, '/euer?from=2026-12-31&to=2026-01-01').text           # kein stiller Tausch
    assert 'warnColor' in get(server_port, '/euer?from=2026-01-01').text


def test_the_pdf_route_answers_with_a_download(server_port, auto_login):
    db = auto_login.db
    db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Firma GmbH', company_name='Eigene Firma GmbH')
    some_books(db)
    year = get(server_port, '/euer/pdf?from=2026-01-01&to=2026-12-31')
    assert year.status == 200 and year.headers['Content-Type'] == 'application/pdf' and year.body.startswith(b'%PDF-')
    assert year.headers['Content-Disposition'].startswith('attachment;') and 'EUER_2026.pdf' in year.headers['Content-Disposition']
    assert year.headers['X-Filename'] == quote('EUER_2026.pdf', safe='')
    month = get(server_port, '/euer/pdf?from=2026-03-01&to=2026-03-31')
    assert month.status == 200 and month.headers['X-Filename'] == quote('EUER_2026-03-01_2026-03-31.pdf', safe='')
    texts = [text for stream in _streams(month.body) for _x, _y, text in _cells(stream)]
    assert 'Eigene Firma GmbH' in texts and 'Zeitraum: 01.03.2026 bis 31.03.2026' in texts and 'Ergebnis' in texts


def test_the_pdf_route_refuses_what_it_cannot_read(server_port, auto_login):
    for path in ('/euer/pdf', '/euer/pdf?from=2026-01-01', '/euer/pdf?to=2026-12-31', '/euer/pdf?from=2026-02-30&to=2026-12-31',
                 '/euer/pdf?from=%202026-01-01&to=2026-12-31', '/euer/pdf?from=26-01-01&to=2026-12-31',
                 '/euer/pdf?from=2026-12-31&to=2026-01-01', '/euer/pdf?from=2026-01-01&from=2026-01-02&to=2026-12-31'):
        response = get(server_port, path)
        assert response.status == 400 and response.text == 'Ungültiger Zeitraum.', path
        assert response.headers['Content-Type'] == 'text/plain; charset=utf-8'


def test_a_failing_pdf_is_a_fixed_sentence_never_the_text_of_the_error(server_port, auto_login, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError('Geheimer Pfad /srv/daten')
    monkeypatch.setattr(pdf_euer, 'generate_euer_pdf', broken)
    response = get(server_port, '/euer/pdf?from=2026-01-01&to=2026-12-31')
    assert response.status == 500 and response.text == EUER_PDF_FAILED_TEXT
    assert 'Geheimer Pfad' not in response.text and response.headers['Content-Type'] == 'text/plain; charset=utf-8'
