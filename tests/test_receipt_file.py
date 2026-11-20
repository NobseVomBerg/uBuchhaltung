# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Datei eines Belegs ansehen und herunterladen (2.5.11): ``GET /receipts/file?id=<n>[&inline=1]``.

* Welche Datei es gibt, entscheidet allein ``receipt_files.stored_receipt_path``: Der Wert in ``Documents.Path`` gilt nur innerhalb
  von ``Belege/`` und ``Documents/`` (Bestand des früheren Uploads) des angemeldeten Benutzers – relativ zu seinem Datenordner oder
  absolut, nach Auflösen von Links und ``..``. Alles andere (``/etc/passwd``, ``..``, die Datenbank, der Ordner eines anderen
  Benutzers, ein Link nach außen) ist dieselbe Antwort: 404 mit festem Satz, nie mit einem Pfad; der Pfad steht nur im Server-Log.
* Angezeigt wird die Datei nur mit ``inline=1`` UND wenn Endung und Anfangsbytes zur Positivliste passen (PDF und die Rasterbilder der
  Logos); alles andere – auch SVG, HTML, XML, ein ``.pdf`` mit HTML-Inhalt – ist immer ``attachment`` mit ``application/octet-stream``.
* Die Datei geht in Stücken aus dem offenen Deskriptor hinaus, nie ganz durch den Speicher; die Antwort trägt ``no-store`` und
  ``nosniff``; die Route liest nur und prüft die Herkunft wie ``/backup/download``.
* Die Liste zeigt „Ansehen“ (nur, wo es geht) und „Herunterladen“; ein gespeicherter Pfad, der nicht gilt, erscheint als Text, nie als Pfad.

Alle Namen und Inhalte sind erfundene Testwerte.
"""
import html
import json
import os
import re
import shutil
import socket
import subprocess
import time
import tracemalloc
import urllib.parse
from types import SimpleNamespace

import pytest

import auth
import userctx
from server import app as server_app
from server import receipt_files
from tests import installation
from tests.dom_helpers import Dom
from tests.http_client import FOREIGN_ORIGIN, TIMEOUT, get, own_origin, post
from tests.test_backup_download import make_symlink, raw_get
from tests.test_dirlock import exclusive_held

DAY = '2026-03-04'
PDF = b'%PDF-1.4\n% ein Beleg\n'
PNG = b'\x89PNG\r\n\x1a\nBildbytes'
JPG = b'\xff\xd8\xff\xe0JFIFBildbytes'
GIF = b'GIF89aBildbytes'
WEBP = b'RIFF\x1a\x00\x00\x00WEBPVP8 Bildbytes'


# ── Hilfen ───────────────────────────────────────────────────────────────────
def admin_dir():
    return os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN)


def put(relative, content=b'', user_dir=None):
    path = os.path.join(user_dir or admin_dir(), *relative.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(content)
    return path


def put_sparse(relative, size):
    path = os.path.join(admin_dir(), *relative.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.truncate(size)
    return path


def add(db, relative, content=PDF, number='26B001', filename=None, stored=None):
    """Eine Datei unter dem Datenordner ablegen und den Beleg dazu anlegen; liefert seine ID. ``stored`` ist der Wert von ``Path``."""
    put(relative, content)
    db.insert_receipt(number, DAY, filename if filename is not None else relative.rsplit('/', 1)[-1],
                      relative if stored is None else stored, '')
    return db.fetch_receipts()[-1][0]


def set_path(db, receipt_id, value):
    """Den gespeicherten Pfad roh setzen – so ändert es nur die SQL-Konsole oder eine zurückgespielte Sicherung."""
    import contextlib
    import sqlite3
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute('UPDATE Documents SET Path = ? WHERE ID = ?', (value, receipt_id))
        con.commit()


def url(receipt_id, inline=None):
    return f'/receipts/file?id={receipt_id}' + ('' if inline is None else f'&inline={inline}')


# ── Eine Datei holen ─────────────────────────────────────────────────────────
def test_a_receipt_file_is_delivered_byte_for_byte_as_a_download(server_port, auto_login):
    receipt = add(auto_login.db, 'Belege/2026/26B001_rechnung.pdf', PDF * 100, filename='rechnung.pdf')
    resp = get(server_port, url(receipt))
    assert resp.status == 200 and resp.body == PDF * 100
    assert resp.headers['Content-Length'] == str(len(PDF) * 100)
    assert resp.headers['Content-Type'] == 'application/octet-stream'
    assert resp.headers['Content-Disposition'].startswith('attachment; filename="rechnung.pdf"')
    assert resp.headers['X-Filename'] == 'rechnung.pdf'
    assert resp.headers['X-Content-Type-Options'] == 'nosniff' and resp.headers['X-Frame-Options'] == 'DENY'
    assert resp.headers['Cache-Control'] == 'no-store'                      # ein Beleg gehört in keinen Zwischenspeicher


@pytest.mark.parametrize('tail', ['.png', '.css', '.js'])
def test_an_extension_at_the_end_of_the_address_never_makes_a_receipt_cacheable(server_port, auto_login, tail):
    """Die Seiten-Dateien (``.css``, ``.js``, ``.png``) dürfen einen Tag lang ``public`` im Zwischenspeicher liegen – ein Beleg nie, auch
    nicht, wenn die Adresse so endet."""
    receipt = add(auto_login.db, 'Belege/2026/26B001_a.pdf')
    resp = get(server_port, f'/receipts/file?id={receipt}&x={tail}')
    assert resp.status == 200 and resp.headers['Cache-Control'] == 'no-store'
    assert get(server_port, f'/receipts/file?id=99999&x={tail}').headers['Cache-Control'] == 'no-store'


def test_a_name_with_umlauts_travels_percent_encoded_in_both_headers(server_port, auto_login):
    receipt = add(auto_login.db, 'Belege/2026/26B001_x.pdf', filename='Büro Rechnung März.pdf')
    resp = get(server_port, url(receipt))
    assert urllib.parse.unquote(resp.headers['X-Filename']) == 'Büro Rechnung März.pdf'
    assert resp.headers['X-Filename'] == urllib.parse.quote('Büro Rechnung März.pdf', safe='')
    assert "filename*=UTF-8''B%C3%BCro%20Rechnung%20M%C3%A4rz.pdf" in resp.headers['Content-Disposition']


def test_without_a_name_the_file_name_on_disk_is_used(server_port, auto_login):
    receipt = add(auto_login.db, 'Belege/2026/26B001_rechnung.pdf', filename='')
    resp = get(server_port, url(receipt))
    assert resp.headers['X-Filename'] == '26B001_rechnung.pdf'
    assert resp.headers['Content-Disposition'].startswith('attachment; filename="26B001_rechnung.pdf"')


def test_the_connection_stays_usable_after_a_whole_download(server_port, auto_login):
    from tests.http_client import KeepAlive
    receipt = add(auto_login.db, 'Belege/2026/26B001_a.pdf', os.urandom(300_000))
    with KeepAlive(server_port) as conn:
        assert len(conn.get(url(receipt)).body) == 300_000
        assert conn.get('/about').status == 200


# ── Anzeigen nur, was die Positivliste kennt ─────────────────────────────────
INLINE = [
    pytest.param('beleg.pdf', PDF, 'application/pdf', id='PDF'),
    pytest.param('BELEG.PDF', PDF, 'application/pdf', id='PDF-grosse-Endung'),
    pytest.param('foto.png', PNG, 'image/png', id='PNG'),
    pytest.param('foto.jpg', JPG, 'image/jpeg', id='JPG'),
    pytest.param('foto.jpeg', JPG, 'image/jpeg', id='JPEG'),
    pytest.param('bild.gif', GIF, 'image/gif', id='GIF'),
    pytest.param('bild.webp', WEBP, 'image/webp', id='WEBP'),
]


@pytest.mark.parametrize('name, content, content_type', INLINE)
def test_a_pdf_and_the_listed_images_are_shown_inline_with_their_type(server_port, auto_login, name, content, content_type):
    receipt = add(auto_login.db, f'Belege/2026/26B001_{name}', content, filename=name)
    resp = get(server_port, url(receipt, inline=1))
    assert resp.status == 200 and resp.body == content
    assert resp.headers['Content-Type'] == content_type
    assert resp.headers['Content-Disposition'].startswith(f'inline; filename="{name}"')
    assert resp.headers['X-Content-Type-Options'] == 'nosniff' and resp.headers['Cache-Control'] == 'no-store'
    # ohne inline=1 ist es ein Download
    plain = get(server_port, url(receipt))
    assert plain.headers['Content-Type'] == 'application/octet-stream' and plain.headers['Content-Disposition'].startswith('attachment')


NEVER_INLINE = [
    pytest.param('falsch.pdf', b'<html><script>alert(1)</script></html>', id='PDF-mit-HTML-Inhalt'),
    pytest.param('bild.svg', b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', id='SVG'),
    pytest.param('seite.html', b'<html><script>alert(1)</script></html>', id='HTML'),
    pytest.param('seite.htm', b'<html></html>', id='HTM'),
    pytest.param('daten.xml', b'<?xml version="1.0"?><a/>', id='XML'),
    pytest.param('png-als-pdf.pdf', PNG, id='PNG-im-PDF'),
    pytest.param('pdf-als-png.png', PDF, id='PDF-im-PNG'),
    pytest.param('jpg-als-gif.gif', JPG, id='JPG-im-GIF'),
    pytest.param('kurz.webp', b'RIFF', id='zu-kurzes-WEBP'),
    pytest.param('notiz.txt', PDF, id='PDF-in-TXT'),
    pytest.param('ohne-endung', PDF, id='ohne-Endung'),
    pytest.param('doppelt.pdf.html', PDF, id='PDF-dann-HTML'),
    pytest.param('skript.js', b'alert(1)', id='JavaScript'),
    pytest.param('leer.pdf', b'', id='leeres-PDF'),
]


@pytest.mark.parametrize('name, content', NEVER_INLINE)
def test_anything_else_is_only_ever_a_download_even_with_inline(server_port, auto_login, name, content):
    receipt = add(auto_login.db, f'Belege/2026/26B001_{name}', content, filename=name)
    for query in ('&inline=1', '&inline=1&inline=0'):
        resp = get(server_port, f'/receipts/file?id={receipt}{query}')
        assert resp.status == 200 and resp.body == content
        assert resp.headers['Content-Type'] == 'application/octet-stream'
        assert resp.headers['Content-Disposition'].startswith('attachment')
        assert resp.headers['X-Content-Type-Options'] == 'nosniff'


@pytest.mark.parametrize('value', ['0', '2', 'true', 'yes', '', ' 1', '1 ', '01', 'on'])
def test_only_inline_1_shows_a_file(server_port, auto_login, value):
    receipt = add(auto_login.db, 'Belege/2026/26B001_a.pdf')
    resp = get(server_port, f'/receipts/file?id={receipt}&inline={urllib.parse.quote(value)}')
    assert resp.status == 200 and resp.headers['Content-Disposition'].startswith('attachment')


@pytest.mark.parametrize('name, head, expected', [
    ('a.pdf', b'%PDF-1.7', 'application/pdf'), ('a.PDF', b'%PDF-', 'application/pdf'), ('a.pdf', b'%PDF', None),
    ('a.pdf', b'<html>', None), ('a.pdf', b'', None), ('a.png', PNG[:16], 'image/png'), ('a.png', b'\x89PNG', None),
    ('a.jpg', b'\xff\xd8\xff', 'image/jpeg'), ('a.gif', b'GIF87a', 'image/gif'), ('a.gif', b'GIF89a', 'image/gif'),
    ('a.gif', b'GIF88a', None), ('a.webp', WEBP[:16], 'image/webp'), ('a.webp', b'RIFF\x00\x00\x00\x00AVI ', None),
    ('a.svg', b'<svg/>', None), ('a.html', b'<html/>', None), ('a.xml', b'<a/>', None), ('a.txt', b'text', None),
    ('a.png.exe', PNG, None), ('png', PNG, None), ('', PNG, None), (None, PNG, None),
])
def test_inline_type_needs_the_extension_and_the_first_bytes(name, head, expected):
    assert receipt_files.inline_type(name, head) == expected


# ── Der gespeicherte Pfad gilt nur innerhalb der Beleg-Ordner ────────────────
@pytest.fixture
def secret(tmp_path):
    """Eine Datei außerhalb des Datenordners, die nie ausgeliefert werden darf."""
    path = tmp_path / 'draussen.pdf'
    path.write_bytes(b'%PDF-1.4 GEHEIM')
    return path


def other_user_dir():
    return os.path.join(userctx.DATA_ROOT, 'users', 'alice')


OUTSIDE = [
    pytest.param(lambda s: '/etc/passwd', id='absolut-System'),
    pytest.param(lambda s: str(s), id='absolut-andere-Datei'),
    pytest.param(lambda s: '../../../etc/passwd', id='aufwaerts'),
    pytest.param(lambda s: 'Belege/../../../etc/passwd', id='aufwaerts-ueber-Belege'),
    pytest.param(lambda s: 'Belege/2026/../../buch.db', id='die-Datenbank-ueber-Belege'),
    pytest.param(lambda s: 'buch.db', id='die-Datenbank'),
    pytest.param(lambda s: 'invoices/2026/R26001.pdf', id='anderer-Ordner'),
    pytest.param(lambda s: 'Belegefoo/a.pdf', id='Ordner-mit-gleichem-Anfang'),
    pytest.param(lambda s: os.path.join(other_user_dir(), 'Belege', 'fremd.pdf'), id='Ordner-eines-anderen-Benutzers'),
    pytest.param(lambda s: '../alice/Belege/fremd.pdf', id='anderer-Benutzer-aufwaerts'),
    pytest.param(lambda s: 'Belege/a\x00b.pdf', id='NUL'),
    pytest.param(lambda s: 'C:\\Windows\\win.ini', id='Windows-Pfad'),
    pytest.param(lambda s: os.path.dirname(userctx.DATA_ROOT), id='Ordner-ueber-der-Datenwurzel'),
]


@pytest.mark.parametrize('build', OUTSIDE)
def test_a_path_outside_the_receipt_folders_is_a_404_with_a_fixed_sentence_and_no_path(server_port, auto_login, secret, capsys, build):
    put('Belege/2026/echt.pdf', b'%PDF-1.4 echt')
    put('Belegefoo/a.pdf', b'%PDF-1.4 nicht-in-Belege')
    put('invoices/2026/R26001.pdf', b'%PDF-1.4 rechnung')
    put('Belege/fremd.pdf', b'x', user_dir=other_user_dir())
    stored = build(secret)
    auto_login.db.insert_receipt('26B001', DAY, 'a.pdf', 'platzhalter', '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    set_path(auto_login.db, receipt, stored)
    resp = get(server_port, url(receipt, inline=1))
    assert resp.status == 404 and resp.text == receipt_files.NO_FILE == 'Zu diesem Beleg liegt keine Datei vor.'
    assert resp.headers['Content-Type'] == 'text/plain; charset=utf-8' and resp.headers['Cache-Control'] == 'no-store'
    for text in (stored, userctx.DATA_ROOT, 'GEHEIM', '/etc/passwd'):
        assert text not in resp.text, text
    assert b'GEHEIM' not in resp.body and b'root:' not in resp.body
    assert repr(stored) in capsys.readouterr().err                         # der Pfad steht nur im Server-Log


def test_a_link_in_belege_that_leads_outside_is_never_followed(server_port, auto_login, secret):
    os.makedirs(os.path.join(admin_dir(), 'Belege', '2026'))
    make_symlink(secret, os.path.join(admin_dir(), 'Belege', '2026', 'link.pdf'))
    receipt = auto_login.db.insert_receipt('26B001', DAY, 'link.pdf', 'Belege/2026/link.pdf', '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    resp = get(server_port, url(receipt, inline=1))
    assert resp.status == 404 and resp.text == receipt_files.NO_FILE and b'GEHEIM' not in resp.body


def test_the_folder_belege_may_itself_be_a_link_to_another_place(server_port, auto_login, tmp_path):
    """Belege auf einem NAS (DEPLOYMENT.md): Der Ordner ist ein Link, die Dateien darin werden trotzdem geliefert."""
    elsewhere = tmp_path / 'nas'
    (elsewhere / '2026').mkdir(parents=True)
    (elsewhere / '2026' / '26B001_a.pdf').write_bytes(PDF)
    make_symlink(elsewhere, os.path.join(admin_dir(), 'Belege'))
    auto_login.db.insert_receipt('26B001', DAY, 'a.pdf', 'Belege/2026/26B001_a.pdf', '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    assert get(server_port, url(receipt)).body == PDF


def test_the_old_folder_documents_stays_readable(server_port, auto_login):
    """Der frühere Upload legte nach ``Documents/`` ab; sein Bestand (relativer oder absoluter Pfad) bleibt les- und herunterladbar."""
    first = add(auto_login.db, 'Documents/alt.pdf', b'%PDF-1.4 alt', filename='alt.pdf')
    absolute = os.path.join(admin_dir(), 'Documents', 'absolut.pdf')
    second = add(auto_login.db, 'Documents/absolut.pdf', b'%PDF-1.4 absolut', filename='absolut.pdf', stored=absolute)
    assert get(server_port, url(first, inline=1)).headers['Content-Type'] == 'application/pdf'
    assert get(server_port, url(second)).body == b'%PDF-1.4 absolut'


@pytest.mark.parametrize('stored', [None, '', '   '])
def test_a_receipt_without_a_stored_path_has_no_file_and_nothing_is_logged(server_port, auto_login, capsys, stored):
    auto_login.db.insert_receipt('26B001', DAY, 'papier.pdf', stored, '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    resp = get(server_port, url(receipt))
    assert resp.status == 404 and resp.text == receipt_files.NO_FILE
    assert 'außerhalb' not in capsys.readouterr().err


@pytest.mark.parametrize('stored', ['Belege', 'Belege/2026', 'Documents'])
def test_a_folder_is_no_file(server_port, auto_login, stored):
    os.makedirs(os.path.join(admin_dir(), 'Belege', '2026'), exist_ok=True)
    os.makedirs(os.path.join(admin_dir(), 'Documents'), exist_ok=True)
    auto_login.db.insert_receipt('26B001', DAY, 'a.pdf', stored, '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    resp = get(server_port, url(receipt))
    assert resp.status == 404 and resp.text == receipt_files.NO_FILE


def test_a_missing_file_is_the_same_404(server_port, auto_login):
    auto_login.db.insert_receipt('26B001', DAY, 'weg.pdf', 'Belege/2026/weg.pdf', '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    resp = get(server_port, url(receipt))
    assert resp.status == 404 and resp.text == receipt_files.NO_FILE


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='Pipen gibt es nur auf POSIX-Systemen')
def test_a_pipe_in_place_of_the_file_does_not_hold_the_request(server_port, auto_login):
    path = os.path.join(admin_dir(), 'Belege', '2026', 'pipe.pdf')
    os.makedirs(os.path.dirname(path))
    os.mkfifo(path)
    auto_login.db.insert_receipt('26B001', DAY, 'pipe.pdf', 'Belege/2026/pipe.pdf', '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    resp = get(server_port, url(receipt))                                    # ein Öffnen ohne ``O_NONBLOCK`` hinge hier bis zum Timeout
    assert resp.status == 404 and resp.text == receipt_files.NO_FILE


# ── Gültige und ungültige IDs ────────────────────────────────────────────────
@pytest.mark.parametrize('query', ['', '?', '?id=', '?id=abc', '?id=1.5', '?id=-1', '?id=0', f'?id={2 ** 63}', f'?id={"9" * 30}',
                                   '?id=99999', '?number=26B001'])
def test_a_missing_invalid_or_unknown_id_is_the_same_404(server_port, auto_login, query):
    auto_login.db.insert_receipt('26B001', DAY, 'a.pdf', 'Belege/2026/a.pdf', '')            # es gibt einen Beleg (ID 1) mit Datei
    put('Belege/2026/a.pdf', PDF)
    resp = get(server_port, '/receipts/file' + query)
    assert resp.status == 404 and resp.text == receipt_files.NOT_FOUND == 'Diesen Beleg gibt es nicht.'
    assert resp.headers['Content-Type'] == 'text/plain; charset=utf-8' and resp.headers['Cache-Control'] == 'no-store'


# ── Herkunft und Methode ─────────────────────────────────────────────────────
@pytest.mark.parametrize('headers, status', [
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, 403, id='cross-site'),
    pytest.param({'Sec-Fetch-Site': 'same-site'}, 403, id='same-site'),
    pytest.param({'Sec-Fetch-Site': 'unbekannt'}, 403, id='unbekannter-wert'),
    pytest.param({'Sec-Fetch-Site': 'same-origin'}, 200, id='same-origin'),
    pytest.param({'Sec-Fetch-Site': 'none'}, 200, id='none-eigene-eingabe'),
    pytest.param({}, 200, id='ohne-kopf'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, 403, id='fremder-origin-ohne-fetch-metadata'),
])
def test_a_request_from_a_foreign_page_is_refused(server_port, auto_login, headers, status):
    receipt = add(auto_login.db, 'Belege/2026/26B001_a.pdf', b'%PDF-1.4 inhalt')
    resp = get(server_port, url(receipt, inline=1), headers)
    assert resp.status == status
    if status == 403:
        assert resp.text == receipt_files.FOREIGN == 'Belege lassen sich nur über die Seite „Belege“ öffnen.'
        assert b'inhalt' not in resp.body and resp.headers['Cache-Control'] == 'no-store'
    assert get(server_port, url(receipt), {'Origin': own_origin(server_port)}).status == 200      # die eigene Seite


def test_only_get_serves_a_file_and_the_route_is_no_exception_to_the_lock(server_port, auto_login):
    receipt = add(auto_login.db, 'Belege/2026/26B001_a.pdf', b'%PDF-1.4 inhalt')
    resp = post(server_port, url(receipt), headers={'Origin': own_origin(server_port)}, csrf=auto_login.csrf)
    assert resp.status == 404 and b'inhalt' not in resp.body
    # Ein Beleg liegt im Datenordner, den eine Wiederherstellung ersetzt: anders als die Sicherungen wartet die Route auf sie
    assert '/receipts/file' not in server_app._LOCK_FREE_ROUTES
    with exclusive_held(admin_dir()):
        busy = get(server_port, url(receipt))
    assert busy.status == 503 and 'Retry-After' in busy.headers and b'inhalt' not in busy.body


# ── Wer holt was ─────────────────────────────────────────────────────────────
@pytest.fixture
def two_users():
    installation.set_up(auto_login=False, users=[('alice', False)])
    return SimpleNamespace(admin={'Cookie': f"session={auth.create_session('admin')}"},
                           alice={'Cookie': f"session={auth.create_session('alice')}"})


def test_a_user_only_gets_the_receipts_of_the_own_folder(server_port, two_users):
    admin_db, alice_db = installation.open_db('admin'), installation.open_db('alice')
    own = add(admin_db, 'Belege/2026/26B001_a.pdf', b'%PDF-1.4 Beleg des Administrators', filename='a.pdf')
    put('Belege/2026/26B001_a.pdf', b'%PDF-1.4 Beleg von Alice', user_dir=other_user_dir())
    alice_db.insert_receipt('26B001', DAY, 'a.pdf', 'Belege/2026/26B001_a.pdf', '')
    alice_id = alice_db.fetch_receipts()[-1][0]
    assert own == alice_id == 1                                              # dieselbe ID in zwei Datenbanken
    assert get(server_port, url(own), two_users.admin).body == b'%PDF-1.4 Beleg des Administrators'
    assert get(server_port, url(own), two_users.alice).body == b'%PDF-1.4 Beleg von Alice'         # jeder aus dem eigenen Ordner
    # ein Pfad in Alices Datenbank, der in den Ordner des Administrators zeigt, gilt nicht
    set_path(alice_db, alice_id, os.path.join(admin_dir(), 'Belege', '2026', '26B001_a.pdf'))
    resp = get(server_port, url(alice_id), two_users.alice)
    assert resp.status == 404 and resp.text == receipt_files.NO_FILE
    set_path(alice_db, alice_id, '../admin/Belege/2026/26B001_a.pdf')
    assert get(server_port, url(alice_id), two_users.alice).status == 404


def test_without_a_login_a_file_goes_to_the_login_page(server_port, two_users):
    admin_db = installation.open_db('admin')
    receipt = add(admin_db, 'Belege/2026/26B001_a.pdf', b'%PDF-1.4 geheim')
    resp = get(server_port, url(receipt))
    assert resp.status == 303 and resp.headers['Location'] == '/login' and b'geheim' not in resp.body


# ── Große Dateien, Abbrüche ──────────────────────────────────────────────────
def test_a_large_file_goes_out_in_pieces_never_whole_through_the_memory(server_port, auto_login):
    size = 96 * 1024 ** 2
    put_sparse('Belege/2026/26B001_gross.bin', size)
    auto_login.db.insert_receipt('26B001', DAY, 'gross.bin', 'Belege/2026/26B001_gross.bin', '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    received = 0
    tracemalloc.start()
    try:
        sock, head = raw_get(server_port, url(receipt), 1)
        try:
            sock.settimeout(TIMEOUT)
            buffer = head
            while b'\r\n\r\n' not in buffer:
                buffer += sock.recv(4096)
            header, _, rest = buffer.partition(b'\r\n\r\n')
            assert header.startswith(b'HTTP/1.1 200') and f'Content-Length: {size}'.encode() in header
            received = len(rest)
            while received < size:
                block = sock.recv(256 * 1024)
                if not block:
                    break
                received += len(block)                                    # die Daten werden nicht gehalten
        finally:
            sock.close()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert received == size
    assert peak < 16 * 1024 ** 2                                          # ein Stück von 1 MiB, nie die 96 MiB der Datei


def test_a_client_that_hangs_up_does_not_hurt_the_server(server_port, auto_login, capsys):
    put_sparse('Belege/2026/26B001_gross.bin', 100 * 1024 ** 2)
    auto_login.db.insert_receipt('26B001', DAY, 'gross.bin', 'Belege/2026/26B001_gross.bin', '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    sock, head = raw_get(server_port, url(receipt), 1024)
    assert head.startswith(b'HTTP/1.1 200')
    sock.close()                                                          # ungelesene Daten: der Server bekommt ein RST
    err, deadline = '', time.monotonic() + 15
    while f'Download abgebrochen: Beleg {receipt}' not in err and time.monotonic() < deadline:
        time.sleep(0.05)
        err += capsys.readouterr().err
    assert f'Download abgebrochen: Beleg {receipt}' in err and 'Traceback' not in err
    assert get(server_port, '/about').status == 200


def test_a_file_that_gets_shorter_while_sending_ends_the_answer_early_and_the_server_goes_on(server_port, auto_login, monkeypatch, capsys):
    import http.client
    real = receipt_files.open_receipt
    receipt = add(auto_login.db, 'Belege/2026/26B001_a.pdf', b'z' * 1000)

    def shrinking(path, log=True):
        opened = real(path, log)
        return opened._replace(size=50_000)
    monkeypatch.setattr(receipt_files, 'open_receipt', shrinking)
    conn = http.client.HTTPConnection('127.0.0.1', server_port, timeout=TIMEOUT)
    try:
        conn.request('GET', url(receipt))
        resp = conn.getresponse()
        assert resp.status == 200 and resp.getheader('Content-Length') == '50000'
        with pytest.raises(http.client.IncompleteRead) as raised:
            resp.read()
        assert raised.value.partial == b'z' * 1000
    finally:
        conn.close()
    assert get(server_port, '/about').status == 200
    assert f'Download unvollständig (1000 von 50000 Bytes): Beleg {receipt}' in capsys.readouterr().err


def test_an_unexpected_error_before_the_headers_is_a_500_without_the_text_of_the_error(server_port, auto_login, monkeypatch, capsys):
    receipt = add(auto_login.db, 'Belege/2026/26B001_a.pdf')

    def boom(path, log=True):
        raise OSError('/geheim/pfad kaputt')
    monkeypatch.setattr(receipt_files, 'open_receipt', boom)
    resp = get(server_port, url(receipt))
    assert resp.status == 500 and resp.text == receipt_files.OPEN_FAILED and 'geheim' not in resp.text
    assert '/geheim/pfad kaputt' in capsys.readouterr().err
    assert get(server_port, '/about').status == 200


# ── stored_receipt_path ──────────────────────────────────────────────────────
def test_stored_receipt_path_takes_relative_and_absolute_values_inside_the_two_folders(tmp_db):
    root = os.path.realpath(userctx.DATA_ROOT)
    for relative in ('Belege/2026/a.pdf', 'Documents/b.pdf'):
        put_in_root(relative)
        expected = os.path.join(root, *relative.split('/'))
        assert receipt_files.stored_receipt_path(relative) == expected
        assert receipt_files.stored_receipt_path(os.path.join(userctx.DATA_ROOT, *relative.split('/'))) == expected
    assert receipt_files.stored_receipt_path('Belege/2026/../2027/c.pdf') == os.path.join(root, 'Belege', '2027', 'c.pdf')


def put_in_root(relative):
    path = os.path.join(userctx.DATA_ROOT, *relative.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(b'x')
    return path


@pytest.mark.parametrize('value', [None, '', '   ', 5, b'Belege/a.pdf', 'Belege/a\x00b.pdf', '/etc/passwd', '../x.pdf', 'Belege/../x.pdf',
                                   'Belegefoo/a.pdf', 'Documentsx/a.pdf', 'buch.db', 'C:\\Windows\\x.pdf', '//server/share/a.pdf'])
def test_stored_receipt_path_refuses_everything_else(tmp_db, capsys, value):
    assert receipt_files.stored_receipt_path(value) is None
    err = capsys.readouterr().err
    if value is None or (isinstance(value, str) and not value.strip()):
        assert err == ''                                                  # ein Beleg ohne Datei ist keine Meldung wert
    else:
        assert 'außerhalb der Beleg-Ordner ignoriert' in err and repr(value) in err
    assert receipt_files.stored_receipt_path(value, log=False) is None and capsys.readouterr().err == ''


def test_a_stored_value_over_the_limit_is_no_path_at_all(tmp_db):
    """Ein sehr langer, tief verschachtelter Wert bremste jede Liste, die ihn auflöst: Über ``STORED_PATH_LIMIT`` Zeichen gilt er als kein Pfad."""
    limit = receipt_files.STORED_PATH_LIMIT
    assert limit == 1024
    at_limit = 'Belege/' + 'a/' * ((limit - len('Belege/') - 1) // 2) + 'x'
    assert len(at_limit) == limit and receipt_files.stored_receipt_path(at_limit) is not None          # genau die Grenze gilt noch
    for value in (at_limit + 'x', 'Belege/2026/' + 'a' * 5000, 'Belege/' + 'x/' * 2500 + 'a.pdf'):
        assert len(value) > limit
        assert receipt_files.stored_receipt_path(value) is None and receipt_files.stored_receipt_path(value, log=False) is None


def test_a_receipt_with_an_overlong_stored_path_has_no_file_and_the_list_says_so(server_port, auto_login):
    auto_login.db.insert_receipt('26B001', DAY, 'a.pdf', 'Belege/2026/' + 'a' * 5000, '')
    receipt = auto_login.db.fetch_receipts()[-1][0]
    resp = get(server_port, url(receipt))
    assert resp.status == 404 and resp.text == receipt_files.NO_FILE
    rows = list_rows(get(server_port, '/receipts?from=2026-01-01&to=2026-12-31').text)
    assert rows[receipt].cell == '<span class="muted">Datei nicht im Belegordner</span>'


# ── Die Liste ────────────────────────────────────────────────────────────────
def list_rows(page):
    """Die Zeilen der Belegliste je ID: (Zelle „Datei“, onclick des Löschens)."""
    found = {}
    for chunk in re.findall(r'<tr class="receipt-row"(.*?)</tr>', page, re.S):
        receipt_id = int(re.search(r'data-id="(\d+)"', chunk).group(1))
        cells = re.findall(r'<td>(.*?)</td>', chunk, re.S)
        found[receipt_id] = SimpleNamespace(cell=cells[3], delete=re.search(r'delete-icon" title="[^"]*" onclick="(.*?)">&#128465;', chunk).group(1))
    return found


def test_the_list_offers_view_and_download_where_they_work_and_says_so_in_text_where_not(server_port, auto_login):
    pdf = add(auto_login.db, 'Belege/2026/26B001_rechnung.pdf', PDF, number='26B001', filename='rechnung.pdf')
    text = add(auto_login.db, 'Belege/2026/26B002_notiz.txt', b'Notiz', number='26B002', filename='notiz.txt')
    auto_login.db.insert_receipt('26B003', DAY, 'papier.pdf', '', '')
    paper = auto_login.db.fetch_receipts()[-1][0]
    auto_login.db.insert_receipt('26B004', DAY, 'alt.pdf', '/srv/alt/rechnung.pdf', '')
    stale = auto_login.db.fetch_receipts()[-1][0]
    auto_login.db.insert_receipt('26B005', DAY, 'weg.pdf', 'Belege/2026/weg.pdf', '')
    gone = auto_login.db.fetch_receipts()[-1][0]
    page = get(server_port, '/receipts?from=2026-01-01&to=2026-12-31').text
    rows = list_rows(page)
    assert (f'<a href="/receipts/file?id={pdf}&amp;inline=1" target="_blank" rel="noopener">Ansehen</a>' in rows[pdf].cell
            and f"onclick=\"appDownload('/receipts/file?id={pdf}')\">Herunterladen</a>" in rows[pdf].cell)
    assert 'Ansehen' not in rows[text].cell and f"appDownload('/receipts/file?id={text}')" in rows[text].cell   # nur, was angezeigt werden darf
    assert rows[paper].cell == '–'
    for receipt in (stale, gone):
        assert rows[receipt].cell == '<span class="muted">Datei nicht im Belegordner</span>'
    assert '/srv/alt' not in page and 'Belege/2026' not in page and admin_dir() not in page      # nie ein Pfad auf der Seite
    assert 'data-path' not in page
    assert '<th>Datei</th>' in page and '<th>Pfad</th>' not in page
    assert [a['href'] for a in Dom(page).all('a') if a.get('target') == '_blank'] == [f'/receipts/file?id={pdf}&inline=1']


def test_the_question_before_deleting_a_receipt_with_a_file_says_so(server_port, auto_login):
    with_file = add(auto_login.db, 'Belege/2026/26B001_a.pdf', number='26B001')
    legacy = add(auto_login.db, 'Documents/alt.pdf', number='ALT-1')
    auto_login.db.insert_receipt('P-1', DAY, 'papier.pdf', '', '')
    paper = auto_login.db.fetch_receipts()[-1][0]
    rows = list_rows(get(server_port, '/receipts?from=2026-01-01&to=2026-12-31').text)
    assert html.unescape(rows[with_file].delete) == (
        f"appConfirmPost('/receipts/delete?id={with_file}', \"Beleg 26B001 und seine Datei wirklich löschen?\")")
    # ohne Datei – und bei Bestand in Documents/, den das Löschen nicht anfasst – bleibt die Frage, wie sie war
    for receipt in (legacy, paper):
        assert rows[receipt].delete == f"appConfirmPost('/receipts/delete?id={receipt}', 'Beleg wirklich löschen?')"


@pytest.mark.parametrize('number', ["X', alert(1), '", '"><img src=x onerror=alert(1)>', '</script><b>', 'a&b\\c', "O'Brien \u2028"])
def test_a_hostile_number_stays_data_in_the_question_with_a_file(server_port, auto_login, number):
    receipt = add(auto_login.db, 'Belege/2026/26B001_a.pdf', number=number)
    page = get(server_port, '/receipts?from=2026-01-01&to=2026-12-31').text
    onclick = html.unescape(list_rows(page)[receipt].delete)
    found = re.fullmatch(r"appConfirmPost\('([^']*)', (\".*\")\)", onclick)
    assert found and found.group(1) == f'/receipts/delete?id={receipt}'
    assert json.loads(found.group(2)) == f'Beleg {number.strip()} und seine Datei wirklich löschen?'   # eine Zeichenkette, nichts eingeschleust
    assert Dom(page).all('img') == [] and number not in page


def test_the_page_of_a_receipt_names_its_file_as_text(server_port, auto_login):
    add(auto_login.db, 'Belege/2026/26B001_rechnung.pdf', number='26B001')
    auto_login.db.insert_receipt('P-1', DAY, 'papier.pdf', '', '')
    auto_login.db.insert_receipt('X-1', DAY, 'x.pdf', '/srv/alt/x.pdf', '')
    assert '26B001_rechnung.pdf' in get(server_port, '/receipts/edit?number=26B001').text
    assert 'keine Datei' in get(server_port, '/receipts/edit?number=P-1').text
    stale = get(server_port, '/receipts/edit?number=X-1').text
    assert 'Datei nicht im Belegordner' in stale and '/srv/alt' not in stale


# ── Das Skript der Seite: Bearbeiten und Suche (Node, ohne Browser) ──────────
NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die Textprüfungen derselben Datei decken das Wichtigste ab')

EDIT_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, scenarioFile] = process.argv;
const scenario = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));

const byId = {};
const element = () => ({value: '', textContent: '', hidden: true, action: '', style: {display: ''}, scrollIntoView() {}});
const rows = scenario.rows.map(data => {
  const attributes = {'data-number': data.number, 'data-filename': data.filename, 'data-info': data.info,
                      'data-file': data.file, 'data-path': data.path};
  return {dataset: {id: data.id, number: data.number, date: data.date, filename: data.filename, info: data.info, file: data.file},
          style: {display: ''}, getAttribute: name => (name in attributes ? attributes[name] : null)};
});
const sandbox = {
  document: {getElementById: id => byId[id] || (byId[id] = element()), querySelectorAll: () => rows},
  location: {reload() {}},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);

const out = {};
vm.runInContext('editReceiptFromRow', sandbox)({closest: () => rows[scenario.edit]});
const field = id => byId[id] || (byId[id] = element());
out.edit = {id: field('editId').value, number: field('editNumber').value, date: field('editDate').value,
            filename: field('editFilename').value, info: field('editInfo').value, fileText: field('editFileText').textContent,
            fileRowHidden: field('editFileRow').hidden, action: field('receiptForm').action,
            newHidden: field('btnNew').style.display, editShown: field('btnEdit').style.display,
            hasPathField: 'editPath' in byId};
out.search = {};
for (const term of scenario.searches) {
  field('receiptSearch').value = term;
  vm.runInContext('filterReceipts', sandbox)();
  out.search[term] = rows.map(row => row.style.display === '');
}
console.log(JSON.stringify(out));
"""

ROWS = [
    {'id': '1', 'number': '26B001', 'date': DAY, 'filename': 'Strom Januar.pdf', 'info': 'Abschlag', 'file': '26B001_Strom Januar.pdf',
     'path': 'Belege/2026/geheimer-ordner/x.pdf'},
    {'id': '2', 'number': 'ER-7', 'date': DAY, 'filename': 'Papierbeleg', 'info': 'Büromaterial', 'file': 'keine Datei', 'path': ''},
]


@NO_NODE
def test_the_edit_script_fills_the_form_names_the_file_as_text_and_the_search_covers_number_name_and_info(server_port, auto_login, tmp_path):
    page = get(server_port, '/receipts').text
    (script,) = [text for text in Dom(page).scripts if 'editReceiptFromRow' in text]
    for forbidden in ('innerHTML', 'outerHTML', 'document.write', 'editPath'):
        assert forbidden not in script, forbidden
    harness, source, data = (tmp_path / name for name in ('harness.js', 'edit.js', 'scenario.json'))
    harness.write_text(EDIT_HARNESS, encoding='utf-8')
    source.write_text(script, encoding='utf-8')
    data.write_text(json.dumps({'rows': ROWS, 'edit': 0, 'searches': ['26b001', 'strom', 'Büromaterial', 'ER-7', 'geheimer', 'nichts', '']}),
                    encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(source), str(data)], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out['edit'] == {'id': '1', 'number': '26B001', 'date': DAY, 'filename': 'Strom Januar.pdf', 'info': 'Abschlag',
                           'fileText': '26B001_Strom Januar.pdf', 'fileRowHidden': False, 'action': '/update_receipt',
                           'newHidden': 'none', 'editShown': '', 'hasPathField': False}
    assert out['search'] == {'26b001': [True, False], 'strom': [True, False], 'Büromaterial': [False, True], 'ER-7': [False, True],
                             'geheimer': [False, False],                 # ein Pfad wird nicht durchsucht (er steht gar nicht mehr in der Zeile)
                             'nichts': [False, False], '': [True, True]}
