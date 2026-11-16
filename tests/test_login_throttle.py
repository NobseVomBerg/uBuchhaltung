# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Drosselung der Anmeldung (2.3.6): ``auth.attempt_login`` und die Antworten von ``POST /login``.

* Vom fünften Fehlversuch in Folge an ist der Name gesperrt: 30 s, dann jeweils doppelt, höchstens 900 s. In der
  Sperre wird nicht geprüft (auch das richtige Passwort nicht), nicht gezählt und nichts verlängert; ein Erfolg
  vergisst die Fehlversuche, ein Eintrag verfällt 900 s nach dem letzten Fehlversuch bzw. dem Ende der Sperre.
* Ob es den Namen gibt, sieht man nicht: unbekannte Namen verhalten sich genauso. Ihre Einträge sind auf
  ``LOGIN_TRACK_MAX`` begrenzt und verdrängen nie den eines vorhandenen Benutzers.
* Der Fehlversuch wird gezählt, bevor gehasht wird, mit einem der zwei scrypt-Plätze in der Hand: Auch
  gleichzeitige Versuche zählen, und sind beide Plätze belegt, ist die Antwort „ausgelastet“ ohne zu zählen.
* Neue Passwörter brauchen 8 Zeichen; bestehende kürzere bleiben gültig.

Die Uhr gibt der Test mit ``now=`` vor (Sekunden wie ``time.monotonic``); der Zustand liegt im Speicher und
wird vor jedem Test geleert (conftest.py). Namen und Passwörter sind erfundene Testwerte.
"""
import contextlib
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

import auth
from server import handlers
from server.handlers import LoginOutcome
from server.pages_login import PageLogin
from tests import installation
from tests.http_client import get, own_origin, post

GOOD = 'richtig-1234'
WRONG = 'falsch-12345'


@pytest.fixture(autouse=True)
def _auth_tables():
    """Die Tabellen der auth.db gibt es im Betrieb immer, sobald man sich anmelden kann."""
    auth.init_auth_db()


@pytest.fixture
def alice():
    auth.create_user('alice', GOOD)


@pytest.fixture
def hashes(monkeypatch):
    """Zählt die scrypt-Läufe ab hier (die Fixture ``alice`` legt ihren Benutzer vorher an)."""
    calls = []
    real = auth._scrypt

    def counting(password, salt):
        calls.append(1)
        return real(password, salt)
    monkeypatch.setattr(auth, '_scrypt', counting)
    return calls


def attempt(name, password, now):
    return auth.attempt_login(name, password, now=now)


def nothing_remembered():
    return not auth._known_failures and not auth._unknown_failures


# ── Sperrdauer ───────────────────────────────────────────────────────────────
def test_the_block_starts_with_the_fifth_failure_and_doubles_up_to_the_maximum(alice):
    t = 1000.0
    for _ in range(4):
        assert attempt('alice', WRONG, t) == ('wrong', 0)
        t += 1
    for seconds in (30, 60, 120, 240, 480, 900, 900, 900):
        assert attempt('alice', WRONG, t) == ('blocked', seconds)
        t += seconds                                  # die Sperre ist genau abgelaufen


def test_a_correct_login_makes_the_server_forget_the_failures(alice):
    for _ in range(4):
        assert attempt('alice', WRONG, 0) == ('wrong', 0)
    assert attempt('alice', GOOD, 0) == ('ok', 0)
    for _ in range(4):
        assert attempt('alice', WRONG, 0) == ('wrong', 0)             # wieder bei null
    assert attempt('alice', WRONG, 0) == ('blocked', 30)


def test_during_the_block_nothing_is_checked_counted_or_extended(alice, hashes):
    for _ in range(5):
        result = attempt('alice', WRONG, 0)
    assert result == ('blocked', 30)
    runs = len(hashes)
    assert attempt('alice', GOOD, 10) == ('blocked', 20)              # auch das richtige Passwort kommt nicht durch
    assert attempt('alice', WRONG, 29.5) == ('blocked', 1)            # aufgerundet
    for _ in range(20):
        assert attempt('alice', WRONG, 5)[0] == 'blocked'
    assert len(hashes) == runs                                        # nichts davon wurde gehasht
    # Nach dem Ende der Sperre ist es der sechste Fehlversuch (60 s) – die Versuche in der Sperre zählten nicht
    assert attempt('alice', WRONG, 30) == ('blocked', 60)


@pytest.mark.parametrize('name', ['alice', 'niemand'])
def test_an_unknown_name_gets_the_same_answers_and_work_as_an_existing_one(alice, hashes, name):
    results = [attempt(name, WRONG, 0) for _ in range(4)]
    results += [attempt(name, WRONG, 0), attempt(name, GOOD, 0)]
    assert results == [('wrong', 0)] * 4 + [('blocked', 30)] * 2
    assert len(hashes) == 5                                           # fünf Läufe, beim Unbekannten mit Dummy-Hash


def test_the_names_are_counted_together_whatever_their_case(alice):
    for name in ('ALICE', 'Alice', 'alice', 'aLICE'):
        assert attempt(name, WRONG, 0) == ('wrong', 0)
    assert attempt('alice', WRONG, 0) == ('blocked', 30)
    assert set(auth._known_failures) == {'alice'} and not auth._unknown_failures


@pytest.mark.parametrize('name, password', [
    pytest.param('', WRONG, id='ohne-Namen'),
    pytest.param('ab', WRONG, id='zu-kurz'),
    pytest.param('x' * 33, WRONG, id='zu-lang'),
    pytest.param('a b c', WRONG, id='Leerzeichen'),
    pytest.param('ali/ce', WRONG, id='Schrägstrich'),
    pytest.param('alice', '', id='leeres-Passwort'),
    pytest.param('alice', None, id='kein-Passwort'),
])
def test_what_cannot_be_a_login_is_refused_without_hashing_and_without_a_trace(alice, hashes, name, password):
    for _ in range(10):
        assert attempt(name, password, 0) == ('wrong', 0)
    assert hashes == [] and nothing_remembered()


def test_the_reserved_names_count_like_any_other_name():
    # „demo“ gibt es im Demo-Modus als Benutzer: Es hat das Format eines Namens, auch wenn man es nicht anlegen darf
    for _ in range(5):
        result = attempt('demo', WRONG, 0)
    assert result == ('blocked', 30)


# ── Vergessen ────────────────────────────────────────────────────────────────
def test_failures_are_forgotten_after_a_while(alice):
    for _ in range(4):
        attempt('alice', WRONG, 0)
    later = auth.LOGIN_FORGET_AFTER + 1
    for _ in range(4):
        assert attempt('alice', WRONG, later) == ('wrong', 0)
    assert attempt('alice', WRONG, later) == ('blocked', 30)


@pytest.mark.parametrize('after_the_block, expected', [
    pytest.param(899, ('blocked', 60), id='noch-gemerkt'),
    pytest.param(901, ('wrong', 0), id='vergessen'),
])
def test_the_time_to_forget_counts_from_the_end_of_the_block(alice, after_the_block, expected):
    for _ in range(5):
        attempt('alice', WRONG, 0)                    # Sperre bis 30
    assert attempt('alice', WRONG, 30 + after_the_block) == expected


# ── Tabellen ─────────────────────────────────────────────────────────────────
def test_the_unknown_names_stay_few_and_never_push_out_an_existing_user(alice, monkeypatch):
    monkeypatch.setattr(auth, 'LOGIN_TRACK_MAX', 5)
    for _ in range(4):
        attempt('alice', WRONG, 0)
    for i in range(40):
        attempt(f'fremd{i:02d}', WRONG, 1 + i)
        assert len(auth._unknown_failures) <= 5
    assert auth._known_failures['alice'].count == 4
    assert attempt('alice', WRONG, 50) == ('blocked', 30)             # seine vier Fehlversuche sind nicht weggespült


def test_a_full_table_drops_the_expired_entries_first_and_then_the_oldest(monkeypatch):
    monkeypatch.setattr(auth, 'LOGIN_TRACK_MAX', 3)
    for i, t in enumerate((0, 100, 200)):
        attempt(f'fremd{i}', WRONG, t)
    attempt('fremd3', WRONG, 250)                     # voll, nichts verfallen: der älteste geht
    assert set(auth._unknown_failures) == {'fremd1', 'fremd2', 'fremd3'}
    attempt('fremd4', WRONG, 1100)                    # fremd1 und fremd2 sind seit 1000 bzw. 1100 verfallen
    assert set(auth._unknown_failures) == {'fremd3', 'fremd4'}


def test_a_name_that_exists_by_now_moves_its_failures_to_the_protected_table():
    for _ in range(2):
        attempt('spaeter', WRONG, 0)                  # noch kein Benutzer: unbekannter Name
    assert 'spaeter' in auth._unknown_failures
    auth.create_user('spaeter', GOOD)
    attempt('spaeter', WRONG, 1)
    assert 'spaeter' in auth._known_failures and 'spaeter' not in auth._unknown_failures
    assert auth._known_failures['spaeter'].count == 3


# ── scrypt-Plätze ────────────────────────────────────────────────────────────
def test_both_hash_slots_taken_means_busy_and_nothing_is_counted(alice, monkeypatch):
    monkeypatch.setattr(auth, 'LOGIN_SLOT_WAIT', 0.05)
    assert auth._HASH_SLOTS.acquire(timeout=5) and auth._HASH_SLOTS.acquire(timeout=5)
    try:
        assert attempt('alice', GOOD, 0) == ('busy', 0)
        assert nothing_remembered()
    finally:
        auth._HASH_SLOTS.release()
        auth._HASH_SLOTS.release()
    assert attempt('alice', GOOD, 0) == ('ok', 0)


def test_a_free_slot_is_taken_back_after_every_login(alice):
    for _ in range(5):                                # fünf Läufe, zwei Plätze: bliebe einer belegt, hinge es hier
        attempt('alice', WRONG, 0)
    for _ in range(2):
        assert auth._HASH_SLOTS.acquire(timeout=5)
    auth._HASH_SLOTS.release()
    auth._HASH_SLOTS.release()


def test_hashing_waits_for_a_slot_or_gives_up():
    assert auth._HASH_SLOTS.acquire(timeout=5) and auth._HASH_SLOTS.acquire(timeout=5)
    try:
        with pytest.raises(auth.LoginBusy):
            auth._hash('irgendwas', b'\x00' * 16, wait=0.05)
    finally:
        auth._HASH_SLOTS.release()
        auth._HASH_SLOTS.release()
    assert len(auth._hash('irgendwas', b'\x00' * 16)) == 32


def test_a_block_that_starts_while_waiting_for_a_slot_is_honoured(alice, hashes, monkeypatch):
    for _ in range(4):
        attempt('alice', WRONG, 0)
    runs = len(hashes)
    real_row = auth._user_row

    def row_then_block(username, db_path=None):
        row = real_row(username, db_path)             # der Versuch hat seinen Platz, …
        auth._known_failures['alice'].blocked_until = 30.0        # … da löst ein paralleler Versuch die Sperre aus
        return row
    monkeypatch.setattr(auth, '_user_row', row_then_block)
    assert attempt('alice', GOOD, 0) == ('blocked', 30)           # trotz richtigem Passwort
    assert len(hashes) == runs and auth._known_failures['alice'].count == 4


def test_parallel_attempts_are_counted_before_they_are_hashed(alice, monkeypatch):
    for _ in range(3):
        attempt('alice', WRONG, 0)
    meeting = threading.Barrier(2, timeout=10)

    def both_inside(password, salt):
        meeting.wait()                                # erst wenn beide Läufe zugleich im Hash sind, geht es weiter
        return b'\x00' * 32
    monkeypatch.setattr(auth, '_scrypt', both_inside)
    results = []
    workers = [threading.Thread(target=lambda: results.append(attempt('alice', WRONG, 0))) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    assert sorted(results) == [('blocked', 30), ('wrong', 0)]         # der vierte und der fünfte Fehlversuch


# ── handle_login und die Anmeldeseite ────────────────────────────────────────
def test_handle_login_for_a_correct_and_a_wrong_password(alice):
    assert handlers.handle_login({'username': [' alice '], 'password': [GOOD]}) == \
        LoginOutcome(True, 'alice', None, 200, 0)
    assert handlers.handle_login({'username': ['alice'], 'password': [WRONG]}) == \
        LoginOutcome(False, 'alice', 'Benutzername oder Passwort falsch.', 200, 0)
    assert handlers.handle_login({'password': [WRONG]}) == \
        LoginOutcome(False, '', 'Benutzername oder Passwort falsch.', 200, 0)


@pytest.mark.parametrize('seconds, text', [
    (1, '1 Sekunde'), (30, '30 Sekunden'), (59, '59 Sekunden'), (60, '1 Minute'), (61, '2 Minuten'),
    (120, '2 Minuten'), (900, '15 Minuten'),
])
def test_the_wait_is_named_in_seconds_below_a_minute_and_in_rounded_up_minutes_above(monkeypatch, seconds, text):
    monkeypatch.setattr(auth, 'attempt_login', lambda *args, **kwargs: ('blocked', seconds))
    outcome = handlers.handle_login({'username': ['alice'], 'password': [WRONG]})
    assert outcome == LoginOutcome(
        False, 'alice', f'Zu viele fehlgeschlagene Anmeldeversuche. Bitte in {text} erneut versuchen.', 429, seconds)


def test_handle_login_for_a_busy_server(monkeypatch):
    monkeypatch.setattr(auth, 'attempt_login', lambda *args, **kwargs: ('busy', 0))
    assert handlers.handle_login({'username': ['alice'], 'password': [WRONG]}) == LoginOutcome(
        False, 'alice', 'Der Server ist gerade ausgelastet. Bitte gleich noch einmal versuchen.', 503, 5)


def test_a_new_block_is_logged_with_the_client_and_never_with_the_typed_name(alice, capsys):
    typed = 'hunter2xyz'                              # ein Passwort im Namensfeld ist keine Seltenheit
    for _ in range(5):
        handlers.handle_login({'username': [typed], 'password': ['x' * 8]}, client='203.0.113.7')
    logged = capsys.readouterr().out
    assert logged.count('Anmeldung gesperrt') == 1                    # einmal, beim Beginn
    assert '203.0.113.7' in logged and '30 s' in logged
    assert typed not in logged and 'x' * 8 not in logged
    handlers.handle_login({'username': [typed], 'password': ['x' * 8]}, client='203.0.113.7')
    assert capsys.readouterr().out == ''                              # kein weiteres Protokoll in der Sperre


def test_the_login_page_escapes_the_typed_name_and_the_message():
    html = PageLogin(error_msg='<b>Achtung</b>', username='"><script>alert(1)</script>')
    assert '<script>alert(1)</script>' not in html
    assert 'value="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in html
    assert '&lt;b&gt;Achtung&lt;/b&gt;' in html and '<b>Achtung</b>' not in html


# ── über HTTP ────────────────────────────────────────────────────────────────
@pytest.fixture
def login_site():
    installation.set_up(auto_login=False)


def login(port, password):
    return post(port, '/login', headers={'Origin': own_origin(port)},
                form={'username': 'admin', 'password': password})


def test_over_http_the_fifth_failure_is_429_with_retry_after(server_port, login_site):
    for _ in range(4):
        resp = login(server_port, WRONG)
        assert resp.status == 200 and 'Benutzername oder Passwort falsch.' in resp.text
        assert 'Retry-After' not in resp.headers
    resp = login(server_port, WRONG)
    assert resp.status == 429
    assert resp.headers['Retry-After'] == '30'
    assert 'Zu viele fehlgeschlagene Anmeldeversuche. Bitte in 30 Sekunden erneut versuchen.' in resp.text
    assert 'Set-Cookie' not in resp.headers
    assert 'value="admin"' in resp.text                               # der Name bleibt im Feld


def test_over_http_the_block_holds_against_the_right_password_too(server_port, login_site):
    for _ in range(5):
        login(server_port, WRONG)
    resp = login(server_port, installation.PASSWORD)
    assert resp.status == 429 and 'Set-Cookie' not in resp.headers
    assert 1 <= int(resp.headers['Retry-After']) <= 30
    assert installation.session_count() == 0


def test_over_http_a_correct_login_after_four_failures_still_works(server_port, login_site):
    for _ in range(4):
        login(server_port, WRONG)
    resp = login(server_port, installation.PASSWORD)
    assert resp.status == 303 and resp.headers['Set-Cookie'].startswith('session=')
    assert login(server_port, WRONG).status == 200                    # wieder bei null


def test_over_http_another_name_is_not_blocked(server_port, login_site):
    for _ in range(5):
        login(server_port, WRONG)
    resp = post(server_port, '/login', headers={'Origin': own_origin(server_port)},
                form={'username': 'nobody', 'password': WRONG})
    assert resp.status == 200                                         # eigener Zähler
    assert get(server_port, '/login').status == 200                   # die Anmeldeseite selbst bleibt erreichbar


def test_over_http_a_busy_server_is_503_with_retry_after(server_port, login_site, monkeypatch):
    monkeypatch.setattr(auth, 'LOGIN_SLOT_WAIT', 0.05)
    assert auth._HASH_SLOTS.acquire(timeout=5) and auth._HASH_SLOTS.acquire(timeout=5)
    try:
        resp = login(server_port, installation.PASSWORD)
    finally:
        auth._HASH_SLOTS.release()
        auth._HASH_SLOTS.release()
    assert resp.status == 503 and resp.headers['Retry-After'] == '5'
    assert 'Der Server ist gerade ausgelastet.' in resp.text and 'Set-Cookie' not in resp.headers
    assert login(server_port, installation.PASSWORD).status == 303    # danach geht es wieder


# ── Mindestlänge ─────────────────────────────────────────────────────────────
def test_a_new_password_needs_eight_characters():
    with pytest.raises(ValueError, match='mindestens 8 Zeichen'):
        auth.create_user('bob', '1234567')
    auth.create_user('bob', '12345678')
    with pytest.raises(ValueError, match='mindestens 8 Zeichen'):
        auth.set_password('bob', '1234567')
    auth.set_password('bob', 'abcdefgh')
    assert auth.authenticate('bob', 'abcdefgh') is True


def test_the_user_management_says_so(alice):
    location = handlers.handle_user_reset_password({'username': ['alice'], 'password': ['1234567']})
    assert 'mindestens 8 Zeichen' in parse_qs(urlsplit(location).query)['err'][0]
    assert auth.authenticate('alice', GOOD) is True                   # unverändert


def test_a_shorter_password_that_exists_stays_valid():
    auth.create_user('bob', '12345678')
    salt = b'\x01' * 16
    with contextlib.closing(sqlite3.connect(auth.AUTH_DB)) as con:    # ein Bestand aus der Zeit der Mindestlänge 6
        con.execute("UPDATE Users SET PwHash = ?, Salt = ? WHERE Username = 'bob'",
                    (auth._hash('kurz12', salt), salt))
        con.commit()
    assert auth.authenticate('bob', 'kurz12') is True
    assert attempt('bob', 'kurz12', 0) == ('ok', 0)
