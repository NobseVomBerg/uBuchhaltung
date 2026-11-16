# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Fahrtenbuch: Lückenprüfung auf Wunsch, je Fahrzeug und Jahr (rein lesend).

* ``db.trips.logbook_gaps`` (reine Rechnung, ohne Datenbank): lückenlos, Lücke, Überschneidung, Fahrten ohne Tachostand
  dazwischen, nur Fahrten ohne Tachostand, leere Liste, mehr als 100 Lücken, mehrere Fahrer, Reihenfolge.
* ``Database.trip_logbook_check`` / ``get_all_vehicles``: welche Fahrten zählen (alle Fahrer, Kalenderjahr, Fahrzeug
  nach ``strip()`` + ``casefold()``).
* Die Seite: der Hinweistext steht immer da, das Ergebnis nur nach einer Anfrage; ein ungültiges Jahr oder ein zu
  langer Fahrzeugname heißt „keine Prüfung“ und ist kein Fehler; alles Eingesetzte ist maskiert; die Prüfung schreibt
  nichts.

Alle Namen, Kennzeichen und Zahlen sind erfunden.
"""
import random
import re
import urllib.parse

import pytest

from db.trips import LOGBOOK_MAX_GAPS, logbook_gaps
from server.pages_trips import PageTrips
from tests.dom_helpers import Dom, assert_same_structure
from tests.http_client import get
from tests.installation import dump

HINT = ('Lückenlos muss ein Fahrtenbuch nur sein, wenn die private Nutzung nach der Fahrtenbuchmethode ermittelt wird '
        '– dann für das ganze Jahr (bzw. ab der Anschaffung oder bis zum Verkauf des Fahrzeugs). Für die pauschale '
        'Versteuerung (1 %-Regelung, bei Elektrofahrzeugen 0,25 %), die Abrechnung nach Kilometerpauschale, anderweitig '
        'abgerechnete Fahrten oder als Gedächtnisstütze genügen einzelne Fahrten. Die Prüfung hier ist ein Hilfsmittel, '
        'keine Steuerberatung.')
NO_GAPS = 'Keine Lücken zwischen den Tachoständen.'
NO_TRIPS = 'Keine Fahrten mit diesem Fahrzeug im Jahr.'
PERIOD = ('2026-03-01', '2026-03-31')


def trip(id, date, start=None, end=None, km=None, driver=1, time=''):
    """Eine Fahrt, wie ``logbook_gaps`` sie liest (Spalten der Tabelle ``Trips``)."""
    return {'ID': id, 'DriverID': driver, 'StartDate': date, 'StartTime': time,
            'DistanceKm': km, 'StartKm': start, 'EndKm': end}


def gap(prev, nxt, diff, between=0, prev_driver=1, next_driver=1, prev_date='2026-01-02', next_date='2026-01-03',
        prev_end=150, next_start=None):
    return {'prev_id': prev, 'prev_driver': prev_driver, 'prev_date': prev_date, 'prev_end_km': prev_end,
            'next_id': nxt, 'next_driver': next_driver, 'next_date': next_date,
            'next_start_km': prev_end + diff if next_start is None else next_start,
            'diff': diff, 'between': between}


# ── logbook_gaps ─────────────────────────────────────────────────────────────

def test_a_gapless_logbook_has_no_gaps():
    result = logbook_gaps([trip(1, '2026-01-02', 100, 150, 50), trip(2, '2026-01-03', 150, 210, 60)])
    assert result == {'trips': 2, 'total_km': 110, 'without_odometer': 0, 'first_start_km': 100,
                      'last_end_km': 210, 'gaps': [], 'gaps_count': 0}


def test_a_gap_is_the_difference_to_the_end_of_the_previous_trip():
    result = logbook_gaps([trip(1, '2026-01-02', 100, 150), trip(2, '2026-01-03', 160, 200)])
    assert result['gaps'] == [gap(1, 2, +10)] and result['gaps_count'] == 1


def test_an_overlap_is_a_negative_difference():
    result = logbook_gaps([trip(1, '2026-01-02', 100, 150), trip(2, '2026-01-03', 140, 200)])
    assert result['gaps'] == [gap(1, 2, -10)] and result['gaps_count'] == 1


def test_trips_without_an_odometer_in_between_are_counted_not_reported_as_gaps():
    rows = [trip(1, '2026-01-02', 100, 150), trip(2, '2026-01-03'), trip(3, '2026-01-04'),
            trip(4, '2026-01-05', 160, 200, driver=2)]
    result = logbook_gaps(rows)
    assert result['gaps'] == [gap(1, 4, +10, between=2, next_driver=2, next_date='2026-01-05')]
    assert result['without_odometer'] == 2 and result['trips'] == 4


def test_a_trip_with_a_start_but_no_end_is_compared_and_leaves_the_reading_unknown():
    """Ein Start ohne Ende (155) wird noch gegen das Ende 150 gelesen. Danach ist der Stand unbekannt – die Strecke
    dieser Fahrt fehlt –, und der Start danach (160) hat keinen Vorgänger: Dieselben Kilometer stehen nicht zweimal da."""
    rows = [trip(1, '2026-01-02', 100, 150), trip(2, '2026-01-03', 155), trip(3, '2026-01-04', 160, 200),
            trip(4, '2026-01-05', 210, 250)]
    result = logbook_gaps(rows)
    assert result['gaps'] == [gap(1, 2, +5, next_date='2026-01-03'),
                              gap(3, 4, +10, prev_date='2026-01-04', next_date='2026-01-05', prev_end=200)]
    assert result['without_odometer'] == 1


def test_a_trip_that_starts_where_the_last_known_end_was_is_no_gap_even_with_trips_between():
    rows = [trip(1, '2026-01-02', 100, 150), trip(2, '2026-01-03'), trip(3, '2026-01-04', 150, 200)]
    result = logbook_gaps(rows)
    assert result['gaps'] == [] and result['gaps_count'] == 0 and result['without_odometer'] == 1


def test_only_trips_without_an_odometer_have_no_gaps_and_no_readings():
    result = logbook_gaps([trip(1, '2026-01-02', km=30), trip(2, '2026-01-03', km=12), trip(3, '2026-01-04')])
    assert result == {'trips': 3, 'total_km': 42, 'without_odometer': 3, 'first_start_km': None,
                      'last_end_km': None, 'gaps': [], 'gaps_count': 0}


def test_an_empty_list_gives_an_empty_result():
    assert logbook_gaps([]) == {'trips': 0, 'total_km': 0, 'without_odometer': 0, 'first_start_km': None,
                                'last_end_km': None, 'gaps': [], 'gaps_count': 0}


def test_the_first_known_start_and_the_last_known_end_are_reported_even_from_incomplete_trips():
    rows = [trip(1, '2026-01-02', None, 90), trip(2, '2026-01-03', 95, None), trip(3, '2026-01-04', 100, None),
            trip(4, '2026-01-05', None, None)]
    result = logbook_gaps(rows)
    assert (result['first_start_km'], result['last_end_km']) == (95, 90)
    assert result['without_odometer'] == 4


def test_more_than_100_gaps_are_cut_to_100_and_counted():
    rows = [trip(n, '2026-01-01', start=100 * n + 60, end=100 * n + 90) for n in range(1, 111)]   # 109 Lücken von +70
    result = logbook_gaps(rows)
    assert result['gaps_count'] == 109 and len(result['gaps']) == LOGBOOK_MAX_GAPS == 100
    assert result['gaps'][0]['prev_id'] == 1 and result['gaps'][0]['next_id'] == 2
    assert result['gaps'][-1]['next_id'] == 101                    # die ersten 100 in der Reihenfolge der Fahrten
    assert {g['diff'] for g in result['gaps']} == {70}


def test_exactly_100_gaps_are_all_reported():
    rows = [trip(n, '2026-01-01', start=100 * n + 60, end=100 * n + 90) for n in range(1, 102)]
    result = logbook_gaps(rows)
    assert result['gaps_count'] == 100 == len(result['gaps'])


def test_the_gap_names_both_drivers_and_both_dates():
    rows = [trip(7, '2026-02-01', 1000, 1100, driver=3), trip(9, '2026-02-02', 1130, 1200, driver=5)]
    (entry,) = logbook_gaps(rows)['gaps']
    assert entry == {'prev_id': 7, 'prev_driver': 3, 'prev_date': '2026-02-01', 'prev_end_km': 1100,
                     'next_id': 9, 'next_driver': 5, 'next_date': '2026-02-02', 'next_start_km': 1130,
                     'diff': 30, 'between': 0}


def test_the_order_is_date_then_time_with_empty_first_then_id():
    # gleicher Tag: ohne Uhrzeit zuerst, dann 07:00, dann 08:00 – eingegeben in beliebiger Reihenfolge
    rows = [trip(1, '2026-01-02', 150, 200, time='08:00'), trip(2, '2026-01-02', 100, 120, time=''),
            trip(3, '2026-01-02', 120, 150, time='07:00')]
    assert logbook_gaps(rows)['gaps_count'] == 0
    # gleiches Datum und gleiche Uhrzeit: die ID entscheidet
    rows = [trip(5, '2026-01-02', 120, 150), trip(4, '2026-01-02', 100, 120)]
    assert logbook_gaps(rows)['gaps_count'] == 0
    # eine fehlende Uhrzeit (NULL) zählt wie eine leere
    rows = [trip(2, '2026-01-02', 120, 150, time='08:00'), {**trip(1, '2026-01-02', 100, 120), 'StartTime': None}]
    assert logbook_gaps(rows)['gaps_count'] == 0


def test_the_result_does_not_depend_on_the_order_of_the_input():
    rows = [trip(n, f'2026-01-{n % 27 + 1:02d}', start=(n * 37) % 500, end=(n * 37) % 500 + 20) for n in range(1, 60)]
    expected = logbook_gaps(rows)
    for seed in range(5):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        assert logbook_gaps(shuffled) == expected


def test_a_reading_that_is_not_a_whole_number_counts_as_missing():
    """Ein Eingriff von Hand (SQL-Konsole) kann Text oder einen Wahrheitswert in die Spalten geschrieben haben."""
    rows = [trip(1, '2026-01-02', 'abc', 150, 'x'), trip(2, '2026-01-03', True, 210, 20)]
    result = logbook_gaps(rows)
    assert result['without_odometer'] == 2 and result['total_km'] == 20
    assert result['first_start_km'] is None and result['last_end_km'] == 210 and result['gaps'] == []


def test_the_input_is_not_changed():
    rows = [trip(2, '2026-01-03', 160, 200), trip(1, '2026-01-02', 100, 150)]
    before = [dict(r) for r in rows]
    logbook_gaps(rows)
    assert rows == before


# ── Database: welche Fahrten zählen ──────────────────────────────────────────

def _driver(db, name):
    db.insert_contact(contact_type='own', entity_type='person', display_name=name,
                      first_name=name.split()[0], last_name=name.split()[-1])
    return next(c[0] for c in db.fetch_contacts(contact_type='own') if c[3] == name)


@pytest.fixture
def drivers(tmp_db):
    return _driver(tmp_db, 'Erika Probe'), _driver(tmp_db, 'Max Muster')


def _add(db, driver, date, vehicle, start=None, end=None, km=None):
    return db.insert_trip(driver, date, destination='Ziel', vehicle=vehicle, start_km=start, end_km=end,
                          distance_km=km)


def test_only_trips_of_the_calendar_year_count(tmp_db, drivers):
    erika, _ = drivers
    for date in ('2025-12-31', '2026-01-01', '2026-06-15', '2026-12-31', '2027-01-01'):
        _add(tmp_db, erika, date, 'B-XY 1', km=10)
    result = tmp_db.trip_logbook_check('B-XY 1', 2026)
    assert (result['trips'], result['total_km']) == (3, 30)
    assert tmp_db.trip_logbook_check('B-XY 1', 2025)['trips'] == 1
    assert tmp_db.trip_logbook_check('B-XY 1', 2027)['trips'] == 1
    assert tmp_db.trip_logbook_check('B-XY 1', 2024)['trips'] == 0


def test_a_start_date_with_a_time_part_on_the_last_day_still_counts(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-12-31 23:00', 'B-XY 1', km=10)
    assert tmp_db.trip_logbook_check('B-XY 1', 2026)['trips'] == 1


def test_all_drivers_count_together(tmp_db, drivers):
    erika, max_ = drivers
    _add(tmp_db, erika, '2026-01-02', 'B-XY 1', 100, 150)
    _add(tmp_db, max_, '2026-01-03', 'B-XY 1', 170, 200)
    result = tmp_db.trip_logbook_check('B-XY 1', 2026)
    (entry,) = result['gaps']
    assert result['trips'] == 2 and (entry['prev_driver'], entry['next_driver']) == (erika, max_)


@pytest.mark.parametrize('stored, asked', [
    ('B-XY 1', 'B-XY 1'), ('b-xy 1', 'B-XY 1'), ('  B-XY 1 ', 'B-XY 1'), ('B-XY 1', ' b-xy 1\t'),
    ('Straße 7', 'STRASSE 7'), ('STRASSE 7', 'straße 7'),                     # ß und ss sind für casefold gleich
    ('Ärmel-Bus', 'ärmel-bus'), ('ÄRMEL-BUS', 'Ärmel-Bus'),
])
def test_the_vehicle_is_compared_trimmed_and_case_insensitively_in_python(tmp_db, drivers, stored, asked):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-01-02', stored, km=5)
    assert tmp_db.trip_logbook_check(asked, 2026)['trips'] == 1


def test_another_vehicle_and_trips_without_a_vehicle_do_not_count(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-01-02', 'B-XY 1', km=5)
    _add(tmp_db, erika, '2026-01-03', 'B-XY 2', km=6)
    _add(tmp_db, erika, '2026-01-04', '', km=7)
    _add(tmp_db, erika, '2026-01-05', None, km=8)
    assert tmp_db.trip_logbook_check('B-XY 1', 2026)['total_km'] == 5
    assert tmp_db.trip_logbook_check('B-XY 2', 2026)['total_km'] == 6
    assert tmp_db.trip_logbook_check('B-XY', 2026)['trips'] == 0                 # nur das ganze Fahrzeug, kein Teilstück


def test_the_check_reads_the_trips_in_date_order_whatever_the_order_of_entry(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-01-04', 'B-XY 1', 200, 250)                        # zuerst eingetragen
    _add(tmp_db, erika, '2026-01-02', 'B-XY 1', 100, 150)
    _add(tmp_db, erika, '2026-01-03', 'B-XY 1', 150, 200)
    assert tmp_db.trip_logbook_check('B-XY 1', 2026)['gaps_count'] == 0


def test_the_check_writes_nothing(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-01-02', 'B-XY 1', 100, 150)
    _add(tmp_db, erika, '2026-01-03', 'B-XY 1', 170, 200)
    before = dump(tmp_db.db_name)
    tmp_db.trip_logbook_check('B-XY 1', 2026)
    tmp_db.get_all_vehicles()
    assert dump(tmp_db.db_name) == before


def test_all_vehicles_are_trimmed_deduplicated_by_casefold_and_sorted(tmp_db, drivers):
    erika, max_ = drivers
    assert tmp_db.get_all_vehicles() == []
    for driver, vehicle in [(erika, 'B-XY 2'), (max_, 'b-xy 2'), (erika, ' B-XY 1 '), (max_, 'Straße 7'),
                            (erika, 'STRASSE 7'), (erika, ''), (max_, '   '), (erika, '\n'), (max_, None),
                            (erika, 'ärmel-Bus'), (max_, 'Ärmel-Bus')]:
        _add(tmp_db, driver, '2026-01-02', vehicle)
    vehicles = tmp_db.get_all_vehicles()
    assert [v.casefold() for v in vehicles] == ['b-xy 1', 'b-xy 2', 'strasse 7', 'ärmel-bus']   # nach Zeichen sortiert
    assert 'B-XY 1' in vehicles                                                  # getrimmt
    assert len(vehicles) == 4 and all(v == v.strip() for v in vehicles)


# ── Die Seite ────────────────────────────────────────────────────────────────

def _page(db, driver, **kwargs):
    return PageTrips(db, person_id=driver, date_from=PERIOD[0], date_to=PERIOD[1], **kwargs)


def _text(html):
    """Der Text der Seite ohne Skriptblöcke (Leerraum zusammengezogen)."""
    return ' '.join(re.sub(r'<script\b.*?</script>', ' ', html, flags=re.S).split())


def _hidden(dom, name):
    """Werte der versteckten Felder ``name`` in der Reihenfolge der Seite."""
    return [a.get('value') for t, a in dom.tags if t == 'input' and a.get('type') == 'hidden' and a.get('name') == name]


def _select_options(html):
    """Die Auswahl des Prüfformulars: [(Wert, gewählt)] – nicht die Vorschläge der Fahrtenmaske (``datalist``)."""
    block = re.search(r'<select name="check_vehicle">(.*?)</select>', html, re.S).group(1)
    return [(Dom(f'<p>{tag}</p>').tags[1][1]['value'], ' selected' in tag) for tag in re.findall(r'<option[^>]*>', block)]


def test_the_hint_is_always_there(tmp_db, drivers):
    erika, _ = drivers
    assert HINT in _page(tmp_db, erika)                                          # ohne Fahrzeuge
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100, 150)
    assert HINT in _page(tmp_db, erika)                                          # mit Fahrzeugen, ohne Prüfung
    assert HINT in _page(tmp_db, erika, check_vehicle='B-XY 1', check_year=2026)  # mit Ergebnis
    assert 'Fahrtenbuch auf Lücken prüfen' in _page(tmp_db, erika)


def test_without_any_vehicle_there_is_only_the_hint_and_no_form(tmp_db, drivers):
    erika, _ = drivers
    tmp_db.insert_trip(erika, '2026-03-02', destination='Kiel', distance_km=10)
    dom = Dom(_page(tmp_db, erika))
    assert not [a for t, a in dom.tags if t == 'select' and a.get('name') == 'check_vehicle']
    assert not [a for t, a in dom.tags if t == 'input' and a.get('name') == 'check_year']


def test_with_vehicles_the_form_is_a_get_form_with_person_period_vehicle_and_year(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100, 150)
    _add(tmp_db, erika, '2026-03-03', 'a-ab 9', 150, 160)
    html = _page(tmp_db, erika)
    dom = Dom(html)

    form = dom.one('form', action='/trips')
    assert form['method'].upper() == 'GET'
    assert _hidden(dom, 'person') == [str(erika)]                                # nur das Prüfformular nennt „person“
    assert _hidden(dom, 'from') == [PERIOD[0]] * 2 and _hidden(dom, 'to') == [PERIOD[1]] * 2   # und das Formular der Fahrt
    assert dom.one('input', name='check_year')['value'] == '2026'                # das Jahr von „bis“
    assert dom.one('input', name='check_year')['type'] == 'number'
    assert _select_options(html) == [('a-ab 9', False), ('B-XY 1', False)]       # sortiert ohne Groß-/Kleinschreibung
    assert dom.one('input', type='submit', value='Prüfen')


def test_the_year_is_prefilled_from_the_end_of_the_period(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2025-03-02', 'B-XY 1')
    html = PageTrips(tmp_db, person_id=erika, date_from='2025-01-01', date_to='2025-12-31')
    assert Dom(html).one('input', name='check_year')['value'] == '2025'


@pytest.mark.parametrize('date_to', ['"><img src=x onerror=alert(1)>', 'x', '', '20', '２０２６-０３-３１', '1800-01-01',
                                     '9999-01-01'])
def test_a_hostile_end_of_period_gives_a_plausible_year(tmp_db, drivers, date_to):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1')
    html = PageTrips(tmp_db, person_id=erika, date_from='2026-03-01', date_to=date_to)
    year = Dom(html).one('input', name='check_year')['value']
    assert year.isascii() and year.isdigit() and 1900 <= int(year) <= 2999
    assert '<img src=x' not in html
    assert _select_options(html) == [('B-XY 1', False)]


def test_the_result_appears_only_after_a_request(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100, 150)
    plain = _text(_page(tmp_db, erika))
    for needle in (NO_GAPS, NO_TRIPS, 'im Jahr', 'zusammen', 'Tachostand:'):
        assert needle not in plain, needle
    asked = _text(_page(tmp_db, erika, check_vehicle='B-XY 1', check_year=2026))
    assert NO_GAPS in asked and 'B-XY 1 im Jahr 2026' in asked


@pytest.mark.parametrize('kwargs', [{'check_vehicle': 'B-XY 1'}, {'check_year': 2026},
                                    {'check_vehicle': '', 'check_year': 2026}, {'check_vehicle': 'B-XY 1', 'check_year': 0}])
def test_a_half_request_checks_nothing(tmp_db, drivers, kwargs):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100, 150)
    assert _page(tmp_db, erika, **kwargs) == _page(tmp_db, erika)


def test_a_gapless_year_says_so_with_the_summary(tmp_db, drivers):
    erika, max_ = drivers
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100, 150, km=50)
    _add(tmp_db, max_, '2026-03-03', 'b-xy 1', 150, 210, km=60)
    text = _text(_page(tmp_db, erika, check_vehicle='B-XY 1', check_year=2026))
    assert 'B-XY 1 im Jahr 2026' in text
    assert '2 Fahrten, zusammen 110 km.' in text
    assert 'Fahrten ohne vollständigen Tachostand: 0' in text
    assert 'Erster Tachostand: 100 km, letzter Tachostand: 210 km' in text
    assert NO_GAPS in text and NO_TRIPS not in text


def test_trips_without_a_complete_odometer_are_named_instead_of_claiming_no_gaps(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100, 150)
    _add(tmp_db, erika, '2026-03-03', 'B-XY 1', km=30)
    _add(tmp_db, erika, '2026-03-04', 'B-XY 1', 150, 190)
    _add(tmp_db, erika, '2026-03-05', 'B-XY 1', start=190)
    text = _text(_page(tmp_db, erika, check_vehicle='B-XY 1', check_year=2026))
    assert ('Zwischen den Fahrten mit Tachostand gibt es keine Lücken – 2 Fahrten haben keinen '
            'vollständigen Tachostand.') in text
    assert NO_GAPS not in text


def test_one_trip_without_a_complete_odometer_reads_in_the_singular(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100, 150)
    _add(tmp_db, erika, '2026-03-03', 'B-XY 1', km=30)
    text = _text(_page(tmp_db, erika, check_vehicle='B-XY 1', check_year=2026))
    assert '1 Fahrt hat keinen vollständigen Tachostand.' in text and '1 Fahrten' not in text


def test_a_year_without_trips_of_the_vehicle_says_so(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2025-03-02', 'B-XY 1', 100, 150)
    text = _text(_page(tmp_db, erika, check_vehicle='B-XY 1', check_year=2026))
    assert NO_TRIPS in text and NO_GAPS not in text and '0 Fahrten, zusammen 0 km.' in text
    assert 'Erster Tachostand: –, letzter Tachostand: –' in text


def test_the_gaps_are_listed_with_sign_word_between_and_an_edit_link(tmp_db, drivers):
    erika, max_ = drivers
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100, 150)
    _add(tmp_db, erika, '2026-03-03', 'B-XY 1', km=5)
    later = _add(tmp_db, max_, '2026-03-04', 'B-XY 1', 175, 200)
    overlap = _add(tmp_db, max_, '2026-03-05', 'B-XY 1', 190, 240)
    html = _page(tmp_db, erika, check_vehicle='B-XY 1', check_year=2026)
    text = _text(html)

    assert NO_GAPS not in text and 'Keine Fahrten' not in text
    assert '02.03.2026' in text and '04.03.2026' in text and '05.03.2026' in text
    assert '+25 km fehlen' in text and '-10 km Überschneidung' in text
    assert 'dazwischen 1 Fahrt ohne Tachostand' in text
    links = [a['href'] for t, a in Dom(html).tags if t == 'a' and a.get('href', '').startswith('/trips/edit')]
    assert links == [f'/trips/edit?id={later}&person={max_}&from=2026-03-01&to=2026-03-31',
                     f'/trips/edit?id={overlap}&person={max_}&from=2026-03-01&to=2026-03-31']


def test_more_than_100_gaps_say_how_many_more(tmp_db, drivers):
    erika, _ = drivers
    for n in range(1, 111):
        _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100 * n + 60, 100 * n + 90)
    html = _page(tmp_db, erika, check_vehicle='B-XY 1', check_year=2026)
    assert '… und 9 weitere' in _text(html)
    links = [a for t, a in Dom(html).tags if t == 'a' and a.get('href', '').startswith('/trips/edit?id=')]
    assert len(links) == 100


def test_the_requested_vehicle_is_selected(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-03-02', 'A-AA 1')
    _add(tmp_db, erika, '2026-03-03', 'B-XY 1')
    html = _page(tmp_db, erika, check_vehicle=' b-xy 1 ', check_year=2026)
    assert _select_options(html) == [('A-AA 1', False), ('B-XY 1', True)]
    assert Dom(html).one('input', name='check_year')['value'] == '2026'


def test_a_vehicle_name_with_markup_is_masked_everywhere_it_appears(tmp_db, drivers):
    erika, _ = drivers
    hostile = '</script><img src=x onerror=alert(1)>"\'<b>'
    _add(tmp_db, erika, '2026-03-02', f'Auto {hostile}', 100, 150)
    _add(tmp_db, erika, '2026-03-03', f'Auto {hostile}', 170, 200)
    page = _page(tmp_db, erika, check_vehicle=f'Auto {hostile}', check_year=2026)

    assert hostile not in page and '<img src=x' not in page
    assert _select_options(page) == [(f'Auto {hostile}', True)]                  # unverändert angekommen, nur maskiert
    assert_same_structure(page, _benign_twin(tmp_db, erika))


def _benign_twin(db, driver):
    """Dieselben Fahrten mit einem harmlosen Fahrzeugnamen: Maßstab für den Aufbau der Seite."""
    import sqlite3
    con = sqlite3.connect(db.db_name)
    try:
        con.execute("UPDATE Trips SET Vehicle = 'Auto x'")
        con.commit()
    finally:
        con.close()
    return _page(db, driver, check_vehicle='Auto x', check_year=2026)


def test_a_vehicle_that_is_not_in_the_list_is_still_answered_and_masked(tmp_db, drivers):
    erika, _ = drivers
    _add(tmp_db, erika, '2026-03-02', 'B-XY 1', 100, 150)
    page = _page(tmp_db, erika, check_vehicle='<script>alert(1)</script>', check_year=2026)
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt; im Jahr 2026' in _text(page)
    assert NO_TRIPS in _text(page)


# ── Über den echten Server ───────────────────────────────────────────────────

@pytest.fixture
def books(auto_login):
    db = auto_login.db
    erika = _driver(db, 'Erika Probe')
    _add(db, erika, '2026-03-02', 'B-XY 1', 100, 150, km=50)
    _add(db, erika, '2026-03-04', 'B-XY 1', 175, 200, km=25)
    return erika


def _check_url(**params):
    return '/trips?' + urllib.parse.urlencode({'from': PERIOD[0], 'to': PERIOD[1], **params})


def test_over_http_the_check_runs_for_a_valid_request(server_port, auto_login, books):
    resp = get(server_port, _check_url(person=books, check_vehicle='b-xy 1', check_year='2026'))
    assert resp.status == 200
    text = _text(resp.text)
    assert 'B-XY 1' in text and '2 Fahrten, zusammen 75 km.' in text and '+25 km fehlen' in text


@pytest.mark.parametrize('params', [
    {'check_vehicle': 'B-XY 1', 'check_year': '202'}, {'check_vehicle': 'B-XY 1', 'check_year': '20266'},
    {'check_vehicle': 'B-XY 1', 'check_year': 'abcd'}, {'check_vehicle': 'B-XY 1', 'check_year': '２０２６'},
    {'check_vehicle': 'B-XY 1', 'check_year': '1899'}, {'check_vehicle': 'B-XY 1', 'check_year': '3000'},
    {'check_vehicle': 'B-XY 1', 'check_year': '2026.5'}, {'check_vehicle': 'B-XY 1', 'check_year': ' 2026'},
    {'check_vehicle': 'B-XY 1', 'check_year': '-202'}, {'check_vehicle': 'B-XY 1', 'check_year': '+202'},
    {'check_vehicle': 'B-XY 1', 'check_year': ''}, {'check_vehicle': 'B-XY 1'},
    {'check_vehicle': '', 'check_year': '2026'}, {'check_vehicle': '   ', 'check_year': '2026'},
    {'check_year': '2026'}, {'check_vehicle': 'x' * 501, 'check_year': '2026'},
    {'check_vehicle': ' ' + 'x' * 501, 'check_year': '2026'},
])
def test_over_http_an_invalid_request_checks_nothing_and_is_no_error(server_port, auto_login, books, params):
    plain = get(server_port, _check_url(person=books))
    resp = get(server_port, _check_url(person=books, **params))
    assert resp.status == 200
    assert resp.body == plain.body                                                # dieselbe Seite wie ohne Anfrage


def test_over_http_the_vehicle_is_trimmed_and_500_characters_are_still_answered(server_port, auto_login, books):
    resp = get(server_port, _check_url(person=books, check_vehicle=' ' + 'x' * 500 + ' ', check_year='2026'))
    assert resp.status == 200 and NO_TRIPS in _text(resp.text)
    boundary = get(server_port, _check_url(person=books, check_vehicle='  B-XY 1  ', check_year='1900'))
    assert NO_TRIPS in _text(boundary.text)


def test_over_http_a_hostile_vehicle_in_the_address_is_masked(server_port, auto_login, books):
    hostile = '"><img src=x onerror=alert(1)><script>alert(2)</script>'
    resp = get(server_port, _check_url(person=books, check_vehicle=hostile, check_year='2026'))
    assert resp.status == 200
    assert '<img src=x' not in resp.text and '<script>alert(2)' not in resp.text
    assert hostile.replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;') in resp.text
    assert_same_structure(resp.text, get(server_port, _check_url(person=books, check_vehicle='Auto x',
                                                                  check_year='2026')).text)


def test_over_http_the_check_changes_nothing_in_the_database(server_port, auto_login, books):
    before = dump(auto_login.db.db_name)
    for params in ({'check_vehicle': 'B-XY 1', 'check_year': '2026'}, {'check_vehicle': 'B-XY 1', 'check_year': 'x'},
                   {'check_vehicle': 'x' * 600, 'check_year': '2026'}):
        assert get(server_port, _check_url(person=books, **params)).status == 200
    assert dump(auto_login.db.db_name) == before


def test_over_http_the_edit_link_of_a_gap_opens_that_trip(server_port, auto_login, books):
    page = get(server_port, _check_url(person=books, check_vehicle='B-XY 1', check_year='2026'))
    (link,) = [a['href'] for t, a in Dom(page.text).tags if t == 'a' and a.get('href', '').startswith('/trips/edit')]
    later = auto_login.db.fetch_trips(books)[1][0]
    assert link == f'/trips/edit?id={later}&person={books}&from=2026-03-01&to=2026-03-31'

    edit = get(server_port, link)

    assert edit.status == 200 and 'trLoadEntry(' in edit.text and f'"id": {later}' in edit.text


def test_over_http_the_check_form_keeps_person_and_period(server_port, auto_login, books):
    html = get(server_port, _check_url(person=books)).text
    dom = Dom(html)
    form = dom.one('form', action='/trips')
    assert form['method'].upper() == 'GET'
    values = {(a['name'], a['value']) for t, a in dom.tags
              if t == 'input' and a.get('type') == 'hidden' and a.get('name') in ('person', 'from', 'to')}
    assert {('person', str(books)), ('from', PERIOD[0]), ('to', PERIOD[1])} <= values
