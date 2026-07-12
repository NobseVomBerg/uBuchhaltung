# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
File upload handler for receipts and bank statements.

Liefert eine JSON-Antwort mit einer Import-Vorschau pro Beleg, damit die
Transaktionsseite den Import inline (ohne Seitenwechsel) anzeigen kann.
"""
import os
import json

from .import_preview import build_import_preview
from .multipart import parse_multipart

try:
    from document_parser import DocumentParser
    PARSER_AVAILABLE = True
except ImportError:
    PARSER_AVAILABLE = False


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

    content_type = request_handler.headers['Content-Type']
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
                        'transactions': transactions,
                    })
                else:
                    other_files.append({
                        'filename': filename,
                        'status': 'organized',
                        'path': os.path.relpath(new_path, upload_dir),
                    })
            except FileExistsError as e:
                other_files.append({
                    'filename': filename, 'status': 'warning', 'error': str(e)
                })
            except Exception as e:
                import traceback
                traceback.print_exc()
                other_files.append({
                    'filename': filename, 'status': 'error', 'error': str(e)
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
