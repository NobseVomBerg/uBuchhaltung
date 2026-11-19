# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kontoauszug-Import: Umsatzliste der DKB als CSV (``importers/dkb_csv.py``, 2.5.0).

* Beide bekannten Formate – neu (``Buchungsdatum``, zwei Gegenseiten-Spalten, Datum ``TT.MM.JJ``, UTF-8 mit BOM) und alt
  (``Buchungstag``, eine Spalte, ``TT.MM.JJJJ``, Windows-1252, auch mit Komma statt Semikolon und mit verlorenem Umlaut) –
  werden an der Datei selbst erkannt, die Spalten nach Bedeutung gefunden.
* ``money.parse_grouped_amount``: der Punkt ist immer ein Tausendertrenner (``-15.000`` ist −15000, nicht −15).
* Befunde: ``skipped_status`` (vorgemerkt), ``unparsed``, ``no_balance`` (immer), ``no_iban``; Fußzeilen still übergangen.
* Fehler sind feste Sätze ohne Feldwerte der Datei; Grenzen für Größe und Zeilenzahl; nichts landet auf der Platte.
* Upload über ``handle_file_upload``: erkannt → Vorschau, nicht erkannt → Warnung, nie im ``Documents``-Ordner.

Alle Namen, IBANs und Beträge sind erfunden; die IBANs stammen aus der erlaubten Liste oder sind Platzhalter.
"""
import io
import json
import os
import uuid
from contextlib import closing
from datetime import datetime
from decimal import Decimal

import pytest

import userctx
from importers import check_statement
from importers import dkb_csv
from importers.base import BankStatement
from importers.dkb_csv import (MAX_CSV_BYTES, MAX_CSV_ROWS, NOT_RECOGNISED, TOO_BIG, UNREADABLE, DkbCsvError,
                               parse_csv)
from money import parse_amount, parse_grouped_amount, to_minor
from server import handlers, upload_handler

OWN_IBAN = 'DE89370400440532013000'
PEER_IBAN = 'DE02120300000000202051'

NEW_HEAD = ('"Buchungsdatum";"Wertstellung";"Status";"Zahlungspflichtige*r";"Zahlungsempfänger*in";"Verwendungszweck";'
            '"Umsatztyp";"IBAN";"Betrag (€)";"Gläubiger-ID";"Mandatsreferenz";"Kundenreferenz"')
OLD_HEAD = ('"Buchungstag";"Wertstellung";"Buchungstext";"Auftraggeber / Begünstigter";"Verwendungszweck";"Kontonummer";'
            '"BLZ";"Betrag (EUR)";"Gläubiger-ID";"Mandatsreferenz";"Kundenreferenz";')


def new_row(day, payer, payee, purpose, amount, status='Gebucht', iban=''):
    cells = [day, day, status, payer, payee, purpose, 'Ausgang' if amount.startswith('-') else 'Eingang', iban, amount,
             '', '', '']
    return ';'.join(f'"{cell}"' for cell in cells)


def new_format(*rows, iban=OWN_IBAN, head=NEW_HEAD):
    """Eine Umsatzliste im neuen Format; ``iban=None`` lässt die IBAN im Vorspann weg."""
    lines = ['"Girokonto";"%s"' % iban if iban else '"Girokonto"', '"Zeitraum:";"01.01.2024 - 31.12.2024"',
             '"Kontostand vom 01.06.2026:";"10.956,80 €"', '', head, *rows]
    return '\r\n'.join(lines) + '\r\n'


def old_row(day, text, party, purpose, account, amount):
    cells = [day, day, text, party, purpose, account, '12030000' if account else '', amount, '', '', '', '']
    return ';'.join(f'"{cell}"' for cell in cells)


def old_format(*rows, iban=OWN_IBAN, head=OLD_HEAD):
    lines = ['"Kontonummer:";"%s"' % iban, '""', '"Von:";"01.01.2022"', '"Bis:";"31.12.2022"',
             '"Kontostand vom 31.12.2022:";"1.000,00 EUR"', '""', head, *rows]
    return '\r\n'.join(lines) + '\r\n'


NEW_ROWS = (
    new_row('30.12.24', 'ISSUER', 'S-Bahn Beispielstadt GmbH', 'Fahrkarte vom 29.12.2024', '-15', iban=PEER_IBAN),
    new_row('02.12.24', 'Musterkasse Vers.', 'Erika Probe', '000000000A00001 05.02.2024- 3', '1.234,56'),
    new_row('01.12.24', 'Erika Probe', 'Musterkasse Vers.', '000000000A00001 01.12.2024   Beitrag', '-895,56'),
)
OLD_ROWS = (
    old_row('30.12.2022', 'Abschluss', '', 'Abrechnung 30.12.2022  siehe Anlage', '', '-3,00'),
    old_row('15.11.2022', 'Lastschrift', 'STADTWERKE BEISPIEL', 'Abschlag Strom  Kundennummer 4711', '1234567890', '-89,00'),
    old_row('02.11.2022', 'Gutschrift', 'Mieter G', 'Miete Beispielweg 1 November', PEER_IBAN, '430,00'),
)
# dieselbe Tabelle, aber mit Komma getrennt, auf vier Spalten verkürzt und mit schon vor dem Schreiben verlorenem Umlaut
OLD_COMMA = ('Kontonummer:,' + OWN_IBAN + '\n,,,\nVon:,01.01.2023,,\nBis:,31.12.2023,,\n,,,\n'
             'Buchungstag,Auftraggeber / Beg�nstigter,Verwendungszweck,Betrag (EUR)\n'
             '15.11.2023,STADTWERKE BEISPIEL,Abschlag Strom,"-89,00"\n')


def read(text, encoding='utf-8-sig'):
    return parse_csv(text.encode(encoding))


def rows_of(statement):
    return [(t.date, t.amount, t.recipient, t.reference, t.foreign_iban) for t in statement.transactions]


# ══ parse_grouped_amount ═════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('text, expected', [
    ('1.500', '1500'), ('-15.000', '-15000'), ('-1.100', '-1100'), ('12.345.678', '12345678'), ('-15', '-15'),
    ('1.234,56', '1234.56'), ('+3,5', '3.5'), ('0', '0'), ('0,50', '0.50'), ('-0,01', '-0.01'), ('1234,56', '1234.56'),
    ('-1.234.567,89', '-1234567.89'), ('15', '15'), ('999.999.999.999,99', '999999999999.99'),
    ('10.956,80 €', '10956.80'), ('10.956,80 €', '10956.80'), ('15,00 EUR', '15.00'), ('15,00EUR', '15.00'),
    (' 12,5 ', '12.5'), ('  -41,25  ', '-41.25'), ('\t1.000,00\n', '1000.00'), ('15,00  EUR ', '15.00'),
])
def test_grouped_amounts_are_read_exactly_with_the_dot_as_thousands_separator(text, expected):
    result = parse_grouped_amount(text)
    assert isinstance(result, Decimal) and result == Decimal(expected)


@pytest.mark.parametrize('text', [
    '12.5', '1.5', '1.23', '1.2345', '0.500', '00.500', '-0.001', '1,234.56', '1e3', '1E3', '1_000', 'nan', 'NaN', 'inf', '-inf', 'Infinity',
    '١٢', '١٢,٣٤', '１２,３４', '', ' ', '\n', '12,345', '12,', ',5', '.5', '5.', '1.500.', '--1', '+-1', '-', '+', ',', '.',
    '€', 'EUR', 'EUR 12', '€ 12', '12 EUR EUR', '12,5%', '(12,5)', '1.2.3,4', '12.34,56', '0x10', 'abc', '1,5 EUR x',
    '1.000.000.000.000', '1000000000000', '-1.000.000.000.000,00', '9' * 31, '0' * 31, '1,' + '1' * 29,
    # Leerraum zwischen den Zeichen eines Betrags: zwei Zahlen sind nie eine (``12 34`` ist nicht 1234)
    '1 234,56', '12 34', '1 2', '- 5', '-\t5', '1. 500', '1 .500', '12 ,50', '12, 50', '1\u00a0234', '1\u202f234,00',
])
def test_everything_else_is_not_an_amount(text):
    assert parse_grouped_amount(text) is None


@pytest.mark.parametrize('value', [None, 5, 5.5, Decimal('5'), b'5', ['5']])
def test_only_text_is_an_amount(value):
    assert parse_grouped_amount(value) is None


def test_thirty_characters_are_the_limit():
    assert parse_grouped_amount('0' * 18 + '1' * 12) == Decimal('1' * 12)       # 30 Zeichen
    assert parse_grouped_amount('0' * 19 + '1' * 12) is None                     # 31 Zeichen


def test_a_huge_padded_value_is_cheap_and_none():
    assert parse_grouped_amount(' ' * 1_000_000 + 'x' + ' ' * 1_000_000) is None


def test_the_decimal_keeps_its_scale():
    """Kein Umweg über float: ``0,10`` bleibt ``0.10``."""
    assert str(parse_grouped_amount('0,10')) == '0.10'
    assert parse_grouped_amount('0,1') + parse_grouped_amount('0,2') == parse_grouped_amount('0,3')


def test_the_two_readers_differ_on_purpose_for_a_dot_without_a_comma():
    """Die DKB lässt bei runden Beträgen die Nachkommastellen weg: ``1.500`` heißt 1500. ``parse_amount`` (WISO-Textexport,
    PDF-Auszüge) liest einen Punkt ohne Komma weiter als Dezimalpunkt (1,5) – bewusst nicht geändert."""
    assert parse_grouped_amount('1.500') == Decimal('1500')
    assert parse_amount('1.500') == Decimal('1.500')
    assert parse_grouped_amount('-15.000') == Decimal('-15000') and parse_amount('-15.000') == Decimal('-15.000')


# ══ Beide Formate ════════════════════════════════════════════════════════════════════════════════════════════════

def test_the_new_format_is_read_with_the_direction_picking_the_counterparty():
    statement = read(new_format(*NEW_ROWS))
    assert rows_of(statement) == [
        ('2024-12-30', -15.0, 'S-Bahn Beispielstadt GmbH', 'Fahrkarte vom 29.12.2024', PEER_IBAN),
        ('2024-12-02', 1234.56, 'Musterkasse Vers.', '000000000A00001 05.02.2024- 3', ''),     # Gutschrift: Zahlungspflichtiger
        ('2024-12-01', -895.56, 'Musterkasse Vers.', '000000000A00001 01.12.2024 Beitrag', ''),   # Belastung: Zahlungsempfänger
    ]
    assert statement.bank_code == 'DKB' and statement.iban == OWN_IBAN


def test_the_old_format_has_one_counterparty_column_and_the_sign_is_the_direction():
    statement = read(old_format(*OLD_ROWS), 'cp1252')
    assert rows_of(statement) == [
        ('2022-12-30', -3.0, '', 'Abrechnung 30.12.2022 siehe Anlage', ''),               # nichts erfunden: Gegenseite leer
        ('2022-11-15', -89.0, 'STADTWERKE BEISPIEL', 'Abschlag Strom Kundennummer 4711', ''),   # alte Kontonummer: keine IBAN
        ('2022-11-02', 430.0, 'Mieter G', 'Miete Beispielweg 1 November', PEER_IBAN),
    ]
    assert statement.bank_code == 'DKB' and statement.iban == OWN_IBAN


def test_the_comma_separated_variant_with_a_lost_umlaut_is_read():
    """Kein Semikolon, vier Spalten – und der Umlaut von „Begünstigter“ ging schon beim Schreiben verloren: Keine Spalte
    erkennt sich an einem Umlaut."""
    statement = read(OLD_COMMA)
    assert rows_of(statement) == [('2023-11-15', -89.0, 'STADTWERKE BEISPIEL', 'Abschlag Strom', '')]
    assert statement.iban == OWN_IBAN


def test_the_delimiter_is_read_off_the_header_not_the_amounts():
    """Jeder deutsche Betrag trägt ein Komma: gezählt über die ganze Datei sähe ein Semikolon-Export wie ein Komma-Export aus."""
    assert len(read(new_format(*NEW_ROWS)).transactions) == 3 and len(read(old_format(*OLD_ROWS)).transactions) == 3


def test_a_bom_does_not_hide_the_first_column():
    """Mit BOM hieße die erste Spalte ``\\ufeffBuchungsdatum`` und würde nie gefunden."""
    text = new_format(*NEW_ROWS)
    assert len(parse_csv(b'\xef\xbb\xbf' + text.encode('utf-8')).transactions) == 3
    assert len(parse_csv(text.encode('utf-8')).transactions) == 3                  # ohne BOM ebenso
    assert len(parse_csv(('﻿' + text).encode('utf-8')).transactions) == 3


def test_windows_1252_is_read_and_utf_8_comes_first():
    umlauts = new_format(new_row('02.12.24', 'Musterkasse', 'Bäckerei Müller & Söhne', 'Brötchen für Größe 5', '-4,50'))
    for encoding in ('utf-8-sig', 'cp1252'):
        (_, _, recipient, reference, _), = rows_of(read(umlauts, encoding))
        assert (recipient, reference) == ('Bäckerei Müller & Söhne', 'Brötchen für Größe 5'), encoding


def test_a_file_that_is_neither_utf_8_nor_windows_1252_is_still_read():
    """Windows-1252 kennt fünf Bytes nicht (0x81, 0x8D, 0x8F, 0x90, 0x9D): Dann Ersatzzeichen statt eines Abbruchs – die
    Kopfzeile ist ASCII, nichts erkennt sich an einem Umlaut."""
    raw = new_format(new_row('02.12.24', 'Musterkasse', 'Laden', 'Zweck', '-4,50')).encode('cp1252') + b'\x81'
    assert len(parse_csv(raw).transactions) == 1


def test_columns_are_found_by_meaning_so_an_extra_column_in_between_changes_nothing():
    head = ('"Buchungstag";"Wertstellung";"Umsatztyp";"Auftraggeber / Begünstigter";"Neue Spalte";"Verwendungszweck";'
            '"Betrag (EUR)"')
    text = '\r\n'.join(['"Konto";"%s"' % OWN_IBAN, head,
                         '"15.11.2022";"15.11.2022";"Ausgang";"WER";"ZUSATZ";"WOFÜR";"-89,00"']) + '\r\n'
    assert rows_of(read(text)) == [('2022-11-15', -89.0, 'WER', 'WOFÜR', '')]


def test_the_value_date_and_the_type_are_never_taken_for_the_amount():
    """„Wertstellung“ enthält *wert*, „Umsatztyp“ *umsatz*: eine weite Wortliste läse das Datum als Betrag."""
    head = '"Buchungstag";"Wertstellung";"Umsatztyp";"Auftraggeber";"Verwendungszweck"'
    with pytest.raises(DkbCsvError, match='Betrag'):
        read(head + '\r\n"15.11.2022";"15.11.2022";"Ausgang";"WER";"WOFÜR"\r\n')


def test_the_purpose_is_squeezed_and_may_span_lines():
    row = new_row('02.12.24', 'Musterkasse', 'Laden', 'Zeile 1\r\n   Zeile  2\tEnde', '-1')
    statement = read(new_format(row))
    assert statement.transactions[0].reference == 'Zeile 1 Zeile 2 Ende'


def test_the_purpose_is_not_cut_short():
    long_text = 'Wort ' * 200
    statement = read(new_format(new_row('02.12.24', 'Musterkasse', 'Laden', long_text, '-1')))
    assert statement.transactions[0].reference == long_text.strip() and len(statement.transactions[0].reference) > 200


def test_a_whole_iban_of_the_other_side_is_taken_and_anything_else_is_not():
    rows = [new_row('02.12.24', 'Musterkasse', 'A', 'x', '-1', iban=iban)
            for iban in (PEER_IBAN, 'DE02 1203 0000 0000 2020 51', 'de02120300000000202051', 'DE12', '1234567890', '')]
    ibans = [t.foreign_iban for t in read(new_format(*rows)).transactions]
    assert ibans == [PEER_IBAN, PEER_IBAN, PEER_IBAN, '', '', '']


def test_amounts_are_cent_rounded_floats_like_camt_and_round_amounts_keep_their_thousands():
    rows = [new_row('02.12.24', 'Musterkasse', 'A', 'x', amount) for amount in ('-15.000', '1.500', '-15', '0,10', '1.234,56')]
    amounts = [t.amount for t in read(new_format(*rows)).transactions]
    assert amounts == [-15000.0, 1500.0, -15.0, 0.1, 1234.56] and all(type(a) is float for a in amounts)
    assert [to_minor(a) for a in amounts] == [-150000000, 15000000, -150000, 1000, 12345600]


def test_dates_with_a_two_or_four_digit_year_are_iso_dates():
    """Das zweistellige Jahr zuerst: als ``%Y`` gelesen wäre ``24`` das Jahr vierundzwanzig."""
    rows = [new_row(day, 'M', 'A', 'x', '-1') for day in ('06.02.24', '30.12.2022', '1.2.2024', '29.02.24')]
    assert [t.date for t in read(new_format(*rows)).transactions] == ['2024-02-06', '2022-12-30', '2024-02-01', '2024-02-29']


# ══ Befunde ══════════════════════════════════════════════════════════════════════════════════════════════════════

def test_there_is_never_an_opening_balance_so_no_balance_is_always_set():
    assert read(new_format(*NEW_ROWS)).warnings == ['no_balance']
    assert read(old_format(*OLD_ROWS)).warnings == ['no_balance']
    assert read(new_format()).warnings == ['no_balance']


def test_pending_rows_are_skipped_with_a_finding():
    rows = (new_row('03.12.24', 'Erika Probe', 'Laden', 'noch offen', '-9,99', status='Vorgemerkt'),
            new_row('02.12.24', 'Erika Probe', 'Laden', 'fertig', '-5,00'),
            new_row('04.12.24', 'Erika Probe', 'Laden', 'noch offen 2', '-1', status='  vorgemerkt (Kartenumsatz)'))
    statement = read(new_format(*rows))
    assert [t.reference for t in statement.transactions] == ['fertig']
    assert statement.warnings == ['skipped_status', 'no_balance']


def test_the_old_format_has_no_status_so_everything_counts_as_booked():
    assert 'skipped_status' not in read(old_format(*OLD_ROWS)).warnings


def test_a_row_with_a_date_but_no_readable_amount_is_not_imported_and_flagged():
    rows = (new_row('02.12.24', 'M', 'Laden', 'ohne Betrag', ''),
            new_row('03.12.24', 'M', 'Laden', 'englisch', '12.5'),
            new_row('04.12.24', 'M', 'Laden', 'Text', 'abc'),
            new_row('05.12.24', 'M', 'Laden', 'gut', '-3,00'))
    statement = read(new_format(*rows))
    assert [t.reference for t in statement.transactions] == ['gut']
    assert statement.warnings == ['unparsed', 'no_balance']


def test_a_row_with_an_amount_but_no_valid_date_is_flagged_as_well():
    """Nichts wird still weggeworfen, was einen lesbaren Betrag trägt."""
    rows = (new_row('31.02.24', 'M', 'Laden', 'gibt es nicht', '-1,00'), new_row('2024-12-02', 'M', 'Laden', 'ISO', '-2,00'),
            new_row('05.12.24', 'M', 'Laden', 'gut', '-3,00'))
    statement = read(new_format(*rows))
    assert [t.reference for t in statement.transactions] == ['gut'] and statement.warnings == ['unparsed', 'no_balance']


def test_a_row_without_a_date_but_with_something_in_the_amount_column_is_flagged_not_dropped():
    """Still übergangen wird nur, was nichts in der Betragsspalte trägt – ein unlesbarer Betrag ohne Datum fehlt sonst
    ohne ein Wort."""
    rows = (new_row('', 'M', 'Laden', 'beides kaputt', '12.5'), new_row('irgendwann', 'M', 'Laden', 'Text', 'abc'),
            new_row('05.12.24', 'M', 'Laden', 'gut', '-3,00'))
    statement = read(new_format(*rows))
    assert [t.reference for t in statement.transactions] == ['gut'] and statement.warnings == ['unparsed', 'no_balance']


@pytest.mark.parametrize('day', ['05.01.0024', '31.12.1989', '01.01.0001', '31.12.99', '01.01.2999', '31.12.9999'])
def test_a_date_no_bank_can_have_booked_on_is_not_a_date(day):
    """Ein Tippfehler im Jahr oder ein zweistelliges Jahr aus dem letzten Jahrhundert (``99`` wäre 2099) landete sonst als
    Buchung in einem Jahr, das niemand ansieht."""
    statement = read(new_format(new_row(day, 'M', 'Laden', 'Jahr', '-1,00'), new_row('05.12.24', 'M', 'Laden', 'gut', '-3,00')))
    assert [t.reference for t in statement.transactions] == ['gut'] and statement.warnings == ['unparsed', 'no_balance']


def test_the_window_of_dates_moves_with_the_clock_and_is_read_freshly():
    from datetime import date as real_date, timedelta

    def day_text(day):
        return day.strftime('%d.%m.%Y')

    # zwei Tage Abstand zur Grenze: Der Test darf über Mitternacht laufen
    today = real_date.today()
    soon, late = today + timedelta(days=dkb_csv.MAX_DAYS_AHEAD - 2), today + timedelta(days=dkb_csv.MAX_DAYS_AHEAD + 2)
    rows = (new_row(day_text(today), 'M', 'L', 'heute', '-1'), new_row(day_text(soon), 'M', 'L', 'Rand', '-1'),
            new_row(day_text(late), 'M', 'L', 'zu spät', '-1'), new_row('01.01.1990', 'M', 'L', 'früh', '-1'))
    statement = read(new_format(*rows))
    assert [t.reference for t in statement.transactions] == ['heute', 'Rand', 'früh']
    assert statement.warnings == ['unparsed', 'no_balance'] and dkb_csv.MIN_YEAR == 1990


# ── verrutschte Zeilen ──

def test_an_unquoted_delimiter_in_a_text_never_turns_the_bank_code_into_an_amount():
    """Altes Format ohne Anführung: Ein Semikolon im Verwendungszweck schiebt jede Zelle dahinter um eins – in der
    Betragsspalte steht dann die Bankleitzahl, und ``12030000`` wäre ein lesbarer Betrag."""
    head = 'Buchungstag;Wertstellung;Buchungstext;Auftraggeber / Begünstigter;Verwendungszweck;Kontonummer;BLZ;Betrag (EUR)'
    good = '15.11.2022;15.11.2022;Lastschrift;STADTWERKE BEISPIEL;Abschlag Strom;1234567890;12030000;-89,00'
    shifted = '16.11.2022;16.11.2022;Lastschrift;STADTWERKE BEISPIEL;Abschlag; Strom;1234567890;12030000;-89,00'
    statement = read(f'Kontonummer:;{OWN_IBAN}\r\n{head}\r\n{good}\r\n{shifted}\r\n')
    assert rows_of(statement) == [('2022-11-15', -89.0, 'STADTWERKE BEISPIEL', 'Abschlag Strom', '')]
    assert statement.warnings == ['unparsed', 'no_balance']


def test_an_unquoted_amount_in_a_comma_file_is_never_read_as_whole_euros():
    """``-89,50`` ohne Anführung sind in einer Komma-Datei zwei Zellen: ``-89`` wäre ein lesbarer, falscher Betrag."""
    text = ('Kontonummer:,' + OWN_IBAN + '\nBuchungstag,Auftraggeber / Beguenstigter,Verwendungszweck,Betrag (EUR)\n'
            '15.11.2023,STADTWERKE BEISPIEL,Abschlag Strom,-89,50\n16.11.2023,STADTWERKE BEISPIEL,Abschlag Gas,"-12,00"\n')
    statement = read(text)
    assert [(t.amount, t.reference) for t in statement.transactions] == [(-12.0, 'Abschlag Gas')]
    assert statement.warnings == ['unparsed', 'no_balance']


def test_a_shifted_row_is_flagged_even_when_the_extra_cells_are_empty():
    """Die überzählige Zelle am Ende ist oft die leere letzte Spalte der Zeile – zählte sie nicht, bliebe die Verschiebung
    unbemerkt."""
    shifted = new_row('03.12.24', 'M', 'Laden";"dazwischen', 'x', '-7,00')
    statement = read(new_format(shifted, new_row('05.12.24', 'M', 'Laden', 'gut', '-3,00')))
    assert [t.reference for t in statement.transactions] == ['gut'] and statement.warnings == ['unparsed', 'no_balance']


def test_a_row_that_ends_before_the_amount_purpose_or_counterparty_is_flagged():
    head = '"Buchungstag";"Betrag (EUR)";"Auftraggeber / Begünstigter";"Verwendungszweck"'
    text = (f'"Kontonummer:";"{OWN_IBAN}"\r\n{head}\r\n"15.11.2022";"-1,00";"A";"ganz"\r\n"16.11.2022";"-2,00";"A"\r\n'
            '"17.11.2022";"-3,00"\r\n"18.11.2022"\r\n')
    statement = read(text)
    assert [t.reference for t in statement.transactions] == ['ganz'] and statement.warnings == ['unparsed', 'no_balance']


def test_a_trailing_delimiter_on_header_and_rows_is_the_old_format_and_reads_fine():
    assert len(read(old_format(*OLD_ROWS)).transactions) == 3 and OLD_HEAD.endswith(';')


def test_a_footer_without_a_date_and_without_an_amount_is_passed_over_silently():
    footer = ('', ';;;;;;;;', '"Summe";"";"";"";"";"";"";"";"";"";"";""', '"Kontostand vom 01.06.2026:";"10.956,80 €"')
    statement = read(new_format(*NEW_ROWS, *footer))
    assert len(statement.transactions) == 3 and statement.warnings == ['no_balance']      # kein ``unparsed``


def test_the_iban_comes_from_the_preamble_without_spaces_and_without_it_the_finding_is_no_iban():
    spaced = read(new_format(*NEW_ROWS, iban='DE89 3704 0044 0532 0130 00'))
    assert spaced.iban == OWN_IBAN
    absent = read(new_format(*NEW_ROWS, iban=None))
    assert absent.iban is None
    assert check_statement(absent).warnings == ['no_balance', 'no_iban']               # ``check_statement`` setzt ihn
    assert check_statement(read(new_format(*NEW_ROWS))).warnings == ['no_balance']


def test_a_list_without_transactions_is_flagged_by_the_central_check():
    statement = check_statement(read(new_format()))
    assert statement.transactions == [] and statement.warnings == ['no_balance', 'no_transactions']


def test_the_counterpart_iban_in_a_data_row_is_never_taken_for_the_account():
    """Nur der Vorspann nennt das Konto – die IBAN einer Zeile gehört der Gegenseite."""
    statement = read(new_format(*NEW_ROWS, iban=None))
    assert statement.iban is None and statement.transactions[0].foreign_iban == PEER_IBAN


# ══ document_date und die Plausibilitätsprüfung ══════════════════════════════════════════════════════════════════

def test_a_whole_year_export_raises_no_date_range_finding():
    """``daterange`` misst ein Datum am Belegdatum (mehr als 400 Tage davor, mehr als 31 danach). Eine Umsatzliste hat keines –
    sie umfasst einen beliebigen Zeitraum und wird später exportiert („Kontostand vom 01.06.2026“): Mit ``document_date=None``
    entfällt die Prüfung, statt bei einem Jahresexport Zeilen zu Unrecht zu melden."""
    days = ('02.01.22', '30.06.22', '31.12.22', '15.05.23', '30.12.24')
    rows = [new_row(day, 'M', 'Laden', f'Umsatz {day}', '-1,00') for day in days]
    statement = check_statement(read(new_format(*rows)))
    assert statement.document_date is None
    assert [t.warnings for t in statement.transactions] == [[]] * 5


def test_the_serialised_statement_carries_no_document_date():
    data = check_statement(read(new_format(*NEW_ROWS))).as_dict()
    assert data['document_date'] is None and data['bank_code'] == 'DKB' and data['currency'] is None
    json.dumps(data)                                                                # stand für den Upload JSON-fähig


# ══ Fehler ohne Feldwerte, Grenzen ═══════════════════════════════════════════════════════════════════════════════

SECRET = 'GEHEIMER-WERT-4711'


@pytest.mark.parametrize('raw', [b'', b'a;b\n1;2\n', b'%PDF-1.4 binaer', b'"Wert";"Betrag"\n"x";"1"\n',
                                 new_format().replace('Buchungsdatum', 'Datum').encode()])
def test_a_file_without_the_header_row_is_not_recognised(raw):
    with pytest.raises(DkbCsvError) as caught:
        parse_csv(raw)
    assert str(caught.value) == NOT_RECOGNISED and 'DKB' in NOT_RECOGNISED


def test_the_header_must_be_near_the_top():
    preamble = ''.join(f'"Zeile";"{n}"\r\n' for n in range(dkb_csv.MAX_PREAMBLE_ROWS + 5))
    with pytest.raises(DkbCsvError, match='nicht als Kontoauszug erkannt'):
        parse_csv((preamble + new_format(*NEW_ROWS)).encode())


def test_a_missing_amount_column_names_the_meaning_but_no_field_value():
    head = NEW_HEAD.replace('"Betrag (€)"', '"Summe"')
    text = new_format(new_row('02.12.24', SECRET, 'Laden', SECRET, '-1,00'), head=head)
    with pytest.raises(DkbCsvError) as caught:
        parse_csv(text.encode())
    message = str(caught.value)
    assert 'Betrag' in message and 'Gegenseite' not in message
    assert SECRET not in message and '-1,00' not in message and OWN_IBAN not in message
    assert 'Buchungsdatum' in message and 'Summe' in message                      # Spaltennamen der Kopfzeile dürfen stehen


def test_a_missing_counterparty_column_names_itself():
    head = '"Buchungstag";"Wertstellung";"Verwendungszweck";"Betrag (EUR)"'
    with pytest.raises(DkbCsvError, match='Gegenseite') as caught:
        read(head + f'\r\n"15.11.2022";"15.11.2022";"{SECRET}";"-1,00"\r\n')
    assert SECRET not in str(caught.value)


def test_only_one_of_the_two_sides_is_not_enough():
    head = '"Buchungsdatum";"Zahlungsempfänger*in";"Verwendungszweck";"Betrag (€)"'
    with pytest.raises(DkbCsvError, match='Gegenseite'):
        read(head + '\r\n"15.11.22";"A";"B";"-1,00"\r\n')


def test_one_side_next_to_a_single_column_is_not_enough_either():
    """Die einzelne Spalte trüge sonst für jede Zeile die Gegenseite – neben einer Spalte, die nur für eine Richtung gilt."""
    head = '"Buchungsdatum";"Zahlungsempfänger*in";"Auftraggeber";"Verwendungszweck";"Betrag (€)"'
    with pytest.raises(DkbCsvError, match='Gegenseite'):
        read(head + '\r\n"15.11.22";"A";"B";"C";"-1,00"\r\n')


def test_a_missing_purpose_column_names_itself():
    head = '"Buchungstag";"Wertstellung";"Auftraggeber / Begünstigter";"Betrag (EUR)"'
    with pytest.raises(DkbCsvError, match='Spalte für Verwendungszweck\\.') as caught:
        read(head + f'\r\n"15.11.2022";"15.11.2022";"{SECRET}";"-1,00"\r\n')
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize('name', ['Betrag (USD)', 'Betrag ( chf )', 'Betrag (GBP)'])
def test_a_list_in_another_currency_is_refused_with_a_fixed_sentence(name):
    text = new_format(new_row('02.12.24', SECRET, 'Laden', SECRET, '-1,00'), head=NEW_HEAD.replace('Betrag (€)', name))
    with pytest.raises(DkbCsvError) as caught:
        read(text)
    assert str(caught.value) == dkb_csv.FOREIGN_CURRENCY and 'Euro' in dkb_csv.FOREIGN_CURRENCY


@pytest.mark.parametrize('name', ['Betrag (€)', 'Betrag (EUR)', 'Betrag (eur)', 'Betrag', 'Betrag (\ufffd)', 'Betrag in EUR'])
def test_euro_or_no_currency_in_the_column_name_is_read(name):
    text = new_format(new_row('02.12.24', 'M', 'Laden', 'x', '-1,00'), head=NEW_HEAD.replace('Betrag (€)', name))
    assert [t.amount for t in read(text).transactions] == [-1.0]


def test_a_row_with_an_amount_in_another_currency_is_flagged():
    statement = read(new_format(new_row('02.12.24', 'M', 'Laden', 'x', '-1,00 USD'), new_row('03.12.24', 'M', 'L', 'gut', '-1')))
    assert [t.reference for t in statement.transactions] == ['gut'] and 'unparsed' in statement.warnings


def test_both_missing_are_named_together():
    with pytest.raises(DkbCsvError, match='Betrag und Gegenseite'):
        read('"Buchungsdatum";"Verwendungszweck"\r\n"15.11.22";"B"\r\n')


def test_column_names_in_a_message_are_cut_to_forty_characters_and_their_number_is_limited():
    long_name = 'X' * 80
    head = '"Buchungsdatum";"%s";' % long_name + ';'.join(f'"Spalte {n}"' for n in range(60))
    with pytest.raises(DkbCsvError) as caught:
        read(head + '\r\n')
    message = str(caught.value)
    assert 'X' * 40 in message and 'X' * 41 not in message
    assert f'Spalte {dkb_csv.MAX_NAMES_SHOWN}' not in message and message.count('Spalte ') < dkb_csv.MAX_NAMES_SHOWN + 2
    assert ' …' in message


def test_a_header_with_absurdly_many_columns_is_not_a_dkb_list():
    with pytest.raises(DkbCsvError, match='nicht als Kontoauszug erkannt'):
        parse_csv(('Buchungsdatum;' + ';'.join('x' * 1 for _ in range(dkb_csv.MAX_COLUMNS + 5)) + '\n').encode())


def test_a_line_with_absurdly_many_delimiters_stops_before_it_becomes_a_list():
    """Vorspann wie Tabelle: Das csv-Modul begrenzt die Länge eines Feldes, nicht ihre Zahl – zwanzig Millionen Semikolons
    wären zwanzig Millionen Zellen."""
    import time
    wide = ';' * (dkb_csv.MAX_LINE_DELIMITERS + 1)
    for text in (wide + '\r\n' + new_format(*NEW_ROWS), new_format(*NEW_ROWS) + wide + '\r\n'):
        with pytest.raises(DkbCsvError) as caught:
            read(text)
        assert str(caught.value) == UNREADABLE
    assert len(read(';' * dkb_csv.MAX_LINE_DELIMITERS + '\r\n' + new_format(*NEW_ROWS)).transactions) == 3
    huge = (';' * (MAX_CSV_BYTES - 1000) + '\r\n' + new_format(*NEW_ROWS)).encode()
    started = time.perf_counter()
    with pytest.raises(DkbCsvError):
        parse_csv(huge)
    assert time.perf_counter() - started < 5


def test_the_iban_is_looked_for_in_the_first_columns_of_a_preamble_row_only():
    far = ';' * (dkb_csv.MAX_COLUMNS + 5) + OWN_IBAN + '\r\n'
    assert read(far + new_format(*NEW_ROWS, iban=None)).iban is None
    near = ';' * (dkb_csv.MAX_COLUMNS - 1) + OWN_IBAN + '\r\n'
    assert read(near + new_format(*NEW_ROWS, iban=None)).iban == OWN_IBAN


def test_a_file_over_the_size_limit_is_refused_unread():
    assert MAX_CSV_BYTES == 20 * 1024 * 1024
    with pytest.raises(DkbCsvError) as caught:
        parse_csv(b'x' * (MAX_CSV_BYTES + 1))
    assert str(caught.value) == TOO_BIG and '20 MB' in TOO_BIG


def test_the_row_limit_counts_every_row_after_the_header():
    assert MAX_CSV_ROWS == 100_000
    row = new_row('02.12.24', 'M', 'Laden', 'Zweck', '-1')
    head = new_format().rstrip('\r\n')
    assert len(read(head + '\r\n' + (row + '\r\n') * MAX_CSV_ROWS).transactions) == MAX_CSV_ROWS
    with pytest.raises(DkbCsvError, match='100.000 Zeilen'):
        read(head + '\r\n' + (row + '\r\n') * (MAX_CSV_ROWS + 1))


def test_blank_lines_count_towards_the_row_limit_and_stop_quickly():
    with pytest.raises(DkbCsvError, match='Zeilen'):
        read(new_format().rstrip('\r\n') + '\r\n' + '\r\n' * (MAX_CSV_ROWS + 10))


def test_an_unclosed_quote_that_swallows_the_file_is_unreadable_not_a_crash():
    """Ein Feld über der Standardgrenze des csv-Moduls (131072 Zeichen): fester Satz, kein Feldwert."""
    text = new_format().rstrip('\r\n') + '\r\n"01.01.24";"01.01.24";"Gebucht";"A";"B";"' + SECRET * 30000 + '\r\n'
    with pytest.raises(DkbCsvError) as caught:
        read(text)
    assert str(caught.value) == UNREADABLE and SECRET not in str(caught.value)


def test_the_global_field_size_limit_of_the_csv_module_is_left_alone():
    import csv
    before = csv.field_size_limit()
    read(new_format(*NEW_ROWS))
    assert csv.field_size_limit() == before == 131072


def test_the_module_never_writes_to_the_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    read(new_format(*NEW_ROWS))
    assert list(tmp_path.iterdir()) == []


# ══ Upload ═══════════════════════════════════════════════════════════════════════════════════════════════════════

class _FakeRequest:
    """Minimaler Ersatz für den BaseHTTPRequestHandler im Test."""

    def __init__(self, content_type, body):
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(body))}
        self.rfile = io.BytesIO(body)


def _multipart(files):
    boundary = 'GRENZE' + uuid.uuid4().hex
    body = b''
    for name, content in files:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                 'Content-Type: text/csv\r\n\r\n').encode() + content + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    return f'multipart/form-data; boundary={boundary}', body


def upload(db, *files):
    content_type, body = _multipart(files)
    status, text = upload_handler.handle_file_upload(_FakeRequest(content_type, body), db)
    assert status == 200, text
    return json.loads(text)


def _everything_under_the_data_dir():
    root = userctx.user_data_dir()
    return sorted(os.path.relpath(os.path.join(base, name), root)
                  for base, _dirs, names in os.walk(root) for name in names
                  if not name.endswith(('.json', '.log', '.sql')) and 'pending_imports' not in base)


@pytest.fixture
def giro(tmp_db):
    tmp_db.insert_account('Giro', 'Owner', OWN_IBAN, 'TESTDEFF', 'Testbank', skr_account=1800)
    return tmp_db


def test_a_dkb_list_goes_into_the_preview_like_a_statement_and_is_never_stored(giro):
    data = upload(giro, ('umsaetze.csv', new_format(*NEW_ROWS).encode('utf-8-sig')))
    assert data['other_files'] == [] and data['import_id']
    (card,) = data['files']
    assert card['filename'] == 'umsaetze.csv' and card['bank_code'] == 'DKB' and card['iban'] == OWN_IBAN
    assert card['document_date'] is None and card['currency'] is None
    assert card['account_name'] == 'Giro' and card['status'] == 'warn'                  # ``no_balance``
    assert card['warnings'] == ['no_balance']
    assert (card['total'], card['new_count'], card['dup_count']) == (3, 3, 0)
    assert [t['amount'] for t in card['transactions']] == [-15.0, 1234.56, -895.56]
    documents = os.path.join(userctx.user_data_dir(), 'Documents')
    assert os.listdir(documents) == []                                                  # nichts abgelegt
    assert _everything_under_the_data_dir() == []


def test_the_extension_is_matched_without_regard_to_case_and_the_name_never_leaves_the_folder(giro):
    data = upload(giro, ('../../UMSAETZE.CSV', old_format(*OLD_ROWS).encode('cp1252')))
    (card,) = data['files']
    assert card['filename'] == 'UMSAETZE.CSV' and card['total'] == 3
    assert os.listdir(os.path.join(userctx.user_data_dir(), 'Documents')) == []


def test_a_list_for_an_unknown_account_shows_up_as_an_error_card(giro):
    (card,) = upload(giro, ('umsaetze.csv', new_format(*NEW_ROWS, iban='DE00000000001000000001').encode()))['files']
    assert card['status'] == 'error' and card['account_id'] is None
    absent = upload(giro, ('umsaetze.csv', new_format(*NEW_ROWS, iban=None).encode()))['files'][0]
    assert absent['status'] == 'error' and 'no_iban' in absent['warnings']


def test_a_foreign_csv_is_a_warning_and_is_not_filed_as_a_receipt(giro):
    data = upload(giro, ('anderes.csv', b'Name;Wert\r\nA;1\r\n'))
    assert data['files'] == [] and data['import_id'] is None
    assert data['other_files'] == [{'filename': 'anderes.csv', 'status': 'warning', 'error': NOT_RECOGNISED}]
    assert NOT_RECOGNISED == ('Die CSV-Datei wurde nicht als Kontoauszug erkannt (unterstützt wird die Umsatzliste der DKB) '
                              '– nichts importiert.')
    assert os.listdir(os.path.join(userctx.user_data_dir(), 'Documents')) == []


def test_a_dkb_list_with_a_missing_column_is_a_warning_with_the_clear_text(giro):
    head = NEW_HEAD.replace('"Betrag (€)"', '"Summe"')
    data = upload(giro, ('kaputt.csv', new_format(new_row('02.12.24', SECRET, 'Laden', SECRET, '-1,00'), head=head).encode()))
    (entry,) = data['other_files']
    assert entry['status'] == 'warning' and 'Betrag' in entry['error'] and SECRET not in entry['error']
    assert data['files'] == [] and os.listdir(os.path.join(userctx.user_data_dir(), 'Documents')) == []


def test_an_unexpected_error_in_the_reader_is_a_fixed_sentence_and_the_text_stays_in_the_log(giro, monkeypatch, capsys):
    def boom(data):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(dkb_csv, 'parse_csv', boom)
    data = upload(giro, ('umsaetze.csv', new_format(*NEW_ROWS).encode()))
    assert data['other_files'] == [{'filename': 'umsaetze.csv', 'status': 'warning', 'error': UNREADABLE}]
    assert SECRET not in json.dumps(data)
    assert SECRET in capsys.readouterr().err                                            # der volle Fehler steht im Server-Log


def test_several_csv_files_and_a_foreign_one_can_be_mixed_in_one_upload(giro):
    data = upload(giro, ('a.csv', new_format(*NEW_ROWS).encode()), ('b.csv', b'x;y\r\n1;2\r\n'),
                  ('c.csv', old_format(*OLD_ROWS).encode('cp1252')))
    assert [card['filename'] for card in data['files']] == ['a.csv', 'c.csv']
    assert [card['file_index'] for card in data['files']] == [0, 1]
    assert [entry['filename'] for entry in data['other_files']] == ['b.csv']


def test_the_imported_rows_are_booked_on_the_account_with_exact_amounts(giro):
    data = upload(giro, ('umsaetze.csv', new_format(*NEW_ROWS).encode('utf-8-sig')))
    status, body = handlers.handle_confirm_import(giro, {'import_id': [data['import_id']]})
    assert status == 200 and json.loads(body)['results'][0]['inserted'] == 3
    with closing(giro._get_connection()) as con:
        booked = con.execute("SELECT DateBooking, Amount, RecipientClient, ForeignBankAccount FROM Bookings "
                             "WHERE BookingType = 'bank' ORDER BY DateBooking").fetchall()
    assert booked == [('2024-12-01', -8955600, 'Musterkasse Vers.', ''), ('2024-12-02', 12345600, 'Musterkasse Vers.', ''),
                      ('2024-12-30', -150000, 'S-Bahn Beispielstadt GmbH', PEER_IBAN)]


def test_the_same_list_twice_is_all_duplicates_the_second_time(giro):
    first = upload(giro, ('umsaetze.csv', new_format(*NEW_ROWS).encode()))
    handlers.handle_confirm_import(giro, {'import_id': [first['import_id']]})
    (card,) = upload(giro, ('umsaetze.csv', new_format(*NEW_ROWS).encode()))['files']
    assert (card['new_count'], card['dup_count']) == (0, 3) and all(t['dup'] for t in card['transactions'])


def test_the_statement_object_is_a_bank_statement_with_floats_for_the_import():
    statement = read(new_format(*NEW_ROWS))
    assert isinstance(statement, BankStatement) and isinstance(statement.document_date, (datetime, type(None)))
    assert all(isinstance(t.amount, float) for t in statement.transactions)
