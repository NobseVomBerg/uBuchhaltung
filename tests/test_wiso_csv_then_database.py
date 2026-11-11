# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""WISO: CSV-Import und Datenbank-Import nacheinander (2.0.6) – die Regeln der Übernahme.

Der CSV-Import schreibt keine ``SourceGroup`` und erkennt Duplikate am Schlüssel Beleg-Nr. +
Datum + Konto + Betrag. Der Datenbank-Import kannte bisher nur die ``SourceGroup`` – nach einem
CSV-Import legte er alles ein zweites Mal an. Jetzt übernimmt er eine vorhandene Buchung, statt sie
neu anzulegen – aber eng gefasst: lieber eine sichtbare zweite Buchung als eine still falsche
Zuordnung. Ein WISO-Satz übernimmt eine Buchung nur, wenn ALLES gilt:

1. Kandidat ist eine ``entry`` ohne ``SourceGroup``, ohne ``AutoMirror`` und ohne ``Account_ID``;
   gleiches Datum, gleicher Betrag, gleiches Konto (``COA_ID``), gleiche Beleg-Nr. (Leerraum
   zusammengefasst, leer = NULL).
2. Der Text ist gleich – immer (Leerraum zusammengefasst, Groß-/Kleinschreibung egal; der Text aus der
   WISO-Datenbank wird von latin-1 nach cp1252 umgesetzt, damit „€“, „–“, „…“ übereinstimmen). Zwei
   LEERE Texte gelten nur als gleich, wenn das Gegenkonto auf beiden Seiten bekannt und gleich ist.
3. Das Gegenkonto passt: gleich (beide bekannt) oder der Kandidat hat keines. Ein ANDERES Gegenkonto
   passt nie.
4. Tragen mehrere Sätze desselben Imports denselben Schlüssel und Text, aber verschiedene Gegenkonten,
   übernimmt keiner von ihnen einen Kandidaten ohne Gegenkonto (alle werden neu angelegt); Kandidaten
   mit gleichem bekanntem Gegenkonto bleiben übernehmbar.
5. Gewählt wird zuerst der Kandidat mit gleichem Gegenkonto, dann der ohne; bei Gleichstand die kleinste ID.
6. Ersatzsuche ohne Konto (``COA_ID IS NULL``): nur wenn es von Anfang an keinen Kandidaten mit Konto
   gab, und nur bei gleichem bekanntem Gegenkonto UND gleichem, nicht leerem Text.

Übernommen wird: ``SourceGroup`` setzen, leere Konten füllen, ``TaxRate``/``TaxAmount`` (nur wenn beide
leer sind) und ``RecipientClient`` (wenn leer) ergänzen – sonst bleibt alles. Was nicht übernommen wird,
obwohl für denselben Schlüssel ein Satz neu angelegt wurde, meldet das Ergebnis als
``possible_duplicates`` (die Berichtstests stehen in ``test_wiso_adoption_report.py``).

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
         (8405, 4405, 'Erlöse 19 % noch offen'), (4930, 6815, 'Bürobedarf'),
         (4900, 6300, 'Sonstige Aufwendungen'), (4830, 6220, 'Abschreibungen auf Sachanlagen'),
         (320, 520, 'Pkw'))
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
    # der Text ordnet zu – auch die beiden gleich hohen Teilbuchungen bekommen ihre eigene Quellgruppe
    assert {text: after[text]['SourceGroup'] for text in before} == {
        'Probe Verkauf': '700', 'Probe Buero': '701', 'Probe Split A': '702',
        'Probe Split B': '703', 'Probe ohne Beleg': '704'}


def test_gleichbetragige_teilbuchungen_je_einzeln_uebernommen(db_with_coa, tmp_path):
    """Zwei CSV-Zeilen ↔ zwei Sätze: beide werden übernommen, jede genau einmal."""
    _csv_import(db_with_coa)
    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert result['adopted'] == 5
    split = [row for row in _entries(db_with_coa) if row['DocumentNumber'] == 'PR-003']
    assert len(split) == 2
    assert {row['Text']: row['SourceGroup'] for row in split} == {
        'Probe Split A': '702', 'Probe Split B': '703'}


@pytest.mark.parametrize('reverse', [False, True], ids=['A vor B', 'B vor A'])
def test_eine_csv_zeile_fuer_zwei_gleich_hohe_saetze(db_with_coa, tmp_path, reverse):
    """Eine CSV-Zeile ↔ zwei Sätze: nur der Satz mit demselben Text übernimmt sie, der andere wird neu
    angelegt – gleich, in welcher Reihenfolge WISO sie liefert (kein „der Erste nimmt sie“)."""
    rows = [row for row in CSV_ROWS if row[3] != 'Probe Split B']
    _csv_import(db_with_coa, rows)
    csv_a = _by_text(db_with_coa)['Probe Split A']
    records = _records()
    if reverse:
        records[SPLIT_A], records[SPLIT_B] = records[SPLIT_B], records[SPLIT_A]

    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))
    assert (result['imported'], result['adopted']) == (2, 4)      # Split B und Vorjahr neu
    assert result['possible_duplicates'] == []                    # keine Zeile blieb übrig
    split = [row for row in _entries(db_with_coa) if row['DocumentNumber'] == 'PR-003']
    assert {row['Text']: row['SourceGroup'] for row in split} == {
        'Probe Split A': '702', 'Probe Split B': '703'}
    assert _row(db_with_coa, csv_a['ID'])['SourceGroup'] == '702'   # die CSV-Zeile blieb die von A


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


def test_geaenderter_text_verhindert_die_uebernahme(db_with_coa, tmp_path):
    """Hat der Nutzer den Text einer CSV-Zeile geändert, passt sie nicht mehr zum Satz aus WISO: Sie
    bleibt, wie sie ist, der Satz kommt als zweite Buchung dazu – und die Zeile steht im Ergebnis als
    „möglicherweise doppelt“."""
    _csv_import(db_with_coa)
    _execute(db_with_coa, "UPDATE Bookings SET Text = 'Büro, vom Nutzer umbenannt'"
                          " WHERE Text = 'Probe Buero'")
    renamed = [r for r in _entries(db_with_coa) if r['Text'] == 'Büro, vom Nutzer umbenannt'][0]

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (2, 4)
    assert _row(db_with_coa, renamed['ID']) == renamed
    assert [d['id'] for d in result['possible_duplicates']] == [renamed['ID']]
    assert result['possible_duplicates_count'] == 1
    assert len([r for r in _entries(db_with_coa) if r['DocumentNumber'] == 'PR-002']) == 2


def test_uebernahme_behaelt_text_und_verknuepfung(db_with_coa, tmp_path):
    """Beleg-Nr., Steuer, Bankverknüpfung, Konto, Kategorie, Kontakt, Empfänger und Status der
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
    _execute(db, "UPDATE Bookings SET Contact_ID = ?, Category_ID = ?, RecipientClient = 'Kundin',"
                 " Status = 'geprüft', DateTax = '2024-03-31' WHERE Text = 'Probe Verkauf'",
             (contact, category))
    before = {row['ID']: row for row in _entries(db)}

    result = _fdb_import(db, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (1, 5)

    after = {row['ID']: row for row in _entries(db)}
    for entry_id, row in before.items():
        assert after[entry_id] == {**row, 'SourceGroup': after[entry_id]['SourceGroup']}, row['Text']
        assert after[entry_id]['SourceGroup'], row['Text']              # überall gesetzt
    changed = [r for r in after.values() if r['Text'] == 'Probe Verkauf'][0]
    assert (changed['Contact_ID'], changed['Category_ID'], changed['RecipientClient']) == (
        contact, category, 'Kundin')
    assert (changed['Status'], changed['DateTax']) == ('geprüft', '2024-03-31')
    assert after[linked['ID']]['ParentBooking_ID'] == bank_booking
    # die Bankbewegung selbst blieb unangetastet
    assert _row(db, bank_booking)['SourceGroup'] is None


# ── Text und Gegenkonto ──────────────────────────────────────────────────────

#: Kandidat (Abweichungen von ``_candidate``) und ob der Satz ``_record()`` ihn übernimmt. Satz:
#: Bank an Erlöse (Gegenkonto 4400), Text „Probe Verkauf“. Der Text muss IMMER gleich sein.
COUNTER_CASES = [
    ('Gegenkonto gleich, Text gleich', dict(), True),
    ('Gegenkonto gleich, Text gleich bis auf Leerraum und Schreibweise',
     dict(text=' probe\tVERKAUF '), True),
    ('Gegenkonto gleich, Text anders', dict(text='ganz anderer Text'), False),
    ('Gegenkonto gleich, Text leer', dict(text=''), False),
    ('Gegenkonto leer, Text gleich', dict(counter=None), True),
    ('Gegenkonto leer, Text gleich bis auf Leerraum und Schreibweise',
     dict(counter=None, text='  PROBE   verkauf '), True),
    ('Gegenkonto leer, Text anders', dict(counter=None, text='Verkauf Kunde A'), False),
    ('Gegenkonto leer, Text leer', dict(counter=None, text=''), False),
    ('anderes Gegenkonto, Text gleich', dict(counter=1810), False),
]


@pytest.mark.parametrize('name,overrides,adopted', COUNTER_CASES, ids=[c[0] for c in COUNTER_CASES])
def test_text_und_gegenkonto_muessen_passen(db_with_coa, tmp_path, name, overrides, adopted):
    """Der Text muss gleich sein, und das Gegenkonto muss gleich sein oder dem Kandidaten fehlen. Sonst
    bleibt der Kandidat unberührt, der Satz kommt neu dazu, und die Zeile steht als „möglicherweise
    doppelt“ im Ergebnis."""
    booking = _candidate(db_with_coa, **overrides)
    before = _row(db_with_coa, booking)

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    after = _row(db_with_coa, booking)
    if adopted:
        assert (result['imported'], result['adopted']) == (0, 1)
        assert after['SourceGroup'] == '900'
        assert after['CounterCOA_ID'] == _coa(db_with_coa, 4400)             # gefüllt oder gleich
        assert len(_entries(db_with_coa)) == 1
        assert result['possible_duplicates'] == []
    else:
        assert (result['imported'], result['adopted']) == (1, 0)
        assert after == before
        assert len(_entries(db_with_coa)) == 2
        assert [d['id'] for d in result['possible_duplicates']] == [booking]


#: Leere Texte: der Satz hat den Text '' (Gegenkonto 4400, sofern nicht anders angegeben). Zwei leere
#: Texte gelten nur als gleich, wenn das Gegenkonto auf beiden Seiten bekannt und gleich ist.
EMPTY_TEXT_CASES = [
    ('beide leer, Gegenkonto beidseitig gleich', dict(text=''), dict(), True),
    ('beide leer (nur Leerraum), Gegenkonto beidseitig gleich', dict(text='  \t '), dict(), True),
    ('beide leer, Kandidat ohne Gegenkonto', dict(text='', counter=None), dict(), False),
    ('beide leer, anderes Gegenkonto', dict(text='', counter=1810), dict(), False),
    ('beide leer, Gegenkonto beidseitig unbekannt', dict(text='', counter=None),
     dict(CONTRA_ACCOUNTNO=None), False),
    ('Satz leer, Kandidat mit Text', dict(), dict(), False),
]


@pytest.mark.parametrize('name,overrides,record_overrides,adopted', EMPTY_TEXT_CASES,
                         ids=[c[0] for c in EMPTY_TEXT_CASES])
def test_leere_texte_gelten_nur_mit_bekanntem_gleichem_gegenkonto_als_gleich(
        db_with_coa, tmp_path, name, overrides, record_overrides, adopted):
    booking = _candidate(db_with_coa, **overrides)
    before = _row(db_with_coa, booking)
    record = _record(ACCOUNTING_TEXT='', **record_overrides)

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [record]))
    if adopted:
        assert (result['imported'], result['adopted']) == (0, 1)
        assert _row(db_with_coa, booking)['SourceGroup'] == '900'
    else:
        assert (result['imported'], result['adopted']) == (1, 0)
        assert _row(db_with_coa, booking) == before


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


# ── Mehrdeutigkeit: gleicher Schlüssel und Text, verschiedene Gegenkonten ───

def _payment(number, group, counter, text, day='2024-03-12'):
    """Zahlungseingang von 119,00 € auf Bankkonto 2 (SKR03 1210) ohne Beleg-Nr.; ``counter`` ist das
    Gegenkonto in WISO (Personenkonto ab 10000 oder SKR03-Erlöskonto)."""
    return dict(ID=number, ACCOUNTINGID=group, ACCOUNTING_DATE=f'{day} 00:00:00', AMOUNTGROSS=119.0,
                ACCOUNTNO=1210, CONTRA_ACCOUNTNO=counter, ACCOUNTING_TEXT=text)


def _payment_row(text, day='12.03.2024', counter=''):
    """Dieselbe Zahlung als CSV-Zeile: Konto 1810, Gegenkonto leer (oder ``counter``)."""
    return (day, '1810', counter, text, '', '119,00', '')


@pytest.mark.parametrize('text', ['Zahlungseingang', ''], ids=['gleicher Text', 'leere Texte'])
def test_gleicher_schluessel_und_text_bei_verschiedenen_gegenkonten_uebernimmt_keiner(
        db_with_coa, tmp_path, text):
    """Zwei CSV-Zeilen „Bank an (leer) 119,00“ am selben Tag ohne Beleg-Nr. mit demselben Text; WISO
    liefert B (Personenkonto 10002) vor A (10001). Welche Zeile zu welchem Satz gehört, lässt sich nicht
    entscheiden – keiner übernimmt eine, es entstehen 4 Buchungen, und beide CSV-Zeilen stehen als
    „möglicherweise doppelt“ im Ergebnis. (Leere Texte gelten ohne Gegenkonto nie als gleich.)"""
    _csv_import(db_with_coa, [_payment_row(text), _payment_row(text)])
    csv_rows = _entries(db_with_coa)
    assert len(csv_rows) == 2

    records = [_payment(1, 802, 10002, text), _payment(2, 801, 10001, text)]
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))

    assert (result['imported'], result['adopted']) == (2, 0)
    assert len(_entries(db_with_coa)) == 4
    assert [_row(db_with_coa, row['ID']) for row in csv_rows] == csv_rows        # beide unberührt
    assert sorted(d['id'] for d in result['possible_duplicates']) == sorted(r['ID'] for r in csv_rows)
    assert result['possible_duplicates_count'] == 2


def test_bei_mehrdeutigkeit_bleiben_zeilen_mit_gleichem_bekanntem_gegenkonto_uebernehmbar(
        db_with_coa, tmp_path):
    """Haben die CSV-Zeilen ihr Gegenkonto (4400 / 4405), ist die Zuordnung eindeutig: jeder Satz nimmt
    die Zeile mit seinem Gegenkonto, auch wenn alle denselben Text tragen und WISO B vor A liefert."""
    _csv_import(db_with_coa, [_payment_row('Zahlungseingang', counter='4400'),
                              _payment_row('Zahlungseingang', counter='4405')])
    row_4400, row_4405 = _entries(db_with_coa)
    assert (row_4400['CounterCOA_ID'], row_4405['CounterCOA_ID']) == (
        _coa(db_with_coa, 4400), _coa(db_with_coa, 4405))

    records = [_payment(1, 802, 8405, 'Zahlungseingang'), _payment(2, 801, 8400, 'Zahlungseingang')]
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))
    assert (result['imported'], result['adopted']) == (0, 2)
    assert _row(db_with_coa, row_4400['ID'])['SourceGroup'] == '801'
    assert _row(db_with_coa, row_4405['ID'])['SourceGroup'] == '802'
    assert len(_entries(db_with_coa)) == 2


def test_gleiche_saetze_mit_gleichem_gegenkonto_sind_nicht_mehrdeutig(db_with_coa, tmp_path):
    """Zwei Sätze mit gleichem Schlüssel, Text UND Gegenkonto sind austauschbar: beide übernehmen eine
    Zeile ohne Gegenkonto."""
    _csv_import(db_with_coa, [_payment_row('Zahlungseingang')] * 2)
    csv_rows = _entries(db_with_coa)
    records = [_payment(1, 801, 10001, 'Zahlungseingang'), _payment(2, 802, 10001, 'Zahlungseingang')]
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))
    assert (result['imported'], result['adopted']) == (0, 2)
    assert sorted(_row(db_with_coa, r['ID'])['SourceGroup'] for r in csv_rows) == ['801', '802']
    assert all(_row(db_with_coa, r['ID'])['CounterCOA_ID'] == _coa(db_with_coa, 10001)
               for r in csv_rows)


def test_mehrdeutigkeit_gilt_je_schluessel(db_with_coa, tmp_path):
    """Derselbe Text bei verschiedenen Gegenkonten an VERSCHIEDENEN Tagen ist nicht mehrdeutig."""
    _csv_import(db_with_coa, [_payment_row('Zahlungseingang', day='12.03.2024'),
                              _payment_row('Zahlungseingang', day='13.03.2024')])
    records = [_payment(1, 801, 10001, 'Zahlungseingang', day='2024-03-12'),
               _payment(2, 802, 10002, 'Zahlungseingang', day='2024-03-13')]
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))
    assert (result['imported'], result['adopted']) == (0, 2)
    by_day = {r['DateBooking']: r for r in _entries(db_with_coa)}
    assert by_day['2024-03-12']['SourceGroup'] == '801'
    assert by_day['2024-03-13']['SourceGroup'] == '802'


# ── Mehrere Kandidaten mit gleichem Schlüssel ────────────────────────────────

def test_zahlungen_zweier_kunden_werden_nach_text_zugeordnet(db_with_coa, tmp_path):
    """Zwei CSV-Zeilen „Bank an (leer) 119,00“ am selben Tag ohne Beleg-Nr., Zahlung Kunde A und B.
    Die Datenbank liefert B vor A, mit den Personenkonten 10002 und 10001: Zeile A bekommt 10001 und
    die Gruppe von A, Zeile B 10002 und die Gruppe von B – nicht „die erste Zeile dem ersten Satz“."""
    _csv_import(db_with_coa, [_payment_row('Zahlung Kunde A'), _payment_row('Zahlung Kunde B')])
    csv_a, csv_b = [_by_text(db_with_coa)[f'Zahlung Kunde {k}'] for k in 'AB']
    assert csv_a['ID'] < csv_b['ID']

    records = [_payment(1, 802, 10002, 'Zahlung Kunde B'), _payment(2, 801, 10001, 'Zahlung Kunde A')]
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


#: Kandidaten in ID-Reihenfolge (Abweichungen von ``_candidate``) und der Index dessen, der gewinnt.
#: Der Satz hat den Text „Probe Verkauf“ und das Gegenkonto 4400.
PICK_CASES = [
    ('gleiches Gegenkonto schlägt kein Gegenkonto', [dict(counter=None), dict()], 1),
    ('kein Gegenkonto vorn, gleiches Gegenkonto und kein Gegenkonto danach',
     [dict(counter=None), dict(), dict(counter=None)], 1),
    ('erster Kandidat passt nicht (Text), zweiter schon', [dict(counter=None, text='anders'), dict(counter=None)], 1),
    ('erster Kandidat passt nicht (Gegenkonto), zweiter schon', [dict(counter=1810), dict()], 1),
    ('gleiches Gegenkonto, aber anderer Text: passt nicht', [dict(text='anders'), dict(counter=None)], 1),
    ('beide mit gleichem Gegenkonto: die kleinste ID', [dict(), dict()], 0),
    ('beide ohne Gegenkonto: die kleinste ID', [dict(counter=None), dict(counter=None)], 0),
]


@pytest.mark.parametrize('name,candidates,winner', PICK_CASES, ids=[c[0] for c in PICK_CASES])
def test_unter_mehreren_kandidaten_gewinnt_der_passende(db_with_coa, tmp_path, name, candidates, winner):
    """Zuerst der Kandidat mit gleichem Gegenkonto, dann der ohne; bei Gleichstand die kleinste ID –
    und nie einfach der erste nach ID."""
    ids = [_candidate(db_with_coa, **overrides) for overrides in candidates]
    before = {i: _row(db_with_coa, i) for i in ids}

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    assert (result['imported'], result['adopted']) == (0, 1)
    for index, booking in enumerate(ids):
        if index == winner:
            assert _row(db_with_coa, booking)['SourceGroup'] == '900'
        else:
            assert _row(db_with_coa, booking) == before[booking]
    assert result['possible_duplicates'] == []         # der Satz wurde nicht neu angelegt


@pytest.mark.parametrize('reverse', [False, True], ids=['X vor Y', 'Y vor X'])
def test_text_ordnet_zeilen_mit_und_ohne_gegenkonto_zu_unabhaengig_von_der_reihenfolge(
        db_with_coa, tmp_path, reverse):
    """K1 (Gegenkonto 1810, Text „Y“), K2 (kein Gegenkonto, Text „X“); die Sätze X und Y haben beide das
    Gegenkonto 1810: X übernimmt K2, Y übernimmt K1 – in jeder Reihenfolge, es bleiben 2 Buchungen."""
    fields = dict(coa=6815, document_number=None, amount=-50.0, date_booking='2024-03-12',
                  tax_rate=None, tax_amount=None)
    k1 = _candidate(db_with_coa, counter=1810, text='Y', **fields)
    k2 = _candidate(db_with_coa, counter=None, text='X', **fields)

    def line(number, group, text):
        return dict(ID=number, ACCOUNTINGID=group, ACCOUNTING_DATE='2024-03-12 00:00:00',
                    AMOUNTGROSS=50.0, ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210, ACCOUNTING_TEXT=text)

    records = [line(1, 881, 'X'), line(2, 882, 'Y')]
    if reverse:
        records.reverse()
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))

    assert (result['imported'], result['adopted']) == (0, 2)
    assert _row(db_with_coa, k2)['SourceGroup'] == '881'
    assert _row(db_with_coa, k1)['SourceGroup'] == '882'
    assert _row(db_with_coa, k2)['CounterCOA_ID'] == _coa(db_with_coa, 1810)       # gefüllt
    assert len(_entries(db_with_coa)) == 2


def test_bei_gleichstand_gewinnt_die_kleinste_id(db_with_coa, tmp_path):
    """Passen mehrere Kandidaten gleich gut (hier vollkommen gleich), nimmt der Satz die mit der
    kleinsten ID – und nur eine: die andere bleibt unberührt (und wird nicht als doppelt gemeldet,
    weil der Satz nicht neu angelegt wurde)."""
    first, second = _candidate(db_with_coa), _candidate(db_with_coa)
    before_second = _row(db_with_coa, second)
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    assert (result['imported'], result['adopted']) == (0, 1)
    assert _row(db_with_coa, first)['SourceGroup'] == '900'
    assert _row(db_with_coa, second) == before_second
    assert result['possible_duplicates'] == []


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


def _open_line(db, **overrides):
    """Eine Buchung ohne Konto (25,00 € Ausgabe am 12.03.2024, ohne Beleg-Nr.)."""
    fields = dict(coa=None, counter=1810, text='Briefmarken', document_number=None, amount=-25.0,
                  date_booking='2024-03-12', tax_rate=None, tax_amount=None)
    fields.update(overrides)
    return _candidate(db, **fields)


_STAMPS = dict(ID=1, ACCOUNTINGID=821, ACCOUNTING_DATE='2024-03-12 00:00:00', AMOUNTGROSS=25.0,
               ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210, ACCOUNTING_TEXT='Briefmarken')


def test_ersatzsuche_ohne_konto_braucht_gleiches_gegenkonto_und_gleichen_text(db_with_coa, tmp_path):
    """Ohne eigenes Konto findet die Zeile ihren Satz nur über ein gleiches, bekanntes Gegenkonto UND
    einen gleichen, nicht leeren Text."""
    same = _open_line(db_with_coa)
    other_counter = _open_line(db_with_coa, counter=1800)
    no_counter = _open_line(db_with_coa, counter=None)
    other_text = _open_line(db_with_coa, text='noch offen')
    before = {i: _row(db_with_coa, i) for i in (other_counter, no_counter, other_text)}

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [dict(_STAMPS)]))
    assert (result['imported'], result['adopted']) == (0, 1)
    row = _row(db_with_coa, same)
    assert (row['SourceGroup'], row['COA_ID'], row['CounterCOA_ID']) == (
        '821', _coa(db_with_coa, 6815), _coa(db_with_coa, 1810))           # kontiert
    assert all(_row(db_with_coa, i) == before[i] for i in before)


def test_ersatzsuche_ohne_konto_nimmt_keine_leeren_texte(db_with_coa, tmp_path):
    """Selbst bei bekanntem, gleichem Gegenkonto: ohne Konto und ohne Text ist die Zeile kein
    Kandidat – leer ist nicht „derselbe Text“."""
    booking = _open_line(db_with_coa, text='')
    before = _row(db_with_coa, booking)
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [dict(_STAMPS, ACCOUNTING_TEXT='')]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert _row(db_with_coa, booking) == before


def test_ersatzsuche_gilt_nur_ohne_kandidaten_mit_konto_von_anfang_an(db_with_coa, tmp_path):
    """Gab es für den Schlüssel von Anfang an eine Buchung mit Konto, nimmt der zweite Satz nicht
    ersatzweise eine ohne Konto – auch nicht bei gleichem Gegenkonto und Text. Er kommt neu dazu."""
    with_account = _open_line(db_with_coa, coa=6815, text='mit Konto')
    without_account = _open_line(db_with_coa, text='mit Konto')
    before = _row(db_with_coa, without_account)
    records = [dict(_STAMPS, ID=number, ACCOUNTINGID=830 + number, ACCOUNTING_TEXT='mit Konto')
               for number in (1, 2)]
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))

    assert (result['imported'], result['adopted']) == (1, 1)
    assert _row(db_with_coa, with_account)['SourceGroup'] in ('831', '832')
    assert _row(db_with_coa, without_account) == before
    assert len(_entries(db_with_coa)) == 3


def test_ersatzsuche_gilt_auch_nicht_wenn_der_kandidat_mit_konto_nicht_passt(db_with_coa, tmp_path):
    """„Kandidat mit Konto“ heißt: eine Buchung mit gleichem Schlüssel (Datum, Betrag, Konto, Beleg-Nr.)
    gab es – auch wenn sie wegen ihres anderen Gegenkontos nicht übernommen wird. Dann sucht der
    Import nicht noch ersatzweise ohne Konto weiter, sondern legt den Satz neu an."""
    other_counter = _open_line(db_with_coa, coa=6815, counter=1800, text='mit Konto')
    without_account = _open_line(db_with_coa, text='mit Konto')
    before = {i: _row(db_with_coa, i) for i in (other_counter, without_account)}

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [dict(_STAMPS, ACCOUNTING_TEXT='mit Konto')]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert {i: _row(db_with_coa, i) for i in before} == before
    assert len(_entries(db_with_coa)) == 3


def test_ersatzsuche_gilt_je_belegnummer(db_with_coa, tmp_path):
    """Ob es zum Schlüssel einen Kandidaten mit Konto gab, entscheidet die Beleg-Nr.: eine Zeile mit
    Konto unter einer ANDEREN Nummer sperrt die Ersatzsuche nicht."""
    other_number = _open_line(db_with_coa, coa=6815, text='mit Konto', document_number='PR-A')
    without_account = _open_line(db_with_coa, document_number='PR-B')
    before = _row(db_with_coa, other_number)

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [dict(_STAMPS, REFERENCENO='PR-B')]))
    assert (result['imported'], result['adopted']) == (0, 1)
    row = _row(db_with_coa, without_account)
    assert (row['SourceGroup'], row['COA_ID']) == ('821', _coa(db_with_coa, 6815))
    assert _row(db_with_coa, other_number) == before


def test_ersatzsuche_gilt_je_belegnummer_nach_zusammenfassen_des_leerraums(db_with_coa, tmp_path):
    """…und „PR  7“ (zwei Leerzeichen) ist dieselbe Nummer wie „PR 7“: die Zeile mit Konto sperrt die
    Ersatzsuche, auch wenn sie ihres anderen Gegenkontos wegen selbst nicht übernommen wird."""
    with_account = _open_line(db_with_coa, coa=6815, counter=1800, text='mit Konto',
                              document_number='PR  7')
    without_account = _open_line(db_with_coa, document_number='PR 7')
    before = {i: _row(db_with_coa, i) for i in (with_account, without_account)}

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [dict(_STAMPS, REFERENCENO='PR 7')]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert {i: _row(db_with_coa, i) for i in before} == before


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
    """Eine Bankbewegung mit gleichem Datum, Betrag, Beleg-Nr., Text und Gegenkonto ist kein
    Buchungssatz – auch nicht ohne Konto und ohne ihr Bankkonto (das schon allein sie ausschlösse)."""
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
    eigene Erfassung des Nutzers und wird nicht mit einem WISO-Satz gleichgesetzt, auch nicht bei
    gleichem Text."""
    bank_id = _account_id(db_with_coa, 'Testbank')
    mask = _candidate(db_with_coa, account_id=bank_id)
    before = _row(db_with_coa, mask)

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert _row(db_with_coa, mask) == before
    assert len(_entries(db_with_coa)) == 2


def test_automirror_kind_der_maske_wird_nicht_uebernommen(db_with_coa, tmp_path):
    """Das Spiegelkind einer Bankbewegung aus der Maske („Druckerpapier −50“, 6815 an 1810) gehört zu
    dieser Bankbewegung – auch dann nicht übernommen, wenn der WISO-Satz denselben Text trägt. Beide
    existieren danach."""
    bank_id = _account_id(db_with_coa, 'Testkonto 2')                  # SKR 1810
    bank = db_with_coa.insert_booking('2024-03-12', -50.0, account_id=bank_id,
                                      text='Druckerpapier', booking_type='bank')
    mirror = db_with_coa.insert_booking(
        '2024-03-12', -50.0, coa_id=_coa(db_with_coa, 6815), counter_coa_id=_coa(db_with_coa, 1810),
        text='Druckerpapier', booking_type='entry', parent_booking_id=bank, auto_mirror=True)
    before = _row(db_with_coa, mirror)
    assert before['AutoMirror'] == 1 and before['Account_ID'] is None
    same_text = dict(ID=1, ACCOUNTINGID=841, ACCOUNTING_DATE='2024-03-12 00:00:00', AMOUNTGROSS=50.0,
                     ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210, ACCOUNTING_TEXT='Druckerpapier')

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [same_text]))
    assert (result['imported'], result['adopted']) == (1, 0)
    assert _row(db_with_coa, mirror) == before
    assert [r['Text'] for r in _entries(db_with_coa)] == ['Druckerpapier', 'Druckerpapier']


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


def test_manuelle_buchung_mit_gleichem_schluessel_text_und_gegenkonto_wird_uebernommen(
        db_with_coa, tmp_path):
    """Dokumentiertes Restrisiko: Eine von Hand erfasste Buchung mit gleicher Beleg-Nr., Datum, Konto,
    Betrag, Gegenkonto UND Text gilt als dieselbe Buchung (die Annahme trifft der CSV-Import schon).
    Sie bekommt die Quellgruppe und wird nicht doppelt angelegt."""
    manual = _candidate(db_with_coa, date_booking='2024-03-05', amount=-238.0, coa=6815,
                        counter=1800, document_number='PR-002', text='Probe Buero',
                        tax_rate=None, tax_amount=None)
    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (5, 1)

    row = _row(db_with_coa, manual)
    assert (row['SourceGroup'], row['Text']) == ('701', 'Probe Buero')
    assert len([r for r in _entries(db_with_coa) if r['DocumentNumber'] == 'PR-002']) == 1


def test_manuelle_buchung_mit_anderem_text_wird_nicht_uebernommen(db_with_coa, tmp_path):
    """…aber schon ein anderer Text genügt, damit aus „dieselbe Buchung“ zwei werden."""
    manual = _candidate(db_with_coa, date_booking='2024-03-05', amount=-238.0, coa=6815,
                        counter=1800, document_number='PR-002', text='von Hand erfasst',
                        tax_rate=None, tax_amount=None)
    before = _row(db_with_coa, manual)
    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert (result['imported'], result['adopted']) == (6, 0)
    assert _row(db_with_coa, manual) == before
    assert len([r for r in _entries(db_with_coa) if r['DocumentNumber'] == 'PR-002']) == 2


# ── Die Szenarien der fachlichen Prüfung ─────────────────────────────────────

def _editor_line(db, text, coa=6815, counter=1810, amount=-50.0):
    """Eine Teilzeile aus dem Buchungssatz-Editor: Kind einer Bankbewegung, Konto und Gegenkonto gesetzt,
    weder ``AutoMirror`` noch ``Account_ID`` (anders als die Spiegelbuchung der Maske)."""
    bank = db.insert_booking('2024-03-12', amount, account_id=_account_id(db, 'Testkonto 2'),
                             text='Überweisung Papierhandel', booking_type='bank')
    line = db.insert_booking('2024-03-12', amount, coa_id=_coa(db, coa) if coa else None,
                             counter_coa_id=_coa(db, counter), text=text, booking_type='entry',
                             parent_booking_id=bank)
    row = _row(db, line)
    assert (row['AutoMirror'], row['Account_ID'], row['ParentBooking_ID']) == (0, None, bank)
    return line


@pytest.mark.parametrize('wiso_text,adopted', [('Toner', False), ('Druckerpapier', True)],
                         ids=['Text anders', 'Text gleich'])
def test_teilzeile_aus_dem_editor_wird_nur_bei_gleichem_text_uebernommen(db_with_coa, tmp_path,
                                                                         wiso_text, adopted):
    """„Druckerpapier −50, 6815/1810“ als Teilzeile des Editors und der WISO-Satz „Toner“ mit gleichem
    Schlüssel und Gegenkonto sind zwei Buchungen – nur bei gleichem Text ist es eine."""
    line = _editor_line(db_with_coa, 'Druckerpapier')
    before = _row(db_with_coa, line)
    record = dict(ID=1, ACCOUNTINGID=841, ACCOUNTING_DATE='2024-03-12 00:00:00', AMOUNTGROSS=50.0,
                  ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210, ACCOUNTING_TEXT=wiso_text)

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [record]))
    if adopted:
        assert (result['imported'], result['adopted']) == (0, 1)
        assert _row(db_with_coa, line) == {**before, 'SourceGroup': '841'}
        assert len(_entries(db_with_coa)) == 1
    else:
        assert (result['imported'], result['adopted']) == (1, 0)
        assert _row(db_with_coa, line) == before
        assert sorted(r['Text'] for r in _entries(db_with_coa)) == ['Druckerpapier', 'Toner']
        assert [d['id'] for d in result['possible_duplicates']] == [line]


@pytest.mark.parametrize('wiso_text,adopted', [('Porto', False), ('noch offen', True)],
                         ids=['Text anders', 'Text gleich'])
def test_offene_teilzeile_ohne_konto_wird_nur_bei_gleichem_text_uebernommen(db_with_coa, tmp_path,
                                                                            wiso_text, adopted):
    """„noch offen −30“ ohne Konto, Gegenkonto 1810, und der WISO-Satz „Porto“ (4930/1810): nicht
    dieselbe Buchung. Hieße der Satz „noch offen“, würde die Zeile übernommen und kontiert."""
    line = _editor_line(db_with_coa, 'noch offen', coa=None, amount=-30.0)
    before = _row(db_with_coa, line)
    assert before['COA_ID'] is None
    record = dict(ID=1, ACCOUNTINGID=851, ACCOUNTING_DATE='2024-03-12 00:00:00', AMOUNTGROSS=30.0,
                  ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1210, ACCOUNTING_TEXT=wiso_text)

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [record]))
    if adopted:
        assert (result['imported'], result['adopted']) == (0, 1)
        assert _row(db_with_coa, line) == {**before, 'SourceGroup': '851',
                                           'COA_ID': _coa(db_with_coa, 6815)}
    else:
        assert (result['imported'], result['adopted']) == (1, 0)
        assert _row(db_with_coa, line) == before
        assert len(_entries(db_with_coa)) == 2


def _euer_total(db, number, year=2025):
    return dict((nr, total) for nr, _name, total in
                db.get_euer_data(f'{year}-01-01', f'{year}-12-31', None)).get(number)


def test_afa_der_app_wird_nicht_mit_einer_wiso_afa_verwechselt(db_with_coa, tmp_path):
    """Die AfA der App („AfA Laptop 2025“, 6220 an 0520, 400 €) und der WISO-Satz „AfA Drucker“ mit
    gleichem Datum, Betrag, Konto und Gegenkonto sind zwei Abschreibungen: nichts wird übernommen, und
    die EÜR enthält beide (800 € statt 400 €)."""
    db = db_with_coa
    asset = db.insert_asset('Laptop', '2025-01-10', 1200.0, 3, coa_id=_coa(db, 520))
    booking = db.book_depreciation(asset, 2025, coa_id_expense=_coa(db, 6220))
    before = _row(db, booking)
    assert (before['Account_ID'], before['AutoMirror'], before['Amount']) == (None, 0, 4000000)
    assert _euer_total(db, 6220) == -400.0

    record = dict(ID=1, ACCOUNTINGID=861, ACCOUNTING_DATE='2025-12-31 00:00:00', AMOUNTGROSS=400.0,
                  ACCOUNTNO=4830, CONTRA_ACCOUNTNO=320, ACCOUNTING_TEXT='AfA Drucker')
    result = _fdb_import(db, _fdb(tmp_path, [record]))

    assert (result['imported'], result['adopted']) == (1, 0)
    assert _row(db, booking) == before
    assert len(_entries(db)) == 2
    assert _euer_total(db, 6220) == -800.0


@pytest.mark.parametrize('text', ['Rechnung 100 € netto', 'Büro – Material', 'Abo Netflix…',
                                  'Test … Ende'],
                         ids=['Euro', 'Gedankenstrich', 'Auslassung am Ende', 'Auslassung in der Mitte'])
def test_text_mit_cp1252_zeichen_stimmt_in_csv_und_datenbank_ueberein(db_with_coa, tmp_path, text):
    """Die CSV ist cp1252, die WISO-Datenbank liefert dieselben Bytes als latin-1 gelesen (aus „€“ wird
    \\x80, aus „–“ \\x96, aus „…“ \\x85): beide Wege nennen denselben Text, die Zeile wird übernommen."""
    _csv_import(db_with_coa, [('12.03.2024', '6815', '1800', text, 'PR-C1', '30,00', '')])
    wiso_text = text.encode('cp1252').decode('latin-1')
    assert wiso_text != text
    record = dict(ID=1, ACCOUNTINGID=891, ACCOUNTING_DATE='2024-03-12 00:00:00', AMOUNTGROSS=30.0,
                  ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200, ACCOUNTING_TEXT=wiso_text,
                  REFERENCENO='PR-C1')

    result = _fdb_import(db_with_coa, _fdb(tmp_path, [record]))
    assert (result['imported'], result['adopted']) == (0, 1)
    (row,) = _entries(db_with_coa)
    assert (row['SourceGroup'], row['Text']) == ('891', text)


def test_text_mit_in_cp1252_unbelegten_bytes_bricht_den_import_nicht_ab(db_with_coa, tmp_path):
    """Die Bytes 0x81, 0x8D, 0x8F, 0x90 und 0x9D gibt es in cp1252 nicht. Steht eines in einem
    WISO-Text, darf die Umsetzung für den Vergleich daran nicht scheitern: Der Satz wird angelegt (und
    ist beim zweiten Lauf an seiner Quellgruppe wiederzuerkennen)."""
    record = dict(ID=1, ACCOUNTINGID=892, ACCOUNTING_DATE='2024-03-12 00:00:00', AMOUNTGROSS=30.0,
                  ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200, ACCOUNTING_TEXT='Abo \x81\x8d\x8f\x90\x9d Ende',
                  REFERENCENO='PR-C2')
    path = _fdb(tmp_path, [record])
    first = _fdb_import(db_with_coa, path)
    assert (first['imported'], first['adopted']) == (1, 0)
    second = _fdb_import(db_with_coa, path)
    assert (second['imported'], second['skipped']) == (0, 1)
    assert len(_entries(db_with_coa)) == 1


def test_umbuchung_mit_gleichem_text_und_gegenkonto_wird_uebernommen(db_with_coa, tmp_path):
    """„Umb. zu Re.“: WISO bucht zur Zahlung der Rechnung eine Umbuchung der Erlöse (4405 an 4400) mit
    demselben Betrag. Gleicher Text und Gegenkonto: übernommen – sonst stünde der Erlös doppelt da."""
    _csv_import(db_with_coa, [
        ('30.04.2024', '1810', '10000', 'Zahlung zu Re. 2024004', '2024004', '7500,00', ''),
        ('30.04.2024', '4405', '4400', 'Umb. zu Re. 2024004', '2024004', '7500,00', '')])
    records = [
        dict(ID=1, ACCOUNTINGID=500, ACCOUNTING_DATE='2024-04-30 00:00:00', AMOUNTGROSS=7500.0,
             ACCOUNTNO=1210, CONTRA_ACCOUNTNO=10000, ACCOUNTING_TEXT='Zahlung zu Re. 2024004',
             REFERENCENO='2024004'),
        dict(ID=2, ACCOUNTINGID=500, ACCOUNTING_DATE='2024-04-30 00:00:00', AMOUNTNET=6302.52,
             AMOUNTGROSS=7500.0, TAXRATE=19.0, ACCOUNTNO=8405, CONTRA_ACCOUNTNO=8400,
             ACCOUNTING_TEXT='Umb. zu Re. 2024004', REFERENCENO='2024004')]

    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))
    assert (result['imported'], result['adopted']) == (0, 2)
    assert len(_entries(db_with_coa)) == 2
    assert [r['SourceGroup'] for r in _entries(db_with_coa)] == ['500', '500']


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


def test_leerer_empfaenger_bleibt_leer_wenn_wiso_keinen_nennt(db_with_coa, tmp_path):
    """Nennt WISO keinen Empfänger, bleibt die Zeile, wie sie ist – auch ein leerer Text wird nicht zu
    NULL (die Übernahme ändert nur, was sie ergänzt)."""
    booking = _candidate(db_with_coa, recipient_client='')
    result = _fdb_import(db_with_coa, _fdb(tmp_path, [_record()]))
    assert result['adopted'] == 1
    assert _row(db_with_coa, booking)['RecipientClient'] == ''


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
    for skr03, skr04, text in CHART + ((4832, 6222, 'Abschreibungen auf Kfz'),):
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
