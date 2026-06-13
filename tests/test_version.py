"""Versionsnummer (TODO #2): Format-Check und Kopplung MINOR ↔ DB-Schema-Version."""
import re
import sqlite3

import version


def test_app_version_format():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.APP_VERSION), version.APP_VERSION


def test_minor_equals_schema_version():
    # Die mittlere Versionsstelle ist an die DB-Schema-Version gekoppelt.
    minor = int(version.APP_VERSION.split(".")[1])
    assert minor == version.SCHEMA_VERSION


def test_db_user_version_matches_schema_version(tmp_db):
    # Frisch initialisierte DB trägt SCHEMA_VERSION als PRAGMA user_version.
    con = sqlite3.connect(tmp_db.db_name)
    user_version = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    assert user_version == version.SCHEMA_VERSION


def test_migration_upgrades_old_db(tmp_path):
    """Eine bestehende v1-DB (Trips ohne Tacho/Beleg-Spalten) wird beim Öffnen
    automatisch migriert und auf die aktuelle Schema-Version gestempelt."""
    from db import Database
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
