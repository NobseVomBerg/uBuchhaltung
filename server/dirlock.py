# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Das Wiederherstellen läuft allein: Sperre je Datenverzeichnis.

Während ``backup.restore_backup`` das Datenverzeichnis eines Benutzers ersetzt, bearbeitet der Server keine andere Anfrage dieses
Benutzers (sie läse halb ersetzte Dateien oder schriebe in eine Datenbank, die gleich ersetzt wird). Umgekehrt beginnt das
Wiederherstellen erst, wenn keine andere Anfrage dieses Benutzers mehr läuft.

* ``shared``: Jede Anfrage eines angemeldeten Benutzers läuft unter der Lesesperre seines Datenverzeichnisses
  (``SimpleWebServer._run_locked``). Sie wartet nie: Läuft für das Verzeichnis eine Wiederherstellung – oder wartet sie auf
  laufende Anfragen –, gibt es sofort ``DirBusy(RESTORE_RUNNING)``, und der Server antwortet mit 503 und ``Retry-After``. So
  hungert eine Kette neuer Anfragen das Wiederherstellen nicht aus.
* ``exclusive``: ``handlers.handle_backup_restore`` nimmt die ausschließliche Sperre. Ab sofort laufen keine neuen Anfragen mehr
  an; auf die laufenden wartet sie höchstens ``RESTORE_WAIT_SECONDS``. Läuft die Zeit ab, wird abgelehnt
  (``DirBusy(WAIT_REFUSED)``) und am Datenverzeichnis ist nichts geändert. Die Anfrage, die selbst wiederherstellt, hält ihre
  eigene Lesesperre und wartet nicht auf sich selbst.
* Reihenfolge der Sperren: ``dirlock`` außen, ``backup._exclusive`` innen – nie umgekehrt.
* Ob es das Archiv gibt, prüft der Handler VOR der ausschließlichen Sperre: Ein Versuch, der nie beginnen kann, hält keine
  Anfrage auf. Im Demo-Modus (alle Besucher teilen ein Verzeichnis) wartet das Wiederherstellen nur ``DEMO_WAIT_SECONDS`` und
  ruht nach einer Ablehnung ``DEMO_COOLDOWN_SECONDS``.

Schlüssel ist der aufgelöste Pfad des Datenverzeichnisses (wie bei ``backup._exclusive``): Zwei Schreibweisen desselben
Verzeichnisses sind dieselbe Sperre, verschiedene Verzeichnisse (verschiedene Benutzer) sperren einander nicht.

Nicht erfasst – die Sperre gilt innerhalb EINES Serverprozesses und nur für Anfragen: ein zweiter Prozess auf demselben
Datenordner; Hintergrundarbeit ohne Anfrage (das Komprimieren rotierter Protokolle in ``document_parser.py``);
``layout.migrate()`` beim Start; Dateizugriffe von Hand.
"""
import os
import threading
import time
from contextlib import contextmanager

RESTORE_WAIT_SECONDS = 30        # so lange wartet das Wiederherstellen auf laufende Anfragen
RETRY_AFTER_SECONDS = 15         # Retry-After der 503 während einer Wiederherstellung
# Im Demo-Modus teilen sich alle Besucher ein Datenverzeichnis: Dort wartet ein Wiederherstellen nur kurz, und nach einer
# Ablehnung beginnt für eine Weile kein neuer Versuch – sonst hielte eine Folge von Versuchen, die nie beginnen können, alle
# anderen Besucher mit 503 auf.
DEMO_WAIT_SECONDS = 2
DEMO_COOLDOWN_SECONDS = 60

RESTORE_RUNNING = 'Die Daten werden gerade wiederhergestellt – bitte in einigen Sekunden erneut versuchen.'
WAIT_REFUSED = ('Die Wiederherstellung wurde nicht begonnen: Es laufen noch andere Anfragen für dieses '
                'Datenverzeichnis (zum Beispiel ein Import, ein Upload oder eine Sicherung). '
                'Bitte deren Ende abwarten und erneut versuchen. Am Datenverzeichnis wurde nichts geändert.')
ALREADY_RESTORING = ('Für dieses Datenverzeichnis läuft bereits eine Wiederherstellung – '
                     'bitte deren Ergebnis abwarten.')
COOLING_DOWN = ('Eine Wiederherstellung wurde gerade abgelehnt, weil andere Anfragen für dieses Datenverzeichnis liefen – '
                'bitte in einer Minute erneut versuchen. Am Datenverzeichnis wurde nichts geändert.')


class DirBusy(Exception):
    """Klartext für den Nutzer (einer der Sätze oben)."""


class _State:
    """Zustand eines Datenverzeichnisses: ``readers`` = laufende Anfragen, ``restoring`` = eine Wiederherstellung läuft oder
    wartet auf die Anfragen, ``owner`` = der Thread, der sie führt, ``refused_until`` = bis dahin (``time.monotonic``) beginnt nach
    einer Ablehnung mit Abkühlzeit kein neuer Versuch."""

    def __init__(self):
        self.cond = threading.Condition()
        self.readers = 0
        self.restoring = False
        self.owner = None
        self.refused_until = 0.0


# Zustand je Datenverzeichnis (Schlüssel: aufgelöster Pfad); Einträge werden nie entfernt, wie in backup._locks.
_states: dict[str, _State] = {}
_states_guard = threading.Lock()

# Die Lesesperren des eigenen Threads je Verzeichnis: Die Anfrage, die selbst wiederherstellt, wartet nicht auf ihre eigene.
_mine = threading.local()


def _state_of(user_dir):
    key = os.path.realpath(user_dir)
    with _states_guard:
        state = _states.get(key)
        if state is None:
            state = _states[key] = _State()
    return key, state


def _own_holds():
    if not hasattr(_mine, 'holds'):
        _mine.holds = {}
    return _mine.holds


@contextmanager
def shared(user_dir):
    """Eine Anfrage für ``user_dir`` laufen lassen (Lesesperre). Läuft dort eine Wiederherstellung, ``DirBusy(RESTORE_RUNNING)`` –
    sofort, ohne zu warten. Wer die Sperre schon hält (verschachtelt) oder selbst wiederherstellt, darf weiter."""
    key, state = _state_of(user_dir)
    holds = _own_holds()
    with state.cond:
        if state.restoring and state.owner != threading.get_ident() and not holds.get(key):
            raise DirBusy(RESTORE_RUNNING)
        state.readers += 1
        holds[key] = holds.get(key, 0) + 1
    try:
        yield
    finally:
        with state.cond:
            state.readers -= 1
            holds[key] -= 1
            if not holds[key]:
                del holds[key]
            state.cond.notify_all()


@contextmanager
def exclusive(user_dir, timeout=None, cooldown=0):
    """``user_dir`` allein nutzen (Wiederherstellen): Ab sofort laufen keine neuen Anfragen an, dann wartet sie höchstens ``timeout``
    Sekunden (Standard ``RESTORE_WAIT_SECONDS``, bei jedem Aufruf neu gelesen) auf die laufenden – die eigenen Lesesperren dieses
    Threads zählen nicht. Läuft dort schon eine Wiederherstellung, ``DirBusy(ALREADY_RESTORING)``; läuft die Zeit ab,
    ``DirBusy(WAIT_REFUSED)``, und die Sperre ist wieder frei. ``cooldown`` (Sekunden): So lange nach einer solchen Ablehnung wird
    ein neuer Versuch sofort abgelehnt (``DirBusy(COOLING_DOWN)``), ohne dass er neue Anfragen aufhält."""
    wait = RESTORE_WAIT_SECONDS if timeout is None else timeout
    key, state = _state_of(user_dir)
    mine = _own_holds().get(key, 0)
    with state.cond:
        if state.restoring:
            raise DirBusy(ALREADY_RESTORING)           # nicht in das try: Das Kennzeichen gehört dem anderen Thread
        if time.monotonic() < state.refused_until:
            raise DirBusy(COOLING_DOWN)                # ohne das Kennzeichen zu setzen: Anfragen laufen weiter
        state.restoring, state.owner = True, threading.get_ident()
    try:
        with state.cond:
            if not state.cond.wait_for(lambda: state.readers == mine, wait):
                if cooldown > 0:
                    state.refused_until = time.monotonic() + cooldown
                raise DirBusy(WAIT_REFUSED)
        yield
    finally:
        with state.cond:
            state.restoring, state.owner = False, None
            state.cond.notify_all()
