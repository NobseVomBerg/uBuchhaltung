# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: assets."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from .matching import ASSET_ACCOUNT_RANGE, PL_ACCOUNT_RANGE

# Gebucht heißt: die Buchung gibt es – oder der WISO-Import hat das Jahr als gebucht
# übernommen (Status 'booked', ohne Buchung). Eine Zeile 'posted' ohne vorhandene Buchung
# (z. B. per SQL-Konsole gelöscht) gilt als nicht gebucht. Festes SQL-Stück, kein Wert.
_DEPRECIATION_BOOKED = ("(d.Status = 'booked' "
                        "OR EXISTS (SELECT 1 FROM Bookings b WHERE b.ID = d.Booking_ID))")


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
        """Anlage löschen – nur ohne gebuchte AfA und ohne Unteranlagen.

        Buchungen werden nie stillschweigend mitgelöscht (sie blieben sonst verwaist
        stehen oder verschwänden ohne Spur): Wer eine Anlage mit gebuchter AfA löschen
        will, löscht erst die AfA-Buchungen. Als gebucht zählt nur ein Jahr, dessen Buchung
        es gibt; Zeilen des AfA-Plans ohne Buchung (WISO-Import, noch nicht gebuchte oder
        verwaiste Jahre) hindern das Löschen nicht. ``ValueError`` mit Klartext."""
        conn = self._get_connection()
        try:
            years = [row[0] for row in conn.execute(
                'SELECT d.Year FROM AssetDepreciations d WHERE d.Asset_ID = ? '
                'AND EXISTS (SELECT 1 FROM Bookings b WHERE b.ID = d.Booking_ID) '
                'ORDER BY d.Year', (asset_id,))]
            if years:
                raise ValueError(
                    'Für diese Anlage ist AfA gebucht (' + ', '.join(str(y) for y in years)
                    + ') – erst die AfA-Buchungen löschen.')
            if conn.execute('SELECT 1 FROM Assets WHERE Parent_ID = ? LIMIT 1',
                            (asset_id,)).fetchone():
                raise ValueError('Die Anlage hat Erweiterungen (Unteranlagen) – '
                                 'erst diese löschen.')
            conn.execute('DELETE FROM Assets WHERE ID = ?', (asset_id,))
            conn.commit()
        finally:
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
    def get_booked_depreciation_years(self, asset_id) -> set:
        """Jahre, in denen die AfA der Anlage gebucht ist: die Buchung gibt es oder das Jahr
        stammt als 'booked' aus dem WISO-Import. Eine Zeile 'posted' ohne vorhandene Buchung
        gilt als nicht gebucht; ``book_depreciation`` überschreibt sie beim Buchen."""
        conn = self._get_connection()
        try:
            return {row[0] for row in conn.execute(
                f'SELECT d.Year FROM AssetDepreciations d WHERE d.Asset_ID = ? '
                f'AND {_DEPRECIATION_BOOKED}', (asset_id,))}
        finally:
            conn.close()
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
    def resolve_asset_coa(self, asset, coa_id_asset=None):
        """Anlagekonto (COA-ID) einer Anlage: das übergebene, sonst das der Anlage, sonst
        das ihrer AfA-Kategorie; ``None``, wenn nichts davon gepflegt ist.
        ``asset`` ist eine Zeile aus ``get_asset_by_id``."""
        if coa_id_asset:
            return coa_id_asset
        if asset[5]:                          # Assets.COA_ID
            return asset[5]
        if asset[4]:                          # Assets.AssetCategory_ID
            category = self.get_asset_category_by_id(asset[4])
            if category and category[4]:      # AssetCategories.COA_ID
                return category[4]
        return None
    def _require_coa_in_range(self, coa_id, number_range, label):
        """``ValueError`` mit Klartext, wenn das Konto nicht im Nummernbereich liegt
        (``label``: „Aufwandskonto“ bzw. „Anlagekonto“)."""
        conn = self._get_connection()
        try:
            row = conn.execute('SELECT AccountNumber, Name FROM ChartOfAccounts WHERE ID = ?',
                               (coa_id,)).fetchone()
        finally:
            conn.close()
        if row is None or not number_range[0] <= row[0] <= number_range[1]:
            shown = f'Konto {row[0]} {row[1]}' if row else f'Konto-ID {coa_id}'
            raise ValueError(f'{shown} ist kein {label} '
                             f'(erlaubt: Konten {number_range[0]}–{number_range[1]})')
    def book_depreciation(self, asset_id, year, account_id=None, coa_id_expense=None,
                          coa_id_asset=None, description=None):
        """AfA eines Jahres buchen: Umbuchung „Aufwand an Anlagekonto“.

        In der EÜR wird direkt abgeschrieben, ohne Wertberichtigung. Der Satz ist ein
        eigenständiger Buchungssatz ohne Konto (``Account_ID`` leer, kein Geldfluss):
        COA = Aufwandskonto, Gegenkonto = Anlagekonto, Betrag POSITIV (COA im Soll).
        ``classify_entry`` ordnet ihn als Umbuchung ein, der DATEV-Export gibt ihn
        unverändert als „Aufwand an Anlagekonto“ aus. ``account_id`` wird nicht mehr
        verwendet; der Parameter bleibt für bestehende Aufrufe.

        Das Anlagekonto kommt aus ``coa_id_asset``, sonst aus der Anlage oder ihrer
        Kategorie (``resolve_asset_coa``). Das Aufwandskonto muss ein Erfolgskonto
        (``PL_ACCOUNT_RANGE``), das Anlagekonto ein Anlagekonto (``ASSET_ACCOUNT_RANGE``)
        sein – sonst könnte ein Satz wie „6220 an 1460“ entstehen, den die EÜR nicht als
        AfA erkennt. Nach dem Abgang der Anlage (``SaleDate``) gibt es keine AfA mehr; die
        zeitanteilige AfA im Abgangsjahr selbst plant ``calculate_depreciation_plan`` noch
        nicht.

        Je Anlage und Jahr gibt es nur eine Buchung: Ist das Jahr schon gebucht (die
        Buchung gibt es, oder es kam als 'booked' aus dem WISO-Import), kommt ein
        ``ValueError``. Eine Zeile 'posted', deren Buchung fehlt, gilt als nicht gebucht
        und wird überschrieben. Prüfung und Anlegen laufen in einer Transaktion, die den
        Schreibzugriff sofort sperrt – Doppelklick oder zwei gleichzeitige Anfragen buchen
        nie beide.
        """
        asset = self.get_asset_by_id(asset_id)
        if not asset:
            raise ValueError(f"Asset {asset_id} not found")
        plan = self.calculate_depreciation_plan(
            asset[7], asset[6], asset[8], asset[9])
        year_entry = next((e for e in plan if e['year'] == year), None)
        if not year_entry:
            raise ValueError(f"No depreciation planned for year {year}")
        sale_date = asset[15]                     # Assets.SaleDate
        sale_year = str(sale_date)[:4] if sale_date else ''
        if sale_year.isdigit() and year > int(sale_year):
            raise ValueError(f"AfA {year} liegt nach dem Abgang der Anlage ({sale_date})")
        if not coa_id_expense:
            raise ValueError("Aufwandskonto fehlt")
        coa_id_asset = self.resolve_asset_coa(asset, coa_id_asset)
        if not coa_id_asset:
            raise ValueError("Anlage hat kein Anlagenkonto")
        self._require_coa_in_range(coa_id_expense, PL_ACCOUNT_RANGE, 'Aufwandskonto')
        self._require_coa_in_range(coa_id_asset, ASSET_ACCOUNT_RANGE, 'Anlagekonto')
        amount = year_entry['depreciation']
        if not description:
            description = f"AfA {asset[2]} {year} ({asset[1]})"
        booking_date = f"{year}-12-31"
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute(
                    f'SELECT 1 FROM AssetDepreciations d WHERE d.Asset_ID = ? AND d.Year = ? '
                    f'AND {_DEPRECIATION_BOOKED}', (asset_id, year)).fetchone():
                raise ValueError(f"AfA {year} ist bereits gebucht")
            cursor = conn.execute('''
                INSERT INTO Bookings (DateBooking, DateTax, Account_ID, COA_ID, CounterCOA_ID,
                    Amount, Currency, Text, BookingType, Status)
                VALUES (?, ?, NULL, ?, ?, ?, 'EUR', ?, 'entry', 'posted')
            ''', (booking_date, booking_date, coa_id_expense, coa_id_asset,
                  to_minor(abs(amount)), description))
            booking_id = cursor.lastrowid
            # Upsert into AssetDepreciations
            conn.execute('''
                INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Booking_ID, Status, BookedAt)
                VALUES (?, ?, ?, ?, ?, 'posted', CURRENT_TIMESTAMP)
                ON CONFLICT(Asset_ID, Year) DO UPDATE SET
                    Booking_ID=excluded.Booking_ID, Status='posted', BookedAt=CURRENT_TIMESTAMP,
                    DepreciationAmount=excluded.DepreciationAmount, BookValue=excluded.BookValue
            ''', (asset_id, year, to_minor(amount), to_minor(year_entry['book_value_end']),
                  booking_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return booking_id
