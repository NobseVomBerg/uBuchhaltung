# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Textimport der WISO-Exporte (``Database.import_wiso_csv``): Kodierung, Formaterkennung,
Steuerschlüssel und die Hinweise auf der Seite *Verschiedenes*.

* Kodierung: BOM → strenges UTF-8 → cp1252 → latin-1. cp1252 darf nie zuerst stehen, denn es
  dekodiert fast jede Bytefolge: Eine UTF-8-Datei wurde so zu Mojibake, ein BOM zum Zeichenpräfix im
  ersten Spaltennamen.
* Die Fehlermeldungen zu einer unbekannten Datei nennen keinen Wert aus der Datei; ein DATEV-Stapel
  (erstes Feld ``EXTF``/``DTVF``) wird erkannt und mit Klartext abgelehnt.
* ``unknown_tax_keys``: ein Steuerschlüssel, den ``TaxKeys`` nicht kennt, wird nicht geraten – die
  Zeile kommt ohne Steuersatz und wird gezählt; die Seite nennt sie.
* Die Fußzeile des Tabellen-Exports zählt in keinem Zähler.

Alle Namen und Werte sind erfunden.
"""
import io
import json
import os

import pytest

import userctx
from db.wiso_import import MAX_TAX_KEY_LENGTH, MAX_UNKNOWN_TAX_KEYS
from server.handlers import handle_wiso_import
from server.pages_miscellaneous import PageMiscellaneous, _import_notices
from tests.entry_form_helpers import csv_export, query

ORIGINAL_HEAD = 'ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n'
TEXT_WITH_UMLAUTS = 'Büromöbel Größe 5 €'
ORIGINAL = ORIGINAL_HEAD + f'1;08.06.2024;6815;1800;{TEXT_WITH_UMLAUTS};ENC-001;119,00;401;\n'

TABLE_HEAD = ('Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;'
              'opt. Beleg Nr.;Betrag\n')
TABLE_ROW = '01.03.2024;Müller & Söhne GmbH;DE89 3704 0044 0532 0130 00;Rechnung März;;R2024-001;-1190,00\n'

DATEV_MESSAGE = ('Das ist ein DATEV-Buchungsstapel. Er lässt sich hier nicht importieren – bitte den '
                 'Fibu-Export (Bewegungsdaten) oder die WISO-Datenbank verwenden.')
# Vorlauf eines Buchungsstapels: Berater- (70000) und Mandantennummer (99999) stehen im ersten Satz
DATEV_HEAD = ('"EXTF";700;21;"Buchungsstapel";12;20260102030405000;;"RE";"";"";70000;99999;20260101;4;'
              '20260101;20261231;"Test";"";1;0;0;"EUR";;"";;;"03";;;"";""\n')
DATEV_BODY = ('Umsatz (ohne Soll/Haben-Kz);Soll/Haben-Kennzeichen;WKZ Umsatz;Konto;'
              'Gegenkonto (ohne BU-Schlüssel);BU-Schlüssel;Belegdatum;Belegfeld 1;Buchungstext\n'
              '119,00;"S";"EUR";6815;1800;9;0201;"B-1";"Posten"\n')


def _booking_count(db):
    return query(db, 'SELECT COUNT(*) FROM Bookings')[0][0]


def _stored(db, document_number, columns='Text'):
    return query(db, f'SELECT {columns} FROM Bookings WHERE DocumentNumber = ?', (document_number,))


# ── Kodierung ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('encoding', ['cp1252', 'utf-8', 'utf-8-sig'])
def test_original_export_is_read_in_its_own_encoding(db_with_coa, encoding):
    """Eine cp1252-Datei mit Umlauten (ein einzelnes 0xE4 ist kein gültiges UTF-8) wird wie bisher gelesen,
    eine UTF-8-Datei – mit und ohne BOM – nicht mehr als cp1252."""
    result = db_with_coa.import_wiso_csv(ORIGINAL.encode(encoding))

    assert (result['format'], result['errors'], result['imported']) == ('original', [], 1)
    assert _stored(db_with_coa, 'ENC-001') == [(TEXT_WITH_UMLAUTS,)]          # kein Mojibake


def test_a_file_that_is_neither_utf8_nor_cp1252_is_read_as_latin1(db_with_coa):
    """0x81 gibt es in cp1252 nicht: Der Rückfall ist latin-1, die Datei geht trotzdem durch."""
    data = (ORIGINAL_HEAD + '1;08.06.2024;6815;1800;Text\x81Ende;LAT-001;119,00;401;\n').encode('latin-1')

    result = db_with_coa.import_wiso_csv(data)

    assert (result['format'], result['errors'], result['imported']) == ('original', [], 1)
    assert _stored(db_with_coa, 'LAT-001') == [('Text\x81Ende',)]


def test_a_file_with_bom_that_is_not_valid_utf8_is_refused(db_with_coa):
    """Mit BOM gilt strikt UTF-8: Eine beschädigte Datei wird abgelehnt, nicht als cp1252 geraten
    (das BOM stünde sonst als „ï»¿“ im ersten Spaltennamen)."""
    before = _booking_count(db_with_coa)

    result = db_with_coa.import_wiso_csv(b'\xef\xbb\xbf' + ORIGINAL.encode('cp1252'))

    assert result['format'] == 'unknown'
    assert result['errors'] == ['Encoding der Datei nicht erkennbar']
    assert result['imported'] == 0 and _booking_count(db_with_coa) == before


@pytest.mark.parametrize('encoding', ['cp1252', 'utf-8', 'utf-8-sig'])
def test_table_export_is_recognised_in_every_encoding(db_with_coa, encoding):
    """Auch mit BOM: Der erste Spaltenname trägt kein Zeichenpräfix, die Zeile wird also gefunden
    (mit „ï»¿Buchungsdatum“ als Spaltenname bliebe jede Zeile unbeachtet)."""
    db_with_coa.insert_booking('2024-03-01', -1190.0, booking_type='entry', document_number='R2024-001')

    result = db_with_coa.import_wiso_csv((TABLE_HEAD + TABLE_ROW).encode(encoding))

    assert result['format'] == 'table'
    assert (result['errors'], result['not_found'], result['updated'], result['skipped']) == ([], [], 1, 0)
    assert _stored(db_with_coa, 'R2024-001', 'RecipientClient, ForeignBankAccount, Text') == [
        ('Müller & Söhne GmbH', 'DE89 3704 0044 0532 0130 00', 'Rechnung März')]


# ── Unbekanntes Format, DATEV-Stapel ─────────────────────────────────────────

def test_unknown_format_names_no_value_of_the_file(db_with_coa):
    data = 'FELDEINS;"GEHEIM123";FELDDREI\nfoo;bar;baz\n'.encode('utf-8')

    result = db_with_coa.import_wiso_csv(data)

    assert result['format'] == 'unknown'
    assert (result['imported'], result['updated'], result['skipped']) == (0, 0, 0)
    (message,) = result['errors']
    for value in ('FELDEINS', 'GEHEIM123', 'FELDDREI'):
        assert value not in message
    assert message == ('Unbekanntes Format: In der ersten Zeile fehlen die Spalten des WISO-Exports '
                       '(KONTO und GEGENKONTO bzw. Empf./Auft. und Verwendungszweck). '
                       'Spalten in der ersten Zeile: 3.')


@pytest.mark.parametrize('head, line_end', [
    (DATEV_HEAD, '\n'),
    (DATEV_HEAD, '\r\n'),                                               # so schreibt DATEV
    (DATEV_HEAD.replace('"EXTF"', '"DTVF"'), '\r\n'),
    (DATEV_HEAD.replace('"EXTF"', 'extf'), '\n'),                       # ohne Anführungszeichen, klein
    (DATEV_HEAD.replace('"EXTF"', '" Extf "'), '\n'),                   # Leerraum im Feld
])
def test_a_datev_batch_is_named_and_refused_without_its_header_values(db_with_coa, head, line_end):
    data = (head + DATEV_BODY).replace('\r\n', '\n').replace('\n', line_end).encode('cp1252')
    before = _booking_count(db_with_coa)

    result = db_with_coa.import_wiso_csv(data)

    assert result['format'] == 'datev'
    (message,) = result['errors']
    assert message == DATEV_MESSAGE
    assert '70000' not in message and '99999' not in message
    assert (result['imported'], result['updated'], result['skipped']) == (0, 0, 0)
    assert _booking_count(db_with_coa) == before


def test_only_the_whole_first_field_marks_a_datev_batch(db_with_coa):
    result = db_with_coa.import_wiso_csv(b'EXTFX;700;21\nfoo;bar;baz\n')
    assert result['format'] == 'unknown'
    assert 'DATEV' not in result['errors'][0]


# ── Unbekannte Steuerschlüssel ───────────────────────────────────────────────

def test_unknown_tax_keys_are_counted_and_their_rows_imported_without_a_rate(db_with_coa):
    """181 und 250 stehen nicht in TaxKeys (100 seit 2.3.0 schon: Umsatzsteuer 0 %); 400 steht dort, hat aber keinen
    Satz – bekannt. Leer und 0 sind kein Schlüssel."""
    result = db_with_coa.import_wiso_csv(csv_export(
        ('6815', '1800', '119,00', 'K1', '181'),
        ('6815', '1800', '59,50', 'K2', '181'),
        ('6815', '1800', '23,80', 'K3', '250'),
        ('6815', '1800', '11,90', 'K4', '401'),       # bekannt: 19 %
        ('6815', '1800', '5,95', 'K5', '400'),        # bekannt, ohne Satz
        ('6815', '1800', '3,57', 'K6', '0'),
        ('6815', '1800', '2,38', 'K7', ''),
    ))

    assert result['errors'] == []
    assert result['imported'] == 7
    assert result['unknown_tax_keys'] == [{'schluessel': '181', 'anzahl': 2},
                                          {'schluessel': '250', 'anzahl': 1}]
    rates = dict(query(db_with_coa, "SELECT DocumentNumber, TaxRate FROM Bookings WHERE BookingType = 'entry'"))
    assert [rates[ref] for ref in ('K1', 'K2', 'K3', 'K5', 'K6', 'K7')] == [None] * 6
    assert rates['K4'] == pytest.approx(0.19)
    assert query(db_with_coa, "SELECT TaxAmount FROM Bookings WHERE DocumentNumber = 'K1'") == [(None,)]


def test_the_transfer_to_the_revenue_account_gets_its_rate_without_a_key_and_is_not_reported(db_with_coa):
    """„4405 an 4400“ bekommt 19 %, wenn die Zeile keinen Schlüssel trägt (leer oder 0) – dann stimmt „ohne Steuersatz
    importiert“ nicht, und die Zeile zählt nicht. Mit einem Schlüssel, den die App nicht kennt, gilt das nicht mehr (seit 2.3.0):
    Er wird nicht geraten, die Zeile kommt wie jede andere ohne Satz und zählt."""
    result = db_with_coa.import_wiso_csv(csv_export(
        ('4405', '4400', '119,00', 'U1', ''),
        ('4405', '4400', '59,50', 'U3', '0'),
        ('4405', '4400', '29,75', 'U4', '181'),
        ('6815', '1800', '14,88', 'U2', '181'),
    ))

    assert result['errors'] == []
    assert result['imported'] == 4
    assert result['unknown_tax_keys'] == [{'schluessel': '181', 'anzahl': 2}]
    rates = dict(query(db_with_coa, "SELECT DocumentNumber, TaxRate FROM Bookings WHERE BookingType = 'entry'"))
    assert rates['U1'] == pytest.approx(0.19) and rates['U3'] == pytest.approx(0.19)
    assert rates['U4'] is None and rates['U2'] is None


def test_without_an_unknown_tax_key_the_list_is_empty(db_with_coa):
    result = db_with_coa.import_wiso_csv(csv_export(
        ('6815', '1800', '119,00', 'L1', '401'), ('6815', '1800', '59,50', 'L2', ''),
        ('6815', '1800', '29,75', 'L3', '400')))
    assert result['imported'] == 3
    assert result['unknown_tax_keys'] == []


def test_only_rows_that_are_imported_are_counted(db_with_coa):
    """Ein zweiter Lauf legt nichts an (Duplikate) und zählt deshalb auch keinen Schlüssel."""
    lines = csv_export(('6815', '1800', '119,00', 'D1', '181'), ('6815', '1800', '59,50', 'D2', '250'))
    first = db_with_coa.import_wiso_csv(lines)
    again = db_with_coa.import_wiso_csv(lines)

    assert len(first['unknown_tax_keys']) == 2
    assert (again['imported'], again['skipped']) == (0, 2)
    assert again['unknown_tax_keys'] == []


def test_a_long_tax_key_is_cut_and_keys_with_the_same_start_are_counted_together(db_with_coa):
    assert MAX_TAX_KEY_LENGTH == 20
    result = db_with_coa.import_wiso_csv(csv_export(
        ('6815', '1800', '1,00', 'T1', '7' * 20 + 'aaaaa'),
        ('6815', '1800', '2,00', 'T2', '7' * 20 + 'bbbbb')))

    assert result['unknown_tax_keys'] == [{'schluessel': '7' * 20, 'anzahl': 2}]


def test_the_list_of_unknown_tax_keys_is_limited_and_sorted(db_with_coa):
    lines = [('6815', '1800', '1,00', f'C{n:03d}', str(2059 - n)) for n in range(MAX_UNKNOWN_TAX_KEYS + 10)]
    result = db_with_coa.import_wiso_csv(csv_export(*lines))

    keys = [entry['schluessel'] for entry in result['unknown_tax_keys']]
    assert result['imported'] == MAX_UNKNOWN_TAX_KEYS + 10
    assert len(keys) == MAX_UNKNOWN_TAX_KEYS
    assert keys == sorted(keys) and keys[0] == '2000'                      # die ersten nach Schlüssel
    assert all(entry['anzahl'] == 1 for entry in result['unknown_tax_keys'])


def test_the_result_with_unknown_tax_keys_can_be_stored_as_json(db_with_coa):
    result = db_with_coa.import_wiso_csv(csv_export(('6815', '1800', '119,00', 'J1', '181')))
    assert json.loads(json.dumps(result))['unknown_tax_keys'] == [{'schluessel': '181', 'anzahl': 1}]


# ── Fußzeile des Tabellen-Exports ────────────────────────────────────────────

@pytest.mark.parametrize('footer', ['12;;;;;;\n', '12\n', '"12";"";"";"";"";"";""\n'])
def test_the_footer_of_the_table_export_counts_nowhere(db_with_coa, footer):
    db_with_coa.insert_booking('2024-03-01', -1190.0, booking_type='entry', document_number='R2024-001')

    result = db_with_coa.import_wiso_csv((TABLE_HEAD + TABLE_ROW + footer).encode('cp1252'))

    assert result['format'] == 'table'
    assert (result['updated'], result['skipped'], result['imported']) == (1, 0, 0)
    assert (result['errors'], result['not_found']) == ([], [])


def test_a_table_export_with_only_the_footer_changes_nothing_and_counts_nothing(db_with_coa):
    result = db_with_coa.import_wiso_csv((TABLE_HEAD + '3;;;;;;\n').encode('cp1252'))

    assert result['format'] == 'table'
    assert (result['updated'], result['skipped'], result['imported']) == (0, 0, 0)
    assert (result['errors'], result['not_found']) == ([], [])


# ── Hinweise auf der Seite „Verschiedenes“ ───────────────────────────────────

def test_negative_gross_notice_names_the_pattern_wiso_writes():
    html = _import_notices({'negative_gross': 2})

    assert 'Negative Bruttobeträge &ndash; 2 Sätze' in html
    assert 'WISO schreibt Bruttobeträge üblicherweise nicht negativ' in html
    assert 'Stornos und Erstattungen stehen dort mit vertauschten Konten' in html
    assert 'Diese Sätze fallen aus dem Muster (bearbeitete Datei, andere Programmversion)' in html
    assert 'als normale Buchung gelesen werden' not in html


def test_unknown_tax_keys_notice_shows_the_sum_and_the_table():
    html = _import_notices({'unknown_tax_keys': [{'schluessel': '100', 'anzahl': 1},
                                                 {'schluessel': '181', 'anzahl': 2}]})

    assert 'Unbekannte Steuerschlüssel &ndash; 3 Zeilen</h3>' in html
    assert ('Diese Zeilen tragen einen Steuerschlüssel, den die App nicht kennt. Sie wurden ohne '
            'Steuersatz importiert &ndash; bitte prüfen, ob dort Steuer zu buchen ist.') in html
    assert '<tr><th>Schlüssel</th><th>Zeilen</th></tr>' in html
    assert '<tr><td>100</td><td>1</td></tr>' in html
    assert '<tr><td>181</td><td>2</td></tr>' in html


def test_unknown_tax_keys_notice_masks_every_value():
    html = _import_notices({'unknown_tax_keys': [
        {'schluessel': '<script>alert(1)</script>', 'anzahl': 1},
        {'schluessel': '"><img src=x onerror=alert(2)>', 'anzahl': 2}]})

    assert '<script>' not in html and '<img' not in html and 'onerror=alert(2)>' not in html
    assert '<td>&lt;script&gt;alert(1)&lt;/script&gt;</td>' in html
    assert '<td>&quot;&gt;&lt;img src=x onerror=alert(2)&gt;</td>' in html


def test_unknown_tax_keys_notice_survives_a_hand_edited_result_file():
    """Nur Einträge, die ein Objekt sind; eine Zahl, die keine ganze Zahl ab 1 ist, gilt als 0."""
    html = _import_notices({'unknown_tax_keys': [
        {'schluessel': '100', 'anzahl': 2}, {'schluessel': '181', 'anzahl': '9'},
        {'schluessel': '182', 'anzahl': True}, {'schluessel': '183', 'anzahl': -4}, {'schluessel': '184'},
        'x', None, 7, ['100', 3]]})

    assert 'Unbekannte Steuerschlüssel &ndash; 2 Zeilen</h3>' in html
    assert html.count('<tr><td>') == 5
    assert '<tr><td>100</td><td>2</td></tr>' in html
    for key in ('181', '182', '183', '184'):
        assert f'<tr><td>{key}</td><td>0</td></tr>' in html


@pytest.mark.parametrize('value', [[], None, 'x', 5, {'schluessel': '100', 'anzahl': 1},
                                   [1, None, 'x', ['100']], True])
def test_no_unknown_tax_keys_notice_without_a_valid_entry(value):
    assert 'Steuerschl' not in _import_notices({'unknown_tax_keys': value})
    assert 'Steuerschl' not in _import_notices({})


class _FakeRequest:
    """Minimaler Ersatz für den BaseHTTPRequestHandler (Multipart-Upload)."""
    def __init__(self, content_type, body):
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(body))}
        self.rfile = io.BytesIO(body)


def _upload(db, csv_bytes, filename='export.csv'):
    boundary = b'GRENZE0815'
    body = (b'--' + boundary + b'\r\n'
            + f'Content-Disposition: form-data; name="csvfile"; filename="{filename}"'.encode()
            + b'\r\nContent-Type: text/csv\r\n\r\n' + csv_bytes + b'\r\n--' + boundary + b'--\r\n')
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    return handle_wiso_import(_FakeRequest('multipart/form-data; boundary=GRENZE0815', body), db)


def test_unknown_tax_keys_reach_the_page_through_the_upload_and_the_stored_result(db_with_coa):
    """Der ganze Weg: Upload → Import → Ergebnisdatei (unverändert abgelegt) → Seite *Verschiedenes*."""
    status, location = _upload(db_with_coa, csv_export(('6815', '1800', '119,00', 'P1', '181'),
                                                       ('6815', '1800', '59,50', 'P2', '181'),
                                                       ('6815', '1800', '29,75', 'P3', '401')))
    assert status == 303 and 'wiso_import=ok' in location and 'imported=3' in location

    with open(os.path.join(userctx.user_data_dir(), 'wiso_import_result.json'), encoding='utf-8') as handle:
        assert json.load(handle)['unknown_tax_keys'] == [{'schluessel': '181', 'anzahl': 2}]
    html = PageMiscellaneous(db_with_coa)
    assert 'Unbekannte Steuerschlüssel &ndash; 2 Zeilen</h3>' in html
    assert '<tr><td>181</td><td>2</td></tr>' in html


def test_a_datev_batch_through_the_upload_shows_the_message_and_no_header_value(db_with_coa):
    status, location = _upload(db_with_coa, (DATEV_HEAD + DATEV_BODY).encode('cp1252'))
    assert status == 303 and 'wiso_import=ok' in location and 'err_count=1' in location

    html = PageMiscellaneous(db_with_coa)
    assert 'Das ist ein DATEV-Buchungsstapel. Er lässt sich hier nicht importieren' in html
    assert '70000' not in html and '99999' not in html
