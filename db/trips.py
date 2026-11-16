# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: trips (Fahrtenbuch)."""
import sqlite3

#: Die Lückenprüfung (``logbook_gaps``) nennt höchstens so viele Lücken; ``gaps_count`` zählt alle.
LOGBOOK_MAX_GAPS = 100


def _odometer(value):
    """Ein Tachostand oder Kilometerwert als ganze Zahl – ``None``, wenn er fehlt oder keine ist (ein Eingriff von
    Hand, z. B. über die SQL-Konsole, kann Text in die Spalte geschrieben haben)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _vehicle_key(value):
    """Das Fahrzeug, wie die Lückenprüfung es vergleicht: getrimmt und ohne Groß-/Kleinschreibung."""
    return str(value or '').strip().casefold()


def logbook_gaps(rows):
    """Lücken zwischen den Tachoständen der Fahrten ``rows`` (eines Fahrzeugs in einem Jahr) – rein rechnend.

    ``rows``: Fahrten mit den Spalten ``ID``, ``DriverID``, ``StartDate``, ``StartTime``, ``DistanceKm``,
    ``StartKm``, ``EndKm`` (``sqlite3.Row`` oder ``dict``), in beliebiger Reihenfolge. Sortiert wird nach Start-Datum,
    Start-Uhrzeit (leer zuerst) und ID.

    Ein Eintrag in ``gaps`` entsteht für jede Fahrt MIT Tacho Start, deren nächster Vorgänger MIT Tacho Ende einen
    anderen Stand hat: ``diff`` = Start − Ende des Vorgängers (positiv: nicht aufgezeichnete Kilometer, negativ:
    Überschneidung), ``between`` = Zahl der Fahrten zwischen beiden (sie haben gar keinen Tachostand). Fahrten ohne
    Tachostand bilden keine Lücke; ``without_odometer`` zählt die, denen Start oder Ende fehlt. Eine Fahrt mit Tacho
    Start, aber ohne Ende, wird selbst noch verglichen; danach ist der Stand unbekannt (ihre eigene Strecke fehlt), und
    die nächste Fahrt hat keinen Vorgänger – dieselben Kilometer stehen so nie in zwei Einträgen. ``gaps`` hat
    höchstens ``LOGBOOK_MAX_GAPS`` Einträge, ``gaps_count`` die wahre Zahl.
    """
    trips = sorted(rows, key=lambda r: (str(r['StartDate'] or ''), str(r['StartTime'] or ''), r['ID']))
    total_km = without_odometer = gaps_count = 0
    first_start_km = last_end_km = None
    gaps = []
    previous = None                           # (Stelle, Fahrt, Tacho Ende) der jüngsten Fahrt mit Tacho Ende
    for index, trip in enumerate(trips):
        start, end = _odometer(trip['StartKm']), _odometer(trip['EndKm'])
        total_km += _odometer(trip['DistanceKm']) or 0
        if start is None or end is None:
            without_odometer += 1
        if start is not None:
            if first_start_km is None:
                first_start_km = start
            if previous is not None and start != previous[2]:
                previous_index, before, previous_end = previous
                gaps_count += 1
                if len(gaps) < LOGBOOK_MAX_GAPS:
                    gaps.append({
                        'prev_id': before['ID'], 'prev_driver': before['DriverID'],
                        'prev_date': before['StartDate'], 'prev_end_km': previous_end,
                        'next_id': trip['ID'], 'next_driver': trip['DriverID'],
                        'next_date': trip['StartDate'], 'next_start_km': start,
                        'diff': start - previous_end, 'between': index - previous_index - 1,
                    })
        if end is not None:
            last_end_km = end
            previous = (index, trip, end)
        elif start is not None:
            previous = None                   # Start ohne Ende: Der Stand danach ist unbekannt
    return {'trips': len(trips), 'total_km': total_km, 'without_odometer': without_odometer,
            'first_start_km': first_start_km, 'last_end_km': last_end_km,
            'gaps': gaps, 'gaps_count': gaps_count}


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

    def get_all_vehicles(self):
        """Alle Fahrzeuge aller Fahrer (nicht leer, getrimmt), ohne Dubletten nach Groß-/Kleinschreibung, sortiert –
        für die Lückenprüfung des Fahrtenbuchs."""
        conn = self._get_connection()
        try:
            names = [r[0] for r in conn.execute('SELECT DISTINCT Vehicle FROM Trips WHERE Vehicle IS NOT NULL')]
        finally:
            conn.close()
        vehicles = {}
        for name in sorted({str(n).strip() for n in names} - {''}, key=lambda n: (n.casefold(), n)):
            vehicles.setdefault(name.casefold(), name)
        return list(vehicles.values())

    def trip_logbook_check(self, vehicle, year):
        """Lückenprüfung des Fahrtenbuchs für ein Fahrzeug und ein Kalenderjahr – rein lesend.

        Geprüft werden die Fahrten aller Fahrer, deren Start-Datum im Jahr liegt und deren Fahrzeug – getrimmt und
        ohne Groß-/Kleinschreibung – dem gesuchten gleicht (verglichen in Python mit ``casefold``, nicht per SQL).
        Das Ergebnis rechnet ``logbook_gaps``."""
        wanted = _vehicle_key(vehicle)
        year = int(year)
        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                'SELECT ID, DriverID, StartDate, StartTime, Vehicle, DistanceKm, StartKm, EndKm FROM Trips '
                'WHERE StartDate >= ? AND StartDate < ?',
                (f'{year:04d}-01-01', f'{year + 1:04d}-01-01')).fetchall()
        finally:
            conn.close()
        return logbook_gaps([r for r in rows if _vehicle_key(r['Vehicle']) == wanted])

    def insert_trip(self, driver_id, start_date, start_time='', end_date='',
                    end_time='', start_point='', destination='', vehicle='',
                    reason='', distance_km=None, start_km=None, end_km=None,
                    document_id=None):
        """Neue Fahrt anlegen. Liefert die neue ID – oder ``None``, wenn sie sich nicht speichern ließ (Fahrer oder
        Beleg gibt es nicht): Dann ist nichts geschrieben."""
        conn = self._get_connection()
        cursor = conn.cursor()
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
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            conn.rollback()
            return None
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_trip(self, trip_id, start_date, start_time='', end_date='',
                    end_time='', start_point='', destination='', vehicle='',
                    reason='', distance_km=None, start_km=None, end_km=None,
                    document_id=None):
        """Bestehende Fahrt aktualisieren (DriverID bleibt unverändert). ``True``, wenn die Fahrt gespeichert ist;
        ``False``, wenn es die ID nicht gibt oder der Beleg nicht (mehr) existiert – dann ist nichts geändert."""
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
            return cursor.rowcount > 0
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_trip(self, trip_id):
        """Fahrt per ID löschen."""
        conn = self._get_connection()
        try:
            conn.execute('DELETE FROM Trips WHERE ID = ?', (trip_id,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
