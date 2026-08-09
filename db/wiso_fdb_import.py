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
from db.core import coa_id
from money import to_minor

#: uBuchhaltung rechnet in SKR04.
SKR04 = 4


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
                  'memo_rows_skipped': 0, 'missing_coa': {}, 'created_coa': {},
                  'unresolved_accounts': 0, 'blocker': [], 'hints': [],
                  'errors': []}
        try:
            wiso = WisoDatabase(path, standard_chart_path)
        except Exception as exc:                       # defekte/fremde Datei
            result['errors'].append(f'{path}: {exc}')
            return result

        with wiso:
            blocker, hints = wiso.check()
            result['hints'] = hints
            if blocker:
                result['blocker'] = blocker
                result['errors'] += blocker
                return result
            data = wiso.read(self._liquid_skr_accounts())
            result['tax_rows_skipped'] = data.tax_rows_skipped
            result['memo_rows_skipped'] = data.memo_rows_skipped
            result['missing_coa'] = {
                number: {'anzahl': count,
                         'bezeichnung': data.unmapped_labels.get(number, '')}
                for number, count in sorted(data.unmapped_accounts.items(),
                                            key=lambda kv: -kv[1])}
            result['created_coa'] = self._create_missing_coa(data)
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

    def _create_missing_coa(self, data):
        """SKR04-Konten anlegen, die der eigene Kontenrahmen noch nicht kennt.

        Hier wird nichts geraten: Nummer **und** Bezeichnung stammen aus WISOs
        eigener Umschlüsselungstabelle. Ohne diesen Schritt verlören Buchungen
        auf Konten, die uBuchhaltung nicht mitbringt, ihre Kontierung und
        stünden in der Übersicht als offen.
        """
        wanted = {}
        for account in data.chart.values():
            if account.skr04:
                wanted.setdefault(account.skr04, account.text)
        used = {number for booking in data.bookings
                for number in (booking.account, booking.counter_account)
                if number is not None}
        used |= {number for asset in data.assets
                 for number in (asset.account, asset.depreciation_account)
                 if number is not None}

        conn = self._get_connection()
        cursor = conn.cursor()
        known = set(self._coa_map(cursor))
        created = {}
        for number in sorted(used - known):
            name = wanted.get(number) or f'SKR04 {number}'
            cursor.execute('''
                INSERT OR IGNORE INTO ChartOfAccounts
                    (ID, Framework, AccountNumber, Name, Description,
                     IsStandard, PrivateSharePercent, ShowInMenu)
                VALUES (?,?,?,?,?,0,0,1)
            ''', (coa_id(SKR04, number), SKR04, number, name[:120],
                  'aus der WISO-Datenbank übernommen'))
            created[number] = name
        conn.commit()
        conn.close()
        return created

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
            coa = coa_map.get(booking.account)
            counter_coa = coa_map.get(booking.counter_account)
            if booking.account is not None and coa is None:
                result['unresolved_accounts'] += 1
            if booking.counter_account is not None and counter_coa is None:
                result['unresolved_accounts'] += 1
            cursor.execute('''
                INSERT INTO Bookings
                    (DateBooking, COA_ID, CounterCOA_ID, Amount, TaxRate,
                     TaxAmount, Text, DocumentNumber, BookingType, SourceGroup)
                VALUES (?,?,?,?,?,?,?,?,'entry',?)
            ''', (booking.date, coa, counter_coa,
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
            # Anschaffung und Verkauf stehen beide **netto** in den Büchern –
            # nur so ist der Vergleich mit dem Restbuchwert aussagekräftig.
            values = (asset.label or 'Anlagegut', coa_map.get(asset.account),
                      asset.purchase_date, to_minor(asset.purchase_price or 0),
                      asset.useful_life_years or 1, asset.sale_date,
                      self._minor_opt(asset.sale_price),
                      'sold' if asset.sale_date else 'active')
            if row:
                asset_id = row[0]
                cursor.execute('''
                    UPDATE Assets SET Name=?, COA_ID=?, PurchaseDate=?,
                        PurchasePrice=?, UsefulLifeYears=?, SaleDate=?,
                        SalePrice=?, Status=?
                    WHERE ID=?
                ''', values + (asset_id,))
            else:
                cursor.execute('''
                    INSERT INTO Assets
                        (Name, COA_ID, PurchaseDate, PurchasePrice,
                         UsefulLifeYears, SaleDate, SalePrice, Status,
                         InventoryNumber, DepreciationMethod)
                    VALUES (?,?,?,?,?,?,?,?,?,'linear')
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
