# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belegerkennung, Paket P3: gewöhnliche PDFs über ihren Text lesen (``receipt_reading.pdf_text``, ``text_rules``) und der
Kindprozess mit hartem Zeitlimit (``receipt_reading.worker``, ``isolated``).

Grundsatz der Regeln: lieber leer als geraten – jede Regel hat ihre Tests für das, was NICHT gelesen wird. Die Regeln laufen als
Tabellen-Tests direkt auf ``read_text`` (Seitentexte, ohne PDF); dazu echte PDFs (der Helfer ``make_pdf`` der Kontoauszug-Tests,
die Rechnungs-PDF der App ohne ihr eingebettetes XML), die Seitenwahl und Grenzen von ``extract_pdf_text`` und der echte Rundlauf
durch den Kindprozess.

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import io
import json
import logging
import os
import subprocess
import sys
import time
import types
import warnings

import pytest

import receipt_reading
from export.einvoice_model import _VAT_ID_RULES as MODEL_VAT_ID_RULES, tax_id_kind
from export.invoice_files import generate_invoice_documents, stored_invoice_document
from export.pdf_core import build_multi_page_pdf, escape_pdf_string
from receipt_reading import Reading, isolated, pdf_text, read_document, text_rules, worker
from tests.test_invoice_documents import _make_invoice, inv_db  # noqa: F401  (Fixture)
from tests.test_receipt_reading_einvoice import DTD_XML, ROOT, cii, ubl
from tests.test_receipt_reading_einvoice import make_pdf as hand_pdf
from tests.test_statement_import_guards import CLOSING_LINE, OPENING_LINE, A_BOOKING, B_BOOKING, make_pdf, one_page

OWN = ([], [])
OTHER_VAT_ID = 'DE987654321'
SELLER_VAT_ID = 'DE123456789'
ARABIC_DIGITS = ''.join(chr(0x0660 + digit) for digit in range(1, 10))              # ١٢٣٤٥٦٧٨٩: ASCII-Ziffern sind die einzigen


def read(*lines, own=OWN):
    """Die Zeilen als eine Seite durch die Textregeln."""
    return text_rules.read_text(['\n'.join(lines)], own)


def fields(reading):
    """``{Feld: Wert}`` der gelesenen Felder, ohne die Art."""
    return {name: entry['value'] for name, entry in reading.raw.get('fields', {}).items() if name != 'kind'}


# ── Beträge: Bezeichnung → Betrag ────────────────────────────────────────────

GROSS_LABELS = ['Rechnungsbetrag', 'Gesamtbetrag', 'Bruttobetrag', 'Endbetrag', 'Gesamtsumme', 'Summe brutto', 'Gesamt brutto',
                'Zahlbetrag', 'zu zahlen', 'Zu zahlender Betrag', 'Brutto', 'Total']
NET_LABELS = ['Summe netto', 'Nettobetrag', 'Gesamt netto', 'Nettosumme', 'Zwischensumme', 'Netto']


@pytest.mark.parametrize('label', GROSS_LABELS)
def test_every_gross_label_pairs_with_the_amount_behind_it(label):
    for line in (f'{label}: 119,00 EUR', f'{label.upper()} 119,00 €', f'{label.lower()}:119,00'):
        reading = read(line)
        assert (reading.gross_minor, reading.net_minor, reading.tax_minor) == (1190000, None, None), line
    assert read(f'{label}: 119,00 EUR').raw['fields']['gross_minor'] == {
        'value': 1190000, 'source': 'rule', 'quote': f'{label}: 119,00 EUR', 'page': 1}


@pytest.mark.parametrize('label', NET_LABELS)
def test_every_net_label_pairs_with_the_amount_behind_it(label):
    for line in (f'{label}: 100,00 EUR', f'{label.upper()} 100,00 €'):
        reading = read(line)
        assert (reading.net_minor, reading.gross_minor, reading.tax_minor) == (1000000, None, None), line


def test_a_label_is_a_whole_word_and_the_amount_must_stand_behind_it_in_the_same_line():
    for line in ('Gesamtbetragsangabe: 119,00 EUR', 'Subtotal 119,00 EUR', 'Bruttopreis 119,00 EUR', 'Nettopreis 100,00 EUR',
                 '119,00 EUR Gesamtbetrag', 'Gesamtbetrag', 'Mini-Gesamtsummen: 119,00'):
        assert read(line).status == 'empty', line
    assert read('Gesamtbetrag', '119,00 EUR').status == 'empty'                      # der Betrag steht in der nächsten Zeile
    assert read('Preis (Gesamtbetrag):119,00').gross_minor == 1190000                  # Satzzeichen davor und dahinter sind Grenzen


def test_only_the_best_rank_counts_for_gross():
    lines = ['Total: 50,00 EUR', 'Zahlbetrag: 60,00 EUR', 'Gesamtbetrag: 119,00 EUR']
    assert read(*lines).gross_minor == 1190000 and read(*lines).notes == []          # kein Konflikt: andere Ränge zählen nicht
    assert read(*lines[:2]).gross_minor == 600000
    assert read(lines[0]).gross_minor == 500000


def test_only_the_best_rank_counts_for_net():
    lines = ['Netto: 1,00 EUR', 'Zwischensumme: 2,00 EUR', 'Summe netto: 100,00 EUR']
    assert read(*lines).net_minor == 1000000 and read(*lines).notes == []
    assert read(*lines[:2]).net_minor == 20000
    assert read(lines[0]).net_minor == 10000


@pytest.mark.parametrize('line, gross, net', [
    ('Gesamtbetrag netto: 100,00 EUR', None, 1000000),                  # ein Nettobetrag, kein Brutto
    ('Gesamtbetrag zzgl. MwSt.: 100,00 EUR', None, None),
    ('Gesamtbetrag inkl. MwSt.: 119,00 EUR', 1190000, None),            # inkl. ist die Gegenbezeichnung des Nettos
    ('Gesamtbetrag (brutto): 119,00 EUR', 1190000, None),
    ('Nettobetrag brutto: 119,00 EUR', 1190000, None),                  # brutto steht vor dem Betrag: kein Netto
    ('Summe netto inkl. Rabatt: 100,00 EUR', None, None),
    ('Summe netto zzgl. Fracht: 100,00 EUR', None, 1000000),
    ('Zwischensumme brutto 119,00 EUR', 1190000, None),
])
def test_a_counter_label_between_label_and_amount_voids_the_pair(line, gross, net):
    reading = read(line)
    assert (reading.gross_minor, reading.net_minor) == (gross, net)


def test_two_labels_in_one_line_each_take_the_first_amount_behind_them():
    reading = read('Summe netto: 100,00 EUR Gesamtbetrag: 119,00 EUR')
    assert (reading.net_minor, reading.gross_minor) == (1000000, 1190000)
    assert read('Gesamtbetrag Summe netto 100,00 EUR').gross_minor is None             # die Gegenbezeichnung dazwischen


@pytest.mark.parametrize('text, minor', [
    ('119,00', 1190000), ('0,50', 5000), ('1.234,56', 12345600), ('12.345.678,90', 123456789000),
    ('1234567,89', 12345678900), ('-119,00', -1190000), ('-0,01', -100), ('999.999.999.999,99', 9999999999999900)])
def test_amounts_are_german_with_exactly_two_decimals(text, minor):
    reading = read(f'Gesamtbetrag: {text} EUR')
    assert reading.gross_minor == minor
    assert Reading.from_dict(reading.to_dict()) == reading                              # die Grenze für Beträge hält


@pytest.mark.parametrize('text', [
    '119', '119.00', '119,0', '119,000', '1,234.56', '1.23,45', '1.234.56,00', '١١٩,٠٠', '1.000.000.000.000,00',
    '19,00 %', '19,00%', '12 345,67', '1\N{NO-BREAK SPACE}234,56', '1\N{NARROW NO-BREAK SPACE}234,56', "1'234,56", '1\N{RIGHT SINGLE QUOTATION MARK}234,56', '\N{MINUS SIGN}119,00',
    '\N{MINUS SIGN} 119,00', '- 119,00', '\N{EN DASH}119,00', '\N{EM DASH} 119,00', '119,00-', '119,00\N{MINUS SIGN}', 'nan'])
def test_what_is_no_clean_amount_is_not_read(text):
    assert read(f'Gesamtbetrag: {text} EUR').gross_minor is None, text


def test_an_amount_next_to_other_amounts_in_columns_is_still_read():
    reading = read('Gesamtbetrag 100,00 19,00 119,00')
    assert reading.gross_minor == 1000000                                               # der ERSTE Betrag hinter der Bezeichnung
    assert read('Gesamtbetrag: 12,50 EUR Skonto 2,00 %').gross_minor == 125000
    assert read('Gesamtbetrag 5 119,00 EUR').gross_minor is None                         # 5 119,00: Menge und Preis oder 5.119,00?


# ── Beträge: das Vorzeichen muss eindeutig sein ──────────────────────────────

@pytest.mark.parametrize('text', [
    # davor: Minus, Gedankenstrich oder öffnende Klammer – dazwischen nur Leerraum und ein Währungszeichen
    '-€119,00', '-EUR 119,00', '- € 119,00', '-€ 119,00', '\N{MINUS SIGN}€119,00', '\N{EN DASH} EUR 119,00', '\N{EM DASH}$119,00',
    '(119,00)', '(€ 119,00)', '(EUR 119,00)', '( 119,00)', '(€119,00 EUR)',
    # dahinter: Minus oder ein Soll-/Haben-Zeichen – dazwischen nur Leerraum und ein Währungszeichen
    '119,00 -', '119,00-', '119,00 €-', '119,00 € -', '119,00 EUR-', '119,00 EUR -', '119,00 \N{MINUS SIGN}', '119,00\N{EN DASH}',
    '119,00 CR', '119,00 DR', '119,00 S', '119,00 H', '119,00 € S', '119,00 EUR CR', '119,00CR', '119,00 S.'])
def test_an_amount_without_an_unambiguous_sign_is_not_read(text):
    assert read(f'Gesamtbetrag: {text}').gross_minor is None, text


@pytest.mark.parametrize('text, minor', [
    ('-119,00', -1190000), ('€ -119,00', -1190000), ('EUR -119,00', -1190000), ('119,00', 1190000), ('119,00 EUR', 1190000),
    ('€ 119,00', 1190000), ('119,00 €', 1190000), ('119,00 EUR-Konto', 1190000),           # ein Bindestrich vor einem Buchstaben
    ('119,00 Skonto', 1190000), ('119,00 CRM', 1190000), ('119,00 € Haben', 1190000),       # ``S``, ``H``, ``CR`` nur als ganzes Zeichen
    ('119,00 -5,00', 1190000), ('119,00 -Konto', 1190000),                                  # das Minus gehört zur nächsten Zahl bzw. zum Wort
    ('119,00 (Skonto)', 1190000)])
def test_an_amount_with_a_clear_sign_or_none_at_all_is_read(text, minor):
    assert read(f'Gesamtbetrag: {text}').gross_minor == minor, text


def test_the_sign_rules_hold_for_every_amount_not_only_the_gross():
    reading = read('Summe netto: (100,00)', 'Gesamtbetrag: 119,00 EUR')
    assert (reading.net_minor, reading.gross_minor) == (None, 1190000)
    assert read('MwSt. 19%: 19,00-').tax_minor is None and read('MwSt. 19%: - 19,00').tax_minor is None
    assert read('MwSt. 19%: -19,00').tax_minor == -190000


def test_a_dash_behind_an_amount_voids_only_that_amount():
    reading = read('Gesamtbetrag: 119,00 EUR - zahlbar sofort', 'Summe netto: 100,00 EUR')
    assert (reading.gross_minor, reading.net_minor) == (None, 1000000)                      # das Minus hinter dem Betrag: nicht eindeutig
    assert read('Summe netto: 100,00 EUR Gesamtbetrag: 119,00 EUR').gross_minor == 1190000


# ── Steuer ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('word', ['MwSt.', 'MWST', 'mwst', 'USt.', 'UST', 'Umsatzsteuer', 'Mehrwertsteuer', 'zzgl. USt', '(MwSt)'])
def test_every_tax_word_marks_a_tax_line(word):
    reading = read(f'{word} 19%: 19,00 EUR')
    assert (reading.tax_minor, reading.tax_rate) == (190000, 0.19)


def test_one_rate_gives_the_amount_and_the_rate_with_their_line():
    reading = read('zzgl. 19% MwSt.: 19,00 EUR')
    assert (reading.tax_minor, reading.tax_rate) == (190000, 0.19)
    assert reading.raw['fields']['tax_minor'] == {'value': 190000, 'source': 'rule', 'quote': 'zzgl. 19% MwSt.: 19,00 EUR', 'page': 1}
    assert reading.raw['fields']['tax_rate'] == {'value': 0.19, 'source': 'rule', 'quote': 'zzgl. 19% MwSt.: 19,00 EUR', 'page': 1}
    assert 'tax_breakdown' not in reading.raw


@pytest.mark.parametrize('line, rate', [
    ('MwSt. 19%: 19,00 EUR', 0.19), ('MwSt. 19 %: 19,00 EUR', 0.19), ('MwSt. 19,00 %: 19,00 EUR', 0.19),
    ('MwSt. 7%: 19,00 EUR', 0.07), ('MwSt. 7,0 % 19,00 EUR', 0.07), ('zzgl. 7%MwSt. 19,00', 0.07),
    ('MwSt. 5,5%: 19,00 EUR', 0.055), ('MwSt. 0 %: 19,00 EUR', 0.0), ('MwSt. (16%) 19,00 EUR', 0.16)])
def test_the_rate_is_read_in_its_spellings(line, rate):
    assert read(line).tax_rate == rate


def test_a_percentage_is_no_amount_and_the_tax_is_the_amount_that_fits_the_rate():
    assert read('19% MwSt. auf 100,00 EUR: 19,00 EUR').tax_minor == 190000
    assert read('MwSt. 19,00 % auf 100,00 EUR 19,00 EUR').tax_minor == 190000
    reading = read('MwSt. 19,00 %')
    assert (reading.tax_minor, reading.tax_rate, reading.status) == (None, None, 'empty')


@pytest.mark.parametrize('line, tax, rate', [
    ('USt 19% 100,00 19,00 119,00', 190000, 0.19),               # Basis, Steuer, Brutto: nicht der letzte Betrag
    ('USt 19% 19,00 119,00', 190000, 0.19), ('MwSt 19 % 119,00 19,00', 190000, 0.19), ('MwSt 19 % 19,00 100,00', 190000, 0.19),
    ('USt 7% 7,00 107,00', 70000, 0.07), ('USt 7 % 100,00 7,00', 70000, 0.07), ('USt 0 % 100,00 0,00', 0, 0.0),
    ('USt 19% 100,00 19,01', 190100, 0.19), ('USt 19% 100,00 18,99', 189900, 0.19),         # auf den Cent genau: ein Cent Spielraum
    ('USt 19% 100,00 19,00 19,00', 190000, 0.19),                # derselbe Wert zweimal: eine Steuer
    ('Umsatzsteuer 19 % 1.000,00 190,00 1.190,00', 1900000, 0.19), ('MwSt. 19,00 % 100,00 EUR 19,00 EUR', 190000, 0.19),
    ('USt 19% 100,00 19,00 119,00 EUR', 190000, 0.19)])
def test_a_tax_line_with_several_amounts_takes_the_one_that_fits_the_rate_against_another_amount_of_the_line(line, tax, rate):
    reading = read(line)
    assert (reading.tax_minor, reading.tax_rate) == (tax, rate), line
    assert reading.raw['fields']['tax_minor'] == {'value': tax, 'source': 'rule', 'quote': line, 'page': 1}


@pytest.mark.parametrize('line', [
    'USt 19% 100,00 20,00 120,00',                               # keiner passt
    'USt 19% 100,00 19,00 18,99', 'USt 19% 100,00 19,00 19,01',  # zwei verschiedene passen: nichts wird geraten
    'USt 100,00 19,00 119,00', 'MwSt.: 19,00 119,00',            # kein Satz: keine Probe
    'MwSt 19% 7% 19,00 7,00', 'MwSt 19% (Ermäßigung 7%): 19,00 7,00',       # mehrere Sätze: keine Probe
    'USt 19% 100,00 19,00 119,00 1,00 2,00 3,00 4,00 5,00 6,00'])     # eine Tabellenzeile: mehr als MAX_LINE_AMOUNTS Beträge
def test_a_tax_line_with_several_amounts_that_does_not_decide_gives_no_tax(line):
    assert text_rules.MAX_LINE_AMOUNTS == 8
    reading = read(line)
    assert (reading.tax_minor, reading.tax_rate) == (None, None), line
    assert reading.notes == [] and 'tax_minor' not in reading.raw.get('fields', {})


@pytest.mark.parametrize('line', [
    '19 % MwSt auf 100,00 €', 'MwSt 19 % auf 100,00 EUR', '19% MwSt. auf 100,00', 'enthaltene MwSt 19 % aus 119,00',
    'MwSt 19 % von 100,00', 'USt auf 100,00', 'MwSt. 7 % AUF 100,00 EUR', 'MwSt 19 % VON 100,00', 'Umsatzsteuer: aus 119,00'])
def test_a_tax_line_whose_only_amount_follows_auf_von_or_aus_gives_no_tax_because_it_is_the_basis(line):
    reading = read(line)
    assert reading.tax_minor is None and reading.status == 'empty', line


@pytest.mark.parametrize('line, tax', [
    ('zzgl. 19 % MwSt.: 38,00 EUR', 380000), ('MwSt 19 %  19,00', 190000), ('MwSt.: 19,00 EUR', 190000),
    ('MwSt 19 % aufgeschlagen: 19,00', 190000), ('MwSt 19 % Ausland 19,00', 190000), ('MwSt 19 % Auslagen 19,00', 190000),  # nur als ganzes Wort
    ('Zahlung auf 19 % MwSt 19,00 €', 190000),                   # das „auf“ steht VOR dem Steuerwort, nicht zwischen ihm und dem Betrag
    ('100,00 € zzgl. 19 % USt 19,00 €', 190000)])                # ein Betrag VOR dem Steuerwort zählt nicht mit
def test_a_tax_line_with_exactly_one_amount_behind_the_tax_word_keeps_it(line, tax):
    assert read(line).tax_minor == tax, line


def test_the_limit_of_amounts_per_tax_line_decides_nothing_above_it(monkeypatch):
    line = 'USt 19% 1,00 2,00 3,00 4,00 5,00 6,00 7,00 19,00 100,00'                   # neun Beträge, genau einer passt (19,00 zu 100,00)
    assert read(line).tax_minor is None
    monkeypatch.setattr(text_rules, 'MAX_LINE_AMOUNTS', 9)
    assert read(line).tax_minor == 190000


def test_a_tax_line_with_several_amounts_gives_the_whole_reading_with_both_proofs():
    reading = read('Summe netto: 100,00 EUR', 'USt 19% 100,00 19,00 119,00', 'Gesamtbetrag: 119,00 EUR')
    assert (reading.net_minor, reading.tax_minor, reading.gross_minor, reading.tax_rate) == (1000000, 190000, 1190000, 0.19)
    assert reading.raw['checks'] == {'sum': 'ok', 'rate': 'ok'} and reading.notes == []
    assert reading.raw['fields']['tax_rate']['quote'] == 'USt 19% 100,00 19,00 119,00'
    only = read('19 % MwSt auf 100,00 €', 'Gesamtbetrag: 119,00 EUR')                  # die Basis ist nicht die Steuer
    assert (only.tax_minor, only.gross_minor, only.raw['checks']['sum']) == (None, 1190000, 'incomplete')


def test_the_tax_lines_of_several_rates_with_several_amounts_make_the_breakdown():
    reading = read('Summe netto: 200,00 EUR', 'USt 19% 100,00 19,00 119,00', 'USt 7% 100,00 7,00 107,00', 'Gesamtbetrag: 226,00 EUR')
    assert (reading.tax_minor, reading.tax_rate, reading.gross_minor) == (260000, None, 2260000)
    assert [(entry['rate'], entry['tax_minor']) for entry in reading.raw['tax_breakdown']] == [(0.19, 190000), (0.07, 70000)]
    assert reading.raw['checks'] == {'sum': 'ok', 'rate': 'incomplete'}


def test_a_tax_line_without_a_rate_gives_the_amount_without_a_rate():
    reading = read('MwSt.: 19,00 EUR')
    assert (reading.tax_minor, reading.tax_rate) == (190000, None) and 'tax_rate' not in reading.raw['fields']
    assert read('MwSt. 19% (Ermäßigung 7%): 19,00 EUR').tax_rate is None             # zwei Sätze in einer Zeile: keiner gilt


def test_several_rates_give_their_sum_and_a_breakdown_but_no_single_rate():
    reading = read('Summe netto: 200,00 EUR', 'zzgl. 19% MwSt.: 19,00 EUR', 'zzgl. 7% MwSt.: 7,00 EUR',
                   'Gesamtbetrag: 226,00 EUR')
    assert (reading.tax_minor, reading.tax_rate, reading.gross_minor) == (260000, None, 2260000)
    assert 'tax_rate' not in reading.raw['fields']
    assert reading.raw['tax_breakdown'] == [
        {'rate': 0.19, 'category': None, 'basis_minor': None, 'tax_minor': 190000, 'quote': 'zzgl. 19% MwSt.: 19,00 EUR', 'page': 1},
        {'rate': 0.07, 'category': None, 'basis_minor': None, 'tax_minor': 70000, 'quote': 'zzgl. 7% MwSt.: 7,00 EUR', 'page': 1}]
    assert reading.raw['checks'] == {'sum': 'ok', 'rate': 'incomplete'}                # ohne Basis je Satz keine Satzprobe
    assert Reading.from_dict(reading.to_dict()) == reading


def test_the_same_tax_line_twice_counts_once_and_a_summary_line_without_rate_is_the_same_tax():
    assert read('MwSt. 19%: 19,00 EUR', 'MwSt. 19%: 19,00 EUR').tax_minor == 190000
    reading = read('zzgl. 19% MwSt.: 19,00 EUR', 'zzgl. 7% MwSt.: 7,00 EUR', 'enthaltene MwSt. gesamt: 26,00 EUR')
    assert reading.tax_minor == 260000 and reading.notes == []                         # nicht 52,00: die Summe wird nicht addiert


@pytest.mark.parametrize('line', [
    'USt-IdNr.: DE123456789 19,00', 'USt.-ID 19,00', 'UStID 19,00', 'Umsatzsteuer-ID 19,00', 'Umsatzsteuer-Identifikationsnummer 19,00',
    'MwSt. Steuernummer 19,00', 'USt Steuer-Nr. 19,00', 'Bust 19,00 EUR', 'Mwsts 19,00 EUR', 'Mustermwst 19,00 EUR'])
def test_an_identifier_line_or_a_word_that_only_contains_the_tax_word_is_no_tax_line(line):
    assert read(line).tax_minor is None, line


@pytest.mark.parametrize('line', [
    'Gesamtbetrag inkl. 19% MwSt. 119,00 EUR', 'Rechnungsbetrag (inkl. 19 % MwSt.) 119,00 EUR', 'Bruttobetrag inkl. 19% USt.: 119,00',
    'Nettobetrag zzgl. 19 % MwSt. 100,00 EUR', 'Summe netto (zzgl. 19% MwSt.): 100,00 EUR'])
def test_a_line_whose_amount_belongs_to_a_gross_or_net_label_gives_no_tax(line):
    """Der Betrag gehört der Bezeichnung davor: ``119,00`` ist hier nicht die Steuer."""
    reading = read(line)
    assert reading.tax_minor is None and (reading.gross_minor is not None or reading.net_minor is not None)


def test_an_amount_before_the_tax_word_is_not_the_tax():
    assert read('119,00 EUR inkl. 19% MwSt.').tax_minor is None
    assert read('Summe 119,00 EUR, darin 19% MwSt.').tax_minor is None


def test_two_different_amounts_for_one_rate_are_a_conflict_unless_the_sum_decides():
    lines = ['zzgl. 19% MwSt.: 19,00 EUR', 'zzgl. 19% MwSt.: 21,00 EUR']
    reading = read(*lines)
    assert reading.tax_minor is None and reading.notes == ['conflict_tax_minor']
    decided = read('Summe netto: 100,00 EUR', *lines, 'Gesamtbetrag: 119,00 EUR')
    assert (decided.net_minor, decided.tax_minor, decided.gross_minor) == (1000000, 190000, 1190000)
    assert decided.notes == ['by_checksum'] and decided.raw['checks'] == {'sum': 'ok', 'rate': 'ok'}


def test_too_many_rates_or_combinations_leave_the_tax_empty():
    many = [f'MwSt. {rate}%: 1,00 EUR' for rate in range(1, text_rules.MAX_RATES + 2)]
    reading = read(*many)
    assert reading.tax_minor is None and reading.notes == ['conflict_tax_minor']


# ── Summenprobe ──────────────────────────────────────────────────────────────

def test_the_sum_proof_is_ok_mismatch_or_incomplete():
    ok = read('Summe netto: 100,00 EUR', 'zzgl. 19% MwSt.: 19,00 EUR', 'Gesamtbetrag: 119,00 EUR')
    assert ok.raw['checks'] == {'sum': 'ok', 'rate': 'ok'}
    off = read('Summe netto: 100,00 EUR', 'zzgl. 19% MwSt.: 19,00 EUR', 'Gesamtbetrag: 119,01 EUR')
    assert off.raw['checks']['sum'] == 'mismatch' and off.gross_minor == 1190100                # gelesen wird es trotzdem
    for lines in (('Summe netto: 100,00 EUR', 'Gesamtbetrag: 119,00 EUR'), ('zzgl. 19% MwSt.: 19,00 EUR', 'Gesamtbetrag: 119,00 EUR'),
                  ('Summe netto: 100,00 EUR', 'zzgl. 19% MwSt.: 19,00 EUR')):
        assert read(*lines).raw['checks']['sum'] == 'incomplete', lines


@pytest.mark.parametrize('net, tax, expected', [
    ('100,00', '19,00', 'ok'), ('100,00', '19,01', 'ok'), ('100,00', '18,99', 'ok'), ('100,00', '19,02', 'mismatch'),
    ('99,99', '19,00', 'ok'), ('100,00', '190,00', 'mismatch')])
def test_the_rate_proof_allows_one_cent(net, tax, expected):
    reading = read(f'Summe netto: {net} EUR', f'zzgl. 19% MwSt.: {tax} EUR')
    assert reading.raw['checks']['rate'] == expected


def test_the_rate_proof_needs_exactly_one_rate_and_the_net():
    assert read('zzgl. 19% MwSt.: 19,00 EUR').raw['checks']['rate'] == 'incomplete'                   # kein Netto
    assert read('Summe netto: 100,00 EUR', 'MwSt.: 19,00 EUR').raw['checks']['rate'] == 'incomplete'  # kein Satz
    assert read('Summe netto: 100,00 EUR', 'MwSt. 0%: 0,00 EUR').raw['checks']['rate'] == 'ok'


# ── Mehrere Kandidaten ───────────────────────────────────────────────────────

def test_the_same_value_twice_is_no_conflict():
    reading = read('Gesamtbetrag: 119,00 EUR', 'Gesamtbetrag: 119,00 EUR', 'Rechnungsdatum: 04.03.2026',
                   'Rechnungsdatum: 04.03.2026')
    assert (reading.gross_minor, reading.doc_date, reading.notes) == (1190000, '2026-03-04', [])
    assert reading.raw['fields']['gross_minor']['quote'] == 'Gesamtbetrag: 119,00 EUR'


def test_two_different_totals_leave_the_field_empty_and_say_so():
    reading = read('Gesamtbetrag: 119,00 EUR', 'Gesamtbetrag: 129,00 EUR')
    assert (reading.status, reading.gross_minor, reading.notes) == ('empty', None, ['conflict_gross_minor'])
    assert reading.raw == {}


def test_two_nets_are_a_conflict_too():
    reading = read('Summe netto: 100,00 EUR', 'Summe netto: 90,00 EUR', 'Rechnungsdatum: 04.03.2026')
    assert (reading.net_minor, reading.doc_date, reading.notes) == (None, '2026-03-04', ['conflict_net_minor'])


def test_the_sum_proof_decides_when_exactly_one_combination_passes():
    base = ['Summe netto: 100,00 EUR', 'zzgl. 19% MwSt.: 19,00 EUR']
    reading = read(*base, 'Gesamtbetrag: 119,00 EUR', 'Gesamtbetrag: 129,00 EUR')
    assert (reading.gross_minor, reading.notes) == (1190000, ['by_checksum'])
    net = read('Summe netto: 100,00 EUR', 'Summe netto: 90,00 EUR', 'zzgl. 19% MwSt.: 19,00 EUR', 'Gesamtbetrag: 119,00 EUR')
    assert (net.net_minor, net.notes) == (1000000, ['by_checksum'])
    assert net.raw['fields']['net_minor']['quote'] == 'Summe netto: 100,00 EUR'      # das Zitat der gewählten Zeile


def test_the_sum_proof_does_not_decide_when_no_or_several_combinations_pass():
    none = read('Summe netto: 100,00 EUR', 'zzgl. 19% MwSt.: 10,00 EUR', 'Gesamtbetrag: 119,00 EUR', 'Gesamtbetrag: 129,00 EUR')
    assert (none.gross_minor, none.net_minor, none.tax_minor) == (None, 1000000, 100000)
    assert none.notes == ['conflict_gross_minor']
    several = read('Summe netto: 100,00 EUR', 'Summe netto: 200,00 EUR', 'zzgl. 19% MwSt.: 19,00 EUR', 'zzgl. 19% MwSt.: 38,00 EUR',
                   'Gesamtbetrag: 119,00 EUR', 'Gesamtbetrag: 238,00 EUR')
    assert (several.gross_minor, several.net_minor, several.tax_minor) == (None, None, None)
    assert several.notes == ['conflict_gross_minor', 'conflict_net_minor', 'conflict_tax_minor']
    missing = read('Gesamtbetrag: 119,00 EUR', 'Gesamtbetrag: 129,00 EUR', 'Summe netto: 100,00 EUR')
    assert missing.gross_minor is None and missing.notes == ['conflict_gross_minor']           # ohne Steuer keine Probe


def test_more_candidates_than_the_limit_are_never_tried_against_each_other():
    totals = [f'Gesamtbetrag: {n},00 EUR' for n in range(1, text_rules.MAX_CANDIDATES + 2)]
    reading = read('Summe netto: 100,00 EUR', 'zzgl. 19% MwSt.: 19,00 EUR', *totals, 'Gesamtbetrag: 119,00 EUR')
    assert reading.gross_minor is None and 'conflict_gross_minor' in reading.notes and 'by_checksum' not in reading.notes


# ── Datum ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('line, iso', [
    ('Rechnungsdatum: 04.03.2026', '2026-03-04'), ('Rechnungsdatum 4.3.2026', '2026-03-04'), ('Rechnungsdatum: 2026-03-04', '2026-03-04'),
    ('RECHNUNGSDATUM:04.03.2026', '2026-03-04'), ('Belegdatum: 04.03.2026', '2026-03-04'), ('Ausstellungsdatum: 4.3.2026', '2026-03-04'),
    ('Datum der Rechnung: 04.03.2026', '2026-03-04'), ('Datum: 04.03.2026', '2026-03-04'), ('Berlin, Datum 29.02.2024', '2024-02-29'),
    ('Rechnungsdatum 01.01.1990', '1990-01-01'), ('Rechnungsdatum 31.12.2100', '2100-12-31')])
def test_the_date_is_read_in_both_forms_as_iso(line, iso):
    reading = read(line)
    assert reading.doc_date == iso
    assert reading.raw['fields']['doc_date'] == {'value': iso, 'source': 'rule', 'quote': line, 'page': 1}


@pytest.mark.parametrize('line', [
    'Rechnungsdatum: 31.02.2026', 'Rechnungsdatum: 04.13.2026', 'Rechnungsdatum: 00.03.2026', 'Rechnungsdatum: 29.02.2025',
    'Rechnungsdatum: 01.01.1989', 'Rechnungsdatum: 01.01.2101', 'Rechnungsdatum: 2026-13-01', 'Rechnungsdatum: 2026-02-30',
    'Rechnungsdatum: 04.03.26', 'Rechnungsdatum: 04. März 2026', 'Rechnungsdatum 04.03.20260', 'Rechnungsdatum 104.03.2026',
    'Rechnungsdatum: ١٢.٠٣.٢٠٢٦', 'Rechnungsdatum: 31.02.2026 04.03.2026', 'Datum', '04.03.2026 Rechnungsdatum'])
def test_what_is_no_valid_calendar_date_is_not_read(line):
    assert read(line).doc_date is None, line


@pytest.mark.parametrize('line', [
    'Leistungsdatum: 04.03.2026', 'Rechnungsdatum / Leistungsdatum: 04.03.2026', 'Datum der Lieferung: 04.03.2026',
    'Lieferdatum 04.03.2026', 'Datum Fällig: 04.03.2026', 'Datum Faellig am 04.03.2026', 'Datum zahlbar bis 04.03.2026',
    'Datum Bestellung 04.03.2026', 'Datum Auftrag 04.03.2026', 'Datum Valuta 04.03.2026', 'Datum Leistungszeitraum 04.03.2026',
    'Rechnungsdatum 04.03.2026 zahlbar bis 18.03.2026'])
def test_a_line_about_delivery_due_or_order_dates_is_never_the_invoice_date(line):
    assert read(line).doc_date is None, line


def test_the_best_date_rank_counts():
    lines = ['Datum: 01.01.2026', 'Belegdatum: 02.02.2026', 'Rechnungsdatum: 04.03.2026']
    assert read(*lines).doc_date == '2026-03-04' and read(*lines).notes == []
    assert read(*lines[:2]).doc_date == '2026-02-02'
    assert read(lines[0]).doc_date == '2026-01-01'


def test_different_dates_in_the_same_rank_are_a_conflict():
    reading = read('Rechnungsdatum: 04.03.2026', 'Rechnungsdatum: 05.03.2026')
    assert (reading.doc_date, reading.notes) == (None, ['conflict_doc_date'])
    same = read('Rechnungsdatum: 04.03.2026', 'Rechnungsdatum: 2026-03-04')
    assert (same.doc_date, same.notes) == ('2026-03-04', [])                          # dasselbe Datum in zwei Schreibweisen


# ── Rechnungsnummer ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('line, number', [
    ('Rechnungsnummer: 2026-0042', '2026-0042'), ('Rechnungsnummer 2026-0042', '2026-0042'), ('Rechnungs-Nr.: R-2026/001', 'R-2026/001'),
    ('Rechnung Nr. 4711', '4711'), ('Rechnung Nr: 4711', '4711'), ('Rechnungsnr. 4711', '4711'), ('Rechnung-Nr: A_17.3', 'A_17.3'),
    ('Invoice No: INV-0099', 'INV-0099'), ('Invoice No. 12', '12'), ('RECHNUNGSNUMMER: ab12', 'ab12'),
    ('Rechnungsnummer: 12345.', '12345'), ('Muster GmbH Rechnungsnummer 4711 vom 04.03.2026', '4711'),
    ('Rechnungs-Nr.: 26R010', '26R010')])
def test_the_invoice_number_is_the_token_behind_the_label(line, number):
    reading = read(line)
    assert reading.invoice_number == number
    assert reading.raw['fields']['invoice_number'] == {'value': number, 'source': 'rule', 'quote': line, 'page': 1}


@pytest.mark.parametrize('line', [
    'Rechnungsnummer:', 'Rechnungsnummer: abc', 'Rechnungsnummer: 04.03.2026', 'Rechnungsnummer: 2026-03-04', 'Rechnungsnummer: 4.3.26',
    'Rechnungsnummer: 31.02.2026', 'Bitte die Rechnungsnummer angeben', 'Rechnungsnummer-4711', 'Meine Rechnungsnummern 12',
    'Rechnungsnummer: ' + 'A1' * 21, 'Rechnungsnummer: /4711', 'Rechnungsnummer: ١٢٣'])
def test_a_token_without_a_digit_a_date_a_wrong_start_or_too_long_is_no_number(line):
    assert read(line).invoice_number is None, line


def test_different_numbers_are_a_conflict_and_the_same_number_twice_is_not():
    assert read('Rechnungsnummer: R-1', 'Rechnungs-Nr.: R-2').notes == ['conflict_invoice_number']
    same = read('Rechnungsnummer: R-1', 'Rechnungs-Nr.: R-1')
    assert (same.invoice_number, same.notes) == ('R-1', [])
    assert read('Rechnungsnummer: R-1', 'Rechnungsnummer: r-1').notes == ['conflict_invoice_number']     # die Schreibweise zählt


# ── USt-IdNr. ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('line, vat_id', [
    ('USt-IdNr.: DE123456789', 'DE123456789'), ('USt-IdNr. DE123456789', 'DE123456789'), ('USt-ID: DE 123 456 789', 'DE123456789'),
    ('UStIdNr DE123456789', 'DE123456789'), ('UST-IDNR.: de123456789', 'DE123456789'), ('USt.-ID-Nr.: DE-123456789', 'DE123456789'),
    ('Umsatzsteuer-ID: DE123456789', 'DE123456789'), ('Umsatzsteuer-Identifikationsnummer DE123456789', 'DE123456789'),
    ('UID: ATU12345678', 'ATU12345678'), ('UID-Nr. ATU 12345678', 'ATU12345678'), ('VAT ID: NL123456789B01', 'NL123456789B01'),
    ('VAT No. NL 123456789 B01', 'NL123456789B01'), ('VAT: DE123456789', 'DE123456789'), ('VAT Number: FR12345678901', 'FR12345678901'),
    ('UID: CHE-123.456.789 MWST', 'CHE123456789'),
    ('USt-IdNr.: DE123456789, Steuernummer 12/345/67890', 'DE123456789'),
    ('12345 Musterstadt USt-IdNr.: DE123456789 IBAN: DE89 3704 0044 0532 0130 00', 'DE123456789'),           # der Fuß der App-PDF
    ('Steuer: DE 123 456 789', 'DE123456789'), ('Bankverbindung DE123456789 Muster', 'DE123456789'),          # ohne Bezeichnung
    ('Tel. 030 12 DE 123456789 Fax', 'DE123456789'), ('DE123 456 789', 'DE123456789')])
def test_the_vat_id_is_read_with_or_without_a_label(line, vat_id):
    reading = read(line)
    assert reading.sender_vat_id == vat_id
    assert reading.raw['fields']['sender_vat_id']['value'] == vat_id and reading.raw['fields']['sender_vat_id']['quote'] == line


@pytest.mark.parametrize('line', [
    'USt-IdNr.: DE12345678', 'USt-IdNr.: DE1234567890', 'USt-IdNr.: XX123456789', 'USt-IdNr.: 123456789', 'USt-IdNr.: ',
    'St.-Nr.: 12/345/67890', 'Steuernummer 123/456/78901', 'IBAN: DE89 3704 0044 0532 0130 00', 'IBAN DE89370400440532013000',
    'USt-IdNr.: DE' + ARABIC_DIGITS, 'VAT included', 'xDE123456789', 'DE123456789x', 'DE 1234 56789'])
def test_what_is_no_vat_id_is_not_read(line):
    assert read(line).sender_vat_id is None, line


def test_an_own_vat_id_is_never_the_senders_whatever_its_spelling():
    lines = ['Ihre USt-IdNr.: DE987654321', 'USt-IdNr.: DE123456789']
    assert read(*lines, own=([], [OTHER_VAT_ID])).sender_vat_id == 'DE123456789'
    assert read(*lines, own=([], ['de 987 654 321'])).sender_vat_id == 'DE123456789'      # die Schreibweise der eigenen Nummer
    assert read(lines[0], own=([], [OTHER_VAT_ID])).sender_vat_id is None
    assert read('DE 987 654 321', own=([], [OTHER_VAT_ID])).sender_vat_id is None          # auch ohne Bezeichnung


def test_two_different_foreign_vat_ids_are_a_conflict_and_the_same_one_twice_is_not():
    reading = read('USt-IdNr.: DE123456789', 'Ihre USt-IdNr.: DE987654321')
    assert (reading.sender_vat_id, reading.notes) == (None, ['conflict_sender_vat_id'])
    same = read('USt-IdNr.: DE123456789', 'DE 123 456 789 Seite 2')
    assert (same.sender_vat_id, same.notes) == ('DE123456789', [])


def test_the_vat_rules_are_the_ones_of_the_einvoice_model():
    """``text_rules`` kopiert die Tabelle, weil ``export.einvoice_model`` das ganze ``db``-Paket nachzieht – hier bleiben beide gleich."""
    assert text_rules._VAT_ID_RULES == MODEL_VAT_ID_RULES
    samples = ['DE123456789', 'DE 123 456 789', 'de-123.456.789', 'DE12345678', 'DE1234567890', 'ATU12345678', 'AT12345678', 'NL123456789B01',
               'NL123456789B1', 'CHE-123.456.789', 'CHE123456789MWST', 'CHE123456789XYZ', 'FR12345678901', 'FR1A345678901', 'IT12345678901',
               'EL123456789', 'GB123456789', 'GBGD123', 'NO123456789MVA', 'NO 123456789', 'XX123456789', 'HRB 12345', '12/345/67890', '',
               'IE1234567T', 'IE1A34567T', 'ES A1234567B', 'ESX1234567Y', 'DE' + ARABIC_DIGITS]
    for sample in samples:
        kind, value = tax_id_kind(sample)
        assert text_rules.vat_id_value(sample) == (value if kind == 'vat' else None), sample


# ── Absender ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('line', [
    'Muster GmbH', 'Muster AG', 'Muster UG (haftungsbeschränkt)', 'Muster KG', 'Muster OHG', 'Muster GbR', 'Max Muster e.K.',
    'Muster Verwaltungs mbH', 'Muster SE', 'Muster eG', 'Muster e.V.', 'Muster Ltd', 'Muster Inc', 'Muster GmbH & Co. KG',
    'MUSTER GMBH', 'muster gmbh'])
def test_a_line_with_a_legal_form_as_a_whole_word_is_the_sender(line):
    reading = read(line, 'Rechnung')
    assert reading.sender_name == line
    assert reading.raw['fields']['sender_name'] == {'value': line, 'source': 'rule', 'quote': line, 'page': 1}


@pytest.mark.parametrize('line', ['Mustertag', 'Muster Tagung 5', 'Eggert Schmidt', 'Musterag', 'Muster Seen', 'Muster Egon', 'Rechnung',
                                  'Muster G.m.b.H.', 'Muster AGB'])
def test_a_word_that_only_contains_a_legal_form_is_no_sender(line):
    assert read(line).sender_name is None


@pytest.mark.parametrize('line, name', [
    ('Muster GmbH · Musterstraße 1 · 12345 Musterstadt', 'Muster GmbH'), ('Muster GmbH | Musterstraße 1', 'Muster GmbH'),
    ('Muster GmbH – Musterstraße 1', 'Muster GmbH'), ('Muster GmbH, Musterstraße 1, 12345 Musterstadt', 'Muster GmbH'),
    ('Musterstraße 1 · Muster Handel AG', 'Muster Handel AG'), ('Muster GmbH & Co. KG, Musterweg 3', 'Muster GmbH & Co. KG'),
    ('Muster-Handel GmbH - Musterweg 3', 'Muster-Handel GmbH - Musterweg 3')])               # der Bindestrich trennt nicht
def test_the_sender_is_the_section_with_the_legal_form(line, name):
    assert read(line).sender_name == name


@pytest.mark.parametrize('line', [
    '12345 Musterstadt GmbH', 'Muster GmbH 12345', 'Muster GmbH Tel: 030 12', 'Muster GmbH info@example.com', 'Muster GmbH E-Mail: info',
    'Muster GmbH Steuernummer 12345678', 'X' * 80 + ' GmbH', 'Kontakt: Muster GmbH'])
def test_a_section_with_a_postcode_a_colon_an_at_sign_or_too_long_is_no_name(line):
    assert read(line).sender_name is None


def test_a_name_of_exactly_the_limit_is_taken():
    exact = 'X' * (text_rules.MAX_SENDER_CHARS - 5) + ' GmbH'
    assert len(exact) == text_rules.MAX_SENDER_CHARS and read(exact).sender_name == exact
    assert read(exact + 'e').sender_name is None


def test_the_own_company_is_never_the_sender_but_another_one_is():
    assert read('Muster GmbH', own=(['Muster GmbH'], [])).sender_name is None
    assert read('MUSTER   gmbh', own=(['Muster GmbH'], [])).sender_name is None                  # Groß-/Kleinschreibung, Leerraum
    assert read('Muster GmbH & Co. KG', own=(['Muster GmbH'], [])).sender_name is None            # der eigene Name steckt darin
    assert read('Muster GmbH', 'Beispiel AG', own=(['Muster GmbH'], [])).sender_name == 'Beispiel AG'
    assert read('Muster GmbH', own=(['', '  ', 'Beispiel AG'], [])).sender_name == 'Muster GmbH'  # ein leerer Name steckt nicht in jedem


def test_only_the_first_fifteen_lines_of_the_first_page_name_the_sender():
    filler = [f'Zeile {n}' for n in range(14)]
    assert read(*filler, 'Muster GmbH').sender_name == 'Muster GmbH'
    assert read(*filler, 'Zeile 14', 'Muster GmbH').sender_name is None
    assert text_rules._lines(['\n'.join(filler + ['Muster GmbH'])])[14].text == 'Muster GmbH'
    assert text_rules.read_text(['Zeile 1', 'Muster GmbH'], OWN).sender_name is None            # Seite 2 nennt keinen Absender


def test_two_candidates_are_decided_by_the_foot_of_the_last_page_or_are_a_conflict():
    head = ['Alpha GmbH', 'Beta AG', 'Rechnung']
    both = text_rules.read_text(['\n'.join(head), 'Seite 2\nBeta AG · Geschäftsführer Muster'], OWN)
    assert both.sender_name == 'Beta AG' and both.notes == []
    assert both.raw['fields']['sender_name']['quote'] == 'Beta AG' and both.raw['fields']['sender_name']['page'] == 1
    neither = read(*head)
    assert (neither.sender_name, neither.notes) == (None, ['conflict_sender_name'])
    twice = text_rules.read_text(['\n'.join(head), 'Alpha GmbH\nBeta AG'], OWN)
    assert (twice.sender_name, twice.notes) == (None, ['conflict_sender_name'])
    assert read('Alpha GmbH', 'Alpha GmbH', 'alpha  gmbh').notes == []                            # dieselbe Firma dreimal


def test_the_footer_of_the_app_does_not_hide_the_sender():
    """Die Spalten der Fußzeile stehen in einer Zeile: ``Muster GmbH Tel: …`` und ``… Bank: Testbank AG`` sind keine Namen."""
    reading = read('Muster GmbH · Musterstraße 1 · 12345 Musterstadt', 'Malermeister Pinsel Rechnung',
                   'Muster GmbH Tel: 030 1234567 Bankverbindung', 'Musterstraße 1 E-Mail: info@example.com Bank: Testbank AG')
    assert (reading.sender_name, reading.notes) == ('Muster GmbH', [])


# ── Art, Währung ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('line', ['Gutschrift', 'GUTSCHRIFT Nr. 5', 'Stornorechnung zu R-1', 'Rechnungskorrektur 2', 'Gutschriftsanzeige'])
def test_a_line_that_begins_with_a_credit_word_makes_a_credit_note(line):
    reading = read(line, 'Gesamtbetrag: -119,00 EUR')
    assert reading.kind == 'credit_note' and reading.gross_minor == -1190000
    assert reading.raw['fields']['kind'] == {'value': 'credit_note', 'source': 'rule', 'quote': line, 'page': 1}


def test_the_kind_is_an_invoice_otherwise_and_only_the_first_twenty_lines_count():
    reading = read('Rechnung', 'Gesamtbetrag: 119,00 EUR')
    assert reading.kind == 'invoice' and reading.raw['fields']['kind'] == {'value': 'invoice', 'source': 'default'}
    assert read('Zur Gutschrift siehe unten', 'Gesamtbetrag: 119,00 EUR').kind == 'invoice'       # nicht am Zeilenanfang
    filler = [f'Zeile {n}' for n in range(text_rules.KIND_LINES - 1)]
    assert read(*filler, 'Gutschrift', 'Gesamtbetrag: 1,00 EUR').kind == 'credit_note'
    assert read(*filler, 'Zeile 19', 'Gutschrift', 'Gesamtbetrag: 1,00 EUR').kind == 'invoice'
    assert read('Gutschrift').kind is None                                                          # nichts gelesen: keine Art


def test_the_currency_is_euro_when_an_amount_is_read_and_the_text_names_euro():
    for line in ('Gesamtbetrag: 119,00 €', 'Gesamtbetrag: 119,00 EUR', 'Gesamtbetrag: EUR 119,00', 'Gesamtbetrag: € 119,00'):
        reading = read(line)
        assert reading.currency == 'EUR' and reading.raw['fields']['currency']['quote'] == line, line
    elsewhere = read('Preise in EUR', 'Gesamtbetrag: 119,00')
    assert elsewhere.currency == 'EUR' and elsewhere.raw['fields']['currency']['quote'] == 'Preise in EUR'
    assert read('Gesamtbetrag: 119,00').currency is None and read('Gesamtbetrag: 119,00').gross_minor == 1190000   # Beträge bleiben
    assert read('Rechnungsdatum: 04.03.2026', 'Betrag in EUR').currency is None                  # kein Betrag gelesen: keine Währung
    assert read('Gesamtbetrag: 119,00 EURO').currency is None and read('Gesamtbetrag: 119,00 NEUR').currency is None


@pytest.mark.parametrize('line', [
    'Gesamtbetrag: $ 119,00', 'Gesamtbetrag: $119,00', 'Gesamtbetrag: 119,00 $', 'Gesamtbetrag: 119,00 USD', 'Gesamtbetrag: USD 119,00',
    'Gesamtbetrag: 119,00 CHF', 'Gesamtbetrag: CHF 119,00', 'Gesamtbetrag: 119,00 GBP', 'Gesamtbetrag: 119,00 £', 'Gesamtbetrag: £ 119,00'])
def test_amounts_in_another_currency_stay_empty_and_say_so(line):
    reading = read('Rechnungsdatum: 04.03.2026', line)
    assert (reading.gross_minor, reading.net_minor, reading.tax_minor, reading.currency) == (None, None, None, None)
    assert reading.notes == ['foreign_currency'] and reading.doc_date == '2026-03-04'              # was keine Währung hat, bleibt
    assert set(reading.raw['fields']) == {'doc_date', 'kind'}


def test_another_currency_next_to_euro_or_not_at_an_amount_changes_nothing():
    mixed = read('Gesamtbetrag: 119,00 EUR', 'Umrechnung: 129,00 USD')
    assert mixed.gross_minor == 1190000 and mixed.currency == 'EUR' and mixed.notes == []
    words = read('Alle Preise in USD', 'Gesamtbetrag: 119,00')
    assert words.gross_minor == 1190000 and words.currency is None


EURO_LINES = ['Gesamtbetrag: 119,00 €', 'Gesamtbetrag: 119,00 EUR', 'Gesamtbetrag: EUR 119,00', 'Gesamtbetrag: € 119,00',
              'Gesamtbetrag: 119,00€', 'Gesamtbetrag: EUR119,00', 'Gesamtbetrag: 119,00 EUR/Stk']
FOREIGN_LINES = [line for line in (
    'Gesamtbetrag: $ 119,00', 'Gesamtbetrag: $119,00', 'Gesamtbetrag: 119,00 $', 'Gesamtbetrag: 119,00 USD', 'Gesamtbetrag: USD 119,00',
    'Gesamtbetrag: 119,00 CHF', 'Gesamtbetrag: CHF 119,00', 'Gesamtbetrag: 119,00 GBP', 'Gesamtbetrag: 119,00 £', 'Gesamtbetrag: £ 119,00',
    'Gesamtbetrag: 119,00USD', 'Gesamtbetrag: GBP119,00')]


@pytest.mark.parametrize('line', EURO_LINES)
def test_a_euro_sign_or_code_right_at_the_amount_makes_it_euro_and_its_line_the_quote(line):
    for other in ([], ['Preise in EUR'], ['Konto: USD-Konto']):
        reading = read(*other, line)
        assert (reading.gross_minor, reading.currency, reading.notes) == (1190000, 'EUR', []), (other, line)
        assert reading.raw['fields']['currency'] == {'value': 'EUR', 'source': 'rule', 'quote': line, 'page': 1}


@pytest.mark.parametrize('line', FOREIGN_LINES)
def test_a_foreign_sign_or_code_right_at_the_gross_voids_the_amounts_even_when_the_text_names_euro_elsewhere(line):
    for other in ([], ['IBAN EUR-Konto'], ['Alle Preise in EUR'], ['Konto: EUR'], ['Zahlung in €'], ['Netto in EUR']):
        reading = read('Rechnungsdatum: 04.03.2026', *other, line)
        assert (reading.gross_minor, reading.net_minor, reading.tax_minor, reading.currency) == (None, None, None, None), (other, line)
        assert reading.notes == ['foreign_currency'] and reading.doc_date == '2026-03-04', (other, line)
        assert 'tax_rate' not in reading.raw['fields'] and 'tax_breakdown' not in reading.raw and 'currency' not in reading.raw['fields']


def test_what_stands_at_the_gross_decides_not_what_stands_at_another_amount():
    assert read('Gesamtbetrag: 119,00 EUR USD 5,00').gross_minor == 1190000                  # das EUR steht am Brutto: Euro
    assert read('Netto 100,00 EUR', 'Gesamtbetrag: 119,00 USD').gross_minor is None          # das USD steht am Brutto: fremd


@pytest.mark.parametrize('euro', ['IBAN EUR-Konto', 'Alle Preise in EUR', 'Zahlung in €', 'EUR'])
def test_a_foreign_code_at_any_amount_voids_the_amounts_when_euro_stands_only_outside_of_numbers(euro):
    reading = read(euro, 'Gesamtbetrag: 119,00', 'Fracht 5,00 USD')
    assert (reading.gross_minor, reading.currency, reading.notes) == (None, None, ['foreign_currency'])
    assert read('Gesamtbetrag: 119,00', euro, 'Fracht: $ 5,00').gross_minor is None


def test_a_euro_sign_at_any_amount_or_no_foreign_code_at_an_amount_keeps_the_old_rule():
    # kein Zeichen am Brutto, aber Euro an einem anderen Betrag: Euro (ein fremdes Kürzel an einer anderen Zahl ändert nichts)
    reading = read('Gesamtbetrag: 119,00', 'Netto 100,00 EUR', 'Fracht 5,00 USD')
    assert (reading.gross_minor, reading.net_minor, reading.currency, reading.notes) == (1190000, 1000000, 'EUR', [])
    # Euro nur im Text, nirgends ein fremdes Kürzel an einer Zahl: Euro, das Zitat ist die Zeile, die Euro nennt (wie bisher)
    elsewhere = read('Preise in EUR', 'Gesamtbetrag: 119,00')
    assert elsewhere.currency == 'EUR' and elsewhere.raw['fields']['currency']['quote'] == 'Preise in EUR'
    assert read('Alle Preise in USD', 'Gesamtbetrag: 119,00').currency is None          # ein Kürzel ohne Zahl: weder fremd noch Euro
    assert read('Gesamtbetrag: 119,00').currency is None


def test_without_a_gross_the_currency_is_decided_at_the_net_and_the_tax():
    euro = read('Summe netto: 100,00 EUR')
    assert (euro.net_minor, euro.currency) == (1000000, 'EUR') and euro.raw['fields']['currency']['quote'] == 'Summe netto: 100,00 EUR'
    for lines in (('Summe netto: 100,00 USD',), ('zzgl. 19% MwSt.: 19,00 $',), ('Summe netto: 100,00 USD', 'zzgl. 19% MwSt.: 19,00 USD'),
                  ('Summe netto: 100,00 EUR', 'zzgl. 19% MwSt.: 19,00 $'), ('Summe netto: 100,00 $', 'zzgl. 19% MwSt.: 19,00 EUR')):
        reading = read(*lines)
        assert (reading.net_minor, reading.tax_minor, reading.currency, reading.notes) == (None, None, None, ['foreign_currency']), lines
    tax = read('zzgl. 19% MwSt.: 19,00 EUR')
    assert (tax.tax_minor, tax.currency, tax.raw['fields']['currency']['quote']) == (190000, 'EUR', 'zzgl. 19% MwSt.: 19,00 EUR')


def test_with_a_gross_only_the_gross_decides_the_currency():
    reading = read('Gesamtbetrag: 119,00 EUR', 'Summe netto: 100,00 USD')                  # Netto und Steuer zählen nur ohne Brutto
    assert (reading.gross_minor, reading.net_minor, reading.currency, reading.notes) == (1190000, 1000000, 'EUR', [])


def test_the_euro_sign_of_a_tax_line_with_several_amounts_is_the_sign_at_its_tax_amount():
    reading = read('USt 19% 100,00 19,00 EUR 119,00')
    assert (reading.tax_minor, reading.currency) == (190000, 'EUR')
    foreign = read('USt 19% 100,00 19,00 USD 119,00')
    assert (foreign.tax_minor, foreign.currency, foreign.notes) == (None, None, ['foreign_currency'])


# ── Zitat, Seite, Status, Text ───────────────────────────────────────────────

def test_every_field_names_its_line_and_the_page_of_the_file():
    pages = pdf_text.PageTexts(['Rechnungsnummer: R-5\nRechnungsdatum: 04.03.2026', 'Zwischentext',
                                'Summe netto: 100,00 EUR\nzzgl. 19% MwSt.: 19,00 EUR\nGesamtbetrag: 119,00 EUR'], [1, 2, 12])
    reading = text_rules.read_text(pages, OWN)
    assert {name: (entry['quote'], entry['page']) for name, entry in reading.raw['fields'].items() if 'quote' in entry} == {
        'invoice_number': ('Rechnungsnummer: R-5', 1), 'doc_date': ('Rechnungsdatum: 04.03.2026', 1),
        'net_minor': ('Summe netto: 100,00 EUR', 12), 'tax_minor': ('zzgl. 19% MwSt.: 19,00 EUR', 12),
        'tax_rate': ('zzgl. 19% MwSt.: 19,00 EUR', 12), 'gross_minor': ('Gesamtbetrag: 119,00 EUR', 12),
        'currency': ('Gesamtbetrag: 119,00 EUR', 12)}
    assert all(entry['source'] == 'rule' for name, entry in reading.raw['fields'].items() if name != 'kind')
    plain = text_rules.read_text(['A', 'Gesamtbetrag: 1,00 EUR'], OWN)                              # eine einfache Liste: 1, 2, …
    assert plain.raw['fields']['gross_minor']['page'] == 2


def test_a_quote_is_cleaned_and_cut_to_the_limit():
    reading = read('Gesamtbetrag:\N{ZERO WIDTH SPACE} 119,00 EUR\x00' + ' x' * 150)
    quote = reading.raw['fields']['gross_minor']['quote']
    assert quote.startswith('Gesamtbetrag: 119,00 EUR x') and len(quote) <= receipt_reading.RAW_STRING_LIMIT
    assert '\N{ZERO WIDTH SPACE}' not in quote and '\x00' not in quote
    assert Reading.from_dict(reading.to_dict()) == reading                                         # raw ist schon in gespeicherter Form


def test_a_reading_with_text_rules_survives_the_dict_and_the_row():
    reading = read('Muster GmbH', 'Rechnungs-Nr.: R-5', 'Rechnungsdatum: 04.03.2026', 'Summe netto: 100,00 EUR',
                   'zzgl. 19% MwSt.: 19,00 EUR', 'Gesamtbetrag: 119,00 EUR', 'USt-IdNr.: ' + SELLER_VAT_ID)
    assert Reading.from_dict(json.loads(json.dumps(reading.to_dict()))) == reading
    row = reading.to_row()
    assert (row['Status'], row['Source'], row['GrossMinor'], row['SenderName'], row['Kind']) == ('ok', 'text', 1190000, 'Muster GmbH', 'invoice')
    assert json.loads(row['Raw'])['checks'] == {'sum': 'ok', 'rate': 'ok'} and json.loads(row['Raw'])['notes'] == []


def test_nothing_read_is_empty_and_keeps_the_text_and_the_notes():
    reading = read('Irgendein Text', 'ohne Felder')
    assert (reading.status, reading.source, reading.raw, reading.kind) == ('empty', 'text', {}, None)
    assert reading.text == 'Irgendein Text\nohne Felder'
    assert Reading.from_dict(reading.to_dict()) == reading
    for pages in ([], [''], ['  \n\n \N{NO-BREAK SPACE} '], ['\n']):
        blank = text_rules.read_text(pages, OWN)
        assert (blank.status, blank.source, blank.notes) == ('empty', 'text', []), pages


def test_the_text_is_the_pages_joined_and_cut_to_the_stored_size(monkeypatch):
    reading = text_rules.read_text(['Seite eins', 'Seite zwei'], OWN)
    assert reading.text == 'Seite eins\nSeite zwei'
    monkeypatch.setattr(receipt_reading, 'MAX_TEXT_CHARS', 12)
    assert text_rules.read_text(['Seite eins', 'Seite zwei'], OWN).text == 'Seite eins\nS'


def test_the_own_data_may_be_anything_without_breaking_the_rules():
    for own in (None, (), ([], None), (['A'], 5), ([5, None, 'Muster GmbH'], [7, None, OTHER_VAT_ID]), 'ab'):
        assert text_rules.read_text(['Gesamtbetrag: 1,00 EUR'], own).gross_minor == 10000, own


# ── Grenzen der Regeln ───────────────────────────────────────────────────────

def test_a_line_is_cut_at_the_limit_and_the_whitespace_is_collapsed():
    far = 'Gesamtbetrag: ' + 'x' * text_rules.MAX_LINE_CHARS + ' 119,00'
    assert read(far).gross_minor is None                                                           # der Betrag liegt hinter der Grenze
    spaced = read('Gesamtbetrag:\t\t  \N{NO-BREAK SPACE}  119,00\N{NARROW NO-BREAK SPACE}EUR')
    assert spaced.gross_minor == 1190000 and spaced.raw['fields']['gross_minor']['quote'] == 'Gesamtbetrag: 119,00 EUR'


def test_only_the_first_lines_are_read(monkeypatch):
    monkeypatch.setattr(text_rules, 'MAX_LINES', 3)
    assert read('a', 'b', 'Gesamtbetrag: 1,00 EUR', 'Rechnungsdatum: 04.03.2026').doc_date is None
    assert read('a', 'b', 'Gesamtbetrag: 1,00 EUR', 'Rechnungsdatum: 04.03.2026').gross_minor == 10000
    assert len(text_rules._lines(['a\n\n\nb\n  \nc\nd'])) == 3                                      # leere Zeilen zählen nicht


FILLER = '0123456789.,   '
WORDS = 'Gesamtbetrag % MwSt % netto % brutto % Datum % USt-IdNr % Rechnungsnummer % '


def _text_of(size, pattern, width):
    stream = (pattern * (size // len(pattern) + 1))[:size]
    return '\n'.join(stream[i:i + width] for i in range(0, size, width))


@pytest.mark.parametrize('pattern, width', [(FILLER, 300), (FILLER, 40), (WORDS, 300), (WORDS, 60), ('1.234' * 7, 300),
                                            ('x ', 100_000_000)])
def test_the_rules_take_a_second_at_most_for_200000_characters(pattern, width):
    """Der Text kommt aus einer hochgeladenen Datei: Alle Muster laufen linear – kein Aufbau, der die Zeit hochtreibt."""
    text = _text_of(200_000, pattern, width)
    started = time.perf_counter()
    reading = text_rules.read_text([text], OWN)
    assert time.perf_counter() - started < 1.0 and reading.status in ('ok', 'empty')


@pytest.mark.parametrize('pattern', ['Gesamtbetrag 1,00 ', 'MwSt 19% 12,34 ', 'Rechnungsnummer AB12 ', 'USt-IdNr. DE 123 456 78 ',
                                     'Datum 01.02.2026 ', 'Muster GmbH · Foo AG, Bar KG | ',
                                     'Gesamtbetrag - € 1,00 € - ', '(€ 1,00) S 1,00 CR ', 'EUR 1,00 USD 2,00 $ '])           # Vorzeichen, Währung am Betrag
def test_lines_full_of_matches_are_slow_but_never_more_than_a_few_seconds(pattern):
    """Jede Zeile besteht nur aus Treffern: Das Lesen jedes Treffers kostet, aber nichts wächst über das Lesen der Zeilen hinaus."""
    text = _text_of(200_000, pattern, 300)
    started = time.perf_counter()
    text_rules.read_text([text], OWN)
    assert time.perf_counter() - started < 5.0


def test_thousands_of_tax_lines_with_as_many_amounts_as_the_rate_proof_still_compares_stay_fast():
    """Die Satzprobe vergleicht die Beträge einer Zeile paarweise – darum gilt eine Grenze je Zeile (``MAX_LINE_AMOUNTS``)."""
    amounts = ' '.join(f'{number},00' for number in range(1, text_rules.MAX_LINE_AMOUNTS + 1))
    text = '\n'.join([f'MwSt 19% {amounts}'] * 4000)
    started = time.perf_counter()
    reading = text_rules.read_text([text], OWN)
    assert time.perf_counter() - started < 5.0 and reading.status in ('ok', 'empty')


def test_the_time_grows_with_the_text_not_with_its_square():
    def seconds(size):
        text = _text_of(size, FILLER + WORDS, 300)
        started = time.perf_counter()
        text_rules.read_text([text], OWN)
        return time.perf_counter() - started
    seconds(20_000)                                                                                 # die Muster sind übersetzt
    small, large = min(seconds(25_000) for _ in range(3)), min(seconds(200_000) for _ in range(3))
    assert large < 25 * small + 0.2                                                                 # 8-fache Größe: nicht 64-fach


# ── PDF: Seitenwahl und Grenzen ──────────────────────────────────────────────

def numbered_pdf(count):
    return make_pdf([[f'Seite {number} von {count} Beispieltext zum Lesen'] for number in range(1, count + 1)])


@pytest.mark.parametrize('count, expected', [
    (0, []), (1, [0]), (5, [0, 1, 2, 3, 4]), (7, [0, 1, 2, 3, 4, 5, 6]), (8, [0, 1, 2, 3, 4, 5, 6, 7]),
    (9, [0, 1, 2, 3, 4, 6, 7, 8]), (12, [0, 1, 2, 3, 4, 9, 10, 11]), (100, [0, 1, 2, 3, 4, 97, 98, 99])])
def test_the_first_five_and_the_last_three_pages_are_wanted_without_duplicates(count, expected):
    assert pdf_text.wanted_pages(count) == expected


def test_a_pdf_of_twelve_pages_is_read_from_page_1_to_5_and_10_to_12():
    pages, hint = pdf_text.extract_pdf_text(numbered_pdf(12), 200_000, None)
    assert hint is None and pages.numbers == [1, 2, 3, 4, 5, 10, 11, 12]
    assert pages == [f'Seite {n} von 12 Beispieltext zum Lesen' for n in (1, 2, 3, 4, 5, 10, 11, 12)]
    assert isinstance(pages, list)
    for count in (1, 8):
        short, hint = pdf_text.extract_pdf_text(numbered_pdf(count), 200_000, None)
        assert hint is None and short.numbers == list(range(1, count + 1))


def test_a_total_on_a_late_page_names_the_page_of_the_file():
    pages = [['Seite %d' % n, 'Beispieltext zum Lesen'] for n in range(1, 11)]
    pages[9] += ['Summe netto: 100,00 EUR', 'zzgl. 19% MwSt.: 19,00 EUR', 'Gesamtbetrag: 119,00 EUR']
    reading = read_document('lang.pdf', make_pdf(pages), OWN)
    assert reading.gross_minor == 1190000 and reading.raw['fields']['gross_minor']['page'] == 10
    assert reading.raw['fields']['net_minor']['page'] == 10


def test_the_character_limit_ends_the_reading_with_a_note(monkeypatch):
    data = numbered_pdf(12)
    pages, hint = pdf_text.extract_pdf_text(data, 60, None)
    assert hint == 'text_truncated' and sum(len(page) for page in pages) == 60 and pages.numbers == [1, 2]
    two = numbered_pdf(2)
    whole = sum(len(page) for page in pdf_text.extract_pdf_text(two, 200_000, None)[0])
    exact, hint = pdf_text.extract_pdf_text(two, whole, None)                                       # genau die Grenze: nichts fehlt
    assert hint is None and len(exact) == 2 and sum(len(page) for page in exact) == whole
    over, hint = pdf_text.extract_pdf_text(two, whole - 1, None)
    assert hint == 'text_truncated' and sum(len(page) for page in over) == whole - 1                # die letzte Seite wird gekürzt
    one_page, hint = pdf_text.extract_pdf_text(numbered_pdf(1), 25, None)
    assert hint == 'text_truncated' and sum(len(page) for page in one_page) == 25                   # eine Seite allein kann sie überschreiten
    monkeypatch.setattr(receipt_reading, 'MAX_EXTRACT_CHARS', 60)
    reading = read_document('lang.pdf', data, OWN)
    assert (reading.status, reading.notes) == ('empty', ['text_truncated']) and len(reading.text) <= 61


def test_the_deadline_ends_the_reading_after_the_page_in_hand():
    data = numbered_pdf(12)
    pages, hint = pdf_text.extract_pdf_text(data, 200_000, time.monotonic() - 1)
    assert hint == 'text_truncated' and pages.numbers == [1]
    pages, hint = pdf_text.extract_pdf_text(data, 200_000, time.monotonic() + 3600)
    assert hint is None and len(pages) == 8
    reading = read_document('lang.pdf', data, OWN, deadline=time.monotonic() - 1)
    assert reading.notes == ['text_truncated'] and reading.text == 'Seite 1 von 12 Beispieltext zum Lesen'
    single, hint = pdf_text.extract_pdf_text(numbered_pdf(1), 200_000, time.monotonic() - 1)       # die letzte Seite ist gelesen
    assert hint is None and len(single) == 1


def test_a_pdf_with_less_than_twenty_visible_characters_has_no_text():
    for lines in ([], ['abcdefghij klmnopqrs'], ['a b c  d e f', 'g h i j k l m n']):
        pages, hint = pdf_text.extract_pdf_text(make_pdf([lines]), 200_000, None)
        assert (pages, hint) == ([], 'no_text'), lines
        assert read_document('scan.pdf', make_pdf([lines]), OWN).status == 'no_text'
    pages, hint = pdf_text.extract_pdf_text(make_pdf([['abcdefghij klmnopqrst']]), 200_000, None)
    assert (pages, hint) == (['abcdefghij klmnopqrst'], None)
    split, hint = pdf_text.extract_pdf_text(make_pdf([['abcdefghij'], ['klmnopqrst']]), 200_000, None)
    assert hint is None and len(split) == 2                                                          # insgesamt, nicht je Seite


ENCRYPT = (b'/Encrypt << /Filter /Standard /V 1 /R 2 /O (' + b'0' * 32 + b') /U (' + b'1' * 32 + b') /P -4 >> '
           b'/ID [(aaaaaaaaaaaaaaaa) (aaaaaaaaaaaaaaaa)]')


def test_a_pdf_that_needs_a_password_is_unsupported_and_a_broken_one_failed():
    assert pdf_text.extract_pdf_text(hand_pdf({}, trailer=ENCRYPT), 200_000, None) == ([], 'encrypted')
    for data in (b'%PDF-1.7\n' + b'\xff' * 500, b'', None, 'text', b'%PDF-'):
        assert pdf_text.extract_pdf_text(data, 200_000, None) == ([], 'pdf_invalid'), data
    reading = read_document('a.pdf', hand_pdf({}, trailer=ENCRYPT), OWN)
    assert (reading.status, reading.source, reading.notes, reading.text) == ('unsupported', None, ['encrypted'], None)
    reading = read_document('a.pdf', b'%PDF-1.7\n' + b'\xff' * 500, OWN)
    assert (reading.status, reading.notes) == ('failed', ['pdf_invalid'])


def test_a_pdf_without_pages_has_no_text_and_one_with_garbage_content_never_raises():
    assert pdf_text.extract_pdf_text(hand_pdf({}), 200_000, None) == ([], 'no_text')
    good = numbered_pdf(3)
    for cut in (len(good) // 2, len(good) - 40, len(good) - 400):
        pages, hint = pdf_text.extract_pdf_text(good[:cut], 200_000, None)
        assert hint in (None, 'no_text', 'pdf_invalid', 'text_truncated')


# ── PDF: der ganze Weg ───────────────────────────────────────────────────────

INVOICE_PAGE = ['Muster Handel GmbH · Musterweg 3 · 12345 Musterstadt', 'Rechnung Nr. R-2026/17', 'Rechnungsdatum: 04.03.2026',
                'Leistungsdatum: 01.03.2026', 'Fällig am 18.03.2026', 'Summe netto: 1.000,00 €', 'zzgl. 19 % MwSt.: 190,00 €',
                'Gesamtbetrag: 1.190,00 €', 'USt-IdNr.: ' + SELLER_VAT_ID, 'IBAN: DE89 3704 0044 0532 0130 00']


def test_an_invoice_pdf_with_a_text_layer_is_read_by_the_rules():
    reading = read_document('rechnung.pdf', make_pdf([INVOICE_PAGE]), (['Beispiel Kunde AG'], [OTHER_VAT_ID]))
    assert (reading.status, reading.source, reading.kind, reading.notes) == ('ok', 'text', 'invoice', [])
    assert (reading.doc_date, reading.invoice_number, reading.sender_name, reading.sender_vat_id, reading.currency) == (
        '2026-03-04', 'R-2026/17', 'Muster Handel GmbH', SELLER_VAT_ID, 'EUR')
    assert (reading.net_minor, reading.tax_minor, reading.tax_rate, reading.gross_minor) == (10000000, 1900000, 0.19, 11900000)
    assert reading.raw['checks'] == {'sum': 'ok', 'rate': 'ok'}
    assert reading.raw['fields']['doc_date']['quote'] == 'Rechnungsdatum: 04.03.2026'
    assert reading.text == '\n'.join(INVOICE_PAGE)
    assert Reading.from_dict(reading.to_dict()) == reading


@pytest.fixture
def app_invoice_pdf(inv_db):
    """Die Rechnungs-PDF der App – ohne ihr eingebettetes XML: Der Anhang trägt einen anderen Namen, der E-Rechnungs-Weg
    findet nichts, und gelesen wird der Text."""
    iid = _make_invoice(inv_db, number='26R010')
    generate_invoice_documents(inv_db, iid)
    data, name, error = stored_invoice_document(inv_db, iid, 'pdf')
    assert error is None
    renamed = data.replace(b'(factur-x.xml)', b'(abcdefgh.xml)')
    assert renamed != data
    return name, data, renamed


def test_the_text_of_the_apps_own_invoice_pdf_gives_every_field_and_the_sum_proof_holds(app_invoice_pdf):
    name, original, renamed = app_invoice_pdf
    reading = read_document(name, renamed, (['Malermeister Pinsel'], []))
    assert (reading.status, reading.source, reading.kind, reading.notes) == ('ok', 'text', 'invoice', [])
    assert (reading.gross_minor, reading.net_minor, reading.tax_minor, reading.tax_rate) == (1190000, 1000000, 190000, 0.19)
    assert (reading.doc_date, reading.invoice_number, reading.currency) == ('2026-03-04', '26R010', 'EUR')
    assert (reading.sender_name, reading.sender_vat_id) == ('Muster GmbH', SELLER_VAT_ID)
    assert reading.raw['checks'] == {'sum': 'ok', 'rate': 'ok'}
    assert {field: entry['quote'] for field, entry in reading.raw['fields'].items() if 'quote' in entry} == {
        'doc_date': 'Rechnungsdatum: 2026-03-04', 'invoice_number': 'Rechnungs-Nr.: 26R010', 'gross_minor': 'Gesamtbetrag: 119,00 EUR',
        'net_minor': 'Summe netto: 100,00 EUR', 'tax_minor': 'zzgl. 19% MwSt.: 19,00 EUR', 'tax_rate': 'zzgl. 19% MwSt.: 19,00 EUR',
        'currency': 'Gesamtbetrag: 119,00 EUR', 'sender_name': 'Muster GmbH · Musterstraße 1 · 12345 Musterstadt',
        'sender_vat_id': '12345 Musterstadt USt-IdNr.: DE123456789 IBAN: DE89370400440532013000'}
    # dieselbe PDF mit ihrem XML: Der E-Rechnungs-Weg liest dieselben Werte
    via_xml = read_document(name, original, (['Malermeister Pinsel'], []))
    assert via_xml.source == 'einvoice_cii'
    for field in ('gross_minor', 'net_minor', 'tax_minor', 'tax_rate', 'doc_date', 'invoice_number', 'currency', 'sender_name',
                  'sender_vat_id', 'kind'):
        assert getattr(via_xml, field) == getattr(reading, field), field


def test_the_app_pdf_is_read_as_the_supplier_of_the_own_company_only_when_the_names_differ(app_invoice_pdf):
    name, _original, renamed = app_invoice_pdf
    as_buyer = read_document(name, renamed, (['Malermeister Pinsel'], []))
    as_seller = read_document(name, renamed, (['Muster GmbH'], [SELLER_VAT_ID]))                    # die eigene Firma ist der Aussteller
    assert as_buyer.sender_name == 'Muster GmbH' and as_buyer.sender_vat_id == SELLER_VAT_ID
    assert (as_seller.sender_name, as_seller.sender_vat_id) == (None, None)
    assert as_seller.gross_minor == as_buyer.gross_minor == 1190000


def test_a_statement_is_unsupported_but_its_text_is_kept():
    data = make_pdf(one_page(OPENING_LINE, *A_BOOKING, *B_BOOKING, CLOSING_LINE))
    reading = read_document('auszug.pdf', data, OWN)
    assert (reading.status, reading.source, reading.notes, reading.gross_minor) == ('unsupported', None, ['statement'], None)
    assert 'alter Kontostand' in reading.text and 'Musterhaendler GmbH' in reading.text
    assert reading.raw == {} and Reading.from_dict(reading.to_dict()) == reading


def test_the_statement_note_comes_after_a_truncation_note():
    pages = one_page(OPENING_LINE, *A_BOOKING, CLOSING_LINE) + [[f'Seite {n} von 12 Beispieltext zum Lesen'] for n in range(2, 13)]
    reading = read_document('auszug.pdf', make_pdf(pages), OWN, deadline=time.monotonic() - 1)         # nur die erste Seite
    assert (reading.status, reading.notes) == ('unsupported', ['text_truncated', 'statement'])
    assert 'neuer Kontostand' in reading.text and 'Seite 2 von 12' not in reading.text


def test_a_statement_is_a_text_the_importers_know_with_the_balances_of_a_statement(monkeypatch):
    seen = []
    balanced = 'alter Kontostand vom 30.11.2024 1.000,00 H'

    def finder(answer):
        def find(name, text):
            seen.append((name, text))
            return answer
        return find
    cases = [(object, balanced, True), (None, balanced, False), (object, 'Rechnung ohne Salden', False),
             (object, 'neuer Kontostand ohne Betrag', False), (object, 'Text\n' + balanced.replace('alter', 'neuer'), True)]
    for answer, text, expected in cases:
        monkeypatch.setattr(receipt_reading, 'find_importer', finder(answer))
        assert receipt_reading._is_statement('auszug.pdf', text) is expected, (answer, text)
    assert seen[0] == ('auszug.pdf', balanced) and receipt_reading._is_statement(None, balanced) is True and seen[-1][0] == ''


def test_an_invoice_that_names_a_bank_of_the_importers_in_its_footer_is_no_statement():
    """Der Fuß einer Rechnung nennt die Bank des Lieferanten – ``find_importer`` allein hielte sie für einen Kontoauszug."""
    lines = INVOICE_PAGE + ['Bankverbindung: Volksbank Musterstadt eG', 'Konto bei der DKB oder der Deutschen Kreditbank']
    assert receipt_reading.find_importer('rechnung.pdf', '\n'.join(lines)) is not None              # die Voraussetzung dieses Tests
    for name in ('rechnung.pdf', 'vbr_rechnung.pdf'):
        reading = read_document(name, make_pdf([lines]), OWN)
        assert (reading.status, reading.source, reading.gross_minor, reading.notes) == ('ok', 'text', 11900000, []), name


def text_pdf_with(xml, lines):
    """Eine PDF mit Text (wie ``make_pdf``) und einem eingebetteten ``factur-x.xml`` – beliebige Bytes."""
    ops = ['BT', '/F1 10 Tf']
    for number, line in enumerate(lines):
        ops += [f'1 0 0 1 40 {800 - 14 * number} Tm', f'({escape_pdf_string(line)}) Tj']
    ops.append('ET')
    return build_multi_page_pdf([ops], facturx_xml=xml)


EMPTY_UBL = b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>'


@pytest.mark.parametrize('xml, notes', [(DTD_XML, ['xml_dtd']), (b'<a>', ['xml_invalid']), (b'<a/>', ['not_einvoice']),
                                        (EMPTY_UBL, ['einvoice_empty']),
                                        (ubl(gross='1e5', net=None, tax=None, tax_totals='', date=None, seller=None, vat_id=None,
                                             number=None, currency=None, rate=None), ['bad_gross_minor', 'einvoice_empty'])])
def test_a_pdf_with_text_and_an_attachment_that_gives_no_reading_is_read_by_its_text_with_the_notes_of_the_attachment(xml, notes):
    reading = read_document('a.pdf', text_pdf_with(xml, INVOICE_PAGE), OWN)
    assert (reading.status, reading.source, reading.gross_minor, reading.invoice_number) == ('ok', 'text', 11900000, 'R-2026/17')
    assert reading.notes == notes


def test_an_attachment_with_a_reading_wins_over_the_text_of_the_pdf():
    reading = read_document('a.pdf', text_pdf_with(cii(), INVOICE_PAGE), OWN)
    assert (reading.source, reading.gross_minor, reading.text) == ('einvoice_cii', 1190000, None)


# ── Der Kindprozess: Kopfzeile, Grenzen, Ausgabe ─────────────────────────────

def head_line(**overrides):
    request = {'name': 'a.pdf', 'own': [['Muster GmbH'], [OTHER_VAT_ID]], 'timeout': 15, **overrides}
    return json.dumps(request).encode('utf-8') + b'\n'


def test_the_header_of_a_request_is_checked_strictly():
    assert worker.parse_header(head_line()) == ('a.pdf', (['Muster GmbH'], [OTHER_VAT_ID]), 15.0)
    assert worker.parse_header(head_line(timeout=0.5, name='')) == ('', (['Muster GmbH'], [OTHER_VAT_ID]), 0.5)
    bad = [b'', b'\n', b'kein JSON\n', b'[]\n', b'"x"\n', b'null\n', head_line()[:-1], head_line() + b'x',
           head_line(name=5), head_line(name=None), head_line(own=None), head_line(own=[[]]), head_line(own=[[], [], []]),
           head_line(own=[['a'], 'b']), head_line(own=[[5], []]), head_line(own={'a': 1}), head_line(timeout=0), head_line(timeout=-1),
           head_line(timeout=True), head_line(timeout='15'), head_line(timeout=None), head_line(timeout=worker.MAX_TIMEOUT + 1),
           head_line(timeout=float('inf')), b'\xff\xfe\n', b'{"name": "a.pdf"}\n', b'x' * (worker.MAX_HEADER_BYTES + 1) + b'\n']
    for line in bad:
        assert worker.parse_header(line) is None, line[:60]


class FakeResource:
    RLIMIT_AS, RLIMIT_CPU, RLIM_INFINITY = 9, 0, -1

    def __init__(self, hard=-1, error=None):
        self.hard, self.error, self.calls = hard, error, []

    def getrlimit(self, which):
        return (self.hard, self.hard)

    def setrlimit(self, which, limits):
        if self.error:
            raise self.error
        self.calls.append((which, limits))


def test_the_limits_are_set_under_posix_only_and_never_above_the_hard_limit(monkeypatch):
    fake = FakeResource()
    monkeypatch.setattr(worker, 'resource', fake)
    worker.limit_memory()
    worker.limit_cpu(15)
    worker.limit_cpu(0.2)
    mib = 768 * 1024 * 1024
    assert fake.calls == [(9, (mib, mib)), (0, (20, 20)), (0, (6, 6))]                              # Zeitlimit + 5 s, aufgerundet
    lower = FakeResource(hard=100 * 1024 * 1024)
    monkeypatch.setattr(worker, 'resource', lower)
    worker.limit_memory()
    assert lower.calls == [(9, (100 * 1024 * 1024,) * 2)]
    for error in (ValueError('no'), OSError('no')):
        monkeypatch.setattr(worker, 'resource', FakeResource(error=error))
        worker.limit_memory()                                                                      # ein System ohne Erlaubnis: kein Abbruch
        worker.limit_cpu(15)
    monkeypatch.setattr(worker, 'resource', None)                                                  # Windows: gibt es nicht
    worker.limit_memory()
    worker.limit_cpu(15)


@pytest.fixture
def run_worker(monkeypatch):
    """``worker.main()`` im eigenen Prozess der Tests: stdin/stdout durch Puffer ersetzt, die Grenzen des Systems nie gesetzt
    (``resource`` fehlt), Logging und Warnungen danach wie vorher."""
    monkeypatch.setattr(worker, 'resource', None)
    monkeypatch.setattr(logging.root.manager, 'disable', logging.root.manager.disable)

    def run(stdin_bytes):
        out = io.BytesIO()
        monkeypatch.setattr(sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(stdin_bytes)))
        monkeypatch.setattr(sys, 'stdout', types.SimpleNamespace(buffer=out))
        with warnings.catch_warnings():
            code = worker.main()
        return code, out.getvalue()
    return run


def test_the_worker_answers_with_one_json_object_that_is_the_reading(run_worker):
    data = make_pdf([INVOICE_PAGE])
    code, out = run_worker(head_line(own=[[], []]) + data)
    assert code == 0
    assert Reading.from_dict(json.loads(out.decode('utf-8'))) == read_document('a.pdf', data, OWN)
    code, out = run_worker(head_line(name='kein.pdf') + b'text')
    assert code == 0 and Reading.from_dict(json.loads(out.decode('utf-8'))) == Reading(status='unsupported')


def test_the_worker_writes_nothing_else_to_stdout_not_even_a_print_of_a_library(run_worker, monkeypatch):
    def noisy(name, data, own, deadline=None):
        print('Importer VBR detect failed: Geheimnis')
        sys.stdout.write('mehr')
        return Reading(status='empty', notes=['x'])
    monkeypatch.setattr(worker, 'read_document', noisy)
    code, out = run_worker(head_line() + b'data')
    assert code == 0 and json.loads(out.decode('utf-8')) == Reading(status='empty', notes=['x']).to_dict()


def test_the_worker_gives_the_reading_a_soft_deadline_of_four_fifths_of_the_limit(run_worker, monkeypatch):
    seen = []
    monkeypatch.setattr(worker, 'read_document', lambda name, data, own, deadline=None: seen.append((name, data, own, deadline))
                        or Reading(status='empty'))
    before = time.monotonic()
    run_worker(head_line(timeout=10) + b'bytes')
    (name, data, own, deadline), = seen
    assert (name, data, own) == ('a.pdf', b'bytes', (['Muster GmbH'], [OTHER_VAT_ID]))
    assert before + 8 <= deadline <= time.monotonic() + 8


def test_the_worker_refuses_a_bad_header_with_code_2_and_no_output(run_worker):
    for stdin in (b'', b'kein JSON\n', head_line(timeout=0) + b'x', head_line()[:-1]):
        assert run_worker(stdin) == (2, b'')


def test_the_real_worker_process_gives_code_2_for_a_bad_header_and_nothing_on_stdout():
    done = subprocess.run([sys.executable, '-m', 'receipt_reading.worker'], input=b'kein JSON\n', capture_output=True, cwd=ROOT,
                          timeout=60)
    assert (done.returncode, done.stdout) == (2, b'')


# ── Der Kindprozess: der Rundlauf ────────────────────────────────────────────

def test_a_text_pdf_read_in_the_child_is_the_reading_of_the_direct_call():
    data = make_pdf([INVOICE_PAGE])
    own = (['Beispiel Kunde AG'], [OTHER_VAT_ID])
    direct = read_document('rechnung.pdf', data, own)
    isolated_reading = isolated.read_isolated('rechnung.pdf', data, own)
    assert isolated_reading.status == 'ok' and isolated_reading == direct
    assert isolated_reading.raw['fields']['gross_minor']['quote'] == 'Gesamtbetrag: 1.190,00 €'     # Umlaute und € überstehen den Weg


def test_a_ubl_xml_read_in_the_child_is_the_reading_of_the_direct_call():
    data = ubl()
    direct = read_document('beleg.xml', data, OWN)
    assert direct.status == 'ok' and direct.source == 'einvoice_ubl'
    assert isolated.read_isolated('beleg.xml', data, OWN) == direct
    assert isolated.read_isolated('beleg.xml', cii(), OWN) == read_document('beleg.xml', cii(), OWN)


def test_the_child_gives_the_same_notes_and_statuses_for_what_cannot_be_read():
    for name, data in (('a.pdf', b'%PDF-1.7\n' + b'\xff' * 500), ('a.png', b'\x89PNG\r\n\x1a\n0000'), ('a.txt', b'text'),
                       ('a.xml', b'<a>'), ('a.pdf', make_pdf([[]])), ('a.pdf', hand_pdf({}, trailer=ENCRYPT)),
                       ('auszug.pdf', make_pdf(one_page(OPENING_LINE, *A_BOOKING, CLOSING_LINE)))):
        assert isolated.read_isolated(name, data, OWN) == read_document(name, data, OWN), name


def python(code):
    return [sys.executable, '-c', code]


def test_the_child_that_runs_too_long_is_killed_and_the_reading_is_too_large_with_the_note_time():
    started = time.perf_counter()
    reading = isolated.read_isolated('a.pdf', b'%PDF-', OWN, timeout=0.5, command=python('import time; time.sleep(30)'))
    assert (reading.status, reading.notes) == ('too_large', ['time'])
    assert time.perf_counter() - started < 10                                                        # nicht die 30 Sekunden des Kindes


def test_the_default_timeout_is_read_when_the_function_is_called(monkeypatch):
    assert receipt_reading.READ_TIMEOUT == 15
    monkeypatch.setattr(receipt_reading, 'READ_TIMEOUT', 0.4)
    reading = isolated.read_isolated('a.pdf', b'%PDF-', OWN, command=python('import time; time.sleep(30)'))
    assert (reading.status, reading.notes) == ('too_large', ['time'])


def test_a_child_that_answers_with_a_reading_is_believed_only_in_the_stored_form():
    code = 'import json, sys; sys.stdout.write(json.dumps({"status": "empty", "source": "text", "notes": ["x"]}))'
    assert isolated.read_isolated('a.pdf', b'%PDF-', OWN, command=python(code)) == Reading(status='empty', source='text', notes=['x'])


@pytest.mark.parametrize('code', [
    'print("x")', 'import sys; sys.stdout.write("")', 'import sys; sys.stdout.write("[]")', 'import sys; sys.stdout.write("null")',
    'import sys; sys.stdout.buffer.write(b"\\xff\\xfe")', 'import sys; sys.stdout.write("[" * 100000)',
    'import json, sys; sys.stdout.write(json.dumps({"status": "bogus"}))',
    'import json, sys; sys.stdout.write(json.dumps({"status": "ok", "gross_minor": 1.5}))',
    'import json, sys; sys.stdout.write(json.dumps({"status": "ok", "extra": 1}))',
    'import json, sys; sys.stdout.write(json.dumps({"status": "ok", "raw": {"a": "x" * 500}}))',
    'import json, sys; sys.stdout.write(json.dumps({"status": "ok", "sender_name": " x"}))',
    'import json, sys; sys.stdout.write(json.dumps({"status": "ok", "notes": "x"}))',
    'import sys; sys.stdout.write("x" * 2_000_000)', 'import sys; sys.stdout.write(\'{"status": "empty", "pad": "\' + "x" * 2_000_000 + \'"}\')'])
def test_an_output_that_is_no_reading_is_failed_with_reader_output(code):
    reading = isolated.read_isolated('a.pdf', b'%PDF-', OWN, command=python(code))
    assert (reading.status, reading.notes, reading.raw, reading.text) == ('failed', ['reader_output'], {}, None)


@pytest.mark.parametrize('code', ['import sys; sys.exit(3)', 'import sys; sys.exit(1)', 'raise RuntimeError("Geheimnis")',
                                  'import os; os._exit(7)', 'import json, sys; sys.stdout.write(json.dumps({"status": "empty"})); sys.exit(2)'])
def test_a_child_that_ends_with_an_error_code_is_failed_even_if_it_wrote_a_good_answer(code):
    reading = isolated.read_isolated('a.pdf', b'%PDF-', OWN, command=python(code))
    assert (reading.status, reading.notes) == ('failed', ['reader_crashed'])


def test_a_big_file_goes_to_a_child_that_stops_reading_or_never_answers_without_a_hang_or_an_error():
    big = b'%PDF-' + b'x' * 5_000_000
    quits = isolated.read_isolated('a.pdf', big, OWN, command=python('import sys; sys.exit(3)'))
    assert (quits.status, quits.notes) == ('failed', ['reader_crashed'])
    started = time.perf_counter()
    hangs = isolated.read_isolated('a.pdf', big, OWN, timeout=1, command=python('import time; time.sleep(30)'))
    assert (hangs.status, hangs.notes) == ('too_large', ['time']) and time.perf_counter() - started < 10


def test_a_child_that_is_not_there_is_failed(tmp_path):
    reading = isolated.read_isolated('a.pdf', b'%PDF-', OWN, command=[str(tmp_path / 'gibt-es-nicht.exe')])
    assert (reading.status, reading.notes) == ('failed', ['reader_crashed'])


def test_what_the_child_writes_to_stderr_is_never_in_the_reading_but_clipped_in_the_log(caplog):
    caplog.set_level(logging.WARNING, logger='receipt_reading.isolated')
    reading = isolated.read_isolated('a.pdf', b'%PDF-', OWN, command=python(
        'import sys; sys.stderr.write("Geheimnis " * 500); sys.exit(3)'))
    assert (reading.status, reading.notes) == ('failed', ['reader_crashed'])
    assert 'Geheimnis' not in json.dumps(reading.to_dict()) and 'Geheimnis' not in str(reading.to_row())
    (record,) = caplog.records
    message = record.getMessage()
    assert 'Code 3' in message and 'Geheimnis' in message and len(message) < 600


def test_the_child_gets_the_header_and_the_bytes_and_works_in_the_project_folder():
    code = ('import json, os, sys; head = sys.stdin.buffer.readline(); body = sys.stdin.buffer.read(); '
            'request = json.loads(head); '
            'sys.stdout.write(json.dumps({"status": "empty", "notes": [request["name"], str(len(body)), str(request["timeout"]), '
            'str(request["own"]), os.getcwd()]}))')
    reading = isolated.read_isolated('Rechnung ä.pdf', b'%PDF-1.7 mehr', ([' Muster GmbH'], [OTHER_VAT_ID]), timeout=30, command=python(code))
    assert reading.notes[:4] == ['Rechnung ä.pdf', '13', '30', f"[[' Muster GmbH'], ['{OTHER_VAT_ID}']]"]
    assert os.path.samefile(reading.notes[4], ROOT)


def test_a_file_over_the_limit_never_starts_a_process(monkeypatch):
    def started(*args, **kwargs):
        raise AssertionError('es darf kein Prozess starten')
    monkeypatch.setattr(receipt_reading, 'MAX_READ_BYTES', 100)
    monkeypatch.setattr(isolated.subprocess, 'run', started)
    assert isolated.read_isolated('a.pdf', b'%PDF-' + b'0' * 200, OWN) == Reading(status='too_large')
    assert isolated.read_isolated('a.pdf', b'x' * 101, OWN).status == 'too_large'
    monkeypatch.undo()
    monkeypatch.setattr(receipt_reading, 'MAX_READ_BYTES', 100)
    exactly = isolated.read_isolated('a.txt', b'x' * 100, OWN)                                       # genau die Grenze: gelesen
    assert exactly == Reading(status='unsupported')


def test_read_isolated_never_raises_whatever_it_is_given():
    for args in ((None, None, None), ('a.pdf', 'text', OWN), ('a.pdf', b'x', None), ('a.pdf', b'x', 5), (b'name', b'x', OWN),
                 ('a.pdf', object(), OWN), ('a.pdf', b'x', ([object()], []))):
        reading = isolated.read_isolated(*args)
        assert reading.status in ('failed', 'unsupported'), args
    assert isolated.read_isolated('a.pdf', b'x', OWN, timeout='lange').status == 'failed'
    reading = isolated.read_isolated('a.pdf', b'x', OWN, timeout=0.001, command=python('import time; time.sleep(30)'))
    assert reading.status in ('too_large', 'failed')


def test_the_child_is_started_without_dash_i_in_the_project_folder(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(Reading(status='empty').to_dict()).encode(), stderr=b'')
    monkeypatch.setattr(isolated.subprocess, 'run', fake_run)
    assert isolated.read_isolated('a.pdf', b'%PDF-', ([], [])) == Reading(status='empty')
    (command, kwargs), = calls
    assert command == [sys.executable, '-m', 'receipt_reading.worker'] and '-I' not in command
    assert os.path.samefile(kwargs['cwd'], ROOT) and kwargs['timeout'] == receipt_reading.READ_TIMEOUT
    assert kwargs['capture_output'] is True
    head, _, body = kwargs['input'].partition(b'\n')
    assert json.loads(head) == {'name': 'a.pdf', 'own': [[], []], 'timeout': receipt_reading.READ_TIMEOUT} and body == b'%PDF-'
