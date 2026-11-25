# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""CAMT.053: die Zusatzfelder einer Bankbewegung (``importers/camt.py``, ``importers/base.py``).

Gelesen werden die Valuta (``Ntry/ValDt``), die Bankreferenz (``Ntry/AcctSvcrRef``, nur auf der Ebene des Umsatzes: alle Teile einer
Sammelbuchung teilen sie), die End-to-End-Referenz (``Refs/EndToEndId``), die Mandatsreferenz (``Refs/MndtId``) und die Gläubiger-ID
(``RltdPties/Cdtr``, in der Form von camt.053.001.02/.04 wie in der von .08, nur das Schema ``SEPA``). Was ein Wert nicht sicher hergibt –
ein Platzhalter der Bank, mehr als 35 Zeichen, ein Steuerzeichen, ein Datum, das es nicht gibt –, bleibt leer. Das Modell trägt die Felder
nur, wo sie einen Wert haben (``as_dict``), der Import schreibt sie in ``BankTransactionDetails`` und die Maske der Bankbewegung zeigt sie
schreibgeschützt.

Namen, Referenzen und IBANs sind erfundene Testwerte.
"""
from contextlib import closing
from xml.sax.saxutils import escape

import pytest

from importers import CANONICAL_FIELDS, check_statement
from importers.base import DETAIL_FIELDS, StatementTransaction
from importers.camt import parse_xml
from server import handlers
from server.pages_transactions import PageTransactions
from tests.test_booking_edit_keeps_fields import _read_form
from tests.test_confirm_import import IBAN_A, _txn, db_two_accounts, pending_factory  # noqa: F401

OWN_IBAN = "DE89370400440532013000"
CREDITOR = "DE98ZZZ09999999999"
OLD_KEYS = {"date", "amount", "recipient", "reference", "foreign_iban", "warnings"}


# ── erfundenes CAMT ──────────────────────────────────────────────────────────

def _other(identifier, scheme="SEPA"):
    scheme_xml = f"<SchmeNm><Prtry>{escape(scheme)}</Prtry></SchmeNm>" if scheme is not None else ""
    return f"<Othr><Id>{escape(identifier)}</Id>{scheme_xml}</Othr>"


def _creditor(*others, v08=False):
    identification = f"<Id><PrvtId>{''.join(others)}</PrvtId></Id>" if others else ""
    if v08:                                            # camt.053.001.08: die Partei steht unter Pty
        return f"<Cdtr><Pty><Nm>Muster AG</Nm>{identification}</Pty></Cdtr>"
    return f"<Cdtr><Nm>Muster AG</Nm>{identification}</Cdtr>"


def _tx(*, e2e=None, mandate=None, others=(), v08=False, amount=None, text="Zweck"):
    refs = (f"<EndToEndId>{escape(e2e)}</EndToEndId>" if e2e is not None else "") + \
           (f"<MndtId>{escape(mandate)}</MndtId>" if mandate is not None else "")
    refs = f"<Refs>{refs}</Refs>" if refs else ""
    own_amount = f'<Amt Ccy="EUR">{amount}</Amt><CdtDbtInd>DBIT</CdtDbtInd>' if amount else ""
    parties = f"<RltdPties>{_creditor(*others, v08=v08)}</RltdPties>" if others else ""
    return f"<TxDtls>{refs}{own_amount}{parties}<RmtInf><Ustrd>{escape(text)}</Ustrd></RmtInf></TxDtls>"


def _entry(*transactions, amount="10.00", acct_ref=None, value_date="2026-03-05"):
    ref = f"<AcctSvcrRef>{escape(acct_ref)}</AcctSvcrRef>" if acct_ref is not None else ""
    value = value_date if value_date.startswith("<") else (f"<ValDt><Dt>{value_date}</Dt></ValDt>" if value_date else "")
    return (f'<Ntry><Amt Ccy="EUR">{amount}</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts><BookgDt><Dt>2026-03-05</Dt></BookgDt>'
            f'{value}{ref}<NtryDtls>{"".join(transactions)}</NtryDtls></Ntry>')


def _statement(*entries, version="02"):
    xml = (f'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.{version}"><BkToCstmrStmt><Stmt>'
           f'<Acct><Id><IBAN>{OWN_IBAN}</IBAN></Id></Acct>{"".join(entries)}</Stmt></BkToCstmrStmt></Document>')
    return parse_xml(xml.encode("utf-8"))[0]


def _details(transaction):
    return tuple(getattr(transaction, name) for name in DETAIL_FIELDS)


def _read(*entries, version="02"):
    return _statement(*entries, version=version).transactions


# ── Parser ───────────────────────────────────────────────────────────────────

def test_an_entry_gives_the_value_date_the_bank_reference_and_the_references_of_its_transaction():
    (t,) = _read(_entry(_tx(e2e="E2E-4711", mandate="MANDAT-1", others=[_other(CREDITOR)]),
                        acct_ref="BANK-0815", value_date="2026-03-07"))
    assert DETAIL_FIELDS == ("value_date", "end_to_end_id", "mandate_ref", "creditor_id", "bank_ref")
    assert _details(t) == ("2026-03-07", "E2E-4711", "MANDAT-1", CREDITOR, "BANK-0815")
    assert t.date == "2026-03-05"                           # das Buchungsdatum bleibt, die Valuta ist ein eigenes Feld


def test_the_creditor_id_is_read_in_the_path_of_camt_02_and_04_and_in_the_one_of_08():
    old, new = _read(_entry(_tx(others=[_other(CREDITOR)], v08=False))), \
        _read(_entry(_tx(others=[_other(CREDITOR)], v08=True)), version="08")
    assert [t.creditor_id for t in old + new] == [CREDITOR, CREDITOR]


@pytest.mark.parametrize("others, expected", [
    pytest.param([_other(CREDITOR, "sepa ")], CREDITOR, id="Schreibweise-egal"),
    pytest.param([_other("KUNDE-1", "KUNDENNUMMER"), _other(CREDITOR)], CREDITOR, id="erstes-SEPA-nach-anderem-Schema"),
    pytest.param([_other(CREDITOR), _other("DE00ZZZ00000000000")], CREDITOR, id="das-erste-SEPA"),
    pytest.param([_other("KUNDE-1", "KUNDENNUMMER")], "", id="anderes-Schema"),
    pytest.param([_other(CREDITOR, None)], "", id="ohne-Schema"),
    pytest.param([_other("NOTPROVIDED")], "", id="Platzhalter"),
    pytest.param([_other("X" * 36)], "", id="zu-lang"),
    pytest.param([], "", id="keine-Id"),
])
def test_only_the_sepa_scheme_gives_a_creditor_id(others, expected):
    (t,) = _read(_entry(_tx(others=others)))
    assert t.creditor_id == expected


@pytest.mark.parametrize("value, expected", [
    pytest.param("NOTPROVIDED", "", id="NOTPROVIDED"), pytest.param("nonref", "", id="NONREF-klein"),
    pytest.param("NotAvailable", "", id="NOTAVAILABLE-gemischt"), pytest.param("  ", "", id="nur-Leerraum"),
    pytest.param("A" * 35, "A" * 35, id="35-Zeichen"), pytest.param("A" * 36, "", id="36-Zeichen"),
    pytest.param("AB\tCD", "", id="Steuerzeichen-im-Wert"), pytest.param("  REF-1  ", "REF-1", id="Leerraum-am-Rand"),
])
def test_a_value_the_bank_does_not_give_safely_stays_empty_in_every_field(value, expected):
    (t,) = _read(_entry(_tx(e2e=value, mandate=value, others=[_other(value)]), acct_ref=value))
    assert (t.end_to_end_id, t.mandate_ref, t.creditor_id, t.bank_ref) == (expected,) * 4


@pytest.mark.parametrize("value_date, expected", [
    pytest.param("2026-03-07", "2026-03-07", id="Dt"),
    pytest.param("<ValDt><DtTm>2026-03-07T12:30:00</DtTm></ValDt>", "2026-03-07", id="DtTm"),
    pytest.param("2026-02-30", "", id="ein-Tag-den-es-nicht-gibt"),
    pytest.param("07.03.2026", "", id="andere-Schreibweise"),
    pytest.param("", "", id="keine-Valuta"),
])
def test_the_value_date_is_an_existing_day_or_empty(value_date, expected):
    (t,) = _read(_entry(_tx(), value_date=value_date))
    assert t.value_date == expected


def test_without_the_fields_an_entry_reads_as_before():
    (t,) = _read(_entry(_tx(), value_date=""))
    assert _details(t) == ("",) * 5 and set(t.as_dict()) == OLD_KEYS


def test_a_split_entry_gives_every_part_the_bank_reference_and_its_own_references():
    parts = _tx(e2e="E1", amount="30.00", text="Teil 1") + _tx(e2e="E2", mandate="M2", amount="20.00", text="Teil 2")
    statement = _statement(_entry(parts, amount="50.00", acct_ref="SAMMEL-1", value_date="2026-03-09"))
    assert "split_mismatch" not in statement.warnings
    assert [(t.amount, t.value_date, t.bank_ref, t.end_to_end_id, t.mandate_ref) for t in statement.transactions] == [
        (-30.0, "2026-03-09", "SAMMEL-1", "E1", ""), (-20.0, "2026-03-09", "SAMMEL-1", "E2", "M2")]


def test_a_split_read_as_one_entry_keeps_the_bank_reference_of_the_entry():
    """Stimmen die Teile nicht zur Summe, bleibt es ein Umsatz mit dem Gesamtbetrag – die Referenz des Umsatzes bleibt, die der Teile gibt es dann nicht."""
    parts = _tx(e2e="E1", amount="30.00") + _tx(e2e="E2", amount="10.00")
    statement = _statement(_entry(parts, amount="50.00", acct_ref="SAMMEL-1"))
    assert "split_mismatch" in statement.warnings
    (t,) = statement.transactions
    assert (t.amount, t.bank_ref, t.end_to_end_id) == (-50.0, "SAMMEL-1", "")


def test_the_existing_fixtures_read_with_the_value_date_and_without_anything_else():
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "camt" / "camt053_v02.xml"
    credit, debit = check_statement(parse_xml(fixture.read_bytes())[0]).transactions
    assert (credit.value_date, debit.value_date) == ("2026-03-05", "2026-03-10")
    assert _details(credit)[1:] == ("",) * 4 and _details(debit)[1:] == ("",) * 4


# ── Modell ───────────────────────────────────────────────────────────────────

def test_as_dict_adds_only_the_fields_that_have_a_value():
    assert set(StatementTransaction(date="2026-03-01", amount=-1.0).as_dict()) == OLD_KEYS
    data = StatementTransaction(date="2026-03-01", amount=-1.0, bank_ref="B-1", value_date="2026-03-02").as_dict()
    assert set(data) == OLD_KEYS | {"bank_ref", "value_date"}
    assert (data["bank_ref"], data["value_date"]) == ("B-1", "2026-03-02")


def test_from_dict_cleans_the_fields_the_same_way_and_keeps_the_canonical_columns():
    t = StatementTransaction.from_dict({"bank_ref": " B-1 ", "end_to_end_id": "NOTPROVIDED", "mandate_ref": "x" * 36,
                                        "creditor_id": 5, "value_date": "2026-3-5"})
    assert _details(t) == ("", "", "", "", "B-1")
    assert _details(StatementTransaction.from_dict({})) == ("",) * 5
    assert CANONICAL_FIELDS == ("date", "amount", "recipient", "reference", "foreign_iban")


def test_the_fields_survive_the_round_trip_through_a_pending_import():
    (t,) = _read(_entry(_tx(e2e="E2E-1", mandate="M-1", others=[_other(CREDITOR)]), acct_ref="B-1", value_date="2026-03-07"))
    assert _details(StatementTransaction.from_dict(t.as_dict())) == _details(t)


# ── Import und Maske ─────────────────────────────────────────────────────────

def _bank_ids(db):
    with closing(db._get_connection()) as conn:
        return [row[0] for row in conn.execute("SELECT ID FROM Bookings WHERE BookingType = 'bank' ORDER BY ID")]


def test_the_import_writes_the_fields_of_a_camt_booking_and_no_row_for_a_booking_without_any(db_two_accounts, pending_factory):
    (camt,) = _read(_entry(_tx(e2e="E2E-4711", mandate="MANDAT-1", others=[_other(CREDITOR)]), acct_ref="BANK-0815",
                           value_date="2026-03-07"))
    plain = _txn("2026-03-06", -5.0, "ohne Zusatzfelder")                      # wie ein PDF- oder CSV-Umsatz
    file = {"filename": "a.xml", "bank_code": "CAMT", "iban": IBAN_A, "document_date": "2026-03-31",
            "transactions": [dict(camt.as_dict(), date="2026-03-05"), plain]}
    status, body = handlers.handle_confirm_import(db_two_accounts, {"import_id": [pending_factory([file])]})
    assert status == 200
    camt_id, plain_id = _bank_ids(db_two_accounts)
    details = db_two_accounts.get_bank_details(camt_id)
    assert (details.value_date, details.end_to_end_id, details.mandate_ref, details.creditor_id, details.bank_ref) == (
        "2026-03-07", "E2E-4711", "MANDAT-1", CREDITOR, "BANK-0815")
    assert db_two_accounts.get_bank_details(plain_id) is None
    with closing(db_two_accounts._get_connection()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM BankTransactionDetails").fetchone()[0] == 1


@pytest.fixture
def bank(db_with_coa):
    """Eine Bankbewegung auf der Testbank mit allen fünf Zusatzfeldern – einer davon feindlich – und eine ohne."""
    db = db_with_coa
    account = {a[1]: a[0] for a in db.fetch_accounts()}["Testbank"]
    full = db.insert_booking("2026-03-05", -10.0, account_id=account, text="mit Feldern", booking_type="bank", bank_details={
        "value_date": "2026-03-07", "bank_ref": "BANK-0815", "end_to_end_id": "<b>x</b>", "mandate_ref": "MANDAT-1", "creditor_id": CREDITOR})
    empty = db.insert_booking("2026-03-06", -5.0, account_id=account, text="ohne Felder", booking_type="bank")
    return db, full, empty


def test_the_mask_shows_the_fields_read_only_in_a_fixed_order_and_masked(bank):
    db, full, _empty = bank
    page = PageTransactions(db, edit_transaction_id=full)
    labels = ["Valuta:", "Bankreferenz:", "End-to-End-Referenz:", "Mandatsreferenz:", "Gläubiger-ID:"]
    assert [page.index(f"<td>{label}</td>") for label in labels] == sorted(page.index(f"<td>{label}</td>") for label in labels)
    for label, value in zip(labels, ["2026-03-07", "BANK-0815", "&lt;b&gt;x&lt;/b&gt;", "MANDAT-1", CREDITOR]):
        assert f'<td>{label}</td><td class="muted">{value}</td>' in page, label
    assert "<b>x</b>" not in page                                              # der feindliche Wert steht nur maskiert da
    assert page.index("<td>ID:</td>") < page.index("<td>Valuta:</td>") < page.index("<td>Buchungsdatum:</td>")


def test_the_mask_shows_only_the_values_that_exist_and_nothing_for_a_booking_without_details(bank):
    db, full, empty = bank
    for label in ("Valuta:", "Bankreferenz:", "End-to-End-Referenz:", "Mandatsreferenz:", "Gläubiger-ID:"):
        assert f"<td>{label}</td>" not in PageTransactions(db, edit_transaction_id=empty)
    with closing(db._get_connection()) as conn:                                # nur einer von fünf Werten (ein Satz, den die SQL-Konsole schreibt)
        conn.execute("DELETE FROM BankTransactionDetails WHERE Booking_ID = ?", (full,))
        conn.execute("INSERT INTO BankTransactionDetails (Booking_ID, BankRef) VALUES (?, 'NUR-DIE-REFERENZ')", (full,))
        conn.commit()
    page = PageTransactions(db, edit_transaction_id=full)
    assert '<td>Bankreferenz:</td><td class="muted">NUR-DIE-REFERENZ</td>' in page
    assert all(f"<td>{label}</td>" not in page for label in ("Valuta:", "End-to-End-Referenz:", "Mandatsreferenz:", "Gläubiger-ID:"))


def test_the_mask_of_a_booking_that_is_no_bank_movement_never_shows_the_fields(bank):
    db, full, _empty = bank
    entry = db.insert_booking("2026-03-05", -10.0, text="ein Satz", booking_type="entry")
    with closing(db._get_connection()) as conn:
        conn.execute("INSERT INTO BankTransactionDetails (Booking_ID, BankRef) VALUES (?, 'FREMD')", (entry,))
        conn.commit()
    assert "<td>Bankreferenz:</td>" not in PageTransactions(db, edit_transaction_id=entry)


def test_the_fields_add_no_input_to_the_mask_and_saving_the_mask_leaves_them_alone(bank):
    from tests.test_booking_edit_keeps_fields import _form_post
    db, full, empty = bank
    field_names = lambda booking: sorted({name for name, _value in _read_form(db, booking).fields})
    assert field_names(full) == field_names(empty)                              # dieselben Eingabefelder mit und ohne Zusatzfelder
    before = db.get_bank_details(full)
    assert before is not None
    status, _location = handlers.handle_add_transaction(db, _form_post(db, full))
    assert status == 303
    assert db.get_bank_details(full) == before
