# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Database-Mixin: contacts."""
import sqlite3
import os
import json
from decimal import Decimal
from money import to_minor, from_minor
from . import datev_keys


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
    def get_datev_numbers(self):
        """Berater- und Mandantennummer für den Vorlauf des DATEV-Stapels, aus den Stammdaten der eigenen Firma:
        ``(Beraternummer, Mandantennummer)`` als Ziffernfolgen (4–7 bzw. 1–5 Stellen) – oder ``(None, None)``,
        wenn sie nicht beide eingetragen und gültig sind (ein per SQL-Konsole gesetzter anderer Wert gilt als nicht
        eingetragen). Der Stapel trägt dann Platzhalter. Gibt es mehrere eigene Firmen, gilt die erste mit beiden."""
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT cd.DatevConsultantNo, cd.DatevClientNo FROM Contacts c "
                "JOIN CompanyDetails cd ON cd.ContactID = c.ID WHERE c.ContactType = 'own' ORDER BY c.ID").fetchall()
        finally:
            conn.close()
        for consultant, client in rows:
            consultant, client = datev_keys.consultant_no(consultant), datev_keys.client_no(client)
            if consultant and client:
                return consultant, client
        return None, None

    @staticmethod
    def _datev_numbers_for(contact_type, entity_type, consultant_no, client_no, stored=(None, None)):
        """``(Beraternummer, Mandantennummer)``, wie ``CompanyDetails`` sie bekommt. Nur die eigene Firma
        (``contact_type`` ``own``, ``entity_type`` ``company``) trägt sie, bei jedem anderen Kontakt bleiben beide
        leer (NULL). ``None`` heißt „unverändert“ (``stored``), ein leerer Text „leeren“; alles andere muss eine
        Ziffernfolge von 4–7 bzw. 1–5 Stellen sein, sonst ``ValueError`` mit Klartext."""
        if contact_type != 'own' or entity_type != 'company':
            return None, None
        numbers = []
        for given, kept, valid, error in ((consultant_no, stored[0], datev_keys.consultant_no,
                                           datev_keys.CONSULTANT_NO_ERROR),
                                          (client_no, stored[1], datev_keys.client_no, datev_keys.CLIENT_NO_ERROR)):
            if given is None:
                numbers.append(kept)
            elif str(given).strip() == '':
                numbers.append(None)
            else:
                number = valid(given)
                if number is None:
                    raise ValueError(error)
                numbers.append(number)
        return tuple(numbers)

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
                       type_keys=None, role_keys=None,
                       # DATEV-Vorlauf (nur die eigene Firma, sonst ignoriert)
                       datev_consultant_no=None, datev_client_no=None):
        """Insert a new contact with sub-table records."""
        conn = self._get_connection()
        cursor = conn.cursor()
        if not customer_number or not str(customer_number).strip():
            customer_number = None
        try:
            datev_numbers = self._datev_numbers_for(contact_type, entity_type,
                                                    datev_consultant_no, datev_client_no)
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
                    'INSERT INTO CompanyDetails (ContactID, CompanyName, LegalForm, TaxID, BuyerRouteID, '
                    'DatevConsultantNo, DatevClientNo) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (contact_id, company_name or None, legal_form or None,
                     tax_id or None, buyer_route_id or None, *datev_numbers)
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

            # ContactTypeLinks: alle gewählten Nicht-'own'-Typen. 'own' ist Systemtyp
            # (Spalte ContactType) und kein Link – ein Kontakt darf aber gleichzeitig
            # 'own' UND z.B. 'customer' sein (z.B. eigene Firma als Kunde einer anderen).
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
            return contact_id          # Aufrufer braucht ihn für Verknüpfungen
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
                       type_keys=None, role_keys=None,
                       datev_consultant_no=None, datev_client_no=None):
        """Update an existing contact and all sub-table records.

        ``datev_consultant_no``/``datev_client_no`` (nur die eigene Firma trägt sie): ``None`` lässt die
        gespeicherte Nummer, wie sie ist (die Firmendaten werden gelöscht und neu geschrieben – sie dürfen dabei
        nicht verloren gehen), ein leerer Text leert sie."""
        conn = self._get_connection()
        cursor = conn.cursor()
        if not customer_number or not str(customer_number).strip():
            customer_number = None
        try:
            stored = cursor.execute('SELECT DatevConsultantNo, DatevClientNo FROM CompanyDetails WHERE ContactID=?',
                                    (contact_id,)).fetchone() or (None, None)
            datev_numbers = self._datev_numbers_for(contact_type, entity_type,
                                                    datev_consultant_no, datev_client_no, stored)
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
                    'INSERT INTO CompanyDetails (ContactID, CompanyName, LegalForm, TaxID, BuyerRouteID, '
                    'DatevConsultantNo, DatevClientNo) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (contact_id, company_name or None, legal_form or None,
                     tax_id or None, buyer_route_id or None, *datev_numbers)
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
            # ContactTypeLinks: immer neu setzen. 'own' selbst ist kein Link (Systemtyp
            # in Spalte ContactType), ein Kontakt darf aber gleichzeitig 'own' UND
            # weitere Typen (Kunde, Lieferant …) haben.
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
