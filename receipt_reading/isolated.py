# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Belegdatei im Kindprozess lesen: hartes Zeitlimit, begrenzter Speicher, nie eine Ausnahme.

:func:`read_isolated` startet ``python -m receipt_reading.worker`` (siehe dort das Protokoll), schickt ihm den Namen, die
Angaben der eigenen Firmen und die Bytes der Datei und prüft, was zurückkommt, streng als :class:`receipt_reading.Reading`
(:meth:`~receipt_reading.Reading.from_dict`). Was schiefgeht, ist eine Lesung mit Status und Befund, nie eine Ausnahme und nie ein
Text des Kindprozesses: ``too_large`` (Datei zu groß, ohne Prozess; oder Zeitlimit: Befund ``time``) und ``failed`` (Befund
``reader_crashed``: nicht gestartet oder mit Fehlercode beendet; ``reader_output``: Ausgabe zu groß oder keine Lesung).
Was der Prozess auf stderr schreibt, steht höchstens gekürzt im Log des Servers.

Ohne ``-I``: Der Prozess braucht die Benutzer-Pakete (pdfplumber). Gearbeitet wird im Projektordner (``cwd``), damit
``receipt_reading`` und seine Nachbarn gefunden werden, egal wo der Server gestartet wurde.
"""
import json
import logging
import os
import subprocess
import sys
from typing import Optional

import receipt_reading as package
from . import Reading

MAX_OUTPUT_BYTES = 1024 * 1024           # größer ist keine Lesung (Text 20 000 Zeichen, ``raw`` 64 KB)
STDERR_LOG_CHARS = 500
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_log = logging.getLogger(__name__)


def _failed(note: str) -> Reading:
    return Reading(status='failed', notes=[note])


def _log_stderr(returncode: int, stderr: Optional[bytes]) -> None:
    detail = (stderr or b'')[:STDERR_LOG_CHARS * 4].decode('utf-8', 'replace').strip()[:STDERR_LOG_CHARS]
    _log.warning('Belegleser: Code %s, stderr: %s', returncode, detail)


def _run(name, data: bytes, own, timeout: float, command: Optional[list]) -> Reading:
    if len(data) > package.MAX_READ_BYTES:
        return Reading(status='too_large')
    head = json.dumps({'name': str(name or ''), 'own': [list(own[0]), list(own[1])], 'timeout': timeout}).encode('ascii') + b'\n'
    try:
        done = subprocess.run(command or [sys.executable, '-m', 'receipt_reading.worker'], input=head + data,
                              capture_output=True, timeout=timeout, cwd=_ROOT)
    except subprocess.TimeoutExpired:
        return Reading(status='too_large', notes=['time'])
    except OSError as error:
        _log.warning('Belegleser nicht gestartet: %s', type(error).__name__)
        return _failed('reader_crashed')
    if done.returncode != 0:
        _log_stderr(done.returncode, done.stderr)
        return _failed('reader_crashed')
    if len(done.stdout) > MAX_OUTPUT_BYTES:
        return _failed('reader_output')
    try:
        return Reading.from_dict(json.loads(done.stdout.decode('utf-8')))
    except Exception:                    # kein UTF-8, kein JSON, zu tief verschachtelt, keine Lesung
        _log_stderr(done.returncode, done.stderr)
        return _failed('reader_output')


def read_isolated(name, data: bytes, own, timeout: Optional[float] = None, command: Optional[list] = None) -> Reading:
    """Dasselbe wie :func:`receipt_reading.read_document`, aber im Kindprozess. ``timeout``: Sekunden, hart (Standard:
    ``receipt_reading.READ_TIMEOUT``, bei jedem Aufruf gelesen); ``command``: ein anderer Befehl statt des Lesers (Tests).
    Eine Datei über ``MAX_READ_BYTES`` ist ``too_large``, ohne dass ein Prozess startet. Wirft nie."""
    try:
        return _run(name, data, own, package.READ_TIMEOUT if timeout is None else timeout, command)
    except Exception as error:
        _log.warning('Belegleser: %s', type(error).__name__)
        return _failed('reader_crashed')
