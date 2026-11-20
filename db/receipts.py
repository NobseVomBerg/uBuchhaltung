# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: receipts."""
import sqlite3
import os
import json
import traceback
from decimal import Decimal
from money import to_minor, from_minor
from .core import DeleteRefused, add_fold_function, fold_text, reference_texts, search_limit


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
    def insert_receipt_entry(self, requested_number, date, filename, info):
        """Einen Beleg ohne Datei anlegen (Papierbeleg, Formular der Belege-Seite). Die Nummer kommt aus dem Kreis
        (``take_receipt_number``; gibt es noch keinen, entsteht einer für das laufende Jahr) und wird dabei verbraucht – beides
        in EINER Transaktion; ``requested_number`` ist eine eigene Nummer oder ``None``. Returns ``(ID, Nummer)``."""
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            number = self.take_receipt_number(conn, date, requested_number)
            cursor = conn.execute('INSERT INTO Documents (Number, Date, Filename, Path, Info) VALUES (?, ?, ?, ?, ?)',
                                  (number, date, filename, '', info))
            receipt_id = cursor.lastrowid
            conn.commit()
            return receipt_id, number
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    def insert_receipt_with_file(self, date, original_name, info, content, requested_number=None):
        """Einen Beleg samt Datei anlegen: Nummer, Datei und Eintrag gehören zusammen.

        VOR der Transaktion: den Jahresordner ``<Datenordner>/Belege/<Jahr des Belegdatums>`` anlegen und prüfen (er darf kein Link
        sein, sonst ``UnsafeFolder``) und den Inhalt in eine Teildatei (``<32 Hex-Zeichen>.part``) in ihm schreiben – das Schreiben
        von bis zu 200 MB hält keine Schreibsperre. In EINER Transaktion (``BEGIN IMMEDIATE``): Nummer aus dem Kreis ziehen, den
        Namen ``<Nummer>_<Name>`` belegen (ohne Nummer nur der Name; nie wird eine vorhandene Datei überschrieben – ein belegter
        Name bekommt ``_2``, ``_3`` …), die Teildatei an seinen Platz setzen und den Eintrag schreiben, dessen ``Path`` relativ zum
        Datenordner des Benutzers steht (mit ``/``). Scheitert irgendein Schritt, auch das ``COMMIT``, ist der Zähler nicht
        verbraucht (und kein Kreis angelegt), es gibt keinen Eintrag, und weder die Datei noch die Teildatei bleibt liegen: Sie
        werden vor dem Zurückrollen entfernt, ein scheiterndes Zurückrollen verhindert das nicht und verdeckt die Ausnahme nicht.

        ``original_name`` ist der Name aus dem Upload (er wird für den Dateinamen bereinigt und, gekürzt, als ``Filename``
        gespeichert). Returns ``(ID, Nummer, relativer Pfad)``."""
        from server import receipt_files                    # ``server`` importiert diese Schicht: erst zur Laufzeit
        year = f'{self._year_of(date):04d}'                 # vier Ziffern, wie sie ``receipt_files._uploaded_shape`` verlangt
        folder = receipt_files.year_folder(year)
        part = receipt_files.write_part(folder, content)
        conn = target = None
        try:
            conn = self._get_connection()
            conn.execute('BEGIN IMMEDIATE')
            number = self.take_receipt_number(conn, date, requested_number)
            target, file_name = receipt_files.reserve_name(folder, receipt_files.stored_file_name(number, original_name))
            os.replace(part, target)                        # ersetzt nur die leere Datei, die ``reserve_name`` eben selbst angelegt hat
            part = None
            relative = '/'.join((receipt_files.BELEGE_DIR, year, file_name))
            cursor = conn.execute('INSERT INTO Documents (Number, Date, Filename, Path, Info) VALUES (?, ?, ?, ?, ?)',
                                  (number, date, receipt_files.display_name(original_name), relative, info))
            receipt_id = cursor.lastrowid
            conn.commit()
            return receipt_id, number, relative
        except BaseException:
            for leftover in (target, part):
                if leftover is not None:
                    receipt_files.remove_quietly(leftover)
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    traceback.print_exc()                   # die Dateien sind weg; die ursprüngliche Ausnahme geht weiter
            raise
        finally:
            if conn is not None:
                conn.close()
    def update_receipt(self, receipt_id, number, date, filename, info):
        """Nummer, Datum, Dateiname und Info ändern. Der gespeicherte Pfad bleibt, wie er ist: Die Datei zieht nicht um,
        auch wenn sich das Datum ändert."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE Documents
                SET Number = ?, Date = ?, Filename = ?, Info = ?
                WHERE ID = ?
            ''', (number, date, filename, info, receipt_id))
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
    def get_receipt_by_id(self, receipt_id):
        """Genau EINEN Beleg über seine ID lesen (die Belegnummer ist nicht eindeutig); ``None``, wenn es ihn nicht gibt."""
        conn = self._get_connection()
        try:
            return conn.execute('SELECT * FROM Documents WHERE ID = ?', (receipt_id,)).fetchone()
        finally:
            conn.close()
    def search_receipts(self, text, date_prefix, limit):
        """Belege für die Suche an der Buchung – rein lesend: Rohzeilen ``(ID, Number, Date, Filename, Path, Info)``, jüngste
        zuerst, höchstens ``limit`` (begrenzt auf 1 bis ``SEARCH_LIMIT_MAX``).

        Gefunden wird, was ``text`` in Nummer, Dateiname oder Info enthält – ohne Rücksicht auf die Schreibweise, auch bei Umlauten
        und ß (``ub_fold`` + ``INSTR``: ``LIKE`` faltet in SQLite nur ASCII, und ``%`` und ``_`` sind hier gewöhnliche Zeichen) –,
        dazu die Belege, deren Datum mit ``date_prefix`` beginnt (``JJJJ``, ``JJJJ-MM``, ``JJJJ-MM-TT``; ``None``, wenn die Eingabe
        keines ist). Ohne ``text`` gibt es keine Bedingung: die jüngsten Belege."""
        needle = fold_text(text or '')
        select = 'SELECT ID, Number, Date, Filename, Path, Info FROM Documents'
        order = ' ORDER BY Date DESC, ID DESC LIMIT :n'
        if needle:
            sql = (select + ' WHERE INSTR(ub_fold(Number), :t) > 0 OR INSTR(ub_fold(Filename), :t) > 0'
                            ' OR INSTR(ub_fold(Info), :t) > 0'
                            ' OR (:d IS NOT NULL AND SUBSTR(Date, 1, LENGTH(:d)) = :d)' + order)
            params = {'t': needle, 'd': date_prefix, 'n': search_limit(limit)}
        else:
            sql, params = select + order, {'n': search_limit(limit)}
        conn = self._get_connection()
        try:
            add_fold_function(conn)
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    def receipt_link_counts(self):
        """Je Beleg die Zahl seiner verknüpften Buchungen: ``{ID des Belegs: Zahl}`` – Belege ohne Verknüpfung fehlen."""
        conn = self._get_connection()
        try:
            return {document_id: count for document_id, count in conn.execute(
                'SELECT Document_ID, COUNT(*) FROM BookingDocuments GROUP BY Document_ID')}
        finally:
            conn.close()
    def delete_receipt(self, receipt_id):
        """Genau EINEN Beleg löschen (über seine ID – die Belegnummer ist nicht eindeutig, mehrere Belege können sie tragen).
        Nicht, solange Buchungen, Anlagen oder Fahrten auf ihn verweisen (``DeleteRefused`` mit Klartext, nichts gelöscht); die
        Belege mit derselben Nummer bleiben in jedem Fall. Prüfung und Löschen laufen in einer Transaktion.

        Trug der Beleg die zuletzt vergebene Nummer seines Kreises, geht der Zähler um eins zurück (``release_receipt_number``,
        in derselben Transaktion: abgelehnt oder gescheitert ändert sich auch der Zähler nicht).

        Returns den gespeicherten Pfad des gelöschten Belegs (``None``: es gab ihn nicht oder er hatte keinen) – die Datei
        räumt der Aufrufer weg (``server/receipt_files.remove_receipt_file``)."""
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            used = reference_texts(conn, [
                ('SELECT COUNT(DISTINCT Booking_ID) FROM BookingDocuments WHERE Document_ID = ?',
                 (receipt_id,), 'Buchung', 'Buchungen'),
                ('SELECT COUNT(*) FROM Assets WHERE Document_ID = ?', (receipt_id,), 'Anlage', 'Anlagen'),
                ('SELECT COUNT(*) FROM Trips WHERE DocumentID = ?', (receipt_id,), 'Fahrt', 'Fahrten'),
            ])
            if used:
                raise DeleteRefused('Der Beleg lässt sich nicht löschen, solange andere Daten auf ihn verweisen: '
                                    + ', '.join(used) + '.')
            row = conn.execute('SELECT Number, Path FROM Documents WHERE ID = ?', (receipt_id,)).fetchone()
            conn.execute('DELETE FROM Documents WHERE ID = ?', (receipt_id,))
            if row is not None:
                self.release_receipt_number(conn, row[0])
            conn.commit()
            return row[1] if row is not None else None
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
