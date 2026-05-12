"""
Module tests for db.py – core database functionality.

Coverage:
- Schema: tables exist, UNIQUE constraints, FOREIGN KEY enforcement
- Booking CRUD: insert, fetch, update, delete (cascade)
- Duplicate detection: check_booking_exists
- Bank↔Entry matching: find_unlinked_booking_by_date_amount (single + group)
- TaxKeys: get_tax_rate_for_bu
- fetch_bookings_grouped: bank/entry/normal rows
- fetch_chart_of_accounts
"""
import pytest


# ─────────────────────────────────────────────
# Schema tests
# ─────────────────────────────────────────────

class TestSchema:
    def test_expected_tables_exist(self, tmp_db):
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cur.fetchall()}
        conn.close()

        expected = {
            'Accounts', 'Articles', 'AssetCategories', 'AssetDepreciations',
            'Assets', 'BookingDocuments', 'BookingGroups', 'Bookings',
            'Categories', 'ChartOfAccounts', 'CompanyDetails', 'ContactAddresses',
            'Contacts', 'Documents', 'InvoiceItems', 'InvoicePayments',
            'Invoices', 'NumberRanges', 'PersonDetails', 'TaxKeys',
        }
        assert expected.issubset(tables)

    def test_accounts_name_unique(self, tmp_db):
        """Two accounts with the same name: second insert should be silently rejected."""
        tmp_db.insert_account('UniqueBank', 'Owner', 'DE001', 'BANKDE1X', 'Bank AG', skr_account=1200)
        tmp_db.insert_account('UniqueBank', 'Owner', 'DE002', 'BANKDE1X', 'Bank AG', skr_account=1200)
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM Accounts WHERE Name='UniqueBank'")
        count = cur.fetchone()[0]
        conn.close()
        assert count == 1, "Duplicate account name must not create a second row"

    def test_foreign_key_booking_account(self, tmp_db):
        """Inserting a booking with a non-existent Account_ID should fail."""
        import sqlite3
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        with pytest.raises(sqlite3.IntegrityError):
            cur.execute(
                "INSERT INTO Bookings (DateBooking, Amount, Account_ID, BookingType) VALUES (?,?,?,?)",
                ('2024-01-01', 100.0, 9999, 'bank')
            )
        conn.close()

    def test_tax_keys_seeded(self, tmp_db):
        """TaxKeys table should be seeded from tax_keys.json on init."""
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM TaxKeys')
        count = cur.fetchone()[0]
        conn.close()
        assert count > 0, "TaxKeys should be pre-seeded"

    def test_chart_of_accounts_seeded(self, tmp_db):
        """ChartOfAccounts should be seeded with SKR04 entries on init."""
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM ChartOfAccounts WHERE Framework=4')
        count = cur.fetchone()[0]
        conn.close()
        assert count > 50, "SKR04 should have many entries"


# ─────────────────────────────────────────────
# Booking CRUD
# ─────────────────────────────────────────────

class TestBookingCRUD:
    def test_insert_and_fetch_booking(self, tmp_db):
        bid = tmp_db.insert_booking(
            date_booking='2024-03-15',
            amount=-119.0,
            text='Testbuchung',
            booking_type='entry',
        )
        assert isinstance(bid, int)
        row = tmp_db.get_booking_by_id(bid)
        assert row is not None
        assert row[1] == '2024-03-15'   # DateBooking
        assert row[11] == -119.0         # Amount

    def test_insert_booking_with_tax(self, tmp_db):
        bid = tmp_db.insert_booking(
            date_booking='2024-06-01',
            amount=119.0,
            tax_rate=0.19,
            tax_amount=19.0,
            text='Rechnung mit USt',
        )
        row = tmp_db.get_booking_by_id(bid)
        assert row[13] == 0.19   # TaxRate
        assert row[14] == 19.0   # TaxAmount

    def test_update_booking(self, tmp_db):
        bid = tmp_db.insert_booking('2024-01-10', 50.0, text='Alt')
        tmp_db.update_booking(
            booking_id=bid,
            date_booking='2024-01-10',
            amount=75.0,
            text='Neu',
        )
        row = tmp_db.get_booking_by_id(bid)
        assert row[11] == 75.0
        assert row[15] == 'Neu'

    def test_delete_booking_removes_row(self, tmp_db):
        bid = tmp_db.insert_booking('2024-01-20', 200.0, text='Zu löschen')
        tmp_db.delete_transaction(bid)
        assert tmp_db.get_booking_by_id(bid) is None

    def test_delete_booking_cascades_children(self, tmp_db):
        """Deleting a bank booking must also delete linked entry children."""
        parent_id = tmp_db.insert_booking('2024-02-01', -500.0, booking_type='bank')
        child_id = tmp_db.insert_booking(
            '2024-02-01', -500.0, booking_type='entry',
            parent_booking_id=parent_id,
        )
        tmp_db.delete_transaction(parent_id)
        assert tmp_db.get_booking_by_id(parent_id) is None
        assert tmp_db.get_booking_by_id(child_id) is None


# ─────────────────────────────────────────────
# Duplicate detection
# ─────────────────────────────────────────────

class TestDuplicateDetection:
    def test_check_booking_exists_true(self, tmp_db):
        tmp_db.insert_account('DupBank', 'Owner', 'DE999', 'DUPBDE1X', 'DupBank AG', skr_account=1200)
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Accounts WHERE Name='DupBank'")
        acct_id = cur.fetchone()[0]
        conn.close()
        tmp_db.insert_booking('2024-05-01', -42.0, account_id=acct_id,
                               foreign_bank_account='DE001', text='Dup-Test')
        assert tmp_db.check_booking_exists(
            '2024-05-01', -42.0, account_id=acct_id,
            foreign_bank_account='DE001', text='Dup-Test',
        )

    def test_check_booking_exists_false(self, tmp_db):
        assert not tmp_db.check_booking_exists('2099-12-31', 999999.0)


# ─────────────────────────────────────────────
# Bank↔Entry matching
# ─────────────────────────────────────────────

class TestFindUnlinkedBooking:
    def test_single_match(self, tmp_db):
        tmp_db.insert_booking('2024-04-10', -100.0, booking_type='entry',
                               document_number='INV-001', text='Einzelbuchung')
        result = tmp_db.find_unlinked_booking_by_date_amount('2024-04-10', -100.0)
        assert result is not None
        assert result[0] == 'single'

    def test_no_match_wrong_date(self, tmp_db):
        tmp_db.insert_booking('2024-04-10', -100.0, booking_type='entry')
        result = tmp_db.find_unlinked_booking_by_date_amount('2024-04-11', -100.0)
        assert result is None

    def test_no_match_already_linked(self, tmp_db):
        """A booking that already has an Account_ID is considered linked."""
        tmp_db.insert_account('LinkedBank', 'Owner', 'DE777', 'LNKDE1X', 'LinkedBank AG', skr_account=1200)
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Accounts WHERE Name='LinkedBank'")
        acct_id = cur.fetchone()[0]
        conn.close()
        tmp_db.insert_booking('2024-04-15', -55.0, account_id=acct_id,
                               booking_type='entry', document_number='REF-002')
        result = tmp_db.find_unlinked_booking_by_date_amount('2024-04-15', -55.0)
        assert result is None

    def test_group_match(self, tmp_db):
        """Two entries with the same doc+date form a split group whose sum matches."""
        grp_id = tmp_db.create_booking_group('Split test', total_amount=300.0)
        tmp_db.insert_booking('2024-05-20', 200.0, booking_type='entry',
                               document_number='SPLIT-1', booking_group_id=grp_id)
        tmp_db.insert_booking('2024-05-20', 100.0, booking_type='entry',
                               document_number='SPLIT-1', booking_group_id=grp_id)
        result = tmp_db.find_unlinked_booking_by_date_amount('2024-05-20', 300.0)
        assert result is not None
        assert result[0] == 'group'


# ─────────────────────────────────────────────
# TaxKeys
# ─────────────────────────────────────────────

class TestTaxKeys:
    def test_known_bu_key_401(self, tmp_db):
        rate = tmp_db.get_tax_rate_for_bu('401')
        assert rate == pytest.approx(0.19, abs=1e-4)

    def test_known_bu_key_402(self, tmp_db):
        rate = tmp_db.get_tax_rate_for_bu('402')
        assert rate == pytest.approx(0.07, abs=1e-4)

    def test_unknown_bu_key_returns_none(self, tmp_db):
        assert tmp_db.get_tax_rate_for_bu('XXXXX') is None

    def test_empty_bu_key_returns_none(self, tmp_db):
        assert tmp_db.get_tax_rate_for_bu('') is None


# ─────────────────────────────────────────────
# fetch_bookings_grouped
# ─────────────────────────────────────────────

class TestFetchBookingsGrouped:
    def test_bank_booking_appears(self, tmp_db):
        tmp_db.insert_booking('2024-07-01', -200.0, booking_type='bank', text='Bankbuchung')
        rows = tmp_db.fetch_bookings_grouped()
        bank_rows = [r for r in rows if r['type'] == 'bank']
        assert len(bank_rows) == 1
        assert bank_rows[0]['booking'][15] == 'Bankbuchung'

    def test_entry_booking_appears_as_normal(self, tmp_db):
        tmp_db.insert_booking('2024-07-02', 50.0, booking_type='entry', text='Einzel')
        rows = tmp_db.fetch_bookings_grouped()
        normal_rows = [r for r in rows if r['type'] == 'normal']
        texts = [r['booking'][15] for r in normal_rows]
        assert 'Einzel' in texts

    def test_linked_child_appears_under_bank(self, tmp_db):
        parent_id = tmp_db.insert_booking('2024-07-03', -300.0, booking_type='bank')
        tmp_db.insert_booking('2024-07-03', -300.0, booking_type='entry',
                               parent_booking_id=parent_id, text='KindBuchung')
        rows = tmp_db.fetch_bookings_grouped()
        bank_rows = [r for r in rows if r['type'] == 'bank']
        assert len(bank_rows) == 1
        assert bank_rows[0]['linked'] is True
        child_rows = [r for r in rows if r['type'] == 'child']
        assert len(child_rows) == 1
        assert child_rows[0]['booking'][15] == 'KindBuchung'


# ─────────────────────────────────────────────
# fetch_chart_of_accounts
# ─────────────────────────────────────────────

class TestChartOfAccounts:
    def test_returns_list(self, tmp_db):
        rows = tmp_db.fetch_chart_of_accounts()
        assert isinstance(rows, list)
        assert len(rows) > 0

    def test_contains_skr04_accounts(self, tmp_db):
        rows = tmp_db.fetch_chart_of_accounts()
        account_numbers = [r[2] for r in rows]  # AccountNumber is column 2
        assert 4400 in account_numbers
        assert 6815 in account_numbers
