"""
Tests für die größenbasierte Rotation der SQL-Audit-Logs
(document_parser: _rotate_if_needed / compress_rotated_log).

- Rotation ab SQL_LOG_MAX_BYTES, Archiv mit Zeitstempel-Namen
- Kompression: 7-Zip falls installiert, sonst gzip-Fallback
- Archive werden nie gelöscht, das aktive Log beginnt frisch

Inhalte sind erfundene Test-SQL-Statements.
"""
import gzip
import os
import time
import uuid

import pytest

import document_parser
from document_parser import DocumentParser, compress_rotated_log


def _archives(log_dir, stem):
    # Rotierte Dateien heißen JJJJMMTT-HHMMSS_<stem>.<ext>[.7z|.gz]
    return [f for f in os.listdir(log_dir)
            if ('_' + stem + '.') in f and f[0].isdigit()]


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def parser(tmp_path):
    return DocumentParser(data_dir=str(tmp_path / 'Belege'), log_dir=str(tmp_path))


def test_no_rotation_below_threshold(parser, tmp_path):
    parser.log_sql("INSERT INTO Bookings (Text) VALUES (?)", ("Testeintrag",), "unit test")
    files = os.listdir(tmp_path)
    assert 'sql_operations.log' in files
    assert 'sql_operations.sql' in files
    assert _archives(str(tmp_path), 'sql_operations') == []


def test_rotation_creates_archive_and_fresh_log(parser, tmp_path, monkeypatch):
    monkeypatch.setattr(document_parser, 'SQL_LOG_MAX_BYTES', 2000)

    payload = uuid.uuid4().hex * 4   # ~128 Zeichen pro Statement
    for _ in range(60):
        parser.log_sql(f"INSERT INTO Bookings (Text) VALUES ('{payload}')", (), "rotation test")

    log_dir = str(tmp_path)
    # Kompression läuft im Hintergrund-Thread → auf Archiv warten
    assert _wait_for(lambda: len(_archives(log_dir, 'sql_operations')) >= 1), \
        f"Kein Archiv entstanden: {os.listdir(log_dir)}"
    # Nach Abschluss: keine unkomprimierten Rotate-Reste
    assert _wait_for(lambda: all(f.endswith(('.7z', '.gz'))
                                 for f in _archives(log_dir, 'sql_operations'))), \
        f"Unkomprimierte Reste: {os.listdir(log_dir)}"
    # Aktives Log existiert weiter und ist unter der Schwelle + einem Eintrag
    assert os.path.getsize(os.path.join(log_dir, 'sql_operations.log')) < 4000


def test_gzip_fallback_without_sevenzip(tmp_path, monkeypatch):
    monkeypatch.setattr(document_parser, '_sevenzip_path', None)  # 7-Zip "nicht installiert"
    src = tmp_path / '20990101-000000_sql_operations.log'
    content = ("INSERT INTO Bookings (Text) VALUES ('Fallback-Test');\n" * 100).encode('utf-8')
    src.write_bytes(content)

    result = compress_rotated_log(str(src))

    assert result == str(src) + '.gz'
    assert not src.exists()
    with gzip.open(result, 'rb') as f:
        assert f.read() == content


def test_sevenzip_compression_if_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(document_parser, '_sevenzip_path', '')  # Cache leeren → neu suchen
    if document_parser._find_sevenzip() is None:
        pytest.skip("7-Zip nicht installiert")
    src = tmp_path / '20990101-000000_sql_operations.log'
    src.write_bytes(b"INSERT INTO Bookings (Text) VALUES ('7z-Test');\n" * 100)

    result = compress_rotated_log(str(src))

    assert result == str(src) + '.7z'
    assert os.path.isfile(result)
    assert not src.exists()
