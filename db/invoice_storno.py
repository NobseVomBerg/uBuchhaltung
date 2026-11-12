# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: Stornorechnung (seit 2.2.1).

Eine versendete Rechnung mit positivem Betrag wird nicht mehr von Hand auf „storniert“ gesetzt, sondern über
``cancel_invoice``: Es entsteht ein eigener Beleg, die **Stornorechnung** – mit eigener Nummer aus dem Rechnungskreis
(Jahr des Stornodatums), dem Kopf der Ursprungsrechnung, ihren Positionen mit negativer Menge und exakt negierten
Summen, dem Status „versendet“, ``InvoiceKind = 'storno'`` und ``CancelsInvoiceId`` = die Ursprungsrechnung. Die
Ursprungsrechnung wird „storniert“.

Die Forderung der Ursprungsrechnung („Debitoren an Wartekonto“) wird dabei nie gelöscht. Hat die App sie gebucht,
bucht die Stornierung die **Gegenbuchung** dazu: „Debitoren an Wartekonto“ mit negativem Betrag und negativer Steuer
zur Nummer und zum Datum der Stornorechnung (die Forderung einer Gutschrift, ``db.matching.receivable_condition``).
Hat die Ursprungsrechnung keine Forderung der App (Import, Steuersatz ohne Wartekonto, importierte oder von Hand
gebuchte Forderung), gibt es keine Gegenbuchung. Zahlungen bleiben an der stornierten Rechnung; was mit ihnen
geschieht, steht in ``docs/aufgaben.md``. Der Stornorechnung lässt sich keine Zahlung zuordnen.

Alles – Prüfung, Nummer, Beleg, Positionen, Status der Ursprungsrechnung, Protokoll, Gegenbuchung, Neuberechnung –
läuft in EINER Transaktion (``BEGIN IMMEDIATE``): Es gelingt ganz oder gar nicht, und eine verbrauchte Nummer gibt
es nach einem Fehler nicht. Das Dokument der Stornorechnung erzeugt der Aufrufer danach.

Das Stornodatum liegt nie in der Zukunft (nach dem heutigen Tag des Servers, ``_today``): Ein Tippfehler (2062) legte
sonst einen Nummernkreis 2062 an, und das ließe sich nicht umkehren. Eine Rechnung ohne positiven Betrag (Gutschrift,
0-€-Rechnung) bekommt keine Stornorechnung; sie lässt sich über den Status auf „storniert“ setzen
(``db.invoice_lock.TRANSITIONS``, Art ``cancel``).
"""
import datetime
import sqlite3
from collections import namedtuple

from money import from_minor
from .core import ForeignKeyError
from .invoice_lock import (
    CHANGED_MESSAGE, EDITABLE_STATUSES, LOCKED_STATUSES, STORNO_KIND, _Refusal, canonical_status,
    document_version, invoice_row, is_storno)
from .invoice_versions import is_iso_date
from .numbering import NumberRangeError

#: Zahlungstext der Stornorechnung (steht im Dokument als Zahlungsbedingung)
STORNO_PAYMENT_TEXT = 'Der Betrag wird verrechnet bzw. erstattet.'

STORNO_DATE_MESSAGE = 'Das Stornodatum fehlt oder ist kein gültiges Datum.'
STORNO_DATE_BEFORE_MESSAGE = 'Das Stornodatum darf nicht vor dem Rechnungsdatum liegen.'
STORNO_DATE_FUTURE_MESSAGE = 'Das Stornodatum darf nicht in der Zukunft liegen.'
NO_POSITIVE_AMOUNT_MESSAGE = ('Eine Rechnung ohne positiven Betrag bekommt keine Stornorechnung – sie lässt sich '
                              'über den Status auf „Storniert“ setzen.')
STORNO_FAILED_PREFIX = 'Die Stornierung konnte nicht gespeichert werden'
STORNO_FAILED_MESSAGE = f'{STORNO_FAILED_PREFIX} – nichts wurde geändert.'
#: Hinweis an den Nutzer, wenn die Stornierung keine Gegenbuchung anlegt
NO_COUNTER_BOOKING_HINT = ('Zu dieser Rechnung gibt es keine von der App gebuchte Forderung – es wurde keine '
                           'Gegenbuchung angelegt.')

#: Ergebnis von ``cancel_invoice``: ob storniert wurde, bei einer Ablehnung der Klartext und der Statuscode für die
#: Antwort (nichts wurde geschrieben), sonst ID und Nummer der Stornorechnung und ob die Forderung eine Gegenbuchung
#: bekommen hat
Storno = namedtuple('Storno', 'ok message code storno_id storno_number counter_booked',
                    defaults=(None, None, False))


def _today():
    """Der heutige Tag des Servers (Ortsdatum) – eine eigene Funktion, damit Tests ihn stellen können."""
    return datetime.date.today()


def _negated(value):
    """``-value``, ohne aus einer 0 eine ``-0.0`` zu machen."""
    return -value if value else value


class InvoiceStornoMixin:
    def cancel_invoice(self, invoice_id, storno_date, user_name=None, *, today=None):
        """Eine versendete Rechnung stornieren: die Stornorechnung anlegen, die Rechnung auf „storniert“ setzen und – hat
        die App ihre Forderung gebucht – die Gegenbuchung dazu. ``storno_date`` (ISO-Datum) ist das Rechnungsdatum der
        Stornorechnung und bestimmt Nummernkreis (Jahr) und Datum der Gegenbuchung; ``user_name`` steht im Protokoll.
        ``today``: der Tag, nach dem kein Stornodatum liegen darf (``datetime.date``); ohne Angabe der heutige Tag des
        Servers. Nur die Demodaten geben ihn an – sie laufen zu einem Szenariodatum, das auch ein anderer Tag sein kann.

        Abgelehnt wird (``Storno.ok`` falsch, ``message`` im Klartext, ``code`` der Statuscode, nichts geschrieben):
        ein Stornodatum, das kein ISO-Datum ist oder nach heute liegt (400); eine unbekannte ID oder die ID eines
        Angebots (404); eine Stornorechnung, ein Entwurf, eine abgeschlossene oder schon stornierte Rechnung, ein
        unbekannter Status, eine Rechnung ohne positiven Betrag (sie wird über den Status storniert), eine Rechnung
        mit Stornorechnung (409); ein Stornodatum vor dem Rechnungsdatum (400); ein Nummernkreis ohne brauchbare
        Nummer (409); ein Fehler beim Speichern (409) – zeigt dabei ein Verweis der Rechnung ins Leere
        (``ForeignKeyError``), steht er im Klartext in der Meldung.

        Die Konten der Forderung werden vor der Transaktion aufgelöst (sie legt bei Bedarf Konten an – in eigenen
        Verbindungen, nie darin); eine abgelehnte Stornierung legt auch keines an. Das Dokument der Stornorechnung
        erzeugt der Aufrufer nach dem Commit. Returns :class:`Storno`."""
        if not is_iso_date(storno_date):
            return Storno(False, STORNO_DATE_MESSAGE, 400)
        if storno_date > (today or _today()).isoformat():
            return Storno(False, STORNO_DATE_FUTURE_MESSAGE, 400)
        # Vorprüfung ohne Schreibsperre: Abgelehnt wird vor dem Auflösen der Konten; die Prüfung in der Transaktion
        # (derselbe Aufruf) entscheidet endgültig
        conn = self._get_connection()
        try:
            row = invoice_row(conn, invoice_id)
            blocked = self._storno_blocker(conn, row, storno_date)
        finally:
            conn.close()
        if blocked:
            return Storno(False, *blocked)
        accounts, accounts_key = self._receivable_accounts(row['TaxRate']), self._tax_rate_key(row['TaxRate'])

        try:
            with self.transaction() as tx:
                conn = tx.conn
                conn.execute('BEGIN IMMEDIATE')
                row = invoice_row(conn, invoice_id)
                blocked = self._storno_blocker(conn, row, storno_date)
                if blocked:
                    raise _Refusal(*blocked)
                if accounts_key != self._tax_rate_key(row['TaxRate']):
                    raise _Refusal(CHANGED_MESSAGE, 409)          # der Steuersatz hat sich inzwischen geändert
                try:
                    number = self.allocate_invoice_number(conn, storno_date)
                except NumberRangeError as exc:
                    raise _Refusal(str(exc), 409) from None

                storno_id = self.insert_invoice(self._storno_head(row, number, storno_date), tx=tx)
                self._execute_logged(conn, tx, 'Stornorechnung kennzeichnen',
                                     'UPDATE Invoices SET InvoiceKind = ?, CancelsInvoiceId = ?, '
                                     'SentAt = CURRENT_TIMESTAMP WHERE ID = ?',
                                     (STORNO_KIND, invoice_id, storno_id))
                self._copy_items_negated(conn, tx, invoice_id, storno_id)

                old = canonical_status(row['Status'])
                if self._execute_logged(
                        conn, tx, 'Rechnung stornieren',
                        "UPDATE Invoices SET Status = 'cancelled', UpdatedAt = CURRENT_TIMESTAMP "
                        "WHERE ID = ? AND COALESCE(Status, 'draft') = ?",
                        (invoice_id, row['Status'] or 'draft')).rowcount != 1:
                    raise _Refusal(CHANGED_MESSAGE, 409)
                self._log_invoice_change(conn, invoice_id, row['InvoiceNumber'], user_name, 'status', 'Status',
                                         old, 'cancelled', document_version(row))
                self._log_invoice_change(conn, storno_id, number, user_name, 'status', 'Status', None, 'sent', 1)

                # Die Gegenbuchung gibt es nur zu einer Forderung, die die App gebucht hat; die Forderung der
                # Ursprungsrechnung bleibt in jedem Fall stehen
                counter_booked = False
                if self._app_receivable_ids(conn, row['InvoiceNumber']):
                    counter_booked = self._book_receivable(tx, invoice_row(conn, storno_id), accounts) is not None

                self._recalc_invoice(conn, invoice_id, adjust_status=False)
                self._recalc_invoice(conn, storno_id, adjust_status=False)
        except _Refusal as refusal:
            return Storno(False, str(refusal), refusal.code)
        except ForeignKeyError as exc:
            # Ein Verweis der Rechnung (Bankkonto, Kunde, eigene Firma …) zeigt ins Leere, etwa nach einem Eingriff über
            # die SQL-Konsole: Der Klartext nennt ihn, geschrieben ist nichts, die Nummer nicht verbraucht
            print(f"Stornierung der Rechnung {invoice_id} nicht gespeichert: {exc}")
            return Storno(False, f'{STORNO_FAILED_PREFIX}: {exc}', 409)
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.IntegrityError) and 'CancelsInvoiceId' in str(exc):
                # der eindeutige Index auf der Stornorechnung: ein gleichzeitiger zweiter Aufruf war schneller
                existing = self.find_storno_invoice(invoice_id)
                if existing:
                    return Storno(False, _already_message(existing[1]), 409)
            print(f"Stornierung der Rechnung {invoice_id} nicht gespeichert: {exc}")
            return Storno(False, STORNO_FAILED_MESSAGE, 409)
        return Storno(True, None, 200, storno_id, number, counter_booked)

    @staticmethod
    def _execute_logged(conn, tx, description, sql, params):
        """Eine Anweisung in der Transaktion ausführen und fürs Replay-Log vormerken (es schreibt erst nach dem
        Commit). Returns den Cursor."""
        cursor = conn.execute(sql, params)
        tx.log.append((sql, params, description))
        return cursor

    def _storno_blocker(self, conn, row, storno_date):
        """Warum lässt sich die gespeicherte Rechnung ``row`` (Spalte → Wert, ``None`` wenn es sie nicht gibt oder sie ein
        Angebot ist) zu diesem Datum nicht stornieren? ``(Klartext, Statuscode)`` der ersten Prüfung, die ablehnt,
        sonst ``None``. In der Transaktion von ``conn``."""
        if row is None:
            return 'Rechnung nicht gefunden.', 404
        if is_storno(row):
            return 'Eine Stornorechnung lässt sich nicht stornieren.', 409
        status = canonical_status(row['Status'])
        if status in EDITABLE_STATUSES:
            return 'Ein Entwurf wird nicht storniert – er lässt sich löschen, solange er ein Entwurf ist.', 409
        if status == 'cancelled':
            return 'Die Rechnung ist bereits storniert.', 409
        # status_group() hielte einen unbekannten Status für gesperrt – storniert wird nur ein bekannter
        if status not in LOCKED_STATUSES:
            return f'Der Status „{status}“ ist unbekannt – die Rechnung lässt sich nicht stornieren.', 409
        if (row['SumGross'] or 0) <= 0:
            return NO_POSITIVE_AMOUNT_MESSAGE, 409
        existing = conn.execute('SELECT ID, InvoiceNumber, InvoiceDate FROM Invoices WHERE CancelsInvoiceId = ?',
                                (row['ID'],)).fetchone()
        if existing:
            return _already_message(existing[1]), 409
        if storno_date < str(row['InvoiceDate'] or '')[:10]:
            return STORNO_DATE_BEFORE_MESSAGE, 400
        return None

    @staticmethod
    def _storno_head(row, number, storno_date):
        """Die Angaben der Stornorechnung für ``insert_invoice``: der Kopf der Ursprungsrechnung ``row`` (Absender,
        Kunde samt Anschrift-Momentaufnahme, Käufer-USt-IdNr., Leitweg-ID, Bestellnummer, Bank, Währung, Steuer,
        Leistungsdatum) mit neuer Nummer, dem Stornodatum als Rechnungsdatum, ohne Fälligkeit und Skonto, mit dem
        Zahlungstext der Stornorechnung und den Summen der Ursprungsrechnung – exakt negiert, nichts neu
        gerechnet. Rest = Brutto, Status „versendet“."""
        def euro(minor):
            return from_minor(-minor)
        return {
            'invoice_number': number, 'invoice_date': storno_date,
            'own_company_id': row['OwnCompanyId'],
            'seller_name': row['SellerName'], 'seller_company': row['SellerCompany'],
            'seller_street': row['SellerStreet'], 'seller_postal_code': row['SellerPostalCode'],
            'seller_city': row['SellerCity'], 'seller_country': row['SellerCountry'],
            'seller_vat_id': row['SellerVATID'], 'seller_email': row['SellerEmail'],
            'seller_phone': row['SellerPhone'],
            'customer_id': row['CustomerId'],
            'buyer_name': row['BuyerName'], 'buyer_company': row['BuyerCompany'],
            'buyer_street': row['BuyerStreet'], 'buyer_postal_code': row['BuyerPostalCode'],
            'buyer_city': row['BuyerCity'], 'buyer_country': row['BuyerCountry'],
            'buyer_vat_id': row['BuyerVATID'], 'buyer_reference': row['BuyerReference'],
            'buyer_route_id': row['BuyerRouteID'],
            'order_number': row['OrderNumber'], 'currency': row['Currency'],
            'delivery_date': row['DeliveryDate'],
            'payment_terms': STORNO_PAYMENT_TEXT, 'payment_due_date': None,
            'skonto_days': None, 'skonto_percent': None,
            'bank_account_id': row['BankAccountId'], 'bank_name': row['BankName'],
            'bank_iban': row['BankIBAN'], 'bank_bic': row['BankBIC'],
            'tax_category': row['TaxCategory'], 'tax_rate': row['TaxRate'],
            'sum_net': euro(row['SumNet']), 'tax_amount': euro(row['TaxAmount']),
            'sum_gross': euro(row['SumGross']), 'amount_due': euro(row['SumGross']),
            'status': 'sent', 'pdf_path': None, 'xml_path': None, 'document_type': 'invoice',
        }

    def _copy_items_negated(self, conn, tx, invoice_id, storno_id):
        """Die Positionen der Ursprungsrechnung in derselben Reihenfolge als Positionen der Stornorechnung: Menge und
        Positionssumme negiert, Einzelpreis und alles andere unverändert."""
        rows = conn.execute(
            'SELECT Position, ArticleId, Description, Quantity, Unit, PricePerUnit, TotalNet, TaxCategory, TaxRate '
            'FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position, ID', (invoice_id,)).fetchall()
        for position, article, description, quantity, unit, price, total, category, rate in rows:
            self.insert_invoice_item({
                'invoice_id': storno_id, 'position': position, 'article_id': article,
                'description': description, 'quantity': _negated(quantity), 'unit': unit,
                'price_per_unit': from_minor(price or 0), 'total_net': from_minor(_negated(total or 0)),
                'tax_category': category, 'tax_rate': rate}, tx=tx)

    def find_storno_invoice(self, invoice_id):
        """Die Stornorechnung zu dieser Rechnung als ``(ID, Nummer, Datum)`` – ``None``, wenn es keine gibt."""
        conn = self._get_connection()
        try:
            return conn.execute('SELECT ID, InvoiceNumber, InvoiceDate FROM Invoices WHERE CancelsInvoiceId = ?',
                                (invoice_id,)).fetchone()
        finally:
            conn.close()

    def find_cancelled_invoice(self, storno_id):
        """Die Rechnung, die diese Stornorechnung storniert, als ``(ID, Nummer, Datum)`` – ``None``, wenn es sie nicht
        gibt oder der Beleg keine Stornorechnung ist."""
        conn = self._get_connection()
        try:
            return conn.execute(
                'SELECT o.ID, o.InvoiceNumber, o.InvoiceDate FROM Invoices s JOIN Invoices o ON o.ID = s.CancelsInvoiceId '
                'WHERE s.ID = ? AND s.InvoiceKind = ?', (storno_id, STORNO_KIND)).fetchone()
        finally:
            conn.close()


def _already_message(number):
    return f'Zu dieser Rechnung gibt es bereits die Stornorechnung {number}.'
