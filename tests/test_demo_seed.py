# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Erweiterte Demodaten (demo_seed.py): konsistent über die App-Codepfade."""
import datetime
import sqlite3

import pytest

import demo
import demo_seed
import userctx
from db import Database
from tests import installation


@pytest.fixture(autouse=True)
def _data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(userctx, "DATA_ROOT", str(tmp_path / "data"))
    userctx._mode_cache.clear()
    yield
    userctx._mode_cache.clear()


def _build(tmp_path, today):
    db = Database(str(tmp_path / f"demo_{today}.db"))
    db.load_test_seed_data(include_invoices=False)
    demo_seed.DemoScenario(db, today=today).load_all()
    return db


@pytest.mark.parametrize("today", [
    datetime.date(2026, 11, 3),      # Herbst: beide Jahre gut gefüllt
    datetime.date(2027, 1, 5),       # Jahresanfang: laufendes Jahr fast leer
    datetime.date(2026, 6, 30),
])
def test_demo_scenario_is_consistent(tmp_path, today):
    db = _build(tmp_path, today)
    con = sqlite3.connect(db.db_name)

    # keine Zahlung vor dem Rechnungsdatum, keine Zahlung in der Zukunft
    rows = con.execute(
        "SELECT i.InvoiceNumber, i.InvoiceDate, p.PaymentDate FROM InvoicePayments p "
        "JOIN Invoices i ON i.ID = p.InvoiceID").fetchall()
    assert rows
    for number, inv_date, pay_date in rows:
        assert pay_date >= inv_date, number
        assert pay_date <= today.isoformat(), number

    # Splits gehen auf, DATEV-Export läuft für beide Jahre ohne Fehler durch
    from server.handlers import handle_datev_export
    for year in (today.year - 1, today.year):
        d_from, d_to = f"{year}-01-01", f"{year}-12-31"
        assert db.find_unbalanced_splits(d_from, d_to) == []
        if year == today.year and today.month == 1 and today.day < 8:
            continue                       # kaum Buchungen im frischen Jahr
        result, name = handle_datev_export(db, {"date_from": [d_from], "date_to": [d_to]})
        assert isinstance(result, (bytes, bytearray)), (year, name)
        assert name.endswith(".csv")

    # Debitoren und Wartekonten sind Bestandskonten und bleiben aus der EÜR heraus
    euer = {r[0]: r[2] for r in db.get_euer_data(f"{today.year - 1}-01-01", f"{today.year - 1}-12-31")}
    assert not {10000, 4405, 4345, 4340} & set(euer)


def test_demo_prepare_fills_euer_current_year(tmp_path, monkeypatch):
    """Über den echten Startpfad: EÜR des laufenden Jahres nicht leer,
    Einnahmen > Betriebsausgaben, DATEV-Export fehlerfrei."""
    monkeypatch.setenv(demo.ENV_VAR, "1")
    assert demo.prepare(log=lambda m: None) == demo.SEEDED
    db = installation.open_db(demo.USERNAME)             # data/users/demo/buch.db
    year = datetime.date.today().year
    d_from, d_to = f"{year}-01-01", f"{year}-12-31"
    euer = db.get_euer_data(d_from, d_to)
    assert euer, "EÜR des laufenden Jahres ist leer"
    revenue = sum(v for n, _, v in euer if 4000 <= n < 5000)
    expenses = -sum(v for n, _, v in euer if 6000 <= n < 7000)
    if datetime.date.today() >= datetime.date(year, 3, 1):
        assert revenue > expenses > 0
    from server.handlers import handle_datev_export
    result, _ = handle_datev_export(db, {"date_from": [d_from], "date_to": [d_to]})
    assert isinstance(result, (bytes, bytearray))


def test_demo_has_open_overdue_and_partial_invoices(tmp_path):
    db = _build(tmp_path, datetime.date(2026, 11, 3))
    con = sqlite3.connect(db.db_name)
    statuses = {r[0] for r in con.execute("SELECT Status FROM Invoices")}
    assert {"paid", "partial_payment", "sent"} <= statuses
    assert con.execute("SELECT COUNT(*) FROM Trips").fetchone()[0] >= 5
    # Splits vorhanden (Bankbewegung mit mehreren Buchungssätzen)
    multi = con.execute(
        "SELECT COUNT(*) FROM (SELECT ParentBooking_ID FROM Bookings "
        "WHERE ParentBooking_ID IS NOT NULL GROUP BY ParentBooking_ID HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    assert multi >= 3


def test_invoice_list_statistics_use_gross_and_payments(tmp_path):
    """Statistik der Rechnungsliste: Brutto-Summe, bezahlt (inkl. Teilzahlung), offen – ohne Stornorechnungen; eine
    stornierte Rechnung zählt nur in der Summe, nicht in „bezahlt“ und „offen“."""
    import re
    from server.pages_invoice import PageInvoice
    db = _build(tmp_path, datetime.date(2026, 11, 3))
    con = sqlite3.connect(db.db_name)
    rows = con.execute("SELECT SumGross, AmountDue, Status FROM Invoices "
                       "WHERE DocumentType = 'invoice' AND InvoiceKind IS NOT 'storno'").fetchall()
    gross = sum(g for g, _due, _status in rows)
    paid_expected = sum(g - due for g, due, status in rows if status != 'cancelled')
    open_expected = sum(due for _g, due, status in rows if status not in ('paid', 'cancelled'))
    html = PageInvoice(db, {"date_from": "2000-01-01", "date_to": "2100-12-31"})
    m = re.search(r"Gesamtsumme: ([\d.,]+) € \| \s*Bezahlt: ([\d.,]+) € \| \s*Offen: ([\d.,]+) €", html)
    assert m, "Statistikzeile nicht gefunden"
    total, paid, open_ = (float(x.replace('.', '').replace(',', '.')) for x in m.groups())
    assert total == pytest.approx(gross / 10000, abs=0.01)
    assert open_ == pytest.approx(open_expected / 10000, abs=0.01)
    assert paid == pytest.approx(paid_expected / 10000, abs=0.01) and paid > 0
    assert "Stornorechnungen (nicht mitgezählt): 1" in html


def test_demo_has_a_credit_note_pair_with_a_counter_booking(tmp_path):
    """Die Demo enthält ein Storno-Paar, entstanden über ``cancel_invoice``: Stornorechnung mit eigener Nummer,
    Ursprungsrechnung storniert, Forderung und Gegenbuchung heben sich auf."""
    db = _build(tmp_path, datetime.date(2026, 11, 3))
    con = sqlite3.connect(db.db_name)
    ((storno_id, storno_number, original_id, gross),) = con.execute(
        "SELECT ID, InvoiceNumber, CancelsInvoiceId, SumGross FROM Invoices WHERE InvoiceKind = 'storno'").fetchall()
    original_number, status, original_gross = con.execute(
        "SELECT InvoiceNumber, Status, SumGross FROM Invoices WHERE ID = ?", (original_id,)).fetchone()
    assert status == "cancelled" and gross == -original_gross and gross < 0
    debtors = con.execute(
        "SELECT SUM(Amount) FROM Bookings WHERE DocumentNumber IN (?, ?) AND BookingType = 'entry' "
        "AND COA_ID = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 10000)",
        (original_number, storno_number)).fetchone()[0]
    assert debtors == 0
    assert db.find_payment_inconsistencies() == []
