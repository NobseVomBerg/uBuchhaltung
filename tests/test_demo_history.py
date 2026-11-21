# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der große Demo-Bestand (``demo_history.build_history`` + ``demo_seed``): mehrere Jahre Buchhaltung über die App-Codepfade.

Zwei Teile. Der Erzeuger ist eine reine Funktion – ohne Datenbank, ohne Zufall, ohne Uhr; seine Regeln (Stichtag, Werktage,
keine Rechnung in den letzten 14 Tagen, zwei Stornos je Jahr, Ersatzrechnung erst ab dem 3. Quartal …) prüfen die ersten Tests
für mehrere Stichtage. Der zweite Teil lädt den Bestand EINMAL (Profil ``test``, fester Stichtag) und prüft, dass er in sich
stimmt: lückenlose Nummern in Datumsfolge, leere Prüfliste, ein DATEV-Export ohne Hinweis für jedes abgeschlossene Jahr, ausgeglichene
Splits, Anlagen mit AfA, ein Fahrtenbuch ohne Lücken. Alle Namen, Orte und Beträge sind erfundene Testwerte.
"""
import contextlib
import datetime
import json
import os
import shutil
import sqlite3
import zlib
from decimal import Decimal
from types import SimpleNamespace

import pytest

import auth
import demo_history
import demo_seed
import userctx
from db import Database
from server.handlers import handle_datev_export

# Ein fester Tag, nie die Uhr: Die Suite läuft Minuten (tests/test_clock_at_import.py)
TODAY = datetime.date(2026, 11, 3)
PROFILE = "test"
FIRST_YEAR, CURRENT_YEAR = TODAY.year - 1, TODAY.year
RECENT_LIMIT = TODAY - datetime.timedelta(days=14)
CUT_OFFS = [datetime.date(2026, 11, 3), datetime.date(2026, 6, 30), datetime.date(2027, 3, 31),
            datetime.date(2028, 2, 29), datetime.date(2026, 12, 31), datetime.date(2026, 2, 2)]
SEED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed_data", "test")


def _seed_json(name):
    with open(os.path.join(SEED_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def _iso(text):
    return datetime.date.fromisoformat(text)


def _without_days_ago(item):
    """Ohne die Angaben, die mit dem Stichtag wandern (``days_ago``, auch verschachtelt, und das Beleg-Kennzeichen der letzten
    12 Monate)."""
    if isinstance(item, dict):
        return {key: _without_days_ago(value) for key, value in item.items() if key not in ("days_ago", "receipt")}
    if isinstance(item, list):
        return [_without_days_ago(value) for value in item]
    return item


# ── der Erzeuger (rein) ──────────────────────────────────────────────────────
def test_build_history_is_deterministic():
    """Zweimal dieselben Vorgaben – für beide Profile; nichts hängt an der Uhr, an Dict-Reihenfolgen oder Zufall."""
    for profile in demo_history.PROFILES:
        assert demo_history.build_history(TODAY, profile) == demo_history.build_history(TODAY, profile)


def test_pick_is_the_documented_crc32_choice():
    table = ["a", "b", "c", "d", "e"]
    assert demo_history._pick(table, 2026, 3, "x") == table[zlib.crc32(b"2026-3-x") % 5]
    assert demo_history._pick(table, 2026, 3, "x") == demo_history._pick(table, 2026, 3, "x")


def test_an_unknown_profile_is_refused():
    with pytest.raises(ValueError):
        demo_history.build_history(TODAY, "riesig")


def test_a_calendar_month_looks_the_same_for_two_cut_off_days():
    """Ein Kalendermonat sieht unabhängig vom Stichtag gleich aus (nur ``days_ago`` wandert mit): zwei Stichtage im selben
    Monat ergeben für die Monate davor dieselben Rechnungen, Ausgaben, Anlagen und Überträge."""
    early, late = demo_history.build_history(TODAY, "full"), demo_history.build_history(datetime.date(2026, 11, 20), "full")
    for key, upto in (("invoices", "2026-08-31"), ("one_off", "2026-08-31"), ("splits", "2026-08-31"),
                      ("assets", "2026-08-31"), ("transfers", "2026-08-31"), ("trips", "2026-05-31")):
        before = [_without_days_ago(item) for item in early[key] if item["date"] <= upto]
        assert before, key
        assert before == [_without_days_ago(item) for item in late[key] if item["date"] <= upto], key


@pytest.mark.parametrize("profile", ["test", "full"])
@pytest.mark.parametrize("today", CUT_OFFS)
def test_nothing_lies_after_the_cut_off_day_and_the_chains_keep_their_order(profile, today):
    """Nichts nach dem Stichtag; keine Zahlung vor ihrer Rechnung; kein Storno vor der Zahlung; Erstattung nach der Ersatzrechnung;
    keine Rechnung (auch keine Storno- oder Ersatzrechnung) jünger als 14 Tage; alles an Werktagen."""
    history = demo_history.build_history(today, profile)
    limit = today - datetime.timedelta(days=14)
    for spec in history["invoices"]:
        day = _iso(spec["date"])
        assert day <= limit and day.weekday() < 5, spec["key"]
        for payment in spec["payments"]:
            paid = day + datetime.timedelta(days=payment["after_days"])
            assert day < paid <= today, spec["key"]
        if "cancel_after_days" in spec:
            cancelled = day + datetime.timedelta(days=spec["cancel_after_days"])
            assert cancelled <= limit, spec["key"]
            assert all(cancelled >= day + datetime.timedelta(days=p["after_days"]) for p in spec["payments"])
            replacement = spec.get("replacement")
            if replacement:
                replaced = cancelled + datetime.timedelta(days=replacement["after_days"])
                assert replaced <= limit and replaced >= datetime.date(replaced.year, 7, 1), spec["key"]
                if "refund_after_days" in replacement:
                    assert replaced < replaced + datetime.timedelta(days=replacement["refund_after_days"]) <= today
        else:
            assert "replacement" not in spec, spec["key"]
    for key in ("one_off", "splits", "trips", "transfers", "open_bank", "assets", "quotes", "worktimes"):
        for item in history[key]:
            assert item["date"] <= today.isoformat(), key
    for key in ("one_off", "splits", "trips", "transfers", "open_bank", "assets"):
        for item in history[key]:
            assert (today - _iso(item["date"])).days == item["days_ago"], key


def test_each_year_has_two_stornos_and_the_replacement_starts_in_the_third_quarter():
    """Je Jahr zwei Stornos – eines unbezahlt, eines bezahlt mit niedrigerer Ersatzrechnung und Erstattung des Rests."""
    history = demo_history.build_history(TODAY, "full")
    for year in range(2023, 2026):
        chain = [spec for spec in history["invoices"] if spec["key"].startswith(f"{year}-storno")]
        assert sorted(spec["key"] for spec in chain) == [f"{year}-storno-refund", f"{year}-storno-unpaid"]
        unpaid = next(spec for spec in chain if spec["key"].endswith("unpaid"))
        refund = next(spec for spec in chain if spec["key"].endswith("refund"))
        assert unpaid["payments"] == [] and "cancel_after_days" in unpaid and "replacement" not in unpaid
        assert len(refund["payments"]) == 1 and "refund_after_days" in refund["replacement"]
        quantity = lambda items: sum(item["quantity"] for item in items)
        assert quantity(refund["replacement"]["items"]) < quantity(refund["items"])           # niedriger
        replaced = _iso(refund["date"]) + datetime.timedelta(
            days=refund["cancel_after_days"] + refund["replacement"]["after_days"])
        assert replaced >= datetime.date(year, 7, 1)


def test_the_first_year_is_a_small_business_year_and_later_years_show_tax():
    history = demo_history.build_history(TODAY, "full")
    for spec in history["invoices"]:
        first = spec["date"][:4] == "2023"
        assert spec["show_tax"] is not first, spec["key"]
        assert all((item["tax_rate"] == 0) is first for item in spec["items"]), spec["key"]
    for item in history["one_off"]:
        if item["date"][:4] == "2023":
            assert item["tax_rate"] is None and item["tax_amount"] is None, item["date"]
    for split in history["splits"]:
        if split["date"][:4] == "2023":
            assert all(row["tax_rate"] is None and row["tax_amount"] is None for row in split["rows"]), split["date"]
    rents = [item["amount"] for item in history["one_off"] if item["coa"] == 6310]
    assert min(rents) == Decimal("-595.00") and max(rents) == Decimal("-520.00")


@pytest.mark.parametrize("day, expected", [
    (datetime.date(2026, 11, 7), datetime.date(2026, 11, 6)),        # Samstag → Freitag
    (datetime.date(2026, 11, 8), datetime.date(2026, 11, 9)),        # Sonntag → Montag
    (datetime.date(2026, 11, 6), datetime.date(2026, 11, 6)),        # Werktag bleibt
    (datetime.date(2023, 12, 31), datetime.date(2023, 12, 29)),      # Sonntag am Jahresende: nicht ins neue Jahr
    (datetime.date(2022, 1, 1), datetime.date(2022, 1, 3)),          # Samstag am Jahresanfang: nicht ins alte Jahr
])
def test_workday_moves_a_weekend_but_never_into_another_year(day, expected):
    assert demo_history._workday(day, datetime.date(2030, 1, 1)) == expected


def test_workday_never_passes_the_bound():
    assert demo_history._workday(datetime.date(2026, 11, 8), datetime.date(2026, 11, 8)) == datetime.date(2026, 11, 6)
    assert demo_history._workday(datetime.date(2026, 11, 9), datetime.date(2026, 11, 8)) is None


# ── der geladene Bestand ─────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    """Der Bestand, einmal geladen: Testdaten ohne die fest datierten Anlagen, Angebote und Arbeitszeiten, dann das Szenario
    mit dem Erzeuger (Profil ``test``, fester Stichtag). Die Datenwurzel liegt unter ``tmp_path_factory`` – nichts davon landet
    im echten data/ (``pytest.MonkeyPatch.context``, weil eine modulweite Fixture den ``monkeypatch`` je Test nicht nutzen darf)."""
    root = tmp_path_factory.mktemp("demo_history")
    path = str(root / "loaded.db")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, "DATA_ROOT", str(root / "data"))
        patch.setattr(auth, "AUTH_DB", str(root / "data" / "auth.db"))
        userctx._mode_cache.clear()
        db = Database(path)
        db.load_test_seed_data(include_invoices=False, include_dated=False)
        demo_seed.load_demo_scenario(db, today=TODAY, profile=PROFILE)
    userctx._mode_cache.clear()
    return SimpleNamespace(db=db, path=path, root=root, history=demo_history.build_history(TODAY, PROFILE))


def _rows(loaded, sql, *params):
    with contextlib.closing(sqlite3.connect(loaded.path)) as con:
        return con.execute(sql, params).fetchall()


@pytest.fixture
def copy(loaded, tmp_path):
    """Eine Kopie als Datenbank: Der DATEV-Export stempelt das Steuerdatum, das gemeinsame Exemplar bleibt unberührt."""
    path = str(tmp_path / "copy.db")
    shutil.copyfile(loaded.path, path)
    return Database(path)


def test_invoice_numbers_run_without_gaps_and_in_date_order_in_every_year(loaded):
    """Die Nummern entstehen über die App in Datumsfolge – auch die der Storno- und Ersatzrechnungen und des umgewandelten Angebots."""
    numbers = _rows(loaded, "SELECT InvoiceNumber, InvoiceDate FROM Invoices WHERE InvoiceNumber LIKE '__R___' ORDER BY InvoiceNumber")
    years = {}
    for number, day in numbers:
        years.setdefault(number[:2], []).append((int(number[3:]), day))
    assert sorted(years) == [str(FIRST_YEAR)[2:], str(CURRENT_YEAR)[2:]]
    for year, entries in years.items():
        assert [n for n, _ in entries] == list(range(1, len(entries) + 1)), year
        days = [d for _, d in entries]
        assert days == sorted(days), year
        assert all(d[:4] == f"20{year}" for d in days)
    assert len(numbers) > 120


def test_every_invoice_and_quote_status_occurs(loaded):
    invoice_statuses = {row[0] for row in _rows(
        loaded, "SELECT DISTINCT Status FROM Invoices WHERE DocumentType = 'invoice' AND InvoiceKind IS NOT 'storno'")}
    assert invoice_statuses == {"draft", "finalized", "sent", "overdue", "partial_payment", "paid", "cancelled"}
    assert {row[0] for row in _rows(loaded, "SELECT DISTINCT Status FROM Invoices WHERE DocumentType = 'quote'")} == \
        {"draft", "sent", "accepted", "rejected", "converted"}


def test_a_credit_note_a_replacement_and_a_refund_exist_and_hang_together(loaded):
    """Je Jahr ein Storno-, ein Ersatz- und ein Erstattungspaar: Die Ersatzrechnung hat die Zahlung übernommen, der Rest ging
    zurück, das Guthaben ist ausgeglichen."""
    db = loaded.db
    for year in (FIRST_YEAR, CURRENT_YEAR):
        stornos = _rows(loaded, "SELECT s.InvoiceNumber, o.InvoiceNumber, r.InvoiceNumber, r.Status, r.SumGross, o.SumGross FROM Invoices s "
                              "JOIN Invoices o ON o.ID = s.CancelsInvoiceId LEFT JOIN Invoices r ON r.ReplacesInvoiceId = o.ID "
                              "WHERE s.InvoiceKind = 'storno' AND substr(o.InvoiceDate, 1, 4) = ?", str(year))
        replaced = [row for row in stornos if row[2] is not None]
        assert len(stornos) >= 2 and len(replaced) == 1, year
        _storno, _original, _replacement, status, replacement_gross, original_gross = replaced[0]
        assert status == "paid" and replacement_gross < original_gross                       # niedriger, bezahlt durch den Umzug
        refunds = _rows(loaded, "SELECT COUNT(*), SUM(ip.Amount) FROM InvoicePayments ip JOIN Invoices s ON s.ID = ip.InvoiceID "
                              "WHERE s.InvoiceKind = 'storno' AND s.InvoiceNumber = ?", replaced[0][0])
        assert refunds[0][0] == 1 and refunds[0][1] == -(original_gross - replacement_gross)
    assert set(db.get_storno_credits().values()) == {Decimal(0)}             # die Erstattung ist vollständig (kein offenes Guthaben)


def test_the_payment_check_finds_nothing(loaded):
    assert loaded.db.find_payment_inconsistencies() == []


@pytest.mark.parametrize("year", [FIRST_YEAR, CURRENT_YEAR])
def test_the_splits_balance_in_every_year(loaded, year):
    assert loaded.db.find_unbalanced_splits(f"{year}-01-01", f"{year}-12-31") == []


def test_a_closed_year_exports_to_datev_without_a_hint(copy):
    """Das abgeschlossene Jahr: keine Bankbewegung ohne Satz, kein Übertrag über die Zeitraumgrenze, nichts möglicherweise doppelt –
    der Export liefert die Datei ohne Bestätigung."""
    first, last = f"{FIRST_YEAR}-01-01", f"{FIRST_YEAR}-12-31"
    check = copy.check_transfers(first, last)
    assert (check.unbooked, check.elsewhere, check.elsewhere_out, check.doubtful) == ([], [], [], [])
    result, name = handle_datev_export(copy, {"date_from": [first], "date_to": [last]})
    assert isinstance(result, (bytes, bytearray)) and len(result) > 1000 and name.endswith(".csv")


def test_the_current_year_asks_for_a_confirmation_because_three_movements_are_not_booked_yet(copy):
    status, location = handle_datev_export(copy, {"date_from": [f"{CURRENT_YEAR}-01-01"], "date_to": [f"{CURRENT_YEAR}-12-31"]})
    assert status == 303 and "datev_export=confirm" in location
    assert len(copy.check_transfers(f"{CURRENT_YEAR}-01-01", f"{CURRENT_YEAR}-12-31").unbooked) == 3


def test_only_the_current_month_has_bank_movements_without_a_booking_record(loaded):
    """Absichtlich offen: genau drei Bankbewegungen ohne Satz (heute, gestern, vorgestern), nicht vor dem Monatsersten."""
    open_rows = _rows(loaded, "SELECT DateBooking FROM Bookings b WHERE BookingType = 'bank' AND NOT EXISTS "
                            "(SELECT 1 FROM Bookings c WHERE c.ParentBooking_ID = b.ID) ORDER BY DateBooking")
    assert [row[0] for row in open_rows] == ["2026-11-01", "2026-11-02", "2026-11-03"]


def test_transfers_to_the_savings_account_are_booked_on_both_sides_the_same_day(loaded):
    """Jeder Eingang auf dem Tagesgeld hat am selben Tag seinen Ausgang auf dem Geschäftskonto, beide mit ihrem Spiegel kontiert."""
    incoming = _rows(loaded, "SELECT b.DateBooking, b.Amount, (SELECT COUNT(*) FROM Bookings c WHERE c.ParentBooking_ID = b.ID "
                           "AND c.EntryForm = 'mirror_counter') FROM Bookings b WHERE b.Account_ID = "
                           "(SELECT ID FROM Accounts WHERE Name = 'Tagesgeld')")
    assert len(incoming) == len([t for t in loaded.history["transfers"] if t["kind"] == "savings"]) >= 3
    for day, amount, mirrors in incoming:
        assert amount > 0 and mirrors == 1, day
        out = _rows(loaded, "SELECT COUNT(*) FROM Bookings b WHERE b.DateBooking = ? AND b.Amount = ? AND b.Account_ID = "
                          "(SELECT ID FROM Accounts WHERE Name = 'Geschäftskonto') AND EXISTS (SELECT 1 FROM Bookings c "
                          "WHERE c.ParentBooking_ID = b.ID AND c.EntryForm = 'mirror_counter')", day, -amount)
        assert out == [(1,)], day


def test_depreciation_is_booked_for_every_closed_year_and_the_sale_is_recorded(loaded):
    db = loaded.db
    assets = _rows(loaded, "SELECT ID, Name, Status, SaleDate FROM Assets")
    assert len(assets) == len(loaded.history["assets"]) == 4
    for asset_id, name, _status, _sale in assets:
        plan = db.depreciation_plan(asset_id)
        assert plan, name
        for entry in plan:
            assert entry["booked"] is (entry["year"] < CURRENT_YEAR), (name, entry["year"])
    assert [(name, sale) for _id, name, status, sale in assets if status == "sold"] == [("Notebook Entwicklung", "2026-06-09")]
    afa = _rows(loaded, "SELECT COUNT(*) FROM AssetDepreciations d JOIN Bookings b ON b.ID = d.Booking_ID WHERE b.DateBooking = ?",
                f"{FIRST_YEAR}-12-31")
    assert afa[0][0] == 2                                              # Notebook und Büromöbel (Kauf im Kleinunternehmerjahr)


def test_the_purchase_of_an_asset_is_no_expense_in_the_euer(loaded):
    """Der Kauf des PKW (Anlagenjahr) steht auf dem Anlagekonto, nicht unter den Betriebsausgaben – wie die Seite der EÜR sie zählt
    (``euer_section``: Konten ab 1000 ohne Erlöse, Privat, Bank und Wartekonten)."""
    from db.reporting import bank_skr_numbers, euer_section
    data = loaded.db.get_euer_data(f"{CURRENT_YEAR}-01-01", f"{CURRENT_YEAR}-12-31")
    euer = {number: total for number, _name, total in data}
    assert euer[520] == pytest.approx(-23800.0)
    banks = bank_skr_numbers(loaded.db.fetch_accounts())
    expenses = {number: total for number, total in euer.items() if euer_section(number, banks) == "expense"}
    assert expenses and all(abs(total) < 23800 for total in expenses.values())
    assert 6222 not in euer                                            # die AfA des laufenden Jahres ist erst geplant


def test_the_logbook_has_no_gaps_and_ends_at_the_start_reading_of_the_scenario(loaded):
    db = loaded.db
    scenario = _seed_json("demo_scenario.json")
    vehicle = scenario["vehicle"]
    result = db.trip_logbook_check(vehicle, CURRENT_YEAR)
    assert result["gaps_count"] == 0 and result["gaps"] == []
    generated = len(loaded.history["trips"])
    assert result["trips"] == generated + len(scenario["trips"]) and generated > 0
    # die letzte Fahrt des Erzeugers endet genau beim Startstand des Szenarios
    ends = _rows(loaded, "SELECT EndKm FROM Trips WHERE StartKm < ? ORDER BY StartDate DESC, ID DESC LIMIT 1", scenario["start_km"])
    assert ends == [(scenario["start_km"],)]
    car = next(a for a in loaded.history["assets"] if a["car"])
    assert loaded.history["trips"][0]["date"] > car["date"]                              # ab dem Kauf des PKW


def test_customers_are_seed_contacts_and_invented_recipients_are_recognisable(loaded):
    contacts = _seed_json("test_contacts.json")
    names = {c.get("display_name") for c in contacts} | {c.get("company_name") for c in contacts}
    buyers = {row[0] for row in _rows(loaded, "SELECT BuyerName FROM Invoices")} | \
        {row[0] for row in _rows(loaded, "SELECT BuyerCompany FROM Invoices WHERE BuyerCompany <> ''")}
    assert buyers <= names
    scenario = _seed_json("demo_scenario.json")
    known = {e["recipient"] for key in ("monthly", "quarterly", "one_off") for e in scenario[key]} | \
        {s["recipient"] for s in scenario["splits"]} | names | {"", "Eigenes Konto"}
    for key in ("one_off", "splits", "assets", "transfers", "open_bank"):
        for item in loaded.history[key]:
            assert item["recipient"] in known or item["recipient"].endswith("Beispielweg"), (key, item["recipient"])


def test_documents_exist_only_for_invoices_of_the_last_30_days(loaded):
    """PDF und E-Rechnung nur für Rechnungen der letzten 30 Tage; für ältere gilt „eingefroren, aber keine Datei vorhanden“."""
    cut = (TODAY - datetime.timedelta(days=30)).isoformat()
    for number, day, status, pdf in _rows(loaded, "SELECT InvoiceNumber, InvoiceDate, Status, PDFPath FROM Invoices "
                                                "WHERE DocumentType = 'invoice'"):
        if pdf:
            assert day >= cut and os.path.isfile(pdf), number
        elif status not in ("draft", "finalized"):
            assert day < cut or status == "cancelled", number
    assert _rows(loaded, "SELECT COUNT(*) FROM Invoices WHERE PDFPath IS NOT NULL AND PDFPath <> ''")[0][0] >= 4


def test_no_invoice_of_the_generator_is_younger_than_14_days_and_the_sample_invoice_stays_the_scenarios(loaded):
    """Das E-Rechnungs-Beispiel (``tools/make_einvoice_samples.py``) nimmt die jüngste 19-%-Rechnung (keine Stornorechnung): Es bleibt
    die des Szenarios (K010, 12 Tage alt) – der Erzeuger legt nichts Jüngeres an."""
    younger = _rows(loaded, "SELECT InvoiceNumber, InvoiceDate FROM Invoices WHERE DocumentType = 'invoice' AND InvoiceDate > ?",
                    RECENT_LIMIT.isoformat())
    assert [day for _number, day in younger] == [(TODAY - datetime.timedelta(days=12)).isoformat()]
    normal = next(i for i in loaded.db.fetch_invoices() if float(i[35] or 0) in (19, 0.19) and i[50] != "storno")
    assert normal[1] == younger[0][0] and normal[2] == younger[0][1]
    assert normal[15] == "Buchhandlung Lesezeit e.K."


def test_the_converted_quote_became_a_draft_invoice_no_younger_than_14_days(loaded):
    rows = _rows(loaded, "SELECT i.InvoiceNumber, i.InvoiceDate, i.Status, q.Status FROM Invoices i "
                       "JOIN Invoices q ON q.ID = i.SourceQuoteId")
    assert len(rows) == 1
    number, day, status, quote_status = rows[0]
    assert (status, quote_status) == ("draft", "converted") and day <= RECENT_LIMIT.isoformat()
    assert number == loaded.db.fetch_invoices(status="draft")[0][1]


def test_worktimes_use_only_known_kinds_short_notes_and_one_entry_per_day(loaded):
    from server.pages_worktime import KIND_LABELS, NOTE_MAX_LENGTH
    rows = _rows(loaded, "SELECT PersonID, Date, Kind, Note FROM WorkTimes")
    assert 120 <= len(rows) <= 220 and len({row[0] for row in rows}) == 1
    assert {row[2] for row in rows} == set(KIND_LABELS)
    assert max(len(row[3]) for row in rows) <= NOTE_MAX_LENGTH
    assert len({row[1] for row in rows}) == len(rows)
    assert max(row[1] for row in rows) <= TODAY.isoformat()


def test_the_receipts_of_single_expenses_are_metadata_without_a_file(loaded):
    rows = _rows(loaded, "SELECT d.Number, d.Path, b.DateBooking FROM Documents d JOIN BookingDocuments l ON l.Document_ID = d.ID "
                       "JOIN Bookings b ON b.ID = l.Booking_ID")
    assert rows and all(path == "" for _n, path, _d in rows)
    assert min(day for _n, _p, day in rows) >= (TODAY - datetime.timedelta(days=365)).isoformat()


def test_the_first_year_is_a_small_business_year_in_the_books(loaded):
    """Die Rechnungen des Erzeugers im ersten Jahr tragen keinen Steuerausweis (Platzhalter -1) und buchen auf das Erlöskonto 4185."""
    generated = [s for s in loaded.history["invoices"] if not s["show_tax"]]
    assert generated
    small = _rows(loaded, "SELECT COUNT(*) FROM Invoices WHERE DocumentType = 'invoice' AND InvoiceKind IS NOT 'storno' AND TaxRate < 0")[0][0]
    replacements = [s for s in generated if "replacement" in s]
    assert small == len(generated) + len(replacements)
    revenue = _rows(loaded, "SELECT COUNT(*) FROM Bookings b JOIN ChartOfAccounts c ON c.ID = b.CounterCOA_ID "
                          "WHERE c.AccountNumber = 4185 AND b.Status = 'invoice_payment'")[0][0]
    assert revenue > 0


# ── die App-Änderungen dazu ──────────────────────────────────────────────────
def test_the_dated_test_data_can_be_left_out(tmp_db):
    """``include_dated=False`` lässt die fest datierten Buchungen, Anlagen, Angebote und Arbeitszeiten weg (die Demo bekäme sonst
    ein fast leeres Jahr vor ihrer Historie und Buchungen mit Steuerausweis im Kleinunternehmerjahr) – Kontakte, Konten, Artikel
    und Belege bleiben; der Standard lädt alles wie bisher."""
    tmp_db.load_test_seed_data(include_invoices=False, include_dated=False)
    counts = dict(tmp_db.get_table_statistics())
    assert counts["Assets"] == counts["WorkTimes"] == counts["Bookings"] == 0
    assert counts["Documents"] > 0
    assert counts["Invoices"] == 0 and counts["Contacts"] > 20 and counts["Accounts"] >= 3 and counts["Articles"] == 50
    tmp_db.load_test_seed_data(include_invoices=False)
    counts = dict(tmp_db.get_table_statistics())
    assert counts["Assets"] == len(_seed_json("test_assets.json")) and counts["WorkTimes"] == len(_seed_json("test_worktimes.json"))
    assert counts["Invoices"] == len(_seed_json("test_quotes.json")) > 0
    assert counts["Bookings"] > 0


def test_a_quote_converts_on_the_day_it_is_given(tmp_db):
    tmp_db.load_test_seed_data(include_invoices=False)
    quote = tmp_db.fetch_quotes()[0]
    invoice_id = tmp_db.convert_quote_to_invoice(quote[0], today=datetime.date(2024, 5, 6))
    invoice = tmp_db.get_invoice_by_id(invoice_id)
    assert invoice[2] == "2024-05-06" and invoice[25] == "2024-05-06" and invoice[1].startswith("24")
    other = tmp_db.fetch_quotes()[1]
    later = tmp_db.convert_quote_to_invoice(other[0])                    # ohne Angabe: der heutige Tag wie bisher
    assert tmp_db.get_invoice_by_id(later)[2] == datetime.date.today().isoformat()
