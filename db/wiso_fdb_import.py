# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Import aus der WISO-Mein-Büro-Datenbank.

Gegenstück zu :mod:`db.wiso_import` (CSV/Text), aber aus der Firebird-Datei
selbst. Der Unterschied ist nicht die Menge, sondern die Genauigkeit: die
Datenbank nennt mit ``ACCOUNTINGID`` ausdrücklich, welche Teilbuchungen
zusammengehören. Sie landet in ``Bookings.SourceGroup`` und ersetzt dort das
Raten über Beleg-Nr. und Datum.

Das Lesen der Datei erledigt :mod:`importers.wiso_fdb`; hier steht nur die
Abbildung auf ``Bookings``, ``Assets`` und ``AssetDepreciations``.
"""
from money import to_minor


class WisoFdbImportMixin:
    """Buchungssätze und Anlagen aus einer ``.FDB``-Datei übernehmen."""

    def import_wiso_fdb(self, path, standard_chart_path=None,
                        with_assets=True) -> dict:
        """WISO-Mandantendatenbank importieren.

        Args:
            path: ``DB1.FDB`` des Mandanten.
            standard_chart_path: ``DB0.FDB`` – liefert Konten, die im
                Mandantenrahmen fehlen. Optional.
            with_assets: Anlagenverzeichnis samt AfA-Plan mitnehmen.

        Returns:
            dict mit ``imported``, ``skipped``, ``assets``, ``depreciations``,
            ``tax_rows_skipped``, ``memo_rows_skipped``, ``missing_coa`` und
            ``errors``.
        """
        from importers.wiso_fdb import WisoDatabase

        result = {'imported': 0, 'skipped': 0, 'assets': 0,
                  'depreciations': 0, 'tax_rows_skipped': 0,
                  'memo_rows_skipped': 0, 'missing_coa': {},
                  'unresolved_accounts': 0, 'errors': []}
        try:
            wiso = WisoDatabase(path, standard_chart_path)
        except Exception as exc:                       # defekte/fremde Datei
            result['errors'].append(f'{path}: {exc}')
            return result

        with wiso:
            data = wiso.read(self._liquid_skr_accounts())
            result['tax_rows_skipped'] = data.tax_rows_skipped
            result['memo_rows_skipped'] = data.memo_rows_skipped
            result['missing_coa'] = dict(sorted(
                data.unmapped_accounts.items(), key=lambda kv: -kv[1]))
            self._insert_wiso_bookings(data.bookings, result)
            if with_assets:
                self._insert_wiso_assets(data.assets, result)
        return result

    # ------------------------------------------------------------------
    def _liquid_skr_accounts(self):
        """SKR-Nummern der eigenen Zahlungskonten – sie bestimmen das Vorzeichen."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                'SELECT SKRAccount FROM Accounts WHERE SKRAccount IS NOT NULL'
            ).fetchall()
        finally:
            conn.close()
        return {row[0] for row in rows}

    def _coa_map(self, cursor):
        cursor.execute('SELECT AccountNumber, ID FROM ChartOfAccounts')
        return {row[0]: row[1] for row in cursor.fetchall()}

    def _insert_wiso_bookings(self, bookings, result):
        """Buchungssätze schreiben; ``SourceGroup`` trägt die Split-Klammer.

        Duplikate erkennt die Quell-Id (``SourceGroup`` + Betrag + Konto reicht
        nicht, weil ein Vorgang gleichbetragige Zeilen enthalten darf): schon
        vorhandene ``SourceGroup``-Werte werden übersprungen, damit ein zweiter
        Lauf nichts verdoppelt.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        coa_map = self._coa_map(cursor)
        cursor.execute("SELECT DISTINCT SourceGroup FROM Bookings "
                       "WHERE SourceGroup IS NOT NULL AND SourceGroup <> ''")
        known_groups = {row[0] for row in cursor.fetchall()}

        for booking in bookings:
            if booking.group and booking.group in known_groups:
                result['skipped'] += 1
                continue
            coa_id = coa_map.get(booking.account)
            counter_coa_id = coa_map.get(booking.counter_account)
            if booking.account is not None and coa_id is None:
                result['unresolved_accounts'] += 1
            if booking.counter_account is not None and counter_coa_id is None:
                result['unresolved_accounts'] += 1
            cursor.execute('''
                INSERT INTO Bookings
                    (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate,
                     TaxAmount, Text, DocumentNumber, BookingType, SourceGroup)
                VALUES (?,?,?,?,?,?,?,?,'entry',?)
            ''', (booking.date, coa_id, counter_coa_id,
                  to_minor(booking.amount or 0), booking.tax_rate,
                  self._minor_opt(booking.tax_amount), booking.text,
                  booking.document_number or None, booking.group or None))
            result['imported'] += 1

        conn.commit()
        conn.close()

    def _insert_wiso_assets(self, assets, result):
        """Anlagegüter samt AfA-Plan übernehmen.

        Die Inventarnummer ist der Schlüssel: ein zweiter Lauf aktualisiert
        das vorhandene Anlagegut, statt es zu verdoppeln.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        coa_map = self._coa_map(cursor)

        for asset in assets:
            number = str(asset.number) if asset.number is not None else None
            cursor.execute('SELECT ID FROM Assets WHERE InventoryNumber = ?',
                           (number,))
            row = cursor.fetchone()
            values = (asset.label or 'Anlagegut', coa_map.get(asset.account),
                      asset.purchase_date, to_minor(asset.purchase_price or 0),
                      asset.useful_life_years or 1, asset.sale_date,
                      'sold' if asset.sale_date else 'active')
            if row:
                asset_id = row[0]
                cursor.execute('''
                    UPDATE Assets SET Name=?, COA_ID=?, PurchaseDate=?,
                        PurchasePrice=?, UsefulLifeYears=?, SaleDate=?, Status=?
                    WHERE ID=?
                ''', values + (asset_id,))
            else:
                cursor.execute('''
                    INSERT INTO Assets
                        (Name, COA_ID, PurchaseDate, PurchasePrice,
                         UsefulLifeYears, SaleDate, Status, InventoryNumber,
                         DepreciationMethod)
                    VALUES (?,?,?,?,?,?,?,?,'linear')
                ''', values + (number,))
                asset_id = cursor.lastrowid
            result['assets'] += 1

            for entry in asset.depreciations:
                if entry.year is None:
                    continue
                cursor.execute('''
                    INSERT INTO AssetDepreciations
                        (Asset_ID, Year, DepreciationAmount, BookValue, Status)
                    VALUES (?,?,?,?,'booked')
                    ON CONFLICT(Asset_ID, Year) DO UPDATE SET
                        DepreciationAmount=excluded.DepreciationAmount,
                        BookValue=excluded.BookValue
                ''', (asset_id, entry.year, to_minor(entry.amount or 0),
                      to_minor(entry.book_value or 0)))
                result['depreciations'] += 1

        conn.commit()
        conn.close()
