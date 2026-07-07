"""
Tests für die SKR-Überarbeitung:
- berechnete IDs (Rahmen·100000 + Kontonummer)
- Löschen (nur Nicht-Standard), Referenz-Prüfung
- next_free_account_number (Kopieren-Vorschlag)
- ShowInMenu: Default, Toggle, Insert/Update, Dropdown-Filter in den Transaktionen
"""
import pytest

from db import Database, coa_id
from server.pages_transactions import PageTransactions


def _by_number(db, number, framework=4):
    return next((r for r in db.fetch_chart_of_accounts()
                 if r[2] == number and r[1] == framework), None)


def test_coa_id_formula():
    assert coa_id(4, 6850) == 406850
    assert coa_id(3, 4400) == 304400
    assert coa_id(4, 12345) == 412345


class TestComputedIds:
    def test_seeded_id_matches_formula(self, tmp_db):
        row = _by_number(tmp_db, 4400)
        assert row is not None and row[0] == 404400
        row2 = _by_number(tmp_db, 6815)
        assert row2 is not None and row2[0] == 406815

    def test_insert_sets_computed_id(self, tmp_db):
        assert tmp_db.insert_chart_of_accounts(4, 9501, 'Testkonto', 'Gruppe', is_standard=0)
        assert _by_number(tmp_db, 9501)[0] == 409501

    def test_insert_duplicate_number_fails(self, tmp_db):
        assert tmp_db.insert_chart_of_accounts(4, 9502, 'A', 'G')
        assert tmp_db.insert_chart_of_accounts(4, 9502, 'B', 'G') is False


class TestDelete:
    def test_delete_non_standard(self, tmp_db):
        tmp_db.insert_chart_of_accounts(4, 9601, 'Custom', 'G', is_standard=0)
        assert tmp_db.delete_chart_of_accounts(coa_id(4, 9601)) is True
        assert _by_number(tmp_db, 9601) is None

    def test_delete_standard_refused(self, tmp_db):
        std = next(r for r in tmp_db.fetch_chart_of_accounts() if r[5] == 1)
        assert tmp_db.delete_chart_of_accounts(std[0]) is False
        assert _by_number(tmp_db, std[2]) is not None


class TestStandardToggle:
    def test_update_can_toggle_standard(self, tmp_db):
        """Eigenes Konto → Standard: geschützt; zurück → wieder löschbar."""
        tmp_db.insert_chart_of_accounts(4, 9801, 'Eigenes', 'G', is_standard=0)
        cid = coa_id(4, 9801)

        tmp_db.update_chart_of_accounts(cid, 4, 9801, 'Eigenes', 'G', is_standard=1)
        assert _by_number(tmp_db, 9801)[5] == 1
        assert tmp_db.delete_chart_of_accounts(cid) is False

        tmp_db.update_chart_of_accounts(cid, 4, 9801, 'Eigenes', 'G', is_standard=0)
        assert _by_number(tmp_db, 9801)[5] == 0
        assert tmp_db.delete_chart_of_accounts(cid) is True

    def test_name_editable_after_destandardize(self, tmp_db):
        """Standard-Konto: Name fix; nach Entfernen des Hakens im selben
        Schritt änderbar (IsStandard wird vor Name/Description gesetzt)."""
        std = next(r for r in tmp_db.fetch_chart_of_accounts() if r[5] == 1)

        tmp_db.update_chart_of_accounts(std[0], std[1], std[2], 'Neuer Name', 'G')
        assert _by_number(tmp_db, std[2], std[1])[3] == std[3]

        tmp_db.update_chart_of_accounts(std[0], std[1], std[2], 'Neuer Name', 'G',
                                        is_standard=0)
        assert _by_number(tmp_db, std[2], std[1])[3] == 'Neuer Name'


class TestFrameworkValidation:
    def test_add_rejects_invalid_framework(self, tmp_db):
        from server import handlers
        for bad in ('0', '-1', '99'):
            _, loc = handlers.handle_add_skr(tmp_db, {
                'name': ['X'], 'group': ['G'],
                'framework_nr': [bad], 'account': ['9901']})
            assert 'error' in loc, f"Rahmen {bad} wurde akzeptiert: {loc}"
        assert _by_number(tmp_db, 9901) is None

    def test_add_rejects_nonpositive_account(self, tmp_db):
        from server import handlers
        _, loc = handlers.handle_add_skr(tmp_db, {
            'name': ['X'], 'group': ['G'],
            'framework_nr': ['4'], 'account': ['0']})
        assert 'error' in loc

    def test_add_standard_account_via_handler(self, tmp_db):
        from server import handlers
        _, loc = handlers.handle_add_skr(tmp_db, {
            'name': ['Österreich-Konto'], 'group': ['G'],
            'framework_nr': ['7'], 'account': ['9903'], 'is_standard': ['1']})
        assert 'error' not in loc, loc
        row = _by_number(tmp_db, 9903, framework=7)
        assert row is not None and row[5] == 1


class TestReferenced:
    def test_referenced_by_booking(self, tmp_db):
        tmp_db.insert_chart_of_accounts(4, 9701, 'Custom', 'G')
        cid = coa_id(4, 9701)
        assert tmp_db.coa_is_referenced(cid) is False
        tmp_db.insert_booking(date_booking='2024-01-01', amount=10.0, coa_id=cid, booking_type='entry')
        assert tmp_db.coa_is_referenced(cid) is True


class TestNextFree:
    def test_skips_taken(self, tmp_db):
        existing = {r[2] for r in tmp_db.fetch_chart_of_accounts() if r[1] == 4}
        n = tmp_db.next_free_account_number(4, 4400)  # 4400 ist belegt
        assert n >= 4400 and n not in existing

    def test_returns_start_when_free(self, tmp_db):
        assert tmp_db.next_free_account_number(4, 98000) == 98000


class TestShowInMenu:
    def test_default_shown_after_seed(self, tmp_db):
        row = _by_number(tmp_db, 4400)
        assert (row[7] if len(row) > 7 else 1) == 1

    def test_toggle(self, tmp_db):
        tmp_db.insert_chart_of_accounts(4, 9801, 'X', 'G')
        cid = coa_id(4, 9801)
        assert tmp_db.toggle_coa_show_in_menu(cid) == 0
        assert tmp_db.toggle_coa_show_in_menu(cid) == 1

    def test_insert_hidden(self, tmp_db):
        tmp_db.insert_chart_of_accounts(4, 9802, 'X', 'G', show_in_menu=0)
        assert _by_number(tmp_db, 9802)[7] == 0

    def test_update_sets_flag_and_name(self, tmp_db):
        tmp_db.insert_chart_of_accounts(4, 9803, 'X', 'G', show_in_menu=1)
        cid = coa_id(4, 9803)
        tmp_db.update_chart_of_accounts(cid, 4, 9803, 'X2', 'G2', show_in_menu=0)
        row = _by_number(tmp_db, 9803)
        assert row[7] == 0
        assert row[3] == 'X2'  # Name (Nicht-Standard) aktualisiert


class TestDropdownFilter:
    def test_hidden_account_not_offered(self, tmp_db):
        cid = coa_id(4, 4400)
        tmp_db.toggle_coa_show_in_menu(cid)  # 4400 ausblenden
        html = PageTransactions(tmp_db, date_from='2024-01-01', date_to='2024-12-31')
        assert f'value="{cid}"' not in html

    def test_selected_hidden_account_still_offered(self, tmp_db):
        cid = coa_id(4, 4400)
        tmp_db.toggle_coa_show_in_menu(cid)  # ausblenden
        bid = tmp_db.insert_booking(date_booking='2024-06-01', amount=5.0,
                                    coa_id=cid, booking_type='entry')
        html = PageTransactions(tmp_db, edit_transaction_id=bid,
                                date_from='2024-01-01', date_to='2024-12-31')
        assert f'value="{cid}"' in html
