# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kasse und Verrechnungskonto 1460 in EÜR und Dashboard (2.0.5).

Die EÜR (``get_euer_data``) nimmt dieselben Zahlungskonten wie der DATEV-Export
(``get_payment_coa_ids``: eigene Bank- und Kassenkonten – die Kasse einer frischen Datenbank
führt 1600 –, der historische Bereich 1000–1099 und das Verrechnungskonto 1460). Ob ein Satz
zählt, entscheidet die Sektion, nicht die Kontenmenge:

1. Buchungssätze unter einer Bankbewegung der gewählten Konten – auch der Kasse
   (Buchungsmaske mit Konto = Kasse);
2. eigenständige Sätze auf einer Kasse und – ohne Kontofilter – auf 1460;
2b. eigenständige Sätze mit ``Account_ID`` auf einer Kasse (über ``classify_entry``), ohne
   Umbuchungen zwischen Anlage- und Erfolgskonto;
3. ohne Kontofilter: Umbuchungen zwischen Anlage- und Erfolgskonto (AfA, Restbuchwert) – ohne
   Steuer, der Betrag gilt brutto als Aufwand.

Jede Buchung zählt höchstens einmal (die erste Sektion, die sie findet, gewinnt).

Geprüft werden die Fälle der Vorher/Nachher-Tabelle (A, A2, C, D, E1–E4, F, G, I) mit
Kasse ohne SKR-Nummer, mit 1600 und mit 1460 – das Ergebnis darf nicht von der Nummer der
Kasse abhängen –, der Kontofilter, die Doppelzählung (auch wenn der Abgleich einen Satz später
mit einer Bankbewegung verknüpft, und für Sätze, die mehrere Sektionen finden), die Umbuchungen
zwischen Anlage- und Erfolgskonto und das Dashboard (folgt den Kontobewegungen: 1460 und
AfA zählen dort nicht). Der Test für die AfA-Buchung selbst steht in
``test_depreciation_booking.py``.

Beträge werden exakt als Decimal verglichen. Alle Namen und Werte sind erfunden.
"""
import csv
import datetime
from collections import Counter, namedtuple
from contextlib import closing
from decimal import Decimal

import pytest

from db import Database
from db.matching import is_asset_transfer
from money import from_minor
from server.handlers import (
    handle_add_transaction, handle_book_depreciation, handle_datev_export,
    handle_update_bankaccount,
)
from server.pages_dashboard import PageDashboard
from tests.fdb_builder import FdbBuilder, INT64, LONG, SHORT, TEXT, TIMESTAMP, Column

D1, D2 = '2026-01-01', '2026-12-31'

# Die Kasse einer frischen Datenbank führt 1600; ältere Datenbanken haben sie ohne
# SKR-Nummer oder (alter Standard) mit dem Verrechnungskonto 1460.
KASSEN = [None, 1600, 1460]
KASSE_ID = {None: 'kasse-ohne-skr', 1600: 'kasse-1600', 1460: 'kasse-1460'}
KASSEN_PARAMS = [pytest.param(skr, id=KASSE_ID[skr]) for skr in KASSEN]


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _coa(db, number):
    """COA-ID zur Kontonummer; ``None`` bleibt ``None`` (leere Seite des Satzes)."""
    return None if number is None else db.get_coa_id_by_account_number(number)


def _query(db, sql, params=()):
    with closing(db._get_connection()) as conn:
        return conn.execute(sql, params).fetchall()


def _execute(db, sql, params=()):
    with closing(db._get_connection()) as conn:
        conn.execute(sql, params)
        conn.commit()


def _make_db(tmp_path, kasse_skr, name='buch.db'):
    """Frische Datenbank mit Girokonto (SKR 1800) und der automatisch angelegten Kasse; deren
    SKR-Nummer wird wie in den Stammdaten (``handle_update_bankaccount``) auf ``kasse_skr``
    gestellt (``None`` = leer)."""
    db = Database(db_name=str(tmp_path / name))
    db.insert_account('Cash-Giro', 'Owner', 'DE00CASHCLEAR', 'BIC', 'Bank', is_cash=0,
                      skr_account=1800)
    kasse = next(a for a in db.fetch_accounts() if a[6])
    handle_update_bankaccount(db, {'id': [str(kasse[0])], 'name': [kasse[1]],
                                   'skr_account': ['' if kasse_skr is None else str(kasse_skr)]})
    return db


def _accounts(db):
    """(Girokonto-ID, Kasse-ID)."""
    accounts = db.fetch_accounts()
    return (next(a[0] for a in accounts if a[1] == 'Cash-Giro'),
            next(a[0] for a in accounts if a[6]))


def _kasse_skr(db):
    return db.get_account_by_id(_accounts(db)[1])[7]


def _decimals(expected):
    return {nr: Decimal(value) for nr, value in expected.items()}


def _euer(db, account_ids=None, date_from=D1, date_to=D2):
    """EÜR als {Kontonummer: Betrag}; ``get_euer_data`` liefert float, hier exakt als Decimal."""
    return {nr: Decimal(str(total))
            for nr, _name, total in db.get_euer_data(date_from, date_to, account_ids)}


def _dash(db, account_ids=None, date_from=D1, date_to=D2):
    totals = db.get_dashboard_totals(date_from, date_to, account_ids)
    return {'income': Decimal(str(totals['income'])), 'private': Decimal(str(totals['private'])),
            'expense': Decimal(str(totals['expense'])), 'bank_count': totals['bank_count']}


def _monthly(db, account_ids=None):
    months = db.get_dashboard_monthly(D1, D2, account_ids)
    assert [m['year_month'] for m in months] == [f'2026-{n:02d}' for n in range(1, 13)]
    return months


def _month_sums(db, account_ids=None):
    months = _monthly(db, account_ids)
    return {key: sum((Decimal(str(m[key])) for m in months), Decimal('0'))
            for key in ('income', 'private', 'expense')}


def _mask(db, account_id, coa_number, amount, rate, tax, date='2026-03-01'):
    """Neue Buchung über die Buchungsmaske (Formular der Seite ``/transactions``)."""
    post = {'transaction_id': ['0'], 'date': [date], 'amount': [amount],
            'account': [str(account_id)], 'currency': ['EUR'], 'coa_id': [str(_coa(db, coa_number))],
            'tax_rate': [str(rate)], 'tax_amount': [tax]}
    status, _location = handle_add_transaction(db, post)
    assert status == 303


def _standalone(db, coa_nr, counter_nr, amount, rate=None, tax=None, date='2026-03-01',
                account_id=None):
    """Eigenständiger Buchungssatz (ohne Bankbewegung), wie ihn die WISO-Importe anlegen."""
    return db.insert_booking(
        date, Decimal(amount), account_id=account_id, coa_id=_coa(db, coa_nr),
        counter_coa_id=_coa(db, counter_nr), tax_rate=rate,
        tax_amount=None if tax is None else Decimal(tax), booking_type='entry')


def _giro_child(db, coa_nr, counter_nr, amount, rate=None, tax=None, date='2026-03-01'):
    """Bankbewegung auf dem Girokonto mit EINEM Buchungssatz (COA, Gegenkonto)."""
    giro, _kasse = _accounts(db)
    bank = db.insert_booking(date, Decimal(amount), account_id=giro, booking_type='bank')
    db.insert_booking(date, Decimal(amount), coa_id=_coa(db, coa_nr),
                      counter_coa_id=_coa(db, counter_nr), tax_rate=rate,
                      tax_amount=None if tax is None else Decimal(tax),
                      booking_type='entry', parent_booking_id=bank)
    return bank


def _old_depreciation(db, account_id, year=2026):
    """AfA-Buchung alter Form (bis 2.0.4): Aufwandskonto auf einem Bank-/Kassenkonto, kein
    Gegenkonto, Betrag negativ, von der AfA-Planzeile referenziert."""
    asset = db.insert_asset('Altlaptop', f'{year}-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    booking = _standalone(db, 6220, None, '-600', date=f'{year}-12-31', account_id=account_id)
    _execute(db, "UPDATE Bookings SET Status = 'posted' WHERE ID = ?", (booking,))
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, "
                 "Booking_ID, Status, BookedAt) VALUES (?, ?, 6000000, 12000000, ?, 'posted', "
                 "CURRENT_TIMESTAMP)", (asset, year, booking))
    return booking


def _datev(db):
    """Zeilen des DATEV-Stapels als (Umsatz, S/H, Konto, Gegenkonto, BU), über den echten Handler."""
    result = handle_datev_export(db, {'date_from': [D1], 'date_to': [D2]})
    assert isinstance(result[0], bytes), result
    lines = [line for line in result[0].decode('cp1252').split('\r\n') if line]
    rows = [next(csv.reader([line], delimiter=';', quotechar='"')) for line in lines[2:]]
    return [(r[0], r[1], r[6], r[7], r[8]) for r in rows]


# ── Fälle der Vorher/Nachher-Tabelle ─────────────────────────────────────────
# Case: Kennung, Aufbau der Buchungen, erwartete EÜR (Kontonummer -> Betrag), erwartetes Dashboard
# (Einnahmen, Ausgaben, Zahl der Zeilen); ``needs_skr``: der Fall braucht eine SKR-Nummer an der
# Kasse; ``dash_1460``: abweichendes Dashboard, wenn die Kasse das Verrechnungskonto führt.
Case = namedtuple('Case', 'id build euer dash needs_skr dash_1460', defaults=(False, None))


def _a(db):
    _mask(db, _accounts(db)[1], 6815, '-11.90', 19, '-1.90')


def _a2(db):
    _mask(db, _accounts(db)[1], 4400, '11.90', 19, '1.90')


def _c(db):
    _standalone(db, 6815, _kasse_skr(db), '-11.90', 0.19, '-1.90')


def _d(db):
    _standalone(db, 6815, None, '-11.90', 0.19, '-1.90', account_id=_accounts(db)[1])


def _e(coa, counter, tax=None):
    def build(db):
        _giro_child(db, coa, counter, '-119', 0.19 if tax else None, tax)
    return build


def _f(db):
    """WISO-Paar, getrennt: Aufwand an Verrechnungskonto (eigenständig) und später die
    Übertragung Verrechnungskonto an Bank als Kind der Bankbewegung."""
    _standalone(db, 6815, 1460, '-119', 0.19, '-19')
    _giro_child(db, 1460, 1800, '-119', date='2026-03-05')


def _g(db):
    """WISO-Paar, beide Sätze als Kinder derselben Bankbewegung."""
    giro, _kasse = _accounts(db)
    bank = db.insert_booking('2026-03-05', Decimal('-119'), account_id=giro, booking_type='bank')
    db.insert_booking('2026-03-05', Decimal('-119'), coa_id=_coa(db, 1460),
                      counter_coa_id=_coa(db, 1800), booking_type='entry', parent_booking_id=bank)
    db.insert_booking('2026-03-05', Decimal('-119'), coa_id=_coa(db, 6815),
                      counter_coa_id=_coa(db, 1460), tax_rate=0.19, tax_amount=Decimal('-19'),
                      booking_type='entry', parent_booking_id=bank)


def _i(db):
    _standalone(db, 6815, 1460, '-119', 0.19, '-19', account_id=_accounts(db)[1])


def _wiso_depreciation(db):
    _standalone(db, 6222, 520, '600', date='2026-12-31')


def _book_value_loss(db):
    """Restbuchwert beim Abgang: Aufwand 6895 an Anlagekonto."""
    _standalone(db, 6895, 520, '1200', date='2026-06-30')


def _wiso_depreciation_on_cash(db):
    """Dieselbe Umbuchung mit Konto = Kasse (die Maske kann ein Konto eintragen)."""
    _standalone(db, 6222, 520, '600', date='2026-12-31', account_id=_accounts(db)[1])


def _asset_purchase(db):
    _giro_child(db, 520, 1800, '-1190', 0.19, '-190')


def _old_depreciation_giro(db):
    _old_depreciation(db, _accounts(db)[0])


def _old_depreciation_kasse(db):
    _old_depreciation(db, _accounts(db)[1])


def _two_sided_2b(db):
    _standalone(db, 6815, 3150, '100', account_id=_accounts(db)[1])


CASES = [
    Case('A-maske-kasse-6815-ausgabe', _a, {1406: '-1.90', 6815: '-10.00'}, ('0', '-11.90', 1)),
    Case('A2-maske-kasse-4400-einnahme', _a2, {3806: '1.90', 4400: '10.00'}, ('11.90', '0', 1)),
    Case('C-eigenstaendig-6815-an-kasse', _c, {1406: '-1.90', 6815: '-10.00'},
         ('0', '-11.90', 1), True),
    Case('D-konto-kasse-gegenkonto-leer', _d, {1406: '-1.90', 6815: '-10.00'},
         ('0', '-11.90', 1)),
    Case('E1-giro-kind-6815-an-1460', _e(6815, 1460, '-19'), {1406: '-19', 6815: '-100'},
         ('0', '-119', 1)),
    Case('E2-giro-kind-4400-an-1460', _e(4400, 1460, '-19'), {3806: '-19', 4400: '-100'},
         ('0', '-119', 1)),
    Case('E3-giro-kind-1460-vorne-leer', _e(1460, None, '-19'), {}, ('0', '-119', 1)),
    Case('E4-giro-kind-1460-an-1800', _e(1460, 1800), {}, ('0', '-119', 1)),
    Case('F-wiso-paar-getrennt', _f, {1406: '-19', 6815: '-100'}, ('0', '-119', 1), False,
         ('0', '-238', 2)),
    Case('G-wiso-paar-als-kinder', _g, {1406: '-19', 6815: '-100'}, ('0', '-119', 1)),
    Case('I-konto-kasse-6815-an-1460', _i, {1406: '-19', 6815: '-100'}, ('0', '-119', 1)),
    Case('wiso-afa-6222-an-0520', _wiso_depreciation, {6222: '-600'}, ('0', '0', 0)),
    Case('wiso-afa-6222-an-0520-konto-kasse', _wiso_depreciation_on_cash, {6222: '-600'}, None),
    Case('restbuchwert-6895-an-0520', _book_value_loss, {6895: '-1200'}, ('0', '0', 0)),
    Case('anlagenkauf-bank-kind-0520-an-1800', _asset_purchase, {520: '-1000', 1406: '-190'},
         ('0', '-1190', 1)),
    Case('afa-altbestand-giro', _old_depreciation_giro, {6220: '-600'}, ('0', '0', 0)),
    Case('afa-altbestand-kasse', _old_depreciation_kasse, {6220: '-600'}, ('0', '0', 0)),
]
TABLE_PARAMS = [
    pytest.param(skr, case, id=f'{case.id}-{KASSE_ID[skr]}')
    for case in CASES for skr in KASSEN if not (case.needs_skr and skr is None)
]


@pytest.mark.parametrize('kasse_skr, case', TABLE_PARAMS)
def test_euer_and_dashboard_of_every_form_do_not_depend_on_the_cash_account_number(
        tmp_path, kasse_skr, case):
    db = _make_db(tmp_path, kasse_skr)
    case.build(db)

    assert _euer(db) == _decimals(case.euer)

    if case.dash is None:
        return                        # das Dashboard ist für diesen Fall nicht festgelegt
    income, expense, count = case.dash_1460 if (kasse_skr == 1460 and case.dash_1460) else case.dash
    totals = _dash(db)
    assert (totals['income'], totals['expense'], totals['bank_count']) == \
           (Decimal(income), Decimal(expense), count)
    assert totals['private'] == 0
    # Die Monatswerte sind dieselbe Abfrage: ihre Summe ist die Summe der Karten
    assert _month_sums(db) == {key: totals[key] for key in ('income', 'private', 'expense')}


def test_pair_f_is_counted_twice_on_the_dashboard_when_the_cash_account_is_1460(tmp_path):
    """BEKANNTE EINSCHRÄNKUNG (docs/aufgaben.md): Führt die Kasse das Verrechnungskonto, zählt das
    Dashboard den eigenständigen Teil „6815 an 1460“ als Kassenbewegung und dazu die Bankzeile.
    Die EÜR stimmt trotzdem. Behoben wird es, indem die Kasse auf 1600 steht – hier nur
    festgeschrieben, damit eine Änderung auffällt."""
    db = _make_db(tmp_path, 1460)
    _f(db)
    assert _euer(db) == {1406: Decimal('-19'), 6815: Decimal('-100')}
    assert _dash(db)['expense'] == Decimal('-238')
    assert _dash(db)['bank_count'] == 2


@pytest.mark.parametrize('kasse_skr', [None, 1600], ids=['kasse-ohne-skr', 'kasse-1600'])
def test_pair_g_keeps_the_split_balanced_unless_the_cash_account_is_1460(tmp_path, kasse_skr):
    """Beide Sätze unter derselben Bankbewegung ergeben zusammen die Bankbewegung: kein Fehlbetrag,
    der DATEV-Export ist nicht gesperrt."""
    db = _make_db(tmp_path, kasse_skr)
    _g(db)
    assert db.find_unbalanced_splits(D1, D2) == []
    assert len(_datev(db)) == 2


# ── Buchungsmaske mit Konto = Kasse (A, A2) ──────────────────────────────────

@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_mask_booking_on_the_cash_account_creates_bank_row_with_one_child(tmp_path, kasse_skr):
    """Voraussetzung der Fälle A/A2: Die Maske legt eine Bankbewegung auf der Kasse an, ihr Kind
    trägt kein Konto und als Gegenkonto die SKR-Nummer der Kasse (oder keines)."""
    db = _make_db(tmp_path, kasse_skr)
    _giro, kasse = _accounts(db)
    _mask(db, kasse, 6815, '-11.90', 19, '-1.90')

    bank = _query(db, "SELECT ID, Account_ID, Amount FROM Bookings WHERE BookingType = 'bank'")
    assert [(b[1]) for b in bank] == [kasse]
    kids = _query(db, "SELECT BookingType, Account_ID, COA_ID, CounterCOA_ID, Amount, TaxRate, "
                      "TaxAmount FROM Bookings WHERE ParentBooking_ID = ?", (bank[0][0],))
    assert len(kids) == 1
    kind, account, coa, counter, amount, rate, tax = kids[0]
    assert (kind, account, coa, counter) == ('entry', None, _coa(db, 6815), _coa(db, kasse_skr))
    assert (from_minor(amount), rate, from_minor(tax)) == (Decimal('-11.90'), 0.19, Decimal('-1.90'))


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_dashboard_months_show_the_cash_booking_in_its_month(tmp_path, kasse_skr):
    db = _make_db(tmp_path, kasse_skr)
    kasse = _accounts(db)[1]
    _mask(db, kasse, 6815, '-11.90', 19, '-1.90', date='2026-03-01')
    _mask(db, kasse, 4400, '23.80', 19, '3.80', date='2026-05-20')

    months = {m['year_month']: m for m in _monthly(db)}
    assert (months['2026-03']['expense'], months['2026-03']['income']) == (-11.9, 0.0)
    assert (months['2026-05']['expense'], months['2026-05']['income']) == (0.0, 23.8)
    assert all(m['expense'] == m['income'] == 0.0
               for key, m in months.items() if key not in ('2026-03', '2026-05'))


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_private_withdrawal_from_the_cash_account_is_private_and_not_an_expense(tmp_path,
                                                                                kasse_skr):
    """Eine Entnahme (2100) über die Kasse steht in der EÜR auf 2100 und zählt im Dashboard als
    Privatentnahme – weder als Ausgabe noch als Einnahme."""
    db = _make_db(tmp_path, kasse_skr)
    _mask(db, _accounts(db)[1], 2100, '-50.00', '', '')

    assert _euer(db) == {2100: Decimal('-50')}
    assert _dash(db) == {'income': 0, 'private': Decimal('-50'), 'expense': 0, 'bank_count': 1}
    assert _month_sums(db) == {'income': 0, 'private': Decimal('-50'), 'expense': 0}


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_cash_booking_follows_editing_and_deleting_it(tmp_path, kasse_skr):
    """Die Kassenbuchung bleibt eine Bankbewegung mit einem Kind: Bearbeiten in der Maske zieht den
    Buchungssatz mit (die EÜR zählt weiter genau einmal), Löschen nimmt sie aus EÜR und Dashboard."""
    db = _make_db(tmp_path, kasse_skr)
    kasse = _accounts(db)[1]
    _mask(db, kasse, 6815, '-11.90', 19, '-1.90')
    bank = _query(db, "SELECT ID FROM Bookings WHERE BookingType = 'bank'")[0][0]

    status, _location = handle_add_transaction(db, {
        'transaction_id': [str(bank)], 'date': ['2026-03-01'], 'amount': ['-23.80'],
        'account': [str(kasse)], 'currency': ['EUR'], 'coa_id': [str(_coa(db, 6815))],
        'tax_rate': ['19'], 'tax_amount': ['-3.80']})

    assert status == 303
    assert _euer(db) == {1406: Decimal('-3.80'), 6815: Decimal('-20')}
    assert (_dash(db)['expense'], _dash(db)['bank_count']) == (Decimal('-23.80'), 1)
    assert _query(db, "SELECT COUNT(*) FROM Bookings WHERE ParentBooking_ID = ?", (bank,)) == [(1,)]

    db.delete_transaction(bank)

    assert _euer(db) == {}
    assert _dash(db) == {'income': 0, 'private': 0, 'expense': 0, 'bank_count': 0}


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_datev_of_cash_mask_bookings_is_unchanged(tmp_path, kasse_skr):
    """Der DATEV-Stapel zeigt die Kassenbuchung wie vorher: Zweckkonto im Soll (Ausgabe) bzw. Haben
    (Einnahme), das Gegenkonto ist die SKR-Nummer der Kasse (leer ohne)."""
    db = _make_db(tmp_path, kasse_skr)
    kasse = _accounts(db)[1]
    _mask(db, kasse, 6815, '-11.90', 19, '-1.90')
    _mask(db, kasse, 4400, '11.90', 19, '1.90', date='2026-03-02')
    counter = '' if kasse_skr is None else str(kasse_skr)
    assert _datev(db) == [('11,90', 'S', '6815', counter, '9'), ('11,90', 'H', '4400', counter, '3')]


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_dashboard_page_lists_cash_booking_and_depreciation_in_the_euer(tmp_path, kasse_skr):
    """Die EÜR-Ansicht des Dashboards zeigt die Kassenbuchung unter den Ausgaben, die AfA als
    Aufwand (nicht das Anlagekonto), und rechnet beide ins Ergebnis."""
    db = _make_db(tmp_path, kasse_skr)
    _mask(db, _accounts(db)[1], 6815, '-11.90', 19, '-1.90')
    asset = db.insert_asset('Dash-Laptop', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    handle_book_depreciation(db, {'asset_id': [str(asset)], 'year': ['2026'],
                                  'coa_id': [str(_coa(db, 6220))]})

    html = PageDashboard(db, D1, D2)

    expenses = html[html.index('Summe Betriebseinnahmen'):]
    for account in ('6815', '6220', '1406'):
        assert f'<td>{account}</td>' in expenses
    assert '<td>520</td>' not in html
    assert '-611,90' in html                         # 10 + 1,90 + 600 EUR als Ausgaben


# ── Kontofilter ──────────────────────────────────────────────────────────────

def _filter_data(db):
    """Girokonto-Einnahme, Kassenbuchung über die Maske, eigenständiger Satz auf 1460 und zwei
    Umbuchungen Aufwand an Anlagekonto (WISO-Form und AfA-Buchung des Programms)."""
    _giro, kasse = _accounts(db)
    _giro_child(db, 4400, 1800, '119.00', 0.19, '19.00', date='2026-03-02')
    _mask(db, kasse, 6815, '-11.90', 19, '-1.90', date='2026-03-03')
    _standalone(db, 6805, 1460, '-50.00', date='2026-03-04')
    _wiso_depreciation(db)
    asset = db.insert_asset('Filter-Laptop', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    handle_book_depreciation(db, {'asset_id': [str(asset)], 'year': ['2026'],
                                  'coa_id': [str(_coa(db, 6220))]})


GIRO_ROWS = {3806: '19', 4400: '100'}
KASSE_ROWS = {1406: '-1.90', 6815: '-10'}
CLEARING_ROWS = {6805: '-50'}
TRANSFER_ROWS = {6222: '-600', 6220: '-600'}


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_account_filter_selects_accounts_and_leaves_out_unassigned_records(tmp_path, kasse_skr):
    """Sektion 3 und die 1460-Sätze hängen an keinem Konto: Sie zählen nur ohne Filter – außer die
    Kasse führt selbst 1460, dann ist der Satz ein Kassensatz und folgt dem Filter der Kasse."""
    db = _make_db(tmp_path, kasse_skr)
    giro, kasse = _accounts(db)
    _filter_data(db)

    everything = {**GIRO_ROWS, **KASSE_ROWS, **CLEARING_ROWS, **TRANSFER_ROWS}
    assert _euer(db) == _decimals(everything)

    cash_clearing = CLEARING_ROWS if kasse_skr == 1460 else {}
    assert _euer(db, [giro]) == _decimals(GIRO_ROWS)
    assert _euer(db, [kasse]) == _decimals({**KASSE_ROWS, **cash_clearing})


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_all_accounts_selected_is_the_same_as_no_filter(tmp_path, kasse_skr):
    db = _make_db(tmp_path, kasse_skr)
    giro, kasse = _accounts(db)
    _filter_data(db)

    expected = db.get_euer_data(D1, D2)
    assert expected
    assert db.get_euer_data(D1, D2, [giro, kasse]) == expected
    assert db.get_euer_data(D1, D2, [kasse, giro]) == expected
    assert db.get_euer_data(D1, D2, [giro, kasse, giro]) == expected
    assert db.get_euer_data(D1, D2, []) == expected                  # leere Auswahl = kein Filter
    assert _dash(db, [giro, kasse]) == _dash(db) == _dash(db, [])


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_selection_without_an_existing_account_shows_nothing(tmp_path, kasse_skr):
    db = _make_db(tmp_path, kasse_skr)
    _filter_data(db)
    assert db.get_euer_data(D1, D2, [9999]) == []
    assert _dash(db, [9999]) == {'income': 0, 'private': 0, 'expense': 0, 'bank_count': 0}
    assert db.get_dashboard_monthly(D1, D2, [9999]) == []


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_dashboard_follows_the_account_filter(tmp_path, kasse_skr):
    """Das Dashboard zählt Kontobewegungen: Das Verrechnungskonto 1460 zählt nur, wenn es die
    Kasse selbst führt, und die AfA nie."""
    db = _make_db(tmp_path, kasse_skr)
    giro, kasse = _accounts(db)
    _filter_data(db)

    clearing = Decimal('-50') if kasse_skr == 1460 else 0
    extra = 1 if kasse_skr == 1460 else 0
    assert _dash(db, [giro]) == {'income': Decimal('119'), 'private': 0, 'expense': 0,
                                 'bank_count': 1}
    assert _dash(db, [kasse]) == {'income': 0, 'private': 0,
                                  'expense': Decimal('-11.90') + clearing, 'bank_count': 1 + extra}
    assert _dash(db) == {'income': Decimal('119'), 'private': 0,
                         'expense': Decimal('-11.90') + clearing, 'bank_count': 2 + extra}
    for ids in ([giro], [kasse], None):
        month = _month_sums(db, ids)
        totals = _dash(db, ids)
        assert month == {key: totals[key] for key in month}


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_cash_account_entry_follows_the_cash_filter(tmp_path, kasse_skr):
    """Fall D (Konto = Kasse, Gegenkonto leer) und Fall I (Konto = Kasse, Satz 6815 an 1460):
    gleiche Zahlen mit Filter „nur Kasse“ und ohne Filter, nichts mit „nur Giro“."""
    db = _make_db(tmp_path, kasse_skr)
    giro, kasse = _accounts(db)
    _d(db)
    _i(db)
    expected = {1406: Decimal('-20.90'), 6815: Decimal('-110')}
    assert _euer(db) == expected
    assert _euer(db, [kasse]) == expected
    assert _euer(db, [giro]) == {}


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_wiso_depreciation_counts_only_without_a_filter(tmp_path, kasse_skr):
    db = _make_db(tmp_path, kasse_skr)
    giro, kasse = _accounts(db)
    _wiso_depreciation(db)
    assert _euer(db) == {6222: Decimal('-600')}
    assert _euer(db, [giro, kasse]) == {6222: Decimal('-600')}
    assert _euer(db, [giro]) == {}
    assert _euer(db, [kasse]) == {}


# ── Keine Doppelzählung ──────────────────────────────────────────────────────

@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_euer_of_all_forms_together_is_the_sum_of_the_single_cases(tmp_path, kasse_skr):
    """Derselbe Satz erscheint nie in zwei Sektionen: Die EÜR (und das Dashboard) einer Datenbank mit
    allen Formen ist die Summe der Einzelfälle."""
    cases = [case for case in CASES if not (case.needs_skr and kasse_skr is None)]
    cases.append(Case('2b-zwei-seiten', _two_sided_2b, {3150: '100', 6815: '-100'}, None))

    euer_sum, dash_sum = Counter(), Counter()
    for number, case in enumerate(cases):
        single = _make_db(tmp_path, kasse_skr, f'einzel{number}.db')
        case.build(single)
        euer_sum.update(_euer(single))
        dash_sum.update(_dash(single))

    together = _make_db(tmp_path, kasse_skr, 'zusammen.db')
    for case in cases:
        case.build(together)

    assert _euer(together) == {nr: value for nr, value in euer_sum.items() if value}
    assert _dash(together) == dict(dash_sum)


STANDALONE_FORMS = {
    # Kennung -> (Aufbau, Kasse muss eine SKR-Nummer führen)
    'verrechnungskonto': (lambda db: _standalone(db, 6815, 1460, '-119', 0.19, '-19',
                                                 date='2026-03-05'), False),
    'kasse-skr': (lambda db: _standalone(db, 6815, _kasse_skr(db), '-119', 0.19, '-19',
                                         date='2026-03-05'), True),
    'kasse-konto': (lambda db: _standalone(db, 6815, None, '-119', 0.19, '-19', date='2026-03-05',
                                           account_id=_accounts(db)[1]), False),
}
LINK_PARAMS = [pytest.param(skr, form, id=f'{form}-{KASSE_ID[skr]}')
               for form, (_build, needs_skr) in STANDALONE_FORMS.items()
               for skr in KASSEN if not (needs_skr and skr is None)]
PAID = {1406: Decimal('-19'), 6815: Decimal('-100')}


@pytest.mark.parametrize('kasse_skr, form', LINK_PARAMS)
def test_standalone_entry_counts_once_before_and_after_the_matching_links_it(
        tmp_path, kasse_skr, form):
    """Ein eigenständiger Satz (Sektion 2/2b) wandert mit der Verknüpfung in Sektion 1 und zählt
    vorher wie nachher genau einmal."""
    db = _make_db(tmp_path, kasse_skr)
    giro, _kasse = _accounts(db)
    STANDALONE_FORMS[form][0](db)
    bank = db.insert_booking('2026-03-05', Decimal('-119'), account_id=giro, booking_type='bank')
    assert _euer(db) == PAID                                   # vorher: nur der eigenständige Satz

    result = db.link_bank_to_entries()

    assert result['linked'] == 1
    assert _query(db, "SELECT COUNT(*) FROM Bookings WHERE ParentBooking_ID = ?", (bank,)) == [(1,)]
    assert _euer(db) == PAID                                   # nachher: Kind der Bankbewegung
    assert _euer(db, [giro]) == PAID


@pytest.mark.parametrize('kasse_skr', [None, 1600], ids=['kasse-ohne-skr', 'kasse-1600'])
def test_dashboard_counts_the_linked_clearing_booking_once(tmp_path, kasse_skr):
    """Die Bankzeile ist die Zahlung; der eigenständige 1460-Satz kommt im Dashboard nicht vor –
    weder vor noch nach der Verknüpfung."""
    db = _make_db(tmp_path, kasse_skr)
    giro, _kasse = _accounts(db)
    STANDALONE_FORMS['verrechnungskonto'][0](db)
    db.insert_booking('2026-03-05', Decimal('-119'), account_id=giro, booking_type='bank')
    before = _dash(db)
    db.link_bank_to_entries()
    assert before == _dash(db) == {'income': 0, 'private': 0, 'expense': Decimal('-119'),
                                   'bank_count': 1}


@pytest.mark.parametrize('kasse_skr, form', LINK_PARAMS)
def test_standalone_entry_counts_once_when_the_split_editor_adopts_it(tmp_path, kasse_skr, form):
    """Dasselbe über den anderen App-Weg: Der Buchungssatz-Editor der Bankmaske übernimmt die
    bestehende Buchung („vorhandene Buchung zuordnen“)."""
    db = _make_db(tmp_path, kasse_skr)
    giro, _kasse = _accounts(db)
    entry = STANDALONE_FORMS[form][0](db)
    bank = db.insert_booking('2026-03-05', Decimal('-119'), account_id=giro, booking_type='bank')
    assert _euer(db) == PAID

    status, _location = handle_add_transaction(db, {
        'transaction_id': [str(bank)], 'date': ['2026-03-05'], 'amount': ['-119.00'],
        'account': [str(giro)], 'currency': ['EUR'],
        'split_id': [str(entry)], 'split_amount': ['-119.00'], 'split_coa': [str(_coa(db, 6815))],
        'split_tax_rate': ['19'], 'split_tax_amount': ['-19.00'], 'split_docnr': [''],
        'split_text': ['']})

    assert status == 303
    assert _query(db, "SELECT ParentBooking_ID FROM Bookings WHERE ID = ?", (entry,)) == [(bank,)]
    assert _euer(db) == PAID
    assert _euer(db, [giro]) == PAID


# ── Satz auf der Kasse mit zwei kontierten Seiten (Sektion 2b) ───────────────

@pytest.mark.parametrize('amount, expected', [
    ('100', {3150: '100', 6815: '-100'}),
    ('-100', {3150: '-100', 6815: '100'}),
], ids=['positiv', 'negativ'])
@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_cash_account_entry_with_two_booked_sides_reads_as_a_transfer(tmp_path, kasse_skr, amount,
                                                                      expected):
    """FESTGESCHRIEBENER SONDERFALL: Ein Satz mit Konto = Kasse, aber beiden Seiten kontiert und
    ohne liquide Seite (6815 / 3150, +100) lief früher als Zahlung auf 6815 (+100). Seit 2.0.5 geht
    auch Sektion 2b über ``classify_entry`` und liest ihn als Umbuchung: Betrag positiv =
    Konto im Soll, Gegenkonto im Haben."""
    db = _make_db(tmp_path, kasse_skr)
    giro, kasse = _accounts(db)
    _standalone(db, 6815, 3150, amount, account_id=kasse)
    assert _euer(db) == _decimals(expected)
    assert _euer(db, [kasse]) == _decimals(expected)
    assert _euer(db, [giro]) == {}


# ── Unabhängigkeit von der Kassennummer, breite Kombination ──────────────────

# (Zweckkonto, Betrag, Satz, Steuer) wie in tests/test_entry_forms.py, dazu Erstattung,
# Storno, Aufwand ohne Steuer und Privatentnahme
PURPOSES = [
    (4400, '119.00', 0.19, '19.00'), (6815, '-59.50', 0.19, '-9.50'),
    (4300, '107.00', 0.07, '7.00'), (6815, '-107.00', 0.07, '-7.00'),
    (6815, '59.50', 0.19, '9.50'), (4400, '-119.00', 0.19, '-19.00'),
    (6805, '-10.00', None, None), (2100, '-50.00', None, None),
]
# Geldseiten, die in jeder Datenbank liquide sind: das eigene Bankkonto und das Verrechnungskonto
# (der frühere feste Bereich 1000–1099 gehört seit 2.1.0 nicht mehr dazu). Die Kasse der App zählt über
# ihr eigenes Konto: 1600 ist nur dann liquide, wenn die Kasse es führt.
FIXED_SIDES = [1800, 1460]
OWN_CASH_SIDE = 1600


def _forms(kasse_skr):
    sides = FIXED_SIDES + ([OWN_CASH_SIDE] if kasse_skr == OWN_CASH_SIDE else [])
    return ([('zweckkonto-vorne', side) for side in sides]
            + [('geldkonto-vorne', side) for side in sides]
            + [('gegenkonto-leer', None), ('vorne-leer', None)])


def _form_accounts(form, purpose, side):
    """(COA, Gegenkonto) eines Satzes zum Zweckkonto und der liquiden Seite."""
    return {'zweckkonto-vorne': (purpose, side), 'geldkonto-vorne': (side, purpose),
            'gegenkonto-leer': (purpose, None), 'vorne-leer': (None, purpose)}[form]


def _expected_payment(purpose, amount, rate, tax):
    """Erwartete EÜR einer Zahlung: Netto auf dem Zweckkonto, Steuer auf 3806 (Einnahme) bzw.
    1401/1406 (Ausgabe)."""
    gross, tax_amount = Decimal(amount), Decimal(tax or '0')
    expected = {purpose: gross - tax_amount}
    if tax_amount:
        income = 4000 <= purpose <= 4999
        expected[3806 if income else (1401 if rate == 0.07 else 1406)] = tax_amount
    return expected


def _combination_db(tmp_path, kasse_skr):
    """Alle Kombinationen aus Zweckkonto, Form und liquider Seite als Kinder je einer Girobewegung,
    jede an einem eigenen Tag. Liefert die Datenbank und {Tag: (Zweckkonto, Form, liquide Seite)}."""
    db = _make_db(tmp_path, kasse_skr, f'kombination-{KASSE_ID[kasse_skr]}.db')
    assert db.insert_chart_of_accounts(4, 1050, 'Kassenbereich 1050', 'Test')
    day = datetime.date(2026, 1, 1)
    rows = {}
    for purpose in PURPOSES:
        for form, side in _forms(kasse_skr):
            coa, counter = _form_accounts(form, purpose[0], side)
            _giro_child(db, coa, counter, purpose[1], purpose[2], purpose[3], date=day.isoformat())
            rows[day.isoformat()] = (purpose, form, side)
            day += datetime.timedelta(days=1)
    return db, rows


def test_euer_of_a_bank_child_does_not_depend_on_the_account_number_of_the_cash_account(tmp_path):
    """Differenztest: Ein Kind einer Girobewegung mit Bank oder Verrechnungskonto als Geldseite
    (Zweckkonto vorne oder Geldkonto vorne) ergibt in der EÜR dieselben Zahlen wie mit dem eigenen
    Bankkonto – egal, ob die Kasse keine SKR-Nummer, 1600 oder 1460 führt. Führt die Kasse 1600, ist
    auch 1600 eine solche Geldseite."""
    results = {}
    for kasse_skr in KASSEN:
        db, rows = _combination_db(tmp_path, kasse_skr)
        assert len(rows) == len(PURPOSES) * len(_forms(kasse_skr))
        results[kasse_skr] = {rows[day]: _euer(db, date_from=day, date_to=day) for day in rows}

    for kasse_skr in KASSEN:
        for (purpose, form, side), euer in results[kasse_skr].items():
            assert euer == _decimals(_expected_payment(*purpose)), \
                (KASSE_ID[kasse_skr], purpose, form, side)
    common = set(results[None]) & set(results[1600]) & set(results[1460])
    assert len(common) == len(PURPOSES) * (2 * len(FIXED_SIDES) + 2)
    assert all(results[None][combo] == results[1600][combo] == results[1460][combo]
               for combo in common)


# ── Umbuchungen zwischen Anlage- und Erfolgskonto (Sektion 3) ────────────────

ASSETS_, PL_, LIQUID_ = {520, 560}, {6220, 6222, 6815, 4400}, {1800, 1460, 1600}


@pytest.mark.parametrize('coa, counter, expected', [
    pytest.param(6220, 520, True, id='aufwand-an-anlage'),
    pytest.param(520, 6220, True, id='anlage-an-aufwand'),
    pytest.param(6222, 560, True, id='andere-anlage'),
    pytest.param(4400, 520, True, id='erloes-und-anlage'),
    pytest.param(520, 3300, False, id='anlage-an-verbindlichkeit'),
    pytest.param(3300, 520, False, id='verbindlichkeit-an-anlage'),
    pytest.param(520, 1460, False, id='anlage-an-verrechnungskonto'),
    pytest.param(1460, 520, False, id='verrechnungskonto-an-anlage'),
    pytest.param(520, 1800, False, id='anlagenkauf-bank'),
    pytest.param(1800, 520, False, id='bank-vorne-anlage'),
    pytest.param(520, 1600, False, id='anlagenkauf-kasse'),
    pytest.param(520, 560, False, id='anlage-an-anlage'),
    pytest.param(6220, 6815, False, id='aufwand-an-aufwand'),
    pytest.param(6220, 3300, False, id='aufwand-an-verbindlichkeit'),
    pytest.param(520, None, False, id='anlage-ohne-gegenkonto'),
    pytest.param(None, 520, False, id='leer-an-anlage'),
    pytest.param(6220, None, False, id='aufwand-ohne-gegenkonto'),
    pytest.param(None, None, False, id='leer'),
])
def test_is_asset_transfer_truth_table(coa, counter, expected):
    assert is_asset_transfer(coa, counter, LIQUID_, ASSETS_, PL_) is expected


@pytest.mark.parametrize('coa, counter, liquid, expected', [
    pytest.param(6220, 520, set(), True, id='keine-liquide-seite'),
    pytest.param(6220, 520, {6220}, False, id='aufwandskonto-ist-geldkonto'),
    pytest.param(520, 6220, {6220}, False, id='aufwandskonto-gegenseite-ist-geldkonto'),
    pytest.param(6220, 520, {520}, False, id='anlagekonto-ist-geldkonto'),
    pytest.param(520, 6220, {520}, False, id='anlagekonto-vorne-ist-geldkonto'),
])
def test_is_asset_transfer_leaves_the_classification_to_classify_entry(coa, counter, liquid, expected):
    """Die Einordnung bleibt an EINER Stelle (``classify_entry``): Ist eine Seite ein Geldkonto – auch wenn
    ein eigenes Konto eine Nummer im Anlage- oder Erfolgsbereich trägt –, ist der Satz keine Umbuchung."""
    assert is_asset_transfer(coa, counter, liquid, ASSETS_, PL_) is expected


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_asset_and_loss_account_ranges_follow_the_documented_boundaries(tmp_path, kasse_skr):
    """Anlagekonten sind 1–999, Erfolgskonten 4000–7999 (SKR04): Grenzfälle als Sätze mit dem
    jeweils anderen Konto (Aufwand 6220 bzw. Anlage 0520)."""
    db = _make_db(tmp_path, kasse_skr)
    for number in (1, 999, 1000, 3999, 4000, 7999, 8000):
        assert db.insert_chart_of_accounts(4, number, f'Grenzkonto {number}', 'Test')

    expected = {}
    day = datetime.date(2026, 2, 1)
    for number, counts in ((1, True), (999, True), (1000, False)):           # Anlageseite
        _standalone(db, 6220, number, '100', date=day.isoformat())
        expected[day.isoformat()] = {6220: Decimal('-100')} if counts else {}
        day += datetime.timedelta(days=1)
    for number, counts in ((3999, False), (4000, True), (7999, True), (8000, False)):  # Erfolgsseite
        _standalone(db, number, 520, '100', date=day.isoformat())
        expected[day.isoformat()] = {number: Decimal('-100')} if counts else {}
        day += datetime.timedelta(days=1)

    for date, wanted in expected.items():
        assert _euer(db, date_from=date, date_to=date) == wanted, date


@pytest.mark.parametrize('coa, counter, amount, expected', [
    pytest.param(6222, 520, '600', {6222: '-600'}, id='afa-positiv'),
    pytest.param(520, 6222, '-600', {6222: '-600'}, id='gleicher-satz-anlage-vorne'),
    pytest.param(6222, 520, '-600', {6222: '600'}, id='storno'),
    pytest.param(6895, 520, '1200', {6895: '-1200'}, id='restbuchwert-beim-abgang'),
])
@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_asset_transfers_show_only_the_loss_account(tmp_path, kasse_skr, coa, counter, amount,
                                                    expected):
    """Das Anlagekonto erscheint nicht (als Einnahme) in der EÜR – nur die Aufwandsseite."""
    db = _make_db(tmp_path, kasse_skr)
    _standalone(db, coa, counter, amount, date='2026-12-31')
    assert _euer(db) == _decimals(expected)


@pytest.mark.parametrize('coa, counter, amount, rate, tax, expected', [
    pytest.param(6220, 520, '119', 0.19, '19', {6220: '-119'}, id='aufwand-an-anlage-19'),
    pytest.param(520, 6220, '-119', 0.19, '-19', {6220: '-119'}, id='anlage-vorne-19'),
    pytest.param(6222, 520, '107', 0.07, '7', {6222: '-107'}, id='aufwand-an-anlage-7'),
    pytest.param(6220, 520, '-119', 0.19, '-19', {6220: '119'}, id='storno'),
    pytest.param(6895, 520, '1190', 0.19, '190', {6895: '-1190'}, id='restbuchwert'),
])
@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_asset_transfer_books_no_tax(tmp_path, kasse_skr, coa, counter, amount, rate, tax,
                                     expected):
    """FESTGESCHRIEBEN (Sektion 3 ohne Steuer): Eine Umbuchung zwischen Anlage- und Erfolgskonto ist
    keine Zahlung. Steuersatz und -betrag bleiben unberücksichtigt, der Betrag steht – wie gebucht,
    also BRUTTO – als Aufwand; es entsteht keine Zeile 1401/1406/3806. („6220 an 0520“ +119 mit 19 %
    ergibt 6220 −119, nicht 6220 −100 und 1406 +19.)"""
    db = _make_db(tmp_path, kasse_skr)
    _standalone(db, coa, counter, amount, rate, tax, date='2026-12-31')

    result = _euer(db)

    assert result == _decimals(expected)
    assert not {1401, 1406, 3806} & set(result)


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_bank_child_transfers_between_other_accounts_keep_their_tax(tmp_path, kasse_skr):
    """Gegenprobe: Nur die Umbuchungen zwischen Anlage- und Erfolgskonto (Sektion 3) lassen die Steuer
    weg. Die Umbuchung 4405 → 4400 unter einer Bankbewegung bucht die USt wie bisher."""
    db = _make_db(tmp_path, kasse_skr)
    _giro_child(db, 4405, 4400, '119.00', 0.19, '19.00')
    assert _euer(db) == {4400: Decimal('100'), 3806: Decimal('19')}


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_asset_transfer_entered_on_a_cash_account_counts_exactly_once(tmp_path, kasse_skr):
    """Ein eigenständiger Satz Anlage ↔ Erfolgskonto mit Konto = Kasse („6222 an 0520“ +600) ist eine
    Umbuchung der Sektion 3 und nicht zusätzlich ein Kassensatz der Sektion 2b: Ohne Filter zählt er
    einmal (6222 −600, nicht −1200), mit dem Filter „nur Kasse“ gar nicht – die Umbuchung hängt an
    keinem Konto, auch nicht an dem in Konto eingetragenen."""
    db = _make_db(tmp_path, kasse_skr)
    giro, kasse = _accounts(db)
    _standalone(db, 6222, 520, '600', date='2026-12-31', account_id=kasse)

    assert _euer(db) == {6222: Decimal('-600')}
    assert _euer(db, [giro, kasse]) == {6222: Decimal('-600')}
    assert _euer(db, [kasse]) == {}
    assert _euer(db, [giro]) == {}


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_asset_transfer_with_tax_entered_on_a_cash_account_counts_once_and_without_tax(
        tmp_path, kasse_skr):
    db = _make_db(tmp_path, kasse_skr)
    _giro, kasse = _accounts(db)
    _standalone(db, 6220, 520, '119', 0.19, '19', date='2026-12-31', account_id=kasse)
    assert _euer(db) == {6220: Decimal('-119')}


DEPRECIATION_COUNTERS = [pytest.param(skr, counter, id=f'{counter}-{KASSE_ID[skr]}')
                         for counter in ('1460', 'kasse-skr', 'kasse-konto-und-1460')
                         for skr in KASSEN if not (counter == 'kasse-skr' and skr is None)]


@pytest.mark.parametrize('kasse_skr, counter', DEPRECIATION_COUNTERS)
def test_depreciation_linked_entry_touching_a_cash_account_counts_once(tmp_path, kasse_skr,
                                                                       counter):
    """Jede Buchung ist über ihre ID eindeutig: Ein von der AfA-Planzeile referenzierter Satz, der
    zugleich das Verrechnungskonto oder das Kassenkonto berührt (Sektion 2 findet ihn über die Konten,
    Sektion 3 über die Planzeile, Sektion 2b über das Konto), zählt einmal."""
    db = _make_db(tmp_path, kasse_skr)
    _giro, kasse = _accounts(db)
    asset = db.insert_asset('Altlaptop', '2026-01-10', 1800.0, 3, coa_id=_coa(db, 520))
    counter_number = _kasse_skr(db) if counter == 'kasse-skr' else 1460
    booking = _standalone(db, 6220, counter_number, '-600', date='2026-12-31',
                          account_id=kasse if counter == 'kasse-konto-und-1460' else None)
    _execute(db, "INSERT INTO AssetDepreciations (Asset_ID, Year, DepreciationAmount, BookValue, "
                 "Booking_ID, Status, BookedAt) VALUES (?, 2026, 6000000, 12000000, ?, 'posted', "
                 "CURRENT_TIMESTAMP)", (asset, booking))

    assert _euer(db) == {6220: Decimal('-600')}


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_asset_purchase_through_the_clearing_account_keeps_the_account_in_the_euer(
        tmp_path, kasse_skr):
    """Der Anlagenkauf als Zahlung (Anlagekonto gegen Geldkonto, auch 1460) ist keine Umbuchung: wie
    bisher steht der Nettobetrag auf dem Anlagekonto, die Vorsteuer auf 1406."""
    db = _make_db(tmp_path, kasse_skr)
    _giro_child(db, 520, 1460, '-1190', 0.19, '-190')
    assert _euer(db) == {520: Decimal('-1000'), 1406: Decimal('-190')}


def _wiso_database(path, rows):
    """Synthetische WISO-Datenbank (nur Kontenrahmen und Buchungssätze) mit den Zeilen ``rows``."""
    builder = FdbBuilder(page_size=16384)
    chart = builder.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4), Column('SKR04', LONG, 4), Column('BOOKINGYEAR', SHORT, 2),
        Column('ACCOUNTTEXT', TEXT, 40)])
    for skr03, skr04, text in ((320, 520, 'Pkw'), (4832, 6222, 'Abschreibungen auf Kfz'),
                               (1210, 1810, 'Bankkonto')):
        chart.add(ID=skr03, SKR04=skr04, BOOKINGYEAR=2026, ACCOUNTTEXT=text)
    bookings = builder.table('MOV_FINACC_ACCRECORDS', [
        Column('ID', LONG, 4), Column('ACCOUNTINGID', LONG, 4), Column('ACCOUNTING_DATE', TIMESTAMP, 8),
        Column('AMOUNTNET', INT64, 8, scale=-2), Column('AMOUNTGROSS', INT64, 8, scale=-2),
        Column('TAXRATE', INT64, 8, scale=-2), Column('ACCOUNTNO', LONG, 4),
        Column('CONTRA_ACCOUNTNO', LONG, 4), Column('ACCOUNTING_TEXT', TEXT, 60),
        Column('REFERENCENO', TEXT, 20)])
    for number, row in enumerate(rows, start=1):
        bookings.add(ID=number, ACCOUNTINGID=900 + number, **row)
    return builder.write(str(path))


@pytest.mark.parametrize('kasse_skr', KASSEN_PARAMS)
def test_depreciation_from_the_wiso_database_is_in_the_euer(tmp_path, kasse_skr):
    """Über den echten Importweg: WISOs AfA-Satz „Abschreibungen auf Kfz an Pkw“ (SKR03 4832/320)
    steht als Umbuchung 6222 / 0520 in den Büchern und zählt in der EÜR als Aufwand – ohne
    Kontofilter."""
    db = _make_db(tmp_path, kasse_skr)
    path = _wiso_database(tmp_path / 'db1.fdb', [
        dict(ACCOUNTING_DATE='2026-12-31 00:00:00', AMOUNTGROSS=600.0, ACCOUNTNO=4832,
             CONTRA_ACCOUNTNO=320, ACCOUNTING_TEXT='AfA Pkw 2026')])

    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)

    assert result['imported'] == 1 and not result['errors'], result
    assert _query(db, "SELECT Amount FROM Bookings WHERE Text = 'AfA Pkw 2026'") == [(6000000,)]
    giro, _kasse = _accounts(db)
    assert _euer(db) == {6222: Decimal('-600')}
    assert _euer(db, [giro]) == {}
