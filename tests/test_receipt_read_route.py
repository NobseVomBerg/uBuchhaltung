# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belegerkennung, Paket P4 (2.6.0): Lesen auslösen und anzeigen – ``POST /receipts/read``, die Spalte „Gelesen“ der Belegliste, der
Kasten über der Liste und der Kasten „Gelesen“ auf der Seite eines Belegs.

* ``POST /receipts/read`` (JSON ``{"documentId"}``, ``_JSON_PATHS`` + ``_POST_ONLY_JSON_PATHS``) liest die Datei EINES Belegs und legt die
  Lesung in ``DocumentReadings`` ab; am Beleg selbst ändert sich nichts. Geprüft wie jede JSON-Route (GET 405, Herkunft, Token, ID
  streng) und: Demo-Modus 403, unbekannter Beleg 404, höchstens ein Lesen zurzeit (409 ``busy``), ein Beleg, der sich während des
  Lesens ändert, wird nicht beschrieben (409), ein unerwarteter Fehler ist ein fester Satz (500) – nie ein Pfad, der Name der Datei,
  ihr Text oder ein Ausnahmetext in der Antwort. Eine Datei über der Grenze wird nicht gelesen, ein Beleg ohne Datei bekommt eine
  Lesung ``failed`` mit ``no_file``.
* Der Upload liest nie selbst; seine Antwort trägt die IDs der angelegten Belege, die das Skript der Seite an ``readReceipts`` gibt.
* Seiten: Spalte „Gelesen“, der Kasten mit dem Knopf (nur mit ungelesenen Belegen, nicht im Demo-Modus, höchstens 100), der Kasten
  „Gelesen“ auf der Seite eines Belegs (Felder, Zitat bzw. BT-Bezeichner, Summen- und Satzprobe, Hinweise, „Gelesenes Datum übernehmen“) –
  jeder Wert maskiert, die Skripte nur mit ``var``/``function`` und ohne Markup aus Zeichenketten.

Die Lesung selbst (Kindprozess, Regeln) prüfen ``test_receipt_reading_*.py``; hier läuft der Kindprozess nur für je eine PNG-, CSV-, UBL-
und PDF-Datei, sonst steht ein Ersatz für ``isolated.read_isolated`` da. Alle Namen, Nummern und Beträge sind erfundene Testwerte;
Zeitpunkte sind feste Werte (``receipt_reader._now``, ``local_datetime``).
"""
import contextlib
import datetime
import html
import json
import os
import re
import sqlite3
import threading

import pytest

import receipt_reading
import userctx
from db import numbering
from receipt_reading import Reading, isolated
from server import app as server_app
from server import receipt_files, receipt_reader
from server.pages_receipts import READ_JS, READING_HINT, RECEIPT_READ_JS, PageReceiptEdit
from tests import installation
from tests.document_helpers import add_contacts, state
from tests.dom_helpers import Dom
from tests.http_client import FOREIGN_ORIGIN, get, own_origin, send
from tests.test_receipt_reading_einvoice import ubl
from tests.test_receipt_upload import created, demo_site, upload
from tests.test_statement_import_guards import make_pdf

DAY = '2026-03-04'
NOW = '2026-03-05 10:30:00'                     # ``receipt_reader._now``: UTC
LOCAL_NOW = datetime.datetime(2026, 3, 5, 11, 30)       # derselbe Zeitpunkt in Ortszeit (``local_datetime``)
SECRET_TEXT = 'GEHEIMER-TEXT-DER-DATEI Rechnung 4711'
SECRET_NAME = 'geheime-ablage.xml'
LONG_SENDER = 'Eine sehr lange Firmenbezeichnung GmbH & Co. KG'
HOSTILE_SENDER = '<script>alert(1)</script>'
HOSTILE_NUMBER = '"><img src=x onerror=alert(1)>'
HOSTILE_QUOTE = '"><img src=x>'
PNG = b'\x89PNG\r\n\x1a\n' + b'kein echtes Bild'
REAL_NOW = receipt_reader._now                  # vor dem Austausch durch ``fixed_clock``: nur die Funktion, die Uhr liest sie erst beim Aufruf
INVALID_REQUEST = {'success': False, 'error': 'Ungültige Anfrage.'}
READING_KEYS = {'status', 'statusLabel', 'sourceLabel', 'readAt', 'outdated', 'fields', 'checks', 'notes', 'docDate'}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """Zeitpunkte sind fest: der der Lesung (UTC), seine Ortszeit, der Tag des Uploads und das laufende Jahr des Nummernkreises."""
    monkeypatch.setattr(receipt_reader, '_now', lambda: NOW)
    monkeypatch.setattr(receipt_reader, 'local_datetime', lambda stamp: LOCAL_NOW)
    monkeypatch.setattr(receipt_files, '_today', lambda: DAY)
    monkeypatch.setattr(numbering, '_current_year', lambda: 2026)


# ── Hilfen ───────────────────────────────────────────────────────────────────
def user_dir(user=installation.ADMIN):
    return os.path.join(userctx.DATA_ROOT, 'users', user)


def add_file_receipt(db, name, content, number='26B001', day=DAY, user=installation.ADMIN):
    """Ein Beleg mit Datei, wie der Upload ihn ablegt: ``Belege/<Jahr>/<Nummer>_<Name>``, ``Path`` relativ. Liefert die ID."""
    folder = os.path.join(user_dir(user), 'Belege', day[:4])
    os.makedirs(folder, exist_ok=True)
    stored = f'{number}_{name}'
    with open(os.path.join(folder, stored), 'wb') as handle:
        handle.write(content)
    db.insert_receipt(number, day, name, f'Belege/{day[:4]}/{stored}', '')
    return db.fetch_receipts()[-1][0]


def add_receipt(db, number, path, day=DAY):
    """Ein Beleg mit einem gespeicherten Pfad, zu dem es keine Datei geben muss; liefert die ID."""
    db.insert_receipt(number, day, 'beleg.pdf', path, '')
    return db.fetch_receipts()[-1][0]


def raw(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql, args).fetchall()


def run_sql(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute(sql, args)
        con.commit()


def reading_rows(db):
    return raw(db, 'SELECT Document_ID, Status FROM DocumentReadings ORDER BY ID')


def good_reading(**over):
    """Eine gelesene Rechnung (so, wie ein Leser sie liefert) – mit Zitat und BT-Bezeichner, Summen- und Satzprobe, einem Befund und einem Text."""
    fields = dict(
        status='ok', source='text', doc_date='2026-03-04', gross_minor=1190000, net_minor=1000000, tax_minor=190000, tax_rate=0.19,
        currency='EUR', sender_name='Muster GmbH', sender_vat_id='DE123456789', invoice_number='R-100', kind='invoice',
        raw={'fields': {'gross_minor': {'value': 1190000, 'source': 'rule', 'quote': 'Gesamtbetrag 119,00 EUR', 'page': 1},
                        'doc_date': {'value': '2026-03-04', 'source': 'einvoice', 'ref': 'BT-2'}},
             'checks': {'sum': 'ok', 'rate': 'ok'}},
        text=SECRET_TEXT, notes=['by_checksum'])
    fields.update(over)
    return Reading(**fields)


def stored_row(reading=None, **over):
    """Die Zeile einer Lesung, wie ``read_receipt`` sie schreibt (``Reading.to_row`` plus ``ReadAt``); ``over`` überschreibt Spalten."""
    return {**(reading or good_reading()).to_row(), 'ReadAt': NOW, **over}


def store_reading(db, receipt, reading=None, **over):
    """Die Lesung eines Belegs ablegen (``save_document_reading``)."""
    path = db.get_receipt_by_id(receipt)[4]
    assert db.save_document_reading(receipt, path, stored_row(reading, **over))


class FakeReader:
    """Ersatz für ``isolated.read_isolated``: merkt sich die Aufrufe (Name, Bytes, eigene Angaben) und liefert die nächste vorbereitete
    Lesung (``queue``), sonst ``good_reading()``. ``before_return`` läuft mitten im Lesen."""

    def __init__(self):
        self.calls, self.queue, self.before_return = [], [], None

    def __call__(self, name, data, own, timeout=None, command=None):
        self.calls.append((name, data, own))
        if self.before_return:
            self.before_return()
        return self.queue.pop(0) if self.queue else good_reading()


@pytest.fixture
def fake_reader(monkeypatch):
    fake = FakeReader()
    monkeypatch.setattr(isolated, 'read_isolated', fake)
    return fake


@pytest.fixture
def circle(auto_login):
    auto_login.db.insert_number_range('receipt_company', 2026, 'B', current_number=0)
    return auto_login


def post_read(port, login, payload, headers=None):
    """``POST /receipts/read`` wie die Seite: eigene Herkunft, Token als Header; ``payload``: ein Objekt oder fertiger Text."""
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    return send(port, 'POST', '/receipts/read', {'Origin': own_origin(port), 'Content-Type': 'application/json',
                                                  'X-CSRF-Token': login.csrf, **(headers or {})}, body)


def read(port, login, receipt):
    return post_read(port, login, {'documentId': receipt})


def answer(response):
    assert response.headers['Content-Type'] == 'application/json'
    return json.loads(response.text)


def cells_of(page):
    """Die Zellen jeder Zeile der Belegliste je ID des Belegs (Entitäten aufgelöst)."""
    found = {}
    for chunk in re.findall(r'<tr class="receipt-row"(.*?)</tr>', page, re.S):
        receipt_id = int(re.search(r'data-id="(\d+)"', chunk).group(1))
        found[receipt_id] = [html.unescape(cell) for cell in re.findall(r'<td(?: [^>]*)?>(.*?)</td>', chunk, re.S)]
    return found


# ── Die Route: Methode, Herkunft, Token, ID ──────────────────────────────────
def test_the_route_is_a_post_only_json_route_and_not_in_the_diagnosis_server():
    assert '/receipts/read' in server_app._JSON_PATHS and '/receipts/read' in server_app._POST_ONLY_JSON_PATHS
    assert '/receipts/read' in server_app._JSON_ANSWER_ROUTES            # auch die 503 während einer Wiederherstellung ist JSON
    assert '/receipts/read' not in server_app._ACTION_PATHS
    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'mcp_ubuchhaltung.py')
    with open(tool, encoding='utf-8') as handle:
        assert '/receipts/read' not in handle.read()                       # die Positivliste des Diagnose-MCP bleibt, wie sie war


def test_get_is_405_and_reads_nothing(server_port, auto_login, fake_reader):
    receipt = add_file_receipt(auto_login.db, 'rechnung.xml', ubl())
    response = get(server_port, f'/receipts/read?documentId={receipt}')
    assert response.status == 405 and response.headers['Allow'] == 'POST'
    assert fake_reader.calls == [] and reading_rows(auto_login.db) == []


@pytest.mark.parametrize('headers, status, text', [
    pytest.param({'Origin': FOREIGN_ORIGIN}, 403, 'fremder Herkunft', id='fremder-Origin'),
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, 403, 'fremder Herkunft', id='cross-site'),
    pytest.param({'X-CSRF-Token': None}, 403, 'Sicherheits-Token', id='Token-fehlt'),
    pytest.param({'X-CSRF-Token': 'falsch'}, 403, 'Sicherheits-Token', id='Token-falsch'),
])
def test_a_request_from_a_foreign_page_or_without_a_token_reads_nothing(server_port, auto_login, fake_reader, headers, status, text):
    receipt = add_file_receipt(auto_login.db, 'rechnung.xml', ubl())
    sent = {'Origin': own_origin(server_port), 'Content-Type': 'application/json', 'X-CSRF-Token': auto_login.csrf, **headers}
    sent = {name: value for name, value in sent.items() if value is not None}
    response = send(server_port, 'POST', '/receipts/read', sent, json.dumps({'documentId': receipt}))
    assert response.status == status and text in response.text
    assert fake_reader.calls == [] and reading_rows(auto_login.db) == []


@pytest.mark.parametrize('payload', [
    pytest.param(b'{}', id='leeres-Objekt'),
    pytest.param(b'{"documentId": null}', id='null'),
    pytest.param(b'{"documentId": ""}', id='leer'),
    pytest.param(b'{"documentId": "01"}', id='fuehrende-Null'),
    pytest.param(b'{"documentId": " 1"}', id='Leerzeichen'),
    pytest.param(b'{"documentId": "abc"}', id='Text'),
    pytest.param(b'{"documentId": 1.5}', id='Bruch'),
    pytest.param(b'{"documentId": 1.0}', id='Gleitkomma'),
    pytest.param(b'{"documentId": true}', id='Wahrheitswert'),
    pytest.param(b'{"documentId": [1]}', id='Liste'),
    pytest.param(b'{"documentId": 0}', id='null-ist-keine-ID'),
    pytest.param(b'{"documentId": -1}', id='negativ'),
    pytest.param(b'{"documentId": 9223372036854775808}', id='ueber-64-Bit'),
    pytest.param(b'{"id": 1}', id='falscher-Schluessel'),
    pytest.param(b'[1]', id='Liste-statt-Objekt'),
    pytest.param(b'{', id='kaputtes-JSON'),
])
def test_an_unusable_id_is_400_before_anything_happens(server_port, auto_login, fake_reader, payload):
    add_file_receipt(auto_login.db, 'rechnung.xml', ubl())
    before = state(auto_login.db, 'Documents', 'DocumentReadings')
    response = post_read(server_port, auto_login, payload)
    assert (response.status, answer(response)) == (400, INVALID_REQUEST)
    assert fake_reader.calls == [] and state(auto_login.db, 'Documents', 'DocumentReadings') == before


def test_an_id_as_digits_in_normal_form_is_an_id_like_a_number(server_port, auto_login, fake_reader):
    """Wie bei den anderen Routen (``_json_positive_id``): ``"7"`` ist die ID 7, ``"07"`` keine."""
    response = post_read(server_port, auto_login, {'documentId': '7'})
    assert (response.status, answer(response)) == (404, {'success': False, 'error': receipt_reader.READ_NOT_FOUND})


def test_an_unknown_receipt_is_404_and_nothing_is_stored(server_port, auto_login, fake_reader):
    response = read(server_port, auto_login, 999)
    assert (response.status, answer(response)) == (404, {'success': False, 'error': 'Diesen Beleg gibt es nicht.'})
    assert fake_reader.calls == [] and reading_rows(auto_login.db) == []


# ── Der Demo-Modus ───────────────────────────────────────────────────────────
def test_in_demo_mode_nothing_is_read_and_nothing_is_stored(server_port, monkeypatch, fake_reader):
    csrf = demo_site(monkeypatch)
    db = installation.open_db('demo')
    receipt = add_file_receipt(db, 'rechnung.xml', ubl(), user='demo')
    before = installation.tree(userctx.DATA_ROOT)
    response = send(server_port, 'POST', '/receipts/read', {'Origin': own_origin(server_port), 'X-CSRF-Token': csrf,
                                                             'Content-Type': 'application/json'}, json.dumps({'documentId': receipt}))
    assert (response.status, answer(response)) == (403, {'success': False, 'error': receipt_reader.READ_DEMO_REFUSED})
    assert receipt_reader.READ_DEMO_REFUSED == 'Im Demo-Modus ist das Lesen von Belegen abgeschaltet.'
    assert fake_reader.calls == [] and reading_rows(db) == []
    assert installation.tree(userctx.DATA_ROOT) == before                  # keine Zeile, keine Datei, kein Ordner


# ── Lesen: der Weg und seine Antwort ─────────────────────────────────────────
def test_a_read_stores_one_reading_and_answers_with_its_short_form_and_changes_nothing_at_the_receipt(server_port, auto_login, fake_reader):
    db = auto_login.db
    add_contacts(db)                                                        # die eigene Firma: ihre Angaben gehen an den Leser
    content = ubl()
    receipt = add_file_receipt(db, SECRET_NAME, content, number='26B001')
    before = state(db, 'Documents')
    response = read(server_port, auto_login, receipt)
    assert response.status == 200
    body = answer(response)
    assert body['success'] is True and set(body['reading']) == READING_KEYS
    assert (body['reading']['status'], body['reading']['statusLabel'], body['reading']['sourceLabel']) == (
        'ok', 'gelesen', 'aus dem Text gelesen')
    assert body['reading']['readAt'] == '05.03.2026 11:30' and body['reading']['outdated'] is False
    assert [(f['label'], f['value']) for f in body['reading']['fields']] == [
        ('Belegdatum', '04.03.2026'), ('Brutto', '119,00 EUR'), ('Netto', '100,00 EUR'), ('Steuer', '19,00 EUR'),
        ('Steuersatz', '19 %'), ('Währung', 'EUR'), ('Absender', 'Muster GmbH'), ('USt-IdNr.', 'DE123456789'),
        ('Rechnungsnummer', 'R-100'), ('Art', 'Rechnung')]
    assert body['reading']['checks'] == ['Netto + Steuer = Brutto: stimmt', 'Steuer passt zum Satz: stimmt']          # Summenprobe, Satzprobe
    assert body['reading']['notes'] == [receipt_reader.NOTE_TEXTS['by_checksum']] and body['reading']['docDate'] == DAY
    # der Leser bekam den Namen auf der Platte, die Bytes der Datei und die Angaben der eigenen Firma
    (call,) = fake_reader.calls
    assert call[0] == '26B001_' + SECRET_NAME and call[1] == content and call[2] == db.own_identity()
    assert call[2][0] and call[2][1] == ['DE123456789']
    # gespeichert: eine Zeile mit allen Feldern, dem Zeitpunkt (UTC) und dem Text – am Beleg ändert sich nichts
    row = db.get_document_reading(receipt)
    assert (row['Status'], row['Source'], row['ReaderVersion'], row['ReadAt'], row['DocDate'], row['GrossMinor'], row['NetMinor'],
            row['TaxMinor'], row['TaxRate'], row['Currency'], row['SenderName'], row['SenderVatId'], row['InvoiceNumber'],
            row['Kind']) == ('ok', 'text', receipt_reading.READER_VERSION, NOW, DAY, 1190000, 1000000, 190000, 0.19, 'EUR',
                             'Muster GmbH', 'DE123456789', 'R-100', 'invoice')
    assert row['Text'] == SECRET_TEXT and json.loads(row['Raw'])['notes'] == ['by_checksum']
    assert state(db, 'Documents') == before
    with open(os.path.join(user_dir(), 'Belege', '2026', '26B001_' + SECRET_NAME), 'rb') as handle:
        assert handle.read() == content


def test_the_answer_never_names_a_path_the_file_name_the_text_or_an_error_text(server_port, auto_login, fake_reader):
    receipt = add_file_receipt(auto_login.db, SECRET_NAME, ubl())
    response = read(server_port, auto_login, receipt)
    for text in (SECRET_TEXT, 'GEHEIMER', SECRET_NAME, 'geheime-ablage', 'Belege', '26B001_', user_dir(), 'Traceback'):
        assert text not in response.text, text
    assert '"text"' not in response.text and '"Text"' not in response.text and '"path"' not in response.text.lower()


def test_a_second_read_replaces_the_reading_there_is_one_row_per_receipt(server_port, auto_login, fake_reader, monkeypatch):
    receipt = add_file_receipt(auto_login.db, 'rechnung.xml', ubl())
    fake_reader.queue = [good_reading(), Reading(status='empty', source='text', notes=['not_einvoice'])]
    assert read(server_port, auto_login, receipt).status == 200
    first = auto_login.db.get_document_reading(receipt)
    monkeypatch.setattr(receipt_reader, '_now', lambda: '2026-03-06 08:00:00')
    body = answer(read(server_port, auto_login, receipt))
    assert body['reading']['status'] == 'empty' and body['reading']['fields'] == []
    assert body['reading']['notes'] == [receipt_reader.NOTE_TEXTS['not_einvoice']]
    assert reading_rows(auto_login.db) == [(receipt, 'empty')]
    second = auto_login.db.get_document_reading(receipt)
    assert second['ID'] == first['ID'] and second['ReadAt'] == '2026-03-06 08:00:00'
    assert (second['GrossMinor'], second['SenderName'], second['DocDate'], second['Text']) == (None, None, None, None)   # nichts bleibt vom alten Stand


# ── Ein Beleg ohne Datei, zu große Dateien ───────────────────────────────────
@pytest.mark.parametrize('path', [
    pytest.param('', id='Papierbeleg'),
    pytest.param('Belege/2026/weg.pdf', id='Datei-fehlt'),
    pytest.param('/srv/alt/rechnung.pdf', id='ausserhalb-der-Beleg-Ordner'),
    pytest.param('Belege/../../buch.db', id='mit-..'),
])
def test_a_receipt_without_a_usable_file_gets_a_failed_reading_with_no_file_and_nothing_is_read(server_port, auto_login, fake_reader, path):
    receipt = add_receipt(auto_login.db, '26B001', path)
    response = read(server_port, auto_login, receipt)
    assert response.status == 200
    body = answer(response)['reading']
    assert (body['status'], body['statusLabel'], body['fields'], body['notes']) == (
        'failed', 'Lesen fehlgeschlagen', [], [receipt_reader.NOTE_TEXTS['no_file']])
    row = auto_login.db.get_document_reading(receipt)
    assert (row['Status'], row['Source'], row['ReaderVersion'], row['ReadAt'], row['Text']) == (
        'failed', None, receipt_reading.READER_VERSION, NOW, None)
    assert json.loads(row['Raw'])['notes'] == ['no_file']
    assert fake_reader.calls == []
    assert '/srv/alt' not in response.text and 'buch.db' not in response.text


def test_a_file_over_the_limit_is_too_large_and_is_not_read_the_limit_is_read_at_each_call(server_port, auto_login, fake_reader, monkeypatch):
    monkeypatch.setattr(receipt_reading, 'MAX_READ_BYTES', 10)
    big = add_file_receipt(auto_login.db, 'gross.xml', b'x' * 11, number='26B001')
    exact = add_file_receipt(auto_login.db, 'genau.xml', b'x' * 10, number='26B002')
    body = answer(read(server_port, auto_login, big))['reading']
    assert (body['status'], body['statusLabel']) == ('too_large', 'nicht gelesen (zu groß oder zu aufwendig)')
    assert fake_reader.calls == []                                          # die Datei wurde nicht gelesen
    assert auto_login.db.get_document_reading(big)['Status'] == 'too_large'
    assert answer(read(server_port, auto_login, exact))['reading']['status'] == 'ok'         # genau die Grenze geht
    assert [call[1] for call in fake_reader.calls] == [b'x' * 10]


def test_a_file_that_grew_after_it_was_opened_is_too_large_too(server_port, auto_login, fake_reader, monkeypatch):
    """Die Größe kommt vom geöffneten Dateiobjekt; wächst die Datei danach, liest der Server trotzdem höchstens die Grenze plus ein Byte."""
    monkeypatch.setattr(receipt_reading, 'MAX_READ_BYTES', 10)
    receipt = add_file_receipt(auto_login.db, 'waechst.xml', b'x' * 50)
    opened_for_real = receipt_files.open_receipt

    def reports_a_small_size(path, log=True):
        found = opened_for_real(path, log)
        return found._replace(size=3)

    monkeypatch.setattr(receipt_files, 'open_receipt', reports_a_small_size)
    assert answer(read(server_port, auto_login, receipt))['reading']['status'] == 'too_large'
    assert fake_reader.calls == []


# ── Die echten Dateiarten durch den Kindprozess ──────────────────────────────
def test_a_png_is_no_text_and_a_csv_is_unsupported(server_port, auto_login):
    png = add_file_receipt(auto_login.db, 'foto.png', PNG, number='26B001')
    csv = add_file_receipt(auto_login.db, 'liste.csv', b'a;b\n1;2\n', number='26B002')
    assert answer(read(server_port, auto_login, png))['reading']['statusLabel'] == 'nicht lesbar (kein Text – Scan oder Bild)'
    assert answer(read(server_port, auto_login, csv))['reading']['statusLabel'] == 'nicht lesbar (Dateityp)'
    assert reading_rows(auto_login.db) == [(png, 'no_text'), (csv, 'unsupported')]


def test_an_einvoice_in_ubl_is_read_through_the_child_process(server_port, auto_login):
    receipt = add_file_receipt(auto_login.db, 'rechnung.xml', ubl(number='R-4711', seller='Probe AG'))
    body = answer(read(server_port, auto_login, receipt))['reading']
    assert (body['status'], body['sourceLabel']) == ('ok', 'aus der E-Rechnung')
    values = {field['label']: field['value'] for field in body['fields']}
    assert (values['Brutto'], values['Absender'], values['Rechnungsnummer'], values['Belegdatum']) == (
        '119,00 EUR', 'Probe AG', 'R-4711', '04.03.2026')
    assert {field['label']: field['ref'] for field in body['fields'] if 'ref' in field}['Brutto'] == 'BT-112'
    row = auto_login.db.get_document_reading(receipt)
    assert (row['Source'], row['GrossMinor'], row['SenderName'], row['InvoiceNumber']) == ('einvoice_ubl', 1190000, 'Probe AG', 'R-4711')


def test_a_text_pdf_is_read_through_the_child_process(server_port, auto_login):
    lines = ['Muster GmbH', 'Musterstrasse 1, 12345 Musterstadt', 'Rechnung Nr. R-77', 'Rechnungsdatum: 04.03.2026',
             'Nettobetrag 100,00 EUR', 'MwSt 19 % 19,00 EUR', 'Gesamtbetrag 119,00 EUR']
    receipt = add_file_receipt(auto_login.db, 'rechnung.pdf', make_pdf([lines]))
    body = answer(read(server_port, auto_login, receipt))['reading']
    assert (body['status'], body['sourceLabel']) == ('ok', 'aus dem Text gelesen')
    brutto = next(field for field in body['fields'] if field['label'] == 'Brutto')
    assert brutto['value'] == '119,00 EUR' and brutto['quote'] == 'Gesamtbetrag 119,00 EUR'             # die Zeile, aus der es stammt
    row = auto_login.db.get_document_reading(receipt)
    assert (row['Source'], row['GrossMinor'], row['DocDate'], row['InvoiceNumber']) == ('text', 1190000, DAY, 'R-77')
    assert 'Gesamtbetrag' in row['Text']


# ── Höchstens ein Lesen zurzeit ──────────────────────────────────────────────
def test_while_a_read_is_running_a_second_one_is_busy_and_stores_nothing(server_port, auto_login, fake_reader):
    first = add_file_receipt(auto_login.db, 'a.xml', ubl(), number='26B001')
    second = add_file_receipt(auto_login.db, 'b.xml', ubl(), number='26B002')
    started, release, results = threading.Event(), threading.Event(), []
    fake_reader.before_return = lambda: (started.set(), release.wait(10))
    worker = threading.Thread(target=lambda: results.append(read(server_port, auto_login, first)))
    worker.start()
    try:
        assert started.wait(10)
        busy = read(server_port, auto_login, second)
        assert (busy.status, answer(busy)) == (409, {'success': False, 'busy': True, 'error': receipt_reader.READ_BUSY})
        assert receipt_reader.READ_BUSY == 'Es wird gerade ein anderer Beleg gelesen – bitte gleich noch einmal.'
    finally:
        release.set()
        worker.join(10)
    assert results[0].status == 200 and reading_rows(auto_login.db) == [(first, 'ok')]       # nur das erste Lesen hat gespeichert
    fake_reader.before_return = None
    assert read(server_port, auto_login, second).status == 200                                 # danach ist die Sperre frei
    assert reading_rows(auto_login.db) == [(first, 'ok'), (second, 'ok')]


def test_a_held_lock_is_answered_before_anything_is_looked_at(server_port, auto_login, fake_reader):
    receipt = add_file_receipt(auto_login.db, 'a.xml', ubl())
    with receipt_reader._read_lock:
        response = read(server_port, auto_login, receipt)
        unknown = read(server_port, auto_login, 999)
    assert answer(response)['busy'] is True and answer(unknown)['busy'] is True             # auch ohne Beleg: zuerst die Sperre
    assert fake_reader.calls == [] and reading_rows(auto_login.db) == []


# ── Der Beleg ändert sich während des Lesens ─────────────────────────────────
def test_a_receipt_deleted_while_it_is_read_is_409_and_nothing_is_stored(server_port, auto_login, fake_reader):
    receipt = add_file_receipt(auto_login.db, 'rechnung.xml', ubl())
    fake_reader.before_return = lambda: auto_login.db.delete_receipt(receipt)
    response = read(server_port, auto_login, receipt)
    assert (response.status, answer(response)) == (409, {'success': False, 'error': receipt_reader.READ_CHANGED})
    assert reading_rows(auto_login.db) == [] and auto_login.db.get_receipt_by_id(receipt) is None


def test_a_receipt_whose_path_changed_while_it_is_read_is_409_and_nothing_is_stored(server_port, auto_login, fake_reader):
    receipt = add_file_receipt(auto_login.db, 'rechnung.xml', ubl())
    fake_reader.before_return = lambda: run_sql(auto_login.db, 'UPDATE Documents SET Path = ? WHERE ID = ?',
                                                'Belege/2026/anderer-name.xml', receipt)
    response = read(server_port, auto_login, receipt)
    assert (response.status, answer(response)) == (409, {'success': False, 'error': receipt_reader.READ_CHANGED})
    assert reading_rows(auto_login.db) == []
    assert auto_login.db.get_receipt_by_id(receipt)[4] == 'Belege/2026/anderer-name.xml'     # der Beleg bleibt, wie er jetzt ist


def test_the_read_runs_outside_any_transaction_of_the_database(server_port, auto_login, fake_reader):
    """Während das Lesen läuft (bis zu 15 s), hält der Server keine Schreibsperre: eine zweite Verbindung bekommt sie sofort."""
    receipt = add_file_receipt(auto_login.db, 'rechnung.xml', ubl())
    seen = []

    def take_the_write_lock():
        with contextlib.closing(sqlite3.connect(auto_login.db.db_name, timeout=0.5)) as con:
            con.execute('BEGIN IMMEDIATE')
            con.rollback()
            seen.append('frei')

    fake_reader.before_return = take_the_write_lock
    assert read(server_port, auto_login, receipt).status == 200 and seen == ['frei']


# ── Ein unerwarteter Fehler ──────────────────────────────────────────────────
def test_an_unexpected_error_is_a_fixed_sentence_and_its_text_stays_in_the_server_log(server_port, auto_login, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError('/srv/geheim')

    monkeypatch.setattr(isolated, 'read_isolated', boom)
    receipt = add_file_receipt(auto_login.db, 'rechnung.xml', ubl())
    response = read(server_port, auto_login, receipt)
    assert (response.status, answer(response)) == (500, {'success': False, 'error': receipt_reader.READ_FAILED})
    for text in ('geheim', '/srv', 'RuntimeError', 'Traceback'):
        assert text not in response.text, text
    assert '/srv/geheim' in capsys.readouterr().err                        # der volle Fehler steht im Server-Log
    assert reading_rows(auto_login.db) == [] and not receipt_reader._read_lock.locked()
    monkeypatch.setattr(isolated, 'read_isolated', FakeReader())
    assert read(server_port, auto_login, receipt).status == 200            # die Sperre ist frei, der Server antwortet weiter


# ── Der Upload liest nie selbst, seine Antwort trägt die IDs ─────────────────
def test_the_upload_reads_nothing_and_its_answer_carries_the_ids_the_page_hands_to_the_reader(server_port, circle, fake_reader):
    entries = created(upload(server_port, circle, ('a.xml', ubl()), ('b.xml', ubl())))
    ids = [entry['id'] for entry in entries]
    assert [entry['status'] for entry in entries] == ['created', 'created'] and all(type(i) is int for i in ids)
    assert ids == [row[0] for row in circle.db.fetch_receipts()]
    assert fake_reader.calls == [] and reading_rows(circle.db) == []       # kein Lesen beim Hochladen
    assert [answer(read(server_port, circle, receipt))['reading']['status'] for receipt in ids] == ['ok', 'ok']
    assert reading_rows(circle.db) == [(ids[0], 'ok'), (ids[1], 'ok')]


# ── Die Kurzform einer Lesung ────────────────────────────────────────────────
def payload_of(**over):
    return receipt_reader.reading_payload(stored_row(**over))


def test_the_short_form_of_a_stored_reading_is_complete_ordered_and_json():
    payload = payload_of()
    assert set(payload) == READING_KEYS
    assert [field['label'] for field in payload['fields']] == list(receipt_reader.FIELD_LABELS.values())
    assert list(receipt_reader.FIELD_LABELS.values()) == ['Belegdatum', 'Brutto', 'Netto', 'Steuer', 'Steuersatz', 'Währung',
                                                         'Absender', 'USt-IdNr.', 'Rechnungsnummer', 'Art']
    by_label = {field['label']: field for field in payload['fields']}
    assert by_label['Brutto'] == {'label': 'Brutto', 'value': '119,00 EUR', 'quote': 'Gesamtbetrag 119,00 EUR'}
    assert by_label['Belegdatum'] == {'label': 'Belegdatum', 'value': '04.03.2026', 'ref': 'BT-2'}
    assert 'quote' not in by_label['Netto'] and 'ref' not in by_label['Netto']
    assert payload['checks'] == ['Netto + Steuer = Brutto: stimmt', 'Steuer passt zum Satz: stimmt'] and payload['docDate'] == DAY
    assert SECRET_TEXT not in json.dumps(payload) and 'GEHEIMER' not in json.dumps(payload)        # nie der Text der Datei


def test_there_is_no_short_form_without_a_reading():
    assert receipt_reader.reading_payload(None) is None and receipt_reader.reading_payload('x') is None


@pytest.mark.parametrize('rate, text', [(0.19, '19 %'), (0.07, '7 %'), (0.055, '5,5 %'), (0.0, '0 %'), (1.0, '100 %'), (1, '100 %'), (0, '0 %')])
def test_the_rate_is_a_percentage_without_float_noise(rate, text):
    assert {f['label']: f['value'] for f in payload_of(TaxRate=rate)['fields']}['Steuersatz'] == text


@pytest.mark.parametrize('rate', [None, True, '0.19', -0.1, 1.5, float('nan'), float('inf')])
def test_a_rate_that_is_no_share_is_not_shown(rate):
    assert 'Steuersatz' not in {f['label'] for f in payload_of(TaxRate=rate)['fields']}


@pytest.mark.parametrize('minor, currency, text', [
    (1190000, 'EUR', '119,00 EUR'), (-125000, 'EUR', '-12,50 EUR'), (123456000, 'USD', '12.345,60 USD'), (0, 'EUR', '0,00 EUR'),
    (1190000, None, '119,00'), (1190000, '', '119,00'), (1190000, '  ', '119,00')])
def test_an_amount_is_in_german_format_with_the_currency(minor, currency, text):
    assert receipt_reader.amount_text(minor, currency) == text


@pytest.mark.parametrize('value', [None, True, '119', 119.0, 10 ** 40])
def test_what_is_no_whole_amount_is_not_shown_and_never_raises(value):
    assert receipt_reader.amount_text(value, 'EUR') is None
    assert 'Brutto' not in {f['label'] for f in payload_of(GrossMinor=value)['fields']}


@pytest.mark.parametrize('stored', [
    pytest.param(None, id='NULL'), pytest.param('', id='leer'), pytest.param('kein JSON', id='kein-JSON'),
    pytest.param('[]', id='Liste'), pytest.param('"text"', id='Text'), pytest.param('5', id='Zahl'), pytest.param('null', id='null'),
    pytest.param('{"fields": 5, "checks": [], "notes": "x"}', id='falsche-Typen'),
    pytest.param('[' * 50000 + ']' * 50000, id='tief-verschachtelt')])
def test_a_broken_raw_reads_as_empty_and_the_columns_still_show(stored):
    payload = payload_of(Raw=stored)
    assert [f['label'] for f in payload['fields']][:2] == ['Belegdatum', 'Brutto']
    assert all('quote' not in f and 'ref' not in f for f in payload['fields'])
    assert payload['checks'] == [] and payload['notes'] == []


def test_only_known_notes_appear_as_their_fixed_sentence_once_each():
    notes = ['by_checksum', 'by_checksum', 'conflict_gross_minor', 'bad_tax_breakdown', 'bad_doc_date', 'no_file', 'evil<script>',
             '', 5, None, ['x'], {'a': 1}]
    raw_json = json.dumps({'notes': notes})
    assert payload_of(Raw=raw_json)['notes'] == [
        receipt_reader.NOTE_TEXTS[code] for code in ('by_checksum', 'conflict_gross_minor', 'bad_tax_breakdown', 'bad_doc_date', 'no_file')]
    assert receipt_reader.NOTE_TEXTS['conflict_gross_minor'] == 'Der Beleg nennt mehrere verschiedene Werte für „Brutto“ – das Feld bleibt leer.'
    assert all(isinstance(text, str) and text for text in receipt_reader.NOTE_TEXTS.values())
    assert payload_of(Raw=json.dumps({'notes': 'by_checksum'}))['notes'] == []                            # keine Liste: nichts


def test_every_note_code_of_the_reading_package_has_a_sentence():
    """Die Schlüssel stehen im Kopf von ``receipt_reading``; dazu ``conflict_``/``bad_`` je Feld (und ``bad_tax_breakdown``)."""
    head = receipt_reading.__doc__
    codes = set(re.findall(r'^``([a-z_]+)``', head, re.M)) - {'conflict_<feld>', 'bad_<feld>'}
    assert codes and not codes - set(receipt_reader.NOTE_TEXTS)
    for name in receipt_reader.FIELD_LABELS:
        assert f'conflict_{name}' in receipt_reader.NOTE_TEXTS and f'bad_{name}' in receipt_reader.NOTE_TEXTS
    assert 'bad_tax_breakdown' in receipt_reader.NOTE_TEXTS


@pytest.mark.parametrize('verdict, expected', [('ok', ['Netto + Steuer = Brutto: stimmt']), ('mismatch', ['Netto + Steuer = Brutto: stimmt nicht']),
                                               ('incomplete', []), ('weiss-nicht', []), (['ok'], []), ({'a': 1}, []), (None, [])])
def test_the_sum_proof_is_a_fixed_sentence_only_when_it_has_a_result(verdict, expected):
    assert payload_of(Raw=json.dumps({'checks': {'sum': verdict}}))['checks'] == expected
    assert payload_of(Raw=json.dumps({'checks': 'ok'}))['checks'] == []


@pytest.mark.parametrize('verdict, expected', [('ok', ['Steuer passt zum Satz: stimmt']), ('mismatch', ['Steuer passt zum Satz: stimmt nicht']),
                                               ('incomplete', []), ('weiss-nicht', []), (['ok'], []), ({'a': 1}, []), (None, [])])
def test_the_rate_proof_is_a_fixed_sentence_only_when_it_has_a_result_exactly_like_the_sum_proof(verdict, expected):
    """Die zweite Probe der Textregeln (``checks.rate``: Netto × Satz = Steuer auf den Cent), nach der Summenprobe; ``incomplete`` – ohne Basis
    je Satz oder ohne Netto – und alles Unbekannte ergibt keinen Satz, eine E-Rechnung kennt nur die Summenprobe."""
    assert payload_of(Raw=json.dumps({'checks': {'rate': verdict}}))['checks'] == expected
    both = payload_of(Raw=json.dumps({'checks': {'sum': 'mismatch', 'rate': verdict}}))['checks']
    assert both == ['Netto + Steuer = Brutto: stimmt nicht', *expected]                                     # erst die Summe, dann die Satzprobe
    assert payload_of(Raw=json.dumps({'checks': {'sum': 'ok'}}))['checks'] == ['Netto + Steuer = Brutto: stimmt']


@pytest.mark.parametrize('source, label', [('einvoice_cii', 'aus der E-Rechnung'), ('einvoice_ubl', 'aus der E-Rechnung'),
                                           ('text', 'aus dem Text gelesen'), ('anderswo', ''), (None, '')])
def test_the_source_has_a_label_and_an_unknown_one_none(source, label):
    assert payload_of(Source=source)['sourceLabel'] == label


@pytest.mark.parametrize('status, label', [
    ('ok', 'gelesen'), ('empty', 'nichts gefunden'), ('no_text', 'nicht lesbar (kein Text – Scan oder Bild)'),
    ('unsupported', 'nicht lesbar (Dateityp)'), ('too_large', 'nicht gelesen (zu groß oder zu aufwendig)'),
    ('failed', 'Lesen fehlgeschlagen'), ('<b>x</b>', 'unbekannter Stand')])
def test_every_status_has_its_word_and_an_unknown_one_a_neutral_word(status, label):
    payload = payload_of(Status=status)
    assert payload['statusLabel'] == label and payload['status'] == (status if status in receipt_reader.STATUS_LABELS else 'unknown')


@pytest.mark.parametrize('version, outdated', [(receipt_reading.READER_VERSION, False), (receipt_reading.READER_VERSION + 1, False),
                                               (0, True), (None, True), ('1', True), (True, True)])
def test_an_older_reader_makes_the_reading_outdated(version, outdated):
    assert payload_of(ReaderVersion=version)['outdated'] is outdated


@pytest.mark.parametrize('day, iso', [('2026-03-04', '2026-03-04'), ('04.03.2026', None), ('2026-02-30', None), ('kaputt', None), (None, None),
                                      ('2026-03-04 ', None), (20260304, None)])
def test_the_read_date_is_shown_and_offered_only_when_it_is_a_real_iso_date(day, iso):
    payload = payload_of(DocDate=day)
    assert payload['docDate'] == iso
    assert ('Belegdatum' in {f['label'] for f in payload['fields']}) == (iso is not None)


def test_the_time_of_the_reading_is_local_time_and_empty_when_it_cannot_be_read(monkeypatch):
    assert payload_of()['readAt'] == '05.03.2026 11:30'
    monkeypatch.setattr(receipt_reader, 'local_datetime', lambda stamp: None)
    assert payload_of(ReadAt='kein Zeitpunkt')['readAt'] == ''


def test_a_quote_and_a_reference_are_only_text_and_at_most_the_raw_limit():
    long = 'x' * (receipt_reading.RAW_STRING_LIMIT + 50)
    raw_json = json.dumps({'fields': {'gross_minor': {'quote': long, 'ref': 5}, 'net_minor': {'quote': 7, 'ref': 'BT-109'},
                                      'tax_minor': 'kein Objekt', 'kind': {'quote': '   '}}})
    fields = {f['label']: f for f in payload_of(Raw=raw_json)['fields']}
    assert fields['Brutto']['quote'] == 'x' * receipt_reading.RAW_STRING_LIMIT and 'ref' not in fields['Brutto']
    assert 'quote' not in fields['Netto'] and fields['Netto']['ref'] == 'BT-109'
    assert 'quote' not in fields['Steuer'] and 'quote' not in fields['Art']


def test_a_credit_note_is_named_and_an_unknown_kind_is_left_out():
    assert {f['label']: f['value'] for f in payload_of(Kind='credit_note')['fields']}['Art'] == 'Gutschrift'
    assert 'Art' not in {f['label'] for f in payload_of(Kind='sonstiges')['fields']}


@pytest.mark.parametrize('summary, text', [
    (('ok', 1190000, 'EUR', 'Muster GmbH', 1), '119,00 EUR · Muster GmbH'),
    (('ok', 1190000, None, None, 1), '119,00'),
    (('ok', None, 'EUR', 'Muster GmbH', 1), 'Muster GmbH'),
    (('ok', None, None, None, 1), 'gelesen'),
    (('ok', None, None, '   ', 1), 'gelesen'),
    (('ok', 1190000, 'EUR', 'x' * 30, 1), '119,00 EUR · ' + 'x' * 30),
    (('ok', 1190000, 'EUR', 'x' * 31, 1), '119,00 EUR · ' + 'x' * 29 + '…'),
    (('ok', 1190000, 'EUR', 'Muster GmbH', 0), '119,00 EUR · Muster GmbH (ältere Regeln)'),
    (('no_text', None, None, None, 1), 'nicht lesbar (kein Text – Scan oder Bild)'),
    (('empty', 1190000, 'EUR', 'Muster GmbH', 1), 'nichts gefunden'),
    (('failed', None, None, None, 0), 'Lesen fehlgeschlagen (ältere Regeln)'),
    (('unsupported', None, None, None, 1), 'nicht lesbar (Dateityp)'),
    (('too_large', None, None, None, 1), 'nicht gelesen (zu groß oder zu aufwendig)'),
    (('seltsam', None, None, None, 1), 'unbekannter Stand'),
    ((None, None, None, None, 1), 'unbekannter Stand')])
def test_the_text_of_the_list_column(summary, text):
    assert receipt_reader.summary_text(summary) == text


def test_the_time_of_a_reading_is_utc_in_the_form_of_the_database():
    assert re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}', REAL_NOW())     # die echte Uhr: nur die Form zählt


# ── Die Liste der Belege ─────────────────────────────────────────────────────
def test_the_list_has_a_column_read_after_the_file_with_a_text_per_state(server_port, auto_login):
    db = auto_login.db
    paper = add_receipt(db, 'P-1', '')
    unread = add_file_receipt(db, 'a.xml', ubl(), number='26B001')
    full = add_file_receipt(db, 'b.xml', ubl(), number='26B002')
    bare = add_file_receipt(db, 'c.xml', ubl(), number='26B003')
    scan = add_file_receipt(db, 'd.png', PNG, number='26B004')
    old = add_file_receipt(db, 'e.xml', ubl(), number='26B005')
    long = add_file_receipt(db, 'f.xml', ubl(), number='26B006')
    store_reading(db, full)
    store_reading(db, bare, good_reading(gross_minor=None, net_minor=None, tax_minor=None, tax_rate=None, currency=None, sender_name=None))
    store_reading(db, scan, Reading(status='no_text'))
    store_reading(db, old, ReaderVersion=0)
    store_reading(db, long, good_reading(sender_name=LONG_SENDER))
    page = get(server_port, '/receipts').text
    assert '<th>Datei</th><th class="readCell">Gelesen</th><th>Info</th>' in page
    cells = cells_of(page)
    assert {receipt: row[4] for receipt, row in cells.items()} == {
        paper: '–', unread: 'ungelesen', full: '119,00 EUR · Muster GmbH', bare: 'gelesen',
        scan: 'nicht lesbar (kein Text – Scan oder Bild)', old: '119,00 EUR · Muster GmbH (ältere Regeln)',
        long: '119,00 EUR · ' + LONG_SENDER[:29] + '…'}                    # höchstens 30 Zeichen des Absenders, mit „…“
    assert all(len(row) == 8 for row in cells.values())                     # Nr., Datum, Dateiname, Datei, Gelesen, Info, Buchungen, Aktionen
    assert len(LONG_SENDER[:29] + '…') == 30


def test_the_read_column_has_a_minimum_width_through_a_class_of_the_stylesheet(server_port, auto_login):
    """In einem schmalen Fenster bricht der Text der Spalte sonst auf viele Zeilen um: Kopf- und Datenzelle tragen ``readCell``, die
    Mindestbreite steht in ``buch.css`` (nie inline)."""
    add_file_receipt(auto_login.db, 'a.xml', ubl(), number='26B001')
    add_receipt(auto_login.db, 'P-1', '')
    page = get(server_port, '/receipts').text
    assert '<th class="readCell">Gelesen</th>' in page
    assert len(re.findall(r'<td class="readCell">', page)) == 2                      # je Zeile eine Datenzelle, auch ohne Datei
    assert [cell for cell in Dom(page).all('td') if cell.get('class') == 'readCell' and 'style' in cell] == []
    stylesheet = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'buch.css')
    with open(stylesheet, encoding='utf-8') as handle:
        assert re.search(r'^\.readCell\s*\{\s*min-width:\s*14em;\s*\}', handle.read(), re.M)


def test_a_receipt_with_a_stored_path_that_does_not_apply_counts_as_unread_and_the_list_says_so(server_port, auto_login):
    receipt = add_receipt(auto_login.db, '26B001', 'Belege/2026/weg.pdf')
    assert cells_of(get(server_port, '/receipts').text)[receipt][4] == 'ungelesen'
    assert receipt_reading.READER_VERSION and auto_login.db.count_unread_receipts(receipt_reading.READER_VERSION) == 1


def read_box(page):
    """Der Kasten über der Liste: ``(Attribute, Text im Kasten, onclick des Knopfes oder None)``."""
    dom = Dom(page)
    box = dom.one('div', id='readBox')
    start = page.index('id="readBox"')
    text = re.sub(r'<[^>]+>', '', page[start:page.index('</div>', start)])
    buttons = [a for a in dom.all('button') if 'readReceipts' in (a.get('onclick') or '')]
    return box, html.unescape(text.split('>', 1)[-1]).strip(), (buttons[0]['onclick'] if buttons else None)


def test_the_box_over_the_list_offers_to_read_the_unread_receipts_with_their_ids(server_port, auto_login):
    db = auto_login.db
    add_receipt(db, 'P-1', '')                                               # ohne Datei: zählt nicht
    first = add_file_receipt(db, 'a.xml', ubl(), number='26B001')
    done = add_file_receipt(db, 'b.xml', ubl(), number='26B002')
    second = add_file_receipt(db, 'c.xml', ubl(), number='26B003')
    store_reading(db, done)
    box, text, onclick = read_box(get(server_port, '/receipts').text)
    assert 'hidden' not in box and box['class'] == 'rectRounded'
    assert text.startswith('2 Belege mit Datei sind noch nicht (oder nur mit älteren Regeln) gelesen.') and 'nächsten' not in text
    assert onclick == f'readReceipts([{first}, {second}])'
    assert '2 ungelesene Belege lesen' in get(server_port, '/receipts').text
    store_reading(db, second, ReaderVersion=0)                               # eine Lesung eines älteren Lesers gilt als ungelesen
    assert read_box(get(server_port, '/receipts').text)[2] == f'readReceipts([{first}, {second}])'


def test_one_unread_receipt_is_a_singular_sentence_and_button(server_port, auto_login):
    receipt = add_file_receipt(auto_login.db, 'a.xml', ubl())
    page = get(server_port, '/receipts').text
    _, text, onclick = read_box(page)
    assert text.startswith('1 Beleg mit Datei ist noch nicht (oder nur mit älteren Regeln) gelesen.') and onclick == f'readReceipts([{receipt}])'
    assert '1 ungelesenen Beleg lesen' in page


def test_without_unread_receipts_the_box_is_an_empty_hidden_place_for_the_progress_of_an_upload(server_port, auto_login):
    receipt = add_file_receipt(auto_login.db, 'a.xml', ubl())
    store_reading(auto_login.db, receipt)
    page = get(server_port, '/receipts').text
    box, text, onclick = read_box(page)
    assert box == {'id': 'readBox', 'class': 'rectRounded', 'hidden': None} and text == '' and onclick is None
    assert 'ungelesene' not in page and 'function readReceipts(' in page    # das Skript steht trotzdem da: der Upload braucht es


def test_more_than_a_hundred_unread_receipts_offer_the_next_hundred(server_port, auto_login):
    db = auto_login.db
    ids = [add_receipt(db, f'B-{n:03d}', f'Belege/2026/b{n}.pdf') for n in range(101)]
    _, text, onclick = read_box(get(server_port, '/receipts').text)
    assert text.startswith('101 Belege mit Datei sind noch nicht (oder nur mit älteren Regeln) gelesen. Gelesen werden die nächsten 100.')
    assert json.loads(onclick[len('readReceipts('):-1]) == ids[:100]
    assert '100 ungelesene Belege lesen' in get(server_port, '/receipts').text


def test_in_demo_mode_the_page_has_neither_the_box_nor_the_reader(server_port, monkeypatch):
    demo_site(monkeypatch)
    db = installation.open_db('demo')
    add_file_receipt(db, 'a.xml', ubl(), user='demo')
    page = get(server_port, '/receipts').text
    assert 'readBox' not in page and 'readReceipts' not in page and '/receipts/read' not in page and 'ungelesene' not in page
    assert cells_of(page)[db.fetch_receipts()[0][0]][4] == 'ungelesen'          # die Spalte bleibt: sie zeigt nur, was gespeichert ist


# ── Alles Fremde bleibt Text ─────────────────────────────────────────────────
def hostile_reading():
    return good_reading(sender_name=HOSTILE_SENDER, invoice_number=HOSTILE_NUMBER, sender_vat_id=HOSTILE_QUOTE,
                        raw={'fields': {'gross_minor': {'value': 1190000, 'source': 'rule', 'quote': HOSTILE_QUOTE, 'page': 1},
                                        'sender_name': {'value': HOSTILE_SENDER, 'source': 'rule', 'quote': HOSTILE_SENDER, 'page': 1},
                                        'doc_date': {'value': '2026-03-04', 'source': 'einvoice', 'ref': HOSTILE_NUMBER}},
                             'checks': {'sum': 'mismatch'}})


def test_hostile_values_of_a_reading_are_text_in_the_list_and_on_the_page_of_the_receipt(server_port, auto_login):
    db = auto_login.db
    receipt = add_file_receipt(db, 'a.xml', ubl())
    store_reading(db, receipt, hostile_reading())
    for page in (get(server_port, '/receipts').text, get(server_port, f'/receipts/edit?id={receipt}').text):
        for text in (HOSTILE_SENDER, HOSTILE_NUMBER, HOSTILE_QUOTE, '<script>alert', '<img'):
            assert text not in page, text
        assert Dom(page).all('img') == []
        assert [a for a in Dom(page).scripts if 'alert(1)' in a] == []
    assert html.escape(HOSTILE_SENDER[:30], quote=True) in get(server_port, '/receipts').text            # maskiert, nicht weggelassen
    edit = get(server_port, f'/receipts/edit?id={receipt}').text
    assert html.escape(HOSTILE_SENDER, quote=True) in edit and html.escape(HOSTILE_NUMBER, quote=True) in edit
    assert f'„{html.escape(HOSTILE_QUOTE, quote=True)}“' in edit                                           # das Zitat, als Text
    assert Dom(edit).one('div', id='receiptReading') == {'class': 'rectRounded', 'id': 'receiptReading'}


# ── Die Seite eines Belegs ───────────────────────────────────────────────────
def edit_page(port, receipt):
    page = get(port, f'/receipts/edit?id={receipt}')
    assert page.status == 200
    return page.text


def box_of(page):
    """Der Kasten „Gelesen“ als Text (Entitäten aufgelöst) und sein Markup."""
    start = page.index('id="receiptReading"')
    markup = page[start:page.index('<script>', start)]
    return markup, html.unescape(re.sub(r'<[^>]+>', ' ', markup))


def buttons_of(page):
    return {(a.get('onclick') or ''): a for a in Dom(page).all('button')}


def test_the_page_of_a_receipt_has_the_date_field_with_an_id_and_the_box_before_the_linked_bookings(server_port, auto_login):
    receipt = add_file_receipt(auto_login.db, 'a.xml', ubl())
    page = edit_page(server_port, receipt)
    assert Dom(page).one('input', id='receiptDate')['name'] == 'date' and Dom(page).one('input', id='receiptDate')['value'] == DAY
    assert page.index('id="receiptReading"') < page.index('<h2>Verknüpfte Buchungen</h2>')
    assert page.index('id="receiptDate"') < page.index('id="receiptReading"')


def test_a_receipt_with_a_file_and_no_reading_says_so_and_offers_to_read(server_port, auto_login):
    receipt = add_file_receipt(auto_login.db, 'a.xml', ubl())
    page = edit_page(server_port, receipt)
    markup, text = box_of(page)
    assert 'Noch nicht gelesen.' in text and READING_HINT not in text and 'table' not in markup
    buttons = buttons_of(page)
    assert buttons['receiptRead(this)']['data-id'] == str(receipt)
    assert [a for a in Dom(page).all('button') if (a.get('onclick') or '') == 'receiptRead(this)'] and '>Lesen</button>' in markup
    assert 'Neu lesen' not in markup and 'receiptTakeDate' not in markup


@pytest.mark.parametrize('path, sentence', [
    pytest.param('', 'Ohne Datei gibt es nichts zu lesen.', id='Papierbeleg'),
    pytest.param('Belege/2026/weg.pdf', 'Ohne Datei gibt es nichts zu lesen.', id='Datei-fehlt'),
    pytest.param('/srv/alt/x.pdf', 'Ohne Datei gibt es nichts zu lesen.', id='ausserhalb'),
])
def test_a_receipt_without_a_file_has_no_button_to_read(server_port, auto_login, path, sentence):
    receipt = add_receipt(auto_login.db, '26B001', path)
    page = edit_page(server_port, receipt)
    markup, text = box_of(page)
    assert sentence in text and '<button' not in markup and '/srv/alt' not in page


def test_a_stored_reading_is_shown_with_status_source_time_fields_quotes_references_proof_and_notes(server_port, auto_login):
    db = auto_login.db
    receipt = add_file_receipt(db, 'a.xml', ubl())
    store_reading(db, receipt, good_reading(notes=['by_checksum', 'foreign_currency', 'kein_bekannter_befund']))
    page = edit_page(server_port, receipt)
    markup, text = box_of(page)
    assert 'gelesen · aus dem Text gelesen · 05.03.2026 11:30' in re.sub(r'\s+', ' ', text)
    table = [[html.unescape(re.sub(r'<[^>]+>', '', cell)).strip() for cell in re.findall(r'<td>(.*?)</td>', row, re.S)]
             for row in re.findall(r'<tr>(.*?)</tr>', markup, re.S)]
    assert table == [
        ['Belegdatum', '04.03.2026BT-2'], ['Brutto', '119,00 EUR„Gesamtbetrag 119,00 EUR“'], ['Netto', '100,00 EUR'],
        ['Steuer', '19,00 EUR'], ['Steuersatz', '19 %'], ['Währung', 'EUR'], ['Absender', 'Muster GmbH'], ['USt-IdNr.', 'DE123456789'],
        ['Rechnungsnummer', 'R-100'], ['Art', 'Rechnung']]
    assert '<span class="readingQuote muted">„Gesamtbetrag 119,00 EUR“</span>' in markup and '<span class="readingQuote muted">BT-2</span>' in markup
    assert 'Netto + Steuer = Brutto: stimmt' in text and 'Steuer passt zum Satz: stimmt' in text and 'stimmt nicht' not in text
    assert receipt_reader.NOTE_TEXTS['by_checksum'] in text and receipt_reader.NOTE_TEXTS['foreign_currency'] in text
    assert 'kein_bekannter_befund' not in page and SECRET_TEXT not in page and 'GEHEIMER' not in page          # nie der Text der Datei
    assert READING_HINT in text and READING_HINT == 'Gelesene Werte sind eine Lesung der Datei – bitte prüfen.'
    assert 'Neu lesen' in markup and '>Lesen<' not in markup and 'älteren Regeln' not in text


def test_a_sum_that_does_not_match_is_marked_as_a_warning_and_an_old_reader_is_named(server_port, auto_login):
    db = auto_login.db
    receipt = add_file_receipt(db, 'a.xml', ubl())
    store_reading(db, receipt, good_reading(raw={'checks': {'sum': 'mismatch'}}), ReaderVersion=0)
    markup, text = box_of(edit_page(server_port, receipt))
    assert '<p class="warnColor">Netto + Steuer = Brutto: stimmt nicht</p>' in markup and 'mit älteren Regeln gelesen' in text


def test_a_reading_without_fields_shows_the_status_and_the_notes_but_no_table(server_port, auto_login):
    db = auto_login.db
    receipt = add_file_receipt(db, 'a.png', PNG)
    store_reading(db, receipt, Reading(status='no_text', notes=['pdf_invalid']))
    markup, text = box_of(edit_page(server_port, receipt))
    assert 'nicht lesbar (kein Text – Scan oder Bild)' in text and receipt_reader.NOTE_TEXTS['pdf_invalid'] in text
    assert '<table' not in markup and 'Neu lesen' in markup


def test_the_button_to_take_the_read_date_is_offered_only_when_it_differs_and_only_sets_the_field(server_port, auto_login):
    db = auto_login.db
    same = add_file_receipt(db, 'a.xml', ubl(), number='26B001')                          # Belegdatum = gelesenes Datum
    other = add_file_receipt(db, 'b.xml', ubl(), number='26B002', day='2026-03-09')       # Belegdatum weicht ab
    none = add_file_receipt(db, 'c.xml', ubl(), number='26B003')                          # die Lesung nennt kein Datum
    broken = add_file_receipt(db, 'd.xml', ubl(), number='26B004')                        # ein Datum, das keines ist (SQL-Konsole)
    for receipt in (same, other):
        store_reading(db, receipt)
    store_reading(db, none, good_reading(doc_date=None))
    store_reading(db, broken, DocDate='04.03.2026')
    for receipt in (same, none, broken):
        markup, _ = box_of(edit_page(server_port, receipt))
        assert 'receiptTakeDate' not in markup and 'receiptDateHint' not in markup, receipt
    before = state(db, 'Documents')
    page = edit_page(server_port, other)
    markup, text = box_of(page)
    button = buttons_of(page)['receiptTakeDate(this)']
    assert button['data-date'] == DAY and 'Gelesenes Datum übernehmen (04.03.2026)' in text
    assert Dom(page).one('span', id='receiptDateHint')['class'] == 'warnColor'
    assert state(db, 'Documents') == before                                              # die Seite ändert nichts: gespeichert wird übers Formular


def test_in_demo_mode_the_page_of_a_receipt_has_no_button_to_read_but_still_offers_the_date(server_port, monkeypatch):
    demo_site(monkeypatch)
    db = installation.open_db('demo')
    receipt = add_file_receipt(db, 'a.xml', ubl(), user='demo', day='2026-03-09')
    store_reading(db, receipt)
    markup, text = box_of(edit_page(server_port, receipt))
    assert 'receiptRead(this)' not in markup and 'Neu lesen' not in markup and '>Lesen<' not in markup
    assert 'receiptTakeDate(this)' in markup and 'gelesen · aus dem Text gelesen' in re.sub(r'\s+', ' ', text)


def test_the_page_of_a_receipt_still_says_not_found_for_an_unknown_id(tmp_db):
    assert PageReceiptEdit(tmp_db, 999) == 'Beleg nicht gefunden.' and PageReceiptEdit(tmp_db, -1) == 'Beleg nicht gefunden.'


# ── Die Skripte ──────────────────────────────────────────────────────────────
FORBIDDEN = ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'javascript:', 'eval(', 'Function(')


@pytest.mark.parametrize('name, script', [('READ_JS', READ_JS), ('RECEIPT_READ_JS', RECEIPT_READ_JS)])
def test_the_reading_scripts_set_no_markup_and_use_only_var_and_function(name, script):
    for forbidden in FORBIDDEN:
        assert forbidden not in script, (name, forbidden)
    assert not re.search(r'(?<![A-Za-z0-9_$.])(const|let)\s', script), name
    assert '=>' not in script and '</script' not in script.lower() and "'<" not in script and '"<' not in script


def test_the_list_reader_goes_one_receipt_at_a_time_with_the_busy_rules_of_the_route():
    assert "fetch('/receipts/read', {method: 'POST'" in READ_JS and 'JSON.stringify({documentId: id})' in READ_JS
    assert 'var READ_BUSY_WAIT = 2000;' in READ_JS and 'var READ_MAX_ATTEMPTS = 30;' in READ_JS         # 2 s warten, höchstens 30 Versuche
    assert 'data.busy && attempts < READ_MAX_ATTEMPTS' in READ_JS and 'setTimeout(next, READ_BUSY_WAIT)' in READ_JS
    assert "progress.textContent = 'Lese Beleg ' + (position + 1) + ' von ' + ids.length + ' …';" in READ_JS
    assert "cancel.textContent = 'Abbrechen';" in READ_JS and 'cancelled = true;' in READ_JS and 'if (cancelled || position >= ids.length)' in READ_JS
    assert ".catch(function () { return null; })" in READ_JS and 'counts.failed += 1;' in READ_JS         # ein Fehler zählt und hält nichts an
    assert "counts.read + ' gelesen, ' + counts.unreadable + ' nicht lesbar'" in READ_JS
    assert 'location.reload()' in READ_JS and 'if (typeof done ===' in READ_JS


def test_the_page_script_hands_the_ids_of_the_uploaded_receipts_to_the_reader_instead_of_reloading_at_once(server_port, auto_login):
    page = get(server_port, '/receipts').text
    (script,) = [text for text in Dom(page).scripts if 'uploadFiles' in text]
    assert 'function readReceipts(' in script and 'function readOne(' in script       # im selben Skript: Lesen und Hochladen gibt es nur gemeinsam
    assert "if (typeof entry.id === 'number') created.push(entry.id);" in script
    assert 'if (created.length) readReceipts(created, afterwards);' in script and 'else afterwards();' in script
    assert re.search(r'const afterwards = \(\) => \{\s*if \(problem\) \{.*?\} else \{\s*setTimeout\(\(\) => location\.reload\(\), 3000\);', script, re.S)
    assert "addUploadNote('✓ ' + entry.filename" in script                          # die Meldungen des Uploads bleiben stehen
    assert len([text for text in Dom(page).scripts if 'function readReceipts(' in text]) == 1
    for forbidden in FORBIDDEN:
        assert forbidden not in script, forbidden


def test_the_script_of_the_page_of_a_receipt_reads_by_post_and_takes_the_date_into_the_field_only(server_port, auto_login):
    receipt = add_file_receipt(auto_login.db, 'a.xml', ubl())
    (script,) = [text for text in Dom(edit_page(server_port, receipt)).scripts if 'function receiptRead(' in text]
    assert script == RECEIPT_READ_JS and 'receiptLinksRender' not in script
    assert "fetch('/receipts/read', {method: 'POST'" in script and 'Number(button.dataset.id)' in script and 'location.reload()' in script
    assert "appMsg(data && data.error ? data.error" in script and 'button.disabled = false;' in script
    take = script[script.index('function receiptTakeDate('):]
    assert "document.getElementById('receiptDate').value = button.dataset.date;" in take
    assert "document.getElementById('receiptDateHint').textContent = " in take
    assert 'submit' not in script and 'fetch(' not in take                          # nichts wird gespeichert: das tut das Formular
