# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Werte aus Adresse und Datenbank in JavaScript-Kontexten (Skriptblöcke, ``onclick``), 2.0.3.

Ein ``</script>`` in einem Wert beendet den Skriptblock, ein nur HTML-maskiertes ``'`` löst der
Browser im ``onclick`` wieder zu einem echten Hochkomma auf. Deshalb gilt:

- JSON für ``<script>``-Blöcke nur über ``pages.script_json`` (``<``, ``>``, ``&``, U+2028/2029
  als ``\\uXXXX``, sonst ``json.dumps(ensure_ascii=False)``)
- ``?error=`` auf ``/trips`` und ``/worktime``, gespeicherte Daten in den Datenblöcken beider Seiten
- ``PageSkr(msg=…)`` (``/masterdata/skr?msg=…``)
- die Belegnummer im „Löschen“-Link der Belegliste steht prozentkodiert in der Adresse

Gelesen werden die Seiten wie im Browser (``tests/dom_helpers.py``). Alle Werte sind erfundene
Testdaten.
"""
import json
import re
import sqlite3
import urllib.parse

import pytest

from server.pages import script_json
from server.pages_masterdata import PageSkr
from server.pages_receipts import PageReceipts
from server.pages_trips import PageTrips
from server.pages_worktime import PageWorkTimes
from tests.dom_helpers import Dom, app_msg_arguments, assert_same_structure, data_block
from tests.http_client import get, post

PAYLOAD = '</script><img src=x onerror=alert(1)>'


def encode(**params):
    return urllib.parse.urlencode(params)


# ── pages.script_json ────────────────────────────────────────────────────────
@pytest.mark.parametrize('value', [
    PAYLOAD, '<!--', '-->', 'a&b', 'a\u2028b\u2029c', "x'y\"z\\w",
    {'k</script>': ['<b>', 1, None, True, 2.5]}, ['ä', 'ö', 'ß', '€', '日本'], 'Zeile\nZwei\tTab',
    {'verschachtelt': {'a': [{'b': '</script>'}]}}, '', 0, None, [],
])
def test_script_json_reads_back_as_the_original_value(value):
    assert json.loads(script_json(value)) == value


@pytest.mark.parametrize('value', ['</script>', '<', '>', '&', '\u2028', '\u2029', '<!-- <script>',
                                   {'<': '>'}, ['</SCRIPT>']])
def test_script_json_leaves_no_character_that_could_close_or_open_an_element(value):
    out = script_json(value)
    for char in '<>&\u2028\u2029':
        assert char not in out, f'{char!r} steht roh in {out!r}'


def test_script_json_escapes_with_unicode_escapes():
    out = script_json('</script>&\u2028\u2029')
    assert out.lower() == '"\\u003c/script\\u003e\\u0026\\u2028\\u2029"'


@pytest.mark.parametrize('value', [
    'Hamburg', 'Büro Süd', {'a': 1, 'b': [1, 2.5, None, True, 'ä']}, ['x', {'y': 'z'}], 42, None,
    'Zeile\nZwei', "x'y\"z\\w", '日本',
])
def test_script_json_is_json_dumps_for_values_without_the_special_characters(value):
    assert script_json(value) == json.dumps(value, ensure_ascii=False)


# ── ?error= auf /trips und /worktime, gespeicherte Daten ─────────────────────
@pytest.fixture
def person(auto_login):
    """Eigener Kontakt (Person) – damit /trips und /worktime eine Auswahl haben."""
    db = auto_login.db
    db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                      first_name='Erika', last_name='Probe')
    return db.fetch_contacts(contact_type='own')[0][0]


@pytest.mark.parametrize('page', ['/trips', '/worktime'])
def test_the_error_parameter_cannot_close_the_script_block(server_port, person, page):
    benign = get(server_port, f'{page}?error=Normale+Meldung')
    hostile = get(server_port, f'{page}?{encode(error=PAYLOAD)}')
    assert hostile.status == 200
    assert_same_structure(hostile.text, benign.text)
    assert '</script><img' not in hostile.text
    # Die Meldung kommt trotzdem unverändert beim Nutzer an
    assert app_msg_arguments(Dom(hostile.text)) == [PAYLOAD, 'error']


@pytest.mark.parametrize('page', ['/trips', '/worktime'])
def test_the_error_parameter_survives_exotic_characters(server_port, person, page):
    message = 'Fehler: <b>fett</b> & "zitiert" \'einfach\' \u2028 Umlaut äöü 日本\nzweite Zeile'
    resp = get(server_port, f'{page}?{encode(error=message)}')
    assert resp.status == 200
    assert app_msg_arguments(Dom(resp.text)) == [message, 'error']


def _pages(db, person, trip, worktime):
    return {
        'trips': PageTrips(db, person_id=person, date_from='2026-03-01', date_to='2026-03-31'),
        'trips-edit': PageTrips(db, person_id=person, date_from='2026-03-01', date_to='2026-03-31',
                                edit_id=trip),
        'worktime': PageWorkTimes(db, person_id=person, date_from='2026-03-01', date_to='2026-03-31'),
        'worktime-edit': PageWorkTimes(db, person_id=person, date_from='2026-03-01',
                                       date_to='2026-03-31', edit_id=worktime),
    }


def test_stored_data_with_a_script_end_tag_cannot_close_the_block(auto_login):
    """Fahrtziel, Notiz, Ort und Name stehen in JSON-Blöcken (bekannte Strecken, Vorlage,
    Bearbeiten, Städte) und in HTML – ein </script> darin ändert den Aufbau der Seite nicht."""
    db = auto_login.db
    db.insert_contact(contact_type='own', entity_type='person', display_name=f'Person {PAYLOAD}',
                      first_name='Erika', last_name='Probe', street=f'Weg {PAYLOAD}',
                      city=f'Ort {PAYLOAD}')
    db.insert_contact(contact_type='customer', entity_type='company', display_name=f'Kunde {PAYLOAD}',
                      company_name=f'Kunde {PAYLOAD}', city=f'Stadt {PAYLOAD}')
    person = db.fetch_contacts(contact_type='own')[0][0]
    trip = db.insert_trip(person, '2026-03-10', start_point=f'Start {PAYLOAD}',
                          destination=f'Ziel {PAYLOAD}', vehicle=f'Auto {PAYLOAD}',
                          reason=f'Grund {PAYLOAD}', distance_km=12)
    worktime = db.insert_worktime(person, '2026-03-10', start_time='08:00', end_time='16:00',
                                  location_mode='other', location_city=f'Ort {PAYLOAD}',
                                  note=f'Notiz {PAYLOAD}', pause_text=f'Pause {PAYLOAD}')
    hostile = _pages(db, person, trip, worktime)

    # Maßstab: dieselben Seiten mit harmlosem Text an denselben Stellen
    con = sqlite3.connect(db.db_name)
    try:
        con.execute("UPDATE Trips SET StartPoint='Start x', Destination='Ziel x', Vehicle='Auto x', "
                    "Reason='Grund x'")
        con.execute("UPDATE WorkTimes SET LocationCity='Ort x', Note='Notiz x', PauseText='Pause x'")
        con.execute("UPDATE Contacts SET DisplayName='Person x'")
        con.execute("UPDATE ContactAddresses SET Street='Weg x', City='Ort x'")
        con.commit()
    finally:
        con.close()
    benign = _pages(db, person, trip, worktime)

    for name in hostile:
        assert_same_structure(hostile[name], benign[name])
        assert '</script><img' not in hostile[name], name
        assert '<img src=x onerror=alert(1)>' not in hostile[name], name


def test_stored_data_with_a_script_end_tag_is_still_readable_in_the_data_block(auto_login):
    db = auto_login.db
    db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                      first_name='Erika', last_name='Probe')
    person = db.fetch_contacts(contact_type='own')[0][0]
    db.insert_trip(person, '2026-03-10', start_point='A', destination='</script>Ziel', distance_km=12)
    data = data_block(Dom(PageTrips(db, person_id=person, date_from='2026-03-01',
                                    date_to='2026-03-31')), 'TRIPS_DATA')
    assert data['knownRoutes'] == {'a|</script>ziel': 12}


# ── PageSkr(msg=…) ───────────────────────────────────────────────────────────
NORMAL_MESSAGE = "Konto „Größe“ wurde gelöscht: 'Nr. 4711' (Rückstrich \\)\nZweite Zeile"


def test_skr_message_with_a_script_end_tag_cannot_close_the_block(tmp_db):
    hostile = PageSkr(tmp_db, msg=PAYLOAD, msg_type='info')
    benign = PageSkr(tmp_db, msg='Normale Meldung', msg_type='info')
    assert_same_structure(hostile, benign)
    assert '</script><img' not in hostile and '<img src=x onerror=alert(1)>' not in hostile
    assert app_msg_arguments(Dom(hostile)) == [PAYLOAD, 'info']


def test_skr_message_type_cannot_close_the_block_either(tmp_db):
    hostile = PageSkr(tmp_db, msg='Meldung', msg_type="');</script><img src=x onerror=alert(1)>")
    assert_same_structure(hostile, PageSkr(tmp_db, msg='Meldung', msg_type='error'))
    assert '</script><img' not in hostile
    assert app_msg_arguments(Dom(hostile)) == ['Meldung', "');</script><img src=x onerror=alert(1)>"]


@pytest.mark.parametrize('msg_type', ['info', 'error', 'warn'])
def test_skr_message_reaches_the_page_unchanged(tmp_db, msg_type):
    """Umlaute, Hochkomma, Rückstrich und Zeilenumbruch kommen so an, wie sie gespeichert sind."""
    html = PageSkr(tmp_db, msg=NORMAL_MESSAGE, msg_type=msg_type)
    assert app_msg_arguments(Dom(html)) == [NORMAL_MESSAGE, msg_type]
    assert "DOMContentLoaded" in html


def test_skr_without_a_message_has_no_message_call(tmp_db):
    assert not [s for s in Dom(PageSkr(tmp_db)).scripts if 'function(){appMsg(' in s]


def test_skr_message_defaults_to_the_info_type(tmp_db):
    assert app_msg_arguments(Dom(PageSkr(tmp_db, msg='Hinweis', msg_type=None))) == ['Hinweis', 'info']


def test_skr_message_over_http(server_port, auto_login):
    benign = get(server_port, '/masterdata/skr?msg=Normale+Meldung')
    hostile = get(server_port, f'/masterdata/skr?{encode(msg=PAYLOAD)}')
    assert hostile.status == 200
    assert_same_structure(hostile.text, benign.text)
    assert '</script><img' not in hostile.text
    assert app_msg_arguments(Dom(hostile.text)) == [PAYLOAD, 'info']


def test_skr_message_with_type_over_http(server_port, auto_login):
    resp = get(server_port, f'/masterdata/skr?{encode(msg=NORMAL_MESSAGE, type="error")}')
    assert resp.status == 200
    assert app_msg_arguments(Dom(resp.text)) == [NORMAL_MESSAGE, 'error']
    hostile = get(server_port, f'/masterdata/skr?{encode(msg="x", type=PAYLOAD)}')
    assert hostile.status == 200 and '</script><img' not in hostile.text
    assert app_msg_arguments(Dom(hostile.text)) == ['x', PAYLOAD]


def test_skr_literal_payload_in_the_address(server_port, auto_login):
    """Fast wörtlich, wie ein Angreifer den Link baut (nur die Leerzeichen sind kodiert)."""
    resp = get(server_port, '/masterdata/skr?msg=</script><img%20src=x%20onerror=alert(1)>')
    assert resp.status == 200
    assert '</script><img' not in resp.text
    assert app_msg_arguments(Dom(resp.text)) == [PAYLOAD, 'info']


# ── Belegnummer im „Löschen“-Link ────────────────────────────────────────────
DELETE_ONCLICK = re.compile(r'onclick="appConfirmPost\(\'/receipts/delete\?number=(.*?)\', '
                            r'\'Beleg wirklich löschen\?\'\)"')
HOSTILE_NUMBER = "X', alert(1), '"
AWKWARD_NUMBERS = ["R 2026/004 & Co #1 +5% Ü", 'B"1"', 'a<b>c', 'x;y=z?w', "O'Brien-1", '\\1',
                   'Beleg\u2028Zwei']


def receipt_page(db, *numbers):
    for index, number in enumerate(numbers):
        db.insert_receipt(number, f'2026-03-{index + 1:02d}', f'beleg{index}.pdf',
                          f'data/beleg{index}.pdf', 'Info')
    return PageReceipts(db, '2026-01-01', '2026-12-31')


def test_receipt_number_is_percent_encoded_in_the_delete_address(tmp_db):
    html = receipt_page(tmp_db, HOSTILE_NUMBER)
    found = DELETE_ONCLICK.findall(html)
    assert found == [urllib.parse.quote(HOSTILE_NUMBER, safe='')]
    for form in ("'", '&#x27;', '&#39;', '&apos;', '"', '&quot;', ' ', ','):
        assert form not in found[0], form
    # Gelesen wie im Browser: genau zwei Zeichenketten-Argumente, nichts eingeschleust
    onclick = [a['onclick'] for t, a in Dom(html).tags if t == 'a' and 'delete-icon' in a.get('class', '')]
    assert onclick == ["appConfirmPost('/receipts/delete?number=X%27%2C%20alert%281%29%2C%20%27', "
                       "'Beleg wirklich löschen?')"]
    assert re.fullmatch(r"appConfirmPost\('[^']*', '[^']*'\)", onclick[0])


@pytest.mark.parametrize('number', AWKWARD_NUMBERS)
def test_awkward_receipt_numbers_stay_inside_their_string_and_survive_the_round_trip(tmp_db, number):
    html = receipt_page(tmp_db, number)
    onclick = [a['onclick'] for t, a in Dom(html).tags if t == 'a' and 'delete-icon' in a.get('class', '')]
    assert len(onclick) == 1
    found = re.fullmatch(r"appConfirmPost\('([^']*)', '([^']*)'\)", onclick[0])
    assert found, onclick[0]
    address = found.group(1)
    assert address == '/receipts/delete?number=' + urllib.parse.quote(number, safe='')
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(address).query)['number'] == [number]
    for form in ("'", '&#x27;', '&#39;', '"', '<', '>', ' ', '\u2028'):
        assert form not in address, form


def test_normal_receipt_numbers_keep_their_old_markup(tmp_db):
    html = receipt_page(tmp_db, 'B-2026-001', 'R26003', 'Beleg_7.final~1')
    for number in ('B-2026-001', 'R26003', 'Beleg_7.final~1'):
        assert (f"onclick=\"appConfirmPost('/receipts/delete?number={number}', "
                f"'Beleg wirklich löschen?')\">&#128465;</a>") in html


def test_a_hostile_receipt_number_does_not_change_the_page_structure(tmp_db):
    hostile = receipt_page(tmp_db, HOSTILE_NUMBER)
    con = sqlite3.connect(tmp_db.db_name)
    try:
        con.execute("UPDATE Documents SET Number = 'B-2026-001'")
        con.commit()
    finally:
        con.close()
    assert_same_structure(hostile, PageReceipts(tmp_db, '2026-01-01', '2026-12-31'))


def test_receipt_with_an_awkward_number_can_be_deleted_through_its_link(server_port, auto_login):
    """Die Adresse aus der Seite führt über POST wieder zu genau diesem Beleg."""
    number = "R 2026/004 & Co #1 +5% Ü 'x'"
    auto_login.db.insert_receipt(number, '2026-03-01', 'beleg.pdf', 'data/beleg.pdf', 'Info')
    auto_login.db.insert_receipt('B-2026-001', '2026-03-02', 'zweiter.pdf', 'data/zweiter.pdf', 'Info')
    page = get(server_port, '/receipts?from=2026-01-01&to=2026-12-31')
    assert page.status == 200
    addresses = [m for m in DELETE_ONCLICK.findall(page.text)]
    assert urllib.parse.quote(number, safe='') in addresses
    resp = post(server_port, '/receipts/delete?number=' + urllib.parse.quote(number, safe=''),
                csrf=auto_login.csrf)
    assert resp.status == 303
    assert [r[1] for r in auto_login.db.fetch_receipts()] == ['B-2026-001']
