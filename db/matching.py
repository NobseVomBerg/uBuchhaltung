# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: matching."""
import sqlite3
import os
import json
import bisect
import datetime
import re
from decimal import Decimal
from typing import NamedTuple, Optional
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


def ledger_form(stored, coa_id, counter_coa_id, liquid_coa_ids):
    """Form und Soll-Seite einer Buchungszeile, so wie der Export des Buchungsstapels sie liest – EINE Stelle für den Export und die
    Summen- und Saldenliste (``db.susa``), damit beide nie eine verschiedene Lesart haben.

    Eine Zeile mit gespeicherter Form (``Bookings.EntryForm``) oder mit den Zahlungskonten (``liquid_coa_ids``) bekommt ihre Form
    (``read_entry_form``) und daraus ihre Soll-Seite (``soll_side``). Ohne beides – keine Form, keine Geldkonten – gibt es keine Form,
    und allein das Vorzeichen zählt: die Gegenkonto-Seite. Returns ``(Form oder None, 'coa' | 'counter')``."""
    if stored in ENTRY_FORMS or liquid_coa_ids is not None:
        form = read_entry_form(stored, coa_id, counter_coa_id, liquid_coa_ids)
        return form, soll_side(form)
    return None, SIDE_COUNTER


def coa_in_soll(is_inflow: bool, side: str) -> bool:
    """Steht das Feld „Konto“ einer Buchungszeile im Soll? ``is_inflow``: der Betrag ist nicht negativ, ``side``: die Soll-Seite der
    Zeile (``ledger_form``). Bei positivem Betrag steht das Konto im Soll, wenn die Soll-Seite das Feld „Konto“ ist (Geldkonto vorne,
    Umbuchung, Spiegel mit dem Betrag auf dem Konto), sonst im Haben; bei negativem Betrag ist es umgekehrt. Das Soll/Haben-Kennzeichen
    des Exports ist ``S``, wenn das zutrifft."""
    return is_inflow == (side == SIDE_COA)


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


def link_effect(stored, amount, coa_id, counter_coa_id, parent_coa_ids, bank_coa_ids, payment_coa_ids):
    """Wirkung eines noch freien Buchungssatzes auf das Konto einer Bankbewegung – die Frage des
    automatischen Abgleichs (``link_bank_to_entries``): Darf dieser Satz Kind dieser Bewegung werden?

    Die Regel: Ein Satz wird nur verknüpft, wenn seine Wirkung auf das Konto der Bewegung – mit
    Vorzeichen – deren Betrag entspricht, bei einer Gruppe die Summe der Wirkungen. Gerechnet wird mit
    ``bank_effect``, also so wie die Split-Summe des Editors und ``find_unbalanced_splits``: Jede
    automatische Verknüpfung ergibt damit eine ausgeglichene Split-Summe (im Rahmen der Toleranz,
    mit der der Abgleich Beträge vergleicht).

    Returns ``None``, wenn der Satz nie zu dieser Bewegung gehört:

    * ``mirror_legacy`` – die Lesart seines Betrags ist unbekannt (``read_entry_form``), und der
      Abgleich deutet sie nie, sonst würde er einen Satz nach einer Vermutung verknüpfen;
    * ein Spiegel (``mirror_coa``/``mirror_counter``), der das Konto der Bewegung nicht berührt;
    * jede andere Form, bei der eine Seite ein Zahlungskonto ist, das nicht das Konto der Bewegung
      ist (Kasse, anderes eigenes Bankkonto, Verrechnungskonto 1460: ihre Geldseite ist eine andere);
    * eine Umbuchung (``transfer``), die das Konto der Bewegung berührt oder nicht ganz kontiert ist,
      und ein Zahlungssatz, bei dem das Konto der Bewegung nicht auf der Geldseite seiner Form steht
      (``_MONEY_SIDES``): Die Wirkung wäre dort das Gegenteil des gespeicherten Betrags, ``bank_effect``
      zählt ihn aber wie gespeichert – der Satz hinge an der Bewegung in der falschen Richtung;

    sonst ``bank_effect`` – die Wirkung mit Vorzeichen, **0 bei einer Umbuchung ohne Geldseite**
    (Wartekonto an Erlöskonto, Privatanteil): Sie gehört zum Beleg, bewegt aber kein Geld, und
    verknüpft wird sie nur zusammen mit dem Satz, der das Geld bewegt.

    ``parent_coa_ids``: die COA-IDs des Kontos der Bewegung (leer/``None``, wenn es keine SKR-Nummer
    hat), ``bank_coa_ids``: die eigenen Bank-/Kassenkonten, ``payment_coa_ids``: alle Zahlungskonten
    (``Database.get_payment_coa_ids``, dazu zählt das Verrechnungskonto 1460)."""
    form = read_entry_form(stored, coa_id, counter_coa_id, payment_coa_ids)
    if form == FORM_MIRROR_LEGACY:
        return None
    parent = parent_coa_ids or set()
    touches = coa_id in parent or counter_coa_id in parent
    if form in (FORM_MIRROR_COA, FORM_MIRROR_COUNTER):
        return bank_effect(form, amount, coa_id, counter_coa_id, parent, bank_coa_ids) if touches else None
    money = set(bank_coa_ids) | set(payment_coa_ids)
    if any(side is not None and side in money and side not in parent
           for side in (coa_id, counter_coa_id)):
        return None
    if form == FORM_TRANSFER:
        # Eine Umbuchung hat keine Geldseite: Ihr Betrag sagt „Konto im Soll“, nicht die Richtung einer
        # Geldbewegung. Berührt sie trotzdem das Konto der Bewegung (es wurde erst später Bankkonto) oder
        # ist eine Seite leer, ließe sich die Richtung nur vermuten – nie Kandidat.
        return None if touches or coa_id is None or counter_coa_id is None else 0
    # Zahlungssatz: Der Betrag trägt die Richtung auf der Geldseite der Form. Steht das Konto der Bewegung
    # auf der ANDEREN Seite, wäre die Wirkung das Gegenteil des Betrags – ``bank_effect`` zählte ihn trotzdem
    # mit Vorzeichen wie gespeichert, der Satz hinge an der Bewegung in der falschen Richtung.
    coa_is_money = _MONEY_SIDES.get(form, (False, True))[0]
    money_side, other_side = (coa_id, counter_coa_id) if coa_is_money else (counter_coa_id, coa_id)
    if money_side in parent or (money_side is None and other_side not in parent):
        return bank_effect(form, amount, coa_id, counter_coa_id, parent, bank_coa_ids)
    return None


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


def depreciation_effect(stored_form, amount, coa_number, counter_number) -> int:
    """Wirkung eines AfA-Satzes auf sein Anlagekonto, vorzeichenrichtig (Minor Units) – der Betrag, den der
    AfA-Plan der App für das Jahr als gebucht zählt (``Database.depreciation_plan``, WISO-Datenbank-Import).

    Steht ein Anlagekonto (``ASSET_ACCOUNT_RANGE``) im **Haben**, ist es eine Abschreibung: **+|Betrag|**
    („Aufwand an Anlagekonto“). Steht es im **Soll**, ist es eine Korrektur oder Zuschreibung – WISO schreibt
    eine Umkehrung mit vertauschten Konten: **−|Betrag|**. Berührt keine Seite ein Anlagekonto oder beide,
    zählt der Satz +|Betrag| (die AfA alter Form auf einem Bankkonto, Konto ohne Nummer).

    Die Soll-Seite folgt der gespeicherten Form (``soll_side``): Bei positivem Betrag steht die Seite der Form
    im Soll, bei negativem die andere – eine Zeile ohne Form gilt als ``transfer``. Konten und Betrag so, wie die
    Zeile in den Büchern steht; ein Kontentausch mit negiertem Betrag (``reform``) ändert das Ergebnis nicht."""
    amount = amount or 0
    form = stored_form if stored_form in ENTRY_FORMS else FORM_TRANSFER
    soll_is_coa = (soll_side(form) == SIDE_COA) == (amount >= 0)
    low, high = ASSET_ACCOUNT_RANGE
    coa_asset = coa_number is not None and low <= coa_number <= high
    counter_asset = counter_number is not None and low <= counter_number <= high
    if coa_asset == counter_asset:
        return abs(amount)
    return -abs(amount) if coa_asset == soll_is_coa else abs(amount)


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


#: Wie viele Tage zwischen zwei Elementen eines Übertrags zwischen eigenen Konten höchstens liegen, damit sie zu einem
#: Abschnitt gehören (die Wertstellung auf beiden Konten liegt meist 0–3 Tage, über Feiertage bis zu einer Woche
#: auseinander). Gilt für ``settle_transfers``.
TRANSFER_DAY_WINDOW = 7

#: Das Umfeld eines Abschnitts: die Elemente derselben Gruppe, die höchstens so viele Tage vor seinem ersten oder nach seinem
#: letzten Element liegen. Gilt für ``settle_transfers``.
TRANSFER_NEIGHBOUR_DAYS = 14

_ISO_DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def _iso_day(value):
    """Das Datum einer Zeile der Übertrags-Rechnung: nur ``JJJJ-MM-TT`` – ein Kalendertag, Jahr 1900–2200, ohne Zeitanteil und
    ohne andere Schreibweise (dieselbe Regel wie ``server.period.parse_iso_date``; die Datenbankschicht lädt das Server-Package
    nicht, und ``db.invoice_versions`` lädt diese Datei). Alles andere ist ``None``: Die Zeile nimmt nicht teil – im Stapel
    steht sie nach ihrem Text, nicht nach einem Kalendertag, und das Umfeld (± Tage) läuft nie über das Datumsende hinaus."""
    if not isinstance(value, str) or not _ISO_DAY.fullmatch(value):
        return None
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return day if 1900 <= day.year <= 2200 else None


def _whole(value):
    """Ein Betrag in Minor Units: nur eine ganze Zahl (nie ein Text, ein ``bool`` oder eine Kommazahl) – sonst ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ── Überträge zwischen eigenen Konten: zählen statt paaren ───────────────────────────────────────────────────────────
# Wer beide Bankbewegungen eines Übertrags kontiert (Maske, Bank-Editor), bekommt unter jeder einen Spiegel: „1810 an 1800“
# und „1800 an 1810“ stünden im DATEV-Stapel als dieselbe Buchung zweimal. Einzelne Spiegel zu paaren ist nicht
# zuverlässig (zwei gleich hohe Überträge, von denen jeder nur auf einer Seite kontiert ist, sähen wie ein Paar aus). Deshalb
# wird gezählt: Je Richtung und Betrag werden die Bankbewegungen mit Spiegel-Satz („Beine“), die ohne Satz und die
# eigenständigen Spiegel auf einer Zeitachse zu Gruppen zusammengefasst; eine Lücke von mehr als ``TRANSFER_DAY_WINDOW`` Tagen
# trennt sie in Abschnitte. Zusammengelegt wird nur, wo es ohne Paarbildung sicher ist: Im Abschnitt und in seinem Umfeld
# (``TRANSFER_NEIGHBOUR_DAYS``) darf keine Ausgangsbewegung ohne Satz und kein eigenständiger Spiegel liegen – sonst könnte
# ein Eingangs-Satz der einzige seines Übertrags sein, dessen Ausgang nur außerhalb des Abschnitts steht. Eine Bewegung ohne
# Satz gilt nur als gedeckt, wenn die Zahl der überschüssigen Spiegel der Gegenseite – gezählt gegen die Sätze der anderen Seite
# im Umfeld, auch gegen anders kontierte Bankbewegungen – für alle ihrer Seite reicht; ein ganz unkontierter Übertrag zwischen
# kontierten wird so gemeldet. Eine Bankbewegung, die anders kontiert ist (Zahlung, Umbuchung in anderer Form), bildet keine Gruppe
# und legt nichts zusammen, zählt aber als verbraucht. Bleibt ein Eingangs-Satz trotz eines Ausgangs-Spiegels oder einer anders
# kontierten Ausgangsbewegung im Umfeld im Stapel (oder steht ein eigenständiger Spiegel neben einer kontierten Bankbewegung), steht
# sein Übertrag möglicherweise doppelt – das meldet der Export, mit der Bewegung im Umfeld, die das Zusammenlegen sperrt. Die Rechnung
# kennt weder Datenbank noch Formen: ``transfer_legs`` macht aus den Zeilen Beine, ``settle_transfers`` zählt.

class TransferLeg(NamedTuple):
    """Ein Bein eines Übertrags: eine Bankbewegung ``R`` auf einem eigenen Konto.

    Mit Spiegel-Satz (``entry_id`` gesetzt): ``R`` hat ein Kind, das ein Spiegel zwischen ``account`` und ``other`` ist;
    ``amount`` ist der Betrag des Spiegels. Ohne Satz (``entry_id`` ``None``): ``R`` hat gar kein Kind, ``amount`` ist ihr
    Betrag, das Gegenkonto bleibt unbekannt. Die Richtung kommt allein aus dem Vorzeichen von ``R`` (nie aus der Form des
    Satzes – ein ``mirror_legacy`` wird nicht gedeutet): ``R < 0`` ist ein Ausgang von ``account``, ``R > 0`` ein Eingang."""
    bank_id: int
    account: int                                     # Accounts.ID des Kontos von R
    outgoing: bool
    amount: int                                      # |Betrag| in Minor Units, > 0
    day: datetime.date                               # Datum von R
    other: Optional[int] = None                      # das andere Konto des Spiegels
    entry_id: Optional[int] = None                   # der Spiegel-Satz
    entry_day: Optional[datetime.date] = None        # Datum des Spiegel-Satzes


class TransferMirror(NamedTuple):
    """Ein eigenständiger Spiegel (``entry`` ohne Bankbewegung) zwischen zwei teilnehmenden Konten. Mit bekannter Lesart
    (``known``): ``out_account`` verliert ``amount``, ``in_account`` bekommt ihn. Ohne – ``mirror_legacy`` oder gar keine
    Form –: die Konten stehen in beliebiger Reihenfolge; so ein Spiegel legt nichts zusammen und deckt nichts, wird aber als
    möglicherweise doppelt genannt, wenn im Umfeld eine kontierte Bankbewegung gleichen Betrags zwischen denselben Konten liegt."""
    entry_id: int
    day: datetime.date
    out_account: int
    in_account: int
    amount: int
    known: bool = True


class TransferOther(NamedTuple):
    """Eine Bankbewegung auf einem teilnehmenden Konto, die Kinder hat, aber kein Spiegel-Kind zwischen eigenen Konten: Sie ist
    anders kontiert (etwa als Zahlung oder als Umbuchung in einer anderen Form) und kann doch die Seite eines Übertrags sein.
    Sie bildet keine Gruppe und legt nichts zusammen; sie zählt in der Deckung als verbraucht und lässt einen Satz der
    Gegenseite im Umfeld als möglicherweise doppelt nennen."""
    bank_id: int
    account: int
    outgoing: bool
    amount: int                                      # |Betrag| der Bankbewegung in Minor Units, > 0
    day: datetime.date


class StackEntry(NamedTuple):
    """Ein Satz im Stapel: ID, Datum des Satzes und Betrag in Minor Units."""
    entry_id: int
    day: datetime.date
    amount: int


class MergedSection(NamedTuple):
    """Ein Abschnitt, dessen Eingangs-Sätze der Export weglässt: diese Sätze (``omitted``) und die Ausgangs-Sätze, deren Übertrag
    sie sind (``outgoing``). Je Abschnitt zählt ``Database.check_transfers``, ob der Zeitraum einen Hinweis braucht."""
    omitted: tuple
    outgoing: tuple


class DoubtfulEntry(NamedTuple):
    """Ein Satz im Stapel, dessen Übertrag möglicherweise doppelt darin steht (``day`` und ``amount`` des Satzes): ein
    Eingangs-Satz, der bleibt, obwohl im Umfeld seines Abschnitts ein Ausgang mit Spiegel oder anders kontiert liegt, ein
    Ausgangs-Satz mit einer anders kontierten Eingangsbewegung im Umfeld oder ein eigenständiger Spiegel mit einer kontierten
    Bankbewegung im Umfeld. ``trigger`` ist der Tag der ältesten Bewegung, die das Zusammenlegen sperrt oder den Hinweis
    auslöst (Ausgang ohne Satz, eigenständiger Spiegel, anders kontierte Bewegung), sonst ``None``."""
    entry_id: int
    day: datetime.date
    amount: int
    trigger: Optional[datetime.date] = None


class TransferSettlement(NamedTuple):
    omitted: dict           # {Satz-ID: StackEntry} – die Eingangs-Sätze, die der Export weglässt
    covered: frozenset      # IDs der Bankbewegungen ohne Satz, die ein Abschnitt deckt
    doubtful: dict          # {Satz-ID: DoubtfulEntry} – Sätze, deren Übertrag möglicherweise doppelt im Stapel steht
    merged: list            # [MergedSection] – die Abschnitte, deren Eingangs-Sätze wegfallen


_OUT_BOOKED, _IN_BOOKED, _OUT_FREE, _IN_FREE, _STANDALONE = range(5)


def own_transfer_accounts(account_coas):
    """``{Accounts.ID: COA-IDs}`` der eigenen Konten, die an der Erkennung der Überträge teilnehmen: Bank-/Kassenkonten mit
    SKR-Nummer, die kein anderes eigenes Konto ebenfalls trägt. Haben zwei Konten dieselbe Nummer, nimmt keins von beiden
    teil – ein Satz auf dieser Nummer ließe sich keinem der beiden zuordnen: Es gibt kein Bein, nichts wird zusammengelegt,
    und eine Bankbewegung ohne Satz auf einem von ihnen wird gemeldet. ``account_coas`` ist ``_account_coa_sets``."""
    holders = {}
    for account_id, coa_ids in account_coas.items():
        for coa_id in coa_ids:
            holders.setdefault(coa_id, set()).add(account_id)
    return {account_id: frozenset(coa_ids) for account_id, coa_ids in account_coas.items()
            if coa_ids and all(len(holders[coa_id]) == 1 for coa_id in coa_ids)}


def transfer_legs(bank_rows, with_children, entry_rows, account_coas, bank_coa_ids):
    """Aus den Zeilen der Datenbank die Beine, die eigenständigen Spiegel und die anders kontierten Bankbewegungen für
    ``settle_transfers`` machen – ohne Datenbankzugriff.

    ``bank_rows``: ``(ID, DateBooking, Amount, Account_ID)`` der Bankbewegungen (Betrag ≠ 0); ``with_children``: die IDs der
    Bankbewegungen, die mindestens ein Kind haben; ``entry_rows``: ``(ID, ParentBooking_ID, DateBooking, Amount, COA_ID,
    CounterCOA_ID, EntryForm)`` der Sätze, deren beide Seiten eigene Bank-/Kassenkonten sind; ``account_coas`` und
    ``bank_coa_ids`` wie ``Database._account_coa_sets`` und ``_get_bank_coa_ids``.

    Ein **Bein mit Spiegel** ist eine Bankbewegung auf einem teilnehmenden Konto (``own_transfer_accounts``) mit einem Kind,
    für das ``is_mirror_row`` gilt und dessen Seiten genau dieses Konto und ein anderes teilnehmendes sind; mehrere
    Spiegel-Kinder sind mehrere Beine. Ein **Bein ohne Satz** ist eine Bankbewegung auf einem teilnehmenden Konto ohne ein
    einziges Kind. Eine Bankbewegung mit Kindern, aber ohne Spiegel-Kind, ist kein Bein, sondern **anders kontiert**
    (``TransferOther``). Ein **eigenständiger Spiegel** (``ParentBooking_ID`` leer) mit bekannter Lesart
    (``mirror_coa``/``mirror_counter``) nimmt an den Gruppen teil – welches Konto verliert, sagt ``bank_effect`` –; mit
    ``mirror_legacy`` oder ohne Form legt er nichts zusammen und deckt nichts (``known=False``).

    Eine Zeile nimmt nur teil, wenn ihr Datum ein ISO-Datum (``_iso_day``) und ihr Betrag eine ganze Zahl ist (``_whole``): Ein
    Satz mit unlesbarem Datum oder Betrag ist kein Bein, seine Bankbewegung also anders kontiert."""
    own = own_transfer_accounts(account_coas)
    owner = {coa_id: account_id for account_id, coa_ids in own.items() for coa_id in coa_ids}
    banks = {}
    for bank_id, date, amount, account_id in bank_rows:
        day, minor = _iso_day(date), _whole(amount)
        if account_id in own and minor and day is not None:
            banks[bank_id] = (account_id, minor, day)

    legs = [TransferLeg(bank_id, account_id, amount < 0, abs(amount), day)
            for bank_id, (account_id, amount, day) in banks.items() if bank_id not in with_children]
    mirrors, legged = [], set()
    for entry_id, parent_id, date, amount, coa_id, counter_coa_id, form in entry_rows:
        day, minor = _iso_day(date), _whole(amount)
        side_a, side_b = owner.get(coa_id), owner.get(counter_coa_id)
        if (not minor or day is None or side_a is None or side_b is None or side_a == side_b
                or not is_mirror_row(form, coa_id, counter_coa_id, bank_coa_ids)):
            continue
        if parent_id is None:
            if form in (FORM_MIRROR_COA, FORM_MIRROR_COUNTER):
                loses_coa_side = bank_effect(form, minor, coa_id, counter_coa_id, own[side_a], bank_coa_ids) < 0
                out_account, in_account = (side_a, side_b) if loses_coa_side else (side_b, side_a)
                mirrors.append(TransferMirror(entry_id, day, out_account, in_account, abs(minor)))
            else:
                mirrors.append(TransferMirror(entry_id, day, side_a, side_b, abs(minor), False))
        elif parent_id in banks:
            account_id, bank_amount, bank_day = banks[parent_id]
            if account_id in (side_a, side_b):
                other = side_b if account_id == side_a else side_a
                legs.append(TransferLeg(parent_id, account_id, bank_amount < 0, abs(minor), bank_day, other, entry_id, day))
                legged.add(parent_id)
    others = [TransferOther(bank_id, account_id, amount < 0, abs(amount), day)
              for bank_id, (account_id, amount, day) in banks.items() if bank_id in with_children and bank_id not in legged]
    return legs, mirrors, others


def _any_between(days, low, high):
    """Liegt in der aufsteigenden Liste ``days`` ein Tag von ``low`` bis ``high`` (einschließlich)?"""
    return _first_between(days, low, high) is not None


def _first_between(days, low, high):
    """Der früheste Tag der aufsteigenden Liste ``days`` von ``low`` bis ``high`` (einschließlich), sonst ``None``."""
    index = bisect.bisect_left(days, low)
    return days[index] if index < len(days) and days[index] <= high else None


def _count_between(days, low, high):
    """Wie viele Tage der aufsteigenden Liste ``days`` liegen von ``low`` bis ``high`` (einschließlich)?"""
    return bisect.bisect_right(days, high) - bisect.bisect_left(days, low)


def _earliest(*days):
    """Der früheste der Tage, die nicht ``None`` sind – ``None``, wenn es keinen gibt."""
    known = [day for day in days if day is not None]
    return min(known) if known else None


class _Around(NamedTuple):
    """Was zum Umfeld eines Abschnitts gehört, aus der ganzen Gruppe: die Tage (aufsteigend) der Ausgänge ohne Satz und der
    eigenständigen Spiegel (``blocking``), der Ausgänge und Eingänge mit Spiegel, der anders kontierten Ausgänge auf dem
    Ausgangskonto und der anders kontierten Eingänge auf dem Eingangskonto – und die Länge des Umfelds."""
    blocking: list
    booked_out: list
    booked_in: list
    other_out: list
    other_in: list
    reach: datetime.timedelta


def _settle_section(section, around, omitted, covered, doubtful, merged):
    """Ein Abschnitt einer Gruppe (siehe ``settle_transfers``): zählen, zusammenlegen, auf Doppelung hinweisen, decken."""
    kinds = ([], [], [], [], [])
    for _day, _id, kind, member in section:
        kinds[kind].append(member)
    out_booked, in_booked, out_free, in_free, standalone = kinds
    a_m, b_m, a_c, b_c, s = len(out_booked), len(in_booked), len(out_free), len(in_free), len(standalone)
    low, high = section[0][0] - around.reach, section[-1][0] + around.reach
    out_around, in_around = _count_between(around.booked_out, low, high), _count_between(around.booked_in, low, high)
    a_o, b_o = _count_between(around.other_out, low, high), _count_between(around.other_in, low, high)
    blocker = _first_between(around.blocking, low, high)
    if a_m >= 1 and b_m >= 1 and a_m >= b_m + b_c and blocker is None:
        entries = tuple(StackEntry(leg.entry_id, leg.entry_day, leg.amount) for leg in in_booked)
        omitted.update((entry.entry_id, entry) for entry in entries)
        merged.append(MergedSection(entries, tuple(StackEntry(leg.entry_id, leg.entry_day, leg.amount) for leg in out_booked)))
    elif b_m >= 1 and (out_around or a_o):
        trigger = _earliest(blocker, _first_between(around.other_out, low, high))
        for leg in in_booked:
            doubtful[leg.entry_id] = DoubtfulEntry(leg.entry_id, leg.entry_day, leg.amount, trigger)
    if a_m >= 1 and b_o:
        trigger = _first_between(around.other_in, low, high)
        for leg in out_booked:
            doubtful[leg.entry_id] = DoubtfulEntry(leg.entry_id, leg.entry_day, leg.amount, trigger)
    if standalone and (out_around or in_around):
        for mirror in standalone:
            doubtful[mirror.entry_id] = DoubtfulEntry(mirror.entry_id, mirror.day, mirror.amount)
        s = 0                                                       # ein Spiegel neben einer kontierten Bankbewegung deckt nichts
    if a_c <= max(0, b_m - out_around - a_o) + s:
        covered.update(leg.bank_id for leg in out_free)
    if b_c <= max(0, a_m - in_around - b_o) + s:
        covered.update(leg.bank_id for leg in in_free)


def settle_transfers(legs, mirrors=(), others=(), window=TRANSFER_DAY_WINDOW, neighbour=TRANSFER_NEIGHBOUR_DAYS):
    """Überträge zwischen eigenen Konten zählen: welche Eingangs-Sätze der DATEV-Export weglässt, weil ihre Buchung schon
    in einem Ausgangs-Satz steht, welche Bankbewegungen ohne Satz durch die Spiegel der Gegenseite gedeckt sind und welche
    Sätze möglicherweise doppelt im Stapel stehen. Eine reine Rechnung über ``TransferLeg``, ``TransferMirror`` und
    ``TransferOther`` (``transfer_legs``) – keine Datenbank, höchstens O(n log n): Gruppen über Wörterbücher, je Gruppe einmal
    sortiert, das Umfeld über Halbierungssuche auf den sortierten Tagen.

    **Gruppen.** Schlüssel ist (Ausgangskonto X, Eingangskonto Y, Betrag a). Hinein gehören die Ausgangs-Beine mit Spiegel
    X → Y, die Eingangs-Beine mit Spiegel (auf Y, Gegenkonto X) und die eigenständigen Spiegel mit bekannter Lesart X → Y – und,
    nur wenn die Gruppe dadurch nicht erst entsteht, die Ausgänge ohne Satz auf X und die Eingänge ohne Satz auf Y mit dem
    Betrag a (ein Bein ohne Satz kann so in mehreren Gruppen liegen). Jede Gruppe wird nach Datum sortiert (dann nach ID) und
    in **Abschnitte** zerlegt: Ein neuer beginnt, wenn der Abstand zum vorherigen Element größer als ``window`` Tage ist. Das
    **Umfeld** eines Abschnitts sind alle Elemente der Gruppe, die höchstens ``neighbour`` Tage vor seinem ersten oder nach
    seinem letzten Element liegen (er selbst gehört dazu); dazu gehören auch die anders kontierten Ausgänge auf X und die anders
    kontierten Eingänge auf Y mit dem Betrag a.

    Je Abschnitt: ``a_m`` Ausgänge mit Spiegel, ``a_c`` Ausgänge ohne Satz, ``b_m`` Eingänge mit Spiegel, ``b_c`` Eingänge
    ohne Satz, ``s`` eigenständige Spiegel; im Umfeld ``A``/``B`` Ausgänge/Eingänge mit Spiegel und ``a_o``/``b_o`` anders
    kontierte Ausgänge/Eingänge.

    **Zusammenlegen.** Die Eingangs-Sätze eines Abschnitts fallen aus dem Stapel genau dann, wenn ``a_m ≥ 1``, ``b_m ≥ 1``
    und ``a_m ≥ b_m + b_c`` gilt und im UMFELD kein Ausgang ohne Satz und kein eigenständiger Spiegel liegt: Jede
    Ausgangsbewegung dort trägt ihren Satz, und es gibt mindestens so viele Ausgänge wie Eingänge – jeder Eingang gehört zu
    einem Ausgang, dessen Satz im Stapel steht. Dann fallen ALLE ``b_m`` Eingangs-Sätze des Abschnitts weg, sonst keiner
    (welcher zu welchem Ausgang gehört, bleibt offen: Gepaart wird nicht). Anders kontierte Bewegungen ändern daran nichts.

    **Möglicherweise doppelt** (``doubtful``). Bleiben die Eingangs-Sätze eines Abschnitts im Stapel, liegt aber in seinem
    Umfeld mindestens ein Ausgang mit Spiegel oder ein anders kontierter Ausgang (``A`` oder ``a_o``), steht jeder dieser
    Eingangs-Sätze möglicherweise doppelt im Stapel; ebenso jeder Ausgangs-Satz eines Abschnitts, in dessen Umfeld ein anders
    kontierter Eingang liegt (``b_o``). Ebenso ein eigenständiger Spiegel mit bekannter Lesart, in dessen Umfeld ein Bein mit
    Spiegel liegt (ein Import-Spiegel und eine kontierte Bankbewegung), und ein eigenständiger Spiegel ohne bekannte Lesart
    (``known=False``), neben dem im Umfeld ein Bein mit Spiegel zwischen denselben Konten und mit demselben Betrag liegt.

    **Gedeckt.** Die Ausgänge ohne Satz eines Abschnitts gelten als gedeckt genau dann, wenn
    ``a_c ≤ max(0, b_m − A − a_o) + s``, die Eingänge ohne Satz, wenn ``b_c ≤ max(0, a_m − B − b_o) + s``: Ein Eingangs-Satz,
    dessen Ausgang mit Satz oder anders kontiert in der Nähe steht, ist dessen Gegenseite, nicht die eines Ausgangs ohne
    Satz. ``s`` zählt nur, wenn im Umfeld kein Bein mit Spiegel liegt – ein Spiegel neben einer kontierten Bankbewegung deckt
    nichts (er wird als möglicherweise doppelt genannt). Reicht es nicht, ist KEINE ohne Satz dieser Seite in diesem Abschnitt
    gedeckt. Eine Bewegung ist gedeckt, wenn sie es in mindestens einem Abschnitt ist.

    Returns: ``TransferSettlement(omitted, covered, doubtful, merged)``."""
    groups = {}
    free_out, free_in = {}, {}
    other_out, other_in = {}, {}
    booked_pairs = {}                        # (kleineres Konto, größeres Konto, Betrag) -> Tage der Beine mit Spiegel, beide Richtungen
    unknown = []
    for leg in legs:
        if leg.entry_id is None:
            (free_out if leg.outgoing else free_in).setdefault((leg.account, leg.amount), []).append(leg)
            continue
        booked_pairs.setdefault((min(leg.account, leg.other), max(leg.account, leg.other), leg.amount), []).append(leg.day)
        if leg.outgoing:
            groups.setdefault((leg.account, leg.other, leg.amount), []).append((leg.day, leg.bank_id, _OUT_BOOKED, leg))
        else:
            groups.setdefault((leg.other, leg.account, leg.amount), []).append((leg.day, leg.bank_id, _IN_BOOKED, leg))
    for mirror in mirrors:
        if mirror.known:
            groups.setdefault((mirror.out_account, mirror.in_account, mirror.amount), []).append(
                (mirror.day, mirror.entry_id, _STANDALONE, mirror))
        else:
            unknown.append(mirror)
    for other in others:
        (other_out if other.outgoing else other_in).setdefault((other.account, other.amount), []).append(other.day)
    for days in (*other_out.values(), *other_in.values(), *booked_pairs.values()):
        days.sort()

    omitted, covered, doubtful, merged = {}, set(), {}, []
    reach = datetime.timedelta(days=neighbour)
    for (out_account, in_account, amount), members in groups.items():
        members.extend((leg.day, leg.bank_id, _OUT_FREE, leg) for leg in free_out.get((out_account, amount), ()))
        members.extend((leg.day, leg.bank_id, _IN_FREE, leg) for leg in free_in.get((in_account, amount), ()))
        members.sort(key=lambda member: member[:3])
        around = _Around([member[0] for member in members if member[2] in (_OUT_FREE, _STANDALONE)],
                         [member[0] for member in members if member[2] == _OUT_BOOKED],
                         [member[0] for member in members if member[2] == _IN_BOOKED],
                         other_out.get((out_account, amount), ()), other_in.get((in_account, amount), ()), reach)
        section, previous = [], None
        for member in members:
            if previous is not None and (member[0] - previous).days > window:
                _settle_section(section, around, omitted, covered, doubtful, merged)
                section = []
            section.append(member)
            previous = member[0]
        _settle_section(section, around, omitted, covered, doubtful, merged)
    for mirror in unknown:
        days = booked_pairs.get((min(mirror.out_account, mirror.in_account), max(mirror.out_account, mirror.in_account),
                                 mirror.amount), ())
        if _any_between(days, mirror.day - reach, mirror.day + reach):
            doubtful[mirror.entry_id] = DoubtfulEntry(mirror.entry_id, mirror.day, mirror.amount)
    return TransferSettlement(omitted, frozenset(covered), doubtful, merged)


def _edge_entries(entries, count, from_start):
    """Die ``count`` Einträge von ``entries`` (nach Datum sortiert), die an dem Rand des Zeitraums liegen, über den ihr Übertrag
    läuft: höchstens ``from_start`` vom Anfang, der Rest vom Ende."""
    first = min(count, from_start)
    return entries[:first] + (entries[len(entries) - (count - first):] if count > first else [])


def _boundary_entries(merged, first, last):
    """Die Sätze, die ein Zeitraum ``first`` bis ``last`` in Hinweis 2 nennt, je Abschnitt gezählt: Fallen im Zeitraum mehr
    Eingangs-Sätze eines Abschnitts weg, als Ausgangs-Sätze dieses Abschnitts im Zeitraum stehen, fehlen die überzähligen in
    diesem Stapel (``incoming``: ihre Ausgangsseite steht im Stapel eines anderen Zeitraums). Stehen umgekehrt mehr Ausgangs-Sätze
    im Zeitraum, als Eingangs-Sätze des Abschnitts wegfallen, und fallen außerhalb welche weg, stehen die überzähligen Ausgangs-Sätze
    hier und ihre Eingangsseite in einem anderen Stapel – dort vielleicht auch (``outgoing``; höchstens so viele, wie
    Eingangs-Sätze außerhalb wegfallen). Genannt werden die Sätze am Rand, über den der Übertrag läuft: Vom Anfang so viele, wie
    zu Beginn des Zeitraums unterwegs sind (Ausgänge vor dem Zeitraum, deren Eingang noch aussteht – oder umgekehrt), der Rest vom
    Ende; eine lange, ausgeglichene Reihe davor zählt nicht. Returns ``(incoming, outgoing)``, je ``[StackEntry]`` nach Datum
    und ID."""
    incoming, outgoing = [], []
    for section in merged:
        omitted_here = sorted((entry for entry in section.omitted if first <= entry.day <= last), key=lambda entry: (entry.day, entry.entry_id))
        outgoing_here = sorted((entry for entry in section.outgoing if first <= entry.day <= last), key=lambda entry: (entry.day, entry.entry_id))
        surplus = len(omitted_here) - len(outgoing_here)
        if surplus == 0:
            continue
        omitted_before = sum(1 for entry in section.omitted if entry.day < first)
        outgoing_before = sum(1 for entry in section.outgoing if entry.day < first)
        if surplus > 0:
            outside = len(section.outgoing) - len(outgoing_here)
            incoming += _edge_entries(omitted_here, min(surplus, outside), max(0, outgoing_before - omitted_before))
        else:
            outside = len(section.omitted) - len(omitted_here)
            outgoing += _edge_entries(outgoing_here, min(-surplus, outside), max(0, omitted_before - outgoing_before))
    key = lambda entry: (entry.day, entry.entry_id)
    return sorted(incoming, key=key), sorted(outgoing, key=key)


class TransferCheck(NamedTuple):
    """Was ``Database.check_transfers`` für einen Zeitraum findet."""
    unbooked: list      # [(BankID, DateBooking, Amount€)]: Bankbewegungen im Zeitraum ohne Buchungssatz, die kein Abschnitt deckt
    omitted: set        # die Sätze, die der Export weglässt (über den ganzen Bestand)
    elsewhere: list     # [(Satz-ID, DateBooking, Amount€)]: weggelassene Eingangs-Sätze im Zeitraum, deren Ausgangsseite im Stapel eines anderen Zeitraums steht
    elsewhere_out: list  # [(Satz-ID, DateBooking, Amount€)]: Ausgangs-Sätze im Zeitraum, deren Eingangsseite außerhalb weggelassen wird
    doubtful: list      # [(Satz-ID, DateBooking, Amount€)]: Sätze im Zeitraum, deren Übertrag möglicherweise doppelt im Stapel steht
    trigger: Optional[tuple]    # (Datum, Betrag€) der ältesten Bewegung, die einen dieser Sätze auslöst (Hinweis 3), sonst ``None``


#: Die Spalten eines Kandidaten des Auto-Abgleichs, in der Reihenfolge von ``_Cand``.
_ID_CHUNK = 500      # so viele IDs je ``IN (…)``: unter der Parametergrenze jeder SQLite-Version

_CAND_COLUMNS = 'ID, Text, COA_ID, CounterCOA_ID, DocumentNumber, RecipientClient, Amount, EntryForm, Account_ID'


class _Cand(NamedTuple):
    """Ein freier Buchungssatz als Kandidat im Auto-Abgleich (``link_bank_to_entries``): Die Stufen greifen
    auf Felder mit Namen zu, nie auf einen Index – so landet nie ein falsches Feld im Text der Bankzeile."""
    id: int
    text: Optional[str]
    coa: Optional[int]
    counter: Optional[int]
    docnr: Optional[str]
    recipient: Optional[str]
    amount: Optional[int]       # Minor Units
    form: Optional[str]         # ``Bookings.EntryForm``
    account_id: Optional[int]


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

    def _unlinked_entry_sql(self, cursor):
        """Die Bedingung (``sql`` ohne ``WHERE``, ``params``) dafür, dass ein Buchungssatz einer Bankbewegung im
        Buchungssatz-Editor zugeordnet werden darf: ein ``entry`` ohne Bankbewegung (``ParentBooking_ID``), ohne eigenes Konto
        (``Account_ID``), nicht vom Abgleich gelöst (``resolved``), kein markierter Satz einer Rechnungszahlung (``invoice_payment``
        – frei gibt es ihn nur nach einem Eingriff an der App vorbei, und er gehörte zu einer Zuordnung, nicht in den Editor) und
        keine Forderungsbuchung (``receivable_condition``). Die
        EINE Definition für die Kandidaten des Zuordnen-Widgets (``get_unlinked_entry_bookings``) und die Prüfung beim Speichern
        (``adoptable_entry_ids``) – die Abfrage muss auf ``Bookings`` ohne Alias laufen. ``cursor`` wie bei ``_receivable_sql``."""
        receivable_sql, receivable_params = self._receivable_sql(cursor)
        return ("BookingType = 'entry' AND ParentBooking_ID IS NULL AND Account_ID IS NULL "
                f"AND (Status IS NULL OR Status NOT IN ('resolved', 'invoice_payment')) "
                f"AND NOT {receivable_sql}"), receivable_params

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
        where, where_params = self._unlinked_entry_sql(cursor)
        order = ("ABS(JULIANDAY(DateBooking) - JULIANDAY(?)), DateBooking DESC"
                 if around_date else "DateBooking DESC")
        params = where_params + ([around_date] if around_date else []) + [limit]
        cursor.execute(f'''
            SELECT ID, DateBooking, Amount, DocumentNumber, Text,
                   RecipientClient, COA_ID, CounterCOA_ID, EntryForm, TaxRate, TaxAmount
            FROM Bookings
            WHERE {where}
            ORDER BY {order}
            LIMIT ?
        ''', params)
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 2, 10) for r in rows]   # Amount, TaxAmount

    def adoptable_entry_ids(self, ids):
        """Welche dieser Buchungen darf der Buchungssatz-Editor einer Bankbewegung zuordnen? Returns die IDs als ``set``.

        Dieselbe Bedingung wie die Kandidaten des Zuordnen-Widgets (``_unlinked_entry_sql``), aber ohne dessen Begrenzung auf
        50 Zeilen: Ein freier Satz bleibt zuordenbar, auch wenn er nicht unter den angezeigten Kandidaten steht. Rein lesend.
        Der Editor fragt damit jede ID, die ein Formular mitbringt und die kein Satz unter der Bankbewegung ist – die ID ist
        eine Eingabe des Browsers, kein Beweis, dass der Nutzer die Buchung gesehen hat."""
        ids = [int(booking_id) for booking_id in ids]
        if not ids:
            return set()
        conn = self._get_connection()
        try:
            where, params = self._unlinked_entry_sql(conn.cursor())
            found = set()
            for start in range(0, len(ids), _ID_CHUNK):       # SQLite begrenzt die Zahl der Parameter je Abfrage
                chunk = ids[start:start + _ID_CHUNK]
                found.update(row[0] for row in conn.execute(
                    f"SELECT ID FROM Bookings WHERE {where} AND ID IN ({','.join('?' * len(chunk))})", params + chunk))
            return found
        finally:
            conn.close()

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

    def _settle_stock(self, cursor):
        """Den ganzen Bestand lesen und die Überträge zwischen eigenen Konten zählen (``settle_transfers``). Gelesen wird
        immer alles, nicht ein Zeitraum: Ein Abschnitt kann über eine Zeitraumgrenze reichen. Drei Abfragen, keine je
        Zeile: die Bankbewegungen, die Bankbewegungen mit Kindern und die Sätze zwischen eigenen Konten."""
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        account_coas = self._account_coa_sets(cursor)
        cursor.execute("SELECT ID, DateBooking, Amount, Account_ID FROM Bookings "
                       "WHERE BookingType = 'bank' AND Amount <> 0 AND Account_ID IS NOT NULL")
        bank_rows = cursor.fetchall()
        cursor.execute('SELECT DISTINCT ParentBooking_ID FROM Bookings WHERE ParentBooking_ID IS NOT NULL')
        with_children = {row[0] for row in cursor.fetchall()}
        entry_rows = []
        if bank_coa_ids:
            marks = ','.join('?' * len(bank_coa_ids))
            cursor.execute(f"""
                SELECT ID, ParentBooking_ID, DateBooking, Amount, COA_ID, CounterCOA_ID, EntryForm FROM Bookings
                WHERE BookingType = 'entry' AND COA_ID IN ({marks}) AND CounterCOA_ID IN ({marks})
            """, [*bank_coa_ids, *bank_coa_ids])
            entry_rows = cursor.fetchall()
        legs, mirrors, others = transfer_legs(bank_rows, with_children, entry_rows, account_coas, bank_coa_ids)
        return settle_transfers(legs, mirrors, others)

    def check_transfers(self, date_from: str, date_to: str) -> TransferCheck:
        """Was der DATEV-Export für diesen Zeitraum wissen muss, aus EINEM Stand der Datenbank (nur lesend; die Rechnung
        steht in ``settle_transfers``, höchstens O(n log n) im Bestand):

        * ``unbooked``: Bankbewegungen im Zeitraum ohne einen einzigen Buchungssatz – im Stapel stehen nur Sätze, sie fehlen
          darin –, sofern keine Gegenseite sie deckt. Gemeint ist eine Zeile ``BookingType='bank'`` mit ``DateBooking`` im
          Zeitraum, Betrag ≠ 0 (eine Zahl) und ohne Kind (``ParentBooking_ID``); ein Kind mit Datum außerhalb des Zeitraums
          zählt als kontiert. Eine Bankzeile mit eigenem ``COA_ID``, aber ohne Kind, zählt mit, ebenso eine Kassenbewegung.
          Bewegungen auf einem Konto ohne SKR-Nummer oder mit einer Nummer, die ein anderes Konto auch trägt, sind nie gedeckt.
        * ``omitted``: die Eingangs-Sätze, die der Export weglässt, weil derselbe Übertrag schon in einem Ausgangs-Satz steht –
          über den ganzen Bestand, nicht je Zeitraum.
        * ``elsewhere`` und ``elsewhere_out`` (Hinweis 2), je Abschnitt gezählt (``_boundary_entries``): weggelassene Sätze im
          Zeitraum, die über die Zahl der Ausgangs-Sätze des Abschnitts im Zeitraum hinausgehen – ihr Übertrag steht in diesem
          Stapel nicht, sondern mit seiner Ausgangsseite im Stapel eines anderen Zeitraums –, und umgekehrt Ausgangs-Sätze im
          Zeitraum, die über die weggelassenen Sätze des Abschnitts im Zeitraum hinausgehen, während dessen Eingangsseite
          außerhalb wegfällt: Wurde der andere Zeitraum schon exportiert, bevor beide Seiten kontiert waren, stehen sie dort
          ebenfalls.
        * ``doubtful`` und ``trigger`` (Hinweis 3): die Sätze im Zeitraum, deren Übertrag möglicherweise doppelt im Stapel
          steht (Eingangs-Sätze, die bleiben, obwohl im Umfeld ein Ausgang mit Spiegel oder anders kontiert liegt;
          Ausgangs-Sätze neben einem anders kontierten Eingang; eigenständige Spiegel neben einer kontierten Bankbewegung), und
          die älteste Bewegung, die das Zusammenlegen sperrt oder den Hinweis auslöst.

        Returns: ``TransferCheck(unbooked, omitted, elsewhere, elsewhere_out, doubtful, trigger)``, die Listen nach Datum und ID."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            settlement = self._settle_stock(cursor)
            cursor.execute("""
                SELECT b.ID, b.DateBooking, b.Amount FROM Bookings b
                WHERE b.BookingType = 'bank' AND b.DateBooking BETWEEN ? AND ? AND b.Amount <> 0
                  AND typeof(b.Amount) IN ('integer', 'real')
                  AND NOT EXISTS (SELECT 1 FROM Bookings c WHERE c.ParentBooking_ID = b.ID)
                ORDER BY b.DateBooking, b.ID
            """, (date_from, date_to))
            unbooked = [(bank_id, date, from_minor(amount)) for bank_id, date, amount in cursor.fetchall()
                        if bank_id not in settlement.covered]
        finally:
            conn.close()
        first, last = _iso_day(date_from), _iso_day(date_to)
        elsewhere, elsewhere_out, doubtful, trigger = [], [], [], None
        if first is not None and last is not None:
            incoming, outgoing = _boundary_entries(settlement.merged, first, last)
            elsewhere = [(entry.entry_id, entry.day.isoformat(), from_minor(entry.amount)) for entry in incoming]
            elsewhere_out = [(entry.entry_id, entry.day.isoformat(), from_minor(entry.amount)) for entry in outgoing]
            named = sorted((entry for entry in settlement.doubtful.values() if first <= entry.day <= last),
                           key=lambda entry: (entry.day, entry.entry_id))
            doubtful = [(entry.entry_id, entry.day.isoformat(), from_minor(entry.amount)) for entry in named]
            triggered = [entry for entry in named if entry.trigger is not None]
            if triggered:
                oldest = min(triggered, key=lambda entry: (entry.trigger, entry.entry_id))
                trigger = (oldest.trigger.isoformat(), from_minor(oldest.amount))
        return TransferCheck(unbooked, set(settlement.omitted), elsewhere, elsewhere_out, doubtful, trigger)

    def find_unbooked_bank_movements(self, date_from: str, date_to: str):
        """Bankbewegungen im Zeitraum ohne Buchungssatz, die keine Gegenseite deckt (``check_transfers``).

        Returns: [(BankID, DateBooking, Amount€)] nach Datum und ID."""
        return self.check_transfers(date_from, date_to).unbooked

    def duplicate_transfer_rows(self) -> set:
        """IDs der Eingangs-Sätze, die der DATEV-Export weglässt, weil derselbe Übertrag zwischen eigenen Konten schon in einem
        Ausgangs-Satz steht (``settle_transfers``) – über den ganzen Bestand."""
        conn = self._get_connection()
        try:
            return set(self._settle_stock(conn.cursor()).omitted)
        finally:
            conn.close()

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

        Angefasst werden nur Bankbewegungen ohne Kinder und Sätze ohne
        ParentBooking_ID: Eine bestehende Verknüpfung (auch eine von Hand
        gesetzte, falsch gerichtete) und der Text der Bankbewegung ändern
        sich nie.

        Richtungsregel (Stufen 1–6): Ein Satz wird nur verknüpft, wenn seine
        Wirkung auf das Konto der Bankbewegung – mit Vorzeichen, gerechnet wie
        in der Split-Summe (``link_effect``, ``bank_effect``) – dem Betrag der
        Bewegung entspricht; bei einer Gruppe die Summe der Wirkungen. Eine
        Lastschrift (−) wird so nie mit einer Einnahme (+) verknüpft, ein Satz
        auf einer anderen Geldseite (Kasse, anderes eigenes Bankkonto, 1460)
        oder mit dem Account_ID eines anderen Kontos nie mit dieser Bewegung.
        Ein Spiegel der Form ``mirror_legacy`` (Lesart unbekannt) wird nie
        automatisch verknüpft. Umbuchungen ohne Geldseite (Wirkung 0, z.B.
        Wartekonto an Erlöskonto, Privatanteil) zählen bei der Eindeutigkeit der
        Einzelstufen mit, werden dort aber nie allein verknüpft – nur zusammen
        mit dem Satz, der das Geld bewegt (Gruppen-Stufen). Die Beträge
        vergleichen alle Stufen mit einer Toleranz von unter 50 Minor Units.

        Matching-Strategien (in der Reihenfolge, in der sie laufen: 0, 1, 2,
        4, 3, 3b, 3c, 3d, 5, 6, 6b, Nachlauf 6c, 7):

        Stufe 0 – Quellklammer (SourceGroup, z.B. WISO-ACCOUNTINGID):
            Wo der Import die Zusammengehörigkeit mitgeliefert hat, wird
            nichts geraten; die Summe rechnet schon mit Vorzeichen.

        Stufe 1 – Datum + normalisierter Empfänger + Wirkung:
            Leerzeichen in RecipientClient werden komprimiert, Groß-/Klein-
            schreibung ignoriert. Doppik-Entries (beide Seiten eigene
            Bankkonten) werden rausgefiltert. Mehrfach-Treffer (z.B. Fraenk):
            der Token-Treffer, sonst der erste, dessen Wirkung passt. Passt
            keiner, geht es mit Stufe 2 weiter.

        Stufe 2 – Datum + Wirkung:
            Ohne Empfänger-Bedingung, Doppik-Filter aktiv.
            Ein eindeutiger Treffer wird verknüpft, wenn seine Wirkung passt.

        Stufe 4 – DocumentNumber als Tiebreaker (läuft vor Stufe 3):
            Falls Stufe 2 mehrere Treffer liefert, wird versucht
            ob genau einer die passende Belegnummer enthält (und passt).

        Stufe 3 – Split-Gruppe: Datum + SUM der Wirkungen der Gruppenmitglieder:
            Für Bank-Buchungen die einer BookingGroup (Split) entsprechen.
            Vorfilter: ABS der Summe der Beträge.

        Stufe 3b – Rechnungs-Split: Datum + SUM der Wirkungen:
            Für Ausgangsrechnungs-Zahlungen die als Doppelbuchung
            erfasst werden (z.B. Bank 1810 + Erlöse 4405).  Die Gruppe
            wird nur verknüpft, wenn mindestens ein Mitglied ein
            Bank-COA hat. Vorfilter: ABS(SUM/Anzahl).

        Stufe 3c – Privatanteil-Split: Datum + SUM ohne Privatentnahme-Offset:
            Für Split-Gruppen deren Summe durch eine positive
            Privatentnahme-Gegenbuchung (COA 2100–2199) verfälscht wird.
            Die Gruppensumme abzüglich des positiven Privatanteils
            muss dem Bankbetrag entsprechen; die Wirkungen müssen es auch.

        Stufe 3d – Sammelzahlung: Datum + mehrere Rechnungsnummern im Text:
            Für Bank-Buchungen mit komma-getrennten Rechnungsnummern im
            Text (z.B. "2025011,2025010").  Die Entries mehrerer
            BookingGroups werden zusammengefasst.  Summe der Bank-COA-
            Entries über alle Gruppen muss dem Bankbetrag entsprechen, und
            die Summe der Wirkungen aller Entries ebenso.

        Gruppen (3, 3b, 3c, 3d) gelten nur als Treffer, wenn jedes Mitglied,
        das verknüpft würde (unverknüpft, keine Forderung), eine Wirkung hat
        (nicht ``None``), mindestens eines Geld bewegt (≠ 0) und die Summe der
        Wirkungen den Bankbetrag trifft.

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
            und die Ähnlichkeit > 50 % beträgt – und seine Wirkung passt.

        Stufe 6b/6c – mehrere Quellgruppen zusammen (und der Nachlauf dazu):
            WISO zerlegt manche Belege in mehrere Vorgänge; die Summe
            rechnet mit Vorzeichen wie Stufe 0.

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
        # Alle Zahlungskonten (eigene Konten + 1460) für ``link_effect``: Ein Satz, dessen
        # Geldseite ein anderes Zahlungskonto ist, gehört nie zu dieser Bankbewegung.
        payment_coa_ids = self._get_payment_coa_ids(cursor)

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
                if e.coa in bank_coa_ids and e.counter in bank_coa_ids:
                    continue
                filtered.append(e)
            return filtered

        def _filter_already(entries):
            """Bereits in diesem Durchlauf verknüpfte Entries und Forderungen rausfiltern."""
            return [e for e in entries
                    if e.id not in already_linked_entry_ids and e.id not in receivable_ids]

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

        def _token_tiebreak(bank_text, entries):
            """Unter mehreren Entries denjenigen finden, der einen
            gemeinsamen numerischen Token mit dem Banktext teilt.

            Wenn mehrere Entries überlappende Tokens haben (z.B. weil
            gemeinsame CRED-/IBAN-Nummern in allen PayPal-Texten stehen),
            wird der Entry mit den *meisten* gemeinsamen Tokens genommen,
            sofern er eindeutig mehr hat als alle anderen.

            Args:
                bank_text: Text der Bank-Buchung
                entries:   Kandidaten-Liste (``_Cand``); verglichen wird ihr Text

            Returns:
                Einzel-Kandidat (``_Cand``) oder None.
            """
            bank_tokens = _extract_tokens(bank_text)
            if not bank_tokens:
                return None
            matches = [e for e in entries
                       if _extract_tokens(e.text) & bank_tokens]
            if len(matches) == 1:
                return matches[0]
            if len(matches) >= 2:
                # Score = Anzahl gemeinsamer Tokens; höchster gewinnt
                scored = [(len(_extract_tokens(e.text) & bank_tokens), e)
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

        def _docnr_members(docnr, bank_date):
            """Die Sätze, die ``_link_docnr_group`` mit der Bank verknüpfen würde: alle
            unverknüpften Buchungen eines Belegs an diesem Tag, ohne Forderungen (dieselbe
            Bedingung wie dort). Als ``_Cand``."""
            cursor.execute(f'''
                SELECT {_CAND_COLUMNS} FROM Bookings
                WHERE DocumentNumber = ? AND DateBooking = ?
                  AND BookingType = 'entry' AND ParentBooking_ID IS NULL
                  AND NOT {receivable_sql}
            ''', [docnr, bank_date] + receivable_params)
            return [_Cand(*row) for row in cursor.fetchall()]

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

        def _do_link(bank_id, cand):
            """Einzelnen Kandidaten (``_Cand``) mit der Bank-Buchung verknüpfen."""
            cursor.execute('''
                UPDATE Bookings SET ParentBooking_ID = ?
                WHERE ID = ?
            ''', (bank_id, cand.id))
            already_linked_entry_ids.add(cand.id)
            # WISO-Text auf die Bank-Buchung übernehmen (manuell kuratiert)
            if cand.text:
                cursor.execute(
                    'UPDATE Bookings SET Text = ? WHERE ID = ?',
                    (cand.text, bank_id))

        # Richtungsregel (siehe ``link_effect``). Die vier Helfer rechnen für die Bankbewegung, die die
        # Schleife unten gerade prüft: ``bank_account_id`` und ``bank_amount`` sind deren Konto und Betrag.
        def effect(c):
            """Wirkung des Satzes auf das Konto der Bankbewegung, mit Vorzeichen (``link_effect``);
            ``None`` = gehört nicht zu ihr. Ein Satz mit eigenem ``Account_ID`` (eigenständig auf
            einer Kasse) gehört nur zu Bewegungen desselben Kontos."""
            if c.account_id is not None and c.account_id != bank_account_id:
                return None
            if c.amount is not None and (isinstance(c.amount, bool) or not isinstance(c.amount, (int, float))):
                return None             # Betrag als Text (Eingriff von Hand): nicht rechnen, nie Kandidat
            return link_effect(c.form, c.amount, c.coa, c.counter,
                               account_coas.get(bank_account_id), bank_coa_ids, payment_coa_ids)

        def fits(c):
            """Bewegt der Satz Geld, und zwar den Betrag der Bankbewegung (in deren Richtung)?
            Eine Umbuchung (Wirkung 0) passt nie."""
            e = effect(c)
            return e is not None and e != 0 and abs(e - bank_amount) < 50

        def _single_candidates(pool):
            """Einzelkandidaten der Bankbewegung (Stufen 1, 2, 4, 5, 6): ohne Doppik-Spiegel, ohne
            schon verknüpfte Sätze und Forderungen, ohne Sätze, die nie zu dieser Bewegung gehören
            oder in die falsche Richtung bewegen. Umbuchungen (Wirkung 0) bleiben: Sie zählen bei
            der Eindeutigkeit mit (sonst verknüpfte Stufe 2 bei „Bank an Debitoren“ + „Wartekonto an
            Erlöskonto“ die Zahlung allein und ließe die Umbuchung frei), verknüpft wird nur, was ``fits``."""
            return [c for c in _filter_already(_filter_doppik(pool))
                    if fits(c) or effect(c) == 0]

        def _group_fits(members):
            """Trifft die Gruppe die Bankbewegung? Jedes Mitglied, das verknüpft würde, hat eine Wirkung,
            mindestens eines bewegt Geld, und die Summe der Wirkungen entspricht dem Betrag."""
            effects = [effect(c) for c in members]
            if not effects or None in effects or not any(effects):
                return False
            return abs(sum(effects) - bank_amount) < 50

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

            # ── Kandidaten der Einzelstufen 1, 2, 4, 5, 6 ────────────────
            # EINE Abfrage je Bankbewegung (nichts wird verknüpft, bevor eine Stufe
            # ``continue`` sagt, der Bestand ist für alle Stufen derselbe): Betrag
            # ohne Vorzeichen als Vorfilter, ID-Reihenfolge wie bisher. Die Richtung
            # entscheidet erst ``_single_candidates`` (Wirkung mit Vorzeichen).
            cursor.execute(f'''
                SELECT {_CAND_COLUMNS} FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND ABS(ABS(Amount) - ?) < 50
                ORDER BY ID
            ''', (bank_date, abs_amount))
            candidates = _single_candidates([_Cand(*row) for row in cursor.fetchall()])

            # ── Stufe 1: Datum + Empfänger (normalisiert) + Wirkung ──────
            if recip_norm:
                entries = [c for c in candidates
                           if _norm(c.recipient) == recip_norm
                           or _norm(c.text) != '' and recip_norm in _norm(c.text)]
                hit = None
                if len(entries) == 1:
                    hit = entries[0] if fits(entries[0]) else None
                elif len(entries) >= 2:
                    # Mehrere Treffer: Token-Tiebreak (z.B. Fraenk-Nummern)
                    hit = _token_tiebreak(bank_text, entries)
                    if hit is None:
                        # Fallback: den ersten passenden nehmen
                        hit = next((c for c in entries if fits(c)), None)
                    elif not fits(hit):
                        # Die Kennung zeigt auf eine Umbuchung: Ihr Beleg gehört als Gruppe zur Bewegung
                        # (Stufe 3b) – hier keinen Satz eines anderen Belegs desselben Empfängers nehmen
                        hit = None
                if hit is not None:
                    _do_link(bank_id, hit)
                    linked += 1
                    continue

            # ── Stufe 2: Datum + Wirkung ─────────────────────────────────
            if len(candidates) == 1 and fits(candidates[0]):
                # Nur diesen Entry linken, NICHT die ganze Gruppe
                _do_link(bank_id, candidates[0])
                linked += 1
                continue

            # ── Stufe 4: DocumentNumber als Tiebreaker ───────────────────
            if len(candidates) > 1 and bank_docnr:
                doc_match = [c for c in candidates
                             if c.docnr and (bank_docnr in c.docnr or c.docnr in bank_docnr)]
                if len(doc_match) == 1 and fits(doc_match[0]):
                    # Nur diesen Entry linken, NICHT die ganze Gruppe
                    _do_link(bank_id, doc_match[0])
                    linked += 1
                    continue

            # ── Stufe 3: Split-Gruppe — SUM(Betrag) und Wirkung passen ───
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
                      if not (_docnr_ids(g[0], bank_date) & already_linked_entry_ids)
                      and _group_fits(_docnr_members(g[0], bank_date))]
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
                if (coa_ids & bank_coa_ids  # mindestens ein Bank-COA
                        and _group_fits(_docnr_members(docnr, bank_date))):
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
                            if not (_docnr_ids(g[0], bank_date) & already_linked_entry_ids)
                            and _group_fits(_docnr_members(g[0], bank_date))]
            if len(priv_matches) == 1:
                _link_docnr_group(bank_id, priv_matches[0], bank_date)
                linked += 1
                continue

            # ── Stufe 3d: Sammelzahlung ────────────────────────────────
            # Muster: Bank-Text enthält mehrere komma- oder leerzeichen-
            # getrennte Rechnungsnummern (z.B. "2025011,2025010").
            # Die zugehörigen Entries liegen in verschiedenen
            # BookingGroups.  Summe der Bank-COA-Entries über alle
            # Gruppen muss dem Bankbetrag entsprechen – und die Summe der
            # Wirkungen aller Entries auf das Konto der Bankbewegung ebenso
            # (mit Vorzeichen).
            doc_nr_candidates = set(re.findall(r'\b\d{4,}\b',
                                               bank_text or ''))
            if len(doc_nr_candidates) >= 2:
                ph = ','.join('?' * len(doc_nr_candidates))
                cursor.execute(f'''
                    SELECT {_CAND_COLUMNS}
                    FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NULL
                      AND DateBooking = ?
                      AND DocumentNumber IN ({ph})
                ''', (bank_date, *doc_nr_candidates))
                sammel_entries = _filter_already([_Cand(*row) for row in cursor.fetchall()])
                doc_nrs_found = {e.docnr for e in sammel_entries}
                if len(doc_nrs_found) >= 2 and len(sammel_entries) >= 2:
                    # Zahlungseingänge sind als Doppelbuchung erfasst (COA =
                    # Bankkonto): dann zählt nur deren Bank-Seite. Ausgaben
                    # (COA = Aufwand, Gegenkonto = Bank) haben keine solche
                    # Zeile – dort ist die Summe aller Belege maßgeblich, sonst
                    # bliebe eine Sammelüberweisung an Lieferanten unverknüpft.
                    bank_coa_sum = sum(
                        e.amount for e in sammel_entries
                        if e.coa in bank_coa_ids)
                    if bank_coa_sum == 0:
                        bank_coa_sum = sum(e.amount for e in sammel_entries)
                    if (abs(abs(bank_coa_sum) - abs_amount) < 50
                            and _group_fits(sammel_entries)):
                        for e in sammel_entries:
                            cursor.execute(
                                'UPDATE Bookings SET ParentBooking_ID = ?'
                                ' WHERE ID = ?',
                                (bank_id, e.id))
                            already_linked_entry_ids.add(e.id)
                        linked += 1
                        continue

            # ── Stufe 5: Text-Token-Matching (letzte Chance) ───────────
            # Suche unter allen ungelinkten Entries desselben Datums+Betrags
            # (die Kandidaten von oben) nach einem gemeinsamen numerischen
            # Token (>= 6 Stellen) im Buchungstext.  Deckt z.B. fraenk-EREF-
            # Nummern, SHBB-RNR-Nummern und andere Fälle mit Transaktions-IDs
            # im Text ab.  Verknüpft wird der Token-Treffer nur, wenn seine
            # Wirkung passt.
            token_hit = _token_tiebreak(bank_text, candidates)
            if token_hit is not None and fits(token_hit):
                _do_link(bank_id, token_hit)
                linked += 1
                continue

            # ── Stufe 6: Text-Similarity (fehlende BelegNr) ────────────
            # Wenn mehrere Entries zum selben Datum+Betrag passen, aber
            # weder DocNr- noch Token-Tiebreak greift (z.B. Privatent-
            # nahmen ohne BelegNr), wird der Entry mit dem ähnlichsten
            # Text gewählt.  Normalisierung: Leerzeichen entfernen,
            # lowercase.  Eindeutig bester Score (> zweitbester und > 0.5)
            # wird verknüpft – wenn seine Wirkung passt.
            if len(candidates) >= 2:
                def _text_norm(s):
                    return ''.join((s or '').lower().split())
                bank_norm = _text_norm(bank_text)
                if bank_norm:
                    scored = [
                        (SequenceMatcher(None, bank_norm,
                                         _text_norm(c.text)).ratio(), c)
                        for c in candidates
                    ]
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if (scored[0][0] > scored[1][0]
                            and scored[0][0] > 0.5):
                        best = scored[0][1]
                        if fits(best):
                            _do_link(bank_id, best)
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
