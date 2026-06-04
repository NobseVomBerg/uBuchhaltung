"""
Unit-Tests für Kontakt-Kürzel (Abbreviation).

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
