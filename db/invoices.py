# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: invoices."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


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
    def get_invoice_items(self, invoice_id):
        """Get all items for an invoice"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', (invoice_id,))
        rows = cursor.fetchall()
        conn.close()
        # PricePerUnit(7), TotalNet(8) -> Euro-Decimal
        return [self._euro_row(r, 7, 8) for r in rows]
    def insert_invoice(self, invoice_data):
        """Insert a new invoice with all fields
        
        Args:
            invoice_data: Dictionary with all invoice fields
        
        Returns:
            invoice_id: The ID of the newly created invoice
        """
        conn = self._get_connection()
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
        
        try:
            cursor.execute(sql_template, params)
            invoice_id = cursor.lastrowid
            conn.commit()
            self._log_sql(sql_template, params, "Insert invoice")
            return invoice_id
        except Exception as e:
            print(f"Error inserting invoice: {e}")
            print(f"Params: {params}")
            conn.rollback()
            raise
        finally:
            conn.close()
    def insert_invoice_item(self, item_data):
        """Insert an invoice item
        
        Args:
            item_data: Dictionary with item fields
        """
        conn = self._get_connection()
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
        
        try:
            cursor.execute(sql_template, params)
            conn.commit()
            self._log_sql(sql_template, params, "Insert invoice item")
        except Exception as e:
            print(f"Error inserting invoice item: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    def update_invoice_status(self, invoice_id, status):
        """Update invoice status"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE Invoices SET Status = ?, UpdatedAt = CURRENT_TIMESTAMP WHERE ID = ?', (status, invoice_id))
            conn.commit()
        except Exception as e:
            print(f"Error updating invoice status: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    def delete_invoice(self, invoice_id):
        """Delete an invoice and its items (cascade)"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('DELETE FROM Invoices WHERE ID = ?', (invoice_id,))
            conn.commit()
        except Exception as e:
            print(f"Error deleting invoice: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
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
        """Ein angenommenes Angebot in eine neue Rechnung kopieren.

        Kopiert Kopf, Texte und Positionen in eine neue Rechnung (DocumentType
        'invoice', Status 'draft', SourceQuoteId=quote_id). Bank/Zahlung bleiben leer
        (im Bearbeiten-Modus zu ergänzen). Das Angebot erhält Status 'converted'.

        Returns:
            new_invoice_id oder None (falls Angebot fehlt / kein Angebot ist).
        """
        import datetime
        quote = self.get_invoice_by_id(quote_id)
        if not quote:
            return None
        # Nur echte Angebote umwandeln
        if (quote[45] if len(quote) > 45 else 'invoice') != 'quote':
            return None

        # Neue Rechnungsnummer ermitteln: vorhandenen Invoice-Range nutzen, sonst 'R'.
        year = datetime.datetime.now().year
        letter, prefix = 'R', ''
        for r in self.fetch_number_ranges('invoice'):
            # r: ID,Type,Year,Letter,Prefix,...
            if r[2] == year:
                letter, prefix = r[3], (r[4] or '')
                break
        else:
            ranges = self.fetch_number_ranges('invoice')
            if ranges:
                letter, prefix = ranges[0][3], (ranges[0][4] or '')
        new_number = self.get_next_number('invoice', year, letter, prefix)

        sum_gross = quote[38] or 0
        invoice_data = {
            'invoice_number': new_number,
            'invoice_date': datetime.date.today().isoformat(),
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
            'delivery_date': None,
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
        new_invoice_id = self.insert_invoice(invoice_data)

        # Positionen kopieren (Euro-Decimals werden in insert_invoice_item zu Minor)
        for it in self.get_invoice_items(quote_id):
            self.insert_invoice_item({
                'invoice_id': new_invoice_id,
                'position': it[2], 'article_id': it[3],
                'description': it[4], 'quantity': it[5], 'unit': it[6] or 'C62',
                'price_per_unit': it[7] or 0, 'total_net': it[8] or 0,
                'tax_category': it[9] or 'S', 'tax_rate': it[10],
            })

        # Angebot als umgewandelt markieren
        self.update_invoice_status(quote_id, 'converted')
        return new_invoice_id
    def link_invoice_to_transaction(self, invoice_id, transaction_id, amount_paid):
        """Link an invoice to a payment booking via InvoicePayments and recalculate AmountDue."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT SumGross FROM Invoices WHERE ID = ?', (invoice_id,))
            invoice_data = cursor.fetchone()
            if not invoice_data:
                raise ValueError(f"Invoice {invoice_id} not found")

            # Get payment date from booking
            cursor.execute('SELECT DateBooking FROM Bookings WHERE ID = ?', (transaction_id,))
            booking = cursor.fetchone()
            payment_date = booking[0] if booking else None

            # Prevent duplicate links for the same booking
            cursor.execute(
                'SELECT ID FROM InvoicePayments WHERE InvoiceID = ? AND BookingID = ?',
                (invoice_id, transaction_id))
            if cursor.fetchone():
                raise ValueError(f"Booking {transaction_id} is already linked to invoice {invoice_id}")

            cursor.execute('''
                INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate)
                VALUES (?, ?, ?, ?)
            ''', (invoice_id, transaction_id, to_minor(amount_paid), payment_date))

            # Recalculate AmountDue from all payments – alles in Minor Units (exakt).
            cursor.execute(
                'SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                (invoice_id,))
            total_paid_minor = cursor.fetchone()[0]
            gross_minor = invoice_data[0] or 0          # SumGross ist bereits Minor Units
            new_due_minor = gross_minor - total_paid_minor
            new_due = from_minor(new_due_minor)

            cursor.execute('''
                UPDATE Invoices
                SET AmountDue = ?, UpdatedAt = CURRENT_TIMESTAMP
                WHERE ID = ?
            ''', (new_due_minor, invoice_id))

            # Auto-update status (100 Minor Units = 0,01 EUR bei SCALE=4).
            # new_due < 100 statt abs(): auch überzahlte Rechnungen gelten als bezahlt.
            if new_due_minor < 100:
                new_status = 'paid'
            elif total_paid_minor > 0:
                new_status = 'partial_payment'
            else:
                new_status = None

            if new_status:
                cursor.execute('''
                    UPDATE Invoices SET Status = ?, UpdatedAt = CURRENT_TIMESTAMP WHERE ID = ?
                ''', (new_status, invoice_id))

            conn.commit()
            print(f"Invoice {invoice_id} linked to booking {transaction_id}, new due: {new_due:.2f}")

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
        """Remove an InvoicePayments entry and recalculate AmountDue on the invoice."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT InvoiceID, Amount FROM InvoicePayments WHERE ID = ?', (payment_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError(f"InvoicePayment {payment_id} not found")
            invoice_id = row[0]

            cursor.execute('DELETE FROM InvoicePayments WHERE ID = ?', (payment_id,))

            # Recalculate AmountDue – alles in Minor Units (exakt).
            cursor.execute('SELECT SumGross FROM Invoices WHERE ID = ?', (invoice_id,))
            sum_gross = cursor.fetchone()[0]            # bereits Minor Units
            cursor.execute(
                'SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                (invoice_id,))
            total_paid_minor = cursor.fetchone()[0]
            new_due_minor = (sum_gross or 0) - total_paid_minor
            new_due = from_minor(new_due_minor)

            # Recalculate status (100 Minor Units = 0,01 EUR bei SCALE=4)
            if new_due_minor < 100:
                new_status = 'paid'
            elif total_paid_minor > 0:
                new_status = 'partial_payment'
            else:
                new_status = 'finalized'

            cursor.execute('''
                UPDATE Invoices SET AmountDue = ?, Status = ?, UpdatedAt = CURRENT_TIMESTAMP
                WHERE ID = ?
            ''', (new_due_minor, new_status, invoice_id))

            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()
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
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT SumGross, Status FROM Invoices WHERE ID = ?',
                           (invoice_id,))
            row = cursor.fetchone()
            if not row:
                return
            gross_minor = row[0] or 0
            status = row[1]
            cursor.execute(
                'SELECT COALESCE(SUM(Amount), 0) FROM InvoicePayments WHERE InvoiceID = ?',
                (invoice_id,))
            total_paid_minor = cursor.fetchone()[0]
            new_due_minor = gross_minor - total_paid_minor

            new_status = None
            if adjust_status:
                if total_paid_minor > 0 and new_due_minor < 100:
                    new_status = 'paid'
                elif total_paid_minor > 0:
                    new_status = 'partial_payment'
                elif status in ('paid', 'partial_payment', 'partial'):
                    # Zahlungs-Status ohne Zahlungen → zurück auf Abgeschlossen
                    new_status = 'finalized'

            if new_status:
                cursor.execute('''
                    UPDATE Invoices SET AmountDue = ?, Status = ?,
                                        UpdatedAt = CURRENT_TIMESTAMP
                    WHERE ID = ?''', (new_due_minor, new_status, invoice_id))
            else:
                cursor.execute('''
                    UPDATE Invoices SET AmountDue = ?, UpdatedAt = CURRENT_TIMESTAMP
                    WHERE ID = ?''', (new_due_minor, invoice_id))
            conn.commit()
        finally:
            conn.close()

    def get_open_invoices(self):
        """Offene Rechnungen für die Zahlungs-Zuordnung.

        Nicht draft/cancelled/paid, Restbetrag > 0; Altbestand ohne AmountDue
        (NULL) gilt als offen. Volle Invoice-Rows (Euro-Decimals wie üblich).
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM Invoices
            WHERE (DocumentType = 'invoice' OR DocumentType IS NULL)
            AND Status NOT IN ('draft', 'cancelled', 'paid')
            AND (AmountDue IS NULL OR AmountDue > 0)
            ORDER BY InvoiceDate DESC, InvoiceNumber DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 36, 37, 38, 39) for r in rows]

    def resolve_revenue_coa(self, tax_rate):
        """Passendes Erlöskonto für einen Rechnungs-Steuersatz ermitteln.

        Lernt zuerst aus der eigenen Historie: das zuletzt bei einer
        Zahlungs-Zuordnung verwendete Erlöskonto einer Rechnung mit gleichem
        Steuersatz (0% und §19-Sentinel -1 bilden einen gemeinsamen
        "steuerfrei"-Topf). Fallback auf die Standardkonten: 19% → 4400,
        7% → 4300, steuerfrei/§19 → 4185 (DATEV: "Erlöse als Kleinunternehmer
        nach § 19 Abs. 1 UStG"; wird in älteren DBs ohne dieses Seed-Konto
        automatisch nachgelegt, Vorbild ensure_kasse_exists).
        tax_rate wie in Invoices.TaxRate (0.19, 19, 0 oder -1).
        """
        if tax_rate is None:
            key = -999.0
        elif tax_rate <= 0:
            key = 0.0
        else:
            pct = tax_rate * 100 if tax_rate <= 1 else tax_rate
            key = round(pct, 2)
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT b.COA_ID
            FROM InvoicePayments ip
            JOIN Bookings b ON b.ID = ip.BookingID
            JOIN Invoices i ON i.ID = ip.InvoiceID
            WHERE b.COA_ID IS NOT NULL
              AND (CASE WHEN i.TaxRate IS NULL THEN -999.0
                        WHEN i.TaxRate <= 0 THEN 0.0
                        WHEN i.TaxRate <= 1 THEN ROUND(i.TaxRate * 100, 2)
                        ELSE ROUND(i.TaxRate, 2) END) = ?
            ORDER BY ip.ID DESC LIMIT 1
        ''', (key,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
        if key == 19:
            return self.get_coa_id_by_account_number(4400)
        if key == 7:
            return self.get_coa_id_by_account_number(4300)
        if key == 0:
            return self._ensure_tax_free_revenue_coa()
        return None

    def _ensure_tax_free_revenue_coa(self):
        """Kleinunternehmer-Erlöskonto 4185 liefern (DATEV-SKR04); fehlt es
        (DB älter als der Seed-Eintrag), wird es als Standardkonto im
        dominanten Kontenrahmen nachgelegt – sonst hätten §19-/0%-Rechnungen
        kein Zielkonto."""
        coa = self.get_coa_id_by_account_number(4185)
        if coa:
            return coa
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT Framework FROM ChartOfAccounts '
                       'GROUP BY Framework ORDER BY COUNT(*) DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        framework = row[0] if row else 4
        self.insert_chart_of_accounts(framework, 4185,
                                      'Erlöse als Kleinunternehmer nach § 19 Abs. 1 UStG',
                                      'Betriebliche Erträge', is_standard=1)
        return self.get_coa_id_by_account_number(4185)

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
        """Nicht zugeordneter Rest einer Buchung in Euro (Decimal, min. 0)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ABS(COALESCE(Amount, 0)) - COALESCE(
                       (SELECT SUM(ip.Amount) FROM InvoicePayments ip
                        WHERE ip.BookingID = Bookings.ID), 0)
            FROM Bookings WHERE ID = ?
        ''', (booking_id,))
        row = cursor.fetchone()
        conn.close()
        if not row or row[0] is None or row[0] <= 0:
            return Decimal(0)
        return from_minor(row[0])

    def get_unallocated_bank_bookings(self, contact_id=None, contact_only=False,
                                      limit=20):
        """Bank-Zahlungseingänge mit noch nicht zugeordnetem Rest.

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
        cursor.execute(f'''
            SELECT * FROM (
                SELECT b.ID, b.DateBooking, b.RecipientClient, b.Contact_ID,
                       b.Amount,
                       ABS(b.Amount) - COALESCE((SELECT SUM(ip.Amount)
                                                 FROM InvoicePayments ip
                                                 WHERE ip.BookingID = b.ID), 0)
                           AS FreeMinor
                FROM Bookings b
                WHERE b.BookingType = 'bank' AND b.Amount > 0 {contact_filter}
            )
            WHERE FreeMinor > 0
            ORDER BY CASE WHEN ? IS NOT NULL AND Contact_ID = ? THEN 0 ELSE 1 END,
                     DateBooking DESC, ID DESC
            LIMIT ?
        ''', params + [contact_id, contact_id, limit])
        rows = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 4, 5) for r in rows]

    def get_open_items_for_contact(self, contact_id):
        """Offene Posten eines Kontakts für die Kontaktseite.

        Returns dict:
          open_invoices: [(ID, InvoiceNumber, InvoiceDate, Status, Offen€)]
          credits:       [(BookingID, DateBooking, RecipientClient, Contact_ID,
                           Amount€, Frei€)]  – nicht zugeordnete Zahlungs-Reste
          saldo:         Forderungen − Guthaben (Euro-Decimal)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ID, InvoiceNumber, InvoiceDate, Status,
                   COALESCE(AmountDue, SumGross, 0)
            FROM Invoices
            WHERE CustomerId = ?
              AND (DocumentType = 'invoice' OR DocumentType IS NULL)
              AND Status NOT IN ('draft', 'cancelled', 'paid')
              AND (AmountDue IS NULL OR AmountDue > 0)
            ORDER BY InvoiceDate DESC, ID DESC
        ''', (contact_id,))
        open_invoices = [self._euro_row(r, 4) for r in cursor.fetchall()]
        conn.close()
        credits = self.get_unallocated_bank_bookings(contact_id, contact_only=True,
                                                     limit=100)
        saldo = (sum((r[4] for r in open_invoices), Decimal(0))
                 - sum((r[5] for r in credits), Decimal(0)))
        return {'open_invoices': open_invoices, 'credits': credits, 'saldo': saldo}
