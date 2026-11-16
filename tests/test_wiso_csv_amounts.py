# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Beträge im Textimport der WISO-Exporte (``Database.import_wiso_csv``) und in den PDF-Auszügen lesen streng und
ohne Kommazahlen (``money.parse_amount``):

* ``nan``, ``inf``, ``1e5``, ``1_000``, Ziffern anderer Schriften und alles, was keine deutsche oder Punkt-Schreibweise
  ist, wird eine Fehlerzeile („Zeile n: Ungültiger Betrag …“) – nichts wird angelegt, die übrigen Zeilen laufen
  weiter, und es gibt keinen 500.
* Der Steuerbetrag des Original-Exports wird exakt auf dem Brutto mit einer Rundung gerechnet (bei Beträgen mit zwei
  Nachkommastellen dasselbe wie ``money.tax_from_gross`` in Cent); der
  gespeicherte Betrag bleibt, was er war.
* Die Importer für Kontoauszüge (VBR, DKB) behandeln einen Betrag, den ``parse_amount`` ablehnt, wie eine Zeile ohne
  lesbaren Betrag.

Alle Namen, Nummern und Beträge sind erfunden.
"""
import io
import json
import os
from decimal import Decimal

import pytest

import money
import userctx
from document_parser import DocumentParser
from importers.vbr import parse_text
from server.handlers import handle_wiso_import
from tests.entry_form_helpers import csv_export, execute, query

TABLE_HEAD = ('Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;'
              'opt. Beleg Nr.;Betrag\n')
BAD_AMOUNTS = ['nan', 'NaN', 'inf', '-inf', 'Infinity', '1e5', '1E5', '1_000', '١٢,٣٤', '1.2.3,4', '1,2,3',
               '12,', ',5', '1 234,56', '--1', '1,234.56', '0x10', '119,00 EUR']


def _bookings(db):
    return query(db, 'SELECT COUNT(*) FROM Bookings')[0][0]


def _import_with_one_bad_line(db, amount):
    before = _bookings(db)
    result = db.import_wiso_csv(csv_export(('6815', '1800', amount, 'BAD-1', '401')))
    return result, before


# ── Original-Export ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('amount', BAD_AMOUNTS)
def test_an_invalid_amount_is_a_line_error_and_nothing_is_created(db_with_coa, amount):
    result, before = _import_with_one_bad_line(db_with_coa, amount)

    assert result['format'] == 'original'
    assert result['imported'] == 0 and result['skipped'] == 0
    assert result['errors'] == [f"Zeile 1: Ungültiger Betrag '{amount}'"]
    assert _bookings(db_with_coa) == before


def test_the_other_lines_of_the_file_are_still_imported(db_with_coa):
    before = _bookings(db_with_coa)
    result = db_with_coa.import_wiso_csv(csv_export(
        ('6815', '1800', '119,00', 'OK-1', '401'),
        ('6815', '1800', 'nan', 'BAD-1', '401'),
        ('6815', '1800', '1e5', 'BAD-2', '401'),
        ('6815', '1800', '59,50', 'OK-2', '401')))

    assert result['imported'] == 2
    assert [e.split(':')[0] for e in result['errors']] == ['Zeile 2', 'Zeile 3']
    assert _bookings(db_with_coa) == before + 2
    assert query(db_with_coa, "SELECT COUNT(*) FROM Bookings WHERE DocumentNumber LIKE 'BAD-%'") == [(0,)]


@pytest.mark.parametrize('amount, expected_minor', [
    ('119,00', 1190000), ('-41,25', -412500), ('1.234,56', 12345600), ('0,01', 100), ('7', 70000),
    ('1234.56', 12345600), ('+3,5', 35000), ('0', 0),
])
def test_valid_amounts_are_stored_as_before(db_with_coa, amount, expected_minor):
    """Das Vorzeichen richtet der Import an den liquiden Seiten aus (Bank = Gegenkonto: Abgang); der Betrag selbst
    steht exakt in Minor Units."""
    result = db_with_coa.import_wiso_csv(csv_export(('6815', '1800', amount, 'GOOD-1', '')))

    assert result['errors'] == [] and result['imported'] == 1
    (stored,) = query(db_with_coa, "SELECT Amount FROM Bookings WHERE DocumentNumber = 'GOOD-1'")[0]
    assert stored == -abs(expected_minor)


def test_the_result_file_of_the_page_can_still_be_written_with_the_amounts_of_skipped_rows(db_with_coa):
    """``skipped_rows`` geht als JSON in ``wiso_import_result.json`` – die Beträge bleiben Zahlen, kein Decimal."""
    data = csv_export(('6815', '1800', '119,00', 'DUP-1', '401'))
    db_with_coa.import_wiso_csv(data)

    result = db_with_coa.import_wiso_csv(data)

    assert result['skipped'] == 1
    (row,) = result['skipped_rows']
    assert row['betrag'] == -119.0 and isinstance(row['betrag'], float)
    json.dumps(result)                                   # kein Decimal im Ergebnis


# ── Steuerbetrag ─────────────────────────────────────────────────────────────

def _tax_of(db, ref):
    return query(db, 'SELECT TaxAmount, TaxRate FROM Bookings WHERE DocumentNumber = ?', (ref,))[0]


@pytest.mark.parametrize('cents', [1, 3, 72, 191, 310, 4125, 11900, 12345, 99999, 123456])
@pytest.mark.parametrize('key, percent', [('401', 19), ('402', 7), ('7', 16), ('6', 5)])
def test_the_tax_is_computed_by_money_py_in_cents_with_one_rounding(db_with_coa, cents, key, percent):
    """Rechnen in Minor Units und danach auf Cent runden ergäbe bei 0,72 € und 19 % 0,12 € statt 0,11 € (zweimal
    gerundet): Der Import rechnet in Cent, ``tax_from_gross`` hängt nicht von der Einheit ab."""
    amount = f'{cents // 100},{cents % 100:02d}'
    db_with_coa.import_wiso_csv(csv_export(('6815', '1800', amount, 'TAX-1', key, '15.03.2026')))

    tax, rate = _tax_of(db_with_coa, 'TAX-1')

    assert tax == -money.tax_from_gross(cents, percent) * 100      # Abgang: Vorzeichen des Betrags
    assert rate == pytest.approx(percent / 100)
    # genau die Rechnung, die der Import vorher mit Kommazahlen machte
    assert tax == money.to_minor(-round(cents / 100 - cents / 100 / (1 + percent / 100), 2))


@pytest.mark.parametrize('amount, tax_minor', [('0,155', 200), ('0,032', 100), ('0,094', 200), ('10,0049', 16000)])
def test_the_tax_of_a_gross_with_more_than_two_decimals_is_rounded_once_on_the_exact_gross(db_with_coa, amount, tax_minor):
    """0,155 € bei 19 % sind 0,02475 € Steuer, also 0,02 € – wie die Kommazahl-Rechnung von früher. Ein vorab auf den
    Cent gerundetes Brutto (0,16 €) ergäbe 0,03 €."""
    db_with_coa.import_wiso_csv(csv_export(('6815', '1800', amount, 'FINE-1', '401', '15.03.2026')))

    assert _tax_of(db_with_coa, 'FINE-1')[0] == -tax_minor


@pytest.mark.parametrize('amount', ['1234567890123456789012345', '1.000.000.000.000,00', '99999999999999999999,99'])
def test_an_amount_of_a_trillion_or_more_is_a_row_error_and_the_other_rows_are_imported(db_with_coa, amount):
    """Vorher brach ein solcher Betrag die ganze Datei mit einem technischen Fehler ab."""
    result = db_with_coa.import_wiso_csv(csv_export(('6815', '1800', amount, 'BIG-1', '401', '15.03.2026'),
                                                    ('6815', '1800', '11,90', 'BIG-2', '401', '15.03.2026')))

    assert query(db_with_coa, "SELECT COUNT(*) FROM Bookings WHERE DocumentNumber = 'BIG-1'") == [(0,)]
    assert query(db_with_coa, "SELECT COUNT(*) FROM Bookings WHERE DocumentNumber = 'BIG-2'") == [(1,)]
    assert any('Ungültiger Betrag' in error for error in result['errors'])


def test_a_tax_on_a_half_cent_boundary_follows_money_py(db_with_coa):
    """Bei 100 % und 0,03 € sind es genau 1,5 Cent: ``tax_from_gross`` rundet kaufmännisch auf 2 Cent – die
    Kommazahl-Rechnung von früher lag hier bei 1 Cent (0,015 ist als float knapp darunter)."""
    execute(db_with_coa, "INSERT INTO TaxKeys (Code, Description, TaxRate, TaxType) "
                         "VALUES ('7777', 'Testschlüssel 100 %', 1.0, 'USt')")

    db_with_coa.import_wiso_csv(csv_export(('6815', '1800', '0,03', 'HALF-1', '7777'),
                                           ('1800', '6815', '0,03', 'HALF-2', '7777')))

    assert _tax_of(db_with_coa, 'HALF-1')[0] == -money.tax_from_gross(3, 100) * 100 == -200
    assert _tax_of(db_with_coa, 'HALF-2')[0] == money.tax_from_gross(3, 100) * 100 == 200


def test_a_tax_rate_of_zero_or_no_rate_has_no_tax_amount(db_with_coa):
    db_with_coa.import_wiso_csv(csv_export(('6815', '1800', '119,00', 'NOTAX-1', ''),
                                           ('6815', '1800', '119,00', 'NOTAX-2', '100')))
    assert _tax_of(db_with_coa, 'NOTAX-1')[0] is None
    assert _tax_of(db_with_coa, 'NOTAX-2')[0] is None


def test_the_special_case_4405_to_4400_still_sets_19_percent_without_a_key(db_with_coa):
    db_with_coa.import_wiso_csv(csv_export(('4405', '4400', '119,00', 'SPECIAL-1', '')))
    tax, rate = _tax_of(db_with_coa, 'SPECIAL-1')
    assert tax == money.tax_from_gross(11900, 19) * 100 and rate == pytest.approx(0.19)


# ── Tabellen-Export ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('amount', ['nan', '1e5', 'inf', '1_000', '١٢,٣٤', '1.2.3,4'])
def test_the_table_export_refuses_an_invalid_amount_as_a_line_error(db_with_coa, amount):
    db_with_coa.insert_booking('2024-03-01', -1190.0, booking_type='entry', document_number='R2024-001')
    before = query(db_with_coa, 'SELECT ID, RecipientClient, Text FROM Bookings ORDER BY ID')

    result = db_with_coa.import_wiso_csv(
        (TABLE_HEAD + f'01.03.2024;Müller & Söhne GmbH;;Rechnung März;;R2024-001;{amount}\n').encode('utf-8'))

    assert result['format'] == 'table'
    assert result['errors'] == [f"Zeile 1: Ungültiger Betrag '{amount}'"]
    assert (result['updated'], result['skipped'], result['not_found']) == (0, 0, [])
    assert query(db_with_coa, 'SELECT ID, RecipientClient, Text FROM Bookings ORDER BY ID') == before


def test_the_table_export_still_matches_with_german_amounts(db_with_coa):
    db_with_coa.insert_booking('2024-03-01', -1190.0, booking_type='entry', document_number='R2024-001')

    result = db_with_coa.import_wiso_csv(
        (TABLE_HEAD + '01.03.2024;Müller & Söhne GmbH;;Rechnung März;;R2024-001;-1.190,00\n').encode('utf-8'))

    assert (result['errors'], result['updated'], result['not_found']) == ([], 1, [])


def test_the_amount_of_an_unmatched_table_row_is_a_number_in_the_result(db_with_coa):
    result = db_with_coa.import_wiso_csv(
        (TABLE_HEAD + '01.03.2024;Niemand;;Zweck;;X-1;-12,50\n').encode('utf-8'))

    (row,) = result['not_found']
    assert row['betrag'] == -12.5 and isinstance(row['betrag'], float)
    json.dumps(result)


class _FakeRequest:
    """Minimaler Ersatz für den BaseHTTPRequestHandler (Multipart-Upload)."""

    def __init__(self, content_type, body):
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(body))}
        self.rfile = io.BytesIO(body)


def _upload(db, csv_bytes):
    boundary = b'GRENZE0815'
    body = (b'--' + boundary + b'\r\nContent-Disposition: form-data; name="csvfile"; filename="export.csv"'
            b'\r\nContent-Type: text/csv\r\n\r\n' + csv_bytes + b'\r\n--' + boundary + b'--\r\n')
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    return handle_wiso_import(_FakeRequest('multipart/form-data; boundary=GRENZE0815', body), db)


def _stored_result():
    with open(os.path.join(userctx.user_data_dir(), 'wiso_import_result.json'), encoding='utf-8') as handle:
        return json.load(handle)


def test_the_upload_with_invalid_amounts_ends_on_the_page_without_an_error(db_with_coa):
    """Der Weg über den Upload (``handle_wiso_import``) leitet auf die Seite zurück – eine Datei mit ``nan`` ist ein
    Eintrag in den Fehlern, nie ein 500."""
    status, location = _upload(db_with_coa, csv_export(('6815', '1800', 'nan', 'BAD-1', '401'),
                                                       ('6815', '1800', '119,00', 'OK-1', '401')))

    assert status == 303 and location.startswith('/miscellaneous?wiso_import=ok')
    assert 'imported=1' in location and 'err_count=1' in location
    assert _stored_result()['errors'] == ["Zeile 1: Ungültiger Betrag 'nan'"]


def test_the_stored_result_keeps_the_amounts_of_skipped_rows(db_with_coa):
    """``json.dump`` scheiterte an einem ``Decimal`` – und ``handle_wiso_import`` schluckt das (Ergebnisdatei nicht
    kritisch): Ohne Datei zeigte die Seite keine übersprungenen Zeilen mehr."""
    data = csv_export(('6815', '1800', '119,00', 'DUP-1', '401'))
    _upload(db_with_coa, data)

    _upload(db_with_coa, data)

    (row,) = _stored_result()['skipped_rows']
    assert row['betrag'] == -119.0 and row['ref'] == 'DUP-1'


# ── Kontoauszüge (VBR-Text, DKB-Seite) ────────────────────────────────────────

VBR_OK = '01.12. 01.12. Lastschrift PN:931 1.142,18 S\nMuster AG\nRechnung\n'


def test_vbr_reads_a_valid_amount_as_before():
    (row,) = parse_text(VBR_OK, 2024)
    assert row['amount'] == -1142.18 and isinstance(row['amount'], float)
    (row,) = parse_text('01.12. 01.12. Gutschrift 17,35 H\nMuster AG\n', 2024)
    assert row['amount'] == 17.35


@pytest.mark.parametrize('amount', ['1.2.3,4', '1,2,3', ',', '.,', '1,', ',5', '12.34,56',
                                    '1.142', '1142', '12.34'])       # ohne Komma: Ein Auszug schreibt so nie, nichts wird geraten
def test_vbr_treats_an_unreadable_amount_like_a_head_without_an_amount(amount):
    """Vorher: ``float('.')`` & Co. warfen einen ValueError, und der Import verlor den ganzen Auszug
    (``parse_error``). Jetzt: Befund ``unparsed``, die Zeile fällt weg, die übrigen werden gelesen."""
    text = (f'01.12. 01.12. Lastschrift PN:931 {amount} S\nMuster AG\nRechnung\n'
            + '02.12. 02.12. Lastschrift PN:932 10,00 S\nBeispiel GmbH\nRechnung\n')
    problems = []

    rows = parse_text(text, 2024, problems=problems)

    assert [r['date'] for r in rows] == ['2024-12-02'] and rows[0]['amount'] == -10.0
    assert problems == ['unparsed']


class _Page:
    """Eine DKB-Seite mit fertigem Text und Wortpositionen (nur das, was ``_parse_dkb_page`` liest)."""

    def __init__(self, text, words):
        self._text, self._words = text, words

    def extract_text(self):
        return self._text

    def extract_words(self):
        return self._words


def _dkb_page(lines):
    """Text und Wörter für ``lines``: (Datum, Betrag als Text, x-Lage des Betrags)."""
    text = 'Kontoauszug Nummer 004 / 2016\n'
    words = []
    for number, (day, amount, x0) in enumerate(lines):
        top = 200 + 20 * number
        text += f'{day} {day} Lastschrift {amount}\nMuster AG\n'
        words += [{'text': day, 'x0': 30, 'top': top}, {'text': amount, 'x0': x0, 'top': top}]
    return _Page(text, words)


def test_dkb_reads_a_valid_amount_as_before():
    parser = DocumentParser.__new__(DocumentParser)
    rows, _ = parser._parse_dkb_page(_dkb_page([('04.01.', '1.234,56', 560), ('05.01.', '7,50', 400)]), 2016, 2016)

    assert [r['amount'] for r in rows] == [1234.56, -7.5]          # Gutschrift-Spalte x0 ≥ 500, sonst Belastung
    assert all(isinstance(r['amount'], float) for r in rows)


def test_dkb_amount_is_the_strict_german_form_only():
    """Der Betrag am Zeilenende muss ``1.234,56`` sein; alles andere ist kein Betrag und fällt auf den Weg
    „ohne Betrag“ (Fortsetzung der Vorseite, später übersprungen)."""
    parser = DocumentParser.__new__(DocumentParser)
    page = _dkb_page([('04.01.', '12,5', 400), ('05.01.', '12,345', 400), ('06.01.', '3,00', 400)])

    rows, continuation = parser._parse_dkb_page(page, 2016, 2016)

    assert [r['amount'] for r in rows] == [-3.0]
    assert continuation                                              # die Zeilen ohne Betrag kamen nie als Buchung


def test_every_amount_of_the_expected_pattern_is_accepted_by_parse_amount():
    """Die Muster der Importer (VBR: ``[\\d.,]+``, DKB: ``\\d{1,3}(\\.\\d{3})*,\\d{2}``) liefern, was ``parse_amount``
    liest – bis auf das, was es ablehnen soll."""
    for text in ('0,01', '999,99', '1.000,00', '12.345.678,90'):
        assert money.parse_amount(text) == Decimal(text.replace('.', '').replace(',', '.'))
