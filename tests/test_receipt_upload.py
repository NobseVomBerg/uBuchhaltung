# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belege hochladen (2.5.11): ``POST /receipts/upload`` – jede Datei wird ein Beleg.

* Jede Datei, die auf der Belege-Seite hochgeladen wird, bekommt sofort einen Eintrag (Nummer aus dem Kreis „Belegnummern
  Firma“, Datum, Dateiname) und liegt unter ``Belege/<Jahr>/<Nummer>_<Name>``; ``Documents.Path`` steht relativ zum Datenordner
  des Benutzers. Den Pfad setzt der Server. Nummer, Datei und Eintrag gehören zusammen: Scheitert ein Schritt, ist der Zähler
  nicht verbraucht, es gibt keinen Eintrag, und keine Datei bleibt liegen.
* Ein Kontoauszug ist dort ein Beleg wie jede andere Datei (Datum aus dem Auszug, ein Hinweis auf den Import der Seite „Buchungen“):
  kein Import, nichts in ``pending_imports/``, nichts unter ``Documents/``. ``POST /upload_receipts`` bleibt der Import dieser Seite.
* Der Dateiname wird bereinigt (nie ein Pfad, nie ein Name aus dem Ordner heraus, nie ein Windows-Gerät, höchstens 200 Bytes, kein
  Name der Ordner des Kontoauszug-Imports); eine vorhandene Datei wird nie überschrieben (``_2``, ``_3`` …).
* Die Datei wird VOR der Transaktion als Teildatei geschrieben (das Schreiben hält keine Schreibsperre der Datenbank); danach belegt die
  Transaktion nur den Namen. Ein Jahresordner, der ein Link ist, nimmt nichts an.
* Ohne jeden Kreis „Belegnummern Firma“ entsteht einer für das LAUFENDE Jahr – sonst nie von selbst. Eine eigene Nummer hebt einen
  Zähler nur bis ``MAX_RECEIPT_RUNNING`` und löst nie einen Fehler aus.
* Vor dem ersten gelesenen Byte stehen das CSRF-Token, die Herkunft, der Demo-Modus (abgeschaltet) und die Größe.
* Das Formular „Beleg hinzufügen“ (Papierbeleg) verbraucht die Nummer jetzt auch; ein Pfad wird dort nie angenommen.

Alle Namen, Nummern und Inhalte sind erfundene Testwerte; „heute“ ist ein festes Datum (``receipt_files._today``), das laufende Jahr
des Kreises ebenso (``numbering._current_year``).
"""
import contextlib
import json
import os
import re
import sqlite3

import pytest

import auth
import demo
import userctx
from db import Database, numbering
from server import receipt_files
from server.app import MAX_UPLOAD_BODY
from server.upload_handler import (MAX_RECEIPT_FILES, RECEIPT_EMPTY_FILE, RECEIPT_NO_NUMBER_HINT, RECEIPT_STATEMENT_HINT,
                                   RECEIPT_TOO_MANY_FILES, RECEIPT_UPLOAD_FAILED, RECEIPTS_DEMO_REFUSED)
from tests import installation
from tests.dom_helpers import Dom
from tests.http_client import FOREIGN_ORIGIN, get, own_origin, post, send
from tests.test_backup_download import make_symlink
from tests.test_body_limits import assert_closed, exchange
from tests.test_dkb_csv_import import NEW_ROWS, new_format
from tests.test_statement_import_guards import (A_BOOKING, B_BOOKING, CLOSING_LINE, OPENING_LINE, make_pdf, one_page)

DAY = '2026-03-04'
SECRET = '/srv/geheim/ablage'
FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    """Der Tag des Uploads und das laufende Jahr sind fest: Datum, Jahresordner und der Kreis, der ohne jeden Kreis entsteht, hängen nie
    an der Uhr der Maschine."""
    monkeypatch.setattr(receipt_files, '_today', lambda: DAY)
    monkeypatch.setattr(numbering, '_current_year', lambda: 2026)


@pytest.fixture
def circle(auto_login):
    """Ein Nummernkreis „Belegnummern Firma“ für 2026, Zähler 0: Die Nummern heißen 26B001, 26B002 …"""
    auto_login.db.insert_number_range('receipt_company', 2026, 'B', current_number=0)
    return auto_login


# ── Hilfen ───────────────────────────────────────────────────────────────────
def plain_pdf():
    return make_pdf([['Eine Rechnung', 'Nr. 4711']])


def statement_pdf():
    return make_pdf(one_page(OPENING_LINE, *A_BOOKING, *B_BOOKING, CLOSING_LINE))


def admin_dir():
    return os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN)


def belege(*parts):
    return os.path.join(admin_dir(), 'Belege', *parts)


def multipart(files, escape=True):
    """Ein Multipart-Body mit je einem Teil ``files``. ``escape`` setzt Rückstrich und Anführungszeichen im Dateinamen als
    Quoted-Pair (``\\\\``, ``\\"``), so kommt der Name verlustfrei an; ohne ``escape`` stehen sie roh da – wie bei Browsern, die
    nur ``"`` und Zeilenumbrüche ersetzen (der Parser macht aus ``C:\\a\\b.pdf`` dann ``C:ab.pdf``)."""
    boundary = 'GRENZE1234567890'
    body = b''
    for name, content in files:
        shown = name.replace('\\', '\\\\').replace('"', '\\"') if escape else name
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{shown}"\r\n'
                 'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8') + content + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    return f'multipart/form-data; boundary={boundary}', body


def upload(port, login, *files, headers=None, escape=True):
    """Dateien über die Route des Servers hochladen, wie es die Belege-Seite tut (eigene Herkunft, Token als Header)."""
    content_type, body = multipart(files, escape)
    return send(port, 'POST', '/receipts/upload', {'Origin': own_origin(port), 'X-CSRF-Token': login.csrf,
                                                    'Content-Type': content_type, **(headers or {})}, body)


def created(response):
    """Die Einträge einer gelungenen Antwort."""
    assert response.status == 200, response.text
    assert response.headers['Content-Type'] == 'application/json'
    data = json.loads(response.text)
    assert data['success'] is True
    return data['files']


def counter(db):
    return db.fetch_number_ranges('receipt_company')[0][5]


def read(path):
    with open(path, 'rb') as handle:
        return handle.read()


def files_in(root):
    """Alle Dateien unter ``root`` (relativ, mit ``/``), ohne Ordner."""
    found = []
    for folder, _dirs, names in os.walk(root):
        found += [os.path.relpath(os.path.join(folder, name), root).replace(os.sep, '/') for name in names]
    return sorted(found)


def scalar(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql, args).fetchone()[0]


def staging_is_empty():
    """Das Lesen eines PDF legt seine Datei nur für diesen Aufruf in ``upload_tmp/receipts/`` ab."""
    folder = os.path.join(admin_dir(), 'upload_tmp', 'receipts')
    return not os.path.exists(folder) or os.listdir(folder) == []


# ── Der Weg: Eintrag, Datei, Zähler ──────────────────────────────────────────
def test_an_uploaded_file_becomes_a_receipt_with_number_date_name_and_file(server_port, circle):
    content = plain_pdf()
    entries = created(upload(server_port, circle, ('rechnung.pdf', content)))
    (row,) = circle.db.fetch_receipts()
    assert entries == [{'filename': 'rechnung.pdf', 'status': 'created', 'id': row[0], 'number': '26B001', 'date': DAY}]
    assert row[1:] == ('26B001', DAY, 'rechnung.pdf', 'Belege/2026/26B001_rechnung.pdf', '')     # relativ, mit /, vom Server
    assert read(belege('2026', '26B001_rechnung.pdf')) == content
    assert counter(circle.db) == 1
    assert staging_is_empty()                                                                  # das Lesen hinterließ nichts
    assert not os.path.exists(os.path.join(admin_dir(), 'pending_imports'))
    assert not os.path.exists(os.path.join(admin_dir(), 'Documents'))                          # nicht der Ordner des Imports


def test_two_files_get_two_numbers_in_order_and_each_file_its_own_name(server_port, circle):
    entries = created(upload(server_port, circle, ('a.pdf', plain_pdf()), ('a.txt', b'Notiz'), ('a.pdf', b'anderer Inhalt')))
    assert [(e['filename'], e['number']) for e in entries] == [('a.pdf', '26B001'), ('a.txt', '26B002'), ('a.pdf', '26B003')]
    assert files_in(belege()) == ['2026/26B001_a.pdf', '2026/26B002_a.txt', '2026/26B003_a.pdf']
    assert counter(circle.db) == 3
    assert [row[1] for row in circle.db.fetch_receipts()] == ['26B001', '26B002', '26B003']


def test_a_circle_without_a_usable_template_gives_the_receipt_no_number_a_hint_and_only_its_name(server_port, auto_login):
    """Liefert der Kreis keine Nummer (Vorlage ohne Zähler), hat der Beleg keine, die Antwort einen Hinweis, und die Datei trägt nur ihren
    Namen. Es entsteht auch kein weiterer Kreis – nur ein Bestand GANZ ohne Kreis bekommt einen (siehe unten)."""
    auto_login.db.insert_number_range('receipt_company', 2026, 'B', number_format='{yy}{l}')
    (entry,) = created(upload(server_port, auto_login, ('rechnung.pdf', plain_pdf())))
    assert entry['number'] == '' and entry['hint'] == RECEIPT_NO_NUMBER_HINT
    assert RECEIPT_NO_NUMBER_HINT == 'Kein brauchbarer Nummernkreis „Belegnummern Firma“ – der Beleg hat keine Nummer.'
    (row,) = auto_login.db.fetch_receipts()
    assert row[1] == '' and row[4] == 'Belege/2026/rechnung.pdf'                               # ohne Nummer nur der Name
    assert files_in(belege()) == ['2026/rechnung.pdf']
    assert [r[2:4] for r in auto_login.db.fetch_number_ranges('receipt_company')] == [(2026, 'B')]       # kein zweiter Kreis


def test_without_any_circle_the_first_upload_creates_one_for_the_current_year_and_numbers_from_it(server_port, auto_login, monkeypatch):
    """Ein Bestand ohne Kreis „Belegnummern Firma“: Der erste Upload legt einen an – für das LAUFENDE Jahr, nicht für das Jahr des
    Belegdatums (es stammt aus der hochgeladenen Datei) – und nummeriert aus ihm; der zweite zählt weiter, ohne einen zweiten Kreis."""
    monkeypatch.setattr(numbering, '_current_year', lambda: 2031)
    first, second = created(upload(server_port, auto_login, ('a.pdf', plain_pdf()), ('b.pdf', plain_pdf())))
    assert (first['number'], second['number']) == ('31B001', '31B002') and first['date'] == second['date'] == DAY
    assert 'hint' not in first and 'hint' not in second
    assert [row[1:] for row in auto_login.db.fetch_number_ranges('receipt_company')] == [
        ('receipt_company', 2031, 'B', '', 2, 'Belegnummern Firma', '{yy}{l}{nnn}{s}')]
    assert files_in(belege()) == ['2026/31B001_a.pdf', '2026/31B002_b.pdf']                    # der Ordner folgt dem Belegdatum
    assert [row[1] for row in auto_login.db.fetch_receipts()] == ['31B001', '31B002']


def test_a_new_year_never_creates_a_circle_by_itself(server_port, auto_login, monkeypatch):
    """Es entsteht nur ein Kreis, wenn es noch keinen gibt – auch zum Jahreswechsel nie ein weiterer: Der Kreis des Vorjahres zählt weiter,
    wie es der Vorschlag der Seite zeigt."""
    auto_login.db.insert_number_range('receipt_company', 2026, 'B', current_number=3)
    monkeypatch.setattr(numbering, '_current_year', lambda: 2027)
    (entry,) = created(upload(server_port, auto_login, ('rechnung.pdf', plain_pdf())))
    assert entry['number'] == '26B004'
    assert [(r[2], r[5]) for r in auto_login.db.fetch_number_ranges('receipt_company')] == [(2026, 4)]


def test_a_template_without_a_counter_gives_no_number_either(server_port, auto_login):
    auto_login.db.insert_number_range('receipt_company', 2026, 'B', number_format='{yy}{l}')
    (entry,) = created(upload(server_port, auto_login, ('rechnung.pdf', plain_pdf())))
    assert entry['number'] == '' and entry['hint'] == RECEIPT_NO_NUMBER_HINT
    assert auto_login.db.fetch_number_ranges('receipt_company')[0][5] == 0


def test_a_year_without_a_circle_takes_the_youngest_circle_like_the_proposal_does(server_port, auto_login):
    """Wie ``peek_next_number`` und ``consume_number``: Gibt es für das Jahr des Belegs keinen Kreis, zählt der jüngste weiter."""
    auto_login.db.insert_number_range('receipt_company', 2025, 'B', current_number=11)
    (entry,) = created(upload(server_port, auto_login, ('rechnung.pdf', plain_pdf())))           # der Tag des Uploads liegt in 2026
    assert entry['number'] == '25B012' and entry['date'] == DAY
    assert files_in(belege()) == ['2026/25B012_rechnung.pdf']                                  # der Ordner folgt dem Belegdatum
    assert [r[2:4] for r in auto_login.db.fetch_number_ranges('receipt_company')] == [(2025, 'B')]
    assert counter(auto_login.db) == 12


# ── Kontoauszüge sind Belege wie jede andere Datei ───────────────────────────
def test_a_bank_statement_is_a_receipt_with_the_date_of_the_statement_and_a_hint_and_no_import(server_port, circle):
    circle.db.insert_number_range('receipt_company', 2025, 'B', current_number=4)
    content = statement_pdf()
    (entry,) = created(upload(server_port, circle, ('auszug-januar.pdf', content)))
    (row,) = circle.db.fetch_receipts()
    assert entry == {'filename': 'auszug-januar.pdf', 'status': 'created', 'id': row[0], 'number': '25B005',
                     'date': '2025-01-03', 'hint': RECEIPT_STATEMENT_HINT}
    assert RECEIPT_STATEMENT_HINT == ('Kontoauszug erkannt und als Beleg abgelegt – '
                                      'Umsätze werden auf der Seite „Buchungen“ importiert.')
    assert row[2] == '2025-01-03' and row[4] == 'Belege/2025/25B005_auszug-januar.pdf'
    assert files_in(belege()) == ['2025/25B005_auszug-januar.pdf'] and read(belege('2025', '25B005_auszug-januar.pdf')) == content
    # kein Import und nichts Ausstehendes; die Datei wandert nicht nach Belege/<Jahr>/Konten/<Bank>/ oder Documents/
    assert not os.path.exists(os.path.join(admin_dir(), 'pending_imports'))
    assert not os.path.exists(os.path.join(admin_dir(), 'Documents'))
    assert scalar(circle.db, 'SELECT COUNT(*) FROM Bookings') == 0                                  # keine Bankbewegung importiert
    assert counter_of_year(circle.db, 2025) == 5 and counter_of_year(circle.db, 2026) == 0


def counter_of_year(db, year):
    return next(row[5] for row in db.fetch_number_ranges('receipt_company') if row[2] == year)


def test_camt_and_csv_statements_are_receipts_too(server_port, circle):
    camt = read(os.path.join(FIXTURES, 'camt', 'camt053_v02.xml'))
    csv = new_format(*NEW_ROWS).encode('utf-8-sig')
    entries = created(upload(server_port, circle, ('camt.xml', camt), ('umsaetze.csv', csv)))
    assert [(e['filename'], e['date'], e['hint']) for e in entries] == [
        ('camt.xml', '2026-03-31', RECEIPT_STATEMENT_HINT),                      # das Belegdatum nennt der Auszug
        ('umsaetze.csv', DAY, RECEIPT_STATEMENT_HINT)]                           # eine Umsatzliste nennt keines: der Tag des Uploads
    assert files_in(belege()) == ['2026/26B001_camt.xml', '2026/26B002_umsaetze.csv']
    assert read(belege('2026', '26B001_camt.xml')) == camt and read(belege('2026', '26B002_umsaetze.csv')) == csv
    assert not os.path.exists(os.path.join(admin_dir(), 'pending_imports'))


@pytest.mark.parametrize('name, content', [
    pytest.param('kaputt.pdf', b'das ist kein PDF', id='kein-PDF'),
    pytest.param('kaputt.xml', b'<keine-camt-datei/>', id='kein-CAMT'),
    pytest.param('liste.csv', b'a;b\n1;2\n', id='keine-DKB-Liste'),
    pytest.param('archiv.zip', b'PK\x03\x04nicht wirklich', id='kein-ZIP'),
    pytest.param('foto.png', b'\x89PNG\r\n\x1a\nbild', id='Bild'),
])
def test_a_file_that_cannot_be_read_as_a_statement_is_a_receipt_all_the_same(server_port, circle, capsys, name, content):
    (entry,) = created(upload(server_port, circle, (name, content)))
    assert entry == {'filename': name, 'status': 'created', 'id': entry['id'], 'number': '26B001', 'date': DAY}   # kein Hinweis
    assert read(belege('2026', f'26B001_{name}')) == content
    assert 'Traceback' not in capsys.readouterr().err and staging_is_empty()


def test_reading_a_pdf_leaves_nothing_behind_and_logs_no_content_even_when_the_reader_fails(circle, monkeypatch, capsys):
    import document_parser

    def broken(self, filepath):
        raise RuntimeError('geheimer Inhalt der Datei ' + SECRET)
    monkeypatch.setattr(document_parser.DocumentParser, 'parse_document', broken)
    userctx.set_user(installation.ADMIN)
    try:
        assert receipt_files.receipt_date('x.pdf', plain_pdf()) == (DAY, False)
        folder = os.path.join(userctx.user_data_dir(), 'upload_tmp', 'receipts')
    finally:
        userctx.clear()
    assert os.listdir(folder) == []
    err = capsys.readouterr().err
    assert 'RuntimeError' in err and 'geheimer Inhalt' not in err and SECRET not in err      # nur der Name der Ausnahme


# ── Namen ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('original, expected', [
    ('Rechnung 4711.pdf', 'Rechnung 4711.pdf'),
    ('Büro Müller ß (2).pdf', 'Büro Müller ß (2).pdf'),
    ('A\u0308rger.pdf', 'Ärger.pdf'),                                   # NFC: ein zerlegtes Ä bleibt erlaubt
    ('Cafe\u0301.pdf', 'Caf_.pdf'),                                     # é gehört nicht zu den erlaubten Zeichen
    ('../../x.pdf', 'x.pdf'),
    ('..\\..\\x.pdf', 'x.pdf'),
    ('C:\\a\\..\\b.pdf', 'b.pdf'),
    ('/etc/passwd', 'passwd'),
    ('a/b/c.tar.gz', 'c.tar.gz'),
    ('a\x00b.pdf', 'a_b.pdf'),
    ('a\nb\r.pdf', 'a_b_.pdf'),
    ('a:b*c?d"e<f>g|h.pdf', 'a_b_c_d_e_f_g_h.pdf'),
    ('<img src=x onerror=alert(1)>.pdf', '_img src_x onerror_alert(1)_.pdf'),
    ('...', 'Beleg'), ('..', 'Beleg'), ('.', 'Beleg'), ('', 'Beleg'), (None, 'Beleg'), ('   ', 'Beleg'), ('. .', 'Beleg'),
    ('.hidden', 'hidden'), ('  mit Rand .pdf  ', 'mit Rand .pdf'), ('endet.', 'endet'),
    ('x' * 500, 'x' * 120),
    ('x' * 500 + '.pdf', 'x' * 116 + '.pdf'),                          # die Endung bleibt
    ('x' * 200 + '.' + 'e' * 50, 'x' * 100 + '.' + 'e' * 19),            # eine lange Endung wird gekürzt
    ('NUL.pdf', '_NUL.pdf'), ('con', '_con'), ('Com1.txt', '_Com1.txt'), ('LPT9', '_LPT9'), ('aux.tar.gz', '_aux.tar.gz'),
    ('nul .pdf', '_nul .pdf'), ('CONSOLE.pdf', 'CONSOLE.pdf'), ('COM10.pdf', 'COM10.pdf'), ('Anul.pdf', 'Anul.pdf'),
])
def test_the_stored_file_name_is_a_plain_safe_name(original, expected):
    assert receipt_files.safe_receipt_name(original) == expected
    name = receipt_files.safe_receipt_name(original)
    assert name == os.path.basename(name) and '/' not in name and '\\' not in name and '\0' not in name
    assert not name.startswith(('.', ' ')) and not name.endswith(('.', ' ')) and 0 < len(name) <= 120


@pytest.mark.parametrize('original, expected', [
    ('rechnung.pdf', 'rechnung.pdf'), ('../../x.pdf', 'x.pdf'), ('C:\\a\\b.pdf', 'b.pdf'), ('a\x00b\n.pdf', 'ab.pdf'),
    ('   ', 'Beleg'), ('', 'Beleg'), ('/', 'Beleg'), ('<b>x.pdf', '<b>x.pdf'), ('y' * 400, 'y' * 255),
])
def test_the_shown_name_is_the_name_without_path_and_control_characters(original, expected):
    assert receipt_files.display_name(original) == expected


@pytest.mark.parametrize('original, expected', [
    ('re‮gnp.pdf', 'regnp.pdf'),                       # Richtungsumkehr (Cf): „gnp.pdf“ erschiene als „pdf.png“
    ('a​b.pdf', 'ab.pdf'),                              # unsichtbares Leerzeichen der Breite null (Cf)
    ('a⁦b⁩.pdf', 'ab.pdf'),                        # Richtungsisolate (Cf)
    ('a\u0085b\u009f.pdf', 'ab.pdf'),                        # C1-Steuerzeichen (Cc)
    ('‮​', 'Beleg'),                               # nichts Sichtbares bleibt
    ('Büro Rechnung ß (2).pdf', 'Büro Rechnung ß (2).pdf'),  # sichtbare Zeichen bleiben
])
def test_the_shown_name_loses_invisible_format_and_c1_control_characters(original, expected):
    assert receipt_files.display_name(original) == expected


def test_a_name_that_reverses_its_direction_is_shown_and_stored_without_the_reversal(server_port, circle):
    (entry,) = created(upload(server_port, circle, ('re‮gnp.pdf', plain_pdf())))
    assert entry['filename'] == 'regnp.pdf'
    (row,) = circle.db.fetch_receipts()
    assert row[3] == 'regnp.pdf' and '‮' not in row[3] + row[4]
    assert '‮' not in get(server_port, '/receipts').text


@pytest.mark.parametrize('number, original, expected', [
    ('26B001', 'rechnung.pdf', '26B001_rechnung.pdf'),
    ('', 'rechnung.pdf', 'rechnung.pdf'), (None, 'rechnung.pdf', 'rechnung.pdf'),
    ('ER/77', 'x.pdf', 'ER_77_x.pdf'),                          # die Nummer geht durch dieselbe Bereinigung wie der Name
    ('...', 'x.pdf', 'x.pdf'),                                   # von der Nummer bleibt nichts: kein Unterstrich allein davor
    ('N' * 500, 'x.pdf', 'N' * 60 + '_x.pdf'),                   # die Nummer ist höchstens 60 Bytes lang
    ('Ä' * 500, 'x.pdf', 'Ä' * 30 + '_x.pdf'),                   # in Bytes, nicht in Zeichen
    ('26B001', 'con.pdf', '26B001__con.pdf'),                    # Name eines Windows-Geräts: wie bisher
    # der ganze Name ist höchstens 200 Bytes lang: gekürzt wird der Stamm, die Endung bleibt, nie mitten in einem Zeichen
    ('Z' * 41, 'ä' * 300 + '.pdf', 'Z' * 41 + '_' + 'ä' * 77 + '.pdf'),     # 42 + 154 + 4 = 200 Bytes
    ('Z' * 40, 'ä' * 300 + '.pdf', 'Z' * 40 + '_' + 'ä' * 77 + '.pdf'),     # ein Byte bleibt frei: die nächste ``ä`` passte nicht mehr ganz
    ('', 'ä' * 300 + '.pdf', 'ä' * 98 + '.pdf'),                            # 196 + 4 = 200 Bytes
    ('26B001', 'x' * 400 + '.pdf', '26B001_' + 'x' * 116 + '.pdf'),         # bis zu 200 Bytes bleibt es bei den 120 Zeichen
    ('26B001', '  mit Rand .pdf  ', '26B001_mit Rand .pdf'),                 # ein Leerzeichen vor der Endung bleibt, solange nichts gekürzt wird
    ('', 'ä' * 97 + ' ' + 'ä' * 10 + '.pdf', 'ä' * 97 + '.pdf'),             # der Schnitt fällt hinter das Leerzeichen: es geht mit
])
def test_the_stored_name_joins_number_and_name_within_the_byte_limit(number, original, expected):
    stored = receipt_files.stored_file_name(number, original)
    assert stored == expected and len(stored.encode('utf-8')) <= receipt_files.NAME_BYTES == 200


@pytest.mark.parametrize('length', range(0, 70, 7))
def test_the_stored_name_never_exceeds_200_bytes_whatever_the_length_of_the_number(length):
    for original in ('ä' * 300 + '.pdf', 'ü' * 119 + 'x.pdf', 'x' * 400, 'ä' * 150 + '.' + 'ö' * 19):
        stored = receipt_files.stored_file_name('Ä' * length, original)
        assert len(stored.encode('utf-8')) <= 200 and '�' not in stored
        assert stored.endswith(os.path.splitext(receipt_files.safe_receipt_name(original))[1])        # die Endung bleibt


@pytest.mark.parametrize('number, original, expected', [
    ('', 'Konten', '_Konten'), ('', 'KONTEN', '_KONTEN'), ('', 'konten', '_konten'), ('', 'konten.', '_konten'),
    ('', 'general', '_general'), ('', 'GENERAL', '_GENERAL'), ('', 'General', '_General'),
    ('', 'Konten.pdf', 'Konten.pdf'), ('', 'Kontenplan', 'Kontenplan'), ('', 'general.txt', 'general.txt'),      # nur der ganze Name
    ('26B001', 'Konten', '26B001_Konten'), ('26B001', 'general', '26B001_general'),                              # mit Nummer heißt nichts so
])
def test_a_name_like_a_folder_of_the_statement_import_gets_an_underscore(number, original, expected):
    assert receipt_files.stored_file_name(number, original) == expected


def test_a_file_named_like_an_import_folder_is_stored_with_an_underscore_and_leaves_the_folder_free(server_port, auto_login):
    """Der Kontoauszug-Import legt unter ``Belege/<Jahr>/`` die Ordner ``Konten/<Bank>/`` und ``general/`` an; eine Datei, die ohne Nummer so heißt,
    nähme ihnen den Namen (die Nummer fehlt hier, weil die Vorlage des Kreises keinen Zähler hat)."""
    auto_login.db.insert_number_range('receipt_company', 2026, 'B', number_format='{yy}{l}')
    entries = created(upload(server_port, auto_login, ('Konten', b'a'), ('GENERAL', b'b'), ('Konten.pdf', b'c')))
    assert [e['status'] for e in entries] == ['created'] * 3
    assert files_in(belege()) == ['2026/Konten.pdf', '2026/_GENERAL', '2026/_Konten']
    assert [row[4] for row in auto_login.db.fetch_receipts()] == ['Belege/2026/_Konten', 'Belege/2026/_GENERAL', 'Belege/2026/Konten.pdf']
    os.makedirs(belege('2026', 'Konten', 'VBR'))                                          # die Ordner des Imports lassen sich anlegen
    os.makedirs(belege('2026', 'general'))


def test_a_long_name_of_two_byte_characters_gets_a_stored_name_of_at_most_200_bytes(server_port, auto_login):
    """150 × „ä“ (300 Bytes) mit einem Kreis, dessen Zusatz 35 Zeichen lang ist: Der Upload gelingt, der abgelegte Name hat höchstens 200 Bytes
    und endet auf ``.pdf``. (Ein Dateiname darf höchstens 255 Bytes haben; ``_2`` … ``_1000`` kommen noch dazu.)"""
    auto_login.db.insert_number_range('receipt_company', 2026, 'B', prefix='_' + 'Z' * 34, current_number=0)
    content = plain_pdf()
    (entry,) = created(upload(server_port, auto_login, ('ä' * 150 + '.pdf', content)))
    assert entry['status'] == 'created' and entry['number'] == '26B001_' + 'Z' * 34
    (stored,) = [name.split('/', 1)[1] for name in files_in(belege())]
    assert len(stored.encode('utf-8')) <= 200 and stored.endswith('.pdf') and stored.startswith(entry['number'] + '_')
    assert read(belege('2026', stored)) == content
    assert auto_login.db.fetch_receipts()[0][4] == f'Belege/2026/{stored}'
    assert auto_login.db.fetch_receipts()[0][3] == 'ä' * 150 + '.pdf'                       # der angezeigte Name bleibt ganz


@pytest.mark.parametrize('name, stored', [
    pytest.param('../../x.pdf', '26B001_x.pdf', id='aufwaerts'),
    pytest.param('C:\\a\\..\\b.pdf', '26B001_b.pdf', id='Windows-Pfad'),
    pytest.param('/etc/cron.d/job', '26B001_job', id='absolut'),
    pytest.param('x' * 500 + '.pdf', '26B001_' + 'x' * 116 + '.pdf', id='500-Zeichen'),
    pytest.param('...', '26B001_Beleg', id='nur-Punkte'),
    pytest.param('a\x00b.pdf', '26B001_a_b.pdf', id='NUL'),
    pytest.param('con.pdf', '26B001__con.pdf', id='Windows-Gerätename'),
])
def test_a_hostile_name_stays_inside_the_folder_and_is_cleaned(server_port, circle, name, stored):
    tmp_root = os.path.dirname(userctx.DATA_ROOT)
    before = installation.tree(tmp_root)
    content = plain_pdf()
    (entry,) = created(upload(server_port, circle, (name, content)))
    assert entry['status'] == 'created' and entry['number'] == '26B001'
    assert files_in(belege()) == [f'2026/{stored}'] and read(belege('2026', stored)) == content
    (row,) = circle.db.fetch_receipts()
    assert row[4] == f'Belege/2026/{stored}' and '\x00' not in row[3] and '\\' not in row[3] and '/' not in row[3]
    after = installation.tree(tmp_root)
    # außer Datenbank, Protokollen und den Ordnern ist unter dem Testordner nichts entstanden als diese eine Datei
    new_files = {path for path, digest in after.items() if digest is not None and path not in before}
    assert [p for p in new_files if not p.endswith(('.db', '.log', '.sql', '-journal'))] == [
        os.path.join('data', 'users', installation.ADMIN, 'Belege', '2026', stored)]


def test_raw_backslashes_as_browsers_send_them_stay_inside_the_folder_too(server_port, circle):
    (entry,) = created(upload(server_port, circle, ('C:\\Users\\x\\..\\beleg.pdf', plain_pdf()), escape=False))
    assert entry['filename'] == 'C:Usersx..beleg.pdf'                     # der Parser entfernt die Rückstriche (Quoted-Pair)
    assert files_in(belege()) == ['2026/26B001_C_Usersx..beleg.pdf']


def test_a_file_name_of_spaces_only_is_no_file_like_an_empty_file_field(server_port, circle):
    response = upload(server_port, circle, ('   ', b'Inhalt'))
    assert response.status == 400 and json.loads(response.text)['error'] == 'Ungültige Anfrage.'
    assert circle.db.fetch_receipts() == []


def test_a_hostile_name_is_text_in_the_list_never_markup(server_port, circle):
    name = '<img src=x onerror=alert(1)>.pdf'
    (entry,) = created(upload(server_port, circle, (name, b'Inhalt')))
    assert entry['filename'] == name                               # die Antwort ist JSON-Daten, die Seite setzt nur textContent
    page = get(server_port, '/receipts').text
    assert '<img src=x' not in page and Dom(page).all('img') == []
    assert '&lt;img src=x onerror=alert(1)&gt;.pdf' in page
    row = next(a for a in Dom(page).all('tr') if 'receipt-row' in a.get('class', '').split())
    assert row['data-filename'] == name


def test_an_existing_file_is_never_overwritten_the_new_one_gets_a_suffix(server_port, circle):
    os.makedirs(belege('2026'))
    with open(belege('2026', '26B001_beleg.pdf'), 'wb') as handle:
        handle.write(b'alt')
    (entry,) = created(upload(server_port, circle, ('beleg.pdf', b'neu')))
    assert entry['number'] == '26B001'
    assert read(belege('2026', '26B001_beleg.pdf')) == b'alt' and read(belege('2026', '26B001_beleg_2.pdf')) == b'neu'
    assert circle.db.fetch_receipts()[0][4] == 'Belege/2026/26B001_beleg_2.pdf'
    circle.db.update_number_range(circle.db.fetch_number_ranges('receipt_company')[0][0], 2026, 'B', current_number=0)
    (entry,) = created(upload(server_port, circle, ('beleg.pdf', b'dritte')))                       # derselbe Name noch einmal
    assert read(belege('2026', '26B001_beleg_3.pdf')) == b'dritte' and read(belege('2026', '26B001_beleg.pdf')) == b'alt'


def test_a_link_at_the_target_name_is_never_followed(tmp_path):
    folder = tmp_path / 'ziel'
    folder.mkdir()
    outside = tmp_path / 'draussen.txt'
    try:
        os.symlink(outside, folder / 'a.pdf')                                      # zeigt ins Leere
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'das Dateisystem kann keine symbolischen Links anlegen ({exc})')
    path, name = receipt_files.reserve_name(str(folder), 'a.pdf')
    assert name == 'a_2.pdf' and read(path) == b'' and not outside.exists()          # der belegte Platz ist eine leere Datei


def test_a_directory_at_the_target_name_counts_as_taken_too(tmp_path):
    (tmp_path / 'a.pdf').mkdir()
    path, name = receipt_files.reserve_name(str(tmp_path), 'a.pdf')
    assert name == 'a_2.pdf' and os.path.isfile(path)


def test_the_name_search_gives_up_after_a_fixed_number_of_tries(tmp_path, monkeypatch):
    monkeypatch.setattr(receipt_files, 'MAX_NAME_TRIES', 3)
    for name in ('a.pdf', 'a_2.pdf', 'a_3.pdf'):
        (tmp_path / name).write_bytes(b'vorhanden')
    with pytest.raises(FileExistsError):
        receipt_files.reserve_name(str(tmp_path), 'a.pdf')
    assert sorted(os.listdir(tmp_path)) == ['a.pdf', 'a_2.pdf', 'a_3.pdf'] and read(tmp_path / 'a.pdf') == b'vorhanden'


def test_a_write_that_fails_leaves_no_partial_file(tmp_path):
    with pytest.raises(TypeError):
        receipt_files.write_part(str(tmp_path), 'kein Bytes-Objekt')                  # ``write`` scheitert nach dem Anlegen
    assert os.listdir(tmp_path) == []


def test_a_part_file_has_a_random_name_ends_in_part_and_holds_the_content(tmp_path):
    first = receipt_files.write_part(str(tmp_path), b'Inhalt')
    second = receipt_files.write_part(str(tmp_path), b'Inhalt')
    names = sorted(os.listdir(tmp_path))
    assert first != second and len(names) == 2 and all(re.fullmatch(r'[0-9a-f]{32}\.part', name) for name in names)
    assert read(first) == read(second) == b'Inhalt'
    if os.name == 'posix':
        assert os.stat(first).st_mode & 0o137 == 0                                # nie ausführbar, nie von der Gruppe beschreibbar, nie für andere


# ── Nichts halb: Eintrag, Datei und Zähler gehören zusammen ──────────────────
class InsertFails(sqlite3.Connection):
    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith('INSERT INTO DOCUMENTS'):
            raise sqlite3.OperationalError('simulierter Fehler bei ' + SECRET)
        return super().execute(sql, *args)


def test_a_failing_insert_leaves_no_file_no_entry_and_no_used_number(server_port, circle, monkeypatch, capsys):
    def connect(self):
        conn = sqlite3.connect(self.db_name, factory=InsertFails)
        conn.execute('PRAGMA foreign_keys = ON')
        return conn
    original = Database._get_connection
    monkeypatch.setattr(Database, '_get_connection', connect)
    response = upload(server_port, circle, ('rechnung.pdf', plain_pdf()))
    assert created(response) == [{'filename': 'rechnung.pdf', 'status': 'error', 'error': RECEIPT_UPLOAD_FAILED}]
    for text in (SECRET, 'simulierter', 'Errno', userctx.DATA_ROOT):
        assert text not in response.text, text                          # kein Ausnahmetext, kein Pfad in der Antwort
    log = capsys.readouterr().err
    assert 'Traceback' in log and SECRET in log                         # der volle Fehler steht im Server-Log
    monkeypatch.setattr(Database, '_get_connection', original)
    assert circle.db.fetch_receipts() == [] and counter(circle.db) == 0
    assert files_in(belege()) == []                                      # die eben angelegte Datei ist wieder weg
    (entry,) = created(upload(server_port, circle, ('rechnung.pdf', plain_pdf())))
    assert entry['number'] == '26B001'                                  # der Zähler war nicht verbraucht


def test_a_failing_write_leaves_no_entry_and_no_used_number(server_port, circle, monkeypatch, capsys):
    def refuse(folder, content):
        raise OSError(13, 'Permission denied', SECRET)
    monkeypatch.setattr(receipt_files, 'write_part', refuse)
    response = upload(server_port, circle, ('rechnung.pdf', plain_pdf()), ('zweite.pdf', b'x'))
    assert [(e['filename'], e['status'], e['error']) for e in created(response)] == [
        ('rechnung.pdf', 'error', RECEIPT_UPLOAD_FAILED), ('zweite.pdf', 'error', RECEIPT_UPLOAD_FAILED)]
    assert SECRET not in response.text and 'Permission' not in response.text
    assert SECRET in capsys.readouterr().err
    assert circle.db.fetch_receipts() == [] and counter(circle.db) == 0 and files_in(belege()) == []


def break_document_inserts(monkeypatch):
    """Jede Verbindung der Datenbank wirft beim ``INSERT INTO Documents`` (``InsertFails``); der Rückgabewert macht sie wieder normal."""
    original = Database._get_connection

    def connect(self):
        conn = sqlite3.connect(self.db_name, factory=InsertFails)
        conn.execute('PRAGMA foreign_keys = ON')
        return conn
    monkeypatch.setattr(Database, '_get_connection', connect)
    return lambda: monkeypatch.setattr(Database, '_get_connection', original)


def test_a_failing_insert_takes_the_circle_it_had_created_back_too(server_port, auto_login, monkeypatch):
    """Ohne Kreis legt die Nummer einen an – in derselben Transaktion: Scheitert der Eintrag danach, gibt es auch den Kreis nicht."""
    restore = break_document_inserts(monkeypatch)
    response = upload(server_port, auto_login, ('rechnung.pdf', plain_pdf()))
    assert created(response) == [{'filename': 'rechnung.pdf', 'status': 'error', 'error': RECEIPT_UPLOAD_FAILED}]
    restore()
    assert auto_login.db.fetch_number_ranges('receipt_company') == [] and auto_login.db.fetch_receipts() == []
    assert files_in(belege()) == []
    (entry,) = created(upload(server_port, auto_login, ('rechnung.pdf', plain_pdf())))
    assert entry['number'] == '26B001' and counter(auto_login.db) == 1


def test_a_failing_insert_never_removes_or_changes_a_foreign_file_of_the_same_name(server_port, circle, monkeypatch):
    os.makedirs(belege('2026'))
    with open(belege('2026', '26B001_a.pdf'), 'wb') as handle:
        handle.write(b'fremd')
    break_document_inserts(monkeypatch)
    assert created(upload(server_port, circle, ('a.pdf', b'neu')))[0]['status'] == 'error'
    assert files_in(belege()) == ['2026/26B001_a.pdf'] and read(belege('2026', '26B001_a.pdf')) == b'fremd'     # die neue Datei ist weg, die fremde nicht
    assert counter(circle.db) == 0


def test_the_file_is_written_before_the_transaction_and_the_name_is_taken_inside_it(tmp_db, monkeypatch):
    """Das Schreiben von bis zu 200 MB hielt die Schreibsperre der Datenbank – andere Schreiber scheiterten solange. Jetzt entsteht die Teildatei
    VOR der Transaktion; erst Nummer und Name werden unter der Sperre vergeben."""
    tmp_db.insert_number_range('receipt_company', 2026, 'B', current_number=0)
    probes = []

    def other_writer():
        other = sqlite3.connect(tmp_db.db_name, timeout=0)                  # wartet nicht: gesperrt heißt sofort ein Fehler
        try:
            other.execute('BEGIN IMMEDIATE')
            other.rollback()
            return 'frei'
        except sqlite3.OperationalError:
            return 'gesperrt'
        finally:
            other.close()

    real_write, real_reserve = receipt_files.write_part, receipt_files.reserve_name

    def write(folder, content):
        probes.append(('Teildatei', other_writer()))
        return real_write(folder, content)

    def reserve(folder, name):
        probes.append(('Name', other_writer()))
        return real_reserve(folder, name)
    monkeypatch.setattr(receipt_files, 'write_part', write)
    monkeypatch.setattr(receipt_files, 'reserve_name', reserve)
    tmp_db.insert_receipt_with_file(DAY, 'a.pdf', '', b'Inhalt')
    assert probes == [('Teildatei', 'frei'), ('Name', 'gesperrt')]


class CommitFails(sqlite3.Connection):
    def commit(self):
        raise sqlite3.OperationalError('simulierter Fehler beim COMMIT bei ' + SECRET)


class CommitAndRollbackFail(CommitFails):
    def rollback(self):
        raise sqlite3.OperationalError('simulierter Fehler beim Zurückrollen')


@pytest.mark.parametrize('factory', [CommitFails, CommitAndRollbackFail])
def test_a_failing_commit_leaves_no_file_no_part_and_no_used_number_even_when_the_rollback_fails_too(tmp_db, monkeypatch, capsys, factory):
    """Zuerst räumt der Fehlerweg die Dateien weg, dann rollt er zurück – ein scheiterndes Zurückrollen verhindert das Aufräumen nicht und verdeckt
    die ursprüngliche Ausnahme nicht."""
    tmp_db.insert_number_range('receipt_company', 2026, 'B', current_number=0)

    def connect(self):
        conn = sqlite3.connect(self.db_name, factory=factory)
        conn.execute('PRAGMA foreign_keys = ON')
        return conn
    with monkeypatch.context() as patch:
        patch.setattr(Database, '_get_connection', connect)
        with pytest.raises(sqlite3.OperationalError, match='beim COMMIT'):         # die Ausnahme des COMMIT, nicht die des Zurückrollens
            tmp_db.insert_receipt_with_file(DAY, 'a.pdf', 'Info', b'Inhalt')
    assert os.listdir(os.path.join(userctx.DATA_ROOT, 'Belege', '2026')) == []         # weder die Datei noch die Teildatei
    assert tmp_db.fetch_receipts() == [] and tmp_db.fetch_number_ranges('receipt_company')[0][5] == 0
    if factory is CommitAndRollbackFail:
        assert 'beim Zurückrollen' in capsys.readouterr().err                          # im Log, nicht in der Ausnahme


def test_a_failing_replace_leaves_no_file_no_part_and_no_used_number(tmp_db, monkeypatch):
    tmp_db.insert_number_range('receipt_company', 2026, 'B', current_number=0)

    def refuse(source, target):
        raise PermissionError(13, 'Permission denied (Test)', str(target))
    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', refuse)
        with pytest.raises(PermissionError):
            tmp_db.insert_receipt_with_file(DAY, 'a.pdf', '', b'Inhalt')
    assert os.listdir(os.path.join(userctx.DATA_ROOT, 'Belege', '2026')) == []         # auch der leere Platz ist weg
    assert tmp_db.fetch_receipts() == [] and tmp_db.fetch_number_ranges('receipt_company')[0][5] == 0


def test_when_no_free_name_is_left_nothing_is_created_a_foreign_file_stays_and_no_number_is_used(tmp_db, monkeypatch):
    tmp_db.insert_number_range('receipt_company', 2026, 'B', current_number=0)
    folder = os.path.join(userctx.DATA_ROOT, 'Belege', '2026')
    os.makedirs(folder)
    with open(os.path.join(folder, '26B001_a.pdf'), 'wb') as handle:
        handle.write(b'alt')
    monkeypatch.setattr(receipt_files, 'MAX_NAME_TRIES', 1)
    with pytest.raises(FileExistsError):
        tmp_db.insert_receipt_with_file(DAY, 'a.pdf', '', b'neu')
    assert os.listdir(folder) == ['26B001_a.pdf'] and read(os.path.join(folder, '26B001_a.pdf')) == b'alt'
    assert tmp_db.fetch_receipts() == [] and tmp_db.fetch_number_ranges('receipt_company')[0][5] == 0


def test_a_link_at_the_target_name_is_not_followed_by_the_upload_either(server_port, circle, tmp_path):
    outside = tmp_path / 'draussen.txt'
    outside.write_bytes(b'vorher')
    os.makedirs(belege('2026'))
    make_symlink(outside, belege('2026', '26B001_a.pdf'))
    (entry,) = created(upload(server_port, circle, ('a.pdf', b'neu')))
    assert entry['status'] == 'created' and outside.read_bytes() == b'vorher'               # nichts außerhalb wurde überschrieben
    assert read(belege('2026', '26B001_a_2.pdf')) == b'neu'
    assert circle.db.fetch_receipts()[0][4] == 'Belege/2026/26B001_a_2.pdf'


def test_a_year_folder_that_is_a_link_to_the_outside_takes_no_file_and_uses_no_number(server_port, circle, tmp_path):
    outside = tmp_path / 'fremd'
    outside.mkdir()
    os.makedirs(belege())
    make_symlink(outside, belege('2026'))
    response = upload(server_port, circle, ('rechnung.pdf', plain_pdf()), ('zweite.pdf', b'x'))
    assert created(response) == [{'filename': 'rechnung.pdf', 'status': 'error', 'error': RECEIPT_UPLOAD_FAILED},
                                 {'filename': 'zweite.pdf', 'status': 'error', 'error': RECEIPT_UPLOAD_FAILED}]
    assert str(outside) not in response.text and 'Link' not in response.text                # kein Pfad und kein Ausnahmetext in der Antwort
    assert os.listdir(outside) == []                                                       # außerhalb entsteht nichts, auch keine Teildatei
    assert circle.db.fetch_receipts() == [] and counter(circle.db) == 0


def test_a_year_folder_that_is_a_link_inside_belege_is_refused_too(server_port, circle):
    os.makedirs(belege('2025'))
    make_symlink(belege('2025'), belege('2026'))
    assert created(upload(server_port, circle, ('rechnung.pdf', plain_pdf())))[0]['status'] == 'error'
    assert os.listdir(belege('2025')) == [] and counter(circle.db) == 0


def test_the_folder_belege_itself_may_be_a_link_and_the_upload_follows_it(server_port, circle, tmp_path):
    """Belege auf einem NAS (DEPLOYMENT.md): ``Belege/`` ist ein Link, der Jahresordner darin ein echter Ordner."""
    nas = tmp_path / 'nas'
    nas.mkdir()
    os.makedirs(admin_dir(), exist_ok=True)
    make_symlink(nas, belege())
    (entry,) = created(upload(server_port, circle, ('rechnung.pdf', plain_pdf())))
    assert entry['status'] == 'created' and read(nas / '2026' / '26B001_rechnung.pdf') == plain_pdf()


def test_the_database_method_is_all_or_nothing(tmp_db):
    tmp_db.insert_number_range('receipt_company', 2026, 'B', current_number=0)
    with pytest.raises(TypeError):
        tmp_db.insert_receipt_with_file(DAY, 'a.pdf', '', 'kein Bytes-Objekt')
    assert tmp_db.fetch_receipts() == [] and tmp_db.fetch_number_ranges('receipt_company')[0][5] == 0
    folder = os.path.join(userctx.DATA_ROOT, 'Belege', '2026')
    assert os.listdir(folder) == []                                      # die Teildatei ist weg
    receipt_id, number, relative = tmp_db.insert_receipt_with_file(DAY, 'a.pdf', 'Info', b'Inhalt')
    assert (number, relative) == ('26B001', 'Belege/2026/26B001_a.pdf')
    assert tmp_db.fetch_receipts() == [(receipt_id, '26B001', DAY, 'a.pdf', 'Belege/2026/26B001_a.pdf', 'Info')]


def test_an_empty_file_is_an_error_of_its_row_and_the_others_are_created(server_port, circle):
    entries = created(upload(server_port, circle, ('leer.pdf', b''), ('voll.pdf', b'Inhalt')))
    assert entries[0] == {'filename': 'leer.pdf', 'status': 'error', 'error': RECEIPT_EMPTY_FILE}
    assert RECEIPT_EMPTY_FILE == 'Die Datei ist leer.'
    assert (entries[1]['status'], entries[1]['number']) == ('created', '26B001')                   # die Nummer der leeren ist nicht verbraucht
    assert [row[3] for row in circle.db.fetch_receipts()] == ['voll.pdf'] and counter(circle.db) == 1


def test_more_than_the_limit_of_files_is_refused_and_nothing_is_created(server_port, circle):
    response = upload(server_port, circle, *[(f'd{n}.txt', b'x') for n in range(MAX_RECEIPT_FILES + 1)])
    assert response.status == 400 and json.loads(response.text) == {'success': False, 'error': RECEIPT_TOO_MANY_FILES}
    assert RECEIPT_TOO_MANY_FILES == 'Höchstens 100 Dateien je Upload.' and MAX_RECEIPT_FILES == 100
    assert circle.db.fetch_receipts() == [] and counter(circle.db) == 0 and not os.path.isdir(belege())
    entries = created(upload(server_port, circle, *[(f'd{n}.txt', b'x') for n in range(MAX_RECEIPT_FILES)]))
    assert len(entries) == 100 and {e['status'] for e in entries} == {'created'} and counter(circle.db) == 100   # genau die Grenze geht


@pytest.mark.parametrize('headers, body', [
    pytest.param({'Content-Type': 'text/plain'}, b'keine Datei', id='kein-Multipart'),
    pytest.param({'Content-Type': 'application/json'}, b'{"files": []}', id='JSON'),
    pytest.param({'Content-Type': 'multipart/form-data; boundary=X'}, b'--X\r\nContent-Disposition: form-data; name="a"\r\n\r\nWert\r\n--X--\r\n',
                 id='Multipart-ohne-Datei'),
    pytest.param({'Content-Type': 'multipart/form-data; boundary=X'}, b'', id='leerer-Body'),
])
def test_a_request_without_a_file_is_400_and_creates_nothing(server_port, circle, headers, body):
    response = send(server_port, 'POST', '/receipts/upload', {'Origin': own_origin(server_port),
                                                              'X-CSRF-Token': circle.csrf, **headers}, body)
    assert response.status == 400 and json.loads(response.text) == {'success': False, 'error': 'Ungültige Anfrage.'}
    assert circle.db.fetch_receipts() == [] and counter(circle.db) == 0


# ── Die Route: Token, Herkunft, Größe, Methode, Demo ─────────────────────────
@pytest.mark.parametrize('headers, status, text', [
    pytest.param({'X-CSRF-Token': None}, 403, 'Sicherheits-Token', id='Token-fehlt'),
    pytest.param({'X-CSRF-Token': 'falsch'}, 403, 'Sicherheits-Token', id='Token-falsch'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, 403, 'fremder Herkunft', id='fremder-Origin'),
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, 403, 'fremder Herkunft', id='cross-site'),
    pytest.param({'Sec-Fetch-Site': 'same-site'}, 403, 'fremder Herkunft', id='same-site'),
])
def test_a_request_without_token_or_from_a_foreign_page_is_403_and_creates_nothing(server_port, circle, headers, status, text):
    content_type, body = multipart([('rechnung.pdf', plain_pdf())])
    sent = {'Origin': own_origin(server_port), 'X-CSRF-Token': circle.csrf, 'Content-Type': content_type, **headers}
    sent = {name: value for name, value in sent.items() if value is not None}
    response = send(server_port, 'POST', '/receipts/upload', sent, body)
    assert response.status == status and text in response.text
    assert circle.db.fetch_receipts() == [] and counter(circle.db) == 0 and not os.path.isdir(belege())


def test_a_body_over_the_limit_is_413_without_being_read_and_creates_nothing(server_port, circle):
    status, resp, body = exchange(server_port, '/receipts/upload',
                                  {'Content-Type': 'multipart/form-data; boundary=X', 'X-CSRF-Token': circle.csrf,
                                   'Content-Length': str(MAX_UPLOAD_BODY + 1)})
    assert status == 413 and json.loads(body)['success'] is False
    assert json.loads(body)['error'] == 'Die Anfrage ist zu groß (höchstens 200 MB).'
    assert_closed(resp)
    assert circle.db.fetch_receipts() == [] and get(server_port, '/about').status == 200


def test_get_is_405(server_port, circle):
    response = get(server_port, '/receipts/upload')
    assert response.status == 405 and response.headers['Allow'] == 'POST'


def demo_site(monkeypatch):
    """Nur Demodaten, Variable gesetzt: Jeder Besucher arbeitet als ``demo``."""
    monkeypatch.setenv(demo.ENV_VAR, '1')
    userctx.set_mode('multi')
    auth.create_user('demo', installation.PASSWORD, is_admin=False, allow_reserved=True)
    installation.open_db('demo')
    with open(os.path.join(userctx.DATA_ROOT, 'users', 'demo', demo.MARKER_FILE), 'w', encoding='utf-8') as handle:
        handle.write('x')
    return auth.csrf_for('auto-login:demo')


def test_in_demo_mode_the_upload_is_403_before_anything_is_read_and_nothing_is_created(server_port, monkeypatch):
    csrf = demo_site(monkeypatch)
    before = installation.tree(userctx.DATA_ROOT)
    status, resp, body = exchange(server_port, '/receipts/upload',
                                  {'Content-Type': 'multipart/form-data; boundary=X', 'X-CSRF-Token': csrf,
                                   'Origin': own_origin(server_port), 'Content-Length': '5000000'})   # der Body kommt nie an
    assert status == 403 and json.loads(body) == {'success': False, 'error': RECEIPTS_DEMO_REFUSED}
    assert RECEIPTS_DEMO_REFUSED == 'Im Demo-Modus ist das Hochladen von Belegen abgeschaltet.'
    assert_closed(resp)
    assert installation.tree(userctx.DATA_ROOT) == before                    # keine Datei, kein Eintrag, kein Ordner
    # und mit vollständigem Body ebenso
    content_type, payload = multipart([('rechnung.pdf', plain_pdf())])
    response = send(server_port, 'POST', '/receipts/upload', {'Origin': own_origin(server_port), 'X-CSRF-Token': csrf,
                                                              'Content-Type': content_type}, payload)
    assert response.status == 403 and json.loads(response.text)['error'] == RECEIPTS_DEMO_REFUSED
    assert installation.tree(userctx.DATA_ROOT) == before


def test_in_demo_mode_the_page_shows_the_sentence_instead_of_the_drop_zone(server_port, monkeypatch):
    demo_site(monkeypatch)
    page = get(server_port, '/receipts').text
    assert RECEIPTS_DEMO_REFUSED in page
    assert 'id="dropZone"' not in page and 'id="fileInput"' not in page and '/receipts/upload' not in page
    assert 'uploadFiles' not in page
    assert 'id="receiptForm"' in page                                       # das Formular für Papierbelege bleibt


def test_outside_the_demo_the_page_posts_to_the_new_route_and_takes_every_kind_of_file(server_port, auto_login):
    page = get(server_port, '/receipts').text
    assert RECEIPTS_DEMO_REFUSED not in page and "fetch('/receipts/upload'" in page and '/upload_receipts' not in page
    field = Dom(page).one('input', id='fileInput')
    assert 'accept' not in field and 'multiple' in field                      # jede Datei, mehrere auf einmal


# ── /upload_receipts bleibt der Import der Seite „Buchungen“ ─────────────────
def test_the_import_of_the_transactions_page_is_unchanged_and_creates_no_receipt(server_port, circle):
    content_type, body = multipart([('rechnung.pdf', plain_pdf())])
    response = send(server_port, 'POST', '/upload_receipts', {'Origin': own_origin(server_port),
                                                              'X-CSRF-Token': circle.csrf, 'Content-Type': content_type}, body)
    assert response.status == 200
    data = json.loads(response.text)
    assert set(data) == {'import_id', 'files', 'other_files'} and data['files'] == []
    assert [entry['status'] for entry in data['other_files']] == ['organized']
    assert circle.db.fetch_receipts() == [] and counter(circle.db) == 0                 # kein Beleg, kein Zähler
    assert read(os.path.join(admin_dir(), 'Documents', 'rechnung.pdf')) and files_in(belege()) == []       # wie bisher: Documents/


# ── Das Formular „Beleg hinzufügen“ (Papierbeleg) ────────────────────────────
def form_values(page):
    dom = Dom(page)
    return dom.one('input', id='editNumber')['value'], dom.one('input', name='number_suggestion')['value']


def add_receipt(port, login, **fields):
    form = {'number': '', 'number_suggestion': '', 'date': DAY, 'filename': 'papier.pdf', 'info': '', **fields}
    return post(port, '/add_receipt', headers={'Origin': own_origin(port)}, form=form, csrf=login.csrf)


def test_the_form_takes_the_proposed_number_and_uses_it_up(server_port, circle):
    number, suggestion = form_values(get(server_port, '/receipts').text)
    assert number == suggestion == '26B001'
    response = add_receipt(server_port, circle, number=number, number_suggestion=suggestion)
    assert response.status == 303 and response.headers['Location'] == '/receipts'
    (row,) = circle.db.fetch_receipts()
    assert row[1:] == ('26B001', DAY, 'papier.pdf', '', '') and counter(circle.db) == 1               # ohne Datei, die Nummer ist verbraucht
    assert form_values(get(server_port, '/receipts').text)[0] == '26B002'                              # der nächste Vorschlag


def test_a_proposal_that_another_window_used_up_is_replaced_by_the_next_number(server_port, circle):
    add_receipt(server_port, circle, number='26B001', number_suggestion='26B001')
    add_receipt(server_port, circle, number='26B001', number_suggestion='26B001', filename='zweites-fenster.pdf')
    assert [row[1] for row in circle.db.fetch_receipts()] == ['26B001', '26B002'] and counter(circle.db) == 2


def test_an_own_number_stays_and_raises_the_counter_but_never_lowers_it(server_port, circle):
    add_receipt(server_port, circle, number='ER-77', number_suggestion='26B001')                       # passt zu keinem Kreis
    assert counter(circle.db) == 0
    add_receipt(server_port, circle, number='26B010', number_suggestion='26B001')                      # passt zur Vorlage: hebt den Zähler
    assert counter(circle.db) == 10
    add_receipt(server_port, circle, number='26B004', number_suggestion='26B011')                      # unter dem Zähler: er bleibt
    assert counter(circle.db) == 10
    assert [row[1] for row in circle.db.fetch_receipts()] == ['ER-77', '26B010', '26B004']


def test_the_form_without_a_circle_stores_a_typed_number_as_typed_and_creates_no_circle(server_port, auto_login):
    add_receipt(server_port, auto_login, number='A-1')
    add_receipt(server_port, auto_login, number='26B001')                                              # auch eine, die zu einer Vorlage passen würde
    assert [row[1] for row in auto_login.db.fetch_receipts()] == ['A-1', '26B001']
    assert auto_login.db.fetch_number_ranges('receipt_company') == []                                   # eine eigene Nummer legt keinen Kreis an


def test_the_form_without_a_number_and_without_any_circle_creates_the_circle_of_the_current_year(server_port, auto_login):
    """Wie beim Upload: Die Nummer, die der Vorschlag nicht kannte (es gibt keinen Kreis), kommt aus einem neuen Kreis des laufenden Jahres."""
    response = add_receipt(server_port, auto_login, number='')
    assert response.status == 303
    assert [row[1] for row in auto_login.db.fetch_receipts()] == ['26B001']
    assert [row[1:] for row in auto_login.db.fetch_number_ranges('receipt_company')] == [
        ('receipt_company', 2026, 'B', '', 1, 'Belegnummern Firma', '{yy}{l}{nnn}{s}')]
    add_receipt(server_port, auto_login, number='')
    assert [row[1] for row in auto_login.db.fetch_receipts()] == ['26B001', '26B002'] and len(auto_login.db.fetch_number_ranges('receipt_company')) == 1


@pytest.mark.parametrize('number', [
    pytest.param('26B' + '9' * 25, id='25-Neunen'),
    pytest.param('26B9223372036854775807', id='groesste-64-Bit-Zahl'),
    pytest.param('26B1000000000', id='eine-ueber-der-Grenze'),
    pytest.param('26B' + '9' * 5000, id='5000-Ziffern'),
])
def test_an_own_number_beyond_any_counter_is_kept_as_typed_and_lifts_no_counter_and_is_no_error(server_port, circle, number):
    """Eine eigene Nummer, die zur Vorlage passt, hebt den Zähler auf ihre laufende Nummer – aber nur bis ``MAX_RECEIPT_RUNNING``. Davor löste
    eine zu große Zahl einen ``OverflowError`` aus (500) und eine mit 64-Bit-Höchstwert einen Zähler am Anschlag, dessen nächster Schritt
    scheiterte."""
    assert numbering.MAX_RECEIPT_RUNNING == 999_999_999
    response = add_receipt(server_port, circle, number=number, number_suggestion='26B001')
    assert response.status == 303 and response.headers['Location'] == '/receipts'
    assert [row[1] for row in circle.db.fetch_receipts()] == [number] and counter(circle.db) == 0
    (entry,) = created(upload(server_port, circle, ('rechnung.pdf', plain_pdf())))
    assert entry['number'] == '26B001' and counter(circle.db) == 1                                       # der Zähler läuft ganz normal weiter


def test_an_own_number_up_to_the_maximum_still_lifts_the_counter(server_port, circle):
    add_receipt(server_port, circle, number='26B999999999', number_suggestion='26B001')
    assert counter(circle.db) == 999_999_999
    (entry,) = created(upload(server_port, circle, ('rechnung.pdf', plain_pdf())))
    assert entry['number'] == '26B1000000000'                                                          # der nächste Schritt rechnet ohne Fehler weiter


def test_the_form_never_takes_a_path_and_a_missing_field_is_no_error(server_port, circle):
    response = add_receipt(server_port, circle, number='26B001', number_suggestion='26B001', path='/etc/passwd')
    assert response.status == 303 and circle.db.fetch_receipts()[0][4] == ''
    bare = post(server_port, '/add_receipt', headers={'Origin': own_origin(server_port)},
                form={'number': '26B777'}, csrf=circle.csrf)                                          # kein Datum, kein Name, keine Info
    assert bare.status == 303
    assert circle.db.fetch_receipts()[1][1:] == ('26B777', '', '', '', '')


def test_changing_a_receipt_never_changes_its_stored_path(server_port, circle):
    (entry,) = created(upload(server_port, circle, ('rechnung.pdf', plain_pdf())))
    response = post(server_port, '/update_receipt', headers={'Origin': own_origin(server_port)}, csrf=circle.csrf,
                    form={'id': entry['id'], 'number': 'NEU-1', 'date': '2025-12-31', 'filename': 'neu.pdf',
                          'path': '/etc/passwd', 'info': 'Info'})
    assert response.status == 303
    row = circle.db.fetch_receipts()[0]
    assert row[1:] == ('NEU-1', '2025-12-31', 'neu.pdf', 'Belege/2026/26B001_rechnung.pdf', 'Info')   # die Datei zieht nicht um
    assert files_in(belege()) == ['2026/26B001_rechnung.pdf']


def test_the_receipt_form_has_no_path_field_and_names_the_file_as_text(server_port, circle):
    (entry,) = created(upload(server_port, circle, ('rechnung.pdf', plain_pdf())))
    page = get(server_port, '/receipts').text
    assert Dom(page).all('input') and not [a for a in Dom(page).all('input') if a.get('name') == 'path']
    row = next(a for a in Dom(page).all('tr') if 'receipt-row' in a.get('class', '').split())
    assert row['data-file'] == '26B001_rechnung.pdf' and 'data-path' not in row
    assert 'Belege/2026' not in page and admin_dir() not in page             # der Pfad steht nirgends auf der Seite
    edit = get(server_port, '/receipts/edit?number=26B001').text
    assert 'Datei:' in edit and '26B001_rechnung.pdf' in edit and not [a for a in Dom(edit).all('input') if a.get('name') == 'path']
