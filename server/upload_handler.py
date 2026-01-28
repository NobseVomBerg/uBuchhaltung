"""
File upload handler for receipts and bank statements
"""
import os
from .pages import Header1, Header2, Footer

try:
    from document_parser import DocumentParser
    PARSER_AVAILABLE = True
except ImportError:
    PARSER_AVAILABLE = False

def handle_file_upload(request_handler):
    """Handle multipart file upload"""
    from email.parser import BytesParser
    
    # Create directory if it doesn't exist
    upload_dir = "./data/Documents"
    os.makedirs(upload_dir, exist_ok=True)
    
    # Parse multipart/form-data
    content_type = request_handler.headers['Content-Type']
    if 'multipart/form-data' not in content_type:
        return 400, "Ungültiger Content-Type"
    
    # Get boundary
    boundary = content_type.split("boundary=")[1].encode()
    content_length = int(request_handler.headers['Content-Length'])
    post_data = request_handler.rfile.read(content_length)
    
    # Parse multipart data
    parts = post_data.split(b'--' + boundary)
    uploaded_files = []
    all_transactions = []  # Collect all transactions from all files
    combined_import_data = {
        'files': [],
        'transactions': []
    }
    
    for part in parts:
        if b'Content-Disposition' in part:
            # Extract filename
            if b'filename=' in part:
                header_end = part.find(b'\r\n\r\n')
                if header_end == -1:
                    continue
                
                header = part[:header_end].decode('utf-8', errors='ignore')
                content = part[header_end + 4:]
                
                # Remove trailing boundary markers
                if content.endswith(b'\r\n'):
                    content = content[:-2]
                
                # Extract filename from header
                filename_start = header.find('filename="') + 10
                filename_end = header.find('"', filename_start)
                filename = header[filename_start:filename_end]
                
                if filename:
                    # Save file temporarily
                    filepath = os.path.join(upload_dir, filename)
                    with open(filepath, 'wb') as f:
                        f.write(content)
                    
                    # Try to parse and organize document
                    if PARSER_AVAILABLE and filename.lower().endswith('.pdf'):
                        try:
                            parser = DocumentParser()
                            new_path, parsed_data = parser.process_and_organize(filepath)
                            
                            # If bank statement with transactions, collect them
                            if parsed_data.get('transactions') and len(parsed_data['transactions']) > 0:
                                combined_import_data['files'].append({
                                    'filename': filename,
                                    'iban': parsed_data.get('iban'),
                                    'document_date': parsed_data.get('document_date'),
                                    'transaction_count': len(parsed_data['transactions'])
                                })
                                # Add all transactions to combined list
                                combined_import_data['transactions'].extend(parsed_data['transactions'])
                                
                                # Keep IBAN from first file (or use last one if they differ)
                                if 'iban' not in combined_import_data or not combined_import_data['iban']:
                                    combined_import_data['iban'] = parsed_data.get('iban')
                                
                                uploaded_files.append({
                                    'filename': filename,
                                    'status': 'parsed',
                                    'transaction_count': len(parsed_data['transactions'])
                                })
                            else:
                                uploaded_files.append({
                                    'filename': filename,
                                    'status': 'organized',
                                    'path': os.path.relpath(new_path, upload_dir)
                                })
                        except FileExistsError as e:
                            # File exists with different content
                            uploaded_files.append({
                                'filename': filename,
                                'status': 'warning',
                                'error': str(e)
                            })
                        except Exception as e:
                            print(f"Error parsing {filename}: {e}")
                            import traceback
                            traceback.print_exc()
                            uploaded_files.append({
                                'filename': filename,
                                'status': 'error',
                                'error': str(e)
                            })
                    else:
                        uploaded_files.append({
                            'filename': filename,
                            'status': 'uploaded'
                        })
    
    if uploaded_files:
        # Build response HTML
        s = Header1()
        s+= Header2()
        s+= "<h1>Upload erfolgreich</h1>"
        
        # Check if we have transactions to import
        has_transactions = len(combined_import_data['transactions']) > 0
        
        if has_transactions:
            # Save combined import data with single import_id
            parser = DocumentParser()
            combined_filename = f"combined_{len(combined_import_data['files'])}_files"
            import_id = parser.save_parsed_data(combined_filename, combined_import_data)
            
            s+= "<h2>Gefundene Transaktionen:</h2>"
            s+= "<ul>"
            for file_info in combined_import_data['files']:
                s+= f"<li><strong>{file_info['filename']}</strong>: {file_info['transaction_count']} Transaktionen</li>"
            s+= "</ul>"
            s+= f"<p><strong>Gesamt: {len(combined_import_data['transactions'])} Transaktionen</strong></p>"
            s+= f"<p><a href='/confirm_transactions?import_id={import_id}' style='background-color: green; color: white; padding: 10px 20px; text-decoration: none; display: inline-block; border-radius: 5px;'>Alle Transaktionen bestätigen</a></p>"
        
        # Show other files (non-parsed or errors)
        other_files = [f for f in uploaded_files if f.get('status') != 'parsed']
        if other_files:
            if has_transactions:
                s+= "<h2>Weitere Dateien:</h2>"
            for file_info in other_files:
                if file_info['status'] == 'organized':
                    s+= f"<p>✓ <strong>{file_info['filename']}</strong> verschoben nach {file_info['path']}</p>"
                elif file_info['status'] == 'warning':
                    s+= f"<p style='color: orange;'>⚠ <strong>{file_info['filename']}</strong>: {file_info['error']}</p>"
                elif file_info['status'] == 'error':
                    s+= f"<p>⚠ <strong>{file_info['filename']}</strong>: Fehler beim Parsen - {file_info['error']}</p>"
                else:
                    s+= f"<p>✓ <strong>{file_info['filename']}</strong> hochgeladen</p>"
        
        s+= "<p><a href='/receipts'>Zurück zu Belegen</a></p>"
        s+= Footer()
        return 200, s
    else:
        return 200, "Keine Dateien hochgeladen."
