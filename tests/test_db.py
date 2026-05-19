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
- NumberRanges: _apply_number_format, get_next_number, insert/update, format templates
"""
import pytest
from db import Database


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


# ─────────────────────────────────────────────
# Depreciation booking
# ─────────────────────────────────────────────

class TestBookDepreciation:
    """Tests for book_depreciation(): AfA-Buchung anlegen."""

    def _setup_coa(self, db):
        """Insert minimal COA rows needed for depreciation booking."""
        conn = db._get_connection()
        cur = conn.cursor()
        cur.executemany(
            'INSERT OR IGNORE INTO ChartOfAccounts (Framework, AccountNumber, Name, Description, IsStandard) VALUES (?,?,?,?,?)',
            [
                (4, 4830, 'AfA', 'Abschreibungen', 1),
                (4, 420,  'Betriebs- und Geschäftsausstattung', 'BGA', 1),
            ],
        )
        conn.commit()
        cur.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber=4830')
        coa_expense = cur.fetchone()[0]
        cur.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber=420')
        coa_asset = cur.fetchone()[0]
        conn.close()
        return coa_expense, coa_asset

    def test_booking_type_is_entry(self, tmp_db):
        """book_depreciation must create a Booking with BookingType='entry', not 'expense'."""
        coa_expense, coa_asset = self._setup_coa(tmp_db)
        asset_id = tmp_db.insert_asset(
            name='Laptop', purchase_date='2024-01-01',
            purchase_price=1200.0, useful_life_years=3,
            coa_id=coa_asset,
        )
        booking_id = tmp_db.book_depreciation(
            asset_id=asset_id, year=2024,
            account_id=None,
            coa_id_expense=coa_expense,
            coa_id_asset=coa_asset,
        )
        row = tmp_db.get_booking_by_id(booking_id)
        assert row is not None
        assert row[17] == 'entry', f"Expected BookingType='entry', got '{row[17]}'"

    def test_booking_amount_matches_plan(self, tmp_db):
        """The booked amount must equal the planned depreciation for the year."""
        coa_expense, coa_asset = self._setup_coa(tmp_db)
        asset_id = tmp_db.insert_asset(
            name='Drucker', purchase_date='2023-01-01',
            purchase_price=900.0, useful_life_years=3,
            coa_id=coa_asset,
        )
        plan = tmp_db.calculate_depreciation_plan(900.0, '2023-01-01', 3, 'linear')
        expected = next(e['depreciation'] for e in plan if e['year'] == 2023)

        booking_id = tmp_db.book_depreciation(
            asset_id=asset_id, year=2023,
            account_id=None,
            coa_id_expense=coa_expense,
            coa_id_asset=coa_asset,
        )
        row = tmp_db.get_booking_by_id(booking_id)
        assert abs(row[11]) == pytest.approx(expected, abs=0.01)

    def test_asset_depreciation_record_posted(self, tmp_db):
        """After booking, AssetDepreciations must have Status='posted' for that year."""
        coa_expense, coa_asset = self._setup_coa(tmp_db)
        asset_id = tmp_db.insert_asset(
            name='Monitor', purchase_date='2022-06-01',
            purchase_price=600.0, useful_life_years=3,
            coa_id=coa_asset,
        )
        tmp_db.book_depreciation(
            asset_id=asset_id, year=2022,
            account_id=None,
            coa_id_expense=coa_expense,
            coa_id_asset=coa_asset,
        )
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT Status FROM AssetDepreciations WHERE Asset_ID=? AND Year=?",
            (asset_id, 2022),
        )
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 'posted'

    def test_invalid_asset_raises(self, tmp_db):
        """Passing a non-existent asset_id must raise ValueError."""
        with pytest.raises(ValueError, match="not found"):
            tmp_db.book_depreciation(
                asset_id=9999, year=2024,
                account_id=None,
                coa_id_expense=1,
                coa_id_asset=2,
            )

    def test_invalid_year_raises(self, tmp_db):
        """Passing a year outside the depreciation plan must raise ValueError."""
        coa_expense, coa_asset = self._setup_coa(tmp_db)
        asset_id = tmp_db.insert_asset(
            name='Kamera', purchase_date='2024-01-01',
            purchase_price=1500.0, useful_life_years=2,
            coa_id=coa_asset,
        )
        with pytest.raises(ValueError, match="No depreciation planned"):
            tmp_db.book_depreciation(
                asset_id=asset_id, year=2099,
                account_id=None,
                coa_id_expense=coa_expense,
                coa_id_asset=coa_asset,
            )


# ─────────────────────────────────────────────
# NumberRanges / Nummernkreise
# ─────────────────────────────────────────────

class TestApplyNumberFormat:
    """Unit tests for Database._apply_number_format() – pure function."""

    def test_default_format_no_suffix(self):
        """Standard-Format ohne Suffix → 26F001"""
        result = Database._apply_number_format('{yy}{l}{nnn}{s}', 2026, 'F', 1, '')
        assert result == '26F001'

    def test_default_format_with_suffix(self):
        """Standard-Format mit Suffix → 26F002_A"""
        result = Database._apply_number_format('{yy}{l}{nnn}{s}', 2026, 'F', 2, '_A')
        assert result == '26F002_A'

    def test_suffix_b(self):
        """Suffix _B → 26F002_B"""
        result = Database._apply_number_format('{yy}{l}{nnn}{s}', 2026, 'F', 2, '_B')
        assert result == '26F002_B'

    def test_different_letter(self):
        """Buchstabe V → 26V001"""
        result = Database._apply_number_format('{yy}{l}{nnn}{s}', 2026, 'V', 1, '')
        assert result == '26V001'

    def test_four_digit_year(self):
        """{yyyy} → vollständiges Jahr"""
        result = Database._apply_number_format('{yyyy}{l}{nnn}{s}', 2026, 'R', 5, '')
        assert result == '2026R005'

    def test_unpadded_number(self):
        """{n} → ohne führende Nullen"""
        result = Database._apply_number_format('{yy}{l}{n}', 2026, 'R', 7, '')
        assert result == '26R7'

    def test_two_digit_padded_number(self):
        """{nn} → 2-stellig aufgefüllt"""
        result = Database._apply_number_format('{yy}{l}{nn}{s}', 2026, 'B', 3, '')
        assert result == '26B03'

    def test_custom_separator_in_template(self):
        """Freitext-Template mit Trennzeichen"""
        result = Database._apply_number_format('{yyyy}-{l}-{nnn}', 2026, 'X', 42, '')
        assert result == '2026-X-042'

    def test_none_format_uses_default(self):
        """Leeres Format-Template fällt auf Standard zurück"""
        result = Database._apply_number_format('', 2026, 'F', 1, '')
        assert result == '26F001'

    def test_none_suffix_treated_as_empty(self):
        """{s} mit None-Suffix → kein Anhang"""
        result = Database._apply_number_format('{yy}{l}{nnn}{s}', 2026, 'F', 1, None)
        assert result == '26F001'

    def test_three_digit_padding(self):
        """Nummer ≥ 100 wird nicht abgeschnitten"""
        result = Database._apply_number_format('{yy}{l}{nnn}{s}', 2026, 'R', 123, '')
        assert result == '26R123'

    def test_lowercase_letter_uppercased(self):
        """{l} gibt Buchstaben immer in Großschreibung aus"""
        result = Database._apply_number_format('{yy}{l}{nnn}', 2026, 'r', 1, '')
        assert result == '26R001'


class TestNumberRangeOperations:
    """Integration tests für get_next_number, insert/update und get_current_number_info."""

    def test_get_next_number_default_format(self, tmp_db):
        """Erster Aufruf ohne vorhandenen Nummernkreis liefert 001"""
        result = tmp_db.get_next_number('invoice', 2026, 'F')
        assert result == '26F001'

    def test_get_next_number_increments(self, tmp_db):
        """Zweiter Aufruf liefert 002"""
        tmp_db.get_next_number('invoice', 2026, 'F')
        result = tmp_db.get_next_number('invoice', 2026, 'F')
        assert result == '26F002'

    def test_get_next_number_with_suffix(self, tmp_db):
        """Suffix _A wird ans Ende angehängt, nicht vor die Nummer"""
        tmp_db.insert_number_range('invoice', 2026, 'F', prefix='_A', current_number=0)
        result = tmp_db.get_next_number('invoice', 2026, 'F', prefix='_A')
        assert result == '26F001_A'

    def test_get_next_number_custom_format(self, tmp_db):
        """Benutzerdefiniertes Format wird aus der DB gelesen und angewendet"""
        tmp_db.insert_number_range('invoice', 2026, 'R', number_format='{yyyy}/{l}/{nnn}')
        result = tmp_db.get_next_number('invoice', 2026, 'R')
        assert result == '2026/R/001'

    def test_get_next_number_different_ranges_independent(self, tmp_db):
        """Verschiedene Typen haben unabhängige Zähler"""
        tmp_db.get_next_number('invoice', 2026, 'F')
        tmp_db.get_next_number('invoice', 2026, 'F')
        result_v = tmp_db.get_next_number('receipt_company', 2026, 'V')
        assert result_v == '26V001'

    def test_insert_and_read_back_number_format(self, tmp_db):
        """NumberFormat wird gespeichert und mit fetch_number_ranges zurückgegeben"""
        tmp_db.insert_number_range(
            'invoice', 2026, 'X',
            number_format='{yyyy}{l}{nnn}',
            description='Testkreis',
        )
        ranges = tmp_db.fetch_number_ranges('invoice')
        nr = next((r for r in ranges if r[3] == 'X'), None)
        assert nr is not None
        assert nr[7] == '{yyyy}{l}{nnn}'  # NumberFormat ist Index 7

    def test_update_number_format(self, tmp_db):
        """update_number_range speichert geändertes Format"""
        tmp_db.insert_number_range('invoice', 2026, 'U', number_format='{yy}{l}{nnn}{s}')
        ranges = tmp_db.fetch_number_ranges('invoice')
        nr = next(r for r in ranges if r[3] == 'U')
        tmp_db.update_number_range(nr[0], 2026, 'U', number_format='{yyyy}-{l}-{nnn}')
        updated = tmp_db.get_number_range_by_id(nr[0])
        assert updated[7] == '{yyyy}-{l}-{nnn}'

    def test_get_current_number_info_uses_stored_format(self, tmp_db):
        """get_current_number_info wendet das gespeicherte Format an"""
        tmp_db.insert_number_range(
            'invoice', 2026, 'Z',
            current_number=4,
            number_format='{yyyy}{l}{nnn}',
        )
        info = tmp_db.get_current_number_info('invoice', 2026, 'Z')
        assert info['current_number'] == 4
        assert info['next_number'] == 5
        assert info['formatted_next'] == '2026Z005'

    def test_get_current_number_info_empty_range(self, tmp_db):
        """Nicht vorhandener Nummernkreis liefert next=1 mit Standard-Format"""
        info = tmp_db.get_current_number_info('invoice', 2099, 'Q')
        assert info['next_number'] == 1
        assert info['formatted_next'] == '99Q001'

    def test_user_preferred_examples(self, tmp_db):
        """Nutzerpräferenz: 26F001, 26F002_A, 26F002_B, 26V001"""
        # 26F001 – erster Aufruf ohne Suffix
        assert tmp_db.get_next_number('invoice', 2026, 'F') == '26F001'

        # 26V001 – anderer Buchstabe
        assert tmp_db.get_next_number('receipt_company', 2026, 'V') == '26V001'

        # 26F002_A / 26F002_B – gleiche Sequenznummer, verschiedene Suffixe
        tmp_db.insert_number_range('invoice', 2026, 'F', prefix='_A', current_number=1)
        tmp_db.insert_number_range('invoice', 2026, 'F', prefix='_B', current_number=1)
        assert tmp_db.get_next_number('invoice', 2026, 'F', prefix='_A') == '26F002_A'
        assert tmp_db.get_next_number('invoice', 2026, 'F', prefix='_B') == '26F002_B'

    def test_numberformat_column_exists_in_schema(self, tmp_db):
        """NumberFormat-Spalte ist nach init in NumberRanges vorhanden"""
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(NumberRanges)")
        columns = {row[1] for row in cur.fetchall()}
        conn.close()
        assert 'NumberFormat' in columns


# ─────────────────────────────────────────────
# Invoice CRUD – column-index regression
# ─────────────────────────────────────────────

class TestInvoiceCRUD:
    """Regression tests for get_invoice_by_id column positions.

    These guard against silent column-order shifts whenever the Invoices
    schema changes.  The indices are used directly in pages_invoice.py and
    handlers.py – a mismatch would produce wrong data without raising an
    error at runtime.

    Column order (0-based, SELECT *):
      0  ID            1  InvoiceNumber   2  InvoiceDate
      3  OwnCompanyId  4  SellerName      5  SellerCompany
      …
      34 TaxCategory   35 TaxRate         36 SumNet
      37 TaxAmount     38 SumGross        39 AmountDue
      40 Status        41 PDFPath         42 XMLPath
    """

    _MINIMAL = {
        'invoice_number':  'REGR-001',
        'invoice_date':    '2026-01-15',
        'seller_name':     'Test Verkäufer',
        'seller_company':  'Test GmbH',
        'buyer_name':      'Test Käufer',
        'buyer_company':   'Käufer AG',
        'tax_rate':        0.19,
        'sum_net':         100.0,
        'tax_amount':      19.0,
        'sum_gross':       119.0,
        'amount_due':      119.0,
        'status':          'draft',
    }

    def test_key_column_indices(self, tmp_db):
        """Critical column indices used by the UI must match SELECT * order."""
        inv_id = tmp_db.insert_invoice(self._MINIMAL)
        row = tmp_db.get_invoice_by_id(inv_id)
        assert row is not None
        assert row[1]  == 'REGR-001',          f"InvoiceNumber expected at [1], got {row[1]!r}"
        assert row[2]  == '2026-01-15',         f"InvoiceDate expected at [2], got {row[2]!r}"
        assert row[35] == pytest.approx(0.19),    f"TaxRate expected at [35], got {row[35]!r}"
        assert row[36] == pytest.approx(100.0), f"SumNet expected at [36], got {row[36]!r}"
        assert row[38] == pytest.approx(119.0), f"SumGross expected at [38], got {row[38]!r}"
        assert row[40] == 'draft',              f"Status expected at [40], got {row[40]!r}"
        assert row[41] is None,                 f"PDFPath expected at [41], got {row[41]!r}"

    def test_non_default_status_preserved(self, tmp_db):
        """insert_invoice must persist any provided status, not just the default."""
        data = dict(self._MINIMAL, invoice_number='REGR-002', status='finalized')
        inv_id = tmp_db.insert_invoice(data)
        row = tmp_db.get_invoice_by_id(inv_id)
        assert row[40] == 'finalized'

    def test_overdue_status_label_spelling(self):
        """INVOICE_STATUS_LABELS['overdue'] must be spelled correctly."""
        from server.pages_invoice import INVOICE_STATUS_LABELS
        assert INVOICE_STATUS_LABELS['overdue'] == 'Überfällig'


# ─────────────────────────────────────────────
# handle_add_transaction – manual booking logic
# ─────────────────────────────────────────────

class TestHandleAddTransaction:
    """Integration tests for handle_add_transaction() in server/handlers.py.

    Verifies the three creation scenarios:
    a) bank account + COA set  → bank row + entry child (green checkmark)
    b) bank account only       → bank row, no child (unlinked)
    c) COA only (no bank acct) → plain entry row
    And the pre-existing update path (unlinked bank → COA set → entry child).
    """

    # Minimal post_data helper (URL-encoded form values are lists of strings)
    @staticmethod
    def _post(**kwargs):
        defaults = {
            'transaction_id': ['0'],
            'date': ['2026-05-01'],
            'date_tax': [''],
            'recipient': ['Test GmbH'],
            'text': ['Testbuchung'],
            'amount': ['119.0'],
            'currency': ['EUR'],
            'account': [''],
            'foreign_account': [''],
            'contact_id': [''],
            'coa_id': [''],
            'booking_group_id': [''],
            'tax_rate': ['19'],
            'tax_amount': ['19.0'],
            'document_nr': ['RE-001'],
        }
        defaults.update({k: [str(v)] for k, v in kwargs.items()})
        return defaults

    def _make_account(self, db):
        """Insert a bank account and return its ID."""
        db.insert_account('Testbank', 'Inhaber', 'DE89370400440532013000', 'TESTDE1X',
                          'Testbank AG', skr_account=1810)
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Accounts WHERE Name='Testbank'")
        acct_id = cur.fetchone()[0]
        conn.close()
        return acct_id

    def _make_coa(self, db):
        """Insert a minimal COA entry and return its ID."""
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute(
            'INSERT OR IGNORE INTO ChartOfAccounts '
            '(Framework, AccountNumber, Name, Description, IsStandard) VALUES (?,?,?,?,?)',
            (4, 4400, 'Erlöse', 'Umsatzerlöse 19%', 1),
        )
        conn.commit()
        cur.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber=4400')
        coa_id = cur.fetchone()[0]
        conn.close()
        return coa_id

    def test_new_bank_with_coa_creates_two_rows(self, tmp_db):
        """Bank account + COA on new booking → bank row + entry child immediately."""
        from server.handlers import handle_add_transaction
        acct_id = self._make_account(tmp_db)
        coa_id  = self._make_coa(tmp_db)

        handle_add_transaction(tmp_db, self._post(account=acct_id, coa_id=coa_id))

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID, BookingType, Account_ID, COA_ID, ParentBooking_ID FROM Bookings")
        rows = cur.fetchall()
        conn.close()

        assert len(rows) == 2, f"Expected 2 rows (bank + entry), got {len(rows)}"
        bank_rows  = [r for r in rows if r[1] == 'bank']
        entry_rows = [r for r in rows if r[1] == 'entry']
        assert len(bank_rows)  == 1, "Expected exactly 1 bank row"
        assert len(entry_rows) == 1, "Expected exactly 1 entry row"
        assert bank_rows[0][2]  == acct_id, "Bank row must have Account_ID set"
        assert entry_rows[0][3] == coa_id,  "Entry row must have COA_ID set"
        assert entry_rows[0][4] == bank_rows[0][0], \
            "Entry row ParentBooking_ID must point to bank row"

    def test_new_bank_without_coa_creates_one_unlinked_row(self, tmp_db):
        """Bank account without COA → single bank row, no entry child."""
        from server.handlers import handle_add_transaction
        acct_id = self._make_account(tmp_db)

        handle_add_transaction(tmp_db, self._post(account=acct_id))

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT BookingType, ParentBooking_ID FROM Bookings")
        rows = cur.fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == 'bank'
        assert rows[0][1] is None

    def test_new_entry_only_creates_one_entry_row(self, tmp_db):
        """COA without bank account → plain entry row (e.g. cash / correction)."""
        from server.handlers import handle_add_transaction
        coa_id = self._make_coa(tmp_db)

        handle_add_transaction(tmp_db, self._post(coa_id=coa_id))

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT BookingType, Account_ID FROM Bookings")
        rows = cur.fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == 'entry'
        assert rows[0][1] is None

    def test_update_unlinked_bank_with_coa_creates_entry_child(self, tmp_db):
        """Editing an unlinked bank booking with COA set must create an entry child."""
        from server.handlers import handle_add_transaction
        acct_id = self._make_account(tmp_db)
        coa_id  = self._make_coa(tmp_db)

        # Create unlinked bank booking first
        bank_id = tmp_db.insert_booking(
            date_booking='2026-05-01', amount=-100.0,
            account_id=acct_id, booking_type='bank',
        )
        post = self._post(transaction_id=bank_id, account=acct_id, coa_id=coa_id)
        handle_add_transaction(tmp_db, post)

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT BookingType, ParentBooking_ID FROM Bookings WHERE ID != ?", (bank_id,)
        )
        children = cur.fetchall()
        conn.close()

        assert len(children) == 1
        assert children[0][0] == 'entry'
        assert children[0][1] == bank_id
