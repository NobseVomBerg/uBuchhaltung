# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Das eingebettete XML einer ZUGFeRD-/Factur-X-PDF finden – und nie mehr entpacken, als erlaubt ist.

Eine hybride E-Rechnung trägt ihr XML als eingebettete Datei: im Katalog unter ``AF`` (Associated Files) und im Namensbaum
``Names/EmbeddedFiles`` – so schreibt es auch die App (``export/pdfa.py``). Gesucht wird nach dem Namen der Datei
(``factur-x.xml``, ``zugferd-invoice.xml``, ``xrechnung.xml``). pdfminer liest nur die Struktur; den Datenstrom des Anhangs
entpackt :func:`_bounded_inflate` selbst und begrenzt – ``PDFStream.get_data()`` entpackte beliebig viel, und eine
Zip-Bombe im Anhang füllte den Speicher. (Querverweis- und Objektströme entpackt pdfminer beim Lesen der Struktur weiter
selbst; dagegen schützen nur ``MAX_READ_BYTES`` und die Grenzen des Prozesses, in dem gelesen wird.)
"""
import io
import itertools
import re
import zlib

from pdfminer.pdfdocument import PDFDocument, PDFEncryptionError
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import LITERALS_FLATE_DECODE, PDFObjRef, PDFStream, list_value, resolve1
from pdfminer.utils import decode_text

import receipt_reading as package

#: Dateinamen (klein, ohne Pfad), unter denen ein E-Rechnungs-XML in einer PDF liegt
EINVOICE_NAMES = frozenset({'factur-x.xml', 'zugferd-invoice.xml', 'xrechnung.xml'})
MAX_TREE_DEPTH = 8        # Tiefe des Namensbaums (``Kids``)
MAX_ENTRIES = 100         # so viele eingebettete Dateien werden höchstens angesehen
MAX_ATTEMPTS = 3          # so viele Anhänge mit dem Namen einer E-Rechnung werden höchstens entpackt (jeder Versuch kostet bis zu MAX_XML_BYTES)

_PATH_SEPARATOR = re.compile(r'[\\/]')


class _TooLarge(Exception):
    """Der Anhang ist entpackt größer als die Grenze."""


def _bounded_inflate(stream: PDFStream, limit: int):
    """Der Inhalt des Datenstroms, höchstens ``limit`` Bytes lang – oder ``None``, wenn er sich nicht sicher entpacken lässt:
    eine Kette aus mehreren Filtern, ein anderer Filter als Flate, Flate mit ``Predictor`` in den Parametern, kaputte oder
    abgeschnittene Daten. Ohne Filter zählt die Länge der Rohdaten. Ist der Inhalt größer als ``limit``, wird
    :class:`_TooLarge` geworfen – entpackt wird nie weiter als ``limit + 1`` Bytes, nie über ``stream.get_data()``."""
    filters = stream.get_filters()
    raw = stream.get_rawdata()
    if not isinstance(raw, bytes):
        return None
    if not filters:
        if len(raw) > limit:
            raise _TooLarge
        return raw
    if len(filters) != 1:
        return None
    name, params = filters[0]
    if name not in LITERALS_FLATE_DECODE or (params is not None and not isinstance(params, dict)) or (
            params and 'Predictor' in params):
        return None
    inflater = zlib.decompressobj()
    try:
        content = inflater.decompress(raw, limit + 1)
    except zlib.error:
        return None
    if len(content) > limit or inflater.unconsumed_tail:
        raise _TooLarge
    return content if inflater.eof else None


def _base_name(spec: dict) -> str:
    """Der Name der Datei (klein, ohne Pfad) aus ``UF``, sonst ``F``; leer, wenn die Angabe fehlt."""
    for key in ('UF', 'F'):
        value = resolve1(spec.get(key))
        if isinstance(value, bytes) and value:
            return _PATH_SEPARATOR.split(decode_text(value))[-1].lower()
    return ''


def _embedded_stream(spec: dict):
    files = resolve1(spec.get('EF'))
    if isinstance(files, dict):
        for key in ('F', 'UF'):
            stream = resolve1(files.get(key))
            if isinstance(stream, PDFStream):
                return stream
    return None


def _tree_values(node, depth: int, visited: set):
    """Die Werte der ``Names``-Paare eines Namensbaums, danach die seiner ``Kids`` – höchstens ``MAX_TREE_DEPTH`` tief, jedes
    Knotenobjekt höchstens einmal (ein Knoten, der auf einen Vorgänger zeigt, ist ein Kreis; viele Verweise auf denselben
    Knoten wären sonst eine Explosion) und höchstens ``MAX_ENTRIES`` Knoten (viele leere Knoten ergäben keine Werte, die
    ``_find`` zählt)."""
    node = resolve1(node)
    if depth > MAX_TREE_DEPTH or not isinstance(node, dict):
        return
    names = list_value(node.get('Names'))
    for index in range(1, len(names), 2):
        yield names[index]
    for kid in list_value(node.get('Kids')):
        if isinstance(kid, PDFObjRef):
            if kid.objid in visited:
                continue
            if len(visited) >= MAX_ENTRIES:
                return
            visited.add(kid.objid)
        yield from _tree_values(kid, depth + 1, visited)


def _candidates(catalog: dict):
    yield from list_value(catalog.get('AF'))
    names = resolve1(catalog.get('Names'))
    if isinstance(names, dict):
        yield from _tree_values(names.get('EmbeddedFiles'), 0, set())


def _find(catalog: dict):
    problem = None
    attempts = 0
    tried = set()                                   # Datenströme (``objid``), die schon entpackt wurden
    for item in itertools.islice(_candidates(catalog), MAX_ENTRIES):
        spec = resolve1(item)
        if not isinstance(spec, dict) or _base_name(spec) not in EINVOICE_NAMES:
            continue
        stream = _embedded_stream(spec)
        if stream is not None:
            if stream.objid is not None and stream.objid in tried:
                continue                            # dieselbe Datei, die ``AF`` und der Namensbaum beide nennen: zählt einmal
            if attempts >= MAX_ATTEMPTS:
                break                               # der bisherige Befund bleibt: ``attachment_unreadable``
            attempts += 1
            if stream.objid is not None:
                tried.add(stream.objid)
            content = _bounded_inflate(stream, package.MAX_XML_BYTES)
            if content is not None:
                return content, None
        problem = problem or 'attachment_unreadable'
    return None, problem


def find_embedded_einvoice(data: bytes) -> tuple[bytes | None, str | None]:
    """``(XML-Bytes, None)``, wenn die PDF ein E-Rechnungs-XML eingebettet trägt; ``(None, None)``, wenn nicht; sonst
    ``(None, Hinweis)``: ``encrypted`` (verschlüsselte PDF – nichts wird entschlüsselt), ``pdf_invalid`` (jede Ausnahme von
    pdfminer), ``too_large`` (der Anhang ist entpackt größer als ``MAX_XML_BYTES``) und ``attachment_unreadable`` (ein
    Anhang mit dem Namen einer E-Rechnung, der sich nicht sicher entpacken lässt). Der erste Anhang mit passendem Namen,
    der sich lesen lässt, gilt – es werden höchstens ``MAX_ATTEMPTS`` Anhänge entpackt (dieselbe Datei über ``AF`` und den
    Namensbaum zählt einmal), danach endet die Suche mit dem bisherigen Befund; der Inhalt ist ungeprüft –
    :func:`receipt_reading.einvoice.read_einvoice_xml` prüft ihn."""
    try:
        document = PDFDocument(PDFParser(io.BytesIO(data)))
        if document.encryption is not None:
            return None, 'encrypted'
        return _find(document.catalog)
    except _TooLarge:
        return None, 'too_large'
    except PDFEncryptionError:
        return None, 'encrypted'
    except Exception:
        return None, 'pdf_invalid'
