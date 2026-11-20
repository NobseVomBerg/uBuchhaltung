# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Meldungen auf der Seite *Sonstiges* (2.5.5): feste Sätze statt Ausnahmetext.

Nach Sicherung, Wiederherstellung, DATEV-Export und WISO-Import (``/miscellaneous?…&msg=``) stand der Text der Ausnahme in der
Adresse und auf der Seite – mit Serverpfaden, der Ausgabe von 7-Zip oder dem Fehler von SQLite. Jetzt:

* Klartext nur aus ``backup.BackupRefused`` (Ablehnungen des Moduls mit eigenem Wortlaut); jeder andere Fehler ist ein fester
  Satz (``BACKUP_FAILED``, ``RESTORE_FAILED``, ``DATEV_FAILED``, ``WISO_IMPORT_FAILED``), der volle Fehler steht im Server-Log.
* Die Ausgabe von 7-Zip und Pfade aus dem Archiv stehen nur im Log, die Meldung nennt den Grund in eigenen Worten.
* Die Seite liest ``msg`` nicht mehr im Browser (``URLSearchParams`` plus ``decodeURIComponent``: ein „%“ in der Meldung ließ sie
  verschwinden), sondern bekommt sie vom Server als JSON im Skriptblock ``miscMsg`` (``script_json``, höchstens
  ``MSG_MAX_LENGTH`` Zeichen) und zeigt sie nur über ``textContent``/``appMsg``.

Alle Namen, Pfade und Inhalte sind erfundene Testwerte.
"""
import io
import json
import os
import urllib.parse
from decimal import Decimal
from types import SimpleNamespace

import pytest

import userctx
from server import backup as backup_mod
from server import handlers
from server.pages_miscellaneous import MSG_MAX_LENGTH
from tests.backup_helpers import Archives, MoveFault, STEM, snapshot, standard_files, write_db
from tests.dom_helpers import Dom
from tests.http_client import get, own_origin, post

SECRET = '/geheim/pfad/archiv.7zip'
LISTING = (b'\n7-Zip 23.01 (x64)\n\n--\nPath = archiv.7zip\nType = 7z\n\n----------\n'
           b'Path = buch.db\nSize = 5\nAttributes = A -rw-r--r--\n')


def answer(result):
    """(Status, Pfad, Parameter, ganze Adresse) einer Weiterleitung ``(303, Adresse)``."""
    status, location = result
    parts = urllib.parse.urlsplit(location)
    return status, parts.path, urllib.parse.parse_qs(parts.query), location


def shown(location):
    """Die Meldung, wie die Seite sie bekäme: der Parameter ``msg`` der Adresse, einmal dekodiert."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)['msg'][0]


@pytest.fixture
def user_dir():
    """Das Datenverzeichnis, das die Handler ohne Benutzer im Kontext nehmen (``userctx.user_data_dir()``)."""
    os.makedirs(userctx.DATA_ROOT)
    return userctx.DATA_ROOT


# ── Sichern ──────────────────────────────────────────────────────────────────
def test_a_failed_backup_is_a_fixed_sentence_and_the_error_is_in_the_log(tmp_db, monkeypatch, capsys):
    def full(*args, **kwargs):
        raise OSError(f'{SECRET} voll')
    monkeypatch.setattr(backup_mod, 'create_backup', full)
    status, path, query, location = answer(handlers.handle_backup_create(tmp_db, {'scope': ['db']}))
    assert (status, path) == (303, '/miscellaneous')
    assert query == {'backup': ['error'], 'msg': [handlers.BACKUP_FAILED]}
    assert 'geheim' not in urllib.parse.unquote(location) and 'voll' not in location
    assert SECRET in capsys.readouterr().err                          # die Einzelheiten stehen im Server-Log


def test_a_backup_that_cannot_start_because_one_is_running_says_so_in_plain_text(tmp_db, user_dir):
    with backup_mod._exclusive(user_dir):
        _, _, query, _ = answer(handlers.handle_backup_create(tmp_db, {}))
    assert query == {'backup': ['error'], 'msg': [backup_mod.BUSY]}


def test_the_stderr_of_sevenzip_never_reaches_the_message_of_a_backup(tmp_db, user_dir, monkeypatch, capsys):
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: '/fake/7z')
    monkeypatch.setattr(backup_mod.subprocess, 'run', lambda command, *a, **k: SimpleNamespace(
        returncode=2, stdout=b'', stderr=f'ERROR: {SECRET}: Zugriff verweigert'.encode()))
    _, _, query, location = answer(handlers.handle_backup_create(tmp_db, {'scope': ['db']}))
    assert query['msg'] == [handlers.BACKUP_FAILED] and 'geheim' not in urllib.parse.unquote(location)
    assert SECRET in capsys.readouterr().err
    assert backup_mod.list_backups(user_dir) == []                    # und kein halbes Archiv bleibt liegen


# ── Wiederherstellen ─────────────────────────────────────────────────────────
@pytest.fixture
def sevenzip_archive(user_dir, monkeypatch):
    """Ein „7-Zip-Archiv“ in backup/ und ein 7-Zip, das auflisten kann, beim Entpacken aber mit einem Pfad im Text scheitert."""
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: '/fake/7z')
    name = STEM + '.7zip'
    os.makedirs(os.path.join(user_dir, 'backup'))
    with open(os.path.join(user_dir, 'backup', name), 'wb') as handle:
        handle.write(b'kein echtes Archiv')

    def run(command, *args, **kwargs):
        if command[1] == 'l':
            return SimpleNamespace(returncode=0, stdout=LISTING, stderr=b'')
        return SimpleNamespace(returncode=2, stdout=b'', stderr=f'ERROR: {SECRET}: Data Error'.encode())
    monkeypatch.setattr(backup_mod.subprocess, 'run', run)
    return name


def test_the_stderr_of_sevenzip_never_reaches_the_message_of_a_restore(sevenzip_archive, capsys):
    _, _, query, location = answer(handlers.handle_backup_restore(None, {'archive': [sevenzip_archive]}))
    assert query == {'restore': ['error'], 'msg': [backup_mod.EXTRACT_FAILED]}
    assert 'geheim' not in urllib.parse.unquote(location) and 'ERROR' not in location
    assert f'7-Zip-Fehler beim Entpacken: ERROR: {SECRET}' in capsys.readouterr().err


def test_a_path_in_the_archive_is_in_the_log_but_not_in_the_message(user_dir, monkeypatch, capsys):
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: '/fake/7z')
    name = STEM + '.7zip'
    os.makedirs(os.path.join(user_dir, 'backup'))
    with open(os.path.join(user_dir, 'backup', name), 'wb') as handle:
        handle.write(b'kein echtes Archiv')
    unsafe = LISTING + b'Path = /etc/geheim_passwd\nSize = 5\n'
    monkeypatch.setattr(backup_mod.subprocess, 'run',
                        lambda command, *a, **k: SimpleNamespace(returncode=0, stdout=unsafe, stderr=b''))
    _, _, query, location = answer(handlers.handle_backup_restore(None, {'archive': [name]}))
    assert query['msg'] == [backup_mod.UNSAFE_PATH] and 'geheim_passwd' not in urllib.parse.unquote(location)
    assert "Sicherung abgelehnt, Pfad im Archiv: '/etc/geheim_passwd'" in capsys.readouterr().err


def test_a_zip_with_a_path_out_of_the_folder_names_it_only_in_the_log(user_dir, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)
    import zipfile
    os.makedirs(os.path.join(user_dir, 'backup'))
    with zipfile.ZipFile(os.path.join(user_dir, 'backup', STEM + '.zip'), 'w') as archive:
        archive.writestr('../../geheim_aussen.txt', 'böse')
    _, _, query, location = answer(handlers.handle_backup_restore(None, {'archive': [STEM + '.zip']}))
    assert query['msg'] == [backup_mod.UNSAFE_PATH] and 'geheim_aussen' not in urllib.parse.unquote(location)
    assert 'geheim_aussen' in capsys.readouterr().err


@pytest.fixture
def wipe_world(user_dir, tmp_path, monkeypatch):
    """Datenverzeichnis mit Datenbank und Archiv; ``MoveFault`` lässt das Verschieben beim Wiederherstellen scheitern."""
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)
    write_db(os.path.join(user_dir, 'buch.db'), value='original')
    with open(os.path.join(user_dir, 'notiz.txt'), 'w', encoding='utf-8') as handle:
        handle.write('Notiz Version 1')
    name = Archives('zip', tmp_path).build(user_dir, standard_files(tmp_path))
    return SimpleNamespace(folder=user_dir, name=name, before=snapshot(user_dir))


def test_a_failed_move_says_that_nothing_changed_without_the_text_of_the_os_error(wipe_world, monkeypatch, capsys):
    MoveFault(wipe_world.folder, fail_at=3).install(monkeypatch)
    _, _, query, location = answer(handlers.handle_backup_restore(None, {'archive': [wipe_world.name], 'mode': ['wipe']}))
    message = query['msg'][0]
    assert query['restore'] == ['error'] and 'vorherige Stand ist unverändert' in message
    assert 'Einzelheiten stehen im Server-Log' in message
    assert '[Errno' not in message and 'Eingabe-/Ausgabefehler' not in message and wipe_world.folder not in urllib.parse.unquote(location)
    log = capsys.readouterr().err
    assert '[Errno 5] Eingabe-/Ausgabefehler (Test)' in log           # der volle Fehler steht im Server-Log
    assert snapshot(wipe_world.folder) == wipe_world.before


def test_a_rollback_that_fails_too_names_the_folder_of_the_previous_state_but_no_os_error(wipe_world, monkeypatch, capsys):
    MoveFault(wipe_world.folder, fail_at=2, onwards=True).install(monkeypatch)
    _, _, query, location = answer(handlers.handle_backup_restore(None, {'archive': [wipe_world.name], 'mode': ['wipe']}))
    message = query['msg'][0]
    assert 'backup/replaced_' in message and 'Einzelheiten stehen im Server-Log' in message
    assert '[Errno' not in message and wipe_world.folder not in urllib.parse.unquote(location)
    assert '[Errno 5]' in capsys.readouterr().err


def test_an_unexpected_error_during_a_restore_is_a_fixed_sentence(wipe_world, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise ValueError(f'{SECRET} unerwartet')
    monkeypatch.setattr(backup_mod, '_check_staging', boom)
    _, _, query, location = answer(handlers.handle_backup_restore(None, {'archive': [wipe_world.name]}))
    assert query == {'restore': ['error'], 'msg': [handlers.RESTORE_FAILED]} and 'geheim' not in urllib.parse.unquote(location)
    assert SECRET in capsys.readouterr().err


def test_a_restore_without_an_archive_or_with_an_unknown_one_has_a_fixed_sentence(user_dir):
    assert answer(handlers.handle_backup_restore(None, {}))[2] == {'restore': ['error'], 'msg': [handlers.RESTORE_NO_ARCHIVE]}
    for spelling in ('gibtsnicht.7zip', '../../etc/passwd'):
        query = answer(handlers.handle_backup_restore(None, {'archive': [spelling]}))[2]
        assert query == {'restore': ['error'], 'msg': [backup_mod.NOT_FOUND]}, spelling


# ── DATEV und WISO ───────────────────────────────────────────────────────────
def test_the_datev_export_names_no_text_of_an_error(tmp_db, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError(f'{SECRET} kaputt')
    monkeypatch.setattr(tmp_db, 'find_unbalanced_splits', boom)
    _, _, query, location = answer(handlers.handle_datev_export(
        tmp_db, {'date_from': ['2026-01-01'], 'date_to': ['2026-12-31']}))
    assert query == {'datev_export': ['error'], 'msg': [handlers.DATEV_FAILED]}
    assert 'geheim' not in urllib.parse.unquote(location) and SECRET in capsys.readouterr().err


class FakeRequest:
    """Minimaler Ersatz für den Request-Handler (wie in tests/test_upload_handlers.py)."""

    def __init__(self, body):
        self.headers = {'Content-Type': 'multipart/form-data; boundary=B', 'Content-Length': str(len(body))}
        self.rfile = io.BytesIO(body)


def test_the_wiso_text_import_names_no_text_of_an_error(db_with_coa, monkeypatch, capsys):
    body = (b'--B\r\nContent-Disposition: form-data; name="csvfile"; filename="export.csv"\r\n'
            b'Content-Type: text/csv\r\n\r\nirgendein Inhalt\r\n--B--\r\n')

    def boom(*args, **kwargs):
        raise RuntimeError(f'{SECRET} Platte voll')
    monkeypatch.setattr(db_with_coa, 'import_wiso_csv', boom)
    _, _, query, location = answer(handlers.handle_wiso_import(FakeRequest(body), db_with_coa))
    assert query == {'wiso_import': ['error'], 'msg': [handlers.WISO_IMPORT_FAILED]}
    assert 'geheim' not in urllib.parse.unquote(location) and SECRET in capsys.readouterr().err


def test_the_fixed_sentences_say_where_the_details_are_and_need_no_path():
    for sentence in (handlers.BACKUP_FAILED, handlers.RESTORE_FAILED, handlers.DATEV_FAILED, handlers.WISO_IMPORT_FAILED):
        assert sentence.endswith('Einzelheiten stehen im Server-Log.') and '/' not in sentence and '\\' not in sentence
    assert handlers.RESTORE_NO_ARCHIVE == 'Kein Archiv ausgewählt.'


# ── Die Seite ────────────────────────────────────────────────────────────────
def misc_msg(page):
    """Die Meldung aus dem Skriptblock ``var miscMsg = …;`` einer Seite, wie der Browser sie läse (ein ``</script>`` darin beendet
    den Block)."""
    start = 'var miscMsg = '
    found = [script for script in Dom(page).scripts if script.startswith(start)]
    assert len(found) == 1, found
    value, end = json.JSONDecoder().raw_decode(found[0], len(start))
    assert found[0][end:] == ';'
    return value


def test_the_page_gets_the_message_from_the_server_as_json_in_a_script_block(server_port, auto_login):
    assert misc_msg(get(server_port, '/miscellaneous').text) == ''
    page = get(server_port, '/miscellaneous?backup=error&msg=' + urllib.parse.quote(handlers.BACKUP_FAILED, safe='')).text
    assert misc_msg(page) == handlers.BACKUP_FAILED
    assert "text = 'Backup fehlgeschlagen: ' + miscMsg;" in page
    assert page.index('var miscMsg') < page.index("p.get('backup')")      # steht vor jedem Skript, das sie braucht


@pytest.mark.parametrize('raw, text', [
    ('%3C%2Fscript%3E%3Cb%3Ex', '</script><b>x'),
    ('100%25', '100%'),
    ('%2525', '%25'),
    ('a%26b%3Dc%23d+e', 'a&b=c#d e'),
    ('%22%27%5C%0A%E2%80%A8', '"\'\\\n '),
    ('Fehler%20in%20%C3%84rger', 'Fehler in Ärger'),
])
def test_a_message_from_the_address_never_leaves_its_script_block_and_is_decoded_once(server_port, auto_login, raw, text):
    page = get(server_port, f'/miscellaneous?restore=error&msg={raw}').text
    assert misc_msg(page) == text
    assert '</script><b>' not in page and not [tag for tag, _ in Dom(page).tags if tag == 'b']
    assert 'decodeURIComponent(p.get' not in page


def test_a_very_long_message_is_cut_at_the_limit_and_the_first_of_several_counts(server_port, auto_login):
    assert len(misc_msg(get(server_port, '/miscellaneous?msg=' + 'x' * 5000).text)) == MSG_MAX_LENGTH
    assert misc_msg(get(server_port, '/miscellaneous?msg=erste&msg=zweite').text) == 'erste'


def test_the_longest_datev_hint_of_the_program_fits_and_arrives_whole(server_port, auto_login):
    """Ein Hinweis, der abgeschnitten käme, ließe den Nutzer bestätigen, was er nicht gelesen hat: Die Grenze liegt über der längsten
    Meldung des DATEV-Exports (alle drei Hinweise mit je fünf Einträgen und großen Beträgen)."""
    rows = [(index, f'2026-03-{index + 1:02d}', Decimal('-999999999.99')) for index in range(9)]
    message = ('Nicht exportiert: ' + ' '.join([handlers._datev_unbooked_hint(rows),
                                                 handlers._datev_elsewhere_hint(rows, rows),
                                                 handlers._datev_doubtful_hint(rows, ('2026-03-05', Decimal('-999999999.99')))])
               + ' Bitte bestätigen, um trotzdem zu exportieren.')
    assert 1000 < len(message) < MSG_MAX_LENGTH
    page = get(server_port, '/miscellaneous?datev_export=confirm&msg=' + urllib.parse.quote(message)).text
    assert misc_msg(page) == message


def test_the_scripts_of_the_page_show_the_message_only_as_text(server_port, auto_login):
    page = get(server_port, '/miscellaneous').text
    for use in ("'❌ Fehler beim Export: ' + miscMsg", "'❌ DATEV-Export Fehler: ' + miscMsg", "'⚠️ DATEV-Export: ' + miscMsg",
                "'\\u274c Fehler: ' + miscMsg", "'Backup fehlgeschlagen: ' + miscMsg", "'Wiederherstellung fehlgeschlagen: ' + miscMsg",
                "+ miscMsg\n"):
        assert use in page, use
    assert page.count('miscMsg') == 9                                     # die Definition und acht Stellen, die sie anzeigen
    for forbidden in ("p.get('msg')", 'decodeURIComponent(p.get'):
        assert forbidden not in page


def test_the_source_of_the_page_never_reads_the_message_in_the_browser():
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'server', 'pages_miscellaneous.py'),
              encoding='utf-8') as handle:
        source = handle.read()
    assert "decodeURIComponent(p.get('msg')" not in source and "p.get('msg')" not in source


def test_a_failed_backup_over_http_shows_the_fixed_sentence_and_not_the_error(server_port, auto_login, monkeypatch, capsys):
    def full(*args, **kwargs):
        raise OSError(f'{SECRET} voll')
    monkeypatch.setattr(backup_mod, 'create_backup', full)
    resp = post(server_port, '/backup/create', headers={'Origin': own_origin(server_port)}, csrf=auto_login.csrf,
                form={'scope': 'db'})
    assert resp.status == 303
    assert answer((resp.status, resp.headers['Location']))[2] == {'backup': ['error'], 'msg': [handlers.BACKUP_FAILED]}
    page = get(server_port, resp.headers['Location'])
    assert page.status == 200 and misc_msg(page.text) == handlers.BACKUP_FAILED and 'geheim' not in page.text
    assert SECRET in capsys.readouterr().err


# ── Überschreiben: der Schritt der Datenbank nennt den Stand ─────────────────
def _overlay_world(tmp_path):
    user_dir, staging = tmp_path / 'user', tmp_path / 'staging'
    user_dir.mkdir()
    staging.mkdir()
    (user_dir / 'buch.db').write_bytes(b'alt')
    (staging / 'buch.db').write_bytes(b'neu')
    (staging / 'beleg.txt').write_bytes(b'beleg')
    return str(user_dir), str(staging)


def test_a_failing_database_step_of_the_overlay_names_the_state(tmp_path, monkeypatch):
    """Scheitert beim Überschreiben erst das Ersetzen der Datenbank, sind die übrigen Dateien schon zurückgespielt – die
    Meldung sagt das, statt des allgemeinen „fehlgeschlagen“, und nennt weder Pfad noch Systemfehler."""
    user_dir, staging = _overlay_world(tmp_path)
    real = backup_mod._move_file

    def failing(src, dst):
        if os.path.basename(dst) == 'buch.db':
            raise OSError(5, 'Input/output error', dst)
        real(src, dst)
    monkeypatch.setattr(backup_mod, '_move_file', failing)
    with pytest.raises(backup_mod.BackupRefused) as raised:
        backup_mod._overlay(staging, user_dir)
    assert str(raised.value) == backup_mod.OVERLAY_DB_FAILED and isinstance(raised.value.__cause__, OSError)
    assert user_dir not in str(raised.value) and 'Errno' not in str(raised.value)
    with open(os.path.join(user_dir, 'buch.db'), 'rb') as handle:
        assert handle.read() == b'alt'
    assert os.path.exists(os.path.join(user_dir, 'beleg.txt'))


def test_a_sidecar_that_cannot_be_removed_is_named_and_the_others_still_go(tmp_path, monkeypatch):
    """Ein altes Journal neben der zurückgespielten Datenbank würde sie beschädigen: Lässt es sich nicht entfernen, sagt die
    Meldung, was zu tun ist; die übrigen Begleitdateien werden trotzdem entfernt."""
    user_dir, staging = _overlay_world(tmp_path)
    for name in backup_mod._DB_SIDECARS:
        with open(os.path.join(user_dir, name), 'wb') as handle:
            handle.write(b'x')
    stuck = backup_mod._DB_SIDECARS[0]
    real = os.remove

    def remove(path, *args, **kwargs):
        if os.path.basename(path) == stuck:
            raise PermissionError(13, 'Permission denied', path)
        real(path, *args, **kwargs)
    monkeypatch.setattr(backup_mod.os, 'remove', remove)
    with pytest.raises(backup_mod.BackupRefused) as raised:
        backup_mod._overlay(staging, user_dir)
    assert str(raised.value) == backup_mod.OVERLAY_SIDECARS_LEFT
    with open(os.path.join(user_dir, 'buch.db'), 'rb') as handle:
        assert handle.read() == b'neu'
    assert [name for name in backup_mod._DB_SIDECARS if os.path.exists(os.path.join(user_dir, name))] == [stuck]
