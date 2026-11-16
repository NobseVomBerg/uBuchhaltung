# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Zwischenablage für den Upload der WISO-Datenbank (Import aus der Datenbank, seit 2.3.7).

Der Browser legt ``DB1.FDB`` (Mandant, Pflicht) und optional ``DB0.FDB`` (Standard-Kontenrahmen) je als rohen Body über
``POST /wiso/fdb-upload`` ab (``server/app.py``); das Formular des Imports nennt danach nur noch die Kennung des Ordners
(``upload``). Die Dateien liegen unter ``<Datenordner des Benutzers>/upload_tmp/wiso_fdb/<Kennung>/`` – ``upload_tmp`` ist von
Datensicherung und Wiederherstellung ausgenommen (``server/backup.py``) – und tragen FESTE Namen: Der Dateiname des Browsers
wird nie verwendet. Die Kennung (``secrets.token_hex(16)``: 32 Kleinbuchstaben und Ziffern) ist die einzige Eingabe, die einen
Pfad bestimmt; alles andere ergibt keinen.

Je Benutzer wartet höchstens EIN Upload: ``begin()`` räumt vorher alle anderen Ordner weg (``sweep``), außer einem, in dem
gerade ein Import läuft (Markerdatei ``.importing``, höchstens ``STALE_AFTER`` alt). Der Import löscht seinen Ordner in jedem
Fall (``discard``).
"""
import os
import re
import secrets
import shutil
import tempfile
import time
from contextlib import suppress

import userctx

MAX_WISO_DB_BODY = 2 * 1024 ** 3        # größte Datei je Upload (Bytes)
CHUNK = 1024 * 1024                     # so viel liest ``store`` auf einmal von der Verbindung
FREE_SPACE_MARGIN = 64 * 1024 * 1024    # so viel Platz muss nach dem Upload mindestens frei bleiben
STALE_AFTER = 6 * 3600                  # ein Marker ``.importing``, der älter ist, schützt seinen Ordner nicht mehr (Sekunden)

#: Dateinamen, die WISO Mein Büro im DB-Verzeichnis anlegt – unter diesen Namen liegen die Uploads
WISO_CLIENT_DB = 'DB1.FDB'
WISO_STANDARD_DB = 'DB0.FDB'
SLOT_FILES = {'client': WISO_CLIENT_DB, 'standard': WISO_STANDARD_DB}

#: Im Demo-Modus ist der Import abgeschaltet (Route, Handler und Seite sagen denselben Satz)
DEMO_REFUSED = 'Im Demo-Modus ist der Import aus der WISO-Datenbank abgeschaltet.'

IMPORTING = '.importing'
_TOKEN = re.compile(r'[0-9a-f]{32}')


class UploadIncomplete(Exception):
    """Es kamen weniger Bytes an, als der Kopf versprach (oder die Verbindung brach ab)."""


class UploadFailed(Exception):
    """Die Datei ließ sich nicht schreiben (Platte voll, Ordner verschwunden, keine Rechte)."""


def upload_root():
    """Ordner, unter dem die Zwischenordner des angemeldeten Benutzers liegen."""
    return os.path.join(userctx.user_data_dir(), 'upload_tmp', 'wiso_fdb')


def valid_token(token):
    return isinstance(token, str) and _TOKEN.fullmatch(token) is not None


def folder_path(token):
    """Pfad des Zwischenordners zu ``token`` – gleich, ob es ihn gibt – oder ``None``, wenn die Kennung ungültig ist."""
    return os.path.join(upload_root(), token) if valid_token(token) else None


def upload_dir(token):
    """Zwischenordner zu ``token`` oder ``None``: Die Kennung muss gültig sein, der Ordner existieren, direkt unter
    ``upload_root()`` liegen und kein Link sein."""
    path = folder_path(token)
    if path is None or os.path.islink(path) or not os.path.isdir(path):
        return None
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(upload_root()):
        return None
    return path


def _importing(folder, now):
    try:
        modified = os.lstat(os.path.join(folder, IMPORTING)).st_mtime
    except OSError:
        return False
    return now - modified < STALE_AFTER


def sweep(keep=None):
    """Alle Zwischenordner unter ``upload_root()`` entfernen – außer ``keep`` und einem, in dem ein Import läuft (frischer
    Marker ``.importing``). Ein Link dort wird nur selbst entfernt, nie sein Ziel."""
    root = upload_root()
    try:
        names = os.listdir(root)
    except OSError:
        return
    now = time.time()
    for name in names:
        path = os.path.join(root, name)
        if name == keep:
            continue
        if os.path.islink(path):
            try:
                os.remove(path)
            except OSError:
                with suppress(OSError):
                    os.rmdir(path)              # Windows: Verzeichnis-Link
        elif os.path.isdir(path) and not _importing(path, now):
            shutil.rmtree(path, ignore_errors=True)


def begin():
    """Neuen Zwischenordner anlegen (vorher ``sweep``) und seine Kennung liefern; ``UploadFailed``, wenn er sich nicht
    anlegen lässt (kein Schreibrecht, Platte voll)."""
    sweep()
    root = upload_root()
    token = secrets.token_hex(16)
    try:
        os.makedirs(root, exist_ok=True)
        os.mkdir(os.path.join(root, token), 0o700)
    except OSError as exc:
        raise UploadFailed(str(exc)) from exc
    return token


def enough_space(length):
    """Reicht der freie Platz für ``length`` Bytes plus ``FREE_SPACE_MARGIN``? Gemessen wird dort, wo der Zwischenordner liegt
    (oder liegen wird)."""
    folder = os.path.abspath(upload_root())
    while not os.path.isdir(folder) and os.path.dirname(folder) != folder:
        folder = os.path.dirname(folder)
    return shutil.disk_usage(folder).free >= length + FREE_SPACE_MARGIN


def _remove_quietly(path):
    with suppress(OSError):
        os.remove(path)


def store(token, slot, rfile, length):
    """``length`` Bytes aus ``rfile`` unter dem festen Namen von ``slot`` in den Zwischenordner ``token`` schreiben.

    In Stücken von ``CHUNK`` in eine Temporärdatei (``*.part``), erst nach der vollen Länge per ``os.replace`` an ihren
    Platz. Kommen weniger Bytes an (``UploadIncomplete``) oder scheitert das Schreiben (``UploadFailed``), bleibt keine
    Temporärdatei liegen."""
    folder = upload_dir(token)
    if folder is None:
        raise UploadFailed('Der Zwischenordner existiert nicht mehr.')
    target = os.path.join(folder, SLOT_FILES[slot])
    try:
        handle, part = tempfile.mkstemp(dir=folder, suffix='.part')
    except OSError as exc:
        raise UploadFailed(str(exc)) from exc
    try:
        with os.fdopen(handle, 'wb') as out:
            remaining = length
            while remaining:
                try:
                    chunk = rfile.read(min(CHUNK, remaining))
                except OSError as exc:                  # Verbindung abgebrochen
                    raise UploadIncomplete(str(exc)) from exc
                if not chunk:
                    raise UploadIncomplete('Die Verbindung endete vor der erklärten Länge.')
                out.write(chunk)
                remaining -= len(chunk)
        os.replace(part, target)
    except UploadIncomplete:
        _remove_quietly(part)
        raise
    except OSError as exc:
        _remove_quietly(part)
        raise UploadFailed(str(exc)) from exc
    except BaseException:
        _remove_quietly(part)
        raise


def mark_importing(token):
    """Den Zwischenordner als „Import läuft“ kennzeichnen: ``sweep`` lässt ihn bis ``STALE_AFTER`` in Ruhe."""
    folder = upload_dir(token)
    if folder is not None:
        with open(os.path.join(folder, IMPORTING), 'w', encoding='utf-8'):
            pass


def discard(token):
    """Den Zwischenordner zu ``token`` samt Inhalt entfernen; bei einer ungültigen oder unbekannten Kennung geschieht nichts."""
    folder = upload_dir(token)
    if folder is not None:
        _remove_quietly(os.path.join(folder, IMPORTING))      # bleibt Rest liegen (Windows), räumt ihn der nächste ``sweep``
        shutil.rmtree(folder, ignore_errors=True)


def files(token):
    """``(Mandantendatei, Standardkontenrahmen oder None)`` des Zwischenordners – ``(None, None)``, wenn es den Ordner oder
    ``DB1.FDB`` nicht gibt."""
    folder = upload_dir(token)
    if folder is None:
        return None, None
    client = os.path.join(folder, WISO_CLIENT_DB)
    if not os.path.isfile(client):
        return None, None
    standard = os.path.join(folder, WISO_STANDARD_DB)
    return client, (standard if os.path.isfile(standard) else None)
