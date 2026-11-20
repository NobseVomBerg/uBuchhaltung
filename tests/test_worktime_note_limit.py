# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Notiz eines Arbeitszeit-Eintrags ist auf 300 Zeichen begrenzt (seit 2.5.4, Entscheidung des Owners).

* Die Grenze steht an EINER Stelle (``pages_worktime.NOTE_MAX_LENGTH``); das Formularfeld bekommt daraus ``maxlength``.
* Der Server prüft beim Anlegen UND beim Ändern über eine Hilfsstelle (``handlers._wt_note_valid``): Gemessen wird ``len()`` des
  Textes ohne Leerraum am Rand – ein Umlaut oder Emoji zählt eins. Ist die Notiz länger, wird nichts gespeichert und die Seite
  zeigt über den Fehlerweg (``?error=``) den festen Satz; gekürzt wird nie.
* Die Reihenfolge der Prüfungen: Uhrzeit, Notiz, Überschneidung. Der Kundenfilter der Seite bleibt in der Weiterleitung.
* Der Bestand bleibt, wie er ist: Eine längere Notiz, die schon in der Datenbank steht, wird nicht gekürzt oder migriert;
  das PDF bricht sie wie bisher um und endet nach acht Zeilen mit „…“. Wird der Eintrag geändert, greift die Prüfung.

Alle Namen und Texte sind erfunden.
"""
import inspect
import urllib.parse

import pytest

from export.pdf_worktime import MAX_INFO_LINES, generate_worktime_pdf, info_lines
from server import handlers, pages_worktime
from server.pages_worktime import NOTE_MAX_LENGTH, PageWorkTimes
from tests.dom_helpers import Dom, app_msg_arguments
from tests.http_client import get, post
from tests.test_worktime_pdf_info import _info_cells
from tests.test_worktime_pdf_pages import MARCH, _streams, _world

MESSAGE = 'Die Notiz ist zu lang (höchstens 300 Zeichen).'


def _form(person, **fields):
    form = {'person_id': [str(person)], 'date': ['2026-03-10'], 'kind': ['work'], 'start_time': ['08:00'],
            'end_time': ['09:00'], 'location_mode': ['other'], 'location_city': ['Kiel'], 'from': [MARCH[0]],
            'to': [MARCH[1]], 'note': ['']}
    form.update({key: [value] for key, value in fields.items()})
    return form


def _query(location):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)


def _rows(db, person):
    return db.fetch_worktimes(person, *MARCH)


# ── Die Grenze und das Formular ───────────────────────────────────────────────
def test_the_limit_is_300_characters_and_the_sentence_is_built_from_the_constant():
    assert NOTE_MAX_LENGTH == 300
    assert handlers._WT_NOTE_ERROR == MESSAGE == f'Die Notiz ist zu lang (höchstens {NOTE_MAX_LENGTH} Zeichen).'


def test_the_number_stands_in_one_place():
    page_source = inspect.getsource(pages_worktime)
    assert page_source.count('NOTE_MAX_LENGTH = 300') == 1
    assert 'maxlength="300"' not in page_source and 'maxlength="{NOTE_MAX_LENGTH}"' in page_source
    check = inspect.getsource(handlers._wt_note_valid)
    assert 'NOTE_MAX_LENGTH' in check and '300' not in check
    assert 'NOTE_MAX_LENGTH' in inspect.getsource(handlers) and '300 Zeichen' not in inspect.getsource(handlers)


def test_the_form_field_of_the_note_carries_maxlength_300(tmp_db):
    world = _world(tmp_db)
    html = PageWorkTimes(tmp_db, person_id=world.person, date_from=MARCH[0], date_to=MARCH[1])
    note = Dom(html).one('input', id='wtNote')
    assert note['name'] == 'note' and note['maxlength'] == '300' == str(NOTE_MAX_LENGTH)
    assert '<input type="text" name="note" id="wtNote" maxlength="300" placeholder="Was wurde gemacht?">' in html


# ── Anlegen ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('note, accepted', [
    ('', True), ('x' * 299, True), ('x' * 300, True), ('x' * 301, False), ('x' * 1000, False),
    ('ä' * 300, True), ('ä' * 301, False), ('ß' * 300, True), ('Ö' * 301, False),
    ('😀' * 300, True), ('😀' * 301, False),                         # len() zählt Zeichen, nicht Bytes (ein Emoji sind vier)
    ('é' * 150, True), ('é' * 151, False),               # 300 bzw. 302 Zeichen: auch ein Akzent zählt eins
    ('  ' + 'x' * 300 + ' \t ', True), (' ' + 'x' * 301 + ' ', False),     # Leerraum am Rand zählt nicht mit
    ('\n' + 'x' * 300 + '\r\n', True), (' ' * 400, True), ('x' * 150 + ' ' * 200 + 'x' * 150, False),
])
def test_a_new_note_of_up_to_300_characters_is_stored_a_longer_one_is_refused(tmp_db, note, accepted):
    world = _world(tmp_db)
    status, location = handlers.handle_add_worktime(tmp_db, _form(world.person, note=note))
    assert status == 303
    rows = _rows(tmp_db, world.person)
    if accepted:
        assert 'error' not in _query(location)
        assert len(rows) == 1 and rows[0][10] == note.strip()       # gespeichert wird ohne Leerraum am Rand
    else:
        assert _query(location)['error'] == [MESSAGE]
        assert rows == []                                              # nichts gespeichert, nichts gekürzt


def test_leading_and_trailing_blanks_never_get_into_the_database(tmp_db):
    """Gemessen wird ohne Leerraum am Rand – also wird auch so gespeichert: Sonst stünde hinter 300 Zeichen ein Megabyte
    Leerzeichen in der Datenbank, und die zugesagte Grenze gälte dort nicht."""
    world = _world(tmp_db)
    status, location = handlers.handle_add_worktime(tmp_db, _form(world.person, note='x' * 300 + ' ' * 1_000_000))
    assert status == 303 and 'error' not in _query(location)
    assert _rows(tmp_db, world.person)[0][10] == 'x' * 300
    entry = _rows(tmp_db, world.person)[0][0]
    handlers.handle_update_worktime(tmp_db, _form(world.person, id=str(entry), note='\t neu \r\n' + ' ' * 5000))
    assert tmp_db.get_worktime_by_id(entry)[10] == 'neu'


@pytest.mark.parametrize('kind', ['Work', 'urlaub', 'xyz', '', ' work', '<b>', 'work\n'])
def test_a_kind_the_form_does_not_offer_is_refused_and_nothing_is_saved(tmp_db, kind):
    """Eine unbekannte Art gälte in jeder Auswertung als „keine Arbeit“ (0 Stunden) und stünde roh auf Seite und Zettel."""
    world = _world(tmp_db)
    status, location = handlers.handle_add_worktime(tmp_db, _form(world.person, kind=kind))
    assert status == 303 and _query(location)['error'] == ['Bitte eine gültige Art wählen.']
    assert _rows(tmp_db, world.person) == []
    entry = tmp_db.insert_worktime(world.person, '2026-03-12', start_time='08:00', end_time='09:00')
    before = tuple(tmp_db.get_worktime_by_id(entry))
    status, location = handlers.handle_update_worktime(tmp_db, _form(world.person, id=str(entry), date='2026-03-12',
                                                                     kind=kind))
    assert _query(location)['error'] == ['Bitte eine gültige Art wählen.']
    assert tuple(tmp_db.get_worktime_by_id(entry)) == before


def test_every_kind_of_the_form_is_accepted(tmp_db):
    from server.pages_worktime import KINDS
    world = _world(tmp_db)
    for number, (kind, _label) in enumerate(KINDS, start=1):
        status, location = handlers.handle_add_worktime(
            tmp_db, _form(world.person, kind=kind, date=f'2026-03-{number:02d}'))
        assert status == 303 and 'error' not in _query(location), kind
    assert len(_rows(tmp_db, world.person)) == len(KINDS)


def test_a_kind_from_the_database_is_escaped_on_the_page(tmp_db):
    """Altbestand oder SQL-Konsole: Die Art steht dann roh in der Spalte – auf der Seite maskiert."""
    from server.pages_worktime import PageWorkTimes
    world = _world(tmp_db)
    entry = tmp_db.insert_worktime(world.person, '2026-03-12', start_time='08:00', end_time='09:00')
    with tmp_db._get_connection() as connection:
        connection.execute('UPDATE WorkTimes SET Kind = ? WHERE ID = ?', ('"><img src=x onerror=alert(1)>', entry))
    html = PageWorkTimes(tmp_db, person_id=world.person, date_from=MARCH[0], date_to=MARCH[1])
    assert '<img src=x' not in html and '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;' in html


@pytest.mark.parametrize('customer_id', ['²', '１２', '٧', '-1', '1.0', 'x', ' ', '9' * 30])
def test_a_customer_field_that_is_no_plain_number_means_no_customer_and_never_an_error(tmp_db, customer_id):
    world = _world(tmp_db)
    status, location = handlers.handle_add_worktime(tmp_db, _form(world.person, customer_id=customer_id))
    assert status == 303 and 'error' not in _query(location)
    assert _rows(tmp_db, world.person)[0][4] is None


def test_the_sentence_names_no_part_of_the_note(tmp_db):
    world = _world(tmp_db)
    note = 'Vertraulich-Geheimnis ' * 30
    status, location = handlers.handle_add_worktime(tmp_db, _form(world.person, note=note))
    assert status == 303 and _query(location)['error'] == [MESSAGE]
    assert 'Vertraulich' not in location and 'Geheimnis' not in location and 'Vertraulich' not in MESSAGE


def test_the_refusal_keeps_the_period_the_person_and_a_valid_customer_filter(tmp_db):
    world = _world(tmp_db)
    base = {'person': [str(world.person)], 'from': [MARCH[0]], 'to': [MARCH[1]]}
    for customer, expected in (('7', {'customer': ['7']}), ('none', {'customer': ['none']}), ('', {}), ('abc', {}), ('07', {})):
        status, location = handlers.handle_add_worktime(tmp_db, _form(world.person, note='x' * 301, customer=customer))
        assert status == 303 and _query(location) == {**base, **expected, 'error': [MESSAGE]}, customer
    assert _rows(tmp_db, world.person) == []


# ── Ändern ────────────────────────────────────────────────────────────────────
def test_changing_an_entry_checks_the_note_too_and_leaves_the_entry_as_it_was(tmp_db):
    world = _world(tmp_db)
    entry = tmp_db.insert_worktime(world.person, '2026-03-10', start_time='08:00', end_time='09:00', note='kurz')
    before = tuple(tmp_db.get_worktime_by_id(entry))
    status, location = handlers.handle_update_worktime(
        tmp_db, _form(world.person, id=str(entry), note='x' * 301, end_time='10:00', customer='7'))
    assert status == 303 and _query(location)['error'] == [MESSAGE] and _query(location)['customer'] == ['7']
    assert tuple(tmp_db.get_worktime_by_id(entry)) == before            # weder die Notiz noch die Endzeit
    status, location = handlers.handle_update_worktime(tmp_db, _form(world.person, id=str(entry), note='x' * 300,
                                                                      end_time='10:00'))
    assert status == 303 and 'error' not in _query(location)
    row = tmp_db.get_worktime_by_id(entry)
    assert row[10] == 'x' * 300 and row[6] == '10:00'


# ── Reihenfolge: Uhrzeit, Notiz, Überschneidung ───────────────────────────────
def test_the_checks_run_in_the_order_time_then_note_then_overlap_when_adding(tmp_db):
    world = _world(tmp_db)
    tmp_db.insert_worktime(world.person, '2026-03-10', start_time='08:00', end_time='12:00')
    too_long, fine = 'x' * 301, 'x' * 300

    def error(**fields):
        status, location = handlers.handle_add_worktime(tmp_db, _form(world.person, **fields))
        assert status == 303
        return _query(location)['error'][0]

    assert error(start_time='abc', note=too_long) == handlers._WT_TIME_ERROR            # Uhrzeit zuerst
    assert error(start_time='09:00', end_time='10:00', note=too_long) == MESSAGE         # dann die Notiz, vor der Überschneidung
    assert error(start_time='09:00', end_time='10:00', note=fine).startswith('Überschneidung am 2026-03-10')
    assert len(_rows(tmp_db, world.person)) == 1


def test_the_checks_run_in_the_same_order_when_changing(tmp_db):
    world = _world(tmp_db)
    tmp_db.insert_worktime(world.person, '2026-03-10', start_time='08:00', end_time='12:00')
    other = tmp_db.insert_worktime(world.person, '2026-03-10', start_time='13:00', end_time='14:00')
    too_long, fine = 'x' * 301, 'x' * 300

    def error(**fields):
        status, location = handlers.handle_update_worktime(tmp_db, _form(world.person, id=str(other), **fields))
        assert status == 303
        return _query(location)['error'][0]

    assert error(start_time='abc', note=too_long) == handlers._WT_TIME_ERROR
    assert error(start_time='09:00', end_time='10:00', note=too_long) == MESSAGE
    assert error(start_time='09:00', end_time='10:00', note=fine).startswith('Überschneidung am 2026-03-10')
    row = tmp_db.get_worktime_by_id(other)
    assert (row[5], row[6], row[10]) == ('13:00', '14:00', '')


# ── Bestand ───────────────────────────────────────────────────────────────────
def test_a_longer_note_in_the_database_stays_and_the_pdf_wraps_it_with_an_ellipsis_after_eight_lines(tmp_db):
    world = _world(tmp_db)
    legacy = 'wort ' * 400                                              # 2000 Zeichen
    entry = tmp_db.insert_worktime(world.person, '2026-03-12', start_time='08:00', end_time='09:00', note=legacy)
    assert tmp_db.get_worktime_by_id(entry)[10] == legacy               # die Datenbankschicht begrenzt nichts, nichts wird gekürzt
    page = PageWorkTimes(tmp_db, person_id=world.person, date_from=MARCH[0], date_to=MARCH[1])
    assert f'data-id="{entry}"' in page and Dom(page).all('tr')
    pdf = generate_worktime_pdf(tmp_db, world.person, *MARCH, store=False, with_notes=True)[0]
    info = [text for _, text in _info_cells(_streams(pdf)[0])]
    assert info == info_lines(legacy) and len(info) == MAX_INFO_LINES and info[-1].endswith('…')
    # ein anderer Eintrag lässt den Bestand unberührt
    status, location = handlers.handle_add_worktime(tmp_db, _form(world.person, date='2026-03-13', note='kurz'))
    assert status == 303 and 'error' not in _query(location)
    assert tmp_db.get_worktime_by_id(entry)[10] == legacy


def test_changing_an_entry_with_a_longer_note_checks_it_and_names_the_limit(tmp_db):
    world = _world(tmp_db)
    legacy = 'wort ' * 400
    entry = tmp_db.insert_worktime(world.person, '2026-03-12', start_time='08:00', end_time='09:00', note=legacy)
    before = tuple(tmp_db.get_worktime_by_id(entry))
    status, location = handlers.handle_update_worktime(
        tmp_db, _form(world.person, id=str(entry), date='2026-03-12', note=legacy))          # unverändert zurückgeschickt
    assert status == 303 and _query(location)['error'] == [MESSAGE]
    assert tuple(tmp_db.get_worktime_by_id(entry)) == before
    status, location = handlers.handle_update_worktime(
        tmp_db, _form(world.person, id=str(entry), date='2026-03-12', note=legacy[:300]))   # gekürzt von Hand: geht
    assert status == 303 and 'error' not in _query(location)
    assert tmp_db.get_worktime_by_id(entry)[10] == legacy[:300].strip()


# ── Über den Server ───────────────────────────────────────────────────────────
def test_the_server_refuses_a_long_note_and_the_page_shows_the_sentence(server_port, auto_login):
    world = _world(auto_login.db)
    form = {'csrf': auto_login.csrf, 'person_id': str(world.person), 'date': '2026-03-10', 'kind': 'work',
            'start_time': '08:00', 'end_time': '09:00', 'location_mode': 'other', 'location_city': 'Kiel',
            'from': MARCH[0], 'to': MARCH[1]}
    refused = post(server_port, '/worktime/add', form={**form, 'note': 'x' * 301, 'customer': str(world.customer)})
    assert refused.status == 303
    location = refused.headers['Location']
    assert _query(location)['error'] == [MESSAGE] and _query(location)['customer'] == [str(world.customer)]
    assert _rows(auto_login.db, world.person) == []
    page = get(server_port, location)
    assert page.status == 200 and app_msg_arguments(Dom(page.text)) == [MESSAGE, 'error']
    accepted = post(server_port, '/worktime/add', form={**form, 'note': 'x' * 300})
    assert accepted.status == 303 and 'error' not in _query(accepted.headers['Location'])
    assert [row[10] for row in _rows(auto_login.db, world.person)] == ['x' * 300]
    entry = _rows(auto_login.db, world.person)[0][0]
    refused = post(server_port, '/worktime/update', form={**form, 'id': str(entry), 'note': 'y' * 301})
    assert refused.status == 303 and _query(refused.headers['Location'])['error'] == [MESSAGE]
    assert [row[10] for row in _rows(auto_login.db, world.person)] == ['x' * 300]
