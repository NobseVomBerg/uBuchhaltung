# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Belege lesen (seit 2.6.0): die Datei EINES Belegs lesen und die Lesung in ``DocumentReadings`` ablegen – ohne HTTP.

Der Upload liest nie selbst; gelesen wird auf Klick, je Anfrage ein Beleg (``read_receipt``, ``POST /receipts/read``). Die Lesung
steht nur in ``DocumentReadings``: Nummer, Datum, Dateiname und Info des Belegs bleiben, wie der Nutzer sie pflegt.

``read_receipt`` geht in dieser Reihenfolge vor: Demo-Modus (abgeschaltet) → eine Sperre je Prozess (``_read_lock``: es läuft nur
ein Lesen zurzeit, ein zweites bekommt sofort ``busy``) → Beleg aus der Datenbank → die Datei NUR über ``receipt_files.open_receipt``
(``Belege/``/``Documents/``, kein Link, reguläre Datei) → Größe → Bytes lesen, Datei schließen → Lesen im Kindprozess
(``receipt_reading.isolated.read_isolated``) – alles AUSSERHALB einer Transaktion der Datenbank → die Lesung schreiben
(``Database.save_document_reading``: nur, wenn der Beleg noch da ist und denselben Pfad trägt wie beim Lesen). Jede Ablehnung und jeder
Fehler ist ein fester Satz, und nie steht ein Pfad, der Name der Datei auf der Platte, der ganze Text der Datei (``Text``) oder der Text
einer Ausnahme in der Antwort; die Zitate der gelesenen Zeilen (je höchstens 200 Zeichen) gehören zur Lesung in der Antwort.

``reading_payload`` ist die Kurzform einer gespeicherten Lesung für Antwort, Liste und Seite des Belegs: Texte fertig formatiert,
Befunde als feste Sätze (ein unbekannter Befund-Schlüssel erscheint nie), ``Raw`` streng gelesen (kaputt heißt leer).
"""
import datetime
import json
import threading
import traceback
from decimal import Decimal

import demo
import receipt_reading
from db.invoice_versions import local_datetime
from money import format_de
from receipt_reading import Reading, isolated
from . import receipt_files
from .period import parse_iso_date

READ_DEMO_REFUSED = 'Im Demo-Modus ist das Lesen von Belegen abgeschaltet.'
READ_BUSY = 'Es wird gerade ein anderer Beleg gelesen – bitte gleich noch einmal.'
READ_FAILED = 'Der Beleg konnte nicht gelesen werden – Einzelheiten stehen im Server-Log.'
READ_CHANGED = 'Der Beleg wurde während des Lesens geändert oder gelöscht – bitte noch einmal lesen.'
READ_NOT_FOUND = receipt_files.NOT_FOUND

READ_BATCH = 100                       # so viele ungelesene Belege bietet die Liste auf einmal zum Lesen an
NO_FILE_NOTE = 'no_file'               # Befund dieser Schicht: der Beleg hat keine Datei, die sich öffnen lässt
SENDER_LIST_LIMIT = 30                 # so viele Zeichen des Absenders zeigt die Liste höchstens
OUTDATED_SUFFIX = ' (ältere Regeln)'
UNKNOWN_STATUS = 'unbekannter Stand'
_CHUNK = 1024 * 1024

STATUS_LABELS = {
    'ok': 'gelesen',
    'empty': 'nichts gefunden',
    'no_text': 'nicht lesbar (kein Text – Scan oder Bild)',
    'unsupported': 'nicht lesbar (Dateityp)',
    'too_large': 'nicht gelesen (zu groß oder zu aufwendig)',
    'failed': 'Lesen fehlgeschlagen',
}
SOURCE_LABELS = {
    'einvoice_cii': 'aus der E-Rechnung',
    'einvoice_ubl': 'aus der E-Rechnung',
    'text': 'aus dem Text gelesen',
}
KIND_LABELS = {'invoice': 'Rechnung', 'credit_note': 'Gutschrift'}

# Feld der Lesung → Anzeigename, in der Reihenfolge der Anzeige
FIELD_LABELS = {
    'doc_date': 'Belegdatum', 'gross_minor': 'Brutto', 'net_minor': 'Netto', 'tax_minor': 'Steuer', 'tax_rate': 'Steuersatz',
    'currency': 'Währung', 'sender_name': 'Absender', 'sender_vat_id': 'USt-IdNr.', 'invoice_number': 'Rechnungsnummer',
    'kind': 'Art',
}
_FIELD_COLUMNS = {
    'doc_date': 'DocDate', 'gross_minor': 'GrossMinor', 'net_minor': 'NetMinor', 'tax_minor': 'TaxMinor', 'tax_rate': 'TaxRate',
    'currency': 'Currency', 'sender_name': 'SenderName', 'sender_vat_id': 'SenderVatId', 'invoice_number': 'InvoiceNumber',
    'kind': 'Kind',
}

_BASE_NOTES = {
    'xml_dtd': 'Die XML-Datei trägt eine DTD oder eine Entity-Deklaration und wurde nicht gelesen.',
    'xml_invalid': 'Die XML-Datei ist nicht wohlgeformt.',
    'xml_too_large': 'Die XML-Datei ist zu groß und wurde nicht gelesen.',
    'not_einvoice': 'Die XML-Datei ist keine unterstützte E-Rechnung (XRechnung/UBL oder ZUGFeRD/CII).',
    'encrypted': 'Die PDF-Datei ist verschlüsselt – ein eingebettetes XML wurde nicht gelesen.',
    'pdf_invalid': 'Die PDF-Datei ließ sich nicht lesen.',
    'too_large': 'Der Anhang der PDF-Datei ist entpackt zu groß und wurde nicht gelesen.',
    'attachment_unreadable': 'Ein Anhang der PDF-Datei mit dem Namen einer E-Rechnung ließ sich nicht sicher entpacken.',
    'einvoice_empty': 'Die E-Rechnung im Anhang der PDF-Datei nennt kein lesbares Feld – gelesen wurde der Text der PDF-Datei.',
    'text_truncated': 'Der Text der PDF-Datei wurde gekürzt – nur der Anfang ist gelesen.',
    'statement': 'Die Datei ist ein Kontoauszug, kein Beleg.',
    'foreign_currency': 'Die Beträge stehen in einer anderen Währung als Euro und wurden nicht gelesen.',
    'by_checksum': 'Die Beträge waren mehrdeutig – genau eine Kombination besteht die Summenprobe.',
    'time': 'Das Lesen hat zu lange gedauert und wurde abgebrochen.',
    'reader_crashed': 'Der Leser ließ sich nicht starten oder endete mit einem Fehler.',
    'reader_output': 'Die Ausgabe des Lesers war keine brauchbare Lesung.',
    'read_error': 'Beim Lesen trat ein Fehler auf.',
    'raw_truncated': 'Die Einzelheiten der Lesung wurden gekürzt.',
    NO_FILE_NOTE: 'Zu diesem Beleg liegt keine Datei vor, oder sie ließ sich nicht öffnen.',
}
_BAD_LABELS = {**FIELD_LABELS, 'tax_breakdown': 'Steueraufschlüsselung'}
# ``conflict_<Feld>`` und ``bad_<Feld>`` (``receipt_reading``): je Feld ein fester Satz
NOTE_TEXTS = {
    **_BASE_NOTES,
    **{f'conflict_{name}': f'Der Beleg nennt mehrere verschiedene Werte für „{label}“ – das Feld bleibt leer.'
       for name, label in FIELD_LABELS.items()},
    **{f'bad_{name}': f'Der Wert für „{label}“ stand im Beleg, passte aber nicht streng – das Feld bleibt leer.'
       for name, label in _BAD_LABELS.items()},
}
SUM_CHECK_TEXTS = {'ok': 'Netto + Steuer = Brutto: stimmt', 'mismatch': 'Netto + Steuer = Brutto: stimmt nicht'}
RATE_CHECK_TEXTS = {'ok': 'Steuer passt zum Satz: stimmt', 'mismatch': 'Steuer passt zum Satz: stimmt nicht'}

# Es läuft höchstens EIN Lesen je Prozess: Das Lesen belegt einen Kindprozess mit bis zu ``READ_TIMEOUT`` Sekunden und bis zu
# 30 MB im Speicher – die Anfragen der Seite kommen nacheinander, eine zweite gleichzeitige bekommt sofort ``busy``.
_read_lock = threading.Lock()


def _now():
    """Der Zeitpunkt der Lesung: UTC, ``JJJJ-MM-TT HH:MM:SS`` (wie ``CURRENT_TIMESTAMP``). Ein Haken für die Tests."""
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _error(text):
    return {'success': False, 'error': text}


def _read_up_to(source, limit):
    """Höchstens ``limit`` Bytes aus ``source``, in Stücken bis zum Ende der Datei (ein einziges ``read`` einer ungepufferten
    Datei muss nicht alles liefern)."""
    parts, total = [], 0
    while total < limit:
        chunk = source.read(min(_CHUNK, limit - total))
        if not chunk:
            break
        parts.append(chunk)
        total += len(chunk)
    return b''.join(parts)


def _read_file(db, stored_path):
    """Die Datei, auf die ``stored_path`` zeigt, lesen: die Spalten der Lesung ohne ``ReadAt`` (``Reading.to_row``). Ohne Datei, die
    sich öffnen lässt, ist es eine Lesung ``failed`` mit dem Befund ``no_file``; eine Datei über ``MAX_READ_BYTES`` wird nicht
    gelesen (``too_large``). Die Grenze wird bei jedem Aufruf gelesen. Hier läuft nichts in einer Transaktion der Datenbank."""
    try:
        opened = receipt_files.open_receipt(stored_path)
    except OSError:
        opened = None
    if opened is None:
        return Reading(status='failed', notes=[NO_FILE_NOTE]).to_row()
    limit = receipt_reading.MAX_READ_BYTES
    try:
        data = None if opened.size > limit else _read_up_to(opened.source, limit + 1)
    finally:
        opened.source.close()
    if data is None or len(data) > limit:           # zu groß – oder nach dem Öffnen gewachsen
        return Reading(status='too_large').to_row()
    return isolated.read_isolated(opened.name, data, db.own_identity()).to_row()


def read_receipt(db, document_id):
    """Den Beleg ``document_id`` lesen und die Lesung speichern. Returns ``(Status, Antwort als dict)``:

    * 403 – Demo-Modus (nichts geschieht), 409 mit ``busy: true`` – es läuft schon ein Lesen (nichts geschieht),
    * 404 – es gibt den Beleg nicht, 409 – der Beleg oder sein Pfad hat sich während des Lesens geändert (nichts gespeichert),
    * 500 – ein unerwarteter Fehler (``READ_FAILED``, der Traceback steht nur im Server-Log),
    * 200 ``{"success": true, "reading": …}`` (``reading_payload``) – auch wenn die Lesung ``failed``, ``no_text`` … ist: Sie ist
      gespeichert, ihr Status sagt, was geschah."""
    if demo.is_active():
        return 403, _error(READ_DEMO_REFUSED)
    if not _read_lock.acquire(blocking=False):
        return 409, {'success': False, 'busy': True, 'error': READ_BUSY}
    try:
        receipt = db.get_receipt_by_id(document_id)
        if receipt is None:
            return 404, _error(READ_NOT_FOUND)
        stored_path = receipt[4]                    # Documents: ID, Number, Date, Filename, Path, Info
        row = _read_file(db, stored_path)
        row['ReadAt'] = _now()
        if not db.save_document_reading(document_id, stored_path, row):
            return 409, _error(READ_CHANGED)
        return 200, {'success': True, 'reading': reading_payload(db.get_document_reading(document_id))}
    except Exception:
        traceback.print_exc()                       # der volle Fehler nur ins Server-Log, nie in die Antwort
        return 500, _error(READ_FAILED)
    finally:
        _read_lock.release()


# ── Anzeige ──────────────────────────────────────────────────────────────────

def _text(value):
    """``value``, wenn es ein nicht leerer Text ist, sonst ``None``."""
    return value if isinstance(value, str) and value.strip() else None


def amount_text(minor, currency=None):
    """Ein Betrag in Minor Units als Anzeigetext (``1.234,56 EUR``; ohne Währung nur die Zahl) – ``None``, wenn es keine ganze Zahl ist."""
    if isinstance(minor, bool) or not isinstance(minor, int):
        return None
    try:
        text = format_de(minor)
    except ArithmeticError:                         # eine Zahl, die kein Leser geschrieben haben kann (SQL-Konsole)
        return None
    code = _text(currency)
    return f'{text} {code.strip()}' if code else text


def _rate_text(rate):
    """Der Steuersatz (Anteil, 0,19) als ``19 %`` – ``None``, wenn es kein Anteil von 0 bis 1 ist."""
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
        return None
    return format((Decimal(str(rate)) * 100).normalize(), 'f').replace('.', ',') + ' %'


def _german_date(day):
    return datetime.date.fromisoformat(day).strftime('%d.%m.%Y')


def _is_outdated(version):
    return isinstance(version, bool) or not isinstance(version, int) or version < receipt_reading.READER_VERSION


def _raw(row):
    """``Raw`` einer gespeicherten Lesung, streng gelesen: ein Objekt – kaputtes JSON oder etwas anderes ist leer."""
    try:
        data = json.loads(row.get('Raw') or '')
    except (TypeError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _origin(raw, name):
    """Woher ein Feld stammt: ``{'quote': Zeile}`` (Textregeln) und/oder ``{'ref': BT-Bezeichner}`` (E-Rechnung)."""
    fields = raw.get('fields')
    entry = fields.get(name) if isinstance(fields, dict) else None
    if not isinstance(entry, dict):
        return {}
    limit = receipt_reading.RAW_STRING_LIMIT
    return {key: entry[key][:limit] for key in ('quote', 'ref') if _text(entry.get(key))}


def _display(name, row):
    value = row.get(_FIELD_COLUMNS[name])
    if name == 'doc_date':
        day = parse_iso_date(value)
        return _german_date(day) if day else None
    if name in ('gross_minor', 'net_minor', 'tax_minor'):
        return amount_text(value, row.get('Currency'))
    if name == 'tax_rate':
        return _rate_text(value)
    if name == 'kind':
        return KIND_LABELS.get(value)
    return _text(value)


def reading_payload(row):
    """Eine gespeicherte Lesung (``Database.get_document_reading``) als JSON-fähige Kurzform – ``None`` ohne Lesung:

    ``status`` und ``statusLabel``, ``sourceLabel`` (leer, wenn die Herkunft unbekannt ist), ``readAt`` (Ortszeit ``TT.MM.JJJJ HH:MM``,
    leer, wenn sich der Zeitpunkt nicht lesen lässt), ``outdated`` (von einem älteren Leser), ``fields`` (``{label, value}`` je
    gelesenem Feld, Werte fertig formatiert, dazu ``quote`` – die Zeile des Belegs – oder ``ref`` – der BT-Bezeichner der E-Rechnung),
    ``checks`` (die Summenprobe, dann die Satzprobe der Textregeln, je als fester Satz und nur, wenn sie ein Ergebnis hat – ``incomplete``
    und alles Unbekannte ergibt keinen), ``notes`` (feste Sätze zu den Befunden; ein unbekannter Schlüssel erscheint nie) und ``docDate``
    (ISO, nur ein gültiges Datum). Nie der ganze Text der Datei (``Text``) – aber die Zitate der gelesenen Zeilen (``quote``, je höchstens
    ``receipt_reading.RAW_STRING_LIMIT`` Zeichen)."""
    if not isinstance(row, dict):
        return None
    raw = _raw(row)
    status = row.get('Status')
    status = status if status in STATUS_LABELS else 'unknown'
    fields = []
    for name, label in FIELD_LABELS.items():
        value = _display(name, row)
        if value is not None:
            fields.append({'label': label, 'value': value, **_origin(raw, name)})
    checks_raw = raw.get('checks')
    checks = []
    if isinstance(checks_raw, dict):
        for name, texts in (('sum', SUM_CHECK_TEXTS), ('rate', RATE_CHECK_TEXTS)):
            verdict = checks_raw.get(name)
            if isinstance(verdict, str) and verdict in texts:
                checks.append(texts[verdict])
    notes = []
    given = raw.get('notes')
    for code in (given[:receipt_reading.NOTES_LIMIT] if isinstance(given, list) else []):
        text = NOTE_TEXTS.get(code) if isinstance(code, str) else None
        if text and text not in notes:
            notes.append(text)
    moment = local_datetime(row.get('ReadAt'))
    source = row.get('Source')
    return {
        'status': status, 'statusLabel': STATUS_LABELS.get(status, UNKNOWN_STATUS),
        'sourceLabel': SOURCE_LABELS.get(source, '') if isinstance(source, str) else '',
        'readAt': moment.strftime('%d.%m.%Y %H:%M') if moment else '',
        'outdated': _is_outdated(row.get('ReaderVersion')),
        'fields': fields, 'checks': checks, 'notes': notes, 'docDate': parse_iso_date(row.get('DocDate')),
    }


def summary_text(summary):
    """Der Text der Spalte „Gelesen“ in der Liste der Belege (``Database.document_reading_summaries``: ``(Status, Brutto, Währung,
    Absender, Version des Lesers)``): bei einer gelesenen Datei Betrag und Absender (höchstens ``SENDER_LIST_LIMIT`` Zeichen; was
    fehlt, entfällt; ohne beides ``gelesen``), sonst das Wort zum Status – mit ``OUTDATED_SUFFIX``, wenn ein älterer Leser gelesen
    hat. Reiner Text: Die Seite maskiert ihn."""
    status, gross, currency, sender, version = summary
    if status == 'ok':
        name = _text(sender)
        name = name.strip() if name else None
        if name and len(name) > SENDER_LIST_LIMIT:
            name = name[:SENDER_LIST_LIMIT - 1] + '…'
        text = ' · '.join(part for part in (amount_text(gross, currency), name) if part) or STATUS_LABELS['ok']
    else:
        text = STATUS_LABELS.get(status, UNKNOWN_STATUS) if isinstance(status, str) else UNKNOWN_STATUS
    return text + (OUTDATED_SUFFIX if _is_outdated(version) else '')
