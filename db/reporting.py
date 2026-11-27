# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: reporting."""
import datetime
import sqlite3
import os
import json
import math
from collections import Counter
from decimal import Decimal
from typing import NamedTuple
from money import to_minor, from_minor
from . import datev_keys
from .matching import (
    FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_PURPOSE_FIRST, FORM_TRANSFER, MIRROR_FORMS,
    is_asset_transfer, read_entry_form, ASSET_ACCOUNT_RANGE, PL_ACCOUNT_RANGE,
    REVENUE_ACCOUNT_RANGE, bank_effect, is_mirror_row, _ID_CHUNK,
)

# SKR04-Kontenklasse 4 = Betriebliche Erträge (Erlöse). Einzige Quelle für die
# Einnahmen-Klassifizierung in EÜR/Dashboard – die frühere Aufzählung
# (4400/4640/4845) übersah z. B. 4185 (§19), 4300 (7%) und 4736 (Skonti).
INCOME_ACCOUNT_RANGE = REVENUE_ACCOUNT_RANGE

# Bestandskonten des Offene-Posten-Verfahrens: die Forderung aus einer
# versendeten Rechnung (10000) und die Wartekonten, auf denen der Erlös bis zur
# Zahlung parkt. Die EÜR ist eine Ist-Rechnung – erst die Umbuchung vom
# Wartekonto auf das Erlöskonto (bei Zahlung) ist der Einnahmezeitpunkt.
# Stünden diese Konten mit in der Auswertung, höbe 4405 -netto den Erlös auf
# 4400 +netto wieder auf (beide liegen in der Erlösklasse 4000-4999) und die
# Forderung erschiene als Ausgabe.
NEUTRAL_ACCOUNT_NUMBERS = (10000, 4340, 4345, 4405)

# Die virtuelle Zeile der EÜR für Vorsteuer, die auf keinem Vorsteuerkonto steht: Die Zeilen 1401 und 1406 kennen nur die Sätze 5, 7, 16 und 19 Prozent; ein anderer
# Satz (etwa 5,5 Prozent) hat sein Konto in den Stammdaten (``TaxRates.InputTaxCoaId``) oder keines, ebenso ein Steuerbetrag ohne Satz. Die Vorsteuer ist gezahlt und
# gehört in die EÜR; auf diese Zeile kommt, was sonst fehlte. Jede Zeile braucht eine ganze Zahl (``euer_section``, ``bwa.group_of`` und die Liquiditätsrechnung lesen nur
# solche): 1409 liegt in der Gruppe „Gezahlte Vorsteuer“ (1400–1439) und in keinem Standard-Kontenrahmen – keine Nummer, die die App bucht, sondern der Platz der Zeile,
# nach dem Muster der virtuellen 3806. Führt ein Bestand ein echtes Konto 1409 mit Saldo, stehen zwei Zeilen (wie bei der echten und der virtuellen 3806).
EUER_INPUT_TAX_UNASSIGNED = 1409
EUER_INPUT_TAX_UNASSIGNED_NAME = 'Vorsteuer ohne Steuerkonto'

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


# „Sonstige Ausgaben“ der EÜR-Seite: Die Konten werden angezeigt, zählen aber nicht im Ergebnis.
OTHER_EXPENSE_ACCOUNTS = frozenset({3160, 3720, 3740})


def bank_skr_numbers(accounts) -> set:
    """Die SKR-Nummern der eigenen Bank- und Kassenkonten (Zeilen von ``fetch_accounts``, Index 7). Als Zweckkonto
    zeigt die EÜR sie nie: Sie sind Geldkonten, kein Ertrag und kein Aufwand."""
    return {account[7] for account in accounts if account[7]}


def euer_section(account_number, bank_numbers) -> str | None:
    """Wo eine Zeile von ``get_euer_data`` in der EÜR steht – EINE Regel für die Seite des Dashboards und die
    betriebswirtschaftliche Auswertung (``db.bwa``):

    * ``'other'``   – „Sonstige Ausgaben“ (``OTHER_EXPENSE_ACCOUNTS``): angezeigt, aber nicht im Ergebnis
    * ``'income'``  – Betriebseinnahme: ein Erlöskonto (``is_income_account``) und die virtuelle Zeile 3806, auf der
      ``get_euer_data`` die Umsatzsteuer aus Einnahmen ausweist
    * ``'expense'`` – Betriebsausgabe: jedes andere Konto ab 1000
    * ``None``      – steht nicht in der EÜR: die Bestandskonten des Offene-Posten-Verfahrens
      (``NEUTRAL_ACCOUNT_NUMBERS``, auch als zweite Sicherung – ``get_euer_data`` liefert sie ohnehin nie, ein neues
      Wartekonto soll aber nicht durchrutschen), die Geldkonten (``bank_numbers``), Privatkonten (2100–2199) und alles
      unter 1000 (Anlagekonten)

    Die Konten von ``OTHER_EXPENSE_ACCOUNTS`` stehen ohne weitere Prüfung unter „Sonstige Ausgaben“, wie bisher auf der Seite."""
    if account_number in OTHER_EXPENSE_ACCOUNTS:
        return 'other'
    excluded = account_number in NEUTRAL_ACCOUNT_NUMBERS or account_number in bank_numbers
    if is_income_account(account_number) or account_number == 3806:
        return None if excluded else 'income'
    if excluded or 2100 <= account_number < 2200 or account_number < 1000:
        return None
    return 'expense'


def _cash_amount_sql(cash_coa_ids, alias='b'):
    """SQL-Ausdruck (und Parameter) für den Betrag einer Zeile aus Sicht der Kasse.

    Ein Spiegel, den ein Import schreibt (``mirror_coa``: positiv = Konto im Soll), trägt den
    Betrag auf der Seite „Konto“. Steht die Kasse dort im Gegenkonto, hat sie das Gegenteil
    bewegt: Abhebung „1600 an 1800“ bringt der Kasse +100, die Bareinzahlung „1800 an 1600“
    −100. Für ``mirror_counter`` (Betrag auf dem Gegenkonto, wie die Buchungsmaske ihn
    schreibt) gilt dasselbe spiegelbildlich. Jede andere Zeile – auch ``mirror_legacy`` und
    Zeilen ohne Form – zählt mit ihrem Betrag, wie bisher; ebenso ein Spiegel, in dem die Kasse
    auf keiner Seite steht."""
    if not cash_coa_ids:
        return f'{alias}.Amount', []
    marks = ','.join('?' * len(cash_coa_ids))
    sql = (f"(CASE "
           f"WHEN {alias}.BookingType = 'entry' AND {alias}.EntryForm = ? "
           f"AND COALESCE({alias}.COA_ID, -1) NOT IN ({marks}) "
           f"AND COALESCE({alias}.CounterCOA_ID, -1) IN ({marks}) THEN -{alias}.Amount "
           f"WHEN {alias}.BookingType = 'entry' AND {alias}.EntryForm = ? "
           f"AND COALESCE({alias}.CounterCOA_ID, -1) NOT IN ({marks}) "
           f"AND COALESCE({alias}.COA_ID, -1) IN ({marks}) THEN -{alias}.Amount "
           f"ELSE {alias}.Amount END)")
    cash = list(cash_coa_ids)
    return sql, [FORM_MIRROR_COA, *cash, *cash, FORM_MIRROR_COUNTER, *cash, *cash]


#: Die größte Zahl Minor Units, die die Summen- und Saldenliste als Betrag eines Satzes liest (10^15 = 100 Milliarden Euro): ein größerer
#: Wert – und jeder, der keine ganze Zahl ist – steht nur im Hinweis (``Database.get_susa``)
_SUSA_MAX_MINOR = 10 ** 15
#: Ein Datum, das die Summen- und Saldenliste liest: ein Kalendertag als Text ``JJJJ-MM-TT``. SQLite rückt ein unmögliches Datum auf einen echten
#: Tag (``date('2026-02-30')`` ist ``'2026-03-02'``), kennt ``date('Dezember')`` nicht und liest eine Zahl nicht als Text – in jedem dieser Fälle
#: ist das Ergebnis ungleich dem gespeicherten Wert. Das Muster davor hält die Form fest, gleich welche SQLite-Version rechnet (ein Jahr mit
#: Vorzeichen gibt ``date()`` je nach Version als sich selbst zurück), und das Jahr 0000 kennt der Kalender nicht (geprüft an den ersten vier Zeichen: Die Spalte hat numerische Affinität, ein Vergleich mit ``'0001'`` vergliche mit der Zahl 1). ``{a}`` ist der Tabellenalias
#: mit Punkt (``'e.'``) oder leer, wie bei ``LEDGER_ROWS_WHERE``.
_SUSA_DATE_OK = ("(typeof({a}DateBooking) = 'text' AND {a}DateBooking GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
                 "AND substr({a}DateBooking, 1, 4) <> '0000' AND {a}DateBooking IS date({a}DateBooking))")


def _sum_groups_in_sql(cursor, keys, values, tail, params):
    """``SELECT <keys>, COUNT(*), SUM(<values>) <tail> GROUP BY <keys>``: je Gruppe ``(*Schlüssel, Zahl, *Summen)``, ohne Schlüssel genau eine Zeile.
    ``keys`` und ``values`` sind SQL-Ausdrücke, ``tail`` das ``FROM … WHERE …``; jeder Wert steht in ``params`` (Reihenfolge der ``?``: erst die in
    ``keys`` und ``values``, dann die in ``tail``)."""
    select = ''.join(f'{key}, ' for key in keys)
    sums = ', '.join(f'COALESCE(SUM({value}), 0)' for value in values)
    group = f" GROUP BY {', '.join(str(index) for index in range(1, len(keys) + 1))}" if keys else ''
    return cursor.execute(f'SELECT {select}COUNT(*), {sums} {tail}{group}', params).fetchall()


def _sum_groups_in_python(cursor, keys, values, tail, params):
    """Dieselben Gruppen und Summen wie ``_sum_groups_in_sql``, aber die Zeilen kommen einzeln und Python summiert mit ganzen Zahlen ohne Grenze."""
    select = ''.join(f'{key}, ' for key in keys)
    found: dict = {}
    for row in cursor.execute(f"SELECT {select}{', '.join(values)} {tail}", params):
        sums = found.setdefault(row[:len(keys)], [0] * (len(values) + 1))
        sums[0] += 1
        for index, value in enumerate(row[len(keys):], start=1):
            sums[index] += value or 0
    if not keys and not found:
        found[()] = [0] * (len(values) + 1)
    return [(*key, *sums) for key, sums in found.items()]


def _sum_groups(cursor, keys, values, tail, params):
    """Eine Abfrage mit Summen, die auch bei einem Bestand ohne Obergrenze gelingt: SQLite summiert in 64 Bit und wirft ab 2^63 ``integer
    overflow`` (Beträge bis 10^15 Minor Units, rund 9000 Sätze genügen); dann liest ``_sum_groups_in_python`` dieselben Zeilen und Python
    summiert. Beide Wege liefern dasselbe. Jeder andere ``sqlite3.OperationalError`` wird weitergereicht."""
    try:
        return _sum_groups_in_sql(cursor, keys, values, tail, params)
    except sqlite3.OperationalError as error:
        if 'integer overflow' not in str(error):
            raise
    return _sum_groups_in_python(cursor, keys, values, tail, params)


def _name_text(value) -> str:
    """Ein Name aus der Datenbank als Text: NULL ist leer, ein Blob wird als UTF-8 gelesen (was keines ist, als Ersatzzeichen), alles andere gilt als
    sein Text – die Spalte kann per SQL-Konsole jeden Wert tragen, und die Liste sortiert und verbindet Namen."""
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


_MONTH_ABBR = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun',
               'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']


#: Ab hier zählt eine Kommazahl aus der Datenbank nicht mehr als Betrag (``_minor_or_zero``): Eine Ganzzahl-Spalte fasst
#: 64 Bit, alles darüber hat jemand an der App vorbei geschrieben – und ``from_minor`` könnte es nicht mehr darstellen.
_MAX_MINOR = 2 ** 63


def _minor_or_zero(value) -> int:
    """Ein Betrag aus der Datenbank als ganze Minor Units: eine Ganzzahl (kein ``bool``) gilt, eine endliche
    Kommazahl unter 2^63 wird abgeschnitten, alles andere – Text, NULL, NaN, eine größere Kommazahl – zählt 0. Das Dashboard rechnet nie mit einem
    Betrag, den es nicht lesen kann, und wirft deshalb nie einen Fehler (per SQL-Konsole lässt sich jeder Wert
    schreiben)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and abs(value) < _MAX_MINOR:
        return int(value)
    return 0


def _strict_minor(value):
    """Ein Betrag aus der Datenbank als ganze Minor Units, wo er sich sicher lesen lässt: eine Ganzzahl (kein ``bool``) gilt, eine ganzzahlige endliche
    Kommazahl unter 2^63 wird zur Ganzzahl; alles andere – Text, NULL, ein Bruchteil, NaN, eine größere Zahl – ist ``None`` (nicht lesbar). Anders als
    ``_minor_or_zero`` wird nie still 0 daraus: Die Umsatzsteuer-Voranmeldung nennt so einen Satz."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and abs(value) < _MAX_MINOR:
        return int(value)
    return None


def _by_sign(rest):
    """Ein Rest nach seinem Vorzeichen: ``(Einnahme, Ausgabe)`` – positiv ist eine Einnahme, alles andere eine Ausgabe."""
    return (rest, 0) if rest > 0 else (0, rest)


def split_bank_movement(amount, children, parent_coa_ids, bank_coa_ids, payment_coa_ids, private_coa_ids,
                        covered=False):
    """Eine Bankbewegung ohne Rest in ``(Einnahme, privat, Ausgabe, Übertrag)`` zerlegen (Minor Units, seit 2.4.1).

    Die vier Teile ergeben immer ``amount``. ``children``: ``[(Amount, COA_ID, CounterCOA_ID, EntryForm)]`` der Sätze
    unter der Bewegung; ``parent_coa_ids``: COA-IDs des Kontos der Bewegung (leer/``None`` ohne SKR-Nummer);
    ``bank_coa_ids``, ``payment_coa_ids``, ``private_coa_ids``: eigene Bank-/Kassenkonten, alle Zahlungskonten, Konten
    2100–2199.

    Ohne Kind ist die Bewegung ganz Übertrag, wenn die Übertragsrechnung des DATEV-Exports sie deckt (``covered``,
    ``settle_transfers``), sonst nach dem Vorzeichen Einnahme oder Ausgabe. Mit Kindern zählt die Wirkung jedes Satzes
    auf das Konto der Bewegung (``bank_effect`` – wie in der Split-Summe):

    * privat: bei einer Umbuchung (``transfer``) mit 2100–2199 auf EINER Seite −Betrag (im Konto) bzw. +Betrag (im
      Gegenkonto), auf beiden Seiten 0 – auch wenn sie nicht ganz kontiert ist; bei jeder anderen Form außer den
      Spiegeln ihr Betrag (bei einer Zahlung die Richtung der Geldbewegung – auch, wenn sie über das Verrechnungskonto
      1460 läuft und auf das Konto der Bewegung selbst nicht wirkt);
    * Übertrag: nur, wenn die Bewegung mindestens einen Spiegel zwischen eigenen Konten trägt (``is_mirror_row``): der
      Betrag minus die Wirkung aller übrigen Sätze. Der Betrag des Spiegels selbst wird nie gelesen, auch nicht bei
      ``mirror_legacy`` (Lesart unbekannt);
    * der Rest geht nach seinem Vorzeichen in Einnahme oder Ausgabe."""
    total = _minor_or_zero(amount)
    if not children:
        if covered:
            return 0, 0, 0, total
        income, expense = _by_sign(total)
        return income, 0, expense, 0
    private = other = 0
    mirror = False
    for child_amount, coa, counter, stored in children:
        child_amount = _minor_or_zero(child_amount)
        if is_mirror_row(stored, coa, counter, bank_coa_ids):
            mirror = True
            continue
        form = read_entry_form(stored, coa, counter, payment_coa_ids)
        effect = bank_effect(form, child_amount, coa, counter, parent_coa_ids or set(), bank_coa_ids)
        other += effect
        coa_private, counter_private = coa in private_coa_ids, counter in private_coa_ids
        if form == FORM_TRANSFER:
            if coa_private != counter_private:
                private += -child_amount if coa_private else child_amount
        elif form not in MIRROR_FORMS and (coa_private or counter_private):
            # Der Betrag des Satzes, nicht seine Wirkung: Bei einer Zahlung trägt er die Richtung der Geldbewegung, und er
            # weicht von der Wirkung nur ab, wenn keine Seite ein eigenes Konto ist – „2100 an 1460“ unter der Bankbewegung
            # (WISO-Paar mit „1460 an Bank“, Kasse nicht auf 1460) hat die Wirkung 0 und bliebe sonst Betriebsausgabe.
            private += child_amount
    transfers = total - other if mirror else 0
    income, expense = _by_sign(total - private - transfers)
    return income, private, expense, transfers


def split_cash_entry(cash_amount, coa, counter, stored, bank_coa_ids, private_coa_ids):
    """Ein eigenständiger Satz auf einer Kasse als ``(Einnahme, privat, Ausgabe, Übertrag)`` – ganz in EINEM Teil.

    ``cash_amount`` ist der Betrag aus Sicht der Kasse (``_cash_amount_sql``). Ein Spiegel zwischen eigenen Konten
    (``is_mirror_row``, jede Spiegel-Form) ist ein Übertrag, ein Satz mit 2100–2199 auf einer Seite privat, alles andere
    zählt nach dem Vorzeichen."""
    total = _minor_or_zero(cash_amount)
    if is_mirror_row(stored, coa, counter, bank_coa_ids):
        return 0, 0, 0, total
    if coa in private_coa_ids or counter in private_coa_ids:
        return 0, total, 0, 0
    income, expense = _by_sign(total)
    return income, 0, expense, 0


def _dashboard_month_list(date_from, date_to):
    """``[(Jahr-Monat, Beschriftung)]`` je Kalendermonat von ``date_from`` bis ``date_to``, zeitlich geordnet; ``[]``,
    wenn Anfang oder Ende kein Jahr und keinen Monat nennen oder der Monat des Anfangs nicht 1–12 ist."""
    try:
        year, month = int(date_from[:4]), int(date_from[5:7])
        end = (int(date_to[:4]), int(date_to[5:7]))
    except (TypeError, ValueError):
        return []
    if not 1 <= month <= 12:
        return []
    cross_year = date_from[:4] != date_to[:4]
    months = []
    while (year, month) <= end:
        label = _MONTH_ABBR[month - 1] + (f" '{year % 100:02d}" if cross_year else '')
        months.append((f'{year:04d}-{month:02d}', label))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


class EuerEntry(NamedTuple):
    """Ein Satz, den ``get_euer_data`` liest (``ReportingMixin._euer_entries``), wie die Datenbank ihn trägt – die Beträge ungeprüft, der Steuerbetrag
    und der Steuersatz ohne NULL (0). ``parent_date``: das Datum der Bankbewegung (nur Sektion 1), sonst ``None``."""
    amount: object
    coa_id: object
    counter_coa_id: object
    tax_amount: object
    tax_rate: object
    stored_form: object
    parent_date: object
    entry_date: object
    section: int               # 1, 2 (auch 2b) oder 3


class EuerScan(NamedTuple):
    """Was ``_euer_entries`` liest: die Sätze (``{Buchungs-ID: EuerEntry}``, die erste Sektion gewinnt) und die Kontenmengen, mit denen Form und Art eines
    Satzes gelesen werden (Zahlungskonten von jetzt, Anlage- und Erfolgskonten)."""
    entries: dict
    liquid_coa_ids: set
    asset_coa_ids: set
    pl_coa_ids: set


class EuerTotals(NamedTuple):
    """Das Ergebnis von ``_euer_totals``: ``totals`` (Saldo je COA-ID) und ``ust_total`` als Akkumulatoren wie bisher, ``unassigned_input_tax`` (Vorsteuer
    ohne Vorsteuerkonto, Zeile 1409), dazu ``vat_minor`` (dieselben Summanden wie ``ust_total``) und ``input_tax_minor`` (die Summe aller Steuerbeträge im
    Vorsteuer-Zweig, mit dem Vorzeichen, wie gebucht) als ganze Zahlen."""
    totals: dict
    ust_total: float
    unassigned_input_tax: object
    vat_minor: int
    input_tax_minor: int


def _ustva_entry(booking_id, entry: EuerEntry, source: str, scan: EuerScan, coa_numbers: dict):
    """Ein gelesener Satz als ``db.ustva.UstvaEntry``: Form und Art wie in ``get_euer_data`` (``read_entry_form``, ``is_asset_transfer``; ein Satz der Sektion 3
    ist immer eine Umbuchung ohne Geldfluss), Kontonummern statt IDs und die Beträge streng gelesen (``_strict_minor``). Ein Datum, das kein Text ist
    (per SQL-Konsole schreibbar), liegt in keinem Zeitraum: leerer Text."""
    from .ustva import UstvaEntry
    form = read_entry_form(entry.stored_form, entry.coa_id, entry.counter_coa_id, scan.liquid_coa_ids)
    asset_transfer = entry.section == 3 or is_asset_transfer(entry.coa_id, entry.counter_coa_id, scan.liquid_coa_ids, scan.asset_coa_ids,
                                                              scan.pl_coa_ids, form=form)
    dates = [value if value is None or isinstance(value, str) else '' for value in (entry.entry_date, entry.parent_date)]
    return UstvaEntry(booking_id, source, form, coa_numbers.get(entry.coa_id), coa_numbers.get(entry.counter_coa_id), _strict_minor(entry.amount),
                      _strict_minor(entry.tax_amount), entry.tax_rate, dates[0], dates[1], asset_transfer)


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

    def _dashboard_scope(self, cursor, account_ids):
        """Welche Konten das Dashboard zählt: ``(Konten-IDs, COA-IDs der Kassen, Konten-IDs der Kassen)`` der gewählten
        Konten (leere Auswahl oder ``None`` = alle) – oder ``None``, wenn es kein Konto gibt und damit nichts zu zählen
        ist."""
        if account_ids:
            ph = ','.join('?' * len(account_ids))
            cursor.execute(
                f"SELECT ID, SKRAccount, IsCash FROM Accounts WHERE ID IN ({ph})",
                account_ids)
        else:
            cursor.execute("SELECT ID, SKRAccount, IsCash FROM Accounts")
        accounts = cursor.fetchall()
        acct_ids = [a[0] for a in accounts]
        cash_acct_ids = [a[0] for a in accounts if a[2]]
        cash_skr = [a[1] for a in accounts if a[2]]
        cash_coa_ids = []
        if cash_skr:
            cursor.execute(
                f"SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN "
                f"({','.join('?' * len(cash_skr))})", cash_skr)
            cash_coa_ids = [r[0] for r in cursor.fetchall()]
        if not (acct_ids or cash_coa_ids or cash_acct_ids):
            return None
        return acct_ids, cash_coa_ids, cash_acct_ids

    def _dashboard_movements(self, cursor, date_from, date_to, scope):
        """Die Bewegungen, die das Dashboard zählt – je Bewegung eine Zeile ``(Jahr-Monat, Einnahme, privat, Ausgabe,
        Übertrag)`` in Minor Units (Jahr-Monat ``None``, wenn SQLite das Datum nicht liest). ``scope``:
        ``_dashboard_scope``.

        Gezählt werden (A) die Bankbewegungen der gewählten Konten – auch die der Kasse (Buchungsmaske, Konto = Kasse) –
        und (C) eigenständige Sätze auf Kassenkonten; die Sätze unter einer Bankbewegung (B) zerlegt
        ``split_bank_movement``. Die Übertragsrechnung des DATEV-Exports liest den ganzen Bestand und läuft nur, wenn
        eine Bewegung ohne Satz dabei ist (``settle_transfers``)."""
        acct_ids, cash_coa_ids, cash_acct_ids = scope
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        payment_coa_ids = self._get_payment_coa_ids(cursor)
        account_coas = self._account_coa_sets(cursor)
        cursor.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber >= 2100 AND AccountNumber < 2200')
        private_coa_ids = {row[0] for row in cursor.fetchall()}

        # A: Bankbewegungen
        acct_marks = ','.join('?' * len(acct_ids))
        cursor.execute(
            f"SELECT b.ID, strftime('%Y-%m', b.DateBooking), b.Amount, b.Account_ID FROM Bookings b "
            f"WHERE b.BookingType = 'bank' AND b.DateBooking BETWEEN ? AND ? AND b.Account_ID IN ({acct_marks})",
            [date_from, date_to, *acct_ids])
        banks = cursor.fetchall()

        # B: ihre Buchungssätze, gesammelt je Bewegung
        cursor.execute(
            f"SELECT c.ParentBooking_ID, c.Amount, c.COA_ID, c.CounterCOA_ID, c.EntryForm FROM Bookings c "
            f"JOIN Bookings b ON b.ID = c.ParentBooking_ID "
            f"WHERE b.BookingType = 'bank' AND b.DateBooking BETWEEN ? AND ? AND b.Account_ID IN ({acct_marks})",
            [date_from, date_to, *acct_ids])
        children = {}
        for parent_id, *child in cursor.fetchall():
            children.setdefault(parent_id, []).append(tuple(child))

        needs_cover = any(bank_id not in children and isinstance(amount, int) and not isinstance(amount, bool)
                          and amount != 0 for bank_id, _month, amount, _account in banks)
        covered = self._settle_stock(cursor).covered if needs_cover else frozenset()
        rows = []
        for bank_id, month, amount, account_id in banks:
            rows.append((month, *split_bank_movement(
                amount, children.get(bank_id, ()), account_coas.get(account_id), bank_coa_ids, payment_coa_ids,
                private_coa_ids, bank_id in covered)))

        # C: eigenständige Sätze auf Kassenkonten – mit dem Konto der Kasse auf einer Seite (Section 2) oder mit der
        # Kasse als Account_ID (Section 2b: manuelle Einzelbuchungen, Kasse ohne SKRAccount)
        branches, branch_params = [], []
        if cash_coa_ids:
            coa_marks = ','.join('?' * len(cash_coa_ids))
            branches.append(f"(b.COA_ID IN ({coa_marks}) OR b.CounterCOA_ID IN ({coa_marks}))")
            branch_params += cash_coa_ids * 2
        if cash_acct_ids:
            cash_marks = ','.join('?' * len(cash_acct_ids))
            not_cash_coa = ''
            if cash_coa_ids:
                not_cash_coa = (f" AND NOT (COALESCE(b.COA_ID, -1) IN ({coa_marks})"
                                f" OR COALESCE(b.CounterCOA_ID, -1) IN ({coa_marks}))")
            # Eine AfA-Buchung alter Form (mit Account_ID) ist kein Geldfluss und zählt hier nicht.
            branches.append(f"(b.Account_ID IN ({cash_marks}){not_cash_coa} AND NOT EXISTS ("
                            f"SELECT 1 FROM AssetDepreciations d WHERE d.Booking_ID = b.ID))")
            branch_params += cash_acct_ids
            if cash_coa_ids:
                branch_params += cash_coa_ids * 2
        if branches:
            cash_sql, cash_params = _cash_amount_sql(cash_coa_ids)
            cursor.execute(
                f"SELECT strftime('%Y-%m', b.DateBooking), {cash_sql}, b.COA_ID, b.CounterCOA_ID, b.EntryForm "
                f"FROM Bookings b WHERE b.BookingType = 'entry' AND b.ParentBooking_ID IS NULL "
                f"AND b.DateBooking BETWEEN ? AND ? AND ({' OR '.join(branches)})",
                [*cash_params, date_from, date_to, *branch_params])
            for month, cash_amount, coa, counter, stored in cursor.fetchall():
                rows.append((month, *split_cash_entry(
                    cash_amount, coa, counter, stored, bank_coa_ids, private_coa_ids)))
        return rows

    def get_dashboard(self, date_from: str, date_to: str, account_ids: list | None = None):
        """Das Dashboard in EINEM Lauf über eine Verbindung: ``(totals, monthly)`` (seit 2.4.1).

        Das Dashboard folgt den Kontobewegungen: den Bankbewegungen aller gewählten Konten – auch der Kasse, wenn sie
        über die Buchungsmaske gebucht wird – und den eigenständigen Buchungssätzen auf Kassenkonten. Das
        Verrechnungskonto 1460 und die AfA (kein Geldfluss) zählen hier nicht. Jede Bewegung wird ohne Rest in vier
        Töpfe zerlegt (``split_bank_movement``, ``split_cash_entry``), ihre Summe ist der Saldo:

          - Einnahmen: positiver Rest der Bewegung
          - Betriebsausgaben: negativer Rest
          - Privatentnahmen: der Anteil der Konten 2100-2199 (netto: Einlagen gegen Entnahmen)
          - Überträge: Bewegungen zwischen eigenen Konten (Bank ↔ Bank, Bank ↔ Kasse) – weder Einnahme noch
            Ausgabe. Ein Übertrag auf ein nicht gewähltes Konto bleibt ein Übertrag; er steht dann netto hier.

        Args:
            date_from:   Start date  (YYYY-MM-DD)
            date_to:     End date    (YYYY-MM-DD)
            account_ids: Optional list of Account IDs to include.
                         None (or empty) = all accounts.

        Returns:
            ``totals``: income, private, expense, transfers, balance (= Summe der vier), bank_count (gezählte
            Bewegungen), unlinked_count (Bankbewegungen ohne Buchungssatz, ohne die über 0,00 €) – Beträge als float in Euro.
            ``monthly``: eine Liste, geordnet nach Monat, je Kalendermonat des Zeitraums ``{'year_month': 'YYYY-MM',
            'label': str, 'income': float, 'private': float, 'expense': float, 'transfers': float}``; eine Bewegung
            mit unlesbarem Datum steht nur in ``totals``. Ohne Konto ``([…alle 0…], [])``."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            scope = self._dashboard_scope(cursor, account_ids)
            if scope is None:
                return ({'income': 0, 'private': 0, 'expense': 0, 'transfers': 0, 'balance': 0,
                         'bank_count': 0, 'unlinked_count': 0}, [])
            rows = self._dashboard_movements(cursor, date_from, date_to, scope)
            acct_ids = scope[0]
            # Offen = Bankbewegungen ohne Buchungssatz; eine Bewegung über 0,00 € zählt nicht (nichts zu verbuchen)
            unlinked = cursor.execute(f'''
                SELECT COUNT(*) FROM Bookings b
                WHERE b.BookingType = 'bank' AND b.Amount IS NOT 0
                  AND b.DateBooking BETWEEN ? AND ? AND b.Account_ID IN ({','.join('?' * len(acct_ids))})
                  AND NOT EXISTS (
                      SELECT 1 FROM Bookings e
                      WHERE e.ParentBooking_ID = b.ID)
            ''', [date_from, date_to, *acct_ids]).fetchone()[0]
        finally:
            conn.close()

        income, private, expense, transfers = (sum(row[column] for row in rows) for column in range(1, 5))
        by_month = {}
        for month, *parts in rows:
            if month is not None:
                cell = by_month.setdefault(month, [0, 0, 0, 0])
                for column, part in enumerate(parts):
                    cell[column] += part
        monthly = []
        for month, label in _dashboard_month_list(date_from, date_to):
            month_income, month_private, month_expense, month_transfers = by_month.get(month, (0, 0, 0, 0))
            monthly.append({
                'year_month': month,
                'label': label,
                'income':    float(from_minor(month_income)),
                'private':   float(from_minor(month_private)),
                'expense':   float(from_minor(month_expense)),
                'transfers': float(from_minor(month_transfers)),
            })
        return {
            'income':         float(from_minor(income)),
            'private':        float(from_minor(private)),
            'expense':        float(from_minor(expense)),
            'transfers':      float(from_minor(transfers)),
            'balance':        float(from_minor(income + private + expense + transfers)),
            'bank_count':     len(rows),
            'unlinked_count': unlinked,
        }, monthly

    def get_dashboard_monthly(self, date_from: str, date_to: str,
                              account_ids: list | None = None):
        """Monthly 4-way split of bookings for the dashboard – der Monatsteil von ``get_dashboard``, dort
        die Regel (Kontobewegungen, vier Töpfe).

        Returns:
            list  ordered chronologically, one entry per calendar month in the
            range.  Each entry: {'year_month': 'YYYY-MM', 'label': str,
            'income': float, 'private': float, 'expense': float, 'transfers': float}.
        """
        return self.get_dashboard(date_from, date_to, account_ids)[1]

    def get_dashboard_totals(self, date_from: str, date_to: str,
                             account_ids: list | None = None):
        """Aggregate totals for the dashboard metric cards – der Summenteil von ``get_dashboard``, dort
        die Regel (Kontobewegungen, vier Töpfe).

        Returns dict with:
          income, private, expense, transfers, balance (= Summe der vier),
          bank_count, unlinked_count
        """
        return self.get_dashboard(date_from, date_to, account_ids)[0]

    def _euer_entries(self, cursor, date_from: str, date_to: str, account_ids: list | None = None,
                      only_asset_transfers: bool = False) -> EuerScan:
        """Die Sätze, die ``get_euer_data`` liest – die Sektionen 1, 2, 2b und 3 mit den Regeln aus dessen Docstring, je Satz (Schlüssel
        ist seine ID, die erste Sektion gewinnt) als ``EuerEntry`` samt Sektion und beiden Daten. Dazu die Kontenmengen, mit denen
        ``_euer_totals`` (und ``get_ustva``) die Form und die Art eines Satzes lesen. Rein lesend."""
        # Zahlungskonten von jetzt: nur für Zeilen ohne gespeicherte Form (Roh-SQL)
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

        # Jede Buchung zählt höchstens einmal: Schlüssel ist ihre ID, die erste Sektion gewinnt.
        # Die Sektionen sind so geschnitten, dass sie sich möglichst nicht überschneiden; die ID
        # sorgt dafür, dass auch eine Überschneidung nie doppelt zählt (z. B. ein Satz Anlage ↔
        # Erfolgskonto mit Account_ID = Kasse, den 2b und 3 beide träfen).
        entries: dict[int, EuerEntry] = {}

        def _collect(rows, section):
            for booking_id, *values in rows:
                entries.setdefault(booking_id, EuerEntry(*values, section))

        # Anlage- und Erfolgskonten: Umbuchungen zwischen beiden (AfA) gehören in Sektion 3
        asset_coa_ids = self._get_asset_coa_ids(cursor)
        pl_coa_ids = self._get_pl_coa_ids(cursor)
        asset_transfer_ids: set[int] = set()            # nur für ``only_asset_transfers``

        # ── 1. Entry-Kinder von Bank-Buchungen ───────────────────────
        if acct_ids:
            ph = ','.join('?' * len(acct_ids))
            cursor.execute(f"""
              SELECT e.ID, e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0), e.EntryForm, p.DateBooking, e.DateBooking
                FROM Bookings e
                JOIN Bookings p ON p.ID = e.ParentBooking_ID
                WHERE e.BookingType = 'entry'
                  AND p.Account_ID IN ({ph})
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
            """, acct_ids + [date_from, date_to])
            children = cursor.fetchall()
            _collect(children, 1)
            if only_asset_transfers:
                # Eine Umbuchung zwischen Anlage- und Erfolgskonto UNTER einer Bankbewegung (der Restbuchwert
                # im Split des Verkaufs, eine dort zugeordnete AfA) bewegt so wenig Geld wie eine eigenständige:
                # Ihre Wirkung auf das Konto der Bewegung ist 0. Sie zählt mit, was sie hier beisteuert.
                asset_transfer_ids.update(
                    row[0] for row in children
                    if is_asset_transfer(row[2], row[3], liquid_coa_ids, asset_coa_ids, pl_coa_ids,
                                         form=read_entry_form(row[6], row[2], row[3], liquid_coa_ids)))

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
              SELECT e.ID, e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0), e.EntryForm, NULL, e.DateBooking
                FROM Bookings e
                WHERE e.BookingType = 'entry'
                  AND e.ParentBooking_ID IS NULL
                  AND (e.COA_ID IN ({ph}) OR e.CounterCOA_ID IN ({ph}))
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
            """, coa_list + coa_list + [date_from, date_to])
            _collect(cursor.fetchall(), 2)

        # ── 2b. Kassen-Buchungen direkt via Account_ID ────────────────
        # Erfasst manuelle Einzel-Buchungen, bei denen Account_ID auf ein
        # Kassenkonto zeigt, aber kein COA-Spiegel gesetzt ist (z.B. ältere
        # Einträge ohne CounterCOA_ID oder Kasse ohne SKRAccount). Eine AfA-Buchung
        # alter Form (mit Account_ID) und eine Umbuchung zwischen Anlage- und
        # Erfolgskonto sind kein Geldfluss und gehören in Sektion 3 – auch wenn die
        # Maske ihnen eine Kasse als Konto eingetragen hat (sonst zählten sie doppelt).
        # Mit Kontofilter „nur Kasse“ zählen sie deshalb nicht: AfA hängt an keinem Konto.
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
              SELECT e.ID, e.Amount, e.COA_ID, e.CounterCOA_ID,
                  COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0), e.EntryForm, NULL, e.DateBooking
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
            _collect((row for row in cursor.fetchall()
                      if not is_asset_transfer(
                          row[2], row[3], liquid_coa_ids, asset_coa_ids, pl_coa_ids,
                          form=read_entry_form(row[6], row[2], row[3], liquid_coa_ids))), 2)

        # ── 3. Umbuchungen zwischen Anlage- und Erfolgskonto (AfA) ────
        # Nicht zahlungswirksam, an kein Konto gebunden: nur ohne Kontofilter. Sätze, die
        # eine AfA-Planzeile referenziert (auch alter Form: Aufwandskonto, kein
        # Gegenkonto), gehören immer dazu; alle übrigen nur als Anlagen-Umbuchung.
        # Eine solche Umbuchung trägt keine Zahlung, also auch keine Vorsteuer: Steuerbetrag
        # und -satz bleiben außen vor, der Betrag gilt, wie gebucht, als Aufwand (sonst käme
        # z. B. aus „6220 an 0520“ +119 mit 19 % eine Vorsteuer auf 1406 dazu). Der
        # Anlagenkauf mit Vorsteuer läuft als Kind einer Bankbewegung (Sektion 1).
        if unfiltered:
            asset_cond, asset_params = '', []
            if asset_coa_ids:
                asset_list = list(asset_coa_ids)
                ph = ','.join('?' * len(asset_list))
                asset_cond = f" OR e.COA_ID IN ({ph}) OR e.CounterCOA_ID IN ({ph})"
                asset_params = asset_list + asset_list
            cursor.execute(f"""
              SELECT e.ID, e.Amount, e.COA_ID, e.CounterCOA_ID,
                  EXISTS (SELECT 1 FROM AssetDepreciations d WHERE d.Booking_ID = e.ID),
                  e.EntryForm, e.DateBooking
                FROM Bookings e
                WHERE e.BookingType = 'entry'
                  AND e.ParentBooking_ID IS NULL
                  AND e.DateBooking BETWEEN ? AND ?
                  AND (e.Status IS NULL OR e.Status != 'resolved')
                  AND (EXISTS (SELECT 1 FROM AssetDepreciations d
                               WHERE d.Booking_ID = e.ID){asset_cond})
            """, [date_from, date_to] + asset_params)
            for (booking_id, amount, coa_id, counter_coa_id, depreciation_linked,
                 stored_form, entry_date) in cursor.fetchall():
                if depreciation_linked or is_asset_transfer(
                        coa_id, counter_coa_id, liquid_coa_ids, asset_coa_ids, pl_coa_ids,
                        form=read_entry_form(stored_form, coa_id, counter_coa_id,
                                             liquid_coa_ids)):
                    if booking_id not in entries:          # nur ein Satz, den Sektion 3 als erste findet
                        asset_transfer_ids.add(booking_id)
                    entries.setdefault(booking_id,
                                       EuerEntry(amount, coa_id, counter_coa_id, 0, 0, stored_form, None, entry_date, 3))
        if only_asset_transfers:
            entries = {booking_id: entries[booking_id] for booking_id in asset_transfer_ids}

        return EuerScan(entries, liquid_coa_ids, asset_coa_ids, pl_coa_ids)

    def _euer_totals(self, cursor, scan: EuerScan) -> EuerTotals:
        """Das Aufsummieren von ``get_euer_data``: je Satz das Zweckkonto bestimmen (Form, ``purpose_first`` das Konto, sonst das Gegenkonto; bei
        einer Umbuchung beide Seiten), den Saldo je Konto, die Umsatzsteuer aus Einnahmen (virtuelle Zeile 3806) und die Vorsteuer aus Ausgaben
        (1401/1406, Konten der Stammdaten, sonst die virtuelle Zeile 1409) bilden – die Beträge wie bisher in den Akkumulatoren ``totals`` und
        ``ust_total``. ``vat_minor`` und ``input_tax_minor`` sind dieselben Summanden als ganze Zahl, ohne Rundung und ohne ``float``. Rein
        lesend."""
        from collections import defaultdict
        from .susa import TAX_ACCOUNTS, TAX_INPUT          # hier, nicht oben: ``susa`` importiert dieses Modul

        entries, liquid_coa_ids, asset_coa_ids = scan.entries, scan.liquid_coa_ids, scan.asset_coa_ids

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
        report_accounts = self._report_tax_accounts(cursor)      # Vorsteuerkonten der Steuersätze (Stammdaten), je Satz

        neutral_coa_ids = self._get_neutral_coa_ids(cursor)

        # ── Zweck-Konto bestimmen und aggregieren ─────────────────────
        # Netto-Beträge pro Konto + virtuelle USt-Zeile (3806) + virtuelle Vorsteuerzeile (1409)
        totals: dict[int, float] = defaultdict(float)
        ust_total: float = 0.0   # nur USt aus Einnahmen → 3806
        unassigned_input_tax = 0  # Vorsteuer ohne Vorsteuerkonto → 1409
        vat_minor = 0            # dieselben Summanden wie ust_total, als ganze Zahl (Umsatzsteuer-Voranmeldung)
        input_tax_minor = 0      # jeder Steuerbetrag, der in den Vorsteuer-Zweig geht (Rohvorzeichen)

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

        for entry in entries.values():
            amount, coa_id, counter_coa_id, tax_amount, tax_rate, stored_form = entry[:6]
            netto = amount - tax_amount  # Brutto → Netto

            purpose_coa_id = None

            form = read_entry_form(stored_form, coa_id, counter_coa_id, liquid_coa_ids)
            if form in MIRROR_FORMS:
                continue                      # Übertrag zwischen Geldkonten

            if form == FORM_TRANSFER:
                # Umbuchung (beide Seiten kontiert, keine liquide, z. B. 4405→4400):
                # Betrag auf beide Konten verteilen, damit z. B. Erlöse unter 4400
                # erscheinen und 4405 reduziert wird. Anlagekonten bleiben draußen.
                purpose_coa_id = _purpose(counter_coa_id)
                _book_transfer(counter_coa_id, netto)
                if counter_coa_id in income_coa_ids:
                    ust_total += tax_amount
                    vat_minor += tax_amount
                _book_transfer(coa_id, -netto)
            else:
                # Zahlung: Das Zweckkonto ist die nicht-liquide Seite. Fehlt die
                # liquide Seite (Bankkonto ohne SKR-Nummer, nachträglich verknüpfter
                # Beleg), bleibt es eine Zahlung – früher lief so ein Satz in den
                # Umbuchungs-Zweig und kam mit umgedrehtem Vorzeichen und ohne
                # Steuer in die EÜR.
                # (Ohne Zweckkonto – nichts kontiert – ist ``purpose`` leer: nichts wird gebucht.)
                purpose = coa_id if form == FORM_PURPOSE_FIRST else counter_coa_id
                if purpose:
                    purpose_coa_id = _purpose(purpose)
                    _book(purpose, netto)
                    if purpose in income_coa_ids:
                        ust_total += tax_amount
                        vat_minor += tax_amount

            # Vorsteuer aus Ausgaben separat ausweisen: auf 1401/1406 (5, 7, 16, 19 %); jede andere – sie ist gezahlt und gehört
            # in die EÜR – auf dem Vorsteuerkonto der Stammdaten, sonst auf der virtuellen Zeile 1409.
            if (purpose_coa_id is not None
                    and purpose_coa_id not in income_coa_ids
                    and tax_amount != 0):
                input_tax_minor += tax_amount
                input_tax_account = None
                if abs(tax_rate - 0.05) < 0.0001 or abs(tax_rate - 0.07) < 0.0001:
                    input_tax_account = 1401
                elif abs(tax_rate - 0.16) < 0.0001 or abs(tax_rate - 0.19) < 0.0001:
                    input_tax_account = 1406
                if input_tax_account in input_tax_coa_ids:
                    totals[input_tax_coa_ids[input_tax_account]] += tax_amount
                else:
                    # Kein Satz von 5, 7, 16, 19 % (oder dessen Konto fehlt im Kontenrahmen, oder kein Satz): erst die feste Tabelle der Summen- und
                    # Saldenliste (sie fängt einen als Prozentzahl gespeicherten Satz, 19 statt 0,19), dann die Stammdaten, dann die virtuelle Zeile.
                    pct = datev_keys.percent(tax_rate)
                    fixed_account = TAX_ACCOUNTS[TAX_INPUT].get(pct)
                    if fixed_account in input_tax_coa_ids:
                        totals[input_tax_coa_ids[fixed_account]] += tax_amount
                    elif pct in report_accounts.input:
                        totals[report_accounts.input[pct]] += tax_amount
                    else:
                        unassigned_input_tax += tax_amount

        return EuerTotals(totals, ust_total, unassigned_input_tax, vat_minor, input_tax_minor)

    def get_euer_data(self, date_from: str, date_to: str,
                      account_ids: list | None = None, *,
                      only_asset_transfers: bool = False) -> list:
        """EÜR-Auswertung: Saldo pro SKR-Konto im Zeitraum.

        Für jede Entry-Buchung wird das „Zweck-Konto" ermittelt – nach der Form, die der Satz
        beim Schreiben bekommen hat (``Bookings.EntryForm``, ``db.matching``):
        - Geldkonto vorne (``liquid_first``) → CounterCOA_ID
        - Zweckkonto vorne (``purpose_first``) → COA_ID
        - Umbuchung (``transfer``) → beide Seiten
        - Spiegel (``mirror_*``, beide Seiten Geldkonten – Doppik) → ignoriert

        Die Form ist festgeschrieben: Ob ein Konto später Geldkonto wird oder aufhört, eines zu
        sein, ändert die EÜR eines gebuchten Satzes nicht. Nur eine Zeile ohne Form (per
        Roh-SQL angelegt) wird mit den Zahlungskonten von jetzt eingeordnet
        (``get_payment_coa_ids``, ``db.matching.classify_entry``). Fehlt die liquide Seite
        (Bankkonto ohne SKR-Nummer), bleibt der Satz eine Zahlung mit Vorzeichen und
        Steuer – als Umbuchung zählt er nur, wenn beide Seiten kontiert sind und keine
        liquide ist.

        Ob ein Satz in die EÜR kommt, entscheidet die Sektion, nicht die Kontenmenge:
        1. Buchungssätze unter einer Bankbewegung der gewählten Konten – auch der Kasse
           (Buchungsmaske mit Konto = Kasse);
        2. eigenständige Sätze (ohne Bankbewegung), die ein Kassenkonto berühren, und –
           ohne Kontofilter – das Verrechnungskonto 1460 (WISO: „Aufwand an Verrechnung“
           gilt mit seinem Datum als abgeflossen);
        2b. eigenständige Sätze mit Account_ID auf einer Kasse, die in 2 nicht vorkommen
           (ältere Einträge ohne Kassen-SKR); Umbuchungen zwischen Anlage- und
           Erfolgskonto und AfA-Buchungen alter Form gehören nicht dazu;
        3. ohne Kontofilter: Umbuchungen zwischen Anlage- und Erfolgskonto (AfA,
           Restbuchwert beim Abgang). Sie bewegen kein Geld und hängen an keinem Konto –
           auch nicht an dem in Account_ID eingetragenen; ihre Steuerangaben bleiben
           unberücksichtigt.
        Sektion 1 verlangt eine Bankbewegung als Elternzeile, 2, 2b und 3 verlangen keine –
        ein Satz zählt deshalb nur einmal, auch wenn der Abgleich ihn später verknüpft.
        Darüber hinaus bleibt jede Buchung über ihre ID eindeutig: Die erste Sektion, die
        sie findet, gewinnt.

        ``only_asset_transfers=True`` liefert nur die Zeilen aus den Sätzen, die kein Geld
        bewegen: die Umbuchungen zwischen Anlage- und Erfolgskonto (AfA, Restbuchwert beim
        Abgang) – die Sätze, die Sektion 3 beisteuert, und die Umbuchungen dieser Art unter
        einer Bankbewegung (Sektion 1, ``is_asset_transfer``: ihre Wirkung auf das Konto der
        Bewegung ist 0). Es sind dieselben Abfragen und dasselbe Aufsummieren – jeder Satz
        mit dem, was er im vollen Lauf beisteuert (ein Kind einer Bankbewegung also mit
        seiner Steuer) –, nur ohne die übrigen Sätze; für die Liquiditätsrechnung
        (``db.liquidity``). Ein Satz, den Sektion 2 oder 2b findet, gehört nicht dazu.
        Mit Kontofilter läuft Sektion 3 nicht; es bleiben dann nur die Umbuchungen unter den
        Bankbewegungen der gewählten Konten.

        Die Vorsteuer einer Ausgabe steht immer in der EÜR – sie ist gezahlt: auf 1401 (5 und 7 %)
        bzw. 1406 (16 und 19 %), wie bisher; zu jedem anderen Satz auf dem Vorsteuerkonto, das ein
        Eintrag der Steuersätze dafür nennt (``Database._report_tax_accounts``, erster Eintrag
        nach Reihenfolge und ID), sonst – auch bei einem Steuerbetrag ohne Satz und bei fehlendem
        1401/1406 im Kontenrahmen – auf der virtuellen Zeile 1409 „Vorsteuer ohne Steuerkonto“
        (``EUER_INPUT_TAX_UNASSIGNED``). Die Umsatzsteuer aus Einnahmen bleibt auf der virtuellen
        3806, gleich zu welchem Satz.

        Returns:
            list of (AccountNumber, Name, total_amount)
            sortiert nach AccountNumber, nur Konten mit Saldo ≠ 0.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        scan = self._euer_entries(cursor, date_from, date_to, account_ids, only_asset_transfers)
        t = self._euer_totals(cursor, scan)
        totals, ust_total, unassigned_input_tax = t.totals, t.ust_total, t.unassigned_input_tax

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

        # ── Virtuelle Zeile 1409 (Vorsteuer ohne Vorsteuerkonto) ──────
        unassigned_input_tax = from_minor(unassigned_input_tax)  # Minor Units -> Euro-Decimal
        if abs(unassigned_input_tax) >= Decimal('0.01'):
            result.append((EUER_INPUT_TAX_UNASSIGNED, EUER_INPUT_TAX_UNASSIGNED_NAME, float(unassigned_input_tax)))

        conn.close()
        result.sort(key=lambda x: x[0])
        return result

    def get_bwa(self, year: int, month: int, variant: str):
        """Betriebswirtschaftliche Auswertung für einen Monat (``db.bwa.BwaReport``): eine reine Umgruppierung von
        ``get_euer_data`` ohne Kontofilter – keine eigene Lesart der Buchungen. Gelesen wird je Zeitraum der Darstellung
        (``db.bwa.periods_for``); dieselbe Spanne nur einmal (im Januar sind Monat und aufgelaufen gleich). Rein lesend."""
        from .bwa import bwa_periods, build_report, periods_for
        spans = bwa_periods(year, month)
        periods = {key: spans[key] for key in periods_for(variant)}
        rows: dict[str, list] = {}
        by_span: dict[tuple, list] = {}
        for key, span in periods.items():
            if span not in by_span:
                by_span[span] = self.get_euer_data(*span)
            rows[key] = by_span[span]
        return build_report(year, month, variant, periods, rows, bank_skr_numbers(self.fetch_accounts()))

    def _liquidity_unbooked(self, cursor, date_from: str, date_to: str, acct_ids, covered=None):
        """Die Bankbewegungen ohne Buchungssatz im Zeitraum, die kein Übertrag deckt: ``(Summe in Minor Units, Zahl)``. Dieselbe Auswahl wie der Zweig
        „ohne Kind“ des Dashboards (``_dashboard_movements``, ``split_bank_movement``): Bankbewegungen der Konten ``acct_ids`` mit Datum im Zeitraum und
        ohne einen einzigen Satz (wie die Zählung ``unlinked`` in ``get_dashboard``); die Übertragsrechnung des DATEV-Exports (``_settle_stock``) läuft nur,
        wenn eine davon einen ganzzahligen Betrag ≠ 0 hat, und eine gedeckte Bewegung ist ein Übertrag und zählt hier nicht. Beträge über ``_minor_or_zero``
        (ein Wert, den das Dashboard nicht lesen kann, zählt 0 – die Bewegung zählt trotzdem mit). Eine Bewegung über genau 0,00 € zählt nicht (seit 2.8.4:
        nichts zu verbuchen, ``Amount IS NOT 0``). ``covered``: eine Funktion ohne Argument, die die
        gedeckten IDs liefert – so rechnet ein Aufruf über mehrere Zeiträume die Übertragsrechnung (sie liest den ganzen Bestand) nur einmal."""
        marks = ','.join('?' * len(acct_ids))
        cursor.execute(
            f"SELECT b.ID, b.Amount FROM Bookings b WHERE b.BookingType = 'bank' AND b.Amount IS NOT 0 AND b.DateBooking BETWEEN ? AND ? "
            f"AND b.Account_ID IN ({marks}) AND NOT EXISTS (SELECT 1 FROM Bookings e WHERE e.ParentBooking_ID = b.ID)",
            [date_from, date_to, *acct_ids])
        movements = cursor.fetchall()
        needs_cover = any(isinstance(amount, int) and not isinstance(amount, bool) and amount != 0 for _bank_id, amount in movements)
        covered_ids = (covered() if covered else self._settle_stock(cursor).covered) if needs_cover else frozenset()
        open_amounts = [_minor_or_zero(amount) for bank_id, amount in movements if bank_id not in covered_ids]
        return sum(open_amounts), len(open_amounts)

    def get_liquidity(self, year: int, month: int, variant: str):
        """Liquiditätsrechnung für einen Monat (``db.liquidity.LiquidityReport``): vom Ergebnis der betriebswirtschaftlichen Auswertung zur Veränderung der
        flüssigen Mittel – ohne eigene Lesart der Buchungen. Gelesen wird je Zeitraum der Darstellung (``db.bwa.periods_for``; dieselbe Spanne nur einmal, im
        Januar sind Monat und aufgelaufen gleich): die Zeilen von ``get_euer_data`` (ohne Kontofilter; daraus auch der Bericht der betriebswirtschaftlichen
        Auswertung) und die Töpfe des Dashboards (``_dashboard_movements``, alle Konten) – je Spanne ein Aufruf, damit Einnahme + privat + Ausgabe + Übertrag
        auf den Minor-Wert der Karte ``get_dashboard(…)['balance']`` derselben Spanne gleich sind. Dazu die Bankbewegungen ohne Buchungssatz
        (``_liquidity_unbooked``) und je Spanne die Zeilen der Umbuchungen zwischen Anlage- und Erfolgskonto (``get_euer_data(…, only_asset_transfers=True)``:
        dieselbe Abfrage, nur die Sätze, die Sektion 3 beisteuert) – nur sie rechnet die Überleitung als Abschreibung ohne Geldfluss zurück. Rein lesend.

        ``ValueError`` bei einem Monat außerhalb von 1 bis 12, einem Jahr außerhalb von 1 bis 9999 oder einer unbekannten Darstellung."""
        from .bwa import bwa_periods, build_report, periods_for
        from .liquidity import CashFigures, build_liquidity
        if not (isinstance(year, int) and isinstance(month, int) and 1 <= month <= 12 and 1 <= year <= 9999):
            raise ValueError(f'Ungültiger Zeitraum: Jahr {year!r}, Monat {month!r}')
        spans = bwa_periods(year, month)
        periods = {key: spans[key] for key in periods_for(variant)}
        rows: dict[str, list] = {}
        by_span: dict[tuple, list] = {}
        transfer_rows: dict[str, list] = {}
        transfers_by_span: dict[tuple, list] = {}
        for key, span in periods.items():
            if span not in by_span:
                by_span[span] = self.get_euer_data(*span)
                transfers_by_span[span] = self.get_euer_data(*span, only_asset_transfers=True)
            rows[key] = by_span[span]
            transfer_rows[key] = transfers_by_span[span]

        cash: dict[str, CashFigures] = {}
        figures: dict[tuple, CashFigures] = {}
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            scope = self._dashboard_scope(cursor, None)
            covered_ids: list = []

            def covered():
                if not covered_ids:
                    covered_ids.append(self._settle_stock(cursor).covered)
                return covered_ids[0]

            for key, span in periods.items():
                if span not in figures:
                    if scope is None:
                        figures[span] = CashFigures(0, 0, 0, 0)
                    else:
                        movements = self._dashboard_movements(cursor, span[0], span[1], scope)
                        income, private, expense, transfers = (sum(row[column] for row in movements) for column in range(1, 5))
                        unbooked, count = self._liquidity_unbooked(cursor, span[0], span[1], scope[0], covered)
                        figures[span] = CashFigures(income + private + expense, transfers, unbooked, count)
                cash[key] = figures[span]
        finally:
            conn.close()

        banks = bank_skr_numbers(self.fetch_accounts())
        bwa_report = build_report(year, month, variant, periods, rows, banks)
        return build_liquidity(year, month, variant, periods, rows, transfer_rows, bwa_report, banks, cash)

    def get_ustva(self, year: int, month: int, kind: str):
        """Umsatzsteuer-Voranmeldung für einen Monat oder ein Quartal (``db.ustva.UstvaReport``): die Kennzahlen 81, 86, 66 und 83 aus denselben Sätzen wie
        die EÜR (``_euer_entries`` ohne Kontofilter – es zählt das Datum der Bankbewegung bzw. der Kassenbuchung, auch bei der Vorsteuer) und drei Ergänzungen,
        damit nichts still fehlt: (a) Kinder einer Bankbewegung im Zeitraum, deren eigenes Datum außerhalb liegt, (b) eigenständige Sätze ohne Bankbewegung, die die
        EÜR nicht liest – außer der Forderung und der Gegenbuchung der Rechnungskette ohne Erlösseite: Sie bewegen kein Geld, die Steuer entsteht erst mit der Zahlung –
        und (c) Bankbewegungen ohne Satz (``_liquidity_unbooked``, nur gezählt). Wohin ein Satz gehört, steht in ``db.ustva.assign``; die Abstimmung nimmt
        Umsatzsteuer und Vorsteuer aus ``_euer_totals``. Rein lesend.

        ``ValueError`` wie ``db.ustva.ustva_period``: Jahr außerhalb von 1 bis 9999, Monat außerhalb von 1 bis 12, unbekannte Art oder ein Quartal, dessen
        Monat nicht 3, 6, 9 oder 12 ist."""
        from .ustva import SOURCE_EUER, SOURCE_OTHER_DATE, SOURCE_STANDALONE, build_ustva, is_chain_without_revenue, ustva_period
        date_from, date_to = ustva_period(year, month, kind)
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            scan = self._euer_entries(cursor, date_from, date_to)                                  # ohne Kontofilter, wie /euer
            # ``_euer_totals`` rechnet nur mit Zahlen: Ein Satz, dessen Betrag, Steuerbetrag oder Satz Text ist (per SQL-Konsole schreibbar), brächte es zum
            # Absturz, wie ``get_euer_data``. Er steht hier unter „Betrag nicht lesbar“ und fehlt nur in der Gegenrechnung mit der EÜR.
            summable = {booking_id: entry for booking_id, entry in scan.entries.items()
                        if all(isinstance(value, (int, float)) and math.isfinite(value) for value in (entry.amount, entry.tax_amount, entry.tax_rate))}
            euer = self._euer_totals(cursor, scan._replace(entries=summable))

            coa_numbers: dict = {}
            names: dict = {}
            for coa_id, number, name in cursor.execute('SELECT ID, AccountNumber, Name FROM ChartOfAccounts ORDER BY ID').fetchall():
                number = datev_keys.account_number(number)
                coa_numbers[coa_id] = number
                if number is not None:
                    names.setdefault(number, _name_text(name))

            entries = [_ustva_entry(booking_id, entry, SOURCE_EUER, scan, coa_numbers) for booking_id, entry in scan.entries.items()]
            columns = "e.ID, e.Amount, e.COA_ID, e.CounterCOA_ID, COALESCE(e.TaxAmount, 0), COALESCE(e.TaxRate, 0), e.EntryForm, {parent}, e.DateBooking"
            # (a) Kinder einer Bankbewegung im Zeitraum mit eigenem Datum außerhalb: dieselbe Auswahl wie Sektion 1 der EÜR, nur mit getauschten Daten
            for booking_id, *values in cursor.execute(
                    f"SELECT {columns.format(parent='p.DateBooking')} FROM Bookings e JOIN Bookings p ON p.ID = e.ParentBooking_ID "
                    "WHERE e.BookingType = 'entry' AND p.Account_ID IN (SELECT ID FROM Accounts) AND p.DateBooking BETWEEN ? AND ? "
                    "AND NOT (e.DateBooking BETWEEN ? AND ?) AND (e.Status IS NULL OR e.Status != 'resolved')",
                    [date_from, date_to, date_from, date_to]).fetchall():
                entries.append(_ustva_entry(booking_id, EuerEntry(*values, 0), SOURCE_OTHER_DATE, scan, coa_numbers))
            # (b) eigenständige Sätze im Zeitraum, die die EÜR nicht liest
            for booking_id, *values in cursor.execute(
                    f"SELECT {columns.format(parent='NULL')} FROM Bookings e "
                    "WHERE e.BookingType = 'entry' AND e.ParentBooking_ID IS NULL AND e.DateBooking BETWEEN ? AND ? "
                    "AND (e.Status IS NULL OR e.Status != 'resolved')", [date_from, date_to]).fetchall():
                if booking_id in scan.entries:
                    continue
                entry = _ustva_entry(booking_id, EuerEntry(*values, 0), SOURCE_STANDALONE, scan, coa_numbers)
                if not is_chain_without_revenue(entry):
                    entries.append(entry)
            # (c) Bankbewegungen ohne Satz, die kein Übertrag deckt
            scope = self._dashboard_scope(cursor, None)
            unbooked = self._liquidity_unbooked(cursor, date_from, date_to, scope[0]) if scope else (0, 0)
        finally:
            conn.close()
        return build_ustva(year, month, kind, entries, names, int(euer.vat_minor), int(euer.input_tax_minor), unbooked)

    def _susa_unbalanced(self, cursor, end: str, bound: int):
        """Die Bankbewegungen bis ``end``, deren Buchungssätze nicht ihren Betrag ergeben – dieselbe Regel und dieselbe Auswahl wie
        ``find_unbalanced_splits('0000-01-01', end)`` (Wirkung ``matching.bank_effect``), nur mit lesbaren Beträgen: ``find_unbalanced_splits`` bricht
        mit einem Fehler ab, sobald ein Betrag unter einer Bankbewegung Text ist (per SQL-Konsole schreibbar); die Liste soll an so einem Bestand nicht
        scheitern, der Betrag steht unter „nicht lesbar“. Returns ``(Zahl, Summe der Reste ohne Vorzeichen in Minor Units)``."""
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        account_coas = self._account_coa_sets(cursor)
        cursor.execute('''
            SELECT p.ID, p.Amount, p.Account_ID, e.Amount, e.COA_ID, e.CounterCOA_ID, e.EntryForm
            FROM Bookings p
            JOIN Bookings e ON e.ParentBooking_ID = p.ID
            WHERE p.BookingType = 'bank'
              AND p.ID IN (
                  SELECT b.ID FROM Bookings b WHERE b.BookingType = 'bank' AND b.DateBooking BETWEEN '0000-01-01' AND ?
                  UNION
                  SELECT c.ParentBooking_ID FROM Bookings c WHERE c.ParentBooking_ID IS NOT NULL AND c.DateBooking BETWEEN '0000-01-01' AND ?
              )
              AND typeof(p.Amount) = 'integer' AND p.Amount BETWEEN ? AND ?
              AND typeof(e.Amount) = 'integer' AND e.Amount BETWEEN ? AND ?
        ''', [end, end, -bound, bound, -bound, bound])
        amounts, sums = {}, {}
        for bank_id, amount, account_id, child_amount, coa, counter, form in cursor.fetchall():
            amounts[bank_id] = amount
            sums[bank_id] = sums.get(bank_id, 0) + bank_effect(form, child_amount, coa, counter, account_coas.get(account_id), bank_coa_ids)
        rests = [abs(amount - sums[bank_id]) for bank_id, amount in amounts.items() if amount != sums[bank_id]]
        return len(rests), sum(rests)

    def get_susa(self, year: int, month: int):
        """Summen- und Saldenliste bis zum Ende eines Monats (``db.susa.SusaReport``). Rein lesend: Sie stempelt nichts (kein ``DateTax``) und
        schreibt nichts.

        Gelesen werden dieselben Zeilen wie im Export des Buchungsstapels (``accounts.LEDGER_ROWS_WHERE``: alle Sätze außer den Bankbewegungen
        bis zum Monatsende, auch die der Vorjahre – sie bilden den Anfangssaldo), ohne die Eingangs-Sätze gepaarter Überträge, die der Export
        weglässt (``matching.settle_transfers``): Der Ausgangs-Satz bucht beide Bankseiten. Form, Soll und Haben und die Art liest
        ``db.susa.book_group`` mit denselben Funktionen wie der Export. Dazu kommen die Bankbewegungen ohne Buchungssatz, die kein Übertrag deckt:
        Das Bankkonto steht gegen „Nicht kontiert“, sonst stimmte es nicht mit dem Kontoauszug.

        Die Sätze werden in SQL nach dem gruppiert, was ihre Lesart bestimmt (Form, Konten, Richtung des Betrags und der Steuer, Steuersatz,
        Zeitraum, Konto der Bankbewegung), und je Gruppe einmal gebucht – die Regel ist linear, die Summen sind die der einzelnen Sätze. Eine
        Zeile je Satz in Python wäre bei 200 000 Buchungen deutlich langsamer; reicht eine Summe über die 64 Bit von SQLite hinaus, rechnet
        ``_sum_groups`` sie in Python. Nichts fällt still weg: Was sich nicht buchen lässt – ein Betrag, der keine ganze Zahl bis 10^15 Minor
        Units ist, ein Datum, das kein Kalendertag als Text ``JJJJ-MM-TT`` ist (``_SUSA_DATE_OK``; es fehlt in jedem Monat), eine Zeile ohne
        Buchungsart, ein Satz mit Betrag 0 und einem Steuerbetrag – wird gezählt (Hinweise); dazu die Sätze, die gebucht sind, aber den Abgleich
        der Bankkonten verschieben (ein anderes Datum als die Bankbewegung über das Monatsende, ein anderes eigenes Konto als das der Bewegung).
        Eine Zeile mit einer anderen Buchungsart als ``entry`` und ``bank`` liest der Export wie einen Satz, und so bucht sie die Liste.

        Der Steuerbetrag zu einem Satz außerhalb von 19, 16, 7 und 5 % steht auf dem Umsatzsteuer- bzw. Vorsteuerkonto, das ein Eintrag der Steuersätze dafür nennt
        (``_report_tax_accounts``: der Stand der Stammdaten von jetzt, auch für frühere Monate), sonst auf „Steuer ohne Steuerkonto“. Der Export für das Steuerbüro kennt
        diese Konten nicht.

        ``ValueError`` bei einem Monat außerhalb von 1 bis 12 oder einem Jahr außerhalb von 1 bis 9999."""
        from . import datev_keys
        from .accounts import LEDGER_ROWS_WHERE
        from .bwa import bwa_periods
        from .susa import (BAD_DATE, CHILD_OTHER_PERIOD, DOUBTFUL, NOT_POSTED, NOT_READABLE, TAX_INPUT, TAX_VAT, UNBALANCED, UNBOOKED,
                           UNBOOKED_NO_NUMBER, UNKNOWN_TYPE, ZERO_AMOUNT_TAX, BankCheck, GroupRow, Posting, SusaContext, Tally, book_group,
                           build_susa)
        if not (isinstance(year, int) and isinstance(month, int) and 1 <= month <= 12 and 1 <= year <= 9999):
            raise ValueError(f'Ungültiger Zeitraum: Jahr {year!r}, Monat {month!r}')
        month_start, end = bwa_periods(year, month)['month']
        year_start = f'{year:04d}-01-01'
        bound = _SUSA_MAX_MINOR
        window = LEDGER_ROWS_WHERE.format(a='e.')
        entry_date, parent_date, bank_date = (_SUSA_DATE_OK.format(a=alias) for alias in ('e.', 'p.', 'b.'))
        amount_ok = ("typeof(e.Amount) = 'integer' AND e.Amount BETWEEN ? AND ? "
                     "AND (e.TaxAmount IS NULL OR (typeof(e.TaxAmount) = 'integer' AND e.TaxAmount BETWEEN ? AND ?))")
        bounds = [-bound, bound, -bound, bound]
        readable_amount = "CASE WHEN typeof(Amount) = 'integer' AND Amount BETWEEN ? AND ? THEN ABS(Amount) ELSE 0 END"
        waiting = tuple(self.get_waiting_account_numbers())
        tally = Tally()

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            liquid = frozenset(self._get_payment_coa_ids(cursor))
            coa_numbers, names = {}, {}
            for coa_id, number, name in cursor.execute('SELECT ID, AccountNumber, Name FROM ChartOfAccounts ORDER BY ID').fetchall():
                number = datev_keys.account_number(number)
                coa_numbers[coa_id] = number
                if number is not None:
                    names.setdefault(number, (_name_text(name), False))
            accounts = [(account_id, _name_text(name), is_cash, datev_keys.account_number(skr))
                        for account_id, name, is_cash, skr in cursor.execute('SELECT ID, Name, IsCash, SKRAccount FROM Accounts').fetchall()]
            account_numbers = {account_id: number for account_id, _name, _cash, number in accounts}
            for _id, name, _cash, number in accounts:
                if number is not None:
                    names.setdefault(number, (name, True))                  # ein Bankkonto ohne Zeile im Kontenrahmen
            # Die Steuerkonten der Stammdaten für Sätze außerhalb von 19, 16, 7 und 5 % – als Kontonummern, denn die Liste bucht nach Nummern
            directory = self._report_tax_accounts(cursor)
            tax_accounts = {tax_type: {pct: coa_numbers[coa_id] for pct, coa_id in by_rate.items() if coa_numbers.get(coa_id) is not None}
                            for tax_type, by_rate in ((TAX_VAT, directory.vat), (TAX_INPUT, directory.input))}
            context = SusaContext(liquid, coa_numbers, account_numbers, waiting, tax_accounts)
            settlement = self._settle_stock(cursor)

            def read_groups(extra='', extra_params=()):
                """Die Sätze bis zum Monatsende, gruppiert nach dem, was ihre Lesart bestimmt (Eingabe von ``book_group``)."""
                keys = ('e.EntryForm', 'e.COA_ID', 'e.CounterCOA_ID', 'e.Amount > 0', 'e.TaxRate',
                        "CASE WHEN e.DateBooking < ? THEN 'prior' WHEN e.DateBooking < ? THEN 'year' ELSE 'month' END",
                        'p.Account_ID',
                        'CASE WHEN COALESCE(e.TaxAmount, 0) > 0 THEN 1 WHEN COALESCE(e.TaxAmount, 0) < 0 THEN -1 ELSE 0 END')
                tail = (f"FROM Bookings e LEFT JOIN Bookings p ON p.ID = e.ParentBooking_ID AND p.BookingType = 'bank' "
                        f"WHERE {window} AND {entry_date} AND e.Amount <> 0 AND {amount_ok} {extra}")
                found = _sum_groups(cursor, keys, ('e.Amount', 'COALESCE(e.TaxAmount, 0)'), tail,
                                    [year_start, month_start, '', end, *bounds, *extra_params])
                return [GroupRow(form, coa_id, counter_id, bool(inflow), rate, bucket, parent, count, amount, tax)
                        for form, coa_id, counter_id, inflow, rate, bucket, parent, _tax_side, count, amount, tax in found]

            for row in read_groups():
                book_group(tally, context, row)
            # Die Eingangs-Sätze gepaarter Überträge: mitgelesen, also wieder abziehen (dieselbe Gruppierung, nur diese Sätze)
            omitted = sorted(settlement.omitted)
            for start in range(0, len(omitted), _ID_CHUNK):
                chunk = omitted[start:start + _ID_CHUNK]
                for row in read_groups(f"AND e.ID IN ({','.join('?' * len(chunk))})", chunk):
                    book_group(tally, context, row, sign=-1)

            # ── Was sich nicht buchen lässt, wird gezählt ────────────────────────
            # Nicht lesbar: ein Betrag oder Steuerbetrag, der keine ganze Zahl bis 10^15 ist (Zeilen mit gültigem Datum bis zum Monatsende)
            unreadable = cursor.execute(
                f'SELECT COUNT(*) FROM Bookings e WHERE {window} AND {entry_date} AND NOT ({amount_ok})', ['', end, *bounds]).fetchone()[0]
            unreadable += cursor.execute(
                f"SELECT COUNT(*) FROM Bookings b WHERE b.BookingType = 'bank' AND b.DateBooking >= '' AND b.DateBooking <= ? AND {bank_date} "
                "AND NOT (typeof(b.Amount) = 'integer' AND b.Amount BETWEEN ? AND ?)", [end, -bound, bound]).fetchone()[0]
            tally.notice(NOT_READABLE, unreadable)
            # Kein Kalendertag als Datum: in keinem Monat zu finden, also ohne Fenster – jede Buchungsart, auch die Bankbewegungen (eine Zeile
            # ohne Buchungsart steht nur unter ``unknown_type``, jede Zeile in genau einem Hinweis)
            ((count, amount),) = _sum_groups(cursor, (), (readable_amount,),
                                             f"FROM Bookings WHERE BookingType IS NOT NULL AND NOT {_SUSA_DATE_OK.format(a='')}", [-bound, bound])
            tally.notice(BAD_DATE, count, amount)
            # Ohne Buchungsart: kein Satz und keine Bankbewegung – ``BookingType != 'bank'`` trifft NULL nicht, der Export liest die Zeile nie
            ((count, amount),) = _sum_groups(cursor, (), (readable_amount,), 'FROM Bookings WHERE BookingType IS NULL', [-bound, bound])
            tally.notice(UNKNOWN_TYPE, count, amount)
            # Betrag 0 mit Steuerbetrag: ohne Betrag gibt es keine Seite zu buchen, die Einnahmenüberschussrechnung zählt die Steuer
            ((count, amount),) = _sum_groups(
                cursor, (), ('ABS(e.TaxAmount)',),
                f'FROM Bookings e WHERE {window} AND {entry_date} AND e.Amount = 0 AND {amount_ok} AND COALESCE(e.TaxAmount, 0) <> 0',
                ['', end, *bounds])
            tally.notice(ZERO_AMOUNT_TAX, count, amount)

            # ── Bankbewegungen ohne Buchungssatz: das Bankkonto gegen „Nicht kontiert“ ──
            unbooked = incoming = outgoing = 0
            for bank_id, date, amount, account_id in cursor.execute(
                    "SELECT b.ID, b.DateBooking, b.Amount, b.Account_ID FROM Bookings b "
                    f"WHERE b.BookingType = 'bank' AND b.DateBooking >= '' AND b.DateBooking <= ? AND {bank_date} AND b.Amount <> 0 "
                    "AND NOT EXISTS (SELECT 1 FROM Bookings c WHERE c.ParentBooking_ID = b.ID)", (end,)).fetchall():
                if bank_id in settlement.covered:
                    continue                                                  # die Gegenseite eines Übertrags bucht sie schon
                if isinstance(amount, bool) or not isinstance(amount, int) or not -bound <= amount <= bound:
                    continue                                                  # als nicht lesbar schon gezählt
                number = account_numbers.get(account_id)
                if number is None:
                    tally.notice(UNBOOKED_NO_NUMBER, 1, abs(amount))
                    continue
                period = 'prior' if date < year_start else 'year' if date < month_start else 'month'
                tally.post(period, Posting(number, max(amount, 0), max(-amount, 0)))
                tally.post(period, Posting(NOT_POSTED, max(-amount, 0), max(amount, 0)))
                unbooked += 1
                incoming += max(amount, 0)
                outgoing += max(-amount, 0)
            tally.notice(UNBOOKED, unbooked, 0, incoming, outgoing)

            first_day, last_day = datetime.date(year, 1, 1), datetime.date.fromisoformat(end)
            tally.notice(DOUBTFUL, sum(1 for entry in settlement.doubtful.values() if first_day <= entry.day <= last_day))
            # Die Summe aller Bankbewegungen bis zum Monatsende, wie sie der Kontoauszug zeigt: Auch eine Bewegung, deren Datum als Text vor dem
            # Monatsende sortiert, ohne ein Kalendertag zu sein, zählt mit – fehlt sie in der Liste (``bad_date``), steht die Abweichung hier
            movements = {account: total for account, _count, total in _sum_groups(
                cursor, ('Account_ID',), ('Amount',),
                "FROM Bookings WHERE BookingType = 'bank' AND DateBooking >= '' AND DateBooking <= ? "
                "AND typeof(Amount) = 'integer' AND Amount BETWEEN ? AND ?", [end, -bound, bound])}
            tally.notice(UNBALANCED, *self._susa_unbalanced(cursor, end, bound))
            # Sätze, die gebucht sind und den Abgleich der Bankkonten trotzdem verschieben: Satz und Bankbewegung auf verschiedenen Seiten des
            # Monatsendes (nur einer von beiden zählt bis dahin) – das andere eigene Konto meldet ``db.susa.book_group``
            ((count, amount),) = _sum_groups(
                cursor, (), ('ABS(e.Amount)',),
                "FROM Bookings e JOIN Bookings p ON p.ID = e.ParentBooking_ID AND p.BookingType = 'bank' "
                "WHERE e.BookingType != 'bank' AND ((e.DateBooking <= ?) <> (p.DateBooking <= ?)) "
                f"AND e.Amount <> 0 AND {amount_ok} AND {entry_date} AND {parent_date}", [end, end, *bounds])
            tally.notice(CHILD_OTHER_PERIOD, count, amount)
        finally:
            conn.close()

        report = build_susa(year, month, tally.postings(), names, tally.notices(), [], [])

        # Der Abgleich mit den Bankbewegungen: je eigenem Bankkonto (keine Kasse) mit eigener SKR-Nummer der Endsaldo der Liste gegen die Summe
        # aller Bankbewegungen bis zum Monatsende
        closing = {account.number: account.closing for group in report.groups for account in group.accounts if account.number is not None}
        holders = Counter(number for _id, _name, _cash, number in accounts if number is not None)
        for account_id, name, is_cash, number in sorted(accounts, key=lambda item: (item[3] is None, item[3] or 0, item[1])):
            if is_cash == 1:
                continue
            if number is None or holders[number] > 1:
                report.bank_skipped.append(name)
            else:
                report.bank_checks.append(BankCheck(name, number, closing.get(number, 0), movements.get(account_id, 0)))
        return report