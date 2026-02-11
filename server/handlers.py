"""
POST request handlers for form submissions
"""
import os
import json
from .pages import Header1, Header2, Footer
from db import Database

try:
    from document_parser import DocumentParser
    PARSER_AVAILABLE = True
except ImportError:
    PARSER_AVAILABLE = False

def handle_add_receipt(db, post_data):
    """Handle adding new receipt"""
    number = post_data["number"][0]
    date = post_data["date"][0]
    filename = post_data["filename"][0]
    path = post_data["path"][0]
    info = post_data["info"][0]
    db.insert_receipt(number, date, filename, path, info)
    return 303, "/receipts"

def handle_update_receipt(db, post_data):
    """Handle updating receipt"""
    receipt_id = int(post_data["id"][0])
    number = post_data["number"][0]
    date = post_data["date"][0]
    filename = post_data["filename"][0]
    path = post_data["path"][0]
    info = post_data["info"][0]
    db.update_receipt(receipt_id, number, date, filename, path, info)
    return 303, "/receipts"

def handle_confirm_import(db: Database, post_data):
    """Handle transaction import confirmation"""
    import_id = post_data.get("import_id", [""])[0]
    action = post_data.get("action", [""])[0]
    
    if not import_id:
        return 400, "Fehlende import_id"
    
    if not PARSER_AVAILABLE:
        return 500, "Parser nicht verfügbar"
    
    parser = DocumentParser()
    
    # Load pending import data
    import_dir = os.path.join('data', 'pending_imports')
    import_files = [f for f in os.listdir(import_dir) if f.startswith(import_id)]
    
    if not import_files:
        return 404, "Import nicht gefunden"
    
    import_file = os.path.join(import_dir, import_files[0])
    
    # Handle cancel action
    if action == "Abbrechen":
        try:
            os.remove(import_file)
            s = Header1()
            s += Header2()
            s += f"<h1>Import abgebrochen</h1>"
            s += f"<p>Die Transaktionen wurden nicht importiert.</p>"
            s += "<p><a href='/receipts'>Zurück zu Belegen</a></p>"
            s += Footer()
            return 200, s
        except Exception as e:
            return 500, f"Fehler beim Löschen: {str(e)}"
    
    # Handle import action
    try:
        with open(import_file, 'r', encoding='utf-8') as f:
            import_data = json.load(f)
        
        # Get account ID from IBAN
        account_iban = import_data.get('iban', '')
        accounts = db.fetch_accounts()
        account_id = None
        for acc in accounts:
            if acc[3] == account_iban:  # IBAN is at index 3
                account_id = acc[0]
                break
        
        if not account_id:
            return 400, f"Kein Konto gefunden für IBAN: {account_iban}"
        
        # Insert transactions
        inserted_count = 0
        skipped_count = 0
        skipped_transactions = []
        
        # Get account info for duplicate check
        account_id_int = None
        for acc in accounts:
            if acc[0] == account_id:
                account_id_int = acc[0]
                break
        
        for trans in import_data.get('transactions', []):
            # Build text from recipient and reference
            recipient = trans['recipient']
            text = trans['reference']
            foreign_iban = trans.get('foreign_iban', '')
            
            # Check if booking already exists
            if db.check_booking_exists(trans['date'], trans['amount'], account_id_int, foreign_iban, text):
                skipped_count += 1
                skipped_transactions.append(trans)
                continue
            
            # Execute insert with automatic SQL logging
            db.insert_booking(
                date_booking=trans['date'],
                amount=trans['amount'],
                account_id=account_id_int,
                foreign_bank_account=foreign_iban,
                recipient_client=recipient,
                text=text,
                document_number=None,
                log_description="VBR bank statement import"
            )
            inserted_count += 1
        
        # Delete pending import file
        os.remove(import_file)
        
        # Redirect with success message
        s = Header1()
        s += Header2()
        s += f"<h1>Import erfolgreich</h1>"
        s += f"<p>{inserted_count} Transaktionen wurden importiert.</p>"
        
        if skipped_count > 0:
            s += f"<p style='color: orange;'>{skipped_count} Duplikate wurden übersprungen:</p>"
            s += "<table border='1'>"
            s += "<tr><th>Datum</th><th>Empfänger</th><th>Verwendungszweck</th><th>Betrag</th><th>Fremd-IBAN</th></tr>"
            for trans in skipped_transactions:
                date_str = trans['date'][:10] if isinstance(trans['date'], str) else trans['date']
                amount_color = "green" if trans['amount'] > 0 else "red"
                s += f"<tr>"
                s += f"<td>{date_str}</td>"
                s += f"<td>{trans['recipient'][:30]}</td>"
                s += f"<td>{trans['reference'][:40]}...</td>"
                s += f"<td style='color:{amount_color}'>{trans['amount']:.2f} €</td>"
                s += f"<td>{trans.get('foreign_iban', '')[:10]}...</td>"
                s += f"</tr>"
            s += "</table>"
        
        s += "<p><a href='/transactions'>Zu Zahlungen</a></p>"
        s += Footer()
        return 200, s
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f"Fehler beim Import: {str(e)}"

def handle_add_transaction(db: Database, post_data):
    """Handle manual booking entry (insert or update)"""
    transaction_id = int(post_data.get("transaction_id", ["0"])[0])
    date = post_data.get("date", [""])[0]
    date_tax = post_data.get("date_tax", [""])[0] or None
    recipient = post_data.get("recipient", [""])[0]
    text = post_data.get("text", [""])[0]
    amount = post_data.get("amount", ["0"])[0]
    currency = post_data.get("currency", ["EUR"])[0]
    account_id = post_data.get("account", [""])[0]
    foreign_account = post_data.get("foreign_account", [""])[0]
    contact_id = post_data.get("contact_id", [""])[0]
    coa_id = post_data.get("coa_id", [""])[0]
    booking_group_id = post_data.get("booking_group_id", [""])[0]
    tax_rate = post_data.get("tax_rate", [""])[0]
    tax_amount = post_data.get("tax_amount", [""])[0]
    document_nr = post_data.get("document_nr", [""])[0]
    
    try:
        # Convert IDs to int or None
        account_id = int(account_id) if account_id else None
        contact_id = int(contact_id) if contact_id else None
        coa_id = int(coa_id) if coa_id else None
        booking_group_id = int(booking_group_id) if booking_group_id else None
        
        # Convert tax_rate from percentage to decimal
        tax_rate = float(tax_rate) / 100 if tax_rate else None
        tax_amount = float(tax_amount) if tax_amount else None
        
        # Update or insert booking
        if transaction_id > 0:
            # Update existing booking
            db.update_booking(
                booking_id=transaction_id,
                date_booking=date,
                date_tax=date_tax,
                booking_group_id=booking_group_id,
                amount=float(amount),
                account_id=account_id,
                foreign_bank_account=foreign_account,
                recipient_client=recipient,
                contact_id=contact_id,
                coa_id=coa_id,
                currency=currency,
                tax_rate=tax_rate,
                tax_amount=tax_amount,
                text=text,
                document_number=document_nr or None,
                log_description="Manual booking update"
            )
        else:
            # Insert new booking
            transaction_id = db.insert_booking(
                date_booking=date,
                date_tax=date_tax,
                booking_group_id=booking_group_id,
                amount=float(amount),
                account_id=account_id,
                foreign_bank_account=foreign_account,
                recipient_client=recipient,
                contact_id=contact_id,
                coa_id=coa_id,
                currency=currency,
                tax_rate=tax_rate,
                tax_amount=tax_amount,
                text=text,
                document_number=document_nr or None,
                log_description="Manual booking entry"
            )
        
        return 303, "/transactions"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f"Fehler beim Speichern: {str(e)}"

def handle_add_bankaccount(db: Database, post_data):
    """Handle adding new bank account"""
    name = post_data["name"][0]
    holder = post_data.get("holder", [""])[0]
    iban = post_data.get("iban", [""])[0]
    bic = post_data.get("bic", [""])[0]
    bank_name = post_data.get("bank_name", [""])[0]
    db.insert_account(name, holder, iban, bic, bank_name)
    return 303, "/settings/bankaccounts"

def handle_create_booking_group(db: Database, post_data):
    """Handle creating a new booking group"""
    description = post_data.get("description", [""])[0]
    total_amount = post_data.get("total_amount", [""])[0]
    
    try:
        total_amount = float(total_amount) if total_amount else None
        group_id = db.create_booking_group(description, total_amount)
        return 303, f"/bookinggroups/view?id={group_id}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f"Fehler beim Erstellen der Gruppe: {str(e)}"

def handle_link_document(db: Database, post_data):
    """Handle linking a document to a booking"""
    booking_id = int(post_data.get("booking_id", ["0"])[0])
    document_id = int(post_data.get("document_id", ["0"])[0])
    relation_type = post_data.get("relation_type", ["receipt"])[0]
    
    try:
        db.link_booking_to_document(booking_id, document_id, relation_type)
        # Redirect back to the booking edit page
        return 303, f"/transactions/edit?id={booking_id}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f"Fehler beim Verknüpfen: {str(e)}"

def handle_update_bankaccount(db: Database, post_data):
    """Handle updating bank account"""
    account_id = int(post_data["id"][0])
    name = post_data["name"][0]
    holder = post_data.get("holder", [""])[0]
    iban = post_data.get("iban", [""])[0]
    bic = post_data.get("bic", [""])[0]
    bank_name = post_data.get("bank_name", [""])[0]
    db.update_account(account_id, name, holder, iban, bic, bank_name)
    return 303, "/settings/bankaccounts"

def handle_add_skr(db: Database, post_data):
    """Handle adding new SKR entry"""
    framework_nr = post_data["framework_nr"][0]
    account = post_data["account"][0]
    name = post_data["name"][0]
    group = post_data["group"][0]
    db.insert_skr(framework_nr, account, name, group)
    return 303, "/skr"

def handle_update_skr(db: Database, post_data):
    """Handle updating SKR entry"""
    id = post_data["id"][0]
    framework_nr = post_data["framework_nr"][0]
    account = post_data["account"][0]
    name = post_data["name"][0]
    group = post_data["group"][0]
    db.update_skr(id, framework_nr, account, name, group)
    return 303, "/skr"

def handle_init_content(db: Database, post_data):
    """Handle database initialization"""
    db.init_content()
    return 303, "/"

def handle_execute_sql(db: Database, post_data):
    """Handle SQL command execution"""
    import sqlite3
    
    sql_commands = post_data.get("sql_commands", [""])[0]
    
    if not sql_commands.strip():
        return generate_sql_result_page("Fehler: Keine SQL-Befehle eingegeben.", False)
    
    # Split commands by semicolon and filter out empty ones
    commands = [cmd.strip() for cmd in sql_commands.split(';') if cmd.strip()]
    
    results = []
    errors = []
    success_count = 0
    
    conn = sqlite3.connect(db.db_name)
    cursor = conn.cursor()
    
    try:
        for i, command in enumerate(commands, 1):
            try:
                cursor.execute(command)
                success_count += 1
                results.append(f"✓ Befehl {i}: Erfolgreich ausgeführt")
                results.append(f"  {command[:100]}{'...' if len(command) > 100 else ''}")
            except sqlite3.Error as e:
                errors.append(f"✗ Befehl {i}: Fehler - {str(e)}")
                errors.append(f"  {command[:100]}{'...' if len(command) > 100 else ''}")
        
        conn.commit()
        
        # Build result message
        message = f"<h2>Ergebnis</h2>"
        message += f"<p><strong>{success_count} von {len(commands)} Befehlen erfolgreich ausgeführt</strong></p>"
        
        if results:
            message += "<h3 class='sql-success-title'>Erfolgreiche Befehle:</h3>"
            message += "<pre class='sql-success-box'>"
            message += "\n".join(results)
            message += "</pre>"
        
        if errors:
            message += "<h3 class='sql-error-title'>Fehler:</h3>"
            message += "<pre class='sql-error-box'>"
            message += "\n".join(errors)
            message += "</pre>"
        
        message += "<p><a href='/'>Zurück zum Dashboard</a></p>"
        
        return generate_sql_result_page(message, len(errors) == 0)
        
    except Exception as e:
        conn.rollback()
        return generate_sql_result_page(f"<h2>Fehler</h2><p class='sql-error-title'>{str(e)}</p><p><a href='/'>Zurück zum Dashboard</a></p>", False)
    finally:
        conn.close()

def generate_sql_result_page(message, success):
    """Generate result page for SQL execution"""
    s = Header1('dashboard')
    s += Header2()
    s += "<h1>SQL-Ausführung</h1>"
    s += message
    s += Footer()
    return s

def handle_add_contact(db: Database, post_data):
    """Handle adding a new contact"""
    contact_type = post_data.get('contact_type', ['customer'])[0]
    customer_number = post_data.get('customer_number', [''])[0]
    name = post_data.get('name', [''])[0]
    company = post_data.get('company', [''])[0]
    street = post_data.get('street', [''])[0]
    postal_code = post_data.get('postal_code', [''])[0]
    city = post_data.get('city', [''])[0]
    country = post_data.get('country', [''])[0]
    email = post_data.get('email', [''])[0]
    phone = post_data.get('phone', [''])[0]
    tax_id = post_data.get('tax_id', [''])[0]
    notes = post_data.get('notes', [''])[0]
    logo = post_data.get('logo', [''])[0]
    
    db.insert_contact(
        name=name,
        contact_type=contact_type,
        customer_number=customer_number,
        company=company,
        street=street,
        postal_code=postal_code,
        city=city,
        country=country,
        email=email,
        phone=phone,
        tax_id=tax_id,
        notes=notes,
        logo=logo
    )
    
    return 303, "/contacts"

def handle_update_contact(db: Database, post_data):
    """Handle updating an existing contact"""
    contact_id = int(post_data.get('contact_id', [0])[0])
    contact_type = post_data.get('contact_type', ['customer'])[0]
    customer_number = post_data.get('customer_number', [''])[0]
    name = post_data.get('name', [''])[0]
    company = post_data.get('company', [''])[0]
    street = post_data.get('street', [''])[0]
    postal_code = post_data.get('postal_code', [''])[0]
    city = post_data.get('city', [''])[0]
    country = post_data.get('country', [''])[0]
    email = post_data.get('email', [''])[0]
    phone = post_data.get('phone', [''])[0]
    tax_id = post_data.get('tax_id', [''])[0]
    notes = post_data.get('notes', [''])[0]
    logo = post_data.get('logo', [''])[0]
    
    db.update_contact(
        contact_id=contact_id,
        name=name,
        contact_type=contact_type,
        customer_number=customer_number,
        company=company,
        street=street,
        postal_code=postal_code,
        city=city,
        country=country,
        email=email,
        phone=phone,
        tax_id=tax_id,
        notes=notes,
        logo=logo
    )
    
    return 303, "/contacts"

def handle_add_article(db: Database, post_data):
    """Handle adding a new article"""
    name = post_data.get('name', [''])[0]
    unit = post_data.get('unit', ['Stk.'])[0]
    unit_price = float(post_data.get('unit_price', ['0'])[0] or 0)
    tax_rate = float(post_data.get('tax_rate', ['19'])[0] or 19)
    description = post_data.get('description', [''])[0]
    active = 1 if 'active' in post_data else 0
    
    db.insert_article(
        name=name,
        unit=unit,
        unit_price=unit_price,
        tax_rate=tax_rate,
        description=description,
        active=active
    )
    
    return 303, "/articles"

def handle_update_article(db: Database, post_data):
    """Handle updating an existing article"""
    article_id = int(post_data.get('id', [0])[0])
    name = post_data.get('name', [''])[0]
    unit = post_data.get('unit', ['Stk.'])[0]
    unit_price = float(post_data.get('unit_price', ['0'])[0] or 0)
    tax_rate = float(post_data.get('tax_rate', ['19'])[0] or 19)
    description = post_data.get('description', [''])[0]
    active = 1 if 'active' in post_data else 0
    
    db.update_article(
        article_id=article_id,
        name=name,
        unit=unit,
        unit_price=unit_price,
        tax_rate=tax_rate,
        description=description,
        active=active
    )
    
    return 303, "/articles"


def handle_add_number_range(db: Database, post_data):
    """Handle adding a new number range"""
    range_type = post_data.get('type', ['invoice'])[0]
    year = int(post_data.get('year', ['2026'])[0])
    letter = post_data.get('letter', ['R'])[0].upper()
    prefix = post_data.get('prefix', [''])[0].upper()
    current_number = int(post_data.get('current_number', ['0'])[0] or 0)
    description = post_data.get('description', [''])[0]
    
    db.insert_number_range(
        range_type=range_type,
        year=year,
        letter=letter,
        prefix=prefix,
        current_number=current_number,
        description=description
    )
    
    return 303, "/settings/numberranges"


def handle_update_number_range(db: Database, post_data):
    """Handle updating an existing number range"""
    range_id = int(post_data.get('id', [0])[0])
    year = int(post_data.get('year', ['2026'])[0])
    letter = post_data.get('letter', ['R'])[0].upper()
    prefix = post_data.get('prefix', [''])[0].upper()
    current_number = int(post_data.get('current_number', ['0'])[0] or 0)
    description = post_data.get('description', [''])[0]
    
    db.update_number_range(
        range_id=range_id,
        year=year,
        letter=letter,
        prefix=prefix,
        current_number=current_number,
        description=description
    )
    
    return 303, "/settings/numberranges"


def handle_generate_invoice_pdf(post_body: bytes) -> bytes:
    """Generate a simple PDF invoice from JSON data"""
    import base64
    data = json.loads(post_body.decode('utf-8'))
    
    # Get own contact data - use selected company if provided
    db = Database()
    own_company_id = data.get('ownCompanyId')
    if own_company_id:
        own_contact = db.get_contact_by_id(int(own_company_id))
    else:
        own_contacts = db.fetch_contacts(contact_type='own')
        own_contact = own_contacts[0] if own_contacts else None
    
    # Try to load logo using PIL - convert to JPEG for PDF compatibility
    logo_data = None
    logo_width = 0
    logo_height = 0
    logo_filter = "/DCTDecode"  # JPEG filter
    try:
        from PIL import Image
        import io
        import os
        
        # Use logo from selected company if available
        logo_path = 'static/logo.png'
        if own_contact and len(own_contact) > 13 and own_contact[13]:
            logo_path = own_contact[13]
            # Ensure the path is relative to the project directory
            if not os.path.isabs(logo_path) and not os.path.exists(logo_path):
                # Try prepending common paths
                for prefix in ['', './', '../']:
                    test_path = prefix + logo_path
                    if os.path.exists(test_path):
                        logo_path = test_path
                        break
        
        print(f"Trying to load logo from: {os.path.abspath(logo_path)}")
        
        if os.path.exists(logo_path):
            with Image.open(logo_path) as img:
                # Convert to RGB if necessary (remove alpha channel)
                if img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    if img.mode in ('RGBA', 'LA'):
                        background.paste(img, mask=img.split()[-1])  # Use alpha channel as mask
                        img = background
                    else:
                        img = img.convert('RGB')
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                logo_width, logo_height = img.size
                # Convert to JPEG in memory
                jpeg_buffer = io.BytesIO()
                img.save(jpeg_buffer, format='JPEG', quality=90)
                logo_data = jpeg_buffer.getvalue()
                print(f"Logo loaded: {logo_width}x{logo_height}, JPEG size: {len(logo_data)}")
        else:
            print(f"Logo file not found: {os.path.abspath(logo_path)}")
    except Exception as e:
        import traceback
        print(f"Logo load error: {e}")
        traceback.print_exc()
    
    # PDF helper functions
    def pdf_obj(num: int, content: str) -> str:
        return f"{num} 0 obj\n{content}\nendobj\n"
    
    def pdf_obj_binary(num: int, header: str, binary_data: bytes) -> bytes:
        header_bytes = f"{num} 0 obj\n{header}\nstream\n".encode('latin-1')
        footer_bytes = b"\nendstream\nendobj\n"
        return header_bytes + binary_data + footer_bytes
    
    def pdf_stream(content: str) -> bytes:
        # Encode first, then calculate length from encoded bytes
        encoded = content.encode('latin-1', errors='replace')
        header = f"<< /Length {len(encoded)} >>\nstream\n".encode('latin-1')
        footer = b"\nendstream"
        return header + encoded + footer
    
    # Build PDF content
    objects = []
    obj_offsets = []
    binary_objects = {}  # Track which objects are binary
    
    # Catalog (object 1)
    objects.append(pdf_obj(1, "<< /Type /Catalog /Pages 2 0 R >>"))
    
    # Pages (object 2) - will be updated later
    objects.append(pdf_obj(2, "<< /Type /Pages /Kids [8 0 R] /Count 1 >>"))
    
    # Font objects (3-5)
    objects.append(pdf_obj(3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"))
    objects.append(pdf_obj(4, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"))
    objects.append(pdf_obj(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique /Encoding /WinAnsiEncoding >>"))
    
    # Image XObject (object 6) - if logo exists
    xobject_ref = ""
    if logo_data and logo_width > 0 and logo_height > 0:
        img_header = f"<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter {logo_filter} /Length {len(logo_data)} >>"
        objects.append(None)  # Placeholder
        binary_objects[5] = (img_header, logo_data)
        xobject_ref = "/XObject << /Logo 6 0 R >> "
    else:
        objects.append(pdf_obj(6, "<< >>"))  # Empty placeholder
    
    # Build page content stream
    content_lines = []
    
    def encode_text(text):
        """Encode text for PDF with WinAnsiEncoding (supports German umlauts)"""
        text = str(text)
        # Escape special PDF characters
        text = text.replace('\\', '\\\\')
        text = text.replace('(', '\\(')
        text = text.replace(')', '\\)')
        # WinAnsiEncoding octal codes for German characters
        text = text.replace('ä', '\\344')  # octal 344 = 228
        text = text.replace('ö', '\\366')  # octal 366 = 246
        text = text.replace('ü', '\\374')  # octal 374 = 252
        text = text.replace('Ä', '\\304')  # octal 304 = 196
        text = text.replace('Ö', '\\326')  # octal 326 = 214
        text = text.replace('Ü', '\\334')  # octal 334 = 220
        text = text.replace('ß', '\\337')  # octal 337 = 223
        text = text.replace('€', '\\200')  # octal 200 = 128 (Euro in WinAnsi)
        return text
    
    def add_text(x, y, text, font="F1", size=10):
        text = encode_text(text)
        return f"BT /{font} {size} Tf {x} {y} Td ({text}) Tj ET\n"
    
    def add_text_gray(x, y, text, font="F1", size=10, gray=0.4):
        text = encode_text(text)
        return f"{gray} g BT /{font} {size} Tf {x} {y} Td ({text}) Tj ET 0 g\n"
    
    def add_line(x1, y1, x2, y2, width=0.5, gray=0):
        return f"{gray} G {width} w {x1} {y1} m {x2} {y2} l S 0 G\n"
    
    def add_rect_fill(x, y, w, h, r, g, b):
        return f"{r} {g} {b} rg {x} {y} {w} {h} re f 0 g\n"
    
    # --- PAGE MARGINS ---
    margin_left = 64  # 50 + 14pt (5mm)
    margin_right = 549  # 560 - 11pt (4mm)
    
    # --- LOGO (top left) ---
    if logo_data and logo_width > 0:
        # Scale logo to max 100pt width, 50pt height
        scale_w = 100 / logo_width if logo_width > 100 else 1
        scale_h = 50 / logo_height if logo_height > 50 else 1
        scale = min(scale_w, scale_h)
        disp_w = logo_width * scale
        disp_h = logo_height * scale
        content_lines.append(f"q {disp_w} 0 0 {disp_h} {margin_left} {790 - disp_h} cm /Logo Do Q\n")
    
    # --- META INFO (top right) ---
    meta_y = 780
    meta_label_x = 406  # angepasst für rechten Rand
    meta_value_x = 486  # angepasst für rechten Rand
    # Format date as DD.MM.YYYY
    date_raw = data.get('date', '')
    if date_raw and '-' in date_raw:
        parts = date_raw.split('-')
        if len(parts) == 3:
            date_formatted = f"{parts[2]}.{parts[1]}.{parts[0]}"
        else:
            date_formatted = date_raw
    else:
        date_formatted = date_raw
    content_lines.append(add_text(meta_label_x, meta_y, "Datum", "F1", 10))
    content_lines.append(add_text(meta_value_x, meta_y, date_formatted, "F1", 10))
    meta_y -= 14
    content_lines.append(add_text(meta_label_x, meta_y, "Rechnungs-Nr.", "F1", 10))
    content_lines.append(add_text(meta_value_x, meta_y, data.get('number', ''), "F1", 10))
    meta_y -= 14
    content_lines.append(add_text(meta_label_x, meta_y, "Kunden-Nr.", "F1", 10))
    content_lines.append(add_text(meta_value_x, meta_y, data.get('customerNumber', ''), "F1", 10))
    
    # --- SENDER LINE (small, dark gray, position for envelope window) ---
    # Envelope window typically at ~45mm from top, ~20mm from left
    # In PDF points: top = 842 - 127 = 715, left = 57
    sender_y = 700
    if own_contact:
        sender_name = own_contact[4] or own_contact[3] or ''
        sender_street = own_contact[5] or ''
        sender_postal = own_contact[6] or ''
        sender_city = own_contact[7] or ''
        sender_line = f"{sender_name} - {sender_street} - {sender_postal} {sender_city}"
        content_lines.append(add_text_gray(margin_left, sender_y, sender_line, "F1", 7, 0.4))
    
    # --- CUSTOMER ADDRESS (below sender, no separator line) ---
    addr_y = sender_y - 18  # 6 pixels more spacing than original
    # Get customer name from data if available
    customer_name = data.get('customerName', '')
    customer_lines = data.get('customerAddress', '').split('\n')
    first_line = True
    for line in customer_lines:
        if line.strip():
            content_lines.append(add_text(margin_left, addr_y, line.strip(), "F1", 11))
            addr_y -= 14
            # Insert customer name after company name (first line)
            if first_line and customer_name:
                content_lines.append(add_text(margin_left, addr_y, customer_name, "F1", 11))
                addr_y -= 14
                first_line = False
            elif first_line:
                first_line = False
    
    # --- TITLE "Rechnung" (left-aligned, normal case) ---
    title_y = addr_y - 30
    content_lines.append(add_text(margin_left, title_y, "Rechnung", "F2", 18))
    
    # --- ITEMS TABLE ---
    table_y = title_y - 35
    table_left = margin_left
    table_right = margin_right
    
    # Light blue background for header (RGB: 0.9, 0.95, 1.0)
    content_lines.append(add_rect_fill(table_left, table_y - 5, table_right - table_left, 18, 0.9, 0.95, 1.0))
    
    # Table column positions (adjusted for margins)
    col_pos = table_left + 2
    col_qty = table_left + 35
    col_unit = table_left + 80
    col_desc = table_left + 125
    col_price = table_left + 350
    col_total = table_left + 430
    
    # Table header text (no line above)
    content_lines.append(add_text(col_pos, table_y, "Pos.", "F2", 9))
    content_lines.append(add_text(col_qty, table_y, "Menge", "F2", 9))
    content_lines.append(add_text(col_unit, table_y, "Einheit", "F2", 9))
    content_lines.append(add_text(col_desc, table_y, "Bezeichnung", "F2", 9))
    content_lines.append(add_text(col_price, table_y, "Einzelpreis", "F2", 9))
    content_lines.append(add_text(col_total, table_y, "Gesamt", "F2", 9))
    
    # Line below header
    table_y -= 7
    content_lines.append(add_line(table_left, table_y, table_right, table_y, 0.5))
    table_y -= 14
    
    # Table items
    for item in data.get('items', []):
        content_lines.append(add_text(col_pos, table_y, item.get('pos', ''), "F1", 9))
        content_lines.append(add_text(col_qty, table_y, item.get('quantity', ''), "F1", 9))
        content_lines.append(add_text(col_unit, table_y, item.get('unit', ''), "F1", 9))
        content_lines.append(add_text(col_desc, table_y, item.get('description', '')[:45], "F1", 9))
        content_lines.append(add_text(col_price, table_y, item.get('price', '') + ' \u20ac', "F1", 9))
        content_lines.append(add_text(col_total, table_y, item.get('total', ''), "F1", 9))
        table_y -= 14
    
    # --- TOTALS ---
    table_y -= 5
    content_lines.append(add_line(col_pos, table_y, table_right, table_y, 0.5))
    table_y -= 14
    content_lines.append(add_text(col_pos, table_y, "Summe netto", "F1", 10))
    content_lines.append(add_text(col_total, table_y, data.get('sumNet', '0,00 EUR'), "F2", 10))
    table_y -= 14
    tax_rate = data.get('taxRate', 19)
    content_lines.append(add_text(col_pos, table_y, f"MwSt. {tax_rate}%", "F1", 10))
    content_lines.append(add_text(col_total, table_y, data.get('taxAmount', '0,00 EUR'), "F1", 10))
    table_y -= 5
    content_lines.append(add_line(col_pos, table_y, table_right, table_y, 0.3))
    table_y -= 14
    content_lines.append(add_text(col_pos, table_y, "Gesamtbetrag", "F2", 10))
    content_lines.append(add_text(col_total, table_y, data.get('sumGross', '0,00 EUR'), "F2", 10))
    content_lines.append(add_line(col_pos, table_y - 3, table_right, table_y - 3, 1.0))
    
    # --- PAYMENT TERMS (full page width like table: 50 to 560 = 510pt) ---
    table_y -= 35
    payment_terms = data.get('paymentTerms', '')
    if payment_terms:
        words = payment_terms.split()
        line = ""
        max_chars = 110  # Full width (510pt / ~5.4pt per char gives 95 chars, but than the line is not full, so we can allow more chars and it will just wrap to next line)
        for word in words:
            if len(line + " " + word) > max_chars:
                content_lines.append(add_text(table_left, table_y, line, "F1", 9))
                table_y -= 12
                line = word
            else:
                line = (line + " " + word).strip()
        if line:
            content_lines.append(add_text(table_left, table_y, line, "F1", 9))
    
    # --- FOOTER (dark gray, structured columns) ---
    footer_y = 53  # 95 - 42pt (1,5cm tiefer)
    # Gray line above footer
    content_lines.append(add_line(table_left, footer_y + 15, table_right, footer_y + 15, 0.3, 0.4))
    
    if own_contact:
        # Left column - Company info
        y_left = footer_y
        content_lines.append(add_text_gray(margin_left, y_left, own_contact[4] or own_contact[3] or '', "F2", 8, 0.3))
        y_left -= 10
        # Contact name (Ansprechpartner) between company and street
        if own_contact[3]:
            content_lines.append(add_text_gray(margin_left, y_left, own_contact[3], "F1", 8, 0.3))
            y_left -= 10
        content_lines.append(add_text_gray(margin_left, y_left, own_contact[5] or '', "F1", 8, 0.3))
        y_left -= 10
        content_lines.append(add_text_gray(margin_left, y_left, f"{own_contact[6] or ''} {own_contact[7] or ''}", "F1", 8, 0.3))
        
        # Center column - Contact details (label + value columns, no colons)
        center_label_x = 220
        center_value_x = 260
        y_center = footer_y
        content_lines.append(add_text_gray(center_label_x, y_center, "Tel", "F1", 8, 0.3))
        content_lines.append(add_text_gray(center_value_x, y_center, own_contact[10] or '-', "F1", 8, 0.3))
        y_center -= 10
        content_lines.append(add_text_gray(center_label_x, y_center, "E-Mail", "F1", 8, 0.3))
        content_lines.append(add_text_gray(center_value_x, y_center, own_contact[9] or '-', "F1", 8, 0.3))
        y_center -= 10
        content_lines.append(add_text_gray(center_label_x, y_center, "UStIdNr", "F1", 8, 0.3))
        content_lines.append(add_text_gray(center_value_x, y_center, own_contact[11] or '-', "F1", 8, 0.3))
        
        # Right column - Bank details (structured with labels)
        bank_label_x = 400
        bank_value_x = 435
        y_bank = footer_y
        content_lines.append(add_text_gray(bank_label_x, y_bank, "Bankverbindung", "F2", 8, 0.3))
        y_bank -= 10
        
        # Parse bank details from HTML (strip all HTML tags)
        import re
        bank_html = data.get('bankDetails', '')
        # Remove all HTML tags
        bank_text = re.sub(r'<[^>]+>', ' ', bank_html)
        # Split by common separators and clean up
        bank_parts = bank_text.replace('\n', ' ').split()
        bank_name = ''
        bank_iban = ''
        bank_bic = ''
        i = 0
        while i < len(bank_parts):
            part = bank_parts[i]
            if part.upper() == 'BANK':
                # Next parts until IBAN are bank name
                i += 1
                name_parts = []
                while i < len(bank_parts) and bank_parts[i].upper() not in ['IBAN', 'BIC']:
                    name_parts.append(bank_parts[i])
                    i += 1
                bank_name = ' '.join(name_parts)
            elif part.upper() == 'IBAN':
                i += 1
                if i < len(bank_parts):
                    bank_iban = bank_parts[i]
                    i += 1
            elif part.upper() == 'BIC':
                i += 1
                if i < len(bank_parts):
                    bank_bic = bank_parts[i]
                    i += 1
            else:
                # First unknown text is bank name if not set
                if not bank_name and part.upper() not in ['BANK', 'IBAN', 'BIC']:
                    bank_name = part
                i += 1
        
        content_lines.append(add_text_gray(bank_label_x, y_bank, "Bank", "F1", 8, 0.3))
        content_lines.append(add_text_gray(bank_value_x, y_bank, bank_name[:25], "F1", 8, 0.3))
        y_bank -= 10
        content_lines.append(add_text_gray(bank_label_x, y_bank, "IBAN", "F1", 8, 0.3))
        content_lines.append(add_text_gray(bank_value_x, y_bank, bank_iban, "F1", 8, 0.3))
        y_bank -= 10
        content_lines.append(add_text_gray(bank_label_x, y_bank, "BIC", "F1", 8, 0.3))
        content_lines.append(add_text_gray(bank_value_x, y_bank, bank_bic, "F1", 8, 0.3))
    
    # Combine content
    page_content = "".join(content_lines)
    
    # Page content stream (object 7) - needs special handling for binary
    content_stream_data = pdf_stream(page_content)
    objects.append(None)  # Placeholder for binary content stream
    binary_objects[6] = content_stream_data  # Index 6 = Object 7
    
    # Page (object 8)
    resources = f"<< /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> {xobject_ref}>>"
    objects.append(pdf_obj(8, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 7 0 R /Resources {resources} >>"))
    
    # Build PDF
    pdf_header = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    
    # Build body, handling binary objects
    pdf_body_parts = []
    for i, obj in enumerate(objects):
        obj_num = i + 1
        if i in binary_objects:
            bin_entry = binary_objects[i]
            if isinstance(bin_entry, tuple):
                # Image XObject: (header, data)
                header, bin_data = bin_entry
                pdf_body_parts.append(pdf_obj_binary(obj_num, f"<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter {logo_filter} /Length {len(bin_data)} >>", bin_data))
            else:
                # Content stream: already formatted bytes
                obj_header = f"{obj_num} 0 obj\n".encode('latin-1')
                obj_footer = b"\nendobj\n"
                pdf_body_parts.append(obj_header + bin_entry + obj_footer)
        elif obj is not None:
            pdf_body_parts.append(obj.encode('latin-1', errors='replace'))
    
    pdf_body = b"".join(pdf_body_parts)
    
    # Calculate xref offsets
    offset = len(pdf_header.encode('latin-1'))
    for i, part in enumerate(pdf_body_parts):
        obj_offsets.append(offset)
        offset += len(part)
    
    # Build xref table
    xref_offset = len(pdf_header.encode('latin-1')) + len(pdf_body)
    xref = f"xref\n0 {len(objects) + 1}\n"
    xref += "0000000000 65535 f \n"
    for off in obj_offsets:
        xref += f"{off:010d} 00000 n \n"
    
    # Trailer
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF"
    
    # Combine PDF
    full_pdf = pdf_header.encode('latin-1') + pdf_body + xref.encode('latin-1') + trailer.encode('latin-1')
    
    return full_pdf