"""
POST request handlers for form submissions
"""
import os
import json
import datetime
from .pages import Header1, Header2, Header3, Footer
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
        
        linked_count = 0
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
            trans_date = trans['date']
            trans_amount = trans['amount']

            # Check if banking already exists (exact duplicate)
            if db.check_booking_exists(trans_date, trans_amount, account_id_int, foreign_iban, text):
                skipped_count += 1
                skipped_transactions.append(trans)
                continue

            # Versuche, eine passende WISO-Buchung/-Gruppe zu finden und zu verknüpfen
            # Stufe 1: Einzelbuchung (Datum + Betrag exact)
            # Stufe 2: Split-Gruppe (Datum + SUM der Teilbeträge)
            match = db.find_unlinked_booking_by_date_amount(trans_date, trans_amount)
            if match:
                match_type, match_id = match
                conn = db._get_connection()
                cur = conn.cursor()
                update_sql = '''
                    UPDATE Bookings
                    SET Account_ID=?,
                        ForeignBankAccount=?,
                        RecipientClient=CASE WHEN (RecipientClient IS NULL OR RecipientClient='')
                                             THEN ? ELSE RecipientClient END
                    WHERE {where}
                '''
                if match_type == 'single':
                    cur.execute(update_sql.format(where='ID=?'),
                                (account_id_int, foreign_iban, recipient, match_id))
                else:  # 'group' → alle Teilbuchungen der Gruppe verknüpfen
                    cur.execute(update_sql.format(where='BookingGroup_ID=?'),
                                (account_id_int, foreign_iban, recipient, match_id))
                conn.commit()
                conn.close()
                linked_count += 1
                continue

            # Kein Match → neue Buchung anlegen
            db.insert_booking(
                date_booking=trans_date,
                amount=trans_amount,
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
        s += f"<p>{inserted_count} Transaktionen neu angelegt.</p>"
        if linked_count > 0:
            s += f"<p style='color: green;'>{linked_count} WISO-Buchungen mit Bankdaten verknüpft.</p>"
        
        if skipped_count > 0:
            s += f"<p style='color: orange;'>{skipped_count} Duplikate wurden übersprungen:</p>"
            s += "<table>"
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
    skr_raw = post_data.get("skr_account", [""])[0]
    skr_account = int(skr_raw) if skr_raw.strip() else None
    db.insert_account(name, holder, iban, bic, bank_name, skr_account=skr_account)
    return 303, "/masterdata/bankaccounts"

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

def handle_update_booking_group(db: Database, post_data):
    """Beschreibung/Betrag einer bestehenden Gruppe speichern."""
    try:
        group_id    = int(post_data.get("group_id", ["0"])[0])
        description = post_data.get("description", [""])[0]
        total_amount_str = post_data.get("total_amount", [""])[0]
        total_amount = float(total_amount_str) if total_amount_str else None
        db.update_booking_group(group_id, description, total_amount)
        return 303, f"/bookinggroups/view?id={group_id}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f"Fehler beim Aktualisieren der Gruppe: {str(e)}"

def handle_delete_booking_group(db: Database, group_id: int):
    """Gruppe löschen (Buchungen bleiben, werden nur aus Gruppe gelöst)."""
    try:
        db.delete_booking_group(group_id)
        return 303, "/bookinggroups"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f"Fehler beim Löschen der Gruppe: {str(e)}"

def handle_unlink_booking_from_group(db: Database, booking_id: int, group_id: int):
    """Buchung aus Gruppe herauslösen."""
    try:
        db.unlink_booking_from_group(booking_id)
        return 303, f"/bookinggroups/view?id={group_id}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f"Fehler beim Herauslösen der Buchung: {str(e)}"

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
    skr_raw = post_data.get("skr_account", [""])[0]
    skr_account = int(skr_raw) if skr_raw.strip() else None
    db.update_account(account_id, name, holder, iban, bic, bank_name, skr_account=skr_account)
    return 303, "/masterdata/bankaccounts"

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

def handle_db_export(db: Database):
    """Export all DB data as INSERT statements to ./data/db-export.sql"""
    import os
    filepath = os.path.join('data', 'db-export.sql')
    try:
        tables, rows = db.export_to_sql(filepath)
        return 303, f"/miscellaneous?export=ok&tables={tables}&rows={rows}"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 303, f"/miscellaneous?export=error&msg={str(e)}"


def handle_datev_export(db: Database, post_data: dict):
    """DATEV Buchungsstapel-CSV für einen Datumsbereich erzeugen.

    Setzt dabei für alle exportierten Buchungen das Steuerdatum (DateTax)
    auf das heutige Datum.

    Returns:
        (csv_bytes, filename)  – für einen Datei-Download, oder
        (303, location_str)    – bei Fehler (Redirect)
    """
    import datetime
    import sys
    import traceback
    sys.path.insert(0, '.')
    try:
        import datev as datev_module
    except ImportError:
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("datev", os.path.join(os.getcwd(), "datev.py"))
        datev_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(datev_module)

    try:
        date_from = post_data.get('date_from', [''])[0].strip()
        date_to   = post_data.get('date_to',   [''])[0].strip()
        if not date_from or not date_to:
            return 303, '/miscellaneous?datev_export=error&msg=Datumsbereich+fehlt'

        bookings = db.fetch_bookings_range(date_from, date_to)
        if not bookings:
            return 303, f'/miscellaneous?datev_export=error&msg=Keine+Buchungen+im+Zeitraum+{date_from}+bis+{date_to}'

        coa_map = db.get_coa_id_to_number_map()
        csv_bytes, exported_ids = datev_module.export_to_datev(
            bookings, coa_map, date_from, date_to
        )

        # Steuerdatum der exportierten Buchungen auf heute setzen
        today_iso = datetime.date.today().isoformat()
        db.update_bookings_date_tax_batch(exported_ids, today_iso)

        year = date_from[:4]
        filename = f"DATEV_Buchungsstapel_{year}_{date_from.replace('-','')}-{date_to.replace('-','')}.csv"
        return csv_bytes, filename

    except Exception as e:
        traceback.print_exc()
        msg = str(e).replace(' ', '+')
        return 303, f'/miscellaneous?datev_export=error&msg={msg}'


def handle_wiso_import(request_handler, db: Database):
    """WISO Mein Büro CSV-Datei importieren (Multipart-Upload).

    Erwartet eine Datei im Formularfeld „csvfile" (enctype=multipart/form-data).

    Returns:
        (303, location_str) – immer ein Redirect zu /miscellaneous
    """
    from urllib.parse import quote

    content_type = request_handler.headers.get('Content-Type', '')
    if 'multipart/form-data' not in content_type:
        return 303, '/miscellaneous?wiso_import=error&msg=Kein+Multipart+Form+Data'

    try:
        boundary = content_type.split('boundary=')[1].strip().encode()
    except IndexError:
        return 303, '/miscellaneous?wiso_import=error&msg=Boundary+fehlt'

    content_length = int(request_handler.headers['Content-Length'])
    raw = request_handler.rfile.read(content_length)

    # Datei-Inhalt aus Multipart-Body extrahieren
    csv_bytes = None
    for part in raw.split(b'--' + boundary):
        if b'Content-Disposition' in part and b'filename=' in part:
            header_end = part.find(b'\r\n\r\n')
            if header_end == -1:
                continue
            content = part[header_end + 4:]
            if content.endswith(b'\r\n'):
                content = content[:-2]
            if content:
                csv_bytes = content
            break

    if not csv_bytes:
        return 303, '/miscellaneous?wiso_import=error&msg=Keine+Datei+im+Upload'

    try:
        import json, os
        result = db.import_wiso_csv(csv_bytes)
        imported  = result['imported']
        skipped   = result['skipped']
        errs      = result['errors']

        # Detailergebnis für Anzeige auf der Seite persistieren
        result_path = os.path.join('data', 'wiso_import_result.json')
        try:
            with open(result_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # Nicht kritisch

        return 303, (
            f'/miscellaneous?wiso_import=ok'
            f'&imported={imported}&updated={result.get("updated",0)}&skipped={skipped}'
            f'&err_count={len(errs)}'
            f'&not_found={len(result.get("not_found", []))}'
            f'&missing_coa={len(result.get("missing_coa", []))}'
            f'&missing_skr={len(result.get("missing_skr", []))}'
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 303, f'/miscellaneous?wiso_import=error&msg={quote(str(e))}'


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
        message = f"<p><strong>{success_count} von {len(commands)} Befehlen erfolgreich ausgeführt</strong></p>"
        
        if results:
            message += "<h2 class='successColor'>Erfolgreiche Befehle:</h2>"
            message += "<pre class='sql-success-box'>"
            message += "\n".join(results)
            message += "</pre>"
        
        if errors:
            message += "<h2 class='errorColor'>Fehler:</h2>"
            message += "<pre class='sql-error-box'>"
            message += "\n".join(errors)
            message += "</pre>"
        
        message += "<p><a href='/miscellaneous'>Zurück zu Sonstiges</a></p>"
        
        return generate_sql_result_page(message, len(errors) == 0)
        
    except Exception as e:
        conn.rollback()
        return generate_sql_result_page(f"<h2 class='errorColor'>Fehler</h2><p class='errorColor'>{str(e)}</p><p><a href='/miscellaneous'>Zurück zu Sonstiges</a></p>", False)
    finally:
        conn.close()

def generate_sql_result_page(message, success):
    """Generate result page for SQL execution"""
    s = Header1('miscellaneous')
    submenu = '<span id="ActivePage">SQL</span>'
    s += Header2(submenu)
    s += Header3()
    s += "<h1>Ergebnis SQL-Ausführung</h1>"
    s += message
    s += Footer()
    return s

def handle_add_contact(db: Database, post_data):
    """Handle adding a new contact (Option C schema)"""
    def _get(key, default=''):
        return post_data.get(key, [default])[0]

    try:
        db.insert_contact(
            contact_type      = _get('contact_type', 'customer'),
            entity_type       = _get('entity_type',  'company'),
            display_name      = _get('display_name') or None,
            customer_number   = _get('customer_number') or None,
            email             = _get('email'),
            phone             = _get('phone'),
            notes             = _get('notes'),
            logo              = _get('logo'),
            # address
            address_line1     = _get('address_line1'),
            street            = _get('street'),
            postal_code       = _get('postal_code'),
            city              = _get('city'),
            country           = _get('country', 'DE'),
            # company
            company_name      = _get('company_name'),
            legal_form        = _get('legal_form'),
            tax_id            = _get('tax_id'),
            buyer_route_id    = _get('buyer_route_id'),
            # person
            salutation        = _get('salutation'),
            title             = _get('title'),
            first_name        = _get('first_name'),
            last_name         = _get('last_name'),
            date_of_birth     = _get('date_of_birth'),
            company_contact_id= _get('company_contact_id') or None,
            company_name_free = _get('company_name_free'),
        )
        return 303, '/masterdata/contacts'
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f'Fehler beim Hinzufügen des Kontakts: {str(e)}'


def handle_update_contact(db: Database, post_data):
    """Handle updating an existing contact (Option C schema)"""
    def _get(key, default=''):
        return post_data.get(key, [default])[0]

    contact_id = int(_get('contact_id', 0))
    try:
        db.update_contact(
            contact_id        = contact_id,
            contact_type      = _get('contact_type', 'customer'),
            entity_type       = _get('entity_type',  'company'),
            display_name      = _get('display_name') or None,
            customer_number   = _get('customer_number') or None,
            email             = _get('email'),
            phone             = _get('phone'),
            notes             = _get('notes'),
            logo              = _get('logo'),
            address_line1     = _get('address_line1'),
            street            = _get('street'),
            postal_code       = _get('postal_code'),
            city              = _get('city'),
            country           = _get('country', 'DE'),
            company_name      = _get('company_name'),
            legal_form        = _get('legal_form'),
            tax_id            = _get('tax_id'),
            buyer_route_id    = _get('buyer_route_id'),
            salutation        = _get('salutation'),
            title             = _get('title'),
            first_name        = _get('first_name'),
            last_name         = _get('last_name'),
            date_of_birth     = _get('date_of_birth'),
            company_contact_id= _get('company_contact_id') or None,
            company_name_free = _get('company_name_free'),
        )
        return 303, '/masterdata/contacts'
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f'Fehler beim Aktualisieren des Kontakts: {str(e)}'

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
    
    return 303, "/masterdata/numberranges"


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
    
    return 303, "/masterdata/numberranges"


def handle_save_invoice(data: dict, pdf_path: str = None) -> int:
    """Save invoice to database with all metadata
    
    Args:
        data: Invoice data dictionary from frontend
        pdf_path: Path to generated PDF file
        
    Returns:
        invoice_id: ID of inserted invoice
    """
    import datetime
    db = Database()
    
    # Get own company data (seller)
    own_company_id = data.get('ownCompanyId')
    if own_company_id:
        own_contact = db.get_contact_by_id(int(own_company_id))
    else:
        own_contacts = db.fetch_contacts(contact_type='own')
        own_contact = own_contacts[0] if own_contacts else None
    
    if not own_contact:
        raise ValueError("No seller contact found")
    
    # Extract seller data (snapshot)
    seller_name = own_contact[4] or own_contact[3] or ''
    seller_company = own_contact[4] or ''  # Company name
    seller_street = own_contact[5] or ''
    seller_postal = own_contact[6] or ''
    seller_city = own_contact[7] or ''
    seller_country = own_contact[8] if len(own_contact) > 8 else 'DE'
    seller_vat_id = own_contact[11] or ''
    
    # Get buyer data - try to find by customer number or parse from address
    buyer_name = ''
    buyer_company = ''
    buyer_street = ''
    buyer_postal = ''
    buyer_city = ''
    buyer_country = 'DE'  # Default
    buyer_vat_id = ''
    buyer_route_id = data.get('buyerRouteId') or ''
    
    customer_number = data.get('customerNumber', '')
    if customer_number:
        # Try to find customer by number
        contacts = db.fetch_contacts(contact_type='customer')
        for contact in contacts:
            if contact[2] == customer_number:  # CustomerNumber
                buyer_name = contact[3] or ''
                buyer_company = contact[4] or ''  # Company name
                buyer_street = contact[5] or ''
                buyer_postal = contact[6] or ''
                buyer_city = contact[7] or ''
                buyer_country = contact[8] if len(contact) > 8 else 'DE'
                buyer_vat_id = contact[11] or ''
                if not buyer_route_id and len(contact) > 14:
                    buyer_route_id = contact[14] or ''
                break
    
    # If no customer found, parse from address text
    if not buyer_name:
        customer_address = data.get('customerAddress', '')
        customer_name = data.get('customerName', '')
        address_lines = customer_address.split('\n')
        if len(address_lines) >= 1:
            buyer_name = address_lines[0].strip()
        if len(address_lines) >= 2:
            buyer_street = address_lines[1].strip()
        if len(address_lines) >= 3:
            # Parse postal code and city
            last_line = address_lines[2].strip()
            parts = last_line.split(' ', 1)
            if len(parts) >= 2:
                buyer_postal = parts[0]
                buyer_city = parts[1]
            else:
                buyer_city = last_line
    
    # Get bank account data (snapshot) - parse from HTML
    import re
    bank_html = data.get('bankDetails', '')
    bank_text = re.sub(r'<[^>]+>', ' ', bank_html)
    bank_parts = bank_text.replace('\n', ' ').split()
    
    bank_account_holder = seller_name  # Default to seller name
    bank_iban = ''
    bank_bic = ''
    bank_name = ''
    
    i = 0
    while i < len(bank_parts):
        part = bank_parts[i]
        if part.upper() == 'BANK':
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
            i += 1
    
    # Extract numeric values from formatted strings
    def parse_amount(amount_str):
        """Parse amount like '1.234,56 €' to float 1234.56"""
        if not amount_str:
            return 0.0
        # Remove currency symbol and whitespace
        amount_str = str(amount_str).replace('€', '').replace(' ', '').strip()
        # Replace comma with dot
        amount_str = amount_str.replace(',', '.')
        try:
            return float(amount_str)
        except ValueError:
            return 0.0
    
    sum_net = parse_amount(data.get('sumNet', '0'))
    sum_gross = parse_amount(data.get('sumGross', '0'))
    tax_amount = parse_amount(data.get('taxAmount', '0'))
    tax_rate = float(data.get('taxRate', 19))
    
    # Determine tax category based on rate
    if tax_rate == 0:
        tax_category = 'Z'  # Zero rate
    elif tax_rate == 19 or tax_rate == 7:
        tax_category = 'S'  # Standard rate
    else:
        tax_category = 'S'  # Default to standard
    
    # Payment terms
    payment_term_days = data.get('paymentTermDays') or 14
    payment_due_date = data.get('paymentDueDate') or None
    discount_percentage = data.get('discountPercentage') or None
    discount_days = data.get('discountDays') or None
    
    # Build invoice data dictionary
    invoice_data = {
        'InvoiceNumber': data.get('number', ''),
        'InvoiceDate': data.get('date', ''),
        'Currency': 'EUR',
        'OrderNumber': data.get('orderNumber') or None,
        'DeliveryDate': data.get('deliveryDate') or None,
        'SellerName': seller_name,
        'SellerCompany': seller_company,
        'SellerStreet': seller_street,
        'SellerPostalCode': seller_postal,
        'SellerCity': seller_city,
        'SellerCountry': seller_country,
        'SellerVATID': seller_vat_id,
        'BuyerName': buyer_name,
        'BuyerCompany': buyer_company,
        'BuyerStreet': buyer_street,
        'BuyerPostalCode': buyer_postal,
        'BuyerCity': buyer_city,
        'BuyerCountry': buyer_country,
        'BuyerVATID': buyer_vat_id,
        'BuyerRouteID': buyer_route_id,
        'PaymentTermDays': payment_term_days,
        'PaymentDueDate': payment_due_date,
        'DiscountPercentage': discount_percentage,
        'DiscountDays': discount_days,
        'BankAccountHolder': bank_account_holder,
        'BankIBAN': bank_iban,
        'BankBIC': bank_bic,
        'BankName': bank_name,
        'PaymentTerms': data.get('paymentTerms', ''),
        'Notes': None,
        'TaxCategory': tax_category,
        'TaxRate': tax_rate,
        'SumNet': sum_net,
        'TaxAmount': tax_amount,
        'SumGross': sum_gross,
        'AmountDue': sum_gross,  # Initially same as gross
        'Status': 'draft',  # Always start as draft
        'PDFPath': pdf_path,
        'XMLPath': None
    }
    
    # Insert invoice
    invoice_id = db.insert_invoice(invoice_data)
    
    # Insert invoice items
    for item in data.get('items', []):
        # Parse quantity and price
        quantity = float(item.get('quantity', 1))
        price_str = item.get('price', '0')
        try:
            price = float(price_str)
        except ValueError:
            price = 0.0
        
        total_net = quantity * price
        
        item_data = {
            'InvoiceID': invoice_id,
            'Position': int(item.get('pos', 1)),
            'ArticleID': None,  # We don't track article IDs in current implementation
            'Description': item.get('description', ''),
            'Quantity': quantity,
            'Unit': item.get('unit', 'Stk'),
            'PricePerUnit': price,
            'TotalNet': total_net,
            'TaxCategory': tax_category,
            'TaxRate': tax_rate
        }
        
        db.insert_invoice_item(item_data)
    
    return invoice_id


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
        # Format price: convert to float, format with 2 decimals, replace . with ,
        price_value = item.get('price', '0')
        try:
            price_formatted = f"{float(price_value):.2f}".replace('.', ',') + ' €'
        except (ValueError, TypeError):
            price_formatted = str(price_value) + ' €'
        
        content_lines.append(add_text(col_pos, table_y, item.get('pos', ''), "F1", 9))
        content_lines.append(add_text(col_qty, table_y, item.get('quantity', ''), "F1", 9))
        content_lines.append(add_text(col_unit, table_y, item.get('unit', ''), "F1", 9))
        content_lines.append(add_text(col_desc, table_y, item.get('description', '')[:45], "F1", 9))
        content_lines.append(add_text(col_price, table_y, price_formatted, "F1", 9))
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
    
    # Save invoice to database
    import os
    try:
        # Create PDF storage directory
        current_year = datetime.datetime.now().year
        pdf_dir = f"data/invoices/{current_year}"
        os.makedirs(pdf_dir, exist_ok=True)
        
        # Generate PDF filename
        invoice_number = data.get('number', 'Entwurf')
        # Sanitize filename (remove special characters)
        safe_number = "".join(c for c in invoice_number if c.isalnum() or c in ['-', '_'])
        pdf_filename = f"Rechnung_{safe_number}.pdf"
        pdf_path = os.path.join(pdf_dir, pdf_filename)
        
        # Save PDF file
        with open(pdf_path, 'wb') as f:
            f.write(full_pdf)
        
        print(f"PDF saved to: {pdf_path}")
        
        # Save invoice to database
        invoice_id = handle_save_invoice(data, pdf_path)
        print(f"Invoice saved to database with ID: {invoice_id}")
        
    except Exception as e:
        import traceback
        print(f"Error saving invoice: {e}")
        traceback.print_exc()
        # Continue and return PDF even if saving fails
    
    return full_pdf


def handle_update_invoice_status(post_body: bytes):
    """Update invoice status and increment number range if finalizing
    
    Returns tuple: (status_code, redirect_path)
    """
    data = json.loads(post_body.decode('utf-8'))
    invoice_id = int(data.get('invoice_id'))
    new_status = data.get('status')
    
    if not invoice_id or not new_status:
        return 400, "/invoice"
    
    db = Database()
    
    # Validate status
    valid_statuses = ['draft', 'finalized', 'sent', 'paid', 'cancelled']
    if new_status not in valid_statuses:
        return 400, "/invoice"
    
    # Get current invoice
    invoice = db.get_invoice_by_id(invoice_id)
    if not invoice:
        return 404, "/invoice"
    
    current_status = invoice[37] or 'draft'
    
    # If transitioning from draft to finalized, increment number range
    if current_status == 'draft' and new_status == 'finalized':
        invoice_number = invoice[1]  # InvoiceNumber
        
        # Parse invoice number to find matching range
        # Format: YYLPnnn (e.g., 26R001)
        if invoice_number and len(invoice_number) >= 3:
            year_short = invoice_number[:2]
            letter = invoice_number[2] if len(invoice_number) > 2 else ''
            
            # Find corresponding number range
            try:
                current_year = datetime.datetime.now().year
                year_full = 2000 + int(year_short) if int(year_short) >= 0 else current_year
                
                ranges = db.fetch_number_ranges('invoice')
                for r in ranges:
                    if r[2] == year_full and r[3] == letter:
                        # Increment this range
                        new_number = (r[5] or 0) + 1
                        db.update_number_range(
                            range_id=r[0],
                            range_type='invoice',
                            year=r[2],
                            letter=r[3],
                            prefix=r[4] or '',
                            current_number=new_number,
                            description=r[6] or ''
                        )
                        print(f"Incremented number range {r[0]} to {new_number}")
                        break
            except Exception as e:
                print(f"Warning: Could not increment number range: {e}")
                # Continue with status update even if increment fails
    
    # Update status
    db.update_invoice_status(invoice_id, new_status)
    print(f"Invoice {invoice_id} status updated to: {new_status}")
    
    return 303, f"/invoice/view?id={invoice_id}"


def handle_link_invoice_payment(post_body: bytes):
    """Link invoice to a payment transaction
    
    Returns tuple: (status_code, redirect_path)
    """
    data = json.loads(post_body.decode('utf-8'))
    invoice_id = int(data.get('invoice_id'))
    transaction_id = int(data.get('transaction_id'))
    amount_paid = float(data.get('amount_paid'))
    
    if not invoice_id or not transaction_id or not amount_paid:
        return 400, "/invoice"
    
    db = Database()
    
    try:
        db.link_invoice_to_transaction(invoice_id, transaction_id, amount_paid)
        return 200, f"/invoice/view?id={invoice_id}"
    except Exception as e:
        print(f"Error linking payment: {e}")
        return 500, "/invoice"


def handle_invoice_save(post_body: bytes):
    """Save or update invoice to database
    
    Returns JSON response with invoice_id
    """
    try:
        data = json.loads(post_body.decode('utf-8'))
        
        # Check if this is an update (invoiceId present) or new invoice
        invoice_id = data.get('invoiceId')
        is_update = invoice_id is not None
        
        # Extract invoice data
        invoice_number = data.get('invoiceNumber')
        invoice_date = data.get('invoiceDate')
        customer_id = data.get('customerId')
        customer_number = data.get('customerNumber')
        own_company_id = data.get('ownCompanyId')
        buyer_reference = data.get('buyerReference')
        payment_terms = data.get('paymentTerms')
        payment_terms_days = data.get('paymentTermsDays', 14)
        due_date = data.get('dueDate')
        bank_account_id = data.get('bankAccountId')
        net_amount = data.get('netAmount')
        tax_rate = data.get('taxRate')
        tax_amount = data.get('taxAmount')
        gross_amount = data.get('grossAmount')
        currency = data.get('currency', 'EUR')
        status = data.get('status', 'draft')
        payment_means_code = data.get('paymentMeansCode', '58')
        payment_means_text = data.get('paymentMeansText', 'SEPA Überweisung')
        items = data.get('items', [])
        
        # XRechnung optional fields
        order_number = data.get('orderNumber')
        delivery_date = data.get('deliveryDate')
        discount_percentage = data.get('discountPercentage')
        discount_days = data.get('discountDays')
        
        # Validate required fields
        if not invoice_number or not invoice_date or not customer_id or not own_company_id:
            return json.dumps({'success': False, 'error': 'Pflichtfelder fehlen'}).encode()
        
        if not items or len(items) == 0:
            return json.dumps({'success': False, 'error': 'Mindestens eine Position erforderlich'}).encode()
        
        db = Database()
        
        # Get customer data for invoice
        customer = db.get_contact_by_id(customer_id)
        if not customer:
            return json.dumps({'success': False, 'error': 'Kunde nicht gefunden'}).encode()
        
        # Build customer name and address
        customer_name = customer[4] or customer[3] or ''  # Company or Name
        customer_address_parts = []
        if customer[4]:  # Company
            customer_address_parts.append(customer[4])
        if customer[3]:  # Name
            customer_address_parts.append(customer[3])
        if customer[5]:  # Street
            customer_address_parts.append(customer[5])
        if customer[6] or customer[7]:  # PostalCode, City
            city_line = f"{customer[6] or ''} {customer[7] or ''}".strip()
            if city_line:
                customer_address_parts.append(city_line)
        if customer[8]:  # Country
            customer_address_parts.append(customer[8])
        customer_address = '\n'.join(customer_address_parts)
        
        # Get own company for sender line
        own_company = db.get_contact_by_id(own_company_id)
        if not own_company:
            return json.dumps({'success': False, 'error': 'Eigene Firma nicht gefunden'}).encode()
        
        sender_name = own_company[4] or own_company[3] or ''
        sender_street = own_company[5] or ''
        sender_postal = own_company[6] or ''
        sender_city = own_company[7] or ''
        sender_line = f"{sender_name}, {sender_street}, {sender_postal} {sender_city}".strip()
        
        # Get bank account data
        iban = None
        account_name = None
        bic = None
        if bank_account_id:
            account = db.get_account_by_id(bank_account_id)
            if account:
                iban = account[3]  # IBAN
                bic = account[4]  # BIC
                account_name = account[2]  # Inhaber
        
        # Calculate remaining amount (initially = gross amount)
        remaining_amount = gross_amount
        
        # Prepare invoice data dictionary
        invoice_data = {
            'invoice_number': invoice_number,
            'invoice_date': invoice_date,
            'own_company_id': own_company_id,
            'seller_name': own_company[4] or own_company[3] or '',
            'seller_company': own_company[4] or '',
            'seller_street': own_company[5] or '',
            'seller_postal_code': own_company[6] or '',
            'seller_city': own_company[7] or '',
            'seller_country': own_company[8] or 'DE',
            'seller_vat_id': own_company[11] or '',
            'seller_email': own_company[9] or '',
            'seller_phone': own_company[10] or '',
            'customer_id': customer_id,
            'buyer_name': customer[4] or customer[3] or '',
            'buyer_company': customer[4] or '',
            'buyer_street': customer[5] or '',
            'buyer_postal_code': customer[6] or '',
            'buyer_city': customer[7] or '',
            'buyer_country': customer[8] or 'DE',
            'buyer_vat_id': customer[11] or '',
            'buyer_reference': buyer_reference,
            'buyer_route_id': buyer_reference,  # Same as buyer_reference for XRechnung
            'order_number': order_number,
            'currency': currency,
            'delivery_date': delivery_date,
            'payment_terms': payment_terms,
            'payment_due_date': due_date,
            'skonto_days': discount_days,
            'skonto_percent': discount_percentage,
            'bank_account_id': bank_account_id,
            'bank_name': account[5] if bank_account_id and account else '',
            'bank_iban': iban or '',
            'bank_bic': bic or '',
            'tax_category': 'S',  # Standard tax
            'tax_rate': tax_rate,
            'sum_net': net_amount,
            'tax_amount': tax_amount,
            'sum_gross': gross_amount,
            'amount_due': remaining_amount,
            'status': status,
            'pdf_path': None,
            'xml_path': None
        }
        
        # Insert or update invoice
        if is_update:
            # Update existing invoice
            # First, delete old invoice items
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM InvoiceItems WHERE InvoiceId = ?', (invoice_id,))
            conn.commit()
            conn.close()
            
            # Update invoice (we need to add an update_invoice method to db.py)
            # For now, we'll do it manually
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE Invoices SET
                    InvoiceNumber=?, InvoiceDate=?, OwnCompanyId=?,
                    SellerName=?, SellerCompany=?, SellerStreet=?, SellerPostalCode=?, SellerCity=?, SellerCountry=?,
                    SellerVATID=?, SellerEmail=?, SellerPhone=?,
                    CustomerId=?, BuyerName=?, BuyerCompany=?, BuyerStreet=?, BuyerPostalCode=?, BuyerCity=?, BuyerCountry=?,
                    BuyerVATID=?, BuyerReference=?, BuyerRouteID=?,
                    OrderNumber=?, Currency=?, DeliveryDate=?,
                    PaymentTerms=?, PaymentDueDate=?, SkontoDays=?, SkontoPercent=?,
                    BankAccountId=?, BankName=?, BankIBAN=?, BankBIC=?,
                    TaxCategory=?, TaxRate=?, SumNet=?, TaxAmount=?, SumGross=?, AmountDue=?,
                    Status=?
                WHERE ID=?
            ''', (
                invoice_data['invoice_number'], invoice_data['invoice_date'], invoice_data['own_company_id'],
                invoice_data['seller_name'], invoice_data['seller_company'], invoice_data['seller_street'], invoice_data['seller_postal_code'],
                invoice_data['seller_city'], invoice_data['seller_country'],
                invoice_data['seller_vat_id'], invoice_data['seller_email'], invoice_data['seller_phone'],
                invoice_data['customer_id'], invoice_data['buyer_name'], invoice_data['buyer_company'], invoice_data['buyer_street'],
                invoice_data['buyer_postal_code'], invoice_data['buyer_city'], invoice_data['buyer_country'],
                invoice_data['buyer_vat_id'], invoice_data['buyer_reference'], invoice_data['buyer_route_id'],
                invoice_data['order_number'], invoice_data['currency'], invoice_data['delivery_date'],
                invoice_data['payment_terms'], invoice_data['payment_due_date'],
                invoice_data['skonto_days'], invoice_data['skonto_percent'],
                invoice_data['bank_account_id'], invoice_data['bank_name'], invoice_data['bank_iban'], invoice_data['bank_bic'],
                invoice_data['tax_category'], invoice_data['tax_rate'], invoice_data['sum_net'],
                invoice_data['tax_amount'], invoice_data['sum_gross'], invoice_data['amount_due'],
                invoice_data['status'],
                invoice_id
            ))
            conn.commit()
            conn.close()
        else:
            # Insert new invoice
            invoice_id = db.insert_invoice(invoice_data)
        
        # Insert invoice items
        for item in items:
            item_data = {
                'invoice_id': invoice_id,
                'position': item.get('position'),
                'article_id': None,  # No article linkage for now
                'description': item.get('description'),
                'quantity': item.get('quantity'),
                'unit': item.get('unit', 'C62'),  # C62 = piece
                'price_per_unit': item.get('unitPrice'),
                'total_net': item.get('totalPrice'),
                'tax_category': 'S',
                'tax_rate': item.get('taxRate', tax_rate)
            }
            db.insert_invoice_item(item_data)
        
        # If status is 'finalized' and this is a NEW invoice, increment number range
        if status == 'finalized' and not is_update:
            # Extract year and letter from invoice number (e.g., "26R001" -> year=2026, letter="R")
            if len(invoice_number) >= 2:
                year_short = invoice_number[:2]
                current_year = datetime.datetime.now().year
                year = 2000 + int(year_short) if int(year_short) <= 99 else int(year_short)
                
                if len(invoice_number) > 2:
                    letter = invoice_number[2]
                    # Find and increment the number range
                    ranges = db.fetch_number_ranges('invoice')
                    for r in ranges:
                        if r[2] == year and r[3] == letter:
                            # Extract number from invoice_number
                            num_part = invoice_number[3:].lstrip('_ABCDEFGHIJKLMNOPQRSTUVWXYZ')
                            try:
                                current_num = int(num_part)
                                db.update_number_range(r[0], current_num)
                            except:
                                pass
                            break
        
        return json.dumps({'success': True, 'invoice_id': invoice_id}).encode()
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return json.dumps({'success': False, 'error': str(e)}).encode()


def handle_send_invoice_email(post_body: bytes):
    """Send invoice via email
    
    Returns JSON response with success status
    """
    data = json.loads(post_body.decode('utf-8'))
    invoice_id = int(data.get('invoice_id'))
    recipient_email = data.get('recipient_email')
    recipient_name = data.get('recipient_name', '')
    message_text = data.get('message_text')
    
    if not invoice_id or not recipient_email:
        return json.dumps({'success': False, 'error': 'Fehlende Parameter'}).encode()
    
    db = Database()
    invoice = db.get_invoice_by_id(invoice_id)
    
    if not invoice:
        return json.dumps({'success': False, 'error': 'Rechnung nicht gefunden'}).encode()
    
    pdf_path = invoice[38]  # PDFPath
    if not pdf_path:
        return json.dumps({'success': False, 'error': 'Keine PDF-Datei vorhanden'}).encode()
    
    invoice_number = invoice[1]  # InvoiceNumber
    
    # Get seller contact for sender information
    own_contacts = db.fetch_contacts(contact_type='own')
    if not own_contacts:
        return json.dumps({'success': False, 'error': 'Keine Absender-Kontaktdaten gefunden'}).encode()
    
    own_contact = own_contacts[0]
    sender_name = own_contact[4] or own_contact[3] or 'Ihre Firma'
    sender_email = own_contact[9] or ''  # Email
    
    # Send email
    try:
        from email_sender import EmailSender
        email_sender = EmailSender()
        success, error = email_sender.send_invoice_email(
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            invoice_number=invoice_number,
            pdf_path=pdf_path,
            sender_name=sender_name,
            sender_email=sender_email,
            message_text=message_text
        )
        
        if success:
            return json.dumps({'success': True}).encode()
        else:
            return json.dumps({'success': False, 'error': error}).encode()
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return json.dumps({'success': False, 'error': str(e)}).encode()


# ─── Asset-Handler ───────────────────────────────────────────────────────────

def handle_add_asset(db: Database, post_data: dict):
    """Neue Anlage anlegen"""
    name              = post_data.get('name', [''])[0].strip()
    description       = post_data.get('description', [''])[0].strip()
    cat_id            = post_data.get('asset_category_id', [None])[0] or None
    coa_id            = post_data.get('coa_id', [None])[0] or None
    purchase_date     = post_data.get('purchase_date', [''])[0].strip()
    purchase_price    = float(post_data.get('purchase_price', ['0'])[0] or 0)
    useful_life       = int(post_data.get('useful_life_years', ['1'])[0] or 1)
    method            = post_data.get('depreciation_method', ['linear'])[0]
    serial            = post_data.get('serial_number', [''])[0].strip()
    location          = post_data.get('location', [''])[0].strip()
    supplier_id       = post_data.get('supplier_id', [None])[0] or None
    document_id       = post_data.get('document_id', [None])[0] or None
    notes             = post_data.get('notes', [''])[0].strip()
    parent_id         = post_data.get('parent_id', [None])[0] or None

    db.insert_asset(
        name=name,
        description=description,
        asset_category_id=int(cat_id) if cat_id else None,
        coa_id=int(coa_id) if coa_id else None,
        purchase_date=purchase_date,
        purchase_price=purchase_price,
        useful_life_years=useful_life,
        depreciation_method=method,
        serial_number=serial,
        location=location,
        supplier_id=int(supplier_id) if supplier_id else None,
        document_id=int(document_id) if document_id else None,
        notes=notes,
        parent_id=int(parent_id) if parent_id else None,
    )
    return 303, '/assets'


def handle_update_asset(db: Database, post_data: dict):
    """Bestehende Anlage aktualisieren"""
    asset_id          = int(post_data.get('asset_id', ['0'])[0])
    name              = post_data.get('name', [''])[0].strip()
    description       = post_data.get('description', [''])[0].strip()
    cat_id            = post_data.get('asset_category_id', [None])[0] or None
    coa_id            = post_data.get('coa_id', [None])[0] or None
    purchase_date     = post_data.get('purchase_date', [''])[0].strip()
    purchase_price    = float(post_data.get('purchase_price', ['0'])[0] or 0)
    useful_life       = int(post_data.get('useful_life_years', ['1'])[0] or 1)
    method            = post_data.get('depreciation_method', ['linear'])[0]
    serial            = post_data.get('serial_number', [''])[0].strip()
    location          = post_data.get('location', [''])[0].strip()
    supplier_id       = post_data.get('supplier_id', [None])[0] or None
    document_id       = post_data.get('document_id', [None])[0] or None
    notes             = post_data.get('notes', [''])[0].strip()
    status            = post_data.get('status', ['active'])[0]

    db.update_asset(
        asset_id=asset_id,
        name=name,
        description=description,
        asset_category_id=int(cat_id) if cat_id else None,
        coa_id=int(coa_id) if coa_id else None,
        purchase_date=purchase_date,
        purchase_price=purchase_price,
        useful_life_years=useful_life,
        depreciation_method=method,
        serial_number=serial,
        location=location,
        supplier_id=int(supplier_id) if supplier_id else None,
        document_id=int(document_id) if document_id else None,
        notes=notes,
        status=status,
    )
    return 303, f'/assets/edit?id={asset_id}'


def handle_book_depreciation(db: Database, post_data: dict):
    """AfA für ein Jahr buchen"""
    asset_id    = int(post_data.get('asset_id', ['0'])[0])
    year        = int(post_data.get('year', ['0'])[0])
    account_id  = int(post_data.get('account_id', ['0'])[0])
    coa_id      = int(post_data.get('coa_id', ['0'])[0])
    description = post_data.get('description', [''])[0].strip() or None

    db.book_depreciation(
        asset_id=asset_id,
        year=year,
        account_id=account_id,
        coa_id_expense=coa_id,
        coa_id_asset=coa_id,
        description=description,
    )
    return 303, f'/assets/view?id={asset_id}'


def handle_asset_sale(db: Database, post_data: dict):
    """Anlage verkaufen oder abgehen"""
    asset_id   = int(post_data.get('asset_id', ['0'])[0])
    sale_date  = post_data.get('sale_date', [''])[0].strip()
    sale_price = float(post_data.get('sale_price', ['0'])[0] or 0)

    db.sell_asset(asset_id, sale_date, sale_price)
    return 303, f'/assets/view?id={asset_id}'


def handle_add_asset_category(db: Database, post_data: dict):
    """Neue AfA-Kategorie anlegen"""
    name        = post_data.get('name', [''])[0].strip()
    years       = int(post_data.get('useful_life_years', ['1'])[0] or 1)
    method      = post_data.get('depreciation_method', ['linear'])[0]
    coa_id      = post_data.get('coa_id', [None])[0] or None
    notes       = post_data.get('notes', [''])[0].strip()

    db.insert_asset_category(
        name=name,
        useful_life_years=years,
        depreciation_method=method,
        coa_id=int(coa_id) if coa_id else None,
        notes=notes,
    )
    return 303, '/asset_categories'


def handle_update_asset_category(db: Database, post_data: dict):
    """Bestehende AfA-Kategorie aktualisieren"""
    cat_id  = int(post_data.get('category_id', ['0'])[0])
    name    = post_data.get('name', [''])[0].strip()
    years   = int(post_data.get('useful_life_years', ['1'])[0] or 1)
    method  = post_data.get('depreciation_method', ['linear'])[0]
    coa_id  = post_data.get('coa_id', [None])[0] or None
    notes   = post_data.get('notes', [''])[0].strip()

    db.update_asset_category(
        category_id=cat_id,
        name=name,
        useful_life_years=years,
        depreciation_method=method,
        coa_id=int(coa_id) if coa_id else None,
        notes=notes,
    )
    return 303, '/asset_categories'


# ─── (end of Asset-Handler) ───────────────────────────────────────────────────

def handle_invoice_pdf_by_id(invoice_id: int):
    """Generate PDF for an existing invoice by ID
    
    Args:
        invoice_id: ID of the invoice to generate PDF for
        
    Returns:
        tuple: (pdf_bytes, filename) or (None, None) if error
    """
    from pdf_generator import generate_invoice_pdf
    
    db = Database()
    
    try:
        # Generate PDF from database
        pdf_bytes, pdf_path = generate_invoice_pdf(db, invoice_id)
        
        if pdf_bytes is None:
            print(f"Failed to generate PDF for invoice {invoice_id}")
            return None, None
        
        # Extract filename from path
        import os
        filename = os.path.basename(pdf_path) if pdf_path else f"Rechnung_{invoice_id}.pdf"
        
        print(f"Successfully generated PDF for invoice {invoice_id}: {filename}")
        return pdf_bytes, filename
        
    except Exception as e:
        import traceback
        print(f"Error generating PDF for invoice {invoice_id}: {e}")
        traceback.print_exc()
        return None, None
