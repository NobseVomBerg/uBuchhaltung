# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die reinen Funktionen für Steuersätze aus den Stammdaten (``tax_groups.py``, 2.8.0, Etappe E0).

Nichts in der App ruft sie auf. Dieser Test hält fest, was sie können – und dass sich für jeden Aufruf von heute nichts ändert:

* ``group_tax``: zweistufig (``exact`` falsch, der Standard) ist die Formel von vor 2.8.0 Bit für Bit; einstufig (``exact`` wahr) ist die Regel der Norm
  (EN 16931, BR-CO-17: Basis mal Satz, einmal auf den Cent gerundet). Für jeden ganzzahligen Satz und jedes Netto in ganzen Cent sind beide gleich
  (der Beweis steht im Docstring von ``group_tax``, hier als Aufzählung und als Zufallsprobe); mit 5,5 % weicht genau jeder Betrag ab, dessen Cent-Zahl
  ≡ 9 (mod 200) ist (0,09 € → 0,00 € statt 0,01 €). Beide Wege sind spiegelbildlich;
* ``rate_groups``, ``item_groups``, ``mixed_groups``: Mit Zweier-Tupeln sind sie von der Fassung 2.7.0 nicht zu unterscheiden – Ergebnis, Darstellung der
  Sätze, Art und Text der Ablehnung (die Referenz steht in diesem Test, aus dem Code von damals kopiert). Mit einem dritten Wert rechnen sie einstufig, aber
  nur eine Gruppe, deren Positionen ihn alle tragen;
* ``parse_rate_bp``, ``bp_pct``, ``group_label``: ein Satz wird streng gelesen und genau dargestellt;
* ``check_tax_entry``: jede Regel mit genau ihrem Satz (``TAX_ENTRY_*``). Es gibt keinen Befreiungscode (Entscheidung D1 des Plans 2.8.0).

Alle Beträge, Bezeichnungen und Gründe sind erfunden.
"""
import ast
import inspect
import pathlib
import random
from decimal import ROUND_HALF_UP, Decimal

import pytest

import tax_groups
from money import round_minor, tax_from_net
from tax_groups import (EXEMPT, MAX_LABEL_LENGTH, MAX_RATE_BP, MAX_REASON_LENGTH, MIXED_TAX_RATES, STANDARD, TAX_ENTRY_EXEMPT_RATE, TAX_ENTRY_KIND,
                        TAX_ENTRY_LABEL, TAX_ENTRY_LAST_ACTIVE, TAX_ENTRY_RATE, TAX_ENTRY_REASON, TAX_ENTRY_REVENUE_NOT_INCOME,
                        TAX_ENTRY_REVENUE_REQUIRED, RateGroup, RateGroupsMismatch, bp_pct, check_tax_entry, group_label, group_tax, item_groups,
                        item_rate, item_rate_of, mixed_groups, parse_rate_bp, rate_groups, rate_pct)

D = Decimal


def cents(amount):
    """Ein Betrag in Cent als Minor Units (``9`` = 0,09 € = 900)."""
    return amount * 100


# ── Die Referenz: die Fassung 2.7.0, aus dem Code von damals kopiert ──────────────────────────────────────────────────────────────

def two_step(net, pct):
    """Die Steuer einer Gruppe vor 2.8.0: auf die Minor Unit gerundet, danach auf den Cent."""
    return round_minor(tax_from_net(net, pct), 2)


def br_co_17(net, pct):
    """BR-CO-17 der Norm, direkt in ``Decimal``: Basis (in Euro) mal Satz, auf zwei Nachkommastellen gerundet – zurück in Minor Units."""
    euro = D(net) / 10000
    return int((euro * pct / 100).quantize(D('0.01'), rounding=ROUND_HALF_UP) * 10000)


def old_rate_groups(lines):
    """``tax_groups.rate_groups`` am Stand 2.7.0."""
    net_by_rate = {}
    for pct, net in lines:
        net_by_rate[pct] = net_by_rate.get(pct, 0) + net
    groups = []
    for pct in sorted(net_by_rate, reverse=True):
        net = net_by_rate[pct]
        tax = round_minor(tax_from_net(net, pct), 2)
        groups.append(RateGroup(pct=pct, net=net, tax=tax, gross=net + tax))
    return groups


def old_item_groups(pairs, default=None):
    """``tax_groups.item_groups`` am Stand 2.7.0."""
    return old_rate_groups([(item_rate(rate, default), net) for rate, net in pairs])


def old_mixed_groups(sum_net, tax_amount, sum_gross, pairs):
    """``tax_groups.mixed_groups`` am Stand 2.7.0."""
    pairs = list(pairs)
    if not pairs:
        raise RateGroupsMismatch('Der Beleg hat keine Position.')
    for number, (rate, net) in enumerate(pairs, 1):
        if type(net) is not int:
            raise RateGroupsMismatch(f'Position {number}: Der Nettobetrag ist keine ganze Zahl.')
        try:
            pct = rate_pct(rate)
        except (ArithmeticError, ValueError):
            raise RateGroupsMismatch(f'Position {number}: Der Steuersatz ist keine Zahl.') from None
        if pct is None or not 0 <= pct <= 100:
            raise RateGroupsMismatch(f'Position {number}: Der Steuersatz fehlt oder liegt nicht zwischen 0 und 100 Prozent.')
    groups = old_item_groups(pairs)
    stored = (sum_net, tax_amount, sum_gross)
    found = (sum(group.net for group in groups), sum(group.tax for group in groups), sum(group.gross for group in groups))
    if any(type(value) is not int for value in stored) or stored != found:
        raise RateGroupsMismatch(f'Die Summen des Belegs {stored} passen nicht zu seinen Satzgruppen {found}.')
    return groups


def outcome(function, *args, **kwargs):
    """Was ein Aufruf liefert, als vergleichbarer Wert: das Ergebnis samt der Darstellung der Sätze (``repr``) oder Art und Text der Ausnahme."""
    try:
        return 'ok', repr(function(*args, **kwargs))
    except Exception as error:      # verglichen wird gerade, was geworfen wird
        return 'error', type(error).__name__, str(error)


def sums_of(groups):
    return sum(group.net for group in groups), sum(group.tax for group in groups), sum(group.gross for group in groups)


# ── group_tax ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_two_step_rounding_is_the_formula_of_before_bit_for_bit():
    chance = random.Random(280004)
    rates = [D(0), D(5), D('5.5'), D(7), D('7.4'), D('10.7'), D(16), D('19.25'), D(19), D(100)]
    limits = [0, 1, -1, 49, 50, 51, 99, 100, 101, 149, 150, 151, 9900, 9950, 10000, 10 ** 11, -(10 ** 11)]
    cases = [(net, pct) for net in limits for pct in rates]
    cases += [(chance.randint(-10 ** 9, 10 ** 9), chance.choice(rates)) for _ in range(20_000)]

    for net, pct in cases:
        assert group_tax(net, pct) == group_tax(net, pct, False) == group_tax(net, pct, exact=False) == two_step(net, pct), (net, pct)


def test_for_every_whole_rate_and_every_whole_cent_amount_from_minus_200_to_plus_200_euro_both_roundings_give_the_same_tax():
    """Der Beweis: Netto = 100·c Minor Units (ganze Cent), Satz p ganzzahlig – die exakte Steuer c·p ist eine ganze Zahl von Minor Units, der erste Schritt
    der zweistufigen Rundung rundet nichts, und beide Wege runden dieselbe Zahl einmal auf den Cent. Hier die Aufzählung."""
    different = []
    for rate in range(101):
        pct = D(rate)
        for amount in range(-20_000, 20_001):
            net = cents(amount)
            if group_tax(net, pct, True) != two_step(net, pct):
                different.append((rate, amount))

    assert not different, different[:5]


def test_random_whole_rates_and_whole_cent_amounts_up_to_10_million_euro_give_the_same_tax_in_both_roundings():
    chance = random.Random(280001)
    for _ in range(50_000):
        net = cents(chance.randint(-10 ** 9, 10 ** 9))                                              # ganze Cent, bis ±10 Mio. €
        for pct in (D(19), D(7), D(chance.randint(0, 100))):
            assert group_tax(net, pct, True) == group_tax(net, pct) == two_step(net, pct), (pct, net)


def test_with_five_and_a_half_percent_exactly_the_amounts_of_9_cents_modulo_200_differ_by_one_cent():
    """5,5 % von 0,09 € sind 0,00495 €: einstufig 0,00 €, zweistufig (erst 0,00495 → 0,0050, dann 0,0050 → 0,01) 0,01 €. Das trifft jeden 200. Cent-Betrag."""
    pct = D('5.5')
    different = {}
    for amount in range(-20_000, 20_001):
        one, two = group_tax(cents(amount), pct, True), group_tax(cents(amount), pct)
        if one != two:
            different[amount] = two - one

    assert sorted(different) == [amount for amount in range(-20_000, 20_001) if abs(amount) % 200 == 9]
    assert all(diff == (100 if amount > 0 else -100) for amount, diff in different.items())              # immer genau ein Cent, spiegelbildlich
    assert (group_tax(cents(9), pct), group_tax(cents(9), pct, True)) == (cents(1), 0)                  # 0,09 € → 0,01 € zweistufig, 0,00 € einstufig
    assert (group_tax(cents(-9), pct), group_tax(cents(-9), pct, True)) == (-cents(1), 0)
    assert (group_tax(cents(209), pct), group_tax(cents(209), pct, True)) == (cents(12), cents(11))     # 11,495 € → 12 Cent bzw. 11 Cent


def test_a_net_with_a_fraction_of_a_cent_differs_too_even_for_a_whole_rate():
    """Das Netto einer Position steht in Minor Units; ein Bruchteil eines Cents entsteht nur per SQL-Konsole. 261 Minor Units × 19 % = 49,59 Minor Units."""
    assert group_tax(261, D(19)) == 100 and group_tax(261, D(19), True) == 0


def test_the_one_step_rounding_is_rule_br_co_17_of_the_norm():
    chance = random.Random(280002)
    for _ in range(30_000):
        pct = bp_pct(chance.randint(0, MAX_RATE_BP))
        net = chance.randint(-10 ** 11, 10 ** 11)                                                    # beliebige Minor Units, auch mit Bruchteil eines Cents

        assert group_tax(net, pct, True) == br_co_17(net, pct), (pct, net)


@pytest.mark.parametrize('exact', [False, True])
def test_both_roundings_are_symmetric_so_a_credit_note_has_the_negated_tax(exact):
    chance = random.Random(280003)
    for _ in range(20_000):
        pct = bp_pct(chance.randint(0, MAX_RATE_BP))
        net = chance.randint(0, 10 ** 11)

        assert group_tax(-net, pct, exact) == -group_tax(net, pct, exact), (pct, net)


@pytest.mark.parametrize('exact', [False, True])
def test_a_half_cent_rounds_away_from_zero(exact):
    """5 % von 0,10 € sind genau 0,005 €."""
    assert group_tax(cents(10), D(5), exact) == cents(1)
    assert group_tax(cents(-10), D(5), exact) == -cents(1)
    assert group_tax(cents(30), D(5), exact) == cents(2)                                               # 1,5 Cent → 2 Cent
    assert group_tax(cents(10), D('0.5'), exact) == 0                                                   # 0,0005 € → 0,00 €


@pytest.mark.parametrize('exact', [False, True])
def test_the_tax_is_a_whole_number_of_cents_and_zero_for_nothing(exact):
    chance = random.Random(280005)
    for _ in range(5_000):
        tax = group_tax(chance.randint(-10 ** 9, 10 ** 9), bp_pct(chance.randint(0, MAX_RATE_BP)), exact)

        assert type(tax) is int and tax % 100 == 0
    assert group_tax(0, D(19), exact) == 0 and group_tax(cents(100), D(0), exact) == 0
    assert group_tax(cents(10_000), D(100), exact) == cents(10_000)


# ── rate_groups ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────

# ``7.0`` und ``19.00`` sind dieselben Sätze wie 7 und 19, nur anders geschrieben
RATES = [D(0), D('0.5'), D(5), D('5.5'), D(7), D('7.0'), D('7.4'), D('10.7'), D(16), D(19), D('19.00'), D('19.25')]


def random_lines(chance):
    return [(chance.choice(RATES), chance.randint(-5_000_000, 50_000_000)) for _ in range(chance.randint(0, 14))]


def test_with_pairs_rate_groups_is_the_one_of_2_7_0_down_to_the_way_the_rates_are_written():
    chance = random.Random(280010)
    for _ in range(1_500):
        lines = random_lines(chance)

        assert repr(rate_groups(lines)) == repr(old_rate_groups(lines)), lines
        assert repr(rate_groups(iter(lines))) == repr(old_rate_groups(lines))                             # ein Iterator ebenso
        assert repr(rate_groups([list(line) for line in lines])) == repr(old_rate_groups(lines))          # Listen statt Tupel
        assert repr(rate_groups(zip([pct for pct, _net in lines], [net for _pct, net in lines]))) == repr(old_rate_groups(lines))


def test_a_third_value_that_is_false_or_none_changes_nothing():
    chance = random.Random(280011)
    for _ in range(500):
        lines = random_lines(chance)

        assert repr(rate_groups([(pct, net, False) for pct, net in lines])) == repr(old_rate_groups(lines))
        assert repr(rate_groups([(pct, net, None) for pct, net in lines])) == repr(old_rate_groups(lines))


def test_a_group_whose_lines_are_all_exact_rounds_in_one_step_five_and_a_half_percent_of_0_09_euro_is_nothing():
    assert rate_groups([(D('5.5'), cents(9))]) == [RateGroup(D('5.5'), cents(9), cents(1), cents(10))]            # wie bisher
    assert rate_groups([(D('5.5'), cents(9), True)]) == [RateGroup(D('5.5'), cents(9), 0, cents(9))]
    assert rate_groups([(D('5.5'), cents(4), True), (D('5.5'), cents(5), True)]) == [RateGroup(D('5.5'), cents(9), 0, cents(9))]


def test_one_line_that_is_not_exact_makes_the_whole_group_round_in_two_steps_the_other_groups_keep_their_own_rule():
    assert rate_groups([(D('5.5'), cents(4), True), (D('5.5'), cents(5), False)]) == [RateGroup(D('5.5'), cents(9), cents(1), cents(10))]
    assert rate_groups([(D('5.5'), cents(4), True), (D('5.5'), cents(5))]) == [RateGroup(D('5.5'), cents(9), cents(1), cents(10))]
    assert rate_groups([(D('5.5'), cents(4), False), (D('5.5'), cents(5), True)]) == [RateGroup(D('5.5'), cents(9), cents(1), cents(10))]
    # vier Gruppen mit 0,09 € netto, bei jedem dieser Sätze liegt die exakte Steuer knapp unter einem halben Cent (49,5 bis 49,77 Minor Units)
    mixed = rate_groups([(D('5.5'), cents(9), True), (D('5.51'), cents(9), False), (D('5.52'), cents(9), True), (D('5.53'), cents(9))])
    assert [(group.pct, group.tax) for group in mixed] == [(D('5.53'), cents(1)), (D('5.52'), 0), (D('5.51'), cents(1)), (D('5.5'), 0)]


def test_equal_rates_written_differently_stay_one_group_and_are_exact_when_all_their_lines_are():
    (group,) = rate_groups([(D('5.50'), cents(4), True), (D('5.5'), cents(5), True)])

    assert (group.pct, group.net, group.tax) == (D('5.5'), cents(9), 0)


def test_all_lines_exact_on_whole_rates_and_whole_cents_give_the_groups_of_before():
    chance = random.Random(280012)
    rates = [D(0), D(5), D(7), D(16), D(19), D(25)]
    for _ in range(500):
        lines = [(chance.choice(rates), cents(chance.randint(-50_000, 500_000))) for _ in range(chance.randint(0, 20))]

        assert rate_groups([(pct, net, True) for pct, net in lines]) == rate_groups(lines)


def test_the_groups_of_a_credit_note_are_the_negated_groups_of_the_invoice_in_both_roundings():
    chance = random.Random(280013)
    for exact in (False, True):
        for _ in range(500):
            lines = [(pct, net, exact) for pct, net in random_lines(chance)]

            positive = rate_groups(lines)
            negative = rate_groups([(pct, -net, flag) for pct, net, flag in lines])

            assert [(g.pct, -g.net, -g.tax, -g.gross) for g in negative] == [(g.pct, g.net, g.tax, g.gross) for g in positive]


def test_a_line_that_is_neither_a_pair_nor_a_triple_is_a_value_error_as_unpacking_was_before():
    for line in ((D(19),), (D(19), 100, True, 'extra'), ()):
        with pytest.raises(ValueError):
            rate_groups([line])


# ── item_groups und item_rate_of ─────────────────────────────────────────────────────────────────────────────────────────────

WRITTEN = [0.19, 19, '19', '0.19', D('19.00'), 0.07, 7, '7', 5.5, '5.5', 0.055, 10.7, 0, 100, None, '']                    # so steht ein Satz in der Datenbank


def test_with_pairs_item_groups_is_the_one_of_2_7_0_down_to_the_way_the_rates_are_written():
    chance = random.Random(280020)
    for _ in range(1_500):
        pairs = [(chance.choice(WRITTEN), chance.randint(-5_000_000, 50_000_000)) for _ in range(chance.randint(0, 12))]

        assert outcome(item_groups, pairs, default=D(19)) == outcome(old_item_groups, pairs, default=D(19)), pairs
        assert outcome(item_groups, iter(pairs), D(19)) == outcome(old_item_groups, pairs, D(19))
        assert outcome(item_groups, pairs) == outcome(old_item_groups, pairs), pairs                      # ohne Vorgabe: dieselbe Ablehnung
    assert outcome(item_groups, zip([19, 7], [cents(100), cents(50)])) == outcome(old_item_groups, [(19, cents(100)), (7, cents(50))])


def test_a_third_value_of_none_in_item_groups_changes_nothing():
    chance = random.Random(280021)
    for _ in range(500):
        pairs = [(chance.choice(WRITTEN), chance.randint(-5_000_000, 50_000_000)) for _ in range(chance.randint(0, 12))]

        assert outcome(item_groups, [(rate, net, None) for rate, net in pairs], D(19)) == outcome(old_item_groups, pairs, D(19))


def test_the_rate_of_a_position_comes_from_its_hundredths_of_a_percent_when_it_has_them():
    assert item_rate_of(19, None, None) == item_rate(19) == D(19)
    assert item_rate_of(None, None, D(7)) == item_rate(None, D(7)) == D(7)
    assert item_rate_of(0.19, 1900) == D(19) and item_rate_of(None, 550) == D('5.5') and item_rate_of('', 550, D(19)) == D('5.5')
    assert item_rate_of('kein Satz', 1000) == D(10) and str(item_rate_of('kein Satz', 1000)) == '10'              # nie 1E+1; ``rate`` zählt dann nicht


def test_a_position_with_its_rate_in_hundredths_of_a_percent_rounds_in_one_step():
    assert item_groups([(5.5, cents(9))]) == [RateGroup(D('5.5'), cents(9), cents(1), cents(10))]                # ohne: wie bisher
    assert item_groups([(5.5, cents(9), 550)]) == [RateGroup(D('5.5'), cents(9), 0, cents(9))]
    assert item_groups([(0.055, cents(9), 550)]) == [RateGroup(D('5.5'), cents(9), 0, cents(9))]                 # auch der Satz als Bruch
    assert item_groups([(None, cents(9), 550)]) == [RateGroup(D('5.5'), cents(9), 0, cents(9))]                  # er braucht weder ``rate`` noch eine Vorgabe
    with pytest.raises(Exception):
        item_groups([(None, cents(9))])                                                                       # ohne ``bp`` und ohne Vorgabe: kein stilles Raten


def test_positions_of_one_rate_form_one_group_and_it_is_exact_only_if_all_of_them_carry_hundredths_of_a_percent():
    assert item_groups([(5.5, cents(4), 550), (5.5, cents(5), 550)]) == [RateGroup(D('5.5'), cents(9), 0, cents(9))]
    assert item_groups([(5.5, cents(4), 550), (5.5, cents(5))]) == [RateGroup(D('5.5'), cents(9), cents(1), cents(10))]
    assert item_groups([(5.5, cents(4), 550), (5.5, cents(5), None)]) == [RateGroup(D('5.5'), cents(9), cents(1), cents(10))]
    (group,) = item_groups([(10, cents(100)), (None, cents(50), 1000)])                                         # ``1E+1`` und ``10``: dieselbe Gruppe
    assert (group.pct, group.net, group.tax) == (D(10), cents(150), cents(15))


def test_a_position_with_hundredths_of_a_percent_and_whole_rates_gives_the_groups_of_before():
    chance = random.Random(280022)
    for _ in range(500):
        pairs = [(chance.choice([19, 7, 0, 16, 25, 100]), cents(chance.randint(-50_000, 500_000))) for _ in range(chance.randint(0, 12))]
        with_bp = [(rate, net, int(rate) * 100) for rate, net in pairs]

        assert item_groups(with_bp) == item_groups(pairs)


def test_the_groups_of_credit_note_positions_with_hundredths_of_a_percent_are_the_negated_ones():
    chance = random.Random(280023)
    for _ in range(500):
        rates = [chance.choice([5.5, 19, 7, 0, 10.7]) for _ in range(chance.randint(1, 8))]
        pairs = [(rate, chance.randint(-5_000_000, 50_000_000), round(rate * 100)) for rate in rates]

        positive = item_groups(pairs)
        negative = item_groups([(rate, -net, bp) for rate, net, bp in pairs])

        assert [(g.pct, -g.net, -g.tax, -g.gross) for g in negative] == [(g.pct, g.net, g.tax, g.gross) for g in positive]


# ── mixed_groups ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

VALID_RATES = [19, 7, 0.19, 0.07, '19', '7', D('19.00'), 5.5, '5.5', 0.055, 0, 100, 10.7]
INVALID_RATES = [None, '', -1, -7, 150, 'sieben', True, float('nan'), D('Infinity')]
INVALID_NETS = ['50.00', 5000000.0, True, None, D(100)]


def test_with_pairs_mixed_groups_is_the_one_of_2_7_0_down_to_the_text_of_each_refusal():
    chance = random.Random(280030)
    for _ in range(1_500):
        pairs = [(chance.choice(VALID_RATES), chance.randint(-10 ** 6, 10 ** 8)) for _ in range(chance.randint(1, 6))]
        net, tax, gross = sums_of(old_item_groups(pairs))
        for stored in ((net, tax, gross), (net + 1, tax, gross), (net, tax - 100, gross), (net, tax, gross * 2), (None, tax, gross), (net, '1', gross)):
            assert outcome(mixed_groups, *stored, pairs) == outcome(old_mixed_groups, *stored, pairs)
        broken = list(pairs)
        broken[chance.randrange(len(broken))] = (chance.choice(INVALID_RATES), chance.randint(0, 10 ** 6))
        assert outcome(mixed_groups, net, tax, gross, broken) == outcome(old_mixed_groups, net, tax, gross, broken)
        broken[chance.randrange(len(broken))] = (chance.choice(VALID_RATES), chance.choice(INVALID_NETS))
        assert outcome(mixed_groups, net, tax, gross, broken) == outcome(old_mixed_groups, net, tax, gross, broken)
    assert outcome(mixed_groups, 0, 0, 0, []) == outcome(old_mixed_groups, 0, 0, 0, [])
    assert outcome(mixed_groups, 1, 1, 1, iter([(19, 1)])) == outcome(old_mixed_groups, 1, 1, 1, [(19, 1)])


def test_a_third_value_of_none_in_mixed_groups_changes_nothing():
    chance = random.Random(280031)
    for _ in range(500):
        pairs = [(chance.choice(VALID_RATES), chance.randint(-10 ** 6, 10 ** 8)) for _ in range(chance.randint(1, 6))]
        net, tax, gross = sums_of(old_item_groups(pairs))

        assert outcome(mixed_groups, net, tax, gross, [(rate, value, None) for rate, value in pairs]) == outcome(old_mixed_groups, net, tax, gross, pairs)


def test_a_position_with_its_hundredths_of_a_percent_is_read_in_one_step_and_the_sums_must_say_so():
    """19 % × 100,00 € und 5,5 % × 0,09 €: Die Steuer der zweiten Gruppe ist einstufig 0,00 € (zweistufig 0,01 €); die gespeicherten Summen gehören zu der
    Lesart, mit der der Beleg gespeichert wurde."""
    with_bp = [(19, cents(10_000), 1900), (5.5, cents(9), 550)]
    without = [(19, cents(10_000)), (5.5, cents(9))]
    one_step = (cents(10_009), cents(1_900), cents(11_909))
    two_steps = (cents(10_009), cents(1_901), cents(11_910))

    assert sums_of(mixed_groups(*one_step, with_bp)) == one_step
    assert sums_of(mixed_groups(*two_steps, without)) == two_steps
    assert [(g.pct, g.net, g.tax) for g in mixed_groups(*one_step, with_bp)] == [(D(19), cents(10_000), cents(1_900)), (D('5.5'), cents(9), 0)]
    for stored, pairs in ((two_steps, with_bp), (one_step, without)):
        with pytest.raises(RateGroupsMismatch):
            mixed_groups(*stored, pairs)


def test_hundredths_of_a_percent_that_agree_with_the_rate_give_the_groups_of_the_invoice_without_them():
    pairs = [(19.0, cents(6000)), (7.0, cents(5000)), (19.0, cents(4000))]
    with_bp = [(19.0, cents(6000), 1900), (7.0, cents(5000), 700), (19.0, cents(4000), 1900)]
    total = (cents(15000), cents(2250), cents(17250))

    assert mixed_groups(*total, with_bp) == mixed_groups(*total, pairs) == item_groups(with_bp)
    assert mixed_groups(*total, [(0.19, cents(10000), 1900), ('7', cents(5000), 700)]) == mixed_groups(*total, [(19, cents(10000)), (7, cents(5000))])
    assert mixed_groups(-cents(15000), -cents(2250), -cents(17250), [(19, -cents(10000), 1900), (7, -cents(5000), 700)])[0].gross == -cents(11900)


@pytest.mark.parametrize('pair', [
    (19, cents(100), 700),            # ein anderer Satz
    (19, cents(100), 1901),           # ein Hundertstel daneben
    (19, cents(100), 0),
    (7, cents(100), 1900),
    (0, cents(100), 700),
    (5.5, cents(100), 55),
    (5.5, cents(100), 5500),
    (19, cents(100), '1900'),         # kein ganzzahliger Wert: Text, Kommazahl, Decimal, Wahrheitswert
    (19, cents(100), 1900.0),
    (19, cents(100), D(1900)),
    (0.01, cents(100), True),
    (0.5, cents(100), 50),            # unter 1 Prozent: ``rate_pct`` liest 0,5 als Bruch, also als 50 %
    (0.01, cents(100), 1),
])
def test_hundredths_of_a_percent_that_do_not_agree_with_the_rate_of_the_position_are_refused(pair):
    with pytest.raises(RateGroupsMismatch, match='Position 1: Der Satz in Hundertstel Prozent passt nicht'):
        mixed_groups(cents(100), cents(19), cents(119), [pair])
    with pytest.raises(RateGroupsMismatch, match='Position 2: Der Satz in Hundertstel Prozent passt nicht'):
        mixed_groups(cents(200), cents(38), cents(238), [(19, cents(100), 1900), pair])


@pytest.mark.parametrize('rate', [None, '', -1, 150, 'sieben'])
def test_a_position_without_a_readable_rate_is_refused_even_with_hundredths_of_a_percent(rate):
    """``bp`` ersetzt den Satz der Position nicht: Er wird gegen ihn geprüft."""
    with pytest.raises(RateGroupsMismatch, match='Position 1: Der Steuersatz'):
        mixed_groups(cents(100), cents(19), cents(119), [(rate, cents(100), 1900)])


def test_a_position_with_a_net_that_is_not_a_whole_number_is_refused_whatever_its_hundredths_of_a_percent():
    for net in INVALID_NETS + [1.5]:
        with pytest.raises(RateGroupsMismatch):
            mixed_groups(0, 0, 0, [(19, net, 1900)])


# ── parse_rate_bp und bp_pct ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, bp', [
    ('0', 0), ('0,0', 0), ('0.00', 0), ('0,5', 50), ('0,05', 5), ('0,01', 1), ('1', 100), ('5', 500), ('5,5', 550), ('5.5', 550), ('5.50', 550),
    ('5,05', 505), ('7', 700), ('007', 700), ('07,5', 750), ('16', 1600), ('19', 1900), ('19,0', 1900), ('19,00', 1900), ('19.25', 1925),
    ('99,99', 9999), ('100', 10000), ('100,0', 10000), ('100.00', 10000),
])
def test_parse_rate_bp_reads_a_rate_in_hundredths_of_a_percent(text, bp):
    assert parse_rate_bp(text) == bp and type(parse_rate_bp(text)) is int


@pytest.mark.parametrize('text', [
    '', ' ', '5 ', ' 5', '\t5', '5\n', '\n5', '5 ', ' 5', '5​', '+5', '-5', '-0', '−1',                  # Leerraum, Vorzeichen, Minuszeichen
    '5%', '5 %', '19%', '1e2', '1E2', '1e+2', '0x13', '1_9', 'nan', 'NaN', 'inf', '-inf', 'Infinity', 'neunzehn', 'None',   # keine Zahl in dieser Schreibweise
    '5,555', '5.555', '5,500', '5,', '5.', ',5', '.5', '5,,5', '5..5', '5.5.5', '5,5,5', '1.234,5', '1,000',                # Nachkommastellen, Trenner
    '101', '100,01', '100,1', '150', '999', '1000',                                                                       # über 100 Prozent
    '٥', '٥,٥', '１９', '१९', '5٫5',                                                # Ziffern und Trenner anderer Schriften
    '1' * 5000, '9' * 5000, '0' * 5000, '1,' + '0' * 5000,                                                                # zu lang
])
def test_parse_rate_bp_reads_nothing_else(text):
    assert parse_rate_bp(text) is None


@pytest.mark.parametrize('value', [None, 19, 5.5, True, b'19', ['19'], D(19), object()])
def test_parse_rate_bp_reads_only_text(value):
    assert parse_rate_bp(value) is None


def test_every_rate_in_hundredths_of_a_percent_is_read_back_from_each_of_its_spellings():
    for bp in range(MAX_RATE_BP + 1):
        whole, fraction = divmod(bp, 100)
        spellings = [f'{whole}.{fraction:02d}', f'{whole},{fraction:02d}']
        if fraction == 0:
            spellings.append(str(whole))
        if fraction % 10 == 0:
            spellings.append(f'{whole},{fraction // 10}')

        assert all(parse_rate_bp(text) == bp for text in spellings), (bp, spellings)


def test_bp_pct_is_exact_and_never_written_with_an_exponent():
    for bp in range(MAX_RATE_BP + 1):
        pct = bp_pct(bp)

        assert pct == D(bp) / 100 and 'E' not in str(pct) and D(str(pct)) == pct, bp
    assert [str(bp_pct(bp)) for bp in (0, 5, 50, 100, 505, 520, 550, 700, 1000, 1900, 10000)] == [
        '0', '0.05', '0.5', '1', '5.05', '5.2', '5.5', '7', '10', '19', '100']


def test_bp_pct_is_rate_pct_of_the_same_rate_except_below_one_percent():
    """``rate_pct`` liest einen Wert zwischen 0 und 1 als Bruch: Für 0,5 Prozent liefert es 50. Ein Eintrag mit einem Satz unter 1 Prozent lässt sich in
    ``InvoiceItems.TaxRate`` deshalb nicht eindeutig führen – ``mixed_groups`` lehnt so eine Position ab (Test oben)."""
    for bp in range(MAX_RATE_BP + 1):
        pct = bp_pct(bp)
        if bp == 0 or bp >= 100:
            assert rate_pct(pct) == pct, bp
        else:
            assert rate_pct(pct) == pct * 100 != pct, bp


def test_the_rate_text_a_form_sends_gives_the_rate_a_group_carries():
    for text, pct in (('19', D(19)), ('5,5', D('5.5')), ('5.50', D('5.5')), ('100', D(100)), ('0', D(0)), ('16,25', D('16.25'))):
        assert bp_pct(parse_rate_bp(text)) == pct == rate_pct(pct), text


# ── group_label ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('pct, exempt, label', [
    (D(19), False, '19 %'), (D(7), False, '7 %'), (D('5.5'), False, '5,5 %'), (D('1E+1'), False, '10 %'), (D('1E+2'), False, '100 %'),
    (D('10.7'), False, '10,7 %'), (D(0), False, '0 %'),
    (D(0), True, 'steuerfrei'), (D(19), True, 'steuerfrei'),
])
def test_group_label_names_the_rate_of_a_group_or_says_it_is_tax_exempt(pct, exempt, label):
    assert group_label(pct, exempt) == label


# ── check_tax_entry ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def entry(**changes):
    """Die Argumente eines gültigen Eintrags (19 %, Standardkonto) – ``changes`` ändert einzelne."""
    arguments = dict(label='Regelsteuersatz', bp=1900, kind=STANDARD, reason=None, revenue_coa_id=None, has_default_revenue=True)
    arguments.update(changes)
    return arguments


def check(**changes):
    return check_tax_entry(**entry(**changes))


def test_the_signature_has_no_exemption_code_and_the_module_does_not_know_one():
    """Der Befreiungsgrund ist nur Text (BT-120); einen Code (BT-121) gibt es in 2.8.0 nicht. Die ersten sieben Parameter sind die von E0; die Steuerkonten
    (E2) kommen als Schlüsselwort-Parameter mit Vorgaben dazu – ein Parameter für einen Befreiungscode ist keiner davon."""
    parameters = inspect.signature(check_tax_entry).parameters
    positional = [name for name, parameter in parameters.items() if parameter.kind is not inspect.Parameter.KEYWORD_ONLY]
    assert positional == ['label', 'bp', 'kind', 'reason', 'revenue_coa_id', 'has_default_revenue', 'revenue_is_income']
    assert not [name for name in parameters if 'code' in name.lower()]
    assert parameters['revenue_is_income'].default is True
    assert 'VATEX' not in inspect.getsource(tax_groups)
    assert not [name for name in dir(tax_groups) if 'EXEMPT' in name.upper() and 'CODE' in name.upper()]


def test_the_sentences_are_the_fixed_ones_of_the_plan():
    assert TAX_ENTRY_LABEL == 'Bitte eine Bezeichnung angeben (höchstens 60 Zeichen).'
    assert TAX_ENTRY_RATE == 'Bitte den Steuersatz als Zahl angeben: 0 oder 1 bis 100, mit höchstens zwei Nachkommastellen.'
    assert TAX_ENTRY_KIND == 'Bitte eine gültige Art wählen.'
    assert TAX_ENTRY_EXEMPT_RATE == 'Eine steuerfreie Leistung hat den Satz 0.'
    assert TAX_ENTRY_REASON == 'Bitte den Grund der Steuerbefreiung angeben – er steht auf der Rechnung (höchstens 300 Zeichen).'
    assert TAX_ENTRY_REVENUE_REQUIRED == 'Bitte ein Erlöskonto wählen – für diesen Steuersatz gibt es kein Standardkonto.'
    assert TAX_ENTRY_REVENUE_NOT_INCOME == 'Das gewählte Konto ist kein Erlöskonto.'
    assert TAX_ENTRY_LAST_ACTIVE == 'Der letzte wählbare Steuersatz lässt sich nicht löschen oder abschalten.'
    assert (MAX_LABEL_LENGTH, MAX_REASON_LENGTH, MAX_RATE_BP, STANDARD, EXEMPT) == (60, 300, 10000, 'standard', 'exempt')


def test_an_entry_of_the_preset_rates_needs_no_account_but_may_have_one():
    assert check() is None
    assert check(label='Ermäßigter Satz', bp=700) is None
    assert check(revenue_coa_id=17) is None                                  # ein Erlöskonto: ``revenue_is_income`` ist ohne Angabe wahr
    assert check(revenue_coa_id=17, revenue_is_income=True) is None


def test_an_entry_of_any_other_rate_needs_an_account_that_is_a_revenue_account():
    for bp in (550, 0, 100, 1600, 10000):
        assert check(bp=bp, has_default_revenue=False) == TAX_ENTRY_REVENUE_REQUIRED, bp
        assert check(bp=bp, has_default_revenue=False, revenue_coa_id=17) is None, bp
        assert check(bp=bp, has_default_revenue=False, revenue_coa_id=17, revenue_is_income=False) == TAX_ENTRY_REVENUE_NOT_INCOME, bp
    assert check(revenue_coa_id=17, revenue_is_income=False) == TAX_ENTRY_REVENUE_NOT_INCOME                  # auch mit Standardkonto: ein gewähltes Konto zählt


def test_a_standard_entry_with_the_rate_0_is_allowed_with_an_account():
    """Nullsatz (Kategorie Z): Die Liste ersetzt das freie Feld ``0 %`` ohne § 19 – mit Pflicht-Erlöskonto, nie 4185 geraten."""
    assert check(label='Nullsatz', bp=0, has_default_revenue=False, revenue_coa_id=3) is None
    assert check(label='Nullsatz', bp=0, has_default_revenue=False) == TAX_ENTRY_REVENUE_REQUIRED


def test_a_tax_exempt_entry_has_the_rate_0_a_reason_and_an_account():
    exempt = dict(label='Steuerfrei (erfunden)', bp=0, kind=EXEMPT, reason='Beispielhafter Befreiungsgrund', has_default_revenue=False, revenue_coa_id=3)

    assert check(**exempt) is None
    assert check(**{**exempt, 'revenue_coa_id': None}) == TAX_ENTRY_REVENUE_REQUIRED
    assert check(**{**exempt, 'revenue_is_income': False}) == TAX_ENTRY_REVENUE_NOT_INCOME
    assert check(**{**exempt, 'bp': 700}) == TAX_ENTRY_EXEMPT_RATE
    assert check(**{**exempt, 'bp': 100}) == TAX_ENTRY_EXEMPT_RATE
    assert check(**{**exempt, 'bp': 1}) == TAX_ENTRY_RATE                                                        # unter 1 %: schon der Satz gilt nicht
    for reason in (None, '', ' ', '\t \n', ' ', 5, ['Grund'], 'Grund\nmit Umbruch', 'Grund\tmit Tabulator', 'Grund\x00', 'Grund\u202e'):
        assert check(**{**exempt, 'reason': reason}) == TAX_ENTRY_REASON, reason
    assert check(**{**exempt, 'reason': 'x' * MAX_REASON_LENGTH}) is None
    assert check(**{**exempt, 'reason': 'x' * (MAX_REASON_LENGTH + 1)}) == TAX_ENTRY_REASON
    assert check(**{**exempt, 'reason': 'ä' * MAX_REASON_LENGTH}) is None                                         # gemessen wird in Zeichen, nicht in Bytes


def test_the_reason_counts_only_for_a_tax_exempt_entry():
    for reason in (None, '', 'Ein Grund, den eine normale Leistung nicht braucht', 'x' * 1000):
        assert check(reason=reason) is None


def test_the_label_is_a_text_of_1_to_60_characters_without_control_and_format_characters():
    assert check(label='x' * MAX_LABEL_LENGTH) is None and check(label='ä' * MAX_LABEL_LENGTH) is None and check(label='x') is None
    assert check(label='Satz "steuerfrei" <b>&</b> mehr') is None and check(label='<script>alert(1)</script>') is None          # maskiert wird bei der Ausgabe
    for label in ('', ' ', '   ', ' ', '\t', 'a\tb', 'a\nb', 'a\rb', 'a\x00b', 'a\x7fb', 'a​b', 'a‮b', 'a­b', 'a﻿b',
                  'x' * (MAX_LABEL_LENGTH + 1), None, 19, b'Satz', ['Satz']):
        assert check(label=label) == TAX_ENTRY_LABEL, repr(label)


def test_the_rate_is_a_whole_number_of_hundredths_of_a_percent_from_0_to_100_percent():
    for bp in (0, 100, 550, 1900, MAX_RATE_BP):
        assert check(bp=bp, has_default_revenue=True) is None, bp
    for bp in (None, -1, 1, 50, 99, MAX_RATE_BP + 1, 10 ** 9, '1900', '19', 19.0, 5.5, D(1900), True, False):                 # 1–99: unter 1 %, in keinem Beleg verwendbar
        assert check(bp=bp) == TAX_ENTRY_RATE, repr(bp)


def test_the_kind_is_exactly_standard_or_exempt():
    for kind in (None, '', 'Standard', 'EXEMPT', ' exempt', 'exempt ', 'free', 1, True, ['exempt']):
        assert check(kind=kind) == TAX_ENTRY_KIND, repr(kind)
    assert check(kind=STANDARD) is None


def test_the_first_rule_that_is_broken_decides():
    broken = dict(label='', bp=None, kind='?', reason='', revenue_coa_id=5, has_default_revenue=False, revenue_is_income=False)

    assert check_tax_entry(**broken) == TAX_ENTRY_LABEL
    broken['label'] = 'Satz'
    assert check_tax_entry(**broken) == TAX_ENTRY_RATE
    broken['bp'] = 700
    assert check_tax_entry(**broken) == TAX_ENTRY_KIND
    broken['kind'] = EXEMPT
    assert check_tax_entry(**broken) == TAX_ENTRY_EXEMPT_RATE
    broken['bp'] = 0
    assert check_tax_entry(**broken) == TAX_ENTRY_REASON
    broken['reason'] = 'Grund'
    assert check_tax_entry(**broken) == TAX_ENTRY_REVENUE_NOT_INCOME
    broken['revenue_is_income'] = True
    assert check_tax_entry(**broken) is None


def test_the_rule_about_the_last_selectable_entry_is_not_part_of_the_check():
    """Sie braucht die anderen Einträge und steht in der Datenbankschicht; ihr Satz liegt hier, an der einen Stelle der Texte."""
    sentences = {check(label=label, bp=bp, kind=kind, reason=reason, revenue_coa_id=coa, has_default_revenue=default, revenue_is_income=income)
                 for label in ('Satz', '') for bp in (0, 700, 1900, None) for kind in (STANDARD, EXEMPT, '?') for reason in ('Grund', '')
                 for coa in (None, 4) for default in (False, True) for income in (False, True)}

    assert TAX_ENTRY_LAST_ACTIVE not in sentences
    assert sentences == {None, TAX_ENTRY_LABEL, TAX_ENTRY_RATE, TAX_ENTRY_KIND, TAX_ENTRY_EXEMPT_RATE, TAX_ENTRY_REASON, TAX_ENTRY_REVENUE_REQUIRED,
                         TAX_ENTRY_REVENUE_NOT_INCOME}


# ── Was sich nicht ändert ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_preset_rates_are_the_ones_of_before():
    assert MIXED_TAX_RATES == (D('19'), D('7')) and all(type(rate) is D for rate in MIXED_TAX_RATES)


def test_the_module_stays_pure_it_imports_only_the_standard_library_and_money():
    """Keine Datenbank, keine Uhr, kein ``server``, ``db`` oder ``export`` – so kann jede Schicht es nutzen."""
    tree = ast.parse(pathlib.Path(tax_groups.__file__).read_text(encoding='utf-8'))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or '').split('.')[0])

    assert imported <= {'dataclasses', 'decimal', 'money', 're', 'unicodedata'}, imported


# ── Die Steuerkonten eines Eintrags (E2): Regeln 7 bis 10 von check_tax_entry ─────────────────────────────────────────────────────

from tax_groups import TAX_ENTRY_INPUT_TAX_ACCOUNT, TAX_ENTRY_TAX_ACCOUNT_FIXED, TAX_ENTRY_TAX_ACCOUNT_ZERO, TAX_ENTRY_VAT_ACCOUNT


def check_before_the_tax_accounts(label, bp, kind, reason, revenue_coa_id, has_default_revenue, revenue_is_income=True):
    """``check_tax_entry`` am Stand von E0 (vor den Steuerkonten), aus dem Code von damals kopiert."""
    if not tax_groups._plain_text(label, MAX_LABEL_LENGTH):
        return TAX_ENTRY_LABEL
    if type(bp) is not int or not (bp == 0 or tax_groups.MIN_RATE_BP <= bp <= MAX_RATE_BP):
        return TAX_ENTRY_RATE
    if kind not in (STANDARD, EXEMPT):
        return TAX_ENTRY_KIND
    if kind == EXEMPT:
        if bp != 0:
            return TAX_ENTRY_EXEMPT_RATE
        if not tax_groups._plain_text(reason, MAX_REASON_LENGTH):
            return TAX_ENTRY_REASON
    if revenue_coa_id is None:
        return None if has_default_revenue else TAX_ENTRY_REVENUE_REQUIRED
    return None if revenue_is_income else TAX_ENTRY_REVENUE_NOT_INCOME


def with_accounts(**changes):
    """Ein gültiger Eintrag über 5,5 % mit Erlöskonto – ``changes`` ändert einzelne Argumente (auch die Steuerkonten)."""
    return check(**{**dict(label='Ermäßigt (erfunden)', bp=550, has_default_revenue=False, revenue_coa_id=17), **changes})


def test_the_sentences_of_the_tax_accounts_are_the_fixed_ones_of_the_plan():
    assert TAX_ENTRY_TAX_ACCOUNT_ZERO == 'Ein Steuersatz von 0 % trägt kein Umsatzsteuer- und kein Vorsteuerkonto.'
    assert TAX_ENTRY_TAX_ACCOUNT_FIXED == 'Für diesen Steuersatz stehen die Steuerkonten fest – bitte kein Umsatzsteuer- oder Vorsteuerkonto wählen.'
    assert TAX_ENTRY_VAT_ACCOUNT == 'Das gewählte Konto ist kein Umsatzsteuerkonto.'
    assert TAX_ENTRY_INPUT_TAX_ACCOUNT == 'Das gewählte Konto ist kein Vorsteuerkonto.'


def test_the_tax_accounts_are_keyword_only_parameters_with_defaults_that_change_nothing():
    parameters = inspect.signature(check_tax_entry).parameters
    keyword_only = {name: parameter.default for name, parameter in parameters.items() if parameter.kind is inspect.Parameter.KEYWORD_ONLY}

    assert keyword_only == dict(vat_coa_id=None, input_tax_coa_id=None, has_fixed_tax_accounts=False, vat_is_vat_account=True, input_is_input_account=True)


def test_without_a_tax_account_every_result_is_the_one_of_before_whatever_the_other_new_arguments_say():
    """Eigenschaft über ein Gitter aus Bezeichnungen, Sätzen, Arten, Gründen und Erlöskonten (3 888 Aufrufe): Ohne Steuerkonto ist jedes Ergebnis das
    der Fassung von E0 – auch wenn die Kennzeichen der Steuerkonten gesetzt sind (sie zählen erst, wenn ein Konto gesetzt ist)."""
    grid = [(label, bp, kind, reason, coa, default, income)
            for label in ('Satz', '', 'x' * (MAX_LABEL_LENGTH + 1))
            for bp in (None, -1, 0, 1, 99, 100, 550, 700, 1900, MAX_RATE_BP, MAX_RATE_BP + 1, '1900')
            for kind in (STANDARD, EXEMPT, '?')
            for reason in ('Grund', '', None)
            for coa in (None, 4)
            for default in (False, True)
            for income in (False, True)]

    assert len(grid) == 3 * 12 * 3 * 3 * 2 * 2 * 2
    for label, bp, kind, reason, coa, default, income in grid:
        before = check_before_the_tax_accounts(label, bp, kind, reason, coa, default, income)
        assert check_tax_entry(label, bp, kind, reason, coa, default, income) == before
        assert check_tax_entry(label, bp, kind, reason, coa, default, income, vat_coa_id=None, input_tax_coa_id=None, has_fixed_tax_accounts=False,
                               vat_is_vat_account=True, input_is_input_account=True) == before
        assert check_tax_entry(label, bp, kind, reason, coa, default, income, has_fixed_tax_accounts=True, vat_is_vat_account=False,
                               input_is_input_account=False) == before, (label, bp, kind, reason, coa, default, income)


def test_an_entry_with_valid_tax_accounts_passes():
    assert with_accounts() is None
    assert with_accounts(vat_coa_id=30) is None
    assert with_accounts(input_tax_coa_id=31) is None
    assert with_accounts(vat_coa_id=30, input_tax_coa_id=31) is None
    assert with_accounts(bp=1000, vat_coa_id=30, input_tax_coa_id=31) is None


def test_rule_7_a_rate_of_0_carries_no_tax_account_neither_a_tax_free_nor_a_standard_entry():
    exempt = dict(label='Steuerfrei (erfunden)', bp=0, kind=EXEMPT, reason='Beispielhafter Befreiungsgrund', has_default_revenue=False, revenue_coa_id=3)

    for accounts in (dict(vat_coa_id=30), dict(input_tax_coa_id=31), dict(vat_coa_id=30, input_tax_coa_id=31)):
        assert check(**exempt, **accounts) == TAX_ENTRY_TAX_ACCOUNT_ZERO, accounts
        assert with_accounts(bp=0, **accounts) == TAX_ENTRY_TAX_ACCOUNT_ZERO, accounts                       # Art „Standard“ mit Satz 0
    assert check(**exempt) is None and with_accounts(bp=0) is None
    assert with_accounts(bp=0, vat_coa_id=30, has_fixed_tax_accounts=True) == TAX_ENTRY_TAX_ACCOUNT_ZERO     # 7 vor 8


@pytest.mark.parametrize('bp', [1900, 1600, 700, 500])
def test_rule_8_a_rate_with_fixed_tax_accounts_takes_none_of_its_own(bp):
    for accounts in (dict(vat_coa_id=30), dict(input_tax_coa_id=31), dict(vat_coa_id=30, input_tax_coa_id=31)):
        assert with_accounts(bp=bp, has_fixed_tax_accounts=True, **accounts) == TAX_ENTRY_TAX_ACCOUNT_FIXED, accounts
    assert with_accounts(bp=bp, has_fixed_tax_accounts=True) is None                                            # ohne Konto bleibt sie, wie sie war


def test_the_flag_of_the_fixed_tax_accounts_is_the_callers_not_a_list_in_the_function():
    """Die Funktion kennt keinen Satz: Welche Sätze feste Steuerkonten haben, sagt allein der Aufrufer (die Datenbankschicht, aus der Tabelle der Summen- und
    Saldenliste)."""
    assert with_accounts(bp=1900, vat_coa_id=30) is None
    assert with_accounts(bp=550, vat_coa_id=30, has_fixed_tax_accounts=True) == TAX_ENTRY_TAX_ACCOUNT_FIXED


def test_rule_9_a_chosen_vat_account_is_a_vat_account():
    assert with_accounts(vat_coa_id=30, vat_is_vat_account=False) == TAX_ENTRY_VAT_ACCOUNT
    assert with_accounts(vat_coa_id=30, input_tax_coa_id=31, vat_is_vat_account=False) == TAX_ENTRY_VAT_ACCOUNT
    assert with_accounts(vat_coa_id=30, vat_is_vat_account=True) is None
    assert with_accounts(input_tax_coa_id=31, vat_is_vat_account=False) is None            # ohne Umsatzsteuerkonto gibt es nichts zu prüfen


def test_rule_10_a_chosen_input_tax_account_is_an_input_tax_account():
    assert with_accounts(input_tax_coa_id=31, input_is_input_account=False) == TAX_ENTRY_INPUT_TAX_ACCOUNT
    assert with_accounts(vat_coa_id=30, input_tax_coa_id=31, input_is_input_account=False) == TAX_ENTRY_INPUT_TAX_ACCOUNT
    assert with_accounts(input_tax_coa_id=31, input_is_input_account=True) is None
    assert with_accounts(vat_coa_id=30, input_is_input_account=False) is None              # ohne Vorsteuerkonto gibt es nichts zu prüfen


def test_the_rules_of_the_tax_accounts_come_after_the_six_of_before_and_in_their_own_order():
    broken = dict(label='', bp=None, kind='?', reason='', revenue_coa_id=5, has_default_revenue=False, revenue_is_income=False,
                  vat_coa_id=30, input_tax_coa_id=31, has_fixed_tax_accounts=True, vat_is_vat_account=False, input_is_input_account=False)

    assert check_tax_entry(**broken) == TAX_ENTRY_LABEL
    broken['label'] = 'Satz'
    assert check_tax_entry(**broken) == TAX_ENTRY_RATE
    broken['bp'] = 0
    assert check_tax_entry(**broken) == TAX_ENTRY_KIND
    broken['kind'] = EXEMPT
    assert check_tax_entry(**broken) == TAX_ENTRY_REASON
    broken['reason'] = 'Grund'
    assert check_tax_entry(**broken) == TAX_ENTRY_REVENUE_NOT_INCOME                         # 6 vor 7
    broken['revenue_is_income'] = True
    assert check_tax_entry(**broken) == TAX_ENTRY_TAX_ACCOUNT_ZERO                           # 7
    broken.update(bp=550, kind=STANDARD)
    assert check_tax_entry(**broken) == TAX_ENTRY_TAX_ACCOUNT_FIXED                          # 8 vor 9 und 10
    broken['has_fixed_tax_accounts'] = False
    assert check_tax_entry(**broken) == TAX_ENTRY_VAT_ACCOUNT                                # 9 vor 10
    broken['vat_is_vat_account'] = True
    assert check_tax_entry(**broken) == TAX_ENTRY_INPUT_TAX_ACCOUNT                          # 10
    broken['input_is_input_account'] = True
    assert check_tax_entry(**broken) is None


def test_a_standard_entry_of_the_rate_0_without_an_account_is_refused_for_the_missing_revenue_account_before_its_tax_account():
    assert check(label='Nullsatz', bp=0, has_default_revenue=False, vat_coa_id=30) == TAX_ENTRY_REVENUE_REQUIRED


def test_every_sentence_the_check_can_return_is_one_of_the_fixed_ones():
    sentences = {check_tax_entry(label, bp, kind, reason, coa, default, income, vat_coa_id=vat, input_tax_coa_id=input_tax, has_fixed_tax_accounts=fixed,
                                 vat_is_vat_account=is_vat, input_is_input_account=is_input)
                 for label in ('Satz', '') for bp in (0, 550, 1900, None) for kind in (STANDARD, EXEMPT, '?') for reason in ('Grund', '')
                 for coa in (None, 4) for default in (False, True) for income in (False, True) for vat in (None, 30) for input_tax in (None, 31)
                 for fixed in (False, True) for is_vat in (False, True) for is_input in (False, True)}

    assert sentences == {None, TAX_ENTRY_LABEL, TAX_ENTRY_RATE, TAX_ENTRY_KIND, TAX_ENTRY_EXEMPT_RATE, TAX_ENTRY_REASON, TAX_ENTRY_REVENUE_REQUIRED,
                         TAX_ENTRY_REVENUE_NOT_INCOME, TAX_ENTRY_TAX_ACCOUNT_ZERO, TAX_ENTRY_TAX_ACCOUNT_FIXED, TAX_ENTRY_VAT_ACCOUNT,
                         TAX_ENTRY_INPUT_TAX_ACCOUNT}
    assert TAX_ENTRY_LAST_ACTIVE not in sentences
