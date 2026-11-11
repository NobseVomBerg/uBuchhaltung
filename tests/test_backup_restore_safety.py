# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Wiederherstellen prüft vor dem Ersetzen (server/backup.py, Version 2.0.3).

Kern: Scheitert Entpacken oder Prüfen, ist das Datenverzeichnis byteweise unverändert und in
backup/ liegen keine restore_*/replaced_*-Reste – im Modus „überschreiben“ wie bei „vorher
löschen“ (wipe), für zip (stdlib) und 7-Zip. Geprüft werden:

- defekte Archive (zip und 7-Zip), 7-Zip nicht installiert
- die Datenbank im Archiv: beschädigt, leer, neuer als das Programm, gleich alt/älter (angenommen)
- „wipe“ ohne Datenbank im Archiv, „keine Daten“, ausgenommene Einträge (backup/, users/, tests/)
- Zip-Slip, symbolische Links im zip, Typkonflikte (Datei über Verzeichnis und umgekehrt)
- Begleitdateien der Datenbank (-journal/-wal/-shm) bei Sicherung und Wiederherstellung
- Erfolg von „wipe“ (leere Verzeichnisse, Ausnahmen unberührt), Zurückrollen bei Fehlern,
  Reihenfolge entpacken → prüfen → ersetzen, der Handler handle_backup_restore

Weitere Absicherungen (anderes Dateisystem, Datenbank zuletzt, nur-Datenbank-Sicherung,
7-Zip-Liste, Sperre, Aufräumen) stehen in test_backup_restore_hardening.py; Hilfen in
backup_helpers.py. Alle Inhalte sind synthetische Testdaten.
"""
import os
import random
import re
import shutil
import sqlite3
import urllib.parse
import zipfile
import zlib
from types import SimpleNamespace

import pytest

import userctx
import version
from server import backup as backup_mod
from server import handlers
from tests import installation
from tests.backup_helpers import (
    SIDECARS, STEM, Archives, MoveFault, archive_names, archive_path, damaged_db_bytes, db_bytes,
    db_value, leftovers, make_archives, make_user_dir, require_sevenzip, sevenzip_add, snapshot, standard_files,
    write_db)
from tests.http_client import post

WIPE = pytest.mark.parametrize('wipe', [False, True], ids=['ueberschreiben', 'wipe'])


@pytest.fixture
def user_dir(tmp_path):
    return make_user_dir(tmp_path)


@pytest.fixture(params=['zip', '7z'])
def fmt(request, monkeypatch, tmp_path):
    """Beide Wege von create_backup/restore_backup: stdlib-zip und 7-Zip."""
    return make_archives(request.param, monkeypatch, tmp_path)


@pytest.fixture
def zip_only(monkeypatch, tmp_path):
    return make_archives('zip', monkeypatch, tmp_path)


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def assert_rejected(user_dir, name, wipe, match=None):
    """Das Wiederherstellen scheitert mit RuntimeError, das Datenverzeichnis ist byteweise
    unverändert, backup/ ohne Reste, das Archiv noch da. Liefert die Fehlermeldung."""
    before = snapshot(user_dir)
    with pytest.raises(RuntimeError, match=match) as raised:
        backup_mod.restore_backup(user_dir, name, wipe=wipe)
    assert snapshot(user_dir) == before, 'das Datenverzeichnis wurde verändert'
    assert leftovers(user_dir) == []
    assert os.path.isfile(archive_path(user_dir, name))
    return str(raised.value)


# ── Defekte Archive ──────────────────────────────────────────────────────────
def _sample_zip(path, tmp_path, stored=False):
    text = ('Zeile mit etwas Text, die sich gut komprimieren lässt. ' * 4000).encode('utf-8')
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, 'w', method) as zf:
        zf.writestr('Belege/gross.txt', text)
        zf.writestr('buch.db', db_bytes(tmp_path, value='aus Sicherung'))
        zf.writestr('notiz.txt', 'Notiz aus der Sicherung')


def _data_offset(path, member):
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(member)
    return info.header_offset + 30 + len(info.filename.encode('utf-8')) + len(info.extra)


def write_defective_zip(kind, path, tmp_path):
    if kind == 'zufallsbytes':
        data = random.Random(1).randbytes(4096)
    elif kind == 'null-bytes':
        data = b''
    else:
        _sample_zip(path, tmp_path, stored=(kind == 'pruefsumme-falsch'))
        with open(path, 'rb') as f:
            data = bytearray(f.read())
        offset = _data_offset(path, 'Belege/gross.txt')
        if kind == 'abgeschnitten':
            data = data[:len(data) // 2]
        elif kind == 'datenstrom-verfaelscht':
            for i in range(offset + 20, offset + 60):
                data[i] ^= 0xFF
        else:                                                     # pruefsumme-falsch
            data[offset + 100] ^= 0x01
    with open(path, 'wb') as f:
        f.write(data)


DEFECTIVE_ZIPS = ['zufallsbytes', 'null-bytes', 'abgeschnitten', 'datenstrom-verfaelscht',
                  'pruefsumme-falsch']


@pytest.mark.parametrize('kind', DEFECTIVE_ZIPS)
@WIPE
def test_defective_zip_is_rejected_and_nothing_changes(user_dir, zip_only, tmp_path, kind, wipe):
    name = STEM + '.zip'
    write_defective_zip(kind, archive_path(user_dir, name), tmp_path)
    assert_rejected(user_dir, name, wipe, 'beschädigt')


def test_defective_zip_fixtures_provoke_the_intended_errors(tmp_path):
    """Die Aufbauten oben treffen die beiden Zweige der Fehlerbehandlung: ein zerstörter
    Datenstrom löst zlib.error aus, eine falsche Prüfsumme BadZipFile (Bad CRC-32)."""
    broken = tmp_path / 'datenstrom.zip'
    write_defective_zip('datenstrom-verfaelscht', broken, tmp_path)
    with zipfile.ZipFile(broken) as zf, pytest.raises(zlib.error):
        zf.extractall(tmp_path / 'probe1')
    wrong_crc = tmp_path / 'crc.zip'
    write_defective_zip('pruefsumme-falsch', wrong_crc, tmp_path)
    with zipfile.ZipFile(wrong_crc) as zf, pytest.raises(zipfile.BadZipFile, match='CRC'):
        zf.extractall(tmp_path / 'probe2')


@pytest.mark.parametrize('error', [zipfile.BadZipFile('x'), zlib.error('x'), EOFError()],
                         ids=['BadZipFile', 'zlib.error', 'EOFError'])
def test_low_level_zip_errors_become_a_damaged_archive_message(user_dir, zip_only, tmp_path,
                                                               monkeypatch, error):
    """EOFError (Datenstrom endet vor seinem Ende) lässt sich mit einem echten Archiv kaum
    erzeugen – die Übersetzung wird deshalb auch direkt geprüft."""
    name = zip_only.build(user_dir, standard_files(tmp_path))

    def failing(self, *args, **kwargs):
        raise error
    monkeypatch.setattr(zipfile.ZipFile, 'extractall', failing)
    assert_rejected(user_dir, name, False, 'beschädigt')


def test_zip_that_is_really_a_sevenzip_archive_is_damaged(user_dir, tmp_path):
    sevenzip = require_sevenzip()
    source = tmp_path / 'quelle'
    source.mkdir()
    (source / 'notiz.txt').write_text('Notiz', encoding='utf-8')
    name = STEM + '.zip'
    sevenzip_add(sevenzip, archive_path(user_dir, name), source, '-t7z')    # 7z-Format trotz Endung .zip
    assert_rejected(user_dir, name, False, 'beschädigt')


@pytest.mark.parametrize('ext', ['.7zip', '.7z'])
@WIPE
def test_sevenzip_archive_without_sevenzip_is_rejected(user_dir, tmp_path, monkeypatch, ext, wipe):
    name = STEM + ext
    path = archive_path(user_dir, name)
    real = backup_mod._find_sevenzip()
    if real:
        source = tmp_path / 'quelle'
        source.mkdir()
        (source / 'notiz.txt').write_text('Notiz', encoding='utf-8')
        sevenzip_add(real, path, source)
    else:
        with open(path, 'wb') as f:
            f.write(b'7z\xbc\xaf\x27\x1c' + bytes(64))
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)
    assert_rejected(user_dir, name, wipe, 'Kein 7-Zip installiert')


def _write_sevenzip_variant(kind, user_dir, tmp_path):
    sevenzip = require_sevenzip()
    name = STEM + '.7zip'
    path = archive_path(user_dir, name)
    if kind == 'zufallsbytes':
        data = random.Random(1).randbytes(4096)
    elif kind == 'null-bytes':
        data = b''
    else:
        files = {'Belege/gross.bin': random.Random(2).randbytes(60000),
                 'buch.db': db_bytes(tmp_path), 'notiz.txt': 'Notiz aus der Sicherung'}
        Archives('7z', tmp_path, sevenzip).build(user_dir, files)
        with open(path, 'rb') as f:
            data = bytearray(f.read())
        if kind == 'abgeschnitten':
            data = data[:len(data) // 2]
        else:                                                     # verfaelscht
            for i in range(40, 80):
                data[i] ^= 0xFF
    with open(path, 'wb') as f:
        f.write(data)
    return name


@pytest.mark.parametrize('kind', ['zufallsbytes', 'null-bytes', 'abgeschnitten', 'verfaelscht'])
@WIPE
def test_defective_sevenzip_archive_is_rejected_and_nothing_changes(user_dir, tmp_path, kind, wipe):
    name = _write_sevenzip_variant(kind, user_dir, tmp_path)
    assert_rejected(user_dir, name, wipe, '7-Zip')


# ── Die Datenbank im Archiv ──────────────────────────────────────────────────
@pytest.mark.parametrize('kind', ['zufallsbytes', 'zerstoerte-seite', 'kopf-zerstoert',
                                  'abgeschnitten'])
@WIPE
def test_damaged_database_in_archive_is_rejected(user_dir, fmt, tmp_path, kind, wipe):
    files = {'buch.db': damaged_db_bytes(tmp_path, kind), 'notiz.txt': 'Notiz aus der Sicherung'}
    name = fmt.build(user_dir, files)
    assert_rejected(user_dir, name, wipe, 'beschädigt')


def test_empty_database_files_are_told_apart_from_damaged_ones(user_dir, fmt, tmp_path):
    """Eine 0-Byte-Datei gilt SQLite als gültige, leere Datenbank – zurückgespielt ersetzte sie
    die Buchhaltung durch nichts. Ebenso eine Datenbank ohne Tabellen."""
    no_tables = tmp_path / 'ohne_tabellen.db'
    conn = sqlite3.connect(no_tables)
    conn.execute(f'PRAGMA user_version = {version.SCHEMA_VERSION}')
    conn.commit()
    conn.close()
    for content in (b'', no_tables.read_bytes()):
        name = fmt.build(user_dir, {'buch.db': content, 'notiz.txt': 'Notiz aus der Sicherung'})
        for wipe in (False, True):
            message = assert_rejected(user_dir, name, wipe, 'leer')
            assert 'beschädigt' not in message


@WIPE
def test_database_from_a_newer_program_version_is_rejected_with_both_versions(
        user_dir, fmt, tmp_path, wipe):
    newer = version.SCHEMA_VERSION + 1
    files = {'buch.db': db_bytes(tmp_path, user_version=newer), 'notiz.txt': 'Notiz aus der Sicherung'}
    name = fmt.build(user_dir, files)
    message = assert_rejected(user_dir, name, wipe, 'neueren Programmversion')
    assert version.describe_schema_version(newer) in message
    assert version.describe_schema_version(version.SCHEMA_VERSION) in message
    assert str(newer) not in message                  # Klartext statt PRAGMA-Zahl


def test_database_from_a_much_newer_major_version_is_rejected(user_dir, fmt, tmp_path):
    files = {'buch.db': db_bytes(tmp_path, user_version=version.SCHEMA_VERSION + 1000),
             'notiz.txt': 'Notiz aus der Sicherung'}
    name = fmt.build(user_dir, files)
    assert_rejected(user_dir, name, True, 'neueren Programmversion')


@pytest.mark.parametrize('stamp', [version.SCHEMA_VERSION, version.SCHEMA_VERSION - 1, 6, 0],
                         ids=['gleiche-Version', 'aeltere-Version', '1.x-Stand', 'ungestempelt'])
@WIPE
def test_database_of_the_same_or_an_older_version_is_accepted(user_dir, fmt, tmp_path, stamp, wipe):
    """Genau die Grenze: gleiche Zahl wie das Programm wird angenommen, nur eine höhere nicht."""
    files = {'buch.db': db_bytes(tmp_path, value='aus Sicherung', user_version=stamp),
             'notiz.txt': 'Notiz aus der Sicherung'}
    name = fmt.build(user_dir, files)
    backup_mod.restore_backup(user_dir, name, wipe=wipe)
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'
    assert read(os.path.join(user_dir, 'notiz.txt')) == 'Notiz aus der Sicherung'
    assert leftovers(user_dir) == []


def test_restored_database_is_byte_identical_to_the_one_in_the_archive(user_dir, fmt, tmp_path):
    """Geprüft wird nur lesend: Die Datenbank kommt so zurück, wie sie im Archiv lag."""
    files = standard_files(tmp_path)
    name = fmt.build(user_dir, files)
    backup_mod.restore_backup(user_dir, name, wipe=False)
    with open(os.path.join(user_dir, 'buch.db'), 'rb') as f:
        assert f.read() == files['buch.db']


# ── „wipe“ ohne Datenbank, leere Archive, ausgenommene Einträge ──────────────
def test_wipe_without_database_in_archive_is_rejected(user_dir, fmt):
    name = fmt.build(user_dir, {'notiz.txt': 'neu', 'Belege/beleg1.txt': 'neu'})
    assert_rejected(user_dir, name, True, 'keine Datenbank')


def test_overwrite_without_database_in_archive_keeps_the_existing_database(user_dir, fmt):
    database = os.path.join(user_dir, 'buch.db')
    before = installation.file_digest(database)
    name = fmt.build(user_dir, {'notiz.txt': 'neu'})
    backup_mod.restore_backup(user_dir, name, wipe=False)
    assert installation.file_digest(database) == before
    assert db_value(database) == 'original'
    assert read(os.path.join(user_dir, 'notiz.txt')) == 'neu'
    assert leftovers(user_dir) == []


@WIPE
def test_empty_zip_archive_contains_no_data(user_dir, zip_only, wipe):
    name = STEM + '.zip'
    with zipfile.ZipFile(archive_path(user_dir, name), 'w'):
        pass
    assert_rejected(user_dir, name, wipe, 'keine Daten')


@WIPE
def test_archive_with_only_excluded_entries_contains_no_data(user_dir, fmt, wipe):
    files = {'backup/aelter.zip': 'x', 'users/u.txt': 'x', 'tests/t.txt': 'x',
             'buch.db-journal': b'\0' * 64, 'buch.db-wal': b'\0' * 64, 'buch.db-shm': b'\0' * 64}
    name = fmt.build(user_dir, files)
    assert_rejected(user_dir, name, wipe, 'keine Daten')


@WIPE
def test_excluded_top_level_entries_are_not_restored_but_nested_ones_are(user_dir, fmt,
                                                                          tmp_path, wipe):
    """backup/, users/ und tests/ gelten nur direkt im Datenverzeichnis; gleichnamige Ordner
    weiter unten (Belege/tests/) gehören zur Sicherung."""
    for rel in ('users/vorhanden.txt', 'tests/vorhanden.txt', 'backup/Eigene Notiz.txt'):
        path = os.path.join(user_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('vorhanden ' + rel)

    def excluded():
        return {path: state for path, state in installation.tree(user_dir).items()
                if path.split(os.sep)[0] in ('users', 'tests', 'backup')}

    files = standard_files(tmp_path)
    files.update({'backup/aus_dem_archiv.txt': 'x', 'users/aus_dem_archiv.txt': 'x',
                  'tests/aus_dem_archiv.txt': 'x', 'Belege/tests/tief.txt': 'tief tests',
                  'Belege/users/tief.txt': 'tief users', 'Belege/backup/tief.txt': 'tief backup'})
    name = fmt.build(user_dir, files)
    before = excluded()
    backup_mod.restore_backup(user_dir, name, wipe=wipe)

    assert excluded() == before, 'backup/, users/ und tests/ müssen unangetastet bleiben'
    for folder in ('backup', 'users', 'tests'):
        assert not os.path.exists(os.path.join(user_dir, folder, 'aus_dem_archiv.txt'))
        assert read(os.path.join(user_dir, 'Belege', folder, 'tief.txt')) == 'tief ' + folder
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'
    assert leftovers(user_dir) == []


# ── Zip-Slip, Links im zip ───────────────────────────────────────────────────
SLIP_NAMES = ['../x.txt', '../../x.txt', '../../../ausserhalb.txt', 'Belege/../../x.txt',
              'ABSOLUT']


@pytest.mark.parametrize('evil', SLIP_NAMES)
@WIPE
def test_zip_slip_member_is_rejected_and_nothing_is_created(user_dir, zip_only, tmp_path, evil,
                                                            wipe):
    if evil == 'ABSOLUT':
        evil = str(tmp_path / 'absolut_ausserhalb.txt')
    name = STEM + '.zip'
    with zipfile.ZipFile(archive_path(user_dir, name), 'w') as zf:
        zf.writestr('buch.db', db_bytes(tmp_path))
        zf.writestr('notiz.txt', 'neu')
        zf.writestr(evil, 'böse')
    world = installation.tree(tmp_path)
    with pytest.raises(RuntimeError, match='Unsicherer Pfad'):
        backup_mod.restore_backup(user_dir, name, wipe=wipe)
    assert installation.tree(tmp_path) == world, 'es darf nirgends etwas angelegt werden'


def test_symlink_member_in_a_zip_is_extracted_as_a_plain_file(user_dir, zip_only, tmp_path):
    """Python legt Links aus zip-Archiven nie als Links an – es entsteht eine gewöhnliche Datei
    mit dem Ziel als Text. Ein Link, der aus dem Datenverzeichnis hinausführt, entsteht so nicht."""
    files = standard_files(tmp_path)
    name = zip_only.build(user_dir, files, links={'Belege/weg': '/etc/hostname'})
    backup_mod.restore_backup(user_dir, name, wipe=False)
    target = os.path.join(user_dir, 'Belege', 'weg')
    assert os.path.isfile(target) and not os.path.islink(target)
    assert read(target) == '/etc/hostname'
    for root, dirs, names in os.walk(user_dir):
        assert not [n for n in dirs + names if os.path.islink(os.path.join(root, n))]


@pytest.mark.parametrize('kind', ['datei', 'verzeichnis', 'ins-leere'])
@WIPE
def test_check_staging_rejects_a_link_that_slipped_through(user_dir, tmp_path, wipe, kind):
    """Zweite Sicherung nach dem Entpacken: Ein Link im Zwischenverzeichnis wird nie
    zurückgespielt – auch dann nicht, wenn die Liste vor dem Entpacken ihn nicht erkannt hat."""
    staging = tmp_path / 'zwischen'
    (staging / 'Belege').mkdir(parents=True)
    write_db(staging / 'buch.db', value='aus Sicherung')
    (staging / 'notiz.txt').write_text('neu', encoding='utf-8')
    target = {'datei': 'notiz.txt', 'verzeichnis': 'Belege', 'ins-leere': '/gibt/es/nicht'}[kind]
    try:
        os.symlink(target, staging / 'Belege' / 'verweis')
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'das Dateisystem kann keine symbolischen Links anlegen ({exc})')
    before = snapshot(user_dir)
    with pytest.raises(RuntimeError, match='symbolische Links'):
        backup_mod._check_staging(str(staging), user_dir, wipe)
    assert snapshot(user_dir) == before


# ── Typkonflikte beim Überschreiben ──────────────────────────────────────────
def _conflict_datei_ueber_verzeichnis(user_dir):
    return {'Belege/beleg1.txt'}, {'Belege': 'ich bin eine Datei'}


def _conflict_verzeichnis_ueber_datei(user_dir):
    return {'notiz.txt'}, {'notiz.txt/inhalt.txt': 'ich liege in einem Ordner'}


def _conflict_verschachtelt_datei_ueber_verzeichnis(user_dir):
    os.makedirs(os.path.join(user_dir, 'Belege', 'unter'))
    with open(os.path.join(user_dir, 'Belege', 'unter', 'x.txt'), 'w', encoding='utf-8') as f:
        f.write('vorhanden')
    return set(), {'Belege/unter': 'ich bin eine Datei'}


def _conflict_verschachtelt_verzeichnis_ueber_datei(user_dir):
    return {'Belege/beleg1.txt'}, {'Belege/beleg1.txt/tief.txt': 'ich liege in einem Ordner'}


# Jeder Aufbau liefert (Einträge, die aus der Sicherung wegfallen, Einträge, die hinzukommen):
# Das Archiv selbst muss in sich stimmig sein, der Konflikt entsteht erst mit dem Datenverzeichnis.
CONFLICTS = {
    'datei-ueber-verzeichnis': _conflict_datei_ueber_verzeichnis,
    'verzeichnis-ueber-datei': _conflict_verzeichnis_ueber_datei,
    'verschachtelt-datei-ueber-verzeichnis': _conflict_verschachtelt_datei_ueber_verzeichnis,
    'verschachtelt-verzeichnis-ueber-datei': _conflict_verschachtelt_verzeichnis_ueber_datei,
}


def _conflicting_files(user_dir, tmp_path, conflict):
    drop, add = CONFLICTS[conflict](user_dir)
    files = {rel: content for rel, content in standard_files(tmp_path).items() if rel not in drop}
    files.update(add)
    return files


@pytest.mark.parametrize('conflict', list(CONFLICTS))
def test_type_conflict_is_rejected_before_anything_is_changed(user_dir, fmt, tmp_path, conflict):
    """Datei über Verzeichnis (und umgekehrt) lässt sich nicht überschreiben. Die Prüfung
    kommt vor der ersten Änderung: Datenbank und Notiz aus der Sicherung bleiben draußen."""
    files = _conflicting_files(user_dir, tmp_path, conflict)
    name = fmt.build(user_dir, files)
    assert_rejected(user_dir, name, False, 'zurückspielen')


@pytest.mark.parametrize('conflict', list(CONFLICTS))
def test_wipe_replaces_even_where_types_differ(user_dir, fmt, tmp_path, conflict):
    files = _conflicting_files(user_dir, tmp_path, conflict)
    name = fmt.build(user_dir, files)
    backup_mod.restore_backup(user_dir, name, wipe=True)
    for rel, content in files.items():
        if rel == 'buch.db':
            continue
        assert read(os.path.join(user_dir, *rel.split('/'))) == content
    assert leftovers(user_dir) == []


# ── Begleitdateien der Datenbank ─────────────────────────────────────────────
def _put_sidecars(folder):
    for sidecar in SIDECARS:
        with open(os.path.join(folder, sidecar), 'wb') as f:
            f.write(b'\0' * 512)            # Nullen: kein „heißes“ Journal, SQLite fasst es nicht an


def test_create_backup_leaves_the_database_companions_out(user_dir, fmt):
    _put_sidecars(user_dir)
    os.makedirs(os.path.join(user_dir, 'Belege'), exist_ok=True)
    with open(os.path.join(user_dir, 'Belege', 'buch.db-wal'), 'wb') as f:
        f.write(b'\0' * 16)                 # gleichnamig, aber nicht oberste Ebene: gehört dazu
    archive, _ = backup_mod.create_backup(user_dir, os.path.join(user_dir, 'buch.db'), 'all')
    names = archive_names(archive)
    assert 'buch.db' in names
    for sidecar in SIDECARS:
        assert sidecar not in names
    assert 'Belege/buch.db-wal' in names


def test_overwrite_with_a_database_in_the_archive_removes_the_old_companions(user_dir, fmt,
                                                                             tmp_path):
    _put_sidecars(user_dir)
    name = fmt.build(user_dir, standard_files(tmp_path))
    backup_mod.restore_backup(user_dir, name, wipe=False)
    for sidecar in SIDECARS:
        assert not os.path.lexists(os.path.join(user_dir, sidecar)), sidecar
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'


def test_overwrite_without_a_database_in_the_archive_keeps_the_companions(user_dir, fmt):
    _put_sidecars(user_dir)
    before = {s: installation.file_digest(os.path.join(user_dir, s)) for s in SIDECARS}
    name = fmt.build(user_dir, {'notiz.txt': 'neu'})
    backup_mod.restore_backup(user_dir, name, wipe=False)
    for sidecar in SIDECARS:
        assert installation.file_digest(os.path.join(user_dir, sidecar)) == before[sidecar]


@WIPE
def test_companions_in_the_archive_are_never_restored(user_dir, fmt, tmp_path, wipe):
    files = standard_files(tmp_path)
    files.update({sidecar: b'\0' * 64 for sidecar in SIDECARS})
    name = fmt.build(user_dir, files)
    backup_mod.restore_backup(user_dir, name, wipe=wipe)
    for sidecar in SIDECARS:
        assert not os.path.lexists(os.path.join(user_dir, sidecar)), sidecar
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'


@WIPE
def test_a_wal_database_in_the_archive_leaves_no_wal_files_behind(user_dir, fmt, tmp_path, wipe):
    """Das nur lesende Öffnen zur Prüfung legt -wal/-shm neben der Datenbank an; zurückgespielt
    werden sie nicht."""
    files = standard_files(tmp_path)
    files['buch.db'] = db_bytes(tmp_path, 'wal.db', value='aus Sicherung', rows=50, wal=True)
    name = fmt.build(user_dir, files)
    backup_mod.restore_backup(user_dir, name, wipe=wipe)
    for sidecar in SIDECARS:                       # vor dem Öffnen prüfen: das legt sie wieder an
        assert not os.path.lexists(os.path.join(user_dir, sidecar)), sidecar
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'


# ── „wipe“ gelingt ───────────────────────────────────────────────────────────
def test_wipe_replaces_the_old_state_and_leaves_the_exceptions_alone(user_dir, fmt, tmp_path):
    for rel in ('users/u.txt', 'tests/t.txt', 'backup/Eigene Notiz.txt'):
        path = os.path.join(user_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('vorhanden ' + rel)
    # Nach der Sicherung entstanden: eine neue Datei, ein neuer Ordner, Begleitdateien
    with open(os.path.join(user_dir, 'neu.txt'), 'w', encoding='utf-8') as f:
        f.write('nach der Sicherung')
    os.makedirs(os.path.join(user_dir, 'neuer Ordner', 'tief'))
    _put_sidecars(user_dir)
    files = standard_files(tmp_path)
    name = fmt.build(user_dir, files, dirs=['leer', 'Belege/auch leer'])
    older = fmt.build(user_dir, {'notiz.txt': 'älter'}, name='20250101_Backup' + fmt.ext)

    def exceptions():
        return {path: state for path, state in installation.tree(user_dir).items()
                if path.split(os.sep)[0] in ('users', 'tests', 'backup')}

    kept = exceptions()
    backup_mod.restore_backup(user_dir, name, wipe=True)

    assert exceptions() == kept, 'backup/ (Archive), users/ und tests/ bleiben unangetastet'
    assert os.path.isfile(archive_path(user_dir, older))
    assert sorted(n for n in os.listdir(user_dir) if n not in ('backup', 'users', 'tests')) \
        == ['Belege', 'buch.db', 'leer', 'notiz.txt']
    assert os.listdir(os.path.join(user_dir, 'leer')) == []
    assert os.path.isdir(os.path.join(user_dir, 'Belege', 'auch leer'))
    assert read(os.path.join(user_dir, 'notiz.txt')) == files['notiz.txt']
    assert read(os.path.join(user_dir, 'Belege', 'beleg1.txt')) == files['Belege/beleg1.txt']
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'
    assert leftovers(user_dir) == []


@WIPE
def test_empty_directories_of_the_archive_are_created(user_dir, fmt, tmp_path, wipe):
    """Auch ein Ordner ohne Dateien gehört zur Sicherung (z. B. ein leeres Belege/ oder invoices/)."""
    name = fmt.build(user_dir, standard_files(tmp_path), dirs=['leer', 'Belege/auch leer', 'a/b/c'])
    backup_mod.restore_backup(user_dir, name, wipe=wipe)
    for rel in ('leer', 'Belege/auch leer', 'a/b/c'):
        path = os.path.join(user_dir, *rel.split('/'))
        assert os.path.isdir(path) and os.listdir(path) == [], rel
    assert leftovers(user_dir) == []


def test_restoring_twice_works(user_dir, fmt, tmp_path):
    name = fmt.build(user_dir, standard_files(tmp_path))
    backup_mod.restore_backup(user_dir, name, wipe=True)
    with open(os.path.join(user_dir, 'notiz.txt'), 'w', encoding='utf-8') as f:
        f.write('zwischendurch geändert')
    backup_mod.restore_backup(user_dir, name, wipe=False)
    assert read(os.path.join(user_dir, 'notiz.txt')) == 'Notiz aus Sicherung'
    assert leftovers(user_dir) == []


# ── Zurückrollen ─────────────────────────────────────────────────────────────
@pytest.fixture
def wipe_world(user_dir, zip_only, tmp_path):
    """Datenverzeichnis, Archiv und eine unberührte Vorlage davon. Jeder Lauf bekommt eine
    Kopie (``run``), weil das Wiederherstellen das Verzeichnis verändert."""
    name = zip_only.build(user_dir, standard_files(tmp_path))
    template = tmp_path / 'vorlage'
    shutil.copytree(user_dir, template)
    counter = [0]

    def run():
        counter[0] += 1
        folder = str(tmp_path / f'lauf{counter[0]}')
        shutil.copytree(template, folder)
        return folder

    return SimpleNamespace(name=name, template=str(template), run=run, tmp_path=tmp_path)


def _count_moves(world, monkeypatch):
    """Probelauf ohne Fehler: wie viele Verschiebungen braucht „wipe“?"""
    folder = world.run()
    with monkeypatch.context() as m:
        fault = MoveFault(folder).install(m)
        backup_mod.restore_backup(folder, world.name, wipe=True)
    return len(fault.calls)


@pytest.mark.parametrize('n', range(1, 9))
def test_failing_move_rolls_back_to_the_previous_state(wipe_world, monkeypatch, n):
    """Der n-te Verschiebevorgang scheitert – beim Wegschieben des alten Stands (die ersten
    Aufrufe) wie beim Hereinholen des neuen. Danach ist alles wie vorher und es liegt nichts
    herum."""
    total = _count_moves(wipe_world, monkeypatch)
    assert total >= 4, 'wipe verschiebt alten und neuen Stand'
    if n > total:
        pytest.skip(f'wipe braucht nur {total} Verschiebungen')
    folder = wipe_world.run()
    with monkeypatch.context() as m:
        fault = MoveFault(folder, fail_at=n).install(m)
        with pytest.raises(RuntimeError, match='vorherige Stand ist unverändert'):
            backup_mod.restore_backup(folder, wipe_world.name, wipe=True)
    assert fault.refused, 'der eingespielte Fehler ist nie aufgetreten'
    assert snapshot(folder) == snapshot(wipe_world.template)
    assert leftovers(folder) == []
    assert db_value(os.path.join(folder, 'buch.db')) == 'original'


@pytest.mark.parametrize('n', range(2, 9))
def test_failed_rollback_keeps_the_previous_state_in_the_replaced_folder(wipe_world, monkeypatch, n):
    """Scheitert auch das Zurücklegen, nennt die Meldung backup/replaced_… und der Ordner
    existiert: Jeder Eintrag des alten Stands liegt entweder wieder im Datenverzeichnis oder dort,
    byteweise unverändert."""
    total = _count_moves(wipe_world, monkeypatch)
    if n > total:
        pytest.skip(f'wipe braucht nur {total} Verschiebungen')
    folder = wipe_world.run()
    with monkeypatch.context() as m:
        MoveFault(folder, fail_at=n, onwards=True).install(m)
        with pytest.raises(RuntimeError) as raised:
            backup_mod.restore_backup(folder, wipe_world.name, wipe=True)
    found = re.search(r'backup/(replaced_\w+)', str(raised.value))
    assert found, str(raised.value)
    replaced = os.path.join(folder, 'backup', found.group(1))
    assert os.path.isdir(replaced)
    assert [entry for entry in leftovers(folder) if entry.startswith('restore_')] == []

    before = snapshot(wipe_world.template)
    now = snapshot(folder)
    held = installation.tree(replaced)
    assert not set(now) & set(held), 'ein Eintrag liegt an beiden Orten'
    assert {**held, **now} == before, 'vom alten Stand fehlt etwas oder es ist etwas Neues dabei'


# ── Reihenfolge: entpacken → prüfen → ersetzen ───────────────────────────────
@WIPE
def test_data_directory_is_untouched_while_extracting_and_checking(user_dir, fmt, tmp_path,
                                                                   monkeypatch, wipe):
    """Reihenfolge-Wächter: Solange entpackt und geprüft wird, ist das Datenverzeichnis noch
    unverändert; ersetzt wird erst danach (die alte Reihenfolge löschte zuerst)."""
    name = fmt.build(user_dir, standard_files(tmp_path))
    before = snapshot(user_dir)
    seen = []

    def guard(label, original):
        def guarded(*args, **kwargs):
            assert snapshot(user_dir) == before, f'{label}: Datenverzeichnis vor dem Aufruf verändert'
            result = original(*args, **kwargs)
            assert snapshot(user_dir) == before, f'{label}: Datenverzeichnis danach verändert'
            seen.append(label)
            return result
        return guarded

    def replacing(label, original):
        def replace(*args, **kwargs):
            assert seen == ['extract', 'check'], f'{label} vor Entpacken und Prüfen: {seen}'
            seen.append(label)
            return original(*args, **kwargs)
        return replace

    monkeypatch.setattr(backup_mod, '_extract', guard('extract', backup_mod._extract))
    monkeypatch.setattr(backup_mod, '_check_staging', guard('check', backup_mod._check_staging))
    monkeypatch.setattr(backup_mod, '_replace_all', replacing('replace', backup_mod._replace_all))
    monkeypatch.setattr(backup_mod, '_overlay', replacing('overlay', backup_mod._overlay))

    backup_mod.restore_backup(user_dir, name, wipe=wipe)
    assert seen == ['extract', 'check', 'replace' if wipe else 'overlay']
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'


@WIPE
def test_failing_check_never_reaches_the_replace_step(user_dir, zip_only, tmp_path, monkeypatch,
                                                      wipe):
    name = zip_only.build(user_dir, {'buch.db': damaged_db_bytes(tmp_path, 'zufallsbytes'),
                                     'notiz.txt': 'neu'})
    reached = []
    monkeypatch.setattr(backup_mod, '_replace_all', lambda *a, **k: reached.append('replace'))
    monkeypatch.setattr(backup_mod, '_overlay', lambda *a, **k: reached.append('overlay'))
    assert_rejected(user_dir, name, wipe, 'beschädigt')
    assert reached == []


# ── Namen und Verzeichnisse ──────────────────────────────────────────────────
@pytest.mark.parametrize('spelling', ['../fremd_Backup.zip', '..\\fremd_Backup.zip',
                                      'ABSOLUT', 'backup/fremd_Backup.zip'])
def test_archive_name_is_reduced_to_a_name_inside_backup(user_dir, zip_only, tmp_path, spelling):
    """Ein gültiges Archiv liegt außerhalb von backup/: Es lässt sich über keine Schreibweise
    des Namens erreichen. Nur der Basisname zählt, gesucht wird in backup/."""
    outside = os.path.join(user_dir, 'fremd_Backup.zip')
    with zipfile.ZipFile(outside, 'w') as zf:
        zf.writestr('buch.db', db_bytes(tmp_path))
        zf.writestr('notiz.txt', 'aus dem fremden Archiv')
    if spelling == 'ABSOLUT':
        spelling = outside
    before = snapshot(user_dir)
    with pytest.raises(FileNotFoundError):
        backup_mod.restore_backup(user_dir, spelling, wipe=False)
    assert snapshot(user_dir) == before
    assert leftovers(user_dir) == []


@pytest.mark.parametrize('wipe', [False, True], ids=['ueberschreiben', 'wipe'])
def test_restore_works_in_a_directory_with_special_characters(tmp_path, monkeypatch, wipe):
    """Die Datenbank wird über eine file:-Adresse geöffnet; Leerzeichen, Umlaute, #, ? und %
    im Pfad (z. B. der Projektordner) dürfen sie nicht verfälschen."""
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)
    folder = tmp_path / 'Büro Daten #1 100% ?x' / 'user'
    try:
        folder.mkdir(parents=True)
    except OSError as exc:                                    # z. B. „?“ in Windows-Pfaden
        pytest.skip(f'das Dateisystem erlaubt diesen Ordnernamen nicht ({exc})')
    write_db(folder / 'buch.db', value='original')
    (folder / 'notiz.txt').write_text('Notiz Version 1', encoding='utf-8')
    name = Archives('zip', tmp_path).build(str(folder), standard_files(tmp_path))
    backup_mod.restore_backup(str(folder), name, wipe=wipe)
    assert db_value(folder / 'buch.db') == 'aus Sicherung'
    assert leftovers(str(folder)) == []


# ── Handler handle_backup_restore ────────────────────────────────────────────
@pytest.fixture
def as_user_dir(user_dir, monkeypatch):
    monkeypatch.setattr(handlers.userctx, 'user_data_dir', lambda: user_dir)
    return user_dir


def _location(result):
    status, location = result
    parts = urllib.parse.urlsplit(location)
    return status, parts.path, urllib.parse.parse_qs(parts.query)


@WIPE
def test_handler_reports_a_damaged_archive_as_an_error_redirect(as_user_dir, zip_only, tmp_path, wipe):
    name = STEM + '.zip'
    write_defective_zip('zufallsbytes', archive_path(as_user_dir, name), tmp_path)
    before = snapshot(as_user_dir)
    status, path, query = _location(handlers.handle_backup_restore(
        None, {'archive': [name], 'mode': ['wipe' if wipe else 'overwrite']}))
    assert (status, path) == (303, '/miscellaneous')
    assert query['restore'] == ['error']
    assert 'beschädigt' in query['msg'][0]
    assert snapshot(as_user_dir) == before
    assert leftovers(as_user_dir) == []


def test_handler_reports_a_newer_database_as_an_error_redirect(as_user_dir, zip_only, tmp_path):
    newer = version.SCHEMA_VERSION + 1
    name = zip_only.build(as_user_dir, {'buch.db': db_bytes(tmp_path, user_version=newer),
                                        'notiz.txt': 'neu'})
    before = snapshot(as_user_dir)
    status, path, query = _location(handlers.handle_backup_restore(
        None, {'archive': [name], 'mode': ['wipe']}))
    assert (status, path, query['restore']) == (303, '/miscellaneous', ['error'])
    assert version.describe_schema_version(newer) in query['msg'][0]
    assert snapshot(as_user_dir) == before


@pytest.mark.parametrize('mode, wiped', [('wipe', True), ('overwrite', False), ('', False),
                                         ('unbekannt', False)])
def test_handler_success_redirects_with_the_file_name(as_user_dir, zip_only, tmp_path, mode, wiped):
    name = zip_only.build(as_user_dir, standard_files(tmp_path))
    with open(os.path.join(as_user_dir, 'neu.txt'), 'w', encoding='utf-8') as f:
        f.write('nach der Sicherung')
    status, path, query = _location(handlers.handle_backup_restore(
        None, {'archive': [name], 'mode': [mode]}))
    assert (status, path) == (303, '/miscellaneous')
    assert query == {'restore': ['ok'], 'file': [name]}
    assert os.path.exists(os.path.join(as_user_dir, 'neu.txt')) is not wiped
    assert db_value(os.path.join(as_user_dir, 'buch.db')) == 'aus Sicherung'


def test_handler_without_an_archive_name_is_an_error(as_user_dir):
    before = snapshot(as_user_dir)
    status, path, query = _location(handlers.handle_backup_restore(None, {}))
    assert (status, path, query['restore']) == (303, '/miscellaneous', ['error'])
    assert 'Kein Archiv' in query['msg'][0]
    assert snapshot(as_user_dir) == before


def test_handler_unknown_archive_is_an_error_and_changes_nothing(as_user_dir):
    before = snapshot(as_user_dir)
    for spelling in ('gibtsnicht.7zip', '../../etc/passwd', '..\\..\\boese.zip'):
        status, path, query = _location(handlers.handle_backup_restore(
            None, {'archive': [spelling], 'mode': ['wipe']}))
        assert (status, path, query['restore']) == (303, '/miscellaneous', ['error'])
    assert snapshot(as_user_dir) == before


# ── Über den echten Server ───────────────────────────────────────────────────
def _admin_dir():
    return os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN)


def test_http_restore_error_redirects_and_keeps_the_data(server_port, auto_login, monkeypatch,
                                                         tmp_path):
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)
    user = _admin_dir()
    auto_login.db.insert_article(name='Probeartikel', unit_price=10, tax_rate=19)
    name = STEM + '.zip'
    write_defective_zip('abgeschnitten', archive_path(user, name), tmp_path)
    before = snapshot(user)
    resp = post(server_port, '/backup/restore', csrf=auto_login.csrf,
                form={'archive': name, 'mode': 'wipe'})
    assert resp.status == 303
    parts = urllib.parse.urlsplit(resp.headers['Location'])
    query = urllib.parse.parse_qs(parts.query)
    assert parts.path == '/miscellaneous' and query['restore'] == ['error']
    assert 'beschädigt' in query['msg'][0]
    assert snapshot(user) == before
    assert leftovers(user) == []
    assert len(installation.open_db(installation.ADMIN).fetch_articles()) == 1


def test_http_restore_wipe_brings_back_the_saved_state(server_port, auto_login, monkeypatch):
    monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)
    user = _admin_dir()
    auto_login.db.insert_article(name='Probeartikel', unit_price=10, tax_rate=19)
    with open(os.path.join(user, 'notiz.txt'), 'w', encoding='utf-8') as f:
        f.write('Notiz zur Sicherung')
    archive, _ = backup_mod.create_backup(user, auto_login.db.db_name, 'all')
    auto_login.db.insert_article(name='Zweiter Artikel', unit_price=20, tax_rate=19)
    with open(os.path.join(user, 'neu.txt'), 'w', encoding='utf-8') as f:
        f.write('nach der Sicherung')
    resp = post(server_port, '/backup/restore', csrf=auto_login.csrf,
                form={'archive': os.path.basename(archive), 'mode': 'wipe'})
    assert resp.status == 303
    parts = urllib.parse.urlsplit(resp.headers['Location'])
    assert parts.path == '/miscellaneous'
    assert urllib.parse.parse_qs(parts.query) == {'restore': ['ok'],
                                                  'file': [os.path.basename(archive)]}
    assert not os.path.exists(os.path.join(user, 'neu.txt'))
    assert [a[1] for a in installation.open_db(installation.ADMIN).fetch_articles()] == ['Probeartikel']
    assert leftovers(user) == []
