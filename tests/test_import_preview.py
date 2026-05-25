"""
Tests für die Kontoauszug-Import-Vorschau (server/import_preview.py).

Deckt ab:
- match_account: IBAN-Vergleich ignoriert Leerzeichen, leere/unbekannte IBAN
- build_import_preview: Status ok/warn/error, Zähler neu/Duplikat,
  Duplikat-Erkennung gegen vorhandene Buchungen, Parse-Hinweise
"""
import pytest

from server.import_preview import build_import_preview, match_account


IBAN = "DE89 3704 0044 0532 0130 00"
FOREIGN_IBAN = "DE00111122223333444455"


def _txn(date="2025-01-15", recipient="ACME GmbH", reference="Rechnung 100",
         amount=-50.0, foreign_iban=FOREIGN_IBAN):
    return {"date": date, "recipient": recipient, "reference": reference,
            "amount": amount, "foreign_iban": foreign_iban}


def _file(iban, transactions, filename="kontoauszug.pdf", bank_code="VBR",
          document_date="2025-01-31"):
    return {"filename": filename, "bank_code": bank_code, "iban": iban,
            "document_date": document_date, "transactions": transactions}


@pytest.fixture
def db_with_account(tmp_db):
    """DB mit genau einem Bankkonto, dessen IBAN Leerzeichen enthält."""
    tmp_db.insert_account("Giro", "Owner", IBAN, "GENODE1X", "Volksbank", skr_account=1200)
    return tmp_db


class TestMatchAccount:
    def test_matches_ignoring_spaces(self, db_with_account):
        accounts = db_with_account.fetch_accounts()
        acc_id, name = match_account(accounts, "DE89370400440532013000")
        assert acc_id is not None
        assert name == "Giro"

    def test_unknown_iban_returns_none(self, db_with_account):
        accounts = db_with_account.fetch_accounts()
        assert match_account(accounts, "DE00000000000000000000") == (None, None)

    def test_empty_iban_returns_none(self, db_with_account):
        accounts = db_with_account.fetch_accounts()
        assert match_account(accounts, None) == (None, None)


class TestBuildImportPreview:
    def test_account_not_found_is_error(self, db_with_account):
        data = {"files": [_file("DE00000000000000000000", [_txn()])]}
        out = build_import_preview(db_with_account, data)["files"][0]
        assert out["status"] == "error"
        assert out["account_id"] is None

    def test_clean_file_is_ok(self, db_with_account):
        data = {"files": [_file(IBAN, [
            _txn(),
            _txn(reference="Rechnung 200", amount=-20.0),
        ])]}
        out = build_import_preview(db_with_account, data)["files"][0]
        assert out["status"] == "ok"
        assert out["total"] == 2
        assert out["new_count"] == 2
        assert out["dup_count"] == 0
        assert out["problems"] == []
        assert out["account_name"] == "Giro"

    def test_duplicate_detected(self, db_with_account):
        acc_id, _ = match_account(db_with_account.fetch_accounts(), IBAN)
        db_with_account.insert_booking(
            date_booking="2025-01-15", amount=-50.0, account_id=acc_id,
            foreign_bank_account=FOREIGN_IBAN, recipient_client="ACME GmbH",
            text="Rechnung 100", booking_type="bank")

        data = {"files": [_file(IBAN, [_txn()])]}
        out = build_import_preview(db_with_account, data)["files"][0]
        assert out["dup_count"] == 1
        assert out["new_count"] == 0
        assert out["status"] == "warn"
        assert out["transactions"][0]["dup"] is True
        assert out["problems"][0]["dup"] is True

    def test_parse_warnings_flagged(self, db_with_account):
        bad = _txn(amount=0, recipient="", reference="", date="")
        data = {"files": [_file(IBAN, [bad])]}
        out = build_import_preview(db_with_account, data)["files"][0]
        assert out["status"] == "warn"
        warns = out["transactions"][0]["warn"]
        assert "amount" in warns
        assert "date" in warns
        assert "empty" in warns

    def test_mixed_new_and_duplicate(self, db_with_account):
        acc_id, _ = match_account(db_with_account.fetch_accounts(), IBAN)
        db_with_account.insert_booking(
            date_booking="2025-01-15", amount=-50.0, account_id=acc_id,
            foreign_bank_account=FOREIGN_IBAN, recipient_client="ACME GmbH",
            text="Rechnung 100", booking_type="bank")

        data = {"files": [_file(IBAN, [
            _txn(),                                   # Duplikat
            _txn(reference="Neu", amount=-10.0),      # neu
        ])]}
        out = build_import_preview(db_with_account, data)["files"][0]
        assert out["total"] == 2
        assert out["dup_count"] == 1
        assert out["new_count"] == 1

    def test_per_file_account_resolution(self, db_with_account):
        """Zwei Belege, nur einer hat ein bekanntes Konto."""
        data = {"files": [
            _file(IBAN, [_txn()]),
            _file("DE99999999999999999999", [_txn()], filename="fremd.pdf"),
        ]}
        files = build_import_preview(db_with_account, data)["files"]
        assert files[0]["status"] in ("ok", "warn")
        assert files[0]["account_id"] is not None
        assert files[1]["status"] == "error"
        assert files[1]["account_id"] is None
