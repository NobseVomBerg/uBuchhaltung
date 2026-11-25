# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: articles."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from .core import DeleteRefused, reference_texts


class ArticleExists(ValueError):
    """Es gibt schon einen Artikel mit dieser Bezeichnung (aktiv oder nicht, ohne Rücksicht auf Schreibweise und Leerraum). Der Aufrufer sagt es dem Nutzer mit eigenem Klartext."""


def article_key(name):
    """Der Vergleichsschlüssel einer Bezeichnung: Leerraum zusammengefasst und außen entfernt, Kleinbuchstaben – dieselbe Regel wie ``artNorm`` der Masken von Rechnung und Angebot:
    Zwei Artikel, die die Maske nicht auseinanderhält, gibt es nicht (sie fände zu keiner Zeile mehr einen von beiden)."""
    return ' '.join(str(name or '').split()).lower()


class ArticlesMixin:
    def fetch_articles(self, active_only=False):
        """Fetch all articles
        
        Args:
            active_only: If True, only return active articles (default: False)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        if active_only:
            cursor.execute('SELECT * FROM Articles WHERE Active = 1 ORDER BY Name')
        else:
            cursor.execute('SELECT * FROM Articles ORDER BY Name')
        rows = cursor.fetchall()
        conn.close()
        # UnitPrice (Index 3) von Minor Units in Euro-Decimal wandeln
        return [self._euro_row(r, 3) for r in rows]
    def get_article_by_id(self, article_id):
        """Get article by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM Articles WHERE ID = ?', (article_id,))
        row = cursor.fetchone()
        conn.close()
        return self._euro_row(row, 3)  # UnitPrice (Index 3) -> Euro-Decimal
    def insert_article(self, name, unit="Stk.", unit_price=0, tax_rate=19, description="", active=1):
        """Insert new article
        
        Args:
            name: Article name (required)
            unit: Unit of measurement (default: Stk.)
            unit_price: Net unit price (default: 0)
            tax_rate: Tax rate in percent (default: 19)
            description: Optional description
            active: Whether article is active (default: 1=True)
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        sql_template = '''
            INSERT INTO Articles (Name, Unit, UnitPrice, TaxRate, Description, Active)
            VALUES (?, ?, ?, ?, ?, ?)'''
        params = (name, unit, to_minor(unit_price or 0), tax_rate, description, active)

        try:
            cursor.execute(sql_template, params)
            conn.commit()
            self._log_sql(sql_template, params, "Insert article")
        except sqlite3.IntegrityError as e:
            print("Error inserting article:", e)
            conn.rollback()
        finally:
            conn.close()
    def insert_article_unique(self, name, unit, unit_price, tax_rate, description=""):
        """Einen aktiven Artikel anlegen – nur, wenn es keinen mit derselben Bezeichnung gibt (``article_key``, aktive und inaktive; sonst ``ArticleExists``, nichts geschrieben).
        Prüfung und Anlegen laufen in EINER Transaktion (``BEGIN IMMEDIATE``): Zwei gleichzeitige Anfragen legen nie zwei Artikel an. Verglichen wird in Python, nicht in SQL
        (``LOWER()`` von SQLite kennt nur ASCII). Liefert den Artikel wie ``get_article_by_id`` (Preis in Euro)."""
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            key = article_key(name)
            if any(article_key(existing) == key for (existing,) in conn.execute('SELECT Name FROM Articles')):
                raise ArticleExists(name)
            sql_template = '''
                INSERT INTO Articles (Name, Unit, UnitPrice, TaxRate, Description, Active)
                VALUES (?, ?, ?, ?, ?, ?)'''
            params = (name, unit, to_minor(unit_price or 0), tax_rate, description, 1)
            article_id = conn.execute(sql_template, params).lastrowid
            row = conn.execute('SELECT * FROM Articles WHERE ID = ?', (article_id,)).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._log_sql(sql_template, params, "Insert article")
        return self._euro_row(row, 3)
    def update_article(self, article_id, name, unit="Stk.", unit_price=0, tax_rate=19, description="", active=1):
        """Update existing article"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            sql_template = '''
                UPDATE Articles
                SET Name = ?, Unit = ?, UnitPrice = ?, TaxRate = ?, Description = ?, Active = ?
                WHERE ID = ?'''
            params = (name, unit, to_minor(unit_price or 0), tax_rate, description, active, article_id)
            cursor.execute(sql_template, params)
            conn.commit()
            self._log_sql(sql_template, params, "Update article")
        except sqlite3.IntegrityError as e:
            print("Error updating article:", e)
            conn.rollback()
        finally:
            conn.close()
    def delete_article(self, article_id):
        """Artikel löschen – nicht, solange er in einer Position einer Rechnung oder eines Angebots steht
        (``DeleteRefused`` mit Klartext, nichts gelöscht). Die Artikelmaske kennt „Aktiv“: Ist der Artikel noch aktiv
        (die Spalte ``Active`` ist wahr; wie in der Maske zählt ein leerer Wert als inaktiv), nennt die Meldung, dass er
        sich stattdessen auf inaktiv setzen lässt – bei einem schon inaktiven Artikel nicht. Prüfung, Lesen von ``Active``
        und Löschen laufen in einer Transaktion."""
        conn = self._get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            used = reference_texts(conn, [
                ('SELECT COUNT(*) FROM InvoiceItems WHERE ArticleId = ?', (article_id,),
                 'Position in Rechnungen oder Angeboten', 'Positionen in Rechnungen oder Angeboten'),
            ])
            if used:
                row = conn.execute('SELECT Active FROM Articles WHERE ID = ?', (article_id,)).fetchone()
                hint = ' Er lässt sich stattdessen auf inaktiv setzen.' if row and row[0] else ''
                raise DeleteRefused('Der Artikel lässt sich nicht löschen, solange er in Belegen steht: '
                                    + used[0] + '.' + hint)
            conn.execute('DELETE FROM Articles WHERE ID = ?', (article_id,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
