# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Zeitraum-Parameter und Header (server/period.py, server/app.py, Seiten, DATEV-Export), 2.0.3.

``from``/``to`` in der Adresse und der Cookie ``period`` kommen vom Client und landen in HTML,
JavaScript, Set-Cookie-Headern, Weiterleitungen und Dateinamen. Deshalb gelten nur ISO-Daten
(``period.parse_iso_date``); alles andere wird verworfen. Geprüft wird

- ``parse_iso_date``, ``valid_period``, ``_read_cookie``, ``resolve_period``,
  ``period_cookie_header`` (Tabellen gültig/ungültig, Priorität Query > Cookie > Standardjahr)
- ``_header_safe``/``send_header`` (CR/LF/NUL und Zeichen außerhalb von Latin-1) und ``_int_param``
- ``period_filter_widget`` und die Seiten Trips, Arbeitszeiten, Dashboard mit feindlichen Werten:
  kein roher Wert in der Ausgabe, der DOM-Aufbau hängt nicht vom Wert ab
- über den echten Server: Seiten mit feindlicher Adresse/feindlichem Cookie, ``person=abc``,
  Header-Injektion in der Rohantwort, Weiterleitungen von ``/trips/add`` und ``/worktime/add``
- ``handle_datev_export`` mit ungültigen Daten

Alle Werte sind erfundene Testdaten.
"""
import re
import socket
import sqlite3
import sys
import urllib.parse
from datetime import datetime
from types import SimpleNamespace

import pytest

from server import handlers, period
from server.app import SimpleWebServer, _header_safe, _int_param
from server.pages_dashboard import PageDashboard
from server.pages_trips import PageTrips
from server.pages_worktime import PageWorkTimes
from tests.dom_helpers import Dom, assert_same_structure, data_block
from tests.http_client import TIMEOUT, get, post

SCRIPT = '<script>alert(1)</script>'
FORMS = {'script': '"><script>alert(1)</script>', 'js': "2026-01-01'+alert(1)+'"}


# ── parse_iso_date ───────────────────────────────────────────────────────────
VALID_DATES = ['2026-01-01', '2026-12-31', '2024-02-29', '2000-02-29', '2026-02-28', '1900-01-01',
               '2200-12-31', '1999-12-31', '2026-03-05']

INVALID_DATES = [
    pytest.param(' 2026-01-01', id='Leerzeichen-davor'),
    pytest.param('2026-01-01 ', id='Leerzeichen-dahinter'),
    pytest.param('\t2026-01-01', id='Tabulator-davor'),
    pytest.param('2026-1-5', id='ohne-fuehrende-Nullen'),
    pytest.param('2026-01-5', id='Tag-einstellig'),
    pytest.param('2026-1-05', id='Monat-einstellig'),
    pytest.param('2026-02-30', id='30.-Februar'),
    pytest.param('2025-02-29', id='kein-Schaltjahr'),
    pytest.param('1900-02-29', id='1900-kein-Schaltjahr'),
    pytest.param('2100-02-29', id='2100-kein-Schaltjahr'),
    pytest.param('2026-04-31', id='31.-April'),
    pytest.param('2026-13-01', id='Monat-13'),
    pytest.param('2026-00-10', id='Monat-0'),
    pytest.param('2026-01-00', id='Tag-0'),
    pytest.param('2026-01-32', id='Tag-32'),
    pytest.param('1899-12-31', id='Jahr-1899'),
    pytest.param('2201-01-01', id='Jahr-2201'),
    pytest.param('0000-01-01', id='Jahr-0'),
    pytest.param('0001-01-01', id='Jahr-1'),
    pytest.param('9999-12-31', id='Jahr-9999'),
    pytest.param('２０２６-０１-０１', id='Ziffern-Vollbreite'),
    pytest.param('٢٠٢٦-٠١-٠١', id='Ziffern-arabisch-indisch'),
    pytest.param('२०२६-०१-०१', id='Ziffern-Devanagari'),
    pytest.param(None, id='None'),
    pytest.param(20260101, id='Zahl'),
    pytest.param(20260101.0, id='Kommazahl'),
    pytest.param(True, id='Wahrheitswert'),
    pytest.param(['2026-01-01'], id='Liste'),
    pytest.param(('2026-01-01',), id='Tupel'),
    pytest.param(b'2026-01-01', id='Bytes'),
    pytest.param('2026-01-01\n', id='Zeilenumbruch-am-Ende'),
    pytest.param('2026-01-01\r\n', id='CRLF-am-Ende'),
    pytest.param('\n2026-01-01', id='Zeilenumbruch-davor'),
    pytest.param('2026-01-01\x00', id='NUL-am-Ende'),
    pytest.param('2026-01-01\x00evil', id='NUL-in-der-Mitte'),
    pytest.param('2026-01-01\r\nSet-Cookie: a=b', id='Header-Einschleusung'),
    pytest.param(SCRIPT, id='Skript'),
    pytest.param('"' + '>' + SCRIPT, id='Attribut-Ausbruch'),
    pytest.param("2026-01-01'+alert(1)+'", id='JavaScript-Ausbruch'),
    pytest.param('x' * 10000, id='10000-Zeichen'),
    pytest.param('2026-01-01' + 'x' * 10000, id='Datum-mit-10000-Zeichen-dahinter'),
    pytest.param('2026-01-01' * 1000, id='Datum-tausendfach'),
    pytest.param('', id='leer'),
    pytest.param('2026/01/01', id='Schraegstriche'),
    pytest.param('2026.01.01', id='Punkte'),
    pytest.param('01.01.2026', id='deutsche-Schreibweise'),
    pytest.param('20260101', id='ohne-Trennzeichen'),
    pytest.param('2026-W01-1', id='Wochendatum'),
    pytest.param('2026-001', id='Tag-im-Jahr'),
    pytest.param('2026-01-01T00:00:00', id='mit-Uhrzeit'),
    pytest.param('+2026-01-01', id='Plus-davor'),
    pytest.param('-2026-01-01', id='Minus-davor'),
    pytest.param('2026-01-01..2026-12-31', id='ganzer-Zeitraum'),
    pytest.param('2026-01-01&to=2026-01-02', id='Parameter-angehaengt'),
    pytest.param('2026-01-01;', id='Semikolon'),
]


@pytest.mark.parametrize('value', VALID_DATES)
def test_parse_iso_date_returns_a_valid_date_unchanged(value):
    result = period.parse_iso_date(value)
    assert result == value and type(result) is str


@pytest.mark.parametrize('value', INVALID_DATES)
def test_parse_iso_date_rejects_everything_else(value):
    assert period.parse_iso_date(value) is None


def test_iso_pattern_is_ascii_only_and_anchored_on_both_ends():
    """Auch ohne den Kalender-Test dahinter: ``\\d`` träfe andere Ziffernschriften, ``match``
    ließe Angehängtes durch, ``$`` einen Zeilenumbruch am Ende."""
    pattern = period._ISO_DATE
    assert pattern.fullmatch('2026-01-01')
    for value in ('٢٠٢٦-٠١-٠١', '２０２６-０１-０１', '2026-01-01\n', '2026-01-01x', 'x2026-01-01',
                  '2026-01-01\r\n', ' 2026-01-01'):
        assert pattern.fullmatch(value) is None, repr(value)


def test_the_pattern_alone_rejects_what_the_calendar_check_would_let_through(monkeypatch):
    """Zwei Sicherungen hintereinander: Muster und Kalenderprüfung. Mit einem nachsichtigen Kalender
    (nimmt jede Zeichenkette an) muss das Muster allein alles Unzulässige abfangen."""
    class Lenient(period.date):
        @classmethod
        def fromisoformat(cls, value):
            return cls(2026, 1, 1)
    monkeypatch.setattr(period, 'date', Lenient)
    for value in ('2026-01-01\n', '2026-01-01\r\n', '٢٠٢٦-٠١-٠١', '２０２６-０１-０１', '2026-01-01x',
                  'x2026-01-01', ' 2026-01-01', '2026-01-01 ', '2026-1-1', '2026-01-01' + 'x' * 1000, ''):
        assert period.parse_iso_date(value) is None, repr(value)
    assert period.parse_iso_date('2026-01-01') == '2026-01-01'


# ── valid_period ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize('pair, expected', [
    pytest.param(('2026-01-01', '2026-12-31'), ('2026-01-01', '2026-12-31'), id='gueltig'),
    pytest.param(('2026-12-31', '2026-01-01'), ('2026-01-01', '2026-12-31'), id='vertauscht'),
    pytest.param(('2026-03-05', '2026-03-05'), ('2026-03-05', '2026-03-05'), id='ein-Tag'),
    pytest.param(('1900-01-01', '2200-12-31'), ('1900-01-01', '2200-12-31'), id='Grenzen'),
    pytest.param(('2200-12-31', '1900-01-01'), ('1900-01-01', '2200-12-31'), id='Grenzen-vertauscht'),
    pytest.param(('2025-12-31', '2026-01-01'), ('2025-12-31', '2026-01-01'), id='Jahreswechsel'),
    pytest.param(('2026-01-01', 'x'), None, id='bis-ungueltig'),
    pytest.param(('x', '2026-12-31'), None, id='von-ungueltig'),
    pytest.param((None, None), None, id='None'),
    pytest.param(('', ''), None, id='leer'),
    pytest.param(('2026-01-01', None), None, id='bis-None'),
    pytest.param(('2026-01-01', '2026-02-30'), None, id='bis-gibt-es-nicht'),
    pytest.param((' 2026-01-01', '2026-02-01'), None, id='Leerzeichen'),
    pytest.param(('2026-01-01\n', '2026-02-01'), None, id='Zeilenumbruch'),
])
def test_valid_period(pair, expected):
    assert period.valid_period(*pair) == expected


# ── _read_cookie ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize('header, expected', [
    pytest.param(None, ('', ''), id='kein-Header'),
    pytest.param('', ('', ''), id='leerer-Header'),
    pytest.param('period=2026-03-01..2026-03-31', ('2026-03-01', '2026-03-31'), id='gueltig'),
    pytest.param('period=2026-03-31..2026-03-01', ('2026-03-01', '2026-03-31'), id='vertauscht'),
    pytest.param('session=abc; period=2026-03-01..2026-03-31', ('2026-03-01', '2026-03-31'),
                 id='nach-anderem-Cookie'),
    pytest.param('period=2026-03-01..2026-03-31; session=abc', ('2026-03-01', '2026-03-31'),
                 id='vor-anderem-Cookie'),
    pytest.param('period=<script>..x', ('', ''), id='Skript'),
    pytest.param('period=abc..def', ('', ''), id='Buchstaben'),
    pytest.param('period=2026-03-01', ('', ''), id='ohne-Trenner'),
    pytest.param('period=2026-03-01..', ('', ''), id='bis-fehlt'),
    pytest.param('period=..2026-03-31', ('', ''), id='von-fehlt'),
    pytest.param('period=..', ('', ''), id='nur-Trenner'),
    pytest.param('period=', ('', ''), id='leer'),
    pytest.param('period=2026-03-01..2026-03-31..2026-04-01', ('', ''), id='drei-Teile'),
    pytest.param('period=2026-03-01..2026-02-30', ('', ''), id='Datum-gibt-es-nicht'),
    pytest.param('other=2026-03-01..2026-03-31', ('', ''), id='anderer-Name'),
    pytest.param('period=<script>alert(1)</script>..<script>alert(2)</script>', ('', ''), id='zwei-Skripte'),
    pytest.param('\x00\x01 ;;; ===', ('', ''), id='Muell'),
])
def test_read_cookie(header, expected):
    assert period._read_cookie(header) == expected


# ── resolve_period ───────────────────────────────────────────────────────────
@pytest.fixture
def frozen_year(monkeypatch):
    """„Heute“ liegt fest im Jahr 2031, damit das Standardjahr nicht vom Lauf abhängt."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2031, 6, 15, 12, 0, 0)
    monkeypatch.setattr(period, 'datetime', Frozen)
    return 2031


COOKIE = 'period=2025-01-01..2025-12-31'
DEFAULT = ('2031-01-01', '2031-12-31', False)


def q(**params):
    return {name: [value] if isinstance(value, str) else value for name, value in params.items()}


@pytest.mark.parametrize('query, cookie, expected', [
    pytest.param(q(), None, DEFAULT, id='nichts-gibt-das-Standardjahr'),
    pytest.param(q(), COOKIE, ('2025-01-01', '2025-12-31', False), id='Cookie-ohne-Query'),
    pytest.param(q(**{'from': '2026-03-01', 'to': '2026-03-31'}), None,
                 ('2026-03-01', '2026-03-31', True), id='Query-setzt-den-Cookie'),
    pytest.param(q(**{'from': '2026-03-01', 'to': '2026-03-31'}), COOKIE,
                 ('2026-03-01', '2026-03-31', True), id='Query-vor-Cookie'),
    pytest.param(q(**{'from': '2026-03-31', 'to': '2026-03-01'}), None,
                 ('2026-03-01', '2026-03-31', True), id='Query-vertauscht'),
    pytest.param(q(date_from='2026-03-01', date_to='2026-03-31'), None,
                 ('2026-03-01', '2026-03-31', True), id='date_from-date_to'),
    pytest.param(q(**{'from': '2026-03-01', 'to': '2026-03-31', 'date_from': '2025-05-01',
                      'date_to': '2025-05-31'}), None,
                 ('2026-03-01', '2026-03-31', True), id='from-to-gewinnt'),
    pytest.param(q(**{'from': SCRIPT, 'to': '2026-03-31'}), COOKIE,
                 ('2025-01-01', '2025-12-31', False), id='ungueltige-Query-faellt-auf-Cookie-zurueck'),
    pytest.param(q(**{'from': SCRIPT, 'to': '2026-03-31'}), None, DEFAULT,
                 id='ungueltige-Query-ohne-Cookie'),
    pytest.param(q(**{'from': '2026-03-01', 'to': 'x'}), 'period=abc..def', DEFAULT,
                 id='alles-ungueltig'),
    pytest.param(q(**{'from': '2026-03-01'}), COOKIE, ('2025-01-01', '2025-12-31', False),
                 id='nur-von'),
    pytest.param(q(**{'to': '2026-03-31'}), None, DEFAULT, id='nur-bis'),
    pytest.param(q(date_from='2026-03-01'), None, DEFAULT, id='nur-date_from'),
    pytest.param(q(**{'from': ['2026-03-01', '2027-01-01'], 'to': ['2026-03-31', 'x']}), None,
                 ('2026-03-01', '2026-03-31', True), id='mehrere-Werte-der-erste-zaehlt'),
    pytest.param(q(**{'from': [], 'to': []}), None, DEFAULT, id='leere-Listen'),
    pytest.param(q(**{'from': '2026-03-01\n', 'to': '2026-03-31'}), None, DEFAULT,
                 id='Zeilenumbruch-am-Ende'),
    pytest.param(q(**{'from': '２０２６-０３-０１', 'to': '2026-03-31'}), None, DEFAULT,
                 id='Vollbreite-Ziffern'),
    pytest.param(q(), 'period=<script>..x', DEFAULT, id='feindlicher-Cookie'),
    pytest.param(q(), 'period=2026-03-31..2026-03-01', ('2026-03-01', '2026-03-31', False),
                 id='Cookie-vertauscht'),
    pytest.param(q(**{'from': '2026-03-01', 'to': '2026-03-31'}), 'period=<script>..x',
                 ('2026-03-01', '2026-03-31', True), id='gueltige-Query-feindlicher-Cookie'),
])
def test_resolve_period(frozen_year, query, cookie, expected):
    assert period.resolve_period(query, cookie) == expected


@pytest.mark.parametrize('query', [
    q(**{'from': '"><script>alert(1)</script>', 'to': '2026-12-31'}),
    q(**{'from': "2026-01-01'+alert(1)+'", 'to': "x'"}),
    q(**{'from': '2026-01-01\r\nSet-Cookie: a=b', 'to': '2026-12-31'}),
    q(**{'from': 'x' * 10000, 'to': 'y' * 10000}),
])
def test_resolved_dates_always_make_a_valid_cookie(frozen_year, query):
    date_from, date_to, set_cookie = period.resolve_period(query, 'period=<script>..x')
    assert (date_from, date_to) == ('2031-01-01', '2031-12-31') and set_cookie is False
    assert period.period_cookie_header(date_from, date_to).startswith('period=2031-01-01..2031-12-31;')


# ── period_cookie_header ─────────────────────────────────────────────────────
def test_period_cookie_header_is_exact():
    assert period.period_cookie_header('2026-03-01', '2026-03-31') == \
        'period=2026-03-01..2026-03-31; Path=/; Max-Age=31536000; SameSite=Lax'


def test_period_cookie_header_swaps_a_swapped_pair():
    assert period.period_cookie_header('2026-03-31', '2026-03-01').startswith(
        'period=2026-03-01..2026-03-31;')


@pytest.mark.parametrize('pair', [
    pytest.param(('2026-01-01\r\nSet-Cookie: x=1', '2026-12-31'), id='CRLF'),
    pytest.param(('2026-01-01', '2026-12-31; Domain=evil.example'), id='Attribut'),
    pytest.param(('x', 'y'), id='Buchstaben'),
    pytest.param((None, None), id='None'),
    pytest.param(('', ''), id='leer'),
    pytest.param(('2026-01-01', '2026-02-30'), id='Datum-gibt-es-nicht'),
    pytest.param((SCRIPT, '2026-12-31'), id='Skript'),
])
def test_period_cookie_header_refuses_an_invalid_period(pair):
    with pytest.raises(ValueError):
        period.period_cookie_header(*pair)


def test_a_period_cookie_survives_the_round_trip():
    header = period.period_cookie_header('2026-03-01', '2026-03-31')
    assert period._read_cookie(header.split(';')[0]) == ('2026-03-01', '2026-03-31')


# ── _header_safe, send_header ────────────────────────────────────────────────
@pytest.mark.parametrize('value, expected', [
    pytest.param('a\r\nb', 'ab', id='CRLF'),
    pytest.param('a\rb', 'ab', id='CR'),
    pytest.param('a\nb', 'ab', id='LF'),
    pytest.param('a\0b', 'ab', id='NUL'),
    pytest.param('a\r\n\0b\r\n', 'ab', id='alles'),
    pytest.param('Größe ÿ', 'Größe ÿ', id='Latin-1-bleibt'),
    pytest.param('a€b', 'a?b', id='Euro'),
    pytest.param('Ā', '?', id='U+0100'),
    pytest.param('日本', '??', id='Kanji'),
    pytest.param('a😀b', 'a?b', id='ausserhalb-der-BMP'),
    pytest.param(123, '123', id='Zahl'),
    pytest.param(None, 'None', id='None'),
    pytest.param('', '', id='leer'),
])
def test_header_safe(value, expected):
    assert _header_safe(value) == expected


def _handler():
    """Ein SimpleWebServer ohne Verbindung: send_header schreibt nur in _headers_buffer."""
    handler = SimpleWebServer.__new__(SimpleWebServer)
    handler.request_version = 'HTTP/1.1'
    handler._headers_buffer = []
    return handler


@pytest.mark.parametrize('name, value, expected', [
    pytest.param('X-Test', 'a\r\nInjected: 1', b'X-Test: aInjected: 1\r\n', id='CRLF-im-Wert'),
    pytest.param('X-A\r\nEvil', 'v', b'X-AEvil: v\r\n', id='CRLF-im-Namen'),
    pytest.param('X-Test', 'a\0b', b'X-Test: ab\r\n', id='NUL'),
    pytest.param('X-Test', 'a€b', b'X-Test: a?b\r\n', id='ausserhalb-von-Latin-1'),
    pytest.param('X-Test', 'ä', 'X-Test: ä\r\n'.encode('latin-1'), id='Latin-1-bleibt'),
    pytest.param('Location', '/trips?from=2026-01-01\r\nSet-Cookie: x=1',
                 b'Location: /trips?from=2026-01-01Set-Cookie: x=1\r\n', id='Location'),
    pytest.param('Content-Length', 42, b'Content-Length: 42\r\n', id='Zahl'),
])
def test_send_header_filters_name_and_value(name, value, expected):
    handler = _handler()
    handler.send_header(name, value)
    assert handler._headers_buffer == [expected]
    assert b'\r\n' not in handler._headers_buffer[0][:-2]


# ── _int_param ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize('qs, expected', [
    pytest.param({}, None, id='fehlt'),
    pytest.param({'person': []}, None, id='leere-Liste'),
    pytest.param({'person': ['']}, None, id='leer'),
    pytest.param({'person': ['abc']}, None, id='Buchstaben'),
    pytest.param({'person': ['1.5']}, None, id='Kommazahl'),
    pytest.param({'person': ['1e3']}, None, id='Exponent'),
    pytest.param({'person': ['0x10']}, None, id='hexadezimal'),
    pytest.param({'person': ['-3']}, -3, id='negativ'),
    pytest.param({'person': [' 7 ']}, 7, id='Leerzeichen'),
    pytest.param({'person': ['12']}, 12, id='Zahl'),
    pytest.param({'person': ['0']}, 0, id='null'),
    pytest.param({'person': ['abc', '5']}, None, id='erster-Wert-zaehlt-ungueltig'),
    pytest.param({'person': ['5', 'abc']}, 5, id='erster-Wert-zaehlt-gueltig'),
    pytest.param({'id': ['5']}, None, id='anderer-Name'),
    pytest.param({'person': [SCRIPT]}, None, id='Skript'),
    pytest.param({'person': ['9' * 100]}, int('9' * 100), id='hundert-Ziffern'),
])
def test_int_param(qs, expected):
    assert _int_param(qs, 'person') == expected


def test_int_param_survives_a_number_longer_than_python_converts():
    limit = sys.get_int_max_str_digits()
    digits = '9' * ((limit or 100) + 1)
    result = _int_param({'person': [digits]}, 'person')
    assert result is None if limit else result == int(digits)


# ── period_filter_widget ─────────────────────────────────────────────────────
WIDGET = period.period_filter_widget


def test_widget_for_valid_dates_keeps_its_exact_markup():
    html = WIDGET('2026-06-01', '2026-06-30', '/transactions')
    assert '<input type="date" id="dateFrom" value="2026-06-01" onchange="gotoPeriod()">' in html
    assert '<input type="date" id="dateTo" value="2026-06-30" onchange="gotoPeriod()">' in html
    assert "if (f && t) window.location.href = '/transactions?from=' + f + '&to=' + t + '';" in html
    assert "onclick=\"window.location.href='/transactions?from=2026-01-01&to=2026-12-31'\">Jun</button>" in html


def test_widget_keeps_a_plain_base_path_unchanged():
    for base in ('/transactions', '/receipts', '/worktime', '/trips', '/invoice'):
        html = WIDGET('2026-01-01', '2026-12-31', base, extra_params={'person': 3})
        assert f"window.location.href = '{base}?from=' + f + '&to=' + t + '&person=3';" in html
        assert f"href='{base}?from=2026-02-01&to=2026-02-28&person=3'" in html


def test_widget_keeps_dates_inside_their_attributes():
    html = WIDGET(FORMS['script'], FORMS['js'], '/transactions')
    dom = Dom(html)
    assert dom.one('input', id='dateFrom')['value'] == FORMS['script']
    assert dom.one('input', id='dateTo')['value'] == FORMS['js']
    assert len(dom.scripts) == 1
    assert 'img' not in dom.names
    assert SCRIPT not in html and '"><script' not in html
    assert "2026-01-01'+alert(1)+'" not in html


def test_widget_with_hostile_dates_has_the_same_elements_as_with_valid_ones():
    assert_same_structure(WIDGET(FORMS['script'], FORMS['js'], '/transactions'),
                          WIDGET('2026-06-01', '2026-06-30', '/transactions'))


@pytest.mark.parametrize('base', ["/x'y", '/x"y', '/x<y>', "/x'\"<y>&z", '/x\\y', '/x</script>y'])
def test_widget_encodes_a_hostile_base_path(base):
    html = WIDGET('2026-06-01', '2026-06-30', base)
    encoded = urllib.parse.quote(base, safe='/')
    dom = Dom(html)
    assert len(dom.scripts) == 1
    assert f"window.location.href = '{encoded}?from=' + f + '&to=' + t + '';" in dom.scripts[0]
    clicks = [a['onclick'] for a in dom.all('button')]
    assert len(clicks) == 16
    for click in clicks:
        assert re.fullmatch(r"window\.location\.href='[^'\"<>\\ ]*'", click), click
        assert click.startswith(f"window.location.href='{encoded}?from=")


def test_widget_encodes_hostile_extra_parameters():
    key, value = 'st\'at"us', '</script>'
    html = WIDGET('2026-06-01', '2026-06-30', '/invoice',
                  extra_params={'search': "a'b\"c<d>&e", key: value})
    dom = Dom(html)
    assert len(dom.scripts) == 1
    extra = f"&search={urllib.parse.quote(chr(97) + chr(39) + 'b' + chr(34) + 'c<d>&e')}"
    assert extra == '&search=a%27b%22c%3Cd%3E%26e'
    assert extra in dom.scripts[0]
    assert f'&{urllib.parse.quote(key)}={urllib.parse.quote(value)}' in dom.scripts[0]
    assert '</script>' not in dom.scripts[0]


# ── Seiten direkt aufgerufen ─────────────────────────────────────────────────
HOSTILE_FROM = '"><img src=x onerror=alert(1)>'
HOSTILE_TO = "~'\"><script>alert(2)</script>"          # „~“ sortiert hinter jedem Datum


def _driver(db, name='Erika Probe'):
    con = sqlite3.connect(db.db_name)
    try:
        cid = con.execute("INSERT INTO Contacts (ContactType, EntityType, DisplayName) "
                          "VALUES ('own','person',?)", (name,)).lastrowid
        con.commit()
    finally:
        con.close()
    return cid


def test_trips_page_with_hostile_dates_has_the_same_elements_as_with_valid_dates(tmp_db):
    """Der Wertebereich ist so gewählt, dass die Fahrt trotzdem in der Auswahl landet – die
    Löschadresse der Zeile enthält den Zeitraum und wird mitgeprüft."""
    driver = _driver(tmp_db)
    trip = tmp_db.insert_trip(driver, '2026-06-03', destination='Hamburg', distance_km=290)
    hostile = PageTrips(tmp_db, person_id=driver, date_from=HOSTILE_FROM, date_to=HOSTILE_TO)
    benign = PageTrips(tmp_db, person_id=driver, date_from='2026-06-01', date_to='2026-06-30')
    assert_same_structure(hostile, benign)
    assert '<img src=x onerror=alert(1)>' not in hostile
    assert '<script>alert(2)</script>' not in hostile
    dom = Dom(hostile)
    assert dom.one('input', type='hidden', name='from')['value'] == HOSTILE_FROM
    assert dom.one('input', type='hidden', name='to')['value'] == HOSTILE_TO
    quoted_from = urllib.parse.quote(HOSTILE_FROM, safe='')
    quoted_to = urllib.parse.quote(HOSTILE_TO, safe='')
    delete = [a['onclick'] for t, a in dom.tags if t == 'a' and 'delete-icon' in a.get('class', '')]
    assert delete == [f"appConfirmPost('/trips/delete?person={driver}&from={quoted_from}"
                      f"&to={quoted_to}&id={trip}', 'Fahrt wirklich löschen?')"]
    select = dom.all('select')[0]['onchange']
    assert f"'&from={quoted_from}&to={quoted_to}'" in select


def test_trips_page_keeps_valid_dates_in_their_old_form(tmp_db):
    driver = _driver(tmp_db)
    trip = tmp_db.insert_trip(driver, '2026-06-03', destination='Hamburg', distance_km=290)
    html = PageTrips(tmp_db, person_id=driver, date_from='2026-06-01', date_to='2026-06-30')
    assert '<input type="hidden" name="from" value="2026-06-01">' in html
    assert '<input type="hidden" name="to" value="2026-06-30">' in html
    assert (f"appConfirmPost('/trips/delete?person={driver}&from=2026-06-01&to=2026-06-30"
            f"&id={trip}', 'Fahrt wirklich löschen?')") in html
    assert "+ '&from=2026-06-01&to=2026-06-30'" in html


@pytest.mark.parametrize('date_from, date_to', [
    (HOSTILE_FROM, HOSTILE_TO), ('2026-13-45', '2026-99-99'), ('x', 'y'), ('<script>', '</script>'),
    ('2026-01-01"><svg onload=alert(1)>', '2026-12-31'),
])
def test_worktime_page_never_renders_an_unchecked_period(tmp_db, date_from, date_to):
    """Die Seite parst den Zeitraum zuerst als Datum: Ein feindlicher Wert bricht dort ab, bevor
    etwas ausgegeben wird – entscheidend ist, dass er nie roh in der Ausgabe steht."""
    driver = _driver(tmp_db)
    tmp_db.insert_worktime(driver, '2026-06-03', start_time='08:00', end_time='16:00', note='Tag')
    try:
        html = PageWorkTimes(tmp_db, person_id=driver, date_from=date_from, date_to=date_to)
    except ValueError:
        return
    for raw in (date_from, date_to):
        if raw:
            assert raw not in html or raw in ('x', 'y')


class Sneaky(str):
    """Eine Zeichenkette, die als Datum besteht (``date.fromisoformat``, ``quote`` und
    ``html.escape`` sehen ihren Inhalt), sich aber als etwas anderes ausgibt, sobald man sie
    einsetzt (f-String, ``str()``). So zeigt sich jede Stelle einer Seite, an der der Zeitraum
    ohne Kodierung oder Maskierung eingesetzt wird – auch dort, wo ein feindlicher Wert
    schon vor der Seite am Datumstest scheitern würde (Arbeitszeiten)."""

    def __new__(cls, content, shown):
        value = super().__new__(cls, content)
        value.shown = shown
        return value

    def __str__(self):
        return self.shown

    def __format__(self, spec):
        return self.shown


@pytest.mark.parametrize('page', [PageWorkTimes, PageTrips], ids=['arbeitszeiten', 'fahrten'])
def test_period_is_encoded_wherever_a_page_puts_it_into_an_address_or_attribute(tmp_db, page):
    driver = _driver(tmp_db)
    tmp_db.insert_worktime(driver, '2026-06-03', start_time='08:00', end_time='16:00', note='Tag')
    tmp_db.insert_trip(driver, '2026-06-03', destination='Hamburg', distance_km=290)
    benign = page(tmp_db, person_id=driver, date_from='2026-06-01', date_to='2026-06-30')
    sneaky = page(tmp_db, person_id=driver, date_from=Sneaky('2026-06-01', HOSTILE_FROM),
                  date_to=Sneaky('2026-06-30', HOSTILE_TO))
    assert '<img src=x onerror=alert(1)>' not in sneaky
    assert '<script>alert(2)</script>' not in sneaky
    assert_same_structure(sneaky, benign)
    a, b = Dom(sneaky), Dom(benign)
    for kind in ('a', 'select'):                         # Löschlinks, Personenauswahl
        assert [t.get('onclick') or t.get('onchange') for t in a.all(kind)] == \
            [t.get('onclick') or t.get('onchange') for t in b.all(kind)], kind
    assert [i.get('value') for i in a.all('input') if i.get('type') == 'hidden'] == \
        [i.get('value') for i in b.all('input') if i.get('type') == 'hidden']
    if page is PageWorkTimes:
        assert data_block(a, 'WT_DATA')['pdfBase'] == data_block(b, 'WT_DATA')['pdfBase']


def test_worktime_page_keeps_valid_dates_in_their_old_form(tmp_db):
    driver = _driver(tmp_db)
    entry = tmp_db.insert_worktime(driver, '2026-06-03', start_time='08:00', end_time='16:00', note='Tag')
    html = PageWorkTimes(tmp_db, person_id=driver, date_from='2026-06-01', date_to='2026-06-30')
    assert '<input type="hidden" name="from" value="2026-06-01">' in html
    assert '<input type="hidden" name="to" value="2026-06-30">' in html
    assert (f"appConfirmPost('/worktime/delete?person={driver}&from=2026-06-01&to=2026-06-30"
            f"&id={entry}', 'Eintrag wirklich löschen?')") in html
    data = data_block(Dom(html), 'WT_DATA')
    assert data['pdfBase'] == f'/worktime/pdf?person={driver}&from=2026-06-01&to=2026-06-30'


def test_dashboard_page_with_hostile_dates_has_the_same_elements_as_with_valid_dates(tmp_db):
    """Das Dashboard liest nur die ersten Stellen als Jahr und Monat – Angehängtes kommt durch
    die Datenbankschicht bis in die Seite."""
    hostile_from = '2026-01-01"><img src=x onerror=alert(1)>'
    hostile_to = "2026-12-31'\"><script>alert(2)</script>"
    hostile = PageDashboard(tmp_db, hostile_from, hostile_to)
    benign = PageDashboard(tmp_db, '2026-01-01', '2026-12-31')
    assert_same_structure(hostile, benign)
    assert '<img src=x onerror=alert(1)>' not in hostile
    assert '<script>alert(2)</script>' not in hostile
    dom = Dom(hostile)
    assert dom.one('input', id='dateFrom')['value'] == hostile_from
    assert dom.one('input', id='dateTo')['value'] == hostile_to


def test_dashboard_range_label_is_escaped_where_it_is_shown(tmp_db):
    hostile_from = '2026-01-01"><img src=x onerror=alert(1)>'
    html = PageDashboard(tmp_db, hostile_from, '2026-12-31')
    assert '"><img' not in html
    assert 'img' not in Dom(html).names
    assert '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;' in html


def test_dashboard_keeps_valid_dates_in_their_old_form(tmp_db):
    html = PageDashboard(tmp_db, '2026-01-01', '2026-12-31')
    assert 'value="2026-01-01"' in html and 'value="2026-12-31"' in html
    assert 'Betriebsausgaben (2026)' in html
    html = PageDashboard(tmp_db, '2026-03-01', '2026-03-31')
    assert 'Betriebsausgaben (2026-03-01 &ndash; 2026-03-31)' in html


# ── Über den echten Server ───────────────────────────────────────────────────
PAGES = ['/trips', '/worktime', '/dashboard', '/transactions', '/receipts']


@pytest.fixture
def zeiten(auto_login):
    """Eigener Kontakt (Person) mit einem Arbeitszeit- und einem Fahrteintrag."""
    db = auto_login.db
    db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                      first_name='Erika', last_name='Probe')
    person = db.fetch_contacts(contact_type='own')[0][0]
    worktime = db.insert_worktime(person, '2026-03-10', start_time='08:00', end_time='16:00',
                                  pause_minutes=30, note='Tag eins')
    trip = db.insert_trip(person, '2026-03-10', destination='Hamburg', distance_km=290)
    return SimpleNamespace(db=db, person=person, worktime=worktime, trip=trip, csrf=auto_login.csrf)


def set_cookies(resp):
    return resp.headers.get_all('Set-Cookie') or []


def period_cookies(resp):
    return [c for c in set_cookies(resp) if c.startswith('period=')]


def encode(**params):
    return urllib.parse.urlencode(params)


HOSTILE_QUERIES = {
    'skript-in-from': encode(**{'from': FORMS['script'], 'to': '2026-12-31'}),
    'skript-in-to': encode(**{'from': '2026-01-01', 'to': FORMS['script']}),
    'js-in-from': encode(**{'from': FORMS['js'], 'to': '2026-12-31'}),
    'beide-feindlich': encode(**{'from': FORMS['script'], 'to': "2026-12-31'+alert(2)+'"}),
    'date_from-schreibweise': encode(date_from=FORMS['script'], date_to='2026-12-31'),
    'woertlich-skript': 'from="><script>alert(1)</script>&to=2026-12-31',
    'woertlich-js': "from=2026-01-01'%2Balert(1)%2B'&to=2026-12-31",
    'vollbreite-ziffern': encode(**{'from': '２０２６-０１-０１', 'to': '２０２６-１２-３１'}),
    'zeilenumbruch-am-ende': encode(**{'from': '2026-01-01\n', 'to': '2026-12-31'}),
    'nur-von-feindlich': encode(**{'from': FORMS['script']}),
    'datum-gibt-es-nicht': encode(**{'from': '2026-02-30', 'to': '2026-13-01'}),
    'lang': encode(**{'from': 'x' * 5000, 'to': 'y' * 5000}),
}

HOSTILE_COOKIES = ['period=<script>..x', 'period=abc..def', 'period=2026-13-45..2026-99-99',
                   'period=<script>alert(1)</script>..<script>alert(2)</script>',
                   'period=2026-03-01..', 'period=..', 'period=', 'period=2026-03-01',
                   'period=2026-03-01..2026-03-31..x', "period=2026-01-01'+alert(1)+'..2026-12-31",
                   'period=" onfocus=alert(1) x="..".."']


def assert_no_payload(text):
    """Keine der Seiten enthält sonst „alert(“ – jede Spur der Nutzlast fiele hier auf."""
    assert 'alert(' not in text and 'alert%28' not in text
    assert '<script>alert' not in text


@pytest.mark.parametrize('case', list(HOSTILE_QUERIES))
def test_hostile_period_in_the_address_is_dropped(server_port, zeiten, case):
    for page in PAGES:
        plain = get(server_port, page)
        resp = get(server_port, f'{page}?{HOSTILE_QUERIES[case]}')
        assert resp.status == 200, page
        assert period_cookies(resp) == [], f'{page}: ein ungültiger Zeitraum darf keinen Cookie setzen'
        assert resp.body == plain.body, f'{page}: die Seite zeigt etwas anderes als ohne Angabe'
        assert_no_payload(resp.text)


@pytest.mark.parametrize('cookie', HOSTILE_COOKIES)
def test_hostile_period_cookie_is_ignored(server_port, zeiten, cookie):
    """Früher 500 auf /dashboard und /worktime: Der Cookie wurde ungeprüft als Datum gelesen."""
    for page in PAGES:
        plain = get(server_port, page)
        resp = get(server_port, page, headers={'Cookie': cookie})
        assert resp.status == 200, page
        assert period_cookies(resp) == [], page
        assert resp.body == plain.body, f'{page}: die Seite zeigt nicht den Standardzeitraum'
        assert_no_payload(resp.text)


SET_COOKIE = 'period=2026-03-01..2026-03-31; Path=/; Max-Age=31536000; SameSite=Lax'


def shown_period(resp):
    dom = Dom(resp.text)
    return dom.one('input', id='dateFrom')['value'], dom.one('input', id='dateTo')['value']


def test_a_valid_period_sets_the_cookie_and_is_shown(server_port, zeiten):
    for page in PAGES:
        resp = get(server_port, f'{page}?from=2026-03-01&to=2026-03-31')
        assert resp.status == 200, page
        assert period_cookies(resp) == [SET_COOKIE], page
        assert shown_period(resp) == ('2026-03-01', '2026-03-31'), page


def test_a_swapped_period_is_swapped_back(server_port, zeiten):
    for page in PAGES:
        resp = get(server_port, f'{page}?from=2026-03-31&to=2026-03-01')
        assert resp.status == 200, page
        assert period_cookies(resp) == [SET_COOKIE], page
        assert shown_period(resp) == ('2026-03-01', '2026-03-31'), page


def test_a_valid_cookie_is_used_without_setting_it_again(server_port, zeiten):
    for page in PAGES:
        resp = get(server_port, page, headers={'Cookie': 'period=2025-05-01..2025-05-31'})
        assert resp.status == 200 and period_cookies(resp) == [], page
        assert shown_period(resp) == ('2025-05-01', '2025-05-31'), page


def test_an_invalid_query_falls_back_to_a_valid_cookie_and_sets_none(server_port, zeiten):
    for page in PAGES:
        resp = get(server_port, f'{page}?{HOSTILE_QUERIES["skript-in-from"]}',
                   headers={'Cookie': 'period=2025-05-01..2025-05-31'})
        assert resp.status == 200 and period_cookies(resp) == [], page
        assert shown_period(resp) == ('2025-05-01', '2025-05-31'), page


def test_a_valid_query_beats_a_hostile_cookie(server_port, zeiten):
    for page in PAGES:
        resp = get(server_port, f'{page}?from=2026-03-01&to=2026-03-31',
                   headers={'Cookie': 'period=<script>..x'})
        assert resp.status == 200, page
        assert period_cookies(resp) == [SET_COOKIE], page
        assert shown_period(resp) == ('2026-03-01', '2026-03-31'), page


def test_the_default_period_is_the_current_year(server_port, zeiten):
    year = datetime.now().year
    for page in PAGES:
        dom = Dom(get(server_port, page).text)
        assert dom.one('input', id='dateFrom')['value'] == f'{year}-01-01', page
        assert dom.one('input', id='dateTo')['value'] == f'{year}-12-31', page


# -- person=abc -----------------------------------------------------------------
BAD_PERSONS = ['abc', '', '1.5', '-3', '99999999999999999999999999',
               urllib.parse.quote(SCRIPT, safe='')]


@pytest.mark.parametrize('person', BAD_PERSONS)
@pytest.mark.parametrize('page', ['/worktime', '/trips'])
def test_an_invalid_person_shows_the_first_person_instead_of_a_500(server_port, zeiten, page, person):
    resp = get(server_port, f'{page}?person={person}&from=2026-03-01&to=2026-03-31')
    assert resp.status == 200
    assert f'<option value="{zeiten.person}" selected>' in resp.text
    assert SCRIPT not in resp.text


@pytest.mark.parametrize('person', ['abc', ''])
def test_an_invalid_person_on_the_edit_pages_preloads_the_first_persons_entry(server_port, zeiten, person):
    worktime = get(server_port, f'/worktime/edit?id={zeiten.worktime}&person={person}')
    assert worktime.status == 200 and 'wtLoadEntry(' in worktime.text
    trips = get(server_port, f'/trips/edit?id={zeiten.trip}&person={person}')
    assert trips.status == 200 and 'trLoadEntry(' in trips.text


@pytest.mark.parametrize('query', ['person=abc', 'person=', 'person=1.5',
                                   'from=2026-03-01&to=2026-03-31', 'from=2026-03-01&to=2026-03-31&person=abc',
                                   encode(person=SCRIPT)])
def test_the_worktime_pdf_refuses_a_missing_or_invalid_person_with_plain_text(server_port, zeiten, query):
    resp = get(server_port, f'/worktime/pdf?{query}')
    assert resp.status == 400
    assert resp.headers['Content-Type'].startswith('text/plain')
    assert 'Person' in resp.text and '<' not in resp.text


# -- Header-Injektion in der Rohantwort --------------------------------------------
class Raw:
    def __init__(self, data):
        self.head, _, self.body = data.partition(b'\r\n\r\n')
        self.lines = self.head.split(b'\r\n')
        self.status = int(self.lines[0].split()[1])
        self.headers = [tuple(line.split(b': ', 1)) if b': ' in line else (line, b'')
                        for line in self.lines[1:]]

    def header(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower().encode()]


def raw_request(port, method, target, form=None, extra=()):
    body = urllib.parse.urlencode(form).encode('utf-8') if form is not None else b''
    lines = [f'{method} {target} HTTP/1.1', f'Host: 127.0.0.1:{port}', 'Connection: close', *extra]
    if form is not None:
        lines += ['Content-Type: application/x-www-form-urlencoded', f'Content-Length: {len(body)}']
    data = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + body
    with socket.create_connection(('127.0.0.1', port), timeout=TIMEOUT) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return Raw(b''.join(chunks))


INJECTED = 'X-Injected'


def assert_no_injected_header(raw):
    assert INJECTED.encode() not in raw.head, raw.head
    assert not [k for k, _ in raw.headers if k.lower().startswith(b'x-inj')]
    assert raw.head.count(b'HTTP/1.') == 1, 'mehr als eine Statuszeile'


CRLF = '%0d%0a' + INJECTED + ':%201'


CRLF_QUERIES = {
    'from': f'from=2026-01-01{CRLF}&to=2026-12-31',
    'to': f'from=2026-01-01&to=2026-12-31{CRLF}',
    'date_from': f'date_from=2026-01-01{CRLF}&date_to=2026-12-31',
    'date_to': f'date_from=2026-01-01&date_to=2026-12-31{CRLF}',
    'beide': f'from=2026-01-01{CRLF}&to=2026-12-31{CRLF}',
}


@pytest.mark.parametrize('name', list(CRLF_QUERIES))
def test_crlf_in_a_query_parameter_injects_no_header(server_port, zeiten, name):
    for page in PAGES:
        raw = raw_request(server_port, 'GET', f'{page}?{CRLF_QUERIES[name]}')
        assert raw.status == 200, page
        assert_no_injected_header(raw)
        assert raw.header('Set-Cookie') == [], page


@pytest.mark.parametrize('page', ['/trips', '/worktime'])
def test_crlf_in_the_person_injects_no_header(server_port, zeiten, page):
    raw = raw_request(server_port, 'GET', f'{page}?person=1{CRLF}')
    assert raw.status == 200
    assert_no_injected_header(raw)


@pytest.mark.parametrize('name', ['from', 'to'])
@pytest.mark.parametrize('route', ['/trips/delete', '/worktime/delete'])
def test_crlf_in_the_delete_redirects_injects_no_header(server_port, zeiten, route, name):
    other = 'to' if name == 'from' else 'from'
    entry = zeiten.trip if route.startswith('/trips') else zeiten.worktime
    raw = raw_request(server_port, 'POST',
                      f'{route}?id={entry}&person={zeiten.person}&{name}=2026-03-01{CRLF}&{other}=2026-03-31',
                      form={'csrf': zeiten.csrf})
    assert raw.status == 303
    assert_no_injected_header(raw)
    assert INJECTED.encode() not in b''.join(raw.header('Location'))


# -- Weiterleitungen von /trips/add, /worktime/add … ---------------------------------
def trip_form(zeiten, **period_fields):
    return {'csrf': zeiten.csrf, 'person_id': str(zeiten.person), 'start_date': '2026-03-11',
            'destination': 'Kiel', **period_fields}


def worktime_form(zeiten, day='2026-03-12', **period_fields):
    return {'csrf': zeiten.csrf, 'person_id': str(zeiten.person), 'date': day,
            'start_time': '08:00', 'end_time': '12:00', 'kind': 'work', 'location_mode': 'other',
            'location_city': 'Kiel', **period_fields}


ROUTES = {
    '/trips/add': ('/trips', trip_form),
    '/worktime/add': ('/worktime', worktime_form),
}

INVALID_PERIODS = {
    'zeilenumbruch': ('2026-03-01\r\n' + INJECTED + ': 1', '2026-03-31\r\n' + INJECTED + ': 2'),
    'skript': (FORMS['script'], FORMS['script']),
    'js': (FORMS['js'], "2026-03-31'+alert(2)+'"),
    'euro': ('€uro', '日本'),
    'ausserhalb-der-BMP': ('😀', '😀'),
    'leer': ('', ''),
    'datum-gibt-es-nicht': ('2026-02-30', '2026-13-01'),
    'vollbreite': ('２０２６-０３-０１', '２０２６-０３-３１'),
    'parameter-eingeschleust': ('2026-03-01&person=9', '2026-03-31#x'),
}


def _location(raw_or_resp):
    if isinstance(raw_or_resp, Raw):
        return raw_or_resp.header('Location')[0].decode('latin-1')
    return raw_or_resp.headers['Location']


@pytest.mark.parametrize('case', list(INVALID_PERIODS))
def test_redirect_after_saving_drops_an_invalid_period(server_port, zeiten, case):
    date_from, date_to = INVALID_PERIODS[case]
    for route, (page, form) in ROUTES.items():
        raw = raw_request(server_port, 'POST', route,
                          form=form(zeiten, **{'from': date_from, 'to': date_to}))
        assert raw.status == 303, raw.head
        assert_no_injected_header(raw)
        location = _location(raw)
        parts = urllib.parse.urlsplit(location)
        query = urllib.parse.parse_qs(parts.query)
        assert parts.path == page, route
        assert query.get('person') == [str(zeiten.person)], location
        assert 'from' not in query and 'to' not in query, location
        for bad in ('<', '>', '"', "'", 'alert', INJECTED, '€', '日本', '#', ' '):
            assert bad not in location, (bad, location)


@pytest.mark.parametrize('route', list(ROUTES))
def test_redirect_after_saving_keeps_a_valid_period(server_port, zeiten, route):
    page, form = ROUTES[route]
    raw = raw_request(server_port, 'POST', route, form=form(zeiten, **{'from': '2026-03-01', 'to': '2026-03-31'}))
    assert raw.status == 303
    parts = urllib.parse.urlsplit(_location(raw))
    assert parts.path == page
    assert urllib.parse.parse_qs(parts.query) == {'person': [str(zeiten.person)],
                                                  'from': ['2026-03-01'], 'to': ['2026-03-31']}


@pytest.mark.parametrize('route', list(ROUTES))
def test_redirect_after_saving_without_a_period_has_none(server_port, zeiten, route):
    page, form = ROUTES[route]
    resp = post(server_port, route, form=form(zeiten))
    assert resp.status == 303
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(resp.headers['Location']).query)
    assert 'from' not in query and 'to' not in query


def test_a_save_with_a_character_outside_latin_1_is_no_500(server_port, zeiten):
    """Früher: UnicodeEncodeError beim Schreiben des Location-Headers → 500 mit doppelter Statuszeile."""
    for route, (page, form) in ROUTES.items():
        raw = raw_request(server_port, 'POST', route, form=form(zeiten, **{'from': '€', 'to': 'Łódź'}))
        assert raw.status == 303, raw.head
        assert raw.head.count(b'HTTP/1.') == 1
    assert len(zeiten.db.fetch_trips(zeiten.person)) == 2
    assert len(zeiten.db.fetch_worktimes(zeiten.person)) == 2


@pytest.mark.parametrize('route', ['/trips/update', '/worktime/update'])
def test_redirect_after_updating_drops_an_invalid_period(server_port, zeiten, route):
    if route.startswith('/trips'):
        form = {'csrf': zeiten.csrf, 'person_id': str(zeiten.person), 'id': str(zeiten.trip),
                'start_date': '2026-03-10', 'destination': 'Lübeck'}
    else:
        form = worktime_form(zeiten, day='2026-03-10')
        form['id'] = str(zeiten.worktime)
    bad = raw_request(server_port, 'POST', route,
                      form={**form, 'from': FORMS['script'], 'to': INVALID_PERIODS['zeilenumbruch'][1]})
    assert bad.status == 303
    assert_no_injected_header(bad)
    assert '<' not in _location(bad)
    good = post(server_port, route, form={**form, 'from': '2026-03-01', 'to': '2026-03-31'})
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(good.headers['Location']).query)
    assert query['from'] == ['2026-03-01'] and query['to'] == ['2026-03-31']


def test_the_overlap_redirect_of_a_worktime_drops_an_invalid_period_and_keeps_the_error(server_port, zeiten):
    form = worktime_form(zeiten, day='2026-03-10', **{'from': FORMS['script'], 'to': '2026-03-31'})
    raw = raw_request(server_port, 'POST', '/worktime/add', form=form)     # kollidiert mit dem Eintrag
    assert raw.status == 303
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(_location(raw)).query)
    assert 'Überschneidung' in query['error'][0]
    assert '<' not in _location(raw) and 'alert' not in _location(raw)


# ── DATEV-Export mit ungültigen Daten ────────────────────────────────────────
@pytest.fixture
def booked_plain(tmp_db):
    """Zwei Buchungen im März 2026 ohne Kontenrahmen – genug für die Fälle, in denen ohnehin
    nichts exportiert werden darf (das Laden der Testdaten dauert über eine Sekunde)."""
    for doc, amount in (('D1', -119.0), ('D2', -50.0)):
        tmp_db.insert_booking(date_booking='2026-03-10', amount=amount, tax_rate=0.19,
                              document_number=doc, booking_type='entry')
    return tmp_db


@pytest.fixture
def booked(db_with_coa):
    """Datenbank mit zwei Buchungen D1 und D2 im März 2026 (DateTax noch leer) neben den Testdaten."""
    db = db_with_coa
    coa = {r[2]: r[0] for r in db.fetch_chart_of_accounts()}
    for doc, amount in (('D1', -119.0), ('D2', -50.0)):
        db.insert_booking(date_booking='2026-03-10', amount=amount, coa_id=coa.get(6815),
                          counter_coa_id=coa.get(1460), tax_rate=0.19, document_number=doc,
                          booking_type='entry')
    return db


def date_taxes(db, documents=None):
    """(ID, DateTax) aller Buchungen – oder nur der mit diesen Belegnummern."""
    con = sqlite3.connect(db.db_name)
    try:
        if documents is None:
            return con.execute('SELECT ID, DateTax FROM Bookings ORDER BY ID').fetchall()
        marks = ','.join('?' * len(documents))
        return con.execute(f'SELECT ID, DateTax FROM Bookings WHERE DocumentNumber IN ({marks}) '
                           'ORDER BY ID', list(documents)).fetchall()
    finally:
        con.close()


def datev_post(date_from, date_to):
    return {'date_from': [date_from], 'date_to': [date_to]}


INVALID_EXPORTS = {
    'unsinn': ('x', '2026-12-31'),
    'bis-unsinn': ('2026-01-01', 'x'),
    'skript': (FORMS['script'], '2026-12-31'),
    'js': ('2026-01-01', "2026-12-31'+alert(1)+'"),
    'zeilenumbruch-eingeschleust': ('2026-01-01\r\nSet-Cookie: a=b', '2026-12-31'),
    'zeilenumbruch-in-bis': ('2026-01-01', '2026-12-31\r\nX-Injected: 1'),
    'vertauscht': ('2026-12-31', '2026-01-01'),
    'datum-gibt-es-nicht': ('2026-02-30', '2026-03-31'),
    'jahr-ausserhalb': ('1899-01-01', '2026-12-31'),
    'vollbreite': ('２０２６-０１-０１', '2026-12-31'),
    'leer-von': ('', '2026-12-31'),
    'leer-bis': ('2026-01-01', ''),
    'lang': ('x' * 5000, 'y' * 5000),
}


# Welche Meldung zu welchem Fall gehört – ein bloßes „Fehler“ ließe offen, ob die Prüfung oder erst
# das Fehlen von Buchungen im (leeren) Zeitraum angeschlagen hat
DATEV_MESSAGE = {case: 'Ungültiger Datumsbereich' for case in INVALID_EXPORTS}
DATEV_MESSAGE.update({'vertauscht': 'liegt nach', 'leer-von': 'Datumsbereich fehlt',
                      'leer-bis': 'Datumsbereich fehlt'})


@pytest.mark.parametrize('case', list(INVALID_EXPORTS))
def test_datev_export_rejects_an_invalid_period_and_exports_nothing(booked_plain, monkeypatch, case):
    from export import datev
    called = []
    monkeypatch.setattr(datev, 'export_to_datev', lambda *a, **k: called.append(a))
    before = date_taxes(booked_plain)
    assert len(before) == 2
    result = handlers.handle_datev_export(booked_plain, datev_post(*INVALID_EXPORTS[case]))
    assert result[0] == 303 and len(result) == 2
    location = result[1]
    assert location.startswith('/miscellaneous?datev_export=error'), location
    for bad in ('<', '>', '"', "'", '\r', '\n', 'alert', 'Set-Cookie'):
        assert bad not in location, (bad, location)
    message = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)['msg'][0]
    assert DATEV_MESSAGE[case] in message, message
    assert called == [], 'trotz ungültigem Zeitraum wurde exportiert'
    assert date_taxes(booked_plain) == before, 'DateTax wurde verändert'


@pytest.mark.parametrize('post_data', [{}, {'date_from': ['2026-01-01']}, {'date_to': ['2026-12-31']},
                                       {'date_from': [], 'date_to': []}])
def test_datev_export_without_a_period_is_an_error(booked_plain, post_data):
    before = date_taxes(booked_plain)
    result = handlers.handle_datev_export(booked_plain, post_data)
    assert result[0] == 303 and result[1].startswith('/miscellaneous?datev_export=error')
    assert date_taxes(booked_plain) == before


def test_datev_export_of_a_valid_period_names_the_file_and_sets_the_tax_date(booked):
    csv_bytes, filename = handlers.handle_datev_export(booked, datev_post('2026-01-01', '2026-12-31'))
    assert filename == 'DATEV_Buchungsstapel_2026_20260101-20261231.csv'
    assert csv_bytes.startswith(b'"EXTF"')
    assert date_taxes(booked, ['D1', 'D2']) and \
        {tax for _, tax in date_taxes(booked, ['D1', 'D2'])} == {datetime.now().date().isoformat()}


@pytest.mark.parametrize('date_from, date_to, name', [
    ('2026-03-01', '2026-03-31', 'DATEV_Buchungsstapel_2026_20260301-20260331.csv'),
    ('2026-03-10', '2026-03-10', 'DATEV_Buchungsstapel_2026_20260310-20260310.csv'),
    ('2025-12-01', '2026-03-31', 'DATEV_Buchungsstapel_2025_20251201-20260331.csv'),
])
def test_datev_export_file_name_follows_the_validated_dates(booked, date_from, date_to, name):
    csv_bytes, filename = handlers.handle_datev_export(booked, datev_post(date_from, date_to))
    assert filename == name and isinstance(csv_bytes, bytes)


def test_datev_http_rejects_an_invalid_period_without_a_header_injection(server_port, auto_login):
    auto_login.db.load_test_seed_data()
    before = date_taxes(auto_login.db)
    raw = raw_request(server_port, 'POST', '/datev/export', form={
        'csrf': auto_login.csrf, 'date_from': '2026-01-01\r\n' + INJECTED + ': 1',
        'date_to': '2026-12-31'})
    assert raw.status == 303
    assert_no_injected_header(raw)
    assert _location(raw).startswith('/miscellaneous?datev_export=error')
    assert date_taxes(auto_login.db) == before


def test_datev_http_exports_a_valid_period(server_port, auto_login):
    db = auto_login.db
    db.load_test_seed_data()
    coa = {r[2]: r[0] for r in db.fetch_chart_of_accounts()}
    db.insert_booking(date_booking='2026-03-10', amount=-119.0, coa_id=coa.get(6815),
                      counter_coa_id=coa.get(1460), tax_rate=0.19, document_number='D1',
                      booking_type='entry')
    resp = post(server_port, '/datev/export', csrf=auto_login.csrf,
                form={'date_from': '2026-01-01', 'date_to': '2026-12-31'})
    assert resp.status == 200
    assert resp.headers['Content-Disposition'] == \
        'attachment; filename="DATEV_Buchungsstapel_2026_20260101-20261231.csv"'
    assert resp.body.decode('cp1252').startswith('"EXTF"')
