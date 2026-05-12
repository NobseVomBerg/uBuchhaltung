"""
Tests for WISO CSV import functionality (db.import_wiso_csv).

Coverage:
- Format auto-detection (original 9-col vs table 6-col)
- Successful import counts
- Duplicate detection and skipping
- Tax calculation from BU-Schlüssel (401→19%, 402→7%)
- Special case 4405→4400 gets 19% even without BU key
- Amount sign logic (liquides Gegenkonto → negative)
- Split group creation for same Referenznummer
- Table format updates existing bookings (RecipientClient, Text)
- Encoding fallback (cp1252)
- Unknown format returns error
"""
import os
import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def _load_fixture(filename: str, encoding='utf-8') -> bytes:
    with open(os.path.join(FIXTURES, filename), encoding=encoding) as f:
        return f.read().encode(encoding)


# ─────────────────────────────────────────────
# Format detection
# ─────────────────────────────────────────────

class TestFormatDetection:
    def test_original_format_detected(self, db_with_coa):
        data = _load_fixture('wiso_original.csv')
        result = db_with_coa.import_wiso_csv(data)
        assert result['format'] == 'original'
        assert result['errors'] == []

    def test_table_format_detected(self, db_with_coa):
        # Table format requires existing bookings to update; just check format detection
        data = _load_fixture('wiso_table.csv')
        result = db_with_coa.import_wiso_csv(data)
        assert result['format'] != 'unknown', f"Expected table format, got: {result}"

    def test_unknown_format_returns_error(self, db_with_coa):
        csv_bytes = b"Col1;Col2;Col3\nfoo;bar;baz\n"
        result = db_with_coa.import_wiso_csv(csv_bytes)
        assert result['format'] == 'unknown'
        assert len(result['errors']) > 0

    def test_empty_file_raises_error(self, db_with_coa):
        result = db_with_coa.import_wiso_csv(b"")
        assert result['format'] == 'unknown'


# ─────────────────────────────────────────────
# Original format import
# ─────────────────────────────────────────────

class TestOriginalFormatImport:
    def test_basic_import_count(self, db_with_coa):
        """All 7 fixture rows should be imported on first run."""
        data = _load_fixture('wiso_original.csv')
        result = db_with_coa.import_wiso_csv(data)
        assert result['errors'] == []
        assert result['imported'] == 7

    def test_no_skipped_on_first_import(self, db_with_coa):
        data = _load_fixture('wiso_original.csv')
        result = db_with_coa.import_wiso_csv(data)
        assert result['skipped'] == 0

    def test_duplicate_import_skips_all(self, db_with_coa):
        """Re-importing the same file should skip all rows."""
        data = _load_fixture('wiso_original.csv')
        db_with_coa.import_wiso_csv(data)
        result2 = db_with_coa.import_wiso_csv(data)
        assert result2['imported'] == 0
        assert result2['skipped'] == 7

    def test_tax_rate_19_from_bu_401(self, db_with_coa):
        """BU 401 → TaxRate = 0.19"""
        csv = (
            "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"
            "1;01.06.2024;4400;1200;Testverkauf;TAX-19;119,00;401;\n"
        ).encode('utf-8')
        db_with_coa.import_wiso_csv(csv)
        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT TaxRate, TaxAmount FROM Bookings WHERE DocumentNumber='TAX-19'")
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert abs(row[0] - 0.19) < 1e-4
        assert abs(abs(row[1]) - 19.0) < 0.01   # 119 → |USt| = 19

    def test_tax_rate_7_from_bu_402(self, db_with_coa):
        """BU 402 → TaxRate = 0.07"""
        csv = (
            "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"
            "1;02.06.2024;4400;1200;Testverkauf 7;TAX-7;107,00;402;\n"
        ).encode('utf-8')
        db_with_coa.import_wiso_csv(csv)
        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT TaxRate, TaxAmount FROM Bookings WHERE DocumentNumber='TAX-7'")
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert abs(row[0] - 0.07) < 1e-4
        assert abs(abs(row[1]) - 7.0) < 0.01   # 107 → |USt| ≈ 7

    def test_special_4405_4400_gets_19_percent(self, db_with_coa):
        """4405→4400 Umbuchung ohne BU-Schlüssel bekommt implizit 19%."""
        csv = (
            "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"
            "1;03.06.2024;4405;4400;Umbuchung;UMBU-001;595,00;;\n"
        ).encode('utf-8')
        db_with_coa.import_wiso_csv(csv)
        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT TaxRate FROM Bookings WHERE DocumentNumber='UMBU-001'")
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert abs(row[0] - 0.19) < 1e-4

    def test_no_tax_without_bu_key(self, db_with_coa):
        """Row without BU-Schlüssel (not 4405→4400) has no tax rate."""
        csv = (
            "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"
            "1;04.06.2024;6815;1200;Reise;NO-TAX;500,00;;\n"
        ).encode('utf-8')
        db_with_coa.import_wiso_csv(csv)
        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT TaxRate, TaxAmount FROM Bookings WHERE DocumentNumber='NO-TAX'")
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] is None
        assert row[1] is None

    def test_amount_sign_liquid_gegenkonto_negative(self, db_with_coa):
        """GEGENKONTO=1200 (Bank, liquide) → Amount should be negative (Ausgabe)."""
        csv = (
            "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"
            "1;05.06.2024;6815;1200;Ausgabe;SIGN-OUT;238,00;401;\n"
        ).encode('utf-8')
        db_with_coa.import_wiso_csv(csv)
        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT Amount FROM Bookings WHERE DocumentNumber='SIGN-OUT'")
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] < 0, "Ausgabe (liquid Gegenkonto) muss negativ sein"

    def test_amount_sign_liquid_konto_positive(self, db_with_coa):
        """KONTO=1200 (Bank, liquide) → Amount should be positive (Einnahme)."""
        csv = (
            "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"
            "1;06.06.2024;1200;4400;Einnahme;SIGN-IN;1190,00;401;\n"
        ).encode('utf-8')
        db_with_coa.import_wiso_csv(csv)
        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT Amount FROM Bookings WHERE DocumentNumber='SIGN-IN'")
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] > 0, "Einnahme (liquid Konto) muss positiv sein"

    def test_split_group_created_for_same_reference(self, db_with_coa):
        """Two rows with same REFERENZNUMMER+Datum → BookingGroup created."""
        csv = (
            "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"
            "1;07.06.2024;4400;1200;Teil 1;GRP-001;500,00;401;\n"
            "2;07.06.2024;6815;1200;Teil 2;GRP-001;100,00;401;\n"
        ).encode('utf-8')
        db_with_coa.import_wiso_csv(csv)
        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT BookingGroup_ID FROM Bookings WHERE DocumentNumber='GRP-001'")
        rows = cur.fetchall()
        conn.close()
        group_ids = [r[0] for r in rows]
        assert all(g is not None for g in group_ids), "Both rows must be in a group"
        assert len(set(group_ids)) == 1, "Both rows must be in the same group"

    def test_cp1252_encoding(self, db_with_coa):
        """Import of CP1252-encoded file should succeed."""
        csv_text = (
            "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"
            "1;08.06.2024;6815;1200;Büromöbel;ENC-001;119,00;401;\n"
        )
        csv_bytes = csv_text.encode('cp1252')
        result = db_with_coa.import_wiso_csv(csv_bytes)
        assert result['errors'] == []
        assert result['imported'] == 1


# ─────────────────────────────────────────────
# Table format import
# ─────────────────────────────────────────────

class TestTableFormatImport:
    def _seed_bookings(self, db):
        """Insert base bookings that the table-format import can update."""
        db.insert_booking('2024-03-01', -1190.0, booking_type='entry', document_number='R2024-001')
        db.insert_booking('2024-03-05', -238.0,  booking_type='entry', document_number='R2024-002')
        db.insert_booking('2024-03-10',  107.0,  booking_type='entry', document_number='R2024-003')
        db.insert_booking('2024-03-15', -500.0,  booking_type='entry', document_number='R2024-004')

    def test_table_format_updates_recipient(self, db_with_coa):
        self._seed_bookings(db_with_coa)
        data = _load_fixture('wiso_table.csv')
        result = db_with_coa.import_wiso_csv(data)
        assert result['errors'] == []
        assert result['updated'] >= 1

        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT RecipientClient FROM Bookings WHERE DocumentNumber='R2024-001'")
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 'Mustermann GmbH'

    def test_table_format_no_duplicate_created(self, db_with_coa):
        """Table format should update, not insert new rows."""
        self._seed_bookings(db_with_coa)
        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM Bookings")
        before = cur.fetchone()[0]
        conn.close()

        data = _load_fixture('wiso_table.csv')
        db_with_coa.import_wiso_csv(data)

        conn = db_with_coa._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM Bookings")
        after = cur.fetchone()[0]
        conn.close()
        assert after == before, "Table-format import must not add new rows"
