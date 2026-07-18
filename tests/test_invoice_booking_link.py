# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""todo #2: Rechnung ↔ Buchung verknüpfen (Zahlungs-Zuordnung).

Deckt ab: get_open_invoices-Filter, Kappungs-Logik (Teil-/Über-/Sammelzahlung
mit freiem Buchungsrest), handle_add_transaction mit invoice_id (Insert- und
Update-Pfad, Redirect), den /invoice/link-payment-Handler, Offene-Posten-/
Kandidaten-Helper sowie das Formular-Prefill via from_invoice.
"""
import json
import sqlite3
from decimal import Decimal

from server import handlers
from server.handlers import (handle_add_transaction, handle_link_invoice_payment,
                             link_booking_to_invoice_capped)
from server.pages_transactions import PageTransactions


def _invoice(db, number='R-1', gross=119.00, status='finalized', **kw):
    data = {
        'invoice_number': number, 'invoice_date': '2026-01-01',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': 0.19, 'sum_net': round(gross / 1.19, 2),
        'tax_amount': round(gross - gross / 1.19, 2),
        'sum_gross': gross, 'amount_due': gross, 'status': status,
    }
    data.update(kw)
    return db.insert_invoice(data)


def _bank_booking(db, amount=119.00, date='2026-02-01', **kw):
    return db.insert_booking(date, amount, booking_type='bank',
                             recipient_client=kw.pop('recipient', 'Käufer AG'),
                             **kw)


def _raw(db, sql, *params):
    con = sqlite3.connect(db.db_name)
    rows = con.execute(sql, params).fetchall()
    con.close()
    return rows


# ── get_open_invoices ────────────────────────────────────────────────────────

def test_get_open_invoices_filters(tmp_db):
    open_id = _invoice(tmp_db, 'R-OPEN')
    sent_id = _invoice(tmp_db, 'R-SENT', status='sent')
    _invoice(tmp_db, 'R-DRAFT', status='draft')
    _invoice(tmp_db, 'R-CANC', status='cancelled')
    _invoice(tmp_db, 'R-PAID', status='paid')
    _invoice(tmp_db, 'Q-1', status='sent', document_type='quote')
    settled = _invoice(tmp_db, 'R-NULL-REST', status='sent', amount_due=0)

    ids = {r[0] for r in tmp_db.get_open_invoices()}
    assert ids == {open_id, sent_id}
    assert settled not in ids


# ── Kappung: Über-, Teil- und Sammelzahlung ──────────────────────────────────

def test_overpayment_is_capped_and_leaves_free_rest(tmp_db):
    inv = _invoice(tmp_db, gross=100.00)
    bk = _bank_booking(tmp_db, amount=120.00)

    ok, err = link_booking_to_invoice_capped(tmp_db, inv, bk)
    assert ok, err
    (due, status), = _raw(tmp_db, 'SELECT AmountDue, Status FROM Invoices WHERE ID=?', inv)
    assert due == 0 and status == 'paid'
    assert tmp_db.get_booking_unallocated_amount(bk) == Decimal('20.0000')


def test_collective_payment_splits_over_two_invoices(tmp_db):
    inv_a = _invoice(tmp_db, 'R-A', gross=100.00)
    inv_b = _invoice(tmp_db, 'R-B', gross=100.00)
    bk = _bank_booking(tmp_db, amount=150.00)

    assert link_booking_to_invoice_capped(tmp_db, inv_a, bk)[0]
    assert link_booking_to_invoice_capped(tmp_db, inv_b, bk)[0]

    (a_due, a_st), = _raw(tmp_db, 'SELECT AmountDue, Status FROM Invoices WHERE ID=?', inv_a)
    (b_due, b_st), = _raw(tmp_db, 'SELECT AmountDue, Status FROM Invoices WHERE ID=?', inv_b)
    assert a_due == 0 and a_st == 'paid'
    assert b_due == 500000 and b_st == 'partial_payment'   # 50 € Rest
    assert tmp_db.get_booking_unallocated_amount(bk) == 0

    # Duplikat-Paar bleibt abgewiesen, ohne neue Zahlung anzulegen
    ok, err = link_booking_to_invoice_capped(tmp_db, inv_a, bk)
    assert not ok and 'bereits' in err
    assert len(tmp_db.get_invoice_payments(inv_a)) == 1


def test_exhausted_booking_rejected(tmp_db):
    inv_a = _invoice(tmp_db, 'R-A', gross=100.00)
    inv_b = _invoice(tmp_db, 'R-B', gross=100.00)
    bk = _bank_booking(tmp_db, amount=100.00)
    assert link_booking_to_invoice_capped(tmp_db, inv_a, bk)[0]
    ok, err = link_booking_to_invoice_capped(tmp_db, inv_b, bk)
    assert not ok and 'ausgeschöpft' in err


def test_quote_rejected(tmp_db):
    q = _invoice(tmp_db, 'Q-1', document_type='quote')
    bk = _bank_booking(tmp_db)
    ok, _ = link_booking_to_invoice_capped(tmp_db, q, bk)
    assert not ok


# ── handle_add_transaction mit invoice_id ────────────────────────────────────

def _post(**kw):
    base = {'transaction_id': ['0'], 'date': ['2026-02-01'], 'amount': ['119.00']}
    base.update({k: [str(v)] for k, v in kw.items()})
    return base


def test_add_transaction_links_and_redirects(tmp_db):
    inv = _invoice(tmp_db)
    status, location = handle_add_transaction(tmp_db, _post(invoice_id=inv))
    assert (status, location) == (303, f'/invoice/edit?id={inv}')

    payments = tmp_db.get_invoice_payments(inv)
    assert len(payments) == 1
    assert payments[0][3] == Decimal('119.0000')
    (inv_status,), = _raw(tmp_db, 'SELECT Status FROM Invoices WHERE ID=?', inv)
    assert inv_status == 'paid'

    # Re-Speichern derselben Buchung (Update-Pfad) erzeugt keinen Doppel-Link
    booking_id = payments[0][2]
    status, location = handle_add_transaction(
        tmp_db, _post(transaction_id=booking_id, invoice_id=inv))
    assert (status, location) == (303, f'/invoice/edit?id={inv}')
    assert len(tmp_db.get_invoice_payments(inv)) == 1


def test_add_transaction_partial_payment(tmp_db):
    inv = _invoice(tmp_db, gross=119.00)
    status, _ = handle_add_transaction(tmp_db, _post(amount='50.00', invoice_id=inv))
    assert status == 303
    (due, inv_status), = _raw(tmp_db, 'SELECT AmountDue, Status FROM Invoices WHERE ID=?', inv)
    assert due == 690000                    # 69,00 € Rest in Minor Units
    assert inv_status == 'partial_payment'  # nicht mehr 'partial'


def test_add_transaction_without_invoice_keeps_default_redirect(tmp_db):
    status, location = handle_add_transaction(tmp_db, _post())
    assert (status, location) == (303, '/transactions')


# ── /invoice/link-payment (JSON-Endpunkt) ────────────────────────────────────

def test_link_payment_endpoint_caps_amount(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda: tmp_db)
    inv = _invoice(tmp_db, gross=100.00)
    bk = _bank_booking(tmp_db, amount=120.00)

    body = json.dumps({'invoice_id': inv, 'transaction_id': bk,
                       'amount_paid': 999.99}).encode()
    status, _ = handle_link_invoice_payment(body)
    assert status == 200
    assert tmp_db.get_invoice_payments(inv)[0][3] == Decimal('100.0000')

    # Rechnung ist bezahlt → weitere Zuordnung wird abgewiesen
    bk2 = _bank_booking(tmp_db, amount=10.00)
    body = json.dumps({'invoice_id': inv, 'transaction_id': bk2,
                       'amount_paid': 10}).encode()
    status, msg = handle_link_invoice_payment(body)
    assert status == 400


def test_link_payment_endpoint_rejects_bad_params(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda: tmp_db)
    status, _ = handle_link_invoice_payment(b'{"invoice_id": 0, "transaction_id": 0}')
    assert status == 400


# ── Offene Posten / Kandidaten ───────────────────────────────────────────────

def test_open_items_and_candidates_for_contact(tmp_db):
    tmp_db.insert_contact(display_name='Käufer AG', company_name='Käufer AG')
    contact_id = _raw(tmp_db,
                      "SELECT ID FROM Contacts WHERE DisplayName='Käufer AG'")[0][0]
    inv = _invoice(tmp_db, gross=100.00, customer_id=contact_id)
    bk = _bank_booking(tmp_db, amount=150.00, contact_id=contact_id)
    link_booking_to_invoice_capped(tmp_db, inv, bk)   # 100 zugeordnet, 50 frei

    inv2 = _invoice(tmp_db, 'R-2', gross=80.00, customer_id=contact_id)

    items = tmp_db.get_open_items_for_contact(contact_id)
    assert [r[0] for r in items['open_invoices']] == [inv2]
    assert items['open_invoices'][0][4] == Decimal('80.0000')
    assert [c[0] for c in items['credits']] == [bk]
    assert items['credits'][0][5] == Decimal('50.0000')
    assert items['saldo'] == Decimal('30.0000')       # 80 offen − 50 Guthaben

    # Kandidatenliste: Kontakt-Buchung vorn, voll zugeordnete fehlen
    bk_other = _bank_booking(tmp_db, amount=99.00, recipient='Andere Person')
    candidates = tmp_db.get_unallocated_bank_bookings(contact_id)
    assert [c[0] for c in candidates][:1] == [bk]
    assert {c[0] for c in candidates} == {bk, bk_other}


# ── Formular-Prefill (from_invoice) ──────────────────────────────────────────

def test_page_transactions_prefill_from_invoice(tmp_db):
    inv = _invoice(tmp_db, 'R-PRE', gross=119.00)
    html = PageTransactions(tmp_db, from_invoice=inv)
    assert 'Zahlung zu Rechnung R-PRE' in html
    assert 'value="119.00"' in html                    # Betrag = offener Rest
    assert 'value="19"' in html                        # Steuersatz %
    assert 'Zahlung Rechnung R-PRE' in html            # Verwendungszweck
    coa_4400 = tmp_db.get_coa_id_by_account_number(4400)
    assert coa_4400 and f'value="{coa_4400}" selected' in html
    assert f'value="{inv}" selected' in html           # Rechnung im Dropdown

    # Teilzahlung: Prefill = Restbetrag
    bk = _bank_booking(tmp_db, amount=19.00)
    link_booking_to_invoice_capped(tmp_db, inv, bk)
    html = PageTransactions(tmp_db, from_invoice=inv)
    assert 'value="100.00"' in html


def test_page_transactions_prefill_kleinunternehmer(tmp_db):
    # Sentinel -1 (§19): kein Steuersatz/-betrag vorbelegen
    inv = _invoice(tmp_db, 'R-K', gross=100.00, tax_rate=-1, tax_amount=0)
    html = PageTransactions(tmp_db, from_invoice=inv)
    assert 'Zahlung zu Rechnung R-K' in html
    assert 'value="100.00"' in html
    assert 'id="tax_rate" value=""' in html


def test_page_transactions_prefill_rejects_quote_and_paid(tmp_db):
    q = _invoice(tmp_db, 'Q-PRE', document_type='quote')
    assert 'Zahlung zu Rechnung' not in PageTransactions(tmp_db, from_invoice=q)
    p = _invoice(tmp_db, 'R-PAID2', status='paid')
    assert 'Zahlung zu Rechnung' not in PageTransactions(tmp_db, from_invoice=p)
