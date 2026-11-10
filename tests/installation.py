# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen, um Installationsstände unter ``userctx.DATA_ROOT`` aufzubauen und zu vergleichen.

DATA_ROOT und ``auth.AUTH_DB`` liegen je Test unter ``tmp_path`` (``_isolated_data_root``
in conftest.py). Benutzernamen und Passwörter sind erfundene Testwerte.
"""
import contextlib
import hashlib
import json
import os
import sqlite3

import auth
import userctx
from db import Database, _CoreMixin

ADMIN = "admin"
PASSWORD = "geheim123"        # erfundenes Test-Passwort (mindestens 6 Zeichen)


def forget_config_cache():
    """config.json beim nächsten Zugriff neu lesen. ``_neutral_app_mode`` (conftest.py) hält
    den Modus je Test als „nicht eingerichtet“ im Cache – eine von Hand geschriebene
    config.json sähe ``get_mode()`` sonst nie."""
    userctx._mode_cache.clear()
    userctx._auto_login_cache.clear()


def write_config(data):
    """config.json von Hand schreiben (Dict oder Rohtext) – wie ein Betreiber es täte."""
    os.makedirs(userctx.DATA_ROOT, exist_ok=True)
    with open(userctx.config_path(), "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    forget_config_cache()


def read_config():
    """Inhalt der config.json als Dict, direkt aus der Datei."""
    with open(userctx.config_path(), encoding="utf-8") as f:
        return json.load(f)


def set_up(admin=ADMIN, auto_login=True, users=(), password=PASSWORD):
    """Eingerichtete Installation: ``mode: multi``, ein Administrator und optional weitere
    Benutzer ``(Name, ist_Administrator)``. Mit ``auto_login`` wird der Administrator
    automatisch angemeldet (heutiger Betrieb für eine Person)."""
    userctx.set_mode("multi")
    auth.create_user(admin, password, is_admin=True)
    for name, is_admin in users:
        auth.create_user(name, password, is_admin=is_admin)
    if auto_login:
        userctx.set_auto_login(admin)


def open_db(username):
    """Datenbank des Benutzers (``users/<Name>/buch.db``). Der Benutzer-Kontext gilt nur für
    das Öffnen, danach ist er wieder leer."""
    userctx.set_user(username)
    try:
        return Database()
    finally:
        userctx.clear()


def file_digest(path):
    """sha256 einer Datei (zum Nachweis, dass sie byteweise unverändert blieb)."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def user_version(db_path):
    """``PRAGMA user_version`` einer Datenbankdatei, ohne sie über ``Database`` zu öffnen."""
    with contextlib.closing(sqlite3.connect(db_path)) as con:
        return con.execute("PRAGMA user_version").fetchone()[0]


def dump(db_path):
    """Inhalt einer Datenbank (Schema und Zeilen) als SQL-Text-Liste, zum Vergleich über iterdump()."""
    with contextlib.closing(sqlite3.connect(db_path)) as con:
        return list(con.iterdump())


def stamp_user_version(db_path, number):
    """``PRAGMA user_version`` einer vorhandenen Datenbank setzen (als hätte ein anderes Programm sie
    zuletzt geöffnet) und sie für diesen Prozess als „noch nicht geöffnet“ vermerken – sonst prüft
    ``Database(...)`` den Stand nicht erneut."""
    with contextlib.closing(sqlite3.connect(db_path)) as con:
        con.execute(f"PRAGMA user_version = {int(number)}")
        con.commit()
    _CoreMixin._initialized_dbs.discard(os.path.abspath(db_path))


def session_count():
    """Anzahl der Sitzungen in der auth.db (0, wenn es sie noch nicht gibt)."""
    if not os.path.exists(auth.AUTH_DB):
        return 0
    with contextlib.closing(sqlite3.connect(auth.AUTH_DB)) as con:
        return con.execute("SELECT COUNT(*) FROM Sessions").fetchone()[0]


def tree(root):
    """Inhalt unterhalb von ``root``: ``{relativer Pfad: sha256 der Datei bzw. None bei
    Ordnern}``. Fehlt ``root``, ist das Ergebnis leer."""
    root = os.path.normpath(str(root))
    state = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        for name in dirnames:
            state[os.path.normpath(os.path.join(rel, name))] = None
        for name in filenames:
            with open(os.path.join(dirpath, name), "rb") as f:
                state[os.path.normpath(os.path.join(rel, name))] = hashlib.sha256(f.read()).hexdigest()
    return state
