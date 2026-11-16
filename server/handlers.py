# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
POST request handlers for form submissions
"""
import os
import json
import math
import hashlib
import sqlite3
import datetime
import shutil
import tempfile
import threading
import time
from typing import NamedTuple
from urllib.parse import quote
import auth
import demo
import userctx
from . import wiso_upload
from .pages import Header1, Header2, Header3, Footer
from .import_preview import match_account
from .period import parse_iso_date, valid_period
from .richtext import sanitize_richtext as _sanitize_richtext
from db import Database
from db import datev_keys
from db.core import fk_explained, ForeignKeyError
from db.invoices import (InvoiceCol, InvoiceNotDeletable, QUOTE_STAYS_CONVERTED_MESSAGE,
                         QUOTE_STAYS_CONVERTED_SQL, QuoteNotConvertible, QuoteStatusLocked, is_storno_row)
from db.invoice_lock import (DocumentNotFound, EDITABLE_STATUSES, InvoiceLocked, STATUS_LABELS,
                             buyer_snapshot, status_group)
from db.invoice_storno import NO_COUNTER_BOOKING_HINT
from db.invoice_versions import CorrectionInvalid
from db.invoice_payments import RECEIVABLE_STATUSES, PaymentRemovalRefused
from db.numbering import NumberRangeError, number_format_error
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
    entfernt (``_write_split_rows``: gelöscht, ein importierter Satz oder eine
    AfA-Buchung nur abgehängt). Die Summe wird NICHT erzwungen – Teilerfassung
    ist erlaubt und an der dreistufigen Statusanzeige der Übersicht erkennbar.

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
    Zahl in einer späteren Zeile bricht ab, bevor eine frühere gespeichert ist. Das
    gilt auch für die Zeilen, die der Nutzer entfernt hat: Würde dabei die Zahlung einer
    stornierten Rechnung wegfallen, zu der schon erstattet wurde, lehnt das Planen ab
    (``_Refused``) – alle Zeilen zusammen geprüft, bevor irgendetwas geschrieben ist.
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
    payment_coa_ids = db.get_payment_coa_ids()
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
            elif old_counter in payment_coa_ids and old_counter != account_coa_id:
                # Zweckkonto vorne, und die Geldseite hinten ist schon ein ANDERES Zahlungskonto (die
                # Kasse, ein anderes eigenes Bankkonto, das Verrechnungskonto 1460): Der Satz sagt, über
                # welches Konto er bezahlt wurde – das Zuordnen überschreibt das nicht. Konten und Form
                # bleiben, die Split-Summe zählt ihn, wie ``bank_effect`` ihn für diese Bankbewegung
                # zählt; wer das Konto ändern will, tut es ausdrücklich. Ein leeres Gegenkonto füllt
                # weiter das Konto der Bank.
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
            # überschrieben würden sonst korrekt erfasste Angaben. Sie werden
            # gar nicht erst geschrieben (auch ein leeres Feld bleibt leer).
            for name in ('date_booking', 'date_tax', 'recipient_client', 'currency'):
                del fields[name]
        elif old_row:
            # Satz unter dieser Bankbewegung: Datum, Steuerdatum und Empfänger folgen ihrer Änderung nur,
            # wenn er den Wert mit ihr geteilt hat (derselbe wie bis jetzt, leer = leer). Hat er einen
            # eigenen – eine zugeordnete Bestandsbuchung, ein verknüpfter Beleg –, bleibt der, je Feld.
            for name, column in (('date_booking', 1), ('date_tax', 2), ('recipient_client', 6)):
                if (old_row[column] or None) != (bank_row[column] or None):
                    del fields[name]
        if old_row:
            # Text und Beleg-Nr. schreibt der Editor nur, wenn die Zeile sie geändert hat (leer und NULL sind
            # dasselbe): Ein Satz, den man zuordnet und wieder löst, soll in jeder Spalte so dastehen wie vorher.
            if (old_row[15] or '') == fields['text']:
                del fields['text']
            if (old_row[16] or None) == fields['document_number']:
                del fields['document_number']
        planned.append((rid, fields, adopt))
    if bank_id:
        # Schritt 2: Was der Nutzer entfernt hat, darf keine Zahlung mitnehmen, die das Guthaben einer stornierten Rechnung
        # deckt. Geprüft wird jetzt, vor dem ersten Schreiben – ein Speichern gelingt ganz oder gar nicht.
        dropped = _dropped_split_rows(db, bank_id, {rid for rid, _fields, _adopt in planned if rid is not None})
        blocker = db.split_removal_blocker(dropped)
        if blocker:
            raise _Refused(blocker)
    return planned


def _dropped_split_rows(db, bank_id, kept_ids):
    """IDs der Sätze unter der Bankbewegung, deren Zeile der Nutzer entfernt hat: alle außer ``kept_ids`` und den
    Doppik-Spiegeln (COA und Gegenkonto beide liquide) – die tauchen im Editor nicht auf und bleiben deshalb unberührt."""
    bank_coa_ids = db.get_bank_coa_ids()
    return [child[0] for child in db.get_child_bookings_for_bank(bank_id)
            if child[0] not in kept_ids and not is_mirror_row(child[9], child[1], child[2], bank_coa_ids)]


def _write_split_rows(db, bank_id, planned):
    """Geplante Buchungssätze (aus ``_plan_split_rows``) schreiben; die Sätze der Zeilen, die der Nutzer
    entfernt hat, übernimmt ``Database.remove_split_row``: gelöscht, ein Satz aber, der nicht im Editor
    entstanden ist (Import, AfA-Buchung), nur von der Bankbewegung abgehängt."""
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

    # Vom Nutzer entfernte Zeilen – Doppik-Spiegel (COA und Gegenkonto beide
    # liquide) tauchen im Editor nicht auf und bleiben deshalb unberührt.
    for child_id in _dropped_split_rows(db, bank_id, keep_ids):
        db.remove_split_row(child_id)


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
    except (_Refused, PaymentRemovalRefused) as e:
        # abgelehnt. ``_Refused`` kommt aus dem Planen: nichts ist gespeichert. ``PaymentRemovalRefused`` kommt erst beim
        # Schreiben – nur, wenn sich der Bestand zwischen Planen und Schreiben geändert hat oder die Zeilen einzeln nicht in
        # dieser Reihenfolge gehen (``_plan_split_rows`` prüft sie zusammen): Dann ist die Zahlung geschützt geblieben, was
        # davor geschrieben wurde (Bankzeile, frühere Zeilen), steht aber schon – Klartext statt 500
        return 400, str(e)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, f"Fehler beim Speichern: {str(e)}"

def handle_delete_transaction(db: Database, booking_id):
    """Eine Buchung aus der Liste löschen (``POST /transactions/delete``) und zurück auf die Liste. Würde dabei die Zahlung
    einer stornierten Rechnung wegfallen, zu der schon erstattet wurde (``PaymentRemovalRefused``), ist nichts geändert, und die
    Liste zeigt den Grund.

    Returns tuple: (status_code, location)"""
    try:
        db.delete_transaction(booking_id)
    except PaymentRemovalRefused as e:
        return 303, f"/transactions?err={quote(str(e), safe='')}"
    return 303, "/transactions"


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
    except Exception:
        import traceback
        traceback.print_exc()                       # der volle Fehler steht im Server-Log, nicht in der Adresse
        return 303, f"/miscellaneous?export=error&msg={quote('Export fehlgeschlagen.')}"


#: Der Stapel kennt nur Tag und Monat des Belegdatums, und sein Wirtschaftsjahr beginnt am 1. Januar: Ein Zeitraum über den
#: Jahreswechsel würde Buchungen des Folgejahres still auf das Jahr des Vorlaufs legen.
DATEV_YEAR_REFUSED = ('Der Zeitraum geht über den Jahreswechsel – der Stapel kennt nur Tag und Monat. '
                      'Bitte je Kalenderjahr einzeln exportieren.')


def _datev_entries(rows, limit=5):
    """„Datum Betrag EUR; …; und k weitere“ für die ersten ``limit`` von ``rows`` ``[(Datum, Betrag€)]`` – nie Text oder Empfänger."""
    parts = [f"{day} {fmt_eur(amount, symbol=False)} EUR" for day, amount in rows[:limit]]
    if len(rows) > limit:
        parts.append(f"und {len(rows) - limit} weitere")
    return '; '.join(parts)


def _datev_unbooked_hint(unbooked):
    """Hinweis auf Bankbewegungen ohne Buchungssatz (``Database.check_transfers``): Anzahl, Eingänge und Ausgänge getrennt statt
    einer Summe mit Vorzeichen und die ersten fünf."""
    incoming = sum(to_minor(amount) for _bid, _day, amount in unbooked if amount > 0)
    outgoing = -sum(to_minor(amount) for _bid, _day, amount in unbooked if amount < 0)
    count = ('1 Bankbewegung im Zeitraum hat keinen Buchungssatz und steht'
             if len(unbooked) == 1 else
             f'{len(unbooked)} Bankbewegungen im Zeitraum haben keinen Buchungssatz und stehen')
    return (f"{count} nicht im Stapel (Eingänge {fmt_eur(from_minor(incoming), symbol=False)} EUR, "
            f"Ausgänge {fmt_eur(from_minor(outgoing), symbol=False)} EUR) – "
            + _datev_entries([(day, amount) for _bid, day, amount in unbooked]) + '.')


def _datev_elsewhere_hint(elsewhere, elsewhere_out):
    """Hinweis 2 (``Database.check_transfers``): Überträge, deren Eingangs-Satz dieser Stapel weglässt, weil die Ausgangsseite in
    einem anderen Zeitraum steht – und, in einem eigenen Satz, Überträge mit ihrer Ausgangsseite in diesem Stapel und ihrer
    Eingangsseite außerhalb (wurde der andere Zeitraum schon exportiert, bevor beide Seiten kontiert waren, stehen sie dort auch)."""
    parts = []
    if elsewhere:
        entries = _datev_entries([(day, amount) for _eid, day, amount in elsewhere])
        if len(elsewhere) == 1:
            parts.append('1 Übertrag zwischen eigenen Konten steht mit seiner Ausgangsseite außerhalb des Zeitraums und deshalb '
                         f'nicht in diesem Stapel – {entries}. Er gehört in den Stapel des Zeitraums seiner Ausgangsseite.')
        else:
            parts.append(f'{len(elsewhere)} Überträge zwischen eigenen Konten stehen mit ihrer Ausgangsseite außerhalb des Zeitraums '
                         f'und deshalb nicht in diesem Stapel – {entries}. Sie gehören in den Stapel des Zeitraums ihrer Ausgangsseite.')
    if elsewhere_out:
        entries = _datev_entries([(day, amount) for _eid, day, amount in elsewhere_out])
        if len(elsewhere_out) == 1:
            parts.append('1 Übertrag steht mit seiner Ausgangsseite in diesem Stapel, seine Eingangsseite liegt außerhalb des '
                         'Zeitraums – wurde der andere Zeitraum schon exportiert, bevor beide Seiten kontiert waren, steht er dort '
                         f'ebenfalls: {entries}.')
        else:
            parts.append(f'{len(elsewhere_out)} Überträge stehen mit ihrer Ausgangsseite in diesem Stapel, ihre Eingangsseite liegt '
                         'außerhalb des Zeitraums – wurde der andere Zeitraum schon exportiert, bevor beide Seiten kontiert waren, '
                         f'stehen sie dort ebenfalls: {entries}.')
    return ' '.join(parts)


def _datev_doubtful_hint(doubtful, trigger=None):
    """Hinweis 3 (``Database.check_transfers``): Überträge, die möglicherweise doppelt im Stapel stehen – Eingangs-Sätze, die der
    Stapel behält, obwohl im Umfeld ein Ausgang mit Satz oder anders kontiert liegt, Ausgangs-Sätze neben einem anders kontierten
    Eingang und eigenständige Spiegel neben einer kontierten Bankbewegung. ``trigger`` (Datum, Betrag€) nennt die älteste
    Bewegung, die das Zusammenlegen sperrt oder den Hinweis auslöst – nur Datum und Betrag."""
    entries = _datev_entries([(day, amount) for _eid, day, amount in doubtful])
    cause = ('im Umfeld ist nicht jede Bankbewegung kontiert, die beiden Seiten liegen weit auseinander, oder es gibt zusätzlich '
             'einen eigenständigen Umbuchungssatz')
    if len(doubtful) == 1:
        text = (f'1 Übertrag zwischen eigenen Konten steht möglicherweise doppelt im Stapel – {cause}: {entries}. '
                'Bitte die Bankbewegungen dieses Tages prüfen.')
    else:
        text = (f'{len(doubtful)} Überträge zwischen eigenen Konten stehen möglicherweise doppelt im Stapel – {cause}: {entries}. '
                'Bitte die Bankbewegungen dieser Tage prüfen.')
    if trigger:
        text += f' (Auslöser im Umfeld: {trigger[0]} {fmt_eur(trigger[1], symbol=False)} EUR)'
    return text


def _datev_check(date_from, date_to, check):
    """Prüfsumme des Hinweises: der Zeitraum und ALLE Einträge aller Hinweise (je Hinweisart: Satz- bzw. Bankbewegungs-ID, Datum und
    Betrag in Minor Units – auch die, die der Wortlaut nicht nennt, und der Auslöser), nicht der Wortlaut. Die Bestätigung gilt nur,
    solange nichts davon sich ändert: Wird eine Bewegung kontiert und kommt eine andere dazu, kommt der Hinweis neu."""
    parts = [date_from, date_to]
    for kind, rows in (('unbooked', check.unbooked), ('elsewhere', check.elsewhere), ('elsewhere_out', check.elsewhere_out),
                       ('doubtful', check.doubtful)):
        parts.append(kind + ':' + ';'.join(f'{entry_id},{day},{to_minor(amount)}' for entry_id, day, amount in rows))
    parts.append('trigger:' + (f'{check.trigger[0]},{to_minor(check.trigger[1])}' if check.trigger else ''))
    return hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]


def handle_datev_export(db: Database, post_data: dict):
    """DATEV Buchungsstapel-CSV für einen Datumsbereich erzeugen.

    Setzt dabei für alle exportierten Buchungen das Steuerdatum (DateTax)
    auf das heutige Datum.

    Der Zeitraum liegt in einem Kalenderjahr (``DATEV_YEAR_REFUSED``). Unstimmige Splits sperren den Export. Danach
    sammelt ``Database.check_transfers`` Hinweise (null bis drei, in dieser Reihenfolge): Bankbewegungen im Zeitraum ohne
    Buchungssatz, die kein Spiegel der Gegenseite deckt, fehlen im Stapel; Überträge zwischen eigenen Konten, deren
    Eingangs-Satz der Stapel weglässt, weil die Ausgangsseite außerhalb des Zeitraums liegt, fehlen darin ebenfalls (und
    umgekehrt: Steht die Ausgangsseite hier und die Eingangsseite außerhalb, steht der Übertrag vielleicht auch in einem schon
    exportierten anderen Stapel); und Eingangs-Sätze, die der Stapel behält, obwohl im Umfeld ein Ausgang mit Satz oder anders
    kontiert liegt (ebenso Ausgangs-Sätze neben einem anders kontierten Eingang und eigenständige Spiegel neben einer
    kontierten Bankbewegung), stehen möglicherweise doppelt darin. Gibt es Hinweise, läuft der Export nur mit ausdrücklicher
    Bestätigung (``confirm_export`` = ``1`` und ``confirmed_check`` = ``_datev_check`` über Zeitraum und alle Einträge des frisch
    berechneten Hinweises), sonst leitet er mit dem Hinweis zurück (``datev_export=confirm``). Überträge zwischen eigenen Konten, die auf
    beiden Bankbewegungen kontiert sind, stehen nur einmal im Stapel (``Database.check_transfers``: die Eingangsseite fällt
    weg, wo es sicher zu erkennen ist); die weggelassenen Sätze bekommen kein Steuerdatum.

    Returns:
        (csv_bytes, filename)  – für einen Datei-Download, oder
        (303, location_str)    – bei Fehler oder fehlender Bestätigung (Redirect)
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
        if date_from[:4] != date_to[:4]:
            return 303, '/miscellaneous?datev_export=error&msg=' + quote(DATEV_YEAR_REFUSED)

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

        # Was im Stapel fehlt oder doppelt stehen könnte, darf nicht still so bleiben – das kann gewollt sein (etwa im
        # Parallelbetrieb mit einer anderen Buchhaltung), also verlangt der Export eine ausdrückliche Bestätigung, und die gilt
        # nur für genau diesen Hinweis (Prüfsumme über Zeitraum und alle Einträge). Die Meldung nennt weder Text noch Empfänger
        # der Bankbewegungen.
        check = db.check_transfers(date_from, date_to)
        hints = []
        if check.unbooked:
            hints.append(_datev_unbooked_hint(check.unbooked))
        if check.elsewhere or check.elsewhere_out:
            hints.append(_datev_elsewhere_hint(check.elsewhere, check.elsewhere_out))
        if check.doubtful:
            hints.append(_datev_doubtful_hint(check.doubtful, check.trigger))
        if hints:
            checksum = _datev_check(date_from, date_to, check)
            if not (post_data.get('confirm_export', [''])[0] == '1'
                    and post_data.get('confirmed_check', [''])[0] == checksum):
                msg = 'Nicht exportiert: ' + ' '.join(hints) + ' Bitte bestätigen, um trotzdem zu exportieren.'
                return 303, ('/miscellaneous?datev_export=confirm'
                             f'&datev_from={date_from}&datev_to={date_to}&datev_check={checksum}&msg={quote(msg)}')

        # Ein Übertrag zwischen eigenen Konten, auf beiden Bankbewegungen kontiert, steht nur einmal im Stapel: Die Sätze der
        # Eingangsseite fehlen (gezählt über den ganzen Bestand, nicht nur im Zeitraum) und bekommen kein Steuerdatum.
        bookings = [row for row in db.fetch_bookings_range(date_from, date_to) if row[0] not in check.omitted]
        if not bookings:
            return 303, f'/miscellaneous?datev_export=error&msg=Keine+Buchungen+im+Zeitraum+{date_from}+bis+{date_to}'

        coa_map = db.get_coa_id_to_number_map()
        consultant_no, client_no = db.get_datev_numbers()
        csv_bytes, exported_ids = datev_module.export_to_datev(
            bookings, coa_map, date_from, date_to,
            liquid_coa_ids=db.get_payment_coa_ids(),
            waiting_numbers=db.get_waiting_account_numbers(),
            invoices=db.get_datev_invoice_data(),
            consultant_no=consultant_no, client_no=client_no
        )

        # Steuerdatum der exportierten Buchungen auf heute setzen
        today_iso = datetime.date.today().isoformat()
        db.update_bookings_date_tax_batch(exported_ids, today_iso)

        year = date_from[:4]
        filename = f"DATEV_Buchungsstapel_{year}_{date_from.replace('-','')}-{date_to.replace('-','')}.csv"
        return csv_bytes, filename

    except Exception as e:
        traceback.print_exc()
        return 303, f'/miscellaneous?datev_export=error&msg={quote(str(e))}'


#: Größte Anfrage des Logo-Uploads in Bytes (die Datei samt Multipart-Rahmen); die Grenze gilt vor dem Lesen
MAX_LOGO_BYTES = 5 * 1024 * 1024

LOGO_TYPE_ERROR = 'Nur Bilddateien (PNG, JPG, GIF, WEBP) sind als Logo erlaubt.'
LOGO_SIZE_ERROR = f'Das Logo ist zu groß (höchstens {MAX_LOGO_BYTES // (1024 * 1024)} MB).'


def _logo_refusal(message, request_handler=None):
    """Ablehnung des Logo-Uploads als JSON. Mit ``request_handler`` wurde der Body NICHT gelesen: Die
    Verbindung wird nach der Antwort geschlossen, sonst läse der Server die ungelesenen Bytes als nächste
    Anfrage (Keep-Alive)."""
    if request_handler is not None:
        request_handler.close_connection = True
    return json.dumps({'success': False, 'error': message}).encode()


def handle_logo_upload(request_handler):
    """Logo-/Bild-Datei eines Kontakts hochladen (Multipart, Feld 'logofile').

    Browser geben aus Sicherheitsgründen nie den echten Dateipfad preis. Daher wird
    die Datei tatsächlich nach data/logos/ gespeichert und der serverseitige Pfad
    zurückgegeben, den das Logo-Feld dann übernimmt.

    Angenommen werden nur Bilddateien (Positivliste ``export.pdf_core.LOGO_CONTENT_TYPES``), deren
    Anfang zur Endung passt, bis ``MAX_LOGO_BYTES``. Die Größe prüft ``Content-Length`` vor dem Lesen: Fehlt
    sie, ist sie keine Zahl, 0 oder zu groß, wird abgelehnt, ohne den Body zu lesen.

    Returns: JSON-bytes {'success': bool, 'path'|'error': str}
    """
    import os
    import re as _re_logo
    from export.pdf_core import logo_content_matches, logo_extension
    from .multipart import first_file

    content_type = request_handler.headers.get('Content-Type', '')
    if 'multipart/form-data' not in content_type:
        return _logo_refusal('Kein Multipart-Upload', request_handler)
    declared = (request_handler.headers.get('Content-Length') or '').strip()
    if not (declared.isascii() and declared.isdigit()) or int(declared) == 0:
        return _logo_refusal('Keine Datei empfangen', request_handler)
    length = int(declared)
    if length > MAX_LOGO_BYTES:
        return _logo_refusal(LOGO_SIZE_ERROR, request_handler)
    raw = request_handler.rfile.read(length)
    part = first_file(content_type, raw)
    if not part or not part.content:
        return _logo_refusal('Keine Datei empfangen')

    base = os.path.basename(part.filename or 'logo')
    safe = _re_logo.sub(r'[^A-Za-z0-9._-]', '_', base) or 'logo'
    extension = logo_extension(safe)
    if not extension or not logo_content_matches(extension, part.content):
        return _logo_refusal(LOGO_TYPE_ERROR)
    # Physisch im logos-Verzeichnis des aktuellen Nutzers ablegen; in der DB und
    # nach außen bleibt der logische Pfad 'data/logos/<datei>' (kein Username in
    # der URL – der Static-Handler löst ihn pro Nutzer auf).
    import userctx
    try:
        physical = os.path.join(userctx.user_subdir('logos'), safe)
        with open(physical, 'wb') as f:
            f.write(part.content)
    except Exception:
        import traceback
        traceback.print_exc()                      # Einzelheiten (Pfade) nur ins Server-Log
        return _logo_refusal('Speichern fehlgeschlagen.')
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


#: Dateinamen, die WISO Mein Büro im DB-Verzeichnis anlegt (unter diesen Namen liegt der Upload, siehe ``wiso_upload``).
WISO_CLIENT_DB = wiso_upload.WISO_CLIENT_DB
WISO_STANDARD_DB = wiso_upload.WISO_STANDARD_DB

#: Der Import aus der WISO-Datenbank ist für einen leeren Bestand gedacht: Ist ``Bookings`` nicht leer, läuft er nur mit
#: ausdrücklicher Bestätigung (Pflicht-Kästchen ``confirm_existing`` im Formular, Wert genau ``1``).
WISO_FDB_STOCK_REFUSED = 'Der Bestand ist nicht leer – bitte den Import ausdrücklich bestätigen.'

#: Zur Kennung des Formulars gibt es keine hochgeladene Mandantendatei (mehr): Jeder Import löscht seinen Zwischenordner.
WISO_FDB_UPLOAD_MISSING = 'Die hochgeladene Datei wurde nicht gefunden – bitte erneut hochladen.'

#: Die hochgeladene Datei ist keine lesbare WISO-Datenbank: Es wurde nichts importiert.
WISO_FDB_UNREADABLE = ('Die hochgeladene Datei ließ sich nicht als WISO-Datenbank lesen – bitte DB1.FDB (und DB0.FDB) '
                       'aus dem DB-Verzeichnis von WISO wählen. Es wurde nichts importiert.')


def _without_upload_path(value, token):
    """``value`` – ein Text oder beliebig verschachtelte Listen, Tupel und Wörterbücher aus Texten – ohne den Pfad des
    Zwischenordners zu ``token``, mit und ohne abschließenden Trenner und auch in der aufgelösten und der Schreibweise, in der
    ein ``OSError`` Pfade nennt (Rückstriche doppelt). Fehlertexte der Importer nennen den Pfad der Datei; er gehört nicht in
    die Antwort, die Weiterleitung und ``wiso_fdb_result.json`` – von ``<Ordner>/DB1.FDB`` bleibt ``DB1.FDB``."""
    folder = wiso_upload.folder_path(token)
    if folder is None:
        return value
    bases = {folder, os.path.abspath(folder), os.path.realpath(folder)}
    bases |= {base.replace('\\', '\\\\') for base in bases}
    variants = sorted({base + trailer for base in bases for trailer in ('', '/', '\\', '\\\\')}, key=len, reverse=True)

    def clean(item):
        if isinstance(item, str):
            for variant in variants:
                item = item.replace(variant, '')
            return item
        if isinstance(item, dict):
            return {clean(key): clean(inner) for key, inner in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(inner) for inner in item]
        return item

    return clean(value)


def handle_wiso_fdb_import(db: Database, post_data):
    """Import direkt aus der WISO-Datenbank (Firebird-Datei).

    Die Mandantendatei (``DB1.FDB``, dazu optional der Standard-Kontenrahmen ``DB0.FDB``) kommt als Upload: Der Browser hat sie
    vorher über ``POST /wiso/fdb-upload`` in einen Zwischenordner des Benutzers gelegt (``server/wiso_upload.py``) und nennt
    im Formular nur dessen Kennung (``upload``). Ein Pfad aus der Anfrage wird nie gelesen. **Der Zwischenordner wird in jedem
    Fall gelöscht** – nach dem Import, bei einem Blocker, einem Fehler und jeder Ablehnung.

    Im Demo-Modus wird nichts importiert. Der Import ist für einen leeren Bestand gedacht: Ist ``Bookings`` nicht leer
    (``Database.wiso_import_stock``) und ``confirm_existing`` nicht ``1``, wird
    **vor** jedem Zugriff auf die Datei nichts importiert und die Seite meldet
    :data:`WISO_FDB_STOCK_REFUSED`. ``Database.import_wiso_fdb`` selbst prüft das nicht.

    Meldungen, Ergebnisdatei und Weiterleitung nennen nie den Pfad des Zwischenordners (``_without_upload_path``);
    ``result['file']`` ist ``DB1.FDB``.

    Returns:
        (303, location_str) – immer ein Redirect zu /miscellaneous
    """
    from urllib.parse import quote

    def flag(name):
        return post_data.get(name, ['0'])[0] in ('1', 'on', 'true')

    token = post_data.get('upload', [''])[0]
    try:
        if demo.is_active():
            return 303, f'/miscellaneous?wiso_fdb=error&msg={quote(wiso_upload.DEMO_REFUSED)}'
        if db.wiso_import_stock()['bookings'] > 0 and post_data.get('confirm_existing', [''])[0] != '1':
            return 303, f'/miscellaneous?wiso_fdb=error&msg={quote(WISO_FDB_STOCK_REFUSED)}'

        with_assets = flag('with_assets')
        with_invoices = flag('with_invoices')
        client, standard = wiso_upload.files(token)
        if client is None:
            return 303, f'/miscellaneous?wiso_fdb=error&msg={quote(WISO_FDB_UPLOAD_MISSING)}'
        wiso_upload.mark_importing(token)

        try:
            result = _without_upload_path(
                db.import_wiso_fdb(client, standard, with_assets=with_assets, with_invoices=with_invoices), token)
            result['file'] = WISO_CLIENT_DB
            result['standard_chart'] = WISO_STANDARD_DB if standard else ''
            if result.get('unreadable'):
                # Keine WISO-Datenbank (falsche Datei gewählt, Upload beschädigt): ein Fehler, kein „Import abgeschlossen“
                _store_fdb_result(result)
                return 303, f'/miscellaneous?wiso_fdb=error&msg={quote(WISO_FDB_UNREADABLE)}'
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
                payments = _without_upload_path(db.link_wiso_invoice_payments(client, standard), token)
                result['payments_linked'] = payments['payments_linked']
                result['payment_warnings'] = payments['payment_warnings']
                result['errors'] += payments['errors']
            _store_fdb_result(result)

            return 303, (
                f'/miscellaneous?wiso_fdb=ok'
                f'&file={quote(WISO_CLIENT_DB)}'
                f'&imported={result["imported"]}&skipped={result["skipped"]}'
                f'&adopted={result.get("adopted", 0)}'
                f'&possible={result.get("possible_duplicates_count", 0)}'
                f'&regrouped={result.get("regrouped_rows", 0)}&orphans={result.get("orphaned_rows_count", 0)}'
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
            return 303, f'/miscellaneous?wiso_fdb=error&msg={quote(_without_upload_path(str(e), token))}'
    finally:
        wiso_upload.discard(token)


def _store_fdb_result(result):
    """Detailergebnis für die Anzeige auf /miscellaneous ablegen."""
    path = os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json')
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass                                    # Anzeige ist nicht kritisch


# ── SQL-Konsole ──────────────────────────────────────────────────────────────
# Zwei Modi; das Formularfeld ``write`` mit genau dem Wert ``1`` schaltet den Schreibmodus ein, alles andere (auch
# fehlend) bleibt im Lesemodus:
#  * Lesemodus (Standard): PRAGMA query_only = ON und ein Authorizer, der nur Abfragen durchlässt. Jeder Befehl läuft für
#    sich, ein Fehler stoppt die folgenden nicht. Der MCP-Server (tools/mcp_ubuchhaltung.py) schickt nie ``write``.
#  * Schreibmodus: Fremdschlüssel an, ein Wachstumslimit für die Datei und EINE Transaktion (BEGIN IMMEDIATE durch den
#    Handler) für alle Befehle: Der erste Fehler, ein Abbruch (Zeit, Platte) oder ein scheiterndes COMMIT nimmt alles
#    zurück; erst danach gehen die schreibenden Befehle ins SQL-Protokoll.
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
# Seit 2.3.11: Grenzen der Eingabe (_console_statements prüft sie, bevor etwas läuft), der Fehlerliste und der Platte.
_CONSOLE_MAX_SQL_BYTES = 10_000_000     # UTF-8-Bytes der Eingabe (die Anfrage selbst endet ohnehin bei MAX_FORM_BODY)
_CONSOLE_MAX_STATEMENTS = 200_000       # Befehle je Anfrage
_CONSOLE_MAX_SPLIT_TRIES = 2_000        # je Befehl geprüfte Semikolons: sonst würde ein Text voller Semikolons quadratisch
_CONSOLE_MAX_SPLIT_SCAN = 500_000_000   # … und so viele Zeichen darf das Trennen insgesamt erneut lesen (Semikolons in Texten)
_CONSOLE_MAX_ERRORS = 100               # Einträge in "errors"; weitere fasst eine Zeile „… n weitere Fehler“ zusammen
_CONSOLE_MAX_GROWTH_BYTES = 1_000_000_000   # Schreibmodus: so viel darf die Datenbankdatei wachsen (PRAGMA max_page_count)
_CONSOLE_MIN_FREE_BYTES = 256_000_000   # freier Platz im Datenordner und im Temp-Ordner: sinkt er darunter, bricht die Anfrage ab
_CONSOLE_MAX_DISK_BYTES = 1_000_000_000  # … ebenso, wenn der freie Platz seit Beginn der Anfrage um mehr gesunken ist
_CONSOLE_DISK_CHECK_EVERY = 100         # der Platz wird bei jedem n-ten Aufruf des Fortschrittshandlers geprüft (je 10000 Schritte)

# Meldungen der Konsole. Sie stehen fest hier (nie Pfade oder Daten der Anfrage), Tests prüfen den Wortlaut.
CONSOLE_READONLY = 'nicht erlaubt – die Konsole läuft nur lesend; zum Ändern „Schreiben erlauben“ ankreuzen'
CONSOLE_DENIED = ('nicht erlaubt – ATTACH, DETACH, VACUUM sowie verändernde oder unbekannte PRAGMAs sind in der Konsole '
                  'gesperrt, ebenso die Funktionen fts3_tokenizer und load_extension')
CONSOLE_DENIED_WRITE = (CONSOLE_DENIED + '; im Schreibmodus außerdem BEGIN, COMMIT, ROLLBACK, SAVEPOINT, RELEASE und '
                        'PRAGMA foreign_keys mit Wert (die Befehle laufen dort in einer eigenen Transaktion)')
CONSOLE_NOTHING_CHANGED = ('Nichts wurde geändert – im Schreibmodus laufen alle Befehle einer Anfrage in einer '
                           'Transaktion.')
CONSOLE_BUSY = 'Es läuft bereits eine Anfrage der SQL-Konsole – bitte warten.'
CONSOLE_LOCKED = ('Die Datenbank ist gerade gesperrt (ein anderer Vorgang schreibt) – nichts wurde ausgeführt. '
                  'Bitte gleich noch einmal versuchen.')
CONSOLE_LOG_WARNING = 'Die Änderungen sind gespeichert, ließen sich aber nicht ins SQL-Protokoll schreiben.'

# Eine Anfrage der Konsole zur Zeit (je Prozess): Jede läuft bis zu 60 s, hält im Schreibmodus die Schreibsperre der
# Datenbank und belegt eine Verbindung samt Speicher – mehrere übereinander könnten den Server lahmlegen.
_console_lock = threading.Lock()


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


# Was der Authorizer im Lesemodus durchlässt: Abfragen, Lesen, Funktionen, rekursive Abfragen und – nach den Positivlisten
# von _console_authorizer – PRAGMAs. Jede andere Aktion (INSERT, UPDATE, DELETE, CREATE …, Transaktionen, Savepoints,
# ANALYZE, REINDEX …) verlangt etwas Schreibendes.
_CONSOLE_READ_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE,
    sqlite3.SQLITE_PRAGMA,
})


class _ConsoleAuthorizer:
    """Der Authorizer einer Anfrage der SQL-Konsole (``Connection.set_authorizer``).

    Die Sperren von ``_console_authorizer`` gelten in beiden Modi. Im Lesemodus lässt er darüber hinaus nur
    ``_CONSOLE_READ_ACTIONS`` durch. Im Schreibmodus sperrt er zusätzlich BEGIN, COMMIT, ROLLBACK, SAVEPOINT und
    ``PRAGMA foreign_keys`` mit Wert: Alle Befehle laufen dort in der einen Transaktion des Handlers, und das PRAGMA wäre
    in ihr wirkungslos. ``wrote`` merkt sich, ob der zuletzt geprüfte Befehl etwas Schreibendes verlangt hat – der Handler
    setzt es vor jedem Befehl zurück und legt die Verbindung ohne Statement-Cache an, damit SQLite jeden Befehl neu prüft.
    """

    def __init__(self, write):
        self.write = write
        self.wrote = False

    def __call__(self, action, arg1, arg2, dbname, source):
        if _console_authorizer(action, arg1, arg2, dbname, source) != sqlite3.SQLITE_OK:
            return sqlite3.SQLITE_DENY
        if self.write:
            if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_PRAGMA and (arg1 or '').lower() == 'foreign_keys' and arg2 is not None:
                return sqlite3.SQLITE_DENY
        if action not in _CONSOLE_READ_ACTIONS:
            if not self.write:
                return sqlite3.SQLITE_DENY
            self.wrote = True
        return sqlite3.SQLITE_OK


class _ConsoleRefused(Exception):
    """Die Eingabe überschreitet eine Grenze der Konsole; ``str(…)`` ist der Text für den Nutzer."""


def _console_statements(sql):
    """Die Befehle einer Eingabe der SQL-Konsole als Liste.

    Getrennt wird an einem ``;`` nur dort, wo ``sqlite3.complete_statement`` den Text seit dem letzten Befehl als vollständig
    ansieht: Semikolons in Zeichenketten, Kommentaren und Rümpfen von Triggern (``BEGIN … END;``) trennen nichts. Der Rest
    ohne abschließendes ``;`` ist der letzte Befehl; leere Befehle (nur Leerraum oder ``;``) entfallen. Die Grenzen
    (_CONSOLE_MAX_SQL_BYTES, _CONSOLE_MAX_STATEMENTS, _CONSOLE_MAX_SPLIT_TRIES, _CONSOLE_MAX_SPLIT_SCAN) gelten vor jeder
    Ausführung: Wer sie überschreitet, bekommt ``_ConsoleRefused`` mit Klartext, und nichts läuft.
    """
    if len(sql.encode('utf-8', 'replace')) > _CONSOLE_MAX_SQL_BYTES:
        raise _ConsoleRefused(f'Die Eingabe ist zu groß (höchstens {_CONSOLE_MAX_SQL_BYTES} Bytes) – '
                              'nichts wurde ausgeführt.')
    if '\x00' in sql:
        raise _ConsoleRefused('Die Eingabe enthält ein Nullzeichen – nichts wurde ausgeführt.')
    too_many = (f'Die Eingabe enthält zu viele Befehle (höchstens {_CONSOLE_MAX_STATEMENTS}) – '
                'nichts wurde ausgeführt.')
    commands = []
    start = tries = rescanned = 0
    position = sql.find(';')
    while position != -1:
        piece = sql[start:position + 1]
        try:
            complete = sqlite3.complete_statement(piece)
        except ValueError:                              # ein einzelnes Surrogat-Zeichen lässt sich nicht kodieren
            raise _ConsoleRefused('Die Eingabe enthält ein ungültiges Zeichen – nichts wurde ausgeführt.') from None
        if complete:
            start, tries = position + 1, 0
            if piece.replace(';', '').strip():
                commands.append(piece.strip())
                if len(commands) > _CONSOLE_MAX_STATEMENTS:
                    raise _ConsoleRefused(too_many)
        else:
            tries += 1
            rescanned += len(piece)
            if tries > _CONSOLE_MAX_SPLIT_TRIES or rescanned > _CONSOLE_MAX_SPLIT_SCAN:
                raise _ConsoleRefused(f'Befehl {len(commands) + 1}: zu viele Semikolons in einer Zeichenkette oder einem '
                                      'Kommentar – nichts wurde ausgeführt.')
        position = sql.find(';', position + 1)
    rest = sql[start:].strip()
    if rest:
        commands.append(rest)
        if len(commands) > _CONSOLE_MAX_STATEMENTS:
            raise _ConsoleRefused(too_many)
    return commands


def _console_log_text(command):
    """Der Befehl, wie ihn das SQL-Protokoll ablegt: ohne Kommentare und ohne das abschließende Semikolon.

    ``DocumentParser.log_sql`` schreibt Befehle einzeilig ins Protokoll (sql_operations.sql); ein ``--``-Kommentar
    verschluckte dabei den Rest des Befehls, und ein Nachspielen des Protokolls änderte mehr, als die Konsole geändert hat.
    Zeichenketten und Bezeichner in Anführungszeichen bleiben unberührt."""
    kept, closing, i = [], None, 0
    while i < len(command):
        char = command[i]
        if closing:
            kept.append(char)
            if char == closing:
                closing = None
        elif char in '\'"`[':
            kept.append(char)
            closing = ']' if char == '[' else char
        elif command.startswith('--', i):
            end = command.find('\n', i)
            i = len(command) if end == -1 else end
            continue
        elif command.startswith('/*', i):
            end = command.find('*/', i + 2)
            kept.append(' ')
            i = len(command) if end == -1 else end + 2
            continue
        else:
            kept.append(char)
        i += 1
    return ''.join(kept).strip().rstrip(';').rstrip()


def _console_write_log(commands):
    """Die schreibenden Befehle einer festgeschriebenen Anfrage ins SQL-Protokoll (``DocumentParser.log_sql``, wie überall,
    Beschreibung ``SQL-Konsole (<Benutzer>)``). ``False``, wenn das Protokoll sich nicht schreiben ließ – die Änderung steht
    dann trotzdem in der Datenbank; der Fehler selbst steht nur im Server-Log."""
    user = userctx.get_user()
    description = f'SQL-Konsole ({user})' if user else 'SQL-Konsole'
    try:
        parser = DocumentParser()
        for command in commands:
            parser.log_sql(_console_log_text(command), (), description)
    except Exception as e:
        print(f'WARNUNG: SQL-Konsole: Das SQL-Protokoll konnte nicht geschrieben werden: {e}')
        return False
    return True


def _console_disk_folders(db):
    """Die Ordner, deren freien Platz die Konsole im Auge behält: der der Datenbank (Journal, Wachstum) und der Temp-Ordner
    (Zwischenergebnisse großer Abfragen)."""
    folders = [os.path.dirname(os.path.abspath(db.db_name))]
    try:
        folders.append(tempfile.gettempdir())
    except Exception:                                   # kein brauchbarer Temp-Ordner: ein Fehler beim Messen bricht nichts ab
        pass
    return folders


def _console_free_space(folders):
    """Freier Platz in Bytes je Ordner; ``None`` für einen Ordner, den sich nicht messen lässt."""
    free = []
    for folder in folders:
        try:
            free.append(shutil.disk_usage(folder).free)
        except Exception:
            free.append(None)
    return free


def _console_disk_exceeded(folders, free_at_start):
    """Hat die Anfrage zu viel Platz belegt? Ja, wenn der freie Platz in einem der Ordner seit ihrem Beginn um mehr als
    ``_CONSOLE_MAX_DISK_BYTES`` gesunken ist – oder gesunken ist und nun unter ``_CONSOLE_MIN_FREE_BYTES`` liegt. Ein von
    vornherein kleiner Ordner (ein knapp bemessener Temp-Ordner), in dem sich nichts ändert, bricht nichts ab; ebenso wenig
    ein Ordner, der sich nicht messen lässt."""
    for now, before in zip(_console_free_space(folders), free_at_start):
        if now is None or before is None or now >= before:
            continue
        if now < _CONSOLE_MIN_FREE_BYTES or before - now > _CONSOLE_MAX_DISK_BYTES:
            return True
    return False


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


def _console_result(success_count, total, errors, output, **extra):
    """Die Antwort der Konsole als dict (``handle_execute_sql`` macht JSON daraus)."""
    return {'success_count': success_count, 'total': total, 'errors': errors, 'output': output, **extra}


def _console_undone(conn, write, total, errors):
    """Antwort einer Anfrage, die nichts festschreibt: ``success_count`` 0, keine Ausgabe. Im Schreibmodus wird die
    Transaktion zurückgerollt (``rolled_back`` ist wahr, der Satz „Nichts wurde geändert …“ steht hinter den Fehlern).
    Authorizer und Fortschrittshandler fallen vorher weg: ROLLBACK ist dem Nutzer gesperrt, dem Handler nicht."""
    if not write:
        return _console_result(0, total, errors, '')
    try:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
        conn.rollback()
    except sqlite3.Error:                               # die Verbindung schließt gleich: Auch das nimmt alles zurück
        pass
    return _console_result(0, total, errors + [CONSOLE_NOTHING_CHANGED], '', rolled_back=True)


def _console_error_text(error, write):
    """Der Text zu einem Fehler eines Befehls: Was der Authorizer ablehnt, bekommt die feste Meldung des Modus."""
    text = str(error)
    if 'not authorized' in text or 'authorization denied' in text:
        return CONSOLE_DENIED_WRITE if write else CONSOLE_READONLY
    if not write and 'readonly database' in text:       # die zweite Schicht des Lesemodus: PRAGMA query_only
        return CONSOLE_READONLY
    return text


def _run_console(db, commands, write):
    """Führt die Befehle einer Anfrage aus; liefert ``(Antwort als dict, schreibende Befehle)``.

    Lesemodus: jeder Befehl für sich, ein Fehler stoppt die folgenden nicht. Schreibmodus: alles in EINER Transaktion; der
    erste Fehler, ein Abbruch durch Zeit- oder Plattengrenze und ein scheiterndes COMMIT nehmen alles zurück. Die
    schreibenden Befehle (nach dem Urteil des Authorizers) gibt es nur bei festgeschriebener Transaktion zurück."""
    total = len(commands)
    authorizer = _ConsoleAuthorizer(write)
    output_lines = []
    output_bytes = 0
    output_truncated = False
    errors = []
    error_count = 0
    success_count = 0
    written = []
    aborted = None                       # ('time' oder 'disk', Nummer des Befehls)
    failure = None                       # Schreibmodus: der Fehler, der die Anfrage beendet

    # Ohne Statement-Cache: Sonst prüfte SQLite einen zweiten, gleich lautenden Befehl nicht mehr gegen den Authorizer,
    # und ``wrote`` bliebe falsch.
    conn = sqlite3.connect(db.db_name, isolation_level=None, cached_statements=0)
    try:
        if hasattr(conn, 'setlimit'):                   # ab Python 3.11
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _CONSOLE_MAX_VALUE_BYTES)
            # Python holt eine Ergebniszeile ganz, bevor die Ausgabegrenze prüfen kann: Erst
            # Wertgröße × Spaltenzahl begrenzt, was eine einzelne Zeile an Speicher belegt.
            conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, _CONSOLE_MAX_COLUMNS)
        if write:
            # Vor dem Authorizer, der diese PRAGMAs und BEGIN dem Nutzer sperrt. foreign_keys wirkt nur außerhalb einer
            # Transaktion, also vor BEGIN. PRAGMAs nehmen keine Parameter; der Wert ist eine selbst errechnete ganze Zahl.
            try:
                conn.execute('PRAGMA foreign_keys = ON')
                pages = conn.execute('PRAGMA page_count').fetchall()[0][0]
                page_size = conn.execute('PRAGMA page_size').fetchall()[0][0]
                limit = pages + max(1, _CONSOLE_MAX_GROWTH_BYTES // page_size)
                conn.execute(f'PRAGMA max_page_count = {int(limit)}').fetchall()
                conn.execute('BEGIN IMMEDIATE')
            except sqlite3.Error as e:
                text = str(e).lower()
                if 'locked' in text or 'busy' in text:
                    return _console_result(0, total, [CONSOLE_LOCKED], ''), []
                return _console_result(0, total, [f'Der Schreibmodus ließ sich nicht starten ({e}) – nichts wurde '
                                                  'ausgeführt.'], ''), []
        else:
            conn.execute('PRAGMA query_only = ON')
        conn.set_authorizer(authorizer)

        deadline = time.monotonic() + _CONSOLE_TIME_LIMIT_S
        folders = _console_disk_folders(db)
        free_at_start = _console_free_space(folders)
        state = {'calls': 0, 'abort': None}

        def progress():
            if time.monotonic() > deadline:
                state['abort'] = 'time'
                return 1
            state['calls'] += 1
            if state['calls'] % _CONSOLE_DISK_CHECK_EVERY == 0 and _console_disk_exceeded(folders, free_at_start):
                state['abort'] = 'disk'
                return 1
            return 0

        conn.set_progress_handler(progress, 10000)

        for i, command in enumerate(commands, 1):
            if time.monotonic() > deadline:
                aborted = ('time', i)
                break
            authorizer.wrote = False
            cursor = conn.cursor()
            try:
                cursor.execute(command)
                # Das Ergebnis jedes Befehls mit Spalten (SELECT, WITH, PRAGMA, EXPLAIN, RETURNING) ausgeben – bis zu den
                # Ausgabegrenzen, danach nur noch ausführen
                if cursor.description is not None and not output_truncated:
                    for line in _console_result_lines(cursor):
                        size = len(line.encode('utf-8')) + 1        # + Zeilenumbruch
                        if (len(output_lines) >= _CONSOLE_MAX_OUTPUT_ROWS
                                or output_bytes + size > _CONSOLE_MAX_OUTPUT_BYTES):
                            output_truncated = True
                            break
                        output_lines.append(line)
                        output_bytes += size
                success_count += 1                  # erst nach dem Lesen: ein Fehler in einer späteren Zeile zählt nicht als Erfolg
            except sqlite3.Error as e:
                if state['abort'] and isinstance(e, sqlite3.OperationalError) and 'interrupted' in str(e):
                    # Vom Fortschrittshandler abgebrochen. Dass er es war, steht in ``state``: Der Text allein träfe
                    # auch „no such table: interrupted“.
                    aborted = (state['abort'], i)
                    break
                text = f'Befehl {i}: {_console_error_text(e, write)}'
                if write:
                    failure = text
                    break
                error_count += 1
                if len(errors) < _CONSOLE_MAX_ERRORS:
                    errors.append(text)
                continue
            finally:
                cursor.close()                  # beendet auch ein halb gelesenes Ergebnis (RETURNING) vor dem COMMIT
            if authorizer.wrote:
                written.append(command)

        if aborted:
            reason, number = aborted
            if reason == 'time':
                text = f'Befehl {number}: abgebrochen – Zeitlimit von {_CONSOLE_TIME_LIMIT_S} s überschritten.'
            else:
                text = f'Befehl {number}: abgebrochen – die Anfrage belegte zu viel Plattenplatz.'
            return _console_undone(conn, write, total, [text]), []
        if failure:
            return _console_undone(conn, write, total, [failure]), []
        if write:
            conn.set_authorizer(None)           # COMMIT ist dem Nutzer gesperrt, dem Handler nicht
            conn.set_progress_handler(None, 0)
            try:
                conn.commit()
            except sqlite3.Error as e:          # aufgeschobene Fremdschlüssel-Prüfung (PRAGMA defer_foreign_keys)
                return _console_undone(conn, True, total, [f'Beim Festschreiben (COMMIT): {e}']), []
        if output_truncated:
            max_size = (f'{_CONSOLE_MAX_OUTPUT_BYTES // 1_000_000} MB'
                        if _CONSOLE_MAX_OUTPUT_BYTES % 1_000_000 == 0
                        else f'{_CONSOLE_MAX_OUTPUT_BYTES} Bytes')
            output_lines.append(
                f'… Ausgabe gekürzt (mehr als {_CONSOLE_MAX_OUTPUT_ROWS} Zeilen bzw. {max_size}) '
                f'– bitte mit WHERE oder LIMIT eingrenzen')
        if error_count > len(errors):
            errors.append(f'… {error_count - len(errors)} weitere Fehler')

        result = _console_result(success_count, total, errors, '\n'.join(output_lines))
        if write:
            result['rolled_back'] = False
        return result, written

    except Exception as e:
        return _console_undone(conn, write, total, [str(e)]), []
    finally:
        conn.close()


def handle_execute_sql(db: Database, post_data):
    """Handle SQL command execution – returns JSON with results.

    Lesemodus (Standard) oder Schreibmodus (``write`` = ``1``): siehe oben. Je Anfrage gelten ein Zeitlimit
    (_CONSOLE_TIME_LIMIT_S), Größengrenzen für Eingabe, Werte und Ausgabe (_CONSOLE_MAX_*) und eine Grenze für den
    belegten Plattenplatz; wird eine überschritten, bricht die Anfrage ab (im Schreibmodus mit Rollback). Es läuft
    höchstens eine Anfrage zur Zeit je Prozess. Diese Grenzen nicht aufweichen.

    Die Antwort trägt ``success_count``, ``total``, ``errors`` und ``output``; im Schreibmodus dazu ``rolled_back`` (nur,
    wenn die Transaktion lief) und, wenn die Änderungen sich nicht ins SQL-Protokoll schreiben ließen, ``warnings``.
    """
    write = post_data.get('write', [''])[0] == '1'
    sql = post_data.get('sql_commands', [''])[0]
    if not sql.strip():
        return json.dumps(_console_result(0, 0, ['Keine SQL-Befehle eingegeben.'], ''), ensure_ascii=False)
    try:
        commands = _console_statements(sql)
    except _ConsoleRefused as refused:
        return json.dumps(_console_result(0, 0, [str(refused)], ''), ensure_ascii=False)
    if not commands:
        return json.dumps(_console_result(0, 0, ['Keine SQL-Befehle eingegeben.'], ''), ensure_ascii=False)

    if not _console_lock.acquire(blocking=False):
        return json.dumps(_console_result(0, len(commands), [CONSOLE_BUSY], ''), ensure_ascii=False)
    try:
        result, written = _run_console(db, commands, write)
        if written and not _console_write_log(written):
            result['warnings'] = [CONSOLE_LOG_WARNING]
    finally:
        _console_lock.release()
    return json.dumps(result, ensure_ascii=False)


def _datev_number_args(post_data):
    """Berater- und Mandantennummer aus dem Formular einer Firma: ``(Beraternummer, Mandantennummer, Fehler)``.

    Nur das Formular der eigenen Firma führt die beiden Felder (``datev_consultant_no``, ``datev_client_no``); fehlt eines,
    ist der Wert ``None`` – die Datenbank lässt die gespeicherte Nummer dann, wie sie ist. Ein leeres Feld leert sie. Sonst
    zählen nur Ziffern (Berater 4–7, Mandant 1–5 Stellen); alles andere ist ein Klartext als ``Fehler`` und nichts wird
    geschrieben."""
    numbers = []
    for field, valid, error in (('datev_consultant_no', datev_keys.consultant_no, datev_keys.CONSULTANT_NO_ERROR),
                                ('datev_client_no', datev_keys.client_no, datev_keys.CLIENT_NO_ERROR)):
        if field not in post_data:
            numbers.append(None)
            continue
        value = post_data[field][0].strip()
        if value and valid(value) is None:
            return None, None, error
        numbers.append(value)
    return numbers[0], numbers[1], None


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
    consultant_no, client_no, number_error = _datev_number_args(post_data)
    if number_error:
        return 303, f'/masterdata/contacts?error={quote(number_error)}'

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
            datev_consultant_no = consultant_no,
            datev_client_no     = client_no,
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
    consultant_no, client_no, number_error = _datev_number_args(post_data)
    if number_error:
        return 303, f'/masterdata/contacts/edit?id={contact_id}&error={quote(number_error)}'

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
            datev_consultant_no = consultant_no,
            datev_client_no     = client_no,
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
    error = number_format_error(number_format)
    if error:
        return 303, f"/masterdata/numberranges?err={quote(error, safe='')}"

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
    error = number_format_error(number_format)
    if error:
        return 303, f"/masterdata/numberranges/edit?id={range_id}&err={quote(error, safe='')}"

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
    """Status einer Rechnung setzen – nach der Übergangstabelle ``db.invoice_lock.TRANSITIONS``.

    Der Wechsel läuft in ``Database.set_invoice_status`` in einer Transaktion: Status, Forderungsbuchung
    (beim Versenden und Reaktivieren gebucht, beim Zurückholen entfernt) und beim
    Zurückholen das Ablegen der versendeten Dokumente in „Fassungen“ gelingen gemeinsam oder gar nicht.
    Auf „storniert“ setzt diese Route nur eine Rechnung ohne positiven Betrag (Gutschrift, 0-€-Rechnung); jede andere
    wird über ``/invoice/cancel`` (``handle_invoice_cancel``) storniert, hier 409.
    Hier folgt danach nur das Erzeugen des Dokuments beim Versenden (Entwurf/abgeschlossen → versendet,
    oder eine stornierte Rechnung ohne Datei). Zwischen versendet und überfällig wird nichts gebucht
    und nichts erzeugt. Versendet die Route eine Ersatzrechnung, ziehen die Zahlungen der stornierten Rechnung um
    (in derselben Transaktion); die Antwort nennt in ``hint``, was geschah.

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
    if new_status not in STATUS_LABELS:
        return 400, '{"success": false, "error": "Ung\u00fcltiger Status"}'

    # Nur Rechnungen: ein Angebot wird hier nie versendet (und bekommt keine Rechnungs-PDF)
    if not db.get_document(invoice_id, 'invoice'):
        return 404, '{"success": false, "error": "Rechnung nicht gefunden"}'

    change = db.set_invoice_status(invoice_id, new_status, userctx.get_user())
    if not change.ok:
        return change.code, json.dumps({'success': False, 'error': change.message})
    print(f"Invoice {invoice_id} status updated to: {new_status}")

    result = {'success': True, 'invoice_id': invoice_id}
    if change.archived:
        result['hint'] = ('Die versendeten Dokumente (PDF/XML) liegen jetzt im Ordner „Fassungen“ '
                          'dieser Rechnung und bleiben dort erhalten.')
    if change.payment_hint:             # Versand einer Ersatzrechnung: was mit den Zahlungen der stornierten geschah
        result['hint'] = change.payment_hint
    warning = _ensure_sent_documents(db, invoice_id, change.old, new_status)
    if warning:
        result['warning'] = warning
    return 200, json.dumps(result)


def handle_invoice_cancel(post_body: bytes):
    """Eine versendete Rechnung stornieren (``POST /invoice/cancel``, JSON ``{"invoiceId", "date"}``).

    ``Database.cancel_invoice`` legt in einer Transaktion die Stornorechnung (eigene Nummer, negative Beträge,
    Status „versendet“) an, setzt die Rechnung auf „storniert“ und bucht – hat die App die Forderung gebucht – die
    Gegenbuchung dazu. Hier folgt danach nur das Dokument der Stornorechnung (PDF, bei E-Rechnungsdaten auch XML);
    scheitert es, bleibt die Stornierung gespeichert und die Antwort trägt eine ``warning`` – „PDF + E-Rechnung“
    erzeugt die fehlende Datei. ``hint`` sagt, dass es keine Gegenbuchung gab.

    ``invoiceId`` liest die Route streng als Ganzzahl (wie ``/invoice/save``), das Datum prüft die Datenbankschicht.

    ``reissue`` (Wahrheitswert, ohne Angabe ``false``; alles andere ist eine ungültige Anfrage, bevor etwas geschieht):
    Mit ``true`` legt die Route nach der Stornierung und dem Dokument der Stornorechnung den Entwurf der neuen Rechnung
    an (``Database.create_replacement_draft``, „Stornieren und neu ausstellen“) und nennt ihn als ``replacement_id``. Das ist eine
    zweite Transaktion: Scheitert sie, bleibt die Stornierung stehen, und die Antwort trägt dazu eine ``warning``.
    Returns tuple: (status_code, json_body)
    """
    data = _json_request(post_body)
    if data.get('invoiceId') is None:
        raise InvalidRequest('invoiceId fehlt')
    try:
        invoice_id = _invoice_id(data)
    except _SaveRefused as refusal:
        return refusal.status, json.dumps({'success': False, 'error': refusal.text})
    reissue = data.get('reissue', False)
    if not isinstance(reissue, bool):
        raise InvalidRequest('reissue ist kein Wahrheitswert')

    db = Database()
    storno = db.cancel_invoice(invoice_id, data.get('date'), userctx.get_user())
    if not storno.ok:
        return storno.code, json.dumps({'success': False, 'error': storno.message})
    print(f"Invoice {invoice_id} cancelled by credit note {storno.storno_number}")

    result = {'success': True, 'storno_id': storno.storno_id, 'storno_number': storno.storno_number}
    if not storno.counter_booked:
        result['hint'] = NO_COUNTER_BOOKING_HINT
    warning = _generate_storno_documents(db, storno.storno_id)
    if warning:
        result['warning'] = warning
    if reissue:
        replacement = db.create_replacement_draft(invoice_id, userctx.get_user())
        if replacement.ok:
            print(f"Invoice {invoice_id} replaced by draft {replacement.invoice_number}")
            result['replacement_id'] = replacement.invoice_id
        else:
            note = (f'Der Entwurf der neuen Rechnung wurde nicht angelegt: {replacement.message} – „Neu ausstellen“ '
                    'auf der Seite der stornierten Rechnung holt es nach.')
            result['warning'] = f"{result['warning']} {note}" if result.get('warning') else note
    return 200, json.dumps(result)


def handle_invoice_reissue(post_body: bytes):
    """Zu einer stornierten Rechnung den Entwurf der neuen Rechnung anlegen (``POST /invoice/reissue``, JSON
    ``{"invoiceId"}``, „Neu ausstellen“): ``Database.create_replacement_draft`` – die Daten der stornierten Rechnung,
    neue Nummer, heutiges Datum, Entwurf. Die Antwort nennt ihn (``replacement_id``, ``replacement_number``); eine
    Ablehnung trägt Statuscode und Klartext (``error``), und nichts wurde geschrieben. ``invoiceId`` liest die Route
    streng als Ganzzahl (wie ``/invoice/cancel``).

    Returns tuple: (status_code, json_body)
    """
    data = _json_request(post_body)
    if data.get('invoiceId') is None:
        raise InvalidRequest('invoiceId fehlt')
    try:
        invoice_id = _invoice_id(data)
    except _SaveRefused as refusal:
        return refusal.status, json.dumps({'success': False, 'error': refusal.text})

    db = Database()
    replacement = db.create_replacement_draft(invoice_id, userctx.get_user())
    if not replacement.ok:
        return replacement.code, json.dumps({'success': False, 'error': replacement.message})
    print(f"Invoice {invoice_id} replaced by draft {replacement.invoice_number}")
    return 200, json.dumps({'success': True, 'replacement_id': replacement.invoice_id,
                            'replacement_number': replacement.invoice_number})


def _generate_storno_documents(db, storno_id):
    """Nach dem Stornieren das PDF (+ XML) der Stornorechnung erzeugen. Returns einen Warntext, falls das nicht
    gelang; „PDF + E-Rechnung“ erzeugt die fehlende Datei dann (eine eingefrorene Rechnung ohne abgelegtes PDF
    darf erzeugt werden)."""
    from export.invoice_files import generate_invoice_documents
    try:
        result = generate_invoice_documents(db, storno_id, overwrite_sent=True)
    except Exception as e:
        import traceback
        print(f"Error generating documents for credit note {storno_id}: {e}")
        traceback.print_exc()
        return ('Die Stornorechnung ist gespeichert, aber das Dokument konnte nicht erstellt werden – '
                'bitte „PDF + E-Rechnung“ ausführen.')
    if result.get('error'):
        # Fehlen nur Angaben (``incomplete``), hilft ein erneutes „PDF + E-Rechnung“ nicht: Die Meldung nennt, was zu ergänzen ist
        retry = '' if result.get('incomplete') else ' Bitte „PDF + E-Rechnung“ ausführen.'
        return f"Die Stornorechnung ist gespeichert, aber: {result['error']}{retry}"
    return None


def _ensure_sent_documents(db, invoice_id, current_status, new_status):
    """Beim Versenden das PDF (+ XML) erzeugen (todo #2).

    Ab „versendet“ darf die Rechnung nicht mehr geändert werden – ab hier muss also das Dokument
    vorliegen, das der Kunde bekommt. Beim Eintritt aus Entwurf/abgeschlossen wird es neu
    geschrieben. Wird eine stornierte Rechnung wieder versendet, bleibt eine vorhandene Datei
    unangetastet und wird nur erzeugt, wenn sie fehlt. Alle übrigen Wechsel (versendet ↔ überfällig,
    derselbe Status noch einmal, Teilzahlung/bezahlt → versendet) erzeugen nichts.

    Returns:
        str | None: Warntext, falls das Dokument nicht erzeugt werden konnte.
    """
    if new_status != 'sent':
        return None

    from export.invoice_files import generate_invoice_documents, stored_invoice_path

    entering = current_status in EDITABLE_STATUSES
    if not entering and (current_status != 'cancelled' or stored_invoice_path(db, invoice_id, 'pdf')[0]):
        return None
    try:
        result = generate_invoice_documents(db, invoice_id, overwrite_sent=entering)
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
    sie, davor und nach dem Storno nicht (``RECEIVABLE_STATUSES``). Nur noch für die Demodaten
    (``demo_seed``) – der Statuswechsel der Oberfläche bucht und entfernt sie selbst in seiner
    Transaktion (``Database.set_invoice_status``). Nimmt eine Zuordnung die letzte Zahlung einer
    Rechnung weg, richtet ``Database._after_payment_removed`` die Forderung nach derselben Regel aus.

    Ein Fehler hier kippt den Aufrufer nicht – die Buchung lässt sich jederzeit nachziehen.
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

    Für eine Stornorechnung ist es die Erstattung: Die ausgehende Bankbewegung wird ihr zugeordnet
    (``Database.assign_storno_refund``, gekappt auf Guthaben und freien Rest der Bewegung; ``amount_paid`` ist
    der gewünschte Erstattungsbetrag). Vom Anzeige-Backfill der Zahlung (``_backfill_booking_skr``) bleibt nur
    der Kunde: Ohne ihn füllte die Bankmaske ihn beim ersten Speichern ein (sie zeigt den Kontakt der Sätze
    darunter), und „Öffnen und unverändert speichern“ änderte etwas. Erlöskonto, Steuer und Beleg-Nr. bekommt
    die Bankzeile einer Erstattung nicht – sie gehören zur Rechnung, nicht zur Bewegung.

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
        invoice = db.get_document(invoice_id, 'invoice')
        if is_storno_row(invoice):
            refund = db.assign_storno_refund(invoice_id, transaction_id, amount_paid, revenue_coa_id)
            ok, err = refund.ok, refund.message
            if ok:
                try:
                    db.backfill_booking_fields(transaction_id, contact_id=invoice[13])      # 13 = CustomerId
                except Exception as e:
                    # Zuordnung ist gespeichert; ein Backfill-Fehler darf sie nicht kippen
                    print(f"Error backfilling contact for booking {transaction_id}: {e}")
        else:
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
    except PaymentRemovalRefused as e:
        return 409, str(e)             # abgelehnt, nichts geändert (Guthaben würde negativ)
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


class JsonBody(bytes):
    """Body einer JSON-Antwort mit ihrem Statuscode (Vorgabe 200): ``handle_invoice_save`` liefert wie
    die übrigen Speichern-Routen den Body, ``app._post_json_route`` liest ``status`` dazu."""
    status = 200


def _json_body(payload, status=200):
    body = JsonBody(json.dumps(payload).encode())
    body.status = status
    return body


class _SaveRefused(Exception):
    """Das Speichern wird abgelehnt: ``text`` geht als Fehlermeldung an die Oberfläche."""

    def __init__(self, text, status=200):
        super().__init__(text)
        self.text = text
        self.status = status


def handle_invoice_save(post_body: bytes):
    """Save or update invoice to database

    Returns the JSON response (bytes) with invoice_id. Status und Rest (``AmountDue``) kommen nie vom
    Client: eine neue Rechnung ist ein Entwurf (oder abgeschlossen), jeder andere Status wird über
    ``/invoice/status`` gesetzt. Bei einer versendeten Rechnung lehnt ``Database.correct_invoice`` jede
    Änderung an Finanzangaben ab (``db.invoice_lock``): Antwort mit Statuscode 409; die änderbaren Felder
    schreibt sie als Berichtigung (neue Fassung, Protokoll).
    """
    data = _json_request(post_body)         # kein gültiges Objekt: InvalidRequest → 400 (app.py)
    try:
        return _save_invoice(data)
    except _SaveRefused as refusal:
        return _json_body({'success': False, 'error': refusal.text}, refusal.status)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _json_body({'success': False, 'error': _save_error_text(e)})


def _open_invoice_fields(data):
    """Die Felder einer versendeten oder stornierten Rechnung, die sich noch ändern lassen. Das
    Formular schickt für sie nur, was der Nutzer geändert hat (kein ``items``): Zahlungshinweis,
    Bestellnummer, Leitweg-ID, Leistungsdatum. Die Anschrift bleibt, wie sie steht – nur mit
    ``refreshAddress`` wird sie neu aus den Stammdaten desselben Kunden übernommen (das erledigt
    ``Database.correct_invoice``). Ein Entwurf oder eine unbekannte Rechnung wird so nicht gespeichert:
    Dort gilt das komplette Formular."""
    fields = {}
    for key, field in (('paymentTerms', 'payment_terms'), ('orderNumber', 'order_number'),
                       ('deliveryDate', 'delivery_date')):
        if key in data:
            fields[field] = data[key]
    if 'buyerReference' in data:
        fields['buyer_reference'] = fields['buyer_route_id'] = data['buyerReference']
    return fields


def _check_skonto(invoice_date, due_date, days, percent, gross):
    """Die Skonto-Angaben des Formulars prüfen (``export.einvoice_model.parse_skonto``); sonst :class:`_SaveRefused` mit dem
    Klartext. Skonto gibt es nur bei einem positiven Rechnungsbetrag (``gross``, Euro) und mit lesbarem Rechnungsdatum, dessen
    Frist sich darstellen lässt (``skonto_until`` rechnet immer, auch ohne Fälligkeit); endet die Frist nach der Fälligkeit, ist
    das ein Widerspruch."""
    from export.einvoice_model import (SKONTO_AFTER_DUE_MESSAGE, SKONTO_DATE_MESSAGE, SKONTO_POSITIVE_MESSAGE,
                                       parse_skonto, skonto_until)
    try:
        skonto = parse_skonto(days, percent)
    except ValueError as invalid:
        raise _SaveRefused(str(invalid)) from None
    if skonto is None:
        return
    if not gross > 0:
        raise _SaveRefused(SKONTO_POSITIVE_MESSAGE)
    try:
        start = datetime.date.fromisoformat(str(invoice_date or '')[:10])
    except ValueError:
        raise _SaveRefused(SKONTO_DATE_MESSAGE) from None
    until = skonto_until(start, skonto[0])
    if until is None:
        raise _SaveRefused(SKONTO_DATE_MESSAGE)
    try:
        due = datetime.date.fromisoformat(str(due_date or '')[:10])
    except ValueError:
        return                                   # ohne lesbare Fälligkeit gibt es nichts zu vergleichen
    if until > due:
        raise _SaveRefused(SKONTO_AFTER_DUE_MESSAGE)


def _invoice_fields(db, data, stored):
    """Kopf und Positionen aus dem Formular: ``(invoice_data, item_rows)``, Summen serverseitig
    exakt neu berechnet. Fehlt etwas, :class:`_SaveRefused`."""
    invoice_number = data.get('invoiceNumber')
    invoice_date = data.get('invoiceDate')
    customer_id = data.get('customerId')
    own_company_id = data.get('ownCompanyId')
    buyer_reference = data.get('buyerReference')
    payment_terms = data.get('paymentTerms')
    due_date = data.get('dueDate')
    bank_account_id = data.get('bankAccountId')
    tax_rate = data.get('taxRate')
    show_tax = data.get('showTax', True)   # False = Kleinunternehmer (§19), keine USt
    currency = data.get('currency', 'EUR')
    items = data.get('items', [])

    # XRechnung optional fields
    order_number = data.get('orderNumber')
    delivery_date = data.get('deliveryDate')
    discount_percentage = data.get('discountPercentage')
    discount_days = data.get('discountDays')

    # Validate required fields
    if not invoice_number or not invoice_date or not customer_id or not own_company_id:
        raise _SaveRefused('Pflichtfelder fehlen')

    if not items or len(items) == 0:
        raise _SaveRefused('Mindestens eine Position erforderlich')

    # Leistungsdatum ist Pflicht (§ 14 UStG). Bereits versendete Rechnungen
    # sind eingefroren und werden nicht nachträglich geprüft.
    frozen = stored is not None and status_group(stored[InvoiceCol.STATUS]) != 'editable'
    if not delivery_date and not frozen:
        raise _SaveRefused('Leistungsdatum fehlt – Pflichtangabe nach § 14 UStG '
                           '(Datum der Lieferung/Leistung).')

    # Phase 2: Summen serverseitig exakt neu berechnen – den vom Client
    # gelieferten net/tax/gross-Werten wird bewusst nicht vertraut. Bei
    # Kleinunternehmer (§19) wird ohne Steuer gerechnet (Brutto = Netto).
    net_amount, tax_amount, gross_amount, line_nets = recompute_invoice_totals(
        items, _rate_to_pct(tax_rate if show_tax else 0))

    # Skonto: Prozent und Tage zusammen und brauchbar, nur bei positivem Betrag, die Frist nicht nach der Fälligkeit. Eine
    # eingefrorene Rechnung wird nicht geprüft: Ihr Skonto ist gesperrt, und ein Altwert darf sie nicht an einer Berichtigung
    # hindern.
    if not frozen:
        _check_skonto(invoice_date, due_date, discount_days, discount_percentage, gross_amount)
    # Sentinel -1 in TaxRate kennzeichnet "kein USt-Ausweis" (vs. echte 0%).
    stored_tax_rate = tax_rate if show_tax else -1

    # Get customer data for invoice
    customer = db.get_contact_by_id(customer_id)
    if not customer:
        raise _SaveRefused('Kunde nicht gefunden')

    # Get own company for sender line
    own_company = db.get_contact_by_id(own_company_id)
    if not own_company:
        raise _SaveRefused('Eigene Firma nicht gefunden')

    # Get bank account data
    iban = None
    bic = None
    account = None
    if bank_account_id:
        account = db.get_account_by_id(bank_account_id)
        if account:
            iban = account[3]  # IBAN
            bic = account[4]  # BIC

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
        **(buyer_snapshot(customer) if not frozen else {}),
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
        'amount_due': gross_amount,   # vor dem Versenden gibt es keine Zahlung; danach rechnet es die Zahlungslogik
    }
    # Positionen (Positionssumme serverseitig berechnet)
    item_rows = [{
        'position': item.get('position'),
        'article_id': None,  # No article linkage for now
        'description': item.get('description'),
        'quantity': item.get('quantity'),
        'unit': item.get('unit', 'C62'),  # C62 = piece
        'price_per_unit': item.get('unitPrice'),
        'total_net': line_nets[idx],
        'tax_category': 'S',
        'tax_rate': item.get('taxRate', tax_rate),
    } for idx, item in enumerate(items)]
    return invoice_data, item_rows


def _invoice_id(data):
    """``invoiceId`` des Formulars als Ganzzahl; fehlt es (oder ist es ``null``), ist es eine neue Rechnung
    (``None``). Gelesen wird nur eine ganze Zahl – als Zahl, als ganzzahliger Bruch (``1.0``) oder als Text in
    Normalform (``"1"``). Alles andere (``1.5``, ``true``, ``"01"``, ``"1.0"``, ``" 1"``, ``""``) lehnt die Anfrage
    ab: Die Datenbank fände die Rechnung 1 auch damit, aber den Ordner ``Fassungen/1.0`` gibt es nicht – die
    Nummernsperre eines Bestands aus 2.1.2 griffe nicht."""
    value = data.get('invoiceId')
    if value is None:
        return None
    try:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        elif isinstance(value, str) and value.isascii() and value.isdigit() and str(int(value)) == value:
            value = int(value)
    except ValueError:                           # mehr Ziffern, als Python in eine Zahl wandelt
        value = None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _SaveRefused('Ungültige Rechnungs-ID', 400)
    return value


def _save_invoice(data):
    # Check if this is an update (invoiceId present) or new invoice
    invoice_id = _invoice_id(data)
    is_update = invoice_id is not None
    db = Database()
    # Der Stand vor dem Speichern bestimmt nur, welche Angaben das Formular mitbringen muss; ob
    # etwas gesperrt ist, entscheidet ``update_invoice_fields`` in der Transaktion des Schreibens
    stored = db.get_document(invoice_id, 'invoice') if is_update else None
    sent = stored is not None and status_group(stored[InvoiceCol.STATUS]) != 'editable'
    if sent and 'items' not in data:
        invoice_data, item_rows = _open_invoice_fields(data), None
    else:
        invoice_data, item_rows = _invoice_fields(db, data, stored)
    if sent:
        return _correct_invoice(db, invoice_id, invoice_data, item_rows, data)

    # Kopf und Positionen in EINER Transaktion: scheitert ein Schritt, bleibt
    # nichts halb gespeichert zurück – kein Kopf ohne Positionen (der nächste
    # Versuch scheiterte sonst an der schon vergebenen Rechnungs-Nr.), keine gelöschten
    # Positionen hinter einem gescheiterten Update und keine verbrauchte Rechnungsnummer.
    try:
        with db.transaction() as tx:
            if is_update:
                db.update_invoice_fields(tx, invoice_id, invoice_data, item_rows)
            else:
                # Die Nummer wird beim ersten Speichern vergeben – auch als Entwurf. Ist die Nummer des
                # Formulars noch der Vorschlag, den es beim Öffnen bekam (``numberSuggestion``), zählt sie
                # nicht als eigene: Ein zweites Fenster kann ihn inzwischen verbraucht haben, vergeben wird
                # frisch, und die Antwort nennt die Nummer, die gilt.
                requested = invoice_data['invoice_number']
                if requested == data.get('numberSuggestion'):
                    requested = None
                invoice_data['invoice_number'] = db.allocate_invoice_number(
                    tx.conn, invoice_data['invoice_date'], requested)
                invoice_data['status'] = 'finalized' if data.get('status') == 'finalized' else 'draft'
                invoice_id = db.insert_invoice(invoice_data, tx=tx)
                for item in item_rows:
                    db.insert_invoice_item({**item, 'invoice_id': invoice_id}, tx=tx)
    except InvoiceLocked as locked:
        raise _SaveRefused(str(locked), 409) from None
    except (DocumentNotFound, NumberRangeError) as refusal:
        raise _SaveRefused(str(refusal)) from None
    result = {'success': True, 'invoice_id': invoice_id}
    if not is_update:
        result['invoice_number'] = invoice_data['invoice_number']
    return _json_body(result)


def _correct_invoice(db, invoice_id, invoice_data, item_rows, data):
    """Das Speichern einer versendeten (oder stornierten) Rechnung ist eine Berichtigung
    (``Database.correct_invoice``): Ohne Abweichung vom Gespeicherten geschieht nichts; sonst legt sie in
    einer Transaktion die neue Fassung und das Protokoll an. ``documentVersion`` ist die Fassung, die die Maske
    geladen hat – fehlt sie oder ist sie veraltet, wird abgelehnt (409). Danach erzeugt dieser Weg das
    Dokument der neuen Fassung, wenn es eines abgelegt gab; scheitert das, bleibt die Berichtigung gespeichert
    und die Antwort trägt eine Warnung."""
    try:
        correction = db.correct_invoice(
            invoice_id, invoice_data, data.get('documentVersion'), userctx.get_user(),
            refresh_address=data.get('refreshAddress') is True, items=item_rows)
    except InvoiceLocked as locked:
        raise _SaveRefused(str(locked), 409) from None
    except CorrectionInvalid as invalid:                # vor jedem allgemeinen ValueError: eine ungültige Angabe, 400
        raise _SaveRefused(str(invalid), 400) from None
    except DocumentNotFound as refusal:
        raise _SaveRefused(str(refusal)) from None
    result = {'success': True, 'invoice_id': invoice_id}
    if correction.corrected:
        result['documentVersion'] = correction.version
        if correction.archived:
            warning = _generate_corrected_documents(db, invoice_id)
            if warning:
                result['warning'] = warning
    return _json_body(result)


def _generate_corrected_documents(db, invoice_id):
    """Nach einer Berichtigung das PDF (+ XML) der neuen Fassung erzeugen – die bisherigen liegen in
    „Fassungen“. Returns einen Warntext, falls das nicht gelang; „PDF + E-Rechnung“ erzeugt die fehlende Datei
    dann (eine eingefrorene Rechnung ohne abgelegtes PDF darf erzeugt werden)."""
    from export.invoice_files import generate_invoice_documents
    try:
        result = generate_invoice_documents(db, invoice_id, overwrite_sent=True)
    except Exception as e:
        import traceback
        print(f"Error generating documents for corrected invoice {invoice_id}: {e}")
        traceback.print_exc()
        return ('Die Berichtigung ist gespeichert, aber das neue Dokument konnte nicht erstellt werden – '
                'bitte „PDF + E-Rechnung“ ausführen.')
    if result.get('error'):
        retry = '' if result.get('incomplete') else ' Bitte „PDF + E-Rechnung“ ausführen.'
        return f"Die Berichtigung ist gespeichert, aber: {result['error']}{retry}"
    return None


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
            message_text=message_text,
            storno=is_storno_row(invoice)
        )
        
        if success:
            return json.dumps({'success': True}).encode()
        else:
            return json.dumps({'success': False, 'error': error}).encode()
            
    except Exception:
        import traceback
        traceback.print_exc()                       # der volle Fehler nur ins Server-Log, nie in die Antwort
        return json.dumps({'success': False, 'error': 'Fehler beim E-Mail-Versand.'}).encode()


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

    if parent_id:
        # Eine Erweiterung gehört zu einer vorhandenen Hauptanlage, nicht zu einer anderen Erweiterung –
        # sonst fehlte ihr Betrag im Plan (``Database.counts_toward_parent``). Die Meldung geht wie bei jeder
        # abgelehnten Aktion der Anlagenseite als ``err`` an die Seite der Anlage (``/assets/view``)
        parent = db.get_asset_by_id(int(parent_id))
        refusal = ('Die Hauptanlage gibt es nicht.' if parent is None
                   else 'Eine Erweiterung kann keine eigene Erweiterung bekommen.' if parent[19]  # Parent_ID
                   else None)
        if refusal:
            return 303, f'/assets/view?id={int(parent_id)}&err={quote(refusal, safe="")}'

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

    if status != 'active' and db.counts_toward_parent(asset_id):
        # Eine Erweiterung hat keinen eigenen Abgang (``Database.sell_asset``) und damit auch keinen eigenen
        # Status: nichts wird geschrieben, die Meldung geht wie bei jeder abgelehnten Aktion als ``err`` an die Seite
        return 303, (f'/assets/view?id={asset_id}&err='
                     + quote('Eine Erweiterung behält den Status der Hauptanlage.', safe=''))

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

    try:
        db.sell_asset(asset_id, sale_date, sale_price)
    except ValueError as exc:                     # eine Erweiterung bucht keinen eigenen Abgang
        return 303, f'/assets/view?id={asset_id}&err={quote(str(exc), safe="")}'
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


def handle_invoice_version_download(invoice_id: int, filename, year=None):
    """Eine abgelegte Fassung (PDF/XML aus ``Fassungen/<ID>``) zum Download laden – rein lesend.

    ``filename`` ist der Dateiname aus der Anfrage; er muss eine Datei im Fassungen-Ordner genau dieser
    Rechnung sein (``export.invoice_files.version_file_path``), die ID eines Angebots oder einer anderen
    Rechnung liefert nichts. ``year`` (der Jahresordner) trennt gleiche Namen in zwei Jahresordnern: Ist er
    angegeben, muss die Datei in genau diesem Ordner liegen, sonst gilt der erste Treffer.

    Returns:
        tuple: ``(bytes, dateiname, fehlertext)``; bei Erfolg ist der Fehlertext ``None``.
    """
    from export.invoice_files import version_file_path

    db = Database()
    try:
        if db.get_document(invoice_id, 'invoice') is None:
            return None, None, 'Rechnung nicht gefunden.'
        path = version_file_path(invoice_id, filename, year)
        if not path:
            return None, None, 'Diese Fassung gibt es nicht.'
        with open(path, 'rb') as f:
            return f.read(), os.path.basename(path), None
    except Exception as e:
        import traceback
        print(f"Error reading version {filename!r} of invoice {invoice_id}: {e}")
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
        except (DocumentNotFound, QuoteStatusLocked) as e:
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
            WHERE ID=? AND DocumentType='quote' AND ''' + QUOTE_STAYS_CONVERTED_SQL + ''' 
        ''', (
            doc['invoice_number'], doc['invoice_date'], doc['own_company_id'],
            doc['seller_name'], doc['seller_company'], doc['seller_street'], doc['seller_postal_code'], doc['seller_city'], doc['seller_country'],
            doc['seller_vat_id'], doc['seller_email'], doc['seller_phone'],
            doc['customer_id'], doc['buyer_name'], doc['buyer_company'], doc['buyer_street'], doc['buyer_postal_code'], doc['buyer_city'], doc['buyer_country'], doc['buyer_vat_id'],
            doc['currency'], doc['tax_category'], doc['tax_rate'],
            to_minor(doc['sum_net'] or 0), to_minor(doc['tax_amount'] or 0),
            to_minor(doc['sum_gross'] or 0), to_minor(doc['amount_due'] or 0),
            doc['status'], doc['valid_until'], doc['intro_text'], doc['closing_text'],
            quote_id, doc['status'],
        ))
    if cursor.rowcount == 0:
        # Es gibt das Angebot, aber es soll den Status „umgewandelt“ verlassen, obwohl eine Rechnung daraus besteht
        if tx.conn.execute("SELECT 1 FROM Invoices WHERE ID = ? AND DocumentType = 'quote'",
                           (quote_id,)).fetchone():
            raise QuoteStatusLocked(QUOTE_STAYS_CONVERTED_MESSAGE)
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
    try:
        db.set_quote_status(quote_id, new_status)
    except DocumentNotFound:
        return 404, '{"success": false, "error": "Angebot nicht gefunden"}'
    except QuoteStatusLocked as refusal:
        return 409, json.dumps({'success': False, 'error': str(refusal)})
    return 200, f'{{"success": true, "quote_id": {quote_id}}}'


def handle_convert_quote_to_invoice(post_data):
    """Angebot in Rechnung umwandeln. Returns (303, redirect_path).

    Ein Angebot wird nur einmal umgewandelt; versucht es jemand ein zweites Mal (oder ist es
    abgelehnt), geht die Weiterleitung mit dem Grund als ``err`` zurück auf das Angebot."""
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
    except (QuoteNotConvertible, NumberRangeError) as refusal:
        return 303, f"/quote?id={int(quote_id)}&err={quote(str(refusal), safe='')}"
    except Exception as e:
        print(f"Error converting quote {quote_id}: {e}")
        return 303, f"/quote?id={quote_id}"


def handle_delete_invoice(db: Database, invoice_id):
    """Entwurf löschen (``POST /invoice/delete``). Returns (status_code, Weiterleitung bzw. Text).

    Gelöscht wird nur ein Entwurf ohne Zahlung und Forderung (``Database.delete_draft_invoice``);
    sonst geht die Weiterleitung mit dem Grund als ``err`` zurück auf die Rechnung. Die abgelegten
    Dokumente des Entwurfs räumt erst der erfolgreiche Löschvorgang weg – nur im Dokumentordner und
    nur, wenn kein anderer Beleg auf sie verweist. Die ID eines Angebots ergibt 404."""
    from export.invoice_files import remove_document_file, stored_invoice_path

    invoice = db.get_document(invoice_id, 'invoice')
    if invoice is None:
        return 404, 'Rechnung nicht gefunden.'
    paths = [stored_invoice_path(db, invoice_id, kind, invoice)[0] for kind in ('pdf', 'xml')]
    try:
        db.delete_draft_invoice(invoice_id)
    except DocumentNotFound:
        return 404, 'Rechnung nicht gefunden.'
    except InvoiceNotDeletable as refusal:
        return 303, f"/invoice?id={invoice_id}&err={quote(str(refusal), safe='')}"
    for path in paths:
        if path:
            remove_document_file(path, db, invoice_id)
    return 303, "/invoice"


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
from .pages_worktime import DAY_MINUTES, clock_minutes
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


def _wt_span(start, end):
    """(Beginn, Ende) in Minuten ab Mitternacht des Eintragsdatums, oder None ohne gültige Zeiten.
    Ein Ende vor dem Beginn liegt am Folgetag (wie in ``compute_minutes``)."""
    start_min, end_min = clock_minutes(start), clock_minutes(end)
    if start_min is None or end_min is None:
        return None
    return start_min, (end_min + DAY_MINUTES if end_min < start_min else end_min)


def _wt_has_overlap(db: Database, person_id, fields, exclude_id=None):
    """True, wenn der Eintrag zeitlich mit einem bestehenden Arbeitseintrag desselben
    Tags/derselben Person kollidiert. Urlaub/Feiertag lösen keine Sperre aus und
    kollidieren auch nicht mit anderen Einträgen. Verglichen wird auf der Zeitachse des
    Eintragsdatums; Einträge der Nachbartage, in die ein Eintrag über Mitternacht hineinreicht,
    prüft das nicht.
    """
    if fields['kind'] in _WT_NO_LOCK_KINDS:
        return False
    new = _wt_span(fields['start_time'], fields['end_time'])
    if new is None:
        return False  # ohne Zeiten keine Überschneidung prüfbar
    for e in db.fetch_worktimes(person_id, fields['date'], fields['date']):
        if exclude_id and e[0] == exclude_id:
            continue
        if (e[3] or 'work') in _WT_NO_LOCK_KINDS:
            continue
        other = _wt_span(e[5], e[6])
        if other is None:
            continue
        if new[0] < other[1] and other[0] < new[1]:        # echte Zeitüberschneidung
            return True
    return False


_WT_TIME_ERROR = 'Bitte die Uhrzeit als HH:MM angeben.'


def _wt_times_valid(fields):
    """Start- und Endzeit sind leer oder eine gültige Uhrzeit (``clock_minutes``)."""
    return all(not fields[key] or clock_minutes(fields[key]) is not None
               for key in ('start_time', 'end_time'))


def _wt_error_redirect(person_id, date_from, date_to, text):
    """Zurück zur Seite, die ``text`` über ``?error=`` als Meldung zeigt; nichts ist gespeichert."""
    return 303, (f"/worktime?person={quote(str(person_id), safe='')}"
                 f"{_period_args(date_from, date_to)}&error={quote(text)}")


def _wt_overlap_redirect(person_id, date_from, date_to, date):
    return _wt_error_redirect(person_id, date_from, date_to,
                              f"Überschneidung am {date}: in diesem Zeitraum existiert bereits ein Eintrag.")


def handle_add_worktime(db: Database, post_data):
    """Neuen Arbeitszeit-Eintrag anlegen (mit Überschneidungssperre)."""
    g = lambda k, d='': post_data.get(k, [d])[0]
    person_id = int(g('person_id', '0') or 0)
    date_from = g('from', '')
    date_to = g('to', '')
    fields = _wt_parse(db, post_data)
    if person_id and fields['date']:
        if not _wt_times_valid(fields):
            return _wt_error_redirect(person_id, date_from, date_to, _WT_TIME_ERROR)
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
        if not _wt_times_valid(fields):
            return _wt_error_redirect(person_id, date_from, date_to, _WT_TIME_ERROR)
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
# Das Fahrtenbuch bleibt locker (einzelne Fahrten, ohne Tachostand, in beliebiger Reihenfolge nachgetragen): Beim
# Speichern wird nie ein fehlender Anschluss an die Vorfahrt geprüft – abgelehnt wird nur, was unmöglich ist.
# Lückenlos muss es nur bei der Fahrtenbuchmethode sein; die Prüfung dafür gibt es auf Wunsch (``trip_logbook_check``).
TRIP_TEXT_MAX = 500
TRIP_KM_MAX = 9_999_999
_TRIP_NOT_SAVED = 'Die Fahrt konnte nicht gespeichert werden (Fahrer oder Beleg gibt es nicht mehr).'
_TRIP_GONE = 'Die Fahrt gibt es nicht mehr.'


def _trip_redirect(person_id, date_from, date_to, error=None):
    """Zurück zur Fahrtenseite (Person und Zeitraum bleiben); ``error`` zeigt die Seite als Meldung."""
    location = f"/trips?person={quote(str(person_id or 0), safe='')}{_period_args(date_from, date_to)}"
    return location + f"&error={quote(error, safe='')}" if error else location


def _trip_id(value):
    """Eine ID aus dem Formular: positive ganze Zahl aus ASCII-Ziffern, die in 64 Bit passt. Alles andere – leer,
    Text, 0, negativ, zu groß – gilt als fehlend (``None``)."""
    text = (value or '').strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 19:
        return None
    number = int(text)
    return number if 1 <= number <= 2 ** 63 - 1 else None


def _trip_km(text):
    """Tachostand oder Kilometer: leer → ``None``, sonst eine ganze Zahl aus ASCII-Ziffern von 0 bis ``TRIP_KM_MAX``
    (``ValueError`` bei allem anderen – auch negativ, Dezimalkomma, Text, zu groß)."""
    if text == '':
        return None
    digits = text.lstrip('0')
    # die Länge zuerst: Eine Ziffernfolge von tausenden Stellen soll nicht erst in eine Zahl umgerechnet werden
    if not (text.isascii() and text.isdigit()) or len(digits) > len(str(TRIP_KM_MAX)):
        raise ValueError(text)
    number = int(digits or '0')
    if number > TRIP_KM_MAX:
        raise ValueError(text)
    return number


def _trip_time_ok(text):
    """``HH:MM`` mit 00–23 und 00–59, nur ASCII-Ziffern."""
    hours, colon, minutes = text[:2], text[2:3], text[3:]
    return (len(text) == 5 and colon == ':' and (hours + minutes).isascii() and (hours + minutes).isdigit()
            and int(hours) < 24 and int(minutes) < 60)


def _trip_fields(post_data):
    """Die Felder einer Fahrt aus dem POST-Body (ohne Fahrer und Zeitraum), geprüft: ``(fields, None)``, oder
    ``(None, Klartext)`` beim ersten Verstoß – dann wird nichts gespeichert. Eine reine Funktion für Anlegen und Ändern.

    Abgelehnt wird nur, was unmöglich ist (kein Datum, Ende vor Start, Tacho Ende unter Tacho Start, Kilometer, die
    keine Zahl sind …). Sind beide Tachostände gesetzt, ist die Differenz die gefahrene Strecke – auch ohne
    mitgesendeten Client-Wert."""
    g = lambda k: post_data.get(k, [''])[0].strip()
    if _trip_id(g('person_id')) is None or not g('start_date') or not g('destination'):
        return None, 'Bitte Fahrer, Start-Datum und Ziel angeben.'
    start_date, end_date = g('start_date'), g('end_date')
    if parse_iso_date(start_date) is None:
        return None, 'Bitte ein gültiges Start-Datum angeben.'
    if end_date and parse_iso_date(end_date) is None:
        return None, 'Bitte ein gültiges End-Datum angeben.'
    start_time, end_time = g('start_time'), g('end_time')
    if not all(t == '' or _trip_time_ok(t) for t in (start_time, end_time)):
        return None, 'Bitte die Uhrzeit als HH:MM angeben.'
    # ISO-Daten und HH:MM sortieren wie Zeitpunkte; ein leeres End-Datum heißt: derselbe Tag
    if ((end_date and end_date < start_date)
            or (end_date in ('', start_date) and start_time and end_time and end_time < start_time)):
        return None, 'Das Ende der Fahrt liegt vor dem Start.'
    try:
        start_km, end_km, distance_km = (_trip_km(g(name)) for name in ('start_km', 'end_km', 'distance_km'))
    except ValueError:
        return None, 'Kilometer bitte als ganze Zahl ab 0 angeben.'
    if start_km is not None and end_km is not None:
        if end_km < start_km:
            return None, 'Tacho Ende liegt unter Tacho Start.'
        distance_km = end_km - start_km
    document_id = _trip_id(g('document_id'))
    if g('document_id') and document_id is None:
        return None, 'Der gewählte Beleg ist ungültig.'
    texts = {name: g(name) for name in ('start_point', 'destination', 'vehicle', 'reason')}
    if any(len(text) > TRIP_TEXT_MAX for text in texts.values()):
        return None, f'Der Text ist zu lang (höchstens {TRIP_TEXT_MAX} Zeichen).'
    return {
        'start_date': start_date, 'start_time': start_time, 'end_date': end_date, 'end_time': end_time,
        **texts, 'distance_km': distance_km, 'start_km': start_km, 'end_km': end_km, 'document_id': document_id,
    }, None


def handle_add_trip(db: Database, post_data):
    """Neue Fahrt anlegen (benötigt Fahrer, Start-Datum und Ziel). Eine unmögliche Angabe oder ein Speicherfehler
    kommt als Meldung (``error``) auf die Fahrtenseite; gespeichert wird dann nichts."""
    g = lambda k: post_data.get(k, [''])[0]
    person_id = _trip_id(g('person_id'))
    fields, error = _trip_fields(post_data)
    if error is None and db.insert_trip(driver_id=person_id, **fields) is None:
        error = _TRIP_NOT_SAVED
    return 303, _trip_redirect(person_id, g('from'), g('to'), error)


def handle_update_trip(db: Database, post_data):
    """Bestehende Fahrt aktualisieren; Meldungen wie bei ``handle_add_trip``."""
    g = lambda k: post_data.get(k, [''])[0]
    person_id, trip_id = _trip_id(g('person_id')), _trip_id(g('id'))
    fields, error = _trip_fields(post_data)
    if error is None:
        if trip_id is None:
            error = _TRIP_GONE
        elif not db.update_trip(trip_id, **fields):
            error = _TRIP_GONE if db.get_trip_by_id(trip_id) is None else _TRIP_NOT_SAVED
    return 303, _trip_redirect(person_id, g('from'), g('to'), error)


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


class LoginOutcome(NamedTuple):
    """Ergebnis von ``handle_login``. ``status`` ist der HTTP-Status der Antwort (200 bei falschen Angaben,
    429 gesperrt, 503 ausgelastet), ``retry_after`` die Sekunden für den Kopf ``Retry-After`` (0: keiner)."""
    ok: bool
    username: str
    error: str | None
    status: int
    retry_after: int


def _wait_text(seconds):
    """Wartezeit für den Nutzer: bis 59 Sekunden in Sekunden, darüber auf Minuten aufgerundet."""
    if seconds < 60:
        return f"{seconds} Sekunde" if seconds == 1 else f"{seconds} Sekunden"
    minutes = math.ceil(seconds / 60)
    return f"{minutes} Minute" if minutes == 1 else f"{minutes} Minuten"


def handle_login(post_data, client=''):
    """Anmeldedaten prüfen, gedrosselt (``auth.attempt_login``). ``client`` ist die Adresse des Aufrufers,
    nur für die Zeile im Server-Log, wenn eine Sperre beginnt. Returns ``LoginOutcome``."""
    g = lambda k: post_data.get(k, [''])[0]
    username = g('username').strip()
    password = g('password')

    def log_block(seconds):
        # Nie mit dem eingegebenen Namen: Dort steht gelegentlich ein Passwort.
        print(f"Anmeldung gesperrt: {seconds} s nach zu vielen Fehlversuchen (Client {client or 'unbekannt'}).")

    result, wait = auth.attempt_login(username, password, on_block=log_block)
    if result == 'ok':
        return LoginOutcome(True, username, None, 200, 0)
    if result == 'blocked':
        return LoginOutcome(False, username, 'Zu viele fehlgeschlagene Anmeldeversuche. '
                            f'Bitte in {_wait_text(wait)} erneut versuchen.', 429, wait)
    if result == 'busy':
        return LoginOutcome(False, username, 'Der Server ist gerade ausgelastet. '
                            'Bitte gleich noch einmal versuchen.', 503, 5)
    return LoginOutcome(False, username, "Benutzername oder Passwort falsch.", 200, 0)


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
    info = f'Benutzer „{username}" angelegt.'
    try:
        has_data = bool(os.listdir(os.path.join(userctx.DATA_ROOT, userctx.USERS_DIR, username)))
    except OSError:                    # kein Datenverzeichnis (oder nicht lesbar): nichts zu melden
        has_data = False
    if has_data:
        info += (' Für diesen Namen gibt es bereits ein Datenverzeichnis – '
                 'der Benutzer arbeitet mit diesen Daten weiter.')
    return _users_redirect(info=info)


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


def handle_user_reset_password(post_data, session_token=None):
    """Passwort eines Benutzers setzen; seine Sitzungen enden. Nur wer sein eigenes Passwort ändert, behält
    die Sitzung ``session_token`` (die, mit der er gerade angemeldet ist)."""
    g = lambda k: post_data.get(k, [''])[0]
    username = g('username').strip()
    password = g('password')
    if not auth.user_exists(username):
        return _users_redirect(err='Benutzer existiert nicht.')
    keep = session_token if username == userctx.get_user() else None
    try:
        auth.set_password(username, password, keep_session=keep)
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
        auth.delete_all_sessions()
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
        auth.delete_all_sessions()      # die neue Sitzung von „active“ legt der Aufrufer erst danach an
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
