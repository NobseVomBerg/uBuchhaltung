# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: bookings."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from db.matching import (
    ENTRY_FORMS, SIDE_COUNTER, derive_entry_form, is_bank_effective, is_mirror_row,
    purpose_in_counter, read_entry_form, reform,
)
from db.invoice_payments import PAYMENT_MARK


class BookingsMixin:
    def fetch_bookings(self):
        """Fetch all bookings ordered by date descending"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Bookings ORDER BY DateBooking DESC')
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 11, 14) for r in rows]  # Amount(11), TaxAmount(14)
    @staticmethod
    def _group_entries_by_document(normal):
        """Buchungssätze desselben Belegs zu einer Split-Zeile zusammenfassen.

        Betrifft Belege ohne Bankbewegung – Kasse, Verrechnungskonto, von Hand
        erfasste Splits.

        Die Klammer ist die Beleg-Nr. am selben Tag – dieselbe Regel wie im
        Auto-Abgleich (_link_docnr_group). Erst wo keine Beleg-Nr. vorliegt,
        greift die aus dem Quellsystem übernommene ``SourceGroup``
        (WISO: ``ACCOUNTINGID``).

        Die Reihenfolge ist wichtig und nicht umkehrbar: WISO vergibt für jede
        Buchung gegen das Verrechnungskonto eine **eigene** ACCOUNTINGID, auch
        wenn mehrere zu einem Beleg gehören. Die Quellgruppe ist damit feiner
        als der Beleg und würde ihn auseinanderreißen. Umgekehrt ist sie nie
        gröber – im Echtbestand liegt keine einzige Quellgruppe über zwei
        Belegen.

        Einzelne Buchungen bleiben unverändert 'normal'.
        """
        buckets = {}
        order = []
        for item in normal:
            booking = item['booking']
            docnr = (booking[16] or '').strip()
            source_group = (booking[21] or '').strip() if len(booking) > 21 else ''
            if docnr:
                key = (docnr, item['date'])
            elif source_group:
                key = ('src', source_group)
            else:
                key = None
            if key is None:
                order.append(('single', item))
                continue
            if key not in buckets:
                buckets[key] = []
                order.append(('group', key))
            buckets[key].append(item)

        result = []
        seq = 0
        for kind, payload in order:
            if kind == 'single':
                result.append(payload)
                continue
            members = buckets[payload]
            if len(members) == 1:
                result.append(members[0])
                continue
            # Datum und Beleg-Nr. stammen aus der Gruppe selbst – bei einer
            # Klammer aus dem Quellsystem steckt beides nicht im Schlüssel.
            first = members[0]['booking']
            date = members[0]['date']
            docnr = (first[16] or '').strip()
            # Laufende Kennung statt Beleg-Nr.: sie dient nur als DOM-Griff zum
            # Auf- und Zuklappen und darf keine Sonderzeichen mitschleppen.
            seq += 1
            gid = f'g{seq}'
            children = [{'type': 'child', 'group_id': gid,
                         'date': m['date'], 'booking': m['booking']}
                        for m in members]
            result.append({
                'type': 'group',
                'group_id': gid,
                'date': date,
                'amount': sum((m['booking'][11] or 0) for m in members),
                'currency': first[12] or 'EUR',
                'description': docnr,
                'count': len(members),
                'account_id': next((m['booking'][4] for m in members if m['booking'][4]), None),
                'contact_id': next((m['booking'][7] for m in members if m['booking'][7]), None),
                'first_coa_id': first[8],
                'first_ccoa_id': first[9],
                'first_recipient': first[6],
                'first_text': first[15],
                'children': children,
            })
        return result

    def fetch_bookings_grouped(self, date_from=None, date_to=None):
        """Fetch bookings for display, with split groups aggregated.

        Args:
            date_from / date_to: optionaler Zeitraum 'YYYY-MM-DD' (einschließlich).
                Beide gesetzt → nur Buchungen dieses Zeitraums. Splits/Bank-Kinder
                werden über Parent bzw. Gruppe einbezogen, damit kein Split zerreißt.


        Returns a flat list of dicts, each with a 'type' key:

        - 'normal': ungrouped booking  →  {'type': 'normal',  'date': str, 'booking': tuple}
                                            'description': str, 'amount': float, 'count': int,
                                            'account_id': int|None, 'currency': str,
                                            'contact_id': int|None}
        - 'child':  individual split   →  {'type': 'child',   'group_id': int, 'date': str,
                                            'booking': tuple}
        - 'bank':   bank transaction   →  {'type': 'bank',    'date': str, 'booking': tuple,
                                            'children': list, 'linked': bool,
                                            'entry_text': str|None, 'entry_coa_id': int|None,
                                            'entry_counter_coa_id': int|None,
                                            'entry_docnr': str|None,
                                            'entry_category_id': int|None,
                                            'entry_contact_id': int|None}

        Bank rows with linked entries carry merged data from the first child so
        the template can render a single merged row. Rein liquide Spiegel-
        Buchungen (COA und Gegenkonto beide Bank-/Liquidkonten) werden aus der
        Normalliste ausgeblendet.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Doppik-COA-IDs laden — nur echte Bankkonten (aus Accounts-Tabelle)
        doppik_coa_ids = self._get_bank_coa_ids(cursor)

        # Optionaler Zeitraum-Filter. Bank-Kinder und Split-Gruppen werden über
        # ihren Parent bzw. ihre Gruppe einbezogen (Subquery) – so zerreißt kein
        # Split und die SQLite-Parametergrenze wird nicht überschritten.
        use_range = bool(date_from and date_to)
        rng       = (date_from, date_to)

        # 1. Bank transactions (top-level parents via ParentBooking_ID)
        if use_range:
            cursor.execute(
                "SELECT * FROM Bookings WHERE BookingType = 'bank' "
                "AND DateBooking BETWEEN ? AND ? ORDER BY DateBooking DESC", rng)
        else:
            cursor.execute(
                "SELECT * FROM Bookings WHERE BookingType = 'bank' "
                "ORDER BY DateBooking DESC")
        bank_rows = [self._euro_row(r, 11, 14) for r in cursor.fetchall()]  # Amount(11), TaxAmount(14)

        # 2. Child bookings linked to bank transactions via ParentBooking_ID
        #    (alle Kinder der im Zeitraum liegenden Bank-Buchungen)
        if use_range:
            cursor.execute(
                "SELECT * FROM Bookings WHERE ParentBooking_ID IS NOT NULL "
                "AND ParentBooking_ID IN (SELECT ID FROM Bookings "
                "WHERE BookingType = 'bank' AND DateBooking BETWEEN ? AND ?) "
                "ORDER BY ParentBooking_ID, DateBooking", rng)
        else:
            cursor.execute(
                "SELECT * FROM Bookings WHERE ParentBooking_ID IS NOT NULL "
                "ORDER BY ParentBooking_ID, DateBooking")
        children_by_parent = {}
        for r in cursor.fetchall():
            pid = r[18]  # ParentBooking_ID
            children_by_parent.setdefault(pid, []).append(self._euro_row(r, 11, 14))

        # 3. Normal (ungrouped) bookings — not bank, not child, not in legacy group
        #    Rein liquide Spiegelbuchungen und resolved Debitoren ausblenden.
        normal_sql = (
            "SELECT * FROM Bookings "
            "WHERE (BookingType IS NULL OR BookingType = 'entry') "
            "AND ParentBooking_ID IS NULL "
            "AND (Status IS NULL OR Status != 'resolved')")
        if use_range:
            cursor.execute(normal_sql + " AND DateBooking BETWEEN ? AND ? "
                           "ORDER BY DateBooking DESC", rng)
        else:
            cursor.execute(normal_sql + " ORDER BY DateBooking DESC")
        normal = []
        for r in cursor.fetchall():
            coa_id = r[8]          # COA_ID
            counter_coa_id = r[9]  # CounterCOA_ID
            if is_mirror_row(r[22], coa_id, counter_coa_id, doppik_coa_ids):  # EntryForm
                continue  # Doppik-Eintrag verbergen
            normal.append({'type': 'normal', 'date': r[1] or '', 'booking': self._euro_row(r, 11, 14)})

        conn.close()

        # 3b. Belege ohne Bankbewegung zusammenfassen (Kasse, Verrechnungskonto,
        #     manuelle Splits): mehrere Buchungssätze desselben Belegs am selben
        #     Tag gehören zusammen. Bei Bankbewegungen leistet das
        #     ParentBooking_ID – hier gibt es keine, und ohne diese Klammer
        #     stünden die Teile lose nebeneinander.
        normal = self._group_entries_by_document(normal)

        # Build bank dicts with merged entry data
        banks = []
        for b in bank_rows:
            bid = b[0]
            raw_children = children_by_parent.get(bid, [])
            children = [
                {'type': 'child', 'group_id': f'b{bid}', 'date': c[1] or '', 'booking': c}
                for c in raw_children
            ]
            # Merge: ersten (nicht-Doppik) Child als Entry-Quelle nutzen
            entry_src = None
            for c in raw_children:
                if not is_mirror_row(c[22], c[8], c[9], doppik_coa_ids):
                    entry_src = c
                    break
            banks.append({
                'type':     'bank',
                'date':     b[1] or '',
                'booking':  b,
                'children': children,
                'linked':   len(raw_children) > 0,
                'entry_text':             entry_src[15] if entry_src else None,
                'entry_coa_id':           entry_src[8]  if entry_src else None,
                'entry_counter_coa_id':   entry_src[9]  if entry_src else None,
                'entry_docnr':            entry_src[16] if entry_src else None,
                'entry_category_id':      entry_src[10] if entry_src else None,
                'entry_contact_id':       entry_src[7]  if entry_src else None,
                'entry_tax_rate':         entry_src[13] if entry_src else None,
            })

        # Merge top-level items sorted by date descending
        top_level = banks + normal
        top_level.sort(key=lambda x: x['date'], reverse=True)

        # Build flat result: parent row immediately followed by its children
        result = []
        for item in top_level:
            result.append(item)
            if item['type'] in ('bank', 'group'):
                result.extend(item.get('children', []))

        return result
    def insert_booking(self, date_booking, amount, account_id=None, foreign_bank_account="", 
                       recipient_client="", contact_id=None, coa_id=None, category_id=None,
                       currency="EUR", tax_rate=None, tax_amount=None, text="", 
                       document_number=None, date_tax=None,
                       counter_coa_id=None, log_description=None,
                       booking_type='entry', parent_booking_id=None,
                       auto_mirror=False, entry_form=None, tx=None):
        """Insert a new booking into Bookings table
        
        Args:
            date_booking: Transaction date (required)
            amount: Amount (positive = credit/Haben, negative = debit/Soll)
            account_id: FK to Accounts table
            foreign_bank_account: External IBAN/account number
            recipient_client: Name of recipient/client
            contact_id: FK to Contacts table
            coa_id: FK to ChartOfAccounts (SKR) - Sollkonto
            counter_coa_id: FK to ChartOfAccounts (SKR) - Habenkonto/Gegenkonto
            category_id: FK to Categories
            currency: Currency code (default: EUR)
            tax_rate: Tax rate as decimal (e.g., 0.19 for 19%)
            tax_amount: Calculated tax amount
            text: Notes/purpose
            document_number: External document reference
            date_tax: Tax date (optional)
            log_description: Description for SQL logging (optional)
            booking_type: 'bank', 'entry', or 'split_child' (default: 'entry')
            parent_booking_id: FK to parent Bookings row (bank transaction)
            entry_form: Form des Buchungssatzes (``db.matching.ENTRY_FORMS``). Ohne Angabe wird sie
                bei einem ``entry`` aus den Konten abgeleitet (``derive_entry_form`` mit den
                Zahlungskonten von jetzt): Der Betrag bezieht sich auf das Gegenkonto, wie die
                Buchungsmaske ihn schreibt – ein Spiegel wird so ``mirror_counter``. Schreiber mit
                einer anderen Konvention (Importe, Umbuchungen) geben die Form ausdrücklich an.
                Eine Bankzeile hat keine Form.
            tx: optionale :class:`db.core.Transaction` – dann wird darin geschrieben und erst mit
                ihr committet (der Eintrag im Replay-Log folgt nach dem Commit)
        
        Returns:
            int: ID of inserted booking
        """
        if entry_form is not None and entry_form not in ENTRY_FORMS:
            raise ValueError(f"Unbekannte Form eines Buchungssatzes: {entry_form!r}")
        conn = tx.conn if tx else self._get_connection()
        cursor = conn.cursor()
        if booking_type != 'entry':
            entry_form = None
        elif entry_form is None:
            entry_form = derive_entry_form(coa_id, counter_coa_id,
                                           self._get_payment_coa_ids(cursor), SIDE_COUNTER)
        
        sql_template = '''INSERT INTO Bookings
            (DateBooking, DateTax, Account_ID, ForeignBankAccount,
             RecipientClient, Contact_ID, COA_ID, CounterCOA_ID, Category_ID, Amount, Currency,
             TaxRate, TaxAmount, Text, DocumentNumber, BookingType, ParentBooking_ID, AutoMirror,
             EntryForm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''

        params = (date_booking, date_tax, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, counter_coa_id, category_id,
                  to_minor(amount or 0), currency,
                  tax_rate, self._minor_opt(tax_amount), text, document_number, booking_type,
                  parent_booking_id, 1 if auto_mirror else 0, entry_form)

        cursor.execute(sql_template, params)
        last_id = cursor.lastrowid
        if not tx:
            conn.commit()
            conn.close()
        
        # Optional SQL logging
        if log_description:
            if tx:
                tx.log.append((sql_template, params, log_description))
            else:
                self._log_sql(sql_template, params, log_description)
        
        return last_id
    def backfill_booking_fields(self, booking_id, coa_id=None, tax_rate=None,
                                tax_amount=None, contact_id=None,
                                document_number=None):
        """Leere Felder einer Buchung nachtragen – vorhandene Werte werden
        NIE überschrieben (COALESCE). Für die automatische SKR-Zuordnung beim
        nachträglichen Verknüpfen einer Zahlung mit einer Rechnung (todo #2).

        Bekommt ein Buchungssatz sein Konto erst hier, ändert sich seine Kontierung: Die Form
        wird neu abgeleitet (``db.matching.reform``), der Betrag behält sein Soll/Haben – und mit
        ihm der gespeicherte Steuerbetrag, der das Vorzeichen des Betrags trägt. Dann steht die ganze
        Änderung (Konto, Form, negierter Betrag und Steuerbetrag) als eine Anweisung im Protokoll – sonst
        ließe sich der Stand nicht nachspielen.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        old = cursor.execute('SELECT BookingType, EntryForm, COA_ID, CounterCOA_ID, Amount, TaxAmount '
                             'FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()
        form = amount = negated_tax = None
        if old and old[0] == 'entry' and old[2] is None and coa_id is not None:
            money_ids = self._get_payment_coa_ids(cursor)
            old_form = read_entry_form(old[1], old[2], old[3], money_ids)
            form, new_amount = reform(old_form, old[2], old[3], coa_id, old[3], money_ids, old[4])
            amount = new_amount if new_amount != old[4] else None
            if amount is not None and old[5]:
                negated_tax = -old[5]
        sql_template = '''
            UPDATE Bookings SET
                COA_ID     = COALESCE(COA_ID, ?),
                TaxRate    = COALESCE(TaxRate, ?),
                TaxAmount  = COALESCE(?, TaxAmount, ?),
                Contact_ID = COALESCE(Contact_ID, ?),
                DocumentNumber = CASE WHEN DocumentNumber IS NULL
                                        OR DocumentNumber = ''
                                      THEN ? ELSE DocumentNumber END,
                EntryForm  = COALESCE(?, EntryForm),
                Amount     = COALESCE(?, Amount)
            WHERE ID = ?
        '''
        params = (coa_id, tax_rate, negated_tax, self._minor_opt(tax_amount), contact_id,
                  document_number, form, amount, booking_id)
        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()
        if form is not None:
            self._log_sql(sql_template, params,
                          'Buchung nachgetragen: Konto, Form, Betrag und Steuerbetrag')

    def check_booking_exists(self, date, amount, account_id=None, foreign_bank_account="", text=""):
        """Anzahl vorhandener Buchungen mit gleichem Datum/Betrag/Konto.

        Absichtlich OHNE Text-/IBAN-Vergleich: beide Felder überschreibt der
        WISO-Tabellen-Export-Import nachträglich (Verwendungszweck, Konto-Nr.),
        wodurch ein Kontoauszug-Re-Import die Buchungen sonst nicht wiederfindet.
        Rückgabe ist die Anzahl (truthy = vorhanden), damit Aufrufer bei
        mehrfach identischen Transaktionen zählbasiert deduplizieren können.
        foreign_bank_account/text bleiben aus API-Kompatibilität erhalten.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM Bookings
            WHERE DateBooking=? AND Amount=? AND Account_ID=?
        ''', (date, to_minor(amount or 0), account_id))
        count = cursor.fetchone()[0]
        conn.close()
        return count
    def find_history_suggestion(self, recipient, exclude_booking_id=None):
        """Zuletzt kontierte Buchung mit ähnlichem Empfänger (todo #1).

        Grundlage der Vorschlagsfunktion: Wer denselben Zahlungspartner schon
        einmal kontiert hat, will fast immer dasselbe SKR-Konto und denselben
        Steuersatz wieder. Gesucht wird zuerst exakt (Leerzeichen ignoriert,
        Groß-/Kleinschreibung egal), danach über das längste Wort des
        Empfängers – Kontoauszüge schreiben denselben Partner selten zweimal
        identisch ("O2 Germany GmbH" vs. "O2 Germany").

        Gefunden wird die Buchung; geliefert werden ihre Buchungssätze: bei
        einem Split alle Teilbuchungen, sonst die eine. Damit kann der
        Aufrufer eine wiederkehrende Aufteilung als Ganzes übernehmen.

        Returns dict oder None:
          {'source_id', 'date', 'recipient', 'is_split',
           'rows': [{'coa_id', 'counter_coa_id', 'amount'€, 'tax_rate',
                     'tax_amount'€, 'text', 'document_number'}]}
        """
        norm = ' '.join((recipient or '').split()).replace(' ', '').lower()
        if not norm:
            return None
        conn = self._get_connection()
        cursor = conn.cursor()

        exclude = exclude_booking_id or -1
        # Teilbuchungen tragen den Empfänger nicht immer selbst – bei
        # importierten Splits steht er nur an der Bankbewegung. Deshalb zählt
        # ersatzweise der Empfänger des Elternteils.
        base = '''
            SELECT b.ID, b.DateBooking,
                   COALESCE(NULLIF(b.RecipientClient, ''), p.RecipientClient),
                   b.ParentBooking_ID, b.BookingType
            FROM Bookings b
            LEFT JOIN Bookings p ON p.ID = b.ParentBooking_ID
            WHERE b.COA_ID IS NOT NULL
              AND COALESCE(NULLIF(b.RecipientClient, ''), p.RecipientClient, '') != ''
              AND b.ID != ? AND COALESCE(b.ParentBooking_ID, -1) != ?
        '''
        eff_recipient = ("COALESCE(NULLIF(b.RecipientClient, ''),"
                         " p.RecipientClient, '')")
        cursor.execute(base + f'''
              AND LOWER(REPLACE({eff_recipient}, ' ', '')) = ?
            ORDER BY b.DateBooking DESC, b.ID DESC LIMIT 1
        ''', (exclude, exclude, norm))
        hit = cursor.fetchone()

        if not hit:
            # Fallback: längstes Wort des Empfängers als Kern (>= 4 Zeichen)
            words = sorted((recipient or '').split(), key=len, reverse=True)
            token = next((w for w in words if len(w) >= 4), None)
            if token:
                cursor.execute(base + f'''
                      AND LOWER({eff_recipient}) LIKE ?
                    ORDER BY b.DateBooking DESC, b.ID DESC LIMIT 1
                ''', (exclude, exclude, f'%{token.lower()}%'))
                hit = cursor.fetchone()

        if not hit:
            conn.close()
            return None

        hit_id, date, found_recipient, parent_id, booking_type = hit
        # Buchungssätze der Fundstelle: Split → alle Geschwister, sonst sie selbst
        group_parent = parent_id or (hit_id if booking_type == 'bank' else None)
        if group_parent:
            cursor.execute('''
                SELECT COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount,
                       Text, DocumentNumber, EntryForm
                FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID
            ''', (group_parent,))
        else:
            cursor.execute('''
                SELECT COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount,
                       Text, DocumentNumber, EntryForm
                FROM Bookings WHERE ID = ?
            ''', (hit_id,))
        raw = cursor.fetchall()
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        conn.close()

        rows = []
        for coa, counter, amount, rate, tax, text, docnr, stored_form in raw:
            if is_mirror_row(stored_form, coa, counter, bank_coa_ids):
                continue                      # reiner Doppik-Spiegel
            # Geldkonto vorne („Bank an 1460“ eingeschlossen): das Zweckkonto steht hinten
            purpose = counter if purpose_in_counter(stored_form, coa, counter,
                                                    bank_coa_ids) else coa
            # Umbuchungen (Privatanteil, Wartekonto) brauchen ihr Gegenkonto
            # mit: ohne das würde eine Kopie zur Bankbewegung und die Summe
            # der Teilbuchungen stimmte nicht mehr.
            nobank = not is_bank_effective(coa, counter, bank_coa_ids)
            rows.append({
                'coa_id': purpose,
                'counter_coa_id': counter if nobank else None,
                'nobank': nobank,
                'amount': from_minor(amount or 0),
                'tax_rate': rate,
                'tax_amount': from_minor(tax) if tax is not None else None,
                'text': text or '',
                'document_number': docnr or '',
            })
        if not rows:
            return None
        return {'source_id': group_parent or hit_id, 'date': date,
                'recipient': found_recipient, 'is_split': len(rows) > 1,
                'rows': rows}

    def get_bookings_by_import_key(self, date, amount, account_id):
        """Buchungen zum Import-Duplikatschlüssel (Datum, Betrag, Konto).

        Grundlage für den Text-Backfill beim Re-Import von Kontoauszügen.
        Returns: [(ID, Text)]
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ID, Text FROM Bookings
            WHERE DateBooking=? AND Amount=? AND Account_ID=?
        ''', (date, to_minor(amount or 0), account_id))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def update_booking_text(self, booking_id, text):
        """Nur das Text-Feld einer Buchung setzen (Text-Backfill).

        Entry-Kindbuchungen, deren Text noch leer ist oder dem alten
        Bank-Text entspricht, ziehen mit – Bank-Zeile und Buchungssatz
        bleiben so konsistent. Abweichende Kind-Texte bleiben unberührt.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT Text FROM Bookings WHERE ID=?', (booking_id,))
        row = cursor.fetchone()
        old_text = row[0] if row else None
        cursor.execute('UPDATE Bookings SET Text=? WHERE ID=?',
                       (text, booking_id))
        cursor.execute('''
            UPDATE Bookings SET Text=?
            WHERE ParentBooking_ID=? AND (Text IS NULL OR Text='' OR Text=?)
        ''', (text, booking_id, old_text or ''))
        conn.commit()
        conn.close()

    def sync_entry_child(self, child_id, date_booking, date_tax, amount,
                         recipient_client, coa_id, counter_coa_id, currency,
                         tax_rate, tax_amount, text, document_number,
                         log_description=None):
        """Auto-erzeugten Buchungssatz an die geänderte Bank-Buchung angleichen.

        NUR für Kinder aufrufen, die nachweislich der 1:1-Spiegel der
        Bank-Zeile sind (siehe handle_add_transaction) – eigenständig
        erfasste, bloß verknüpfte Buchungen haben eigene Angaben und eine
        eigene Kontierungsrichtung, die hier zerstört würden.

        Betrag und Steuer werden immer gemeinsam gesetzt: TaxAmount ist ein
        aus dem Betrag abgeleiteter Wert; einzeln geschrieben ergäbe die
        EÜR-Rechnung ``Netto = Amount − TaxAmount`` einen erfundenen Wert.
        Contact_ID bleibt bewusst erhalten (die Maske kennt nur Kunden-
        Kontakte und würde andere stillschweigend leeren), ebenso
        Account_ID, BookingType, ParentBooking_ID, Status.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        # Die Maske schreibt den Betrag der Bankbewegung, das Gegenkonto ist das Konto der Bank:
        # Zweckkonto vorne, bei zwei Geldkonten ein Spiegel mit Betrag auf dem Gegenkonto.
        form = derive_entry_form(coa_id, counter_coa_id, self._get_payment_coa_ids(cursor),
                                 SIDE_COUNTER)
        sql_template = '''UPDATE Bookings
            SET DateBooking=?, DateTax=?, RecipientClient=?, COA_ID=?,
                CounterCOA_ID=?, Amount=?, Currency=?, TaxRate=?, TaxAmount=?,
                Text=?, DocumentNumber=?, EntryForm=?
            WHERE ID=?'''
        params = (date_booking, date_tax, recipient_client, coa_id,
                  counter_coa_id, to_minor(amount or 0), currency,
                  tax_rate, self._minor_opt(tax_amount), text, document_number,
                  form, child_id)
        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()
        if log_description:
            self._log_sql(sql_template, params, log_description)

    _BOOKING_FIELD_COLUMNS = {
        'date_booking': 'DateBooking', 'date_tax': 'DateTax', 'account_id': 'Account_ID',
        'foreign_bank_account': 'ForeignBankAccount', 'recipient_client': 'RecipientClient',
        'contact_id': 'Contact_ID', 'coa_id': 'COA_ID', 'counter_coa_id': 'CounterCOA_ID',
        'category_id': 'Category_ID', 'amount': 'Amount', 'currency': 'Currency',
        'tax_rate': 'TaxRate', 'tax_amount': 'TaxAmount', 'text': 'Text',
        'document_number': 'DocumentNumber', 'parent_booking_id': 'ParentBooking_ID',
        'entry_form': 'EntryForm',
    }

    def _form_after_accounts_change(self, conn, booking_id, values):
        """Form eines Buchungssatzes, dessen Konto oder Gegenkonto sich ändert – ``values`` sind
        die geänderten Spalten (Minor Units), wie ``update_booking_fields`` sie schreibt. Nur für
        einen ``entry``, dessen Konten sich wirklich ändern und für den die Form nicht
        ausdrücklich genannt ist; sonst ``None``. Returns ``(form, amount, tax_amount)``: ``amount``
        ist der negierte Betrag, wenn die Form auf die andere Seite kippt und der Aufrufer keinen
        eigenen mitgibt (``db.matching.reform``), sonst ``None`` – dann bleibt der Betrag, wie er ist.
        Ein Betrag, der unverändert zurückkommt, gilt als nicht mitgegeben: Die Masken schicken
        den gespeicherten Betrag immer mit, und ein Wechsel des Zweckkontos soll Soll/Haben
        erhalten (ein anderer Betrag gilt dagegen für die neue Kontierung).

        Mit dem Betrag dreht der **Steuerbetrag**: Er trägt das Vorzeichen des Betrags (die EÜR
        rechnet Netto = Betrag − Steuer). ``tax_amount`` ist der negierte gespeicherte Steuerbetrag,
        wenn der Betrag negiert wird und der Aufrufer ihn nicht oder unverändert mitgibt; ein Steuerbetrag,
        den der Aufrufer ändert (auch leert), gilt wie eingegeben, ebenso ein Satz ohne Steuer
        – dann ist es ``None`` und der Aufrufer schreibt, was er hat. Alle Schreibwege, die den Betrag
        so negieren, nehmen diesen Wert mit (``update_booking_fields``, ``update_booking``, der
        Tabellen-Export, ``backfill_booking_fields``)."""
        if 'entry_form' in values or not ({'coa_id', 'counter_coa_id'} & set(values)):
            return None
        old = conn.execute('SELECT BookingType, EntryForm, COA_ID, CounterCOA_ID, Amount, TaxAmount '
                           'FROM Bookings WHERE ID=?', (booking_id,)).fetchone()
        if old is None or old[0] != 'entry':
            return None
        coa_id = values.get('coa_id', old[2])
        counter_coa_id = values.get('counter_coa_id', old[3])
        if (coa_id, counter_coa_id) == (old[2], old[3]):
            return None
        money_ids = self._get_payment_coa_ids(conn.cursor())
        old_form = read_entry_form(old[1], old[2], old[3], money_ids)
        form, amount = reform(old_form, old[2], old[3], coa_id, counter_coa_id, money_ids, old[4])
        resent = values.get('amount', old[4]) == old[4]
        negated = resent and amount != old[4]
        tax_amount = None
        if negated and old[5] and values.get('tax_amount', old[5]) == old[5]:
            tax_amount = -old[5]
        return form, (amount if negated else None), tax_amount

    def update_booking_fields(self, booking_id, log_description=None, **fields):
        """Nur die genannten Felder einer Buchung ändern; alle anderen bleiben, wie sie sind.

        Für Masken, die nicht jede Spalte führen: Was das Formular nicht kennt
        (Gegenkonto, Kategorie …), darf beim Speichern nicht geleert werden.
        Feldnamen wie bei ``insert_booking``; ``amount``/``tax_amount`` in Euro.

        Ändern sich Konto oder Gegenkonto eines Buchungssatzes, bekommt er die dazu passende
        Form (``db.matching.reform``), sofern der Aufrufer sie nicht selbst als ``entry_form``
        nennt. Ohne eigenen ``amount`` – oder mit dem unveränderten gespeicherten – bleibt dabei
        das Soll/Haben erhalten: Kippt die Form auf die andere Seite, wird der Betrag negiert –
        und mit ihm der Steuerbetrag, sofern ihn der Aufrufer nicht ändert
        (``_form_after_accounts_change``).
        """
        unknown = set(fields) - set(self._BOOKING_FIELD_COLUMNS)
        if unknown:
            raise TypeError(f"Unbekannte Buchungsfelder: {sorted(unknown)}")
        if not fields:
            return
        if fields.get('entry_form') is not None and fields['entry_form'] not in ENTRY_FORMS:
            raise ValueError(f"Unbekannte Form eines Buchungssatzes: {fields['entry_form']!r}")
        values = dict(fields)
        if 'amount' in values:
            values['amount'] = to_minor(values['amount'] or 0)
        if 'tax_amount' in values:
            values['tax_amount'] = self._minor_opt(values['tax_amount'])
        conn = self._get_connection()
        try:
            reformed = self._form_after_accounts_change(conn, booking_id, values)
            if reformed is not None:
                values['entry_form'] = reformed[0]
                if reformed[1] is not None:
                    values['amount'] = reformed[1]
                if reformed[2] is not None:
                    values['tax_amount'] = reformed[2]
            assignments = ', '.join(f"{self._BOOKING_FIELD_COLUMNS[name]}=?" for name in values)
            sql_template = f"UPDATE Bookings SET {assignments} WHERE ID=?"
            params = tuple(values.values()) + (booking_id,)
            conn.execute(sql_template, params)
            conn.commit()
        finally:
            conn.close()
        if log_description:
            self._log_sql(sql_template, params, log_description)

    def update_booking(self, booking_id, date_booking, amount, account_id=None,
                       foreign_bank_account="", recipient_client="", contact_id=None, 
                       coa_id=None, category_id=None, currency="EUR", tax_rate=None, 
                       tax_amount=None, text="", document_number=None, 
                       date_tax=None, counter_coa_id=None, log_description=None,
                       booking_type=None, parent_booking_id=None):
        """Update an existing booking
        
        Setzt ALLE Spalten – Masken, die nicht alle Felder führen, nehmen
        ``update_booking_fields``.
        
        Args:
            booking_id: ID of booking to update
            [same parameters as insert_booking]
            booking_type: 'bank', 'entry', or 'split_child' (None = keep current)
            parent_booking_id: FK to parent Bookings row (None = keep current)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Ändern sich die Konten eines Buchungssatzes, bekommt er die dazu passende Form
        # (kippt sie, drehen Betrag und unveränderter Steuerbetrag mit)
        reformed = self._form_after_accounts_change(
            conn, booking_id, {'coa_id': coa_id, 'counter_coa_id': counter_coa_id,
                               'amount': to_minor(amount or 0),
                               'tax_amount': self._minor_opt(tax_amount)})
        sql_template = '''UPDATE Bookings
            SET DateBooking=?, DateTax=?, Account_ID=?, ForeignBankAccount=?,
                RecipientClient=?, Contact_ID=?, COA_ID=?, CounterCOA_ID=?, Category_ID=?, Amount=?, Currency=?,
                TaxRate=?, TaxAmount=?, Text=?, DocumentNumber=?, BookingType=COALESCE(?, BookingType), ParentBooking_ID=COALESCE(?, ParentBooking_ID),
                EntryForm=COALESCE(?, EntryForm)
            WHERE ID=?'''
        
        params = (date_booking, date_tax, account_id, foreign_bank_account,
                  recipient_client, contact_id, coa_id, counter_coa_id, category_id,
                  reformed[1] if reformed and reformed[1] is not None else to_minor(amount or 0),
                  currency,
                  tax_rate,
                  reformed[2] if reformed and reformed[2] is not None else self._minor_opt(tax_amount),
                  text, document_number, booking_type, parent_booking_id,
                  reformed[0] if reformed else None, booking_id)

        cursor.execute(sql_template, params)
        conn.commit()
        conn.close()

        # Optional SQL logging
        if log_description:
            self._log_sql(sql_template, params, log_description)
    @staticmethod
    def _delete_booking_rows(conn, booking_ids):
        """Buchungen samt aller Verweise auf sie löschen, in der Transaktion von ``conn``:
        BookingDocuments und InvoicePayments werden gelöscht, Assets.Booking_ID wird auf NULL
        gesetzt. Eine AfA-Planzeile verliert ihre Buchung und geht zurück auf 'planned' – das
        Jahr lässt sich neu buchen. Gelöscht wird nur, was in ``booking_ids`` steht – die
        Kinder davon zuerst, dann die übrigen (Fremdschlüssel)."""
        if not booking_ids:
            return
        marks = ','.join('?' * len(booking_ids))
        conn.execute(f'DELETE FROM BookingDocuments WHERE Booking_ID IN ({marks})', booking_ids)
        conn.execute(f'DELETE FROM InvoicePayments WHERE BookingID IN ({marks})', booking_ids)
        conn.execute(f'UPDATE Assets SET Booking_ID = NULL WHERE Booking_ID IN ({marks})',
                     booking_ids)
        conn.execute("UPDATE AssetDepreciations SET Booking_ID = NULL, Status = 'planned', "
                     f"BookedAt = NULL WHERE Booking_ID IN ({marks})", booking_ids)
        conn.execute(f'DELETE FROM Bookings WHERE ID IN ({marks}) AND ParentBooking_ID IN ({marks})',
                     booking_ids + booking_ids)
        conn.execute(f'DELETE FROM Bookings WHERE ID IN ({marks})', booking_ids)

    def delete_transaction(self, booking_id: int):
        """Buchung (und verknüpfte Kinder via ParentBooking_ID) löschen – in EINER Transaktion,
        auch die Neuberechnung der betroffenen Rechnungen.

        Bereinigt vor dem Löschen alle referenzierenden Zeilen (``_delete_booking_rows``).
        Rechnungen gelöschter Zahlungen werden neu berechnet (AmountDue/Status), damit keine
        verwaisten "bezahlt"-Zustände zurückbleiben (todo #2).

        * **Bankbewegung:** Ihre Kinder und alle Zuordnungen gehen mit. Eine Forderungsbuchung
          (``db.matching.receivable_condition``), die ein Altbestand als Kind angehängt hat,
          gehört zur Rechnung und bleibt: Sie wird abgehängt (``ParentBooking_ID`` und Status
          leer) und ist damit wieder offen.
        * **Buchungssatz einer Zuordnung** (Marke ``'invoice_payment'``, auch aus dem
          Split-Editor): Die ganze Zuordnung wird entfernt (``unassign_invoice_payment``) –
          Zahlungs- und Umbuchungssatz bleiben nie einzeln übrig.
        * **Eintrag mit eigener Zuordnung** (Altbestand): Die Zuordnung wird gelöscht.
        """
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT Status, ParentBooking_ID FROM Bookings WHERE ID = ?',
                               (booking_id,)).fetchone()
            if row is None:
                conn.commit()
                return
            if row[0] == PAYMENT_MARK and row[1] is not None:
                self.remove_assignment_for_child(conn, booking_id)
                conn.commit()
                return

            receivable_sql, receivable_params = self._receivable_sql(conn.cursor())
            child_ids = [r[0] for r in conn.execute(
                'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', (booking_id,))]
            kept_ids = [r[0] for r in conn.execute(
                "SELECT ID FROM Bookings WHERE ParentBooking_ID = ? AND BookingType = 'entry' "
                f"AND {receivable_sql}", [booking_id] + receivable_params)]
            deleted_ids = [booking_id] + [c for c in child_ids if c not in kept_ids]
            marks = ','.join('?' * len(deleted_ids))
            affected_invoice_ids = [r[0] for r in conn.execute(
                f'SELECT DISTINCT InvoiceID FROM InvoicePayments WHERE BookingID IN ({marks})',
                deleted_ids)]

            if kept_ids:
                kept_marks = ','.join('?' * len(kept_ids))
                conn.execute('UPDATE Bookings SET ParentBooking_ID = NULL, Status = NULL '
                             f'WHERE ID IN ({kept_marks})', kept_ids)
            self._delete_booking_rows(conn, deleted_ids)
            for invoice_id in affected_invoice_ids:
                self._after_payment_removed(conn, invoice_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def detachable_split_rows(self, booking_ids):
        """Welche dieser Sätze löst der Bank-Editor beim Entfernen ihrer Zeile nur von der Bankbewegung,
        statt sie zu löschen? Returns die IDs als ``set``.

        Es sind Sätze, die nachweislich nicht im Editor entstanden sind: importierte (mit
        Quellklammer ``SourceGroup``), AfA-Buchungen (ein Jahr des AfA-Plans verweist mit
        ``Booking_ID`` auf sie – so erkennt ``db/assets.py`` ein Jahr als gebucht) und
        Forderungsbuchungen (``db.matching.receivable_condition``), die ein Altbestand als Kind
        angehängt hat – sie gehören zur Rechnung, wie beim Löschen der Bankbewegung
        (``delete_transaction``). Ein markierter Zahlungssatz (``PAYMENT_MARK``) nie: Er geht mit
        seiner ganzen Zuordnung."""
        ids = [int(booking_id) for booking_id in booking_ids]
        if not ids:
            return set()
        marks = ','.join('?' * len(ids))
        conn = self._get_connection()
        try:
            receivable_sql, receivable_params = self._receivable_sql(conn.cursor(), alias='b')
            rows = conn.execute(
                f"SELECT b.ID FROM Bookings b WHERE b.ID IN ({marks}) AND b.BookingType = 'entry' "
                "AND COALESCE(b.Status, '') != ? "
                "AND (COALESCE(b.SourceGroup, '') != '' "
                "     OR EXISTS (SELECT 1 FROM AssetDepreciations d WHERE d.Booking_ID = b.ID) "
                f"     OR ({receivable_sql}))",
                ids + [PAYMENT_MARK] + list(receivable_params)).fetchall()
        finally:
            conn.close()
        return {row[0] for row in rows}

    def remove_split_row(self, booking_id):
        """Die Zeile eines Satzes im Buchungssatz-Editor der Bankmaske entfernen.

        Ein Satz, der nachweislich nicht im Editor entstanden ist (``detachable_split_rows``: Import,
        AfA-Buchung), wird **abgehängt**: ``ParentBooking_ID`` leer und ein vom Abgleich gesetzter Status
        ``resolved`` zurückgesetzt – wie beim Abhängen einer Forderung (``delete_transaction``) –,
        sonst bleibt jede Spalte, wie sie ist. Er ist danach wieder eine eigenständige Buchung (Liste,
        Zuordnen-Widget, EÜR wie vor dem Zuordnen), das AfA-Jahr bleibt gebucht und der nächste
        Abgleich findet den Satz wieder. Jeder andere Satz wird gelöscht (``delete_transaction``,
        markierte Zahlungssätze mit ihrer ganzen Zuordnung). ``delete_transaction`` selbst – eine Buchung
        ausdrücklich aus der Liste löschen – bleibt unverändert."""
        if booking_id not in self.detachable_split_rows([booking_id]):
            self.delete_transaction(booking_id)
            return
        sql = ("UPDATE Bookings SET ParentBooking_ID = NULL, "
               "Status = CASE WHEN Status = 'resolved' THEN NULL ELSE Status END WHERE ID = ?")
        conn = self._get_connection()
        try:
            conn.execute(sql, (booking_id,))
            conn.commit()
        finally:
            conn.close()
        self._log_sql(sql, (booking_id,), "Split row detach")

    def get_booking_by_id(self, booking_id):
        """Get a single booking by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Bookings WHERE ID=?', (booking_id,))
        booking = cursor.fetchone()
        conn.close()
        return self._euro_row(booking, 11, 14)  # Amount(11), TaxAmount(14)
    def link_booking_to_document(self, booking_id, document_id, relation_type="receipt"):
        """Create a link between a booking and a document
        
        Args:
            booking_id: ID of the booking
            document_id: ID of the document
            relation_type: Type of relation (e.g., 'invoice', 'receipt', 'contract')
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO BookingDocuments (Booking_ID, Document_ID, RelationType)
                VALUES (?, ?, ?)
            ''', (booking_id, document_id, relation_type))
            conn.commit()
        except sqlite3.IntegrityError:
            # Link already exists
            conn.rollback()
        finally:
            conn.close()
    def get_documents_for_booking(self, booking_id):
        """Get all documents linked to a booking"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT d.*, bd.RelationType 
            FROM Documents d
            JOIN BookingDocuments bd ON d.ID = bd.Document_ID
            WHERE bd.Booking_ID = ?
        ''', (booking_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows
    def get_bookings_for_document(self, document_id):
        """Get all bookings linked to a document"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT b.*, bd.RelationType 
            FROM Bookings b
            JOIN BookingDocuments bd ON b.ID = bd.Booking_ID
            WHERE bd.Document_ID = ?
        ''', (document_id,))
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 11, 14) for r in rows]  # b.* -> Amount(11), TaxAmount(14)
    def unlink_booking_from_document(self, booking_id, document_id):
        """Remove link between booking and document"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM BookingDocuments 
            WHERE Booking_ID = ? AND Document_ID = ?
        ''', (booking_id, document_id))
        conn.commit()
        conn.close()
