# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die reinen Funktionen für die Buchungen eines Belegs mit Steuersatz je Position (``tax_groups``, 2.7.0): ``split_payment`` (wie sich eine
Zahlung auf die Satzgruppen verteilt – Betrag und Steuer je Satz), ``mixed_groups`` (die Gruppen eines Belegs, geprüft gegen seine Summen),
``booking_rate`` (die Form, in der der Satz in ``Bookings.TaxRate`` steht), ``pct_text`` und – für die Erstattung und den Schutz beim Entfernen einer Zahlung – ``refund_pool``
(die Gruppen, aus denen erstattet wird) und ``booked_fault`` (ob der gebuchte Stand je Satz zu ihnen passt: die Eingangsprüfung von ``split_payment``).

Die feste Rechnung ist **M**: 19 % × 100,00 und 7 % × 50,00 – Gruppe 19: netto 100,00 / Steuer 19,00 / brutto 119,00, Gruppe 7: 50,00 / 3,50 /
53,50, zusammen 150,00 / 22,50 / 172,50. Beträge stehen in Minor Units (1 Cent = 100); ``eur`` schreibt sie als Cent-Betrag hin.

Alle Beträge und Sätze sind erfunden.
"""
import random
from decimal import Decimal

import pytest

from money import prorate, round_minor
from tax_groups import (RateGroup, RateGroupsMismatch, SplitPart, booked_fault, booking_rate, mixed_groups, pct_text, rate_groups, refund_pool,
                        split_payment)

D = Decimal


def eur(cents):
    """Ein Cent-Betrag (``100`` = 1,00 €) in Minor Units."""
    return cents * 100


def groups_of(*lines):
    """Satzgruppen aus ``(Satz, Netto in Cent)``, wie die Buchungen sie bilden (``rate_groups``)."""
    return rate_groups([(D(rate), eur(net)) for rate, net in lines])


M = groups_of((19, 10000), (7, 5000))


def parts_of(result):
    """``{Satz: (Betrag, Steuer)}`` in Cent, zum Vergleichen."""
    return {int(part.pct) if part.pct == int(part.pct) else part.pct: (part.amount // 100, part.tax // 100) for part in result}


def booked_of(result, before=None):
    """Was die Zahlungen bisher je Satz tragen, als Eingabe der nächsten Zahlung."""
    booked = dict(before or {})
    for part in result:
        done_amount, done_tax = booked.get(part.pct, (0, 0))
        booked[part.pct] = (done_amount + part.amount, done_tax + part.tax)
    return booked


# ── Die Beispielrechnung ─────────────────────────────────────────────────────

def test_the_groups_of_the_example_invoice():
    assert M == [RateGroup(D(19), eur(10000), eur(1900), eur(11900)), RateGroup(D(7), eur(5000), eur(350), eur(5350))]


def test_a_part_payment_of_100_goes_68_99_to_19_percent_and_31_01_to_7_percent():
    """Ziel 7 % = 100,00 × 53,50 / 172,50 = 31,0145 → 31,01; der Rest 68,99 geht an 19 %. Steuer 19 %: 19,00 × 68,99 / 119,00 = 11,0152 →
    11,02; Steuer 7 %: 3,50 × 31,01 / 53,50 = 2,0288 → 2,03."""
    result = split_payment(M, {}, eur(10000))

    assert parts_of(result) == {19: (6899, 1102), 7: (3101, 203)}
    assert all(isinstance(part, SplitPart) for part in result) and [part.pct for part in result] == [D(19), D(7)]


def test_the_closing_payment_after_it_closes_every_group_with_its_rest():
    first = split_payment(M, {}, eur(10000))

    closing = split_payment(M, booked_of(first), eur(7250))

    assert parts_of(closing) == {19: (5001, 798), 7: (2249, 147)}                 # 119,00 − 68,99 und 53,50 − 31,01; Steuerrest
    total = booked_of(closing, booked_of(first))
    assert total == {D(19): (eur(11900), eur(1900)), D(7): (eur(5350), eur(350))}


def test_three_payments_of_50_50_and_72_50():
    first = split_payment(M, {}, eur(5000))
    assert parts_of(first) == {19: (3449, 551), 7: (1551, 101)}
    second = split_payment(M, booked_of(first), eur(5000))                            # Ziel 7 % jetzt 31,01 insgesamt: 15,50
    assert parts_of(second) == {19: (3450, 551), 7: (1550, 101)}
    third = split_payment(M, booked_of(second, booked_of(first)), eur(7250))
    assert parts_of(third) == {19: (5001, 798), 7: (2249, 148)}                       # Steuerrest: 19,00 − 11,02 und 3,50 − 2,02
    assert booked_of(third, booked_of(second, booked_of(first))) == {D(19): (eur(11900), eur(1900)), D(7): (eur(5350), eur(350))}


def test_a_payment_of_the_whole_invoice_pays_each_group_in_full():
    result = split_payment(M, {}, eur(17250))

    assert parts_of(result) == {19: (11900, 1900), 7: (5350, 350)}


# ── Rundung und Gleichstand ──────────────────────────────────────────────────

def test_an_exact_half_cent_goes_to_the_larger_group():
    """19 %: brutto 3,00, 7 %: brutto 1,00, Zahlung 0,02: Das Ziel der kleineren Gruppe ist genau 0,005 – bei „halb auf“ bekäme sie 0,01
    (0,01/0,01), der Cent-Rest soll aber an die größere Gruppe gehen."""
    groups = [RateGroup(D(19), 25200, 4800, 30000), RateGroup(D(7), 9300, 700, 10000)]

    result = split_payment(groups, {}, 200)

    assert [(part.pct, part.amount, part.tax) for part in result] == [(D(19), 200, 0), (D(7), 0, 0)]


def test_with_equal_open_amounts_the_higher_rate_gets_the_rest():
    groups = [RateGroup(D(19), 8400, 1600, 10000), RateGroup(D(7), 9300, 700, 10000)]

    result = split_payment(groups, {}, 100)                                           # 0,01: Ziel 7 % = 0,005 → 0,00

    assert [(part.pct, part.amount) for part in result] == [(D(19), 100), (D(7), 0)]


def test_a_group_that_gets_closed_before_the_end_takes_the_rest_of_its_tax():
    """172,49 von 172,50: Das Ziel von 7 % ist 53,4969 → 53,50 = ihr ganzer Rest (Steuer 3,50). 19 % bekommt 118,99 (Steuer anteilig 18,9984 →
    19,00, begrenzt auf den Rest 19,00). Die letzten 0,01 gehen an 19 % – ohne Steuer, denn die ist schon gebucht; nie eine Zeile mit Betrag 0 und
    Steuer."""
    first = split_payment(M, {}, eur(17249))

    assert parts_of(first) == {19: (11899, 1900), 7: (5350, 350)}
    second = split_payment(M, booked_of(first), 100)
    assert [(part.pct, part.amount, part.tax) for part in second] == [(D(19), 100, 0), (D(7), 0, 0)]


def test_a_group_without_a_share_carries_no_tax():
    result = split_payment(M, {}, eur(1))                                              # 0,01 von 172,50

    assert [(part.pct, part.amount, part.tax) for part in result] == [(D(19), 100, 0), (D(7), 0, 0)]


def test_the_shortfall_goes_to_the_other_groups_and_a_group_never_gets_less_than_nothing():
    """Hat eine frühere Zahlung eine Gruppe über ihr Ziel gebracht, bekommt sie nichts mehr (nie unter 0) – der Rest geht an die anderen."""
    groups = groups_of((19, 10000), (7, 5000), (5, 4000))
    # 19 %: schon fast ganz bezahlt (118,00 von 119,00), 7 % und 5 % nicht
    booked = {D(19): (eur(11800), eur(1880))}
    result = split_payment(groups, booked, eur(5000))

    assert sum(part.amount for part in result) == eur(5000)
    assert result[0].amount <= eur(100)                                                # höchstens der Rest von 19 %
    assert all(part.amount >= 0 for part in result)


def test_the_overhang_of_the_largest_group_goes_to_the_other_groups_in_their_order():
    """Vier Gruppen, jede hat noch 0,01 offen, bezahlt werden 0,02: Die Ziele der drei anderen (auf den Cent gerundet) liegen bei dem, was sie schon
    gezahlt haben – ihr Anteil ist 0 –, also bekäme die größte (bei Gleichstand: 19 %) 0,02, mehr, als bei ihr offen ist. Der Überhang geht in der
    Reihenfolge der Gruppen an die nächste mit Platz: 16 %. Dort schließt der Teil die Gruppe, also bekommt sie den Rest ihrer Steuer (hier 0)."""
    groups = [RateGroup(D(19), 2100, 400, 2500), RateGroup(D(16), 4100, 700, 4800),
              RateGroup(D(7), 4900, 300, 5200), RateGroup(D('5.5'), 5900, 300, 6200)]
    booked = {D(19): (2400, 400), D(16): (4700, 700), D(7): (5100, 300), D('5.5'): (6100, 300)}

    result = split_payment(groups, booked, 200)

    assert [(part.pct, part.amount, part.tax) for part in result] == [(D(19), 100, 0), (D(16), 100, 0), (D(7), 0, 0), (D('5.5'), 0, 0)]


# ── Ablehnungen ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('amount', [0, -100, eur(17251), True, 100.0, '100', None])
def test_an_amount_that_is_not_a_positive_whole_number_within_the_open_amount_is_refused(amount):
    with pytest.raises(ValueError):
        split_payment(M, {}, amount)


def test_more_than_what_is_still_open_is_refused():
    booked = {D(19): (eur(11900), eur(1900))}

    with pytest.raises(ValueError):
        split_payment(M, booked, eur(5351))
    assert split_payment(M, booked, eur(5350))[1].amount == eur(5350)


@pytest.mark.parametrize('booked', [
    {D(19): (eur(11901), 0)},                           # mehr gebucht als das Brutto der Gruppe
    {D(19): (-1, 0)},                                   # negativ
    {D(19): (0, eur(1901))},                            # mehr Steuer als die Gruppe hat
    {D(19): (eur(11900), eur(1800))},                   # ganz bezahlt, Steuer aber nicht ganz gebucht: der Bestand ist nicht stimmig
    {D(5): (100, 0)},                                   # ein Satz, den der Beleg nicht trägt
    {D(19): (100.0, 0)},                                # keine ganze Zahl
])
def test_an_inconsistent_stock_is_refused(booked):
    with pytest.raises(ValueError):
        split_payment(M, booked, 100)


@pytest.mark.parametrize('groups', [
    [],
    [RateGroup(D(19), 0, 0, 0)],                        # ohne Brutto
    [RateGroup(D(19), -100, -19, -119)],
    [RateGroup(D(19), 100, 19, 119), RateGroup(D(19), 100, 19, 119)],      # derselbe Satz zweimal
])
def test_groups_that_cannot_share_a_payment_are_refused(groups):
    with pytest.raises(ValueError):
        split_payment(groups, {}, 100)


# ── Eigenschaften (Zufall mit festem Seed) ───────────────────────────────────

RATES = [D(19), D(7), D(16), D(5), D('5.5')]


def random_groups(rng):
    rates = rng.sample(RATES, rng.randint(1, 4))
    return rate_groups([(rate, eur(rng.randint(1, 500000))) for rate in rates])


def random_payments(rng, total):
    """Eine Folge von 1 bis 6 Zahlungen (in Cent-Vielfachen), jede höchstens so hoch wie der Rest; sie endet mit der schließenden Zahlung oder
    davor. Auch 1-Cent-Zahlungen kommen vor."""
    left, amounts = total, []
    for _ in range(rng.randint(1, 6)):
        if left == 0:
            break
        if rng.random() < 0.2:
            amount = min(100, left)
        else:
            amount = min(left, eur(rng.randint(1, max(left // 100, 1))))
        amounts.append(amount)
        left -= amount
    if left and rng.random() < 0.7:
        amounts.append(left)                           # schließt
    return amounts


@pytest.mark.parametrize('seed', [1, 2, 3, 4, 5])
def test_every_payment_keeps_the_invariants(seed):
    rng = random.Random(seed)
    for _ in range(60):
        groups = random_groups(rng)
        total = sum(group.gross for group in groups)
        booked = {}
        paid = 0
        for amount in random_payments(rng, total):
            open_before = {group.pct: group.gross - booked.get(group.pct, (0, 0))[0] for group in groups}
            left_tax = {group.pct: group.tax - booked.get(group.pct, (0, 0))[1] for group in groups}
            result = split_payment(groups, booked, amount)

            assert [part.pct for part in result] == [group.pct for group in groups]
            assert sum(part.amount for part in result) == amount                        # Summe der Teile = Betrag
            for part in result:
                assert 0 <= part.amount <= open_before[part.pct]                       # nie über dem Rest, nie unter 0
                assert 0 <= part.tax <= left_tax[part.pct]                             # die Steuer bleibt im Rest
                if part.amount == 0:
                    assert part.tax == 0                                               # nie eine Steuer ohne Betrag
            if len(groups) == 1:                                                       # EIN Satz: alles an ihn, Steuer wie beim Beleg mit einem Satz
                (group,), (part,) = groups, result
                assert part.amount == amount
                closing = amount == open_before[group.pct]
                prorated = round_minor(prorate(group.tax, amount, group.gross), 2)
                assert part.tax == (left_tax[group.pct] if closing else min(prorated, left_tax[group.pct]))
            booked = booked_of(result, booked)
            paid += amount
        if paid == total:                                                              # die letzte Zahlung hat alles geschlossen
            assert booked == {group.pct: (group.gross, group.tax) for group in groups}


def test_the_split_is_pure_and_deterministic():
    booked = {D(19): (eur(100), eur(15))}

    assert split_payment(M, booked, eur(3000)) == split_payment(M, booked, eur(3000))
    assert booked == {D(19): (eur(100), eur(15))}


# ── mixed_groups ─────────────────────────────────────────────────────────────

def test_the_groups_of_an_invoice_come_from_its_positions_and_equal_its_sums():
    pairs = [(19.0, eur(6000)), (7.0, eur(5000)), (19.0, eur(4000))]

    assert mixed_groups(eur(15000), eur(2250), eur(17250), pairs) == M


def test_an_invoice_without_the_marker_rate_form_reads_a_fraction_like_a_percentage():
    assert mixed_groups(eur(15000), eur(2250), eur(17250), [(0.19, eur(10000)), (0.07, eur(5000))]) == M


@pytest.mark.parametrize('pairs, label', [
    ([], 'keine Position'),
    ([(19, eur(10000)), (None, eur(5000))], 'Satz fehlt'),
    ([(19, eur(10000)), ('', eur(5000))], 'Satz leer'),
    ([(19, eur(10000)), (-1, eur(5000))], 'Platzhalter -1'),
    ([(19, eur(10000)), (-7, eur(5000))], 'negativer Satz'),
    ([(19, eur(10000)), (150, eur(5000))], 'Satz über 100'),
    ([(19, eur(10000)), ('sieben', eur(5000))], 'Satz kein Zahlenwert'),
    ([(19, eur(10000)), (True, eur(5000))], 'Satz ein Wahrheitswert'),
    ([(19, eur(10000)), (7, '50.00')], 'Netto als Text'),
    ([(19, eur(10000)), (7, 5000000.0)], 'Netto als Kommazahl'),
    ([(19, eur(10000)), (7, True)], 'Netto ein Wahrheitswert'),
    ([(19, eur(10000)), (7, None)], 'Netto fehlt'),
])
def test_unreadable_positions_are_refused(pairs, label):
    with pytest.raises(RateGroupsMismatch):
        mixed_groups(eur(15000), eur(2250), eur(17250), pairs)


@pytest.mark.parametrize('sums', [
    (eur(15001), eur(2250), eur(17250)),
    (eur(15000), eur(2251), eur(17250)),
    (eur(15000), eur(2250), eur(17251)),
    (None, eur(2250), eur(17250)),
    (eur(15000), '2250', eur(17250)),
])
def test_sums_that_do_not_equal_the_groups_are_refused(sums):
    with pytest.raises(RateGroupsMismatch):
        mixed_groups(*sums, [(19, eur(10000)), (7, eur(5000))])


def test_a_group_does_not_have_to_be_positive_a_credit_note_has_negative_ones():
    groups = mixed_groups(-eur(15000), -eur(2250), -eur(17250), [(19, -eur(10000)), (7, -eur(5000))])

    assert [group.gross for group in groups] == [-eur(11900), -eur(5350)]


def test_the_groups_are_the_ones_of_the_pdf_and_the_e_invoice():
    """Dieselbe Rechnung wie ``tax_groups.item_groups`` (die EINE Stelle für PDF und E-Rechnung)."""
    from tax_groups import item_groups
    pairs = [(19.0, eur(3333)), (7.0, eur(1111)), (19.0, eur(3334)), (7.0, eur(1)), (19.0, 1)]

    groups = item_groups(pairs)
    total = (sum(group.net for group in groups), sum(group.tax for group in groups), sum(group.gross for group in groups))

    assert mixed_groups(*total, pairs) == groups


# ── refund_pool und booked_fault: der Stand je Satz vor der nächsten Erstattung ──────────────────────────────

def test_the_pool_of_a_refund_is_what_the_payments_carried_per_rate_highest_rate_first():
    paid = {D(7): (eur(3100), eur(200)), D(19): (eur(6900), eur(1100))}

    assert refund_pool(paid) == [RateGroup(D(19), 0, eur(1100), eur(6900)), RateGroup(D(7), 0, eur(200), eur(3100))]
    assert refund_pool({}) == [] and refund_pool({D(19): (0, 0), D(7): (eur(100), eur(10))}) == [RateGroup(D(7), 0, eur(10), eur(100))]   # ein Satz ohne Zahlung hat keine Gruppe


@pytest.mark.parametrize('booked', [
    {},
    {D(19): (eur(100), eur(15))},
    {D(19): (eur(11900), eur(1900)), D(7): (eur(5350), eur(350))},       # alles bezahlt, jede Steuer ganz gebucht
    {D(19): (eur(11900), eur(1900)), D(7): (0, 0)},
])
def test_a_stock_that_fits_its_groups_has_no_fault(booked):
    assert booked_fault(M, booked) is None


@pytest.mark.parametrize('booked, rate', [
    ({D(19): (eur(11901), 0)}, D(19)),                                  # mehr gebucht als das Brutto der Gruppe
    ({D(19): (-1, 0)}, D(19)),                                          # negativ
    ({D(7): (0, eur(351))}, D(7)),                                      # mehr Steuer, als die Gruppe hat
    ({D(7): (eur(5350), eur(349))}, D(7)),                              # ganz bezahlt, Steuer aber nicht ganz gebucht: ein Rest von Cent bliebe
    ({D(19): (100.0, 0)}, D(19)),                                       # keine ganze Zahl
    ({D(19): (True, 0)}, D(19)),                                        # ein Wahrheitswert ist keine Zahl
    ({D(5): (100, 0)}, D(5)),                                           # ein Satz, den die Gruppen nicht tragen
])
def test_every_way_a_stock_can_miss_its_groups_names_the_rate(booked, rate):
    fault = booked_fault(M, booked)

    assert fault is not None and fault[0] == rate and fault[1]          # der Satz und ein Satz für das Server-Log


def test_the_first_rate_in_the_order_of_the_groups_is_named_and_an_unknown_rate_comes_last():
    assert booked_fault(M, {D(7): (eur(5351), 0), D(19): (eur(11901), 0)})[0] == D(19)
    assert booked_fault(M, {D(5): (100, 0), D(7): (eur(5351), 0)})[0] == D(7)
    assert booked_fault(M, {D(5): (100, 0), D(3): (100, 0)})[0] == D(5)


def test_the_refusal_of_split_payment_is_exactly_the_fault_of_the_stock():
    """``split_payment`` benutzt ``booked_fault`` als Eingangsprüfung: Hat ein Stand einen Fehler, lehnt es ab, und passt er, nimmt es einen Betrag, der noch offen
    ist. Zufallsstände (fester Seed) aus gültigen Zahlungsfolgen, dazu der Stand mit je einem Wert verschoben."""
    rng = random.Random(77)
    checked = faulty = 0
    for _ in range(300):
        groups = random_groups(rng)
        booked = {}
        for amount in random_payments(rng, sum(group.gross for group in groups)):
            if rng.random() < 0.3:
                break
            booked = booked_of(split_payment(groups, booked, amount), booked)
        victim = rng.choice(groups).pct
        for change in (None, (rng.choice([-100, 100, eur(100)]), 0), (0, rng.choice([-100, 100])), ('rate', 0)):
            state = dict(booked)
            if change == ('rate', 0):
                state[D(rng.choice([3, 11]))] = (100, 0)                 # ein Satz, den der Beleg nicht trägt
            elif change is not None:
                done_amount, done_tax = state.get(victim, (0, 0))
                state[victim] = (done_amount + change[0], done_tax + change[1])
            fault = booked_fault(groups, state)
            open_total = sum(group.gross - state.get(group.pct, (0, 0))[0] for group in groups)
            try:
                split_payment(groups, state, 1)
                raised = False
            except ValueError:
                raised = True
            if fault is not None:
                assert raised, (groups, state)
                faulty += 1
            elif open_total >= 1:
                assert not raised, (groups, state)
            checked += 1
    assert checked == 1200 and faulty > 100                                  # der Test prüft beide Seiten


# ── booking_rate und pct_text ────────────────────────────────────────────────

def test_the_booking_rate_is_the_fraction_the_form_of_a_single_rate_invoice_sends():
    assert booking_rate(D(19)) == 0.19 and booking_rate(D(7)) == 0.07 and booking_rate(D('5.5')) == 0.055
    assert type(booking_rate(D(19))) is float
    assert booking_rate(D(0)) is None and booking_rate(D('0.00')) is None             # kein Satz: wie ``_booking_tax_rate``


def test_the_text_of_a_rate_has_a_comma_and_no_superfluous_zeros():
    assert pct_text(D(19)) == '19' and pct_text(D(7)) == '7'
    assert pct_text(D('5.5')) == '5,5'
    assert pct_text(D('1E+1')) == '10'                                                # ``normalize`` schriebe 10 als 1E+1
    assert pct_text(D('1E+2')) == '100'
