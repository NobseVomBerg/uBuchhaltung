# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Import aus der WISO-Datenbank in die Bookings-/Assets-Tabellen.

Der fachliche Kern: WISO liefert mit ``ACCOUNTINGID`` die Klammer eines
Vorgangs mit. Sie landet in ``Bookings.SourceGroup`` und entscheidet im
Auto-Abgleich, welche Buchungen zu einer Bankbewegung gehören – auch dann,
wenn am selben Tag mehrere Vorgänge über denselben Betrag laufen.

Alle Daten sind synthetisch (siehe :mod:`tests.fdb_builder`).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fdb_builder import (FdbBuilder, INT64, LONG, SHORT, TEXT,  # noqa: E402
                         TIMESTAMP, Column)


def _chart(builder):
    chart = builder.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4),
        Column('SKR04', LONG, 4),
        Column('BOOKINGYEAR', SHORT, 2),
        Column('ACCOUNTTEXT', TEXT, 40),
    ])
    for skr03, skr04, text in ((1210, 1810, 'Bankkonto'),
                               (1360, 1460, 'Verrechnungskonto'),
                               (8400, 4400, 'Erlöse 19 %'),
                               (8405, 4405, 'Erlöse 19 % noch offen'),
                               (8801, 6885, 'Erlöse Anlagenverkauf'),
                               (4980, 6850, 'Betriebsbedarf'),
                               (320, 520, 'Pkw'),
                               (4832, 6222, 'Abschreibungen auf Kfz')):
        chart.add(ID=skr03, SKR04=skr04, BOOKINGYEAR=2024, ACCOUNTTEXT=text)
    return chart


def _bookings_table(builder):
    return builder.table('MOV_FINACC_ACCRECORDS', [
        Column('ID', LONG, 4),
        Column('ACCOUNTINGID', LONG, 4),
        Column('ACCOUNTING_DATE', TIMESTAMP, 8),
        Column('AMOUNTNET', INT64, 8, scale=-2),
        Column('AMOUNTGROSS', INT64, 8, scale=-2),
        Column('TAXRATE', INT64, 8, scale=-2),
        Column('ACCOUNTNO', LONG, 4),
        Column('CONTRA_ACCOUNTNO', LONG, 4),
        Column('ACCOUNTING_TEXT', TEXT, 60),
        Column('REFERENCENO', TEXT, 20),
    ])


@pytest.fixture
def anlagenverkauf_fdb(tmp_path):
    """Der Fall, an dem der alte Abgleich scheiterte.

    Am selben Tag laufen zwei Vorgänge über denselben Betrag: die Zahlung der
    Rechnung (über das Bankkonto) und die Umbuchung des verkauften Anlageguts
    (über das Verrechnungskonto). Nur die ACCOUNTINGID trennt sie.
    """
    builder = FdbBuilder(page_size=16384)
    _chart(builder)
    bookings = _bookings_table(builder)
    bookings.add(ID=1, ACCOUNTINGID=500, ACCOUNTING_DATE='2024-04-30 00:00:00',
                 AMOUNTGROSS=7500.0, ACCOUNTNO=1210, CONTRA_ACCOUNTNO=10000,
                 ACCOUNTING_TEXT='Zahlung zu Re. 2024004', REFERENCENO='2024004')
    bookings.add(ID=2, ACCOUNTINGID=500, ACCOUNTING_DATE='2024-04-30 00:00:00',
                 AMOUNTNET=6302.52, AMOUNTGROSS=7500.0, TAXRATE=19.0,
                 ACCOUNTNO=8405, CONTRA_ACCOUNTNO=8400,
                 ACCOUNTING_TEXT='Umb. zu Re. 2024004', REFERENCENO='2024004')
    bookings.add(ID=3, ACCOUNTINGID=501, ACCOUNTING_DATE='2024-04-30 00:00:00',
                 AMOUNTGROSS=7500.0, ACCOUNTNO=1360, CONTRA_ACCOUNTNO=8801,
                 ACCOUNTING_TEXT='Umbuchung Anlagegut')
    return builder.write(str(tmp_path / 'db1.fdb'))


# ----------------------------------------------------------------------
def test_import_schreibt_die_klammer_mit(db_with_coa, anlagenverkauf_fdb):
    result = db_with_coa.import_wiso_fdb(anlagenverkauf_fdb)
    assert result['imported'] == 3
    assert not result['errors']

    conn = db_with_coa._get_connection()
    rows = conn.execute(
        'SELECT SourceGroup, COUNT(*) FROM Bookings'
        ' WHERE SourceGroup IS NOT NULL GROUP BY SourceGroup'
        ' ORDER BY SourceGroup').fetchall()
    conn.close()
    assert rows == [('500', 2), ('501', 1)]


def test_konten_und_betraege_kommen_richtig_an(db_with_coa, anlagenverkauf_fdb):
    db_with_coa.import_wiso_fdb(anlagenverkauf_fdb)
    conn = db_with_coa._get_connection()
    row = conn.execute('''
        SELECT c.AccountNumber, g.AccountNumber, b.Amount, b.TaxRate, b.Text
        FROM Bookings b
        LEFT JOIN ChartOfAccounts c ON c.ID = b.COA_ID
        LEFT JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID
        WHERE b.Text LIKE 'Umb.%'
    ''').fetchone()
    conn.close()
    assert row[0] == 4405 and row[1] == 4400          # SKR03 8405/8400
    assert row[3] == pytest.approx(0.19)


def test_zweiter_lauf_verdoppelt_nichts(db_with_coa, anlagenverkauf_fdb):
    """Die Quellgruppe ist der Wiedererkennungsschlüssel."""
    db_with_coa.import_wiso_fdb(anlagenverkauf_fdb)
    second = db_with_coa.import_wiso_fdb(anlagenverkauf_fdb)
    assert second['imported'] == 0
    assert second['skipped'] == 3

    conn = db_with_coa._get_connection()
    total = conn.execute('SELECT COUNT(*) FROM Bookings'
                         ' WHERE SourceGroup IS NOT NULL').fetchone()[0]
    conn.close()
    assert total == 3


def test_abgleich_waehlt_die_gruppe_des_eigenen_bankkontos(db_with_coa,
                                                           anlagenverkauf_fdb):
    """Beide Gruppen summieren auf 7.500 € am selben Tag.

    Verknüpft werden darf nur die, die über das SKR-Konto **dieser** Bank
    läuft – sonst hängt der Anlagenabgang an der Zahlung.
    """
    db_with_coa.import_wiso_fdb(anlagenverkauf_fdb)
    account_id = next(a[0] for a in db_with_coa.fetch_accounts()
                      if a[1] == 'Testkonto 2')        # SKR 1810
    bank_id = db_with_coa.insert_booking(
        '2024-04-30', 7500.0, account_id=account_id,
        recipient_client='Kundin', text='Ueberweisung', booking_type='bank')

    db_with_coa.link_bank_to_entries()

    conn = db_with_coa._get_connection()
    linked = conn.execute(
        'SELECT SourceGroup FROM Bookings WHERE ParentBooking_ID = ?'
        ' ORDER BY ID', (bank_id,)).fetchall()
    conn.close()
    assert [r[0] for r in linked] == ['500', '500']    # der ganze Vorgang
    assert '501' not in [r[0] for r in linked]         # der Abgang bleibt frei


def test_uebersicht_klammert_nach_der_quellgruppe(db_with_coa,
                                                  anlagenverkauf_fdb):
    """Ohne Bankbewegung hält die SourceGroup die Teilbuchungen zusammen."""
    db_with_coa.import_wiso_fdb(anlagenverkauf_fdb)
    rows = db_with_coa.fetch_bookings_grouped()      # flache Liste

    def source_groups(entry):
        return {child['booking'][21] for child in entry.get('children', [])}

    groups = [r for r in rows if r.get('type') == 'group']
    ours = [g for g in groups if source_groups(g) == {'500'}]
    assert len(ours) == 1
    assert ours[0]['count'] == 2
    # Die einzelne Umbuchung bleibt eine eigenständige Zeile.
    assert not [g for g in groups if source_groups(g) == {'501'}]


# ----------------------------------------------------------------------
def test_anlagen_landen_im_verzeichnis(db_with_coa, tmp_path):
    builder = FdbBuilder(page_size=16384)
    _chart(builder)
    _bookings_table(builder)
    builder.table('BAS_INVENTORY', [
        Column('ID', LONG, 4), Column('INVNO', LONG, 4),
        Column('LABEL', TEXT, 40), Column('PURCHASEDATE', TIMESTAMP, 8),
        Column('PURCHASEAMOUNTNET', INT64, 8, scale=-2),
        Column('SALEDATE', TIMESTAMP, 8), Column('SERVICELIFE', SHORT, 2),
        Column('FINACIALACCOUNT', LONG, 4),
        Column('FINACIALACCOUNT_AFA', LONG, 4),
    ]).add(ID=5, INVNO=1, LABEL='Dienstwagen',
           PURCHASEDATE='2022-06-07 00:00:00', PURCHASEAMOUNTNET=0.01,
           SERVICELIFE=3, FINACIALACCOUNT=320, FINACIALACCOUNT_AFA=4832)
    builder.table('MOV_INVENTORY_BOOKINGS', [
        Column('ID', LONG, 4), Column('INVENTORYID', LONG, 4),
        Column('BOOKINGDATE', TIMESTAMP, 8),
        Column('AMOUNTNET', INT64, 8, scale=-2),
        Column('DESCRIPTION', TEXT, 40),
    ]).add(ID=1, INVENTORYID=5, BOOKINGDATE='2022-06-07 00:00:00',
           AMOUNTNET=5232.0, DESCRIPTION='Kauf')
    builder.table('MOV_INVENTORY_AMORTIZATIONS', [
        Column('ID', LONG, 4), Column('INVENTORYID', LONG, 4),
        Column('BOOKINGDATE', TIMESTAMP, 8),
        Column('AMORT_AMOUNT', INT64, 8, scale=-2),
        Column('AMORT_CUMULATED_AMOUNT', INT64, 8, scale=-2),
        Column('RESIDUALVALUE_AMOUNT', INT64, 8, scale=-2),
    ]).add(ID=1, INVENTORYID=5, BOOKINGDATE='2022-12-31 00:00:00',
           AMORT_AMOUNT=2418.27, AMORT_CUMULATED_AMOUNT=2418.27)
    path = builder.write(str(tmp_path / 'db1.fdb'))

    result = db_with_coa.import_wiso_fdb(path)
    assert result['assets'] == 1 and result['depreciations'] == 1

    conn = db_with_coa._get_connection()
    asset = conn.execute(
        'SELECT ID, InventoryNumber, Name, PurchasePrice, UsefulLifeYears,'
        ' Status FROM Assets WHERE Name = ?', ('Dienstwagen',)).fetchone()
    afa = conn.execute(
        'SELECT Year, DepreciationAmount, BookValue FROM AssetDepreciations'
        ' WHERE Asset_ID = ?', (asset[0],)).fetchone()
    conn.close()
    asset = asset[1:]
    assert asset[0] == '1' and asset[1] == 'Dienstwagen'
    assert asset[2] == 52320000                       # 5232,00 € in Minor Units
    assert asset[3] == 3 and asset[4] == 'active'
    assert afa[0] == 2022 and afa[2] == 28137300      # 5232,00 − 2418,27


def test_defekte_datei_meldet_fehler_statt_abzustuerzen(db_with_coa, tmp_path):
    path = tmp_path / 'kaputt.fdb'
    path.write_bytes(b'\x00' * 2048)
    result = db_with_coa.import_wiso_fdb(str(path))
    assert result['imported'] == 0
    assert result['errors']
