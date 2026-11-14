# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: wiso_import."""
import sqlite3
import os
import json
from collections import Counter
from decimal import Decimal
from money import to_minor, from_minor
from .datev_keys import key_rate, parse_key
from .matching import (
    FORM_MIRROR_COA, FORM_MIRROR_LEGACY, LEGACY_CASH_NUMBERS, LEGACY_CASH_RANGE,
    import_entry_form,
)

#: SKR04-Konten 1000–1099 (Vorräte): Ältere Versionen haben sie als Kasse gelesen.
STOCK_RANGE = LEGACY_CASH_NUMBERS

#: Der Import nennt höchstens so viele verschiedene unbekannte Steuerschlüssel (``unknown_tax_keys``),
#: jeden höchstens so lang: Die Werte stammen aus der Datei und landen in der Ergebnisdatei und auf der Seite.
MAX_UNKNOWN_TAX_KEYS = 50
MAX_TAX_KEY_LENGTH = 20


class WisoImportMixin:
    def import_wiso_csv(self, csv_bytes: bytes) -> dict:
        """WISO Mein Büro CSV-Export in die Bookings-Tabelle importieren.
        
        Unterstützt zwei Formate:
        
        1. Original-Export (9 Spalten):
           ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER
           
        2. Tabellen-Export (7 Spalten):
           Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;opt. Beleg Nr.;Betrag
           Die Spalte ``opt. Beleg Nr.`` kann ``Beleg Nr.`` heißen; ältere Exporte haben die
           Spalte ``Konto-Nr. / IBAN`` nicht.

        Format wird automatisch erkannt. Tabellen-Export aktualisiert bestehende Buchungen.

        Returns:
            dict: {imported: int, updated: int, skipped: int, errors: list[str], format: str}
            ``format`` ist ``original``, ``table``, ``datev`` (ein DATEV-Buchungsstapel: lässt sich
            hier nicht importieren) oder ``unknown``; die Meldung zu den beiden letzten nennt
            keinen Wert aus der Datei.
            Der Original-Export meldet zusätzlich ``negative_gross`` (Zeilen mit negativem
            Bruttobetrag und genau einer liquiden Seite: WISO schreibt den Bruttobetrag nicht
            negativ – Umkehrungen stehen als vertauschte Konten; der Zähler ist ein Wächter für
            Sätze außerhalb dieses Musters. Die Regel ist festgeschrieben: Bei genau einer
            liquiden Seite bestimmt die Geldbewegung das Vorzeichen), ``stock_range_accounts``
            (Konten 1000–1099 in der Datei: ältere Versionen haben sie als Kasse gelesen; je Konto
            ``nummer``, ``bezeichnung``, ``anzahl`` der Zeilen), ``legacy_mirrors`` (Spiegel der
            Datei, die als schon vorhandene Spiegel des Bestands wiedererkannt wurden – mit
            gleichem oder umgekehrtem Vorzeichen; sie stehen noch in der alten Lesart und bleiben
            unverändert), ``legacy_stock_rows`` (Zeilen auf Konten 1000–1099, die eine ältere
            Version mit ihrer Lesart – diese Konten als Kasse – gespeichert hat: Sie gelten als
            Duplikat und werden nicht erneut angelegt) und ``unknown_tax_keys`` (Steuerschlüssel
            der Datei, die ``TaxKeys`` nicht kennt, ausgenommen leer und ``0``: Die Zeilen sind
            ohne Steuersatz importiert. Liste von ``{'schluessel': str, 'anzahl': int}``, nach
            Schlüssel sortiert, höchstens 50 Einträge, der Schlüssel auf 20 Zeichen gekürzt, leer,
            wenn es keinen gibt. Gezählt werden nur Zeilen, die tatsächlich importiert wurden –
            kein Duplikat).
        """
        import csv, io, datetime

        # Kodierung: Mit UTF-8-BOM strikt UTF-8, sonst zuerst strenges UTF-8, dann cp1252, zuletzt
        # latin-1. cp1252 dekodiert fast jede Bytefolge und darf deshalb nie zuerst stehen: Eine
        # UTF-8-Datei würde so zu Mojibake („Ã¼“ statt „ü“), ein BOM zu einem Zeichenpräfix im ersten
        # Spaltennamen. Ein einzelnes 0xE4 (cp1252-„ä“) ist kein gültiges UTF-8 und fällt auf cp1252.
        # Eine Datei mit BOM, die danach kein gültiges UTF-8 ist, ist beschädigt: abgelehnt, nicht geraten.
        encodings = (('utf-8-sig',) if csv_bytes.startswith(b'\xef\xbb\xbf')
                     else ('utf-8', 'cp1252', 'latin-1'))
        text = None
        for enc in encodings:
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
        # Ein DATEV-Buchungsstapel beginnt mit dem Vorlauf (erstes Feld EXTF bzw. DTVF, dann Berater-
        # und Mandantennummer). Die Meldungen unten nennen keinen Feldwert der Datei.
        elif headers[0].strip().casefold() in ('extf', 'dtvf'):
            return {'imported': 0, 'updated': 0, 'skipped': 0,
                    'errors': ['Das ist ein DATEV-Buchungsstapel. Er lässt sich hier nicht importieren '
                               '– bitte den Fibu-Export (Bewegungsdaten) oder die WISO-Datenbank '
                               'verwenden.'],
                    'format': 'datev'}
        else:
            return {'imported': 0, 'updated': 0, 'skipped': 0,
                    'errors': ['Unbekanntes Format: In der ersten Zeile fehlen die Spalten des '
                               'WISO-Exports (KONTO und GEGENKONTO bzw. Empf./Auft. und '
                               f'Verwendungszweck). Spalten in der ersten Zeile: {len(headers)}.'],
                    'format': 'unknown'}
    def _import_wiso_original_format(self, text: str) -> dict:
        """Import des Original WISO-Exports (9 Spalten).
        
        CSV-Spalten:
            ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER

        Mapping:
            KONTO      → ChartOfAccounts.AccountNumber → COA_ID
            GEGENKONTO → ChartOfAccounts.AccountNumber → CounterCOA_ID
            SCHLUESSEL → BU-Schlüssel → TaxRate: zuerst die Tabelle der Regeln (``db.datev_keys.key_rate``: 401/101/121
                         Regelsatz, 402/102/122 ermäßigt, 7/6/5/4 die anderen Sätze – im zweiten Halbjahr 2020 mit
                         16 % und 5 %), sonst ``TaxKeys``; ein Schlüssel, den beide nicht kennen, bekommt keinen
                         Steuersatz und wird gezählt (``unknown_tax_keys``, siehe :meth:`import_wiso_csv`). Ein Schlüssel von
                         1 bis 9999 steht außerdem am Satz (``Bookings.DatevKey``) und geht unverändert in den
                         DATEV-Stapel; leer, 0 und alles andere bleiben ohne (der Export leitet ihn ab)

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
        cursor.execute('SELECT AccountNumber, Name FROM ChartOfAccounts '
                       'WHERE AccountNumber BETWEEN ? AND ?',
                       (LEGACY_CASH_RANGE[0], LEGACY_CASH_RANGE[1]))
        stock_names = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()

        reader = csv.DictReader(io.StringIO(text), delimiter=';', quotechar='"')
        # Spaltennamen normalisieren (führende/nachgestellte Leerzeichen entfernen)
        if reader.fieldnames:
            reader.fieldnames = [f.strip() for f in reader.fieldnames]
        imported = 0
        updated = 0
        skipped = 0
        legacy_mirrors = 0
        legacy_stock_rows = 0
        negative_gross = 0
        stock_counts = Counter()    # Konto 1000–1099 → Zeilen der Datei
        unknown_keys = Counter()    # Steuerschlüssel ohne Eintrag in TaxKeys → importierte Zeilen
        skipped_rows = []       # Liste übersprungener Zeilen mit Details
        missing_coa = set()     # SKR-Kontonummern (KONTO), die nicht in ChartOfAccounts gefunden wurden
        missing_counter_coa = set()  # SKR-Kontonummern (GEGENKONTO), die nicht in ChartOfAccounts gefunden wurden
        errors = []

        # Dieselbe Menge wie der Datenbank-Import und get_payment_coa_ids (keine zweite
        # Kopie): das Verrechnungskonto 1460. Die Kasse der App (SKR04 1600) und die
        # Bankkonten zählen über ihr eigenes Konto in liquid_account_nrs. Der SKR03-Kassenbereich
        # 1000–1099 zählt seit 2.1.0 nicht mehr (SKR04: Vorräte); Zeilen darauf meldet der Import.
        from importers.wiso_fdb import DEFAULT_LIQUID_ACCOUNTS

        def _is_liquid(nr):
            """Prüft ob die SKR-Nummer ein liquides Konto ist (Bank/Kasse/Verrechnungskonto)."""
            return nr is not None and (
                nr in liquid_account_nrs  # aus Accounts-Tabelle (z.B. 1810)
                or nr in DEFAULT_LIQUID_ACCOUNTS
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
                # Dazu die Form des Satzes, wie das Vorzeichen gesetzt wurde. Zwei liquide Seiten:
                # Spiegel, der Rohbetrag bleibt (positiv = Konto im Soll). Keine: Umbuchung.
                konto_liquid, gegen_liquid = _is_liquid(konto_nr), _is_liquid(gegenkonto_nr)
                raw_amount = amount
                if gegen_liquid != konto_liquid:
                    if amount < 0:
                        # WISO schreibt den Brutto nicht negativ (Umkehrungen stehen als vertauschte
                        # Konten): Wächter für Sätze außerhalb des Musters. Die Geldseite bestimmt das Vorzeichen.
                        negative_gross += 1
                    amount = -abs(amount) if gegen_liquid else abs(amount)
                # Berührt die Zeile ein Konto 1000–1099, hat eine ältere Version sie mit einem anderen
                # Betrag gespeichert (diese Konten galten als Kasse): der Betrag der Regel von 2.0.x
                legacy_amount = None
                if konto_nr in STOCK_RANGE or gegenkonto_nr in STOCK_RANGE:
                    legacy_konto = konto_liquid or konto_nr in STOCK_RANGE
                    legacy_gegen = gegen_liquid or gegenkonto_nr in STOCK_RANGE
                    legacy_amount = ((-abs(raw_amount) if legacy_gegen else abs(raw_amount))
                                     if legacy_konto != legacy_gegen else raw_amount)
                form = import_entry_form(coa_id, konto_liquid, counter_coa_id, gegen_liquid)
                for nr in {konto_nr, gegenkonto_nr}:
                    if nr in STOCK_RANGE:
                        stock_counts[nr] += 1

                schluessel = row.get('SCHLUESSEL', '').strip()
                # Der Schlüssel steht am Satz, wenn er eine ganze Zahl von 1 bis 9999 ist; den Satz nennt zuerst die
                # Tabelle der Regeln (mit den befristeten Sätzen im zweiten Halbjahr 2020), sonst TaxKeys
                datev_key = parse_key(schluessel)
                tax_rate = key_rate(datev_key, booking_date)
                known_key = tax_rate is not None
                if not known_key:
                    tax_rate = BU_TO_TAXRATE.get(schluessel)
                    known_key = schluessel in BU_TO_TAXRATE
                # Ein Schlüssel, den die Regeln und TaxKeys nicht kennen (leer und 0 sind keiner), wird nicht geraten:
                # Die Zeile kommt ohne Steuersatz und wird gezählt, sobald sie importiert ist (unten).
                # In TaxKeys steht auch ein Schlüssel ohne Satz (z. B. 400) – der ist bekannt.
                unknown_key = (schluessel[:MAX_TAX_KEY_LENGTH]
                               if schluessel not in ('', '0') and not known_key else None)
                # 4405→4400 Umbuchung: implizit 19% USt, aber nur ohne BU-Schlüssel (leer oder 0) – eine Zeile mit einem
                # Schlüssel, auch einem ohne Satz (490), behält, was der Schlüssel sagt
                if schluessel in ('', '0') and tax_rate is None and konto_nr == 4405 and gegenkonto_nr == 4400:
                    tax_rate = 0.19
                    unknown_key = None      # die Zeile bekommt ihren Satz – nichts zu melden
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
                    'text': text_val, 'doc': doc_number, 'form': form,
                    'legacy_amount': legacy_amount, 'unknown_key': unknown_key, 'datev_key': datev_key,
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

        # Mehrzeilige Belege brauchen keine eigene Gruppen-Tabelle mehr: Die
        # Klammer ist die Beleg-Nr. selbst (DocumentNumber + Datum), nach der
        # auch der Auto-Abgleich gruppiert.

        dup_conn = self._get_connection()
        dup_cur  = dup_conn.cursor()

        # Zählbasierte Duplikat-Erkennung: innerhalb eines Splits können mehrere
        # Zeilen denselben Schlüssel (Beleg, Datum, Konto, Betrag) haben (z.B.
        # zwei gleichteure Positionen). Der DB-Bestand wird pro Schlüssel einmalig
        # VOR den Inserts dieses Laufs ermittelt; übersprungen werden nur so viele
        # CSV-Zeilen, wie die DB bereits enthält.
        db_dup_counts = {}   # dup_key → Anzahl Zeilen in DB vor diesem Import
        csv_dup_seen  = {}   # dup_key → Anzahl in dieser CSV bereits verarbeiteter Zeilen
        csv_dup_total = Counter(
            (pr['doc'], pr['date'], pr['coa_id'], to_minor(pr['amount'] or 0))
            for pr in parsed_rows)                # dup_key → Zeilen dieser CSV insgesamt
        legacy_twins  = {}   # (Beleg, Datum, Konto, Gegenkonto, Betrag) → IDs der mirror_legacy-Zeilen
        legacy_known  = {}   # dasselbe → Zahl der mirror_legacy-Zeilen mit genau diesem Betrag
        legacy_seen   = Counter()

        def _legacy_duplicate(pr, minor_amount):
            """Ist die Zeile (ein Spiegel mit dem Betrag der Datei) das Duplikat eines Spiegels des
            Bestands, der noch in der alten Lesart steht (``mirror_legacy``)? Jede vorhandene Zeile
            zählt höchstens einmal. Nur für die Meldung; geschrieben wird nichts."""
            if pr['form'] != FORM_MIRROR_COA or pr['coa_id'] is None or pr['counter_coa_id'] is None:
                return False
            key = (pr['doc'], pr['date'], pr['coa_id'], pr['counter_coa_id'], minor_amount)
            if key not in legacy_known:
                dup_cur.execute(
                    "SELECT COUNT(*) FROM Bookings WHERE BookingType='entry' AND EntryForm=? "
                    "AND COALESCE(DocumentNumber,'')=? AND DateBooking=? AND COA_ID=? "
                    "AND CounterCOA_ID=? AND Amount=?",
                    (FORM_MIRROR_LEGACY, key[0], key[1], key[2], key[3], key[4]))
                legacy_known[key] = dup_cur.fetchone()[0]
            if legacy_seen[key] < legacy_known[key]:
                legacy_seen[key] += 1
                return True
            return False

        def _db_dup_count(dup_key):
            """Zeilen im Bestand zu (Beleg, Datum, Konto, Betrag), vor den Einfügungen dieses Laufs –
            ohne Beleg-Nr. nur Buchungssätze. Je Schlüssel einmal gelesen."""
            if dup_key not in db_dup_counts:
                doc, date, coa, minor = dup_key
                if doc:
                    sql, params = ('SELECT COUNT(*) FROM Bookings WHERE DocumentNumber=? AND DateBooking=? '
                                   'AND COA_ID ' + ('=?' if coa is not None else 'IS NULL')
                                   + ' AND Amount=?'), [doc, date]
                else:
                    sql, params = ("SELECT COUNT(*) FROM Bookings WHERE COALESCE(DocumentNumber,'')='' "
                                   'AND DateBooking=? AND COA_ID ' + ('=?' if coa is not None else 'IS NULL')
                                   + " AND Amount=? AND BookingType='entry'"), [date]
                if coa is not None:
                    params.append(coa)
                params.append(minor)
                dup_cur.execute(sql, params)
                db_dup_counts[dup_key] = dup_cur.fetchone()[0]
            return db_dup_counts[dup_key]

        legacy_stock_known = {}   # (Beleg, Datum, Konto, Betrag, Gegenkonto) → Zeilen im Bestand
        legacy_stock_seen  = Counter()

        def _legacy_stock_duplicate(pr, minor_amount):
            """Hat eine ältere Version diese Zeile schon gespeichert? Berührt sie ein Konto 1000–1099,
            galt dort die Regel von 2.0.x (diese Konten als Kasse) und gab ihr einen anderen Betrag. Ein
            Treffer mit diesem Betrag und gleicher Beleg-Nr., gleichem Datum, Konto und Gegenkonto ist
            dieselbe Buchung: Duplikat, die vorhandene Zeile bleibt, wie sie ist. Jede vorhandene Zeile
            zählt höchstens einmal, und nicht die, die eine andere Zeile der Datei mit ihrem eigenen
            Betrag beansprucht. Ein Konto, das im Kontenrahmen fehlt, ist auf beiden Seiten leer
            und gilt als gleich (der Import legte die Zeile damals auch so an)."""
            if pr['legacy_amount'] is None:
                return False
            legacy_minor = to_minor(pr['legacy_amount'] or 0)
            if legacy_minor == minor_amount:
                return False                  # derselbe Betrag: die gewöhnliche Duplikatsuche hat entschieden
            own = (pr['doc'], pr['date'], pr['coa_id'], legacy_minor)
            key = own + (pr['counter_coa_id'],)
            if key not in legacy_stock_known:
                dup_cur.execute(
                    "SELECT COUNT(*) FROM Bookings WHERE BookingType='entry' "
                    "AND COALESCE(DocumentNumber,'')=? AND DateBooking=? AND COA_ID IS ? "
                    "AND Amount=? AND CounterCOA_ID IS ?",
                    (pr['doc'], pr['date'], pr['coa_id'], legacy_minor, pr['counter_coa_id']))
                legacy_stock_known[key] = dup_cur.fetchone()[0]
            unclaimed = _db_dup_count(own) - csv_dup_total[own]
            if legacy_stock_seen[key] < legacy_stock_known[key] and unclaimed > 0:
                legacy_stock_seen[key] += 1
                db_dup_counts[own] -= 1          # diese Zeile ist verbraucht
                return True
            return False

        def _legacy_twin(pr, minor_amount):
            """ID einer Spiegelzeile des Bestands (``mirror_legacy``), die dieselbe Buchung mit
            umgekehrtem Vorzeichen ist, oder ``None``. Der Betrag einer solchen Zeile steht
            gegenläufig zur Datei (Richtung auf dem Gegenkonto statt Konto im Soll). Verlangt:
            Spiegel in der Datei, Beleg-Nr., Datum, Konto und Gegenkonto gleich, Betrag
            −gross – und die Zeile ist nicht schon von einer Zeile dieser Datei mit ihrem eigenen
            Betrag beansprucht (sonst wäre es deren Duplikat, nicht der Zwilling dieser Zeile).
            Nichts davon ist geraten: Fehlt eine Bedingung, wird neu angelegt."""
            if (pr['form'] != FORM_MIRROR_COA or not pr['doc'] or pr['coa_id'] is None
                    or pr['counter_coa_id'] is None):
                return None
            own = (pr['doc'], pr['date'], pr['coa_id'], -minor_amount)
            if own not in db_dup_counts:
                dup_cur.execute(
                    'SELECT COUNT(*) FROM Bookings WHERE DocumentNumber=? AND DateBooking=? '
                    'AND COA_ID=? AND Amount=?', own)
                db_dup_counts[own] = dup_cur.fetchone()[0]
            key = own[:3] + (pr['counter_coa_id'], own[3])
            if key not in legacy_twins:
                dup_cur.execute(
                    "SELECT ID FROM Bookings WHERE BookingType='entry' AND EntryForm=? "
                    'AND DocumentNumber=? AND DateBooking=? AND COA_ID=? AND CounterCOA_ID=? '
                    'AND Amount=? ORDER BY ID',
                    (FORM_MIRROR_LEGACY, own[0], own[1], own[2], pr['counter_coa_id'], own[3]))
                legacy_twins[key] = [row[0] for row in dup_cur.fetchall()]
            unclaimed = db_dup_counts[own] - csv_dup_total[own]
            if legacy_twins[key] and unclaimed > 0:
                db_dup_counts[own] -= 1          # dieser Zwilling ist verbraucht
                return legacy_twins[key].pop(0)
            return None

        for pr in parsed_rows:
            try:
                doc_number   = pr['doc']
                booking_date = pr['date']
                amount       = pr['amount']
                coa_id       = pr['coa_id']

                # Duplikat-Prüfung
                minor_amount = to_minor(amount or 0)
                # Ohne Belegnummer (z.B. privat/1%-Methode): nur Entry-Zeilen zählen als
                # Treffer. Der Text gehört NICHT in den Schlüssel, weil der
                # Tabellen-Export-Import ihn nachträglich überschreibt (Verwendungszweck) –
                # gleichartige Zeilen unterscheidet die Zählung über die Anzahl, nicht über den Text.
                dup_key = (doc_number or '', booking_date, coa_id, minor_amount)
                _db_dup_count(dup_key)
                seen = csv_dup_seen.get(dup_key, 0)
                csv_dup_seen[dup_key] = seen + 1
                if seen < db_dup_counts[dup_key]:
                    if _legacy_duplicate(pr, minor_amount):
                        legacy_mirrors += 1
                    skipped += 1
                    skipped_rows.append({
                        'zeile': pr['zeile'], 'datum': booking_date,
                        'ref': doc_number, 'konto': pr['konto_str'],
                        'betrag': amount, 'text': pr['text'][:60],
                    })
                    continue

                # Kein Treffer mit diesem Betrag: Vielleicht hat eine ältere Version die Zeile mit dem
                # Betrag ihrer Regel gespeichert (Konten 1000–1099 als Kasse) – dann ist sie ein Duplikat.
                if _legacy_stock_duplicate(pr, minor_amount):
                    legacy_stock_rows += 1
                    skipped += 1
                    skipped_rows.append({
                        'zeile': pr['zeile'], 'datum': booking_date,
                        'ref': doc_number, 'konto': pr['konto_str'],
                        'betrag': amount, 'text': pr['text'][:60],
                    })
                    continue

                # Kein Treffer mit gleichem Betrag: Vielleicht ist die Zeile ein Spiegel des
                # Bestands mit umgekehrtem Vorzeichen – dann ist sie ein Duplikat. Die vorhandene
                # Zeile bleibt, wie sie ist (ihre Lesart ist unbekannt, siehe ``mirror_legacy``).
                twin = _legacy_twin(pr, minor_amount)
                if twin is not None:
                    legacy_mirrors += 1
                    skipped += 1
                    skipped_rows.append({
                        'zeile': pr['zeile'], 'datum': booking_date,
                        'ref': doc_number, 'konto': pr['konto_str'],
                        'betrag': amount, 'text': pr['text'][:60],
                    })
                    continue

                # Mehrzeilige Belege bleiben über DocumentNumber + Datum
                # zusammen – eine eigene Gruppen-Zeile braucht es nicht.
                dup_cur.execute('''
                    INSERT INTO Bookings
                        (DateBooking, COA_ID, CounterCOA_ID,
                         Amount, TaxRate, TaxAmount, Text, DocumentNumber, BookingType, EntryForm, DatevKey)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ''', (booking_date, coa_id, pr['counter_coa_id'],
                      to_minor(amount or 0), pr['tax_rate'], self._minor_opt(pr['tax_amount']), pr['text'],
                      doc_number, 'entry', pr['form'], pr['datev_key']))
                imported += 1
                if pr['unknown_key'] is not None:
                    unknown_keys[pr['unknown_key']] += 1

            except Exception as e:
                errors.append(f"Zeile {pr['zeile']}: {str(e)}")

        dup_conn.commit()
        dup_conn.close()

        return {
            'imported':          imported,
            'updated':           updated,
            'skipped':           skipped,
            'skipped_rows':      skipped_rows,
            'missing_coa':       sorted(missing_coa),
            'missing_counter_coa': sorted(missing_counter_coa),
            'negative_gross':    negative_gross,
            'stock_range_accounts': [
                {'nummer': nr, 'bezeichnung': stock_names.get(nr, ''), 'anzahl': count}
                for nr, count in sorted(stock_counts.items())],
            'legacy_mirrors':    legacy_mirrors,
            'legacy_stock_rows': legacy_stock_rows,
            'unknown_tax_keys': [
                {'schluessel': key, 'anzahl': count}
                for key, count in sorted(unknown_keys.items())[:MAX_UNKNOWN_TAX_KEYS]],
            'errors':            errors,
            'format':            'original'
        }
    def _import_wiso_table_format(self, text: str) -> dict:
        """Import des WISO Tabellen-Exports (7 Spalten).

        CSV-Spalten:
            Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;opt. Beleg Nr./Beleg Nr.;Betrag
            (ältere Exporte haben die Spalte ``Konto-Nr. / IBAN`` nicht)

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
                # Datum parsen – die Fußzeile des Exports (reine Ziffernfolge als Datum) ist keine
                # Buchung und zählt in keinem Zähler mit
                date_str = row.get('Buchungsdatum', '').strip()[:10]
                if date_str.lstrip('-').isdigit():
                    continue
                if not date_str:
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
                        # Stage 3: Beleg-Split – alle Buchungen gleicher
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
                        # Ein mitgebrachter BU-Schlüssel passt nicht mehr zum neuen Konto: Er fällt – dieselbe Regel
                        # (``_datev_key_stale``) wie in ``update_booking_fields``
                        if self._datev_key_stale(conn, t_id, {'coa_id': coa_id_from_category}):
                            fields.append('DatevKey=?'); values.append(None)
                        # Bekommt ein Buchungssatz sein Konto erst hier, ändert sich seine
                        # Kontierung: Die Form wird mit den Konten neu abgeleitet
                        reformed = self._form_after_accounts_change(
                            conn, t_id, {'coa_id': coa_id_from_category})
                        if reformed is not None:
                            fields.append('EntryForm=?'); values.append(reformed[0])
                            if reformed[1] is not None:
                                fields.append('Amount=?'); values.append(reformed[1])
                            if reformed[2] is not None:
                                fields.append('TaxAmount=?'); values.append(reformed[2])
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
