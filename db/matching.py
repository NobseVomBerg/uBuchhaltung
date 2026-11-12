# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: matching."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor, is_covered


def is_bank_effective(coa_id, counter_coa_id, bank_coa_ids) -> bool:
    """Bewegt dieser Buchungssatz Geld auf dem Bankkonto?

    Unter einer Bankbewegung hängen nicht nur Zahlungen, sondern auch reine
    Umbuchungen: die Zahlungs-Umbuchung 4405→4400 einer Rechnung oder der
    Privatanteil 6805→2100. Sie gehören zum Beleg, bewegen aber kein Geld und
    dürfen deshalb nicht in die Split-Summe eingehen.

    Nicht bankwirksam ist ein Satz nur dann, wenn BEIDE Seiten kontiert sind
    und KEINE davon ein Bankkonto ist. Unvollständig kontierte Sätze zählen
    bewusst mit – sonst sähe ein halbfertiger Split vollständig aus.
    """
    if coa_id in bank_coa_ids or counter_coa_id in bank_coa_ids:
        return True
    return coa_id is None or counter_coa_id is None


ENTRY_MIRROR = 'mirror'                # beide Seiten liquide (Übertrag/Doppik-Spiegel)
ENTRY_LIQUID_FIRST = 'liquid_first'    # Geldkonto vorne (oder vorne leer), Zweckkonto hinten
ENTRY_PURPOSE_FIRST = 'purpose_first'  # Zweckkonto vorne, Geldkonto hinten (oder hinten leer)
ENTRY_TRANSFER = 'transfer'            # Umbuchung: beide Seiten kontiert, keine liquide
ENTRY_EMPTY = 'empty'                  # kein Zweckkonto erkennbar


def classify_entry(coa_id, counter_coa_id, liquid_coa_ids) -> str:
    """Form eines Buchungssatzes – EINE Einordnung für EÜR, DATEV-Export und Masken.

    Bei einer Zahlung trägt der Betrag das Vorzeichen der Geldbewegung (Einnahme
    positiv), egal auf welcher Seite das Zweckkonto steht. Fehlt die liquide Seite
    (Bankkonto ohne SKR-Nummer, unvollständig kontierter Satz), bleibt es eine
    Zahlung – dieselbe Sicht wie ``is_bank_effective``. Eine Umbuchung liegt nur
    vor, wenn BEIDE Seiten kontiert sind und keine liquide ist; dort bedeutet ein
    positiver Betrag: COA im Soll, Gegenkonto im Haben.
    """
    coa_liquid = coa_id in liquid_coa_ids
    counter_liquid = counter_coa_id in liquid_coa_ids
    if coa_liquid and counter_liquid:
        return ENTRY_MIRROR
    if coa_liquid:
        return ENTRY_LIQUID_FIRST
    if coa_id is None:
        if counter_coa_id is None or counter_liquid:
            return ENTRY_EMPTY
        return ENTRY_LIQUID_FIRST
    if counter_coa_id is None or counter_liquid:
        return ENTRY_PURPOSE_FIRST
    return ENTRY_TRANSFER


# ── Gespeicherte Form (Bookings.EntryForm) ───────────────────────────────────
# ``classify_entry`` fragt die Kontenliste von HEUTE. Ein Satz, der gestern geschrieben wurde,
# soll aber morgen noch genauso gelesen werden – auch wenn ein Konto inzwischen Geldkonto
# geworden ist oder aufgehört hat, eines zu sein. Deshalb schreibt jeder Schreibweg die Form
# als Daten mit, und die Auswertungen lesen sie, statt sie neu zu erraten.
#
#   Form            Geldseite          Betrag = Richtung auf …   bei Betrag > 0 im Soll
#   purpose_first   Gegenkonto/leer    dem Gegenkonto            Gegenkonto
#   liquid_first    Konto/leer         dem Konto                 Konto
#   transfer        keine              (Umbuchung)               Konto
#   mirror_coa      beide              dem Konto                 Konto
#   mirror_counter  beide              dem Gegenkonto            Gegenkonto
#   mirror_legacy   beide              unbekannt (Altbestand)    Gegenkonto
#
# ``mirror_legacy`` setzt die Migration auf 2.1.0 bei jedem Spiegel des Bestands: Welche der
# beiden Konventionen ein Import oder die Maske damals benutzt hat, steht nicht in den Daten.
# Die Zeile wird gelesen, wie sie vorher gelesen wurde. Kein Import stellt sie um (die Importe
# zählen sie nur: ``legacy_mirrors``), und eine Maske ändert ihre Form nur zusammen mit den Konten
# (``reform``); die Umstellung bleibt einer Prüfliste vorbehalten, die den Nutzer entscheiden
# lässt (``docs/aufgaben.md``).
FORM_PURPOSE_FIRST = ENTRY_PURPOSE_FIRST
FORM_LIQUID_FIRST = ENTRY_LIQUID_FIRST
FORM_TRANSFER = ENTRY_TRANSFER
FORM_MIRROR_COA = 'mirror_coa'
FORM_MIRROR_COUNTER = 'mirror_counter'
FORM_MIRROR_LEGACY = 'mirror_legacy'

ENTRY_FORMS = (FORM_PURPOSE_FIRST, FORM_LIQUID_FIRST, FORM_TRANSFER,
               FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY)
MIRROR_FORMS = (FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY)

SIDE_COA = 'coa'
SIDE_COUNTER = 'counter'

_SOLL_SIDE = {
    FORM_PURPOSE_FIRST: SIDE_COUNTER,
    FORM_LIQUID_FIRST: SIDE_COA,
    FORM_TRANSFER: SIDE_COA,
    FORM_MIRROR_COA: SIDE_COA,
    FORM_MIRROR_COUNTER: SIDE_COUNTER,
    FORM_MIRROR_LEGACY: SIDE_COUNTER,
}


def soll_side(form) -> str:
    """Welche Seite des Satzes steht bei positivem Betrag im Soll? ``'coa'`` (Feld „Konto“)
    oder ``'counter'`` (Gegenkonto). Daran hängt das Soll/Haben-Kennzeichen des DATEV-Exports."""
    return _SOLL_SIDE[form]


def derive_entry_form(coa_id, counter_coa_id, money_ids, sign_side) -> str:
    """Form für einen Satz, der JETZT geschrieben wird – aus den Konten und der Menge der
    Geldkonten (``Database.get_payment_coa_ids``) zu diesem Zeitpunkt.

    Bei einer Zahlung folgt die Form der Kontierung (``classify_entry``); ein Satz ohne
    Zweckkonto (heute ``ENTRY_EMPTY``) gilt als „Zweckkonto vorne“: Die EÜR bucht nichts,
    der Betrag steht wie bisher auf der Gegenkonto-Seite. Nur beim Spiegel (beide Seiten
    Geldkonten) lässt sich die Form nicht aus den Konten ablesen: ``sign_side`` sagt, auf
    welche Seite sich der Betrag bezieht – ``'coa'`` (Importe: positiv = Konto im Soll) oder
    ``'counter'`` (Buchungsmaske: Betrag = Richtung auf dem Bankkonto im Gegenkonto)."""
    kind = classify_entry(coa_id, counter_coa_id, money_ids)
    if kind == ENTRY_MIRROR:
        return FORM_MIRROR_COA if sign_side == SIDE_COA else FORM_MIRROR_COUNTER
    if kind == ENTRY_EMPTY:
        return FORM_PURPOSE_FIRST
    return kind


def _form_from_roles(coa_id, coa_is_money, counter_coa_id, counter_is_money, sign_side) -> str:
    """Wie ``derive_entry_form``, nur sagt der Aufrufer selbst, welche Seite ein Geldkonto ist
    (statt dass die Kontenliste von heute es entscheidet). Eine Seite ohne Konto ist nie eines."""
    money = set()
    if coa_is_money and coa_id is not None:
        money.add(coa_id)
    if counter_is_money and counter_coa_id is not None:
        money.add(counter_coa_id)
    return derive_entry_form(coa_id, counter_coa_id, money, sign_side)


def import_entry_form(coa_id, coa_is_money, counter_coa_id, counter_is_money) -> str:
    """Form eines Satzes, den ein Import schreibt. Wie ``derive_entry_form``, nur bestimmt der
    Import selbst, welche Seite ein Geldkonto ist (nach Kontonummern, daran richtet er auch das
    Vorzeichen aus), nicht die Kontenliste. Eine Seite ohne Konto ist nie ein Geldkonto. Ein
    Spiegel trägt den Rohbetrag der Datei: positiv heißt „Konto im Soll“ (``mirror_coa``).

    Ist keine Seite ein Zahlungskonto, bleibt der Rohbetrag, und WISO meint „Konto im Soll“:
    ``transfer`` (positiv = Konto im Soll) – auch bei leerem oder unbekanntem Gegenkonto, nie
    ``purpose_first`` (dort stünde das Gegenkonto im Soll)."""
    if not (coa_is_money or counter_is_money):
        return FORM_TRANSFER
    return _form_from_roles(coa_id, coa_is_money, counter_coa_id, counter_is_money, SIDE_COA)


def read_entry_form(stored, coa_id, counter_coa_id, money_ids) -> str:
    """Die Form eines Satzes zum Lesen: die gespeicherte (``Bookings.EntryForm``) – nur für
    eine Zeile ohne Form (per Roh-SQL angelegt) die Einordnung mit der Kontenliste von heute,
    also die Lesart vor der Form. Ein Spiegel ohne Form gilt als ``mirror_legacy``."""
    if stored in ENTRY_FORMS:
        return stored
    kind = classify_entry(coa_id, counter_coa_id, money_ids)
    if kind == ENTRY_MIRROR:
        return FORM_MIRROR_LEGACY
    if kind == ENTRY_EMPTY:
        return FORM_PURPOSE_FIRST
    return kind


#: Welche Seite eines Satzes ein Geldkonto ist, je Form: (Konto, Gegenkonto).
_MONEY_SIDES = {
    FORM_PURPOSE_FIRST: (False, True),
    FORM_LIQUID_FIRST: (True, False),
    FORM_TRANSFER: (False, False),
    FORM_MIRROR_COA: (True, True),
    FORM_MIRROR_COUNTER: (True, True),
    FORM_MIRROR_LEGACY: (True, True),
}


def reform(old_form, old_coa_id, old_counter_coa_id, coa_id, counter_coa_id, money_ids, amount):
    """Neue Form samt Betrag, wenn sich Konto oder Gegenkonto eines bestehenden Satzes ändern.

    Eine Seite, die gleich bleibt, behält ihre Rolle (Geldkonto oder nicht) aus der alten Form –
    der Satz wurde so geschrieben, und die Kontenliste von heute ändert daran nichts. Nur eine
    Seite, die sich ändert, wird mit den Zahlungskonten von jetzt (``money_ids``) eingeordnet.

    Der Betrag bezieht sich weiter auf dieselbe Seite wie vorher (``soll_side`` der alten
    Form). Passt die neue Kontierung nur mit der anderen Seite – etwa wenn aus „Aufwand an
    Bank“ eine Umbuchung wird –, kippt die Form und der Betrag wird negiert: im Soll/Haben
    ist das dieselbe Buchung. Ein Spiegel bleibt, was er war (auch ``mirror_legacy``), solange
    beide Seiten Geldkonten sind – die Änderung der Konten sagt nichts über die Lesart seines
    Betrags. Returns ``(form, amount)``; ``amount`` darf ``None`` sein."""
    side = soll_side(old_form)
    old_coa_money, old_counter_money = _MONEY_SIDES[old_form]
    coa_money = old_coa_money if coa_id == old_coa_id else coa_id in money_ids
    counter_money = (old_counter_money if counter_coa_id == old_counter_coa_id
                     else counter_coa_id in money_ids)
    form = _form_from_roles(coa_id, coa_money, counter_coa_id, counter_money, side)
    if old_form in MIRROR_FORMS and form in MIRROR_FORMS:
        form = old_form
    if soll_side(form) != side and amount is not None:
        amount = -amount
    return form, amount


def is_mirror_row(stored, coa_id, counter_coa_id, bank_coa_ids) -> bool:
    """Rein liquider Spiegel (Doppik), der in Listen und im Buchungssatz-Editor ausgeblendet wird?

    Wie in 2.0.x: beide Seiten sind eigene Bank-/Kassenkonten (``Database.get_bank_coa_ids``).
    Ein Spiegel mit dem Verrechnungskonto (1460) oder einem anderen Konto, das kein eigenes ist,
    bleibt sichtbar und bearbeitbar – auch wenn seine Form bekannt ist. Eine Zahlung oder
    Umbuchung (``purpose_first``, ``liquid_first``, ``transfer``) ist nie ein Spiegel: Ihre
    Form steht fest, auch wenn eines ihrer Konten später ein eigenes Bankkonto wird."""
    if stored in ENTRY_FORMS and stored not in MIRROR_FORMS:
        return False
    return coa_id in bank_coa_ids and counter_coa_id in bank_coa_ids


def purpose_in_counter(stored, coa_id, counter_coa_id, bank_coa_ids) -> bool:
    """Steht das Zweckkonto eines Satzes auf der Gegenseite („Geldkonto vorne“)? Danach richten
    sich der Buchungssatz-Editor (das Feld „SKR-Konto“ zeigt das Zweckkonto), die Maske und der
    Kontovorschlag; speichert die Maske, bleibt das Geldkonto vorne, wie es war.

    ``liquid_first``: ja (sofern ein Konto da ist). ``purpose_first`` und ``transfer``: nein.
    Alles andere – ein Spiegel (auch einer mit bekannter Lesart) und eine Zeile ohne Form –
    liest sich wie in 2.0.x: Das Konto ist ein eigenes Bankkonto, das Gegenkonto nicht (eigene
    Bank-/Kassenkonten: ``Database.get_bank_coa_ids``). So bleibt „Bank an 1460“ im Editor ein Satz
    mit dem Zweckkonto 1460 und wird beim Speichern nicht zu „Bank an Bank“."""
    if stored == FORM_LIQUID_FIRST:
        return coa_id is not None
    if stored in (FORM_PURPOSE_FIRST, FORM_TRANSFER):
        return False
    return coa_id in bank_coa_ids and counter_coa_id not in bank_coa_ids


def bank_effect(stored, amount, coa_id, counter_coa_id, parent_coa_ids, bank_coa_ids):
    """Wirkung eines Buchungssatzes auf das Konto seiner Bankbewegung (Split-Summe).

    ``stored`` ist die gespeicherte Form (oder ``None``), ``parent_coa_ids`` die COA-IDs des
    Kontos der Bankbewegung (``Accounts.SKRAccount``; leer/``None``, wenn es keine
    SKR-Nummer hat). Ein Spiegel mit bekannter Lesart (``mirror_coa``/``mirror_counter``)
    wirkt nach seiner Position: Steht das Konto der Bank auf der Seite, auf die sich der
    Betrag bezieht, ist es der Betrag, auf der anderen Seite sein Gegenteil, berührt er das
    Konto nicht, ist es 0. Alles andere – auch ``mirror_legacy`` – zählt wie bisher
    (``is_bank_effective``: der Betrag, wenn der Satz ein eigenes Bankkonto berührt oder nicht
    ganz kontiert ist, sonst 0)."""
    amount = amount or 0
    if stored in (FORM_MIRROR_COA, FORM_MIRROR_COUNTER) and parent_coa_ids:
        if stored == FORM_MIRROR_COA:
            reference, other = coa_id, counter_coa_id
        else:
            reference, other = counter_coa_id, coa_id
        if reference in parent_coa_ids:
            return amount
        if other in parent_coa_ids:
            return -amount
        return 0
    return amount if is_bank_effective(coa_id, counter_coa_id, bank_coa_ids) else 0


# SKR04-Konten 1000–1099 sind Vorräte. Bis 2.0.x galten sie als Kasse (der SKR03-Kassenbereich,
# den die WISO-Importe fest als liquide behandelt haben); die Migration auf 2.1.0 schreibt
# fest, wie der Bestand damals gelesen wurde. Seither sind sie kein Geldkonto mehr – Importe
# melden Buchungen darauf (``stock_range_accounts``).
LEGACY_CASH_RANGE = (1000, 1099)
LEGACY_CASH_NUMBERS = range(LEGACY_CASH_RANGE[0], LEGACY_CASH_RANGE[1] + 1)

# SKR04: Klasse 0 = Anlagevermögen, Klassen 4–7 = Erträge und Aufwendungen. Feste
# Nummern wie in den übrigen Auswertungen, bis die Funktionskonten-Aufgabe
# (docs/aufgaben.md) Rollen statt Nummern einführt.
ASSET_ACCOUNT_RANGE = (1, 999)
PL_ACCOUNT_RANGE = (4000, 7999)
# Klasse 4 = Erlöse. Die Wartekonten (4340/4345/4405) liegen darin, sind aber keine Erlöskonten:
# ``db.reporting.is_income_account`` nimmt sie heraus.
REVENUE_ACCOUNT_RANGE = (4000, 4999)


def is_asset_transfer(coa_id, counter_coa_id, liquid_coa_ids, asset_coa_ids, pl_coa_ids,
                      form=None) -> bool:
    """Ist das eine Umbuchung zwischen Anlagekonto und Erfolgskonto?

    Das ist die Form der AfA („Aufwand an Anlagekonto“) und des Restbuchwerts beim
    Abgang. Der Satz bewegt kein Geld, gehört aber in die EÜR: Wahr, wenn der Satz eine
    Umbuchung ist und eine Seite in ``asset_coa_ids``, die andere in ``pl_coa_ids`` liegt.
    Ein Anlagenkauf (Anlagekonto gegen Geldkonto) oder eine Verbindlichkeit auf der
    Gegenseite ist keine.

    ``form`` ist die gelesene Form des Satzes (``read_entry_form``); ohne sie ordnet
    ``classify_entry`` mit ``liquid_coa_ids`` ein.
    """
    if form is None:
        form = classify_entry(coa_id, counter_coa_id, liquid_coa_ids)
    if form != ENTRY_TRANSFER:
        return False
    return ((coa_id in asset_coa_ids and counter_coa_id in pl_coa_ids)
            or (coa_id in pl_coa_ids and counter_coa_id in asset_coa_ids))


def receivable_condition(debtor_coa_ids, payment_coa_ids, alias=''):
    """SQL-Bedingung „Satz ist eine Forderungsbuchung“ samt Parametern – die EINE Definition.

    Eine Forderung (Debitoren an Wartekonto, entsteht beim Versenden der Rechnung) hat auf keiner
    Seite ein Zahlungskonto (``get_payment_coa_ids``) und das Debitorenkonto 10000 im SOLL:

    * ``COA_ID`` ist 10000 und der Betrag positiv, oder
    * ``CounterCOA_ID`` ist 10000 und der Betrag negativ (Vorzeichenregel der Umbuchungen), oder
    * ``COA_ID`` ist 10000 und der Betrag negativ **und** die Beleg-Nr. ist die Nummer einer
      Rechnung mit negativem Brutto: die Gutschrift. Die App bucht ihre Forderung als „10000 an
      Wartekonto“ mit negativem Betrag. Ohne eine solche Rechnung ist „10000 an X, negativ“ keine
      Forderung (10000 stünde dort im Haben).

    Sie ist ein eigenständiger Satz und hängt nie an einer Bankbewegung: Der Auto-Abgleich
    verknüpft sie nicht, ``delete_transaction`` löscht sie nicht mit.

    Keine Forderung ist, was 10000 im HABEN führt: die Zahlung „Bank an 10000“, aber auch
    Erlösminderung, Skonto und Forderungsverlust („… an 10000“, Betrag positiv) – sie gehören zur
    Bankbewegung bzw. zur Zahlung. Die WISO-Zahlungssätze „1210 an 10000“ und ihr Storno haben
    außerdem ein Zahlungskonto auf einer Seite und bleiben verknüpfbar.

    ``alias``: Tabellenalias der Bookings-Zeile (ohne Alias gilt der Tabellenname).
    Returns ``(sql, params)``.
    """
    if not debtor_coa_ids:
        return '0', []
    p = f'{alias}.' if alias else ''
    row = alias or 'Bookings'
    debtor = ','.join('?' * len(debtor_coa_ids))
    payment = ','.join('?' * len(payment_coa_ids))
    # COALESCE auf jeder Seite: Die Bedingung ist nie NULL, ein Satz ohne Konten oder ohne Betrag
    # (NULL) ist schlicht keine Forderung – sonst ergäbe ``NOT (…)`` für ihn NULL und er fiele heraus.
    # Die Rechnung wird über den Tabellennamen bzw. Alias der Zeile angesprochen, damit ``DocumentNumber``
    # nicht mit einer Spalte der Rechnungstabelle verwechselt werden kann.
    sql = (f'(('
           f'(COALESCE({p}COA_ID, -1) IN ({debtor}) AND COALESCE({p}Amount, 0) > 0)'
           f' OR (COALESCE({p}CounterCOA_ID, -1) IN ({debtor}) AND COALESCE({p}Amount, 0) < 0)'
           f' OR (COALESCE({p}COA_ID, -1) IN ({debtor}) AND COALESCE({p}Amount, 0) < 0'
           f' AND EXISTS (SELECT 1 FROM Invoices credit_note'
           f' WHERE credit_note.InvoiceNumber = {row}.DocumentNumber AND credit_note.SumGross < 0)))'
           f' AND COALESCE({p}COA_ID, -1) NOT IN ({payment})'
           f' AND COALESCE({p}CounterCOA_ID, -1) NOT IN ({payment}))')
    params = list(debtor_coa_ids) * 3 + list(payment_coa_ids) * 2
    return sql, params


class MatchingMixin:
    @staticmethod
    def _account_coa_sets(cursor):
        """{Accounts.ID: COA-IDs seines SKR-Kontos} – Konten ohne SKR-Nummer fehlen. Daran
        erkennt ``bank_effect`` die Seite der Bankbewegung in einem Spiegel."""
        cursor.execute('''
            SELECT a.ID, c.ID FROM Accounts a
            JOIN ChartOfAccounts c ON c.AccountNumber = a.SKRAccount
            WHERE a.SKRAccount IS NOT NULL
        ''')
        sets = {}
        for account_id, coa_id in cursor.fetchall():
            sets.setdefault(account_id, set()).add(coa_id)
        return sets

    def get_account_coa_sets(self):
        """Öffentliche Variante von ``_account_coa_sets`` (ohne Cursor-Parameter)."""
        conn = self._get_connection()
        try:
            return self._account_coa_sets(conn.cursor())
        finally:
            conn.close()

    def _receivable_sql(self, cursor, alias=''):
        """``receivable_condition`` für diese Datenbank, gelesen über ``cursor`` (so läuft sie
        auch innerhalb einer laufenden Transaktion). Returns ``(sql, params)``."""
        cursor.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?',
                       (self.DEBTOR_ACCOUNT_NUMBER,))
        debtor_coa_ids = [row[0] for row in cursor.fetchall()]
        return receivable_condition(debtor_coa_ids, sorted(self._get_payment_coa_ids(cursor)),
                                    alias)

    @staticmethod
    def _debtor_settled_minor(cursor, number, debtor_coa_ids):
        """Summe der Sätze „… an Debitoren“ (Zahlungen, Skonti, Gutschriften) mit dieser Beleg-Nr.,
        die an einer Bankbewegung hängen, in Minor Units. Ob sie die Forderung ausgleicht, sagt
        ``money.is_covered`` – Stufe 7 des Auto-Abgleichs und das Entfernen einer Zuordnung
        fragen beide danach."""
        marks = ','.join('?' * len(debtor_coa_ids))
        cursor.execute(
            f"SELECT COALESCE(SUM(Amount), 0) FROM Bookings WHERE BookingType = 'entry' "
            f"AND ParentBooking_ID IS NOT NULL AND DocumentNumber = ? "
            f"AND CounterCOA_ID IN ({marks})", [number, *debtor_coa_ids])
        return cursor.fetchone()[0]

    def get_linked_entry_for_bank(self, bank_booking_id: int):
        """Hole die wichtigsten Felder des ersten verknüpften Entry-Bookings.

        Für Bank-Buchungen, die über ParentBooking_ID mit Entry-Buchungen
        verknüpft sind.  Doppik-Einträge (COA = Bankkonto) werden übersprungen.

        Returns:
            tuple(COA_ID, CounterCOA_ID, TaxRate, TaxAmount, DocumentNumber,
                  Contact_ID, Category_ID) oder None.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        cursor.execute('''
            SELECT COA_ID, CounterCOA_ID, TaxRate, TaxAmount,
                   DocumentNumber, Contact_ID, Category_ID, EntryForm
            FROM Bookings
            WHERE ParentBooking_ID = ?
            ORDER BY ID
        ''', (bank_booking_id,))
        for row in cursor.fetchall():
            if not is_mirror_row(row[7], row[0], row[1], bank_coa_ids):
                conn.close()
                return self._euro_row(row[:7], 3)  # TaxAmount an Index 3 -> Euro-Decimal
        conn.close()
        return None
    def get_child_bookings_for_bank(self, bank_booking_id: int):
        """ALLE Kindbuchungen einer Bank-Buchung (auch Doppik-Spiegel).

        Grundlage dafür, den Buchungssatz beim Bearbeiten der Bank-Zeile
        mitzuziehen. Bewusst ungefiltert: Die Existenzprüfung "hat diese
        Bank-Buchung schon einen Buchungssatz?" muss alle Zeilen sehen,
        sonst entstünde bei jedem Speichern ein weiteres Kind.

        AutoMirror=1 kennzeichnet die von uBuchhaltung selbst als Spiegel der
        Bank-Bewegung angelegten Buchungssätze – nur sie dürfen mitgezogen
        werden (Herkunft ist aus den Werten allein nicht rekonstruierbar).

        Returns: [(ID, COA_ID, CounterCOA_ID, AutoMirror,
                   Amount€, TaxRate, TaxAmount€, DocumentNumber, Text, EntryForm)]
        Die Indizes 0–3 sind stabil; die weiteren Felder speisen den
        Buchungssatz-Editor der Bank-Maske.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ID, COA_ID, CounterCOA_ID, COALESCE(AutoMirror, 0),
                   Amount, TaxRate, TaxAmount, DocumentNumber, Text, EntryForm
            FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID
        ''', (bank_booking_id,))
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 4, 6) for r in rows]  # Amount, TaxAmount

    def get_unlinked_entry_bookings(self, around_date=None, limit=50):
        """Noch keiner Bankbewegung zugeordnete Buchungssätze.

        Kandidaten für die manuelle Zuordnung im Buchungssatz-Editor: Die
        Verbindung entsteht dort über die ID (ParentBooking_ID), unabhängig
        von Belegnummern – das deckt auch Sammelüberweisungen mehrerer
        Rechnungen mit je eigener Beleg-Nr. ab.

        around_date sortiert die zeitlich nächstliegenden nach vorn.

        Forderungsbuchungen (``receivable_condition``) sind keine Kandidaten: Sie gehören
        zur Rechnung, nicht zu einer Bankbewegung.

        Returns: [(ID, DateBooking, Amount€, DocumentNumber, Text,
                   RecipientClient, COA_ID, CounterCOA_ID, EntryForm, TaxRate, TaxAmount€)]
        Steuersatz und -betrag stehen für die Zeile des Editors bereit: Beim Zuordnen bleibt die
        Steuer der Buchung, sonst fehlte sie danach in EÜR und DATEV-Export.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        receivable_sql, receivable_params = self._receivable_sql(cursor)
        order = ("ABS(JULIANDAY(DateBooking) - JULIANDAY(?)), DateBooking DESC"
                 if around_date else "DateBooking DESC")
        params = receivable_params + ([around_date] if around_date else []) + [limit]
        cursor.execute(f'''
            SELECT ID, DateBooking, Amount, DocumentNumber, Text,
                   RecipientClient, COA_ID, CounterCOA_ID, EntryForm, TaxRate, TaxAmount
            FROM Bookings
            WHERE BookingType = 'entry' AND ParentBooking_ID IS NULL
              AND Account_ID IS NULL
              AND (Status IS NULL OR Status != 'resolved')
              AND NOT {receivable_sql}
            ORDER BY {order}
            LIMIT ?
        ''', params)
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 2, 10) for r in rows]   # Amount, TaxAmount

    def find_unbalanced_splits(self, date_from: str, date_to: str):
        """Bankbewegungen im Zeitraum, deren Buchungssätze nicht aufgehen.

        Geprüft werden nur Bewegungen mit mindestens einem Buchungssatz – eine
        ganz unkontierte Bankbewegung ist kein Split, sondern schlicht offen.
        Umbuchungen (4405→4400, Privatanteil 6805→2100) zählen nicht mit: sie
        gehören zum Beleg, bewegen aber kein Geld.

        Grundlage der DATEV-Prüfung: ein solcher Stapel wäre in sich unstimmig.

        Betroffen ist eine Bankbewegung, wenn sie selbst ODER einer ihrer
        Buchungssätze im Zeitraum liegt – zugeordnete Bestandsbuchungen behalten
        ihr eigenes Datum, und exportiert werden die Buchungssätze. Gerechnet
        wird dann über ALLE ihre Sätze, sonst ergäbe die Summe nie den
        Bankbetrag.

        Returns: [(BankID, DateBooking, Amount€, Rest€, Text)] nach Datum.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        account_coas = self._account_coa_sets(cursor)
        cursor.execute('''
            SELECT p.ID, p.DateBooking, p.Amount, p.Text, p.Account_ID,
                   e.Amount, e.COA_ID, e.CounterCOA_ID, e.EntryForm
            FROM Bookings p
            JOIN Bookings e ON e.ParentBooking_ID = p.ID
            WHERE p.BookingType = 'bank'
              AND p.ID IN (
                  SELECT b.ID FROM Bookings b
                   WHERE b.BookingType = 'bank'
                     AND b.DateBooking BETWEEN ? AND ?
                  UNION
                  SELECT c.ParentBooking_ID FROM Bookings c
                   WHERE c.ParentBooking_ID IS NOT NULL
                     AND c.DateBooking BETWEEN ? AND ?
              )
            ORDER BY p.DateBooking, p.ID
        ''', (date_from, date_to, date_from, date_to))
        rows = cursor.fetchall()
        conn.close()

        parents = {}
        sums = {}
        for bank_id, date, amount, text, account_id, child_amount, coa, counter, form in rows:
            parents[bank_id] = (date, amount, text)
            sums.setdefault(bank_id, 0)
            sums[bank_id] += bank_effect(form, child_amount, coa, counter,
                                         account_coas.get(account_id), bank_coa_ids)

        result = []
        for bank_id, (date, amount, text) in parents.items():
            rest = (amount or 0) - sums[bank_id]
            if rest:
                result.append((bank_id, date, from_minor(amount or 0),
                               from_minor(rest), text or ''))
        result.sort(key=lambda r: (r[1], r[0]))
        return result

    def find_unlinked_booking_by_date_amount(self, date: str, amount: float):
        """Suche nach einer unverknüpften Buchung/Beleg-Gruppe anhand Datum + Betrag.

        Stufe 1 – Einzelbuchung: exakter Treffer auf DateBooking + Amount.
        Stufe 2 – Beleg-Gruppe:  mehrere Buchungen desselben Belegs am selben
                                  Tag, deren Summe dem Bankbetrag entspricht und
                                  die alle noch unverknüpft sind (Account_ID NULL).

        Die Gruppierung ergibt sich aus der Beleg-Nr. selbst – genau daraus
        bildete früher auch der WISO-Import seine BookingGroups. Sonderfall
        Mehrfachreferenz ('25F009, 25F073'): die Buchungen teilen sich diese
        kombinierte Beleg-Nr. und werden dadurch weiterhin zusammen gefunden.

        Returns:
            ('single', booking_id)   – eindeutige Einzelbuchung
            ('docnr',  (nr, datum))  – eindeutige Beleg-Gruppe
            None                     – kein eindeutiger Treffer
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        amount_minor = to_minor(amount or 0)

        # Stufe 1: einzelne, noch nicht verknüpfte Buchung (kein Split).
        # Buchungen, die zu einem mehrzeiligen Beleg gehören, bleiben Stufe 2
        # vorbehalten – sonst würde eine Teilzeile fälschlich allein verknüpft.
        cursor.execute('''
            SELECT ID FROM Bookings b
            WHERE DateBooking = ? AND Amount = ? AND Account_ID IS NULL
              AND (DocumentNumber IS NULL OR DocumentNumber = '' OR
                   (SELECT COUNT(*) FROM Bookings x
                    WHERE x.DocumentNumber = b.DocumentNumber
                      AND x.DateBooking = b.DateBooking) = 1)
        ''', (date, amount_minor))
        rows = cursor.fetchall()
        if len(rows) == 1:
            conn.close()
            return ('single', rows[0][0])

        # Stufe 2: Beleg-Gruppe, deren Gesamtbetrag passt und deren Mitglieder
        # alle noch unverknüpft sind.
        cursor.execute('''
            SELECT b.DocumentNumber,
                   SUM(b.Amount)                                        AS total,
                   COUNT(*)                                             AS cnt,
                   SUM(CASE WHEN b.Account_ID IS NULL THEN 1 ELSE 0 END) AS unlinked
            FROM Bookings b
            WHERE b.DocumentNumber IS NOT NULL AND b.DocumentNumber != ''
              AND b.DateBooking = ?
            GROUP BY b.DocumentNumber
            HAVING cnt > 1
               AND cnt = unlinked
               AND total = ?
        ''', (date, amount_minor))
        rows = cursor.fetchall()
        conn.close()
        if len(rows) == 1:
            return ('docnr', (rows[0][0], date))

        return None   # 0 oder mehrere Treffer → nicht verlässlich verknüpfbar
    def link_bank_to_entries(self) -> dict:
        """Verknüpft Bank-Buchungen (BookingType='bank') mit passenden
        Entry-Buchungen (BookingType='entry') über ParentBooking_ID.

        Matching-Strategien (in dieser Reihenfolge):

        Stufe 1 – Datum + normalisierter Empfänger + ABS(Betrag):
            Leerzeichen in RecipientClient werden komprimiert (REPLACE+LOWER).
            Doppik-Entries (COA 1460-1940) werden rausgefiltert.
            Mehrfach-Treffer (z.B. Fraenk) werden 1:1 zugeordnet.

        Stufe 2 – Datum + ABS(Betrag):
            Ohne Empfänger-Bedingung, Doppik-Filter aktiv.
            Eindeutiger Treffer wird verknüpft.

        Stufe 3 – Split-Gruppe: Datum + ABS(SUM der Gruppenmitglieder):
            Für Bank-Buchungen die einer BookingGroup (Split) entsprechen.

        Stufe 3b – Rechnungs-Split: Datum + ABS(SUM/Anzahl):
            Für Ausgangsrechnungs-Zahlungen die als Doppelbuchung
            erfasst werden (z.B. Bank 1810 + Erlöse 4405).  Die Gruppe
            wird nur verknüpft, wenn mindestens ein Mitglied ein
            Bank-COA hat.

        Stufe 3c – Privatanteil-Split: Datum + SUM ohne Privatentnahme-Offset:
            Für Split-Gruppen deren Summe durch eine positive
            Privatentnahme-Gegenbuchung (COA 2100–2199) verfälscht wird.
            Die Gruppensumme abzüglich des positiven Privatanteils
            muss dem Bankbetrag entsprechen.

        Stufe 3d – Sammelzahlung: Datum + mehrere Rechnungsnummern im Text:
            Für Bank-Buchungen mit komma-getrennten Rechnungsnummern im
            Text (z.B. "2025011,2025010").  Die Entries mehrerer
            BookingGroups werden zusammengefasst.  Summe der Bank-COA-
            Entries über alle Gruppen muss dem Bankbetrag entsprechen.

        Stufe 4 – DocumentNumber als Tiebreaker:
            Falls Stufe 2 mehrere Treffer liefert, wird versucht
            ob genau einer die passende Belegnummer enthält.

        Stufe 5 – Text-Token-Matching:
            Letzte Chance: Extrahiert lange Ziffernfolgen (>= 8 Stellen)
            aus dem Banktext und sucht denselben Token im Entry-Text.
            Deckt Fälle wie fraenk-Rechnungsnummern oder andere
            Transaktions-IDs im Verwendungszweck ab.

        Stufe 6 – Text-Similarity-Matching (fehlende BelegNr):
            Wenn mehrere Entries auf Datum+Betrag matchen, aber weder
            DocumentNumber- noch Token-Tiebreak greifen (z.B.
            Privatentnahmen ohne BelegNr), wird der Entry mit dem
            ähnlichsten Text gewählt (SequenceMatcher, normalisiert).
            Nur wenn der Beste eindeutig besser ist als der Zweitbeste
            und die Ähnlichkeit > 50 % beträgt.

        Stufe 7 – Debitoren-Auflösung (nach der Hauptschleife):
            Debitoren-Entries (COA 10000) bei Rechnungserstellung haben
            ein früheres Datum als die spätere Zahlung und können daher
            nie per Datum matchen.  Erreicht die Summe der bereits
            verknüpften Sätze „… an Debitoren“ (gleiche DocumentNumber,
            CounterCOA=Debitoren) den Betrag des Debitoren-Entry, wird er
            als Status='resolved' markiert – nicht schon nach der ersten
            Teilzahlung (``money.is_covered``).

        Nach dem Linken wird der Text der Bank-Buchung durch den Text der
        Entry-Buchung ersetzt (WISO-kuratierter Text hat Vorrang).

        Forderungsbuchungen (``receivable_condition``) werden nie verknüpft – weder einzeln
        noch als Teil einer Beleg-Gruppe: Sie gehören zur Rechnung, nicht zur Bankbewegung.
        Die Stufe 0 (Quellklammer) bleibt davon unberührt.

        Returns:
            dict mit { 'linked': int, 'skipped': int, 'repaired': int,
                        'resolved': int, 'errors': list[str] }
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # ── Schritt 0: Altdaten-Reparatur ─────────────────────────────────
        cursor.execute('''
            UPDATE Bookings SET BookingType = 'bank', EntryForm = NULL
            WHERE (BookingType IS NULL OR BookingType = 'entry')
              AND Account_ID IS NOT NULL
              AND COA_ID IS NULL
              AND CounterCOA_ID IS NULL
        ''')
        repaired = cursor.rowcount
        if repaired:
            conn.commit()

        # Doppik-COA-IDs — nur echte Bankkonten (aus Accounts-Tabelle)
        bank_coa_ids = self._get_bank_coa_ids(cursor)

        # Privatentnahmen-COA-IDs (SKR04 Konten 2100-2199)
        cursor.execute('''
            SELECT ID FROM ChartOfAccounts
            WHERE AccountNumber >= 2100 AND AccountNumber < 2200
        ''')
        private_coa_ids = {r[0] for r in cursor.fetchall()}

        # ── Schritt 1: Alle unverknüpften Bank-Buchungen laden ───────────
        cursor.execute('''
            SELECT b.ID, b.DateBooking, b.Amount, b.Account_ID,
                   b.DocumentNumber, b.ForeignBankAccount,
                   b.RecipientClient, b.Text
            FROM Bookings b
            WHERE b.BookingType = 'bank'
              AND b.ID NOT IN (
                  SELECT ParentBooking_ID FROM Bookings
                  WHERE ParentBooking_ID IS NOT NULL
              )
            ORDER BY b.DateBooking, b.Amount
        ''')
        bank_bookings = cursor.fetchall()

        # Forderungsbuchungen: nie Kind einer Bankbewegung (IDs einmal zu Beginn laden)
        receivable_sql, receivable_params = self._receivable_sql(cursor)
        cursor.execute(f"SELECT ID FROM Bookings WHERE BookingType = 'entry' AND {receivable_sql}",
                       receivable_params)
        receivable_ids = {row[0] for row in cursor.fetchall()}

        linked = 0
        skipped = 0
        errors = []
        already_linked_entry_ids = set()  # Für 1:1 Multi-Match (Fraenk)
        offen_geblieben = []              # für den Nachlauf der Gruppensumme

        def _norm(s):
            """Empfänger normalisieren: Leerzeichen komprimieren + lowercase."""
            return ' '.join((s or '').split()).lower()

        def _filter_doppik(entries):
            """Rein liquide Spiegelbuchungen rausfiltern (COA und Gegenkonto)."""
            filtered = []
            for e in entries:
                coa_id = e[2]
                counter_coa_id = e[3]
                if coa_id in bank_coa_ids and counter_coa_id in bank_coa_ids:
                    continue
                filtered.append(e)
            return filtered

        def _filter_already(entries):
            """Bereits in diesem Durchlauf verknüpfte Entries und Forderungen rausfiltern."""
            return [e for e in entries
                    if e[0] not in already_linked_entry_ids and e[0] not in receivable_ids]

        import re
        from difflib import SequenceMatcher
        _TOKEN_DIGITS = re.compile(r'\d{6,}')
        _TOKEN_ALNUM = re.compile(r'[A-Za-z0-9]{6,}')

        def _extract_tokens(text):
            """Kennungen aus einem Text: Ziffern- **und** Buchstabenfolgen.

            Dient als eindeutige Kennung (Rechnungs-/Transaktionsnummern),
            z.B. '1040749116593' (fraenk EREF) oder '870136' (SHBB RNR).

            Rein numerisch reicht nicht: zwei Amazon-Lastschriften desselben
            Tages über denselben Betrag tragen dieselbe Bestellnummer und
            unterscheiden sich nur in der Transaktionskennung
            ('2BSTZZGVGU16KNKI'). Deshalb zusätzlich alphanumerische Folgen –
            aber nur solche mit mindestens einer Ziffer, sonst würde jedes
            längere Wort ('AMZNBusiness', 'Lohnzahlung') zum Token.

            Beide Mengen werden vereinigt: '123456' aus 'Rechnung123456' muss
            weiter auf ein blankes '123456' im Banktext passen.
            """
            text = text or ''
            tokens = set(_TOKEN_DIGITS.findall(text))
            tokens |= {t for t in _TOKEN_ALNUM.findall(text)
                       if any(c.isdigit() for c in t)}
            return tokens

        def _token_tiebreak(bank_text, entries, text_idx=1):
            """Unter mehreren Entries denjenigen finden, der einen
            gemeinsamen numerischen Token mit dem Banktext teilt.

            Wenn mehrere Entries überlappende Tokens haben (z.B. weil
            gemeinsame CRED-/IBAN-Nummern in allen PayPal-Texten stehen),
            wird der Entry mit den *meisten* gemeinsamen Tokens genommen,
            sofern er eindeutig mehr hat als alle anderen.

            Args:
                bank_text: Text der Bank-Buchung
                entries:   Kandidaten-Liste (Tuples)
                text_idx:  Index des Text-Feldes im Tuple. Alle Abfragen hier
                           liefern (ID, Text, COA_ID, …) – der Text steht auf
                           1. Der frühere Default 2 traf COA_ID und ließ die
                           Stufe mit TypeError abstürzen, sobald sie überhaupt
                           mehrere Kandidaten bekam.

            Returns:
                Einzel-Entry-Tuple oder None.
            """
            bank_tokens = _extract_tokens(bank_text)
            if not bank_tokens:
                return None
            matches = [e for e in entries
                       if _extract_tokens(e[text_idx]) & bank_tokens]
            if len(matches) == 1:
                return matches[0]
            if len(matches) >= 2:
                # Score = Anzahl gemeinsamer Tokens; höchster gewinnt
                scored = [(len(_extract_tokens(e[text_idx]) & bank_tokens), e)
                          for e in matches]
                scored.sort(key=lambda x: x[0], reverse=True)
                if scored[0][0] > scored[1][0]:
                    return scored[0][1]
            return None

        def _link_docnr_group(bank_id, docnr, bank_date):
            """Alle Buchungen eines Belegs desselben Tages mit der Bank
            verknüpfen (ersetzt die frühere BookingGroup-Klammer) – außer der
            Forderung zur Rechnung."""
            cursor.execute(f'''
                UPDATE Bookings SET ParentBooking_ID = ?
                WHERE DocumentNumber = ? AND DateBooking = ?
                  AND BookingType = 'entry' AND ParentBooking_ID IS NULL
                  AND NOT {receivable_sql}
            ''', [bank_id, docnr, bank_date] + receivable_params)
            cursor.execute(
                'SELECT ID FROM Bookings WHERE DocumentNumber = ?'
                ' AND DateBooking = ?', (docnr, bank_date))
            for r in cursor.fetchall():
                already_linked_entry_ids.add(r[0])

        def _docnr_ids(docnr, bank_date):
            """IDs aller Buchungen eines Belegs an diesem Tag (ohne Forderungen)."""
            cursor.execute(f'''
                SELECT ID FROM Bookings WHERE DocumentNumber = ?
                  AND DateBooking = ? AND BookingType = 'entry'
                  AND NOT {receivable_sql}
            ''', [docnr, bank_date] + receivable_params)
            return {r[0] for r in cursor.fetchall()}

        # COA des jeweils eigenen Zahlungskontos – nur damit lässt sich unter
        # mehreren gleichbetragigen Gruppen desselben Tages die richtige
        # auswählen (Anlagenverkauf: Zahlung über 1810 vs. Umbuchung über 1460).
        cursor.execute('''
            SELECT a.ID, c.ID FROM Accounts a
            JOIN ChartOfAccounts c ON c.AccountNumber = a.SKRAccount
            WHERE a.SKRAccount IS NOT NULL
        ''')
        own_coa_of_account = dict(cursor.fetchall())
        account_coas = self._account_coa_sets(cursor)

        def _source_group_match(bank_id, bank_date, bank_amount, account_id):
            """Eindeutige SourceGroup zu dieser Bankbewegung finden.

            Bedingungen, alle drei zusammen:

            * kein Mitglied ist schon verknüpft,
            * mindestens ein Mitglied läuft über das SKR-Konto **dieser** Bank,
            * die Summe der bankwirksamen Mitglieder trifft den Bankbetrag.

            Die Vorauswahl über das eigene Konto grenzt **Alternativen** ein –
            sie greift bewusst nicht in die Summenbildung ein, die weiterhin
            über alle Mitglieder der Gruppe läuft.
            """
            own_coa = own_coa_of_account.get(account_id)
            if own_coa is None:
                return None
            cursor.execute('''
                SELECT SourceGroup FROM Bookings
                WHERE BookingType = 'entry' AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND SourceGroup IS NOT NULL AND SourceGroup != ''
                GROUP BY SourceGroup
            ''', (bank_date,))
            hits = []
            for (group,) in cursor.fetchall():
                members = _source_group_members(group)
                if not members:
                    continue
                if {m[0] for m in members} & already_linked_entry_ids:
                    continue
                if not any(own_coa in (m[2], m[3]) for m in members):
                    continue
                total = sum(bank_effect(m[5], m[1], m[2], m[3],
                                        account_coas.get(account_id), bank_coa_ids)
                            for m in members)
                if abs(total - bank_amount) < 50:
                    hits.append(group)
            return hits[0] if len(hits) == 1 else None

        def _source_group_sum_match(bank_date, bank_amount, account_id,
                                    recipient):
            """Mehrere Quellgruppen eines Tages, die zusammen aufgehen.

            WISO zerlegt manche Belege in mehrere Vorgänge mit je eigener
            ACCOUNTINGID: Stromabschlag betrieblich + Privatanteil, eine
            Kartenabrechnung in ihre Einzelposten, eine Überweisung in
            Teilzahlungen auf mehrere Rechnungen. Keine Gruppe trifft dann
            den Bankbetrag, ihre Summe aber genau.

            Zuerst wird die Summe **aller** offenen Gruppen des Tages
            geprüft, die über das SKR-Konto dieser Bank laufen. Geht sie
            nicht auf – typisch bei zwei Bankbewegungen am selben Tag –,
            wird auf die Gruppen eingegrenzt, die den Empfänger im Text
            nennen. Trifft auch das nicht, bleibt die Bewegung offen:
            Teilmengen zu suchen hieße raten.
            """
            own_coa = own_coa_of_account.get(account_id)
            if own_coa is None:
                return None
            cursor.execute('''
                SELECT SourceGroup FROM Bookings
                WHERE BookingType = 'entry' AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND SourceGroup IS NOT NULL AND SourceGroup != ''
                GROUP BY SourceGroup
            ''', (bank_date,))
            kandidaten = []
            for (gruppe,) in cursor.fetchall():
                members = _source_group_members(gruppe)
                if not members or {m[0] for m in members} & already_linked_entry_ids:
                    continue
                if not any(own_coa in (m[2], m[3]) for m in members):
                    continue
                summe = sum(bank_effect(m[5], m[1], m[2], m[3],
                                        account_coas.get(account_id), bank_coa_ids)
                            for m in members)
                kandidaten.append((gruppe, summe, members))
            if len(kandidaten) < 2:
                return None            # Einzelgruppen erledigt Stufe 0

            if abs(sum(k[1] for k in kandidaten) - bank_amount) < 50:
                return [k[0] for k in kandidaten]

            marke = _norm(recipient)
            if marke:
                genannt = [k for k in kandidaten
                           if marke in _norm(' '.join(m[4] or '' for m in k[2]))]
                if len(genannt) >= 2 and \
                        abs(sum(k[1] for k in genannt) - bank_amount) < 50:
                    return [k[0] for k in genannt]
            return None

        def _source_group_members(group):
            cursor.execute('''
                SELECT ID, Amount, COA_ID, CounterCOA_ID, Text, EntryForm FROM Bookings
                WHERE BookingType = 'entry' AND SourceGroup = ?
            ''', (group,))
            return cursor.fetchall()

        def _link_source_group(bank_id, group):
            """Alle Mitglieder einer Quellgruppe an die Bankbewegung hängen."""
            members = _source_group_members(group)
            cursor.execute('''
                UPDATE Bookings SET ParentBooking_ID = ?
                WHERE SourceGroup = ? AND BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
            ''', (bank_id, group))
            for member in members:
                already_linked_entry_ids.add(member[0])
            # Text der Bankbewegung durch den kuratierten WISO-Text ersetzen –
            # genommen wird der des bankwirksamen Mitglieds.
            for member in members:
                if member[4] and is_bank_effective(member[2], member[3],
                                                   bank_coa_ids):
                    cursor.execute('UPDATE Bookings SET Text = ? WHERE ID = ?',
                                   (member[4], bank_id))
                    break

        def _do_link(bank_id, entry_id, entry_text):
            """Einzelne Entry-Buchung mit der Bank-Buchung verknüpfen."""
            cursor.execute('''
                UPDATE Bookings SET ParentBooking_ID = ?
                WHERE ID = ?
            ''', (bank_id, entry_id))
            already_linked_entry_ids.add(entry_id)
            # WISO-Text auf die Bank-Buchung übernehmen (manuell kuratiert)
            if entry_text:
                cursor.execute(
                    'UPDATE Bookings SET Text = ? WHERE ID = ?',
                    (entry_text, bank_id))

        for bank in bank_bookings:
            (bank_id, bank_date, bank_amount, bank_account_id,
             bank_docnr, bank_iban, bank_recipient, bank_text) = bank

            abs_amount = round(abs(bank_amount), 2)
            recip_norm = _norm(bank_recipient)

            # ── Stufe 0: Klammer aus dem Quellsystem ─────────────────────
            # Wo der Import die Zusammengehörigkeit mitgeliefert hat
            # (WISO: ACCOUNTINGID → SourceGroup), muss nichts geraten werden.
            # Deshalb steht diese Stufe vor allen Heuristiken.
            group = _source_group_match(bank_id, bank_date, bank_amount,
                                        bank_account_id)
            if group is not None:
                _link_source_group(bank_id, group)
                linked += 1
                continue

            # ── Stufe 1: Datum + Empfänger (normalisiert) + ABS(Betrag) ──
            if recip_norm:
                cursor.execute('''
                    SELECT ID, Text, COA_ID, CounterCOA_ID, RecipientClient FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NULL
                      AND DateBooking = ?
                      AND ABS(ABS(Amount) - ?) < 50
                ''', (bank_date, abs_amount))
                raw = cursor.fetchall()
                entries = _filter_already(_filter_doppik(
                    [e for e in raw if _norm(e[4]) == recip_norm
                     or _norm(e[1]) != '' and recip_norm in _norm(e[1])]
                ))
                if not entries:
                    # Fallback: direkter DB-Vergleich (REPLACE normalisiert)
                    cursor.execute('''
                        SELECT ID, Text, COA_ID, CounterCOA_ID FROM Bookings
                        WHERE BookingType = 'entry'
                          AND ParentBooking_ID IS NULL
                          AND DateBooking = ?
                          AND ABS(ABS(Amount) - ?) < 50
                          AND LOWER(REPLACE(REPLACE(REPLACE(TRIM(
                              COALESCE(RecipientClient,'')), '  ', ' '), '  ', ' '), '  ', ' '))
                            = ?
                    ''', (bank_date, abs_amount, recip_norm))
                    entries = _filter_already(_filter_doppik(cursor.fetchall()))
                if len(entries) == 1:
                    _do_link(bank_id, entries[0][0], entries[0][1])
                    linked += 1
                    continue
                if len(entries) >= 2:
                    # Mehrere Treffer: Token-Tiebreak (z.B. Fraenk-Nummern)
                    token_hit = _token_tiebreak(bank_text, entries)
                    if token_hit:
                        _do_link(bank_id, token_hit[0], token_hit[1])
                        linked += 1
                        continue
                    # Fallback: ersten verfügbaren nehmen
                    _do_link(bank_id, entries[0][0], entries[0][1])
                    linked += 1
                    continue

            # ── Stufe 2: Datum + ABS(Betrag) ─────────────────────────────
            cursor.execute('''
                                SELECT ID, Text, COA_ID, CounterCOA_ID, DocumentNumber
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND ABS(ABS(Amount) - ?) < 50
            ''', (bank_date, abs_amount))
            entries = _filter_already(_filter_doppik(cursor.fetchall()))
            if len(entries) == 1:
                # Nur diesen Entry linken, NICHT die ganze Gruppe
                _do_link(bank_id, entries[0][0], entries[0][1])
                linked += 1
                continue

            # ── Stufe 4: DocumentNumber als Tiebreaker ───────────────────
            if len(entries) > 1 and bank_docnr:
                doc_match = [e for e in entries
                             if e[4] and (bank_docnr in e[4] or e[4] in bank_docnr)]
                if len(doc_match) == 1:
                    # Nur diesen Entry linken, NICHT die ganze Gruppe
                    _do_link(bank_id, doc_match[0][0], doc_match[0][1])
                    linked += 1
                    continue

            # ── Stufe 3: Split-Gruppe — SUM(Betrag) passt ────────────────
            cursor.execute('''
                SELECT b.DocumentNumber, COUNT(*) AS cnt
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.DocumentNumber IS NOT NULL AND b.DocumentNumber != ''
                  AND b.DateBooking = ?
                GROUP BY b.DocumentNumber
                HAVING cnt > 1
                   AND ABS(ABS(SUM(b.Amount)) - ?) < 50
            ''', (bank_date, abs_amount))
            groups = [g for g in cursor.fetchall()
                      if not (_docnr_ids(g[0], bank_date) & already_linked_entry_ids)]
            if len(groups) == 1:
                _link_docnr_group(bank_id, groups[0][0], bank_date)
                linked += 1
                continue

            # ── Stufe 3b: Rechnungs-Split — Betrag = SUM/Anzahl ─────────
            # Muster: Ausgangsrechnung wird bezahlt → 2 Entries mit
            # gleichem Betrag (COA Bank + COA Erlöse), SUM = 2× Bankbetrag.
            # Erkennung: Ein Gruppenmitglied hat COA = Bankkonto.
            cursor.execute('''
                SELECT b.DocumentNumber, COUNT(*) AS cnt,
                       SUM(b.Amount) AS total
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.DocumentNumber IS NOT NULL AND b.DocumentNumber != ''
                  AND b.DateBooking = ?
                GROUP BY b.DocumentNumber
                HAVING cnt > 1
                   AND ABS(ABS(total * 1.0 / cnt) - ?) < 50
            ''', (bank_date, abs_amount))
            # Filtern: Beleg muss ein Mitglied mit Bank-COA haben
            inv_matches = []
            for g in cursor.fetchall():
                docnr = g[0]
                if _docnr_ids(docnr, bank_date) & already_linked_entry_ids:
                    continue
                cursor.execute(
                    "SELECT COA_ID FROM Bookings WHERE DocumentNumber = ?"
                    " AND DateBooking = ? AND BookingType = 'entry'",
                    (docnr, bank_date))
                coa_ids = {r[0] for r in cursor.fetchall()}
                if coa_ids & bank_coa_ids:  # mindestens ein Bank-COA
                    inv_matches.append(docnr)
            if len(inv_matches) == 1:
                _link_docnr_group(bank_id, inv_matches[0], bank_date)
                linked += 1
                continue

            # ── Stufe 3c: Privatanteil-Split ─────────────────────────────
            # Muster: Split-Gruppe enthält eine positive Gegenbuchung auf
            # ein Privatentnahme-Konto (2100–2199), die den Bankbetrag
            # verfälscht.  Erkennung: Gruppensumme ohne positive
            # Privatentnahme-Einträge ≈ Bankbetrag.
            cursor.execute('''
                SELECT b.DocumentNumber,
                       SUM(b.Amount) AS total,
                       SUM(CASE WHEN b.Amount > 0 AND b.COA_ID IN
                           (SELECT ID FROM ChartOfAccounts
                            WHERE AccountNumber >= 2100 AND AccountNumber < 2200)
                           THEN b.Amount ELSE 0 END) AS private_offset
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.DocumentNumber IS NOT NULL AND b.DocumentNumber != ''
                  AND b.DateBooking = ?
                GROUP BY b.DocumentNumber
                HAVING COUNT(*) > 1
                   AND private_offset > 0
                   AND ABS(ABS(total - private_offset) - ?) < 50
            ''', (bank_date, abs_amount))
            priv_matches = [g[0] for g in cursor.fetchall()
                            if not (_docnr_ids(g[0], bank_date) & already_linked_entry_ids)]
            if len(priv_matches) == 1:
                _link_docnr_group(bank_id, priv_matches[0], bank_date)
                linked += 1
                continue

            # ── Stufe 3d: Sammelzahlung ────────────────────────────────
            # Muster: Bank-Text enthält mehrere komma- oder leerzeichen-
            # getrennte Rechnungsnummern (z.B. "2025011,2025010").
            # Die zugehörigen Entries liegen in verschiedenen
            # BookingGroups.  Summe der Bank-COA-Entries über alle
            # Gruppen muss dem Bankbetrag entsprechen.
            doc_nr_candidates = set(re.findall(r'\b\d{4,}\b',
                                               bank_text or ''))
            if len(doc_nr_candidates) >= 2:
                ph = ','.join('?' * len(doc_nr_candidates))
                cursor.execute(f'''
                    SELECT ID, Text, COA_ID,
                           CounterCOA_ID, DocumentNumber, Amount
                    FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NULL
                      AND DateBooking = ?
                      AND DocumentNumber IN ({ph})
                ''', (bank_date, *doc_nr_candidates))
                sammel_entries = _filter_already(cursor.fetchall())
                doc_nrs_found = {e[4] for e in sammel_entries}
                if len(doc_nrs_found) >= 2 and len(sammel_entries) >= 2:
                    # Zahlungseingänge sind als Doppelbuchung erfasst (COA =
                    # Bankkonto): dann zählt nur deren Bank-Seite. Ausgaben
                    # (COA = Aufwand, Gegenkonto = Bank) haben keine solche
                    # Zeile – dort ist die Summe aller Belege maßgeblich, sonst
                    # bliebe eine Sammelüberweisung an Lieferanten unverknüpft.
                    bank_coa_sum = sum(
                        e[5] for e in sammel_entries
                        if e[2] in bank_coa_ids)
                    if bank_coa_sum == 0:
                        bank_coa_sum = sum(e[5] for e in sammel_entries)
                    if abs(abs(bank_coa_sum) - abs_amount) < 50:
                        for e in sammel_entries:
                            cursor.execute(
                                'UPDATE Bookings SET ParentBooking_ID = ?'
                                ' WHERE ID = ?',
                                (bank_id, e[0]))
                            already_linked_entry_ids.add(e[0])
                        linked += 1
                        continue

            # ── Stufe 5: Text-Token-Matching (letzte Chance) ───────────
            # Suche unter allen ungelinkten Entries desselben Datums+Betrags
            # nach einem gemeinsamen numerischen Token (>= 6 Stellen) im
            # Buchungstext.  Deckt z.B. fraenk-EREF-Nummern, SHBB-RNR-
            # Nummern und andere Fälle mit Transaktions-IDs im Text ab.
            cursor.execute('''
                SELECT ID, Text, COA_ID, CounterCOA_ID
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND ABS(ABS(Amount) - ?) < 50
            ''', (bank_date, abs_amount))
            all_candidates = _filter_already(_filter_doppik(cursor.fetchall()))
            token_hit = _token_tiebreak(bank_text, all_candidates)
            if token_hit:
                _do_link(bank_id, token_hit[0], token_hit[1])
                linked += 1
                continue

            # ── Stufe 6: Text-Similarity (fehlende BelegNr) ────────────
            # Wenn mehrere Entries zum selben Datum+Betrag passen, aber
            # weder DocNr- noch Token-Tiebreak greift (z.B. Privatent-
            # nahmen ohne BelegNr), wird der Entry mit dem ähnlichsten
            # Text gewählt.  Normalisierung: Leerzeichen entfernen,
            # lowercase.  Eindeutig bester Score (> zweitbester und > 0.5)
            # wird verknüpft.
            if len(all_candidates) >= 2:
                def _text_norm(s):
                    return ''.join((s or '').lower().split())
                bank_norm = _text_norm(bank_text)
                if bank_norm:
                    scored = [
                        (SequenceMatcher(None, bank_norm,
                                         _text_norm(e[1])).ratio(), e)
                        for e in all_candidates
                    ]
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if (scored[0][0] > scored[1][0]
                            and scored[0][0] > 0.5):
                        best = scored[0][1]
                        _do_link(bank_id, best[0], best[2])
                        linked += 1
                        continue

            # ── Stufe 6b: mehrere Quellgruppen zusammen ─────────────────
            # Letzte Stufe, damit alle genaueren zuerst greifen. Deckt den
            # Fall ab, dass WISO einen Beleg in mehrere Vorgänge zerlegt
            # hat (Strom betrieblich + Privatanteil, Kartenabrechnung mit
            # vielen Einzelposten, Teilzahlungen auf mehrere Rechnungen) –
            # jede Zeile mit eigener ACCOUNTINGID und ohne Beleg-Nr.
            gruppen = _source_group_sum_match(bank_date, bank_amount,
                                              bank_account_id, bank_recipient)
            if gruppen:
                for gruppe in gruppen:
                    _link_source_group(bank_id, gruppe)
                linked += 1
                continue

            offen_geblieben.append((bank_id, bank_date, bank_amount,
                                    bank_account_id, bank_recipient))
            skipped += 1

        # ── Stufe 6c: Nachlauf für die Gruppensumme ───────────────────────
        # Fallen mehrere Bankbewegungen auf einen Tag, sieht die zuerst
        # geprüfte auch die Gruppen der anderen – die Summe geht dann nicht
        # auf. Sobald die übrigen verknüpft sind, bleibt der Rest übrig und
        # passt. Deshalb so lange wiederholen, wie es noch Fortschritt gibt.
        while offen_geblieben:
            rest = []
            for bank_id, bank_date, bank_amount, account_id, recipient in \
                    offen_geblieben:
                gruppen = _source_group_sum_match(bank_date, bank_amount,
                                                  account_id, recipient)
                if not gruppen:
                    rest.append((bank_id, bank_date, bank_amount, account_id,
                                 recipient))
                    continue
                for gruppe in gruppen:
                    _link_source_group(bank_id, gruppe)
                linked += 1
                skipped -= 1
            if len(rest) == len(offen_geblieben):
                break                       # kein Fortschritt mehr
            offen_geblieben = rest

        # ── Stufe 7: Debitoren-Auflösung ─────────────────────────────────
        # Debitoren-Entries (COA 10000) entstehen bei Rechnungserstellung
        # und haben ein früheres Datum als Bank- und Zahlungsbuchungen.
        # Sie können nie per Datum-Match verknüpft werden.
        # Lösung: Gleichen die bereits verknüpften Zahlung-Entries (CounterCOA =
        # Debitoren) mit gleicher DocumentNumber zusammen den Betrag des
        # Debitoren-Entry aus, setze Status = 'resolved' auf ihm – bei einer
        # Teilzahlung bleibt er offen.
        cursor.execute('''
            SELECT ID FROM ChartOfAccounts
            WHERE AccountNumber = 10000
        ''')
        debitoren_row = cursor.fetchone()
        resolved_count = 0
        if debitoren_row:
            debitoren_coa_id = debitoren_row[0]
            cursor.execute('''
                SELECT ID, DocumentNumber, Amount
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND COA_ID = ?
                  AND (Status IS NULL OR Status != 'resolved')
            ''', (debitoren_coa_id,))
            debitoren_entries = cursor.fetchall()

            for deb_id, doc_nr, deb_amount in debitoren_entries:
                if not doc_nr:
                    continue
                # Summe der verknüpften Zahlung-Entries mit gleicher DocNr
                # und CounterCOA = Debitoren (d.h. Zahlungen auf den Debitor)
                settled = self._debtor_settled_minor(cursor, doc_nr, [debitoren_coa_id])
                if is_covered(deb_amount or 0, settled):
                    cursor.execute(
                        "UPDATE Bookings SET Status = 'resolved'"
                        " WHERE ID = ?",
                        (deb_id,))
                    resolved_count += 1

        conn.commit()
        conn.close()
        return {'linked': linked, 'skipped': skipped, 'repaired': repaired,
                'resolved': resolved_count, 'errors': errors}
