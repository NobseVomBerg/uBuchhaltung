# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""todo #2: Rechnung ↔ Buchung verknüpfen (Zahlungs-Zuordnung).

Deckt ab: get_open_invoices-Filter, Kappungs-Logik (Teil-/Über-/Sammelzahlung
mit freiem Buchungsrest), handle_add_transaction mit invoice_id (Insert- und
Update-Pfad, Redirect), den /invoice/link-payment-Handler, Offene-Posten-/
Kandidaten-Helper sowie das Formular-Prefill via from_invoice.

Eine Zahlung lässt sich nur auf eine versendete Rechnung zuordnen und braucht ein Bankkonto
mit SKR-Nummer: Die Hilfen unten legen Rechnungen als „versendet“ und Bankbewegungen
auf einem solchen Konto an.
"""
import json
import sqlite3
from decimal import Decimal

from server import handlers
from server.handlers import (handle_add_transaction, handle_link_invoice_payment,
                             link_booking_to_invoice_capped)
from server.pages_transactions import PageTransactions


def _invoice(db, number='R-1', gross=119.00, status='sent', **kw):
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


def _account(db, name='Link-Bank', skr=1800):
    """Bankkonto mit SKR-Nummer (ohne sie lässt sich eine Zahlung nicht kontieren)."""
    if not [a for a in db.fetch_accounts() if a[1] == name]:
        db.insert_account(name, 'Ich', 'DE00LINK', 'BIC', 'Bank', is_cash=0, skr_account=skr)
    return [a for a in db.fetch_accounts() if a[1] == name][0][0]


def _bank_booking(db, amount=119.00, date='2026-02-01', **kw):
    kw.setdefault('account_id', _account(db))
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
    sent_id = _invoice(tmp_db, 'R-SENT')
    overdue_id = _invoice(tmp_db, 'R-OVERDUE', status='overdue')
    partial_id = _invoice(tmp_db, 'R-PARTIAL', status='partial_payment', amount_due=50.00)
    _invoice(tmp_db, 'R-FINAL', status='finalized')      # noch nicht versendet: nicht zahlbar
    _invoice(tmp_db, 'R-DRAFT', status='draft')
    _invoice(tmp_db, 'R-CANC', status='cancelled')
    _invoice(tmp_db, 'R-PAID', status='paid')
    _invoice(tmp_db, 'Q-1', status='sent', document_type='quote')
    settled = _invoice(tmp_db, 'R-NULL-REST', status='sent', amount_due=0)

    ids = {r[0] for r in tmp_db.get_open_invoices()}
    assert ids == {sent_id, overdue_id, partial_id}
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


def _payment_post(db, **kw):
    """Formular „Zahlung zu Rechnung“: eine Rechnungszahlung braucht das Bankkonto."""
    return _post(account=_account(db), **kw)


def test_add_transaction_links_and_redirects(tmp_db):
    inv = _invoice(tmp_db)
    status, location = handle_add_transaction(tmp_db, _payment_post(tmp_db, invoice_id=inv))
    assert (status, location) == (303, f'/invoice/edit?id={inv}')

    payments = tmp_db.get_invoice_payments(inv)
    assert len(payments) == 1
    assert payments[0][3] == Decimal('119.0000')
    (inv_status,), = _raw(tmp_db, 'SELECT Status FROM Invoices WHERE ID=?', inv)
    assert inv_status == 'paid'

    # Re-Speichern derselben Buchung (Update-Pfad) erzeugt keinen Doppel-Link; die
    # Rechnungsseite bekommt den Grund als Meldung (die Rechnung ist bezahlt)
    booking_id = payments[0][2]
    status, location = handle_add_transaction(
        tmp_db, _payment_post(tmp_db, transaction_id=booking_id, invoice_id=inv))
    assert status == 303
    assert location.startswith(f'/invoice/edit?id={inv}&payment_error=')
    assert len(tmp_db.get_invoice_payments(inv)) == 1


def test_add_transaction_partial_payment(tmp_db):
    inv = _invoice(tmp_db, gross=119.00)
    status, _ = handle_add_transaction(tmp_db, _payment_post(tmp_db, amount='50.00',
                                                             invoice_id=inv))
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


# ── Buchung löschen / verwaiste Zustände heilen ─────────────────────────────

def test_delete_booking_resets_invoice_state(tmp_db):
    inv = _invoice(tmp_db, gross=119.00)
    handle_add_transaction(tmp_db, _payment_post(tmp_db, invoice_id=inv))
    booking_id = tmp_db.get_invoice_payments(inv)[0][2]
    (st,), = _raw(tmp_db, 'SELECT Status FROM Invoices WHERE ID=?', inv)
    assert st == 'paid'

    tmp_db.delete_transaction(booking_id)

    (due, st), = _raw(tmp_db, 'SELECT AmountDue, Status FROM Invoices WHERE ID=?', inv)
    assert due == 1190000 and st == 'sent'   # Rest wiederhergestellt; versendet bleibt versendet
    assert tmp_db.get_invoice_payments(inv) == []
    assert inv in {r[0] for r in tmp_db.get_open_invoices()}


def test_status_change_heals_orphaned_paid_state(tmp_db, monkeypatch):
    # Verwaister Zustand (wie vor dem Fix entstanden): "bezahlt", Rest 0,
    # aber keine Zahlungen mehr – Rechnung war nirgends mehr verknüpfbar.
    monkeypatch.setattr(handlers, 'Database', lambda: tmp_db)
    inv = _invoice(tmp_db, gross=119.00)
    con = sqlite3.connect(tmp_db.db_name)
    con.execute("UPDATE Invoices SET Status='paid', AmountDue=0 WHERE ID=?", (inv,))
    con.commit()
    con.close()
    assert inv not in {r[0] for r in tmp_db.get_open_invoices()}

    body = json.dumps({'invoice_id': inv, 'status': 'sent'}).encode()
    status, _ = handlers.handle_update_invoice_status(body)
    assert status == 200

    (due, st), = _raw(tmp_db, 'SELECT AmountDue, Status FROM Invoices WHERE ID=?', inv)
    assert due == 1190000 and st == 'sent'
    assert inv in {r[0] for r in tmp_db.get_open_invoices()}


# ── SKR-Backfill beim nachträglichen Verknüpfen ─────────────────────────────

def test_link_backfills_skr_and_creates_entry_child(tmp_db):
    tmp_db.insert_account('E2E-Bank', 'Ich', 'DE00', 'BIC', 'Bank', is_cash=0,
                          skr_account=1800)
    acct_id = [a for a in tmp_db.fetch_accounts() if a[1] == 'E2E-Bank'][0][0]
    tmp_db.insert_contact(display_name='Käufer AG', company_name='Käufer AG')
    contact_id = _raw(tmp_db,
                      "SELECT ID FROM Contacts WHERE DisplayName='Käufer AG'")[0][0]
    inv = _invoice(tmp_db, 'R-BF', gross=119.00, customer_id=contact_id)
    # Importierte Bankbuchung: kein COA, keine Steuer, kein Kontakt, keine Beleg-Nr.
    bk = tmp_db.insert_booking('2026-02-01', 119.00, account_id=acct_id,
                               booking_type='bank', recipient_client='Käufer AG')

    ok, err = link_booking_to_invoice_capped(tmp_db, inv, bk)
    assert ok, err

    coa_4400 = tmp_db.get_coa_id_by_account_number(4400)
    coa_1800 = tmp_db.get_coa_id_by_account_number(1800)
    b = tmp_db.get_booking_by_id(bk)
    assert b[8] == coa_4400                       # Erlöskonto nachgetragen
    assert b[13] == 0.19                          # Steuersatz
    assert b[14] == Decimal('19.0000')            # enthaltene USt
    assert b[7] == contact_id                     # Kunde nachgetragen
    assert b[16] == 'R-BF'                        # Beleg-Nr. = Rechnungsnummer
    # Buchungssatz (entry-Kind) mit Gegenkonto = SKR des Bankkontos
    entry = tmp_db.get_linked_entry_for_bank(bk)
    assert entry is not None
    assert entry[0] == coa_4400 and entry[1] == coa_1800


def test_link_backfill_never_overwrites(tmp_db):
    coa_4405 = tmp_db.get_coa_id_by_account_number(4405)
    inv = _invoice(tmp_db, 'R-KEEP', gross=119.00)
    bk = _bank_booking(tmp_db, coa_id=coa_4405, tax_rate=0.07, tax_amount=7.79,
                       document_number='EIGENE-NR')
    ok, err = link_booking_to_invoice_capped(tmp_db, inv, bk)
    assert ok, err
    b = tmp_db.get_booking_by_id(bk)
    assert b[8] == coa_4405 and b[13] == 0.07     # nichts überschrieben
    assert b[14] == Decimal('7.7900')
    assert b[16] == 'EIGENE-NR'


def test_revenue_coa_is_learned_from_the_payment_rows_only(tmp_db):
    """Das Erlöskonto je Steuersatz lernt die App nur aus den Buchungssätzen, die sie selbst für
    eine Zuordnung angelegt hat – nicht aus dem Konto der Bankzeile. Das kann alles sein: hier
    4405 (Wartekonto, Stellvertreter für ein beliebiges falsches Konto); früher lernte der
    §19-Satz daraus „4405“ und die nächste Zahlung buchte „4405 an Bank“."""
    coa_4405 = tmp_db.get_coa_id_by_account_number(4405)
    coa_4185 = tmp_db.get_coa_id_by_account_number(4185)
    coa_4400 = tmp_db.get_coa_id_by_account_number(4400)

    # §19-Rechnung: Die Bankbewegung trägt ein fremdes Konto
    inv1 = _invoice(tmp_db, 'R-KU1', gross=100.00, tax_rate=-1, tax_amount=0)
    bk1 = _bank_booking(tmp_db, 100.00, date='2026-03-01', coa_id=coa_4405)
    assert link_booking_to_invoice_capped(tmp_db, inv1, bk1)[0]
    assert tmp_db.get_booking_by_id(bk1)[8] == coa_4405          # die Anzeige der Bankzeile bleibt
    assert tmp_db.resolve_revenue_coa(-1) == coa_4185            # … und lehrt nichts

    # Nächste §19-Rechnung landet auf 4185 (nicht auf 4405)
    inv2 = _invoice(tmp_db, 'R-KU2', gross=50.00, tax_rate=-1, tax_amount=0)
    bk2 = _bank_booking(tmp_db, amount=50.00)
    assert link_booking_to_invoice_capped(tmp_db, inv2, bk2)[0]
    assert _raw(tmp_db, 'SELECT COA_ID FROM Bookings WHERE ParentBooking_ID = ?', bk2) == [(coa_4185,)]

    # Das Formular „Zahlung verbuchen“ bietet kein Erlöskonto an: Die Kontierung entsteht
    # aus der Rechnung
    inv3 = _invoice(tmp_db, 'R-KU3', gross=70.00, tax_rate=-1, tax_amount=0)
    html = PageTransactions(tmp_db, from_invoice=inv3)
    assert 'name="coa_id"' not in html

    # Andere Steuersätze lernen nicht mit: 19% fällt weiter auf 4400 zurück
    assert tmp_db.resolve_revenue_coa(0.19) == coa_4400


def test_learning_takes_the_youngest_payment_row_of_the_rate_and_only_if_it_is_a_revenue_account(tmp_db):
    """Der jüngste Satz zum Steuersatz gewinnt; ist sein Konto kein Erlöskonto (4000–4999 ohne
    Wartekonten), gilt das Standardkonto. (Ohne Forderung steht das Erlöskonto im Satz
    „Erlöskonto an Bank“ als Konto.)"""
    coa_4400 = tmp_db.get_coa_id_by_account_number(4400)
    tmp_db.insert_chart_of_accounts(4, 4401, 'Erlöse Sonderfall', 'Betriebliche Erträge')
    other_coa = tmp_db.get_coa_id_by_account_number(4401)
    for number, day in (('R-L1', '2026-03-01'), ('R-L2', '2026-03-02')):
        invoice = _invoice(tmp_db, number, gross=119.00)
        assert link_booking_to_invoice_capped(tmp_db, invoice, _bank_booking(tmp_db, 119.00,
                                                                             date=day))[0]
    older, younger = [r[0] for r in _raw(
        tmp_db, "SELECT ID FROM Bookings WHERE Status = 'invoice_payment' AND COA_ID = ? "
                "ORDER BY ID", coa_4400)]

    def teach(row_id, coa):
        con = sqlite3.connect(tmp_db.db_name)
        con.execute('UPDATE Bookings SET COA_ID = ? WHERE ID = ?', (coa, row_id))
        con.commit()
        con.close()

    teach(older, other_coa)
    assert tmp_db.resolve_revenue_coa(0.19) == coa_4400      # der jüngste (4400) gewinnt
    teach(younger, other_coa)
    assert tmp_db.resolve_revenue_coa(0.19) == other_coa     # jetzt trägt der jüngste 4401

    for not_revenue in (4405, 6300, 1800):                   # Wartekonto, Aufwand, Bank
        teach(younger, tmp_db.get_coa_id_by_account_number(not_revenue))
        assert tmp_db.resolve_revenue_coa(0.19) == coa_4400, not_revenue
    assert tmp_db.resolve_revenue_coa(0.07) == tmp_db.get_coa_id_by_account_number(4300)


def test_tax_free_invoice_gets_coa_4185_and_entry_child(tmp_db):
    # §19-/0%-Rechnung ohne Historie: Kleinunternehmer-Konto 4185 (DATEV)
    # wird gesetzt und der Buchungssatz (entry-Kind = grünes Häkchen) erzeugt
    tmp_db.insert_account('BF-Bank', 'Ich', 'DE01', 'BIC', 'Bank', is_cash=0,
                          skr_account=1800)
    acct_id = [a for a in tmp_db.fetch_accounts() if a[1] == 'BF-Bank'][0][0]
    inv = _invoice(tmp_db, 'R-0PCT', gross=100.00, tax_rate=-1, tax_amount=0)
    bk = tmp_db.insert_booking('2026-02-01', 100.00, account_id=acct_id,
                               booking_type='bank', recipient_client='Käufer AG')

    ok, err = link_booking_to_invoice_capped(tmp_db, inv, bk)
    assert ok, err

    coa_4185 = tmp_db.get_coa_id_by_account_number(4185)
    b = tmp_db.get_booking_by_id(bk)
    assert b[8] == coa_4185
    assert b[13] is None and b[14] is None      # keine Steuer bei §19
    entry = tmp_db.get_linked_entry_for_bank(bk)
    assert entry is not None and entry[0] == coa_4185


def test_resolve_creates_4185_in_legacy_db(tmp_db):
    # Ältere DBs haben 4185 nicht im Seed → wird beim ersten Bedarf angelegt
    coa_4185 = tmp_db.get_coa_id_by_account_number(4185)
    con = sqlite3.connect(tmp_db.db_name)
    con.execute('DELETE FROM ChartOfAccounts WHERE ID = ?', (coa_4185,))
    con.commit()
    con.close()
    assert tmp_db.get_coa_id_by_account_number(4185) is None

    resolved = tmp_db.resolve_revenue_coa(-1)
    assert resolved is not None
    assert resolved == tmp_db.get_coa_id_by_account_number(4185)
    # Explizite 0% landen im selben Topf
    assert tmp_db.resolve_revenue_coa(0) == resolved


# ── Formular-Prefill (from_invoice) ──────────────────────────────────────────

def test_page_transactions_prefill_from_invoice(tmp_db):
    inv = _invoice(tmp_db, 'R-PRE', gross=119.00)
    html = PageTransactions(tmp_db, from_invoice=inv)
    assert 'Zahlung zu Rechnung R-PRE' in html
    assert 'value="119.00"' in html                    # Betrag = offener Rest
    assert 'Zahlung Rechnung R-PRE' in html            # Verwendungszweck
    assert f'value="{inv}" selected' in html           # Rechnung im Dropdown
    # Kein Erlöskonto, kein Steuersatz: die Kontierung entsteht aus der Rechnung; das
    # Bankkonto ist Pflicht
    assert 'name="coa_id"' not in html
    assert 'name="tax_rate"' not in html and 'name="tax_amount"' not in html
    assert 'Kontierung (Erlöskonto, Umsatzsteuer) entsteht aus der Rechnung' in html
    assert '<select name="account" required>' in html

    # Teilzahlung: Prefill = Restbetrag
    bk = _bank_booking(tmp_db, amount=19.00)
    link_booking_to_invoice_capped(tmp_db, inv, bk)
    html = PageTransactions(tmp_db, from_invoice=inv)
    assert 'value="100.00"' in html


def test_page_transactions_prefill_kleinunternehmer(tmp_db):
    # Sentinel -1 (§19): auch hier weder Steuer noch Erlöskonto in der Maske
    inv = _invoice(tmp_db, 'R-K', gross=100.00, tax_rate=-1, tax_amount=0)
    html = PageTransactions(tmp_db, from_invoice=inv)
    assert 'Zahlung zu Rechnung R-K' in html
    assert 'value="100.00"' in html
    assert 'name="tax_rate"' not in html and 'name="coa_id"' not in html


def test_page_transactions_prefill_rejects_quote_and_paid(tmp_db):
    q = _invoice(tmp_db, 'Q-PRE', document_type='quote')
    assert 'Zahlung zu Rechnung' not in PageTransactions(tmp_db, from_invoice=q)
    p = _invoice(tmp_db, 'R-PAID2', status='paid')
    assert 'Zahlung zu Rechnung' not in PageTransactions(tmp_db, from_invoice=p)
    f = _invoice(tmp_db, 'R-FINAL2', status='finalized')      # noch nicht versendet
    assert 'Zahlung zu Rechnung' not in PageTransactions(tmp_db, from_invoice=f)
