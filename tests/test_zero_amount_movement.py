# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Bankbewegung über 0,00 € ohne Buchungssatz gilt nicht als offen (Entscheidung des Owners, 2.8.4).

Der Fall: Eine Bank bucht Zeilen über 0,00 € (eine Quartalsabrechnung ohne Zinsen). Der Import legt sie als gewöhnliche Bankbewegung an
(``tests/test_import_zero_amount.py``) – verbuchen lässt sich daran nichts, sie stand aber als „offen“ in der Liste und zählte im Dashboard
und in der Liquiditätsrechnung als Bewegung ohne Buchungssatz, bis jemand einen Satz über 0,00 € darunter hängte.

* Buchungsliste: graues Abzeichen „0,00“ statt „offen“, keine Zeile in der Farbe der offenen Bewegungen.
* Dashboard: ``unlinked_count`` zählt sie nicht; ``bank_count`` und der Saldo bleiben.
* Liquiditätsrechnung: Die Zahl der Bankbewegungen ohne Buchungssatz zählt sie nicht (ihr Betrag war schon 0).
* DATEV-Export und Summen- und Saldenliste übergingen sie schon vorher – hier festgehalten.
* Jeder andere Betrag bleibt offen, auch ein Cent.

Namen und Beträge sind erfunden.
"""
import pytest

from db import susa
from server.pages_transactions import ZERO_MOVEMENT_HINT, PageTransactions, is_zero_movement
from tests.test_invoice_payment_assignment import _account, _bank

FIRST, LAST = '2026-01-01', '2026-12-31'
ZERO_TEXT = 'Quartalsabrechnung ohne Zinsen'
OPEN_BADGE = "<span class='badge bg-orange' title='Noch nicht verbucht'>offen</span>"
ZERO_BADGE = f"<span class='badge bg-gray' title='{ZERO_MOVEMENT_HINT}'>0,00</span>"


@pytest.fixture
def db(tmp_db):
    return tmp_db


@pytest.fixture
def acct(db):
    return _account(db)


def row_of(page, text):
    """Die Tabellenzeile der Buchungsliste, die ``text`` als Verwendungszweck trägt."""
    start = page.index(f"data-text='{text}'")
    return page[page.rindex('<tr', 0, start):page.index('</tr>', start)]


def page_of(db):
    return PageTransactions(db, date_from=FIRST, date_to=LAST)


# ── Buchungsliste ────────────────────────────────────────────────────────────

def test_the_list_shows_a_zero_movement_without_the_open_badge(db, acct):
    _bank(db, acct, 0.00, date='2026-03-31', text=ZERO_TEXT)
    _bank(db, acct, 12.50, date='2026-03-31', text='Gutschrift Probe')

    page = page_of(db)

    zero, other = row_of(page, ZERO_TEXT), row_of(page, 'Gutschrift Probe')
    assert ZERO_BADGE in zero and OPEN_BADGE not in zero
    assert "class='transaction-row' " in zero and 'row-open' not in zero and "style='color:inherit'>0,00<" in zero
    assert OPEN_BADGE in other and "class='transaction-row row-open' " in other
    assert 'openEditForm(' in zero and '/transactions/delete?id=' in zero            # bearbeiten und löschen wie jede Bewegung


@pytest.mark.parametrize('amount', [0.01, -0.01, 119.00, -0.50])
def test_every_other_amount_stays_open(db, acct, amount):
    _bank(db, acct, amount, date='2026-03-31', text='Bewegung Probe')
    row = row_of(page_of(db), 'Bewegung Probe')
    assert OPEN_BADGE in row and 'row-open' in row and ZERO_BADGE not in row
    assert db.get_dashboard(FIRST, LAST)[0]['unlinked_count'] == 1


@pytest.mark.parametrize('value, expected', [(0, True), (0.0, True), (-0.0, True), (None, False), ('', False), ('0', False), (0.01, False), (-5, False)])
def test_only_a_stored_amount_of_exactly_zero_is_a_zero_movement(value, expected):
    assert is_zero_movement(value) is expected


def test_a_zero_movement_with_an_entry_keeps_its_ordinary_status(db, acct):
    """Mit einem Satz darunter ist sie eine gewöhnliche verbuchte Bewegung – die Regel gilt nur der Bewegung ohne Satz."""
    bank = _bank(db, acct, 0.00, date='2026-03-31', text=ZERO_TEXT)
    db.insert_booking('2026-03-31', 0.00, booking_type='entry', parent_booking_id=bank, text='Satz Probe')
    row = row_of(page_of(db), ZERO_TEXT)
    assert ZERO_BADGE not in row and OPEN_BADGE not in row


# ── Dashboard ────────────────────────────────────────────────────────────────

def test_the_dashboard_does_not_count_a_zero_movement_as_open(db, acct):
    _bank(db, acct, 0.00, date='2026-03-31', text=ZERO_TEXT)
    _bank(db, acct, -0.00, date='2026-04-30', text='Entgelt entfaellt')
    _bank(db, acct, 12.50, date='2026-03-31', text='Gutschrift Probe')

    totals = db.get_dashboard(FIRST, LAST)[0]

    assert (totals['bank_count'], totals['unlinked_count']) == (3, 1)                  # gezählt werden alle drei, offen ist eine
    assert round(totals['balance'], 2) == 12.50


# ── Liquidität, DATEV-Export, Summen- und Saldenliste ────────────────────────

def test_the_liquidity_statement_does_not_count_a_zero_movement(db, acct):
    _bank(db, acct, 0.00, date='2026-03-31', text=ZERO_TEXT)
    conn = db._get_connection()
    try:
        assert db._liquidity_unbooked(conn.cursor(), '2026-03-01', '2026-03-31', [acct]) == (0, 0)
    finally:
        conn.close()
    _bank(db, acct, 12.50, date='2026-03-15', text='Gutschrift Probe')
    conn = db._get_connection()
    try:
        assert db._liquidity_unbooked(conn.cursor(), '2026-03-01', '2026-03-31', [acct]) == (125000, 1)
    finally:
        conn.close()


def test_the_export_check_and_the_ledger_list_never_named_a_zero_movement(db, acct):
    _bank(db, acct, 0.00, date='2026-03-31', text=ZERO_TEXT)
    assert db.check_transfers(FIRST, LAST).unbooked == []
    assert [notice.key for notice in db.get_susa(2026, 3).notices if notice.key == susa.UNBOOKED] == []
