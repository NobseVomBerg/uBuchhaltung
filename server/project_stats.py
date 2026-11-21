# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Zahlen der About-Seite („Projekt in Zahlen“): ``project_stats.json`` neben ``version.py`` streng lesen.

``tools/project_stats.py`` zählt Dateien, Zeilen und Tests und legt das Ergebnis als kleine JSON-Datei im Projektordner ab. Die Seite
liest nur diese Datei – zur Laufzeit wird nichts gezählt (im Docker-Image fehlt ``tests/``). Der Ort ist der Ordner von ``version.py``,
nie das Arbeitsverzeichnis: Der Server läuft auch mit einem anderen.

Gelesen wird streng (``load_stats``): höchstens ``MAX_BYTES``, ein JSON-Objekt, ``version`` ein kurzer Text aus Ziffern, Buchstaben und
``.+-``, jede Zahl eine ganze Zahl von 0 bis ``MAX_NUMBER`` (kein ``bool``, keine Kommazahl, kein Text), genau die Schlüssel von
``SHAPE`` (zusätzliche werden überhört). Fehlt etwas oder ist es ungültig – oder wirft das Lesen –, ist das Ergebnis ``None`` für die
ganze Datei, nie ein Fehler der Seite; sie lässt den Abschnitt dann weg.
"""
import json
import os
import re

import version as _version

STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(_version.__file__)), 'project_stats.json')
MAX_BYTES = 64 * 1024
MAX_NUMBER = 1_000_000_000
_VERSION = re.compile(r'[0-9A-Za-z.+-]{1,20}')

# Die Schlüssel je Gruppe – dieselben schreibt tools/project_stats.py
SHAPE = {
    'code': ('files', 'lines', 'code_lines'),
    'tests': ('files', 'lines', 'code_lines', 'functions', 'cases'),
    'data': ('files', 'lines', 'non_blank_lines'),
}


def format_count(number: int) -> str:
    """Ganzzahl mit Tausenderpunkt: ``1234567`` → ``1.234.567``."""
    return f'{number:,}'.replace(',', '.')


def _is_count(value) -> bool:
    return type(value) is int and 0 <= value <= MAX_NUMBER


def _checked(data) -> dict | None:
    """Die bekannten Schlüssel aus ``data`` oder ``None``, wenn einer fehlt oder ungültig ist."""
    if not isinstance(data, dict):
        return None
    version = data.get('version')
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        return None
    stats = {'version': version}
    for section, keys in SHAPE.items():
        values = data.get(section)
        if not isinstance(values, dict) or not all(_is_count(values.get(key)) for key in keys):
            return None
        stats[section] = {key: values[key] for key in keys}
    return stats


def load_stats(path=None) -> dict | None:
    """Die geprüften Zahlen ``{'version': …, 'code': {…}, 'tests': {…}, 'data': {…}}`` aus ``path`` (Standard: ``STATS_PATH``) oder
    ``None``. Jede Ausnahme beim Lesen ergibt ``None``."""
    try:
        with open(STATS_PATH if path is None else path, 'rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        return _checked(json.loads(raw.decode('utf-8')))
    except Exception:
        return None
