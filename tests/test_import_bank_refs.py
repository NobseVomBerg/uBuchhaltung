# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kontoauszug-Import: Die Bankreferenz macht ein Duplikat verdächtig – die Zählung ändert sie nie.

Die Duplikatregel bleibt die heutige: Konto + Buchungsdatum + Betrag, zählbasiert je Datei, über die Dateien eines Laufs das Maximum, Quelle
``db`` vor ``upload``. Von selbst wird nie doppelt importiert. Trägt ein Umsatz eine Bankreferenz (``bank_ref``; CAMT ``AcctSvcrRef``, im Bestand
``BankTransactionDetails.BankRef``), dann prüft ``server.import_preview.classify_duplicates`` in zwei Durchgängen, ob er eine Zeile des Bestands belegt:

1. Jeder Umsatz mit Referenz belegt die erste freie Bestandszeile mit genau dieser Referenz.
2. Die übrigen Umsätze in Dateireihenfolge: ein Umsatz mit Referenz nimmt die erste freie Bestandszeile OHNE Referenz, einer ohne Referenz zuerst die
   erste freie MIT Referenz, sonst die erste ohne.

``dup_source`` bleibt Zeile für Zeile das Ergebnis der heutigen Zählung. ``ref_differs`` trägt genau ein Umsatz, den die Zählung als Duplikat verwirft, der aber
eine eigene Referenz hat und nach beiden Durchgängen keine Bestandszeile belegt: Die Vorschau markiert ihn (``ref_differs``, ``forceable``, Warnung ``ref_differs``), und der
Nutzer kann ihn über das Feld ``force`` von ``/confirm_transactions`` doch anlegen lassen. ``force`` wird streng und vor dem ersten Schreiben geprüft
(wie ``selection``, dazu: jede Position muss in der frisch gerechneten Einordnung verdächtig sein, sonst 400), gilt auch für ein reines Geschäftskonto und ist von ``selection`` unabhängig.
Ohne ``force`` ist das Ergebnis des Imports Bit für Bit das von früher.

Namen, Beträge und Referenzen sind erfunden; die IBANs sind die erlaubten Test-IBANs.
"""
import datetime
import json
import subprocess
from contextlib import closing
from itertools import chain, combinations, product
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from server import handlers
from server.import_preview import build_import_preview, classify_duplicates, match_account
from tests.http_client import own_origin, post, send
from tests.test_confirm_import import IBAN_A, IBAN_B, _bank_bookings, _txn, db_two_accounts, pending_factory  # noqa: F401
from tests.test_import_selection import _by_class, _cards, _render, _set_mixed, _toggle_and_list, books  # noqa: F401
from tests.test_import_upload_http import (NO_NODE, NODE, OWN_IBAN, Tree, _buttons, _multipart, _preview_source, _text,  # noqa: F401
                                           _walk, giro)

DATE = "2026-03-02"
DATE_OTHER = "2026-03-03"
AMOUNT = -12.5
KEY = (DATE, AMOUNT, 1)
INVALID = {"ok": False, "error": "Ungültige Auswahl"}
STALE = {"ok": False, "error": "Die Vorschau ist nicht mehr aktuell – bitte verwerfen und die Datei erneut hochladen."}


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _row(ref, date=DATE, text="Kaffee"):
    """Ein Umsatz mit der Bankreferenz ``ref`` (``''`` = keine, wie bei PDF und CSV und in einer Importdatei von früher: dann fehlt der Schlüssel)."""
    row = _txn(date, AMOUNT, text)
    if ref:
        row["bank_ref"] = ref
    return row


def _file(refs, iban=IBAN_A, date=DATE, name="auszug.xml"):
    return {"filename": name, "bank_code": "CAMT", "iban": iban, "document_date": "2026-03-31",
            "transactions": [_row(ref, date) for ref in refs]}


def _seed(db, refs, iban=IBAN_A, date=DATE, text="Kaffee"):
    """Bankbewegungen im Bestand, je mit ihrer Bankreferenz (``''`` = keine Zeile in ``BankTransactionDetails``)."""
    account = match_account(db.fetch_accounts(), iban)[0]
    for ref in refs:
        db.insert_booking(date, AMOUNT, account_id=account, foreign_bank_account="DE00FOREIGN", recipient_client="Partner",
                          text=text, booking_type="bank", bank_details={"bank_ref": ref} if ref else None)


def _refs(db):
    """Die Bankreferenzen aller Bankbewegungen, sortiert; ``''`` = keine."""
    with closing(db._get_connection()) as conn:
        return sorted(row[0] or "" for row in conn.execute(
            "SELECT d.BankRef FROM Bookings b LEFT JOIN BankTransactionDetails d ON d.Booking_ID = b.ID WHERE b.BookingType = 'bank'"))


def _refs_on(db, date):
    """Die Bankreferenzen der Bankbewegungen eines Tages, sortiert; ``''`` = keine."""
    with closing(db._get_connection()) as conn:
        return sorted(row[0] or "" for row in conn.execute(
            "SELECT d.BankRef FROM Bookings b LEFT JOIN BankTransactionDetails d ON d.Booking_ID = b.ID WHERE b.BookingType = 'bank' AND b.DateBooking = ?", (date,)))


def _confirm(db, import_id, force=None, selection=None, file_index=None):
    form = {"import_id": [import_id]}
    for name, value in (("force", force), ("selection", selection)):
        if value is not None:
            form[name] = [value if isinstance(value, str) else json.dumps(value)]
    if file_index is not None:
        form["file_index"] = [str(file_index)]
    status, body = handlers.handle_confirm_import(db, form)
    return status, json.loads(body)


def _run(db, pending_factory, files, mode="all", force=None):
    """Den Import einer Dateiliste ausführen: ``all`` = ein Aufruf („Alle importieren“), ``single`` = je Datei einer nacheinander (mit dem ``force`` dieser Datei)."""
    import_id = pending_factory(files)
    if mode == "all":
        status, data = _confirm(db, import_id, force=force)
        assert status == 200 and data["ok"], data
        return data["results"]
    results = []
    for index in range(len(files)):
        own = {str(index): force[str(index)]} if force and str(index) in force else None
        status, data = _confirm(db, import_id, force=own, file_index=index)
        assert status == 200 and data["ok"], data
        results.append(data["results"][0])
    return results


def _counts(results):
    """``(angelegt, Duplikate, davon aus anderer Datei)`` je Beleg."""
    return [(r["inserted"], r["skipped"], r["skipped_upload"]) for r in results]


# ── die reine Funktion ───────────────────────────────────────────────────────

def _classify(stock, refs):
    """``stock`` = [(Quelle, Referenz)] oder [(Quelle, Referenz, erzwungen)], ``refs`` = die Referenzen der Umsätze in Dateireihenfolge → [(dup_source, ref_differs)] je Umsatz."""
    triples = [row if len(row) == 3 else (*row, False) for row in stock]
    verdicts = classify_duplicates([(n, KEY, ref) for n, ref in enumerate(refs)], lambda key: list(triples))
    return [verdicts[n] for n in range(len(refs))]


@pytest.mark.parametrize("stock, refs, expected", [
    pytest.param([("db", "A")], ["B"], [("db", True)], id="fremde-Referenz"),
    pytest.param([("db", "A"), ("db", "B")], ["B", "C"], [("db", False), ("db", True)], id="eine-passt-eine-nicht"),
    pytest.param([("db", "A")], ["B", "A"], [(None, False), ("db", False)], id="die-passende-Zeile-ist-das-Duplikat-auch-wenn-sie-hinten-steht"),
    pytest.param([("db", "A")], ["A"], [("db", False)], id="gleiche-Referenz"),
    pytest.param([("db", "")], ["B"], [("db", False)], id="Bestand-ohne-Referenz"),
    pytest.param([("db", "A")], [""], [("db", False)], id="Umsatz-ohne-Referenz"),
    pytest.param([("db", "")], [""], [("db", False)], id="beide-ohne"),
    pytest.param([], ["A", "B"], [(None, False), (None, False)], id="nichts-im-Bestand"),
    pytest.param([("db", "A")], ["A", "A"], [("db", False), (None, False)], id="zweimal-gleich-einmal-im-Bestand"),
    pytest.param([("db", "A"), ("db", "")], ["B", "C"], [("db", False), ("db", True)], id="eine-Zeile-ohne-Referenz-nimmt-den-ersten"),
    pytest.param([("db", "A"), ("db", "")], ["", "B"], [("db", False), ("db", False)], id="ohne-Referenz-nimmt-zuerst-eine-mit"),
    pytest.param([("db", "A")], ["", "A"], [(None, False), ("db", False)], id="die-gleiche-Referenz-geht-dem-Umsatz-ohne-Referenz-vor"),
    pytest.param([("db", "A"), ("upload", "B")], ["B", "C"], [("upload", False), ("db", True)], id="die-Quelle-ist-die-der-belegten-Zeile"),
    pytest.param([("db", "A"), ("upload", "B")], ["C", "D"], [("db", True), ("upload", True)], id="beide-fremd-Quelle-nach-Zaehlung-db-vor-upload"),
    pytest.param([("upload", "A")], ["A", "B"], [("upload", False), (None, False)], id="Bestand-aus-diesem-Lauf"),
    pytest.param([("db", "A"), ("db", "B")], ["A", "C", "D"], [("db", False), ("db", True), (None, False)], id="aufgefuellt-wird-der-erste-nicht-zugeordnete"),
    pytest.param([("db", "A"), ("db", "B")], ["C", "D", "E"], [("db", True), ("db", True), (None, False)], id="nur-so-viele-wie-die-Zahl-erlaubt"),
    # Zeilen, die dieser Lauf erzwungen angelegt hat: sie zählen nicht mit, treffen aber bei gleicher Referenz
    pytest.param([("db", "A"), ("upload", "B", True)], ["B", "C"], [("upload", False), (None, False)], id="erzwungene-Zeile-trifft-und-zaehlt-nicht"),
    pytest.param([("upload", "B", True)], ["C"], [(None, False)], id="nur-eine-erzwungene-Zeile-im-Bestand"),
    pytest.param([("db", "A"), ("upload", "B", True), ("upload", "C", True)], ["A", "B", "C"], [("db", False), ("upload", False), ("upload", False)], id="Treffer-duerfen-die-Zahl-uebersteigen"),
    pytest.param([("db", "A"), ("upload", "B", True)], ["X"], [("db", True)], id="die-Quelle-nach-Zaehlung-nie-von-einer-erzwungenen-Zeile"),
    pytest.param([("db", ""), ("upload", "B", True)], ["B", "C"], [("upload", False), (None, False)], id="der-Treffer-nimmt-den-Platz-der-Zahl"),
])
def test_the_references_decide_who_is_the_duplicate_and_who_looks_foreign(stock, refs, expected):
    assert _classify(stock, refs) == expected


def test_the_keys_of_one_file_do_not_disturb_each_other():
    stocks = {("2026-03-02", -1.0, 1): [("db", "A", False)], ("2026-03-02", -2.0, 1): [("db", "B", False)], ("2026-03-03", -1.0, 1): []}
    rows = [(0, ("2026-03-02", -1.0, 1), "B"), (1, ("2026-03-02", -2.0, 1), "B"), (2, ("2026-03-03", -1.0, 1), "B"), (3, ("2026-03-02", -1.0, 1), "A")]
    assert classify_duplicates(rows, stocks.__getitem__) == {0: (None, False), 1: ("db", False), 2: (None, False), 3: ("db", False)}


def _sequences(alphabet, longest):
    return [seq for length in range(longest + 1) for seq in product(alphabet, repeat=length)]


def _subsets(items):
    return chain.from_iterable(combinations(items, size) for size in range(len(items) + 1))


def _after_the_import(stock, refs, verdicts, forced):
    """Die Referenzen im Bestand nach dem Import der Datei: der Bestand, jeder Umsatz, der kein Duplikat ist, und jeder erzwungene."""
    return [row[1] for row in stock] + [refs[n] for n, (source, _differs) in enumerate(verdicts) if source is None or n in forced]


def _never_doubles(stock, refs, after):
    """Jede Referenz der Datei steht im Bestand höchstens so oft, wie sie vorher dort stand oder in der Datei vorkommt."""
    before = [row[1] for row in stock]
    return all(after.count(ref) <= max(before.count(ref), refs.count(ref)) for ref in set(refs) - {""})


def test_the_number_of_duplicates_is_the_count_for_every_small_combination():
    """Alle Bestände und Dateien aus höchstens drei Zeilen mit den Referenzen „keine“, A und B (und jeder Aufteilung des Bestands in ``db`` und ``upload``): Die Zahl der Duplikate ist
    ``min(Umsätze, Bestand)`` – die Referenz ändert daran nie etwas. Ohne Referenz auf einer Seite sind auch die Zeilen der Datei, die Duplikate sind, die ersten nach Position (bei einem Bestand ohne Referenz
    auch ihre Quelle); verdächtig ist nur ein Duplikat mit eigener Referenz. Und: Wer verdächtig ist und erzwungen wird, legt nur sich selbst an – kein als neu gezeigter Umsatz verdoppelt eine vorhandene
    Bewegung."""
    checked = 0
    for refs_in_stock in _sequences(("", "A", "B"), 3):
        for split in range(len(refs_in_stock) + 1):
            stock = [("db" if n < split else "upload", ref, False) for n, ref in enumerate(refs_in_stock)]
            for refs in _sequences(("", "A", "B"), 3):
                verdicts = _classify(stock, refs)
                where = (stock, refs)
                assert sum(source is not None for source, _ in verdicts) == min(len(refs), len(stock)), where
                for n, (source, differs) in enumerate(verdicts):
                    assert not differs or (source is not None and refs[n] != ""), (where, n)
                if not any(ref for _source, ref, _forced in stock) or not any(refs):
                    assert [source is not None for source, _ in verdicts] == [n < len(stock) for n in range(len(refs))], where
                    assert not any(differs for _source, differs in verdicts), where
                if not any(ref for _source, ref, _forced in stock):
                    assert [source for source, _ in verdicts] == [stock[n][0] if n < len(stock) else None for n in range(len(refs))], where
                flagged = [n for n, (_source, differs) in enumerate(verdicts) if differs]
                for forced in _subsets(flagged):
                    assert _never_doubles(stock, refs, _after_the_import(stock, refs, verdicts, set(forced))), (where, forced)
                checked += 1
    assert checked > 3000


def test_forced_rows_of_the_run_never_count_but_they_meet_the_same_reference():
    """Bestände mit bis zu drei Zeilen, darunter erzwungene (immer mit eigener Referenz): Die Zahl der Duplikate ist die Zählung über die nicht erzwungenen Zeilen – nur Treffer mit gleicher
    Referenz auf eine erzwungene Zeile können sie übersteigen. Auch hier verdoppelt kein neuer und kein erzwungener Umsatz eine vorhandene Bewegung."""
    kinds = [("db", ""), ("db", "A"), ("db", "B"), ("upload", "A", True), ("upload", "B", True)]
    checked = 0
    for length in range(4):
        for rows in product(kinds, repeat=length):
            stock = [row if len(row) == 3 else (*row, False) for row in rows]
            counted = sum(1 for _s, _r, forced in stock if not forced)
            forced_refs = {ref for _s, ref, forced in stock if forced}
            for refs in _sequences(("", "A", "B"), 3):
                verdicts = _classify(stock, refs)
                duplicates = sum(source is not None for source, _ in verdicts)
                assert duplicates >= min(len(refs), counted), (stock, refs)
                if not forced_refs & set(refs):
                    assert duplicates == min(len(refs), counted), (stock, refs)
                for n, (source, differs) in enumerate(verdicts):
                    assert not differs or (source is not None and refs[n] != ""), (stock, refs, n)
                flagged = [n for n, (_source, differs) in enumerate(verdicts) if differs]
                for forced in _subsets(flagged):
                    assert _never_doubles(stock, refs, _after_the_import(stock, refs, verdicts, set(forced))), (stock, refs, forced)
                checked += 1
    assert checked > 5000


def test_the_whole_way_does_what_the_function_says_for_every_small_combination(db_two_accounts, pending_factory):
    """Bestand und Datei aus höchstens zwei Zeilen mit den Referenzen „keine“, A und B – je Kombination ein eigener Tag –, durch Vorschau und Import, mit ``force`` auf jede verdächtige Zeile:
    Die Vorschau meldet, was die Funktion sagt; der Import legt genau die neuen und die erzwungenen Zeilen an; die Referenzen im Bestand danach sind die der Simulation – und keine verdoppelt
    eine vorhandene Bewegung."""
    day = datetime.date(2027, 1, 1)
    checked = 0
    for stock_refs, file_refs in product(_sequences(("", "A", "B"), 2), [seq for seq in _sequences(("", "A", "B"), 2) if seq]):
        date = (day + datetime.timedelta(days=checked)).isoformat()
        _seed(db_two_accounts, list(stock_refs), date=date)
        file = _file(list(file_refs), date=date)
        verdicts = _classify([("db", ref) for ref in stock_refs], list(file_refs))
        (card,) = _preview(db_two_accounts, [file])
        assert [(t["dup"], t["ref_differs"]) for t in card["transactions"]] == [(source is not None, differs) for source, differs in verdicts], (stock_refs, file_refs)
        flagged = [n for n, (_source, differs) in enumerate(verdicts) if differs]
        status, data = _confirm(db_two_accounts, pending_factory([file]), force={"0": flagged} if flagged else None)
        assert status == 200, (stock_refs, file_refs, data)
        (result,) = data["results"]
        new = sum(source is None for source, _ in verdicts)
        assert (result["inserted"], result["skipped"], result.get("forced", 0)) == (new + len(flagged), len(file_refs) - new - len(flagged), len(flagged)), (stock_refs, file_refs)
        after = _after_the_import([("db", ref) for ref in stock_refs], list(file_refs), verdicts, set(flagged))
        assert _refs_on(db_two_accounts, date) == sorted(after) and _never_doubles([("db", ref) for ref in stock_refs], list(file_refs), after), (stock_refs, file_refs)
        checked += 1
    assert checked > 100


def test_without_any_reference_the_result_is_the_plain_count():
    for stock_size, rows in product(range(4), range(5)):
        stock = [("db", "")] * min(stock_size, 2) + [("upload", "")] * max(0, stock_size - 2)
        verdicts = _classify(stock, [""] * rows)
        assert verdicts == [(stock[n][0] if n < stock_size else None, False) for n in range(rows)]


# ── Vorschau und Import: die Fälle ───────────────────────────────────────────

#: je Fall: ``stock`` = Referenzen im Bestand, ``files`` = Referenzen der Umsätze je Datei (alle mit gleichem Datum und Betrag), ``preview`` = je Datei (neu, Duplikate, davon aus
#: anderer Datei), ``flags`` = je Datei die Positionen, die die Vorschau als verdächtig markiert, ``all`` = je Datei (angelegt, Duplikate, davon aus anderer Datei) bei „Alle
#: importieren“ OHNE ``force`` – die Zahlen sind die der Zählung allein –, ``end`` = die Referenzen im Bestand danach.
CASES = {
    "fremde-referenz": dict(stock=["A"], files=[["B"]], preview=[(0, 1, 0)], flags=[[0]], all=[(0, 1, 0)], end=["A"]),
    "eine-passt-eine-nicht": dict(stock=["A", "B"], files=[["B", "C"]], preview=[(0, 2, 0)], flags=[[1]], all=[(0, 2, 0)], end=["A", "B"]),
    "die-passende-steht-hinten": dict(stock=["A"], files=[["B", "A"]], preview=[(1, 1, 0)], flags=[[]], all=[(1, 1, 0)], end=["A", "B"]),
    "treffer-hinten-fremder-vorn": dict(stock=["A", "B"], files=[["C", "A"]], preview=[(0, 2, 0)], flags=[[0]], all=[(0, 2, 0)], end=["A", "B"]),
    "drei-zeilen-ein-treffer": dict(stock=["A", "B"], files=[["A", "C", "D"]], preview=[(1, 2, 0)], flags=[[1]], all=[(1, 2, 0)], end=["A", "B", "D"]),
    "gleiche-referenz": dict(stock=["A"], files=[["A"]], preview=[(0, 1, 0)], flags=[[]], all=[(0, 1, 0)], end=["A"]),
    "bestand-ohne-referenz": dict(stock=[""], files=[["B"]], preview=[(0, 1, 0)], flags=[[]], all=[(0, 1, 0)], end=[""]),
    "umsatz-ohne-referenz": dict(stock=["A"], files=[[""]], preview=[(0, 1, 0)], flags=[[]], all=[(0, 1, 0)], end=["A"]),
    "beide-ohne-referenz": dict(stock=[""], files=[[""]], preview=[(0, 1, 0)], flags=[[]], all=[(0, 1, 0)], end=[""]),
    "neue-umsaetze": dict(stock=[], files=[["A", "B"]], preview=[(2, 0, 0)], flags=[[]], all=[(2, 0, 0)], end=["A", "B"]),
    "zweimal-gleich": dict(stock=["A"], files=[["A", "A"]], preview=[(1, 1, 0)], flags=[[]], all=[(1, 1, 0)], end=["A", "A"]),
    "bestand-mit-und-ohne-referenz": dict(stock=["A", ""], files=[["B", "C"]], preview=[(0, 2, 0)], flags=[[1]], all=[(0, 2, 0)], end=["", "A"]),
    "alle-passen": dict(stock=["A", "B"], files=[["A", "B"]], preview=[(0, 2, 0)], flags=[[]], all=[(0, 2, 0)], end=["A", "B"]),
    "ohne-referenz-nimmt-zuerst-eine-mit": dict(stock=["A", ""], files=[["", "B"]], preview=[(0, 2, 0)], flags=[[]], all=[(0, 2, 0)], end=["", "A"]),
    "ohne-referenz-steht-vor-der-gleichen": dict(stock=["A"], files=[["", "A"]], preview=[(1, 1, 0)], flags=[[]], all=[(1, 1, 0)], end=["", "A"]),
    "mehr-umsaetze-als-bestand": dict(stock=["A"], files=[["B", "C"]], preview=[(1, 1, 0)], flags=[[0]], all=[(1, 1, 0)], end=["A", "C"]),
    "zwei-dateien-fremd": dict(stock=["A"], files=[["B"], ["B"]], preview=[(0, 1, 0), (0, 1, 0)], flags=[[0], [0]],
                               all=[(0, 1, 0), (0, 1, 0)], end=["A"]),
    "zwei-dateien-erst-neu-dann-fremd": dict(stock=[], files=[["A"], ["B"]], preview=[(1, 0, 0), (0, 1, 1)], flags=[[], [0]],
                                             all=[(1, 0, 0), (0, 1, 1)], end=["A"]),
    "zwei-dateien-gleiche-referenz": dict(stock=[], files=[["A"], ["A"]], preview=[(1, 0, 0), (0, 1, 1)], flags=[[], []],
                                          all=[(1, 0, 0), (0, 1, 1)], end=["A"]),
    "zwei-dateien-ueberlappend": dict(stock=[], files=[["A", "B"], ["A", "B", "C"]], preview=[(2, 0, 0), (1, 2, 2)], flags=[[], []],
                                      all=[(2, 0, 0), (1, 2, 2)], end=["A", "B", "C"]),
    "zwei-dateien-erste-passt": dict(stock=["A"], files=[["A"], ["B", "C"]], preview=[(0, 1, 0), (1, 1, 0)], flags=[[], [0]],
                                     all=[(0, 1, 0), (1, 1, 0)], end=["A", "C"]),
    "datei-ohne-referenzen": dict(stock=["A"], files=[["", ""]], preview=[(1, 1, 0)], flags=[[]], all=[(1, 1, 0)], end=["", "A"]),
}
CASE_PARAMS = [pytest.param(case, id=name) for name, case in CASES.items()]
MODES = ("all", "single")


def _preview(db, files):
    return build_import_preview(db, {"files": files})["files"]


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_the_preview_marks_exactly_the_suspect_rows_and_counts_as_before(db_two_accounts, case):
    _seed(db_two_accounts, case["stock"])
    cards = _preview(db_two_accounts, [_file(refs, name=f"auszug{n}.xml") for n, refs in enumerate(case["files"])])
    assert [(c["new_count"], c["dup_count"], c["dup_upload_count"]) for c in cards] == case["preview"]
    assert [[t["index"] for t in c["transactions"] if t["ref_differs"]] for c in cards] == case["flags"]
    for card in cards:
        for t in card["transactions"]:
            assert t["forceable"] is t["ref_differs"]
            assert not t["ref_differs"] or (t["dup"] and not t["importable"] and "ref_differs" in t["warn"] and t["dup_source"] in ("db", "upload"))
            assert t["ref_differs"] or "ref_differs" not in t["warn"]
        flagged = [t for t in card["transactions"] if t["ref_differs"]]
        assert [p for p in card["problems"] if p["ref_differs"]] == flagged            # auch die Liste „Auffällige Buchungen“ nennt sie
        if flagged:
            assert card["status"] == "warn"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", CASE_PARAMS)
def test_without_force_the_import_counts_as_before_and_equals_the_preview(db_two_accounts, pending_factory, case, mode):
    """Die Zählung von heute: ``skipped_upload`` kennt nur der Lauf über alle Dateien. Vorschau und Import stimmen überein (``new_count`` = ``inserted``, ``dup_count`` = ``skipped``,
    ``dup_upload_count`` = ``skipped_upload``); die Zeilen, die der Import anlegt, tragen ihre Bankreferenz; nichts wird erzwungen (kein Schlüssel ``forced``)."""
    _seed(db_two_accounts, case["stock"])
    files = [_file(refs, name=f"auszug{n}.xml") for n, refs in enumerate(case["files"])]
    cards = _preview(db_two_accounts, files)
    results = _run(db_two_accounts, pending_factory, files, mode)
    assert _counts(results) == [(inserted, skipped, up if mode == "all" else 0) for inserted, skipped, up in case["all"]]
    if mode == "all":
        assert [(c["new_count"], c["dup_count"], c["dup_upload_count"]) for c in cards] == _counts(results)
    assert all("forced" not in r and r["error"] is None for r in results)
    assert _refs(db_two_accounts) == sorted(case["end"])


# ── force: erzwingen, was die Vorschau verdächtig nennt ──────────────────────

#: je Fall mit verdächtigen Zeilen: ``force`` (je Datei die Positionen), ``all`` = je Datei (angelegt, Duplikate, davon aus anderer Datei, erzwungen), ``end`` = die Referenzen danach
FORCED = {
    "fremde-referenz": dict(force={"0": [0]}, all=[(1, 0, 0, 1)], end=["A", "B"]),
    "eine-passt-eine-nicht": dict(force={"0": [1]}, all=[(1, 1, 0, 1)], end=["A", "B", "C"]),
    "treffer-hinten-fremder-vorn": dict(force={"0": [0]}, all=[(1, 1, 0, 1)], end=["A", "B", "C"]),
    "drei-zeilen-ein-treffer": dict(force={"0": [1]}, all=[(2, 1, 0, 1)], end=["A", "B", "C", "D"]),
    "bestand-mit-und-ohne-referenz": dict(force={"0": [1]}, all=[(1, 1, 0, 1)], end=["", "A", "C"]),
    "mehr-umsaetze-als-bestand": dict(force={"0": [0]}, all=[(2, 0, 0, 1)], end=["A", "B", "C"]),
    "zwei-dateien-erst-neu-dann-fremd": dict(force={"1": [0]}, all=[(1, 0, 0, 0), (1, 0, 0, 1)], end=["A", "B"]),
    "zwei-dateien-erste-passt": dict(force={"1": [0]}, all=[(0, 1, 0, 0), (2, 0, 0, 1)], end=["A", "B", "C"]),
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", sorted(FORCED))
def test_force_creates_exactly_the_named_suspect_row_and_nothing_else(db_two_accounts, pending_factory, name, mode):
    case, forced = CASES[name], FORCED[name]
    _seed(db_two_accounts, case["stock"])
    files = [_file(refs, name=f"auszug{n}.xml") for n, refs in enumerate(case["files"])]
    flags = [[t["index"] for t in c["transactions"] if t["forceable"]] for c in _preview(db_two_accounts, files)]
    assert {str(n): positions for n, positions in enumerate(flags) if positions} == forced["force"]      # genau das bietet die Vorschau an
    results = _run(db_two_accounts, pending_factory, files, mode, force=forced["force"])
    assert [(r["inserted"], r["skipped"], r["skipped_upload"] if mode == "all" else 0, r.get("forced", 0)) for r in results] == [
        (i, s, u if mode == "all" else 0, f) for i, s, u, f in forced["all"]]
    assert all((r.get("forced", 0) > 0) == ("forced" in r) for r in results)               # der Schlüssel steht nur bei erzwungenen Umsätzen da
    assert _refs(db_two_accounts) == sorted(forced["end"])
    # die erzwungene Zeile trägt ihre eigene Referenz
    for n, positions in enumerate(flags):
        for position in positions:
            assert case["files"][n][position] in _refs(db_two_accounts)


def test_forcing_the_same_transaction_from_two_files_creates_it_once(db_two_accounts, pending_factory):
    """Der Umsatz „B“ steht in zwei Dateien, beide Kästchen sind angehakt (beide Karten zeigten ihn als verdächtig): Die erste Datei legt ihn an, in der zweiten trifft seine Referenz die erzwungene
    Zeile – ein gewöhnliches Duplikat, aus diesem Lauf (``skipped_upload``), nicht erzwungen."""
    _seed(db_two_accounts, ["A"])
    files = [_file(["B"], name="eins.xml"), _file(["B"], name="zwei.xml")]
    results = _run(db_two_accounts, pending_factory, files, "all", force={"0": [0], "1": [0]})
    assert [(r["inserted"], r["skipped"], r["skipped_upload"], r.get("forced", 0)) for r in results] == [(1, 0, 0, 1), (0, 1, 1, 0)]
    assert _refs(db_two_accounts) == ["A", "B"]


def test_a_forced_row_never_doubles_an_existing_booking(db_two_accounts, pending_factory):
    """Bestand „A“; die Datei hat „B“ (echt neu) vor „A“ (der schon importierten Bewegung). Die passende Zeile ist das Duplikat: „A“ wird übersprungen, „B“ ist neu – nichts ist erzwingbar, und
    ein ``force`` auf „B“ ist die Vorschau von vorgestern (400). Als „B“ noch das Duplikat und „A“ neu war, stand „A“ mit ``force`` zweimal im Bestand."""
    _seed(db_two_accounts, ["A"])
    file = _file(["B", "A"])
    (card,) = _preview(db_two_accounts, [file])
    assert [(t["index"], t["dup"], t["importable"], t["forceable"]) for t in card["transactions"]] == [(0, False, True, False), (1, True, False, False)]
    import_id = pending_factory([file])
    assert _confirm(db_two_accounts, import_id, force={"0": [0]}) == (400, STALE)
    assert _refs(db_two_accounts) == ["A"]
    status, data = _confirm(db_two_accounts, import_id)
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["skipped"]) == (1, 1)
    assert _refs(db_two_accounts) == ["A", "B"]                                                   # „A“ steht einmal da


def test_a_forced_row_of_an_earlier_file_does_not_swallow_a_new_row_of_the_next(db_two_accounts, pending_factory):
    """Bestand „A“; Datei 0 erzwingt „B“; Datei 1 hat „B“ und „C“. Die erzwungene Zeile zählt nicht für die Zahl, aber ihre Referenz trifft: „B“ ist das Duplikat dieser Zeile, „C“ ist neu – Ende „A“, „B“,
    „C“. Die Vorschau (sie kennt kein ``force``) zeigt Datei 1 genauso: „C“ ist neu."""
    _seed(db_two_accounts, ["A"])
    files = [_file(["B"], name="null.xml"), _file(["B", "C"], name="eins.xml")]
    cards = _preview(db_two_accounts, files)
    assert [(c["new_count"], c["dup_count"]) for c in cards] == [(0, 1), (1, 1)]
    assert [t["index"] for t in cards[1]["transactions"] if t["forceable"]] == [0]
    results = _run(db_two_accounts, pending_factory, files, force={"0": [0]})
    assert [(r["inserted"], r["skipped"], r["skipped_upload"], r.get("forced", 0)) for r in results] == [(1, 0, 0, 1), (1, 1, 1, 0)]
    assert _refs(db_two_accounts) == ["A", "B", "C"]


def test_the_same_with_force_ticked_on_both_cards_changes_nothing_for_the_second(db_two_accounts, pending_factory):
    """Hakt der Nutzer auch „B“ der zweiten Karte an (dort war es in der Vorschau verdächtig), prüft der Import es gegen seinen Bestand: Es trifft die erzwungene Zeile, das ``force`` ist wirkungslos."""
    _seed(db_two_accounts, ["A"])
    files = [_file(["B"], name="null.xml"), _file(["B", "C"], name="eins.xml")]
    results = _run(db_two_accounts, pending_factory, files, force={"0": [0], "1": [0]})
    assert [(r["inserted"], r["skipped"], r["skipped_upload"], r.get("forced", 0)) for r in results] == [(1, 0, 0, 1), (1, 1, 1, 0)]
    assert _refs(db_two_accounts) == ["A", "B", "C"]


def test_a_separate_import_after_a_forced_one_counts_the_forced_row_like_any_other(db_two_accounts, pending_factory):
    """Zwei Aufrufe nacheinander (der Einzelimport): Die erzwungene Zeile ist beim zweiten Aufruf Bestand wie jede andere. Datei 1 („B“, „C“) hat dann zwei Umsätze für zwei Bestandszeilen („A“, „B“):
    „B“ trifft seine Zeile, „C“ ist nur nach Zählung ein Duplikat – verdächtig, die nächste Vorschau bietet es an."""
    _seed(db_two_accounts, ["A"])
    files = [_file(["B"], name="null.xml"), _file(["B", "C"], name="eins.xml")]
    results = _run(db_two_accounts, pending_factory, files, "single", force={"0": [0]})
    assert [(r["inserted"], r["skipped"], r.get("forced", 0)) for r in results] == [(1, 0, 1), (0, 2, 0)]
    assert _refs(db_two_accounts) == ["A", "B"]
    (card,) = _preview(db_two_accounts, [files[1]])
    assert [t["index"] for t in card["transactions"] if t["forceable"]] == [1]
    assert _run(db_two_accounts, pending_factory, [files[1]], force={"0": [1]})[0]["forced"] == 1
    assert _refs(db_two_accounts) == ["A", "B", "C"]


@pytest.mark.parametrize("selection", [[1], [0, 1], [], [2]], ids=lambda s: "Auswahl-" + "-".join(map(str, s)) if s else "Auswahl-leer")
def test_force_is_independent_of_the_selection_on_a_mixed_account(db_two_accounts, pending_factory, selection):
    """Konto A gemischt genutzt: Der Bestand hat „A“, die Datei „B“ (verdächtig, Position 0), „C“ und „D“ (neu, Positionen 1 und 2). ``force`` legt „B“ an, auch wenn ``selection`` die
    Position 0 nicht nennt; nennt sie sie, ändert das nichts (ein Duplikat bleibt ohne ``force`` draußen). Von den neuen Umsätzen kommen nur die gewählten dazu."""
    _set_mixed(db_two_accounts, IBAN_A)
    _seed(db_two_accounts, ["A"])
    import_id = pending_factory([_file(["B", "C", "D"])])
    status, data = _confirm(db_two_accounts, import_id, force={"0": [0]}, selection={"0": selection})
    assert status == 200, data
    (result,) = data["results"]
    new_chosen = len({1, 2} & set(selection))
    assert (result["inserted"], result["skipped"], result["skipped_unselected"], result["forced"]) == (1 + new_chosen, 0, 2 - new_chosen, 1)
    assert _refs(db_two_accounts) == sorted(["A", "B"] + [("C", "D")[position - 1] for position in selection if position in (1, 2)])


def test_a_selection_naming_the_suspect_row_alone_still_leaves_it_out_without_force(db_two_accounts, pending_factory):
    _set_mixed(db_two_accounts, IBAN_A)
    _seed(db_two_accounts, ["A"])
    status, data = _confirm(db_two_accounts, pending_factory([_file(["B", "C"])]), selection={"0": [0, 1]})
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["skipped"]) == (1, 1) and "forced" not in data["results"][0]
    assert _refs(db_two_accounts) == ["A", "C"]


def test_force_works_for_a_pure_business_account_where_a_selection_is_refused(db_two_accounts, pending_factory):
    _seed(db_two_accounts, ["A"], iban=IBAN_B)
    import_id = pending_factory([_file(["B"], iban=IBAN_B)])
    status, data = _confirm(db_two_accounts, import_id, selection={"0": [0]})
    assert status == 400 and data["error"].startswith("Für ein reines Geschäftskonto")
    assert _refs(db_two_accounts) == ["A"]
    status, data = _confirm(db_two_accounts, import_id, force={"0": [0]})
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["forced"]) == (1, 1)
    assert _refs(db_two_accounts) == ["A", "B"]


@pytest.fixture
def mixed_upload(db_two_accounts, pending_factory):
    """Fünf Dateien: eine mit verdächtiger Zeile (0), eine mit gleicher Referenz (1), eine mit Duplikat ohne Referenz (2), eine mit neuer Zeile (3), eine mit unvollständiger Zeile (4)
    und eine für eine IBAN ohne Konto (5)."""
    _seed(db_two_accounts, ["A"])
    _seed(db_two_accounts, ["X"], date=DATE_OTHER)
    incomplete = _file(["Q"], name="vier.xml")
    incomplete["transactions"][0]["date"] = None
    files = [_file(["B"], name="null.xml"), _file(["A"], name="eins.xml"), _file([""], date=DATE_OTHER, name="zwei.xml"),
             _file(["Q"], date="2026-03-04", name="drei.xml"), incomplete, _file(["B"], iban="DE00000000001000000001", name="fuenf.xml")]
    return SimpleNamespace(db=db_two_accounts, import_id=pending_factory(files), files=files, before=_bank_bookings(db_two_accounts))


#: Formfehler und Belege, die dieser Lauf nicht anlegt (Datei ohne Konto): „Ungültige Auswahl“
INVALID_FORCE = [
    pytest.param({"5": [0]}, id="Datei-ohne-Konto"),
    pytest.param({"0": [1]}, id="Position-ausserhalb"),
    pytest.param({"0": [-1]}, id="negative-Position"),
    pytest.param({"6": [0]}, id="unbekannter-Beleg"),
    pytest.param({"00": [0]}, id="Schluessel-nicht-kanonisch"),
    pytest.param({"+0": [0]}, id="Schluessel-mit-Vorzeichen"),
    pytest.param({"x": [0]}, id="Schluessel-kein-Zahl"),
    pytest.param({"0": [True]}, id="bool-statt-Zahl"),
    pytest.param({"0": [0.0]}, id="Kommazahl"),
    pytest.param({"0": ["0"]}, id="Text-statt-Zahl"),
    pytest.param({"0": [0, 0]}, id="doppelte-Position"),
    pytest.param({"0": 0}, id="keine-Liste"),
    pytest.param({"0": None}, id="null-statt-Liste"),
    pytest.param([0], id="Liste-statt-Objekt"),
    pytest.param("{\"0\": [0]", id="kein-JSON"),
    pytest.param("{\"0\": [0], \"0\": [0]}", id="doppelter-Schluessel"),
    pytest.param("\"0\"", id="JSON-Text"),
    pytest.param("1", id="JSON-Zahl"),
]

#: eine Position im Lauf, die in der frisch gerechneten Einordnung nicht (mehr) verdächtig ist: der feste Satz zur veralteten Vorschau
STALE_FORCE = [
    pytest.param({"1": [0]}, id="gleiche-Referenz-ist-kein-Verdacht"),
    pytest.param({"2": [0]}, id="Duplikat-ohne-Referenz"),
    pytest.param({"3": [0]}, id="neue-Zeile"),
    pytest.param({"4": [0]}, id="unvollstaendige-Zeile"),
    pytest.param({"0": [0], "3": [0]}, id="eine-verdaechtige-und-eine-nicht"),
]


@pytest.mark.parametrize("force", INVALID_FORCE)
def test_a_force_with_a_bad_form_is_refused_before_anything_is_written(mixed_upload, force):
    status, data = _confirm(mixed_upload.db, mixed_upload.import_id, force=force)
    assert (status, data) == (400, INVALID)
    assert _bank_bookings(mixed_upload.db) == mixed_upload.before
    # nichts ist verbraucht: dieselbe Vorschau lässt sich danach mit einem gültigen force bestätigen
    status, data = _confirm(mixed_upload.db, mixed_upload.import_id, force={"0": [0]}, file_index=0)
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["forced"]) == (1, 1)


@pytest.mark.parametrize("force", STALE_FORCE)
def test_a_force_on_a_row_that_is_not_suspect_any_more_says_the_preview_is_out_of_date(mixed_upload, force):
    status, data = _confirm(mixed_upload.db, mixed_upload.import_id, force=force)
    assert (status, data) == (400, STALE)
    assert _bank_bookings(mixed_upload.db) == mixed_upload.before
    status, data = _confirm(mixed_upload.db, mixed_upload.import_id, force={"0": [0]}, file_index=0)
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["forced"]) == (1, 1)


def test_a_single_import_after_a_preview_over_several_files_says_so_when_the_preview_assumed_the_other_file(db_two_accounts, pending_factory):
    """Die Vorschau rechnete die neue Zeile der ersten Datei („A“) schon als Bestand: „B“ der zweiten war darum verdächtig. Wer die zweite Datei allein importiert, hat diesen Bestand nicht – „B“ ist dort neu,
    das ``force`` darauf ist ein Rest der Vorschau (feste Meldung, nichts geschrieben); ohne ``force`` wird „B“ angelegt."""
    files = [_file(["A"], name="eins.xml"), _file(["B"], name="zwei.xml")]
    cards = _preview(db_two_accounts, files)
    assert [t["index"] for t in cards[1]["transactions"] if t["forceable"]] == [0]
    import_id = pending_factory(files)
    assert _confirm(db_two_accounts, import_id, force={"1": [0]}, file_index=1) == (400, STALE)
    assert _refs(db_two_accounts) == []
    status, data = _confirm(db_two_accounts, import_id, file_index=1)
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["skipped"]) == (1, 0)
    assert _refs(db_two_accounts) == ["B"]


def test_the_field_force_may_appear_only_once(mixed_upload):
    form = {"import_id": [mixed_upload.import_id], "force": [json.dumps({"0": [0]}), json.dumps({"0": [0]})]}
    status, body = handlers.handle_confirm_import(mixed_upload.db, form)
    assert (status, json.loads(body)) == (400, INVALID) and _bank_bookings(mixed_upload.db) == mixed_upload.before


@pytest.mark.parametrize("empty", ["", "{}", {"0": []}], ids=["Feld-leer", "leeres-Objekt", "leere-Liste"])
def test_an_empty_force_is_no_force(db_two_accounts, pending_factory, empty):
    _seed(db_two_accounts, ["A"])
    status, data = _confirm(db_two_accounts, pending_factory([_file(["A", "C"])]), force=empty)
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["skipped"]) == (1, 1) and "forced" not in data["results"][0]


def test_force_for_a_file_outside_this_run_or_already_imported_is_refused(mixed_upload):
    """Ein Einzelimport nimmt nur ``force`` für seinen eigenen Beleg; ein schon importierter Beleg kann nichts mehr erzwingen."""
    status, data = _confirm(mixed_upload.db, mixed_upload.import_id, force={"0": [0]}, file_index=1)
    assert (status, data) == (400, INVALID) and _bank_bookings(mixed_upload.db) == mixed_upload.before
    status, data = _confirm(mixed_upload.db, mixed_upload.import_id, file_index=0)                       # Beleg 0 ohne force importieren: das Duplikat bleibt draußen
    assert status == 200 and data["results"][0]["inserted"] == 0
    status, data = _confirm(mixed_upload.db, mixed_upload.import_id, force={"0": [0]}, file_index=1)
    assert (status, data) == (400, INVALID)
    status, data = _confirm(mixed_upload.db, mixed_upload.import_id, force={"0": [0]})
    assert (status, data) == (400, INVALID)                                                              # Beleg 0 ist schon importiert
    assert _bank_bookings(mixed_upload.db) == mixed_upload.before


def test_after_forcing_the_row_is_an_ordinary_duplicate_with_the_same_reference(db_two_accounts, pending_factory):
    _seed(db_two_accounts, ["A"])
    file = _file(["B"])
    assert _run(db_two_accounts, pending_factory, [file], force={"0": [0]})[0]["forced"] == 1
    assert _refs(db_two_accounts) == ["A", "B"]
    (card,) = _preview(db_two_accounts, [file])
    (row,) = card["transactions"]
    assert (row["dup"], row["dup_source"], row["ref_differs"], row["forceable"]) == (True, "db", False, False)
    assert "ref_differs" not in row["warn"] and (card["new_count"], card["dup_count"]) == (0, 1)
    import_id = pending_factory([file])
    assert _confirm(db_two_accounts, import_id, force={"0": [0]}) == (400, STALE)                     # nicht mehr verdächtig
    status, data = _confirm(db_two_accounts, import_id)
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["skipped"]) == (0, 1)
    assert _refs(db_two_accounts) == ["A", "B"]


def test_a_forced_row_is_stock_for_the_next_file_of_the_run_as_a_match(db_two_accounts, pending_factory):
    """Bestand „A“; Datei 1 erzwingt „B“; Datei 2 („A“, „B“) liest den Bestand mit der erzwungenen Zeile: Beide Umsätze treffen ihre Zeile und sind Duplikate, die erzwungene stammt aus diesem Lauf."""
    _seed(db_two_accounts, ["A"])
    files = [_file(["B"], name="eins.xml"), _file(["A", "B"], name="zwei.xml")]
    results = _run(db_two_accounts, pending_factory, files, force={"0": [0]})
    assert [(r["inserted"], r["skipped"], r["skipped_upload"], r.get("forced", 0)) for r in results] == [(1, 0, 0, 1), (0, 2, 1, 0)]
    assert _refs(db_two_accounts) == ["A", "B"]


def _texts(db):
    return [row[2] for row in _bank_bookings(db)]


def test_a_row_created_by_force_keeps_its_text_when_a_skipped_duplicate_of_the_file_has_a_longer_one(db_two_accounts, pending_factory):
    """Bestand „A“ und „C“ mit dem langen Text; die Datei hat „X“ (verdächtig, kurzer Text) und „A“ (Duplikat mit dem langen Text). Das Auffüllen des Textes eines Duplikats ändert nie eine Zeile, die
    derselbe Beleg angelegt hat: Die erzwungene Zeile „X“ ist ein Präfix des langen Textes, aber ihr Text bleibt."""
    long_text = "Miete Maerz Wohnung 3"
    _seed(db_two_accounts, ["A", "C"], text=long_text)
    file = _file(["X", "A"])
    file["transactions"][0]["reference"] = "Miete"
    file["transactions"][1]["reference"] = long_text
    (card,) = _preview(db_two_accounts, [file])
    assert [(t["index"], t["dup"], t["forceable"]) for t in card["transactions"]] == [(0, True, True), (1, True, False)]
    (result,) = _run(db_two_accounts, pending_factory, [file], force={"0": [0]})
    assert (result["inserted"], result["skipped"], result["forced"], result["text_updated"]) == (1, 1, 1, 0)
    assert _texts(db_two_accounts) == [long_text, long_text, "Miete"]


def test_a_row_forced_by_an_earlier_file_keeps_its_text_too(db_two_accounts, pending_factory):
    """Dasselbe über zwei Belege: Datei 0 erzwingt „X“ (kurzer Text), Datei 1 hat das Duplikat „A“ mit dem langen Text. Die erzwungene Zeile ist ein eigener Umsatz – ihr Text bleibt."""
    long_text = "Miete Maerz Wohnung 3"
    _seed(db_two_accounts, ["A", "C"], text=long_text)
    files = [_file(["X"], name="null.xml"), _file(["A"], name="eins.xml")]
    files[0]["transactions"][0]["reference"] = "Miete"
    files[1]["transactions"][0]["reference"] = long_text
    results = _run(db_two_accounts, pending_factory, files, force={"0": [0]})
    assert [(r["inserted"], r["skipped"], r["text_updated"]) for r in results] == [(1, 0, 0), (0, 1, 0)]
    assert _texts(db_two_accounts) == [long_text, long_text, "Miete"]


def test_the_text_of_a_row_the_file_created_still_makes_the_repair_of_an_older_row_ambiguous(db_two_accounts, pending_factory):
    """Ohne Referenzen: Bestand „Miete“; die Datei hat „Miete Maerz“ (Duplikat) und „Miete“ (neu, angelegt). Zwei Zeilen kommen als Ziel des längeren Textes in Frage – wie bisher wird nichts nachgetragen
    (mehrere gleiche Beträge am selben Tag wären mehrdeutig), und die angelegte Zeile wird nie überschrieben."""
    _seed(db_two_accounts, [""], text="Miete")
    file = _file(["", ""])
    file["transactions"][0]["reference"] = "Miete Maerz"
    file["transactions"][1]["reference"] = "Miete"
    (result,) = _run(db_two_accounts, pending_factory, [file])
    assert (result["inserted"], result["skipped"], result["text_updated"]) == (1, 1, 0)
    assert _texts(db_two_accounts) == ["Miete", "Miete"]


def test_an_older_row_with_a_cut_text_is_still_repaired_by_the_duplicate(db_two_accounts, pending_factory):
    _seed(db_two_accounts, [""], text="Miete")
    file = _file([""])
    file["transactions"][0]["reference"] = "Miete Maerz"
    (result,) = _run(db_two_accounts, pending_factory, [file])
    assert (result["inserted"], result["skipped"], result["text_updated"]) == (0, 1, 1)
    assert _texts(db_two_accounts) == ["Miete Maerz"]


def test_an_import_file_from_before_the_references_behaves_as_before(db_two_accounts, pending_factory):
    """Ohne den Schlüssel ``bank_ref`` (PDF, CSV, ein Import von früher): nur die Zählung, kein Verdacht, keine Zeile in ``BankTransactionDetails``."""
    _seed(db_two_accounts, ["A"])
    file = _file(["", ""])
    assert all("bank_ref" not in t for t in file["transactions"])
    (card,) = _preview(db_two_accounts, [file])
    assert [t["ref_differs"] for t in card["transactions"]] == [False, False] and (card["new_count"], card["dup_count"]) == (1, 1)
    (result,) = _run(db_two_accounts, pending_factory, [file])
    assert (result["inserted"], result["skipped"]) == (1, 1)
    with closing(db_two_accounts._get_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM BankTransactionDetails").fetchone()[0] == 1       # nur die Zeile aus dem Bestand


def test_the_reference_is_read_again_for_the_comparison_so_a_hostile_value_cannot_match(db_two_accounts):
    """Der Vergleich liest die Referenz eines Umsatzes wie jede andere durch ``clean_bank_text``: Ein Platzhalter oder ein zu langer Wert gilt als „keine Referenz“."""
    _seed(db_two_accounts, ["A"])
    file = _file([""])
    file["transactions"][0]["bank_ref"] = "NOTPROVIDED"
    assert _preview(db_two_accounts, [file])[0]["transactions"][0]["ref_differs"] is False
    file["transactions"][0]["bank_ref"] = "X" * 36
    assert _preview(db_two_accounts, [file])[0]["transactions"][0]["ref_differs"] is False
    file["transactions"][0]["bank_ref"] = 7
    assert _preview(db_two_accounts, [file])[0]["transactions"][0]["ref_differs"] is False


def test_the_stock_of_another_account_or_day_never_makes_a_row_suspect(db_two_accounts):
    _seed(db_two_accounts, ["A"], iban=IBAN_B)
    _seed(db_two_accounts, ["A"], date=DATE_OTHER)
    (card,) = _preview(db_two_accounts, [_file(["B"])])
    assert (card["new_count"], card["dup_count"]) == (1, 0) and not card["transactions"][0]["ref_differs"]


def test_a_file_without_an_account_adds_nothing_for_the_files_after_it(db_two_accounts, pending_factory):
    """Eine Datei zu einer IBAN ohne Konto wird nicht importiert und zählt deshalb nicht zu dem, was die folgenden Dateien finden – weder in der Vorschau noch im Import: Ihr Umsatz „B“ ist für die nächste Datei kein Bestand."""
    files = [_file(["B"], iban="DE00000000001000000001", name="fremd.xml"), _file(["C"], name="eigen.xml")]
    unknown, own = _preview(db_two_accounts, files)
    assert unknown["status"] == "error" and unknown["transactions"][0]["ref_differs"] is False
    assert (own["new_count"], own["dup_count"], own["dup_upload_count"], own["transactions"][0]["ref_differs"]) == (1, 0, 0, False)
    results = _run(db_two_accounts, pending_factory, files)
    assert [r["account_found"] for r in results] == [False, True] and (results[1]["inserted"], results[1]["skipped"]) == (1, 0)
    assert _refs(db_two_accounts) == ["C"]


# ── über HTTP: CAMT mit Bankreferenz ─────────────────────────────────────────

def _camt_with_references(entries):
    """CAMT.053 mit gebuchten Umsätzen ``(Betrag, CRDT|DBIT, Datum, Verwendungszweck, Bankreferenz)``; keine Salden."""
    body = "".join(
        f'<Ntry><Amt Ccy="EUR">{amount}</Amt><CdtDbtInd>{sign}</CdtDbtInd><Sts>BOOK</Sts><BookgDt><Dt>{date}</Dt></BookgDt>'
        f'<ValDt><Dt>{date}</Dt></ValDt><AcctSvcrRef>{ref}</AcctSvcrRef><NtryDtls><TxDtls><RltdPties><Dbtr><Nm>Beispiel Kunde GmbH</Nm></Dbtr></RltdPties>'
        f'<RmtInf><Ustrd>{text}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>' for amount, sign, date, text, ref in entries)
    return (f'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt><Id>S1</Id><CreDtTm>2026-03-31T08:00:00</CreDtTm>'
            f'<Acct><Id><IBAN>{OWN_IBAN}</IBAN></Id></Acct>{body}</Stmt></BkToCstmrStmt></Document>').encode("utf-8")


def _http_upload(port, login, name, content):
    content_type, body = _multipart([(name, content)])
    response = send(port, "POST", "/upload_receipts",
                    {"Origin": own_origin(port), "X-CSRF-Token": login.csrf, "Content-Type": content_type}, body)
    assert response.status == 200, response.text
    return json.loads(response.text)


def _http_confirm(port, login, import_id, **form):
    response = post(port, "/confirm_transactions", headers={"Origin": own_origin(port)}, form={"import_id": import_id, **form}, csrf=login.csrf)
    return response.status, json.loads(response.text)


def test_over_http_a_camt_file_with_another_reference_is_marked_and_can_be_forced(server_port, giro):
    first = _http_upload(server_port, giro, "eins.xml", _camt_with_references([("12.50", "DBIT", DATE, "Kaffee", "BANK-0001")]))
    assert _http_confirm(server_port, giro, first["import_id"])[1]["results"][0]["inserted"] == 1

    second = _http_upload(server_port, giro, "zwei.xml", _camt_with_references([("12.50", "DBIT", DATE, "Kaffee", "BANK-0002")]))
    (card,) = second["files"]
    (row,) = card["transactions"]
    assert (card["new_count"], card["dup_count"], card["status"]) == (0, 1, "warn")
    assert (row["dup"], row["dup_source"], row["importable"], row["ref_differs"], row["forceable"]) == (True, "db", False, True, True)

    status, data = _http_confirm(server_port, giro, second["import_id"])                      # ohne force: wie immer, nichts wird angelegt
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["skipped"]) == (0, 1)
    third = _http_upload(server_port, giro, "zwei.xml", _camt_with_references([("12.50", "DBIT", DATE, "Kaffee", "BANK-0002")]))
    assert _http_confirm(server_port, giro, third["import_id"], force="{\"0\": [1]}")[0] == 400
    status, data = _http_confirm(server_port, giro, third["import_id"], force="{\"0\": [0]}")
    assert status == 200 and (data["results"][0]["inserted"], data["results"][0]["forced"]) == (1, 1)
    account = match_account(giro.db.fetch_accounts(), OWN_IBAN)[0]
    assert [ref for _id, ref in giro.db.bank_stock(DATE, -12.5, account)] == ["BANK-0001", "BANK-0002"]
    details = giro.db.get_bank_details(giro.db.bank_stock(DATE, -12.5, account)[1][0])
    assert (details.value_date, details.bank_ref) == (DATE, "BANK-0002")


# ── die Seite: Kästchen „trotzdem importieren“ (Node) ────────────────────────

@pytest.fixture
def suspect_preview(books):
    """Karte 0: gemischt genutztes Konto A, Bestand „A“ und „D“, Datei „B“ (verdächtig, Position 0), „A“ (gewöhnliches Duplikat, 1), „C“ (neu, 2). Karte 1: reines Geschäftskonto B,
    Bestand „A“, Datei „B“ (verdächtig, 0) und „C“ (neu, 1). Karte 2: ein neuer Umsatz ohne jede Referenz, gar kein Verdacht."""
    _seed(books, ["A", "D"])
    _seed(books, ["A"], iban=IBAN_B)
    files = [_file(["B", "A", "C"], name="null.xml"), _file(["B", "C"], iban=IBAN_B, name="eins.xml"),
             _file([""], date=DATE_OTHER, name="zwei.xml")]
    return build_import_preview(books, {"files": files})["files"]


def _rows_of(card):
    """Die Tabellenzeilen der Einzelbuchungen einer Karte: (Kopfzeile, [Datenzeilen]) als Listen ihrer Zellen."""
    (table,) = [n for n in _walk(card) if n["tag"] == "table" and n["children"] and any(
        c["tag"] == "tr" and any(h["tag"] == "th" for h in c["children"]) for c in n["children"])]
    rows = [[cell for cell in row["children"]] for row in table["children"] if row["tag"] == "tr"]
    return rows[0], rows[1:]


@NO_NODE
def test_a_business_account_card_with_a_suspect_row_gets_its_box_and_an_open_list(tmp_path, books, suspect_preview):
    out = _render(tmp_path, books, suspect_preview)
    assert Tree(out["html"]).errors == []
    card = _cards(out)[1]
    (box,) = _by_class(card, "input", "forceBox")
    assert box["attrs"] == {"type": "checkbox", "class": "forceBox", "data-idx": "1", "data-i": "0"}        # nie vorab angehakt
    (label,) = [n for n in _walk(card) if n["tag"] == "label"]
    assert _text(label).strip() == "trotzdem importieren"
    button, listing = _toggle_and_list(card)
    assert _text(button) == "Alle Einzelbuchungen" and listing["attrs"] == {"id": "det-1"}                    # offen: ohne display:none
    assert not _by_class(card, "input", "selBox") and not _by_class(card, "input", "selSearch")
    (imp,) = _buttons(card, "btnImportBeleg")
    assert _text(imp) == "Importieren (1 neu)" and imp["attrs"]["data-new"] == "1"
    head, rows = _rows_of(card)
    assert [_text(c) for c in head] == ["Datum", "Empfänger", "Zweck", "Betrag", ""]
    assert [len(r) for r in rows] == [5, 5] and [bool(_by_class(r[-1], "input", "forceBox")) for r in rows] == [True, False]


@NO_NODE
def test_a_mixed_account_card_keeps_its_selection_and_adds_the_box_for_the_suspect_row_only(tmp_path, books, suspect_preview):
    card = _cards(_render(tmp_path, books, suspect_preview))[0]
    selection = _by_class(card, "input", "selBox")
    assert [("checked" in b["attrs"], "disabled" in b["attrs"]) for b in selection] == [(False, True), (False, True), (True, False)]
    (box,) = _by_class(card, "input", "forceBox")
    assert (box["attrs"]["data-idx"], box["attrs"]["data-i"]) == ("0", "0") and "checked" not in box["attrs"]
    head, rows = _rows_of(card)
    assert len(head) == 6 and [len(r) for r in rows] == [6, 6, 6]                                              # jede Zeile gleich breit
    assert [bool(_by_class(r[-1], "input", "forceBox")) for r in rows] == [True, False, False]
    (imp,) = _buttons(card, "btnImportBeleg")
    assert _text(imp) == "Importieren (1 ausgewählt)" and "data-new" not in imp["attrs"]


@NO_NODE
def test_a_card_without_a_suspect_row_looks_exactly_as_it_did_without_the_new_keys(tmp_path, books, suspect_preview):
    plain = suspect_preview[2]
    stripped = dict(plain, transactions=[{k: v for k, v in t.items() if k not in ("ref_differs", "forceable")} for t in plain["transactions"]],
                    problems=[])
    html = _render(tmp_path, books, [plain])["html"]
    assert html == _render(tmp_path, books, [stripped])["html"]
    assert "forceBox" not in html and "data-new" not in html and "trotzdem" not in html


def test_the_page_source_knows_the_label_the_box_and_the_field(tmp_db):
    script = _preview_source(tmp_db)
    assert "ref_differs: 'gleiches Datum und gleicher Betrag, aber andere Bankreferenz als die vorhandene Bewegung'" in script
    assert "vermutlich" not in script[script.index("var WARN_LABELS"):script.index("var FILE_WARN_LABELS")]
    for text in ("class=\"forceBox\"", " trotzdem importieren</label>", "body.append('force'", "function forceOf(", "function updateForce(",
                 "' trotzdem importiert)'"):
        assert text in script, text
    card = script[script.index("function renderCard("):script.index("document.addEventListener('click'")]
    new_markup = card[card.index("const fileNo"):card.index("h += '</table></div>'")]
    assert new_markup.count("style=") == 1                                                                     # kein neues Inline-Style


FORCE_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, scenarioFile] = process.argv;
const scenario = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));
const messages = [], calls = [], generic = {}, buttons = [], cards = {};
function element(id) {
  return generic[id] || (generic[id] = {id, innerHTML: '', style: {}, textContent: '', disabled: false, removed: false, dataset: {},
                                        remove() { this.removed = true; }});
}
for (const [idx, card] of Object.entries(scenario.cards)) {
  cards[idx] = {
    sel: (card.sel || []).map(r => ({checked: !!r.checked, disabled: !!r.disabled, dataset: {i: String(r.i), idx}})),
    force: (card.force || []).map(r => ({checked: !!r.checked, dataset: {i: String(r.i), idx}})),
  };
  const button = element('btnimp-' + idx);
  button.dataset = Object.assign({idx}, card.data || {});
  buttons.push(button);
}
function select(sel) {
  let m;
  if ((m = /^#det-(\d+) \.selBox$/.exec(sel))) return (cards[m[1]] || {sel: []}).sel;
  if ((m = /^#det-(\d+) \.forceBox$/.exec(sel))) return (cards[m[1]] || {force: []}).force;
  if (sel === '.btnImportBeleg' || sel === '.btnImportBeleg, .btnImportAll, .btnDismiss') return buttons;
  return [];
}
const sandbox = {
  document: {getElementById: element, querySelectorAll: select, addEventListener: () => {}},
  window: {location: {href: 'http://127.0.0.1/transactions'}},
  appMsg: (text, kind) => messages.push([text, kind]),
  fetch: (url, init) => {
    calls.push({url, body: init && init.body ? String(init.body) : ''});
    return Promise.resolve({json: () => Promise.resolve(scenario.response || {}), text: () => Promise.resolve('')});
  },
  DOMParser: class { parseFromString() { return {getElementById: () => null}; } },
  URLSearchParams, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);
(async () => {
  for (const step of [].concat(scenario.action)) {
    vm.runInContext(step, sandbox);
    await new Promise(resolve => setTimeout(resolve, 30));
  }
  console.log(JSON.stringify({messages, calls, elements: Object.fromEntries(
    Object.entries(generic).map(([id, e]) => [id, {text: e.textContent, disabled: e.disabled}]))}));
})();
"""


def _force_dom(tmp_path, db, cards, action, response=None):
    """Das Skript der Seite gegen ein kleines DOM: ``cards`` = ``{Karte: {sel: [{i, checked, disabled}], force: [{i, checked}], data: {…Attribute des Knopfes}}}``."""
    harness, script, data = (tmp_path / name for name in ("force_harness.js", "preview.js", "force_scenario.json"))
    harness.write_text(FORCE_HARNESS, encoding="utf-8")
    script.write_text(_preview_source(db), encoding="utf-8")
    data.write_text(json.dumps({"cards": cards, "action": action, "response": response or {}}), encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(script), str(data)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _sent(out):
    (call,) = [c for c in out["calls"] if c["url"] == "/confirm_transactions"]
    return {key: values[0] for key, values in parse_qs(call["body"]).items()}


def _ok(*results):
    return {"ok": True, "results": [dict({"file_index": n, "skipped": 0, "account_found": True}, **r) for n, r in enumerate(results)]}


@NO_NODE
def test_the_single_import_sends_force_for_the_ticked_boxes_of_its_card_only(tmp_path, books):
    cards = {"1": {"force": [{"i": 0, "checked": True}, {"i": 3, "checked": False}, {"i": 5, "checked": True}]}}
    out = _force_dom(tmp_path, books, cards, "currentImportId = 'abc'; importBeleg('1')", _ok({}, {"inserted": 3, "forced": 2}))
    sent = _sent(out)
    assert sent["import_id"] == "abc" and sent["file_index"] == "1" and json.loads(sent["force"]) == {"1": [0, 5]}
    assert "selection" not in sent


@NO_NODE
def test_without_a_ticked_box_no_force_field_is_sent(tmp_path, books):
    cards = {"1": {"force": [{"i": 0, "checked": False}]}, "2": {}}
    assert "force" not in _sent(_force_dom(tmp_path, books, cards, "currentImportId = 'abc'; importBeleg('1')", _ok({}, {"inserted": 1})))
    assert "force" not in _sent(_force_dom(tmp_path, books, cards, "currentImportId = 'abc'; importAll()", _ok({"inserted": 1})))


@NO_NODE
def test_all_cards_import_collects_the_ticked_boxes_of_every_card_next_to_the_selections(tmp_path, books):
    cards = {"0": {"sel": [{"i": 0, "disabled": True}, {"i": 2, "checked": True}], "force": [{"i": 0, "checked": True}]},
             "1": {"force": [{"i": 0, "checked": True}, {"i": 1, "checked": False}]}, "2": {"force": [{"i": 4, "checked": False}]}}
    out = _force_dom(tmp_path, books, cards, "currentImportId = 'abc'; importAll()", _ok({"inserted": 2, "forced": 1}, {"inserted": 1, "forced": 1}, {"inserted": 0}))
    sent = _sent(out)
    assert json.loads(sent["force"]) == {"0": [0], "1": [0]} and json.loads(sent["selection"]) == {"0": [2]} and "file_index" not in sent


@NO_NODE
def test_the_messages_name_the_forced_rows_only_when_there_are_some(tmp_path, books):
    single = _force_dom(tmp_path, books, {"0": {}}, "importBeleg('0')", _ok({"inserted": 2, "forced": 1, "skipped": 1}))
    assert single["messages"] == [["2 neu importiert (davon 1 trotzdem importiert), 1 Duplikate übersprungen", "success"]]
    many = _force_dom(tmp_path, books, {"0": {}}, "importAll()", _ok({"inserted": 1, "forced": 1}, {"inserted": 2, "forced": 1}))
    assert many["messages"] == [["3 neu importiert (davon 2 trotzdem importiert)", "success"]]
    quiet = _force_dom(tmp_path, books, {"0": {}}, "importAll()", _ok({"inserted": 3}))
    assert quiet["messages"] == [["3 neu importiert", "success"]]


@NO_NODE
def test_the_button_of_a_card_without_selection_counts_the_ticked_boxes(tmp_path, books):
    cards = {"1": {"force": [{"i": 0, "checked": True}, {"i": 2, "checked": True}, {"i": 4, "checked": False}], "data": {"new": "3"}}}
    assert _force_dom(tmp_path, books, cards, "updateForce('1')")["elements"]["btnimp-1"]["text"] == "Importieren (3 neu, 2 trotzdem)"
    cards["1"]["force"] = [{"i": 0, "checked": False}]
    assert _force_dom(tmp_path, books, cards, "updateForce('1')")["elements"]["btnimp-1"]["text"] == "Importieren (3 neu)"


@NO_NODE
def test_the_button_of_a_card_with_selection_counts_the_ticked_boxes_and_stays_usable_when_only_those_are_ticked(tmp_path, books):
    sel = [{"i": 0, "disabled": True}, {"i": 1, "checked": False}]
    cards = {"0": {"sel": sel, "force": [{"i": 0, "checked": True}]}}
    out = _force_dom(tmp_path, books, cards, "updateForce('0')")
    assert out["elements"]["btnimp-0"] == {"text": "Importieren (0 ausgewählt, 1 trotzdem)", "disabled": False}
    cards = {"0": {"sel": sel, "force": [{"i": 0, "checked": False}]}}
    out = _force_dom(tmp_path, books, cards, "updateForce('0')")
    assert out["elements"]["btnimp-0"] == {"text": "Importieren (0 ausgewählt)", "disabled": True}               # wie bisher
    assert out["elements"]["selcount-0"]["text"] == "0 von 1 ausgewählt"
