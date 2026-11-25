# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: payment_suggestions – Vorschläge Zahlungseingang ↔ offene Rechnung (seit 2.8.1), rein lesend.

Die Rangfolge steht in ``db/payment_matching.py`` (rein); dieses Modul liest die Zeilen dafür und setzt beides zusammen. Es wird nichts
geschrieben und nichts zugeordnet – zugeordnet wird allein auf Klick, über ``assign_invoice_payment`` (``POST /invoice/link-payment``, Feld
``invoice_id`` der Maske der Bankbewegung).

* **Offene Rechnungen** (``_open_invoices``): eine Rechnung (kein Angebot, keine Stornorechnung) in einem zahlbaren Status
  (``PAYABLE_STATUSES``) mit offenem Betrag größer 0 – der Betrag, den auch die Maske zeigt: ``AmountDue``, sonst der Brutto. Höchstens
  ``MAX_INVOICES``, die jüngsten zuerst. **Skonto (seit 2.9.0):** Dieselbe Abfrage liest die Angaben, die darüber entscheiden, ob die Rechnung ein Skonto zulässt (Brutto, Netto und Steuer, Skonto-Bedingungen,
  Satz im Kopf, das Kennzeichen „Steuersatz je Position“, ob eine Position als Erlöskonto aus den Stammdaten selbst ein Skonto-Konto trägt – ein ``EXISTS`` je Rechnung über den Index auf ``InvoiceItems``);
  für die Rechnungen mit dem Kennzeichen liest EINE weitere Abfrage die Positionen aller (höchstens ``MAX_INVOICES``), daraus entstehen die Sätze der Gruppen (``tax_groups.mixed_groups``; passen die
  Positionen nicht zu den Summen, gibt es keine Sätze – die Rechnung lässt dann kein Skonto zu); gerechnet wird mit den Regeln des Zuordnens (``invoice_payments.skonto_terms``, ``skonto_open_minor``),
  nie mit eigenen: ``skonto_open_minor`` ist der offene Betrag abzüglich des Skontos (das Signal SKONTO der Vorschläge), ``skonto_limit_minor`` das Skonto selbst (für die Häkchen der Seiten); ohne
  Skonto beides ``None``. Eine Rechnung mit unbrauchbaren Werten ist kein Fehler, sie hat kein Skonto. **Bekannte Grenze:** Ein Widerspruch der Momentaufnahme der Stammdaten, eine Gruppe ohne
  positives Brutto und Vorzahlungen, die sich nicht auf die Sätze aufteilen lassen, sieht erst das Zuordnen (``SKONTO_MIXED_SPLIT``, ``TAX_ENTRY_CONTRADICTION`` …): Häkchen und Vorschlag erscheinen
  dann trotzdem, der Klick lehnt mit einem festen Satz ab.
* **Bewegungen mit freiem Rest.** Für EINE Bankbewegung (die Maske, ``suggest_invoices_for_booking``) gilt der freie Rest nach derselben Formel
  wie überall (``_free_minor_sql``: Betrag − MAX(Σ Zuordnungen, Σ bankwirksame Sätze darunter), wie ``get_unallocated_bank_bookings``). Für die
  Listen (die Übersicht der Seite *Buchungen*, die Rechnungsseite) wäre diese Formel für JEDEN Zahlungseingang des Bestands zu lesen: Gemessen an
  einem Bestand mit 200 000 Buchungen (60 000 Bankbewegungen, 3 700 Zuordnungen) kostete das gut 4 Sekunden je Aufruf – ``InvoicePayments`` hat
  keinen Index auf ``BookingID``, jede Bewegung liest die ganze Tabelle. Die Listen nehmen deshalb die billigere Auswahl
  (``_unbooked_movements``): Bankbewegungen mit Betrag größer 0, **ohne Kind und ohne Zuordnung** (der freie Rest ist dort der Betrag) – das ist,
  was ein frischer Import liefert; gemessen rund 15 ms. Eine Bewegung, die schon teilweise kontiert oder zugeordnet ist, wird dort nicht
  vorgeschlagen (sie steht weiter in den Listen der Seiten, die den freien Rest mit der Formel lesen). Höchstens ``MAX_MOVEMENTS``, die jüngsten zuerst.
* Ein Paar, das schon in ``InvoicePayments`` steht, ist nie ein Vorschlag (``linked`` der reinen Funktionen).

Eine Zeile mit einem Betrag, der keine ganze Zahl ist (Text, Kommazahl, NULL), ist kein Kandidat; ein unlesbares Datum oder ein Text, der kein Text
ist, wirft nie (die reinen Funktionen lesen sie als „kein Signal“). Ein Fehler der Datenbank selbst geht weiter."""
from collections import namedtuple

from tax_groups import RateGroupsMismatch, mixed_groups
from .invoice_payments import (PAYABLE_STATUSES, SKONTO_ACCOUNT_NUMBERS, SKONTO_NO_TERMS, STORNO_KIND, has_mixed_rates, skonto_open_minor, skonto_terms)
from .payment_matching import pair_up, rank_invoices, rank_movements

MAX_INVOICES = 500       # so viele offene Rechnungen liest eine Abfrage höchstens (die jüngsten)
MAX_MOVEMENTS = 500      # … und so viele Bewegungen (die jüngsten)

# Das Ergebnis der Übersicht: die Vorschläge (höchstens ``limit``) und wie viele weitere es gäbe
PaymentOverview = namedtuple('PaymentOverview', 'items more')

# Was eine Rechnung an Skonto zulässt (``Database.skonto_offer``): der Fehlertext der Ablehnung (``invoice_payments.SKONTO_*``) und sonst die Grenze (Minor Units: das Skonto, das die Rechnung
# vorsieht), dazu die Bedingungen, wie sie gelten (Tage, Prozent). Bei einer Ablehnung sind Grenze, Tage und Prozent ``None``; ``SKONTO_NO_TERMS`` heißt: Die Rechnung sieht kein Skonto vor
SkontoOffer = namedtuple('SkontoOffer', 'error limit days percent')

# Offene Rechnungen. Platzhalter: die Kontonummern der Skonto-Konten (``SKONTO_ACCOUNT_NUMBERS``, im ``EXISTS`` der Auswahlliste), der Wert von ``Invoices.InvoiceKind`` einer Stornorechnung, dann die
# zahlbaren Status. Ein Altbestand ohne ``DocumentType`` zählt als Rechnung (wie ``fetch_invoices``); ``COALESCE(AmountDue, SumGross, 0)`` ist der offene Betrag der Maske. Die letzte Spalte: Trägt eine
# Position als Erlöskonto aus den Stammdaten selbst ein Skonto-Konto (dieselbe Bedingung wie ``InvoicePaymentsMixin._revenue_on_discount``)?
_OPEN_INVOICES = f'''
    SELECT i.ID, i.InvoiceNumber, i.InvoiceDate, COALESCE(i.AmountDue, i.SumGross, 0), i.CustomerId, i.BuyerCompany, i.BuyerName,
           i.SumGross, i.SkontoDays, i.SkontoPercent, i.TaxRate, i.MixedTaxRates, i.SumNet, i.TaxAmount,
           EXISTS (SELECT 1 FROM InvoiceItems it JOIN ChartOfAccounts c ON c.ID = it.RevenueCoaId
                   WHERE it.InvoiceId = i.ID AND c.AccountNumber IN ({','.join('?' * len(SKONTO_ACCOUNT_NUMBERS))}))
    FROM Invoices i
    WHERE (i.DocumentType = 'invoice' OR i.DocumentType IS NULL)
      AND i.InvoiceKind IS NOT ?
      AND i.Status IN ({','.join('?' * len(PAYABLE_STATUSES))})
      AND typeof(COALESCE(i.AmountDue, i.SumGross, 0)) = 'integer'
      AND COALESCE(i.AmountDue, i.SumGross, 0) > 0
      AND UPPER(TRIM(COALESCE(i.Currency, ''))) IN ('', 'EUR')'''

# Bewegungen ohne Kind und ohne Zuordnung, in Euro. Die Zuordnungen werden EINMAL gelesen (``NOT IN`` über eine Unterabfrage ohne Bezug zur
# Zeile): ``InvoicePayments`` hat keinen Index auf ``BookingID``, eine Unterabfrage je Bewegung läse die ganze Tabelle für jede unkontierte
# Bewegung neu (gemessen an 20 000 unkontierten Eingängen und 3 700 Zuordnungen: 1,2 s statt 28 ms). Der freie Rest ist der Betrag.
_UNBOOKED_MOVEMENTS = '''
    SELECT b.ID, b.DateBooking, b.RecipientClient, b.Text, b.DocumentNumber, b.Contact_ID, b.Amount, b.Amount
    FROM Bookings b
    WHERE b.BookingType = 'bank' AND typeof(b.Amount) = 'integer' AND b.Amount > 0
      AND UPPER(TRIM(COALESCE(b.Currency, ''))) IN ('', 'EUR')
      AND NOT EXISTS (SELECT 1 FROM Bookings k WHERE k.ParentBooking_ID = b.ID)
      AND b.ID NOT IN (SELECT ip.BookingID FROM InvoicePayments ip WHERE ip.BookingID IS NOT NULL)
    ORDER BY b.DateBooking DESC, b.ID DESC
    LIMIT ?'''


def _name(value) -> str:
    return '' if value is None else str(value).strip()


def _skonto_offer(gross, days, percent, rate, mixed, group_pcts=None, revenue_on_discount=False) -> SkontoOffer:
    """Was die Rechnung an Skonto zulässt – die Regeln des Zuordnens (``invoice_payments.skonto_terms``), mit den Werten, wie die Tabelle sie trägt. Ein Wert, der sich nicht lesen
    lässt (Text statt Zahl, per SQL-Konsole geschrieben), ist nie ein Fehler: Die Rechnung sieht dann kein Skonto vor (``SKONTO_NO_TERMS``). ``gross``: ``Invoices.SumGross`` – nur eine ganze
    Zahl zählt, sonst 0 (dann ist auch das Skonto 0, und die Grenze fehlt). ``group_pcts``: bei einem Beleg mit Steuersatz je Position die Sätze seiner Gruppen (``None``: nicht lesbar),
    ``revenue_on_discount``: Trägt eine Position ein Skonto-Konto als Erlöskonto?"""
    from export.einvoice_model import parse_skonto          # ``export`` importiert diese Schicht: erst zur Laufzeit
    try:
        error, terms = skonto_terms(gross if type(gross) is int else 0, days, percent, rate, mixed, group_pcts, bool(revenue_on_discount))
        if error is not None:
            return SkontoOffer(error, None, None, None)
        parsed = parse_skonto(days, percent)
    except (ArithmeticError, TypeError, ValueError):
        return SkontoOffer(SKONTO_NO_TERMS, None, None, None)
    return SkontoOffer(None, terms[1], parsed[0], parsed[1])


def _invoice_dict(row, group_pcts=None) -> dict:
    """Eine Zeile von ``_OPEN_INVOICES`` als Eingabe der reinen Funktionen. ``customer_name``: die Firma des Käufers, sonst sein Name – zum
    Vergleichen der Namenswörter (nicht beides: Der Vorname eines Ansprechpartners ist kein Beweis für den Kunden); ``label``: dasselbe für die Seiten.
    ``group_pcts``: die Sätze der Gruppen, wenn die Rechnung das Kennzeichen „Steuersatz je Position“ trägt (``PaymentSuggestionsMixin._mixed_group_pcts``; ``None``: nicht lesbar).
    ``skonto_open_minor``: der offene Betrag abzüglich des Skontos (``invoice_payments.skonto_open_minor``), ``skonto_limit_minor``: das Skonto, das die Rechnung vorsieht – beides ``None``,
    wo sie keines zulässt (oder es 0 ist: ein Skonto, das es nicht gibt, bucht nichts)."""
    ident, number, date, open_minor, customer_id, company, name, gross, days, percent, rate, mixed, _net, _tax, on_discount = row
    company, name = _name(company), _name(name)
    gross = gross if type(gross) is int else 0
    offer = _skonto_offer(gross, days, percent, rate, mixed, group_pcts, on_discount)
    try:
        skonto_open = skonto_open_minor(open_minor, gross, days, percent, rate, mixed, group_pcts, bool(on_discount))
    except (ArithmeticError, TypeError, ValueError):
        skonto_open = None
    return {'id': ident, 'number': number, 'date': date, 'open_minor': open_minor, 'customer_id': customer_id,
            'customer_name': company or name, 'label': company or name,
            'skonto_open_minor': skonto_open, 'skonto_limit_minor': offer.limit if offer.limit else None}


def _movement_dicts(rows) -> list:
    """Zeilen ``(ID, Datum, Empfänger, Text, Beleg-Nr., Kontakt, Betrag, freier Rest)`` als Eingabe der reinen Funktionen (``amount_minor``: der
    FREIE Rest, ``total_minor``: der Betrag der Bewegung). Ein freier Rest, der keine ganze Zahl ist, macht die Zeile zu keinem Kandidaten."""
    return [{'id': ident, 'date': date, 'recipient': recipient, 'text': text, 'document_number': number, 'contact_id': contact,
             'amount_minor': free, 'total_minor': amount}
            for ident, date, recipient, text, number, contact, amount, free in rows if type(free) is int]


class PaymentSuggestionsMixin:
    def _open_invoices(self, conn, limit=MAX_INVOICES, invoice_id=None):
        """Die offenen Rechnungen als ``dict`` (``id``, ``number``, ``date``, ``open_minor``, ``customer_id``, ``customer_name``, ``label``), die
        jüngsten zuerst, höchstens ``limit``; mit ``invoice_id`` nur diese eine (wenn sie offen ist)."""
        sql, params = _OPEN_INVOICES, [*sorted(SKONTO_ACCOUNT_NUMBERS), STORNO_KIND, *PAYABLE_STATUSES]
        if invoice_id is not None:
            sql += ' AND i.ID = ?'
            params.append(invoice_id)
        rows = conn.execute(sql + ' ORDER BY i.InvoiceDate DESC, i.ID DESC LIMIT ?', params + [limit]).fetchall()
        group_pcts = self._mixed_group_pcts(conn, rows)
        return [_invoice_dict(row, group_pcts.get(row[0])) for row in rows]

    @staticmethod
    def _mixed_group_pcts(conn, rows):
        """Die Sätze der Gruppen der Rechnungen unter ``rows`` (Zeilen von ``_OPEN_INVOICES``), die das Kennzeichen „Steuersatz je Position“ tragen: ``{ID der Rechnung: (Satz, …)}`` nach Satz
        absteigend, ``None`` statt der Sätze, wenn sich die Positionen nicht zu den Summen der Rechnung gruppieren lassen (``tax_groups.mixed_groups`` – wie beim Zuordnen, ``_mixed_groups``).
        EINE Abfrage über die Positionen aller dieser Rechnungen (höchstens ``MAX_INVOICES`` je Abfrage); kaputte Werte sind kein Fehler: Die Rechnung hat dann keine Sätze und kein Skonto."""
        mixed = [row for row in rows if has_mixed_rates(row[11])]
        positions = {}
        ids = [row[0] for row in mixed]
        for start in range(0, len(ids), MAX_INVOICES):
            chunk = ids[start:start + MAX_INVOICES]
            for invoice_id, rate, net, bp in conn.execute(
                    'SELECT p.InvoiceId, p.TaxRate, p.TotalNet, p.TaxRateBp FROM InvoiceItems p '
                    f'WHERE p.InvoiceId IN ({",".join("?" * len(chunk))}) ORDER BY p.InvoiceId, p.Position, p.ID', chunk):
                positions.setdefault(invoice_id, []).append((rate, net, bp))
        found = {}
        for row in mixed:
            try:
                found[row[0]] = tuple(group.pct for group in mixed_groups(row[12], row[13], row[7], positions.get(row[0], [])))
            except (RateGroupsMismatch, ArithmeticError, TypeError, ValueError):
                found[row[0]] = None
        return found

    def _unbooked_movements(self, conn, limit=MAX_MOVEMENTS):
        """Die Bankbewegungen mit Betrag größer 0, ohne Kind und ohne Zuordnung, als ``dict`` (``id``, ``date``, ``recipient``, ``text``,
        ``document_number``, ``contact_id``, ``amount_minor`` = ``total_minor`` = der Betrag), die jüngsten zuerst, höchstens ``limit``."""
        return _movement_dicts(conn.execute(_UNBOOKED_MOVEMENTS, (limit,)).fetchall())

    def _free_movement(self, conn, booking_id):
        """Die Bankbewegung ``booking_id`` mit ihrem freien Rest nach ``_free_minor_sql`` – ``[dict]`` wie bei ``_unbooked_movements`` (``amount_minor``
        ist der freie Rest), oder ``[]``, wenn sie keine Bankbewegung mit Betrag und freiem Rest größer 0 ist."""
        free_sql, free_params = self._free_minor_sql(self._get_bank_coa_ids(conn.cursor()))
        rows = conn.execute(f'''
            SELECT * FROM (
                SELECT b.ID, b.DateBooking, b.RecipientClient, b.Text, b.DocumentNumber, b.Contact_ID, b.Amount, {free_sql} AS FreeMinor
                FROM Bookings b
                WHERE b.ID = ? AND b.BookingType = 'bank' AND typeof(b.Amount) = 'integer' AND b.Amount > 0
                  AND UPPER(TRIM(COALESCE(b.Currency, ''))) IN ('', 'EUR')
            )
            WHERE FreeMinor > 0''', free_params + [booking_id]).fetchall()
        return _movement_dicts(rows)

    @staticmethod
    def _linked_pairs(conn, invoice_ids):
        """Die Paare ``(ID der Bewegung, ID der Rechnung)`` aus ``InvoicePayments`` zu diesen Rechnungen."""
        invoice_ids, pairs = list(invoice_ids), set()
        for start in range(0, len(invoice_ids), MAX_INVOICES):
            chunk = invoice_ids[start:start + MAX_INVOICES]
            pairs.update((booking, invoice) for invoice, booking in conn.execute(
                f'SELECT InvoiceID, BookingID FROM InvoicePayments WHERE InvoiceID IN ({",".join("?" * len(chunk))})', chunk))
        return frozenset(pairs)

    def suggest_invoice_payments(self, limit=20):
        """Die Übersicht für die Seite *Buchungen*: Zahlungseingänge ohne Kind und ohne Zuordnung (``_unbooked_movements``), zu denen eine offene
        Rechnung passt – eins zu eins (``payment_matching.pair_up``: jede Bewegung und jede Rechnung höchstens einmal, die zeitlich nächste
        zuerst), unabhängig vom gewählten Zeitraum. Rein lesend, zugeordnet wird nichts.

        Returns ``PaymentOverview(items, more)``: ``items`` sind höchstens ``limit`` Tripel ``(PaymentSuggestion, Bewegung, Rechnung)`` in
        der Reihenfolge von ``pair_up`` (Bewegung und Rechnung als ``dict``, siehe ``_unbooked_movements`` und ``_open_invoices``), ``more``
        ist die Zahl der weiteren Vorschläge."""
        conn = self._get_connection()
        try:
            invoices = self._open_invoices(conn)
            movements = self._unbooked_movements(conn) if invoices else []
            linked = self._linked_pairs(conn, (invoice['id'] for invoice in invoices)) if movements else frozenset()
        finally:
            conn.close()
        by_movement = {movement['id']: movement for movement in movements}
        by_invoice = {invoice['id']: invoice for invoice in invoices}
        pairs = pair_up(movements, invoices, linked)
        shown = max(0, int(limit))
        return PaymentOverview([(pair, by_movement[pair.movement_id], by_invoice[pair.invoice_id]) for pair in pairs[:shown]],
                               max(0, len(pairs) - shown))

    def suggest_invoices_for_booking(self, booking_id):
        """Die offenen Rechnungen, die zu EINER Bankbewegung passen – für die Maske der Bewegung. Rein lesend. Returns ``[(PaymentSuggestion,
        Rechnung), …]`` in der Rangfolge von ``payment_matching.rank_invoices`` (höchstens ``MAX_SUGGESTIONS``); leer, wenn die Buchung keine
        Bankbewegung mit Betrag und freiem Rest größer 0 ist (nur ein Zahlungseingang lässt sich einer Rechnung zuordnen). Der freie Rest
        gilt hier nach ``_free_minor_sql`` (auch eine teilweise zugeordnete Bewegung); eine Rechnung, der diese Bewegung schon zugeordnet ist,
        kommt nie vor."""
        conn = self._get_connection()
        try:
            movements = self._free_movement(conn, booking_id)
            if not movements:
                return []
            invoices = self._open_invoices(conn)
            linked = frozenset((booking_id, invoice) for (invoice,) in conn.execute(
                'SELECT InvoiceID FROM InvoicePayments WHERE BookingID = ?', (booking_id,)))
        finally:
            conn.close()
        by_invoice = {invoice['id']: invoice for invoice in invoices}
        return [(pair, by_invoice[pair.invoice_id]) for pair in rank_invoices(movements[0], invoices, linked)]

    def suggest_bookings_for_invoice(self, invoice_id):
        """Die Bankbewegungen, die zu EINER Rechnung passen – für die Liste „Vorhandene Buchung verknüpfen“ der Rechnungsseite. Rein lesend.
        Returns ``[(PaymentSuggestion, Bewegung), …]`` in der Rangfolge von ``payment_matching.rank_movements`` (höchstens ``MAX_SUGGESTIONS``); leer,
        wenn die Rechnung nicht offen ist (kein zahlbarer Status, Angebot, Stornorechnung, nichts offen). Die Bewegungen sind die der
        Übersicht (``_unbooked_movements``: ohne Kind und ohne Zuordnung); eine, die der Rechnung schon zugeordnet ist, kommt nie vor."""
        conn = self._get_connection()
        try:
            invoices = self._open_invoices(conn, 1, invoice_id=invoice_id)
            if not invoices:
                return []
            movements = self._unbooked_movements(conn)
            linked = frozenset((booking, invoice_id) for (booking,) in conn.execute(
                'SELECT BookingID FROM InvoicePayments WHERE InvoiceID = ?', (invoice_id,)))
        finally:
            conn.close()
        by_movement = {movement['id']: movement for movement in movements}
        return [(pair, by_movement[pair.movement_id]) for pair in rank_movements(invoices[0], movements, linked)]

    def skonto_offer(self, invoice_id):
        """Was diese Rechnung an Skonto zulässt – für ihre Seite (die Zeile „Skonto: …“ und die Häkchen). Rein lesend, mit denselben Regeln wie das Zuordnen
        (``invoice_payments.skonto_terms``: keine oder unlesbare Bedingungen zuerst – die Seite sagt dazu nichts –, dann die Gruppen eines Belegs mit Steuersatz je Position, ein Satz ohne
        Skonto-Konto, ein Erlöskonto der Stammdaten, das selbst ein Skonto-Konto ist). Returns
        :class:`SkontoOffer` – ``(Fehlertext, None, None, None)`` oder ``(None, Grenze, Tage, Prozent)``; eine unbekannte Rechnung sieht kein Skonto vor. Die Frist spielt keine Rolle."""
        conn = self._get_connection()
        try:
            row = conn.execute('SELECT SumGross, SkontoDays, SkontoPercent, TaxRate, MixedTaxRates FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()
            if row is None:
                return SkontoOffer(SKONTO_NO_TERMS, None, None, None)
            group_pcts = None
            if has_mixed_rates(row[4]):
                try:
                    group_pcts = tuple(group.pct for group in self._mixed_groups(conn, invoice_id))
                except (RateGroupsMismatch, ArithmeticError, TypeError, ValueError):
                    group_pcts = None
            revenue_on_discount = self._revenue_on_discount(conn, invoice_id)
        finally:
            conn.close()
        return _skonto_offer(*row, group_pcts, revenue_on_discount)

    def skonto_limits(self):
        """Das Skonto, das jede offene Rechnung vorsieht – für die Auswahl „Rechnung“ der Buchungsmaske: ``{ID der Rechnung: Grenze in Minor Units}``, nur für Rechnungen, die ein Skonto
        zulassen. Die Rechnungen sind die der Vorschläge (``_open_invoices``: höchstens ``MAX_INVOICES``, die jüngsten; Euro). Rein lesend."""
        conn = self._get_connection()
        try:
            invoices = self._open_invoices(conn)
        finally:
            conn.close()
        return {invoice['id']: invoice['skonto_limit_minor'] for invoice in invoices if invoice['skonto_limit_minor']}
