# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Steuer eines Belegs je Satzgruppe (``tax_groups.py``).

* feste Werte: Gruppen, Rundung aus der Nettosumme der Gruppe, Reihenfolge, beliebig viele und beliebige Sätze (auch 5,5 % und 0 %);
* Eigenschaft mit festem Seed: ``server.handlers.recompute_invoice_totals`` liefert für zufällige Positionen dieselben vier Rückgabewerte wie
  die Formel vor ``tax_groups`` (eine Kopie davon steht in diesem Test) – am Stand 2.6.7 gegen den Code von damals geschrieben;
* dieselbe Steuer in den Summen der Rechnung und in der E-Rechnung (``export.einvoice_model.build_einvoice``);
* ``rate_pct`` gegen ``export.einvoice_model.rate_to_pct`` und ``server.handlers._rate_to_pct``.

Beträge und Positionen sind erfunden.
"""
import dataclasses
import itertools
import random
from decimal import Decimal

import pytest

from export.einvoice_model import build_einvoice, rate_to_pct
from money import from_minor, multiply, round_minor, tax_from_net, to_minor
from server.handlers import _rate_to_pct, recompute_invoice_totals
from tax_groups import RateGroup, rate_groups, rate_pct


def euro(text):
    return to_minor(text)


def group(pct, net, tax):
    return RateGroup(Decimal(pct), euro(net), euro(tax), euro(net) + euro(tax))


# ── Feste Werte ──────────────────────────────────────────────────────────────

def test_two_rates_make_two_groups_each_with_its_own_tax():
    groups = rate_groups([(Decimal('19'), euro('100.00')), (Decimal('7'), euro('50.00'))])

    assert groups == [group('19', '100.00', '19.00'), group('7', '50.00', '3.50')]
    assert [(g.net, g.tax, g.gross) for g in groups] == [
        (euro('100.00'), euro('19.00'), euro('119.00')), (euro('50.00'), euro('3.50'), euro('53.50'))]


def test_the_tax_of_a_group_comes_from_the_sum_of_its_net_amounts_not_from_each_position():
    """Drei Positionen zu 0,10 € mit 7 %: Aus der Summe (0,30 €) sind es 0,021 € – 0,02 €; je Position gerundet wären es drei Mal 0,01 €."""
    groups = rate_groups([(Decimal('7'), euro('0.10'))] * 3)

    assert groups == [group('7', '0.30', '0.02')]
    assert sum(round_minor(tax_from_net(euro('0.10'), 7), 2) for _ in range(3)) == euro('0.03')


def test_a_group_of_zero_percent_has_no_tax():
    assert rate_groups([(Decimal('0'), euro('100.00'))]) == [group('0', '100.00', '0.00')]


def test_four_different_rates_including_five_and_a_half_percent():
    lines = [(Decimal('5.5'), euro('200.00')), (Decimal('19'), euro('100.00')),
             (Decimal('0'), euro('10.00')), (Decimal('7'), euro('50.00'))]

    assert rate_groups(lines) == [group('19', '100.00', '19.00'), group('7', '50.00', '3.50'),
                                  group('5.5', '200.00', '11.00'), group('0', '10.00', '0.00')]


@pytest.mark.parametrize('count', range(1, 13))
def test_any_number_of_rates_makes_that_many_groups(count):
    """Keine Zahl von Sätzen ist fest vorgegeben: ``count`` verschiedene Sätze, mehrere Positionen je Satz."""
    rates = [Decimal(str(value)) for value in (19, 7, 5.5, 0, 16, 9.9, 0.5, 25, 3, 12.5, 1, 22)][:count]
    lines = [(rate, euro('33.33') + index) for index, rate in enumerate(rates)] * 2

    groups = rate_groups(lines)

    assert [g.pct for g in groups] == sorted(rates, reverse=True)
    assert all(g.net == 2 * (euro('33.33') + rates.index(g.pct)) for g in groups)
    assert all(g.tax == round_minor(tax_from_net(g.net, g.pct), 2) and g.gross == g.net + g.tax for g in groups)


def test_the_order_is_descending_by_rate_whatever_the_order_of_the_positions():
    lines = [(Decimal('5.5'), euro('200.00')), (Decimal('19'), euro('100.00')),
             (Decimal('0'), euro('10.00')), (Decimal('7'), euro('50.00'))]
    expected = rate_groups(lines)

    assert [g.pct for g in expected] == [Decimal('19'), Decimal('7'), Decimal('5.5'), Decimal('0')]
    for permutation in itertools.permutations(lines):
        assert rate_groups(permutation) == expected


def test_equal_rates_form_one_group_even_when_written_differently():
    groups = rate_groups([(Decimal('19.00'), euro('59.97')), (Decimal('19'), euro('0.01'))])

    assert len(groups) == 1
    assert (groups[0].net, groups[0].tax, groups[0].gross) == (euro('59.98'), euro('11.40'), euro('71.38'))   # 11,3962 → 11,40


def test_a_group_with_a_net_of_zero_stays():
    assert rate_groups([(Decimal('19'), 0)]) == [RateGroup(Decimal('19'), 0, 0, 0)]
    assert rate_groups([(Decimal('19'), euro('50.00')), (Decimal('19'), -euro('50.00'))]) == [RateGroup(Decimal('19'), 0, 0, 0)]


def test_no_positions_no_groups():
    assert rate_groups([]) == []
    assert rate_groups(iter(())) == []


def test_negative_amounts_give_a_negative_tax_rounded_the_same_way():
    """Eine Stornorechnung trägt negative Nettobeträge; gerundet wird kaufmännisch (halb weg von null)."""
    assert rate_groups([(Decimal('19'), -euro('100.00'))]) == [group('19', '-100.00', '-19.00')]
    assert rate_groups([(Decimal('5'), euro('0.10'))])[0].tax == euro('0.01')           # 0,005 → 0,01
    assert rate_groups([(Decimal('5'), -euro('0.10'))])[0].tax == -euro('0.01')


def test_a_group_cannot_be_changed():
    with pytest.raises(dataclasses.FrozenInstanceError):
        rate_groups([(Decimal('19'), euro('1.00'))])[0].tax = 1


def test_random_positions_the_groups_add_up_and_follow_the_rule():
    rng = random.Random(20260702)
    rates = [Decimal(str(value)) for value in (0, 5, 5.5, 7, 16, 19)]
    for _ in range(300):
        lines = [(rng.choice(rates), rng.randint(-5_000_000, 50_000_000)) for _ in range(rng.randint(0, 40))]

        groups = rate_groups(lines)

        assert [g.pct for g in groups] == sorted({pct for pct, _net in lines}, reverse=True)
        assert sum(g.net for g in groups) == sum(net for _pct, net in lines)
        for g in groups:
            net = sum(net for pct, net in lines if pct == g.pct)
            assert (g.net, g.tax, g.gross) == (net, round_minor(tax_from_net(net, g.pct), 2), net + g.tax)


# ── recompute_invoice_totals: dieselben Zahlen wie vor tax_groups ─────────────

def _old_recompute_invoice_totals(items, default_rate_pct):
    """Die Formel von ``server.handlers.recompute_invoice_totals`` am Stand 2.6.7, vor ``tax_groups`` (aus dem Code von damals kopiert)."""
    line_net_minors = []
    net_by_rate = {}                      # Decimal(rate_pct) -> Netto-Summe (Minor)
    for item in items:
        qty = item.get('quantity')
        qty = 1 if qty in (None, '') else qty
        price_minor = to_minor(item.get('unitPrice') or 0)
        line_net = round_minor(multiply(price_minor, qty), 2)
        line_net_minors.append(line_net)
        rate = _rate_to_pct(item.get('taxRate'), default_rate_pct)
        net_by_rate[rate] = net_by_rate.get(rate, 0) + line_net

    sum_net_minor = sum(line_net_minors)
    tax_minor = sum(round_minor(tax_from_net(net, rate), 2)
                    for rate, net in net_by_rate.items())
    gross_minor = sum_net_minor + tax_minor

    return (from_minor(sum_net_minor), from_minor(tax_minor),
            from_minor(gross_minor), [from_minor(n) for n in line_net_minors])


RATES = [0, 5, 5.5, 7, 16, 19]
QUANTITIES = [None, '', 1, 2, 3, 0.5, '2.5', 10, 1.25, 0.1, 7, 100]
PRICES = [0, None, '', 0.01, 0.1, 1, 1.5, 9.99, 19.99, '1.2345', 100, 249.9, '0.0049', 1234.56, 3333.33]


def _random_items(rng, count):
    """Positionen, wie das Formular sie schickt – den Satz mal als Prozentzahl, mal als Bruch, mal als Text, mal gar nicht."""
    items = []
    for _ in range(count):
        item = {'quantity': rng.choice(QUANTITIES), 'unitPrice': rng.choice(PRICES)}
        rate = rng.choice(RATES)
        form = rng.choice(('number', 'fraction', 'text', 'missing'))
        if form == 'number':
            item['taxRate'] = rate
        elif form == 'fraction':
            item['taxRate'] = round(rate / 100, 4)
        elif form == 'text':
            item['taxRate'] = str(rate)
        items.append(item)
    return items


def test_recompute_gives_the_same_four_values_as_the_formula_before_tax_groups():
    rng = random.Random(20260702)
    for _ in range(400):
        items = _random_items(rng, rng.randint(1, 40))
        default = _rate_to_pct(rng.choice([19, 7, 0, 5.5, 0.19]))

        new = recompute_invoice_totals(items, default)
        old = _old_recompute_invoice_totals(items, default)

        assert new == old
        assert repr(new) == repr(old)                                  # auch die Darstellung der Decimal-Werte


def test_recompute_without_positions_is_zero():
    assert recompute_invoice_totals([], Decimal('19')) == _old_recompute_invoice_totals([], Decimal('19')) == (
        Decimal('0.0000'), Decimal('0.0000'), Decimal('0.0000'), [])


def test_recompute_rounds_the_tax_per_rate_group():
    items = [{'quantity': 1, 'unitPrice': 100.00, 'taxRate': 19}, {'quantity': 1, 'unitPrice': 50.00, 'taxRate': 7},
             {'quantity': 1, 'unitPrice': 0.10, 'taxRate': 7}, {'quantity': 1, 'unitPrice': 0.10, 'taxRate': 7},
             {'quantity': 1, 'unitPrice': 0.10, 'taxRate': 7}]

    net, tax, gross, lines = recompute_invoice_totals(items, Decimal('19'))

    assert (net, tax, gross) == (Decimal('150.3000'), Decimal('22.5200'), Decimal('172.8200'))   # 19,00 + (50,30 × 7 % = 3,521 → 3,52)
    assert lines == [Decimal('100.0000'), Decimal('50.0000'), Decimal('0.1000'), Decimal('0.1000'), Decimal('0.1000')]


# ── Dieselbe Steuer in den Summen und in der E-Rechnung ──────────────────────

def _invoice_and_items(items, header_rate=0.19):
    """Die Zeile einer Rechnung und ihrer Positionen so, wie ``build_einvoice`` sie liest (Spalten laut ``db/schema.py``), mit den Summen,
    die ``recompute_invoice_totals`` für ``items`` ergibt."""
    net, tax, gross, line_nets = recompute_invoice_totals(items, _rate_to_pct(header_rate))
    invoice = [None] * 55
    invoice[0], invoice[1], invoice[2], invoice[24] = 1, 'T-0001', '2026-03-01', 'EUR'
    invoice[35], invoice[36], invoice[37], invoice[38] = header_rate, net, tax, gross
    rows = []
    for position, (item, line_net) in enumerate(zip(items, line_nets), start=1):
        row = [None] * 11
        row[2], row[4], row[5], row[6] = position, f'Position {position}', item.get('quantity') or 1, 'Stk.'
        row[7], row[8], row[10] = item.get('unitPrice') or 0, line_net, item.get('taxRate')
        rows.append(tuple(row))
    return tuple(invoice), rows, (net, tax, gross)


def test_the_einvoice_breakdown_has_one_entry_per_rate_with_the_tax_of_the_group():
    items = [{'quantity': 1, 'unitPrice': 100.00, 'taxRate': 19}, {'quantity': 1, 'unitPrice': 50.00, 'taxRate': 7},
             {'quantity': 3, 'unitPrice': 0.10, 'taxRate': 7}, {'quantity': 1, 'unitPrice': 200.00, 'taxRate': 5.5},
             {'quantity': 1, 'unitPrice': 10.00, 'taxRate': 0}]
    invoice, rows, (net, tax, gross) = _invoice_and_items(items)

    model = build_einvoice(None, invoice, rows)

    assert [(b.category, b.rate, b.basis, b.amount) for b in model.tax_breakdown] == [
        ('S', Decimal('5.5'), euro('200.00'), euro('11.00')),
        ('S', Decimal('7'), euro('50.30'), euro('3.52')),
        ('S', Decimal('19'), euro('100.00'), euro('19.00')),
        ('Z', Decimal('0'), euro('10.00'), 0)]
    assert (model.totals.line_total, model.totals.tax_total, model.totals.grand_total) == (to_minor(net), to_minor(tax), to_minor(gross))
    assert model.warnings == []                                        # die gespeicherten Summen stimmen mit denen aus den Positionen


def test_the_einvoice_and_the_stored_totals_always_agree_on_the_tax():
    """Für zufällige Positionen mit gemischten Sätzen: Steuer, Netto und Brutto der E-Rechnung sind die Summen der Rechnung (sonst nennt
    das Modell eine Warnung), und die Aufschlüsselung trägt je Satz die Steuer von ``rate_groups``."""
    rng = random.Random(20260702)
    for _ in range(150):
        items = _random_items(rng, rng.randint(1, 25))
        for item in items:
            item.setdefault('taxRate', 19)                              # das Modell liest den Satz der Position, nicht den Standard
        invoice, rows, (net, tax, gross) = _invoice_and_items(items)

        model = build_einvoice(None, invoice, rows)

        assert (model.totals.line_total, model.totals.tax_total, model.totals.grand_total) == (
            to_minor(net), to_minor(tax), to_minor(gross))
        assert model.warnings == []
        taxed = {g.pct: g.tax for g in rate_groups((rate_to_pct(item['taxRate']), to_minor(line_net))
                                                   for item, line_net in zip(items, [r[8] for r in rows]))}
        assert {b.rate: b.amount for b in model.tax_breakdown if b.category == 'S'} == {
            pct: amount for pct, amount in taxed.items() if pct > 0}


# ── rate_pct ─────────────────────────────────────────────────────────────────

# Werte, bei denen beide heutigen Funktionen dieselbe Zahl liefern (höchstens zwei Nachkommastellen in Prozent)
SAME_NUMBER = [0, 0.0, '0.00', 0.07, 0.19, 0.055, 0.5, 1, '1', 1.0, 7, 19, 19.0, '19', '19.00', 5.5, '7.00', 16, 0.16, 100, -1, '-1', -0.0,
               Decimal('0.19'), Decimal('19'), '0.0555']
# Werte, bei denen sie sich unterscheiden: mehr als zwei Nachkommastellen in Prozent – ``rate_to_pct`` rundet auf zwei, ``_rate_to_pct`` nicht
DIFFERENT_NUMBER = ['5.555', '0.05555', '19.999', 5.555, Decimal('19.004')]
# Werte, die ``rate_to_pct`` ablehnt (``InvalidOperation``) und ``_rate_to_pct`` zum Teil durchlässt (unendlich, riesig)
REFUSED = ['abc', 'NaN', ' ', True, '5,5', 'inf', 'Infinity', 1e30, 1e500]


def _outcome(function, value):
    try:
        return 'value', function(value)
    except Exception as error:                                          # jede Ausnahme zählt, die Art steht im Vergleich
        return 'raises', type(error)


@pytest.mark.parametrize('value', SAME_NUMBER, ids=repr)
def test_rate_pct_is_the_number_both_existing_functions_give(value):
    assert rate_pct(value) == rate_to_pct(value) == _rate_to_pct(value)


@pytest.mark.parametrize('value', [None, ''], ids=repr)
def test_without_a_rate_there_is_none_and_the_caller_picks_the_default(value):
    assert rate_pct(value) is None
    assert rate_to_pct(value, None) is None and _rate_to_pct(value, None) is None


@pytest.mark.parametrize('value', SAME_NUMBER + DIFFERENT_NUMBER + REFUSED + [None, ''], ids=repr)
def test_rate_pct_follows_the_rule_of_the_einvoice_model_in_every_case(value):
    """Gleiche Zahl, gleiche Darstellung, oder die gleiche Ausnahme – ohne den Vorgabewert (``None`` statt 19)."""
    expected = _outcome(lambda v: rate_to_pct(v, None), value)
    actual = _outcome(rate_pct, value)

    assert actual == expected
    if actual[0] == 'value' and actual[1] is not None:
        assert repr(actual[1]) == repr(expected[1])


@pytest.mark.parametrize('value', DIFFERENT_NUMBER, ids=repr)
def test_the_handler_function_is_deliberately_not_unified_with_it(value):
    """``server.handlers._rate_to_pct`` liefert die Zahl, wie sie dasteht; ``rate_pct`` (wie ``rate_to_pct``) rundet auf zwei Stellen. Beide
    bleiben, wie sie sind: Die Summen der Rechnung und die E-Rechnung behalten ihre Zahlen. Wer sie vereinheitlicht, entscheidet das
    bewusst – und nimmt diesen Test mit."""
    assert rate_pct(value) != _rate_to_pct(value)


@pytest.mark.parametrize('value', ['inf', 'Infinity', 1e30], ids=repr)
def test_the_handler_function_lets_an_infinite_or_huge_rate_through_where_the_other_refuses(value):
    assert _outcome(_rate_to_pct, value)[0] == 'value'
    assert _outcome(rate_pct, value)[0] == 'raises'
