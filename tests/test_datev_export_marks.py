# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""DATEV-Export: Die Sätze des Stapels tragen danach das Merkmal „exportiert“ (``BookingExports``).

``handle_datev_export`` ruft ``Database.mark_datev_export``: Das Steuerdatum der Sätze im Stapel wird wie bisher auf heute gesetzt, und jeder dieser Sätze bekommt – falls noch keine –
eine Zeile in ``BookingExports``. Nur die Sätze, die im Stapel stehen: Was der Stapel weglässt (ein Eingangs-Satz eines Übertrags, den die Ausgangsseite deckt) und was außerhalb des Zeitraums
liegt, bekommt nichts; ein Export, der nichts liefert (Rückfrage, Fehler), markiert nichts. Nur der erste Export zählt: Ein zweiter ändert ``ExportedAt`` nie. Der Stapel selbst bleibt Byte für Byte, wie
er ohne das Merkmal wäre.

Alle Werte sind erfundene Testdaten.
"""
import datetime
import shutil
import sqlite3
from contextlib import closing

from db import Database
from tests.datev_helpers import confirmed_export, datev_post, is_confirm_redirect
from tests.test_datev_transfer_duplicates import PERIOD, _book_transfer, _bank, _handler, _rows_of, _stamped, world  # noqa: F401
from server import handlers


def _marks(db):
    """``{Buchungs-ID: ExportedAt}`` – alle Sätze mit dem Merkmal."""
    with closing(sqlite3.connect(db.db_name)) as con:
        return dict(con.execute('SELECT Booking_ID, ExportedAt FROM BookingExports'))


def _tax_dates(db):
    with closing(sqlite3.connect(db.db_name)) as con:
        return dict(con.execute('SELECT ID, DateTax FROM Bookings'))


def _execute(db, sql, params=()):
    with closing(sqlite3.connect(db.db_name)) as con:
        con.execute(sql, params)
        con.commit()


def _today_candidates(before):
    """Der Tag, den der Export stempelt: heute – gemessen vor und nach dem Aufruf, falls er über Mitternacht läuft."""
    return {before, datetime.date.today().isoformat()}


def _transfer_and_a_row_outside(world):
    """Ein Übertrag, auf beiden Bankbewegungen kontiert, im März (der Eingangs-Satz fällt im Stapel weg), und ein Übertrag im April (außerhalb des Zeitraums)."""
    out_bank, out_row, in_bank, in_row = _book_transfer(world)
    _april = _book_transfer(world, day_out='2026-04-10')
    return out_row, in_row, set(_april)


def test_an_export_marks_exactly_the_rows_of_its_stack(world):
    out_row, in_row, april = _transfer_and_a_row_outside(world)
    rows = _rows_of(confirmed_export(world.db, *PERIOD))
    marks = _marks(world.db)
    assert set(marks) == {out_row} and len(rows) == 1                          # der Eingangs-Satz fiel im Stapel weg und bekommt nichts
    assert not set(marks) & april and in_row not in marks
    assert set(marks) == _stamped(world.db)                                    # dieselben Sätze, die das Steuerdatum bekommen haben
    assert all(isinstance(at, str) and at for at in marks.values())


def test_the_tax_date_is_stamped_as_before_and_only_on_the_rows_of_the_stack(world):
    out_row, in_row, april = _transfer_and_a_row_outside(world)
    before = datetime.date.today().isoformat()
    assert _rows_of(confirmed_export(world.db, *PERIOD))
    stamps = _tax_dates(world.db)
    assert stamps[out_row] in _today_candidates(before)
    assert [booking_id for booking_id, tax in stamps.items() if tax and booking_id != out_row] == []


def test_a_second_export_keeps_the_time_of_the_first_and_stamps_the_tax_date_again(world):
    out_row, _in_row, _april = _transfer_and_a_row_outside(world)
    assert _rows_of(confirmed_export(world.db, *PERIOD))
    _execute(world.db, "UPDATE BookingExports SET ExportedAt = '2001-02-03 04:05:06'")
    _execute(world.db, "UPDATE Bookings SET DateTax = '1999-12-31' WHERE ID = ?", (out_row,))
    before = datetime.date.today().isoformat()
    assert _rows_of(confirmed_export(world.db, *PERIOD))
    assert _marks(world.db) == {out_row: '2001-02-03 04:05:06'}                # der erste Export zählt
    assert _tax_dates(world.db)[out_row] in _today_candidates(before)           # das Steuerdatum wie bisher bei jedem Export


def test_a_row_that_enters_a_later_stack_gets_its_own_mark_the_earlier_one_stays(world):
    first = _book_transfer(world)
    later = _book_transfer(world, day_out='2026-04-10')
    assert _rows_of(confirmed_export(world.db, *PERIOD))
    _execute(world.db, "UPDATE BookingExports SET ExportedAt = '2001-02-03 04:05:06'")
    assert _rows_of(confirmed_export(world.db, '2026-01-01', '2026-12-31'))
    marks = _marks(world.db)
    assert set(marks) == {first[1], later[1]} and marks[first[1]] == '2001-02-03 04:05:06' and marks[later[1]] != '2001-02-03 04:05:06'


def test_an_export_that_asks_first_or_fails_marks_nothing(world):
    _book_transfer(world)
    _bank(world, world.a, '2026-03-05', '-10.00')                               # eine Bankbewegung ohne Buchungssatz: der Export fragt zurück
    asked = _handler(world, *PERIOD)
    assert is_confirm_redirect(asked) and _marks(world.db) == {} and _stamped(world.db) == set()
    for bad in (('', ''), ('2026-03-31', '2026-03-01'), ('2025-12-01', '2026-01-31')):
        result = handlers.handle_datev_export(world.db, datev_post(*bad))
        assert result[0] == 303
    assert _marks(world.db) == {} and _stamped(world.db) == set()


def test_the_stack_is_byte_for_byte_the_one_without_the_marks(world, tmp_path):
    """Dieselbe Datenbank, einmal mit dem Merkmal (``mark_datev_export``), einmal mit dem alten Weg (``update_bookings_date_tax_batch``): gleiche Bytes, gleiche Steuerdaten."""
    _transfer_and_a_row_outside(world)
    copy = str(tmp_path / 'ohne_merkmal.db')
    shutil.copyfile(world.db.db_name, copy)
    twin = Database(db_name=copy)
    twin.mark_datev_export = lambda ids, date_tax: twin.update_bookings_date_tax_batch(ids, date_tax)
    new = confirmed_export(world.db, *PERIOD)
    old = confirmed_export(twin, *PERIOD)
    assert isinstance(new[0], bytes) and bytes(new[0]) == bytes(old[0]) and new[1] == old[1]
    assert _tax_dates(world.db) == _tax_dates(twin)
    assert _marks(world.db) and _marks(twin) == {}
