# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""``money.parse_amount``: Beträge aus Dateien streng lesen (Textexport, Kontoauszug), und ``money.to_minor``
lehnt NaN und Infinity ab.

``float()`` nähme ``nan``, ``inf``, ``1e5``, ``1_000`` und Ziffern anderer Schriften an; ``parse_amount`` kennt nur
ASCII-Ziffern, ein Vorzeichen vorn und zwei Schreibweisen: deutsch (``1.234,56``) oder ohne Komma (``1234.56``).
"""
from decimal import Decimal

import pytest

import money
from money import parse_amount


@pytest.mark.parametrize('text, expected', [
    ('1.234,56', Decimal('1234.56')),
    ('-41,25', Decimal('-41.25')),
    ('+3,5', Decimal('3.5')),
    ('1234.56', Decimal('1234.56')),
    ('0', Decimal('0')),
    ('12', Decimal('12')),
    ('-1.234.567,89', Decimal('-1234567.89')),
    ('0,50', Decimal('0.50')),
    ('1234,56', Decimal('1234.56')),          # deutsch ohne Tausenderpunkte
    ('-0,01', Decimal('-0.01')),
    ('  -41,25  ', Decimal('-41.25')),        # Leerraum außen fällt weg
    ('\t1.000,00\n', Decimal('1000.00')),
])
def test_valid_amounts_are_read_exactly(text, expected):
    result = parse_amount(text)
    assert isinstance(result, Decimal)
    assert result == expected


def test_the_decimal_keeps_its_scale():
    """Kein Umweg über float: ``0,10`` bleibt ``0.10`` und ``0.1 + 0.2`` ist exakt ``0.3``."""
    assert str(parse_amount('0,10')) == '0.10'
    assert parse_amount('0,1') + parse_amount('0,2') == parse_amount('0,3')


def test_a_number_without_a_comma_has_a_dot_as_the_decimal_point():
    """Wie bisher gelesen und bewusst nicht geändert: ``1.234`` ohne Komma ist 1,234 – kein Tausenderpunkt."""
    assert parse_amount('1.234') == Decimal('1.234')
    assert money.to_minor(parse_amount('1.234')) == 12340


@pytest.mark.parametrize('text', [
    'nan', 'NaN', '-nan', 'inf', '-inf', 'Infinity', '+Infinity', 'sNaN',
    '1e5', '1E5', '1,5e3', '1e-2',
    '1_000', '1_000,00',
    '١٢', '١٢,٣٤', '１２,３４',                  # Ziffern anderer Schriften (arabisch-indisch, vollbreit)
    '1.2.3,4', '1,2,3', '12,', ',5', '.5', '5.', '1.234.567', '12.34,56',
    '1 234,56', '1 234,56', '12 ,5',          # Leerraum innen (auch geschütztes Leerzeichen)
    '', ' ', '\n',
    '--1', '+-1', '-+1', '-', '+', ',', '.',
    '0x10', '0b1', '12abc', 'abc', '1,5 €', '€ 1,5', '12,5%', '(12,5)',
    '1,234.56',                                    # englische Tausenderkommas
])
def test_everything_else_is_refused(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_a_value_of_more_than_30_characters_is_refused():
    assert parse_amount('0' * 18 + '1' * 12) == Decimal('1' * 12)        # 30 Zeichen
    with pytest.raises(ValueError):
        parse_amount('0' * 19 + '1' * 12)                                # 31 Zeichen
    with pytest.raises(ValueError):
        parse_amount('0,' + '1' * 29)             # 31 Zeichen
    assert parse_amount('0,' + '1' * 28) == Decimal('0.' + '1' * 28)


def test_a_huge_padded_value_is_cheap_and_does_not_echo_the_whole_text():
    with pytest.raises(ValueError) as caught:
        parse_amount(' ' * 1_000_000 + 'x' + ' ' * 1_000_000)
    assert len(str(caught.value)) < 200


@pytest.mark.parametrize('value', [None, 5, 5.5, Decimal('5'), b'5', ['5']])
def test_only_text_is_read(value):
    with pytest.raises(ValueError):
        parse_amount(value)


# ── to_minor lehnt NaN und Infinity ab ────────────────────────────────────────

@pytest.mark.parametrize('value', [
    float('nan'), float('inf'), float('-inf'),
    Decimal('NaN'), Decimal('sNaN'), Decimal('Infinity'), Decimal('-Infinity'),
    'nan', 'NaN', 'Infinity', '-Infinity', 'inf', ' inf ',
])
def test_to_minor_refuses_values_that_are_not_finite(value):
    with pytest.raises(ValueError):
        money.to_minor(value)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), Decimal('NaN'), 'nan', 'Infinity'])
def test_the_other_entry_points_refuse_them_too(value):
    """``_to_decimal`` ist die eine Stelle: ``multiply``, ``tax_from_net`` und ``fmt_eur`` laufen darüber."""
    with pytest.raises(ValueError):
        money.multiply(10000, value)
    with pytest.raises(ValueError):
        money.tax_from_net(10000, value)
    with pytest.raises(ValueError):
        money.fmt_eur(value)


@pytest.mark.parametrize('value, expected', [
    (0, 0), (1.5, 15000), (Decimal('1.5'), 15000), ('1.234,56', 12345600), ('1234.56', 12345600),
    (-5.5, -55000), ('1e2', 1000000),
])
def test_finite_values_are_still_read_as_before(value, expected):
    assert money.to_minor(value) == expected


@pytest.mark.parametrize('text, value', [
    ('999999999999,99', '999999999999.99'), ('-999.999.999.999,99', '-999999999999.99'), ('999999999999.9999', '999999999999.9999'),
])
def test_an_amount_below_a_trillion_is_read(text, value):
    assert parse_amount(text) == Decimal(value)


@pytest.mark.parametrize('text', ['1000000000000', '1.000.000.000.000,00', '-1000000000000,00', '1' * 25, '9' * 30])
def test_an_amount_of_a_trillion_or_more_is_refused(text):
    """Solche Werte liefen später in ``to_minor`` (InvalidOperation) oder beim Speichern (64 Bit) in einen technischen
    Fehler – sie sind ein gewöhnlicher ``ValueError`` wie jeder andere ungültige Betrag."""
    with pytest.raises(ValueError):
        parse_amount(text)
