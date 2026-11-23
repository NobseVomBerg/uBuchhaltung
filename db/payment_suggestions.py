# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: payment_suggestions – Vorschläge Zahlungseingang ↔ offene Rechnung (seit 2.8.1), rein lesend.

Die Rangfolge steht in ``db/payment_matching.py`` (rein); dieses Modul liest die Zeilen dafür und setzt beides zusammen. Es wird nichts
geschrieben und nichts zugeordnet – zugeordnet wird allein auf Klick, über ``assign_invoice_payment`` (``POST /invoice/link-payment``, Feld
``invoice_id`` der Maske der Bankbewegung).

* **Offene Rechnungen** (``_open_invoices``): eine Rechnung (kein Angebot, keine Stornorechnung) in einem zahlbaren Status
  (``PAYABLE_STATUSES``) mit offenem Betrag größer 0 – der Betrag, den auch die Maske zeigt: ``AmountDue``, sonst der Brutto. Höchstens
  ``MAX_INVOICES``, die jüngsten zuerst.
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

from .invoice_payments import PAYABLE_STATUSES, STORNO_KIND
from .payment_matching import pair_up, rank_invoices, rank_movements

MAX_INVOICES = 500       # so viele offene Rechnungen liest eine Abfrage höchstens (die jüngsten)
MAX_MOVEMENTS = 500      # … und so viele Bewegungen (die jüngsten)

# Das Ergebnis der Übersicht: die Vorschläge (höchstens ``limit``) und wie viele weitere es gäbe
PaymentOverview = namedtuple('PaymentOverview', 'items more')

# Offene Rechnungen. Platzhalter: der Wert von ``Invoices.InvoiceKind`` einer Stornorechnung, dann die zahlbaren Status. Ein Altbestand ohne
# ``DocumentType`` zählt als Rechnung (wie ``fetch_invoices``); ``COALESCE(AmountDue, SumGross, 0)`` ist der offene Betrag der Maske.
_OPEN_INVOICES = f'''
    SELECT i.ID, i.InvoiceNumber, i.InvoiceDate, COALESCE(i.AmountDue, i.SumGross, 0), i.CustomerId, i.BuyerCompany, i.BuyerName
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


def _invoice_dict(row) -> dict:
    """Eine Zeile von ``_OPEN_INVOICES`` als Eingabe der reinen Funktionen. ``customer_name``: die Firma des Käufers, sonst sein Name – zum
    Vergleichen der Namenswörter (nicht beides: Der Vorname eines Ansprechpartners ist kein Beweis für den Kunden); ``label``: dasselbe für die Seiten."""
    ident, number, date, open_minor, customer_id, company, name = row
    company, name = _name(company), _name(name)
    return {'id': ident, 'number': number, 'date': date, 'open_minor': open_minor, 'customer_id': customer_id,
            'customer_name': company or name, 'label': company or name}


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
        sql, params = _OPEN_INVOICES, [STORNO_KIND, *PAYABLE_STATUSES]
        if invoice_id is not None:
            sql += ' AND i.ID = ?'
            params.append(invoice_id)
        rows = conn.execute(sql + ' ORDER BY i.InvoiceDate DESC, i.ID DESC LIMIT ?', params + [limit]).fetchall()
        return [_invoice_dict(row) for row in rows]

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
