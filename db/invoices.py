# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: invoices."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from .core import fk_explained
from .invoice_lock import (DocumentNotFound, invoice_row, REOPEN_ATTACHED_MESSAGE,
                           REOPEN_FOREIGN_MESSAGE, REOPEN_IMPORTED_MESSAGE)
from .invoice_payments import PAYABLE_STATUSES, PAYMENT_MARK
from .matching import FORM_TRANSFER
from .reporting import is_income_account


class InvoiceCol:
    """Positionen der Spalten in einer ``SELECT * FROM Invoices``-Zeile (Reihenfolge laut
    db/schema.py, neue Spalten stehen immer am Ende). Nur, was benannt gelesen wird; wer eine
    Zeile neu liest, nimmt diese Namen statt nackter Indizes."""
    ID = 0
    NUMBER = 1           # InvoiceNumber
    DATE = 2             # InvoiceDate
    BUYER_NAME = 14
    BUYER_COMPANY = 15
    DUE_DATE = 27        # PaymentDueDate
    SUM_GROSS = 38
    AMOUNT_DUE = 39
    STATUS = 40
    DOCUMENT_TYPE = 45   # 'invoice' | 'quote'
    INVOICE_KIND = 50    # NULL = Rechnung (Stornobeleg vorgesehen, noch ungenutzt)
    CANCELS_INVOICE_ID = 51
    REPLACES_INVOICE_ID = 52
    SENT_AT = 53         # erstmals versendet (UTC, CURRENT_TIMESTAMP); Import: NULL
    DOCUMENT_VERSION = 54    # Nummer der aktuellen Fassung; NULL = Fassung 1


# Aus diesen Status wird ein Angebot in eine Rechnung umgewandelt – einmal: danach ist es „converted“.
# Ein abgelehntes Angebot wird erst wieder angenommen.
QUOTE_CONVERTIBLE_STATUSES = ('draft', 'sent', 'accepted')
# Dorthin geht ein Angebot zurück, wenn der Entwurf seiner Rechnung gelöscht wird
QUOTE_RESTORED_STATUS = 'accepted'


class QuoteNotConvertible(ValueError):
    """Das Angebot lässt sich nicht (noch einmal) in eine Rechnung umwandeln; ``str(exc)`` ist ein
    Klartext für die Oberfläche."""


class QuoteStatusLocked(ValueError):
    """Das Angebot bleibt „umgewandelt“, solange es eine Rechnung daraus gibt (sonst ließe es sich ein
    zweites Mal umwandeln); ``str(exc)`` ist ein Klartext für die Oberfläche."""


QUOTE_STAYS_CONVERTED_MESSAGE = ('Aus diesem Angebot wurde schon eine Rechnung erstellt – es bleibt '
                                 '„Umgewandelt“, solange es diese Rechnung gibt.')
# Bedingung eines UPDATE auf ein Angebot, das den Status „umgewandelt“ verlassen oder behalten soll; ein
# Parameter: der neue Status. Mit der Anweisung zusammen geprüft, nicht davor – zwei Anfragen laufen nacheinander
QUOTE_STAYS_CONVERTED_SQL = ("(Invoices.Status IS NOT 'converted' OR ? = 'converted' "
                             "OR NOT EXISTS (SELECT 1 FROM Invoices o WHERE o.SourceQuoteId = Invoices.ID))")


class InvoiceNotDeletable(ValueError):
    """Die Rechnung lässt sich nicht löschen (nur ein Entwurf ohne Zahlung und Forderung);
    ``str(exc)`` ist ein Klartext für die Oberfläche."""


def document_type(row):
    """Belegtyp einer Invoices-Zeile: ``'invoice'`` oder ``'quote'``.

    Fehlt die Spalte oder ist sie NULL (Altbestand), gilt die Zeile als Rechnung – wie in
    ``fetch_invoices``. Rechnungen und Angebote teilen sich die Tabelle; jede Route, die einen
    Beleg anhand einer ID liest oder verändert, prüft den Typ über diese Funktion bzw.
    ``Database.get_document``."""
    if row is not None and len(row) > InvoiceCol.DOCUMENT_TYPE:
        return row[InvoiceCol.DOCUMENT_TYPE] or 'invoice'
    return 'invoice'


class InvoicesMixin:
    def fetch_invoices(self, status=None, doc_type='invoice'):
        """Fetch documents, optionally filtered by status.

        doc_type filtert auf DocumentType ('invoice' Default). Angebote ('quote')
        werden so aus Rechnungsliste UND -Statistik herausgehalten. Altbestand ohne
        DocumentType (NULL) zählt als 'invoice'.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        if doc_type == 'invoice':
            type_clause = "(DocumentType = 'invoice' OR DocumentType IS NULL)"
        else:
            type_clause = "DocumentType = ?"
        params = [] if doc_type == 'invoice' else [doc_type]
        if status:
            params.append(status)
            cursor.execute(
                f'SELECT * FROM Invoices WHERE {type_clause} AND Status = ? '
                'ORDER BY InvoiceDate DESC, InvoiceNumber DESC', params)
        else:
            cursor.execute(
                f'SELECT * FROM Invoices WHERE {type_clause} '
                'ORDER BY InvoiceDate DESC, InvoiceNumber DESC', params)
        rows = cursor.fetchall()
        conn.close()
        # SumNet(36), TaxAmount(37), SumGross(38), AmountDue(39) -> Euro-Decimal
        return [self._euro_row(r, 36, 37, 38, 39) for r in rows]
    def fetch_quotes(self, status=None):
        """Angebote (DocumentType='quote') laden – spiegelbildlich zu fetch_invoices."""
        return self.fetch_invoices(status=status, doc_type='quote')
    def get_invoice_by_id(self, invoice_id):
        """Get a single invoice by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Invoices WHERE ID = ?', (invoice_id,))
        row = cursor.fetchone()
        conn.close()
        return self._euro_row(row, 36, 37, 38, 39)  # SumNet/TaxAmount/SumGross/AmountDue
    def get_document(self, doc_id, doc_type):
        """Rechnung oder Angebot (``doc_type``: ``'invoice'`` | ``'quote'``) nach ID.

        ``None``, wenn es die ID nicht gibt oder der Beleg von anderem Typ ist: Die
        Rechnungs- und die Angebotsrouten holen ihren Beleg hierüber, damit keine Route
        den Beleg der anderen Art liest oder verändert."""
        row = self.get_invoice_by_id(doc_id)
        return row if row is not None and document_type(row) == doc_type else None
    def get_invoice_items(self, invoice_id):
        """Get all items for an invoice"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', (invoice_id,))
        rows = cursor.fetchall()
        conn.close()
        # PricePerUnit(7), TotalNet(8) -> Euro-Decimal
        return [self._euro_row(r, 7, 8) for r in rows]
    def insert_invoice(self, invoice_data, tx=None):
        """Insert a new invoice with all fields

        Args:
            invoice_data: Dictionary with all invoice fields
            tx: optionale :class:`db.core.Transaction` – dann wird darin
                geschrieben und erst mit ihr committet

        Returns:
            invoice_id: The ID of the newly created invoice
        """
        conn = tx.conn if tx else self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''
            INSERT INTO Invoices (
                InvoiceNumber, InvoiceDate,
                OwnCompanyId, SellerName, SellerCompany, SellerStreet, SellerPostalCode, SellerCity, SellerCountry, SellerVATID, SellerEmail, SellerPhone,
                CustomerId, BuyerName, BuyerCompany, BuyerStreet, BuyerPostalCode, BuyerCity, BuyerCountry, BuyerVATID, BuyerReference, BuyerRouteID,
                OrderNumber, Currency, DeliveryDate,
                PaymentTerms, PaymentDueDate, SkontoDays, SkontoPercent,
                BankAccountId, BankName, BankIBAN, BankBIC,
                TaxCategory, TaxRate, SumNet, TaxAmount, SumGross, AmountDue,
                Status, PDFPath, XMLPath,
                DocumentType, ValidUntil, IntroText, ClosingText, SourceQuoteId
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
        '''

        params = (
            invoice_data.get('invoice_number'),
            invoice_data.get('invoice_date'),
            invoice_data.get('own_company_id'),
            invoice_data.get('seller_name'),
            invoice_data.get('seller_company'),
            invoice_data.get('seller_street'),
            invoice_data.get('seller_postal_code'),
            invoice_data.get('seller_city'),
            invoice_data.get('seller_country', 'DE'),
            invoice_data.get('seller_vat_id'),
            invoice_data.get('seller_email'),
            invoice_data.get('seller_phone'),
            invoice_data.get('customer_id'),
            invoice_data.get('buyer_name'),
            invoice_data.get('buyer_company'),
            invoice_data.get('buyer_street'),
            invoice_data.get('buyer_postal_code'),
            invoice_data.get('buyer_city'),
            invoice_data.get('buyer_country', 'DE'),
            invoice_data.get('buyer_vat_id'),
            invoice_data.get('buyer_reference'),
            invoice_data.get('buyer_route_id'),
            invoice_data.get('order_number'),
            invoice_data.get('currency', 'EUR'),
            invoice_data.get('delivery_date'),
            invoice_data.get('payment_terms'),
            invoice_data.get('payment_due_date'),
            invoice_data.get('skonto_days'),
            invoice_data.get('skonto_percent'),
            invoice_data.get('bank_account_id'),
            invoice_data.get('bank_name'),
            invoice_data.get('bank_iban'),
            invoice_data.get('bank_bic'),
            invoice_data.get('tax_category', 'S'),
            invoice_data.get('tax_rate'),
            to_minor(invoice_data.get('sum_net') or 0),
            to_minor(invoice_data.get('tax_amount') or 0),
            to_minor(invoice_data.get('sum_gross') or 0),
            to_minor(invoice_data.get('amount_due') or 0),
            invoice_data.get('status', 'finalized'),
            invoice_data.get('pdf_path'),
            invoice_data.get('xml_path'),
            invoice_data.get('document_type', 'invoice'),
            invoice_data.get('valid_until'),
            invoice_data.get('intro_text'),
            invoice_data.get('closing_text'),
            invoice_data.get('source_quote_id'),
        )
        
        refs = {'OwnCompanyId': invoice_data.get('own_company_id'),
                'CustomerId': invoice_data.get('customer_id'),
                'BankAccountId': invoice_data.get('bank_account_id'),
                'SourceQuoteId': invoice_data.get('source_quote_id')}
        try:
            with fk_explained(conn, 'Invoices', refs):
                cursor.execute(sql_template, params)
            invoice_id = cursor.lastrowid
            if tx:
                tx.log.append((sql_template, params, "Insert invoice"))
            else:
                conn.commit()
                self._log_sql(sql_template, params, "Insert invoice")
            return invoice_id
        except Exception as e:
            print(f"Error inserting invoice: {e}")
            print(f"Params: {params}")
            if not tx:
                conn.rollback()
            raise
        finally:
            if not tx:
                conn.close()
    def insert_invoice_item(self, item_data, tx=None):
        """Insert an invoice item

        Args:
            item_data: Dictionary with item fields
            tx: optionale :class:`db.core.Transaction` (siehe insert_invoice)
        """
        conn = tx.conn if tx else self._get_connection()
        cursor = conn.cursor()
        
        sql_template = '''
            INSERT INTO InvoiceItems (
                InvoiceId, Position, ArticleId, Description, Quantity, Unit, PricePerUnit, TotalNet, TaxCategory, TaxRate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''
        
        params = (
            item_data.get('invoice_id'),
            item_data.get('position'),
            item_data.get('article_id'),
            item_data.get('description'),
            item_data.get('quantity'),
            item_data.get('unit', 'C62'),
            to_minor(item_data.get('price_per_unit') or 0),
            to_minor(item_data.get('total_net') or 0),
            item_data.get('tax_category', 'S'),
            item_data.get('tax_rate')
        )
        
        refs = {'InvoiceId': item_data.get('invoice_id'),
                'ArticleId': item_data.get('article_id')}
        try:
            with fk_explained(conn, 'InvoiceItems', refs):
                cursor.execute(sql_template, params)
            if tx:
                tx.log.append((sql_template, params, "Insert invoice item"))
            else:
                conn.commit()
                self._log_sql(sql_template, params, "Insert invoice item")
        except Exception as e:
            print(f"Error inserting invoice item: {e}")
            if not tx:
                conn.rollback()
            raise
        finally:
            if not tx:
                conn.close()
    def update_invoice_status(self, invoice_id, status):
        """Update invoice status. Der ungeprüfte Baustein für Seed und Tests (der Weg der Oberfläche ist
        ``set_invoice_status``): Wer auf „versendet“ setzt, bekommt wie dort den Versandzeitpunkt, falls
        die Rechnung noch keinen hat; ein Protokoll schreibt dieser Weg nicht."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE Invoices SET Status = ?, UpdatedAt = CURRENT_TIMESTAMP, '
                           "SentAt = CASE WHEN ? = 'sent' THEN COALESCE(SentAt, CURRENT_TIMESTAMP) ELSE SentAt END "
                           'WHERE ID = ?', (status, status, invoice_id))
            conn.commit()
        except Exception as e:
            print(f"Error updating invoice status: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    def set_quote_status(self, quote_id, status):
        """Den Status eines Angebots setzen – der Weg der Angebotsroute. Ein Angebot, aus dem eine Rechnung
        entstanden ist, bleibt „umgewandelt“ (:class:`QuoteStatusLocked`); ``DocumentNotFound``, wenn es
        das Angebot nicht gibt (oder die ID eine Rechnung ist). ``update_invoice_status`` bleibt der
        ungeprüfte Baustein für Seed und Tests."""
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "UPDATE Invoices SET Status = ?, UpdatedAt = CURRENT_TIMESTAMP "
                f"WHERE ID = ? AND DocumentType = 'quote' AND {QUOTE_STAYS_CONVERTED_SQL}",
                (status, quote_id, status))
            if cursor.rowcount == 0:
                exists = conn.execute("SELECT 1 FROM Invoices WHERE ID = ? AND DocumentType = 'quote'",
                                      (quote_id,)).fetchone()
                conn.rollback()
                if exists:
                    raise QuoteStatusLocked(QUOTE_STAYS_CONVERTED_MESSAGE)
                raise DocumentNotFound('Angebot nicht gefunden.')
            conn.commit()
        finally:
            conn.close()
    def delete_draft_invoice(self, invoice_id):
        """Einen Entwurf löschen – samt Positionen (CASCADE), in EINER Transaktion.

        Nur eine Rechnung im Status ``draft``, ohne zugeordnete Zahlung und ohne Forderungsbuchung
        (:class:`InvoiceNotDeletable` sonst, :class:`DocumentNotFound` ohne Rechnung). Ein
        abgeschlossener Entwurf wird erst auf „Entwurf“ zurückgesetzt: Eine schon heruntergeladene
        PDF soll nicht spurlos verschwinden können. Die Rechnungsnummer geht an den Nummernkreis
        zurück (``release_invoice_number``) – außer die Rechnung war schon einmal versendet
        (``was_sent_before``): Dann bleibt die Lücke. Stammt der Entwurf aus einem
        Angebot und gibt es keine weitere Rechnung dazu, ist das Angebot wieder umwandelbar.

        Die abgelegten Dokumente des Entwurfs räumt der Aufrufer nach dem Commit weg; was in
        „Fassungen“ liegt, nie."""
        with self.transaction() as tx:
            conn = tx.conn
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT InvoiceNumber, Status, DocumentType, SourceQuoteId '
                               'FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()
            if row is None or (row[2] or 'invoice') != 'invoice':
                raise DocumentNotFound('Rechnung nicht gefunden.')
            number, status, _, quote_id = row
            status = status or 'draft'
            if status != 'draft':
                raise InvoiceNotDeletable(
                    'Eine abgeschlossene Rechnung lässt sich nicht löschen – erst auf „Entwurf“ '
                    'zurücksetzen.' if status == 'finalized' else
                    'Nur ein Entwurf lässt sich löschen.')
            if conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ?',
                            (invoice_id,)).fetchone():
                raise InvoiceNotDeletable('Zu dieser Rechnung sind Zahlungen zugeordnet.')
            receivable_sql, receivable_params = self._receivable_sql(conn.cursor())
            if conn.execute(f"SELECT 1 FROM Bookings WHERE DocumentNumber = ? AND BookingType = 'entry' "
                            f"AND {receivable_sql}", [number, *receivable_params]).fetchone():
                raise InvoiceNotDeletable('Zu dieser Rechnung gibt es eine Forderungsbuchung.')
            # War die Rechnung schon einmal versendet, kann der Kunde ein Dokument mit dieser Nummer haben:
            # Sie wird nie wieder vergeben, die Lücke bleibt
            if not self.was_sent_before(invoice_id, conn):
                self.release_invoice_number(conn, number)
            conn.execute("DELETE FROM Invoices WHERE ID = ? AND COALESCE(Status, 'draft') = 'draft'",
                         (invoice_id,))
            if quote_id is not None:
                conn.execute(
                    "UPDATE Invoices SET Status = ?, UpdatedAt = CURRENT_TIMESTAMP "
                    "WHERE ID = ? AND DocumentType = 'quote' AND Status = 'converted' "
                    "AND NOT EXISTS (SELECT 1 FROM Invoices o WHERE o.SourceQuoteId = ?)",
                    (QUOTE_RESTORED_STATUS, quote_id, quote_id))
    def delete_quote(self, quote_id):
        """Ein Angebot löschen (inkl. Positionen via CASCADE).

        Löst vorher die Rück-Verknüpfung: falls aus dem Angebot bereits eine
        Rechnung erzeugt wurde, wird deren SourceQuoteId auf NULL gesetzt (sonst
        blockiert der Fremdschlüssel). Die erzeugte Rechnung selbst bleibt erhalten.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE Invoices SET SourceQuoteId = NULL WHERE SourceQuoteId = ?',
                           (quote_id,))
            cursor.execute("DELETE FROM Invoices WHERE ID = ? AND DocumentType = 'quote'",
                           (quote_id,))
            conn.commit()
        except Exception as e:
            print(f"Error deleting quote {quote_id}: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    def convert_quote_to_invoice(self, quote_id):
        """Ein Angebot in eine neue Rechnung kopieren – einmal je Angebot, in EINER Transaktion.

        Kopiert Kopf, Texte und Positionen in eine neue Rechnung (DocumentType 'invoice', Status
        'draft', SourceQuoteId=quote_id). Bank/Zahlung bleiben leer (im Bearbeiten-Modus zu
        ergänzen). Die Nummer kommt aus dem Nummernkreis (``allocate_invoice_number``), das Angebot
        bekommt den Status 'converted'. Umgewandelt wird nur aus ``QUOTE_CONVERTIBLE_STATUSES``: Der
        Statuswechsel des Angebots ist ein ``UPDATE … WHERE Status IN (…)`` – zwei gleichzeitige
        Anfragen legen nie zwei Rechnungen an.

        Returns:
            new_invoice_id oder None (falls Angebot fehlt / kein Angebot ist).
        Raises:
            QuoteNotConvertible: das Angebot ist schon umgewandelt oder abgelehnt.
        """
        import datetime
        today = datetime.date.today().isoformat()
        with self.transaction() as tx:
            conn = tx.conn
            conn.execute('BEGIN IMMEDIATE')
            quote = self._euro_row(
                conn.execute('SELECT * FROM Invoices WHERE ID = ?', (quote_id,)).fetchone(),
                36, 37, 38, 39)
            # Nur echte Angebote umwandeln
            if quote is None or document_type(quote) != 'quote':
                return None
            marks = ','.join('?' * len(QUOTE_CONVERTIBLE_STATUSES))
            if conn.execute(
                    "UPDATE Invoices SET Status = 'converted', UpdatedAt = CURRENT_TIMESTAMP "
                    f"WHERE ID = ? AND DocumentType = 'quote' AND COALESCE(Status, 'draft') IN ({marks})",
                    (quote_id, *QUOTE_CONVERTIBLE_STATUSES)).rowcount != 1:
                raise QuoteNotConvertible(
                    'Dieses Angebot wurde bereits in eine Rechnung umgewandelt.'
                    if quote[InvoiceCol.STATUS] == 'converted' else
                    'Ein abgelehntes Angebot lässt sich nicht in eine Rechnung umwandeln – erst auf '
                    '„Angenommen“ setzen.')
            sum_gross = quote[38] or 0
            invoice_data = {
                'invoice_number': self.allocate_invoice_number(conn, today),
                'invoice_date': today,
                'own_company_id': quote[3],
                'seller_name': quote[4], 'seller_company': quote[5],
                'seller_street': quote[6], 'seller_postal_code': quote[7],
                'seller_city': quote[8], 'seller_country': quote[9] or 'DE',
                'seller_vat_id': quote[10], 'seller_email': quote[11], 'seller_phone': quote[12],
                'customer_id': quote[13],
                'buyer_name': quote[14], 'buyer_company': quote[15],
                'buyer_street': quote[16], 'buyer_postal_code': quote[17],
                'buyer_city': quote[18], 'buyer_country': quote[19] or 'DE',
                'buyer_vat_id': quote[20], 'buyer_reference': quote[21], 'buyer_route_id': quote[22],
                'order_number': quote[23], 'currency': quote[24] or 'EUR',
                'delivery_date': today,  # Pflicht, = Rechnungsdatum
                'payment_terms': None, 'payment_due_date': None,
                'skonto_days': None, 'skonto_percent': None,
                'bank_account_id': None, 'bank_name': None, 'bank_iban': None, 'bank_bic': None,
                'tax_category': quote[34] or 'S', 'tax_rate': quote[35],
                'sum_net': quote[36] or 0, 'tax_amount': quote[37] or 0,
                'sum_gross': sum_gross, 'amount_due': sum_gross,
                'status': 'draft', 'pdf_path': None, 'xml_path': None,
                'document_type': 'invoice',
                'valid_until': None,
                'intro_text': quote[47] if len(quote) > 47 else None,
                'closing_text': quote[48] if len(quote) > 48 else None,
                'source_quote_id': quote_id,
            }
            new_invoice_id = self.insert_invoice(invoice_data, tx=tx)

            # Positionen kopieren (Euro-Decimals werden in insert_invoice_item zu Minor)
            for row in conn.execute('SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position',
                                    (quote_id,)).fetchall():
                it = self._euro_row(row, 7, 8)
                self.insert_invoice_item({
                    'invoice_id': new_invoice_id,
                    'position': it[2], 'article_id': it[3],
                    'description': it[4], 'quantity': it[5], 'unit': it[6] or 'C62',
                    'price_per_unit': it[7] or 0, 'total_net': it[8] or 0,
                    'tax_category': it[9] or 'S', 'tax_rate': it[10],
                }, tx=tx)
        return new_invoice_id
    def link_invoice_to_transaction(self, invoice_id, transaction_id, amount_paid):
        """Link an invoice to a payment booking via InvoicePayments and recalculate AmountDue.

        Der WISO-Import ordnet darüber mit Vorzeichen zu (Storno, Gutschrift) und legt keine
        Buchungssätze an – WISO liefert sie mit. Deshalb keine Prüfung von Status, Vorzeichen
        oder Buchungsart; die Oberfläche ordnet über ``assign_invoice_payment`` zu. Die
        Dublettenprüfung läuft in derselben Transaktion wie das Einfügen."""
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone():
                raise ValueError(f"Invoice {invoice_id} not found")

            # Get payment date from booking
            booking = conn.execute('SELECT DateBooking FROM Bookings WHERE ID = ?',
                                   (transaction_id,)).fetchone()
            payment_date = booking[0] if booking else None

            # Prevent duplicate links for the same booking
            if conn.execute('SELECT ID FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                            (invoice_id, transaction_id)).fetchone():
                raise ValueError(f"Booking {transaction_id} is already linked to invoice {invoice_id}")

            try:
                conn.execute('''
                    INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate)
                    VALUES (?, ?, ?, ?)
                ''', (invoice_id, transaction_id, to_minor(amount_paid), payment_date))
            except sqlite3.IntegrityError:
                # der eindeutige Index auf (Rechnung, Bewegung): ein gleichzeitiger zweiter Aufruf
                raise ValueError(
                    f"Booking {transaction_id} is already linked to invoice {invoice_id}")

            self._recalc_invoice(conn, invoice_id)
            conn.commit()
            print(f"Invoice {invoice_id} linked to booking {transaction_id}")

        except Exception as e:
            print(f"Error linking invoice to transaction: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    def get_invoice_payments(self, invoice_id):
        """Return all payment entries for a given invoice.

        Returns list of tuples:
          (ID, InvoiceID, BookingID, Amount, PaymentDate, Notes, BookingReference)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ip.ID, ip.InvoiceID, ip.BookingID, ip.Amount, ip.PaymentDate, ip.Notes,
                   COALESCE(b.DocumentNumber, b.Text, '') AS BookingRef
            FROM InvoicePayments ip
            LEFT JOIN Bookings b ON b.ID = ip.BookingID
            WHERE ip.InvoiceID = ?
            ORDER BY ip.PaymentDate, ip.ID
        ''', (invoice_id,))
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 3) for r in rows]  # Amount (Index 3) -> Euro-Decimal
    def delete_invoice_payment(self, payment_id):
        """Eine Zuordnung samt ihren Buchungssätzen entfernen und die Rechnung neu rechnen –
        siehe ``unassign_invoice_payment``."""
        self.unassign_invoice_payment(payment_id)
    def update_invoice_pdf_path(self, invoice_id, pdf_path):
        """Update PDFPath field for an invoice
        
        Args:
            invoice_id: ID of the invoice
            pdf_path: Path to the PDF file
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Invoices 
                SET PDFPath = ?,
                    UpdatedAt = CURRENT_TIMESTAMP
                WHERE ID = ?
            ''', (pdf_path, invoice_id))
            
            conn.commit()
        except Exception as e:
            print(f"Error updating invoice PDF path: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    def document_path_in_use(self, path, exclude_id):
        """Verweist ein ANDERER Beleg auf diese Datei (PDFPath/XMLPath, nach Auflösen des Pfads)?

        Schutz vor dem Löschen fremder Dokumente: Der gespeicherte Pfad eines Belegs lässt sich
        per SQL-Konsole oder zurückgespielter Sicherung auf die Datei eines anderen umbiegen."""
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError, TypeError):
            return False
        conn = self._get_connection()
        try:
            rows = conn.execute(
                'SELECT PDFPath, XMLPath FROM Invoices '
                'WHERE ID <> ? AND (PDFPath IS NOT NULL OR XMLPath IS NOT NULL)',
                (exclude_id if exclude_id is not None else -1,)).fetchall()
        finally:
            conn.close()
        for row in rows:
            for stored in row:
                if not isinstance(stored, str) or not stored:
                    continue
                try:
                    if os.path.realpath(stored) == real:
                        return True
                except (OSError, ValueError):
                    continue
        return False

    def update_invoice_xml_path(self, invoice_id, xml_path):
        """Update XMLPath field for an invoice (XRechnung-Datei)

        Args:
            invoice_id: ID of the invoice
            xml_path: Path to the XRechnung XML file
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Invoices
                SET XMLPath = ?,
                    UpdatedAt = CURRENT_TIMESTAMP
                WHERE ID = ?
            ''', (xml_path, invoice_id))

            conn.commit()
        except Exception as e:
            print(f"Error updating invoice XML path: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    def get_overdue_invoices(self):
        """Get all overdue invoices (sent status, past due date, amount due > 0)
        
        Returns:
            List of invoice tuples with overdue invoices
        """
        from datetime import date
        today = date.today().isoformat()
        
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM Invoices
            WHERE Status IN ('finalized', 'sent')
            AND (DocumentType = 'invoice' OR DocumentType IS NULL)
            AND PaymentDueDate < ?
            AND (AmountDue IS NULL OR AmountDue > 0)
            ORDER BY PaymentDueDate ASC
        ''', (today,))
        invoices = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 36, 37, 38, 39) for r in invoices]
    def get_invoices_due_soon(self, days=7):
        """Get invoices due within the next N days
        
        Args:
            days: Number of days to look ahead
            
        Returns:
            List of invoice tuples
        """
        from datetime import date, timedelta
        today = date.today()
        future_date = (today + timedelta(days=days)).isoformat()
        today_str = today.isoformat()
        
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM Invoices
            WHERE Status IN ('finalized', 'sent')
            AND (DocumentType = 'invoice' OR DocumentType IS NULL)
            AND PaymentDueDate BETWEEN ? AND ?
            AND (AmountDue IS NULL OR AmountDue > 0)
            ORDER BY PaymentDueDate ASC
        ''', (today_str, future_date))
        invoices = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 36, 37, 38, 39) for r in invoices]

    # ── Zahlungs-Zuordnung (Rechnung ↔ Buchung, todo #2) ─────────────────────

    def recalc_invoice_payment_state(self, invoice_id, adjust_status=True):
        """AmountDue (und optional Status) aus den tatsächlichen Zahlungen
        neu berechnen.

        Selbstheilung für verwaiste Zustände: Wird z. B. eine verknüpfte
        Buchung gelöscht, verschwinden die InvoicePayments-Zeilen – ohne
        Neuberechnung bliebe die Rechnung fälschlich auf "bezahlt"/Rest 0.
        adjust_status=False aktualisiert nur AmountDue (für manuelle
        Statuswechsel, deren Wahl nicht sofort überschrieben werden soll).
        Die Statusregel steht in ``db.invoice_payments.next_payment_status``.
        """
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            self._recalc_invoice(conn, invoice_id, adjust_status)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_open_invoices(self):
        """Offene Rechnungen für die Zahlungs-Zuordnung.

        Nur zahlbare Status (versendet, überfällig, Teilzahlung – ``PAYABLE_STATUSES``),
        Restbetrag > 0; Altbestand ohne AmountDue (NULL) gilt als offen. Volle
        Invoice-Rows (Euro-Decimals wie üblich).
        """
        marks = ','.join('?' * len(PAYABLE_STATUSES))
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(f'''
            SELECT * FROM Invoices
            WHERE (DocumentType = 'invoice' OR DocumentType IS NULL)
            AND Status IN ({marks})
            AND (AmountDue IS NULL OR AmountDue > 0)
            ORDER BY InvoiceDate DESC, InvoiceNumber DESC
        ''', PAYABLE_STATUSES)
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 36, 37, 38, 39) for r in rows]

    @staticmethod
    def _revenue_rate_key(tax_rate):
        """Steuersatz als Vergleichsschlüssel für die Erlöskonten: Prozent, auf zwei
        Stellen gerundet. ``tax_rate`` wie in Invoices.TaxRate bzw. Bookings.TaxRate (0.19,
        19, 0 oder -1); 0 und der §19-Sentinel bilden einen gemeinsamen „steuerfrei“-Topf,
        ``None`` (kein Satz) ist ein eigener. Das SQL-Gegenstück ist ``_revenue_rate_key_sql``."""
        if tax_rate is None:
            return -999.0
        if tax_rate <= 0:
            return 0.0
        pct = tax_rate * 100 if tax_rate <= 1 else tax_rate
        return round(pct, 2)

    @staticmethod
    def _revenue_rate_key_sql(column):
        """SQL-Ausdruck, der die Spalte ``column`` (ein Steuersatz) wie ``_revenue_rate_key``
        zum Vergleichsschlüssel macht."""
        return (f'(CASE WHEN {column} IS NULL THEN -999.0 WHEN {column} <= 0 THEN 0.0 '
                f'WHEN {column} <= 1 THEN ROUND({column} * 100, 2) '
                f'ELSE ROUND({column}, 2) END)')

    def _learned_revenue_coa(self, key):
        """Das Erlöskonto, auf das die App zuletzt eine Zahlung zu einer Rechnung mit diesem
        Steuersatz-Schlüssel (``_revenue_rate_key``) gebucht hat – oder ``None``.

        Gelesen wird NUR aus den Sätzen, die die App selbst für eine Zuordnung angelegt hat
        (Marke ``PAYMENT_MARK``), und nur in der Form, in der sie sie schreibt:

        * Umbuchung „Wartekonto an Erlöskonto“: ``COA`` ist ein Wartekonto
          (``OPEN_REVENUE_ACCOUNTS``), gelernt wird das Gegenkonto;
        * Einzelsatz „Erlöskonto an Bank“: das Gegenkonto ist ein Zahlungskonto
          (``get_payment_coa_ids``), gelernt wird ``COA``.

        Alles andere lehrt nichts – auch „Bank an Debitoren“ nicht, und kein Satz, den ein Nutzer
        im Split-Editor in eine andere Form gebracht hat (aus „4405 an 4400“ wurde „4736 an 4400“).
        Der jüngste passende Satz gewinnt. Das Konto der Bankzeile oder der Sätze eines Imports
        sagt nichts über das Erlöskonto (es kann ein Bankkonto, ein Aufwandskonto oder das
        Wartekonto sein) und zählt nicht. Das Ergebnis gilt nur, wenn es ein Erlöskonto ist
        (``is_income_account``: 4000–4999 ohne Wartekonten)."""
        waiting = tuple(number for number, _name in self.OPEN_REVENUE_ACCOUNTS.values())
        waiting_marks = ','.join('?' * len(waiting))
        conn = self._get_connection()
        try:
            payment_ids = sorted(self._get_payment_coa_ids(conn.cursor()))
            payment_marks = ','.join('?' * len(payment_ids))
            row = conn.execute(f'''
                SELECT cand.ID, cand.AccountNumber
                FROM Bookings k
                JOIN Invoices i ON i.InvoiceNumber = k.DocumentNumber
                LEFT JOIN ChartOfAccounts own ON own.ID = k.COA_ID
                JOIN ChartOfAccounts cand ON cand.ID = CASE
                    WHEN own.AccountNumber IN ({waiting_marks}) THEN k.CounterCOA_ID
                    ELSE k.COA_ID END
                WHERE k.Status = ? AND k.ParentBooking_ID IS NOT NULL
                  AND (i.DocumentType = 'invoice' OR i.DocumentType IS NULL)
                  AND (own.AccountNumber IN ({waiting_marks})
                       OR k.CounterCOA_ID IN ({payment_marks}))
                  AND {self._revenue_rate_key_sql('i.TaxRate')} = ?
                ORDER BY k.ID DESC LIMIT 1
            ''', (*waiting, PAYMENT_MARK, *waiting, *payment_ids, key)).fetchone()
        finally:
            conn.close()
        return row[0] if row and is_income_account(row[1]) else None

    def resolve_revenue_coa(self, tax_rate, create=True):
        """Passendes Erlöskonto für einen Rechnungs-Steuersatz ermitteln.

        Lernt zuerst aus der eigenen Historie (``_learned_revenue_coa``): das zuletzt von der
        App bei einer Zahlungs-Zuordnung gebuchte Erlöskonto einer Rechnung mit gleichem
        Steuersatz (0% und §19-Sentinel -1 bilden einen gemeinsamen "steuerfrei"-Topf).
        Fallback auf die Standardkonten: 19% → 4400, 7% → 4300, steuerfrei/§19 → 4185
        (DATEV: "Erlöse als Kleinunternehmer nach § 19 Abs. 1 UStG"; wird in älteren DBs ohne
        dieses Seed-Konto automatisch nachgelegt, Vorbild ensure_kasse_exists).
        tax_rate wie in Invoices.TaxRate (0.19, 19, 0 oder -1).

        Für andere Sätze (z. B. 5 %, 16 %) gibt es ohne Historie kein Konto: ``None``. Dann
        wählt der Nutzer eines (``assign_invoice_payment(revenue_coa_id=…)``); die Sätze der
        Zuordnung tragen es, und die nächste Rechnung mit diesem Satz übernimmt es – geraten
        wird nie.

        ``create=False`` schlägt nur nach und legt nichts an (für reine Anzeigen):
        fehlt das Standardkonto, ist das Ergebnis ``None``.
        """
        key = self._revenue_rate_key(tax_rate)
        learned = self._learned_revenue_coa(key)
        if learned is not None:
            return learned
        if key == 19:
            return self.get_coa_id_by_account_number(4400)
        if key == 7:
            return self.get_coa_id_by_account_number(4300)
        if key == 0:
            if create:
                return self._ensure_tax_free_revenue_coa()
            return self.get_coa_id_by_account_number(self.TAX_FREE_REVENUE_ACCOUNT_NUMBER)
        return None

    # Wartekonten für offene Posten: die Rechnung wird beim Versenden hierhin
    # gebucht, erst die Zahlung bucht auf das echte Erlöskonto um. Damit sieht
    # das Steuerbüro im DATEV-Export die noch offenen Posten (Sollversteuerung).
    OPEN_REVENUE_ACCOUNTS = {
        19: (4405, 'Erlöse 19% USt noch offen'),
        7:  (4345, 'Umsatzerlöse 7% noch offen'),
        0:  (4340, 'Umsatzerlöse 0% noch offen'),
    }
    DEBTOR_ACCOUNT_NUMBER = 10000
    TAX_FREE_REVENUE_ACCOUNT_NUMBER = 4185

    def resolve_open_revenue_coa(self, tax_rate):
        """Wartekonto ("noch offen") zum Steuersatz einer Rechnung.

        19% → 4405, 7% → 4345, steuerfrei/§19 → 4340. Fehlt das Konto in einer
        älteren DB, wird es nachgelegt (Vorbild _ensure_tax_free_revenue_coa).
        Andere Sätze haben kein Wartekonto und liefern None – der Aufrufer
        bucht dann wie bisher erst bei Zahlung.
        """
        key = self._tax_rate_key(tax_rate)
        entry = self.OPEN_REVENUE_ACCOUNTS.get(key)
        if not entry:
            return None
        number, name = entry
        return self._ensure_standard_coa(number, name)

    @staticmethod
    def _tax_rate_key(tax_rate):
        """Invoices.TaxRate (0.19, 19, 0, -1, None) → 19 / 7 / 0 / None."""
        if tax_rate is None:
            return None
        if tax_rate <= 0:          # 0% und §19-Sentinel -1 sind ein Topf
            return 0
        pct = tax_rate * 100 if tax_rate <= 1 else tax_rate
        return int(round(pct))

    def _ensure_standard_coa(self, number, name,
                             description='Betriebliche Erträge'):
        """COA-ID zu einer Kontonummer liefern, notfalls als Standardkonto
        im dominanten Kontenrahmen anlegen."""
        coa = self.get_coa_id_by_account_number(number)
        if coa:
            return coa
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT Framework FROM ChartOfAccounts '
                       'GROUP BY Framework ORDER BY COUNT(*) DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        self.insert_chart_of_accounts(row[0] if row else 4, number, name,
                                      description, is_standard=1)
        return self.get_coa_id_by_account_number(number)

    def get_receivable_booking(self, invoice_id):
        """Forderungsbuchung einer Rechnung oder None.

        Erkannt über Beleg-Nr. + ``db.matching.receivable_condition`` (Debitoren 10000 im Soll,
        kein Zahlungskonto) – dieselbe Bedingung wie überall, damit ein WISO-Storno
        „10000 an 1810“ oder eine Erlösminderung „… an 10000“ mit der Rechnungsnummer nicht
        als Forderung gilt. Ein eigenes Schema-Feld wäre dafür zu viel: Rechnungsnummern sind
        eindeutig.
        """
        invoice = self.get_invoice_by_id(invoice_id)
        if not invoice:
            return None
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            receivable_sql, receivable_params = self._receivable_sql(cursor)
            cursor.execute(
                f"SELECT ID FROM Bookings WHERE DocumentNumber = ? AND BookingType = 'entry' "
                f"AND {receivable_sql} ORDER BY ID LIMIT 1", [invoice[1], *receivable_params])
            row = cursor.fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def _receivable_accounts(self, tax_rate):
        """``(Wartekonto, Debitorenkonto)`` für die Forderung einer Rechnung mit diesem Steuersatz,
        ``None`` ohne Wartekonto (z. B. 5 %, 16 %: dort wird erst bei Zahlung gebucht). Legt fehlende
        Konten in eigenen Verbindungen an – also vor einer Transaktion, nie darin."""
        open_coa = self.resolve_open_revenue_coa(tax_rate)
        if not open_coa:
            return None
        debtor_coa = self._ensure_standard_coa(
            self.DEBTOR_ACCOUNT_NUMBER, 'Debitoren',
            'Vortrags-, Kapital-, Korrektur- u. statistische Konten')
        return open_coa, debtor_coa

    def ensure_receivable_booking(self, invoice_id):
        """Offenen Posten einer Rechnung buchen: Debitoren an Wartekonto.

        Idempotent – existiert die Buchung schon, passiert nichts. Angebote,
        Entwürfe und Steuersätze ohne Wartekonto (z.B. 5%) werden übersprungen.
        Der Statuswechsel bucht sie selbst in seiner Transaktion
        (``Database.set_invoice_status``); dieser Weg ist für alles andere (Demodaten, Tests).

        Returns: Booking-ID oder None.
        """
        invoice = self.get_invoice_by_id(invoice_id)
        if not invoice or (len(invoice) > 45 and invoice[45] == 'quote'):
            return None
        accounts = self._receivable_accounts(invoice[35])
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            row = invoice_row(tx.conn, invoice_id)
            return self._book_receivable(tx, row, accounts) if row else None

    def _book_receivable(self, tx, row, accounts):
        """Die Forderung einer Rechnung („Debitoren an Wartekonto“) in der Transaktion ``tx`` buchen,
        wenn es sie noch nicht gibt. ``row``: die Rechnung (Spalte → Wert, Minor Units), ``accounts``:
        siehe ``_receivable_accounts``. Returns die Buchungs-ID oder ``None`` (Angebot, kein Wartekonto)."""
        if accounts is None or (row.get('DocumentType') or 'invoice') != 'invoice':
            return None
        number = row['InvoiceNumber']
        existing = [r[0] for r in self._receivable_rows(tx.conn, number)]
        if existing:
            return existing[0]
        open_coa, debtor_coa = accounts
        rate = row['TaxRate']
        tax_rate = (rate if rate <= 1 else rate / 100) if rate and rate > 0 else None
        customer = row['BuyerCompany'] or row['BuyerName'] or ''
        return self.insert_booking(
            date_booking=row['InvoiceDate'],
            amount=from_minor(row['SumGross'] or 0),
            recipient_client=customer,
            contact_id=row['CustomerId'],
            coa_id=debtor_coa,
            counter_coa_id=open_coa,
            currency=row['Currency'] or 'EUR',
            tax_rate=tax_rate,
            tax_amount=from_minor(row['TaxAmount']) if row['TaxAmount'] else None,
            text=f"Rechnung {number} {customer}".strip(),
            document_number=number,
            booking_type='entry',
            entry_form=FORM_TRANSFER,
            log_description="Offener Posten zur Rechnung",
            tx=tx)

    def _receivable_rows(self, conn, number):
        """Die Forderungsbuchungen zu einer Rechnungsnummer (``db.matching.receivable_condition``)
        mit dem, was über ihr Löschen entscheidet: ``[(ID, ParentBooking_ID, SourceGroup, COA_ID,
        CounterCOA_ID)]``, in der Transaktion von ``conn``."""
        receivable_sql, receivable_params = self._receivable_sql(conn.cursor())
        return conn.execute(
            "SELECT ID, ParentBooking_ID, COALESCE(SourceGroup, ''), COA_ID, CounterCOA_ID FROM Bookings "
            f"WHERE DocumentNumber = ? AND BookingType = 'entry' AND {receivable_sql} ORDER BY ID",
            [number, *receivable_params]).fetchall()

    def _receivable_removal(self, conn, number):
        """Welche Forderungsbuchungen einer Rechnung darf die App entfernen? Nur die, die sie selbst
        gebucht hat und die an nichts hängt: ohne Quellklammer (``SourceGroup`` – Import), mit einem
        Wartekonto (``OPEN_REVENUE_ACCOUNTS``) als Gegenkonto, keiner Bankbewegung zugeordnet. Alles andere
        gehört dem Nutzer oder einem Import und wird nie gelöscht.

        Returns ``(IDs der löschbaren, Klartext zur ersten nicht löschbaren oder None)`` –
        der Klartext ist der Grund, mit dem ein Zurückholen der Rechnung abgelehnt wird."""
        waiting = tuple(n for n, _name in self.OPEN_REVENUE_ACCOUNTS.values())
        waiting_ids = {r[0] for r in conn.execute(
            f"SELECT ID FROM ChartOfAccounts WHERE AccountNumber IN ({','.join('?' * len(waiting))})", waiting)}
        removable, reasons = [], []
        for booking_id, parent, source_group, _coa, counter in self._receivable_rows(conn, number):
            if source_group:
                reasons.append((0, REOPEN_IMPORTED_MESSAGE))
            elif parent is not None:
                reasons.append((1, REOPEN_ATTACHED_MESSAGE))
            elif counter not in waiting_ids:
                reasons.append((2, REOPEN_FOREIGN_MESSAGE))
            else:
                removable.append(booking_id)
        return removable, (min(reasons)[1] if reasons else None)

    def _drop_receivable(self, conn, invoice_id, number):
        """Die Forderungsbuchung einer Rechnung entfernen, in der Transaktion von ``conn`` – nur, solange
        nichts daran hängt (keine Zahlung) und nur eine, die die App selbst gebucht hat
        (``_receivable_removal``). Returns ``True``, wenn gelöscht wurde."""
        if conn.execute('SELECT 1 FROM InvoicePayments WHERE InvoiceID = ?', (invoice_id,)).fetchone():
            return False
        removable, _ = self._receivable_removal(conn, number)
        self._delete_booking_rows(conn, removable)
        return bool(removable)

    def remove_receivable_booking(self, invoice_id):
        """Forderungsbuchung wieder entfernen (Rechnung zurück auf Entwurf, storniert).

        Nur solange nichts daran hängt: keine Zahlung auf der Rechnung und die Buchung keiner
        Bankbewegung zugeordnet – und nur eine, die die App selbst gebucht hat: eine importierte
        (``SourceGroup``) oder von Hand gebuchte bleibt immer stehen. Der Statuswechsel entfernt sie
        selbst in seiner Transaktion (``Database.set_invoice_status``); dieser Weg ist für alles andere.

        Returns: True, wenn gelöscht wurde.
        """
        with self.transaction() as tx:
            tx.conn.execute('BEGIN IMMEDIATE')
            row = invoice_row(tx.conn, invoice_id)
            return row is not None and self._drop_receivable(tx.conn, invoice_id, row['InvoiceNumber'])

    def _ensure_tax_free_revenue_coa(self):
        """Kleinunternehmer-Erlöskonto 4185 liefern (DATEV-SKR04); fehlt es
        (DB älter als der Seed-Eintrag), wird es als Standardkonto im
        dominanten Kontenrahmen nachgelegt – sonst hätten §19-/0%-Rechnungen
        kein Zielkonto."""
        return self._ensure_standard_coa(
            self.TAX_FREE_REVENUE_ACCOUNT_NUMBER,
            'Erlöse als Kleinunternehmer nach § 19 Abs. 1 UStG')

    def get_booking_allocations(self, booking_id):
        """Zahlungs-Zuordnungen einer Buchung.

        Returns: [(PaymentID, InvoiceID, InvoiceNumber, Amount€)]
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ip.ID, ip.InvoiceID, i.InvoiceNumber, ip.Amount
            FROM InvoicePayments ip
            JOIN Invoices i ON i.ID = ip.InvoiceID
            WHERE ip.BookingID = ?
            ORDER BY ip.ID
        ''', (booking_id,))
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 3) for r in rows]

    def get_booking_unallocated_amount(self, booking_id):
        """Freier Rest einer Bankbewegung in Euro (Decimal, min. 0).

        Nur ein Zahlungseingang (Bankbewegung mit Betrag > 0) lässt sich einer Rechnung
        zuordnen; für alles andere ist der Rest 0. Frei ist, was weder zugeordnet noch bereits
        bankwirksam kontiert ist (``_free_minor_sql``) – eine schon als Erlös gebuchte
        Bewegung ist keine Rechnungszahlung mehr."""
        conn = self._get_connection()
        try:
            row = conn.execute('SELECT BookingType, Amount FROM Bookings WHERE ID = ?',
                               (booking_id,)).fetchone()
            if not row or row[0] != 'bank' or (row[1] or 0) <= 0:
                return Decimal(0)
            free = self._booking_free_minor(conn, booking_id,
                                            self._get_bank_coa_ids(conn.cursor()))
        finally:
            conn.close()
        return from_minor(free) if free > 0 else Decimal(0)

    def get_unallocated_bank_bookings(self, contact_id=None, contact_only=False,
                                      limit=20):
        """Bank-Zahlungseingänge mit freiem Rest (``_free_minor_sql``).

        Kandidaten für die Verknüpfung mit offenen Rechnungen (Import-Fall,
        Über-/Sammelzahlungen). Buchungen des Kontakts stehen vorn, danach
        Datum absteigend. contact_only=True filtert hart auf den Kontakt.

        Returns: [(BookingID, DateBooking, RecipientClient, Contact_ID,
                   Amount€, Frei€)]
        """
        contact_filter = 'AND b.Contact_ID = ?' if contact_only and contact_id else ''
        params = [contact_id] if contact_filter else []
        conn = self._get_connection()
        cursor = conn.cursor()
        free_sql, free_params = self._free_minor_sql(self._get_bank_coa_ids(cursor))
        cursor.execute(f'''
            SELECT * FROM (
                SELECT b.ID, b.DateBooking, b.RecipientClient, b.Contact_ID,
                       b.Amount, {free_sql} AS FreeMinor
                FROM Bookings b
                WHERE b.BookingType = 'bank' AND b.Amount > 0 {contact_filter}
            )
            WHERE FreeMinor > 0
            ORDER BY CASE WHEN ? IS NOT NULL AND Contact_ID = ? THEN 0 ELSE 1 END,
                     DateBooking DESC, ID DESC
            LIMIT ?
        ''', free_params + params + [contact_id, contact_id, limit])
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 4, 5) for r in rows]

    def get_open_items_for_contact(self, contact_id):
        """Offene Posten eines Kontakts für die Kontaktseite.

        Forderungen sind die versendeten Rechnungen (``PAYABLE_STATUSES``) mit Rest; ein
        Guthaben ist der freie Rest eines Zahlungseingangs des Kontakts. Nicht versendete
        (abgeschlossene) Rechnungen werden getrennt geführt und gehen nicht in den Saldo ein –
        sie stehen noch nicht in den Büchern.

        Returns dict:
          open_invoices:   [(ID, InvoiceNumber, InvoiceDate, Status, Offen€)]
          unsent_invoices: dasselbe für abgeschlossene, noch nicht versendete Rechnungen
          credits:         [(BookingID, DateBooking, RecipientClient, Contact_ID,
                             Amount€, Frei€)]  – nicht zugeordnete Zahlungs-Reste
          saldo:           Forderungen (versendet) − Guthaben (Euro-Decimal)
        """
        conn = self._get_connection()

        def invoices_with_status(statuses):
            marks = ','.join('?' * len(statuses))
            rows = conn.execute(f'''
                SELECT ID, InvoiceNumber, InvoiceDate, Status,
                       COALESCE(AmountDue, SumGross, 0)
                FROM Invoices
                WHERE CustomerId = ?
                  AND (DocumentType = 'invoice' OR DocumentType IS NULL)
                  AND Status IN ({marks})
                  AND (AmountDue IS NULL OR AmountDue > 0)
                ORDER BY InvoiceDate DESC, ID DESC
            ''', (contact_id, *statuses)).fetchall()
            return [self._euro_row(r, 4) for r in rows]

        open_invoices = invoices_with_status(PAYABLE_STATUSES)
        unsent_invoices = invoices_with_status(('finalized',))
        conn.close()
        credits = self.get_unallocated_bank_bookings(contact_id, contact_only=True,
                                                     limit=100)
        saldo = (sum((r[4] for r in open_invoices), Decimal(0))
                 - sum((r[5] for r in credits), Decimal(0)))
        return {'open_invoices': open_invoices, 'unsent_invoices': unsent_invoices,
                'credits': credits, 'saldo': saldo}
