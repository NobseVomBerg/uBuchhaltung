"""
Tests for get_euer_data – EÜR (Einnahmenüberschussrechnung) computation.

Coverage:
- Income: positive amounts appear on income SKR account (Netto)
- Expense: negative amounts appear on expense SKR account (Netto)
- USt on income → virtual account 3806
- Vorsteuer 19% on expense → virtual account 1406
- Vorsteuer 7% on expense → virtual account 1401
- Doppik mirror entries (both sides liquid) are excluded
- Empty result when no bookings in date range
"""
import pytest


def _get_acct_id(db, name: str) -> int:
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ID FROM Accounts WHERE Name=?", (name,))
    row = cur.fetchone()
    conn.close()
    return row[0]


def _get_coa_id(db, account_number: int) -> int:
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ID FROM ChartOfAccounts WHERE AccountNumber=?", (account_number,))
    row = cur.fetchone()
    conn.close()
    assert row is not None, f"COA {account_number} not found – check db_with_coa fixture"
    return row[0]


def _euer_map(db, date_from, date_to, account_ids=None):
    """Return {AccountNumber: total_amount} from get_euer_data."""
    rows = db.get_euer_data(date_from, date_to, account_ids)
    return {r[0]: r[2] for r in rows}


class TestEuerData:
    def test_empty_result_no_bookings(self, db_with_coa):
        result = db_with_coa.get_euer_data('2020-01-01', '2020-12-31')
        assert result == []

    def test_income_entry_appears_as_positive(self, db_with_coa):
        """Bank booking + entry child for income: 4400 should show net amount."""
        bank_acct = _get_acct_id(db_with_coa, 'Testbank')
        coa_bank = _get_coa_id(db_with_coa, 1200)
        coa_income = _get_coa_id(db_with_coa, 4400)

        # Bank (parent)
        parent_id = db_with_coa.insert_booking(
            '2024-01-15', 119.0, account_id=bank_acct, booking_type='bank')
        # Entry child
        db_with_coa.insert_booking(
            '2024-01-15', 119.0,
            coa_id=coa_bank, counter_coa_id=coa_income,
            tax_rate=0.19, tax_amount=19.0,
            booking_type='entry', parent_booking_id=parent_id,
        )

        euer = _euer_map(db_with_coa, '2024-01-01', '2024-12-31')
        # Netto = 119 - 19 = 100
        assert 4400 in euer
        assert abs(euer[4400] - 100.0) < 0.01

    def test_expense_entry_appears_as_negative(self, db_with_coa):
        """Expense booking: 6815 should have negative netto amount."""
        bank_acct = _get_acct_id(db_with_coa, 'Testbank')
        coa_bank = _get_coa_id(db_with_coa, 1200)
        coa_expense = _get_coa_id(db_with_coa, 6815)

        parent_id = db_with_coa.insert_booking(
            '2024-02-10', -119.0, account_id=bank_acct, booking_type='bank')
        db_with_coa.insert_booking(
            '2024-02-10', -119.0,
            coa_id=coa_expense, counter_coa_id=coa_bank,
            tax_rate=0.19, tax_amount=-19.0,
            booking_type='entry', parent_booking_id=parent_id,
        )

        euer = _euer_map(db_with_coa, '2024-01-01', '2024-12-31')
        assert 6815 in euer
        assert euer[6815] < 0

    def test_ust_from_income_appears_on_3806(self, db_with_coa):
        """USt-Anteil einer Einnahme soll virtuell auf Konto 3806 erscheinen."""
        bank_acct = _get_acct_id(db_with_coa, 'Testbank')
        coa_bank = _get_coa_id(db_with_coa, 1200)
        coa_income = _get_coa_id(db_with_coa, 4400)

        parent_id = db_with_coa.insert_booking(
            '2024-03-01', 595.0, account_id=bank_acct, booking_type='bank')
        db_with_coa.insert_booking(
            '2024-03-01', 595.0,
            coa_id=coa_bank, counter_coa_id=coa_income,
            tax_rate=0.19, tax_amount=95.0,
            booking_type='entry', parent_booking_id=parent_id,
        )

        euer = _euer_map(db_with_coa, '2024-01-01', '2024-12-31')
        assert 3806 in euer, "USt-Einnahmen müssen auf 3806 erscheinen"
        assert abs(euer[3806] - 95.0) < 0.01

    def test_vorsteuer_19_on_1406(self, db_with_coa):
        """Vorsteuer 19% auf Ausgaben soll auf Konto 1406 erscheinen."""
        bank_acct = _get_acct_id(db_with_coa, 'Testbank')
        coa_bank = _get_coa_id(db_with_coa, 1200)
        coa_expense = _get_coa_id(db_with_coa, 6815)

        parent_id = db_with_coa.insert_booking(
            '2024-04-05', -238.0, account_id=bank_acct, booking_type='bank')
        db_with_coa.insert_booking(
            '2024-04-05', -238.0,
            coa_id=coa_expense, counter_coa_id=coa_bank,
            tax_rate=0.19, tax_amount=-38.0,
            booking_type='entry', parent_booking_id=parent_id,
        )

        euer = _euer_map(db_with_coa, '2024-01-01', '2024-12-31')
        assert 1406 in euer, "Vorsteuer 19% muss auf 1406 erscheinen"
        assert abs(euer[1406] - (-38.0)) < 0.01

    def test_vorsteuer_7_on_1401(self, db_with_coa):
        """Vorsteuer 7% auf Ausgaben soll auf Konto 1401 erscheinen."""
        bank_acct = _get_acct_id(db_with_coa, 'Testbank')
        coa_bank = _get_coa_id(db_with_coa, 1200)
        coa_expense = _get_coa_id(db_with_coa, 6815)

        parent_id = db_with_coa.insert_booking(
            '2024-04-10', -107.0, account_id=bank_acct, booking_type='bank')
        db_with_coa.insert_booking(
            '2024-04-10', -107.0,
            coa_id=coa_expense, counter_coa_id=coa_bank,
            tax_rate=0.07, tax_amount=-7.0,
            booking_type='entry', parent_booking_id=parent_id,
        )

        euer = _euer_map(db_with_coa, '2024-01-01', '2024-12-31')
        assert 1401 in euer, "Vorsteuer 7% muss auf 1401 erscheinen"
        assert abs(euer[1401] - (-7.0)) < 0.01

    def test_doppik_mirror_excluded(self, db_with_coa):
        """Entry where both COA_ID and CounterCOA_ID are bank accounts → excluded from EÜR."""
        bank_acct = _get_acct_id(db_with_coa, 'Testbank')
        coa_bank = _get_coa_id(db_with_coa, 1200)

        parent_id = db_with_coa.insert_booking(
            '2024-05-01', 500.0, account_id=bank_acct, booking_type='bank')
        # Both sides are the same bank COA → Doppik-Spiegel
        db_with_coa.insert_booking(
            '2024-05-01', 500.0,
            coa_id=coa_bank, counter_coa_id=coa_bank,
            booking_type='entry', parent_booking_id=parent_id,
        )

        euer = _euer_map(db_with_coa, '2024-01-01', '2024-12-31')
        # 1200 should not appear (or have 0 total)
        assert euer.get(1200, 0.0) == pytest.approx(0.0, abs=0.01)

    def test_date_range_filter(self, db_with_coa):
        """Bookings outside the date range must not appear."""
        bank_acct = _get_acct_id(db_with_coa, 'Testbank')
        coa_bank = _get_coa_id(db_with_coa, 1200)
        coa_income = _get_coa_id(db_with_coa, 4400)

        parent_id = db_with_coa.insert_booking(
            '2023-12-31', 119.0, account_id=bank_acct, booking_type='bank')
        db_with_coa.insert_booking(
            '2023-12-31', 119.0,
            coa_id=coa_bank, counter_coa_id=coa_income,
            tax_rate=0.19, tax_amount=19.0,
            booking_type='entry', parent_booking_id=parent_id,
        )

        euer = _euer_map(db_with_coa, '2024-01-01', '2024-12-31')
        assert 4400 not in euer or euer.get(4400, 0.0) == pytest.approx(0.0, abs=0.01)
