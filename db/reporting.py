# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: reporting."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from .matching import (
    classify_entry, is_asset_transfer, ASSET_ACCOUNT_RANGE, PL_ACCOUNT_RANGE,
    ENTRY_MIRROR, ENTRY_EMPTY, ENTRY_PURPOSE_FIRST, ENTRY_TRANSFER,
)

# SKR04-Kontenklasse 4 = Betriebliche Erträge (Erlöse). Einzige Quelle für die
# Einnahmen-Klassifizierung in EÜR/Dashboard – die frühere Aufzählung
# (4400/4640/4845) übersah z. B. 4185 (§19), 4300 (7%) und 4736 (Skonti).
INCOME_ACCOUNT_RANGE = (4000, 4999)

# Bestandskonten des Offene-Posten-Verfahrens: die Forderung aus einer
# versendeten Rechnung (10000) und die Wartekonten, auf denen der Erlös bis zur
# Zahlung parkt. Die EÜR ist eine Ist-Rechnung – erst die Umbuchung vom
# Wartekonto auf das Erlöskonto (bei Zahlung) ist der Einnahmezeitpunkt.
# Stünden diese Konten mit in der Auswertung, höbe 4405 -netto den Erlös auf
# 4400 +netto wieder auf (beide liegen in der Erlösklasse 4000-4999) und die
# Forderung erschiene als Ausgabe.
NEUTRAL_ACCOUNT_NUMBERS = (10000, 4340, 4345, 4405)

# Verrechnungskonto (Geldtransit): WISO bucht darüber aufgeteilte Zahlungen und
# Anlagen-Umbuchungen. Ein eigenständiger Satz „Aufwand an Verrechnung“ gilt in der EÜR
# mit seinem Datum als abgeflossen; die spätere Übertragung „Verrechnung an Bank“ ist
# ein reiner Geldkonten-Spiegel und zählt nicht noch einmal.
CLEARING_ACCOUNT_NUMBER = 1460


def is_income_account(account_number) -> bool:
    """True für Erlös-/Ertragskonten (SKR04-Klasse 4)."""
    try:
        nr = int(account_number)
    except (TypeError, ValueError):
        return False
    if nr in NEUTRAL_ACCOUNT_NUMBERS:
        return False
    return INCOME_ACCOUNT_RANGE[0] <= nr <= INCOME_ACCOUNT_RANGE[1]


class ReportingMixin:
    def _get_neutral_coa_ids(self, cursor):
        """COA-IDs der auswertungsneutralen Bestandskonten.

        Forderungen und Wartekonten des Offene-Posten-Verfahrens – siehe
        NEUTRAL_ACCOUNT_NUMBERS.
        """
        ph = ','.join('?' * len(NEUTRAL_ACCOUNT_NUMBERS))
        cursor.execute(
            f'SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN ({ph})',
            NEUTRAL_ACCOUNT_NUMBERS)
        return {r[0] for r in cursor.fetchall()}

    def _get_asset_coa_ids(self, cursor):
        """COA-IDs der Anlagekonten (SKR04-Klasse 0, siehe ASSET_ACCOUNT_RANGE)."""
        cursor.execute(
            'SELECT ID FROM ChartOfAccounts WHERE AccountNumber BETWEEN ? AND ?',
            ASSET_ACCOUNT_RANGE)
        return {r[0] for r in cursor.fetchall()}

    def _get_pl_coa_ids(self, cursor):
        """COA-IDs der Erfolgskonten (Erträge und Aufwendungen, siehe PL_ACCOUNT_RANGE)."""
        cursor.execute(
            'SELECT ID FROM ChartOfAccounts WHERE AccountNumber BETWEEN ? AND ?',
            PL_ACCOUNT_RANGE)
        return {r[0] for r in cursor.fetchall()}

    def get_dashboard_monthly(self, date_from: str, date_to: str,
                              account_ids: list | None = None):
        """Monthly 3-way split of bookings for the dashboard.

        Enthält die Bankbewegungen (``bank``) aller gewählten Konten – auch der
        Kasse, wenn sie über die Buchungsmaske gebucht wird – und eigenständige
        Buchungssätze auf Kassenkonten. Das Dashboard folgt den Kontobewegungen:
        Das Verrechnungskonto 1460 und die AfA (kein Geldfluss) zählen hier nicht.

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

        # Bankbewegungen aller gewählten Konten – auch die der Kasse (Buchungsmaske, Konto = Kasse)
        acct_ids = [a[0] for a in accounts]
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
        if acct_ids:
            ph = ','.join('?' * len(acct_ids))
            type_parts.append(
                f"(b.BookingType='bank' AND b.Account_ID IN ({ph}))")
            type_params += acct_ids
        if cash_coa_ids:
            ph = ','.join('?' * len(cash_coa_ids))
            type_parts.append(
                f"(b.BookingType='entry' AND b.ParentBooking_ID IS NULL "
                f"AND (b.COA_ID IN ({ph}) OR b.CounterCOA_ID IN ({ph})))")
            type_params += cash_coa_ids * 2
        # Kassen-Buchungen direkt via Account_ID (kein COA-Spiegel, z.B.
        # manuelle Einzel-Buchungen oder Kasse ohne SKRAccount). Eine AfA-Buchung
        # alter Form (mit Account_ID) ist kein Geldfluss und zählt hier nicht.
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
                f"AND b.Account_ID IN ({ph}){not_s2} "
                f"AND NOT EXISTS (SELECT 1 FROM AssetDepreciations d "
                f"WHERE d.Booking_ID = b.ID))")
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

        Enthält die Bankbewegungen (``bank``) aller gewählten Konten – auch der
        Kasse, wenn sie über die Buchungsmaske gebucht wird – und eigenständige
        Buchungssätze auf Kassenkonten. Das Dashboard folgt den Kontobewegungen:
        Das Verrechnungskonto 1460 und die AfA (kein Geldfluss) zählen hier nicht.

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

        # Bankbewegungen aller gewählten Konten – auch die der Kasse (Buchungsmaske, Konto = Kasse)
        acct_ids = [a[0] for a in accounts]
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
        if acct_ids:
            ph = ','.join('?' * len(acct_ids))
            type_parts.append(
                f"(b.BookingType='bank' AND b.Account_ID IN ({ph}))")
            type_params += acct_ids
        if cash_coa_ids:
            ph = ','.join('?' * len(cash_coa_ids))
            type_parts.append(
                f"(b.BookingType='entry' AND b.ParentBooking_ID IS NULL "
                f"AND (b.COA_ID IN ({ph}) OR b.CounterCOA_ID IN ({ph})))")
            type_params += cash_coa_ids * 2
        # Kassen-Buchungen direkt via Account_ID (kein COA-Spiegel, z.B.
        # manuelle Einzel-Buchungen oder Kasse ohne SKRAccount). Eine AfA-Buchung
        # alter Form (mit Account_ID) ist kein Geldfluss und zählt hier nicht.
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
                f"AND b.Account_ID IN ({ph}){not_s2} "
                f"AND NOT EXISTS (SELECT 1 FROM AssetDepreciations d "
                f"WHERE d.Booking_ID = b.ID))")
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
        if acct_ids:
            ph = ','.join('?' * len(acct_ids))
            unlinked_filter = f' AND b.Account_ID IN ({ph})'
            unlinked_params += acct_ids
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
        - Wenn COA_ID ein liquides Konto (Zahlungskonto) ist → CounterCOA_ID
        - Sonst → COA_ID

        Liquide sind alle Zahlungskonten (``get_payment_coa_ids``: eigene Bank- und
        Kassenkonten, Kassenbereich 1600–1699, Verrechnungskonto 1460) – dieselbe
        Menge wie im DATEV-Export. Rein liquide Spiegelbuchungen (Doppik) werden
        ignoriert. Fehlt die liquide Seite (Bankkonto ohne SKR-Nummer), bleibt der Satz
        eine Zahlung mit Vorzeichen und Steuer – als Umbuchung zählt er nur, wenn beide
        Seiten kontiert sind und keine liquide ist (``db.matching.classify_entry``).

        Ob ein Satz in die EÜR kommt, entscheidet die Sektion, nicht die Kontenmenge:
        1. Buchungssätze unter einer Bankbewegung der gewählten Konten – auch der Kasse
           (Buchungsmaske mit Konto = Kasse);
        2. eigenständige Sätze (ohne Bankbewegung), die ein Kassenkonto berühren, und –
           ohne Kontofilter – das Verrechnungskonto 1460 (WISO: „Aufwand an Verrechnung“
           gilt mit seinem Datum als abgeflossen);
        2b. eigenständige Sätze mit Account_ID auf einer Kasse, die in 2 nicht vorkommen
           (ältere Einträge ohne Kassen-SKR); AfA-Buchungen alter Form gehören nicht dazu;
        3. ohne Kontofilter: Umbuchungen zwischen Anlage- und Erfolgskonto (AfA,
           Restbuchwert beim Abgang). Sie bewegen kein Geld und hängen an keinem Konto.
        Sektion 1 verlangt eine Bankbewegung als Elternzeile, 2, 2b und 3 verlangen keine –
        ein Satz zählt deshalb nur einmal, auch wenn der Abgleich ihn später verknüpft.

        Returns:
            list of (AccountNumber, Name, total_amount)
            sortiert nach AccountNumber, nur Konten mit Saldo ≠ 0.
        """
        from collections import defaultdict

        conn = self._get_connection()
        cursor = conn.cursor()

        # Liquide Konten: alle Zahlungskonten, dieselbe Menge wie im DATEV-Export
        liquid_coa_ids = self._get_payment_coa_ids(cursor)

        cursor.execute("SELECT ID FROM Accounts")
        all_acct_ids = {r[0] for r in cursor.fetchall()}
        if account_ids:
            ph = ','.join('?' * len(account_ids))
            cursor.execute(
                f"SELECT ID, SKRAccount, IsCash FROM Accounts "
                f"WHERE ID IN ({ph})", account_ids)
        else:
            cursor.execute("SELECT ID, SKRAccount, IsCash FROM Accounts")
        accts = cursor.fetchall()

        # Sätze, die an keinem Konto hängen (Sektion 3, Verrechnungskonto), zählen nur,
        # wenn alle Konten gewählt sind
        unfiltered = not account_ids or all_acct_ids <= set(account_ids)

        acct_ids = [a[0] for a in accts]               # auch die Kasse (Buchungsmaske)
        cash_skr = [a[1] for a in accts if a[2]]
        cash_coa_ids: set[int] = set()
        if cash_skr:
            ph = ','.join('?' * len(cash_skr))
            cursor.execute(
                f"SELECT ID FROM ChartOfAccounts "
                f"WHERE AccountNumber IN ({ph})", cash_skr)
            cash_coa_ids = {r[0] for r in cursor.fetchall()}

        # ── 1. Entry-Kinder von Bank-Buchungen ───────────────────────
        entries: list[tuple] = []
        if acct_ids:
            ph = ','.join('?' * len(acct_ids))
            cursor.execute(f"""
              SELECT e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0)
                FROM Bookings e
                JOIN Bookings p ON p.ID = e.ParentBooking_ID
                WHERE e.BookingType = 'entry'
                  AND p.Account_ID IN ({ph})
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
            """, acct_ids + [date_from, date_to])
            entries.extend(cursor.fetchall())

        # ── 2. Standalone-Sätze auf Kassenkonten (und dem Verrechnungskonto) ──
        # Das Verrechnungskonto hängt an keinem Konto: Es zählt nur ohne Kontofilter.
        # Eine einzige Abfrage über beide Mengen, damit ein Satz nicht doppelt kommt,
        # wenn 1460 zugleich die SKR-Nummer einer Kasse ist.
        s2_coa = set(cash_coa_ids)
        if unfiltered:
            cursor.execute("SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?",
                           (CLEARING_ACCOUNT_NUMBER,))
            s2_coa.update(r[0] for r in cursor.fetchall())
        if s2_coa:
            coa_list = list(s2_coa)
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
        # Einträge ohne CounterCOA_ID oder Kasse ohne SKRAccount). Eine AfA-Buchung
        # alter Form (mit Account_ID) ist kein Geldfluss und gehört in Sektion 3.
        cash_acct_ids = [a[0] for a in accts if a[2]]  # IsCash=1
        if cash_acct_ids:
            ph2 = ','.join('?' * len(cash_acct_ids))
            if s2_coa:
                # Ausschließen was schon in Sektion 2 erfasst wird
                coa_list = list(s2_coa)
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
                  AND NOT EXISTS (SELECT 1 FROM AssetDepreciations d
                                  WHERE d.Booking_ID = e.ID)
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
            """, cash_acct_ids + excl_params + [date_from, date_to])
            entries.extend(cursor.fetchall())

        # ── 3. Umbuchungen zwischen Anlage- und Erfolgskonto (AfA) ────
        # Nicht zahlungswirksam, an kein Konto gebunden: nur ohne Kontofilter. Sätze, die
        # eine AfA-Planzeile referenziert (auch alter Form: Aufwandskonto, kein
        # Gegenkonto), gehören immer dazu; alle übrigen nur als Anlagen-Umbuchung.
        asset_coa_ids = self._get_asset_coa_ids(cursor)
        if unfiltered:
            pl_coa_ids = self._get_pl_coa_ids(cursor)
            asset_cond, asset_params = '', []
            if asset_coa_ids:
                asset_list = list(asset_coa_ids)
                ph = ','.join('?' * len(asset_list))
                asset_cond = f" OR e.COA_ID IN ({ph}) OR e.CounterCOA_ID IN ({ph})"
                asset_params = asset_list + asset_list
            cursor.execute(f"""
              SELECT e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0),
                  EXISTS (SELECT 1 FROM AssetDepreciations d WHERE d.Booking_ID = e.ID)
                FROM Bookings e
                WHERE e.BookingType = 'entry'
                  AND e.ParentBooking_ID IS NULL
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
                  AND (EXISTS (SELECT 1 FROM AssetDepreciations d
                               WHERE d.Booking_ID = e.ID){asset_cond})
            """, [date_from, date_to] + asset_params)
            for (amount, coa_id, counter_coa_id, tax_amount, tax_rate,
                 depreciation_linked) in cursor.fetchall():
                if depreciation_linked or is_asset_transfer(
                        coa_id, counter_coa_id, liquid_coa_ids, asset_coa_ids, pl_coa_ids):
                    entries.append((amount, coa_id, counter_coa_id, tax_amount, tax_rate))

        # ── Einnahmen-COA-IDs ermitteln (für USt-Zuordnung auf 3806) ──
        # Die Wartekonten liegen zwar in der Erlösklasse, sind aber noch keine
        # Einnahme – ihre USt entsteht erst mit der Umbuchung bei Zahlung.
        _neutral_ph = ','.join('?' * len(NEUTRAL_ACCOUNT_NUMBERS))
        cursor.execute(f"""
            SELECT ID FROM ChartOfAccounts
            WHERE AccountNumber BETWEEN ? AND ?
              AND AccountNumber NOT IN ({_neutral_ph})
        """, list(INCOME_ACCOUNT_RANGE) + list(NEUTRAL_ACCOUNT_NUMBERS))
        income_coa_ids = {r[0] for r in cursor.fetchall()}

        cursor.execute("""
            SELECT ID, AccountNumber FROM ChartOfAccounts
            WHERE AccountNumber IN (1401, 1406)
        """)
        input_tax_coa_ids = {acct_nr: coa_id for coa_id, acct_nr in cursor.fetchall()}

        neutral_coa_ids = self._get_neutral_coa_ids(cursor)

        # ── Zweck-Konto bestimmen und aggregieren ─────────────────────
        # Netto-Beträge pro Konto + virtuelle USt-Zeile (3806)
        totals: dict[int, float] = defaultdict(float)
        ust_total: float = 0.0   # nur USt aus Einnahmen → 3806

        def _book(coa, value):
            """Betrag verbuchen – Bestandskonten bleiben aus der EÜR heraus."""
            if coa and coa not in neutral_coa_ids:
                totals[coa] += value

        def _purpose(coa):
            """Zweckkonto; ein Bestandskonto ist keines (kein Vorsteuerabzug)."""
            return coa if coa and coa not in neutral_coa_ids else None

        def _book_transfer(coa, value):
            """Umbuchung verbuchen – ein Anlagekonto ist kein Einnahme- oder Ausgabekonto
            (AfA „Aufwand an Anlage“ soll 0520 nicht als Einnahme zeigen)."""
            if coa not in asset_coa_ids:
                _book(coa, value)

        for amount, coa_id, counter_coa_id, tax_amount, tax_rate in entries:
            netto = amount - tax_amount  # Brutto → Netto

            purpose_coa_id = None

            kind = classify_entry(coa_id, counter_coa_id, liquid_coa_ids)
            if kind in (ENTRY_MIRROR, ENTRY_EMPTY):
                continue                      # Übertrag zwischen Geldkonten bzw. nichts kontiert

            if kind == ENTRY_TRANSFER:
                # Umbuchung (beide Seiten kontiert, keine liquide, z. B. 4405→4400):
                # Betrag auf beide Konten verteilen, damit z. B. Erlöse unter 4400
                # erscheinen und 4405 reduziert wird. Anlagekonten bleiben draußen.
                purpose_coa_id = _purpose(counter_coa_id)
                _book_transfer(counter_coa_id, netto)
                if counter_coa_id in income_coa_ids:
                    ust_total += tax_amount
                _book_transfer(coa_id, -netto)
            else:
                # Zahlung: Das Zweckkonto ist die nicht-liquide Seite. Fehlt die
                # liquide Seite (Bankkonto ohne SKR-Nummer, nachträglich verknüpfter
                # Beleg), bleibt es eine Zahlung – früher lief so ein Satz in den
                # Umbuchungs-Zweig und kam mit umgedrehtem Vorzeichen und ohne
                # Steuer in die EÜR.
                purpose = coa_id if kind == ENTRY_PURPOSE_FIRST else counter_coa_id
                if purpose:
                    purpose_coa_id = _purpose(purpose)
                    _book(purpose, netto)
                    if purpose in income_coa_ids:
                        ust_total += tax_amount

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