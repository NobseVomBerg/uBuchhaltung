# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
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

    def test_sepa_fields_removed_fieldwise(self, parser):
        """EREF/MREF/CRED/IBAN/BIC werden feldweise entfernt (auch bei
        Zeilenumbruch mitten im Feld) — der Inhalt davor bleibt vollständig.
        Früher wurde ab dem ersten Schlüsselwort der gesamte Rest verworfen."""
        text = (
            "13.07. 13.07. Lastschrift PN:931 8,22 S\n"
            "Onlineshop Beispiel S.C.A.\n"
            "302-1234567-8901234 Bestellung ABC123XYZ EREF\n"
            ": ABC123XYZ MREF: xTestMandat)28R\n"
            "4 CRED: DE98ZZZ09999999999 IBAN: DE89370400440532013000\n"
            "0 BIC: GENODEF1XXX\n"
        )
        txns = parser._parse_vbr_text(text, YEAR)
        assert len(txns) == 1
        t = txns[0]
        assert t['recipient'] == 'Onlineshop Beispiel S.C.A.'
        assert t['reference'] == '302-1234567-8901234 Bestellung ABC123XYZ'
        assert t['foreign_iban'] == 'DE89370400440532013000'

    def test_multiline_purpose_fully_preserved(self, parser):
        """Mehrzeiliger Verwendungszweck vor dem SEPA-Block bleibt komplett
        erhalten (Kern des Verlustfrei-Fixes)."""
        text = (
            "02.04. 02.04. Überweisung 250,00 S\n"
            "Vermietung Beispiel GmbH\n"
            "Miete April Objekt Musterweg 1\n"
            "inkl. Nebenkosten lt. Vertrag\n"
            "EREF: NOTPROVIDED IBAN: DE89370400440532013000 BIC: GENODEF1XXX\n"
        )
        txns = parser._parse_vbr_text(text, YEAR)
        assert len(txns) == 1
        t = txns[0]
        assert t['recipient'] == 'Vermietung Beispiel GmbH'
        assert t['reference'] == ('Miete April Objekt Musterweg 1\n'
                                  'inkl. Nebenkosten lt. Vertrag')

    def test_content_between_and_after_sepa_fields_preserved(self, parser):
        """Feldwerte sind einzelne Token – Inhalt ZWISCHEN und NACH den
        Technikfeldern bleibt erhalten (Review-Befund: die erste Fassung war
        funktional noch ein Komplett-Abschnitt ab dem ersten Schlüsselwort)."""
        text = (
            "05.05. 05.05. Überweisung 99,00 S\n"
            "Empfänger GmbH\n"
            "Rechnung 4711 EREF: NOTPROVIDED Zusatz Vereinbarung\n"
            "IBAN: DE89370400440532013000 BIC: GENODEF1XXX Danke sehr\n"
        )
        txns = parser._parse_vbr_text(text, YEAR)
        ref = txns[0]['reference']
        assert 'Rechnung 4711' in ref
        assert 'Zusatz Vereinbarung' in ref
        assert 'Danke sehr' in ref
        assert 'NOTPROVIDED' not in ref
        assert 'DE8937' not in ref and 'GENODEF' not in ref

    def test_keyword_lookalikes_not_swallowed(self, parser):
        """Beidseitige Wortgrenzen + Pflicht-Doppelpunkt: 'Arabic', 'DB IC'
        und der Vorname 'Iban' sind keine SEPA-Schlüsselwörter."""
        text = (
            "11.03. 11.03. Überweisung 50,00 H\n"
            "Iban Garcia\n"
            "Arabic-Coffee Bestellung DB IC 2024 Hamburg\n"
        )
        txns = parser._parse_vbr_text(text, YEAR)
        assert txns[0]['recipient'] == 'Iban Garcia'
        assert txns[0]['reference'] == 'Arabic-Coffee Bestellung DB IC 2024 Hamburg'

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


class TestPageBody:
    """Seitenumbruch: Kopf und Fuß raus, Buchung wieder zusammensetzen.

    Jede VBR-Seite trägt oben den Briefkopf bis "Bu-Tag Wert Vorgang" und
    unten Formularnummern plus die Fußnote. Läuft eine Buchung über den
    Umbruch, standen beide Blöcke mitten in ihren Detailzeilen: Die Fußnote
    wurde zum Empfänger, die echten Detailzeilen der Folgeseite fielen weg.
    """

    HEADER = (
        "Internet: www.volksbank-musterstadt.de\n"
        "Telefon: 0000 000-0 / Fax: 0000 000-000\n"
        "Bankleitzahl: 123 456 78\n"
        "Firmenkonto M\n"
        "EUR-Konto Kontonummer 12345678\n"
        "Nina Nutzer\n"
        "Kontoauszug Nr. 1/2024\n"
        "erstellt am 31.01.2024 22:05 Blatt 2 von 3\n"
        "Bu-Tag Wert Vorgang\n"
    )
    FOOTER = "0128\n000\nK00001234\n5M Bitte beachten Sie die Hinweise auf der Rueckseite\n"
    # Neuere Auszuege liefern die Fussnote ohne Leerzeichen
    FOOTER_TIGHT = "0128\n000\nK00001234 BittebeachtenSiedieHinweiseaufderRueckseite\n"

    def test_header_and_footer_are_cut(self):
        from importers.vbr import page_body
        body = page_body(self.HEADER + "01.12. 01.12. Lastschrift 10,00 S\n"
                         "Musterhaendler GmbH\n" + self.FOOTER)
        assert body == "01.12. 01.12. Lastschrift 10,00 S\nMusterhaendler GmbH"

    def test_footer_without_spaces_is_cut(self):
        from importers.vbr import page_body
        body = page_body(self.HEADER + "01.12. 01.12. Lastschrift 10,00 S\n"
                         "Musterhaendler GmbH\n" + self.FOOTER_TIGHT)
        assert 'beachten' not in body.lower().replace(' ', '')
        assert '0128' not in body

    def test_page_without_table_carries_no_bookings(self):
        """Die Hinweisseite am Ende hat keine Tabellenueberschrift."""
        from importers.vbr import page_body
        assert page_body("Sehr geehrte Kundin,\nSie haben eine Bankmitteilung\n") == ''

    def test_transaction_across_the_page_break_is_reassembled(self, parser):
        """Der gemeldete Fehler: Kopfzeile am Seitenende, Details auf der
        Folgeseite. Frueher wurde die Fussnote zum Empfaenger."""
        from importers.vbr import page_body
        seite1 = (self.HEADER + "17.01. 17.01. Lastschrift PN:931 42,08 S\n"
                  + self.FOOTER)
        seite2 = (self.HEADER + "Telefonanbieter Musterstadt GmbH\n"
                  "Kd-Nr.: 111, Rg-Nr.: 222, Ihre Rechnung\n"
                  "19.01. 19.01. Entgelt 5,00 S\nJahrespreis Karte\n" + self.FOOTER)

        rumpf = '\n'.join(page_body(s) for s in (seite1, seite2))
        txns = parser._parse_vbr_text(rumpf, YEAR)

        assert len(txns) == 2
        assert txns[0]['recipient'] == 'Telefonanbieter Musterstadt GmbH'
        assert 'Rg-Nr.: 222' in txns[0]['reference']
        assert txns[0]['amount'] == pytest.approx(-42.08)
        assert txns[1]['recipient'] == 'Jahrespreis Karte'

    def test_seitenweise_lesen_verlor_den_empfaenger(self, parser):
        """Gegenprobe: ohne den Zusammenbau bleibt nur Muell uebrig."""
        seite1 = (self.HEADER + "17.01. 17.01. Lastschrift PN:931 42,08 S\n"
                  + self.FOOTER)
        txns = parser._parse_vbr_text(seite1, YEAR)
        assert txns and 'Bitte beachten' in txns[0]['recipient']
