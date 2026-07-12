# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Phase 2 (Decimal-Migration): Rechnungssummen werden serverseitig exakt neu
berechnet – den vom Client gelieferten net/tax/gross-Werten wird nicht vertraut.
"""
import json
from decimal import Decimal

import pytest

from server.handlers import recompute_invoice_totals, _rate_to_pct, handle_invoice_save
import server.handlers as handlers


# ── _rate_to_pct ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (19, Decimal("19")),
    (7, Decimal("7")),
    (0.19, Decimal("19.00")),          # Bruch -> Prozent
    (0.07, Decimal("7.00")),
    ("19", Decimal("19")),
    (0, Decimal("0")),                 # 0 bleibt 0 (kein Bruch)
    (None, Decimal("19")),             # Default
    ("", Decimal("19")),
])
def test_rate_to_pct(value, expected):
    assert _rate_to_pct(value) == expected


# ── recompute_invoice_totals ───────────────────────────────────────────────────

def test_recompute_simple():
    items = [{'quantity': 2, 'unitPrice': 10.00, 'taxRate': 19}]
    net, tax, gross, lines = recompute_invoice_totals(items, Decimal('19'))
    assert net == Decimal("20.0000")
    assert tax == Decimal("3.8000")
    assert gross == Decimal("23.8000")
    assert lines == [Decimal("20.0000")]


def test_recompute_ignores_client_totals_logic():
    # Mehrere Positionen, Summe muss exakt aus Menge x Preis stammen.
    items = [
        {'quantity': 3, 'unitPrice': 19.99, 'taxRate': 19},
        {'quantity': 1, 'unitPrice': 0.01, 'taxRate': 19},
    ]
    net, tax, gross, lines = recompute_invoice_totals(items, Decimal('19'))
    assert lines == [Decimal("59.9700"), Decimal("0.0100")]
    assert net == Decimal("59.9800")
    assert tax == Decimal("11.4000")          # 19% auf 59,98 = 11,3962 -> 11,40
    assert gross == Decimal("71.3800")


def test_recompute_mixed_tax_rates_grouped():
    # Steuer wird je Satz gruppiert und gerundet.
    items = [
        {'quantity': 1, 'unitPrice': 100.00, 'taxRate': 19},
        {'quantity': 1, 'unitPrice': 100.00, 'taxRate': 7},
    ]
    net, tax, gross, _ = recompute_invoice_totals(items, Decimal('19'))
    assert net == Decimal("200.0000")
    assert tax == Decimal("26.0000")          # 19 + 7
    assert gross == Decimal("226.0000")


def test_recompute_fourdigit_unit_price_rounds_line_to_cents():
    items = [{'quantity': 3, 'unitPrice': '1.2345', 'taxRate': 19}]
    net, _, _, lines = recompute_invoice_totals(items, Decimal('19'))
    # 3 * 1,2345 = 3,7035 -> auf Cent gerundet 3,70
    assert lines == [Decimal("3.7000")]
    assert net == Decimal("3.7000")


def test_recompute_missing_quantity_defaults_to_one():
    items = [{'unitPrice': 50.00, 'taxRate': 19}]
    net, _, _, _ = recompute_invoice_totals(items, Decimal('19'))
    assert net == Decimal("50.0000")


def test_recompute_uses_default_rate_when_item_has_none():
    items = [{'quantity': 1, 'unitPrice': 100.00}]
    _, tax, _, _ = recompute_invoice_totals(items, Decimal('7'))
    assert tax == Decimal("7.0000")


# ── End-to-End: handle_invoice_save speichert Server-Summen, nicht Client ───────

def _contact_id(db, display_name):
    import sqlite3
    con = sqlite3.connect(db.db_name)
    row = con.execute("SELECT ID FROM Contacts WHERE DisplayName=?", (display_name,)).fetchone()
    con.close()
    return row[0]


def _make_contacts(db):
    db.insert_contact(contact_type='own', entity_type='company',
                      display_name='Meine Firma', company_name='Meine Firma GmbH',
                      street='Hauptstr. 1', postal_code='10115', city='Berlin')
    db.insert_contact(contact_type='customer', entity_type='company',
                      display_name='Kunde', company_name='Kunde AG',
                      street='Nebenweg 2', postal_code='80331', city='München')
    return _contact_id(db, 'Meine Firma'), _contact_id(db, 'Kunde')


def test_handle_invoice_save_recomputes_and_stores_integer(tmp_db, monkeypatch):
    own_id, cust_id = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)

    payload = {
        'invoiceNumber': '26R001',
        'invoiceDate': '2026-06-07',
        'customerId': cust_id,
        'ownCompanyId': own_id,
        'taxRate': 0.19,                      # Bruch wie vom Client geliefert
        # absichtlich FALSCHE Client-Summen – muessen ignoriert werden:
        'netAmount': 999.99,
        'taxAmount': 999.99,
        'grossAmount': 999.99,
        'status': 'draft',
        'items': [
            {'position': 1, 'quantity': 2, 'unit': 'C62', 'description': 'Pos A',
             'unitPrice': 10.00, 'totalPrice': 999.99, 'taxRate': 19},
            {'position': 2, 'quantity': 1, 'unit': 'C62', 'description': 'Pos B',
             'unitPrice': 5.00, 'totalPrice': 999.99, 'taxRate': 19},
        ],
    }
    resp = json.loads(handle_invoice_save(json.dumps(payload).encode()))
    assert resp['success'] is True
    inv_id = resp['invoice_id']

    inv = tmp_db.get_invoice_by_id(inv_id)
    assert inv[36] == Decimal("25.0000")      # SumNet  = 20 + 5
    assert inv[37] == Decimal("4.7500")       # TaxAmount 19% auf 25
    assert inv[38] == Decimal("29.7500")      # SumGross
    assert inv[39] == Decimal("29.7500")      # AmountDue = Gross

    items = tmp_db.get_invoice_items(inv_id)
    assert items[0][8] == Decimal("20.0000")  # TotalNet Pos A (serverseitig)
    assert items[1][8] == Decimal("5.0000")   # TotalNet Pos B


def test_handle_invoice_save_stores_minor_integers(tmp_db, monkeypatch):
    import sqlite3
    own_id, cust_id = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)

    payload = {
        'invoiceNumber': '26R002', 'invoiceDate': '2026-06-07',
        'customerId': cust_id, 'ownCompanyId': own_id, 'taxRate': 0.19,
        'netAmount': 0, 'taxAmount': 0, 'grossAmount': 0, 'status': 'draft',
        'items': [{'position': 1, 'quantity': 1, 'unit': 'C62',
                   'description': 'X', 'unitPrice': 100.00, 'taxRate': 19}],
    }
    resp = json.loads(handle_invoice_save(json.dumps(payload).encode()))
    inv_id = resp['invoice_id']

    con = sqlite3.connect(tmp_db.db_name)
    net, tax, gross, due = con.execute(
        "SELECT SumNet, TaxAmount, SumGross, AmountDue FROM Invoices WHERE ID=?",
        (inv_id,)).fetchone()
    con.close()
    assert (net, tax, gross, due) == (1000000, 190000, 1190000, 1190000)
    assert all(isinstance(v, int) for v in (net, tax, gross, due))
