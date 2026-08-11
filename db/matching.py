# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: matching."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


def is_bank_effective(coa_id, counter_coa_id, bank_coa_ids) -> bool:
    """Bewegt dieser Buchungssatz Geld auf dem Bankkonto?

    Unter einer Bankbewegung hängen nicht nur Zahlungen, sondern auch reine
    Umbuchungen: die Zahlungs-Umbuchung 4405→4400 einer Rechnung oder der
    Privatanteil 6805→2100. Sie gehören zum Beleg, bewegen aber kein Geld und
    dürfen deshalb nicht in die Split-Summe eingehen.

    Nicht bankwirksam ist ein Satz nur dann, wenn BEIDE Seiten kontiert sind
    und KEINE davon ein Bankkonto ist. Unvollständig kontierte Sätze zählen
    bewusst mit – sonst sähe ein halbfertiger Split vollständig aus.
    """
    if coa_id in bank_coa_ids or counter_coa_id in bank_coa_ids:
        return True
    return coa_id is None or counter_coa_id is None


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
    def get_child_bookings_for_bank(self, bank_booking_id: int):
        """ALLE Kindbuchungen einer Bank-Buchung (auch Doppik-Spiegel).

        Grundlage dafür, den Buchungssatz beim Bearbeiten der Bank-Zeile
        mitzuziehen. Bewusst ungefiltert: Die Existenzprüfung "hat diese
        Bank-Buchung schon einen Buchungssatz?" muss alle Zeilen sehen,
        sonst entstünde bei jedem Speichern ein weiteres Kind.

        AutoMirror=1 kennzeichnet die von uBuchhaltung selbst als Spiegel der
        Bank-Bewegung angelegten Buchungssätze – nur sie dürfen mitgezogen
        werden (Herkunft ist aus den Werten allein nicht rekonstruierbar).

        Returns: [(ID, COA_ID, CounterCOA_ID, AutoMirror,
                   Amount€, TaxRate, TaxAmount€, DocumentNumber, Text)]
        Die Indizes 0–3 sind stabil; die weiteren Felder speisen den
        Buchungssatz-Editor der Bank-Maske.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ID, COA_ID, CounterCOA_ID, COALESCE(AutoMirror, 0),
                   Amount, TaxRate, TaxAmount, DocumentNumber, Text
            FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID
        ''', (bank_booking_id,))
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 4, 6) for r in rows]  # Amount, TaxAmount

    def get_unlinked_entry_bookings(self, around_date=None, limit=50):
        """Noch keiner Bankbewegung zugeordnete Buchungssätze.

        Kandidaten für die manuelle Zuordnung im Buchungssatz-Editor: Die
        Verbindung entsteht dort über die ID (ParentBooking_ID), unabhängig
        von Belegnummern – das deckt auch Sammelüberweisungen mehrerer
        Rechnungen mit je eigener Beleg-Nr. ab.

        around_date sortiert die zeitlich nächstliegenden nach vorn.

        Returns: [(ID, DateBooking, Amount€, DocumentNumber, Text,
                   RecipientClient, COA_ID)]
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        order = ("ABS(JULIANDAY(DateBooking) - JULIANDAY(?)), DateBooking DESC"
                 if around_date else "DateBooking DESC")
        params = ([around_date] if around_date else []) + [limit]
        cursor.execute(f'''
            SELECT ID, DateBooking, Amount, DocumentNumber, Text,
                   RecipientClient, COA_ID
            FROM Bookings
            WHERE BookingType = 'entry' AND ParentBooking_ID IS NULL
              AND Account_ID IS NULL
              AND (Status IS NULL OR Status != 'resolved')
            ORDER BY {order}
            LIMIT ?
        ''', params)
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 2) for r in rows]

    def find_unbalanced_splits(self, date_from: str, date_to: str):
        """Bankbewegungen im Zeitraum, deren Buchungssätze nicht aufgehen.

        Geprüft werden nur Bewegungen mit mindestens einem Buchungssatz – eine
        ganz unkontierte Bankbewegung ist kein Split, sondern schlicht offen.
        Umbuchungen (4405→4400, Privatanteil 6805→2100) zählen nicht mit: sie
        gehören zum Beleg, bewegen aber kein Geld.

        Grundlage der DATEV-Prüfung: ein solcher Stapel wäre in sich unstimmig.

        Betroffen ist eine Bankbewegung, wenn sie selbst ODER einer ihrer
        Buchungssätze im Zeitraum liegt – zugeordnete Bestandsbuchungen behalten
        ihr eigenes Datum, und exportiert werden die Buchungssätze. Gerechnet
        wird dann über ALLE ihre Sätze, sonst ergäbe die Summe nie den
        Bankbetrag.

        Returns: [(BankID, DateBooking, Amount€, Rest€, Text)] nach Datum.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        bank_coa_ids = self._get_bank_coa_ids(cursor)
        cursor.execute('''
            SELECT p.ID, p.DateBooking, p.Amount, p.Text,
                   e.Amount, e.COA_ID, e.CounterCOA_ID
            FROM Bookings p
            JOIN Bookings e ON e.ParentBooking_ID = p.ID
            WHERE p.BookingType = 'bank'
              AND p.ID IN (
                  SELECT b.ID FROM Bookings b
                   WHERE b.BookingType = 'bank'
                     AND b.DateBooking BETWEEN ? AND ?
                  UNION
                  SELECT c.ParentBooking_ID FROM Bookings c
                   WHERE c.ParentBooking_ID IS NOT NULL
                     AND c.DateBooking BETWEEN ? AND ?
              )
            ORDER BY p.DateBooking, p.ID
        ''', (date_from, date_to, date_from, date_to))
        rows = cursor.fetchall()
        conn.close()

        parents = {}
        sums = {}
        for bank_id, date, amount, text, child_amount, coa, counter in rows:
            parents[bank_id] = (date, amount, text)
            sums.setdefault(bank_id, 0)
            if is_bank_effective(coa, counter, bank_coa_ids):
                sums[bank_id] += (child_amount or 0)

        result = []
        for bank_id, (date, amount, text) in parents.items():
            rest = (amount or 0) - sums[bank_id]
            if rest:
                result.append((bank_id, date, from_minor(amount or 0),
                               from_minor(rest), text or ''))
        result.sort(key=lambda r: (r[1], r[0]))
        return result

    def find_unlinked_booking_by_date_amount(self, date: str, amount: float):
        """Suche nach einer unverknüpften Buchung/Beleg-Gruppe anhand Datum + Betrag.

        Stufe 1 – Einzelbuchung: exakter Treffer auf DateBooking + Amount.
        Stufe 2 – Beleg-Gruppe:  mehrere Buchungen desselben Belegs am selben
                                  Tag, deren Summe dem Bankbetrag entspricht und
                                  die alle noch unverknüpft sind (Account_ID NULL).

        Die Gruppierung ergibt sich aus der Beleg-Nr. selbst – genau daraus
        bildete früher auch der WISO-Import seine BookingGroups. Sonderfall
        Mehrfachreferenz ('25F009, 25F073'): die Buchungen teilen sich diese
        kombinierte Beleg-Nr. und werden dadurch weiterhin zusammen gefunden.

        Returns:
            ('single', booking_id)   – eindeutige Einzelbuchung
            ('docnr',  (nr, datum))  – eindeutige Beleg-Gruppe
            None                     – kein eindeutiger Treffer
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        amount_minor = to_minor(amount or 0)

        # Stufe 1: einzelne, noch nicht verknüpfte Buchung (kein Split).
        # Buchungen, die zu einem mehrzeiligen Beleg gehören, bleiben Stufe 2
        # vorbehalten – sonst würde eine Teilzeile fälschlich allein verknüpft.
        cursor.execute('''
            SELECT ID FROM Bookings b
            WHERE DateBooking = ? AND Amount = ? AND Account_ID IS NULL
              AND (DocumentNumber IS NULL OR DocumentNumber = '' OR
                   (SELECT COUNT(*) FROM Bookings x
                    WHERE x.DocumentNumber = b.DocumentNumber
                      AND x.DateBooking = b.DateBooking) = 1)
        ''', (date, amount_minor))
        rows = cursor.fetchall()
        if len(rows) == 1:
            conn.close()
            return ('single', rows[0][0])

        # Stufe 2: Beleg-Gruppe, deren Gesamtbetrag passt und deren Mitglieder
        # alle noch unverknüpft sind.
        cursor.execute('''
            SELECT b.DocumentNumber,
                   SUM(b.Amount)                                        AS total,
                   COUNT(*)                                             AS cnt,
                   SUM(CASE WHEN b.Account_ID IS NULL THEN 1 ELSE 0 END) AS unlinked
            FROM Bookings b
            WHERE b.DocumentNumber IS NOT NULL AND b.DocumentNumber != ''
              AND b.DateBooking = ?
            GROUP BY b.DocumentNumber
            HAVING cnt > 1
               AND cnt = unlinked
               AND total = ?
        ''', (date, amount_minor))
        rows = cursor.fetchall()
        conn.close()
        if len(rows) == 1:
            return ('docnr', (rows[0][0], date))

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
                coa_id = e[2]
                counter_coa_id = e[3]
                if coa_id in bank_coa_ids and counter_coa_id in bank_coa_ids:
                    continue
                filtered.append(e)
            return filtered

        def _filter_already(entries):
            """Bereits in diesem Durchlauf verknüpfte Entries rausfiltern."""
            return [e for e in entries if e[0] not in already_linked_entry_ids]

        import re
        from difflib import SequenceMatcher
        _TOKEN_DIGITS = re.compile(r'\d{6,}')
        _TOKEN_ALNUM = re.compile(r'[A-Za-z0-9]{6,}')

        def _extract_tokens(text):
            """Kennungen aus einem Text: Ziffern- **und** Buchstabenfolgen.

            Dient als eindeutige Kennung (Rechnungs-/Transaktionsnummern),
            z.B. '1040749116593' (fraenk EREF) oder '870136' (SHBB RNR).

            Rein numerisch reicht nicht: zwei Amazon-Lastschriften desselben
            Tages über denselben Betrag tragen dieselbe Bestellnummer und
            unterscheiden sich nur in der Transaktionskennung
            ('2BSTZZGVGU16KNKI'). Deshalb zusätzlich alphanumerische Folgen –
            aber nur solche mit mindestens einer Ziffer, sonst würde jedes
            längere Wort ('AMZNBusiness', 'Lohnzahlung') zum Token.

            Beide Mengen werden vereinigt: '123456' aus 'Rechnung123456' muss
            weiter auf ein blankes '123456' im Banktext passen.
            """
            text = text or ''
            tokens = set(_TOKEN_DIGITS.findall(text))
            tokens |= {t for t in _TOKEN_ALNUM.findall(text)
                       if any(c.isdigit() for c in t)}
            return tokens

        def _token_tiebreak(bank_text, entries, text_idx=1):
            """Unter mehreren Entries denjenigen finden, der einen
            gemeinsamen numerischen Token mit dem Banktext teilt.

            Wenn mehrere Entries überlappende Tokens haben (z.B. weil
            gemeinsame CRED-/IBAN-Nummern in allen PayPal-Texten stehen),
            wird der Entry mit den *meisten* gemeinsamen Tokens genommen,
            sofern er eindeutig mehr hat als alle anderen.

            Args:
                bank_text: Text der Bank-Buchung
                entries:   Kandidaten-Liste (Tuples)
                text_idx:  Index des Text-Feldes im Tuple. Alle Abfragen hier
                           liefern (ID, Text, COA_ID, …) – der Text steht auf
                           1. Der frühere Default 2 traf COA_ID und ließ die
                           Stufe mit TypeError abstürzen, sobald sie überhaupt
                           mehrere Kandidaten bekam.

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

        def _link_docnr_group(bank_id, docnr, bank_date):
            """Alle Buchungen eines Belegs desselben Tages mit der Bank
            verknüpfen (ersetzt die frühere BookingGroup-Klammer)."""
            cursor.execute('''
                UPDATE Bookings SET ParentBooking_ID = ?
                WHERE DocumentNumber = ? AND DateBooking = ?
                  AND BookingType = 'entry' AND ParentBooking_ID IS NULL
            ''', (bank_id, docnr, bank_date))
            cursor.execute(
                'SELECT ID FROM Bookings WHERE DocumentNumber = ?'
                ' AND DateBooking = ?', (docnr, bank_date))
            for r in cursor.fetchall():
                already_linked_entry_ids.add(r[0])

        def _docnr_ids(docnr, bank_date):
            """IDs aller Buchungen eines Belegs an diesem Tag."""
            cursor.execute(
                "SELECT ID FROM Bookings WHERE DocumentNumber = ?"
                " AND DateBooking = ? AND BookingType = 'entry'",
                (docnr, bank_date))
            return {r[0] for r in cursor.fetchall()}

        # COA des jeweils eigenen Zahlungskontos – nur damit lässt sich unter
        # mehreren gleichbetragigen Gruppen desselben Tages die richtige
        # auswählen (Anlagenverkauf: Zahlung über 1810 vs. Umbuchung über 1460).
        cursor.execute('''
            SELECT a.ID, c.ID FROM Accounts a
            JOIN ChartOfAccounts c ON c.AccountNumber = a.SKRAccount
            WHERE a.SKRAccount IS NOT NULL
        ''')
        own_coa_of_account = dict(cursor.fetchall())

        def _source_group_match(bank_id, bank_date, bank_amount, account_id):
            """Eindeutige SourceGroup zu dieser Bankbewegung finden.

            Bedingungen, alle drei zusammen:

            * kein Mitglied ist schon verknüpft,
            * mindestens ein Mitglied läuft über das SKR-Konto **dieser** Bank,
            * die Summe der bankwirksamen Mitglieder trifft den Bankbetrag.

            Die Vorauswahl über das eigene Konto grenzt **Alternativen** ein –
            sie greift bewusst nicht in die Summenbildung ein, die weiterhin
            über alle Mitglieder der Gruppe läuft.
            """
            own_coa = own_coa_of_account.get(account_id)
            if own_coa is None:
                return None
            cursor.execute('''
                SELECT SourceGroup FROM Bookings
                WHERE BookingType = 'entry' AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND SourceGroup IS NOT NULL AND SourceGroup != ''
                GROUP BY SourceGroup
            ''', (bank_date,))
            hits = []
            for (group,) in cursor.fetchall():
                members = _source_group_members(group)
                if not members:
                    continue
                if {m[0] for m in members} & already_linked_entry_ids:
                    continue
                if not any(own_coa in (m[2], m[3]) for m in members):
                    continue
                total = sum(m[1] or 0 for m in members
                            if is_bank_effective(m[2], m[3], bank_coa_ids))
                if abs(total - bank_amount) < 50:
                    hits.append(group)
            return hits[0] if len(hits) == 1 else None

        def _source_group_members(group):
            cursor.execute('''
                SELECT ID, Amount, COA_ID, CounterCOA_ID, Text FROM Bookings
                WHERE BookingType = 'entry' AND SourceGroup = ?
            ''', (group,))
            return cursor.fetchall()

        def _link_source_group(bank_id, group):
            """Alle Mitglieder einer Quellgruppe an die Bankbewegung hängen."""
            members = _source_group_members(group)
            cursor.execute('''
                UPDATE Bookings SET ParentBooking_ID = ?
                WHERE SourceGroup = ? AND BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
            ''', (bank_id, group))
            for member in members:
                already_linked_entry_ids.add(member[0])
            # Text der Bankbewegung durch den kuratierten WISO-Text ersetzen –
            # genommen wird der des bankwirksamen Mitglieds.
            for member in members:
                if member[4] and is_bank_effective(member[2], member[3],
                                                   bank_coa_ids):
                    cursor.execute('UPDATE Bookings SET Text = ? WHERE ID = ?',
                                   (member[4], bank_id))
                    break

        def _do_link(bank_id, entry_id, entry_text):
            """Einzelne Entry-Buchung mit der Bank-Buchung verknüpfen."""
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

            # ── Stufe 0: Klammer aus dem Quellsystem ─────────────────────
            # Wo der Import die Zusammengehörigkeit mitgeliefert hat
            # (WISO: ACCOUNTINGID → SourceGroup), muss nichts geraten werden.
            # Deshalb steht diese Stufe vor allen Heuristiken.
            group = _source_group_match(bank_id, bank_date, bank_amount,
                                        bank_account_id)
            if group is not None:
                _link_source_group(bank_id, group)
                linked += 1
                continue

            # ── Stufe 1: Datum + Empfänger (normalisiert) + ABS(Betrag) ──
            if recip_norm:
                cursor.execute('''
                    SELECT ID, Text, COA_ID, CounterCOA_ID, RecipientClient FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NULL
                      AND DateBooking = ?
                      AND ABS(ABS(Amount) - ?) < 50
                ''', (bank_date, abs_amount))
                raw = cursor.fetchall()
                entries = _filter_already(_filter_doppik(
                    [e for e in raw if _norm(e[4]) == recip_norm
                     or _norm(e[1]) != '' and recip_norm in _norm(e[1])]
                ))
                if not entries:
                    # Fallback: direkter DB-Vergleich (REPLACE normalisiert)
                    cursor.execute('''
                        SELECT ID, Text, COA_ID, CounterCOA_ID FROM Bookings
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
                    _do_link(bank_id, entries[0][0], entries[0][1])
                    linked += 1
                    continue
                if len(entries) >= 2:
                    # Mehrere Treffer: Token-Tiebreak (z.B. Fraenk-Nummern)
                    token_hit = _token_tiebreak(bank_text, entries)
                    if token_hit:
                        _do_link(bank_id, token_hit[0], token_hit[1])
                        linked += 1
                        continue
                    # Fallback: ersten verfügbaren nehmen
                    _do_link(bank_id, entries[0][0], entries[0][1])
                    linked += 1
                    continue

            # ── Stufe 2: Datum + ABS(Betrag) ─────────────────────────────
            cursor.execute('''
                                SELECT ID, Text, COA_ID, CounterCOA_ID, DocumentNumber
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND ABS(ABS(Amount) - ?) < 50
            ''', (bank_date, abs_amount))
            entries = _filter_already(_filter_doppik(cursor.fetchall()))
            if len(entries) == 1:
                # Nur diesen Entry linken, NICHT die ganze Gruppe
                _do_link(bank_id, entries[0][0], entries[0][1])
                linked += 1
                continue

            # ── Stufe 4: DocumentNumber als Tiebreaker ───────────────────
            if len(entries) > 1 and bank_docnr:
                doc_match = [e for e in entries
                             if e[4] and (bank_docnr in e[4] or e[4] in bank_docnr)]
                if len(doc_match) == 1:
                    # Nur diesen Entry linken, NICHT die ganze Gruppe
                    _do_link(bank_id, doc_match[0][0], doc_match[0][1])
                    linked += 1
                    continue

            # ── Stufe 3: Split-Gruppe — SUM(Betrag) passt ────────────────
            cursor.execute('''
                SELECT b.DocumentNumber, COUNT(*) AS cnt
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.DocumentNumber IS NOT NULL AND b.DocumentNumber != ''
                  AND b.DateBooking = ?
                GROUP BY b.DocumentNumber
                HAVING cnt > 1
                   AND ABS(ABS(SUM(b.Amount)) - ?) < 50
            ''', (bank_date, abs_amount))
            groups = [g for g in cursor.fetchall()
                      if not (_docnr_ids(g[0], bank_date) & already_linked_entry_ids)]
            if len(groups) == 1:
                _link_docnr_group(bank_id, groups[0][0], bank_date)
                linked += 1
                continue

            # ── Stufe 3b: Rechnungs-Split — Betrag = SUM/Anzahl ─────────
            # Muster: Ausgangsrechnung wird bezahlt → 2 Entries mit
            # gleichem Betrag (COA Bank + COA Erlöse), SUM = 2× Bankbetrag.
            # Erkennung: Ein Gruppenmitglied hat COA = Bankkonto.
            cursor.execute('''
                SELECT b.DocumentNumber, COUNT(*) AS cnt,
                       SUM(b.Amount) AS total
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.DocumentNumber IS NOT NULL AND b.DocumentNumber != ''
                  AND b.DateBooking = ?
                GROUP BY b.DocumentNumber
                HAVING cnt > 1
                   AND ABS(ABS(total * 1.0 / cnt) - ?) < 50
            ''', (bank_date, abs_amount))
            # Filtern: Beleg muss ein Mitglied mit Bank-COA haben
            inv_matches = []
            for g in cursor.fetchall():
                docnr = g[0]
                if _docnr_ids(docnr, bank_date) & already_linked_entry_ids:
                    continue
                cursor.execute(
                    "SELECT COA_ID FROM Bookings WHERE DocumentNumber = ?"
                    " AND DateBooking = ? AND BookingType = 'entry'",
                    (docnr, bank_date))
                coa_ids = {r[0] for r in cursor.fetchall()}
                if coa_ids & bank_coa_ids:  # mindestens ein Bank-COA
                    inv_matches.append(docnr)
            if len(inv_matches) == 1:
                _link_docnr_group(bank_id, inv_matches[0], bank_date)
                linked += 1
                continue

            # ── Stufe 3c: Privatanteil-Split ─────────────────────────────
            # Muster: Split-Gruppe enthält eine positive Gegenbuchung auf
            # ein Privatentnahme-Konto (2100–2199), die den Bankbetrag
            # verfälscht.  Erkennung: Gruppensumme ohne positive
            # Privatentnahme-Einträge ≈ Bankbetrag.
            cursor.execute('''
                SELECT b.DocumentNumber,
                       SUM(b.Amount) AS total,
                       SUM(CASE WHEN b.Amount > 0 AND b.COA_ID IN
                           (SELECT ID FROM ChartOfAccounts
                            WHERE AccountNumber >= 2100 AND AccountNumber < 2200)
                           THEN b.Amount ELSE 0 END) AS private_offset
                FROM Bookings b
                WHERE b.BookingType = 'entry'
                  AND b.ParentBooking_ID IS NULL
                  AND b.DocumentNumber IS NOT NULL AND b.DocumentNumber != ''
                  AND b.DateBooking = ?
                GROUP BY b.DocumentNumber
                HAVING COUNT(*) > 1
                   AND private_offset > 0
                   AND ABS(ABS(total - private_offset) - ?) < 50
            ''', (bank_date, abs_amount))
            priv_matches = [g[0] for g in cursor.fetchall()
                            if not (_docnr_ids(g[0], bank_date) & already_linked_entry_ids)]
            if len(priv_matches) == 1:
                _link_docnr_group(bank_id, priv_matches[0], bank_date)
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
                    SELECT ID, Text, COA_ID,
                           CounterCOA_ID, DocumentNumber, Amount
                    FROM Bookings
                    WHERE BookingType = 'entry'
                      AND ParentBooking_ID IS NULL
                      AND DateBooking = ?
                      AND DocumentNumber IN ({ph})
                ''', (bank_date, *doc_nr_candidates))
                sammel_entries = _filter_already(cursor.fetchall())
                doc_nrs_found = {e[4] for e in sammel_entries}
                if len(doc_nrs_found) >= 2 and len(sammel_entries) >= 2:
                    # Zahlungseingänge sind als Doppelbuchung erfasst (COA =
                    # Bankkonto): dann zählt nur deren Bank-Seite. Ausgaben
                    # (COA = Aufwand, Gegenkonto = Bank) haben keine solche
                    # Zeile – dort ist die Summe aller Belege maßgeblich, sonst
                    # bliebe eine Sammelüberweisung an Lieferanten unverknüpft.
                    bank_coa_sum = sum(
                        e[5] for e in sammel_entries
                        if e[2] in bank_coa_ids)
                    if bank_coa_sum == 0:
                        bank_coa_sum = sum(e[5] for e in sammel_entries)
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
                SELECT ID, Text, COA_ID, CounterCOA_ID
                FROM Bookings
                WHERE BookingType = 'entry'
                  AND ParentBooking_ID IS NULL
                  AND DateBooking = ?
                  AND ABS(ABS(Amount) - ?) < 50
            ''', (bank_date, abs_amount))
            all_candidates = _filter_already(_filter_doppik(cursor.fetchall()))
            token_hit = _token_tiebreak(bank_text, all_candidates)
            if token_hit:
                _do_link(bank_id, token_hit[0], token_hit[1])
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
                                         _text_norm(e[1])).ratio(), e)
                        for e in all_candidates
                    ]
                    scored.sort(key=lambda x: x[0], reverse=True)
                    if (scored[0][0] > scored[1][0]
                            and scored[0][0] > 0.5):
                        best = scored[0][1]
                        _do_link(bank_id, best[0], best[2])
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
