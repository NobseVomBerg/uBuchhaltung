"""
File upload handler for receipts and bank statements.

Liefert eine JSON-Antwort mit einer Import-Vorschau pro Beleg, damit die
Transaktionsseite den Import inline (ohne Seitenwechsel) anzeigen kann.
"""
import os
import json

from .import_preview import build_import_preview

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
    upload_dir = "./data/Documents"
    os.makedirs(upload_dir, exist_ok=True)

    content_type = request_handler.headers['Content-Type']
    if 'multipart/form-data' not in content_type:
        return 400, json.dumps({'error': 'Ungültiger Content-Type'})

    boundary = content_type.split("boundary=")[1].encode()
    content_length = int(request_handler.headers['Content-Length'])
    post_data = request_handler.rfile.read(content_length)

    parts = post_data.split(b'--' + boundary)
    statement_files = []   # als Kontoauszug erkannt (mit Transaktionen)
    other_files = []       # alles andere (abgelegte Belege, Fehler, ...)

    for part in parts:
        if b'Content-Disposition' not in part or b'filename=' not in part:
            continue

        header_end = part.find(b'\r\n\r\n')
        if header_end == -1:
            continue

        header = part[:header_end].decode('utf-8', errors='ignore')
        content = part[header_end + 4:]
        if content.endswith(b'\r\n'):
            content = content[:-2]

        filename_start = header.find('filename="') + 10
        filename_end = header.find('"', filename_start)
        filename = header[filename_start:filename_end]
        if not filename:
            continue

        # Datei temporär speichern
        filepath = os.path.join(upload_dir, filename)
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
