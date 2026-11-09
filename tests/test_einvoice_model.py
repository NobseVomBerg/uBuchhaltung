# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Tests für das neutrale E-Rechnungsmodell (Summen, Steuer, Einheiten)."""
from decimal import Decimal

import pytest

from export.einvoice_model import build_einvoice, unit_code, rate_to_pct
from tests.golden_cases import make_case


def _model(db, case):
    iid = make_case(db, case)
    return build_einvoice(db, db.get_invoice_by_id(iid), db.get_invoice_items(iid))


def test_totals_are_exactly_consistent(tmp_db):
    m = _model(tmp_db, 'std19')
    t = m.totals
    assert t.line_total == sum(l.net for l in m.lines)
    assert t.tax_total == sum(b.amount for b in m.tax_breakdown)
    assert t.grand_total == t.tax_basis + t.tax_total
    assert t.payable == t.grand_total - t.prepaid
    assert m.warnings == []                      # gespeicherte Summen stimmen


def test_tax_breakdown_is_list_per_rate(tmp_db):
    m = _model(tmp_db, 'std7')
    assert [(b.category, b.rate) for b in m.tax_breakdown] == [('S', Decimal('7'))]


def test_kleinunternehmer_is_exempt_with_reason(tmp_db):
    m = _model(tmp_db, 'ku')
    assert m.is_exempt
    b = m.tax_breakdown[0]
    assert (b.category, b.rate, b.amount) == ('E', Decimal('0'), 0)
    assert '§ 19 UStG' in b.exemption_reason
    assert all(l.tax_category == 'E' for l in m.lines)


@pytest.mark.parametrize('text, code', [
    ('Stk.', 'C62'), ('Std.', 'HUR'), ('HUR', 'HUR'), ('C62', 'C62'),
    ('Tag', 'DAY'), ('', 'C62'), (None, 'C62'), ('unbekannt', 'C62')])
def test_unit_code(text, code):
    assert unit_code(text) == code


@pytest.mark.parametrize('value, expected', [
    (19, Decimal('19')), (0.19, Decimal('19')), (7, Decimal('7')),
    (0, Decimal('0')), (None, Decimal('19'))])
def test_rate_to_pct(value, expected):
    assert rate_to_pct(value) == expected
