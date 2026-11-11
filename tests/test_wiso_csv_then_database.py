# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""WISO: CSV-Import und Datenbank-Import nacheinander (2.0.6).

Der CSV-Import schreibt keine ``SourceGroup`` und erkennt Duplikate am Schlüssel Beleg-Nr. +
Datum + Konto + Betrag. Der Datenbank-Import kannte bisher nur die ``SourceGroup`` – nach einem
CSV-Import legte er alles ein zweites Mal an. Jetzt übernimmt er eine vorhandene Buchung, statt
sie neu anzulegen (Zähler ``adopted``, ``adopted_rows``, Meldung auf der Seite „Verschiedenes“) –
aber nur, wenn alles gilt:

1. Kandidat ist ``entry`` ohne ``SourceGroup``, kein ``AutoMirror``, ohne ``Account_ID``; gleiches
   Datum, gleicher Betrag, gleiches Konto (``COA_ID``), gleiche Beleg-Nr. (Leerraum zusammengefasst,
   leer = NULL).
2. Das Gegenkonto passt: beide kennen es und es ist gleich – oder der Kandidat hat keines und der
   Text stimmt überein (Leerraum zusammengefasst, Groß-/Kleinschreibung egal). Ein ANDERES
   Gegenkonto verhindert die Übernahme: lieber eine zweite, sichtbare Buchung als eine still
   falsche Zuordnung.
3. Mehrere Kandidaten: gleiches Gegenkonto gewinnt, sonst gleicher Text – nie „der erste nach ID“.
4. Ersatzsuche ohne Konto (``COA_ID IS NULL``): nur wenn es für den Schlüssel von Anfang an keinen
   Kandidaten mit Konto gab, und nur bei gleichem, bekanntem Gegenkonto.
5. Übernommen wird: ``SourceGroup`` setzen, leere Konten füllen, ``TaxRate``/``TaxAmount`` (nur wenn
   beide leer sind) und ``RecipientClient`` (wenn leer) ergänzen – sonst bleibt alles.

Die Sätze sind erfunden (``tests/fdb_builder.py``): ein Verkauf, ein Büro-Einkauf, zwei gleich hohe
Teilbuchungen desselben Belegs, ein Satz ohne Beleg-Nr. und ein Satz aus dem Vorjahr, den die CSV
nicht enthält. Die Reihenfolgen und Zahlen sind die Tabelle aus dem Umsetzungsplan.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, quote, urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import userctx  # noqa: E402
from fdb_builder import (FdbBuilder, INT64, LONG, SHORT, TEXT,  # noqa: E402
                         TIMESTAMP, Column)
from server import handlers  # noqa: E402
from server.pages_miscellaneous import PageMiscellaneous  # noqa: E402
from tests.http_client import get, own_origin, post, send  # noqa: E402
from tests.test_wiso_fdb_invoices import _payments, _zahlungs_fdb  # noqa: E402

#: SKR03 → SKR04 wie WISOs Umschlüsselung (BAS_FINACC_PLAN); 1800/1810 sind die Bankkonten der
#: Testdaten, 1600 die Kasse, 1460 das Verrechnungskonto. Personenkonten (ab 10000) bleiben, wie
#: sie sind, und brauchen keine Zeile.
CHART = ((1200, 1800, 'Bank'), (1210, 1810, 'Bankkonto 2'), (1000, 1600, 'Kasse'),
         (1360, 1460, 'Verrechnungskonto'), (8400, 4400, 'Erlöse 19 %'),
         (4930, 6815, 'Bürobedarf'), (4900, 6300, 'Sonstige Aufwendungen'))
#: ein Konto, das der eigene Kontenrahmen nicht kennt
NEW_ACCOUNT = (4999, 6999, 'Neu angelegt')

VERKAUF, BUERO, SPLIT_A, SPLIT_B, OHNE_BELEG, VORJAHR = range(6)


def _records():
    """Die sechs Sätze der WISO-Datenbank (MOV_FINACC_ACCRECORDS)."""
    return [
        dict(ID=1, ACCOUNTINGID=700, ACCOUNTING_DATE='2024-03-01 00:00:00', AMOUNTNET=1000.0,
             AMOUNTGROSS=1190.0, TAXRATE=19.0, ACCOUNTNO=1200, CONTRA_ACCOUNTNO=8400,
             ACCOUNTING_TEXT='Probe Verkauf', REFERENCENO='PR-001'),
        dict(ID=2, ACCOUNTINGID=701, ACCOUNTING_DATE='2024-03-05 00:00:00', AMOUNTNET=200.0,
             AMOUNTGROSS=238.0, TAXRATE=19.0, ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200,
             ACCOUNTING_TEXT='Probe Buero', REFERENCENO='PR-002'),
        dict(ID=3, ACCOUNTINGID=702, ACCOUNTING_DATE='2024-03-05 00:00:00', AMOUNTNET=50.0,
             AMOUNTGROSS=50.0, ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200,
             ACCOUNTING_TEXT='Probe Split A', REFERENCENO='PR-003'),
        dict(ID=4, ACCOUNTINGID=703, ACCOUNTING_DATE='2024-03-05 00:00:00', AMOUNTNET=50.0,
             AMOUNTGROSS=50.0, ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200,
             ACCOUNTING_TEXT='Probe Split B', REFERENCENO='PR-003'),
        dict(ID=5, ACCOUNTINGID=704, ACCOUNTING_DATE='2024-03-15 00:00:00', AMOUNTGROSS=500.0,
             ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200, ACCOUNTING_TEXT='Probe ohne Beleg'),
        dict(ID=6, ACCOUNTINGID=705, ACCOUNTING_DATE='2023-12-01 00:00:00', AMOUNTGROSS=40.0,
             ACCOUNTNO=4900, CONTRA_ACCOUNTNO=1200, ACCOUNTING_TEXT='Probe Vorjahr',
             REFERENCENO='PR-000'),
    ]


def _record(**overrides):
    """Ein einzelner Satz: der „Verkauf“ (1.190,00 € am 01.03.2024, Beleg PR-001, Bank an Erlöse) –
    mit den Abweichungen aus ``overrides``. Er passt zu dem, was ``_candidate`` anlegt."""
    values = dict(ID=1, ACCOUNTINGID=900, ACCOUNTING_DATE='2024-03-01 00:00:00', AMOUNTNET=1000.0,
                  AMOUNTGROSS=1190.0, TAXRATE=19.0, ACCOUNTNO=1200, CONTRA_ACCOUNTNO=8400,
                  ACCOUNTING_TEXT='Probe Verkauf', REFERENCENO='PR-001')
    values.update(overrides)
    return values


def _fdb(tmp_path, records=None, extra_chart=(), name='db1.fdb'):
    """Synthetische WISO-Mandantendatenbank mit diesen Sätzen (Standard: die sechs von oben)."""
    builder = FdbBuilder(page_size=16384)
    chart = builder.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4), Column('SKR04', LONG, 4),
        Column('BOOKINGYEAR', SHORT, 2), Column('ACCOUNTTEXT', TEXT, 40)])
    for skr03, skr04, text in CHART + tuple(extra_chart):
        chart.add(ID=skr03, SKR04=skr04, BOOKINGYEAR=2024, ACCOUNTTEXT=text)
    table = builder.table('MOV_FINACC_ACCRECORDS', [
        Column('ID', LONG, 4), Column('ACCOUNTINGID', LONG, 4),
        Column('ACCOUNTING_DATE', TIMESTAMP, 8), Column('AMOUNTNET', INT64, 8, scale=-2),
        Column('AMOUNTGROSS', INT64, 8, scale=-2), Column('TAXRATE', INT64, 8, scale=-2),
        Column('ACCOUNTNO', LONG, 4), Column('CONTRA_ACCOUNTNO', LONG, 4),
        Column('ACCOUNTING_TEXT', TEXT, 60), Column('REFERENCENO', TEXT, 20)])
    for record in (_records() if records is None else records):
        table.add(**record)
    return builder.write(str(tmp_path / name))


#: Die CSV-Zeilen (Original-Export): Datum, Konto, Gegenkonto, Text, Beleg-Nr., Brutto, Schlüssel.
#: Konten sind SKR04-Nummern; der Satz „Vorjahr“ steht nicht in der CSV.
CSV_ROWS = [
    ('01.03.2024', '1800', '4400', 'Probe Verkauf', 'PR-001', '1190,00', '401'),
    ('05.03.2024', '6815', '1800', 'Probe Buero', 'PR-002', '238,00', '401'),
    ('05.03.2024', '6815', '1800', 'Probe Split A', 'PR-003', '50,00', ''),
    ('05.03.2024', '6815', '1800', 'Probe Split B', 'PR-003', '50,00', ''),
    ('15.03.2024', '6815', '1800', 'Probe ohne Beleg', '', '500,00', ''),
]


def _csv(rows=None):
    lines = ['ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER']
    for number, row in enumerate(CSV_ROWS if rows is None else rows, 1):
        lines.append(f'{number};' + ';'.join(row) + ';')
    return ('\n'.join(lines) + '\n').encode('cp1252')


def _csv_import(db, rows=None):
    result = db.import_wiso_csv(_csv(rows))
    assert not result['errors'], result['errors']
    return result


def _fdb_import(db, path):
    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)
    assert not result['errors'], result['errors']
    return result


def _run(db, kind, path):
    """Ein Lauf: ``csv`` oder ``db``; liefert (angelegt, übersprungen, übernommen)."""
    if kind == 'csv':
        result = _csv_import(db)
        return result['imported'], result['skipped'], None
    result = _fdb_import(db, path)
    return result['imported'], result['skipped'], result['adopted']


# ── Lesen und Schreiben der Datenbank ────────────────────────────────────────

def _query(db, sql, params=()):
    conn = db._get_connection()
    try:
        cursor = conn.execute(sql, params)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def _execute(db, sql, params=()):
    conn = db._get_connection()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _entries(db):
    """Alle Buchungssätze ab Dezember 2023 (die Testdaten liegen davor)."""
    return _query(db, "SELECT * FROM Bookings WHERE DateBooking >= '2023-12-01'"
                      " AND BookingType = 'entry' ORDER BY ID")


def _by_text(db):
    rows = _entries(db)
    by_text = {row['Text']: row for row in rows}
    assert len(by_text) == len(rows), 'Texte nicht eindeutig'
    return by_text


def _row(db, booking_id):
    return _query(db, 'SELECT * FROM Bookings WHERE ID = ?', (booking_id,))[0]


def _coa(db, number):
    return _query(db, 'SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?', (number,))[0]['ID']


def _account_id(db, name):
    return _query(db, 'SELECT ID FROM Accounts WHERE Name = ?', (name,))[0]['ID']


def _keys(db):
    """Schlüssel der Buchungssätze (Datum, Betrag, Beleg-Nr.) als sortierte Liste."""
    return sorted((row['DateBooking'], row['Amount'], row['DocumentNumber'] or '')
                  for row in _entries(db))


def _candidate(db, coa=1800, counter=4400, **overrides):
    """Eine vorhandene Buchung (CSV-Zeile oder von Hand), die zum Satz ``_record()`` passt – mit den
    Abweichungen aus ``overrides``. ``coa`` und ``counter`` sind SKR-Nummern oder ``None``."""
    values = dict(date_booking='2024-03-01', amount=1190.0, document_number='PR-001',
                  text='Probe Verkauf', tax_rate=0.19, tax_amount=190.0, booking_type='entry',
                  coa_id=_coa(db, coa) if coa else None,
                  counter_coa_id=_coa(db, counter) if counter else None)
    values.update(overrides)
    return db.insert_booking(**values)


# ── Alle Reihenfolgen ────────────────────────────────────────────────────────

#: (Bezeichnung, Läufe, Buchungen danach, je Lauf (angelegt, übersprungen, übernommen))
ORDERS = [
    ('CSV', ['csv'], 5, [(5, 0, None)]),
    ('Datenbank', ['db'], 6, [(6, 0, 0)]),
    ('CSV, CSV', ['csv', 'csv'], 5, [(5, 0, None), (0, 5, None)]),
    ('Datenbank, Datenbank', ['db', 'db'], 6, [(6, 0, 0), (0, 6, 0)]),
    ('CSV, Datenbank', ['csv', 'db'], 6, [(5, 0, None), (1, 0, 5)]),
    ('CSV, Datenbank, Datenbank', ['csv', 'db', 'db'], 6,
     [(5, 0, None), (1, 0, 5), (0, 6, 0)]),
    ('CSV, Datenbank, CSV', ['csv', 'db', 'csv'], 6, [(5, 0, None), (1, 0, 5), (0, 5, None)]),
    ('Datenbank, CSV', ['db', 'csv'], 6, [(6, 0, 0), (0, 5, None)]),
]


@pytest.mark.parametrize('name,kinds,count,expected', ORDERS, ids=[o[0] for o in ORDERS])
def test_every_order_ends_with_each_booking_once(db_with_coa, tmp_path, name, kinds, count, expected):
    path = _fdb(tmp_path)
    runs = [_run(db_with_coa, kind, path) for kind in kinds]
    assert runs == expected
    assert len(_entries(db_with_coa)) == count
    # jeder Satz genau einmal – derselbe Schlüssel unabhängig von der Reihenfolge
    all_keys = sorted([('2023-12-01', -400000, 'PR-000'), ('2024-03-01', 11900000, 'PR-001'),
                       ('2024-03-05', -2380000, 'PR-002'), ('2024-03-05', -500000, 'PR-003'),
                       ('2024-03-05', -500000, 'PR-003'), ('2024-03-15', -5000000, '')])
    expected_keys = all_keys if 'db' in kinds else [k for k in all_keys if k[2] != 'PR-000']
    assert _keys(db_with_coa) == expected_keys
    if 'db' in kinds:
        assert all(row['SourceGroup'] for row in _entries(db_with_coa))


#: dieselben Läufe, aber die CSV kennt das Konto des Satzes „ohne Beleg“ noch nicht (6999 fehlt im
#: Kontenrahmen: die Zeile bekommt kein Konto); der Datenbank-Import legt es an
ORDERS_UNKNOWN_ACCOUNT = [
    ('CSV (Konto fehlt), Datenbank', ['csv', 'db'], [(5, 0, None), (1, 0, 5)]),
    ('CSV (Konto fehlt), Datenbank, Datenbank', ['csv', 'db', 'db'],
     [(5, 0, None), (1, 0, 5), (0, 6, 0)]),
    ('CSV (Konto fehlt), Datenbank, CSV', ['csv', 'db', 'csv'],
     [(5, 0, None), (1, 0, 5), (0, 5, None)]),
]


def _unknown_account_files(tmp_path):
    records = _records()
    records[OHNE_BELEG]['ACCOUNTNO'] = 4999
    rows = [list(row) for row in CSV_ROWS]
    rows[OHNE_BELEG][1] = '6999'
    return _fdb(tmp_path, records, extra_chart=[NEW_ACCOUNT]), [tuple(row) for row in rows]


@pytest.mark.parametrize('name,kinds,expected', ORDERS_UNKNOWN_ACCOUNT,
                         ids=[o[0] for o in ORDERS_UNKNOWN_ACCOUNT])
def test_orders_with_an_account_the_csv_did_not_know(db_with_coa, tmp_path, name, kinds, expected):
    path, rows = _unknown_account_files(tmp_path)
    runs = []
    for kind in kinds:
        if kind == 'csv':
            result = _csv_import(db_with_coa, rows)
            runs.append((result['imported'], result['skipped'], None))
        else:
            runs.append(_run(db_with_coa, kind, path))
    assert runs == expected
    assert len(_entries(db_with_coa)) == 6
    row = _by_text(db_with_coa)['Probe ohne Beleg']
    assert row['COA_ID'] == _coa(db_with_coa, 6999) and row['SourceGroup'] == '704'


# ── Die Übernahme im Einzelnen ───────────────────────────────────────────────

def test_csv_dann_datenbank_verdoppelt_nicht(db_with_coa, tmp_path):
    """5 Sätze aus dem CSV-Import werden übernommen, nur der Vorjahressatz ist neu: gleiche IDs,
    ``SourceGroup`` gesetzt."""
    _csv_import(db_with_coa)
    before = _by_text(db_with_coa)
    assert len(before) == 5 and all(row['SourceGroup'] is None for row in before.values())

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['skipped'], result['adopted']) == (1, 0, 5)

    after = _by_text(db_with_coa)
    assert len(after) == 6
    for text, row in before.items():
        assert after[text]['ID'] == row['ID'], text
    assert {text: after[text]['SourceGroup'] for text in
            ('Probe Verkauf', 'Probe Buero', 'Probe ohne Beleg')} == {
        'Probe Verkauf': '700', 'Probe Buero': '701', 'Probe ohne Beleg': '704'}
    # die beiden gleich hohen Teilbuchungen: jede bekommt eine eigene Quellgruppe
    assert {after['Probe Split A']['SourceGroup'], after['Probe Split B']['SourceGroup']} == {
        '702', '703'}


def test_gleichbetragige_teilbuchungen_je_einzeln_uebernommen(db_with_coa, tmp_path):
    """Zwei CSV-Zeilen ↔ zwei Sätze: beide werden übernommen, jede genau einmal."""
    _csv_import(db_with_coa)
    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert result['adopted'] == 5
    split = [row for row in _entries(db_with_coa) if row['DocumentNumber'] == 'PR-003']
    assert len(split) == 2
    assert sorted(row['SourceGroup'] for row in split) == ['702', '703']
    assert sorted(row['Text'] for row in split) == ['Probe Split A', 'Probe Split B']


def test_eine_csv_zeile_fuer_zwei_gleich_hohe_saetze(db_with_coa, tmp_path):
    """Eine CSV-Zeile ↔ zwei Sätze: einer wird übernommen, der andere neu angelegt – die
    Zeile wird nicht doppelt beansprucht."""
    rows = [row for row in CSV_ROWS if row[3] != 'Probe Split B']
    _csv_import(db_with_coa, rows)
    assert len([r for r in _entries(db_with_coa) if r['DocumentNumber'] == 'PR-003']) == 1

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (2, 4)      # Split B und Vorjahr neu
    split = [row for row in _entries(db_with_coa) if row['DocumentNumber'] == 'PR-003']
    assert sorted(row['SourceGroup'] for row in split) == ['702', '703']
    assert sorted(row['Text'] for row in split) == ['Probe Split A', 'Probe Split B']


def test_ohne_belegnummer_wird_uebernommen(db_with_coa, tmp_path):
    """Ohne Beleg-Nr. gilt Datum + Konto + Betrag. Die CSV schreibt die fehlende Nummer als '',
    die Datenbank als NULL – die Übernahme ändert das nicht."""
    _csv_import(db_with_coa)
    row = _by_text(db_with_coa)['Probe ohne Beleg']
    assert row['DocumentNumber'] == ''

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert result['adopted'] == 5
    after = _by_text(db_with_coa)['Probe ohne Beleg']
    assert (after['ID'], after['SourceGroup'], after['DocumentNumber']) == (row['ID'], '704', '')
    assert len([r for r in _entries(db_with_coa) if r['DateBooking'] == '2024-03-15']) == 1


def test_vorjahr_ohne_csv_wird_neu_angelegt(db_with_coa, tmp_path):
    """Die CSV deckt nur die Jahre des Exports ab: der Vorjahressatz hat kein Gegenstück und
    kommt als neue Buchung dazu – die übrigen werden übernommen, nicht ergänzt."""
    _csv_import(db_with_coa)
    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (1, 5)
    row = _by_text(db_with_coa)['Probe Vorjahr']
    assert (row['SourceGroup'], row['DocumentNumber'], row['DateBooking']) == (
        '705', 'PR-000', '2023-12-01')
    assert row['ID'] > max(r['ID'] for t, r in _by_text(db_with_coa).items() if t != 'Probe Vorjahr')


def test_nur_im_csv_vorhandene_zeilen_bleiben_unberuehrt(db_with_coa, tmp_path):
    """Eine CSV-Zeile ohne Satz in der Datenbank bleibt, wie sie ist (ohne Quellgruppe)."""
    extra = ('20.03.2024', '6815', '1800', 'Probe nur im CSV', 'PR-777', '10,00', '')
    _csv_import(db_with_coa, CSV_ROWS + [extra])
    only_csv = _by_text(db_with_coa)['Probe nur im CSV']

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (1, 5)
    assert _by_text(db_with_coa)['Probe nur im CSV'] == only_csv
    assert len(_entries(db_with_coa)) == 7


def test_datenbank_dann_csv_verdoppelt_nicht(db_with_coa, tmp_path):
    """Die Gegenrichtung gilt schon vorher und bleibt: der CSV-Import überspringt, was der
    Datenbank-Import angelegt hat, und ändert daran nichts."""
    _fdb_import(db_with_coa, _fdb(tmp_path))
    before = _entries(db_with_coa)

    result = _csv_import(db_with_coa)
    assert (result['imported'], result['skipped']) == (0, 5)
    assert _entries(db_with_coa) == before


def test_csv_ohne_konto_wird_uebernommen_und_kontiert(db_with_coa, tmp_path):
    """Kannte der Kontenrahmen das Konto beim CSV-Import nicht, hat die Zeile keins. Der
    Datenbank-Import legt es an und findet die Zeile ersatzweise ohne Konto wieder (gleiches,
    bekanntes Gegenkonto), statt sie zu verdoppeln – danach ist sie kontiert."""
    path, rows = _unknown_account_files(tmp_path)
    _csv_import(db_with_coa, rows)
    before = _by_text(db_with_coa)['Probe ohne Beleg']
    assert before['COA_ID'] is None and before['CounterCOA_ID'] == _coa(db_with_coa, 1800)

    result = _fdb_import(db_with_coa, path)
    assert result['created_coa'] == {6999: 'Neu angelegt'}
    assert (result['imported'], result['adopted']) == (1, 5)

    after = _by_text(db_with_coa)['Probe ohne Beleg']
    assert after['ID'] == before['ID'] and after['SourceGroup'] == '704'
    assert after['COA_ID'] == _coa(db_with_coa, 6999)                 # gefüllt
    assert after['CounterCOA_ID'] == before['CounterCOA_ID']          # unverändert
    assert len(_entries(db_with_coa)) == 6


def test_nach_uebernahme_zweiter_lauf_ueberspringt(db_with_coa, tmp_path):
    """Nach der Übernahme erkennt der nächste Lauf alles an der Quellgruppe: nichts neu, nichts
    mehr als „übernommen“ gezählt."""
    path = _fdb(tmp_path)
    _csv_import(db_with_coa)
    _fdb_import(db_with_coa, path)
    before = _entries(db_with_coa)

    result = _fdb_import(db_with_coa, path)
    assert (result['imported'], result['skipped'], result['adopted']) == (0, 6, 0)
    assert _entries(db_with_coa) == before


def test_leeres_gegenkonto_wird_gefuellt(db_with_coa, tmp_path):
    """Fehlt der CSV-Zeile das Gegenkonto, entscheidet der Text (hier gleich); die Übernahme
    füllt das Gegenkonto aus WISO, das Konto bleibt."""
    rows = [list(row) for row in CSV_ROWS]
    rows[VERKAUF][2] = ''
    _csv_import(db_with_coa, [tuple(row) for row in rows])
    assert _by_text(db_with_coa)['Probe Verkauf']['CounterCOA_ID'] is None

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (1, 5)
    row = _by_text(db_with_coa)['Probe Verkauf']
    assert row['CounterCOA_ID'] == _coa(db_with_coa, 4400) and row['COA_ID'] == _coa(db_with_coa, 1800)
    assert row['SourceGroup'] == '700'


def test_geaendertes_gegenkonto_verhindert_die_uebernahme(db_with_coa, tmp_path):
    """Hat der Nutzer das Gegenkonto einer CSV-Zeile geändert (1810 statt 1800), passt die Zeile nicht
    mehr zum Satz aus WISO: Sie bleibt, wie sie ist, und der Satz kommt als zweite, sichtbare Buchung
    dazu – statt dass die Zeile still dem falschen Satz zugeordnet wird."""
    _csv_import(db_with_coa)
    other_bank = _coa(db_with_coa, 1810)
    _execute(db_with_coa, "UPDATE Bookings SET CounterCOA_ID = ? WHERE Text = 'Probe Buero'",
             (other_bank,))
    user_row = _by_text(db_with_coa)['Probe Buero']

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (2, 4)         # Büro und Vorjahr neu
    assert _row(db_with_coa, user_row['ID']) == user_row             # unberührt, keine Quellgruppe
    buero = [r for r in _entries(db_with_coa) if r['DocumentNumber'] == 'PR-002']
    assert len(buero) == 2
    assert sorted((r['SourceGroup'] or '', r['CounterCOA_ID']) for r in buero) == sorted([
        ('', other_bank), ('701', _coa(db_with_coa, 1800))])


def test_uebernahme_behaelt_text_und_verknuepfung(db_with_coa, tmp_path):
    """Text, Beleg-Nr., Steuer, Bankverknüpfung, Konto, Kategorie, Kontakt und Empfänger der
    CSV-Zeilen bleiben, wie der Nutzer sie hinterlassen hat – nur die Quellgruppe kommt dazu. Eine von
    der Abgleichlogik mit einer Bankbewegung verknüpfte Zeile wird weiter übernommen."""
    _csv_import(db_with_coa)
    db = db_with_coa
    bank_id = _account_id(db, 'Testbank')
    bank_booking = db.insert_booking('2024-03-05', -238.0, account_id=bank_id,
                                     text='Überweisung Papierhandel', booking_type='bank')
    db.link_bank_to_entries()
    linked = _by_text(db)['Probe Buero']
    assert linked['ParentBooking_ID'] == bank_booking                           # Vorbedingung
    assert linked['AutoMirror'] == 0 and linked['Account_ID'] is None           # …ohne Maske

    _execute(db, "INSERT INTO Categories (Name, Text) VALUES ('Privat', 'Test')")
    category = _query(db, "SELECT ID FROM Categories WHERE Name = 'Privat'")[0]['ID']
    contact = _query(db, 'SELECT ID FROM Contacts ORDER BY ID LIMIT 1')[0]['ID']
    _execute(db, "UPDATE Bookings SET Text = 'Verkauf, vom Nutzer ergänzt', Contact_ID = ?,"
                 " Category_ID = ?, RecipientClient = 'Kundin', Status = 'geprüft',"
                 " DateTax = '2024-03-31' WHERE Text = 'Probe Verkauf'", (contact, category))
    before = {row['ID']: row for row in _entries(db)}

    result = _fdb_import(db, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (1, 5)

    after = {row['ID']: row for row in _entries(db)}
    for entry_id, row in before.items():
        assert after[entry_id] == {**row, 'SourceGroup': after[entry_id]['SourceGroup']}, row['Text']
        assert after[entry_id]['SourceGroup'], row['Text']              # überall gesetzt
    changed = [r for r in after.values() if r['Text'] == 'Verkauf, vom Nutzer ergänzt'][0]
    assert (changed['Contact_ID'], changed['Category_ID'], changed['RecipientClient']) == (
        contact, category, 'Kundin')
    assert after[linked['ID']]['ParentBooking_ID'] == bank_booking
    # die Bankbewegung selbst blieb unangetastet
    assert _row(db, bank_booking)['SourceGroup'] is None


# ── Gegenkonto und Text ──────────────────────────────────────────────────────

#: Kandidat (Abweichungen von ``_candidate``) und ob der Satz ``_record()`` ihn übernimmt. Satz:
#: Bank an Erlöse, Text „Probe Verkauf“.
COUNTER_CASES = [
    ('Gegenkonto gleich, Text anders', dict(text='ganz anderer Text'), True),
    ('Gegenkonto leer, Text gleich', dict(counter=None), True),
    ('Gegenkonto leer, Text gleich bis auf Leerraum und Schreibweise',
     dict(counter=None, text='  PROBE   verkauf '), True),
    ('Gegenkonto leer, Text anders', dict(counter=None, text='Verkauf Kunde A'), False),
    ('Gegenkonto leer, Text leer', dict(counter=None, text=''), False),
    ('anderes Gegenkonto, Text gleich', dict(counter=1810), False),
]


@pytest.mark.parametrize('name,overrides,adopted', COUNTER_CASES, ids=[c[0] for c in COUNTER_CASES])
def test_gegenkonto_oder_text_muss_passen(db_with_coa, tmp_path, name, overrides, adopted):
    """Beide kennen das Gegenkonto → es muss gleich sein; kennt es der Kandidat nicht → der Text
    muss übereinstimmen. Sonst bleibt der Kandidat unberührt und der Satz kommt neu dazu."""
    booking = _candidate(db_with_coa, **overrides)
    before = _row(db_with_coa, booking)

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    after = _row(db_with_coa, booking)
    if adopted:
        assert (result['imported'], result['adopted']) == (0, 1)
        assert after['SourceGroup'] == '900'
        assert after['CounterCOA_ID'] == _coa(db_with_coa, 4400)             # gefüllt oder gleich
        assert len(_entries(db_with_coa)) == 1
    else:
        assert (result['imported'], result['adopted']) == (1, 0)
        assert after == before
        assert len(_entries(db_with_coa)) == 2


def test_kandidat_mit_gegenkonto_passt_nicht_zu_einem_satz_ohne(db_with_coa, tmp_path):
    """Kennt nur der Kandidat das Gegenkonto, passt es nicht – auch bei gleichem Text."""
    booking = _candidate(db_with_coa)
    before = _row(db_with_coa, booking)
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record(CONTRA_ACCOUNTNO=None)]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert _row(db_with_coa, booking) == before


def test_ohne_gegenkonto_auf_beiden_seiten_entscheidet_der_text(db_with_coa, tmp_path):
    same = _candidate(db_with_coa, counter=None)
    other = _candidate(db_with_coa, counter=None, text='Probe etwas anderes')
    before_other = _row(db_with_coa, other)
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record(CONTRA_ACCOUNTNO=None)]))
    assert (result['imported'], result['adopted']) == (0, 1)
    assert _row(db_with_coa, same)['SourceGroup'] == '900'
    assert _row(db_with_coa, other) == before_other


# ── Mehrere Kandidaten mit gleichem Schlüssel ────────────────────────────────

def test_zahlungen_zweier_kunden_werden_nach_text_zugeordnet(db_with_coa, tmp_path):
    """Zwei CSV-Zeilen „Bank an (leer) 119,00“ am selben Tag ohne Beleg-Nr., Zahlung Kunde A und B.
    Die Datenbank liefert B vor A, mit den Personenkonten 10002 und 10001: Zeile A bekommt 10001 und
    die Gruppe von A, Zeile B 10002 und die Gruppe von B – nicht „die erste Zeile dem ersten Satz“."""
    rows = [('12.03.2024', '1810', '', 'Zahlung Kunde A', '', '119,00', ''),
            ('12.03.2024', '1810', '', 'Zahlung Kunde B', '', '119,00', '')]
    _csv_import(db_with_coa, rows)
    csv_a, csv_b = [_by_text(db_with_coa)[f'Zahlung Kunde {k}'] for k in 'AB']
    assert csv_a['ID'] < csv_b['ID']

    def payment(number, group, counter, name):
        return dict(ID=number, ACCOUNTINGID=group, ACCOUNTING_DATE='2024-03-12 00:00:00',
                    AMOUNTGROSS=119.0, ACCOUNTNO=1210, CONTRA_ACCOUNTNO=counter,
                    ACCOUNTING_TEXT=f'Zahlung Kunde {name}')

    records = [payment(1, 802, 10002, 'B'), payment(2, 801, 10001, 'A')]       # B vor A
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))
    assert (result['imported'], result['adopted']) == (0, 2)

    row_a, row_b = _row(db_with_coa, csv_a['ID']), _row(db_with_coa, csv_b['ID'])
    assert (row_a['SourceGroup'], row_a['CounterCOA_ID']) == ('801', _coa(db_with_coa, 10001))
    assert (row_b['SourceGroup'], row_b['CounterCOA_ID']) == ('802', _coa(db_with_coa, 10002))
    assert len(_entries(db_with_coa)) == 2


def test_gleiches_gegenkonto_entscheidet_zwischen_zeilen_mit_gleichem_text(db_with_coa, tmp_path):
    """Zwei Zeilen mit gleichem Schlüssel und gleichem Text, aber verschiedenem Gegenkonto: jeder Satz
    nimmt die Zeile mit seinem Gegenkonto – auch wenn die andere die kleinere ID hat."""
    rows = [('12.03.2024', '6815', '1800', 'Probe Kopie', '', '50,00', ''),
            ('12.03.2024', '6815', '1810', 'Probe Kopie', '', '50,00', '')]
    _csv_import(db_with_coa, rows)
    csv_1800, csv_1810 = [r for r in _entries(db_with_coa)]
    assert (csv_1800['CounterCOA_ID'], csv_1810['CounterCOA_ID']) == (
        _coa(db_with_coa, 1800), _coa(db_with_coa, 1810))

    def copy(number, group, counter):
        return dict(ID=number, ACCOUNTINGID=group, ACCOUNTING_DATE='2024-03-12 00:00:00',
                    AMOUNTGROSS=50.0, ACCOUNTNO=4930, CONTRA_ACCOUNTNO=counter,
                    ACCOUNTING_TEXT='Probe Kopie')

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [copy(1, 811, 1210), copy(2, 812, 1200)]))
    assert (result['imported'], result['adopted']) == (0, 2)
    assert _row(db_with_coa, csv_1810['ID'])['SourceGroup'] == '811'     # Satz mit 1810
    assert _row(db_with_coa, csv_1800['ID'])['SourceGroup'] == '812'     # Satz mit 1800


#: Kandidaten in ID-Reihenfolge (Abweichungen von ``_candidate``) und der Index dessen, der gewinnt
PICK_CASES = [
    ('gleiches Gegenkonto schlägt gleichen Text',
     [dict(counter=None), dict(text='anders')], 1),
    ('erster Kandidat passt nicht, zweiter schon',
     [dict(counter=None, text='anders'), dict(counter=None)], 1),
    ('anderes Gegenkonto vorn, gleiches dahinter', [dict(counter=1810), dict()], 1),
    ('beide passen: der mit gleichem Gegenkonto', [dict(counter=None), dict(counter=4400)], 1),
    ('anderes Gegenkonto und leeres mit gleichem Text', [dict(counter=1810), dict(counter=None)], 1),
    ('gleiches Gegenkonto, darunter der gleiche Text', [dict(text='anders'), dict()], 1),
    ('gleiches Gegenkonto und gleicher Text vorn bleibt vorn', [dict(), dict(text='anders')], 0),
]


@pytest.mark.parametrize('name,candidates,winner', PICK_CASES, ids=[c[0] for c in PICK_CASES])
def test_unter_mehreren_kandidaten_gewinnt_der_passende(db_with_coa, tmp_path, name, candidates, winner):
    """Gegenkonto vor Text, und nie einfach der erste nach ID."""
    ids = [_candidate(db_with_coa, **overrides) for overrides in candidates]
    before = {i: _row(db_with_coa, i) for i in ids}

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    assert (result['imported'], result['adopted']) == (0, 1)
    for index, booking in enumerate(ids):
        if index == winner:
            assert _row(db_with_coa, booking)['SourceGroup'] == '900'
        else:
            assert _row(db_with_coa, booking) == before[booking]


def test_bei_gleichstand_gewinnt_die_kleinste_id(db_with_coa, tmp_path):
    """Passen mehrere Kandidaten gleich gut (hier vollkommen gleich), nimmt der Satz die mit der
    kleinsten ID – und nur eine: die andere bleibt für den nächsten Satz."""
    first, second = _candidate(db_with_coa), _candidate(db_with_coa)
    before_second = _row(db_with_coa, second)
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    assert (result['imported'], result['adopted']) == (0, 1)
    assert _row(db_with_coa, first)['SourceGroup'] == '900'
    assert _row(db_with_coa, second) == before_second


# ── Ersatzsuche ohne Konto ───────────────────────────────────────────────────

def test_offene_buchung_ohne_konto_und_gegenkonto_wird_nicht_uebernommen(db_with_coa, tmp_path):
    """Eine manuelle Buchung „noch offen: Parkhaus“ (−25,00 €, kein Konto, kein Gegenkonto) ist nicht
    der WISO-Satz „6815 an 1810 Briefmarken 25,00“ – nur weil Datum und Betrag stimmen. Der Satz wird
    neu angelegt, die offene Buchung bleibt, wie sie ist."""
    parkhaus = db_with_coa.insert_booking('2024-03-12', -25.0, text='noch offen: Parkhaus',
                                          booking_type='entry')
    before = _row(db_with_coa, parkhaus)
    briefmarken = dict(ID=1, ACCOUNTINGID=821, ACCOUNTING_DATE='2024-03-12 00:00:00',
                       AMOUNTGROSS=25.0, ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210,
                       ACCOUNTING_TEXT='Briefmarken')
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [briefmarken]))

    assert (result['imported'], result['adopted']) == (1, 0)
    assert _row(db_with_coa, parkhaus) == before
    assert before['COA_ID'] is None and before['SourceGroup'] is None
    assert sorted(r['Text'] for r in _entries(db_with_coa)) == [
        'Briefmarken', 'noch offen: Parkhaus']


def test_ersatzsuche_ohne_konto_braucht_das_gleiche_gegenkonto(db_with_coa, tmp_path):
    """Ohne eigenes Konto findet die Zeile ihren Satz nur über ein gleiches, bekanntes Gegenkonto."""
    same = _candidate(db_with_coa, coa=None, counter=1810, text='noch offen', document_number=None,
                      amount=-25.0, date_booking='2024-03-12', tax_rate=None, tax_amount=None)
    other = _candidate(db_with_coa, coa=None, counter=1800, text='noch offen', document_number=None,
                       amount=-25.0, date_booking='2024-03-12', tax_rate=None, tax_amount=None)
    unknown = _candidate(db_with_coa, coa=None, counter=None, text='Briefmarken',
                         document_number=None, amount=-25.0, date_booking='2024-03-12',
                         tax_rate=None, tax_amount=None)
    before = {i: _row(db_with_coa, i) for i in (other, unknown)}
    briefmarken = dict(ID=1, ACCOUNTINGID=821, ACCOUNTING_DATE='2024-03-12 00:00:00',
                       AMOUNTGROSS=25.0, ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210,
                       ACCOUNTING_TEXT='Briefmarken')
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [briefmarken]))

    assert (result['imported'], result['adopted']) == (0, 1)
    row = _row(db_with_coa, same)
    assert (row['SourceGroup'], row['COA_ID'], row['CounterCOA_ID']) == (
        '821', _coa(db_with_coa, 6815), _coa(db_with_coa, 1810))           # kontiert
    assert all(_row(db_with_coa, i) == before[i] for i in (other, unknown))


def test_ersatzsuche_gilt_nur_ohne_kandidaten_mit_konto_von_anfang_an(db_with_coa, tmp_path):
    """Gab es für den Schlüssel von Anfang an eine Buchung mit Konto, nimmt der zweite Satz nicht
    ersatzweise eine ohne Konto – auch nicht bei gleichem Gegenkonto. Er kommt neu dazu."""
    fields = dict(counter=1810, document_number=None, amount=-25.0, date_booking='2024-03-12',
                  tax_rate=None, tax_amount=None)
    with_account = _candidate(db_with_coa, coa=6815, text='mit Konto', **fields)
    without_account = _candidate(db_with_coa, coa=None, text='ohne Konto', **fields)
    before = _row(db_with_coa, without_account)
    records = [dict(ID=number, ACCOUNTINGID=830 + number, ACCOUNTING_DATE='2024-03-12 00:00:00',
                    AMOUNTGROSS=25.0, ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210,
                    ACCOUNTING_TEXT='mit Konto') for number in (1, 2)]
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))

    assert (result['imported'], result['adopted']) == (1, 1)
    assert _row(db_with_coa, with_account)['SourceGroup'] in ('831', '832')
    assert _row(db_with_coa, without_account) == before
    assert len(_entries(db_with_coa)) == 3


def test_ersatzsuche_gilt_auch_nicht_wenn_der_kandidat_mit_konto_nicht_passt(db_with_coa, tmp_path):
    """„Kandidat mit Konto“ heißt: eine Buchung mit gleichem Schlüssel (Datum, Betrag, Konto, Beleg-Nr.)
    gab es – auch wenn sie wegen ihres anderen Gegenkontos nicht übernommen wird. Dann sucht der
    Import nicht noch ersatzweise ohne Konto weiter, sondern legt den Satz neu an."""
    fields = dict(document_number=None, amount=-25.0, date_booking='2024-03-12', tax_rate=None,
                  tax_amount=None)
    other_counter = _candidate(db_with_coa, coa=6815, counter=1800, text='mit Konto', **fields)
    without_account = _candidate(db_with_coa, coa=None, counter=1810, text='ohne Konto', **fields)
    before = {i: _row(db_with_coa, i) for i in (other_counter, without_account)}
    record = dict(ID=1, ACCOUNTINGID=851, ACCOUNTING_DATE='2024-03-12 00:00:00', AMOUNTGROSS=25.0,
                  ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210, ACCOUNTING_TEXT='mit Konto')

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [record]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert {i: _row(db_with_coa, i) for i in before} == before
    assert len(_entries(db_with_coa)) == 3


# ── Was nie übernommen wird ──────────────────────────────────────────────────

def test_buchung_mit_fremder_quellgruppe_wird_nicht_uebernommen(db_with_coa, tmp_path):
    """Eine Buchung mit eigener Quellgruppe gehört schon zu einem anderen Satz."""
    foreign = _candidate(db_with_coa)
    _execute(db_with_coa, "UPDATE Bookings SET SourceGroup = 'X1' WHERE ID = ?", (foreign,))
    before = _row(db_with_coa, foreign)

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (6, 0)
    assert _row(db_with_coa, foreign) == before
    assert len(_entries(db_with_coa)) == 7


def test_bankbewegung_wird_nie_uebernommen(db_with_coa, tmp_path):
    """Eine Bankbewegung mit gleichem Datum, Betrag, Beleg-Nr. und Gegenkonto ist kein Buchungssatz –
    auch nicht ohne Konto und ohne ihr Bankkonto (das schon allein sie ausschlösse)."""
    bank_id = _account_id(db_with_coa, 'Testbank')
    on_account = _candidate(db_with_coa, account_id=bank_id, booking_type='bank')
    no_account = _candidate(db_with_coa, booking_type='bank')
    no_coa = _candidate(db_with_coa, coa=None, counter=None, booking_type='bank')
    ids = (on_account, no_account, no_coa)
    before = [_row(db_with_coa, i) for i in ids]

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (6, 0)
    assert [_row(db_with_coa, i) for i in ids] == before
    assert len(_entries(db_with_coa)) == 6


def test_buchung_der_maske_mit_bankkonto_wird_nicht_uebernommen(db_with_coa, tmp_path):
    """Ein eigenständiger Buchungssatz bekommt in der Maske das vorgewählte Bankkonto – er ist eine
    eigene Erfassung des Nutzers und wird nicht mit einem WISO-Satz gleichgesetzt."""
    bank_id = _account_id(db_with_coa, 'Testbank')
    mask = _candidate(db_with_coa, account_id=bank_id)
    before = _row(db_with_coa, mask)

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert _row(db_with_coa, mask) == before
    assert len(_entries(db_with_coa)) == 2


def test_automirror_kind_der_maske_wird_nicht_uebernommen(db_with_coa, tmp_path):
    """Das Spiegelkind einer Bankbewegung aus der Maske („Druckerpapier −50“, 6815 an 1810) gehört zu
    dieser Bankbewegung; der WISO-Satz „Toner −50“ mit denselben Konten ist ein anderer Vorgang. Beide
    existieren danach."""
    bank_id = _account_id(db_with_coa, 'Testkonto 2')                  # SKR 1810
    bank = db_with_coa.insert_booking('2024-03-12', -50.0, account_id=bank_id,
                                      text='Druckerpapier', booking_type='bank')
    mirror = db_with_coa.insert_booking(
        '2024-03-12', -50.0, coa_id=_coa(db_with_coa, 6815), counter_coa_id=_coa(db_with_coa, 1810),
        text='Druckerpapier', booking_type='entry', parent_booking_id=bank, auto_mirror=True)
    before = _row(db_with_coa, mirror)
    assert before['AutoMirror'] == 1 and before['Account_ID'] is None
    toner = dict(ID=1, ACCOUNTINGID=841, ACCOUNTING_DATE='2024-03-12 00:00:00', AMOUNTGROSS=50.0,
                 ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210, ACCOUNTING_TEXT='Toner')

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [toner]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert _row(db_with_coa, mirror) == before
    assert sorted(r['Text'] for r in _entries(db_with_coa)) == ['Druckerpapier', 'Toner']


def test_automirror_null_gilt_als_nicht_gespiegelt(db_with_coa, tmp_path):
    """Nur ein gesetztes ``AutoMirror`` schließt eine Buchung aus – NULL (ältere Bestände) zählt wie 0."""
    booking = _candidate(db_with_coa)
    _execute(db_with_coa, 'UPDATE Bookings SET AutoMirror = NULL WHERE ID = ?', (booking,))
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    assert (result['imported'], result['adopted']) == (0, 1)
    assert _row(db_with_coa, booking)['SourceGroup'] == '900'


@pytest.mark.parametrize('abweichung', [
    {'date_booking': '2024-03-02'},               # anderes Datum
    {'amount': 1189.0},                           # anderer Betrag
    {'amount': -1190.0},                          # Gegenbuchung: anderes Vorzeichen
    {'coa': 6815},                                # anderes Konto (nicht leer)
    {'document_number': 'PR-999'},                # andere Beleg-Nr.
    {'document_number': ''},                      # Beleg-Nr. fehlt in der CSV-Zeile
    {'document_number': None},
    {'document_number': 'PR 001'},                # nur Leerraum zusammenfassen, nicht Zeichen ändern
], ids=['Datum', 'Betrag', 'Vorzeichen', 'Konto', 'Beleg-Nr.', 'Beleg-Nr. leer', 'Beleg-Nr. NULL',
        'Beleg-Nr. anders geschrieben'])
def test_buchung_mit_anderem_schluessel_wird_nicht_uebernommen(db_with_coa, tmp_path, abweichung):
    """Datum, Betrag, Konto und Beleg-Nr. gehören alle zum Schlüssel: weicht eines ab, bleibt die
    Buchung, wie sie ist, und der Satz aus der Datenbank kommt neu dazu."""
    manual = _candidate(db_with_coa, **abweichung)
    before = _row(db_with_coa, manual)

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert _row(db_with_coa, manual) == before
    assert len(_entries(db_with_coa)) == 2


@pytest.mark.parametrize('number', ['PR  001', ' PR-001 ', 'PR-001\t', '\tPR-001'],
                         ids=['doppeltes Leerzeichen', 'Rand', 'Tabulator hinten', 'Tabulator vorn'])
def test_leerraum_in_der_belegnummer_zaehlt_nicht(db_with_coa, tmp_path, number):
    """„RE  12“ und „RE 12“ sind dieselbe Beleg-Nr.: Leerraum wird zusammengefasst."""
    record = _record(REFERENCENO='PR 001' if ' ' in number.strip() else 'PR-001')
    manual = _candidate(db_with_coa, document_number=number)
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [record]))
    assert (result['imported'], result['adopted']) == (0, 1)
    row = _row(db_with_coa, manual)
    assert row['SourceGroup'] == '900' and row['DocumentNumber'] == number      # Wert bleibt


def test_manuelle_buchung_mit_gleichem_schluessel_wird_uebernommen(db_with_coa, tmp_path):
    """Dokumentiertes Restrisiko: Eine von Hand erfasste Buchung mit gleicher Beleg-Nr., Datum,
    Konto, Betrag UND Gegenkonto gilt als dieselbe Buchung (die Annahme trifft der CSV-Import schon).
    Sie bekommt die Quellgruppe, behält ihren Text und wird nicht doppelt angelegt."""
    manual = _candidate(db_with_coa, date_booking='2024-03-05', amount=-238.0, coa=6815,
                        counter=1800, document_number='PR-002', text='von Hand erfasst',
                        tax_rate=None, tax_amount=None)
    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (5, 1)

    row = _row(db_with_coa, manual)
    assert (row['SourceGroup'], row['Text']) == ('701', 'von Hand erfasst')
    assert len([r for r in _entries(db_with_coa) if r['DocumentNumber'] == 'PR-002']) == 1


# ── Felder, die die Übernahme ergänzt – nur wo sie leer sind ────────────────

def test_steuer_wird_nur_ergaenzt_wenn_beides_leer_ist(db_with_coa, tmp_path):
    """``TaxRate`` und ``TaxAmount`` kommen aus WISO, wenn die Zeile beide nicht kennt. Steht schon
    ein Satz oder ein Betrag da, bleibt alles, wie es ist (auch ein anderer Satz als in WISO)."""
    rows = [list(row) for row in CSV_ROWS[:3]]
    rows[VERKAUF][6] = ''                              # kein Steuerschlüssel: beide leer
    rows[BUERO][6] = '402'                             # 7 % statt der 19 % aus WISO
    rows[SPLIT_A][6] = '401'                           # 19 % und der Steuerbetrag dazu
    _csv_import(db_with_coa, [tuple(row) for row in rows])
    before = _by_text(db_with_coa)
    assert (before['Probe Verkauf']['TaxRate'], before['Probe Verkauf']['TaxAmount']) == (None, None)
    assert before['Probe Buero']['TaxRate'] == pytest.approx(0.07)
    # nur der Satz fehlt, der Betrag steht noch: „beide leer“ gilt nicht
    _execute(db_with_coa, "UPDATE Bookings SET TaxRate = NULL WHERE Text = 'Probe Split A'")
    before['Probe Split A'] = _by_text(db_with_coa)['Probe Split A']
    assert before['Probe Split A']['TaxAmount'] is not None

    records = _records()[:3]
    records[SPLIT_A].update(TAXRATE=19.0, AMOUNTNET=42.02)
    _fdb_import(db_with_coa, _fdb(tmp_path, records))

    after = _by_text(db_with_coa)
    assert after['Probe Verkauf']['TaxRate'] == pytest.approx(0.19)
    assert after['Probe Verkauf']['TaxAmount'] == 1900000                      # 190,00 €
    for text in ('Probe Buero', 'Probe Split A'):
        assert (after[text]['TaxRate'], after[text]['TaxAmount']) == (
            before[text]['TaxRate'], before[text]['TaxAmount']), text
    assert {t: after[t]['SourceGroup'] for t in after} == {
        'Probe Verkauf': '700', 'Probe Buero': '701', 'Probe Split A': '702'}


def test_empfaenger_wird_ergaenzt_wenn_leer_und_sonst_behalten(db_with_coa, tmp_path):
    """Rechnungsbezogene Sätze kennen den Kunden als Empfänger. Die Übernahme trägt ihn nach, wo die
    Zeile keinen hat (NULL oder leerer Text) – ein vom Nutzer eingetragener Empfänger bleibt."""
    text = 'Zahlung zu Re. W-2024001'
    path = _zahlungs_fdb(tmp_path, [(500 + n, 1210, 10000, 1190.0, text) for n in range(3)])
    fields = dict(coa=1810, counter=None, date_booking='2024-03-10', document_number=None,
                  text=text, tax_rate=None, tax_amount=None)
    null_recipient = _candidate(db_with_coa, recipient_client='', **fields)
    _execute(db_with_coa, 'UPDATE Bookings SET RecipientClient = NULL WHERE ID = ?',
             (null_recipient,))
    empty_recipient = _candidate(db_with_coa, recipient_client='', **fields)
    user_recipient = _candidate(db_with_coa, recipient_client='Kundin', **fields)
    assert _row(db_with_coa, null_recipient)['RecipientClient'] is None
    assert _row(db_with_coa, empty_recipient)['RecipientClient'] == ''

    result = db_with_coa.import_wiso_fdb(path)
    assert not result['errors'] and (result['imported'], result['adopted']) == (0, 3)
    assert _row(db_with_coa, null_recipient)['RecipientClient'] == 'Beispiel GmbH'
    assert _row(db_with_coa, empty_recipient)['RecipientClient'] == 'Beispiel GmbH'
    assert _row(db_with_coa, user_recipient)['RecipientClient'] == 'Kundin'


# ── Sätze ohne Quellgruppe ───────────────────────────────────────────────────

def _groupless_records():
    """Zwei Sätze ohne ACCOUNTINGID (Quellgruppe) mit demselben Schlüssel."""
    return [dict(ID=number, ACCOUNTING_DATE='2024-05-01 00:00:00', AMOUNTGROSS=10.0,
                 ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200, ACCOUNTING_TEXT=f'Probe ohne Gruppe {number}',
                 REFERENCENO='PR-900') for number in (1, 2)]


def test_gleiche_saetze_ohne_quellgruppe_im_selben_lauf_bleiben_beide(db_with_coa, tmp_path):
    """Die eigenen Einfügungen eines Laufs sind nie Kandidaten: zwei gleiche Sätze ohne
    Quellgruppe ergeben zwei Buchungen, nicht eine."""
    result = _fdb_import(db_with_coa, _fdb(tmp_path, _groupless_records()))
    assert (result['imported'], result['adopted']) == (2, 0)
    assert len(_entries(db_with_coa)) == 2


def test_zweiter_lauf_ohne_quellgruppe_verdoppelt_nicht(db_with_coa, tmp_path):
    """Ohne Quellgruppe greift nur der Schlüssel – ein zweiter Lauf legt trotzdem nichts an."""
    path = _fdb(tmp_path, _groupless_records())
    _fdb_import(db_with_coa, path)
    result = _fdb_import(db_with_coa, path)
    assert result['imported'] == 0
    assert len(_entries(db_with_coa)) == 2


# ── Vorzeichen: beide Wege speichern denselben Betrag ───────────────────────

#: (Bezeichnung, Konto der Kasse in den Konten, SKR-Konto, SKR-Gegenkonto, SKR03-Konto/-Gegenkonto
#: in WISO, Betrag, erwartetes Vorzeichen). Der Schlüssel enthält den Betrag mit Vorzeichen: Läge
#: der eine Weg anders, griffe die Übernahme nicht und die Buchung entstünde doppelt.
SIGNS = [
    ('Bank, Ausgabe', 1600, 6815, 1800, 4930, 1200, 25.0, -1),
    ('Bank, Einnahme', 1600, 1800, 4400, 1200, 8400, 25.0, +1),
    ('Kasse 1600, Ausgabe', 1600, 6815, 1600, 4930, 1000, 25.0, -1),
    ('Kasse 1600, Einnahme', 1600, 1600, 4400, 1000, 8400, 25.0, +1),
    ('Kasse ohne SKR, Ausgabe', None, 6815, 1600, 4930, 1000, 25.0, +1),
    ('Kasse ohne SKR, Einnahme', None, 1600, 4400, 1000, 8400, 25.0, +1),
    ('Verrechnungskonto', 1600, 6815, 1460, 4930, 1360, 25.0, -1),
]


@pytest.mark.parametrize('first', ['csv', 'db'])
@pytest.mark.parametrize('name,cash_skr,konto,gegen,konto03,gegen03,amount,sign', SIGNS,
                         ids=[s[0] for s in SIGNS])
def test_vorzeichen_beider_wege_stimmen_ueberein(db_with_coa, tmp_path, first, name, cash_skr,
                                                 konto, gegen, konto03, gegen03, amount, sign):
    """Dieselbe Buchung über CSV und Datenbank ergibt denselben Betrag – auch für die Kasse (mit
    SKR 1600 und ohne SKR-Nummer) und das Verrechnungskonto; so greift die Übernahme in jeder
    Reihenfolge."""
    if cash_skr is None:
        _execute(db_with_coa, 'UPDATE Accounts SET SKRAccount = NULL WHERE IsCash = 1 AND Name = ?',
                 ('Kasse',))
    record = dict(ID=1, ACCOUNTINGID=800, ACCOUNTING_DATE='2024-06-03 00:00:00',
                  AMOUNTGROSS=amount, ACCOUNTNO=konto03, CONTRA_ACCOUNTNO=gegen03,
                  ACCOUNTING_TEXT='Probe Vorzeichen', REFERENCENO='PR-800')
    path = _fdb(tmp_path, [record])
    row = ('03.06.2024', str(konto), str(gegen), 'Probe Vorzeichen', 'PR-800',
           f'{amount:.2f}'.replace('.', ','), '')

    for kind in (first, 'db' if first == 'csv' else 'csv'):
        if kind == 'csv':
            _csv_import(db_with_coa, [row])
        else:
            _fdb_import(db_with_coa, path)

    (booking,) = _entries(db_with_coa)                      # genau eine Buchung, keine zweite
    assert booking['Amount'] == sign * 250000
    assert booking['SourceGroup'] == '800'


# ── Rechnungszahlungen und Anlagen nach einer Übernahme ─────────────────────

def _csv_payment():
    return [('10.03.2024', '1810', '', 'Zahlung zu Re. W-2024001', '', '1190,00', '')]


def test_rechnungszahlung_findet_die_uebernommene_buchung(db_with_coa, tmp_path):
    """``link_wiso_invoice_payments`` sucht die Zahlung über die Quellgruppe – die übernommene
    CSV-Zeile (ohne Gegenkonto, gleicher Text) trägt sie jetzt und wird verknüpft; die Rechnung ist
    bezahlt."""
    path = _zahlungs_fdb(tmp_path, [(500, 1210, 10000, 1190.0, 'Zahlung zu Re. W-2024001')])
    _csv_import(db_with_coa, _csv_payment())
    csv_row = _query(db_with_coa, "SELECT * FROM Bookings WHERE Text LIKE 'Zahlung zu Re.%'")[0]

    result = db_with_coa.import_wiso_fdb(path)
    assert (result['imported'], result['adopted'], result['invoices']) == (0, 1, 1)
    assert not result['errors']
    linked = db_with_coa.link_wiso_invoice_payments(path)
    assert linked['payments_linked'] == 1 and not linked['payment_warnings']

    payments, open_amount = _payments(db_with_coa)
    assert payments == [(11900000, 'entry')] and open_amount == 0
    paid = _query(db_with_coa, 'SELECT BookingID FROM InvoicePayments')
    assert [row['BookingID'] for row in paid] == [csv_row['ID']]       # genau die CSV-Zeile
    assert _row(db_with_coa, csv_row['ID'])['SourceGroup'] == '500'

    again = db_with_coa.link_wiso_invoice_payments(path)               # und nicht doppelt
    assert again['payments_linked'] == 0
    assert len(_payments(db_with_coa)[0]) == 1


def test_rechnungszahlung_haengt_an_der_bankbewegung_der_csv_zeile(db_with_coa, tmp_path):
    """Hing die CSV-Zeile schon an einer Bankbewegung, bleibt das so – und die Zahlung der
    Rechnung wird mit der Bankbewegung verknüpft (sie steht im Kontoauszug)."""
    path = _zahlungs_fdb(tmp_path, [(500, 1210, 10000, 1190.0, 'Zahlung zu Re. W-2024001')])
    _csv_import(db_with_coa, _csv_payment())
    account_id = _account_id(db_with_coa, 'Testkonto 2')            # SKR 1810
    bank_id = db_with_coa.insert_booking('2024-03-10', 1190.0, account_id=account_id,
                                         text='Ueberweisung', booking_type='bank')
    db_with_coa.link_bank_to_entries()
    csv_row = _query(db_with_coa, "SELECT * FROM Bookings WHERE Text LIKE 'Zahlung zu Re.%'"
                                  " AND BookingType = 'entry'")[0]
    assert csv_row['ParentBooking_ID'] == bank_id                    # Vorbedingung

    db_with_coa.import_wiso_fdb(path)
    db_with_coa.link_bank_to_entries()
    assert db_with_coa.link_wiso_invoice_payments(path)['payments_linked'] == 1

    assert _row(db_with_coa, csv_row['ID'])['ParentBooking_ID'] == bank_id
    payments, open_amount = _payments(db_with_coa)
    assert payments == [(11900000, 'bank')] and open_amount == 0


def _asset_fdb(tmp_path):
    """Datenbank mit dem Satz „Verkauf“ und einem Anlagegut samt AfA."""
    builder = FdbBuilder(page_size=16384)
    chart = builder.table('BAS_FINACC_PLAN', [
        Column('ID', LONG, 4), Column('SKR04', LONG, 4),
        Column('BOOKINGYEAR', SHORT, 2), Column('ACCOUNTTEXT', TEXT, 40)])
    for skr03, skr04, text in CHART + ((320, 520, 'Pkw'), (4832, 6222, 'Abschreibungen auf Kfz')):
        chart.add(ID=skr03, SKR04=skr04, BOOKINGYEAR=2024, ACCOUNTTEXT=text)
    table = builder.table('MOV_FINACC_ACCRECORDS', [
        Column('ID', LONG, 4), Column('ACCOUNTINGID', LONG, 4),
        Column('ACCOUNTING_DATE', TIMESTAMP, 8), Column('AMOUNTNET', INT64, 8, scale=-2),
        Column('AMOUNTGROSS', INT64, 8, scale=-2), Column('TAXRATE', INT64, 8, scale=-2),
        Column('ACCOUNTNO', LONG, 4), Column('CONTRA_ACCOUNTNO', LONG, 4),
        Column('ACCOUNTING_TEXT', TEXT, 60), Column('REFERENCENO', TEXT, 20)])
    table.add(**_records()[VERKAUF])
    builder.table('BAS_INVENTORY', [
        Column('ID', LONG, 4), Column('INVNO', LONG, 4), Column('LABEL', TEXT, 40),
        Column('PURCHASEDATE', TIMESTAMP, 8), Column('PURCHASEAMOUNTNET', INT64, 8, scale=-2),
        Column('SALEDATE', TIMESTAMP, 8), Column('SERVICELIFE', SHORT, 2),
        Column('FINACIALACCOUNT', LONG, 4), Column('FINACIALACCOUNT_AFA', LONG, 4),
    ]).add(ID=5, INVNO=1, LABEL='Probe Dienstwagen', PURCHASEDATE='2022-06-07 00:00:00',
           PURCHASEAMOUNTNET=0.01, SERVICELIFE=3, FINACIALACCOUNT=320, FINACIALACCOUNT_AFA=4832)
    builder.table('MOV_INVENTORY_BOOKINGS', [
        Column('ID', LONG, 4), Column('INVENTORYID', LONG, 4), Column('BOOKINGDATE', TIMESTAMP, 8),
        Column('AMOUNTNET', INT64, 8, scale=-2), Column('DESCRIPTION', TEXT, 40),
    ]).add(ID=1, INVENTORYID=5, BOOKINGDATE='2022-06-07 00:00:00', AMOUNTNET=5232.0,
           DESCRIPTION='Kauf')
    builder.table('MOV_INVENTORY_AMORTIZATIONS', [
        Column('ID', LONG, 4), Column('INVENTORYID', LONG, 4), Column('BOOKINGDATE', TIMESTAMP, 8),
        Column('AMORT_AMOUNT', INT64, 8, scale=-2), Column('AMORT_CUMULATED_AMOUNT', INT64, 8, scale=-2),
        Column('RESIDUALVALUE_AMOUNT', INT64, 8, scale=-2),
    ]).add(ID=1, INVENTORYID=5, BOOKINGDATE='2022-12-31 00:00:00', AMORT_AMOUNT=2418.27,
           AMORT_CUMULATED_AMOUNT=2418.27)
    return builder.write(str(tmp_path / 'db1.fdb'))


def test_anlagen_kommen_nach_einer_uebernahme_weiter_an(db_with_coa, tmp_path):
    """Die Anlagenverwaltung läuft neben der Übernahme der Buchungen weiter: das Anlagegut kommt
    an, und ein zweiter Lauf aktualisiert es, statt es zu verdoppeln."""
    path = _asset_fdb(tmp_path)
    _csv_import(db_with_coa, CSV_ROWS[:1])

    result = db_with_coa.import_wiso_fdb(path)
    assert (result['imported'], result['adopted'], result['assets'], result['depreciations']) == (
        0, 1, 1, 1)
    assert not result['errors']

    second = db_with_coa.import_wiso_fdb(path)
    assert (second['imported'], second['skipped'], second['adopted']) == (0, 1, 0)
    assert second['assets'] == 1                                    # aktualisiert
    assets = _query(db_with_coa, "SELECT Name, PurchasePrice FROM Assets WHERE Name = 'Probe Dienstwagen'")
    assert [(a['Name'], a['PurchasePrice']) for a in assets] == [('Probe Dienstwagen', 52320000)]
    assert len(_entries(db_with_coa)) == 1


# ── Ergebnis: adopted_rows und SQL-Protokoll ─────────────────────────────────

def test_adopted_rows_nennen_die_uebernommenen_buchungen(db_with_coa, tmp_path):
    """Das Ergebnis führt jede übernommene Buchung auf: ID der vorhandenen Zeile, Datum, Betrag und
    den Text aus WISO (nicht den der CSV-Zeile)."""
    rows = [list(row) for row in CSV_ROWS]
    rows[VERKAUF][3] = 'Verkauf März (Text aus der CSV)'
    _csv_import(db_with_coa, [tuple(row) for row in rows])
    csv_ids = {r['Text']: r['ID'] for r in _entries(db_with_coa)}

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    listed = result['adopted_rows']
    assert len(listed) == result['adopted'] == 5
    assert {row['id'] for row in listed} == set(csv_ids.values())
    verkauf = [row for row in listed if row['id'] == csv_ids['Verkauf März (Text aus der CSV)']][0]
    assert verkauf['text'] == 'Probe Verkauf'                    # WISO-Text
    assert verkauf['date'] == '2024-03-01'
    assert sorted(row['date'] for row in listed) == [
        '2024-03-01', '2024-03-05', '2024-03-05', '2024-03-05', '2024-03-15']
    assert all(set(row) >= {'id', 'date', 'amount', 'text'} for row in listed)


def test_adopted_rows_sind_leer_wenn_nichts_uebernommen_wurde(db_with_coa, tmp_path):
    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert result['adopted'] == 0 and result['adopted_rows'] == []


def test_adopted_rows_haben_hoechstens_500_eintraege(db_with_coa, tmp_path):
    """Die Liste ist begrenzt (Seite und Ergebnisdatei bleiben klein), der Zähler zählt alles."""
    count = 520
    rows = [('02.04.2024', '6815', '1800', f'Probe Massen {n:03d}', f'PR-M{n:03d}',
             f'{n + 1},00', '') for n in range(count)]
    records = [dict(ID=n + 1, ACCOUNTINGID=2000 + n, ACCOUNTING_DATE='2024-04-02 00:00:00',
                    AMOUNTGROSS=float(n + 1), ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200,
                    ACCOUNTING_TEXT=f'Probe Massen {n:03d}', REFERENCENO=f'PR-M{n:03d}')
               for n in range(count)]
    _csv_import(db_with_coa, rows)
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))
    assert (result['imported'], result['adopted']) == (0, count)
    assert len(result['adopted_rows']) == 500


def _sql_log(db):
    path = os.path.join(userctx.user_data_dir(), 'sql_operations.sql')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as handle:
        return handle.read().splitlines()


def test_uebernahme_steht_im_sql_protokoll(db_with_coa, tmp_path):
    """Jede Änderung an einer vorhandenen Buchung gehört ins Protokoll (Nachvollziehbarkeit): das
    UPDATE nennt die Zeile und die gesetzte Quellgruppe."""
    _csv_import(db_with_coa)
    ids = {r['Text']: r['ID'] for r in _entries(db_with_coa)}
    _fdb_import(db_with_coa, _fdb(tmp_path))

    updates = [line for line in _sql_log(db_with_coa) if line.startswith('UPDATE Bookings SET')
               and 'SourceGroup' in line]
    assert len(updates) == 5
    for text in ('Probe Verkauf', 'Probe Buero', 'Probe ohne Beleg'):
        group = {'Probe Verkauf': '700', 'Probe Buero': '701', 'Probe ohne Beleg': '704'}[text]
        assert any(f"'{group}'" in line and re.search(rf'\bID\s*=\s*{ids[text]}\b', line)
                   for line in updates), text


def test_fehler_mitten_im_lauf_laesst_keine_halbe_uebernahme_zurueck(db_with_coa, tmp_path,
                                                                     monkeypatch):
    """Übernehmen und Anlegen laufen in EINER Transaktion: bricht der Lauf beim vierten Satz ab, ist
    nichts übernommen und nichts im SQL-Protokoll – und derselbe Import danach geht sauber durch."""
    path = _fdb(tmp_path)
    _csv_import(db_with_coa)
    before = _entries(db_with_coa)

    database = type(db_with_coa)
    original = database._minor_opt
    calls = []

    def fail_on_the_fourth(value):
        calls.append(value)
        if len(calls) == 4:
            raise RuntimeError('Festplatte voll')
        return original(value)

    # eigener Kontext: ein monkeypatch.undo() risse auch die Datenwurzel der Fixture mit zurück
    with monkeypatch.context() as patch:
        patch.setattr(database, '_minor_opt', staticmethod(fail_on_the_fourth))
        with pytest.raises(RuntimeError):
            db_with_coa.import_wiso_fdb(path, with_assets=False, with_invoices=False)

    assert len(calls) == 4
    assert _entries(db_with_coa) == before                     # keine Quellgruppe, nichts neu
    assert [line for line in _sql_log(db_with_coa) if line.startswith('UPDATE Bookings')] == []

    result = _fdb_import(db_with_coa, path)
    assert (result['imported'], result['adopted']) == (1, 5)


def test_handler_meldet_einen_fehler_und_laesst_die_buchungen_unveraendert(db_with_coa, tmp_path,
                                                                           monkeypatch):
    """Scheitert der Import mitten im Lauf, antwortet der Handler mit der Fehlerseite – und die
    Buchungen aus dem CSV-Import sind unberührt (nichts halb übernommen, nichts angelegt)."""
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    path = _fdb(tmp_path)
    _csv_import(db_with_coa)
    before = _entries(db_with_coa)
    database = type(db_with_coa)
    original = database._minor_opt
    calls = []

    def fail_on_the_third(value):
        calls.append(value)
        if len(calls) == 3:
            raise RuntimeError('Festplatte voll')
        return original(value)

    with monkeypatch.context() as patch:
        patch.setattr(database, '_minor_opt', staticmethod(fail_on_the_third))
        status, location = handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [path]})

    assert status == 303
    assert _query_of(location) == {'wiso_fdb': 'error', 'msg': 'Festplatte voll'}
    assert _entries(db_with_coa) == before


# ── Handler und Seite ────────────────────────────────────────────────────────

def _query_of(location):
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


def test_handler_meldet_uebernommene(db_with_coa, tmp_path):
    """Die Weiterleitung nennt ``adopted``; die Ergebnisdatei führt die Zeilen auf; ein zweiter Lauf
    meldet 0."""
    os.makedirs(userctx.user_data_dir(), exist_ok=True)           # dorthin schreibt der Handler
    path = _fdb(tmp_path)
    _csv_import(db_with_coa)

    status, location = handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [path]})
    assert status == 303 and location.startswith('/miscellaneous?')
    params = _query_of(location)
    assert (params['wiso_fdb'], params['imported'], params['skipped'], params['adopted']) == (
        'ok', '1', '0', '5')
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'),
              encoding='utf-8') as handle:
        stored = json.load(handle)
    assert stored['adopted'] == 5 and stored['imported'] == 1
    assert len(stored['adopted_rows']) == 5

    status, location = handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [path]})
    params = _query_of(location)
    assert (params['imported'], params['skipped'], params['adopted']) == ('0', '6', '0')
    assert len(_entries(db_with_coa)) == 6


def test_seite_zeigt_detailzeile_nur_bei_uebernommenen(db_with_coa, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    path = _fdb(tmp_path)
    _csv_import(db_with_coa)
    handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [path]})
    page = PageMiscellaneous(db_with_coa)
    assert '5 Buchungssätze waren schon aus dem CSV-Import da' in page
    assert 'wurden aber nicht doppelt angelegt' in page

    handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [path]})      # zweiter Lauf: 0
    assert 'aus dem CSV-Import da' not in PageMiscellaneous(db_with_coa)


def _details(page):
    """Die aufklappbare Liste „Welche Buchungen das waren“ als Zeilen mit Zellentexten."""
    (block,) = re.findall(r'<details><summary>Welche Buchungen das waren</summary>(.*?)</details>',
                          page, re.S)
    return [re.findall(r'<td>(.*?)</td>', row) for row in re.findall(r'<tr>(.*?)</tr>', block, re.S)
            if '<td>' in row], block


def test_seite_listet_die_uebernommenen_buchungen_maskiert_auf(db_with_coa, tmp_path):
    """Datum, Betrag (deutsch), Text aus WISO und ID der übernommenen Zeilen stehen aufklappbar im
    Detailblock; Werte aus WISO sind maskiert."""
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    records = _records()
    records[VERKAUF]['ACCOUNTING_TEXT'] = '<b>Verkauf</b> & Co'
    _csv_import(db_with_coa)
    ids = {r['DocumentNumber'] + r['DateBooking']: r['ID'] for r in _entries(db_with_coa)
           if r['DocumentNumber'] in ('PR-001', 'PR-002')}
    handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [_fdb(tmp_path, records)]})

    page = PageMiscellaneous(db_with_coa)
    rows, block = _details(page)
    assert len(rows) == 5 and 'Genannt werden die ersten' not in page
    by_date = {}
    for date, amount, text, booking_id in rows:
        by_date.setdefault(date, []).append((amount, text, booking_id))
    verkauf = by_date['2024-03-01'][0]
    assert verkauf[:2] == ('1.190,00 \u20ac', '&lt;b&gt;Verkauf&lt;/b&gt; &amp; Co')
    assert verkauf[2] == str(ids['PR-0012024-03-01'])
    assert ('-238,00 \u20ac', 'Probe Buero', str(ids['PR-0022024-03-05'])) in by_date['2024-03-05']
    assert '<b>Verkauf</b>' not in page and '<b>' not in block
    assert sorted(date for date, *_ in rows) == [
        '2024-03-01', '2024-03-05', '2024-03-05', '2024-03-05', '2024-03-15']


def test_seite_nennt_hoechstens_500_buchungen(db_with_coa, tmp_path):
    """Die Liste auf der Seite endet bei 500 und sagt es; der Zähler darüber nennt alle."""
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    count = 510
    _csv_import(db_with_coa, [('02.04.2024', '6815', '1800', f'Probe Massen {n:03d}', f'PR-M{n:03d}',
                               f'{n + 1},00', '') for n in range(count)])
    records = [dict(ID=n + 1, ACCOUNTINGID=2000 + n, ACCOUNTING_DATE='2024-04-02 00:00:00',
                    AMOUNTGROSS=float(n + 1), ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200,
                    ACCOUNTING_TEXT=f'Probe Massen {n:03d}', REFERENCENO=f'PR-M{n:03d}')
               for n in range(count)]
    status, location = handlers.handle_wiso_fdb_import(db_with_coa,
                                                       {'fdb_path': [_fdb(tmp_path, records)]})
    assert _query_of(location)['adopted'] == str(count)
    page = PageMiscellaneous(db_with_coa)
    rows, _block = _details(page)
    assert len(rows) == 500 and 'Genannt werden die ersten 500.' in page
    assert f'{count} Buchungssätze waren schon aus dem CSV-Import da' in page


def test_hinweistexte_nennen_die_wiedererkennung(db_with_coa):
    page = PageMiscellaneous(db_with_coa)
    assert 'Duplikate (gleiche Referenznummer + Datum + Konto + Betrag) werden übersprungen' in page
    assert 'auch, wenn die Buchungen schon aus dem Datenbank-Import stammen' in page


def test_detailzeile_maskiert_den_gespeicherten_wert(db_with_coa):
    """Auch ein manipulierter Wert in der Ergebnisdatei landet nicht roh im HTML."""
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    payload = '<script>alert(1)</script>'
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), 'w',
              encoding='utf-8') as handle:
        json.dump({'adopted': payload, 'file': 'DB1.FDB'}, handle)
    page = PageMiscellaneous(db_with_coa)
    assert payload not in page and '&lt;script&gt;alert(1)&lt;/script&gt;' in page


def _fdb_script(page):
    """Der Skriptblock der Seite, der die Meldung nach dem Datenbank-Import baut."""
    scripts = re.findall(r'<script>(.*?)</script>', page, re.S)
    (script,) = [s for s in scripts if "p.get('wiso_fdb')" in s]
    return script


def test_meldungsskript_liest_adopted_als_zahl(db_with_coa):
    """Statisch: ``adopted`` wird als Zahl gelesen und nur bei > 0 genannt."""
    script = _fdb_script(PageMiscellaneous(db_with_coa))
    assert "parseInt(p.get('adopted') || '0')" in script
    assert "if (ad > 0) msg += ', ' + ad + ' aus dem CSV-Import wiedererkannt'" in script


NODE = shutil.which('node')


def _shown_messages(script, search, tmp_path):
    """Führt das Meldungsskript mit einer Adresse in Node aus und liefert den angezeigten Text."""
    runner = tmp_path / 'run_message.js'
    runner.write_text(
        "const vm = require('vm');\n"
        "const shown = [];\n"
        "const div = {_style: {}, set style(v) { this._css = v; }, get style() { return this._style; },\n"
        "             append(text) { shown.push(String(text)); }};\n"
        "const sandbox = {window: {location: {search: process.argv[3]}},\n"
        "  document: {createElement: () => div, currentScript: {parentNode: {insertBefore() {}}}},\n"
        "  URLSearchParams, decodeURIComponent, parseInt};\n"
        "vm.runInNewContext(require('fs').readFileSync(process.argv[2], 'utf8'), sandbox);\n"
        "console.log(JSON.stringify(shown));\n", encoding='utf-8')
    source = tmp_path / 'message.js'
    source.write_text(script, encoding='utf-8')
    done = subprocess.run([NODE, str(runner), str(source), search], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – der statische Test '
                                         '(test_meldungsskript_liest_adopted_als_zahl) deckt es ab')
def test_meldung_nennt_wiedererkannte_buchungen(db_with_coa, tmp_path):
    script = _fdb_script(PageMiscellaneous(db_with_coa))
    base = '?wiso_fdb=ok&imported=1&skipped=0&assets=0&afa=0'
    (message,) = _shown_messages(script, base + '&adopted=5', tmp_path)
    assert '1 Buchungssätze, 0 übersprungen' in message
    assert ', 5 aus dem CSV-Import wiedererkannt' in message
    (without,) = _shown_messages(script, base + '&adopted=0', tmp_path)
    assert 'wiedererkannt' not in without
    (missing,) = _shown_messages(script, base, tmp_path)
    assert 'wiedererkannt' not in missing


@pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – der statische Test '
                                         '(test_adopted_aus_der_adresse_landet_nicht_roh_im_html) '
                                         'deckt den Server-Teil ab')
@pytest.mark.parametrize('value', ['<script>alert(1)</script>', '5<img src=x onerror=alert(1)>',
                                   '"><b>x</b>', '-3', '1e9', ''])
def test_meldung_uebernimmt_keinen_rohen_wert_aus_der_adresse(db_with_coa, tmp_path, value):
    """Nur eine Zahl > 0 wird genannt; alles andere erscheint nicht in der Meldung."""
    script = _fdb_script(PageMiscellaneous(db_with_coa))
    (message,) = _shown_messages(
        script, '?wiso_fdb=ok&imported=1&skipped=0&assets=0&afa=0&adopted=' + quote(value),
        tmp_path)
    for fragment in ('<script>', '<img', '<b>', 'onerror', 'alert'):
        assert fragment not in message, message
    if value.startswith('5'):
        assert ', 5 aus dem CSV-Import wiedererkannt' in message
    elif value == '1e9':
        assert ', 1 aus dem CSV-Import wiedererkannt' in message       # parseInt liest „1“
    else:
        assert 'wiedererkannt' not in message


def test_adopted_aus_der_adresse_landet_nicht_roh_im_html(server_port, auto_login):
    """Die Seite wird vom Server ausgeliefert: der Wert aus ``?adopted=`` steht nirgends im HTML –
    ihn liest erst das Skript im Browser, und dort wird er zur Zahl."""
    payload = '<script>alert(7)</script>'
    page = get(server_port, '/miscellaneous?wiso_fdb=ok&imported=1&skipped=0&adopted='
                            + payload.replace('<', '%3C').replace('>', '%3E'))
    assert page.status == 200
    assert 'alert(7)' not in page.text and payload not in page.text


def _multipart_csv(csv_bytes, csrf):
    boundary = 'GRENZE0815'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{csrf}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="csvfile"; filename="export.csv"\r\n'
            'Content-Type: text/csv\r\n\r\n').encode() + csv_bytes + f'\r\n--{boundary}--\r\n'.encode()
    return f'multipart/form-data; boundary={boundary}', body


def test_csv_und_datenbank_ueber_den_server_nacheinander(server_port, auto_login, tmp_path):
    """Der ganze Weg über HTTP: CSV-Import, dann Datenbank-Import – nichts doppelt, die
    Weiterleitung nennt ``adopted``, die Seite zeigt die Detailzeile."""
    db = auto_login.db
    db.load_test_seed_data()
    path = _fdb(tmp_path)
    content_type, body = _multipart_csv(_csv(), auto_login.csrf)
    imported = send(server_port, 'POST', '/wiso/import',
                    {'Origin': own_origin(server_port), 'Content-Type': content_type}, body)
    assert imported.status == 303 and 'wiso_import=ok' in imported.headers['Location']
    assert len(_entries(db)) == 5

    answer = post(server_port, '/wiso/fdb-import', headers={'Origin': own_origin(server_port)},
                  csrf=auto_login.csrf,
                  form={'fdb_path': path, 'with_assets': '1', 'with_invoices': '1'})
    assert answer.status == 303
    params = _query_of(answer.headers['Location'])
    assert (params['wiso_fdb'], params['imported'], params['skipped'], params['adopted']) == (
        'ok', '1', '0', '5')
    assert len(_entries(db)) == 6

    page = get(server_port, answer.headers['Location'])
    assert page.status == 200
    assert '5 Buchungssätze waren schon aus dem CSV-Import da' in page.text

    second = post(server_port, '/wiso/fdb-import', headers={'Origin': own_origin(server_port)},
                  csrf=auto_login.csrf,
                  form={'fdb_path': path, 'with_assets': '1', 'with_invoices': '1'})
    params = _query_of(second.headers['Location'])
    assert (params['imported'], params['skipped'], params['adopted']) == ('0', '6', '0')
    assert len(_entries(db)) == 6
