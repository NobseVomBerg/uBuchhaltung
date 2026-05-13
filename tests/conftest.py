"""
Shared pytest fixtures for PyBuch tests.
"""
import os
import sys
import pytest

# Ensure the project root is on sys.path so `db`, `document_parser` etc. are importable.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from db import Database


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
