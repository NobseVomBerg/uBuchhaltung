# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: trips (Fahrtenbuch)."""
import sqlite3


class TripsMixin:
    def fetch_trips(self, driver_id, date_from=None, date_to=None):
        """Fahrten eines Fahrers, optional auf Zeitraum [from, to] begrenzt.

        Sortiert nach Start-Datum, dann Start-Uhrzeit. Liefert sqlite3.Row-Liste.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        sql = 'SELECT * FROM Trips WHERE DriverID = ?'
        params = [driver_id]
        if date_from and date_to:
            sql += ' AND StartDate >= ? AND StartDate <= ?'
            params.extend([date_from, date_to])
        sql += ' ORDER BY StartDate, StartTime, ID'
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_trip_by_id(self, trip_id):
        """Einzelne Fahrt holen (oder None)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Trips WHERE ID = ?', (trip_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def get_last_trip(self, driver_id):
        """Neueste Fahrt des Fahrers (Vorlage für neue Einträge, z.B. Fahrzeug)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM Trips
            WHERE DriverID = ?
            ORDER BY StartDate DESC, ID DESC
            LIMIT 1
        ''', (driver_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    def get_known_routes(self, driver_id):
        """Bekannte Strecken des Fahrers als {"start|ziel": km} für die km-Vorbelegung.

        Schlüssel ist "<startpunkt>|<ziel>" (case-insensitiv, getrimmt); leerer
        Startpunkt bleibt leer (= eigene Adresse). Bei mehreren Fahrten derselben
        Strecke gewinnt die zuletzt gefahrene (jüngstes Datum/ID).
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT StartPoint, Destination, DistanceKm
            FROM Trips
            WHERE DriverID = ? AND Destination IS NOT NULL AND DistanceKm IS NOT NULL
            ORDER BY StartDate ASC, ID ASC
        ''', (driver_id,))
        routes = {}
        for start, dest, km in cursor.fetchall():
            key = f"{(start or '').strip().lower()}|{(dest or '').strip().lower()}"
            routes[key] = km   # spätere (jüngere) Fahrt überschreibt → letzter Wert gewinnt
        conn.close()
        return routes

    def get_vehicles(self, driver_id):
        """Bisher genutzte Fahrzeuge des Fahrers (für datalist-Vorschläge)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT DISTINCT Vehicle FROM Trips
            WHERE DriverID = ? AND Vehicle IS NOT NULL AND TRIM(Vehicle) <> ''
            ORDER BY Vehicle
        ''', (driver_id,))
        vehicles = [r[0] for r in cursor.fetchall()]
        conn.close()
        return vehicles

    def insert_trip(self, driver_id, start_date, start_time='', end_date='',
                    end_time='', start_point='', destination='', vehicle='',
                    reason='', distance_km=None, start_km=None, end_km=None,
                    document_id=None):
        """Neue Fahrt anlegen. Liefert die neue ID (oder None)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        new_id = None
        try:
            cursor.execute('''
                INSERT INTO Trips
                    (DriverID, StartDate, StartTime, EndDate, EndTime, StartPoint,
                     Destination, Vehicle, Reason, DistanceKm, StartKm, EndKm, DocumentID)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (driver_id, start_date, start_time, end_date, end_time, start_point,
                  destination, vehicle, reason, distance_km, start_km, end_km,
                  document_id))
            conn.commit()
            new_id = cursor.lastrowid
        except sqlite3.IntegrityError as e:
            print("Error inserting trip:", e)
            conn.rollback()
        finally:
            conn.close()
        return new_id

    def update_trip(self, trip_id, start_date, start_time='', end_date='',
                    end_time='', start_point='', destination='', vehicle='',
                    reason='', distance_km=None, start_km=None, end_km=None,
                    document_id=None):
        """Bestehende Fahrt aktualisieren (DriverID bleibt unverändert)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Trips
                SET StartDate = ?, StartTime = ?, EndDate = ?, EndTime = ?,
                    StartPoint = ?, Destination = ?, Vehicle = ?, Reason = ?,
                    DistanceKm = ?, StartKm = ?, EndKm = ?, DocumentID = ?
                WHERE ID = ?
            ''', (start_date, start_time, end_date, end_time, start_point,
                  destination, vehicle, reason, distance_km, start_km, end_km,
                  document_id, trip_id))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print("Error updating trip:", e)
            conn.rollback()
        finally:
            conn.close()

    def delete_trip(self, trip_id):
        """Fahrt per ID löschen."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM Trips WHERE ID = ?', (trip_id,))
        conn.commit()
        conn.close()
