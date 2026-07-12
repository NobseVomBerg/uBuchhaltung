# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: reporting."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class ReportingMixin:
    def get_dashboard_monthly(self, date_from: str, date_to: str,
                              account_ids: list | None = None):
        """Monthly 3-way split of bookings for the dashboard.

        Includes both bank-type bookings (for bank accounts) and standalone
        entry-type bookings (for cash accounts like Kasse).

        Categories:
          - Einnahmen: bookings with Amount > 0, excluding private
          - Privatentnahmen: all bookings (positive + negative) associated
            with COA AccountNumber 2100-2199 (netted: Einlagen vs Entnahmen)
          - Betriebsausgaben: negative bookings not associated with private COA

        Args:
            date_from:   Start date  (YYYY-MM-DD)
            date_to:     End date    (YYYY-MM-DD)
            account_ids: Optional list of Account IDs to include.
                         None = all accounts.

        Returns:
            list  ordered chronologically, one entry per calendar month in the
            range.  Each entry: {'year_month': 'YYYY-MM', 'label': str,
            'income': float, 'private': float, 'expense': float}.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # ── Determine bank account IDs and cash COA IDs ───────────────
        if account_ids:
            ph = ','.join('?' * len(account_ids))
            cursor.execute(
                f"SELECT ID, SKRAccount, IsCash FROM Accounts WHERE ID IN ({ph})",
                account_ids)
        else:
            cursor.execute("SELECT ID, SKRAccount, IsCash FROM Accounts")
        accounts = cursor.fetchall()

        bank_acct_ids = [a[0] for a in accounts if not a[2]]
        cash_skr = [a[1] for a in accounts if a[2]]
        cash_coa_ids = []
        if cash_skr:
            cursor.execute(
                f"SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN "
                f"({','.join('?' * len(cash_skr))})", cash_skr)
            cash_coa_ids = [r[0] for r in cursor.fetchall()]

        # ── Build booking-type filter ─────────────────────────────────
        type_parts = []
        type_params: list = []
        if bank_acct_ids:
            ph = ','.join('?' * len(bank_acct_ids))
            type_parts.append(
                f"(b.BookingType='bank' AND b.Account_ID IN ({ph}))")
            type_params += bank_acct_ids
        if cash_coa_ids:
            ph = ','.join('?' * len(cash_coa_ids))
            type_parts.append(
                f"(b.BookingType='entry' AND b.ParentBooking_ID IS NULL "
                f"AND (b.COA_ID IN ({ph}) OR b.CounterCOA_ID IN ({ph})))")
            type_params += cash_coa_ids * 2
        # Kassen-Buchungen direkt via Account_ID (kein COA-Spiegel, z.B.
        # manuelle Einzel-Buchungen oder Kasse ohne SKRAccount)
        cash_acct_ids = [a[0] for a in accounts if a[2]]
        if cash_acct_ids:
            ph = ','.join('?' * len(cash_acct_ids))
            if cash_coa_ids:
                excl_ph = ','.join('?' * len(cash_coa_ids))
                not_s2 = (
                    f" AND NOT (COALESCE(b.COA_ID,-1) IN ({excl_ph})"
                    f" OR COALESCE(b.CounterCOA_ID,-1) IN ({excl_ph}))")
            else:
                not_s2 = ""
            type_parts.append(
                f"(b.BookingType='entry' AND b.ParentBooking_ID IS NULL "
                f"AND b.Account_ID IN ({ph}){not_s2})")
            # Reihenfolge der Params muss der SQL-Reihenfolge entsprechen:
            # erst IN ({ph}) = cash_acct_ids, dann NOT IN ({excl_ph}) = cash_coa_ids*2
            type_params += cash_acct_ids
            if cash_coa_ids:
                type_params += cash_coa_ids * 2

        if not type_parts:
            conn.close()
            return []

        acct_filter = ' AND (' + ' OR '.join(type_parts) + ')'

        # ── Privatentnahmen condition ─────────────────────────────────
        # Bank bookings: child entry has COA or CounterCOA 2100-2199
        # Entry bookings: own COA or CounterCOA is 2100-2199
        private_cond = '''
            AND (
                (b.BookingType='bank' AND EXISTS (
                    SELECT 1 FROM Bookings e
                    JOIN ChartOfAccounts c
                      ON (c.ID = e.COA_ID OR c.ID = e.CounterCOA_ID)
                    WHERE e.ParentBooking_ID = b.ID
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
                OR
                (b.BookingType='entry' AND EXISTS (
                    SELECT 1 FROM ChartOfAccounts c
                    WHERE (c.ID = b.COA_ID OR c.ID = b.CounterCOA_ID)
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
            )'''
        not_private_cond = '''
            AND NOT (
                (b.BookingType='bank' AND EXISTS (
                    SELECT 1 FROM Bookings e
                    JOIN ChartOfAccounts c
                      ON (c.ID = e.COA_ID OR c.ID = e.CounterCOA_ID)
                    WHERE e.ParentBooking_ID = b.ID
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
                OR
                (b.BookingType='entry' AND EXISTS (
                    SELECT 1 FROM ChartOfAccounts c
                    WHERE (c.ID = b.COA_ID OR c.ID = b.CounterCOA_ID)
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
            )'''

        p_base = [date_from, date_to] + type_params

        income_rows = cursor.execute(f'''
            SELECT strftime('%Y-%m', b.DateBooking),
                   COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              AND b.Amount > 0
              {acct_filter}
              {not_private_cond}
            GROUP BY strftime('%Y-%m', b.DateBooking)
        ''', p_base).fetchall()

        private_rows = cursor.execute(f'''
            SELECT strftime('%Y-%m', b.DateBooking),
                   COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              {acct_filter}
              {private_cond}
            GROUP BY strftime('%Y-%m', b.DateBooking)
        ''', p_base).fetchall()

        expense_rows = cursor.execute(f'''
            SELECT strftime('%Y-%m', b.DateBooking),
                   COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              AND b.Amount < 0
              {acct_filter}
              {not_private_cond}
            GROUP BY strftime('%Y-%m', b.DateBooking)
        ''', p_base).fetchall()

        income_map  = {row[0]: row[1] for row in income_rows}
        private_map = {row[0]: row[1] for row in private_rows}
        expense_map = {row[0]: row[1] for row in expense_rows}

        # Build ordered month list for the full date range
        _MONTH_ABBR = ['Jan', 'Feb', 'M\u00e4r', 'Apr', 'Mai', 'Jun',
                       'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']
        cross_year = date_from[:4] != date_to[:4]
        all_months: list[str] = []
        cy, cm = int(date_from[:4]), int(date_from[5:7])
        end_y, end_m = int(date_to[:4]), int(date_to[5:7])
        while (cy, cm) <= (end_y, end_m):
            all_months.append(f'{cy:04d}-{cm:02d}')
            if cm == 12:
                cy, cm = cy + 1, 1
            else:
                cm += 1

        result = []
        for ym in all_months:
            month_num = int(ym[5:7])
            label = _MONTH_ABBR[month_num - 1]
            if cross_year:
                label += f" '{int(ym[:4]) % 100:02d}"
            result.append({
                'year_month': ym,
                'label': label,
                'income':  float(from_minor(income_map.get(ym, 0))),
                'private': float(from_minor(private_map.get(ym, 0))),
                'expense': float(from_minor(expense_map.get(ym, 0))),
            })

        conn.close()
        return result
    def get_dashboard_totals(self, date_from: str, date_to: str,
                             account_ids: list | None = None):
        """Aggregate totals for the dashboard metric cards.

        Includes both bank-type bookings (for bank accounts) and standalone
        entry-type bookings (for cash accounts like Kasse).

        Returns dict with:
          income, private, expense, balance,
          bank_count, unlinked_count
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # ── Determine bank account IDs and cash COA IDs ───────────────
        if account_ids:
            ph = ','.join('?' * len(account_ids))
            cursor.execute(
                f"SELECT ID, SKRAccount, IsCash FROM Accounts WHERE ID IN ({ph})",
                account_ids)
        else:
            cursor.execute("SELECT ID, SKRAccount, IsCash FROM Accounts")
        accounts = cursor.fetchall()

        bank_acct_ids = [a[0] for a in accounts if not a[2]]
        cash_skr = [a[1] for a in accounts if a[2]]
        cash_coa_ids = []
        if cash_skr:
            cursor.execute(
                f"SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN "
                f"({','.join('?' * len(cash_skr))})", cash_skr)
            cash_coa_ids = [r[0] for r in cursor.fetchall()]

        # ── Build booking-type filter ─────────────────────────────────
        type_parts = []
        type_params: list = []
        if bank_acct_ids:
            ph = ','.join('?' * len(bank_acct_ids))
            type_parts.append(
                f"(b.BookingType='bank' AND b.Account_ID IN ({ph}))")
            type_params += bank_acct_ids
        if cash_coa_ids:
            ph = ','.join('?' * len(cash_coa_ids))
            type_parts.append(
                f"(b.BookingType='entry' AND b.ParentBooking_ID IS NULL "
                f"AND (b.COA_ID IN ({ph}) OR b.CounterCOA_ID IN ({ph})))")
            type_params += cash_coa_ids * 2
        # Kassen-Buchungen direkt via Account_ID (kein COA-Spiegel, z.B.
        # manuelle Einzel-Buchungen oder Kasse ohne SKRAccount)
        cash_acct_ids = [a[0] for a in accounts if a[2]]
        if cash_acct_ids:
            ph = ','.join('?' * len(cash_acct_ids))
            if cash_coa_ids:
                excl_ph = ','.join('?' * len(cash_coa_ids))
                not_s2 = (
                    f" AND NOT (COALESCE(b.COA_ID,-1) IN ({excl_ph})"
                    f" OR COALESCE(b.CounterCOA_ID,-1) IN ({excl_ph}))")
            else:
                not_s2 = ""
            type_parts.append(
                f"(b.BookingType='entry' AND b.ParentBooking_ID IS NULL "
                f"AND b.Account_ID IN ({ph}){not_s2})")
            # Reihenfolge der Params muss der SQL-Reihenfolge entsprechen:
            # erst IN ({ph}) = cash_acct_ids, dann NOT IN ({excl_ph}) = cash_coa_ids*2
            type_params += cash_acct_ids
            if cash_coa_ids:
                type_params += cash_coa_ids * 2

        if not type_parts:
            conn.close()
            return {'income': 0, 'private': 0, 'expense': 0,
                    'balance': 0, 'bank_count': 0, 'unlinked_count': 0}

        acct_filter = ' AND (' + ' OR '.join(type_parts) + ')'
        params = [date_from, date_to] + type_params

        # ── Privatentnahmen condition ─────────────────────────────────
        private_cond = '''
            AND (
                (b.BookingType='bank' AND EXISTS (
                    SELECT 1 FROM Bookings e
                    JOIN ChartOfAccounts c
                      ON (c.ID = e.COA_ID OR c.ID = e.CounterCOA_ID)
                    WHERE e.ParentBooking_ID = b.ID
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
                OR
                (b.BookingType='entry' AND EXISTS (
                    SELECT 1 FROM ChartOfAccounts c
                    WHERE (c.ID = b.COA_ID OR c.ID = b.CounterCOA_ID)
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
            )'''
        not_private_cond = '''
            AND NOT (
                (b.BookingType='bank' AND EXISTS (
                    SELECT 1 FROM Bookings e
                    JOIN ChartOfAccounts c
                      ON (c.ID = e.COA_ID OR c.ID = e.CounterCOA_ID)
                    WHERE e.ParentBooking_ID = b.ID
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
                OR
                (b.BookingType='entry' AND EXISTS (
                    SELECT 1 FROM ChartOfAccounts c
                    WHERE (c.ID = b.COA_ID OR c.ID = b.CounterCOA_ID)
                      AND c.AccountNumber >= 2100
                      AND c.AccountNumber < 2200))
            )'''

        income = cursor.execute(f'''
            SELECT COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              AND b.Amount > 0 {acct_filter}
              {not_private_cond}
        ''', params).fetchone()[0]

        private = cursor.execute(f'''
            SELECT COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              {acct_filter}
              {private_cond}
        ''', params).fetchone()[0]

        expense = cursor.execute(f'''
            SELECT COALESCE(SUM(b.Amount), 0)
            FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ?
              AND b.Amount < 0 {acct_filter}
              {not_private_cond}
        ''', params).fetchone()[0]

        bank_count = cursor.execute(f'''
            SELECT COUNT(*) FROM Bookings b
            WHERE b.DateBooking BETWEEN ? AND ? {acct_filter}
        ''', params).fetchone()[0]

        # Unlinked = bank bookings without child entries
        unlinked_params = [date_from, date_to]
        unlinked_filter = ''
        if bank_acct_ids:
            ph = ','.join('?' * len(bank_acct_ids))
            unlinked_filter = f' AND b.Account_ID IN ({ph})'
            unlinked_params += bank_acct_ids
        unlinked = cursor.execute(f'''
            SELECT COUNT(*) FROM Bookings b
            WHERE b.BookingType = 'bank'
              AND b.DateBooking BETWEEN ? AND ? {unlinked_filter}
              AND NOT EXISTS (
                  SELECT 1 FROM Bookings e
                  WHERE e.ParentBooking_ID = b.ID)
        ''', unlinked_params).fetchone()[0]

        conn.close()
        return {
            'income':         float(from_minor(income)),
            'private':        float(from_minor(private)),
            'expense':        float(from_minor(expense)),
            'balance':        float(from_minor(income + private + expense)),
            'bank_count':     bank_count,
            'unlinked_count': unlinked,
        }
    def get_euer_data(self, date_from: str, date_to: str,
                      account_ids: list | None = None) -> list:
        """EÜR-Auswertung: Saldo pro SKR-Konto im Zeitraum.

        Für jede Entry-Buchung wird das „Zweck-Konto" ermittelt:
        - Wenn COA_ID ein liquides Konto (Bank/Kasse) ist → CounterCOA_ID
        - Sonst → COA_ID

        Rein liquide Spiegelbuchungen (Doppik) werden ignoriert.

        Returns:
            list of (AccountNumber, Name, total_amount)
            sortiert nach AccountNumber, nur Konten mit Saldo ≠ 0.
        """
        from collections import defaultdict

        conn = self._get_connection()
        cursor = conn.cursor()

        # Liquide Konten: Bankkonten (aus Accounts-Tabelle) + Kassenkonten
        bank_coa_ids = self._get_bank_coa_ids(cursor)

        if account_ids:
            ph = ','.join('?' * len(account_ids))
            cursor.execute(
                f"SELECT ID, SKRAccount, IsCash FROM Accounts "
                f"WHERE ID IN ({ph})", account_ids)
        else:
            cursor.execute("SELECT ID, SKRAccount, IsCash FROM Accounts")
        accts = cursor.fetchall()

        bank_acct_ids = [a[0] for a in accts if not a[2]]
        cash_skr = [a[1] for a in accts if a[2]]
        cash_coa_ids: set[int] = set()
        if cash_skr:
            ph = ','.join('?' * len(cash_skr))
            cursor.execute(
                f"SELECT ID FROM ChartOfAccounts "
                f"WHERE AccountNumber IN ({ph})", cash_skr)
            cash_coa_ids = {r[0] for r in cursor.fetchall()}

        liquid_coa_ids = bank_coa_ids | cash_coa_ids

        # ── 1. Entry-Kinder von Bank-Buchungen ───────────────────────
        entries: list[tuple] = []
        if bank_acct_ids:
            ph = ','.join('?' * len(bank_acct_ids))
            cursor.execute(f"""
              SELECT e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0)
                FROM Bookings e
                JOIN Bookings p ON p.ID = e.ParentBooking_ID
                WHERE e.BookingType = 'entry'
                  AND p.Account_ID IN ({ph})
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
            """, bank_acct_ids + [date_from, date_to])
            entries.extend(cursor.fetchall())

        # ── 2. Standalone Kassen-Buchungen (COA-Spiegel) ─────────────
        if cash_coa_ids:
            coa_list = list(cash_coa_ids)
            ph = ','.join('?' * len(coa_list))
            cursor.execute(f"""
              SELECT e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0)
                FROM Bookings e
                WHERE e.BookingType = 'entry'
                  AND e.ParentBooking_ID IS NULL
                  AND (e.COA_ID IN ({ph}) OR e.CounterCOA_ID IN ({ph}))
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
            """, coa_list + coa_list + [date_from, date_to])
            entries.extend(cursor.fetchall())

        # ── 2b. Kassen-Buchungen direkt via Account_ID ────────────────
        # Erfasst manuelle Einzel-Buchungen, bei denen Account_ID auf ein
        # Kassenkonto zeigt, aber kein COA-Spiegel gesetzt ist (z.B. ältere
        # Einträge ohne CounterCOA_ID oder Kasse ohne SKRAccount).
        cash_acct_ids = [a[0] for a in accts if a[2]]  # IsCash=1
        direct_entries: list[tuple] = []
        if cash_acct_ids:
            ph2 = ','.join('?' * len(cash_acct_ids))
            if cash_coa_ids:
                # Ausschließen was schon in Sektion 2 erfasst wird
                coa_list = list(cash_coa_ids)
                excl_ph = ','.join('?' * len(coa_list))
                not_in_s2 = (
                    f" AND NOT (COALESCE(e.COA_ID,-1) IN ({excl_ph})"
                    f" OR COALESCE(e.CounterCOA_ID,-1) IN ({excl_ph}))")
                excl_params: list = coa_list + coa_list
            else:
                not_in_s2 = ""
                excl_params = []
            cursor.execute(f"""
              SELECT e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0)
                FROM Bookings e
                WHERE e.BookingType = 'entry'
                  AND e.ParentBooking_ID IS NULL
                  AND e.Account_ID IN ({ph2})
                  {not_in_s2}
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
            """, cash_acct_ids + excl_params + [date_from, date_to])
            direct_entries = cursor.fetchall()

        # ── Einnahmen-COA-IDs ermitteln (für USt-Zuordnung auf 3806) ──
        cursor.execute("""
            SELECT ID FROM ChartOfAccounts
            WHERE AccountNumber IN (4400, 4640, 4845)
        """)
        income_coa_ids = {r[0] for r in cursor.fetchall()}

        cursor.execute("""
            SELECT ID, AccountNumber FROM ChartOfAccounts
            WHERE AccountNumber IN (1401, 1406)
        """)
        input_tax_coa_ids = {acct_nr: coa_id for coa_id, acct_nr in cursor.fetchall()}

        # ── Zweck-Konto bestimmen und aggregieren ─────────────────────
        # Netto-Beträge pro Konto + virtuelle USt-Zeile (3806)
        totals: dict[int, float] = defaultdict(float)
        ust_total: float = 0.0   # nur USt aus Einnahmen → 3806
        for amount, coa_id, counter_coa_id, tax_amount, tax_rate in entries:
            netto = amount - tax_amount  # Brutto → Netto

            purpose_coa_id = None

            # Doppik-Spiegel überspringen (beide Seiten liquid)
            if coa_id in liquid_coa_ids and counter_coa_id in liquid_coa_ids:
                continue

            if coa_id in liquid_coa_ids:
                # Cash-flow: liquides Konto → Zweck = CounterCOA
                if counter_coa_id:
                    purpose_coa_id = counter_coa_id
                    totals[counter_coa_id] += netto
                    if counter_coa_id in income_coa_ids:
                        ust_total += tax_amount
            elif counter_coa_id and counter_coa_id in liquid_coa_ids:
                # Cash-flow: Zweck = COA, Gegenstück ist liquid
                purpose_coa_id = coa_id
                totals[coa_id] += netto
                if coa_id in income_coa_ids:
                    ust_total += tax_amount
            else:
                # Umbuchung (keine Seite liquid, z.B. 4405→4400):
                # Betrag auf beide Konten verteilen, damit z.B.
                # Erlöse unter 4400 erscheinen und 4405 reduziert wird.
                if counter_coa_id:
                    purpose_coa_id = counter_coa_id
                    totals[counter_coa_id] += netto
                    if counter_coa_id in income_coa_ids:
                        ust_total += tax_amount
                if coa_id:
                    totals[coa_id] -= netto

            # Vorsteuer aus Ausgaben separat auf 1401/1406 ausweisen.
            if (purpose_coa_id is not None
                    and purpose_coa_id not in income_coa_ids
                    and tax_amount != 0):
                input_tax_account = None
                if abs(tax_rate - 0.05) < 0.0001 or abs(tax_rate - 0.07) < 0.0001:
                    input_tax_account = 1401
                elif abs(tax_rate - 0.16) < 0.0001 or abs(tax_rate - 0.19) < 0.0001:
                    input_tax_account = 1406
                if input_tax_account in input_tax_coa_ids:
                    totals[input_tax_coa_ids[input_tax_account]] += tax_amount

        # ── 2b. Direkte Kassen-Buchungen (Account_ID, kein COA-Spiegel) ──
        # Account_ID zeigt auf die liquide Kassenseite; COA_ID ist das
        # Zweckkonto (Aufwand/Ertrag). Kein liquid-Routing nötig.
        for amount, coa_id, counter_coa_id, tax_amount, tax_rate in direct_entries:
            netto = amount - tax_amount
            # Doppik-Spiegel überspringen
            if coa_id in liquid_coa_ids and counter_coa_id in liquid_coa_ids:
                continue
            # Zweckkonto bestimmen: nicht-liquide Seite bevorzugen
            purpose_coa_id = None
            if coa_id and coa_id not in liquid_coa_ids:
                purpose_coa_id = coa_id
                totals[coa_id] += netto
                if coa_id in income_coa_ids:
                    ust_total += tax_amount
            elif counter_coa_id and counter_coa_id not in liquid_coa_ids:
                purpose_coa_id = counter_coa_id
                totals[counter_coa_id] += netto
                if counter_coa_id in income_coa_ids:
                    ust_total += tax_amount
            # Vorsteuer
            if (purpose_coa_id is not None
                    and purpose_coa_id not in income_coa_ids
                    and tax_amount != 0):
                input_tax_account = None
                if abs(tax_rate - 0.05) < 0.0001 or abs(tax_rate - 0.07) < 0.0001:
                    input_tax_account = 1401
                elif abs(tax_rate - 0.16) < 0.0001 or abs(tax_rate - 0.19) < 0.0001:
                    input_tax_account = 1406
                if input_tax_account in input_tax_coa_ids:
                    totals[input_tax_coa_ids[input_tax_account]] += tax_amount

        # ── COA-Details laden ─────────────────────────────────────────
        result: list[tuple] = []
        if totals:
            ph = ','.join('?' * len(totals))
            cursor.execute(f"""
                SELECT ID, AccountNumber, Name FROM ChartOfAccounts
                WHERE ID IN ({ph})
            """, list(totals.keys()))
            for coa_id, acct_nr, name in cursor.fetchall():
                total = from_minor(totals[coa_id])  # Minor Units -> Euro-Decimal
                if abs(total) >= Decimal('0.01'):
                    result.append((acct_nr, name, float(total)))

        # ── Virtuelles Konto 3806 (Umsatzsteuer) ─────────────────────
        ust_total = from_minor(ust_total)  # Minor Units -> Euro-Decimal
        if abs(ust_total) >= Decimal('0.01'):
            # Name aus DB holen, falls vorhanden
            cursor.execute(
                "SELECT Name FROM ChartOfAccounts WHERE AccountNumber = 3806"
            )
            row_3806 = cursor.fetchone()
            name_3806 = row_3806[0] if row_3806 else 'Umsatzsteuer 19%'
            result.append((3806, name_3806, float(ust_total)))

        conn.close()
        result.sort(key=lambda x: x[0])
        return result