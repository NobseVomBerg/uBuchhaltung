"""
Unit-Tests für die Arbeitszeiterfassung (WorkTimes).

Testet:
- DB-CRUD: insert/fetch/update/delete, Zeitraum-Filter, neuester Eintrag, CASCADE
- compute_hours: Normalfall, Pause, leere/ungültige Zeiten
- Handler: add/update → 303 + korrekte Location; LocationCity-Auflösung (own/customer/other)
"""
import pytest

from db import Database
from server.pages_worktime import compute_hours


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _add_contact(db: Database, **kw):
    """Kontakt anlegen und neue ID zurückgeben."""
    db.insert_contact(**kw)
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute("SELECT MAX(ID) FROM Contacts")
    cid = cur.fetchone()[0]
    conn.close()
    return cid


def _person(db, first='Erika', last='Muster'):
    return _add_contact(db, contact_type='employee', entity_type='person',
                        first_name=first, last_name=last)


def _customer(db, name='Kunde GmbH', city='Kundenstadt'):
    return _add_contact(db, contact_type='customer', entity_type='company',
                        company_name=name, city=city)


def _own(db, city='Eigenstadt'):
    return _add_contact(db, contact_type='own', entity_type='company',
                        company_name='Eigene Firma', city=city)


# ── DB-CRUD ───────────────────────────────────────────────────────────────────

class TestWorkTimeCRUD:
    def test_insert_and_fetch(self, tmp_db):
        pid = _person(tmp_db)
        tmp_db.insert_worktime(pid, '2026-06-15', start_time='08:00',
                               end_time='16:00', pause_minutes=30, note='Tag 1')
        rows = tmp_db.fetch_worktimes(pid, '2026-06-01', '2026-06-30')
        assert len(rows) == 1
        assert rows[0][2] == '2026-06-15'
        assert rows[0][5] == '08:00'
        assert rows[0][10] == 'Tag 1'

    def test_fetch_filters_by_period(self, tmp_db):
        pid = _person(tmp_db)
        tmp_db.insert_worktime(pid, '2026-05-31')
        tmp_db.insert_worktime(pid, '2026-06-15')
        tmp_db.insert_worktime(pid, '2026-07-01')
        rows = tmp_db.fetch_worktimes(pid, '2026-06-01', '2026-06-30')
        assert len(rows) == 1
        assert rows[0][2] == '2026-06-15'

    def test_fetch_only_for_person(self, tmp_db):
        p1 = _person(tmp_db, 'A', 'Eins')
        p2 = _person(tmp_db, 'B', 'Zwei')
        tmp_db.insert_worktime(p1, '2026-06-10')
        tmp_db.insert_worktime(p2, '2026-06-11')
        rows = tmp_db.fetch_worktimes(p1, '2026-06-01', '2026-06-30')
        assert len(rows) == 1
        assert rows[0][1] == p1

    def test_update(self, tmp_db):
        pid = _person(tmp_db)
        wid = tmp_db.insert_worktime(pid, '2026-06-15', start_time='08:00', end_time='16:00')
        tmp_db.update_worktime(wid, '2026-06-16', start_time='09:00', end_time='17:00',
                               pause_minutes=45, note='geändert')
        e = tmp_db.get_worktime_by_id(wid)
        assert e[2] == '2026-06-16'
        assert e[5] == '09:00'
        assert e[7] == 45
        assert e[10] == 'geändert'

    def test_delete(self, tmp_db):
        pid = _person(tmp_db)
        wid = tmp_db.insert_worktime(pid, '2026-06-15')
        tmp_db.delete_worktime(wid)
        assert tmp_db.get_worktime_by_id(wid) is None

    def test_get_last_returns_newest(self, tmp_db):
        pid = _person(tmp_db)
        tmp_db.insert_worktime(pid, '2026-06-10', note='alt')
        tmp_db.insert_worktime(pid, '2026-06-20', note='neu')
        tmp_db.insert_worktime(pid, '2026-06-15', note='mitte')
        last = tmp_db.get_last_worktime_for_person(pid)
        assert last[2] == '2026-06-20'
        assert last[10] == 'neu'

    def test_cascade_on_person_delete(self, tmp_db):
        pid = _person(tmp_db)
        tmp_db.insert_worktime(pid, '2026-06-15')
        conn = tmp_db._get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM Contacts WHERE ID=?", (pid,))
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM WorkTimes WHERE PersonID=?", (pid,))
        remaining = cur.fetchone()[0]
        conn.close()
        assert remaining == 0


# ── compute_hours ─────────────────────────────────────────────────────────────

class TestComputeHours:
    def test_normal(self):
        assert compute_hours('08:00', '16:00', 0) == 8.0

    def test_with_pause(self):
        assert compute_hours('08:00', '17:00', 60) == 8.0

    def test_half_hours(self):
        assert compute_hours('09:15', '12:45', 0) == 3.5

    def test_empty_times(self):
        assert compute_hours('', '', 0) == 0.0
        assert compute_hours('08:00', '', 0) == 0.0

    def test_negative_clamped(self):
        assert compute_hours('17:00', '08:00', 0) == 0.0

    def test_invalid(self):
        assert compute_hours('abc', '16:00', 0) == 0.0


# ── Handler ───────────────────────────────────────────────────────────────────

def _post(person_id, **kw):
    base = {
        'person_id': [str(person_id)], 'from': ['2026-06-01'], 'to': ['2026-06-30'],
        'id': [''], 'date': ['2026-06-15'], 'kind': ['work'], 'customer_id': [''],
        'start_time': ['08:00'], 'end_time': ['16:00'], 'pause_minutes': ['30'],
        'location_mode': ['customer'], 'location_city': [''], 'note': [''],
    }
    for k, v in kw.items():
        base[k] = [str(v)]
    return base


class TestWorkTimeHandlers:
    def test_add_returns_redirect_with_context(self, tmp_db):
        from server.handlers import handle_add_worktime
        pid = _person(tmp_db)
        status, location = handle_add_worktime(tmp_db, _post(pid, location_mode='other',
                                                             location_city='Irgendwo'))
        assert status == 303
        assert f'person={pid}' in location
        assert 'from=2026-06-01' in location
        assert 'to=2026-06-30' in location
        rows = tmp_db.fetch_worktimes(pid, '2026-06-01', '2026-06-30')
        assert len(rows) == 1

    def test_update_changes_entry(self, tmp_db):
        from server.handlers import handle_update_worktime
        pid = _person(tmp_db)
        wid = tmp_db.insert_worktime(pid, '2026-06-15', start_time='08:00', end_time='16:00')
        post = _post(pid, id=wid, start_time='10:00', end_time='18:00',
                     location_mode='other', location_city='X')
        status, location = handle_update_worktime(tmp_db, post)
        assert status == 303
        e = tmp_db.get_worktime_by_id(wid)
        assert e[5] == '10:00'
        assert e[6] == '18:00'

    def test_location_other_uses_free_text(self, tmp_db):
        from server.handlers import handle_add_worktime
        pid = _person(tmp_db)
        handle_add_worktime(tmp_db, _post(pid, location_mode='other',
                                          location_city='Sonderort'))
        e = tmp_db.fetch_worktimes(pid, '2026-06-01', '2026-06-30')[0]
        assert e[9] == 'Sonderort'

    def test_location_customer_uses_customer_city(self, tmp_db):
        from server.handlers import handle_add_worktime
        pid = _person(tmp_db)
        cid = _customer(tmp_db, city='Kundenstadt')
        handle_add_worktime(tmp_db, _post(pid, customer_id=cid,
                                          location_mode='customer', location_city=''))
        e = tmp_db.fetch_worktimes(pid, '2026-06-01', '2026-06-30')[0]
        assert e[9] == 'Kundenstadt'

    def test_location_own_uses_own_city(self, tmp_db):
        from server.handlers import handle_add_worktime
        _own(tmp_db, city='Eigenstadt')
        pid = _person(tmp_db)
        handle_add_worktime(tmp_db, _post(pid, location_mode='own', location_city=''))
        e = tmp_db.fetch_worktimes(pid, '2026-06-01', '2026-06-30')[0]
        assert e[9] == 'Eigenstadt'


class TestWorkTimeOverlap:
    """Überschneidungssperre: Arbeit blockiert, Urlaub/Feiertag nicht."""

    def _count(self, db, pid):
        return len(db.fetch_worktimes(pid, '2026-06-01', '2026-06-30'))

    def test_overlapping_work_blocked(self, tmp_db):
        from server.handlers import handle_add_worktime
        pid = _person(tmp_db)
        handle_add_worktime(tmp_db, _post(pid, date='2026-06-15',
                                          start_time='08:00', end_time='16:00',
                                          location_mode='other', location_city='X'))
        status, location = handle_add_worktime(tmp_db, _post(pid, date='2026-06-15',
                                          start_time='12:00', end_time='18:00',
                                          location_mode='other', location_city='X'))
        assert status == 303
        assert 'error=' in location
        assert self._count(tmp_db, pid) == 1          # zweiter Eintrag nicht angelegt

    def test_exact_duplicate_blocked(self, tmp_db):
        from server.handlers import handle_add_worktime
        pid = _person(tmp_db)
        p = _post(pid, date='2026-06-15', start_time='08:00', end_time='16:00',
                  location_mode='other', location_city='X')
        handle_add_worktime(tmp_db, p)
        status, location = handle_add_worktime(tmp_db, p)
        assert status == 303 and 'error=' in location
        assert self._count(tmp_db, pid) == 1

    def test_adjacent_nonoverlap_allowed(self, tmp_db):
        from server.handlers import handle_add_worktime
        pid = _person(tmp_db)
        handle_add_worktime(tmp_db, _post(pid, date='2026-06-15',
                                          start_time='08:00', end_time='12:00',
                                          location_mode='other', location_city='X'))
        handle_add_worktime(tmp_db, _post(pid, date='2026-06-15',
                                          start_time='12:00', end_time='17:00',
                                          location_mode='other', location_city='X'))
        assert self._count(tmp_db, pid) == 2          # angrenzend, keine Überschneidung

    def test_vacation_not_locked(self, tmp_db):
        from server.handlers import handle_add_worktime
        pid = _person(tmp_db)
        handle_add_worktime(tmp_db, _post(pid, date='2026-06-15', kind='work',
                                          start_time='08:00', end_time='16:00',
                                          location_mode='other', location_city='X'))
        # Urlaub am selben Tag trotz Arbeitseintrag erlaubt
        handle_add_worktime(tmp_db, _post(pid, date='2026-06-15', kind='vacation',
                                          start_time='', end_time='',
                                          location_mode='own', location_city=''))
        assert self._count(tmp_db, pid) == 2

    def test_update_into_overlap_blocked(self, tmp_db):
        from server.handlers import handle_update_worktime
        pid = _person(tmp_db)
        tmp_db.insert_worktime(pid, '2026-06-15', start_time='08:00', end_time='12:00')
        wid2 = tmp_db.insert_worktime(pid, '2026-06-15', start_time='13:00', end_time='17:00')
        # wid2 so ändern, dass es mit dem ersten überlappt
        post = _post(pid, id=wid2, date='2026-06-15', start_time='11:00', end_time='15:00',
                     location_mode='other', location_city='X')
        status, location = handle_update_worktime(tmp_db, post)
        assert status == 303 and 'error=' in location
        e = tmp_db.get_worktime_by_id(wid2)
        assert e[5] == '13:00'                        # unverändert (Update abgelehnt)
