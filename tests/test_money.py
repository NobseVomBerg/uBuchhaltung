"""Tests fuer das zentrale Geld-Modul (money.py, Festkomma SCALE=4)."""
from decimal import Decimal

import pytest

import money


# ── to_minor: Parsing & Rundung ───────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (0, 0),
    (1, 10000),
    (1.0, 10000),
    (Decimal("1"), 10000),
    ("1", 10000),
    (12.34, 123400),
    ("12.34", 123400),
    ("12,34", 123400),                 # deutsches Komma
    ("1.234,56", 12345600),            # deutsch mit Tausenderpunkt
    ("1234.56", 12345600),             # englisch
    (Decimal("0.0001"), 1),            # kleinste Minor Unit
    (-5.5, -55000),
    ("-1.234,5678", -12345678),
])
def test_to_minor_parsing(value, expected):
    assert money.to_minor(value) == expected


def test_to_minor_half_up_rounding():
    # 0.00005 EUR -> 0.5 Minor Units -> HALF_UP -> 1
    assert money.to_minor(Decimal("0.00005")) == 1
    # 0.00004 -> 0.4 -> 0
    assert money.to_minor(Decimal("0.00004")) == 0
    # negatives runden ebenfalls kaufmaennisch vom Betrag weg
    assert money.to_minor(Decimal("-0.00005")) == -1


def test_to_minor_float_no_binary_artifacts():
    # 0.1 + 0.2 als Eingabe darf nicht zu 0.30000000000000004 fuehren
    assert money.to_minor(0.1) + money.to_minor(0.2) == money.to_minor(0.3)


@pytest.mark.parametrize("bad", ["", "   ", "abc", "1.2.3x"])
def test_to_minor_invalid_string_raises(bad):
    with pytest.raises(ValueError):
        money.to_minor(bad)


@pytest.mark.parametrize("bad", [None, True, [1], {}])
def test_to_minor_bad_type_raises(bad):
    with pytest.raises((TypeError, ValueError)):
        money.to_minor(bad)


# ── from_minor & Round-Trip ───────────────────────────────────────────────────

def test_from_minor():
    assert money.from_minor(123400) == Decimal("12.3400")
    assert money.from_minor(1) == Decimal("0.0001")
    assert money.from_minor(-55000) == Decimal("-5.5000")


@pytest.mark.parametrize("euro", ["0.0000", "12.3456", "1000000.0001", "-7.5000"])
def test_round_trip(euro):
    assert money.from_minor(money.to_minor(euro)) == Decimal(euro)


# ── Summen exakt (Kernmotivation) ─────────────────────────────────────────────

def test_sum_is_exact():
    cents = [money.to_minor("0.10") for _ in range(10)]
    assert sum(cents) == money.to_minor("1.00")


# ── multiply ──────────────────────────────────────────────────────────────────

def test_multiply_integer_quantity():
    assert money.multiply(money.to_minor("10.00"), 3) == money.to_minor("30.00")


def test_multiply_fractional_quantity():
    # 1,5 Stunden * 80,00 EUR = 120,00
    assert money.multiply(money.to_minor("80.00"), Decimal("1.5")) == money.to_minor("120.00")


def test_multiply_rounds_half_up():
    # 3 * 0.3333 EUR = 0.9999 (kein Rundungsbedarf bei SCALE 4)
    assert money.multiply(money.to_minor("0.3333"), 3) == money.to_minor("0.9999")


# ── Steuerberechnung ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("net, rate, tax", [
    ("100.00", 19, "19.00"),
    ("100.00", 7, "7.00"),
    ("19.99", 19, "3.7981"),
    ("0.00", 19, "0.00"),
])
def test_tax_from_net(net, rate, tax):
    assert money.tax_from_net(money.to_minor(net), rate) == money.to_minor(tax)


@pytest.mark.parametrize("gross, rate, tax", [
    ("119.00", 19, "19.00"),
    ("107.00", 7, "7.00"),
    ("27.20", 19, "4.3429"),
])
def test_tax_from_gross(gross, rate, tax):
    assert money.tax_from_gross(money.to_minor(gross), rate) == money.to_minor(tax)


def test_gross_from_net():
    assert money.gross_from_net(money.to_minor("100.00"), 19) == money.to_minor("119.00")


def test_tax_rate_as_float_and_decimal():
    n = money.to_minor("100.00")
    assert money.tax_from_net(n, 19.0) == money.tax_from_net(n, Decimal("19"))


# ── round_minor (Rundung auf Cent o.ae.) ──────────────────────────────────────

@pytest.mark.parametrize("minor, dp, expected", [
    (37035, 2, 37000),                 # 3,7035 -> 3,70
    (37050, 2, 37100),                 # 3,7050 -> 3,71 (HALF_UP)
    (37049, 2, 37000),                 # 3,7049 -> 3,70
    (-37050, 2, -37100),               # negativ ebenfalls vom Betrag weg
    (123400, 2, 123400),               # bereits glatt
    (12345, 4, 12345),                 # dp == SCALE -> unveraendert
    (12345, 0, 10000),                 # auf ganze Euro: 1,2345 -> 1
    (15000, 0, 20000),                 # 1,5 -> 2 (HALF_UP)
])
def test_round_minor(minor, dp, expected):
    assert money.round_minor(minor, dp) == expected


def test_round_minor_default_is_cents():
    assert money.round_minor(money.to_minor("3.7035")) == money.to_minor("3.70")


# ── Formatierung ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("minor, dp, expected", [
    (money.to_minor("1234.56"), 2, "1.234,56"),
    (money.to_minor("0.00"), 2, "0,00"),
    (money.to_minor("-1234.50"), 2, "-1.234,50"),
    (money.to_minor("1000000.00"), 2, "1.000.000,00"),
    (money.to_minor("12.3456"), 4, "12,3456"),
    (money.to_minor("5.00"), 0, "5"),
])
def test_format_de(minor, dp, expected):
    assert money.format_de(minor, dp) == expected


@pytest.mark.parametrize("minor, dp, expected", [
    (money.to_minor("1234.56"), 2, "1234.56"),
    (money.to_minor("12.3456"), 4, "12.3456"),
    (money.to_minor("0.005"), 2, "0.01"),    # Anzeige rundet HALF_UP
    (money.to_minor("-7.5"), 2, "-7.50"),
])
def test_format_plain(minor, dp, expected):
    assert money.format_plain(minor, dp) == expected


def test_display_rounding_does_not_change_storage():
    # gespeichert wird 4-stellig, Anzeige rundet nur die Darstellung
    m = money.to_minor("12.3456")
    assert money.format_de(m, 2) == "12,35"
    assert money.from_minor(m) == Decimal("12.3456")
