# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Phase 1f (Decimal-Migration): Bookings.Amount/TaxAmount als Festkomma-Integer.
Amount=Index 11, TaxAmount=Index 14."""
import sqlite3
from decimal import Decimal


def test_amount_roundtrip(tmp_db):
    bid = tmp_db.insert_booking('2026-01-15', 119.00, tax_rate=0.19, tax_amount=19.00,
                                text='Test', booking_type='entry')
    b = tmp_db.get_booking_by_id(bid)
    assert b[11] == Decimal("119.0000")  # Amount
    assert b[14] == Decimal("19.0000")   # TaxAmount


def test_amount_stored_as_integer(tmp_db):
    tmp_db.insert_booking('2026-01-15', 119.00, tax_amount=19.00, text='T')
    con = sqlite3.connect(tmp_db.db_name)
    amount, tax = con.execute("SELECT Amount, TaxAmount FROM Bookings").fetchone()
    con.close()
    assert isinstance(amount, int) and isinstance(tax, int)
    assert (amount, tax) == (1190000, 190000)


def test_negative_amount(tmp_db):
    bid = tmp_db.insert_booking('2026-01-15', -41.25, text='Ausgabe')
    assert tmp_db.get_booking_by_id(bid)[11] == Decimal("-41.2500")


def test_tax_amount_none_stays_none(tmp_db):
    bid = tmp_db.insert_booking('2026-01-15', 50.00, text='ohne Steuer')
    b = tmp_db.get_booking_by_id(bid)
    assert b[14] is None


def test_fetch_bookings_returns_decimal(tmp_db):
    tmp_db.insert_booking('2026-01-15', 10.00, text='A')
    assert tmp_db.fetch_bookings()[0][11] == Decimal("10.0000")


def test_update_booking_amount(tmp_db):
    bid = tmp_db.insert_booking('2026-01-15', 10.00, text='A')
    tmp_db.update_booking(bid, '2026-01-15', 99.99, text='A')
    assert tmp_db.get_booking_by_id(bid)[11] == Decimal("99.9900")


def test_fourdigit_precision(tmp_db):
    bid = tmp_db.insert_booking('2026-01-15', '12.3456', text='A')
    assert tmp_db.get_booking_by_id(bid)[11] == Decimal("12.3456")
