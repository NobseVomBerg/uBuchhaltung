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
    db = Database(userctx.user_db_path())
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
    """Statistik der Rechnungsliste: Brutto-Summe, bezahlt (inkl. Teilzahlung), offen."""
    import re
    from server.pages_invoice import PageInvoice
    db = _build(tmp_path, datetime.date(2026, 11, 3))
    con = sqlite3.connect(db.db_name)
    gross, due = con.execute(
        "SELECT SUM(SumGross), SUM(AmountDue) FROM Invoices WHERE DocumentType = 'invoice'"
    ).fetchone()
    html = PageInvoice(db, {"date_from": "2000-01-01", "date_to": "2100-12-31"})
    m = re.search(r"Gesamtsumme: ([\d.]+) € \| \s*Bezahlt: ([\d.]+) € \| \s*Offen: ([\d.]+) €", html)
    assert m, "Statistikzeile nicht gefunden"
    total, paid, open_ = (float(x) for x in m.groups())
    assert total == pytest.approx(gross / 10000, abs=0.01)
    assert open_ == pytest.approx(due / 10000, abs=0.01)
    assert paid == pytest.approx((gross - due) / 10000, abs=0.01) and paid > 0
