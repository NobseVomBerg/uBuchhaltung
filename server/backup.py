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
selbst und (defensiv) users/ sind von "alle Daten" ausgenommen.
"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime

from document_parser import _find_sevenzip

# Verzeichnisse, die nie mitgesichert oder beim Wiederherstellen gelöscht werden
_EXCLUDED_DIRS = ('backup', 'users')


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
                # Alles außer backup/, users/ und der Live-DB (die kommt als Snapshot)
                excludes = [f'-xr!{d}' for d in _EXCLUDED_DIRS] + ['-x!buch.db']
                r = subprocess.run([sevenzip, 'a', '-bd', '-y', archive, '*'] + excludes,
                                   cwd=user_dir, capture_output=True, timeout=600)
                if r.returncode not in (0, 1):   # 1 = Warnung (z.B. Datei gerade gesperrt)
                    raise RuntimeError(f'7-Zip-Fehler: {r.stderr.decode(errors="replace")[:200]}')
            r = subprocess.run([sevenzip, 'a', '-bd', '-y', archive, db_copy],
                               capture_output=True, timeout=600)
            if r.returncode not in (0, 1):
                raise RuntimeError(f'7-Zip-Fehler: {r.stderr.decode(errors="replace")[:200]}')
        else:
            import zipfile
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
                            if rel == 'buch.db':
                                continue   # Live-DB: Snapshot ist schon drin
                            zf.write(os.path.join(root, f), rel)

    return archive, os.path.getsize(archive)


def restore_backup(user_dir, archive_name, wipe=False):
    """Backup wiederherstellen.

    archive_name: Dateiname (ohne Pfad) aus <UserDir>/backup/.
    wipe=True: vorhandene Daten vorher löschen (außer backup/ und users/),
    sonst werden bestehende Dateien überschrieben und übrige bleiben liegen.
    """
    # Kein Pfad-Traversal: nur Basename, muss im backup-Verzeichnis liegen
    archive_name = os.path.basename(archive_name)
    archive = os.path.join(user_dir, 'backup', archive_name)
    if not os.path.isfile(archive):
        raise FileNotFoundError(f'Backup {archive_name} nicht gefunden.')

    if wipe:
        for entry in os.listdir(user_dir):
            if entry in _EXCLUDED_DIRS:
                continue
            path = os.path.join(user_dir, entry)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

    sevenzip = _find_sevenzip()
    if sevenzip and archive.endswith(('.7zip', '.7z')):
        r = subprocess.run([sevenzip, 'x', '-bd', '-y', f'-o{user_dir}', archive],
                           capture_output=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f'7-Zip-Fehler: {r.stderr.decode(errors="replace")[:200]}')
    elif archive.endswith('.zip'):
        import zipfile
        with zipfile.ZipFile(archive) as zf:
            base = os.path.realpath(user_dir)
            for member in zf.namelist():
                # Zip-Slip-Schutz: Ziel muss im Benutzerverzeichnis bleiben
                target = os.path.realpath(os.path.join(user_dir, member))
                if not (target == base or target.startswith(base + os.sep)):
                    raise RuntimeError(f'Unsicherer Pfad im Archiv: {member}')
            zf.extractall(user_dir)
    else:
        raise RuntimeError('Kein 7-Zip installiert – .7zip-Archiv kann nicht entpackt werden.')
