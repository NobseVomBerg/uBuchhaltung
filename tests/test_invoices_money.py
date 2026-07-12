# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Phase 1e (Decimal-Migration): Invoices- und InvoiceItems-Geldspalten als
Festkomma-Integer gespeichert, als Euro-Decimal gelesen.

Indizes: Invoices SumNet=36, TaxAmount=37, SumGross=38, AmountDue=39;
InvoiceItems PricePerUnit=7, TotalNet=8.
"""
import sqlite3
from decimal import Decimal


def _make_invoice(db, sum_net=100.00, tax_amount=19.00, sum_gross=119.00):
    return db.insert_invoice({
        'invoice_number': 'INV-MONEY', 'invoice_date': '2026-01-01',
        'seller_name': 'V', 'seller_company': 'V GmbH',
        'buyer_name': 'K', 'buyer_company': 'K AG',
        'tax_rate': 19,
        'sum_net': sum_net, 'tax_amount': tax_amount,
        'sum_gross': sum_gross, 'amount_due': sum_gross, 'status': 'finalized',
    })


def test_invoice_totals_roundtrip(tmp_db):
    inv_id = _make_invoice(tmp_db)
    inv = tmp_db.get_invoice_by_id(inv_id)
    assert inv[36] == Decimal("100.0000")  # SumNet
    assert inv[37] == Decimal("19.0000")   # TaxAmount
    assert inv[38] == Decimal("119.0000")  # SumGross
    assert inv[39] == Decimal("119.0000")  # AmountDue


def test_invoice_totals_stored_as_integer(tmp_db):
    _make_invoice(tmp_db)
    con = sqlite3.connect(tmp_db.db_name)
    row = con.execute("SELECT SumNet, TaxAmount, SumGross, AmountDue FROM Invoices").fetchone()
    con.close()
    assert all(isinstance(v, int) for v in row)
    assert row == (1000000, 190000, 1190000, 1190000)


def test_invoice_total_compares_equal_to_float(tmp_db):
    # Bestandstests vergleichen teils gegen float – Decimal == float muss gelten
    inv_id = _make_invoice(tmp_db, sum_gross=119.00)
    inv = tmp_db.get_invoice_by_id(inv_id)
    assert inv[38] == 119.00


def test_invoice_item_roundtrip(tmp_db):
    inv_id = _make_invoice(tmp_db)
    tmp_db.insert_invoice_item({
        'invoice_id': inv_id, 'position': 1, 'description': 'Pos',
        'quantity': 2, 'unit': 'Stk.', 'price_per_unit': 12.34,
        'total_net': 24.68, 'tax_rate': 19,
    })
    items = tmp_db.get_invoice_items(inv_id)
    assert len(items) == 1
    assert items[0][7] == Decimal("12.3400")  # PricePerUnit
    assert items[0][8] == Decimal("24.6800")  # TotalNet


def test_invoice_item_fourdigit_price(tmp_db):
    inv_id = _make_invoice(tmp_db)
    tmp_db.insert_invoice_item({
        'invoice_id': inv_id, 'position': 1, 'description': 'Cent-Bruchteil',
        'quantity': 1, 'price_per_unit': '0.0079', 'total_net': '0.0079', 'tax_rate': 19,
    })
    assert tmp_db.get_invoice_items(inv_id)[0][7] == Decimal("0.0079")


def test_payment_recalc_after_invoice_migration(tmp_db):
    # 1c-Bruecke entfernt: SumGross ist jetzt Minor Units; Recalc muss exakt bleiben
    inv_id = _make_invoice(tmp_db, sum_gross=119.00)
    bk_id = tmp_db.insert_booking('2026-01-15', 119.00, text='Zahlung')
    tmp_db.link_invoice_to_transaction(inv_id, bk_id, 50.00)
    inv = tmp_db.get_invoice_by_id(inv_id)
    assert inv[39] == Decimal("69.0000")  # AmountDue = 119 - 50, exakt
