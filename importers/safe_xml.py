# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""XML aus hochgeladenen Dateien sicher lesen.

Gemeinsamer Wächter für das CAMT-Lesen (``importers.camt``) und das Lesen von E-Rechnungen
(``receipt_reading``): Ein Expat-Durchlauf bricht jede DTD und jede Entity-Deklaration ab (egal in welcher Kodierung das
Dokument steht), erst danach baut ElementTree den Baum. So gibt es weder eine Entity-Bombe noch einen Zugriff auf
Dateien oder das Netz. Die Größe wird vor dem ersten Lesen geprüft.
"""
import xml.etree.ElementTree as ET
from xml.parsers import expat


class UnsafeXml(ValueError):
    """Das XML wird nicht gelesen. ``reason``: ``too_large`` (über der Größengrenze), ``dtd`` (DTD oder
    Entity-Deklaration) oder ``invalid`` (nicht wohlgeformt; ``detail`` nennt den Befund des Parsers)."""

    def __init__(self, reason: str, detail: str = ''):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _forbid(*_args):
    raise UnsafeXml('dtd')


def parse_untrusted_xml(data: bytes, max_bytes: int) -> ET.Element:
    """Wurzelelement von ``data``; höchstens ``max_bytes`` Bytes, ohne DTD und Entity-Deklaration, sonst
    :class:`UnsafeXml`."""
    if len(data) > max_bytes:
        raise UnsafeXml('too_large')
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _forbid
    guard.EntityDeclHandler = _forbid
    try:
        guard.Parse(data, True)
        return ET.fromstring(data)
    except UnsafeXml:
        raise
    except (ET.ParseError, expat.ExpatError) as error:
        raise UnsafeXml('invalid', str(error)) from error
