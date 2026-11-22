# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Beleg-Nr. mit MEHREREN Forderungszeilen: Stufe 7 des Auto-Abgleichs und das Entfernen einer Zahlung.

Eine Rechnung bucht heute eine Forderungszeile („10000 an Wartekonto“); trägt eine Beleg-Nr. mehrere solche Zeilen (eine Rechnung mit
mehreren Steuersätzen bekommt später je Satz eine), darf keine Zeile für sich mit der Summe ALLER Zahlungen der Beleg-Nr. verglichen werden:
Die kleinere wäre nach einer Teilzahlung zu früh ausgeglichen (``Status = 'resolved'``). Maßgeblich ist die Summe ALLER eigenständigen
Forderungszeilen der Nummer (auch der schon aufgelösten) – ist sie gedeckt (``money.is_covered``), werden alle noch offenen Zeilen der Nummer
gemeinsam ``resolved``; ist sie nach dem Entfernen einer Zahlung nicht mehr gedeckt (``Database._after_payment_removed``), gehen alle aufgelösten
gemeinsam zurück auf offen. Bei genau einer Zeile ändert sich nichts (``tests/test_invoice_payment_assignment.py``,
``tests/test_single_rate_snapshots.py``).

Beträge, Namen und Nummern sind erfunden.
"""
import sqlite3

import pytest

from db.matching import FORM_TRANSFER

NUMBER = 'R-MIX'


# ── Hilfen ───────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_db):
    return tmp_db


@pytest.fixture
def acct(db):
    db.insert_account('Auflösungs-Bank', 'Ich', 'DE00AUFLOESUNG', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    return [a for a in db.fetch_accounts() if a[1] == 'Auflösungs-Bank'][0][0]


def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _raw(db, sql, *params):
    con = sqlite3.connect(db.db_name)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _status(db, booking_id):
    return _raw(db, 'SELECT Status FROM Bookings WHERE ID = ?', booking_id)[0][0]


def _invoice(db, number, gross):
    """Eine versendete Rechnung über ``gross`` (Kopf mit 19 %) – ohne Forderungsbuchung: die Zeilen setzt der Test selbst."""
    net = round(gross / 1.19, 2)
    return db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': 0.19, 'sum_net': net, 'tax_amount': round(gross - net, 2),
        'sum_gross': gross, 'amount_due': gross, 'status': 'sent'})


def _receivable(db, number, amount, waiting=4405, rate=0.19):
    """Eine eigenständige Forderungszeile „10000 an Wartekonto“, wie ``_book_receivable`` sie schreibt."""
    return db.insert_booking('2026-01-01', amount, coa_id=_coa(db, 10000), counter_coa_id=_coa(db, waiting),
                             tax_rate=rate, tax_amount=round(amount - amount / (1 + rate), 2), text=f'Rechnung {number}',
                             document_number=number, booking_type='entry', entry_form=FORM_TRANSFER)


def _pay(db, invoice_id, account_id, amount, date='2026-02-01'):
    """Eine Zahlung über ``amount`` zuordnen („Bank an Debitoren“ und „Wartekonto an Erlöskonto“); Returns die ID der Zuordnung."""
    bank = db.insert_booking(date, amount, account_id=account_id, booking_type='bank', recipient_client='Käufer AG')
    result = db.assign_invoice_payment(invoice_id, bank, amount)
    assert result.ok, result.message
    return result.payment_id


def _mixed(db, acct):
    """Rechnung über 172,50 € mit zwei Forderungszeilen unter EINER Beleg-Nr.: 119,00 € (Wartekonto 19 %) und 53,50 € (Wartekonto 7 %)."""
    invoice = _invoice(db, NUMBER, 172.50)
    return invoice, _receivable(db, NUMBER, 119.00), _receivable(db, NUMBER, 53.50, waiting=4345, rate=0.07)


# ── Mehrere Forderungszeilen ─────────────────────────────────────────────────

def test_a_part_payment_that_would_cover_the_smaller_row_resolves_neither(db, acct):
    """60,00 € auf 119,00 € + 53,50 €: Für sich genommen wäre die kleinere Zeile (53,50 €) „gedeckt“ – am Stand vor der neuen Regel
    wurde sie aufgelöst, obwohl die Rechnung erst zu einem Drittel bezahlt ist."""
    invoice, big, small = _mixed(db, acct)
    _pay(db, invoice, acct, 60.00)

    assert db.link_bank_to_entries()['resolved'] == 0
    assert (_status(db, big), _status(db, small)) == (None, None)


def test_both_rows_resolve_together_once_the_payments_cover_the_sum(db, acct):
    invoice, big, small = _mixed(db, acct)
    _pay(db, invoice, acct, 60.00)
    assert db.link_bank_to_entries()['resolved'] == 0

    _pay(db, invoice, acct, 112.50, date='2026-02-02')                    # 60,00 + 112,50 = 172,50: die ganze Rechnung

    assert db.link_bank_to_entries()['resolved'] == 2                     # gezählt wird, was sich ändert: beide Zeilen
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')
    assert db.link_bank_to_entries()['resolved'] == 0                     # nichts mehr zu tun


def test_removing_a_payment_reopens_both_rows_together(db, acct):
    invoice, big, small = _mixed(db, acct)
    _pay(db, invoice, acct, 60.00)
    last = _pay(db, invoice, acct, 112.50, date='2026-02-02')
    db.link_bank_to_entries()
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')

    db.unassign_invoice_payment(last)                                      # die Summe ist nicht mehr gedeckt

    assert (_status(db, big), _status(db, small)) == (None, None)
    assert db.link_bank_to_entries()['resolved'] == 0                      # die verbleibenden 60,00 € decken die Summe nicht


def test_removing_a_payment_that_the_rest_still_covers_leaves_both_rows_resolved(db, acct):
    """Zwei Zahlungen, jede für sich so hoch wie die Summe (die zweite ein Satz „Bank an Debitoren“ ohne Zuordnung, Altbestand): Fällt
    die zugeordnete weg, deckt die andere die Summe weiter – die Zeilen bleiben aufgelöst."""
    invoice, big, small = _mixed(db, acct)
    first = _pay(db, invoice, acct, 172.50)
    legacy_bank = db.insert_booking('2026-02-03', 172.50, account_id=acct, booking_type='bank', recipient_client='Käufer AG')
    legacy = db.insert_booking('2026-02-03', 172.50, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000), booking_type='entry',
                               parent_booking_id=legacy_bank, document_number=NUMBER, text='Zahlung (Altbestand, ohne Zuordnung)')
    db.link_bank_to_entries()
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')

    db.unassign_invoice_payment(first)

    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')
    assert db.get_booking_by_id(legacy) is not None


def test_a_row_resolved_earlier_still_counts_in_the_sum(db, acct):
    """Eine Zeile, die der Bestand schon als aufgelöst führt (nach der früheren Regel), zählt mit ihrem Betrag in der Summe: Nach
    119,00 € ist die Summe 172,50 € nicht gedeckt – die andere Zeile bleibt offen, erst die volle Zahlung löst sie."""
    invoice, big, small = _mixed(db, acct)
    con = sqlite3.connect(db.db_name)
    con.execute("UPDATE Bookings SET Status = 'resolved' WHERE ID = ?", (big,))
    con.commit()
    con.close()
    _pay(db, invoice, acct, 119.00)

    assert db.link_bank_to_entries()['resolved'] == 0
    assert (_status(db, big), _status(db, small)) == ('resolved', None)

    _pay(db, invoice, acct, 53.50, date='2026-02-02')

    assert db.link_bank_to_entries()['resolved'] == 1                      # nur die Zeile, die sich ändert
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')


def test_rows_of_different_numbers_resolve_on_their_own(db, acct):
    paid_invoice, open_invoice = _invoice(db, 'R-PAID', 119.00), _invoice(db, 'R-OPEN', 119.00)
    paid_row, open_row = _receivable(db, 'R-PAID', 119.00), _receivable(db, 'R-OPEN', 119.00)
    _pay(db, paid_invoice, acct, 119.00)
    _pay(db, open_invoice, acct, 50.00, date='2026-02-02')

    assert db.link_bank_to_entries()['resolved'] == 1
    assert (_status(db, paid_row), _status(db, open_row)) == ('resolved', None)


def test_a_row_without_a_number_is_left_alone(db, acct):
    invoice, big, small = _mixed(db, acct)
    nameless, empty = _receivable(db, None, 10.00), _receivable(db, '', 10.00)
    _pay(db, invoice, acct, 172.50)

    assert db.link_bank_to_entries()['resolved'] == 2
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')
    assert (_status(db, nameless), _status(db, empty)) == (None, None)


# ── Genau eine Forderungszeile: wie heute ────────────────────────────────────

def test_a_single_row_resolves_with_the_covering_payment_and_reopens_with_its_removal(db, acct):
    invoice = _invoice(db, 'R-ONE', 119.00)
    row = db.ensure_receivable_booking(invoice)                            # eine Zeile, wie beim Versenden gebucht
    _pay(db, invoice, acct, 50.00)

    assert db.link_bank_to_entries()['resolved'] == 0                      # nach der Teilzahlung noch offen
    assert _status(db, row) is None

    last = _pay(db, invoice, acct, 69.00, date='2026-02-02')
    assert db.link_bank_to_entries()['resolved'] == 1
    assert _status(db, row) == 'resolved'
    assert db.link_bank_to_entries()['resolved'] == 0

    db.unassign_invoice_payment(last)
    assert _status(db, row) is None                                        # wieder offen


def test_a_single_row_stays_resolved_while_the_rest_still_covers_it(db, acct):
    invoice = _invoice(db, 'R-ONE2', 119.00)
    row = db.ensure_receivable_booking(invoice)
    _pay(db, invoice, acct, 119.00)
    db.link_bank_to_entries()
    assert _status(db, row) == 'resolved'

    other = _pay(db, _invoice(db, 'R-ONE3', 119.00), acct, 119.00, date='2026-02-03')
    db.unassign_invoice_payment(other)

    assert _status(db, row) == 'resolved'
