"""
Tests for DocumentParser._parse_vbr_text – VBR bank statement text parser.

These tests work on plain text strings (no PDF required) and verify the
regex-based transaction extraction logic.
"""
import sys
import os
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from document_parser import DocumentParser


@pytest.fixture
def parser():
    return DocumentParser(data_dir='/tmp/test_belege', log_dir='/tmp/test_log')


YEAR = 2024


class TestParseVbrText:
    def test_single_debit_transaction(self, parser):
        """S = Soll = debit = negative amount."""
        text = (
            "01.12. 01.12. Lastschrift PN:931 1.142,18 S\n"
            "Telekom AG\n"
            "Rechnung Nov\n"
        )
        txns = parser._parse_vbr_text(text, YEAR)
        assert len(txns) == 1
        t = txns[0]
        assert t['amount'] == pytest.approx(-1142.18)
        assert t['date'] == '2024-12-01'
        assert 'Telekom' in t['recipient']

    def test_single_credit_transaction(self, parser):
        """H = Haben = credit = positive amount."""
        text = (
            "15.03. 15.03. Gutschrift 2.500,00 H\n"
            "Kunde GmbH\n"
            "Zahlung Rechnungen\n"
        )
        txns = parser._parse_vbr_text(text, YEAR)
        assert len(txns) == 1
        assert txns[0]['amount'] == pytest.approx(2500.0)
        assert txns[0]['date'] == '2024-03-15'

    def test_multiple_transactions(self, parser):
        """Two consecutive transactions are both parsed."""
        text = (
            "03.01. 03.01. Überweisung 200,00 S\n"
            "Empfänger A\n"
            "Referenz A\n"
            "10.01. 10.01. Gutschrift 500,00 H\n"
            "Empfänger B\n"
            "Referenz B\n"
        )
        txns = parser._parse_vbr_text(text, YEAR)
        assert len(txns) == 2
        amounts = sorted(t['amount'] for t in txns)
        assert amounts[0] == pytest.approx(-200.0)
        assert amounts[1] == pytest.approx(500.0)

    def test_german_thousands_separator(self, parser):
        """1.234,56 (German format) parses correctly."""
        text = "07.06. 07.06. Lastschrift 1.234,56 S\nMuster AG\n"
        txns = parser._parse_vbr_text(text, YEAR)
        assert txns[0]['amount'] == pytest.approx(-1234.56)

    def test_iban_extracted(self, parser):
        """IBAN in detail lines is captured in foreign_iban."""
        text = (
            "20.05. 20.05. Überweisung 100,00 S\n"
            "Empfänger\n"
            "IBAN: DE89370400440532013000\n"
            "Verwendungszweck\n"
        )
        txns = parser._parse_vbr_text(text, YEAR)
        assert len(txns) == 1
        assert txns[0]['foreign_iban'] == 'DE89370400440532013000'

    def test_footer_stripped(self, parser):
        """Page footer block (K-number + 'Bitte beachten') is ignored."""
        text = (
            "05.02. 05.02. Gutschrift 300,00 H\n"
            "Firma Z\n"
            "\n0128\n000\nK00009283\nBitte beachten Sie die Hinweise auf der Rückseite\n"
            "10.02. 10.02. Lastschrift 50,00 S\n"
            "Lieferant\n"
        )
        txns = parser._parse_vbr_text(text, YEAR)
        assert len(txns) == 2

    def test_empty_text_returns_empty_list(self, parser):
        txns = parser._parse_vbr_text("", YEAR)
        assert txns == []

    def test_no_transactions_in_header_only(self, parser):
        """Header lines without transaction patterns produce no results."""
        text = "Kontoauszug Nr. 1/2024\nIBAN DE89370400440532013000\nBIC VBRTDE1X\n"
        txns = parser._parse_vbr_text(text, YEAR)
        assert txns == []

    def test_year_used_in_date(self, parser):
        """Year parameter is applied to parsed dates."""
        text = "01.06. 01.06. Gutschrift 10,00 H\nKunde\n"
        txns_2023 = parser._parse_vbr_text(text, 2023)
        txns_2024 = parser._parse_vbr_text(text, 2024)
        assert txns_2023[0]['date'].startswith('2023')
        assert txns_2024[0]['date'].startswith('2024')
