# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Datensicherung und Wiederherstellung des Benutzer-Datenverzeichnisses.

Backups landen in <UserDir>/backup/ als JJJJMMTT_Backup.7zip (7-Zip-Format;
ohne installiertes 7-Zip als .zip via stdlib). Bei mehreren Sicherungen am
selben Tag wird eine laufende Nummer angehängt (JJJJMMTT_Backup_2.7zip).

Die Datenbank wird nie roh kopiert, sondern über die SQLite-Backup-API
konsistent gezogen (auch bei laufendem Server). Das backup/-Verzeichnis
selbst und (defensiv) users/ sind von "alle Daten" ausgenommen; ebenso die
Begleitdateien der Live-Datenbank (buch.db-journal/-wal/-shm): Ein Journal
neben dem Schnappschuss würde ihn beim Öffnen verändern.

Wiederherstellen prüft erst, dann ersetzt es: Das Archiv wird in ein
Zwischenverzeichnis backup/restore_* entpackt und dort geprüft (keine
symbolischen Links, Datenbank lesbar und nicht neuer als das Programm). Erst
danach werden die Daten ersetzt. Bei "vorher löschen" wird der vorherige Stand
nach backup/replaced_* verschoben und erst nach vollständigem Erfolg gelöscht,
bei einem Fehler zurückgelegt. Scheitert schon das Entpacken oder Prüfen, ist
das Datenverzeichnis unverändert. Beide Zwischenverzeichnisse liegen in
backup/ – das wird nie gesichert oder gelöscht und liegt auf demselben
Dateisystem wie das Ziel (Verschieben statt Kopieren).
"""
import os
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
import zlib
from datetime import datetime

import version
from document_parser import _find_sevenzip

# Verzeichnisse, die nie mitgesichert oder beim Wiederherstellen gelöscht werden.
# 'tests' = Artefakte des letzten pytest-Laufs (data/tests/, siehe tests/conftest.py).
_EXCLUDED_DIRS = ('backup', 'users', 'tests')

# Begleitdateien der Live-Datenbank: Sie gehören zu genau dieser Datei und würden eine
# andere (Schnappschuss, zurückgespielte Datenbank) beim Öffnen verändern bzw. beschädigen.
_DB_SIDECARS = ('buch.db-journal', 'buch.db-wal', 'buch.db-shm')


def backup_dir(user_dir):
    d = os.path.join(user_dir, 'backup')
    os.makedirs(d, exist_ok=True)
    return d


def list_backups(user_dir):
    """Vorhandene Backup-Archive, neueste zuerst."""
    d = os.path.join(user_dir, 'backup')
    if not os.path.isdir(d):
        return []
    return sorted((f for f in os.listdir(d)
                   if f.endswith(('.7zip', '.7z', '.zip')) and 'Backup' in f),
                  reverse=True)


def _next_archive_path(user_dir, ext):
    """Freien Archivnamen bestimmen: JJJJMMTT_Backup[_n].<ext>."""
    d = backup_dir(user_dir)
    stamp = datetime.now().strftime('%Y%m%d')
    base = f'{stamp}_Backup'
    existing = {os.path.splitext(f)[0] for f in os.listdir(d)}
    name, n = base, 1
    while name in existing:
        n += 1
        name = f'{base}_{n}'
    return os.path.join(d, name + ext)


def _snapshot_db(db_path, dest_path):
    """Konsistente Kopie der SQLite-DB via Backup-API (transaktionssicher)."""
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def create_backup(user_dir, db_path, scope='all'):
    """Backup erstellen. scope: 'db' (nur Datenbank) oder 'all' (alle Daten).

    Returns (archive_path, size_bytes).
    """
    # Absolute Pfade: der 7z-Aufruf läuft mit cwd=user_dir – ein relativer
    # Archivpfad würde dort doppelt aufgelöst und das Archiv landet daneben
    user_dir = os.path.abspath(user_dir)
    db_path = os.path.abspath(db_path)
    sevenzip = _find_sevenzip()
    ext = '.7zip' if sevenzip else '.zip'
    archive = _next_archive_path(user_dir, ext)

    with tempfile.TemporaryDirectory() as tmp:
        db_copy = os.path.join(tmp, 'buch.db')
        _snapshot_db(db_path, db_copy)

        if sevenzip:
            if scope == 'all':
                # Alles außer backup/, users/, tests/, der Live-DB (die kommt als Snapshot)
                # und deren Begleitdateien (-journal/-wal/-shm).
                # -x! (ohne r) trifft nur die oberste Ebene – gleichnamige Unterordner
                # weiter unten (z. B. Belege/tests/) gehören zur Sicherung.
                excludes = ([f'-x!{d}' for d in _EXCLUDED_DIRS]
                            + [f'-x!{n}' for n in ('buch.db',) + _DB_SIDECARS])
                r = subprocess.run([sevenzip, 'a', '-bd', '-y', archive, '*'] + excludes,
                                   cwd=user_dir, capture_output=True, timeout=600)
                if r.returncode not in (0, 1):   # 1 = Warnung (z.B. Datei gerade gesperrt)
                    raise RuntimeError(f'7-Zip-Fehler: {r.stderr.decode(errors="replace")[:200]}')
            r = subprocess.run([sevenzip, 'a', '-bd', '-y', archive, db_copy],
                               capture_output=True, timeout=600)
            if r.returncode not in (0, 1):
                raise RuntimeError(f'7-Zip-Fehler: {r.stderr.decode(errors="replace")[:200]}')
        else:
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(db_copy, 'buch.db')
                if scope == 'all':
                    for root, dirs, files in os.walk(user_dir):
                        rel_root = os.path.relpath(root, user_dir)
                        top = rel_root.split(os.sep)[0]
                        if top in _EXCLUDED_DIRS:
                            dirs[:] = []
                            continue
                        for f in files:
                            rel = os.path.normpath(os.path.join(rel_root, f))
                            if rel == 'buch.db' or rel in _DB_SIDECARS:
                                continue   # Live-DB: Snapshot ist schon drin, ihr Journal gehört nicht daneben
                            zf.write(os.path.join(root, f), rel)

    return archive, os.path.getsize(archive)


def _remove_entry(path):
    """Datei, Link oder Verzeichnis (samt Inhalt) löschen."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def _extract(archive, dest):
    """Archiv (.7zip/.7z mit 7-Zip, .zip per stdlib) nach ``dest`` entpacken."""
    sevenzip = _find_sevenzip()
    if sevenzip and archive.endswith(('.7zip', '.7z')):
        r = subprocess.run([sevenzip, 'x', '-bd', '-y', f'-o{dest}', archive],
                           capture_output=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f'7-Zip-Fehler: {r.stderr.decode(errors="replace")[:200]}')
    elif archive.endswith('.zip'):
        try:
            with zipfile.ZipFile(archive) as zf:
                base = os.path.realpath(dest)
                for member in zf.namelist():
                    # Zip-Slip-Schutz: Ziel muss im Zielverzeichnis bleiben
                    target = os.path.realpath(os.path.join(dest, member))
                    if not (target == base or target.startswith(base + os.sep)):
                        raise RuntimeError(f'Unsicherer Pfad im Archiv: {member}')
                zf.extractall(dest)
        except (zipfile.BadZipFile, zlib.error, EOFError):
            # BadZipFile: kein zip bzw. Prüfsumme falsch; zlib.error/EOFError: mitten im
            # Datenstrom beschädigt oder abgeschnitten
            raise RuntimeError('Archiv ist beschädigt und lässt sich nicht entpacken.') from None
    else:
        raise RuntimeError('Kein 7-Zip installiert – .7zip-Archiv kann nicht entpackt werden.')


def _drop_unwanted(staging):
    """Entfernt aus dem Zwischenverzeichnis, was nie zurückgespielt wird: Einträge der
    obersten Ebene, die von der Sicherung ausgenommen sind, und die Begleitdateien der Datenbank."""
    for name in os.listdir(staging):
        if name in _EXCLUDED_DIRS or name in _DB_SIDECARS:
            _remove_entry(os.path.join(staging, name))


def _check_database(db_file):
    """Datenbank aus der Sicherung nur lesend prüfen: unversehrt, nicht leer und nicht neuer
    als das Programm."""
    try:
        conn = sqlite3.connect(pathlib.Path(os.path.abspath(db_file)).as_uri() + '?mode=ro', uri=True)
        try:
            check = conn.execute('PRAGMA quick_check').fetchall()
            user_version = conn.execute('PRAGMA user_version').fetchone()[0]
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        raise RuntimeError('Die Datenbank in der Sicherung ist beschädigt.') from None
    if check != [('ok',)]:
        raise RuntimeError('Die Datenbank in der Sicherung ist beschädigt.')
    if not tables:
        # Eine leere Datei gilt SQLite als gültige, leere Datenbank – zurückgespielt
        # ersetzte sie die Buchhaltung durch nichts
        raise RuntimeError('Die Datenbank in der Sicherung ist leer.')
    if user_version > version.SCHEMA_VERSION:
        raise RuntimeError(
            f'Die Sicherung stammt aus einer neueren Programmversion '
            f'({version.describe_schema_version(user_version)}) und kann mit dieser Version '
            f'({version.describe_schema_version(version.SCHEMA_VERSION)}) nicht geöffnet werden.')


def _check_staging(staging, user_dir, wipe):
    """Entpacktes Archiv prüfen, bevor am Datenverzeichnis etwas geändert wird.

    Nimmt heraus, was nie zurückgespielt wird, und wirft RuntimeError mit Klartext,
    wenn die Sicherung nicht brauchbar ist.
    """
    # Links könnten beim Zurückspielen aus dem Datenverzeichnis hinausführen
    for root, dirs, files in os.walk(staging, followlinks=False):
        for name in dirs + files:
            if os.path.islink(os.path.join(root, name)):
                raise RuntimeError('Archiv enthält symbolische Links und wird aus '
                                   'Sicherheitsgründen nicht zurückgespielt.')

    _drop_unwanted(staging)
    if not os.listdir(staging):
        raise RuntimeError('Archiv enthält keine Daten.')

    db_file = os.path.join(staging, 'buch.db')
    if os.path.isfile(db_file):
        _check_database(db_file)
        # Das nur lesende Öffnen einer Datenbank im WAL-Modus legt -wal/-shm neu an
        _drop_unwanted(staging)
    elif wipe:
        raise RuntimeError('Die Sicherung enthält keine Datenbank – ‚vorher löschen‘ '
                           'würde alle Daten entfernen.')

    if not wipe:
        # Überschreiben kann keine Datei über ein Verzeichnis legen (und umgekehrt)
        for root, dirs, files in os.walk(staging):
            rel_root = os.path.relpath(root, staging)
            for name in dirs + files:
                rel = os.path.normpath(os.path.join(rel_root, name))
                target = os.path.join(user_dir, rel)
                if not os.path.lexists(target):
                    continue
                if name in dirs and not os.path.isdir(target):
                    raise RuntimeError(f'Das Verzeichnis „{rel}“ aus der Sicherung lässt sich '
                                       f'nicht über eine vorhandene Datei zurückspielen.')
                if name not in dirs and os.path.isdir(target):
                    raise RuntimeError(f'Die Datei „{rel}“ aus der Sicherung lässt sich '
                                       f'nicht über ein vorhandenes Verzeichnis zurückspielen.')


def _replace_all(staging, user_dir):
    """Datenverzeichnis (außer backup/, users/, tests/) durch den Inhalt von ``staging`` ersetzen.

    Der vorherige Stand wird nach backup/replaced_* verschoben, nicht gelöscht: Erst wenn
    alles an seinem Platz ist, wird er entfernt; bei einem Fehler wird er zurückgelegt.
    """
    replaced = tempfile.mkdtemp(prefix='replaced_', dir=backup_dir(user_dir))
    moved_out, moved_in = [], []
    try:
        for name in os.listdir(user_dir):
            if name in _EXCLUDED_DIRS:
                continue
            os.rename(os.path.join(user_dir, name), os.path.join(replaced, name))
            moved_out.append(name)
        for name in os.listdir(staging):
            os.rename(os.path.join(staging, name), os.path.join(user_dir, name))
            moved_in.append(name)
    except Exception as e:
        stuck = False
        for name in reversed(moved_in):
            try:
                _remove_entry(os.path.join(user_dir, name))
            except OSError:
                stuck = True
        for name in reversed(moved_out):
            try:
                os.rename(os.path.join(replaced, name), os.path.join(user_dir, name))
            except OSError:
                stuck = True
        if stuck:
            # replaced_* bleibt bewusst liegen: Dort steht, was nicht zurückgelegt werden konnte
            raise RuntimeError(
                f'Wiederherstellen abgebrochen ({e}), und der vorherige Stand konnte nicht '
                f'vollständig zurückgelegt werden. Er liegt in backup/{os.path.basename(replaced)} '
                f'– bitte von dort zurückkopieren und den Ordner erst danach löschen.') from e
        shutil.rmtree(replaced, ignore_errors=True)
        raise RuntimeError(
            f'Wiederherstellen abgebrochen, der vorherige Stand ist unverändert: {e}') from e
    shutil.rmtree(replaced, ignore_errors=True)


def _overlay(staging, user_dir):
    """Dateien der Sicherung über das Datenverzeichnis legen; Vorhandenes, das nicht in
    der Sicherung steht, bleibt liegen."""
    replaces_db = os.path.isfile(os.path.join(staging, 'buch.db'))
    for root, dirs, files in os.walk(staging):
        rel_root = os.path.relpath(root, staging)
        target_root = user_dir if rel_root == '.' else os.path.join(user_dir, rel_root)
        os.makedirs(target_root, exist_ok=True)       # auch leere Verzeichnisse der Sicherung
        for name in files:
            os.replace(os.path.join(root, name), os.path.join(target_root, name))
    if replaces_db:
        # Journal/WAL gehören zur alten Datei und würden die zurückgespielte beschädigen
        for name in _DB_SIDECARS:
            path = os.path.join(user_dir, name)
            if os.path.lexists(path):
                os.remove(path)


def restore_backup(user_dir, archive_name, wipe=False):
    """Backup wiederherstellen – erst prüfen, dann ersetzen.

    archive_name: Dateiname (ohne Pfad) aus <UserDir>/backup/.

    Das Archiv wird zuerst in ein Zwischenverzeichnis backup/restore_* entpackt und
    dort geprüft (keine symbolischen Links, Datenbank lesbar und nicht neuer als das
    Programm). Scheitert das, wirft die Funktion RuntimeError und das Datenverzeichnis
    ist unverändert. Nicht zurückgespielt werden backup/, users/ und tests/ sowie die
    Begleitdateien der Datenbank (buch.db-journal/-wal/-shm).

    wipe=True: vorhandene Daten (außer backup/, users/ und tests/) werden durch die
    Sicherung ersetzt, die dafür eine Datenbank enthalten muss. Der vorherige Stand
    wird verschoben und erst nach Erfolg gelöscht; bei einem Fehler kommt er zurück.
    Sonst werden bestehende Dateien überschrieben und übrige bleiben liegen.
    """
    # Kein Pfad-Traversal: nur Basename, muss im backup-Verzeichnis liegen
    archive_name = os.path.basename(archive_name)
    archive = os.path.join(user_dir, 'backup', archive_name)
    if not os.path.isfile(archive):
        raise FileNotFoundError(f'Backup {archive_name} nicht gefunden.')

    staging = tempfile.mkdtemp(prefix='restore_', dir=backup_dir(user_dir))
    try:
        _extract(archive, staging)
        _check_staging(staging, user_dir, wipe)
        if wipe:
            _replace_all(staging, user_dir)
        else:
            _overlay(staging, user_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
