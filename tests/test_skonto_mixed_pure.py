# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Skonto bei einer Rechnung mit mehreren Steuersätzen: die reinen Bausteine (Stufe 2).

Die feste Rechnung ist **M** (``M-100``): 19 % × 100,00 (Steuer 19,00, brutto 119,00) und 7 % × 50,00 (3,50 / 53,50), zusammen 172,50; das Skonto von 2 % sind 3,45.
Beträge stehen in Minor Units (0,0001 €; ein Cent sind 100).

* ``tax_groups.split_skonto``: das Skonto verteilt sich auf die Gruppen **im Verhältnis ihres Bruttos**, auf den Cent gerundet (bei genau einem halben Cent nach unten), die Gruppe mit
  dem größten Brutto (bei Gleichstand der höhere Satz) bekommt den Rest. Die Verteilung hängt nur an den Gruppen und am Skonto – nie an den Zahlungen davor.
* ``mixed_skonto_parts``: was die schließende Zahlung je Gruppe umbucht (Rest der Gruppe, Rest ihrer Steuer) und was sie als Skonto ausbucht (Anteil, Steuer darin); eine Aufteilung, die
  nicht aufgeht, ist ``ValueError`` – die Zuordnung lehnt dann mit ``SKONTO_MIXED_SPLIT`` ab.
* ``skonto_moved_by_rate``: was je Satz nach der Zahlung umgebucht ist (Brutto minus Anteil, Steuer minus Steuer des Anteils) – die Erwartung der Prüfliste.
* ``skonto_group_key`` und ``skonto_terms`` (mit den Sätzen der Gruppen): welche Sätze ein Skonto-Konto haben, die Reihenfolge der Ablehnungen, der Schlüssel (``(19, 7)``).
* ``payment_rows(skonto_parts=…)``: „Bank an Debitoren“ über Geld plus Skonto, je Satz „Wartekonto an Erlöskonto“ (ohne Forderung „Erlöskonto an Bank“), danach je Satz der Skonto-Satz.
* der Wortlaut der neuen festen Sätze.

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
from decimal import Decimal
from fractions import Fraction

import pytest

from db import invoice_payments as payments
from db.invoice_payments import (MIXED_GROUPS_MISMATCH, PaymentPart, SKONTO_GROUP_RATE, SKONTO_MIXED_SPLIT, SKONTO_NO_TERMS, SKONTO_RATE, SKONTO_REVENUE_DISCOUNT,
                                 SkontoPart, SkontoState, mixed_skonto_parts, payment_rows, skonto_group_key, skonto_moved_by_rate, skonto_open_minor,
                                 skonto_tax, skonto_terms)
from tax_groups import RateGroup, split_payment, split_skonto

D = Decimal
G19 = RateGroup(D('19'), 1000000, 190000, 1190000)             # 100,00 netto, 19,00 Steuer, 119,00 brutto
G7 = RateGroup(D('7'), 500000, 35000, 535000)                  # 50,00 / 3,50 / 53,50
G0 = RateGroup(D('0'), 1000000, 0, 1000000)                    # steuerfrei: 100,00
G55 = RateGroup(D('5.5'), 500000, 27500, 527500)               # 5,5 %: kein Skonto-Konto
GROUPS = [G19, G7]                                             # M: nach Satz absteigend, wie ``mixed_groups`` sie liefert
CENT = 100


def part(pct, amount, tax, skonto, skonto_tax):
    return payments.MixedSkontoPart(D(pct), amount, tax, skonto, skonto_tax)


# ── split_skonto ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('groups, skonto, expected', [
    pytest.param(GROUPS, 34500, [23800, 10700], id='a: 3,45 € – 7 % genau 1,07, 19 % der Rest 2,38'),
    pytest.param(GROUPS, 34400, [23700, 10700], id='b: 3,44 € – 7 %: 1,0669 → 1,07, 19 %: 2,37'),
    pytest.param(GROUPS, 34300, [23700, 10600], id='b+1: 3,43 € – 7 %: 1,0638 → 1,06, 19 %: 2,37'),
    pytest.param([G19, G7, G0], 54500, [23800, 10700, 20000], id='d: drei Gruppen, 5,45 € – 2,38 / 1,07 / 2,00'),
    pytest.param([RateGroup(D('19'), 0, 0, 1000000), RateGroup(D('7'), 0, 0, 1000000)], 300, [200, 100],
                 id='e: genau ein halber Cent wird abgerundet – die kleinere Gruppe bekommt 0,01 €, die größere 0,02 €'),
    pytest.param([G7, G19], 34500, [10700, 23800], id='die Reihenfolge der Gruppen bestimmt die Reihenfolge der Anteile'),
    pytest.param(GROUPS, 100, [100, 0], id='ein Cent: die größere Gruppe bekommt ihn (der Anteil der kleineren rundet auf 0)'),
    pytest.param([G19], 23800, [23800], id='eine Gruppe: das ganze Skonto'),
    pytest.param(GROUPS, 1725000, [1190000, 535000], id='das ganze Brutto'),
])
def test_split_skonto_examples(groups, skonto, expected):
    assert split_skonto(groups, skonto) == expected
    assert sum(expected) == skonto


def test_the_largest_group_is_the_one_with_the_largest_gross_and_on_a_tie_the_higher_rate():
    tied = [RateGroup(D('7'), 0, 0, 1000000), RateGroup(D('19'), 0, 0, 1000000)]                # gleich groß: 19 % bekommt den Rest
    assert split_skonto(tied, 300) == [100, 200]
    assert split_skonto(list(reversed(tied)), 300) == [200, 100]
    bigger_but_lower = [RateGroup(D('19'), 0, 0, 1000000), RateGroup(D('7'), 0, 0, 1000100)]    # 7 % ist größer: sie bekommt den Rest
    assert split_skonto(bigger_but_lower, 300) == [100, 200]


@pytest.mark.parametrize('groups', [GROUPS, [G19, G7, G0], [G19, G55, G7]], ids=['zwei', 'drei', 'mit-5,5'])
def test_every_share_is_the_proportional_part_rounded_half_down_and_the_largest_takes_the_rest(groups):
    """Gegenrechnung mit Brüchen über jedes Skonto von einem Cent bis zum ganzen Brutto in Schritten – ohne Gleitkomma."""
    total = sum(group.gross for group in groups)
    largest = max(range(len(groups)), key=lambda i: (groups[i].gross, groups[i].pct))
    for cents in range(1, total // CENT + 1, 37):
        shares = split_skonto(groups, cents * CENT)
        assert sum(shares) == cents * CENT and all(share >= 0 and share % CENT == 0 for share in shares), cents
        for i, group in enumerate(groups):
            if i == largest:
                continue
            exact = Fraction(group.gross * cents, total)                              # in Cent
            floor = exact.numerator // exact.denominator
            rest = exact - floor
            assert shares[i] == (floor + (1 if rest > Fraction(1, 2) else 0)) * CENT, (cents, i)


def test_the_split_does_not_depend_on_the_payments_before():
    """Dieselbe Verteilung, gleich was vorher gezahlt wurde: Sie hängt nur an den Gruppen und am Skonto (so rechnet die Prüfliste sie nach)."""
    assert split_skonto(GROUPS, 34500) == [23800, 10700]
    assert [p.skonto for p in mixed_skonto_parts(GROUPS, {}, 1690500, 34500)] == [23800, 10700]
    booked = {D('19'): (689900, 110200), D('7'): (310100, 20300)}
    assert [p.skonto for p in mixed_skonto_parts(GROUPS, booked, 690500, 34500)] == [23800, 10700]


@pytest.mark.parametrize('groups, skonto', [
    pytest.param(GROUPS, 0, id='kein Skonto'), pytest.param(GROUPS, -100, id='negativ'), pytest.param(GROUPS, 34550, id='kein ganzer Cent'),
    pytest.param(GROUPS, 1725100, id='mehr als das Brutto'), pytest.param(GROUPS, True, id='ein Wahrheitswert'), pytest.param(GROUPS, 345.0, id='eine Kommazahl'),
    pytest.param(GROUPS, '345', id='ein Text'), pytest.param(GROUPS, None, id='nichts'), pytest.param([], 100, id='keine Gruppe'),
    pytest.param([G19, RateGroup(D('19'), 0, 0, 100)], 100, id='gleiche Sätze'), pytest.param([G19, RateGroup(D('7'), 0, 0, 0)], 100, id='ein Brutto von 0'),
    pytest.param([G19, RateGroup(D('7'), 0, 0, -100)], 100, id='ein negatives Brutto'),
])
def test_split_skonto_refuses_what_it_cannot_split(groups, skonto):
    with pytest.raises(ValueError):
        split_skonto(groups, skonto)


def test_split_skonto_refuses_when_rounding_would_leave_the_largest_group_a_negative_share():
    """Fünf gleich große Gruppen, 0,03 € Skonto: Jede der vier kleineren rundet 0,6 Cent auf 1 Cent, zusammen mehr als das Skonto – der Rest der größten wäre −0,01 €. Die Verteilung rät nicht."""
    groups = [RateGroup(D(pct), 0, 0, 1000000) for pct in (19, 16, 7, 5, 0)]
    with pytest.raises(ValueError):
        split_skonto(groups, 300)
    assert sum(split_skonto(groups, 500)) == 500                                                  # ein größeres Skonto geht auf


# ── mixed_skonto_parts ───────────────────────────────────────────────────────

def test_a_closing_payment_with_the_exact_discount_books_every_group_whole_and_the_discount_on_top():
    """Beispiel a: 169,05 € gezahlt, 3,45 € Skonto. Die Zahlung schließt beide Gruppen mit ihrem ganzen Brutto und ihrer ganzen Steuer, der Skonto-Satz je Gruppe nimmt davon ab."""
    assert mixed_skonto_parts(GROUPS, {}, 1690500, 34500) == (part(19, 1190000, 190000, 23800, 3800), part(7, 535000, 35000, 10700, 700))


def test_the_tax_of_the_discount_is_rounded_once_per_group():
    parts = mixed_skonto_parts(GROUPS, {}, 1690600, 34400)                                          # b: 169,06 € gezahlt
    assert [(p.skonto, p.skonto_tax) for p in parts] == [(23700, 3800), (10700, 700)]               # 2,37 × 19/119 = 0,3784 → 0,38; 1,07 × 7/107 = 0,07
    assert [p.skonto_tax for p in parts] == [skonto_tax(23700, 19), skonto_tax(10700, 7)]
    third = mixed_skonto_parts([G19, G7, G0], {}, 2670500, 54500)                              # d: 267,00 € gezahlt, 5,45 € Skonto
    assert [(p.pct, p.skonto, p.skonto_tax) for p in third] == [(D('19'), 23800, 3800), (D('7'), 10700, 700), (D('0'), 20000, 0)]


def test_a_part_payment_before_leaves_the_rest_of_each_group_and_of_its_tax():
    """Beispiel c: erst 100,00 € (68,99 + 31,01), dann 69,05 € mit Skonto. Die schließende Zahlung nimmt den Rest jeder Gruppe samt Rest ihrer Steuer."""
    first = split_payment(GROUPS, {}, 1000000)
    assert [(p.amount, p.tax) for p in first] == [(689900, 110200), (310100, 20300)]
    booked = {p.pct: (p.amount, p.tax) for p in first}

    assert mixed_skonto_parts(GROUPS, booked, 690500, 34500) == (part(19, 500100, 79800, 23800, 3800), part(7, 224900, 14700, 10700, 700))
    total_tax = sum(p.tax for p in first) + 79800 + 14700 - 3800 - 700
    assert total_tax == 220500                                                                       # Σ Umsatzsteuer: 22,05 €


def test_the_amounts_booked_are_the_rest_of_the_invoice_the_payment_settles():
    """Geld plus Skonto ist, was die Umbuchungen tragen: Die Beträge der Teile ergeben ``alloc + skonto``, die Anteile ``skonto``."""
    parts = mixed_skonto_parts(GROUPS, {}, 1690500, 34500)
    assert sum(p.amount for p in parts) == 1690500 + 34500 and sum(p.skonto for p in parts) == 34500
    assert sum(p.amount - p.skonto for p in parts) == 1690500                                          # die Bankseite ist das Geld


def test_a_split_that_does_not_go_up_is_refused():
    """Fall f: Die Teilzahlung von 119,00 € steht (per SQL) ganz auf 19 %; für die 7 % bleibt alles offen, für 19 % nichts. Das Skonto von 3,45 € weist 19 % 2,38 € zu – bei einem Rest von 0."""
    booked = {D('19'): (1190000, 190000)}
    with pytest.raises(ValueError):
        mixed_skonto_parts(GROUPS, booked, 500500, 34500)
    with pytest.raises(ValueError):
        mixed_skonto_parts(GROUPS, booked, 524300, 10700)                                          # auch der Rest von 1,07 €: 19 % bekäme 0,74 €


def test_a_discount_tax_above_the_rest_of_the_tax_of_the_group_is_refused():
    booked = {D('19'): (100000, 189900)}                                                             # fast die ganze Steuer ist schon gebucht, der Betrag nicht
    with pytest.raises(ValueError):
        mixed_skonto_parts(GROUPS, booked, 1690500 - 100000, 34500)


def test_a_payment_that_does_not_close_the_invoice_is_refused():
    with pytest.raises(ValueError):
        mixed_skonto_parts(GROUPS, {}, 1600000, 34500)                                                # 160,00 + 3,45 sind nicht die ganzen 172,50
    with pytest.raises(ValueError):
        mixed_skonto_parts(GROUPS, {}, 1690500, 30000)                                                # 3,00 € Skonto: Rest nach der Zahlung bliebe offen


@pytest.mark.parametrize('groups', [[G19, G55], [G55, G7], [G19, G7, G55]], ids=['19+5,5', '5,5+7', 'drei'])
def test_a_group_without_a_discount_account_is_refused(groups):
    total = sum(group.gross for group in groups)
    with pytest.raises(ValueError):
        mixed_skonto_parts(groups, {}, total - 10000, 10000)


def test_what_split_payment_refuses_is_refused_here_too():
    with pytest.raises(ValueError):
        mixed_skonto_parts(GROUPS, {D('16'): (100, 0)}, 1690500, 34500)                              # ein Satz, den der Beleg nicht trägt
    with pytest.raises(ValueError):
        mixed_skonto_parts(GROUPS, {}, 0, 34500)                                                      # kein Geld
    with pytest.raises(ValueError):
        mixed_skonto_parts(GROUPS, {}, 1690500, 0)                                                    # kein Skonto


# ── skonto_moved_by_rate ─────────────────────────────────────────────────────

def test_skonto_moved_by_rate_is_what_each_rate_carries_after_the_payment():
    assert skonto_moved_by_rate(GROUPS, 34500) == {D('19'): (1166200, 186200), D('7'): (524300, 34300)}
    assert skonto_moved_by_rate([G19, G7, G0], 54500) == {D('19'): (1166200, 186200), D('7'): (524300, 34300), D('0'): (980000, 0)}


def test_without_a_discount_it_is_the_gross_and_the_tax_of_each_group():
    assert skonto_moved_by_rate(GROUPS, 0) == {D('19'): (1190000, 190000), D('7'): (535000, 35000)}
    assert skonto_moved_by_rate([G19, G55], 0) == {D('19'): (1190000, 190000), D('5.5'): (527500, 27500)}      # ohne Skonto gibt es keine Bedingung an den Satz


def test_it_is_what_mixed_skonto_parts_books_net_of_the_discount():
    for booked, alloc in (({}, 1690500), ({D('19'): (689900, 110200), D('7'): (310100, 20300)}, 690500)):
        parts = mixed_skonto_parts(GROUPS, booked, alloc, 34500)
        booked_total = {p.pct: (p.amount - p.skonto, p.tax - p.skonto_tax) for p in parts}
        for pct, (amount, tax) in booked.items():
            booked_total[pct] = (booked_total[pct][0] + amount, booked_total[pct][1] + tax)
        assert booked_total == skonto_moved_by_rate(GROUPS, 34500)


@pytest.mark.parametrize('groups, skonto', [
    pytest.param([G19, G55], 10000, id='5,5 % hat kein Skonto-Konto'), pytest.param(GROUPS, 34550, id='kein ganzer Cent'), pytest.param(GROUPS, -100, id='negativ'),
    pytest.param(GROUPS, 1725100, id='mehr als das Brutto'),
])
def test_skonto_moved_by_rate_refuses_what_it_cannot_split(groups, skonto):
    with pytest.raises(ValueError):
        skonto_moved_by_rate(groups, skonto)


# ── skonto_group_key ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('pct, expected', [
    (D('19'), 19), (D('19.00'), 19), (D('7'), 7), (D('0'), 0), (D('0.00'), 0), (19, 19), (7, 7), (0, 0),
    (D('5.5'), None), (D('16'), None), (D('5'), None), (D('1'), None), (D('100'), None), (D('18.99'), None), (D('7.01'), None), (D('19.5'), None),
])
def test_skonto_group_key(pct, expected):
    key = skonto_group_key(pct)
    assert key == expected and (key is None or type(key) is int)


# ── skonto_terms und skonto_open_minor mit den Sätzen der Gruppen ────────────

def _terms(**over):
    values = dict(gross=1725000, days=14, percent=2.0, tax_rate=0.19, mixed=1, group_pcts=(D('19'), D('7')), revenue_on_discount=False)
    values.update(over)
    return skonto_terms(**values)


def test_the_key_of_a_rate_per_position_is_the_tuple_of_the_keys_of_its_groups():
    assert _terms() == (None, ((19, 7), 34500))
    assert _terms(group_pcts=(D('19'), D('7'), D('0'))) == (None, ((19, 7, 0), 34500))
    assert _terms(group_pcts=(D('7'), D('0'))) == (None, ((7, 0), 34500))
    assert _terms(group_pcts=(D('19'),)) == (None, ((19,), 34500))
    assert _terms(percent='2.50', days='7') == (None, ((19, 7), 43100))                            # 2,5 % von 172,50 € = 4,3125 € → 4,31 €


def test_the_key_of_an_invoice_with_one_rate_stays_the_number():
    assert skonto_terms(1190000, 14, 2.0, 0.19, None) == (None, (19, 23800))
    assert skonto_terms(1190000, 14, 2.0, 0.19, None, None, False) == (None, (19, 23800))
    assert skonto_terms(1000000, 14, 2.0, 0, None) == (None, (0, 20000))
    assert skonto_terms(1000000, 14, 2.0, -1, None) == (None, (0, 20000))


def test_the_head_rate_of_a_rate_per_position_is_only_a_placeholder():
    """Der Kopfsatz ist dort der höchste Satz als Platzhalter – gelesen wird er nie: Auch 16 % im Kopf lehnt nicht ab, wenn die Gruppen ein Skonto-Konto haben."""
    assert _terms(tax_rate=0.16) == (None, ((19, 7), 34500))
    assert _terms(tax_rate=None) == (None, ((19, 7), 34500))


@pytest.mark.parametrize('pcts, shown', [
    ((D('19'), D('5.5')), '5,5'), ((D('5.5'), D('7')), '5,5'), ((D('19'), D('16'), D('7')), '16'), ((D('16'), D('5')), '16'), ((D('19'), D('5'), D('0')), '5'),
    ((D('19'), D('7.4')), '7,4'),
])
def test_a_group_without_a_discount_account_names_its_rate(pcts, shown):
    assert _terms(group_pcts=pcts) == (SKONTO_GROUP_RATE.format(rate=shown), None)


def test_the_refusals_come_in_their_order():
    # 1. keine oder unlesbare Bedingungen – vor allem anderen: Eine Rechnung ohne Skonto sagt nichts über Sätze, Gruppen oder Skonto-Konten
    assert _terms(days=None, percent=None) == (SKONTO_NO_TERMS, None)
    assert skonto_terms(1190000, None, None, 0.19, None) == (SKONTO_NO_TERMS, None)
    assert _terms(group_pcts=None, revenue_on_discount=True, days=None, percent=None) == (SKONTO_NO_TERMS, None)
    assert _terms(group_pcts=(D('19'), D('16'), D('5.5')), revenue_on_discount=True, days=None, percent=None) == (SKONTO_NO_TERMS, None)
    assert skonto_terms(1190000, None, None, 0.16, None, None, True) == (SKONTO_NO_TERMS, None)
    assert _terms(revenue_on_discount=True, days=14, percent=None) == (SKONTO_NO_TERMS, None)
    # 2. die Gruppen sind nicht lesbar – vor den Sätzen und dem Erlöskonto
    assert _terms(group_pcts=None, revenue_on_discount=True) == (MIXED_GROUPS_MISMATCH, None)
    # 3. der erste Satz (absteigend) ohne Skonto-Konto – vor dem Erlöskonto
    assert _terms(group_pcts=(D('19'), D('16'), D('5.5')), revenue_on_discount=True) == (SKONTO_GROUP_RATE.format(rate='16'), None)
    # 4. ein Beleg mit einem Satz: sein Kopfsatz ohne Skonto-Konto – vor dem Erlöskonto
    assert skonto_terms(1190000, 14, 2.0, 0.16, None, None, True) == (SKONTO_RATE, None)
    # 5. ein Erlöskonto, das selbst ein Skonto-Konto ist – bei beiden Arten von Belegen
    assert _terms(revenue_on_discount=True) == (SKONTO_REVENUE_DISCOUNT, None)
    assert skonto_terms(1190000, 14, 2.0, 0.19, None, None, True) == (SKONTO_REVENUE_DISCOUNT, None)


@pytest.mark.parametrize('mixed', [None, 0, True, 1.0, '1', 2])
def test_only_the_whole_number_one_marks_a_rate_per_position_here_too(mixed):
    """Streng gelesen (``has_mixed_rates``): Alles andere ist ein Beleg mit einem Satz und nimmt den Kopfsatz – ``group_pcts`` zählt dann nicht."""
    assert _terms(mixed=mixed, group_pcts=(D('5.5'),)) == (None, (19, 34500))


def test_skonto_open_minor_passes_the_groups_through():
    assert skonto_open_minor(1725000, 1725000, 14, 2.0, 0.19, 1, (D('19'), D('7'))) == 1725000 - 34500
    assert skonto_open_minor(1725000, 1725000, 14, 2.0, 0.19, 1, (D('19'), D('7')), False) == 1690500
    assert skonto_open_minor(1725000, 1725000, 14, 2.0, 0.19, 1, (D('19'), D('5.5'))) is None
    assert skonto_open_minor(1725000, 1725000, 14, 2.0, 0.19, 1, None) is None
    assert skonto_open_minor(1725000, 1725000, 14, 2.0, 0.19, 1, (D('19'), D('7')), True) is None
    assert skonto_open_minor(1190000, 1190000, 14, 2.0, 0.19, None, None, True) is None
    assert skonto_open_minor(1190000, 1190000, 14, 2.0, 0.19, None) == 1166200


# ── SkontoState ──────────────────────────────────────────────────────────────

def test_the_state_carries_the_parts_only_for_a_rate_per_position():
    assert SkontoState(19, 23800, 23800).parts is None
    assert SkontoState((19, 7), 34500, 34500, ()).parts == ()
    assert SkontoState._fields == ('rate_key', 'limit', 'amount', 'parts')


# ── payment_rows(skonto_parts=…) ─────────────────────────────────────────────

BASE = dict(number='M-100', customer_id=7, bank_id=11, bank_date='2025-04-10', bank_tax_date='2025-04-11', recipient='Käufer AG', currency='EUR', amount=1690500,
            tax_rate=None, tax_value=None, bank_coa=100, revenue_coa=None, debtor_side=300)
PARTS = [PaymentPart(201, 401, 1190000, 0.19, 190000), PaymentPart(202, 402, 535000, 0.07, 35000)]
DISCOUNTS = [SkontoPart(501, 23800, 0.19, 3800), SkontoPart(502, 10700, 0.07, 700)]


def _values(rows):
    return [(row.values['EntryForm'], row.values['COA_ID'], row.values['CounterCOA_ID'], row.values['Amount'], row.values['TaxRate'], row.values['TaxAmount'], row.values['Text'])
            for row in rows]


def test_payment_rows_with_discounts_per_rate_and_a_receivable():
    rows = payment_rows(**BASE, parts=PARTS, skonto_parts=DISCOUNTS)

    assert _values(rows) == [
        ('liquid_first', 100, 300, 1725000, None, None, 'Zahlung zu Re. M-100'),                  # Bank an Debitoren: Geld plus Skonto
        ('transfer', 201, 401, 1190000, 0.19, 190000, 'Umb. zu Re. M-100'),                       # je Satz Wartekonto an Erlöskonto
        ('transfer', 202, 402, 535000, 0.07, 35000, 'Umb. zu Re. M-100'),
        ('purpose_first', 501, 100, -23800, 0.19, -3800, 'Skonto zu Re. M-100'),                  # danach je Satz der Skonto-Satz: Skonto-Konto an Bank
        ('purpose_first', 502, 100, -10700, 0.07, -700, 'Skonto zu Re. M-100')]
    assert [row.description for row in rows] == ['Payment child (bank to debtor)', 'Payment child (open item to revenue)', 'Payment child (open item to revenue)',
                                                 'Payment child (discount to bank)', 'Payment child (discount to bank)']
    for row in rows:                                                                              # alle markiert, alle mit den Angaben der Bewegung
        assert (row.values['Status'], row.values['DocumentNumber'], row.values['ParentBooking_ID'], row.values['DateBooking'], row.values['DateTax']) == (
            payments.PAYMENT_MARK, 'M-100', 11, '2025-04-10', '2025-04-11')
        assert (row.values['Contact_ID'], row.values['Currency'], row.values['RecipientClient']) == (7, 'EUR', 'Käufer AG')
    bank_side = rows[0].values['Amount'] + sum(row.values['Amount'] for row in rows[3:])
    assert bank_side == 1690500                                                                    # die Bankseite ist genau das Geld


def test_payment_rows_with_discounts_per_rate_without_a_receivable():
    parts = [PaymentPart(None, 401, 1190000, 0.19, 190000), PaymentPart(None, 402, 535000, 0.07, 35000)]
    rows = payment_rows(**{**BASE, 'debtor_side': None}, parts=parts, skonto_parts=DISCOUNTS)

    assert _values(rows) == [
        ('purpose_first', 401, 100, 1190000, 0.19, 190000, 'Zahlung zu Re. M-100'),               # je Satz Erlöskonto an Bank
        ('purpose_first', 402, 100, 535000, 0.07, 35000, 'Zahlung zu Re. M-100'),
        ('purpose_first', 501, 100, -23800, 0.19, -3800, 'Skonto zu Re. M-100'),
        ('purpose_first', 502, 100, -10700, 0.07, -700, 'Skonto zu Re. M-100')]
    assert [row.description for row in rows] == ['Payment child (revenue to bank)'] * 2 + ['Payment child (discount to bank)'] * 2


def test_a_group_without_tax_has_a_discount_row_without_rate_and_tax():
    parts = PARTS + [PaymentPart(203, 403, 1000000, None, None)]
    discounts = DISCOUNTS + [SkontoPart(503, 20000, None, None)]
    rows = payment_rows(**{**BASE, 'amount': 2670500}, parts=parts, skonto_parts=discounts)

    assert _values(rows)[3] == ('transfer', 203, 403, 1000000, None, None, 'Umb. zu Re. M-100')
    assert _values(rows)[-1] == ('purpose_first', 503, 100, -20000, None, None, 'Skonto zu Re. M-100')


def test_without_discounts_the_rows_are_what_they_always_were():
    plain = payment_rows(**{**BASE, 'amount': 1725000}, parts=PARTS)

    assert _values(plain) == [('liquid_first', 100, 300, 1725000, None, None, 'Zahlung zu Re. M-100'),
                              ('transfer', 201, 401, 1190000, 0.19, 190000, 'Umb. zu Re. M-100'),
                              ('transfer', 202, 402, 535000, 0.07, 35000, 'Umb. zu Re. M-100')]
    assert payment_rows(**{**BASE, 'amount': 1725000}, parts=PARTS, skonto_parts=None) == plain
    assert payment_rows(**{**BASE, 'amount': 1725000}, parts=PARTS, skonto=None) == plain


@pytest.mark.parametrize('case', ['ohne-Teile', 'beides', 'Erstattung', 'ohne-Betrag', 'Summe', 'Skonto-ohne-Betrag', 'negatives-Skonto'])
def test_payment_rows_refuse_a_discount_per_rate_where_there_cannot_be_one(case):
    kwargs = dict(BASE, parts=PARTS, skonto_parts=DISCOUNTS)
    if case == 'ohne-Teile':
        kwargs.update(parts=None, tax_rate=0.19, tax_value=190000, revenue_coa=400, waiting_side=200)
    elif case == 'beides':
        kwargs.update(skonto=SkontoPart(500, 23800, 0.19, 3800))
    elif case == 'Erstattung':
        kwargs.update(amount=-1690500, parts=[PaymentPart(201, 401, -1190000, 0.19, -190000), PaymentPart(202, 402, -535000, 0.07, -35000)])
    elif case == 'ohne-Betrag':
        kwargs.update(amount=0)
    elif case == 'Summe':
        kwargs.update(amount=1725000)                                                              # die Teile ergeben Geld plus Skonto (172,50), nicht 172,50 plus Skonto
    elif case == 'Skonto-ohne-Betrag':
        kwargs.update(skonto_parts=[SkontoPart(501, 0, 0.19, 0), DISCOUNTS[1]])
    elif case == 'negatives-Skonto':
        kwargs.update(skonto_parts=[SkontoPart(501, -23800, 0.19, -3800), DISCOUNTS[1]])
    with pytest.raises(ValueError):
        payment_rows(**kwargs)


def test_the_single_discount_still_refuses_a_rate_per_position():
    with pytest.raises(ValueError):
        payment_rows(**{**BASE, 'amount': 1690500}, parts=PARTS, skonto=SkontoPart(500, 34500, 0.19, 3800))


# ── Die festen Sätze ─────────────────────────────────────────────────────────

def test_the_new_fixed_sentences():
    assert SKONTO_GROUP_RATE == 'Skonto lässt sich für {rate} % dieser Rechnung noch nicht ausbuchen.'
    assert SKONTO_REVENUE_DISCOUNT == 'Skonto lässt sich nicht ausbuchen: Ein Erlöskonto dieser Rechnung ist selbst ein Skonto-Konto.'
    assert SKONTO_MIXED_SPLIT == 'Das Skonto lässt sich nicht auf die Steuersätze dieser Rechnung aufteilen – die bisherigen Zahlungen lassen es nicht zu.'
    assert SKONTO_GROUP_RATE.format(rate='5,5') == 'Skonto lässt sich für 5,5 % dieser Rechnung noch nicht ausbuchen.'


def test_the_sentences_of_stage_one_that_no_place_delivers_any_more_are_gone():
    assert not hasattr(payments, 'SKONTO_MIXED') and not hasattr(payments, 'SKONTO_TAX_ENTRY')
    assert payments.SKONTO_RATE == 'Skonto lässt sich für den Steuersatz dieser Rechnung noch nicht ausbuchen.'


def test_the_new_sentences_are_short_and_carry_no_placeholder_but_their_own():
    for text in (SKONTO_REVENUE_DISCOUNT, SKONTO_MIXED_SPLIT):
        assert text.endswith('.') and '{' not in text and len(text) < 160, text
    assert SKONTO_GROUP_RATE.endswith('.') and SKONTO_GROUP_RATE.count('{') == 1 and len(SKONTO_GROUP_RATE.format(rate='16')) < 160
