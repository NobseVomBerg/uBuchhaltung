# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Phase 1d (Decimal-Migration): Anlagen-Geldspalten als Festkomma-Integer.
PurchasePrice/SalePrice (Assets), DepreciationAmount/BookValue (AssetDepreciations).
Der AfA-Plan rechnet intern float; gespeicherte Werte sind exakt (to_minor)."""
import sqlite3
from decimal import Decimal


def test_purchase_price_roundtrip(tmp_db):
    aid = tmp_db.insert_asset(name="Laptop", purchase_date="2025-03-01",
                              purchase_price=1234.56, useful_life_years=3)
    asset = tmp_db.get_asset_by_id(aid)
    assert asset[7] == Decimal("1234.5600")  # Index 7 = PurchasePrice


def test_purchase_price_stored_as_integer(tmp_db):
    tmp_db.insert_asset(name="Laptop", purchase_date="2025-03-01",
                        purchase_price=1234.56, useful_life_years=3)
    con = sqlite3.connect(tmp_db.db_name)
    raw = con.execute("SELECT PurchasePrice FROM Assets").fetchone()[0]
    con.close()
    assert isinstance(raw, int)
    assert raw == 12345600


def test_fetch_assets_returns_decimal(tmp_db):
    tmp_db.insert_asset(name="X", purchase_date="2025-01-01",
                        purchase_price=999.99, useful_life_years=5)
    assets = tmp_db.fetch_assets()
    assert assets[0][7] == Decimal("999.9900")


def test_sell_asset_price_roundtrip(tmp_db):
    aid = tmp_db.insert_asset(name="Auto", purchase_date="2025-01-01",
                              purchase_price=20000.00, useful_life_years=6)
    tmp_db.sell_asset(aid, "2026-06-01", 8500.50)
    asset = tmp_db.get_asset_by_id(aid)
    assert asset[16] == Decimal("8500.5000")  # Index 16 = SalePrice
    con = sqlite3.connect(tmp_db.db_name)
    raw = con.execute("SELECT SalePrice FROM Assets").fetchone()[0]
    con.close()
    assert raw == 85005000


def test_depreciation_plan_accepts_decimal_input(tmp_db):
    # get_asset_by_id liefert Decimal; calculate_depreciation_plan muss damit umgehen
    aid = tmp_db.insert_asset(name="Maschine", purchase_date="2025-01-01",
                              purchase_price=10000.00, useful_life_years=5)
    asset = tmp_db.get_asset_by_id(aid)
    plan = tmp_db.calculate_depreciation_plan(asset[7], asset[6], asset[8], asset[9])
    assert len(plan) == 5
    assert plan[0]['depreciation'] == 2000.0          # 10000 / 5, volles erstes Jahr
    assert sum(e['depreciation'] for e in plan) == 10000.0


def test_degressive_plan_with_decimal_input(tmp_db):
    # Degressiver Zweig nutzt 0.25-Satz – darf mit Decimal-Eingabe nicht brechen
    aid = tmp_db.insert_asset(name="Geraet", purchase_date="2025-01-01",
                              purchase_price=10000.00, useful_life_years=5,
                              depreciation_method='degressiv')
    asset = tmp_db.get_asset_by_id(aid)
    plan = tmp_db.calculate_depreciation_plan(asset[7], asset[6], asset[8], asset[9])
    assert plan[0]['depreciation'] == 2500.0          # 25% von 10000 im ersten Jahr


def test_book_depreciation_stores_integer(db_with_coa):
    db = db_with_coa
    acct_id = db.fetch_accounts()[0][0]
    coas = db.fetch_chart_of_accounts()
    coa_exp, coa_asset = coas[0][0], coas[1][0]
    aid = db.insert_asset(name="Maschine", purchase_date="2025-01-15",
                          purchase_price=10000.00, useful_life_years=5)
    db.book_depreciation(aid, 2025, acct_id, coa_exp, coa_asset)
    deps = db.get_depreciations_for_asset(aid)
    assert len(deps) == 1
    assert deps[0][3] == Decimal("2000.0000")   # DepreciationAmount (Index 3)
    assert deps[0][4] == Decimal("8000.0000")   # BookValue (Index 4)
    con = sqlite3.connect(db.db_name)
    raw = con.execute("SELECT DepreciationAmount, BookValue FROM AssetDepreciations").fetchone()
    con.close()
    assert isinstance(raw[0], int) and isinstance(raw[1], int)
    assert raw == (20000000, 80000000)
