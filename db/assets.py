"""Database-Mixin: assets."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class AssetsMixin:
    def fetch_asset_categories(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM AssetCategories ORDER BY Name ASC')
        rows = cursor.fetchall()
        conn.close()
        return rows
    def get_asset_category_by_id(self, category_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM AssetCategories WHERE ID = ?', (category_id,))
        row = cursor.fetchone()
        conn.close()
        return row
    def insert_asset_category(self, name, useful_life_years, depreciation_method='linear', coa_id=None, notes=''):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO AssetCategories (Name, UsefulLifeYears, DepreciationMethod, COA_ID, Notes)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, useful_life_years, depreciation_method, coa_id, notes))
        conn.commit()
        conn.close()
    def update_asset_category(self, category_id, name, useful_life_years, depreciation_method, coa_id, notes):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE AssetCategories SET Name=?, UsefulLifeYears=?, DepreciationMethod=?, COA_ID=?, Notes=?
            WHERE ID=?
        ''', (name, useful_life_years, depreciation_method, coa_id, notes, category_id))
        conn.commit()
        conn.close()
    def delete_asset_category(self, category_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM AssetCategories WHERE ID = ?', (category_id,))
        conn.commit()
        conn.close()
    def _generate_inventory_number(self, purchase_date, cursor):
        """Generate next inventory number INV-YY-### using the provided cursor.

        Must be called within the same transaction as the INSERT to be atomic.
        Uses MAX to avoid reusing numbers even if items were deleted.
        """
        year_short = str(purchase_date)[:4][-2:]  # e.g. '25' from '2025-01-01'
        pattern = f"INV-{year_short}-%"
        cursor.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(InventoryNumber, 8) AS INTEGER)), 0) "
            "FROM Assets WHERE InventoryNumber LIKE ?", (pattern,))
        max_num = cursor.fetchone()[0]
        return f"INV-{year_short}-{max_num + 1:03d}"
    def fetch_assets(self, status=None, parent_only=True):
        """Fetch assets with optional status filter. By default only top-level assets."""
        conn = self._get_connection()
        cursor = conn.cursor()
        conditions = []
        params = []
        if status:
            conditions.append('a.Status = ?')
            params.append(status)
        if parent_only:
            conditions.append('a.Parent_ID IS NULL')
        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        cursor.execute(f'''
            SELECT a.*, ac.Name as CategoryName, c.DisplayName as SupplierName
            FROM Assets a
            LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
            LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
            {where}
            ORDER BY a.PurchaseDate DESC
        ''', params)
        rows = cursor.fetchall()
        conn.close()
        # PurchasePrice (7), SalePrice (16) -> Euro-Decimal
        return [self._euro_row(r, 7, 16) for r in rows]
    def get_asset_by_id(self, asset_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT a.*, ac.Name as CategoryName, c.DisplayName as SupplierName
            FROM Assets a
            LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
            LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
            WHERE a.ID = ?
        ''', (asset_id,))
        row = cursor.fetchone()
        conn.close()
        return self._euro_row(row, 7, 16)  # PurchasePrice (7), SalePrice (16)
    def get_asset_children(self, parent_id):
        """Fetch sub-assets (extensions) of a parent asset"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT a.*, ac.Name as CategoryName, c.DisplayName as SupplierName
            FROM Assets a
            LEFT JOIN AssetCategories ac ON a.AssetCategory_ID = ac.ID
            LEFT JOIN Contacts c ON a.Supplier_ID = c.ID
            WHERE a.Parent_ID = ?
            ORDER BY a.PurchaseDate ASC
        ''', (parent_id,))
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 7, 16) for r in rows]  # PurchasePrice (7), SalePrice (16)
    def insert_asset(self, name, purchase_date, purchase_price, useful_life_years,
                     description='', asset_category_id=None, coa_id=None,
                     depreciation_method='linear', serial_number='', location='',
                     supplier_id=None, document_id=None, booking_id=None,
                     notes='', parent_id=None):
        conn = self._get_connection()
        cursor = conn.cursor()
        inv_number = self._generate_inventory_number(purchase_date, cursor)
        cursor.execute('''
            INSERT INTO Assets (InventoryNumber, Name, Description, AssetCategory_ID, COA_ID,
                PurchaseDate, PurchasePrice, UsefulLifeYears, DepreciationMethod,
                SerialNumber, Location, Supplier_ID, Document_ID, Booking_ID,
                Notes, Parent_ID, Status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        ''', (inv_number, name, description, asset_category_id, coa_id,
              purchase_date, to_minor(purchase_price or 0), useful_life_years, depreciation_method,
              serial_number, location, supplier_id, document_id, booking_id,
              notes, parent_id))
        asset_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return asset_id
    def update_asset(self, asset_id, name, purchase_date, purchase_price, useful_life_years,
                     description='', asset_category_id=None, coa_id=None,
                     depreciation_method='linear', serial_number='', location='',
                     supplier_id=None, document_id=None, booking_id=None,
                     notes='', status='active'):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE Assets SET
                Name=?, Description=?, AssetCategory_ID=?, COA_ID=?,
                PurchaseDate=?, PurchasePrice=?, UsefulLifeYears=?, DepreciationMethod=?,
                SerialNumber=?, Location=?, Supplier_ID=?, Document_ID=?, Booking_ID=?,
                Notes=?, Status=?
            WHERE ID=?
        ''', (name, description, asset_category_id, coa_id,
              purchase_date, to_minor(purchase_price or 0), useful_life_years, depreciation_method,
              serial_number, location, supplier_id, document_id, booking_id,
              notes, status, asset_id))
        conn.commit()
        conn.close()
    def sell_asset(self, asset_id, sale_date, sale_price):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE Assets SET SaleDate=?, SalePrice=?, Status='sold' WHERE ID=?
        ''', (sale_date, to_minor(sale_price or 0), asset_id))
        conn.commit()
        conn.close()
    def delete_asset(self, asset_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM Assets WHERE ID = ?', (asset_id,))
        conn.commit()
        conn.close()
    def get_depreciations_for_asset(self, asset_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM AssetDepreciations WHERE Asset_ID = ? ORDER BY Year ASC
        ''', (asset_id,))
        rows = cursor.fetchall()
        conn.close()
        # DepreciationAmount (3), BookValue (4) -> Euro-Decimal
        return [self._euro_row(r, 3, 4) for r in rows]
    def calculate_depreciation_plan(self, purchase_price, purchase_date, useful_life_years,
                                     depreciation_method='linear'):
        """Calculate full AfA plan. Returns list of dicts per year.
        purchase_date: 'YYYY-MM-DD'
        Handles partial first/last year (months-based).
        Degressive: 25% fixed, switches to linear when linear is higher.
        GWG: if purchase_price <= 800, full write-off in purchase year.
        """
        import datetime as dt
        plan = []

        if not purchase_price or not purchase_date or not useful_life_years:
            return plan

        # AfA-Plan rechnet intern in float (gerundete Naeherung). Eingabe kann
        # Euro-Decimal aus der DB-Grenze sein -> normalisieren. Gespeicherte
        # Werte werden ueber to_minor exakt abgelegt (siehe book_depreciation).
        purchase_price = float(purchase_price)

        try:
            pd = dt.date.fromisoformat(str(purchase_date)[:10])
        except Exception:
            return plan

        purchase_year = pd.year
        # Months remaining in purchase year (including purchase month)
        months_in_first_year = 13 - pd.month  # e.g. March → 10 months

        # GWG: Sofortabschreibung up to 800 €
        if purchase_price <= 800.0:
            plan.append({
                'year': purchase_year,
                'book_value_start': purchase_price,
                'depreciation': round(purchase_price, 2),
                'book_value_end': 0.0,
                'method': 'GWG',
            })
            return plan

        if depreciation_method == 'linear':
            annual = purchase_price / useful_life_years
            remaining = purchase_price
            first_depr = round(annual * months_in_first_year / 12, 2)
            plan.append({
                'year': purchase_year,
                'book_value_start': round(remaining, 2),
                'depreciation': first_depr,
                'book_value_end': round(remaining - first_depr, 2),
                'method': 'linear',
            })
            remaining -= first_depr
            year = purchase_year + 1
            while remaining > 0.005:
                depr = round(min(annual, remaining), 2)
                plan.append({
                    'year': year,
                    'book_value_start': round(remaining, 2),
                    'depreciation': depr,
                    'book_value_end': round(remaining - depr, 2),
                    'method': 'linear',
                })
                remaining = round(remaining - depr, 2)
                year += 1

        else:  # degressive
            deg_rate = 0.25  # 25% fixed (§ 7 Abs. 2 EStG 2025)
            linear_annual = purchase_price / useful_life_years
            remaining = purchase_price
            year = purchase_year
            first = True
            while remaining > 0.005:
                deg_depr = remaining * deg_rate
                lin_depr = remaining / max(1, useful_life_years - (year - purchase_year))
                # Switch to linear when linear is higher
                if lin_depr >= deg_depr:
                    method = 'linear'
                    annual_depr = lin_depr
                else:
                    method = 'degressiv'
                    annual_depr = deg_depr
                # Partial first year
                if first:
                    annual_depr = annual_depr * months_in_first_year / 12
                    first = False
                annual_depr = round(min(annual_depr, remaining), 2)
                plan.append({
                    'year': year,
                    'book_value_start': round(remaining, 2),
                    'depreciation': annual_depr,
                    'book_value_end': round(remaining - annual_depr, 2),
                    'method': method,
                })
                remaining = round(remaining - annual_depr, 2)
                year += 1

        return plan
    def get_book_value_at_date(self, asset_id, at_date=None):
        """Calculate current book value of an asset at a given date."""
        import datetime as dt
        asset = self.get_asset_by_id(asset_id)
        if not asset:
            return 0.0
        purchase_price = float(asset[7]) if asset[7] is not None else 0.0   # PurchasePrice
        purchase_date = asset[6]    # PurchaseDate
        useful_life = asset[8]      # UsefulLifeYears
        method = asset[9]           # DepreciationMethod
        if at_date is None:
            at_date = dt.date.today()
        plan = self.calculate_depreciation_plan(purchase_price, purchase_date, useful_life, method)
        current_year = at_date.year
        book_value = purchase_price
        for entry in plan:
            if entry['year'] <= current_year:
                book_value = entry['book_value_end']
            else:
                break
        return max(0.0, book_value)
    def book_depreciation(self, asset_id, year, account_id, coa_id_expense,
                          coa_id_asset, description=None):
        """Book an AfA entry: creates a Booking and marks depreciation as posted."""
        import datetime as dt
        asset = self.get_asset_by_id(asset_id)
        if not asset:
            raise ValueError(f"Asset {asset_id} not found")
        plan = self.calculate_depreciation_plan(
            asset[7], asset[6], asset[8], asset[9])
        year_entry = next((e for e in plan if e['year'] == year), None)
        if not year_entry:
            raise ValueError(f"No depreciation planned for year {year}")
        amount = year_entry['depreciation']
        if not description:
            description = f"AfA {asset[2]} {year} ({asset[1]})"
        booking_date = f"{year}-12-31"
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO Bookings (DateBooking, DateTax, Account_ID, COA_ID,
                Amount, Currency, Text, BookingType, Status)
            VALUES (?, ?, ?, ?, ?, 'EUR', ?, 'entry', 'posted')
        ''', (booking_date, booking_date, account_id, coa_id_expense,
              to_minor(-abs(amount)), description))
        booking_id = cursor.lastrowid
        # Upsert into AssetDepreciations
        cursor.execute('''
            INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Booking_ID, Status, BookedAt)
            VALUES (?, ?, ?, ?, ?, 'posted', CURRENT_TIMESTAMP)
            ON CONFLICT(Asset_ID, Year) DO UPDATE SET
                Booking_ID=excluded.Booking_ID, Status='posted', BookedAt=CURRENT_TIMESTAMP,
                DepreciationAmount=excluded.DepreciationAmount, BookValue=excluded.BookValue
        ''', (asset_id, year, to_minor(amount), to_minor(year_entry['book_value_end']), booking_id))
        conn.commit()
        conn.close()
        return booking_id
