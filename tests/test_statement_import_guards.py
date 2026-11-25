# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Kontoauszug-Import liest nichts still falsch oder unvollständig ein (2.3.9).

* CAMT: Ein Auszug, der irgendwo eine andere Währung als Euro nennt, liefert keine Umsätze (sie wären als Euro
  gebucht worden), nur den Befund ``foreign_currency``; ein Einzelbetrag einer Sammelbuchung in anderer Währung
  ist kein eigener Euro-Betrag; fehlt ein Saldo, steht ``no_balance`` da.
* Volksbank-PDF: Das Jahr der Buchungszeilen kommt aus dem Kopf und wird am Erstellungsdatum des Auszugs
  geprüft (Dezember auf dem ersten Januar-Auszug); ein Datum, das es nicht gibt, ist ``None`` statt „heute“; eine
  Zeile ohne Betrag (``unparsed``), ein fehlendes Jahr (``no_year``) und eine Saldozeile im Verwendungszweck
  fallen nicht mehr still durch.
* PDF: Anfangs-/Schlusssaldo aus dem Text (``find_balances``) gegen die Summe der Umsätze (``check_balance``).
* Upload und Vorschau: Die Belegwarnungen eines PDF-Auszugs gehen nicht mehr verloren, ein erkannter Auszug ohne
  Umsätze wird nicht als „abgelegt“ gemeldet, die Oberfläche kennt die neuen Schlüssel.

Namen, Beträge und Daten sind erfundene Testwerte; PDFs entstehen mit dem PDF-Kern der App
(``export.pdf_core``), die IBANs sind die erlaubten Test-IBANs.
"""
import contextlib
import io
import json
import pathlib
import re
import sqlite3
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

import importers
from document_parser import DocumentParser
from export.pdf_core import build_multi_page_pdf, escape_pdf_string
from importers import pdftext, vbr
from importers.base import BankStatement, StatementTransaction
from importers.camt import parse_upload, parse_xml
from importers.dkb import DkbImporter
from importers.pdftext import find_balances
from importers.plausibility import (MAX_DAYS_AFTER_DOCUMENT, check_balance, check_statement,
                                    check_transaction)
from importers.vbr import VbrImporter, header_year, parse_text, statement_year
from money import to_minor
from server import handlers, upload_handler
from server.import_preview import build_import_preview
from tests.test_import_upload_http import NO_NODE, _node, _page_script, _preview_source

OWN_IBAN = "DE89370400440532013000"
OTHER_IBAN = "DE02120300000000202051"


@pytest.fixture
def giro(tmp_db):
    """Datenbank mit dem Bankkonto zur IBAN der Auszüge."""
    tmp_db.insert_account("Giro", "Owner", OWN_IBAN, "TESTDEFF", "Testbank", skr_account=1800)
    return tmp_db


# ══ CAMT: Fremdwährung, Einzelbeträge, fehlender Saldo ═══════════════════════════════════════════════════════════

def _amt(value, ccy):
    attribute = "" if ccy is None else f' Ccy="{ccy}"'
    return f"<Amt{attribute}>{value}</Amt>"


def _entry(amount, sign="DBIT", ccy="EUR", day="2026-03-05", details="", status="BOOK"):
    return (f"<Ntry>{_amt(amount, ccy)}<CdtDbtInd>{sign}</CdtDbtInd><Sts>{status}</Sts>"
            f"<BookgDt><Dt>{day}</Dt></BookgDt>{details}</Ntry>")


def _balance(code, balance):
    if balance is None:
        return ""
    value, ccy = balance
    return (f"<Bal><Tp><CdOrPrtry><Cd>{code}</Cd></CdOrPrtry></Tp>{_amt(value, ccy)}"
            f"<CdtDbtInd>CRDT</CdtDbtInd></Bal>")


def camt_xml(entries=None, *, iban=OWN_IBAN, acct_ccy=None, opening=("100.00", "EUR"),
             closing=("90.00", "EUR")):
    """Ein CAMT.053-Auszug mit einem ``Stmt``. Die Währung steht – je nach Aufruf – am Konto (``acct_ccy``), an den
    Salden (``(Betrag, Währung)``) und an den Umsätzen; ``None`` = die Angabe fehlt, ein Saldo ``None`` = der Saldo
    fehlt. Standard: ein Umsatz von 10,00 € im Soll, Saldo 100,00 → 90,00."""
    entries = [_entry("10.00")] if entries is None else entries
    account = "" if acct_ccy is None else f"<Ccy>{acct_ccy}</Ccy>"
    return (
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>'
        f"<CreDtTm>2026-03-31T08:00:00</CreDtTm><Acct><Id><IBAN>{iban}</IBAN></Id>{account}</Acct>"
        f'{_balance("OPBD", opening)}{_balance("CLBD", closing)}{"".join(entries)}'
        "</Stmt></BkToCstmrStmt></Document>").encode()


def read_camt(data):
    """Der einzige Auszug einer Datei, geprüft wie im Upload (``check_statement``)."""
    (statement,) = parse_xml(data)
    return check_statement(statement)


FOREIGN_PLACES = {
    "an-den-Umsaetzen": dict(entries=[_entry("10.00", ccy="USD")]),
    "nur-am-Konto": dict(acct_ccy="USD", entries=[_entry("10.00", ccy=None)],
                         opening=("100.00", None), closing=("90.00", None)),
    "nur-am-Anfangssaldo": dict(entries=[_entry("10.00", ccy=None)], opening=("100.00", "USD"),
                                closing=("90.00", None)),
    "nur-am-Schlusssaldo": dict(entries=[_entry("10.00", ccy=None)], opening=("100.00", None),
                                closing=("90.00", "USD")),
}


@pytest.mark.parametrize("place", FOREIGN_PLACES)
def test_a_statement_in_a_foreign_currency_yields_no_transactions(place):
    statement = read_camt(camt_xml(**FOREIGN_PLACES[place]))
    assert statement.transactions == []
    assert statement.warnings == ["foreign_currency"]           # weder no_transactions noch no_iban noch balance
    assert statement.currency == "USD"
    assert statement.iban == OWN_IBAN                            # IBAN und Belegdatum wie sonst
    assert statement.document_date == datetime(2026, 3, 31)


def test_a_foreign_statement_carries_no_other_finding():
    """Auch ein Auszug, der sonst Befunde hätte (Saldo, nicht gebuchter Umsatz, unlesbarer Umsatz), nennt nur den einen."""
    entries = [_entry("10.00", ccy="USD"), _entry("5.00", ccy="USD", status="PDNG"),
               "<Ntry><CdtDbtInd>DBIT</CdtDbtInd></Ntry>"]
    (statement,) = parse_xml(camt_xml(entries, opening=("100.00", "EUR"), closing=("1.00", "EUR")))
    assert statement.warnings == ["foreign_currency"] and statement.transactions == []


@pytest.mark.parametrize("ccy, code", [(" usd ", "USD"), ("chf", "CHF"), ("GBP", "GBP")])
def test_the_code_is_trimmed_and_upper_cased(ccy, code):
    statement = read_camt(camt_xml([_entry("10.00", ccy=ccy)]))
    assert statement.warnings == ["foreign_currency"] and statement.currency == code


@pytest.mark.parametrize("ccy", ["EURO", "€", "", "E1R", "EU", "12", "E U R"])
def test_a_value_that_is_no_currency_code_counts_as_foreign_without_a_code(ccy):
    statement = read_camt(camt_xml([_entry("10.00", ccy=ccy)]))
    assert statement.warnings == ["foreign_currency"]
    assert statement.transactions == [] and statement.currency is None


def test_the_first_deviating_code_is_named():
    """Reihenfolge: Konto, gebuchte Umsätze, Salden – ein unlesbarer Wert davor ergibt keinen Code."""
    entries = [_entry("10.00", ccy="EUR"), _entry("5.00", ccy="CHF")]
    assert read_camt(camt_xml(entries, closing=("90.00", "USD"))).currency == "CHF"
    assert read_camt(camt_xml(entries, acct_ccy="GBP")).currency == "GBP"
    assert read_camt(camt_xml([_entry("10.00", ccy="??")], closing=("90.00", "USD"))).currency is None
    assert read_camt(camt_xml([_entry("10.00", ccy="USD")], closing=("90.00", "??"))).currency == "USD"


def test_a_pending_entry_in_another_currency_does_not_count():
    """Nur gebuchte Umsätze zählen – ein vorgemerkter in anderer Währung macht den Auszug nicht zum Fremdwährungsauszug."""
    statement = read_camt(camt_xml([_entry("10.00"), _entry("999.99", ccy="USD", status="PDNG")]))
    assert [t.amount for t in statement.transactions] == [-10.0]
    assert "foreign_currency" not in statement.warnings and statement.currency == "EUR"


def test_a_balance_that_is_not_used_does_not_count():
    """Verwendet werden Anfangs- (OPBD/PRCD) und Schlusssaldo (CLBD); ein anderer Saldo in Fremdwährung stört nicht."""
    other = ('<Bal><Tp><CdOrPrtry><Cd>CLAV</Cd></CdOrPrtry></Tp><Amt Ccy="USD">5.00</Amt>'
             "<CdtDbtInd>CRDT</CdtDbtInd></Bal>")
    data = camt_xml().replace(b"<Ntry>", other.encode() + b"<Ntry>", 1)
    statement = read_camt(data)
    assert statement.warnings == [] and statement.currency == "EUR"


def test_without_any_currency_the_statement_is_read_as_before():
    statement = read_camt(camt_xml([_entry("10.00", ccy=None)], opening=("100.00", None), closing=("90.00", None)))
    assert [t.amount for t in statement.transactions] == [-10.0]
    assert statement.warnings == [] and statement.currency is None


@pytest.mark.parametrize("ccy", ["EUR", "eur", " Eur "])
def test_euro_in_any_spelling_is_euro(ccy):
    statement = read_camt(camt_xml([_entry("10.00", ccy=ccy)], acct_ccy="EUR",
                                   opening=("100.00", ccy), closing=("90.00", ccy)))
    assert [t.amount for t in statement.transactions] == [-10.0]
    assert statement.warnings == [] and statement.currency == "EUR"


def test_the_account_currency_alone_decides_when_nothing_else_names_one():
    foreign = read_camt(camt_xml([_entry("10.00", ccy=None)], acct_ccy="JPY",
                                 opening=("100.00", None), closing=("90.00", None)))
    assert foreign.warnings == ["foreign_currency"] and foreign.currency == "JPY"
    euro = read_camt(camt_xml([_entry("10.00", ccy=None)], acct_ccy="EUR",
                              opening=("100.00", None), closing=("90.00", None)))
    assert euro.warnings == [] and euro.currency == "EUR"


def test_the_currency_is_part_of_the_serialised_statement():
    assert read_camt(camt_xml()).as_dict()["currency"] == "EUR"
    assert read_camt(camt_xml([_entry("1.00", ccy="USD")])).as_dict()["currency"] == "USD"
    assert BankStatement(bank_code="VBR").as_dict()["currency"] is None


def test_a_euro_statement_without_entries_still_reports_no_transactions():
    statement = read_camt(camt_xml([], opening=("100.00", "EUR"), closing=("100.00", "EUR")))
    assert statement.warnings == ["no_transactions"]


# ── Sammelbuchung: Einzelbeträge in Fremdwährung ─────────────────────────────────────────────────────────────────

def _tx(amount_xml, text):
    return f"<TxDtls>{amount_xml}<RmtInf><Ustrd>{text}</Ustrd></RmtInf></TxDtls>"


def _split(*txs, total="50.00"):
    return [_entry(total, details="<NtryDtls>" + "".join(txs) + "</NtryDtls>")]


def _instructed(value, ccy="USD"):
    return f'<AmtDtls><InstdAmt><Amt Ccy="{ccy}">{value}</Amt></InstdAmt></AmtDtls>'


def _book(value, instructed=None):
    inner = f'<TxAmt><Amt Ccy="EUR">{value}</Amt></TxAmt>'
    if instructed:
        inner += f'<InstdAmt><Amt Ccy="USD">{instructed}</Amt></InstdAmt>'
    return f"<AmtDtls>{inner}</AmtDtls>"


SPLIT_BALANCES = dict(opening=("100.00", "EUR"), closing=("50.00", "EUR"))


def test_split_with_only_instructed_amounts_in_another_currency_stays_one_entry():
    entries = _split(_tx(_instructed("33.00"), "P1"), _tx(_instructed("22.00"), "P2"))
    statement = read_camt(camt_xml(entries, **SPLIT_BALANCES))
    assert [t.amount for t in statement.transactions] == [-50.0]       # der Gesamtbetrag in Euro
    assert statement.warnings == []                                    # kein split_mismatch


def test_amounts_under_amtdtls_never_split_an_entry():
    """Nur ``TxDtls/Amt`` teilt eine Sammelbuchung auf. Mit Beträgen allein unter ``AmtDtls`` (``TxAmt``, auch neben
    ``InstdAmt``) bleibt es EIN Umsatz mit dem Gesamtbetrag – so hat der Import solche Auszüge immer gelesen, und ein
    erneuter Import findet seine Summenzeile wieder (Duplikate laufen über Datum und Betrag)."""
    entries = _split(_tx(_book("30.00", "33.00"), "P1"), _tx(_book("20.00", "22.00"), "P2"))
    statement = read_camt(camt_xml(entries, **SPLIT_BALANCES))
    assert [t.amount for t in statement.transactions] == [-50.0]
    assert statement.warnings == []


def test_split_uses_amt_and_ignores_the_amounts_under_amtdtls():
    entries = _split(_tx('<Amt Ccy="EUR">30.00</Amt>' + _instructed("33.00"), "P1"),
                     _tx('<Amt Ccy="EUR">20.00</Amt>' + _book("99.00"), "P2"))
    statement = read_camt(camt_xml(entries, **SPLIT_BALANCES))
    assert [t.amount for t in statement.transactions] == [-30.0, -20.0]
    assert statement.warnings == []


def test_split_part_in_another_currency_makes_the_entry_one_booking():
    entries = _split(_tx('<Amt Ccy="USD">30.00</Amt>', "P1"), _tx('<Amt Ccy="EUR">20.00</Amt>', "P2"))
    statement = read_camt(camt_xml(entries, **SPLIT_BALANCES))
    assert [t.amount for t in statement.transactions] == [-50.0]
    assert "split_mismatch" not in statement.warnings


def test_split_with_wrong_euro_parts_still_warns():
    entries = _split(_tx('<Amt Ccy="EUR">30.00</Amt>', "P1"), _tx('<Amt Ccy="EUR">10.00</Amt>', "P2"))
    statement = read_camt(camt_xml(entries, **SPLIT_BALANCES))
    assert [t.amount for t in statement.transactions] == [-50.0]
    assert "split_mismatch" in statement.warnings


def test_split_part_without_a_currency_counts_as_euro():
    entries = _split(_tx("<Amt>30.00</Amt>", "P1"), _tx("<Amt>20.00</Amt>", "P2"))
    statement = read_camt(camt_xml(entries, **SPLIT_BALANCES))
    assert [t.amount for t in statement.transactions] == [-30.0, -20.0]


# ── fehlender Saldo ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("missing", [
    dict(opening=None), dict(closing=None), dict(opening=None, closing=None),
], ids=["Anfangssaldo", "Schlusssaldo", "beide"])
def test_a_missing_balance_is_reported_once(missing):
    statement = read_camt(camt_xml(**missing))
    assert statement.warnings == ["no_balance"]
    assert [t.amount for t in statement.transactions] == [-10.0]       # die Umsätze selbst bleiben


def test_an_unreadable_balance_is_a_balance_finding_not_a_missing_one():
    statement = read_camt(camt_xml(closing=("abc", "EUR")))
    assert statement.warnings == ["balance"]


def test_a_complete_matching_statement_has_no_finding():
    assert read_camt(camt_xml()).warnings == []


# ── Mischfall: ZIP mit Euro- und Dollar-Auszug ───────────────────────────────────────────────────────────────────

class _FakeRequest:
    """Minimaler Ersatz für den BaseHTTPRequestHandler im Test."""

    def __init__(self, content_type, body):
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)


def _multipart(files):
    boundary = "GRENZE" + uuid.uuid4().hex
    body = b""
    for name, content in files:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode() + content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


def upload(db, *files):
    """Dateien über den Upload-Handler hochladen; liefert die JSON-Antwort."""
    content_type, body = _multipart(files)
    status, text = upload_handler.handle_file_upload(_FakeRequest(content_type, body), db)
    assert status == 200, text
    return json.loads(text)


def _zip(*entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _bookings(db):
    with contextlib.closing(sqlite3.connect(db.db_name)) as connection:
        return connection.execute("SELECT DateBooking, Amount FROM Bookings WHERE BookingType = 'bank'"
                                  " ORDER BY ID").fetchall()


def test_in_a_mixed_zip_the_euro_statement_stays_importable(giro):
    archive = _zip(("usd.xml", camt_xml([_entry("10.00", ccy="USD")])),
                   ("eur.xml", camt_xml([_entry("12.50", day="2026-03-07", details="<AddtlNtryInf>Miete</AddtlNtryInf>")],
                                        closing=("87.50", "EUR"))))
    parsed = parse_upload("auszuege.zip", archive)
    assert [label for label, _ in parsed] == ["auszuege.zip/usd.xml", "auszuege.zip/eur.xml"]

    data = upload(giro, ("auszuege.zip", archive))
    assert data["other_files"] == []
    usd, eur = data["files"]
    assert (usd["status"], usd["warnings"], usd["currency"], usd["total"], usd["new_count"]) == (
        "warn", ["foreign_currency"], "USD", 0, 0)
    assert usd["account_name"] == "Giro"
    assert (eur["status"], eur["warnings"], eur["currency"], eur["new_count"]) == ("ok", [], "EUR", 1)

    status, body = handlers.handle_confirm_import(giro, {"import_id": [data["import_id"]]})
    results = json.loads(body)["results"]
    assert status == 200 and [r["inserted"] for r in results] == [0, 1]
    assert _bookings(giro) == [("2026-03-07", to_minor(-12.5))]       # nur der Euro-Auszug, Minor Units


# ══ Volksbank-PDF: Jahr und Datum ═════════════════════════════════════════════════════════════════════════════════

def _dates(text, year, latest=None):
    problems = []
    dates = [t["date"] for t in parse_text(text, year, latest=latest, problems=problems)]
    return dates, problems


DEC = "30.12. 30.12. Lastschrift 10,00 S\nMuster AG\n"
JAN = "02.01. 02.01. Gutschrift 5,00 H\nKunde GmbH\n"


def test_a_december_line_on_the_first_january_statement_belongs_to_the_year_before():
    """„Kontoauszug Nr. 1/2025“, erstellt am 03.01.2025: Der Dezember kann nicht 2025 sein."""
    assert _dates(DEC, 2025, datetime(2025, 1, 3)) == (["2024-12-30"], [])
    assert _dates(JAN, 2025, datetime(2025, 1, 3)) == (["2025-01-02"], [])
    both = parse_text(DEC + JAN, 2025, latest=datetime(2025, 1, 3))
    assert [t["date"] for t in both] == ["2024-12-30", "2025-01-02"]


def test_latest_may_be_a_date_as_well_as_a_datetime():
    assert _dates(DEC, 2025, date(2025, 1, 3))[0] == ["2024-12-30"]
    assert _dates(DEC, 2025, datetime(2025, 1, 3, 23, 59))[0] == ["2024-12-30"]


def test_a_december_statement_created_on_the_last_day_keeps_its_year():
    text = "15.12. 15.12. Lastschrift 1,00 S\nA\n31.12. 31.12. Lastschrift 2,00 S\nB\n"
    assert _dates(text, 2024, datetime(2024, 12, 31)) == (["2024-12-15", "2024-12-31"], [])


def test_the_comparison_is_by_day_without_tolerance():
    """Ein Tag nach dem Erstellungsdatum ist schon das Vorjahr, der Erstellungstag selbst nicht."""
    on_the_day = "03.01. 03.01. Lastschrift 1,00 S\nA\n"
    day_after = "04.01. 04.01. Lastschrift 1,00 S\nA\n"
    assert _dates(on_the_day, 2025, datetime(2025, 1, 3))[0] == ["2025-01-03"]
    assert _dates(day_after, 2025, datetime(2025, 1, 3))[0] == ["2024-01-04"]


def test_without_latest_the_year_applies_to_every_line():
    assert _dates(DEC + JAN, 2025)[0] == ["2025-12-30", "2025-01-02"]


def test_a_date_that_exists_only_in_the_year_before_takes_that_year():
    leap_day = "29.02. 29.02. Lastschrift 1,00 S\nA\n"
    assert _dates(leap_day, 2025, datetime(2025, 3, 5))[0] == ["2024-02-29"]


@pytest.mark.parametrize("year, latest", [
    pytest.param(2025, None, id="ohne-latest"),
    pytest.param(2023, datetime(2023, 3, 5), id="in-keinem-der-beiden-Jahre"),
    pytest.param(2025, datetime(2024, 2, 1), id="Vorjahr-liegt-nach-latest"),
])
def test_a_date_that_does_not_exist_is_none(year, latest):
    dates, _ = _dates("29.02. 29.02. Lastschrift 1,00 S\nA\n", year, latest)
    assert dates == [None]


@pytest.mark.parametrize("day_month", ["31.02.", "30.02.", "31.04.", "00.01.", "32.01.", "15.13."])
def test_an_impossible_date_is_none_never_today(day_month):
    text = f"{day_month} {day_month} Lastschrift 1,00 S\nA\n"
    transactions = parse_text(text, 2025, latest=datetime(2025, 6, 30))
    assert [t["date"] for t in transactions] == [None]
    assert [t["date"] for t in parse_text(text, 2025)] == [None]


def test_a_leap_day_stays_in_a_leap_year():
    assert _dates("29.02. 29.02. Lastschrift 1,00 S\nA\n", 2024, datetime(2024, 3, 5))[0] == ["2024-02-29"]


def test_a_line_without_an_amount_is_reported_exactly_once():
    text = ("01.12. 01.12. Lastschrift\nFoo\n"
            "02.12. 02.12. Lastschrift ohne Betrag\nBar\n"
            "03.12. 03.12. Gutschrift 2,00 H\nBaz\n"
            "04.12. 04.12. Gutschrift ohne Betrag\n")
    dates, problems = _dates(text, 2024)
    assert dates == ["2024-12-03"] and problems == ["unparsed"]


def test_a_clean_text_reports_nothing_and_problems_is_optional():
    problems = []
    parse_text(DEC + JAN, 2025, problems=problems)
    assert problems == []
    assert len(parse_text("01.12. 01.12. Lastschrift\nFoo\n", 2024)) == 0     # ohne problems: wie bisher, kein Fehler


@pytest.mark.parametrize("balance_line", [
    "neuer Kontostand vom 31.01.2025 1.234,56 H",
    "Neuer Kontostand vom 31.01.2025 1.234,56 H",
    "neuerKontostandvom31.01.20251.234,56H",
    "NEUER  KONTOSTAND  1.234,56 H",
    "alter Kontostand vom 01.01.2025 99,00 S",
])
def test_a_balance_line_does_not_end_up_in_the_reference_of_the_last_booking(balance_line):
    text = ("30.01. 30.01. Lastschrift 10,00 S\nMuster AG\nRechnung 4711\n" + balance_line + "\n")
    (transaction,) = parse_text(text, 2025)
    assert transaction["recipient"] == "Muster AG"
    assert transaction["reference"] == "Rechnung 4711"


def test_a_balance_line_between_the_details_is_skipped_and_the_rest_stays():
    text = ("30.01. 30.01. Lastschrift 10,00 S\nMuster AG\nneuer Kontostand vom 31.01.2025 1,00 H\nRechnung 4711\n")
    (transaction,) = parse_text(text, 2025)
    assert transaction["reference"] == "Rechnung 4711"


RULE = "\u2500" * 64


def test_the_rule_lines_around_the_closing_balance_are_not_part_of_the_last_booking():
    """Neuere Auszüge rahmen „neuer Kontostand“ mit zwei Strichzeilen ein – sie landeten im Verwendungszweck."""
    text = ("30.01. 30.01. Lastschrift 10,00 S\nMuster AG\nRechnung 4711\n"
            f"{RULE}\nneuer Kontostand vom 31.01.2025 1,00 H\n{RULE}\n")
    (transaction,) = parse_text(text, 2025)
    assert transaction["recipient"] == "Muster AG"
    assert transaction["reference"] == "Rechnung 4711"


@pytest.mark.parametrize("line", ["\u2500", "\u2501" * 10, "\u2500\u2550\u2500"])
def test_a_line_of_box_drawing_characters_only_is_skipped(line):
    (transaction,) = parse_text(f"30.01. 30.01. Lastschrift 10,00 S\nMuster AG\n{line}\nRechnung 4711\n", 2025)
    assert transaction["reference"] == "Rechnung 4711"


@pytest.mark.parametrize("line", ["-----", "\u2500 Rechnung \u2500", "_____", "====="])
def test_other_lines_stay_in_the_reference(line):
    """Nur reine Rahmenzeilen fallen weg – nichts, was ein Verwendungszweck sein könnte."""
    (transaction,) = parse_text(f"30.01. 30.01. Lastschrift 10,00 S\nMuster AG\n{line}\nRechnung 4711\n", 2025)
    assert line in transaction["reference"]


def test_header_year_and_statement_year():
    assert header_year("Kontoauszug Nr. 3/2024 Blatt 1") == 2024
    assert header_year("erstellt am 05.01.2023") == 2023
    assert header_year("Kontoauszug Nr. 3/2024 erstellt am 05.01.2023") == 2024      # der Kopf geht vor
    assert header_year("ohne Jahr") is None
    assert header_year("") is None
    assert statement_year("Kontoauszug Nr. 3/2024") == 2024
    assert statement_year("erstellt am 05.01.2023") == 2023
    assert statement_year("ohne Jahr") == datetime.now().year


# ══ Volksbank-PDF: der ganze Weg (echte PDFs und Ersatz für pdfplumber) ═══════════════════════════════════════════

def make_pdf(pages):
    """Ein PDF, dessen Seiten aus diesen Textzeilen bestehen (Liste von Listen)."""
    streams = []
    for lines in pages:
        ops = ["BT", "/F1 10 Tf"]
        for number, line in enumerate(lines):
            ops += [f"1 0 0 1 40 {800 - 14 * number} Tm", f"({escape_pdf_string(line)}) Tj"]
        ops.append("ET")
        streams.append(ops)
    return build_multi_page_pdf(streams)


def head(number="1/2025", created="03.01.2025"):
    """Briefkopf einer VBR-Seite bis zur Tabellenüberschrift."""
    return ["Volksbank Musterstadt eG", f"Kontoauszug Nr. {number}", f"erstellt am {created} 22:05 Blatt 1 von 1",
            "IBAN: DE89 3704 0044 0532 0130 00 BIC: GENODEF1XXX", "Bu-Tag Wert Vorgang"]


FOOT = ["0128", "000", "K00001234", "Bitte beachten Sie die Hinweise auf der Rueckseite"]
A_BOOKING = ["30.12. 30.12. Lastschrift PN:931 120,50 S", "Musterhaendler GmbH", "Rechnung 4711"]
B_BOOKING = ["02.01. 02.01. Gutschrift 300,00 H", "Beispiel Kunde GmbH", "Zahlung Rechnung 12"]
OPENING_LINE = "alter Kontostand vom 30.11.2024 1.000,00 H"
CLOSING_LINE = "neuer Kontostand vom 03.01.2025 1.179,50 H"          # 1.000,00 - 120,50 + 300,00


def statement_pdf(tmp_path, pages, name="auszug.pdf"):
    path = tmp_path / name
    path.write_bytes(make_pdf(pages))
    return str(path)


def one_page(*body, **head_args):
    return [head(**head_args) + list(body) + FOOT]


def test_a_real_statement_over_the_year_end_is_read_with_the_right_dates(tmp_path):
    pdf = statement_pdf(tmp_path, one_page(OPENING_LINE, *A_BOOKING, *B_BOOKING, CLOSING_LINE))
    statement = importers.parse_statement(pdf, "auszug.pdf")
    assert statement.iban == OWN_IBAN and statement.document_date == datetime(2025, 1, 3)
    assert [(t.date, t.amount) for t in statement.transactions] == [("2024-12-30", -120.5), ("2025-01-02", 300.0)]
    assert statement.warnings == []                                    # Saldo stimmt, nichts fehlt
    assert statement.transactions[1].reference == "Zahlung Rechnung 12"   # keine Saldozeile darin
    assert all("Kontostand" not in t.reference for t in statement.transactions)


def test_a_real_statement_with_the_closing_on_a_later_page(tmp_path):
    """Eine Buchung über den Seitenumbruch, Anfangssaldo auf Seite 1 und Schlusssaldo auf Seite 2."""
    page1 = head() + [OPENING_LINE, *A_BOOKING[:1]] + FOOT
    page2 = head() + [*A_BOOKING[1:], *B_BOOKING, CLOSING_LINE] + FOOT
    statement = importers.parse_statement(statement_pdf(tmp_path, [page1, page2]), "auszug.pdf")
    assert [(t.date, t.amount) for t in statement.transactions] == [("2024-12-30", -120.5), ("2025-01-02", 300.0)]
    assert statement.transactions[0].recipient == "Musterhaendler GmbH"
    assert statement.warnings == []


def test_a_real_statement_with_a_wrong_closing_balance_is_flagged(tmp_path):
    pdf = statement_pdf(tmp_path, one_page(OPENING_LINE, *A_BOOKING, *B_BOOKING,
                                           CLOSING_LINE.replace("1.179,50", "1.179,51")))
    assert importers.parse_statement(pdf, "auszug.pdf").warnings == ["balance"]


def test_a_real_statement_without_balances_is_flagged_as_unchecked(tmp_path):
    pdf = statement_pdf(tmp_path, one_page(*A_BOOKING, *B_BOOKING))
    assert importers.parse_statement(pdf, "auszug.pdf").warnings == ["no_balance"]


def test_a_booking_line_without_an_amount_is_flagged_and_breaks_the_sum(tmp_path):
    """Der Betrag ist in die Folgezeile gerutscht: Die Buchung fiele still weg – jetzt meldet es der Befund, und der
    Schlusssaldo (um die verlorene Buchung niedriger) stimmt nicht mehr."""
    lost = ["05.01. 05.01. Lastschrift Telefonanbieter", "50,00 S", "Verlorener Umsatz"]
    closing = CLOSING_LINE.replace("1.179,50", "1.129,50")
    pdf = statement_pdf(tmp_path, one_page(OPENING_LINE, *A_BOOKING, *B_BOOKING, *lost, closing))
    statement = importers.parse_statement(pdf, "auszug.pdf")
    assert len(statement.transactions) == 2
    assert statement.warnings == ["unparsed", "balance"]


def test_a_statement_without_the_year_in_the_head_is_flagged(tmp_path):
    head_without_year = ["Volksbank Musterstadt eG", "IBAN: DE89 3704 0044 0532 0130 00 BIC: GENODEF1XXX",
                         "Bu-Tag Wert Vorgang"]
    pdf = statement_pdf(tmp_path, [head_without_year + [OPENING_LINE, *A_BOOKING, CLOSING_LINE] + FOOT])
    statement = importers.parse_statement(pdf, "auszug.pdf")
    assert "no_year" in statement.warnings
    assert len(statement.transactions) == 1 and statement.document_date is None


def test_the_year_in_the_head_means_no_year_finding(tmp_path):
    pdf = statement_pdf(tmp_path, one_page(OPENING_LINE, *A_BOOKING, *B_BOOKING, CLOSING_LINE))
    assert "no_year" not in importers.parse_statement(pdf, "auszug.pdf").warnings


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePdf:
    def __init__(self, pages):
        self.pages = [_FakePage(text) for text in pages]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _page_text(*body, **head_args):
    return "\n".join(head(**head_args) + list(body) + FOOT)


def test_parse_with_the_pages_replaced_matches_the_text_based_result(monkeypatch):
    """Ohne echtes PDF: ``VbrImporter.parse`` liest die Seiten über ``pdfplumber.open`` und den Text aus dem Argument."""
    page = _page_text(OPENING_LINE, *A_BOOKING, *B_BOOKING, CLOSING_LINE)
    monkeypatch.setattr(vbr.pdfplumber, "open", lambda path: _FakePdf([page]))
    statement = VbrImporter().parse("auszug.pdf", page)
    assert [t.date for t in statement.transactions] == ["2024-12-30", "2025-01-02"]
    assert statement.warnings == []


def test_a_parse_error_stays_and_the_balance_is_checked_in_addition(monkeypatch, capsys):
    def broken(path):
        raise OSError("kaputt")

    monkeypatch.setattr(vbr.pdfplumber, "open", broken)
    page = _page_text(OPENING_LINE, *A_BOOKING, CLOSING_LINE)
    statement = VbrImporter().parse("auszug.pdf", page)
    assert statement.transactions == []
    assert statement.warnings == ["parse_error", "balance"]
    assert "kaputt" in capsys.readouterr().out                         # der volle Fehler steht im Server-Log


def test_each_finding_stands_once_even_if_two_causes_meet(monkeypatch):
    page = _page_text(OPENING_LINE, "05.01. 05.01. Lastschrift", "Verloren", "06.01. 06.01. Lastschrift", "Auch",
                      CLOSING_LINE)
    monkeypatch.setattr(vbr.pdfplumber, "open", lambda path: _FakePdf([page]))
    statement = VbrImporter().parse("auszug.pdf", page)
    assert statement.warnings == ["unparsed", "balance"]


# ══ DKB ═══════════════════════════════════════════════════════════════════════════════════════════════════════════

def _dkb_raw():
    return {"bank_code": "DKB", "iban": OWN_IBAN, "document_date": datetime(2026, 2, 4), "transactions": [
        {"date": "2026-01-10", "amount": -20.0, "recipient": "Muster AG", "reference": "Miete", "foreign_iban": ""},
        {"date": "2026-01-12", "amount": 50.5, "recipient": "Kunde GmbH", "reference": "Zahlung", "foreign_iban": ""},
    ]}


def test_dkb_checks_the_balances_found_in_the_text(monkeypatch):
    monkeypatch.setattr(DocumentParser, "parse_bank_statement_dkb", lambda self, filepath: _dkb_raw())
    text = "ALTER KONTOSTAND 100,00 +\nNEUER KONTOSTAND 130,50 +"
    assert DkbImporter().parse("auszug.pdf", text).warnings == []
    assert DkbImporter().parse("auszug.pdf", text.replace("130,50", "130,51")).warnings == ["balance"]
    assert DkbImporter().parse("auszug.pdf", "keine Salden im Text").warnings == ["no_balance"]


def test_dkb_reads_the_text_itself_when_none_is_passed(monkeypatch):
    monkeypatch.setattr(DocumentParser, "parse_bank_statement_dkb", lambda self, filepath: _dkb_raw())
    calls = []

    def fake_extract(filepath):
        calls.append(filepath)
        return "ALTER KONTOSTAND 100,00 +\nNEUER KONTOSTAND 130,50 +"

    monkeypatch.setattr(pdftext, "extract_text", fake_extract)
    assert DkbImporter().parse("auszug.pdf").warnings == [] and calls == ["auszug.pdf"]
    calls.clear()
    DkbImporter().parse("auszug.pdf", "ALTER KONTOSTAND 100,00 +\nNEUER KONTOSTAND 130,50 +")
    assert calls == []                                                 # ein übergebener Text wird nicht noch einmal gelesen


# ══ find_balances ═════════════════════════════════════════════════════════════════════════════════════════════════

D = Decimal


@pytest.mark.parametrize("text, expected", [
    pytest.param("alter Kontostand vom 30.11.2024 1.234,56 H\nneuer Kontostand vom 31.12.2024 2.000,00 H",
                 (D("1234.56"), D("2000.00")), id="mit-vom"),
    pytest.param("alter Kontostand 100,00 H\nneuer Kontostand 99,99 H", (D("100.00"), D("99.99")), id="ohne-vom"),
    pytest.param("alterKontostandvom30.11.20241.234,56H\nneuerKontostandvom31.12.2024999,00H",
                 (D("1234.56"), D("999.00")), id="Woerter-zusammengezogen"),
    pytest.param("ALTER KONTOSTAND 1.000,00 +\nNEUER KONTOSTAND 500,00 +", (D("1000.00"), D("500.00")),
                 id="Grossbuchstaben-Plus"),
    pytest.param("Alter Kontostand: 1.000,00 EUR\nNeuer Kontostand: 500,00 EUR", (D("1000.00"), D("500.00")),
                 id="Doppelpunkt-EUR"),
    pytest.param("alter Kontostand 10,00 S\nneuer Kontostand 20,00 S", (D("-10.00"), D("-20.00")), id="Soll-negativ"),
    pytest.param("alter Kontostand 10,00 H\nneuer Kontostand 20,00 H", (D("10.00"), D("20.00")), id="Haben-positiv"),
    pytest.param("alter Kontostand 10,00-\nneuer Kontostand 20,00 -", (D("-10.00"), D("-20.00")), id="Minuszeichen"),
    pytest.param("alter Kontostand 10,00+\nneuer Kontostand 20,00 +", (D("10.00"), D("20.00")), id="Pluszeichen"),
    pytest.param("alter Kontostand 10,00 EUR S\nneuer Kontostand 20,00 EUR H", (D("-10.00"), D("20.00")),
                 id="EUR-dann-S-H"),
    pytest.param("alter Kontostand 10,00 S EUR", (D("-10.00"), None), id="S-dann-EUR"),
    pytest.param("alter Kontostand 10,00", (D("10.00"), None), id="ohne-Zeichen-positiv"),
    pytest.param("alter Kontostand 1234,56 H", (D("1234.56"), None), id="ohne-Tausenderpunkt"),
    pytest.param("alter Kontostand 0,00 H\nneuer Kontostand 0,00 S", (D("0.00"), D("0.00")), id="null"),
    pytest.param("nichts davon", (None, None), id="nichts-gefunden"),
    pytest.param("", (None, None), id="leerer-Text"),
])
def test_find_balances(text, expected):
    assert find_balances(text) == expected


def test_the_first_opening_and_the_last_closing_balance_count():
    text = ("alter Kontostand 100,00 H\nBuchung\nneuer Kontostand 1,00 H\n"
            "alter Kontostand 5,00 H\nBuchung\nneuer Kontostand 7,77 S\n")
    assert find_balances(text) == (D("100.00"), D("-7.77"))


def test_balances_over_several_pages_joined_without_a_separator():
    """``pdftext.extract_text`` hängt die Seiten ohne Trennzeichen aneinander."""
    page1 = "Briefkopf\nalter Kontostand vom 30.11.2024 1.000,00 H\n01.12. 01.12. Lastschrift 1,00 S\nFuss"
    page2 = "Briefkopf\n02.12. 02.12. Lastschrift 2,00 S\nneuer Kontostand vom 03.12.2024 997,00 H\nFuss"
    assert find_balances(page1 + page2) == (D("1000.00"), D("997.00"))


def test_the_amount_must_stand_in_the_same_line():
    assert find_balances("alter Kontostand vom 30.11.2024\n1.000,00 H") == (None, None)
    assert find_balances("alter Kontostand\n\n12,00 H\nneuer Kontostand\n13,00 H") == (None, None)


@pytest.mark.parametrize("text", [
    "alter Kontostand 1.234,5 H",          # eine Nachkommastelle
    "alter Kontostand 1.234,567 H",        # drei Nachkommastellen
    "alter Kontostand 1.234 H",            # keine
    "alter Kontostand 12.34,56 H",         # kaputte Gruppierung
    "alter Kontostand 1,234.56 H",         # englische Schreibweise
    "alter Kontostand -1.234,56",          # Vorzeichen vor dem Betrag: nicht geraten
    "alter Kontostand 31.12.2024 1.234,56 H",   # Datum ohne „vom“
])
def test_a_spelling_outside_the_pattern_is_no_balance(text):
    assert find_balances(text) == (None, None)


@pytest.mark.parametrize("tail", ["Soll", "Haben", "Seite 2", "Euro", "S0ll"])
def test_a_word_instead_of_the_sign_leaves_the_sign_open(tail):
    """Statt S/H/+/-/EUR ein anderes Wort: das Vorzeichen bliebe geraten – die Stelle zählt nicht."""
    assert find_balances(f"alter Kontostand 100,00 {tail}") == (None, None)


def test_a_word_glued_to_the_amount_is_no_sign():
    assert find_balances("alter Kontostand 1.234,56Seite 2") == (None, None)


def test_a_sign_stands_even_when_more_text_follows():
    assert find_balances("alter Kontostand 100,00 H Seite 1 von 3") == (D("100.00"), None)
    assert find_balances("alter Kontostand 100,00 S Blatt 2") == (D("-100.00"), None)


@pytest.mark.parametrize("value", ["1.000.000.000.000,00", "9.999.999.999.999,99", "1000000000000,00"])
def test_an_amount_from_a_trillion_is_none(value):
    assert find_balances(f"alter Kontostand {value} H\nneuer Kontostand 10,00 H") == (None, D("10.00"))


def test_an_amount_just_below_the_limit_is_read():
    assert find_balances("alter Kontostand 999.999.999.999,99 H") == (D("999999999999.99"), None)


def test_balances_are_decimals_and_exact():
    opening, closing = find_balances("alter Kontostand 0,10 H\nneuer Kontostand 0,30 H")
    assert isinstance(opening, Decimal) and opening + opening + opening == closing     # 0,1 + 0,1 + 0,1 = 0,3


def test_the_first_opening_wins_even_if_it_is_unreadable():
    """Der erste Treffer entscheidet – ein späterer rückt nicht nach."""
    assert find_balances("alter Kontostand 1.000.000.000.000,00 H\nalter Kontostand 5,00 H") == (None, None)


HOSTILE_TEXTS = {
    "Leerraum-nach-dem-Wort": lambda: "alter Kontostand" + " " * 200_000 + "x",
    "Leerraum-zwischen-allen-Teilen": lambda: ("alter" + " " * 200_000 + "kontostand" + " " * 200_000 + ":"
                                               + " " * 200_000 + "vom" + " " * 200_000 + "01.01.2025"),
    "Ziffernfolge": lambda: "alter Kontostand " + "1" * 200_000,
    "Tausendergruppen": lambda: "alter Kontostand 1" + ".234" * 50_000,
    "Leerraum-nach-dem-Betrag": lambda: "alter Kontostand 1,00" + " " * 200_000 + "x" * 200_000,
}


@pytest.mark.parametrize("name", HOSTILE_TEXTS)
def test_a_crafted_text_does_not_keep_the_server_busy(name):
    """Der Text kommt aus einem hochgeladenen PDF. Eine lange Folge von Leerzeichen oder Ziffern darf die Suche nicht
    quadratisch verlangsamen (mit aneinandergereihten ``[^\\S\\n]*`` brauchte schon eine Folge von 16 000 Zeichen Sekunden)."""
    text = HOSTILE_TEXTS[name]()
    started = time.perf_counter()
    find_balances(text)
    assert time.perf_counter() - started < 5


# ══ check_balance ═════════════════════════════════════════════════════════════════════════════════════════════════

def _statement(*amounts):
    return BankStatement(bank_code="VBR", iban=OWN_IBAN, transactions=[
        StatementTransaction(date="2026-03-01", amount=amount, recipient="Muster AG", reference="Rechnung")
        for amount in amounts])


def test_a_matching_balance_has_no_finding():
    statement = check_balance(_statement(-10.0, 5.5), D("100.00"), D("95.50"))
    assert statement.warnings == []


def test_one_cent_off_is_a_balance_finding():
    assert check_balance(_statement(-10.0, 5.5), D("100.00"), D("95.51")).warnings == ["balance"]
    assert check_balance(_statement(-10.0, 5.5), D("100.00"), D("95.49")).warnings == ["balance"]


def test_a_statement_without_transactions_matches_equal_balances():
    assert check_balance(_statement(), D("10.00"), D("10.00")).warnings == []
    assert check_balance(_statement(), D("10.00"), D("11.00")).warnings == ["balance"]


@pytest.mark.parametrize("opening, closing", [
    (None, D("1.00")), (D("1.00"), None), (None, None),
], ids=["Anfangssaldo", "Schlusssaldo", "beide"])
def test_a_missing_balance_is_no_balance(opening, closing):
    assert check_balance(_statement(1.0), opening, closing).warnings == ["no_balance"]


def test_a_transaction_without_an_amount_makes_the_check_impossible():
    assert check_balance(_statement(-10.0, None), D("100.00"), D("90.00")).warnings == ["no_balance"]


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), 1e300])
def test_an_amount_that_is_no_usable_number_makes_the_check_impossible(amount):
    assert check_balance(_statement(1.0, amount), D("1.00"), D("2.00")).warnings == ["no_balance"]


def test_a_finding_stands_once_and_other_warnings_stay():
    statement = _statement(1.0)
    statement.warnings.append("parse_error")
    check_balance(statement, D("1.00"), D("5.00"))
    check_balance(statement, D("1.00"), D("5.00"))
    assert statement.warnings == ["parse_error", "balance"]
    check_balance(statement, None, None)
    assert statement.warnings == ["parse_error", "balance", "no_balance"]


def test_the_check_returns_the_statement():
    statement = _statement(1.0)
    assert check_balance(statement, D("1.00"), D("2.00")) is statement


def test_many_odd_amounts_add_up_exactly():
    """Cent-Beträge, die als float nicht aufgehen (0,1 + 0,2 ≠ 0,3): Gerechnet wird in ganzen Cent."""
    amounts = [(-1) ** n * (n % 97 + 1) / 100 for n in range(1, 1501)]
    exact = sum((D(str(a)) for a in amounts), D(0))
    opening = D("1000.00")
    assert check_balance(_statement(*amounts), opening, opening + exact).warnings == []
    assert check_balance(_statement(*amounts), opening, opening + exact + D("0.01")).warnings == ["balance"]
    assert check_balance(_statement(*amounts), opening, opening + exact - D("0.01")).warnings == ["balance"]


def test_float_noise_does_not_flag_a_correct_statement():
    assert 0.1 + 0.2 != 0.3                                            # der klassische Fall
    assert check_balance(_statement(0.1, 0.2), D("0.00"), D("0.30")).warnings == []


def test_amounts_are_rounded_to_the_cent_half_up():
    assert check_balance(_statement(0.005), D("0.00"), D("0.01")).warnings == []


# ══ Plausibilität: Datum nach dem Belegdatum, Beleg ohne Umsätze ═════════════════════════════════════════════════

DOCUMENT_DAY = datetime(2026, 3, 31)


def _transaction(day):
    return StatementTransaction(date=day.strftime("%Y-%m-%d"), amount=-5.0, recipient="Muster AG", reference="x")


def test_forty_days_after_the_document_date_is_flagged_twenty_are_not():
    assert "daterange" in check_transaction(_transaction(DOCUMENT_DAY + timedelta(days=40)), DOCUMENT_DAY)
    assert "daterange" not in check_transaction(_transaction(DOCUMENT_DAY + timedelta(days=20)), DOCUMENT_DAY)


def test_the_limit_after_the_document_date_is_31_days():
    assert MAX_DAYS_AFTER_DOCUMENT == 31
    assert "daterange" not in check_transaction(_transaction(DOCUMENT_DAY + timedelta(days=31)), DOCUMENT_DAY)
    assert "daterange" in check_transaction(_transaction(DOCUMENT_DAY + timedelta(days=32)), DOCUMENT_DAY)


def test_the_limit_before_the_document_date_stays_400_days():
    assert "daterange" not in check_transaction(_transaction(DOCUMENT_DAY - timedelta(days=400)), DOCUMENT_DAY)
    assert "daterange" in check_transaction(_transaction(DOCUMENT_DAY - timedelta(days=401)), DOCUMENT_DAY)


def test_a_december_booking_a_year_late_is_flagged():
    """Der Jahresdreher, um den es geht: Dezember 2025 auf einem Auszug vom 3. Januar 2025."""
    late = StatementTransaction(date="2025-12-30", amount=-1.0, recipient="Muster AG", reference="x")
    assert "daterange" in check_transaction(late, datetime(2025, 1, 3))


def test_a_text_as_document_date_checks_nothing():
    """Aus einer abgelegten Import-Datei kommt das Belegdatum als Text: Die Vorschau prüft dann nichts nach."""
    far = _transaction(DOCUMENT_DAY + timedelta(days=400))
    assert "daterange" not in check_transaction(far, "2026-03-31")


def test_a_foreign_currency_statement_is_not_reported_as_empty():
    statement = BankStatement(bank_code="CAMT", iban=OWN_IBAN, warnings=["foreign_currency"], currency="USD")
    assert check_statement(statement).warnings == ["foreign_currency"]
    empty = BankStatement(bank_code="CAMT", iban=OWN_IBAN)
    assert check_statement(empty).warnings == ["no_transactions"]


# ══ Upload und Vorschau ═══════════════════════════════════════════════════════════════════════════════════════════

def test_the_warnings_of_a_pdf_statement_reach_the_preview(giro, tmp_path):
    pdf = make_pdf(one_page(OPENING_LINE, *A_BOOKING, *B_BOOKING, CLOSING_LINE.replace("1.179,50", "1.179,51")))
    data = upload(giro, ("auszug_mit_abweichung.pdf", pdf))
    assert data["other_files"] == [] and data["import_id"]
    (card,) = data["files"]
    assert card["warnings"] == ["balance"]
    assert card["status"] == "warn" and card["total"] == 2 and card["new_count"] == 2
    assert card["currency"] is None


def test_a_clean_pdf_statement_stays_ok(giro):
    pdf = make_pdf(one_page(OPENING_LINE, *A_BOOKING, *B_BOOKING, CLOSING_LINE))
    (card,) = upload(giro, ("auszug.pdf", pdf))["files"]
    assert card["warnings"] == [] and card["status"] == "ok"
    assert [t["date"] for t in card["transactions"]] == ["2024-12-30", "2025-01-02"]


def test_a_pdf_statement_without_transactions_is_not_reported_as_filed(giro):
    pdf = make_pdf(one_page(OPENING_LINE, CLOSING_LINE))
    data = upload(giro, ("leer.pdf", pdf))
    assert data["files"] == [] and data["import_id"] is None
    (entry,) = data["other_files"]
    assert entry["filename"] == "leer.pdf" and entry["status"] == "warning"
    assert entry["error"] == "Kontoauszug erkannt, aber keine Umsätze gelesen."
    assert entry["path"].endswith("leer.pdf") and "Konten" in entry["path"].replace("\\", "/")   # abgelegt wie bisher


def test_a_pdf_that_is_no_statement_is_still_filed(giro):
    pdf = make_pdf([["Mietvertrag", "ueber Raeume im Erdgeschoss", "Seite 1"]])
    data = upload(giro, ("vertrag.pdf", pdf))
    assert data["files"] == []
    (entry,) = data["other_files"]
    assert entry["status"] == "organized" and entry["path"].endswith("vertrag.pdf")
    assert "error" not in entry


def test_a_foreign_currency_statement_shows_up_with_its_finding_in_the_upload(giro):
    data = upload(giro, ("dollar.xml", camt_xml([_entry("10.00", ccy="USD")])))
    (card,) = data["files"]
    assert card["warnings"] == ["foreign_currency"] and card["currency"] == "USD"
    assert card["total"] == 0 and card["new_count"] == 0 and card["status"] == "warn"


class FakeDb:
    """Genug Datenbank für ``build_import_preview``: Konten nach (ID, Name, …, IBAN), nie ein Duplikat im Bestand."""

    def __init__(self, *ibans):
        self.accounts = [(number, "Giro", "", iban) for number, iban in enumerate(ibans, 1)]

    def fetch_accounts(self):
        return self.accounts

    def bank_stock(self, *args):
        return []

    def account_allows_selection(self, account_id):
        # seit 2.5.0 fragt die Vorschau je Beleg, ob das Konto eine Auswahl einzelner Umsätze erlaubt (hier: nie)
        return False


def test_the_preview_passes_warnings_and_currency_on_and_stays_warn():
    giro_iban = {"iban": OWN_IBAN, "filename": "x.xml", "bank_code": "CAMT", "document_date": "2026-03-31"}
    clean = {"date": "2026-03-05", "amount": -10.0, "recipient": "Muster AG", "reference": "Rechnung", "warnings": []}
    other = dict(clean, date="2026-03-06", amount=-20.0)
    files = {"files": [dict(giro_iban, currency="USD", warnings=["foreign_currency"], transactions=[]),
                       dict(giro_iban, currency="EUR", warnings=["no_balance"], transactions=[clean]),
                       dict(giro_iban, transactions=[other])]}
    usd, euro, plain = build_import_preview(FakeDb(OWN_IBAN), files)["files"]
    assert (usd["currency"], usd["warnings"], usd["status"]) == ("USD", ["foreign_currency"], "warn")
    assert (euro["currency"], euro["warnings"], euro["status"]) == ("EUR", ["no_balance"], "warn")
    assert (plain["currency"], plain["warnings"], plain["status"]) == (None, [], "ok")      # alte Importdatei: ohne beides


def test_a_file_without_a_known_account_stays_an_error_with_its_findings():
    file = {"iban": OTHER_IBAN, "filename": "x.xml", "currency": "USD", "warnings": ["foreign_currency"],
            "transactions": []}
    (card,) = build_import_preview(FakeDb(), {"files": [file]})["files"]
    assert card["status"] == "error" and card["warnings"] == ["foreign_currency"]


def test_a_booking_without_a_date_is_reported_by_the_check_and_is_incomplete_in_the_preview():
    """Ein Datum, das es nicht gibt (31.02.), ist ``None`` – die Prüfung meldet ``date``, die Vorschau ``incomplete``
    (nicht neu, nicht importiert), statt dass die Buchung auf „heute“ landet."""
    (raw,) = parse_text("31.02. 31.02. Lastschrift 10,00 S\nMuster AG\nRechnung\n", 2025, latest=datetime(2025, 6, 30))
    statement = BankStatement(bank_code="VBR", iban=OWN_IBAN, document_date=datetime(2025, 6, 30),
                              transactions=[StatementTransaction.from_dict(raw)])
    check_statement(statement)
    assert statement.transactions[0].date is None and "date" in statement.transactions[0].warnings
    (card,) = build_import_preview(FakeDb(OWN_IBAN), {"files": [dict(statement.as_dict(), filename="x.pdf")]})["files"]
    assert card["new_count"] == 0 and card["status"] == "warn"
    assert "incomplete" in card["transactions"][0]["warn"] and "date" in card["transactions"][0]["warn"]


# ── die Oberfläche kennt die neuen Schlüssel ─────────────────────────────────────────────────────────────────────

FILE_LABELS = {
    "foreign_currency": "Der Auszug lautet nicht auf Euro – seine Umsätze werden nicht importiert.",
    "no_balance": "Anfangs- oder Schlusssaldo nicht gefunden – die Vollständigkeit des Auszugs ist nicht geprüft.",
    "unparsed": "Mindestens eine Zeile des Auszugs ließ sich nicht lesen – Umsätze können fehlen.",
    "no_year": "Das Jahr des Auszugs wurde nicht erkannt – bitte die Buchungsdaten prüfen.",
    "parse_error": "Der Auszug ließ sich nicht vollständig lesen – Umsätze können fehlen.",
}


def _file_labels(db):
    script = _page_script(db)
    block = script[script.index("var FILE_WARN_LABELS = {"):]
    return block[:block.index("};")]


@pytest.mark.parametrize("key", FILE_LABELS)
def test_the_page_names_every_new_finding(tmp_db, key):
    assert f"{key}: '{FILE_LABELS[key]}'" in _file_labels(tmp_db)


def test_the_page_still_names_the_old_findings(tmp_db):
    labels = _file_labels(tmp_db)
    for key in ("balance", "split_mismatch", "skipped_status", "bad_entry", "no_iban", "no_transactions"):
        assert f"{key}: '" in labels


def test_every_finding_a_module_sets_has_a_label_and_every_label_is_set_somewhere(tmp_db):
    """Jeder Schlüssel, den ein Importmodul an den Beleg hängt, steht in der Oberfläche – sonst erschiene der Schlüssel
    selbst –, und kein Text dort gehört zu einem Schlüssel, den kein Modul mehr setzt."""
    labels = _file_labels(tmp_db)
    sources = "".join(path.read_text(encoding="utf-8")
                      for path in (pathlib.Path(importers.__file__).parent).glob("*.py"))
    keys = ["balance", "no_balance", "split_mismatch", "skipped_status", "bad_entry", "foreign_currency",
            "no_iban", "no_transactions", "parse_error", "unparsed", "no_year"]
    for key in keys:
        assert f"{key}: '" in labels, key
        assert f"'{key}'" in sources, key
    assert sorted(re.findall(r"^\s+(\w+): '", labels, re.M)) == sorted(keys)


@NO_NODE
def test_an_unknown_finding_is_shown_as_escaped_text(tmp_path, tmp_db):
    """Ein Schlüssel ohne Text erscheint als Schlüssel selbst – maskiert, nie als HTML."""
    card = {"file_index": 0, "filename": "x.xml", "bank_code": "CAMT", "iban": OWN_IBAN, "document_date": None,
            "account_id": 1, "account_name": "Giro", "total": 0, "new_count": 0, "dup_count": 0,
            "dup_upload_count": 0, "dup_upload_differs_count": 0, "status": "warn",
            "warnings": ["foreign_currency", "brand_new_key", '<img src=x onerror=alert(1)>'],
            "problems": [], "transactions": []}
    out = _node(tmp_path, _preview_source(tmp_db), {"preview": {"files": [card], "other_files": []}})
    html = out["html"]
    assert FILE_LABELS["foreign_currency"] in html
    assert "brand_new_key" in html
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html


@NO_NODE
def test_an_upload_warning_for_an_empty_statement_is_shown_with_its_text(tmp_path, tmp_db):
    entry = {"filename": "leer.pdf", "status": "warning", "error": "Kontoauszug erkannt, aber keine Umsätze gelesen.",
             "path": "x/leer.pdf"}
    out = _node(tmp_path, _preview_source(tmp_db), {"preview": {"files": [], "other_files": [entry]}})
    assert "leer.pdf: Kontoauszug erkannt, aber keine Umsätze gelesen." in out["html"]
    assert "abgelegt" not in out["html"]


# ══ Aufrufer: der alte Weg über DocumentParser bleibt lesbar ═════════════════════════════════════════════════════

def test_the_old_parser_entry_point_still_returns_the_dict_shape(tmp_path):
    pdf = statement_pdf(tmp_path, one_page(OPENING_LINE, *A_BOOKING, *B_BOOKING, CLOSING_LINE))
    result = DocumentParser(data_dir=str(tmp_path / "Belege"), log_dir=str(tmp_path)).parse_bank_statement_vbr(pdf)
    assert set(result) >= {"bank_code", "iban", "document_date", "transactions"}
    assert "warnings" not in result
    assert [t["date"] for t in result["transactions"]] == ["2024-12-30", "2025-01-02"]
