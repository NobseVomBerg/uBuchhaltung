# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: „Zuordnung neu aufbauen“ – die Rechnungszahlungen EINER Rechnung aus einem älteren Bestand reparieren.

Eine Zuordnung kann ohne ihre Buchungssätze dastehen (nur die ``InvoicePayments``-Zeile), die Forderung als Kind an einer
Bankbewegung hängen, eine Zuordnung doppelt stehen oder der offene Rest nicht zu den Zuordnungen passen
(``find_payment_inconsistencies`` meldet es). ``rebuild_invoice_payments`` macht davon vier Dinge, je Rechnung und nur nach
einem Probelauf – in dieser Reihenfolge:

A. **Doppelte Zuordnungen** desselben Paares (Rechnung, Bewegung) mit gleichem Betrag zusammenfassen: Die kleinste ID bleibt.
B. **Die angehängte Forderung der App** (ohne ``SourceGroup``, Wartekonto als Gegenkonto) von der Bankbewegung lösen
   (``ParentBooking_ID`` leer, ein Status ``resolved`` zurückgesetzt – wie ``Database.remove_split_row``).
C. Eine **Zuordnung ohne eigene Sätze** mit den heutigen Bausteinen neu schreiben (``_unassign`` + ``_assign_in_tx``) – alle
   Zuordnungen der Rechnung **in zwei Phasen in EINEM Savepoint**: erst alle entfernen, dann in der Reihenfolge der Bankbewegungen
   (ihr Datum, dann die ID der Zuordnung) neu buchen. So rechnet jede die Steuer wie beim Zuordnen nacheinander (die späteren zählen noch nicht als bezahlt, die letzte
   schließt die Rechnung und bekommt den Rest), und die Sätze sind die, die ``assign_invoice_payment`` in dieser Folge auf einem
   frischen Bestand geschrieben hätte. Passt eine nicht mit genau ihrem Betrag, geht der ganze Durchgang zurück (Zuordnungen,
   Sätze, Replay-Log), sie bleibt samt ID liegen (``not_free``), und die übrigen laufen ohne sie noch einmal. Bedingung („R1“): Die
   Zuordnung hängt an einer Bankbewegung mit Zahlungseingang; unter der Bewegung steht kein Satz mit ``SourceGroup`` und keiner
   mit der Nummer dieser Rechnung; jeder andere Satz gehört einer anderen Rechnung, die selbst der Bewegung zugeordnet ist; die
   Forderung der Rechnung ist genau eine und von der App; das Bankkonto hat eine SKR-Nummer; der Betrag hat keinen Bruchteil eines
   Cents; für den Steuersatz gibt es ein Erlöskonto (nie wird eines angelegt) – und **die Rechnung hat nirgends sonst Sätze**
   (``elsewhere``): kein Buchungssatz außer der Forderung und außer denen unter einer Bewegung, der die Rechnung zugeordnet ist,
   trägt ihre Nummer (ohne Rücksicht auf die Schreibweise und auf Leerraum am Rand – so, wie ``str.strip()`` ihn kennt, auch
   Tabulator, Zeilenumbruch und geschütztes Leerzeichen; eigenständig, auf der Kasse, unter einer fremden Bewegung, mit oder ohne
   ``SourceGroup``), und die Prüfliste meldet für sie keinen ``orphan_payment_rows`` – sonst stünde der Erlös nach dem Neuaufbau
   doppelt in den Büchern. **Und die vorhandenen Sätze buchen nicht mehr Erlös, als die übrigen Zuordnungen erklären** (Erlös = Betrag
   der Sätze, die auf einer Seite ein Erlöskonto führen, ohne Wartekonten: „Wartekonto an Erlöskonto“ und „Erlöskonto an Bank“ je
   einmal, „Bank an Debitoren“ nie; ein Betrag, der keine ganze Zahl ist, sperrt): je Rechnung (``overbooked``: der Erlös aller Sätze
   mit ihrer Nummer liegt nicht über der Summe ihrer Zuordnungen, die nicht neu gebaut werden) und je Bewegung
   (``overbooked_movement``: der Erlös der Sätze unter der Bewegung – sie gehören alle zu anderen Rechnungen – liegt nicht über
   der Summe von deren Zuordnungen dort). Diese Bedingungen nicht lockern – lieber melden als falsch reparieren.
D. Den **offenen Betrag** neu rechnen. Der Status bleibt, wie er ist (eine von Hand auf „bezahlt“ gesetzte Rechnung bleibt es);
   nur nach A rechnet ihn ``next_payment_status`` neu.

**Kein Buchungssatz wird gelöscht oder geändert.** Neue Sätze kommen dazu (C); B löst eine Forderung vom Elternsatz und sonst
nichts. Storno-Paare (die Rechnung hat eine Stornorechnung oder ist eine) und Rechnungen außerhalb von versendet bis bezahlt
bekommen nur den neu gerechneten offenen Betrag (D); alles andere wird gemeldet (``skipped``), nicht angefasst.
``_booking_snapshot_unchanged`` prüft das mechanisch: Hat ein Baustein trotzdem einen vorhandenen Satz der Rechnung oder ihrer
Bewegungen verändert (etwa den Status einer aufgelösten Forderung), wird alles zurückgenommen und nur gemeldet. Ein zweites Netz
liest die Prüfliste nach dem Schreiben noch einmal: Nennt sie eine Kategorie, die sie vorher nicht nannte (etwa
``transfer_mismatch`` – der Erlös stünde doppelt da), wird ebenso alles zurückgenommen; die Kategorien stehen nur im Server-Log.
Grenze: Einen Satz ohne die Rechnungsnummer, der denselben Erlös schon bucht, erkennt die Funktion nicht – deshalb zeigt der
Probelauf die Wirkung auf die EÜR, und der Hinweis bittet um die Prüfung.

**Probelauf und Ausführung sind derselbe Code** (``_rebuild_in``): Der Probelauf endet mit ROLLBACK und hinterlässt nichts – keine
Zeile, keinen Zähler (``sqlite_sequence``), keinen Eintrag im Replay-Log. Ausgeführt wird nur mit der Prüfsumme des Probelaufs
(``RebuildPlan.check``): Hat sich der Bestand seither geändert, kommt der neue Plan zurück und nichts ist geschrieben. Alles läuft
in EINER ``BEGIN IMMEDIATE``-Transaktion; Konten legt die Funktion nie an.
"""
import datetime
import hashlib
import json
from collections import defaultdict, namedtuple

from money import fmt_eur, from_minor
from .invoice_payments import (NO_REVENUE_COA, PAYMENT_ROW_COLUMNS, RECEIVABLE_STATUSES, STORNO_KIND,
                               next_payment_status)
from .invoice_versions import is_iso_date
from .reporting import is_income_account

# Auf diese Status baut die Reparatur eine Zuordnung neu auf; 'partial' ist der Alt-Wert von 'partial_payment'
REBUILD_STATUSES = RECEIVABLE_STATUSES | {'partial'}

# Was die Reparatur tut, in der Reihenfolge des Plans: ``kind`` ist dedupe | detach | rebuild | recalc. ``payment_id``: die
# Zuordnung (bei ``rebuild`` die alte, nie die neue), ``booking_id``: die Bankbewegung, ``amount`` in Minor Units, ``text``: der
# feste Satz für den Nutzer
RebuildAction = namedtuple('RebuildAction', 'kind payment_id booking_id amount text')
# Ein neuer Buchungssatz (ohne seine ID): ``account``/``counter_account`` als Kontonummern, Beträge in Minor Units,
# ``euer_account`` die Kontonummer des Erlöskontos, auf dem der Satz in der EÜR wirkt (sonst ``None``), ``columns`` alle Spalten
# des Satzes in der Reihenfolge ``PAYMENT_ROW_COLUMNS`` – sie gehen nur in die Prüfsumme (Empfänger, Steuerdatum, Währung …)
RebuildRow = namedtuple('RebuildRow', 'bank_id date account counter_account amount tax_rate tax text form euer_account columns')
# Die Wirkung der neuen Sätze auf die EÜR je Datum und Erlöskonto (Minor Units, Kontonummer): netto und Umsatzsteuer
EuerEffect = namedtuple('EuerEffect', 'date account net tax')
# Der Plan einer Rechnung: Aktionen, neue Sätze, Wirkung auf die EÜR, Status und offener Betrag vorher/nachher, die Befunde der
# Prüfliste vorher und nachher (je ``(Kategorie, Text)``), was nicht angefasst wird (Texte) und die Prüfsumme ``check``
RebuildPlan = namedtuple('RebuildPlan', 'invoice_id invoice_number actions rows euer status_before status_after due_before '
                                        'due_after findings_before findings_after skipped revenue_coa check')
# Ergebnis von ``rebuild_invoice_payments``: ob es gelang, Statuscode, Klartext (oder ``None``), der Plan (oder ``None``), ob
# ausgeführt wurde, und ob das Erlöskonto fehlt (dann wählt der Nutzer eines)
RebuildResult = namedtuple('RebuildResult', 'ok code message plan executed needs_revenue_coa')
# Eine Zuordnung, die sich neu aufbauen lässt (C): ihre ID, die Bankbewegung, der Betrag (Minor Units), die Notiz, das bisherige
# Zahlungsdatum der Zuordnung, das Datum der Bankbewegung und deren Bankkonto (``Accounts.ID``)
RebuildCandidate = namedtuple('RebuildCandidate', 'payment_id booking_id amount notes pay_date bank_date account_id')

REBUILD_NOT_FOUND = 'Rechnung nicht gefunden'
REBUILD_CHANGED_MESSAGE = ('Der Bestand hat sich seit dem Probelauf geändert – bitte das neue Ergebnis prüfen und erneut '
                           'bestätigen.')
REBUILD_CONFIRM_MISSING = 'Bitte zuerst den Probelauf ansehen und bestätigen.'
REBUILD_NOTHING_MESSAGE = 'Es gibt nichts, was sich automatisch neu aufbauen lässt.'
REBUILD_DONE_MESSAGE = 'Die Zuordnung wurde neu aufgebaut.'
REBUILD_UNSAFE_MESSAGE = ('Die Zuordnung lässt sich nicht automatisch neu aufbauen: Dabei hätte sich ein vorhandener '
                          'Buchungssatz geändert. Es wurde nichts geändert – bitte von Hand prüfen.')
REBUILD_WARNING = ('Die neuen Buchungssätze tragen das Datum der jeweiligen Bankbewegung. Einnahmen-Überschuss-Rechnung und '
                   'Umsatzsteuer dieses Zeitraums ändern sich so, wie die Tabelle es zeigt. Ist der Zeitraum schon gemeldet '
                   'oder an ein Steuerbüro übergeben (etwa per DATEV-Export), ist die Änderung dort nachzutragen. Bitte vorher '
                   'prüfen, dass Zahlung und Erlös dieser Rechnung nicht schon an anderer Stelle gebucht sind (etwa als eigener '
                   'Buchungssatz ohne Rechnungsnummer).')
# Warum etwas nicht angefasst wird: Schlüssel → Klartext. Platzhalter: {payment} (Zuordnung), {booking} (Bankbewegung)
REBUILD_SKIP_REASONS = {
    'storno_pair': 'Die Rechnung gehört zu einem Storno-Paar – Zahlungen und Erstattungen werden nicht automatisch neu aufgebaut.',
    'status': 'Bei diesem Status der Rechnung wird nichts neu aufgebaut (nur versendet, überfällig, teilweise oder ganz bezahlt).',
    'amount': 'Die Rechnung hat keinen positiven Betrag – es wird nichts neu aufgebaut.',
    'receivable': 'Die Forderung der Rechnung stammt nicht von der App oder steht mehrfach in den Büchern.',
    'elsewhere': ('Zu dieser Rechnung stehen schon Buchungssätze an anderer Stelle in den Büchern (eigenständig oder unter einer '
                  'anderen Bankbewegung) – der Erlös würde sonst doppelt gebucht. Bitte von Hand prüfen.'),
    'overbooked': ('Zu dieser Rechnung ist schon mehr Erlös gebucht, als ihre übrigen Zuordnungen erklären – der Erlös würde '
                   'sonst doppelt gebucht. Bitte von Hand prüfen.'),
    'not_bank': ('Zuordnung #{payment} hängt an einem Buchungssatz, nicht an einer Bankbewegung – bitte die Zuordnung '
                 'entfernen und die Bankbewegung zuordnen.'),
    'outgoing': 'Zuordnung #{payment} liegt auf einem Zahlungsausgang – bitte prüfen und die Zuordnung entfernen.',
    'own_rows': 'Unter Bewegung #{booking} stehen schon Buchungssätze zu dieser Rechnung; sie werden nicht neu geschrieben.',
    'foreign_rows': ('Unter Bewegung #{booking} stehen Buchungssätze, die keiner anderen zugeordneten Rechnung gehören – '
                     'bitte von Hand prüfen.'),
    'overbooked_movement': ('Unter Bewegung #{booking} ist für andere Rechnungen schon mehr Erlös gebucht, als ihnen dort '
                            'zugeordnet ist – bitte von Hand prüfen.'),
    'no_skr': 'Das Bankkonto der Bewegung #{booking} hat keine SKR-Nummer.',
    'duplicate_amounts': 'Bewegung #{booking} ist mehrfach mit verschiedenen Beträgen zugeordnet – bitte von Hand prüfen.',
    'cent': 'Zuordnung #{payment} hat einen Betrag mit Bruchteil eines Cents.',
    'not_free': ('Zuordnung #{payment} lässt sich nicht mit genau ihrem Betrag neu buchen (Rechnung oder Bankbewegung '
                 'ausgeschöpft).'),
}

# Leerraum am Rand einer Beleg-Nr., wie Pythons ``str.strip()`` ihn kennt: SQLites ``TRIM`` ohne zweites Argument nimmt nur das
# Leerzeichen, Tabulator, Zeilenumbruch und geschütztes Leerzeichen blieben stehen
_EDGE_WHITESPACE = ('\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f\x20\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009'
                    '\u200a\u2028\u2029\u202f\u205f\u3000')
# „Der Satz trägt die Nummer der Rechnung“: ohne Leerraum am Rand und ohne Rücksicht auf die Schreibweise (``LOWER`` kennt nur
# ASCII). Parameter in dieser Reihenfolge: ``_EDGE_WHITESPACE``, die Nummer der Rechnung, ``_EDGE_WHITESPACE``
_SAME_NUMBER_SQL = 'LOWER(TRIM(CAST(DocumentNumber AS TEXT), ?)) = LOWER(TRIM(?, ?))'


class _RebuildStop(Exception):
    """Beendet die Transaktion von ``rebuild_invoice_payments`` mit ROLLBACK und trägt das Ergebnis (Probelauf, Ablehnung)."""

    def __init__(self, result):
        super().__init__()
        self.result = result


def _euro(minor):
    return fmt_eur(from_minor(minor))


class InvoiceRepairMixin:
    # ── Öffentlich ───────────────────────────────────────────────────────────
    def rebuild_invoice_payments(self, invoice_id, dry_run=True, confirmed_check=None, revenue_coa_id=None, today=None):
        """Die Zahlungszuordnung einer Rechnung neu aufbauen (siehe Modul): als Probelauf (``dry_run``, Standard) oder mit
        ``confirmed_check`` = ``RebuildPlan.check`` des Probelaufs ausgeführt.

        Der Probelauf läuft denselben Code wie die Ausführung und rollt am Ende zurück: Er schreibt nichts, auch nicht ins
        Replay-Log. Die Ausführung schreibt nur, wenn der frisch gerechnete Plan dieselbe Prüfsumme hat; sonst (auch ohne
        ``confirmed_check``) kommt 409 mit dem Plan, und nichts ist geschrieben. Gibt es nichts zu tun, ist es 200 ohne Schreiben.

        ``revenue_coa_id``: das Erlöskonto, das der Nutzer wählt – nur wenn für den Steuersatz keines hinterlegt ist
        (``resolve_revenue_coa(…, create=False)``); es muss ein Erlöskonto sein. Fehlt es und braucht eine Zuordnung eines, ist das
        Ergebnis ``needs_revenue_coa``. ``today``: der Tag für den Status (ISO-Datum; Standard: heute).

        Returns :class:`RebuildResult`. Eine Ausnahme heißt: Datenbankfehler oder nicht erwartetes Verhalten – dann ist nichts
        geschrieben."""
        today = today or datetime.date.today().isoformat()
        ctx = self._rebuild_prerequisites(invoice_id, revenue_coa_id)
        if isinstance(ctx, RebuildResult):
            return ctx
        try:
            with self.transaction() as tx:
                tx.conn.execute('BEGIN IMMEDIATE')
                outcome = self._rebuild_in(tx, invoice_id, ctx, today)
                if isinstance(outcome, RebuildResult):
                    raise _RebuildStop(outcome)
                plan = outcome
                if dry_run:
                    raise _RebuildStop(RebuildResult(True, 200, None, plan, False, False))
                if not plan.actions:
                    raise _RebuildStop(RebuildResult(True, 200, REBUILD_NOTHING_MESSAGE, plan, False, False))
                if confirmed_check is None:
                    raise _RebuildStop(RebuildResult(False, 409, REBUILD_CONFIRM_MISSING, plan, False, False))
                if confirmed_check != plan.check:
                    raise _RebuildStop(RebuildResult(False, 409, REBUILD_CHANGED_MESSAGE, plan, False, False))
        except _RebuildStop as stop:
            return stop.result
        return RebuildResult(True, 200, REBUILD_DONE_MESSAGE, plan, True, False)

    # ── Vorbereitung (eigene Verbindungen, nichts wird geschrieben) ──────────
    def _rebuild_prerequisites(self, invoice_id, revenue_coa_id):
        """Was die Transaktion von außen braucht, gelesen in eigenen Verbindungen und ohne zu schreiben: das Erlöskonto der
        Rechnung (``None``, wenn keines hinterlegt ist und der Nutzer keines gewählt hat – nie wird eines angelegt), das SKR-Konto
        jedes Bankkontos ihrer Zuordnungen und die eigenen Bankkonten. Returns ein Kontext-dict oder ein :class:`RebuildResult`
        (Rechnung unbekannt, gewähltes Konto kein Erlöskonto)."""
        invoice = self.get_document(invoice_id, 'invoice')
        if invoice is None:
            return RebuildResult(False, 404, REBUILD_NOT_FOUND, None, False, False)
        revenue_coa = self.resolve_revenue_coa(invoice[35], create=False)        # Spalte 35: TaxRate
        if revenue_coa is None and revenue_coa_id not in (None, ''):
            revenue_coa, error = self._chosen_revenue_coa(revenue_coa_id)
            if error:
                return RebuildResult(False, 400, error, None, False, False)
        conn = self._get_connection()
        try:
            account_ids = [row[0] for row in conn.execute(
                'SELECT DISTINCT b.Account_ID FROM InvoicePayments ip JOIN Bookings b ON b.ID = ip.BookingID '
                'WHERE ip.InvoiceID = ? AND b.Account_ID IS NOT NULL ORDER BY b.Account_ID', (invoice_id,))]
        finally:
            conn.close()
        bank_coas = {}
        for account_id in account_ids:
            error, bank_coa = self._bank_coa_of(account_id)
            if not error:
                bank_coas[account_id] = bank_coa
        return {'revenue_coa': revenue_coa, 'bank_coas': bank_coas, 'bank_coa_ids': self.get_bank_coa_ids()}

    # ── Die Transaktion ──────────────────────────────────────────────────────
    def _rebuild_in(self, tx, invoice_id, ctx, today):
        """Prüfen, schreiben und den Plan bauen – in der Transaktion ``tx`` (``BEGIN IMMEDIATE``; der Aufrufer rollt zurück oder
        committet). Probelauf und Ausführung laufen genau diesen Code. ``ctx``: ``_rebuild_prerequisites``, ``today``: ISO-Datum.

        Die Zuordnungen werden zuerst nur gelesen und eingeordnet (A–C, jede Ablehnung steht damit vor dem ersten Schreiben);
        dann folgen A, B, C und D in dieser Reihenfolge. Nach dem Schreiben prüfen zwei Netze, dass sich kein vorhandener Satz
        verändert hat und die Prüfliste keine neue Kategorie nennt. Returns den :class:`RebuildPlan` oder ein
        :class:`RebuildResult` (Rechnung weg, Erlöskonto fehlt, vorhandener Satz verändert, neuer Befund)."""
        conn = tx.conn
        invoice = conn.execute(
            'SELECT InvoiceNumber, Status, SumGross, TaxRate, InvoiceKind, PaymentDueDate, AmountDue FROM Invoices '
            "WHERE ID = ? AND (DocumentType = 'invoice' OR DocumentType IS NULL)", (invoice_id,)).fetchone()
        if invoice is None:
            return RebuildResult(False, 404, REBUILD_NOT_FOUND, None, False, False)
        number, status, gross, rate, kind, due_date, due = invoice
        gross = gross or 0
        findings_before = [(f['category'], f['text']) for f in self._payment_findings(conn, invoice_id)]
        max_id = conn.execute('SELECT COALESCE(MAX(ID), 0) FROM Bookings').fetchone()[0]
        payments = conn.execute(
            'SELECT ID, BookingID, Amount, Notes, PaymentDate FROM InvoicePayments WHERE InvoiceID = ? ORDER BY PaymentDate, ID',
            (invoice_id,)).fetchall()
        skipped, actions = [], []

        def skip(key, **names):
            text = REBUILD_SKIP_REASONS[key].format(**names)
            if text not in skipped:
                skipped.append(text)

        # ── Einordnen (nur lesend) ───────────────────────────────────────────
        removals, locked, detach, candidates = [], set(), None, []
        paired = self._in_storno_pair(conn, invoice_id)
        if paired:
            skip('storno_pair')
        elif status not in REBUILD_STATUSES:
            skip('status')
        elif gross <= 0:
            skip('amount')
        else:
            removals, locked = self._rebuild_duplicates(payments, skip)
            removed = {removal[0] for removal in removals}
            receivables = self._receivable_rows(conn, number)
            waiting_ids = self._waiting_coa_ids(conn)
            numbers = dict(conn.execute('SELECT ID, AccountNumber FROM ChartOfAccounts'))
            receivable_ok = (len(receivables) == 1
                             and self._is_app_receivable(receivables[0][2], receivables[0][4], waiting_ids))
            if receivable_ok and receivables[0][1] is not None:
                detach = (receivables[0][0], receivables[0][1])                   # (Forderung, Bankbewegung)
            # Steht der Erlös der Rechnung schon an anderer Stelle (Sätze mit ihrer Nummer außerhalb ihrer Zuordnungen, oder ein
            # Befund „Zahlungssatz ohne Zuordnung“ der Prüfliste), baut C nichts auf: Er würde ein zweites Mal gebucht
            elsewhere = bool(payments) and (
                any(category == 'orphan_payment_rows' for category, _text in findings_before)
                or self._rows_elsewhere(conn, invoice_id, number, [row[0] for row in receivables]))
            candidates = self._rebuild_candidates(
                conn, invoice_id, number, [pay for pay in payments if pay[0] not in removed and pay[1] not in locked],
                ctx, receivables, receivable_ok, detach[0] if detach else None, elsewhere, numbers, waiting_ids, skip)
            # Buchen die vorhandenen Sätze der Rechnung mehr Erlös, als die Zuordnungen erklären, die bleiben (nicht zusammengefasst,
            # nicht neu gebaut), stünde der Erlös der Kandidatinnen nach dem Neuaufbau zusätzlich da
            if candidates and self._overbooked(conn, number, receivables, payments, removed, candidates, numbers, waiting_ids):
                skip('overbooked')
                candidates = []
            if candidates and ctx['revenue_coa'] is None:
                return RebuildResult(False, 400, NO_REVENUE_COA, None, False, True)

        # ── Schreiben ────────────────────────────────────────────────────────
        bank_ids = sorted({pay[1] for pay in payments} | ({detach[1]} if detach else set()))
        names, before = self._booking_snapshot(conn, number, bank_ids, max_id)
        for payment_id, booking_id, amount, keep_id in removals:                       # A
            self._unassign(conn, payment_id)
            tx.log.append(('DELETE FROM InvoicePayments WHERE ID = ?', (payment_id,), 'Rebuild: remove duplicate assignment'))
            actions.append(RebuildAction(
                'dedupe', payment_id, booking_id, amount,
                f'Doppelte Zuordnung #{payment_id} zu Bewegung #{booking_id} ({_euro(amount)}) wird entfernt; '
                f'Zuordnung #{keep_id} bleibt.'))
        if detach:                                                                     # B
            sql = ("UPDATE Bookings SET ParentBooking_ID = NULL, "
                   "Status = CASE WHEN Status = 'resolved' THEN NULL ELSE Status END WHERE ID = ?")
            conn.execute(sql, (detach[0],))
            tx.log.append((sql, (detach[0],), 'Rebuild: detach receivable'))
            amount = conn.execute('SELECT Amount FROM Bookings WHERE ID = ?', (detach[0],)).fetchone()[0]
            actions.append(RebuildAction(
                'detach', None, detach[1], amount,
                f'Die Forderung #{detach[0]} wird von der Bankbewegung #{detach[1]} gelöst; Datum, Konten und Betrag bleiben.'))
        # C in zwei Phasen, je Durchgang in EINEM Savepoint: erst alle Kandidatinnen entfernen, dann in zeitlicher Folge neu buchen.
        # So zählen beim Buchen einer Zuordnung nur die früheren und die nicht angefassten als bezahlt – wie beim Zuordnen
        # nacheinander: Die Steuer ist anteilig, und erst die letzte, die die Rechnung schließt, bekommt den Rest. (Einzeln
        # entfernen und sofort buchen ließe die übrigen als bezahlt zählen, und der Rest landete auf der zuerst gebauten.)
        # Die Folge ist die der Bankbewegungen (ihr Datum, dann die ID der Zuordnung) – das Datum, das jede neue Zuordnung als
        # Zahlungsdatum bekommt; das bisherige Zahlungsdatum einer Zuordnung ohne Sätze bestimmt die Reihenfolge nicht
        pending = sorted(candidates, key=lambda cand: (str(cand.bank_date or ''), cand.payment_id))
        while pending:                                                                 # C
            log_length = len(tx.log)
            conn.execute('SAVEPOINT rebuild_payments')
            for cand in pending:                                                       # Phase 1: alle entfernen
                self._unassign(conn, cand.payment_id)
                tx.log.append(('DELETE FROM InvoicePayments WHERE ID = ?', (cand.payment_id,), 'Rebuild: remove assignment'))
            failed = None
            for cand in pending:                                                       # Phase 2: in zeitlicher Folge neu buchen
                result = self._assign_in_tx(
                    tx, invoice_id, cand.booking_id, cand.amount,
                    {'tax_rate': rate, 'account_id': cand.account_id, 'bank_coa': ctx['bank_coas'][cand.account_id],
                     'revenue_coa': ctx['revenue_coa'], 'bank_coa_ids': ctx['bank_coa_ids']})
                stored = (conn.execute('SELECT Amount FROM InvoicePayments WHERE ID = ?', (result.payment_id,)).fetchone()
                          if result.ok else None)
                if stored is None or stored[0] != cand.amount:
                    failed = cand                                  # nicht mit genau ihrem Betrag neu zu buchen
                    break
                if cand.notes:                                     # die Notiz der alten Zuordnung geht nicht verloren
                    conn.execute('UPDATE InvoicePayments SET Notes = ? WHERE ID = ?', (cand.notes, result.payment_id))
                    tx.log.append(('UPDATE InvoicePayments SET Notes = ? WHERE InvoiceID = ? AND BookingID = ?',
                                   (cand.notes, invoice_id, cand.booking_id), 'Rebuild: keep note of the assignment'))
            if failed is None:
                conn.execute('RELEASE rebuild_payments')
                actions += [self._rebuild_action(cand) for cand in pending]
                break
            # Der ganze Durchgang geht zurück – Zuordnungen, Sätze, Rechnung und Log-Einträge –, die nicht passende bleibt samt ID
            # liegen, und die übrigen laufen ohne sie noch einmal (höchstens so viele Durchgänge, wie es Kandidatinnen gibt)
            conn.execute('ROLLBACK TO rebuild_payments')
            conn.execute('RELEASE rebuild_payments')
            del tx.log[log_length:]
            skip('not_free', payment=failed.payment_id)
            pending.remove(failed)

        # ── D: offener Betrag und Status ─────────────────────────────────────
        # Geschrieben wird, wenn A–C etwas geschrieben haben (ihre Bausteine rechnen die Rechnung zwischendurch mit der
        # Uhr von jetzt neu – hier kommt der Status zurück, den sie hatte) oder wenn der Rest nicht zu den Zuordnungen
        # passt; eine Überzahlung (Rest unter 0, keine Stornorechnung) bleibt ein Befund
        paid = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                            (invoice_id,)).fetchone()[0]
        due_after = gross - paid
        status_after = next_payment_status(status, gross, paid, due_date, today) if removals else status
        if actions or (due_after != due and (due_after >= 0 or kind == STORNO_KIND)):
            conn.execute('UPDATE Invoices SET AmountDue = ?, Status = ?, UpdatedAt = CURRENT_TIMESTAMP WHERE ID = ?',
                         (due_after, status_after, invoice_id))
        else:
            due_after, status_after = due, status
        if (due_after, status_after) != (due, status):
            text = f'Offener Betrag {_euro(due)} → {_euro(due_after)}'
            if status_after != status:
                text += f'; Status {status} → {status_after}'
            actions.append(RebuildAction('recalc', None, None, due_after, text))

        if not self._booking_snapshot_unchanged(conn, number, bank_ids, max_id, names, before,
                                                {detach[0]} if detach else set()):
            return RebuildResult(False, 409, REBUILD_UNSAFE_MESSAGE, None, False, False)
        # Das zweite Netz: Die Reparatur darf keinen Befund erzeugen. Steht danach eine Kategorie in der Prüfliste, die vorher nicht
        # darin stand (etwa ``transfer_mismatch``: der Erlös stünde doppelt da), wird alles zurückgenommen – wie beim Schutz oben.
        # Die Kategorien stehen nur im Server-Log, nie in der Antwort.
        findings_after = [(f['category'], f['text']) for f in self._payment_findings(conn, invoice_id)]
        introduced = {category for category, _text in findings_after} - {category for category, _text in findings_before}
        if introduced:
            print(f"Zuordnung neu aufbauen: Rechnung {invoice_id} – nach dem Schreiben neue Befunde ({', '.join(sorted(introduced))}); "
                  'alles wurde zurückgenommen.')
            return RebuildResult(False, 409, REBUILD_UNSAFE_MESSAGE, None, False, False)
        # Der Tag geht nur in die Prüfsumme ein, wenn Dubletten zusammengefasst werden: Nur dann hängt der Status von ihm ab
        return self._rebuild_plan(conn, invoice_id, number, actions, skipped, max_id, status, status_after, due, due_after,
                                  findings_before, findings_after, ctx['revenue_coa'] if candidates else None,
                                  today if removals else None)

    @staticmethod
    def _rebuild_duplicates(payments, skip):
        """A (nur lesend): die doppelten Zuordnungen eines Paares (Rechnung, Bewegung) mit gleichem Betrag – ``[(ID der
        überzähligen Zuordnung, Bewegung, Betrag, ID der bleibenden)]`` (die kleinste ID bleibt), dazu die Bewegungen, deren
        mehrfache Zuordnung verschiedene Beträge hat (``locked``: sie bleiben, mit Meldung, und sind für C gesperrt)."""
        by_booking = defaultdict(list)
        for payment_id, booking_id, amount, _notes, _pay_date in payments:
            by_booking[booking_id].append((payment_id, amount))
        removals, locked = [], set()
        for booking_id in sorted(by_booking):
            rows = sorted(by_booking[booking_id])
            if len(rows) < 2:
                continue
            if len({amount for _payment_id, amount in rows}) == 1:
                removals += [(payment_id, booking_id, amount, rows[0][0]) for payment_id, amount in rows[1:]]
            else:
                locked.add(booking_id)
                skip('duplicate_amounts', booking=booking_id)
        return removals, locked

    @staticmethod
    def _rows_elsewhere(conn, invoice_id, number, receivable_ids):
        """Steht zur Rechnung schon etwas an anderer Stelle in den Büchern? Ja, wenn es einen Buchungssatz gibt, der keine
        Bankzeile ist, die Nummer der Rechnung trägt (ohne Leerraum am Rand – wie ``str.strip()`` ihn kennt – und ohne Rücksicht
        auf die Groß-/Kleinschreibung, auch ``SourceGroup`` und Kasse zählen), nicht ihre Forderung ist (``receivable_ids``) und
        nicht unter einer Bankbewegung steht, der diese Rechnung zugeordnet ist – also eigenständig oder unter einer fremden
        Bewegung. Neu aufgebaut würde der Erlös dann ein zweites Mal gebucht. Nur lesend; ``DocumentNumber`` hat keinen Index, die
        Abfrage liest die Tabelle einmal."""
        marks = ','.join('?' * len(receivable_ids))
        return conn.execute(
            "SELECT 1 FROM Bookings WHERE COALESCE(BookingType, '') <> 'bank' "
            f'AND {_SAME_NUMBER_SQL} AND ID NOT IN ({marks}) '
            'AND (ParentBooking_ID IS NULL OR ParentBooking_ID NOT IN (SELECT BookingID FROM InvoicePayments WHERE InvoiceID = ?)) '
            'LIMIT 1', [_EDGE_WHITESPACE, number, _EDGE_WHITESPACE, *receivable_ids, invoice_id]).fetchone() is not None

    @staticmethod
    def _revenue_booked(rows, numbers, waiting_ids):
        """Wie viel Erlös buchen diese Sätze? Die Summe der Beträge (Minor Units, ohne Vorzeichen) der Sätze, die auf einer Seite
        ein Erlöskonto führen (``is_income_account``, ohne die Wartekonten ``waiting_ids``): „Wartekonto an Erlöskonto“ und „Erlöskonto
        an Bank“ zählen je einmal – auch wenn beide Seiten Erlöskonten wären –, „Bank an Debitoren“ nie. ``rows``: ``(COA_ID,
        CounterCOA_ID, Amount)``; ``numbers``: Konto-ID → Kontonummer. Ist der Betrag eines zählenden Satzes keine ganze Zahl (Text,
        Kommazahl, Wahrheitswert), ergibt es ``None``: unbekannt, der Aufrufer sperrt dann."""
        total = 0
        for coa_id, counter_id, amount in rows:
            if not any(account not in waiting_ids and is_income_account(numbers.get(account)) for account in (coa_id, counter_id)):
                continue
            if isinstance(amount, float) and amount.is_integer():
                amount = int(amount)
            if isinstance(amount, bool) or not isinstance(amount, int):
                return None
            total += abs(amount)
        return total

    def _overbooked(self, conn, number, receivables, payments, removed, candidates, numbers, waiting_ids):
        """Buchen die vorhandenen Sätze der Rechnung mehr Erlös, als ihre übrigen Zuordnungen erklären? Dann stünde der Erlös der
        Kandidatinnen nach dem Neuaufbau zusätzlich da, und C baut nichts (Grund ``overbooked``). Gezählt wird der Erlös
        (``_revenue_booked``) aller Sätze, die keine Bankzeile sind, die Nummer der Rechnung tragen (``_SAME_NUMBER_SQL``) und nicht
        ihre Forderung sind (``receivables``) – nach ``elsewhere`` stehen sie alle unter einer Bewegung, der die Rechnung zugeordnet
        ist –, gegen die Summe der Beträge ihrer Zuordnungen, die bleiben: nicht die zusammengefassten (``removed``: Zuordnungs-IDs),
        nicht die der Kandidatinnen, eine negative mindert. Nur lesend. Ein Betrag, der keine ganze Zahl ist, sperrt."""
        marks = ','.join('?' * len(receivables))
        rows = conn.execute(
            "SELECT COA_ID, CounterCOA_ID, Amount FROM Bookings WHERE COALESCE(BookingType, '') <> 'bank' "
            f'AND {_SAME_NUMBER_SQL} AND ID NOT IN ({marks})',
            [_EDGE_WHITESPACE, number, _EDGE_WHITESPACE, *[row[0] for row in receivables]]).fetchall()
        booked = self._revenue_booked(rows, numbers, waiting_ids)
        rebuilt = {cand.payment_id for cand in candidates}
        explained = sum(amount for payment_id, _booking_id, amount, _notes, _pay_date in payments
                        if payment_id not in removed and payment_id not in rebuilt)
        return booked is None or booked > explained

    def _overbooked_movement(self, conn, invoice_id, booking_id, rows, numbers, waiting_ids):
        """Buchen die Sätze unter der Bewegung (``rows``: ``(ID, Beleg-Nr., Quellgruppe, Gegenkonto, Konto, Betrag)``, ohne die
        gelöste Forderung) mehr Erlös, als den anderen Rechnungen dort zugeordnet ist (Grund ``overbooked_movement``)? Dann
        deckt ein Satz mehr als die Zuordnung seiner Rechnung, und der Neuaufbau dieser Rechnung buchte ihren Erlös zusätzlich.
        Nur lesend; ein Betrag, der keine ganze Zahl ist, sperrt."""
        booked = self._revenue_booked([(row[4], row[3], row[5]) for row in rows], numbers, waiting_ids)
        others_total = conn.execute('SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE BookingID = ? AND InvoiceID <> ?',
                                    (booking_id, invoice_id)).fetchone()[0]
        return booked is None or booked > others_total

    def _rebuild_candidates(self, conn, invoice_id, number, payments, ctx, receivables, receivable_ok, detached_id, elsewhere,
                            numbers, waiting_ids, skip):
        """C (nur lesend): die Zuordnungen, die sich neu aufbauen lassen (Bedingungen im Modul), als :class:`RebuildCandidate` in
        der Reihenfolge von ``payments``. Was sich nicht aufbauen lässt, steht – wenn dazu ein Befund offen ist – mit dem ersten
        Grund in ``skip``. ``detached_id``: die Forderung, die B löst; sie zählt nicht als Satz unter der Bewegung.
        ``elsewhere``: Die Rechnung hat Sätze an anderer Stelle (``_rows_elsewhere`` oder ein Befund ``orphan_payment_rows``) –
        dann ist keine ihrer Zuordnungen eine Kandidatin. ``numbers`` (Konto-ID → Kontonummer) und ``waiting_ids``: für die Summe
        je Bewegung (``_overbooked_movement``)."""
        debtor_ids = self._debtor_coa_ids(conn)
        candidates = []
        for payment_id, booking_id, amount, notes, pay_date in payments:
            bank = conn.execute("SELECT BookingType, Amount, Account_ID, DateBooking, COALESCE(SourceGroup, '') FROM Bookings "
                                'WHERE ID = ?', (booking_id,)).fetchone()
            if bank is None or bank[0] != 'bank':
                if bank is not None and not bank[4]:                       # ein Satz des Imports (Barzahlung) ist kein Befund
                    skip('not_bank', payment=payment_id, booking=booking_id)
                continue
            bank_amount = bank[1] or 0
            if amount > 0 and bank_amount < 0:
                skip('outgoing', payment=payment_id, booking=booking_id)
                continue
            if amount <= 0 or bank_amount <= 0:
                continue
            rows = [row for row in conn.execute(
                "SELECT ID, COALESCE(DocumentNumber, ''), COALESCE(SourceGroup, ''), CounterCOA_ID, COA_ID, Amount FROM Bookings "
                'WHERE ParentBooking_ID = ?', (booking_id,)) if row[0] != detached_id]
            if any(row[2] for row in rows):
                continue                                      # Sätze eines Imports: nie anfassen, und kein Befund der Prüfliste
            own = [row for row in rows if row[1] == number]
            if own:
                if receivables and not any(row[3] in debtor_ids for row in own):
                    skip('own_rows', payment=payment_id, booking=booking_id)
                continue
            if not receivables:
                continue                                      # ohne Forderung kein Befund „Bank an Debitoren fehlt“
            others = {row[0] for row in conn.execute(
                'SELECT i.InvoiceNumber FROM InvoicePayments ip JOIN Invoices i ON i.ID = ip.InvoiceID '
                'WHERE ip.BookingID = ? AND ip.InvoiceID <> ?', (booking_id, invoice_id))}
            # Die Gründe, die die ganze Rechnung betreffen (die Forderung, Sätze an anderer Stelle), vor denen, die nur diese
            # Bewegung betreffen
            if not receivable_ok:
                skip('receivable')
            elif elsewhere:
                skip('elsewhere')
            elif any(not row[1] or row[1] not in others for row in rows):
                skip('foreign_rows', payment=payment_id, booking=booking_id)
            elif self._overbooked_movement(conn, invoice_id, booking_id, rows, numbers, waiting_ids):
                skip('overbooked_movement', booking=booking_id)
            elif bank[2] not in ctx['bank_coas']:
                skip('no_skr', payment=payment_id, booking=booking_id)
            elif amount % 100:
                skip('cent', payment=payment_id, booking=booking_id)
            else:
                candidates.append(RebuildCandidate(payment_id, booking_id, amount, notes, pay_date, bank[3], bank[2]))
        return candidates

    @staticmethod
    def _rebuild_action(cand):
        """Die Aktion ``rebuild`` zu einer Kandidatin. Das Zahlungsdatum der neuen Zuordnung ist immer das Datum der Bankbewegung
        (``_assign_in_tx``): Weicht das bisherige ab, nennt der Text es – das alte Datum nur, wenn es ein ISO-Datum ist."""
        text = (f'Zuordnung #{cand.payment_id} ({_euro(cand.amount)}) zu Bewegung #{cand.booking_id} vom {cand.bank_date} wird mit '
                'den Sätzen „Bank an Debitoren“ und „Wartekonto an Erlöskonto“ neu geschrieben.')
        if cand.pay_date != cand.bank_date:
            text += ' Das Zahlungsdatum der Zuordnung wird das Datum der Bankbewegung'
            text += f' (bisher {cand.pay_date}).' if is_iso_date(cand.pay_date) else '.'
        return RebuildAction('rebuild', cand.payment_id, cand.booking_id, cand.amount, text)

    # ── Schutz: kein vorhandener Buchungssatz ändert sich ────────────────────
    @staticmethod
    def _booking_snapshot(conn, number, bank_ids, max_id):
        """Die vorhandenen Buchungssätze (ID ≤ ``max_id``), an die ein Baustein der Neubuchung rühren könnte – die der Rechnung
        (Beleg-Nr.), die Bankbewegungen ``bank_ids`` und ihre Kinder – als ``(Spaltennamen, {ID: Zeile})``."""
        where, params = 'DocumentNumber = ?', [number]
        if bank_ids:
            marks = ','.join('?' * len(bank_ids))
            where += f' OR ParentBooking_ID IN ({marks}) OR ID IN ({marks})'
            params += bank_ids * 2
        cursor = conn.execute(f'SELECT * FROM Bookings WHERE ID <= ? AND ({where})', [max_id, *params])
        return [column[0] for column in cursor.description], {row[0]: row for row in cursor}

    def _booking_snapshot_unchanged(self, conn, number, bank_ids, max_id, names, before, detached):
        """Sind die Sätze aus ``_booking_snapshot`` noch genau so da – keiner gelöscht, keiner geändert? Ausgenommen: die gelöste
        Forderung (``detached``) in ``ParentBooking_ID`` und ``Status``."""
        _names, after = self._booking_snapshot(conn, number, bank_ids, max_id)
        if before.keys() != after.keys():
            return False
        loose = {names.index('ParentBooking_ID'), names.index('Status')}
        for booking_id, row in before.items():
            now = after[booking_id]
            if booking_id in detached:
                if any(was != has for index, (was, has) in enumerate(zip(row, now)) if index not in loose):
                    return False
            elif row != now:
                return False
        return True

    # ── Plan und Prüfsumme ───────────────────────────────────────────────────
    def _rebuild_plan(self, conn, invoice_id, number, actions, skipped, max_id, status_before, status_after, due_before,
                      due_after, findings_before, findings_after, revenue_coa, today):
        """Der Plan aus dem, was die Transaktion geschrieben hat: die neuen Sätze (ID > ``max_id``), ihre Wirkung auf die EÜR,
        die Prüfliste danach (``findings_after``, vom Aufrufer gelesen) und die Prüfsumme. Sie hängt nie an der ID eines neuen
        Satzes, an ``UpdatedAt`` oder an der Uhrzeit – nur an dem, was ein Probelauf zeigt, und an ``today``, wenn der Aufrufer es
        nennt (``None`` sonst: nur das Zusammenfassen von Dubletten rechnet den Status mit dem Tag)."""
        numbers = dict(conn.execute('SELECT ID, AccountNumber FROM ChartOfAccounts'))
        rows, euer = [], defaultdict(lambda: [0, 0])
        for values in conn.execute(f'SELECT {", ".join(PAYMENT_ROW_COLUMNS)} FROM Bookings WHERE ID > ? ORDER BY ID', (max_id,)):
            record = dict(zip(PAYMENT_ROW_COLUMNS, values))
            account, counter = numbers.get(record['COA_ID']), numbers.get(record['CounterCOA_ID'])
            euer_account = next((nr for nr in (account, counter) if nr is not None and is_income_account(nr)), None)
            rows.append(RebuildRow(record['ParentBooking_ID'], record['DateBooking'], account, counter, record['Amount'],
                                   record['TaxRate'], record['TaxAmount'], record['Text'], record['EntryForm'], euer_account,
                                   tuple(values)))
            if euer_account is not None:
                tax = record['TaxAmount'] or 0
                effect = euer[(record['DateBooking'], euer_account)]
                effect[0] += record['Amount'] - tax
                effect[1] += tax
        effects = [EuerEffect(date, account, net, tax) for (date, account), (net, tax) in sorted(euer.items())]
        payload = {
            'invoice_id': invoice_id, 'invoice_number': number,
            'actions': [action._asdict() for action in actions],
            'rows': [row._asdict() for row in rows], 'euer': [effect._asdict() for effect in effects],
            'status_before': status_before, 'status_after': status_after, 'due_before': due_before, 'due_after': due_after,
            'findings_before': findings_before, 'findings_after': sorted(category for category, _text in findings_after),
            'skipped': skipped, 'revenue_coa': revenue_coa, 'today': today}
        check = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                          separators=(',', ':')).encode('utf-8')).hexdigest()
        return RebuildPlan(invoice_id, number, actions, rows, effects, status_before, status_after, due_before, due_after,
                           findings_before, findings_after, skipped, revenue_coa, check)
