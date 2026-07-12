# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: wiso_import."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class WisoImportMixin:
    def import_wiso_csv(self, csv_bytes: bytes) -> dict:
        """WISO Mein Büro CSV-Export in die Bookings-Tabelle importieren.
        
        Unterstützt zwei Formate:
        
        1. Original-Export (9 Spalten):
           ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER
           
        2. Tabellen-Export (6 Spalten):
           Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr.;Betrag
           
        Format wird automatisch erkannt. Tabellen-Export aktualisiert bestehende Buchungen.

        Returns:
            dict: {imported: int, updated: int, skipped: int, errors: list[str], format: str}
        """
        import csv, io, datetime

        # Encoding-Erkennung: CP1252 zuerst, dann Fallback
        text = None
        for enc in ('cp1252', 'utf-8-sig', 'utf-8', 'latin-1'):
            try:
                text = csv_bytes.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            return {'imported': 0, 'updated': 0, 'skipped': 0,
                    'errors': ['Encoding der Datei nicht erkennbar'], 'format': 'unknown'}
        
        # Format-Erkennung über Header-Zeile
        # Anführungszeichen und Whitespace aus den Spaltenbezeichnungen entfernen
        first_line = text.split('\n')[0].strip()
        headers = [h.strip().strip('"').strip("'") for h in first_line.split(';')]
        
        # Tabellen-Format erkennen (Empf./Auft. + Verwendungszweck vorhanden)
        if any('Empf' in h or 'Auft' in h for h in headers) and any('Verwendungszweck' in h for h in headers):
            return self._import_wiso_table_format(text)
        # Original-Format erkennen (KONTO + GEGENKONTO vorhanden)
        elif 'KONTO' in headers and 'GEGENKONTO' in headers:
            return self._import_wiso_original_format(text)
        else:
            return {'imported': 0, 'updated': 0, 'skipped': 0,
                    'errors': [f'Unbekanntes Format. Gefundene Spalten: {", ".join(headers)}'], 
                    'format': 'unknown'}
    def _import_wiso_original_format(self, text: str) -> dict:
        """Import des Original WISO-Exports (9 Spalten).
        
        CSV-Spalten:
            ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER

        Mapping:
            KONTO      → ChartOfAccounts.AccountNumber → COA_ID
            GEGENKONTO → ChartOfAccounts.AccountNumber → CounterCOA_ID
            SCHLUESSEL → BU-Schlüssel → TaxRate (401=19%, 402=7%, 121=0%)

        Returns:
            dict: {imported: int, updated: int, skipped: int, errors: list[str]}
        """
        import csv, io, datetime

        # BU-Schlüssel → Steuersatz aus DB-Tabelle laden
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT Code, TaxRate FROM TaxKeys')
        BU_TO_TAXRATE = {row[0]: row[1] for row in cursor.fetchall()}

        # Lookup-Maps einmalig aufbauen
        cursor.execute('SELECT AccountNumber, ID FROM ChartOfAccounts')
        coa_map = {row[0]: row[1] for row in cursor.fetchall()}

        # Bankkonten-SKR-Nummern für Vorzeichen-Logik
        cursor.execute('SELECT SKRAccount FROM Accounts WHERE SKRAccount IS NOT NULL')
        liquid_account_nrs = {row[0] for row in cursor.fetchall()}
        conn.close()

        reader = csv.DictReader(io.StringIO(text), delimiter=';', quotechar='"')
        # Spaltennamen normalisieren (führende/nachgestellte Leerzeichen entfernen)
        if reader.fieldnames:
            reader.fieldnames = [f.strip() for f in reader.fieldnames]
        imported = 0
        updated = 0
        skipped = 0
        skipped_rows = []       # Liste übersprungener Zeilen mit Details
        missing_coa = set()     # SKR-Kontonummern (KONTO), die nicht in ChartOfAccounts gefunden wurden
        missing_counter_coa = set()  # SKR-Kontonummern (GEGENKONTO), die nicht in ChartOfAccounts gefunden wurden
        errors = []

        def _is_liquid(nr):
            """Prüft ob die SKR-Nummer ein liquides Konto ist (Bank/Kasse/Verrechnungskonto)."""
            return nr is not None and (
                nr in liquid_account_nrs  # aus Accounts-Tabelle (z.B. 1810)
                or 1000 <= nr <= 1099     # Kasse (SKR04)
                or nr == 1460             # Verrechnungskonto
            )

        # ── Pass 1: alle Zeilen parsen ───────────────────────────────────────
        parsed_rows = []  # list of dicts
        for i, row in enumerate(reader, 1):
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            try:
                date_str = row.get('DATUM', '').strip()[:10]
                try:
                    booking_date = datetime.datetime.strptime(date_str, '%d.%m.%Y').strftime('%Y-%m-%d')
                except ValueError:
                    errors.append(f"Zeile {i}: Ungültiges Datum '{date_str}'")
                    continue

                amount_str = row.get('BRUTTOBETRAG', '').strip()
                if ',' in amount_str:
                    amount_str = amount_str.replace('.', '').replace(',', '.')
                try:
                    amount = float(amount_str)
                except ValueError:
                    errors.append(f"Zeile {i}: Ungültiger Betrag '{amount_str}'")
                    continue

                konto_str = row.get('KONTO', '').strip()
                try:
                    konto_nr = int(konto_str) if konto_str else None
                    coa_id = coa_map.get(konto_nr) if konto_nr is not None else None
                    if konto_nr is not None and coa_id is None:
                        missing_coa.add(konto_nr)
                except (ValueError, TypeError):
                    coa_id = None
                    konto_nr = None

                gegenkonto_str = row.get('GEGENKONTO', '').strip()
                try:
                    gegenkonto_nr = int(gegenkonto_str) if gegenkonto_str else None
                    counter_coa_id = coa_map.get(gegenkonto_nr) if gegenkonto_nr is not None else None
                    if gegenkonto_nr is not None and counter_coa_id is None:
                        missing_counter_coa.add(gegenkonto_nr)
                except (ValueError, TypeError):
                    counter_coa_id = None
                    gegenkonto_nr = None

                # Vorzeichen: GEGENKONTO = liquides Konto → Abgang (negativ)
                #             KONTO      = liquides Konto → Zugang (positiv)
                if _is_liquid(gegenkonto_nr) and not _is_liquid(konto_nr):
                    amount = -abs(amount)
                elif _is_liquid(konto_nr) and not _is_liquid(gegenkonto_nr):
                    amount = abs(amount)

                schluessel = row.get('SCHLUESSEL', '').strip()
                tax_rate = BU_TO_TAXRATE.get(schluessel)
                # 4405→4400 Umbuchung: implizit 19% USt, auch ohne BU-Schlüssel
                if tax_rate is None and konto_nr == 4405 and gegenkonto_nr == 4400:
                    tax_rate = 0.19
                # Steuerbetrag berechnen (Brutto → MwSt-Anteil)
                tax_amount = None
                if tax_rate is not None and tax_rate > 0 and amount != 0:
                    tax_amount = round(abs(amount) - abs(amount) / (1 + tax_rate), 2)
                    if amount < 0:
                        tax_amount = -tax_amount
                text_val = row.get('TEXT', '').strip()
                doc_number = row.get('REFERENZNUMMER', '').strip()

                parsed_rows.append({
                    'zeile': i, 'date': booking_date, 'amount': amount,
                    'coa_id': coa_id, 'counter_coa_id': counter_coa_id,
                    'konto_nr': konto_nr, 'konto_str': konto_str,
                    'tax_rate': tax_rate, 'tax_amount': tax_amount,
                    'text': text_val, 'doc': doc_number,
                })
            except Exception as e:
                errors.append(f"Zeile {i}: {str(e)}")

        # ── Pass 2: Split-Gruppen erkennen (gleiche REFERENZNUMMER + Datum) ──
        # Mehrere Zeilen mit gleicher Referenz = eine Kontoauszugs-Buchung
        # wurde buchhalterisch auf mehrere Konten aufgeteilt.
        from collections import defaultdict
        ref_groups = defaultdict(list)
        for pr in parsed_rows:
            key = (pr['doc'], pr['date']) if pr['doc'] else None
            if key:
                ref_groups[key].append(pr)

        # booking_group_id_for_key: key → int (wird bei Bedarf angelegt)
        group_id_cache = {}
        reused_group_ids = set()  # bestehende Gruppen, deren TotalAmount am Ende neu berechnet wird

        dup_conn = self._get_connection()
        dup_cur  = dup_conn.cursor()

        def _get_or_create_group(key, total_amount, date):
            if key in group_id_cache:
                return group_id_cache[key]
            # Bestehende Gruppe wiederverwenden, falls Teile des Splits schon
            # in einem früheren Import gelandet sind (key = (doc_number, date))
            dup_cur.execute(
                'SELECT BookingGroup_ID FROM Bookings '
                'WHERE DocumentNumber=? AND DateBooking=? AND BookingGroup_ID IS NOT NULL '
                'LIMIT 1', key
            )
            existing = dup_cur.fetchone()
            if existing:
                group_id_cache[key] = existing[0]
                reused_group_ids.add(existing[0])
                return existing[0]
            dup_cur.execute(
                'INSERT INTO BookingGroups (Description, CreatedDate, TotalAmount) VALUES (?,?,?)',
                (key[0], date, self._minor_opt(total_amount))  # key = (doc_number, date)
            )
            gid = dup_cur.lastrowid
            group_id_cache[key] = gid
            return gid

        # Zählbasierte Duplikat-Erkennung: innerhalb eines Splits können mehrere
        # Zeilen denselben Schlüssel (Beleg, Datum, Konto, Betrag) haben (z.B.
        # zwei gleichteure Positionen). Der DB-Bestand wird pro Schlüssel einmalig
        # VOR den Inserts dieses Laufs ermittelt; übersprungen werden nur so viele
        # CSV-Zeilen, wie die DB bereits enthält.
        db_dup_counts = {}   # dup_key → Anzahl Zeilen in DB vor diesem Import
        csv_dup_seen  = {}   # dup_key → Anzahl in dieser CSV bereits verarbeiteter Zeilen

        for pr in parsed_rows:
            try:
                doc_number   = pr['doc']
                booking_date = pr['date']
                amount       = pr['amount']
                coa_id       = pr['coa_id']

                # Duplikat-Prüfung
                minor_amount = to_minor(amount or 0)
                if doc_number:
                    dup_key = (doc_number, booking_date, coa_id, minor_amount)
                    if dup_key not in db_dup_counts:
                        if coa_id is not None:
                            dup_cur.execute(
                                'SELECT COUNT(*) FROM Bookings WHERE DocumentNumber=? AND DateBooking=? AND COA_ID=? AND Amount=?',
                                (doc_number, booking_date, coa_id, minor_amount)
                            )
                        else:
                            dup_cur.execute(
                                'SELECT COUNT(*) FROM Bookings WHERE DocumentNumber=? AND DateBooking=? AND COA_ID IS NULL AND Amount=?',
                                (doc_number, booking_date, minor_amount)
                            )
                        db_dup_counts[dup_key] = dup_cur.fetchone()[0]
                else:
                    # Ohne Belegnummer (z.B. privat/1%-Methode): nur Entry-Zeilen
                    # zählen als Treffer. Der Text gehört NICHT in den Schlüssel,
                    # weil der Tabellen-Export-Import ihn nachträglich überschreibt
                    # (Verwendungszweck) – gleichartige Zeilen unterscheidet die
                    # Zählung über die Anzahl, nicht über den Text.
                    dup_key = ('', booking_date, coa_id, minor_amount)
                    if dup_key not in db_dup_counts:
                        if coa_id is not None:
                            dup_cur.execute(
                                "SELECT COUNT(*) FROM Bookings WHERE COALESCE(DocumentNumber,'')='' "
                                "AND DateBooking=? AND COA_ID=? AND Amount=? AND BookingType='entry'",
                                (booking_date, coa_id, minor_amount)
                            )
                        else:
                            dup_cur.execute(
                                "SELECT COUNT(*) FROM Bookings WHERE COALESCE(DocumentNumber,'')='' "
                                "AND DateBooking=? AND COA_ID IS NULL AND Amount=? AND BookingType='entry'",
                                (booking_date, minor_amount)
                            )
                        db_dup_counts[dup_key] = dup_cur.fetchone()[0]
                seen = csv_dup_seen.get(dup_key, 0)
                csv_dup_seen[dup_key] = seen + 1
                if seen < db_dup_counts[dup_key]:
                    skipped += 1
                    skipped_rows.append({
                        'zeile': pr['zeile'], 'datum': booking_date,
                        'ref': doc_number, 'konto': pr['konto_str'],
                        'betrag': amount, 'text': pr['text'][:60],
                    })
                    continue

                # BookingGroup_ID ermitteln wenn zugehörige Gruppe >1 Zeile hat
                booking_group_id = None
                if doc_number:
                    key = (doc_number, booking_date)
                    if len(ref_groups.get(key, [])) > 1:
                        total = sum(abs(r['amount']) for r in ref_groups[key])
                        booking_group_id = _get_or_create_group(key, total, booking_date)

                dup_cur.execute('''
                    INSERT INTO Bookings
                        (DateBooking, BookingGroup_ID, COA_ID, CounterCOA_ID,
                         Amount, TaxRate, TaxAmount, Text, DocumentNumber, BookingType)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                ''', (booking_date, booking_group_id, coa_id, pr['counter_coa_id'],
                      to_minor(amount or 0), pr['tax_rate'], self._minor_opt(pr['tax_amount']), pr['text'],
                      doc_number, 'entry'))
                imported += 1

            except Exception as e:
                errors.append(f"Zeile {pr['zeile']}: {str(e)}")

        # Gruppensummen wiederverwendeter Gruppen aus dem DB-Bestand neu berechnen
        for gid in reused_group_ids:
            dup_cur.execute('SELECT COALESCE(SUM(ABS(Amount)),0) FROM Bookings WHERE BookingGroup_ID=?', (gid,))
            total_minor = dup_cur.fetchone()[0]
            dup_cur.execute('UPDATE BookingGroups SET TotalAmount=? WHERE ID=?', (total_minor, gid))

        dup_conn.commit()
        dup_conn.close()

        return {
            'imported':          imported,
            'updated':           updated,
            'skipped':           skipped,
            'skipped_rows':      skipped_rows,
            'missing_coa':       sorted(missing_coa),
            'missing_counter_coa': sorted(missing_counter_coa),
            'errors':            errors,
            'format':            'original'
        }
    def _import_wiso_table_format(self, text: str) -> dict:
        """Import des WISO Tabellen-Exports (6 Spalten).
        
        CSV-Spalten:
            Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr./opt. Beleg Nr.;Betrag
        
        Dieser Import aktualisiert bestehende Buchungen mit zusätzlichen Daten:
        - RecipientClient (Empf./Auft.)
        - Text (Verwendungszweck) - Zeilenumbrüche werden in Leerzeichen konvertiert
        - Kategorie → COA_ID Mapping
        - Suche nach: Datum + DocumentNumber + Amount
        
        Hinweis: Zeilenumbrüche in Textfeldern (z.B. bei Überweisungstexten) werden 
        automatisch durch Leerzeichen ersetzt, um Kompatibilität zu gewährleisten.
        
        Returns:
            dict: {imported: int, updated: int, skipped: int, errors: list[str]}
        """
        import csv, io, datetime
        
        # Lookup-Map für Kategorie-Beschreibung → COA_ID
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT Name, ID FROM ChartOfAccounts')
        coa_name_map = {row[0].lower(): row[1] for row in cursor.fetchall()}
        conn.close()

        reader = csv.DictReader(io.StringIO(text), delimiter=';', quotechar='"')
        # Spaltennamen normalisieren (führende/nachgestellte Leerzeichen entfernen)
        if reader.fieldnames:
            reader.fieldnames = [f.strip() for f in reader.fieldnames]
        imported = 0
        updated = 0
        skipped = 0
        not_found = []
        errors = []

        import unicodedata

        def _normalize_text(text):
            """Verwendungszweck für den Vergleich normalisieren.

            Zeilenumbrüche/Mehrfach-Leerzeichen → einfaches Leerzeichen,
            Unicode-NFC, Lowercase. So matchen Bewegungsdaten- und
            Tabellen-Export trotz unterschiedlicher Formatierung.
            """
            text = unicodedata.normalize('NFC', text or '')
            return ' '.join(text.split()).lower()

        conn = self._get_connection()
        cursor = conn.cursor()
        for i, row in enumerate(reader, 1):
            # Zeilenwerte normalisieren
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            try:
                # Datum parsen – Footer-Zeilen (reine Ziffernfolge als Datum) überspringen
                date_str = row.get('Buchungsdatum', '').strip()[:10]
                if not date_str or date_str.lstrip('-').isdigit():
                    skipped += 1
                    continue
                try:
                    booking_date = datetime.datetime.strptime(date_str, '%d.%m.%Y').strftime('%Y-%m-%d')
                except ValueError:
                    errors.append(f"Zeile {i}: Ungültiges Datum '{date_str}'")
                    continue
                
                # Empfänger/Auftraggeber (Zeilenumbrüche normalisieren)
                recipient = ' '.join(row.get('Empf./Auft.', '').split())
                
                # Konto-Nr. / IBAN (neues Feld im erweiterten Tabellen-Export)
                iban = ' '.join(row.get('Konto-Nr. / IBAN', '').split())
                
                # Verwendungszweck (Zeilenumbrüche normalisieren)
                purpose = ' '.join(row.get('Verwendungszweck', '').split())
                
                # Belegnummer (flexibel für beide Varianten)
                doc_number = row.get('opt. Beleg Nr.', row.get('Beleg Nr.', '')).strip()
                
                # Betrag parsen – deutsches Format z.B. -41,25 oder 1.234,56
                amount_str = row.get('Betrag', '').strip()
                if ',' in amount_str:
                    amount_str = amount_str.replace('.', '').replace(',', '.')
                try:
                    amount = float(amount_str)
                except ValueError:
                    errors.append(f"Zeile {i}: Ungültiger Betrag '{amount_str}'")
                    continue
                amount_minor = to_minor(amount)  # DB-Amount ist Minor Units (Phase 1f)

                # Kategorie → COA_ID
                category_desc = ' '.join(row.get('Kategorie', '').split())
                coa_id_from_category = coa_name_map.get(category_desc.lower()) if category_desc else None
                
                # Suche nach bestehender Buchung: Datum + Belegnummer + Betrag
                # Betrag-Suche mit ABS(), da Original-Export positive und
                # Tabellen-Export negative Vorzeichen verwenden kann.

                # Ziel-Buchung(en) bestimmen. target_rows: Liste von
                # (ID, RecipientClient, Text, COA_ID, ForeignBankAccount).
                target_rows = []
                is_split = False

                if doc_number:
                    # Beleg-Nr. kann im Original als Mehrfach-Ref gespeichert sein,
                    # z.B. "25F009, 25F073" – LIKE-Suche fängt alle Varianten ab.
                    cursor.execute('''
                        SELECT ID, RecipientClient, Text, COA_ID, ForeignBankAccount
                        FROM Bookings
                        WHERE DateBooking=? AND ABS(Amount)=ABS(?)
                          AND (
                            DocumentNumber = ?
                            OR DocumentNumber LIKE (? || ',%')
                            OR DocumentNumber LIKE ('%,' || ?)
                            OR DocumentNumber LIKE ('%,' || ? || ',%')
                          )
                        LIMIT 1
                    ''', (booking_date, amount_minor, doc_number, doc_number, doc_number, doc_number))
                else:
                    # Ohne Belegnummer: Datum + |Betrag|.
                    # BookingType + ParentBooking_ID werden für die bank+entry-
                    # Paar-Erkennung (Stage A) mitgeladen.
                    # LIMIT 3: 1 → direkt; 2 → Paar-Check; ≥3 → Textdisambiguierung.
                    cursor.execute('''
                        SELECT ID, RecipientClient, Text, COA_ID, ForeignBankAccount,
                               BookingType, ParentBooking_ID
                        FROM Bookings
                        WHERE DateBooking=? AND ABS(Amount)=ABS(?) AND (DocumentNumber IS NULL OR DocumentNumber='')
                        LIMIT 3
                    ''', (booking_date, amount_minor))

                rows_full = cursor.fetchall()
                rows = [r[:5] for r in rows_full]   # Standard-5-Tupel für target_rows

                if len(rows) == 1:
                    # Eindeutige Einzelbuchung über Datum + |Betrag|
                    target_rows = rows
                elif not doc_number:
                    # Stage A: bank+entry-Paar – eine bank-Buchung (DocNr=NULL) mit
                    # genau einem entry-Child (DocNr='', ParentBooking_ID=bank.ID)
                    # und identischem Betrag. Beide werden gemeinsam aktualisiert,
                    # analog zu Stage 2 (Belegnummer-Pfad). Typische Fälle:
                    # Privatentnahmen, Bankgebühren, Zinsen ohne Belegnummer.
                    if len(rows_full) == 2:
                        bank_cands  = [r for r in rows_full if r[5] == 'bank']
                        entry_cands = [r for r in rows_full if r[5] != 'bank']
                        if (len(bank_cands) == 1 and len(entry_cands) == 1
                                and entry_cands[0][6] == bank_cands[0][0]):
                            target_rows = rows  # bank + entry gemeinsam
                            is_split = True

                    if not target_rows:
                        # Stage B: Disambiguierung/Gruppierung über den
                        # normalisierten Verwendungszweck. Deckt zwei Fälle ab:
                        #  a) Split-Gruppe – mehrere Teilbuchungen mit gleichem Text,
                        #     deren Summe dem Zeilenbetrag entspricht (1%-Methode).
                        #  b) Mehrere gleichartige Einzelbuchungen mit identischem
                        #     Datum+Betrag, die sich nur im Verwendungszweck
                        #     unterscheiden (z.B. Amazon mit verschiedenen EREF/MREF).
                        # Ohne Verwendungszweck ist keine Zuordnung möglich → not_found.
                        norm_purpose = _normalize_text(purpose)
                        if norm_purpose:
                            cursor.execute('''
                                SELECT ID, RecipientClient, Text, COA_ID, ForeignBankAccount, Amount,
                                       BookingType, ParentBooking_ID
                                FROM Bookings
                                WHERE DateBooking=? AND (DocumentNumber IS NULL OR DocumentNumber='')
                            ''', (booking_date,))
                            all_bookings = cursor.fetchall()

                            # Nur Buchungen mit exakt passendem (normalisiertem) Text
                            grp = [b for b in all_bookings if _normalize_text(b[2]) == norm_purpose]

                            # bank+entry-Paar mit identischem Text (wie Stage A, aber
                            # per Text disambiguiert – z.B. zwei betragsgleiche Paare
                            # am selben Tag): beide gemeinsam aktualisieren, KEINE
                            # Split-Summenprüfung (das Paar ist dieselbe Buchung).
                            if len(grp) == 2:
                                bank_c  = [r for r in grp if r[6] == 'bank']
                                entry_c = [r for r in grp if r[6] != 'bank']
                                if (len(bank_c) == 1 and len(entry_c) == 1
                                        and entry_c[0][7] == bank_c[0][0]
                                        and abs(abs(bank_c[0][5]) - abs(amount_minor)) < 50):
                                    target_rows = [r[:5] for r in grp]
                                    is_split = True

                            # Gruppensumme muss dem Zeilenbetrag entsprechen. Bei genau
                            # einem Treffer ist es eine Einzelbuchung, bei mehreren ein Split.
                            if (not target_rows and grp
                                    and abs(abs(sum(r[5] for r in grp)) - abs(amount_minor)) < 50):
                                target_rows = [r[:5] for r in grp]
                                is_split = len(grp) > 1
                elif doc_number:
                    # Stage 2: Bank-Buchung, deren verknüpfte Entry-Kinder die
                    # Belegnummer tragen → Ziele = Parent + alle Kinder.
                    cursor.execute('''
                        SELECT b.ID FROM Bookings b
                        WHERE b.BookingType = 'bank' AND b.DateBooking = ? AND ABS(b.Amount) = ABS(?)
                          AND EXISTS (
                            SELECT 1 FROM Bookings c
                            WHERE c.ParentBooking_ID = b.ID
                              AND (
                                c.DocumentNumber = ?
                                OR c.DocumentNumber LIKE (? || ',%')
                                OR c.DocumentNumber LIKE ('%,' || ?)
                                OR c.DocumentNumber LIKE ('%,' || ? || ',%')
                              )
                          )
                        LIMIT 1
                    ''', (booking_date, amount_minor, doc_number, doc_number, doc_number, doc_number))
                    prow = cursor.fetchone()
                    if prow:
                        parent_id = prow[0]
                        cursor.execute('''
                            SELECT ID, RecipientClient, Text, COA_ID, ForeignBankAccount
                            FROM Bookings WHERE ID=? OR ParentBooking_ID=?
                        ''', (parent_id, parent_id))
                        target_rows = cursor.fetchall()
                        is_split = True
                    else:
                        # Stage 3: BookingGroup-Split – alle Buchungen gleicher
                        # Belegnummer (+Datum), deren Summe dem Zeilenbetrag entspricht.
                        cursor.execute('''
                            SELECT ID, RecipientClient, Text, COA_ID, ForeignBankAccount, Amount
                            FROM Bookings
                            WHERE DateBooking=?
                              AND (
                                DocumentNumber = ?
                                OR DocumentNumber LIKE (? || ',%')
                                OR DocumentNumber LIKE ('%,' || ?)
                                OR DocumentNumber LIKE ('%,' || ? || ',%')
                              )
                        ''', (booking_date, doc_number, doc_number, doc_number, doc_number))
                        grp = cursor.fetchall()
                        if grp and abs(abs(sum(r[5] for r in grp)) - abs(amount_minor)) < 50:
                            target_rows = [r[:5] for r in grp]
                            is_split = True

                if not target_rows:
                    not_found.append({
                        'zeile':  i,
                        'datum':  booking_date,
                        'beleg':  doc_number,
                        'betrag': amount,
                        'text':   purpose[:60],
                    })
                    continue

                # Updates auf alle Ziele anwenden. Bei Splits geht der Empfänger
                # auf ALLE Teilbuchungen; Zeilen-COA/Text bleiben dort erhalten.
                any_updated = False
                for tr in target_rows:
                    t_id, t_text, t_coa = tr[0], tr[2], tr[3]
                    fields = []
                    values = []
                    if recipient:
                        fields.append('RecipientClient=?'); values.append(recipient)
                    if iban:
                        fields.append('ForeignBankAccount=?'); values.append(iban)
                    if purpose and not (t_text or ''):
                        fields.append('Text=?'); values.append(purpose)
                    if (not is_split) and coa_id_from_category and not t_coa:
                        fields.append('COA_ID=?'); values.append(coa_id_from_category)
                    if fields:
                        values.append(t_id)
                        cursor.execute(
                            f'UPDATE Bookings SET {", ".join(fields)} WHERE ID=?', values)
                        any_updated = True

                if any_updated:
                    updated += 1
                else:
                    skipped += 1

            except Exception as e:
                errors.append(f"Zeile {i}: {str(e)}")

        conn.commit()
        conn.close()

        return {
            'imported':  imported,
            'updated':   updated,
            'skipped':   skipped,
            'not_found': not_found,
            'errors':    errors,
            'format':    'table'
        }
