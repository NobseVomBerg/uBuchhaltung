# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""WISO-Datenbank-Import: was er nach einer Übernahme meldet (2.0.6).

Gegenstück zu ``test_wiso_csv_then_database.py`` (dort stehen die Regeln der Übernahme): hier das
Ergebnis (``adopted``, ``adopted_rows``, ``possible_duplicates``), das SQL-Protokoll, die
Transaktion, die Weiterleitung des Handlers und die Seite „Verschiedenes“ samt ihrem Skript – mit
feindlichen und kaputten Werten in der Ergebnisdatei.

Die Sätze und Helfer sind die aus ``test_wiso_csv_then_database.py``; alle Daten sind erfunden.
"""
import json
import os
import re
import shutil
import subprocess
from urllib.parse import parse_qs, quote, urlsplit

import pytest

import userctx
from server import handlers
from server.pages_miscellaneous import PageMiscellaneous
from tests.http_client import get, own_origin, post, send
from tests.test_wiso_csv_then_database import (CSV_ROWS, VERKAUF, _account_id, _candidate,  # noqa: F401
                                               _csv, _csv_import, _entries, _execute, _fdb,
                                               _fdb_import, _query, _record, _records, _row)

# ── Ergebnis: adopted_rows und SQL-Protokoll ─────────────────────────────────

def test_adopted_rows_nennen_die_uebernommenen_buchungen(db_with_coa, tmp_path):
    """Das Ergebnis führt jede übernommene Buchung auf: ID der vorhandenen Zeile, Datum, Betrag und
    den Text aus WISO (nicht den der CSV-Zeile)."""
    rows = [list(row) for row in CSV_ROWS]
    rows[VERKAUF][3] = 'probe   VERKAUF'                         # anders geschrieben, derselbe Text
    _csv_import(db_with_coa, [tuple(row) for row in rows])
    csv_ids = {r['Text']: r['ID'] for r in _entries(db_with_coa)}

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    listed = result['adopted_rows']
    assert len(listed) == result['adopted'] == 5
    assert {row['id'] for row in listed} == set(csv_ids.values())
    verkauf = [row for row in listed if row['id'] == csv_ids['probe   VERKAUF']][0]
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


# ── Ergebnis: possible_duplicates („möglicherweise doppelt“) ──────────────────

def test_moegliche_dubletten_nennen_id_datum_betrag_und_text(db_with_coa, tmp_path):
    """Eine Zeile, die als Kandidat zum Schlüssel gehörte, aber nicht übernommen wurde, obwohl für den
    Schlüssel ein Satz neu angelegt wurde, steht im Ergebnis – mit ID, Datum, Betrag und ihrem Text."""
    _csv_import(db_with_coa)
    _execute(db_with_coa, "UPDATE Bookings SET Text = 'Büro, vom Nutzer umbenannt'"
                          " WHERE Text = 'Probe Buero'")
    renamed = [r for r in _entries(db_with_coa) if r['Text'] == 'Büro, vom Nutzer umbenannt'][0]

    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert result['possible_duplicates_count'] == 1
    (entry,) = result['possible_duplicates']
    assert set(entry) >= {'id', 'date', 'amount', 'text'}
    assert (entry['id'], entry['date'], entry['text']) == (
        renamed['ID'], '2024-03-05', 'Büro, vom Nutzer umbenannt')
    assert entry['amount'] == -238.0                                   # in Euro, wie bei adopted_rows


def test_uebernommene_zeilen_sind_keine_moeglichen_dubletten(db_with_coa, tmp_path):
    """Drei Zeilen zum selben Schlüssel, zwei Sätze: der eine übernimmt „T1“, der andere (Text „T4“)
    wird neu angelegt. Gemeldet werden die beiden nicht übernommenen Zeilen, nicht die übernommene."""
    lines = [_candidate(db_with_coa, text=text) for text in ('T1', 'T2', 'T3')]
    records = [_record(ID=1, ACCOUNTINGID=901, ACCOUNTING_TEXT='T1'),
               _record(ID=2, ACCOUNTINGID=902, ACCOUNTING_TEXT='T4')]
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))

    assert (result['imported'], result['adopted']) == (1, 1)
    assert [d['id'] for d in result['possible_duplicates']] == lines[1:]
    assert result['possible_duplicates_count'] == 2
    assert [d['id'] for d in result['adopted_rows']] == [lines[0]]


def test_keine_moeglichen_dubletten_wenn_alles_uebernommen_oder_nichts_leer_blieb(db_with_coa, tmp_path):
    _csv_import(db_with_coa)
    result = _fdb_import(db_with_coa, _fdb(tmp_path))
    assert result['possible_duplicates'] == [] and result['possible_duplicates_count'] == 0


def test_moegliche_dubletten_sind_auf_500_begrenzt_der_zaehler_nicht(db_with_coa, tmp_path):
    count = 510
    _csv_import(db_with_coa, [('02.04.2024', '6815', '1800', f'CSV Zeile {n:03d}', f'PR-D{n:03d}',
                               f'{n + 1},00', '') for n in range(count)])
    records = [dict(ID=n + 1, ACCOUNTINGID=3000 + n, ACCOUNTING_DATE='2024-04-02 00:00:00',
                    AMOUNTGROSS=float(n + 1), ACCOUNTNO=4930, CONTRA_ACCOUNTNO=1200,
                    ACCOUNTING_TEXT=f'WISO Satz {n:03d}', REFERENCENO=f'PR-D{n:03d}')
               for n in range(count)]
    result = _fdb_import(db_with_coa, _fdb(tmp_path, records))
    assert (result['imported'], result['adopted']) == (count, 0)
    assert result['possible_duplicates_count'] == count
    assert len(result['possible_duplicates']) == 500


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
    assert (params['wiso_fdb'], params['imported'], params['skipped'], params['adopted'],
            params['possible']) == ('ok', '1', '0', '5', '0')
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'),
              encoding='utf-8') as handle:
        stored = json.load(handle)
    assert stored['adopted'] == 5 and stored['imported'] == 1
    assert len(stored['adopted_rows']) == 5 and stored['possible_duplicates'] == []

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
    rows = [list(row) for row in CSV_ROWS]
    rows[VERKAUF][3] = '<b>Verkauf</b> & Co'                      # derselbe Text, sonst keine Übernahme
    _csv_import(db_with_coa, [tuple(row) for row in rows])
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


def _possible(page):
    """Die Liste „Möglicherweise doppelt“: (Zähler in der Überschrift, Zeilen mit Zellentexten)."""
    found = re.findall(r'<details><summary>Möglicherweise doppelt \((.*?)\)</summary>(.*?)</details>',
                       page, re.S)
    assert len(found) == 1, found
    count, block = found[0]
    return count, [re.findall(r'<td>(.*?)</td>', row) for row in re.findall(r'<tr>(.*?)</tr>', block, re.S)
                   if '<td>' in row], block


def test_seite_listet_moegliche_dubletten_und_die_weiterleitung_nennt_sie(db_with_coa, tmp_path):
    """Eine CSV-Zeile, die nicht übernommen wurde, obwohl ein Satz neu dazukam, steht in einer zweiten
    aufklappbaren Liste samt Hinweis; die Weiterleitung nennt die Zahl (``possible``)."""
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    _csv_import(db_with_coa)
    _execute(db_with_coa, "UPDATE Bookings SET Text = 'Büro <b>umbenannt</b>' WHERE Text = 'Probe Buero'")
    renamed = [r for r in _entries(db_with_coa) if r['Text'] == 'Büro <b>umbenannt</b>'][0]

    status, location = handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [_fdb(tmp_path)]})
    params = _query_of(location)
    assert (params['imported'], params['adopted'], params['possible']) == ('2', '4', '1')

    page = PageMiscellaneous(db_with_coa)
    count, rows, block = _possible(page)
    assert count == '1' and rows == [['2024-03-05', '-238,00 \u20ac', 'Büro &lt;b&gt;umbenannt&lt;/b&gt;',
                                      str(renamed['ID'])]]
    assert '<b>umbenannt</b>' not in page and 'Bitte prüfen und eine von beiden löschen' in page
    assert 'Genannt werden die ersten' not in block


def test_seite_ohne_moegliche_dubletten_zeigt_keine_zweite_liste(db_with_coa, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    _csv_import(db_with_coa)
    handlers.handle_wiso_fdb_import(db_with_coa, {'fdb_path': [_fdb(tmp_path)]})
    assert 'Möglicherweise doppelt' not in PageMiscellaneous(db_with_coa)


@pytest.mark.parametrize('count,note', [(1, False), (499, False), (500, True)])
def test_hinweis_auf_die_ersten_500_gilt_auch_fuer_moegliche_dubletten(db_with_coa, count, note):
    _write_result({'possible_duplicates_count': count + 3, 'possible_duplicates': [
        {'id': n, 'date': '2024-04-02', 'amount': float(n), 'text': f'Zeile {n}'}
        for n in range(count)]})
    page = PageMiscellaneous(db_with_coa)
    shown, rows, _block = _possible(page)
    assert shown == str(count + 3) and len(rows) == count          # der Zähler nennt alle
    assert ('Genannt werden die ersten 500.' in page) is note


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


def _write_result(result):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), 'w',
              encoding='utf-8') as handle:
        json.dump(result, handle)


def test_kaputte_werte_in_der_ergebnisdatei_erscheinen_maskiert(db_with_coa):
    """Feindliche und kaputte Werte in ``adopted_rows`` (Markup, ``null``, Listen als Betrag, leere
    Zeile) lassen die Seite nicht abstürzen; Markup erscheint maskiert, kaputte Beträge als Text."""
    hostile = '<img src=x onerror=alert(1)>'
    _write_result({'adopted': 4, 'adopted_rows': [
        {'id': hostile, 'date': hostile, 'amount': hostile, 'text': hostile},
        {'id': None, 'date': None, 'amount': None, 'text': None},
        {'id': 3, 'date': '2024-01-01', 'amount': [1, 2], 'text': {'a': 1}},
        {}], 'possible_duplicates': [
        {'id': hostile, 'date': hostile, 'amount': hostile, 'text': hostile}]})
    page = PageMiscellaneous(db_with_coa)
    _count, possible_rows, _block = _possible(page)
    escaped = '&lt;img src=x onerror=alert(1)&gt;'
    assert possible_rows == [[escaped, escaped, escaped, escaped]]

    rows, block = _details(page)
    assert len(rows) == 4
    assert rows[0] == [escaped, escaped, escaped, escaped]
    assert rows[1] == ['', '-', '', ''] and rows[3] == ['', '-', '', '']
    assert rows[2][:2] == ['2024-01-01', '[1, 2]'] and rows[2][3] == '3'
    assert '<img' not in page and hostile not in page


@pytest.mark.parametrize('count,note', [(1, False), (499, False), (500, True), (501, True)])
def test_hinweis_auf_die_ersten_500_steht_ab_500_zeilen(db_with_coa, count, note):
    """Die Seite sagt „Genannt werden die ersten 500.“, sobald die Liste die Grenze erreicht."""
    _write_result({'adopted': count, 'adopted_rows': [
        {'id': n, 'date': '2024-04-02', 'amount': float(n), 'text': f'Zeile {n}'} for n in range(count)]})
    page = PageMiscellaneous(db_with_coa)
    rows, _block = _details(page)
    assert len(rows) == count
    assert ('Genannt werden die ersten 500.' in page) is note


#: Formen, die die App nie schreibt, die eine von Hand geänderte oder beschädigte Ergebnisdatei aber
#: haben kann: die Seite darf daran nicht scheitern
BROKEN_SHAPES = [['text'], [None], [5], [[1, 2]], 'abc', 5, {'a': 1}, None, True]


@pytest.mark.parametrize('key', ['adopted_rows', 'possible_duplicates'])
@pytest.mark.parametrize('rows', BROKEN_SHAPES,
                         ids=['Text', 'null', 'Zahl', 'Liste', 'String', 'Zahl statt Liste',
                              'Objekt statt Liste', 'null statt Liste', 'true statt Liste'])
def test_kaputte_form_der_ergebnisdatei_laesst_die_seite_nicht_abstuerzen(db_with_coa, key, rows):
    """Einträge, die kein Objekt sind, und Listen, die keine Liste sind, werden übergangen."""
    _write_result({'adopted': 1, 'possible_duplicates_count': 1, key: rows})
    page = PageMiscellaneous(db_with_coa)                 # darf nicht abstürzen
    assert '1 Buchungssätze waren schon aus dem CSV-Import da' in page


@pytest.mark.parametrize('key', ['adopted_rows', 'possible_duplicates'])
def test_kaputte_eintraege_werden_uebergangen_die_heilen_bleiben(db_with_coa, key):
    good = {'id': 7, 'date': '2024-01-01', 'amount': 1.5, 'text': 'gültig'}
    _write_result({'adopted': 1, 'possible_duplicates_count': 1,
                   key: ['text', None, 5, [1, 2], good, {'id': 8, 'text': 'auch gültig'}]})
    page = PageMiscellaneous(db_with_coa)
    shown = [cells for cells in re.findall(r'<tr><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>', page)
             if cells[2] in ('gültig', 'auch gültig')]
    assert sorted(cells[2] for cells in shown) == ['auch gültig', 'gültig']


def _fdb_script(page):
    """Der Skriptblock der Seite, der die Meldung nach dem Datenbank-Import baut."""
    scripts = re.findall(r'<script>(.*?)</script>', page, re.S)
    (script,) = [s for s in scripts if "p.get('wiso_fdb')" in s]
    return script


def test_meldungsskript_liest_adopted_als_zahl(db_with_coa):
    """Statisch: ``adopted`` und ``possible`` werden als Zahl gelesen und nur bei > 0 genannt."""
    script = _fdb_script(PageMiscellaneous(db_with_coa))
    assert "parseInt(p.get('adopted') || '0')" in script
    assert "parseInt(p.get('possible') || '0')" in script
    assert "if (pd > 0) msg += ', ' + pd + ' vorhandene Buchungen m" in script
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
    assert 'wiedererkannt' not in missing and 'doppelt' not in missing
    (possible,) = _shown_messages(script, base + '&adopted=5&possible=3', tmp_path)
    assert ', 3 vorhandene Buchungen möglicherweise doppelt – siehe Liste unten' in possible
    (none,) = _shown_messages(script, base + '&possible=0', tmp_path)
    assert 'doppelt' not in none


@pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – der statische Test '
                                         '(test_meldungsskript_liest_adopted_als_zahl) deckt es ab')
@pytest.mark.parametrize('value', ['<script>alert(1)</script>', '2<img src=x onerror=alert(1)>',
                                   '-3', ''])
def test_meldung_uebernimmt_keinen_rohen_wert_fuer_moegliche_dubletten(db_with_coa, tmp_path, value):
    script = _fdb_script(PageMiscellaneous(db_with_coa))
    (message,) = _shown_messages(
        script, '?wiso_fdb=ok&imported=1&skipped=0&assets=0&afa=0&possible=' + quote(value), tmp_path)
    for fragment in ('<script>', '<img', 'onerror', 'alert'):
        assert fragment not in message, message
    assert ('2 vorhandene Buchungen' in message) is value.startswith('2')


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
    listed, _block = _details(page.text)                       # und die Liste dazu
    assert sorted(row[0] for row in listed) == ['2024-03-01', '2024-03-05', '2024-03-05',
                                                '2024-03-05', '2024-03-15']

    second = post(server_port, '/wiso/fdb-import', headers={'Origin': own_origin(server_port)},
                  csrf=auto_login.csrf,
                  form={'fdb_path': path, 'with_assets': '1', 'with_invoices': '1'})
    params = _query_of(second.headers['Location'])
    assert (params['imported'], params['skipped'], params['adopted']) == ('0', '6', '0')
    assert len(_entries(db)) == 6
