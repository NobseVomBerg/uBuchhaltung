# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
File upload handler for receipts and bank statements.

Liefert eine JSON-Antwort mit einer Import-Vorschau pro Beleg, damit die
Transaktionsseite den Import inline (ohne Seitenwechsel) anzeigen kann
(``handle_file_upload``, ``POST /upload_receipts``).

Die Belege-Seite hat ihren eigenen Weg (``handle_receipt_upload``, ``POST /receipts/upload``): Jede Datei wird ein Beleg –
auch ein Kontoauszug –, ohne Import-Vorschau.
"""
import os
import json
import traceback

from . import receipt_files
from .import_preview import build_import_preview
from .multipart import parse_multipart

try:
    from document_parser import DocumentExists, DocumentParser
    PARSER_AVAILABLE = True
except ImportError:
    PARSER_AVAILABLE = False

# Was die Antwort je Datei sagt, wenn das Ablegen scheitert: ein fester Satz. Der Text einer Systemausnahme nennt Serverpfade –
# er steht nur im Server-Log. Klartext tragen allein eigene Klassen (``CamtError``, ``DkbCsvError``, ``DocumentExists``).
UPLOAD_FAILED = 'Die Datei konnte nicht verarbeitet werden – Einzelheiten stehen im Server-Log.'

# Beleg-Upload der Belege-Seite: Die Antwort nennt je Datei nur diese festen Sätze (nie den Text einer Ausnahme, nie einen Pfad)
RECEIPTS_DEMO_REFUSED = 'Im Demo-Modus ist das Hochladen von Belegen abgeschaltet.'
RECEIPT_UPLOAD_FAILED = 'Die Datei konnte nicht als Beleg gespeichert werden – Einzelheiten stehen im Server-Log.'
RECEIPT_EMPTY_FILE = 'Die Datei ist leer.'
RECEIPT_INVALID_REQUEST = 'Ungültige Anfrage.'
RECEIPT_TOO_MANY_FILES = 'Höchstens 100 Dateien je Upload.'
RECEIPT_STATEMENT_HINT = ('Kontoauszug erkannt und als Beleg abgelegt – '
                          'Umsätze werden auf der Seite „Buchungen“ importiert.')
RECEIPT_NO_NUMBER_HINT = 'Kein brauchbarer Nummernkreis „Belegnummern Firma“ – der Beleg hat keine Nummer.'
MAX_RECEIPT_FILES = 100             # so viele Dateien nimmt EIN Upload der Belege-Seite höchstens an


def _collect_camt(name, content, statement_files, other_files):
    """CAMT.053-Upload auswerten und wie einen PDF-Kontoauszug einreihen."""
    from importers import check_statement
    from importers.camt import CamtError, parse_upload
    try:
        parsed = parse_upload(name, content)
    except CamtError as e:
        other_files.append({'filename': name, 'status': 'error', 'error': str(e)})
        return
    for label, statement in parsed:
        data = check_statement(statement).as_dict()
        data['filename'] = label
        statement_files.append(data)


def _collect_csv(name, content, statement_files, other_files):
    """DKB-Umsatzliste (CSV) auswerten und wie einen PDF-Kontoauszug einreihen – nur im Speicher, nie auf der Platte.

    Was keine DKB-Umsatzliste ist oder sich nicht lesen lässt, wird nicht als Beleg abgelegt, sondern als Warnung gemeldet:
    mit dem festen Satz der Importschicht (``DkbCsvError``), nie mit dem Text einer anderen Ausnahme (der steht im Log)."""
    from importers import check_statement
    from importers.dkb_csv import UNREADABLE, DkbCsvError, parse_csv
    try:
        statement = parse_csv(content)
    except DkbCsvError as e:
        other_files.append({'filename': name, 'status': 'warning', 'error': str(e)})
        return
    except Exception:
        import traceback
        traceback.print_exc()                       # der volle Fehler nur ins Server-Log, nie in die Antwort
        other_files.append({'filename': name, 'status': 'warning', 'error': UNREADABLE})
        return
    data = check_statement(statement).as_dict()
    data['filename'] = name
    statement_files.append(data)


def handle_file_upload(request_handler, db):
    """Multipart-Upload von Belegen / Kontoauszügen verarbeiten.

    Returns (status_code, json_string):
        { "import_id": str|None,
          "files": [ <Beleg-Vorschau>, ... ],
          "other_files": [ {filename, status, ...}, ... ] }
    """
    import userctx
    upload_dir = os.path.join(userctx.user_data_dir(), "Documents")
    os.makedirs(upload_dir, exist_ok=True)

    content_type = request_handler.headers.get('Content-Type') or ''
    if 'multipart/form-data' not in content_type:
        return 400, json.dumps({'error': 'Ungültiger Content-Type'})

    content_length = int(request_handler.headers['Content-Length'])
    post_data = request_handler.rfile.read(content_length)

    statement_files = []   # als Kontoauszug erkannt (mit Transaktionen)
    other_files = []       # alles andere (abgelegte Belege, Fehler, ...)

    for part in parse_multipart(content_type, post_data):
        filename = part.filename
        if not filename:
            continue
        content = part.content

        # Schutz gegen Path-Traversal: nur den reinen Dateinamen verwenden,
        # nie vom Client gelieferte Verzeichnisanteile (.., absolute oder
        # Windows-Backslash-Pfade). basename nach Normalisierung der Trenner.
        safe_name = os.path.basename(filename.replace('\\', '/'))
        if not safe_name or safe_name in ('.', '..'):
            continue
        lower_name = safe_name.lower()

        # CAMT.053 (XML / ZIP): nur im Speicher lesen, nie auf Platte ablegen
        if lower_name.endswith(('.xml', '.zip')):
            _collect_camt(safe_name, content, statement_files, other_files)
            continue

        # DKB-Umsatzliste (CSV): ebenfalls nur im Speicher lesen, nie auf Platte ablegen
        if lower_name.endswith('.csv'):
            _collect_csv(safe_name, content, statement_files, other_files)
            continue

        # Datei temporär speichern
        filepath = os.path.join(upload_dir, safe_name)
        with open(filepath, 'wb') as f:
            f.write(content)

        if PARSER_AVAILABLE and filename.lower().endswith('.pdf'):
            try:
                parser = DocumentParser()
                new_path, parsed_data = parser.process_and_organize(filepath)
                transactions = parsed_data.get('transactions') or []

                if transactions:
                    statement_files.append({
                        'filename': filename,
                        'bank_code': parsed_data.get('bank_code'),
                        'iban': parsed_data.get('iban'),
                        'document_date': parsed_data.get('document_date'),
                        'currency': parsed_data.get('currency'),
                        # Befunde zum ganzen Beleg (Saldo, nicht lesbare Zeile …): die Vorschau zeigt sie
                        'warnings': list(parsed_data.get('warnings') or []),
                        'transactions': transactions,
                    })
                elif 'transactions' in parsed_data:
                    # Als Kontoauszug erkannt, aber nichts gelesen: nicht wie ein abgelegter Beleg melden
                    other_files.append({
                        'filename': filename,
                        'status': 'warning',
                        'error': 'Kontoauszug erkannt, aber keine Umsätze gelesen.',
                        'path': os.path.relpath(new_path, upload_dir),
                    })
                else:
                    other_files.append({
                        'filename': filename,
                        'status': 'organized',
                        'path': os.path.relpath(new_path, upload_dir),
                    })
            except DocumentExists as e:
                other_files.append({
                    'filename': filename, 'status': 'warning', 'error': str(e)
                })
            except Exception:
                import traceback
                traceback.print_exc()                   # der volle Fehler nur ins Server-Log, nie in die Antwort
                other_files.append({
                    'filename': filename, 'status': 'error', 'error': UPLOAD_FAILED
                })
        else:
            other_files.append({'filename': filename, 'status': 'uploaded'})

    # Kontoauszüge: pending-import speichern + Vorschau berechnen
    import_id = None
    preview_files = []
    if statement_files:
        parser = DocumentParser()
        combined = {'files': statement_files}
        combined_name = f"combined_{len(statement_files)}_files"
        import_id = parser.save_parsed_data(combined_name, combined)
        preview_files = build_import_preview(db, combined)['files']

    return 200, json.dumps({
        'import_id': import_id,
        'files': preview_files,
        'other_files': other_files,
    }, default=str)


def _store_receipt(db, filename, content):
    """Eine hochgeladene Datei als Beleg ablegen; liefert den Eintrag der Antwort (``status`` ``created`` oder ``error``).
    Jede unerwartete Ausnahme steht nur im Server-Log, die Antwort nennt den festen Satz ``RECEIPT_UPLOAD_FAILED``."""
    shown = receipt_files.display_name(filename)
    if not content:
        return {'filename': shown, 'status': 'error', 'error': RECEIPT_EMPTY_FILE}
    try:
        day, statement = receipt_files.receipt_date(shown, content)
        receipt_id, number, _path = db.insert_receipt_with_file(day, filename, '', content)
    except Exception:
        traceback.print_exc()                       # der volle Fehler nur ins Server-Log, nie in die Antwort
        return {'filename': shown, 'status': 'error', 'error': RECEIPT_UPLOAD_FAILED}
    entry = {'filename': shown, 'status': 'created', 'id': receipt_id, 'number': number, 'date': day}
    hints = [hint for hint, applies in ((RECEIPT_STATEMENT_HINT, statement), (RECEIPT_NO_NUMBER_HINT, not number)) if applies]
    if hints:
        entry['hint'] = ' '.join(hints)
    return entry


def handle_receipt_upload(db, content_type, body):
    """Dateien der Belege-Seite (``POST /receipts/upload``, Multipart) als Belege ablegen: je Datei ein Eintrag mit Nummer aus
    dem Kreis „Belegnummern Firma“, Datum und Dateiname, die Datei unter ``Belege/<Jahr>/`` (``Database.insert_receipt_with_file``).
    Ein Kontoauszug ist dabei ein Beleg wie jede andere Datei (``receipt_files.receipt_date`` liest ihn nur für das Datum und den
    Hinweis): kein Import, nichts in ``pending_imports/``.

    ``body`` ist der schon gelesene Body (der Server prüft Token, Demo-Modus und Größe, bevor er liest). Returns
    ``(Status, Antwort als dict)``: ``{"success": true, "files": [{"filename", "status": "created"|"error", "id", "number", "date",
    "hint", "error"}, …]}`` (je Datei nur die zutreffenden Schlüssel). Kein Multipart und keine Datei: 400; mehr als
    ``MAX_RECEIPT_FILES`` Dateien: 400, nichts angelegt. Eine Datei, die sich nicht ablegen lässt, ist ein Fehler ihrer Zeile –
    die übrigen werden trotzdem angelegt."""
    parts = [part for part in parse_multipart(content_type, body) if part.filename]
    if not parts:
        return 400, {'success': False, 'error': RECEIPT_INVALID_REQUEST}
    if len(parts) > MAX_RECEIPT_FILES:
        return 400, {'success': False, 'error': RECEIPT_TOO_MANY_FILES}
    return 200, {'success': True, 'files': [_store_receipt(db, part.filename, part.content) for part in parts]}
