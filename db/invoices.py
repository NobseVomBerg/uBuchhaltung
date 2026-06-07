"""Database-Mixin: invoices."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class InvoicesMixin:
    def fetch_invoices(self, status=None):
        """Fetch invoices, optionally filtered by status"""
        conn = self._get_connection()
        cursor = conn.cursor()
        if status:
            cursor.execute('SELECT * FROM Invoices WHERE Status = ? ORDER BY InvoiceDate DESC, InvoiceNumber DESC', (status,))
        else:
            cursor.execute('SELECT * FROM Invoices ORDER BY InvoiceDate DESC, InvoiceNumber DESC')
        rows = cursor.fetchall()
        conn.close()
        # SumNet(36), TaxAmount(37), SumGross(38), AmountDue(39) -> Euro-Decimal
        return [self._euro_row(r, 36, 37, 38, 39) for r in rows]
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
                Status, PDFPath, XMLPath
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
            invoice_data.get('xml_path')
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

            # Auto-update status (100 Minor Units = 0,01 EUR bei SCALE=4)
            if abs(new_due_minor) < 100:
                new_status = 'paid'
            elif total_paid_minor > 0:
                new_status = 'partial'
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
            if abs(new_due_minor) < 100:
                new_status = 'paid'
            elif total_paid_minor > 0:
                new_status = 'partial'
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
            AND PaymentDueDate BETWEEN ? AND ?
            AND (AmountDue IS NULL OR AmountDue > 0)
            ORDER BY PaymentDueDate ASC
        ''', (today_str, future_date))
        invoices = cursor.fetchall()
        conn.close()
        return [self._euro_row(r, 36, 37, 38, 39) for r in invoices]
