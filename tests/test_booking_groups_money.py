# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Phase 1b (Decimal-Migration): BookingGroups.TotalAmount als Festkomma-Integer
gespeichert, als Euro-Decimal gelesen, None bleibt None."""
import sqlite3
from decimal import Decimal


def test_total_amount_roundtrip(tmp_db):
    gid = tmp_db.create_booking_group(description="Split", total_amount=119.99)
    groups = tmp_db.fetch_booking_groups()
    grp = next(g for g in groups if g[0] == gid)
    assert grp[3] == Decimal("119.9900")  # Index 3 = TotalAmount


def test_total_amount_stored_as_integer(tmp_db):
    tmp_db.create_booking_group(description="X", total_amount=119.99)
    con = sqlite3.connect(tmp_db.db_name)
    raw = con.execute("SELECT TotalAmount FROM BookingGroups").fetchone()[0]
    con.close()
    assert isinstance(raw, int)
    assert raw == 1199900


def test_total_amount_none_stays_none(tmp_db):
    gid = tmp_db.create_booking_group(description="ohne Betrag")
    grp = next(g for g in tmp_db.fetch_booking_groups() if g[0] == gid)
    assert grp[3] is None


def test_update_total_amount(tmp_db):
    gid = tmp_db.create_booking_group(description="X", total_amount=10.00)
    tmp_db.update_booking_group(gid, "X", total_amount=42.50)
    grp = next(g for g in tmp_db.fetch_booking_groups() if g[0] == gid)
    assert grp[3] == Decimal("42.5000")


def test_update_to_none(tmp_db):
    gid = tmp_db.create_booking_group(description="X", total_amount=10.00)
    tmp_db.update_booking_group(gid, "X", total_amount=None)
    grp = next(g for g in tmp_db.fetch_booking_groups() if g[0] == gid)
    assert grp[3] is None
