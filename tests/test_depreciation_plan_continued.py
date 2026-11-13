# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""AfA-Plan mit Fortschreibung (2.2.6): ``Database.depreciation_plan``.

Gebuchte Jahre sind Geschichte, der Restbuchwert verteilt sich neu. Eine Anlage, die nur in der App geführt
wurde (jedes gebuchte Jahr gleicht auf den Cent dem Jahr von ``calculate_depreciation_plan``), behält genau
den alten Plan – in jedem Zustand, auch beim Buchen außer der Reihe. Weicht die Geschichte ab (WISO-Import,
nachträglich geänderter Anschaffungswert oder Betrag einer Buchung), gelten die gebuchten Beträge, und der Rest
verteilt sich linear auf die Monate bis zum Planende; Jahre zwischen gebuchten haben keine eigene Zeile, und ein
Jahr, das auf 0 Cent rundet, entfällt. Der gebuchte Betrag ist, was in den Büchern steht (bei einem von der App
gebuchten Jahr die Wirkung der Buchung auf das Anlagekonto: Aufwand an Anlagekonto +|Betrag|, eine umgedrehte
Buchung negativ, das Jahr zählt dann mit 0), die Buchwerte der gebuchten Jahre rechnen vom heutigen Anschaffungswert,
und gebucht werden kann in der Fortschreibung nur das erste offene Jahr. ``book_depreciation`` bucht den Betrag aus diesem Plan und liest ihn erst nach der
Schreibsperre. Gerechnet wird in ganzen Cent.

Geprüft werden außerdem ``get_book_value_at_date`` und die beiden Seiten der Anlagen. Alle Namen und
Beträge sind erfunden.
"""
import datetime
import re
import sqlite3
import threading
import time
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace

import pytest

from db.assets import METHOD_BOOKED, _spread
from db.matching import depreciation_effect
from money import to_minor
from server.pages_assets import PageAssets, PageAssetView, _method_label

THIS_YEAR = datetime.date.today().year
OLD_KEYS = ('year', 'book_value_start', 'depreciation', 'book_value_end', 'method')


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _rows(db, sql, params=()):
    with closing(db._get_connection()) as conn:
        cursor = conn.execute(sql, params)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def _execute(db, sql, params=()):
    with closing(db._get_connection()) as conn:
        conn.execute(sql, params)
        conn.commit()


@pytest.fixture
def world(tmp_db):
    db = tmp_db
    return SimpleNamespace(db=db, expense=_coa(db, 6220), anlage=_coa(db, 520))


def _asset(world, date, price, years, method='linear', name='Plan Probe'):
    return world.db.insert_asset(name, date, price, years, coa_id=world.anlage, depreciation_method=method)


def _book(world, asset, year):
    return world.db.book_depreciation(asset, year, None, world.expense, world.anlage)


def _imported(db, asset, year, amount, book_value):
    """Planzeile, wie der WISO-Import sie für ein gebuchtes Jahr anlegt: Status ``booked``, keine Buchung."""
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Status) "
                 "VALUES (?, ?, ?, ?, 'booked')", (asset, year, to_minor(amount), to_minor(book_value)))


def _import_years(db, asset, first_year, amounts, price):
    """Aufeinanderfolgende Jahre als importiert gebucht; der Buchwert folgt dem Preis, den der Import kannte."""
    left = price
    for offset, amount in enumerate(amounts):
        left -= amount
        _imported(db, asset, first_year + offset, amount, left)


def _view(plan):
    return [(e['year'], e['depreciation'], e['booked']) for e in plan]


def _open(plan):
    return [e for e in plan if not e['booked']]


def _cents_sum(plan):
    return sum(to_minor(e['depreciation']) for e in plan)


def _old(db, asset):
    row = db.get_asset_by_id(asset)
    return db.calculate_depreciation_plan(row[7], row[6], row[8], row[9])


def _is_old_plan(plan, old, booked_years):
    """Genau die Jahre und Beträge des alten Plans, ``booked`` nach den gebuchten Jahren, jedes nicht gebuchte
    Jahr buchbar (in jeder Reihenfolge)."""
    return ([{key: entry[key] for key in OLD_KEYS} for entry in plan] == old
            and [entry['booked'] for entry in plan] == [entry['year'] in booked_years for entry in old]
            and [entry['bookable'] for entry in plan] == [entry['year'] not in booked_years for entry in old])


def _assert_consistent(plan, price):
    """Jahre aufsteigend, kein Betrag negativ, gebuchte Jahre rechnen vom Preis, offene ketten sich bis 0 und
    schließen an; buchbar ist nur das erste offene Jahr."""
    years = [e['year'] for e in plan]
    assert years == sorted(set(years))
    assert all(to_minor(e['depreciation']) >= 0 for e in plan)
    spent = 0
    for entry in plan:
        if entry['booked']:
            spent += to_minor(entry['depreciation'])
            assert to_minor(entry['book_value_end']) == max(0, to_minor(price) - spent)
            assert to_minor(entry['book_value_start']) == to_minor(entry['book_value_end']) + to_minor(entry['depreciation'])
    open_rows = _open(plan)
    for before, after in zip(open_rows, open_rows[1:]):
        assert after['book_value_start'] == before['book_value_end']
    for entry in open_rows:
        assert to_minor(entry['book_value_start']) - to_minor(entry['depreciation']) == to_minor(entry['book_value_end'])
    assert [e['year'] for e in plan if e['bookable']] == [e['year'] for e in open_rows[:1]]
    if open_rows:
        assert open_rows[-1]['book_value_end'] == 0.0
        assert _cents_sum(plan) == to_minor(price)
        booked = [e for e in plan if e['booked']]
        if booked:
            assert open_rows[0]['book_value_start'] == booked[-1]['book_value_end'] or to_minor(price) < spent


# ── Unverändert: der alte Plan, in jedem Zustand ─────────────────────────────

@pytest.mark.parametrize('method', ['linear', 'degressive'])
@pytest.mark.parametrize('price', [1234.56, 799.99, 1000.01, 9999.99, 45000.0])
def test_an_asset_kept_only_in_the_app_has_exactly_the_old_plan_in_every_state(world, method, price):
    """Kein, ein, alle Jahre über ``book_depreciation`` gebucht – das Ergebnis bleibt der alte Plan Zeile für
    Zeile (auch Preise, die sich nicht glatt teilen, Käufe im Januar, Juli und Dezember, GWG bis 800 €)."""
    db = world.db
    for month in (1, 7, 12):
        for years in (1, 3, 7):
            asset = _asset(world, f'2026-{month:02d}-15', price, years, method, f'Probe {month}-{years}')
            old = _old(db, asset)
            booked = set()
            assert _is_old_plan(db.depreciation_plan(asset), old, booked)
            for entry in old:
                _book(world, asset, entry['year'])
                booked.add(entry['year'])
                assert _is_old_plan(db.depreciation_plan(asset), old, booked), (month, years, entry['year'])


def test_booking_out_of_order_keeps_the_old_plan(world):
    db = world.db
    asset = _asset(world, '2026-03-15', 6000.0, 5)
    old = _old(db, asset)

    _book(world, asset, 2029)
    _book(world, asset, 2027)

    assert _is_old_plan(db.depreciation_plan(asset), old, {2027, 2029})


def test_an_asset_without_bookings_has_the_old_plan_with_no_year_booked(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    plan = db.depreciation_plan(asset)
    assert _view(plan) == [(2026, 600.0, False), (2027, 600.0, False), (2028, 600.0, False)]
    assert [e['method'] for e in plan] == ['linear'] * 3


def test_a_gwg_asset_stays_one_year_booked_or_not(world):
    db = world.db
    asset = _asset(world, '2026-03-15', 700.0, 4)
    assert _view(db.depreciation_plan(asset)) == [(2026, 700.0, False)]
    _book(world, asset, 2026)
    plan = db.depreciation_plan(asset)
    assert _view(plan) == [(2026, 700.0, True)] and plan[0]['method'] == 'GWG'


def test_a_booked_year_matching_the_old_plan_keeps_the_plan_even_if_the_stored_book_value_differs(world):
    """Verglichen wird der gebuchte Betrag; so geführte Anlagen behalten ihren Plan."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    _imported(db, asset, 2026, 600.0, 1.0)
    assert _is_old_plan(db.depreciation_plan(asset), _old(db, asset), {2026})


def test_a_posted_row_without_a_booking_is_not_a_booked_year(world):
    """Wie ``book_depreciation`` und die Seiten: ohne vorhandene Buchung gilt ein Jahr nicht als gebucht."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Status) "
                 "VALUES (?, 2026, 1, 1, 'posted')", (asset,))
    assert _view(db.depreciation_plan(asset)) == [(2026, 600.0, False), (2027, 600.0, False), (2028, 600.0, False)]


def test_every_asset_of_the_test_data_has_exactly_the_old_plan(db_with_coa):
    """Die Anlagen der Testdaten (verschiedene Preise, Daten und Nutzungsdauern; keine gebuchten Jahre)."""
    db = db_with_coa
    assets = db.fetch_assets(parent_only=False)
    assert len(assets) > 10
    for asset in assets:
        assert _is_old_plan(db.depreciation_plan(asset[0]), _old(db, asset[0]), set()), asset[1]


def test_an_unknown_asset_has_no_plan(world):
    assert world.db.depreciation_plan(99999) == []
    assert world.db.get_book_value_at_date(99999) == 0.0


# ── Das Beispiel des Owners und die Fortschreibung ───────────────────────────

def _owner_example(world):
    """Zehn Jahre, in fünf Jahren je 1.000 € gebucht (wie ein Import), der Anschaffungswert ist jetzt 15.000 €."""
    asset = _asset(world, '2020-01-10', 15000.0, 10, name='Anlage Zehn')
    _import_years(world.db, asset, 2020, [1000.0] * 5, 10000.0)
    return asset


def test_the_rest_book_value_spreads_over_the_remaining_years(world):
    db = world.db
    asset = _owner_example(world)

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(year, 1000.0, True) for year in range(2020, 2025)] + \
                          [(year, 2000.0, False) for year in range(2025, 2030)]
    assert _cents_sum(plan) == to_minor(15000.0)
    assert plan[-1]['book_value_end'] == 0.0
    open_rows = _open(plan)
    assert [(e['book_value_start'], e['book_value_end']) for e in open_rows] == [
        (10000.0, 8000.0), (8000.0, 6000.0), (6000.0, 4000.0), (4000.0, 2000.0), (2000.0, 0.0)]
    assert {e['method'] for e in open_rows} == {'linear'}


def test_booked_years_stand_with_their_booked_amount_and_a_book_value_from_the_current_price(world):
    """Die Buchwerte der gebuchten Jahre rechnen fortlaufend vom heutigen Anschaffungswert (15.000 €), nicht
    vom gespeicherten Buchwert der Planzeile (der Import kannte 10.000 €): So schließt das erste offene Jahr an."""
    db = world.db
    asset = _owner_example(world)

    plan = db.depreciation_plan(asset)
    booked = [e for e in plan if e['booked']]

    assert [(e['year'], e['book_value_start'], e['depreciation'], e['book_value_end'], e['method'])
            for e in booked] == [
        (2020, 15000.0, 1000.0, 14000.0, METHOD_BOOKED), (2021, 14000.0, 1000.0, 13000.0, METHOD_BOOKED),
        (2022, 13000.0, 1000.0, 12000.0, METHOD_BOOKED), (2023, 12000.0, 1000.0, 11000.0, METHOD_BOOKED),
        (2024, 11000.0, 1000.0, 10000.0, METHOD_BOOKED)]
    assert all(isinstance(e['depreciation'], float) and e['booked'] is True and e['bookable'] is False
               for e in booked)
    assert _open(plan)[0]['book_value_start'] == booked[-1]['book_value_end'] == 10000.0
    _assert_consistent(plan, 15000.0)


def test_a_july_purchase_over_six_years_has_seven_plan_years_the_last_one_partial(world):
    db = world.db
    asset = _asset(world, '2026-07-05', 6000.0, 6, name='Anlage Juli')

    plan = db.depreciation_plan(asset)

    assert [e['year'] for e in plan] == list(range(2026, 2033))
    assert [e['depreciation'] for e in plan] == [500.0] + [1000.0] * 5 + [500.0]       # erstes und letztes Jahr: 6 Monate
    assert _cents_sum(plan) == to_minor(6000.0)


def test_a_july_purchase_after_deviating_bookings_still_ends_with_the_six_months_of_the_last_year(world):
    db = world.db
    asset = _asset(world, '2026-07-05', 6000.0, 6, name='Anlage Juli')
    _import_years(db, asset, 2026, [450.0], 6000.0)

    plan = db.depreciation_plan(asset)

    assert [e['year'] for e in plan] == list(range(2026, 2033))
    open_rows = _open(plan)
    assert [e['year'] for e in open_rows] == list(range(2027, 2033))
    # 5.550 € auf 66 Monate: ein Jahr (12 Monate) 1.009,09 €, das letzte Jahr (6 Monate) ein halbes
    assert [e['depreciation'] for e in open_rows[:-1]] == [1009.09] * 5
    assert open_rows[-1]['depreciation'] == pytest.approx(504.55, abs=0.01)
    assert _cents_sum(plan) == to_minor(6000.0)
    _assert_consistent(plan, 6000.0)


@pytest.mark.parametrize('price', [10000.03, 1234.56, 5555.55, 9999.99])
@pytest.mark.parametrize('month', [1, 2, 7, 12])
@pytest.mark.parametrize('years', [2, 4, 6, 10])
def test_booked_plus_open_is_the_purchase_price_to_the_cent_and_no_year_is_negative(world, price, month, years):
    """Abweichende gebuchte Beträge (zwei Jahre, krumme Cent): Summe gebucht + offen = Anschaffungswert."""
    db = world.db
    asset = _asset(world, f'2020-{month:02d}-10', price, years)
    _import_years(db, asset, 2020, [123.45, 98.76], price)

    plan = db.depreciation_plan(asset)

    _assert_consistent(plan, price)
    assert [e['year'] for e in plan if e['booked']] == [2020, 2021]
    assert all(e['year'] > 2021 for e in _open(plan))


def test_raising_the_purchase_price_after_bookings_spreads_the_rest_from_the_next_open_year(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 5000.0, 5)
    _book(world, asset, 2026)
    _book(world, asset, 2027)

    row = db.get_asset_by_id(asset)
    db.update_asset(asset, row[2], row[6], 6500.0, row[8], coa_id=row[5], depreciation_method=row[9])
    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(2026, 1000.0, True), (2027, 1000.0, True),
                           (2028, 1500.0, False), (2029, 1500.0, False), (2030, 1500.0, False)]
    assert [e['method'] for e in plan] == [METHOD_BOOKED] * 2 + ['linear'] * 3
    assert _cents_sum(plan) == to_minor(6500.0)
    # die gebuchten Jahre rechnen vom neuen Preis, das erste offene Jahr schließt an (4.500 €)
    assert [(e['book_value_start'], e['book_value_end']) for e in plan[:2]] == [(6500.0, 5500.0), (5500.0, 4500.0)]
    assert plan[2]['book_value_start'] == 4500.0
    _assert_consistent(plan, 6500.0)
    assert db.get_book_value_at_date(asset, datetime.date(2027, 12, 31)) == 4500.0       # nach der Preisänderung


def test_lowering_the_price_to_the_booked_sum_or_below_leaves_no_open_year(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 5000.0, 5)
    _book(world, asset, 2026)
    _book(world, asset, 2027)
    row = db.get_asset_by_id(asset)

    for price in (2000.0, 1500.0):                                  # genau die gebuchte Summe, darunter
        db.update_asset(asset, row[2], row[6], price, row[8], coa_id=row[5], depreciation_method=row[9])
        plan = db.depreciation_plan(asset)
        assert _view(plan) == [(2026, 1000.0, True), (2027, 1000.0, True)], price
        assert plan[-1]['book_value_end'] == 0.0 and plan[0]['book_value_end'] == price - 1000.0, price
        assert [e['bookable'] for e in plan] == [False, False]


def test_an_expired_useful_life_puts_the_whole_rest_into_the_next_year(world):
    db = world.db
    asset = _asset(world, '2024-01-10', 1000.0, 2)
    _import_years(db, asset, 2024, [100.0, 100.0], 1000.0)                  # beide Planjahre sind um

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(2024, 100.0, True), (2025, 100.0, True), (2026, 800.0, False)]
    assert plan[-1]['book_value_start'] == 800.0 and plan[-1]['book_value_end'] == 0.0


def test_a_gap_between_booked_years_has_no_row_and_its_amount_stays_in_the_open_rest(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 5000.0, 5)
    _imported(db, asset, 2026, 900.0, 4100.0)
    _imported(db, asset, 2028, 1100.0, 3000.0)                              # 2027 fehlt

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(2026, 900.0, True), (2028, 1100.0, True), (2029, 1500.0, False), (2030, 1500.0, False)]
    assert _cents_sum(plan) == to_minor(5000.0)
    with pytest.raises(ValueError, match='No depreciation planned for year 2027'):
        _book(world, asset, 2027)


def test_in_the_continuation_only_the_first_open_year_can_be_booked(world):
    """Ein späteres Jahr zuerst zu buchen machte die Jahre davor zu Lücken (ihr Betrag wanderte in die späteren):
    Die Fortschreibung lehnt es ab, nichts wird geschrieben."""
    db = world.db
    asset = _owner_example(world)
    before = db.depreciation_plan(asset)

    for year in (2026, 2027, 2029):
        with pytest.raises(ValueError, match=f'^AfA {year} lässt sich erst buchen, wenn 2025 gebucht ist\\.$'):
            _book(world, asset, year)

    assert _rows(db, 'SELECT COUNT(*) AS n FROM Bookings')[0]['n'] == 0
    assert _rows(db, "SELECT COUNT(*) AS n FROM AssetDepreciations WHERE Status = 'posted'")[0]['n'] == 0
    assert db.depreciation_plan(asset) == before
    assert [e['year'] for e in before if e['bookable']] == [2025]
    _book(world, asset, 2025)                                              # das erste offene Jahr geht
    assert [e['year'] for e in db.depreciation_plan(asset) if e['bookable']] == [2026]
    with pytest.raises(ValueError, match='AfA 2027 lässt sich erst buchen, wenn 2026 gebucht ist'):
        _book(world, asset, 2027)


def test_the_year_order_is_free_for_an_asset_kept_only_in_the_app(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 5000.0, 5)

    _book(world, asset, 2029)
    _book(world, asset, 2027)
    _book(world, asset, 2026)

    assert [e['bookable'] for e in db.depreciation_plan(asset)] == [False, False, True, False, True]


def test_deleting_the_booking_of_an_earlier_year_in_the_continuation_leaves_a_gap(world):
    """Das gelöschte Jahr liegt vor dem größten gebuchten: Es ist wieder ungebucht, hat aber keine eigene Zeile
    mehr (sein Betrag steckt im Rest der folgenden Jahre). Bei einer nur in der App geführten Anlage bleibt das
    Jahr dagegen buchbar (alter Plan)."""
    db = world.db
    asset = _owner_example(world)
    first = _book(world, asset, 2025)
    _book(world, asset, 2026)

    db.delete_transaction(first)

    plan = db.depreciation_plan(asset)
    assert [(e['year'], e['depreciation'], e['booked']) for e in plan[5:]] == [
        (2026, 2000.0, True), (2027, 2666.67, False), (2028, 2666.67, False), (2029, 2666.66, False)]
    assert _cents_sum(plan) == to_minor(15000.0)
    with pytest.raises(ValueError, match='No depreciation planned for year 2025'):
        _book(world, asset, 2025)
    assert [e['year'] for e in plan if e['bookable']] == [2027]

    own = _asset(world, '2026-01-10', 1800.0, 3, name='Anlage Nur App')
    again = _book(world, own, 2026)
    _book(world, own, 2027)
    db.delete_transaction(again)
    assert _book(world, own, 2026)


def test_a_booked_year_after_the_plan_end_starts_the_rest_in_the_following_year(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 2000.0, 2)
    _imported(db, asset, 2030, 500.0, 1500.0)

    assert _view(db.depreciation_plan(asset)) == [(2030, 500.0, True), (2031, 1500.0, False)]


def test_the_first_open_year_is_at_least_the_year_of_purchase(world):
    """Gebuchte Jahre vor dem Kaufjahr (Altlast) verschieben den Anfang nicht vor den Kauf."""
    db = world.db
    asset = _asset(world, '2026-01-10', 3000.0, 3)
    _imported(db, asset, 2024, 100.0, 2900.0)

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(2024, 100.0, True), (2026, 966.67, False), (2027, 966.67, False), (2028, 966.66, False)]
    assert _cents_sum(plan) == to_minor(3000.0)


@pytest.mark.parametrize('field, value, start, end', [
    ('PurchasePrice', 0, 700.0, 0.0),            # ohne Preis ist nichts mehr da (nie unter 0)
    ('UsefulLifeYears', 0, 1800.0, 1100.0), ('PurchaseDate', 'kaputt', 1800.0, 1100.0),
    ('PurchaseDate', '', 1800.0, 1100.0),
], ids=['kein-preis', 'keine-nutzungsdauer', 'kein-datum-text', 'kein-datum-leer'])
def test_without_price_date_or_life_only_the_booked_years_show(world, field, value, start, end):
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    _imported(db, asset, 2026, 700.0, 1100.0)
    _execute(db, f'UPDATE Assets SET {field} = ? WHERE ID = ?', (value, asset))

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(2026, 700.0, True)]
    assert (plan[0]['book_value_start'], plan[0]['book_value_end'], plan[0]['method'], plan[0]['bookable']) == (
        start, end, METHOD_BOOKED, False)


def test_without_price_date_or_life_and_without_bookings_there_is_no_plan(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 0.0, 3)
    assert db.depreciation_plan(asset) == []
    with pytest.raises(ValueError, match='No depreciation planned for year 2026'):
        _book(world, asset, 2026)


def test_an_imported_asset_under_the_gwg_limit_spread_by_wiso_has_bookable_open_years(world):
    """700 € wären als GWG ein Jahr (2026); WISO hat die Anlage über vier Jahre verteilt, zwei Jahre sind
    gebucht. Die offenen Jahre sind buchbar – kein „No depreciation planned“."""
    db = world.db
    asset = _asset(world, '2024-03-15', 700.0, 4, name='Anlage Klein')
    assert _view(db.depreciation_plan(asset)) == [(2024, 700.0, False)]            # ohne Geschichte: GWG
    _import_years(db, asset, 2024, [140.0, 140.0], 700.0)

    plan = db.depreciation_plan(asset)

    assert [(e['year'], e['booked']) for e in plan] == [(2024, True), (2025, True), (2026, False), (2027, False), (2028, False)]
    assert [e['depreciation'] for e in _open(plan)] == [193.85, 193.85, 32.30]     # 420 € auf 12 + 12 + 2 Monate
    assert _cents_sum(plan) == to_minor(700.0)
    booking = _book(world, asset, 2026)
    assert booking and _rows(db, 'SELECT Amount FROM Bookings')[0]['Amount'] == to_minor(193.85)


def test_the_gwg_and_degressive_rules_do_not_apply_in_the_continuation(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 10000.0, 8, 'degressive', 'Anlage Degressiv')
    assert [(e['year'], e['depreciation'], e['method']) for e in db.depreciation_plan(asset)][:2] == [
        (2026, 2500.0, 'degressiv'), (2027, 1875.0, 'degressiv')]
    _imported(db, asset, 2026, 1000.0, 9000.0)                              # nicht der degressive Betrag (2.500 €)

    plan = db.depreciation_plan(asset)

    assert [(e['year'], e['method']) for e in plan] == [(2026, METHOD_BOOKED)] + [(y, 'linear') for y in range(2027, 2034)]
    assert [e['depreciation'] for e in plan[1:-1]] == [1285.71] * 6       # 9.000 € auf sieben Jahre, linear
    assert _cents_sum(plan) == to_minor(10000.0)


# ── Rundung: das letzte Jahr nimmt den Rest, nie negativ ─────────────────────

def test_spread_rounds_each_year_commercially_and_the_last_year_takes_the_rest():
    assert _spread(1000, [12, 12, 12]) == [333, 333, 334]
    assert _spread(1001, [12, 12, 12]) == [334, 334, 333]        # 333,67 rundet auf, der Rest ist kleiner
    assert _spread(5, [1, 1]) == [3, 2]                          # 2,5 rundet auf (halb auf)
    assert _spread(7, [12]) == [7]


@pytest.mark.parametrize('rest, months, expected', [
    (2, [6, 12, 12, 12, 6], [0, 1, 1, 0, 0]),       # 0, 1, 1, 1 ergäben 3 > 2: das letzte Jahr wäre −1
    (3, [1, 1, 1, 1, 2], [1, 1, 1, 0, 0]),           # viermal 0,5 rundet auf 4 > 3, der Überschuss reicht zwei Jahre zurück
])
def test_spread_caps_the_last_year_at_zero_and_takes_the_excess_from_the_years_before(rest, months, expected):
    result = _spread(rest, months)
    assert result == expected
    assert sum(result) == rest and min(result) >= 0


def test_spread_never_goes_negative_and_always_adds_up_to_the_rest():
    for rest in list(range(1, 60)) + [999, 1000, 100001, 3353567]:
        for months in ([12], [6, 12, 6], [6, 12, 12, 12, 6], [11, 12, 12, 12, 12, 1], [1, 1, 1, 1, 2], [3] * 9):
            result = _spread(rest, months)
            assert sum(result) == rest and min(result) >= 0, (rest, months, result)


def test_an_overshooting_rounding_in_a_real_plan_leaves_no_negative_year(world):
    """Zwei Cent Rest auf vier offene Jahre (12, 12, 12, 6 Monate): gerundet ergäbe 1, 1, 1 und −1. Der Überschuss
    mindert die Jahre davor (1, 1, 0, 0); die beiden Jahre mit 0 Cent entfallen."""
    db = world.db
    asset = _asset(world, '2024-07-10', 600.02, 4, name='Anlage Rest')
    _import_years(db, asset, 2024, [600.0], 600.02)

    open_rows = _open(db.depreciation_plan(asset))

    assert [e['year'] for e in open_rows] == [2025, 2026]
    assert [e['depreciation'] for e in open_rows] == [0.01, 0.01]
    assert sum(to_minor(e['depreciation']) for e in open_rows) == to_minor(0.02)


def test_a_year_that_rounds_to_zero_cents_drops_out_and_the_first_remaining_year_is_bookable(world):
    """1 Cent Rest auf vier offene Jahre (12, 12, 12, 2 Monate): nur das Jahr mit dem Rest bleibt – buchbar –, die
    Jahre davor entfallen und lassen sich nicht buchen; eine Buchung über 0,00 € gibt es nicht."""
    db = world.db
    asset = _asset(world, '2024-03-15', 1000.0, 4, name='Anlage Cent')
    _imported(db, asset, 2024, 999.99, 0.01)

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(2024, 999.99, True), (2028, 0.01, False)]
    assert [e['bookable'] for e in plan] == [False, True]
    assert (plan[1]['book_value_start'], plan[1]['book_value_end']) == (0.01, 0.0)
    for year in (2025, 2026, 2027):
        with pytest.raises(ValueError, match=f'^No depreciation planned for year {year}$'):
            _book(world, asset, year)
    assert _rows(db, 'SELECT COUNT(*) AS n FROM Bookings')[0]['n'] == 0
    booking = _book(world, asset, 2028)
    assert _rows(db, 'SELECT Amount FROM Bookings WHERE ID = ?', (booking,)) == [{'Amount': to_minor(0.01)}]
    assert _net_books(db, world.expense, world.anlage) == to_minor(0.01)


def test_the_old_plan_keeps_every_year_it_has(world):
    """Der Wegfall der 0-Cent-Jahre gilt der Fortschreibung; der alte Plan (Unverändert-Regel) bleibt, wie er ist."""
    db = world.db
    asset = _asset(world, '2024-03-15', 1000.0, 4)
    _book(world, asset, 2024)

    assert _is_old_plan(db.depreciation_plan(asset), _old(db, asset), {2024})


# ── Gebucht ist, was in den Büchern steht ────────────────────────────────────

def _net_books(db, expense, anlage):
    """Netto-AfA in den Büchern (Minor Units), nach Soll und Haben am Anlagekonto – nicht über die Planzeilen: Eine
    Abschreibung („Aufwand an Anlagekonto“) zählt +|Betrag|, eine Korrektur oder Rückbuchung (Anlagekonto im Soll)
    −|Betrag|. Form ``transfer``: Bei positivem Betrag steht das Konto im Soll, bei negativem das Gegenkonto."""
    total = 0
    for row in _rows(db, 'SELECT COA_ID, CounterCOA_ID, Amount FROM Bookings WHERE (COA_ID = ? AND CounterCOA_ID = ?) '
                         'OR (COA_ID = ? AND CounterCOA_ID = ?)', (expense, anlage, anlage, expense)):
        soll = row['COA_ID'] if row['Amount'] > 0 else row['CounterCOA_ID']
        total += abs(row['Amount']) if soll == expense else -abs(row['Amount'])
    return total


def test_a_year_booked_by_the_app_counts_with_the_amount_of_its_booking(world):
    """Ändert der Nutzer den Betrag der AfA-Buchung im Editor, weicht die Geschichte von der Formel ab: Der Rest
    verteilt sich neu, die Summe in den Büchern bleibt der Anschaffungswert."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)                                  # 600 € je Jahr
    first = _book(world, asset, 2026)
    assert _is_old_plan(db.depreciation_plan(asset), _old(db, asset), {2026})
    _execute(db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', (to_minor(500.0), first))       # im Editor geändert

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(2026, 500.0, True), (2027, 650.0, False), (2028, 650.0, False)]
    assert [e['method'] for e in plan] == [METHOD_BOOKED, 'linear', 'linear']
    _assert_consistent(plan, 1800.0)
    _book(world, asset, 2027)
    _book(world, asset, 2028)
    assert _net_books(db, world.expense, world.anlage) == to_minor(1800.0)


A = 60000000                       # 6.000 € in Minor Units


@pytest.mark.parametrize('form, amount, coa_number, counter_number, expected', [
    ('transfer', A, 6220, 520, A),            # Aufwand an Anlagekonto: Abschreibung
    ('transfer', A, 520, 6220, -A),           # vertauschte Konten (WISOs Umkehrung): Korrektur, Anlagekonto im Soll
    ('transfer', -A, 520, 6220, A),           # Kontentausch mit negiertem Betrag (reform): dieselbe Buchung wie die erste
    ('transfer', -A, 6220, 520, -A),          # nur der Betrag negiert: Anlagekonto im Soll
    ('transfer', A, 6220, 6222, A),           # kein Anlagekonto: wie bisher +|Betrag|
    ('transfer', A, 520, 560, A),             # beide Seiten Anlagekonten
    ('transfer', A, None, 520, A),            # ein Konto ohne Nummer
    ('transfer', A, 6220, None, A),
    (None, A, 6220, 520, A),                  # eine Zeile ohne Form gilt als Umbuchung
    ('purpose_first', -A, 6222, None, A),     # AfA alter Form (auf einem Bankkonto, ohne Gegenkonto): +|Betrag|
    ('purpose_first', A, 6220, 520, -A),      # Form „Zweckkonto vorne“: bei positivem Betrag steht das Gegenkonto im Soll
    ('liquid_first', A, 520, 6220, -A),
    ('transfer', 0, 6220, 520, 0),
], ids=['abschreibung', 'korrektur', 'reform', 'negiert', 'kein-anlagekonto', 'beide-anlagekonten', 'konto-ohne-nummer',
        'gegenkonto-ohne-nummer', 'ohne-form', 'alte-form', 'zweckkonto-vorne', 'geld-vorne', 'null'])
def test_the_effect_of_an_afa_row_on_the_asset_account(form, amount, coa_number, counter_number, expected):
    assert depreciation_effect(form, amount, coa_number, counter_number) == expected


def test_a_swap_of_the_accounts_with_a_negated_amount_changes_nothing(world):
    """``reform``: Konto und Gegenkonto getauscht, der Betrag negiert – im Soll und Haben dieselbe Buchung."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    first = _book(world, asset, 2026)
    _execute(db, 'UPDATE Bookings SET COA_ID = ?, CounterCOA_ID = ?, Amount = ? WHERE ID = ?',
             (world.anlage, world.expense, -to_minor(600.0), first))

    assert _is_old_plan(db.depreciation_plan(asset), _old(db, asset), {2026})


@pytest.mark.parametrize('how', ['konten-getauscht', 'betrag-negiert'])
def test_a_booking_turned_into_a_reversal_counts_as_zero_and_the_rest_is_spread_anew(world, how):
    """Dreht der Nutzer die AfA-Buchung zur Rückbuchung um (Anlagekonto im Soll), wirkt sie negativ: Das Jahr bleibt
    gebucht, mit 0, und der Rest verteilt sich neu."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    first = _book(world, asset, 2026)
    if how == 'konten-getauscht':
        _execute(db, 'UPDATE Bookings SET COA_ID = ?, CounterCOA_ID = ? WHERE ID = ?', (world.anlage, world.expense, first))
    else:
        _execute(db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', (-to_minor(600.0), first))

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(2026, 0.0, True), (2027, 900.0, False), (2028, 900.0, False)]
    assert [e['bookable'] for e in plan] == [False, True, False]
    assert plan[0]['book_value_end'] == 1800.0
    _assert_consistent(plan, 1800.0)


def test_a_booking_reversed_with_the_mask_changes_the_plan_like_the_swap(world):
    """Der Weg der Oberfläche (``update_booking_fields``: Konto und Gegenkonto getauscht) liefert dasselbe."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    first = _book(world, asset, 2026)

    db.update_booking_fields(first, coa_id=world.anlage, counter_coa_id=world.expense)

    assert _view(db.depreciation_plan(asset))[0] == (2026, 0.0, True)


def test_an_afa_row_of_the_old_shape_on_a_bank_account_still_counts_with_its_amount(world):
    """AfA vor 2.0.5: Aufwandskonto auf einem Bankkonto, kein Gegenkonto, Betrag negativ – kein Anlagekonto im Spiel."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    booking = db.insert_booking('2026-12-31', Decimal('-600'), account_id=db.fetch_accounts()[0][0],
                                coa_id=world.expense, booking_type='entry')
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Booking_ID, Status, "
                 "BookedAt) VALUES (?, 2026, ?, ?, ?, 'posted', CURRENT_TIMESTAMP)",
             (asset, to_minor(600.0), to_minor(1200.0), booking))

    assert _is_old_plan(db.depreciation_plan(asset), _old(db, asset), {2026})


def test_a_booking_of_the_app_decides_over_the_stored_amount_of_its_plan_row(world):
    """Die Planzeile hat ihren Betrag zur Zeit des Buchens gespeichert; maßgeblich ist die Buchung, auf die sie zeigt."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    first = _book(world, asset, 2026)
    _execute(db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', (to_minor(450.0), first))
    assert _rows(db, 'SELECT DepreciationAmount FROM AssetDepreciations')[0]['DepreciationAmount'] == to_minor(600.0)

    assert db.depreciation_plan(asset)[0]['depreciation'] == 450.0


def test_an_imported_year_counts_with_its_plan_row_and_the_app_year_with_its_booking_side_by_side(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 5000.0, 5)
    _imported(db, asset, 2026, 900.0, 4100.0)                                       # Import: die Planzeile zählt
    first = _book(world, asset, 2027)                                               # App: 1.025 € (Rest 4.100 auf 4 Jahre)
    _execute(db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', (to_minor(1000.0), first))

    plan = db.depreciation_plan(asset)

    assert _view(plan)[:2] == [(2026, 900.0, True), (2027, 1000.0, True)]
    assert _cents_sum(plan) == to_minor(5000.0)
    _assert_consistent(plan, 5000.0)


def test_the_stale_plan_row_without_a_booking_does_not_count_as_booked_with_its_amount(world):
    """Eine ``posted``-Zeile, deren Buchung fehlt, ist nicht gebucht – der Betrag der Zeile spielt keine Rolle."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    first = _book(world, asset, 2026)
    with closing(db._get_connection()) as conn:                  # wie die SQL-Konsole: ohne Fremdschlüsselprüfung
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute('DELETE FROM Bookings WHERE ID = ?', (first,))
        conn.commit()

    assert _view(db.depreciation_plan(asset)) == [(2026, 600.0, False), (2027, 600.0, False), (2028, 600.0, False)]


# ── Buchen in der Fortschreibung ─────────────────────────────────────────────

def test_booking_books_the_plan_amount_and_the_plan_keeps_its_years(world):
    db = world.db
    asset = _owner_example(world)
    before = db.depreciation_plan(asset)

    booking = _book(world, asset, 2025)

    assert _rows(db, 'SELECT Amount FROM Bookings WHERE ID = ?', (booking,)) == [{'Amount': to_minor(2000.0)}]
    row = _rows(db, 'SELECT * FROM AssetDepreciations WHERE Year = 2025')[0]
    assert (row['DepreciationAmount'], row['BookValue'], row['Booking_ID'], row['Status']) == (
        to_minor(2000.0), to_minor(8000.0), booking, 'posted')
    after = db.depreciation_plan(asset)
    assert [(e['year'], e['depreciation'], e['book_value_end']) for e in after if e['year'] >= 2025] == \
           [(e['year'], e['depreciation'], e['book_value_end']) for e in before if e['year'] >= 2025]
    assert [e['booked'] for e in after if e['year'] >= 2025] == [True, False, False, False, False]
    assert after[5]['method'] == METHOD_BOOKED


def test_booking_a_booked_year_of_the_continuation_is_refused_as_already_booked(world):
    db = world.db
    asset = _owner_example(world)
    with pytest.raises(ValueError, match='AfA 2022 ist bereits gebucht'):
        _book(world, asset, 2022)
    _book(world, asset, 2025)
    with pytest.raises(ValueError, match='AfA 2025 ist bereits gebucht'):
        _book(world, asset, 2025)
    assert _rows(db, 'SELECT COUNT(*) AS n FROM Bookings')[0]['n'] == 1


def test_a_year_outside_the_continuation_is_refused_with_the_known_message(world):
    asset = _owner_example(world)
    for year in (2019, 2030, 2099):
        with pytest.raises(ValueError, match=f'^No depreciation planned for year {year}$'):
            _book(world, asset, year)


def test_booking_after_the_disposal_year_stays_refused_in_the_continuation(world):
    db = world.db
    asset = _owner_example(world)
    db.sell_asset(asset, '2026-06-30', 4000.0)

    _book(world, asset, 2025)
    _book(world, asset, 2026)                                                # Abgangsjahr
    with pytest.raises(ValueError, match=r'^AfA 2027 liegt nach dem Abgang der Anlage \(2026-06-30\)$'):
        _book(world, asset, 2027)


def test_booking_the_first_open_year_leaves_the_other_open_years_as_they_were_up_to_the_rounding(world):
    """Ein Rest von 1.000,01 € (Kauf im Februar, drei Jahre): 480,00 / 480,00 / 40,01 €. Nach dem Buchen des
    ersten offenen Jahres rechnet der Plan den Rest neu und rundet anders: das zweite Jahr 480,01, das letzte
    40,00 € – die Summe bleibt, eine Abweichung von einem Cent."""
    db = world.db
    asset = _asset(world, '2026-02-10', 1500.01, 3, name='Anlage Rund')
    _import_years(db, asset, 2026, [500.0], 1500.01)
    assert [(e['year'], e['depreciation']) for e in _open(db.depreciation_plan(asset))] == [
        (2027, 480.0), (2028, 480.0), (2029, 40.01)]

    _book(world, asset, 2027)

    assert [(e['year'], e['depreciation']) for e in _open(db.depreciation_plan(asset))] == [
        (2028, 480.01), (2029, 40.0)]
    _assert_consistent(db.depreciation_plan(asset), 1500.01)


@pytest.mark.parametrize('years', [3, 5, 8])
@pytest.mark.parametrize('month', [1, 4, 11])
@pytest.mark.parametrize('price', [10000.03, 7777.77])
def test_booking_in_order_changes_the_other_open_years_by_at_most_the_rounding(world, price, month, years):
    """Gemessen: Ein mittleres Jahr ändert sich durch das Buchen eines Vorjahres um höchstens 1 Cent, das letzte
    Jahr (es nimmt den Rest) um höchstens so viele Cent, wie offene Jahre davor liegen – und jeder Zustand
    bleibt in sich stimmig (Summe = Anschaffungswert, kein Jahr negativ)."""
    db = world.db
    asset = _asset(world, f'2020-{month:02d}-10', price, years)
    _import_years(db, asset, 2020, [777.77], price)
    original = {e['year']: e['depreciation'] for e in _open(db.depreciation_plan(asset))}
    worst_middle = worst_last = 0

    while True:
        open_rows = _open(db.depreciation_plan(asset))
        if len(open_rows) < 2:
            break
        _book(world, asset, open_rows[0]['year'])
        plan = db.depreciation_plan(asset)
        _assert_consistent(plan, price)
        last = open_rows[-1]['year']
        for entry in _open(plan):
            drift = abs(to_minor(entry['depreciation']) - to_minor(next(
                e['depreciation'] for e in open_rows if e['year'] == entry['year']))) // 100
            if entry['year'] == last:
                worst_last = max(worst_last, drift)
                assert drift <= len(open_rows) - 1, (entry['year'], drift)
            else:
                worst_middle = max(worst_middle, drift)
                assert drift <= 1, (entry['year'], drift)
    assert worst_middle <= 1 and worst_last <= years
    assert set(original) >= {e['year'] for e in _open(db.depreciation_plan(asset))}


# ── Der Plan wird nach der Schreibsperre gelesen ─────────────────────────────

def _hold_the_lock_while(world, asset, year, writer):
    """Die erste Anfrage hält die Schreibsperre und schreibt (``writer``), die zweite bucht ``year`` und wartet.
    Liefert die Ausgänge der zweiten Anfrage."""
    db = world.db
    outcome = []

    def second_request():
        try:
            outcome.append(db.book_depreciation(asset, year, None, world.expense, world.anlage))
        except ValueError as exc:
            outcome.append(str(exc))

    with closing(sqlite3.connect(db.db_name, timeout=30)) as first_request:
        first_request.execute('PRAGMA foreign_keys = ON')
        first_request.execute('BEGIN IMMEDIATE')
        thread = threading.Thread(target=second_request)
        thread.start()
        time.sleep(1.0)                                    # die zweite hat den Plan gelesen, wenn sie ihn vorher liest
        writer(first_request)
        first_request.commit()
    thread.join(timeout=30)
    assert not thread.is_alive()
    return outcome


def test_the_amount_follows_a_change_that_arrives_while_the_booking_waits_for_the_lock(world):
    """Ändert eine andere Anfrage (oder ein Import) zwischen Lesen und Sperre den Anschaffungswert, kommt der
    Betrag aus dem Stand nach dem Commit. Vorher gelesen (2.000 €), wäre er veraltet – nachher sind es 1.400 €."""
    db = world.db
    asset = _owner_example(world)
    assert _open(db.depreciation_plan(asset))[0]['depreciation'] == 2000.0          # 2025

    def writer(conn):
        conn.execute('UPDATE Assets SET PurchasePrice = ? WHERE ID = ?', (to_minor(12000.0), asset))

    outcome = _hold_the_lock_while(world, asset, 2025, writer)

    assert len(outcome) == 1 and isinstance(outcome[0], int)
    # 12.000 − 5.000 = 7.000 € auf 60 Monate (2025 bis 2029)
    assert _rows(db, 'SELECT Amount FROM Bookings') == [{'Amount': to_minor(1400.0)}]
    assert _rows(db, 'SELECT DepreciationAmount, BookValue FROM AssetDepreciations WHERE Year = 2025') == [
        {'DepreciationAmount': to_minor(1400.0), 'BookValue': to_minor(5600.0)}]


def test_a_year_that_became_a_gap_while_the_booking_waited_is_refused(world):
    """Schreibt die erste Anfrage (ein Import) ein späteres Jahr als gebucht, ist das Jahr der zweiten eine Lücke
    ohne eigene Zeile: Sie wird nach der Sperre abgelehnt, statt mit einem Betrag aus dem alten Stand zu buchen."""
    db = world.db
    asset = _owner_example(world)

    def writer(conn):
        conn.execute("INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Status) "
                     "VALUES (?, 2028, ?, ?, 'booked')", (asset, to_minor(2000.0), to_minor(4000.0)))

    outcome = _hold_the_lock_while(world, asset, 2025, writer)

    assert outcome == ['No depreciation planned for year 2025']
    assert _rows(db, 'SELECT COUNT(*) AS n FROM Bookings')[0]['n'] == 0


# ── get_book_value_at_date ───────────────────────────────────────────────────

def test_the_book_value_at_a_date_comes_from_the_plan_of_the_continuation(world):
    db = world.db
    asset = _owner_example(world)

    def at(year, month=6):
        return db.get_book_value_at_date(asset, datetime.date(year, month, 1))

    assert at(2019) == 15000.0                      # vor dem ersten Planjahr: der Anschaffungswert
    assert at(2022) == 12000.0                      # gebuchtes Jahr: Anschaffungswert minus gebuchte Beträge
    assert at(2024) == 10000.0
    assert at(2025) == 8000.0                       # offene Jahre: vom Restbuchwert abwärts
    assert at(2027) == 4000.0
    assert at(2029) == 0.0 and at(2040) == 0.0


def test_the_book_value_of_an_asset_kept_only_in_the_app_is_unchanged(world):
    """Dieselbe Rechnung wie vor 2.2.6: Buchwert am Ende des letzten Planjahrs bis zum Jahr des Datums."""
    db = world.db
    asset = _asset(world, '2026-03-15', 1234.56, 4)
    _book(world, asset, 2026)
    old = _old(db, asset)

    for year in range(2025, 2032):
        expected = 1234.56
        for entry in old:
            if entry['year'] <= year:
                expected = entry['book_value_end']
        assert db.get_book_value_at_date(asset, datetime.date(year, 12, 31)) == max(0.0, expected), year


# ── Seiten ───────────────────────────────────────────────────────────────────

def _plan_by_year(html):
    """{Jahr: HTML der Zeile} der Tabelle „AfA-Plan“."""
    block = html[html.index('<h3>AfA-Plan</h3>'):html.index('Erweiterungen / Nachkäufe')]
    parts = re.split(r'<tr(?: class="row-ok")?><td>(\d{4})</td>', block)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}


def _page_asset(world):
    """Zehn Jahre ab fünf Jahren vor heute, fünf Jahre je 1.000 € als importiert gebucht, Preis jetzt 15.000 €."""
    first = THIS_YEAR - 5
    asset = _asset(world, f'{first}-01-10', 15000.0, 10, name='Anlage Seite')
    _import_years(world.db, asset, first, [1000.0] * 5, 10000.0)
    return asset, first


def test_the_asset_page_shows_the_booked_amount_for_booked_years_and_the_plan_for_the_rest(world):
    db = world.db
    asset, first = _page_asset(world)

    plan = _plan_by_year(PageAssetView(db, asset))

    assert set(plan) == set(range(first, first + 10))
    for year in range(first, THIS_YEAR):
        assert '1.000,00' in plan[year] and '✓ Gebucht' in plan[year] and 'Wie gebucht' in plan[year], year
        assert 'AfA buchen' not in plan[year]
    assert '2.000,00' in plan[THIS_YEAR] and 'Linear' in plan[THIS_YEAR]
    assert '● Laufendes Jahr' in plan[THIS_YEAR] and f'id="depr_form_{asset}_{THIS_YEAR}"' in plan[THIS_YEAR]
    assert '10.000,00' in plan[THIS_YEAR] and '8.000,00' in plan[THIS_YEAR]            # Buchwert Anfang und Ende
    assert '15.000,00' in plan[first] and '14.000,00' in plan[first]                   # gebuchtes Jahr: vom Preis
    assert '○ Geplant' in plan[THIS_YEAR + 1] and '2.000,00' in plan[THIS_YEAR + 1]
    assert '0,00' in plan[first + 9]


def test_the_asset_page_offers_the_booking_only_at_the_first_open_year_of_the_continuation(world):
    """Mehrere offene Jahre liegen in der Vergangenheit bzw. laufen: Die Seite bietet den Knopf nur am ersten an,
    die übrigen zeigen ihren Status ohne Knopf (vergangene weiter „Nicht gebucht“)."""
    db = world.db
    first = THIS_YEAR - 5
    asset = _asset(world, f'{first}-01-10', 15000.0, 10, name='Anlage Seite')
    _import_years(db, asset, first, [1000.0] * 2, 10000.0)                          # offen: ab THIS_YEAR - 3

    html = PageAssetView(db, asset)
    plan = _plan_by_year(html)

    assert html.count('AfA buchen</button>') == 1
    assert '⚠ Nicht gebucht' in plan[THIS_YEAR - 3] and f'id="depr_form_{asset}_{THIS_YEAR - 3}"' in plan[THIS_YEAR - 3]
    for year in (THIS_YEAR - 2, THIS_YEAR - 1):
        assert '⚠ Nicht gebucht' in plan[year] and 'AfA buchen' not in plan[year], year
    assert '● Laufendes Jahr' in plan[THIS_YEAR] and 'AfA buchen' not in plan[THIS_YEAR]
    assert '○ Geplant' in plan[THIS_YEAR + 1]
    _book(world, asset, THIS_YEAR - 3)                                              # nach dem Buchen: das nächste Jahr
    html = PageAssetView(db, asset)
    assert html.count('AfA buchen</button>') == 1
    assert f'id="depr_form_{asset}_{THIS_YEAR - 2}"' in _plan_by_year(html)[THIS_YEAR - 2]


def test_the_asset_page_offers_every_open_year_of_an_asset_kept_only_in_the_app(world):
    """Eine nur in der App geführte Anlage bleibt, wie sie war: jedes vergangene und das laufende Jahr buchbar."""
    db = world.db
    asset = _asset(world, f'{THIS_YEAR - 3}-01-10', 4000.0, 8, name='Anlage Nur App')

    html = PageAssetView(db, asset)

    assert html.count('AfA buchen</button>') == 4                                   # drei vergangene Jahre und das laufende


def test_the_asset_page_offers_no_booking_after_the_disposal_in_the_continuation(world):
    db = world.db
    asset, first = _page_asset(world)
    db.sell_asset(asset, f'{THIS_YEAR}-06-30', 3000.0)

    plan = _plan_by_year(PageAssetView(db, asset))

    assert 'AfA buchen' in plan[THIS_YEAR]
    for year in range(THIS_YEAR + 1, first + 10):
        assert '○ Nach Abgang' in plan[year] and 'AfA buchen' not in plan[year], year


def test_the_list_counts_the_open_year_of_the_continuation_with_the_plan_amount(world):
    db = world.db
    _page_asset(world)

    html = PageAssets(db)

    assert 'Anschaffungskosten (aktiv): 15.000,00 €' in html
    assert f'AfA {THIS_YEAR} gebucht: 0,00 € | AfA {THIS_YEAR} geplant: 2.000,00 €' in html
    assert 'Gesamtrestbuchwert: 8.000,00 €' in html
    assert '● offen' in next(row for row in html.split('<tr>') if '>Anlage Seite<' in row)


def test_the_list_counts_a_booked_current_year_with_its_booked_amount(world):
    """Das laufende Jahr ist importiert gebucht (700 €, der Plan der App hätte 600 €): Es zählt mit 700 €."""
    db = world.db
    asset = _asset(world, f'{THIS_YEAR}-01-10', 1800.0, 3)
    _imported(db, asset, THIS_YEAR, 700.0, 1100.0)

    html = PageAssets(db)

    assert f'AfA {THIS_YEAR} gebucht: 700,00 € | AfA {THIS_YEAR} geplant: 0,00 €' in html


def test_the_method_label_of_a_booked_year_is_a_label_and_other_values_stay_as_they_were():
    assert _method_label(METHOD_BOOKED) == 'Wie gebucht'
    assert (_method_label('linear'), _method_label('degressive'), _method_label('GWG')) == (
        'Linear', 'Degressiv', 'GWG (Sofort)')
    assert _method_label('degressiv') == 'degressiv'                    # unbekannte Werte erscheinen unverändert
