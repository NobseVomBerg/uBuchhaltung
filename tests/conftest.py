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

from db import Database


@pytest.fixture(autouse=True)
def _neutral_app_mode():
    """Tests modus-neutral halten: unabhängig von einer real vorhandenen
    data/config.json (Entwickler-Installation) verhält sich der App-Modus wie
    'noch nicht gewählt' (kein Login), sofern ein Test den Modus nicht selbst
    über userctx.set_mode/DATA_ROOT setzt."""
    import userctx
    userctx._mode_cache[userctx.config_path()] = None
    userctx.clear()
    yield
    userctx.clear()


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
