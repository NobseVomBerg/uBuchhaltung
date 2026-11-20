# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kundenfilter für Arbeitszeiten-Seite und Stundenzettel, dazu „Mit leeren Tagen“ (seit 2.5.4).

Der Wunsch: Wer je Kunde abrechnet, braucht einen Zettel je Kunde; wer als Mitarbeiter seine Zeit nachweist, den
bisherigen Zettel über alles. Auf einem Kundenzettel stehen der ganze Monat samt freier Tage und Urlaub oder Krank.

* EINE Regel für Seite und PDF (``entry_matches_customer``): Zum Zettel des Kunden gehören seine Einträge und die Einträge
  einer anderen Art als „Arbeit“ ohne Kunden (Urlaub, Krank, Feiertag); Arbeitseinträge anderer Kunden und Arbeitseinträge
  ohne Kunde nie. „ohne Kunde“ nimmt alle Einträge ohne Kunden; kein Filter nimmt alles.
* Den Filter liest ``customer_filter`` streng: ``''``, ``'none'`` oder eine positive ASCII-Ganzzahl in 64 Bit. Auf der
  Seite heißt ungültig „nicht angegeben“; der Stundenzettel lehnt es mit 400 ab – ein Zettel für alle darf nie entstehen,
  weil ein Kundenwert unlesbar war.
* Der Filter bleibt über Speichern, Löschen, Zeitraum und PDF erhalten; die Personenauswahl setzt ihn zurück.
* Der Kundenzettel nennt den Kunden im Kopf, im rechten Unterschriftenblock und im Dateinamen.
* Ohne Filter bleibt alles, wie es war.

Alle Namen und Werte sind erfunden; die Einträge liegen an festen Tagen im März 2026.
"""
import io
import json
import os
import re
import time
import urllib.parse
from html.parser import HTMLParser
from types import SimpleNamespace

import pdfplumber
import pytest
from PIL import Image

import userctx
from export.pdf_worktime import (COL_X, CUSTOMER_LINE_CHARS, CUSTOMER_LINE_SIZE, CUSTOMER_LINE_WIDTH, CUSTOMER_LINE_Y,
                                 ROW_H, TABLE_TOP, customer_line, generate_worktime_pdf, info_width)
from server import handlers
from server.pages_worktime import (CUSTOMER_ALL, CUSTOMER_NONE, PageWorkTimes, _WORKTIME_JS, customer_filter,
                                   customer_query, entry_matches_customer)
from tests.dom_helpers import Dom, data_block
from tests.http_client import get, post
from tests.test_copy_as_new import function_body
from tests.test_worktime_pdf_info import _info_cells
from tests.test_worktime_pdf_pages import (MARCH, YEAR, _cells, _data_rows, _signed, _streams, _sum_cells, _texts, _world)

INT64_MAX = 2 ** 63 - 1


# ── Aufbau der Welt ───────────────────────────────────────────────────────────
def _scenario(db):
    """Person „Erika Probe“ mit fünf Einträgen im März 2026, drei Kunden und einem Kunden ohne Eintrag:

    * 02.03. Arbeit bei Kunde X („Kunde GmbH“), 8 Stunden
    * 03.03. Arbeit bei Kunde Y („albrecht Bau“, klein geschrieben: die Sortierung ignoriert die Schreibweise), 4 Stunden
    * 04.03. Urlaub, ohne Kunden
    * 05.03. Arbeit ohne Kunden, 1 Stunde
    * 06.03. Krank, bei Kunde Y
    Kunde Z („Dritter Kunde KG“) hat keinen Eintrag."""
    world = _world(db)
    other = db.insert_contact(contact_type='customer', entity_type='company', display_name='albrecht Bau',
                              company_name='albrecht Bau', city='Beispielhausen')
    idle = db.insert_contact(contact_type='customer', entity_type='company', display_name='Dritter Kunde KG',
                             company_name='Dritter Kunde KG', city='Nirgendwo')
    ids = dict(
        x_work=db.insert_worktime(world.person, '2026-03-02', customer_id=world.customer, start_time='08:00',
                                  end_time='16:00'),
        y_work=db.insert_worktime(world.person, '2026-03-03', customer_id=other, start_time='08:00', end_time='12:00'),
        vacation=db.insert_worktime(world.person, '2026-03-04', kind='vacation'),
        free_work=db.insert_worktime(world.person, '2026-03-05', start_time='09:00', end_time='10:00'),
        sick_y=db.insert_worktime(world.person, '2026-03-06', kind='sick', customer_id=other))
    return SimpleNamespace(person=world.person, x=world.customer, y=other, z=idle, **ids)


def _chosen(s, which):
    """Der Filterwert zu einem Namen: ``all``, ``none`` oder der Kunde ``x``, ``y``, ``z``."""
    return {'all': CUSTOMER_ALL, 'none': CUSTOMER_NONE, 'x': s.x, 'y': s.y, 'z': s.z}[which]


def _expected(s, chosen):
    """Filter → (die Einträge, die er nimmt, in der Reihenfolge der Liste; Summe; Arbeitstage)."""
    return {
        CUSTOMER_ALL: ([s.x_work, s.y_work, s.vacation, s.free_work, s.sick_y], '13,00', 3),
        CUSTOMER_NONE: ([s.vacation, s.free_work], '1,00', 1),
        s.x: ([s.x_work, s.vacation], '8,00', 1),
        s.y: ([s.y_work, s.vacation, s.sick_y], '4,00', 1),
        s.z: ([s.vacation], '0,00', 0),
    }[chosen]


def _days(count):
    return f'{count} Arbeitstag' if count == 1 else f'{count} Arbeitstage'


def _page(db, s, customer=CUSTOMER_ALL, **kwargs):
    return PageWorkTimes(db, person_id=s.person, date_from=MARCH[0], date_to=MARCH[1], customer=customer, **kwargs)


def _listed(html):
    """Die IDs der Einträge in der Tabelle der Seite, in der Reihenfolge der Zeilen."""
    return [int(attrs['data-id']) for tag, attrs in Dom(html).tags if tag == 'tr' and 'data-id' in attrs]


def _sum_of(html):
    """(Summe, Arbeitstage) der Summenzeile der Seite."""
    found = re.search(r'Summe \(([^)]*)\):</td><td style="text-align:right;">([0-9,]+)</td>', html)
    assert found, 'keine Summenzeile'
    return found.group(2), found.group(1)


class _Options(HTMLParser):
    """Die Optionen einer Auswahl: ``[[Wert, gewählt, Text]]`` – Entitäten aufgelöst, wie der Browser sie liest."""

    def __init__(self, text, select_id):
        super().__init__(convert_charrefs=True)
        self.select_id, self.inside, self.options, self._current = select_id, False, [], None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select':
            self.inside = attrs.get('id') == self.select_id
        elif tag == 'option' and self.inside:
            self._current = [attrs.get('value'), 'selected' in attrs, '']
            self.options.append(self._current)

    def handle_endtag(self, tag):
        if tag == 'select':
            self.inside = False
        elif tag == 'option':
            self._current = None

    def handle_data(self, data):
        if self._current is not None:
            self._current[2] += data


def _options(html):
    return [tuple(option) for option in _Options(html, 'wtCustomerFilter').options]


# ── customer_filter: streng lesen ─────────────────────────────────────────────
@pytest.mark.parametrize('value, expected', [
    ('', CUSTOMER_ALL), ('none', CUSTOMER_NONE), ('7', 7), ('12', 12), ('1', 1), (str(INT64_MAX), INT64_MAX),
])
def test_the_filter_reads_all_none_and_a_positive_id(value, expected):
    result = customer_filter(value)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize('value', [
    '07', '0', '00', '-1', '+7', '1.0', '1,5', '1e3', '0x10', '7 7', '７', '٧', '9' * 20, str(INT64_MAX + 1), 'x', 'None',
    'NONE', 'all', 'null', '7;', '7\x00', '<script>', None, 7, 7.0, True, [], ['7'], b'7',
    # Leerraum ist kein leerer Wert: ``customer=%20`` gälte sonst als „alle“ – und ergäbe einen Zettel über alles
    ' ', '   ', '\t', '\u2003', ' 7 ', '7 ', ' 7', '\t12\n', ' none ', 'none ',
])
def test_the_filter_refuses_everything_else(value):
    assert customer_filter(value) is None


def test_an_address_part_names_the_filter_only_when_one_is_set():
    assert customer_query(CUSTOMER_ALL) == ''
    assert customer_query(CUSTOMER_NONE) == '&customer=none' and customer_query(12) == '&customer=12'


# ── entry_matches_customer: die Tabelle der Regel ─────────────────────────────
def _entry(kind, customer):
    return (1, 1, '2026-03-02', kind, customer, '08:00', '16:00', 0, 'customer', '', '', '')


X, OTHER = 7, 8


@pytest.mark.parametrize('kind, customer, takes', [
    #                      kein Filter, „ohne Kunde“, Kunde X
    ('work', X, (True, False, True)),
    ('work', OTHER, (True, False, False)),
    ('work', None, (True, True, False)),
    ('work', '', (True, True, False)),
    ('work', 0, (True, True, False)),
    (None, X, (True, False, True)),                  # keine Art gespeichert gilt als Arbeit
    (None, None, (True, True, False)),
    ('', None, (True, True, False)),
    ('vacation', X, (True, False, True)),
    ('vacation', OTHER, (True, False, False)),       # der Urlaub eines anderen Kunden steht nicht auf diesem Zettel
    ('vacation', None, (True, True, True)),          # Urlaub ohne Kunden steht auf jedem Zettel
    ('vacation', '', (True, True, True)),
    ('vacation', 0, (True, True, True)),
    ('sick', X, (True, False, True)),
    ('sick', OTHER, (True, False, False)),
    ('sick', None, (True, True, True)),
    # eine Art, die das Formular nicht kennt (SQL-Konsole, Altbestand), ist keine Abwesenheit: auf keinem Kundenzettel
    ('xyz', None, (True, True, False)),
    ('Work', None, (True, True, False)),
    ('VACATION', '', (True, True, False)),
    ('xyz', X, (True, False, True)),
    ('xyz', OTHER, (True, False, False)),
    ('holiday', OTHER, (True, False, False)),
    ('holiday', None, (True, True, True)),
])
def test_the_rule_for_every_kind_and_customer(kind, customer, takes):
    entry = _entry(kind, customer)
    assert tuple(entry_matches_customer(entry, selected) for selected in (CUSTOMER_ALL, CUSTOMER_NONE, X)) == takes


# ── Die Seite ─────────────────────────────────────────────────────────────────
def test_the_selection_names_all_the_customers_of_the_period_and_none_in_alphabetical_order(tmp_db):
    s = _scenario(tmp_db)
    # albrecht (klein) vor Kunde (groß): Die Sortierung ignoriert die Schreibweise; „Dritter Kunde KG“ hat keinen Eintrag
    assert _options(_page(tmp_db, s)) == [('', True, 'Alle'), (str(s.y), False, 'albrecht Bau'),
                                          (str(s.x), False, 'Kunde GmbH'), ('none', False, 'ohne Kunde')]


def test_the_selection_lists_only_customers_with_an_entry_in_the_shown_period(tmp_db):
    s = _scenario(tmp_db)

    def april():
        return PageWorkTimes(tmp_db, person_id=s.person, date_from='2026-04-01', date_to='2026-04-30')

    assert _options(april()) == [('', True, 'Alle'), ('none', False, 'ohne Kunde')]
    tmp_db.insert_worktime(s.person, '2026-04-07', customer_id=s.z, start_time='08:00', end_time='09:00')
    assert [text for _, _, text in _options(april())] == ['Alle', 'Dritter Kunde KG', 'ohne Kunde']


def test_the_selection_of_one_person_does_not_list_the_customers_of_another(tmp_db):
    s = _scenario(tmp_db)
    colleague = tmp_db.insert_contact(contact_type='employee', entity_type='person', display_name='Anton Beispiel',
                                      first_name='Anton', last_name='Beispiel')
    tmp_db.insert_worktime(colleague, '2026-03-02', customer_id=s.z, start_time='08:00', end_time='09:00')
    page = PageWorkTimes(tmp_db, person_id=colleague, date_from=MARCH[0], date_to=MARCH[1])
    assert [text for _, _, text in _options(page)] == ['Alle', 'Dritter Kunde KG', 'ohne Kunde']


@pytest.mark.parametrize('which', ['all', 'none', 'x', 'y', 'z'])
def test_exactly_the_chosen_filter_is_selected_even_for_a_customer_without_entries(tmp_db, which):
    s = _scenario(tmp_db)
    chosen = _chosen(s, which)
    options = _options(_page(tmp_db, s, chosen))
    assert [value for value, selected, _ in options if selected] == [str(chosen)]
    if which == 'z':                                             # der gewählte Kunde ohne Eintrag steht in der Auswahl
        assert [text for _, _, text in options] == ['Alle', 'albrecht Bau', 'Dritter Kunde KG', 'Kunde GmbH', 'ohne Kunde']


def test_an_unknown_customer_still_filters_and_stays_chosen(tmp_db):
    """Nichts fällt still auf „Alle“ zurück: Der Filter gilt (es bleibt nur, was zu keinem Kunden gehört), und die Auswahl nennt
    den Kunden mit dem Zusatz „unbekannt“."""
    s = _scenario(tmp_db)
    html = _page(tmp_db, s, 99999)
    assert _listed(html) == [s.vacation]                         # Urlaub ohne Kunden gehört zu jedem Kunden
    assert [option for option in _options(html) if option[1]] == [('99999', True, 'Kunde 99999 (unbekannt)')]
    assert _sum_of(html) == ('0,00', '0 Arbeitstage')
    colleague = tmp_db.insert_contact(contact_type='employee', entity_type='person', display_name='Berta Beispiel',
                                      first_name='Berta', last_name='Beispiel')
    tmp_db.insert_worktime(colleague, '2026-03-02', start_time='08:00', end_time='09:00')
    page = PageWorkTimes(tmp_db, person_id=colleague, date_from=MARCH[0], date_to=MARCH[1], customer=99999)
    assert _listed(page) == [] and page.count('title="Eintrag anlegen"') == 31          # eine wirklich leere Liste


@pytest.mark.parametrize('which', ['all', 'none', 'x', 'y', 'z'])
def test_list_and_sum_show_only_the_entries_of_the_filter(tmp_db, which):
    s = _scenario(tmp_db)
    listed, total, days = _expected(s, _chosen(s, which))
    html = _page(tmp_db, s, _chosen(s, which))
    assert _listed(html) == listed
    assert _sum_of(html) == (total, _days(days))


def test_the_sum_of_a_customer_is_eight_instead_of_twelve(tmp_db):
    """Das Beispiel des Wunsches: zwei Kunden, 8 und 4 Stunden – der Zettel des einen zählt nur seine acht."""
    world = _world(tmp_db)
    other = tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='Zweiter Kunde',
                                  company_name='Zweiter Kunde')
    tmp_db.insert_worktime(world.person, '2026-03-02', customer_id=world.customer, start_time='08:00', end_time='16:00')
    tmp_db.insert_worktime(world.person, '2026-03-03', customer_id=other, start_time='08:00', end_time='12:00')
    page = PageWorkTimes(tmp_db, person_id=world.person, date_from=MARCH[0], date_to=MARCH[1])
    assert _sum_of(page) == ('12,00', '2 Arbeitstage')
    assert _sum_of(PageWorkTimes(tmp_db, person_id=world.person, date_from=MARCH[0], date_to=MARCH[1],
                                 customer=world.customer)) == ('8,00', '1 Arbeitstag')


@pytest.mark.parametrize('which', ['all', 'none', 'x', 'y'])
def test_the_empty_days_of_the_month_always_stay(tmp_db, which):
    """Die Zeile „Eintrag anlegen“ steht für jeden Tag ohne (passenden) Eintrag – ein Eintrag, den der Filter ausblendet,
    lässt seinen Tag leer stehen, damit man dort etwas anlegen kann."""
    s = _scenario(tmp_db)
    listed = _expected(s, _chosen(s, which))[0]
    html = _page(tmp_db, s, _chosen(s, which))
    assert html.count('title="Eintrag anlegen"') == 31 - len(listed)      # in der Szene hat jeder Eintrag seinen eigenen Tag


def test_the_form_and_the_script_carry_the_filter(tmp_db):
    s = _scenario(tmp_db)
    for chosen, hidden, remembered, address in ((CUSTOMER_ALL, '', '', ''), (CUSTOMER_NONE, 'none', '', '&customer=none'),
                                                (s.x, str(s.x), str(s.x), f'&customer={s.x}')):
        dom = Dom(_page(tmp_db, s, chosen))
        assert dom.one('input', type='hidden', name='customer')['value'] == hidden
        data = data_block(dom, 'WT_DATA')
        assert data['filterCustomer'] == remembered
        assert data['pdfBase'] == f'/worktime/pdf?person={s.person}&from={MARCH[0]}&to={MARCH[1]}{address}'


def test_the_delete_links_keep_the_filter(tmp_db):
    s = _scenario(tmp_db)
    for chosen, tail in ((s.x, f'&customer={s.x}'), (CUSTOMER_NONE, '&customer=none'), (CUSTOMER_ALL, '')):
        links = [attrs['onclick'] for tag, attrs in Dom(_page(tmp_db, s, chosen)).tags
                 if tag == 'a' and attrs.get('title') == 'Löschen']
        listed = _expected(s, chosen)[0]
        assert len(links) == len(listed)
        for onclick, entry in zip(links, listed):
            assert onclick == (f"appConfirmPost('/worktime/delete?person={s.person}&from={MARCH[0]}&to={MARCH[1]}{tail}"
                               f"&id={entry}', 'Eintrag wirklich löschen?')")


def test_without_a_filter_no_address_of_the_page_names_a_customer(tmp_db):
    s = _scenario(tmp_db)
    dom = Dom(_page(tmp_db, s))
    assert 'customer=' not in data_block(dom, 'WT_DATA')['pdfBase']
    assert not [a for t, a in dom.tags if t == 'a' and 'customer=' in (a.get('onclick') or '')]
    buttons = [a['onclick'] for t, a in dom.tags if t == 'button' and '/worktime?from=' in (a.get('onclick') or '')]
    assert len(buttons) == 16 and not [onclick for onclick in buttons if 'customer' in onclick]     # 4 Jahre, 12 Monate
    [script] = [script for script in dom.scripts if 'function gotoPeriod()' in script]
    assert 'customer' not in script


def test_the_period_filter_and_the_selection_keep_the_filter_the_person_selection_drops_it(tmp_db):
    s = _scenario(tmp_db)
    dom = Dom(_page(tmp_db, s, s.x))
    buttons = [a['onclick'] for t, a in dom.tags if t == 'button' and '/worktime?from=' in (a.get('onclick') or '')]
    assert len(buttons) == 16
    assert all(onclick.endswith(f"&person={s.person}&customer={s.x}'") for onclick in buttons)
    [script] = [script for script in dom.scripts if 'function gotoPeriod()' in script]          # die Von/Bis-Felder
    assert f"+ t + '&person={s.person}&customer={s.x}';" in script
    selects = [a['onchange'] for t, a in dom.tags if t == 'select' and a.get('onchange')]
    person_select = [onchange for onchange in selects if "this.value + '&from=" in onchange]
    assert person_select == [f"window.location.href='/worktime?person=' + this.value + '&from={MARCH[0]}&to={MARCH[1]}'"]
    assert dom.one('select', id='wtCustomerFilter')['onchange'] == (
        f"window.location.href='/worktime?person={s.person}&from={MARCH[0]}&to={MARCH[1]}&customer=' "
        "+ encodeURIComponent(this.value)")


def test_a_customer_name_with_markup_is_escaped_in_the_selection(tmp_db):
    world = _world(tmp_db)
    name = 'Meier <b>&"Söhne" \'Co\''
    nasty = tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name=name, company_name=name)
    tmp_db.insert_worktime(world.person, '2026-03-02', customer_id=nasty, start_time='08:00', end_time='09:00')
    html = PageWorkTimes(tmp_db, person_id=world.person, date_from=MARCH[0], date_to=MARCH[1], customer=nasty)
    assert 'Meier <b>' not in html and 'Meier &lt;b&gt;&amp;&quot;S' in html
    assert (str(nasty), True, name) in _options(html)            # der Browser liest daraus wieder den Namen


def test_the_pdf_switch_for_empty_days_is_there_and_on(tmp_db):
    html = _page(tmp_db, _scenario(tmp_db))
    assert ('<label title="Im Monatszettel auch die Tage ohne Eintrag auflisten">'
            '<input type="checkbox" id="wtPdfEmptyDays" checked> Mit leeren Tagen</label>') in html
    assert html.index('id="wtPdfLocation"') < html.index('id="wtPdfEmptyDays"')            # neben „Mit Arbeitsort“


def test_the_script_names_the_empty_days_switch_and_presets_the_customer_of_a_new_entry():
    assert ("url += '&empty=' + (document.getElementById('wtPdfEmptyDays').checked ? '1' : '0');"
            in function_body(_WORKTIME_JS, 'wtOpenPdf'))
    assert "document.getElementById('wtCustomer').value = WT_DATA.filterCustomer || '';" in function_body(_WORKTIME_JS, 'wtClear')
    assert 'wtSetLocCity();' in function_body(_WORKTIME_JS, 'wtClear')         # der Ort folgt dem vorbelegten Kunden


# ── Handler: Speichern behält den Filter ──────────────────────────────────────
def _form(s, **fields):
    form = {'person_id': [str(s.person)], 'date': ['2026-03-20'], 'kind': ['work'], 'start_time': ['08:00'],
            'end_time': ['09:00'], 'location_mode': ['other'], 'location_city': ['Kiel'], 'from': [MARCH[0]],
            'to': [MARCH[1]]}
    form.update({key: [value] for key, value in fields.items()})
    return form


def _query(location):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)


def test_saving_redirects_with_the_filter_when_there_is_a_valid_one(tmp_db):
    s = _scenario(tmp_db)
    base = f'/worktime?person={s.person}&from={MARCH[0]}&to={MARCH[1]}'
    assert handlers.handle_add_worktime(tmp_db, _form(s, date='2026-03-20', customer=str(s.x))) == (303, base + f'&customer={s.x}')
    assert handlers.handle_add_worktime(tmp_db, _form(s, date='2026-03-21', customer='none')) == (303, base + '&customer=none')
    assert handlers.handle_add_worktime(tmp_db, _form(s, date='2026-03-22', customer='')) == (303, base)
    assert handlers.handle_add_worktime(tmp_db, _form(s, date='2026-03-23')) == (303, base)             # das Feld fehlt
    for number, value in enumerate(('abc', '07', '0', '-3', '１', 'None', '7;8', '1e2'), start=1):
        assert handlers.handle_add_worktime(tmp_db, _form(s, date=f'2026-03-{23 + number}', customer=value)) == (303, base), value
    assert len(tmp_db.fetch_worktimes(s.person, '2026-03-20', '2026-03-31')) == 12             # in jedem Fall gespeichert


def test_the_filter_is_not_the_customer_of_the_entry(tmp_db):
    """Das Formular hat ``customer_id`` (der Kunde des Eintrags) und ``customer`` (der Filter der Seite): zwei Felder."""
    s = _scenario(tmp_db)
    status, location = handlers.handle_add_worktime(tmp_db, _form(s, date='2026-03-20', customer_id=str(s.x), customer=str(s.y)))
    assert status == 303 and _query(location)['customer'] == [str(s.y)]
    [row] = tmp_db.fetch_worktimes(s.person, '2026-03-20', '2026-03-20')
    assert row[4] == s.x


def test_the_error_and_overlap_redirects_keep_the_filter(tmp_db):
    s = _scenario(tmp_db)
    status, location = handlers.handle_add_worktime(tmp_db, _form(s, start_time='abc', customer=str(s.x)))
    assert status == 303 and _query(location) == {'person': [str(s.person)], 'from': [MARCH[0]], 'to': [MARCH[1]],
                                                  'customer': [str(s.x)], 'error': [handlers._WT_TIME_ERROR]}
    status, location = handlers.handle_add_worktime(tmp_db, _form(s, date='2026-03-02', customer='none'))      # Überschneidung
    query = _query(location)
    assert status == 303 and query['customer'] == ['none'] and query['error'][0].startswith('Überschneidung am 2026-03-02')
    status, location = handlers.handle_add_worktime(tmp_db, _form(s, date='2026-03-02', customer='abc'))
    assert 'customer' not in _query(location) and 'Überschneidung' in _query(location)['error'][0]
    assert len(tmp_db.fetch_worktimes(s.person, MARCH[0], MARCH[1])) == 5                             # nichts gespeichert


def test_updating_keeps_the_filter_too(tmp_db):
    s = _scenario(tmp_db)
    base = f'/worktime?person={s.person}&from={MARCH[0]}&to={MARCH[1]}'
    update = _form(s, id=str(s.x_work), date='2026-03-02', start_time='08:00', end_time='15:00', customer_id=str(s.x))
    assert handlers.handle_update_worktime(tmp_db, {**update, 'customer': [str(s.x)]}) == (303, base + f'&customer={s.x}')
    assert tmp_db.get_worktime_by_id(s.x_work)[6] == '15:00'
    assert handlers.handle_update_worktime(tmp_db, {**update, 'customer': ['abc']}) == (303, base)
    status, location = handlers.handle_update_worktime(tmp_db, {**update, 'start_time': ['xx'], 'customer': ['none']})
    assert status == 303 and _query(location)['customer'] == ['none'] and _query(location)['error'] == [handlers._WT_TIME_ERROR]


# ── Routen ────────────────────────────────────────────────────────────────────
@pytest.fixture
def zeiten(auto_login):
    s = _scenario(auto_login.db)
    s.csrf, s.db = auto_login.csrf, auto_login.db
    return s


def _address(s, **extra):
    return urllib.parse.urlencode({'person': s.person, 'from': MARCH[0], 'to': MARCH[1], **extra})


def test_the_page_route_filters_by_the_customer_of_the_address(server_port, zeiten):
    s = zeiten
    for chosen in (s.x, s.y, s.z, CUSTOMER_NONE):
        response = get(server_port, '/worktime?' + _address(s, customer=chosen))
        assert response.status == 200
        listed, total, days = _expected(s, chosen)
        assert _listed(response.text) == listed and _sum_of(response.text) == (total, _days(days))
    assert _listed(get(server_port, '/worktime?' + _address(s)).text) == _expected(s, CUSTOMER_ALL)[0]


@pytest.mark.parametrize('value', ['abc', '07', '0', '-1', '1.5', '１', 'None', '<script>alert(1)</script>',
                                   '3%0D%0AX-Injected: 1', str(INT64_MAX + 1)])
def test_an_unreadable_customer_on_the_page_means_no_filter(server_port, zeiten, value):
    """Ein Filter ist ein Filter: fehlt er oder ist er ungültig, gilt er als nicht angegeben – die Seite bleibt benutzbar."""
    s = zeiten
    response = get(server_port, '/worktime?' + _address(s) + '&customer=' + urllib.parse.quote(value, safe='%'))
    assert response.status == 200 and _listed(response.text) == _expected(s, CUSTOMER_ALL)[0]
    assert '<script>alert(1)</script>' not in response.text and 'X-Injected' not in str(response.headers)
    assert Dom(response.text).one('input', type='hidden', name='customer')['value'] == ''


def test_the_edit_route_keeps_the_filter_and_preloads_the_entry_as_before(server_port, zeiten):
    s = zeiten
    response = get(server_port, f'/worktime/edit?id={s.y_work}&' + _address(s, customer=s.x))
    assert response.status == 200
    assert _listed(response.text) == [s.x_work, s.vacation]                       # die Liste folgt dem Filter …
    preload = re.search(r'wtLoadEntry\((\{.*?\})\);', response.text)
    entry = json.loads(preload.group(1))
    assert entry['id'] == s.y_work and entry['customer'] == str(s.y)               # … der geladene Eintrag nicht


def test_saving_and_deleting_through_the_server_keep_a_valid_filter(server_port, zeiten):
    s = zeiten
    form = {'csrf': s.csrf, 'person_id': str(s.person), 'date': '2026-03-20', 'kind': 'work', 'start_time': '08:00',
            'end_time': '09:00', 'location_mode': 'other', 'location_city': 'Kiel', 'from': MARCH[0], 'to': MARCH[1]}
    base = f'/worktime?person={s.person}&from={MARCH[0]}&to={MARCH[1]}'
    response = post(server_port, '/worktime/add', form={**form, 'customer': str(s.x)})
    assert response.status == 303 and response.headers['Location'] == base + f'&customer={s.x}'
    response = post(server_port, '/worktime/add', form={**form, 'date': '2026-03-21', 'customer': 'abc'})
    assert response.status == 303 and response.headers['Location'] == base                   # ungültig: fällt weg
    assert len(s.db.fetch_worktimes(s.person, '2026-03-20', '2026-03-21')) == 2
    for chosen, tail in ((s.x, f'&customer={s.x}'), (CUSTOMER_NONE, '&customer=none')):
        response = post(server_port, '/worktime/delete?' + _address(s, id=99999, customer=chosen), csrf=s.csrf)
        assert response.status == 303 and response.headers['Location'] == base + tail
    for value in ('abc', '07', '3%0D%0AX-Injected: 1'):
        response = post(server_port, '/worktime/delete?' + _address(s, id=99999) + '&customer=' +
                        urllib.parse.quote(value, safe='%'), csrf=s.csrf)
        assert response.status == 303 and response.headers['Location'] == base
        assert 'X-Injected' not in str(response.headers)
    response = post(server_port, '/worktime/delete?' + _address(s, id=s.free_work, customer=s.x), csrf=s.csrf)
    assert response.status == 303 and s.db.get_worktime_by_id(s.free_work) is None


# ── Der Stundenzettel über die Route ──────────────────────────────────────────
def _pdf_response(server_port, s, query=''):
    return get(server_port, f'/worktime/pdf?person={s.person}&from={MARCH[0]}&to={MARCH[1]}{query}')


@pytest.mark.parametrize('value', ['abc', '07', '0', '-1', '1.5', '１', 'None', '<script>alert(1)</script>', str(INT64_MAX + 1),
                                   str(INT64_MAX), '99999'])
def test_the_pdf_refuses_an_unreadable_or_unknown_customer_instead_of_printing_one_for_all(server_port, zeiten, value):
    response = _pdf_response(server_port, zeiten, '&customer=' + urllib.parse.quote(value, safe=''))
    assert response.status == 400 and response.text == 'Ungültiger Kunde.'
    assert response.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert not response.body.startswith(b'%PDF')


def test_the_pdf_takes_an_empty_or_missing_customer_as_all_and_any_contact_as_a_customer(server_port, zeiten):
    s = zeiten
    for query in ('', '&customer=', f'&customer={s.person}'):
        response = _pdf_response(server_port, s, query)
        assert response.status == 200 and response.headers['Content-Type'] == 'application/pdf', query


@pytest.mark.parametrize('query', ['&customer=%20', '&customer=%E2%80%83', '&customer=%20&customer={x}',
                                   '&customer=%20{x}', '&customer={x}%20', '&customer=None', '&customer=NONE'])
def test_the_pdf_never_answers_a_customer_value_it_cannot_read_with_the_sheet_for_all(server_port, zeiten, query):
    response = _pdf_response(server_port, zeiten, query.format(x=zeiten.x))
    assert response.status == 400 and response.text == 'Ungültiger Kunde.'
    assert not response.body.startswith(b'%PDF')


def test_a_very_long_customer_name_gives_a_short_file_name(tmp_db):
    """Ein Kontaktname hat keine Längengrenze; ungekürzt ergäbe er einen Antwort-Kopf, den der Browser ablehnt."""
    from export.pdf_worktime import FILE_PART_LENGTH, _file_part
    assert FILE_PART_LENGTH == 80
    assert _file_part('K' * 50_000) == 'K' * 80 and _file_part('  Beispiel   Kunde AG ') == 'Beispiel___Kunde_AG'
    assert _file_part('../..\\a/b\r\nX: 1') == 'abX_1' and _file_part('') == ''      # kein Pfadanteil, kein Zeilenumbruch
    assert _file_part('<' * 5000 + 'Kunde') == ''                          # betrachtet wird nur der Anfang des Namens
    world = _world(tmp_db)
    with tmp_db._get_connection() as connection:
        connection.execute('UPDATE Contacts SET DisplayName = ? WHERE ID = ?', ('Kunde ' * 9000, world.customer))
    _, path = generate_worktime_pdf(tmp_db, world.person, *MARCH, store=False, customer=world.customer)
    assert len(os.path.basename(path)) < 200


@pytest.mark.parametrize('which, info', [('all', ['Urlaub', 'Krank']), ('none', ['Urlaub']), ('x', ['Urlaub']),
                                         ('y', ['Urlaub', 'Krank']), ('z', ['Urlaub'])])
def test_the_pdf_route_prints_the_sheet_of_the_customer(server_port, zeiten, which, info):
    s = zeiten
    chosen = _chosen(s, which)
    query = f'&customer={chosen}' if which != 'all' else ''
    response = _pdf_response(server_port, s, query)
    assert response.status == 200 and response.headers['Content-Type'] == 'application/pdf'
    [stream] = _streams(response.body)
    cells = _cells(stream)
    _, total, days = _expected(s, chosen)
    assert [text for _, _, text in _sum_cells(cells)] == [f'Summe ({_days(days)}):'] and total in _texts(stream)
    assert [text for _, text in _info_cells(stream)] == info
    assert len(_data_rows(cells)) == 31
    names = {'x': 'Kunde GmbH', 'y': 'albrecht Bau', 'z': 'Dritter Kunde KG', 'none': 'ohne Kunde'}
    head = [text for x, y, text in cells if y == CUSTOMER_LINE_Y]
    assert head == ([] if which == 'all' else [f'Kunde: {names[which]}'])


def test_the_pdf_route_names_the_customer_in_the_file_name(server_port, zeiten):
    s = zeiten
    for query, name in (('', 'Stundenzettel_Erika_Probe_2026-03.pdf'),
                        (f'&customer={s.x}', 'Stundenzettel_Erika_Probe_Kunde_GmbH_2026-03.pdf'),
                        (f'&customer={s.y}&inline=1', 'Stundenzettel_Erika_Probe_albrecht_Bau_2026-03.pdf'),
                        ('&customer=none', 'Stundenzettel_Erika_Probe_ohne-Kunde_2026-03.pdf')):
        disposition = _pdf_response(server_port, s, query).headers['Content-Disposition']
        assert disposition.startswith('inline; ' if 'inline' in query else 'attachment; ')
        assert f'filename="{name}"' in disposition and f"filename*=UTF-8''{name}" in disposition


@pytest.mark.parametrize('switch, rows', [('', 31), ('&empty=1', 31), ('&empty=ja', 31), ('&empty=', 31), ('&empty=0', 2),
                                          ('&empty=false', 2), ('&empty=off', 2)])
def test_the_empty_days_switch_of_the_address_is_on_unless_it_says_no(server_port, zeiten, switch, rows):
    response = _pdf_response(server_port, zeiten, '&customer=none' + switch)
    [stream] = _streams(response.body)
    assert len(_data_rows(_cells(stream))) == rows


def test_without_the_empty_days_only_the_days_with_entries_stand_in_order(server_port, zeiten):
    s = zeiten
    [stream] = _streams(_pdf_response(server_port, s, f'&customer={s.y}&empty=0').body)
    cells = _cells(stream)
    assert _column(cells, 'date') == ['03.03.', '04.03.', '06.03.']
    assert sorted(_data_rows(cells), reverse=True) == [TABLE_TOP, TABLE_TOP - ROW_H, TABLE_TOP - 2 * ROW_H]
    assert [(x, y) for x, y, text in _sum_cells(cells)] == [(40.0, TABLE_TOP - 3 * ROW_H - 3)]


# ── Der Stundenzettel selbst ──────────────────────────────────────────────────
def _sheet(db, s, customer=CUSTOMER_ALL, period=MARCH, **flags):
    return generate_worktime_pdf(db, s.person, *period, store=False, customer=customer, **flags)


def _column(cells, key):
    """Die Texte der Datenzeilen in einer Spalte (ohne Tabellenkopf und leere Zellen), von oben nach unten."""
    rows = sorted(cells, key=lambda cell: -cell[1])
    return [text for x, y, text in rows if x == COL_X[key] and y <= TABLE_TOP and text]


def _same_day(streams):
    """Die Streams ohne das Datum „heute“ in den Unterschriftszeilen – zwei Aufrufe über Mitternacht sollen nicht scheitern."""
    return [re.sub(r'den \d\d\.\d\d\.\d{4}', 'den heute', stream) for stream in streams]


@pytest.mark.parametrize('which', ['all', 'none', 'x', 'y', 'z'])
def test_sum_and_workdays_of_a_sheet_count_only_its_entries(tmp_db, which):
    s = _scenario(tmp_db)
    chosen = _chosen(s, which)
    _, total, days = _expected(s, chosen)
    [stream] = _streams(_sheet(tmp_db, s, chosen)[0])
    cells = _cells(stream)
    assert [text for _, _, text in _sum_cells(cells)] == [f'Summe ({_days(days)}):'] and total in _texts(stream)
    assert len(_data_rows(cells)) == 31
    starts = {'all': ['08:00', '08:00', '09:00'], 'none': ['09:00'], 'x': ['08:00'], 'y': ['08:00'], 'z': []}
    assert _column(cells, 'start') == starts[which]


def test_the_right_signature_belongs_to_the_chosen_customer_even_without_work_in_the_period(tmp_db):
    s = _scenario(tmp_db)
    april = ('2026-04-01', '2026-04-30')                                 # kein Eintrag, nirgends
    [stream] = _streams(_sheet(tmp_db, s, s.x, april)[0])
    cells = _cells(stream)
    assert sorted(x for x, _ in _signed(cells)) == [40.0, 320.0]
    right = {text for x, _, text in cells if x == 320.0}
    assert 'Kunde GmbH' in right and any(text.startswith('Kundenstadt, den ') for text in right)
    assert [text for _, _, text in _sum_cells(cells)] == ['Summe (0 Arbeitstage):'] and '0,00' in _texts(stream)
    [stream] = _streams(_sheet(tmp_db, s, s.y)[0])                       # ein anderer Kunde: seine Stadt, sein Name
    assert {text for x, _, text in _cells(stream) if x == 320.0} >= {'albrecht Bau'}


def test_without_a_customer_there_is_no_right_block_and_the_head_says_so(tmp_db):
    s = _scenario(tmp_db)
    [stream] = _streams(_sheet(tmp_db, s, CUSTOMER_NONE)[0])
    cells = _cells(stream)
    assert [x for x, _ in _signed(cells)] == [40.0]
    assert (40.0, float(CUSTOMER_LINE_Y), 'Kunde: ohne Kunde') in cells
    assert not [text for x, _, text in cells if x == 320.0]


def test_without_a_filter_the_old_rule_stays_one_customer_in_the_entries_makes_the_right_block(tmp_db):
    s = _scenario(tmp_db)
    assert [x for x, _ in _signed(_cells(_streams(_sheet(tmp_db, s)[0])[0]))] == [40.0]      # zwei Kunden: kein rechter Block
    colleague = tmp_db.insert_contact(contact_type='employee', entity_type='person', display_name='Anton Beispiel',
                                      first_name='Anton', last_name='Beispiel')
    tmp_db.insert_worktime(colleague, '2026-03-02', customer_id=s.x, start_time='08:00', end_time='09:00')
    cells = _cells(_streams(generate_worktime_pdf(tmp_db, colleague, *MARCH, store=False)[0])[0])
    assert sorted(x for x, _ in _signed(cells)) == [40.0, 320.0]                              # einer: rechter Block
    assert 'Kunde GmbH' in {text for x, _, text in cells if x == 320.0}


def test_without_a_filter_nothing_changes_for_the_same_data(tmp_db):
    s = _scenario(tmp_db)
    plain = generate_worktime_pdf(tmp_db, s.person, *MARCH, store=False)[0]
    explicit = generate_worktime_pdf(tmp_db, s.person, *MARCH, store=False, customer=CUSTOMER_ALL, with_empty_days=True)[0]
    assert _same_day(_streams(plain)) == _same_day(_streams(explicit))
    cells = _cells(_streams(plain)[0])
    assert not [cell for cell in cells if cell[1] == CUSTOMER_LINE_Y]    # keine vierte Kopfzeile
    assert (40.0, 777.0, 'Zeitraum: März 2026') in cells and len(_data_rows(cells)) == 31


def test_the_customer_line_stands_under_the_period_and_above_the_table_header(tmp_db):
    s = _scenario(tmp_db)
    cells = _cells(_streams(_sheet(tmp_db, s, s.x)[0])[0])
    assert (40.0, 777.0, 'Zeitraum: März 2026') in cells and (40.0, 765.0, 'Kunde: Kunde GmbH') in cells
    assert 751 < CUSTOMER_LINE_Y < 777 and CUSTOMER_LINE_SIZE == 9


def test_the_customer_line_stands_on_every_page(tmp_db):
    world = _world(tmp_db)
    for index in range(60):
        tmp_db.insert_worktime(world.person, f'2026-{1 + index // 28:02d}-{1 + index % 28:02d}', customer_id=world.customer,
                               start_time='08:00', end_time='08:20')
    streams = _streams(_sheet(tmp_db, world, world.customer, YEAR)[0])
    assert len(streams) == 2
    for stream in streams:
        assert (40.0, 765.0, 'Kunde: Kunde GmbH') in _cells(stream)


# ── Die Zeile mit dem Namen des Kunden ────────────────────────────────────────
def test_a_short_name_stays_whole_and_whitespace_counts_as_one_blank():
    assert customer_line('Kunde GmbH') == 'Kunde: Kunde GmbH'
    assert customer_line('Kunde\n\t  GmbH ') == 'Kunde: Kunde GmbH'
    assert customer_line('漢字 AG') == 'Kunde: ?? AG'                    # wie gedruckt: ein Zeichen außerhalb von WinAnsi


def test_a_long_name_is_cut_with_an_ellipsis_before_the_logo_and_uses_the_room():
    name = 'Sehr langer Kundenname ' * 30
    line = customer_line(name)
    assert line.startswith('Kunde: Sehr langer Kundenname') and line.endswith('…')
    assert CUSTOMER_LINE_WIDTH - 12 < info_width(line, CUSTOMER_LINE_SIZE) <= CUSTOMER_LINE_WIDTH
    assert 40 + CUSTOMER_LINE_WIDTH < 430                              # das Logo beginnt bei x = 430


def test_more_than_the_first_characters_are_never_read_and_the_running_time_stays_flat():
    customer_line('Kunde GmbH')                 # lädt die Schrift einmal: gemessen wird nur die Länge des Namens
    for name in ('N' * 2_000_000, 'wort ' * 400_000, '漢' * 200_000, "'" * (CUSTOMER_LINE_CHARS + 1)):
        started = time.perf_counter()
        line = customer_line(name)
        assert time.perf_counter() - started < 1
        assert line.endswith('…') and info_width(line, CUSTOMER_LINE_SIZE) <= CUSTOMER_LINE_WIDTH


def test_brackets_and_backslashes_in_a_customer_name_stay_text_in_the_head_and_the_signature(tmp_db):
    """Der Name steht in einem PDF-Textliteral: Klammern und Backslashes werden maskiert, nichts davon wird ein Befehl."""
    world = _world(tmp_db)
    name = 'Müller (Süd) \\ ) Tj ET BT ( Söhne'
    nasty = tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name=name, company_name=name,
                                  city='Beispielhausen')
    tmp_db.insert_worktime(world.person, '2026-03-02', customer_id=nasty, start_time='08:00', end_time='09:00')
    pdf = generate_worktime_pdf(tmp_db, world.person, *MARCH, store=False, customer=nasty)[0]
    [stream] = _streams(pdf)
    cells = _cells(stream)
    assert (40.0, float(CUSTOMER_LINE_Y), 'Kunde: ' + name) in cells                # im Kopf
    assert (320.0, 236.0, name) in [(x, round(y, 1), text) for x, y, text in cells]  # und unter dem rechten Unterschriftenblock
    assert len(_data_rows(cells)) == 31 and [text for _, _, text in _sum_cells(cells)] == ['Summe (1 Arbeitstag):']


def test_a_long_customer_name_does_not_run_into_the_logo_on_the_page(tmp_db):
    logos = userctx.user_subdir('logos', create=False)
    os.makedirs(logos, exist_ok=True)
    Image.new('RGB', (40, 20), 'red').save(os.path.join(logos, 'firma.png'))
    world = _world(tmp_db, logo='data/logos/firma.png')
    name = 'Sehr langer Kundenname ' * 12
    long_customer = tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name=name,
                                          company_name=name, city='Beispielhausen')
    tmp_db.insert_worktime(world.person, '2026-03-02', customer_id=long_customer, start_time='08:00', end_time='09:00')
    pdf = generate_worktime_pdf(tmp_db, world.person, *MARCH, store=False, customer=long_customer)[0]
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        page = document.pages[0]
        words = page.extract_words()
        [logo] = page.images
    [head] = [word for word in words if word['text'] == 'Kunde:']
    line = [word for word in words if abs(word['top'] - head['top']) < 2]
    assert line[-1]['text'].endswith('…') and len(line) > 3
    assert max(word['x1'] for word in line) <= 425 and logo['x0'] >= 429 > max(word['x1'] for word in line)


# ── Dateiname ─────────────────────────────────────────────────────────────────
def test_the_file_name_names_the_customer_cleaned_like_the_person(tmp_db):
    s = _scenario(tmp_db)
    odd = tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='Müller/Söhne: <AG>',
                                company_name='Müller/Söhne: <AG>')
    names ={chosen: os.path.basename(_sheet(tmp_db, s, chosen)[1]) for chosen in (CUSTOMER_ALL, CUSTOMER_NONE, s.x, odd)}
    assert names == {CUSTOMER_ALL: 'Stundenzettel_Erika_Probe_2026-03.pdf',
                     CUSTOMER_NONE: 'Stundenzettel_Erika_Probe_ohne-Kunde_2026-03.pdf',
                     s.x: 'Stundenzettel_Erika_Probe_Kunde_GmbH_2026-03.pdf',
                     odd: 'Stundenzettel_Erika_Probe_MüllerSöhne_AG_2026-03.pdf'}
    assert os.path.basename(_sheet(tmp_db, s, s.x, YEAR)[1]) == (
        'Stundenzettel_Erika_Probe_Kunde_GmbH_2026-01-01_2026-12-31.pdf')


def test_a_stored_sheet_of_a_customer_lands_under_its_own_name(tmp_db):
    s = _scenario(tmp_db)
    pdf, path = generate_worktime_pdf(tmp_db, s.person, *MARCH, customer=s.x)               # store=True
    assert os.path.basename(path) == 'Stundenzettel_Erika_Probe_Kunde_GmbH_2026-03.pdf'
    with open(path, 'rb') as stored:
        assert stored.read() == pdf


# ── Mit leeren Tagen ──────────────────────────────────────────────────────────
@pytest.mark.parametrize('which, dates', [('all', ['02.03.', '03.03.', '04.03.', '05.03.', '06.03.']),
                                          ('none', ['04.03.', '05.03.']), ('x', ['02.03.', '04.03.']),
                                          ('z', ['04.03.'])])
def test_without_the_empty_days_a_month_sheet_lists_only_the_days_with_entries(tmp_db, which, dates):
    s = _scenario(tmp_db)
    chosen = _chosen(s, which)
    pdf = _sheet(tmp_db, s, chosen, with_empty_days=False)[0]
    [stream] = _streams(pdf)
    cells = _cells(stream)
    assert _column(cells, 'date') == dates
    assert [text for _, _, text in _sum_cells(cells)] == [f'Summe ({_days(_expected(s, chosen)[2])}):']
    assert sorted(_data_rows(cells), reverse=True) == [TABLE_TOP - ROW_H * number for number in range(len(dates))]


def test_a_month_without_any_entry_and_without_empty_days_is_a_sheet_with_only_the_sum(tmp_db):
    s = _scenario(tmp_db)
    pdf = _sheet(tmp_db, s, period=('2026-04-01', '2026-04-30'), with_empty_days=False)[0]
    [stream] = _streams(pdf)
    cells = _cells(stream)
    assert _data_rows(cells) == [] and [text for _, _, text in _sum_cells(cells)] == ['Summe (0 Arbeitstage):']
    assert (40.0, TABLE_TOP - 3.0, 'Summe (0 Arbeitstage):') in cells


def test_over_several_months_the_switch_changes_nothing(tmp_db):
    s = _scenario(tmp_db)
    with_days = _streams(_sheet(tmp_db, s, s.x, YEAR, with_empty_days=True)[0])
    without = _streams(_sheet(tmp_db, s, s.x, YEAR, with_empty_days=False)[0])
    assert _same_day(with_days) == _same_day(without)
    assert _column(_cells(with_days[0]), 'date') == ['02.03.', '04.03.']
