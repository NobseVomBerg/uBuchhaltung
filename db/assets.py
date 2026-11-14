# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: assets."""
import datetime
import sqlite3
import os
import json
from decimal import Decimal
from money import FACTOR, to_minor, from_minor, round_minor
from .matching import ASSET_ACCOUNT_RANGE, FORM_TRANSFER, PL_ACCOUNT_RANGE, depreciation_effect

# Gebucht heißt: die Buchung gibt es – oder der WISO-Import hat das Jahr als gebucht
# übernommen (Status 'booked', ohne Buchung). Eine Zeile 'posted' ohne vorhandene Buchung
# (z. B. per SQL-Konsole gelöscht) gilt als nicht gebucht. Festes SQL-Stück, kein Wert.
_DEPRECIATION_BOOKED = ("(d.Status = 'booked' "
                        "OR EXISTS (SELECT 1 FROM Bookings b WHERE b.ID = d.Booking_ID))")

# Eine Unter-Anlage ``c`` zählt zu ihrer Hauptanlage (``counts_toward_parent``), wenn sie gar keine eigene Zeile
# in ``AssetDepreciations`` hat – gleich welchen Status: Löscht der Nutzer ihre einzige AfA-Buchung, bleibt die
# Planzeile als 'planned' stehen, und sie bleibt eine eigene Anlage – und keine eigenen Unter-Anlagen. Hauptanlage,
# Datum und Preis prüfen ``_counting_extension_on`` und ``_additions_on``. Festes SQL-Stück, kein Wert.
_COUNTS_TOWARD_PARENT = (
    "c.Parent_ID IS NOT NULL AND c.Parent_ID <> c.ID "
    "AND NOT EXISTS (SELECT 1 FROM AssetDepreciations d WHERE d.Asset_ID = c.ID) "
    "AND NOT EXISTS (SELECT 1 FROM Assets g WHERE g.Parent_ID = c.ID)")

# Ein Cent in Minor Units: Der Plan rechnet in ganzen Cent.
_CENT = FACTOR // 100

# Methode der gebuchten Jahre in der Fortschreibung des Plans (``depreciation_plan``).
METHOD_BOOKED = 'gebucht'


def _cents(minor):
    """Minor Units -> ganze Cent, kaufmännisch gerundet."""
    return round_minor(minor, 2) // _CENT


def _euro(cents):
    """Ganze Cent -> Euro als float auf zwei Stellen (die Seiten rechnen mit float)."""
    return float(from_minor(cents * _CENT))


def _spread(rest, months):
    """``rest`` (ganze Cent, > 0) im Verhältnis der ``months`` (je Jahr > 0) auf die Jahre verteilen.

    Je Jahr kaufmännisch gerundet, ganzzahlig (``rest * m / M``, halb auf); das letzte Jahr bekommt
    den Rest, sodass die Summe genau ``rest`` ist. Überschießt die Rundung den Rest, wird das letzte
    Jahr auf 0 begrenzt und der Überschuss von den Jahren davor abgezogen – kein Jahr wird negativ."""
    total = sum(months)
    amounts = [(rest * m * 2 + total) // (2 * total) for m in months]
    amounts[-1] = rest - sum(amounts[:-1])
    excess = -amounts[-1] if amounts[-1] < 0 else 0
    if excess:
        amounts[-1] = 0
        for i in range(len(amounts) - 2, -1, -1):
            cut = min(excess, amounts[i])
            amounts[i] -= cut
            excess -= cut
            if not excess:
                break
    return amounts


def _iso_day(value):
    """Das Datum aus den ersten zehn Zeichen (so liest es auch ``calculate_depreciation_plan``);
    ``None``, wenn sich keines lesen lässt."""
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _disposal_year(sale_date):
    """Das Abgangsjahr: die ersten vier Zeichen des Abgangsdatums, wenn sie ASCII-Ziffern sind; sonst
    ``None`` (ältere Einträge können ein Datum in anderer Schreibweise tragen)."""
    text = str(sale_date)[:4] if sale_date else ''
    return int(text) if text.isascii() and text.isdigit() else None


def _extension(purchase_date, price):
    """``(Tag, Cent)`` einer Unter-Anlage, die zählen darf: lesbares Anschaffungsdatum und Preis über
    0; sonst ``None``."""
    day, cents = _iso_day(purchase_date), _cents(price or 0)
    return (day, cents) if day is not None and cents > 0 else None


def _plannable(purchase_date, price, useful_life):
    """Kann die Anlage einen AfA-Plan tragen (``calculate_depreciation_plan``)? Nur dann hängt sich eine
    Erweiterung an sie: lesbares Kaufdatum, Preis über 0 Cent und Nutzungsdauer über 0."""
    return (_iso_day(purchase_date) is not None and _cents(price or 0) > 0
            and isinstance(useful_life, (int, float)) and useful_life > 0)


def _continue(rest, first, later, begin, end):
    """Den Restbuchwert ``rest`` (ganze Cent) ab dem Jahr ``first`` linear nach Monaten bis zum Planende
    ``end`` verteilen (``begin``/``end``: Monatszähler, ``end`` exklusiv). Liefert ``(Jahr, Rest vor dem
    Jahr, Betrag)`` je Jahr.

    ``later`` (Jahr → Cent) sind spätere Erweiterungen: Im Jahr der Erweiterung kommt ihr Betrag zum Rest
    (als wäre er zu Jahresbeginn aufgewendet) und verteilt sich mit ihm neu bis zum Planende. Ein Rest
    ≤ 0 ergibt in seinem Abschnitt keine Zeilen – die nächste Erweiterung wird trotzdem addiert. Ist
    die Nutzungsdauer abgelaufen, kommt der ganze Rest ins erste Jahr des Abschnitts; ein Jahr, das
    auf 0 Cent rundet, entfällt (die Summe bleibt der Rest)."""
    rows, year = [], first
    pending = sorted((y, c) for y, c in later.items() if y > first)
    last_year = (end - 1) // 12
    while True:
        stop = pending[0][0] if pending else None
        if rest > 0:
            years = list(range(year, last_year + 1))
            if years:
                amounts = _spread(rest, [min(end, y * 12 + 12) - max(begin, y * 12) for y in years])
            else:                                     # Nutzungsdauer abgelaufen: der ganze Rest
                years, amounts = [year], [rest]
            for y, a in zip(years, amounts):
                if stop is not None and y >= stop:
                    break
                if a > 0:
                    rows.append((y, rest, a))
                    rest -= a
        if stop is None:
            break
        rest += pending.pop(0)[1]
        year = stop
    return rows


def _linear_entries(rows):
    """Planzeilen (Form von ``calculate_depreciation_plan``, Methode 'linear') zu den
    ``(Jahr, Rest vor dem Jahr, Betrag)`` von ``_continue``."""
    return [{'year': year, 'book_value_start': _euro(before), 'depreciation': _euro(amount),
             'book_value_end': _euro(before - amount), 'method': 'linear'} for year, before, amount in rows]


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
        """Abgang einer Anlage buchen (Verkauf oder Verschrottung): Abgangsdatum, Erlös, Status 'sold'.

        Eine Erweiterung, die zur Hauptanlage zählt (``counts_toward_parent``), hat keinen eigenen Abgang – ihr
        Wert steckt in der Hauptanlage: ``ValueError`` mit Klartext, nichts wird geschrieben."""
        if self.counts_toward_parent(asset_id):
            raise ValueError('Der Abgang einer Erweiterung lässt sich nicht einzeln buchen – sie gehört zur Hauptanlage.')
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
        verwaiste Jahre) hindern das Löschen nicht.

        Eine Erweiterung, die zur Hauptanlage zählt (``counts_toward_parent``), steckt in deren Plan und in
        ihren Buchungen: Hat die Hauptanlage seit dem Jahr der Erweiterung (nie vor ihrem Kaufjahr) ein
        gebuchtes Jahr (``_DEPRECIATION_BOOKED``, auch ein importiertes), lässt sie sich nicht löschen;
        ohne ein solches Jahr schon. ``ValueError`` mit Klartext."""
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
            extension = self._counting_extension_on(conn, asset_id)
            if extension:
                parent_id, extension_year = extension[:2]
                parent_years = [row[0] for row in conn.execute(
                    f'SELECT d.Year FROM AssetDepreciations d WHERE d.Asset_ID = ? AND d.Year >= ? '
                    f'AND {_DEPRECIATION_BOOKED} ORDER BY d.Year', (parent_id, extension_year))]
                if parent_years:
                    raise ValueError(
                        'Die Hauptanlage hat seit dem Jahr dieser Erweiterung AfA gebucht ('
                        + ', '.join(str(y) for y in parent_years)
                        + ') – die Erweiterung steckt in diesen Buchungen und lässt sich nicht löschen.')
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
    def counts_toward_parent(self, asset_id) -> bool:
        """Zählt die Unter-Anlage (Erweiterung, ``Assets.Parent_ID``) zu ihrer Hauptanlage?

        Nachträgliche Anschaffungskosten erhöhen den Anschaffungswert der Hauptanlage, statt eine eigene
        Anlage mit eigenem Plan zu sein (``depreciation_plan``). Sie zählen, wenn alles gilt: Die
        Hauptanlage gibt es (und ist nicht die Anlage selbst) und kann einen Plan haben (lesbares Kaufdatum,
        Preis über 0 Cent, Nutzungsdauer über 0), die Unter-Anlage hat **gar keine eigene Zeile** in
        ``AssetDepreciations`` – gleich welchen Status – und **keine eigenen Unter-Anlagen**, ihr
        Anschaffungsdatum ist ein Datum und ihr Preis über 0. Eine Unter-Anlage mit eigener AfA-Zeile (Bestand,
        WISO-Import; auch eine, deren einzige Buchung gelöscht wurde: ihre Planzeile bleibt ``planned``) oder
        unter einer Hauptanlage ohne Plan bleibt eine eigene Anlage mit eigenem Plan und fließt nicht in die
        Hauptanlage."""
        conn = self._get_connection()
        try:
            return self._counts_toward_parent_on(conn, asset_id)
        finally:
            conn.close()
    def _counts_toward_parent_on(self, conn, asset_id):
        """``counts_toward_parent`` über eine vorhandene Verbindung."""
        return self._counting_extension_on(conn, asset_id) is not None
    def _counting_extension_on(self, conn, asset_id):
        """``(Hauptanlage-ID, Jahr, Cent)`` der Unter-Anlage, wenn sie zur Hauptanlage zählt
        (``counts_toward_parent``), sonst ``None``. Das Jahr ist das ihres Anschaffungsdatums – maßgeblich ist
        der Tag, an dem die Kosten entstanden sind, nicht der der Zahlung –, nie vor dem Kaufjahr der
        Hauptanlage. Die Hauptanlage wird dabei einmal gelesen."""
        row = conn.execute(
            'SELECT c.Parent_ID, c.PurchaseDate, c.PurchasePrice, p.PurchaseDate, p.PurchasePrice, p.UsefulLifeYears '
            'FROM Assets c JOIN Assets p ON p.ID = c.Parent_ID '
            f'WHERE c.ID = ? AND {_COUNTS_TOWARD_PARENT}', (asset_id,)).fetchone()
        if row is None or not _plannable(*row[3:]):
            return None
        extension = _extension(row[1], row[2])
        if extension is None:
            return None
        day, cents = extension
        return row[0], max(day.year, _iso_day(row[3]).year), cents
    def _additions_on(self, conn, asset_id):
        """``(Jahr, Cent)`` der Unter-Anlagen von ``asset_id``, die zu ihr zählen (``counts_toward_parent``).

        Das Jahr ist das des Anschaffungsdatums der Erweiterung – maßgeblich ist der Tag, an dem die Kosten
        entstanden sind, nicht der der Zahlung –, nie vor dem Kaufjahr der Hauptanlage. Kann die Anlage keinen
        Plan haben, zählt keine Unter-Anlage zu ihr. Die Hauptanlage wird einmal gelesen."""
        parent = conn.execute('SELECT PurchaseDate, PurchasePrice, UsefulLifeYears FROM Assets WHERE ID = ?',
                              (asset_id,)).fetchone()
        if parent is None or not _plannable(*parent):
            return []
        bought = _iso_day(parent[0])
        additions = []
        for purchase_date, price in conn.execute(
                f'SELECT c.PurchaseDate, c.PurchasePrice FROM Assets c '
                f'WHERE c.Parent_ID = ? AND {_COUNTS_TOWARD_PARENT} ORDER BY c.ID', (asset_id,)):
            extension = _extension(purchase_date, price)
            if extension:
                day, cents = extension
                additions.append((max(day.year, bought.year), cents))
        return additions
    def acquisition_cost(self, asset_id, year=None) -> float:
        """Anschaffungswert in Euro (zwei Stellen): der Preis der Anlage plus die zählenden Erweiterungen
        (``counts_toward_parent``) mit Jahr ≤ ``year`` (Standard: das laufende Jahr; das Jahr einer Erweiterung
        ist das ihres Anschaffungsdatums, nie vor dem Kaufjahr der Anlage). Für eine Erweiterung selbst ist es
        ihr eigener Preis, eine unbekannte Anlage ergibt 0."""
        if year is None:
            year = datetime.date.today().year
        conn = self._get_connection()
        try:
            row = conn.execute('SELECT PurchasePrice FROM Assets WHERE ID = ?', (asset_id,)).fetchone()
            if row is None:
                return 0.0
            return _euro(_cents(row[0] or 0)
                         + sum(cents for added_year, cents in self._additions_on(conn, asset_id)
                               if added_year <= year))
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
    def depreciation_plan(self, asset_id):
        """AfA-Plan einer Anlage: gebuchte Jahre sind Geschichte, der Rest verteilt sich neu.

        Liefert je Jahr ein dict wie ``calculate_depreciation_plan`` (``year``, ``book_value_start``,
        ``depreciation``, ``book_value_end``, ``method``, Beträge als float auf zwei Stellen) und
        zusätzlich ``booked`` (das Jahr ist gebucht: die Buchung gibt es oder es kam als 'booked' aus
        dem WISO-Import) und ``bookable`` (``book_depreciation`` nimmt das Jahr jetzt an: jedes nicht
        gebuchte Jahr der Formel, in der Fortschreibung nur das erste offene). Unbekannte Anlage: leere
        Liste; ebenso eine Erweiterung, die zur Hauptanlage zählt (``counts_toward_parent``: ihre AfA
        steckt im Plan der Hauptanlage). Gerechnet wird in ganzen Cent.

        **Der Plan endet mit dem Abgangsjahr** (``SaleDate``: die ersten vier Zeichen, wenn sie Ziffern
        sind): Nicht gebuchte Jahre danach entfallen – in der Unverändert-Regel wie in der Fortschreibung –,
        gebuchte bleiben. Das Abgangsjahr selbst steht, wie es ist (die zeitanteilige AfA im Abgangsjahr
        ist offen).

        **Gebucht** ist, was in den Büchern steht: Bei einem von der App gebuchten Jahr (die Planzeile
        zeigt über ``Booking_ID`` auf eine vorhandene Buchung) ist der gebuchte Betrag die Wirkung dieser
        Buchung auf das Anlagekonto (``db.matching.depreciation_effect``: „Aufwand an Anlagekonto“ zählt
        +|Betrag|; dreht der Nutzer die Buchung zur Rückbuchung um, zählt sie negativ und das Jahr gilt
        mit 0 als gebucht) – ändert der Nutzer die Buchung im Editor, folgt der Plan –, bei einem
        importierten Jahr (Status 'booked', keine Buchung) ``DepreciationAmount``.

        **Unverändert:** Gibt es keine gebuchten Jahre, oder stimmt jedes gebuchte Jahr auf den Cent mit
        dem gleichen Jahr des erwarteten Plans überein, ist das Ergebnis genau dieser Plan – ohne
        Erweiterungen ist er ``calculate_depreciation_plan`` (linear, degressiv, GWG): Für eine nur in der
        App geführte Anlage ändert sich nichts.

        **Erweiterungen** (Unter-Anlagen, die zählen – ``counts_toward_parent``) erhöhen den Anschaffungswert
        der Hauptanlage; maßgeblich ist das Anschaffungsdatum der Erweiterung, nicht das der Zahlung. Im Jahr,
        in dem sie entstehen, zählen sie wie zu Jahresbeginn aufgewendet (keine Kürzung nach Monaten): Der
        Restbuchwert plus Erweiterung verteilt sich linear nach Monaten auf die Restnutzungsdauer der
        Hauptanlage. Fällt die Erweiterung ins Anschaffungsjahr der Hauptanlage, ist es die Formel auf
        Anschaffungswert plus Erweiterung (ab dem Kaufmonat der Hauptanlage). Der erwartete Plan ist dann
        die Formel auf den Anschaffungswert samt Erweiterungen des Kaufjahrs bis zum Jahr vor der ersten
        späteren Erweiterung; ab diesem Jahr rechnet ``_continue`` linear über die Restnutzungsdauer – auch bei
        degressiver Methode oder GWG der Hauptanlage (dieselbe Vereinfachung wie in der Fortschreibung), und
        jede weitere Erweiterung kommt in ihrem Jahr zum Rest. Nach dem Ende der Nutzungsdauer steht der
        ganze Betrag im Jahr der Erweiterung.

        **Fortschreibung** (die Geschichte weicht ab: Import, nachträglich geänderter Anschaffungswert,
        Betrag einer Buchung, eine Erweiterung in einem schon gebuchten Jahr): Gebuchte Jahre stehen mit
        ihrem gebuchten Betrag im Plan (Methode ``METHOD_BOOKED``), ihr Buchwert rechnet fortlaufend vom
        heutigen Anschaffungswert (Ende = Anschaffungswert bis zu diesem Jahr einschließlich der
        Erweiterungen minus die gebuchten Beträge bis einschließlich des Jahres, nie unter 0; Anfang = Ende
        plus Betrag) – so schließt das erste offene Jahr an. Der Restbuchwert (Anschaffungswert samt den
        Erweiterungen bis zum ersten offenen Jahr minus alle gebuchten Beträge) verteilt sich linear auf die
        Monate vom ersten offenen Jahr bis zum Planende (Kaufmonat plus Nutzungsdauer; erstes offenes Jahr:
        das Jahr nach dem größten gebuchten Jahr, mindestens das Kaufjahr); spätere Erweiterungen kommen in
        ihrem Jahr zum Rest. Nicht gebuchte Jahre zwischen gebuchten bekommen keine eigene Zeile – ihr Betrag
        steckt im Rest. Ein Rest ≤ 0 ergibt keine offenen Jahre; ist die Nutzungsdauer abgelaufen, kommt der
        ganze Rest ins erste offene Jahr. Ein Jahr, das auf 0 Cent rundet, entfällt (die Summe bleibt der
        Rest), und buchbar ist das erste verbleibende offene Jahr. GWG-Regel und degressive Rechnung gelten
        hier nicht. Fehlt Preis, Kaufdatum oder Nutzungsdauer, bleiben nur die gebuchten Jahre. Buchbar ist
        nur das erste offene Jahr: Ein späteres Jahr zuerst zu buchen machte die Jahre davor zu Lücken.
        """
        conn = self._get_connection()
        try:
            return self._depreciation_plan_on(conn, asset_id)
        finally:
            conn.close()
    def _depreciation_plan_on(self, conn, asset_id):
        """``depreciation_plan`` über eine vorhandene Verbindung – ``book_depreciation`` liest den Plan
        so in seiner Transaktion, nach der Schreibsperre."""
        row = conn.execute('SELECT PurchasePrice, PurchaseDate, UsefulLifeYears, DepreciationMethod, SaleDate '
                           'FROM Assets WHERE ID = ?', (asset_id,)).fetchone()
        if row is None or self._counts_toward_parent_on(conn, asset_id):
            return []                                 # unbekannt – oder eine Erweiterung: ihre AfA steckt in der Hauptanlage
        price, purchase_date, useful_life, method, sale_date = row
        booked = {}
        for year, plan_amount, booking_id, amount, form, number, counter_number in conn.execute(
                f'SELECT d.Year, d.DepreciationAmount, k.ID, k.Amount, k.EntryForm, c.AccountNumber, g.AccountNumber '
                f'FROM AssetDepreciations d LEFT JOIN Bookings k ON k.ID = d.Booking_ID '
                f'LEFT JOIN ChartOfAccounts c ON c.ID = k.COA_ID LEFT JOIN ChartOfAccounts g ON g.ID = k.CounterCOA_ID '
                f'WHERE d.Asset_ID = ? AND {_DEPRECIATION_BOOKED}', (asset_id,)):
            # gebucht ist, was in den Büchern steht: bei einer Buchung ihre Wirkung auf das Anlagekonto (eine
            # Umkehrung zählt negativ, das Jahr bleibt mit 0 gebucht), sonst die Zeile des Imports
            booked[year] = (plan_amount if booking_id is None
                            else max(0, depreciation_effect(form, amount, number, counter_number)))
        additions = {}                                # Jahr -> Cent der zählenden Erweiterungen
        for year, cents in self._additions_on(conn, asset_id):
            additions[year] = additions.get(year, 0) + cents

        def cost_until(year):                         # Anschaffungswert samt Erweiterungen bis zu diesem Jahr (Cent)
            return _cents(price or 0) + sum(cents for added, cents in additions.items() if added <= year)

        bought = _iso_day(purchase_date)
        buy = bought.year if bought else None
        later = {year: cents for year, cents in additions.items() if buy is not None and year > buy}
        # Die Formel rechnet auf den Anschaffungswert samt den Erweiterungen des Kaufjahrs (ab dem Kaufmonat)
        base = (price or 0) + additions.get(buy, 0) * _CENT
        old = self.calculate_depreciation_plan(from_minor(base), purchase_date, useful_life, method)
        if old:                                       # sonst fehlen Preis, Kaufdatum oder Nutzungsdauer
            begin = bought.year * 12 + bought.month - 1                   # erster Planmonat (Monatszähler)
            end = begin + 12 * int(useful_life)                           # Planende, exklusiv
        expected = old
        if old and later:
            # Ab der ersten späteren Erweiterung linear nach Monaten über die Restnutzungsdauer (auch bei
            # degressiver Methode oder GWG der Hauptanlage); davor gilt die Formel
            first_later = min(later)
            kept = [entry for entry in old if entry['year'] < first_later]
            rest = (_cents(base) + later[first_later]
                    - sum(_cents(to_minor(entry['depreciation'])) for entry in kept))
            expected = kept + _linear_entries(_continue(
                rest, first_later, {y: c for y, c in later.items() if y > first_later}, begin, end))
        expected_by_year = {entry['year']: entry for entry in expected}
        if all(year in expected_by_year and amount == to_minor(expected_by_year[year]['depreciation'])
               for year, amount in booked.items()):
            plan = [dict(entry, booked=entry['year'] in booked, bookable=entry['year'] not in booked)
                    for entry in expected]
        else:
            plan, spent = [], 0
            for year, amount in sorted(booked.items()):
                cents = _cents(amount)
                spent += cents
                left = max(0, cost_until(year) - spent)
                plan.append({'year': year, 'book_value_start': _euro(left + cents), 'depreciation': _euro(cents),
                             'book_value_end': _euro(left), 'method': METHOD_BOOKED, 'booked': True,
                             'bookable': False})
            if old:
                first = max(max(booked) + 1, buy)                         # erstes offenes Jahr
                open_rows = _linear_entries(_continue(
                    cost_until(first) - spent, first, {y: c for y, c in additions.items() if y > first}, begin, end))
                plan += [dict(entry, booked=False, bookable=index == 0) for index, entry in enumerate(open_rows)]
        disposal_year = _disposal_year(sale_date)         # der Plan endet mit dem Abgangsjahr; Gebuchtes bleibt
        return [entry for entry in plan if entry['booked'] or disposal_year is None or entry['year'] <= disposal_year]
    def get_book_value_at_date(self, asset_id, at_date=None):
        """Calculate current book value of an asset at a given date.

        Aus ``depreciation_plan``: der Buchwert am Ende des letzten Planjahrs bis zum Jahr des
        Datums, davor der Anschaffungswert. Nach dem Abgang ist er 0.0 – der Abgang hat den Rest ausgebucht:
        Lässt sich ``SaleDate`` als ISO-Datum lesen, ab diesem Tag (am Abgangstag selbst und danach);
        sonst, wenn die ersten vier Zeichen ein Jahr ergeben (``_disposal_year``, wie beim Ende des Plans),
        sobald das Jahr des Datums nach dem Abgangsjahr liegt; sonst gilt kein Abgang. Eine Erweiterung, die
        zur Hauptanlage zählt (``counts_toward_parent``), hat keinen eigenen Wert (0.0): er steckt in der
        Hauptanlage."""
        asset = self.get_asset_by_id(asset_id)
        if not asset:
            return 0.0
        if at_date is None:
            at_date = datetime.date.today()
        sold = _iso_day(asset[15])                                          # SaleDate
        if sold is not None:
            if at_date >= sold:
                return 0.0
        else:
            disposal_year = _disposal_year(asset[15])
            if disposal_year is not None and at_date.year > disposal_year:
                return 0.0
        if self.counts_toward_parent(asset_id):
            return 0.0
        purchase_price = float(asset[7]) if asset[7] is not None else 0.0   # PurchasePrice
        current_year = at_date.year
        book_value = purchase_price
        for entry in self.depreciation_plan(asset_id):
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
        AfA erkennt. Nach dem Abgang der Anlage (``SaleDate``) gibt es keine AfA mehr: Der Plan endet
        mit dem Abgangsjahr, ein Jahr danach lehnt ``book_depreciation`` mit dem Abgang als Begründung
        ab; die zeitanteilige AfA im Abgangsjahr selbst plant ``depreciation_plan`` noch nicht.

        Eine Erweiterung, die zur Hauptanlage zählt (``counts_toward_parent``), hat keine eigene AfA: Ihr
        Betrag steckt im Plan der Hauptanlage, ``book_depreciation`` lehnt sie als Erstes ab. Eine
        Unter-Anlage mit eigener AfA-Zeile (Bestand) oder unter einer Hauptanlage ohne Plan bleibt eine eigene
        Anlage und lässt sich buchen.

        Betrag und Buchwert kommen aus ``depreciation_plan``; ein Jahr, das dort nicht
        vorkommt, wird abgelehnt (``ValueError`` „No depreciation planned …“, nach der Prüfung des
        Abgangs, aber noch vor den Prüfungen der Konten, und erneut mit dem Plan der Transaktion). In der
        Fortschreibung des Plans (die gebuchte Geschichte weicht von der Formel ab) ist nur das
        erste offene Jahr buchbar: Jedes andere lehnt ``book_depreciation`` mit Klartext ab –
        sonst würden die Jahre davor zu Lücken, und ihr Betrag wanderte in die späteren Jahre.
        Für eine nur in der App geführte Anlage gilt jede Reihenfolge.

        Je Anlage und Jahr gibt es nur eine Buchung: Ist das Jahr schon gebucht (die
        Buchung gibt es, oder es kam als 'booked' aus dem WISO-Import), kommt ein
        ``ValueError``. Eine Zeile 'posted', deren Buchung fehlt, gilt als nicht gebucht
        und wird überschrieben. Prüfung und Anlegen laufen in einer Transaktion, die den
        Schreibzugriff sofort sperrt – Doppelklick oder zwei gleichzeitige Anfragen buchen
        nie beide. Der Plan wird erst in dieser Transaktion gelesen: Die Fortschreibung hängt
        an den gebuchten Jahren, und ein anderes Buchen derselben Anlage (oder ein Import)
        zwischen Lesen und Sperre würde den Betrag sonst umrechnen.
        """
        asset = self.get_asset_by_id(asset_id)
        if not asset:
            raise ValueError(f"Asset {asset_id} not found")
        if self.counts_toward_parent(asset_id):
            raise ValueError('Die AfA einer Erweiterung läuft über die Hauptanlage – bitte dort buchen.')
        sale_date = asset[15]                     # Assets.SaleDate
        sale_year = _disposal_year(sale_date)
        if sale_year is not None and year > sale_year:
            # vor der Planprüfung: Der Plan endet mit dem Abgangsjahr und kennt diese Jahre nicht mehr
            raise ValueError(f"AfA {year} liegt nach dem Abgang der Anlage ({sale_date})")
        if not any(e['year'] == year for e in self.depreciation_plan(asset_id)):
            # vor den Prüfungen der Konten; maßgeblich ist der Plan in der Transaktion (unten)
            raise ValueError(f"No depreciation planned for year {year}")
        if not coa_id_expense:
            raise ValueError("Aufwandskonto fehlt")
        coa_id_asset = self.resolve_asset_coa(asset, coa_id_asset)
        if not coa_id_asset:
            raise ValueError("Anlage hat kein Anlagenkonto")
        self._require_coa_in_range(coa_id_expense, PL_ACCOUNT_RANGE, 'Aufwandskonto')
        self._require_coa_in_range(coa_id_asset, ASSET_ACCOUNT_RANGE, 'Anlagekonto')
        if not description:
            description = f"AfA {asset[2]} {year} ({asset[1]})"
        booking_date = f"{year}-12-31"
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            plan = self._depreciation_plan_on(conn, asset_id)
            year_entry = next((e for e in plan if e['year'] == year), None)
            if not year_entry:
                raise ValueError(f"No depreciation planned for year {year}")
            if year_entry['booked']:                  # ``_DEPRECIATION_BOOKED``, in dieser Transaktion gelesen
                raise ValueError(f"AfA {year} ist bereits gebucht")
            if not year_entry['bookable']:            # Fortschreibung: nur das erste offene Jahr
                first = next(e['year'] for e in plan if e['bookable'])
                raise ValueError(f"AfA {year} lässt sich erst buchen, wenn {first} gebucht ist.")
            amount = year_entry['depreciation']
            cursor = conn.execute('''
                INSERT INTO Bookings (DateBooking, DateTax, Account_ID, COA_ID, CounterCOA_ID,
                    Amount, Currency, Text, BookingType, Status, EntryForm)
                VALUES (?, ?, NULL, ?, ?, ?, 'EUR', ?, 'entry', 'posted', ?)
            ''', (booking_date, booking_date, coa_id_expense, coa_id_asset,
                  to_minor(abs(amount)), description, FORM_TRANSFER))
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
