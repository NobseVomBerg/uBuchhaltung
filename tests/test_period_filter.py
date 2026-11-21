# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Tests für den zentralen Zeitraum-Filter (server/period.py).

Deckt das erweiterte period_filter_widget ab:
  - fünf Jahres-Buttons als Standard, dazu Monats-Buttons
  - aktiver Monat hervorgehoben (single_month)
  - Monats-Toggle: Klick auf aktiven Monat führt zurück aufs ganze Jahr
  - Jahres-Buttons monatserhaltend bei Monatswahl
  - extra_params werden (URL-enkodiert) mitgeführt, eine Liste wiederholt den Schlüssel
  - die drei Arten von Monats-Buttons (toggle / always / none) und ``period_month``
Sowie die Integration in den betroffenen Seiten (Filter in Header2,
Monats-Buttons in Header3). Dass JEDE Seite mit Zeitraum ihre dritte Menüzeile aus dieser einen
Funktion bekommt, prüft ``test_period_menu_row.py``.
"""
import datetime
import re

import pytest

from server.period import (MONTHS_ALWAYS, MONTHS_NONE, MONTHS_TOGGLE, YEAR_BUTTONS, period_filter_widget,
                           period_month)
from tests.dom_helpers import Dom

TODAY = datetime.date(2026, 11, 21)             # ein festes „heute“ – die Tests hängen nicht an der Uhr
MONTH_LABELS = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']


# ── reine Widget-Funktion ────────────────────────────────────────────────────

def test_widget_full_year_has_year_and_month_buttons():
    h = period_filter_widget('2026-01-01', '2026-12-31', '/transactions')
    # alle Monatskürzel vorhanden
    for m in ['Jan', 'Feb', 'Mär', 'Dez']:
        assert f'>{m}<' in h
    # aktives Jahr markiert
    assert "class='active'" in h
    # im Ganzjahres-Modus ist kein Monat aktiv
    assert not re.search(r"class='active'[^>]*>(Jan|Feb|Mär|Apr|Mai|Jun|Jul|Aug|Sep|Okt|Nov|Dez)<", h)


def test_widget_single_month_marks_active_month():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/transactions')
    assert re.search(r"<button class='active' type=\"button\"[^>]*>Jun<", h)


def test_widget_active_month_toggles_back_to_full_year():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/transactions')
    m = re.search(r"<button class='active' type=\"button\" onclick=\"window.location.href='([^']+)'\">Jun<", h)
    assert m, 'aktiver Monats-Button nicht gefunden'
    assert m.group(1) == '/transactions?from=2026-01-01&to=2026-12-31'


def test_widget_year_button_is_month_preserving_in_month_mode():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/transactions')
    m = re.search(r"onclick=\"window.location.href='([^']+)'\">2025<", h)
    assert m
    assert m.group(1) == '/transactions?from=2025-06-01&to=2025-06-30'


def test_widget_extra_params_are_carried_and_encoded():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/invoice',
                             extra_params={'status': 'paid', 'search': 'a b', 'id': 3})
    # an Buttons angehängt
    assert '&status=paid&search=a%20b&id=3' in h
    # auch im gotoPeriod-JS für die Von/Bis-Felder
    assert "+ t + '&status=paid&search=a%20b&id=3'" in h


def test_widget_empty_extra_params_are_skipped():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/invoice',
                             extra_params={'status': '', 'search': None, 'id': ''})
    assert "from=2026-01-01&to=2026-12-31'" in h  # Toggle-Ziel ohne Anhang
    assert '&status=' not in h
    assert '&search=' not in h


# ── fünf Jahre, Aufbau der Zeile ─────────────────────────────────────────────

def _buttons(html):
    """(Beschriftung, Adresse, aktiv) je Knopf der Zeile, in der Reihenfolge der Seite."""
    pattern = r"<button( class='active')? type=\"button\" onclick=\"window\.location\.href='([^']*)'\">([^<]*)</button>"
    return [(m.group(3), m.group(2), bool(m.group(1))) for m in re.finditer(pattern, html)]


def _years(html):
    return [button for button in _buttons(html) if button[0].isdigit()]


def _months(html):
    return [button for button in _buttons(html) if button[0] in MONTH_LABELS]


def _active_months(html):
    return [label for label, _href, active in _months(html) if active]


def _active_years(html):
    return [label for label, _href, active in _years(html) if active]


def test_widget_shows_five_year_buttons_by_default():
    assert YEAR_BUTTONS == 5
    h = period_filter_widget('2026-01-01', '2026-12-31', '/transactions', today=TODAY)
    assert [label for label, _href, _active in _years(h)] == ['2026', '2025', '2024', '2023', '2022']
    assert len(_months(h)) == 12
    three = period_filter_widget('2026-01-01', '2026-12-31', '/transactions', num_years=3, today=TODAY)
    assert [label for label, _href, _active in _years(three)] == ['2026', '2025', '2024']


def test_year_buttons_count_from_the_day_the_page_passes_in():
    h = period_filter_widget('2031-01-01', '2031-12-31', '/trips', today=datetime.date(2031, 6, 30))
    assert [label for label, _href, _active in _years(h)] == ['2031', '2030', '2029', '2028', '2027']
    assert _active_years(h) == ['2031']


def test_the_row_has_the_blocks_dates_years_months_in_this_order():
    names = Dom(period_filter_widget('2026-06-01', '2026-06-30', '/transactions', today=TODAY)).names
    assert names == ['div', 'div', 'label', 'input', 'label', 'input', 'div', *['button'] * 5, 'div', *['button'] * 12, 'script']


# ── die drei Arten von Monatsknöpfen ─────────────────────────────────────────

def test_toggle_is_the_default_and_the_active_month_leads_back_to_the_year():
    default = period_filter_widget('2026-06-01', '2026-06-30', '/transactions', today=TODAY)
    assert period_filter_widget('2026-06-01', '2026-06-30', '/transactions', months=MONTHS_TOGGLE, today=TODAY) == default
    [(label, href, _active)] = [button for button in _months(default) if button[2]]
    assert (label, href) == ('Jun', '/transactions?from=2026-01-01&to=2026-12-31')
    assert _active_months(period_filter_widget('2026-01-01', '2026-12-31', '/transactions', today=TODAY)) == []     # ein Jahr: kein Monat aktiv


ALWAYS = {'months': MONTHS_ALWAYS, 'today': TODAY}


@pytest.mark.parametrize('date_from, date_to, month, year', [
    ('2026-06-01', '2026-06-30', 'Jun', '2026'),           # genau ein Monat: dieser
    ('2025-01-01', '2025-12-31', 'Dez', '2025'),           # ganzes Vorjahr: dessen Dezember
    ('2026-01-01', '2026-12-31', 'Nov', '2026'),           # ganzes laufendes Jahr: der laufende Monat
    ('2027-01-01', '2027-12-31', 'Dez', None),             # ein Jahr in der Zukunft: Dezember (für 2027 gibt es keinen Jahresknopf)
    ('2026-02-10', '2026-05-20', 'Mai', '2026'),           # irgendein Bereich: der Monat des Bis-Datums
    ('2025-11-15', '2026-02-03', 'Feb', '2026'),           # über den Jahreswechsel
    ('x', 'y', 'Nov', '2026'),                             # kein Datum: der laufende Monat
])
def test_always_marks_exactly_one_month_and_the_year_of_that_month(date_from, date_to, month, year):
    h = period_filter_widget(date_from, date_to, '/bwa', **ALWAYS)
    assert _active_months(h) == [month]
    assert _active_years(h) == ([year] if year else [])


def test_always_the_active_month_leads_to_itself_and_the_others_to_their_month_of_the_active_year():
    months = {label: href for label, href, _active in _months(period_filter_widget('2026-06-01', '2026-06-30', '/bwa', **ALWAYS))}
    assert months['Jun'] == '/bwa?from=2026-06-01&to=2026-06-30'            # kein Umschalten aufs Jahr
    assert months['Jan'] == '/bwa?from=2026-01-01&to=2026-01-31' and months['Feb'] == '/bwa?from=2026-02-01&to=2026-02-28'
    assert months['Dez'] == '/bwa?from=2026-12-01&to=2026-12-31'
    previous = {label: href for label, href, _active in _months(period_filter_widget('2025-01-01', '2025-12-31', '/bwa', **ALWAYS))}
    assert previous['Dez'] == '/bwa?from=2025-12-01&to=2025-12-31'          # der aktive Dezember führt auf sich selbst
    assert previous['Mär'] == '/bwa?from=2025-03-01&to=2025-03-31'          # die anderen: im Jahr des aktiven Monats
    assert all(href.startswith('/bwa?from=2025-') for href in previous.values())


def test_always_the_year_buttons_keep_a_chosen_month_and_otherwise_lead_to_whole_years():
    one_month = _years(period_filter_widget('2026-02-01', '2026-02-28', '/susa', **ALWAYS))
    assert [href for _label, href, _active in one_month] == [
        '/susa?from=2026-02-01&to=2026-02-28', '/susa?from=2025-02-01&to=2025-02-28', '/susa?from=2024-02-01&to=2024-02-29',
        '/susa?from=2023-02-01&to=2023-02-28', '/susa?from=2022-02-01&to=2022-02-28']                     # 2024 ist ein Schaltjahr
    whole = _years(period_filter_widget('2025-01-01', '2025-12-31', '/susa', **ALWAYS))
    assert [href for _label, href, _active in whole] == [f'/susa?from={year}-01-01&to={year}-12-31' for year in (2026, 2025, 2024, 2023, 2022)]
    several = _years(period_filter_widget('2026-02-10', '2026-05-20', '/susa', **ALWAYS))
    assert [href for _label, href, _active in several] == [f'/susa?from={year}-01-01&to={year}-12-31' for year in (2026, 2025, 2024, 2023, 2022)]


def test_always_has_exactly_one_active_month_for_every_period_and_day():
    periods = [('2026-01-01', '2026-12-31'), ('2025-01-01', '2025-12-31'), ('2026-03-01', '2026-03-31'), ('2024-02-01', '2024-02-29'),
               ('2026-02-10', '2026-05-20'), ('2025-11-15', '2026-02-03'), ('2026-12-01', '2026-12-31'), ('2030-01-01', '2030-12-31'),
               ('2026-03-05', '2026-03-05'), ('2019-04-01', '2019-06-30'), ('x', '2026-12-31'), ('', ''), (None, None)]
    for today in (datetime.date(2026, 1, 1), datetime.date(2026, 3, 15), datetime.date(2026, 11, 21), datetime.date(2026, 12, 31),
                  datetime.date(2027, 6, 30)):
        for date_from, date_to in periods:
            h = period_filter_widget(date_from, date_to, '/bwa', months=MONTHS_ALWAYS, today=today)
            year, month = period_month(date_from, date_to, today)
            assert _active_months(h) == [MONTH_LABELS[month - 1]], (date_from, date_to, today)
            assert _active_years(h) in ([str(year)], []), (date_from, date_to, today)
            assert (str(year) in [label for label, _href, _active in _years(h)]) == (_active_years(h) == [str(year)])


def test_always_carries_the_extra_parameters_in_every_address():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/liquiditaet', extra_params={'variant': 'jahr'}, **ALWAYS)
    assert all(href.endswith('&variant=jahr') for _label, href, _active in _buttons(h)) and len(_buttons(h)) == 5 + 12
    assert "+ t + '&variant=jahr'" in h


def test_none_has_no_month_buttons_and_only_two_blocks_in_the_row():
    h = period_filter_widget('2026-03-01', '2026-03-31', '/dashboard', months=MONTHS_NONE, today=TODAY)
    dom = Dom(h)
    assert dom.names == ['div', 'div', 'label', 'input', 'label', 'input', 'div', *['button'] * 5, 'script']
    assert len(dom.all('div')) == 3 and _months(h) == []                    # die Zeile und genau zwei innere Blöcke
    assert 'Jan' not in h and 'Dez' not in h


def test_none_leads_the_year_buttons_to_whole_years_and_marks_the_year_of_the_from_date():
    # ohne Monatsknöpfe gäbe es keinen Weg zurück vom Monat aufs Jahr: Die Jahresknöpfe wählen immer das ganze Jahr
    month = period_filter_widget('2026-03-01', '2026-03-31', '/dashboard', months=MONTHS_NONE, today=TODAY)
    assert [href for _label, href, _active in _years(month)] == [f'/dashboard?from={year}-01-01&to={year}-12-31'
                                                                 for year in (2026, 2025, 2024, 2023, 2022)]
    assert _active_years(month) == ['2026']
    earlier = period_filter_widget('2025-04-10', '2025-09-30', '/dashboard', months=MONTHS_NONE, today=TODAY)
    assert _active_years(earlier) == ['2025']
    assert _active_years(period_filter_widget('2019-01-01', '2019-12-31', '/dashboard', months=MONTHS_NONE, today=TODAY)) == []


@pytest.mark.parametrize('months', ['', 'TOGGLE', 'never', None, 1, ['toggle']])
def test_an_unknown_kind_of_month_buttons_is_refused(months):
    with pytest.raises(ValueError):
        period_filter_widget('2026-06-01', '2026-06-30', '/transactions', months=months)


# ── Listen in extra_params ───────────────────────────────────────────────────

def test_a_list_in_extra_params_repeats_the_key():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/dashboard', extra_params={'acct': [1, 2]}, months=MONTHS_NONE, today=TODAY)
    assert all(href.endswith('&acct=1&acct=2') for _label, href, _active in _buttons(h)) and len(_buttons(h)) == 5
    assert "+ t + '&acct=1&acct=2'" in h
    assert period_filter_widget('2026-06-01', '2026-06-30', '/dashboard', extra_params={'acct': (7,)}, today=TODAY).count('&acct=7') == 17 + 1


def test_empty_items_and_empty_lists_in_extra_params_are_left_out():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/invoice', today=TODAY,
                             extra_params={'acct': [], 'x': ['', None, 3], 'y': (), 'z': (4, 5), 'person': 7})
    assert '&x=3&z=4&z=5&person=7' in h
    assert 'acct' not in h and '&y=' not in h and '&x=&' not in h and '&x=None' not in h
    bare = period_filter_widget('2026-06-01', '2026-06-30', '/invoice', today=TODAY)
    assert period_filter_widget('2026-06-01', '2026-06-30', '/invoice', today=TODAY, extra_params={'acct': [], 'x': ['', None]}) == bare


def test_hostile_items_of_a_list_are_encoded_like_any_other_value():
    h = period_filter_widget('2026-06-01', '2026-06-30', '/dashboard', today=TODAY,
                             extra_params={'acct': ["1'2", '</script>', '"><img src=x>']})
    dom = Dom(h)
    assert len(dom.scripts) == 1 and 'img' not in dom.names
    assert '&acct=1%272&acct=%3C/script%3E&acct=%22%3E%3Cimg%20src%3Dx%3E' in h
    assert "1'2" not in h and '"><img' not in h and '</script>' not in dom.scripts[0]


# ── Integration in die Seiten ────────────────────────────────────────────────

def test_invoice_filters_in_header2_and_month_in_header3(db_with_coa):
    from server.pages_invoice import PageInvoice
    h = PageInvoice(db_with_coa,
                    filters={'date_from': '2026-06-01', 'date_to': '2026-06-30'},
                    invoice_id=None)
    assert 'id="searchQuery"' in h
    assert 'id="statusFilter"' in h
    assert '>Jun<' in h
    # alte Inline-Jahreswahl ist weg
    assert 'setInvoiceYear' not in h


def test_receipts_search_present_and_month_buttons(db_with_coa):
    from server.pages_receipts import PageReceipts
    h = PageReceipts(db_with_coa, date_from='2026-06-01', date_to='2026-06-30')
    assert 'id="receiptSearch"' in h
    assert '>Jun<' in h


def test_transactions_account_dropdown_and_filters(db_with_coa):
    from server.pages_transactions import PageTransactions
    h = PageTransactions(db_with_coa, date_from='2026-06-01', date_to='2026-06-30')
    assert 'id="acctMenuPanel"' in h
    assert 'position:absolute' in h          # Overlay statt 2. Header-Zeile
    assert 'id="account_all"' in h          # Checkbox-Logik bleibt erhalten
    assert 'id="txSearch"' in h
    assert 'function toggleAcctMenu' in h
    assert '>Jun<' in h


def test_worktime_uses_central_widget(db_with_coa):
    from server.pages_worktime import PageWorkTimes
    h = PageWorkTimes(db_with_coa, person_id=None,
                      date_from='2026-06-01', date_to='2026-06-30')
    assert '>Jun<' in h
    assert '&person=' in h
    assert 'wtGotoDates' not in h           # eigene JS-Funktion ersetzt


# ── period_month: der Monat einer Monatsauswertung zum gemeinsamen Zeitraum ──

@pytest.mark.parametrize('date_from, date_to, expected', [
    ('2026-03-01', '2026-03-31', (2026, 3)),               # genau ein Kalendermonat
    ('2026-12-01', '2026-12-31', (2026, 12)),              # ein Monat bleibt, auch wenn er hinter „heute“ liegt
    ('2025-01-01', '2025-12-31', (2025, 12)),              # ganzes Vorjahr: dessen letzter Monat
    ('2026-01-01', '2026-12-31', (2026, 11)),              # ganzes laufendes Jahr: der laufende Monat
    ('2027-01-01', '2027-12-31', (2027, 12)),              # ein Jahr in der Zukunft: Dezember, wie es ist
    ('2026-02-10', '2026-05-20', (2026, 5)),               # irgendein Bereich: der Monat des Bis-Datums
    ('2026-03-05', '2026-03-05', (2026, 3)),               # ein einzelner Tag
    ('2025-11-15', '2026-02-03', (2026, 2)),               # über den Jahreswechsel
    ('2025-11-15', '2026-12-31', (2026, 11)),              # über den Jahreswechsel, Bis hinter „heute“: höchstens der laufende Monat
    ('2024-02-01', '2024-02-29', (2024, 2)),               # Februar im Schaltjahr: genau ein Monat
    ('2024-02-01', '2024-02-28', (2024, 2)),               # kein ganzer Februar: der Monat des Bis-Datums, derselbe
    ('2025-02-01', '2025-02-28', (2025, 2)),
    ('2023-12-31', '2024-01-01', (2024, 1)),               # zwei Tage über den Jahreswechsel
])
def test_period_month(date_from, date_to, expected):
    assert period_month(date_from, date_to, TODAY) == expected


@pytest.mark.parametrize('date_from, date_to', [
    ('x', 'y'), ('', ''), (None, None), ('2026-03-01', 'x'), ('x', '2026-03-31'), ('2026-13-01', '2026-13-31'), ('2026-02-30', '2026-03-31'),
    (' 2026-03-01', '2026-03-31'), ('2026-3-1', '2026-3-31'), ('２０２６-03-01', '2026-03-31'), ('1899-01-01', '1899-12-31'),
    (datetime.date(2026, 3, 1), datetime.date(2026, 3, 31)), (20260301, 20260331),
])
def test_period_month_of_dates_that_are_none_is_the_current_month(date_from, date_to):
    assert period_month(date_from, date_to, TODAY) == (2026, 11)


@pytest.mark.parametrize('today, expected', [
    (datetime.date(2026, 1, 1), (2026, 1)), (datetime.date(2026, 3, 15), (2026, 3)), (datetime.date(2026, 12, 31), (2026, 12)),
    (datetime.date(2027, 5, 5), (2026, 12)),               # das Jahr liegt in der Vergangenheit: sein Dezember
    (datetime.date(2025, 8, 8), (2026, 12)),               # das Jahr liegt in der Zukunft: sein Dezember
])
def test_a_whole_year_shows_its_last_month_but_never_one_in_the_future_of_the_current_year(today, expected):
    assert period_month('2026-01-01', '2026-12-31', today) == expected
