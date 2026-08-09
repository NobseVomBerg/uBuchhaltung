# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Offene Posten: Wartekonten, Forderungsbuchung und bankwirksame Splits.

Eine versendete Rechnung wird als offener Posten gebucht (Debitoren 10000 an
Wartekonto 4405/4345/4340); erst die Zahlung bucht von dort auf das echte
Erlöskonto um. Die Umbuchung hängt zwar an der Bankbewegung, bewegt aber kein
Geld – sie darf weder die Split-Summe noch die EÜR verfälschen.
"""
import json
from decimal import Decimal

from db.matching import is_bank_effective
from server.handlers import (handle_add_transaction, handle_datev_export,
                             handle_update_invoice_status,
                             link_booking_to_invoice_capped)
from server.pages_transactions import PageTransactions


def _invoice(db, number='R-1', gross=119.00, rate=0.19, status='finalized', **kw):
    net = round(gross / (1 + rate), 2) if rate > 0 else gross
    data = {
        'invoice_number': number, 'invoice_date': '2026-01-01',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': rate, 'sum_net': net, 'tax_amount': round(gross - net, 2),
        'sum_gross': gross, 'amount_due': gross, 'status': status,
    }
    data.update(kw)
    return db.insert_invoice(data)


def _bank_account(db, name='OP-Bank', skr=1800):
    db.insert_account(name, 'Owner', 'DE00OP', 'BIC', 'Bank',
                      is_cash=0, skr_account=skr)
    return [a for a in db.fetch_accounts() if a[1] == name][0][0]


def _set_status(db, invoice_id, status, monkeypatch):
    """handle_update_invoice_status ruft Database() selbst auf – auf die
    Test-DB umbiegen, damit der Statuswechsel dort landet."""
    import server.handlers as h
    monkeypatch.setattr(h, 'Database', lambda *a, **k: db)
    return handle_update_invoice_status(
        json.dumps({'invoice_id': invoice_id, 'status': status}).encode())


# ── Wartekonten ──────────────────────────────────────────────────────────────

def test_open_revenue_accounts_per_tax_rate(tmp_db):
    assert tmp_db.resolve_open_revenue_coa(0.19) == \
        tmp_db.get_coa_id_by_account_number(4405)
    assert tmp_db.resolve_open_revenue_coa(0.07) == \
        tmp_db.get_coa_id_by_account_number(4345)
    # 0% und der §19-Sentinel -1 teilen sich das Wartekonto 4340
    coa_4340 = tmp_db.get_coa_id_by_account_number(4340)
    assert tmp_db.resolve_open_revenue_coa(0) == coa_4340
    assert tmp_db.resolve_open_revenue_coa(-1) == coa_4340
    # Satz ohne Wartekonto: kein offener Posten, gebucht wird bei Zahlung
    assert tmp_db.resolve_open_revenue_coa(0.05) is None


def test_open_revenue_account_is_created_when_missing(tmp_db):
    """Bestands-DB ohne 4345: das Konto wird nachgelegt statt zu scheitern."""
    import sqlite3
    con = sqlite3.connect(tmp_db.db_name)
    con.execute('DELETE FROM ChartOfAccounts WHERE AccountNumber = 4345')
    con.commit()
    con.close()

    coa = tmp_db.resolve_open_revenue_coa(0.07)
    assert coa and coa == tmp_db.get_coa_id_by_account_number(4345)


# ── Forderungsbuchung am Status ──────────────────────────────────────────────

def test_sent_invoice_books_open_item(tmp_db, monkeypatch):
    inv = _invoice(tmp_db, 'R-OP1', gross=119.00)
    _set_status(tmp_db, inv, 'sent', monkeypatch)

    bid = tmp_db.get_receivable_booking(inv)
    assert bid
    b = tmp_db.get_booking_by_id(bid)
    assert b[8] == tmp_db.get_coa_id_by_account_number(10000)   # Debitoren
    assert b[9] == tmp_db.get_coa_id_by_account_number(4405)    # Wartekonto
    assert b[11] == Decimal('119.0000')
    assert b[16] == 'R-OP1'
    assert b[17] == 'entry' and b[18] is None   # eigenständig, keine Bank


def test_open_item_is_idempotent_and_removable(tmp_db, monkeypatch):
    inv = _invoice(tmp_db, 'R-OP2')
    _set_status(tmp_db, inv, 'sent', monkeypatch)
    first = tmp_db.get_receivable_booking(inv)
    _set_status(tmp_db, inv, 'overdue', monkeypatch)
    assert tmp_db.get_receivable_booking(inv) == first   # keine zweite Buchung

    _set_status(tmp_db, inv, 'finalized', monkeypatch)
    assert tmp_db.get_receivable_booking(inv) is None    # zurückgenommen


def test_draft_invoice_has_no_open_item(tmp_db, monkeypatch):
    inv = _invoice(tmp_db, 'R-OP3', status='draft')
    _set_status(tmp_db, inv, 'finalized', monkeypatch)
    assert tmp_db.get_receivable_booking(inv) is None


def test_open_item_survives_when_payment_exists(tmp_db, monkeypatch):
    """Storno nach Zahlung räumt die Forderung nicht weg – das entscheidet
    der Nutzer, sonst verschwände ein bereits exportierter Beleg."""
    inv = _invoice(tmp_db, 'R-OP4', gross=119.00)
    _set_status(tmp_db, inv, 'sent', monkeypatch)
    acct = _bank_account(tmp_db)
    bk = tmp_db.insert_booking('2026-02-01', 119.00, account_id=acct,
                               booking_type='bank')
    ok, err = link_booking_to_invoice_capped(tmp_db, inv, bk)
    assert ok, err

    _set_status(tmp_db, inv, 'cancelled', monkeypatch)
    assert tmp_db.get_receivable_booking(inv) is not None


# ── Zahlung erzeugt Zahlungs- und Umbuchungssatz ─────────────────────────────

def _payment_children(db, bank_id):
    return {c[8]: c for c in
            [db.get_booking_by_id(x[0]) for x in
             db.get_child_bookings_for_bank(bank_id)]}


def test_payment_creates_settlement_and_transfer(tmp_db, monkeypatch):
    inv = _invoice(tmp_db, 'R-PAY', gross=119.00)
    _set_status(tmp_db, inv, 'sent', monkeypatch)
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    debtor = tmp_db.get_coa_id_by_account_number(10000)
    open_coa = tmp_db.get_coa_id_by_account_number(4405)
    bk = tmp_db.insert_booking('2026-02-01', 119.00, account_id=acct,
                               booking_type='bank')

    ok, err = link_booking_to_invoice_capped(tmp_db, inv, bk)
    assert ok, err

    kids = _payment_children(tmp_db, bk)
    assert set(kids) == {bank_coa, open_coa}
    zahlung, umb = kids[bank_coa], kids[open_coa]
    assert zahlung[9] == debtor and zahlung[13] is None          # ohne Steuer
    assert umb[9] == tmp_db.get_coa_id_by_account_number(4400)
    assert umb[14] == Decimal('19.0000')                         # USt am Erlös
    assert zahlung[11] == umb[11] == Decimal('119.0000')
    assert zahlung[15] == 'Zahlung zu Re. R-PAY'
    assert umb[15] == 'Umb. zu Re. R-PAY'


def test_partial_payment_transfers_only_the_allocated_share(tmp_db, monkeypatch):
    inv = _invoice(tmp_db, 'R-PART', gross=119.00)
    _set_status(tmp_db, inv, 'sent', monkeypatch)
    acct = _bank_account(tmp_db)
    bk = tmp_db.insert_booking('2026-02-01', 59.50, account_id=acct,
                               booking_type='bank')

    ok, err = link_booking_to_invoice_capped(tmp_db, inv, bk)
    assert ok, err

    amounts = {c[11] for c in
               [tmp_db.get_booking_by_id(x[0])
                for x in tmp_db.get_child_bookings_for_bank(bk)]}
    assert amounts == {Decimal('59.5000')}


def test_deleting_the_payment_reopens_the_item(tmp_db, monkeypatch):
    """Zahlung löschen: beide Buchungssätze verschwinden, die Rechnung ist
    wieder offen – der offene Posten selbst bleibt bestehen."""
    inv = _invoice(tmp_db, 'R-DEL', gross=119.00)
    _set_status(tmp_db, inv, 'sent', monkeypatch)
    receivable = tmp_db.get_receivable_booking(inv)
    acct = _bank_account(tmp_db)
    bk = tmp_db.insert_booking('2026-02-01', 119.00, account_id=acct,
                               booking_type='bank')
    link_booking_to_invoice_capped(tmp_db, inv, bk)

    tmp_db.delete_transaction(bk)

    assert tmp_db.get_child_bookings_for_bank(bk) == []
    assert tmp_db.get_receivable_booking(inv) == receivable
    assert tmp_db.get_invoice_by_id(inv)[39] == Decimal('119.0000')


def test_payment_without_open_item_keeps_single_child(tmp_db):
    """Fremdbeleg/Altbestand ohne Forderungsbuchung: unverändert ein Satz."""
    inv = _invoice(tmp_db, 'R-NOOP', gross=119.00)
    acct = _bank_account(tmp_db)
    bk = tmp_db.insert_booking('2026-02-01', 119.00, account_id=acct,
                               booking_type='bank')
    ok, err = link_booking_to_invoice_capped(tmp_db, inv, bk)
    assert ok, err
    assert len(tmp_db.get_child_bookings_for_bank(bk)) == 1


# ── Bankwirksamkeit ──────────────────────────────────────────────────────────

def test_is_bank_effective_rules():
    bank = {10}
    assert is_bank_effective(10, 99, bank)        # Zahlung, Bank vorne
    assert is_bank_effective(99, 10, bank)        # Ausgabe, Bank hinten
    assert not is_bank_effective(41, 44, bank)    # Umbuchung 4405→4400
    assert not is_bank_effective(21, 68, bank)    # Privatanteil 2100←6805
    assert is_bank_effective(None, 10, bank)      # unkontiert zählt mit
    assert is_bank_effective(68, None, bank)


def test_split_badge_ignores_transfer_rows(tmp_db, monkeypatch):
    """WISO-Muster: Zahlung + Umbuchung unter EINER Bankbewegung ergeben den
    Haken – die Umbuchung darf keinen Rest erzeugen."""
    inv = _invoice(tmp_db, 'R-BADGE', gross=119.00)
    _set_status(tmp_db, inv, 'sent', monkeypatch)
    acct = _bank_account(tmp_db)
    bk = tmp_db.insert_booking('2026-02-01', 119.00, account_id=acct,
                               booking_type='bank')
    link_booking_to_invoice_capped(tmp_db, inv, bk)

    html = PageTransactions(tmp_db, date_from='2026-01-01', date_to='2026-12-31')
    assert 'Bank + Buchungssätze vollständig' in html
    assert 'offener Rest' not in html


def test_private_share_row_does_not_count(tmp_db):
    """O2-Muster: -15,00 und -14,99 sind bankwirksam, der Privatanteil +7,50
    (6805 → 2100) ist eine reine Umbuchung."""
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    coa_tk = tmp_db.get_coa_id_by_account_number(6805)
    coa_priv = tmp_db.get_coa_id_by_account_number(2100)
    bk = tmp_db.insert_booking('2026-02-01', -29.99, account_id=acct,
                               booking_type='bank')
    for amount in (-15.00, -14.99):
        tmp_db.insert_booking('2026-02-01', amount, coa_id=coa_tk,
                              counter_coa_id=bank_coa, booking_type='entry',
                              parent_booking_id=bk)
    tmp_db.insert_booking('2026-02-01', 7.50, coa_id=coa_priv,
                          counter_coa_id=coa_tk, booking_type='entry',
                          parent_booking_id=bk)

    html = PageTransactions(tmp_db, date_from='2026-01-01', date_to='2026-12-31')
    assert 'Bank + Buchungssätze vollständig' in html
    assert 'offener Rest' not in html
    assert tmp_db.find_unbalanced_splits('2026-01-01', '2026-12-31') == []


def test_editor_save_keeps_transfer_counter_account(tmp_db, monkeypatch):
    """Speichern der Bankmaske darf aus der Umbuchung keine Zahlung machen."""
    inv = _invoice(tmp_db, 'R-KEEP', gross=119.00)
    _set_status(tmp_db, inv, 'sent', monkeypatch)
    acct = _bank_account(tmp_db)
    open_coa = tmp_db.get_coa_id_by_account_number(4405)
    revenue = tmp_db.get_coa_id_by_account_number(4400)
    bk = tmp_db.insert_booking('2026-02-01', 119.00, account_id=acct,
                               booking_type='bank')
    link_booking_to_invoice_capped(tmp_db, inv, bk)

    rows = [(c[0], c[1], c[2]) for c in tmp_db.get_child_bookings_for_bank(bk)]
    post = {'transaction_id': [str(bk)], 'date': ['2026-02-01'],
            'amount': ['119.00'], 'account': [str(acct)], 'currency': ['EUR'],
            'split_id': [], 'split_amount': [], 'split_coa': [],
            'split_tax_rate': [], 'split_tax_amount': [],
            'split_docnr': [], 'split_text': []}
    for rid, coa, _counter in rows:
        post['split_id'].append(str(rid))
        post['split_amount'].append('119.00')
        post['split_coa'].append(str(coa))
        post['split_tax_rate'].append('')
        post['split_tax_amount'].append('')
        post['split_docnr'].append('R-KEEP')
        post['split_text'].append('')
    handle_add_transaction(tmp_db, post)

    umb = [c for c in tmp_db.get_child_bookings_for_bank(bk) if c[1] == open_coa]
    assert len(umb) == 1
    assert umb[0][2] == revenue      # Gegenkonto unverändert, nicht die Bank


# ── EÜR bleibt Ist-Rechnung ──────────────────────────────────────────────────

def test_euer_shows_revenue_once_at_payment(tmp_db, monkeypatch):
    inv = _invoice(tmp_db, 'R-EUER', gross=119.00)
    _set_status(tmp_db, inv, 'sent', monkeypatch)

    # Nur versendet, noch nicht bezahlt → keine Einnahme
    euer = {nr: t for nr, _, t in tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert 4400 not in euer and 4405 not in euer and 10000 not in euer

    acct = _bank_account(tmp_db)
    bk = tmp_db.insert_booking('2026-02-01', 119.00, account_id=acct,
                               booking_type='bank')
    link_booking_to_invoice_capped(tmp_db, inv, bk)

    euer = {nr: t for nr, _, t in tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert round(euer[4400], 2) == 100.00      # Netto genau einmal
    assert round(euer[3806], 2) == 19.00       # USt am virtuellen Konto
    assert 4405 not in euer and 10000 not in euer


def test_euer_kleinunternehmer_uses_4340_and_4185(tmp_db, monkeypatch):
    inv = _invoice(tmp_db, 'R-19', gross=100.00, rate=-1)
    _set_status(tmp_db, inv, 'sent', monkeypatch)
    assert tmp_db.get_booking_by_id(tmp_db.get_receivable_booking(inv))[9] == \
        tmp_db.get_coa_id_by_account_number(4340)

    acct = _bank_account(tmp_db)
    bk = tmp_db.insert_booking('2026-02-01', 100.00, account_id=acct,
                               booking_type='bank')
    link_booking_to_invoice_capped(tmp_db, inv, bk)

    euer = {nr: t for nr, _, t in tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert round(euer[4185], 2) == 100.00
    assert 4340 not in euer and 3806 not in euer


# ── DATEV-Export ─────────────────────────────────────────────────────────────

def test_datev_export_rejects_unbalanced_split(tmp_db):
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = tmp_db.insert_booking('2026-02-01', -238.00, account_id=acct,
                               booking_type='bank')
    tmp_db.insert_booking('2026-02-01', -200.00, coa_id=coa,
                          counter_coa_id=bank_coa, booking_type='entry',
                          parent_booking_id=bk)

    found = tmp_db.find_unbalanced_splits('2026-01-01', '2026-12-31')
    assert [(f[0], f[3]) for f in found] == [(bk, Decimal('-38.0000'))]

    status, location = handle_datev_export(
        tmp_db, {'date_from': ['2026-01-01'], 'date_to': ['2026-12-31']})
    assert status == 303 and 'datev_export=error' in location


def test_datev_export_accepts_invoice_pattern(tmp_db, monkeypatch):
    """Zahlung + Umbuchung sind stimmig; die Forderungszeile geht mit."""
    inv = _invoice(tmp_db, 'R-DATEV', gross=119.00)
    _set_status(tmp_db, inv, 'sent', monkeypatch)
    acct = _bank_account(tmp_db)
    bk = tmp_db.insert_booking('2026-02-01', 119.00, account_id=acct,
                               booking_type='bank')
    link_booking_to_invoice_capped(tmp_db, inv, bk)

    assert tmp_db.find_unbalanced_splits('2026-01-01', '2026-12-31') == []
    result = handle_datev_export(
        tmp_db, {'date_from': ['2026-01-01'], 'date_to': ['2026-12-31']})
    assert isinstance(result[0], bytes)
    csv_text = result[0].decode('cp1252')
    assert csv_text.count('R-DATEV') >= 3   # Forderung + Zahlung + Umbuchung
    assert ';10000;' in csv_text or ';4405;' in csv_text


# ── Buchungen ohne Kind blockieren nicht ─────────────────────────────────────

def test_check_follows_the_children_dates(tmp_db):
    """Zugeordnete Buchungen behalten ihr eigenes Datum. Liegt ein Satz im
    Exportzeitraum, muss seine Bankbewegung geprüft werden – auch wenn die
    selbst davor datiert. Exportiert werden schließlich die Sätze."""
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = tmp_db.insert_booking('2025-12-30', -300.00, account_id=acct,
                               booking_type='bank', text='Sammelueberweisung')
    tmp_db.insert_booking('2026-01-05', -100.00, coa_id=coa,
                          counter_coa_id=bank_coa, booking_type='entry',
                          parent_booking_id=bk)

    found = tmp_db.find_unbalanced_splits('2026-01-01', '2026-12-31')
    assert [(f[0], f[3]) for f in found] == [(bk, Decimal('-200.0000'))]


def test_uncoded_bank_booking_is_not_a_split(tmp_db):
    acct = _bank_account(tmp_db)
    tmp_db.insert_booking('2026-02-01', -50.00, account_id=acct,
                          booking_type='bank')
    assert tmp_db.find_unbalanced_splits('2026-01-01', '2026-12-31') == []
