# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: receipts."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from .core import DeleteRefused, reference_texts


class ReceiptsMixin:
    def fetch_receipts(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Documents')
        rows = cursor.fetchall()
        conn.close()
        return rows
    def insert_receipt(self, number, date, filename, path, info):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO Documents (Number, Date, Filename, Path, Info)
                VALUES (?, ?, ?, ?, ?)
            ''', (number, date, filename, path, info))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting receipt:", e)
            conn.rollback()
        finally:
            conn.close()
    def update_receipt(self, receipt_id, number, date, filename, path, info):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Documents
                SET Number = ?, Date = ?, Filename = ?, Path = ?, Info = ?
                WHERE ID = ?
            ''', (number, date, filename, path, info, receipt_id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating receipt:", e)
            conn.rollback()
        finally:
            conn.close()
    def get_receipt_by_number(self, number):
        """Get a single receipt by number"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Documents WHERE Number = ?', (number,))
        row = cursor.fetchone()
        conn.close()
        return row
    def delete_receipt(self, number):
        """Beleg(e) mit dieser Nummer löschen – nicht, solange Buchungen, Anlagen oder Fahrten auf einen davon
        verweisen (``DeleteRefused`` mit Klartext, nichts gelöscht). Prüfung und Löschen laufen in einer
        Transaktion."""
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            used = reference_texts(conn, [
                ('SELECT COUNT(DISTINCT Booking_ID) FROM BookingDocuments '
                 'WHERE Document_ID IN (SELECT ID FROM Documents WHERE Number = ?)',
                 (number,), 'Buchung', 'Buchungen'),
                ('SELECT COUNT(*) FROM Assets WHERE Document_ID IN (SELECT ID FROM Documents WHERE Number = ?)',
                 (number,), 'Anlage', 'Anlagen'),
                ('SELECT COUNT(*) FROM Trips WHERE DocumentID IN (SELECT ID FROM Documents WHERE Number = ?)',
                 (number,), 'Fahrt', 'Fahrten'),
            ])
            if used:
                raise DeleteRefused('Der Beleg lässt sich nicht löschen, solange andere Daten auf ihn verweisen: '
                                    + ', '.join(used) + '.')
            conn.execute('DELETE FROM Documents WHERE Number = ?', (number,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
