"""
POST request handlers for form submissions
"""
import os
import json
from .pages import Header1, Header2, Footer

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
    number = post_data["number"][0]
    date = post_data["date"][0]
    filename = post_data["filename"][0]
    path = post_data["path"][0]
    info = post_data["info"][0]
    db.update_receipt(number, date, filename, path, info)
    return 303, "/receipts"

def handle_confirm_import(db, post_data):
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
        
        # Get own IBAN for duplicate check
        own_iban = ""
        for acc in accounts:
            if acc[0] == account_id:
                own_iban = acc[3]
                break
        
        for trans in import_data.get('transactions', []):
            # Build note
            note = f"{trans['recipient']}\n{trans['reference']}"
            foreign_iban = trans.get('foreign_iban', '')
            
            # Check if transaction already exists (including note for uniqueness)
            if db.check_transaction_exists(trans['date'], trans['amount'], own_iban, foreign_iban, note):
                skipped_count += 1
                skipped_transactions.append(trans)
                continue
            
            # Prepare parameters tuple for logging
            parameters = (account_id, trans['date'], trans['amount'], None, note, foreign_iban)
            
            # Prepare SQL statement for logging
            sql_statement = f"""INSERT INTO Zahlung (KontoId, Datum, Betrag, Beleg, Notiz, FremdIban) 
                              VALUES ({account_id}, '{trans['date']}', {trans['amount']}, 
                              NULL, '{note.replace("'", "''")}', '{foreign_iban}')"""
            
            # Log SQL before execution
            parser.log_sql(sql_statement, parameters, "VBR bank statement import")
            
            # Execute insert
            db.insert_transaction(
                date=trans['date'],
                amount=trans['amount'],
                own_iban=own_iban,
                foreign_iban=foreign_iban,
                note=note,
                receipt_number=None
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

def handle_add_transaction(db, post_data):
    """Handle manual transaction entry (insert or update)"""
    transaction_id = int(post_data.get("transaction_id", ["0"])[0])
    date = post_data.get("date", [""])[0]
    recipient = post_data.get("recipient", [""])[0]
    reference = post_data.get("reference", [""])[0]
    amount = post_data.get("amount", ["0"])[0]
    account_id = post_data.get("account", [""])[0]
    skr_account = post_data.get("skr_account", [""])[0]
    receipt_nr = post_data.get("receipt_nr", [""])[0]
    
    try:
        # Get account IBAN
        accounts = db.fetch_accounts()
        own_iban = None
        for acc in accounts:
            if str(acc[0]) == account_id:
                own_iban = acc[3]  # IBAN is at index 3
                break
        
        # Build note
        note = f"{recipient}\n{reference}" if recipient and reference else (recipient or reference or "")
        
        # Update or insert transaction
        if transaction_id > 0:
            # Update existing transaction
            db.update_transaction(
                transaction_id=transaction_id,
                date=date,
                amount=float(amount),
                own_iban=own_iban or "",
                foreign_iban="",
                note=note,
                receipt_number=receipt_nr or None
            )
            log_desc = "Manual transaction update"
            sql_statement = f'''UPDATE Zahlung SET Datum1='{date}', Datum2=NULL, BankEigen='{own_iban or ""}', BankFremd='', Zweck='{note.replace("'", "''")}', BelegNummer={f"'{receipt_nr}'" if receipt_nr else "NULL"}, Betrag={amount}, SkrBuchJoinId=NULL WHERE ID={transaction_id}'''
        else:
            # Insert new transaction
            transaction_id = db.insert_transaction(
                date=date,
                amount=float(amount),
                own_iban=own_iban or "",
                foreign_iban="",
                note=note,
                receipt_number=receipt_nr or None
            )
            log_desc = "Manual transaction entry"
            sql_statement = f'''INSERT INTO Zahlung (Datum1, Datum2, BankEigen, BankFremd, Zweck, BelegNummer, Betrag, SkrBuchJoinId)
VALUES ('{date}', NULL, '{own_iban or ""}', '', '{note.replace("'", "''")}', {f"'{receipt_nr}'" if receipt_nr else "NULL"}, {amount}, NULL)'''
        
        # Log the operation
        if PARSER_AVAILABLE:
            parser = DocumentParser()
            parser.log_sql(sql_statement, (date, None, own_iban or "", "", note, receipt_nr, float(amount), None), log_desc)
        
        return 303, "/transactions"
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f"Fehler beim Speichern: {str(e)}"

def handle_add_bankaccount(db, post_data):
    """Handle adding new bank account"""
    name = post_data["name"][0]
    holder = post_data.get("holder", [""])[0]
    iban = post_data.get("iban", [""])[0]
    bic = post_data.get("bic", [""])[0]
    bank_name = post_data.get("bank_name", [""])[0]
    db.insert_account(name, holder, iban, bic, bank_name)
    return 303, "/settings/bankaccounts"

def handle_update_bankaccount(db, post_data):
    """Handle updating bank account"""
    account_id = int(post_data["id"][0])
    name = post_data["name"][0]
    holder = post_data.get("holder", [""])[0]
    iban = post_data.get("iban", [""])[0]
    bic = post_data.get("bic", [""])[0]
    bank_name = post_data.get("bank_name", [""])[0]
    db.update_account(account_id, name, holder, iban, bic, bank_name)
    return 303, "/settings/bankaccounts"

def handle_add_skr(db, post_data):
    """Handle adding new SKR entry"""
    framework_nr = post_data["framework_nr"][0]
    account = post_data["account"][0]
    name = post_data["name"][0]
    group = post_data["group"][0]
    db.insert_skr(framework_nr, account, name, group)
    return 303, "/skr"

def handle_update_skr(db, post_data):
    """Handle updating SKR entry"""
    id = post_data["id"][0]
    framework_nr = post_data["framework_nr"][0]
    account = post_data["account"][0]
    name = post_data["name"][0]
    group = post_data["group"][0]
    db.update_skr(id, framework_nr, account, name, group)
    return 303, "/skr"

def handle_init_content(db, post_data):
    """Handle database initialization"""
    db.init_content()
    return 303, "/"
