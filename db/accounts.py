# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: accounts."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from .core import DeleteRefused, coa_id, reference_texts

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

# Kassenkonto, das eine neue Kasse bekommt: SKR04 1600 „Kasse“. Das Verrechnungskonto
# 1460 ist Geldtransit (WISO bucht darüber), keine Kasse.
DEFAULT_CASH_ACCOUNT = 1600

BUSINESS_ONLY_ERROR = 'Das Kennzeichen „reines Geschäftskonto“ ist 0 oder 1.'

# So viele IDs gehen in EINE Anweisung (SQLite begrenzt die Zahl der Variablen je Anweisung)
_ID_BLOCK = 500

#: Welche Zeilen von ``Bookings`` der Export des Buchungsstapels für einen Zeitraum liest – und mit ihm die Summen- und Saldenliste
#: (``Database.get_susa``): alle Zeilen außer den Bankbewegungen, deren Datum als Text zwischen den beiden Grenzen liegt (beide gehören
#: dazu). ``{a}`` ist der Tabellenalias mit Punkt (``'e.'``) oder leer. Zwei Parameter: von, bis.
LEDGER_ROWS_WHERE = "{a}DateBooking >= ? AND {a}DateBooking <= ? AND {a}BookingType != 'bank'"


def is_business_only(stored):
    """Gilt ein gespeichertes ``Accounts.BusinessOnly`` als „reines Geschäftskonto“?

    Streng gelesen: Nur die ganze Zahl 0 heißt „gemischt genutzt“. Jeder andere gespeicherte Wert – NULL, Text, 2, eine
    Kommazahl, ein Wahrheitswert – gilt als reines Geschäftskonto: Im Zweifel wird ein Kontoauszug vollständig importiert."""
    return not (type(stored) is int and stored == 0)


def _business_only_value(value):
    """Das Kennzeichen als 0 oder 1 zum Speichern – nur ``True``/``False`` und die ganzen Zahlen 0 und 1; alles andere
    ist ``ValueError``."""
    if isinstance(value, (bool, int)) and value in (0, 1):
        return int(value)
    raise ValueError(BUSINESS_ONLY_ERROR)


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
        """True, wenn das Konto in Buchungen, Anlagen, Steuersätzen (``TaxRates``: Erlös-, Umsatzsteuer- oder Vorsteuerkonto) oder
        Rechnungspositionen (``InvoiceItems.RevenueCoaId``) referenziert wird."""
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
            if cursor.fetchone():
                return True
            cursor.execute('SELECT 1 FROM TaxRates WHERE RevenueCoaId = ? OR VatCoaId = ? OR InputTaxCoaId = ? LIMIT 1', (id, id, id))
            if cursor.fetchone():
                return True
            cursor.execute('SELECT 1 FROM InvoiceItems WHERE RevenueCoaId = ? LIMIT 1', (id,))
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
    def account_allows_selection(self, account_id):
        """Dürfen beim Import eines Kontoauszugs einzelne Umsätze ausgewählt werden? Nur für ein vorhandenes Konto, das
        keine Kasse ist und dessen ``BusinessOnly`` genau 0 trägt (gemischt genutzt); jeder andere Wert gilt als reines
        Geschäftskonto (``is_business_only``). Rein lesend."""
        conn = self._get_connection()
        try:
            row = conn.execute('SELECT IsCash, BusinessOnly FROM Accounts WHERE ID = ?', (account_id,)).fetchone()
        finally:
            conn.close()
        return row is not None and not row[0] and not is_business_only(row[1])
    def count_entries_on_account_numbers(self, numbers):
        """Zahl der Buchungssätze, die eines der Konten (SKR-Nummern) als Konto oder
        Gegenkonto führen."""
        numbers = [n for n in numbers if n is not None]
        if not numbers:
            return 0
        marks = ','.join('?' * len(numbers))
        conn = self._get_connection()
        try:
            return conn.execute(
                f"""SELECT COUNT(*) FROM Bookings b
                    WHERE b.BookingType = 'entry'
                      AND (b.COA_ID IN (SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN ({marks}))
                        OR b.CounterCOA_ID IN (SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN ({marks})))""",
                numbers + numbers).fetchone()[0]
        finally:
            conn.close()
    def ensure_cash_coa(self):
        """COA-ID des Kassenkontos 1600 liefern. Fehlt es (Datenbank älter als der
        Seed-Eintrag), wird es als Standardkonto im dominanten Kontenrahmen nachgelegt
        (Vorbild ``_ensure_small_business_revenue_coa``). Nur aufrufen, wo eine Kasse mit
        1600 angelegt wird – nicht bei Seitenaufrufen."""
        return self._ensure_standard_coa(DEFAULT_CASH_ACCOUNT, 'Kasse', 'Umlaufvermögenskonten')
    def insert_account(self, name, holder, number, bic, bank_name, is_cash=0, skr_account=None, business_only=True):
        """Konto anlegen. ``business_only``: Kennzeichen „reines Geschäftskonto“ (Standard ``True``; ``False`` = gemischt
        genutzt, beim Import lassen sich einzelne Umsätze auswählen) – nur ``True``/``False``/0/1, sonst ``ValueError``."""
        business_only = _business_only_value(business_only)
        # Kassenkonten bekommen standardmäßig SKR 1600 (Kasse); das Konto wird bei Bedarf angelegt
        if is_cash and not skr_account:
            self.ensure_cash_coa()
            skr_account = DEFAULT_CASH_ACCOUNT
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                INSERT INTO Accounts (Name, Owner, Number, BIC, BankName, IsCash, SKRAccount, BusinessOnly)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)'''
        params = (name, holder, number, bic, bank_name, is_cash, skr_account, business_only)
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
    def update_account(self, account_id, name, holder, number, bic, bank_name, skr_account=None, business_only=None):
        """Konto ändern. ``business_only``: ``None`` lässt das Kennzeichen „reines Geschäftskonto“ unverändert, sonst
        ``True``/``False``/0/1 (alles andere ist ``ValueError``, nichts wird geschrieben)."""
        business_only = None if business_only is None else _business_only_value(business_only)
        conn = self._get_connection()
        cursor = conn.cursor()
        sql_template = '''
                UPDATE Accounts
                SET Name = ?, Owner = ?, Number = ?, BIC = ?, BankName = ?, SKRAccount = ?,
                    BusinessOnly = COALESCE(?, BusinessOnly)
                WHERE ID = ?'''
        params = (name, holder, number, bic, bank_name, skr_account, business_only, account_id)
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
        """Bankkonto löschen. Die Kasse lässt sich nie löschen, ein Konto nicht, solange Buchungen, Rechnungen oder
        Angebote darauf verweisen (``DeleteRefused`` mit Klartext, nichts gelöscht). Eine unbekannte ID ist kein
        Fehler. Prüfung und Löschen laufen in einer Transaktion."""
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT IsCash FROM Accounts WHERE ID = ?', (account_id,)).fetchone()
            if row is not None:
                if row[0] == 1:
                    raise DeleteRefused('Die Kasse lässt sich nicht löschen.')
                used = reference_texts(conn, [
                    ('SELECT COUNT(*) FROM Bookings WHERE Account_ID = ?', (account_id,), 'Buchung', 'Buchungen'),
                    ("SELECT COUNT(*) FROM Invoices WHERE DocumentType = 'invoice' AND BankAccountId = ?",
                     (account_id,), 'Rechnung', 'Rechnungen'),
                    ("SELECT COUNT(*) FROM Invoices WHERE DocumentType = 'quote' AND BankAccountId = ?",
                     (account_id,), 'Angebot', 'Angebote'),
                ])
                if used:
                    raise DeleteRefused('Das Konto lässt sich nicht löschen, solange andere Daten auf es verweisen: '
                                        + ', '.join(used) + '.')
                conn.execute('DELETE FROM Accounts WHERE ID = ?', (account_id,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
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
            'SELECT * FROM Bookings WHERE ' + LEDGER_ROWS_WHERE.format(a='') + ' ORDER BY DateBooking ASC',
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
    def mark_datev_export(self, booking_ids: list, date_tax: str):
        """Die Sätze eines DATEV-Stapels als exportiert vermerken – in EINER Transaktion (``BEGIN IMMEDIATE``).

        Setzt wie ``update_bookings_date_tax_batch`` das Steuerdatum (``date_tax``, ``'YYYY-MM-DD'``) und legt für jeden Satz, der noch keine Zeile hat, eine in ``BookingExports`` an
        (``ExportedAt`` = jetzt, UTC): Nur der erste Export zählt, ein zweiter ändert den Zeitpunkt nie. Eine ID ohne Buchung wird übergangen, wie beim UPDATE. Die IDs gehen in
        Blöcken zu 500 an die Datenbank; scheitert ein Schritt, ist nichts geändert. Das Replay-Log folgt nach dem Commit.

        Args:
            booking_ids: Liste von Booking-IDs (nur die Sätze, die im Stapel standen)
            date_tax:    'YYYY-MM-DD'
        """
        ids = list(booking_ids)
        if not ids:
            return
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            for start in range(0, len(ids), _ID_BLOCK):
                block = ids[start:start + _ID_BLOCK]
                marks = ','.join('?' * len(block))
                for sql, params, description in (
                        (f'UPDATE Bookings SET DateTax=? WHERE ID IN ({marks})', (date_tax, *block), 'DATEV export: set tax date'),
                        ('INSERT OR IGNORE INTO BookingExports (Booking_ID, ExportedAt) SELECT ID, CURRENT_TIMESTAMP FROM Bookings '
                         f'WHERE ID IN ({marks})', tuple(block), 'DATEV export: mark as exported')):
                    tx.conn.execute(sql, params)
                    tx.log.append((sql, params, description))
    @staticmethod
    def exported_booking_ids(conn, ids):
        """Welche der Buchungen ``ids`` in einem DATEV-Stapel standen (eine Zeile in ``BookingExports`` haben), als Menge. Rein lesend, in der Transaktion von ``conn``."""
        ids = list(ids)
        found = set()
        for start in range(0, len(ids), _ID_BLOCK):
            block = ids[start:start + _ID_BLOCK]
            found.update(row[0] for row in conn.execute(
                f"SELECT Booking_ID FROM BookingExports WHERE Booking_ID IN ({','.join('?' * len(block))})", block))
        return found
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
