# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""„Als neu kopieren“ für Arbeitszeiten und Fahrten (seit 2.5.3).

Wie bei der Rechnung: Ein geladener Eintrag wird zur Vorlage eines neuen. Der Knopf speichert nichts – er leert nur die ID
im Formular und stellt es auf „neu“; angelegt wird der Eintrag erst mit „Speichern“ über die gewöhnliche Route
(``/worktime/add``, ``/trips/add``) mit all ihren Prüfungen. Der Ausgangseintrag bleibt, wie er ist.

Geprüft wird ohne Browser, was sich am ausgelieferten Text festmachen lässt: der Knopf (kein Absende-Knopf, anfangs
verborgen), die Schritte des Skripts und dass der Server eine Kopie wie jeden neuen Eintrag anlegt. Der Ablauf selbst
(Laden, Kopieren, Datum, Speichern) ist im Browser durchgespielt.

Alle Namen und Werte sind erfunden.
"""
import re

import pytest

from server import handlers
from server.pages_trips import PageTrips, _TRIPS_JS
from server.pages_worktime import PageWorkTimes, _WORKTIME_JS

PERIOD = {'date_from': '2026-03-01', 'date_to': '2026-03-31'}


@pytest.fixture
def person(tmp_db):
    return tmp_db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                                 first_name='Erika', last_name='Probe')


def function_body(script, name):
    """Der Rumpf einer Funktion des Skripts (bis zur nächsten Zeile, die mit ``}`` beginnt)."""
    found = re.search(r'(?ms)^function ' + re.escape(name) + r'\(\) \{\n(.*?)^\}', script)
    assert found, f'{name} fehlt'
    return found.group(1)


def button(html, element_id):
    found = re.search(r'<(button|input)\b[^>]*\bid="' + re.escape(element_id) + r'"[^>]*>', html)
    assert found, f'{element_id} fehlt'
    return found.group(0)


@pytest.mark.parametrize('page, element_id, call', [(PageWorkTimes, 'wtCopyBtn', 'wtCopyAsNew()'),
                                                     (PageTrips, 'trCopyBtn', 'trCopyAsNew()')])
def test_the_button_is_a_plain_button_that_never_submits_and_starts_hidden(tmp_db, person, page, element_id, call):
    html = page(tmp_db, person_id=person, **PERIOD)
    tag = button(html, element_id)
    assert tag.startswith('<button ') and 'type="button"' in tag            # ein Absende-Knopf speicherte sofort
    assert f'onclick="{call}"' in tag and 'style="display:none;"' in tag and 'formaction' not in tag
    assert html.count('📋 Als neu kopieren') == 1


def test_the_trips_page_no_longer_saves_a_copy_at_once(tmp_db, person):
    """„Als neu speichern“ legte die Fahrt sofort an – mit demselben Datum, wenn es niemand vorher änderte."""
    html = PageTrips(tmp_db, person_id=person, **PERIOD)
    assert 'Als neu speichern' not in html and 'trSaveAsNewBtn' not in html
    assert 'formaction' not in html


@pytest.mark.parametrize('script, prefix', [(_WORKTIME_JS, 'wt'), (_TRIPS_JS, 'tr')])
def test_the_copy_button_shows_only_while_an_entry_is_loaded(script, prefix):
    body = function_body(script, f'{prefix}CheckDirty')
    assert f"const editing = !!document.getElementById('{prefix}Id').value;" in body
    assert f"document.getElementById('{prefix}CopyBtn').style.display = editing ? '' : 'none';" in body


def test_copying_a_worktime_entry_only_turns_the_form_into_a_new_entry_of_today():
    body = function_body(_WORKTIME_JS, 'wtCopyAsNew')
    assert "if (!document.getElementById('wtId').value) return;" in body
    assert "document.getElementById('wtId').value = '';" in body
    assert "document.getElementById('wtDate').value = WT_DATA.today;" in body
    assert 'wtResetFormChrome(true);' in body                               # Ziel ist /worktime/add
    assert 'fetch(' not in body and 'submit' not in body and 'appPost' not in body      # gespeichert wird hier nichts
    # Zeiten, Pause, Kunde, Ort und Notiz bleiben stehen: Kein Feld außer ID und Datum wird gesetzt
    assert set(re.findall(r"getElementById\('(\w+)'\)\.value =", body)) == {'wtId', 'wtDate'}


def test_the_template_of_the_last_entry_never_fills_a_copy():
    """Die Vorlage füllt bei einem neuen Eintrag leere Felder und setzt den Arbeitsort – eine Kopie trüge danach andere
    Angaben als ihr Ausgangseintrag."""
    assert "if (!wtIsCopy && !document.getElementById('wtId').value && document.getElementById('wtDate').value) {" \
        in function_body(_WORKTIME_JS, 'wtOnDateChange')
    assert 'wtIsCopy = true;' in function_body(_WORKTIME_JS, 'wtCopyAsNew')
    assert 'wtIsCopy = false;' in function_body(_WORKTIME_JS, 'wtClear')
    assert _WORKTIME_JS.count('wtIsCopy = false;') == 3                     # Deklaration, Leeren, Laden


def test_copying_a_trip_keeps_route_and_distance_and_drops_what_belongs_to_the_one_trip():
    body = function_body(_TRIPS_JS, 'trCopyAsNew')
    assert "if (!document.getElementById('trId').value) return;" in body
    cleared = set(re.findall(r"getElementById\('(\w+)'\)\.value = '';", body))
    assert cleared == {'trId', 'trStartKm', 'trEndKm', 'trDocument'}        # Tachostände und Beleg wiederholen sich nicht
    assert 'start.value = TRIPS_DATA.today;' in body
    assert "document.getElementById('trDistance').readOnly = false;" in body            # ohne Tachostände wieder frei
    assert 'trResetFormChrome(true);' in body
    assert 'fetch(' not in body and 'submit' not in body and 'appPost' not in body
    for kept in ('trStartPoint', 'trDestination', 'trVehicle', 'trReason', 'trStartTime', 'trEndTime', 'trDistance'):
        assert f"getElementById('{kept}').value" not in body


def test_a_trip_over_several_days_keeps_its_length():
    body = function_body(_TRIPS_JS, 'trCopyAsNew')
    assert 'const days = trDayNumber(end.value) - trDayNumber(start.value);' in body
    assert 'if (end.value) end.value = trDayIso(today + (days > 0 ? days : 0));' in body
    assert 'Date.UTC(' in function_body(_TRIPS_JS.replace('function trDayNumber(iso) {', 'function trDayNumber() {'),
                                         'trDayNumber')                    # in UTC: kein Sprung an der Sommerzeit


def test_the_server_stores_a_copied_worktime_entry_like_any_new_one_and_leaves_the_original(tmp_db, person):
    form = {'person_id': [str(person)], 'kind': ['work'], 'start_time': ['08:00'], 'end_time': ['16:30'],
            'pause_minutes': ['30'], 'location_mode': ['own'], 'note': ['Wartung vor Ort'], 'from': ['2026-03-01'],
            'to': ['2026-03-31']}
    handlers.handle_add_worktime(tmp_db, {**form, 'date': ['2026-03-02']})
    original = tmp_db.fetch_worktimes(person, '2026-03-01', '2026-03-31')
    assert len(original) == 1
    # die Kopie: dieselben Angaben, die ID leer, ein anderes Datum – über die Route für neue Einträge
    handlers.handle_add_worktime(tmp_db, {**form, 'id': [''], 'date': ['2026-03-03']})
    rows = tmp_db.fetch_worktimes(person, '2026-03-01', '2026-03-31')
    assert len(rows) == 2 and original[0] in rows
    copy = next(row for row in rows if row[0] != original[0][0])
    assert copy[3:] == original[0][3:] and str(copy[2]) == '2026-03-03'


def test_the_server_stores_a_copied_trip_like_any_new_one_and_leaves_the_original(tmp_db, person):
    form = {'person_id': [str(person)], 'start_time': ['08:00'], 'end_time': ['17:30'], 'start_point': ['Musterstadt'],
            'destination': ['Beispielort'], 'vehicle': ['XX-MU 100'], 'reason': ['Kundentermin'], 'distance_km': ['288'],
            'from': ['2026-03-01'], 'to': ['2026-03-31']}
    handlers.handle_add_trip(tmp_db, {**form, 'start_date': ['2026-03-02'], 'end_date': ['2026-03-02'],
                                      'start_km': ['48210'], 'end_km': ['48498']})
    original = tmp_db.fetch_trips(person, '2026-03-01', '2026-03-31')
    assert len(original) == 1
    handlers.handle_add_trip(tmp_db, {**form, 'id': [''], 'start_date': ['2026-03-09'], 'end_date': ['2026-03-09'],
                                      'start_km': [''], 'end_km': [''], 'document_id': ['']})
    rows = tmp_db.fetch_trips(person, '2026-03-01', '2026-03-31')
    assert len(rows) == 2 and original[0] in rows
