# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Demo-Modus: uBuchhaltung mit Beispieldaten ausprobieren.

Aktivierung: Umgebungsvariable ``UBUCHHALTUNG_DEMO=1``
(``docker run -e UBUCHHALTUNG_DEMO=1 …`` bzw. ``UBUCHHALTUNG_DEMO=1 python main.py``).

Die Demodaten gehören dem Benutzer ``demo`` und liegen **ausschließlich** unter
``data/users/demo/``. Andere Daten im Datenverzeichnis werden nie angefasst – echte
Bücher und Demo können nebeneinander liegen.

Ablauf beim Start (``prepare()``, in ``main.py`` nach ``layout.migrate()``):

* ``data/users/demo/`` fehlt oder ist leer **und** es gibt noch keinen Benutzer
  ``demo`` → Benutzer ``demo`` (kein Administrator) anlegen, Testdaten aus
  ``seed_data/test/`` und das Demo-Szenario laden und zuletzt einen Marker
  (``.demo``) schreiben. Gibt es noch keine ``config.json``, wird die Installation
  als eingerichtet markiert (``mode: multi``) – die Ersteinrichtung entfällt.
* ``data/users/demo/`` trägt den Marker (Neustart eines Demo-Containers) → keine
  erneute Befüllung, Demo läuft weiter.
* ``data/users/demo/`` enthält etwas anderes oder es gibt bereits einen Benutzer
  ``demo`` → es werden **niemals** Demodaten geladen. Warnung ins Log, normaler Start.

Solange ``UBUCHHALTUNG_DEMO`` gesetzt ist und das Demo-Verzeichnis den Marker trägt
(``is_active()``), wird jeder Besucher automatisch als ``demo`` angemeldet
(``active_user()``, Türsteher in ``server/app.py``): kein Administrator, also keine
Benutzerverwaltung und keine SQL-Konsole; andere Benutzer können sich so lange nicht
anmelden. Unter derselben Bedingung erscheint der Hinweis-Banner. Wer die Variable
versehentlich auf echten Daten setzt, bekommt also weder Demodaten noch einen
falschen Banner.

Ohne die Variable ist ``demo`` ein gewöhnlicher Benutzer ohne bekanntes Passwort –
ein Administrator kann ihm in der Benutzerverwaltung eines setzen oder ihn löschen
(sein Verzeichnis ``data/users/demo/`` bleibt dabei liegen).

NICHT FÜR ÖFFENTLICHES HOSTING: Im Demo-Modus gibt es keine Anmeldung, und jeder
Besucher darf alles in den Demodaten ändern. Der Demo-Modus ist zum lokalen
Ausprobieren gedacht, nicht für Instanzen, die aus dem Internet erreichbar sind.
"""
import os
import secrets

import auth
import userctx

ENV_VAR = "UBUCHHALTUNG_DEMO"
MARKER_FILE = ".demo"
USERNAME = "demo"

BANNER_TEXT = "Demo-Modus – Beispieldaten, nicht für die echte Buchhaltung"

# Ergebnisse von prepare()
DISABLED = "disabled"      # Variable nicht gesetzt
SEEDED = "seeded"          # Demo-Verzeichnis fehlte oder war leer, Demodaten geladen
RESUMED = "resumed"        # früheres Demo-Verzeichnis, unverändert weiterverwendet
REFUSED = "refused"        # bestehende Daten/Benutzer ‚demo‘, NICHTS geladen


def requested():
    """True, wenn ``UBUCHHALTUNG_DEMO`` gesetzt ist (1/true/yes/on)."""
    return os.environ.get(ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def demo_dir():
    return os.path.join(userctx.DATA_ROOT, userctx.USERS_DIR, USERNAME)


def _marker_path():
    return os.path.join(demo_dir(), MARKER_FILE)


def is_active():
    """True, wenn der Demo-Modus gewünscht ist und das Nutzerverzeichnis ``demo``
    Demodaten trägt (steuert Banner und automatische Anmeldung)."""
    return requested() and os.path.isfile(_marker_path())


def active_user():
    """Benutzername der Demo-Anmeldung oder None."""
    return USERNAME if is_active() else None


def _demo_dir_is_free():
    """True, wenn das Demo-Verzeichnis fehlt oder leer ist."""
    path = demo_dir()
    return not os.path.isdir(path) or not os.listdir(path)


def _demo_user_exists():
    # Ohne Datenverzeichnis gibt es auch keine auth.db und damit keinen Benutzer
    if not os.path.isdir(userctx.DATA_ROOT):
        return False
    auth.init_auth_db()
    return auth.user_exists(USERNAME)


def prepare(log=print):
    """Benutzer ``demo`` für den Demo-Modus vorbereiten. Liefert eines von
    ``DISABLED``/``SEEDED``/``RESUMED``/``REFUSED``.

    Muss VOR allem laufen, was ins Datenverzeichnis schreibt (Logs, DB)."""
    if not requested():
        return DISABLED

    log("Demo-Modus aktiv (UBUCHHALTUNG_DEMO) – nur zum Ausprobieren: jeder Besucher arbeitet "
        "ohne Anmeldung als Benutzer ‚demo‘. Nicht öffentlich hosten!")

    if os.path.isfile(_marker_path()):
        log(f"Demo-Modus: '{demo_dir()}' enthält bereits Demodaten – keine erneute Befüllung.")
        return RESUMED
    if not _demo_dir_is_free() or _demo_user_exists():
        log(f"WARNUNG: Demo-Modus angefordert, aber es gibt bereits einen Benutzer ‚demo‘ "
            f"bzw. Daten unter '{demo_dir()}'. Es werden KEINE Demodaten geladen; normaler Start.")
        return REFUSED

    _seed()
    log("Demo-Modus: Beispieldaten für den Benutzer ‚demo‘ geladen.")
    return SEEDED


def _seed():
    from db import Database
    os.makedirs(userctx.DATA_ROOT, exist_ok=True)
    if userctx.get_mode() is None:
        userctx.set_mode("multi")
    auth.init_auth_db()
    auth.create_user(USERNAME, secrets.token_urlsafe(24), is_admin=False, allow_reserved=True)
    os.makedirs(demo_dir(), exist_ok=True)
    userctx.set_user(USERNAME)            # Database() und Dateiablage zeigen auf data/users/demo/
    try:
        db = Database()
        db.load_test_seed_data(include_invoices=False)
        from demo_seed import load_demo_scenario
        load_demo_scenario(db)
        # Marker zuletzt: bricht das Laden ab, bleibt das Verzeichnis „belegt“ und
        # wird beim nächsten Start nicht blind erneut befüllt.
        with open(_marker_path(), "w", encoding="utf-8") as f:
            f.write("uBuchhaltung Demo-Daten (erfundene Beispieldaten)\n")
    finally:
        userctx.set_user(None)
