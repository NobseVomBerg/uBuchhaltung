# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
POST request handlers for form submissions
"""
import os
import json
import datetime
from urllib.parse import quote
import auth
import userctx
from .pages import Header1, Header2, Header3, Footer
from .import_preview import match_account
from db import Database
from money import to_minor, from_minor, multiply, tax_from_net, round_minor
from decimal import Decimal

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
    """Kontoauszug-Import bestätigen – einzelner Beleg oder alle.

    Erwartet ``import_id`` und optional ``file_index`` (genau ein Beleg);
    ohne ``file_index`` werden alle noch nicht importierten Belege übernommen.
    Das Konto wird pro Beleg über dessen IBAN aufgelöst, damit Auszüge
    verschiedener Konten korrekt zugeordnet werden.

    Returns (status_code, json_string).
    """
    import_id = post_data.get("import_id", [""])[0]
    file_index_raw = post_data.get("file_index", [""])[0]

    if not import_id:
        return 400, json.dumps({'ok': False, 'error': 'Fehlende import_id'})

    import_dir = os.path.join(userctx.user_data_dir(), 'pending_imports')
    try:
        import_files = [f for f in os.listdir(import_dir) if f.startswith(import_id)]
    except FileNotFoundError:
        import_files = []
    if not import_files:
        return 404, json.dumps({'ok': False, 'error': 'Import nicht gefunden'})
    import_file = os.path.join(import_dir, import_files[0])

    try:
        with open(import_file, 'r', encoding='utf-8') as f:
            import_data = json.load(f)

        files = import_data.get('files', [])
        if not files:
            os.remove(import_file)
            return 200, json.dumps({'ok': True, 'results': [], 'all_done': True})

        # Zu importierende Belege bestimmen
        if file_index_raw != "":
            try:
                target_indices = [int(file_index_raw)]
            except ValueError:
                return 400, json.dumps({'ok': False, 'error': 'Ungültiger file_index'})
        else:
            target_indices = [i for i, fl in enumerate(files) if not fl.get('imported')]

        accounts = db.fetch_accounts()
        results = []
        any_inserted = False

        # Zählbasierte Duplikat-Erkennung: DB-Bestand pro Schlüssel wird beim
        # ersten Auftreten ermittelt (vor den Inserts dieses Laufs); übersprungen
        # werden nur so viele Transaktionen, wie die DB bereits enthält. So
        # bleiben mehrfach identische Transaktionen (gleicher Tag/Betrag) erhalten.
        dup_db_counts = {}
        dup_seen = {}

        for i in target_indices:
            if i < 0 or i >= len(files):
                continue
            fl = files[i]
            if fl.get('imported'):
                results.append({'file_index': i, 'filename': fl.get('filename'),
                                'inserted': 0, 'skipped': 0,
                                'account_found': True, 'error': 'bereits importiert'})
                continue

            account_id, _name = match_account(accounts, fl.get('iban'))
            if account_id is None:
                results.append({'file_index': i, 'filename': fl.get('filename'),
                                'inserted': 0, 'skipped': 0, 'account_found': False,
                                'error': f"Kein Konto für IBAN {fl.get('iban')}"})
                continue

            bank_code = fl.get('bank_code') or 'unknown'
            inserted = 0
            skipped = 0
            for trans in fl.get('transactions', []):
                recipient = trans.get('recipient', '') or ''
                text = trans.get('reference', '') or ''
                foreign_iban = trans.get('foreign_iban', '') or ''
                trans_date = trans.get('date')
                trans_amount = trans.get('amount')

                if trans_date is None or trans_amount is None:
                    skipped += 1
                    continue
                dup_key = (trans_date, round(float(trans_amount), 2), account_id)
                if dup_key not in dup_db_counts:
                    dup_db_counts[dup_key] = db.check_booking_exists(
                        trans_date, trans_amount, account_id, foreign_iban, text)
                seen = dup_seen.get(dup_key, 0)
                dup_seen[dup_key] = seen + 1
                if seen < dup_db_counts[dup_key]:
                    skipped += 1
                    continue

                db.insert_booking(
                    date_booking=trans_date,
                    amount=trans_amount,
                    account_id=account_id,
                    foreign_bank_account=foreign_iban,
                    recipient_client=recipient,
                    text=text,
                    document_number=None,
                    booking_type='bank',
                    log_description=f"{bank_code} bank statement import"
                )
                inserted += 1
                any_inserted = True

            fl['imported'] = True
            results.append({'file_index': i, 'filename': fl.get('filename'),
                            'inserted': inserted, 'skipped': skipped,
                            'account_found': True, 'error': None})

        # Auto-Linking nur wenn etwas neu angelegt wurde
        linked = repaired = resolved = 0
        if any_inserted:
            link_result = db.link_bank_to_entries()
            linked = link_result.get('linked', 0)
            repaired = link_result.get('repaired', 0)
            resolved = link_result.get('resolved', 0)

        all_done = all(fl.get('imported') for fl in files)
        if all_done:
            os.remove(import_file)
        else:
            with open(import_file, 'w', encoding='utf-8') as f:
                json.dump(import_data, f, indent=2, default=str)

        return 200, json.dumps({
            'ok': True,
            'results': results,
            'linked': linked, 'repaired': repaired, 'resolved': resolved,
            'all_done': all_done,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, json.dumps({'ok': False, 'error': f'Fehler beim Import: {str(e)}'})

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
    invoice_id = post_data.get("invoice_id", [""])[0]

    try:
        # Convert IDs to int or None
        account_id = int(account_id) if account_id else None
        contact_id = int(contact_id) if contact_id else None
        coa_id = int(coa_id) if coa_id else None
        booking_group_id = int(booking_group_id) if booking_group_id else None
        invoice_id = int(invoice_id) if invoice_id else None
        
        # Convert tax_rate from percentage to decimal
        tax_rate = float(tax_rate) / 100 if tax_rate else None
        tax_amount = float(tax_amount) if tax_amount else None
        
        # Update or insert booking
        if transaction_id > 0:
            # Check if this is an unlinked bank booking being completed
            existing = db.get_booking_by_id(transaction_id)
            is_bank = existing and existing[17] == 'bank'
            has_linked_entry = False
            if is_bank:
                has_linked_entry = db.get_linked_entry_for_bank(transaction_id) is not None

            # Update the bank booking itself (COA stays on bank row for
            # display; the real accounting entry is the child)
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

            # Auto-create entry child when completing a bank booking
            if is_bank and coa_id and not has_linked_entry:
                # Look up the account's SKR-COA to set as the liquid counter side
                eff_acct_id = account_id or (existing[4] if existing else None)
                account_coa_id = None
                if eff_acct_id:
                    acct_row = db.get_account_by_id(eff_acct_id)
                    if acct_row and acct_row[7]:  # SKRAccount at index 7
                        account_coa_id = db.get_coa_id_by_account_number(acct_row[7])
                db.insert_booking(
                    date_booking=date,
                    date_tax=date_tax,
                    amount=float(amount),
                    recipient_client=recipient,
                    contact_id=contact_id,
                    coa_id=coa_id,
                    counter_coa_id=account_coa_id,
                    currency=currency,
                    tax_rate=tax_rate,
                    tax_amount=tax_amount,
                    text=text,
                    document_number=document_nr or None,
                    booking_type='entry',
                    parent_booking_id=transaction_id,
                    log_description="Manual bank booking completion (entry child)"
                )
        else:
            # Insert new booking.
            # If a bank account is provided the row is a bank movement;
            # otherwise it is a plain accounting entry.
            new_booking_type = 'bank' if account_id else 'entry'
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
                booking_type=new_booking_type,
                log_description="Manual booking entry"
            )

            # When a bank booking is created with a COA already set,
            # immediately create the linked accounting entry child so the
            # green checkmark appears without requiring a second edit.
            if new_booking_type == 'bank' and coa_id:
                # Look up the account's SKR-COA to set as the liquid counter side
                account_coa_id = None
                if account_id:
                    acct_row = db.get_account_by_id(account_id)
                    if acct_row and acct_row[7]:  # SKRAccount at index 7
                        account_coa_id = db.get_coa_id_by_account_number(acct_row[7])
                db.insert_booking(
                    date_booking=date,
                    date_tax=date_tax,
                    amount=float(amount),
                    recipient_client=recipient,
                    contact_id=contact_id,
                    coa_id=coa_id,
                    counter_coa_id=account_coa_id,
                    currency=currency,
                    tax_rate=tax_rate,
                    tax_amount=tax_amount,
                    text=text,
                    document_number=document_nr or None,
                    booking_type='entry',
                    parent_booking_id=transaction_id,
                    log_description="Manual bank booking – auto entry child"
                )

        # Zahlungs-Zuordnung zur Rechnung (todo #2): idempotent und gekappt;
        # ein Fehler hier darf die bereits gespeicherte Buchung nicht zum 500 machen.
        if invoice_id:
            try:
                link_booking_to_invoice_capped(db, invoice_id, transaction_id)
            except Exception as e:
                print(f"Error linking booking {transaction_id} to invoice {invoice_id}: {e}")
            return 303, f"/invoice/edit?id={invoice_id}"

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

def _skr_redirect(msg, type_='success'):
    return 303, f"/masterdata/skr?msg={quote(msg)}&type={type_}"


def handle_add_skr(db: Database, post_data):
    """Handle adding new SKR entry (ID wird aus Rahmen+Nummer berechnet)."""
    from db.accounts import SKR_FRAMEWORKS
    name = post_data["name"][0]
    group = post_data["group"][0]
    psp = int(post_data.get("private_share_percent", [0])[0] or 0)
    show = 1 if "show_in_menu" in post_data else 0
    is_standard = 1 if "is_standard" in post_data else 0
    try:
        framework_nr = int(post_data["framework_nr"][0])
        account = int(post_data["account"][0])
    except (ValueError, KeyError, TypeError):
        return _skr_redirect("Rahmen-Nr. und Konto müssen Zahlen sein.", "error")
    if framework_nr not in SKR_FRAMEWORKS:
        gueltig = ', '.join(str(n) for n in SKR_FRAMEWORKS)
        return _skr_redirect(f"Ungültiger Kontenrahmen {framework_nr} (gültig: {gueltig}).", "error")
    if account <= 0:
        return _skr_redirect("Kontonummer muss größer 0 sein.", "error")
    ok = db.insert_chart_of_accounts(framework_nr, account, name, group,
                                     is_standard=is_standard, private_share_percent=psp, show_in_menu=show)
    if not ok:
        return _skr_redirect(f"Konto {account} existiert im Rahmen {framework_nr} bereits.", "error")
    return _skr_redirect("Konto angelegt.")

def handle_update_skr(db: Database, post_data):
    """Handle updating SKR entry. Rahmen/Nummer (und ID) sind fix."""
    id = int(post_data["id"][0])
    framework_nr = post_data.get("framework_nr", [""])[0]
    account = post_data.get("account", [""])[0]
    name = post_data["name"][0]
    group = post_data["group"][0]
    psp = int(post_data.get("private_share_percent", [0])[0] or 0)
    show = 1 if "show_in_menu" in post_data else 0
    is_standard = 1 if "is_standard" in post_data else 0
    db.update_chart_of_accounts(id, framework_nr, account, name, group,
                                private_share_percent=psp, show_in_menu=show,
                                is_standard=is_standard)
    return 303, "/masterdata/skr"

def handle_delete_skr(db: Database, coa_id_val):
    """SKR-Konto löschen (nur Nicht-Standard, nur wenn nicht referenziert)."""
    if db.coa_is_referenced(coa_id_val):
        return _skr_redirect("Konto wird in Buchungen/Anlagen verwendet und kann nicht gelöscht werden.", "error")
    if db.delete_chart_of_accounts(coa_id_val):
        return _skr_redirect("Konto gelöscht.")
    return _skr_redirect("Konto konnte nicht gelöscht werden (Standard-Konto?).", "error")

def handle_toggle_skr_menu(db: Database, coa_id_val):
    """Menü-Sichtbarkeit eines SKR-Kontos umschalten."""
    db.toggle_coa_show_in_menu(coa_id_val)
    return 303, "/masterdata/skr"

def handle_backup_create(db: Database, post_data):
    """Backup des Benutzer-Datenverzeichnisses erstellen ('db' oder 'all')."""
    from . import backup as backup_mod
    scope = post_data.get('scope', ['all'])[0]
    if scope not in ('db', 'all'):
        scope = 'all'
    try:
        archive, size = backup_mod.create_backup(userctx.user_data_dir(), db.db_name, scope)
        return 303, (f'/miscellaneous?backup=ok'
                     f'&file={quote(os.path.basename(archive))}&size={size}')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 303, f'/miscellaneous?backup=error&msg={quote(str(e))}'


def handle_backup_restore(db: Database, post_data):
    """Backup wiederherstellen (mode: 'overwrite' Default oder 'wipe')."""
    from . import backup as backup_mod
    archive = post_data.get('archive', [''])[0]
    mode = post_data.get('mode', ['overwrite'])[0]
    if not archive:
        return 303, '/miscellaneous?restore=error&msg=Kein+Archiv+ausgew%C3%A4hlt'
    try:
        backup_mod.restore_backup(userctx.user_data_dir(), archive, wipe=(mode == 'wipe'))
        return 303, f'/miscellaneous?restore=ok&file={quote(os.path.basename(archive))}'
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 303, f'/miscellaneous?restore=error&msg={quote(str(e))}'


def handle_db_export(db: Database):
    """Export all DB data as INSERT statements to <user-data>/db-export.sql"""
    import os
    filepath = os.path.join(userctx.user_data_dir(), 'db-export.sql')
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
    import traceback
    from export import datev as datev_module

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


def handle_logo_upload(request_handler):
    """Logo-/Bild-Datei eines Kontakts hochladen (Multipart, Feld 'logofile').

    Browser geben aus Sicherheitsgründen nie den echten Dateipfad preis. Daher wird
    die Datei tatsächlich nach data/logos/ gespeichert und der serverseitige Pfad
    zurückgegeben, den das Logo-Feld dann übernimmt.

    Returns: JSON-bytes {'success': bool, 'path'|'error': str}
    """
    import os
    import re as _re_logo
    from .multipart import first_file

    content_type = request_handler.headers.get('Content-Type', '')
    if 'multipart/form-data' not in content_type:
        return json.dumps({'success': False, 'error': 'Kein Multipart-Upload'}).encode()
    length = int(request_handler.headers.get('Content-Length', 0))
    raw = request_handler.rfile.read(length)
    part = first_file(content_type, raw)
    if not part or not part.content:
        return json.dumps({'success': False, 'error': 'Keine Datei empfangen'}).encode()

    base = os.path.basename(part.filename or 'logo')
    safe = _re_logo.sub(r'[^A-Za-z0-9._-]', '_', base) or 'logo'
    if '.' not in safe:
        safe += '.png'
    # Physisch im logos-Verzeichnis des aktuellen Nutzers ablegen; in der DB und
    # nach außen bleibt der logische Pfad 'data/logos/<datei>' (kein Username in
    # der URL – der Static-Handler löst ihn pro Nutzer auf).
    import userctx
    physical = os.path.join(userctx.user_subdir('logos'), safe)
    try:
        with open(physical, 'wb') as f:
            f.write(part.content)
    except Exception as e:
        return json.dumps({'success': False, 'error': f'Speichern fehlgeschlagen: {e}'}).encode()
    logical = '/'.join(('data', 'logos', safe))
    return json.dumps({'success': True, 'path': logical}).encode()


def handle_wiso_import(request_handler, db: Database):
    """WISO Mein Büro CSV-Datei importieren (Multipart-Upload).

    Erwartet eine Datei im Formularfeld „csvfile" (enctype=multipart/form-data).

    Returns:
        (303, location_str) – immer ein Redirect zu /miscellaneous
    """
    from urllib.parse import quote
    from .multipart import parse_multipart

    content_type = request_handler.headers.get('Content-Type', '')
    if 'multipart/form-data' not in content_type:
        return 303, '/miscellaneous?wiso_import=error&msg=Kein+Multipart+Form+Data'

    content_length = int(request_handler.headers['Content-Length'])
    raw = request_handler.rfile.read(content_length)

    parts = parse_multipart(content_type, raw)

    # CSRF: klassisches Multipart-Formular – Token steckt als Feld im Body
    # (das Footer-JS ergänzt es beim Submit). Nur im Mehrbenutzer-Modus aktiv.
    if userctx.auth_enabled():
        csrf_part = next((p for p in parts if p.name == 'csrf' and not p.is_file), None)
        csrf_token = csrf_part.content.decode('utf-8', 'replace').strip() if csrf_part else ''
        if not auth.check_csrf(request_handler._session_token(), csrf_token):
            return 303, '/miscellaneous?wiso_import=error&msg=Ung%C3%BCltiges+Sicherheits-Token'

    # Datei-Inhalt aus Multipart-Body extrahieren (robuster email-Parser)
    part = next((p for p in parts if p.is_file), None)
    csv_bytes = part.content if part else None
    if not csv_bytes:
        return 303, '/miscellaneous?wiso_import=error&msg=Keine+Datei+im+Upload'
    filename = os.path.basename(part.filename or '')

    try:
        result = db.import_wiso_csv(csv_bytes)
        result['filename'] = filename
        imported  = result['imported']
        skipped   = result['skipped']
        errs      = result['errors']

        # Nach WISO-Import: Bank↔Entry-Verknüpfung durchführen
        link_result = db.link_bank_to_entries()
        linked_count = link_result.get('linked', 0)
        resolved_count = link_result.get('resolved', 0)

        # Detailergebnis für Anzeige auf der Seite persistieren
        result_path = os.path.join(userctx.user_data_dir(), 'wiso_import_result.json')
        try:
            with open(result_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # Nicht kritisch

        return 303, (
            f'/miscellaneous?wiso_import=ok'
            f'&file={quote(filename)}'
            f'&imported={imported}&updated={result.get("updated",0)}&skipped={skipped}'
            f'&err_count={len(errs)}'
            f'&not_found={len(result.get("not_found", []))}'
            f'&missing_coa={len(result.get("missing_coa", []))}'
            f'&missing_skr={len(result.get("missing_skr", []))}'
            f'&linked={linked_count}'
            f'&resolved={resolved_count}'
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 303, f'/miscellaneous?wiso_import=error&msg={quote(str(e))}'


def handle_execute_sql(db: Database, post_data):
    """Handle SQL command execution – returns JSON with results."""
    import sqlite3, json
    
    sql_commands = post_data.get("sql_commands", [""])[0]
    
    if not sql_commands.strip():
        return json.dumps({"success_count": 0, "total": 0, "errors": ["Keine SQL-Befehle eingegeben."], "output": ""})
    
    # Split commands by semicolon and filter out empty ones
    commands = [cmd.strip() for cmd in sql_commands.split(';') if cmd.strip()]
    
    output_lines = []
    errors = []
    success_count = 0
    
    conn = sqlite3.connect(db.db_name)
    cursor = conn.cursor()
    
    try:
        for i, command in enumerate(commands, 1):
            try:
                cursor.execute(command)
                success_count += 1
                # SELECT-Ergebnis ausgeben
                if command.lstrip().upper().startswith('SELECT'):
                    col_names = [d[0] for d in cursor.description] if cursor.description else []
                    if col_names:
                        output_lines.append(';'.join(col_names))
                    for row in cursor.fetchall():
                        output_lines.append(';'.join(str(v) if v is not None else '' for v in row))
            except sqlite3.Error as e:
                errors.append(f"Befehl {i}: {str(e)}")
        
        conn.commit()
        
        return json.dumps({
            "success_count": success_count,
            "total": len(commands),
            "errors": errors,
            "output": '\n'.join(output_lines)
        }, ensure_ascii=False)
        
    except Exception as e:
        conn.rollback()
        return json.dumps({"success_count": 0, "total": len(commands), "errors": [str(e)], "output": ""}, ensure_ascii=False)
    finally:
        conn.close()

def handle_add_contact(db: Database, post_data):
    """Handle adding a new contact (Option C schema)"""
    from urllib.parse import quote

    def _get(key, default=''):
        return post_data.get(key, [default])[0]

    # Validate abbreviation uniqueness before touching the DB
    abbr = _get('abbreviation').strip().upper() or None
    if abbr:
        is_unique, _ = db.check_abbreviation_unique(abbr)
        if not is_unique:
            return 303, f'/masterdata/contacts?error={quote(f"Kürzel bereits vergeben: {abbr}")}'

    # Multi-value fields (checkboxes). 'own' ist Systemtyp und mit anderen Typen
    # kombinierbar; ist es gewählt, wird es Primärtyp, die übrigen bleiben Links.
    type_keys  = post_data.get('type_keys', [])
    role_keys  = post_data.get('role_keys', [])
    if 'own' in type_keys:
        primary_type = 'own'
    else:
        primary_type = type_keys[0] if type_keys else _get('contact_type', 'customer')

    try:
        db.insert_contact(
            contact_type      = primary_type,
            entity_type       = _get('entity_type',  'company'),
            display_name      = _get('display_name') or None,
            customer_number   = _get('customer_number') or None,
            abbreviation      = abbr,
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
            job_title         = _get('job_title'),
            department        = _get('department'),
            is_primary_contact= 1 if _get('is_primary_contact') == '1' else 0,
            type_keys         = type_keys,
            role_keys         = role_keys,
        )
        return 303, '/masterdata/contacts'
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f'Fehler beim Hinzufügen des Kontakts: {str(e)}'


def handle_update_contact(db: Database, post_data):
    """Handle updating an existing contact (Option C schema)"""
    from urllib.parse import quote

    def _get(key, default=''):
        return post_data.get(key, [default])[0]

    contact_id = int(_get('contact_id', 0))

    # Validate abbreviation uniqueness before touching the DB
    abbr = _get('abbreviation').strip().upper() or None
    if abbr:
        is_unique, _ = db.check_abbreviation_unique(abbr, exclude_id=contact_id)
        if not is_unique:
            return 303, f'/masterdata/contacts/edit?id={contact_id}&error={quote(f"Kürzel bereits vergeben: {abbr}")}'

    # Multi-value fields (checkboxes). 'own' ist Systemtyp (Spalte ContactType) und
    # darf mit anderen Typen kombiniert werden (z.B. eigene Firma als Kunde einer
    # anderen). Ist 'own' gewählt, wird es der Primärtyp; die übrigen bleiben Links.
    type_keys    = post_data.get('type_keys', [])
    role_keys    = post_data.get('role_keys', [])
    if 'own' in type_keys:
        primary_type = 'own'
    else:
        primary_type = type_keys[0] if type_keys else _get('contact_type', 'customer')

    try:
        db.update_contact(
            contact_id        = contact_id,
            contact_type      = primary_type,
            entity_type       = _get('entity_type',  'company'),
            display_name      = _get('display_name') or None,
            customer_number   = _get('customer_number') or None,
            abbreviation      = abbr,
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
            job_title         = _get('job_title'),
            department        = _get('department'),
            is_primary_contact= 1 if _get('is_primary_contact') == '1' else 0,
            type_keys         = type_keys,
            role_keys         = role_keys,
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
    
    return 303, "/masterdata/articles"

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
    
    return 303, "/masterdata/articles"


def handle_add_number_range(db: Database, post_data):
    """Handle adding a new number range"""
    range_type = post_data.get('type', ['invoice'])[0]
    year = int(post_data.get('year', ['2026'])[0])
    letter = post_data.get('letter', ['R'])[0].upper()
    prefix = post_data.get('prefix', [''])[0].upper()
    current_number = int(post_data.get('current_number', ['0'])[0] or 0)
    description = post_data.get('description', [''])[0]
    number_format = post_data.get('number_format', ['{yy}{l}{nnn}{s}'])[0].strip() or '{yy}{l}{nnn}{s}'

    db.insert_number_range(
        range_type=range_type,
        year=year,
        letter=letter,
        prefix=prefix,
        current_number=current_number,
        description=description,
        number_format=number_format
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
    number_format = post_data.get('number_format', ['{yy}{l}{nnn}{s}'])[0].strip() or '{yy}{l}{nnn}{s}'

    db.update_number_range(
        range_id=range_id,
        year=year,
        letter=letter,
        prefix=prefix,
        current_number=current_number,
        description=description,
        number_format=number_format
    )

    return 303, "/masterdata/numberranges"


def handle_update_invoice_status(post_body: bytes):
    """Update invoice status and increment number range if finalizing

    Returns tuple: (status_code, json_body)
    """
    data = json.loads(post_body.decode('utf-8'))
    invoice_id = int(data.get('invoice_id'))
    new_status = data.get('status')
    
    if not invoice_id or not new_status:
        return 400, '{"success": false, "error": "Fehlende Parameter"}'
    
    db = Database()
    
    # Validate status
    valid_statuses = ['draft', 'finalized', 'sent', 'partial_payment', 'overdue', 'paid', 'cancelled']
    if new_status not in valid_statuses:
        return 400, '{"success": false, "error": "Ung\u00fcltiger Status"}'
    
    # Get current invoice
    invoice = db.get_invoice_by_id(invoice_id)
    if not invoice:
        return 404, '{"success": false, "error": "Rechnung nicht gefunden"}'
    
    current_status = invoice[40] or 'draft'
    
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
    
    # Validate: 'paid' requires at least one linked payment
    if new_status == 'paid':
        payments = db.get_invoice_payments(invoice_id)
        if not payments:
            return 400, '{"success": false, "error": "Bezahlt nur m\u00f6glich, wenn mindestens eine Zahlung verkn\u00fcpft ist."}'
    
    # Update status
    db.update_invoice_status(invoice_id, new_status)
    print(f"Invoice {invoice_id} status updated to: {new_status}")
    
    return 200, f'{{"success": true, "invoice_id": {invoice_id}}}'


def link_booking_to_invoice_capped(db, invoice_id, booking_id, amount=None):
    """Buchung einer Rechnung als Zahlung zuordnen (idempotent, gekappt).

    Zuordnungsbetrag = min(gewünschter Betrag, offener Rechnungsrest, freier
    Rest der Buchung) – Rechnungen werden nie überbucht, Überschüsse bleiben
    als freier Buchungsrest dem Kunden zuordenbar (todo #2). amount=None
    bedeutet "so viel wie möglich".

    Returns (True, None) bei Erfolg, sonst (False, Fehlermeldung).
    """
    invoice = db.get_invoice_by_id(invoice_id)
    if not invoice or (len(invoice) > 45 and invoice[45] == 'quote'):
        return False, "Rechnung nicht gefunden"
    if any(a[1] == invoice_id for a in db.get_booking_allocations(booking_id)):
        return False, "Buchung ist bereits mit dieser Rechnung verknüpft"
    due = invoice[39] if invoice[39] is not None else (invoice[38] or Decimal(0))
    alloc = min(due, db.get_booking_unallocated_amount(booking_id))
    if amount is not None:
        alloc = min(alloc, Decimal(str(amount)))
    if alloc <= 0:
        return False, "Kein offener Betrag zuordenbar (Rechnung oder Buchung ausgeschöpft)"
    db.link_invoice_to_transaction(invoice_id, booking_id, alloc)
    return True, None


def handle_link_invoice_payment(post_body: bytes):
    """Link invoice to a payment booking via InvoicePayments table.

    Returns tuple: (status_code, message_or_path)
    """
    data = json.loads(post_body.decode('utf-8'))
    invoice_id = int(data.get('invoice_id') or 0)
    transaction_id = int(data.get('transaction_id') or 0)
    amount_paid = data.get('amount_paid')
    if amount_paid in (None, ''):
        amount_paid = None

    if not invoice_id or not transaction_id:
        return 400, "Ungültige Parameter"

    db = Database()

    try:
        ok, err = link_booking_to_invoice_capped(db, invoice_id, transaction_id,
                                                 amount_paid)
        if not ok:
            return 400, err
        return 200, f"/invoice/view?id={invoice_id}"
    except Exception as e:
        print(f"Error linking payment: {e}")
        return 500, "Fehler beim Verknüpfen"


def handle_delete_invoice_payment(post_body: bytes):
    """Remove an InvoicePayments entry.

    Returns tuple: (status_code, message)
    """
    data = json.loads(post_body.decode('utf-8'))
    payment_id = int(data.get('payment_id'))

    if not payment_id:
        return 400, "Missing payment_id"

    db = Database()

    try:
        db.delete_invoice_payment(payment_id)
        return 200, "ok"
    except Exception as e:
        print(f"Error deleting invoice payment: {e}")
        return 500, str(e)


def _rate_to_pct(value, default=Decimal('19')):
    """Steuersatz robust als Prozentzahl liefern (z.B. 19, 7, 0).

    Akzeptiert sowohl Bruch-Darstellung (0.19) als auch Prozent (19). Werte
    zwischen 0 und 1 werden als Bruch interpretiert und mit 100 multipliziert.
    Leere/None-Werte ergeben den Default.
    """
    if value is None or value == '':
        return default
    r = Decimal(str(value))
    if 0 < r < 1:
        r *= 100
    return r


def recompute_invoice_totals(items, default_rate_pct):
    """Berechnet Rechnungs- und Positionssummen serverseitig exakt.

    Den vom Client gelieferten Summen wird NICHT vertraut – sie werden hier aus
    Menge x Einzelpreis je Position neu berechnet (kaufmaennische Rundung auf
    Cent, exakte Integer-Addition in Minor Units). Die Steuer wird je
    Steuersatz-Gruppe gerundet.

    Args:
        items: Liste der Positions-Dicts (quantity, unitPrice, taxRate).
        default_rate_pct: Steuersatz (Prozent) als Fallback fuer Positionen
            ohne eigenen taxRate.

    Returns:
        (sum_net, tax_amount, sum_gross, line_nets) – alle Geldwerte als
        Euro-Decimal; line_nets ist die Liste der Positions-Nettosummen
        (gleiche Reihenfolge wie items).
    """
    line_net_minors = []
    net_by_rate = {}                      # Decimal(rate_pct) -> Netto-Summe (Minor)
    for item in items:
        qty = item.get('quantity')
        qty = 1 if qty in (None, '') else qty
        price_minor = to_minor(item.get('unitPrice') or 0)
        line_net = round_minor(multiply(price_minor, qty), 2)
        line_net_minors.append(line_net)
        rate = _rate_to_pct(item.get('taxRate'), default_rate_pct)
        net_by_rate[rate] = net_by_rate.get(rate, 0) + line_net

    sum_net_minor = sum(line_net_minors)
    tax_minor = sum(round_minor(tax_from_net(net, rate), 2)
                    for rate, net in net_by_rate.items())
    gross_minor = sum_net_minor + tax_minor

    return (from_minor(sum_net_minor), from_minor(tax_minor),
            from_minor(gross_minor), [from_minor(n) for n in line_net_minors])


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
        show_tax = data.get('showTax', True)   # False = Kleinunternehmer (§19), keine USt
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

        # Phase 2: Summen serverseitig exakt neu berechnen – den vom Client
        # gelieferten net/tax/gross-Werten wird bewusst nicht vertraut. Bei
        # Kleinunternehmer (§19) wird ohne Steuer gerechnet (Brutto = Netto).
        net_amount, tax_amount, gross_amount, line_nets = recompute_invoice_totals(
            items, _rate_to_pct(tax_rate if show_tax else 0))
        # Sentinel -1 in TaxRate kennzeichnet "kein USt-Ausweis" (vs. echte 0%).
        stored_tax_rate = tax_rate if show_tax else -1

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
            'tax_rate': stored_tax_rate,
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
                invoice_data['tax_category'], invoice_data['tax_rate'],
                to_minor(invoice_data['sum_net'] or 0),
                to_minor(invoice_data['tax_amount'] or 0),
                to_minor(invoice_data['sum_gross'] or 0),
                to_minor(invoice_data['amount_due'] or 0),
                invoice_data['status'],
                invoice_id
            ))
            conn.commit()
            conn.close()
        else:
            # Insert new invoice
            invoice_id = db.insert_invoice(invoice_data)
        
        # Insert invoice items (Positionssumme serverseitig berechnet)
        for idx, item in enumerate(items):
            item_data = {
                'invoice_id': invoice_id,
                'position': item.get('position'),
                'article_id': None,  # No article linkage for now
                'description': item.get('description'),
                'quantity': item.get('quantity'),
                'unit': item.get('unit', 'C62'),  # C62 = piece
                'price_per_unit': item.get('unitPrice'),
                'total_net': line_nets[idx],
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
    from export.pdf_invoice import generate_invoice_pdf
    
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


# ── Angebote (Quotes) ─────────────────────────────────────────────────────────

import re as _re_quote
_QUOTE_STATUSES = {'draft', 'sent', 'accepted', 'rejected', 'converted'}
_RICHTEXT_BLOCK_RE = _re_quote.compile(
    r'<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>', _re_quote.IGNORECASE | _re_quote.DOTALL)
_RICHTEXT_ON_ATTR_RE = _re_quote.compile(r'\son\w+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)',
                                         _re_quote.IGNORECASE)


def _sanitize_richtext(html_text):
    """Leichte Bereinigung des contenteditable-HTML (Phase 1, Single-User).

    Entfernt <script>/<style>-Blöcke und on*=-Eventhandler. Erlaubtes Markup
    (b/i/strong/em/p/br/ul/ol/li/div) bleibt erhalten – mehr braucht der
    Phase-1-PDF-Setzer nicht.
    """
    if not html_text:
        return None
    cleaned = _RICHTEXT_BLOCK_RE.sub('', html_text)
    cleaned = _RICHTEXT_ON_ATTR_RE.sub('', cleaned)
    return cleaned.strip() or None


def handle_quote_save(post_body: bytes):
    """Angebot speichern/aktualisieren. Returns JSON (success, quote_id)."""
    try:
        data = json.loads(post_body.decode('utf-8'))
        quote_id = data.get('quoteId')
        is_update = quote_id is not None

        quote_number = data.get('quoteNumber')
        quote_date = data.get('quoteDate')
        customer_id = data.get('customerId')
        own_company_id = data.get('ownCompanyId')
        valid_until = data.get('validUntil')
        intro_text = _sanitize_richtext(data.get('introText'))
        closing_text = _sanitize_richtext(data.get('closingText'))
        tax_rate = data.get('taxRate')
        show_tax = data.get('showTax', True)   # False = Kleinunternehmer (§19), keine USt
        status = data.get('status', 'draft')
        if status not in _QUOTE_STATUSES:
            status = 'draft'
        items = data.get('items', [])

        if not quote_number or not quote_date or not customer_id or not own_company_id:
            return json.dumps({'success': False, 'error': 'Pflichtfelder fehlen'}).encode()
        if not items:
            return json.dumps({'success': False, 'error': 'Mindestens eine Position erforderlich'}).encode()

        # Summen serverseitig exakt (bei §19 ohne Steuer: Brutto = Netto)
        net_amount, tax_amount, gross_amount, line_nets = recompute_invoice_totals(
            items, _rate_to_pct(tax_rate if show_tax else 0))
        # Sentinel -1 in TaxRate kennzeichnet "kein USt-Ausweis" (vs. echte 0%).
        stored_tax_rate = tax_rate if show_tax else -1

        db = Database()
        customer = db.get_contact_by_id(customer_id)
        if not customer:
            return json.dumps({'success': False, 'error': 'Kunde nicht gefunden'}).encode()
        own_company = db.get_contact_by_id(own_company_id)
        if not own_company:
            return json.dumps({'success': False, 'error': 'Eigene Firma nicht gefunden'}).encode()

        doc = {
            'invoice_number': quote_number,
            'invoice_date': quote_date,
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
            'currency': 'EUR',
            'tax_category': 'S',
            'tax_rate': stored_tax_rate,
            'sum_net': net_amount,
            'tax_amount': tax_amount,
            'sum_gross': gross_amount,
            'amount_due': gross_amount,
            'status': status,
            'document_type': 'quote',
            'valid_until': valid_until,
            'intro_text': intro_text,
            'closing_text': closing_text,
        }

        if is_update:
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM InvoiceItems WHERE InvoiceId = ?', (quote_id,))
            cursor.execute('''
                UPDATE Invoices SET
                    InvoiceNumber=?, InvoiceDate=?, OwnCompanyId=?,
                    SellerName=?, SellerCompany=?, SellerStreet=?, SellerPostalCode=?, SellerCity=?, SellerCountry=?,
                    SellerVATID=?, SellerEmail=?, SellerPhone=?,
                    CustomerId=?, BuyerName=?, BuyerCompany=?, BuyerStreet=?, BuyerPostalCode=?, BuyerCity=?, BuyerCountry=?, BuyerVATID=?,
                    Currency=?, TaxCategory=?, TaxRate=?, SumNet=?, TaxAmount=?, SumGross=?, AmountDue=?,
                    Status=?, DocumentType='quote', ValidUntil=?, IntroText=?, ClosingText=?
                WHERE ID=?
            ''', (
                doc['invoice_number'], doc['invoice_date'], doc['own_company_id'],
                doc['seller_name'], doc['seller_company'], doc['seller_street'], doc['seller_postal_code'], doc['seller_city'], doc['seller_country'],
                doc['seller_vat_id'], doc['seller_email'], doc['seller_phone'],
                doc['customer_id'], doc['buyer_name'], doc['buyer_company'], doc['buyer_street'], doc['buyer_postal_code'], doc['buyer_city'], doc['buyer_country'], doc['buyer_vat_id'],
                doc['currency'], doc['tax_category'], doc['tax_rate'],
                to_minor(doc['sum_net'] or 0), to_minor(doc['tax_amount'] or 0),
                to_minor(doc['sum_gross'] or 0), to_minor(doc['amount_due'] or 0),
                doc['status'], doc['valid_until'], doc['intro_text'], doc['closing_text'],
                quote_id,
            ))
            conn.commit()
            conn.close()
        else:
            quote_id = db.insert_invoice(doc)

        for idx, item in enumerate(items):
            db.insert_invoice_item({
                'invoice_id': quote_id,
                'position': item.get('position'),
                'article_id': None,
                'description': item.get('description'),
                'quantity': item.get('quantity'),
                'unit': item.get('unit', 'C62'),
                'price_per_unit': item.get('unitPrice'),
                'total_net': line_nets[idx],
                'tax_category': 'S',
                'tax_rate': item.get('taxRate', tax_rate),
            })

        return json.dumps({'success': True, 'quote_id': quote_id}).encode()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return json.dumps({'success': False, 'error': str(e)}).encode()


def handle_update_quote_status(post_body: bytes):
    """Angebots-Status setzen. Returns (status_code, json_body)."""
    data = json.loads(post_body.decode('utf-8'))
    quote_id = int(data.get('quote_id'))
    new_status = data.get('status')
    if not quote_id or new_status not in _QUOTE_STATUSES:
        return 400, '{"success": false, "error": "Ungültiger Status"}'
    db = Database()
    quote = db.get_invoice_by_id(quote_id)
    if not quote:
        return 404, '{"success": false, "error": "Angebot nicht gefunden"}'
    db.update_invoice_status(quote_id, new_status)
    return 200, f'{{"success": true, "quote_id": {quote_id}}}'


def handle_convert_quote_to_invoice(post_data):
    """Angebot in Rechnung umwandeln. Returns (303, redirect_path)."""
    raw = post_data.get('quote_id', [None])
    quote_id = raw[0] if isinstance(raw, list) else raw
    if not quote_id:
        return 303, "/quote"
    db = Database()
    try:
        new_invoice_id = db.convert_quote_to_invoice(int(quote_id))
        if new_invoice_id:
            return 303, f"/invoice?id={new_invoice_id}"
        return 303, f"/quote?id={quote_id}"
    except Exception as e:
        print(f"Error converting quote {quote_id}: {e}")
        return 303, f"/quote?id={quote_id}"


def handle_quote_pdf_by_id(quote_id: int):
    """PDF für ein bestehendes Angebot erzeugen. Returns (pdf_bytes, filename)."""
    from export.pdf_quote import generate_quote_pdf
    db = Database()
    try:
        pdf_bytes, pdf_path = generate_quote_pdf(db, quote_id)
        if pdf_bytes is None:
            return None, None
        filename = os.path.basename(pdf_path) if pdf_path else f"Angebot_{quote_id}.pdf"
        return pdf_bytes, filename
    except Exception as e:
        import traceback
        print(f"Error generating quote PDF {quote_id}: {e}")
        traceback.print_exc()
        return None, None


# ── Arbeitszeiten (WorkTimes) ─────────────────────────────────────────────────

def _wt_redirect(person_id, date_from, date_to):
    """Redirect-Ziel zurück zur Arbeitszeiten-Seite (Person + Zeitraum erhalten)."""
    return f"/worktime?person={person_id}&from={date_from}&to={date_to}"


def _wt_resolve_city(db: Database, mode, customer_id, free_text):
    """Arbeitsort-Stadt serverseitig aus dem Modus ableiten (robust, nicht nur JS)."""
    if mode == 'own':
        own = list(db.fetch_contacts(contact_type='own'))
        return (own[0][7] if own else '') or ''
    if mode == 'customer':
        if customer_id:
            c = db.get_contact_by_id(customer_id)
            return (c[7] if c else '') or ''
        return ''
    # 'other'
    return (free_text or '').strip()


import re as _re
_WT_PAUSE_RE = _re.compile(r'(\d{1,2})(?::(\d{2}))?\s*(?:-|–|bis)\s*(\d{1,2})(?::(\d{2}))?')


def _wt_pause_minutes_from_text(text):
    """Summe aller Zeitbereiche im Pausentext (z.B. '12:00-12:30, 15-15:10') in Minuten."""
    total = 0
    for m in _WT_PAUSE_RE.finditer(text or ''):
        s = int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0)
        e = int(m.group(3)) * 60 + (int(m.group(4)) if m.group(4) else 0)
        if e > s:
            total += e - s
    return total


def _wt_parse(db: Database, post_data):
    """Gemeinsames Parsen der Arbeitszeit-Felder. Liefert ein dict für insert/update."""
    g = lambda k, d='': post_data.get(k, [d])[0]
    customer_raw = g('customer_id', '')
    customer_id = int(customer_raw) if customer_raw.strip().isdigit() else None
    mode = g('location_mode', 'customer')
    pause_raw = g('pause_minutes', '0')
    try:
        pause = int(pause_raw)
    except ValueError:
        pause = 0
    # Pausentext hat Vorrang: Minuten daraus ableiten, falls auswertbar
    pause_text = g('pause_text', '').strip()
    derived = _wt_pause_minutes_from_text(pause_text)
    if derived > 0:
        pause = derived
    return {
        'date': g('date', ''),
        'kind': g('kind', 'work'),
        'customer_id': customer_id,
        'start_time': g('start_time', ''),
        'end_time': g('end_time', ''),
        'pause_minutes': pause,
        'location_mode': mode,
        'location_city': _wt_resolve_city(db, mode, customer_id, g('location_city', '')),
        'note': g('note', ''),
        'pause_text': pause_text,
    }


# Arten ohne Überschneidungssperre (ganztägig, keine Zeiten)
_WT_NO_LOCK_KINDS = {'vacation', 'holiday'}


def _wt_minutes(t):
    """'HH:MM' → Minuten seit Mitternacht, oder None."""
    try:
        h, m = t.split(':')
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _wt_has_overlap(db: Database, person_id, fields, exclude_id=None):
    """True, wenn der Eintrag zeitlich mit einem bestehenden Arbeitseintrag desselben
    Tags/derselben Person kollidiert. Urlaub/Feiertag lösen keine Sperre aus und
    kollidieren auch nicht mit anderen Einträgen.
    """
    if fields['kind'] in _WT_NO_LOCK_KINDS:
        return False
    ns, ne = _wt_minutes(fields['start_time']), _wt_minutes(fields['end_time'])
    if ns is None or ne is None:
        return False  # ohne Zeiten keine Überschneidung prüfbar
    for e in db.fetch_worktimes(person_id, fields['date'], fields['date']):
        if exclude_id and e[0] == exclude_id:
            continue
        if (e[3] or 'work') in _WT_NO_LOCK_KINDS:
            continue
        es, ee = _wt_minutes(e[5]), _wt_minutes(e[6])
        if es is None or ee is None:
            continue
        if ns < ee and es < ne:        # echte Zeitüberschneidung
            return True
    return False


def _wt_overlap_redirect(person_id, date_from, date_to, date):
    msg = quote(f"Überschneidung am {date}: in diesem Zeitraum existiert bereits ein Eintrag.")
    return 303, f"/worktime?person={person_id}&from={date_from}&to={date_to}&error={msg}"


def handle_add_worktime(db: Database, post_data):
    """Neuen Arbeitszeit-Eintrag anlegen (mit Überschneidungssperre)."""
    g = lambda k, d='': post_data.get(k, [d])[0]
    person_id = int(g('person_id', '0') or 0)
    date_from = g('from', '')
    date_to = g('to', '')
    fields = _wt_parse(db, post_data)
    if person_id and fields['date']:
        if _wt_has_overlap(db, person_id, fields):
            return _wt_overlap_redirect(person_id, date_from, date_to, fields['date'])
        db.insert_worktime(person_id=person_id, **fields)
    return 303, _wt_redirect(person_id, date_from, date_to)


def handle_update_worktime(db: Database, post_data):
    """Bestehenden Arbeitszeit-Eintrag aktualisieren (mit Überschneidungssperre)."""
    g = lambda k, d='': post_data.get(k, [d])[0]
    person_id = int(g('person_id', '0') or 0)
    date_from = g('from', '')
    date_to = g('to', '')
    worktime_id = int(g('id', '0') or 0)
    fields = _wt_parse(db, post_data)
    if worktime_id and fields['date']:
        if _wt_has_overlap(db, person_id, fields, exclude_id=worktime_id):
            return _wt_overlap_redirect(person_id, date_from, date_to, fields['date'])
        db.update_worktime(worktime_id, **fields)
    return 303, _wt_redirect(person_id, date_from, date_to)


def handle_worktime_pdf(db: Database, person_id, date_from, date_to, with_notes=False):
    """PDF-Stundenzettel erzeugen. Returns (pdf_bytes, filename) oder (None, None)."""
    from export.pdf_worktime import generate_worktime_pdf
    try:
        pdf_bytes, pdf_path = generate_worktime_pdf(db, int(person_id), date_from, date_to,
                                                    with_notes=with_notes)
        if pdf_bytes is None:
            return None, None
        filename = os.path.basename(pdf_path) if pdf_path else f"Stundenzettel_{person_id}.pdf"
        return pdf_bytes, filename
    except Exception as e:
        import traceback
        print(f"Error generating worktime PDF: {e}")
        traceback.print_exc()
        return None, None


# ── Fahrten (Fahrtenbuch) ─────────────────────────────────────────────────────
def _trip_redirect(person_id, date_from, date_to):
    return f"/trips?person={person_id}&from={date_from}&to={date_to}"


def _trip_int(value):
    """Leeren/ungültigen Text → None, sonst Integer."""
    value = (value or '').strip()
    if value == '':
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _trip_parse(post_data):
    """Fahrt-Felder aus dem POST-Body ziehen (ohne DriverID/Zeitraum).

    Sind beide Tachostände (Start/Ende) gesetzt und plausibel, ist die Differenz
    maßgeblich für die gefahrenen km – auch ohne mitgesendeten Client-Wert.
    """
    g = lambda k, d='': post_data.get(k, [d])[0]
    start_km = _trip_int(g('start_km', ''))
    end_km = _trip_int(g('end_km', ''))
    distance_km = _trip_int(g('distance_km', ''))
    if start_km is not None and end_km is not None and end_km >= start_km:
        distance_km = end_km - start_km
    return {
        'start_date': g('start_date', ''),
        'start_time': g('start_time', ''),
        'end_date': g('end_date', ''),
        'end_time': g('end_time', ''),
        'start_point': g('start_point', '').strip(),
        'destination': g('destination', '').strip(),
        'vehicle': g('vehicle', '').strip(),
        'reason': g('reason', '').strip(),
        'distance_km': distance_km,
        'start_km': start_km,
        'end_km': end_km,
        'document_id': _trip_int(g('document_id', '')),
    }


def handle_add_trip(db: Database, post_data):
    """Neue Fahrt anlegen (benötigt Fahrer, Start-Datum und Ziel)."""
    g = lambda k, d='': post_data.get(k, [d])[0]
    person_id = int(g('person_id', '0') or 0)
    date_from = g('from', '')
    date_to = g('to', '')
    fields = _trip_parse(post_data)
    if person_id and fields['start_date'] and fields['destination']:
        db.insert_trip(driver_id=person_id, **fields)
    return 303, _trip_redirect(person_id, date_from, date_to)


def handle_update_trip(db: Database, post_data):
    """Bestehende Fahrt aktualisieren."""
    g = lambda k, d='': post_data.get(k, [d])[0]
    person_id = int(g('person_id', '0') or 0)
    date_from = g('from', '')
    date_to = g('to', '')
    trip_id = int(g('id', '0') or 0)
    fields = _trip_parse(post_data)
    if trip_id and fields['start_date'] and fields['destination']:
        db.update_trip(trip_id, **fields)
    return 303, _trip_redirect(person_id, date_from, date_to)


# ── Authentifizierung (TODO #4) ──────────────────────────────────────────────
def handle_setup_mode(post_data):
    """Betriebsmodus bei der Ersteinrichtung wählen. Returns Redirect-Location.

    'multi' → Administrator anlegen (/setup-admin); 'single' → eigene
    Kontaktdaten (/setup, wie gehabt).
    """
    import userctx
    mode = post_data.get('mode', [''])[0]
    if mode not in ('single', 'multi'):
        return '/setup'
    userctx.set_mode(mode)
    return '/setup-admin' if mode == 'multi' else '/setup'


def handle_login(post_data):
    """Anmeldedaten prüfen. Returns (ok, username, error_msg)."""
    g = lambda k: post_data.get(k, [''])[0]
    username = g('username').strip()
    password = g('password')
    if username and auth.authenticate(username, password):
        return True, username, None
    return False, username, "Benutzername oder Passwort falsch."


def _users_redirect(info=None, err=None):
    parts = []
    if info:
        parts.append('info=' + quote(info))
    if err:
        parts.append('err=' + quote(err))
    return '/users' + ('?' + '&'.join(parts) if parts else '')


def handle_user_create(post_data):
    """Neuen Benutzer anlegen (Admin-Aktion). Returns Redirect-Location."""
    g = lambda k: post_data.get(k, [''])[0]
    username = g('username').strip()
    password = g('password')
    is_admin = g('is_admin') in ('1', 'true', 'on')
    try:
        auth.create_user(username, password, is_admin=is_admin)
    except ValueError as e:
        return _users_redirect(err=str(e))
    return _users_redirect(info=f'Benutzer „{username}" angelegt.')


def handle_user_delete(post_data):
    """Benutzer (Login + Sessions) löschen. Die Buchungsdaten bleiben auf der
    Festplatte erhalten. Schützt den letzten Administrator."""
    username = post_data.get('username', [''])[0].strip()
    if not auth.user_exists(username):
        return _users_redirect(err='Benutzer existiert nicht.')
    if auth.is_admin(username) and auth.count_admins() <= 1:
        return _users_redirect(err='Der letzte Administrator kann nicht gelöscht werden.')
    auth.delete_user(username)
    return _users_redirect(info=f'Benutzer „{username}" gelöscht (Daten bleiben erhalten).')


def handle_user_reset_password(post_data):
    g = lambda k: post_data.get(k, [''])[0]
    username = g('username').strip()
    password = g('password')
    if not auth.user_exists(username):
        return _users_redirect(err='Benutzer existiert nicht.')
    try:
        auth.set_password(username, password)
    except ValueError as e:
        return _users_redirect(err=str(e))
    return _users_redirect(info=f'Passwort für „{username}" gesetzt.')


def handle_user_toggle_admin(post_data):
    username = post_data.get('username', [''])[0].strip()
    if not auth.user_exists(username):
        return _users_redirect(err='Benutzer existiert nicht.')
    currently_admin = auth.is_admin(username)
    if currently_admin and auth.count_admins() <= 1:
        return _users_redirect(err='Der letzte Administrator kann nicht herabgestuft werden.')
    auth.set_admin(username, not currently_admin)
    state = 'Administrator' if not currently_admin else 'normaler Benutzer'
    return _users_redirect(info=f'„{username}" ist jetzt {state}.')


def handle_setup_admin(post_data):
    """Erstes Administrator-Konto anlegen. Returns (ok, username, error_msg)."""
    g = lambda k: post_data.get(k, [''])[0]
    username = g('username').strip()
    password = g('password')
    password2 = g('password2')
    if auth.has_any_user():
        return False, username, "Es existiert bereits ein Benutzer."
    if password != password2:
        return False, username, "Die Passwörter stimmen nicht überein."
    try:
        auth.create_user(username, password, is_admin=True)
    except ValueError as e:
        return False, username, str(e)
    return True, username, None


def handle_setup_save(db: Database, post_data: dict):
    """Speichert die Daten aus der Ersteinrichtungs-Seite.

    Erstellt einen 'own'-Kontakt sowie (optional) ein Bankkonto.
    Gibt (303, '/') bei Erfolg oder (200, html) bei Validierungsfehler zurück.
    """
    from .pages_setup import PageSetup

    # post_data stammt aus parse_qs → Werte sind Listen; ersten Wert ziehen.
    def g(key, default=''):
        v = post_data.get(key, [default])
        return (v[0] if isinstance(v, list) else v) or default

    company_name = g('company_name').strip()
    if not company_name:
        return 200, PageSetup(db, message='Bitte gib mindestens den Firmennamen ein.')

    try:
        db.insert_contact(
            contact_type='own',
            entity_type='company',
            display_name=company_name,
            email=g('email').strip(),
            phone=g('phone').strip(),
            street=g('street').strip(),
            postal_code=g('postal_code').strip(),
            city=g('city').strip(),
            country=g('country', 'DE').strip() or 'DE',
            company_name=company_name,
            legal_form=g('legal_form').strip(),
            tax_id=g('tax_id').strip(),
        )
    except Exception as e:
        return 200, PageSetup(db, message=f'Fehler beim Speichern der Kontaktdaten: {e}')

    # Optionales Bankkonto anlegen (nur wenn IBAN angegeben)
    iban = g('iban').replace(' ', '').strip()
    if iban:
        bank_name_label = g('bank_name_label').strip() or 'Bankkonto'
        bic = g('bic').strip()
        bank_name = g('bank_name').strip()
        try:
            db.insert_account(
                name=bank_name_label,
                holder=company_name,
                number=iban,
                bic=bic,
                bank_name=bank_name,
                is_cash=0,
                skr_account=1200,
            )
        except Exception as e:
            return 200, PageSetup(db, message=f'Kontaktdaten gespeichert, aber Fehler beim Bankkonto: {e}')

    return 303, '/'


def handle_load_testdata(db: Database):
    """Lädt Testdaten (Kontakte + Bankkonto) und leitet zum Dashboard weiter."""
    db.load_test_seed_data()
    return 303, '/'
