# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kasse = SKR04-Konto 1600 (2.0.5).

Eine neu angelegte Kasse (``ensure_kasse_exists``, ``insert_account`` ohne Angabe) bekommt
das Kassenkonto 1600 – das Verrechnungskonto 1460 ist Geldtransit, keine Kasse. Fehlt 1600 im
Kontenrahmen (Datenbank älter als der Seed-Eintrag), wird es nachgelegt (``ensure_cash_coa``),
aber nur dort, wo eine Kasse mit 1600 entsteht: Eine vorhandene Kasse behält ihre Nummer (leer
oder 1460), Seitenaufrufe legen nichts an, und eine Kasse, die der Nutzer auf 1600 umstellt
(``handle_update_bankaccount``), bekommt das Konto.

Dazu die Zahlungskonten (``get_payment_coa_ids``: eigene Bank-/Kassenkonten, der feste Bereich
1000–1099 und das Verrechnungskonto 1460), an denen beide WISO-Importe das Vorzeichen ausrichten:
1600 ist ein Zahlungskonto nur, solange ein eigenes Konto die Nummer führt – in einer Datenbank mit
Kasse ohne Nummer oder mit 1460 behält „6815 an 1600“ den Betrag der Datei (wie in 2.0.3, ein
erneuter Import legt nichts neu an) –, und die Demo (die Kasse der Testdaten steht dort als
Portokasse auf 1610, weil 1600 die automatisch angelegte Kasse trägt).

Alle Namen und Werte sind erfunden.
"""
import csv
import datetime
import os
from contextlib import closing
from decimal import Decimal

import pytest

import demo_seed
from db import Database, _CoreMixin
from importers.wiso_fdb import DEFAULT_LIQUID_ACCOUNTS
from money import to_minor
from server.handlers import handle_datev_export, handle_update_bankaccount
from server.pages_masterdata import PageBankAccounts
from server.pages_miscellaneous import PageMiscellaneous
from tests.fdb_builder import FdbBuilder, INT64, LONG, SHORT, TEXT, TIMESTAMP, Column
from tests.http_client import get

D1, D2 = '2026-01-01', '2026-12-31'
OLD_CASH_NUMBERS = [None, 1460]            # so haben ältere Datenbanken ihre Kasse
OLD_ID = {None: 'ohne-skr', 1460: '1460'}
OLD_PARAMS = [pytest.param(skr, id=f'kasse-{OLD_ID[skr]}') for skr in OLD_CASH_NUMBERS]


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _query(db, sql, params=()):
    with closing(db._get_connection()) as conn:
        return conn.execute(sql, params).fetchall()


def _execute(db, sql, params=()):
    with closing(db._get_connection()) as conn:
        conn.execute(sql, params)
        conn.commit()


def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _cash_accounts(db):
    """[(Name, SKR-Nummer)] der Kassen, in der Reihenfolge ihrer Anlage."""
    return [tuple(row) for row in _query(
        db, 'SELECT Name, SKRAccount FROM Accounts WHERE IsCash = 1 ORDER BY ID')]


def _chart_numbers(db):
    return {row[0] for row in _query(db, 'SELECT AccountNumber FROM ChartOfAccounts')}


def _count_chart(db, number):
    return _query(db, 'SELECT COUNT(*) FROM ChartOfAccounts WHERE AccountNumber = ?',
                  (number,))[0][0]


def _old_database(tmp_path, cash_number, name='alt.db'):
    """Datenbank „älter als 2.0.5“, neu geöffnet wie nach einem Neustart: Die Kasse trägt
    ``cash_number`` (leer oder 1460), und der Kontenrahmen kennt 1600 und 1610 nicht."""
    path = str(tmp_path / name)
    first = Database(db_name=path)
    _execute(first, 'UPDATE Accounts SET SKRAccount = ? WHERE IsCash = 1', (cash_number,))
    _execute(first, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (1600, 1610)')
    _CoreMixin._initialized_dbs.discard(os.path.abspath(path))
    return Database(db_name=path)


def _cash_id(db, name='Kasse'):
    return next(a[0] for a in db.fetch_accounts() if a[1] == name)


def _euer(db, date_from=D1, date_to=D2):
    return {nr: Decimal(str(total)) for nr, _name, total in db.get_euer_data(date_from, date_to)}


# ── Frische Datenbank ────────────────────────────────────────────────────────

def test_fresh_database_has_one_cash_account_with_1600(tmp_db):
    assert _cash_accounts(tmp_db) == [('Kasse', 1600)]
    assert tmp_db.get_account_id_by_skr(1600) == _cash_id(tmp_db)


def test_fresh_chart_of_accounts_has_the_cash_accounts_1600_and_1610(tmp_db):
    rows = _query(tmp_db, 'SELECT Framework, AccountNumber, Name, IsStandard, ShowInMenu '
                          'FROM ChartOfAccounts WHERE AccountNumber IN (1600, 1610) '
                          'ORDER BY AccountNumber')
    assert rows == [(4, 1600, 'Kasse', 1, 1), (4, 1610, 'Nebenkasse 1', 1, 1)]
    assert _coa(tmp_db, 1600) in tmp_db.get_bank_coa_ids()      # die Kasse führt ihr Konto


def test_second_cash_account_without_a_number_gets_1600_on_a_fresh_database(tmp_db):
    tmp_db.insert_account('Reisekasse', '', '', '', '', is_cash=1)
    assert _cash_accounts(tmp_db) == [('Kasse', 1600), ('Reisekasse', 1600)]
    assert _count_chart(tmp_db, 1600) == 1                       # das Konto gibt es weiter nur einmal


def test_cash_account_with_an_explicit_number_keeps_it(tmp_db):
    tmp_db.insert_account('Nebenkasse', '', '', '', '', is_cash=1, skr_account=1610)
    tmp_db.insert_account('Altkasse', '', '', '', '', is_cash=1, skr_account=1460)
    assert _cash_accounts(tmp_db)[1:] == [('Nebenkasse', 1610), ('Altkasse', 1460)]


def test_bank_account_without_a_number_stays_without_one(tmp_db):
    tmp_db.insert_account('Giro ohne Nummer', 'Owner', 'DE00CASHACCOUNT', 'BIC', 'Bank', is_cash=0)
    assert _query(tmp_db, "SELECT SKRAccount FROM Accounts WHERE Name = 'Giro ohne Nummer'") == [(None,)]


def test_hints_name_1600_for_the_cash_account(tmp_db):
    """Die Hinweise nennen 1600 für die Kasse und das Verrechnungskonto als Geldtransit – nicht
    mehr 1460 als Kassenkonto."""
    edit = PageBankAccounts(tmp_db, edit_id=_cash_id(tmp_db))
    assert 'placeholder="z.B. 1600"' in edit and 'z.B. 1460' not in edit
    misc = PageMiscellaneous(tmp_db)
    assert '1600 für Kasse' in misc and '1460 für Kasse' not in misc
    assert 'Verrechnungskonto 1460 gilt als Geldtransit' in misc


# ── Bestandsdatenbank: nichts ändert sich ────────────────────────────────────

@pytest.mark.parametrize('cash_number', OLD_PARAMS)
def test_existing_cash_account_and_chart_are_left_alone_when_the_database_is_opened(
        tmp_path, cash_number):
    db = _old_database(tmp_path, cash_number)

    assert _cash_accounts(db) == [('Kasse', cash_number)]       # weder 1600 noch geändert
    assert not {1600, 1610} & _chart_numbers(db)                # das Konto wird nicht nachgelegt
    assert len(db.fetch_accounts()) == 1


@pytest.mark.parametrize('cash_number', OLD_PARAMS)
def test_ensure_kasse_exists_changes_nothing_for_an_existing_cash_account(tmp_path, cash_number):
    db = _old_database(tmp_path, cash_number)
    chart_before = _query(db, 'SELECT COUNT(*) FROM ChartOfAccounts')

    db.ensure_kasse_exists()
    db.ensure_kasse_exists()

    assert _cash_accounts(db) == [('Kasse', cash_number)]
    assert _query(db, 'SELECT COUNT(*) FROM ChartOfAccounts') == chart_before
    assert 1600 not in _chart_numbers(db)


def test_missing_cash_account_is_recreated_with_1600_and_the_account(tmp_path):
    """Gibt es keine Kasse (mehr), legt ``ensure_kasse_exists`` sie mit 1600 an – samt Konto."""
    db = _old_database(tmp_path, None)
    _execute(db, 'DELETE FROM Accounts WHERE IsCash = 1')

    db.ensure_kasse_exists()

    assert _cash_accounts(db) == [('Kasse', 1600)]
    assert _count_chart(db, 1600) == 1


@pytest.mark.parametrize('cash_number', OLD_PARAMS)
def test_second_cash_account_without_a_number_gets_1600_and_creates_the_missing_account(
        tmp_path, cash_number):
    db = _old_database(tmp_path, cash_number)
    assert 1600 not in _chart_numbers(db)

    db.insert_account('Reisekasse', '', '', '', '', is_cash=1)

    assert _cash_accounts(db) == [('Kasse', cash_number), ('Reisekasse', 1600)]
    assert _query(db, 'SELECT Framework, Name, IsStandard, ShowInMenu FROM ChartOfAccounts '
                      'WHERE AccountNumber = 1600') == [(4, 'Kasse', 1, 1)]


def test_explicit_numbers_and_bank_accounts_do_not_create_1600(tmp_path):
    db = _old_database(tmp_path, None)

    db.insert_account('Nebenkasse', '', '', '', '', is_cash=1, skr_account=1610)
    db.insert_account('Giro ohne Nummer', 'Owner', 'DE00CASHACCOUNT', 'BIC', 'Bank', is_cash=0)
    db.insert_account('Giro mit Nummer', 'Owner', 'DE00CASHACCOUNT2', 'BIC', 'Bank', is_cash=0,
                      skr_account=1800)

    assert 1600 not in _chart_numbers(db)


@pytest.mark.parametrize('cash_number', OLD_PARAMS)
def test_ensure_cash_coa_creates_1600_once(tmp_path, cash_number):
    db = _old_database(tmp_path, cash_number)

    first, second = db.ensure_cash_coa(), db.ensure_cash_coa()

    assert first == second == _coa(db, 1600) and first is not None
    assert _count_chart(db, 1600) == 1


def test_ensure_cash_coa_returns_the_existing_account(tmp_db):
    existing = _coa(tmp_db, 1600)
    chart_before = _query(tmp_db, 'SELECT COUNT(*) FROM ChartOfAccounts')
    assert tmp_db.ensure_cash_coa() == existing
    assert _query(tmp_db, 'SELECT COUNT(*) FROM ChartOfAccounts') == chart_before


@pytest.mark.parametrize('cash_number', OLD_PARAMS)
@pytest.mark.parametrize('chosen, creates', [
    ('1600', True), ('1610', False), ('1460', False), ('', False), ('1800', False),
], ids=['1600', '1610', '1460', 'leer', '1800'])
def test_switching_a_cash_account_to_1600_creates_the_account_other_numbers_do_not(
        tmp_path, cash_number, chosen, creates):
    db = _old_database(tmp_path, cash_number)

    status, location = handle_update_bankaccount(
        db, {'id': [str(_cash_id(db))], 'name': ['Kasse'], 'skr_account': [chosen]})

    assert (status, location) == (303, '/masterdata/bankaccounts')
    assert _cash_accounts(db) == [('Kasse', int(chosen) if chosen else None)]
    assert (1600 in _chart_numbers(db)) is creates
    assert _count_chart(db, 1600) == (1 if creates else 0)


def test_switching_to_1600_on_a_fresh_database_creates_nothing_new(tmp_db):
    tmp_db.insert_account('Reisekasse', '', '', '', '', is_cash=1, skr_account=1610)
    handle_update_bankaccount(tmp_db, {'id': [str(_cash_id(tmp_db, 'Reisekasse'))],
                                       'name': ['Reisekasse'], 'skr_account': ['1600']})
    assert _cash_accounts(tmp_db)[1] == ('Reisekasse', 1600)
    assert _count_chart(tmp_db, 1600) == 1


# ── Seitenaufrufe legen nichts an ────────────────────────────────────────────

def test_viewing_pages_creates_no_chart_account_and_leaves_the_cash_account_alone(
        auto_login, server_port):
    """GET-Seiten der Stammdaten, Buchungen und Anlagen (und der Neustart davor) ändern den
    Kontenrahmen und die Konten nicht – auch nicht in einer Datenbank ohne Konto 1600."""
    db = auto_login.db
    _execute(db, 'UPDATE Accounts SET SKRAccount = NULL WHERE IsCash = 1')
    _execute(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber IN (1600, 1610)')
    asset = db.insert_asset('Laptop Probe', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    booking = db.insert_booking('2026-03-01', Decimal('-10'), account_id=_cash_id(db),
                                coa_id=_coa(db, 6815), booking_type='entry')
    _CoreMixin._initialized_dbs.discard(os.path.abspath(db.db_name))     # Neustart
    state = lambda: (_query(db, 'SELECT * FROM ChartOfAccounts ORDER BY ID'),     # noqa: E731
                     _query(db, 'SELECT * FROM Accounts ORDER BY ID'))
    before = state()

    paths = ['/dashboard', '/masterdata', '/masterdata/bankaccounts',
             f'/masterdata/bankaccounts/edit?id={_cash_id(db)}', '/masterdata/skr',
             '/miscellaneous', '/transactions', f'/transactions/edit?id={booking}',
             '/assets', '/assets/new', f'/assets/edit?id={asset}', f'/assets/view?id={asset}']
    for path in paths:
        assert get(server_port, path).status in (200, 303), path

    assert state() == before
    assert 1600 not in _chart_numbers(db) and _cash_accounts(db) == [('Kasse', None)]


# ── Zahlungskonten ───────────────────────────────────────────────────────────
# Fest liquide sind der SKR03-Kassenbereich 1000–1099 und das Verrechnungskonto 1460
# (``DEFAULT_LIQUID_ACCOUNTS``). 1600 und jedes weitere Kassenkonto zählen nur, wenn ein eigenes Konto
# (``Accounts.SKRAccount``) die Nummer führt – eine Datenbank mit Kasse ohne Nummer oder mit 1460
# liest „6815 an 1600“ also wie bisher (2.0.3).

def _numbers(db, coa_ids):
    by_id = db.get_coa_id_to_number_map()
    return {by_id[coa_id] for coa_id in coa_ids}


def _set_cash_number(db, number):
    """Die Kasse in den Stammdaten auf ``number`` (None = leer) stellen."""
    status, _location = handle_update_bankaccount(
        db, {'id': [str(_cash_id(db))], 'name': ['Kasse'],
             'skr_account': ['' if number is None else str(number)]})
    assert status == 303


def test_import_constant_is_the_skr03_cash_range_and_the_clearing_account():
    """Festgeschrieben: 1000–1099 und 1460 – 1600 gehört nicht dazu (es zählt nur über das eigene
    Kassenkonto, siehe ``get_payment_coa_ids``)."""
    assert DEFAULT_LIQUID_ACCOUNTS == frozenset(range(1000, 1100)) | {1460}
    assert all(number in DEFAULT_LIQUID_ACCOUNTS for number in (1000, 1050, 1099, 1460))
    assert not any(number in DEFAULT_LIQUID_ACCOUNTS
                   for number in (999, 1100, 1459, 1461, 1599, 1600, 1610, 1650, 1699, 1700, 1800))


def test_payment_accounts_are_own_accounts_plus_skr03_cash_range_plus_clearing_account(tmp_db):
    """Bank-/Kassen-COAs ∪ vorhandene Konten aus 1000–1099 ∪ 1460. 1600 ist Zahlungskonto nur,
    solange ein eigenes Konto es führt."""
    db = tmp_db
    for number in (999, 1000, 1050, 1099, 1100, 1599, 1650, 1699, 1700):
        assert db.insert_chart_of_accounts(4, number, f'Grenzkonto {number}', 'Test')
    db.insert_account('Pay-Giro', 'Owner', 'DE00PAYMENTIDS', 'BIC', 'Bank', is_cash=0,
                      skr_account=1800)
    never = {999, 1100, 1599, 1699, 1700}

    _set_cash_number(db, None)                                       # die Kasse führt nichts
    numbers = _numbers(db, db.get_payment_coa_ids())
    assert numbers == {1800, 1000, 1050, 1099, 1460}
    assert numbers.isdisjoint(never | {1600, 1610, 1650})
    with closing(db._get_connection()) as conn:
        assert db._get_payment_coa_ids(conn.cursor()) == db.get_payment_coa_ids()

    _set_cash_number(db, 1600)                                       # jetzt führt die Kasse 1600
    assert _numbers(db, db.get_payment_coa_ids()) == {1800, 1600, 1000, 1050, 1099, 1460}

    db.insert_account('Reisekasse', '', '', '', '', is_cash=1, skr_account=1650)
    numbers = _numbers(db, db.get_payment_coa_ids())
    assert numbers == {1800, 1600, 1650, 1000, 1050, 1099, 1460}      # 1610 und 1699 bleiben draußen
    assert numbers.isdisjoint(never | {1610})

    _set_cash_number(db, 1460)                                       # Kasse zurück auf das Verrechnungskonto
    assert _numbers(db, db.get_payment_coa_ids()) == {1800, 1650, 1000, 1050, 1099, 1460}


def test_payment_accounts_of_a_fresh_database_hold_its_cash_account(tmp_db):
    numbers = _numbers(tmp_db, tmp_db.get_payment_coa_ids())
    assert numbers == {1600, 1460}                                   # die Kasse und das Verrechnungskonto
    assert tmp_db.get_bank_coa_ids() == {_coa(tmp_db, 1600)}


# ── WISO-Textexport (CSV) ────────────────────────────────────────────────────

CSV_HEADER = "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER\n"


def _csv(*lines):
    """Bewegungsdaten-Export; lines: (Konto, Gegenkonto, Brutto, Referenz, Schlüssel)."""
    rows = [f'{n};02.01.2026;{konto};{gegen};Posten {ref};{ref};{brutto};{key};'
            for n, (konto, gegen, brutto, ref, key) in enumerate(lines, start=1)]
    return (CSV_HEADER + '\n'.join(rows) + '\n').encode('utf-8')


def _imported(db):
    """{Referenz: (Betrag, Steuer)} in Minor Units."""
    return {row[0]: (row[1], row[2]) for row in _query(
        db, 'SELECT DocumentNumber, Amount, TaxAmount FROM Bookings ORDER BY ID')}


def _datev(db):
    """Zeilen des DATEV-Stapels als (Umsatz, S/H, Konto, Gegenkonto, BU), über den echten Handler."""
    result = handle_datev_export(db, {'date_from': [D1], 'date_to': [D2]})
    assert isinstance(result[0], bytes), result
    lines = [line for line in result[0].decode('cp1252').split('\r\n') if line]
    rows = [next(csv.reader([line], delimiter=';', quotechar='"')) for line in lines[2:]]
    return [(r[0], r[1], r[6], r[7], r[8]) for r in rows]


def _minor(value):
    return None if value is None else to_minor(Decimal(value))


def test_csv_cash_bookings_on_1600_of_a_fresh_database_get_the_sign_of_the_money_flow(tmp_db):
    """Frische Datenbank: Die Kasse führt 1600, also ist 1600 ein Zahlungskonto. Ausgabe: Aufwand an
    Kasse (negativ), Einnahme: Kasse an Erlös (positiv) – beide zählen genau einmal in der EÜR."""
    db = tmp_db
    result = db.import_wiso_csv(_csv(('6815', '1600', '18,35', 'KS-001', '401'),
                                     ('1600', '4400', '50,00', 'KS-002', '401')))

    assert result['imported'] == 2 and not result['errors'], result
    assert _imported(db) == {'KS-001': (_minor('-18.35'), _minor('-2.93')),
                             'KS-002': (_minor('50.00'), _minor('7.98'))}
    assert _euer(db) == {1406: Decimal('-2.93'), 6815: Decimal('-15.42'),
                         3806: Decimal('7.98'), 4400: Decimal('42.02')}
    totals = db.get_dashboard_totals(D1, D2)
    assert (totals['income'], totals['expense'], totals['bank_count']) == (50.0, -18.35, 2)
    assert _datev(db) == [('18,35', 'S', '6815', '1600', '9'), ('50,00', 'S', '1600', '4400', '3')]


# Kassenposten wie in einem WISO-Export: Ausgabe und Einnahme über 1600, dazu ein Satz über 1050
# (SKR03-Kassenbereich, bleibt liquide) und einer über 1650 (kein Zahlungskonto, solange kein
# eigenes Konto es führt)
OLD_CASH_CSV = _csv(('6815', '1600', '18,35', 'KS-001', '401'),
                    ('1600', '4400', '50,00', 'KS-002', '401'),
                    ('6815', '1050', '10,00', 'KS-003', ''),
                    ('6815', '1650', '10,00', 'KS-004', ''))


@pytest.mark.parametrize('cash_number', OLD_PARAMS)
def test_csv_booking_on_1600_keeps_the_raw_amount_when_no_cash_account_carries_1600(
        tmp_path, cash_number):
    """Datenbank mit Kasse ohne Nummer oder mit 1460: 1600 ist KEIN Zahlungskonto, „6815 an 1600“
    behält den Betrag der Datei – Beträge, DATEV-Zeilen und EÜR sind dieselben wie in 2.0.3
    (gemessen an ``git archive 4d535cf`` mit demselben Kontenrahmen). Ein erneuter Import
    derselben Datei legt nichts neu an."""
    db = Database(db_name=str(tmp_path / 'buch.db'))
    for number in (1050, 1650):
        db.insert_chart_of_accounts(4, number, f'Konto {number}', 'Test')
    _set_cash_number(db, cash_number)

    result = db.import_wiso_csv(OLD_CASH_CSV)

    assert result['imported'] == 4 and not result['errors'], result
    assert _imported(db) == {'KS-001': (_minor('18.35'), _minor('2.93')),      # Rohbetrag, positiv
                             'KS-002': (_minor('50.00'), _minor('7.98')),
                             'KS-003': (_minor('-10.00'), None),                # 1050 bleibt liquide
                             'KS-004': (_minor('10.00'), None)}
    assert _datev(db) == [('18,35', 'S', '6815', '1600', '3'), ('50,00', 'S', '1600', '4400', '3'),
                          ('10,00', 'S', '6815', '1050', '0'), ('10,00', 'S', '6815', '1650', '0')]
    assert _euer(db) == {}                                  # keine Kasse führt 1600: in keiner Sektion
    assert db.get_dashboard_totals(D1, D2)['bank_count'] == 0

    again = db.import_wiso_csv(OLD_CASH_CSV)

    assert (again['imported'], again['skipped']) == (0, 4)
    assert _query(db, 'SELECT COUNT(*) FROM Bookings') == [(4,)]


@pytest.mark.parametrize('number, liquid', [
    (999, False), (1000, True), (1050, True), (1099, True), (1100, False),      # SKR03-Kasse
    (1459, False), (1460, True), (1461, False),
    (1599, False), (1600, False), (1650, False), (1699, False), (1700, False),
])
def test_csv_sign_follows_the_skr03_cash_range_and_the_clearing_account(tmp_db, number, liquid):
    """Kasse ohne SKR-Nummer: Nur der feste Bereich (1000–1099, 1460) entscheidet. Ist das Gegenkonto
    liquide, ist die Ausgabe negativ; sonst bleibt der Betrag, wie die Datei ihn nennt."""
    db = tmp_db
    _set_cash_number(db, None)
    if _count_chart(db, number) == 0:
        assert db.insert_chart_of_accounts(4, number, f'Grenzkonto {number}', 'Test')

    db.import_wiso_csv(_csv(('6815', str(number), '10,00', f'GR-{number}', '')))

    assert _imported(db)[f'GR-{number}'][0] == _minor('-10.00' if liquid else '10.00')


@pytest.mark.parametrize('number, liquid', [(1000, True), (1050, True), (1100, False), (1600, False)])
def test_csv_sign_of_a_cash_account_in_the_first_column(tmp_db, number, liquid):
    """Konto liquide, Gegenkonto Zweckkonto: Der Betrag wird positiv (Einnahme). Steht ein
    Konto vorne, das nicht liquide ist (hier 1600 ohne eigenes Kassenkonto), bleibt der Betrag, wie
    er in der Datei steht."""
    db = tmp_db
    _set_cash_number(db, None)
    if _count_chart(db, number) == 0:
        db.insert_chart_of_accounts(4, number, f'Grenzkonto {number}', 'Test')

    db.import_wiso_csv(_csv((str(number), '4400', '-10,00', f'KV-{number}', '')))

    assert _imported(db)[f'KV-{number}'][0] == _minor('10.00' if liquid else '-10.00')


@pytest.mark.parametrize('own_number', [1600, 1610, 1650])
def test_csv_own_cash_account_numbers_are_liquid_wherever_they_lie(tmp_db, own_number):
    """Eine Kasse bestimmt wie ein Bankkonto das Vorzeichen – mit ihrer eigenen Nummer, auch 1600 und
    Nummern außerhalb des festen Bereichs; ohne eigenes Konto bleibt der Betrag, wie er ist."""
    db = tmp_db
    if _count_chart(db, own_number) == 0:
        db.insert_chart_of_accounts(4, own_number, f'Konto {own_number}', 'Test')
    _set_cash_number(db, None)
    db.import_wiso_csv(_csv(('6815', str(own_number), '10,00', 'OHNE-1', '')))
    _set_cash_number(db, own_number)

    db.import_wiso_csv(_csv(('6815', str(own_number), '20,00', 'EIGEN-1', '')))

    assert _imported(db)['OHNE-1'][0] == _minor('10.00')
    assert _imported(db)['EIGEN-1'][0] == _minor('-20.00')


# ── WISO-Datenbank ───────────────────────────────────────────────────────────

def _wiso_database(path, rows, extra_chart=()):
    """Synthetische WISO-Datenbank: Kontenrahmen (SKR03 → SKR04) und Buchungssätze ``rows``."""
    builder = FdbBuilder(page_size=16384)
    chart = builder.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4), Column('SKR04', LONG, 4), Column('BOOKINGYEAR', SHORT, 2),
        Column('ACCOUNTTEXT', TEXT, 40)])
    for skr03, skr04, text in ((1000, 1600, 'Kasse'), (1360, 1460, 'Verrechnungskonto'),
                               (4980, 6850, 'Betriebsbedarf'), (8400, 4400, 'Erlöse 19 %'),
                               *extra_chart):
        chart.add(ID=skr03, SKR04=skr04, BOOKINGYEAR=2026, ACCOUNTTEXT=text)
    bookings = builder.table('MOV_FINACC_ACCRECORDS', [
        Column('ID', LONG, 4), Column('ACCOUNTINGID', LONG, 4), Column('ACCOUNTING_DATE', TIMESTAMP, 8),
        Column('AMOUNTNET', INT64, 8, scale=-2), Column('AMOUNTGROSS', INT64, 8, scale=-2),
        Column('TAXRATE', INT64, 8, scale=-2), Column('ACCOUNTNO', LONG, 4),
        Column('CONTRA_ACCOUNTNO', LONG, 4), Column('ACCOUNTING_TEXT', TEXT, 60),
        Column('REFERENCENO', TEXT, 20)])
    for number, row in enumerate(rows, start=1):
        bookings.add(ID=number, ACCOUNTINGID=700 + number, ACCOUNTING_DATE='2026-01-02 00:00:00',
                     **row)
    return builder.write(str(path))


# Kassenposten der WISO-Datenbank: Ausgabe und Einnahme über SKR03 1000 (→ SKR04 1600) und ein Satz über
# SKR03 1040 (→ SKR04 1050, im festen Kassenbereich)
OLD_CASH_ROWS = [
    dict(AMOUNTNET=15.42, AMOUNTGROSS=18.35, TAXRATE=19.0, ACCOUNTNO=4980, CONTRA_ACCOUNTNO=1000,
         ACCOUNTING_TEXT='Kassenposten Ausgabe', REFERENCENO='KD-001'),
    dict(AMOUNTNET=42.02, AMOUNTGROSS=50.0, TAXRATE=19.0, ACCOUNTNO=1000, CONTRA_ACCOUNTNO=8400,
         ACCOUNTING_TEXT='Kassenposten Einnahme', REFERENCENO='KD-002'),
    dict(AMOUNTGROSS=10.0, ACCOUNTNO=4980, CONTRA_ACCOUNTNO=1040, ACCOUNTING_TEXT='Posten 1050',
         REFERENCENO='KD-003'),
]
OLD_CASH_CHART = [(1040, 1050, 'Konto 1050')]


def test_wiso_database_cash_bookings_on_1600_of_a_fresh_database_get_the_sign_of_the_money_flow(
        tmp_path):
    """SKR03 1000 (Kasse) wird zu SKR04 1600. Frische Datenbank: Die Kasse führt 1600 – Ausgabe: Aufwand
    an Kasse; Einnahme: Kasse an Erlös."""
    db = Database(db_name=str(tmp_path / 'buch.db'))
    path = _wiso_database(tmp_path / 'db1.fdb', OLD_CASH_ROWS, OLD_CASH_CHART)

    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)

    assert result['imported'] == 3 and not result['errors'], result
    assert _imported(db) == {'KD-001': (_minor('-18.35'), _minor('-2.93')),
                             'KD-002': (_minor('50.00'), _minor('7.98')),
                             'KD-003': (_minor('-10.00'), None)}
    rows = _query(db, 'SELECT c.AccountNumber, g.AccountNumber FROM Bookings b '
                      'JOIN ChartOfAccounts c ON c.ID = b.COA_ID '
                      'JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID ORDER BY b.ID')
    assert rows == [(6850, 1600), (1600, 4400), (6850, 1050)]
    assert _euer(db) == {1406: Decimal('-2.93'), 6850: Decimal('-15.42'),
                         3806: Decimal('7.98'), 4400: Decimal('42.02')}
    totals = db.get_dashboard_totals(D1, D2)
    assert (totals['income'], totals['expense'], totals['bank_count']) == (50.0, -18.35, 2)
    assert _datev(db) == [('18,35', 'S', '6850', '1600', '9'), ('50,00', 'S', '1600', '4400', '3'),
                          ('10,00', 'S', '6850', '1050', '0')]


@pytest.mark.parametrize('cash_number', OLD_PARAMS)
def test_wiso_database_booking_on_1600_keeps_the_raw_amount_when_no_cash_account_carries_1600(
        tmp_path, cash_number):
    """Wie beim Textexport: Kasse ohne Nummer oder mit 1460 → SKR04 1600 ist kein Zahlungskonto, der
    Rohbetrag bleibt (Beträge und DATEV-Zeilen wie in 2.0.3); die Sätze über 1050 bleiben liquide."""
    db = Database(db_name=str(tmp_path / 'buch.db'))
    _set_cash_number(db, cash_number)
    path = _wiso_database(tmp_path / 'db1.fdb', OLD_CASH_ROWS, OLD_CASH_CHART)

    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)

    assert result['imported'] == 3 and not result['errors'], result
    assert _imported(db) == {'KD-001': (_minor('18.35'), _minor('2.93')),
                             'KD-002': (_minor('50.00'), _minor('7.98')),
                             'KD-003': (_minor('-10.00'), None)}
    assert _datev(db) == [('18,35', 'S', '6850', '1600', '3'), ('50,00', 'S', '1600', '4400', '3'),
                          ('10,00', 'S', '6850', '1050', '0')]
    assert _euer(db) == {}
    again = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)
    assert (again['imported'], again['skipped']) == (0, 3)


@pytest.mark.parametrize('cash_number, skr03, skr04, liquid', [
    (None, 1000, 1600, False), (None, 1010, 1699, False), (None, 1020, 1700, False),
    (None, 1030, 1599, False), (None, 1040, 1050, True), (None, 1360, 1460, True),
    (1600, 1000, 1600, True), (1699, 1010, 1699, True), (1699, 1000, 1600, False),
], ids=['1600-ohne-kasse', '1699-ohne-kasse', '1700', '1599', 'skr03-bereich', 'verrechnung-1460',
        '1600-mit-kasse', '1699-mit-kasse', '1600-bei-kasse-1699'])
def test_wiso_database_sign_follows_the_fixed_numbers_and_the_own_cash_account(
        tmp_path, cash_number, skr03, skr04, liquid):
    db = Database(db_name=str(tmp_path / 'buch.db'))
    if cash_number == 1699:
        db.insert_chart_of_accounts(4, 1699, 'Nebenkasse 99', 'Test')
    _set_cash_number(db, cash_number)
    path = _wiso_database(tmp_path / 'db1.fdb', [
        dict(AMOUNTGROSS=10.0, ACCOUNTNO=4980, CONTRA_ACCOUNTNO=skr03, ACCOUNTING_TEXT='Posten',
             REFERENCENO='GR-1')], extra_chart=[(1010, 1699, 'Nebenkasse 99'), (1020, 1700, 'Konto 1700'),
                                                (1030, 1599, 'Konto 1599'), (1040, 1050, 'Konto 1050')])

    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)

    assert result['imported'] == 1 and not result['errors'], result
    assert _imported(db)['GR-1'][0] == _minor('-10' if liquid else '10')


# ── Demo ─────────────────────────────────────────────────────────────────────

CASH_BOOKING = "SELECT a.Name, c.AccountNumber, g.AccountNumber, b.Amount FROM Bookings b " \
               "JOIN Accounts a ON a.ID = b.Account_ID JOIN ChartOfAccounts c ON c.ID = b.COA_ID " \
               "JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID WHERE b.DocumentNumber = 'KASSE-001'"


def test_test_data_cash_booking_lives_on_1610_in_the_demo(tmp_path):
    """Die Demo stellt die Kasse der Testdaten (Verrechnungskonto 1460) als Portokasse auf die
    Nebenkasse 1610; die Buchung der Testdaten zieht mit und steht in EÜR und Dashboard genau
    einmal. Die automatisch angelegte Kasse behält 1600."""
    db = Database(str(tmp_path / 'demo.db'))
    db.load_test_seed_data(include_invoices=False)
    assert dict(_cash_accounts(db)) == {'Kasse': 1600, 'Testkasse': 1460}
    assert _query(db, CASH_BOOKING) == [('Testkasse', 6815, 1460, to_minor(Decimal('-18.35')))]

    scenario = demo_seed.DemoScenario(db, today=datetime.date(2026, 11, 3))   # benennt die Konten um
    scenario.load_expenses()          # Ausgaben und Splits der Demo; Rechnungen und PDFs braucht der Fall nicht

    assert dict(_cash_accounts(db)) == {'Kasse': 1600, 'Portokasse': 1610}
    assert _query(db, CASH_BOOKING) == [('Portokasse', 6815, 1610, to_minor(Decimal('-18.35')))]
    # 2022 enthält nur diese Buchung (das Szenario reicht zwei Jahre zurück): einmal in der EÜR …
    assert _euer(db, '2022-01-01', '2022-12-31') == {1406: Decimal('-2.93'), 6815: Decimal('-15.42')}
    totals = db.get_dashboard_totals('2022-01-01', '2022-12-31')
    assert (totals['expense'], totals['income'], totals['bank_count']) == (-18.35, 0, 1)
    # … und das Verrechnungskonto kommt in der ganzen Demo nicht mehr vor
    assert _query(db, 'SELECT COUNT(*) FROM Bookings b WHERE b.COA_ID = ? OR b.CounterCOA_ID = ?',
                  (_coa(db, 1460), _coa(db, 1460))) == [(0,)]


def test_demo_rename_moves_only_the_bookings_of_the_renamed_account(tmp_path):
    """Nur Buchungen, die auf dem umbenannten Konto stehen und die alte Nummer führen, ziehen mit;
    ein Satz auf dem Girokonto mit 1460 als Gegenkonto bleibt."""
    db = Database(str(tmp_path / 'demo.db'))
    db.load_test_seed_data(include_invoices=False)
    giro = _cash_id(db, 'Testbank')
    other = db.insert_booking('2026-03-01', Decimal('-5'), account_id=giro, coa_id=_coa(db, 6815),
                              counter_coa_id=_coa(db, 1460), booking_type='entry')

    demo_seed.rename_demo_accounts(db, {'Testkasse': {'name': 'Portokasse', 'skr_account': 1610}})
    demo_seed.rename_demo_accounts(db, {'Testkasse': {'name': 'Portokasse', 'skr_account': 1610}})

    assert dict(_cash_accounts(db)) == {'Kasse': 1600, 'Portokasse': 1610}       # zweiter Lauf: nichts
    assert _query(db, 'SELECT CounterCOA_ID FROM Bookings WHERE ID = ?', (other,)) == \
           [(_coa(db, 1460),)]
    assert _query(db, CASH_BOOKING) == [('Portokasse', 6815, 1610, to_minor(Decimal('-18.35')))]
