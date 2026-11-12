# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
POST request handlers for form submissions
"""
import os
import json
import math
import sqlite3
import datetime
import time
from urllib.parse import quote
import auth
import userctx
from .pages import Header1, Header2, Header3, Footer
from .import_preview import match_account
from .period import parse_iso_date, valid_period
from .richtext import sanitize_richtext as _sanitize_richtext
from db import Database
from db.core import fk_explained, ForeignKeyError
from db.invoices import InvoiceCol
from db.invoice_payments import RECEIVABLE_STATUSES
from db.matching import FORM_LIQUID_FIRST, FORM_TRANSFER, is_bank_effective, is_mirror_row, purpose_in_counter, read_entry_form
from db.accounts import DEFAULT_CASH_ACCOUNT
from money import to_minor, from_minor, multiply, tax_from_net, tax_from_gross, round_minor, fmt_eur
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

def _normalize_ws(s):
    return ' '.join((s or '').split())


def backfill_import_text(db, trans_date, trans_amount, account_id, new_text):
    """Text-Backfill für als Duplikat übersprungene Import-Transaktionen.

    Der frühere VBR-Parser schnitt Verwendungszwecke ab; ein Re-Import legt
    wegen der Duplikat-Erkennung nichts neu an. Hier wird der gespeicherte
    Text aktualisiert, wenn er leer oder ein Präfix des neu geparsten,
    längeren Texts ist. Nur bei genau EINEM passenden Kandidaten (mehrere
    gleiche Beträge am selben Tag wären mehrdeutig).

    Bekannte Heuristik-Grenzen: Manuell ABWEICHEND gepflegte Texte bleiben
    unberührt, aber (a) ein manuell auf einen Präfix GEKÜRZTER Text ist von
    Parser-Abschnitt nicht unterscheidbar und wird wieder verlängert;
    (b) ein bewusst GELEERTER Text wird neu befüllt. Beides ist gewollt
    konservativ zugunsten der Reparatur abgeschnittener Import-Texte.

    Returns True, wenn ein Text aktualisiert wurde.
    """
    new_norm = _normalize_ws(new_text)
    if not new_norm:
        return False
    candidates = []
    for booking_id, old_text in db.get_bookings_by_import_key(
            trans_date, trans_amount, account_id):
        old_norm = _normalize_ws(old_text)
        if old_norm == new_norm:
            continue                      # bereits vollständig
        if not old_norm or (new_norm.startswith(old_norm)
                            and len(new_norm) > len(old_norm)):
            candidates.append(booking_id)
    if len(candidates) != 1:
        return False
    db.update_booking_text(candidates[0], new_text)
    return True


def handle_confirm_import(db: Database, post_data):
    """Kontoauszug-Import bestätigen – einzelner Beleg oder alle.

    Erwartet ``import_id`` und optional ``file_index`` (genau ein Beleg);
    ohne ``file_index`` werden alle noch nicht importierten Belege übernommen.
    Das Konto wird pro Beleg über dessen IBAN aufgelöst, damit Auszüge
    verschiedener Konten korrekt zugeordnet werden.

    Je Beleg nennt ``results`` ``inserted``, ``skipped`` (Duplikate) und davon
    ``skipped_upload`` (schon durch einen anderen Beleg desselben Laufs da) sowie
    ``skipped_invalid`` (Umsätze ohne Datum oder Betrag: nicht importiert, keine
    Duplikate und nicht in ``skipped``). ``skipped_upload`` ist beim Einzelimport
    immer 0: jeder Aufruf liest den Bestand neu.

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

        # Zählbasierte Duplikat-Erkennung, JE DATEI: Der Bestand je Schlüssel
        # (Datum/Betrag/Konto) wird beim ersten Auftreten in der Datei gelesen und
        # enthält damit auch, was frühere Dateien dieses Laufs angelegt haben;
        # übersprungen werden nur so viele Umsätze, wie der Bestand bereits hat.
        # So bleiben mehrfach identische Umsätze innerhalb EINER Datei erhalten,
        # über mehrere Dateien gilt aber das Maximum statt der Summe (Überlappung
        # oder dieselbe Datei zweimal verdoppelt nichts) – „Alle importieren“
        # verhält sich wie die Einzelimporte nacheinander.
        # run_db_counts hält den Bestand VOR diesem Lauf: ein Duplikat oberhalb
        # davon geht auf eine andere Datei dieses Uploads zurück.
        run_db_counts = {}

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
            skipped_upload = 0    # davon: schon durch eine andere Datei dieses Laufs da
            skipped_invalid = 0   # ohne Datum oder Betrag: nicht importierbar, kein Duplikat
            text_updated = 0
            dup_db_counts = {}
            dup_seen = {}
            # Backfill-Kandidaten je Duplikatschlüssel sammeln und erst NACH
            # der Transaktionsschleife anwenden: enthält die Datei mehrere
            # Duplikate desselben Schlüssels mit VERSCHIEDENEN Texten, ist die
            # Zuordnung mehrdeutig → kein Backfill (sonst könnte dieselbe
            # Buchung mehrfach bzw. mit dem falschen Text aktualisiert werden).
            backfill_texts = {}   # dup_key -> {normalisierte Texte}
            backfill_raw = {}     # dup_key -> (date, amount, account_id, text)
            for trans in fl.get('transactions', []):
                recipient = trans.get('recipient', '') or ''
                text = trans.get('reference', '') or ''
                foreign_iban = trans.get('foreign_iban', '') or ''
                trans_date = trans.get('date')
                trans_amount = trans.get('amount')

                if not trans_date or trans_amount is None:
                    skipped_invalid += 1
                    continue
                dup_key = (trans_date, round(float(trans_amount), 2), account_id)
                if dup_key not in dup_db_counts:
                    dup_db_counts[dup_key] = db.check_booking_exists(
                        trans_date, trans_amount, account_id, foreign_iban, text)
                    run_db_counts.setdefault(dup_key, dup_db_counts[dup_key])
                seen = dup_seen.get(dup_key, 0)
                dup_seen[dup_key] = seen + 1
                if seen < dup_db_counts[dup_key]:
                    # Duplikat: nicht neu anlegen; Text für den späteren
                    # Backfill vormerken (todo Buchungstext)
                    backfill_texts.setdefault(dup_key, set()).add(_normalize_ws(text))
                    backfill_raw[dup_key] = (trans_date, trans_amount,
                                             account_id, text)
                    skipped += 1
                    if seen >= run_db_counts[dup_key]:
                        skipped_upload += 1
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

            # Text-Backfill: nur für Schlüssel mit genau EINEM eindeutigen
            # Text; pro Schlüssel höchstens ein Update pro Lauf.
            for dup_key, texts in backfill_texts.items():
                if len(texts) != 1:
                    continue
                b_date, b_amount, b_account, b_text = backfill_raw[dup_key]
                if backfill_import_text(db, b_date, b_amount, b_account, b_text):
                    text_updated += 1

            fl['imported'] = True
            results.append({'file_index': i, 'filename': fl.get('filename'),
                            'inserted': inserted, 'skipped': skipped,
                            'skipped_upload': skipped_upload,
                            'skipped_invalid': skipped_invalid,
                            'text_updated': text_updated,
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

def handle_transaction_suggest(db: Database, qs: dict):
    """Kontierungs-Vorschlag für eine offene Buchung (todo #1).

    Liefert SKR-Konto und Steuersatz der zuletzt gleich kontierten Buchung
    desselben Empfängers sowie die nächste freie Beleg-Nr. – als JSON, damit
    die Maske die Felder füllt, ohne zu speichern. Verbraucht wird die Nummer
    erst beim Speichern (siehe consume_number).

    War die Vorlage ein Split, kommen alle Teilbuchungen mit; die Maske fragt
    dann nach, ob sie übernommen werden sollen.
    """
    try:
        booking_id = int(qs.get('id', ['0'])[0] or 0)
    except (TypeError, ValueError):
        booking_id = 0
    recipient = (qs.get('recipient', [''])[0] or '').strip()
    booking = db.get_booking_by_id(booking_id) if booking_id else None
    if not recipient and booking:
        recipient = booking[6] or ''

    year = int((booking[1] or '')[:4]) if booking and booking[1] else \
        datetime.datetime.now().year
    docnr, range_id = db.peek_next_number('receipt_company', year)

    hit = db.find_history_suggestion(recipient, exclude_booking_id=booking_id)
    if not hit and not docnr:
        return 200, json.dumps({
            'ok': False,
            'message': 'Keine passende Buchung in der Historie und kein '
                       'Nummernkreis "Belegnummern Firma" hinterlegt.'})

    payload = {'ok': True, 'document_nr': docnr, 'range_id': range_id,
               'recipient': recipient}
    if hit:
        payload.update({
            'source_id': hit['source_id'], 'source_date': hit['date'],
            'source_recipient': hit['recipient'], 'is_split': hit['is_split'],
            'rows': [{
                'coa_id': r['coa_id'],
                'counter_coa_id': r['counter_coa_id'] or '',
                'nobank': r['nobank'],
                'amount': f"{r['amount']:.2f}",
                'tax_rate': (f"{round(float(r['tax_rate']) * 100, 2):g}"
                             if r['tax_rate'] else ''),
                'tax_amount': (f"{r['tax_amount']:.2f}"
                               if r['tax_amount'] is not None else ''),
                'text': r['text'],
            } for r in hit['rows']],
        })
    else:
        payload['message'] = 'Keine ähnliche Buchung gefunden – nur Beleg-Nr.'
        payload['rows'] = []
    return 200, json.dumps(payload)


def _bank_counter_coa(db, account_id):
    """SKR-Konto (COA-ID) eines Bankkontos = liquide Gegenseite der Buchungssätze."""
    if not account_id:
        return None
    acct_row = db.get_account_by_id(account_id)
    if acct_row and acct_row[7]:  # SKRAccount at index 7
        return db.get_coa_id_by_account_number(acct_row[7])
    return None


class _Refused(ValueError):
    """Eine Eingabe, die die Maske nicht schreibt; der Text geht als Klartext an den Nutzer (400)."""


def _refuse_same_account(db, coa_id, counter_coa_id, where=''):
    """Kein Schreibweg der Masken schreibt einen Satz mit demselben Konto auf beiden Seiten – er
    buchte nichts und würde in jeder Auswertung als Spiegel oder Zahlung falsch gelesen."""
    if coa_id is not None and coa_id == counter_coa_id:
        number = db.get_coa_id_to_number_map().get(coa_id, coa_id)
        raise _Refused(f'{where}Konto und Gegenkonto wären dasselbe Konto ({number}) – '
                       'so ein Satz bucht nichts. Bitte ein anderes Konto wählen.')


def _plan_split_rows(db, bank_id, post_data, account_coa_id, date, date_tax,
                     recipient, currency):
    """Buchungssätze einer Bankbewegung aus dem Editor lesen und prüfen (schreibt nichts).

    Liefert die geplanten Zeilen für ``_write_split_rows``. ``bank_id`` ist ``None``,
    wenn die Bankbewegung selbst erst noch angelegt wird.

    Die Zeilen kommen als gleichnamige Felder (split_id, split_amount, …); ein
    leeres split_id legt eine neue Teilbuchung an, weggelassene IDs werden
    gelöscht. Die Summe wird NICHT erzwungen – Teilerfassung ist erlaubt und
    an der dreistufigen Statusanzeige der Übersicht erkennbar.

    Neue Zeilen bekommen Datum, Steuerdatum, Empfänger und Währung der
    Bankbewegung; Konto, Steuer, Beleg-Nr. und Text gehören der Teilbuchung. Ein
    bestehender Satz folgt der Bankbewegung in Datum, Steuerdatum und Empfänger
    nur, wenn er den Wert vorher mit ihr geteilt hat (derselbe Wert wie die
    Bankbewegung bis jetzt trägt; leer = leer), je Feld für sich: Ein eigener Wert
    – eine zugeordnete Bestandsbuchung – bleibt über jedes Speichern. Ordnet der
    Editor eine vorhandene Buchung zu, behält sie Datum, Steuerdatum, Empfänger und
    Währung ebenfalls.

    Die Steuer eines bestehenden Satzes schreibt der Editor nur, wenn das Formular
    sie mitbringt: Fehlt das Feld, oder sind beim Zuordnen einer vorhandenen Buchung
    beide Felder leer (so schickt sie ein Browser mit dem alten Skript der Seite),
    bleibt der gespeicherte Wert. Sonst gilt, was in der Zeile steht; leer heißt
    keine Steuer. Auf dem Server wird sie nie nachgerechnet.

    Erst werden ALLE Zeilen gelesen und geprüft, dann geschrieben: Eine ungültige
    Zahl in einer späteren Zeile bricht ab, bevor eine frühere gespeichert ist.
    Bestehende Zeilen bekommen nur die Felder des Editors geschrieben
    (``update_booking_fields``) – Kontakt, Kategorie, Konto und fremdes Konto der
    Teilbuchung führt der Editor nicht und bleiben deshalb erhalten.
    """
    ids = post_data.get('split_id', [])
    amounts = post_data.get('split_amount', [])
    coas = post_data.get('split_coa', [])
    rates = post_data.get('split_tax_rate', [])
    tax_amounts = post_data.get('split_tax_amount', [])
    docnrs = post_data.get('split_docnr', [])
    texts = post_data.get('split_text', [])
    counters = post_data.get('split_counter_coa', [])

    def _at(seq, i, default=''):
        return seq[i] if i < len(seq) else default

    bank_coa_ids = db.get_bank_coa_ids()
    # Orientierung bestehender Sätze merken: liquide-zuerst gebuchte Sätze
    # (COA = Geldkonto, Gegenkonto = Zweckkonto) behalten ihre Richtung – so, wie die
    # gespeicherte Form (Bookings.EntryForm) sie nennt; ein Spiegel und eine Zeile ohne Form
    # lesen sich wie in 2.0.x (purpose_in_counter).
    existing_by_id = {c[0]: (c[1], c[2])
                      for c in (db.get_child_bookings_for_bank(bank_id) if bank_id else [])}
    # Was die Bankbewegung bis jetzt trägt – sie wird erst nach dem Planen geschrieben. Daran zeigt
    # sich, ob ein Satz Datum, Steuerdatum oder Empfänger mit ihr geteilt hat.
    bank_row = db.get_booking_by_id(bank_id) if bank_id else None

    # Schritt 1: alle Zeilen lesen und prüfen (Zahlen, Bestandsbuchungen) – noch
    # ohne zu schreiben.
    planned = []
    for i in range(len(amounts)):
        raw_amount = _at(amounts, i).strip()
        raw_coa = _at(coas, i).strip()
        if not raw_amount and not raw_coa:
            continue                      # komplett leere Zeile ignorieren
        row_id = _at(ids, i).strip()
        sent_rate = _at(rates, i, None)   # None: das Formular bringt das Feld gar nicht mit
        sent_tax = _at(tax_amounts, i, None)
        row_rate = (sent_rate or '').strip()
        row_tax = (sent_tax or '').strip()
        purpose_coa = int(raw_coa) if raw_coa else None
        new_coa, new_counter = purpose_coa, account_coa_id
        # Mitgeliefertes Gegenkonto (Umbuchung): eine per Vorschlag kopierte
        # Zeile bringt es mit, sonst würde aus dem Privatanteil beim Speichern
        # eine Bankbewegung und die Split-Summe stimmte nicht mehr.
        raw_counter = _at(counters, i).strip()
        if raw_counter and int(raw_counter) not in bank_coa_ids:
            new_counter = int(raw_counter)
        old_row = db.get_booking_by_id(int(row_id)) if row_id else None
        old_coa = old_counter = None
        if old_row:
            old_coa, old_counter = old_row[8], old_row[9]
            if is_mirror_row(old_row[22], old_coa, old_counter, bank_coa_ids):
                # Doppik-Spiegel (beide Seiten eigene Bank-/Kassenkonten): kein Satz des Editors.
                # Ordnet er eine bestehende Buchung zu, bleiben ihre Konten, wie sie sind.
                new_coa, new_counter = old_coa, old_counter
            elif purpose_in_counter(old_row[22], old_coa, old_counter, bank_coa_ids):
                # Geldkonto vorne (z. B. Einnahmen aus dem WISO-Import, „Bank an 1460“): Das
                # Geldkonto bleibt, das Feld „SKR-Konto“ meint das Gegenkonto. Steht dort das
                # Geldkonto selbst (eine Buchung, die der Editor zuordnet, kam mit dem Konto
                # der Zeile), hat der Nutzer nichts gewählt: der Satz bleibt, wie er ist.
                new_coa = old_coa
                new_counter = old_counter if purpose_coa == old_coa else purpose_coa
            elif (old_row[22] == FORM_TRANSFER
                    or not is_bank_effective(old_coa, old_counter, bank_coa_ids)):
                # Umbuchung (4405→4400, Privatanteil 6805→2100): sie hat kein
                # Bankkonto als Gegenseite. Würde hier account_coa_id gesetzt,
                # machte das Speichern der Bankmaske aus dem Beleg eine
                # Zahlung – das Gegenkonto bleibt deshalb unangetastet.
                new_counter = old_counter
        if not (old_row and (new_coa, new_counter) == (old_coa, old_counter)):
            _refuse_same_account(db, new_coa, new_counter, f'Zeile {i + 1}: ')
        rid = int(row_id) if row_id else None
        # Zeilen können auch bestehende, bisher unverknüpfte Buchungen
        # sein ("vorhandene Buchung zuordnen"): dann wird die Verbindung
        # per ID hergestellt – Belegnummern spielen dabei keine Rolle.
        adopt = rid is not None and rid not in existing_by_id
        fields = dict(
            date_booking=date,
            date_tax=date_tax,
            amount=float(raw_amount) if raw_amount else 0.0,
            recipient_client=recipient,
            coa_id=new_coa,
            counter_coa_id=new_counter,
            currency=currency,
            text=_at(texts, i),
            document_number=_at(docnrs, i).strip() or None,
        )
        # Steuer: Ein bestehender Satz behält sie, wenn das Formular sie nicht mitbringt – das Feld fehlt,
        # oder beim Zuordnen sind beide leer (so schickt sie ein Browser mit dem alten Skript der Seite,
        # das die Steuer der Buchung nicht in die Zeile übernahm). Sonst gilt, was in der Zeile steht.
        adopted_without_tax = adopt and not row_rate and not row_tax
        if not (old_row and (sent_rate is None or adopted_without_tax)):
            fields['tax_rate'] = float(row_rate) / 100 if row_rate else None
        if not (old_row and (sent_tax is None or adopted_without_tax)):
            fields['tax_amount'] = float(row_tax) if row_tax else None
        if old_row and adopt:
            # Adoption einer bestehenden Buchung: ihre eigenen Stammdaten
            # (Datum, Steuerdatum, Empfänger, Währung) bleiben erhalten –
            # überschrieben würden sonst korrekt erfasste Angaben.
            fields.update(date_booking=old_row[1], date_tax=old_row[2],
                          recipient_client=old_row[6],
                          currency=old_row[12] or 'EUR')
        elif old_row:
            # Satz unter dieser Bankbewegung: Datum, Steuerdatum und Empfänger folgen ihrer Änderung nur,
            # wenn er den Wert mit ihr geteilt hat (derselbe wie bis jetzt, leer = leer). Hat er einen
            # eigenen – eine zugeordnete Bestandsbuchung, ein verknüpfter Beleg –, bleibt der, je Feld.
            for name, column in (('date_booking', 1), ('date_tax', 2), ('recipient_client', 6)):
                if (old_row[column] or None) != (bank_row[column] or None):
                    del fields[name]
        planned.append((rid, fields, adopt))
    return planned


def _write_split_rows(db, bank_id, planned):
    """Geplante Buchungssätze (aus ``_plan_split_rows``) schreiben und entfernte Zeilen löschen."""
    keep_ids = set()
    for rid, fields, adopt in planned:
        if rid is not None:
            if adopt:
                fields['parent_booking_id'] = bank_id
            db.update_booking_fields(rid,
                                     log_description=("Split row adopt" if adopt
                                                      else "Split row update"),
                                     **fields)
            keep_ids.add(rid)
        else:
            new_id = db.insert_booking(booking_type='entry',
                                       parent_booking_id=bank_id,
                                       log_description="Split row insert",
                                       **fields)
            keep_ids.add(new_id)

    # Entfernte Zeilen löschen – Doppik-Spiegel (COA und Gegenkonto beide
    # liquide) tauchen im Editor nicht auf und bleiben deshalb unberührt.
    bank_coa_ids = db.get_bank_coa_ids()
    for child in db.get_child_bookings_for_bank(bank_id):
        child_id, c_coa, c_counter, c_form = child[0], child[1], child[2], child[9]
        if child_id in keep_ids:
            continue
        if is_mirror_row(c_form, c_coa, c_counter, bank_coa_ids):
            continue                      # Doppik-Spiegel: nicht anfassen
        db.delete_transaction(child_id)


def _save_split_rows(db, bank_id, post_data, account_coa_id, date, date_tax,
                     recipient, currency):
    """Buchungssätze einer Bankbewegung aus dem Editor speichern: erst alles lesen und
    prüfen (``_plan_split_rows``), dann schreiben (``_write_split_rows``)."""
    planned = _plan_split_rows(db, bank_id, post_data, account_coa_id, date, date_tax,
                               recipient, currency)
    _write_split_rows(db, bank_id, planned)


def _new_payment_problem(db, invoice_id, account_id, amount, revenue_coa_id=None):
    """Klartext, warum sich eine NEUE Bankbewegung nicht als Zahlung zu dieser Rechnung anlegen
    lässt (kein Zahlungseingang, Rechnung nicht zahlbar, Bankkonto ohne SKR-Nummer, kein
    Erlöskonto …) – ``None``, wenn sie sich zuordnen lässt. Geprüft wird vor dem Anlegen."""
    try:
        positive = to_minor(amount) > 0
    except (ValueError, TypeError):
        positive = False
    if not positive:
        return ('Der Betrag muss größer 0 sein – nur Zahlungseingänge lassen sich einer '
                'Rechnung zuordnen.')
    return db.payment_precheck(invoice_id, account_id, revenue_coa_id)


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
    tax_rate = post_data.get("tax_rate", [""])[0]
    tax_amount = post_data.get("tax_amount", [""])[0]
    document_nr = post_data.get("document_nr", [""])[0]
    invoice_id = post_data.get("invoice_id", [""])[0]
    # Erlöskonto, das der Nutzer wählt, wenn für den Steuersatz der Rechnung keines hinterlegt
    # ist (sonst ohne Wirkung)
    raw_revenue_coa = post_data.get("revenue_coa_id", [""])[0].strip()
    try:
        revenue_coa_id = int(raw_revenue_coa) if raw_revenue_coa else None
    except ValueError:
        revenue_coa_id = -1        # keine Ganzzahl: die Zuordnung lehnt es als „kein Erlöskonto“ ab
    # Buchungssatz-Editor der Bank-Maske aktiv? Dann kommen die Sätze von dort.
    has_split_rows = 'split_amount' in post_data

    try:
        # Der Betrag muss eine endliche Zahl unter einer Billion sein: nan, inf oder 1e30 ließen
        # sich nicht speichern und kämen sonst als 500 mit dem Namen der Ausnahme zurück.
        if _finite_number(amount, None) is None:
            return 400, 'Der Betrag ist keine gültige Zahl.'

        # Convert IDs to int or None
        account_id = int(account_id) if account_id else None
        contact_id = int(contact_id) if contact_id else None
        coa_id = int(coa_id) if coa_id else None
        invoice_id = int(invoice_id) if invoice_id else None

        # Convert tax_rate from percentage to decimal
        tax_rate = float(tax_rate) / 100 if tax_rate else None
        tax_amount = float(tax_amount) if tax_amount else None

        # Eine neue Bankbewegung als Zahlung zu einer Rechnung: erst prüfen, dann anlegen –
        # sonst bliebe bei einer unzulässigen Zuordnung eine Bewegung ohne Zuordnung zurück.
        # Die Kontierung ergibt sich aus der Rechnung (assign_invoice_payment); ein Erlöskonto
        # aus dem Formular zählt hier nicht.
        if invoice_id and transaction_id <= 0:
            problem = _new_payment_problem(db, invoice_id, account_id, amount, revenue_coa_id)
            if problem:
                return 400, problem
            coa_id = None

        # Update or insert booking
        if transaction_id > 0:
            # Check if this is an unlinked bank booking being completed
            existing = db.get_booking_by_id(transaction_id)
            is_bank = existing and existing[17] == 'bank'

            if has_split_rows and existing:
                # Kontierung und Steuer der Bankzeile stammen bei aktivem
                # Editor nicht mehr aus dem Formular – bestehende Werte
                # erhalten, statt sie mit leeren Feldern zu überschreiben.
                coa_id = existing[8]
                tax_rate = existing[13]
                tax_amount = float(existing[14]) if existing[14] is not None else None

            # Update the bank booking itself (COA stays on bank row for
            # display; the real accounting entry is the child)
            # Nur schreiben, was die Maske führt. Gegenkonto und Kategorie haben dort
            # kein Feld – mit update_booking (setzt alle Spalten) gingen sie bei jedem
            # Speichern verloren, und die EÜR las den Satz danach falsch.
            fields = dict(
                date_booking=date, date_tax=date_tax, amount=float(amount),
                foreign_bank_account=foreign_account, recipient_client=recipient,
                contact_id=contact_id, coa_id=coa_id, currency=currency,
                tax_rate=tax_rate, tax_amount=tax_amount, text=text,
                document_number=document_nr or None,
            )
            # Die Maske wählt das Bankkonto eines Buchungssatzes nur zur Anzeige vor
            # (abgeleitet aus COA/Gegenkonto). Ein Kind einer Bankbewegung trägt selbst
            # kein Account_ID – das bleibt so, sonst zählte es in Auswertungen und im
            # Duplikatabgleich wie eine eigene Bankbewegung.
            if not (existing and existing[18]):          # 18 = ParentBooking_ID
                fields['account_id'] = account_id
            if existing and not is_bank:
                # Geldkonto vorne (COA = Bank/Kasse/Verrechnungskonto, Gegenkonto =
                # Zweckkonto, v. a. aus dem WISO-Import): Die Maske zeigt dann das Geldkonto
                # als „SKR-Konto“. Wählt der Nutzer dort ein Zweckkonto, meint er das
                # Gegenkonto – die Richtung des Satzes bleibt, wie im Editor der Bankmaske
                # (_plan_split_rows). „Kein SKR-Konto“ nimmt die Kontierung zurück: Das
                # Geldkonto bleibt, das Zweckkonto geht – sonst zählte es unsichtbar weiter.
                # Maßgeblich ist die gespeicherte Form des Satzes (Geldkonto vorne), nicht die
                # Kontenliste von heute; ob die Wahl des Nutzers ein Geldkonto ist, entscheiden
                # die Zahlungskonten (auch 1460), nicht nur die eigenen Bankkonten. Kommt das
                # Konto des Satzes unverändert zurück (verglichen mit dem gespeicherten Stand,
                # nicht mit der Kontenliste: es kann inzwischen kein Zahlungskonto mehr sein),
                # hat der Nutzer nichts gewählt – dann bleibt alles, wie es ist.
                liquid_ids = db.get_payment_coa_ids()
                old_coa, old_counter = existing[8], existing[9]
                old_form = read_entry_form(existing[22], old_coa, old_counter, liquid_ids)
                if (old_form == FORM_LIQUID_FIRST and old_coa is not None
                        and coa_id != old_coa and coa_id not in liquid_ids):
                    fields['coa_id'] = old_coa
                    fields['counter_coa_id'] = coa_id
                if (fields['coa_id'], fields.get('counter_coa_id', old_counter)) != (old_coa, old_counter):
                    _refuse_same_account(db, fields['coa_id'],
                                         fields.get('counter_coa_id', old_counter))

            # Legt die Maske für eine Bankzeile ohne Editor einen Satz an, darf er nicht dasselbe
            # Konto auf beiden Seiten haben – bevor irgendetwas geschrieben wird
            if is_bank and not has_split_rows and coa_id and not db.get_child_bookings_for_bank(transaction_id):
                _refuse_same_account(db, coa_id, _bank_counter_coa(
                    db, account_id or (existing[4] if existing else None)))

            # Buchungssätze aus dem Editor der Bank-Maske erst lesen und prüfen, dann
            # schreiben: Eine ungültige Zahl dort darf auch die Bankzeile nicht halb
            # gespeichert zurücklassen.
            planned_splits = None
            if is_bank and has_split_rows:
                eff_acct_id = account_id or (existing[4] if existing else None)
                planned_splits = _plan_split_rows(db, transaction_id, post_data,
                                                  _bank_counter_coa(db, eff_acct_id),
                                                  date, date_tax, recipient, currency)

            db.update_booking_fields(transaction_id, log_description="Manual booking update",
                                     **fields)

            if planned_splits is not None:
                # Die Sätze werden im Editor direkt gepflegt – der AutoMirror-Abgleich
                # unten entfällt.
                _write_split_rows(db, transaction_id, planned_splits)
            elif is_bank:
                # Gegenkonto (liquide Seite) aus dem SKR-Konto der Bank ableiten
                eff_acct_id = account_id or (existing[4] if existing else None)
                account_coa_id = _bank_counter_coa(db, eff_acct_id)

                children = db.get_child_bookings_for_bank(transaction_id)
                # Nur den von uBuchhaltung selbst angelegten Spiegel (AutoMirror)
                # mitziehen. Eigenständig erfasste, bloß verknüpfte Buchungen
                # (WISO-Einnahmen mit COA=Bank, Umbuchungen 4405→4400,
                # Debitoren-Zahlungen) folgen einer eigenen Kontierungslogik und
                # bleiben unangetastet – ihre Herkunft ließe sich aus den Werten
                # allein nicht zuverlässig erkennen.
                mirror_children = [c for c in children if c[3]]
                mirror_child_id = None
                if len(children) == 1 and len(mirror_children) == 1 and coa_id \
                        and coa_id != account_coa_id:
                    mirror_child_id = mirror_children[0][0]

                if mirror_child_id:
                    # Übersicht und EÜR lesen Konto, Steuersatz und Betrag aus
                    # dem Kind – ohne diesen Abgleich käme dort keine weitere
                    # Änderung an. Betrag und Steuer wandern gemeinsam mit,
                    # damit sie zueinander stimmig bleiben.
                    db.sync_entry_child(
                        child_id=mirror_child_id,
                        date_booking=date,
                        date_tax=date_tax,
                        amount=float(amount),
                        recipient_client=recipient,
                        coa_id=coa_id,
                        counter_coa_id=account_coa_id,
                        currency=currency,
                        tax_rate=tax_rate,
                        tax_amount=tax_amount,
                        text=text,
                        document_number=document_nr or None,
                        log_description="Bank booking update (sync entry child)"
                    )
                elif coa_id and not children:
                    # Auto-create entry child when completing a bank booking
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
                        auto_mirror=True,
                        log_description="Manual bank booking completion (entry child)"
                    )
                # Mehrere Kinder = Split: Aufteilung gehört dem Nutzer, die
                # Kinder werden einzeln über ihre eigenen Zeilen bearbeitet.
        else:
            # Insert new booking.
            # If a bank account is provided the row is a bank movement;
            # otherwise it is a plain accounting entry.
            new_booking_type = 'bank' if account_id else 'entry'
            planned_splits = None
            if new_booking_type == 'bank' and not has_split_rows and coa_id:
                _refuse_same_account(db, coa_id, _bank_counter_coa(db, account_id))
            if new_booking_type == 'bank' and has_split_rows:
                # erst prüfen, dann anlegen (siehe oben)
                planned_splits = _plan_split_rows(db, None, post_data,
                                                  _bank_counter_coa(db, account_id),
                                                  date, date_tax, recipient, currency)
            transaction_id = db.insert_booking(
                date_booking=date,
                date_tax=date_tax,
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

            if planned_splits is not None:
                # Buchungssätze direkt aus dem Editor der Bank-Maske
                _write_split_rows(db, transaction_id, planned_splits)
            # When a bank booking is created with a COA already set,
            # immediately create the linked accounting entry child so the
            # green checkmark appears without requiring a second edit.
            # (Bei einer Rechnungszahlung legt die Zuordnung die Sätze an: coa_id ist dort None.)
            elif new_booking_type == 'bank' and coa_id:
                # Look up the account's SKR-COA to set as the liquid counter side
                account_coa_id = _bank_counter_coa(db, account_id)
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
                    auto_mirror=True,
                    log_description="Manual bank booking – auto entry child"
                )

        # Vorgeschlagene Beleg-Nr. jetzt verbrauchen (todo #1): der Vorschlag
        # selbst zählt den Kreis nicht weiter, sonst verbrennte jeder Klick
        # eine Nummer. consume_number prüft, ob die Nummer noch die nächste
        # ist – hat der Nutzer sie überschrieben, passiert nichts.
        suggest_range_id = post_data.get('suggest_range_id', [''])[0].strip()
        if suggest_range_id and document_nr:
            try:
                db.consume_number(int(suggest_range_id), document_nr)
            except Exception as e:
                print(f"Error consuming number range {suggest_range_id}: {e}")

        # Zahlungs-Zuordnung zur Rechnung (todo #2): idempotent und gekappt;
        # ein Fehler hier darf die bereits gespeicherte Buchung nicht zum 500 machen. Die
        # Rechnungsseite nennt den Grund, wenn die Zuordnung nicht zustande kam.
        if invoice_id:
            try:
                ok, message = link_booking_to_invoice_capped(
                    db, invoice_id, transaction_id, revenue_coa_id=revenue_coa_id)
            except Exception as e:
                print(f"Error linking booking {transaction_id} to invoice {invoice_id}: {e}")
                ok, message = False, 'Die Zahlung konnte nicht zugeordnet werden.'
            if not ok:
                return 303, f"/invoice/edit?id={invoice_id}&payment_error={quote(message)}"
            return 303, f"/invoice/edit?id={invoice_id}"

        return 303, "/transactions"
    except _Refused as e:
        return 400, str(e)             # abgelehnt, nichts gespeichert
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
    current = db.get_account_by_id(account_id)
    old_skr = current[7] if current else None
    if skr_account != old_skr:
        # Die Form eines Buchungssatzes (Bookings.EntryForm) steht seit 2.1.0 fest, und EÜR
        # und DATEV-Export lesen sie. Zeilen ohne Form (Roh-SQL), die Zuordnung der Sätze zu
        # Kasse und Dashboard, die Split-Summe und der Abgleich richten sich aber weiter nach
        # den Konten, die gerade Geldkonten sind – das Vorzeichen stammt vom Zeitpunkt der
        # Buchung bzw. des Imports. Hängen schon Sätze am alten oder am neuen Konto, würde die
        # Umstellung sie umdeuten (Ausgabe ↔ Einnahme); die Sperre bleibt.
        affected = db.count_entries_on_account_numbers([old_skr, skr_account])
        if affected:
            msg = (f'SKR-Konto nicht geändert: {affected} Buchungssätze führen das bisherige '
                   f'oder das neue Konto. Die Umstellung würde sie anders lesen als gebucht '
                   f'(Einnahme statt Ausgabe). Das Konto lässt sich nur umstellen, solange '
                   f'keine Buchungen daran hängen – z. B. vor dem ersten Import.')
            return 303, f"/masterdata/bankaccounts/edit?id={account_id}&error={quote(msg)}"
    if skr_account == DEFAULT_CASH_ACCOUNT:
        # Ältere Datenbanken kennen das Kassenkonto 1600 noch nicht – ohne das Konto im
        # Kontenrahmen bliebe die Umstellung einer Kasse auf 1600 wirkungslos.
        db.ensure_cash_coa()
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

        # Die Daten landen im Dateinamen, in der CSV und in der Antwort: nur ISO-Daten
        date_from, date_to = parse_iso_date(date_from), parse_iso_date(date_to)
        if date_from is None or date_to is None:
            return 303, '/miscellaneous?datev_export=error&msg=' + quote('Ungültiger Datumsbereich')
        if date_from > date_to:
            return 303, '/miscellaneous?datev_export=error&msg=' + quote('Das Datum ‚von‘ liegt nach ‚bis‘')

        # Unstimmige Splits blockieren den Export: ein Stapel, in dem die
        # Buchungssätze nicht die Bankbewegung ergeben, ist in sich falsch und
        # beim Steuerbüro nur mit Rückfragen zu klären. Umbuchungen auf die
        # Wartekonten zählen nicht mit – der Rechnungsfall gilt als korrekt.
        unbalanced = db.find_unbalanced_splits(date_from, date_to)
        if unbalanced:
            parts = [f"{d} {fmt_eur(a, symbol=False)} EUR (Rest {fmt_eur(r, symbol=False)})"
                     for _bid, d, a, r, _t in unbalanced[:5]]
            if len(unbalanced) > 5:
                parts.append(f"und {len(unbalanced) - 5} weitere")
            msg = ('Export abgelehnt: Buchungssaetze passen nicht zur '
                   'Bankbewegung – ' + '; '.join(parts))
            return 303, f'/miscellaneous?datev_export=error&msg={quote(msg)}'

        bookings = db.fetch_bookings_range(date_from, date_to)
        if not bookings:
            return 303, f'/miscellaneous?datev_export=error&msg=Keine+Buchungen+im+Zeitraum+{date_from}+bis+{date_to}'

        coa_map = db.get_coa_id_to_number_map()
        csv_bytes, exported_ids = datev_module.export_to_datev(
            bookings, coa_map, date_from, date_to,
            liquid_coa_ids=db.get_payment_coa_ids()
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
    # (das Footer-JS ergänzt es beim Submit). Aktiv, sobald die Installation
    # eingerichtet ist.
    if userctx.auth_enabled():
        csrf_part = next((p for p in parts if p.name == 'csrf' and not p.is_file), None)
        csrf_token = csrf_part.content.decode('utf-8', 'replace').strip() if csrf_part else ''
        if not auth.check_csrf(request_handler._csrf_key(), csrf_token):
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


#: Dateinamen, die WISO Mein Büro im DB-Verzeichnis anlegt.
WISO_CLIENT_DB = 'DB1.FDB'
WISO_STANDARD_DB = 'DB0.FDB'


def _wiso_fdb_paths(entered):
    """Eingabe auf (Mandantendatei, Standardkontenrahmen) auflösen.

    Bequem gehalten: angegeben werden darf das DB-Verzeichnis von WISO oder
    die Mandantendatei selbst. ``DB0.FDB`` wird daneben gesucht, ist aber
    optional – ohne sie fehlen nur Konten, die der Mandant nie benutzt hat.
    """
    entered = (entered or '').strip().strip('"')
    if not entered:
        return None, None, 'Kein Pfad angegeben'
    if os.path.isdir(entered):
        client = os.path.join(entered, WISO_CLIENT_DB)
    else:
        client = entered
    if not os.path.isfile(client):
        return None, None, f'Nicht gefunden: {client}'
    standard = os.path.join(os.path.dirname(client), WISO_STANDARD_DB)
    return client, (standard if os.path.isfile(standard) else None), None


def handle_wiso_fdb_import(db: Database, post_data):
    """Import direkt aus der WISO-Datenbank (Firebird-Datei).

    Kein Upload: die Mandantendatei ist dreistellig megabytegroß und liegt
    ohnehin auf demselben Rechner. Angegeben wird deshalb ihr Pfad.

    Returns:
        (303, location_str) – immer ein Redirect zu /miscellaneous
    """
    from urllib.parse import quote

    def flag(name):
        return post_data.get(name, ['0'])[0] in ('1', 'on', 'true')

    entered = post_data.get('fdb_path', [''])[0]
    with_assets = flag('with_assets')
    with_invoices = flag('with_invoices')
    client, standard, problem = _wiso_fdb_paths(entered)
    if problem:
        return 303, f'/miscellaneous?wiso_fdb=error&msg={quote(problem)}'

    try:
        result = db.import_wiso_fdb(client, standard, with_assets=with_assets,
                                    with_invoices=with_invoices)
        result['file'] = client
        result['standard_chart'] = standard or ''
        if result['blocker']:
            _store_fdb_result(result)
            return 303, ('/miscellaneous?wiso_fdb=incompatible'
                         f'&msg={quote("; ".join(result["blocker"])[:300])}')

        link_result = db.link_bank_to_entries()
        result['linked'] = link_result.get('linked', 0)
        result['resolved'] = link_result.get('resolved', 0)
        if with_invoices:
            # Erst jetzt: die Zahlung soll an der Bankbewegung hängen, und die
            # steht erst nach dem Abgleich an den Buchungssätzen.
            payments = db.link_wiso_invoice_payments(client, standard)
            result['payments_linked'] = payments['payments_linked']
            result['payment_warnings'] = payments['payment_warnings']
            result['errors'] += payments['errors']
        _store_fdb_result(result)

        return 303, (
            f'/miscellaneous?wiso_fdb=ok'
            f'&file={quote(os.path.basename(client))}'
            f'&imported={result["imported"]}&skipped={result["skipped"]}'
            f'&adopted={result.get("adopted", 0)}'
            f'&possible={result.get("possible_duplicates_count", 0)}'
            f'&assets={result["assets"]}&afa={result["depreciations"]}'
            f'&customers={result["customers"]}&invoices={result["invoices"]}'
            f'&items={result["invoice_items"]}'
            f'&payments={result["payments_linked"]}'
            f'&created_coa={len(result["created_coa"])}'
            f'&missing_coa={len(result["missing_coa"])}'
            f'&hints={len(result["hints"])}'
            f'&linked={result["linked"]}&resolved={result["resolved"]}'
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 303, f'/miscellaneous?wiso_fdb=error&msg={quote(str(e))}'


def _store_fdb_result(result):
    """Detailergebnis für die Anzeige auf /miscellaneous ablegen."""
    path = os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json')
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass                                    # Anzeige ist nicht kritisch


# ── SQL-Konsole ──────────────────────────────────────────────────────────────
# PRAGMAs laufen in der Konsole nur per Positivliste, alles andere ist gesperrt:
#  * _CONSOLE_PRAGMAS_WITH_ARG: auch mit Argument/Wert erlaubt (Abfragen, Prüfläufe,
#    Verbindungseinstellungen),
#  * _CONSOLE_PRAGMAS_READ: nur OHNE Argument erlaubt, dann fragen sie bloß ab.
# Gesperrt sind damit user_version=…, journal_mode=…, writable_schema usw., die in
# Migration und Datensicherung eingreifen, aber auch Aufrufe ohne Argument, die die
# Datenbank verändern (optimize, incremental_vacuum, wal_checkpoint), und unbekannte
# Namen. Die Tabellenfunktionen pragma_<name> meldet SQLite mit derselben Aktion
# (SQLITE_PRAGMA, Name und Argument wie bei PRAGMA) – sie fallen unter dieselbe Regel.
_CONSOLE_PRAGMAS_WITH_ARG = frozenset({
    'table_info', 'table_xinfo', 'table_list', 'index_list', 'index_info',
    'index_xinfo', 'foreign_key_list', 'foreign_key_check', 'integrity_check',
    'quick_check', 'foreign_keys', 'defer_foreign_keys', 'case_sensitive_like',
})
_CONSOLE_PRAGMAS_READ = frozenset({
    'user_version', 'schema_version', 'application_id', 'data_version',
    'journal_mode', 'synchronous', 'locking_mode', 'page_size', 'page_count',
    'max_page_count', 'freelist_count', 'encoding', 'auto_vacuum', 'cache_size',
    'busy_timeout', 'secure_delete', 'temp_store', 'recursive_triggers',
    'database_list', 'collation_list', 'compile_options', 'function_list',
    'module_list', 'pragma_list',
})

# Grenzen je Anfrage: Eine endlose rekursive CTE hielte sonst die Sperre auf der
# Datenbank und einen Thread bis zum Neustart; zeroblob(1000000000) oder eine riesige
# Ausgabe könnten den Prozess per Speichermangel beenden. Als Modulkonstanten, damit
# Tests sie klein setzen können (handle_execute_sql liest sie bei jedem Aufruf).
_CONSOLE_TIME_LIMIT_S = 60              # alle Befehle einer Anfrage zusammen, inkl. Lesen der Ergebnisse
_CONSOLE_MAX_VALUE_BYTES = 2_000_000    # größter einzelner Wert (SQLITE_LIMIT_LENGTH, ab Python 3.11)
_CONSOLE_MAX_COLUMNS = 250              # Spalten je Ergebnis/Tabelle (SQLITE_LIMIT_COLUMN, ab Python 3.11)
_CONSOLE_MAX_OUTPUT_ROWS = 200_000      # Zeilen der Ausgabe je Anfrage (Kopfzeilen mitgezählt)
_CONSOLE_MAX_OUTPUT_BYTES = 20_000_000  # UTF-8-Bytes der Ausgabe je Anfrage


# SQL-Funktionen, die aus der Datenbank hinausführen. fts3_tokenizer gibt in SQLite-Builds
# mit ENABLE_FTS3_TOKENIZER (Debian/Ubuntu, also auch das Docker-Image) Speicheradressen
# preis und springt eine übergebene Adresse an – damit lässt sich der Server zum Absturz
# bringen und fremder Code ausführen. load_extension lädt beliebige Bibliotheken.
_CONSOLE_FUNCTIONS_DENIED = frozenset({'fts3_tokenizer', 'load_extension'})


def _console_authorizer(action, arg1, arg2, dbname, source):
    """Hält die SQL-Konsole in der eigenen Datenbank.

    ATTACH/DETACH würden andere Dateien öffnen (auth.db, Datenbanken anderer
    Nutzer); VACUUM (auch VACUUM INTO) läuft intern über ATTACH und ist damit
    ebenfalls gesperrt. PRAGMAs: siehe Positivlisten oben; gesperrte Funktionen:
    _CONSOLE_FUNCTIONS_DENIED. Diese Sperren nicht aufweichen.
    """
    import sqlite3
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or '').lower()
        if name in _CONSOLE_PRAGMAS_WITH_ARG or (arg2 is None and name in _CONSOLE_PRAGMAS_READ):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() in _CONSOLE_FUNCTIONS_DENIED:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _console_cell(value):
    """Ein Wert als Text für die Ausgabe. BLOBs erscheinen nur mit ihrer Größe – als Text
    wären sie ein Mehrfaches so groß und für niemanden lesbar."""
    if value is None:
        return ''
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f'<BLOB {len(value)} Bytes>'
    return str(value)


def _console_result_lines(cursor):
    """Kopfzeile und Zeilen des aktuellen Ergebnisses als `;`-getrennter Text.

    Zeile für Zeile gelesen, nie blockweise: So kann der Aufrufer an den Ausgabegrenzen
    aufhören, bevor weitere (womöglich sehr große) Zeilen im Speicher liegen.
    """
    if cursor.description:
        yield ';'.join(d[0] for d in cursor.description)
    for row in cursor:
        yield ';'.join(_console_cell(v) for v in row)


def handle_execute_sql(db: Database, post_data):
    """Handle SQL command execution – returns JSON with results.

    Je Anfrage gelten ein Zeitlimit (_CONSOLE_TIME_LIMIT_S) und Größengrenzen für Werte
    und Ausgabe (_CONSOLE_MAX_*); wird das Zeitlimit überschritten, bricht die Anfrage
    ab und wird zurückgerollt. Diese Grenzen nicht aufweichen.
    """
    import sqlite3, json
    
    sql_commands = post_data.get("sql_commands", [""])[0]
    
    if not sql_commands.strip():
        return json.dumps({"success_count": 0, "total": 0, "errors": ["Keine SQL-Befehle eingegeben."], "output": ""})
    
    # Split commands by semicolon and filter out empty ones
    commands = [cmd.strip() for cmd in sql_commands.split(';') if cmd.strip()]
    
    output_lines = []
    output_bytes = 0
    output_truncated = False
    errors = []
    success_count = 0
    timed_out_at = None

    conn = sqlite3.connect(db.db_name)
    conn.set_authorizer(_console_authorizer)
    if hasattr(conn, 'setlimit'):                   # ab Python 3.11
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _CONSOLE_MAX_VALUE_BYTES)
        # Python holt eine Ergebniszeile ganz, bevor die Ausgabegrenze prüfen kann: Erst
        # Wertgröße × Spaltenzahl begrenzt, was eine einzelne Zeile an Speicher belegt.
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, _CONSOLE_MAX_COLUMNS)
    deadline = time.monotonic() + _CONSOLE_TIME_LIMIT_S
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    cursor = conn.cursor()

    try:
        for i, command in enumerate(commands, 1):
            if time.monotonic() > deadline:
                timed_out_at = i
                break
            try:
                cursor.execute(command)
                success_count += 1
                # SELECT-Ergebnis ausgeben – bis zu den Ausgabegrenzen, danach nur noch ausführen
                if command.lstrip().upper().startswith('SELECT') and not output_truncated:
                    for line in _console_result_lines(cursor):
                        size = len(line.encode('utf-8')) + 1        # + Zeilenumbruch
                        if (len(output_lines) >= _CONSOLE_MAX_OUTPUT_ROWS
                                or output_bytes + size > _CONSOLE_MAX_OUTPUT_BYTES):
                            output_truncated = True
                            break
                        output_lines.append(line)
                        output_bytes += size
            except sqlite3.Error as e:
                msg = str(e)
                if (isinstance(e, sqlite3.OperationalError) and 'interrupted' in msg
                        and time.monotonic() > deadline):
                    # Vom Fortschrittshandler abgebrochen. Die Frist mitprüfen: Der Text
                    # allein träfe auch „no such table: interrupted“.
                    timed_out_at = i
                    break
                if 'not authorized' in msg or 'authorization denied' in msg:
                    msg = ('nicht erlaubt – ATTACH, DETACH, VACUUM sowie verändernde oder '
                           'unbekannte PRAGMAs sind in der Konsole gesperrt, ebenso die '
                           'Funktionen fts3_tokenizer und load_extension')
                errors.append(f"Befehl {i}: {msg}")

        if timed_out_at is not None:
            conn.rollback()
            return json.dumps({
                "success_count": 0,
                "total": len(commands),
                # Ehrlich bleiben: rollback() nimmt nur die offene Transaktion zurück. Die
                # beginnt Python erst vor dem ersten INSERT/UPDATE/DELETE – DDL davor und
                # alles vor einem ausdrücklichen COMMIT steht schon.
                "errors": [f"Befehl {timed_out_at}: abgebrochen – Zeitlimit von "
                           f"{_CONSOLE_TIME_LIMIT_S} s überschritten. Nicht festgeschriebene "
                           "Änderungen dieser Anfrage wurden zurückgenommen. Was schon "
                           "festgeschrieben war, bleibt bestehen: Befehle vor einem COMMIT und "
                           "Strukturbefehle (CREATE, ALTER, DROP) vor der ersten Datenänderung."],
                "output": ""
            }, ensure_ascii=False)

        conn.commit()
        if output_truncated:
            max_size = (f'{_CONSOLE_MAX_OUTPUT_BYTES // 1_000_000} MB'
                        if _CONSOLE_MAX_OUTPUT_BYTES % 1_000_000 == 0
                        else f'{_CONSOLE_MAX_OUTPUT_BYTES} Bytes')
            output_lines.append(
                f'… Ausgabe gekürzt (mehr als {_CONSOLE_MAX_OUTPUT_ROWS} Zeilen bzw. {max_size}) '
                f'– bitte mit WHERE oder LIMIT eingrenzen')

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

ARTICLE_NUMBER_ERROR = 'Preis und Steuersatz müssen endliche Zahlen sein.'


def _finite_number(text, default):
    """Zahl aus einem Formularfeld (leer: ``default``) – ``None``, wenn sie nicht lesbar oder nicht
    endlich ist (``nan``, ``inf``) oder zu groß für einen Betrag: Das ließe sich nicht speichern."""
    text = (text or '').strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) and abs(value) < 1e12 else None


def _article_error_location(message):
    return f"/masterdata/articles?error={quote(message)}"


def handle_add_article(db: Database, post_data):
    """Handle adding a new article"""
    name = post_data.get('name', [''])[0]
    unit = post_data.get('unit', ['Stk.'])[0]
    unit_price = _finite_number(post_data.get('unit_price', ['0'])[0], 0.0)
    tax_rate = _finite_number(post_data.get('tax_rate', ['19'])[0], 19.0)
    if unit_price is None or tax_rate is None:
        return 303, _article_error_location(ARTICLE_NUMBER_ERROR)
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
    unit_price = _finite_number(post_data.get('unit_price', ['0'])[0], 0.0)
    tax_rate = _finite_number(post_data.get('tax_rate', ['19'])[0], 19.0)
    if unit_price is None or tax_rate is None:
        return 303, _article_error_location(ARTICLE_NUMBER_ERROR)
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


class InvalidRequest(ValueError):
    """Eine JSON-Anfrage lässt sich nicht lesen: kein gültiges JSON-Objekt, fehlende oder
    unlesbare Pflichtfelder. ``server/app.py`` antwortet darauf mit 400 „Ungültige Anfrage.“."""


def _json_request(post_body):
    """Body einer JSON-Route als dict – sonst :class:`InvalidRequest`."""
    try:
        data = json.loads(post_body.decode('utf-8'))
    except (ValueError, RecursionError):        # kaputtes JSON, kaputtes UTF-8, zu tief verschachtelt
        raise InvalidRequest('kein gültiges JSON') from None
    if not isinstance(data, dict):
        raise InvalidRequest('kein JSON-Objekt')
    return data


_REQUIRED = object()


def _json_id(data, key, missing=_REQUIRED):
    """Ganzzahl (ID) aus einem JSON-Objekt. Fehlt das Feld, gilt ``missing`` – ohne Angabe ist es
    eine :class:`InvalidRequest`, ebenso ein Wert, der keine Ganzzahl ist."""
    value = data.get(key)
    if value is None or value == '':
        if missing is _REQUIRED:
            raise InvalidRequest(f'{key} fehlt')
        return missing
    if isinstance(value, bool):
        raise InvalidRequest(f'{key} ist keine Zahl')
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise InvalidRequest(f'{key} ist keine Zahl') from None


def handle_update_invoice_status(post_body: bytes):
    """Update invoice status and increment number range if finalizing

    Returns tuple: (status_code, json_body)
    """
    data = _json_request(post_body)
    invoice_id = _json_id(data, 'invoice_id')
    new_status = data.get('status')
    if not isinstance(new_status, str):
        raise InvalidRequest('status fehlt')

    if not invoice_id or not new_status:
        return 400, '{"success": false, "error": "Fehlende Parameter"}'
    
    db = Database()
    
    # Validate status
    valid_statuses = ['draft', 'finalized', 'sent', 'partial_payment', 'overdue', 'paid', 'cancelled']
    if new_status not in valid_statuses:
        return 400, '{"success": false, "error": "Ung\u00fcltiger Status"}'
    
    # Nur Rechnungen: ein Angebot wird hier nie versendet (und bekommt keine Rechnungs-PDF)
    invoice = db.get_document(invoice_id, 'invoice')
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
                            year=r[2],
                            letter=r[3],
                            prefix=r[4] or '',
                            current_number=new_number,
                            description=r[6] or '',
                            number_format=r[7] if len(r) > 7 else None
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
    # AmountDue an die tatsächlichen Zahlungen angleichen – heilt verwaiste
    # Zustände (z. B. "bezahlt" mit Rest 0, aber ohne Zahlungen; todo #2).
    # Status bleibt die manuelle Wahl (adjust_status=False).
    db.recalc_invoice_payment_state(invoice_id, adjust_status=False)
    _sync_receivable_booking(db, invoice_id, new_status)
    print(f"Invoice {invoice_id} status updated to: {new_status}")

    result = {'success': True, 'invoice_id': invoice_id}
    warning = _ensure_sent_documents(db, invoice_id, current_status, new_status)
    if warning:
        result['warning'] = warning
    return 200, json.dumps(result)


def _ensure_sent_documents(db, invoice_id, current_status, new_status):
    """Beim Wechsel auf „versendet“ das PDF (+ XML) erzeugen (todo #2).

    Ab „versendet“ darf die Rechnung nicht mehr geändert werden – ab hier muss
    also das Dokument vorliegen, das der Kunde bekommt. Beim Eintritt in den
    Status wird es neu geschrieben; ist die Rechnung bereits versendet, bleibt
    die vorhandene Datei unangetastet und wird nur nachgezogen, wenn sie fehlt.

    Returns:
        str | None: Warntext, falls das Dokument nicht erzeugt werden konnte.
    """
    if new_status != 'sent':
        return None

    from export.invoice_files import generate_invoice_documents, stored_invoice_document

    if current_status == 'sent':
        existing, _, _ = stored_invoice_document(db, invoice_id, 'pdf')
        if existing:
            return None
    try:
        result = generate_invoice_documents(db, invoice_id, overwrite_sent=True)
    except Exception as e:
        import traceback
        print(f"Error generating documents for invoice {invoice_id}: {e}")
        traceback.print_exc()
        return 'PDF konnte nicht automatisch erstellt werden.'
    if result.get('error'):
        return f"Status gesetzt, aber: {result['error']}"
    return None


def _sync_receivable_booking(db, invoice_id, status):
    """Forderungsbuchung (Debitoren an Wartekonto) am Status ausrichten: ab „versendet“ gibt es
    sie, davor und nach dem Storno nicht (``RECEIVABLE_STATUSES``). Nimmt eine Zuordnung die
    letzte Zahlung einer Rechnung weg, richtet ``Database._after_payment_removed`` die
    Forderung nach derselben Regel aus.

    Ein Fehler hier darf den Statuswechsel nicht kippen – die Buchung lässt
    sich jederzeit nachziehen, ein halb gesetzter Status nicht.
    """
    try:
        if status in RECEIVABLE_STATUSES:
            db.ensure_receivable_booking(invoice_id)
        else:
            db.remove_receivable_booking(invoice_id)
    except Exception as e:
        print(f"Error syncing receivable booking for invoice {invoice_id}: {e}")


def _backfill_booking_skr(db, booking_id, invoice, revenue_coa_id):
    """Anzeige der Bankbewegung nach einer Zuordnung ergänzen (todo #2).

    Nur leere Felder werden gefüllt, nichts wird überschrieben: Erlöskonto (``revenue_coa_id``:
    das Konto, auf das die Zuordnung tatsächlich gebucht hat), Steuersatz/-betrag (enthaltene USt
    des Buchungsbetrags), Kunde und Beleg-Nr. Das ist reine Anzeige und speist nichts: Das
    Erlöskonto je Steuersatz lernt ``resolve_revenue_coa`` allein aus den Buchungssätzen der
    Zuordnung. Die Buchungssätze der Zahlung legt ``Database.assign_invoice_payment`` an, nicht
    diese Funktion.
    """
    booking = db.get_booking_by_id(booking_id)
    if not booking:
        return
    rate = invoice[35]
    rate_pct = 0 if not rate or rate < 0 else (rate * 100 if rate <= 1 else rate)
    tax_rate = rate_pct / 100 if rate_pct else None
    tax_amount = None
    if rate_pct:
        # Enthaltene USt des gesamten Buchungsbetrags (nicht nur der
        # Zuordnung): das Steuerfeld beschreibt die Buchung als Ganzes.
        tax_amount = from_minor(round_minor(
            tax_from_gross(abs(to_minor(booking[11] or 0)), round(rate_pct, 4)), 2))
    db.backfill_booking_fields(booking_id, coa_id=revenue_coa_id,
                               tax_rate=tax_rate, tax_amount=tax_amount,
                               contact_id=invoice[13], document_number=invoice[1])


def link_booking_to_invoice_capped(db, invoice_id, booking_id, amount=None, revenue_coa_id=None):
    """Buchung einer Rechnung als Zahlung zuordnen (idempotent, gekappt).

    Zuordnungsbetrag = min(gewünschter Betrag, offener Rechnungsrest, freier
    Rest der Buchung) – Rechnungen werden nie überbucht, Überschüsse bleiben
    als freier Buchungsrest dem Kunden zuordenbar (todo #2). amount=None
    bedeutet "so viel wie möglich". Zuordnung und Buchungssätze legt
    ``Database.assign_invoice_payment`` in einer Transaktion an. ``revenue_coa_id``: das
    Erlöskonto, das der Nutzer wählt, wenn für den Steuersatz keines hinterlegt ist (sonst
    ohne Wirkung).

    Returns (True, None) bei Erfolg, sonst (False, Fehlermeldung).
    """
    assignment = db.assign_invoice_payment(invoice_id, booking_id, amount, revenue_coa_id)
    if not assignment.ok:
        return False, assignment.message
    try:
        _backfill_booking_skr(db, booking_id, db.get_invoice_by_id(invoice_id),
                              assignment.revenue_coa_id)
    except Exception as e:
        # Zuordnung ist gespeichert; ein Backfill-Fehler darf sie nicht kippen
        print(f"Error backfilling SKR for booking {booking_id}: {e}")
    return True, None


def handle_link_invoice_payment(post_body: bytes):
    """Link invoice to a payment booking via InvoicePayments table.

    Returns tuple: (status_code, message_or_path)
    """
    data = _json_request(post_body)
    invoice_id = _json_id(data, 'invoice_id', missing=0)
    transaction_id = _json_id(data, 'transaction_id', missing=0)
    amount_paid = data.get('amount_paid')
    if amount_paid in (None, ''):
        amount_paid = None
    revenue_coa_id = _json_id(data, 'revenue_coa_id', missing=None)   # nur bei Wahl des Nutzers

    if not invoice_id or not transaction_id:
        return 400, "Ungültige Parameter"

    db = Database()

    try:
        ok, err = link_booking_to_invoice_capped(db, invoice_id, transaction_id,
                                                 amount_paid, revenue_coa_id)
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
    data = _json_request(post_body)
    payment_id = _json_id(data, 'payment_id')

    if not payment_id:
        return 400, "Missing payment_id"

    db = Database()

    try:
        db.unassign_invoice_payment(payment_id)
        return 200, "ok"
    except Exception as e:
        print(f"Error deleting invoice payment: {e}")
        return 500, "Die Zahlungszuordnung konnte nicht entfernt werden."


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


def _invoice_is_frozen(invoice_id):
    """True, wenn die gespeicherte Rechnung schon versendet (eingefroren) ist."""
    if not invoice_id:
        return False
    row = Database().get_invoice_by_id(invoice_id)
    return bool(row) and (row[40] == 'partial' or row[40] in RECEIVABLE_STATUSES)


def handle_invoice_save(post_body: bytes):
    """Save or update invoice to database

    Returns JSON response with invoice_id
    """
    data = _json_request(post_body)         # kein gültiges Objekt: InvalidRequest → 400 (app.py)
    try:
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

        # Leistungsdatum ist Pflicht (§ 14 UStG). Bereits versendete Rechnungen
        # sind eingefroren und werden nicht nachträglich geprüft.
        if not delivery_date and not _invoice_is_frozen(invoice_id if is_update else None):
            return json.dumps({'success': False, 'error':
                'Leistungsdatum fehlt – Pflichtangabe nach § 14 UStG '
                '(Datum der Lieferung/Leistung).'}).encode()

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
        
        # Kopf und Positionen in EINER Transaktion: scheitert ein Schritt, bleibt
        # nichts halb gespeichert zurück – kein Kopf ohne Positionen (der nächste
        # Versuch scheiterte sonst an der schon vergebenen Rechnungs-Nr.) und
        # keine gelöschten Positionen hinter einem gescheiterten Update.
        try:
            with db.transaction() as tx:
                if is_update:
                    _save_invoice_update(tx, invoice_id, invoice_data)
                else:
                    invoice_id = db.insert_invoice(invoice_data, tx=tx)

                # Positionen (Positionssumme serverseitig berechnet)
                for idx, item in enumerate(items):
                    db.insert_invoice_item({
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
                    }, tx=tx)
        except DocumentNotFound as e:
            return json.dumps({'success': False, 'error': str(e)}).encode()
        except Exception as e:
            import traceback
            traceback.print_exc()
            return json.dumps({'success': False, 'error': _save_error_text(e)}).encode()

        # Die Rechnung ist ab hier gespeichert (Commit oben). Scheitert das Fortschreiben des
        # Nummernkreises, bleibt die Antwort ein Erfolg und nennt nur den Hinweis – sonst legt
        # der Nutzer dieselbe Rechnung ein zweites Mal an.
        result = {'success': True, 'invoice_id': invoice_id}
        if status == 'finalized' and not is_update:
            try:
                _advance_invoice_number_range(db, invoice_number)
            except Exception as e:
                print(f"WARNUNG: Nummernkreis nicht fortgeschrieben ({invoice_number!r}): {e!r}")
                result['warning'] = ('Die Rechnung ist gespeichert, aber der Nummernkreis konnte '
                                     'nicht fortgeschrieben werden – bitte unter Stammdaten '
                                     '→ Nummernkreise prüfen.')

        return json.dumps(result).encode()

    except Exception as e:
        import traceback
        traceback.print_exc()
        return json.dumps({'success': False, 'error': _save_error_text(e)}).encode()


class DocumentNotFound(LookupError):
    """Das Aktualisieren trifft keinen Beleg des erwarteten Typs (Rechnung bzw. Angebot)."""


_NOT_NULL_FIELDS = {
    'InvoiceItems.Position': 'die Positionsnummer',
    'InvoiceItems.Description': 'die Bezeichnung',
    'InvoiceItems.Quantity': 'die Menge',
    'InvoiceItems.PricePerUnit': 'der Einzelpreis',
    'InvoiceItems.TotalNet': 'die Positionssumme',
    'InvoiceItems.TaxRate': 'der Steuersatz',
}


def _save_error_text(exc):
    """Fehlermeldung zum Speichern einer Rechnung oder eines Angebots im Klartext – ohne
    SQL-Details und Python-Meldungen (die stehen im Server-Log, der Aufrufer druckt sie)."""
    if isinstance(exc, DocumentNotFound):
        return str(exc)
    if isinstance(exc, ForeignKeyError):
        return f'{exc} – es wurde nichts gespeichert.'
    if isinstance(exc, sqlite3.IntegrityError):
        detail = str(exc)
        if detail.startswith('UNIQUE') and 'InvoiceNumber' in detail:
            return 'Diese Nummer ist bereits vergeben.'
        if detail.startswith('NOT NULL') and detail.rsplit(': ', 1)[-1] in _NOT_NULL_FIELDS:
            reason = f'In einer Position fehlt {_NOT_NULL_FIELDS[detail.rsplit(": ", 1)[-1]]}'
        else:
            reason = 'Eine Pflichtangabe fehlt oder ist ungültig'
    elif isinstance(exc, sqlite3.Error):
        reason = 'Die Datenbank hat den Vorgang abgelehnt'
    elif isinstance(exc, (ArithmeticError, ValueError, TypeError, AttributeError, KeyError)):
        reason = 'Beträge, Mengen oder Positionen sind ungültig'
    else:
        reason = 'Unerwarteter Fehler (Einzelheiten im Server-Log)'
    return f'Speichern fehlgeschlagen: {reason} – es wurde nichts gespeichert.'


def _advance_invoice_number_range(db, invoice_number):
    """Den Nummernkreis einer neu abgeschlossenen Rechnung auf deren Nummer setzen.

    Aus ``26R001`` werden Jahr 2026 und Buchstabe ``R``; passt ein Nummernkreis, steht sein
    Zähler danach auf der Nummer der Rechnung. Nummern, die diesem Schema nicht folgen, lösen
    eine Ausnahme aus – der Aufrufer meldet das als Hinweis.
    """
    if len(invoice_number) <= 2:
        return
    year_short = invoice_number[:2]
    year = 2000 + int(year_short) if int(year_short) <= 99 else int(year_short)
    letter = invoice_number[2]
    for r in db.fetch_number_ranges('invoice'):
        if r[2] == year and r[3] == letter:
            num_part = invoice_number[3:].lstrip('_ABCDEFGHIJKLMNOPQRSTUVWXYZ')
            db.update_number_range(
                r[0], r[2], r[3], r[4] or '', int(num_part),
                r[6] or '', r[7] if len(r) > 7 else None)
            break


def _save_invoice_update(tx, invoice_id, invoice_data):
    """Kopf einer bestehenden Rechnung überschreiben und ihre Positionen
    entfernen – die neuen schreibt der Aufrufer in derselben Transaktion.

    Trifft die ID keine Rechnung (es gibt sie nicht oder es ist ein Angebot), wird
    :class:`DocumentNotFound` ausgelöst und die Transaktion zurückgerollt."""
    cursor = tx.conn.cursor()
    cursor.execute('DELETE FROM InvoiceItems WHERE InvoiceId = ?', (invoice_id,))
    refs = {'OwnCompanyId': invoice_data['own_company_id'],
            'CustomerId': invoice_data['customer_id'],
            'BankAccountId': invoice_data['bank_account_id']}
    with fk_explained(tx.conn, 'Invoices', refs):
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
                WHERE ID=? AND COALESCE(DocumentType, 'invoice') = 'invoice'
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
    if cursor.rowcount == 0:
        raise DocumentNotFound('Rechnung nicht gefunden.')


def handle_send_invoice_email(post_body: bytes):
    """Send invoice via email
    
    Returns JSON response with success status
    """
    data = _json_request(post_body)
    invoice_id = _json_id(data, 'invoice_id')
    recipient_email = data.get('recipient_email')
    recipient_name = data.get('recipient_name', '')
    message_text = data.get('message_text')
    
    if not invoice_id or not recipient_email:
        return json.dumps({'success': False, 'error': 'Fehlende Parameter'}).encode()
    
    db = Database()
    invoice = db.get_document(invoice_id, 'invoice')   # nur Rechnungen, kein Angebot
    
    if not invoice:
        return json.dumps({'success': False, 'error': 'Rechnung nicht gefunden'}).encode()
    
    # Anhang ist die abgelegte PDF der Rechnung; ihr Pfad gilt nur im Dokumentordner des
    # Nutzers (export.invoice_files.stored_invoice_path). Ohne Datei geht nichts raus.
    from export.invoice_files import stored_invoice_path
    pdf_path, _ = stored_invoice_path(db, invoice_id, 'pdf')
    if not pdf_path:
        return json.dumps({'success': False,
                           'error': 'Kein PDF vorhanden – bitte zuerst erzeugen.'}).encode()

    invoice_number = invoice[InvoiceCol.NUMBER]
    
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
    """AfA für ein Jahr buchen: Umbuchung „Aufwand an Anlagekonto“ (kein Bankkonto).

    ``coa_id`` ist das Aufwandskonto, ``asset_coa_id`` (optional) das Anlagekonto – ohne
    Angabe gilt das der Anlage bzw. ihrer Kategorie. Lehnt die Datenbank ab (Jahr schon
    gebucht, kein Anlagekonto), geht die Meldung als ``err`` an die Anlagenseite zurück."""
    asset_id    = int(post_data.get('asset_id', ['0'])[0])
    year        = int(post_data.get('year', ['0'])[0])
    coa_id      = int(post_data.get('coa_id', ['0'])[0])
    asset_coa   = post_data.get('asset_coa_id', [''])[0].strip()
    asset_coa_id = int(asset_coa) if asset_coa else None
    description = post_data.get('description', [''])[0].strip() or None

    try:
        db.book_depreciation(
            asset_id=asset_id,
            year=year,
            coa_id_expense=coa_id,
            coa_id_asset=asset_coa_id,
            description=description,
        )
    except ValueError as exc:
        return 303, f'/assets/view?id={asset_id}&err={quote(str(exc), safe="")}'
    return 303, f'/assets/view?id={asset_id}'


def handle_asset_sale(db: Database, post_data: dict):
    """Anlage verkaufen oder abgehen"""
    asset_id   = int(post_data.get('asset_id', ['0'])[0])
    sale_date  = post_data.get('sale_date', [''])[0].strip()
    sale_price = float(post_data.get('sale_price', ['0'])[0] or 0)
    if parse_iso_date(sale_date) is None:
        # Das Datum steuert, bis wann AfA gebucht werden darf – nur ein ISO-Datum taugt dafür
        return 303, f'/assets/view?id={asset_id}&err=' + quote('Abgangsdatum fehlt oder ist ungültig (JJJJ-MM-TT).')

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

def handle_invoice_document_download(invoice_id: int, kind: str = 'pdf', doc_type: str = 'invoice'):
    """Abgelegtes Rechnungsdokument (PDF oder XRechnung) zum Download laden.

    Es wird ausschließlich die *gespeicherte* Datei ausgeliefert – nie neu
    erzeugt (todo #3): ab „versendet“ muss der Kunde genau das Dokument
    bekommen, das im Haus liegt. Gilt auch für Angebote (``kind='pdf'``).

    Args:
        invoice_id: ID der Rechnung bzw. des Angebots.
        kind: ``'pdf'`` oder ``'xml'``.
        doc_type: Belegtyp der Route (``'invoice'`` oder ``'quote'``); die ID eines Belegs
            der anderen Art liefert nichts aus.

    Returns:
        tuple: ``(bytes, dateiname, fehlertext)``; bei Erfolg ist der
        Fehlertext ``None``.
    """
    from export.invoice_files import stored_invoice_document

    db = Database()
    try:
        if db.get_document(invoice_id, doc_type) is None:
            return None, None, ('Angebot nicht gefunden.' if doc_type == 'quote'
                                else 'Rechnung nicht gefunden.')
        return stored_invoice_document(db, invoice_id, kind)
    except Exception as e:
        import traceback
        print(f"Error reading {kind} for invoice {invoice_id}: {e}")
        traceback.print_exc()
        return None, None, 'Dokument konnte nicht gelesen werden.'


def handle_invoice_pdf_generate(invoice_id: int):
    """PDF (und bei E-Rechnungsdaten das XML) einer Rechnung neu erzeugen.

    Returns:
        dict: Ergebnis von :func:`export.invoice_files.generate_invoice_documents`.
    """
    from export.invoice_files import generate_invoice_documents

    db = Database()
    try:
        return generate_invoice_documents(db, invoice_id)
    except Exception as e:
        import traceback
        print(f"Error generating documents for invoice {invoice_id}: {e}")
        traceback.print_exc()
        return {'pdf_path': None, 'pdf_filename': None, 'xml_path': None,
                'xml_filename': None, 'error': 'Fehler beim Erstellen der PDF'}


# ── Angebote (Quotes) ─────────────────────────────────────────────────────────

_QUOTE_STATUSES = {'draft', 'sent', 'accepted', 'rejected', 'converted'}


def handle_quote_save(post_body: bytes):
    """Angebot speichern/aktualisieren. Returns JSON (success, quote_id).

    Kopf und Positionen werden in EINER Transaktion geschrieben: scheitert ein Schritt (auch
    eine unvollständige Position), bleibt nichts halb gespeichert zurück. Eine Aktualisierung
    trifft nur ein Angebot – die ID einer Rechnung wird abgewiesen und nichts verändert."""
    data = _json_request(post_body)         # kein gültiges Objekt: InvalidRequest → 400 (app.py)
    try:
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

        try:
            with db.transaction() as tx:
                if is_update:
                    _save_quote_update(tx, quote_id, doc)
                else:
                    quote_id = db.insert_invoice(doc, tx=tx)

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
                    }, tx=tx)
        except DocumentNotFound as e:
            return json.dumps({'success': False, 'error': str(e)}).encode()

        return json.dumps({'success': True, 'quote_id': quote_id}).encode()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return json.dumps({'success': False, 'error': _save_error_text(e)}).encode()


def _save_quote_update(tx, quote_id, doc):
    """Kopf eines bestehenden Angebots überschreiben und seine Positionen entfernen – die neuen
    schreibt der Aufrufer in derselben Transaktion. Trifft die ID kein Angebot (es gibt sie nicht
    oder es ist eine Rechnung), wird :class:`DocumentNotFound` ausgelöst und zurückgerollt."""
    cursor = tx.conn.cursor()
    cursor.execute('DELETE FROM InvoiceItems WHERE InvoiceId = ?', (quote_id,))
    refs = {'OwnCompanyId': doc['own_company_id'], 'CustomerId': doc['customer_id']}
    with fk_explained(tx.conn, 'Invoices', refs):
        cursor.execute('''
            UPDATE Invoices SET
                InvoiceNumber=?, InvoiceDate=?, OwnCompanyId=?,
                SellerName=?, SellerCompany=?, SellerStreet=?, SellerPostalCode=?, SellerCity=?, SellerCountry=?,
                SellerVATID=?, SellerEmail=?, SellerPhone=?,
                CustomerId=?, BuyerName=?, BuyerCompany=?, BuyerStreet=?, BuyerPostalCode=?, BuyerCity=?, BuyerCountry=?, BuyerVATID=?,
                Currency=?, TaxCategory=?, TaxRate=?, SumNet=?, TaxAmount=?, SumGross=?, AmountDue=?,
                Status=?, ValidUntil=?, IntroText=?, ClosingText=?
            WHERE ID=? AND DocumentType='quote'
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
    if cursor.rowcount == 0:
        raise DocumentNotFound('Angebot nicht gefunden.')


def handle_update_quote_status(post_body: bytes):
    """Angebots-Status setzen. Returns (status_code, json_body). Nur für Angebote: die ID
    einer Rechnung ändert nichts (404)."""
    data = _json_request(post_body)
    quote_id = _json_id(data, 'quote_id')
    new_status = data.get('status')
    if not isinstance(new_status, str):
        raise InvalidRequest('status fehlt')
    if not quote_id or new_status not in _QUOTE_STATUSES:
        return 400, '{"success": false, "error": "Ungültiger Status"}'
    db = Database()
    quote = db.get_document(quote_id, 'quote')
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


def handle_quote_pdf_generate(quote_id: int):
    """PDF für ein bestehendes Angebot erzeugen und ablegen (nur per POST aufgerufen).

    Returns:
        tuple: ``(pdf_bytes, pdf_path)`` oder ``(None, None)``. Heruntergeladen wird
        anschließend die abgelegte Datei (``handle_invoice_document_download``).
    """
    from export.pdf_quote import generate_quote_pdf
    db = Database()
    try:
        return generate_quote_pdf(db, quote_id)
    except Exception as e:
        import traceback
        print(f"Error generating quote PDF {quote_id}: {e}")
        traceback.print_exc()
        return None, None


# ── Arbeitszeiten (WorkTimes) ─────────────────────────────────────────────────

def _period_args(date_from, date_to):
    """``&from=…&to=…`` für eine Adresse, prozentkodiert. Der Zeitraum kommt aus dem Formular
    und muss ein gültiges ISO-Datumspaar sein; sonst fehlt er in der Adresse und die Zielseite
    nimmt Cookie oder Standard (statt dass beliebiger Text in der ``Location`` landet)."""
    period = valid_period(date_from, date_to)
    if period is None:
        return ''
    return f"&from={quote(period[0], safe='')}&to={quote(period[1], safe='')}"


def _wt_redirect(person_id, date_from, date_to):
    """Redirect-Ziel zurück zur Arbeitszeiten-Seite (Person + Zeitraum erhalten)."""
    return f"/worktime?person={quote(str(person_id), safe='')}{_period_args(date_from, date_to)}"


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
    return 303, (f"/worktime?person={quote(str(person_id), safe='')}"
                 f"{_period_args(date_from, date_to)}&error={msg}")


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
    """PDF-Stundenzettel für den Download erzeugen. Returns (pdf_bytes, filename) oder (None, None).

    Der Download ist ein GET: Er liefert den Stundenzettel aus, legt aber nichts auf der
    Platte ab (``store=False``)."""
    from export.pdf_worktime import generate_worktime_pdf
    try:
        pdf_bytes, pdf_path = generate_worktime_pdf(db, int(person_id), date_from, date_to,
                                                    with_notes=with_notes, store=False)
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
    return f"/trips?person={quote(str(person_id), safe='')}{_period_args(date_from, date_to)}"


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
def handle_setup_start(post_data):
    """Ersteinrichtung: ersten Administrator anlegen – mit Passwort oder mit
    automatischer Anmeldung. Returns (ok, username, with_login, error_msg);
    ``None`` als erstes Element heißt: Einrichtung ist bereits erfolgt (403).

    Gesperrt ist die Ersteinrichtung, sobald es einen Administrator gibt. Eine vorab
    angelegte config.json mit ``{"mode":"multi"}`` (Headless) oder ein Datenverzeichnis
    nur mit Benutzer ``demo`` lässt sie offen. Die Installation wird hier als
    eingerichtet markiert (``mode: multi``); danach ändert das nichts mehr im Browser
    (sonst könnte ein Nutzer den Login abschalten).
    """
    import secrets
    import demo
    mode = userctx.get_mode()
    if auth.admin_exists():
        # Auch wenn config.json fehlt oder unlesbar ist: Gibt es schon einen
        # Administrator, legt die Ersteinrichtung keinen weiteren an.
        return None, '', False, None
    if demo.active_user():
        # Im laufenden Demo-Modus ist jeder Besucher angemeldet – er darf sich nicht
        # zum ersten Administrator der Installation machen.
        return None, '', False, None
    g = lambda k: post_data.get(k, [''])[0]
    username = g('username').strip()
    with_login = g('login') == 'password'
    if with_login:
        password, password2 = g('password'), g('password2')
        if password != password2:
            return False, username, with_login, 'Die Passwörter stimmen nicht überein.'
    else:
        if g('password') or g('password2'):
            # Die Eingabetaste löst im Browser den ersten Knopf aus („Ohne Anmeldung“) –
            # ein eingetipptes Passwort darf dabei nicht stillschweigend verfallen.
            return False, username, with_login, ('Es ist ein Passwort eingetragen – bitte '
                                                 '„Mit Passwort starten“ wählen oder die Passwortfelder leeren.')
        # Niemand kennt dieses Passwort; es wird beim Ausschalten der automatischen
        # Anmeldung durch ein selbst gewähltes ersetzt.
        password = secrets.token_urlsafe(24)
    try:
        auth.create_user(username, password, is_admin=True)
    except ValueError as e:
        return False, username, with_login, str(e)
    if mode != 'multi':
        userctx.set_mode('multi')
    # Ausdrücklich in beide Richtungen: Eine vorab angelegte config.json kann schon
    # ein auto_login tragen – wer ein Passwort wählt, meldet sich damit an.
    userctx.set_auto_login(None if with_login else username)
    return True, username, with_login, None


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
    if username == userctx.get_auto_login():
        return _users_redirect(err='Dieser Benutzer wird automatisch angemeldet – erst die automatische Anmeldung ausschalten.')
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
    if username == userctx.get_auto_login():
        return _users_redirect(err='Dieser Benutzer wird automatisch angemeldet – erst die automatische Anmeldung ausschalten.')
    import demo
    if username == demo.USERNAME:
        # Im Demo-Modus arbeitet jeder Besucher als dieser Benutzer.
        return _users_redirect(err='Der Demo-Benutzer kann kein Administrator werden.')
    currently_admin = auth.is_admin(username)
    if currently_admin and auth.count_admins() <= 1:
        return _users_redirect(err='Der letzte Administrator kann nicht herabgestuft werden.')
    auth.set_admin(username, not currently_admin)
    state = 'Administrator' if not currently_admin else 'normaler Benutzer'
    return _users_redirect(info=f'„{username}" ist jetzt {state}.')


def handle_user_auto_login(post_data, current_user):
    """Automatische Anmeldung ein-/ausschalten. Returns (Redirect-Location, login_as).

    Einschalten kann ein Administrator nur für sich selbst. Beim Ausschalten wird
    ein Passwort gesetzt (niemand sperrt sich aus); ``login_as`` nennt dann den
    Benutzer, für den der Aufrufer eine Sitzung anlegt.
    """
    g = lambda k: post_data.get(k, [''])[0]
    action = g('action')
    active = auth.auto_login_user()
    if action == 'enable':
        if active:
            return _users_redirect(err='Die automatische Anmeldung ist bereits aktiv.'), None
        if not (current_user and auth.is_admin(current_user)):
            return _users_redirect(err='Nur ein Administrator kann sich automatisch anmelden lassen.'), None
        userctx.set_auto_login(current_user)
        return _users_redirect(info=f'Automatische Anmeldung für „{current_user}" eingeschaltet.'), None
    if action == 'disable':
        if not active:
            return _users_redirect(err='Die automatische Anmeldung ist nicht aktiv.'), None
        password, password2 = g('password'), g('password2')
        if password != password2:
            return _users_redirect(err='Die Passwörter stimmen nicht überein.'), None
        try:
            auth.set_password(active, password)
        except ValueError as e:
            return _users_redirect(err=str(e)), None
        userctx.set_auto_login(None)
        return _users_redirect(info=f'Automatische Anmeldung ausgeschaltet. „{active}" meldet sich ab jetzt mit Passwort an.'), active
    return _users_redirect(err='Unbekannte Aktion.'), None


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
