# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen für die Tests zu Sicherung und Wiederherstellung (server/backup.py).

Archive in beiden Formaten bauen (zip per stdlib, 7-Zip per Kommandozeile), Datenverzeichnisse
Byte für Byte vergleichen, Fehler beim Verschieben und Schreiben einspielen und nebenläufige
Aufrufe anhalten. Verwendet von test_backup_restore_safety.py und
test_backup_restore_hardening.py. Alle Inhalte sind synthetische Testdaten.
"""
import contextlib
import errno
import os
import random
import shutil
import sqlite3
import stat
import struct
import subprocess
import threading
import zipfile
import zlib

import pytest

import version
from server import backup as backup_mod
from tests import installation

STEM = '20260105_Backup'
SIDECARS = ('buch.db-journal', 'buch.db-wal', 'buch.db-shm')
EXCLUDED = ('backup', 'users', 'tests')

# Das echte subprocess.run: Tests ersetzen backup_mod.subprocess.run (derselbe Modulname),
# die Helfer hier dürfen davon nie betroffen sein.
REAL_RUN = subprocess.run


# ── Datenverzeichnis vergleichen ─────────────────────────────────────────────
def snapshot(user_dir):
    """Alle Dateien (sha256 des Inhalts) und Verzeichnisse (None) unter ``user_dir`` außer
    backup/ – der Maßstab für „byteweise unverändert“."""
    return {path: state for path, state in installation.tree(user_dir).items()
            if path != 'backup' and not path.startswith('backup' + os.sep)}


def leftovers(user_dir):
    """Reste von Zwischenverzeichnissen in backup/ (restore_*, replaced_*)."""
    folder = os.path.join(user_dir, 'backup')
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if n.startswith(('restore_', 'replaced_')))


def archive_path(user_dir, name):
    folder = os.path.join(user_dir, 'backup')
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


def top_level(user_dir):
    """Namen im Datenverzeichnis ohne die ausgenommenen Ordner."""
    return sorted(n for n in os.listdir(user_dir) if n not in EXCLUDED)


# ── Datenbanken ──────────────────────────────────────────────────────────────
def write_db(path, value='original', rows=1, user_version=None, wal=False):
    """Eine kleine SQLite-Datenbank (Tabelle T, erste Zeile ``value``) anlegen."""
    user_version = version.SCHEMA_VERSION if user_version is None else user_version
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        conn.execute('CREATE TABLE T (X TEXT)')
        conn.executemany('INSERT INTO T VALUES (?)',
                         [(value if i == 0 else f'{value} {i} ' + 'x' * 10,) for i in range(rows)])
        conn.commit()
        conn.execute(f'PRAGMA user_version = {int(user_version)}')
        if wal:
            conn.execute('PRAGMA journal_mode = WAL')
        conn.commit()


def db_value(path):
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        return conn.execute('SELECT X FROM T ORDER BY rowid LIMIT 1').fetchone()[0]


def db_bytes(tmp_path, name='probe.db', **kwargs):
    """Inhalt einer frisch angelegten Datenbank (für den Einbau in Archive)."""
    folder = tmp_path / 'datenbanken'
    folder.mkdir(exist_ok=True)
    path = folder / name
    if path.exists():
        path.unlink()
    write_db(path, **kwargs)
    return path.read_bytes()


def damaged_db_bytes(tmp_path, kind):
    """Eine beschädigte Datenbank: Zufallsbytes, zerstörte Seite, zerstörter Kopf, abgeschnitten."""
    good = db_bytes(tmp_path, 'gross.db', rows=3000)       # mehr als 20 Seiten
    assert len(good) > 5 * 4096
    if kind == 'zufallsbytes':
        return random.Random(1).randbytes(5000)
    if kind == 'zerstoerte-seite':
        damaged = bytearray(good)
        damaged[2 * 4096:3 * 4096] = b'\xff' * 4096
        return bytes(damaged)
    if kind == 'kopf-zerstoert':
        damaged = bytearray(good)
        damaged[:16] = b'X' * 16
        return bytes(damaged)
    if kind == 'abgeschnitten':
        return good[:len(good) // 2]
    raise ValueError(kind)


def standard_files(tmp_path, value='aus Sicherung'):
    """Inhalt einer brauchbaren Sicherung (Datenbank, Datei, Beleg)."""
    return {'buch.db': db_bytes(tmp_path, value=value),
            'notiz.txt': f'Notiz {value}',
            'Belege/beleg1.txt': f'Beleginhalt {value}'}


def make_user_dir(tmp_path):
    """Datenverzeichnis wie im Betrieb: Datenbank (Zeile „original“), Belege/, notiz.txt."""
    folder = tmp_path / 'user'
    folder.mkdir()
    write_db(folder / 'buch.db', value='original')
    (folder / 'Belege').mkdir()
    (folder / 'Belege' / 'beleg1.txt').write_text('Beleginhalt', encoding='utf-8')
    (folder / 'notiz.txt').write_text('Notiz Version 1', encoding='utf-8')
    return str(folder)


# ── Archive bauen ────────────────────────────────────────────────────────────
def sevenzip_add(sevenzip, archive, source, *options, patterns=('*',)):
    result = REAL_RUN([sevenzip, 'a', '-bd', '-y', *options, str(archive), *patterns],
                      cwd=str(source), capture_output=True)
    assert result.returncode in (0, 1), (result.stdout + result.stderr).decode(errors='replace')


class Archives:
    """Baut Sicherungsarchive im gewählten Format: ``zip`` (stdlib) oder ``7z`` (7-Zip)."""

    def __init__(self, kind, tmp_path, sevenzip=None):
        self.kind = kind
        self.ext = '.zip' if kind == 'zip' else '.7zip'
        self.tmp_path = tmp_path
        self.sevenzip = sevenzip
        self._count = 0

    def _stage(self, files, dirs, links):
        self._count += 1
        source = self.tmp_path / f'quelle{self._count}'
        source.mkdir()
        for rel, content in files.items():
            target = source / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                target.write_text(content, encoding='utf-8')
            else:
                target.write_bytes(content)
        for rel in dirs:
            (source / rel).mkdir(parents=True, exist_ok=True)
        for rel, target in (links or {}).items():
            (source / rel).parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(target, source / rel)
            except (OSError, NotImplementedError) as exc:
                pytest.skip(f'das Dateisystem kann keine symbolischen Links anlegen ({exc})')
        return source

    def build(self, user_dir, files=None, dirs=(), links=None, name=None):
        """Archiv in <user_dir>/backup/ anlegen; ``files``: {Pfad: Text oder Bytes}, ``dirs``:
        leere Verzeichnisse, ``links``: {Pfad: Ziel} (symbolische Links). Liefert den Dateinamen."""
        name = name or STEM + self.ext
        path = archive_path(user_dir, name)
        if os.path.exists(path):
            os.remove(path)                      # 7-Zip „a“ würde sonst an das Archiv anhängen
        files = files or {}
        if self.kind == 'zip':
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for rel, content in files.items():
                    zf.writestr(rel, content)
                for rel in dirs:
                    zf.writestr(rel.rstrip('/') + '/', b'')
                for rel, target in (links or {}).items():
                    info = zipfile.ZipInfo(rel)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    zf.writestr(info, target)
        else:
            source = self._stage(files, dirs, links)
            sevenzip_add(self.sevenzip, path, source, '-snl')    # -snl: Links als Links ablegen
        return name


def make_archives(kind, monkeypatch, tmp_path):
    """Fixture-Rumpf für beide Wege von restore_backup: stdlib-zip (7-Zip „nicht gefunden“) und
    7-Zip – der zweite entfällt, wo es nicht installiert ist."""
    if kind == 'zip':
        monkeypatch.setattr(backup_mod, '_find_sevenzip', lambda: None)
        return Archives('zip', tmp_path)
    sevenzip = backup_mod._find_sevenzip()
    if not sevenzip:
        pytest.skip('7-Zip nicht installiert')
    return Archives('7z', tmp_path, sevenzip)


def require_sevenzip():
    sevenzip = backup_mod._find_sevenzip()
    if not sevenzip:
        pytest.skip('7-Zip nicht installiert')
    return sevenzip


def archive_names(archive):
    """Einträge eines Archivs (zip oder 7-Zip), Verzeichnisse ohne abschließenden Schrägstrich."""
    if str(archive).endswith('.zip'):
        with zipfile.ZipFile(archive) as zf:
            return sorted(n.rstrip('/') for n in zf.namelist())
    sevenzip = require_sevenzip()
    result = REAL_RUN([sevenzip, 'l', '-slt', '-ba', str(archive)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    return sorted(line[len('Path = '):].strip()
                  for line in result.stdout.decode(errors='replace').splitlines()
                  if line.startswith('Path = '))


def rename_in_sevenzip_header(archive, old, new):
    """Benennt einen Eintrag im unkomprimierten 7z-Kopf um (gleiche Länge) und rechnet die beiden
    Prüfsummen neu. So entsteht ein Archiv mit ``..`` im Pfad, das 7-Zip selbst nicht anlegt."""
    assert len(old) == len(new)
    with open(archive, 'rb') as f:
        data = bytearray(f.read())
    next_offset, next_size = struct.unpack_from('<QQ', data, 12)
    start = 32 + next_offset
    header = bytes(data[start:start + next_size])
    old_raw, new_raw = old.encode('utf-16-le'), new.encode('utf-16-le')
    assert old_raw in header, 'der Name steht nicht im Klartext-Kopf (Archiv mit -mhc=off bauen)'
    header = header.replace(old_raw, new_raw)
    data[start:start + next_size] = header
    struct.pack_into('<I', data, 28, zlib.crc32(header))
    struct.pack_into('<I', data, 8, zlib.crc32(bytes(data[12:32])))
    with open(archive, 'wb') as f:
        f.write(data)


# ── Fehler einspielen ────────────────────────────────────────────────────────
def _inside(path, folder):
    path = os.path.realpath(os.fspath(path))
    return path == folder or path.startswith(folder + os.sep)


class MoveFault:
    """Greift in ``os.rename`` und ``os.replace`` ein – nur für Aufrufe, bei denen Quelle oder
    Ziel unter ``root`` liegen; alle anderen laufen unverändert durch.

    * ``fail_at=n``: der n-te Aufruf scheitert mit ``EIO`` (``onwards=True``: auch alle späteren)
    * ``boundary=<Ordner>``: Aufrufe, die diesen Ordner überqueren (Quelle innen und Ziel außen
      oder umgekehrt), scheitern mit ``EXDEV`` – als läge der Ordner auf einem anderen
      Dateisystem. ``names`` begrenzt das auf Quellen mit diesen Namen („einzelne Pfade“),
      ``once`` lässt jede Quelle nur beim ersten Versuch scheitern.

    ``calls``: alle Aufrufe unter ``root`` als (Quelle, Ziel); ``refused``: die abgewiesenen als
    (Quelle, Ziel, errno).
    """

    def __init__(self, root, fail_at=None, onwards=False, boundary=None, names=None, once=False):
        self.root = os.path.realpath(str(root))
        self.fail_at, self.onwards = fail_at, onwards
        self.boundary = os.path.realpath(str(boundary)) if boundary else None
        self.names, self.once = names, once
        self.calls, self.refused, self._seen = [], [], set()

    def _crossing(self, src, dst):
        if self.boundary is None:
            return False
        if _inside(src, self.boundary) == _inside(dst, self.boundary):
            return False
        if self.names is not None and os.path.basename(os.fspath(src)) not in self.names \
                and os.path.basename(os.fspath(dst)) not in self.names:
            return False
        return True

    def _wrap(self, real):
        def moved(src, dst, *args, **kwargs):
            if not (_inside(src, self.root) or _inside(dst, self.root)):
                return real(src, dst, *args, **kwargs)
            self.calls.append((os.fspath(src), os.fspath(dst)))
            number = len(self.calls)
            if self.fail_at is not None and (number == self.fail_at
                                             or (self.onwards and number > self.fail_at)):
                self.refused.append((os.fspath(src), os.fspath(dst), errno.EIO))
                raise OSError(errno.EIO, 'Eingabe-/Ausgabefehler (Test)', os.fspath(src))
            if self._crossing(src, dst):
                key = os.path.realpath(os.fspath(src))
                if not (self.once and key in self._seen):
                    self._seen.add(key)
                    self.refused.append((os.fspath(src), os.fspath(dst), errno.EXDEV))
                    raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), os.fspath(src))
            return real(src, dst, *args, **kwargs)
        return moved

    def install(self, monkeypatch):
        monkeypatch.setattr(os, 'rename', self._wrap(os.rename))
        monkeypatch.setattr(os, 'replace', self._wrap(os.replace))
        return self


class WriteFault:
    """Hält fest, in welcher Reihenfolge Dateien in ``user_dir`` ankommen, und lässt das Ablegen
    der Dateien in ``fail`` (Pfade relativ zu ``user_dir``) scheitern. Erfasst werden alle Wege,
    auf denen eine Datei an ihren Platz kommt: ``os.rename``/``os.replace`` und die
    ``shutil``-Kopierfunktionen. ``arrived``: relative Zielpfade in der Reihenfolge des Eintreffens."""

    def __init__(self, user_dir, fail=()):
        self.root = os.path.realpath(str(user_dir))
        self.backup = os.path.join(self.root, 'backup')
        self.fail = set(fail)
        self.arrived = []
        self.refused = []

    @property
    def order(self):
        """``arrived`` ohne unmittelbare Wiederholungen (copy2 ruft copyfile, move ruft rename)."""
        return [rel for i, rel in enumerate(self.arrived) if i == 0 or self.arrived[i - 1] != rel]

    def _relative(self, dst):
        dst = os.path.realpath(os.fspath(dst))
        if _inside(dst, self.backup) or not _inside(dst, self.root):
            return None
        return os.path.relpath(dst, self.root)

    def _wrap(self, real):
        def arriving(src, dst, *args, **kwargs):
            rel = self._relative(dst)
            if rel is not None and rel in self.fail:
                self.refused.append(rel)
                raise OSError(errno.EIO, 'Eingabe-/Ausgabefehler (Test)', os.fspath(dst))
            result = real(src, dst, *args, **kwargs)
            if rel is not None and os.path.isfile(dst):
                self.arrived.append(rel)
            return result
        return arriving

    def install(self, monkeypatch):
        monkeypatch.setattr(os, 'rename', self._wrap(os.rename))
        monkeypatch.setattr(os, 'replace', self._wrap(os.replace))
        for name in ('copyfile', 'copy', 'copy2', 'move'):
            monkeypatch.setattr(shutil, name, self._wrap(getattr(shutil, name)))
        return self


# ── Nebenläufigkeit ──────────────────────────────────────────────────────────
class Blocker:
    """Hält den ERSTEN Aufruf einer Funktion an, bis ``proceed`` gesetzt ist, und meldet über
    ``entered``, dass er angekommen ist. Spätere Aufrufe laufen ungehindert durch – so hängt ein
    Test nicht, wenn ein Aufruf fälschlich nicht abgelehnt wird."""

    def __init__(self):
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self._lock = threading.Lock()
        self._used = False

    def wrap(self, original):
        def blocked(*args, **kwargs):
            with self._lock:
                first, self._used = not self._used, True
            if first:
                self.entered.set()
                if not self.proceed.wait(timeout=30):
                    raise AssertionError('der angehaltene Aufruf wurde nie freigegeben')
            return original(*args, **kwargs)
        return blocked


class Call(threading.Thread):
    """Ruft ``fn`` in einem eigenen Thread auf und hält Ergebnis oder Ausnahme fest."""

    def __init__(self, fn, *args, **kwargs):
        super().__init__(daemon=True)
        self._fn, self._args, self._kwargs = fn, args, kwargs
        self.value = None
        self.error = None

    def run(self):
        try:
            self.value = self._fn(*self._args, **self._kwargs)
        except BaseException as exc:         # noqa: BLE001 – der Test wertet die Ausnahme aus
            self.error = exc
