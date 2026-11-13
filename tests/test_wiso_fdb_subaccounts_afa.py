# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Datenbank-Import aus WISO: eigene Unterkonten und vorausgeplante AfA.

Teil 1 – **Unterkonten behalten ihre SKR04-Nummer.** ``BAS_FINACC_PLAN`` führt nur alte
Kontenpläne; später angelegte Konten fehlen dort. Ihre SKR04-Nummer speichert WISO je Konto in
``FINT_EXTERNDEFS`` (``EXTERNKIND`` 82, ``EXTERNVALUE``, je Kontenplan ``FINTABLEID``). Der Leser
nimmt sie – nach ``BAS_FINACC_PLAN``, vor dem Rückgriff auf das Basiskonto (``BASEACCOUNTNO``) und
den geprüften Nachträgen (``KNOWN_EQUIVALENTS``, die an der gespeicherten Nummer nicht teilnehmen).
Sie gilt nur für Sachkonten (Wert 1 bis 9999). Ein Import bis 2.2.4 hat den Satz auf das Basiskonto
geschrieben: Der Re-Import erkennt ihn an der Quellgruppe, legt keinen zweiten an, ändert nichts und
zählt ihn (``subaccount_rows``). Unterkonten von Zahlungskonten, die in der App keine sind, werden
gemeldet (``liquid_subaccounts``); eine vorhandene Anlage behält ihr Anlagekonto.

Teil 2 – **Vorausgeplante AfA wird nicht gebucht.** WISO schreibt den ganzen AfA-Plan sofort als
Buchungssätze, auch für künftige Jahre. Ein AfA-Satz (``INVENTORYAMORTID``, WISOs Buchungsart
``ACCOUNTINGKIND`` 28; kennt der Satz die Spalte nicht: eine Zeile der AfA-Tabelle derselben Anlage mit
gleichem Datum und Betrag) nach dem Stichtag (``today``, sonst der Tag des Imports) liefert der Leser
gekennzeichnet (``future_depreciation``), der Import legt ihn nie an. **Der Status eines Planjahrs folgt der
Buchung, nicht dem Datum:** ``booked``, wenn der AfA-Satz der Anlage für dieses Jahr in den Büchern steht
(auch ein künftiger Satz aus einem Import bis 2.2.4, ``future_depreciations_kept``), sonst ``planned`` –
gebucht wird dann im Jahr selbst in der App (``book_depreciation``). Hat die App ein Jahr schon gebucht,
legt ein späterer Import den AfA-Satz der Datenbank nicht an (``depreciation_conflicts``). Einen AfA-Satz, dem
keine Zeile seiner bekannten Gruppe genau gleicht, hat der Nutzer geändert, wenn die Gruppe eine freie Zeile im
Planjahr hat: Er gilt als vorhanden. Hat sie keine, wird er nur nachgetragen, wenn die Anlage in der App eindeutig
zugeordnet ist und ihre Planzeile des Jahres ``planned`` ist. Rechnet die App die geplanten Jahre anders als WISO,
meldet der Import das (``depreciation_plan_mismatches``).

Alle Namen, Nummern und Beträge sind erfunden; die Datenbanken baut ``tests/fdb_builder.py``.
"""
import datetime
import json
import os
import sqlite3

import pytest

import userctx
from db.wiso_fdb_import import MAX_CONFLICT_ROWS, _AssetLink
from importers.wiso_fdb import WisoBooking, WisoDatabase, _cents, read_wiso_database
from server import handlers
from server.pages_miscellaneous import PageMiscellaneous, _import_notices
from tests.entry_form_helpers import csv_export, execute, query
from tests.fdb_builder import DOUBLE, FdbBuilder, INT64, LONG, SHORT, TEXT, TIMESTAMP, Column

#: SKR03 → SKR04 wie in WISOs ``BAS_FINACC_PLAN``; 1200/1210 sind die Bankkonten der Testdaten
#: (SKR04 1800/1810), 1360 das Verrechnungskonto. 6530, 6810 und 6850 kennt der Kontenrahmen der
#: Testdatenbank, 6532, 6812 und 6539 nicht – dort legt der Import sie an.
CHART = ((1200, 1800, 'Bank'), (1210, 1810, 'Bankkonto 2'), (1360, 1460, 'Verrechnungskonto'),
         (4980, 6850, 'Betriebsbedarf'), (4530, 6530, 'Kfz-Betriebskosten'),
         (4925, 6810, 'Beispielkosten A'), (8400, 4400, 'Erlöse 19 %'), (320, 520, 'Pkw'),
         (4832, 6222, 'Abschreibungen auf Kfz'))
#: ein Unterkonto von 4530: nur in ``FINT_ACCOUNTS`` (Konto, Basiskonto, Bezeichnung)
SUB = (4532, 4530, 'Kfz-Betriebskosten Fahrzeug A')
EXTERN_COLUMNS = ('ACCOUNTNO', 'FINTABLEID', 'EXTERNKIND', 'EXTERNVALUE')
VALUE_COLUMNS = {TEXT: 20, LONG: 4, DOUBLE: 8}


def _record(account, counter, gross, text, date='2026-03-05', ref='', **extra):
    """Ein Satz von ``MOV_FINACC_ACCRECORDS`` (SKR03-Nummern)."""
    return dict(ACCOUNTING_DATE=f'{date} 00:00:00', ACCOUNTNO=account, CONTRA_ACCOUNTNO=counter,
                AMOUNTGROSS=gross, ACCOUNTING_TEXT=text, REFERENCENO=ref, **extra)


def _fdb(path, records=(), *, chart=CHART, subaccounts=None, extern=None, extern_type=TEXT,
         extern_columns=EXTERN_COLUMNS, booking_columns=('INVENTORYID', 'INVENTORYAMORTID'),
         assets=(), old_records=()):
    """Synthetische WISO-Datenbank. ``subaccounts`` (``FINT_ACCOUNTS``) und ``extern``
    (``FINT_EXTERNDEFS``: Konto, Plan, Art, Wert) sind optional – ohne Angabe fehlt die Tabelle.
    ``extern_columns`` nennt die Spalten der Tabelle, ``booking_columns`` die Spalten von
    ``MOV_FINACC_ACCRECORDS`` für Anlagen, ``assets`` die Anlagen samt AfA-Plan (siehe ``_asset``).
    ``old_records`` stehen im **älteren Satzformat** von ``MOV_FINACC_ACCRECORDS``, das die Spalte
    ``INVENTORYAMORTID`` noch nicht kannte (die Spalte kam später hinzu); ``records`` im aktuellen."""
    builder = FdbBuilder(page_size=16384)
    plan = builder.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4), Column('SKR04', LONG, 4), Column('BOOKINGYEAR', SHORT, 2),
        Column('ACCOUNTTEXT', TEXT, 40)])
    for skr03, skr04, text in chart:
        plan.add(ID=skr03, SKR04=skr04, BOOKINGYEAR=2026, ACCOUNTTEXT=text)
    if subaccounts is not None:
        table = builder.table('FINT_ACCOUNTS', [
            Column('ID', LONG, 4), Column('ACCOUNTNO', LONG, 4), Column('BASEACCOUNTNO', LONG, 4),
            Column('ACCOUNTLABEL', TEXT, 40)])
        for number, (account, base, label) in enumerate(subaccounts, start=1):
            table.add(ID=number, ACCOUNTNO=account, BASEACCOUNTNO=base, ACCOUNTLABEL=label)
    if extern is not None:
        columns = {'ACCOUNTNO': Column('ACCOUNTNO', LONG, 4), 'FINTABLEID': Column('FINTABLEID', LONG, 4),
                   'EXTERNKIND': Column('EXTERNKIND', SHORT, 2),
                   'EXTERNVALUE': Column('EXTERNVALUE', extern_type, VALUE_COLUMNS[extern_type])}
        table = builder.table('FINT_EXTERNDEFS', [Column('ID', LONG, 4)]
                              + [columns[name] for name in extern_columns])
        for number, (account, plan_id, kind, value) in enumerate(extern, start=1):
            table.add(ID=number, ACCOUNTNO=account, FINTABLEID=plan_id, EXTERNKIND=kind,
                      EXTERNVALUE=value)
    columns = [
        Column('ID', LONG, 4), Column('ACCOUNTINGID', LONG, 4), Column('ACCOUNTING_DATE', TIMESTAMP, 8),
        Column('AMOUNTNET', INT64, 8, scale=-2), Column('AMOUNTGROSS', INT64, 8, scale=-2),
        Column('TAXRATE', INT64, 8, scale=-2), Column('ACCOUNTNO', LONG, 4),
        Column('CONTRA_ACCOUNTNO', LONG, 4), Column('ACCOUNTING_TEXT', TEXT, 60),
        Column('REFERENCENO', TEXT, 20)] + [Column(name, LONG, 4) for name in booking_columns]
    older = [column for column in columns if column.name != 'INVENTORYAMORTID'] if old_records else None
    bookings = builder.table('MOV_FINACC_ACCRECORDS', columns, old_columns=older)
    for number, record in enumerate(records, start=1):
        record = dict(record)
        group = record.pop('ACCOUNTINGID', 700 + number)
        bookings.add(ID=number, ACCOUNTINGID=group, **record)
    for number, record in enumerate(old_records, start=len(records) + 1):
        record = dict(record)
        group = record.pop('ACCOUNTINGID', 700 + number)
        record.pop('INVENTORYAMORTID', None)                              # das ältere Format kennt die Spalte nicht
        bookings.add_old(ID=number, ACCOUNTINGID=group, **record)
    if assets:
        _asset_tables(builder, assets)
    return builder.write(str(path))


# ── Anlagen mit AfA-Plan ─────────────────────────────────────────────────────

#: AfA-Plan einer Anlage über 6.000 € (Kauf 15.03.2024, vier Jahre): Datum, AfA, kumuliert
AFA_PLAN = [('2024-12-31', 1250.0, 1250.0), ('2025-12-31', 1500.0, 2750.0),
            ('2026-12-31', 1500.0, 4250.0), ('2027-12-31', 1500.0, 5750.0),
            ('2028-12-31', 250.0, 6000.0)]


def _asset(number=1, label='Dienstwagen', plan=AFA_PLAN, sale=None, price=6000.0, purchase='2024-03-15',
           account=320, id=None):
    """Eine Anlage; ``id`` ist ``BAS_INVENTORY.ID``, auf die ``INVENTORYID`` der Buchungen zeigt (ohne
    Angabe aus der Inventarnummer), ``account`` ihr Anlagekonto (SKR03)."""
    return dict(id=number + 4 if id is None else id, number=number, label=label, plan=list(plan), sale=sale,
                price=price, purchase=purchase, account=account)


def _amort_id(asset, index):
    return asset['id'] * 100 + index


def _afa_records(asset, amort=True):
    """Die AfA-Sätze, die WISO zu den Planjahren schreibt: „Aufwand an Anlagekonto“ mit
    ``INVENTORYID`` und (``amort``) ``INVENTORYAMORTID``."""
    out = []
    for index, (date, amount, _cumulated) in enumerate(asset['plan'], start=1):
        extra = dict(INVENTORYID=asset['id'])
        if amort:
            extra['INVENTORYAMORTID'] = _amort_id(asset, index)
        out.append(_record(4832, 320, amount, f'Afa fuer Inventar {asset["label"]}', date=date, **extra))
    return out


def _asset_tables(builder, assets):
    inventory = builder.table('BAS_INVENTORY', [
        Column('ID', LONG, 4), Column('INVNO', LONG, 4), Column('LABEL', TEXT, 40),
        Column('PURCHASEDATE', TIMESTAMP, 8), Column('PURCHASEAMOUNTNET', INT64, 8, scale=-2),
        Column('SALEDATE', TIMESTAMP, 8), Column('SERVICELIFE', SHORT, 2),
        Column('FINACIALACCOUNT', LONG, 4), Column('FINACIALACCOUNT_AFA', LONG, 4)])
    payments = builder.table('MOV_INVENTORY_BOOKINGS', [
        Column('ID', LONG, 4), Column('INVENTORYID', LONG, 4), Column('BOOKINGDATE', TIMESTAMP, 8),
        Column('AMOUNTNET', INT64, 8, scale=-2), Column('REMOVEACCOUNT', SHORT, 2),
        Column('DESCRIPTION', TEXT, 40)])
    plans = builder.table('MOV_INVENTORY_AMORTIZATIONS', [
        Column('ID', LONG, 4), Column('INVENTORYID', LONG, 4), Column('BOOKINGDATE', TIMESTAMP, 8),
        Column('AMORT_AMOUNT', INT64, 8, scale=-2), Column('AMORT_CUMULATED_AMOUNT', INT64, 8, scale=-2),
        Column('RESIDUALVALUE_AMOUNT', INT64, 8, scale=-2)])
    for asset in assets:
        inventory.add(ID=asset['id'], INVNO=asset['number'], LABEL=asset['label'],
                      PURCHASEDATE=f'{asset["purchase"]} 00:00:00', PURCHASEAMOUNTNET=0.01,
                      SALEDATE=f'{asset["sale"]} 00:00:00' if asset['sale'] else None,
                      SERVICELIFE=4, FINACIALACCOUNT=asset['account'], FINACIALACCOUNT_AFA=4832)
        payments.add(ID=asset['id'], INVENTORYID=asset['id'], BOOKINGDATE=f'{asset["purchase"]} 00:00:00',
                     AMOUNTNET=asset['price'], DESCRIPTION='Kauf')
        for index, (date, amount, cumulated) in enumerate(asset['plan'], start=1):
            plans.add(ID=_amort_id(asset, index), INVENTORYID=asset['id'],
                      BOOKINGDATE=f'{date} 00:00:00', AMORT_AMOUNT=amount,
                      AMORT_CUMULATED_AMOUNT=cumulated)


# ── Hilfen für die Datenbank der App ─────────────────────────────────────────

def run(db, path, **kwargs):
    kwargs.setdefault('with_invoices', False)
    result = db.import_wiso_fdb(str(path), **kwargs)
    assert not result['errors'], result['errors']
    return result


def coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _on_accounts(db, text_prefix=''):
    """(Konto, Gegenkonto, Betrag, Form, Quellgruppe) der importierten Sätze nach ID."""
    return query(db, '''
        SELECT c.AccountNumber, g.AccountNumber, b.Amount, b.EntryForm, b.SourceGroup
        FROM Bookings b LEFT JOIN ChartOfAccounts c ON c.ID = b.COA_ID
        LEFT JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID
        WHERE b.BookingType = 'entry' AND b.Text LIKE ? ORDER BY b.ID''', (text_prefix + '%',))


def _plan(db):
    """Der AfA-Plan der App: (Jahr, AfA, Buchwert, Status, Booking_ID) nach Jahr."""
    return query(db, 'SELECT Year, DepreciationAmount, BookValue, Status, Booking_ID '
                     'FROM AssetDepreciations ORDER BY Asset_ID, Year')


def _afa_bookings(db):
    """Die AfA-Sätze „6222 an 520“ (aus WISO oder aus der App): (Datum, Quellgruppe) nach Datum."""
    return query(db, 'SELECT DateBooking, SourceGroup FROM Bookings WHERE COA_ID = ? AND CounterCOA_ID = ? '
                     'ORDER BY DateBooking', (coa(db, 6222), coa(db, 520)))


def _write_result(result):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), 'w',
              encoding='utf-8') as handle:
        json.dump(result, handle)


# ═════════════════════════════════════════════════════════════════════════════
# Teil 1 – Unterkonten
# ═════════════════════════════════════════════════════════════════════════════

def _account_of(path, standard=None):
    """SKR04-Nummer, die der Leser dem Konto der ersten Buchung gibt."""
    return read_wiso_database(str(path), standard).bookings[0].account


def _sub_booking(account=4532):
    return [_record(account, 1200, 119.0, 'Probe Kfz Fahrzeug A', ref='PR-S01')]


def test_an_account_only_in_externdefs_gets_the_stored_number(tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), extern=[(4532, 7, 82, '6532')])

    data = read_wiso_database(str(path))

    (booking,) = data.bookings
    assert (booking.account, booking.legacy_account) == (6532, None)     # die alte Regel kannte es nicht
    assert data.chart[4532].skr04 == 6532
    assert data.unmapped_accounts == {} and booking.unmapped == []


def test_the_import_books_on_the_stored_number_and_creates_the_account(db_with_coa, tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), extern=[(4532, 7, 82, '6532')])
    assert 6532 not in {row[0] for row in query(db_with_coa, 'SELECT AccountNumber FROM ChartOfAccounts')}

    result = run(db_with_coa, path)

    assert result['imported'] == 1 and result['missing_coa'] == {}
    assert result['created_coa'] == {6532: 'SKR04 6532'}                 # ohne Bezeichnung in WISO
    ((account, counter, amount, form, group),) = _on_accounts(db_with_coa, 'Probe Kfz')
    assert (account, counter, form, group) == (6532, 1800, 'purpose_first', '701') and amount < 0
    assert query(db_with_coa, "SELECT Name FROM ChartOfAccounts WHERE AccountNumber = 6532") == [('SKR04 6532',)]


def test_the_created_account_carries_the_label_of_the_subaccount(db_with_coa, tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), subaccounts=[SUB], extern=[(4532, 7, 82, '6532')])

    result = run(db_with_coa, path)

    assert result['created_coa'] == {6532: 'Kfz-Betriebskosten Fahrzeug A'}
    assert [row[0] for row in _on_accounts(db_with_coa, 'Probe Kfz')] == [6532]


def test_the_text_of_a_stored_account_is_label_then_plan_text_then_base_text_then_empty(tmp_path):
    chart = CHART + ((4540, None, 'Eigener Plantext'), (4541, None, 'Plantext B'))     # im Plan, ohne SKR04
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), chart=chart,
                subaccounts=[SUB, (4534, 4530, ''), (4540, 4530, ''), (4541, 4530, 'Bezeichnung B')],
                extern=[(4532, 7, 82, '6532'), (4534, 7, 82, '6534'), (4536, 7, 82, '6536'),
                        (4540, 7, 82, '6540'), (4541, 7, 82, '6541')])

    texts = {number: account.text for number, account in read_wiso_database(str(path)).chart.items()
             if number in (4532, 4534, 4536, 4540, 4541)}

    assert texts == {4532: 'Kfz-Betriebskosten Fahrzeug A',             # Bezeichnung aus FINT_ACCOUNTS
                     4534: 'Kfz-Betriebskosten',                         # kein Label: Text des Basiskontos
                     4536: '',                                           # weder Label noch Basiskonto
                     4540: 'Eigener Plantext',                           # kein Label: der Text des Plans
                     4541: 'Bezeichnung B'}                              # Label vor dem Text des Plans


def test_a_stored_number_beats_base_account_plus_distance(tmp_path):
    """Basis 4925 → 6810; das Unterkonto 4927 trägt die gespeicherte 6812, nicht die des Basiskontos."""
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(4927), subaccounts=[(4927, 4925, 'Projekt B')],
                extern=[(4927, 7, 82, '6812')])

    (booking,) = read_wiso_database(str(path)).bookings

    assert (booking.account, booking.legacy_account) == (6812, 6810)


def test_without_a_stored_number_the_subaccount_inherits_the_base_account(tmp_path):
    """Zu 4532 steht nichts in FINT_EXTERNDEFS (nur ein anderes Konto): wie bisher die Nummer des
    Basiskontos."""
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), subaccounts=[SUB], extern=[(4999, 7, 82, '6999')])

    (booking,) = read_wiso_database(str(path)).bookings

    assert (booking.account, booking.legacy_account) == (6530, None)    # nichts weicht ab


def test_a_number_from_bas_finacc_plan_is_never_overwritten(tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(4980), extern=[(4980, 9, 82, '6999')])

    (booking,) = read_wiso_database(str(path)).bookings

    assert (booking.account, booking.legacy_account) == (6850, None)


@pytest.mark.parametrize('rows', [
    [(4532, 9, 82, '6539'), (4532, 3, 82, '6532')],
    [(4532, 3, 82, '6532'), (4532, 9, 82, '6539')]], ids=['jüngster zuerst', 'jüngster zuletzt'])
def test_the_plan_with_the_higher_id_wins(tmp_path, rows):
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), extern=rows)

    assert _account_of(path) == 6539


def test_an_unusable_value_in_the_youngest_plan_does_not_hide_an_older_usable_one(tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), subaccounts=[SUB],
                extern=[(4532, 9, 82, ''), (4532, 3, 82, '6532'), (4532, 5, 82, 'abc')])

    assert _account_of(path) == 6532


@pytest.mark.parametrize('kind,value', [
    (81, '6532'), (83, '6532'), (None, '6532'),                         # eine andere Art
    (82, ''), (82, None), (82, 'abc'), (82, '-6532'), (82, '0'), (82, '65.32'), (82, '6532x'),
    (82, '6 532')], ids=lambda value: str(value))
def test_other_kinds_and_unusable_values_are_ignored(tmp_path, kind, value):
    """Die Nummer fällt dann auf das Basiskonto zurück (6530) – als stünde die Zeile nicht da."""
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), subaccounts=[SUB], extern=[(4532, 7, kind, value)])

    assert _account_of(path) == 6530


@pytest.mark.parametrize('extern_type,value,expected', [
    (LONG, 6532, 6532), (LONG, -6532, 6530), (LONG, 0, 6530),
    (DOUBLE, 6532.0, 6532), (DOUBLE, 6532.5, 6530), (DOUBLE, -1.0, 6530),
    (TEXT, '6532', 6532)], ids=lambda value: str(value))
def test_text_and_numbers_both_count_but_only_a_positive_whole_one(tmp_path, extern_type, value, expected):
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), subaccounts=[SUB], extern=[(4532, 7, 82, value)],
                extern_type=extern_type)

    assert _account_of(path) == expected


@pytest.mark.parametrize('variant', ['ohne Tabelle', 'ohne EXTERNVALUE', 'ohne EXTERNKIND',
                                     'ohne ACCOUNTNO', 'ohne FINTABLEID'])
def test_without_the_table_or_its_columns_the_reader_behaves_as_before(tmp_path, variant):
    plain = _fdb(tmp_path / 'plain.fdb', _sub_booking(), subaccounts=[SUB])
    columns = {'ohne Tabelle': None,
               'ohne EXTERNVALUE': ('ACCOUNTNO', 'FINTABLEID', 'EXTERNKIND'),
               'ohne EXTERNKIND': ('ACCOUNTNO', 'FINTABLEID', 'EXTERNVALUE'),
               'ohne ACCOUNTNO': ('FINTABLEID', 'EXTERNKIND', 'EXTERNVALUE'),
               'ohne FINTABLEID': ('ACCOUNTNO', 'EXTERNKIND', 'EXTERNVALUE')}[variant]
    other = _fdb(tmp_path / 'other.fdb', _sub_booking(), subaccounts=[SUB],
                 extern=None if columns is None else [(4532, 7, 82, '6532')],
                 extern_columns=columns or EXTERN_COLUMNS)

    before = read_wiso_database(str(plain))
    after = read_wiso_database(str(other))

    assert [(b.account, b.legacy_account) for b in after.bookings] == [(6530, None)]
    assert after.bookings == before.bookings
    assert {n: (a.skr04, a.text) for n, a in after.chart.items()} == \
        {n: (a.skr04, a.text) for n, a in before.chart.items()}
    assert after.unmapped_accounts == before.unmapped_accounts == {}
    with WisoDatabase(str(plain)) as one, WisoDatabase(str(other)) as two:
        assert one.check() == two.check()                              # die Tabelle ist kein Teil der Prüfung
        assert not any('FINT_EXTERNDEFS' in hint for hint in two.check()[1])


def test_the_client_beats_the_standard_chart_and_the_standard_fills_the_rest(tmp_path):
    standard = _fdb(tmp_path / 'db0.fdb', extern=[(4532, 9, 82, '6531'), (4534, 9, 82, '6534')])
    client = _fdb(tmp_path / 'db1.fdb', [_record(4532, 1200, 1.0, 'A'), _record(4534, 1200, 2.0, 'B')],
                  extern=[(4532, 1, 82, '6532')])      # niedrigere Plan-Nummer: gilt trotzdem, ist der Mandant

    accounts = [b.account for b in read_wiso_database(str(client), str(standard)).bookings]

    assert accounts == [6532, 6534]


def test_without_a_stored_number_in_the_client_the_standard_chart_has_it(tmp_path):
    standard = _fdb(tmp_path / 'db0.fdb', extern=[(4532, 9, 82, '6531')])
    client = _fdb(tmp_path / 'db1.fdb', _sub_booking(), extern=[(4532, 1, 82, 'unbrauchbar')])

    assert _account_of(client, str(standard)) == 6531
    assert _account_of(client) is None                                   # ohne Standardrahmen: unbekannt


# ── Re-Import über einen Bestand aus einer älteren Version ───────────────────

def _records_with_a_subaccount():
    return [_record(4532, 1200, 119.0, 'Probe Kfz Fahrzeug A', ref='PR-S01'),
            _record(4980, 1200, 59.5, 'Probe Betriebsbedarf', ref='PR-S02')]


def _old_and_new(tmp_path, records=None, **kwargs):
    """Dieselbe Datenbank ohne (wie bis 2.2.4 gelesen) und mit ``FINT_EXTERNDEFS``."""
    records = records or _records_with_a_subaccount()
    old = _fdb(tmp_path / 'old.fdb', records, subaccounts=[SUB], **kwargs)
    new = _fdb(tmp_path / 'new.fdb', records, subaccounts=[SUB], extern=[(4532, 7, 82, '6532')], **kwargs)
    return old, new


def test_a_reimport_over_an_older_import_keeps_the_row_on_the_base_account(db_with_coa, tmp_path):
    old, new = _old_and_new(tmp_path)
    first = run(db_with_coa, old)
    assert (first['imported'], first['subaccount_rows']) == (2, 0)
    stored = query(db_with_coa, 'SELECT * FROM Bookings ORDER BY ID')
    assert [row[0] for row in _on_accounts(db_with_coa, 'Probe')] == [6530, 6850]    # Basiskonto

    second = run(db_with_coa, new)

    assert (second['imported'], second['skipped'], second['subaccount_rows']) == (0, 2, 1)
    assert second['adopted'] == 0 and second['possible_duplicates_count'] == 0
    assert query(db_with_coa, 'SELECT * FROM Bookings ORDER BY ID') == stored    # Konto, Betrag, Form: alles
    third = run(db_with_coa, new)                                                 # und jedes weitere Mal
    assert (third['imported'], third['skipped'], third['subaccount_rows']) == (0, 2, 1)
    assert query(db_with_coa, 'SELECT * FROM Bookings ORDER BY ID') == stored


def test_a_fresh_import_of_the_same_data_lands_on_the_own_subaccount(db_with_coa, tmp_path):
    _old, new = _old_and_new(tmp_path)

    result = run(db_with_coa, new)

    assert (result['imported'], result['subaccount_rows']) == (2, 0)
    assert [row[0] for row in _on_accounts(db_with_coa, 'Probe')] == [6532, 6850]
    assert run(db_with_coa, new)['subaccount_rows'] == 0                  # kein Basiskonto-Fall mehr


def test_the_count_needs_date_amount_and_text_to_match(db_with_coa, tmp_path):
    """Die Zeile der Gruppe wird nur gezählt, wenn Datum, Betrag und Text stimmen – sonst bleibt sie
    stehen, ohne gezählt zu werden (und ohne dass etwas neu entsteht)."""
    old, new = _old_and_new(tmp_path)
    run(db_with_coa, old)
    execute(db_with_coa, "UPDATE Bookings SET Text = 'vom Nutzer umbenannt' WHERE Text = 'Probe Kfz Fahrzeug A'")

    result = run(db_with_coa, new)

    assert (result['imported'], result['skipped'], result['subaccount_rows']) == (0, 2, 0)


def test_both_sides_may_be_subaccounts(db_with_coa, tmp_path):
    """Konto und Gegenkonto sind Unterkonten (hier „Kfz-Kosten an Beispielkosten“)."""
    records = [_record(4532, 4927, 80.0, 'Probe Umbuchung', ref='PR-S03')]
    kwargs = dict(subaccounts=[SUB, (4927, 4925, 'Projekt B')])
    old = _fdb(tmp_path / 'old.fdb', records, **kwargs)
    new = _fdb(tmp_path / 'new.fdb', records, extern=[(4532, 7, 82, '6532'), (4927, 7, 82, '6812')], **kwargs)
    run(db_with_coa, old)
    assert _on_accounts(db_with_coa, 'Probe')[0][:2] == (6530, 6810)

    result = run(db_with_coa, new)

    assert (result['imported'], result['subaccount_rows']) == (0, 1)
    assert _on_accounts(db_with_coa, 'Probe')[0][:2] == (6530, 6810)


def test_the_page_names_the_rows_that_stay_on_the_base_account(db_with_coa, tmp_path):
    old, new = _old_and_new(tmp_path)
    run(db_with_coa, old)
    os.makedirs(userctx.user_data_dir(), exist_ok=True)

    status, location = handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [str(new)]})

    assert status == 303 and 'wiso_fdb=ok' in location
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), encoding='utf-8') as handle:
        stored = json.load(handle)
    assert stored['subaccount_rows'] == 1
    html = PageMiscellaneous(db_with_coa)
    assert NOTICE_SUBACCOUNTS.format(1) in html


# ── Übernahme CSV → Datenbank mit einem Unterkonto ───────────────────────────

CSV_SUB = ('6532', '1800', '119,00', 'PR-S01', '', '05.03.2026', 'Probe Kfz Fahrzeug A')


def _csv_entries(db):
    return query(db, "SELECT ID, COA_ID, CounterCOA_ID, SourceGroup, Amount FROM Bookings "
                     "WHERE BookingType = 'entry' AND Text = 'Probe Kfz Fahrzeug A' ORDER BY ID")


def test_a_csv_row_on_a_missing_subaccount_is_adopted_and_gets_the_own_number(db_with_coa, tmp_path):
    """Die CSV trägt 6532, das Konto fehlt im Kontenrahmen: Der Textimport legt die Zeile ohne Konto an.
    Der Datenbank-Import übernimmt sie und füllt jetzt 6532 – nicht das Basiskonto 6530."""
    csv = db_with_coa.import_wiso_csv(csv_export(CSV_SUB))
    assert csv['missing_coa'] == [6532] and csv['imported'] == 1
    ((csv_id, csv_coa, _counter, csv_group, csv_amount),) = _csv_entries(db_with_coa)
    assert csv_coa is None and csv_group is None
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), subaccounts=[SUB], extern=[(4532, 7, 82, '6532')])

    result = run(db_with_coa, path)

    assert (result['imported'], result['adopted']) == (0, 1)
    ((row_id, coa_id, counter_id, group, amount),) = _csv_entries(db_with_coa)
    assert (row_id, amount) == (csv_id, csv_amount) and group == '701'
    assert coa_id == coa(db_with_coa, 6532) and counter_id == coa(db_with_coa, 1800)
    assert 6532 in result['created_coa'] and 6530 not in result['created_coa']


def test_a_csv_row_on_an_existing_subaccount_is_adopted_without_a_second_row(db_with_coa, tmp_path):
    """6532 steht vorab im Kontenrahmen, die CSV-Zeile trägt es: Der Datenbank-Import übernimmt sie."""
    assert db_with_coa.insert_chart_of_accounts(4, 6532, 'Kfz-Betriebskosten Fahrzeug A', '')
    csv = db_with_coa.import_wiso_csv(csv_export(CSV_SUB))
    assert csv['missing_coa'] == [] and csv['imported'] == 1
    ((csv_id, csv_coa, _counter, _group, _amount),) = _csv_entries(db_with_coa)
    assert csv_coa == coa(db_with_coa, 6532)
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), subaccounts=[SUB], extern=[(4532, 7, 82, '6532')])

    result = run(db_with_coa, path)

    assert (result['imported'], result['adopted'], result['created_coa']) == (0, 1, {})
    ((row_id, coa_id, _counter, group, _amount),) = _csv_entries(db_with_coa)
    assert (row_id, coa_id, group) == (csv_id, coa(db_with_coa, 6532), '701')


def test_without_the_stored_number_that_csv_row_would_not_have_matched(db_with_coa, tmp_path):
    """Gegenprobe: Ohne gespeicherte Nummer (6530 aus dem Basiskonto) passt die CSV-Zeile auf 6532 nicht,
    und es entsteht ein zweiter Satz – deshalb braucht die Übernahme die eigene Nummer."""
    assert db_with_coa.insert_chart_of_accounts(4, 6532, 'Kfz-Betriebskosten Fahrzeug A', '')
    db_with_coa.import_wiso_csv(csv_export(CSV_SUB))
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), subaccounts=[SUB])          # ohne FINT_EXTERNDEFS

    result = run(db_with_coa, path)

    assert (result['imported'], result['adopted']) == (1, 0)
    assert len(_csv_entries(db_with_coa)) == 2


# ── Grenzen der gespeicherten Nummer ─────────────────────────────────────────

def test_a_known_equivalent_keeps_its_assignment_despite_a_stored_number(tmp_path):
    """640 steht in ``KNOWN_EQUIVALENTS`` (3160): Eine gespeicherte Nummer ändert daran nichts."""
    path = _fdb(tmp_path / 'a.fdb', [_record(640, 1200, 100.0, 'Darlehen')], extern=[(640, 7, 82, '3150')])

    data = read_wiso_database(str(path))

    (booking,) = data.bookings
    assert (booking.account, booking.legacy_account) == (3160, None)     # wie bis 2.2.4: nichts weicht ab
    assert data.chart[640].skr04 == 3160


def test_a_known_equivalent_with_a_base_account_inherits_it_as_before(tmp_path):
    """Hat das Konto ein Basiskonto, gilt – wie bis 2.2.4 – dessen Nummer; die gespeicherte bleibt außen vor."""
    path = _fdb(tmp_path / 'a.fdb', [_record(640, 1200, 100.0, 'Darlehen')], subaccounts=[(640, 4530, 'Darlehen X')],
                extern=[(640, 7, 82, '3150')])

    (booking,) = read_wiso_database(str(path)).bookings

    assert (booking.account, booking.legacy_account) == (6530, None)


def test_a_known_equivalent_is_no_base_account_case_for_the_count(db_with_coa, tmp_path):
    """Die Zuordnung von 640 ändert sich durch die gespeicherte Nummer nicht: Der Re-Import zählt den Satz nicht
    als Fall auf dem Basiskonto (``subaccount_rows``)."""
    records = [_record(640, 1200, 100.0, 'Darlehen', ref='PR-K1')]
    old = _fdb(tmp_path / 'old.fdb', records)
    new = _fdb(tmp_path / 'new.fdb', records, extern=[(640, 7, 82, '3150')])
    run(db_with_coa, old)

    result = run(db_with_coa, new)

    assert (result['imported'], result['skipped'], result['subaccount_rows']) == (0, 1, 0)
    assert [row[0] for row in _on_accounts(db_with_coa, 'Darlehen')] == [3160]


@pytest.mark.parametrize('value,expected', [('9999', 9999), ('10000', 6530), ('12345', 6530), ('1', 1)],
                         ids=['obere Grenze', 'zu groß', 'fünfstellig', 'untere Grenze'])
def test_a_stored_number_is_a_four_digit_account_number(tmp_path, value, expected):
    path = _fdb(tmp_path / 'a.fdb', _sub_booking(), subaccounts=[SUB], extern=[(4532, 7, 82, value)])

    assert _account_of(path) == expected


def test_personal_accounts_never_get_an_entry_from_the_stored_numbers(tmp_path):
    """Debitoren und Kreditoren tragen in beiden Rahmen dieselbe Nummer: kein Eintrag, auch keine Bezeichnung."""
    path = _fdb(tmp_path / 'a.fdb', [_record(10005, 1200, 50.0, 'Debitor')],
                extern=[(10005, 7, 82, '6532'), (4532, 7, 82, '6532')])

    data = read_wiso_database(str(path))

    assert 10005 not in data.chart and 4532 in data.chart
    (booking,) = data.bookings
    assert (booking.account, booking.legacy_account) == (10005, None)  # Personenkonten bleiben, wie sie sind


# ── Zahlungskonten unter den Unterkonten ─────────────────────────────────────

BANK_SUB = (1201, 1200, 'Bank B')           # Unterkonto des Bankkontos 1200 (SKR04 1800)


def _bank_sub_records():
    return [_record(4980, 1201, 59.5, 'Probe Bank B', ref='PR-B1'),
            _record(4980, 1201, 20.0, 'Probe Bank B zwei', ref='PR-B2')]


def test_a_subaccount_of_a_payment_account_that_is_none_in_the_app_is_counted(tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _bank_sub_records(), subaccounts=[BANK_SUB], extern=[(1201, 7, 82, '1801')])

    data = read_wiso_database(str(path), liquid_accounts={1800, 1810})

    assert data.liquid_subaccounts == [{'nummer': 1801, 'basis': 1800, 'anzahl': 2}]
    first, _second = data.bookings
    assert (first.account, first.counter_account, first.legacy_counter_account) == (6850, 1801, 1800)
    assert first.form == 'transfer' and first.amount == pytest.approx(59.5)   # keine Sonderbehandlung


def test_a_subaccount_that_is_a_payment_account_of_the_app_is_not_reported(tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _bank_sub_records(), subaccounts=[BANK_SUB], extern=[(1201, 7, 82, '1810')])

    data = read_wiso_database(str(path), liquid_accounts={1800, 1810})

    assert data.liquid_subaccounts == []
    assert data.bookings[0].form == 'purpose_first' and data.bookings[0].amount == pytest.approx(-59.5)


@pytest.mark.parametrize('extern', [None, [(4999, 7, 82, '1801')]], ids=['ohne Tabelle', 'andere Konten'])
def test_without_a_stored_number_the_base_account_stays_liquid_and_nothing_is_reported(tmp_path, extern):
    path = _fdb(tmp_path / 'a.fdb', _bank_sub_records(), subaccounts=[BANK_SUB], extern=extern)

    data = read_wiso_database(str(path), liquid_accounts={1800, 1810})

    assert data.liquid_subaccounts == []
    assert data.bookings[0].counter_account == 1800


def test_both_sides_and_every_account_are_counted_apart(tmp_path):
    records = [_record(1201, 1202, 10.0, 'Probe Umbuchung', ref='PR-B3'), _record(1202, 1200, 5.0, 'Probe Rest', ref='PR-B4')]
    path = _fdb(tmp_path / 'a.fdb', records, subaccounts=[BANK_SUB, (1202, 1210, 'Bank C')],
                extern=[(1201, 7, 82, '1801'), (1202, 7, 82, '1811')])

    data = read_wiso_database(str(path), liquid_accounts={1800, 1810})

    assert data.liquid_subaccounts == [{'nummer': 1801, 'basis': 1800, 'anzahl': 1},
                                       {'nummer': 1811, 'basis': 1810, 'anzahl': 2}]


def test_the_import_reports_the_subaccounts_of_payment_accounts_and_cuts_the_list_at_50(db_with_coa, tmp_path):
    count = 60
    records = [_record(4980, 1300 + n, 1.0, f'Probe Bank {n:02d}', ref=f'PR-N{n:02d}') for n in range(count)]
    subaccounts = [(1300 + n, 1200, f'Bank {n:02d}') for n in range(count)]
    extern = [(1300 + n, 7, 82, str(1900 + n)) for n in range(count)]
    path = _fdb(tmp_path / 'a.fdb', records, subaccounts=subaccounts, extern=extern)

    result = run(db_with_coa, path)

    assert result['imported'] == count and len(result['liquid_subaccounts']) == 50
    assert result['liquid_subaccounts'][0] == {'nummer': 1900, 'basis': 1800, 'anzahl': 1}
    assert [row['nummer'] for row in result['liquid_subaccounts']] == list(range(1900, 1950))
    json.dumps(result)                                                    # die Ergebnisdatei bleibt schreibbar


def test_the_import_names_the_subaccount_of_a_payment_account_on_the_page(db_with_coa, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    path = _fdb(tmp_path / 'a.fdb', _bank_sub_records(), subaccounts=[BANK_SUB], extern=[(1201, 7, 82, '1801')])

    status, location = handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [str(path)]})

    assert status == 303 and 'wiso_fdb=ok' in location
    html = PageMiscellaneous(db_with_coa)
    assert NOTICE_LIQUID in html and '<tr><td>1801</td><td>1800</td><td>2</td></tr>' in html


# ── Das Anlagekonto einer vorhandenen Anlage bleibt ──────────────────────────

def _asset_account(db, number='1'):
    return query(db, 'SELECT c.AccountNumber FROM Assets a JOIN ChartOfAccounts c ON c.ID = a.COA_ID '
                     'WHERE a.InventoryNumber = ?', (number,))[0][0]


def test_an_existing_asset_keeps_its_asset_account_on_a_reimport(db_with_coa, tmp_path):
    """Bestand: Anlage auf 520 (über das Basiskonto 320). Der Leser nennt jetzt 521 – das Anlagekonto bleibt."""
    asset = _asset(account=321, plan=[('2024-12-31', 1250.0, 1250.0)])
    old = _fdb(tmp_path / 'old.fdb', assets=[asset], subaccounts=[(321, 320, 'Pkw B')])
    new = _fdb(tmp_path / 'new.fdb', assets=[asset], subaccounts=[(321, 320, 'Pkw B')], extern=[(321, 7, 82, '521')])
    run(db_with_coa, old)
    assert _asset_account(db_with_coa) == 520

    result = run(db_with_coa, new)

    assert result['assets'] == 1 and _asset_account(db_with_coa) == 520
    assert query(db_with_coa, "SELECT COUNT(*) FROM Assets WHERE InventoryNumber = '1'") == [(1,)]


def test_a_fresh_asset_gets_the_stored_asset_account(db_with_coa, tmp_path):
    asset = _asset(account=321, plan=[('2024-12-31', 1250.0, 1250.0)])
    new = _fdb(tmp_path / 'new.fdb', assets=[asset], subaccounts=[(321, 320, 'Pkw B')], extern=[(321, 7, 82, '521')])

    run(db_with_coa, new)

    assert _asset_account(db_with_coa) == 521


def test_an_asset_without_an_account_in_the_app_gets_the_one_from_the_import(db_with_coa, tmp_path):
    asset = _asset()
    path = _fdb(tmp_path / 'a.fdb', assets=[asset])
    run(db_with_coa, path)
    execute(db_with_coa, 'UPDATE Assets SET COA_ID = NULL')

    run(db_with_coa, path)

    assert _asset_account(db_with_coa) == 520


# ═════════════════════════════════════════════════════════════════════════════
# Teil 2 – vorausgeplante AfA
# ═════════════════════════════════════════════════════════════════════════════

CAR = _asset()
JUNE, YEAR_END = '2026-06-30', '2026-12-31'          # Stichtage: vor und auf dem Datum des Planjahres 2026


def _car_fdb(tmp_path, asset=CAR, records=(), name='afa.fdb', **kwargs):
    return _fdb(tmp_path / name, _afa_records(asset) + list(records), assets=[asset], **kwargs)


def test_afa_rows_before_on_and_after_the_cutoff(tmp_path):
    """Am Stichtag wird gebucht (das Datum liegt nicht danach), danach nicht: Der Leser liefert die Sätze nach dem
    Stichtag gekennzeichnet (``future_depreciation``) und zählt sie nirgends mit; die Planjahre der Anlage bleiben
    vollständig."""
    steuerzeile = dict(_record(1776, 1200, None, 'Steuer'), AMOUNTGROSS=None)
    path = _car_fdb(tmp_path, records=[steuerzeile])

    data = read_wiso_database(str(path), today=YEAR_END)

    assert [(b.date, b.future_depreciation) for b in data.bookings] == [
        ('2024-12-31', False), ('2025-12-31', False), ('2026-12-31', False),
        ('2027-12-31', True), ('2028-12-31', True)]
    assert all(b.is_depreciation and b.inventory_id == CAR['id'] for b in data.bookings)
    assert (data.tax_rows_skipped, data.memo_rows_skipped) == (1, 0)    # die Zählung stimmt weiter
    (asset,) = data.assets
    assert [d.year for d in asset.depreciations] == [2024, 2025, 2026, 2027, 2028]


def test_the_cutoff_may_be_a_date_a_datetime_or_left_out(tmp_path):
    far = [('2020-12-31', 100.0, 100.0), ('2099-12-31', 100.0, 200.0)]
    path = _car_fdb(tmp_path, _asset(plan=far))

    for today in (datetime.date(2026, 6, 30), datetime.datetime(2026, 6, 30, 23, 59), '2026-06-30',
                  '2026-06-30 12:00:00', None):                      # None: heute
        data = read_wiso_database(str(path), today=today)
        assert [(b.date, b.future_depreciation) for b in data.bookings] == [
            ('2020-12-31', False), ('2099-12-31', True)], today
        assert [d.year for d in data.assets[0].depreciations] == [2020, 2099], today      # der Plan bleibt vollständig


def test_a_future_year_is_planned_a_past_year_booked(db_with_coa, tmp_path):
    path = _car_fdb(tmp_path)

    result = run(db_with_coa, path, today=JUNE)

    assert (result['assets'], result['depreciations'], result['depreciations_planned']) == (1, 5, 3)
    assert (result['future_depreciations_skipped'], result['imported']) == (3, 2)
    assert [row[:2] + row[3:] for row in _plan(db_with_coa)] == [
        (2024, 12500000, 'booked', None), (2025, 15000000, 'booked', None),
        (2026, 15000000, 'planned', None), (2027, 15000000, 'planned', None),
        (2028, 2500000, 'planned', None)]
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31']


def test_a_planned_year_can_be_booked_in_the_app(db_with_coa, tmp_path):
    run(db_with_coa, _car_fdb(tmp_path), today=JUNE)
    asset_id = query(db_with_coa, 'SELECT ID FROM Assets')[0][0]

    booking_id = db_with_coa.book_depreciation(asset_id, 2026, coa_id_expense=coa(db_with_coa, 6222))

    assert query(db_with_coa, 'SELECT Status, Booking_ID FROM AssetDepreciations WHERE Year = 2026') == [
        ('posted', booking_id)]
    with pytest.raises(ValueError, match='bereits gebucht'):             # das vergangene Jahr ist 'booked'
        db_with_coa.book_depreciation(asset_id, 2025, coa_id_expense=coa(db_with_coa, 6222))


def test_a_satz_with_an_inventory_id_but_no_amort_id_is_imported_when_the_column_exists(db_with_coa, tmp_path):
    """Eine Abgangsbuchung am Anlagegut trägt ``INVENTORYID``, aber keine ``INVENTORYAMORTID``."""
    other = _record(1360, 8400, 500.0, 'Abgang Dienstwagen', date='2027-01-15', INVENTORYID=CAR['id'])
    path = _car_fdb(tmp_path, records=[other])

    data = read_wiso_database(str(path), today=JUNE)

    assert [(b.text, b.future_depreciation) for b in data.bookings if not b.is_depreciation] == [
        ('Abgang Dienstwagen', False)]                                  # nach dem Stichtag, aber kein AfA-Satz
    assert sum(b.future_depreciation for b in data.bookings) == 3       # nur die drei AfA-Sätze
    result = run(db_with_coa, path, today=JUNE)
    assert (result['imported'], result['future_depreciations_skipped']) == (3, 3)   # zwei AfA-Sätze und der Abgang


def test_without_the_amort_column_the_afa_table_tells_an_afa_satz_from_a_disposal(tmp_path):
    """Ohne ``INVENTORYAMORTID`` erkennt der Leser den AfA-Satz an Datum und Betrag einer Zeile der AfA-Tabelle
    derselben Anlage; eine Abgangsbuchung am Anlagegut ist keiner und wird nie übersprungen."""
    other = _record(1360, 8400, 500.0, 'Abgang Dienstwagen', date='2027-01-15', INVENTORYID=CAR['id'])
    path = _fdb(tmp_path / 'a.fdb', _afa_records(CAR, amort=False) + [other], assets=[CAR],
                booking_columns=('INVENTORYID',))

    data = read_wiso_database(str(path), today=JUNE)

    assert [(b.date, b.is_depreciation, b.depreciation_year, b.future_depreciation) for b in data.bookings] == [
        ('2024-12-31', True, 2024, False), ('2025-12-31', True, 2025, False),
        ('2026-12-31', True, 2026, True), ('2027-12-31', True, 2027, True),      # die drei AfA-Sätze 2026 bis 2028
        ('2028-12-31', True, 2028, True), ('2027-01-15', False, None, False)]
    with WisoDatabase(str(path)) as wiso:                              # kein Blocker, wenn sie fehlt
        blocker, hints = wiso.check()
    assert blocker == [] and not any('INVENTORYAMORTID' in text for text in hints)


def test_a_satz_with_a_column_but_no_value_is_no_afa_satz_even_on_the_day_of_an_afa_row(db_with_coa, tmp_path):
    """Eine Abgangsbuchung am Anlagegut: ``INVENTORYID`` gesetzt, ``INVENTORYAMORTID`` leer (die Spalte gibt es),
    am Tag und mit dem Betrag einer AfA-Zeile derselben Anlage. Sie ist kein AfA-Satz: Der Stichtag überspringt
    sie nie (auch nach dem Stichtag kommt sie), und eine Buchung der App für dieses Jahr ist kein Konflikt."""
    disposal = _record(1360, 8400, 1500.0, 'Abgang Dienstwagen', date='2026-12-31', INVENTORYID=CAR['id'])
    path = _car_fdb(tmp_path, records=[disposal])

    data = read_wiso_database(str(path), today=JUNE)

    assert [(b.text, b.is_depreciation, b.depreciation_year, b.future_depreciation) for b in data.bookings
            if b.text == 'Abgang Dienstwagen'] == [('Abgang Dienstwagen', False, None, False)]
    assert sum(b.future_depreciation for b in data.bookings) == 3       # nur die drei AfA-Sätze 2026 bis 2028
    run(db_with_coa, path, today=JUNE)
    _book_in_app(db_with_coa, 2026)                                     # die App bucht ihr Jahr 2026

    again = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (again['depreciation_conflicts'], again['imported']) == (1, 0)     # der AfA-Satz 2026 ist der Konflikt
    assert [row[0] for row in query(db_with_coa, "SELECT DateBooking FROM Bookings WHERE Text = ?",
                                    ('Abgang Dienstwagen',))] == ['2026-12-31']      # die Abgangsbuchung ist da, einmal


def test_without_the_column_a_satz_on_the_day_of_an_afa_row_needs_the_same_amount(tmp_path):
    """Die Tabelle hat die Spalte nicht: gleiche Anlage und gleiches Datum genügen nicht, der Betrag muss auch
    stimmen (|AMOUNTGROSS| = |AMORT_AMOUNT|, auf den Cent) – schon ein Cent Unterschied ist ein anderer Betrag."""
    same = _record(4832, 320, 1500.0, 'Afa fuer Inventar Dienstwagen', date='2026-12-31', INVENTORYID=CAR['id'])
    other = _record(4832, 320, 1499.98, 'Anderer Betrag', date='2026-12-31', INVENTORYID=CAR['id'])
    cent = _record(4832, 320, 1499.99, 'Einen Cent weniger', date='2026-12-31', INVENTORYID=CAR['id'])
    path = _fdb(tmp_path / 'a.fdb', [same, other, cent], assets=[CAR], booking_columns=('INVENTORYID',))

    data = read_wiso_database(str(path), today='2099-01-01')

    assert [(b.text, b.is_depreciation) for b in data.bookings] == [
        ('Afa fuer Inventar Dienstwagen', True), ('Anderer Betrag', False), ('Einen Cent weniger', False)]


def test_amounts_are_compared_without_sign_and_to_the_cent():
    assert _cents(1500.0) == _cents(-1500.0) == _cents(1500.004) == 15000000
    assert _cents(1499.99) == 14999900 and _cents(0.01) == 100 and _cents(-0.01) == 100
    assert _cents(None) is None


def test_without_the_column_a_differing_amount_is_imported_even_after_the_cutoff(tmp_path):
    other = _record(1360, 8400, 400.0, 'Abgang Dienstwagen', date='2027-12-31', INVENTORYID=CAR['id'])
    same = _record(1360, 8400, 1500.0, 'Umbuchung Dienstwagen', date='2027-12-31', INVENTORYID=CAR['id'])
    path = _fdb(tmp_path / 'a.fdb', [other, same], assets=[CAR], booking_columns=('INVENTORYID',))

    data = read_wiso_database(str(path), today=JUNE)

    assert [(b.text, b.is_depreciation, b.future_depreciation) for b in data.bookings] == [
        ('Abgang Dienstwagen', False, False),                                  # ein anderer Betrag: kein AfA-Satz
        ('Umbuchung Dienstwagen', True, True)]                                 # der gleiche Betrag gilt als AfA-Satz


def test_the_amount_of_a_negative_afa_row_matches_the_booking_without_sign(tmp_path):
    """Eine Korrektur der AfA (``AMORT_AMOUNT`` −0,01) steht als Satz mit dem Betrag 0,01 und vertauschten Konten."""
    plan = [('2025-12-31', 900.0, 900.0), ('2026-12-31', -0.01, 899.99)]
    asset = _asset(plan=plan)
    path = _fdb(tmp_path / 'a.fdb', [_record(320, 4832, 0.01, 'Korrektur', date='2026-12-31', INVENTORYID=asset['id'])],
                assets=[asset], booking_columns=('INVENTORYID',))

    data = read_wiso_database(str(path), today='2099-01-01')

    assert [(b.text, b.is_depreciation, b.depreciation_year) for b in data.bookings] == [('Korrektur', True, 2026)]


def test_an_older_record_format_without_the_column_is_matched_by_the_afa_table(tmp_path):
    """Die Spalte kam später dazu: Sätze im älteren Format kennen sie nicht (kein Schlüssel statt ``None``) und
    werden an der AfA-Tabelle erkannt; Sätze im aktuellen Format mit leerem Wert sind keine AfA-Sätze."""
    older = _afa_records(CAR, amort=False)[:2]                                  # 2024 und 2025, im älteren Format
    disposal = _record(1360, 8400, 1500.0, 'Abgang Dienstwagen', date='2026-12-31', INVENTORYID=CAR['id'])
    current = _afa_records(CAR)[2:3]                                            # 2026 mit Verweis, aktuelles Format
    path = _fdb(tmp_path / 'a.fdb', current + [disposal], assets=[CAR], old_records=older)

    data = read_wiso_database(str(path), today='2099-01-01')

    assert sorted((b.date, b.text, b.is_depreciation, b.depreciation_year) for b in data.bookings) == [
        ('2024-12-31', 'Afa fuer Inventar Dienstwagen', True, 2024),
        ('2025-12-31', 'Afa fuer Inventar Dienstwagen', True, 2025),
        ('2026-12-31', 'Abgang Dienstwagen', False, None),
        ('2026-12-31', 'Afa fuer Inventar Dienstwagen', True, 2026)]


def test_the_catalog_tells_a_missing_column_from_an_empty_value(tmp_path):
    """Was ``catalog.rows`` hergibt: Ein Satz des älteren Formats hat keinen Schlüssel der später ergänzten
    Spalte, ein Satz des aktuellen Formats mit leerem Wert den Schlüssel mit ``None``."""
    older = _afa_records(CAR, amort=False)[:1]
    current = [_record(4980, 1200, 1.0, 'neu', INVENTORYID=None, INVENTORYAMORTID=None)]
    path = _fdb(tmp_path / 'a.fdb', current, old_records=older)

    with WisoDatabase(str(path)) as wiso:
        rows = list(wiso.catalog.rows('MOV_FINACC_ACCRECORDS'))

    by_text = {row['ACCOUNTING_TEXT']: row for row in rows}
    assert 'INVENTORYAMORTID' not in by_text['Afa fuer Inventar Dienstwagen']
    assert 'INVENTORYAMORTID' in by_text['neu'] and by_text['neu']['INVENTORYAMORTID'] is None


def test_the_planning_year_of_a_satz_is_the_year_of_its_afa_row(tmp_path):
    """Über ``INVENTORYAMORTID`` gilt das Jahr der AfA-Zeile – auch wenn der Satz in einem anderen Jahr
    steht (hier im Januar des Folgejahres gebucht); sonst das Jahr des Satzdatums."""
    records = _afa_records(CAR)
    records[0]['ACCOUNTING_DATE'] = '2025-01-02 00:00:00'              # die AfA 2024, im Januar 2025 gebucht
    path = _fdb(tmp_path / 'a.fdb', records, assets=[CAR])

    data = read_wiso_database(str(path), today=JUNE)

    assert [(b.date, b.depreciation_year) for b in data.bookings if not b.future_depreciation] == [
        ('2025-01-02', 2024), ('2025-12-31', 2025)]


@pytest.mark.parametrize('columns', [('INVENTORYID', 'INVENTORYAMORTID'), ('INVENTORYID',), ()],
                         ids=['mit beiden Spalten', 'nur INVENTORYID', 'ohne Anlagenspalten'])
def test_a_satz_without_an_inventory_id_is_never_planned(tmp_path, columns):
    """Auch mit Zukunftsdatum: Ein Satz ohne ``INVENTORYID`` kommt wie bisher."""
    path = _fdb(tmp_path / 'a.fdb', [_record(4980, 1200, 10.0, 'Vorauszahlung', date='2027-02-01')],
                booking_columns=columns)

    data = read_wiso_database(str(path), today=JUNE)

    assert [b.text for b in data.bookings] == ['Vorauszahlung']
    assert not data.bookings[0].is_depreciation and not data.bookings[0].future_depreciation


def test_the_memo_value_after_the_disposal_year_still_counts_as_memo(tmp_path):
    """Nach dem Abgangsjahr plant WISO den Erinnerungswert weiter – ein künftiges Datum macht daraus
    keinen vorausgeplanten Satz: Die Regel für den Erinnerungswert hat Vorrang."""
    sold = _asset(number=1, label='Verkauft', sale='2025-06-30',
                  plan=[('2025-12-31', 900.0, 900.0), ('2026-12-31', 0.01, 900.01), ('2027-12-31', 0.01, 900.02)])
    running = _asset(number=2, label='Läuft noch', plan=[('2026-12-31', 500.0, 500.0), ('2027-12-31', 500.0, 1000.0)])
    path = _fdb(tmp_path / 'a.fdb', _afa_records(sold) + _afa_records(running), assets=[sold, running])

    data = read_wiso_database(str(path), today=JUNE)

    assert data.memo_rows_skipped == 2                                 # 2026 und 2027 des verkauften
    assert [(b.date, b.future_depreciation) for b in data.bookings] == [
        ('2025-12-31', False),                                         # das Abgangsjahr des verkauften
        ('2026-12-31', True), ('2027-12-31', True)]                    # 2026 und 2027 der laufenden: gekennzeichnet
    assert data.tax_rows_skipped == 0


def test_a_later_import_books_a_year_that_has_become_past_exactly_once(db_with_coa, tmp_path):
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today=JUNE)
    assert [row[3] for row in _plan(db_with_coa)] == ['booked', 'booked', 'planned', 'planned', 'planned']

    second = run(db_with_coa, path, today='2027-01-15')

    assert (second['imported'], second['future_depreciations_skipped'], second['depreciations_planned']) == (1, 2, 2)
    assert [row[3] for row in _plan(db_with_coa)] == ['booked', 'booked', 'booked', 'planned', 'planned']
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31', '2026-12-31']
    third = run(db_with_coa, path, today='2027-01-15')
    assert (third['imported'], third['skipped']) == (0, 3)              # nichts doppelt
    assert [row[3] for row in _plan(db_with_coa)] == ['booked', 'booked', 'booked', 'planned', 'planned']
    assert len(_afa_bookings(db_with_coa)) == 3


def test_a_later_import_updates_a_planned_year_but_keeps_it_planned(db_with_coa, tmp_path):
    run(db_with_coa, _car_fdb(tmp_path), today=JUNE)
    changed = _asset(plan=[(date, amount + 10.0 if date[:4] == '2027' else amount, cumulated)
                           for date, amount, cumulated in AFA_PLAN])

    run(db_with_coa, _car_fdb(tmp_path, changed, name='changed.fdb'), today=JUNE)

    assert [row[:2] + row[3:] for row in _plan(db_with_coa)][3] == (2027, 15100000, 'planned', None)


def test_a_year_booked_in_the_app_is_not_imported_a_second_time(db_with_coa, tmp_path):
    """In der App gebucht (2026), danach liefert WISO den AfA-Satz des Jahres nicht mehr als künftig: Er
    wird nicht angelegt, die Planzeile bleibt ganz unverändert, und der Fall wird gemeldet."""
    other = _asset(plan=[(date, 1400.0 if date[:4] == '2026' else amount, cumulated)
                         for date, amount, cumulated in AFA_PLAN])    # WISOs Zahl weicht von der App ab
    path = _car_fdb(tmp_path, other)
    run(db_with_coa, path, today=JUNE)
    asset_id = query(db_with_coa, 'SELECT ID FROM Assets')[0][0]
    booking_id = db_with_coa.book_depreciation(asset_id, 2026, coa_id_expense=coa(db_with_coa, 6222))
    plan_before = _plan(db_with_coa)
    booked_before = query(db_with_coa, 'SELECT BookedAt FROM AssetDepreciations WHERE Year = 2026')
    count_before = len(query(db_with_coa, 'SELECT ID FROM Bookings'))
    assert plan_before[2][3:] == ('posted', booking_id)

    result = run(db_with_coa, path, today='2027-01-15')

    assert result['depreciation_conflicts'] == 1
    assert result['depreciation_conflict_rows'] == [{'anlage': 'Dienstwagen', 'jahr': 2026}]
    assert result['imported'] == 0                                      # kein zweiter AfA-Satz für 2026
    assert _plan(db_with_coa)[:3] == plan_before[:3]                   # Betrag, Status, Buchung: unverändert
    assert query(db_with_coa, 'SELECT BookedAt FROM AssetDepreciations WHERE Year = 2026') == booked_before
    assert len(query(db_with_coa, 'SELECT ID FROM Bookings')) == count_before
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31', '2026-12-31']
    assert [row[1] for row in _afa_bookings(db_with_coa)][2] is None   # die von der App, ohne Quellgruppe


def test_a_future_year_booked_in_the_app_stays_as_it_is(db_with_coa, tmp_path):
    """Die App bucht ein Jahr, das für den Import noch künftig ist (2026 bei Stichtag 30.06.2026): Ein
    erneuter Import mit demselben Stichtag aktualisiert die Planzeile nicht – sie trägt die Buchung der App."""
    other = _asset(plan=[(date, 1400.0 if date[:4] == '2026' else amount, cumulated)
                         for date, amount, cumulated in AFA_PLAN])    # WISOs Zahl weicht von der App ab
    path = _car_fdb(tmp_path, other)
    run(db_with_coa, path, today=JUNE)
    asset_id = query(db_with_coa, 'SELECT ID FROM Assets')[0][0]
    booking_id = db_with_coa.book_depreciation(asset_id, 2026, coa_id_expense=coa(db_with_coa, 6222))
    before = _plan(db_with_coa)
    assert before[2] == (2026, 15000000, 17500000, 'posted', booking_id)   # die Zahlen der App: 1.500 € / 1.750 €

    result = run(db_with_coa, path, today=JUNE)

    assert _plan(db_with_coa) == before
    assert (result['imported'], result['depreciation_conflicts']) == (0, 0)
    assert [row[3] for row in _plan(db_with_coa)] == ['booked', 'booked', 'posted', 'planned', 'planned']


def test_a_conflict_counts_each_asset_and_year_once_and_names_the_asset_masked(db_with_coa, tmp_path):
    label = 'Wagen <script>alert(1)</script>'
    asset = _asset(label=label, plan=[('2025-12-31', 1500.0, 1500.0), ('2026-12-31', 1500.0, 3000.0)])
    twice = _record(4832, 320, 5.0, 'Afa fuer Inventar Nachtrag', date='2025-12-31',
                    INVENTORYID=asset['id'], INVENTORYAMORTID=_amort_id(asset, 1))
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset) + [twice], assets=[asset])
    run(db_with_coa, path, today='2025-06-30')                          # beide Jahre noch künftig
    asset_id = query(db_with_coa, 'SELECT ID FROM Assets')[0][0]
    db_with_coa.book_depreciation(asset_id, 2025, coa_id_expense=coa(db_with_coa, 6222))
    os.makedirs(userctx.user_data_dir(), exist_ok=True)

    result = run(db_with_coa, path, today=YEAR_END)

    assert result['depreciation_conflicts'] == 1                        # zwei Sätze, ein Fall: Anlage + Jahr
    assert result['imported'] == 1                                      # nur 2026 kommt (Planjahr ohne App-Buchung)
    assert result['depreciation_conflict_rows'] == [{'anlage': label, 'jahr': 2025}]
    _write_result(result)
    html = PageMiscellaneous(db_with_coa)
    assert 'AfA schon in der App gebucht &ndash; 1' in html
    assert NOTICE_CONFLICT in html
    assert '<td>Wagen &lt;script&gt;alert(1)&lt;/script&gt;</td><td>2025</td>' in html
    assert '<script>alert(1)</script>' not in html


def test_the_conflict_is_named_by_inventory_number_when_the_asset_has_no_label(db_with_coa, tmp_path):
    asset = _asset(number=7, label='', plan=[('2025-12-31', 1500.0, 1500.0)])
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset), assets=[asset])
    run(db_with_coa, path, today='2025-06-30')
    asset_id = query(db_with_coa, 'SELECT ID FROM Assets')[0][0]
    db_with_coa.book_depreciation(asset_id, 2025, coa_id_expense=coa(db_with_coa, 6222))

    result = run(db_with_coa, path, today=YEAR_END)

    assert result['depreciation_conflict_rows'] == [{'anlage': '7', 'jahr': 2025}]


def test_a_year_of_another_asset_or_a_year_without_an_app_booking_is_no_conflict(db_with_coa, tmp_path):
    first = _asset(number=1, label='Erste', plan=[('2025-12-31', 1000.0, 1000.0), ('2026-12-31', 1000.0, 2000.0)])
    second = _asset(number=2, label='Zweite', plan=[('2025-12-31', 800.0, 800.0), ('2026-12-31', 800.0, 1600.0)])
    path = _fdb(tmp_path / 'a.fdb', _afa_records(first) + _afa_records(second), assets=[first, second])
    run(db_with_coa, path, today='2025-06-30')
    first_id = query(db_with_coa, "SELECT ID FROM Assets WHERE InventoryNumber = '1'")[0][0]
    db_with_coa.book_depreciation(first_id, 2025, coa_id_expense=coa(db_with_coa, 6222))

    result = run(db_with_coa, path, today=YEAR_END)

    assert result['depreciation_conflicts'] == 1
    assert result['depreciation_conflict_rows'] == [{'anlage': 'Erste', 'jahr': 2025}]
    assert result['imported'] == 3                                      # Zweite 2025 und beide 2026
    assert result['future_depreciations_skipped'] == 0


def test_without_a_conflict_nothing_is_reported(db_with_coa, tmp_path):
    result = run(db_with_coa, _car_fdb(tmp_path), today=JUNE)

    assert (result['depreciation_conflicts'], result['depreciation_conflict_rows']) == (0, [])
    assert result['subaccount_rows'] == 0


def test_the_conflict_list_is_sorted_and_limited_to_100_the_counter_counts_all(db_with_coa, monkeypatch):
    links = {number: _AssetLink(f'Anlage {number:03d}', number, {2025: ('posted', True)})
             for number in range(1, 121)}
    monkeypatch.setattr(type(db_with_coa), '_link_assets', lambda self, assets, result, cursor: (links, set()))
    bookings = [WisoBooking(date='2025-12-31', amount=10.0, account=6222, counter_account=520, form='transfer',
                            text='AfA', group=str(number), inventory_id=number, is_depreciation=True,
                            depreciation_year=2025)
                for number in reversed(range(1, 121))]
    result = {key: 0 for key in ('imported', 'skipped', 'adopted', 'possible_duplicates_count',
                                 'unresolved_accounts', 'legacy_mirrors', 'legacy_stock_rows',
                                 'form_conflicts_count', 'subaccount_rows', 'depreciation_conflicts')}
    result.update(adopted_rows=[], possible_duplicates=[], form_conflicts=[], depreciation_conflict_rows=[])

    booked = db_with_coa._insert_wiso_bookings(bookings, result, None)

    assert MAX_CONFLICT_ROWS == 100
    assert (result['depreciation_conflicts'], result['imported'], booked) == (120, 0, set())
    assert len(result['depreciation_conflict_rows']) == 100
    assert [row['anlage'] for row in result['depreciation_conflict_rows']] == [
        f'Anlage {number:03d}' for number in range(1, 101)]
    json.dumps(result)                                                  # die Ergebnisdatei bleibt schreibbar


def test_the_planned_rows_of_an_import_up_to_2_2_4_stay_as_they_are(db_with_coa, tmp_path):
    """Bestand aus einem Import bis 2.2.4: künftige AfA-Buchungen mit Quellgruppe, alle Jahre ``booked``. Ein Import
    mit Stichtag liefert diese Sätze gekennzeichnet; der Import erkennt sie in ihrer Quellgruppe wieder – kein
    Löschen, kein Umstellen, kein zweiter Satz; nur Betrag und Buchwert des Planjahres würden aktualisiert. Egal,
    mit welchem Stichtag."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today='2099-01-01')                         # wie früher: alles als gebucht
    assert [row[3] for row in _plan(db_with_coa)] == ['booked'] * 5 and len(_afa_bookings(db_with_coa)) == 5
    stored = query(db_with_coa, 'SELECT * FROM Bookings ORDER BY ID')
    plan_before = _plan(db_with_coa)

    for today, kept in ((JUNE, 3), (YEAR_END, 2), (AFTER_YEAR_END, 2), ('2099-01-01', 0), ('2020-01-01', 5)):
        result = run(db_with_coa, path, today=today)

        assert (result['imported'], result['skipped']) == (0, 5), today      # alle fünf wiedererkannt, keiner neu
        assert (result['future_depreciations_kept'], result['future_depreciations_skipped']) == (kept, 0), today
        assert result['depreciations_planned'] == 0, today                   # nichts davon wird zu „geplant“
        assert query(db_with_coa, 'SELECT * FROM Bookings ORDER BY ID') == stored, today
        assert _plan(db_with_coa) == plan_before, today                      # Betrag, Buchwert, Status: alles
        assert result['depreciation_plan_mismatches'] == 0, today            # und nichts zu vergleichen


def test_an_existing_booked_year_with_a_new_amount_keeps_its_status(db_with_coa, tmp_path):
    run(db_with_coa, _car_fdb(tmp_path), today=JUNE)
    changed = _asset(plan=[(date, amount + 1.0 if date[:4] == '2024' else amount, cumulated)
                           for date, amount, cumulated in AFA_PLAN])

    run(db_with_coa, _car_fdb(tmp_path, changed, name='changed.fdb'), today=JUNE)

    assert _plan(db_with_coa)[0][:2] + _plan(db_with_coa)[0][3:] == (2024, 12510000, 'booked', None)


def test_the_cutoff_defaults_to_the_day_of_the_import(db_with_coa, tmp_path):
    far = [('2020-12-31', 100.0, 100.0), ('2099-12-31', 100.0, 200.0)]
    path = _car_fdb(tmp_path, _asset(plan=far))

    result = run(db_with_coa, path)

    assert (result['imported'], result['future_depreciations_skipped'], result['depreciations_planned']) == (1, 1, 1)
    assert [row[3] for row in _plan(db_with_coa)] == ['booked', 'planned']


def test_the_check_does_not_require_the_amort_column(tmp_path):
    path = _fdb(tmp_path / 'a.fdb', [_record(4980, 1200, 1.0, 'X')], booking_columns=())
    with WisoDatabase(str(path)) as wiso:
        blocker, hints = wiso.check()
    assert blocker == [] and not any('INVENTORYAMORTID' in text for text in blocker + hints)


# ── Der Planstatus folgt der Buchung, nicht dem Datum ────────────────────────

AFTER_YEAR_END = '2027-01-15'                      # nach dem Jahreswechsel: das Jahr 2026 ist fällig


def _book_in_app(db, year, number='1'):
    """Die AfA eines Jahres in der App buchen (Aufwand 6222, Anlagekonto aus der Anlage)."""
    asset_id = query(db, 'SELECT ID FROM Assets WHERE InventoryNumber = ?', (number,))[0][0]
    return db.book_depreciation(asset_id, year, coa_id_expense=coa(db, 6222))


def _statuses(db):
    return [row[3] for row in _plan(db)]


def test_a_run_without_assets_after_the_year_change_marks_the_planned_year_booked(db_with_coa, tmp_path):
    """Lauf 1 mit Anlagen: 2026 steht als geplant. Lauf 2 nach dem Jahreswechsel ohne Anlagen bringt den Satz
    2026 – die Planzeile muss dann gebucht sein, sonst bucht die App das Jahr ein zweites Mal."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today=JUNE)
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']

    result = run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)

    assert (result['imported'], result['depreciations']) == (1, 0)        # der Satz kommt, kein Plan wird geschrieben
    assert _statuses(db_with_coa) == ['booked', 'booked', 'booked', 'planned', 'planned']
    with pytest.raises(ValueError, match='bereits gebucht'):             # keine Doppelbuchung möglich
        _book_in_app(db_with_coa, 2026)
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31', '2026-12-31']


def test_a_first_run_without_assets_skips_the_future_afa_and_writes_no_plan(db_with_coa, tmp_path):
    path = _car_fdb(tmp_path)

    first = run(db_with_coa, path, today=JUNE, with_assets=False)

    assert (first['imported'], first['future_depreciations_skipped']) == (2, 3)
    assert (first['depreciations'], first['depreciations_planned']) == (0, 0)   # kein Plan, also nichts „geplant“
    assert query(db_with_coa, 'SELECT COUNT(*) FROM AssetDepreciations') == [(0,)]
    second = run(db_with_coa, path, today=JUNE)                          # später mit den Anlagen
    assert (second['imported'], second['skipped'], second['depreciations_planned']) == (0, 2, 3)
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']


def test_a_satz_dated_before_its_plan_row_books_the_year(db_with_coa, tmp_path):
    """Die AfA des Abgangsjahres steht auf dem Abgangstag (30.06.), die Planzeile auf dem 31.12.: Am Stichtag
    13.11. ist der Satz fällig, die Planzeile nicht – der Status folgt der Buchung."""
    asset = _asset(sale='2026-06-30')
    records = _afa_records(asset)
    records[2]['ACCOUNTING_DATE'] = '2026-06-30 00:00:00'
    path = _fdb(tmp_path / 'a.fdb', records, assets=[asset])

    result = run(db_with_coa, path, today='2026-11-13')

    assert (result['imported'], result['depreciations_planned']) == (3, 0)
    assert _statuses(db_with_coa) == ['booked', 'booked', 'booked']
    with pytest.raises(ValueError, match='bereits gebucht'):
        _book_in_app(db_with_coa, 2026)
    assert len(_afa_bookings(db_with_coa)) == 3


def test_a_satz_dated_after_its_plan_row_leaves_the_year_to_the_app(db_with_coa, tmp_path):
    """Die Planzeile liegt vor dem Stichtag, der Satz danach: Er kommt nicht, also steht das Jahr als geplant im
    Plan (nicht als gebucht ohne Buchung) – die App kann es buchen."""
    plan = [(date if date[:4] != '2026' else '2026-06-30', amount, cumulated) for date, amount, cumulated in AFA_PLAN]
    asset = _asset(plan=plan)
    records = _afa_records(asset)
    records[2]['ACCOUNTING_DATE'] = '2026-12-31 00:00:00'
    path = _fdb(tmp_path / 'a.fdb', records, assets=[asset])

    result = run(db_with_coa, path, today='2026-11-13')

    assert (result['imported'], result['future_depreciations_skipped']) == (2, 3)
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']
    _book_in_app(db_with_coa, 2026)                                      # wird nicht abgelehnt
    assert len(_afa_bookings(db_with_coa)) == 3


def test_a_satz_booked_in_the_next_year_counts_for_the_year_of_its_plan_row(db_with_coa, tmp_path):
    records = _afa_records(CAR)
    records[1]['ACCOUNTING_DATE'] = '2026-01-02 00:00:00'               # die AfA 2025, im Januar 2026 gebucht
    path = _fdb(tmp_path / 'a.fdb', records, assets=[CAR])

    run(db_with_coa, path, today=JUNE)

    assert _statuses(db_with_coa)[:3] == ['booked', 'booked', 'planned']
    with pytest.raises(ValueError, match='bereits gebucht'):
        _book_in_app(db_with_coa, 2025)


def test_a_group_over_several_years_gets_the_missing_year_once(db_with_coa, tmp_path):
    """Eine Quellgruppe für den ganzen AfA-Plan: Der Erstlauf legt nur die vergangenen Jahre an. Nach dem
    Jahreswechsel fehlt 2026 in der bekannten Gruppe und wird genau einmal angelegt."""
    records = _afa_records(CAR)
    for record in records:
        record['ACCOUNTINGID'] = 999
    path = _fdb(tmp_path / 'a.fdb', records, assets=[CAR])
    first = run(db_with_coa, path, today=JUNE)
    assert first['imported'] == 2 and len(_afa_bookings(db_with_coa)) == 2

    second = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (second['imported'], second['skipped']) == (1, 2)
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31', '2026-12-31']
    assert _statuses(db_with_coa) == ['booked', 'booked', 'booked', 'planned', 'planned']
    third = run(db_with_coa, path, today=AFTER_YEAR_END)
    assert (third['imported'], third['skipped']) == (0, 3)
    assert len(_afa_bookings(db_with_coa)) == 3
    with pytest.raises(ValueError, match='bereits gebucht'):
        _book_in_app(db_with_coa, 2026)


def test_a_group_over_several_years_gets_no_missing_year_without_an_asset_in_the_app(db_with_coa, tmp_path):
    """Immer ohne Anlagenverzeichnis importiert: Es gibt weder eine Anlage noch eine Planzeile in der App. Fehlt der
    Satz in der bekannten Gruppe, bleibt es dabei, dass ein Satz einer bekannten Gruppe nie neu angelegt wird – der
    Nutzer kann ihn geändert oder gelöscht haben."""
    records = _afa_records(CAR)
    for record in records:
        record['ACCOUNTINGID'] = 999
    path = _fdb(tmp_path / 'a.fdb', records, assets=[CAR])
    run(db_with_coa, path, today=JUNE, with_assets=False)

    second = run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)
    third = run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)

    assert (second['imported'], second['skipped'], third['imported']) == (0, 3, 0)
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31']


def test_a_missing_afa_satz_of_a_booked_year_is_not_created_again(db_with_coa, tmp_path):
    """Das Planjahr steht als gebucht, sein Satz fehlt in der Gruppe (der Nutzer hat die Buchung gelöscht):
    Er kommt nicht wieder."""
    records = _afa_records(CAR)
    for record in records:
        record['ACCOUNTINGID'] = 999
    path = _fdb(tmp_path / 'a.fdb', records, assets=[CAR])
    run(db_with_coa, path, today='2099-01-01')                         # alles gebucht, eine Gruppe
    gone = [row for row in _afa_bookings(db_with_coa) if row[0] == '2026-12-31']
    booking_id = query(db_with_coa, 'SELECT ID FROM Bookings WHERE DateBooking = ? AND SourceGroup = ?',
                       ('2026-12-31', '999'))[0][0]
    db_with_coa.delete_transaction(booking_id)
    assert gone and len(_afa_bookings(db_with_coa)) == 4

    result = run(db_with_coa, path, today='2099-01-01')

    assert (result['imported'], result['skipped']) == (0, 5)           # vier wiedererkannt, einer bleibt weg
    assert len(_afa_bookings(db_with_coa)) == 4 and _statuses(db_with_coa) == ['booked'] * 5


def test_a_known_group_never_gets_another_kind_of_satz_back(db_with_coa, tmp_path):
    """Nur AfA-Sätze eines noch nicht gebuchten Jahres kommen nachträglich: Ein anderer Satz einer bekannten
    Gruppe wird nie neu angelegt."""
    records = [_record(4980, 1200, 10.0, 'Probe Teil A', ref='PR-G1', ACCOUNTINGID=555),
               _record(4980, 1200, 20.0, 'Probe Teil B', ref='PR-G2', ACCOUNTINGID=555)]
    first = _fdb(tmp_path / 'one.fdb', records[:1])
    both = _fdb(tmp_path / 'two.fdb', records)
    run(db_with_coa, first)

    result = run(db_with_coa, both)

    assert (result['imported'], result['skipped']) == (0, 2)


def test_without_the_amort_column_a_conflict_with_an_app_booking_is_still_found(db_with_coa, tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _afa_records(CAR, amort=False), assets=[CAR], booking_columns=('INVENTORYID',))
    run(db_with_coa, path, today=JUNE)
    _book_in_app(db_with_coa, 2026)

    result = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (result['depreciation_conflicts'], result['imported']) == (1, 0)
    assert result['depreciation_conflict_rows'] == [{'anlage': 'Dienstwagen', 'jahr': 2026}]
    assert len(_afa_bookings(db_with_coa)) == 3                          # 2024, 2025 und die der App


def test_without_the_amort_column_the_disposal_after_the_cutoff_is_imported(db_with_coa, tmp_path):
    other = _record(1360, 8400, 500.0, 'Abgang Dienstwagen', date='2027-01-15', INVENTORYID=CAR['id'])
    path = _fdb(tmp_path / 'a.fdb', _afa_records(CAR, amort=False) + [other], assets=[CAR],
                booking_columns=('INVENTORYID',))

    result = run(db_with_coa, path, today=JUNE)

    assert (result['imported'], result['future_depreciations_skipped']) == (3, 3)   # zwei AfA-Sätze und der Abgang


def test_an_asset_without_an_inventory_number_is_warned_about_and_its_afa_is_not_lost(db_with_coa, tmp_path):
    asset = _asset(number=None, id=9)
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset), assets=[asset])

    result = run(db_with_coa, path, today=JUNE)

    assert result['asset_warnings'] == [
        'Dienstwagen: hat keine Inventarnummer – die AfA lässt sich nicht mit dem Plan abgleichen']
    assert (result['imported'], result['depreciation_conflicts']) == (2, 0)
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']
    again = run(db_with_coa, path, today=JUNE, with_assets=False)           # die Sätze sind in ihrer Gruppe bekannt
    assert (again['imported'], again['skipped']) == (0, 2)


def test_assets_with_the_same_inventory_number_are_warned_about_and_their_afa_is_not_lost(db_with_coa, tmp_path):
    first = _asset(number=1, label='Erste', plan=[('2025-12-31', 1000.0, 1000.0), ('2026-12-31', 1000.0, 2000.0)])
    second = dict(_asset(number=2, label='Zweite', plan=[('2025-12-31', 700.0, 700.0), ('2026-12-31', 700.0, 1400.0)]),
                  number=1)
    path = _fdb(tmp_path / 'a.fdb', _afa_records(first) + _afa_records(second), assets=[first, second])

    result = run(db_with_coa, path, today='2025-06-30', with_assets=False)
    assert result['asset_warnings'] == [
        'Erste: Inventarnummer 1 mehrfach vergeben – die AfA lässt sich nicht mit dem Plan abgleichen',
        'Zweite: Inventarnummer 1 mehrfach vergeben – die AfA lässt sich nicht mit dem Plan abgleichen']
    assert (result['imported'], result['future_depreciations_skipped']) == (0, 4)
    later = run(db_with_coa, path, today=YEAR_END, with_assets=False)

    assert (later['imported'], later['depreciation_conflicts']) == (4, 0)   # nichts fällt aus
    assert later['asset_warnings'][:2] == result['asset_warnings']


def test_an_asset_without_an_inventory_number_gets_no_missing_year_in_a_known_group(db_with_coa, tmp_path):
    """Ohne Zuordnung gilt die Gruppenlogik ohne Anlagenbezug: Ein Satz einer bekannten Gruppe wird nie neu
    angelegt – die Warnung zur Inventarnummer sagt, dass sich die AfA nicht abgleichen lässt."""
    asset = _asset(number=None, id=9)
    records = _afa_records(asset)
    for record in records:
        record['ACCOUNTINGID'] = 999
    path = _fdb(tmp_path / 'a.fdb', records, assets=[asset])
    run(db_with_coa, path, today=JUNE, with_assets=False)

    result = run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)

    assert (result['imported'], result['skipped']) == (0, 3)
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31']
    assert result['asset_warnings'] == [
        'Dienstwagen: hat keine Inventarnummer – die AfA lässt sich nicht mit dem Plan abgleichen']


def test_an_afa_satz_adopted_from_the_csv_import_books_its_plan_year(db_with_coa, tmp_path):
    """Die Übernahme einer CSV-Zeile zählt wie ein neu angelegter Satz: Das Planjahr steht als gebucht fest."""
    csv = db_with_coa.import_wiso_csv(csv_export(
        ('6222', '520', '1500,00', '', '', '31.12.2025', 'Afa fuer Inventar Dienstwagen')))
    assert csv['imported'] == 1
    path = _car_fdb(tmp_path)

    result = run(db_with_coa, path, today=JUNE)

    assert (result['adopted'], result['imported']) == (1, 1)             # 2025 übernommen, 2024 neu
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']
    with pytest.raises(ValueError, match='bereits gebucht'):
        _book_in_app(db_with_coa, 2025)


def test_an_unknown_amort_id_falls_back_to_the_year_of_the_satz(tmp_path):
    records = _afa_records(CAR)
    records[0]['INVENTORYAMORTID'] = 99999                               # keine solche Zeile in der AfA-Tabelle
    path = _fdb(tmp_path / 'a.fdb', records, assets=[CAR])

    data = read_wiso_database(str(path), today=JUNE)

    assert [(b.date, b.is_depreciation, b.depreciation_year) for b in data.bookings
            if not b.future_depreciation] == [('2024-12-31', True, 2024), ('2025-12-31', True, 2025)]


def test_the_asset_step_books_the_plan_of_assets_the_booking_step_cannot_match(db_with_coa, tmp_path):
    """Ohne eindeutige Inventarnummer führt der Buchungsschritt den Planstatus nicht mit: Der Schritt der Anlagen
    hebt das geplante Jahr auf gebucht, sobald sein AfA-Satz in den Büchern steht."""
    first = _asset(number=1, label='Erste', plan=[('2025-12-31', 1000.0, 1000.0), ('2026-12-31', 1000.0, 2000.0)])
    second = dict(_asset(number=2, label='Zweite', plan=[('2025-12-31', 700.0, 700.0), ('2026-12-31', 700.0, 1400.0)]),
                  number=1)
    path = _fdb(tmp_path / 'a.fdb', _afa_records(first) + _afa_records(second), assets=[first, second])
    run(db_with_coa, path, today='2025-06-30')
    assert _statuses(db_with_coa) == ['planned', 'planned']

    run(db_with_coa, path, today=YEAR_END)

    assert _statuses(db_with_coa) == ['booked', 'booked']


def test_after_the_app_booking_is_deleted_the_next_import_books_the_year_from_wiso(db_with_coa, tmp_path):
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today=JUNE)
    booking_id = _book_in_app(db_with_coa, 2026)
    assert run(db_with_coa, path, today=AFTER_YEAR_END)['depreciation_conflicts'] == 1   # die App-Buchung gilt
    db_with_coa.delete_transaction(booking_id)
    assert _statuses(db_with_coa)[2] == 'planned'                        # das Löschen gibt das Jahr frei

    result = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (result['imported'], result['depreciation_conflicts']) == (1, 0)
    assert _plan(db_with_coa)[2][3:] == ('booked', None)
    assert query(db_with_coa, 'SELECT SourceGroup FROM Bookings WHERE DateBooking = ? AND COA_ID = ?',
                 ('2026-12-31', coa(db_with_coa, 6222))) == [('703',)]         # der Satz aus WISO, mit Quellgruppe
    with pytest.raises(ValueError, match='bereits gebucht'):
        _book_in_app(db_with_coa, 2026)


def test_an_error_in_the_asset_step_keeps_the_plan_status_in_line_with_the_bookings(db_with_coa, tmp_path, monkeypatch):
    """Die Status vorhandener Planzeilen liegen in der Transaktion der Buchungen: Scheitert danach der Schritt der
    Anlagen, steht der Satz in den Büchern und sein Planjahr als gebucht – nur der Rest des Plans fehlt."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today=JUNE)

    def broken(*args, **kwargs):
        raise RuntimeError('Festplatte voll')

    monkeypatch.setattr(type(db_with_coa), '_insert_wiso_assets', broken)
    with pytest.raises(RuntimeError, match='Festplatte voll'):
        db_with_coa.import_wiso_fdb(str(path), with_invoices=False, today=AFTER_YEAR_END)

    assert len(_afa_bookings(db_with_coa)) == 3                           # der Satz 2026 ist in den Büchern
    assert _statuses(db_with_coa) == ['booked', 'booked', 'booked', 'planned', 'planned']


def test_a_failure_while_writing_the_bookings_rolls_the_plan_status_back_too(db_with_coa, tmp_path, monkeypatch):
    """Statusänderung und Buchungen sind eine Transaktion: Scheitert ein Satz nach dem AfA-Satz, bleibt das Jahr
    geplant, und der AfA-Satz ist nicht angelegt."""
    run(db_with_coa, _car_fdb(tmp_path), today=JUNE)
    path = _car_fdb(tmp_path, records=[_record(4980, 1200, 5.0, 'Probe danach', ref='PR-Z1')], name='second.fdb')
    original = type(db_with_coa)._minor_opt
    calls = []

    def fail_on_the_second(value):
        calls.append(value)
        if len(calls) == 2:
            raise RuntimeError('Festplatte voll')
        return original(value)

    monkeypatch.setattr(type(db_with_coa), '_minor_opt', staticmethod(fail_on_the_second))
    with pytest.raises(RuntimeError, match='Festplatte voll'):
        db_with_coa.import_wiso_fdb(str(path), with_invoices=False, today=AFTER_YEAR_END)

    assert len(calls) == 2
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31']


# ── Nachtrag nur bei geplanter Planzeile der App ─────────────────────────────

def _one_group(records, group=999):
    """Alle Sätze in EINER Quellgruppe (so kann WISO einen Plan schreiben)."""
    for record in records:
        record['ACCOUNTINGID'] = group
    return records


def _drop_asset_in_app(db, number='1'):
    """Die Anlage samt Planzeilen aus der App entfernen – per SQL (``delete_asset`` lehnt gebuchte Jahre ab)."""
    asset_id = query(db, 'SELECT ID FROM Assets WHERE InventoryNumber = ?', (number,))[0][0]
    execute(db, 'DELETE FROM AssetDepreciations WHERE Asset_ID = ?', (asset_id,))
    execute(db, 'DELETE FROM Assets WHERE ID = ?', (asset_id,))


def _bookings(db):
    return query(db, 'SELECT * FROM Bookings ORDER BY ID')


@pytest.mark.parametrize('change', ['amount', 'other_year'])
def test_a_changed_afa_satz_is_not_added_a_second_time_by_a_run_without_assets(db_with_coa, tmp_path, change):
    """Ohne Anlage in der App gibt es keinen Grund, den Satz einer bekannten Gruppe neu anzulegen – auch nicht, wenn
    der Nutzer ihn so geändert hat, dass er keiner Zeile seiner Gruppe mehr genau gleicht: Ein geänderter Betrag zählt
    als vorhanden (die Gruppe hat eine Zeile im Planjahr), ein Datum in einem anderen Jahr nicht. Er bleibt, wie der
    Nutzer ihn gemacht hat, und es kommt in beiden Fällen kein zweiter dazu."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31', '2026-12-31']
    if change == 'amount':
        execute(db_with_coa, "UPDATE Bookings SET Amount = Amount + 10000 WHERE DateBooking = '2026-12-31'")
    else:
        execute(db_with_coa, "UPDATE Bookings SET DateBooking = '2027-01-05' WHERE DateBooking = '2026-12-31'")
    stored = _bookings(db_with_coa)

    result = run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)

    assert (result['imported'], result['skipped']) == (0, 3)
    assert _bookings(db_with_coa) == stored


@pytest.mark.parametrize('change', ['amount', 'day', 'accounts'])
def test_a_changed_afa_satz_counts_as_booked_and_is_not_added_again(db_with_coa, tmp_path, change):
    """Bestand ohne Anlagenverzeichnis; der Nutzer ändert einen vergangenen AfA-Satz (Betrag, Tag im selben Jahr oder
    Konten). Er gleicht keiner Zeile seiner Gruppe mehr genau, aber die Gruppe hat eine Zeile im Planjahr: Das Jahr steht
    in den Büchern. Der erste Lauf mit Anlagen legt keinen zweiten Satz an und schreibt das Planjahr als gebucht (die
    App kann es nicht noch einmal buchen); auch der nächste Lauf legt nichts an."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)
    if change == 'amount':
        execute(db_with_coa, "UPDATE Bookings SET Amount = Amount + 10000 WHERE DateBooking = '2025-12-31'")
    elif change == 'day':
        execute(db_with_coa, "UPDATE Bookings SET DateBooking = '2025-11-30' WHERE DateBooking = '2025-12-31'")
    else:
        execute(db_with_coa, "UPDATE Bookings SET COA_ID = ?, CounterCOA_ID = ? WHERE DateBooking = '2025-12-31'",
                (coa(db_with_coa, 6850), coa(db_with_coa, 6810)))
    stored = _bookings(db_with_coa)

    second = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (second['imported'], second['skipped'], second['assets']) == (0, 3, 1)
    assert _statuses(db_with_coa) == ['booked', 'booked', 'booked', 'planned', 'planned']
    with pytest.raises(ValueError, match='bereits gebucht'):
        _book_in_app(db_with_coa, 2025)
    third = run(db_with_coa, path, today=AFTER_YEAR_END)
    assert (third['imported'], third['skipped']) == (0, 3)
    assert _bookings(db_with_coa) == stored
    assert _statuses(db_with_coa) == ['booked', 'booked', 'booked', 'planned', 'planned']


def test_a_missing_afa_satz_is_not_added_after_the_asset_was_deleted_in_the_app(db_with_coa, tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _one_group(_afa_records(CAR)), assets=[CAR])
    run(db_with_coa, path, today=JUNE)                                  # Anlage und Plan in der App, 2026 geplant
    _drop_asset_in_app(db_with_coa)

    result = run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)

    assert (result['imported'], result['skipped']) == (0, 3)
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31']


def test_a_missing_afa_satz_is_not_added_without_a_plan_row(db_with_coa, tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _one_group(_afa_records(CAR)), assets=[CAR])
    run(db_with_coa, path, today=JUNE)
    execute(db_with_coa, 'DELETE FROM AssetDepreciations WHERE Year = 2026')      # die Anlage gibt es, die Planzeile nicht

    result = run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)

    assert (result['imported'], result['skipped']) == (0, 3)
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31']


def test_a_missing_afa_satz_is_added_by_a_run_without_assets_when_the_plan_row_is_planned(db_with_coa, tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _one_group(_afa_records(CAR)), assets=[CAR])
    run(db_with_coa, path, today=JUNE)
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']

    result = run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)

    assert (result['imported'], result['skipped']) == (1, 2)
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31', '2026-12-31']
    assert _statuses(db_with_coa) == ['booked', 'booked', 'booked', 'planned', 'planned']


#: Zwei AfA-Sätze desselben Planjahres 2025 (1.000 € und 500 €) in EINER Quellgruppe, dazu das Jahr 2026 in eigener
#: Gruppe: so schreibt WISO eine AfA samt Korrektur
TWO_IN_A_YEAR = _asset(plan=[('2025-12-31', 1500.0, 1500.0), ('2026-12-31', 1500.0, 3000.0)])
SIZES = {'big': 1000.0, 'small': 500.0}


def _two_satz_records(first):
    """Die Sätze von ``TWO_IN_A_YEAR``; ``first`` nennt den Satz des Jahres 2025, der in der Datei vorn steht."""
    names = [first] + [name for name in SIZES if name != first]
    records = [_record(4832, 320, SIZES[name], 'Afa fuer Inventar Dienstwagen', date='2025-12-31',
                       INVENTORYID=TWO_IN_A_YEAR['id'], INVENTORYAMORTID=_amort_id(TWO_IN_A_YEAR, 1),
                       ACCOUNTINGID=999) for name in names]
    return records + _afa_records(TWO_IN_A_YEAR)[1:]


@pytest.mark.parametrize('deleted', ['big', 'small'])
@pytest.mark.parametrize('first', ['big', 'small'])
def test_a_deleted_afa_satz_is_added_again_not_counted_as_present_by_the_row_of_the_other(
        db_with_coa, tmp_path, first, deleted):
    """Zwei AfA-Sätze desselben Jahres in einer Gruppe, der Nutzer löscht die Zeile des einen: Erst bekommt jeder Satz
    seine genaue Zeile, dann erst dienen übrige Zeilen als Nachweis. Der gelöschte Satz nimmt also nie die Zeile des
    anderen (auch nicht, wenn er in der Datei vorn steht), und beim Nachtrag (das Jahr steht als geplant) kommt genau
    er zurück – nicht der andere ein zweites Mal."""
    path = _fdb(tmp_path / 'a.fdb', _two_satz_records(first), assets=[TWO_IN_A_YEAR])
    run(db_with_coa, path, today='2099-01-01')                         # alle drei Sätze, alle Jahre gebucht
    execute(db_with_coa, "DELETE FROM Bookings WHERE SourceGroup = '999' AND Amount = ?",
            (int(SIZES[deleted] * 10000),))
    execute(db_with_coa, "UPDATE AssetDepreciations SET Status = 'planned' WHERE Year = 2025")   # das Jahr gilt wieder als geplant

    result = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (result['imported'], result['skipped']) == (1, 2)           # nur der gelöschte Satz kommt zurück
    assert sorted(row[0] for row in query(db_with_coa, "SELECT Amount FROM Bookings WHERE SourceGroup = '999'")) == [
        5000000, 10000000]


@pytest.mark.parametrize('deleted', ['big', 'small'])
@pytest.mark.parametrize('first', ['big', 'small'])
def test_a_row_of_the_group_serves_only_one_afa_satz_as_proof(db_with_coa, tmp_path, first, deleted):
    """Dasselbe mit künftigen Sätzen (Bestand aus einem Import bis 2.2.4): Die verbliebene Zeile des Jahres 2025 gehört
    ihrem Satz; der gelöschte zählt nicht als vorhanden."""
    path = _fdb(tmp_path / 'a.fdb', _two_satz_records(first), assets=[TWO_IN_A_YEAR])
    run(db_with_coa, path, today='2099-01-01')
    execute(db_with_coa, "DELETE FROM Bookings WHERE SourceGroup = '999' AND Amount = ?",
            (int(SIZES[deleted] * 10000),))

    result = run(db_with_coa, path, today='2025-01-31')                  # alle drei Sätze liegen in der Zukunft

    assert (result['imported'], result['future_depreciations_kept'], result['future_depreciations_skipped']) == (0, 2, 1)
    assert _statuses(db_with_coa) == ['booked', 'booked']


def test_one_changed_row_is_the_proof_for_one_afa_satz_only(db_with_coa, tmp_path):
    """Der Nutzer ändert den einen Satz des Jahres und löscht den anderen: Die eine verbliebene Zeile ist der Nachweis
    für einen Satz, nicht für beide (hier den ersten der Datei)."""
    path = _fdb(tmp_path / 'a.fdb', _two_satz_records('big'), assets=[TWO_IN_A_YEAR])
    run(db_with_coa, path, today='2099-01-01')
    execute(db_with_coa, "DELETE FROM Bookings WHERE SourceGroup = '999' AND Amount = ?", (int(SIZES['small'] * 10000),))
    execute(db_with_coa, "UPDATE Bookings SET Amount = Amount + 10000 WHERE SourceGroup = '999'")

    result = run(db_with_coa, path, today='2025-01-31')                  # alle drei Sätze liegen in der Zukunft

    assert (result['imported'], result['future_depreciations_kept'], result['future_depreciations_skipped']) == (0, 2, 1)


def test_a_satz_moved_to_another_year_is_not_recognized(db_with_coa, tmp_path):
    """Grenze: Der Nachweis ist eine freie Zeile der Gruppe im Planjahr. Hat der Nutzer das Datum eines Satzes in ein
    anderes Jahr gelegt, ist er nicht mehr zu erkennen: Der Import legt ihn (ohne Anlage in der App) nicht neu an, aber
    sein Planjahr zählt nicht als gebucht."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today=AFTER_YEAR_END, with_assets=False)
    execute(db_with_coa, "UPDATE Bookings SET DateBooking = '2025-06-30' WHERE DateBooking = '2026-12-31'")
    stored = _bookings(db_with_coa)

    result = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (result['imported'], result['skipped']) == (0, 3)
    assert _statuses(db_with_coa)[:3] == ['booked', 'booked', 'planned']
    assert _bookings(db_with_coa) == stored


def test_a_changed_future_afa_satz_of_a_2_2_4_stock_is_kept_too(db_with_coa, tmp_path):
    """Auch ein künftiger Satz des Bestands, den der Nutzer geändert hat, bleibt in den Büchern und gilt als gebucht:
    Eine Zeile der Gruppe im Planjahr genügt."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today='2099-01-01')                         # Bestand wie 2.2.4: alle fünf Sätze, alles gebucht
    execute(db_with_coa, "UPDATE Bookings SET Amount = Amount + 10000 WHERE DateBooking = '2027-12-31'")
    stored = _bookings(db_with_coa)
    plan_before = _plan(db_with_coa)

    result = run(db_with_coa, path, today=JUNE)

    assert (result['imported'], result['skipped']) == (0, 5)
    assert (result['future_depreciations_kept'], result['future_depreciations_skipped']) == (3, 0)
    assert _bookings(db_with_coa) == stored and _plan(db_with_coa) == plan_before


def test_a_changed_afa_satz_of_a_year_the_app_booked_is_still_a_conflict(db_with_coa, tmp_path):
    """Hat die App das Jahr selbst gebucht, bleibt die Planzeile, wie sie ist, und der Konfliktzweig gilt wie bisher; für
    einen künftigen Satz gibt es keinen Konflikt, der geänderte Satz gilt als vorhanden."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today='2099-01-01')
    booking_id = query(db_with_coa, "SELECT ID FROM Bookings WHERE DateBooking = '2026-12-31'")[0][0]
    execute(db_with_coa, "UPDATE Bookings SET Amount = Amount + 10000 WHERE ID = ?", (booking_id,))     # der Nutzer ändert ihn
    execute(db_with_coa, "UPDATE AssetDepreciations SET Status = 'posted', Booking_ID = ? WHERE Year = 2026",
            (booking_id,))                                                                              # die App hat das Jahr gebucht
    plan_before = _plan(db_with_coa)
    stored = _bookings(db_with_coa)

    due = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (due['depreciation_conflicts'], due['imported']) == (1, 0)
    assert _plan(db_with_coa) == plan_before and _bookings(db_with_coa) == stored
    future = run(db_with_coa, path, today=JUNE)
    assert (future['depreciation_conflicts'], future['future_depreciations_kept']) == (0, 3)
    assert _plan(db_with_coa) == plan_before and _bookings(db_with_coa) == stored


# ── Künftige AfA-Sätze: nie angelegt, im Bestand wiedererkannt ───────────────

def test_a_2_2_4_stock_without_plan_rows_gets_every_year_booked(db_with_coa, tmp_path):
    """Bestand aus einem Import bis 2.2.4 ohne Anlagenverzeichnis: alle fünf AfA-Sätze stehen in den Büchern, auch die
    künftigen, aber es gibt keinen Plan. Der erste Lauf mit Anlagen schreibt ihn: Jede Planzeile ist gebucht, nichts
    steht als geplant, und die App kann kein Jahr ein zweites Mal buchen."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today='2099-01-01', with_assets=False)
    assert query(db_with_coa, 'SELECT COUNT(*) FROM AssetDepreciations') == [(0,)]
    stored = _bookings(db_with_coa)
    assert len(_afa_bookings(db_with_coa)) == 5

    result = run(db_with_coa, path, today=JUNE)

    assert (result['imported'], result['skipped']) == (0, 5)
    assert (result['future_depreciations_kept'], result['future_depreciations_skipped']) == (3, 0)
    assert (result['depreciations'], result['depreciations_planned']) == (5, 0)
    assert _statuses(db_with_coa) == ['booked'] * 5
    for year in range(2024, 2029):
        with pytest.raises(ValueError, match='bereits gebucht'):
            _book_in_app(db_with_coa, year)
    assert _bookings(db_with_coa) == stored


def test_a_2_2_4_stock_stays_booked_when_the_asset_was_deleted_in_the_app(db_with_coa, tmp_path):
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today='2099-01-01')                         # wie 2.2.4: Sätze und Plan, alles gebucht
    _drop_asset_in_app(db_with_coa)
    stored = _bookings(db_with_coa)

    result = run(db_with_coa, path, today=JUNE)

    assert (result['imported'], result['future_depreciations_kept'], result['assets']) == (0, 3, 1)
    assert (result['depreciations'], result['depreciations_planned']) == (5, 0)
    assert _statuses(db_with_coa) == ['booked'] * 5                     # die neue Anlage der App trägt den Plan als gebucht
    with pytest.raises(ValueError, match='bereits gebucht'):
        _book_in_app(db_with_coa, 2026)
    assert _bookings(db_with_coa) == stored


def test_a_recognized_future_satz_leaves_a_year_booked_in_the_app_alone(db_with_coa, tmp_path):
    """Die Planzeile eines Jahres zeigt auf eine vorhandene Buchung der App: Auch ein wiedererkannter künftiger Satz
    ändert sie nicht und ist kein Konflikt."""
    path = _car_fdb(tmp_path)
    run(db_with_coa, path, today='2099-01-01')
    booking_id = query(db_with_coa, "SELECT ID FROM Bookings WHERE DateBooking = '2026-12-31'")[0][0]
    execute(db_with_coa, "UPDATE AssetDepreciations SET Status = 'posted', Booking_ID = ? WHERE Year = 2026",
            (booking_id,))
    before = _plan(db_with_coa)
    stored = _bookings(db_with_coa)

    result = run(db_with_coa, path, today=JUNE)

    assert (result['future_depreciations_kept'], result['depreciation_conflicts']) == (3, 0)
    assert _plan(db_with_coa) == before
    assert _bookings(db_with_coa) == stored


def test_a_future_afa_satz_creates_no_account_but_a_due_one_does(db_with_coa, tmp_path):
    """Die Konten, die nur ein künftiger AfA-Satz braucht, legt der Import nicht an – der Satz kommt nie in die Bücher."""
    chart = CHART + ((4833, 6812, 'Sonder-Abschreibung'),)
    records = _afa_records(CAR)
    records[4]['ACCOUNTNO'] = 4833                                      # die AfA 2028 auf einem Konto, das sonst keiner braucht
    path = _fdb(tmp_path / 'a.fdb', records, chart=chart, assets=[CAR])
    assert query(db_with_coa, 'SELECT COUNT(*) FROM ChartOfAccounts WHERE AccountNumber = 6812') == [(0,)]

    first = run(db_with_coa, path, today=JUNE)

    assert (first['imported'], first['future_depreciations_skipped']) == (2, 3)
    assert 6812 not in first['created_coa'] and first['missing_coa'] == {}
    assert query(db_with_coa, 'SELECT COUNT(*) FROM ChartOfAccounts WHERE AccountNumber = 6812') == [(0,)]

    later = run(db_with_coa, path, today='2099-01-01')                  # jetzt ist der Satz fällig

    assert later['imported'] == 3 and 6812 in later['created_coa']
    assert query(db_with_coa, 'SELECT COUNT(*) FROM ChartOfAccounts WHERE AccountNumber = 6812') == [(1,)]


def test_a_future_afa_satz_is_no_stock_range_case(db_with_coa):
    future = WisoBooking(account=1050, counter_account=520, future_depreciation=True, is_depreciation=True)
    due = WisoBooking(account=1050, counter_account=520, is_depreciation=True)

    assert db_with_coa._stock_range_accounts([future]) == []
    assert [row['nummer'] for row in db_with_coa._stock_range_accounts([future, due])] == [1050]


def test_a_future_afa_satz_never_adopts_a_csv_row_but_a_due_one_does(db_with_coa, tmp_path):
    csv = db_with_coa.import_wiso_csv(csv_export(
        ('6222', '520', '1500,00', '', '', '31.12.2027', 'Afa fuer Inventar Dienstwagen')))
    assert csv['imported'] == 1
    path = _car_fdb(tmp_path)

    first = run(db_with_coa, path, today=JUNE)                          # 2027 liegt noch in der Zukunft

    assert (first['adopted'], first['imported']) == (0, 2)
    assert query(db_with_coa, "SELECT SourceGroup FROM Bookings WHERE DateBooking = '2027-12-31'") == [(None,)]

    later = run(db_with_coa, path, today='2099-01-01')

    assert (later['adopted'], later['imported']) == (1, 2)               # 2027 übernommen, 2026 und 2028 neu
    assert query(db_with_coa, "SELECT SourceGroup FROM Bookings WHERE DateBooking = '2027-12-31'") == [('704',)]


def test_a_future_afa_satz_makes_no_other_satz_ambiguous(db_with_coa, tmp_path):
    """Zwei Sätze mit gleichem Schlüssel und verschiedenen Gegenkonten sind mehrdeutig und übernehmen keine CSV-Zeile
    ohne Gegenkonto. Ein künftiger AfA-Satz kommt nie in die Bücher und zählt dabei nicht mit."""
    csv = db_with_coa.import_wiso_csv(csv_export(
        ('6222', '', '1500,00', '', '', '31.12.2026', 'Afa fuer Inventar Dienstwagen')))
    assert csv['imported'] == 1
    same_key = _record(4832, 4980, 1500.0, 'Afa fuer Inventar Dienstwagen', date='2026-12-31')   # kein AfA-Satz
    path = _fdb(tmp_path / 'a.fdb', _afa_records(CAR) + [same_key], assets=[CAR])

    result = run(db_with_coa, path, today=JUNE)

    assert (result['adopted'], result['future_depreciations_skipped']) == (1, 3)


def test_a_future_afa_satz_shifts_no_counter_of_the_reader(tmp_path):
    """Der Leser liefert künftige AfA-Sätze gekennzeichnet, zählt für sie aber nichts: weder unbekannte Konten noch
    negative Bruttobeträge – und sie stehen nicht unter den übersprungenen Sätzen. Dieselbe Datei mit einem späteren
    Stichtag, an dem der Satz fällig ist, zählt ihn."""
    odd = _record(4999, 1360, -50.0, 'Afa Sonderfall', date='2030-12-31', INVENTORYID=CAR['id'],
                  INVENTORYAMORTID=_amort_id(CAR, 5))        # 4999 ist nicht im Kontenrahmen, 1360 ist liquide
    path = _car_fdb(tmp_path, records=[odd])

    future = read_wiso_database(str(path), today=JUNE)
    due = read_wiso_database(str(path), today='2099-01-01')

    assert (future.unmapped_accounts, future.negative_gross) == ({}, 0)
    assert (due.unmapped_accounts, due.negative_gross) == ({4999: 1}, 1)
    for data in (future, due):
        assert (data.tax_rows_skipped, data.memo_rows_skipped) == (0, 0)
        assert len(data.bookings) == 6
    assert sum(b.future_depreciation for b in future.bookings) == 4 and not any(b.future_depreciation for b in due.bookings)


# ── Der Planstatus wird in der schreibenden Transaktion gelesen ──────────────

def test_the_plan_is_read_inside_the_writing_transaction(db_with_coa, tmp_path, monkeypatch):
    """Zwischen dem Lesen der Planjahre und dem Schreiben der Buchungen darf keine andere Anfrage ein Jahr buchen: Die
    Transaktion beginnt als schreibende (``BEGIN IMMEDIATE``), und die Anlagen der App werden erst darin gelesen. Eine
    zweite Verbindung bekommt die Schreibsperre in diesem Moment nicht, danach schon."""
    original = type(db_with_coa)._link_assets
    seen = []

    def spy(self, assets, result, cursor):
        links = original(self, assets, result, cursor)
        other = sqlite3.connect(self.db_name, timeout=0.2)
        try:
            other.execute('BEGIN IMMEDIATE')
            seen.append('frei')
        except sqlite3.OperationalError as error:
            seen.append(str(error))
        finally:
            other.close()
        return links

    monkeypatch.setattr(type(db_with_coa), '_link_assets', spy)

    run(db_with_coa, _car_fdb(tmp_path), today=JUNE)

    assert seen == ['database is locked']
    other = sqlite3.connect(db_with_coa.db_name, timeout=0.2)
    try:
        other.execute('BEGIN IMMEDIATE')                                # nach dem Import ist die Sperre wieder frei
    finally:
        other.close()


# ── WISOs Buchungsart erkennt AfA-Sätze ──────────────────────────────────────

#: Spalten mit WISOs Buchungsart: ``INVENTORYAMORTID`` bleibt zuletzt (ein älteres Satzformat kennt sie noch nicht)
KIND_COLUMNS = ('INVENTORYID', 'ACCOUNTINGKIND', 'INVENTORYAMORTID')
DEPRECIATION_KIND, DISPOSAL_KIND = 28, 31


def _kind_records(asset, kind=DEPRECIATION_KIND):
    """AfA-Sätze mit WISOs Buchungsart und **leerer** ``INVENTORYAMORTID``."""
    records = _afa_records(asset, amort=False)
    for record in records:
        record['ACCOUNTINGKIND'] = kind
    return records


def test_the_accounting_kind_tells_an_afa_satz_even_with_an_empty_amort_id(tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _kind_records(CAR), assets=[CAR], booking_columns=KIND_COLUMNS)

    data = read_wiso_database(str(path), today=JUNE)

    assert [(b.date, b.is_depreciation, b.depreciation_year, b.future_depreciation) for b in data.bookings] == [
        ('2024-12-31', True, 2024, False), ('2025-12-31', True, 2025, False), ('2026-12-31', True, 2026, True),
        ('2027-12-31', True, 2027, True), ('2028-12-31', True, 2028, True)]


def test_the_accounting_kind_works_without_the_amort_column_too(tmp_path):
    path = _fdb(tmp_path / 'a.fdb', _kind_records(CAR), assets=[CAR], booking_columns=('INVENTORYID', 'ACCOUNTINGKIND'))

    data = read_wiso_database(str(path), today=JUNE)

    assert [b.is_depreciation for b in data.bookings] == [True] * 5


def test_a_disposal_on_the_day_of_an_afa_row_is_no_afa_satz_and_so_is_a_satz_without_an_asset(tmp_path):
    """Eine Abgangsbuchung (Art 31) am Anlagegut, am Tag und mit dem Betrag einer AfA-Zeile, mit leerer
    ``INVENTORYAMORTID`` (die Spalte gibt es): kein AfA-Satz. Art 28 ohne ``INVENTORYID`` ebenso nicht."""
    disposal = _record(1360, 8400, 1500.0, 'Abgang Dienstwagen', date='2026-12-31', INVENTORYID=CAR['id'],
                       ACCOUNTINGKIND=DISPOSAL_KIND)
    no_asset = _record(4832, 320, 1500.0, 'Ohne Anlage', date='2026-12-31', ACCOUNTINGKIND=DEPRECIATION_KIND)
    path = _fdb(tmp_path / 'a.fdb', _kind_records(CAR) + [disposal, no_asset], assets=[CAR], booking_columns=KIND_COLUMNS)

    data = read_wiso_database(str(path), today=JUNE)

    others = {b.text: b for b in data.bookings if b.text in ('Abgang Dienstwagen', 'Ohne Anlage')}
    assert [(text, b.is_depreciation, b.future_depreciation) for text, b in sorted(others.items())] == [
        ('Abgang Dienstwagen', False, False), ('Ohne Anlage', False, False)]


def test_a_record_of_an_older_format_with_another_kind_falls_back_to_the_afa_table(tmp_path):
    """Im älteren Satzformat gibt es die Buchungsart, aber noch keine ``INVENTORYAMORTID``: Ist die Art nicht 28,
    entscheidet ersatzweise die AfA-Tabelle (gleiche Anlage, gleiches Datum, gleicher Betrag); Art 28 gilt auch bei
    abweichendem Betrag."""
    older = _kind_records(CAR, kind=DISPOSAL_KIND)[:1]                           # AfA-Zeile 2024: Datum und Betrag stimmen
    other_amount = _record(4832, 320, 40.0, 'Anderer Betrag', date='2025-12-31', INVENTORYID=CAR['id'],
                           ACCOUNTINGKIND=DISPOSAL_KIND)
    by_kind = _record(4832, 320, 40.0, 'Nach Art', date='2025-12-31', INVENTORYID=CAR['id'],
                      ACCOUNTINGKIND=DEPRECIATION_KIND)
    path = _fdb(tmp_path / 'a.fdb', [], assets=[CAR], booking_columns=KIND_COLUMNS,
                old_records=older + [other_amount, by_kind])

    data = read_wiso_database(str(path), today='2099-01-01')

    assert sorted((b.text, b.is_depreciation) for b in data.bookings) == [
        ('Afa fuer Inventar Dienstwagen', True), ('Anderer Betrag', False), ('Nach Art', True)]


def test_the_check_does_not_require_the_accounting_kind_column(tmp_path):
    path = _fdb(tmp_path / 'a.fdb', [_record(4980, 1200, 1.0, 'X')], booking_columns=('INVENTORYID',))
    with WisoDatabase(str(path)) as wiso:
        blocker, hints = wiso.check()
    assert blocker == [] and not any('ACCOUNTINGKIND' in text for text in blocker + hints)


def test_an_afa_satz_known_by_its_kind_books_its_plan_year_and_conflicts_with_the_app(db_with_coa, tmp_path):
    """Ein vergangener Satz mit Art 28 und leerer ``INVENTORYAMORTID`` ist ein AfA-Satz: Sein Jahr gilt als gebucht,
    und eine Buchung der App für ein Jahr, dessen Satz kommt, ist ein Konflikt."""
    path = _fdb(tmp_path / 'a.fdb', _kind_records(CAR), assets=[CAR], booking_columns=KIND_COLUMNS)

    first = run(db_with_coa, path, today=JUNE)

    assert (first['imported'], first['future_depreciations_skipped']) == (2, 3)
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']
    with pytest.raises(ValueError, match='bereits gebucht'):
        _book_in_app(db_with_coa, 2025)
    _book_in_app(db_with_coa, 2026)                                      # die App bucht ihr Jahr 2026

    again = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (again['depreciation_conflicts'], again['imported']) == (1, 0)
    assert again['depreciation_conflict_rows'] == [{'anlage': 'Dienstwagen', 'jahr': 2026}]


def test_a_disposal_of_kind_31_is_imported_and_leaves_the_plan_years_alone(db_with_coa, tmp_path):
    disposal = _record(1360, 8400, 1500.0, 'Abgang Dienstwagen', date='2026-12-31', INVENTORYID=CAR['id'],
                       ACCOUNTINGKIND=DISPOSAL_KIND)
    path = _fdb(tmp_path / 'a.fdb', _kind_records(CAR) + [disposal], assets=[CAR], booking_columns=KIND_COLUMNS)

    result = run(db_with_coa, path, today=AFTER_YEAR_END)

    assert (result['imported'], result['future_depreciations_skipped']) == (4, 2)     # drei AfA-Sätze und der Abgang
    assert _statuses(db_with_coa) == ['booked', 'booked', 'booked', 'planned', 'planned']
    assert query(db_with_coa, "SELECT COUNT(*) FROM Bookings WHERE Text = 'Abgang Dienstwagen'") == [(1,)]


# ── Der Plan der App weicht von dem aus WISO ab ──────────────────────────────

DEGRESSIVE = [('2024-12-31', 1125.0, 1125.0), ('2025-12-31', 1218.75, 2343.75), ('2026-12-31', 914.06, 3257.81),
              ('2027-12-31', 1371.10, 4628.91), ('2028-12-31', 1371.09, 6000.0)]


def test_a_degressive_plan_from_wiso_is_reported_once_per_asset(db_with_coa, tmp_path):
    asset = _asset(plan=DEGRESSIVE)
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset), assets=[asset])

    result = run(db_with_coa, path, today=JUNE)

    assert result['asset_warnings'] == [
        'Dienstwagen: Die App plant die künftige AfA anders als WISO (2026: App 1.500,00 €, WISO 914,06 €). '
        'Beim Buchen in der App gilt der Plan der App – bitte prüfen.']
    assert result['depreciation_plan_mismatches'] == 1
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']   # gebucht wird nichts
    json.dumps(result)                                                    # die Ergebnisdatei bleibt schreibbar


def test_a_year_the_app_does_not_plan_at_all_is_reported_as_no_afa(db_with_coa, tmp_path):
    """Eine Anlage bis 800 € schreibt die App als GWG in einem Jahr ab; WISO verteilt sie."""
    small = [(f'{year}-12-31', 140.0, 140.0 * (year - 2023)) for year in range(2024, 2029)]
    asset = _asset(plan=small, price=700.0)
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset), assets=[asset])

    result = run(db_with_coa, path, today=JUNE)

    assert result['asset_warnings'] == [
        'Dienstwagen: Die App plant die künftige AfA anders als WISO (2026: App keine AfA, WISO 140,00 €). '
        'Beim Buchen in der App gilt der Plan der App – bitte prüfen.']
    assert result['depreciation_plan_mismatches'] == 1


def test_a_plan_the_app_plans_the_same_is_not_reported(db_with_coa, tmp_path):
    result = run(db_with_coa, _car_fdb(tmp_path), today=JUNE)

    assert result['depreciation_plan_mismatches'] == 0
    assert not any('plant die künftige AfA' in warning for warning in result['asset_warnings'])


@pytest.mark.parametrize('difference,reported', [(0.01, False), (0.02, True), (-0.01, False), (-0.02, True)])
def test_the_plans_may_differ_by_a_cent(db_with_coa, tmp_path, difference, reported):
    plan = [(date, round(amount + difference, 2) if date[:4] == '2027' else amount, cumulated)
            for date, amount, cumulated in AFA_PLAN]
    asset = _asset(plan=plan)
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset), assets=[asset])

    result = run(db_with_coa, path, today=JUNE)

    assert (result['depreciation_plan_mismatches'] == 1) is reported


def test_only_the_planned_years_are_compared_not_the_booked_ones(db_with_coa, tmp_path):
    """Das Jahr 2024 weicht ab, steht aber als gebucht im Plan: Das gebuchte Jahr steht in den Büchern, der
    Vergleich gilt dem, was die App noch bucht."""
    plan = [(date, 900.0 if date[:4] == '2024' else amount, cumulated) for date, amount, cumulated in AFA_PLAN]
    asset = _asset(plan=plan)
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset), assets=[asset])

    result = run(db_with_coa, path, today=JUNE)

    assert result['depreciation_plan_mismatches'] == 0


def test_a_year_booked_in_the_app_is_not_compared_either(db_with_coa, tmp_path):
    """Die App hat 2026 selbst gebucht: Ihr Wert gilt, die Planzeile bleibt – ohne Warnung für dieses Jahr."""
    plan = [(date, 1400.0 if date[:4] == '2026' else amount, cumulated) for date, amount, cumulated in AFA_PLAN]
    asset = _asset(plan=plan)
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset), assets=[asset])
    run(db_with_coa, path, today=JUNE)
    _book_in_app(db_with_coa, 2026)

    result = run(db_with_coa, path, today=JUNE)

    assert result['depreciation_plan_mismatches'] == 0 and result['depreciations_planned'] == 2


def test_the_page_masks_the_asset_name_in_the_warning(db_with_coa, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    asset = _asset(label='Wagen <script>alert(1)</script>', plan=DEGRESSIVE)
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset), assets=[asset])

    status, _location = handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [str(path)], 'with_assets': ['1']})
    html = PageMiscellaneous(db_with_coa)

    assert status == 303
    assert '<li>Wagen &lt;script&gt;alert(1)&lt;/script&gt;: Die App plant die künftige AfA anders als WISO' in html
    assert 'Wagen <script>alert(1)</script>' not in html
    assert 'nicht mit dem AfA-Plan der App abgleichen' in html          # die Einleitung nennt auch diese Warnungen


def test_the_warnings_about_unmatched_assets_are_masked_too(db_with_coa, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    asset = _asset(number=None, id=9, label='Wagen <b>fett</b>')
    path = _fdb(tmp_path / 'a.fdb', _afa_records(asset), assets=[asset])

    handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [str(path)]})
    html = PageMiscellaneous(db_with_coa)

    assert '<li>Wagen &lt;b&gt;fett&lt;/b&gt;: hat keine Inventarnummer' in html and '<b>fett</b>' not in html


# ═════════════════════════════════════════════════════════════════════════════
# Die Seite „Verschiedenes“
# ═════════════════════════════════════════════════════════════════════════════

NOTICE_SUBACCOUNTS = ('{} Buchungen stehen aus einem älteren Import noch auf dem Basiskonto statt auf dem '
                      'eigenen Unterkonto. Sie bleiben unverändert &ndash; für die getrennten Konten die '
                      'Daten in eine frische Datenbank importieren.')
NOTICE_FUTURE = ('{} vorausgeplante AfA-Sätze mit einem Datum nach dem Importtag wurden nicht als Buchung '
                 'übernommen.')
NOTICE_PLANNED = (' {} Jahre stehen als geplant im AfA-Plan der Anlagen; die AfA ist im jeweiligen Jahr in '
                  'der App zu buchen.')
NOTICE_KEPT = ('{} AfA-Buchungen mit einem Datum nach dem Importtag stammen aus einem früheren Import. Sie bleiben '
               'unverändert in den Büchern; ihre Jahre gelten als gebucht.')
NOTICE_CONFLICT = ('Für diese Jahre ist die AfA schon in der App gebucht; der AfA-Satz aus der '
                   'WISO-Datenbank wurde nicht übernommen.')
NOTICE_LIQUID = ('Diese Konten sind in WISO Unterkonten eines Bank-, Kassen- oder Verrechnungskontos, in der '
                 'App aber kein Zahlungskonto. Buchungen darauf werden als Umbuchung gelesen und erscheinen '
                 'nicht in der EÜR. Bitte das Konto vor dem Import als Bank- oder Kassenkonto anlegen und '
                 'neu importieren.')


def test_the_page_names_the_afa_that_was_not_booked_and_the_planned_years(db_with_coa, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    far = [('2020-12-31', 100.0, 100.0), ('2099-12-31', 100.0, 200.0)]       # der Stichtag ist hier „heute“
    path = _car_fdb(tmp_path, _asset(plan=far))

    status, location = handlers.handle_wiso_fdb_import(
        db_with_coa, {'fdb_path': [str(path)], 'with_assets': ['1']})

    assert status == 303 and 'wiso_fdb=ok' in location
    html = PageMiscellaneous(db_with_coa)
    assert '\u2139\ufe0f ' + NOTICE_FUTURE.format(1) + NOTICE_PLANNED.format(1) + '</p>' in html
    assert 'werden im jeweiligen Jahr gebucht' not in html              # es geschieht nicht von selbst


def test_the_page_says_nothing_about_planned_years_when_no_plan_was_written(db_with_coa, tmp_path):
    """Ohne Anlagen im Lauf steht kein Jahr als geplant im Plan: nur der erste Satz."""
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    far = [('2020-12-31', 100.0, 100.0), ('2099-12-31', 100.0, 200.0)]
    path = _car_fdb(tmp_path, _asset(plan=far))

    status, location = handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [str(path)]})

    assert status == 303 and 'wiso_fdb=ok' in location
    html = PageMiscellaneous(db_with_coa)
    assert '\u2139\ufe0f ' + NOTICE_FUTURE.format(1) + '</p>' in html
    assert 'Jahre stehen als geplant' not in html


def test_the_page_names_the_future_afa_that_stays_in_the_books(db_with_coa, tmp_path):
    """Bestand aus einem Import bis 2.2.4 mit einem künftigen AfA-Satz: Der Import lässt ihn, wie er ist, und sagt es –
    statt „nicht übernommen“."""
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    far = [('2020-12-31', 100.0, 100.0), ('2099-12-31', 100.0, 200.0)]       # der Stichtag ist hier „heute“
    path = _car_fdb(tmp_path, _asset(plan=far))
    run(db_with_coa, path, today='2100-01-01')                               # Bestand wie 2.2.4: beide Sätze, beide Jahre gebucht

    status, location = handlers.handle_wiso_fdb_import(
        db_with_coa, {'fdb_path': [str(path)], 'with_assets': ['1']})

    assert status == 303 and 'wiso_fdb=ok' in location
    html = PageMiscellaneous(db_with_coa)
    assert '\u2139\ufe0f ' + NOTICE_KEPT.format(1) + '</p>' in html
    assert 'vorausgeplante AfA-Sätze' not in html and 'Jahre stehen als geplant' not in html


def test_the_page_shows_each_notice_with_its_count(db_with_coa):
    _write_result({'subaccount_rows': 4, 'future_depreciations_skipped': 6, 'depreciations_planned': 5,
                   'future_depreciations_kept': 3, 'depreciation_conflicts': 2,
                   'depreciation_conflict_rows': [{'anlage': 'Dienstwagen', 'jahr': 2025},
                                                  {'anlage': 'Anhänger', 'jahr': 2026}]})

    html = PageMiscellaneous(db_with_coa)

    assert '\u2139\ufe0f ' + NOTICE_SUBACCOUNTS.format(4) in html
    assert '\u2139\ufe0f ' + NOTICE_FUTURE.format(6) + NOTICE_PLANNED.format(5) + '</p>' in html
    assert '\u2139\ufe0f ' + NOTICE_KEPT.format(3) + '</p>' in html
    assert '\u26a0\ufe0f AfA schon in der App gebucht &ndash; 2</h3>' in html
    assert NOTICE_CONFLICT in html
    assert ('<tr><th>Anlage</th><th>Jahr</th></tr><tr><td>Dienstwagen</td><td>2025</td></tr>'
            '<tr><td>Anhänger</td><td>2026</td></tr></table>') in html
    assert 'Genannt werden die ersten' not in html


def test_the_page_names_the_subaccounts_of_payment_accounts(db_with_coa):
    _write_result({'liquid_subaccounts': [{'nummer': 1801, 'basis': 1800, 'anzahl': 3},
                                          {'nummer': 1811, 'basis': 1810, 'anzahl': 1}]})

    html = PageMiscellaneous(db_with_coa)

    assert '\u26a0\ufe0f Unterkonten von Zahlungskonten &ndash; 2</h3>' in html
    assert NOTICE_LIQUID in html
    assert ('<tr><th>Konto</th><th>Unterkonto von</th><th>Buchungen</th></tr>'
            '<tr><td>1801</td><td>1800</td><td>3</td></tr>'
            '<tr><td>1811</td><td>1810</td><td>1</td></tr></table>') in html
    assert 'Genannt werden die ersten' not in html


def test_the_page_masks_and_survives_broken_entries_of_the_payment_subaccounts(db_with_coa):
    hostile = '<img src=x onerror=alert(1)>'
    _write_result({'liquid_subaccounts': [1, None, 'x', {'nummer': hostile, 'basis': hostile, 'anzahl': hostile},
                                          {}]})

    html = PageMiscellaneous(db_with_coa)

    escaped = '&lt;img src=x onerror=alert(1)&gt;'
    assert f'<tr><td>{escaped}</td><td>{escaped}</td><td>{escaped}</td></tr>' in html
    assert '<tr><td></td><td></td><td></td></tr>' in html
    assert hostile not in html and '\u26a0\ufe0f Unterkonten von Zahlungskonten &ndash; 2</h3>' in html
    for broken in ('x', 5, {'a': 1}, [1, 'x'], None):
        _write_result({'liquid_subaccounts': broken})
        assert 'Unterkonten von Zahlungskonten' not in PageMiscellaneous(db_with_coa)


def test_the_list_of_payment_subaccounts_says_when_it_is_cut_off():
    html = _import_notices({'liquid_subaccounts': [{'nummer': 1800 + n, 'basis': 1800, 'anzahl': 1}
                                                   for n in range(1, 51)]})
    assert '&ndash; 50</h3>' in html and html.count('<tr><td>') == 50
    assert 'Genannt werden die ersten 50.' in html


def test_the_page_shows_nothing_without_a_reason(db_with_coa):
    _write_result({'subaccount_rows': 0, 'future_depreciations_skipped': 0, 'depreciations_planned': 4,
                   'future_depreciations_kept': 0,
                   'depreciation_conflicts': 0, 'depreciation_conflict_rows': [], 'liquid_subaccounts': [],
                   'imported': 1})

    html = PageMiscellaneous(db_with_coa)

    assert 'Basiskonto statt auf dem eigenen Unterkonto' not in html
    assert 'vorausgeplante AfA-Sätze' not in html and 'AfA schon in der App gebucht' not in html
    assert 'stammen aus einem früheren Import' not in html
    assert 'Jahre stehen als geplant' not in html and 'Unterkonten von Zahlungskonten' not in html


def test_the_conflict_table_says_when_it_is_cut_off():
    html = _import_notices({'depreciation_conflicts': 250, 'depreciation_conflict_rows': [
        {'anlage': f'A{n}', 'jahr': 2025} for n in range(100)]})
    assert '&ndash; 250</h3>' in html and html.count('<tr><td>') == 100
    assert 'Genannt werden die ersten 100.' in html


@pytest.mark.parametrize('result', [
    {'subaccount_rows': 'viele', 'future_depreciations_skipped': True, 'depreciation_conflicts': [],
     'depreciation_conflict_rows': {'a': 1}, 'future_depreciations_kept': 'viele'},
    {'subaccount_rows': -3, 'future_depreciations_skipped': 1.5, 'depreciation_conflicts': None,
     'depreciation_conflict_rows': 'x', 'future_depreciations_kept': -3},
    {'subaccount_rows': True, 'depreciation_conflicts': 'x', 'depreciation_conflict_rows': [1, None, 'x', [2]],
     'future_depreciations_kept': True},
    {'subaccount_rows': {}, 'future_depreciations_skipped': [3], 'depreciation_conflicts': False,
     'future_depreciations_kept': [2]}],
    ids=['Text, Wahrheitswert, Liste, Objekt', 'negativ, Kommazahl, null, Text', 'Wahrheitswert, Liste ohne Objekte',
         'Objekt, Liste, Wahrheitswert'])
def test_the_page_survives_a_hand_edited_result_file(db_with_coa, result):
    _write_result(result)

    html = PageMiscellaneous(db_with_coa)

    assert 'Basiskonto statt auf dem eigenen Unterkonto' not in html
    assert 'vorausgeplante AfA-Sätze' not in html and 'AfA schon in der App gebucht' not in html
    assert 'stammen aus einem früheren Import' not in html


@pytest.mark.parametrize('planned', ['viele', True, -2, 1.5, None, [3], {'a': 1}])
def test_a_broken_count_of_planned_years_drops_only_the_second_sentence(db_with_coa, planned):
    _write_result({'future_depreciations_skipped': 2, 'depreciations_planned': planned})

    html = PageMiscellaneous(db_with_coa)

    assert '\u2139\ufe0f ' + NOTICE_FUTURE.format(2) + '</p>' in html
    assert 'Jahre stehen als geplant' not in html


def test_broken_entries_in_the_conflict_list_are_skipped_and_valid_ones_masked():
    hostile = '<img src=x onerror=alert(1)>'
    html = _import_notices({'depreciation_conflicts': 3, 'depreciation_conflict_rows': [
        1, None, 'text', {'anlage': hostile, 'jahr': hostile}, {'anlage': None, 'jahr': None}, {}]})
    escaped = '&lt;img src=x onerror=alert(1)&gt;'
    assert f'<tr><td>{escaped}</td><td>{escaped}</td></tr>' in html
    assert hostile not in html and '<img' not in html
    assert html.count('<tr><td>') == 3                                  # drei Objekte, drei Zeilen
