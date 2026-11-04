# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kontoauszug-Import im Format CAMT.053 (importers/camt.py)."""
import io
import json
import zipfile
from pathlib import Path

import pytest

from importers import camt, check_statement
from importers.camt import CamtError, parse_upload, parse_xml
from server import handlers
from server.import_preview import build_import_preview

FIX = Path(__file__).parent / "fixtures" / "camt"
OWN_IBAN = "DE89370400440532013000"


def _read(name):
    return (FIX / name).read_bytes()


def _statements(name):
    return [check_statement(s) for s in parse_xml(_read(name))]


def test_v02_maps_entries_and_skips_pending():
    (st,) = _statements("camt053_v02.xml")
    assert st.bank_code == "CAMT" and st.iban == OWN_IBAN
    assert [(t.date, t.amount) for t in st.transactions] == [
        ("2026-03-05", 119.0), ("2026-03-10", -23.45)]
    credit, debit = st.transactions
    assert credit.recipient == "Beispiel Kunde GmbH"          # Dbtr bei Gutschrift
    assert credit.foreign_iban == "DE02200505501015871393"
    assert credit.reference == "Rechnung R26001 vom 01.03.2026"  # alle Ustrd
    assert debit.recipient == "Lieferant Muster AG"           # Cdtr bei Lastschrift
    assert debit.foreign_iban == "DE12500105170648489890"
    assert "skipped_status" in st.warnings
    assert "balance" not in st.warnings


def test_v04_falls_back_to_additional_entry_info():
    (st,) = _statements("camt053_v04.xml")
    (t,) = st.transactions
    assert t.amount == -30.0 and t.reference == "Kontofuehrungsgebuehr"
    assert "balance" not in st.warnings


def test_v08_split_booking_and_reversal():
    (st,) = _statements("camt053_v08.xml")
    amounts = [t.amount for t in st.transactions]
    assert amounts == [-30.0, -20.5, 25.0]                    # Summe -50,50 stimmt
    assert st.transactions[0].reference == "Sammel Position 1"
    assert st.transactions[2].reference.startswith("Storno: ")
    assert st.transactions[0].date == "2026-03-15"            # aus DtTm
    assert "balance" not in st.warnings and "split_mismatch" not in st.warnings


def test_split_with_wrong_parts_stays_one_entry_with_warning():
    (st,) = _statements("camt053_split_mismatch.xml")
    assert [t.amount for t in st.transactions] == [-50.0]
    assert "split_mismatch" in st.warnings


def test_multiple_statements_in_one_file():
    sts = _statements("camt053_multi.xml")
    assert [s.iban for s in sts] == [OWN_IBAN, "DE02200505501015871393"]


def test_balance_mismatch_is_flagged():
    (st,) = _statements("camt053_balance_mismatch.xml")
    assert "balance" in st.warnings


def test_dtd_and_entities_are_rejected():
    with pytest.raises(CamtError):
        parse_xml(_read("camt053_dtd.xml"))


def test_not_camt_is_rejected():
    with pytest.raises(CamtError):
        parse_xml(b"<root/>")
    with pytest.raises(CamtError):
        parse_xml(b"kein xml")


def test_amounts_are_exact_decimals():
    xml = _read("camt053_v04.xml").replace(b"30.00", b"0.10").replace(b"470.00", b"499.90")
    (st,) = parse_xml(xml)
    assert st.transactions[0].amount == -0.1 and "balance" not in st.warnings


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def test_zip_with_several_xml_files():
    data = _zip([("a.xml", _read("camt053_v02.xml")),
                 ("sub/b.xml", _read("camt053_multi.xml")),
                 ("readme.txt", b"ignoriert")])
    parsed = parse_upload("auszuege.zip", data)
    assert len(parsed) == 3
    assert parsed[0][0] == "auszuege.zip/a.xml"
    assert parsed[1][0] == "auszuege.zip/sub/b.xml (Auszug 1)"


def test_zip_limits(monkeypatch):
    with pytest.raises(CamtError):
        parse_upload("x.zip", _zip([("a.txt", b"nur text")]))
    with pytest.raises(CamtError):
        parse_upload("x.zip", b"kein zip")
    # zu viele Einträge
    monkeypatch.setattr(camt, "MAX_ZIP_ENTRIES", 2)
    many = _zip([(f"{i}.xml", b"<a/>") for i in range(3)])
    with pytest.raises(CamtError):
        parse_upload("x.zip", many)
    # Zip-Bomb: hohes Kompressionsverhältnis
    monkeypatch.setattr(camt, "MAX_ZIP_ENTRIES", 50)
    bomb = _zip([("b.xml", b"<a>" + b"0" * 5_000_000 + b"</a>")])
    with pytest.raises(CamtError):
        parse_upload("x.zip", bomb)
    # absolute Größengrenze
    monkeypatch.setattr(camt, "MAX_XML_BYTES", 100)
    with pytest.raises(CamtError):
        parse_upload("x.zip", _zip([("c.xml", _read("camt053_v02.xml"))]))


def test_zip_is_never_extracted_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parse_upload("x.zip", _zip([("../evil.xml", _read("camt053_v04.xml"))]))
    assert list(tmp_path.iterdir()) == []


# ── Einbindung in Vorschau und Import ────────────────────────────────────────

def test_preview_and_duplicate_detection_on_reimport(tmp_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tmp_db.insert_account("Giro", "Owner", OWN_IBAN, "TESTDEFF", "Testbank", skr_account=1200)

    def files():
        out = []
        for label, st in parse_upload("v02.xml", _read("camt053_v02.xml")):
            d = check_statement(st).as_dict()
            d["filename"] = label
            out.append(d)
        return {"files": out}

    data = files()
    pv = build_import_preview(tmp_db, data)["files"][0]
    assert (pv["total"], pv["new_count"], pv["dup_count"]) == (2, 2, 0)
    assert pv["account_name"] == "Giro" and "skipped_status" in pv["warnings"]

    # Import bestätigen (über den bestehenden Handler)
    import os
    pend = os.path.join("data", "pending_imports")
    os.makedirs(pend, exist_ok=True)
    with open(os.path.join(pend, "camt1_test.json"), "w", encoding="utf-8") as f:
        json.dump({"files": data["files"]}, f, default=str)
    status, body = handlers.handle_confirm_import(tmp_db, {"import_id": ["camt1"]})
    res = json.loads(body)
    assert status == 200 and res["ok"] and res["results"][0]["inserted"] == 2

    again = build_import_preview(tmp_db, files())["files"][0]
    assert (again["new_count"], again["dup_count"]) == (0, 2)


def test_unknown_account_blocks_import(tmp_db):
    d = check_statement(parse_xml(_read("camt053_v02.xml"))[0]).as_dict()
    d["filename"] = "v02.xml"
    pv = build_import_preview(tmp_db, {"files": [d]})["files"][0]
    assert pv["status"] == "error" and pv["account_id"] is None


# ── Storno in beide Richtungen, viele krumme Beträge ─────────────────────────

def _simple_doc(opening, closing, entries):
    body = "".join(
        f'<Ntry><Amt Ccy="EUR">{a}</Amt><CdtDbtInd>{i}</CdtDbtInd>{r}<Sts>BOOK</Sts>'
        f'<BookgDt><Dt>2026-03-05</Dt></BookgDt></Ntry>' for a, i, r in entries)
    bal = lambda c, a: (f'<Bal><Tp><CdOrPrtry><Cd>{c}</Cd></CdOrPrtry></Tp>'
                        f'<Amt Ccy="EUR">{a}</Amt><CdtDbtInd>CRDT</CdtDbtInd></Bal>')
    return (f'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt>'
            f'<Stmt><Acct><Id><IBAN>{OWN_IBAN}</IBAN></Id></Acct>{bal("OPBD", opening)}'
            f'{bal("CLBD", closing)}{body}</Stmt></BkToCstmrStmt></Document>').encode()


def test_reversal_in_both_directions_follows_cdtdbtind():
    rv = "<RvslInd>true</RvslInd>"
    # Storno einer Lastschrift (CRDT) und Storno einer Gutschrift (DBIT)
    (st,) = parse_xml(_simple_doc("100.00", "100.00",
                                  [("12.00", "CRDT", rv), ("12.00", "DBIT", rv)]))
    assert [t.amount for t in st.transactions] == [12.0, -12.0]
    assert all(t.reference.startswith("Storno:") for t in st.transactions)
    assert "balance" not in st.warnings


def test_many_odd_amounts_balance_exactly_and_reach_db_in_cents():
    from decimal import Decimal
    from money import to_minor
    entries = [(f"0.{n % 90 + 10:02d}", "CRDT" if n % 3 else "DBIT", "") for n in range(500)]
    total = sum((Decimal(a) if i == "CRDT" else -Decimal(a)) for a, i, _ in entries)
    closing = Decimal("1000.00") + total
    (st,) = parse_xml(_simple_doc("1000.00", str(closing), entries))
    assert "balance" not in st.warnings
    # float -> Minor Units rundet auf den exakten Cent (nicht 0,1+0,2 = 0,30000000000000004)
    assert sum(to_minor(t.amount) for t in st.transactions) == to_minor(total)
    # und ein um einen Cent falscher Schlusssaldo fällt auf
    (bad,) = parse_xml(_simple_doc("1000.00", str(closing + Decimal("0.01")), entries))
    assert "balance" in bad.warnings
