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
