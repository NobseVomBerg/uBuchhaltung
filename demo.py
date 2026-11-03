# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Demo-Modus: uBuchhaltung mit Beispieldaten ausprobieren.

Aktivierung: Umgebungsvariable ``UBUCHHALTUNG_DEMO=1``
(``docker run -e UBUCHHALTUNG_DEMO=1 …`` bzw. ``UBUCHHALTUNG_DEMO=1 python main.py``).

Ablauf beim Start (``prepare()``, vor allem anderen in ``main.py``):

* Datenverzeichnis fehlt oder ist leer → Einzelbenutzer-Modus einrichten
  (Ersteinrichtung entfällt), Datenbank anlegen, Testdaten aus
  ``seed_data/test/`` laden und einen Marker (``.demo``) schreiben.
* Datenverzeichnis enthält bereits irgendetwas (Datenbank, Konfiguration,
  Logs …) und ist **kein** früheres Demo-Verzeichnis → es werden **niemals**
  Demodaten hineingeladen. Warnung ins Log, normaler Start.
* Datenverzeichnis trägt den Demo-Marker (Neustart eines Demo-Containers) →
  keine erneute Befüllung, Demo läuft weiter.

Der Hinweis-Banner erscheint nur, wenn der Demo-Modus gewünscht ist **und** das
Datenverzeichnis den Marker trägt (``is_active()``). Wer die Variable versehentlich
auf echten Daten setzt, bekommt also weder Demodaten noch einen falschen Banner.

NICHT FÜR ÖFFENTLICHES HOSTING: Im Einzelbenutzer-Modus gibt es keine Anmeldung,
und die SQL-Konsole (Sonstiges) erlaubt beliebige Zugriffe auf die Datenbank.
Der Demo-Modus ist zum lokalen Ausprobieren gedacht, nicht für Instanzen, die
aus dem Internet erreichbar sind.
"""
import os

import userctx

ENV_VAR = "UBUCHHALTUNG_DEMO"
MARKER_FILE = ".demo"

BANNER_TEXT = "Demo-Modus – Beispieldaten, nicht für die echte Buchhaltung"

# Ergebnisse von prepare()
DISABLED = "disabled"      # Variable nicht gesetzt
SEEDED = "seeded"          # leeres Verzeichnis, Demodaten geladen
RESUMED = "resumed"        # früheres Demo-Verzeichnis, unverändert weiterverwendet
REFUSED = "refused"        # bestehende Daten, NICHTS geladen

# Einträge, die ein sonst leeres Verzeichnis nicht „belegt“ machen
# (z. B. von Dateisystem/Volume-Treibern angelegt).
_IGNORED_ENTRIES = {"lost+found"}


def requested():
    """True, wenn ``UBUCHHALTUNG_DEMO`` gesetzt ist (1/true/yes/on)."""
    return os.environ.get(ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def _marker_path():
    return os.path.join(userctx.DATA_ROOT, MARKER_FILE)


def is_active():
    """True, wenn der Demo-Modus gewünscht ist und das Datenverzeichnis ein
    Demo-Verzeichnis ist (steuert den Banner)."""
    return requested() and os.path.isfile(_marker_path())


def _is_empty(path):
    if not os.path.isdir(path):
        return True
    return not [e for e in os.listdir(path) if e not in _IGNORED_ENTRIES]


def prepare(log=print):
    """Datenverzeichnis für den Demo-Modus vorbereiten. Liefert eines von
    ``DISABLED``/``SEEDED``/``RESUMED``/``REFUSED``.

    Muss VOR allem laufen, was ins Datenverzeichnis schreibt (Logs, DB)."""
    if not requested():
        return DISABLED

    log("Demo-Modus aktiv (UBUCHHALTUNG_DEMO) – nur zum lokalen Ausprobieren: "
        "keine Anmeldung, SQL-Konsole offen. Nicht öffentlich hosten!")

    root = userctx.DATA_ROOT
    if os.path.isfile(_marker_path()):
        log(f"Demo-Modus: Datenverzeichnis '{root}' enthält bereits Demodaten – "
            "keine erneute Befüllung.")
        return RESUMED
    if not _is_empty(root):
        log(f"WARNUNG: Demo-Modus angefordert, aber Datenverzeichnis '{root}' ist nicht "
            "leer. Es werden KEINE Demodaten geladen; Start mit den vorhandenen Daten. "
            "(Für eine Demo ein leeres Datenverzeichnis verwenden.)")
        return REFUSED

    _seed(root)
    log(f"Demo-Modus: Beispieldaten in '{root}' geladen.")
    return SEEDED


def _seed(root):
    from db import Database
    os.makedirs(root, exist_ok=True)
    userctx.set_mode("single")
    from server.app import _ensure_directories
    _ensure_directories()
    Database().load_test_seed_data()
    # Marker zuletzt: bricht das Laden ab, bleibt das Verzeichnis „belegt“ und
    # wird beim nächsten Start nicht blind erneut befüllt.
    with open(_marker_path(), "w", encoding="utf-8") as f:
        f.write("uBuchhaltung Demo-Daten (erfundene Beispieldaten)\n")
