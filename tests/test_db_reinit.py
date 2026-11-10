# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Datenbank gilt nur als eingerichtet, solange die Datei selbst das noch sagt.

``Database`` merkt sich je Pfad, dass ``initialize_database`` schon gelaufen ist. Der
Pfad allein reicht nicht: Die Datei kann seither ersetzt worden sein – eine
zurückgespielte Sicherung eines älteren Standes, eine neu angelegte Datei am selben
Ort (pytest 8.x vergibt den Namen eines gelöschten Testverzeichnisses erneut; daran
scheiterten Tests mit „no such table“). Maßgeblich ist deshalb zusätzlich die
Schema-Zahl in der Datei.
"""
import os
import sqlite3

import pytest

import version
from db import Database, SchemaTooNewError, _CoreMixin


def _user_version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _stamp(path, number):
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {int(number)}")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "buch.db")
    Database(db_name=path)
    assert os.path.abspath(path) in _CoreMixin._initialized_dbs
    return path


def test_a_new_file_at_a_known_path_gets_its_tables(db_path):
    os.remove(db_path)
    db = Database(db_name=db_path)                       # derselbe Pfad, aber eine leere neue Datei
    assert db.get_table_statistics()                     # die Tabellen sind wieder da
    assert _user_version(db_path) == version.SCHEMA_VERSION


def test_an_older_file_swapped_in_is_migrated_without_a_restart(db_path):
    """Wie nach dem Zurückspielen einer Sicherung aus einer älteren Version."""
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE Bookings DROP COLUMN SourceGroup")     # Stand vor Schema 6
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    Database(db_name=db_path)

    assert _user_version(db_path) == version.SCHEMA_VERSION
    conn = sqlite3.connect(db_path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(Bookings)")]
    conn.close()
    assert "SourceGroup" in columns


def test_a_newer_file_swapped_in_is_refused_although_the_path_is_known(db_path):
    _stamp(db_path, version.SCHEMA_VERSION + 1)
    with pytest.raises(SchemaTooNewError):
        Database(db_name=db_path)
    assert _user_version(db_path) == version.SCHEMA_VERSION + 1      # nicht zurückgestempelt


def test_an_unchanged_database_is_not_initialised_again(db_path, monkeypatch):
    calls = []
    monkeypatch.setattr(Database, "initialize_database", lambda self: calls.append(self.db_name))
    for _ in range(3):
        Database(db_name=db_path)
    assert calls == []
