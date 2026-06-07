"""Database-Mixin: contacts."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor


class ContactsMixin:
    def fetch_contacts(self, contact_type=None, entity_type=None):
        """Fetch contacts, optionally filtered by ContactType and/or EntityType.
        Returns sqlite3.Row objects (support both index and column-name access)."""
        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        conditions = []
        params = []
        if contact_type:
            if contact_type == 'own':
                # 'own' ist ein Systemtyp, der direkt in Contacts.ContactType steht
                conditions.append("c.ContactType = 'own'")
            else:
                # Alle anderen Typen werden über ContactTypeLinks geprüft
                conditions.append(
                    'EXISTS (SELECT 1 FROM ContactTypeLinks ctl'
                    ' WHERE ctl.ContactID = c.ID AND ctl.TypeKey = ?)'
                )
                params.append(contact_type)
        if entity_type:
            conditions.append('c.EntityType = ?')
            params.append(entity_type)

        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        cursor.execute(f'{self._CONTACTS_QUERY} {where} ORDER BY display_name ASC', params)
        rows = cursor.fetchall()
        conn.close()
        return rows
    def get_contact_by_id(self, contact_id):
        """Get full contact row by ID (same column layout as fetch_contacts)."""
        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f'{self._CONTACTS_QUERY} WHERE c.ID = ?', (contact_id,))
        row = cursor.fetchone()
        conn.close()
        return row
    def check_abbreviation_unique(self, abbreviation, exclude_id=None):
        """Check if abbreviation is unique. Returns (is_unique, suggestion)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        if exclude_id:
            cursor.execute('SELECT COUNT(*) FROM Contacts WHERE Abbreviation=? AND ID!=?',
                           (abbreviation, int(exclude_id)))
        else:
            cursor.execute('SELECT COUNT(*) FROM Contacts WHERE Abbreviation=?', (abbreviation,))
        exists = cursor.fetchone()[0] > 0
        conn.close()
        if not exists:
            return True, abbreviation
        base = abbreviation.rstrip('0123456789') or abbreviation
        n = 2
        conn = self._get_connection()
        cursor = conn.cursor()
        while n <= 999:
            candidate = f'{base}{n}'
            if exclude_id:
                cursor.execute('SELECT COUNT(*) FROM Contacts WHERE Abbreviation=? AND ID!=?',
                               (candidate, int(exclude_id)))
            else:
                cursor.execute('SELECT COUNT(*) FROM Contacts WHERE Abbreviation=?', (candidate,))
            if cursor.fetchone()[0] == 0:
                conn.close()
                return False, candidate
            n += 1
        conn.close()
        return False, f'{base}999'
    def insert_contact(self, contact_type='customer', entity_type='company',
                       display_name=None, customer_number=None, abbreviation='',
                       email='', phone='', notes='', logo='',
                       # address
                       address_line1='', street='', postal_code='', city='', country='DE',
                       # company
                       company_name='', legal_form='', tax_id='', buyer_route_id='',
                       # person
                       salutation='', title='', first_name='', last_name='',
                       date_of_birth='', company_contact_id=None, company_name_free='',
                       job_title='', department='', is_primary_contact=0,
                       # multi-value
                       type_keys=None, role_keys=None):
        """Insert a new contact with sub-table records."""
        conn = self._get_connection()
        cursor = conn.cursor()
        if not customer_number or not str(customer_number).strip():
            customer_number = None
        try:
            cursor.execute(
                'INSERT INTO Contacts (ContactType, EntityType, DisplayName, CustomerNumber, Abbreviation, Email, Phone, Notes, Logo) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (contact_type, entity_type, display_name or None, customer_number,
                 abbreviation or None, email, phone, notes, logo)
            )
            contact_id = cursor.lastrowid

            cursor.execute(
                'INSERT INTO ContactAddresses (ContactID, AddressType, AddressLine1, Street, PostalCode, City, Country) '
                'VALUES (?, \'main\', ?, ?, ?, ?, ?)',
                (contact_id, address_line1 or None, street, postal_code, city, country or 'DE')
            )

            if entity_type == 'company':
                cursor.execute(
                    'INSERT INTO CompanyDetails (ContactID, CompanyName, LegalForm, TaxID, BuyerRouteID) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (contact_id, company_name or None, legal_form or None,
                     tax_id or None, buyer_route_id or None)
                )
            elif entity_type == 'person':
                cursor.execute(
                    'INSERT INTO PersonDetails '
                    '(ContactID, Salutation, Title, FirstName, LastName, DateOfBirth, '
                    'CompanyContactID, CompanyName_Free, JobTitle, Department, IsPrimaryContact) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (contact_id, salutation or None, title or None,
                     first_name, last_name, date_of_birth or None,
                     int(company_contact_id) if company_contact_id else None,
                     company_name_free or None,
                     job_title or None, department or None, 1 if is_primary_contact else 0)
                )

            # ContactTypeLinks: alle Typen außer 'own' (Systemtyp)
            if contact_type != 'own':
                all_types = list(dict.fromkeys([contact_type] + list(type_keys or [])))
                for tk in all_types:
                    if tk and tk != 'own':
                        cursor.execute(
                            'INSERT OR IGNORE INTO ContactTypeLinks (ContactID, TypeKey) VALUES (?, ?)',
                            (contact_id, tk)
                        )

            # PersonRoles
            if entity_type == 'person' and role_keys:
                for rk in role_keys:
                    if rk:
                        cursor.execute(
                            'INSERT OR IGNORE INTO PersonRoles (ContactID, RoleKey) VALUES (?, ?)',
                            (contact_id, rk)
                        )

            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f'Error inserting contact: {e}')
            raise
        finally:
            conn.close()
    def update_contact(self, contact_id, contact_type='customer', entity_type='company',
                       display_name=None, customer_number=None, abbreviation='',
                       email='', phone='', notes='', logo='',
                       address_line1='', street='', postal_code='', city='', country='DE',
                       company_name='', legal_form='', tax_id='', buyer_route_id='',
                       salutation='', title='', first_name='', last_name='',
                       date_of_birth='', company_contact_id=None, company_name_free='',
                       job_title='', department='', is_primary_contact=0,
                       type_keys=None, role_keys=None):
        """Update an existing contact and all sub-table records."""
        conn = self._get_connection()
        cursor = conn.cursor()
        if not customer_number or not str(customer_number).strip():
            customer_number = None
        try:
            cursor.execute(
                'UPDATE Contacts SET ContactType=?, EntityType=?, DisplayName=?, CustomerNumber=?, Abbreviation=?, '
                'Email=?, Phone=?, Notes=?, Logo=? WHERE ID=?',
                (contact_type, entity_type, display_name or None, customer_number,
                 abbreviation or None, email, phone, notes, logo, contact_id)
            )
            # Address: delete + re-insert
            cursor.execute('DELETE FROM ContactAddresses WHERE ContactID=? AND AddressType=\'main\'',
                           (contact_id,))
            cursor.execute(
                'INSERT INTO ContactAddresses (ContactID, AddressType, AddressLine1, Street, PostalCode, City, Country) '
                'VALUES (?, \'main\', ?, ?, ?, ?, ?)',
                (contact_id, address_line1 or None, street, postal_code, city, country or 'DE')
            )
            # Entity details: delete + re-insert
            cursor.execute('DELETE FROM CompanyDetails WHERE ContactID=?', (contact_id,))
            cursor.execute('DELETE FROM PersonDetails  WHERE ContactID=?', (contact_id,))
            if entity_type == 'company':
                cursor.execute(
                    'INSERT INTO CompanyDetails (ContactID, CompanyName, LegalForm, TaxID, BuyerRouteID) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (contact_id, company_name or None, legal_form or None,
                     tax_id or None, buyer_route_id or None)
                )
            elif entity_type == 'person':
                cursor.execute(
                    'INSERT INTO PersonDetails '
                    '(ContactID, Salutation, Title, FirstName, LastName, DateOfBirth, '
                    'CompanyContactID, CompanyName_Free, JobTitle, Department, IsPrimaryContact) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (contact_id, salutation or None, title or None,
                     first_name, last_name, date_of_birth or None,
                     int(company_contact_id) if company_contact_id else None,
                     company_name_free or None,
                     job_title or None, department or None, 1 if is_primary_contact else 0)
                )
            # ContactTypeLinks: delete + re-insert (nur für Nicht-'own'-Kontakte)
            if contact_type != 'own':
                cursor.execute('DELETE FROM ContactTypeLinks WHERE ContactID=?', (contact_id,))
                all_types = list(dict.fromkeys([contact_type] + list(type_keys or [])))
                for tk in all_types:
                    if tk and tk != 'own':
                        cursor.execute(
                            'INSERT OR IGNORE INTO ContactTypeLinks (ContactID, TypeKey) VALUES (?, ?)',
                            (contact_id, tk)
                        )
            # PersonRoles: delete + re-insert
            cursor.execute('DELETE FROM PersonRoles WHERE ContactID=?', (contact_id,))
            if entity_type == 'person' and role_keys:
                for rk in role_keys:
                    if rk:
                        cursor.execute(
                            'INSERT OR IGNORE INTO PersonRoles (ContactID, RoleKey) VALUES (?, ?)',
                            (contact_id, rk)
                        )
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f'Error updating contact: {e}')
            raise
        finally:
            conn.close()
    def delete_contact(self, contact_id):
        """Delete contact (sub-tables are CASCADE deleted)."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM Contacts WHERE ID = ?', (contact_id,))
        conn.commit()
        conn.close()
