# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die dritte Menüzeile (Zeitraum) steht auf jeder Seite mit Zeitraum in derselben Höhe, weil sie überall aus EINER Hilfsfunktion kommt
(``period.period_filter_widget``) – nie als eigenes Formular einer Seite: Auswahlfelder und farbige Knöpfe sind höher, die Querlinie unter den
Menüzeilen spränge (gemessen: 113 statt 109 px auf BWA, EÜR, Summen- und Saldenliste und Liquidität, solange sie ein eigenes Formular hatten).

* Je Seite mit Zeitraum (Dashboard, BWA, EÜR, Summen- und Saldenliste, Liquidität, Rechnung, Belege, Buchungen, Fahrten, Zeiten): Der Inhalt von
  ``.header3`` ist genau das, was ``period_filter_widget`` für diese Seite liefert – ohne Formular, Auswahlfeld, farbigen Knopf und Inline-Style –
  und hat fünf Jahresknöpfe; die vier Auswertungen und das Dashboard haben dieselbe Struktur der Zeile (das Dashboard ohne Monatsknöpfe).
* Das Dashboard: kein ``setDashYear``/``applyDashFilter`` mehr, die Jahresknöpfe in einem eigenen Block, die gewählten Konten in den Adressen.
* Die Darstellung (BWA, Liquidität) bleibt über den Zeitraumwechsel erhalten (``variant`` in den Adressen der Knöpfe), der Zeitraum über den Wechsel
  der Darstellung (versteckte Felder).
* Über den echten Server: Die Auswertungen nehmen den gemeinsamen Zeitraum (Adresse > Cookie > laufendes Jahr, das Cookie wie überall) und zeigen
  daraus ihren Monat (``period_month``); die alte Adresse mit ``year`` und ``month`` zählt nicht; die PDF-Routen lesen ihre Adresse streng wie
  bisher. Ein Gang durch die Seiten wie im Browser (Monat wählen, Seite wechseln, ganzes Vorjahr, Darstellung, PDF) – das Cookie des Zeitraums reicht
  der Test weiter wie ein Browser.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; ein „heute“ steht nie als Konstante da: Die Seiten, die kein ``today`` annehmen, lesen die
Uhr – der Test liest sie dann frisch.
"""
import datetime
import json
import re

import pytest

from db.bwa import MONTH_NAMES
from server.pages_bwa import BwaQuery, PageBwa
from server.pages_dashboard import PageDashboard
from server.pages_euer import EuerQuery, PageEuer
from server.pages_invoice import PageInvoice
from server.pages_liquidity import PageLiquidity
from server.pages_receipts import PageReceipts
from server.pages_susa import PageSusa, SusaQuery
from server.pages_transactions import PageTransactions
from server.pages_trips import PageTrips
from server.pages_worktime import PageWorkTimes, _persons
from server.period import MONTHS_ALWAYS, MONTHS_NONE, period_filter_widget as widget
from tests.dom_helpers import Dom
from tests.http_client import get
from tests.test_period_filter import MONTH_LABELS, _active_months, _active_years, _buttons, _months, _years

FIXED = datetime.date(2026, 11, 21)             # für die Seiten, die ein „heute“ annehmen (die Auswertungen)
H3_OPEN = "<div class='header3'>"
PAGE_NAMES = ['dashboard', 'bwa', 'euer', 'susa', 'liquiditaet', 'invoice', 'receipts', 'transactions', 'trips', 'worktime']
REPORT_NAMES = ['bwa', 'euer', 'susa', 'liquiditaet']
ROW_WITH_MONTHS = ['div', 'div', 'label', 'input', 'label', 'input', 'div', *['button'] * 5, 'div', *['button'] * 12, 'script']
ROW_WITHOUT_MONTHS = ['div', 'div', 'label', 'input', 'label', 'input', 'div', *['button'] * 5, 'script']


def header3(page):
    """Der Inhalt der dritten Menüzeile (``.header3``): vom öffnenden Tag bis zum Skript des Widgets; dahinter darf nichts mehr in der Zeile stehen."""
    start = page.index(H3_OPEN) + len(H3_OPEN)
    script_end = page.index('</script>', start) + len('</script>')
    assert page[script_end:script_end + len('</div>')] == '</div>', 'hinter dem Widget steht noch etwas in der Menüzeile'
    return page[start:script_end]


def page_and_expected_row(name, db):
    """Die Seite zum Zeitraum März 2026 und die Menüzeile, die ``period_filter_widget`` dafür liefert. Seiten ohne ``today``-Argument lesen die Uhr:
    Der Test liest sie frisch und gibt sie der Hilfsfunktion mit."""
    date_from, date_to = '2026-03-01', '2026-03-31'
    now = datetime.date.today()
    person = _persons(db)[0][0]
    if name == 'dashboard':
        return (PageDashboard(db, date_from, date_to, [1, 2]),
                widget(date_from, date_to, '/dashboard', extra_params={'acct': [1, 2]}, months=MONTHS_NONE, today=now))
    if name == 'bwa':
        return (PageBwa(db, BwaQuery(2026, 3, 'jahr'), date_from, date_to, FIXED),
                widget(date_from, date_to, '/bwa', extra_params={'variant': 'jahr'}, months=MONTHS_ALWAYS, today=FIXED))
    if name == 'euer':
        return PageEuer(db, EuerQuery(date_from, date_to), FIXED), widget(date_from, date_to, '/euer', today=FIXED)
    if name == 'susa':
        return PageSusa(db, SusaQuery(2026, 3), date_from, date_to, FIXED), widget(date_from, date_to, '/susa', months=MONTHS_ALWAYS, today=FIXED)
    if name == 'liquiditaet':
        return (PageLiquidity(db, BwaQuery(2026, 3, 'jahr'), date_from, date_to, FIXED),
                widget(date_from, date_to, '/liquiditaet', extra_params={'variant': 'jahr'}, months=MONTHS_ALWAYS, today=FIXED))
    if name == 'invoice':
        return (PageInvoice(db, filters={'date_from': date_from, 'date_to': date_to}, invoice_id=None),
                widget(date_from, date_to, '/invoice', today=now))
    if name == 'receipts':
        return PageReceipts(db, date_from=date_from, date_to=date_to), widget(date_from, date_to, '/receipts', today=now)
    if name == 'transactions':
        return PageTransactions(db, date_from=date_from, date_to=date_to), widget(date_from, date_to, '/transactions', today=now)
    if name == 'trips':
        return (PageTrips(db, person_id=person, date_from=date_from, date_to=date_to),
                widget(date_from, date_to, '/trips', extra_params={'person': person}, today=now))
    if name == 'worktime':
        return (PageWorkTimes(db, person_id=person, date_from=date_from, date_to=date_to),
                widget(date_from, date_to, '/worktime', extra_params={'person': person}, today=now))
    raise AssertionError(name)


# ── Eine Hilfsfunktion für alle ──────────────────────────────────────────────

@pytest.mark.parametrize('name', PAGE_NAMES)
def test_the_third_menu_row_of_every_page_with_a_period_is_what_the_one_helper_builds(db_with_coa, name):
    page, expected = page_and_expected_row(name, db_with_coa)
    row = header3(page)
    assert row == expected
    for forbidden in ('<form', '<select', '<option', 'coloredButton', 'style=', 'Anzeigen'):
        assert forbidden not in row, forbidden
    assert len(re.findall(r'>(\d{4})</button>', row)) == 5                       # fünf Jahresknöpfe
    dom = Dom(row)
    assert dom.one('input', id='dateFrom')['value'] == '2026-03-01' and dom.one('input', id='dateTo')['value'] == '2026-03-31'
    assert dom.names == (ROW_WITHOUT_MONTHS if name == 'dashboard' else ROW_WITH_MONTHS)


def test_the_four_reports_and_the_dashboard_have_the_same_structure_of_the_row(db_with_coa):
    rows = {name: Dom(header3(page_and_expected_row(name, db_with_coa)[0])).names for name in ['dashboard', *REPORT_NAMES]}
    assert rows['bwa'] == rows['euer'] == rows['susa'] == rows['liquiditaet'] == ROW_WITH_MONTHS
    assert rows['dashboard'] == ROW_WITHOUT_MONTHS == [*ROW_WITH_MONTHS[:-14], 'script']     # dasselbe bis auf Monatsblock und -knöpfe
    attributes = {name: [attrs for tag, attrs in Dom(header3(page_and_expected_row(name, db_with_coa)[0])).tags if tag in ('div', 'input', 'label')]
                  for name in ['dashboard', *REPORT_NAMES]}
    assert attributes['bwa'] == attributes['euer'] == attributes['susa'] == attributes['liquiditaet']
    assert attributes['dashboard'] == attributes['bwa'][:-1]                                  # nur der Block der Monatsknöpfe fehlt


@pytest.mark.parametrize('name', REPORT_NAMES)
def test_a_report_shows_no_form_or_select_above_its_content(db_with_coa, name):
    """Was zwischen dem Seitenkopf und dem Inhalt der Seite steht (Menü, Untermenü, Menüzeile), hat kein Formular und kein Auswahlfeld."""
    page, _expected = page_and_expected_row(name, db_with_coa)
    head = page[:page.index('<div class="rectRounded">')]
    for forbidden in ('<form', '<select', 'coloredButton'):
        assert forbidden not in head, forbidden


# ── Das Dashboard ────────────────────────────────────────────────────────────

def test_the_dashboard_has_neither_of_its_old_year_scripts_nor_an_inline_style_in_its_row(db_with_coa):
    page = PageDashboard(db_with_coa, '2026-03-01', '2026-03-31')
    assert 'setDashYear' not in page and 'applyDashFilter' not in page
    assert 'style=' not in header3(page) and 'onchange="gotoPeriod()"' in header3(page)
    for kept in ('_buildDashURL', 'toggleAllDashAccounts', 'syncDashAccounts'):                 # die Kontenauswahl liest weiter #dateFrom/#dateTo
        assert kept in page
    assert "document.getElementById('dateFrom')" in page and 'id="dateFrom"' in header3(page)


def test_the_year_buttons_of_the_dashboard_stand_in_their_own_block_and_lead_to_whole_years(db_with_coa):
    now = datetime.date.today()
    row = header3(PageDashboard(db_with_coa, f'{now.year}-03-01', f'{now.year}-03-31'))
    dom = Dom(row)
    assert dom.names == ROW_WITHOUT_MONTHS and len(dom.all('div')) == 3             # die Zeile, Von/Bis, die Jahre – kein Monatsblock
    assert [label for label, _href, _active in _years(row)] == [str(year) for year in range(now.year, now.year - 5, -1)]
    assert [href for _label, href, _active in _years(row)] == [f'/dashboard?from={year}-01-01&to={year}-12-31' for year in range(now.year, now.year - 5, -1)]
    assert _months(row) == []


def test_the_active_year_button_of_the_dashboard_follows_the_year_of_the_from_date(db_with_coa):
    now = datetime.date.today()
    assert _active_years(header3(PageDashboard(db_with_coa, f'{now.year}-03-01', f'{now.year}-03-31'))) == [str(now.year)]      # auch bei einem Monat
    assert _active_years(header3(PageDashboard(db_with_coa, f'{now.year - 1}-01-01', f'{now.year - 1}-12-31'))) == [str(now.year - 1)]
    assert _active_years(header3(PageDashboard(db_with_coa, f'{now.year - 1}-04-10', f'{now.year}-02-01'))) == [str(now.year - 1)]


def test_the_chosen_accounts_stand_in_the_addresses_of_the_year_buttons_and_of_the_date_fields(db_with_coa):
    row = header3(PageDashboard(db_with_coa, '2026-01-01', '2026-12-31', [2, 3]))
    hrefs = [href for _label, href, _active in _years(row)]
    assert len(hrefs) == 5 and all(href.endswith('&acct=2&acct=3') for href in hrefs)
    assert "'&to=' + t + '&acct=2&acct=3';" in row                                   # die Von/Bis-Felder (gotoPeriod)
    assert '&acct=' not in header3(PageDashboard(db_with_coa, '2026-01-01', '2026-12-31'))          # alle Konten: nichts mitzuführen
    assert '&acct=' not in header3(PageDashboard(db_with_coa, '2026-01-01', '2026-12-31', []))


# ── Die Darstellung bleibt, der Zeitraum bleibt ──────────────────────────────

@pytest.mark.parametrize('path, page_of', [
    ('/bwa', lambda db, variant: PageBwa(db, BwaQuery(2026, 3, variant), '2026-01-01', '2026-12-31', FIXED)),
    ('/liquiditaet', lambda db, variant: PageLiquidity(db, BwaQuery(2026, 3, variant), '2026-01-01', '2026-12-31', FIXED)),
])
def test_the_variant_stays_over_a_change_of_the_period_and_the_period_over_a_change_of_the_variant(db_with_coa, path, page_of):
    chosen = page_of(db_with_coa, 'jahr')
    buttons = _buttons(header3(chosen))
    assert len(buttons) == 5 + 12 and all(href.endswith('&variant=jahr') for _label, href, _active in buttons)
    assert "'&to=' + t + '&variant=jahr';" in header3(chosen)                       # auch die Von/Bis-Felder
    assert all(href.startswith(f'{path}?from=') for _label, href, _active in buttons)
    default = page_of(db_with_coa, 'vorjahr')
    assert all('variant' not in href for _label, href, _active in _buttons(header3(default))) and 'variant' not in header3(default)
    for page, selected in ((chosen, 'jahr'), (default, 'vorjahr')):
        dom = Dom(page)
        form = dom.one('form', action=path)
        assert form['method'] == 'get' and form['class'] == 'rowWithObjects'
        assert dom.one('input', type='hidden', name='from')['value'] == '2026-01-01' and dom.one('input', type='hidden', name='to')['value'] == '2026-12-31'
        assert dom.one('select', name='variant')['onchange'] == 'this.form.submit()'
        assert [attrs['value'] for tag, attrs in dom.tags if tag == 'option' and 'selected' in attrs] == [selected]


def test_the_susa_has_no_variant_to_carry(db_with_coa):
    page = PageSusa(db_with_coa, SusaQuery(2026, 3), '2026-01-01', '2026-12-31', FIXED)
    assert 'variant' not in page and not Dom(page).all('form') and not Dom(page).all('select')


def test_the_variant_form_masks_the_period_it_carries(db_with_coa):
    hostile = '"><img src=x onerror=alert(1)>'
    page = PageBwa(db_with_coa, BwaQuery(2026, 3, 'jahr'), hostile, hostile, FIXED)
    dom = Dom(page)
    assert 'img' not in dom.names and '"><img' not in page
    assert dom.one('input', type='hidden', name='from')['value'] == hostile                  # als Wert, nicht als Markup
    assert dom.one('input', id='dateFrom')['value'] == hostile
    assert _active_months(header3(page)) == [MONTH_LABELS[FIXED.month - 1]]                  # kein Datum: der laufende Monat


# ── Über den echten Server ───────────────────────────────────────────────────
PERIOD_COOKIE = 'period={}..{}; Path=/; Max-Age=31536000; SameSite=Lax'
TITLES = {'/bwa': 'Betriebswirtschaftliche Auswertung', '/susa': 'Summen- und Saldenliste', '/liquiditaet': 'Liquiditätsrechnung'}
PDF_PATHS = {'/bwa': '/bwa/pdf', '/susa': '/susa/pdf', '/liquiditaet': '/liquiditaet/pdf'}


def month_of(path, text):
    """Der Monat in der Überschrift der Seite, z. B. ``März 2026``."""
    return re.search(rf'{re.escape(TITLES[path])} – ([^<]+)</h3>', text).group(1)


def label(year, month):
    return f'{MONTH_NAMES[month - 1]} {year}'


class Browser:
    """Reicht das Cookie des Zeitraums wie ein Browser weiter: ``Set-Cookie`` einer Antwort wird zum ``Cookie`` der nächsten Anfragen."""

    def __init__(self, port):
        self.port, self.cookie = port, None

    def get(self, path):
        response = get(self.port, path, headers={'Cookie': self.cookie} if self.cookie else None)
        for header in response.headers.get_all('Set-Cookie') or []:
            if header.startswith('period='):
                self.cookie = header.split(';', 1)[0]
        return response


def click(text, name):
    """Die Adresse des Knopfes mit dieser Beschriftung in der Menüzeile der Seite."""
    [href] = [href for found, href, _active in _buttons(header3(text)) if found == name]
    return href


@pytest.mark.parametrize('path', sorted(TITLES))
def test_a_period_in_the_address_shows_its_month_and_sets_the_cookie(server_port, auto_login, path):
    response = get(server_port, f'{path}?from=2026-03-01&to=2026-03-31')
    assert response.status == 200 and month_of(path, response.text) == 'März 2026'
    assert response.headers.get_all('Set-Cookie') == [PERIOD_COOKIE.format('2026-03-01', '2026-03-31')]
    row = header3(response.text)
    assert _active_months(row) == ['Mär']
    dom = Dom(row)
    assert dom.one('input', id='dateFrom')['value'] == '2026-03-01' and dom.one('input', id='dateTo')['value'] == '2026-03-31'


@pytest.mark.parametrize('path', sorted(TITLES))
def test_a_whole_previous_year_in_the_cookie_shows_its_december(server_port, auto_login, path):
    previous = datetime.date.today().year - 1
    response = get(server_port, path, headers={'Cookie': f'period={previous}-01-01..{previous}-12-31'})
    assert response.status == 200 and response.headers.get('Set-Cookie') is None            # das Cookie bleibt, wie es ist
    assert month_of(path, response.text) == f'Dezember {previous}'
    row = header3(response.text)
    assert _active_months(row) == ['Dez'] and _active_years(row) == [str(previous)]
    dom = Dom(row)
    assert dom.one('input', id='dateFrom')['value'] == f'{previous}-01-01' and dom.one('input', id='dateTo')['value'] == f'{previous}-12-31'   # Von/Bis: das Jahr


@pytest.mark.parametrize('path', sorted(TITLES))
def test_the_whole_current_year_shows_the_current_month_and_so_does_a_page_without_a_period(server_port, auto_login, path):
    now = datetime.date.today()
    for headers in (None, {'Cookie': f'period={now.year}-01-01..{now.year}-12-31'}):
        response = get(server_port, path, headers=headers)
        assert response.status == 200 and response.headers.get('Set-Cookie') is None
        assert month_of(path, response.text) == label(now.year, now.month), headers
        assert _active_months(header3(response.text)) == [MONTH_LABELS[now.month - 1]]


@pytest.mark.parametrize('path', sorted(TITLES))
@pytest.mark.parametrize('query', ['year=2026&month=3', 'year=2026&month=03&variant=jahr', 'year=x', 'month=3', 'year=2026&year=2025&month=3'])
def test_the_old_address_with_year_and_month_is_an_address_without_a_period(server_port, auto_login, path, query):
    bare = get(server_port, path)
    response = get(server_port, f'{path}?{query}')
    assert response.status == 200 and response.headers.get('Set-Cookie') is None
    if 'variant=jahr' not in query:
        assert response.body == bare.body
    assert month_of(path, response.text) == month_of(path, bare.text) and 'Der gewählte Zeitraum' not in response.text


@pytest.mark.parametrize('path', sorted(TITLES))
def test_a_swapped_period_is_swapped_back_and_an_invalid_one_dropped_without_a_note(server_port, auto_login, path):
    swapped = get(server_port, f'{path}?from=2026-03-31&to=2026-03-01')
    assert month_of(path, swapped.text) == 'März 2026' and swapped.headers.get_all('Set-Cookie') == [PERIOD_COOKIE.format('2026-03-01', '2026-03-31')]
    for query in ('from=2026-13-01&to=2026-03-31', 'from=x&to=y', 'from=2026-03-01', 'date_from=2026-02-30&date_to=2026-03-31'):
        response = get(server_port, f'{path}?{query}')
        assert response.status == 200 and response.headers.get('Set-Cookie') is None, query
        assert 'Der gewählte Zeitraum' not in response.text and 'ungültig' not in response.text, query


def test_the_euer_page_takes_the_common_period_too(server_port, auto_login):
    previous = datetime.date.today().year - 1
    response = get(server_port, '/euer', headers={'Cookie': f'period={previous}-03-01..{previous}-03-31'})
    assert response.status == 200 and response.headers.get('Set-Cookie') is None
    assert f'Einnahmenüberschussrechnung – 01.03.{previous} – 31.03.{previous}</h3>' in response.text
    assert _active_months(header3(response.text)) == ['Mär']


def test_the_pdf_routes_read_their_own_address_strictly_and_ignore_the_common_period(server_port, auto_login):
    """Die PDF-Routen sind unverändert: ``year``/``month``/``variant`` (``/euer/pdf``: ``from``/``to``, kein stiller Tausch), 400 bei einem Fehler – und kein
    Cookie. Der gemeinsame Zeitraum aus Adresse oder Cookie gilt dort nicht."""
    for path in PDF_PATHS.values():
        for query in ('', '?from=2026-03-01&to=2026-03-31', '?year=2026', '?year=2026&month=13', '?year=2026&year=2025&month=3'):
            response = get(server_port, path + query, headers={'Cookie': 'period=2026-03-01..2026-03-31'})
            assert response.status == 400 and response.text == 'Ungültiger Zeitraum.' and response.headers.get('Set-Cookie') is None, (path, query)
        ok = get(server_port, f'{path}?year=2026&month=3', headers={'Cookie': 'period=2025-01-01..2025-12-31'})
        assert ok.status == 200 and ok.headers['Content-Type'] == 'application/pdf' and ok.body.startswith(b'%PDF-') and ok.headers.get('Set-Cookie') is None
    assert get(server_port, '/euer/pdf?from=2026-12-31&to=2026-01-01').status == 400
    assert get(server_port, '/euer/pdf', headers={'Cookie': 'period=2026-03-01..2026-03-31'}).status == 400
    assert get(server_port, '/euer/pdf?from=2026-03-01&to=2026-03-31').status == 200


def test_the_variant_and_the_period_survive_the_clicks_of_the_menu_row(server_port, auto_login):
    """Wie im Browser: Eine Darstellung gewählt, einen Monat angeklickt – die Darstellung bleibt; die Darstellung gewechselt – der Zeitraum bleibt."""
    year = datetime.date.today().year
    browser = Browser(server_port)
    page = browser.get('/bwa?variant=jahr')
    assert '% der Erlöse' in page.text and 'Veränderung %' not in page.text
    march = browser.get(click(page.text, 'Mär'))
    assert month_of('/bwa', march.text) == f'März {year}' and '% der Erlöse' in march.text                # die Darstellung blieb
    form = Dom(march.text)
    default = browser.get(f"/bwa?from={form.one('input', type='hidden', name='from')['value']}&to={form.one('input', type='hidden', name='to')['value']}"
                          f'&variant=vorjahr')
    assert month_of('/bwa', default.text) == f'März {year}' and 'Veränderung %' in default.text           # der Zeitraum blieb
    assert all('variant' not in href for _label, href, _active in _buttons(header3(default.text)))       # der Standard steht nicht in den Adressen


def test_a_walk_through_the_pages_like_in_the_browser(server_port, auto_login):
    """Einen Monat in der BWA wählen, nach Buchungen wechseln (der Zeitraum bleibt), zurück zur Summen- und Saldenliste (der Monat bleibt); ein ganzes
    Vorjahr gewählt – die Liquidität zeigt dessen Dezember; der PDF-Knopf lädt auf jeder der Seiten weiter."""
    now = datetime.date.today()
    previous = now.year - 1
    browser = Browser(server_port)
    bwa = browser.get('/bwa')
    assert month_of('/bwa', bwa.text) == label(now.year, now.month)                          # nichts gewählt: das laufende Jahr, sein laufender Monat
    march = browser.get(click(bwa.text, 'Mär'))
    assert month_of('/bwa', march.text) == f'März {now.year}'
    transactions = browser.get('/transactions')
    dom = Dom(transactions.text)
    assert dom.one('input', id='dateFrom')['value'] == f'{now.year}-03-01' and dom.one('input', id='dateTo')['value'] == f'{now.year}-03-31'
    susa = browser.get('/susa')
    assert month_of('/susa', susa.text) == f'März {now.year}' and _active_months(header3(susa.text)) == ['Mär']
    euer = browser.get('/euer')
    assert f'Einnahmenüberschussrechnung – 01.03.{now.year} – 31.03.{now.year}</h3>' in euer.text
    # auf der Buchungsseite: der aktive Monat schaltet aufs Jahr, dann das Vorjahr (ganzes Jahr)
    whole = browser.get(click(transactions.text, 'Mär'))
    assert Dom(whole.text).one('input', id='dateFrom')['value'] == f'{now.year}-01-01'
    year = browser.get(click(whole.text, str(previous)))
    assert Dom(year.text).one('input', id='dateTo')['value'] == f'{previous}-12-31'
    liquidity = browser.get('/liquiditaet')
    assert month_of('/liquiditaet', liquidity.text) == f'Dezember {previous}' and _active_months(header3(liquidity.text)) == ['Dez']
    assert Dom(liquidity.text).one('input', id='dateFrom')['value'] == f'{previous}-01-01'          # Von/Bis zeigen weiter das ganze Jahr
    # die Jahresknöpfe einer Monatsauswertung bei einem ganzen Jahr führen auf ganze Jahre; ein Monat darin bleibt ein Monat des Jahres
    june = browser.get(click(liquidity.text, 'Jun'))
    assert month_of('/liquiditaet', june.text) == f'Juni {previous}'
    assert click(june.text, str(now.year)) == f'/liquiditaet?from={now.year}-06-01&to={now.year}-06-30'      # monatserhaltend
    for path in sorted(TITLES):
        shown = browser.get(path)
        data = re.search(r'var (?:BWA|SUSA|LIQUIDITY)_DATA = (.*?);\n', Dom(shown.text).scripts[1], re.S).group(1)
        pdf = browser.get(json.loads(data)['pdf'] + ('&accounts=1' if path != '/susa' else ''))
        assert pdf.status == 200 and pdf.body.startswith(b'%PDF-'), path
        assert f'year={previous}&month=6' in json.loads(data)['pdf']                           # die Adresse des Knopfes folgt dem gezeigten Monat
