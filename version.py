# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zentrale Versionsinformation für uBuchhaltung (Single Source of Truth).

Versionsschema MAJOR.MINOR.PATCH (z. B. 2.0.0):

* MAJOR – große Releases / inkompatible Umbrüche (manuell erhöhen; dabei
  ``SCHEMA_STEP`` und ``PATCH`` auf 0 setzen).
* MINOR – zählt die Änderungen am Datenbankschema innerhalb des MAJOR: Bei JEDER
  Schemaänderung ``SCHEMA_STEP`` um 1 erhöhen. ``APP_VERSION`` zieht die mittlere
  Stelle automatisch daraus → DB-Änderung ⇒ MINOR steigt.
* PATCH – sonstige Änderungen / Bugfixes ohne DB-Auswirkung (manuell erhöhen;
  bei einer DB-Änderung auf 0 zurücksetzen).

``SCHEMA_VERSION`` ist die Zahl, die beim DB-Aufbau als ``PRAGMA user_version`` in
jede Datenbank geschrieben wird (siehe ``db/schema.py``): ``MAJOR × 1000 +
SCHEMA_STEP``, z. B. 2000 für 2.0.x und 2001 für 2.1.x. Sie steigt mit jeder
Schemaänderung und mit jedem MAJOR-Sprung; daran erkennt die Migration den Stand
einer Datenbank. Datenbanken aus Version 1.x tragen die Zahlen 0 bis 6. Eine
Datenbank mit einer höheren Zahl als das Programm stammt von einer neueren
Version und wird nicht geöffnet (``db.SchemaTooNewError``).
"""

MAJOR = 2
SCHEMA_STEP = 0      # +1 bei jeder DB-Schema-Änderung (= mittlere Versionsstelle); höchstens 999
PATCH = 3            # +1 bei sonstigen Änderungen ohne DB-Bezug

SCHEMA_VERSION = MAJOR * 1000 + SCHEMA_STEP   # steht als PRAGMA user_version in der Datenbank
APP_VERSION = f"{MAJOR}.{SCHEMA_STEP}.{PATCH}"


def describe_schema_version(number):
    """Lesbare Angabe zu einer ``PRAGMA user_version``-Zahl, z. B. ``'2.1'``."""
    number = int(number)
    if number < 1000:
        return f"1.x (Schemastand {number})"
    return f"{number // 1000}.{number % 1000}"
