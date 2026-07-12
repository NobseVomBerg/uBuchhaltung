# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Phase 1c (Decimal-Migration): InvoicePayments.Amount als Festkomma-Integer.
Der AmountDue-Recalc rechnet exakt in Minor Units (SumGross noch REAL bis 1e)."""
import sqlite3
from decimal import Decimal


def _setup(db, sum_gross=119.00):
    inv_id = db.insert_invoice({
        'invoice_number': 'PAY-TEST', 'invoice_date': '2026-01-01',
        'seller_name': 'Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': 19, 'sum_net': 100.00, 'tax_amount': 19.00,
        'sum_gross': sum_gross, 'amount_due': sum_gross, 'status': 'finalized',
    })
    bk_id = db.insert_booking('2026-01-15', sum_gross, text='Zahlung')
    return inv_id, bk_id


def test_payment_amount_roundtrip(tmp_db):
    inv_id, bk_id = _setup(tmp_db)
    tmp_db.link_invoice_to_transaction(inv_id, bk_id, 50.00)
    payments = tmp_db.get_invoice_payments(inv_id)
    assert len(payments) == 1
    assert payments[0][3] == Decimal("50.0000")  # Index 3 = Amount, Euro-Decimal


def test_payment_stored_as_integer(tmp_db):
    inv_id, bk_id = _setup(tmp_db)
    tmp_db.link_invoice_to_transaction(inv_id, bk_id, 50.00)
    con = sqlite3.connect(tmp_db.db_name)
    raw = con.execute("SELECT Amount FROM InvoicePayments").fetchone()[0]
    con.close()
    assert isinstance(raw, int)
    assert raw == 500000  # 50.00 * 10^4


def test_partial_payment_recalculates_amount_due(tmp_db):
    inv_id, bk_id = _setup(tmp_db, sum_gross=119.00)
    tmp_db.link_invoice_to_transaction(inv_id, bk_id, 50.00)
    con = sqlite3.connect(tmp_db.db_name)
    due, status = con.execute("SELECT AmountDue, Status FROM Invoices WHERE ID=?", (inv_id,)).fetchone()
    con.close()
    assert due == 690000     # 119.00 - 50.00 = 69.00 als Minor Units (seit Phase 1e)
    assert status == 'partial'


def test_full_payment_sets_status_paid(tmp_db):
    inv_id, bk_id = _setup(tmp_db, sum_gross=119.00)
    tmp_db.link_invoice_to_transaction(inv_id, bk_id, 119.00)
    con = sqlite3.connect(tmp_db.db_name)
    due, status = con.execute("SELECT AmountDue, Status FROM Invoices WHERE ID=?", (inv_id,)).fetchone()
    con.close()
    assert round(due, 2) == 0.00
    assert status == 'paid'


def test_delete_payment_restores_amount_due(tmp_db):
    inv_id, bk_id = _setup(tmp_db, sum_gross=119.00)
    tmp_db.link_invoice_to_transaction(inv_id, bk_id, 119.00)
    pay_id = tmp_db.get_invoice_payments(inv_id)[0][0]
    tmp_db.delete_invoice_payment(pay_id)
    con = sqlite3.connect(tmp_db.db_name)
    due, status = con.execute("SELECT AmountDue, Status FROM Invoices WHERE ID=?", (inv_id,)).fetchone()
    con.close()
    assert due == 1190000     # 119.00 als Minor Units (seit Phase 1e)
    assert status == 'finalized'
