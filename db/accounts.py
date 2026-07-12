# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: accounts."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from .core import coa_id

# Sinnvolle DATEV-Standardkontenrahmen für die Auswahl in der SKR-Verwaltung
# (Rahmen-Nr. → Anzeigename). Andere Werte werden beim Anlegen abgelehnt.
SKR_FRAMEWORKS = {
    3:  'SKR03 – Deutschland (Prozessgliederung)',
    4:  'SKR04 – Deutschland (Abschlussgliederung)',
    7:  'SKR07 – Österreich',
    14: 'SKR14 – Land- und Forstwirtschaft',
    49: 'SKR49 – Vereine, Stiftungen, gGmbH',
    51: 'SKR51 – Kfz-Gewerbe',
    70: 'SKR70 – Hotels und Gaststätten',
}


class AccountsMixin:
    def fetch_chart_of_accounts(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM ChartOfAccounts')
        rows = cursor.fetchall()
        conn.close()
        return rows
    def insert_chart_of_accounts(self, framework, account_number, name, description,
                                 is_standard=0, private_share_percent=0, show_in_menu=1):
        """Neues SKR-Konto anlegen. ID wird aus Rahmen+Nummer berechnet.

        Returns True bei Erfolg, False wenn die Nummer im Rahmen schon existiert.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO ChartOfAccounts
                    (ID, Framework, AccountNumber, Name, Description, IsStandard, PrivateSharePercent, ShowInMenu)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (coa_id(framework, account_number), framework, account_number, name, description,
                  is_standard, private_share_percent or 0, 1 if show_in_menu else 0))
            conn.commit()
            return True
        except sqlite3.IntegrityError as e:
            print("Error inserting into ChartOfAccounts:", e)
            conn.rollback()
            return False
        finally:
            conn.close()
    def update_chart_of_accounts(self, id, framework, account_number, name, description,
                                 private_share_percent=None, show_in_menu=None,
                                 is_standard=None):
        """SKR-Konto aktualisieren. Rahmen/Nummer (und damit die ID) sind fix.

        PrivateSharePercent, ShowInMenu und IsStandard sind für ALLE Konten
        editierbar, Name/Description nur für Nicht-Standard-Konten. IsStandard
        wird VOR Name/Description gesetzt: wer den Standard-Haken entfernt,
        kann im selben Schritt Name/Gruppe ändern (und das Konto anschließend
        löschen).
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            # Für ALLE Konten editierbar (auch Standard)
            if private_share_percent is not None:
                cursor.execute(
                    'UPDATE ChartOfAccounts SET PrivateSharePercent = ? WHERE ID = ?',
                    (private_share_percent, id))
            if show_in_menu is not None:
                cursor.execute(
                    'UPDATE ChartOfAccounts SET ShowInMenu = ? WHERE ID = ?',
                    (1 if show_in_menu else 0, id))
            if is_standard is not None:
                cursor.execute(
                    'UPDATE ChartOfAccounts SET IsStandard = ? WHERE ID = ?',
                    (1 if is_standard else 0, id))
            # Name/Description nur für Nicht-Standard-Konten (Rahmen/Nummer bleiben fix)
            cursor.execute('''
                UPDATE ChartOfAccounts
                SET Name = ?, Description = ?
                WHERE ID = ? AND IsStandard = 0
            ''', (name, description, id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating ChartOfAccounts:", e)
            conn.rollback()
        finally:
            conn.close()
    def delete_chart_of_accounts(self, id):
        """Nicht-Standard-Konto löschen. Standard-Konten bleiben unberührt.

        Returns True, wenn eine Zeile gelöscht wurde.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('DELETE FROM ChartOfAccounts WHERE ID = ? AND IsStandard = 0', (id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()
    def coa_is_referenced(self, id):
        """True, wenn das Konto in Buchungen oder Anlagen referenziert wird."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                'SELECT 1 FROM Bookings WHERE COA_ID = ? OR CounterCOA_ID = ? LIMIT 1', (id, id))
            if cursor.fetchone():
                return True
            cursor.execute('SELECT 1 FROM AssetCategories WHERE COA_ID = ? LIMIT 1', (id,))
            if cursor.fetchone():
                return True
            cursor.execute('SELECT 1 FROM Assets WHERE COA_ID = ? LIMIT 1', (id,))
            return cursor.fetchone() is not None
        finally:
            conn.close()
    def next_free_account_number(self, framework, start):
        """Kleinste freie Kontonummer >= start im angegebenen Rahmen."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                'SELECT AccountNumber FROM ChartOfAccounts WHERE Framework = ?', (framework,))
            taken = {row[0] for row in cursor.fetchall()}
        finally:
            conn.close()
        n = int(start)
        while n in taken:
            n += 1
        return n
    def toggle_coa_show_in_menu(self, id):
        """ShowInMenu eines Kontos umschalten. Returns neuer Wert (0/1) oder None."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT ShowInMenu FROM ChartOfAccounts WHERE ID = ?', (id,))
            row = cursor.fetchone()
            if row is None:
                return None
            new_val = 0 if row[0] else 1
            cursor.execute('UPDATE ChartOfAccounts SET ShowInMenu = ? WHERE ID = ?', (new_val, id))
            conn.commit()
            return new_val
        finally:
            conn.close()
    def fetch_accounts(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Accounts ORDER BY IsCash DESC, Name ASC')
        rows = cursor.fetchall()
        conn.close()
        return rows
    def get_account_by_id(self, account_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Accounts WHERE ID = ?', (account_id,))
        row = cursor.fetchone()
        conn.close()
        return row
    def insert_account(self, name, holder, number, bic, bank_name, is_cash=0, skr_account=None):
        # Kassenkonten bekommen standardmäßig SKR 1460 (Verrechnungskonto)
        if is_cash and not skr_account:
            skr_account = 1460
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                INSERT INTO Accounts (Name, Owner, Number, BIC, BankName, IsCash, SKRAccount)
                VALUES (?, ?, ?, ?, ?, ?, ?)'''
        params = (name, holder, number, bic, bank_name, is_cash, skr_account)
        try:
            cursor.execute(sql_template, params)
            conn.commit()
            # Log SQL after successful commit
            self._log_sql(sql_template, params, "Insert new account")
        except sqlite3.IntegrityError as e:
            print("Error inserting account:", e)
            conn.rollback()
        finally:
            conn.close()
    def update_account(self, account_id, name, holder, number, bic, bank_name, skr_account=None):
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                UPDATE Accounts
                SET Name = ?, Owner = ?, Number = ?, BIC = ?, BankName = ?, SKRAccount = ?
                WHERE ID = ?'''
        params = (name, holder, number, bic, bank_name, skr_account, account_id)
        try:
            cursor.execute(sql_template, params)
            conn.commit()
            # Log SQL after successful commit
            self._log_sql(sql_template, params, "Update account")
        except sqlite3.IntegrityError as e:
            print("Error updating account:", e)
            conn.rollback()
        finally:
            conn.close()
    def delete_account(self, account_id):
        conn = self._get_connection()
        cursor = conn.cursor()
        # Only allow deletion if it's not the Kasse account
        cursor.execute('DELETE FROM Accounts WHERE ID = ? AND IsCash = 0', (account_id,))
        conn.commit()
        conn.close()
    def fetch_bookings_range(self, date_from: str, date_to: str):
        """Buchungen eines Datumsbereichs für den DATEV-Export laden.

        Args:
            date_from: 'YYYY-MM-DD' – einschließlich
            date_to:   'YYYY-MM-DD' – einschließlich

        Returns:
            List of tuples (SELECT * FROM Bookings ORDER BY DateBooking)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM Bookings WHERE DateBooking >= ? AND DateBooking <= ? '
            "AND BookingType != 'bank' "
            'ORDER BY DateBooking ASC',
            (date_from, date_to),
        )
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 11, 14) for r in rows]  # Amount(11), TaxAmount(14)
    def get_coa_id_to_number_map(self) -> dict:
        """Liefert {coa_id: account_number} für alle ChartOfAccounts-Einträge."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT ID, AccountNumber FROM ChartOfAccounts')
        result = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()
        return result
    def get_coa_id_by_account_number(self, account_number, framework: int = None):
        """COA-ID anhand der Kontonummer nachschlagen.

        Args:
            account_number: SKR-Kontonummer (int oder str)
            framework:      Kontenrahmen-Nr. (z.B. 3, 4, 7) – optional

        Returns:
            int COA-ID oder None
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        if framework is not None:
            cursor.execute(
                'SELECT ID FROM ChartOfAccounts WHERE AccountNumber=? AND Framework=? LIMIT 1',
                (int(account_number), int(framework)),
            )
        else:
            cursor.execute(
                'SELECT ID FROM ChartOfAccounts WHERE AccountNumber=? LIMIT 1',
                (int(account_number),),
            )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    def update_bookings_date_tax_batch(self, booking_ids: list, date_tax: str):
        """DateTax für mehrere Buchungen auf einmal setzen (nach DATEV-Export).

        Args:
            booking_ids: Liste von Booking-IDs
            date_tax:    'YYYY-MM-DD'
        """
        if not booking_ids:
            return
        conn = self._get_connection()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(booking_ids))
        cursor.execute(
            f'UPDATE Bookings SET DateTax=? WHERE ID IN ({placeholders})',
            [date_tax] + list(booking_ids),
        )
        conn.commit()
        conn.close()
    def get_account_id_by_skr(self, skr_number: int):
        """Account-ID anhand der SKRAccount-Nummer nachschlagen.

        Args:
            skr_number: SKR-Kontonummer (z.B. 1810, 1460)

        Returns:
            int Account-ID oder None
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM Accounts WHERE SKRAccount=? LIMIT 1', (int(skr_number),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
