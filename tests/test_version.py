# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Versionsnummer (TODO #2): Format, Aufbau aus MAJOR.SCHEMA_STEP.PATCH, die Schema-Zahl in der
Datenbank (``PRAGMA user_version`` = MAJOR × 1000 + SCHEMA_STEP) und das Umstempeln älterer
Datenbanken. Die Sperre gegen Datenbanken aus einer neueren Version prüft test_schema_too_new.py."""
import contextlib
import re
import sqlite3

import pytest

import version
from db import Database
from tests import installation


def test_app_version_format():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.APP_VERSION), version.APP_VERSION


def test_app_version_is_built_from_its_parts():
    assert version.APP_VERSION == f"{version.MAJOR}.{version.SCHEMA_STEP}.{version.PATCH}"


def test_middle_digit_is_the_schema_step():
    # Die mittlere Versionsstelle zählt die Schemaänderungen innerhalb des MAJOR.
    minor = int(version.APP_VERSION.split(".")[1])
    assert minor == version.SCHEMA_STEP


def test_schema_version_is_major_times_1000_plus_step():
    assert version.SCHEMA_VERSION == version.MAJOR * 1000 + version.SCHEMA_STEP


def test_schema_step_stays_within_its_three_digits():
    assert 0 <= version.SCHEMA_STEP <= 999


def test_schema_version_lies_above_every_1x_stamp():
    # Datenbanken aus 1.x tragen 0 bis 6; die Zahl der 2.x-Programme darf nie damit verwechselt werden
    assert version.SCHEMA_VERSION >= 1000


@pytest.mark.parametrize("number, text", [
    pytest.param(0, "1.x (Schemastand 0)", id="1.x-Stand-0"),
    pytest.param(6, "1.x (Schemastand 6)", id="1.x-Stand-6"),
    pytest.param(999, "1.x (Schemastand 999)", id="oberhalb-der-1.x-Staende"),
    pytest.param(1000, "1.0", id="Beginn-von-MAJOR-1"),
    pytest.param(2000, "2.0", id="2.0"),
    pytest.param(2001, "2.1", id="2.1"),
    pytest.param(2010, "2.10", id="zweistelliger-Schemastand"),
    pytest.param(2999, "2.999", id="letzter-Stand-von-MAJOR-2"),
    pytest.param(3000, "3.0", id="MAJOR-3"),
    pytest.param("2001", "2.1", id="als-Text"),
])
def test_describe_schema_version(number, text):
    assert version.describe_schema_version(number) == text


def _evaluate_version_module(**constants):
    """version.py mit anderen Werten für MAJOR, SCHEMA_STEP und PATCH auswerten: Die Regeln gelten für
    jede Zahl, nicht nur für die heutigen."""
    with open(version.__file__, encoding="utf-8") as f:
        source = f.read()
    for name, value in constants.items():
        source, count = re.subn(rf"^{name} = \d+", f"{name} = {value}", source, flags=re.M)
        assert count == 1, f"{name} steht nicht genau einmal in version.py"
    namespace = {}
    exec(compile(source, version.__file__, "exec"), namespace)
    return namespace


@pytest.mark.parametrize("major, step, patch", [(2, 0, 0), (2, 7, 4), (2, 999, 12), (3, 0, 0), (3, 12, 1)])
def test_the_numbering_rule_holds_for_other_numbers(major, step, patch):
    ns = _evaluate_version_module(MAJOR=major, SCHEMA_STEP=step, PATCH=patch)
    assert ns["APP_VERSION"] == f"{major}.{step}.{patch}"
    assert ns["SCHEMA_VERSION"] == major * 1000 + step
    assert ns["describe_schema_version"](ns["SCHEMA_VERSION"]) == f"{major}.{step}"


def test_describe_schema_version_names_the_current_state():
    assert version.describe_schema_version(version.SCHEMA_VERSION) == \
        f"{version.MAJOR}.{version.SCHEMA_STEP}"


def test_db_user_version_matches_schema_version(tmp_db):
    # Frisch initialisierte DB trägt SCHEMA_VERSION als PRAGMA user_version.
    assert installation.user_version(tmp_db.db_name) == version.SCHEMA_VERSION
    assert installation.user_version(tmp_db.db_name) == version.MAJOR * 1000 + version.SCHEMA_STEP


def test_migration_upgrades_old_db(tmp_path):
    """Eine bestehende v1-DB (Trips ohne Tacho/Beleg-Spalten) wird beim Öffnen
    automatisch migriert und auf die aktuelle Schema-Version gestempelt."""
    p = str(tmp_path / "old.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE Invoices (ID INTEGER PRIMARY KEY)")   # markiert 'bestehende DB'
    con.execute("CREATE TABLE Trips (ID INTEGER PRIMARY KEY, DriverID INTEGER, "
                "StartDate DATE, Destination TEXT, DistanceKm INTEGER)")
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()

    Database(db_name=p)   # löst initialize_database + Migration aus

    con = sqlite3.connect(p)
    cols = [r[1] for r in con.execute("PRAGMA table_info(Trips)").fetchall()]
    uv = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    assert {"StartKm", "EndKm", "DocumentID"} <= set(cols)
    assert uv == version.SCHEMA_VERSION


def _schema(db_path):
    """Tabellen, Indizes und Spalten (sqlite_master), unabhängig von den Zeilen."""
    with contextlib.closing(sqlite3.connect(db_path)) as con:
        return sorted(con.execute("SELECT type, name, sql FROM sqlite_master"))


@pytest.mark.parametrize("old_state", range(0, 7))
def test_a_1x_database_is_restamped_on_opening(db_with_coa, old_state):
    """Datenbanken aus Version 1.x tragen die Zahlen 0 bis 6; beim Öffnen werden sie auf
    SCHEMA_VERSION gehoben (die Migration vergleicht weiter aufsteigend)."""
    path = db_with_coa.db_name
    schema = _schema(path)
    installation.stamp_user_version(path, old_state)
    assert installation.user_version(path) == old_state

    Database(db_name=path)

    assert installation.user_version(path) == version.SCHEMA_VERSION
    assert _schema(path) == schema


@pytest.mark.parametrize("old_state", [4, 5, 6])
def test_a_database_from_the_last_1x_states_keeps_its_content(db_with_coa, old_state):
    """Ab Stand 4 verändert keine Migration Zeilen einer aktuellen Datenbank: Umstempeln ohne
    inhaltliche Änderung (Vergleich über iterdump). Die Stände davor markieren Spiegelbuchungen
    (v4, siehe test_bank_entry_sync.py) und werden mit den Migrationstests abgedeckt."""
    path = db_with_coa.db_name
    before = installation.dump(path)
    assert any("INSERT INTO" in line for line in before)            # die Testdaten sind da
    installation.stamp_user_version(path, old_state)

    Database(db_name=path)

    assert installation.user_version(path) == version.SCHEMA_VERSION
    assert installation.dump(path) == before


def _marked_mirrors(db_path):
    with contextlib.closing(sqlite3.connect(db_path)) as con:
        return con.execute("SELECT COUNT(*) FROM Bookings WHERE AutoMirror = 1").fetchone()[0]


def test_opening_a_database_below_state_4_marks_its_mirror_bookings(db_with_coa):
    """Das Umstempeln läuft über die Migration: Vor Stand 4 (v4) werden exakte Spiegel einer
    Bankbuchung nachträglich markiert, ab Stand 4 bleiben die Zeilen, wie sie sind."""
    path = db_with_coa.db_name
    assert _marked_mirrors(path) == 0
    installation.stamp_user_version(path, 4)
    Database(db_name=path)
    assert _marked_mirrors(path) == 0
    installation.stamp_user_version(path, 3)
    Database(db_name=path)
    assert _marked_mirrors(path) > 0                                # die Testdaten enthalten Spiegel


def test_a_database_at_the_programs_own_state_stays_as_it_is(db_with_coa):
    path = db_with_coa.db_name
    before = installation.dump(path)
    installation.stamp_user_version(path, version.SCHEMA_VERSION)
    Database(db_name=path)
    assert installation.user_version(path) == version.SCHEMA_VERSION
    assert installation.dump(path) == before
