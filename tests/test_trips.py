# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""TODO #3: Fahrtenbuch (Trips) – DB-Layer, km-Vorbelegung und Seitenrendering."""
import sqlite3

import pytest

from server.pages_trips import PageTrips
from server import handlers


def _driver(db, name="Fahrer"):
    """Hilfs-Kontakt (own) als Fahrer anlegen und ID zurückgeben."""
    con = sqlite3.connect(db.db_name)
    cid = con.execute(
        "INSERT INTO Contacts (ContactType, EntityType, DisplayName) VALUES ('own','person',?)",
        (name,),
    ).lastrowid
    con.commit()
    con.close()
    return cid


def _post(**kw):
    """parse_qs-artiges Dict (Werte als Listen) aus kwargs bauen."""
    return {k: [str(v)] for k, v in kw.items()}


def test_trip_insert_and_fetch_roundtrip(tmp_db):
    drv = _driver(tmp_db)
    tid = tmp_db.insert_trip(drv, "2026-06-01", start_point="Berlin",
                             destination="Hamburg", vehicle="B-XY 1", distance_km=290)
    assert tid
    rows = tmp_db.fetch_trips(drv)
    assert len(rows) == 1
    r = rows[0]
    assert r[1] == drv and r[2] == "2026-06-01"
    assert r[6] == "Berlin" and r[7] == "Hamburg"
    assert r[8] == "B-XY 1" and r[10] == 290


def test_fetch_trips_period_and_driver_filter(tmp_db):
    a = _driver(tmp_db, "A")
    b = _driver(tmp_db, "B")
    tmp_db.insert_trip(a, "2026-05-15", destination="X", distance_km=10)
    tmp_db.insert_trip(a, "2026-06-10", destination="Y", distance_km=20)
    tmp_db.insert_trip(b, "2026-06-11", destination="Z", distance_km=30)
    # Fahrer-Filter
    assert len(tmp_db.fetch_trips(a)) == 2
    assert len(tmp_db.fetch_trips(b)) == 1
    # Zeitraum-Filter
    juni = tmp_db.fetch_trips(a, "2026-06-01", "2026-06-30")
    assert len(juni) == 1 and juni[0][7] == "Y"


def test_update_and_delete_trip(tmp_db):
    drv = _driver(tmp_db)
    tid = tmp_db.insert_trip(drv, "2026-06-01", destination="Kiel", distance_km=100)
    tmp_db.update_trip(tid, "2026-06-02", destination="Kiel", distance_km=111)
    r = tmp_db.get_trip_by_id(tid)
    assert r[2] == "2026-06-02" and r[10] == 111
    tmp_db.delete_trip(tid)
    assert tmp_db.get_trip_by_id(tid) is None


def test_known_routes_prefill_uses_latest(tmp_db):
    drv = _driver(tmp_db)
    tmp_db.insert_trip(drv, "2026-06-01", start_point="Berlin", destination="Hamburg",
                       distance_km=290)
    tmp_db.insert_trip(drv, "2026-06-05", start_point="Berlin", destination="Hamburg",
                       distance_km=296)
    routes = tmp_db.get_known_routes(drv)
    # Schlüssel case-insensitiv; jüngste Fahrt gewinnt
    assert routes["berlin|hamburg"] == 296
    # Leerer Startpunkt = eigene Adresse
    tmp_db.insert_trip(drv, "2026-06-06", start_point="", destination="Lübeck",
                       distance_km=70)
    assert tmp_db.get_known_routes(drv)["|lübeck"] == 70


def test_get_vehicles_distinct(tmp_db):
    drv = _driver(tmp_db)
    tmp_db.insert_trip(drv, "2026-06-01", destination="X", vehicle="B-AA 1", distance_km=5)
    tmp_db.insert_trip(drv, "2026-06-02", destination="Y", vehicle="B-AA 1", distance_km=6)
    tmp_db.insert_trip(drv, "2026-06-03", destination="Z", vehicle="B-BB 2", distance_km=7)
    assert tmp_db.get_vehicles(drv) == ["B-AA 1", "B-BB 2"]


def test_page_renders_form_submenu_and_entries(tmp_db):
    drv = _driver(tmp_db)
    tmp_db.insert_trip(drv, "2026-06-03", destination="Hamburg", vehicle="B-XY 1",
                       reason="Termin", distance_km=290)
    html = PageTrips(tmp_db, person_id=drv, date_from="2026-06-01", date_to="2026-06-30")
    for needle in ("Neue Fahrt", "trStartDate", "trDestination", "trVehicleList",
                   "Arbeitszeiten", "Fahrten", "Hamburg", "knownRoutes"):
        assert needle in html, needle


def test_page_edit_preloads_entry(tmp_db):
    drv = _driver(tmp_db)
    tid = tmp_db.insert_trip(drv, "2026-06-03", destination="Hamburg", distance_km=290)
    html = PageTrips(tmp_db, person_id=drv, date_from="2026-06-01",
                     date_to="2026-06-30", edit_id=tid)
    assert "trLoadEntry(" in html
    assert '"destination": "Hamburg"' in html


def test_odometer_difference_computes_distance(tmp_db):
    """Tacho Start+Ende gesetzt ⇒ DistanceKm = Differenz (serverseitig erzwungen)."""
    drv = _driver(tmp_db)
    sc, _ = handlers.handle_add_trip(tmp_db, _post(
        person_id=drv, **{"from": "2026-06-01", "to": "2026-06-30"},
        start_date="2026-06-04", destination="Hamburg",
        start_km=10000, end_km=10290, distance_km=999))  # distance_km wird ignoriert
    assert sc == 303
    r = tmp_db.fetch_trips(drv)[0]
    assert r[10] == 290 and r[11] == 10000 and r[12] == 10290


def test_distance_kept_when_odometer_incomplete(tmp_db):
    """Nur ein Tachostand ⇒ manuell/vorbelegte km bleiben erhalten."""
    drv = _driver(tmp_db)
    handlers.handle_add_trip(tmp_db, _post(
        person_id=drv, **{"from": "2026-06-01", "to": "2026-06-30"},
        start_date="2026-06-04", destination="Kiel",
        start_km=10000, distance_km=120))
    r = tmp_db.fetch_trips(drv)[0]
    assert r[10] == 120 and r[11] == 10000 and r[12] is None


def test_document_link_stored(tmp_db):
    drv = _driver(tmp_db)
    tmp_db.insert_receipt("R-1", "2026-06-01", "beleg.pdf", "data/x.pdf", "Tankbeleg")
    doc_id = tmp_db.fetch_receipts()[0][0]
    handlers.handle_add_trip(tmp_db, _post(
        person_id=drv, **{"from": "2026-06-01", "to": "2026-06-30"},
        start_date="2026-06-04", destination="Hamburg",
        distance_km=290, document_id=doc_id))
    assert tmp_db.fetch_trips(drv)[0][13] == doc_id
    # Beleg-Dropdown taucht in der Seite auf
    html = PageTrips(tmp_db, person_id=drv, date_from="2026-06-01", date_to="2026-06-30")
    assert "trDocument" in html and "R-1" in html


def test_save_as_new_creates_copy(tmp_db):
    """„Als neu speichern" postet auf /trips/add mit gesetzter id ⇒ neuer Eintrag."""
    drv = _driver(tmp_db)
    orig = tmp_db.insert_trip(drv, "2026-06-04", destination="Hamburg", distance_km=290)
    # Formular trägt beim Kopieren die id des Originals, Ziel /trips/add
    handlers.handle_add_trip(tmp_db, _post(
        id=orig, person_id=drv, **{"from": "2026-06-01", "to": "2026-06-30"},
        start_date="2026-06-04", destination="Hamburg", distance_km=290))
    rows = tmp_db.fetch_trips(drv)
    assert len(rows) == 2                       # Original bleibt, Kopie kommt hinzu
    assert {r[0] for r in rows} != {orig}       # neue ID vergeben
    # Button im Formular vorhanden
    html = PageTrips(tmp_db, person_id=drv, date_from="2026-06-01",
                     date_to="2026-06-30", edit_id=orig)
    assert 'formaction="/trips/add"' in html and "Als neu speichern" in html
