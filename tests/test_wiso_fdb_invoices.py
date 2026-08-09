# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kunden und Ausgangsrechnungen aus der WISO-Datenbank.

Die Eigenheit dieses Teils: der **Positionstext** steht nicht bei der
Rechnungsposition, sondern bei der zugehörigen Auftragsposition – dort hat der
Nutzer ihn für den Beleg angepasst. Und er ist ein RTF-Blob.

Alle Daten sind synthetisch (siehe :mod:`tests.fdb_builder`).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fdb_builder import (BLOB, DATE, DOUBLE, FdbBuilder, INT64,  # noqa: E402
                         LONG, SHORT, TEXT, TIMESTAMP, VARYING, Column)
from importers.wiso_fdb import read_wiso_database  # noqa: E402
from importers.wiso_fdb.rtf import rtf_to_text  # noqa: E402


# ----------------------------------------------------------------------
# RTF – Einleitungs- und Schlusstexte liegen so in der Datenbank
# ----------------------------------------------------------------------
def test_rtf_liefert_nur_den_wortlaut():
    """Schrift- und Farbtabelle dürfen nicht im Text landen.

    Genau daran scheitert ein regulärer Ausdruck: er lässt „Segoe UI;;;;“
    stehen.
    """
    quelle = (r'{\rtf1\ansi{\fonttbl{\f0\fnil Segoe UI;}}'
              r'{\colortbl;\red0\green0\blue0;}'
              r'\f0\fs20 Zahlbar in 14 Tagen.\par Vielen Dank.}')
    assert rtf_to_text(quelle) == 'Zahlbar in 14 Tagen.\nVielen Dank.'


def test_rtf_loest_umlaute_und_sonderzeichen_auf():
    assert rtf_to_text(r"{\rtf1\ansi Gr\'fc\'dfe}") == 'Grüße'
    assert rtf_to_text('{\\rtf1\\ansi 5 \\u8364?}') == '5 €'   # Unicode-Escape
    assert rtf_to_text(r'{\rtf1\ansi a\tab b}') == 'a\tb'
    assert rtf_to_text(r'{\rtf1\ansi Klammer \{auf\}}') == 'Klammer {auf}'


def test_rtf_laesst_reinen_text_unangetastet():
    assert rtf_to_text('  einfach Text  ') == 'einfach Text'
    assert rtf_to_text(None) == ''
    assert rtf_to_text('') == ''


# ----------------------------------------------------------------------
def _builder():
    """WISO-Datenbank mit Firma, Kunden, Rechnungen und Positionen."""
    builder = FdbBuilder(page_size=16384)
    builder.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4), Column('SKR04', LONG, 4),
        Column('BOOKINGYEAR', SHORT, 2), Column('ACCOUNTTEXT', TEXT, 40),
    ]).add(ID=8400, SKR04=4400, BOOKINGYEAR=2024, ACCOUNTTEXT='Erlöse 19 %')
    builder.table('MOV_FINACC_ACCRECORDS', [
        Column('ID', LONG, 4), Column('ACCOUNTINGID', LONG, 4),
        Column('ACCOUNTING_DATE', TIMESTAMP, 8),
        Column('AMOUNTGROSS', INT64, 8, scale=-2),
        Column('ACCOUNTNO', LONG, 4), Column('CONTRA_ACCOUNTNO', LONG, 4),
        Column('ACCOUNTING_TEXT', TEXT, 40),
    ])
    builder.table('BAS_COMPANY', [
        Column('ID', LONG, 4), Column('NAME1', TEXT, 40),
        Column('STREET', TEXT, 40), Column('ZIPCODE', TEXT, 10),
        Column('CITY', TEXT, 30), Column('COUNTRY', TEXT, 4),
        Column('VATID', TEXT, 20), Column('EMAIL', TEXT, 40),
        Column('PHONE1', TEXT, 24), Column('EMPLNAME1', TEXT, 30),
        Column('EMPLNAME2', TEXT, 30),
    ]).add(ID=1, NAME1='Mustermann IT', STREET='Hauptstr. 1', ZIPCODE='11111',
           CITY='Musterstadt', COUNTRY='D', VATID='DE111111111',
           EMAIL='post@example.org', PHONE1='+49 111 1',
           EMPLNAME1='Muster', EMPLNAME2='Max')
    customers = builder.table('BAS_CUSTOMERS', [
        Column('ID', LONG, 4), Column('CUSTNO', LONG, 4),
        Column('NAME1', TEXT, 40), Column('NAME2', TEXT, 40),
        Column('STREET', TEXT, 40), Column('ZIPCODE', TEXT, 10),
        Column('CITY', TEXT, 30), Column('COUNTRY', TEXT, 4),
        Column('EMAIL', TEXT, 40), Column('VATID', TEXT, 20),
    ])
    invoices = builder.table('MOV_INVOICES', [
        Column('ID', LONG, 4), Column('INVNO', VARYING, 42),
        Column('INVDATE', TIMESTAMP, 8), Column('CUSTID', LONG, 4),
        Column('NAME1', TEXT, 40), Column('STREET', TEXT, 40),
        Column('ZIPCODE', TEXT, 10), Column('CITY', TEXT, 30),
        Column('COUNTRY', TEXT, 4), Column('TOTALNET', DOUBLE, 8),
        Column('VAT1', DOUBLE, 8), Column('TOTALGROSS', DOUBLE, 8),
        Column('VAT1PERC', LONG, 4), Column('PAYSTATE', SHORT, 2),
        Column('PAYDAYS', SHORT, 2), Column('SERVICEDATE', DATE, 4),
        Column('TEXT1', BLOB, 8, sub_type=1),
        Column('TEXT2', BLOB, 8, sub_type=1),
    ])
    positions = builder.table('MOV_INVOICES_POSITIONS', [
        Column('ID', LONG, 4), Column('INVID', LONG, 4),
        Column('ORDPOSID', LONG, 4), Column('AMOUNT', DOUBLE, 8),
        Column('TOTAL', DOUBLE, 8), Column('POSID', SHORT, 2),
    ])
    order_positions = builder.table('MOV_ORDERS_POSITIONS', [
        Column('ID', LONG, 4), Column('PRICENET', DOUBLE, 8),
        Column('UNITCODE', SHORT, 2), Column('ARTNO', TEXT, 20),
        Column('ARTDESCR', BLOB, 8, sub_type=1),
    ])
    builder.table('SUP_ARTICLES_UNITS', [
        Column('ID', LONG, 4), Column('LABEL', TEXT, 12),
        Column('OPENTRANSCODE', TEXT, 6),
    ]).add(ID=4, LABEL='Std.', OPENTRANSCODE='HUR')
    return builder, customers, invoices, positions, order_positions


# ----------------------------------------------------------------------
# Rechnungen
# ----------------------------------------------------------------------
def test_rechnung_mit_positionen_wird_gelesen(tmp_path):
    builder, kunden, rechnungen, positionen, auftrag = _builder()
    kunden.add(ID=3, CUSTNO=7, NAME1='Beispiel GmbH', STREET='Weg 2',
               ZIPCODE='22222', CITY='Beispielstadt', COUNTRY='D')
    rechnungen.add(ID=1, INVNO='2024001', INVDATE='2024-03-01 00:00:00',
                   CUSTID=3, NAME1='Beispiel GmbH', STREET='Weg 2',
                   ZIPCODE='22222', CITY='Beispielstadt', COUNTRY='D',
                   TOTALNET=1400.0, VAT1=266.0, TOTALGROSS=1666.0,
                   VAT1PERC=19, PAYSTATE=30, PAYDAYS=14,
                   TEXT2=r'{\rtf1\ansi Zahlbar in 14 Tagen.}')
    auftrag.add(ID=10, PRICENET=70.0, UNITCODE=4, ARTNO='101',
                ARTDESCR='Softwareentwicklung, angepasster Text')
    positionen.add(ID=1, INVID=1, ORDPOSID=10, AMOUNT=20.0, TOTAL=1400.0,
                   POSID=1)
    path = builder.write(str(tmp_path / 'w.fdb'))

    data = read_wiso_database(path)
    assert len(data.invoices) == 1
    invoice = data.invoices[0]
    assert invoice.number == '2024001' and invoice.date == '2024-03-01'
    assert invoice.customer_number == 7
    assert invoice.status == 'paid'
    assert invoice.tax_rate == pytest.approx(0.19)
    assert invoice.sum_net == pytest.approx(1400.0)
    assert invoice.sum_gross == pytest.approx(1666.0)
    assert invoice.closing_text == 'Zahlbar in 14 Tagen.'
    assert invoice.warnings == []

    item = invoice.items[0]
    assert item.description == 'Softwareentwicklung, angepasster Text'
    assert item.quantity == pytest.approx(20.0)
    assert item.price_per_unit == pytest.approx(70.0)
    assert item.unit == 'HUR'                     # aus OPENTRANSCODE
    assert item.tax_rate == pytest.approx(0.19)
    assert item.article_number == '101'


def test_positionstext_kommt_aus_der_auftragsposition(tmp_path):
    """Die Rechnungsposition hat gar kein Textfeld – der Text ist ein Blob
    an der Auftragsposition und muss aufgelöst werden."""
    builder, kunden, rechnungen, positionen, auftrag = _builder()
    kunden.add(ID=3, CUSTNO=7, NAME1='Beispiel GmbH', COUNTRY='D')
    rechnungen.add(ID=1, INVNO='2024001', INVDATE='2024-03-01 00:00:00',
                   CUSTID=3, NAME1='Beispiel GmbH', TOTALNET=100.0,
                   VAT1=19.0, TOTALGROSS=119.0, VAT1PERC=19, PAYSTATE=30)
    auftrag.add(ID=10, PRICENET=100.0, UNITCODE=0,
                ARTDESCR='Für diesen Beleg angepasste Leistungsbeschreibung')
    positionen.add(ID=1, INVID=1, ORDPOSID=10, AMOUNT=1.0, TOTAL=100.0, POSID=1)
    path = builder.write(str(tmp_path / 'w.fdb'))

    item = read_wiso_database(path).invoices[0].items[0]
    assert item.description == 'Für diesen Beleg angepasste Leistungsbeschreibung'
    assert item.unit == 'C62'                     # unbekannte Einheit -> Stück


def test_mehrere_positionen_bleiben_in_reihenfolge(tmp_path):
    builder, kunden, rechnungen, positionen, auftrag = _builder()
    kunden.add(ID=3, CUSTNO=7, NAME1='Beispiel GmbH', COUNTRY='D')
    rechnungen.add(ID=1, INVNO='2024001', INVDATE='2024-03-01 00:00:00',
                   CUSTID=3, NAME1='Beispiel GmbH', TOTALNET=300.0,
                   VAT1=57.0, TOTALGROSS=357.0, VAT1PERC=19, PAYSTATE=30)
    for index, (betrag, text) in enumerate(((100.0, 'Erste'), (200.0, 'Zweite')),
                                           start=1):
        auftrag.add(ID=10 + index, PRICENET=betrag, UNITCODE=0, ARTDESCR=text)
        positionen.add(ID=index, INVID=1, ORDPOSID=10 + index, AMOUNT=1.0,
                       TOTAL=betrag, POSID=3 - index)      # verkehrt eingefügt
    path = builder.write(str(tmp_path / 'w.fdb'))

    items = read_wiso_database(path).invoices[0].items
    assert [i.position for i in items] == [1, 2]
    assert [i.description for i in items] == ['Zweite', 'Erste']


def test_zahlstatus_wird_uebersetzt(tmp_path):
    builder, kunden, rechnungen, positionen, auftrag = _builder()
    kunden.add(ID=3, CUSTNO=7, NAME1='Beispiel GmbH', COUNTRY='D')
    auftrag.add(ID=10, PRICENET=100.0, UNITCODE=0, ARTDESCR='Leistung')
    for index, paystate in enumerate((10, 20, 30), start=1):
        rechnungen.add(ID=index, INVNO=f'20240{index:02d}',
                       INVDATE='2024-03-01 00:00:00', CUSTID=3,
                       NAME1='Beispiel GmbH', TOTALNET=100.0, VAT1=19.0,
                       TOTALGROSS=119.0, VAT1PERC=19, PAYSTATE=paystate)
        positionen.add(ID=index, INVID=index, ORDPOSID=10, AMOUNT=1.0,
                       TOTAL=100.0, POSID=1)
    path = builder.write(str(tmp_path / 'w.fdb'))

    status = {i.number: i.status for i in read_wiso_database(path).invoices}
    assert status == {'2024001': 'sent', '2024002': 'partial_payment',
                      '2024003': 'paid'}


def test_abweichende_positionssumme_wird_gemeldet(tmp_path):
    """Stimmt die Summe der Positionen nicht zum Rechnungsnetto, fällt es auf."""
    builder, kunden, rechnungen, positionen, auftrag = _builder()
    kunden.add(ID=3, CUSTNO=7, NAME1='Beispiel GmbH', COUNTRY='D')
    rechnungen.add(ID=1, INVNO='2024002', INVDATE='2024-03-01 00:00:00',
                   CUSTID=3, NAME1='Beispiel GmbH', TOTALNET=1000.0,
                   VAT1=190.0, TOTALGROSS=1190.0, VAT1PERC=19, PAYSTATE=30)
    auftrag.add(ID=10, PRICENET=700.0, UNITCODE=0, ARTDESCR='Leistung')
    positionen.add(ID=1, INVID=1, ORDPOSID=10, AMOUNT=1.0, TOTAL=700.0, POSID=1)
    path = builder.write(str(tmp_path / 'w.fdb'))

    invoice = read_wiso_database(path).invoices[0]
    assert len(invoice.warnings) == 1
    assert '700.00' in invoice.warnings[0] and '1000.00' in invoice.warnings[0]


def test_geloeschter_kunde_wird_gemeldet_aber_nicht_verworfen(tmp_path):
    """Die Anschrift steht in der Rechnung selbst – sie bleibt brauchbar."""
    builder, kunden, rechnungen, positionen, auftrag = _builder()
    rechnungen.add(ID=1, INVNO='2024003', INVDATE='2024-03-01 00:00:00',
                   CUSTID=99, NAME1='Weg GmbH', STREET='Weg 9',
                   ZIPCODE='33333', CITY='Nirgendwo', COUNTRY='D',
                   TOTALNET=100.0, VAT1=19.0, TOTALGROSS=119.0,
                   VAT1PERC=19, PAYSTATE=30)
    auftrag.add(ID=10, PRICENET=100.0, UNITCODE=0, ARTDESCR='Leistung')
    positionen.add(ID=1, INVID=1, ORDPOSID=10, AMOUNT=1.0, TOTAL=100.0, POSID=1)
    path = builder.write(str(tmp_path / 'w.fdb'))

    invoice = read_wiso_database(path).invoices[0]
    assert invoice.customer_number is None
    assert invoice.buyer_city == 'Nirgendwo'
    assert any('Kundenstamm' in w for w in invoice.warnings)


# ----------------------------------------------------------------------
# Kunden
# ----------------------------------------------------------------------
def test_sammelposten_sind_keine_kunden(tmp_path):
    """„(alle)“ und „(diverse Kunden)“ tragen nicht-positive Ids."""
    builder, kunden, _r, _p, _a = _builder()
    kunden.add(ID=-100, CUSTNO=-1, NAME1='(alle)')
    kunden.add(ID=-1000, CUSTNO=-2, NAME1='(diverse Kunden)')
    kunden.add(ID=3, CUSTNO=7, NAME1='Echte GmbH', COUNTRY='D')
    path = builder.write(str(tmp_path / 'w.fdb'))

    assert [c.number for c in read_wiso_database(path).customers] == [7]


def test_person_und_firma_werden_unterschieden(tmp_path):
    """Vorname ohne Rechtsform im Namen heißt Person; sonst Ansprechpartner."""
    builder, kunden, _r, _p, _a = _builder()
    kunden.add(ID=3, CUSTNO=1, NAME1='Schmidt', NAME2='Anna', COUNTRY='D')
    kunden.add(ID=4, CUSTNO=2, NAME1='Beispiel GmbH', NAME2='Herr Meier',
               COUNTRY='D')
    kunden.add(ID=5, CUSTNO=3, NAME1='Nur Firma', COUNTRY='D')
    path = builder.write(str(tmp_path / 'w.fdb'))

    by_number = {c.number: c for c in read_wiso_database(path).customers}
    person = by_number[1]
    assert person.entity_type == 'person'
    assert (person.first_name, person.last_name) == ('Anna', 'Schmidt')
    assert person.display_name == 'Anna Schmidt'

    firma = by_number[2]
    assert firma.entity_type == 'company'
    assert firma.company_name == 'Beispiel GmbH'
    assert firma.address_line1 == 'Herr Meier'          # Ansprechpartner
    assert by_number[3].entity_type == 'company'


def test_laenderkuerzel_werden_uebersetzt(tmp_path):
    builder, kunden, _r, _p, _a = _builder()
    kunden.add(ID=3, CUSTNO=1, NAME1='Inland GmbH', COUNTRY='D')
    kunden.add(ID=4, CUSTNO=2, NAME1='Alpen AG', COUNTRY='A')
    kunden.add(ID=5, CUSTNO=3, NAME1='Ohne Land GmbH')
    path = builder.write(str(tmp_path / 'w.fdb'))

    laender = {c.number: c.country for c in read_wiso_database(path).customers}
    assert laender == {1: 'DE', 2: 'AT', 3: 'DE'}


def test_eigene_firma_wird_gelesen(tmp_path):
    builder, _k, _r, _p, _a = _builder()
    path = builder.write(str(tmp_path / 'w.fdb'))
    company = read_wiso_database(path).company
    assert company.company == 'Mustermann IT'
    assert company.name == 'Max Muster'              # Vorname Nachname
    assert company.vat_id == 'DE111111111' and company.country == 'DE'


# ----------------------------------------------------------------------
# Übernahme in die Datenbank
# ----------------------------------------------------------------------
@pytest.fixture
def rechnungs_fdb(tmp_path):
    builder, kunden, rechnungen, positionen, auftrag = _builder()
    kunden.add(ID=3, CUSTNO=9007, NAME1='Beispiel GmbH', STREET='Weg 2',
               ZIPCODE='22222', CITY='Beispielstadt', COUNTRY='D',
               EMAIL='rechnung@example.org', VATID='DE222222222')
    kunden.add(ID=4, CUSTNO=9008, NAME1='Schmidt', NAME2='Anna', COUNTRY='D')
    # Unverwechselbare Nummern: die Testdaten bringen eigene Rechnungen mit.
    rechnungen.add(ID=1, INVNO='W-2024001', INVDATE='2024-03-01 00:00:00',
                   CUSTID=3, NAME1='Beispiel GmbH', STREET='Weg 2',
                   ZIPCODE='22222', CITY='Beispielstadt', COUNTRY='D',
                   TOTALNET=1400.0, VAT1=266.0, TOTALGROSS=1666.0,
                   VAT1PERC=19, PAYSTATE=30, PAYDAYS=14,
                   TEXT2=r'{\rtf1\ansi Zahlbar in 14 Tagen.}')
    auftrag.add(ID=10, PRICENET=70.0, UNITCODE=4, ARTNO='101',
                ARTDESCR='Softwareentwicklung')
    positionen.add(ID=1, INVID=1, ORDPOSID=10, AMOUNT=20.0, TOTAL=1400.0,
                   POSID=1)
    return builder.write(str(tmp_path / 'db1.fdb'))


def test_kunden_behalten_ihre_nummer(db_with_coa, rechnungs_fdb):
    result = db_with_coa.import_wiso_fdb(rechnungs_fdb)
    assert result['customers'] == 2

    conn = db_with_coa._get_connection()
    rows = dict(conn.execute(
        'SELECT CustomerNumber, DisplayName FROM Contacts '
        'WHERE CustomerNumber IN (?, ?)', ('9007', '9008')).fetchall())
    stadt = conn.execute(
        "SELECT City FROM ContactAddresses WHERE ContactID = "
        "(SELECT ID FROM Contacts WHERE CustomerNumber = '9007')").fetchone()
    conn.close()
    assert rows == {'9007': 'Beispiel GmbH', '9008': 'Anna Schmidt'}
    assert stadt[0] == 'Beispielstadt'


def test_rechnung_landet_mit_position_und_kundenbezug(db_with_coa,
                                                      rechnungs_fdb):
    result = db_with_coa.import_wiso_fdb(rechnungs_fdb)
    assert result['invoices'] == 1 and result['invoice_items'] == 1

    conn = db_with_coa._get_connection()
    invoice = conn.execute('''
        SELECT i.InvoiceNumber, i.InvoiceDate, i.SumNet, i.SumGross, i.Status,
               i.PaymentDueDate, i.ClosingText, i.SellerCompany, c.CustomerNumber
        FROM Invoices i LEFT JOIN Contacts c ON c.ID = i.CustomerId
        WHERE i.InvoiceNumber = ?''', ('W-2024001',)).fetchone()
    item = conn.execute('''
        SELECT Position, Description, Quantity, Unit, PricePerUnit, TotalNet,
               TaxRate FROM InvoiceItems
        WHERE InvoiceId = (SELECT ID FROM Invoices WHERE InvoiceNumber = ?)''',
        ('W-2024001',)).fetchone()
    conn.close()

    assert invoice[0] == 'W-2024001' and invoice[1] == '2024-03-01'
    assert invoice[2] == 14000000 and invoice[3] == 16660000   # Minor Units
    assert invoice[4] == 'paid'
    assert invoice[5] == '2024-03-15'                # Datum + 14 Tage
    assert invoice[6] == 'Zahlbar in 14 Tagen.'
    assert invoice[7] == 'Mustermann IT'             # eigene Firma
    assert invoice[8] == '9007'

    assert item[1] == 'Softwareentwicklung'
    assert item[2] == pytest.approx(20.0) and item[3] == 'HUR'
    assert item[4] == 700000 and item[5] == 14000000
    assert item[6] == pytest.approx(0.19)


def test_bezahlte_rechnung_hat_nichts_mehr_offen(db_with_coa, rechnungs_fdb):
    db_with_coa.import_wiso_fdb(rechnungs_fdb)
    conn = db_with_coa._get_connection()
    offen = conn.execute('SELECT AmountDue FROM Invoices WHERE InvoiceNumber = ?',
                         ('W-2024001',)).fetchone()[0]
    conn.close()
    assert offen == 0


def test_zweiter_lauf_verdoppelt_rechnungen_nicht(db_with_coa, rechnungs_fdb):
    db_with_coa.import_wiso_fdb(rechnungs_fdb)
    second = db_with_coa.import_wiso_fdb(rechnungs_fdb)
    assert second['invoices'] == 0 and second['invoices_skipped'] == 1
    assert second['customers'] == 0

    # Die Testdaten bringen eigene Rechnungen mit – gezaehlt wird nur unsere.
    conn = db_with_coa._get_connection()
    rechnungen = conn.execute(
        'SELECT COUNT(*) FROM Invoices WHERE InvoiceNumber = ?',
        ('W-2024001',)).fetchone()[0]
    positionen = conn.execute(
        'SELECT COUNT(*) FROM InvoiceItems WHERE InvoiceId IN '
        '(SELECT ID FROM Invoices WHERE InvoiceNumber = ?)',
        ('W-2024001',)).fetchone()[0]
    kunden = conn.execute(
        'SELECT COUNT(*) FROM Contacts WHERE CustomerNumber IN (?, ?)',
        ('9007', '9008')).fetchone()[0]
    conn.close()
    assert (rechnungen, positionen, kunden) == (1, 1, 2)


def test_ohne_rechnungsimport_bleiben_die_tabellen_leer(db_with_coa,
                                                        rechnungs_fdb):
    result = db_with_coa.import_wiso_fdb(rechnungs_fdb, with_invoices=False)
    assert result['invoices'] == 0 and result['customers'] == 0

    conn = db_with_coa._get_connection()
    unsere = conn.execute(
        'SELECT COUNT(*) FROM Invoices WHERE InvoiceNumber = ?',
        ('W-2024001',)).fetchone()[0]
    conn.close()
    assert unsere == 0
