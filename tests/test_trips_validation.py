# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Fahrtenbuch: Beim Speichern wird abgelehnt, was unmöglich ist – und sonst nichts.

* ``_trip_fields`` (eine Stelle für Anlegen und Ändern): fehlender Fahrer/Start-Datum/Ziel, Daten, die keine
  ISO-Daten sind, Ende vor Start, Uhrzeit nicht ``HH:MM``, Kilometer, die keine ganze Zahl ab 0 sind, Tacho Ende unter
  Tacho Start, ein ungültiger Beleg, zu lange Texte. Bei einem Fehler wird nichts gespeichert, die Weiterleitung trägt
  den Klartext als ``error``, und die Seite zeigt ihn nur über ``script_json`` + ``appMsg``.
* Das Fahrtenbuch bleibt locker: Fahrten ohne Tachostand, ohne Anschluss an die Vorfahrt, in beliebiger Reihenfolge
  nachgetragen – das wird ohne jede Meldung gespeichert (die Lückenprüfung gibt es nur auf Wunsch, siehe
  ``test_trips_logbook_check.py``).
* Ein Speicherfehler (Fahrer oder Beleg gibt es nicht, unbekannte ID beim Ändern) wird gemeldet statt verschluckt;
  eine ungültige ``person_id``/``id`` gilt als fehlend und gibt keinen 500.

Alle Namen und Werte sind erfunden.
"""
import re
import urllib.parse

import pytest

from server import handlers
from server.pages_trips import PageTrips, _TRIPS_JS
from tests.dom_helpers import Dom, app_msg_arguments
from tests.http_client import get, own_origin, post

PERIOD = {'from': '2026-03-01', 'to': '2026-03-31'}

REQUIRED = 'Bitte Fahrer, Start-Datum und Ziel angeben.'
START_DATE = 'Bitte ein gültiges Start-Datum angeben.'
END_DATE = 'Bitte ein gültiges End-Datum angeben.'
END_BEFORE_START = 'Das Ende der Fahrt liegt vor dem Start.'
TIME = 'Bitte die Uhrzeit als HH:MM angeben.'
KM = 'Kilometer bitte als ganze Zahl ab 0 angeben.'
TACHO = 'Tacho Ende liegt unter Tacho Start.'
DOCUMENT = 'Der gewählte Beleg ist ungültig.'
TOO_LONG = 'Der Text ist zu lang (höchstens 500 Zeichen).'
NOT_SAVED = 'Die Fahrt konnte nicht gespeichert werden (Fahrer oder Beleg gibt es nicht mehr).'
GONE = 'Die Fahrt gibt es nicht mehr.'


def _driver(db, name='Erika Probe'):
    db.insert_contact(contact_type='own', entity_type='person', display_name=name,
                      first_name=name.split()[0], last_name=name.split()[-1])
    return next(c[0] for c in db.fetch_contacts(contact_type='own') if c[3] == name)


def _form(driver, **fields):
    """Die Felder des Fahrtenformulars (ein gültiger Grundstand, ``None`` entfernt ein Feld)."""
    base = {'person_id': driver, **PERIOD, 'start_date': '2026-03-10', 'destination': 'Hamburg'}
    base.update(fields)
    return {k: [str(v)] for k, v in base.items() if v is not None}


def _error(location):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).get('error')


def _trips(db, driver):
    return [tuple(r) for r in db.fetch_trips(driver)]


@pytest.fixture
def driver(tmp_db):
    return _driver(tmp_db)


# ── Anlegen: jede Ablehnung ──────────────────────────────────────────────────

REJECTIONS = [
    # Pflichtangaben
    pytest.param({'destination': ''}, REQUIRED, id='ziel-leer'),
    pytest.param({'destination': '   '}, REQUIRED, id='ziel-nur-leerraum'),
    pytest.param({'destination': None}, REQUIRED, id='ziel-fehlt'),
    pytest.param({'start_date': ''}, REQUIRED, id='start-datum-leer'),
    pytest.param({'start_date': None}, REQUIRED, id='start-datum-fehlt'),
    pytest.param({'person_id': None}, REQUIRED, id='fahrer-fehlt'),
    pytest.param({'person_id': ''}, REQUIRED, id='fahrer-leer'),
    pytest.param({'person_id': 'abc'}, REQUIRED, id='fahrer-text'),
    pytest.param({'person_id': '0'}, REQUIRED, id='fahrer-null'),
    pytest.param({'person_id': '-3'}, REQUIRED, id='fahrer-negativ'),
    pytest.param({'person_id': '1.5'}, REQUIRED, id='fahrer-dezimal'),
    pytest.param({'person_id': str(2 ** 63)}, REQUIRED, id='fahrer-zu-gross'),
    pytest.param({'person_id': '٣'}, REQUIRED, id='fahrer-arabische-ziffer'),
    # Daten
    pytest.param({'start_date': '2026-02-30'}, START_DATE, id='start-gibt-es-nicht'),
    pytest.param({'start_date': '2026-13-01'}, START_DATE, id='start-monat-13'),
    pytest.param({'start_date': '10.03.2026'}, START_DATE, id='start-deutsch'),
    pytest.param({'start_date': '2026-3-1'}, START_DATE, id='start-ohne-nullen'),
    pytest.param({'start_date': '2026-03-10x'}, START_DATE, id='start-mit-rest'),
    pytest.param({'start_date': '２０２６-０３-１０'}, START_DATE, id='start-vollbreit'),
    pytest.param({'start_date': '1800-01-01'}, START_DATE, id='start-vor-1900'),
    pytest.param({'start_date': '<script>alert(1)</script>'}, START_DATE, id='start-skript'),
    pytest.param({'end_date': '2026-02-30'}, END_DATE, id='ende-gibt-es-nicht'),
    pytest.param({'end_date': 'morgen'}, END_DATE, id='ende-text'),
    pytest.param({'end_date': '11.03.2026'}, END_DATE, id='ende-deutsch'),
    # Ende vor Start
    pytest.param({'end_date': '2026-03-09'}, END_BEFORE_START, id='ende-tag-davor'),
    pytest.param({'end_date': '2025-12-31'}, END_BEFORE_START, id='ende-jahr-davor'),
    pytest.param({'end_date': '2026-03-10', 'start_time': '10:00', 'end_time': '09:59'}, END_BEFORE_START,
                 id='gleicher-tag-uhrzeit-davor'),
    pytest.param({'start_time': '10:00', 'end_time': '09:59'}, END_BEFORE_START, id='ohne-end-datum-uhrzeit-davor'),
    pytest.param({'end_date': '2026-03-09', 'start_time': '08:00', 'end_time': '18:00'}, END_BEFORE_START,
                 id='tag-davor-trotz-spaeterer-uhrzeit'),
    # Uhrzeit
    pytest.param({'start_time': '8:00'}, TIME, id='uhrzeit-ohne-null'),
    pytest.param({'start_time': '24:00'}, TIME, id='uhrzeit-24'),
    pytest.param({'end_time': '12:60'}, TIME, id='minute-60'),
    pytest.param({'end_time': '12:5'}, TIME, id='minute-einstellig'),
    pytest.param({'start_time': '12-30'}, TIME, id='uhrzeit-bindestrich'),
    pytest.param({'start_time': '12:00:00'}, TIME, id='uhrzeit-mit-sekunden'),
    pytest.param({'start_time': '１２:００'}, TIME, id='uhrzeit-vollbreit'),
    pytest.param({'end_time': 'abc'}, TIME, id='uhrzeit-text'),
    pytest.param({'start_time': '12:00', 'end_time': '25:00'}, TIME, id='zweite-uhrzeit'),
    # Kilometer
    *[pytest.param({field: value}, KM, id=f'{field}-{value}')
      for field in ('start_km', 'end_km', 'distance_km')
      for value in ('-1', '12,5', '12.5', 'abc', '10000000', '１２', '+5', '1e3', '12 5', '99999999999999999999999')],
    pytest.param({'start_km': '100', 'end_km': '99'}, TACHO, id='tacho-ende-unter-start'),
    pytest.param({'start_km': '100', 'end_km': '0'}, TACHO, id='tacho-ende-null'),
    # Beleg
    *[pytest.param({'document_id': value}, DOCUMENT, id=f'beleg-{value}')
      for value in ('abc', '0', '-3', '1.5', str(2 ** 63), '٣', '1e2')],
    # Texte
    *[pytest.param({field: 'x' * 501}, TOO_LONG, id=f'{field}-501-zeichen')
      for field in ('start_point', 'destination', 'vehicle', 'reason')],
]


@pytest.mark.parametrize('overrides, message', REJECTIONS)
def test_an_impossible_value_is_refused_and_nothing_is_saved(tmp_db, driver, overrides, message):
    status, location = handlers.handle_add_trip(tmp_db, _form(driver, **overrides))

    assert status == 303
    assert _error(location) == [message]
    assert _trips(tmp_db, driver) == []
    # sonst bleibt der Zeitraum erhalten
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert urllib.parse.urlsplit(location).path == '/trips'
    assert query['from'] == ['2026-03-01'] and query['to'] == ['2026-03-31']


@pytest.mark.parametrize('overrides, message', [p.values for p in REJECTIONS], ids=[p.id for p in REJECTIONS])
def test_the_same_values_are_refused_when_changing_and_the_trip_stays_as_it_was(tmp_db, driver, overrides, message):
    trip = tmp_db.insert_trip(driver, '2026-03-05', destination='Kiel', vehicle='B-XY 1', distance_km=100)
    before = _trips(tmp_db, driver)

    status, location = handlers.handle_update_trip(tmp_db, _form(driver, id=trip, **overrides))

    assert status == 303
    assert _error(location) == [message]
    assert _trips(tmp_db, driver) == before


def test_the_first_violation_is_named_in_the_order_of_the_rules(tmp_db, driver):
    status, location = handlers.handle_add_trip(tmp_db, _form(
        driver, start_date='kaputt', end_date='auch kaputt', start_km='-1', document_id='abc'))
    assert _error(location) == [START_DATE]
    _, location = handlers.handle_add_trip(tmp_db, _form(driver, start_km='-1', document_id='abc'))
    assert _error(location) == [KM]
    _, location = handlers.handle_add_trip(tmp_db, _form(driver, document_id='abc', reason='x' * 501))
    assert _error(location) == [DOCUMENT]


def test_trip_fields_is_one_pure_function_returning_the_fields_or_the_first_message():
    fields, error = handlers._trip_fields(_form(1, start_km='10', end_km='25', distance_km='999', vehicle=' B-XY 1 '))
    assert error is None
    assert fields == {'start_date': '2026-03-10', 'start_time': '', 'end_date': '', 'end_time': '',
                      'start_point': '', 'destination': 'Hamburg', 'vehicle': 'B-XY 1', 'reason': '',
                      'distance_km': 15, 'start_km': 10, 'end_km': 25, 'document_id': None}
    assert handlers._trip_fields(_form(1, destination='')) == (None, REQUIRED)
    assert handlers._trip_fields(_form(1, start_km='x', end_date='2026-03-09')) == (None, END_BEFORE_START)


# ── Lockeres Fahrtenbuch: alles Mögliche geht ohne Meldung durch ─────────────

ACCEPTED = [
    pytest.param({}, id='nur-pflichtangaben'),
    pytest.param({'start_time': '08:00', 'end_time': '08:00'}, id='gleiche-uhrzeit'),
    pytest.param({'start_time': '00:00', 'end_time': '23:59'}, id='grenzen-der-uhrzeit'),
    pytest.param({'end_date': '2026-03-11', 'start_time': '22:00', 'end_time': '06:00'}, id='ueber-nacht'),
    pytest.param({'start_time': '08:00'}, id='nur-start-uhrzeit'),
    pytest.param({'end_time': '08:00'}, id='nur-end-uhrzeit'),
    pytest.param({'end_date': '2026-03-10'}, id='ende-am-selben-tag'),
    pytest.param({'start_km': '0', 'end_km': '0', 'distance_km': '0'}, id='alles-null'),
    pytest.param({'start_km': '9999999', 'end_km': '9999999'}, id='obergrenze'),
    pytest.param({'start_km': '0000012', 'end_km': '15'}, id='fuehrende-nullen'),
    pytest.param({'start_km': ' 100 ', 'end_km': ' 150 '}, id='leerraum-um-die-zahl'),
    pytest.param({'start_km': '100'}, id='nur-tacho-start'),
    pytest.param({'end_km': '100'}, id='nur-tacho-ende'),
    pytest.param({'distance_km': '12'}, id='nur-km'),
    pytest.param({field: 'x' * 500 for field in ('start_point', 'destination', 'vehicle', 'reason')},
                 id='500-zeichen'),
    pytest.param({'start_date': ' 2026-03-10 ', 'destination': '  Hamburg  '}, id='getrimmt'),
]


@pytest.mark.parametrize('overrides', ACCEPTED)
def test_a_possible_trip_is_saved_without_any_message(tmp_db, driver, overrides):
    status, location = handlers.handle_add_trip(tmp_db, _form(driver, **overrides))

    assert status == 303
    assert _error(location) is None
    assert location == f'/trips?person={driver}&from=2026-03-01&to=2026-03-31'
    assert len(_trips(tmp_db, driver)) == 1


def test_the_texts_are_trimmed_and_the_dates_stored_as_given(tmp_db, driver):
    handlers.handle_add_trip(tmp_db, _form(driver, start_date=' 2026-03-10 ', destination='  Hamburg  ',
                                           vehicle=' B-XY 1 ', reason='\tTermin\n'))
    (trip,) = tmp_db.fetch_trips(driver)
    assert (trip[2], trip[7], trip[8], trip[9]) == ('2026-03-10', 'Hamburg', 'B-XY 1', 'Termin')


def test_empty_optional_fields_are_stored_as_before(tmp_db, driver):
    handlers.handle_add_trip(tmp_db, _form(driver))
    (trip,) = tmp_db.fetch_trips(driver)
    assert (trip[3], trip[4], trip[5], trip[6], trip[8], trip[9]) == ('', '', '', '', '', '')
    assert (trip[10], trip[11], trip[12], trip[13]) == (None, None, None, None)


def test_trips_with_gaps_and_overlaps_of_the_odometer_are_saved_in_any_order_without_a_message(tmp_db, driver):
    """Die Lücke zwischen den Tachoständen prüft die Seite nur auf Wunsch – beim Speichern nie."""
    forms = [
        _form(driver, start_date='2026-03-12', start_km='1500', end_km='1600'),     # später gefahren, zuerst eingetragen
        _form(driver, start_date='2026-03-10', start_km='1000', end_km='1100'),     # Lücke 1100 → 1500 liegt dazwischen
        _form(driver, start_date='2026-03-11', start_km='1050', end_km='1200'),     # überschneidet die erste
        _form(driver, start_date='2026-03-13'),                                     # ganz ohne Tachostand
        _form(driver, start_date='2026-03-09', end_km='900'),                       # nur Tacho Ende
        _form(driver, start_date='2026-03-14', start_km='5000', distance_km='40'),  # nur Tacho Start
    ]
    for form in forms:
        status, location = handlers.handle_add_trip(tmp_db, form)
        assert status == 303 and _error(location) is None, location
    assert len(_trips(tmp_db, driver)) == len(forms)


def test_the_odometer_difference_is_the_distance_and_the_client_value_is_ignored(tmp_db, driver):
    handlers.handle_add_trip(tmp_db, _form(driver, start_km='10000', end_km='10290', distance_km='999'))
    handlers.handle_add_trip(tmp_db, _form(driver, start_km='500', distance_km='120'))        # nur ein Stand: km bleiben
    first, second = tmp_db.fetch_trips(driver)
    assert (first[10], first[11], first[12]) == (290, 10000, 10290)
    assert (second[10], second[11], second[12]) == (120, 500, None)


def test_changing_a_trip_saves_the_new_values(tmp_db, driver):
    trip = tmp_db.insert_trip(driver, '2026-03-05', destination='Kiel', distance_km=100)

    status, location = handlers.handle_update_trip(tmp_db, _form(
        driver, id=trip, start_date='2026-03-06', destination='Lübeck', start_km='10', end_km='90'))

    assert status == 303 and _error(location) is None
    row = tmp_db.get_trip_by_id(trip)
    assert (row[2], row[7], row[10], row[11], row[12]) == ('2026-03-06', 'Lübeck', 80, 10, 90)


def test_existing_trips_with_impossible_values_can_still_be_shown_and_deleted(tmp_db, driver):
    """Bestehende Fahrten werden nicht nachträglich geprüft."""
    odd = tmp_db.insert_trip(driver, '2026-03-05', destination='Kiel', start_km=500, end_km=100, distance_km=-5)
    html = PageTrips(tmp_db, person_id=driver, date_from='2026-03-01', date_to='2026-03-31')
    assert 'Kiel' in html
    tmp_db.delete_trip(odd)
    assert tmp_db.get_trip_by_id(odd) is None


# ── Speicherfehler werden gemeldet ───────────────────────────────────────────

def test_an_unknown_driver_is_reported_and_nothing_is_saved(tmp_db, driver):
    status, location = handlers.handle_add_trip(tmp_db, _form(driver + 1000))
    assert status == 303 and _error(location) == [NOT_SAVED]
    assert _trips(tmp_db, driver) == []


def test_an_unknown_document_is_reported_and_nothing_is_saved(tmp_db, driver):
    status, location = handlers.handle_add_trip(tmp_db, _form(driver, document_id='99999'))
    assert status == 303 and _error(location) == [NOT_SAVED]
    assert _trips(tmp_db, driver) == []


def test_a_known_document_is_linked(tmp_db, driver):
    tmp_db.insert_receipt('R-1', '2026-03-01', 'beleg.pdf', 'data/x.pdf', 'Tankbeleg')
    document = tmp_db.fetch_receipts()[0][0]
    handlers.handle_add_trip(tmp_db, _form(driver, document_id=document))
    assert tmp_db.fetch_trips(driver)[0][13] == document


def test_changing_to_an_unknown_document_is_reported_and_the_trip_stays(tmp_db, driver):
    trip = tmp_db.insert_trip(driver, '2026-03-05', destination='Kiel', distance_km=100)
    before = _trips(tmp_db, driver)

    status, location = handlers.handle_update_trip(tmp_db, _form(driver, id=trip, document_id='99999'))

    assert status == 303 and _error(location) == [NOT_SAVED]
    assert _trips(tmp_db, driver) == before


@pytest.mark.parametrize('trip_id', ['99999', 'abc', '', '0', '-1', '1.5', str(2 ** 63), None])
def test_changing_an_unknown_or_invalid_id_is_reported_and_creates_nothing(tmp_db, driver, trip_id):
    status, location = handlers.handle_update_trip(tmp_db, _form(driver, id=trip_id))
    assert status == 303 and _error(location) == [GONE]
    assert _trips(tmp_db, driver) == []


def test_a_trip_deleted_in_the_meantime_is_reported(tmp_db, driver):
    trip = tmp_db.insert_trip(driver, '2026-03-05', destination='Kiel', distance_km=100)
    tmp_db.delete_trip(trip)
    _, location = handlers.handle_update_trip(tmp_db, _form(driver, id=trip))
    assert _error(location) == [GONE]


def test_the_database_layer_reports_a_failure_to_its_caller(tmp_db, driver):
    assert tmp_db.insert_trip(driver + 1000, '2026-03-05', destination='Kiel') is None
    assert tmp_db.insert_trip(driver, '2026-03-05', destination='Kiel', document_id=99999) is None
    trip = tmp_db.insert_trip(driver, '2026-03-05', destination='Kiel')
    assert isinstance(trip, int)
    assert tmp_db.update_trip(trip, '2026-03-06', destination='Kiel') is True
    assert tmp_db.update_trip(trip, '2026-03-06', destination='Kiel') is True        # unverändert ist trotzdem gespeichert
    assert tmp_db.update_trip(trip + 1000, '2026-03-06', destination='Kiel') is False
    assert tmp_db.update_trip(trip, '2026-03-07', destination='Kiel', document_id=99999) is False
    assert tmp_db.get_trip_by_id(trip)[2] == '2026-03-06'                             # der Fehlschlag änderte nichts


def test_a_failed_save_leaves_the_database_usable(tmp_db, driver):
    """Die Verbindung wird auch im Fehlerfall geschlossen: Danach lässt sich sofort weiterschreiben und löschen."""
    for _ in range(5):
        assert tmp_db.insert_trip(driver + 1000, '2026-03-05', destination='Kiel') is None
    trip = tmp_db.insert_trip(driver, '2026-03-05', destination='Kiel')
    tmp_db.delete_trip(trip)
    assert _trips(tmp_db, driver) == []


# ── Über den echten Server: Weiterleitung, Meldung auf der Seite, kein 500 ───

@pytest.fixture
def person(auto_login):
    return _driver(auto_login.db)


def _post_form(port, csrf, route, fields):
    return post(port, route, headers={'Origin': own_origin(port)}, csrf=csrf,
                form={k: v[0] for k, v in fields.items()})


def test_the_page_shows_the_message_of_a_refusal_only_as_script_data(server_port, auto_login, person):
    resp = _post_form(server_port, auto_login.csrf, '/trips/add', _form(person, end_date='2026-03-09'))
    assert resp.status == 303
    location = resp.headers['Location']
    assert location == (f'/trips?person={person}&from=2026-03-01&to=2026-03-31'
                        f'&error={urllib.parse.quote(END_BEFORE_START, safe="")}')

    page = get(server_port, location)

    assert page.status == 200
    assert app_msg_arguments(Dom(page.text)) == [END_BEFORE_START, 'error']
    assert END_BEFORE_START not in re.sub(r'<script\b.*?</script>', '', page.text, flags=re.S)   # nur im Skriptblock
    assert auto_login.db.fetch_trips(person) == []


def test_every_refusal_over_http_redirects_with_its_message_and_saves_nothing(server_port, auto_login, person):
    shown = {}
    for overrides, message in [(p.values[0], p.values[1]) for p in REJECTIONS if 'person_id' not in p.values[0]]:
        resp = _post_form(server_port, auto_login.csrf, '/trips/add', _form(person, **overrides))
        assert resp.status == 303, overrides
        assert _error(resp.headers['Location']) == [message], overrides
        shown.setdefault(message, resp.headers['Location'])
    assert auto_login.db.fetch_trips(person) == []
    assert set(shown) == {REQUIRED, START_DATE, END_DATE, END_BEFORE_START, TIME, KM, TACHO, DOCUMENT, TOO_LONG}
    for message, location in shown.items():                      # und jede Seite zeigt ihn nur als Skriptdaten
        page = get(server_port, location)
        assert page.status == 200
        assert app_msg_arguments(Dom(page.text)) == [message, 'error']
        assert message not in re.sub(r'<script\b.*?</script>', '', page.text, flags=re.S)


@pytest.mark.parametrize('person_id', ['abc', '', '0', '-1', '1.5', '99999999999999999999999', '<script>'])
def test_an_invalid_person_id_is_no_500_and_saves_nothing(server_port, auto_login, person, person_id):
    for route in ('/trips/add', '/trips/update'):
        resp = _post_form(server_port, auto_login.csrf, route, _form(person, id=1, person_id=person_id))
        assert resp.status == 303, route
        assert _error(resp.headers['Location']) == [REQUIRED], route
        assert 'person=0' in resp.headers['Location']
    assert auto_login.db.fetch_trips(person) == []


@pytest.mark.parametrize('trip_id', ['abc', '', '0', '-1', '1.5', '99999999999999999999999', '<script>'])
def test_an_invalid_id_when_changing_is_no_500(server_port, auto_login, person, trip_id):
    trip = auto_login.db.insert_trip(person, '2026-03-05', destination='Kiel', distance_km=100)

    resp = _post_form(server_port, auto_login.csrf, '/trips/update', _form(person, id=trip_id))

    assert resp.status == 303
    assert _error(resp.headers['Location']) == [GONE]
    assert [(r[0], r[7]) for r in auto_login.db.fetch_trips(person)] == [(trip, 'Kiel')]


def test_a_possible_trip_over_http_redirects_without_a_message(server_port, auto_login, person):
    resp = _post_form(server_port, auto_login.csrf, '/trips/add', _form(person, start_km='100', end_km='150'))
    assert resp.status == 303
    assert resp.headers['Location'] == f'/trips?person={person}&from=2026-03-01&to=2026-03-31'
    assert [r[10] for r in auto_login.db.fetch_trips(person)] == [50]


def test_a_changed_period_in_the_form_is_kept_in_the_error_redirect(server_port, auto_login, person):
    resp = _post_form(server_port, auto_login.csrf, '/trips/add',
                      _form(person, **{'from': '2025-01-01', 'to': '2025-12-31', 'destination': ''}))
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(resp.headers['Location']).query)
    assert query['from'] == ['2025-01-01'] and query['to'] == ['2025-12-31'] and query['error'] == [REQUIRED]


# ── Browser: dieselben Regeln, damit die Eingabe nicht verloren geht ─────────

def test_the_form_checks_the_same_rules_before_it_is_sent(tmp_db, driver):
    html = PageTrips(tmp_db, person_id=driver, date_from='2026-03-01', date_to='2026-03-31')
    assert "addEventListener('submit'" in html and 'e.preventDefault()' in html
    assert "appMsg(err, 'error')" in html
    for message in (REQUIRED, END_BEFORE_START, KM, TACHO):
        assert message in _TRIPS_JS, message                     # derselbe Wortlaut wie auf dem Server
    assert html.count('maxlength="500"') == 4                    # Startpunkt, Ziel, Fahrzeug, Grund


def test_the_browser_check_uses_no_innerhtml():
    assert 'innerHTML' not in _TRIPS_JS

