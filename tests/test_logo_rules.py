# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Logo-Dateien: Positivliste für Upload, Auslieferung und PDF.

* Upload (``handle_logo_upload``): nur PNG, JPG/JPEG, GIF und WEBP – Endung UND Anfang der Datei müssen
  stimmen –, höchstens ``MAX_LOGO_BYTES``; die Größe wird an ``Content-Length`` geprüft, ohne den Body zu
  lesen (die Verbindung wird dann geschlossen). SVG, HTML, ausführbare Dateien und Dateien ohne Endung
  werden abgelehnt, nichts wird geschrieben.
* Auslieferung (``/data/logos/``, ``/seed_data/private/``): nur Dateien mit einer Endung der Positivliste;
  auch eine vorhandene SVG-Datei ergibt 404.
* ``resolve_logo_path`` (fürs PDF): nur der Dateiname im ``logos``-Ordner des Nutzers (oder unter
  ``seed_data/private``); ein beliebiger Dateipfad, ``../``, das Verzeichnis eines anderen Nutzers, ein Link
  nach außen und jede andere Endung ergeben ``None``. Der Stundenzettel nimmt denselben Weg wie die Rechnung.
* Jede Endung der Positivliste lässt sich wirklich laden (``load_image_xobject``), und nur diese Formate: Eine Datei
  in einem anderen Bildformat (TIFF, EPS, PSD …) ergibt unter einer Endung der Liste kein Logo.

Alle Namen und Dateien sind erfundene Testwerte.
"""
import http.client
import io
import json
import os

import pytest
from PIL import Image, features

import userctx
from export import pdf_worktime
from export.pdf_core import (LOGO_CONTENT_TYPES, LOGO_PILLOW_FORMATS, load_image_xobject, logo_content_matches,
                             logo_extension, resolve_logo_path)
from export.pdf_worktime import generate_worktime_pdf
from server import handlers
from server.handlers import LOGO_SIZE_ERROR, LOGO_TYPE_ERROR, MAX_LOGO_BYTES
from server.pages import logo_on_server
from tests import installation
from tests.http_client import TIMEOUT, get, send

UPLOAD = '/masterdata/contacts/upload-logo'
BOUNDARY = '----logotestgrenze'
MULTIPART = f'multipart/form-data; boundary={BOUNDARY}'
PILLOW_FORMAT = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.gif': 'GIF', '.webp': 'WEBP'}
SKIP_WEBP = pytest.mark.skipif(not features.check('webp'), reason='dieses Pillow kann kein WEBP')


def image_bytes(extension, color='red'):
    """Ein kleines, echtes Bild im Format der Endung (so, wie Pillow es schreibt)."""
    buf = io.BytesIO()
    Image.new('RGB', (8, 6), color).save(buf, PILLOW_FORMAT[extension])
    return buf.getvalue()


def multipart_body(filename, content, field='logofile'):
    head = (f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8')
    return head + content + f'\r\n--{BOUNDARY}--\r\n'.encode('utf-8')


def write(path, content=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content)
    return path


def link(target, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.symlink(target, path)
    except (OSError, NotImplementedError):
        pytest.skip('Symlinks nicht verfügbar')
    return path


def logos_dir():
    """``logos``-Ordner des aktuellen Nutzers (ohne Anmeldung, wie in den meisten Tests: ``<Datenwurzel>/logos``)."""
    return userctx.user_subdir('logos', create=False)


def admin_logos():
    """``logos``-Ordner des automatisch angemeldeten Administrators (die Anfragen über ``server_port``)."""
    return os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'logos')


class FakeRequest:
    """Ersatz für den Request-Handler: Header, Body und die Flagge, ob die Verbindung geschlossen wird."""

    def __init__(self, headers, body=b''):
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.close_connection = False


def upload(filename, content, **headers):
    """Upload direkt über den Handler (ohne Netz); liefert (Antwort als dict, Anfrage)."""
    body = multipart_body(filename, content)
    request = FakeRequest({'Content-Type': MULTIPART, 'Content-Length': str(len(body)), **headers}, body)
    return json.loads(handlers.handle_logo_upload(request)), request


# ── Positivliste ──────────────────────────────────────────────────────────────
def test_the_list_holds_exactly_the_image_formats_the_pdf_can_embed():
    assert set(LOGO_CONTENT_TYPES) == {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    assert LOGO_CONTENT_TYPES['.png'] == 'image/png' and LOGO_CONTENT_TYPES['.webp'] == 'image/webp'
    assert LOGO_CONTENT_TYPES['.jpg'] == LOGO_CONTENT_TYPES['.jpeg'] == 'image/jpeg'
    for forbidden in ('.svg', '.html', '.htm', '.xml', '.pdf', '.exe', '.js', '.bmp', '.tif', '.ico', ''):
        assert forbidden not in LOGO_CONTENT_TYPES


@pytest.mark.parametrize('extension', [
    '.png', '.jpg', '.jpeg', '.gif', pytest.param('.webp', marks=SKIP_WEBP)])
def test_every_listed_extension_loads_as_an_image_and_has_matching_signature_bytes(extension, tmp_path):
    data = image_bytes(extension)
    assert logo_content_matches(extension, data)
    path = tmp_path / f'logo{extension}'
    path.write_bytes(data)
    image = load_image_xobject(str(path))
    assert image and image['width'] == 8 and image['height'] == 6 and image['data']


def test_the_pillow_format_names_follow_from_the_list():
    assert LOGO_PILLOW_FORMATS == ('GIF', 'JPEG', 'PNG', 'WEBP')
    registry = Image.registered_extensions()                 # Pillows eigene Zuordnung Endung → Format
    for extension in LOGO_CONTENT_TYPES:
        assert registry[extension] in LOGO_PILLOW_FORMATS, extension


def psd_bytes(width=8, height=6):
    """Eine kleine Photoshop-Datei von Hand (Pillow kann PSD lesen, aber nicht schreiben)."""
    be = lambda number, size: number.to_bytes(size, 'big')
    header = b'8BPS' + be(1, 2) + b'\0' * 6 + be(3, 2) + be(height, 4) + be(width, 4) + be(8, 2) + be(3, 2)
    sections = be(0, 4) * 3                                   # Farbtabelle, Ressourcen, Ebenen: leer
    return header + sections + be(0, 2) + b'\xc8' * (width * height) + b'\x0a' * (2 * width * height)


def other_format(pillow_format):
    """Ein Bild in einem Format, das NICHT zur Liste gehört (Bytes)."""
    if pillow_format == 'PSD':
        return psd_bytes()
    buf = io.BytesIO()
    try:
        Image.new('RGB', (8, 6), 'red').save(buf, pillow_format)
    except (OSError, ValueError, KeyError):
        pytest.skip(f'dieses Pillow kann {pillow_format} nicht schreiben')
    return buf.getvalue()


OTHER_FORMATS = ['TIFF', 'EPS', 'PSD', 'BMP', 'PPM', 'TGA', 'PCX', 'SGI']


@pytest.mark.parametrize('extension', ['.png', '.jpg', '.jpeg', '.gif', '.webp'])
@pytest.mark.parametrize('pillow_format', OTHER_FORMATS)
def test_a_file_in_another_image_format_is_not_embedded_even_under_a_listed_extension(tmp_path, pillow_format, extension):
    """Altbestand oder zurückgespielte Sicherung: eine TIFF/EPS/PSD-Datei, die ``.png`` heißt. Pillow würde sie
    öffnen (es erkennt das Format am Inhalt) – ``load_image_xobject`` lässt nur die Formate der Liste zu."""
    path = tmp_path / f'logo{extension}'
    path.write_bytes(other_format(pillow_format))
    assert Image.open(path).format == pillow_format           # Vorbedingung: ohne die Beschränkung ginge sie durch
    assert load_image_xobject(str(path)) is None              # kein Logo – und kein Fehler


def test_a_photo_with_several_pictures_under_jpg_still_loads(tmp_path):
    """Handyfotos sind oft MPO (mehrere JPEG-Bilder in einer Datei): Pillow öffnet sie als JPEG und übernimmt das."""
    buf = io.BytesIO()
    Image.new('RGB', (8, 6), 'red').save(buf, 'MPO', save_all=True, append_images=[Image.new('RGB', (8, 6), 'blue')])
    path = tmp_path / 'foto.jpg'
    path.write_bytes(buf.getvalue())
    image = load_image_xobject(str(path))
    assert image and image['width'] == 8 and image['height'] == 6


@pytest.mark.parametrize('extension', ['.png', '.jpg', '.gif', pytest.param('.webp', marks=SKIP_WEBP)])
def test_an_image_of_the_listed_format_under_another_listed_extension_still_loads(tmp_path, extension):
    """Gleich, wie die Datei heißt: entscheidend ist ein Format der Liste (eine ``.jpg`` mit PNG-Inhalt war nie ein Fehler)."""
    path = tmp_path / 'logo.png'
    path.write_bytes(image_bytes(extension))
    assert load_image_xobject(str(path)) is not None


@pytest.mark.parametrize('name, expected', [
    ('logo.png', '.png'), ('LOGO.PNG', '.png'), ('Foto.JpEg', '.jpeg'), ('a.b.gif', '.gif'),
    ('x.webp', '.webp'), ('logo.svg', None), ('logo.html', None), ('logo.png.exe', None),
    ('logo', None), ('.png', None), ('', None), (None, None), ('logo.png ', None),
])
def test_logo_extension_is_a_case_insensitive_positive_list(name, expected):
    assert logo_extension(name) == expected


@pytest.mark.parametrize('extension, data, ok', [
    ('.png', b'\x89PNG\r\n\x1a\n' + b'x', True),
    ('.png', b'\x89PNG', False),
    ('.png', b'<html>', False),
    ('.jpg', b'\xff\xd8\xff\xe0abc', True),
    ('.jpeg', b'\xff\xd8\xff', True),
    ('.jpg', b'\xff\xd8', False),
    ('.gif', b'GIF87a...', True),
    ('.gif', b'GIF89a...', True),
    ('.gif', b'GIF88a...', False),
    ('.webp', b'RIFF\x10\x00\x00\x00WEBPVP8 ', True),
    ('.webp', b'RIFF\x10\x00\x00\x00WAVEfmt ', False),         # eine WAV-Datei
    ('.webp', b'RIFF\x10\x00\x00\x00WEB', False),               # zu kurz
    ('.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>', False),
    ('', b'\x89PNG\r\n\x1a\n', False),
])
def test_signature_bytes_must_match_the_extension(extension, data, ok):
    assert logo_content_matches(extension, data) is ok


# ── Upload ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('filename, extension', [
    ('logo.png', '.png'), ('foto.jpg', '.jpg'), ('foto.jpeg', '.jpeg'), ('anim.gif', '.gif'),
    pytest.param('bild.webp', '.webp', marks=SKIP_WEBP), ('GROSS.PNG', '.png'),
    ('Mein Logo (neu).png', '.png'),
])
def test_upload_accepts_image_files_and_stores_them_under_a_safe_name(filename, extension):
    data = image_bytes(extension)
    answer, request = upload(filename, data)
    stored = filename.replace(' ', '_').replace('(', '_').replace(')', '_')
    assert answer == {'success': True, 'path': f'data/logos/{stored}'}
    with open(os.path.join(logos_dir(), stored), 'rb') as f:
        assert f.read() == data
    assert request.close_connection is False                  # der Body wurde gelesen: Keep-Alive bleibt möglich


NOT_AN_IMAGE = [
    pytest.param('logo.svg', b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', id='svg'),
    pytest.param('seite.html', b'<html><script>alert(1)</script></html>', id='html'),
    pytest.param('seite.htm', b'<html></html>', id='htm'),
    pytest.param('virus.exe', b'MZ\x90\x00\x03\x00\x00\x00', id='exe'),
    pytest.param('skript.js', b'alert(1)', id='js'),
    pytest.param('beleg.pdf', b'%PDF-1.4 ...', id='pdf'),
    pytest.param('logo.png.exe', image_bytes('.png'), id='doppelte-Endung'),
    pytest.param('logo', image_bytes('.png'), id='ohne-Endung'),
    pytest.param('.png', image_bytes('.png'), id='nur-Endung'),
    pytest.param('', image_bytes('.png'), id='leerer-Name'),
    pytest.param('logo.png', b'<html><body><script>alert(1)</script></body></html>', id='png-mit-HTML-Inhalt'),
    pytest.param('logo.png', image_bytes('.jpg'), id='png-mit-JPEG-Inhalt'),
    pytest.param('logo.jpg', image_bytes('.png'), id='jpg-mit-PNG-Inhalt'),
    pytest.param('logo.gif', b'<svg onload=alert(1)>', id='gif-mit-SVG-Inhalt'),
    pytest.param('logo.webp', b'RIFF\x10\x00\x00\x00WAVEfmt ', id='webp-mit-WAV-Inhalt'),
    pytest.param('logo.png', b'\x89PNG', id='png-zu-kurz'),
]


@pytest.mark.parametrize('filename, content', NOT_AN_IMAGE)
def test_upload_refuses_what_is_not_an_image_and_writes_nothing(filename, content):
    answer, request = upload(filename, content)
    assert answer == {'success': False, 'error': LOGO_TYPE_ERROR}
    assert not os.path.exists(logos_dir()) or os.listdir(logos_dir()) == []
    assert request.rfile.read() == b''                        # der Body wurde gelesen (Keep-Alive bleibt ganz)
    assert request.close_connection is False


def test_the_refusals_are_plain_german_text():
    assert LOGO_TYPE_ERROR == 'Nur Bilddateien (PNG, JPG, GIF, WEBP) sind als Logo erlaubt.'
    assert LOGO_SIZE_ERROR == 'Das Logo ist zu groß (höchstens 5 MB).' and MAX_LOGO_BYTES == 5 * 1024 * 1024


def test_upload_without_a_file_part_says_so():
    body = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="x"\r\n\r\nhallo\r\n--{BOUNDARY}--\r\n'.encode()
    request = FakeRequest({'Content-Type': MULTIPART, 'Content-Length': str(len(body))}, body)
    assert json.loads(handlers.handle_logo_upload(request)) == {'success': False, 'error': 'Keine Datei empfangen'}


def test_upload_that_is_too_large_is_refused_without_reading_the_body():
    body = multipart_body('logo.png', image_bytes('.png') + b'\0' * MAX_LOGO_BYTES)
    assert len(body) > MAX_LOGO_BYTES
    request = FakeRequest({'Content-Type': MULTIPART, 'Content-Length': str(len(body))}, body)
    assert json.loads(handlers.handle_logo_upload(request)) == {'success': False, 'error': LOGO_SIZE_ERROR}
    assert request.rfile.tell() == 0                          # nichts gelesen
    assert request.close_connection is True                   # also wird die Verbindung geschlossen
    assert not os.path.exists(logos_dir())


def test_upload_exactly_at_the_limit_is_still_read():
    content = image_bytes('.png')
    padding = MAX_LOGO_BYTES - len(multipart_body('logo.png', content))
    assert padding > 0
    body = multipart_body('logo.png', content + b'\0' * padding)
    assert len(body) == MAX_LOGO_BYTES
    request = FakeRequest({'Content-Type': MULTIPART, 'Content-Length': str(len(body))}, body)
    assert json.loads(handlers.handle_logo_upload(request))['success'] is True
    assert request.rfile.tell() == len(body)


@pytest.mark.parametrize('length', [None, '', ' ', '0', '-5', 'abc', '1.5', '1e3', '12 34', '１２３', '+7', '0x10'])
def test_upload_with_a_missing_or_invalid_content_length_is_refused_without_reading_the_body(length):
    body = multipart_body('logo.png', image_bytes('.png'))
    headers = {'Content-Type': MULTIPART}
    if length is not None:
        headers['Content-Length'] = length
    request = FakeRequest(headers, body)
    assert json.loads(handlers.handle_logo_upload(request)) == {'success': False, 'error': 'Keine Datei empfangen'}
    assert request.rfile.tell() == 0 and request.close_connection is True
    assert not os.path.exists(logos_dir())


def test_upload_without_multipart_is_refused_without_reading_the_body():
    request = FakeRequest({'Content-Type': 'application/json', 'Content-Length': '2'}, b'{}')
    assert json.loads(handlers.handle_logo_upload(request)) == {'success': False, 'error': 'Kein Multipart-Upload'}
    assert request.rfile.tell() == 0 and request.close_connection is True


def test_a_failing_write_says_only_that_saving_failed_and_logs_the_details(tmp_path, capsys):
    blocker = os.path.join(userctx.DATA_ROOT, 'logos')
    write(blocker, b'ich bin eine Datei, kein Ordner')        # an dieser Stelle gehört der Ordner `logos` hin
    answer, _ = upload('logo.png', image_bytes('.png'))
    assert answer == {'success': False, 'error': 'Speichern fehlgeschlagen.'}
    assert str(tmp_path) not in json.dumps(answer) and 'logos' not in json.dumps(answer)
    assert 'logos' in capsys.readouterr().err                 # die Einzelheiten stehen im Server-Log (stderr)


# ── Upload über den echten Server ─────────────────────────────────────────────
def post_upload(port, csrf, filename, content):
    return send(port, 'POST', UPLOAD, {'Content-Type': MULTIPART, 'X-CSRF-Token': csrf},
                multipart_body(filename, content))


def test_over_http_a_png_is_stored_for_the_user_and_served_back(server_port, auto_login):
    data = image_bytes('.png')
    resp = post_upload(server_port, auto_login.csrf, 'Firmenlogo.png', data)
    assert resp.status == 200 and json.loads(resp.text) == {'success': True, 'path': 'data/logos/Firmenlogo.png'}
    assert os.path.isfile(os.path.join(admin_logos(), 'Firmenlogo.png'))
    served = get(server_port, '/data/logos/Firmenlogo.png')
    assert served.status == 200 and served.body == data
    assert served.headers['Content-Type'] == 'image/png' and served.headers['X-Content-Type-Options'] == 'nosniff'


@pytest.mark.parametrize('filename, content', NOT_AN_IMAGE)
def test_over_http_what_is_not_an_image_is_refused_and_nothing_is_written(
        server_port, auto_login, filename, content):
    resp = post_upload(server_port, auto_login.csrf, filename, content)
    assert resp.status == 200 and json.loads(resp.text) == {'success': False, 'error': LOGO_TYPE_ERROR}
    assert not os.path.exists(admin_logos()) or os.listdir(admin_logos()) == []


def test_over_http_the_size_is_judged_from_the_header_before_any_body_arrives(server_port, auto_login):
    """Der Client schickt nur die Kopfzeilen mit einer zu großen Content-Length und wartet auf die Antwort: Läse
    der Server den Body, bliebe er hier hängen. Danach schließt er die Verbindung (Connection: close)."""
    conn = http.client.HTTPConnection('127.0.0.1', server_port, timeout=TIMEOUT)
    try:
        conn.putrequest('POST', UPLOAD)
        conn.putheader('Content-Type', MULTIPART)
        conn.putheader('Content-Length', str(MAX_LOGO_BYTES + 1))
        conn.putheader('X-CSRF-Token', auto_login.csrf)
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 200 and json.loads(resp.read()) == {'success': False, 'error': LOGO_SIZE_ERROR}
        assert resp.will_close and resp.getheader('Connection') == 'close'
    finally:
        conn.close()
    assert not os.path.exists(admin_logos())


def test_over_http_a_request_without_content_length_is_refused_without_waiting_for_a_body(server_port, auto_login):
    conn = http.client.HTTPConnection('127.0.0.1', server_port, timeout=TIMEOUT)
    try:
        conn.putrequest('POST', UPLOAD)
        conn.putheader('Content-Type', MULTIPART)
        conn.putheader('X-CSRF-Token', auto_login.csrf)
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 200 and json.loads(resp.read()) == {'success': False, 'error': 'Keine Datei empfangen'}
        assert resp.will_close
    finally:
        conn.close()


def test_over_http_an_accepted_upload_keeps_the_connection_open_and_a_refusal_closes_it(server_port, auto_login):
    """Keep-Alive: Nach einem angenommenen Upload (Body gelesen) bleibt die Verbindung offen; nach einer Ablehnung,
    die den Body nicht las, schließt der Server sie – sonst würden die ungelesenen Bytes zur nächsten Anfrage."""
    conn = http.client.HTTPConnection('127.0.0.1', server_port, timeout=TIMEOUT)
    headers = {'Content-Type': MULTIPART, 'X-CSRF-Token': auto_login.csrf}
    try:
        conn.request('POST', UPLOAD, body=multipart_body('logo.png', image_bytes('.png')), headers=headers)
        first = conn.getresponse()
        assert json.loads(first.read())['success'] is True
        assert not first.will_close and first.getheader('Connection') is None
        conn.putrequest('POST', UPLOAD)
        for name, value in {**headers, 'Content-Length': str(MAX_LOGO_BYTES * 2)}.items():
            conn.putheader(name, value)
        conn.endheaders()
        second = conn.getresponse()
        assert json.loads(second.read())['success'] is False and second.will_close
    finally:
        conn.close()


# ── Auslieferung ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize('name, content_type', [
    ('x.png', 'image/png'), ('x.PNG', 'image/png'), ('x.jpg', 'image/jpeg'), ('x.jpeg', 'image/jpeg'),
    ('x.gif', 'image/gif'), ('x.webp', 'image/webp'), ('x.WebP', 'image/webp')])
def test_logos_of_the_positive_list_are_served_with_their_image_type(server_port, auto_login, name, content_type):
    write(os.path.join(admin_logos(), name), b'Bildbytes')
    resp = get(server_port, f'/data/logos/{name}')
    assert resp.status == 200 and resp.body == b'Bildbytes'
    assert resp.headers['Content-Type'] == content_type


@pytest.mark.parametrize('name', ['x.svg', 'x.SVG', 'x.html', 'x.htm', 'x.xml', 'x.txt', 'x.js', 'x.pdf', 'x.exe',
                                  'x', 'x.png.svg', 'x.svg.txt'])
def test_other_files_in_the_logos_folder_are_not_served_even_if_they_exist(server_port, auto_login, name):
    write(os.path.join(admin_logos(), name), b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    resp = get(server_port, f'/data/logos/{name}')
    assert resp.status == 404 and resp.text == 'Datei nicht gefunden.'
    assert b'<svg' not in resp.body


def test_a_link_in_the_logos_folder_is_not_followed_out_of_it_or_to_another_type(server_port, auto_login, tmp_path):
    outside = write(str(tmp_path / 'draussen' / 'geheim.png'), b'Geheimes Bild')
    link(outside, os.path.join(admin_logos(), 'fremd.png'))
    write(os.path.join(admin_logos(), 'echt.svg'), b'<svg/>')
    link(os.path.join(admin_logos(), 'echt.svg'), os.path.join(admin_logos(), 'getarnt.png'))
    assert get(server_port, '/data/logos/fremd.png').status == 404
    assert get(server_port, '/data/logos/getarnt.png').status == 404            # Ziel ist keine Bilddatei


def test_the_private_seed_folder_serves_only_listed_images(server_port, auto_login, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    private = tmp_path / 'seed_data' / 'private'
    write(str(private / 'logo.png'), b'Bild')
    write(str(private / 'logo.svg'), b'<svg/>')
    write(str(private / 'briefpapier.pdf'), b'%PDF')
    write(str(tmp_path / 'geheim.png'), b'nicht ausliefern')
    link(str(tmp_path / 'geheim.png'), str(private / 'draussen.png'))
    ok = get(server_port, '/seed_data/private/logo.png')
    assert ok.status == 200 and ok.body == b'Bild' and ok.headers['Content-Type'] == 'image/png'
    for path in ('logo.svg', 'briefpapier.pdf', 'draussen.png', 'fehlt.png', '../geheim.png', '%2e%2e/geheim.png'):
        assert get(server_port, f'/seed_data/private/{path}').status == 404, path


@pytest.mark.parametrize('path', [
    '/data/logos/a.html%00.png', '/data/logos/%00.png', '/data/logos/x.png%00', '/data/logos/%00',
    '/data/logos/a%00b.png?v=1', '/data/logos/..%00/x.png',
    '/seed_data/private/a.html%00.png', '/seed_data/private/%00.png', '/seed_data/private/x.png%00',
    '/seed_data/private/%00', '/seed_data/private/sub/a%00b.png?v=1'])
def test_a_nul_byte_in_the_path_is_a_404_and_not_a_server_error(server_port, auto_login, tmp_path, monkeypatch, path):
    """``os.path.realpath`` wirft bei einem NUL einen ValueError: Das gab eine 500er-Seite statt „nicht gefunden“."""
    monkeypatch.chdir(tmp_path)
    resp = get(server_port, path)
    assert resp.status == 404 and resp.text == 'Datei nicht gefunden.'
    assert 'Server Fehler' not in resp.text


# ── logo_on_server (Hinweis im Kontaktformular) folgt derselben Liste ─────────
def test_logo_on_server_follows_the_positive_list(tmp_path, monkeypatch):
    write(os.path.join(logos_dir(), 'da.png'))
    write(os.path.join(logos_dir(), 'da.svg'))
    assert logo_on_server('data/logos/da.png') is True
    assert logo_on_server('data/logos/da.svg') is False
    monkeypatch.chdir(tmp_path)
    write(str(tmp_path / 'seed_data' / 'private' / 'a.png'))
    write(str(tmp_path / 'seed_data' / 'private' / 'a.svg'))
    assert logo_on_server('/seed_data/private/a.png') is True
    assert logo_on_server('/seed_data/private/a.svg') is False


def test_logo_on_server_does_not_fail_on_a_nul_byte_in_the_stored_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for value in ('/seed_data/private/a\x00b.png', 'seed_data/private/a.html\x00.png', 'data/logos/a\x00b.png'):
        assert logo_on_server(value) is False, value


# ── resolve_logo_path ─────────────────────────────────────────────────────────
def test_the_file_name_is_resolved_in_the_logos_folder_of_the_user():
    target = write(os.path.join(logos_dir(), 'firma.png'))
    for value in ('firma.png', 'data/logos/firma.png', '/data/logos/firma.png', 'data\\logos\\firma.png',
                  ' data/logos/firma.png ', 'C:\\Irgendwo\\firma.png'):
        got = resolve_logo_path(value)
        assert got and os.path.samefile(got, target), value


def test_an_upper_case_extension_is_accepted():
    target = write(os.path.join(logos_dir(), 'firma.PNG'))
    got = resolve_logo_path('data/logos/firma.PNG')
    assert got and os.path.samefile(got, target)


def test_without_a_file_there_is_no_path():
    assert resolve_logo_path('data/logos/fehlt.png') is None
    assert resolve_logo_path('') is None and resolve_logo_path(None) is None
    assert resolve_logo_path('http://example.org/logo.png') is None
    assert resolve_logo_path('https://example.org/logo.png') is None
    os.makedirs(logos_dir())
    assert resolve_logo_path('data/logos/') is None and resolve_logo_path('data/logos') is None


def test_an_absolute_path_outside_the_logos_folder_is_not_used(tmp_path):
    outside = write(str(tmp_path / 'bilder' / 'logo.png'), image_bytes('.png'))
    os.makedirs(logos_dir())
    assert os.path.isfile(outside)
    assert resolve_logo_path(outside) is None                       # früher: gültig, weil die Datei existiert
    assert resolve_logo_path(outside.replace('/', '\\')) is None


def test_a_path_with_dots_leads_nowhere(tmp_path):
    write(str(tmp_path / 'logo.png'), image_bytes('.png'))
    write(str(tmp_path / 'data' / 'logo.png'), image_bytes('.png'))
    os.makedirs(logos_dir())
    for value in ('../logo.png', '../../logo.png', 'data/logos/../../logo.png', 'data/logos/../logo.png',
                  '..\\..\\logo.png', '/data/logos/../../../logo.png', '%2e%2e/logo.png'):
        assert resolve_logo_path(value) is None, value


def test_the_logo_of_another_user_is_not_found():
    installation.set_up(admin='alice', auto_login=False, users=[('bob', False)])
    bobs = write(os.path.join(userctx.DATA_ROOT, 'users', 'bob', 'logos', 'x.png'), image_bytes('.png'))
    userctx.set_user('alice')
    try:
        assert resolve_logo_path('data/users/bob/logos/x.png') is None
        assert resolve_logo_path(bobs) is None
        assert resolve_logo_path('x.png') is None
        userctx.set_user('bob')
        got = resolve_logo_path('data/logos/x.png')                   # jeder sieht nur seinen eigenen Ordner
        assert got and os.path.samefile(got, bobs)
    finally:
        userctx.clear()


def test_a_link_in_the_logos_folder_that_points_outside_is_not_used(tmp_path):
    outside = write(str(tmp_path / 'geheim' / 'bild.png'), image_bytes('.png'))
    link(outside, os.path.join(logos_dir(), 'logo.png'))
    assert resolve_logo_path('data/logos/logo.png') is None
    # Ein Link innerhalb des Ordners ist in Ordnung (und ein Link auf eine andere Art Datei nicht)
    real = write(os.path.join(logos_dir(), 'echt.png'), image_bytes('.png'))
    link(real, os.path.join(logos_dir(), 'kurz.png'))
    got = resolve_logo_path('data/logos/kurz.png')
    assert got and os.path.samefile(got, real)
    write(os.path.join(logos_dir(), 'echt.svg'))
    link(os.path.join(logos_dir(), 'echt.svg'), os.path.join(logos_dir(), 'getarnt.png'))
    assert resolve_logo_path('data/logos/getarnt.png') is None


def test_the_logos_folder_itself_may_be_a_link(tmp_path):
    """Ein NAS-Ordner als Link auf `logos` (DEPLOYMENT.md) bleibt benutzbar: geprüft wird gegen das Linkziel."""
    nas = tmp_path / 'nas' / 'logos'
    real = write(str(nas / 'firma.png'), image_bytes('.png'))
    link(str(nas), logos_dir())
    got = resolve_logo_path('data/logos/firma.png')
    assert got and os.path.samefile(got, real)


@pytest.mark.parametrize('name', ['logo.svg', 'logo.SVG', 'logo.html', 'logo.pdf', 'logo.exe', 'logo.bmp', 'logo.txt',
                                  'logo', 'logo.png.svg', 'logo.svg.txt'])
def test_a_file_with_another_extension_is_not_used_even_if_it_exists(name):
    write(os.path.join(logos_dir(), name), image_bytes('.png'))           # Pillow könnte sie sogar laden
    assert resolve_logo_path(f'data/logos/{name}') is None


def test_the_private_seed_folder_works_only_when_the_value_names_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    private = tmp_path / 'seed_data' / 'private'
    logo = write(str(private / 'logo.png'), image_bytes('.png'))
    write(str(private / 'logo.svg'))
    for value in ('seed_data/private/logo.png', '/seed_data/private/logo.png', 'seed_data\\private\\logo.png'):
        got = resolve_logo_path(value)
        assert got and os.path.samefile(got, logo), value
    assert resolve_logo_path('logo.png') is None                       # der Wert nennt den Ordner nicht
    assert resolve_logo_path('data/logos/logo.png') is None
    assert resolve_logo_path('seed_data/private/logo.svg') is None
    assert resolve_logo_path('seed_data/private/fehlt.png') is None
    write(str(tmp_path / 'draussen.png'), image_bytes('.png'))
    assert resolve_logo_path('seed_data/private/../../draussen.png') is None      # nur der Dateiname zählt
    link(str(tmp_path / 'draussen.png'), str(private / 'link.png'))
    assert resolve_logo_path('seed_data/private/link.png') is None                # Link nach außen


def test_the_own_logos_folder_wins_over_the_private_seed_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(str(tmp_path / 'seed_data' / 'private' / 'logo.png'), b'seed')
    own = write(os.path.join(logos_dir(), 'logo.png'), b'eigenes')
    got = resolve_logo_path('seed_data/private/logo.png')
    assert got and os.path.samefile(got, own)


def test_a_value_the_filesystem_cannot_hold_leads_to_no_path():
    os.makedirs(logos_dir())
    assert resolve_logo_path('data/logos/a\x00b.png') is None
    assert resolve_logo_path('x' * 5000 + '.png') is None


# ── Stundenzettel ─────────────────────────────────────────────────────────────
def _worktime_world(db, logo):
    db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Firma GmbH',
                      company_name='Eigene Firma GmbH', city='Musterstadt', logo=logo)
    person = db.insert_contact(contact_type='employee', entity_type='person', display_name='Erika Probe',
                               first_name='Erika', last_name='Probe')
    db.insert_worktime(person, '2026-03-10', start_time='08:00', end_time='16:00')
    return person


def test_the_worktime_pdf_takes_the_logo_from_the_logos_folder_via_resolve_logo_path(tmp_db, monkeypatch):
    write(os.path.join(logos_dir(), 'firma.png'), image_bytes('.png'))
    person = _worktime_world(tmp_db, 'data/logos/firma.png')
    calls = []
    real = pdf_worktime.resolve_logo_path
    monkeypatch.setattr(pdf_worktime, 'resolve_logo_path', lambda logo: calls.append(logo) or real(logo))
    pdf, _ = generate_worktime_pdf(tmp_db, person, '2026-03-01', '2026-03-31', store=False)
    assert calls == ['data/logos/firma.png']
    assert pdf.startswith(b'%PDF') and b'/Subtype /Image' in pdf and b'/Logo' in pdf


def test_the_worktime_pdf_has_no_logo_for_a_path_outside_the_logos_folder(tmp_db, tmp_path):
    outside = write(str(tmp_path / 'bilder' / 'firma.png'), image_bytes('.png'))
    person = _worktime_world(tmp_db, outside)
    pdf, _ = generate_worktime_pdf(tmp_db, person, '2026-03-01', '2026-03-31', store=False)
    assert pdf.startswith(b'%PDF') and b'/Subtype /Image' not in pdf       # kein Logo, aber auch kein Fehler


@pytest.mark.parametrize('pillow_format', ['TIFF', 'EPS', 'PSD'])
def test_the_worktime_pdf_has_no_logo_for_a_tiff_eps_or_psd_file_named_png(tmp_db, pillow_format):
    write(os.path.join(logos_dir(), 'firma.png'), other_format(pillow_format))
    person = _worktime_world(tmp_db, 'data/logos/firma.png')
    assert resolve_logo_path('data/logos/firma.png')           # die Endung stimmt: bis hierher ist es „ein Logo“
    pdf, _ = generate_worktime_pdf(tmp_db, person, '2026-03-01', '2026-03-31', store=False)
    assert pdf.startswith(b'%PDF') and b'/Subtype /Image' not in pdf         # das PDF entsteht ohne Logo und ohne Fehler


def test_the_worktime_pdf_has_no_logo_for_an_svg_file(tmp_db):
    write(os.path.join(logos_dir(), 'firma.svg'), image_bytes('.png'))
    person = _worktime_world(tmp_db, 'data/logos/firma.svg')
    pdf, _ = generate_worktime_pdf(tmp_db, person, '2026-03-01', '2026-03-31', store=False)
    assert b'/Subtype /Image' not in pdf
