"""Database-Mixin: worktimes."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class WorkTimesMixin:
    def fetch_worktimes(self, person_id, date_from=None, date_to=None):
        """Arbeitszeiten einer Person, optional auf Zeitraum [from, to] begrenzt.

        Sortiert nach Datum, dann Startzeit. Liefert sqlite3.Row-Liste.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        sql = 'SELECT * FROM WorkTimes WHERE PersonID = ?'
        params = [person_id]
        if date_from and date_to:
            sql += ' AND Date >= ? AND Date <= ?'
            params.extend([date_from, date_to])
        sql += ' ORDER BY Date, StartTime'
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return rows
    def get_worktime_by_id(self, worktime_id):
        """Einzelnen Arbeitszeit-Eintrag holen (oder None)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM WorkTimes WHERE ID = ?', (worktime_id,))
        row = cursor.fetchone()
        conn.close()
        return row
    def get_last_worktime_for_person(self, person_id):
        """Neuester Arbeitszeit-Eintrag der Person (Vorlage für neue Einträge)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM WorkTimes
            WHERE PersonID = ? AND Kind = 'work'
            ORDER BY Date DESC, ID DESC
            LIMIT 1
        ''', (person_id,))
        row = cursor.fetchone()
        conn.close()
        return row
    def insert_worktime(self, person_id, date, kind='work', customer_id=None,
                        start_time='', end_time='', pause_minutes=0,
                        location_mode='customer', location_city='', note='', pause_text=''):
        """Neuen Arbeitszeit-Eintrag anlegen. Liefert die neue ID (oder None)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        new_id = None
        try:
            cursor.execute('''
                INSERT INTO WorkTimes
                    (PersonID, Date, Kind, CustomerID, StartTime, EndTime,
                     PauseMinutes, LocationMode, LocationCity, Note, PauseText)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (person_id, date, kind, customer_id, start_time, end_time,
                  pause_minutes, location_mode, location_city, note, pause_text))
            conn.commit()
            new_id = cursor.lastrowid
        except sqlite3.IntegrityError as e:
            print("Error inserting worktime:", e)
            conn.rollback()
        finally:
            conn.close()
        return new_id
    def update_worktime(self, worktime_id, date, kind='work', customer_id=None,
                        start_time='', end_time='', pause_minutes=0,
                        location_mode='customer', location_city='', note='', pause_text=''):
        """Bestehenden Arbeitszeit-Eintrag aktualisieren (PersonID bleibt unverändert)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE WorkTimes
                SET Date = ?, Kind = ?, CustomerID = ?, StartTime = ?, EndTime = ?,
                    PauseMinutes = ?, LocationMode = ?, LocationCity = ?, Note = ?, PauseText = ?
                WHERE ID = ?
            ''', (date, kind, customer_id, start_time, end_time, pause_minutes,
                  location_mode, location_city, note, pause_text, worktime_id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating worktime:", e)
            conn.rollback()
        finally:
            conn.close()
    def delete_worktime(self, worktime_id):
        """Arbeitszeit-Eintrag per ID löschen."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM WorkTimes WHERE ID = ?', (worktime_id,))
        conn.commit()
        conn.close()
