# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Unit-Tests für Kontaktverwaltung (Abbreviation, ContactTypeLinks, PersonDetails, PersonRoles).

Testet:
- insert_contact speichert Kürzel korrekt
- check_abbreviation_unique: frei, vergeben, Vorschlag, exclude_id
- Duplikat-Insert / Duplikat-Update lösen IntegrityError aus
- handle_add_contact / handle_update_contact geben bei Duplikat
  303-Redirect mit error-Parameter zurück, statt die Exception weiterzugeben
"""
import sqlite3
import pytest

from db import Database


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktion
# ─────────────────────────────────────────────────────────────────────────────

def _add_company(db: Database, abbreviation: str, name: str = None, cust_nr: str = None):
    """Fügt einen minimalen Firmenkontakt mit gegebenem Kürzel ein."""
    db.insert_contact(
        contact_type='customer',
        entity_type='company',
        company_name=name or f'Firma {abbreviation}',
        abbreviation=abbreviation,
        customer_number=cust_nr,
    )


def _add_person(db: Database, abbreviation: str, first: str = 'Hans', last: str = 'Muster'):
    db.insert_contact(
        contact_type='customer',
        entity_type='person',
        first_name=first,
        last_name=last,
        abbreviation=abbreviation,
    )


def _get_abbr(db: Database, contact_id: int):
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute('SELECT Abbreviation FROM Contacts WHERE ID=?', (contact_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _count_abbr(db: Database, abbr: str):
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM Contacts WHERE Abbreviation=?', (abbr,))
    count = cur.fetchone()[0]
    conn.close()
    return count


# ─────────────────────────────────────────────────────────────────────────────
# insert_contact: Kürzel wird gespeichert
# ─────────────────────────────────────────────────────────────────────────────

class TestInsertContactAbbreviation:
    def test_abbreviation_stored_for_company(self, tmp_db):
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT MAX(ID) FROM Contacts")
        before = cur.fetchone()[0] or 0
        conn.close()

        _add_company(tmp_db, 'ABC')

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT Abbreviation FROM Contacts WHERE ID > ?", (before,))
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 'ABC'

    def test_abbreviation_stored_for_person(self, tmp_db):
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT MAX(ID) FROM Contacts")
        before = cur.fetchone()[0] or 0
        conn.close()

        _add_person(tmp_db, 'HM')

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT Abbreviation FROM Contacts WHERE ID > ?", (before,))
        row = cur.fetchone()
        conn.close()
        assert row[0] == 'HM'

    def test_empty_abbreviation_stored_as_null(self, tmp_db):
        """Leeres Kürzel soll als NULL gespeichert werden (kein UNIQUE-Konflikt)."""
        tmp_db.insert_contact(contact_type='other', entity_type='company',
                              company_name='Keine Kürzel GmbH', abbreviation='')
        tmp_db.insert_contact(contact_type='other', entity_type='company',
                              company_name='Auch keine GmbH', abbreviation='')
        # Beide NULL – darf keinen Unique-Fehler geben
        assert _count_abbr(tmp_db, '') == 0   # '' ist nicht gespeichert
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM Contacts WHERE Abbreviation IS NULL AND "
                    "ContactType='other'")
        count = cur.fetchone()[0]
        conn.close()
        assert count == 2


# ─────────────────────────────────────────────────────────────────────────────
# check_abbreviation_unique
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckAbbreviationUnique:
    def test_free_abbreviation_returns_true(self, tmp_db):
        is_unique, suggestion = tmp_db.check_abbreviation_unique('XYZ')
        assert is_unique is True
        assert suggestion == 'XYZ'

    def test_taken_abbreviation_returns_false(self, tmp_db):
        _add_company(tmp_db, 'MUG')
        is_unique, suggestion = tmp_db.check_abbreviation_unique('MUG')
        assert is_unique is False

    def test_suggestion_increments_to_2(self, tmp_db):
        _add_company(tmp_db, 'MUG')
        _, suggestion = tmp_db.check_abbreviation_unique('MUG')
        assert suggestion == 'MUG2'

    def test_suggestion_increments_past_existing_numbers(self, tmp_db):
        _add_company(tmp_db, 'MUG',  cust_nr='K1')
        _add_company(tmp_db, 'MUG2', cust_nr='K2')
        _add_company(tmp_db, 'MUG3', cust_nr='K3')
        _, suggestion = tmp_db.check_abbreviation_unique('MUG')
        assert suggestion == 'MUG4'

    def test_exclude_id_treats_own_abbreviation_as_unique(self, tmp_db):
        """Beim Bearbeiten eines Kontakts soll sein eigenes Kürzel als einzigartig gelten."""
        _add_company(tmp_db, 'MUG')
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='MUG'")
        contact_id = cur.fetchone()[0]
        conn.close()

        is_unique, suggestion = tmp_db.check_abbreviation_unique('MUG', exclude_id=contact_id)
        assert is_unique is True
        assert suggestion == 'MUG'

    def test_exclude_id_still_detects_other_conflict(self, tmp_db):
        """exclude_id schützt nur vor dem eigenen Eintrag, nicht vor fremden.

        Szenario: Kontakt K2 besitzt MUG2 und möchte MUG (von K1 belegt) übernehmen.
        Die Suggestion ist MUG2, weil K2 sein eigenes aktuelles Kürzel zurückbekommt
        (es ist durch den exclude_id aus der Sperrprüfung herausgenommen).
        """
        _add_company(tmp_db, 'MUG', cust_nr='K1')
        _add_company(tmp_db, 'MUG2', cust_nr='K2')

        # ID des zweiten Kontakts (MUG2) als exclude_id
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='MUG2'")
        other_id = cur.fetchone()[0]
        conn.close()

        # 'MUG' ist immer noch durch Kontakt 1 belegt
        is_unique, suggestion = tmp_db.check_abbreviation_unique('MUG', exclude_id=other_id)
        assert is_unique is False
        # MUG2 gilt als frei, weil es dem ausgeschlossenen Kontakt selbst gehört
        assert suggestion == 'MUG2'


# ─────────────────────────────────────────────────────────────────────────────
# DB-Ebene: IntegrityError bei Duplikaten
# ─────────────────────────────────────────────────────────────────────────────

class TestAbbreviationUniqueConstraint:
    def test_insert_duplicate_abbreviation_raises(self, tmp_db):
        _add_company(tmp_db, 'DUP', cust_nr='K1')
        with pytest.raises(sqlite3.IntegrityError):
            _add_company(tmp_db, 'DUP', cust_nr='K2')

    def test_update_to_duplicate_abbreviation_raises(self, tmp_db):
        _add_company(tmp_db, 'AAA', cust_nr='K1')
        _add_company(tmp_db, 'BBB', cust_nr='K2')

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='BBB'")
        bbb_id = cur.fetchone()[0]
        conn.close()

        with pytest.raises((sqlite3.IntegrityError, Exception)):
            tmp_db.update_contact(contact_id=bbb_id, entity_type='company',
                                  company_name='Firma BBB', abbreviation='AAA')


# ─────────────────────────────────────────────────────────────────────────────
# Handler-Ebene: Duplikat → 303 + error-Parameter, keine Exception
# ─────────────────────────────────────────────────────────────────────────────

class TestHandlerAbbreviationDuplicate:
    """Handler sollen bei doppeltem Kürzel einen Redirect mit ?error=... liefern,
    statt den IntegrityError ungefangen durchzureichen."""

    def _post(self, **kwargs):
        """Erstellt ein minimales post_data-Dict im Format {key: [value]}."""
        base = {
            'contact_type': ['customer'],
            'entity_type': ['company'],
            'company_name': ['Test GmbH'],
            'display_name': [''],
            'customer_number': [''],
            'abbreviation': [''],
            'email': [''], 'phone': [''], 'notes': [''], 'logo': [''],
            'address_line1': [''], 'street': [''], 'postal_code': [''],
            'city': [''], 'country': ['DE'],
            'legal_form': [''], 'tax_id': [''], 'buyer_route_id': [''],
            'salutation': [''], 'title': [''], 'first_name': [''],
            'last_name': [''], 'date_of_birth': [''],
            'company_contact_id': [''], 'company_name_free': [''],
        }
        for k, v in kwargs.items():
            base[k] = [v]
        return base

    def test_add_contact_duplicate_abbreviation_returns_redirect(self, tmp_db):
        from server.handlers import handle_add_contact
        _add_company(tmp_db, 'DUP')

        status, location = handle_add_contact(tmp_db, self._post(abbreviation='DUP'))

        assert status == 303
        assert 'error' in location
        assert 'DUP' in location

    def test_add_contact_unique_abbreviation_succeeds(self, tmp_db):
        from server.handlers import handle_add_contact

        status, location = handle_add_contact(tmp_db, self._post(abbreviation='NEW'))

        assert status == 303
        assert 'error' not in location

    def test_update_contact_duplicate_abbreviation_returns_redirect(self, tmp_db):
        from server.handlers import handle_update_contact
        _add_company(tmp_db, 'AAA', cust_nr='K1')
        _add_company(tmp_db, 'BBB', cust_nr='K2')

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='BBB'")
        bbb_id = cur.fetchone()[0]
        conn.close()

        post = self._post(abbreviation='AAA')
        post['contact_id'] = [str(bbb_id)]
        status, location = handle_update_contact(tmp_db, post)

        assert status == 303
        assert 'error' in location
        assert 'AAA' in location
        # Wichtig: Edit-URL enthält die contact_id, damit das Formular wieder geöffnet wird
        assert f'id={bbb_id}' in location

    def test_update_contact_own_abbreviation_is_allowed(self, tmp_db):
        """Speichern mit dem eigenen Kürzel darf nicht als Duplikat gewertet werden."""
        from server.handlers import handle_update_contact
        _add_company(tmp_db, 'OWN', cust_nr='K1')

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='OWN'")
        own_id = cur.fetchone()[0]
        conn.close()

        post = self._post(abbreviation='OWN', company_name='Aktualisiert GmbH')
        post['contact_id'] = [str(own_id)]
        status, location = handle_update_contact(tmp_db, post)

        assert status == 303
        assert 'error' not in location

    def test_update_contact_empty_abbreviation_always_allowed(self, tmp_db):
        """Ohne Kürzel darf nie ein Duplikat-Fehler entstehen."""
        from server.handlers import handle_update_contact
        _add_company(tmp_db, 'ZZZ', cust_nr='K1')

        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='ZZZ'")
        zzz_id = cur.fetchone()[0]
        conn.close()

        post = self._post(abbreviation='')
        post['contact_id'] = [str(zzz_id)]
        status, location = handle_update_contact(tmp_db, post)

        assert status == 303
        assert 'error' not in location


# ─────────────────────────────────────────────────────────────────────────────
# ContactTypeLinks – Mehrfach-Kontakttypen
# ─────────────────────────────────────────────────────────────────────────────

def _get_type_keys(db: Database, contact_id: int) -> set:
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute('SELECT TypeKey FROM ContactTypeLinks WHERE ContactID=?', (contact_id,))
    result = {row[0] for row in cur.fetchall()}
    conn.close()
    return result


class TestContactTypeLinks:
    def test_own_contact_has_no_type_links(self, tmp_db):
        """'own'-Kontakte werden NICHT in ContactTypeLinks eingetragen."""
        tmp_db.insert_contact(contact_type='own', entity_type='company',
                              company_name='Eigene GmbH')
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM ContactTypeLinks')
        count = cur.fetchone()[0]
        conn.close()
        assert count == 0

    def test_customer_contact_creates_type_link(self, tmp_db):
        _add_company(tmp_db, 'CUS', cust_nr='K1')
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='CUS'")
        cid = cur.fetchone()[0]
        conn.close()
        assert _get_type_keys(tmp_db, cid) == {'customer'}

    def test_multi_type_contact_creates_multiple_links(self, tmp_db):
        tmp_db.insert_contact(contact_type='customer', entity_type='company',
                              company_name='Dual GmbH', abbreviation='DUA',
                              type_keys=['supplier'])
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='DUA'")
        cid = cur.fetchone()[0]
        conn.close()
        assert _get_type_keys(tmp_db, cid) == {'customer', 'supplier'}

    def test_fetch_contacts_by_type_via_type_links(self, tmp_db):
        """fetch_contacts(contact_type='supplier') findet Kontakte via ContactTypeLinks."""
        tmp_db.insert_contact(contact_type='customer', entity_type='company',
                              company_name='Nur Kunde', abbreviation='NK1')
        tmp_db.insert_contact(contact_type='supplier', entity_type='company',
                              company_name='Nur Lieferant', abbreviation='NL1')
        tmp_db.insert_contact(contact_type='customer', entity_type='company',
                              company_name='Kunde+Lieferant', abbreviation='KL1',
                              type_keys=['supplier'])

        suppliers = tmp_db.fetch_contacts(contact_type='supplier')
        names = {r[3] for r in suppliers}   # display_name (index 3)
        assert 'Nur Lieferant' in names
        assert 'Kunde+Lieferant' in names
        assert 'Nur Kunde' not in names

    def test_update_contact_replaces_type_links(self, tmp_db):
        """update_contact löscht alte TypeLinks und schreibt neue."""
        _add_company(tmp_db, 'UPD', cust_nr='K1')
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='UPD'")
        cid = cur.fetchone()[0]
        conn.close()

        assert _get_type_keys(tmp_db, cid) == {'customer'}

        tmp_db.update_contact(contact_id=cid, entity_type='company',
                              company_name='Updated GmbH', abbreviation='UPD',
                              type_keys=['partner'])

        assert _get_type_keys(tmp_db, cid) == {'customer', 'partner'}

    def test_delete_contact_cascades_type_links(self, tmp_db):
        """Beim Löschen eines Kontakts werden ContactTypeLinks mit entfernt."""
        _add_company(tmp_db, 'DEL', cust_nr='K1')
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='DEL'")
        cid = cur.fetchone()[0]
        conn.close()

        tmp_db.delete_contact(cid)
        assert _get_type_keys(tmp_db, cid) == set()


# ─────────────────────────────────────────────────────────────────────────────
# PersonDetails – neue Felder JobTitle, Department, IsPrimaryContact
# ─────────────────────────────────────────────────────────────────────────────

def _get_person_details(db: Database, contact_id: int):
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute('SELECT JobTitle, Department, IsPrimaryContact FROM PersonDetails WHERE ContactID=?',
                (contact_id,))
    row = cur.fetchone()
    conn.close()
    return row


class TestPersonDetailsExtended:
    def _insert_person(self, db, abbr, job_title='', department='', is_primary=0):
        db.insert_contact(contact_type='customer', entity_type='person',
                          first_name='Max', last_name='Mustermann',
                          abbreviation=abbr,
                          job_title=job_title, department=department,
                          is_primary_contact=is_primary)
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation=?", (abbr,))
        cid = cur.fetchone()[0]
        conn.close()
        return cid

    def test_job_title_stored(self, tmp_db):
        cid = self._insert_person(tmp_db, 'JT1', job_title='CEO')
        row = _get_person_details(tmp_db, cid)
        assert row[0] == 'CEO'

    def test_department_stored(self, tmp_db):
        cid = self._insert_person(tmp_db, 'DP1', department='Buchhaltung')
        row = _get_person_details(tmp_db, cid)
        assert row[1] == 'Buchhaltung'

    def test_is_primary_contact_stored(self, tmp_db):
        cid = self._insert_person(tmp_db, 'PC1', is_primary=1)
        row = _get_person_details(tmp_db, cid)
        assert row[2] == 1

    def test_is_primary_contact_defaults_to_zero(self, tmp_db):
        cid = self._insert_person(tmp_db, 'PC2')
        row = _get_person_details(tmp_db, cid)
        assert row[2] == 0

    def test_empty_fields_stored_as_null(self, tmp_db):
        cid = self._insert_person(tmp_db, 'NUL')
        row = _get_person_details(tmp_db, cid)
        assert row[0] is None   # JobTitle
        assert row[1] is None   # Department

    def test_contacts_query_returns_job_title(self, tmp_db):
        """_CONTACTS_QUERY liefert JobTitle an Index 28."""
        cid = self._insert_person(tmp_db, 'QJT', job_title='Vertriebsleiter')
        row = tmp_db.get_contact_by_id(cid)
        assert row[28] == 'Vertriebsleiter'

    def test_update_person_details(self, tmp_db):
        cid = self._insert_person(tmp_db, 'UPP', job_title='Alt')
        tmp_db.update_contact(contact_id=cid, entity_type='person',
                              first_name='Max', last_name='Mustermann',
                              abbreviation='UPP', job_title='Neu',
                              department='IT', is_primary_contact=1)
        row = _get_person_details(tmp_db, cid)
        assert row[0] == 'Neu'
        assert row[1] == 'IT'
        assert row[2] == 1


# ─────────────────────────────────────────────────────────────────────────────
# PersonRoles – Fachliche Rollen
# ─────────────────────────────────────────────────────────────────────────────

def _get_role_keys(db: Database, contact_id: int) -> set:
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute('SELECT RoleKey FROM PersonRoles WHERE ContactID=?', (contact_id,))
    result = {row[0] for row in cur.fetchall()}
    conn.close()
    return result


class TestPersonRoles:
    def _insert_person_with_roles(self, db, abbr, role_keys):
        db.insert_contact(contact_type='customer', entity_type='person',
                          first_name='Anna', last_name='Rolle',
                          abbreviation=abbr, role_keys=role_keys)
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation=?", (abbr,))
        cid = cur.fetchone()[0]
        conn.close()
        return cid

    def test_single_role_stored(self, tmp_db):
        cid = self._insert_person_with_roles(tmp_db, 'SR1', ['invoice_recipient'])
        assert _get_role_keys(tmp_db, cid) == {'invoice_recipient'}

    def test_multiple_roles_stored(self, tmp_db):
        cid = self._insert_person_with_roles(tmp_db, 'MR1',
                                             ['invoice_recipient', 'orderer', 'accounting'])
        assert _get_role_keys(tmp_db, cid) == {'invoice_recipient', 'orderer', 'accounting'}

    def test_no_roles_creates_no_rows(self, tmp_db):
        cid = self._insert_person_with_roles(tmp_db, 'NR1', [])
        assert _get_role_keys(tmp_db, cid) == set()

    def test_company_contact_roles_ignored(self, tmp_db):
        """Rollen für Firmenkontakte werden nicht gespeichert."""
        tmp_db.insert_contact(contact_type='customer', entity_type='company',
                              company_name='Firma', abbreviation='CFR',
                              role_keys=['invoice_recipient'])
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT ID FROM Contacts WHERE Abbreviation='CFR'")
        cid = cur.fetchone()[0]
        conn.close()
        assert _get_role_keys(tmp_db, cid) == set()

    def test_update_replaces_roles(self, tmp_db):
        cid = self._insert_person_with_roles(tmp_db, 'UR1', ['orderer'])
        assert _get_role_keys(tmp_db, cid) == {'orderer'}

        tmp_db.update_contact(contact_id=cid, entity_type='person',
                              first_name='Anna', last_name='Rolle',
                              abbreviation='UR1',
                              role_keys=['accounting', 'contract'])
        assert _get_role_keys(tmp_db, cid) == {'accounting', 'contract'}

    def test_delete_cascades_roles(self, tmp_db):
        cid = self._insert_person_with_roles(tmp_db, 'DR1', ['technical'])
        tmp_db.delete_contact(cid)
        assert _get_role_keys(tmp_db, cid) == set()

    def test_contacts_query_returns_role_keys(self, tmp_db):
        """_CONTACTS_QUERY liefert role_keys (kommasepariert) an Index 31."""
        cid = self._insert_person_with_roles(tmp_db, 'QRK',
                                             ['accounting', 'invoice_recipient'])
        row = tmp_db.get_contact_by_id(cid)
        role_set = set(row[31].split(',')) if row[31] else set()
        assert role_set == {'accounting', 'invoice_recipient'}


# ─────────────────────────────────────────────────────────────────────────────
# Seed-Daten – test_contacts.json korrekt geladen
# ─────────────────────────────────────────────────────────────────────────────

class TestSeedDataWithNewFields:
    def test_seed_loads_type_links(self, tmp_db):
        """Kontakte mit type_keys im JSON erzeugen ContactTypeLinks-Einträge."""
        tmp_db.load_test_seed_data()
        # Bäckerei Müller GmbH hat type_keys: ["customer", "supplier"]
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT ctl.TypeKey FROM ContactTypeLinks ctl
            JOIN Contacts c ON c.ID = ctl.ContactID
            WHERE c.DisplayName = 'Bäckerei Müller GmbH'
            ORDER BY ctl.TypeKey
        """)
        types = {r[0] for r in cur.fetchall()}
        conn.close()
        assert 'customer' in types
        assert 'supplier' in types

    def test_seed_loads_job_title(self, tmp_db):
        tmp_db.load_test_seed_data()
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT pd.JobTitle FROM PersonDetails pd
            JOIN Contacts c ON c.ID = pd.ContactID
            WHERE c.CustomerNumber = 'K018'
        """)
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 'Leiterin Einkauf'

    def test_seed_loads_is_primary_contact(self, tmp_db):
        tmp_db.load_test_seed_data()
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT pd.IsPrimaryContact FROM PersonDetails pd
            JOIN Contacts c ON c.ID = pd.ContactID
            WHERE c.CustomerNumber = 'K018'
        """)
        row = cur.fetchone()
        conn.close()
        assert row[0] == 1

    def test_seed_loads_role_keys(self, tmp_db):
        tmp_db.load_test_seed_data()
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT pr.RoleKey FROM PersonRoles pr
            JOIN Contacts c ON c.ID = pr.ContactID
            WHERE c.CustomerNumber = 'K018'
            ORDER BY pr.RoleKey
        """)
        roles = {r[0] for r in cur.fetchall()}
        conn.close()
        assert 'invoice_recipient' in roles
        assert 'purchasing' in roles
