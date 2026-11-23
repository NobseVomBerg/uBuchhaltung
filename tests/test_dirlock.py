# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Das Wiederherstellen läuft allein (2.5.6): die Sperre je Datenverzeichnis (``server/dirlock.py``) und ihr Einbau in den Server.

Fehlfall: Eine Wiederherstellung ersetzte das Datenverzeichnis eines Benutzers, während der Server weiter Anfragen desselben
Benutzers bearbeitete – sie lasen halb ersetzte Dateien oder schrieben in eine Datenbank, die gleich ersetzt wurde. Jetzt:

* Jede Anfrage eines angemeldeten Benutzers läuft unter der Lesesperre seines Datenverzeichnisses (``shared``), das Wiederherstellen
  nimmt die ausschließliche (``exclusive``): Neue Anfragen bekommen sofort 503 mit ``Retry-After``, auf laufende wartet es höchstens
  ``RESTORE_WAIT_SECONDS`` und lehnt sonst ab, ohne etwas zu ändern. Je Verzeichnis gibt es eine Sperre (auch bei anderer
  Schreibweise); verschiedene Benutzer sperren einander nicht.
* Ausgenommen sind der Download einer Sicherung und die statischen Dateien (nur GET); ohne angemeldeten Benutzer gibt es keine
  Sperre. Der Türsteher (``_gate``) läuft vor der Sperre.
* ``do_GET`` und ``do_POST`` bestehen nur noch aus ``_gate()`` und ``_run_locked(…)``: Kein Zweig kommt an der Sperre vorbei.

Alle Namen und Inhalte sind erfundene Testwerte; 7-Zip ist abgeschaltet (Archive entstehen als .zip).
"""
import ast
import contextlib
import http.client
import json
import os
import threading
import time
import urllib.parse

import pytest

import auth
import userctx
from server import app as server_app
from server import backup as backup_mod
from server import dirlock
from server import handlers
from tests import installation
from tests.backup_helpers import Archives, Blocker, Call, leftovers, snapshot, standard_files
from tests.http_client import FOREIGN_ORIGIN, TIMEOUT, KeepAlive, get, own_origin, post

NAME = '20260105_Backup.zip'
JOIN = 10                  # Sekunden, die ein Test auf einen Thread wartet, bevor er scheitert


@pytest.fixture(autouse=True)
def no_sevenzip(monkeypatch):
    """Wie die übrigen Tests der Sicherung: kein 7-Zip, Archive entstehen über die Standardbibliothek (.zip)."""
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)


# ── Hilfen ───────────────────────────────────────────────────────────────────
class Holder(threading.Thread):
    """Hält eine Sperre in einem eigenen Thread, bis ``release`` gerufen wird – so sehen der Server und jeder andere Thread sie als die
    einer fremden Anfrage. ``entered`` zeigt, dass sie gehalten wird; scheitert das Betreten, steht die Ausnahme in ``error``."""

    def __init__(self, lock):
        super().__init__(daemon=True)
        self._lock = lock
        self.entered = threading.Event()
        self._release = threading.Event()
        self.error = None

    def run(self):
        try:
            with self._lock():
                self.entered.set()
                self._release.wait(3 * JOIN)
        except BaseException as exc:             # noqa: BLE001 – der Test wertet sie aus
            self.error = exc

    def release(self):
        self._release.set()
        self.join(JOIN)
        assert not self.is_alive(), 'der Halter der Sperre wurde nicht fertig'


@contextlib.contextmanager
def held(lock):
    """Eine Sperre in einem anderen Thread halten (``lock``: Aufruf, der den Kontextmanager liefert)."""
    holder = Holder(lock)
    holder.start()
    assert holder.entered.wait(JOIN), f'die Sperre wurde nicht gehalten: {holder.error!r}'
    try:
        yield holder
    finally:
        holder.release()


def shared_held(path):
    return held(lambda: dirlock.shared(path))


def exclusive_held(path):
    return held(lambda: dirlock.exclusive(path))


def try_shared(path):
    """Eine Anfrage für ``path`` beginnen und sofort beenden; ``DirBusy``, wenn gerade wiederhergestellt wird."""
    with dirlock.shared(path):
        return 'lief'


def wait_until_restoring(path):
    """Wartet, bis neue Anfragen für ``path`` abgelehnt werden – bis also ein Wiederherstellen angelaufen ist (und auf die laufenden
    Anfragen wartet). Beobachtet wird nur das Verhalten der Sperre, mit einer Frist."""
    deadline = time.monotonic() + JOIN
    while time.monotonic() < deadline:
        try:
            try_shared(path)
        except dirlock.DirBusy:
            return
        time.sleep(0.002)
    raise AssertionError('das Wiederherstellen kam nie an die Sperre')


class RestoreSpy:
    """Ersetzt ``backup.restore_backup``: hält die Aufrufe fest und tut sonst nichts (``action`` läuft währenddessen, ``error`` wird
    danach geworfen)."""

    def __init__(self, action=None, error=None):
        self.calls = []
        self._action, self._error = action, error

    def __call__(self, user_dir, archive_name, wipe=False):
        self.calls.append((user_dir, archive_name, wipe))
        if self._action:
            self._action()
        if self._error:
            raise self._error


def admin_dir():
    return os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN)


def put_backup(name, content):
    folder = os.path.join(admin_dir(), 'backup')
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), 'wb') as handle:
        handle.write(content)


def exchange(port, method, path, headers):
    """Eine Anfrage mit genau diesen Kopfzeilen und OHNE den erklärten Body: Läse der Server ihn, bliebe er bis zum Timeout hängen.
    Liefert ``(Antwort-Objekt, Body)``."""
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=TIMEOUT)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders()
        resp = conn.getresponse()
        return resp, resp.read()
    finally:
        conn.close()


def location_query(resp):
    """Parameter der Weiterleitung auf die Seite *Sonstiges*."""
    assert resp.status == 303
    parts = urllib.parse.urlsplit(resp.headers['Location'])
    assert parts.path == '/miscellaneous'
    return urllib.parse.parse_qs(parts.query)


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / 'daten'
    path.mkdir()
    return str(path)


# ── Die reine Sperre ─────────────────────────────────────────────────────────
def test_two_requests_for_the_same_directory_run_at_the_same_time(folder):
    with shared_held(folder):
        assert try_shared(folder) == 'lief'                 # die zweite wartet nicht auf die erste


def test_a_request_may_nest_in_its_own_thread_and_the_count_stays_right(folder):
    with dirlock.shared(folder):
        with dirlock.shared(folder):
            pass
    with dirlock.exclusive(folder, timeout=0.3):            # eine verlorene Lesesperre hielte das Wiederherstellen auf
        pass


def test_exclusive_waits_for_a_running_request_and_then_runs(folder):
    ran = threading.Event()

    def restore():
        with dirlock.exclusive(folder, timeout=JOIN):
            ran.set()

    with shared_held(folder):
        waiting = Call(restore)
        waiting.start()
        wait_until_restoring(folder)
        assert not ran.is_set(), 'das Wiederherstellen begann, obwohl noch eine Anfrage lief'
    waiting.join(JOIN)
    assert waiting.error is None and ran.is_set()


def test_exclusive_gives_up_after_the_waiting_time_and_leaves_the_lock_free(folder):
    with shared_held(folder):
        started = time.monotonic()
        with pytest.raises(dirlock.DirBusy) as refused:
            with dirlock.exclusive(folder, timeout=0.3):
                pytest.fail('das Wiederherstellen begann trotz einer laufenden Anfrage')
        waited = time.monotonic() - started
        assert str(refused.value) == dirlock.WAIT_REFUSED
        assert 0.25 <= waited < 5
        assert try_shared(folder) == 'lief'                 # „wird wiederhergestellt“ gilt nicht mehr
    with dirlock.exclusive(folder, timeout=0.3):            # und ein späteres Wiederherstellen geht
        pass


def test_while_exclusive_is_held_a_request_is_refused_at_once_and_a_second_restore_too(folder):
    with exclusive_held(folder):
        started = time.monotonic()
        with pytest.raises(dirlock.DirBusy) as busy:
            try_shared(folder)
        assert time.monotonic() - started < 1, 'die Anfrage wartete, statt sofort abgelehnt zu werden'
        assert str(busy.value) == dirlock.RESTORE_RUNNING
        with pytest.raises(dirlock.DirBusy) as again:
            with dirlock.exclusive(folder, timeout=0.3):
                pytest.fail('ein zweites Wiederherstellen begann neben dem ersten')
        assert str(again.value) == dirlock.ALREADY_RESTORING
        with pytest.raises(dirlock.DirBusy):                # die Ablehnung nahm dem laufenden nicht sein Kennzeichen
            try_shared(folder)
    assert try_shared(folder) == 'lief'


def test_a_new_request_is_refused_while_exclusive_still_waits_for_a_running_one(folder):
    ran = threading.Event()

    def restore():
        with dirlock.exclusive(folder, timeout=JOIN):
            ran.set()

    with shared_held(folder):
        waiting = Call(restore)
        waiting.start()
        wait_until_restoring(folder)
        with pytest.raises(dirlock.DirBusy) as busy:        # Vorrang des Wiederherstellens: keine neue Anfrage schiebt sich davor
            try_shared(folder)
        assert str(busy.value) == dirlock.RESTORE_RUNNING
        assert not ran.is_set()
    waiting.join(JOIN)
    assert waiting.error is None and ran.is_set()
    assert try_shared(folder) == 'lief'


def test_the_thread_that_holds_a_request_may_restore_and_nest_inside(folder):
    with dirlock.shared(folder):                            # die Anfrage, die selbst wiederherstellt
        with dirlock.exclusive(folder, timeout=0.3):        # sie wartet nicht auf ihre eigene Lesesperre
            with dirlock.shared(folder):                    # und darf darin weiter verschachteln
                pass
            probe = Call(try_shared, folder)                # jeder andere Thread bleibt ausgeschlossen
            probe.start()
            probe.join(JOIN)
            assert isinstance(probe.error, dirlock.DirBusy)
    assert try_shared(folder) == 'lief'


def test_a_running_request_may_still_nest_while_a_restore_waits_for_it(folder):
    inside, go, ran = threading.Event(), threading.Event(), threading.Event()

    def request():
        with dirlock.shared(folder):
            inside.set()
            assert go.wait(JOIN)
            with dirlock.shared(folder):                    # eine Ablehnung hier ließe die laufende Anfrage mittendrin scheitern
                return 'verschachtelt'

    def restore():
        with dirlock.exclusive(folder, timeout=JOIN):
            ran.set()

    running = Call(request)
    running.start()
    assert inside.wait(JOIN)
    waiting = Call(restore)
    waiting.start()
    wait_until_restoring(folder)
    go.set()
    running.join(JOIN)
    waiting.join(JOIN)
    assert running.error is None and running.value == 'verschachtelt'
    assert waiting.error is None and ran.is_set()


def test_different_directories_do_not_lock_each_other(tmp_path):
    first, second = str(tmp_path / 'erste'), str(tmp_path / 'zweite')
    os.makedirs(first)
    os.makedirs(second)
    with exclusive_held(first):
        assert try_shared(second) == 'lief'
        with dirlock.exclusive(second, timeout=0.3):
            pass


def test_two_spellings_of_one_directory_are_one_lock(folder):
    spellings = [os.path.join(folder, '..', os.path.basename(folder)), os.path.join(folder, ''), os.path.join(folder, '.')]
    with exclusive_held(folder):
        for spelling in spellings:
            with pytest.raises(dirlock.DirBusy):
                try_shared(spelling)
    with exclusive_held(spellings[0]):
        with pytest.raises(dirlock.DirBusy):
            try_shared(folder)


def test_a_link_to_a_directory_is_the_same_lock_as_the_directory(folder, tmp_path):
    link = str(tmp_path / 'verweis')
    try:
        os.symlink(folder, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'das Dateisystem kann keine symbolischen Links anlegen ({exc})')
    with exclusive_held(folder):
        with pytest.raises(dirlock.DirBusy):
            try_shared(link)
    with exclusive_held(link):
        with pytest.raises(dirlock.DirBusy):
            try_shared(folder)


def test_the_lock_is_free_again_after_an_error_in_the_body(folder):
    with pytest.raises(RuntimeError, match='Anfrage'):
        with dirlock.shared(folder):
            raise RuntimeError('Fehler in der Anfrage')
    with dirlock.exclusive(folder, timeout=0.3):            # eine verlorene Lesesperre hielte das Wiederherstellen auf
        pass
    with pytest.raises(RuntimeError, match='Wiederherstellen'):
        with dirlock.exclusive(folder, timeout=0.3):
            raise RuntimeError('Fehler im Wiederherstellen')
    assert try_shared(folder) == 'lief'                     # nicht „wird wiederhergestellt“


def test_the_waiting_time_is_read_at_every_call(folder, monkeypatch):
    monkeypatch.setattr(dirlock, 'RESTORE_WAIT_SECONDS', 0.2)
    with shared_held(folder):
        started = time.monotonic()
        with pytest.raises(dirlock.DirBusy) as refused:
            with dirlock.exclusive(folder):
                pytest.fail('das Wiederherstellen begann trotz einer laufenden Anfrage')
        assert str(refused.value) == dirlock.WAIT_REFUSED
        assert time.monotonic() - started < 5               # nicht die 30 Sekunden des Standards


def test_the_limits_and_the_three_sentences_are_what_the_documentation_names():
    assert (dirlock.RESTORE_WAIT_SECONDS, dirlock.RETRY_AFTER_SECONDS) == (30, 15)
    assert (dirlock.DEMO_WAIT_SECONDS, dirlock.DEMO_COOLDOWN_SECONDS) == (2, 60)
    sentences = {dirlock.RESTORE_RUNNING, dirlock.WAIT_REFUSED, dirlock.ALREADY_RESTORING, dirlock.COOLING_DOWN}
    assert len(sentences) == 4 and all(sentence.endswith('.') for sentence in sentences)
    assert issubclass(dirlock.DirBusy, Exception)


# ── Der Handler ──────────────────────────────────────────────────────────────
@pytest.fixture
def handler_world(tmp_path, monkeypatch):
    """Das Datenverzeichnis, das der Handler ohne Benutzer im Kontext nimmt, und ein Spion statt ``restore_backup``."""
    user_dir = str(tmp_path / 'nutzer')
    os.makedirs(os.path.join(user_dir, 'backup'))
    with open(os.path.join(user_dir, 'backup', NAME), 'wb') as handle:
        handle.write(b'archiv')                             # der Handler fragt vor der Sperre, ob es das Archiv gibt
    monkeypatch.setattr(userctx, 'user_data_dir', lambda: user_dir)
    spy = RestoreSpy()
    monkeypatch.setattr(backup_mod, 'restore_backup', spy)
    return user_dir, spy


def test_the_handler_called_directly_holds_no_request_lock_and_still_restores(handler_world):
    """Ohne ``_run_locked`` (Tests, Werkzeuge) hält der Thread keine Lesesperre – das Wiederherstellen läuft trotzdem."""
    user_dir, spy = handler_world
    assert handlers.handle_backup_restore(None, {'archive': [NAME], 'mode': ['wipe']}) == (
        303, f'/miscellaneous?restore=ok&file={NAME}')
    assert spy.calls == [(user_dir, NAME, True)]
    assert try_shared(user_dir) == 'lief'                   # die Sperre ist wieder frei


def test_a_second_restore_is_answered_with_the_sentence_of_the_lock(handler_world):
    user_dir, spy = handler_world
    with exclusive_held(user_dir):
        status, location = handlers.handle_backup_restore(None, {'archive': [NAME]})
    parts = urllib.parse.urlsplit(location)
    assert (status, parts.path) == (303, '/miscellaneous')
    assert urllib.parse.parse_qs(parts.query) == {'restore': ['error'], 'msg': [dirlock.ALREADY_RESTORING]}
    assert spy.calls == []


def test_a_restore_that_cannot_wait_for_the_running_requests_is_an_error_redirect(handler_world, monkeypatch):
    user_dir, spy = handler_world
    monkeypatch.setattr(dirlock, 'RESTORE_WAIT_SECONDS', 0.2)
    with shared_held(user_dir):
        status, location = handlers.handle_backup_restore(None, {'archive': [NAME]})
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert status == 303 and query == {'restore': ['error'], 'msg': [dirlock.WAIT_REFUSED]}
    assert spy.calls == []


@pytest.mark.parametrize('error, message', [
    pytest.param(backup_mod.BackupRefused('Das Archiv ist beschädigt.'), 'Das Archiv ist beschädigt.', id='abgelehnt'),
    pytest.param(OSError('/geheim/pfad voll'), handlers.RESTORE_FAILED, id='unerwarteter-Fehler'),
])
def test_the_lock_is_free_again_after_a_failed_restore(handler_world, monkeypatch, error, message, capsys):
    user_dir, _ = handler_world
    failing = RestoreSpy(error=error)
    monkeypatch.setattr(backup_mod, 'restore_backup', failing)
    status, location = handlers.handle_backup_restore(None, {'archive': [NAME]})
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert status == 303 and query == {'restore': ['error'], 'msg': [message]}
    assert failing.calls == [(user_dir, NAME, False)]
    assert try_shared(user_dir) == 'lief'
    with dirlock.exclusive(user_dir, timeout=0.3):
        pass
    capsys.readouterr()


# ── Über den echten Server ───────────────────────────────────────────────────
PAGES = ['/about', '/', '/dashboard', '/miscellaneous', '/invoice', '/transactions', '/masterdata/contacts', '/assets',
         '/worktime', '/trips', '/data/logos/logo.png', '/invoice/pdf_download?id=1',
         '/masterdata/contacts/check-abbreviation?value=ab']


@pytest.mark.parametrize('path', PAGES)
def test_a_page_of_the_user_is_answered_with_503_while_the_directory_is_restored(server_port, auto_login, path):
    with exclusive_held(admin_dir()):
        resp = get(server_port, path)
    assert resp.status == 503
    assert resp.text == dirlock.RESTORE_RUNNING
    assert resp.headers['Retry-After'] == '15'
    # Klartext, auch dort, wo die Seite sonst JSON liest (Kürzel-Prüfung: ein JSON ohne „exists“ zeigte fälschlich „✓“)
    assert resp.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert resp.headers['X-Content-Type-Options'] == 'nosniff' and resp.headers['X-Frame-Options'] == 'DENY'


def test_a_503_for_a_page_keeps_the_connection_open_and_everything_answers_again_afterwards(server_port, auto_login):
    with KeepAlive(server_port) as conn:
        with exclusive_held(admin_dir()):
            assert conn.get('/about').status == 503
        assert conn.get('/about').status == 200             # dieselbe Verbindung, die Sperre ist frei
    assert post(server_port, '/quote/delete?id=999', csrf=auto_login.csrf).status == 303


PLAIN_POSTS = ['/quote/delete?id=999', '/masterdata/articles/add', '/backup/create', '/backup/restore', '/backup/delete', '/db_export',
               '/backup/download?file=x']                    # an der Adresse des Downloads gilt die Ausnahme nur für GET


@pytest.mark.parametrize('path', PLAIN_POSTS)
def test_a_form_post_is_answered_with_503_without_reading_the_body_and_the_connection_closes(server_port, auto_login, path):
    headers = {'Origin': own_origin(server_port), 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': '40'}
    with exclusive_held(admin_dir()):
        resp, body = exchange(server_port, 'POST', path, headers)
    assert resp.status == 503 and body.decode() == dirlock.RESTORE_RUNNING
    assert resp.getheader('Retry-After') == '15'
    assert resp.getheader('Content-Type') == 'text/plain; charset=utf-8'
    assert resp.will_close and resp.getheader('Connection') == 'close'          # der Body blieb ungelesen
    assert post(server_port, '/quote/delete?id=999', csrf=auto_login.csrf).status == 303      # die nächste Anfrage geht


JSON_POSTS = [*sorted(server_app._JSON_PATHS), *(f'{path}?id=1' for path in sorted(server_app._GENERATE_PATHS)),
              *(f'{path}?slot=client' for path in sorted(server_app._UPLOAD_PATHS)),
              '/upload_receipts', '/masterdata/contacts/upload-logo', '/confirm_transactions', '/execute_sql']


@pytest.mark.parametrize('path', JSON_POSTS)
def test_a_post_of_a_page_that_reads_json_gets_a_json_503(server_port, auto_login, path):
    """Die Seiten lesen ``success``/``error`` (Rechnungen, Angebote, Upload), ``ok``/``error`` (Import der Auszüge) und
    ``errors`` (SQL-Konsole): Die Antwort trägt alle drei."""
    headers = {'Origin': own_origin(server_port), 'Content-Type': 'application/json', 'Content-Length': '40'}
    with exclusive_held(admin_dir()):
        resp, body = exchange(server_port, 'POST', path, headers)
    assert resp.status == 503 and resp.getheader('Content-Type') == 'application/json'
    assert json.loads(body) == {'success': False, 'ok': False, 'error': dirlock.RESTORE_RUNNING,
                                'errors': [dirlock.RESTORE_RUNNING]}
    assert resp.getheader('Retry-After') == '15'
    assert resp.will_close and resp.getheader('Connection') == 'close'


@pytest.mark.parametrize('path', ['/receipts/search?q=a', '/transactions/search?q=a', '/receipts/suggestions?id={receipt}'])
def test_a_search_of_a_page_gets_a_json_503_while_the_directory_is_restored(server_port, auto_login, path):
    """Die Suchen der Belege und Buchungen (seit 2.5.12) und die Vorschläge zu einem Beleg (seit 2.6.0) sind GET, aber die Seiten lesen JSON
    (``success``/``error``): Auch die 503 trägt es – ein Klartext ergäbe dort „kein JSON“."""
    auto_login.db.insert_receipt('26B001', '2026-03-04', 'beleg.pdf', '', '')        # damit die Vorschläge nach der Sperre mit 200 antworten
    path = path.format(receipt=auto_login.db.fetch_receipts()[-1][0])
    with exclusive_held(admin_dir()):
        resp = get(server_port, path)
    assert resp.status == 503 and resp.headers['Content-Type'] == 'application/json'
    assert json.loads(resp.text) == {'success': False, 'ok': False, 'error': dirlock.RESTORE_RUNNING,
                                     'errors': [dirlock.RESTORE_RUNNING]}
    assert resp.headers['Retry-After'] == '15'
    assert get(server_port, path).status == 200                      # danach ist die Sperre frei, die Suche antwortet


def test_the_static_files_and_the_download_of_a_backup_are_served_during_a_restore(server_port, auto_login):
    """Der Download liest nur aus backup/ (das eine Wiederherstellung nie anfasst) und kann lange dauern: Er darf sie nicht aufhalten."""
    content = os.urandom(20_000)
    put_backup(NAME, content)
    with exclusive_held(admin_dir()):
        css = get(server_port, '/buch.css')
        icon = get(server_port, '/favicon.ico')
        download = get(server_port, '/backup/download?file=' + urllib.parse.quote(NAME, safe=''))
    assert css.status == 200 and css.headers['Content-Type'] == 'text/css' and css.body
    assert icon.status == 200 and icon.body
    assert download.status == 200 and download.body == content


def test_the_gate_runs_before_the_lock(server_port, auto_login):
    """Der Türsteher braucht das Datenverzeichnis nicht: Eine fremde Herkunft bekommt auch während der Wiederherstellung ihre 403."""
    with exclusive_held(admin_dir()):
        resp = post(server_port, '/quote/delete?id=1', headers={'Origin': FOREIGN_ORIGIN}, csrf=auto_login.csrf)
    assert resp.status == 403


def test_without_a_logged_in_user_there_is_no_lock(server_port):
    installation.set_up(auto_login=False)
    with exclusive_held(admin_dir()):
        assert get(server_port, '/login').status == 200
        answer = post(server_port, '/login', headers={'Origin': own_origin(server_port)},
                      form={'username': installation.ADMIN, 'password': 'falsch-und-ausgedacht'})
    assert answer.status != 503 and 'Retry-After' not in answer.headers


def test_another_user_stays_served_while_the_directory_of_one_is_restored(server_port):
    installation.set_up(auto_login=False, users=[('zweite', False)])
    cookies = {name: {'Cookie': f'session={auth.create_session(name)}'} for name in (installation.ADMIN, 'zweite')}
    with exclusive_held(admin_dir()):
        assert get(server_port, '/about', cookies[installation.ADMIN]).status == 503
        assert get(server_port, '/about', cookies['zweite']).status == 200
    assert get(server_port, '/about', cookies[installation.ADMIN]).status == 200


def test_a_real_restore_over_http_succeeds_and_the_server_answers_normally_afterwards(server_port, auto_login):
    user = admin_dir()
    auto_login.db.insert_article(name='Probeartikel', unit_price=10, tax_rate=19)
    archive, _ = backup_mod.create_backup(user, auto_login.db.db_name, 'all')
    auto_login.db.insert_article(name='Zweiter Artikel', unit_price=20, tax_rate=19)
    name = os.path.basename(archive)
    resp = post(server_port, '/backup/restore', csrf=auto_login.csrf, form={'archive': name, 'mode': 'wipe'})
    assert location_query(resp) == {'restore': ['ok'], 'file': [name]}      # die Anfrage wartete nicht auf ihre eigene Lesesperre
    assert get(server_port, '/about').status == 200
    assert get(server_port, '/masterdata/articles').status == 200
    assert [row[1] for row in installation.open_db(installation.ADMIN).fetch_articles()] == ['Probeartikel']
    assert leftovers(user) == []
    with dirlock.exclusive(user, timeout=0.3):                              # nichts blieb gesperrt
        pass


def test_other_requests_get_503_while_the_real_restore_runs(server_port, auto_login, monkeypatch):
    """Mit dem echten ``restore_backup``, angehalten mitten im Entpacken: Auch die innere Sperre (``backup._exclusive``) hält."""
    user = admin_dir()
    archive, _ = backup_mod.create_backup(user, auto_login.db.db_name, 'all')
    name = os.path.basename(archive)
    blocker = Blocker()
    monkeypatch.setattr(backup_mod, '_extract', blocker.wrap(backup_mod._extract))
    restoring = Call(post, server_port, '/backup/restore', csrf=auto_login.csrf, form={'archive': name, 'mode': 'overwrite'})
    restoring.start()
    try:
        assert blocker.entered.wait(JOIN), 'die Wiederherstellung kam nie in _extract an'
        resp = get(server_port, '/about')
        assert resp.status == 503 and resp.text == dirlock.RESTORE_RUNNING and resp.headers['Retry-After'] == '15'
        with pytest.raises(backup_mod.BackupRefused, match='läuft bereits'):
            backup_mod.create_backup(user, auto_login.db.db_name, 'db')
    finally:
        blocker.proceed.set()
        restoring.join(3 * JOIN)
    assert restoring.error is None
    assert location_query(restoring.value) == {'restore': ['ok'], 'file': [name]}
    assert get(server_port, '/about').status == 200


def test_a_restore_that_cannot_start_because_another_request_hangs_is_refused_and_changes_nothing(
        server_port, auto_login, monkeypatch, tmp_path):
    monkeypatch.setattr(dirlock, 'RESTORE_WAIT_SECONDS', 0.3)
    user = admin_dir()
    name = Archives('zip', tmp_path).build(user, standard_files(tmp_path))
    spy = RestoreSpy()
    monkeypatch.setattr(backup_mod, 'restore_backup', spy)
    before = snapshot(user)
    with shared_held(user):                                                 # eine andere Anfrage, die nicht fertig wird
        resp = post(server_port, '/backup/restore', csrf=auto_login.csrf, form={'archive': name, 'mode': 'wipe'})
    assert location_query(resp) == {'restore': ['error'], 'msg': [dirlock.WAIT_REFUSED]}
    assert spy.calls == [], 'restore_backup wurde trotz der laufenden Anfrage aufgerufen'
    assert snapshot(user) == before and leftovers(user) == []
    resp = post(server_port, '/backup/restore', csrf=auto_login.csrf, form={'archive': name, 'mode': 'wipe'})
    assert location_query(resp) == {'restore': ['ok'], 'file': [name]}      # die Sperre blieb nicht hängen
    assert spy.calls == [(user, name, True)]


def test_restore_backup_runs_under_the_exclusive_lock(server_port, auto_login, monkeypatch):
    seen = {}

    def during_the_restore():
        probe = Call(try_shared, admin_dir())
        probe.start()
        probe.join(JOIN)
        seen['probe'] = probe.error
        seen['http'] = get(server_port, '/about')                           # eine Anfrage desselben Benutzers, mitten im Wiederherstellen

    spy = RestoreSpy(action=during_the_restore)
    monkeypatch.setattr(backup_mod, 'restore_backup', spy)
    put_backup(NAME, b'archiv')
    resp = post(server_port, '/backup/restore', csrf=auto_login.csrf, form={'archive': NAME, 'mode': 'wipe'})
    assert location_query(resp) == {'restore': ['ok'], 'file': [NAME]}
    assert spy.calls == [(admin_dir(), NAME, True)]
    assert isinstance(seen['probe'], dirlock.DirBusy) and str(seen['probe']) == dirlock.RESTORE_RUNNING
    assert seen['http'].status == 503 and seen['http'].text == dirlock.RESTORE_RUNNING


# ── Nachgeschärft nach dem Review ────────────────────────────────────────────
def test_a_running_http_request_holds_the_lock_until_it_has_answered(server_port, auto_login, monkeypatch):
    """Die Zusage über HTTP, nicht nur mit einem Halter-Thread: Solange der Server eine Anfrage des Benutzers bearbeitet, beginnt
    kein Wiederherstellen – prüfte ``_run_locked`` die Sperre nur und gäbe sie vor der Bearbeitung wieder frei, gelänge es hier."""
    blocker = Blocker()
    monkeypatch.setattr(server_app.pages, 'PageAbout', blocker.wrap(server_app.pages.PageAbout))
    running = Call(get, server_port, '/about')
    running.start()
    try:
        assert blocker.entered.wait(JOIN), 'die Anfrage kam nie im Handler an'
        with pytest.raises(dirlock.DirBusy) as refused:
            with dirlock.exclusive(admin_dir(), timeout=0.3):
                pytest.fail('das Wiederherstellen begann, während eine Anfrage lief')
        assert str(refused.value) == dirlock.WAIT_REFUSED
    finally:
        blocker.proceed.set()
        running.join(JOIN)
    assert running.error is None and running.value.status == 200
    with dirlock.exclusive(admin_dir(), timeout=JOIN):                      # nach der Antwort ist das Verzeichnis frei
        pass


def test_a_real_restore_waits_for_a_running_http_request_and_then_runs(server_port, auto_login, monkeypatch):
    blocker = Blocker()
    monkeypatch.setattr(server_app.pages, 'PageAbout', blocker.wrap(server_app.pages.PageAbout))
    spy = RestoreSpy()
    monkeypatch.setattr(backup_mod, 'restore_backup', spy)
    put_backup(NAME, b'archiv')
    running = Call(get, server_port, '/about')
    running.start()
    assert blocker.entered.wait(JOIN), 'die Anfrage kam nie im Handler an'
    restoring = Call(post, server_port, '/backup/restore', csrf=auto_login.csrf, form={'archive': NAME, 'mode': 'wipe'})
    restoring.start()
    try:
        wait_until_restoring(admin_dir())                                   # es wartet auf die laufende Anfrage …
        assert spy.calls == []                                              # … und hat noch nichts angefasst
    finally:
        blocker.proceed.set()
        running.join(JOIN)
        restoring.join(3 * JOIN)
    assert running.value.status == 200 and restoring.error is None
    assert location_query(restoring.value) == {'restore': ['ok'], 'file': [NAME]}
    assert spy.calls == [(admin_dir(), NAME, True)]


def test_an_archive_that_does_not_exist_is_refused_before_the_lock_and_holds_nobody_up(handler_world, monkeypatch):
    """Ein Versuch, der nie beginnen kann, setzt das Kennzeichen nicht: Liefe daneben eine andere Anfrage, bekämen sonst alle
    übrigen bis zum Ende der Wartezeit 503."""
    user_dir, spy = handler_world
    taken = []
    real = dirlock.exclusive
    monkeypatch.setattr(dirlock, 'exclusive', lambda *args, **kwargs: taken.append(args) or real(*args, **kwargs))
    with shared_held(user_dir):
        started = time.monotonic()
        status, location = handlers.handle_backup_restore(None, {'archive': ['gibt-es-nicht.zip'], 'mode': ['wipe']})
        assert time.monotonic() - started < 5                               # nicht gewartet
        assert try_shared(user_dir) == 'lief'
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert status == 303 and query == {'restore': ['error'], 'msg': [backup_mod.NOT_FOUND]}
    assert taken == [] and spy.calls == []


def test_a_link_as_archive_is_refused_before_the_lock(handler_world):
    user_dir, spy = handler_world
    link = os.path.join(user_dir, 'backup', '20260106_Backup.zip')
    try:
        os.symlink(os.path.join(user_dir, 'backup', NAME), link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'das Dateisystem kann keine symbolischen Links anlegen ({exc})')
    status, location = handlers.handle_backup_restore(None, {'archive': ['20260106_Backup.zip']})
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert status == 303 and query == {'restore': ['error'], 'msg': [backup_mod.NOT_FOUND]} and spy.calls == []


def test_after_a_refusal_with_cooldown_the_next_attempt_is_refused_at_once_and_holds_nobody_up(folder):
    with shared_held(folder):
        with pytest.raises(dirlock.DirBusy) as first:
            with dirlock.exclusive(folder, timeout=0.2, cooldown=30):
                pytest.fail('das Wiederherstellen begann trotz einer laufenden Anfrage')
        assert str(first.value) == dirlock.WAIT_REFUSED
        started = time.monotonic()
        with pytest.raises(dirlock.DirBusy) as second:
            with dirlock.exclusive(folder, timeout=5, cooldown=30):
                pytest.fail('das Wiederherstellen begann in der Abkühlzeit')
        assert str(second.value) == dirlock.COOLING_DOWN and time.monotonic() - started < 2
        assert try_shared(folder) == 'lief'                                 # das Kennzeichen wurde nie gesetzt
    with pytest.raises(dirlock.DirBusy, match='gerade abgelehnt'):
        with dirlock.exclusive(folder):                                     # auch ohne laufende Anfrage: erst nach der Abkühlzeit
            pytest.fail('das Wiederherstellen begann in der Abkühlzeit')


def test_the_cooldown_ends_and_without_one_the_next_attempt_runs_at_once(folder, tmp_path):
    with shared_held(folder):
        with pytest.raises(dirlock.DirBusy):
            with dirlock.exclusive(folder, timeout=0.1, cooldown=0.3):
                pytest.fail('das Wiederherstellen begann trotz einer laufenden Anfrage')
    deadline = time.monotonic() + JOIN
    while True:
        try:
            with dirlock.exclusive(folder, timeout=0.1):
                break
        except dirlock.DirBusy as busy:
            assert str(busy) == dirlock.COOLING_DOWN and time.monotonic() < deadline
            time.sleep(0.05)
    other = str(tmp_path / 'anderes')
    os.makedirs(other)
    with shared_held(other):
        with pytest.raises(dirlock.DirBusy):
            with dirlock.exclusive(other, timeout=0.1):                     # ohne Abkühlzeit (der gewöhnliche Betrieb)
                pytest.fail('das Wiederherstellen begann trotz einer laufenden Anfrage')
    with dirlock.exclusive(other, timeout=0.1):
        pass


@pytest.mark.parametrize('demo_active, expected', [
    (False, {'timeout': None, 'cooldown': 0}),
    (True, {'timeout': dirlock.DEMO_WAIT_SECONDS, 'cooldown': dirlock.DEMO_COOLDOWN_SECONDS}),
])
def test_in_demo_mode_the_restore_waits_briefly_and_rests_after_a_refusal(handler_world, monkeypatch, demo_active, expected):
    """Im Demo-Modus teilen sich alle Besucher ein Datenverzeichnis: Eine Folge von Versuchen, die nie beginnen können, darf die
    anderen nicht dauerhaft mit 503 aufhalten."""
    user_dir, spy = handler_world
    seen = []
    real = dirlock.exclusive
    monkeypatch.setattr(handlers.demo, 'is_active', lambda: demo_active)
    monkeypatch.setattr(dirlock, 'exclusive', lambda path, **kwargs: seen.append(kwargs) or real(path, **kwargs))
    assert handlers.handle_backup_restore(None, {'archive': [NAME]})[1] == f'/miscellaneous?restore=ok&file={NAME}'
    assert seen == [expected] and spy.calls == [(user_dir, NAME, False)]


# ── Statisch: nichts kommt an der Sperre vorbei ──────────────────────────────
def _method_body(name):
    """Die Anweisungen einer Methode von ``SimpleWebServer`` (ohne Docstring) als Quelltext."""
    with open(server_app.__file__, encoding='utf-8') as handle:
        tree = ast.parse(handle.read())
    server = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'SimpleWebServer')
    body = next(node for node in server.body if isinstance(node, ast.FunctionDef) and node.name == name).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return [ast.unparse(statement) for statement in body]


GATE = 'if not self._gate():\n    return'


def test_do_get_and_do_post_consist_only_of_the_gate_and_the_lock():
    assert _method_body('do_GET') == [GATE, 'self._run_locked(self._dispatch_get)']
    assert _method_body('do_POST') == [GATE, 'self._run_locked(self._dispatch_post)']


def test_the_server_answers_no_other_method_than_get_and_post():
    """Ein ``do_HEAD`` oder ``do_PUT`` käme an ``_run_locked`` vorbei – wer eines braucht, nimmt es unter die Sperre."""
    assert sorted(name for name in vars(server_app.SimpleWebServer) if name.startswith('do_')) == ['do_GET', 'do_POST']


def test_backup_py_never_takes_the_directory_lock():
    """Reihenfolge der Sperren: ``dirlock`` außen, ``backup._exclusive`` innen – nie umgekehrt."""
    with open(backup_mod.__file__, encoding='utf-8') as handle:
        assert 'dirlock' not in handle.read()
