# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""AfA-Buchung (2.0.5): Umbuchung „Aufwand an Anlagekonto“.

``book_depreciation`` bucht die AfA eines Jahres als eigenständigen Buchungssatz ohne Konto
(``Account_ID`` leer, kein Geldfluss): COA = Aufwandskonto (Erfolgskonto 4000–7999), Gegenkonto =
Anlagekonto (1–999), Betrag POSITIV (COA im Soll), Status ``posted``, Datum 31.12. Je Anlage und
Jahr gibt es nur eine Buchung (Sperre in einer Transaktion, auch gegen WISO-Zeilen mit Status
``booked`` und gegen gleichzeitige Aufrufe) – „gebucht“ heißt, dass die Buchung es gibt: Eine Zeile
``posted`` ohne vorhandene Buchung (leer oder per SQL-Konsole gelöscht) gilt als nicht gebucht.
Nach dem Abgang der Anlage gibt es keine AfA mehr. Löscht man die Buchung, geht das Jahr auf
``planned`` zurück (ebenso die Vorlage „Zeitbereich löschen“); ``delete_asset`` lehnt ab, solange AfA
gebucht ist oder Unteranlagen existieren.

Die AfA zählt in der EÜR (Sektion 3, nur ohne Kontofilter), nicht im Dashboard: Sie ist kein
Geldfluss. Der DATEV-Export gibt sie unverändert als „Aufwand an Anlagekonto“ aus.

Geprüft werden außerdem Handler, Routen (``/assets/depreciate``, ``/assets/delete``,
``/assets/view?err=…``) und die Seiten (Anlagenliste, AfA-Plan der Anlage mit dem Formular).
Alle Namen und Werte sind erfunden.
"""
import csv
import datetime
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from money import from_minor
from server.handlers import (
    handle_asset_sale, handle_book_depreciation, handle_datev_export, handle_execute_sql,
)
from server.pages_assets import PageAssets, PageAssetView
from server.pages_miscellaneous import PageMiscellaneous
from tests.dom_helpers import Dom, app_msg_arguments, assert_same_structure
from tests.http_client import get, own_origin, post

D1, D2 = '2026-01-01', '2026-12-31'
PAYLOAD = '</script><img src=x onerror=alert(1)>'


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _rows(db, sql, params=()):
    """Zeilen als Dicts (Spaltenname -> Wert); die Verbindung wird geschlossen."""
    with closing(db._get_connection()) as conn:
        cursor = conn.execute(sql, params)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def _execute(db, sql, params=()):
    with closing(db._get_connection()) as conn:
        conn.execute(sql, params)
        conn.commit()


def _count(db, table):
    return _rows(db, f'SELECT COUNT(*) AS n FROM {table}')[0]['n']


def _plan_rows(db, asset):
    """AfA-Planzeilen einer Anlage: [(Jahr, Booking_ID, Status)]."""
    return [(r['Year'], r['Booking_ID'], r['Status'])
            for r in _rows(db, 'SELECT * FROM AssetDepreciations WHERE Asset_ID = ? ORDER BY Year',
                           (asset,))]


def _euer(db, date_from=D1, date_to=D2, account_ids=None):
    return {nr: Decimal(str(total))
            for nr, _name, total in db.get_euer_data(date_from, date_to, account_ids)}


def _book(db, asset, year=2026, expense=6220, asset_coa=None, **extra):
    """AfA über den Handler der Anlagenseite buchen (Formular ``/assets/depreciate``)."""
    form = {'asset_id': [str(asset)], 'year': [str(year)], 'coa_id': [str(_coa(db, expense))]}
    if asset_coa:
        form['asset_coa_id'] = [str(_coa(db, asset_coa))]
    return handle_book_depreciation(db, {**form, **{k: [str(v)] for k, v in extra.items()}})


def _error_of(location):
    """Die Meldung (``err``) aus der Weiterleitung."""
    return parse_qs(urlsplit(location).query).get('err', [None])[0]


def _chart(db, *numbers):
    """Konten im Kontenrahmen anlegen, falls sie fehlen (Grenzfälle der Kontenprüfung)."""
    for number in numbers:
        if _coa(db, number) is None:
            assert db.insert_chart_of_accounts(4, number, f'Grenzkonto {number}', 'Test')


def _console(db, *commands):
    """SQL über die Konsole der Seite „Verschiedenes“ ausführen (``handle_execute_sql``): eigene
    Verbindung ohne Fremdschlüsselprüfung, wie im Betrieb. Liefert die Antwort als Dict."""
    answer = json.loads(handle_execute_sql(db, {'sql_commands': [';\n'.join(commands)]}))
    assert not answer['errors'], answer
    return answer


def _datev(db):
    result = handle_datev_export(db, {'date_from': [D1], 'date_to': [D2]})
    assert isinstance(result[0], bytes), result
    lines = [line for line in result[0].decode('cp1252').split('\r\n') if line]
    rows = [next(csv.reader([line], delimiter=';', quotechar='"')) for line in lines[2:]]
    return [(r[0], r[1], r[6], r[7], r[8]) for r in rows]


@pytest.fixture
def world(tmp_db):
    """Frische Datenbank mit Girokonto (SKR 1800) und einem Laptop (1.800 EUR, 3 Jahre linear,
    Anlagekonto 0520) vom 10.01.2026: 600 EUR AfA je Jahr (2026, 2027, 2028)."""
    db = tmp_db
    db.insert_account('Afa-Giro', 'Owner', 'DE00DEPRECIATION', 'BIC', 'Bank', is_cash=0,
                      skr_account=1800)
    accounts = db.fetch_accounts()
    return SimpleNamespace(
        db=db, giro=next(a[0] for a in accounts if a[1] == 'Afa-Giro'),
        kasse=next(a[0] for a in accounts if a[6]),
        asset=db.insert_asset('Laptop Probe', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 520)),
        expense=_coa(db, 6220), anlage=_coa(db, 520))


def _wiso_booked(db, asset, year=2026):
    """Planzeile, wie der WISO-Import sie anlegt: Status ``booked``, aber keine Buchung."""
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, "
                 "Booking_ID, Status, BookedAt) VALUES (?, ?, 6000000, 12000000, NULL, 'booked', "
                 "NULL)", (asset, year))


def _old_depreciation(db, asset, account_id, year=2026):
    """AfA-Buchung alter Form (bis 2.0.4): Aufwandskonto auf einem Bank-/Kassenkonto, kein
    Gegenkonto, Betrag negativ, von der Planzeile referenziert."""
    booking = db.insert_booking(f'{year}-12-31', Decimal('-600'), account_id=account_id,
                                coa_id=_coa(db, 6220), booking_type='entry')
    _execute(db, "UPDATE Bookings SET Status = 'posted' WHERE ID = ?", (booking,))
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, "
                 "Booking_ID, Status, BookedAt) VALUES (?, ?, 6000000, 12000000, ?, 'posted', "
                 "CURRENT_TIMESTAMP)", (asset, year, booking))
    return booking


# ── Form der Buchung ─────────────────────────────────────────────────────────

def test_depreciation_is_a_transfer_from_expense_to_asset_account_without_a_bank_account(world):
    db = world.db
    status, location = _book(db, world.asset)

    assert (status, location) == (303, f'/assets/view?id={world.asset}')
    bookings = _rows(db, 'SELECT * FROM Bookings')
    assert len(bookings) == 1
    booking = bookings[0]
    assert booking['BookingType'] == 'entry' and booking['Status'] == 'posted'
    assert booking['Account_ID'] is None and booking['ParentBooking_ID'] is None
    assert (booking['COA_ID'], booking['CounterCOA_ID']) == (world.expense, world.anlage)
    assert from_minor(booking['Amount']) == Decimal('600')        # positiv: Aufwandskonto im Soll
    assert booking['TaxRate'] is None and booking['TaxAmount'] is None
    assert booking['DateBooking'] == booking['DateTax'] == '2026-12-31'
    assert booking['Currency'] == 'EUR'
    asset = db.get_asset_by_id(world.asset)
    assert booking['Text'] == f'AfA {asset[2]} 2026 ({asset[1]})'


def test_depreciation_plan_row_points_at_the_booking(world):
    db = world.db
    booking_id = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)

    (row,) = _rows(db, 'SELECT * FROM AssetDepreciations')
    assert (row['Asset_ID'], row['Year'], row['Booking_ID'], row['Status']) == \
           (world.asset, 2026, booking_id, 'posted')
    assert row['BookedAt'] is not None
    assert from_minor(row['DepreciationAmount']) == Decimal('600')
    assert from_minor(row['BookValue']) == Decimal('1200')
    assert _rows(db, 'SELECT ID FROM Bookings') == [{'ID': booking_id}]


def test_a_given_bank_account_is_ignored(world):
    """``account_id`` bleibt für bestehende Aufrufe, wird aber nicht mehr verwendet – auch nicht,
    wenn eine ältere Seite das Feld ``account_id`` noch mitschickt."""
    db = world.db
    first = db.book_depreciation(world.asset, 2026, world.giro, world.expense, world.anlage)
    _book(db, world.asset, 2027, account_id=world.kasse)
    rows = _rows(db, 'SELECT ID, Account_ID FROM Bookings ORDER BY ID')
    assert rows[0]['ID'] == first
    assert [r['Account_ID'] for r in rows] == [None, None]


def test_custom_description_is_kept(world):
    _book(world.db, world.asset, description='AfA Laptop für das Büro')
    assert _rows(world.db, 'SELECT Text FROM Bookings') == [{'Text': 'AfA Laptop für das Büro'}]


def test_each_year_has_its_own_booking_date_and_amount(world):
    db = world.db
    for year in (2026, 2027, 2028):
        _book(db, world.asset, year)
    rows = _rows(db, 'SELECT DateBooking, Amount FROM Bookings ORDER BY DateBooking')
    assert [(r['DateBooking'], from_minor(r['Amount'])) for r in rows] == [
        ('2026-12-31', Decimal('600')), ('2027-12-31', Decimal('600')),
        ('2028-12-31', Decimal('600'))]


# ── Anlagekonto und Aufwandskonto ────────────────────────────────────────────

def test_asset_account_comes_from_the_call_then_the_asset_then_its_category(tmp_db):
    db = tmp_db
    db.insert_asset_category('Büro-Probe', 3, 'linear', _coa(db, 500), 'Testkategorie')
    category = next(c[0] for c in db.fetch_asset_categories() if c[1] == 'Büro-Probe')
    own = db.insert_asset('Eigenes Konto', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 520),
                          asset_category_id=category)
    by_category = db.insert_asset('Konto der Kategorie', '2026-01-10', 1800.0, 3,
                                  asset_category_id=category)
    nothing = db.insert_asset('Ohne Konto', '2026-01-10', 1800.0, 3)

    assert db.resolve_asset_coa(db.get_asset_by_id(own)) == _coa(db, 520)
    assert db.resolve_asset_coa(db.get_asset_by_id(own), _coa(db, 560)) == _coa(db, 560)
    assert db.resolve_asset_coa(db.get_asset_by_id(by_category)) == _coa(db, 500)
    assert db.resolve_asset_coa(db.get_asset_by_id(nothing)) is None
    assert db.resolve_asset_coa(db.get_asset_by_id(nothing), _coa(db, 560)) == _coa(db, 560)


def test_booking_uses_the_resolved_asset_account(tmp_db):
    db = tmp_db
    db.insert_asset_category('Büro-Probe', 3, 'linear', _coa(db, 500), 'Testkategorie')
    category = next(c[0] for c in db.fetch_asset_categories() if c[1] == 'Büro-Probe')
    by_category = db.insert_asset('Konto der Kategorie', '2026-01-10', 1800.0, 3,
                                  asset_category_id=category)
    own = db.insert_asset('Eigenes Konto', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 520))

    first = db.book_depreciation(by_category, 2026, None, _coa(db, 6220))
    second = db.book_depreciation(own, 2026, None, _coa(db, 6220))
    third = db.book_depreciation(own, 2027, None, _coa(db, 6220), _coa(db, 560))

    assert [db.get_booking_by_id(b)[9] for b in (first, second, third)] == \
           [_coa(db, 500), _coa(db, 520), _coa(db, 560)]


def test_asset_without_any_asset_account_cannot_be_booked(tmp_db):
    db = tmp_db
    asset = db.insert_asset('Ohne Konto', '2026-01-10', 1800.0, 3)

    with pytest.raises(ValueError, match='Anlagenkonto'):
        db.book_depreciation(asset, 2026, None, _coa(db, 6220))

    assert _count(db, 'Bookings') == 0 and _plan_rows(db, asset) == []


def test_missing_expense_account_is_refused(world):
    with pytest.raises(ValueError, match='Aufwandskonto'):
        world.db.book_depreciation(world.asset, 2026, None, None, world.anlage)
    assert _count(world.db, 'Bookings') == 0 and _plan_rows(world.db, world.asset) == []


def test_handler_reports_a_refusal_instead_of_failing(world):
    """Der Handler leitet mit ``err`` auf die Anlagenseite zurück; gebucht wird nichts."""
    db = world.db
    no_account = db.insert_asset('Ohne Konto', '2026-01-10', 1800.0, 3)

    status, location = _book(db, no_account)
    assert status == 303
    assert location.startswith(f'/assets/view?id={no_account}&err=')
    assert _error_of(location) == 'Anlage hat kein Anlagenkonto'

    status, location = handle_book_depreciation(
        db, {'asset_id': [str(world.asset)], 'year': ['2026']})            # kein coa_id
    assert (status, _error_of(location)) == (303, 'Aufwandskonto fehlt')

    status, location = _book(db, world.asset, 2099)
    assert (status, _error_of(location)) == (303, 'No depreciation planned for year 2099')
    assert _count(db, 'Bookings') == 0


# ── Je Anlage und Jahr nur einmal ────────────────────────────────────────────

def test_second_booking_of_the_same_year_is_refused(world):
    db = world.db
    first = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)

    with pytest.raises(ValueError, match='AfA 2026 ist bereits gebucht'):
        db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)

    assert _rows(db, 'SELECT ID FROM Bookings') == [{'ID': first}]
    assert _plan_rows(db, world.asset) == [(2026, first, 'posted')]


def test_handler_redirects_with_the_message_for_a_second_booking(world):
    db = world.db
    _book(db, world.asset)

    status, location = _book(db, world.asset)

    assert status == 303
    assert location.startswith(f'/assets/view?id={world.asset}&err=')
    assert _error_of(location) == 'AfA 2026 ist bereits gebucht'
    assert _count(db, 'Bookings') == 1


def test_other_years_and_other_assets_are_not_blocked(world):
    db = world.db
    other = db.insert_asset('Drucker Probe', '2026-02-01', 1800.0, 3, coa_id=_coa(db, 520))
    for asset, year in ((world.asset, 2026), (world.asset, 2027), (other, 2026)):
        db.book_depreciation(asset, year, None, world.expense, world.anlage)
    assert _count(db, 'Bookings') == 3


def test_year_imported_from_wiso_counts_as_booked(world):
    """WISO-Zeilen haben Status ``booked`` ohne Buchung – die Sperre gilt auch dafür."""
    db = world.db
    _wiso_booked(db, world.asset)

    with pytest.raises(ValueError, match='bereits gebucht'):
        db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)

    assert _count(db, 'Bookings') == 0
    assert _plan_rows(db, world.asset) == [(2026, None, 'booked')]
    status, location = _book(db, world.asset)
    assert (status, _error_of(location)) == (303, 'AfA 2026 ist bereits gebucht')


def test_year_with_a_plan_row_that_is_not_booked_can_be_booked(world):
    db = world.db
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, "
                 "Status) VALUES (?, 2026, 6000000, 12000000, 'planned')", (world.asset,))

    booking = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)

    assert _plan_rows(db, world.asset) == [(2026, booking, 'posted')]


def test_eight_simultaneous_bookings_of_the_same_year_create_exactly_one_booking(world):
    """Doppelklick oder zwei gleichzeitige Anfragen: Prüfung und Anlegen laufen in einer
    Transaktion, die den Schreibzugriff sofort sperrt."""
    db = world.db
    outcomes = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)
            outcomes.append('gebucht')
        except ValueError as exc:
            outcomes.append(str(exc))
        except Exception as exc:                                  # noqa: BLE001 – das wäre ein Fehler
            outcomes.append(f'{type(exc).__name__}: {exc}')

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert dict(Counter(outcomes)) == {'gebucht': 1, 'AfA 2026 ist bereits gebucht': 7}
    assert _count(db, 'Bookings') == 1 and _count(db, 'AssetDepreciations') == 1
    [(_year, booking_id, status)] = _plan_rows(db, world.asset)
    assert status == 'posted' and _rows(db, 'SELECT ID FROM Bookings') == [{'ID': booking_id}]


def test_booking_waits_for_a_concurrent_writer_before_it_checks_the_year(world):
    """Prüfung und Anlegen laufen in einer Transaktion, die den Schreibzugriff SOFORT sperrt (``BEGIN
    IMMEDIATE``): Hält eine andere Anfrage die Sperre, wartet die Buchung, bevor sie prüft – und sieht
    danach, was die andere gebucht hat. Ohne die sofortige Sperre prüfte sie vorher (Jahr noch frei), legte
    nach dem Warten aber eine zweite Buchung an."""
    db = world.db
    outcome = []

    def second_request():
        try:
            db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)
            outcome.append('gebucht')
        except ValueError as exc:
            outcome.append(str(exc))

    with closing(sqlite3.connect(db.db_name, timeout=30)) as first_request:
        first_request.execute('PRAGMA foreign_keys = ON')
        first_request.execute('BEGIN IMMEDIATE')                       # die erste Anfrage hält die Sperre
        thread = threading.Thread(target=second_request)
        thread.start()
        time.sleep(1.0)                                                # die zweite ist bei ihrer Prüfung
        booking = first_request.execute(
            "INSERT INTO Bookings (DateBooking, DateTax, COA_ID, CounterCOA_ID, Amount, Currency, Text, "
            "BookingType, Status) VALUES ('2026-12-31', '2026-12-31', ?, ?, 6000000, 'EUR', 'AfA parallel', "
            "'entry', 'posted')", (world.expense, world.anlage)).lastrowid
        first_request.execute(
            "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Booking_ID, "
            "Status, BookedAt) VALUES (?, 2026, 6000000, 12000000, ?, 'posted', CURRENT_TIMESTAMP)",
            (world.asset, booking))
        first_request.commit()
    thread.join(timeout=30)

    assert not thread.is_alive()
    assert outcome == ['AfA 2026 ist bereits gebucht']
    assert _rows(db, 'SELECT ID FROM Bookings') == [{'ID': booking}]
    assert _plan_rows(db, world.asset) == [(2026, booking, 'posted')]


# ── Gebucht heißt: die Buchung gibt es ───────────────────────────────────────

STALE = ['null', 'dead']


def _stale_posted_row(db, world, year, how):
    """Planzeile mit Status ``posted``, deren Buchung es nicht gibt: ``null`` – ohne Booking_ID (so ließ
    ``delete_transaction`` sie bis 2.0.4 zurück), ``dead`` – die Booking_ID zeigt auf eine Buchung, die
    über die SQL-Konsole (ohne Fremdschlüsselprüfung) gelöscht wurde."""
    if how == 'null':
        _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, "
                     "Booking_ID, Status, BookedAt) VALUES (?, ?, 6000000, 12000000, NULL, 'posted', "
                     "CURRENT_TIMESTAMP)", (world.asset, year))
        return None
    booking = db.book_depreciation(world.asset, year, None, world.expense, world.anlage)
    _console(db, f'DELETE FROM Bookings WHERE ID = {booking}')
    assert (year, booking, 'posted') in _plan_rows(db, world.asset)              # tote Booking_ID
    return booking


@pytest.mark.parametrize('how', STALE)
def test_posted_year_without_an_existing_booking_can_be_booked_again(world, how):
    """Gebucht heißt: Die Buchung gibt es. Eine ``posted``-Zeile ohne vorhandene Buchung sperrt das Jahr
    nicht: ``book_depreciation`` bucht neu – genau eine Buchung, die Planzeile zeigt darauf."""
    db = world.db
    dead = _stale_posted_row(db, world, 2026, how)

    booking = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)

    assert booking != dead
    assert _rows(db, 'SELECT ID FROM Bookings') == [{'ID': booking}]
    assert _plan_rows(db, world.asset) == [(2026, booking, 'posted')]
    assert _euer(db) == {6220: Decimal('-600')}


@pytest.mark.parametrize('how', STALE)
def test_handler_books_a_posted_year_without_an_existing_booking_again(world, how):
    db = world.db
    _stale_posted_row(db, world, 2026, how)

    status, location = _book(db, world.asset)

    assert (status, location) == (303, f'/assets/view?id={world.asset}')
    assert _count(db, 'Bookings') == 1
    assert _plan_rows(db, world.asset)[0][2] == 'posted' and _plan_rows(db, world.asset)[0][1] is not None


@pytest.mark.parametrize('how', STALE)
def test_asset_with_a_posted_year_without_an_existing_booking_can_be_deleted(world, how):
    db = world.db
    _stale_posted_row(db, world, 2026, how)

    db.delete_asset(world.asset)

    assert db.get_asset_by_id(world.asset) is None
    assert _count(db, 'AssetDepreciations') == 0


def test_a_year_with_an_existing_booking_still_blocks_the_deletion_of_the_asset(world):
    """Gegenprobe: Existiert die Buchung, bleibt es bei der Ablehnung (auch nach den toten Zeilen
    eines anderen Jahres)."""
    db = world.db
    _stale_posted_row(db, world, 2026, 'dead')
    db.book_depreciation(world.asset, 2027, None, world.expense, world.anlage)

    with pytest.raises(ValueError) as refusal:
        db.delete_asset(world.asset)

    assert 'AfA gebucht' in str(refusal.value) and '2027' in str(refusal.value)
    assert '2026' not in str(refusal.value)               # das tote Jahr zählt nicht als gebucht
    assert db.get_asset_by_id(world.asset) is not None


@pytest.mark.parametrize('how', STALE)
def test_year_with_a_posted_row_but_no_booking_stays_blocked_only_when_imported_from_wiso(world, how):
    """``booked`` (WISO-Import, keine Buchung vorgesehen) bleibt gesperrt – anders als ``posted``
    ohne Buchung."""
    db = world.db
    _stale_posted_row(db, world, 2026, how)
    _wiso_booked(db, world.asset, 2027)

    db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)       # geht
    with pytest.raises(ValueError, match='bereits gebucht'):
        db.book_depreciation(world.asset, 2027, None, world.expense, world.anlage)    # gesperrt


def test_booked_years_are_the_years_with_an_existing_booking_or_imported_as_booked(world):
    """Der Vertrag hinter Seite, Sperre und Löschen: Gebucht sind die Jahre, deren Buchung es gibt, und
    die als ``booked`` importierten; ``posted`` ohne Buchung (leer oder tot) und ``planned`` nicht."""
    db = world.db
    db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)       # Buchung vorhanden
    _wiso_booked(db, world.asset, 2027)                                              # WISO-Import
    _stale_posted_row(db, world, 2028, 'dead')                                       # Buchung gelöscht
    other = db.insert_asset('Drucker Probe', '2026-02-01', 1800.0, 3, coa_id=_coa(db, 520))
    _stale_posted_row(db, SimpleNamespace(asset=other, expense=world.expense, anlage=world.anlage),
                      2026, 'null')
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, "
                 "Status) VALUES (?, 2027, 6000000, 6000000, 'planned')", (other,))

    assert db.get_booked_depreciation_years(world.asset) == {2026, 2027}
    assert db.get_booked_depreciation_years(other) == set()
    assert db.get_booked_depreciation_years(99999) == set()


def _range_delete_script(db, date_from, date_to):
    """Das Lösch-Skript „Zeitbereich löschen“ der Seite „Verschiedenes“, so zusammengesetzt, wie das
    JavaScript der Seite (``buildRangeDeleteSql``) es tut."""
    html = PageMiscellaneous(db)
    start = html.index('function buildRangeDeleteSql()')
    body = html[start:html.index("document.getElementById('sql_input').value = sql;", start)]
    env = {'from': date_from, 'to': date_to}

    def evaluate(expression):
        parts = []
        for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|([A-Za-z_]\w*)', expression):
            parts.append(match.group(1).replace('\\n', '\n') if match.group(2) is None
                         else env[match.group(2)])
        return ''.join(parts)

    for name in ('b', 'd', 'i'):
        env[name] = evaluate(re.search(rf'const {name} = (.*?);\n', body).group(1))
    return evaluate(re.search(r'const sql =(.*?);\s*$', body.rstrip(), re.S).group(1))


def test_range_delete_script_resets_the_depreciation_rows_of_the_deleted_bookings(world):
    """Die Vorlage „Zeitbereich löschen“ nimmt die AfA-Buchungen des Zeitraums mit und setzt die
    betroffenen Planzeilen auf ``planned`` zurück (ohne Buchung und BookedAt); andere Jahre bleiben."""
    db = world.db
    first = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)
    second = db.book_depreciation(world.asset, 2027, None, world.expense, world.anlage)
    db.insert_booking('2026-05-01', Decimal('-10'), coa_id=_coa(db, 6815), booking_type='entry')
    script = _range_delete_script(db, '2026-01-01', '2026-12-31')

    answer = _console(db, script)

    assert answer['success_count'] == answer['total'] and answer['total'] > 5
    assert _rows(db, 'SELECT ID FROM Bookings') == [{'ID': second}]               # 2026 ist gelöscht
    assert _plan_rows(db, world.asset) == [(2026, None, 'planned'), (2027, second, 'posted')]
    rows = _rows(db, 'SELECT Year, BookedAt FROM AssetDepreciations ORDER BY Year')
    assert rows[0]['BookedAt'] is None and rows[1]['BookedAt'] is not None
    assert first != second
    # das Jahr lässt sich neu buchen
    again = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)
    assert _plan_rows(db, world.asset) == [(2026, again, 'posted'), (2027, second, 'posted')]


def test_range_delete_script_leaves_wiso_rows_without_a_booking_alone(world):
    db = world.db
    _wiso_booked(db, world.asset, 2026)
    db.book_depreciation(world.asset, 2027, None, world.expense, world.anlage)

    _console(db, _range_delete_script(db, '2026-01-01', '2027-12-31'))

    assert _plan_rows(db, world.asset) == [(2026, None, 'booked'), (2027, None, 'planned')]


# ── Nach dem Abgang der Anlage ───────────────────────────────────────────────

def test_depreciation_after_the_year_of_disposal_is_refused(world):
    """Im Abgangsjahr steht die (zeitanteilige) AfA noch, danach nicht mehr: Was der Plan für spätere
    Jahre vorsieht, kommt nicht in die Bücher."""
    db = world.db
    db.sell_asset(world.asset, '2027-06-30', 500.0)                  # Abgang im zweiten Planjahr
    db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)
    db.book_depreciation(world.asset, 2027, None, world.expense, world.anlage)

    with pytest.raises(ValueError,
                       match=r'^AfA 2028 liegt nach dem Abgang der Anlage \(2027-06-30\)$'):
        db.book_depreciation(world.asset, 2028, None, world.expense, world.anlage)

    assert _count(db, 'Bookings') == 2
    assert [row[0] for row in _plan_rows(db, world.asset)] == [2026, 2027]


def test_depreciation_after_a_scrapping_is_refused_as_well(world):
    db = world.db
    db.sell_asset(world.asset, '2026-11-30', 0)                      # Verschrottung im ersten Jahr
    with pytest.raises(ValueError, match='liegt nach dem Abgang'):
        db.book_depreciation(world.asset, 2027, None, world.expense, world.anlage)
    db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)       # Abgangsjahr: geht
    assert _count(db, 'Bookings') == 1


def test_handler_reports_a_year_after_the_disposal(world):
    db = world.db
    db.sell_asset(world.asset, '2026-11-30', 100.0)

    status, location = _book(db, world.asset, 2027)

    assert status == 303 and location.startswith(f'/assets/view?id={world.asset}&err=')
    assert 'liegt nach dem Abgang' in _error_of(location)
    assert _count(db, 'Bookings') == 0


# ── Abgangsdatum ─────────────────────────────────────────────────────────────
# Das Datum steuert, bis wann AfA gebucht werden darf: ``handle_asset_sale`` nimmt nur ein ISO-Datum
# (``parse_iso_date``). Ältere Einträge in anderer Schreibweise lassen Seite und Buchung nicht abstürzen.

SALE_ERROR = 'Abgangsdatum fehlt oder ist ungültig (JJJJ-MM-TT).'


def _sell(db, asset, date, price='500'):
    return handle_asset_sale(db, {'asset_id': [str(asset)], 'sale_date': [date], 'sale_price': [price]})


def test_sale_with_an_iso_date_is_saved(world):
    db = world.db

    status, location = _sell(db, world.asset, '2027-06-30', '500')

    assert (status, location) == (303, f'/assets/view?id={world.asset}')
    asset = db.get_asset_by_id(world.asset)
    assert (asset[15], asset[16], asset[17]) == ('2027-06-30', Decimal('500'), 'sold')


def test_sale_date_is_taken_without_surrounding_blanks(world):
    db = world.db
    assert _sell(db, world.asset, ' 2027-06-30 ')[1] == f'/assets/view?id={world.asset}'
    assert db.get_asset_by_id(world.asset)[15] == '2027-06-30'


@pytest.mark.parametrize('date', ['', '   ', '15.07.2022', '2022-13-01', '2022-02-30', '2022-7-15',
                                  '2022/07/15', 'morgen', '1899-12-31', '2201-01-01'],
                         ids=['leer', 'blanks', 'deutsch', 'monat-13', 'tag-30-feb', 'ohne-nullen',
                              'schraegstrich', 'text', 'vor-1900', 'nach-2200'])
def test_sale_with_a_date_that_is_not_an_iso_date_is_refused_and_saves_nothing(world, date):
    db = world.db

    status, location = _sell(db, world.asset, date)

    assert status == 303 and location.startswith(f'/assets/view?id={world.asset}&err=')
    assert _error_of(location) == SALE_ERROR
    asset = db.get_asset_by_id(world.asset)
    assert (asset[15], asset[16], asset[17]) == (None, None, 'active')
    db.book_depreciation(world.asset, 2028, None, world.expense, world.anlage)       # kein Abgang


@pytest.mark.parametrize('legacy', ['15.07.2022', 'Juli 2022', '??'])
def test_sale_date_in_another_format_counts_as_no_year_of_disposal(world, legacy):
    """Ein Altbestand mit ``SaleDate`` in anderer Schreibweise (per SQL gesetzt) lässt weder die Seiten noch
    ``book_depreciation`` abstürzen: Es gilt kein Abgangsjahr, die AfA bleibt buchbar."""
    db = world.db
    _execute(db, "UPDATE Assets SET SaleDate = ?, Status = 'sold' WHERE ID = ?", (legacy, world.asset))

    page = PageAssetView(db, world.asset)
    listing = PageAssets(db)

    assert '○ Nach Abgang' not in page and 'Laptop Probe' in listing
    booking = db.book_depreciation(world.asset, 2028, None, world.expense, world.anlage)
    assert db.get_booking_by_id(booking)[1] == '2028-12-31'


def test_sale_route_refuses_a_wrong_date_with_a_message_and_saves_a_right_one(web):
    refused = _post(web, '/assets/sell', {'asset_id': web.asset, 'sale_date': '15.07.2026', 'sale_price': 0})

    assert refused.status == 303
    assert _error_of(refused.headers['Location']) == SALE_ERROR
    assert app_msg_arguments(Dom(get(web.port, refused.headers['Location']).text)) == [SALE_ERROR, 'error']
    assert web.db.get_asset_by_id(web.asset)[17] == 'active'

    saved = _post(web, '/assets/sell', {'asset_id': web.asset, 'sale_date': '2026-07-15', 'sale_price': 0})

    assert (saved.status, saved.headers['Location']) == (303, f'/assets/view?id={web.asset}')
    assert web.db.get_asset_by_id(web.asset)[15] == '2026-07-15'


# ── Konten werden geprüft ────────────────────────────────────────────────────
# Aufwandskonto: Erfolgskonto 4000–7999. Anlagekonto: 1–999 (SKR04-Klasse 0).

@pytest.mark.parametrize('expense', [1800, 3150, 520, 3999, 8000])
def test_expense_account_outside_4000_to_7999_is_refused(world, expense):
    db = world.db
    _chart(db, 3999, 8000)
    with pytest.raises(ValueError, match='Aufwandskonto'):
        db.book_depreciation(world.asset, 2026, None, _coa(db, expense), world.anlage)
    assert _count(db, 'Bookings') == 0 and _plan_rows(db, world.asset) == []


@pytest.mark.parametrize('expense, anlage, message', [
    (1800, None, 'Konto 1800 Bank 0 ist kein Aufwandskonto (erlaubt: Konten 4000–7999)'),
    (None, 1800, 'Konto 1800 Bank 0 ist kein Anlagekonto (erlaubt: Konten 1–999)'),
    (999999, None, 'Konto-ID 999999 ist kein Aufwandskonto (erlaubt: Konten 4000–7999)'),
    (None, 999999, 'Konto-ID 999999 ist kein Anlagekonto (erlaubt: Konten 1–999)'),
], ids=['aufwand-bank', 'anlage-bank', 'aufwand-unbekannt', 'anlage-unbekannt'])
def test_refusals_name_the_account_and_the_allowed_range(world, expense, anlage, message):
    """Die Meldung nennt das Konto und den erlaubten Bereich – so steht sie auch im Handler (``err``)."""
    db = world.db
    expense_coa = world.expense if expense is None else expense if expense == 999999 else _coa(db, expense)
    anlage_coa = world.anlage if anlage is None else anlage if anlage == 999999 else _coa(db, anlage)

    with pytest.raises(ValueError) as refusal:
        db.book_depreciation(world.asset, 2026, None, expense_coa, anlage_coa)

    assert str(refusal.value) == message
    assert _count(db, 'Bookings') == 0


def test_unknown_account_ids_are_refused(world):
    db = world.db
    with pytest.raises(ValueError, match='Aufwandskonto'):
        db.book_depreciation(world.asset, 2026, None, 999999, world.anlage)
    with pytest.raises(ValueError, match='Anlagen?konto'):
        db.book_depreciation(world.asset, 2026, None, world.expense, 999999)
    assert _count(db, 'Bookings') == 0


@pytest.mark.parametrize('expense', [4000, 6220, 7999])
def test_expense_accounts_at_the_boundaries_are_accepted(world, expense):
    db = world.db
    _chart(db, 4000, 7999)
    booking = db.book_depreciation(world.asset, 2026, None, _coa(db, expense), world.anlage)
    assert db.get_booking_by_id(booking)[8] == _coa(db, expense)


@pytest.mark.parametrize('anlage', [1000, 1800, 6220, 3999])
def test_asset_account_outside_1_to_999_is_refused(world, anlage):
    db = world.db
    _chart(db, 1000, 3999)
    with pytest.raises(ValueError, match='Anlagen?konto'):
        db.book_depreciation(world.asset, 2026, None, world.expense, _coa(db, anlage))
    assert _count(db, 'Bookings') == 0 and _plan_rows(db, world.asset) == []


@pytest.mark.parametrize('anlage', [1, 470, 999])
def test_asset_accounts_at_the_boundaries_are_accepted(world, anlage):
    db = world.db
    _chart(db, 1, 999)
    booking = db.book_depreciation(world.asset, 2026, None, world.expense, _coa(db, anlage))
    assert db.get_booking_by_id(booking)[9] == _coa(db, anlage)


def test_asset_with_a_wrong_own_account_cannot_be_booked_without_a_better_one(world):
    """Das Anlagekonto der Anlage selbst wird ebenso geprüft wie das übergebene."""
    db = world.db
    wrong = db.insert_asset('Falsches Konto', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 1800))

    with pytest.raises(ValueError, match='Anlagen?konto'):
        db.book_depreciation(wrong, 2026, None, world.expense)
    booking = db.book_depreciation(wrong, 2026, None, world.expense, world.anlage)    # richtiges übergeben

    assert db.get_booking_by_id(booking)[9] == world.anlage


@pytest.mark.parametrize('field, number, message', [
    ('expense', 1800, 'Aufwandskonto'), ('expense', 8000, 'Aufwandskonto'),
    ('asset_coa', 1800, 'Anlagen?konto'), ('asset_coa', 6220, 'Anlagen?konto'),
])
def test_handler_reports_a_wrong_account_with_a_message(world, field, number, message):
    db = world.db
    _chart(db, 8000)
    options = {'expense': 6220, 'asset_coa': 520, field: number}

    status, location = _book(db, world.asset, expense=options['expense'], asset_coa=options['asset_coa'])

    assert status == 303 and location.startswith(f'/assets/view?id={world.asset}&err=')
    assert re.search(message, _error_of(location))
    assert _count(db, 'Bookings') == 0


# ── Buchung löschen setzt das Jahr zurück ────────────────────────────────────

def test_deleting_the_booking_puts_the_year_back_to_planned_and_it_can_be_booked_again(world):
    db = world.db
    first = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)

    db.delete_transaction(first)

    (row,) = _rows(db, 'SELECT * FROM AssetDepreciations')
    assert (row['Booking_ID'], row['Status'], row['BookedAt']) == (None, 'planned', None)
    assert _count(db, 'Bookings') == 0

    again = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)

    assert again != first
    assert _rows(db, 'SELECT ID FROM Bookings') == [{'ID': again}]          # genau eine Buchung
    assert _plan_rows(db, world.asset) == [(2026, again, 'posted')]


def test_deleting_a_booking_leaves_the_other_years_alone(world):
    db = world.db
    first = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)
    second = db.book_depreciation(world.asset, 2027, None, world.expense, world.anlage)
    unrelated = db.insert_booking('2026-05-01', Decimal('-10'), coa_id=_coa(db, 6815),
                                  booking_type='entry')

    db.delete_transaction(unrelated)
    db.delete_transaction(first)

    assert _plan_rows(db, world.asset) == [(2026, None, 'planned'), (2027, second, 'posted')]
    assert _rows(db, 'SELECT ID FROM Bookings') == [{'ID': second}]


def test_old_depreciation_is_replaced_by_deleting_and_booking_again(world):
    """Der Weg, den docs/aufgaben.md für alte AfA-Sätze nennt: Die Buchung alter Form (Aufwandskonto
    auf einem Bankkonto, kein Gegenkonto) löschen und neu buchen. Danach stimmen EÜR und DATEV."""
    db = world.db
    old = _old_depreciation(db, world.asset, world.giro)
    assert _datev(db) == [('600,00', 'S', '6220', '', '0')]

    db.delete_transaction(old)
    db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)

    assert _euer(db) == {6220: Decimal('-600')}
    assert _datev(db) == [('600,00', 'S', '6220', '520', '0')]


# ── Anlage löschen ───────────────────────────────────────────────────────────

def test_asset_with_booked_depreciation_cannot_be_deleted(world):
    db = world.db
    booking = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)
    db.book_depreciation(world.asset, 2027, None, world.expense, world.anlage)

    with pytest.raises(ValueError) as refusal:
        db.delete_asset(world.asset)

    assert 'AfA gebucht' in str(refusal.value) and '2026, 2027' in str(refusal.value)
    assert db.get_asset_by_id(world.asset) is not None
    assert db.get_booking_by_id(booking) is not None
    assert [r[2] for r in _plan_rows(db, world.asset)] == ['posted', 'posted']


def test_asset_with_extensions_cannot_be_deleted_and_nothing_fails_inside_the_database(world):
    db = world.db
    extension = db.insert_asset('Speicher Probe', '2026-02-01', 900.0, 3, parent_id=world.asset)

    with pytest.raises(ValueError, match='Unteranlagen'):        # kein sqlite3.IntegrityError
        db.delete_asset(world.asset)

    assert db.get_asset_by_id(world.asset) is not None and db.get_asset_by_id(extension) is not None
    db.delete_asset(extension)                                    # erst die Erweiterung …
    db.delete_asset(world.asset)                                  # … dann die Anlage
    assert db.get_asset_by_id(world.asset) is None


def test_asset_with_booked_depreciation_of_an_extension_cannot_be_deleted_either(world):
    db = world.db
    # Bestand: Eine Erweiterung mit eigener gebuchter AfA (seit 2.2.7 bucht ``book_depreciation`` für eine
    # Erweiterung ohne gebuchtes Jahr nicht mehr – ihre AfA läuft über die Hauptanlage)
    extension = db.insert_asset('Speicher Probe', '2026-02-01', 1800.0, 3, coa_id=_coa(db, 520))
    db.book_depreciation(extension, 2026, None, world.expense, world.anlage)
    _execute(db, 'UPDATE Assets SET Parent_ID = ? WHERE ID = ?', (world.asset, extension))

    with pytest.raises(ValueError, match='AfA gebucht'):
        db.delete_asset(extension)
    assert db.get_asset_by_id(extension) is not None


def test_asset_without_booked_depreciation_is_deleted_with_its_plan_rows(world):
    """Planzeilen ohne Buchung (WISO-Import, nicht gebuchte Jahre) hindern das Löschen nicht."""
    db = world.db
    _wiso_booked(db, world.asset, 2026)
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, "
                 "Status) VALUES (?, 2027, 6000000, 6000000, 'planned')", (world.asset,))

    db.delete_asset(world.asset)

    assert db.get_asset_by_id(world.asset) is None
    assert _count(db, 'AssetDepreciations') == 0


def test_asset_can_be_deleted_after_its_depreciation_booking_is_deleted(world):
    db = world.db
    booking = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)
    db.delete_transaction(booking)
    db.delete_asset(world.asset)
    assert db.get_asset_by_id(world.asset) is None
    assert _count(db, 'Bookings') == 0 and _count(db, 'AssetDepreciations') == 0


# ── EÜR, Dashboard, DATEV ────────────────────────────────────────────────────

def test_depreciation_is_an_expense_in_the_euer_and_not_a_cash_flow(world):
    db = world.db
    _book(db, world.asset)

    assert _euer(db) == {6220: Decimal('-600')}                   # kein Anlagekonto 0520 darin
    for ids in (None, [], [world.giro], [world.kasse], [world.giro, world.kasse]):
        totals = db.get_dashboard_totals(D1, D2, ids)
        assert (totals['income'], totals['expense'], totals['private'], totals['bank_count']) == \
               (0, 0, 0, 0)
        assert all(m['income'] == m['expense'] == m['private'] == 0
                   for m in db.get_dashboard_monthly(D1, D2, ids))


def test_depreciation_counts_in_the_euer_only_when_all_accounts_are_selected(world):
    db = world.db
    _book(db, world.asset)
    assert _euer(db, account_ids=[world.giro, world.kasse]) == {6220: Decimal('-600')}
    assert _euer(db, account_ids=[world.giro]) == {}
    assert _euer(db, account_ids=[world.kasse]) == {}


def test_depreciation_belongs_to_the_year_of_its_booking_date(tmp_db):
    db = tmp_db
    asset = db.insert_asset('Laptop Probe', '2025-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    _book(db, asset, 2025)
    _book(db, asset, 2026)

    assert _euer(db, '2025-01-01', '2025-12-31') == {6220: Decimal('-600')}
    assert _euer(db, '2026-01-01', '2026-12-31') == {6220: Decimal('-600')}
    assert _euer(db, '2025-01-01', '2026-12-31') == {6220: Decimal('-1200')}
    assert _euer(db, '2027-01-01', '2027-12-31') == {}
    assert _euer(db, '2025-01-01', '2025-12-30') == {}            # gebucht zum 31.12.
    assert _euer(db, '2025-12-31', '2025-12-31') == {6220: Decimal('-600')}


@pytest.mark.parametrize('expense, anlage, plan, amount', [
    (6220, 520, ('Laptop Probe', 1800.0, 3), '600'),
    (6222, 520, ('Lieferwagen Probe', 24000.0, 6), '4000'),
    (6260, 670, ('Monitor Probe', 500.0, 3), '500'),
], ids=['sachanlage', 'fahrzeug', 'gwg'])
def test_depreciation_shows_the_expense_account_in_euer_and_datev(tmp_db, expense, anlage, plan,
                                                                  amount):
    db = tmp_db
    asset = db.insert_asset(plan[0], '2026-01-10', plan[1], plan[2], coa_id=_coa(db, anlage))
    _book(db, asset, 2026, expense=expense)

    assert _euer(db) == {expense: -Decimal(amount)}
    [(umsatz, sh, konto, gegenkonto, bu)] = _datev(db)
    assert (umsatz, sh, konto, gegenkonto, bu) == \
           (f'{amount},00', 'S', str(expense), str(anlage), '0')


def test_datev_row_of_the_new_depreciation_is_expense_to_asset_account(world):
    _book(world.db, world.asset)
    assert _datev(world.db) == [('600,00', 'S', '6220', '520', '0')]


def test_old_depreciation_on_a_bank_account_counts_once_in_the_euer(world):
    """Alte Form auf dem Girokonto: früher fehlte sie in der EÜR, jetzt zählt sie (Sektion 3)."""
    db = world.db
    _old_depreciation(db, world.asset, world.giro)
    assert _euer(db) == {6220: Decimal('-600')}


def test_depreciation_hung_on_an_equal_bank_movement_still_counts_once(world):
    """Der Auto-Abgleich kann die AfA-Buchung (31.12.) an eine gleich hohe Bankbewegung vom 31.12. hängen
    (offene Aufgabe in docs/aufgaben.md). Der Zustand danach – die AfA als Kind der Bankbewegung – zählt
    in der EÜR weiter genau einmal: ohne Filter und mit dem Girokonto, nicht mit einem anderen."""
    db = world.db
    booking = db.book_depreciation(world.asset, 2026, None, world.expense, world.anlage)
    bank = db.insert_booking('2026-12-31', Decimal('-600'), account_id=world.giro, booking_type='bank')
    _execute(db, 'UPDATE Bookings SET ParentBooking_ID = ? WHERE ID = ?', (bank, booking))

    assert _euer(db) == {6220: Decimal('-600')}
    assert _euer(db, account_ids=[world.giro]) == {6220: Decimal('-600')}
    assert _euer(db, account_ids=[world.kasse]) == {}
    assert _plan_rows(db, world.asset) == [(2026, booking, 'posted')]


@pytest.mark.parametrize('with_skr', [True, False], ids=['kasse-mit-skr', 'kasse-ohne-skr'])
def test_old_depreciation_on_the_cash_account_is_no_cash_flow_and_counts_once(world, with_skr):
    """Alte AfA auf der Kasse: nicht im Dashboard (kein Geldfluss), in der EÜR genau einmal – nicht
    in Sektion 2b und 3 zugleich – und nur ohne Kontofilter (die AfA hängt an keinem Konto, auch nicht
    an der Kasse). Ein gewöhnlicher Kassensatz daneben zählt in beiden."""
    db = world.db
    if not with_skr:
        _execute(db, 'UPDATE Accounts SET SKRAccount = NULL WHERE IsCash = 1')
    _old_depreciation(db, world.asset, world.kasse)
    db.insert_booking('2026-12-30', Decimal('-10'), account_id=world.kasse, coa_id=_coa(db, 6815),
                      booking_type='entry')

    assert _euer(db) == {6220: Decimal('-600'), 6815: Decimal('-10')}
    assert _euer(db, account_ids=[world.giro, world.kasse]) == {6220: Decimal('-600'),
                                                                6815: Decimal('-10')}
    assert _euer(db, account_ids=[world.kasse]) == {6815: Decimal('-10')}      # ohne die AfA
    assert _euer(db, account_ids=[world.giro]) == {}
    totals = db.get_dashboard_totals(D1, D2)
    assert (totals['expense'], totals['bank_count']) == (-10.0, 1)
    december = db.get_dashboard_monthly(D1, D2)[11]
    assert (december['year_month'], december['expense']) == ('2026-12', -10.0)


# ── Routen ───────────────────────────────────────────────────────────────────

@pytest.fixture
def web(auto_login, server_port):
    """Echter Server (automatische Anmeldung), die Datenbank des Administrators und ein Laptop."""
    db = auto_login.db
    asset = db.insert_asset('Laptop Probe', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    return SimpleNamespace(
        db=db, asset=asset, port=server_port, csrf=auto_login.csrf,
        headers={'Origin': own_origin(server_port)}, expense=_coa(db, 6220), anlage=_coa(db, 520))


def _post(web, path, form=None):
    return post(web.port, path, headers=web.headers, csrf=web.csrf, form=form)


def _depreciate(web, **override):
    form = {'asset_id': web.asset, 'year': 2026, 'coa_id': web.expense, 'asset_coa_id': web.anlage}
    form.update(override)
    return _post(web, '/assets/depreciate', form)


def test_route_books_the_depreciation_and_redirects_to_the_asset(web):
    resp = _depreciate(web)

    assert resp.status == 303 and resp.headers['Location'] == f'/assets/view?id={web.asset}'
    (booking,) = _rows(web.db, 'SELECT * FROM Bookings')
    assert (booking['Account_ID'], booking['COA_ID'], booking['CounterCOA_ID']) == \
           (None, web.expense, web.anlage)
    assert from_minor(booking['Amount']) == Decimal('600')


def test_route_shows_the_refusal_on_the_asset_page(web):
    _depreciate(web)

    resp = _depreciate(web)

    assert resp.status == 303
    assert _error_of(resp.headers['Location']) == 'AfA 2026 ist bereits gebucht'
    page = get(web.port, resp.headers['Location'])
    assert page.status == 200
    assert app_msg_arguments(Dom(page.text)) == ['AfA 2026 ist bereits gebucht', 'error']
    assert _count(web.db, 'Bookings') == 1


def test_route_without_an_asset_account_shows_the_message(web):
    no_account = web.db.insert_asset('Ohne Konto', '2026-01-10', 1800.0, 3)

    resp = _depreciate(web, asset_id=no_account, asset_coa_id='')

    assert resp.status == 303
    page = get(web.port, resp.headers['Location'])
    assert app_msg_arguments(Dom(page.text)) == ['Anlage hat kein Anlagenkonto', 'error']
    assert _count(web.db, 'Bookings') == 0


def test_delete_route_refuses_while_depreciation_is_booked(web):
    _depreciate(web)

    resp = _post(web, f'/assets/delete?id={web.asset}')

    assert resp.status == 303
    assert resp.headers['Location'].startswith(f'/assets/view?id={web.asset}&err=')
    assert 'AfA gebucht (2026)' in _error_of(resp.headers['Location'])
    assert web.db.get_asset_by_id(web.asset) is not None and _count(web.db, 'Bookings') == 1
    page = get(web.port, resp.headers['Location'])
    assert 'AfA gebucht (2026)' in app_msg_arguments(Dom(page.text))[0]


def test_delete_route_refuses_extensions(web):
    web.db.insert_asset('Speicher Probe', '2026-02-01', 900.0, 3, parent_id=web.asset)

    resp = _post(web, f'/assets/delete?id={web.asset}')

    assert resp.status == 303 and 'Unteranlagen' in _error_of(resp.headers['Location'])
    assert web.db.get_asset_by_id(web.asset) is not None


def test_delete_route_deletes_an_asset_without_depreciation(web):
    resp = _post(web, f'/assets/delete?id={web.asset}')
    assert resp.status == 303 and resp.headers['Location'] == '/assets'
    assert web.db.get_asset_by_id(web.asset) is None


def test_delete_route_deletes_after_the_booking_was_deleted(web):
    _depreciate(web)
    booking = _rows(web.db, 'SELECT Booking_ID FROM AssetDepreciations')[0]['Booking_ID']
    assert _post(web, f'/transactions/delete?id={booking}').status == 303
    assert _plan_rows(web.db, web.asset) == [(2026, None, 'planned')]

    resp = _post(web, f'/assets/delete?id={web.asset}')

    assert resp.headers['Location'] == '/assets' and web.db.get_asset_by_id(web.asset) is None


def test_delete_route_is_not_reachable_by_get(web):
    resp = get(web.port, f'/assets/delete?id={web.asset}')
    assert resp.status == 405
    assert web.db.get_asset_by_id(web.asset) is not None


def test_message_in_the_address_cannot_close_the_script_block(web):
    """``err`` kommt aus der Adresse: Ein ``</script>`` darin ändert den Aufbau der Seite nicht,
    und die Meldung erreicht den Nutzer unverändert."""
    benign = get(web.port, f'/assets/view?id={web.asset}&err=Normale+Meldung')
    hostile = get(web.port, f'/assets/view?id={web.asset}&err={quote(PAYLOAD)}')

    assert hostile.status == 200
    assert_same_structure(hostile.text, benign.text)
    assert '</script><img' not in hostile.text and '<img src=x onerror=alert(1)>' not in hostile.text
    assert app_msg_arguments(Dom(hostile.text)) == [PAYLOAD, 'error']


def test_message_in_the_address_survives_exotic_characters(web):
    message = 'Fehler: <b>fett</b> & "zitiert" \'einfach\'   Umlaut äöü 日本\nzweite Zeile'
    page = get(web.port, f'/assets/view?id={web.asset}&err={quote(message)}')
    assert app_msg_arguments(Dom(page.text)) == [message, 'error']


def test_asset_page_without_a_message_has_no_message_call(web):
    html = get(web.port, f'/assets/view?id={web.asset}').text
    assert not [s for s in Dom(html).scripts if 'function(){appMsg(' in s]


# ── Anlagenliste ─────────────────────────────────────────────────────────────

THIS_YEAR = datetime.date.today().year


def _list_row(html, name):
    return next(row for row in html.split('<tr>') if f'>{name}<' in row)


def test_list_counts_posted_and_booked_years_as_booked_and_planned_years_as_open(tmp_db):
    """Der Status steht in der Spalte „Status“ der Planzeile (nicht in der Buchungs-Id): ``posted``
    (Buchung des Programms) und ``booked`` (WISO-Import) gelten als gebucht, ``planned`` nicht."""
    db = tmp_db
    posted = db.insert_asset('Anlage Gebucht', f'{THIS_YEAR}-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    booked = db.insert_asset('Anlage Wiso', f'{THIS_YEAR}-01-10', 2400.0, 3, coa_id=_coa(db, 500))
    db.insert_asset('Anlage Offen', f'{THIS_YEAR}-01-10', 3000.0, 3, coa_id=_coa(db, 500))
    _book(db, posted, THIS_YEAR)
    _wiso_booked(db, booked, THIS_YEAR)

    html = PageAssets(db)

    summary = re.search(rf'AfA {THIS_YEAR} gebucht: ([\d.,]+) € \| AfA {THIS_YEAR} geplant: ([\d.,]+) €',
                        html)
    # 600 + 600 gebucht – das WISO-Jahr zählt mit seinem gebuchten Betrag (600), nicht mit dem Planbetrag der
    # Anlage (800); 1000 geplant
    assert summary.groups() == ('1.200,00', '1.000,00')
    assert '✓ gebucht' in _list_row(html, 'Anlage Gebucht')
    assert '✓ gebucht' in _list_row(html, 'Anlage Wiso')
    assert '● offen' in _list_row(html, 'Anlage Offen')
    assert '✓ gebucht' not in _list_row(html, 'Anlage Offen')


def test_list_moves_the_year_back_to_open_after_the_booking_is_deleted(tmp_db):
    db = tmp_db
    asset = db.insert_asset('Anlage Gebucht', f'{THIS_YEAR}-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    _book(db, asset, THIS_YEAR)
    booking = _rows(db, 'SELECT Booking_ID FROM AssetDepreciations')[0]['Booking_ID']
    assert f'AfA {THIS_YEAR} gebucht: 600,00 €' in PageAssets(db)

    db.delete_transaction(booking)

    html = PageAssets(db)
    assert f'AfA {THIS_YEAR} gebucht: 0,00 €' in html and f'AfA {THIS_YEAR} geplant: 600,00 €' in html
    assert '● offen' in _list_row(html, 'Anlage Gebucht')


# ── AfA-Plan der Anlage ──────────────────────────────────────────────────────

def _plan_by_year(html):
    """{Jahr: HTML der Zeile} der Tabelle „AfA-Plan“."""
    block = html[html.index('<h3>AfA-Plan</h3>'):html.index('Erweiterungen / Nachkäufe')]
    parts = re.split(r'<tr(?: class="row-ok")?><td>(\d{4})</td>', block)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}


def _has_form(row, asset, year):
    return f'id="depr_form_{asset}_{year}"' in row


@pytest.fixture
def plan_asset(tmp_db):
    """Anlage mit vier Jahren AfA, die vor zwei Jahren beginnen: zwei vergangene, das laufende und
    ein künftiges Jahr (600 EUR je Jahr)."""
    db = tmp_db
    first = THIS_YEAR - 2
    asset = db.insert_asset('Plan Probe', f'{first}-01-10', 2400.0, 4, coa_id=_coa(db, 500))
    return SimpleNamespace(db=db, asset=asset, first=first)


def test_plan_marks_posted_and_booked_years_as_booked_and_offers_booking_for_the_others(plan_asset):
    db, asset, first = plan_asset.db, plan_asset.asset, plan_asset.first
    _book(db, asset, first)                                          # Buchung des Programms
    _wiso_booked(db, asset, first + 1)                               # WISO-Import: ohne Buchung
    booking = _rows(db, 'SELECT Booking_ID FROM AssetDepreciations WHERE Year = ?',
                    (first,))[0]['Booking_ID']

    plan = _plan_by_year(PageAssetView(db, asset))

    assert set(plan) == {first, first + 1, first + 2, first + 3}
    assert '✓ Gebucht' in plan[first] and not _has_form(plan[first], asset, first)
    assert f'<a href="/transactions/edit?id={booking}">Buchung ansehen</a>' in plan[first]
    assert '✓ Gebucht' in plan[first + 1] and not _has_form(plan[first + 1], asset, first + 1)
    assert 'Buchung ansehen' not in plan[first + 1]                  # keine Buchung, kein Link
    assert '● Laufendes Jahr' in plan[first + 2] and _has_form(plan[first + 2], asset, first + 2)
    assert '○ Geplant' in plan[first + 3] and 'AfA buchen' not in plan[first + 3]


def test_plan_offers_booking_again_after_the_booking_is_deleted(plan_asset):
    db, asset, first = plan_asset.db, plan_asset.asset, plan_asset.first
    _book(db, asset, first)
    assert 'AfA buchen' not in _plan_by_year(PageAssetView(db, asset))[first]
    booking = _rows(db, 'SELECT Booking_ID FROM AssetDepreciations')[0]['Booking_ID']

    db.delete_transaction(booking)

    row = _plan_by_year(PageAssetView(db, asset))[first]
    assert '⚠ Nicht gebucht' in row and _has_form(row, asset, first) and '✓ Gebucht' not in row
    assert _book(db, asset, first)[1] == f'/assets/view?id={asset}'      # und lässt sich buchen
    assert _plan_rows(db, asset)[0][2] == 'posted'


def test_plan_does_not_show_the_booking_link_of_a_deleted_booking(plan_asset):
    db, asset, first = plan_asset.db, plan_asset.asset, plan_asset.first
    _book(db, asset, first)
    booking = _rows(db, 'SELECT Booking_ID FROM AssetDepreciations')[0]['Booking_ID']
    db.delete_transaction(booking)
    assert f'/transactions/edit?id={booking}' not in PageAssetView(db, asset)
    assert '/assets/depreciation/view' not in PageAssetView(db, asset)       # alter, toter Link


def test_plan_offers_no_booking_for_years_after_the_disposal(plan_asset):
    db, asset, first = plan_asset.db, plan_asset.asset, plan_asset.first
    db.sell_asset(asset, f'{first + 1}-06-30', 100.0)                # Abgang im zweiten Planjahr

    html = PageAssetView(db, asset)
    plan = _plan_by_year(html)

    assert set(plan) == {first, first + 1}                           # der Plan endet mit dem Abgangsjahr
    for year in (first, first + 1):                                  # bis einschließlich Abgangsjahr
        assert _has_form(plan[year], asset, year), year
    for year in (first + 2, first + 3):                              # danach: keine Zeile, kein Formular
        assert f'depr_form_{asset}_{year}' not in html, year


@pytest.mark.parametrize('how', STALE)
def test_plan_offers_booking_for_a_posted_year_without_an_existing_booking(tmp_db, how):
    db = tmp_db
    asset = db.insert_asset('Plan Probe', f'{THIS_YEAR - 1}-01-10', 1800.0, 3, coa_id=_coa(db, 500))
    world = SimpleNamespace(asset=asset, expense=_coa(db, 6220), anlage=_coa(db, 500))
    booking = _stale_posted_row(db, world, THIS_YEAR - 1, how)

    row = _plan_by_year(PageAssetView(db, asset))[THIS_YEAR - 1]

    assert '⚠ Nicht gebucht' in row and _has_form(row, asset, THIS_YEAR - 1)
    assert '✓ Gebucht' not in row and 'Buchung ansehen' not in row
    if booking:
        assert f'/transactions/edit?id={booking}' not in row


@pytest.mark.parametrize('how', STALE)
def test_list_counts_a_posted_year_without_an_existing_booking_as_open(tmp_db, how):
    db = tmp_db
    asset = db.insert_asset('Anlage Gebucht', f'{THIS_YEAR}-01-10', 1800.0, 3, coa_id=_coa(db, 500))
    world = SimpleNamespace(asset=asset, expense=_coa(db, 6220), anlage=_coa(db, 500))
    _stale_posted_row(db, world, THIS_YEAR, how)

    html = PageAssets(db)

    assert f'AfA {THIS_YEAR} gebucht: 0,00 €' in html and f'AfA {THIS_YEAR} geplant: 600,00 €' in html
    assert '● offen' in _list_row(html, 'Anlage Gebucht')


# ── Formular „AfA buchen“ ────────────────────────────────────────────────────

def _depreciation_form(html, asset, year):
    """{Feldname: [(Kontonummer, vorbelegt)]} der beiden Auswahlfelder des Formulars sowie der
    Formularblock selbst."""
    block = re.search(rf'<div id="depr_form_{asset}_{year}".*?</form>', html, re.S).group(0)
    selects = {}
    for name, body in re.findall(r'<select name="(\w+)"[^>]*>(.*?)</select>', block, re.S):
        options = re.findall(r'<option value="(\d*)"( selected)?>([^<]*)</option>', body)
        selects[name] = [(label.split()[0] if value else '', bool(selected))
                         for value, selected, label in options]
    return block, selects


def _selected(options):
    return [number for number, selected in options if selected]


@pytest.mark.parametrize('name, price, years, anlage, expected_expense', [
    ('Laptop Probe', 1800.0, 3, 500, '6220'),             # Sachanlage
    ('Pkw Probe', 30000.0, 6, 520, '6222'),                # Fahrzeug
    ('Transporter Probe', 20000.0, 6, 560, '6222'),        # sonstiges Transportmittel
    ('Monitor Probe', 500.0, 3, 500, '6260'),              # GWG: Sofortabschreibung
], ids=['sachanlage', 'pkw', 'transportmittel', 'gwg'])
def test_form_preselects_asset_account_and_expense_account(tmp_db, name, price, years, anlage,
                                                           expected_expense):
    db = tmp_db
    asset = db.insert_asset(name, f'{THIS_YEAR}-01-10', price, years, coa_id=_coa(db, anlage))

    block, selects = _depreciation_form(PageAssetView(db, asset), asset, THIS_YEAR)

    assert _selected(selects['asset_coa_id']) == [str(anlage)]
    assert _selected(selects['coa_id']) == [expected_expense]
    assert 'name="account_id"' not in block and 'Buchungskonto' not in block     # kein Bankkonto
    assert 'required' in re.search(r'<select name="asset_coa_id"[^>]*>', block).group(0)
    assert 'required' in re.search(r'<select name="coa_id"[^>]*>', block).group(0)


def test_form_offers_only_asset_accounts_as_asset_account(tmp_db):
    db = tmp_db
    asset = db.insert_asset('Laptop Probe', f'{THIS_YEAR}-01-10', 1800.0, 3, coa_id=_coa(db, 500))

    _block, selects = _depreciation_form(PageAssetView(db, asset), asset, THIS_YEAR)

    numbers = [int(number) for number, _ in selects['asset_coa_id']]
    assert numbers and all(number < 1000 for number in numbers)
    expense_numbers = {int(number) for number, _ in selects['coa_id']}
    assert {6220, 6222, 6260} <= expense_numbers                   # das Aufwandskonto hat die volle Wahl


def test_form_without_an_asset_account_forces_a_choice(tmp_db):
    db = tmp_db
    asset = db.insert_asset('Ohne Konto', f'{THIS_YEAR}-01-10', 1800.0, 3)

    block, selects = _depreciation_form(PageAssetView(db, asset), asset, THIS_YEAR)

    assert _selected(selects['asset_coa_id']) == []
    assert selects['asset_coa_id'][0] == ('', False)               # „– bitte wählen –“
    assert '– bitte wählen –' in block
    assert _selected(selects['coa_id']) == ['6220']                 # das Aufwandskonto bleibt vorbelegt


def test_form_leaves_out_hidden_accounts_except_the_asset_account_itself(tmp_db):
    db = tmp_db
    asset = db.insert_asset('Laptop Probe', f'{THIS_YEAR}-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    for number in (520, 500, 6220):         # eigenes Anlagekonto, anderes Anlagekonto, Aufwandskonto
        assert db.toggle_coa_show_in_menu(_coa(db, number)) == 0          # jetzt ausgeblendet

    _block, selects = _depreciation_form(PageAssetView(db, asset), asset, THIS_YEAR)

    asset_numbers = [number for number, _ in selects['asset_coa_id']]
    assert '520' in asset_numbers and '500' not in asset_numbers
    assert _selected(selects['asset_coa_id']) == ['520']
    assert '6220' not in [number for number, _ in selects['coa_id']]
    assert _selected(selects['coa_id']) == ['6222']                 # Pkw-Konto 520 → Fahrzeug-AfA


def test_form_forces_a_choice_when_the_asset_account_is_not_in_the_list(tmp_db):
    """Steht ein Konto außerhalb der Anlagekonten (hier die Bank 1800) an der Anlage, darf die Auswahl
    nicht stillschweigend das erste Anlagekonto vorwählen: „– bitte wählen –“, nichts vorbelegt."""
    db = tmp_db
    asset = db.insert_asset('Falsches Konto', f'{THIS_YEAR}-01-10', 1800.0, 3, coa_id=_coa(db, 1800))

    block, selects = _depreciation_form(PageAssetView(db, asset), asset, THIS_YEAR)

    assert _selected(selects['asset_coa_id']) == []
    assert selects['asset_coa_id'][0] == ('', False) and '– bitte wählen –' in block
    assert 'required' in re.search(r'<select name="asset_coa_id"[^>]*>', block).group(0)
    assert all(int(number) < 1000 for number, _ in selects['asset_coa_id'][1:])
    assert _selected(selects['coa_id']) == ['6220']
