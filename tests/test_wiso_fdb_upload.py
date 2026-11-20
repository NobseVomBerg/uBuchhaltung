# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Import aus der WISO-Datenbank per Upload statt über einen Serverpfad (2.3.7).

Der Browser legt ``DB1.FDB`` (und optional ``DB0.FDB``) je als rohen Body über ``POST /wiso/fdb-upload`` ab; das Formular
des Imports (``POST /wiso/fdb-import``) nennt danach nur die Kennung des Zwischenordners (``upload``). Geprüft wird:

* ``server/wiso_upload.py``: Kennung, Zwischenordner, Aufräumen (``begin``/``sweep``), stückweises Schreiben, Platzprüfung;
* die Route gegen einen echten Server: Token, Herkunft, Demo-Modus, Slot und Kennung, Länge (nichts wird gelesen, bevor sie
  stimmt), freier Platz, zu kurzer Body, Schreibfehler ohne Serverpfad in der Antwort;
* der Handler: Der Zwischenordner ist nach jedem Import weg (Erfolg, Blocker, Fehler, Ablehnung), und kein Pfad des
  Zwischenordners steht in Antwort, Weiterleitung oder ``wiso_fdb_result.json``;
* die Seite (kein Pfadfeld, Dateifelder ohne ``name``, verstecktes ``upload``) und ihr Skript (in Node, wo es installiert ist);
* die Datensicherung (``upload_tmp/``) lebt in ``test_backup.py``.

Alle Dateien und Namen sind erfunden.
"""
import errno
import io
import json
import os
import re
import shutil
import subprocess
import time
from collections import namedtuple
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest

import auth
import demo
import userctx
from server import handlers, wiso_upload
from server.pages_miscellaneous import PageMiscellaneous
from tests import installation
from tests.entry_form_helpers import bank_account, execute, query, wiso_database, wiso_row
from tests.http_client import KeepAlive, get, own_origin, post, send
from tests.installation import PASSWORD
from tests.test_body_limits import assert_closed, exchange
from tests.wiso_upload_helpers import upload_over_http, upload_request, uploaded

BYTES = bytes(range(256)) * 40                           # 10240 Bytes, jedes Byte kommt vor


# ── Hilfen ───────────────────────────────────────────────────────────────────
def admin_root():
    """Zwischenordner des Benutzers ``admin`` (so sieht ihn der Server; die Testfunktion selbst hat keinen Benutzer)."""
    return os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'upload_tmp', 'wiso_fdb')


def folders(root=None):
    root = root or admin_root()
    return sorted(os.listdir(root)) if os.path.isdir(root) else []


def tree_of(root=None):
    """Alle Dateien unter dem Zwischenordner (relativ), um zu sehen, dass nichts liegen bleibt."""
    root = root or admin_root()
    return sorted(os.path.relpath(os.path.join(base, name), root)
                  for base, _dirs, names in os.walk(root) for name in names)


def read(path):
    with open(path, 'rb') as handle:
        return handle.read()


def headers_of(port, csrf, **extra):
    return {'Origin': own_origin(port), 'X-CSRF-Token': csrf, 'Content-Type': 'application/octet-stream', **extra}


def upload_json(response):
    return json.loads(response.body)


def query_of(location):
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


@pytest.fixture
def user_root():
    """Wo die Zwischenordner dieses Tests liegen (kein Benutzer: ``userctx.user_data_dir()`` ist die Datenwurzel)."""
    return wiso_upload.upload_root()


@pytest.fixture
def symlink_to(tmp_path):
    """Legt einen Verzeichnis-Link an; wo das Dateisystem keine kennt, entfällt der Test."""
    def make(target, link):
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f'das Dateisystem kann keine symbolischen Links anlegen ({exc})')
    return make


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Upload-Bank', 1800)
    return tmp_db


def _fdb(tmp_path, name='a.fdb', rows=None):
    """Eine kleine, gültige WISO-Datenbank (zwei Sätze)."""
    rows = rows or [wiso_row(4980, 1200, 119.0, 'U-1'), wiso_row(1200, 8400, 500.0, 'U-2')]
    return wiso_database(tmp_path / name, rows)


# ═════════════════════════════════════════════════════════════════════════════
# 1 – server/wiso_upload.py
# ═════════════════════════════════════════════════════════════════════════════
def test_the_limits_are_the_documented_ones():
    assert wiso_upload.MAX_WISO_DB_BODY == 2 * 1024 ** 3 and wiso_upload.CHUNK == 1024 * 1024
    assert wiso_upload.FREE_SPACE_MARGIN == 64 * 1024 * 1024 and wiso_upload.STALE_AFTER == 6 * 3600
    assert wiso_upload.SLOT_FILES == {'client': 'DB1.FDB', 'standard': 'DB0.FDB'}


def test_a_token_is_32_lower_case_hex_characters():
    token = wiso_upload.begin()
    assert re.fullmatch(r'[0-9a-f]{32}', token) and wiso_upload.valid_token(token)
    assert wiso_upload.upload_dir(token) == os.path.join(wiso_upload.upload_root(), token)
    assert wiso_upload.begin() != token


@pytest.mark.parametrize('value', ['', 'a' * 31, 'a' * 33, 'A' * 32, '../' + 'a' * 29, 'a' * 31 + '/', 'a' * 32 + '\n',
                                   '٠' * 32, 'g' * 32, None, 5, b'a' * 32, ['a' * 32]],
                         ids=['leer', '31', '33', 'Grossbuchstaben', 'Aufwaerts', 'Schraegstrich', 'Zeilenumbruch',
                              'arabische-Ziffern', 'kein-Hex', 'None', 'Zahl', 'Bytes', 'Liste'])
def test_anything_but_a_token_gives_no_path(value):
    assert not wiso_upload.valid_token(value)
    assert wiso_upload.folder_path(value) is None and wiso_upload.upload_dir(value) is None
    assert wiso_upload.files(value) == (None, None)
    wiso_upload.discard(value)                                      # tut nichts, wirft nichts


def test_a_valid_token_without_a_folder_gives_no_directory_but_a_path():
    token = 'a' * 32
    assert wiso_upload.upload_dir(token) is None
    assert wiso_upload.folder_path(token) == os.path.join(wiso_upload.upload_root(), token)


def test_a_link_is_never_an_upload_folder(tmp_path, symlink_to):
    target = tmp_path / 'woanders'
    target.mkdir()
    os.makedirs(wiso_upload.upload_root())
    token = 'b' * 32
    symlink_to(str(target), os.path.join(wiso_upload.upload_root(), token))
    assert wiso_upload.upload_dir(token) is None
    wiso_upload.discard(token)
    assert target.is_dir()                                          # discard folgt dem Link nicht


def test_the_folder_lives_in_the_data_area_of_the_user(monkeypatch, tmp_path):
    assert wiso_upload.upload_root() == os.path.join(userctx.DATA_ROOT, 'upload_tmp', 'wiso_fdb')
    monkeypatch.setattr(userctx, 'user_data_dir', lambda: str(tmp_path / 'anderswo'))
    assert wiso_upload.upload_root() == os.path.join(str(tmp_path), 'anderswo', 'upload_tmp', 'wiso_fdb')


def test_begin_removes_every_older_folder(user_root):
    first, second = wiso_upload.begin(), wiso_upload.begin()
    assert not os.path.exists(os.path.join(user_root, first))
    third = wiso_upload.begin()
    assert os.listdir(user_root) == [third] and not os.path.exists(os.path.join(user_root, second))


def test_begin_removes_what_an_upload_left_inside_a_folder(user_root):
    token = wiso_upload.begin()
    for name in ('DB1.FDB', 'DB0.FDB', 'abgebrochen.part'):
        with open(os.path.join(user_root, token, name), 'wb') as handle:
            handle.write(b'x' * 10)
    wiso_upload.begin()
    assert not os.path.exists(os.path.join(user_root, token))


def test_begin_keeps_a_folder_with_a_fresh_marker_and_removes_one_with_an_old_marker(user_root):
    running = wiso_upload.begin()
    wiso_upload.mark_importing(running)
    marker = os.path.join(user_root, running, wiso_upload.IMPORTING)
    assert os.path.isfile(marker)

    other = wiso_upload.begin()                                     # ein Import läuft: sein Ordner bleibt
    assert sorted(os.listdir(user_root)) == sorted([running, other])

    old = time.time() - wiso_upload.STALE_AFTER - 60                # der Marker ist älter als sechs Stunden
    os.utime(marker, (old, old))
    third = wiso_upload.begin()
    assert os.listdir(user_root) == [third]                         # der alte Marker schützt nicht mehr, der andere Upload wartete nur


def test_a_marker_just_inside_the_limit_still_protects(user_root):
    token = wiso_upload.begin()
    wiso_upload.mark_importing(token)
    marker = os.path.join(user_root, token, wiso_upload.IMPORTING)
    almost = time.time() - wiso_upload.STALE_AFTER + 120
    os.utime(marker, (almost, almost))
    wiso_upload.sweep()
    assert os.path.isdir(os.path.join(user_root, token))


def test_sweep_keeps_the_named_folder_and_the_other_ones_go(user_root):
    keep = wiso_upload.begin()
    os.mkdir(os.path.join(user_root, 'c' * 32))
    wiso_upload.sweep(keep=keep)
    assert os.listdir(user_root) == [keep]


def test_sweep_removes_a_link_but_never_its_target(tmp_path, user_root, symlink_to):
    target = tmp_path / 'fremd'
    target.mkdir()
    (target / 'wichtig.txt').write_text('bleibt', encoding='utf-8')
    os.makedirs(user_root)
    symlink_to(str(target), os.path.join(user_root, 'd' * 32))
    wiso_upload.begin()
    assert not os.path.lexists(os.path.join(user_root, 'd' * 32))
    assert (target / 'wichtig.txt').read_text(encoding='utf-8') == 'bleibt'


def test_sweep_touches_only_folders_directly_below_the_root(tmp_path, user_root):
    os.makedirs(user_root)
    with open(os.path.join(user_root, 'lose-datei.txt'), 'w', encoding='utf-8') as handle:
        handle.write('keine Kennung')
    outside = tmp_path / 'data' / 'upload_tmp' / 'anderes'
    outside.mkdir(parents=True)
    wiso_upload.begin()
    assert os.path.isfile(os.path.join(user_root, 'lose-datei.txt')) and outside.is_dir()


def test_sweep_without_a_root_does_nothing(user_root):
    assert not os.path.exists(user_root)
    wiso_upload.sweep()
    assert not os.path.exists(user_root)


def test_discard_removes_the_whole_folder_and_leaves_the_others(user_root):
    token = wiso_upload.begin()
    with open(os.path.join(user_root, token, 'DB1.FDB'), 'wb') as handle:
        handle.write(b'x')
    wiso_upload.mark_importing(token)
    other = 'e' * 32
    os.mkdir(os.path.join(user_root, other))
    wiso_upload.discard(token)
    assert not os.path.exists(os.path.join(user_root, token)) and os.path.isdir(os.path.join(user_root, other))
    wiso_upload.discard(token)                                      # ein zweites Mal: nichts zu tun


def test_files_need_the_client_file(user_root):
    token = wiso_upload.begin()
    assert wiso_upload.files(token) == (None, None)
    with open(os.path.join(user_root, token, 'DB0.FDB'), 'wb') as handle:
        handle.write(b'x')
    assert wiso_upload.files(token) == (None, None)               # ohne DB1.FDB gibt es nichts zu importieren
    with open(os.path.join(user_root, token, 'DB1.FDB'), 'wb') as handle:
        handle.write(b'y')
    assert wiso_upload.files(token) == (os.path.join(user_root, token, 'DB1.FDB'), os.path.join(user_root, token, 'DB0.FDB'))


class Counting(io.BytesIO):
    """Ein Lesestrom, der festhält, wie viel jeder ``read`` verlangt."""

    def __init__(self, data):
        super().__init__(data)
        self.asked = []

    def read(self, size=-1):
        self.asked.append(size)
        return super().read(size)


def test_store_writes_the_file_in_chunks_under_the_fixed_name(user_root, monkeypatch):
    monkeypatch.setattr(wiso_upload, 'CHUNK', 1000)
    token = wiso_upload.begin()
    stream = Counting(BYTES + b'Rest')
    wiso_upload.store(token, 'client', stream, len(BYTES))
    assert read(os.path.join(user_root, token, 'DB1.FDB')) == BYTES
    assert stream.asked == [1000] * 10 + [240] and max(stream.asked) <= 1000      # nie mehr als ein Stück auf einmal
    assert stream.read() == b'Rest'                                               # und nicht mehr als erklärt
    assert os.listdir(os.path.join(user_root, token)) == ['DB1.FDB']
    wiso_upload.store(token, 'standard', io.BytesIO(b'zwei'), 4)
    assert sorted(os.listdir(os.path.join(user_root, token))) == ['DB0.FDB', 'DB1.FDB']
    wiso_upload.store(token, 'client', io.BytesIO(b'neu'), 3)                    # erneuter Upload: ersetzt
    assert read(os.path.join(user_root, token, 'DB1.FDB')) == b'neu'


def test_store_with_fewer_bytes_than_declared_leaves_nothing_behind(user_root):
    token = wiso_upload.begin()
    with pytest.raises(wiso_upload.UploadIncomplete):
        wiso_upload.store(token, 'client', io.BytesIO(b'zu kurz'), 100)
    assert os.listdir(os.path.join(user_root, token)) == []


def test_store_keeps_the_earlier_file_when_a_new_upload_is_incomplete(user_root):
    token = wiso_upload.begin()
    wiso_upload.store(token, 'client', io.BytesIO(b'alt'), 3)
    with pytest.raises(wiso_upload.UploadIncomplete):
        wiso_upload.store(token, 'client', io.BytesIO(b'ne'), 3)
    assert os.listdir(os.path.join(user_root, token)) == ['DB1.FDB']
    assert read(os.path.join(user_root, token, 'DB1.FDB')) == b'alt'


def test_a_broken_connection_counts_as_incomplete(user_root):
    class Broken:
        def read(self, size):
            raise ConnectionResetError(errno.ECONNRESET, 'Connection reset by peer')

    token = wiso_upload.begin()
    with pytest.raises(wiso_upload.UploadIncomplete):
        wiso_upload.store(token, 'client', Broken(), 10)
    assert os.listdir(os.path.join(user_root, token)) == []


@pytest.mark.parametrize('failing', ['mkstemp', 'replace'])
def test_a_write_error_is_a_failure_and_leaves_nothing_behind(user_root, monkeypatch, failing):
    token = wiso_upload.begin()
    full = OSError(errno.ENOSPC, 'No space left on device', os.path.join(user_root, token, 'x'))
    if failing == 'mkstemp':
        def mkstemp(*args, **kwargs):
            raise full
        monkeypatch.setattr(wiso_upload, 'tempfile', SimpleNamespace(mkstemp=mkstemp))
    else:
        real = os.replace

        def replace(source, target, *args, **kwargs):
            if str(source).endswith('.part'):
                raise full
            return real(source, target, *args, **kwargs)
        monkeypatch.setattr(os, 'replace', replace)
    with pytest.raises(wiso_upload.UploadFailed):
        wiso_upload.store(token, 'client', io.BytesIO(b'abc'), 3)
    assert os.listdir(os.path.join(user_root, token)) == []


def test_begin_without_a_place_for_the_folder_is_a_failure(user_root):
    os.makedirs(os.path.dirname(os.path.dirname(user_root)))
    with open(os.path.dirname(user_root), 'w', encoding='utf-8') as handle:      # an der Stelle von upload_tmp liegt eine Datei
        handle.write('kein Ordner')
    with pytest.raises(wiso_upload.UploadFailed):
        wiso_upload.begin()


def test_store_without_a_folder_is_a_failure(user_root):
    with pytest.raises(wiso_upload.UploadFailed):
        wiso_upload.store('f' * 32, 'client', io.BytesIO(b'abc'), 3)


@pytest.mark.parametrize('free, length, enough', [
    (100 * 1024 ** 2, 36 * 1024 ** 2, True), (100 * 1024 ** 2, 36 * 1024 ** 2 + 1, False),
    (64 * 1024 ** 2, 1, False), (64 * 1024 ** 2 + 1, 1, True), (0, 1, False)])
def test_enough_space_keeps_a_margin(monkeypatch, free, length, enough):
    usage = namedtuple('usage', 'total used free')
    measured = []
    monkeypatch.setattr(shutil, 'disk_usage', lambda folder: measured.append(folder) or usage(10 ** 12, 0, free))
    assert wiso_upload.enough_space(length) is enough
    assert os.path.isdir(measured[0])                               # gemessen an einem vorhandenen Ordner (hier: weiter oben)


# ═════════════════════════════════════════════════════════════════════════════
# 2 – POST /wiso/fdb-upload gegen einen echten Server
# ═════════════════════════════════════════════════════════════════════════════
def test_the_client_file_arrives_byte_for_byte_under_a_fixed_name(server_port, auto_login):
    answer = upload_request(server_port, auto_login.csrf, BYTES)
    assert answer.status == 200 and answer.headers['Content-Type'] == 'application/json'
    data = upload_json(answer)
    assert data['success'] is True and re.fullmatch(r'[0-9a-f]{32}', data['upload'])
    assert folders() == [data['upload']]
    assert tree_of() == [os.path.join(data['upload'], 'DB1.FDB')]
    assert read(os.path.join(admin_root(), data['upload'], 'DB1.FDB')) == BYTES


def test_the_standard_file_joins_the_client_file_in_the_same_folder(server_port, auto_login):
    token = upload_json(upload_request(server_port, auto_login.csrf, BYTES))['upload']
    answer = upload_request(server_port, auto_login.csrf, b'Standardkontenrahmen', slot='standard', token=token)
    assert answer.status == 200 and upload_json(answer) == {'success': True, 'upload': token}
    assert tree_of() == [os.path.join(token, 'DB0.FDB'), os.path.join(token, 'DB1.FDB')]
    assert read(os.path.join(admin_root(), token, 'DB0.FDB')) == b'Standardkontenrahmen'
    assert read(os.path.join(admin_root(), token, 'DB1.FDB')) == BYTES


def test_the_name_of_the_file_in_the_request_is_never_used(server_port, auto_login):
    answer = upload_request(server_port, auto_login.csrf, b'x', headers={
        'Content-Disposition': 'attachment; filename="../../evil.txt"', 'X-Filename': '../../evil.txt'})
    token = upload_json(answer)['upload']
    assert tree_of() == [os.path.join(token, 'DB1.FDB')]
    assert not os.path.exists(os.path.join(userctx.DATA_ROOT, 'evil.txt'))


def test_a_file_larger_than_one_chunk_arrives_complete(server_port, auto_login, monkeypatch):
    monkeypatch.setattr(wiso_upload, 'CHUNK', 777)                  # dreizehn volle Stücke und ein Rest
    body = (BYTES * 2)[:777 * 13 + 5]
    token = upload_json(upload_request(server_port, auto_login.csrf, body))['upload']
    assert read(os.path.join(admin_root(), token, 'DB1.FDB')) == body


def test_both_files_can_travel_over_one_connection(server_port, auto_login):
    headers = headers_of(server_port, auto_login.csrf)
    with KeepAlive(server_port) as conn:
        first = conn.request('POST', '/wiso/fdb-upload?slot=client', headers, body=BYTES)
        token = upload_json(first)['upload']
        second = conn.request('POST', f'/wiso/fdb-upload?slot=standard&upload={token}', headers, body=b'zwei')
        assert first.status == second.status == 200
        assert conn.get('/about').status == 200                     # der Body war genau so lang wie erklärt
    assert tree_of() == [os.path.join(token, 'DB0.FDB'), os.path.join(token, 'DB1.FDB')]


def test_a_new_client_upload_replaces_the_waiting_one(server_port, auto_login):
    first = upload_json(upload_request(server_port, auto_login.csrf, b'erste'))['upload']
    second = upload_json(upload_request(server_port, auto_login.csrf, b'zweite'))['upload']
    assert first != second and folders() == [second]               # je Benutzer wartet höchstens ein Upload


def test_the_token_can_be_reused_for_the_client_file(server_port, auto_login):
    token = upload_json(upload_request(server_port, auto_login.csrf, b'alt'))['upload']
    os.mkdir(os.path.join(admin_root(), 'a' * 32))                   # ein fremder, wartender Ordner
    answer = upload_request(server_port, auto_login.csrf, b'neu', token=token)
    assert answer.status == 200 and upload_json(answer)['upload'] == token
    assert read(os.path.join(admin_root(), token, 'DB1.FDB')) == b'neu'
    assert folders() == [token]


def test_a_second_client_upload_keeps_the_standard_file_of_the_folder(server_port, auto_login):
    token = upload_json(upload_request(server_port, auto_login.csrf, b'eins'))['upload']
    upload_request(server_port, auto_login.csrf, b'standard', slot='standard', token=token)
    upload_request(server_port, auto_login.csrf, b'zwei', token=token)
    assert read(os.path.join(admin_root(), token, 'DB0.FDB')) == b'standard'


BAD_TOKENS = [('ohne', None), ('unbekannt', '0' * 32), ('Aufwaerts', '../' + 'a' * 29), ('31-Zeichen', 'a' * 31),
              ('Grossbuchstaben', 'A' * 32), ('33-Zeichen', 'a' * 33), ('Pfad', '..%2f..%2f' + 'a' * 22)]


@pytest.mark.parametrize('name, token', BAD_TOKENS, ids=[name for name, _ in BAD_TOKENS])
def test_the_standard_file_needs_a_known_token_and_nothing_is_created(server_port, auto_login, name, token):
    answer = upload_request(server_port, auto_login.csrf, b'x', slot='standard',
                            query=f'?slot=standard' + (f'&upload={token}' if token else ''))
    assert answer.status == 400 and upload_json(answer) == {'success': False, 'error': 'Ungültige Anfrage.'}
    assert not os.path.exists(os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'upload_tmp'))


def test_the_standard_file_needs_the_client_file_in_the_folder_first(server_port, auto_login):
    token = 'a' * 32
    os.makedirs(os.path.join(admin_root(), token))                   # ein Ordner, aber ohne DB1.FDB
    answer = upload_request(server_port, auto_login.csrf, b'x', slot='standard', token=token)
    assert answer.status == 400 and upload_json(answer)['error'] == 'Ungültige Anfrage.'
    assert tree_of() == []


@pytest.mark.parametrize('token', [token for _, token in BAD_TOKENS[1:]], ids=[name for name, _ in BAD_TOKENS[1:]])
def test_a_client_upload_with_a_token_that_is_no_folder_is_400(server_port, auto_login, token):
    answer = upload_request(server_port, auto_login.csrf, b'x', token=token)
    assert answer.status == 400 and upload_json(answer)['error'] == 'Ungültige Anfrage.'
    assert folders() == []


@pytest.mark.parametrize('query', ['', '?', '?slot=', '?slot=other', '?slot=CLIENT', '?slot=client%20', '?upload=' + 'a' * 32,
                                   '?SLOT=client'],
                         ids=['ohne', 'leer', 'Slot-leer', 'unbekannt', 'Grossbuchstaben', 'Leerzeichen', 'nur-Kennung', 'Name'])
def test_an_unknown_slot_is_400_and_nothing_is_created(server_port, auto_login, query):
    answer = upload_request(server_port, auto_login.csrf, b'x', query=query)
    assert answer.status == 400 and upload_json(answer) == {'success': False, 'error': 'Ungültige Anfrage.'}
    assert folders() == []


# ── Herkunft, Token, GET ─────────────────────────────────────────────────────
def test_a_get_is_405(server_port, auto_login):
    for query in ('', '?slot=client'):
        answer = get(server_port, '/wiso/fdb-upload' + query)
        assert answer.status == 405 and answer.headers['Allow'] == 'POST'


def test_without_the_csrf_token_it_is_403_and_nothing_is_stored(server_port, auto_login):
    headers = headers_of(server_port, auto_login.csrf)
    del headers['X-CSRF-Token']
    answer = send(server_port, 'POST', '/wiso/fdb-upload?slot=client', headers, b'x')
    assert answer.status == 403 and 'Sicherheits-Token' in answer.text
    assert folders() == []


@pytest.mark.parametrize('token', ['falsch', '', 'a' * 64])
def test_a_wrong_csrf_token_is_403(server_port, auto_login, token):
    answer = upload_request(server_port, token, b'x')
    assert answer.status == 403 and folders() == []


def test_the_token_of_another_user_is_403(server_port, auto_login):
    assert upload_request(server_port, auth.csrf_for('auto-login:eindringling'), b'x').status == 403
    assert folders() == []


def test_a_foreign_origin_is_403(server_port, auto_login):
    answer = upload_request(server_port, auto_login.csrf, b'x', headers={'Origin': 'http://evil.example'})
    assert answer.status == 403 and 'fremder Herkunft' in answer.text and folders() == []


def test_a_foreign_site_marker_is_403(server_port, auto_login):
    answer = upload_request(server_port, auto_login.csrf, b'x', headers={'Sec-Fetch-Site': 'cross-site'})
    assert answer.status == 403 and folders() == []


def test_a_big_body_with_a_wrong_token_is_refused_without_reading_it(server_port, auto_login):
    declared = 5 * 1024 * 1024
    status, resp, _ = exchange(server_port, '/wiso/fdb-upload?slot=client',
                               headers_of(server_port, 'falsch', **{'Content-Length': str(declared)}))
    assert status == 403
    assert_closed(resp)


# ── Länge, Platz ─────────────────────────────────────────────────────────────
def test_a_body_over_the_limit_is_413_and_not_read_and_creates_nothing(server_port, auto_login, monkeypatch):
    monkeypatch.setattr(wiso_upload, 'MAX_WISO_DB_BODY', 1000)
    headers = headers_of(server_port, auto_login.csrf, **{'Content-Length': '1001'})
    status, resp, body = exchange(server_port, '/wiso/fdb-upload?slot=client', headers)   # nur die Kopfzeilen: Läse der Server, bliebe er hängen
    assert status == 413
    data = json.loads(body)
    assert data['success'] is False and data['error'].startswith('Die Anfrage ist zu groß (höchstens')
    assert_closed(resp)
    assert not os.path.exists(os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'upload_tmp'))
    assert get(server_port, '/about').status == 200


def test_a_body_exactly_at_the_limit_is_stored(server_port, auto_login, monkeypatch):
    monkeypatch.setattr(wiso_upload, 'MAX_WISO_DB_BODY', 1000)
    answer = upload_request(server_port, auto_login.csrf, b'x' * 1000)
    assert answer.status == 200 and len(read(os.path.join(admin_root(), upload_json(answer)['upload'], 'DB1.FDB'))) == 1000


def test_the_limit_applies_to_the_standard_file_too(server_port, auto_login, monkeypatch):
    token = upload_json(upload_request(server_port, auto_login.csrf, b'x'))['upload']
    monkeypatch.setattr(wiso_upload, 'MAX_WISO_DB_BODY', 10)
    status, resp, _ = exchange(server_port, f'/wiso/fdb-upload?slot=standard&upload={token}',
                               headers_of(server_port, auto_login.csrf, **{'Content-Length': '11'}))
    assert status == 413 and tree_of() == [os.path.join(token, 'DB1.FDB')]


BAD_LENGTHS = ['abc', '-1', '1e3', '+5', '1.5', '1 2', '0x10', '²', '1' * 16]


@pytest.mark.parametrize('length', [None] + BAD_LENGTHS)
def test_a_missing_or_unusable_length_is_400_and_creates_nothing(server_port, auto_login, length):
    headers = headers_of(server_port, auto_login.csrf, **({} if length is None else {'Content-Length': length}))
    status, resp, body = exchange(server_port, '/wiso/fdb-upload?slot=client', headers)
    assert status == 400 and json.loads(body)['error'] == 'Ungültige Anfrage.'
    assert_closed(resp)
    assert not os.path.exists(os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'upload_tmp'))


def test_chunked_encoding_is_400(server_port, auto_login):
    status, resp, body = exchange(server_port, '/wiso/fdb-upload?slot=client',
                                  headers_of(server_port, auto_login.csrf, **{'Transfer-Encoding': 'chunked'}),
                                  b'5\r\nhallo\r\n0\r\n\r\n')
    assert status == 400 and json.loads(body)['error'] == 'Ungültige Anfrage.'
    assert_closed(resp)
    assert folders() == []


def test_an_empty_file_is_400(server_port, auto_login):
    answer = upload_request(server_port, auto_login.csrf, b'')
    assert answer.status == 400 and upload_json(answer) == {'success': False, 'error': 'Die Datei ist leer.'}
    assert folders() == []


def test_not_enough_free_space_is_507_before_anything_is_read(server_port, auto_login, monkeypatch):
    asked = []
    monkeypatch.setattr(wiso_upload, 'enough_space', lambda length: asked.append(length) or False)
    status, resp, body = exchange(server_port, '/wiso/fdb-upload?slot=client',
                                  headers_of(server_port, auto_login.csrf, **{'Content-Length': '123456'}))
    assert status == 507 and json.loads(body) == {'success': False,
                                                  'error': 'Nicht genug freier Speicherplatz für den Upload.'}
    assert asked == [123456] and folders() == []
    assert_closed(resp)


def test_the_free_space_is_asked_for_the_declared_length_plus_the_margin(server_port, auto_login, monkeypatch):
    usage = namedtuple('usage', 'total used free')
    free = 1000 + wiso_upload.FREE_SPACE_MARGIN
    monkeypatch.setattr(shutil, 'disk_usage', lambda folder: usage(10 ** 12, 0, free))
    assert upload_request(server_port, auto_login.csrf, b'x' * 1000).status == 200
    free -= 1
    status, _, body = exchange(server_port, '/wiso/fdb-upload?slot=client',
                               headers_of(server_port, auto_login.csrf, **{'Content-Length': '1000'}))
    assert status == 507 and 'Speicherplatz' in json.loads(body)['error']


# ── Abbruch und Fehler ───────────────────────────────────────────────────────
def test_a_body_shorter_than_declared_is_400_and_leaves_no_folder_and_no_part_file(server_port, auto_login):
    status, resp, body = exchange(server_port, '/wiso/fdb-upload?slot=client',
                                  headers_of(server_port, auto_login.csrf, **{'Content-Length': '100'}),
                                  b'nur zehn B', half_close=True)
    assert status == 400 and json.loads(body)['error'] == 'Ungültige Anfrage.'
    assert_closed(resp)
    assert folders() == [] and tree_of() == []                      # weder der neue Ordner noch eine .part-Datei


def test_a_cut_off_standard_upload_keeps_the_folder_and_the_client_file(server_port, auto_login):
    token = upload_json(upload_request(server_port, auto_login.csrf, b'client'))['upload']
    status, resp, _ = exchange(server_port, f'/wiso/fdb-upload?slot=standard&upload={token}',
                               headers_of(server_port, auto_login.csrf, **{'Content-Length': '100'}), b'kurz',
                               half_close=True)
    assert status == 400
    assert tree_of() == [os.path.join(token, 'DB1.FDB')]            # keine .part-Datei, der Ordner und DB1.FDB bleiben


def test_a_write_error_is_500_without_a_path_in_the_answer(server_port, auto_login, monkeypatch, capsys):
    def mkstemp(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'No space left on device', '/geheim/upload_tmp/wiso_fdb/x')
    monkeypatch.setattr(wiso_upload, 'tempfile', SimpleNamespace(mkstemp=mkstemp))
    status, resp, body = exchange(server_port, '/wiso/fdb-upload?slot=client',
                                  headers_of(server_port, auto_login.csrf, **{'Content-Length': '5'}), b'abcde')
    assert status == 500
    assert json.loads(body) == {'success': False, 'error': 'Die Datei konnte nicht gespeichert werden – '
                                                           'Einzelheiten stehen im Server-Log.'}
    for fragment in ('geheim', 'upload_tmp', 'No space', 'Errno', userctx.DATA_ROOT):
        assert fragment not in body.decode() and fragment not in str(resp.headers)
    assert 'geheim' in capsys.readouterr().err                      # die Einzelheiten stehen im Log
    assert folders() == []                                          # der neu angelegte Ordner ist weg
    assert get(server_port, '/about').status == 200


def test_a_folder_that_cannot_be_created_is_500_without_a_path(server_port, auto_login, capsys):
    with open(os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'upload_tmp'), 'w', encoding='utf-8') as handle:
        handle.write('Datei statt Ordner')
    answer = upload_request(server_port, auto_login.csrf, b'abc')
    assert answer.status == 500
    assert upload_json(answer) == {'success': False, 'error': 'Die Datei konnte nicht gespeichert werden – '
                                                              'Einzelheiten stehen im Server-Log.'}
    assert 'upload_tmp' not in answer.text and userctx.DATA_ROOT not in answer.text
    assert 'upload_tmp' in capsys.readouterr().err                  # die Einzelheiten stehen im Log
    assert get(server_port, '/about').status == 200


def test_an_unexpected_error_leaves_no_new_folder_behind(server_port, auto_login, monkeypatch, capsys):
    def store(*args, **kwargs):
        raise RuntimeError('unerwartet')
    monkeypatch.setattr(wiso_upload, 'store', store)
    with pytest.raises(Exception):                                  # der Server bricht die Verbindung ab, ohne Antwort
        upload_request(server_port, auto_login.csrf, b'abc')
    assert folders() == []
    assert 'unerwartet' in capsys.readouterr().err


def test_a_write_error_on_the_standard_file_keeps_the_earlier_upload(server_port, auto_login, monkeypatch):
    token = upload_json(upload_request(server_port, auto_login.csrf, b'client'))['upload']

    def mkstemp(*args, **kwargs):
        raise OSError(errno.EACCES, 'Permission denied')
    monkeypatch.setattr(wiso_upload, 'tempfile', SimpleNamespace(mkstemp=mkstemp))
    answer = upload_request(server_port, auto_login.csrf, b'x', slot='standard', token=token)
    assert answer.status == 500 and 'Permission' not in answer.text
    assert tree_of() == [os.path.join(token, 'DB1.FDB')]


# ── Demo-Modus ───────────────────────────────────────────────────────────────
def demo_site(monkeypatch):
    """Nur Demodaten, Variable gesetzt: Jeder Besucher arbeitet als ``demo``."""
    monkeypatch.setenv(demo.ENV_VAR, '1')
    userctx.set_mode('multi')
    auth.create_user('demo', PASSWORD, is_admin=False, allow_reserved=True)
    installation.open_db('demo')
    with open(os.path.join(userctx.DATA_ROOT, 'users', 'demo', demo.MARKER_FILE), 'w', encoding='utf-8') as handle:
        handle.write('x')
    return SimpleNamespace(csrf=auth.csrf_for('auto-login:demo'))


def test_in_demo_mode_the_upload_is_403_and_nothing_is_created(server_port, monkeypatch):
    site = demo_site(monkeypatch)
    before = installation.tree(userctx.DATA_ROOT)
    status, resp, body = exchange(server_port, '/wiso/fdb-upload?slot=client',
                                  headers_of(server_port, site.csrf, **{'Content-Length': '5000000'}))
    assert status == 403 and json.loads(body) == {'success': False, 'error': wiso_upload.DEMO_REFUSED}
    assert wiso_upload.DEMO_REFUSED == 'Im Demo-Modus ist der Import aus der WISO-Datenbank abgeschaltet.'
    assert_closed(resp)
    assert installation.tree(userctx.DATA_ROOT) == before


def test_in_demo_mode_the_import_is_refused_and_the_page_shows_a_hint_instead_of_the_form(server_port, monkeypatch):
    site = demo_site(monkeypatch)
    answer = post(server_port, '/wiso/fdb-import', headers={'Origin': own_origin(server_port)}, csrf=site.csrf,
                  form={'upload': 'a' * 32, 'confirm_existing': '1'})
    assert answer.status == 303 and query_of(answer.headers['Location']) == {'wiso_fdb': 'error',
                                                                            'msg': wiso_upload.DEMO_REFUSED}
    page = get(server_port, '/miscellaneous').text
    assert wiso_upload.DEMO_REFUSED in page
    assert 'action="/wiso/fdb-import"' not in page and 'wisoFdbForm' not in page       # kein Formular, kein Skript
    assert 'name="upload"' not in page and 'wisoFdbClient' not in page and '/wiso/fdb-upload' not in page
    assert 'name="csvfile"' in page                                  # der Textimport bleibt


def test_in_demo_mode_the_handler_refuses_before_the_stock_check(tmp_db, tmp_path, monkeypatch):
    demo_site(monkeypatch)
    execute(tmp_db, "INSERT INTO Bookings (DateBooking, Amount, BookingType) VALUES ('2026-01-02', 5, 'entry')")
    fields = uploaded(_fdb(tmp_path))                               # der Bestand ist nicht leer, eine Bestätigung fehlt
    status, location = handlers.handle_wiso_fdb_import(tmp_db, fields)
    assert status == 303 and query_of(location) == {'wiso_fdb': 'error', 'msg': wiso_upload.DEMO_REFUSED}
    assert query(tmp_db, 'SELECT COUNT(*) FROM Bookings') == [(1,)]
    assert wiso_upload.upload_dir(fields['upload'][0]) is None      # der Zwischenordner ist auch hier weg
    fields = uploaded(_fdb(tmp_path))                               # und mit Bestätigung ändert sich daran nichts
    status, location = handlers.handle_wiso_fdb_import(tmp_db, {**fields, 'confirm_existing': ['1']})
    assert query_of(location)['msg'] == wiso_upload.DEMO_REFUSED and query(tmp_db, 'SELECT COUNT(*) FROM Bookings') == [(1,)]


# ═════════════════════════════════════════════════════════════════════════════
# 3 – der Import: der Zwischenordner ist danach immer weg
# ═════════════════════════════════════════════════════════════════════════════
def _result_text():
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), encoding='utf-8') as handle:
        return handle.read()


def test_a_successful_import_deletes_the_folder_and_names_only_the_fixed_file_name(db, tmp_path):
    fields = uploaded(_fdb(tmp_path), standard=_fdb(tmp_path, 'standard.fdb'))
    token = fields['upload'][0]
    folder = wiso_upload.upload_dir(token)
    assert wiso_upload.files(token)[1] is not None

    status, location = handlers.handle_wiso_fdb_import(db, fields)

    params = query_of(location)
    assert status == 303 and (params['wiso_fdb'], params['imported'], params['file']) == ('ok', '2', 'DB1.FDB')
    assert not os.path.exists(folder) and os.listdir(wiso_upload.upload_root()) == []
    stored = json.loads(_result_text())
    assert (stored['file'], stored['standard_chart']) == ('DB1.FDB', 'DB0.FDB')
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(2,)]


def test_without_a_standard_file_the_result_names_none(db, tmp_path):
    handlers.handle_wiso_fdb_import(db, uploaded(_fdb(tmp_path)))
    stored = json.loads(_result_text())
    assert (stored['file'], stored['standard_chart']) == ('DB1.FDB', '')


def test_the_import_marks_the_folder_while_it_runs(db, tmp_path, monkeypatch):
    seen = {}
    original = type(db).import_wiso_fdb

    def watching(self, path, *args, **kwargs):
        seen['marker'] = os.path.isfile(os.path.join(os.path.dirname(path), wiso_upload.IMPORTING))
        seen['name'] = os.path.basename(path)
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(type(db), 'import_wiso_fdb', watching)
    handlers.handle_wiso_fdb_import(db, uploaded(_fdb(tmp_path)))
    assert seen == {'marker': True, 'name': 'DB1.FDB'}


def test_a_folder_that_is_being_imported_survives_a_new_upload(db, tmp_path, monkeypatch):
    """Ein Import läuft (lang), währenddessen beginnt ein zweiter Upload: Sein ``begin`` räumt den Ordner des Imports nicht weg."""
    survivors = []
    original = type(db).import_wiso_fdb

    def during(self, path, *args, **kwargs):
        survivors.append(wiso_upload.begin())
        survivors.append(os.path.isfile(path))
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(type(db), 'import_wiso_fdb', during)
    status, location = handlers.handle_wiso_fdb_import(db, uploaded(_fdb(tmp_path)))
    assert query_of(location)['wiso_fdb'] == 'ok' and survivors[1] is True
    assert os.listdir(wiso_upload.upload_root()) == survivors[:1]    # der andere Upload wartet, der Import ist aufgeräumt


def test_a_blocker_deletes_the_folder(db, tmp_path):
    from tests.fdb_builder import FdbBuilder, LONG, Column
    builder = FdbBuilder(page_size=16384)
    builder.table('IRGENDWAS', [Column('ID', LONG, 4)]).add(ID=1)
    fields = uploaded(builder.write(str(tmp_path / 'fremd.fdb')))
    status, location = handlers.handle_wiso_fdb_import(db, fields)
    assert query_of(location)['wiso_fdb'] == 'incompatible'
    assert wiso_upload.upload_dir(fields['upload'][0]) is None and os.listdir(wiso_upload.upload_root()) == []
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(0,)]


def test_an_exception_in_the_import_deletes_the_folder_and_hides_the_path(db, tmp_path, monkeypatch):
    def failing(self, path, *args, **kwargs):
        raise RuntimeError(f'Platte voll bei {path} und {os.path.dirname(path)}{os.sep}')

    monkeypatch.setattr(type(db), 'import_wiso_fdb', failing)
    fields = uploaded(_fdb(tmp_path))
    folder = wiso_upload.upload_dir(fields['upload'][0])
    status, location = handlers.handle_wiso_fdb_import(db, fields)

    message = query_of(location)['msg']
    assert status == 303 and query_of(location)['wiso_fdb'] == 'error'
    assert message == handlers.WISO_IMPORT_FAILED                   # nie der Text der Ausnahme, nicht einmal ohne Pfad
    assert folder not in location and 'upload_tmp' not in location and not os.path.exists(folder)


def test_a_missing_confirmation_deletes_the_folder_and_does_not_read_the_file(db, tmp_path, monkeypatch):
    execute(db, "INSERT INTO Bookings (DateBooking, Amount, BookingType) VALUES ('2026-01-02', 5, 'entry')")
    fields = uploaded(_fdb(tmp_path))
    folder = wiso_upload.upload_dir(fields['upload'][0])
    touched, original = [], wiso_upload.files
    monkeypatch.setattr(wiso_upload, 'files', lambda token: touched.append(token) or original(token))

    status, location = handlers.handle_wiso_fdb_import(db, fields)

    assert query_of(location) == {'wiso_fdb': 'error', 'msg': handlers.WISO_FDB_STOCK_REFUSED}
    assert touched == [] and not os.path.exists(folder)             # die Datei wurde nie angefasst, und der Ordner ist weg
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(1,)]


def test_an_unknown_token_is_reported_as_missing(db):
    status, location = handlers.handle_wiso_fdb_import(db, {'upload': ['1' * 32]})
    assert query_of(location) == {'wiso_fdb': 'error', 'msg': 'Die hochgeladene Datei wurde nicht gefunden – '
                                                              'bitte erneut hochladen.'}


def test_a_folder_without_the_client_file_is_reported_as_missing_and_deleted(db):
    token = wiso_upload.begin()
    status, location = handlers.handle_wiso_fdb_import(db, {'upload': [token]})
    assert query_of(location)['msg'] == handlers.WISO_FDB_UPLOAD_MISSING
    assert wiso_upload.upload_dir(token) is None


def test_the_form_field_with_a_path_is_ignored(db, tmp_path):
    """Ein Pfad aus der Anfrage wird nie gelesen – auch ein altes Formular mit ``fdb_path`` importiert nichts."""
    path = _fdb(tmp_path)
    status, location = handlers.handle_wiso_fdb_import(db, {'fdb_path': [path]})
    assert query_of(location)['msg'] == handlers.WISO_FDB_UPLOAD_MISSING
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(0,)]
    assert os.path.exists(path)


def test_a_token_of_another_user_finds_nothing(db, tmp_path, monkeypatch):
    fields = uploaded(_fdb(tmp_path))
    monkeypatch.setattr(userctx, 'user_data_dir', lambda: os.path.join(userctx.DATA_ROOT, 'users', 'anderer'))
    status, location = handlers.handle_wiso_fdb_import(db, fields)
    assert query_of(location)['msg'] == handlers.WISO_FDB_UPLOAD_MISSING
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(0,)]


# ── kein Serverpfad in Antwort, Weiterleitung und Ergebnisdatei ──────────────
def _no_path(texts, token):
    folder = wiso_upload.folder_path(token)
    for text in texts:
        for fragment in (folder, os.path.dirname(folder), 'upload_tmp', os.path.abspath(folder), os.path.realpath(folder)):
            assert fragment not in text, (fragment, text[:300])


def test_an_unreadable_file_names_no_server_path_anywhere(db, tmp_path):
    """Kein Firebird: ``OdsError`` nennt den Pfad der Datei – er steht weder in der Weiterleitung noch in der Ergebnisdatei."""
    garbage = tmp_path / 'kaputt.fdb'
    garbage.write_bytes(b'kein Firebird, nur Text' * 200)
    fields = uploaded(garbage)
    token = fields['upload'][0]
    status, location = handlers.handle_wiso_fdb_import(db, {**fields, 'with_invoices': ['1']})

    stored = _result_text()
    assert status == 303 and 'keine Firebird-Datei' in stored     # die Meldung bleibt – ohne den Pfad
    # … und die Seite meldet einen Fehler, kein „Import abgeschlossen“ mit lauter Nullen
    assert location == f'/miscellaneous?wiso_fdb=error&msg={quote(handlers.WISO_FDB_UNREADABLE)}'
    assert json.loads(stored)['unreadable'] is True and db.wiso_import_stock()['bookings'] == 0
    _no_path([location, stored], token)
    assert json.loads(stored)['errors'][0].startswith('DB1.FDB: DB1.FDB: keine Firebird-Datei')
    _no_path([PageMiscellaneous(db)], token)


def test_a_path_in_any_nested_result_value_is_removed():
    token = wiso_upload.begin()
    folder = wiso_upload.folder_path(token)
    value = {'blocker': [f'{folder}{os.sep}DB1.FDB: kaputt'], 'hints': (f'bei {folder}',),
             'n': 5, 'ok': True, 'nichts': None, 'tief': {'a': [{'b': f'{os.path.realpath(folder)}/x'}]},
             'doppelt': f'{folder}{os.sep}{folder}{os.sep}', f'{folder}': 1}
    clean = handlers._without_upload_path(value, token)
    assert clean == {'blocker': ['DB1.FDB: kaputt'], 'hints': ['bei '], 'n': 5, 'ok': True, 'nichts': None,
                     'tief': {'a': [{'b': 'x'}]}, 'doppelt': '', '': 1}
    assert handlers._without_upload_path('nichts zu tun', 'kein Token') == 'nichts zu tun'
    assert handlers._without_upload_path(value, 'kein Token') is value
    doubled = folder.replace('\\', '\\\\')                      # so nennt ein OSError einen Windows-Pfad
    assert handlers._without_upload_path(f'[Errno 2] {doubled}\\\\DB1.FDB', token) == '[Errno 2] DB1.FDB'


def test_the_error_text_of_the_payment_step_is_cleaned_too(db, tmp_path, monkeypatch):
    original = type(db).link_wiso_invoice_payments

    def noisy(self, path, standard=None):
        result = original(self, path, standard)
        result['errors'].append(f'{path}: Zahlung nicht lesbar')
        result['payment_warnings'].append(f'Warnung zu {os.path.dirname(path)}{os.sep}DB1.FDB')
        return result

    monkeypatch.setattr(type(db), 'link_wiso_invoice_payments', noisy)
    fields = uploaded(_fdb(tmp_path))
    status, location = handlers.handle_wiso_fdb_import(db, {**fields, 'with_invoices': ['1']})
    stored = _result_text()
    _no_path([location, stored], fields['upload'][0])
    assert 'DB1.FDB: Zahlung nicht lesbar' in stored and 'Warnung zu DB1.FDB' in stored


def test_over_http_nothing_names_the_folder(server_port, auto_login, tmp_path):
    bank_account(auto_login.db, 'Upload-Bank', 1800)
    garbage = tmp_path / 'kaputt.fdb'
    garbage.write_bytes(b'Text statt Firebird' * 300)
    token = upload_over_http(server_port, auto_login.csrf, garbage)

    answer = post(server_port, '/wiso/fdb-import', headers={'Origin': own_origin(server_port)}, csrf=auto_login.csrf,
                  form={'upload': token, 'with_invoices': '1'})

    page = get(server_port, answer.headers['Location'])
    stored = open(os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'wiso_fdb_result.json'),
                  encoding='utf-8').read()
    assert answer.status == 303 and page.status == 200
    for text in (answer.headers['Location'], answer.text, page.text, stored, str(answer.headers)):
        assert 'upload_tmp' not in text and token not in text and userctx.DATA_ROOT not in text
    assert folders() == []


def test_the_whole_way_over_http_imports_and_cleans_up(server_port, auto_login, tmp_path):
    db = auto_login.db
    bank_account(db, 'Upload-Bank', 1800)
    path = _fdb(tmp_path)
    token = upload_over_http(server_port, auto_login.csrf, path, standard=path)
    assert tree_of() == [os.path.join(token, 'DB0.FDB'), os.path.join(token, 'DB1.FDB')]

    answer = post(server_port, '/wiso/fdb-import', headers={'Origin': own_origin(server_port)}, csrf=auto_login.csrf,
                  form={'upload': token})

    params = query_of(answer.headers['Location'])
    assert answer.status == 303 and (params['wiso_fdb'], params['imported'], params['file']) == ('ok', '2', 'DB1.FDB')
    assert query(db, 'SELECT COUNT(*) FROM Bookings WHERE BookingType = ?', ('entry',)) == [(2,)]
    assert folders() == []
    shown = get(server_port, answer.headers['Location']).text
    assert 'Zuletzt gelesen: <code>DB1.FDB</code>' in shown

    again = post(server_port, '/wiso/fdb-import', headers={'Origin': own_origin(server_port)}, csrf=auto_login.csrf,
                 form={'upload': token, 'confirm_existing': '1'})                # derselbe Upload: gibt es nicht mehr
    assert query_of(again.headers['Location'])['msg'] == handlers.WISO_FDB_UPLOAD_MISSING


def test_the_import_route_needs_the_csrf_token_and_the_origin(server_port, auto_login, tmp_path):
    probe = tmp_path / 'probe.fdb'
    probe.write_bytes(b'x')
    token = upload_over_http(server_port, auto_login.csrf, probe)
    refused = post(server_port, '/wiso/fdb-import', headers={'Origin': own_origin(server_port)}, form={'upload': token})
    assert refused.status == 403
    foreign = post(server_port, '/wiso/fdb-import', headers={'Origin': 'http://evil.example'}, csrf=auto_login.csrf,
                   form={'upload': token})
    assert foreign.status == 403
    assert folders() == [token]                                     # nichts davon hat den Upload verbraucht


# ═════════════════════════════════════════════════════════════════════════════
# 4 – die Seite und ihr Skript
# ═════════════════════════════════════════════════════════════════════════════
def _form(page):
    return page.split('action="/wiso/fdb-import"')[1].split('</form>')[0]


def test_the_form_has_file_fields_without_a_name_and_a_hidden_upload_field(db):
    page = PageMiscellaneous(db)
    form = _form(page)
    assert 'fdb_path' not in page and 'type="text"' not in form
    inputs = re.findall(r'<input [^>]*>', form)
    files = [tag for tag in inputs if 'type="file"' in tag]
    assert len(files) == 2 and all(' name=' not in tag for tag in files)            # sie gehen nie im Formular mit
    assert all('accept=".fdb"' in tag for tag in files)
    assert ' required' in files[0] and ' required' not in files[1]                  # DB1.FDB Pflicht, DB0.FDB optional
    hidden = [tag for tag in inputs if 'type="hidden"' in tag]
    assert len(hidden) == 1 and 'name="upload"' in hidden[0] and 'value=""' in hidden[0]
    assert form.count('name="upload"') == 1 and 'name="with_assets"' in form and 'name="with_invoices"' in form
    assert 'DB1.FDB' in form and 'DB0.FDB' in form and 'optional' in form


def test_the_text_no_longer_asks_for_a_path(db):
    page = PageMiscellaneous(db)
    for old in ('Pfad:', 'ProgramData', 'C:\\', 'Angeben lässt sich', 'wird daneben automatisch'):
        assert old not in page
    text = ' '.join(page.split())                                     # Zeilenumbrüche im Quelltext spielen keine Rolle
    assert 'Sie werden hochgeladen, gelesen und danach vom Server wieder gelöscht.' in text
    assert 'WISO sollte dabei geschlossen sein.' in text and 'an der WISO-Datenbank ändert sich nichts' in text
    assert 'Beide Dateien liegen im <strong>DB-Verzeichnis</strong> von WISO Mein Büro' in text


def test_the_form_keeps_the_confirmation_above_the_button(db):
    from tests.entry_form_helpers import execute
    execute(db, "INSERT INTO Bookings (DateBooking, Amount, BookingType) VALUES ('2026-01-02', 5, 'entry')")
    form = _form(PageMiscellaneous(db))
    assert form.index('name="confirm_existing"') < form.index('<button type="submit"')
    assert form.index('name="upload"') < form.index('<button type="submit"')


def test_the_page_stays_inside_the_existing_classes(db):
    page = PageMiscellaneous(db)
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'buch.css'), encoding='utf-8') as css:
        sheet = css.read()
    form = _form(page)
    for name in set(re.findall(r'class="([^"]+)"', form)) | {'errorColor', 'hint'}:
        for cls in name.split():
            assert f'.{cls}' in sheet, cls
    assert 'style=' not in form                                     # keine Inline-Styles im Formular


def _upload_script(page):
    (script,) = [s for s in re.findall(r'<script>(.*?)</script>', page, re.S) if 'wisoFdbForm' in s]
    return script


def test_the_script_carries_the_limit_from_python_and_no_html_from_data(db):
    script = _upload_script(PageMiscellaneous(db))
    assert f'const MAX_BYTES = {wiso_upload.MAX_WISO_DB_BODY};' in script and '__MAX_BYTES__' not in script
    assert 'innerHTML' not in script and 'outerHTML' not in script and 'insertAdjacentHTML' not in script
    assert 'onerror' not in script          # test_datev_unbooked.py sucht dieses Wort als Beleg für einen eingeschleusten Wert
    assert 'textContent' in script and "p.get('wiso_fdb')" not in script        # die Meldungsskripte bleiben die einzigen mit der Adresse
    assert "/wiso/fdb-upload?slot=" in script and "'X-CSRF-Token'" in script and 'application/octet-stream' in script


NODE = shutil.which('node')

RUNNER = r"""
const vm = require('vm');
const fs = require('fs');
const script = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3]);
const log = {xhrs: [], statuses: [], submits: [], requestSubmitCalls: 0, createdInputs: []};
const pending = [];
const elements = {};
class Element {
  constructor(id) { this.id = id; this.hidden = false; this.disabled = false; this.className = ''; this.textContent = '';
                    this.value = ''; this.files = []; this.children = []; this.listeners = {}; }
  checkValidity() { return !scenario.invalidAfterUpload; }
  reportValidity() { log.reportValidityCalls = (log.reportValidityCalls || 0) + 1; return false; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  querySelector(selector) { return selector === 'input[name="csrf"]' ? this.children.find(c => c.name === 'csrf') || null : null; }
  appendChild(child) { this.children.push(child); log.createdInputs.push(child.name); return child; }
}
for (const id of ['wisoFdbForm', 'wisoFdbClient', 'wisoFdbStandard', 'wisoFdbUpload', 'wisoFdbButton', 'wisoFdbStatus']) {
  elements[id] = new Element(id);
}
const form = elements.wisoFdbForm;
const secondPass = [];
if (!scenario.noRequestSubmit) {
  form.requestSubmit = function() {
    log.requestSubmitCalls += 1;
    const event = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
    for (const fn of form.listeners.submit || []) fn(event);
    log.submits.push({pass: 'second', prevented: event.defaultPrevented, csrf: (form.querySelector('input[name="csrf"]') || {}).value || null});
  };
}
form.submit = function() { log.submits.push({pass: 'submit()', csrf: (form.querySelector('input[name="csrf"]') || {}).value || null}); };
if (scenario.client !== null) elements.wisoFdbClient.files = [{name: 'mandant.fdb', size: scenario.client}];
if (scenario.standard !== null) elements.wisoFdbStandard.files = [{name: 'standard.fdb', size: scenario.standard}];
class Target {
  constructor() { this.handlers = {}; }
  addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  fire(type, event) { for (const fn of this.handlers[type] || []) fn(event); }
}
class XHR extends Target {
  constructor() { super(); this.upload = new Target(); this.headers = {}; log.xhrs.push(this); }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(name, value) { this.headers[name] = value; }
  send(body) { this.body = body; pending.push(this); }
  toJSON() { return {method: this.method, url: this.url, headers: this.headers, body: this.body}; }
}
const sandbox = {
  document: {
    getElementById: id => elements[id],
    querySelector: selector => selector === 'meta[name="csrf-token"]' && scenario.csrf !== null ? {content: scenario.csrf} : null,
    createElement: () => new Element('neu'),
  },
  XMLHttpRequest: XHR, JSON, Math, encodeURIComponent,
};
vm.runInNewContext(script, sandbox);
const status = elements.wisoFdbStatus;
function snapshot() { log.statuses.push({text: status.textContent, className: status.className, hidden: status.hidden,
                                         disabled: elements.wisoFdbButton.disabled}); }
function submit() {
  const event = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
  for (const fn of form.listeners.submit || []) fn(event);
  log.submits.push({pass: 'first', prevented: event.defaultPrevented});
}
submit();
if (scenario.doubleSubmit) submit();
let step = 0;
while (pending.length) {
  const xhr = pending.shift();
  const answer = scenario.responses[step++];
  for (const [loaded, total] of answer.progress || []) { xhr.upload.fire('progress', {lengthComputable: true, loaded, total}); snapshot(); }
  if (answer.network) { xhr.fire('error', {}); }
  else { xhr.status = answer.status; xhr.responseText = answer.body; xhr.fire('load', {}); }
  snapshot();
}
log.final = {text: status.textContent, className: status.className, hidden: status.hidden, buttonDisabled: elements.wisoFdbButton.disabled,
             uploadValue: elements.wisoFdbUpload.value};
console.log(JSON.stringify(log));
"""

TOKEN = 'ab' * 16


def run_script(tmp_path, page, **scenario):
    """Das Upload-Skript der Seite in Node, mit einem gespielten Browser (Elemente, ``XMLHttpRequest``): liefert, was es
    getan hat – die gesendeten Anfragen, die Meldungen und ob das Formular abgeschickt wurde."""
    scenario = {'client': 1000, 'standard': None, 'csrf': 'tok', 'responses': [], **scenario}
    source, runner = tmp_path / 'upload.js', tmp_path / 'run_upload.js'
    source.write_text(_upload_script(page), encoding='utf-8')
    runner.write_text(RUNNER, encoding='utf-8')
    done = subprocess.run([NODE, str(runner), str(source), json.dumps(scenario)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def ok(token=TOKEN, **extra):
    return {'status': 200, 'body': json.dumps({'success': True, 'upload': token}), **extra}


needs_node = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – der statische Test '
                                                      '(test_the_script_carries_the_limit_from_python_and_no_html_from_data) '
                                                      'deckt nur die Form ab')


@needs_node
def test_the_script_uploads_the_client_file_and_then_submits_the_form(db, tmp_path):
    log = run_script(tmp_path, PageMiscellaneous(db), responses=[ok(progress=[[45, 100]])])
    (xhr,) = log['xhrs']
    assert xhr['method'] == 'POST' and xhr['url'] == '/wiso/fdb-upload?slot=client'
    assert xhr['headers'] == {'Content-Type': 'application/octet-stream', 'X-CSRF-Token': 'tok'}
    assert xhr['body'] == {'name': 'mandant.fdb', 'size': 1000}                  # die Datei selbst, nicht ihr Inhalt als Text
    assert log['statuses'][0]['text'] == 'DB1.FDB wird hochgeladen … 45 %'
    assert log['final']['uploadValue'] == TOKEN
    assert log['final']['text'].startswith('Import läuft …') and log['final']['buttonDisabled'] is True
    assert log['requestSubmitCalls'] == 1
    assert [s['pass'] for s in log['submits']] == ['first', 'second']
    assert log['submits'][0]['prevented'] is True                                 # der erste Durchlauf schickt nichts ab …
    assert log['submits'][1]['prevented'] is False                                # … der zweite läuft durch
    assert log['submits'][1]['csrf'] == 'tok'                                     # und trägt das CSRF-Feld


@needs_node
def test_the_script_uploads_the_standard_file_with_the_token_of_the_first(db, tmp_path):
    log = run_script(tmp_path, PageMiscellaneous(db), standard=2000,
                     responses=[ok(), ok(progress=[[1, 4]])])
    first, second = log['xhrs']
    assert first['url'] == '/wiso/fdb-upload?slot=client'
    assert second['url'] == f'/wiso/fdb-upload?slot=standard&upload={TOKEN}'
    assert second['body'] == {'name': 'standard.fdb', 'size': 2000}
    assert any(s['text'] == 'DB0.FDB wird hochgeladen … 25 %' for s in log['statuses'])
    assert log['final']['uploadValue'] == TOKEN and log['requestSubmitCalls'] == 1


@needs_node
def test_the_script_does_not_submit_a_form_that_became_invalid_during_the_upload(db, tmp_path):
    """Das Pflicht-Kästchen während des Uploads entfernt: ``requestSubmit`` schickte nichts ab und die Seite bliebe bei
    „Import läuft …“ mit gesperrtem Knopf stehen – stattdessen eine Meldung, und der Knopf ist wieder frei."""
    log = run_script(tmp_path, PageMiscellaneous(db), responses=[ok()], invalidAfterUpload=True)
    assert log['requestSubmitCalls'] == 0 and log['reportValidityCalls'] == 1
    assert log['final']['text'] == 'Bitte die Angaben im Formular prüfen und erneut absenden.'
    assert log['final']['className'] == 'errorColor' and log['final']['buttonDisabled'] is False


@needs_node
def test_the_script_does_not_submit_when_the_server_refuses_and_frees_the_button(db, tmp_path):
    body = json.dumps({'success': False, 'error': 'Die Anfrage ist zu groß (höchstens 2 GB).'})
    log = run_script(tmp_path, PageMiscellaneous(db), responses=[{'status': 413, 'body': body}])
    assert log['final']['text'] == 'Die Anfrage ist zu groß (höchstens 2 GB).'
    assert log['final']['className'] == 'errorColor' and log['final']['buttonDisabled'] is False
    assert log['requestSubmitCalls'] == 0 and log['final']['uploadValue'] == ''


@needs_node
@pytest.mark.parametrize('answer', [{'status': 403, 'body': '<h1>403</h1><p>Ungültiges Token.</p>'},
                                    {'status': 500, 'body': ''},
                                    {'status': 200, 'body': 'kein JSON'},
                                    {'status': 200, 'body': json.dumps({'success': True})},
                                    {'status': 200, 'body': json.dumps({'success': False, 'error': ''})}],
                         ids=['HTML', 'leer', 'kein-JSON', 'ohne-Kennung', 'ohne-Text'])
def test_the_script_names_the_status_when_the_answer_has_no_message(db, tmp_path, answer):
    log = run_script(tmp_path, PageMiscellaneous(db), responses=[answer])
    assert log['final']['text'] == f"Der Upload ist fehlgeschlagen (Status {answer['status']})."
    assert log['final']['buttonDisabled'] is False and log['requestSubmitCalls'] == 0


@needs_node
def test_the_script_explains_a_network_error(db, tmp_path):
    log = run_script(tmp_path, PageMiscellaneous(db), responses=[{'network': True}])
    assert log['final']['text'] == ('Der Upload wurde abgebrochen. Ist die Datei größer, als ein vorgeschalteter Proxy '
                                    'erlaubt?')
    assert log['final']['className'] == 'errorColor' and log['final']['buttonDisabled'] is False
    assert log['requestSubmitCalls'] == 0


@needs_node
def test_a_failing_standard_upload_stops_before_the_import(db, tmp_path):
    body = json.dumps({'success': False, 'error': 'Ungültige Anfrage.'})
    log = run_script(tmp_path, PageMiscellaneous(db), standard=10, responses=[ok(), {'status': 400, 'body': body}])
    assert len(log['xhrs']) == 2 and log['final']['text'] == 'Ungültige Anfrage.'
    assert log['requestSubmitCalls'] == 0 and log['final']['buttonDisabled'] is False


@needs_node
@pytest.mark.parametrize('client, standard, name', [(2 ** 31 + 1, None, 'DB1.FDB'), (10, 2 ** 31 + 1, 'DB0.FDB'),
                                                    (2 ** 40, 2 ** 40, 'DB1.FDB')])
def test_the_script_refuses_a_file_over_the_limit_without_uploading(db, tmp_path, client, standard, name):
    log = run_script(tmp_path, PageMiscellaneous(db), client=client, standard=standard)
    assert log['xhrs'] == [] and log['requestSubmitCalls'] == 0
    assert log['final']['text'] == f'{name} ist größer als 2 GB und lässt sich nicht hochladen.'
    assert log['final']['buttonDisabled'] is False and log['final']['className'] == 'errorColor'


@needs_node
def test_a_file_of_exactly_the_limit_is_uploaded(db, tmp_path):
    log = run_script(tmp_path, PageMiscellaneous(db), client=wiso_upload.MAX_WISO_DB_BODY, responses=[ok()])
    assert len(log['xhrs']) == 1 and log['requestSubmitCalls'] == 1


@needs_node
def test_the_script_asks_for_the_client_file(db, tmp_path):
    log = run_script(tmp_path, PageMiscellaneous(db), client=None)
    assert log['xhrs'] == [] and log['final']['text'] == 'Bitte die Mandantendatei DB1.FDB wählen.'
    assert log['submits'] == [{'pass': 'first', 'prevented': True}]


@needs_node
def test_a_second_submit_while_uploading_starts_nothing_new(db, tmp_path):
    log = run_script(tmp_path, PageMiscellaneous(db), doubleSubmit=True, responses=[ok()])
    assert len(log['xhrs']) == 1 and log['requestSubmitCalls'] == 1
    assert [s['prevented'] for s in log['submits'] if s['pass'] == 'first'] == [True, True]


@needs_node
def test_the_csrf_field_is_set_by_the_script_even_without_the_footer_script(db, tmp_path):
    log = run_script(tmp_path, PageMiscellaneous(db), responses=[ok()])
    assert log['createdInputs'] == ['csrf'] and log['submits'][-1]['csrf'] == 'tok'


@needs_node
def test_the_script_falls_back_to_submit_in_a_browser_without_request_submit(db, tmp_path):
    log = run_script(tmp_path, PageMiscellaneous(db), noRequestSubmit=True, responses=[ok()])
    assert log['requestSubmitCalls'] == 0
    assert log['submits'] == [{'pass': 'first', 'prevented': True}, {'pass': 'submit()', 'csrf': 'tok'}]   # das Feld setzt das Skript selbst


@needs_node
def test_the_script_sends_no_token_header_when_the_page_has_none(db, tmp_path):
    log = run_script(tmp_path, PageMiscellaneous(db), csrf=None, responses=[ok()])
    assert 'X-CSRF-Token' not in log['xhrs'][0]['headers'] and log['createdInputs'] == []
