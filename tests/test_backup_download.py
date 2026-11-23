# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Sicherungen herunterladen (2.5.5): ``GET /backup/download?file=<Name>``, die Liste auf der Seite *Sonstiges* und das, was
dafür in ``server/backup.py`` steht.

* Geliefert wird nur, was ``backup.downloadable_backups`` nennt: die Archive der Datensicherung und die Kopien der Datenbank
  vor einer Programm-Umstellung (``<Datum>_vor_<Version>_buch.db``) – reguläre Dateien direkt in ``backup/`` des angemeldeten
  Benutzers. Nie ``restore_*``/``replaced_*``, Teildateien, Ordner, Links, Namen mit Pfadanteil: immer dieselbe Antwort (404,
  ohne Pfad). Die Route liest nur, steht vor dem Öffnen der Datenbank und prüft die Herkunft (``Sec-Fetch-Site``).
* Die Datei geht in Stücken aus dem offenen Deskriptor hinaus, nie ganz durch den Speicher; eine laufende Sicherung oder
  Wiederherstellung hält das Öffnen auf (nach kurzem Warten 409), nicht aber das Senden. Je Stück gilt eine Zeitgrenze, und
  die Antwort trägt ``no-store``.
* ``create_backup`` räumt ein Teilarchiv weg, ``list_backups`` nennt weder Ordner noch Links.

Alle Namen und Inhalte sind erfundene Testwerte; 7-Zip ist abgeschaltet (Archive entstehen als .zip).
"""
import hashlib
import http.client
import os
import re
import socket
import time
import tracemalloc
import urllib.parse
from datetime import datetime
from types import SimpleNamespace

import pytest

import auth
import userctx
import version
from db import Database
from server import backup as backup_mod
from tests import installation
from tests.dom_helpers import Dom
from tests.http_client import FOREIGN_ORIGIN, TIMEOUT, KeepAlive, get, own_origin, post

NAME = '20260105_Backup.zip'
MIGRATION = '20261120_vor_2.5.0_buch.db'
CHUNK = backup_mod.DOWNLOAD_CHUNK


@pytest.fixture(autouse=True)
def no_sevenzip(monkeypatch):
    """Wie die übrigen Tests der Sicherung: kein 7-Zip, Archive entstehen über die Standardbibliothek (.zip)."""
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)


# ── Hilfen ───────────────────────────────────────────────────────────────────
def admin_dir():
    return os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN)


def backup_folder(user_dir=None):
    folder = os.path.join(user_dir or admin_dir(), 'backup')
    os.makedirs(folder, exist_ok=True)
    return folder


def put(name, content=b'', user_dir=None):
    path = os.path.join(backup_folder(user_dir), name)
    with open(path, 'wb') as handle:
        handle.write(content)
    return path


def put_sparse(name, size, user_dir=None):
    """Eine Datei dieser Größe, ohne dass die Platte sie schreibt (Loch)."""
    path = os.path.join(backup_folder(user_dir), name)
    with open(path, 'wb') as handle:
        handle.truncate(size)
    return path


def url(name):
    return '/backup/download?file=' + urllib.parse.quote(name, safe='')


def read(path):
    with open(path, 'rb') as handle:
        return handle.read()


def tree(folder):
    return sorted(os.listdir(folder))


def make_symlink(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'das Dateisystem kann keine symbolischen Links anlegen ({exc})')


def raw_get(port, path, read_bytes):
    """Eine GET-Anfrage über einen rohen Socket; liest nur die ersten ``read_bytes`` der Antwort und gibt Socket und Bytes zurück."""
    sock = socket.create_connection(('127.0.0.1', port), timeout=TIMEOUT)
    sock.sendall(f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n'.encode())
    received = b''
    while len(received) < read_bytes:
        block = sock.recv(read_bytes - len(received))
        if not block:
            break
        received += block
    return sock, received


# ── Eine Sicherung holen ─────────────────────────────────────────────────────
def test_a_backup_is_delivered_byte_for_byte_as_a_download(server_port, auto_login):
    archive, size = backup_mod.create_backup(admin_dir(), auto_login.db.db_name, 'all')
    name = os.path.basename(archive)
    resp = get(server_port, url(name))
    assert resp.status == 200
    assert resp.body == read(archive) and len(resp.body) == size > 0
    assert resp.headers['Content-Length'] == str(size)
    assert resp.headers['Content-Type'] == 'application/octet-stream'
    assert resp.headers['Content-Disposition'].startswith(f'attachment; filename="{name}"')
    assert resp.headers['X-Content-Type-Options'] == 'nosniff' and resp.headers['X-Frame-Options'] == 'DENY'


def test_the_connection_stays_usable_after_a_whole_download(server_port, auto_login):
    put(NAME, os.urandom(300_000))
    with KeepAlive(server_port) as conn:
        assert len(conn.get(url(NAME)).body) == 300_000
        assert conn.get('/about').status == 200


def test_a_range_header_is_ignored_and_the_whole_file_comes(server_port, auto_login):
    content = os.urandom(50_000)
    put(NAME, content)
    resp = get(server_port, url(NAME), headers={'Range': 'bytes=10-20'})
    assert resp.status == 200 and resp.body == content and 'Content-Range' not in resp.headers


def test_a_copy_before_a_migration_can_be_downloaded_but_not_restored(server_port, auto_login):
    content = os.urandom(20_000)
    put(MIGRATION, content)
    put(NAME, b'archiv')                                           # damit das Formular der Wiederherstellung da ist
    resp = get(server_port, url(MIGRATION))
    assert resp.status == 200 and resp.body == content
    assert resp.headers['Content-Disposition'].startswith(f'attachment; filename="{MIGRATION}"')
    page = get(server_port, '/miscellaneous').text
    assert 'vor Umstellung auf 2.5.0 (nur Datenbank)' in page and MIGRATION in page
    options = re.search(r'<select name="archive">(.*?)</select>', page, re.S).group(1)
    assert NAME in options and MIGRATION not in options            # zurückgespielt wird sie von Hand (README)


def test_the_copy_a_migration_really_writes_is_found_under_its_name(tmp_path):
    """Das Muster der Kopien (``_MIGRATION_NAME``) passt zum Namen, den ``db/schema.py`` vergibt – auch mit laufender Nummer."""
    user_dir = tmp_path / 'user'
    user_dir.mkdir()
    path = str(user_dir / 'buch.db')
    Database(db_name=path)
    for _ in range(2):
        installation.stamp_user_version(path, version.SCHEMA_VERSION - 1)
        Database(db_name=path)                                      # die Umstellung legt backup/<Datum>_vor_<Version>_buch.db an
    found = backup_mod.downloadable_backups(str(user_dir))
    assert len(found) == 2 and {entry.kind for entry in found} == {'migration'}
    assert {entry.version for entry in found} == {version.APP_VERSION}
    assert sorted(re.sub(r'^\d{8}', 'DATUM', entry.name) for entry in found) == [
        f'DATUM_vor_{version.APP_VERSION}_2_buch.db', f'DATUM_vor_{version.APP_VERSION}_buch.db']
    assert [name for name in tree(user_dir / 'backup') if name.endswith('.part')] == []


# ── Was sich nie holen lässt ─────────────────────────────────────────────────
OTHER_NAMES = [
    pytest.param('../buch.db', id='aufwaerts'),
    pytest.param('..%2Fbuch.db', id='aufwaerts-kodiert'),
    pytest.param('%2e%2e%5cbuch.db', id='aufwaerts-rueckstrich'),
    pytest.param('%2Fetc%2Fpasswd', id='absolut'),
    pytest.param('C%3A%5CWindows%5Cx_Backup.zip', id='laufwerk'),
    pytest.param('backup%2F{name}', id='mit-ordner'),
    pytest.param('{name}%00', id='nul'),
    pytest.param('{name}%5C..', id='rueckstrich'),
    pytest.param('{name}%2F', id='schraegstrich-am-ende'),
    pytest.param('buch.db', id='die-datenbank'),
    pytest.param('', id='leer'),
    pytest.param('a' * 300, id='zu-lang'),
    pytest.param('.versteckt_Backup.zip', id='versteckt'),
    pytest.param('replaced_x_Backup.zip', id='replaced-datei'),
    pytest.param('{name}.part', id='teildatei'),
    pytest.param('{name}.tmp', id='tmp-datei'),
    pytest.param('restore_x', id='restore-ordner'),
    pytest.param('replaced_x', id='replaced-ordner'),
    pytest.param('20261120_Backup_3.zip', id='ordner-mit-archivnamen'),
    pytest.param('20261120_vor_2.5.0_buch.db.part', id='halbe-kopie-vor-umstellung'),
    pytest.param('20261120_vor_2.5_buch.db', id='kopie-ohne-patchstelle'),
    pytest.param('20261120_vor_2.5.0_anders.db', id='kopie-anderer-datei'),
]


def plant_decoys():
    """Neben einer echten Sicherung: Dinge in backup/, die nie angeboten werden dürfen (Ordner, Verstecktes, Teildateien …)."""
    folder = backup_folder()
    for name in ('restore_x', 'replaced_x', '20261120_Backup_3.zip'):
        os.makedirs(os.path.join(folder, name, 'innen'))
    for name in ('.versteckt_Backup.zip', 'replaced_x_Backup.zip', NAME + '.part', NAME + '.tmp',
                 '20261120_vor_2.5.0_buch.db.part', '20261120_vor_2.5_buch.db', '20261120_vor_2.5.0_anders.db', 'notiz.txt'):
        put(name, b'nicht anbieten')
    put(NAME, b'die echte Sicherung')


@pytest.mark.parametrize('query', OTHER_NAMES)
def test_everything_but_a_listed_backup_is_the_same_404_without_a_path(server_port, auto_login, query):
    plant_decoys()
    raw = '/backup/download?file=' + query.replace('{name}', urllib.parse.quote(NAME, safe=''))
    resp = get(server_port, raw)
    assert resp.status == 404 and resp.text == backup_mod.DOWNLOAD_NOT_FOUND
    assert resp.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert userctx.DATA_ROOT not in resp.text
    assert get(server_port, url(NAME)).body == b'die echte Sicherung'


@pytest.mark.parametrize('path', ['/backup/download', '/backup/download?', '/backup/download?datei=x',
                                  '/backup/download?file=' + NAME + '&file=' + NAME,
                                  '/backup/download?file=' + NAME + '&file=nichts_Backup.zip'])
def test_no_name_or_two_names_is_a_404(server_port, auto_login, path):
    put(NAME, b'inhalt')
    resp = get(server_port, path)
    assert resp.status == 404 and resp.text == backup_mod.DOWNLOAD_NOT_FOUND


def test_a_link_in_backup_is_never_followed(server_port, auto_login):
    make_symlink(os.path.join('..', 'buch.db'), os.path.join(backup_folder(), '20261120_Backup_9.zip'))
    secret = read(auto_login.db.db_name)
    assert secret
    resp = get(server_port, url('20261120_Backup_9.zip'))
    assert resp.status == 404 and resp.text == backup_mod.DOWNLOAD_NOT_FOUND and secret not in resp.body
    assert backup_mod.downloadable_backups(admin_dir()) == [] and backup_mod.list_backups(admin_dir()) == []
    assert '20261120_Backup_9.zip' not in get(server_port, '/miscellaneous').text


@pytest.mark.parametrize('target', [os.path.join('..', 'buch.db'), 'andere_Backup.zip'],
                         ids=['Ziel-ausserhalb-von-backup', 'Ziel-in-backup'])
def test_a_link_that_replaces_the_file_after_the_list_is_not_followed(tmp_path, monkeypatch, target):
    """Zwischen der Liste und dem Öffnen wird die Datei durch einen Link ersetzt: Das Öffnen schlägt fehl – bei einem Ziel außerhalb
    von backup/ an der Prüfung des Ortes, bei einem Ziel darin an ``O_NOFOLLOW`` (wo es das Betriebssystem kennt)."""
    user_dir = tmp_path / 'user'
    (user_dir / 'backup').mkdir(parents=True)
    (user_dir / 'buch.db').write_bytes(b'die Datenbank')
    (user_dir / 'backup' / 'andere_Backup.zip').write_bytes(b'eine andere Sicherung')
    make_symlink(target, user_dir / 'backup' / NAME)
    entry = backup_mod.BackupEntry(NAME, 'archive', 13, 0.0, None)
    monkeypatch.setattr(backup_mod, 'downloadable_backups', lambda folder: [entry])
    with pytest.raises(backup_mod.BackupNotFound) as raised:
        backup_mod.open_download(str(user_dir), NAME)
    assert str(raised.value) == backup_mod.DOWNLOAD_NOT_FOUND and str(tmp_path) not in str(raised.value)


def test_backup_itself_may_be_a_link_to_another_place(server_port, auto_login, tmp_path):
    """Liegt backup/ auf einem anderen Laufwerk (Link auf einen Ordner), werden die Dateien darin trotzdem geliefert."""
    elsewhere = tmp_path / 'woanders'
    elsewhere.mkdir()
    (elsewhere / NAME).write_bytes(b'auf dem anderen Laufwerk')
    make_symlink(elsewhere, os.path.join(admin_dir(), 'backup'))
    assert get(server_port, url(NAME)).body == b'auf dem anderen Laufwerk'
    assert NAME in get(server_port, '/miscellaneous').text


# ── Wer holt was ─────────────────────────────────────────────────────────────
@pytest.fixture
def two_users():
    """Installation mit Anmeldung: ``admin`` und ``alice`` mit je einer Sitzung (Dummy-Zugangsdaten)."""
    installation.set_up(auto_login=False, users=[('alice', False)])
    return SimpleNamespace(admin={'Cookie': f"session={auth.create_session('admin')}"},
                           alice={'Cookie': f"session={auth.create_session('alice')}"},
                           admin_dir=admin_dir(), alice_dir=os.path.join(userctx.DATA_ROOT, 'users', 'alice'))


def test_a_user_only_gets_the_backups_of_the_own_folder(server_port, two_users):
    put(NAME, b'Sicherung des Administrators', user_dir=two_users.admin_dir)
    put('20260106_Backup.zip', b'nur beim Administrator', user_dir=two_users.admin_dir)
    assert get(server_port, url(NAME), two_users.admin).body == b'Sicherung des Administrators'
    for name in (NAME, '20260106_Backup.zip', f'../admin/backup/{NAME}'):
        resp = get(server_port, url(name), two_users.alice)
        assert resp.status == 404 and resp.text == backup_mod.DOWNLOAD_NOT_FOUND
    put(NAME, b'Sicherung von Alice', user_dir=two_users.alice_dir)             # derselbe Name im eigenen Ordner
    assert get(server_port, url(NAME), two_users.alice).body == b'Sicherung von Alice'
    assert get(server_port, url(NAME), two_users.admin).body == b'Sicherung des Administrators'
    assert '20260106_Backup.zip' not in get(server_port, '/miscellaneous', two_users.alice).text


def test_without_a_login_a_download_goes_to_the_login_page(server_port, two_users):
    put(NAME, b'geheim', user_dir=two_users.admin_dir)
    resp = get(server_port, url(NAME))
    assert resp.status == 303 and resp.headers['Location'] == '/login' and b'geheim' not in resp.body
    resp = get(server_port, url(NAME), {'Cookie': 'session=gibtsnicht'})
    assert resp.status == 303 and resp.headers['Location'] == '/login'


@pytest.mark.parametrize('headers, status', [
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, 403, id='cross-site'),
    pytest.param({'Sec-Fetch-Site': 'same-site'}, 403, id='same-site'),
    pytest.param({'Sec-Fetch-Site': 'unbekannt'}, 403, id='unbekannter-wert'),
    pytest.param({'Sec-Fetch-Site': 'same-origin'}, 200, id='same-origin'),
    pytest.param({'Sec-Fetch-Site': 'none'}, 200, id='none-eigene-eingabe'),
    pytest.param({}, 200, id='ohne-kopf'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, 403, id='fremder-origin-ohne-fetch-metadata'),
])
def test_a_download_from_a_foreign_page_is_refused(server_port, auto_login, headers, status):
    put(NAME, b'inhalt')
    resp = get(server_port, url(NAME), headers)
    assert resp.status == status
    if status == 403:
        assert resp.text == backup_mod.DOWNLOAD_FOREIGN and b'inhalt' not in resp.body
    else:
        assert resp.body == b'inhalt'
    resp = get(server_port, url(NAME), {'Origin': own_origin(server_port)})
    assert resp.status == 200                                        # die eigene Seite


def test_only_get_is_a_download(server_port, auto_login):
    """HEAD und POST liefern nichts aus (die Route liest nur, und nur per GET)."""
    put(NAME, b'inhalt')
    conn = http.client.HTTPConnection('127.0.0.1', server_port, timeout=TIMEOUT)
    try:
        conn.request('HEAD', url(NAME))
        assert conn.getresponse().status == 501
    finally:
        conn.close()
    resp = post(server_port, url(NAME), headers={'Origin': own_origin(server_port)}, csrf=auto_login.csrf)
    assert resp.status == 404 and b'inhalt' not in resp.body


# ── Sperre und Datenbank ─────────────────────────────────────────────────────
def test_a_download_is_refused_while_a_backup_or_restore_runs(server_port, auto_login, monkeypatch):
    monkeypatch.setattr(backup_mod, 'DOWNLOAD_WAIT_SECONDS', 0.2)       # das Öffnen wartet kurz, dann lehnt es ab
    put(NAME, b'inhalt')
    with backup_mod._exclusive(admin_dir()):
        resp = get(server_port, url(NAME))
        assert resp.status == 409 and resp.text == backup_mod.BUSY
    assert get(server_port, url(NAME)).body == b'inhalt'                # danach geht es


def test_a_running_download_does_not_hold_the_lock(server_port, auto_login):
    """Gelesen wird ohne die Sperre: Während ein großer Download läuft, lässt sich eine Sicherung anlegen."""
    put_sparse(NAME, 200 * 1024 ** 2)
    sock, head = raw_get(server_port, url(NAME), 1024)
    try:
        assert head.startswith(b'HTTP/1.1 200')                      # die Köpfe sind da, der Server hängt im Senden
        archive, size = backup_mod.create_backup(admin_dir(), auto_login.db.db_name, 'db')
        assert os.path.getsize(archive) == size
    finally:
        sock.close()


def test_a_backup_can_be_downloaded_although_the_database_cannot_be_opened(server_port, auto_login):
    archive, size = backup_mod.create_backup(admin_dir(), auto_login.db.db_name, 'all')
    installation.stamp_user_version(auto_login.db.db_name, version.SCHEMA_VERSION + 1)         # stammt aus einer neueren Version
    assert get(server_port, '/miscellaneous').status == 500
    resp = get(server_port, url(os.path.basename(archive)))
    assert resp.status == 200 and resp.body == read(archive)


# ── Große Dateien, Abbrüche ──────────────────────────────────────────────────
def test_copy_to_never_holds_more_than_one_chunk_of_a_large_file(tmp_path):
    size = 256 * 1024 ** 2
    path = tmp_path / 'gross.bin'
    with open(path, 'wb') as handle:
        handle.truncate(size)                                       # ein Loch, die Platte schreibt nichts
    sizes = []
    tracemalloc.start()
    try:
        with open(path, 'rb', buffering=0) as source:
            sent = backup_mod.copy_to(source, size, lambda block: sizes.append(len(block)))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert sent == sum(sizes) == size
    assert max(sizes) <= CHUNK and len(sizes) >= size // CHUNK
    assert peak < 4 * 1024 ** 2


def test_a_large_file_arrives_whole_over_http(server_port, auto_login):
    content = os.urandom(8 * 1024 ** 2 + 7)
    put(NAME, content)
    resp = get(server_port, url(NAME))
    assert resp.status == 200 and resp.headers['Content-Length'] == str(len(content))
    assert len(resp.body) == len(content) and hashlib.sha256(resp.body).digest() == hashlib.sha256(content).digest()


@pytest.mark.skipif(os.name == 'nt', reason='Unter Windows lässt sich eine geöffnete Datei nicht löschen')
def test_a_file_that_vanishes_after_opening_is_still_delivered_whole(tmp_path):
    user_dir = tmp_path / 'user'
    (user_dir / 'backup').mkdir(parents=True)
    content = os.urandom(3 * 1024 ** 2 + 5)
    (user_dir / 'backup' / NAME).write_bytes(content)
    source, entry = backup_mod.open_download(str(user_dir), NAME)
    os.remove(user_dir / 'backup' / NAME)
    collected = []
    with source:
        assert backup_mod.copy_to(source, entry.size, collected.append) == entry.size == len(content)
    assert b''.join(collected) == content


def test_open_download_reports_the_size_of_the_opened_file(tmp_path):
    user_dir = tmp_path / 'user'
    (user_dir / 'backup').mkdir(parents=True)
    (user_dir / 'backup' / NAME).write_bytes(b'x' * 1234)
    source, entry = backup_mod.open_download(str(user_dir), NAME)
    with source:
        assert entry == backup_mod.BackupEntry(NAME, 'archive', 1234, entry.mtime, None)
        assert source.read() == b'x' * 1234


@pytest.mark.parametrize('available, asked, expected', [(100, 250, 100), (300, 250, 250), (0, 10, 0), (10, 0, 0)])
def test_copy_to_sends_at_most_what_was_asked_and_stops_where_the_file_ends(tmp_path, available, asked, expected):
    path = tmp_path / 'datei'
    path.write_bytes(b'x' * available)
    collected = []
    with open(path, 'rb', buffering=0) as source:
        assert backup_mod.copy_to(source, asked, collected.append, chunk=64) == expected
    assert b''.join(collected) == b'x' * expected and all(len(block) <= 64 for block in collected)


def test_a_file_that_gets_shorter_while_sending_ends_the_answer_early_and_the_server_goes_on(
        server_port, auto_login, monkeypatch, capsys):
    path = put(NAME, b'z' * 1000)

    def shrinking(user_dir, name):
        return open(path, 'rb', buffering=0), backup_mod.BackupEntry(name, 'archive', 50_000, 0.0, None)
    monkeypatch.setattr(backup_mod, 'open_download', shrinking)
    conn = http.client.HTTPConnection('127.0.0.1', server_port, timeout=TIMEOUT)
    try:
        conn.request('GET', url(NAME))
        resp = conn.getresponse()
        assert resp.status == 200 and resp.getheader('Content-Length') == '50000'
        with pytest.raises(http.client.IncompleteRead) as raised:
            resp.read()
        assert raised.value.partial == b'z' * 1000
    finally:
        conn.close()
    assert get(server_port, '/about').status == 200                 # der Server antwortet danach weiter
    assert 'Download unvollständig (1000 von 50000 Bytes)' in capsys.readouterr().err


def test_a_client_that_hangs_up_does_not_hurt_the_server(server_port, auto_login, capsys):
    put_sparse(NAME, 100 * 1024 ** 2)
    sock, head = raw_get(server_port, url(NAME), 1024)
    assert head.startswith(b'HTTP/1.1 200')
    sock.close()                                                    # ungelesene Daten: der Server bekommt ein RST
    err, deadline = '', time.monotonic() + 15
    while 'Download abgebrochen' not in err and time.monotonic() < deadline:
        time.sleep(0.05)
        err += capsys.readouterr().err
    assert 'Download abgebrochen' in err and 'Traceback' not in err
    assert get(server_port, '/about').status == 200


def test_an_unexpected_error_before_the_headers_is_a_500_without_text_of_the_error(server_port, auto_login, monkeypatch,
                                                                                    capsys):
    def boom(user_dir, name):
        raise OSError('/geheim/pfad kaputt')
    monkeypatch.setattr(backup_mod, 'open_download', boom)
    resp = get(server_port, url(NAME))
    assert resp.status == 500 and 'geheim' not in resp.text and 'Server-Log' in resp.text
    assert '/geheim/pfad kaputt' in capsys.readouterr().err
    assert get(server_port, '/about').status == 200


# ── Die Liste auf der Seite ──────────────────────────────────────────────────
def test_the_page_lists_the_backups_with_link_date_and_size(server_port, auto_login):
    first = put(NAME, b'x' * 1536)
    second = put(MIGRATION, b'y' * (5 * 1024 ** 2))
    page = get(server_port, '/miscellaneous').text
    assert '<h3>Vorhandene Sicherungen</h3>' in page and 'Noch keine Sicherungen vorhanden.' not in page
    assert f'<a href="/backup/download?file={NAME}">Herunterladen</a>' in page
    assert f'<a href="/backup/download?file={MIGRATION}">Herunterladen</a>' in page
    row = re.search(rf'<tr><td>{re.escape(NAME)}</td>(.*?)</tr>', page).group(1)
    assert '<td>Sicherung</td>' in row and '<td class="num">2 KB</td>' in row
    assert datetime.fromtimestamp(os.path.getmtime(first)).strftime('%d.%m.%Y %H:%M') in row
    row = re.search(rf'<tr><td>{re.escape(MIGRATION)}</td>(.*?)</tr>', page).group(1)
    assert '<td>vor Umstellung auf 2.5.0 (nur Datenbank)</td>' in row and '<td class="num">5,0 MB</td>' in row
    assert datetime.fromtimestamp(os.path.getmtime(second)).strftime('%d.%m.%Y %H:%M') in row
    assert 'Sicherungen vor einer Programm-Umstellung stehen nicht im Formular der Wiederherstellung' in page
    assert 'Vorhandene Sicherungen stehen unten in der Liste.' in page


def test_the_hint_on_copies_before_a_migration_only_comes_with_such_a_copy(server_port, auto_login):
    put(NAME, b'x')
    page = get(server_port, '/miscellaneous').text
    assert NAME in page and 'Sicherungen vor einer Programm-Umstellung' not in page


def test_the_page_without_backups_says_so_and_shows_no_table(server_port, auto_login):
    page = get(server_port, '/miscellaneous').text
    assert 'Noch keine Sicherungen vorhanden.' in page and 'backupTable' not in page and 'Vorhandene Sicherungen<' not in page
    assert 'Noch keine Backups vorhanden.' in page                   # das Formular der Wiederherstellung wie bisher
    assert not os.path.exists(os.path.join(admin_dir(), 'backup'))   # die Seite legt backup/ nicht an


def test_the_page_lists_the_newest_backup_first_and_no_decoys(server_port, auto_login):
    plant_decoys()
    old, new = put('20250101_Backup.zip', b'a'), put('20261231_Backup_2.zip', b'b')
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(new, (2_000_000_000, 2_000_000_000))
    os.utime(os.path.join(backup_folder(), NAME), (1_500_000_000, 1_500_000_000))
    page = get(server_port, '/miscellaneous').text
    table = page.split('<table class="backupTable">')[1].split('</table>')[0]
    assert re.findall(r'<tr><td>(.*?)</td>', table) == ['20261231_Backup_2.zip', NAME, '20250101_Backup.zip']
    for decoy in ('restore_x', 'replaced_x', '.versteckt', 'notiz.txt', '.part', '.tmp', 'vor_2.5_buch', 'anders.db'):
        assert decoy not in table


@pytest.mark.skipif(os.name == 'nt', reason='Dieser Dateiname ist unter Windows nicht möglich')
def test_a_hostile_file_name_is_masked_in_text_and_attribute(server_port, auto_login):
    name = 'x"<b>\'_Backup.zip'
    put(name, b'inhalt')
    page = get(server_port, '/miscellaneous').text
    assert name not in page and '<b>\'_Backup' not in page
    assert 'x&quot;&lt;b&gt;&#x27;_Backup.zip' in page
    assert 'href="/backup/download?file=x%22%3Cb%3E%27_Backup.zip"' in page
    assert [attrs for tag, attrs in Dom(page).tags if tag == 'b'] == []        # kein eingeschleustes Element
    resp = get(server_port, '/backup/download?file=x%22%3Cb%3E%27_Backup.zip')
    assert resp.status == 200 and resp.body == b'inhalt'
    assert 'filename="x<b>\'_Backup.zip"' in resp.headers['Content-Disposition']          # das Anführungszeichen fällt weg
    assert "filename*=UTF-8''x%22%3Cb%3E%27_Backup.zip" in resp.headers['Content-Disposition']


@pytest.mark.parametrize('size, text', [
    (0, '1 KB'), (1, '1 KB'), (1024, '1 KB'), (1536, '2 KB'), (1024 ** 2 - 1, '1024 KB'),
    (1024 ** 2, '1,0 MB'), (1536 * 1024, '1,5 MB'), (1024 ** 3 - 1, '1024,0 MB'),
    (1024 ** 3, '1,0 GB'), (int(2.5 * 1024 ** 3), '2,5 GB'), (100 * 1024 ** 3, '100,0 GB'),
])
def test_size_label(size, text):
    from server.pages_miscellaneous import _size_label
    assert _size_label(size) == text


@pytest.mark.parametrize('timestamp, shown', [(10 ** 15, '–'), (-(10 ** 15), '–'), (float('inf'), '–'), (86400 * 365 * 40, None)])
def test_a_date_that_cannot_be_shown_is_a_dash_and_does_not_cost_the_page(timestamp, shown):
    from server.pages_miscellaneous import _date_label
    label = _date_label(timestamp)
    assert label == shown if shown else re.fullmatch(r'\d\d\.\d\d\.\d{4} \d\d:\d\d', label)


# ── backup.py ────────────────────────────────────────────────────────────────
def test_downloadable_backups_names_archives_and_copies_newest_first_and_creates_nothing(tmp_path):
    user_dir = str(tmp_path / 'user')
    assert backup_mod.downloadable_backups(user_dir) == [] and not os.path.exists(user_dir)
    os.makedirs(user_dir)
    assert backup_mod.downloadable_backups(user_dir) == [] and tree(user_dir) == []       # backup/ wird nicht angelegt
    folder = backup_folder(user_dir)
    for name, stamp in ((NAME, 100), ('20260105_Backup_2.7zip', 300), ('Backup.7z', 200), (MIGRATION, 400),
                        ('20261120_vor_2.5.0_2_buch.db', 500), ('20261120_vor_10.20.30_buch.db', 50)):
        put(name, b'x' * stamp, user_dir)
        os.utime(os.path.join(folder, name), (stamp, stamp))
    for name in ('buch.db', 'notiz.txt', 'archiv.zip', '20261120_vor_2.5.0_buch.db.part', 'x_Backup.zip.part'):
        put(name, b'nein', user_dir)                                  # kein Archivname (``Backup``) bzw. Teildatei
    found = backup_mod.downloadable_backups(user_dir)
    assert [(entry.name, entry.kind, entry.version, entry.size) for entry in found] == [
        ('20261120_vor_2.5.0_2_buch.db', 'migration', '2.5.0', 500), (MIGRATION, 'migration', '2.5.0', 400),
        ('20260105_Backup_2.7zip', 'archive', None, 300), ('Backup.7z', 'archive', None, 200),
        (NAME, 'archive', None, 100), ('20261120_vor_10.20.30_buch.db', 'migration', '10.20.30', 50)]


@pytest.mark.parametrize('name, plain', [
    (NAME, True), (MIGRATION, True), ('a b ä.zip', True), ('x' * 255, True), ('a.b.c', True),
    ('', False), ('x' * 256, False), (None, False), (5, False), (b'abc', False),
    ('a/b', False), ('/a', False), ('a\\b', False), ('..', False), ('../a', False), ('.versteckt', False),
    ('a\0b', False), ('C:x', False), ('restore_20260101', False), ('replaced_x', False), ('x.part', False), ('x.tmp', False),
])
def test_plain_name(name, plain):
    assert backup_mod._plain_name(name) is plain


def test_list_backups_names_neither_folders_nor_links(tmp_path):
    user_dir = str(tmp_path / 'user')
    folder = backup_folder(user_dir)
    put(NAME, b'archiv', user_dir)
    os.makedirs(os.path.join(folder, '20261120_Backup_3.zip', 'innen'))
    make_symlink(NAME, os.path.join(folder, '20261120_Backup_9.zip'))                  # zeigt auf eine echte Sicherung
    make_symlink(os.path.join('..', 'gibtsnicht'), os.path.join(folder, '20261120_Backup_8.zip'))
    assert backup_mod.list_backups(user_dir) == [NAME]


def test_restore_refuses_a_link_as_the_archive(tmp_path):
    user_dir = tmp_path / 'user'
    (user_dir / 'backup').mkdir(parents=True)
    (user_dir / 'notiz.txt').write_text('Notiz', encoding='utf-8')
    (user_dir / 'echt.zip').write_bytes(b'x')
    make_symlink(os.path.join('..', 'echt.zip'), user_dir / 'backup' / '20260105_Backup.zip')
    with pytest.raises(backup_mod.BackupNotFound, match=backup_mod.NOT_FOUND):
        backup_mod.restore_backup(str(user_dir), '20260105_Backup.zip')
    assert tree(user_dir) == ['backup', 'echt.zip', 'notiz.txt'] and tree(user_dir / 'backup') == ['20260105_Backup.zip']


def test_the_refusals_are_runtime_errors_and_a_missing_backup_is_also_a_file_not_found():
    refused, missing = backup_mod.BackupRefused('Klartext'), backup_mod.BackupNotFound('weg')
    assert isinstance(refused, RuntimeError) and not isinstance(refused, OSError)
    assert isinstance(missing, FileNotFoundError) and isinstance(missing, backup_mod.BackupRefused)
    assert isinstance(missing, RuntimeError) and str(missing) == 'weg'


def test_a_busy_folder_raises_the_refusal_with_the_plain_text(tmp_path):
    with backup_mod._exclusive(str(tmp_path)):
        with pytest.raises(backup_mod.BackupRefused) as raised:
            with backup_mod._exclusive(str(tmp_path)):
                pass
    assert str(raised.value) == backup_mod.BUSY and 'läuft bereits' in backup_mod.BUSY


# ── create_backup räumt ein Teilarchiv weg ───────────────────────────────────
def _small_user_dir(tmp_path):
    user_dir = tmp_path / 'user'
    user_dir.mkdir()
    from tests.backup_helpers import write_db
    write_db(user_dir / 'buch.db')
    (user_dir / 'notiz.txt').write_text('Notiz', encoding='utf-8')
    return str(user_dir)


@pytest.mark.parametrize('failing_call', [1, 2], ids=['erster-Aufruf', 'zweiter-Aufruf'])
def test_a_failed_sevenzip_run_leaves_no_partial_archive(tmp_path, monkeypatch, failing_call):
    user_dir = _small_user_dir(tmp_path)
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: '/fake/7z')
    calls = []

    def run(command, *args, **kwargs):
        calls.append(command)
        archive = command[command.index('-y') + 1]
        with open(archive, 'ab') as handle:                          # 7-Zip schreibt das Archiv unter dem endgültigen Namen
            handle.write(b'7z halb')
        return SimpleNamespace(returncode=2 if len(calls) == failing_call else 0, stdout=b'', stderr=b'ERROR: kaputt')
    monkeypatch.setattr(backup_mod.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='7-Zip-Fehler'):
        backup_mod.create_backup(user_dir, os.path.join(user_dir, 'buch.db'), 'all')
    assert len(calls) == failing_call
    assert tree(os.path.join(user_dir, 'backup')) == []
    assert backup_mod.list_backups(user_dir) == [] and backup_mod.downloadable_backups(user_dir) == []


def test_a_failed_zip_run_leaves_no_partial_archive_and_keeps_other_files(tmp_path, monkeypatch):
    user_dir = _small_user_dir(tmp_path)
    keep = put('20250101_Backup.zip', b'eine fruehere Sicherung', user_dir)
    real = backup_mod.zipfile.ZipFile.write

    def full_disk(self, filename, arcname=None, *args, **kwargs):
        if arcname == 'notiz.txt':
            raise OSError('Datenträger voll (Test)')
        return real(self, filename, arcname, *args, **kwargs)
    monkeypatch.setattr(backup_mod.zipfile.ZipFile, 'write', full_disk)
    with pytest.raises(OSError, match='Datenträger voll'):
        backup_mod.create_backup(user_dir, os.path.join(user_dir, 'buch.db'), 'all')
    assert tree(os.path.join(user_dir, 'backup')) == ['20250101_Backup.zip'] and read(keep) == b'eine fruehere Sicherung'
    monkeypatch.setattr(backup_mod.zipfile.ZipFile, 'write', real)
    archive, _ = backup_mod.create_backup(user_dir, os.path.join(user_dir, 'buch.db'), 'all')   # die Sperre ist frei
    assert os.path.isfile(archive)


# ── Nachgeschärft nach dem Review ────────────────────────────────────────────
def test_the_opening_waits_a_moment_for_the_lock_instead_of_refusing(tmp_path):
    """Zwei Downloads zur selben Zeit (Doppelklick, Download-Manager) halten die Sperre je nur für das Öffnen: Der zweite
    wartet kurz, statt mit „läuft bereits eine Sicherung“ abgelehnt zu werden. Sicherung und Wiederherstellung warten nie."""
    import threading
    put(NAME, b'inhalt', str(tmp_path))
    release = threading.Timer(0.3, lambda: lock.__exit__(None, None, None))
    lock = backup_mod._exclusive(str(tmp_path))
    lock.__enter__()
    release.start()
    try:
        source, entry = backup_mod.open_download(str(tmp_path), NAME)
        with source:
            assert source.read() == b'inhalt' and entry.size == 6
    finally:
        release.join()
    with backup_mod._exclusive(str(tmp_path)):
        started = time.monotonic()
        with pytest.raises(backup_mod.BackupRefused):
            with backup_mod._exclusive(str(tmp_path)):
                pass
        assert time.monotonic() - started < 0.2                     # ohne ``wait`` wird sofort abgelehnt


def test_a_download_is_never_stored_by_a_cache(server_port, auto_login):
    """Die Antwort trägt den ganzen Datenbestand: ``no-store`` statt des ``no-cache`` der Seiten – auch bei einer Ablehnung."""
    put(NAME, b'inhalt')
    assert get(server_port, url(NAME)).headers['Cache-Control'] == 'no-store'
    assert get(server_port, url('fehlt.zip')).headers['Cache-Control'] == 'no-store'
    assert get(server_port, '/about').headers['Cache-Control'] == 'no-cache'


def test_the_opened_file_is_read_blocking(tmp_path):
    """Geöffnet wird ohne zu warten (eine Pipe an der Stelle der Datei hielte sonst die Sperre), gelesen wird wartend:
    Ein Dateisystem, das das Kennzeichen beachtet, lieferte sonst ein leeres Lesen – für ``copy_to`` das Dateiende."""
    put(NAME, b'inhalt', str(tmp_path))
    source, _ = backup_mod.open_download(str(tmp_path), NAME)
    with source:
        assert os.get_blocking(source.fileno())


@pytest.mark.skipif(os.name == 'nt', reason='Dateinamen aus beliebigen Bytes gibt es nur auf POSIX-Dateisystemen')
def test_a_name_that_is_not_utf8_is_not_offered_and_does_not_cost_the_page(server_port, auto_login):
    folder = backup_folder()
    odd = os.path.join(os.fsencode(folder), b'2026_Backup_\xff.zip')
    try:
        with open(odd, 'wb') as handle:
            handle.write(b'x')
    except OSError as exc:
        pytest.skip(f'das Dateisystem nimmt den Namen nicht an ({exc})')
    put(NAME, b'inhalt')
    assert [entry.name for entry in backup_mod.downloadable_backups(admin_dir())] == [NAME]
    assert backup_mod.list_backups(admin_dir()) == [NAME]
    assert get(server_port, '/miscellaneous').status == 200
    with pytest.raises(backup_mod.BackupNotFound):
        backup_mod.open_download(admin_dir(), os.fsdecode(b'2026_Backup_\xff.zip'))


def test_a_client_that_stops_reading_is_dropped_after_the_send_timeout(server_port, auto_login, monkeypatch, capsys):
    """Ein Client, der die Antwort nicht mehr abholt, hält Verbindung und offene Datei nicht unbegrenzt: Nach der Zeitgrenze
    je Stück endet die Antwort unvollständig, und der Server arbeitet weiter."""
    monkeypatch.setattr(backup_mod, 'DOWNLOAD_SEND_TIMEOUT', 0.5)
    size = 400 * 1024 ** 2
    put_sparse(NAME, size)
    sock, head = raw_get(server_port, url(NAME), 1024)
    try:
        assert head.startswith(b'HTTP/1.1 200')
        time.sleep(2.5)                                             # nichts abholen: die Puffer laufen voll
        received = len(head)
        sock.settimeout(TIMEOUT)
        while True:
            block = sock.recv(1024 * 1024)
            if not block:
                break
            received += len(block)
    finally:
        sock.close()
    assert received < size                                          # der Server hat die Verbindung beendet
    assert get(server_port, '/about').status == 200
    assert 'Download abgebrochen' in capsys.readouterr().err
