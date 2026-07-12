# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: matching."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class MatchingMixin:
    def get_linked_entry_for_bank(self, bank_booking_id: int):
        """Hole die wichtigsten Felder des ersten verknüpften Entry-Bookings.

        Für Bank-Buchungen, die über ParentBooking_ID mit Entry-Buchungen
        verknüpft sind.  Doppik-Einträge (COA = Bankkonto) werden übersprungen.

        Returns:
            tuple(COA_ID, CounterCOA_ID, TaxRate, TaxAmount, DocumentNumber,
                  Contact_ID, Category_ID) oder None.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        cursor.execute('''
            SELECT COA_ID, CounterCOA_ID, TaxRate, TaxAmount,
                   DocumentNumber, Contact_ID, Category_ID
            FROM Bookings
            WHERE ParentBooking_ID = ?
            ORDER BY ID
        ''', (bank_booking_id,))
        for row in cursor.fetchall():
            coa_id = row[0]
            counter_coa_id = row[1]
            if not (coa_id in bank_coa_ids and counter_coa_id in bank_coa_ids):
                conn.close()
                return self._euro_row(row, 3)  # TaxAmount an Index 3 -> Euro-Decimal
        conn.close()
        return None
    def find_unlinked_booking_by_date_amount(self, date: str, amount: float):
        """Suche nach einer WISO-Buchung/-Gruppe (Account_ID IS NULL) anhand Datum + Betrag.

        Stufe 1 – Einzelbuchung: exakter Treffer auf DateBooking + Amount.
        Stufe 2 – Split-Gruppe:  SUM(Amount) der Gruppe entspricht dem Bankbetrag,
                                  alle Mitglieder sind noch unverknüpft (Account_ID IS NULL).

        Sonderfall Mehrreferenz (z.B. DocumentNumber = '25F009, 25F073'):
        Die Buchungen teilen sich eine kombinierte Referenz und landen dadurch
        bereits in einer BookingGroup → wird automatisch über Stufe 2 abgedeckt.

        Returns:
            ('single', booking_id)  – eindeutige Einzelbuchung
            ('group',  group_id)    – eindeutige Split-Gruppe
            None                    – kein eindeutiger Treffer
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        amount_minor = to_minor(amount or 0)

        # Stufe 1: einzelne, noch nicht verknüpfte Buchung (kein Split)
        cursor.execute('''
            SELECT ID FROM Bookings
            WHERE DateBooking = ? AND Amount = ?
              AND Account_ID IS NULL AND BookingGroup_ID IS NULL
        ''', (date, amount_minor))
        rows = cursor.fetchall()
        if len(rows) == 1:
            conn.close()
            return ('single', rows[0][0])

        # Stufe 2: Split-Gruppe, bei der der Gesamtbetrag passt
        # Bedingung: ALLE Mitglieder der Gruppe sind noch unverknüpft
        #            UND mindestens eine Buchung liegt auf dem gesuchten Datum
        #            (erlaubt leichte Datumsabweichungen innerhalb der Gruppe)
        cursor.execute('''
            SELECT b.BookingGroup_ID,
                   SUM(b.Amount)                                        AS total,
                   COUNT(*)                                             AS cnt,
                   SUM(CASE WHEN b.Account_ID IS NULL THEN 1 ELSE 0 END) AS unlinked
            FROM Bookings b
            WHERE b.BookingGroup_ID IS NOT NULL
              AND b.DateBooking = ?
            GROUP BY b.BookingGroup_ID
            HAVING cnt = unlinked
               AND total = ?
        ''', (date, amount_minor))
        rows = cursor.fetchall()
        conn.close()
        if len(rows) == 1:
            return ('group', rows[0][0])

        return None   # 0 oder mehrere Treffer → nicht verlässlich verknüpfbar
    def link_bank_to_entries(self) -> dict:
        """Verknüpft Bank-Buchungen (BookingType='bank') mit passenden
        Entry-Buchungen (BookingType='entry') über ParentBooking_ID.

        Matching-Strategien (in dieser Reihenfolge):

        Stufe 1 – Datum + normalisierter Empfänger + ABS(Betrag):
            Leerzeichen in RecipientClient werden komprimiert (REPLACE+LOWER).
            Doppik-Entries (COA 1460-1940) werden rausgefiltert.
            Mehrfach-Treffer (z.B. Fraenk) werden 1:1 zugeordnet.

        Stufe 2 – Datum + ABS(Betrag):
            Ohne Empfänger-Bedingung, Doppik-Filter aktiv.
            Eindeutiger Treffer wird verknüpft.

        Stufe 3 – Split-Gruppe: Datum + ABS(SUM der Gruppenmitglieder):
            Für Bank-Buchungen die einer BookingGroup (Split) entsprechen.

        Stufe 3b – Rechnungs-Split: Datum + ABS(SUM/Anzahl):
            Für Ausgangsrechnungs-Zahlungen die als Doppelbuchung
            erfasst werden (z.B. Bank 1810 + Erlöse 4405).  Die Gruppe
            wird nur verknüpft, wenn mindestens ein Mitglied ein
            Bank-COA hat.

        Stufe 3c – Privatanteil-Split: Datum + SUM ohne Privatentnahme-Offset:
            Für Split-Gruppen deren Summe durch eine positive
            Privatentnahme-Gegenbuchung (COA 2100–2199) verfälscht wird.
            Die Gruppensumme abzüglich des positiven Privatanteils
            muss dem Bankbetrag entsprechen.

        Stufe 3d – Sammelzahlung: Datum + mehrere Rechnungsnummern im Text:
            Für Bank-Buchungen mit komma-getrennten Rechnungsnummern im
            Text (z.B. "2025011,2025010").  Die Entries mehrerer
            BookingGroups werden zusammengefasst.  Summe der Bank-COA-
            Entries über alle Gruppen muss dem Bankbetrag entsprechen.

        Stufe 4 – DocumentNumber als Tiebreaker:
            Falls Stufe 2 mehrere Treffer liefert, wird versucht
            ob genau einer die passende Belegnummer enthält.

        Stufe 5 – Text-Token-Matching:
            Letzte Chance: Extrahiert lange Ziffernfolgen (>= 8 Stellen)
            aus dem Banktext und sucht denselben Token im Entry-Text.
            Deckt Fälle wie fraenk-Rechnungsnummern oder andere
            Transaktions-IDs im Verwendungszweck ab.

        Stufe 6 – Text-Similarity-Matching (fehlende BelegNr):
            Wenn mehrere Entries auf Datum+Betrag matchen, aber weder
            DocumentNumber- noch Token-Tiebreak greifen (z.B.
            Privatentnahmen ohne BelegNr), wird der Entry mit dem
            ähnlichsten Text gewählt (SequenceMatcher, normalisiert).
            Nur wenn der Beste eindeutig besser ist als der Zweitbeste
            und die Ähnlichkeit > 50 % beträgt.

        Stufe 7 – Debitoren-Auflösung (nach der Hauptschleife):
            Debitoren-Entries (COA 10000) bei Rechnungserstellung haben
            ein früheres Datum als die spätere Zahlung und können daher
            nie per Datum matchen.  Wenn eine Zahlung-Entry (gleiche
            DocumentNumber, CounterCOA=Debitoren) bereits verknüpft ist,
            wird der Debitoren-Entry als Status='resolved' markiert.

        Nach dem Linken wird der Text der Bank-Buchung durch den Text der
        Entry-Buchung ersetzt (WISO-kuratierter Text hat Vorrang).

        Returns:
            dict mit { 'linked': int, 'skipped': int, 'repaired': int,
                        'resolved': int, 'errors': list[str] }
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # ── Schritt 0: Altdaten-Reparatur ─────────────────────────────────
        cursor.execute('''
            UPDATE Bookings SET BookingType = 'bank'
            WHERE (BookingType IS NULL OR BookingType = 'entry')
              AND Account_ID IS NOT NULL
              AND COA_ID IS NULL
              AND CounterCOA_ID IS NULL
        ''')
        repaired = cursor.rowcount
        if repaired:
            conn.commit()

        # Doppik-COA-IDs — nur echte Bankkonten (aus Accounts-Tabelle)
        bank_coa_ids = self._get_bank_coa_ids(cursor)

        # Privatentnahmen-COA-IDs (SKR04 Konten 2100-2199)
        cursor.execute('''
            SELECT ID FROM ChartOfAccounts
            WHERE AccountNumber >= 2100 AND AccountNumber < 2200
        ''')
        private_coa_ids = {r[0] for r in cursor.fetchall()}

        # ── Schritt 1: Alle unverknüpften Bank-Buchungen laden ───────────
        cursor.execute('''
            SELECT b.ID, b.DateBooking, b.Amount, b.Account_ID,
                   b.DocumentNumber, b.ForeignBankAccount,
                   b.RecipientClient, b.Text
            FROM Bookings b
            WHERE b.BookingType = 'bank'
              AND b.ID NOT IN (
                  SELECT ParentBooking_ID FROM Bookings
                  WHERE ParentBooking_ID IS NOT NULL
              )
            ORDER BY b.DateBooking, b.Amount
        ''')
        bank_bookings = cursor.fetchall()

        linked = 0
        skipped = 0
        errors = []
        already_linked_entry_ids = set()  # Für 1:1 Multi-Match (Fraenk)

        def _norm(s):
            """Empfänger normalisieren: Leerzeichen komprimieren + lowercase."""
            return ' '.join((s or '').split()).lower()

        def _filter_doppik(entries):
            """Rein liquide Spiegelbuchungen rausfiltern (COA und Gegenkonto)."""
            filtered = []
            for e in entries:
                coa_id = e[3]
                counter_coa_id = e[4]
                if coa_id in bank_coa_ids and counter_coa_id in bank_coa_ids:
                    continue
                filtered.append(e)
            return filtered

        def _filter_already(entries):
            """Bereits in diesem Durchlauf verknüpfte Entries rausfiltern."""
            return [e for e in entries if e[0] not in already_linked_entry_ids]

        import re
        from difflib import SequenceMatcher
        _TOKEN_RE = re.compile(r'\d{6,}')

        def _extract_tokens(text):
            """Ziffernfolgen (>=6 Stellen) aus Text extrahieren.

            Dient als eindeutige Kennung (Rechnungs-/Transaktionsnummern),
            z.B. '1040749116593' (fraenk EREF) oder '870136' (SHBB RNR)."""
            return set(_TOKEN_RE.findall(text or ''))

        def _token_tiebreak(bank_text, entries, text_idx=2):
            """Unter mehreren Entries denjenigen finden, der einen
            gemeinsamen numerischen Token mit dem Banktext teilt.

            Wenn mehrere Entries überlappende Tokens haben (z.B. weil
            gemeinsame CRED-/IBAN-Nummern in allen PayPal-Texten stehen),
            wird der Entry mit den *meisten* gemeinsamen Tokens genommen,
            sofern er eindeutig mehr hat als alle anderen.

            Args:
                bank_text: Text der Bank-Buchung
                entries:   Kandidaten-Liste (Tuples)
                text_idx:  Index des Text-Feldes im Tuple (default 2)

            Returns:
                Einzel-Entry-Tuple oder None.
            """
            bank_tokens = _extract_tokens(bank_text)
            if not bank_tokens:
                return None
            matches = [e for e in entries
                       if _extract_tokens(e[text_idx]) & bank_tokens]
            if len(matches) == 1:
                return matches[0]
            if len(matches) >= 2:
                # Score = Anzahl gemeinsamer Tokens; höchster gewinnt
                scored = [(len(_extract_tokens(e[text_idx]) & bank_tokens), e)
                          for e in matches]
                scored.sort(key=lambda x: x[0], reverse=True)
                if scored[0][0] > scored[1][0]:
                    return scored[0][1]
            return None

        def _do_link(bank_id, entry_id, entry_group_id, entry_text):
            """Verknüpfe entry (oder ganze Gruppe) mit bank."""
            if entry_group_id:
                # Alle Gruppenmitglieder verknüpfen
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE BookingGroup_ID = ? AND BookingType = 'entry'
                ''', (bank_id, entry_group_id))
                # Alle Gruppen-IDs als bereits verknüpft markieren
                cursor.execute(
                    'SELECT ID FROM Bookings WHERE BookingGroup_ID = ?',
                    (entry_group_id,))
                for r in cursor.fetchall():
                    already_linked_entry_ids.add(r[0])
            else:
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE ID = ?
                ''', (bank_id, entry_id))
                already_linked_entry_ids.add(entry_id)
            # WISO-Text auf die Bank-Buchung übernehmen (manuell kuratiert)
            if entry_text:
                cursor.execute(
                    'UPDATE Bookings SET Text = ? WHERE ID = ?',
                    (entry_text, bank_id))

        for bank in bank_bookings:
            (bank_id, bank_date, bank_amount, bank_account_id,
             bank_docnr, bank_iban, bank_recipient, bank_text) = bank

            abs_amount = round(abs(bank_amount), 2)
            recip_norm = _norm(bank_recipient)

            # ── Stufe 1: Datum + Empfänger (normalisiert) + ABS(Betrag) ──
            if recip_norm:
                cursor.execute('''
                    SELECT ID, BookingGroup_ID, Text, COA_ID, CounterCOA_ID, RecipientClient FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NULL
                      AND DateBooking = ?
                      AND ABS(ABS(Amount) - ?) < 50
                ''', (bank_date, abs_amount))
                raw = cursor.fetchall()
                entries = _filter_already(_filter_doppik(
                    [e for e in raw if _norm(e[5]) == recip_norm
                     or _norm(e[2]) != '' and recip_norm in _norm(e[2])]
                ))
                if not entries:
                    # Fallback: direkter DB-Vergleich (REPLACE normalisiert)
                    cursor.execute('''
                        SELECT ID, BookingGroup_ID, Text, COA_ID, CounterCOA_ID FROM Bookings
                        WHERE BookingType = 'entry'
                          AND ParentBooking_ID IS NULL
                          AND DateBooking = ?
                          AND ABS(ABS(Amount) - ?) < 50
                          AND LOWER(REPLACE(REPLACE(REPLACE(TRIM(
                              COALESCE(RecipientClient,'')), '  ', ' '), '  ', ' '), '  ', ' '))
                            = ?
                    ''', (bank_date, abs_amount, recip_norm))
                    entries = _filter_already(_filter_doppik(cursor.fetchall()))
                if len(entries) == 1:
                    _do_link(bank_id, entries[0][0], None, entries[0][2])
                    linked += 1
                    continue
                if len(entries) >= 2:
                    # Mehrere Treffer: Token-Tiebreak (z.B. Fraenk-Nummern)
                    token_hit = _token_tiebreak(bank_text, entries)
                    if token_hit:
                        _do_link(bank_id, token_hit[0], None, token_hit[2])
                        linked += 1
                        continue
                    # Fallback: ersten verfügbaren nehmen
                    _do_link(bank_id, entries[0][0], None, entries[0][2])
                    linked += 1
                    continue

            # ── Stufe 2: Datum + ABS(Betrag) ─────────────────────────────
            cursor.execute('''
                                SELECT ID, BookingGroup_ID, Text, COA_ID, CounterCOA_ID, DocumentNumber
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND ABS(ABS(Amount) - ?) < 50
            ''', (bank_date, abs_amount))
            entries = _filter_already(_filter_doppik(cursor.fetchall()))
            if len(entries) == 1:
                # Nur diesen Entry linken, NICHT die ganze Gruppe
                _do_link(bank_id, entries[0][0], None, entries[0][2])
                linked += 1
                continue

            # ── Stufe 4: DocumentNumber als Tiebreaker ───────────────────
            if len(entries) > 1 and bank_docnr:
                doc_match = [e for e in entries
                             if e[5] and (bank_docnr in e[5] or e[5] in bank_docnr)]
                if len(doc_match) == 1:
                    # Nur diesen Entry linken, NICHT die ganze Gruppe
                    _do_link(bank_id, doc_match[0][0], None, doc_match[0][2])
                    linked += 1
                    continue

            # ── Stufe 3: Split-Gruppe — SUM(Betrag) passt ────────────────
            cursor.execute('''
                SELECT b.BookingGroup_ID, COUNT(*) AS cnt
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.BookingGroup_ID IS NOT NULL
                  AND b.DateBooking = ?
                GROUP BY b.BookingGroup_ID
                HAVING ABS(ABS(SUM(b.Amount)) - ?) < 50
            ''', (bank_date, abs_amount))
            groups = cursor.fetchall()
            # Bereits verknüpfte Gruppen rausfiltern
            groups = [g for g in groups if g[0] not in
                      {eid for eid in already_linked_entry_ids}]
            if len(groups) == 1:
                group_id = groups[0][0]
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE BookingGroup_ID = ? AND BookingType = 'entry'
                      AND DateBooking = ?
                ''', (bank_id, group_id, bank_date))
                cursor.execute(
                    'SELECT ID FROM Bookings WHERE BookingGroup_ID = ?'
                    ' AND DateBooking = ?',
                    (group_id, bank_date))
                for r in cursor.fetchall():
                    already_linked_entry_ids.add(r[0])
                linked += 1
                continue

            # ── Stufe 3b: Rechnungs-Split — Betrag = SUM/Anzahl ─────────
            # Muster: Ausgangsrechnung wird bezahlt → 2 Entries mit
            # gleichem Betrag (COA Bank + COA Erlöse), SUM = 2× Bankbetrag.
            # Erkennung: Ein Gruppenmitglied hat COA = Bankkonto.
            cursor.execute('''
                SELECT b.BookingGroup_ID, COUNT(*) AS cnt,
                       SUM(b.Amount) AS total
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.BookingGroup_ID IS NOT NULL
                  AND b.DateBooking = ?
                GROUP BY b.BookingGroup_ID
                HAVING cnt > 1
                   AND ABS(ABS(total * 1.0 / cnt) - ?) < 50
            ''', (bank_date, abs_amount))
            inv_groups = cursor.fetchall()
            # Filtern: Gruppe muss ein Mitglied mit Bank-COA haben
            inv_matches = []
            for g in inv_groups:
                gid = g[0]
                if gid in already_linked_entry_ids:
                    continue
                cursor.execute(
                    'SELECT COA_ID FROM Bookings WHERE BookingGroup_ID = ? AND BookingType = ?',
                    (gid, 'entry'))
                coa_ids = {r[0] for r in cursor.fetchall()}
                if coa_ids & bank_coa_ids:  # mindestens ein Bank-COA
                    inv_matches.append(gid)
            if len(inv_matches) == 1:
                group_id = inv_matches[0]
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE BookingGroup_ID = ? AND BookingType = 'entry'
                      AND DateBooking = ?
                ''', (bank_id, group_id, bank_date))
                cursor.execute(
                    'SELECT ID FROM Bookings WHERE BookingGroup_ID = ?'
                    ' AND DateBooking = ?',
                    (group_id, bank_date))
                for r in cursor.fetchall():
                    already_linked_entry_ids.add(r[0])
                linked += 1
                continue

            # ── Stufe 3c: Privatanteil-Split ─────────────────────────────
            # Muster: Split-Gruppe enthält eine positive Gegenbuchung auf
            # ein Privatentnahme-Konto (2100–2199), die den Bankbetrag
            # verfälscht.  Erkennung: Gruppensumme ohne positive
            # Privatentnahme-Einträge ≈ Bankbetrag.
            cursor.execute('''
                SELECT b.BookingGroup_ID,
                       SUM(b.Amount) AS total,
                       SUM(CASE WHEN b.Amount > 0 AND b.COA_ID IN
                           (SELECT ID FROM ChartOfAccounts
                            WHERE AccountNumber >= 2100 AND AccountNumber < 2200)
                           THEN b.Amount ELSE 0 END) AS private_offset
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.BookingGroup_ID IS NOT NULL
                  AND b.DateBooking = ?
                GROUP BY b.BookingGroup_ID
                HAVING private_offset > 0
                   AND ABS(ABS(total - private_offset) - ?) < 50
            ''', (bank_date, abs_amount))
            priv_groups = cursor.fetchall()
            priv_matches = [g[0] for g in priv_groups
                            if g[0] not in already_linked_entry_ids]
            if len(priv_matches) == 1:
                group_id = priv_matches[0]
                cursor.execute('''
                    UPDATE Bookings SET ParentBooking_ID = ?
                    WHERE BookingGroup_ID = ? AND BookingType = 'entry'
                      AND DateBooking = ?
                ''', (bank_id, group_id, bank_date))
                cursor.execute(
                    'SELECT ID FROM Bookings WHERE BookingGroup_ID = ?'
                    ' AND DateBooking = ?',
                    (group_id, bank_date))
                for r in cursor.fetchall():
                    already_linked_entry_ids.add(r[0])
                linked += 1
                continue

            # ── Stufe 3d: Sammelzahlung ────────────────────────────────
            # Muster: Bank-Text enthält mehrere komma- oder leerzeichen-
            # getrennte Rechnungsnummern (z.B. "2025011,2025010").
            # Die zugehörigen Entries liegen in verschiedenen
            # BookingGroups.  Summe der Bank-COA-Entries über alle
            # Gruppen muss dem Bankbetrag entsprechen.
            doc_nr_candidates = set(re.findall(r'\b\d{4,}\b',
                                               bank_text or ''))
            if len(doc_nr_candidates) >= 2:
                ph = ','.join('?' * len(doc_nr_candidates))
                cursor.execute(f'''
                    SELECT ID, BookingGroup_ID, Text, COA_ID,
                           CounterCOA_ID, DocumentNumber, Amount
                    FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NULL
                      AND DateBooking = ?
                      AND DocumentNumber IN ({ph})
                ''', (bank_date, *doc_nr_candidates))
                sammel_entries = _filter_already(cursor.fetchall())
                doc_nrs_found = {e[5] for e in sammel_entries}
                if len(doc_nrs_found) >= 2 and len(sammel_entries) >= 2:
                    bank_coa_sum = sum(
                        e[6] for e in sammel_entries
                        if e[3] in bank_coa_ids)
                    if abs(abs(bank_coa_sum) - abs_amount) < 50:
                        for e in sammel_entries:
                            cursor.execute(
                                'UPDATE Bookings SET ParentBooking_ID = ?'
                                ' WHERE ID = ?',
                                (bank_id, e[0]))
                            already_linked_entry_ids.add(e[0])
                        linked += 1
                        continue

            # ── Stufe 5: Text-Token-Matching (letzte Chance) ───────────
            # Suche unter allen ungelinkten Entries desselben Datums+Betrags
            # nach einem gemeinsamen numerischen Token (>= 6 Stellen) im
            # Buchungstext.  Deckt z.B. fraenk-EREF-Nummern, SHBB-RNR-
            # Nummern und andere Fälle mit Transaktions-IDs im Text ab.
            cursor.execute('''
                SELECT ID, BookingGroup_ID, Text, COA_ID, CounterCOA_ID
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND ABS(ABS(Amount) - ?) < 50
            ''', (bank_date, abs_amount))
            all_candidates = _filter_already(_filter_doppik(cursor.fetchall()))
            token_hit = _token_tiebreak(bank_text, all_candidates)
            if token_hit:
                _do_link(bank_id, token_hit[0], None, token_hit[2])
                linked += 1
                continue

            # ── Stufe 6: Text-Similarity (fehlende BelegNr) ────────────
            # Wenn mehrere Entries zum selben Datum+Betrag passen, aber
            # weder DocNr- noch Token-Tiebreak greift (z.B. Privatent-
            # nahmen ohne BelegNr), wird der Entry mit dem ähnlichsten
            # Text gewählt.  Normalisierung: Leerzeichen entfernen,
            # lowercase.  Eindeutig bester Score (> zweitbester und > 0.5)
            # wird verknüpft.
            if len(all_candidates) >= 2:
                def _text_norm(s):
                    return ''.join((s or '').lower().split())
                bank_norm = _text_norm(bank_text)
                if bank_norm:
                    scored = [
                        (SequenceMatcher(None, bank_norm,
                                         _text_norm(e[2])).ratio(), e)
                        for e in all_candidates
                    ]
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if (scored[0][0] > scored[1][0]
                            and scored[0][0] > 0.5):
                        best = scored[0][1]
                        _do_link(bank_id, best[0], None, best[2])
                        linked += 1
                        continue

            skipped += 1

        # ── Stufe 7: Debitoren-Auflösung ─────────────────────────────────
        # Debitoren-Entries (COA 10000) entstehen bei Rechnungserstellung
        # und haben ein früheres Datum als Bank- und Zahlungsbuchungen.
        # Sie können nie per Datum-Match verknüpft werden.
        # Lösung: Wenn eine Zahlung-Entry (COA = Bank, CounterCOA =
        # Debitoren) mit gleicher DocumentNumber bereits verknüpft ist,
        # setze Status = 'resolved' auf dem Debitoren-Entry.
        cursor.execute('''
            SELECT ID FROM ChartOfAccounts
            WHERE AccountNumber = 10000
        ''')
        debitoren_row = cursor.fetchone()
        resolved_count = 0
        if debitoren_row:
            debitoren_coa_id = debitoren_row[0]
            cursor.execute('''
                SELECT ID, DocumentNumber
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND COA_ID = ?
                  AND (Status IS NULL OR Status != 'resolved')
            ''', (debitoren_coa_id,))
            debitoren_entries = cursor.fetchall()

            for deb_id, doc_nr in debitoren_entries:
                if not doc_nr:
                    continue
                # Suche eine verknüpfte Zahlung-Entry mit gleicher DocNr
                # und CounterCOA = Debitoren (d.h. Zahlung auf Debitor)
                cursor.execute('''
                    SELECT ID FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NOT NULL
                      AND DocumentNumber = ?
                      AND CounterCOA_ID = ?
                    LIMIT 1
                ''', (doc_nr, debitoren_coa_id))
                if cursor.fetchone():
                    cursor.execute(
                        "UPDATE Bookings SET Status = 'resolved'"
                        " WHERE ID = ?",
                        (deb_id,))
                    resolved_count += 1

        conn.commit()
        conn.close()
        return {'linked': linked, 'skipped': skipped, 'repaired': repaired,
                'resolved': resolved_count, 'errors': errors}
