# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Arbeitszeit rechnet in Minuten und über Mitternacht (T63).

* ``clock_minutes``, ``compute_minutes``, ``crosses_midnight``, ``compute_hours``, ``format_hours``
  (``server/pages_worktime.py``): Ende vor Beginn = Folgetag, Ende = Beginn = 0 Minuten, die Pause
  drückt nie unter 0, leere und ungültige Zeiten (auch Ziffern anderer Schriften) ergeben 0
* Summen entstehen aus Minuten, nie aus gerundeten Stundenwerten: 3 × 20 min sind 1,00 h, die Zeilen
  zeigen je 0,33
* Seite: „(+1)“ hinter der Endzeit eines Eintrags über Mitternacht, Arbeitstage zählen Kalendertage
* Speichern: eine Start- oder Endzeit, die nicht leer und nicht HH:MM ist, wird mit Klartext abgelehnt
  (nichts gespeichert, Meldung über ``?error=`` und ``appMsg``)
* Überschneidungssperre rechnet ein Ende vor dem Beginn als Folgetag
* Die Stundenanzeige der Maske (JavaScript) rechnet nach derselben Regel

Alle Namen und Werte sind erfundene Testdaten; „heute“ kommt nirgends vor.
"""
import json
import random
import re
import shutil
import subprocess
import urllib.parse

import pytest

from server.handlers import handle_add_worktime, handle_update_worktime
from server.pages_worktime import (PageWorkTimes, clock_minutes, compute_hours, compute_minutes,
                                   crosses_midnight, format_hours)
from tests.dom_helpers import Dom, app_msg_arguments
from tests.http_client import get, post

TIME_ERROR = 'Bitte die Uhrzeit als HH:MM angeben.'


def _person(db):
    return db.insert_contact(contact_type='employee', entity_type='person', display_name='Erika Probe',
                             first_name='Erika', last_name='Probe')


def _page(db, person, date_from='2026-03-01', date_to='2026-03-31', **kwargs):
    return PageWorkTimes(db, person_id=person, date_from=date_from, date_to=date_to, **kwargs)


def _cells(html_fragment):
    return [re.sub(r'<[^>]+>', '', cell) for cell in re.findall(r'<td[^>]*>(.*?)</td>', html_fragment, re.S)]


def _row(html, entry_id):
    """Die Zellen der Tabellenzeile eines Eintrags: Tag, Datum, Start, Ende, Pause, Std., Info, Notiz, Aktionen."""
    return _cells(re.search(rf'<tr[^>]*data-id="{entry_id}"[^>]*>(.*?)</tr>', html, re.S).group(1))


def _sum_row(html):
    return _cells(re.search(r'<tr class="wt-sum">(.*?)</tr>', html, re.S).group(1))


# ── Die Uhrzeit ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize('value, expected', [
    ('00:00', 0), ('08:00', 480), ('8:00', 480), ('12:30', 750), ('23:59', 1439),
])
def test_clock_minutes_reads_a_valid_time(value, expected):
    assert clock_minutes(value) == expected


@pytest.mark.parametrize('value', [
    None, '', ' ', '25:00', '24:00', '12:60', '-1:00', '+8:00', '8:5', '08:0', 'ab:cd', '12', '12:', ':30',
    '08:00:00', '1:2:3', ' 08:00', '08:00 ', '08:00\n', '08.00',
    '٠٨:٠٠', '０８:００', '०८:००',                    # Ziffern anderer Schriften: int() läse sie, eine Uhrzeit sind sie nicht
    800, 8.5, True, b'08:00', ['08:00'],
])
def test_clock_minutes_rejects_everything_that_is_no_time(value):
    assert clock_minutes(value) is None


# ── Minuten ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('start, end, pause, minutes', [
    ('08:00', '16:00', 0, 480),
    ('08:00', '17:00', 60, 480),
    ('09:15', '12:45', 0, 210),
    ('8:00', '9:30', 0, 90),
    ('08:00', '08:20', 0, 20),
    # Ende vor Beginn: Das Ende liegt am Folgetag
    ('22:00', '02:00', 30, 210),
    ('22:00', '02:00', 0, 240),
    ('17:00', '08:00', 0, 900),
    ('23:59', '00:00', 0, 1),
    ('08:01', '08:00', 0, 1439),
    # Ende = Beginn: kein 24-Stunden-Tag
    ('08:00', '08:00', 0, 0),
    ('00:00', '00:00', 0, 0),
    # Die Pause drückt nie unter 0
    ('08:00', '09:00', 120, 0),
    ('08:00', '09:00', 60, 0),
    ('22:00', '02:00', 300, 0),
    # Negative oder nicht lesbare Pause zählt als 0
    ('08:00', '09:00', -30, 60),
    ('08:00', '09:00', None, 60),
    ('08:00', '09:00', '', 60),
    ('08:00', '09:00', 'abc', 60),
    ('08:00', '09:00', '1e3', 60),
    ('08:00', '09:00', float('nan'), 60),
    ('08:00', '09:00', float('inf'), 60),
    ('08:00', '09:00', '15', 45),
    ('08:00', '09:00', 15, 45),
])
def test_compute_minutes(start, end, pause, minutes):
    assert compute_minutes(start, end, pause) == minutes


@pytest.mark.parametrize('start, end', [
    ('', ''), ('08:00', ''), ('', '16:00'), (None, '16:00'), ('08:00', None), (None, None),
    ('25:00', '16:00'), ('08:00', '12:60'), ('ab:cd', '16:00'), ('12', '16:00'), ('08:00', '16'),
    ('٠٨:٠٠', '16:00'), ('08:00', '１６:００'),
])
def test_an_empty_or_invalid_time_means_no_minutes_and_no_midnight(start, end):
    assert compute_minutes(start, end, 0) == 0
    assert compute_minutes(start, end, 30) == 0
    assert compute_hours(start, end, 0) == 0.0
    assert crosses_midnight(start, end) is False


@pytest.mark.parametrize('start, end, expected', [
    ('22:00', '02:00', True), ('08:01', '08:00', True), ('23:59', '00:00', True),
    ('08:00', '16:00', False), ('08:00', '08:00', False), ('00:00', '23:59', False),
])
def test_crosses_midnight(start, end, expected):
    assert crosses_midnight(start, end) is expected


# ── Stunden ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('minutes, text', [
    (0, '0,00'), (1, '0,02'), (3, '0,05'), (20, '0,33'), (30, '0,50'), (40, '0,67'), (59, '0,98'),
    (60, '1,00'), (61, '1,02'), (90, '1,50'), (210, '3,50'), (600, '10,00'), (2590, '43,17'),
    (6000, '100,00'), (59999, '999,98'), (60000, '1000,00'),
])
def test_format_hours_rounds_commercially_from_the_whole_minutes(minutes, text):
    assert format_hours(minutes) == text


def test_a_row_and_a_sum_of_the_same_minutes_show_the_same_text():
    for minutes in range(24 * 60):
        end = f'{minutes // 60:02d}:{minutes % 60:02d}'
        assert f"{compute_hours('00:00', end, 0):.2f}".replace('.', ',') == format_hours(minutes), minutes


# ── Die Seite: Summen aus Minuten ─────────────────────────────────────────────
def test_three_entries_of_twenty_minutes_sum_to_one_hour_while_each_row_shows_0_33(tmp_db):
    person = _person(tmp_db)
    ids = [tmp_db.insert_worktime(person, f'2026-03-{day:02d}', start_time='08:00', end_time='08:20')
           for day in (2, 3, 4)]
    html = _page(tmp_db, person)
    assert [_row(html, entry)[5] for entry in ids] == ['0,33', '0,33', '0,33']
    assert _sum_row(html) == ['Summe (3 Arbeitstage):', '1,00', '']


def test_the_sum_of_a_period_over_several_months_is_made_of_minutes_as_well(tmp_db):
    person = _person(tmp_db)
    for day in ('2026-03-30', '2026-04-02', '2026-05-04'):
        tmp_db.insert_worktime(person, day, start_time='08:00', end_time='08:20')
    html = _page(tmp_db, person, '2026-03-01', '2026-05-31')
    assert _sum_row(html)[:2] == ['Summe (3 Arbeitstage):', '1,00']


def test_an_entry_over_midnight_counts_to_its_start_day_and_is_marked(tmp_db):
    person = _person(tmp_db)
    night = tmp_db.insert_worktime(person, '2026-03-10', start_time='22:00', end_time='02:00', pause_minutes=30)
    day = tmp_db.insert_worktime(person, '2026-03-11', start_time='08:00', end_time='16:00')
    html = _page(tmp_db, person)
    assert _row(html, night)[1:6] == ['10', '22:00', '02:00 (+1)', '30', '3,50']
    assert _row(html, day)[1:6] == ['11', '08:00', '16:00', '0', '8,00']
    assert html.count('(+1)') == 1
    assert 'data-end="02:00"' in html and 'data-end="02:00 (+1)"' not in html     # das Formular bekommt die echte Endzeit
    assert _sum_row(html)[:2] == ['Summe (2 Arbeitstage):', '11,50']


def test_workdays_are_calendar_days_with_work_over_zero_minutes_not_entries(tmp_db):
    person = _person(tmp_db)
    tmp_db.insert_worktime(person, '2026-03-10', start_time='08:00', end_time='10:00')
    tmp_db.insert_worktime(person, '2026-03-10', start_time='11:00', end_time='12:00')     # derselbe Tag
    tmp_db.insert_worktime(person, '2026-03-11', start_time='08:00', end_time='08:00')     # 0 Minuten
    tmp_db.insert_worktime(person, '2026-03-12', kind='vacation')
    tmp_db.insert_worktime(person, '2026-03-13', start_time='22:00', end_time='06:00')
    html = _page(tmp_db, person)
    assert _sum_row(html)[:2] == ['Summe (2 Arbeitstage):', '11,00']       # 120 + 60 + 480 Minuten an zwei Tagen


def test_two_entries_on_one_day_are_one_workday_in_a_period_over_several_months(tmp_db):
    person = _person(tmp_db)
    tmp_db.insert_worktime(person, '2026-03-10', start_time='08:00', end_time='10:00')
    tmp_db.insert_worktime(person, '2026-03-10', start_time='11:00', end_time='12:00')
    html = _page(tmp_db, person, '2026-03-01', '2026-04-30')
    assert _sum_row(html)[:2] == ['Summe (1 Arbeitstag):', '3,00']


def test_a_page_without_entries_sums_to_zero(tmp_db):
    person = _person(tmp_db)
    assert _sum_row(_page(tmp_db, person)) == ['Summe (0 Arbeitstage):', '0,00', '']


def test_a_stored_time_is_escaped_in_the_row_and_in_the_data_attributes(tmp_db):
    person = _person(tmp_db)
    tmp_db.insert_worktime(person, '2026-03-10', start_time='<b>8', end_time='"><i>16')
    html = _page(tmp_db, person)
    assert '<b>8' not in html and '"><i>16' not in html
    assert '&lt;b&gt;8' in html and '&quot;&gt;&lt;i&gt;16' in html


# ── Speichern: eine ungültige Uhrzeit wird abgelehnt ──────────────────────────
def _post(person, **fields):
    form = {'person_id': [str(person)], 'from': ['2026-03-01'], 'to': ['2026-03-31'], 'id': [''],
            'date': ['2026-03-15'], 'kind': ['work'], 'customer_id': [''], 'start_time': ['08:00'],
            'end_time': ['16:00'], 'pause_minutes': ['30'], 'location_mode': ['other'],
            'location_city': ['Irgendwo'], 'note': ['']}
    form.update({key: [str(value)] for key, value in fields.items()})
    return form


def _error(location):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).get('error')


def _entries(db, person):
    return [(row[2], row[5], row[6]) for row in db.fetch_worktimes(person)]


INVALID_TIMES = ['25:00', '24:00', '12:60', 'ab:cd', '12', '8:5', '08:00:00', ' 08:00', '08:00 ', '-1:00',
                 '٠٨:٠٠', '０８:００', '<b>']


@pytest.mark.parametrize('field', ['start_time', 'end_time'])
@pytest.mark.parametrize('value', INVALID_TIMES)
def test_adding_an_entry_with_an_invalid_time_is_refused_and_nothing_is_saved(tmp_db, field, value):
    person = _person(tmp_db)
    status, location = handle_add_worktime(tmp_db, _post(person, **{field: value}))
    assert status == 303
    assert location.startswith(f'/worktime?person={person}&from=2026-03-01&to=2026-03-31&error=')
    assert _error(location) == [TIME_ERROR]
    assert _entries(tmp_db, person) == []


@pytest.mark.parametrize('field', ['start_time', 'end_time'])
@pytest.mark.parametrize('value', ['25:00', 'ab:cd', '٠٨:٠٠', '08:00:00'])
def test_updating_an_entry_with_an_invalid_time_is_refused_and_the_entry_stays(tmp_db, field, value):
    person = _person(tmp_db)
    entry = tmp_db.insert_worktime(person, '2026-03-15', start_time='08:00', end_time='16:00', pause_minutes=30)
    status, location = handle_update_worktime(tmp_db, _post(person, id=entry, note='geändert', **{field: value}))
    assert status == 303 and _error(location) == [TIME_ERROR]
    stored = tmp_db.get_worktime_by_id(entry)
    assert (stored[5], stored[6], stored[10]) == ('08:00', '16:00', '')          # Zeiten und Notiz wie vorher


@pytest.mark.parametrize('start, end', [
    ('8:00', '16:00'), ('08:00', '16:00'), ('22:00', '02:00'), ('', ''), ('08:00', ''), ('', '16:00'),
])
def test_empty_and_valid_times_are_saved(tmp_db, start, end):
    person = _person(tmp_db)
    status, location = handle_add_worktime(tmp_db, _post(person, start_time=start, end_time=end))
    assert status == 303 and _error(location) is None
    assert _entries(tmp_db, person) == [('2026-03-15', start, end)]


def test_a_day_off_without_times_is_saved(tmp_db):
    person = _person(tmp_db)
    status, location = handle_add_worktime(tmp_db, _post(person, kind='vacation', start_time='', end_time=''))
    assert status == 303 and _error(location) is None
    assert _entries(tmp_db, person) == [('2026-03-15', '', '')]


def test_the_refusal_reaches_the_page_as_a_message_and_saves_nothing(server_port, auto_login):
    db = auto_login.db
    person = db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                               first_name='Erika', last_name='Probe')
    form = {'csrf': auto_login.csrf, **{key: value[0] for key, value in _post(person).items()}, 'end_time': '25:00'}
    resp = post(server_port, '/worktime/add', form=form)
    assert resp.status == 303
    page = get(server_port, resp.headers['Location'])
    assert page.status == 200
    assert app_msg_arguments(Dom(page.text)) == [TIME_ERROR, 'error']
    assert db.fetch_worktimes(person) == []
    # Gegenprobe: dieselbe Anfrage mit gültiger Zeit speichert und zeigt keine Meldung
    resp = post(server_port, '/worktime/add', form={**form, 'end_time': '16:00'})
    assert resp.status == 303 and 'error=' not in resp.headers['Location']
    assert len(db.fetch_worktimes(person)) == 1


# ── Überschneidungssperre mit einem Eintrag über Mitternacht ──────────────────
def _overlap_refused(tmp_db, person, **fields):
    status, location = handle_add_worktime(tmp_db, _post(person, **fields))
    assert status == 303
    return bool(_error(location)) and 'Überschneidung' in _error(location)[0]


def test_an_entry_over_midnight_blocks_a_later_entry_on_its_start_day(tmp_db):
    person = _person(tmp_db)
    assert not _overlap_refused(tmp_db, person, start_time='22:00', end_time='02:00')
    assert _overlap_refused(tmp_db, person, start_time='23:00', end_time='23:30')        # liegt darin
    assert _overlap_refused(tmp_db, person, start_time='21:00', end_time='22:30')        # ragt hinein
    assert len(_entries(tmp_db, person)) == 1


def test_a_new_entry_over_midnight_collides_with_an_evening_entry_of_its_start_day(tmp_db):
    person = _person(tmp_db)
    assert not _overlap_refused(tmp_db, person, start_time='20:00', end_time='23:00')
    assert _overlap_refused(tmp_db, person, start_time='22:00', end_time='02:00')
    assert len(_entries(tmp_db, person)) == 1


def test_an_entry_over_midnight_does_not_block_the_morning_or_the_day_of_its_start_date(tmp_db):
    person = _person(tmp_db)
    assert not _overlap_refused(tmp_db, person, start_time='22:00', end_time='02:00')
    assert not _overlap_refused(tmp_db, person, start_time='01:00', end_time='03:00')    # 01:00 desselben Datums, nicht des Folgetags
    assert not _overlap_refused(tmp_db, person, start_time='08:00', end_time='16:00')
    assert not _overlap_refused(tmp_db, person, start_time='16:00', end_time='22:00')    # grenzt an
    assert len(_entries(tmp_db, person)) == 4


def test_adjacent_entries_over_midnight_do_not_block_each_other(tmp_db):
    person = _person(tmp_db)
    assert not _overlap_refused(tmp_db, person, start_time='22:00', end_time='02:00')
    assert not _overlap_refused(tmp_db, person, start_time='02:00', end_time='06:00')
    assert len(_entries(tmp_db, person)) == 2


# ── Die Stundenanzeige der Maske (JavaScript) ─────────────────────────────────
NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die statische Prüfung '
                                                  '(test_the_preview_script_knows_the_next_day_rule) deckt es ab')

RUNNER = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const out = [];
for (const [start, end, pause] of JSON.parse(process.argv[3])) {
  const elements = {wtStart: {value: start}, wtEnd: {value: end}, wtPause: {value: pause},
                    wtHoursPreview: {textContent: ''}};
  vm.runInNewContext(source + '\nwtUpdateHours();', {document: {getElementById: id => elements[id]}});
  out.push(elements.wtHoursPreview.textContent);
}
console.log(JSON.stringify(out));
"""


def _preview_script(db, person):
    script = next(s for s in Dom(_page(db, person)).scripts if 'function wtUpdateHours()' in s)
    return script[script.index('function wtClockMinutes'):script.index('function wtPauseMinutesFromText')]


def test_the_preview_script_knows_the_next_day_rule(tmp_db):
    script = _preview_script(tmp_db, _person(tmp_db))
    assert 'function wtClockMinutes(' in script and '1440' in script
    assert 'if (min < 0) min = 0;' not in script                        # der alte Weg: Ende vor Beginn = 0


def _run_preview(script, scenarios, tmp_path):
    source = tmp_path / 'preview.js'
    source.write_text(script, encoding='utf-8')
    runner = tmp_path / 'run.js'
    runner.write_text(RUNNER, encoding='utf-8')
    done = subprocess.run([NODE, str(runner), str(source), json.dumps(scenarios)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@NO_NODE
@pytest.mark.parametrize('start, end, pause, shown', [
    ('08:00', '16:00', '0', '8,00'),
    ('08:00', '08:20', '0', '0,33'),
    ('22:00', '02:00', '30', '3,50'),
    ('17:00', '08:00', '0', '15,00'),
    ('08:00', '08:00', '0', '0,00'),
    ('08:00', '09:00', '120', '0,00'),
    ('08:00', '09:00', '-30', '1,00'),
    ('08:00', '09:00', '', '1,00'),
    ('', '16:00', '0', '0,00'),
    ('08:00', '', '0', '0,00'),
    ('25:00', '16:00', '0', '0,00'),
])
def test_the_preview_shows_the_hours_of_the_server_rule(tmp_db, tmp_path, start, end, pause, shown):
    script = _preview_script(tmp_db, _person(tmp_db))
    assert _run_preview(script, [[start, end, pause]], tmp_path) == [shown]
    assert format_hours(compute_minutes(start, end, int(pause or 0))) == shown


@NO_NODE
def test_the_preview_and_the_server_agree_on_many_times(tmp_db, tmp_path):
    rng = random.Random(20260101)
    scenarios = [[f'{rng.randrange(24):02d}:{rng.randrange(60):02d}', f'{rng.randrange(24):02d}:{rng.randrange(60):02d}',
                  str(rng.choice([0, 0, 15, 30, 45, 60, 600]))] for _ in range(300)]
    shown = _run_preview(_preview_script(tmp_db, _person(tmp_db)), scenarios, tmp_path)
    assert shown == [format_hours(compute_minutes(start, end, int(pause))) for start, end, pause in scenarios]
