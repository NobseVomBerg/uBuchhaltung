# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Tests für den Import aus der WISO-Datenbank (Firebird ODS 12).

Alle Prüfungen laufen gegen synthetische Dateien aus
:mod:`tests.fdb_builder` – echte Buchungsdaten kommen hier nicht vor.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fdb_builder import (BLOB, FdbBuilder, INT64, LONG, SHORT, TEXT,  # noqa: E402
                         TIMESTAMP, Column)
from importers.wiso_fdb import WisoDatabase, read_wiso_database  # noqa: E402
from importers.wiso_fdb.catalog import Catalog  # noqa: E402
from importers.wiso_fdb.ods import OdsError, OdsFile  # noqa: E402


# ----------------------------------------------------------------------
# Rohformat
# ----------------------------------------------------------------------
def test_rle_literal_und_wiederholung():
    """Steuerbyte < 128 kopiert, >= 128 wiederholt, 0 beendet."""
    assert OdsFile.decompress(bytes([3, 65, 66, 67, 0])) == b'ABC'
    assert OdsFile.decompress(bytes([253, 88])) == b'XXX'      # 256-253 = 3
    assert OdsFile.decompress(bytes([2, 65, 66, 254, 90, 0])) == b'ABZZ'
    assert OdsFile.decompress(b'') == b''


def test_rle_bricht_bei_abgeschnittenen_daten_nicht_ab():
    """Ein angeschnittener Lauf darf keine Ausnahme werfen."""
    assert OdsFile.decompress(bytes([10, 65, 66])) == b'AB'
    assert OdsFile.decompress(bytes([200])) == b''


def test_keine_firebird_datei(tmp_path):
    path = tmp_path / 'kaputt.fdb'
    path.write_bytes(b'\x00' * 4096)
    with pytest.raises(OdsError):
        OdsFile(str(path))


def test_verschluesselte_datei_wird_abgelehnt(tmp_path):
    """Das Crypt-Bit im Header führt zu einer klaren Fehlermeldung."""
    builder = FdbBuilder()
    builder.table('T', [Column('ID', LONG, 4)]).add(ID=1)
    raw = bytearray(builder.build())
    raw[42] = 0x12 | 0x40                       # hdr_encrypted
    path = tmp_path / 'crypt.fdb'
    path.write_bytes(bytes(raw))
    with pytest.raises(OdsError, match='verschlüsselt'):
        OdsFile(str(path))


# ----------------------------------------------------------------------
# Katalog und Werte
# ----------------------------------------------------------------------
@pytest.fixture
def demo_db(tmp_path):
    builder = FdbBuilder()
    table = builder.table('DEMO', [
        Column('ID', LONG, 4),
        Column('LABEL', TEXT, 10),
        Column('AMOUNT', INT64, 8, scale=-2),
        Column('STAMP', TIMESTAMP, 8),
        Column('NOTE', BLOB, 8, sub_type=1),
    ])
    table.add(ID=1, LABEL='Kasse', AMOUNT=1234.56,
              STAMP='2024-03-01 09:30:00', NOTE='eine Bemerkung')
    table.add(ID=2, LABEL='Bank', AMOUNT=-9.99,
              STAMP='2025-12-31 23:59:59', NOTE=None)
    return builder.write(str(tmp_path / 'demo.fdb'))


def test_katalog_kennt_tabelle_und_spalten(demo_db):
    with Catalog(demo_db) as catalog:
        assert catalog.table_names() == ['DEMO']
        relation = catalog.relation_id['DEMO']
        names = [c.name for c in catalog.columns(relation, 0)]
        assert names == ['ID', 'LABEL', 'AMOUNT', 'STAMP', 'NOTE']


def test_werte_kommen_typisiert_zurueck(demo_db):
    with Catalog(demo_db) as catalog:
        rows = list(catalog.rows('DEMO'))
    assert rows[0]['ID'] == 1
    assert rows[0]['LABEL'] == 'Kasse'
    assert rows[0]['AMOUNT'] == pytest.approx(1234.56)
    assert rows[0]['STAMP'] == '2024-03-01 09:30:00'
    assert rows[1]['AMOUNT'] == pytest.approx(-9.99)
    assert rows[1]['STAMP'] == '2025-12-31 23:59:59'


def test_nullwerte_bleiben_none(demo_db):
    with Catalog(demo_db) as catalog:
        rows = list(catalog.rows('DEMO'))
    assert rows[1]['NOTE'] is None


def test_textblob_wird_aufgeloest(demo_db):
    with Catalog(demo_db) as catalog:
        rows = list(catalog.rows('DEMO', blobs=True))
    assert rows[0]['NOTE'] == 'eine Bemerkung'


def test_umlaute_kommen_als_latin1_zurueck(tmp_path):
    builder = FdbBuilder()
    builder.table('T', [Column('T', TEXT, 20)]).add(T='Anhänger')
    path = builder.write(str(tmp_path / 'u.fdb'))
    with Catalog(path) as catalog:
        assert list(catalog.rows('T'))[0]['T'] == 'Anhänger'


def test_grosse_saetze_ueber_mehrere_seiten(tmp_path):
    """Mehr Sätze, als auf eine Seite passen, brauchen mehrere Datenseiten."""
    builder = FdbBuilder(page_size=16384)
    table = builder.table('VIELE', [Column('ID', LONG, 4),
                                    Column('LABEL', TEXT, 60)])
    for number in range(200):
        table.add(ID=number, LABEL=f'Zeile {number}')
    path = builder.write(str(tmp_path / 'viele.fdb'))
    with Catalog(path) as catalog:
        rows = list(catalog.rows('VIELE'))
    assert len(rows) == 200
    assert {r['ID'] for r in rows} == set(range(200))


# ----------------------------------------------------------------------
# Fachschicht: WISO-Tabellen
# ----------------------------------------------------------------------
def _wiso_builder():
    """Minimale WISO-Datenbank: Kontenrahmen und Buchungssätze."""
    builder = FdbBuilder(page_size=16384)
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
                               (1776, 3806, 'Umsatzsteuer 19 %'),
                               (4980, 6850, 'Betriebsbedarf'),
                               (320, 520, 'Pkw'),
                               (4832, 6222, 'Abschreibungen auf Kfz')):
        chart.add(ID=skr03, SKR04=skr04, BOOKINGYEAR=2024, ACCOUNTTEXT=text)

    bookings = builder.table('MOV_FINACC_ACCRECORDS', [
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
        Column('INVID', LONG, 4),
        Column('INVENTORYID', LONG, 4),
    ])
    return builder, bookings


def test_steuerzeilen_werden_uebersprungen(tmp_path):
    """Sätze ohne AMOUNTGROSS sind WISOs Steuerzeilen und gehören nicht rein."""
    builder, bookings = _wiso_builder()
    bookings.add(ID=1, ACCOUNTINGID=100, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTNET=100.0, AMOUNTGROSS=119.0, TAXRATE=19.0,
                 ACCOUNTNO=4980, CONTRA_ACCOUNTNO=1210,
                 ACCOUNTING_TEXT='Büromaterial', REFERENCENO='B1')
    bookings.add(ID=2, ACCOUNTINGID=100, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTNET=19.0, AMOUNTGROSS=None, TAXRATE=19.0,
                 ACCOUNTNO=1776, CONTRA_ACCOUNTNO=1210,
                 ACCOUNTING_TEXT='Steuer', REFERENCENO='B1')
    path = builder.write(str(tmp_path / 'w.fdb'))

    data = read_wiso_database(path, liquid_accounts={1810})
    assert len(data.bookings) == 1
    assert data.tax_rows_skipped == 1
    assert data.bookings[0].text == 'Büromaterial'


def test_konten_werden_auf_skr04_umgeschluesselt(tmp_path):
    builder, bookings = _wiso_builder()
    bookings.add(ID=1, ACCOUNTINGID=100, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTNET=100.0, AMOUNTGROSS=119.0, TAXRATE=19.0,
                 ACCOUNTNO=4980, CONTRA_ACCOUNTNO=1210,
                 ACCOUNTING_TEXT='Büromaterial', REFERENCENO='B1')
    path = builder.write(str(tmp_path / 'w.fdb'))

    booking = read_wiso_database(path, liquid_accounts={1810}).bookings[0]
    assert booking.account == 6850            # SKR03 4980
    assert booking.counter_account == 1810    # SKR03 1210
    assert booking.tax_rate == pytest.approx(0.19)
    assert booking.tax_amount == pytest.approx(-19.0)


def test_vorzeichen_richtet_sich_nach_dem_zahlungskonto(tmp_path):
    """Gegenkonto liquide → Abgang; Konto liquide → Zugang."""
    builder, bookings = _wiso_builder()
    bookings.add(ID=1, ACCOUNTINGID=1, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTGROSS=119.0, ACCOUNTNO=4980, CONTRA_ACCOUNTNO=1210,
                 ACCOUNTING_TEXT='Ausgabe')
    bookings.add(ID=2, ACCOUNTINGID=2, ACCOUNTING_DATE='2024-05-03 00:00:00',
                 AMOUNTGROSS=500.0, ACCOUNTNO=1210, CONTRA_ACCOUNTNO=8400,
                 ACCOUNTING_TEXT='Einnahme')
    path = builder.write(str(tmp_path / 'w.fdb'))

    amounts = {b.text: b.amount
               for b in read_wiso_database(path, liquid_accounts={1810}).bookings}
    assert amounts['Ausgabe'] == pytest.approx(-119.0)
    assert amounts['Einnahme'] == pytest.approx(500.0)


def test_accountingid_wird_zur_split_klammer(tmp_path):
    """Gleicher Betrag, gleicher Tag, verschiedene Vorgänge – wie beim
    Anlagenverkauf. Nur die ACCOUNTINGID trennt sie sauber."""
    builder, bookings = _wiso_builder()
    bookings.add(ID=1, ACCOUNTINGID=500, ACCOUNTING_DATE='2024-04-30 00:00:00',
                 AMOUNTGROSS=7500.0, ACCOUNTNO=1210, CONTRA_ACCOUNTNO=10000,
                 ACCOUNTING_TEXT='Zahlung zu Re. 1', REFERENCENO='1')
    bookings.add(ID=2, ACCOUNTINGID=500, ACCOUNTING_DATE='2024-04-30 00:00:00',
                 AMOUNTNET=6302.52, AMOUNTGROSS=7500.0, TAXRATE=19.0,
                 ACCOUNTNO=8405, CONTRA_ACCOUNTNO=8400,
                 ACCOUNTING_TEXT='Umb. zu Re. 1', REFERENCENO='1')
    bookings.add(ID=3, ACCOUNTINGID=501, ACCOUNTING_DATE='2024-04-30 00:00:00',
                 AMOUNTGROSS=7500.0, ACCOUNTNO=8400, CONTRA_ACCOUNTNO=1360,
                 ACCOUNTING_TEXT='Umbuchung Anlagegut')
    path = builder.write(str(tmp_path / 'w.fdb'))

    groups = {}
    for booking in read_wiso_database(path, liquid_accounts={1810}).bookings:
        groups.setdefault(booking.group, []).append(booking)
    assert len(groups) == 2
    assert sorted(len(v) for v in groups.values()) == [1, 2]
    # Personenkonto 10000 bleibt unverändert – es steht in keinem Sachrahmen.
    assert groups['500'][0].counter_account == 10000


def test_unbekannte_konten_werden_gemeldet_nicht_geraten(tmp_path):
    builder, bookings = _wiso_builder()
    bookings.add(ID=1, ACCOUNTINGID=1, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTGROSS=10.0, ACCOUNTNO=9999, CONTRA_ACCOUNTNO=1210,
                 ACCOUNTING_TEXT='Unbekannt')
    path = builder.write(str(tmp_path / 'w.fdb'))

    data = read_wiso_database(path, liquid_accounts={1810})
    assert data.bookings[0].account is None
    assert data.bookings[0].unmapped == [9999]
    assert data.unmapped_accounts == {9999: 1}


def test_zeilenumbrueche_im_verwendungszweck_werden_geglaettet(tmp_path):
    builder, bookings = _wiso_builder()
    bookings.add(ID=1, ACCOUNTINGID=1, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTGROSS=10.0, ACCOUNTNO=4980, CONTRA_ACCOUNTNO=1210,
                 ACCOUNTING_TEXT='Erste Zeile\r\nzweite  Zeile')
    path = builder.write(str(tmp_path / 'w.fdb'))
    assert read_wiso_database(path).bookings[0].text == 'Erste Zeile zweite Zeile'


def test_fehlende_anlagentabellen_sind_kein_fehler(tmp_path):
    builder, bookings = _wiso_builder()
    bookings.add(ID=1, ACCOUNTINGID=1, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTGROSS=10.0, ACCOUNTNO=4980, CONTRA_ACCOUNTNO=1210)
    path = builder.write(str(tmp_path / 'w.fdb'))
    assert read_wiso_database(path).assets == []


# ----------------------------------------------------------------------
# Anlagen
# ----------------------------------------------------------------------
def _asset_builder():
    builder, bookings = _wiso_builder()
    assets = builder.table('BAS_INVENTORY', [
        Column('ID', LONG, 4),
        Column('INVNO', LONG, 4),
        Column('LABEL', TEXT, 40),
        Column('PURCHASEDATE', TIMESTAMP, 8),
        Column('PURCHASEAMOUNTNET', INT64, 8, scale=-2),
        Column('SALEDATE', TIMESTAMP, 8),
        Column('SERVICELIFE', SHORT, 2),
        Column('FINACIALACCOUNT', LONG, 4),
        Column('FINACIALACCOUNT_AFA', LONG, 4),
    ])
    payments = builder.table('MOV_INVENTORY_BOOKINGS', [
        Column('ID', LONG, 4),
        Column('INVENTORYID', LONG, 4),
        Column('BOOKINGDATE', TIMESTAMP, 8),
        Column('AMOUNTNET', INT64, 8, scale=-2),
        Column('DESCRIPTION', TEXT, 40),
    ])
    depreciations = builder.table('MOV_INVENTORY_AMORTIZATIONS', [
        Column('ID', LONG, 4),
        Column('INVENTORYID', LONG, 4),
        Column('BOOKINGDATE', TIMESTAMP, 8),
        Column('AMORT_AMOUNT', INT64, 8, scale=-2),
        Column('AMORT_CUMULATED_AMOUNT', INT64, 8, scale=-2),
        Column('RESIDUALVALUE_AMOUNT', INT64, 8, scale=-2),
    ])
    bookings.add(ID=1, ACCOUNTINGID=1, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTGROSS=10.0, ACCOUNTNO=4980, CONTRA_ACCOUNTNO=1210)
    return builder, assets, payments, depreciations


def test_anschaffungswert_ist_die_summe_der_positiven_zahlungen(tmp_path):
    """Der Abgang steht als negative Zeile in derselben Tabelle und zählt nicht."""
    builder, assets, payments, depreciations = _asset_builder()
    assets.add(ID=5, INVNO=1, LABEL='Fahrzeug', PURCHASEDATE='2022-06-07 00:00:00',
               PURCHASEAMOUNTNET=0.01, SALEDATE=None, SERVICELIFE=3,
               FINACIALACCOUNT=320, FINACIALACCOUNT_AFA=4832)
    payments.add(ID=1, INVENTORYID=5, BOOKINGDATE='2022-06-07 00:00:00',
                 AMOUNTNET=3000.0, DESCRIPTION='Teil 1')
    payments.add(ID=2, INVENTORYID=5, BOOKINGDATE='2022-06-09 00:00:00',
                 AMOUNTNET=2232.0, DESCRIPTION='Teil 2')
    payments.add(ID=3, INVENTORYID=5, BOOKINGDATE='2023-09-29 00:00:00',
                 AMOUNTNET=-4000.0, DESCRIPTION='Abschaffung')
    path = builder.write(str(tmp_path / 'a.fdb'))

    asset = read_wiso_database(path).assets[0]
    assert asset.purchase_price == pytest.approx(5232.0)
    assert asset.account == 520 and asset.depreciation_account == 6222
    assert len(asset.payments) == 3          # der Abgang bleibt sichtbar


def test_restbuchwert_beruecksichtigt_nachtraegliche_anschaffungskosten(tmp_path):
    """Eine spätere Zahlung erhöht den Wert – der Restwert muss mitwachsen."""
    builder, assets, payments, depreciations = _asset_builder()
    assets.add(ID=6, INVNO=2, LABEL='Anhänger', PURCHASEDATE='2019-10-26 00:00:00',
               PURCHASEAMOUNTNET=0.01, SERVICELIFE=11,
               FINACIALACCOUNT=320, FINACIALACCOUNT_AFA=4832)
    payments.add(ID=1, INVENTORYID=6, BOOKINGDATE='2019-10-28 00:00:00',
                 AMOUNTNET=486.55, DESCRIPTION='Kauf')
    payments.add(ID=2, INVENTORYID=6, BOOKINGDATE='2023-09-28 00:00:00',
                 AMOUNTNET=1512.61, DESCRIPTION='Nachruestung')
    depreciations.add(ID=1, INVENTORYID=6, BOOKINGDATE='2019-12-31 00:00:00',
                      AMORT_AMOUNT=11.06, AMORT_CUMULATED_AMOUNT=11.06)
    depreciations.add(ID=2, INVENTORYID=6, BOOKINGDATE='2023-12-31 00:00:00',
                      AMORT_AMOUNT=44.23, AMORT_CUMULATED_AMOUNT=187.98)
    path = builder.write(str(tmp_path / 'a.fdb'))

    asset = read_wiso_database(path).assets[0]
    assert asset.purchase_price == pytest.approx(1999.16)
    # 2019: nur die erste Zahlung ist geleistet
    assert asset.depreciations[0].book_value == pytest.approx(486.55 - 11.06)
    # 2023: beide Zahlungen zählen
    assert asset.depreciations[1].book_value == pytest.approx(1999.16 - 187.98)


def _verkauftes_anlagegut(builder, assets, payments, depreciations):
    """Abgang mitten im Jahr: zeitanteilige AfA, danach nur Erinnerungswert."""
    assets.add(ID=7, INVNO=3, LABEL='Verkauft', PURCHASEDATE='2020-02-27 00:00:00',
               PURCHASEAMOUNTNET=0.01, SALEDATE='2024-04-30 00:00:00',
               SERVICELIFE=6, FINACIALACCOUNT=320, FINACIALACCOUNT_AFA=4832)
    payments.add(ID=1, INVENTORYID=7, BOOKINGDATE='2020-02-27 00:00:00',
                 AMOUNTNET=21787.39, DESCRIPTION='Kauf')
    depreciations.add(ID=1, INVENTORYID=7, BOOKINGDATE='2023-12-31 00:00:00',
                      AMORT_AMOUNT=3658.70, AMORT_CUMULATED_AMOUNT=14165.08)
    depreciations.add(ID=2, INVENTORYID=7, BOOKINGDATE='2024-12-31 00:00:00',
                      AMORT_AMOUNT=914.67, AMORT_CUMULATED_AMOUNT=15079.75)
    depreciations.add(ID=3, INVENTORYID=7, BOOKINGDATE='2025-12-31 00:00:00',
                      AMORT_AMOUNT=-0.01, AMORT_CUMULATED_AMOUNT=15079.74)


def test_afa_im_abgangsjahr_bleibt_erhalten(tmp_path):
    """Die zeitanteilige AfA des Abgangsjahres ist echt und muss bleiben.

    Erst was WISO **danach** plant, ist der Erinnerungswert.
    """
    builder, assets, payments, depreciations = _asset_builder()
    _verkauftes_anlagegut(builder, assets, payments, depreciations)
    path = builder.write(str(tmp_path / 'a.fdb'))

    asset = read_wiso_database(path).assets[0]
    assert [d.year for d in asset.depreciations] == [2023, 2024]
    # Restwert im Abgangsjahr = das, was die Abgangsbuchung ausbucht.
    assert asset.depreciations[-1].book_value == pytest.approx(6707.64)


def test_erinnerungswert_buchungen_werden_verworfen(tmp_path):
    """AfA-Buchungen für ein längst verkauftes Anlagegut sind WISO-Ballast."""
    builder, assets, payments, depreciations = _asset_builder()
    _verkauftes_anlagegut(builder, assets, payments, depreciations)
    bookings = next(t for t in builder.tables
                    if t.name == 'MOV_FINACC_ACCRECORDS')
    bookings.add(ID=90, ACCOUNTINGID=90, ACCOUNTING_DATE='2024-12-31 00:00:00',
                 AMOUNTGROSS=914.67, ACCOUNTNO=4832, CONTRA_ACCOUNTNO=320,
                 ACCOUNTING_TEXT='Afa fuer Inventar', INVENTORYID=7)
    bookings.add(ID=91, ACCOUNTINGID=91, ACCOUNTING_DATE='2025-12-31 00:00:00',
                 AMOUNTGROSS=0.01, ACCOUNTNO=320, CONTRA_ACCOUNTNO=4832,
                 ACCOUNTING_TEXT='Afa fuer Inventar', INVENTORYID=7)
    path = builder.write(str(tmp_path / 'a.fdb'))

    data = read_wiso_database(path)
    assert data.memo_rows_skipped == 1
    texte = [b.text for b in data.bookings]
    assert texte.count('Afa fuer Inventar') == 1     # nur die aus 2024


def test_standardrahmen_ergaenzt_fehlende_konten(tmp_path):
    """DB0 liefert Konten, die im Mandantenrahmen fehlen."""
    standard = FdbBuilder(page_size=16384)
    standard.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4), Column('SKR04', LONG, 4),
        Column('BOOKINGYEAR', SHORT, 2), Column('ACCOUNTTEXT', TEXT, 40),
    ]).add(ID=9999, SKR04=7777, BOOKINGYEAR=2020, ACCOUNTTEXT='Nur im Standard')
    standard_path = standard.write(str(tmp_path / 'db0.fdb'))

    builder, bookings = _wiso_builder()
    bookings.add(ID=1, ACCOUNTINGID=1, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTGROSS=10.0, ACCOUNTNO=9999, CONTRA_ACCOUNTNO=1210)
    path = builder.write(str(tmp_path / 'db1.fdb'))

    data = read_wiso_database(path, standard_path, liquid_accounts={1810})
    assert data.bookings[0].account == 7777
    assert data.unmapped_accounts == {}


def test_mandantenrahmen_schlaegt_standardrahmen(tmp_path):
    """Bei Konflikt gilt der Mandant – er ist der gepflegte Stand."""
    standard = FdbBuilder(page_size=16384)
    standard.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4), Column('SKR04', LONG, 4),
        Column('BOOKINGYEAR', SHORT, 2), Column('ACCOUNTTEXT', TEXT, 40),
    ]).add(ID=4980, SKR04=1111, BOOKINGYEAR=2020, ACCOUNTTEXT='Alt')
    standard_path = standard.write(str(tmp_path / 'db0.fdb'))

    builder, bookings = _wiso_builder()          # kennt 4980 → 6850, Jahr 2024
    bookings.add(ID=1, ACCOUNTINGID=1, ACCOUNTING_DATE='2024-05-02 00:00:00',
                 AMOUNTGROSS=10.0, ACCOUNTNO=4980, CONTRA_ACCOUNTNO=1210)
    path = builder.write(str(tmp_path / 'db1.fdb'))

    data = read_wiso_database(path, standard_path, liquid_accounts={1810})
    assert data.bookings[0].account == 6850


def test_tabellenliste_ohne_systemtabellen(tmp_path):
    builder, _bookings = _wiso_builder()
    path = builder.write(str(tmp_path / 'w.fdb'))
    with WisoDatabase(path) as wiso:
        assert sorted(wiso.tables) == ['BAS_FINACC_PLAN', 'MOV_FINACC_ACCRECORDS']
