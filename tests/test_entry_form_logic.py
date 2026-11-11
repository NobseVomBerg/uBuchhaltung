# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die gespeicherte Form eines Buchungssatzes (``Bookings.EntryForm``): die reinen Funktionen.

Sechs Formen – Zweckkonto vorne, Geldkonto vorne, Umbuchung und drei Spiegel (Betrag auf dem Konto,
auf dem Gegenkonto, unbekannt) – und was die Auswertungen daraus lesen: welche Seite bei positivem
Betrag im Soll steht, was ein Satz auf dem Konto seiner Bankbewegung bewegt, wann er als Spiegel
ausgeblendet wird. Kontonummern sind hier frei erfundene Zahlen: 10 und 11 sind Geldkonten, 20 ein
Zweckkonto, 30 ein weiteres.
"""
from itertools import product

import pytest

from db.matching import (
    ENTRY_EMPTY, ENTRY_FORMS, ENTRY_LIQUID_FIRST, ENTRY_MIRROR, ENTRY_PURPOSE_FIRST,
    ENTRY_TRANSFER, FORM_LIQUID_FIRST, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY,
    FORM_PURPOSE_FIRST, FORM_TRANSFER, MIRROR_FORMS, SIDE_COA, SIDE_COUNTER, bank_effect,
    classify_entry, derive_entry_form, import_entry_form, is_asset_transfer, is_bank_effective,
    is_mirror_row, purpose_in_counter, read_entry_form, reform, soll_side,
)

GELD, GELD2, ZWECK, ZWECK2 = 10, 11, 20, 30
MONEY = {GELD, GELD2}
ROLLEN = {'leer': None, 'geld': GELD, 'zweck': ZWECK}


def test_there_are_six_distinct_forms():
    assert len(set(ENTRY_FORMS)) == 6
    assert set(MIRROR_FORMS) == {FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY}
    assert set(MIRROR_FORMS) < set(ENTRY_FORMS)


def test_the_three_payment_forms_share_their_names_with_the_classification():
    assert (FORM_PURPOSE_FIRST, FORM_LIQUID_FIRST, FORM_TRANSFER) == \
        (ENTRY_PURPOSE_FIRST, ENTRY_LIQUID_FIRST, ENTRY_TRANSFER)


@pytest.mark.parametrize('form, side', [
    (FORM_PURPOSE_FIRST, SIDE_COUNTER), (FORM_LIQUID_FIRST, SIDE_COA), (FORM_TRANSFER, SIDE_COA),
    (FORM_MIRROR_COA, SIDE_COA), (FORM_MIRROR_COUNTER, SIDE_COUNTER),
    (FORM_MIRROR_LEGACY, SIDE_COUNTER),
])
def test_soll_side_is_the_side_that_stands_in_the_debit_for_a_positive_amount(form, side):
    assert soll_side(form) == side


# ── derive_entry_form ────────────────────────────────────────────────────────

ABGELEITET = {
    ('leer', 'leer'): FORM_PURPOSE_FIRST,            # nichts kontiert: die EÜR bucht nichts
    ('leer', 'geld'): FORM_PURPOSE_FIRST,
    ('leer', 'zweck'): FORM_LIQUID_FIRST,
    ('geld', 'leer'): FORM_LIQUID_FIRST,
    ('geld', 'zweck'): FORM_LIQUID_FIRST,
    ('zweck', 'leer'): FORM_PURPOSE_FIRST,
    ('zweck', 'geld'): FORM_PURPOSE_FIRST,
    ('zweck', 'zweck'): FORM_TRANSFER,
}


@pytest.mark.parametrize('coa, counter', sorted(ABGELEITET))
@pytest.mark.parametrize('side', [SIDE_COA, SIDE_COUNTER])
def test_derive_follows_the_classification_for_everything_but_the_mirror(coa, counter, side):
    assert derive_entry_form(ROLLEN[coa], ROLLEN[counter], MONEY, side) == ABGELEITET[(coa, counter)]


def test_derive_gives_a_mirror_the_form_of_the_side_the_amount_refers_to():
    assert derive_entry_form(GELD, GELD2, MONEY, SIDE_COA) == FORM_MIRROR_COA
    assert derive_entry_form(GELD, GELD2, MONEY, SIDE_COUNTER) == FORM_MIRROR_COUNTER


def test_derive_never_returns_the_empty_kind():
    results = {derive_entry_form(coa, counter, MONEY, side)
               for coa, counter, side in product([None, GELD, ZWECK], [None, GELD, ZWECK],
                                                 [SIDE_COA, SIDE_COUNTER])}
    assert results <= set(ENTRY_FORMS)
    assert ENTRY_EMPTY not in results and ENTRY_MIRROR not in results


# ── read_entry_form ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('form', ENTRY_FORMS)
@pytest.mark.parametrize('money', [set(), MONEY, {ZWECK}])
def test_a_stored_form_is_read_as_stored_whatever_the_account_list_says(form, money):
    assert read_entry_form(form, GELD, ZWECK, money) == form


@pytest.mark.parametrize('coa, counter', sorted(ABGELEITET))
def test_without_a_stored_form_the_classification_of_today_decides(coa, counter):
    assert read_entry_form(None, ROLLEN[coa], ROLLEN[counter], MONEY) == ABGELEITET[(coa, counter)]


def test_a_mirror_without_a_stored_form_reads_as_legacy_mirror():
    assert classify_entry(GELD, GELD2, MONEY) == ENTRY_MIRROR
    assert read_entry_form(None, GELD, GELD2, MONEY) == FORM_MIRROR_LEGACY


@pytest.mark.parametrize('garbage', ['', 'mirror', 'EMPTY', 'purpose', 0])
def test_an_unknown_stored_value_counts_as_no_form(garbage):
    assert read_entry_form(garbage, ZWECK, GELD, MONEY) == FORM_PURPOSE_FIRST
    assert read_entry_form(garbage, GELD, ZWECK, set()) == FORM_TRANSFER   # heute: kein Geldkonto


# ── reform ───────────────────────────────────────────────────────────────────
# reform(alte Form, altes Konto, altes Gegenkonto, neues Konto, neues Gegenkonto, Geldkonten, Betrag)

def test_reform_keeps_the_form_and_the_amount_when_the_accounts_still_fit():
    assert reform(FORM_PURPOSE_FIRST, ZWECK, GELD, ZWECK2, GELD, MONEY, -5000) == \
        (FORM_PURPOSE_FIRST, -5000)
    assert reform(FORM_LIQUID_FIRST, GELD, ZWECK, GELD, ZWECK2, MONEY, 5000) == \
        (FORM_LIQUID_FIRST, 5000)
    assert reform(FORM_TRANSFER, ZWECK, ZWECK2, ZWECK2, ZWECK, MONEY, 5000) == \
        (FORM_TRANSFER, 5000)


def test_reform_turns_a_purpose_first_into_a_transfer_and_negates_the_amount():
    """Aufwand an Bank (−50: Aufwandskonto im Soll) wird zur Umbuchung zwischen zwei
    Zweckkonten. Die Umbuchung zählt positiv als „Konto im Soll“ – damit der Aufwand im Soll
    bleibt, kippt der Betrag."""
    assert reform(FORM_PURPOSE_FIRST, ZWECK, GELD, ZWECK, ZWECK2, MONEY, -5000) == \
        (FORM_TRANSFER, 5000)


def test_reform_turns_a_transfer_into_a_purpose_first_and_negates_the_amount():
    assert reform(FORM_TRANSFER, ZWECK, ZWECK2, ZWECK, GELD, MONEY, 5000) == \
        (FORM_PURPOSE_FIRST, -5000)


def test_reform_keeps_the_amount_when_the_second_account_is_taken_away():
    assert reform(FORM_LIQUID_FIRST, GELD, ZWECK, GELD, None, MONEY, 5000) == \
        (FORM_LIQUID_FIRST, 5000)
    assert reform(FORM_PURPOSE_FIRST, ZWECK, GELD, ZWECK, None, MONEY, -5000) == \
        (FORM_PURPOSE_FIRST, -5000)


@pytest.mark.parametrize('old', MIRROR_FORMS)
def test_reform_leaves_a_mirror_as_it_was_while_both_sides_are_money_accounts(old):
    """Dass sich ein Konto ändert, sagt nichts über die Lesart des Betrags – auch ein
    ``mirror_legacy`` wird nicht still zu einer Konvention erklärt."""
    assert reform(old, GELD, GELD2, GELD2, GELD, MONEY, 7000) == (old, 7000)


def test_reform_turns_a_mirror_into_a_payment_when_one_side_stops_being_money():
    assert reform(FORM_MIRROR_COA, GELD, GELD2, GELD, ZWECK, MONEY, 7000) == \
        (FORM_LIQUID_FIRST, 7000)
    assert reform(FORM_MIRROR_COUNTER, GELD2, GELD, ZWECK, GELD, MONEY, 7000) == \
        (FORM_PURPOSE_FIRST, 7000)
    # Betrag auf dem Konto, das Geldkonto steht aber hinten: die Seite kippt
    assert reform(FORM_MIRROR_COA, GELD2, GELD, ZWECK, GELD, MONEY, 7000) == \
        (FORM_PURPOSE_FIRST, -7000)
    assert reform(FORM_MIRROR_LEGACY, GELD, GELD2, GELD, ZWECK, MONEY, 7000) == \
        (FORM_LIQUID_FIRST, -7000)


def test_reform_accepts_no_amount():
    assert reform(FORM_PURPOSE_FIRST, ZWECK, GELD, ZWECK, ZWECK2, MONEY, None) == \
        (FORM_TRANSFER, None)


def test_reform_keeps_the_role_of_a_side_that_does_not_change_whatever_the_list_says_today():
    """Der Satz wurde mit dem Geldkonto vorne geschrieben (``liquid_first``). Das Konto vorne
    ist heute keines mehr (nicht in ``money_ids``) – bleibt es stehen und ändert sich nur das
    Gegenkonto, ist es weiter das Geldkonto vorne."""
    assert reform(FORM_LIQUID_FIRST, GELD, ZWECK, GELD, ZWECK2, set(), 5000) == \
        (FORM_LIQUID_FIRST, 5000)
    assert reform(FORM_PURPOSE_FIRST, ZWECK, GELD, ZWECK2, GELD, set(), -5000) == \
        (FORM_PURPOSE_FIRST, -5000)
    # die Seite, die sich ändert, wird dagegen mit der Liste von heute eingeordnet
    assert reform(FORM_LIQUID_FIRST, GELD, ZWECK, GELD, GELD2, {GELD2}, 5000) == \
        (FORM_MIRROR_COA, 5000)


# ── is_mirror_row ────────────────────────────────────────────────────────────

BANK_IDS = {GELD, GELD2}
VERRECHNUNG = 12             # ein Zahlungskonto, das kein eigenes Bank-/Kassenkonto ist (1460)


@pytest.mark.parametrize('stored', [FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY, None])
def test_a_mirror_is_hidden_when_both_sides_are_own_bank_accounts_whatever_its_reading(stored):
    """Wie in 2.0.x: beide Seiten eigene Bank-/Kassenkonten (``get_bank_coa_ids``) – nicht die Form."""
    assert is_mirror_row(stored, GELD, GELD2, BANK_IDS)
    assert not is_mirror_row(stored, GELD, ZWECK, BANK_IDS)
    assert not is_mirror_row(stored, GELD, GELD2, {GELD})      # GELD2 ist (noch) kein eigenes Konto


@pytest.mark.parametrize('stored', [FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY, None])
def test_a_mirror_with_the_clearing_account_stays_visible_and_editable(stored):
    assert not is_mirror_row(stored, VERRECHNUNG, GELD, BANK_IDS)
    assert not is_mirror_row(stored, GELD, VERRECHNUNG, BANK_IDS)


@pytest.mark.parametrize('stored', [FORM_PURPOSE_FIRST, FORM_LIQUID_FIRST, FORM_TRANSFER])
def test_a_payment_form_is_never_a_mirror_even_if_the_accounts_look_like_one_today(stored):
    assert not is_mirror_row(stored, GELD, GELD2, BANK_IDS)


# ── purpose_in_counter ───────────────────────────────────────────────────────

@pytest.mark.parametrize('coa, counter', [(GELD, ZWECK), (ZWECK, GELD), (ZWECK, ZWECK2), (GELD, GELD2),
                                          (VERRECHNUNG, ZWECK), (GELD, VERRECHNUNG), (None, ZWECK),
                                          (GELD, None)])
def test_the_form_decides_where_the_purpose_account_stands_for_the_payment_forms(coa, counter):
    assert purpose_in_counter(FORM_LIQUID_FIRST, coa, counter, BANK_IDS) == (coa is not None)
    assert not purpose_in_counter(FORM_PURPOSE_FIRST, coa, counter, BANK_IDS)
    assert not purpose_in_counter(FORM_TRANSFER, coa, counter, BANK_IDS)


@pytest.mark.parametrize('stored', [FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY, None])
@pytest.mark.parametrize('coa, counter, expected', [
    (GELD, VERRECHNUNG, True),       # Bank an 1460: das Zweckkonto ist 1460, nicht die Bank
    (GELD, ZWECK, True),
    (VERRECHNUNG, GELD, False),      # 1460 an Bank: das Zweckkonto steht vorne
    (ZWECK, GELD, False),
    (GELD, GELD2, False),            # zwei eigene Konten: ein ausgeblendeter Spiegel
    (VERRECHNUNG, ZWECK, False),
    (GELD, None, True),
])
def test_a_mirror_and_a_row_without_a_form_read_as_in_2_0_x(stored, coa, counter, expected):
    """Das Konto ist ein eigenes Bankkonto und das Gegenkonto nicht: Dann ist das Gegenkonto das
    Zweckkonto – so, wie der Editor es in 2.0.x zeigte (und der Satz beim Speichern unverändert blieb)."""
    assert purpose_in_counter(stored, coa, counter, BANK_IDS) is expected


# ── bank_effect ──────────────────────────────────────────────────────────────

PARENT = {GELD}          # das Konto der Bankbewegung
OTHER_PARENT = {99}


@pytest.mark.parametrize('stored, coa, counter, expected', [
    # mirror_coa: der Betrag steht auf dem Konto (positiv = Konto im Soll)
    (FORM_MIRROR_COA, GELD, GELD2, 100),             # Konto der Bank vorne: dieselbe Richtung
    (FORM_MIRROR_COA, GELD2, GELD, -100),            # Konto der Bank hinten: Gegenteil
    (FORM_MIRROR_COA, GELD2, 12, 0),                 # berührt das Konto der Bank nicht
    # mirror_counter: der Betrag steht auf dem Gegenkonto (so schreibt ihn die Buchungsmaske)
    (FORM_MIRROR_COUNTER, GELD2, GELD, 100),
    (FORM_MIRROR_COUNTER, GELD, GELD2, -100),
    (FORM_MIRROR_COUNTER, GELD2, 12, 0),
])
def test_a_mirror_acts_on_the_bank_account_by_its_position(stored, coa, counter, expected):
    assert bank_effect(stored, 100, coa, counter, PARENT, BANK_IDS | {12}) == expected


def test_a_mirror_whose_bank_account_is_unknown_counts_as_before():
    """Ohne SKR-Nummer der Bankbewegung lässt sich keine Position bestimmen: ``is_bank_effective``."""
    assert bank_effect(FORM_MIRROR_COA, 100, GELD, GELD2, None, BANK_IDS) == 100
    assert bank_effect(FORM_MIRROR_COA, 100, GELD, GELD2, set(), BANK_IDS) == 100
    assert bank_effect(FORM_MIRROR_COUNTER, 100, ZWECK, ZWECK2, None, BANK_IDS) == 0


@pytest.mark.parametrize('stored', [FORM_PURPOSE_FIRST, FORM_LIQUID_FIRST, FORM_TRANSFER,
                                    FORM_MIRROR_LEGACY, None])
@pytest.mark.parametrize('coa, counter', sorted(set(product([None, GELD, GELD2, ZWECK],
                                                           [None, GELD, GELD2, ZWECK]))
                                                - {(None, None)}, key=str))
def test_everything_else_counts_the_amount_when_the_row_touches_an_own_bank_account(
        stored, coa, counter):
    expected = 100 if is_bank_effective(coa, counter, BANK_IDS) else 0
    assert bank_effect(stored, 100, coa, counter, PARENT, BANK_IDS) == expected


def test_bank_effect_takes_the_parent_into_account_only_for_a_mirror():
    for stored in (FORM_PURPOSE_FIRST, FORM_MIRROR_LEGACY, None):
        assert bank_effect(stored, 100, GELD, ZWECK, OTHER_PARENT, BANK_IDS) == 100


def test_bank_effect_treats_no_amount_as_zero():
    assert bank_effect(FORM_PURPOSE_FIRST, None, ZWECK, GELD, PARENT, BANK_IDS) == 0
    assert bank_effect(FORM_MIRROR_COA, None, GELD, GELD2, PARENT, BANK_IDS) == 0


# ── import_entry_form ────────────────────────────────────────────────────────

@pytest.mark.parametrize('coa_money, counter_money, coa, counter, expected', [
    (True, True, GELD, GELD2, FORM_MIRROR_COA),          # Spiegel: der Rohbetrag, positiv = Konto im Soll
    (True, False, GELD, ZWECK, FORM_LIQUID_FIRST),
    (False, True, ZWECK, GELD, FORM_PURPOSE_FIRST),
    # keine Seite ein Zahlungskonto: WISO meint „Konto im Soll“ – auch bei leerer oder unbekannter Seite
    (False, False, ZWECK, ZWECK2, FORM_TRANSFER),
    (False, False, ZWECK, None, FORM_TRANSFER),
    (False, False, None, ZWECK, FORM_TRANSFER),
    (False, False, None, None, FORM_TRANSFER),
    (False, True, None, GELD, FORM_PURPOSE_FIRST),
    # ein Geldkonto, das im Kontenrahmen fehlt (kein Konto), zählt nicht als Seite
    (True, False, None, ZWECK, FORM_LIQUID_FIRST),
    (True, True, None, GELD, FORM_PURPOSE_FIRST),
])
def test_the_import_decides_which_side_is_money_not_the_account_list(
        coa_money, counter_money, coa, counter, expected):
    assert import_entry_form(coa, coa_money, counter, counter_money) == expected


# ── is_asset_transfer ────────────────────────────────────────────────────────

ASSET, EXPENSE = {1}, {2}


def test_is_asset_transfer_reads_a_given_form_instead_of_classifying():
    assert is_asset_transfer(2, 1, set(), ASSET, EXPENSE, form=FORM_TRANSFER)
    # dieselben Konten, aber der Satz wurde als Zahlung geschrieben (Geldkonto war eines von beiden)
    assert not is_asset_transfer(2, 1, set(), ASSET, EXPENSE, form=FORM_PURPOSE_FIRST)
    assert not is_asset_transfer(2, 1, set(), ASSET, EXPENSE, form=FORM_MIRROR_LEGACY)


def test_is_asset_transfer_still_classifies_without_a_form():
    assert is_asset_transfer(2, 1, set(), ASSET, EXPENSE)
    assert not is_asset_transfer(2, 1, {1}, ASSET, EXPENSE)
