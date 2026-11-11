# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Wiederherstellen – Nachbesserungen zu 2.0.3 (server/backup.py).

- anderes Dateisystem: Scheitert os.rename/os.replace mit EXDEV (backup/ auf einem eigenen
  Volume, einzelne Pfade), wird kopiert statt verschoben – Ergebnis wie ohne Fehler, auch beim
  Zurückrollen
- Überschreiben ersetzt die Datenbank als LETZTE Datei: Scheitert eine andere, bleibt die alte
- „vorher löschen“ mit einer Nur-Datenbank-Sicherung wird abgelehnt, wenn sonst noch Daten da sind
- 7-Zip: Vor dem Entpacken wird aufgelistet (7z l -slt -t7z); ein .7zip, das keins ist, sowie
  Einträge mit Link-Attribut, „..“ oder absolutem Pfad werden abgelehnt, bevor entpackt wird
- Sperre: Ein zweiter Aufruf für dasselbe Datenverzeichnis wird sofort abgelehnt („läuft bereits“)
- Aufräumen: Alte backup/restore_*-Ordner verschwinden zu Beginn, backup/replaced_* nie

Hilfen in backup_helpers.py. Alle Inhalte sind synthetische Testdaten.
"""
import errno
import io
import os
import shutil
import tarfile
import urllib.parse
import zipfile

import pytest

from server import backup as backup_mod
from server import handlers
from tests import installation
from tests.backup_helpers import (
    REAL_RUN, SIDECARS, STEM, Archives, Blocker, Call, MoveFault, WriteFault, archive_path,
    db_bytes, db_value, leftovers, make_archives, make_user_dir, rename_in_sevenzip_header,
    require_sevenzip, sevenzip_add, snapshot, standard_files, top_level, write_db)

WIPE = pytest.mark.parametrize('wipe', [False, True], ids=['ueberschreiben', 'wipe'])


@pytest.fixture
def user_dir(tmp_path):
    return make_user_dir(tmp_path)


@pytest.fixture(params=['zip', '7z'])
def fmt(request, monkeypatch, tmp_path):
    return make_archives(request.param, monkeypatch, tmp_path)


@pytest.fixture
def zip_only(monkeypatch, tmp_path):
    return make_archives('zip', monkeypatch, tmp_path)


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def clone(user_dir, tmp_path, tag):
    folder = str(tmp_path / tag)
    shutil.copytree(user_dir, folder)
    return folder


def assert_rejected(user_dir, name, wipe, match=None):
    before = snapshot(user_dir)
    with pytest.raises(RuntimeError, match=match) as raised:
        backup_mod.restore_backup(user_dir, name, wipe=wipe)
    assert snapshot(user_dir) == before, 'das Datenverzeichnis wurde verändert'
    assert leftovers(user_dir) == []
    assert os.path.isfile(archive_path(user_dir, name))
    return str(raised.value)


# ── Anderes Dateisystem ──────────────────────────────────────────────────────
# Jeder Fall simuliert den Wechsel anders: backup/ als eigenes Volume (jede Verschiebung über
# die Grenze scheitert), jede Quelle scheitert einmal, nur ein einzelner Eintrag scheitert.
CROSSINGS = {
    'backup-als-eigenes-volume': dict(),
    'jede-quelle-einmal': dict(once=True),
    'einzelne-datei': dict(names={'notiz.txt'}),
    'einzelne-datenbank': dict(names={'buch.db'}),
}


@pytest.mark.parametrize('crossing', list(CROSSINGS))
@WIPE
def test_restore_copies_instead_of_moving_when_the_filesystem_differs(user_dir, fmt, tmp_path,
                                                                      monkeypatch, wipe, crossing):
    name = fmt.build(user_dir, standard_files(tmp_path), dirs=['leer'])
    control = clone(user_dir, tmp_path, 'kontrolle')
    backup_mod.restore_backup(control, name, wipe=wipe)         # Maßstab: dasselbe ohne Fehler

    with monkeypatch.context() as m:
        fault = MoveFault(tmp_path, boundary=os.path.join(user_dir, 'backup'),
                          **CROSSINGS[crossing]).install(m)
        backup_mod.restore_backup(user_dir, name, wipe=wipe)

    assert {code for *_, code in fault.refused} == {errno.EXDEV}, \
        'der simulierte Wechsel des Dateisystems kam nie vor'
    assert snapshot(user_dir) == snapshot(control)
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'
    assert leftovers(user_dir) == []


def test_wipe_copies_a_directory_when_only_that_directory_is_on_another_filesystem(
        user_dir, zip_only, tmp_path, monkeypatch):
    name = zip_only.build(user_dir, standard_files(tmp_path), dirs=['leer'])
    control = clone(user_dir, tmp_path, 'kontrolle')
    backup_mod.restore_backup(control, name, wipe=True)
    with monkeypatch.context() as m:
        fault = MoveFault(tmp_path, boundary=os.path.join(user_dir, 'backup'),
                          names={'Belege', 'leer'}).install(m)
        backup_mod.restore_backup(user_dir, name, wipe=True)
    assert {code for *_, code in fault.refused} == {errno.EXDEV}
    assert snapshot(user_dir) == snapshot(control)
    assert leftovers(user_dir) == []


@pytest.mark.parametrize('n', range(1, 9))
def test_rollback_also_works_when_the_filesystem_differs(user_dir, zip_only, tmp_path, monkeypatch, n):
    """backup/ auf einem anderen Dateisystem UND ein Verschiebevorgang scheitert: Das Zurücklegen
    des alten Stands muss dann ebenfalls kopieren können."""
    name = zip_only.build(user_dir, standard_files(tmp_path))
    template = clone(user_dir, tmp_path, 'vorlage')

    probe = clone(template, tmp_path, 'probe')
    with monkeypatch.context() as m:
        counter = MoveFault(probe, boundary=os.path.join(probe, 'backup')).install(m)
        backup_mod.restore_backup(probe, name, wipe=True)
    if n > len(counter.calls):
        pytest.skip(f'wipe braucht nur {len(counter.calls)} Verschiebungen')

    folder = clone(template, tmp_path, 'lauf')
    with monkeypatch.context() as m:
        fault = MoveFault(folder, fail_at=n, boundary=os.path.join(folder, 'backup')).install(m)
        with pytest.raises(RuntimeError, match='vorherige Stand ist unverändert'):
            backup_mod.restore_backup(folder, name, wipe=True)
    codes = {code for *_, code in fault.refused}
    assert errno.EIO in codes, 'der eingespielte Fehler ist nie aufgetreten'
    assert snapshot(folder) == snapshot(template)
    assert leftovers(folder) == []


def test_wipe_works_when_a_subdirectory_is_a_mount_point(user_dir, zip_only, tmp_path, monkeypatch):
    """Ein einzeln eingebundenes Volume (Docker) lässt sich weder verschieben (EBUSY) noch
    entfernen: Sein Inhalt wird kopiert bzw. gelöscht, das leere Verzeichnis bleibt stehen."""
    name = zip_only.build(user_dir, standard_files(tmp_path))
    control = clone(user_dir, tmp_path, 'kontrolle')
    backup_mod.restore_backup(control, name, wipe=True)
    mount = os.path.realpath(os.path.join(user_dir, 'Belege'))
    refused = []

    def busy(real):
        def guarded(*args, **kwargs):
            paths = [os.path.realpath(a) for a in args[:2] if isinstance(a, (str, os.PathLike))]
            if mount in paths and not kwargs:
                refused.append(real.__name__)
                raise OSError(errno.EBUSY, 'Device or resource busy', mount)
            return real(*args, **kwargs)
        return guarded
    with monkeypatch.context() as m:
        m.setattr(os, 'rename', busy(os.rename))
        m.setattr(os, 'rmdir', busy(os.rmdir))
        backup_mod.restore_backup(user_dir, name, wipe=True)
    assert 'rename' in refused and 'rmdir' in refused, refused
    assert snapshot(user_dir) == snapshot(control)
    assert leftovers(user_dir) == []


@pytest.mark.parametrize('phase', ['alter-stand-wegkopieren', 'neuen-stand-hereinkopieren'])
def test_a_directory_copy_that_fails_leaves_no_half_copy_and_rolls_back(
        user_dir, zip_only, tmp_path, monkeypatch, phase):
    """Über eine Dateisystemgrenze wird ein Verzeichnis kopiert – nicht atomar wie ein rename.
    Bricht das Kopieren ab, bleibt von dem Ziel nichts Halbes zurück, und der alte Stand kommt
    wieder an seinen Platz."""
    files = standard_files(tmp_path)
    files['Belege/neu.txt'] = 'nur im Archiv'
    name = zip_only.build(user_dir, files)
    before = snapshot(user_dir)
    failing = 'beleg1.txt' if phase == 'alter-stand-wegkopieren' else 'neu.txt'
    real_copyfile = shutil.copyfile
    refused = []

    def copyfile(src, dst, *args, **kwargs):
        if os.path.basename(dst) == failing:
            refused.append(dst)
            raise OSError(errno.EIO, 'Eingabe-/Ausgabefehler (Test)', dst)
        return real_copyfile(src, dst, *args, **kwargs)
    with monkeypatch.context() as m:
        MoveFault(tmp_path, boundary=os.path.join(user_dir, 'backup')).install(m)
        m.setattr(shutil, 'copyfile', copyfile)
        with pytest.raises(RuntimeError, match='vorherige Stand ist unverändert'):
            backup_mod.restore_backup(user_dir, name, wipe=True)
    assert refused, 'das Kopieren wurde nie versucht'
    assert snapshot(user_dir) == before
    assert leftovers(user_dir) == []


@WIPE
def test_restore_works_when_backup_is_a_link_to_another_directory(user_dir, zip_only, tmp_path, wipe):
    elsewhere = tmp_path / 'woanders'
    elsewhere.mkdir()
    os.symlink(elsewhere, os.path.join(user_dir, 'backup'))
    name = zip_only.build(user_dir, standard_files(tmp_path))
    backup_mod.restore_backup(user_dir, name, wipe=wipe)
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'
    assert os.path.islink(os.path.join(user_dir, 'backup'))
    assert [n for n in os.listdir(elsewhere) if n.startswith(('restore_', 'replaced_'))] == []
    assert os.path.isfile(elsewhere / name)


# ── Die Datenbank kommt beim Überschreiben zuletzt ───────────────────────────
OVERWRITTEN = ['aaa.txt', 'notiz.txt', 'zzz.txt', 'Belege/beleg1.txt', 'Belege/zzz.txt']


def _overwrite_world(user_dir, tmp_path, fmt):
    """Dateien, die beim Überschreiben einzeln ersetzt werden (alle schon vorhanden), plus
    Archiv mit neuem Inhalt."""
    for rel in ('aaa.txt', 'zzz.txt', 'Belege/zzz.txt'):
        with open(os.path.join(user_dir, *rel.split('/')), 'w', encoding='utf-8') as f:
            f.write('alt')
    files = standard_files(tmp_path)
    files.update({'aaa.txt': 'neu', 'zzz.txt': 'neu', 'Belege/zzz.txt': 'neu'})
    return files, fmt.build(user_dir, files)


@pytest.mark.parametrize('failing', OVERWRITTEN)
def test_a_failing_file_leaves_the_old_database_in_place(user_dir, fmt, tmp_path, monkeypatch,
                                                         failing):
    """Die Datenbank ist der Schlusspunkt: Scheitert irgendeine andere Datei, ist buch.db noch
    die alte (nicht die neue Datenbank mit alten Dateien)."""
    files, name = _overwrite_world(user_dir, tmp_path, fmt)
    database = os.path.join(user_dir, 'buch.db')
    before = installation.file_digest(database)
    with monkeypatch.context() as m:
        fault = WriteFault(user_dir, fail={failing}).install(m)
        with pytest.raises((RuntimeError, OSError)):
            backup_mod.restore_backup(user_dir, name, wipe=False)
    assert failing in fault.refused, 'das Ablegen dieser Datei wurde nie versucht'
    assert installation.file_digest(database) == before
    assert db_value(database) == 'original'
    assert leftovers(user_dir) == []


def assert_database_arrives_last(order, files):
    """Jede Datei der Sicherung kam an, und die Datenbank zuletzt (Hilfsdateien, über die eine
    Kopie läuft, dürfen dazwischen auftauchen)."""
    last = {rel: position for position, rel in enumerate(order)}
    assert set(files) <= set(last), order
    for rel in files:
        if rel != 'buch.db':
            assert last[rel] < last['buch.db'], f'{rel} kam nach der Datenbank: {order}'


def test_the_database_arrives_after_all_other_files(user_dir, fmt, tmp_path, monkeypatch):
    files, name = _overwrite_world(user_dir, tmp_path, fmt)
    with monkeypatch.context() as m:
        fault = WriteFault(user_dir).install(m)
        backup_mod.restore_backup(user_dir, name, wipe=False)
    assert fault.order, 'keine Datei kam über os.replace/os.rename/shutil an'
    assert fault.order[-1] == 'buch.db', fault.order
    assert_database_arrives_last(fault.order, files)


def test_the_database_arrives_last_across_filesystems_too(user_dir, fmt, tmp_path, monkeypatch):
    files, name = _overwrite_world(user_dir, tmp_path, fmt)
    with monkeypatch.context() as m:
        MoveFault(tmp_path, boundary=os.path.join(user_dir, 'backup')).install(m)
        fault = WriteFault(user_dir).install(m)
        backup_mod.restore_backup(user_dir, name, wipe=False)
    assert fault.order, 'keine Datei kam über os.replace/os.rename/shutil an'
    assert_database_arrives_last(fault.order, files)
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'


@pytest.mark.parametrize('failing', ['aaa.txt', 'zzz.txt', 'Belege/zzz.txt'])
def test_a_failing_file_across_filesystems_leaves_the_old_database_in_place(
        user_dir, fmt, tmp_path, monkeypatch, failing):
    files, name = _overwrite_world(user_dir, tmp_path, fmt)
    database = os.path.join(user_dir, 'buch.db')
    before = installation.file_digest(database)
    with monkeypatch.context() as m:
        MoveFault(tmp_path, boundary=os.path.join(user_dir, 'backup')).install(m)
        fault = WriteFault(user_dir, fail={failing}).install(m)
        with pytest.raises((RuntimeError, OSError)):
            backup_mod.restore_backup(user_dir, name, wipe=False)
    assert failing in fault.refused
    assert installation.file_digest(database) == before


# ── „vorher löschen“ mit einer Nur-Datenbank-Sicherung ───────────────────────
def _database_only_archive(how, user_dir, fmt, tmp_path):
    if how == 'create_backup-db':
        archive, _ = backup_mod.create_backup(user_dir, os.path.join(user_dir, 'buch.db'), 'db')
        assert archive.endswith(fmt.ext)
        return os.path.basename(archive)
    files = {'buch.db': db_bytes(tmp_path, value='aus Sicherung')}
    if how == 'mit-ausgenommenem':
        files.update({'backup/aelter.zip': 'x', 'users/u.txt': 'x', 'tests/t.txt': 'x',
                      'buch.db-wal': b'\0' * 32})
    return fmt.build(user_dir, files)


DATABASE_ONLY = ['create_backup-db', 'nur-buch.db', 'mit-ausgenommenem']


@pytest.mark.parametrize('how', DATABASE_ONLY)
@pytest.mark.parametrize('remaining', ['datei', 'ordner', 'beides'])
def test_wipe_with_a_database_only_archive_is_rejected_when_other_data_exists(
        user_dir, fmt, tmp_path, how, remaining):
    """„Vorher löschen“ mit einer Sicherung nur der Datenbank (Umfang „Datenbank“) würde Belege
    und Dateien löschen, die in der Sicherung nie waren."""
    if remaining == 'datei':
        shutil.rmtree(os.path.join(user_dir, 'Belege'))
    elif remaining == 'ordner':
        os.remove(os.path.join(user_dir, 'notiz.txt'))
    name = _database_only_archive(how, user_dir, fmt, tmp_path)
    message = assert_rejected(user_dir, name, True, 'enthält nur die Datenbank')
    assert 'keine Datenbank' not in message


@pytest.mark.parametrize('how', DATABASE_ONLY)
def test_wipe_with_a_database_only_archive_is_accepted_when_nothing_else_is_there(
        user_dir, fmt, tmp_path, how):
    """Im Datenverzeichnis liegen nur buch.db, ihre Begleitdateien und die ausgenommenen
    Ordner (backup/ mit dem Archiv, users/, tests/): Dann geht nichts verloren."""
    name = _database_only_archive(how, user_dir, fmt, tmp_path)
    shutil.rmtree(os.path.join(user_dir, 'Belege'))
    os.remove(os.path.join(user_dir, 'notiz.txt'))
    for sidecar in SIDECARS:
        with open(os.path.join(user_dir, sidecar), 'wb') as f:
            f.write(b'\0' * 64)
    for rel in ('users/u.txt', 'tests/t.txt'):
        os.makedirs(os.path.join(user_dir, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(user_dir, rel), 'w', encoding='utf-8') as f:
            f.write('vorhanden')
    database = os.path.join(user_dir, 'buch.db')
    os.remove(database)
    write_db(database, value='geändert')                      # der Stand nach der Sicherung
    kept = {p: s for p, s in installation.tree(user_dir).items()
            if p.split(os.sep)[0] in ('users', 'tests')}

    backup_mod.restore_backup(user_dir, name, wipe=True)

    assert top_level(user_dir) == ['buch.db']
    assert db_value(database) == ('original' if how == 'create_backup-db' else 'aus Sicherung')
    assert {p: s for p, s in installation.tree(user_dir).items()
            if p.split(os.sep)[0] in ('users', 'tests')} == kept
    assert leftovers(user_dir) == []


@pytest.mark.parametrize('how', DATABASE_ONLY)
def test_overwrite_with_a_database_only_archive_keeps_the_other_data(user_dir, fmt, tmp_path, how):
    name = _database_only_archive(how, user_dir, fmt, tmp_path)
    before = {p: s for p, s in snapshot(user_dir).items() if p != 'buch.db'}
    backup_mod.restore_backup(user_dir, name, wipe=False)
    assert {p: s for p, s in snapshot(user_dir).items() if p != 'buch.db'} == before
    assert leftovers(user_dir) == []


# ── 7-Zip: auflisten, bevor entpackt wird ────────────────────────────────────
@pytest.fixture
def sevenzip_calls(monkeypatch):
    """Alle Aufrufe von subprocess.run in backup.py (Kommandozeilen)."""
    calls = []

    def recording(command, *args, **kwargs):
        calls.append(list(command))
        return REAL_RUN(command, *args, **kwargs)
    monkeypatch.setattr(backup_mod.subprocess, 'run', recording)
    return calls


def subcommands(calls):
    return [command[1] for command in calls]


@pytest.fixture
def sevenzip_archives(tmp_path):
    return Archives('7z', tmp_path, require_sevenzip())


@WIPE
def test_a_sevenzip_archive_is_listed_before_it_is_extracted(user_dir, sevenzip_archives, tmp_path,
                                                             sevenzip_calls, wipe):
    name = sevenzip_archives.build(user_dir, standard_files(tmp_path))
    backup_mod.restore_backup(user_dir, name, wipe=wipe)
    assert subcommands(sevenzip_calls) == ['l', 'x']
    listing = sevenzip_calls[0]
    assert '-slt' in listing and '-t7z' in listing, 'aufgelistet wird technisch und als 7z-Format'
    assert db_value(os.path.join(user_dir, 'buch.db')) == 'aus Sicherung'


def test_a_zip_archive_does_not_involve_sevenzip(user_dir, zip_only, tmp_path, sevenzip_calls):
    name = zip_only.build(user_dir, standard_files(tmp_path))
    backup_mod.restore_backup(user_dir, name, wipe=False)
    assert sevenzip_calls == []


def _disguised(kind, path):
    if kind == 'zip':
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('buch.db', b'x')
            zf.writestr('notiz.txt', 'neu')
    else:
        with tarfile.open(path, 'w') as tf:
            for member, data in (('buch.db', b'x'), ('notiz.txt', b'neu')):
                info = tarfile.TarInfo(member)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize('kind', ['zip', 'tar'])
@pytest.mark.parametrize('ext', ['.7zip', '.7z'])
@WIPE
def test_a_7zip_file_that_is_really_a_zip_or_tar_is_rejected_before_extracting(
        user_dir, tmp_path, sevenzip_calls, kind, ext, wipe):
    require_sevenzip()
    name = STEM + ext
    _disguised(kind, archive_path(user_dir, name))
    message = assert_rejected(user_dir, name, wipe, '7-Zip')
    assert 'x' not in subcommands(sevenzip_calls), 'es wurde trotzdem entpackt'
    assert subcommands(sevenzip_calls)[:1] == ['l']
    assert message


def _links(user_dir, archives, tmp_path):
    return archives.build(user_dir, standard_files(tmp_path), links={'Belege/verweis': 'beleg1.txt'})


def _absolute_link(user_dir, archives, tmp_path):
    return archives.build(user_dir, standard_files(tmp_path), links={'verweis': '/etc'})


def _absolute_path(user_dir, archives, tmp_path):
    source = tmp_path / 'absolut'
    source.mkdir()
    (source / 'evil.txt').write_text('böse', encoding='utf-8')
    (source / 'notiz.txt').write_text('neu', encoding='utf-8')
    name = STEM + '.7zip'
    sevenzip_add(archives.sevenzip, archive_path(user_dir, name), source, '-spf',
                 patterns=(str(source / 'evil.txt'), str(source / 'notiz.txt')))
    return name


def _parent_path(user_dir, archives, tmp_path):
    source = tmp_path / 'ueber'
    (source / 'ab').mkdir(parents=True)
    (source / 'ab' / 'evil.txt').write_text('böse', encoding='utf-8')
    (source / 'notiz.txt').write_text('neu', encoding='utf-8')
    name = STEM + '.7zip'
    path = archive_path(user_dir, name)
    sevenzip_add(archives.sevenzip, path, source, '-mhc=off')      # Kopf unkomprimiert
    rename_in_sevenzip_header(path, 'ab/evil.txt', '../evil.txt')
    return name


UNSAFE_ENTRIES = {
    'relativer-link': _links,
    'absoluter-link': _absolute_link,
    'absoluter-pfad': _absolute_path,
    'pfad-mit-..': _parent_path,
}


@pytest.mark.parametrize('entry', list(UNSAFE_ENTRIES))
@WIPE
def test_unsafe_sevenzip_entries_are_rejected_before_extracting(user_dir, sevenzip_archives, tmp_path,
                                                               sevenzip_calls, entry, wipe):
    """Links, „..“ und absolute Pfade werden schon in der Liste erkannt – nichts davon wird
    entpackt, auch nicht in ein Zwischenverzeichnis."""
    name = UNSAFE_ENTRIES[entry](user_dir, sevenzip_archives, tmp_path)
    outside = installation.tree(tmp_path)
    assert_rejected(user_dir, name, wipe)
    assert subcommands(sevenzip_calls)[:1] == ['l']
    assert 'x' not in subcommands(sevenzip_calls), 'es wurde trotzdem entpackt'
    assert installation.tree(tmp_path) == outside, 'irgendwo ist etwas entstanden'


def test_the_crafted_parent_path_archive_really_lists_dot_dot(user_dir, sevenzip_archives, tmp_path):
    """Selbstprüfung des Aufbaus: Das Archiv aus _parent_path trägt „../evil.txt“ im Kopf."""
    name = _parent_path(user_dir, sevenzip_archives, tmp_path)
    result = REAL_RUN([sevenzip_archives.sevenzip, 'l', '-slt', '-ba', '-t7z',
                       archive_path(user_dir, name)], capture_output=True)
    assert b'Path = ../evil.txt' in result.stdout


UNUSUAL_NAMES = ['Belege/Scan..2026.pdf', '..versteckt.txt', 'Belege/Büro Rechnung (1) = neu.txt',
                 'Belege/[eckig] {geschweift}.txt', 'Belege/-minus.txt', 'Belege/mit äöüß.txt',
                 'Belege/a..b/c..d.txt', 'Belege/ ..mit Leerzeichen.txt']


@WIPE
def test_unusual_but_harmless_names_are_not_mistaken_for_unsafe_ones(user_dir, fmt, tmp_path, wipe):
    """Nur ein Pfadbestandteil, der genau „..“ ist, ist gefährlich – „Scan..pdf“, „..versteckt“
    oder ein „ = “ im Namen (7-Zip schreibt „Path = <Name>“) nicht."""
    files = standard_files(tmp_path)
    files.update({name: f'Inhalt {name}' for name in UNUSUAL_NAMES})
    name = fmt.build(user_dir, files)
    backup_mod.restore_backup(user_dir, name, wipe=wipe)
    for rel in UNUSUAL_NAMES:
        assert read(os.path.join(user_dir, *rel.split('/'))) == f'Inhalt {rel}', rel
    assert leftovers(user_dir) == []


# ── Sperre ───────────────────────────────────────────────────────────────────
def staging_dirs(user_dir):
    return [n for n in leftovers(user_dir) if n.startswith('restore_')]


def _other_world(tmp_path, tag):
    """Zweites Datenverzeichnis mit eigenem Archiv."""
    folder = tmp_path / tag
    folder.mkdir()
    sub = folder / 'user'
    sub.mkdir()
    write_db(sub / 'buch.db', value='original')
    (sub / 'notiz.txt').write_text('Notiz Version 1', encoding='utf-8')
    archives = Archives('zip', tmp_path)
    name = archives.build(str(sub), standard_files(tmp_path))
    return str(sub), name


def _database(user_dir):
    return os.path.join(user_dir, 'buch.db')


def _start_blocked_restore(user_dir, name, monkeypatch, wipe=False):
    blocker = Blocker()
    monkeypatch.setattr(backup_mod, '_extract', blocker.wrap(backup_mod._extract))
    first = Call(backup_mod.restore_backup, user_dir, name, wipe=wipe)
    first.start()
    assert blocker.entered.wait(10), 'die erste Wiederherstellung kam nie in _extract an'
    return blocker, first


def _start_blocked_backup(user_dir, monkeypatch):
    blocker = Blocker()
    monkeypatch.setattr(backup_mod, '_snapshot_db', blocker.wrap(backup_mod._snapshot_db))
    first = Call(backup_mod.create_backup, user_dir, _database(user_dir), 'all')
    first.start()
    assert blocker.entered.wait(10), 'die erste Sicherung kam nie in _snapshot_db an'
    return blocker, first


def _finish(blocker, first):
    blocker.proceed.set()
    first.join(30)
    assert not first.is_alive(), 'der angehaltene Aufruf wurde nicht fertig'
    return first


def _assert_refused(call):
    call.join(5)
    assert not call.is_alive(), 'der zweite Aufruf wartet, statt sofort abzulehnen'
    assert isinstance(call.error, RuntimeError), call.error
    assert 'läuft bereits' in str(call.error)


@pytest.mark.parametrize('second_wipe', [False, True], ids=['zweite-ueberschreiben', 'zweite-wipe'])
def test_a_second_restore_for_the_same_directory_is_refused_immediately(
        user_dir, zip_only, tmp_path, monkeypatch, second_wipe):
    name = zip_only.build(user_dir, standard_files(tmp_path))
    blocker, first = _start_blocked_restore(user_dir, name, monkeypatch)
    try:
        running = staging_dirs(user_dir)
        assert len(running) == 1
        second = Call(backup_mod.restore_backup, user_dir, name, wipe=second_wipe)
        second.start()
        _assert_refused(second)
        # Der abgelehnte Aufruf räumt nichts weg, was dem laufenden gehört
        assert staging_dirs(user_dir) == running
    finally:
        _finish(blocker, first)
    assert first.error is None
    assert db_value(_database(user_dir)) == 'aus Sicherung'
    assert leftovers(user_dir) == []


def test_a_backup_is_refused_while_a_restore_runs(user_dir, zip_only, tmp_path, monkeypatch):
    name = zip_only.build(user_dir, standard_files(tmp_path))
    blocker, first = _start_blocked_restore(user_dir, name, monkeypatch)
    try:
        second = Call(backup_mod.create_backup, user_dir, _database(user_dir), 'all')
        second.start()
        _assert_refused(second)
    finally:
        _finish(blocker, first)
    assert first.error is None


@pytest.mark.parametrize('wipe', [False, True], ids=['ueberschreiben', 'wipe'])
def test_a_restore_is_refused_while_a_backup_runs(user_dir, zip_only, tmp_path, monkeypatch, wipe):
    name = zip_only.build(user_dir, standard_files(tmp_path))
    before = snapshot(user_dir)
    blocker, first = _start_blocked_backup(user_dir, monkeypatch)
    try:
        second = Call(backup_mod.restore_backup, user_dir, name, wipe=wipe)
        second.start()
        _assert_refused(second)
        assert snapshot(user_dir) == before
    finally:
        _finish(blocker, first)
    assert first.error is None and os.path.isfile(first.value[0])


def test_a_second_backup_is_refused_while_a_backup_runs(user_dir, zip_only, monkeypatch):
    blocker, first = _start_blocked_backup(user_dir, monkeypatch)
    try:
        second = Call(backup_mod.create_backup, user_dir, _database(user_dir), 'db')
        second.start()
        _assert_refused(second)
    finally:
        _finish(blocker, first)
    assert first.error is None
    assert len(backup_mod.list_backups(user_dir)) == 1


def test_the_lock_is_free_again_after_an_error(user_dir, zip_only, tmp_path, monkeypatch):
    broken = STEM + '_defekt.zip'
    with open(archive_path(user_dir, broken), 'wb') as f:
        f.write(b'kein Archiv')
    with pytest.raises(RuntimeError, match='beschädigt'):
        backup_mod.restore_backup(user_dir, broken, wipe=True)
    with pytest.raises(FileNotFoundError):
        backup_mod.restore_backup(user_dir, 'gibtsnicht.zip')
    name = zip_only.build(user_dir, standard_files(tmp_path))
    backup_mod.restore_backup(user_dir, name, wipe=True)               # nicht „läuft bereits“
    assert db_value(_database(user_dir)) == 'aus Sicherung'

    real = backup_mod._snapshot_db

    def failing(*args, **kwargs):
        raise OSError('Datenträger voll (Test)')
    monkeypatch.setattr(backup_mod, '_snapshot_db', failing)
    with pytest.raises(OSError, match='Datenträger voll'):
        backup_mod.create_backup(user_dir, _database(user_dir), 'all')
    monkeypatch.setattr(backup_mod, '_snapshot_db', real)
    archive, _ = backup_mod.create_backup(user_dir, _database(user_dir), 'all')   # Sperre frei
    assert os.path.isfile(archive)


def test_the_lock_is_free_again_after_a_successful_run(user_dir, zip_only, tmp_path):
    name = zip_only.build(user_dir, standard_files(tmp_path))
    for wipe in (False, True, False):
        backup_mod.restore_backup(user_dir, name, wipe=wipe)
    archive, _ = backup_mod.create_backup(user_dir, _database(user_dir), 'all')
    assert os.path.isfile(archive)


def test_different_data_directories_do_not_lock_each_other(user_dir, zip_only, tmp_path, monkeypatch):
    name = zip_only.build(user_dir, standard_files(tmp_path))
    other, other_name = _other_world(tmp_path, 'zweite')
    blocker, first = _start_blocked_restore(user_dir, name, monkeypatch)
    try:
        second = Call(backup_mod.restore_backup, other, other_name, wipe=True)
        second.start()
        second.join(20)
        assert not second.is_alive(), 'das zweite Verzeichnis wartet auf das erste'
        assert second.error is None, f'das zweite Verzeichnis wurde abgelehnt: {second.error}'
        assert first.is_alive(), 'die erste Wiederherstellung sollte noch angehalten sein'
        assert db_value(_database(other)) == 'aus Sicherung'
    finally:
        _finish(blocker, first)
    assert first.error is None
    assert db_value(_database(user_dir)) == 'aus Sicherung'


def test_the_lock_follows_the_directory_not_its_spelling(user_dir, zip_only, tmp_path, monkeypatch):
    name = zip_only.build(user_dir, standard_files(tmp_path))
    blocker, first = _start_blocked_restore(user_dir, name, monkeypatch)
    try:
        for spelling in (os.path.join(user_dir, ''),
                         os.path.join(user_dir, '..', os.path.basename(user_dir)),
                         os.path.join(user_dir, '.')):
            second = Call(backup_mod.restore_backup, spelling, name, wipe=False)
            second.start()
            _assert_refused(second)
    finally:
        _finish(blocker, first)


def test_the_handlers_report_a_running_job_as_an_error_redirect(user_dir, zip_only, tmp_path,
                                                                 monkeypatch):
    monkeypatch.setattr(handlers.userctx, 'user_data_dir', lambda: user_dir)
    name = zip_only.build(user_dir, standard_files(tmp_path))
    blocker, first = _start_blocked_restore(user_dir, name, monkeypatch)
    try:
        class FakeDb:
            db_name = _database(user_dir)
        for call in (lambda: handlers.handle_backup_restore(None, {'archive': [name], 'mode': ['wipe']}),
                     lambda: handlers.handle_backup_create(FakeDb(), {'scope': ['all']})):
            status, location = call()
            parts = urllib.parse.urlsplit(location)
            query = urllib.parse.parse_qs(parts.query)
            assert (status, parts.path) == (303, '/miscellaneous')
            assert set(query) & {'restore', 'backup'}
            assert list(query.get('restore', query.get('backup'))) == ['error']
            assert 'läuft bereits' in query['msg'][0]
    finally:
        _finish(blocker, first)


# ── Aufräumen ────────────────────────────────────────────────────────────────
def _litter(user_dir):
    """Reste früherer Läufe und eigene Dinge in backup/."""
    for rel in ('restore_alt1/x.txt', 'restore_alt2/tief/y.txt', 'replaced_alt/Belege/z.txt',
                'Eigene Notiz.txt', 'Eigenes/a.txt'):
        path = archive_path(user_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('vorhanden ' + rel)
    os.makedirs(archive_path(user_dir, 'restore_leer'), exist_ok=True)
    os.makedirs(archive_path(user_dir, 'replaced_leer'), exist_ok=True)


def _backup_tree(user_dir):
    return {p: s for p, s in installation.tree(os.path.join(user_dir, 'backup')).items()}


@pytest.mark.parametrize('outcome', ['gelingt', 'scheitert'])
@WIPE
def test_old_restore_folders_are_removed_at_the_start_but_replaced_folders_never(
        user_dir, zip_only, tmp_path, outcome, wipe):
    name = zip_only.build(user_dir, standard_files(tmp_path))
    if outcome == 'scheitert':
        with open(archive_path(user_dir, name), 'wb') as f:
            f.write(b'kein Archiv')
    _litter(user_dir)
    before = _backup_tree(user_dir)

    if outcome == 'gelingt':
        backup_mod.restore_backup(user_dir, name, wipe=wipe)
    else:
        with pytest.raises(RuntimeError, match='beschädigt'):
            backup_mod.restore_backup(user_dir, name, wipe=wipe)

    after = _backup_tree(user_dir)
    assert not [p for p in after if p.split(os.sep)[0].startswith('restore_')], 'restore_* blieb liegen'
    survivors = {p: s for p, s in before.items() if not p.split(os.sep)[0].startswith('restore_')}
    assert after == survivors, 'backup/ enthält mehr oder weniger als die Archive, replaced_* und Eigenes'
    assert os.path.isdir(archive_path(user_dir, 'replaced_alt'))
    assert os.path.isdir(archive_path(user_dir, 'replaced_leer'))


def test_replaced_folders_are_never_removed_by_a_later_restore(user_dir, zip_only, tmp_path):
    """Auch nicht, wenn ein früherer Lauf einen alten Stand dort zurückgelassen hat."""
    name = zip_only.build(user_dir, standard_files(tmp_path))
    held = archive_path(user_dir, 'replaced_vom_letzten_mal')
    os.makedirs(os.path.join(held, 'Belege'))
    with open(os.path.join(held, 'buch.db'), 'wb') as f:
        f.write(b'alter Stand')
    before = installation.tree(held)
    for wipe in (False, True, False):
        backup_mod.restore_backup(user_dir, name, wipe=wipe)
    assert installation.tree(held) == before
