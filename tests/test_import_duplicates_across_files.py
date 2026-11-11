# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Kontoauszug-Import: Duplikate über die Dateien EINES Uploads (2.0.6).

Duplikat = Konto + Buchungsdatum + Betrag, ohne Text; gezählt wird **je Datei**. Der Bestand
enthält, was frühere Dateien desselben Laufs angelegt haben – über mehrere Dateien gilt das
**Maximum**, nicht die Summe. „Alle importieren“ (``handle_confirm_import`` ohne ``file_index``)
muss deshalb dasselbe liefern wie die Einzelimporte nacheinander, und die Vorschau
(``build_import_preview``) rechnet vorab dasselbe, ohne zu schreiben.

Die Fälle a–h (``SCENARIOS``) sind die Tabelle aus dem Umsetzungsplan:

* a  dieselbe Datei zweimal im Upload
* b  CAMT und PDF desselben Auszugs, Texte verschieden
* c  Tagesauszug und Monatsauszug überlappen
* d  zwei gleiche echte Umsätze in einer Datei bleiben beide (d2: alle schon im Bestand)
* e  Auszug erneut, ein Teil schon im Bestand (e2: derselbe Umsatz zweimal in der Datei)
* f  derselbe Umsatz in zwei Dateien **und** im Bestand
* g  Tag (x1, x1) und Monat (x1, x1, x2) mit mehrfachen Umsätzen
* h  Bestand **und** andere Datei in einer Datei (Quelle „db“ und „upload“ nebeneinander)

Dazu: eine Invariante über zufällige Uploads (fester Seed), Reihenfolge der Dateien, zwei Konten,
Gegenbuchung, der Text-Backfill über Dateigrenzen und zwei Angleichungen von Vorschau und Import:

* Ein Umsatz ohne Datum (``None`` oder leer) oder ohne Betrag ist nicht importierbar – die Vorschau
  zählt ihn weder als neu noch als Duplikat und kennzeichnet ihn (``incomplete``), der Import
  überspringt ihn (``skipped_invalid``, nie bei ``skipped``).
* Ein Duplikat aus einer anderen Datei, dessen Verwendungszweck zu keinem Umsatz früherer Dateien
  passt, trägt ``text_differs`` (je Datei ``dup_upload_differs_count``) – nur ein Hinweis, der Import
  bleibt beim Maximum.

Die HTTP- und die Seiten-Tests dazu stehen in ``test_import_upload_http.py``.

IBANs und Namen sind die erlaubten Testwerte aus ``test_confirm_import.py`` (dort auch die
Fixtures ``db_two_accounts`` und ``pending_factory``); alle Beträge sind erfunden.
"""
import json
import random
from collections import Counter
from pathlib import Path

import pytest

from db import Database
from importers import check_statement
from importers.camt import parse_upload
from money import to_minor
from server import handlers
from server.import_preview import build_import_preview, match_account
from tests.test_confirm_import import (IBAN_A, IBAN_B, _txn,  # noqa: F401
                                       db_two_accounts, pending_factory)

FIX = Path(__file__).parent / "fixtures" / "camt"
UNKNOWN_IBAN = "DE00000000000000000000"        # offensichtlich unecht, kein Konto dazu
MODES = ("all", "single")                      # „Alle importieren“ / Einzelimporte nacheinander

X1 = ("2026-03-02", -12.5, "Kaffee")
X1_CAMT = ("2026-03-02", -12.5, "Kaffee EREF+123")      # derselbe Umsatz, wie CAMT ihn schreibt
X2 = ("2026-03-03", 100.0, "Miete Garage")

K1 = ("2026-03-02", -12.5)                     # Schlüssel (Datum, Betrag) ohne Text
K2 = ("2026-03-03", 100.0)

#: Tabelle aus dem Umsetzungsplan, je Fall: Bestand vorher, Dateien (Datum, Betrag, Text),
#: Endstand (Schlüssel -> Anzahl), je Datei angelegt / übersprungen / davon aus anderer Datei
#: dieses Laufs, und die Vorschau je Datei als (neu, Bestand, Upload).
SCENARIOS = {
    "a": dict(bestand=[], dateien=[[X1, X2], [X1, X2]],
              endstand={K1: 1, K2: 1}, angelegt=[2, 0], uebersprungen=[0, 2],
              davon_upload=[0, 2], vorschau=[(2, 0, 0), (0, 0, 2)]),
    "b": dict(bestand=[], dateien=[[X1_CAMT, X2], [X1, X2]],
              endstand={K1: 1, K2: 1}, angelegt=[2, 0], uebersprungen=[0, 2],
              davon_upload=[0, 2], vorschau=[(2, 0, 0), (0, 0, 2)]),
    "c": dict(bestand=[], dateien=[[X1], [X1, X2]],
              endstand={K1: 1, K2: 1}, angelegt=[1, 1], uebersprungen=[0, 1],
              davon_upload=[0, 1], vorschau=[(1, 0, 0), (1, 0, 1)]),
    "d": dict(bestand=[], dateien=[[X1, X1, X2]],
              endstand={K1: 2, K2: 1}, angelegt=[3], uebersprungen=[0],
              davon_upload=[0], vorschau=[(3, 0, 0)]),
    "d2": dict(bestand=[X1, X1, X2], dateien=[[X1, X1, X2]],
               endstand={K1: 2, K2: 1}, angelegt=[0], uebersprungen=[3],
               davon_upload=[0], vorschau=[(0, 3, 0)]),
    "e": dict(bestand=[X1], dateien=[[X1, X2]],
              endstand={K1: 1, K2: 1}, angelegt=[1], uebersprungen=[1],
              davon_upload=[0], vorschau=[(1, 1, 0)]),
    "e2": dict(bestand=[X1], dateien=[[X1, X1, X2]],
               endstand={K1: 2, K2: 1}, angelegt=[2], uebersprungen=[1],
               davon_upload=[0], vorschau=[(2, 1, 0)]),
    "f": dict(bestand=[X1], dateien=[[X1], [X1, X2]],
              endstand={K1: 1, K2: 1}, angelegt=[0, 1], uebersprungen=[1, 1],
              davon_upload=[0, 0], vorschau=[(0, 1, 0), (1, 1, 0)]),
    "g": dict(bestand=[], dateien=[[X1, X1], [X1, X1, X2]],
              endstand={K1: 2, K2: 1}, angelegt=[2, 1], uebersprungen=[0, 2],
              davon_upload=[0, 2], vorschau=[(2, 0, 0), (1, 0, 2)]),
    "h": dict(bestand=[X1], dateien=[[X1, X1], [X1, X1]],
              endstand={K1: 2}, angelegt=[1, 0], uebersprungen=[1, 2],
              davon_upload=[0, 1], vorschau=[(1, 1, 0), (0, 1, 1)]),
}
SCENARIO_NAMES = sorted(SCENARIOS)


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _file(transactions, iban=IBAN_A, name="auszug.xml"):
    """Eine Datei des Uploads; ``transactions`` = Liste aus (Datum, Betrag, Text)."""
    return {"filename": name, "bank_code": "CAMT", "iban": iban, "document_date": "2026-03-31",
            "transactions": [_txn(date, amount, text) for date, amount, text in transactions]}


def _upload(*transaction_lists, ibans=None):
    """Dateien eines Upload in Dateireihenfolge; ``ibans`` ordnet jeder Datei ihr Konto zu."""
    ibans = ibans or [IBAN_A] * len(transaction_lists)
    return [_file(txns, iban, f"auszug{number}.xml")
            for number, (txns, iban) in enumerate(zip(transaction_lists, ibans), 1)]


def _account_id(db, iban):
    return match_account(db.fetch_accounts(), iban)[0]


def _seed(db, transactions, iban=IBAN_A):
    """Bankbewegungen, die schon in der Datenbank stehen (frühere Importe)."""
    account_id = _account_id(db, iban)
    for date, amount, text in transactions:
        db.insert_booking(date_booking=date, amount=amount, account_id=account_id,
                          foreign_bank_account="DE00FOREIGN", recipient_client="Partner",
                          text=text, booking_type="bank")


def _bank(db):
    """Alle Bankbewegungen als Zähler über (Konto-ID, Datum, Betrag in Minor Units)."""
    conn = db._get_connection()
    try:
        rows = conn.execute("SELECT Account_ID, DateBooking, Amount FROM Bookings"
                            " WHERE BookingType = 'bank'").fetchall()
    finally:
        conn.close()
    return Counter(rows)


def _by_key(db, iban=IBAN_A):
    """Wie ``_bank``, aber für ein Konto und mit dem Schlüssel (Datum, Betrag in Euro)."""
    account_id = _account_id(db, iban)
    return Counter({(date, amount / 10000): n for (acc, date, amount), n in _bank(db).items()
                    if acc == account_id})


def _texts(db):
    conn = db._get_connection()
    try:
        return [row[0] for row in conn.execute(
            "SELECT Text FROM Bookings WHERE BookingType = 'bank' ORDER BY ID")]
    finally:
        conn.close()


def _confirm(db, make_pending, files, mode):
    """Bestätigt den Upload und liefert die Ergebnisse je Datei in Dateireihenfolge.

    ``all`` = „Alle importieren“: EIN Aufruf ohne ``file_index``. ``single`` = die Einzelimporte
    nacheinander: ein Aufruf je Datei, in Dateireihenfolge."""
    # wie im Betrieb: die Datei im Pending-Ordner ist JSON (Datumswerte der Parser als Text)
    import_id = make_pending(json.loads(json.dumps(files, default=str)))
    calls = [{}] if mode == "all" else [{"file_index": [str(i)]} for i in range(len(files))]
    results = []
    for extra in calls:
        status, body = handlers.handle_confirm_import(db, {"import_id": [import_id], **extra})
        data = json.loads(body)
        assert status == 200 and data["ok"], body
        results += data["results"]
    return results


def _preview(db, files):
    return build_import_preview(db, {"files": files})["files"]


def _triples(previews):
    """Je Datei (neu, im Bestand, in anderer Datei dieses Uploads)."""
    return [(p["new_count"], p["dup_count"] - p["dup_upload_count"], p["dup_upload_count"])
            for p in previews]


def _run_case(db, make_pending, name, mode):
    """Spielt einen Fall der Tabelle durch und prüft alle Zahlen; liefert die Ergebnisse."""
    case = SCENARIOS[name]
    _seed(db, case["bestand"])
    files = _upload(*case["dateien"])
    results = _confirm(db, make_pending, files, mode)

    assert _by_key(db) == Counter(case["endstand"]), f"Fall {name}, {mode}"
    assert [r["inserted"] for r in results] == case["angelegt"], f"Fall {name}, {mode}"
    assert [r["skipped"] for r in results] == case["uebersprungen"], f"Fall {name}, {mode}"
    # „davon aus anderer Datei dieses Uploads“ kennt nur „Alle importieren“: beim Einzelimport
    # steckt, was frühere Dateien angelegt haben, schon im Bestand vor dem Aufruf.
    expected_upload = case["davon_upload"] if mode == "all" else [0] * len(files)
    assert [r["skipped_upload"] for r in results] == expected_upload, f"Fall {name}, {mode}"
    assert [r["skipped_invalid"] for r in results] == [0] * len(files), f"Fall {name}, {mode}"
    return files, results


# ── Die Fälle a–g: „alle importieren“ und „einzeln nacheinander“ ────────────

@pytest.mark.parametrize("mode", MODES)
def test_same_file_twice_in_one_upload(db_two_accounts, pending_factory, mode):
    """(a) Dieselbe Datei zweimal im Upload: 2 Buchungen, nicht 4."""
    _run_case(db_two_accounts, pending_factory, "a", mode)


@pytest.mark.parametrize("mode", MODES)
def test_camt_and_pdf_of_same_statement_differing_texts(db_two_accounts, pending_factory, mode):
    """(b) CAMT und PDF desselben Auszugs schreiben den Text verschieden – der Text gehört nicht
    zum Schlüssel; der Text der ersten Datei bleibt, nichts wird verkürzt oder angehängt."""
    _files, results = _run_case(db_two_accounts, pending_factory, "b", mode)
    assert _texts(db_two_accounts) == ["Kaffee EREF+123", "Miete Garage"]
    assert [r["text_updated"] for r in results] == [0, 0]


@pytest.mark.parametrize("mode", MODES)
def test_daily_and_monthly_overlap(db_two_accounts, pending_factory, mode):
    """(c) Tagesauszug (x1) und Monatsauszug (x1, x2): der Überlapp wird nicht doppelt angelegt."""
    _run_case(db_two_accounts, pending_factory, "c", mode)


@pytest.mark.parametrize("name", ["d", "d2"])
@pytest.mark.parametrize("mode", MODES)
def test_identical_transactions_in_one_file_are_kept(db_two_accounts, pending_factory, name, mode):
    """(d) Zwei gleiche echte Umsätze in EINER Datei bleiben beide; (d2) sind alle schon im
    Bestand, wird keiner doppelt angelegt. Ein erneuter Import legt nichts mehr an."""
    _run_case(db_two_accounts, pending_factory, name, mode)
    again = _confirm(db_two_accounts, pending_factory, _upload(*SCENARIOS[name]["dateien"]), mode)
    assert [r["inserted"] for r in again] == [0]
    assert [r["skipped"] for r in again] == [3]
    assert _by_key(db_two_accounts) == Counter(SCENARIOS[name]["endstand"])


@pytest.mark.parametrize("name", ["e", "e2"])
@pytest.mark.parametrize("mode", MODES)
def test_partial_reimport(db_two_accounts, pending_factory, name, mode):
    """(e) Auszug erneut, ein Umsatz schon im Bestand; (e2) derselbe Umsatz zweimal in der Datei:
    angelegt wird nur, was der Bestand noch nicht hat."""
    _run_case(db_two_accounts, pending_factory, name, mode)


@pytest.mark.parametrize("mode", MODES)
def test_overlap_and_already_in_db(db_two_accounts, pending_factory, mode):
    """(f) Derselbe Umsatz steht in zwei Dateien UND im Bestand: er kommt nicht noch einmal hinzu."""
    _run_case(db_two_accounts, pending_factory, "f", mode)


@pytest.mark.parametrize("mode", MODES)
def test_max_not_sum_across_files(db_two_accounts, pending_factory, mode):
    """(g) Tag (x1, x1) und Monat (x1, x1, x2): über die Dateien gilt das Maximum – x1 zweimal,
    nicht viermal."""
    _run_case(db_two_accounts, pending_factory, "g", mode)


@pytest.mark.parametrize("mode", MODES)
def test_bestand_and_other_file_side_by_side(db_two_accounts, pending_factory, mode):
    """(h) Eine Datei, deren Duplikate teils im Bestand, teils in der anderen Datei stecken."""
    _run_case(db_two_accounts, pending_factory, "h", mode)


@pytest.mark.parametrize("before,first,second", [
    (0, 2, 3), (0, 3, 2), (1, 2, 2), (2, 1, 1), (0, 0, 2), (3, 2, 1), (2, 2, 2), (0, 1, 1),
])
@pytest.mark.parametrize("mode", MODES)
def test_final_count_is_the_maximum_not_the_sum(db_two_accounts, pending_factory,
                                                before, first, second, mode):
    """Je Schlüssel gilt: Bestand nachher = größter Wert aus Bestand und den Dateien."""
    _seed(db_two_accounts, [X1] * before)
    files = _upload([X1] * first, [X1] * second)
    _confirm(db_two_accounts, pending_factory, files, mode)
    assert _by_key(db_two_accounts)[K1] == max(before, first, second)


def _wipe(db):
    conn = db._get_connection()
    try:
        conn.execute("DELETE FROM Bookings")
        conn.commit()
    finally:
        conn.close()


# ── skipped_upload im Ergebnis ───────────────────────────────────────────────

@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_skipped_upload_reported(db_two_accounts, pending_factory, name):
    """Jedes Ergebnis nennt ``skipped_upload``; bei „Alle importieren“ sind es die Umsätze, die
    eine andere Datei desselben Laufs angelegt hat, und nie mehr als ``skipped``."""
    _files, results = _run_case(db_two_accounts, pending_factory, name, "all")
    assert all("skipped_upload" in r for r in results)
    assert all(0 <= r["skipped_upload"] <= r["skipped"] for r in results)
    assert [r["skipped_upload"] for r in results] == SCENARIOS[name]["davon_upload"]


def test_single_import_reports_no_upload_skips(db_two_accounts, pending_factory):
    """Der Einzelimport kennt die anderen Dateien nicht: Was sie angelegt haben, steckt vor dem
    Aufruf schon im Bestand – ``skipped`` zählt es, ``skipped_upload`` nicht."""
    files = _upload([X1, X2], [X1, X2])
    results = _confirm(db_two_accounts, pending_factory, files, "single")
    assert [(r["inserted"], r["skipped"], r["skipped_upload"]) for r in results] == [
        (2, 0, 0), (0, 2, 0)]


def test_results_of_unusable_files_have_no_upload_count(db_two_accounts, pending_factory):
    """Datei ohne Konto und bereits importierte Datei: kein Zähler, und der Rest des Laufs läuft."""
    files = _upload([X1], [X1], ibans=[UNKNOWN_IBAN, IBAN_A])
    import_id = pending_factory(files)
    status, body = handlers.handle_confirm_import(
        db_two_accounts, {"import_id": [import_id]})
    results = json.loads(body)["results"]
    assert results[0]["account_found"] is False and results[0].get("skipped_upload", 0) == 0
    assert (results[1]["inserted"], results[1]["skipped"], results[1]["skipped_upload"]) == (1, 0, 0)

    status, body = handlers.handle_confirm_import(
        db_two_accounts, {"import_id": [import_id], "file_index": ["1"]})
    again = json.loads(body)["results"][0]
    assert again["error"] == "bereits importiert" and again.get("skipped_upload", 0) == 0
    assert sum(_bank(db_two_accounts).values()) == 1


# ── Datei ohne Konto ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", MODES)
def test_file_without_account_does_not_shadow_others(db_two_accounts, pending_factory, mode):
    """Eine Datei ohne Konto wird nicht importiert und nimmt den anderen nichts weg: auch die
    spätere Datei findet den Umsatz der früheren, nicht den der übersprungenen."""
    files = _upload([X1], [X1, X2], [X1, X2], ibans=[IBAN_A, UNKNOWN_IBAN, IBAN_A])
    results = _confirm(db_two_accounts, pending_factory, files, mode)

    assert [r["account_found"] for r in results] == [True, False, True]
    assert [r.get("inserted") for r in results] == [1, 0, 1]
    assert [r.get("skipped") for r in results] == [0, 0, 1]
    assert _by_key(db_two_accounts) == Counter({K1: 1, K2: 1})


def test_account_less_file_not_planned(db_two_accounts):
    """Vorschau: Eine Datei ohne Konto wird nicht importiert, ihre Umsätze zählen deshalb nicht zu
    dem, was spätere Dateien schon finden – und sie ist selbst nie „Duplikat“."""
    files = _upload([X1], [X1, X2], [X1, X2], ibans=[UNKNOWN_IBAN, IBAN_A, UNKNOWN_IBAN])
    first, second, third = _preview(db_two_accounts, files)
    assert (first["status"], third["status"]) == ("error", "error")
    assert (second["new_count"], second["dup_count"], second["dup_upload_count"]) == (2, 0, 0)
    # auch zwei Dateien ohne Konto behindern einander nicht
    assert (third["new_count"], third["dup_count"], third["dup_upload_count"]) == (2, 0, 0)
    assert (first["new_count"], first["dup_count"], first["dup_upload_count"]) == (1, 0, 0)
    assert all(t["dup_source"] is None for p in (first, third) for t in p["transactions"])


def test_account_less_files_between_do_not_change_the_file_numbers(db_two_accounts):
    """Dieselbe Vorschau mit und ohne eingestreute Datei ohne Konto: für die Dateien mit Konto
    ändert sich nichts."""
    plain = _preview(db_two_accounts, _upload([X1], [X1, X2]))
    mixed = _preview(db_two_accounts, _upload([X1], [X1, X2], [X1, X2],
                                              ibans=[IBAN_A, IBAN_A, UNKNOWN_IBAN]))
    assert _triples(plain) == _triples(mixed[:2]) == [(1, 0, 0), (1, 0, 1)]


# ── Vorschau: Kennzeichnung und Zahlen ───────────────────────────────────────

def test_second_file_marked_as_upload_duplicate(db_two_accounts):
    """Der Umsatz der zweiten Datei, den schon die erste anlegt, trägt ``dup_source`` = "upload"."""
    first, second = _preview(db_two_accounts, _upload([X1], [X1, X2]))
    assert [t["dup_source"] for t in first["transactions"]] == [None]
    assert [t["dup_source"] for t in second["transactions"]] == ["upload", None]
    assert [t["dup"] for t in second["transactions"]] == [True, False]
    assert (second["new_count"], second["dup_count"], second["dup_upload_count"]) == (1, 1, 1)
    assert all(isinstance(t["text_differs"], bool) for p in (first, second)       # in jeder Zeile
               for t in p["transactions"])
    assert (first["dup_count"], first["dup_upload_count"], first["status"]) == (0, 0, "ok")
    assert second["status"] == "warn"
    assert [p["dup_source"] for p in second["problems"]] == ["upload"]


def test_preview_tells_database_and_upload_duplicates_apart(db_two_accounts):
    """Bestand und andere Datei in einer Datei: "db" geht vor "upload"."""
    _seed(db_two_accounts, [X1])
    first, second = _preview(db_two_accounts, _upload([X1, X1], [X1, X1]))
    assert [t["dup_source"] for t in first["transactions"]] == ["db", None]
    assert [t["dup_source"] for t in second["transactions"]] == ["db", "upload"]
    assert (second["dup_count"], second["dup_upload_count"], second["new_count"]) == (2, 1, 0)


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_preview_counts_new_database_and_upload_per_file(db_two_accounts, name):
    """Die Zahlen je Datei (neu / Bestand / Upload) aus der Tabelle des Plans."""
    case = SCENARIOS[name]
    _seed(db_two_accounts, case["bestand"])
    previews = _preview(db_two_accounts, _upload(*case["dateien"]))
    assert _triples(previews) == case["vorschau"]
    assert [p["total"] for p in previews] == [len(f) for f in case["dateien"]]


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_preview_new_count_matches_confirm(db_two_accounts, pending_factory, name):
    """Was die Vorschau „neu“ nennt, legt „Alle importieren“ an – in Summe und je Datei; die
    Duplikate stimmen mit ``skipped`` und ``skipped_upload`` überein."""
    case = SCENARIOS[name]
    _seed(db_two_accounts, case["bestand"])
    files = _upload(*case["dateien"])
    before = sum(_bank(db_two_accounts).values())
    previews = _preview(db_two_accounts, files)
    results = _confirm(db_two_accounts, pending_factory, files, "all")

    assert sum(p["new_count"] for p in previews) == sum(r["inserted"] for r in results) \
        == sum(_bank(db_two_accounts).values()) - before
    assert [p["new_count"] for p in previews] == [r["inserted"] for r in results]
    assert [p["dup_count"] for p in previews] == [r["skipped"] for r in results]
    assert [p["dup_upload_count"] for p in previews] == [r["skipped_upload"] for r in results]


def test_preview_changes_nothing(db_two_accounts):
    files = _upload([X1, X2], [X1, X2])
    _preview(db_two_accounts, files)
    assert _bank(db_two_accounts) == Counter()


# ── Vorschau: „Text weicht ab, bitte prüfen“ ─────────────────────────────────

#: (Verwendungszweck in der früheren Datei, in der späteren Datei, weicht ab?). Verglichen wird ohne
#: Rücksicht auf Leerraum und Schreibweise; passt einer als Anfang zum anderen (PDF und CAMT kürzen
#: verschieden) oder ist einer leer, gilt der Umsatz als derselbe.
TEXT_PAIRS = [
    ("Netflix", "Spotify", True),
    ("Rechnung 100", "Rechnung 200", True),
    ("Netflix", "flix", True),                       # ein Stück aus der Mitte ist kein Anfang
    ("Netflix", "Netflix", False),
    ("Kaffee EREF+123", "Kaffee", False),            # CAMT, danach PDF
    ("Kaffee", "Kaffee EREF+123", False),            # PDF, danach CAMT
    ("Kaffee   Bar", "kaffee bar", False),           # Leerraum und Schreibweise
    ("Netflix", "Net", False),
    ("", "Netflix", False),                          # leere Texte gelten als übereinstimmend
    ("Netflix", "", False),
]


@pytest.mark.parametrize("earlier,later,differs", TEXT_PAIRS)
def test_text_differs_marks_a_duplicate_with_an_unrelated_text(db_two_accounts, earlier, later,
                                                                differs):
    """Gleicher Umsatz in zwei Dateien eines Uploads, aber ein Verwendungszweck, der zu dem der früheren
    Datei nicht passt (Auszug 5 „Netflix“, Auszug 6 „Spotify“): der Umsatz der späteren Datei trägt
    ``text_differs`` und wird gezählt; passen die Texte zusammen, bleibt er unmarkiert."""
    files = _upload([("2026-03-15", -9.99, earlier)], [("2026-03-15", -9.99, later)])
    first, second = _preview(db_two_accounts, files)

    assert [t["text_differs"] for t in first["transactions"]] == [False]
    assert first["dup_upload_differs_count"] == 0
    (row,) = second["transactions"]
    assert (row["dup"], row["dup_source"], row["text_differs"]) == (True, "upload", differs)
    assert (second["dup_count"], second["dup_upload_count"],
            second["dup_upload_differs_count"]) == (1, 1, 1 if differs else 0)
    assert second["problems"][0]["text_differs"] is differs


@pytest.mark.parametrize("mode", MODES)
def test_text_differs_does_not_change_the_import(db_two_accounts, pending_factory, mode):
    """Der Hinweis ist nur ein Hinweis: der Import entscheidet weiter nach Konto, Datum und Betrag –
    das Maximum über die Dateien, der Text der ersten Datei bleibt."""
    files = _upload([("2026-03-15", -9.99, "Netflix")], [("2026-03-15", -9.99, "Spotify")])
    assert _preview(db_two_accounts, files)[1]["dup_upload_differs_count"] == 1

    results = _confirm(db_two_accounts, pending_factory, files, mode)
    assert [(r["inserted"], r["skipped"], r["text_updated"]) for r in results] == [
        (1, 0, 0), (0, 1, 0)]
    assert _texts(db_two_accounts) == ["Netflix"]
    assert _by_key(db_two_accounts) == Counter({("2026-03-15", -9.99): 1})


def test_text_differs_only_marks_duplicates_from_other_files(db_two_accounts):
    """Ein Duplikat aus dem Bestand und ein neuer Umsatz tragen die Markierung nie, auch bei
    fremdem Text."""
    _seed(db_two_accounts, [("2026-03-15", -9.99, "Netflix")])
    files = _upload([("2026-03-15", -9.99, "Spotify"), ("2026-03-16", -5.0, "Spotify")])
    (preview,) = _preview(db_two_accounts, files)
    assert [(t["dup_source"], t["text_differs"]) for t in preview["transactions"]] == [
        ("db", False), (None, False)]
    assert preview["dup_upload_differs_count"] == 0


def test_text_differs_compares_with_every_earlier_text(db_two_accounts):
    """Verglichen wird mit den Texten ALLER früheren Dateien: wer zu irgendeinem passt, ist
    unmarkiert."""
    day = "2026-03-15"
    files = _upload([(day, -9.99, "Netflix")], [(day, -9.99, "Spotify")],
                    [(day, -9.99, "Amazon")], [(day, -9.99, "spotify")], [(day, -9.99, "NETFLIX abo")])
    previews = _preview(db_two_accounts, files)
    assert [p["transactions"][0]["text_differs"] for p in previews] == [
        False, True, True, False, False]
    assert [p["dup_upload_differs_count"] for p in previews] == [0, 1, 1, 0, 0]


def test_text_differs_is_counted_per_file(db_two_accounts):
    files = _upload([("2026-03-02", -12.5, "Netflix"), ("2026-03-03", 100.0, "Miete")],
                    [("2026-03-02", -12.5, "Spotify"), ("2026-03-03", 100.0, "Miete Garage"),
                     ("2026-03-04", 1.0, "Neu")])
    first, second = _preview(db_two_accounts, files)
    assert (second["new_count"], second["dup_count"], second["dup_upload_count"],
            second["dup_upload_differs_count"]) == (1, 2, 2, 1)
    assert [t["text_differs"] for t in second["transactions"]] == [True, False, False]
    assert first["dup_upload_differs_count"] == 0


def test_text_differs_ignores_the_other_account(db_two_accounts):
    """Derselbe Umsatz auf einem anderen Konto ist kein Duplikat und damit auch nie markiert."""
    files = _upload([("2026-03-15", -9.99, "Netflix")], [("2026-03-15", -9.99, "Spotify")],
                    ibans=[IBAN_A, IBAN_B])
    previews = _preview(db_two_accounts, files)
    assert [t["text_differs"] for p in previews for t in p["transactions"]] == [False, False]
    assert [p["dup_upload_count"] for p in previews] == [0, 0]


# ── Reihenfolge, zwei Konten, Gegenbuchung ───────────────────────────────────

@pytest.mark.parametrize("mode", MODES)
def test_order_of_files_does_not_change_the_result(db_two_accounts, pending_factory, mode):
    """Tag vor Monat oder Monat vor Tag: derselbe Endstand. Welche Datei „neu“ zählt, ändert sich –
    in der Vorschau ebenso wie beim Import."""
    day_first = _upload([X1], [X1, X2])
    month_first = _upload([X1, X2], [X1])
    assert _triples(_preview(db_two_accounts, day_first)) == [(1, 0, 0), (1, 0, 1)]
    assert _triples(_preview(db_two_accounts, month_first)) == [(2, 0, 0), (0, 0, 1)]

    results = _confirm(db_two_accounts, pending_factory, day_first, mode)
    assert [(r["inserted"], r["skipped"]) for r in results] == [(1, 0), (1, 1)]
    after_day_first = _by_key(db_two_accounts)
    _wipe(db_two_accounts)

    results = _confirm(db_two_accounts, pending_factory, month_first, mode)
    assert [(r["inserted"], r["skipped"]) for r in results] == [(2, 0), (0, 1)]
    assert _by_key(db_two_accounts) == after_day_first == Counter({K1: 1, K2: 1})


@pytest.mark.parametrize("mode", MODES)
def test_same_key_on_two_accounts_stays_separate(db_two_accounts, pending_factory, mode):
    """Derselbe Umsatz auf zwei Konten ist kein Duplikat – weder in der Vorschau noch beim Import;
    innerhalb eines Kontos zählt weiter das Maximum."""
    files = _upload([X1], [X1], [X1, X1], [X1], ibans=[IBAN_A, IBAN_B, IBAN_A, IBAN_B])
    previews = _preview(db_two_accounts, files)
    assert _triples(previews) == [(1, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)]

    results = _confirm(db_two_accounts, pending_factory, files, mode)
    assert [r["inserted"] for r in results] == [1, 1, 1, 0]
    assert _by_key(db_two_accounts, IBAN_A) == Counter({K1: 2})
    assert _by_key(db_two_accounts, IBAN_B) == Counter({K1: 1})


def test_bestand_of_another_account_is_not_a_duplicate(db_two_accounts, pending_factory):
    _seed(db_two_accounts, [X1], iban=IBAN_B)
    files = _upload([X1])
    assert _triples(_preview(db_two_accounts, files)) == [(1, 0, 0)]
    results = _confirm(db_two_accounts, pending_factory, files, "all")
    assert results[0]["inserted"] == 1
    assert _by_key(db_two_accounts, IBAN_A) == Counter({K1: 1})


@pytest.mark.parametrize("mode", MODES)
def test_reversal_is_not_a_duplicate(db_two_accounts, pending_factory, mode):
    """Storno / Gegenbuchung: gleiches Datum, gleicher Betrag mit anderem Vorzeichen ist ein
    eigener Umsatz – auch in einer anderen Datei desselben Uploads."""
    charge = ("2026-03-02", -12.5, "Lastschrift Kaffee")
    refund = ("2026-03-02", 12.5, "Storno: Lastschrift Kaffee")
    files = _upload([charge], [refund], [charge, refund])
    assert _triples(_preview(db_two_accounts, files)) == [(1, 0, 0), (1, 0, 0), (0, 0, 2)]

    results = _confirm(db_two_accounts, pending_factory, files, mode)
    assert [r["inserted"] for r in results] == [1, 1, 0]
    assert _by_key(db_two_accounts) == Counter({("2026-03-02", -12.5): 1, ("2026-03-02", 12.5): 1})


# ── Umsatz ohne Datum oder Betrag: nicht importierbar – Vorschau und Import sind sich einig ──

INVALID_KINDS = ("ohne-datum", "leeres-datum", "ohne-betrag")


def _invalid(kind):
    """Ein Umsatz, den der Import nicht anlegen kann: ohne Datum (``None`` oder leer) oder ohne Betrag."""
    values = {"ohne-datum": dict(date=None, amount=5.0), "leeres-datum": dict(date="", amount=5.0),
              "ohne-betrag": dict(date="2026-03-04", amount=None)}[kind]
    return {**values, "reference": f"unvollständig {kind}", "recipient": "P", "foreign_iban": ""}


@pytest.mark.parametrize("mode", MODES)
def test_incomplete_transactions_are_neither_new_nor_duplicates(db_two_accounts, pending_factory, mode):
    """Ein Umsatz ohne Datum oder Betrag ist für Vorschau und Import dasselbe: nicht importierbar.
    Die Vorschau zählt ihn nicht als „neu“ und kennzeichnet ihn (``incomplete``); der Import
    überspringt ihn und zählt ihn bei ``skipped_invalid`` – nie bei ``skipped`` (Duplikate) und nie
    bei ``skipped_upload``. Die Zahlen der übrigen Umsätze bleiben davon unberührt."""
    files = _upload([X1], [X1, X2])
    for one_file in files:
        one_file["transactions"] += [_invalid(kind) for kind in INVALID_KINDS]

    first, second = _preview(db_two_accounts, files)
    assert (first["total"], first["new_count"], first["dup_count"]) == (4, 1, 0)
    assert (second["total"], second["new_count"], second["dup_count"],
            second["dup_upload_count"]) == (5, 1, 1, 1)
    for preview in (first, second):
        marked = [t for t in preview["transactions"] if "incomplete" in t["warn"]]
        assert [t["reference"] for t in marked] == [f"unvollständig {k}" for k in INVALID_KINDS]
        assert all(not t["dup"] and t["dup_source"] is None and t["text_differs"] is False
                   for t in marked)
        assert all(t["warn"][-1] == "incomplete" for t in marked)       # der Befund steht zuletzt
        assert all(t in preview["problems"] for t in marked)        # in der Karte aufgeführt
        assert preview["status"] == "warn"
    assert [t["dup_source"] for t in second["transactions"]] == ["upload", None, None, None, None]

    results = _confirm(db_two_accounts, pending_factory, files, mode)
    assert [(r["inserted"], r["skipped"], r["skipped_invalid"]) for r in results] == [
        (1, 0, 3), (1, 1, 3)]
    assert [r["skipped_upload"] for r in results] == ([0, 1] if mode == "all" else [0, 0])
    assert _by_key(db_two_accounts) == Counter({K1: 1, K2: 1})
    assert all(date for (_account, date, _amount) in _bank(db_two_accounts))   # keine Buchung ohne Datum


@pytest.mark.parametrize("kind", INVALID_KINDS)
@pytest.mark.parametrize("mode", MODES)
def test_two_files_with_an_incomplete_transaction_each_import_nothing(db_two_accounts, pending_factory,
                                                                      kind, mode):
    """Zwei Dateien mit je einem solchen Umsatz: Vorschau 0 neu, importiert 0 – und auch nicht als
    Duplikat der anderen Datei gezählt."""
    files = _upload([], [])
    for one_file in files:
        one_file["transactions"] = [_invalid(kind)]

    previews = _preview(db_two_accounts, files)
    assert [(p["total"], p["new_count"], p["dup_count"], p["dup_upload_count"])
            for p in previews] == [(1, 0, 0, 0)] * 2
    assert all("incomplete" in p["transactions"][0]["warn"] for p in previews)

    results = _confirm(db_two_accounts, pending_factory, files, mode)
    assert [(r["inserted"], r["skipped"], r["skipped_upload"], r["skipped_invalid"])
            for r in results] == [(0, 0, 0, 1)] * 2
    assert _bank(db_two_accounts) == Counter()


def test_incomplete_transaction_in_a_file_without_account_is_flagged_too(db_two_accounts):
    files = _upload([X1], ibans=[UNKNOWN_IBAN])
    files[0]["transactions"].append(_invalid("ohne-datum"))
    (preview,) = _preview(db_two_accounts, files)
    assert preview["status"] == "error"
    assert (preview["new_count"], preview["dup_count"]) == (1, 0)           # nur der vollständige
    assert "incomplete" in preview["transactions"][1]["warn"]


@pytest.mark.parametrize("mode", MODES)
def test_known_limits_are_pinned(db_two_accounts, pending_factory, mode):
    """Grenzen ohne Bankreferenz im Datenmodell (CLAUDE.md, README): Eine CAMT-Sammelbuchung wird in
    Einzelumsätze zerlegt, die PDF zeigt eine Summenzeile – die Beträge passen nicht zusammen, beides
    wird angelegt. Und zwei verschiedene Auszüge desselben Tags mit je einem echten, gleichen Umsatz
    behalten nur einen davon (Maximum statt Summe). Fällt einer dieser Fälle anders aus, hat sich die
    Duplikat-Erkennung geändert – dann gehören Doku und Aufgabenliste mit angepasst."""
    day = "2026-03-15"
    camt_split = [(day, -30.0, "Sammel Position 1"), (day, -20.5, "Sammel Position 2")]
    pdf_sum = [(day, -50.5, "Sammelbuchung")]
    twin_a = [("2026-03-16", -9.99, "Netflix")]
    twin_b = [("2026-03-16", -9.99, "Netflix")]       # ein zweites, echtes Abo am selben Tag

    files = _upload(camt_split, pdf_sum, twin_a, twin_b)
    previews = _preview(db_two_accounts, files)
    assert _triples(previews) == [(2, 0, 0), (1, 0, 0), (1, 0, 0), (0, 0, 1)]

    results = _confirm(db_two_accounts, pending_factory, files, mode)
    assert [r["inserted"] for r in results] == [2, 1, 1, 0]
    assert _by_key(db_two_accounts) == Counter({(day, -30.0): 1, (day, -20.5): 1, (day, -50.5): 1,
                                                ("2026-03-16", -9.99): 1})


# ── Text-Backfill über Dateigrenzen ──────────────────────────────────────────

@pytest.mark.parametrize("mode", MODES)
def test_text_backfill_across_files_extends_the_short_text(db_two_accounts, pending_factory, mode):
    """PDF-artiger kurzer Text zuerst, CAMT-Text danach: der Text wird verlängert, keine zweite
    Buchung. Das Duplikat stammt aus der anderen Datei desselben Laufs."""
    short = ("2026-03-02", -12.5, "Kaffee")
    long = ("2026-03-02", -12.5, "Kaffee Beleg 4711 Teil 2")
    results = _confirm(db_two_accounts, pending_factory, _upload([short], [long]), mode)

    assert [(r["inserted"], r["skipped"], r["text_updated"]) for r in results] == [
        (1, 0, 0), (0, 1, 1)]
    assert [r["skipped_upload"] for r in results] == ([0, 1] if mode == "all" else [0, 0])
    assert _texts(db_two_accounts) == ["Kaffee Beleg 4711 Teil 2"]


@pytest.mark.parametrize("mode", MODES)
def test_text_backfill_never_shortens_or_replaces(db_two_accounts, pending_factory, mode):
    """Kommt der längere Text zuerst, bleibt er; ein fremder Text ersetzt ihn nicht."""
    long = ("2026-03-02", -12.5, "Kaffee Beleg 4711 Teil 2")
    short = ("2026-03-02", -12.5, "Kaffee")
    other = ("2026-03-02", -12.5, "Tee")
    results = _confirm(db_two_accounts, pending_factory, _upload([long], [short], [other]), mode)

    assert [r["text_updated"] for r in results] == [0, 0, 0]
    assert [r["inserted"] for r in results] == [1, 0, 0]
    assert _texts(db_two_accounts) == ["Kaffee Beleg 4711 Teil 2"]


# ── CAMT-Dateien ─────────────────────────────────────────────────────────────

def _camt_files(*names):
    """Die Dateien eines Uploads, wie ``upload_handler`` sie aus CAMT-Dateien aufbaut."""
    files = []
    for number, name in enumerate(names, 1):
        for label, statement in parse_upload(f"{number}_{name}", (FIX / name).read_bytes()):
            data = check_statement(statement).as_dict()
            data["filename"] = label
            files.append(data)
    return files


@pytest.mark.parametrize("mode", MODES)
def test_same_camt_twice_in_upload_imports_once(db_two_accounts, pending_factory, mode):
    """Dieselbe CAMT-Datei zweimal im Upload: die Vorschau kennzeichnet die zweite, der Import legt
    jeden Umsatz einmal an."""
    files = _camt_files("camt053_v02.xml", "camt053_v02.xml")
    first, second = _preview(db_two_accounts, files)
    assert (first["total"], first["new_count"], first["dup_count"]) == (2, 2, 0)
    assert (second["total"], second["new_count"], second["dup_count"],
            second["dup_upload_count"]) == (2, 0, 2, 2)
    assert [t["dup_source"] for t in second["transactions"]] == ["upload", "upload"]
    assert "skipped_status" in second["warnings"]            # Hinweise der Datei bleiben erhalten

    results = _confirm(db_two_accounts, pending_factory, files, mode)
    assert [(r["inserted"], r["skipped"]) for r in results] == [(2, 0), (0, 2)]
    assert [r["skipped_upload"] for r in results] == ([0, 2] if mode == "all" else [0, 0])
    assert _by_key(db_two_accounts) == Counter({("2026-03-05", 119.0): 1,
                                                ("2026-03-10", -23.45): 1})


# ── Invariante über zufällige Uploads ────────────────────────────────────────

#: kleiner Vorrat gleicher und verschiedener Schlüssel – dazu die Gegenbuchung (gleiches Datum,
#: anderes Vorzeichen) und derselbe Betrag an einem anderen Tag
POOL = [("2026-03-02", -12.5), ("2026-03-02", 12.5), ("2026-03-03", -12.5), ("2026-03-03", 100.0)]
#: Verwendungszwecke: „Kaffee“ und „Kaffee EREF+123“ gehören zusammen (Anfang), die übrigen nicht
TEXTS = ["Kaffee", "Kaffee EREF+123", "Miete Garage", "Gutschrift Kaffee", "", "KAFFEE  bar"]
#: gelegentlich ein Umsatz, den der Import nicht anlegen kann
INCOMPLETE = [(None, 5.0), ("", 5.0), ("2026-03-04", None)]


def _random_upload(rng):
    """1–4 Dateien mit je 0–6 Umsätzen auf zwei Konten (und gelegentlich ohne Konto) und ein
    Bestand, der einen Teil der Schlüssel schon enthält."""
    uploads = []
    for _ in range(rng.randint(1, 4)):
        iban = rng.choice([IBAN_A, IBAN_A, IBAN_B, IBAN_B, UNKNOWN_IBAN])
        txns = []
        for _ in range(rng.randint(0, 6)):
            key = rng.choice(INCOMPLETE) if rng.random() < 0.08 else rng.choice(POOL)
            txns.append((*key, rng.choice(TEXTS)))
        uploads.append((iban, txns))
    existing = {(iban, *key): rng.choice([1, 2])
                for iban in (IBAN_A, IBAN_B) for key in POOL if rng.random() < 0.3}
    return uploads, existing


def _same_umsatz(a, b):
    """Verwendungszwecke, die zu demselben Umsatz gehören können: gleich oder einer der Anfang des
    anderen (Leerraum und Schreibweise egal; leer passt zu allem)."""
    a, b = " ".join(a.split()).casefold(), " ".join(b.split()).casefold()
    return a.startswith(b) or b.startswith(a)


def _model(existing, uploads):
    """Die Regel, unabhängig vom Code formuliert: je Datei zählen, der Bestand enthält, was frühere
    Dateien angelegt haben. Liefert je Datei ein Dict mit ``inserted``, ``skipped`` (Duplikate),
    ``from_db``, ``from_upload``, ``differs`` (Duplikate aus früherer Datei, deren Text zu keinem
    früheren passt) und ``invalid`` (ohne Datum oder Betrag) – ``None`` für eine Datei ohne Konto –
    und den Endstand je (IBAN, Datum, Betrag)."""
    start = Counter(existing)
    stock = Counter(existing)
    earlier_texts = {}
    per_file = []
    for iban, txns in uploads:
        if iban == UNKNOWN_IBAN:
            per_file.append(None)
            continue
        valid = [(d, a, text) for d, a, text in txns if d and a is not None]
        before = Counter(stock)
        inserted = from_db = from_upload = differs = 0
        counts = Counter((iban, d, a) for d, a, _text in valid)
        for key, n in counts.items():
            new = max(0, n - stock[key])
            from_db += min(n, start[key])
            from_upload += (n - new) - min(n, start[key])
            inserted += new
            stock[key] += new
        position = Counter()
        for d, a, text in valid:
            key = (iban, d, a)
            index, position[key] = position[key], position[key] + 1
            if start[key] <= index < before[key] and not any(
                    _same_umsatz(text, other) for other in earlier_texts.get(key, [])):
                differs += 1
        for d, a, text in valid:
            earlier_texts.setdefault((iban, d, a), []).append(text)
        per_file.append(dict(inserted=inserted, skipped=len(valid) - inserted, from_db=from_db,
                             from_upload=from_upload, differs=differs,
                             invalid=len(txns) - len(valid)))
    return per_file, stock


def _files_of(uploads):
    return [_file(txns, iban, f"auszug{number}.xml")
            for number, (iban, txns) in enumerate(uploads, 1)]


def test_random_uploads_keep_the_invariants(db_two_accounts, pending_factory, tmp_path):
    """200 zufällige Uploads (fester Seed): Für jeden gilt gleichzeitig

    1. „Alle importieren“ und die Einzelimporte nacheinander legen dieselben Buchungen an,
    2. die Summe ``new_count`` der Vorschau ist die Zahl der angelegten Buchungen – und je Datei
       stimmen Vorschau und Import in neu, Duplikate, „aus anderer Datei“ und „ohne Datum oder
       Betrag“ überein,
    3. je Schlüssel gilt: Bestand nachher = max(Bestand vorher, größte Häufigkeit in einer Datei),
    4. ein zweiter identischer Lauf legt nichts an,
    5. eine andere Reihenfolge der Dateien ändert den Endstand nicht,
    6. die Vorschau markiert genau die Duplikate aus früheren Dateien, deren Text zu keinem früheren
       Umsatz passt – am Import ändert das nichts.
    """
    def second_db(name):
        db = Database(db_name=str(tmp_path / name))
        db.insert_account("Konto A", "Owner", IBAN_A, "GENODE1X", "Bank A", skr_account=1200)
        db.insert_account("Konto B", "Owner", IBAN_B, "INGDDEFF", "Bank B", skr_account=1210)
        return db

    dbs = {"all": db_two_accounts, "single": second_db("single.db"), "order": second_db("order.db")}
    account_ids = {iban: _account_id(db_two_accounts, iban) for iban in (IBAN_A, IBAN_B)}
    rng = random.Random(20261111)
    seen_differs = seen_upload = seen_invalid = 0

    for number in range(200):
        uploads, existing = _random_upload(rng)
        files = _files_of(uploads)
        label = f"Fall {number}: Bestand {existing}, Dateien {[(i, len(t)) for i, t in uploads]}"
        per_file, stock = _model(existing, uploads)
        expected = Counter({(account_ids[iban], date, to_minor(amount)): n
                            for (iban, date, amount), n in stock.items() if n})

        for db in dbs.values():
            _wipe(db)
            for (iban, date, amount), n in existing.items():
                _seed(db, [(date, amount, "Bestand")] * n, iban=iban)

        previews = _preview(dbs["all"], files)            # vor dem Import, wie im Browser
        results = {mode: _confirm(dbs[mode], pending_factory, files, mode) for mode in MODES}

        # 1.+3. beide Wege legen genau den erwarteten Endstand an
        for mode in MODES:
            assert _bank(dbs[mode]) == expected, f"{label} ({mode})"
        assert _bank(dbs["all"]) == _bank(dbs["single"]), label

        # 2.+6. Vorschau und Import stimmen je Datei mit dem Modell überein
        for index, (modeled, preview) in enumerate(zip(per_file, previews)):
            where = f"{label}, Datei {index}"
            incomplete = [t for t in preview["transactions"] if "incomplete" in t["warn"]]
            if modeled is None:                      # ohne Konto: Vorschau „error“, nichts importiert
                assert preview["status"] == "error" and preview["dup_count"] == 0, where
                assert preview["dup_upload_count"] == 0, where
                assert preview["dup_upload_differs_count"] == 0, where
                for mode in MODES:
                    assert results[mode][index]["account_found"] is False, where
                continue
            assert (preview["new_count"], preview["dup_count"], preview["dup_upload_count"],
                    preview["dup_upload_differs_count"], len(incomplete)) == (
                modeled["inserted"], modeled["skipped"], modeled["from_upload"],
                modeled["differs"], modeled["invalid"]), where
            assert sum(1 for t in preview["transactions"] if t["text_differs"]) == modeled["differs"], where
            assert all(t["dup_source"] == "upload" for t in preview["transactions"]
                       if t["text_differs"]), where
            for mode in MODES:
                result = results[mode][index]
                assert (result["inserted"], result["skipped"], result["skipped_invalid"]) == (
                    modeled["inserted"], modeled["skipped"], modeled["invalid"]), (where, mode)
            assert results["all"][index]["skipped_upload"] == modeled["from_upload"], where
            assert results["single"][index]["skipped_upload"] == 0, where
            seen_differs += modeled["differs"]
            seen_upload += modeled["from_upload"]
            seen_invalid += modeled["invalid"]
        known = [p for p, m in zip(previews, per_file) if m is not None]
        assert sum(p["new_count"] for p in known) == sum(
            _bank(dbs["all"]).values()) - sum(existing.values()), label

        # 4. ein zweiter identischer Lauf legt nichts an
        for mode in MODES:
            again = _confirm(dbs[mode], pending_factory, files, mode)
            assert all(r.get("inserted", 0) == 0 for r in again), f"{label}, Wiederholung ({mode})"
            assert _bank(dbs[mode]) == expected, f"{label}, Wiederholung ({mode})"

        # 5. andere Reihenfolge der Dateien, derselbe Endstand
        shuffled = rng.sample(files, len(files))
        _confirm(dbs["order"], pending_factory, shuffled, "all")
        assert _bank(dbs["order"]) == expected, f"{label}, andere Reihenfolge"

    # der Zufall deckt die Fälle auch ab (sonst prüfte der Test zu wenig)
    assert seen_upload > 60 and seen_differs > 15 and seen_invalid > 50, (
        seen_upload, seen_differs, seen_invalid)
