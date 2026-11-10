# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Shared pytest fixtures for uBuchhaltung tests.
"""
import os
import sys
import pytest

# Ensure the project root is on sys.path so `db`, `document_parser` etc. are importable.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import auth
import userctx
from db import Database
from tests import data_guard

# Das echte Datenverzeichnis der App – Tests schreiben dort nie (data/tests/ ausgenommen).
REAL_DATA = os.path.join(PROJECT_ROOT, 'data')
# Alles rund um Testläufe liegt in diesem einen Unterordner: tmp/ (Arbeitsdaten des
# letzten Laufs, siehe pytest_configure) und cache/ (pytest.ini).
TESTS_DIR = os.path.join(REAL_DATA, 'tests')


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Temporäre Testdaten (DBs, SQL-Protokolle, PDFs) des letzten Laufs liegen
    unter <Projekt>/data/tests/tmp/ – pytest leert den Ordner zu Beginn jedes Laufs.
    Daneben (data/tests/) darf Weiteres liegen, etwa der pytest-Cache (pytest.ini).
    Ein ausdrücklich übergebenes --basetemp gewinnt."""
    if config.option.basetemp:
        return
    try:
        os.makedirs(TESTS_DIR, exist_ok=True)     # pytest legt nur basetemp selbst an
    except OSError:
        return                                    # Projektordner schreibgeschützt: pytests Standard
    config.option.basetemp = os.path.join(TESTS_DIR, 'tmp')


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    """Jeder Test arbeitet in einem eigenen Datenverzeichnis unter tmp_path.

    Kein Test darf ins echte data/ des Projektordners schreiben: jeder
    protokollierte Datenbankvorgang (Database._log_sql → DocumentParser) legt
    sql_operations.log/.sql unter userctx.DATA_ROOT ab, standardmäßig './data' –
    auch wenn die Test-DB selbst unter tmp_path liegt. Auf einem Entwicklungsrechner
    sind das die echten Bücher; außerdem verweigert der Demo-Modus danach das
    Befüllen, weil data/ nicht mehr leer ist. Der Wächter _real_data_untouched
    schlägt an, falls doch etwas durchrutscht.

    auth.AUTH_DB ist eine eigene Pfadkonstante (./data/auth.db) und wandert mit.
    Das Verzeichnis wird nicht angelegt: Der Code legt es bei Bedarf selbst an,
    wie bei einer Erstinstallation. Tests mit eigenem Datenbereich (data_root in
    test_auth.py, test_http_guard.py, test_demo.py …) biegen DATA_ROOT danach
    selbst um und bleiben dabei unter tmp_path."""
    root = str(tmp_path / "data")
    monkeypatch.setattr(userctx, "DATA_ROOT", root)
    monkeypatch.setattr(auth, "AUTH_DB", os.path.join(root, "auth.db"))
    return root


@pytest.fixture(autouse=True)
def _neutral_app_mode(_isolated_data_root):
    """Tests modus-neutral halten: Der App-Modus verhält sich wie 'noch nicht
    gewählt' (kein Login), sofern ein Test den Modus nicht selbst über
    userctx.set_mode/DATA_ROOT setzt. Hängt von _isolated_data_root ab, denn der
    Modus-Cache ist je Konfig-Pfad geführt – der Pfad muss schon der des Tests sein."""
    userctx._mode_cache[userctx.config_path()] = None
    userctx.clear()
    yield
    userctx.clear()


@pytest.fixture(scope="session", autouse=True)
def _real_data_untouched():
    """Wächter über das echte <Projekt>/data (ohne data/tests/, siehe tests/data_guard.py):
    vergleicht dessen Zustand vor und nach dem ganzen Lauf. Hat sich etwas geändert, ist das
    ein Fehler beim Abbau des letzten Tests, mit den betroffenen Pfaden in der Meldung."""
    before = data_guard.snapshot(REAL_DATA)
    yield
    changes = data_guard.diff(before, data_guard.snapshot(REAL_DATA))
    if changes:
        pytest.fail(data_guard.message(changes, REAL_DATA), pytrace=False)


@pytest.fixture
def tmp_db(tmp_path):
    """Fresh Database instance backed by a temporary file (not in-memory so
    FOREIGN KEYS and migrations work identically to production)."""
    db_file = str(tmp_path / "test_buch.db")
    db = Database(db_name=db_file)
    yield db


@pytest.fixture
def db_with_coa(tmp_db):
    """Database pre-populated with a minimal SKR04 chart-of-accounts and one
    bank account so that WISO imports and EÜR tests have something to resolve
    COA references against."""
    db = tmp_db
    db.load_test_seed_data()
    yield db
