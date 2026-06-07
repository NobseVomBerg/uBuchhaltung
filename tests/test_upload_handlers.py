"""Integrationstest: Multipart-Upload-Pfad der Handler (nach Umstellung auf
den robusten email-Parser). Prüft, dass der Datei-Inhalt korrekt aus dem
Multipart-Body extrahiert und an den Importer übergeben wird.
"""
import io
import os

from server.handlers import handle_wiso_import

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


class _FakeRequest:
    """Minimaler Ersatz für den BaseHTTPRequestHandler im Test."""
    def __init__(self, content_type, body):
        self.headers = {
            'Content-Type': content_type,
            'Content-Length': str(len(body)),
        }
        self.rfile = io.BytesIO(body)


def _multipart(boundary, field_name, filename, content):
    b = boundary.encode()
    return (
        b'--' + b + b'\r\n'
        + f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode()
        + b'\r\nContent-Type: text/csv\r\n\r\n'
        + content
        + b'\r\n--' + b + b'--\r\n'
    )


def test_wiso_import_extracts_file_from_multipart(db_with_coa, monkeypatch, tmp_path):
    # Schreibvorgänge (data/wiso_import_result.json) in temporäres CWD lenken.
    monkeypatch.chdir(tmp_path)

    with open(os.path.join(FIXTURES, 'wiso_original.csv'), 'rb') as f:
        csv_bytes = f.read()

    body = _multipart('BNDRY', 'csvfile', 'wiso_original.csv', csv_bytes)
    req = _FakeRequest('multipart/form-data; boundary=BNDRY', body)

    status, location = handle_wiso_import(req, db_with_coa)

    assert status == 303
    assert 'wiso_import=ok' in location
    assert 'imported=8' in location   # entspricht der direkten Importzahl der Fixture


def test_wiso_import_no_file_returns_error(db_with_coa, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Multipart ohne Datei-Part (nur ein Textfeld)
    body = (b'--B\r\nContent-Disposition: form-data; name="x"\r\n\r\nhello\r\n--B--\r\n')
    req = _FakeRequest('multipart/form-data; boundary=B', body)

    status, location = handle_wiso_import(req, db_with_coa)
    assert status == 303
    assert 'wiso_import=error' in location
    assert 'Keine+Datei' in location
