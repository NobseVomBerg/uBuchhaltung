# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Erweiterungen zählen zur Hauptanlage, und der Plan endet mit dem Abgang (2.2.7).

Nachträgliche Anschaffungskosten (Unter-Anlagen, ``Assets.Parent_ID``) erhöhen den Anschaffungswert der
Hauptanlage statt eine eigene Anlage zu sein: Im Jahr, in dem die Kosten entstehen (Anschaffungsdatum der
Erweiterung, nicht Zahlungsdatum), zählen sie wie zu Jahresbeginn aufgewendet – keine Kürzung nach Monaten –,
und der Restbuchwert samt Erweiterung verteilt sich linear nach Monaten auf die Restnutzungsdauer der
Hauptanlage. Fällt die Erweiterung ins Anschaffungsjahr, läuft alles ab dem Kaufmonat der Hauptanlage.
Eine Unter-Anlage mit eigener gebuchter AfA (Bestand) bleibt eine eigene Anlage.

Der Plan endet außerdem mit dem Abgangsjahr: Nicht gebuchte Jahre danach entfallen, gebuchte bleiben, und der
Buchwert ist am und nach dem Abgangsdatum 0. Alle Namen und Beträge sind erfunden.
"""
import datetime
import random
import re
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from db.assets import METHOD_BOOKED, _continue
from money import fmt_eur, to_minor
from server.handlers import handle_add_asset, handle_asset_sale, handle_book_depreciation, handle_update_asset
from server.pages_assets import EXTENSION_FORM_HINT, EXTENSION_HINT, PageAssetEdit, PageAssets, PageAssetView
from tests.dom_helpers import Dom, app_msg_arguments
from tests.http_client import get, own_origin, post

THIS_YEAR = datetime.date.today().year
OLD_KEYS = ('year', 'book_value_start', 'depreciation', 'book_value_end', 'method')
REFUSED_BOOKING = 'Die AfA einer Erweiterung läuft über die Hauptanlage – bitte dort buchen.'
NO_EXTENSION_OF_AN_EXTENSION = 'Eine Erweiterung kann keine eigene Erweiterung bekommen.'
NO_PARENT = 'Die Hauptanlage gibt es nicht.'
NO_OWN_DISPOSAL = 'Der Abgang einer Erweiterung lässt sich nicht einzeln buchen – sie gehört zur Hauptanlage.'
NO_OWN_STATUS = 'Eine Erweiterung behält den Status der Hauptanlage.'
EVIL_DATE = '2021-01-01"><img src=x onerror=1>'


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


def _asset(world, date, price, years, method='linear', name='Hauptanlage Probe'):
    return world.db.insert_asset(name, date, price, years, coa_id=world.anlage, depreciation_method=method)


def _extension(world, parent, date, price, name='Erweiterung Probe', years=1):
    """Eine Unter-Anlage; Nutzungsdauer und Methode der Hauptanlage gelten, ihre eigenen sind ohne Bedeutung –
    außer sie zählt nicht, dann hat sie ihren eigenen Plan."""
    return world.db.insert_asset(name, date, price, years, coa_id=world.anlage, parent_id=parent)


def _book(world, asset, year):
    return world.db.book_depreciation(asset, year, None, world.expense, world.anlage)


def _imported(db, asset, year, amount, book_value=0.0):
    """Planzeile, wie der WISO-Import sie für ein gebuchtes Jahr anlegt: Status ``booked``, keine Buchung."""
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Status) "
                 "VALUES (?, ?, ?, ?, 'booked')", (asset, year, to_minor(amount), to_minor(book_value)))


def _own_asset_under(world, parent, date='2025-03-01', price=1200.0, years=3):
    """Bestand: eine Unter-Anlage mit eigenem gebuchtem Jahr (das erste Jahr der Formel, importiert)."""
    db = world.db
    asset = db.insert_asset('Altbestand Probe', date, price, years, coa_id=world.anlage, parent_id=parent)
    first = db.calculate_depreciation_plan(price, date, years, 'linear')[0]
    _imported(db, asset, first['year'], first['depreciation'])
    return asset


def _view(plan):
    return [(e['year'], e['depreciation'], e['booked']) for e in plan]


def _amounts(plan):
    return [(e['year'], e['depreciation']) for e in plan]


def _cents_sum(plan):
    return sum(to_minor(e['depreciation']) for e in plan)


def _old_keys(plan):
    return [{key: entry[key] for key in OLD_KEYS} for entry in plan]


def _booking_amount(db, booking):
    return _rows(db, 'SELECT Amount FROM Bookings WHERE ID = ?', (booking,))[0]['Amount']


def _count(db, table):
    return _rows(db, f'SELECT COUNT(*) AS n FROM {table}')[0]['n']


# ── Das Beispiel des Owners ──────────────────────────────────────────────────

def test_an_extension_after_five_years_spreads_with_the_rest_over_the_remaining_five_years(world):
    """Zehn Jahre je 1.000 €, nach fünf Jahren +5.000 €: Fünf Jahre je 2.000 € – die Summe ist der Anschaffungswert
    samt Erweiterung."""
    db = world.db
    main = _asset(world, '2016-01-01', 10000.0, 10)
    for year in range(2016, 2021):
        _book(world, main, year)
    _extension(world, main, '2021-07-15', 5000.0)

    plan = db.depreciation_plan(main)

    assert _view(plan) == [(year, 1000.0, True) for year in range(2016, 2021)] + \
                          [(year, 2000.0, False) for year in range(2021, 2026)]
    assert _cents_sum(plan) == to_minor(15000.0)
    open_rows = [e for e in plan if not e['booked']]
    assert [(e['book_value_start'], e['book_value_end']) for e in open_rows] == [
        (10000.0, 8000.0), (8000.0, 6000.0), (6000.0, 4000.0), (4000.0, 2000.0), (2000.0, 0.0)]
    assert {e['method'] for e in open_rows} == {'linear'}
    # die Geschichte stimmt mit dem erwarteten Plan überein: Jedes nicht gebuchte Jahr ist buchbar
    assert [e['bookable'] for e in plan] == [False] * 5 + [True] * 5
    assert db.acquisition_cost(main) == 15000.0
    assert db.get_book_value_at_date(main, datetime.date(2021, 12, 31)) == 8000.0


@pytest.mark.parametrize('day', ['2021-01-01', '2021-01-02', '2021-06-30', '2021-07-15', '2021-12-30', '2021-12-31'])
def test_the_day_of_the_extension_within_the_year_does_not_matter(world, day):
    """Die Kosten zählen wie zu Jahresbeginn aufgewendet: keine Kürzung nach Monaten."""
    db = world.db
    reference = _asset(world, '2016-01-01', 10000.0, 10, name='Hauptanlage Vergleich')
    _extension(world, reference, '2021-01-02', 5000.0)
    asset = _asset(world, '2016-01-01', 10000.0, 10)
    _extension(world, asset, day, 5000.0)

    plan = db.depreciation_plan(asset)

    assert plan == db.depreciation_plan(reference)
    assert [e['depreciation'] for e in plan] == [1000.0] * 5 + [2000.0] * 5      # vor der Erweiterung ändert sich nichts
    assert _cents_sum(plan) == to_minor(15000.0)


@pytest.mark.parametrize('day', ['2024-11-01', '2024-03-10', '2024-01-02', '2023-12-01'],
                         ids=['nach-dem-kauf', 'am-kauftag', 'vor-dem-kauf-gleiches-jahr', 'vorjahr'])
def test_an_extension_in_the_year_of_purchase_depreciates_with_everything_from_the_purchase_month(world, day):
    """Ausnahme Anschaffungsjahr: Die Formel auf Anschaffungswert plus Erweiterung, ab dem Kaufmonat der
    Hauptanlage (10 Monate im ersten Jahr). Eine Erweiterung vor dem Kaufjahr zählt dort mit."""
    db = world.db
    main = _asset(world, '2024-03-10', 12000.0, 10)
    _extension(world, main, day, 6000.0)

    plan = db.depreciation_plan(main)

    assert _old_keys(plan) == db.calculate_depreciation_plan(18000.0, '2024-03-10', 10, 'linear')
    assert plan[0]['year'] == 2024 and plan[0]['depreciation'] == 1500.0           # 18.000 / 10 · 10/12
    assert [e['booked'] for e in plan] == [False] * len(plan) and all(e['bookable'] for e in plan)
    assert _cents_sum(plan) == to_minor(18000.0)


# ── Erweiterung in einem Jahr, das schon gebucht ist ─────────────────────────

def test_an_extension_added_for_a_booked_year_goes_into_the_rest_from_the_first_open_year(world):
    """Fünf Jahre sind gebucht; die Erweiterung trägt das Datum des dritten: Die gebuchten Jahre bleiben, der Rest
    samt Erweiterung verteilt sich ab dem ersten offenen Jahr (Fortschreibung)."""
    db = world.db
    main = _asset(world, '2020-01-10', 10000.0, 10)
    for year in range(2020, 2025):
        _book(world, main, year)
    _extension(world, main, '2022-03-01', 5000.0)

    plan = db.depreciation_plan(main)

    assert _view(plan) == [(year, 1000.0, True) for year in range(2020, 2025)] + \
                          [(year, 2000.0, False) for year in range(2025, 2030)]
    assert _cents_sum(plan) == to_minor(15000.0)
    assert [e['bookable'] for e in plan] == [False] * 5 + [True] + [False] * 4
    assert [e['method'] for e in plan] == [METHOD_BOOKED] * 5 + ['linear'] * 5
    # die gebuchten Jahre rechnen vom Anschaffungswert bis zu diesem Jahr: ab 2022 samt Erweiterung
    assert [(e['book_value_start'], e['book_value_end']) for e in plan[:5]] == [
        (10000.0, 9000.0), (9000.0, 8000.0), (13000.0, 12000.0), (12000.0, 11000.0), (11000.0, 10000.0)]
    assert plan[5]['book_value_start'] == 10000.0 and plan[-1]['book_value_end'] == 0.0

    with pytest.raises(ValueError, match=r'^AfA 2026 lässt sich erst buchen, wenn 2025 gebucht ist\.$'):
        _book(world, main, 2026)
    assert _booking_amount(db, _book(world, main, 2025)) == to_minor(2000.0)
    assert [e['year'] for e in db.depreciation_plan(main) if e['bookable']] == [2026]


def test_an_extension_in_the_year_of_purchase_after_that_year_was_booked_is_part_of_the_basis_of_the_booked_year(world):
    db = world.db
    main = _asset(world, '2024-03-10', 12000.0, 10)
    _book(world, main, 2024)                                          # 1.000 € (12.000 · 10/12 / 10)
    _extension(world, main, '2024-11-01', 6000.0)

    plan = db.depreciation_plan(main)

    assert plan[0]['booked'] and plan[0]['depreciation'] == 1000.0
    assert (plan[0]['book_value_start'], plan[0]['book_value_end']) == (18000.0, 17000.0)
    assert plan[1]['year'] == 2025 and plan[1]['bookable'] and not any(e['bookable'] for e in plan[2:])
    assert _cents_sum(plan) == to_minor(18000.0)


# ── Mehrere Erweiterungen ────────────────────────────────────────────────────

def test_two_extensions_in_later_years_add_up_to_the_cent_and_each_year_books_exactly_its_plan_amount(world):
    db = world.db
    main = _asset(world, '2020-01-10', 12000.0, 10)
    _extension(world, main, '2023-05-01', 3000.0, 'Erweiterung A')
    _extension(world, main, '2025-09-01', 2400.0, 'Erweiterung B')

    plan = db.depreciation_plan(main)

    assert _amounts(plan) == [(2020, 1200.0), (2021, 1200.0), (2022, 1200.0), (2023, 1628.57), (2024, 1628.57),
                              (2025, 2108.57), (2026, 2108.57), (2027, 2108.57), (2028, 2108.57), (2029, 2108.58)]
    assert _cents_sum(plan) == to_minor(17400.0)                      # 12.000 + 3.000 + 2.400
    assert plan[-1]['book_value_end'] == 0.0 and [e['method'] for e in plan[3:]] == ['linear'] * 7
    # jede Erweiterung kommt zum Buchwert Anfang ihres Jahres: 12.000 − 3.600 + 3.000 und 11.400 − 2 · 1.628,57 + 2.400
    assert (plan[3]['book_value_start'], plan[5]['book_value_start']) == (11400.0, 10542.86)

    # Jahr für Jahr buchen: Der Betrag der Buchung ist der Planbetrag, und der Plan bleibt, wie er ist
    for year in range(2020, 2025):
        planned = next(e['depreciation'] for e in db.depreciation_plan(main) if e['year'] == year)
        assert _booking_amount(db, _book(world, main, year)) == to_minor(planned), year
        assert _amounts(db.depreciation_plan(main)) == _amounts(plan), year
    assert [e['booked'] for e in db.depreciation_plan(main)] == [True] * 5 + [False] * 5


def test_an_extension_after_the_end_of_the_useful_life_stands_whole_in_its_year(world):
    db = world.db
    main = _asset(world, '2015-01-10', 3000.0, 3)
    for year in (2015, 2016, 2017):
        _book(world, main, year)
    _extension(world, main, '2022-04-01', 600.0)

    plan = db.depreciation_plan(main)

    assert _view(plan) == [(2015, 1000.0, True), (2016, 1000.0, True), (2017, 1000.0, True), (2022, 600.0, False)]
    assert (plan[-1]['book_value_start'], plan[-1]['book_value_end'], plan[-1]['bookable']) == (600.0, 0.0, True)
    assert _booking_amount(db, _book(world, main, 2022)) == to_minor(600.0)


def test_a_gwg_main_asset_keeps_its_purchase_year_and_depreciates_the_later_extension_over_the_remaining_months(world):
    db = world.db
    main = _asset(world, '2024-03-10', 500.0, 4)
    assert _view(db.depreciation_plan(main)) == [(2024, 500.0, False)]
    _extension(world, main, '2026-05-01', 1200.0)

    plan = db.depreciation_plan(main)

    # Plan von 2024-03 bis 2028-02: ab 2026 sind es 12 + 12 + 2 Monate
    assert [(e['year'], e['depreciation'], e['method']) for e in plan] == [
        (2024, 500.0, 'GWG'), (2026, 553.85, 'linear'), (2027, 553.85, 'linear'), (2028, 92.3, 'linear')]
    assert [(e['book_value_start'], e['book_value_end']) for e in plan[1:]] == [
        (1200.0, 646.15), (646.15, 92.3), (92.3, 0.0)]
    assert _cents_sum(plan) == to_minor(1700.0)


def test_a_degressive_main_asset_continues_linearly_from_the_first_later_extension(world):
    db = world.db
    main = _asset(world, '2024-01-10', 10000.0, 8, 'degressive')
    before = db.depreciation_plan(main)
    assert [(e['year'], e['depreciation'], e['method']) for e in before][:2] == [
        (2024, 2500.0, 'degressiv'), (2025, 1875.0, 'degressiv')]
    _extension(world, main, '2026-02-01', 3000.0)

    plan = db.depreciation_plan(main)

    assert plan[:2] == before[:2]                                     # davor gilt die Formel
    assert [(e['year'], e['depreciation'], e['method']) for e in plan[2:]] == [(year, 1437.5, 'linear')
                                                                               for year in range(2026, 2032)]
    assert plan[2]['book_value_start'] == 8625.0                      # 10.000 − 2.500 − 1.875 + 3.000
    assert _cents_sum(plan) == to_minor(13000.0)


# ── Die Erweiterung selbst ───────────────────────────────────────────────────

def test_an_extension_has_no_plan_of_its_own_cannot_be_booked_and_has_no_book_value(world):
    db = world.db
    main = _asset(world, '2024-03-10', 12000.0, 10)
    extension = _extension(world, main, '2025-04-01', 3000.0)

    assert db.counts_toward_parent(extension) is True
    assert db.counts_toward_parent(main) is False
    assert db.depreciation_plan(extension) == []
    assert db.get_book_value_at_date(extension) == 0.0
    assert db.get_book_value_at_date(extension, datetime.date(2030, 1, 1)) == 0.0
    for year in (2025, 2099):                                         # auch ein Jahr, das es nirgends gibt
        with pytest.raises(ValueError) as refusal:
            _book(world, extension, year)
        assert str(refusal.value) == REFUSED_BOOKING
    assert _count(db, 'Bookings') == 0 and _count(db, 'AssetDepreciations') == 0
    assert _cents_sum(db.depreciation_plan(main)) == to_minor(15000.0)  # ihr Betrag steckt im Plan der Hauptanlage


def test_the_refusal_to_book_an_extension_comes_after_asset_not_found(world):
    with pytest.raises(ValueError, match='Asset 99999 not found'):
        _book(world, 99999, 2026)


def test_the_handler_reports_the_refusal_to_book_an_extension_on_its_page(world):
    db = world.db
    main = _asset(world, '2024-03-10', 12000.0, 10)
    extension = _extension(world, main, '2025-04-01', 3000.0)

    status, location = handle_book_depreciation(
        db, {'asset_id': [str(extension)], 'year': ['2025'], 'coa_id': [str(world.expense)]})

    assert status == 303 and location.startswith(f'/assets/view?id={extension}&err=')
    assert parse_qs(urlsplit(location).query)['err'] == [REFUSED_BOOKING]
    assert _count(db, 'Bookings') == 0


def _cases_of_assets_that_do_not_count(world):
    """Fälle, in denen eine Unter-Anlage NICHT zur Hauptanlage zählt (Name -> Anlagen-ID)."""
    db = world.db
    main = _asset(world, '2024-03-10', 12000.0, 10)
    cases = {'hauptanlage': main}
    cases['preis-null'] = _extension(world, main, '2025-04-01', 0.0)
    cases['datum-kaputt'] = _extension(world, main, '2025-04-01', 100.0)
    _execute(db, "UPDATE Assets SET PurchaseDate = 'bald' WHERE ID = ?", (cases['datum-kaputt'],))
    cases['datum-leer'] = _extension(world, main, '2025-04-01', 100.0)
    _execute(db, "UPDATE Assets SET PurchaseDate = '' WHERE ID = ?", (cases['datum-leer'],))
    middle = _extension(world, main, '2025-05-01', 100.0, 'Mittlere Stufe')       # hat selbst eine Unter-Anlage
    _extension(world, middle, '2025-06-01', 50.0, 'Unterste Stufe')
    cases['mit-eigener-erweiterung'] = middle
    cases['selbstbezug'] = _asset(world, '2025-04-01', 100.0, 3, name='Selbstbezug')
    _execute(db, 'UPDATE Assets SET Parent_ID = ID WHERE ID = ?', (cases['selbstbezug'],))
    cases['hauptanlage-fehlt'] = _asset(world, '2025-04-01', 100.0, 3, name='Verwaist')
    with closing(db._get_connection()) as conn:                       # wie die SQL-Konsole: ohne Fremdschlüsselprüfung
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute('UPDATE Assets SET Parent_ID = 99999 WHERE ID = ?', (cases['hauptanlage-fehlt'],))
        conn.commit()
    return cases


@pytest.mark.parametrize('name', ['hauptanlage', 'preis-null', 'datum-kaputt', 'datum-leer', 'mit-eigener-erweiterung',
                                  'selbstbezug', 'hauptanlage-fehlt'])
def test_these_assets_do_not_count_toward_a_parent(world, name):
    db = world.db
    cases = _cases_of_assets_that_do_not_count(world)

    assert db.counts_toward_parent(cases[name]) is False
    assert db.acquisition_cost(cases['hauptanlage'], 2099) == 12000.0     # keine von ihnen erhöht die Hauptanlage
    assert db.counts_toward_parent(99999) is False                        # unbekannt


def test_an_asset_that_does_not_count_keeps_its_own_plan(world):
    db = world.db
    cases = _cases_of_assets_that_do_not_count(world)

    for name in ('selbstbezug', 'hauptanlage-fehlt'):
        assert _view(db.depreciation_plan(cases[name])) == [(2025, 100.0, False)], name       # GWG
        _book(world, cases[name], 2025)
        assert _view(db.depreciation_plan(cases[name])) == [(2025, 100.0, True)], name
    # die mittlere Stufe hat eine zählende Unter-Anlage: 100 + 50 im Jahr 2025
    middle = cases['mit-eigener-erweiterung']
    assert _view(db.depreciation_plan(middle)) == [(2025, 150.0, False)]
    lowest = db.get_asset_children(middle)[0][0]
    assert db.counts_toward_parent(lowest) is True and db.depreciation_plan(lowest) == []
    assert db.acquisition_cost(middle, 2099) == 150.0
    for name in ('preis-null', 'datum-kaputt', 'datum-leer'):             # ohne Preis oder Datum gab es nie einen Plan
        assert db.depreciation_plan(cases[name]) == [], name


def test_an_extension_with_its_own_booked_year_stays_an_asset_of_its_own(world):
    """Bestand (Import, früher gebuchte AfA): Die Unter-Anlage behält ihren Plan und fließt nicht in die Hauptanlage."""
    db = world.db
    main = _asset(world, '2024-01-10', 6000.0, 6)
    before = db.depreciation_plan(main)
    own = _own_asset_under(world, main)

    assert db.counts_toward_parent(own) is False
    plan = db.depreciation_plan(own)
    assert [e['year'] for e in plan] == [2025, 2026, 2027, 2028] and plan[0]['booked']
    assert _cents_sum(plan) == to_minor(1200.0)
    assert db.depreciation_plan(main) == before                       # wie ohne die Unter-Anlage
    assert db.acquisition_cost(main, 2099) == 6000.0
    assert db.get_book_value_at_date(own, datetime.date(2025, 12, 31)) == plan[0]['book_value_end'] == 866.67
    assert _booking_amount(db, _book(world, own, 2026)) == to_minor(plan[1]['depreciation'])


def test_an_extension_booked_before_it_became_one_keeps_its_own_booking_path(world):
    db = world.db
    main = _asset(world, '2024-01-10', 6000.0, 6)
    before = db.depreciation_plan(main)
    own = _asset(world, '2025-03-01', 1200.0, 3, name='Altbestand Probe')
    _book(world, own, 2025)
    _execute(db, 'UPDATE Assets SET Parent_ID = ? WHERE ID = ?', (main, own))

    assert db.counts_toward_parent(own) is False
    assert _booking_amount(db, _book(world, own, 2026)) == to_minor(400.0)
    assert db.depreciation_plan(main) == before and db.acquisition_cost(main, 2099) == 6000.0


@pytest.mark.parametrize('status, booked', [('planned', False), ('posted', False), ('booked', True)])
def test_any_row_of_its_own_in_the_plan_keeps_a_sub_asset_an_asset_of_its_own(world, status, booked):
    """Zählt nur, wer gar keine eigene AfA-Zeile hat: Eine ``planned``-Zeile (die einzige Buchung gelöscht), eine
    ``posted``-Zeile ohne Buchung und eine importierte ``booked``-Zeile halten die Unter-Anlage gleichermaßen
    heraus – sie behält ihren eigenen Plan, ihr Betrag wandert nicht in die Zukunft der Hauptanlage."""
    db = world.db
    main = _asset(world, '2020-01-10', 6000.0, 6)
    before = db.depreciation_plan(main)
    extension = _extension(world, main, '2022-04-01', 1500.0, years=5)
    assert db.counts_toward_parent(extension) is True and db.depreciation_plan(main) != before

    _execute(db, 'INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, Status) '
                 'VALUES (?, 2022, ?, ?, ?)', (extension, to_minor(50.0), to_minor(1450.0), status))

    assert db.counts_toward_parent(extension) is False
    assert db.depreciation_plan(main) == before and db.acquisition_cost(main, 2099) == 6000.0
    plan = db.depreciation_plan(extension)
    assert plan[0]['year'] == 2022 and plan[0]['booked'] is booked
    if not booked:
        assert _old_keys(plan) == db.calculate_depreciation_plan(1500.0, '2022-04-01', 5, 'linear')


def test_deleting_the_only_booking_of_a_sub_asset_leaves_a_planned_row_and_the_asset_of_its_own(world):
    """Bestand: eine Unter-Anlage mit eigener AfA-Buchung (erst gebucht, dann unter die Hauptanlage gehängt). Löscht
    der Nutzer die Buchung über die App, bleibt ihre Planzeile als ``planned`` stehen: Sie bleibt eine eigene Anlage,
    ihr Jahr lässt sich neu buchen, und der Plan der Hauptanlage ändert sich nicht."""
    db = world.db
    main = _asset(world, '2024-01-10', 6000.0, 6)
    before = db.depreciation_plan(main)
    own = _asset(world, '2025-03-01', 1200.0, 3, name='Altbestand Probe')
    booking = _book(world, own, 2025)
    _execute(db, 'UPDATE Assets SET Parent_ID = ? WHERE ID = ?', (main, own))
    assert db.counts_toward_parent(own) is False

    db.delete_transaction(booking)

    assert _rows(db, 'SELECT Year, Booking_ID, Status FROM AssetDepreciations WHERE Asset_ID = ?', (own,)) == [
        {'Year': 2025, 'Booking_ID': None, 'Status': 'planned'}]               # die Planzeile bleibt stehen
    assert db.counts_toward_parent(own) is False
    plan = db.depreciation_plan(own)
    assert _old_keys(plan) == db.calculate_depreciation_plan(1200.0, '2025-03-01', 3, 'linear')
    assert not any(e['booked'] for e in plan) and all(e['bookable'] for e in plan)      # jedes Jahr wieder buchbar
    assert db.depreciation_plan(main) == before and db.acquisition_cost(main, 2099) == 6000.0
    assert _booking_amount(db, _book(world, own, 2025)) == to_minor(plan[0]['depreciation'])


def test_a_sub_asset_counts_again_when_its_plan_rows_are_gone(world):
    db = world.db
    main = _asset(world, '2020-01-10', 6000.0, 6)
    extension = _extension(world, main, '2022-04-01', 1200.0)
    _imported(db, extension, 2022, 100.0)
    assert db.counts_toward_parent(extension) is False and db.acquisition_cost(main, 2099) == 6000.0

    _execute(db, 'DELETE FROM AssetDepreciations WHERE Asset_ID = ?', (extension,))

    assert db.counts_toward_parent(extension) is True and db.acquisition_cost(main, 2099) == 7200.0
    assert _cents_sum(db.depreciation_plan(main)) == to_minor(7200.0)


# ── Die Hauptanlage muss einen Plan haben können ─────────────────────────────────────────────────────

@pytest.mark.parametrize('how', ['preis-null', 'preis-unter-einem-cent', 'datum-kaputt', 'datum-leer',
                                 'nutzungsdauer-null'])
def test_a_sub_asset_of_a_main_asset_that_cannot_have_a_plan_stays_an_asset_of_its_own(world, how):
    """Ohne lesbares Kaufdatum, Preis über 0 Cent und Nutzungsdauer über 0 hat die Hauptanlage keinen Plan, an den sich
    eine Erweiterung hängen ließe: Die Unter-Anlage behält ihren eigenen Plan (wie bis 2.2.6) und lässt sich buchen."""
    db = world.db
    price = {'preis-null': 0.0, 'preis-unter-einem-cent': 0.004}.get(how, 12000.0)
    main = _asset(world, '2024-03-10', price, 10)
    if how == 'datum-kaputt':
        _execute(db, "UPDATE Assets SET PurchaseDate = 'bald' WHERE ID = ?", (main,))
    elif how == 'datum-leer':
        _execute(db, "UPDATE Assets SET PurchaseDate = '' WHERE ID = ?", (main,))
    elif how == 'nutzungsdauer-null':
        _execute(db, 'UPDATE Assets SET UsefulLifeYears = 0 WHERE ID = ?', (main,))
    extension = _extension(world, main, '2025-04-01', 5000.0, years=5)

    assert db.counts_toward_parent(extension) is False
    plan = db.depreciation_plan(extension)
    assert plan and _old_keys(plan) == db.calculate_depreciation_plan(5000.0, '2025-04-01', 5, 'linear')
    assert db.acquisition_cost(main, 2099) == (0.0 if price < 1 else 12000.0)         # nur ihr eigener Preis
    assert db.acquisition_cost(extension) == 5000.0
    assert db.get_book_value_at_date(extension, datetime.date(2025, 12, 31)) == 4250.0   # ein eigener Wert, nicht 0
    assert _booking_amount(db, _book(world, extension, 2025)) == to_minor(750.0)        # 5.000 / 5 · 9/12


def test_a_main_asset_without_a_price_has_no_plan_and_takes_nothing_from_its_sub_asset(world):
    db = world.db
    main = _asset(world, '2024-03-10', 0.0, 10)
    _extension(world, main, '2025-04-01', 5000.0, years=5)

    assert db.depreciation_plan(main) == [] and db.acquisition_cost(main, 2099) == 0.0


def test_the_same_sub_asset_counts_under_a_main_asset_that_has_a_plan(world):
    db = world.db
    main = _asset(world, '2024-03-10', 12000.0, 10)
    extension = _extension(world, main, '2025-04-01', 5000.0, years=5)

    assert db.counts_toward_parent(extension) is True and db.depreciation_plan(extension) == []
    assert db.acquisition_cost(main, 2099) == 17000.0


# ── Anschaffungswert ─────────────────────────────────────────────────────────

def test_the_acquisition_cost_counts_the_extensions_up_to_the_year(world):
    db = world.db
    main = _asset(world, '2020-01-10', 5000.0, 10)
    _extension(world, main, '2021-03-01', 1000.0)
    _extension(world, main, '2023-06-01', 500.0)
    _extension(world, main, '2019-12-01', 200.0)                      # vor dem Kauf: zählt im Kaufjahr
    own = _own_asset_under(world, main, '2022-01-01', 99.0)           # Bestand: eine eigene Anlage, nicht die Hauptanlage

    assert [db.acquisition_cost(main, year) for year in (2019, 2020, 2021, 2022, 2023, 2024)] == [
        5000.0, 5200.0, 6200.0, 6200.0, 6700.0, 6700.0]
    assert db.acquisition_cost(main) == 6700.0                        # das laufende Jahr liegt nach allen Erweiterungen
    assert db.acquisition_cost(own, 2099) == 99.0                     # eine eigene Anlage: ihr Preis
    assert all(isinstance(db.acquisition_cost(main, year), float) for year in (2020, 2023))
    assert db.acquisition_cost(99999) == 0.0


def test_the_acquisition_cost_of_an_extension_is_its_own_price_and_defaults_to_the_current_year(world):
    db = world.db
    main = _asset(world, f'{THIS_YEAR - 1}-01-10', 1234.56, 5)
    now = _extension(world, main, f'{THIS_YEAR}-02-01', 0.01)
    _extension(world, main, f'{THIS_YEAR + 1}-02-01', 800.0)

    assert db.acquisition_cost(now) == 0.01
    assert db.acquisition_cost(main) == 1234.57                       # ohne Angabe: bis zum laufenden Jahr
    assert db.acquisition_cost(main, THIS_YEAR + 1) == 2034.57


# ── Löschen, Abgang und Status einer Erweiterung ─────────────────────────────

DELETE_REFUSAL = ('Die Hauptanlage hat seit dem Jahr dieser Erweiterung AfA gebucht ({years}) – die Erweiterung '
                  'steckt in diesen Buchungen und lässt sich nicht löschen.')


def _booked_main(world, extension_date, booked_years):
    """Hauptanlage von 2020 (zehn Jahre) mit einer Erweiterung; ``booked_years`` sind über ``book_depreciation`` gebucht."""
    main = _asset(world, '2020-01-10', 12000.0, 10)
    extension = _extension(world, main, extension_date, 3000.0)
    for year in booked_years:
        _book(world, main, year)
    return main, extension


def test_an_extension_cannot_be_deleted_once_the_main_asset_has_booked_since_its_year(world):
    db = world.db
    main, extension = _booked_main(world, '2022-05-01', (2020, 2021, 2022, 2023))
    before = db.depreciation_plan(main)

    with pytest.raises(ValueError) as refusal:
        db.delete_asset(extension)

    assert str(refusal.value) == DELETE_REFUSAL.format(years='2022, 2023')      # die Jahre seit der Erweiterung
    assert db.get_asset_by_id(extension) is not None and db.depreciation_plan(main) == before


def test_an_extension_can_be_deleted_while_the_main_asset_booked_only_before_its_year(world):
    db = world.db
    main, extension = _booked_main(world, '2023-05-01', (2020, 2021, 2022))

    db.delete_asset(extension)

    assert db.get_asset_by_id(extension) is None
    plan = db.depreciation_plan(main)
    assert _amounts(plan) == [(year, 1200.0) for year in range(2020, 2030)]     # wieder die Formel ohne Erweiterung
    assert [e['booked'] for e in plan] == [True] * 3 + [False] * 7
    assert db.acquisition_cost(main, 2099) == 12000.0


def test_an_extension_can_be_deleted_while_the_main_asset_has_booked_nothing(world):
    db = world.db
    main, extension = _booked_main(world, '2022-05-01', ())

    db.delete_asset(extension)

    assert db.get_asset_by_id(extension) is None and db.depreciation_plan(main)[0]['depreciation'] == 1200.0


def test_the_year_of_an_extension_before_the_purchase_year_is_the_purchase_year_when_deleting(world):
    """Das Jahr der Erweiterung ist nie früher als das Kaufjahr der Hauptanlage – wie bei ihrem Betrag im Plan: Eine
    Erweiterung vom Vorjahr des Kaufs zählt im Kaufjahr, also hindert das gebuchte Kaufjahr das Löschen, ein
    importiertes Jahr vor dem Kauf (Altlast) aber nicht."""
    db = world.db
    main = _asset(world, '2024-03-10', 12000.0, 10)
    extension = _extension(world, main, '2023-12-01', 3000.0)
    _book(world, main, 2024)

    with pytest.raises(ValueError) as refusal:
        db.delete_asset(extension)
    assert str(refusal.value) == DELETE_REFUSAL.format(years='2024')

    later = _extension(world, main, '2025-06-01', 100.0, 'Spätere Erweiterung')      # nach dem einzigen gebuchten Jahr
    db.delete_asset(later)
    assert db.get_asset_by_id(later) is None and db.get_asset_by_id(extension) is not None

    other = _asset(world, '2026-01-10', 12000.0, 10, name='Hauptanlage mit Altlast')
    before_purchase = _extension(world, other, '2025-06-01', 3000.0, 'Erweiterung vor dem Kauf')
    _imported(db, other, 2025, 500.0)                                 # ein gebuchtes Jahr vor dem Kaufjahr 2026
    db.delete_asset(before_purchase)
    assert db.get_asset_by_id(before_purchase) is None


def test_a_year_of_the_main_asset_imported_as_booked_counts_for_the_refusal_a_planned_one_does_not(world):
    db = world.db
    main = _asset(world, '2024-01-10', 6000.0, 6)
    extension = _extension(world, main, '2025-06-01', 600.0)
    _imported(db, main, 2026, 1000.0)

    with pytest.raises(ValueError) as refusal:
        db.delete_asset(extension)
    assert str(refusal.value) == DELETE_REFUSAL.format(years='2026')

    _execute(db, "UPDATE AssetDepreciations SET Status = 'planned' WHERE Asset_ID = ?", (main,))
    db.delete_asset(extension)
    assert db.get_asset_by_id(extension) is None


def test_a_sub_asset_with_a_plan_row_of_its_own_is_not_covered_by_the_refusal(world):
    """Eine Unter-Anlage mit eigener AfA-Zeile ist eine eigene Anlage: Für sie gilt nur die alte Regel."""
    db = world.db
    main = _asset(world, '2020-01-10', 12000.0, 10)
    for year in (2020, 2021, 2022):
        _book(world, main, year)
    own = _own_asset_under(world, main, '2021-03-01', 1200.0)

    db.delete_asset(own)

    assert db.get_asset_by_id(own) is None and db.get_asset_by_id(main) is not None


def test_the_disposal_of_an_extension_is_refused_and_nothing_is_written(world):
    db = world.db
    main = _asset(world, '2026-01-10', 1800.0, 3)
    extension = _extension(world, main, '2026-02-01', 300.0)

    with pytest.raises(ValueError) as refusal:
        db.sell_asset(extension, '2026-06-30', 100.0)

    assert str(refusal.value) == NO_OWN_DISPOSAL
    row = db.get_asset_by_id(extension)
    assert (row[15], row[16], row[17]) == (None, None, 'active')
    # die Hauptanlage und eine Unter-Anlage mit eigenem Plan gehen weiter ab
    own = _own_asset_under(world, main, '2026-03-01', 900.0)
    db.sell_asset(own, '2026-06-30', 100.0)
    db.sell_asset(main, '2026-07-31', 500.0)
    assert db.get_asset_by_id(own)[17] == 'sold' and db.get_asset_by_id(main)[17] == 'sold'


def test_the_handler_gives_the_refusal_of_a_disposal_back_to_the_page_of_the_extension(world):
    db = world.db
    main = _asset(world, '2026-01-10', 1800.0, 3)
    extension = _extension(world, main, '2026-02-01', 300.0)

    status, location = handle_asset_sale(
        db, {'asset_id': [str(extension)], 'sale_date': ['2026-06-30'], 'sale_price': ['100']})

    assert status == 303 and location.startswith(f'/assets/view?id={extension}&err=')
    assert parse_qs(urlsplit(location).query)['err'] == [NO_OWN_DISPOSAL]
    row = db.get_asset_by_id(extension)
    assert (row[15], row[16], row[17]) == (None, None, 'active')
    assert handle_asset_sale(db, {'asset_id': [str(main)], 'sale_date': ['2026-06-30'], 'sale_price': ['0']}) == (
        303, f'/assets/view?id={main}')
    assert db.get_asset_by_id(main)[15] == '2026-06-30'


def test_the_page_of_an_extension_has_no_form_for_the_disposal(world):
    db = world.db
    main = _asset(world, f'{THIS_YEAR}-01-10', 6000.0, 6)
    extension = _extension(world, main, f'{THIS_YEAR}-03-01', 1200.0)
    own = _own_asset_under(world, main, f'{THIS_YEAR}-06-01', 600.0)

    assert 'id="sell_form"' not in PageAssetView(db, extension) and 'Abgang buchen' not in PageAssetView(db, extension)
    assert 'id="sell_form"' in PageAssetView(db, main) and 'id="sell_form"' in PageAssetView(db, own)


def _update_form(asset, **fields):
    base = {'asset_id': [str(asset)], 'name': ['Geänderter Name'], 'purchase_date': ['2026-02-01'],
            'purchase_price': ['300'], 'useful_life_years': ['3'], 'depreciation_method': ['linear']}
    return {**base, **{key: [str(value)] for key, value in fields.items()}}


@pytest.mark.parametrize('status', ['sold', 'scrapped', 'sonstwas'])
def test_the_handler_refuses_another_status_for_an_extension_and_writes_nothing(world, status):
    db = world.db
    main = _asset(world, '2026-01-10', 1800.0, 3)
    extension = _extension(world, main, '2026-02-01', 300.0)

    result = handle_update_asset(db, _update_form(extension, status=status))

    assert result[0] == 303 and result[1].startswith(f'/assets/view?id={extension}&err=')
    assert parse_qs(urlsplit(result[1]).query)['err'] == [NO_OWN_STATUS]
    row = db.get_asset_by_id(extension)
    assert (row[2], row[17]) == ('Erweiterung Probe', 'active')           # nichts geschrieben, auch der Name nicht


def test_the_handler_saves_an_extension_with_status_active_and_other_assets_with_any_status(world):
    db = world.db
    main = _asset(world, '2026-01-10', 1800.0, 3)
    extension = _extension(world, main, '2026-02-01', 300.0)
    own = _own_asset_under(world, main, '2026-03-01', 900.0)

    assert handle_update_asset(db, _update_form(extension, status='active')) == (303, f'/assets/edit?id={extension}')
    assert db.get_asset_by_id(extension)[2] == 'Geänderter Name'
    assert handle_update_asset(db, _update_form(extension, name='Noch ein Name')) == (303, f'/assets/edit?id={extension}')
    assert db.get_asset_by_id(extension)[2] == 'Noch ein Name'          # ohne Statusfeld gilt ``active``
    assert handle_update_asset(db, _update_form(main, status='sold'))[1] == f'/assets/edit?id={main}'
    assert handle_update_asset(db, _update_form(own, status='scrapped'))[1] == f'/assets/edit?id={own}'
    assert (db.get_asset_by_id(main)[17], db.get_asset_by_id(own)[17]) == ('sold', 'scrapped')


# ── Abgang ───────────────────────────────────────────────────────────────────

def test_open_years_after_the_year_of_disposal_drop_out_of_the_plan_and_booked_ones_stay(world):
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    assert _view(db.depreciation_plan(asset)) == [(2026, 600.0, False), (2027, 600.0, False), (2028, 600.0, False)]
    _book(world, asset, 2028)
    db.sell_asset(asset, '2027-06-30', 500.0)

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(2026, 600.0, False), (2027, 600.0, False), (2028, 600.0, True)]     # gebucht bleibt
    assert [e['bookable'] for e in plan] == [True, True, False]
    with pytest.raises(ValueError, match=r'^AfA 2028 liegt nach dem Abgang der Anlage \(2027-06-30\)$'):
        _book(world, asset, 2028)

    other = _asset(world, '2026-01-10', 1800.0, 3, name='Anlage Ohne Buchung')
    db.sell_asset(other, '2027-06-30', 500.0)
    assert _view(db.depreciation_plan(other)) == [(2026, 600.0, False), (2027, 600.0, False)]   # das Abgangsjahr bleibt
    assert _booking_amount(db, _book(world, other, 2027)) == to_minor(600.0)


def test_open_years_after_the_year_of_disposal_drop_out_of_the_continuation_too(world):
    db = world.db
    asset = _asset(world, '2020-01-10', 15000.0, 10)
    for year in range(2020, 2025):
        _imported(db, asset, year, 1000.0)                            # die Geschichte weicht von der Formel ab
    assert [e['year'] for e in db.depreciation_plan(asset)][-1] == 2029
    db.sell_asset(asset, '2026-06-30', 4000.0)

    plan = db.depreciation_plan(asset)

    assert _view(plan) == [(year, 1000.0, True) for year in range(2020, 2025)] + [(2025, 2000.0, False),
                                                                                   (2026, 2000.0, False)]
    assert [e['bookable'] for e in plan] == [False] * 5 + [True, False]      # nur das erste offene Jahr

    _imported(db, asset, 2028, 2000.0)                                # ein gebuchtes Jahr nach dem Abgang bleibt
    plan = db.depreciation_plan(asset)
    assert _view(plan) == [(year, 1000.0, True) for year in range(2020, 2025)] + [(2028, 2000.0, True)]
    assert not any(e['bookable'] for e in plan)                       # das erste offene Jahr (2029) liegt nach dem Abgang


@pytest.mark.parametrize('sale, day_before', [('2027-06-30', 600.0), ('2027-01-01', 1200.0), ('2027-12-31', 600.0)])
def test_the_book_value_is_zero_on_and_after_the_date_of_disposal(world, sale, day_before):
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    db.sell_asset(asset, sale, 500.0)
    sale_day = datetime.date.fromisoformat(sale)
    one_day = datetime.timedelta(days=1)

    assert db.get_book_value_at_date(asset, datetime.date(2026, 12, 31)) == 1200.0
    assert db.get_book_value_at_date(asset, sale_day - one_day) == day_before     # davor wie bisher
    assert db.get_book_value_at_date(asset, sale_day) == 0.0
    assert db.get_book_value_at_date(asset, sale_day + one_day) == 0.0
    assert db.get_book_value_at_date(asset, datetime.date(2040, 1, 1)) == 0.0


@pytest.mark.parametrize('legacy', ['30.06.2027', 'Juni 2027', '??'])
def test_an_unreadable_date_of_disposal_changes_nothing_about_the_book_value(world, legacy):
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    _execute(db, "UPDATE Assets SET SaleDate = ?, Status = 'sold' WHERE ID = ?", (legacy, asset))

    assert db.get_book_value_at_date(asset, datetime.date(2027, 12, 31)) == 600.0
    assert db.get_book_value_at_date(asset, datetime.date(2026, 6, 1)) == 1200.0
    assert [e['year'] for e in db.depreciation_plan(asset)] == [2026, 2027, 2028]      # kein Abgangsjahr


@pytest.mark.parametrize('legacy', ['2027.06.30', '2027-13-45', '2027'])
def test_a_date_of_disposal_that_is_no_iso_date_but_starts_with_a_year_zeroes_the_value_after_that_year(world, legacy):
    """Der Plan endet mit dem Abgangsjahr (die ersten vier Zeichen); der Buchwert zieht mit: Im Abgangsjahr gilt noch der
    Planwert, ab dem Jahr danach ist er 0 – ein ISO-Datum dagegen gilt ab dem Tag (siehe oben)."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    _execute(db, "UPDATE Assets SET SaleDate = ?, Status = 'sold' WHERE ID = ?", (legacy, asset))

    assert [e['year'] for e in db.depreciation_plan(asset)] == [2026, 2027]
    assert db.get_book_value_at_date(asset, datetime.date(2026, 6, 1)) == 1200.0
    assert db.get_book_value_at_date(asset, datetime.date(2027, 12, 31)) == 600.0
    assert db.get_book_value_at_date(asset, datetime.date(2028, 1, 1)) == 0.0
    assert db.get_book_value_at_date(asset, datetime.date(2040, 1, 1)) == 0.0


def test_booking_after_the_disposal_is_refused_with_the_disposal_as_reason(world):
    """Der Plan kennt die Jahre nach dem Abgang nicht mehr – ``book_depreciation`` nennt trotzdem den Abgang, auch für
    ein Jahr weit hinter dem Plan; ein Jahr vor dem Plan bleibt „nicht geplant“."""
    db = world.db
    asset = _asset(world, '2026-01-10', 1800.0, 3)
    db.sell_asset(asset, '2027-06-30', 500.0)

    for year in (2028, 2099):
        with pytest.raises(ValueError, match=rf'^AfA {year} liegt nach dem Abgang der Anlage \(2027-06-30\)$'):
            _book(world, asset, year)
    with pytest.raises(ValueError, match='^No depreciation planned for year 2025$'):
        _book(world, asset, 2025)
    assert _count(db, 'Bookings') == 0


def test_the_plan_of_a_main_asset_with_extensions_ends_with_the_year_of_disposal_as_well(world):
    db = world.db
    main = _asset(world, '2020-01-10', 12000.0, 10)
    _extension(world, main, '2023-05-01', 3000.0)
    db.sell_asset(main, '2024-09-30', 9000.0)

    assert [e['year'] for e in db.depreciation_plan(main)] == [2020, 2021, 2022, 2023, 2024]
    assert db.get_book_value_at_date(main, datetime.date(2024, 9, 30)) == 0.0
    assert db.get_book_value_at_date(main, datetime.date(2024, 9, 29)) > 0.0


# ── Die reine Verteilung ─────────────────────────────────────────────────────

BEGIN = 2026 * 12                                  # Januar 2026
END_TWO_YEARS = BEGIN + 24


def test_continue_spreads_the_rest_over_the_months_up_to_the_end():
    assert _continue(120000, 2026, {}, BEGIN, END_TWO_YEARS) == [(2026, 120000, 60000), (2027, 60000, 60000)]
    assert _continue(120000, 2027, {}, BEGIN, END_TWO_YEARS) == [(2027, 120000, 120000)]
    # Planende mitten im Jahr: 12 + 12 + 6 Monate
    assert [(year, amount) for year, _before, amount in _continue(3000, 2026, {}, BEGIN, BEGIN + 30)] == [
        (2026, 1200), (2027, 1200), (2028, 600)]


def test_continue_adds_a_later_extension_to_the_rest_and_spreads_both_anew():
    rows = _continue(120000, 2026, {2027: 60000}, BEGIN, BEGIN + 36)

    assert rows == [(2026, 120000, 40000), (2027, 140000, 70000), (2028, 70000, 70000)]
    assert sum(amount for _year, _before, amount in rows) == 180000


def test_continue_takes_a_later_extension_even_when_the_rest_is_used_up_or_negative():
    assert _continue(0, 2026, {2027: 500}, BEGIN, END_TWO_YEARS) == [(2027, 500, 500)]
    assert _continue(-5, 2026, {2027: 3}, BEGIN, END_TWO_YEARS) == []              # −2 bleibt ohne Zeile
    assert _continue(-5, 2026, {2027: 9}, BEGIN, END_TWO_YEARS) == [(2027, 4, 4)]


def test_continue_puts_the_whole_amount_into_the_year_after_the_end_of_the_useful_life():
    assert _continue(1000, 2026, {2030: 400}, BEGIN, END_TWO_YEARS) == [
        (2026, 1000, 500), (2027, 500, 500), (2030, 400, 400)]
    assert _continue(1000, 2030, {}, BEGIN, END_TWO_YEARS) == [(2030, 1000, 1000)]
    assert _continue(1000, 2030, {2032: 200, 2031: 100}, BEGIN, END_TWO_YEARS) == [
        (2030, 1000, 1000), (2031, 100, 100), (2032, 200, 200)]


def test_continue_ignores_extensions_up_to_the_first_year_and_drops_zero_cent_years():
    assert _continue(1000, 2026, {2025: 7, 2026: 9}, BEGIN, END_TWO_YEARS) == [(2026, 1000, 500), (2027, 500, 500)]
    assert _continue(1, 2026, {}, BEGIN, BEGIN + 36) == [(2028, 1, 1)]          # 1 Cent auf drei Jahre


# ── Zufällige Anlagen: Summe auf den Cent, Buchen Jahr für Jahr ──────────────

@pytest.mark.parametrize('seed', range(20))
def test_random_assets_with_extensions_add_up_to_the_cent_and_book_exactly_their_plan(world, seed):
    """Einige Jahre vor den Erweiterungen gebucht (dann kann die Geschichte abweichen), danach Jahr für Jahr in
    beliebiger Reihenfolge gebucht: Jeder Zustand summiert sich auf den Anschaffungswert samt Erweiterungen, und jede
    Buchung trägt genau den Planbetrag."""
    rng = random.Random(seed)
    db = world.db
    year = rng.randint(2014, 2020)
    life = rng.randint(2, 9)
    main = _asset(world, f'{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}', round(rng.uniform(900, 30000), 2),
                  life, rng.choice(['linear', 'degressive']))
    for entry in db.depreciation_plan(main):
        if rng.random() < 0.4:
            _book(world, main, entry['year'])
    for _ in range(rng.randint(1, 3)):
        _extension(world, main, f'{rng.randint(year - 1, year + life + 3)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}',
                   round(rng.uniform(50, 8000), 2))
    total = to_minor(db.acquisition_cost(main, 9999))

    def check(plan):
        assert [e['year'] for e in plan] == sorted({e['year'] for e in plan})
        assert all(to_minor(e['depreciation']) >= 0 for e in plan)
        assert _cents_sum(plan) == total, plan
        return plan

    plan = check(db.depreciation_plan(main))
    for _ in range(40):
        bookable = [e for e in plan if e['bookable']]
        if not bookable:
            break
        entry = rng.choice(bookable)
        assert _booking_amount(db, _book(world, main, entry['year'])) == to_minor(entry['depreciation']), (seed, entry)
        plan = check(db.depreciation_plan(main))
    assert all(e['booked'] for e in plan)
    assert _rows(db, 'SELECT SUM(Amount) AS total FROM Bookings')[0]['total'] == total, seed


# ── Seiten und Handler ───────────────────────────────────────────────────────

def _plan_by_year(html):
    """{Jahr: HTML der Zeile} der Tabelle „AfA-Plan“."""
    block = html[html.index('<h3>AfA-Plan</h3>'):html.index('Erweiterungen / Nachkäufe')]
    parts = re.split(r'<tr(?: class="row-ok")?><td>(\d{4})</td>', block)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}


def _children_box(html):
    return html[html.index('Erweiterungen / Nachkäufe'):]


def test_the_page_of_the_main_asset_shows_the_extension_without_a_book_value_and_the_hint(world):
    db = world.db
    main = _asset(world, f'{THIS_YEAR - 1}-01-10', 6000.0, 6)
    own = _own_asset_under(world, main, f'{THIS_YEAR - 1}-06-01', 1500.0)
    _extension(world, main, f'{THIS_YEAR}-02-01', 1200.0)

    box = _children_box(PageAssetView(db, main))
    rows = {name: next(row for row in box.split('<tr>') if f'<td>{name}</td>' in row)
            for name in ('Erweiterung Probe', 'Altbestand Probe')}

    assert 'in der Hauptanlage' in rows['Erweiterung Probe'] and '1.200,00' in rows['Erweiterung Probe']
    assert 'in der Hauptanlage' not in rows['Altbestand Probe']
    own_value = db.get_book_value_at_date(own)
    assert own_value > 0 and fmt_eur(own_value) in rows['Altbestand Probe']         # die eigene Anlage zeigt ihren Wert
    assert f'<p class="hint">{EXTENSION_HINT}</p>' in box and 'Zahlungsdatum' in EXTENSION_HINT
    assert '+ Erweiterung hinzufügen' in box


def test_the_page_of_an_extension_points_to_the_main_asset_and_offers_no_booking(world):
    db = world.db
    main = _asset(world, f'{THIS_YEAR}-01-10', 6000.0, 6)
    extension = _extension(world, main, f'{THIS_YEAR}-03-01', 1200.0)

    html = PageAssetView(db, extension)

    assert 'Die AfA dieser Erweiterung läuft über die Hauptanlage.' in html
    assert f'<a href="/assets/edit?id={main}">Zur Hauptanlage</a>' in html
    assert 'AfA buchen' not in html and 'Buchwert Anfang' not in html and 'depr_form_' not in html
    assert '+ Erweiterung hinzufügen' not in html and 'Keine Erweiterungen vorhanden.' in html
    assert EXTENSION_HINT not in html                                 # der Hinweis steht unter der Tabelle der Hauptanlage
    assert 'AfA buchen' in PageAssetView(db, main)                    # die Hauptanlage bucht (laufendes Jahr)


def test_a_main_asset_offers_the_extension_link_and_an_extension_with_its_own_booked_year_does_not(world):
    db = world.db
    main = _asset(world, f'{THIS_YEAR}-01-10', 6000.0, 6)
    own = _own_asset_under(world, main, f'{THIS_YEAR}-06-01', 600.0)

    assert f'/assets/new?parent_id={main}' in PageAssetView(db, main)
    assert '+ Erweiterung hinzufügen' not in PageAssetView(db, own)   # auch der Bestand bekommt keine eigene Erweiterung


def test_the_page_of_an_extension_with_its_own_booked_year_keeps_its_plan_table(world):
    db = world.db
    main = _asset(world, f'{THIS_YEAR}-01-10', 6000.0, 6)
    own = _own_asset_under(world, main, f'{THIS_YEAR}-06-01', 600.0)

    html = PageAssetView(db, own)

    assert 'Buchwert Anfang' in html and 'Die AfA dieser Erweiterung läuft über die Hauptanlage.' not in html
    assert '✓ Gebucht' in _plan_by_year(html)[THIS_YEAR]


def test_the_form_of_a_new_extension_names_the_main_asset_and_the_rule(world):
    db = world.db
    main = _asset(world, f'{THIS_YEAR}-01-10', 6000.0, 6, name='Hauptanlage <b>Probe</b>')

    html = PageAssetEdit(db, parent_id=main)

    assert '🔗 Erweiterung von: <strong>Hauptanlage &lt;b&gt;Probe&lt;/b&gt;</strong></p>' in html
    assert f'<p class="hint">{EXTENSION_FORM_HINT}</p>' in html
    assert f'name="parent_id" value="{main}"' in html
    assert EXTENSION_FORM_HINT not in PageAssetEdit(db)               # nur das Formular einer neuen Erweiterung


def test_the_list_shows_the_acquisition_cost_with_the_extensions_of_the_year_and_the_matching_book_value(world):
    db = world.db
    main = _asset(world, f'{THIS_YEAR}-01-10', 1800.0, 3)
    _extension(world, main, f'{THIS_YEAR}-03-01', 600.0)
    later = _asset(world, f'{THIS_YEAR}-01-10', 900.0, 3, name='Anlage Spaeter')
    _extension(world, later, f'{THIS_YEAR + 1}-03-01', 300.0, 'Spaetere Erweiterung')   # kommt erst nächstes Jahr dazu

    html = PageAssets(db)

    # 1.800 + 600 (im Kaufjahr: ab dem Kaufmonat, 800 € im Jahr) und 900 (die Erweiterung zählt ab dem nächsten Jahr)
    assert 'Anschaffungskosten (aktiv): 3.300,00 €' in html
    assert f'AfA {THIS_YEAR} gebucht: 0,00 € | AfA {THIS_YEAR} geplant: 1.100,00 €' in html     # 800 + 300
    assert 'Gesamtrestbuchwert: 2.200,00 €' in html                                            # 1.600 + 600
    row = next(row for row in html.split('<tr>') if '>Hauptanlage Probe<' in row)
    assert '2.400,00' in row and '1.600,00' in row
    assert 'Erweiterung Probe' not in html and 'Spaetere Erweiterung' not in html            # die Liste zeigt nur Hauptanlagen


def test_both_hints_name_the_asset_account_of_the_main_asset_as_well():
    for hint in (EXTENSION_HINT, EXTENSION_FORM_HINT):
        assert 'Nutzungsdauer, Methode und Anlagekonto der Hauptanlage gelten' in hint
    assert 'Zahlungsdatum' in EXTENSION_HINT and 'Tag, an dem die Kosten entstanden sind' in EXTENSION_FORM_HINT


def test_the_purchase_date_is_escaped_in_the_list_the_detail_pages_and_the_extension_table(world):
    """Das Datum steht in der Datenbank (SQL-Konsole, Sicherung): Es kommt maskiert in die Liste, in die Tabelle der
    Erweiterungen und in das Feld des Formulars – ein rohes ``<img`` darf nirgends stehen."""
    db = world.db
    main = _asset(world, '2021-01-01', 6000.0, 6)
    extension = _extension(world, main, '2021-02-01', 600.0)
    _execute(db, 'UPDATE Assets SET PurchaseDate = ? WHERE ID IN (?, ?)', (EVIL_DATE, main, extension))
    assert db.counts_toward_parent(extension) is True                  # die ersten zehn Zeichen sind ein Datum

    shown = 'value="2021-01-01&quot;&gt;&lt;img src=x onerror=1&gt;"'
    pages = {'Liste': PageAssets(db), 'Seite der Hauptanlage': PageAssetView(db, main),
             'Seite der Erweiterung': PageAssetView(db, extension)}
    for name, html in pages.items():
        assert '<img src=x' not in html and 'onerror=1>' not in html, name
    assert '<td>2021-01-01&quot;&gt;&lt;img src=x onerror=1&gt;</td>' in pages['Liste']
    assert '<td>2021-01-01&quot;&gt;&lt;img src=x onerror=1&gt;</td>' in _children_box(pages['Seite der Hauptanlage'])
    assert shown in pages['Seite der Hauptanlage'] and shown in pages['Seite der Erweiterung']


def _post_data(**fields):
    base = {'name': ['Neue Erweiterung'], 'purchase_date': ['2026-03-01'], 'purchase_price': ['100'],
            'useful_life_years': ['3'], 'depreciation_method': ['linear']}
    return {**base, **{key: [str(value)] for key, value in fields.items()}}


def _names(db):
    return sorted(a[2] for a in db.fetch_assets(parent_only=False))


def test_the_handler_refuses_an_extension_of_an_extension_and_creates_nothing(world):
    db = world.db
    main = _asset(world, '2026-01-10', 1800.0, 3)
    extension = _extension(world, main, '2026-02-01', 300.0)
    before = _names(db)

    status, location = handle_add_asset(db, _post_data(parent_id=extension))

    assert status == 303 and location.startswith(f'/assets/view?id={extension}&err=')
    assert parse_qs(urlsplit(location).query)['err'] == [NO_EXTENSION_OF_AN_EXTENSION]
    assert _names(db) == before


def test_the_handler_refuses_an_unknown_main_asset_and_creates_nothing(world):
    db = world.db
    before = _names(db)

    status, location = handle_add_asset(db, _post_data(parent_id=99999))

    assert status == 303 and location.startswith('/assets/view?id=99999&err=')
    assert parse_qs(urlsplit(location).query)['err'] == [NO_PARENT]
    assert _names(db) == before


def test_the_handler_creates_an_extension_of_a_main_asset_and_an_asset_without_parent(world):
    db = world.db
    main = _asset(world, '2026-01-10', 1800.0, 3)

    assert handle_add_asset(db, _post_data(parent_id=main)) == (303, '/assets')
    assert handle_add_asset(db, _post_data(name='Eigene Anlage')) == (303, '/assets')
    assert handle_add_asset(db, _post_data(name='Mit leerer Angabe', parent_id='')) == (303, '/assets')

    children = db.get_asset_children(main)
    assert [a[2] for a in children] == ['Neue Erweiterung'] and db.counts_toward_parent(children[0][0]) is True
    assert 'Eigene Anlage' in _names(db) and 'Mit leerer Angabe' in _names(db)


@pytest.fixture
def web(auto_login, server_port):
    db = auto_login.db
    main = db.insert_asset('Hauptanlage Probe', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    extension = db.insert_asset('Erweiterung Probe', '2026-02-01', 300.0, 3, coa_id=_coa(db, 520), parent_id=main)
    return SimpleNamespace(db=db, main=main, extension=extension, port=server_port, csrf=auto_login.csrf,
                           headers={'Origin': own_origin(server_port)}, expense=_coa(db, 6220), anlage=_coa(db, 520))


def _post(web, path, form=None):
    return post(web.port, path, headers=web.headers, csrf=web.csrf, form=form)


def _add(web, **fields):
    form = {'name': 'Neue Erweiterung', 'purchase_date': '2026-03-01', 'purchase_price': '100',
            'useful_life_years': '3', 'depreciation_method': 'linear', **fields}
    return post(web.port, '/assets/add', headers=web.headers, csrf=web.csrf, form=form)


@pytest.mark.parametrize('parent, message', [('extension', NO_EXTENSION_OF_AN_EXTENSION), ('missing', NO_PARENT)])
def test_the_route_shows_the_refusal_on_the_page(web, parent, message):
    parent_id = web.extension if parent == 'extension' else 99999

    response = _add(web, parent_id=parent_id)

    assert response.status == 303 and f'/assets/view?id={parent_id}&err=' in response.headers['Location']
    page = get(web.port, response.headers['Location'])
    assert page.status == 200
    assert app_msg_arguments(Dom(page.text)) == [message, 'error']
    assert 'Neue Erweiterung' not in _names(web.db)


def test_the_route_creates_an_extension_of_a_main_asset(web):
    response = _add(web, parent_id=web.main)

    assert response.status == 303 and response.headers['Location'] == '/assets'
    assert [a[2] for a in web.db.get_asset_children(web.main)] == ['Erweiterung Probe', 'Neue Erweiterung']
    page = get(web.port, f'/assets/edit?id={web.main}')
    assert page.status == 200 and page.text.count('in der Hauptanlage') == 2


def test_the_route_shows_the_refusal_to_delete_an_extension_on_its_page(web):
    web.db.book_depreciation(web.main, 2026, None, web.expense, web.anlage)       # 2026: das Jahr der Erweiterung

    response = _post(web, f'/assets/delete?id={web.extension}')

    assert response.status == 303 and response.headers['Location'].startswith(f'/assets/view?id={web.extension}&err=')
    page = get(web.port, response.headers['Location'])
    assert page.status == 200
    assert app_msg_arguments(Dom(page.text)) == [DELETE_REFUSAL.format(years='2026'), 'error']
    assert web.db.get_asset_by_id(web.extension) is not None


def test_the_route_deletes_an_extension_while_the_main_asset_has_booked_nothing(web):
    response = _post(web, f'/assets/delete?id={web.extension}')

    assert response.status == 303 and response.headers['Location'] == '/assets'
    assert web.db.get_asset_by_id(web.extension) is None


@pytest.mark.parametrize('path, form, message', [
    ('/assets/sell', {'sale_date': '2026-06-30', 'sale_price': '0'}, NO_OWN_DISPOSAL),
    ('/assets/update', {'name': 'Erweiterung Probe', 'purchase_date': '2026-02-01', 'purchase_price': '300',
                        'useful_life_years': '3', 'depreciation_method': 'linear', 'status': 'sold'}, NO_OWN_STATUS),
], ids=['abgang', 'status'])
def test_the_route_shows_the_refusal_of_a_disposal_or_status_of_an_extension_on_its_page(web, path, form, message):
    response = _post(web, path, {'asset_id': web.extension, **form})

    assert response.status == 303 and response.headers['Location'].startswith(f'/assets/view?id={web.extension}&err=')
    page = get(web.port, response.headers['Location'])
    assert page.status == 200 and app_msg_arguments(Dom(page.text)) == [message, 'error']
    row = web.db.get_asset_by_id(web.extension)
    assert (row[15], row[17]) == (None, 'active')
