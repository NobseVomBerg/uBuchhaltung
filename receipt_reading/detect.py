# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Art einer hochgeladenen Datei: Endung des Namens UND Kennbytes müssen zusammenpassen.

Der Name allein sagt nichts (eine ``.pdf``, die ein Programm oder eine Webseite ist), die Kennbytes allein auch nicht (ein
``.xml`` mit PDF-Inhalt): Nur beides zusammen gilt. Die Rasterbilder sind die der Positivliste der Logos
(``export.pdf_core.LOGO_CONTENT_TYPES``: PNG, JPG, GIF, WEBP – kein SVG).
"""
import os
import re

from export.pdf_core import logo_content_matches, logo_extension

_HEAD = 1024                                   # so viel vom Anfang genügt für Kennbytes, BOM und Leerraum
_LEADING_SPACE = re.compile(rb'[ \t\r\n]*')
_UTF16_BOMS = (b'\xff\xfe', b'\xfe\xff')


def _starts_like_xml(head: bytes) -> bool:
    """Beginnt der Text – nach einer Byte-Order-Mark und Leerraum – mit ``<``?"""
    if head.startswith(b'\xef\xbb\xbf'):
        head = head[3:]
    elif head.startswith(_UTF16_BOMS):
        return head.decode('utf-16', 'ignore').lstrip(' \t\r\n').startswith('<')
    return head[_LEADING_SPACE.match(head).end():][:1] == b'<'


def kind(name, data: bytes) -> str:
    """``'pdf'``, ``'xml'``, ``'image'`` oder ``'other'``. PDF: Endung ``.pdf`` und der Anfang ``%PDF-``; XML: Endung
    ``.xml`` und ein Anfang mit ``<`` (nach BOM und Leerraum); Bild: Endung und Kennbytes der Logo-Positivliste."""
    head = bytes(data[:_HEAD])
    extension = os.path.splitext(str(name or ''))[1].lower()
    if extension == '.pdf':
        return 'pdf' if head.startswith(b'%PDF-') else 'other'
    if extension == '.xml':
        return 'xml' if _starts_like_xml(head) else 'other'
    image = logo_extension(name)
    if image and logo_content_matches(image, head):
        return 'image'
    return 'other'
