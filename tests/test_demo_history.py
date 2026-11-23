# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der große Demo-Bestand (``demo_history.build_history`` + ``demo_seed``): mehrere Jahre Buchhaltung über die App-Codepfade.

Zwei Teile. Der Erzeuger ist eine reine Funktion – ohne Datenbank, ohne Zufall, ohne Uhr; seine Regeln (Stichtag, Werktage,
keine Rechnung in den letzten 14 Tagen, zwei Stornos je Jahr, Ersatzrechnung erst ab dem 3. Quartal …) prüfen die ersten Tests
für mehrere Stichtage. Der zweite Teil lädt den Bestand EINMAL (Profil ``test``, fester Stichtag) und prüft, dass er in sich
stimmt: lückenlose Nummern in Datumsfolge, leere Prüfliste, ein DATEV-Export ohne Hinweis für jedes abgeschlossene Jahr, ausgeglichene
Splits, Anlagen mit AfA, ein Fahrtenbuch ohne Lücken. Ein dritter Teil am Ende gilt der einen Rechnung mit Steuersatz je Position, die nur das
Profil ``full`` hat (``profiles.full.mixed_example``): ihre Regeln für mehrere Stichtage und, an einem eigenen Bestand dieses Profils, ihre Buchung je Satz
und die Proben (Summen- und Saldenliste, Liquidität, Prüfliste). Alle Namen, Orte und Beträge sind erfundene Testwerte.
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
from tests.datev_helpers import confirmed_export

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
        # Die steuerfreie Position der Beispielrechnung mit einem Eintrag der Stammdaten (``tax_entry``) hat den Satz 0 mit Steuerausweis – kein Kleinunternehmerjahr
        assert all((item["tax_rate"] == 0) is first for item in spec["items"] if "tax_entry" not in item), spec["key"]
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
    """Die Rechnungen des Erzeugers im ersten Jahr tragen keinen Steuerausweis (Platzhalter -1) und buchen auf das Erlöskonto für das Datum der Bankbewegung:
    4185 bis 2025, 4184 ab 2026 – im Profil ``test`` (fester Stichtag 2026-11-03) sind das genau zwei Zahlungen vom Januar 2026 auf Rechnungen vom Dezember 2025
    (4184), alle übrigen liegen im Jahr 2025 (4185)."""
    generated = [s for s in loaded.history["invoices"] if not s["show_tax"]]
    assert generated
    small = _rows(loaded, "SELECT COUNT(*) FROM Invoices WHERE DocumentType = 'invoice' AND InvoiceKind IS NOT 'storno' AND TaxRate < 0")[0][0]
    replacements = [s for s in generated if "replacement" in s]
    assert small == len(generated) + len(replacements)
    revenue = _rows(loaded, "SELECT COUNT(*) FROM Bookings b JOIN ChartOfAccounts c ON c.ID = b.CounterCOA_ID "
                          "WHERE c.AccountNumber = 4185 AND b.Status = 'invoice_payment'")[0][0]
    assert revenue > 0
    by_account = _rows(loaded, "SELECT c.AccountNumber, MIN(b.DateBooking), MAX(b.DateBooking), COUNT(*) FROM Bookings b "
                             "JOIN ChartOfAccounts c ON c.ID = b.CounterCOA_ID WHERE c.AccountNumber IN (4184, 4185) "
                             "AND b.Status = 'invoice_payment' GROUP BY c.AccountNumber ORDER BY c.AccountNumber")
    assert [row[0] for row in by_account] == [4184, 4185]
    new_account, old_account = by_account
    assert new_account[1] >= '2026-01-01' and new_account[3] == 2                         # die beiden Zahlungen vom Januar 2026
    assert old_account[2] <= '2025-12-31'                                                 # jede Zahlung mit dem Datum von 2025 liegt auf 4185


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


# ── die eine Rechnung mit Steuersatz je Position (nur das Profil full) ───────
FULL = "full"


def _mixed_specs(history):
    """Die Rechnung mit 7 % und 19 % (``mixed_example``): die mit Steuersatz je Position, deren Positionen keinen Eintrag der Stammdaten wählen. Die zweite –
    19 % und steuerfrei, mit ihrem Eintrag (``tax_entry_example``) – steht weiter unten."""
    return [spec for spec in history["invoices"] if spec.get("tax_mode") == "mixed" and not any("tax_entry" in item for item in spec["items"])]


def test_only_the_full_profile_has_the_one_invoice_with_a_rate_per_position():
    """Das Profil ``test`` und das handgepflegte Szenario bleiben ohne sie: Auf diesem Bestand stehen fest gepinnte Werte (EÜR, Summen- und
    Saldenliste, Liquidität)."""
    assert len(_mixed_specs(demo_history.build_history(TODAY, FULL))) == 1
    assert _mixed_specs(demo_history.build_history(TODAY, PROFILE)) == []
    assert not any(spec.get("tax_mode") for spec in _seed_json("demo_scenario.json")["invoices"])


@pytest.mark.parametrize("today", CUT_OFFS)
def test_the_example_with_a_rate_per_position_follows_the_rules_of_the_generator(today):
    """Ein Werktag, nie in den letzten 14 Tagen, nie im Kleinunternehmerjahr, mit Steuerausweis; zwei Raten, die zweite schließt – und was nach dem
    Stichtag läge, entfällt (hier liegt keine)."""
    specs = _mixed_specs(demo_history.build_history(today, FULL))

    assert len(specs) == 1
    spec = specs[0]
    day = _iso(spec["date"])
    assert day.weekday() < 5 and day <= today - datetime.timedelta(days=14) and day.year > today.year - 3
    assert spec["show_tax"] is True and spec["customer_number"] == "K010" and spec["key"] == f"{day.year}-mixed"
    assert sorted(item["tax_rate"] for item in spec["items"]) == [7, 19]
    first, second = spec["payments"]
    assert (first["percent"], second["percent"], second["closing"]) == (40, 60, True) and "closing" not in first
    assert day < day + datetime.timedelta(days=first["after_days"]) < day + datetime.timedelta(days=second["after_days"]) <= today


@pytest.fixture(scope="module")
def loaded_full(tmp_path_factory):
    """Der Bestand des Profils ``full`` (drei volle Vorjahre), einmal geladen – wie ``loaded``, mit der einen Beispielrechnung mit Steuersatz je Position."""
    root = tmp_path_factory.mktemp("demo_history_full")
    path = str(root / "loaded_full.db")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, "DATA_ROOT", str(root / "data"))
        patch.setattr(auth, "AUTH_DB", str(root / "data" / "auth.db"))
        userctx._mode_cache.clear()
        db = Database(path)
        db.load_test_seed_data(include_invoices=False, include_dated=False)
        demo_seed.load_demo_scenario(db, today=TODAY, profile=FULL)
    userctx._mode_cache.clear()
    return SimpleNamespace(db=db, path=path, root=root, history=demo_history.build_history(TODAY, FULL))


def test_the_example_is_one_invoice_with_a_forderung_per_rate_and_two_payments_that_split_by_rate(loaded_full):
    """7 % × 100,00 (4 Fachbücher zu 25,00) und 19 % × 100,00: netto 200,00, Steuer 7,00 + 19,00, brutto 226,00. Die erste Rate (40 %: 90,40) verteilt sich auf
    47,60 (Steuer 7,60) und 42,80 (2,80), die zweite (60 %: 135,60) auf 71,40 (11,40) und 64,20 (4,20) – je Satz zusammen genau das Brutto und die
    Steuer der Gruppe."""
    spec = _mixed_specs(loaded_full.history)[0]
    # den Tag der Rechnung nennt die Abfrage mit: Die Beispielrechnung mit steuerfreier Position (unten) trägt das Kennzeichen „Steuersatz je Position“ ebenfalls
    (invoice,) = _rows(loaded_full, "SELECT ID, InvoiceNumber, InvoiceDate, Status, SumNet, TaxAmount, SumGross, AmountDue, TaxRate, PDFPath, XMLPath "
                                    "FROM Invoices WHERE MixedTaxRates IS NOT NULL AND InvoiceDate = ?", spec["date"])
    invoice_id, number, day, status, net, tax, gross, due, head, pdf, xml = invoice
    assert (day, status, net, tax, gross, due, head) == (spec["date"], "paid", 2000000, 260000, 2260000, 0, 0.19)
    assert (pdf, xml) == (None, None)                                                  # älter als 30 Tage: Dokumente erzeugt „PDF + E-Rechnung“ später
    chain = _rows(loaded_full, "SELECT c.AccountNumber, g.AccountNumber, b.Amount, b.TaxRate, b.TaxAmount FROM Bookings b "
                               "JOIN ChartOfAccounts c ON c.ID = b.COA_ID JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID "
                               "WHERE b.DocumentNumber = ? AND b.BookingType = 'entry' AND b.ParentBooking_ID IS NULL ORDER BY b.ID", number)
    assert chain == [(10000, 4405, 1190000, 0.19, 190000), (10000, 4345, 1070000, 0.07, 70000)]
    assert _rows(loaded_full, "SELECT Amount FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID", invoice_id) == [(904000,), (1356000,)]
    transfers = _rows(loaded_full, "SELECT b.Amount, b.TaxAmount, c.AccountNumber, g.AccountNumber FROM Bookings b JOIN ChartOfAccounts c ON c.ID = b.COA_ID "
                                   "JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID WHERE b.DocumentNumber = ? AND b.Status = 'invoice_payment' "
                                   "AND b.EntryForm = 'transfer' ORDER BY b.ID", number)
    assert transfers == [(476000, 76000, 4405, 4400), (428000, 28000, 4345, 4300), (714000, 114000, 4405, 4400), (642000, 42000, 4345, 4300)]
    assert [(waiting, sum(row[0] for row in transfers if row[2] == waiting), sum(row[1] for row in transfers if row[2] == waiting))
            for waiting in (4405, 4345)] == [(4405, 1190000, 190000), (4345, 1070000, 70000)]


def test_the_books_with_the_example_pass_every_probe(loaded_full):
    """Prüfliste leer, Splits ausgeglichen, Summen- und Saldenliste im Gleichgewicht, Liquidität ohne „Nicht zugeordnet“ – in den Monaten der Rechnung
    und ihrer Raten."""
    from tests import liquidity_helpers as liq
    from tests import susa_helpers
    db = loaded_full.db
    assert db.find_payment_inconsistencies() == []
    assert db.find_unbalanced_splits("2023-01-01", f"{CURRENT_YEAR}-12-31") == []
    for month in (9, 10, 11):
        susa_helpers.assert_balanced(db.get_susa(CURRENT_YEAR, month), month)
        for variant in ("vorjahr", "jahr"):
            assert set(liq.unassigned_of(db.get_liquidity(CURRENT_YEAR, month, variant)).values()) == {0}, (month, variant)


# ── die zweite Rechnung: Steuersätze aus den Stammdaten (nur das Profil full, seit 2.8.0) ───────────────────
def _entry_specs(history):
    """Die Rechnungen der Vorgaben, deren Positionen einen Eintrag der Stammdaten wählen (``tax_entry``)."""
    return [spec for spec in history["invoices"] if any("tax_entry" in item for item in spec["items"])]


def _entry_invoice(stock):
    """``(ID, Nummer)`` der Rechnung im Bestand, deren Position die Momentaufnahme eines Steuersatzes der Stammdaten trägt – es ist die einzige: Der Tag allein
    bestimmt sie nicht (an einem Tag können mehrere Rechnungen liegen)."""
    (found,) = _rows(stock, "SELECT ID, InvoiceNumber FROM Invoices WHERE ID IN (SELECT InvoiceId FROM InvoiceItems WHERE TaxRateBp IS NOT NULL)")
    return found


def test_only_the_full_profile_has_the_two_entries_of_the_master_data_and_the_invoice_with_the_free_position():
    """Zwei Einträge in den Stammdaten (steuerfrei mit erfundenem Grund und dem Erlöskonto 4100, 5,5 % mit 4180 – zu 5,5 % gibt es keine Rechnung) und EINE
    Rechnung mit steuerfreier Position: nur im Profil ``full``. Das Profil ``test`` und das Szenario bleiben ohne sie – wie bei der Rechnung mit 7 % und 19 %."""
    full = demo_history.build_history(TODAY, FULL)

    assert [(entry["key"], entry["label"], entry["kind"], entry["bp"], entry["revenue_account"]) for entry in full["tax_entries"]] == [
        ("exempt", "Steuerfrei (Beispiel)", "exempt", 0, 4100), ("reduced", "5,5 % (Beispiel)", "standard", 550, 4180)]
    exempt, reduced = full["tax_entries"]
    assert exempt["reason"] == "Steuerfreie Beispielleistung (Demodaten)" and reduced["reason"] is None
    assert not any(sign in exempt["reason"] for sign in ("§", "UStG", "Paragraf"))             # erfunden: keine Vorschrift, die es gibt
    assert len(_entry_specs(full)) == 1
    assert not any(item.get("tax_entry") == "reduced" for spec in full["invoices"] for item in spec["items"])           # keine Rechnung zu 5,5 %
    test = demo_history.build_history(TODAY, PROFILE)
    assert "tax_entries" not in test and _entry_specs(test) == []
    assert not any("tax_entry" in item for spec in _seed_json("demo_scenario.json")["invoices"] for item in spec["items"])


@pytest.mark.parametrize("today", CUT_OFFS)
def test_the_example_with_the_free_position_follows_the_rules_of_the_generator(today):
    """Wie die Rechnung mit 7 % und 19 %: ein Werktag, nie in den letzten 14 Tagen, nie im Kleinunternehmerjahr, mit Steuerausweis, zwei Raten, die zweite
    schließt – an einem anderen Tag als jene. Die steuerfreie Position (Satz 0) wählt den Eintrag ``exempt``, die zu 19 % keinen."""
    history = demo_history.build_history(today, FULL)
    (spec,) = _entry_specs(history)
    (mixed,) = _mixed_specs(history)

    day = _iso(spec["date"])
    assert day.weekday() < 5 and day <= today - datetime.timedelta(days=14) and day.year > today.year - 3 and spec["date"] != mixed["date"]
    assert spec["tax_mode"] == "mixed" and spec["show_tax"] is True and spec["customer_number"] == "K003" and spec["key"] == f"{day.year}-tax-entry"
    assert [(item["tax_rate"], item.get("tax_entry")) for item in spec["items"]] == [(19, None), (0, "exempt")]
    first, second = spec["payments"]
    assert (first["percent"], second["percent"], second["closing"]) == (40, 60, True) and "closing" not in first
    assert day < day + datetime.timedelta(days=first["after_days"]) < day + datetime.timedelta(days=second["after_days"]) <= today


def test_the_profile_test_keeps_only_the_two_preset_rates_in_the_master_data(loaded):
    assert [(entry.label, entry.bp) for entry in loaded.db.fetch_tax_rates()] == [("19 %", 1900), ("7 %", 700)]


def test_the_example_with_the_free_position_is_booked_per_rate_and_both_entries_stand_in_the_master_data(loaded_full):
    """19 % × 100,00 (2 Stunden zu 50,00) und steuerfrei × 80,00: netto 180,00, Steuer 19,00, brutto 199,00. Die erste Rate (40 %: 79,60) verteilt sich auf 47,60
    (Steuer 7,60) und 32,00, die zweite (60 %: 119,40) auf 71,40 (11,40) und 48,00 – je Satz zusammen genau das Brutto der Gruppe. Das Erlöskonto der steuerfreien
    Position ist das des Eintrags (4100); die Position zu 19 % trägt keine Momentaufnahme."""
    db = loaded_full.db
    coa = dict(_rows(loaded_full, "SELECT AccountNumber, ID FROM ChartOfAccounts WHERE AccountNumber IN (4100, 4180)"))
    assert [(e.label, e.bp, e.kind, e.reason, e.revenue_coa_id, e.active, e.valid) for e in db.fetch_tax_rates()] == [
        ("19 %", 1900, "standard", None, None, True, True), ("7 %", 700, "standard", None, None, True, True),
        ("Steuerfrei (Beispiel)", 0, "exempt", "Steuerfreie Beispielleistung (Demodaten)", coa[4100], True, True),
        ("5,5 % (Beispiel)", 550, "standard", None, coa[4180], True, True)]

    invoice_id, number = _entry_invoice(loaded_full)
    (invoice,) = _rows(loaded_full, "SELECT InvoiceDate, Status, SumNet, TaxAmount, SumGross, AmountDue, TaxRate, MixedTaxRates, PDFPath, XMLPath FROM Invoices WHERE ID = ?",
                       invoice_id)
    assert invoice == (_entry_specs(loaded_full.history)[0]["date"], "paid", 1800000, 190000, 1990000, 0, 0.19, 1, None, None)
    assert _rows(loaded_full, "SELECT TaxCategory, TaxRate, TaxRateBp, TaxExemptReason, RevenueCoaId FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position",
                 invoice_id) == [("S", 19.0, None, None, None), ("E", 0.0, 0, "Steuerfreie Beispielleistung (Demodaten)", coa[4100])]
    chain = _rows(loaded_full, "SELECT c.AccountNumber, g.AccountNumber, b.Amount, b.TaxRate, b.TaxAmount FROM Bookings b "
                               "JOIN ChartOfAccounts c ON c.ID = b.COA_ID JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID "
                               "WHERE b.DocumentNumber = ? AND b.BookingType = 'entry' AND b.ParentBooking_ID IS NULL ORDER BY b.ID", number)
    assert chain == [(10000, 4405, 1190000, 0.19, 190000), (10000, 4340, 800000, None, None)]
    assert _rows(loaded_full, "SELECT Amount FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID", invoice_id) == [(796000,), (1194000,)]
    transfers = _rows(loaded_full, "SELECT b.Amount, b.TaxAmount, c.AccountNumber, g.AccountNumber FROM Bookings b JOIN ChartOfAccounts c ON c.ID = b.COA_ID "
                                   "JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID WHERE b.DocumentNumber = ? AND b.Status = 'invoice_payment' "
                                   "AND b.EntryForm = 'transfer' ORDER BY b.ID", number)
    assert transfers == [(476000, 76000, 4405, 4400), (320000, None, 4340, 4100), (714000, 114000, 4405, 4400), (480000, None, 4340, 4100)]
    assert [(waiting, sum(row[0] for row in transfers if row[2] == waiting), sum(row[1] or 0 for row in transfers if row[2] == waiting))
            for waiting in (4405, 4340)] == [(4405, 1190000, 190000), (4340, 800000, 0)]


# ── abgeschlossene Jahre des Profils full: auch mit den Beispielen ohne Befund ──────────────────────────────
NEXT_TODAY = datetime.date(2027, 2, 12)               # die Rechnung mit steuerfreier Position liegt dann im Dezember 2026, ihre zweite Rate im Januar 2027
CLOSED_YEARS = [2024, 2025, 2026]                     # die Historie des Profils beginnt drei Jahre vor dem Stichtag (``years_back``)


@pytest.fixture(scope="module")
def loaded_next_year(tmp_path_factory):
    """Der Bestand des Profils ``full`` zu einem Stichtag im Februar 2027: Die Jahre 2024 bis 2026 sind abgeschlossen, die Beispielrechnung mit steuerfreier Position
    (und die mit 7 % und 19 %) liegen in 2026."""
    root = tmp_path_factory.mktemp("demo_history_next")
    path = str(root / "loaded_next.db")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(userctx, "DATA_ROOT", str(root / "data"))
        patch.setattr(auth, "AUTH_DB", str(root / "data" / "auth.db"))
        userctx._mode_cache.clear()
        db = Database(path)
        db.load_test_seed_data(include_invoices=False, include_dated=False)
        demo_seed.load_demo_scenario(db, today=NEXT_TODAY, profile=FULL)
    userctx._mode_cache.clear()
    return SimpleNamespace(db=db, path=path, root=root, history=demo_history.build_history(NEXT_TODAY, FULL))


def _copy_of(stock, tmp_path):
    """Eine Kopie als Datenbank: Der DATEV-Export stempelt das Steuerdatum, das gemeinsame Exemplar bleibt unberührt."""
    path = str(tmp_path / "copy.db")
    shutil.copyfile(stock.path, path)
    return Database(path)


def test_the_example_with_the_free_position_lies_in_the_closed_year_and_the_books_are_clean(loaded_next_year):
    (spec,) = _entry_specs(loaded_next_year.history)
    invoice_id, _number = _entry_invoice(loaded_next_year)
    assert spec["date"] == "2026-12-14" == _rows(loaded_next_year, "SELECT InvoiceDate FROM Invoices WHERE ID = ?", invoice_id)[0][0]
    assert [day for day, in _rows(loaded_next_year, "SELECT PaymentDate FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID", invoice_id)] == ["2026-12-28", "2027-01-18"]
    assert loaded_next_year.db.find_payment_inconsistencies() == []


@pytest.mark.parametrize("year", CLOSED_YEARS)
def test_every_closed_year_of_the_full_stock_has_no_finding_and_exports_to_datev_without_a_hint(loaded_next_year, tmp_path, year):
    """Splits, Übertragsprüfung und DATEV-Export (die Prüfliste steht oben): Auch mit den beiden Beispielrechnungen kein Befund und kein Hinweis – der Export liefert
    die Datei ohne Bestätigung."""
    copy = _copy_of(loaded_next_year, tmp_path)
    first, last = f"{year}-01-01", f"{year}-12-31"
    assert copy.find_unbalanced_splits(first, last) == []
    check = copy.check_transfers(first, last)
    assert (check.unbooked, check.elsewhere, check.elsewhere_out, check.doubtful) == ([], [], [], [])
    result, name = handle_datev_export(copy, {"date_from": [first], "date_to": [last]})
    assert isinstance(result, (bytes, bytearray)) and len(result) > 1000 and name.endswith(".csv")


def test_the_stack_of_the_closed_year_carries_the_free_group_without_a_key_and_without_a_rate(loaded_next_year, tmp_path):
    """Im Stapel von 2026: die Forderung und die erste Zahlung der Rechnung mit steuerfreier Position – die Gruppe zu 19 % in der Kette ohne Schlüssel mit dem Satz in
    Spalte 40, die steuerfreie ohne Schlüssel und ohne Satz (es gibt dort keinen Satz). Die zweite Rate liegt im Stapel von 2027."""
    import csv
    import io
    _invoice_id, number = _entry_invoice(loaded_next_year)
    copy = _copy_of(loaded_next_year, tmp_path)

    def stack_rows(year):
        result, _name = confirmed_export(copy, f"{year}-01-01", f"{year}-12-31")        # 2027: drei Bankbewegungen des laufenden Monats sind noch nicht gebucht
        rows = csv.reader(io.StringIO(result.decode("cp1252")), delimiter=";")
        return [(row[6], row[7], row[8], row[40]) for row in rows if len(row) > 40 and row[10] == number]

    assert stack_rows(2026) == [("10000", "4405", "0", "19,00"), ("10000", "4340", "0", ""),
                                ("1800", "10000", "0", ""), ("4405", "4400", "0", "19,00"), ("4340", "4100", "0", "")]
    assert stack_rows(2027) == [("1800", "10000", "0", ""), ("4405", "4400", "0", "19,00"), ("4340", "4100", "0", "")]
