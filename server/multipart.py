# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Robustes Parsing von multipart/form-data über den stdlib-email-Parser.

Ersetzt das fragile manuelle ``body.split(b'--' + boundary)``. Der MIME-Parser
behandelt die kniffligen Fälle korrekt, an denen manuelles Splitting scheitert:
- Boundary-Bytes, die zufällig im Datei-Inhalt vorkommen,
- fehlendes/zusätzliches Trailing-CRLF vor der Boundary,
- in Anführungszeichen gesetzte Boundary (``boundary="..."``) im Content-Type,
- RFC-2231/2047-kodierte Dateinamen.

Hinweis: ``cgi.FieldStorage`` ist in Python 3.13 entfernt – daher email-Parser.
"""
from email.parser import BytesParser
from email.policy import default


class Part:
    """Ein Teil eines multipart/form-data-Bodies."""
    __slots__ = ("name", "filename", "content")

    def __init__(self, name, filename, content):
        self.name = name            # Formularfeld-Name (str|None)
        self.filename = filename    # Original-Dateiname (str|None), undekodiert
        self.content = content      # Roh-Bytes des Teils

    @property
    def is_file(self):
        return self.filename is not None


def parse_multipart(content_type, body):
    """Zerlegt einen multipart/form-data-Body.

    Args:
        content_type: vollständiger Content-Type-Header-Wert (str).
        body: Roh-Body als bytes.

    Returns:
        list[Part] – leere Liste, wenn kein multipart/form-data vorliegt.
    """
    if not content_type or 'multipart/form-data' not in content_type.lower():
        return []

    # Der email-Parser erwartet die Header vor dem Body. Content-Type enthält
    # die Boundary; latin-1 ist für Header-Bytes verlustfrei.
    prefix = b'Content-Type: ' + content_type.encode('latin-1') + b'\r\n\r\n'
    msg = BytesParser(policy=default).parsebytes(prefix + body)

    if not msg.is_multipart():
        return []

    parts = []
    for sub in msg.iter_parts():
        if sub.get_content_disposition() != 'form-data':
            continue
        name = sub.get_param('name', header='content-disposition')
        filename = sub.get_filename()
        payload = sub.get_payload(decode=True)
        parts.append(Part(name, filename, payload if payload is not None else b''))
    return parts


def first_file(content_type, body):
    """Liefert den ersten Datei-Part (mit filename) oder None."""
    for part in parse_multipart(content_type, body):
        if part.is_file:
            return part
    return None
