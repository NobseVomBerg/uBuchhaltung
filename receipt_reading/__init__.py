# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belegerkennung: Rechnungsfelder aus den Bytes einer hochgeladenen Belegdatei lesen.

Ein reines Lesepaket: Es bekommt Bytes, öffnet keine Dateien, schreibt nichts und kennt weder ``server/`` noch ``db/``.
Die Eingaben sind nicht vertrauenswürdig: Jede Grenze steht hier als Konstante (die Tests verkleinern sie), und
:func:`read_document` wirft nie – was schiefgeht, ist ein :class:`Reading` mit Status ``failed``, ohne Ausnahmetext.

Wege (``Reading.source``):

* ``einvoice_cii`` / ``einvoice_ubl`` – E-Rechnung (:mod:`receipt_reading.einvoice`): XRechnung-XML oder das XML in einer
  ZUGFeRD-/Factur-X-PDF (:mod:`receipt_reading.pdf_embedded`). Die Felder stehen im Dokument; gelesen wird nur, was
  streng passt, alles andere bleibt leer.
* ``text`` – gewöhnliche PDFs über Textregeln (:mod:`receipt_reading.pdf_text`, :mod:`receipt_reading.text_rules`), Anschluss ist
  :func:`_read_pdf_text`. Lieber leer als geraten: Jedes gelesene Feld nennt die Zeile, aus der es stammt.

Eine Lesung ist ein Vorschlag, kein Befund: Der Aufrufer zeigt sie mit ihrer Herkunft an und übernimmt nichts ohne
Bestätigung. Beträge sind ganzzahlige Minor Units der App (``money.SCALE`` = 4, also 0,0001 € – nicht Cent).

:func:`read_document` liest im eigenen Prozess; :func:`receipt_reading.isolated.read_isolated` führt dasselbe in einem Kindprozess
mit hartem Zeitlimit aus (``receipt_reading.worker``) – so liest der Server hochgeladene Dateien.

Befund-Schlüssel (``Reading.notes``, feste Codes; sie landen in ``raw['notes']``):

=========================  ==================================================================================
``xml_dtd``                das XML trägt eine DTD oder Entity-Deklaration und wurde nicht gelesen
``xml_invalid``            das XML ist nicht wohlgeformt
``xml_too_large``          das XML ist größer als ``MAX_XML_BYTES``
``not_einvoice``           wohlgeformtes XML, aber keine unterstützte E-Rechnung (UBL Invoice/CreditNote, CII)
``encrypted``              die PDF ist verschlüsselt: kein Anhang gelesen; braucht sie ein Passwort, auch kein
                           Text (``unsupported``)
``pdf_invalid``            die PDF ließ sich nicht lesen
``too_large``              der Anhang der PDF ist entpackt größer als ``MAX_XML_BYTES``
``attachment_unreadable``  ein Anhang mit dem Namen einer E-Rechnung ließ sich nicht sicher entpacken
``einvoice_empty``         die E-Rechnung im Anhang nennt kein lesbares Feld: gelesen wurde der Text der PDF
``text_truncated``         der Text der PDF wurde gekürzt (Zeit oder ``MAX_EXTRACT_CHARS``): nur der Anfang ist gelesen
``statement``              ein Kontoauszug, kein Beleg (``unsupported``); der Text bleibt gespeichert
``foreign_currency``       Beträge in einer anderen Währung als Euro: die Beträge bleiben leer
``by_checksum``            Beträge waren mehrdeutig; genau eine Kombination besteht die Summenprobe
``conflict_<feld>``        der Beleg nennt mehrere verschiedene Werte für das Feld: es bleibt leer
``bad_<feld>``             ein Wert stand im Dokument, passte aber nicht streng: das Feld bleibt leer
``time``                   das Lesen im Kindprozess hat das Zeitlimit überschritten (``too_large``)
``reader_crashed``         der Kindprozess ließ sich nicht starten oder endete mit einem Fehler (``failed``)
``reader_output``          die Ausgabe des Kindprozesses war nicht die einer Lesung (``failed``)
``read_error``             beim Lesen trat eine Ausnahme auf (``failed``)
``raw_truncated``          ``raw`` musste gekürzt werden
=========================  ==================================================================================
"""
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from money import FACTOR, MAX_PARSED_AMOUNT

READER_VERSION = 1

# Grenzen – bei jedem Aufruf gelesen, nie als Standardwert gebunden: Die Tests verkleinern sie per monkeypatch.
MAX_READ_BYTES = 30 * 1024 * 1024          # eine Datei, die darüber liegt, wird nicht gelesen
MAX_XML_BYTES = 20 * 1024 * 1024           # ein XML (auch als Anhang entpackt); wie importers.camt.MAX_XML_BYTES
MAX_EXTRACT_CHARS = 200_000                # aus einer PDF gelesener Text (gespeichert werden höchstens ``MAX_TEXT_CHARS``)
MAX_TEXT_CHARS = 20_000                    # gespeicherter Text
MAX_RAW_BYTES = 64 * 1024                  # ``raw`` samt ``notes`` als JSON in UTF-8
READ_TIMEOUT = 15                          # Sekunden, hart: so lange darf ``isolated.read_isolated`` höchstens warten

NAME_LIMIT = 200                           # Absender
VAT_ID_LIMIT = 20                          # USt-IdNr.
NUMBER_LIMIT = 100                         # Rechnungsnummer
RAW_STRING_LIMIT = 200                     # jeder Text in ``raw`` (auch Zitate) und jeder Befund-Schlüssel
RAW_LIST_LIMIT = 20                        # Einträge je Liste in ``raw`` (Steueraufschlüsselung)
RAW_KEYS_LIMIT = 100                       # Schlüssel je Objekt in ``raw``
RAW_DEPTH_LIMIT = 6                        # Verschachtelung in ``raw``
NOTES_LIMIT = 50                           # Befunde je Lesung

STATUSES = ('ok', 'empty', 'no_text', 'unsupported', 'too_large', 'failed')
SOURCES = ('einvoice_cii', 'einvoice_ubl', 'text')
KINDS = ('invoice', 'credit_note')

_MAX_MINOR = int(MAX_PARSED_AMOUNT) * FACTOR     # Beträge einer Lesung: betragsmäßig unter einer Billion Euro
_ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_CURRENCY = re.compile(r'[A-Z]{3}')
_DROPPED_CATEGORIES = frozenset({'Cc', 'Cf', 'Cs'})


def clean_text(value, limit: int) -> Optional[str]:
    """``value`` ohne Steuerzeichen und unsichtbare Formatzeichen (Unicode-Kategorien ``Cc`` außer dem Zeilenumbruch,
    ``Cf`` und – damit der Text sich als UTF-8 speichern lässt – ``Cs``), ohne Leerraum am Rand, höchstens ``limit``
    Zeichen. ``None``, wenn es kein Text ist oder nichts bleibt. Gearbeitet wird nur an einem Anfang des Textes
    (hochgeladene Dateien können sehr lange Werte tragen)."""
    if not isinstance(value, str):
        return None
    kept = ''.join(char for char in value[:limit * 4 + 64]
                   if char == '\n' or unicodedata.category(char) not in _DROPPED_CATEGORIES)
    return kept.strip()[:limit].rstrip() or None


# ── raw ──────────────────────────────────────────────────────────────────────

def _clean_raw(value, depth: int, state: dict):
    """``value`` als JSON-taugliche Struktur: nur Text, Zahlen, Wahrheitswerte, ``None``, Listen und Objekte mit Textschlüsseln;
    Texte bereinigt und gekürzt, Listen und Objekte begrenzt. Was dabei wegfällt, setzt ``state['truncated']``."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return clean_text(value, RAW_STRING_LIMIT) or ''
    if depth >= RAW_DEPTH_LIMIT:
        state['truncated'] = True
        return None
    if isinstance(value, (list, tuple)):
        if len(value) > RAW_LIST_LIMIT:
            state['truncated'] = True
        return [_clean_raw(item, depth + 1, state) for item in value[:RAW_LIST_LIMIT]]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if len(cleaned) >= RAW_KEYS_LIMIT:
                state['truncated'] = True
                break
            if isinstance(key, str):
                cleaned[clean_text(key, RAW_STRING_LIMIT) or ''] = _clean_raw(item, depth + 1, state)
        return cleaned
    return None


def _without_quotes(value):
    """``value`` ohne die Zitate (Schlüssel ``quote``) in jeder Tiefe."""
    if isinstance(value, dict):
        return {key: _without_quotes(item) for key, item in value.items() if key != 'quote'}
    if isinstance(value, list):
        return [_without_quotes(item) for item in value]
    return value


def _raw_size(raw: dict, notes: list) -> int:
    return len(json.dumps({**raw, 'notes': notes}, ensure_ascii=False, sort_keys=True).encode('utf-8'))


def _normalize_raw(raw, notes) -> tuple[dict, list]:
    """``(raw, notes)`` in der Form, in der sie gespeichert werden: bereinigt, begrenzt, und samt ``notes`` als JSON höchstens
    ``MAX_RAW_BYTES`` groß. Ist es zu groß, fallen die Zitate und die Steueraufschlüsselung weg, notfalls alles außer den
    Befunden; jede Kürzung steht als ``raw_truncated`` in den Befunden. ``notes`` gehört nie in ``raw``."""
    state = {'truncated': False}
    cleaned = _clean_raw(raw, 0, state) if isinstance(raw, dict) else {}
    cleaned.pop('notes', None)
    given = list(notes) if isinstance(notes, (list, tuple)) else []
    if len(given) > NOTES_LIMIT:
        state['truncated'] = True
    kept = [text for text in (clean_text(note, RAW_STRING_LIMIT) for note in given[:NOTES_LIMIT]) if text]
    if state['truncated'] and 'raw_truncated' not in kept:
        kept.append('raw_truncated')
    if _raw_size(cleaned, kept) > MAX_RAW_BYTES:
        cleaned.pop('tax_breakdown', None)
        cleaned = _without_quotes(cleaned)
        if 'raw_truncated' not in kept:
            kept.append('raw_truncated')
        if _raw_size(cleaned, kept) > MAX_RAW_BYTES:
            cleaned = {}
    return cleaned, kept


# ── Lesung ───────────────────────────────────────────────────────────────────

_FIELD_NAMES = ('status', 'source', 'doc_date', 'gross_minor', 'net_minor', 'tax_minor', 'tax_rate', 'currency',
                'sender_name', 'sender_vat_id', 'invoice_number', 'kind', 'raw', 'text', 'notes')


def _check_minor(value):
    if value is not None and (type(value) is not int or abs(value) >= _MAX_MINOR):
        raise ValueError('Reading: kein gültiger Betrag')
    return value


def _check_text(value, limit: int):
    if value is not None and value != clean_text(value, limit):
        raise ValueError('Reading: kein gültiger Text')
    return value


def _check_date(value):
    if value is None:
        return None
    if isinstance(value, str) and _ISO_DATE.fullmatch(value) and 1900 <= int(value[:4]) <= 2200:
        try:
            date.fromisoformat(value)
            return value
        except ValueError:
            pass
    raise ValueError('Reading: kein gültiges Datum')


@dataclass
class Reading:
    """Das Ergebnis des Lesens einer Datei.

    ``status``: ``ok`` (mindestens ein Feld gelesen), ``empty`` (ein lesbares Dokument ohne lesbares Feld), ``no_text``
    (ein Bild: kein Text, keine OCR), ``unsupported`` (kein Dokument, das dieses Paket kennt), ``too_large`` und ``failed``.
    ``source``: ``einvoice_cii``, ``einvoice_ubl`` oder ``text``. Beträge in Minor Units (``money``), ``tax_rate`` als Anteil
    (0,19) – nur, wenn das Dokument genau einen Satz nennt. ``raw`` hält Herkunft und Proben (JSON-fähig), ``notes`` die
    Befunde (feste Codes, siehe Kopf des Pakets)."""
    status: str
    source: Optional[str] = None
    doc_date: Optional[str] = None
    gross_minor: Optional[int] = None
    net_minor: Optional[int] = None
    tax_minor: Optional[int] = None
    tax_rate: Optional[float] = None
    currency: Optional[str] = None
    sender_name: Optional[str] = None
    sender_vat_id: Optional[str] = None
    invoice_number: Optional[str] = None
    kind: Optional[str] = None
    raw: dict = field(default_factory=dict)
    text: Optional[str] = None
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """Die Lesung als JSON-fähiges Dict (Ausgabe eines Kindprozesses), bereinigt und gekürzt wie in :meth:`to_row`;
        :meth:`from_dict` ist das Gegenstück."""
        raw, notes = _normalize_raw(self.raw, self.notes)
        return {
            'status': self.status, 'source': self.source, 'doc_date': self.doc_date,
            'gross_minor': self.gross_minor, 'net_minor': self.net_minor, 'tax_minor': self.tax_minor,
            'tax_rate': self.tax_rate, 'currency': self.currency,
            'sender_name': clean_text(self.sender_name, NAME_LIMIT),
            'sender_vat_id': clean_text(self.sender_vat_id, VAT_ID_LIMIT),
            'invoice_number': clean_text(self.invoice_number, NUMBER_LIMIT),
            'kind': self.kind, 'raw': raw, 'text': clean_text(self.text, MAX_TEXT_CHARS), 'notes': notes,
        }

    @classmethod
    def from_dict(cls, data) -> 'Reading':
        """Strenge Gegenprüfung für die Ausgabe eines Kindprozesses (:meth:`to_dict`): Typen, Wertebereiche, erlaubter Status,
        erlaubte Quelle und Art, Längen, Bereinigung – alles Unpassende ist ein ``ValueError`` ohne Inhalt der Eingabe. Was
        :meth:`to_dict` bereinigen oder kürzen würde, ist hier ein Fehler: Die Eingabe muss schon in der gespeicherten Form
        sein."""
        if not isinstance(data, dict) or set(data) - set(_FIELD_NAMES):
            raise ValueError('Reading: unbekannte Felder')
        status, source, kind = data.get('status'), data.get('source'), data.get('kind')
        if status not in STATUSES or (source is not None and source not in SOURCES) or (
                kind is not None and kind not in KINDS):
            raise ValueError('Reading: Status, Quelle oder Art nicht erlaubt')
        rate = data.get('tax_rate')
        if rate is not None:
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
                raise ValueError('Reading: kein gültiger Steuersatz')
            rate = float(rate)
        currency = data.get('currency')
        if currency is not None and not (isinstance(currency, str) and _CURRENCY.fullmatch(currency)):
            raise ValueError('Reading: keine gültige Währung')
        raw, notes = data.get('raw', {}), data.get('notes', [])
        if not isinstance(raw, dict) or not isinstance(notes, list):
            raise ValueError('Reading: raw oder notes nicht in der gespeicherten Form')
        normalized = _normalize_raw(raw, notes)
        if normalized != (raw, notes):
            raise ValueError('Reading: raw oder notes nicht in der gespeicherten Form')
        raw, notes = normalized                                    # gleich, aber eigene Objekte: nichts wird mit dem Aufrufer geteilt
        return cls(
            status=status, source=source, doc_date=_check_date(data.get('doc_date')),
            gross_minor=_check_minor(data.get('gross_minor')), net_minor=_check_minor(data.get('net_minor')),
            tax_minor=_check_minor(data.get('tax_minor')), tax_rate=rate, currency=currency,
            sender_name=_check_text(data.get('sender_name'), NAME_LIMIT),
            sender_vat_id=_check_text(data.get('sender_vat_id'), VAT_ID_LIMIT),
            invoice_number=_check_text(data.get('invoice_number'), NUMBER_LIMIT), kind=kind, raw=raw,
            text=_check_text(data.get('text'), MAX_TEXT_CHARS), notes=notes)

    def to_row(self) -> dict:
        """Die Spalten der Lesung in der Datenbank (ohne ``ReadAt``: Den Zeitpunkt setzt der Aufrufer). Texte sind
        bereinigt und gekürzt (Name 200, USt-IdNr. 20, Nummer 100, Text ``MAX_TEXT_CHARS`` Zeichen), ``Raw`` ist JSON
        (``raw`` samt ``notes``) und höchstens ``MAX_RAW_BYTES`` groß."""
        data = self.to_dict()
        return {
            'Status': data['status'], 'Source': data['source'], 'ReaderVersion': READER_VERSION,
            'DocDate': data['doc_date'], 'GrossMinor': data['gross_minor'], 'NetMinor': data['net_minor'],
            'TaxMinor': data['tax_minor'], 'TaxRate': data['tax_rate'], 'Currency': data['currency'],
            'SenderName': data['sender_name'], 'SenderVatId': data['sender_vat_id'],
            'InvoiceNumber': data['invoice_number'], 'Kind': data['kind'],
            'Raw': json.dumps({**data['raw'], 'notes': data['notes']}, ensure_ascii=False, sort_keys=True),
            'Text': data['text'],
        }


# ── Einstieg ─────────────────────────────────────────────────────────────────

def _joined(*groups) -> list:
    """Die Befunde mehrerer Gruppen hintereinander, jeder nur einmal."""
    return list(dict.fromkeys(note for group in groups for note in group))


def _is_statement(name, text: str) -> bool:
    """Ist die Datei ein Kontoauszug? ``importers.find_importer`` entscheidet, welches Importmodul zuständig wäre – aber es erkennt
    schon den Namen einer Bank im Text (``volksbank``, ``dkb``) oder ``vbr`` im Dateinamen, und der steht im Fuß jeder Rechnung
    eines Lieferanten dieser Bank. Ein Auszug trägt außerdem seine Salden (``importers.pdftext.find_balances``: „alter/neuer
    Kontostand“ mit Betrag), eine Rechnung nicht."""
    return find_importer(str(name or ''), text) is not None and find_balances(text) != (None, None)


def _read_pdf_text(name, data: bytes, own, deadline, notes: list) -> Reading:
    """Eine gewöhnliche PDF über ihren Text lesen (:func:`pdf_text.extract_pdf_text`, :func:`text_rules.read_text`). ``notes``:
    die Befunde des E-Rechnungs-Wegs (kein Anhang, ein unlesbarer Anhang …), die in die Lesung gehören. Ein Scan ohne Text ist
    ``no_text``, eine PDF, die ein Passwort braucht, ``unsupported`` (``encrypted``), eine kaputte ``failed`` (``pdf_invalid``);
    ein Kontoauszug ist ``unsupported`` (``statement``) – sein Text bleibt gespeichert."""
    pages, hint = pdf_text.extract_pdf_text(data, MAX_EXTRACT_CHARS, deadline)
    if hint == 'no_text':
        return Reading(status='no_text', notes=_joined(notes))
    if hint == 'encrypted':
        return Reading(status='unsupported', notes=_joined(notes, [hint]))
    if hint == 'pdf_invalid':
        return Reading(status='failed', notes=_joined(notes, [hint]))
    truncated = [hint] if hint else []
    text = '\n'.join(pages)
    if _is_statement(name, text):
        return Reading(status='unsupported', text=clean_text(text, MAX_TEXT_CHARS), notes=_joined(notes, truncated, ['statement']))
    reading = text_rules.read_text(pages, own)
    reading.notes = _joined(notes, truncated, reading.notes)
    return reading


def _read_pdf(name, data: bytes, own, deadline) -> Reading:
    notes = []
    xml, hint = pdf_embedded.find_embedded_einvoice(data)
    if hint:
        notes.append(hint)
    if xml is not None:
        reading = einvoice.read_einvoice_xml(xml)
        if reading.status == 'ok':
            reading.notes = _joined(notes, reading.notes)
            return reading
        notes.extend(reading.notes)           # unlesbar, keine E-Rechnung oder ohne lesbares Feld: weiter mit dem Text der PDF
        if reading.status == 'empty':
            notes.append('einvoice_empty')
    return _read_pdf_text(name, data, own, deadline, _joined(notes))


def read_document(name: str, data: bytes, own: tuple[list[str], list[str]], deadline: Optional[float] = None) -> Reading:
    """Eine hochgeladene Datei lesen: ``name`` (Endung und Kennbytes müssen zusammenpassen, ``detect.kind``), ``data`` ihre
    Bytes. XML wird als E-Rechnung gelesen, eine PDF zuerst über ein eingebettetes XML (ZUGFeRD, Factur-X), sonst über den
    Textweg, ein Bild ist ``no_text``, alles andere ``unsupported``; über ``MAX_READ_BYTES`` ist es ``too_large``.

    ``own``: zwei Listen mit Angaben der eigenen Firmen – nur der Textweg braucht sie; die E-Rechnung nennt ihren Verkäufer
    selbst. ``deadline``: Zeitpunkt (``time.monotonic()``), bis zu dem der Textweg fertig sein muss; die E-Rechnung liest keine
    Uhr. Wirft nie: Jede Ausnahme ist ``failed`` ohne Ausnahmetext."""
    try:
        if len(data) > MAX_READ_BYTES:
            return Reading(status='too_large')
        kind = detect.kind(name, data)
        if kind == 'xml':
            return einvoice.read_einvoice_xml(data)
        if kind == 'pdf':
            return _read_pdf(name, data, own, deadline)
        return Reading(status='no_text' if kind == 'image' else 'unsupported')
    except Exception:
        return Reading(status='failed', notes=['read_error'])


# Die Teilmodule lesen Reading, die Grenzen und clean_text von hier: erst nach deren Definition laden.
from . import detect, einvoice, pdf_embedded, pdf_text, text_rules  # noqa: E402
from importers import find_importer  # noqa: E402
from importers.pdftext import find_balances  # noqa: E402

__all__ = ['READER_VERSION', 'Reading', 'read_document', 'clean_text', 'STATUSES', 'SOURCES', 'KINDS']
