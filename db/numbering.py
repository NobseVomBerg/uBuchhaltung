# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: numbering."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class NumberRangeMixin:
    @staticmethod
    def _apply_number_format(fmt: str, year: int, letter: str, number: int, suffix: str = '') -> str:
        """Apply a format template to produce a number string.

        Placeholders:
          {yyyy}  - 4-digit year (e.g. 2026)
          {yy}    -  2-digit year (e.g. 26)
          {l}     - letter (uppercase)
          {nnn}   - 3-digit zero-padded number (e.g. 001)
          {nn}    - 2-digit zero-padded number
          {n}     - unpadded number
          {s}     - suffix (appended as-is, empty when not set)

        Default template '{yy}{l}{nnn}{s}' produces '26F001' or '26F002_A'.
        """
        DEFAULT_FORMAT = '{yy}{l}{nnn}{s}'
        template = fmt if fmt else DEFAULT_FORMAT
        result = template
        result = result.replace('{yyyy}', str(year))
        result = result.replace('{yy}', str(year)[-2:])
        result = result.replace('{l}', letter.upper())
        result = result.replace('{nnn}', f'{number:03d}')
        result = result.replace('{nn}', f'{number:02d}')
        result = result.replace('{n}', str(number))
        result = result.replace('{s}', suffix or '')
        return result
    def fetch_number_ranges(self, range_type=None):
        """Fetch all number ranges, optionally filtered by type
        
        Args:
            range_type: Optional filter ('invoice', 'receipt_company', 'receipt_category')
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        if range_type:
            cursor.execute('SELECT * FROM NumberRanges WHERE Type = ? ORDER BY Year DESC, Letter, Prefix', (range_type,))
        else:
            cursor.execute('SELECT * FROM NumberRanges ORDER BY Type, Year DESC, Letter, Prefix')
        rows = cursor.fetchall()
        conn.close()
        return rows
    def get_number_range(self, range_type, year, letter, prefix=''):
        """Get a specific number range"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM NumberRanges 
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()
        conn.close()
        return row
    def get_number_range_by_id(self, range_id):
        """Get number range by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM NumberRanges WHERE ID = ?', (range_id,))
        row = cursor.fetchone()
        conn.close()
        return row
    def insert_number_range(self, range_type, year, letter, prefix='', current_number=0, description='', number_format=None):
        """Insert a new number range

        Args:
            range_type: 'invoice', 'receipt_company', or 'receipt_category'
            year: 4-digit year (will be stored as-is, displayed as 2-digit)
            letter: Single letter identifier (e.g., 'R' for Rechnung)
            prefix: Optional suffix for subdivision (e.g., '_A') – appended after the number
            current_number: Starting number (default: 0)
            description: Optional description
            number_format: Format template (default: '{yy}{l}{nnn}{s}')
        """
        fmt = number_format if number_format else '{yy}{l}{nnn}{s}'
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO NumberRanges (Type, Year, Letter, Prefix, CurrentNumber, Description, NumberFormat)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (range_type, year, letter, prefix, current_number, description, fmt))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error inserting number range:", e)
            conn.rollback()
        finally:
            conn.close()
    def update_number_range(self, range_id, year, letter, prefix='', current_number=0, description='', number_format=None):
        """Update existing number range"""
        fmt = number_format if number_format else '{yy}{l}{nnn}{s}'
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE NumberRanges
                SET Year = ?, Letter = ?, Prefix = ?, CurrentNumber = ?, Description = ?, NumberFormat = ?
                WHERE ID = ?
            ''', (year, letter, prefix, current_number, description, fmt, range_id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating number range:", e)
            conn.rollback()
        finally:
            conn.close()
    def delete_number_range(self, range_id):
        """Delete number range"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM NumberRanges WHERE ID = ?', (range_id,))
        conn.commit()
        conn.close()
    def get_next_number(self, range_type, year, letter, prefix=''):
        """Get the next available number in a range and increment the counter

        Returns the full formatted number string (e.g., '26R001' or '26R001_A')
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Atomic increment: UPDATE...RETURNING avoids SELECT + UPDATE race
        cursor.execute('''
            UPDATE NumberRanges SET CurrentNumber = CurrentNumber + 1
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
            RETURNING CurrentNumber, NumberFormat
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()

        if row:
            next_num, number_format = row
        else:
            # Create new range for this combination
            next_num = 1
            number_format = '{yy}{l}{nnn}{s}'
            cursor.execute('''
                INSERT INTO NumberRanges (Type, Year, Letter, Prefix, CurrentNumber, Description, NumberFormat)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (range_type, year, letter, prefix, next_num, '', number_format))

        conn.commit()
        conn.close()

        return self._apply_number_format(number_format, year, letter, next_num, prefix)
    def format_number(self, year, letter, suffix, number, number_format=None):
        """Format a number using the given template (default: '{yy}{l}{nnn}{s}')"""
        fmt = number_format if number_format else '{yy}{l}{nnn}{s}'
        return self._apply_number_format(fmt, year, letter, number, suffix)
    def parse_number(self, number_str):
        """Parse a formatted number string back to components
        
        Returns: (year, letter, prefix, number) or None if invalid
        """
        import re
        # Pattern: 2-digit year, 1 letter, 3+ digits, optional prefix (_Letter)
        match = re.match(r'^(\d{2})([A-Z])(\d{3,})(_[A-Z])?$', number_str)
        if match:
            year_short = int(match.group(1))
            # Convert 2-digit year to 4-digit (assuming 2000s)
            year = 2000 + year_short if year_short < 100 else year_short
            letter = match.group(2)
            number = int(match.group(3))
            prefix = match.group(4) or ''
            return (year, letter, prefix, number)
        return None
    def shift_numbers_up(self, range_type, year, letter, prefix, from_number):
        """Shift all numbers >= from_number up by 1 in the specified range
        
        This is used when inserting a number that already exists.
        Note: This updates the stored CurrentNumber if needed.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Get current max number
        cursor.execute('''
            SELECT ID, CurrentNumber FROM NumberRanges 
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()
        
        if row:
            range_id, current = row
            if from_number <= current:
                # Increment current number since we're inserting
                cursor.execute('UPDATE NumberRanges SET CurrentNumber = ? WHERE ID = ?', (current + 1, range_id))
        
        conn.commit()
        conn.close()
        
        # Return the new number that was inserted
        return from_number
    def get_current_number_info(self, range_type, year, letter, prefix=''):
        """Get info about the current state of a number range

        Returns: dict with 'current_number', 'next_number', 'formatted_next'
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT CurrentNumber, NumberFormat FROM NumberRanges
            WHERE Type = ? AND Year = ? AND Letter = ? AND Prefix = ?
        ''', (range_type, year, letter, prefix))
        row = cursor.fetchone()
        conn.close()

        current = row[0] if row else 0
        number_format = row[1] if row else '{yy}{l}{nnn}{s}'
        next_num = current + 1

        return {
            'current_number': current,
            'next_number': next_num,
            'formatted_next': self._apply_number_format(number_format, year, letter, next_num, prefix)
        }
