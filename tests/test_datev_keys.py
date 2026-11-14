# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Regeln für den BU-Schlüssel des DATEV-Stapels (``db/datev_keys.py``, seit 2.3.0).

Reine Funktionen, keine Datenbank: die Tabelle (jede Zelle, innerhalb und außerhalb der befristeten Sätze 2020,
die Grenztage), ihre Umkehrung für den Textimport, die Art einer Buchung (``booking_kind``), die Spalte 40
(``is_vat_row``), das Belegfeld und die strengen Prüfungen der Werte, die in den Stapel gehen.

Die Zahlen sind erfunden; die Regeln sind die, die der Export nach WISOs Stapel nachbildet.
"""
import datetime

import pytest

from db import datev_keys as dk
from db.invoices import InvoicesMixin

# (Art, Satz in Prozent) → Schlüssel, jeweils außerhalb und innerhalb der befristeten Sätze
OUTSIDE = {
    (dk.KIND_INPUT, 19): 401, (dk.KIND_INPUT, 7): 402, (dk.KIND_INPUT, 16): 7, (dk.KIND_INPUT, 5): 6,
    (dk.KIND_VAT, 19): 101, (dk.KIND_VAT, 7): 102, (dk.KIND_VAT, 16): 5, (dk.KIND_VAT, 5): 4,
}
INSIDE = {
    (dk.KIND_INPUT, 16): 401, (dk.KIND_INPUT, 5): 402, (dk.KIND_INPUT, 19): 7, (dk.KIND_INPUT, 7): 6,
    (dk.KIND_VAT, 16): 101, (dk.KIND_VAT, 5): 102, (dk.KIND_VAT, 19): 5, (dk.KIND_VAT, 7): 4,
}
DAY_OUTSIDE, DAY_INSIDE = '2026-03-15', '2020-08-15'
CELLS = ([pytest.param(kind, pct, DAY_OUTSIDE, key, id=f'{kind}-{pct}-außerhalb')
          for (kind, pct), key in OUTSIDE.items()]
         + [pytest.param(kind, pct, DAY_INSIDE, key, id=f'{kind}-{pct}-innerhalb')
            for (kind, pct), key in INSIDE.items()])


# ── Die Tabelle ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('kind, pct, day, key', CELLS)
def test_every_cell_of_the_table(kind, pct, day, key):
    assert dk.derive_key(kind, pct, day) == key


@pytest.mark.parametrize('kind, pct, day, key', CELLS)
def test_the_rate_may_be_a_fraction_or_a_percentage(kind, pct, day, key):
    """Der Satz steht als Bruch (0.19) oder als Prozentzahl (19) in den Büchern."""
    assert dk.derive_key(kind, pct / 100, day) == key
    assert dk.derive_key(kind, float(pct), day) == key
    assert dk.derive_key(kind, round(pct / 100, 2), day) == key


@pytest.mark.parametrize('day, inside', [
    ('2020-06-30', False), ('2020-07-01', True), ('2020-12-31', True), ('2021-01-01', False),
    ('2019-12-31', False), ('2020-01-01', False), ('2022-07-01', False)])
def test_the_swap_runs_from_july_first_to_december_31st_inclusive(day, inside):
    assert dk.in_swap(day) is inside
    assert dk.derive_key(dk.KIND_INPUT, 0.19, day) == (7 if inside else 401)
    assert dk.derive_key(dk.KIND_VAT, 0.16, day) == (101 if inside else 5)


@pytest.mark.parametrize('day', [datetime.date(2020, 9, 9), datetime.datetime(2020, 9, 9, 23, 59),
                                 '2020-09-09', '2020-09-09 12:30:00', '2020-09-09T12:30:00'])
def test_the_date_may_be_a_date_a_datetime_or_iso_text(day):
    assert dk.in_swap(day) is True
    assert dk.derive_key(dk.KIND_INPUT, 0.16, day) == 401


@pytest.mark.parametrize('day', [None, '', 'x', '31.12.2020', '2020-13-01', '2020-02-30', 20200901, b'2020-09-09'])
def test_an_unreadable_date_counts_as_outside_the_swap(day):
    assert dk.in_swap(day) is False
    assert dk.derive_key(dk.KIND_INPUT, 0.19, day) == 401


@pytest.mark.parametrize('rate', [None, 0, 0.0, -1, -0.19, '', 'x', 0.107, 0.1899, 0.0701, 0.25, 0.01, 0.2, 100,
                                  float('nan'), float('inf'), 5.5, 0.055])
@pytest.mark.parametrize('kind', [dk.KIND_INPUT, dk.KIND_VAT])
def test_no_rate_zero_percent_or_an_unknown_rate_gives_no_key(kind, rate):
    """Nie der Schlüssel eines anderen Satzes."""
    for day in (DAY_OUTSIDE, DAY_INSIDE):
        assert dk.derive_key(kind, rate, day) == 0


@pytest.mark.parametrize('kind', [dk.KIND_NONE, dk.KIND_INVOICE_CHAIN, 'unbekannt', None])
def test_a_kind_without_a_key_gives_none_at_any_rate(kind):
    for rate in (0.19, 0.07, 0.16, 0.05):
        assert dk.derive_key(kind, rate, DAY_OUTSIDE) == 0 and dk.derive_key(kind, rate, DAY_INSIDE) == 0


def test_the_four_keys_of_a_kind_are_all_different_and_the_two_kinds_share_none():
    assert len(set(OUTSIDE.values())) == 8 and set(OUTSIDE.values()) == set(INSIDE.values())


def test_percent_is_the_normalisation_of_the_revenue_accounts():
    """Dieselbe Normierung wie ``InvoicesMixin._revenue_rate_key`` (Sätze über 0)."""
    for rate in (0.19, 19, 0.07, 7, 0.16, 16, 0.05, 5, 0.107, 10.7, 0.5, 100, 0.0001, 1e-9, 1, 0.999, 19.004, 19.006):
        assert dk.percent(rate) == InvoicesMixin._revenue_rate_key(rate), rate
    for rate in (None, 0, -1, 'x', float('nan')):
        assert dk.percent(rate) == 0.0


# ── Die Umkehrung (Textimport) ───────────────────────────────────────────────

@pytest.mark.parametrize('kind, pct, day, key', CELLS)
def test_key_rate_is_the_inverse_of_the_table(kind, pct, day, key):
    assert dk.key_rate(key, day) == pct / 100


@pytest.mark.parametrize('key, outside, inside', [
    (401, 0.19, 0.16), (402, 0.07, 0.05), (7, 0.16, 0.19), (6, 0.05, 0.07),
    (101, 0.19, 0.16), (102, 0.07, 0.05), (5, 0.16, 0.19), (4, 0.05, 0.07),
    (121, 0.19, 0.16), (122, 0.07, 0.05)])
def test_key_rate_follows_the_swap(key, outside, inside):
    assert dk.key_rate(key, DAY_OUTSIDE) == outside
    assert dk.key_rate(key, DAY_INSIDE) == inside


@pytest.mark.parametrize('key', [0, 100, 120, 490, 400, 8, 9, 2, 3, 1, 10, 181, 9999, 12345, -401, None, '', 'x',
                                 True, 401.5, '0401', ' 4 01'])
def test_key_rate_does_not_know_other_keys(key):
    """Was die Tabelle nicht kennt, bleibt dem Nachschlagen in ``TaxKeys`` (``None``)."""
    assert dk.key_rate(key, DAY_OUTSIDE) is None


def test_key_rate_takes_the_key_as_text_too():
    assert dk.key_rate('401', DAY_INSIDE) == 0.16 and dk.key_rate(' 121 ', DAY_OUTSIDE) == 0.19


def test_vat_keys_are_the_vat_keys_with_a_rate():
    assert dk.VAT_KEYS == {101, 102, 5, 4, 121, 122}


# ── Die Art einer Buchung ────────────────────────────────────────────────────

WAITING = (4405, 4345, 4340)


def _kind(form, coa, counter, inflow=True, waiting=WAITING):
    return dk.booking_kind(form, coa, counter, inflow, waiting)


@pytest.mark.parametrize('form', ['mirror_coa', 'mirror_counter', 'mirror_legacy'])
@pytest.mark.parametrize('coa, counter', [(1800, 1460), (4400, 1800), (10000, 4405), (6815, 1800)])
def test_a_mirror_never_has_a_key(form, coa, counter):
    assert _kind(form, coa, counter) == dk.KIND_NONE and _kind(form, coa, counter, inflow=False) == dk.KIND_NONE


@pytest.mark.parametrize('coa, counter', [(10000, 4405), (4405, 10000), (4405, 4400), (4400, 4405), (10000, 4400),
                                          (4736, 10000), (10000, 4340), (4345, 4300), (4300, 4345), (10000, 6222),
                                          (69999, 1800), (1800, 10000)])
@pytest.mark.parametrize('inflow', [True, False])
def test_a_transfer_with_a_debtor_or_a_waiting_account_is_the_invoice_chain(coa, counter, inflow):
    """Forderung, „Wartekonto an Erlöskonto“, Gegenbuchung einer Stornorechnung: Schlüssel 0, aber eine
    Umsatzsteuer-Zeile – gleich wie das Vorzeichen steht."""
    assert _kind('transfer', coa, counter, inflow) == dk.KIND_INVOICE_CHAIN


@pytest.mark.parametrize('coa, counter, kind', [
    (4400, 2100, dk.KIND_VAT), (2100, 4640, dk.KIND_VAT), (1460, 4645, dk.KIND_VAT), (4999, 3740, dk.KIND_VAT),
    (6222, 520, dk.KIND_INPUT), (520, 6222, dk.KIND_INPUT), (5000, 3740, dk.KIND_INPUT), (7999, 2100, dk.KIND_INPUT),
    (670, 3740, dk.KIND_INPUT), (1, 3740, dk.KIND_INPUT), (999, 2100, dk.KIND_INPUT),
    (4400, 6815, dk.KIND_VAT),                                   # ein Erlöskonto geht vor Aufwand
    (3740, 2100, dk.KIND_NONE), (1200, 3740, dk.KIND_NONE), (8000, 9000, dk.KIND_NONE), (None, 3740, dk.KIND_NONE),
    (None, None, dk.KIND_NONE)])
def test_another_transfer_follows_the_account_class(coa, counter, kind):
    assert _kind('transfer', coa, counter) == kind and _kind('transfer', coa, counter, inflow=False) == kind


@pytest.mark.parametrize('rate, key', [(None, 0), (0.0, 0), (0.12, 0), (0.19, 401), (0.07, 402), (0.16, 7), (0.05, 6)])
def test_the_depreciation_is_of_the_input_tax_kind_and_has_no_key_only_because_it_has_no_rate(rate, key):
    """„Aufwand an Anlagekonto“ (AfA): Die Art ist Vorsteuer, und der Schlüssel fehlt allein, weil kein Steuersatz (oder
    keiner der vier Sätze) da ist – nicht, weil die Art keinen hätte."""
    kind = _kind('transfer', 6222, 520)
    assert kind == dk.KIND_INPUT and dk.derive_key(kind, rate, '2026-07-01') == key


@pytest.mark.parametrize('form, coa, counter, purpose', [
    ('purpose_first', 6815, 1800, 6815), ('purpose_first', 4400, 1800, 4400), ('purpose_first', 10000, 1800, 10000),
    ('liquid_first', 1800, 6815, 6815), ('liquid_first', 1800, 4400, 4400), ('liquid_first', 1800, 10000, 10000)])
@pytest.mark.parametrize('inflow', [True, False])
def test_a_payment_follows_the_purpose_account_not_the_direction_of_the_money(form, coa, counter, purpose, inflow):
    """Erlöskonto → Umsatzsteuer, Debitor → keiner, jedes andere → Vorsteuer – auch bei einer Erstattung
    (WISO: „Geld an Aufwand“ trägt 401)."""
    expected = (dk.KIND_NONE if purpose >= 10000 else dk.KIND_VAT if 4000 <= purpose <= 4999 else dk.KIND_INPUT)
    assert _kind(form, coa, counter, inflow) == expected


@pytest.mark.parametrize('form, coa, counter', [('purpose_first', None, 1800), ('liquid_first', 1800, None),
                                                ('purpose_first', 'x', 1800), ('liquid_first', 1800, '')])
def test_a_payment_without_a_purpose_account_follows_the_direction_of_the_money(form, coa, counter):
    assert _kind(form, coa, counter, inflow=True) == dk.KIND_VAT
    assert _kind(form, coa, counter, inflow=False) == dk.KIND_INPUT


@pytest.mark.parametrize('inflow, kind', [(True, dk.KIND_VAT), (False, dk.KIND_INPUT)])
@pytest.mark.parametrize('coa, counter', [(10000, 4405), (6815, 1800), (4400, 1800), (None, None)])
def test_an_unknown_form_follows_the_direction_of_the_money(coa, counter, inflow, kind):
    assert _kind(None, coa, counter, inflow) == kind


@pytest.mark.parametrize('written, expected', [(4405, 4405), ('4405', 4405), (' 4405 ', 4405), ('4405a', None),
                                               ('abc', None), ('', None), (None, None), (True, None), (-4, None),
                                               ('４４０５', None), (4405.0, None)])
def test_account_numbers_may_be_numbers_or_text_and_other_values_do_not_count(written, expected):
    assert dk.account_number(written) == expected


def test_the_waiting_accounts_may_be_numbers_or_text():
    assert _kind('transfer', '4405', '4400', waiting=('4405',)) == dk.KIND_INVOICE_CHAIN
    assert _kind('transfer', 4405, 4400, waiting=()) == dk.KIND_VAT          # ohne Wartekonten ein Erlöskonto
    assert _kind('transfer', 4405, 4400, waiting=(None, 'x')) == dk.KIND_VAT


@pytest.mark.parametrize('number, kind', [
    (0, dk.KIND_NONE), (1, dk.KIND_INPUT), (999, dk.KIND_INPUT), (1000, dk.KIND_NONE), (3999, dk.KIND_NONE),
    (4000, dk.KIND_VAT), (4999, dk.KIND_VAT), (5000, dk.KIND_INPUT), (7999, dk.KIND_INPUT), (8000, dk.KIND_NONE),
    (9999, dk.KIND_NONE), (10000, dk.KIND_INVOICE_CHAIN), (69999, dk.KIND_INVOICE_CHAIN), (70000, dk.KIND_NONE)])
@pytest.mark.parametrize('side', ['coa', 'counter'])
def test_the_class_boundaries_of_a_transfer(number, kind, side):
    other = 3740                                                            # ein Konto, das keine Klasse hat
    assert (_kind('transfer', number, other) if side == 'coa' else _kind('transfer', other, number)) == kind


# ── Die Forderung ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('coa, counter, expected', [
    (10000, 4405, True), (4405, 10000, True), (10000, 4345, True), (10000, 4340, True), (69999, 4405, True),
    (10000, 4400, False), (4405, 4400, False), (10000, 1800, False), (4405, 4345, False), (10000, None, False),
    (None, None, False)])
def test_only_a_debtor_against_a_waiting_account_is_the_receivable(coa, counter, expected):
    assert dk.is_receivable_row('transfer', coa, counter, WAITING) is expected
    for form in ('purpose_first', 'liquid_first', 'mirror_coa', None):
        assert dk.is_receivable_row(form, coa, counter, WAITING) is False


def test_without_waiting_accounts_there_is_no_receivable():
    assert dk.is_receivable_row('transfer', 10000, 4405, ()) is False


# ── Spalte 40 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('key', sorted(dk.VAT_KEYS))
def test_a_vat_key_with_a_rate_is_a_vat_row_whatever_the_kind(key):
    for kind in (dk.KIND_NONE, dk.KIND_INPUT, dk.KIND_VAT, dk.KIND_INVOICE_CHAIN):
        assert dk.is_vat_row(key, kind, 0.19) is True


@pytest.mark.parametrize('key', [401, 402, 7, 6, 490, 100, 120, 400, 181, 9999])
def test_another_key_is_never_a_vat_row_even_on_a_vat_kind(key):
    for kind in (dk.KIND_NONE, dk.KIND_INPUT, dk.KIND_VAT, dk.KIND_INVOICE_CHAIN):
        assert dk.is_vat_row(key, kind, 0.19) is False


@pytest.mark.parametrize('kind, expected', [(dk.KIND_VAT, True), (dk.KIND_INVOICE_CHAIN, True),
                                            (dk.KIND_INPUT, False), (dk.KIND_NONE, False)])
def test_without_a_key_the_kind_decides(kind, expected):
    assert dk.is_vat_row(0, kind, 0.19) is expected


@pytest.mark.parametrize('rate', [None, 0, 0.0, -1, '', 'x'])
@pytest.mark.parametrize('key', [0, 101, 121, 5])
def test_without_a_rate_there_is_no_vat_row(key, rate):
    assert dk.is_vat_row(key, dk.KIND_VAT, rate) is False and dk.is_vat_row(key, dk.KIND_INVOICE_CHAIN, rate) is False


# ── Belegfeld 1 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('R26003', 'R26003'), ('RE 2026 0001', 'RE20260001'), ('  X 1  ', 'X1'), ('A\tB\nC\r\nD', 'ABCD'),
    ('A B C\x85D', 'ABCD'), ('1,5_x', '1,5_x'), ('', ''), (None, ''), (12345, '12345'),
    ('Abcdefghijklmnopqrstuvwxyz0123456789ÄÖ', 'Abcdefghijklmnopqrstuvwxyz0123456789'),   # 36 Zeichen
    ('1234567890123', '1234567890123')])                                                    # länger als 12: bleibt
def test_belegfeld_without_blanks_and_up_to_36_characters(text, expected):
    assert dk.beleg_field(text) == expected


def test_belegfeld_is_cut_at_the_field_length_and_not_before():
    assert dk.beleg_field('x' * 36) == 'x' * 36 and dk.beleg_field('x' * 37) == 'x' * 36
    assert dk.beleg_field('x' * 100) == 'x' * 36 and len(dk.beleg_field('ab ' * 30)) == 36


# ── Strenge Prüfung der Werte, die in den Stapel gehen ───────────────────────

@pytest.mark.parametrize('value, expected', [
    (1, 1), (401, 401), (9999, 9999), ('401', 401), (' 401 ', 401), (401.0, 401), ('1', 1),
    (0, None), ('0', None), ('', None), (None, None), (-1, None), (10000, None), ('10000', None),
    ('00401', None), ('0401', None), ('+401', None), ('-401', None), ('4 01', None), ('401;1', None),
    ('401"', None), ('40\n1', None), ('4e2', None), ('abc', None), ('４０１', None), ('٤٠١', None), ('401²', None),
    (401.5, None), (float('nan'), None), (float('inf'), None), (True, None), (False, None), (b'401', None),
    ([401], None), ('9' * 5000, None), (10 ** 30, None)])
def test_parse_key_accepts_only_whole_numbers_from_1_to_9999(value, expected):
    assert dk.parse_key(value) == expected


@pytest.mark.parametrize('value', [None, '', ' ', '0', 0, 0.0])
def test_checked_key_reads_none_empty_and_zero_as_no_key(value):
    assert dk.checked_key(value) is None


@pytest.mark.parametrize('value, expected', [(401, 401), ('490', 490), (9999, 9999), (1.0, 1)])
def test_checked_key_gives_a_valid_key_as_an_int(value, expected):
    assert dk.checked_key(value) == expected


@pytest.mark.parametrize('value', [-1, 10000, 'abc', '401;', '4 01', 401.5, True, [401], float('nan'), '0401'])
def test_checked_key_refuses_anything_else(value):
    with pytest.raises(ValueError):
        dk.checked_key(value)


@pytest.mark.parametrize('value, ok', [
    ('1001', True), ('12345', True), ('1234567', True), ('9999999', True), (1234567, True), (1001, True), (1234.0, True),
    ('123', False), ('1000', False), (1000, False), ('0999', False), ('0000', False), ('0', False), (0, False),
    ('01234', False), ('01001', False), ('0123456', False), ('10000000', False), ('12345678', False), (10000000, False),
    ('', False), (None, False), (' 12345', False), ('12 345', False), ('12345\n', False), ('12;45', False),
    ('12"45', False), ('1234a', False), ('-1234', False), (-1234, False), ('１２３４５', False), (1234.5, False),
    (True, False), (123, False), (12345678, False), ('9' * 5000, False)])
def test_the_consultant_number_is_a_number_from_1001_to_9999999_without_a_leading_zero(value, ok):
    assert dk.consultant_no(value) == (str(int(value)) if ok and not isinstance(value, str) else value if ok else None)


@pytest.mark.parametrize('value, ok', [
    ('1', True), ('99999', True), ('42', True), ('12345', True), (7, True), (99999, True), (1.0, True),
    ('0', False), ('00', False), ('00042', False), ('01', False), ('012', False), ('00000', False), (0, False),
    ('', False), ('100000', False), ('123456', False), (None, False), (' 1', False), ('1 ', False), ('1\n', False),
    ('1;', False), ('1"', False), ('a', False), ('-1', False), (-1, False), (100000, False), (123456, False),
    (True, False), (1.5, False), ('９', False), ('9' * 5000, False)])
def test_the_client_number_is_a_number_from_1_to_99999_without_a_leading_zero(value, ok):
    assert dk.client_no(value) == (str(int(value)) if ok and not isinstance(value, str) else value if ok else None)


def test_the_error_messages_name_the_range():
    assert '1001 bis 9999999' in dk.CONSULTANT_NO_ERROR and '1 bis 99999' in dk.CLIENT_NO_ERROR
    assert dk.CONSULTANT_NO_RANGE == (1001, 9999999) and dk.CLIENT_NO_RANGE == (1, 99999)
