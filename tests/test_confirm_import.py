# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Integrationstests für handlers.handle_confirm_import (Kontoauszug-Import).

Deckt ab:
- Konto-Auflösung PRO Beleg (Auszüge verschiedener Konten korrekt zuordnen)
- Duplikat-Überspringung über check_booking_exists
- Meldung bei unbekanntem Konto (account_found False), Datei bleibt erhalten

Läuft gegen eine temporäre DB; die pending-import-Datei wird in
data/pending_imports/ angelegt und nach dem Test wieder entfernt.
"""
import os
import json
import uuid

import pytest

from server import handlers
from server.import_preview import match_account


PENDING_DIR = os.path.join("data", "pending_imports")
IBAN_A = "DE89 3704 0044 0532 0130 00"
IBAN_B = "DE12 5001 0517 0648 4898 90"


def _txn(date, amount, reference, recipient="Partner", foreign_iban="DE00FOREIGN"):
    return {"date": date, "amount": amount, "reference": reference,
            "recipient": recipient, "foreign_iban": foreign_iban}


@pytest.fixture
def pending_factory():
    """Erzeugt pending-import-Dateien und räumt sie hinterher weg."""
    created = []

    def make(files):
        os.makedirs(PENDING_DIR, exist_ok=True)
        import_id = "pytest_" + uuid.uuid4().hex[:8]
        path = os.path.join(PENDING_DIR, f"{import_id}_test.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"files": files, "import_id": import_id}, f)
        created.append((import_id, path))
        return import_id

    yield make

    for _id, path in created:
        if os.path.exists(path):
            os.remove(path)


@pytest.fixture
def db_two_accounts(tmp_db):
    tmp_db.insert_account("Konto A", "Owner", IBAN_A, "GENODE1X", "Bank A", skr_account=1200)
    tmp_db.insert_account("Konto B", "Owner", IBAN_B, "INGDDEFF", "Bank B", skr_account=1210)
    return tmp_db


def _bank_bookings(db):
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute("SELECT Account_ID, Amount, Text FROM Bookings WHERE BookingType='bank' ORDER BY ID")
    rows = cur.fetchall()
    conn.close()
    return rows


def test_resolves_account_per_file(db_two_accounts, pending_factory):
    """Multi-Konto-Bugfix: jeder Beleg landet auf seinem eigenen Konto."""
    acc_a, _ = match_account(db_two_accounts.fetch_accounts(), IBAN_A)
    acc_b, _ = match_account(db_two_accounts.fetch_accounts(), IBAN_B)

    import_id = pending_factory([
        {"filename": "a.pdf", "bank_code": "VBR", "iban": IBAN_A, "document_date": "2025-01-31",
         "transactions": [_txn("2025-01-10", -50.0, "A-1")]},
        {"filename": "b.pdf", "bank_code": "DKB", "iban": IBAN_B, "document_date": "2025-01-31",
         "transactions": [_txn("2025-01-11", 99.0, "B-1")]},
    ])

    status, body = handlers.handle_confirm_import(db_two_accounts, {"import_id": [import_id]})
    data = json.loads(body)

    assert status == 200
    assert data["ok"] is True
    assert all(r["account_found"] for r in data["results"])
    assert data["all_done"] is True

    rows = _bank_bookings(db_two_accounts)
    by_text = {r[2]: r for r in rows}
    assert by_text["A-1"][0] == acc_a
    assert by_text["B-1"][0] == acc_b


def test_duplicate_is_skipped(db_two_accounts, pending_factory):
    acc_a, _ = match_account(db_two_accounts.fetch_accounts(), IBAN_A)
    db_two_accounts.insert_booking(
        date_booking="2025-01-10", amount=-50.0, account_id=acc_a,
        foreign_bank_account="DE00FOREIGN", recipient_client="Partner",
        text="A-1", booking_type="bank")

    import_id = pending_factory([
        {"filename": "a.pdf", "bank_code": "VBR", "iban": IBAN_A, "document_date": "2025-01-31",
         "transactions": [_txn("2025-01-10", -50.0, "A-1"),       # Duplikat
                          _txn("2025-01-12", -7.5, "A-2")]},      # neu
    ])

    status, body = handlers.handle_confirm_import(
        db_two_accounts, {"import_id": [import_id], "file_index": ["0"]})
    data = json.loads(body)

    assert status == 200
    res = data["results"][0]
    assert res["inserted"] == 1
    assert res["skipped"] == 1


def test_reimport_after_table_update_is_skipped(db_two_accounts, pending_factory):
    """Kontoauszug-Re-Import nach WISO-Tabellen-Import erzeugt keine Dubletten.

    Regression: Der Tabellen-Export-Import überschreibt Text und
    ForeignBankAccount der bank-Buchungen. Die Duplikat-Erkennung darf deshalb
    nur über Datum+Betrag+Konto gehen (zählbasiert) — zwei Transaktionen mit
    gleichem Tag/Betrag im selben Auszug bleiben trotzdem beide erhalten.
    """
    txns = [_txn("2025-01-10", -50.0, "A-1"),
            _txn("2025-01-10", -50.0, "A-1b"),   # gleicher Tag/Betrag, andere Referenz
            _txn("2025-01-12", -7.5, "A-2")]

    def make_file():
        return [{"filename": "a.pdf", "bank_code": "VBR", "iban": IBAN_A,
                 "document_date": "2025-01-31", "transactions": list(txns)}]

    import_id = pending_factory(make_file())
    status, body = handlers.handle_confirm_import(db_two_accounts, {"import_id": [import_id]})
    res = json.loads(body)["results"][0]
    assert res["inserted"] == 3, res
    assert res["skipped"] == 0, res

    # Simuliert den WISO-Tabellen-Import: Text + IBAN werden überschrieben
    conn = db_two_accounts._get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE Bookings SET Text = 'Verwendungszweck ' || ID, "
                "ForeignBankAccount = 'DE99 9999 9999 9999 9999 99' "
                "WHERE BookingType = 'bank'")
    conn.commit()
    conn.close()

    import_id2 = pending_factory(make_file())
    status, body = handlers.handle_confirm_import(db_two_accounts, {"import_id": [import_id2]})
    res2 = json.loads(body)["results"][0]
    assert res2["inserted"] == 0, res2
    assert res2["skipped"] == 3, res2
    assert len(_bank_bookings(db_two_accounts)) == 3


def test_unknown_account_reported(db_two_accounts, pending_factory):
    import_id = pending_factory([
        {"filename": "x.pdf", "bank_code": "VBR", "iban": "DE00000000000000000000",
         "document_date": "2025-01-31", "transactions": [_txn("2025-01-10", -5.0, "X-1")]},
    ])

    status, body = handlers.handle_confirm_import(db_two_accounts, {"import_id": [import_id]})
    data = json.loads(body)

    assert status == 200
    assert data["ok"] is True
    res = data["results"][0]
    assert res["account_found"] is False
    assert res["inserted"] == 0
    assert _bank_bookings(db_two_accounts) == []
