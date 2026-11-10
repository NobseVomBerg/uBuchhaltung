# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Wächter für das echte Datenverzeichnis <Projekt>/data.

Kein Test darf dort schreiben (siehe ``_isolated_data_root`` in conftest.py): auf
einem Entwicklungsrechner liegen dort die echten Bücher. Einzige Ausnahme ist der
Ordner ``data/tests/`` – dort legt pytest seine Arbeitsdaten ab (conftest.py,
pytest.ini).

``snapshot()`` hält den Zustand fest, ``diff()`` vergleicht zwei Zustände und
``message()`` macht daraus die Fehlermeldung. conftest.py nimmt den Schnappschuss
vor und nach dem ganzen Lauf; die Hilfen sind in test_test_isolation.py geprüft.
"""
import os

# Direkte Einträge von data/, die der Wächter übergeht (vgl. demo._IGNORED_ENTRIES
# und server/backup._EXCLUDED_DIRS).
IGNORED = ('tests',)

_LIMIT = 10          # so viele Pfade je Gruppe nennt die Meldung


def snapshot(root):
    """Zustand von ``root`` ohne die direkten Einträge aus ``IGNORED``:
    relativer Pfad -> (Größe, mtime_ns) bei Dateien, ``None`` bei Ordnern.
    Fehlt ``root``, ist das Ergebnis leer."""
    state = {}
    root = os.path.normpath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.normpath(dirpath) == root:
            dirnames[:] = [d for d in dirnames if d not in IGNORED]
            filenames = [f for f in filenames if f not in IGNORED]
        rel = os.path.relpath(dirpath, root)
        for name in dirnames:
            state[os.path.normpath(os.path.join(rel, name))] = None
        for name in filenames:
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:                  # während des Durchlaufs verschwunden
                continue
            state[os.path.normpath(os.path.join(rel, name))] = (st.st_size, st.st_mtime_ns)
    return state


def diff(before, after):
    """Unterschiede zweier Schnappschüsse: ``{'neu': [...], 'geändert': [...],
    'entfernt': [...]}`` mit sortierten Pfaden; leer, wenn alles gleich ist."""
    groups = {
        'neu': sorted(set(after) - set(before)),
        'geändert': sorted(p for p in set(before) & set(after) if before[p] != after[p]),
        'entfernt': sorted(set(before) - set(after)),
    }
    return {name: paths for name, paths in groups.items() if paths}


def message(changes, root):
    """Fehlermeldung zu einem nicht leeren ``diff()``-Ergebnis."""
    lines = [f"Tests haben das echte Datenverzeichnis verändert: {root}"]
    for name, paths in changes.items():
        shown = ', '.join(paths[:_LIMIT])
        more = f" … (+{len(paths) - _LIMIT})" if len(paths) > _LIMIT else ""
        lines.append(f"  {name}: {shown}{more}")
    lines.append(
        "Ursache: ein Test (oder neuer Code) schreibt an userctx.DATA_ROOT vorbei, "
        "z. B. über einen relativen Pfad 'data/…' oder eine eigene Pfadkonstante – "
        "Tests müssen unter tmp_path bleiben (_isolated_data_root in tests/conftest.py). "
        "Die Ursache lässt sich mit einzelnen Testdateien eingrenzen. "
        "Schreibt nebenbei die laufende App in dieses data/, ist es ein Fehlalarm.")
    return '\n'.join(lines)
