"""Tests für das robuste multipart/form-data-Parsing (server/multipart.py)."""
from server.multipart import parse_multipart, first_file


def _build(boundary, parts):
    """Baut einen multipart-Body. parts: Liste von (headers:str, content:bytes)."""
    out = b''
    b = boundary.encode()
    for headers, content in parts:
        out += b'--' + b + b'\r\n'
        out += headers.encode() + b'\r\n\r\n'
        out += content + b'\r\n'
    out += b'--' + b + b'--\r\n'
    return out


def test_single_file():
    body = _build("XB", [
        ('Content-Disposition: form-data; name="csvfile"; filename="t.csv"',
         b'a;b;c\r\n1;2;3'),
    ])
    parts = parse_multipart('multipart/form-data; boundary=XB', body)
    assert len(parts) == 1
    assert parts[0].name == 'csvfile'
    assert parts[0].filename == 't.csv'
    assert parts[0].content == b'a;b;c\r\n1;2;3'
    assert parts[0].is_file


def test_text_field_and_file_mixed():
    body = _build("ZZ", [
        ('Content-Disposition: form-data; name="kind"', b'wiso'),
        ('Content-Disposition: form-data; name="f"; filename="x.csv"', b'DATA'),
    ])
    parts = parse_multipart('multipart/form-data; boundary=ZZ', body)
    assert [p.name for p in parts] == ['kind', 'f']
    assert parts[0].is_file is False
    assert parts[0].content == b'wiso'
    assert parts[1].filename == 'x.csv'


def test_boundary_bytes_inside_content_are_preserved():
    # Inhalt enthält Bytes, die wie eine Boundary-Zeile aussehen – manuelles
    # split(b'--'+boundary) würde hier falsch trennen.
    tricky = b'line1\r\n--XB not really a boundary\r\nline2'
    body = _build("XB", [
        ('Content-Disposition: form-data; name="f"; filename="a.bin"', tricky),
    ])
    parts = parse_multipart('multipart/form-data; boundary=XB', body)
    assert len(parts) == 1
    assert parts[0].content == tricky


def test_quoted_boundary_in_content_type():
    body = _build("My Boundary 123", [
        ('Content-Disposition: form-data; name="f"; filename="a.csv"', b'X'),
    ])
    parts = parse_multipart('multipart/form-data; boundary="My Boundary 123"', body)
    assert len(parts) == 1
    assert parts[0].content == b'X'


def test_binary_content_with_crlf_and_nulls():
    blob = bytes(range(256)) * 2  # enthält \r \n \x00 etc.
    body = _build("BB", [
        ('Content-Disposition: form-data; name="f"; filename="x.pdf"', blob),
    ])
    parts = parse_multipart('multipart/form-data; boundary=BB', body)
    assert parts[0].content == blob


def test_non_multipart_returns_empty():
    assert parse_multipart('application/json', b'{}') == []
    assert parse_multipart('', b'') == []


def test_first_file_skips_text_fields():
    body = _build("QQ", [
        ('Content-Disposition: form-data; name="a"', b'text'),
        ('Content-Disposition: form-data; name="up"; filename="d.csv"', b'CSV'),
    ])
    part = first_file('multipart/form-data; boundary=QQ', body)
    assert part is not None and part.filename == 'd.csv' and part.content == b'CSV'


def test_first_file_none_when_no_file():
    body = _build("QQ", [('Content-Disposition: form-data; name="a"', b'text')])
    assert first_file('multipart/form-data; boundary=QQ', body) is None
