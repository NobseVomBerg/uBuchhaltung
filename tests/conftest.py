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
    conn = db._get_connection()
    cur = conn.cursor()

    # Minimal SKR04 accounts needed by tests
    accounts = [
        (4, 1200, 'Bank', 'Bankguthaben', 1),
        (4, 1000, 'Kasse', 'Barkasse', 1),
        (4, 4400, 'Erlöse', 'Umsatzerlöse 19%', 1),
        (4, 4640, 'Erlöse steuerfrei', 'Steuerfreie Umsätze', 1),
        (4, 4845, 'Sonstige Erlöse', 'Sonstige betriebliche Erlöse', 1),
        (4, 6815, 'Betriebsbedarf', 'Betriebsausgaben allg.', 1),
        (4, 4300, 'Wareneingang 19%', 'Wareneinkauf', 1),
        (4, 4405, 'Wareneingang 19% (intern)', 'Umbuchung von 4400', 1),
        (4, 3806, 'Umsatzsteuer 19%', 'Umsatzsteuer', 1),
        (4, 1401, 'Vorsteuer 7%', 'Vorsteuer ermäßigt', 1),
        (4, 1406, 'Vorsteuer 19%', 'Vorsteuer Regelsteuersatz', 1),
    ]
    cur.executemany(
        'INSERT OR IGNORE INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard) VALUES (?,?,?,?,?)',
        accounts,
    )

    # One bank account mapped to SKR04 1200
    cur.execute(
        'INSERT OR IGNORE INTO Accounts (Name, Owner, Number, BIC, BankName, SKRAccount) VALUES (?,?,?,?,?,?)',
        ('Testbank', '', 'DE89370400440532013000', 'TESTDE1X', 'Testbank AG', 1200),
    )
    conn.commit()
    conn.close()
    yield db
