"""Zentrale Versionsinformation für PyBuch (Single Source of Truth).

Versionsschema MAJOR.MINOR.PATCH (z. B. 1.0.0):

* MAJOR – große Releases / inkompatible Umbrüche (manuell erhöhen).
* MINOR – an die DB-Schema-Version gekoppelt: Bei JEDER Änderung am
  Datenbankschema ``SCHEMA_VERSION`` um 1 erhöhen. ``APP_VERSION`` zieht die
  mittlere Stelle automatisch daraus → DB-Änderung ⇒ MINOR steigt.
* PATCH – sonstige Änderungen / Bugfixes ohne DB-Auswirkung (manuell erhöhen;
  bei einer DB-Änderung sinnvollerweise auf 0 zurücksetzen).

``SCHEMA_VERSION`` wird zusätzlich beim DB-Aufbau via ``PRAGMA user_version``
in die Datenbank geschrieben (siehe ``db/schema.py``), damit die DB selbst ihre
Schema-Version kennt.
"""

MAJOR = 1
SCHEMA_VERSION = 2   # +1 bei jeder DB-Schema-Änderung (= mittlere Versionsstelle)
PATCH = 4            # +1 bei sonstigen Änderungen ohne DB-Bezug

APP_VERSION = f"{MAJOR}.{SCHEMA_VERSION}.{PATCH}"
