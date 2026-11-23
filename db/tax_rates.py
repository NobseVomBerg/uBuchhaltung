# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: die Steuersätze der Stammdaten (2.8.0, Schema 2008: Tabelle ``TaxRates``).

Ein Eintrag nennt, was die Auswahl eines Steuersatzes im Beleg liefert: die Bezeichnung, den Satz in Hundertstel Prozent (1900 = 19 %, 550 = 5,5 %),
die Art (``standard`` oder ``exempt`` = steuerfrei, dann mit dem Grund der Befreiung, der auf der Rechnung steht), das Erlöskonto (nur für 19 und 7 Prozent
darf es fehlen – für sie gibt es Standardkonten), freiwillig ein Umsatzsteuer- und ein Vorsteuerkonto (nur für die Auswertungen; nie für 0 Prozent und nie
für die Sätze mit festen Steuerkonten), ob er wählbar ist und an welcher Stelle der Auswahl er steht. Die Regeln stehen an EINER Stelle
(``tax_groups.check_tax_entry``, mit festen Sätzen); diese Datei liefert ihr die Konten, die nur die Datenbank kennt.

**Gelesen wird streng** (``fetch_tax_rates``): Eine Zeile, die diese Regeln nicht erfüllt – weil jemand sie über die SQL-Konsole geschrieben hat: eine Art,
die es nicht gibt, ein Satz, der keine ganze Zahl ist, ein Erlöskonto, das keines mehr ist –, hat ``valid = False``. Sie erscheint nie in einer Auswahl,
die Liste der Stammdaten nennt sie „ungültig“, und sie lässt sich löschen. **Wählbar** (``TaxEntry.selectable``) ist, was gültig und aktiv ist.

Geschrieben wird in EINER Transaktion (``BEGIN IMMEDIATE``): Prüfen, Lesen der Konten und Schreiben gehören zusammen, ein Fehler und jede Ablehnung
nehmen alles zurück, die Verbindung wird in jedem Fall geschlossen. Jeder Schreibweg geht – nach dem Commit – ins SQL-Protokoll. Ein Eintrag lässt sich
frei ändern und löschen, denn kein Beleg verweist auf ihn: Eine Position trägt eine Momentaufnahme (``entry_snapshot``), nie die ID. Nur der letzte
wählbare Eintrag bleibt, löschen und abschalten sind dort abgelehnt (``TAX_ENTRY_LAST_ACTIVE``).

**Die Steuerkonten in den Auswertungen** (``select_report_tax_accounts``, ``Database._report_tax_accounts``): Die EÜR (Vorsteuerkonto) und die Summen- und Saldenliste
(Umsatzsteuer- und Vorsteuerkonto) lesen den Stand von heute – je Art und Satz zählt der erste Eintrag mit einem solchen Konto nach (Reihenfolge, ID), aktiv oder nicht. Für die
Sätze mit festen Steuerkonten gilt dort allein die feste Tabelle; ein Satz ohne Konto steht in der Liste auf „Steuer ohne Steuerkonto“, in der EÜR auf der Zeile „Vorsteuer ohne
Steuerkonto“.

Die Funktionen ``legacy_expressible``, ``entry_snapshot`` und ``select_report_tax_accounts`` sind rein: Sie lesen nur, was man ihnen gibt.
"""
from collections import namedtuple
from typing import Mapping, NamedTuple

from tax_groups import EXEMPT, MAX_RATE_BP, MIXED_TAX_RATES, STANDARD, TAX_ENTRY_LAST_ACTIVE, bp_pct, check_tax_entry
from . import datev_keys, susa
from .bwa import group_of
from .core import DeleteRefused

# Die Konten, die ein Steuersatz als Umsatzsteuer- und als Vorsteuerkonto tragen darf, bestimmen die Gruppen der BWA (``db.bwa.group_of``): So steht ein
# gewähltes Konto in der EÜR und der BWA garantiert an der richtigen Stelle. Vorsteuer: 1400–1439. Umsatzsteuer: 3800–3849 (die 3806 über ``EXCEPTIONS``).
INPUT_TAX_GROUPS = ('input_vat',)
VAT_GROUPS = ('vat_collected', 'vat_settlement')
# Die Sätze mit festen Steuerkonten in der Summen- und Saldenliste (19, 16, 7 und 5 Prozent), in Hundertstel Prozent – aus der einen Tabelle dort abgeleitet.
# Ein Eintrag mit so einem Satz trägt kein Steuerkonto: Ein gewähltes würde still übergangen.
FIXED_TAX_BPS = frozenset(int(round(pct * 100)) for pct in susa.TAX_ACCOUNTS[susa.TAX_INPUT])

TAX_RATE_NOT_FOUND = 'Diesen Steuersatz gibt es nicht (mehr).'


class TaxRateRefused(ValueError):
    """Ein Eintrag der Steuersätze lässt sich so nicht speichern oder abschalten. Der Text ist der feste Klartext für den Nutzer (``tax_groups.TAX_ENTRY_*``,
    ``TAX_RATE_NOT_FOUND``) – nie der Text einer Systemausnahme. Nichts ist geschrieben."""


class TaxEntry(namedtuple('TaxEntry', 'id label bp kind reason revenue_coa_id vat_coa_id input_tax_coa_id active sort_order valid')):
    """Ein Eintrag der Stammdaten, wie ``fetch_tax_rates`` ihn liest: ``bp`` der Satz in Hundertstel Prozent, ``kind`` ``'standard'`` oder ``'exempt'``,
    ``reason`` der Grund der Befreiung (bei ``'standard'`` ohne Bedeutung), ``revenue_coa_id`` das Erlöskonto (``None`` = das Standardkonto von 19 und 7 Prozent),
    ``vat_coa_id`` und ``input_tax_coa_id`` die freiwilligen Steuerkonten, ``active`` ob er wählbar sein soll, ``sort_order`` die Stelle in der Auswahl.

    ``valid``: Die Zeile erfüllt die Regeln eines Eintrags (``tax_groups.check_tax_entry`` mit den Konten, die es heute gibt) und trägt in ``Active`` eine
    0 oder 1. Die übrigen Felder stehen so da, wie die Datenbank sie liefert – bei einer ungültigen Zeile auch unsinnige Werte; wer sie anzeigt, maskiert."""

    __slots__ = ()

    @property
    def selectable(self):
        """Wählbar: gültig und aktiv – nur so ein Eintrag erscheint in einer Auswahl und zählt für „der letzte wählbare“."""
        return self.valid and self.active


_ENTRY_COLUMNS = 'ID, Label, RateBp, Kind, ExemptReason, RevenueCoaId, VatCoaId, InputTaxCoaId, Active, SortOrder'
_Accounts = namedtuple('_Accounts', 'income vat input_tax')


def has_default_revenue(bp, kind):
    """Hat der Satz ein Standard-Erlöskonto, sodass der Eintrag ohne eigenes auskommt? Genau die Sätze, für die die App 4400 und 4300 kennt: die Art
    ``standard`` mit 19 oder 7 Prozent (``MIXED_TAX_RATES``). Jeder andere Satz und jede steuerfreie Leistung braucht ein Erlöskonto im Eintrag."""
    return kind == STANDARD and type(bp) is int and bp_pct(bp) in MIXED_TAX_RATES


def legacy_expressible(entry):
    """Trägt die Wahl dieses Eintrags der bisherige Payload eines Belegs – der Satz allein, ohne Eintrag – ohne Verlust? Ja bei der Art ``standard`` ohne
    Erlöskonto und mit ganzem Prozentsatz (die Vorbelegung 19 und 7 Prozent): Der Satz sagt dort alles, was der Eintrag sagt. Nein bei einem steuerfreien Eintrag
    (Grund und Konto), bei jedem mit Erlöskonto und bei einem Satz mit Nachkommastellen (5,5 %). Die Maske bekommt es je Eintrag als Wahrheitswert – sie kennt
    keinen Satz. Auch der Server hält sich daran: Für so einen Eintrag schreibt das Speichern keine Momentaufnahme, ob die Maske nun eine ID schickt oder nicht
    (``handlers._snapshot_to_write``) – „öffnen und unverändert speichern“ ändert so keine Spalte."""
    return entry.kind == STANDARD and entry.revenue_coa_id is None and type(entry.bp) is int and entry.bp % 100 == 0


def entry_snapshot(entry):
    """Die Momentaufnahme eines Eintrags für eine Position (``InvoiceItems``, Schlüssel wie ``db.invoices.ITEM_SNAPSHOT_KEYS``): die Art (``'E'`` bei
    steuerfrei, sonst ``'S'``), der Satz in Hundertstel Prozent, der Grund der Befreiung (nur bei steuerfrei, sonst ``None``) und das Erlöskonto. Eine Position
    trägt nie die ID des Eintrags: Was später in den Stammdaten geschieht, ändert keinen Beleg."""
    exempt = entry.kind == EXEMPT
    return {'tax_category': 'E' if exempt else 'S', 'tax_rate_bp': entry.bp, 'tax_exempt_reason': entry.reason if exempt else None,
            'revenue_coa_id': entry.revenue_coa_id}


class ReportTaxAccounts(NamedTuple):
    """Die Steuerkonten der Stammdaten für die Auswertungen, je Art ``{Prozentzahl: COA-ID}``: ``vat`` das Umsatzsteuer-, ``input`` das Vorsteuerkonto eines Satzes. Die
    Prozentzahl ist die Normierung der Buchungen (``datev_keys.percent``: 0,055 und 5,5 sind beide 5,5) – so findet eine Buchung den Satz, zu dem sie gehört."""
    vat: Mapping[float, int]
    input: Mapping[float, int]


def _account_in_groups(account_numbers, coa_id, groups):
    """Ist ``coa_id`` ein Konto, das es gibt, mit ganzzahliger Nummer, die HEUTE in einer der BWA-``groups`` liegt? Die Nummer kann sich nach dem Speichern des Eintrags
    geändert haben (der Kontenrahmen ist frei zu pflegen): Maßgeblich ist der Stand von jetzt."""
    number = account_numbers.get(coa_id) if type(coa_id) is int else None
    return type(number) is int and group_of(number) in groups


def select_report_tax_accounts(rows, account_numbers) -> ReportTaxAccounts:
    """Aus den Einträgen der Steuersätze die Steuerkonten, die EÜR und Summen- und Saldenliste für Sätze außerhalb von 19, 16, 7 und 5 Prozent nehmen – rein.

    ``rows``: ``(ID, RateBp, Kind, SortOrder, VatCoaId, InputTaxCoaId)`` in beliebiger Reihenfolge, so, wie die Datenbank sie liefert; ``account_numbers``:
    ``{COA-ID: Kontonummer oder None}``. Je Art und Satz zählt der ERSTE Eintrag nach ``(SortOrder, ID)``, der ein Konto dieser Art trägt – aktiv oder nicht (das
    Abschalten nimmt einem Satz sein Konto in den Auswertungen nicht: die Buchungen mit diesem Satz bleiben), und ein Eintrag ohne Konto davor versperrt keinem späteren
    mit Konto etwas. Übergangen werden: eine Zeile, die kein Eintrag ist (ein Satz, der keine ganze Zahl von 1 bis 10000 ist, eine andere Art als ``standard`` – auch
    die steuerfreie, ihr Satz ist 0), die Sätze mit festen Steuerkonten (``FIXED_TAX_BPS``: die Tabelle der Liste gilt zuerst, ein per SQL gesetztes Konto wirkt nicht)
    und ein Konto, das es nicht mehr gibt oder dessen Nummer heute nicht in ``VAT_GROUPS`` bzw. ``INPUT_TAX_GROUPS`` liegt."""
    vat, input_tax = {}, {}
    for _entry_id, bp, kind, _sort_order, vat_coa_id, input_tax_coa_id in sorted(
            rows, key=lambda row: (row[3] if type(row[3]) is int else 0, row[0])):
        if type(bp) is not int or not 1 <= bp <= MAX_RATE_BP or kind != STANDARD or bp in FIXED_TAX_BPS:
            continue
        pct = datev_keys.percent(bp / 10000)
        for found, coa_id, groups in ((vat, vat_coa_id, VAT_GROUPS), (input_tax, input_tax_coa_id, INPUT_TAX_GROUPS)):
            if pct not in found and _account_in_groups(account_numbers, coa_id, groups):
                found[pct] = coa_id
    return ReportTaxAccounts(vat, input_tax)


def _tax_account_choices(rows):
    """Aus Zeilen ``(COA-ID, Kontonummer, Name)`` die Konten, die als Umsatzsteuer- bzw. Vorsteuerkonto eines Satzes gelten: ``{'vat': […], 'input': […]}`` mit je
    ``(COA-ID, Kontonummer, Name)`` in der Reihenfolge von ``rows``. Maßgeblich ist die Gruppe der Kontonummer (``VAT_GROUPS``, ``INPUT_TAX_GROUPS``); ein Konto,
    dessen Nummer keine ganze Zahl ist, ist keines von beiden."""
    choices = {'vat': [], 'input': []}
    for coa_id, number, name in rows:
        if type(number) is not int:
            continue
        group = group_of(number)
        if group in VAT_GROUPS:
            choices['vat'].append((coa_id, number, name))
        elif group in INPUT_TAX_GROUPS:
            choices['input'].append((coa_id, number, name))
    return choices


def _in(account, ids):
    """Ist ``account`` die ID eines der Konten in ``ids``? Nur eine ganze Zahl (kein ``bool``) kann es sein."""
    return type(account) is int and account in ids


def _violation(label, bp, kind, reason, revenue_coa_id, vat_coa_id, input_tax_coa_id, accounts):
    """Der feste Satz der ersten verletzten Regel eines Eintrags oder ``None`` – ``tax_groups.check_tax_entry`` mit allem, was die Datenbank dazu weiß
    (``accounts``: die IDs der Erlös-, Umsatzsteuer- und Vorsteuerkonten, die es heute gibt). Dieselbe Stelle prüft, was geschrieben wird, und liest, was dasteht."""
    return check_tax_entry(label, bp, kind, reason, revenue_coa_id, has_default_revenue(bp, kind), _in(revenue_coa_id, accounts.income),
                           vat_coa_id=vat_coa_id, input_tax_coa_id=input_tax_coa_id,
                           has_fixed_tax_accounts=type(bp) is int and bp in FIXED_TAX_BPS,
                           vat_is_vat_account=_in(vat_coa_id, accounts.vat), input_is_input_account=_in(input_tax_coa_id, accounts.input_tax))


def _entry(row, accounts):
    """Ein ``TaxEntry`` aus einer Zeile von ``_ENTRY_COLUMNS``, streng gelesen (siehe ``TaxEntry.valid``)."""
    entry_id, label, bp, kind, reason, revenue_coa_id, vat_coa_id, input_tax_coa_id, active, sort_order = row
    flag_ok = type(active) is int and active in (0, 1)
    valid = flag_ok and _violation(label, bp, kind, reason, revenue_coa_id, vat_coa_id, input_tax_coa_id, accounts) is None
    return TaxEntry(entry_id, label, bp, kind, reason, revenue_coa_id, vat_coa_id, input_tax_coa_id, flag_ok and active == 1,
                    sort_order if type(sort_order) is int else 0, valid)


class TaxRatesMixin:
    # ── Lesen ───────────────────────────────────────────────────────────────

    def _tax_account_facts(self, conn):
        """Die IDs der Erlöskonten (``_income_coas``: 4000–4999 ohne die Wartekonten), der Umsatzsteuer- und der Vorsteuerkonten, wie die Datenbank sie heute führt."""
        income = frozenset(row[0] for row in self._income_coas(conn.cursor()))
        choices = _tax_account_choices(conn.execute('SELECT ID, AccountNumber, Name FROM ChartOfAccounts ORDER BY AccountNumber, ID').fetchall())
        return _Accounts(income, frozenset(row[0] for row in choices['vat']), frozenset(row[0] for row in choices['input']))

    def _read_tax_entries(self, conn, entry_id=None, accounts=None):
        """Die Einträge (oder der eine mit ``entry_id``) auf ``conn``, streng gelesen, nach ``SortOrder`` und ID."""
        sql, params = f'SELECT {_ENTRY_COLUMNS} FROM TaxRates', ()
        if entry_id is not None:
            sql, params = sql + ' WHERE ID = ?', (entry_id,)
        rows = conn.execute(sql + ' ORDER BY SortOrder, ID', params).fetchall()
        if not rows:
            return []
        accounts = accounts or self._tax_account_facts(conn)
        return [_entry(row, accounts) for row in rows]

    def fetch_tax_rates(self, active_only=False):
        """Die Einträge der Steuersätze als ``TaxEntry``, nach ``SortOrder`` und ID – auch die inaktiven und die ungültigen (``valid``). ``active_only``: nur die
        wählbaren (aktiv UND gültig): die Auswahl eines Belegs."""
        conn = self._get_connection()
        try:
            entries = self._read_tax_entries(conn)
        finally:
            conn.close()
        return [entry for entry in entries if entry.selectable] if active_only else entries

    def get_tax_rate(self, entry_id, conn=None):
        """Der Eintrag mit dieser ID als ``TaxEntry`` oder ``None``. Mit ``conn`` auf dieser Verbindung (etwa in der Transaktion eines Aufrufers; sie bleibt
        offen), sonst auf einer eigenen."""
        own = conn is None
        conn = self._get_connection() if own else conn
        try:
            found = self._read_tax_entries(conn, entry_id)
        finally:
            if own:
                conn.close()
        return found[0] if found else None

    def missing_revenue_coas(self, conn, coa_ids):
        """Die Konten unter ``coa_ids`` (die ``RevenueCoaId`` von Momentaufnahmen), die es auf ``conn`` nicht als Erlöskonto gibt: nicht im Kontenrahmen, außerhalb von
        4000–4999 oder ein Wartekonto (``_income_coas`` – dieselbe Regel wie beim Speichern eines Eintrags), als ``set``. ``InvoiceItems.RevenueCoaId`` hat keinen
        Fremdschlüssel: Wer eine Momentaufnahme schreibt, fragt es in seiner Transaktion, nachdem er die Schreibsperre hat (``BEGIN IMMEDIATE``) – bis zum Commit löscht oder
        ändert dann niemand das Konto. Rein lesend."""
        return set(coa_ids) - {row[0] for row in self._income_coas(conn.cursor())}

    def get_tax_account_choices(self):
        """Die Konten, aus denen der Nutzer das Umsatzsteuer- und das Vorsteuerkonto eines Satzes wählt: ``{'vat': [(COA-ID, Kontonummer, Name)], 'input': […]}`` nach
        Kontonummer, aus dem Kontenrahmen, gefiltert über dieselben Gruppen der BWA wie die Prüfung beim Speichern (``VAT_GROUPS``, ``INPUT_TAX_GROUPS``)."""
        conn = self._get_connection()
        try:
            return _tax_account_choices(conn.execute('SELECT ID, AccountNumber, Name FROM ChartOfAccounts ORDER BY AccountNumber, ID').fetchall())
        finally:
            conn.close()

    def _report_tax_accounts(self, cursor) -> ReportTaxAccounts:
        """Die Steuerkonten der Stammdaten für die Auswertungen (``select_report_tax_accounts``), gelesen im Cursor des Aufrufers: ``get_euer_data`` und ``get_susa`` rufen es
        einmal je Aufruf, in ihrer eigenen Verbindung und ohne Transaktion – rein lesend. Ohne ein Konto in einem Eintrag (der Normalfall) bleibt es bei der einen Abfrage der
        Einträge; sonst kommt die Liste der Konten des Kontenrahmens dazu (nur ID und Nummer, ohne Obergrenze der Parameter)."""
        rows = cursor.execute('SELECT ID, RateBp, Kind, SortOrder, VatCoaId, InputTaxCoaId FROM TaxRates').fetchall()
        if not any(type(coa_id) is int for row in rows for coa_id in row[4:]):
            return ReportTaxAccounts({}, {})
        return select_report_tax_accounts(rows, dict(cursor.execute('SELECT ID, AccountNumber FROM ChartOfAccounts').fetchall()))

    def tax_rate_waiting_account(self, bp):
        """Das Wartekonto, auf dem eine Rechnung zu GENAU diesem Satz (in Hundertstel Prozent) bis zur Zahlung steht, als ``(Kontonummer, Name)`` – oder ``None``:
        Dann bucht die App erst mit der Zahlung. Es ist die Tabelle ``OPEN_REVENUE_ACCOUNTS`` (nur ganze Prozentsätze; die 0 gilt für 0 % und für steuerfrei),
        nicht auf ganze Prozent gerundet. Nur eine Anzeige der Stammdaten: Der Eintrag legt kein Wartekonto fest."""
        if type(bp) is not int or bp % 100:
            return None
        return self.OPEN_REVENUE_ACCOUNTS.get(bp // 100)

    # ── Schreiben ───────────────────────────────────────────────────────────

    def _refuse_invalid_tax_entry(self, accounts, label, bp, kind, reason, revenue_coa_id, vat_coa_id, input_tax_coa_id):
        problem = _violation(label, bp, kind, reason, revenue_coa_id, vat_coa_id, input_tax_coa_id, accounts)
        if problem:
            raise TaxRateRefused(problem)

    def insert_tax_rate(self, *, label, bp, kind=STANDARD, reason=None, revenue_coa_id=None, vat_coa_id=None, input_tax_coa_id=None, sort_order=None,
                        active=True):
        """Einen Eintrag anlegen und seine ID liefern. Die Regeln prüft ``tax_groups.check_tax_entry`` mit den Konten dieser Datenbank in der Transaktion
        (``TaxRateRefused`` mit festem Satz, nichts geschrieben, kein Zähler verbraucht). ``label`` und ``reason`` so, wie sie gespeichert werden (der Aufrufer entfernt
        Leerraum am Rand); ``bp`` ist der Satz in Hundertstel Prozent (``tax_groups.parse_rate_bp``). Den Grund legt nur ein steuerfreier Eintrag ab.
        ``sort_order``: die Stelle in der Auswahl; ohne Angabe kommt der Eintrag ans Ende (zehn über dem größten Wert)."""
        with self.transaction() as tx:
            conn = tx.conn
            conn.execute('BEGIN IMMEDIATE')
            self._refuse_invalid_tax_entry(self._tax_account_facts(conn), label, bp, kind, reason, revenue_coa_id, vat_coa_id, input_tax_coa_id)
            if sort_order is None:
                sort_order = conn.execute('SELECT COALESCE(MAX(SortOrder), 0) + 10 FROM TaxRates').fetchone()[0]
            sql = ('INSERT INTO TaxRates (Label, RateBp, Kind, ExemptReason, RevenueCoaId, VatCoaId, InputTaxCoaId, Active, SortOrder) '
                   'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)')
            params = (label, bp, kind, reason if kind == EXEMPT else None, revenue_coa_id, vat_coa_id, input_tax_coa_id, 1 if active else 0, sort_order)
            entry_id = conn.execute(sql, params).lastrowid
            tx.log.append((sql, params, 'Insert tax rate'))
        return entry_id

    def update_tax_rate(self, entry_id, *, label, bp, kind, reason=None, revenue_coa_id=None, vat_coa_id=None, input_tax_coa_id=None, sort_order=None,
                        active=None):
        """Einen Eintrag ändern: Alle Felder werden neu geschrieben (ein fehlendes Konto leert es, wie das Formular), ``sort_order`` und ``active`` nur, wenn sie
        genannt sind (``None`` lässt den Wert). Geprüft wie beim Anlegen; ``TaxRateRefused`` auch, wenn es den Eintrag nicht gibt (``TAX_RATE_NOT_FOUND``) und
        wenn der letzte wählbare Eintrag abgeschaltet würde (``TAX_ENTRY_LAST_ACTIVE``). Er wirkt auf neue und neu gespeicherte Entwürfe: Keine Position verweist auf
        den Eintrag."""
        with self.transaction() as tx:
            conn = tx.conn
            conn.execute('BEGIN IMMEDIATE')
            accounts = self._tax_account_facts(conn)
            current = self._read_tax_entries(conn, entry_id, accounts)
            if not current:
                raise TaxRateRefused(TAX_RATE_NOT_FOUND)
            self._refuse_invalid_tax_entry(accounts, label, bp, kind, reason, revenue_coa_id, vat_coa_id, input_tax_coa_id)
            if active is not None and not active:
                self._refuse_last_selectable(conn, current[0], accounts, TaxRateRefused)
            sql = ('UPDATE TaxRates SET Label = ?, RateBp = ?, Kind = ?, ExemptReason = ?, RevenueCoaId = ?, VatCoaId = ?, InputTaxCoaId = ?, '
                   'Active = COALESCE(?, Active), SortOrder = COALESCE(?, SortOrder) WHERE ID = ?')
            params = (label, bp, kind, reason if kind == EXEMPT else None, revenue_coa_id, vat_coa_id, input_tax_coa_id,
                      None if active is None else (1 if active else 0), sort_order, entry_id)
            conn.execute(sql, params)
            tx.log.append((sql, params, 'Update tax rate'))

    def _refuse_last_selectable(self, conn, entry, accounts, refusal):
        """Wirft ``refusal(TAX_ENTRY_LAST_ACTIVE)`` (die Ablehnungsklasse des Aufrufers), wenn ``entry`` der letzte wählbare Eintrag ist. Gibt es einen anderen
        wählbaren, oder ist ``entry`` selbst nicht wählbar (inaktiv, ungültig), geht es durch."""
        if entry.selectable and not any(other.selectable for other in self._read_tax_entries(conn, None, accounts) if other.id != entry.id):
            raise refusal(TAX_ENTRY_LAST_ACTIVE)

    def set_tax_rate_active(self, entry_id, active):
        """Einen Eintrag wählbar machen (``active`` wahr) oder abschalten: Er bleibt in der Liste der Stammdaten und verschwindet aus der Auswahl. Den letzten
        wählbaren Eintrag lässt es nicht abschalten (``TaxRateRefused`` mit ``TAX_ENTRY_LAST_ACTIVE``), einen unbekannten nicht (``TAX_RATE_NOT_FOUND``)."""
        with self.transaction() as tx:
            conn = tx.conn
            conn.execute('BEGIN IMMEDIATE')
            accounts = self._tax_account_facts(conn)
            current = self._read_tax_entries(conn, entry_id, accounts)
            if not current:
                raise TaxRateRefused(TAX_RATE_NOT_FOUND)
            if not active:
                self._refuse_last_selectable(conn, current[0], accounts, TaxRateRefused)
            sql, params = 'UPDATE TaxRates SET Active = ? WHERE ID = ?', (1 if active else 0, entry_id)
            conn.execute(sql, params)
            tx.log.append((sql, params, 'Set tax rate active'))

    def delete_tax_rate(self, entry_id):
        """Einen Eintrag löschen. Immer erlaubt – kein Beleg verweist auf ihn, die Positionen tragen eine Momentaufnahme –, außer beim letzten wählbaren Eintrag
        (``DeleteRefused`` mit ``TAX_ENTRY_LAST_ACTIVE``, nichts gelöscht). Ein unwählbarer, auch ein ungültiger Eintrag lässt sich also löschen. Eine unbekannte ID
        ist kein Fehler. Prüfung und Löschen laufen in einer Transaktion."""
        with self.transaction() as tx:
            conn = tx.conn
            conn.execute('BEGIN IMMEDIATE')
            accounts = self._tax_account_facts(conn)
            current = self._read_tax_entries(conn, entry_id, accounts)
            if not current:
                return
            self._refuse_last_selectable(conn, current[0], accounts, DeleteRefused)
            sql, params = 'DELETE FROM TaxRates WHERE ID = ?', (entry_id,)
            conn.execute(sql, params)
            tx.log.append((sql, params, 'Delete tax rate'))
