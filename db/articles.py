"""Database-Mixin: articles."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


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
        """Delete article"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM Articles WHERE ID = ?', (article_id,))
        conn.commit()
        conn.close()
