# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""WISO-Datenbank-Import: von WISO neu angelegte Sätze (2.3.2) und der Import in einen vorhandenen Bestand.

Legt WISO die Buchungen eines Zeitraums neu an (nach einem DATEV-Export oder einer Korrektur), stehen sie mit **neuer**
``ACCOUNTINGID`` (→ ``Bookings.SourceGroup``) und sonst gleichem Inhalt in der Datenbank; die alten Quellgruppen gibt es
dort nicht mehr. Der Import erkannte einen Satz nur an seiner Quellgruppe wieder und legte alles ein zweites Mal an.
Jetzt gilt:

* **Verwaist** ist eine ``entry`` mit Quellgruppe, die in keinem Satz der Datei vorkommt (auch nicht in einem künftigen
  AfA-Satz). Jeder Satz mit **unbekannter** Quellgruppe bekommt – vor der Übernahme aus dem CSV-Import und vor dem Neuanlegen –
  eine verwaiste Zeile, wenn eine passt. Die Zuordnung steht vor der Hauptschleife fest und hängt nicht von der Reihenfolge der
  Sätze in der Datei ab (Durchgänge über alle Sätze in fester Ordnung nach Inhalt; jede Zeile höchstens einmal, jeder Satz höchstens
  eine Zeile): Jeder dieser Tests läuft deshalb, wo es darauf ankommt, in beiden Reihenfolgen.
* Immer nötig: gleiches Datum, Konto, Gegenkonto (leer = leer) und **gleicher Steuersatz**. Durchgang 1: gleicher Betrag (mit
  Vorzeichen) und gleiche, nicht leere Beleg-Nr., der Text darf abweichen (die Zeile mit gleichem Text geht vor); 2: beide ohne
  Beleg-Nr., gleicher nicht leerer Text; 3: die Zeile ohne Beleg-Nr., der Satz hat in WISO inzwischen eine bekommen, gleicher nicht
  leerer Text (die Beleg-Nr. der Zeile bleibt leer); 4 (nur Altbestand): Zeile ``mirror_legacy`` mit dem umgekehrten Betrag, sonst wie
  1 bis 3, und nur, wo die Datei selbst keinen Satz mit dem Betrag der Zeile hat. Kein Rückfall auf den Absolutbetrag.
* Ein Treffer ist **kein neuer Satz**: Die Zeile bekommt nur die neue ``SourceGroup`` (``regrouped_rows``; weicht der Text ab,
  zusätzlich ``regrouped_text_differs``) und gilt für alles Weitere wie eine in ihrer bekannten Gruppe wiedererkannte.
* Was von früheren Importen übrig bleibt (in WISO gelöscht oder mit anderem Inhalt neu angelegt), meldet der Lauf
  (``orphaned_rows_count``, die ersten 50 in ``orphaned_rows``), die Seite *Verschiedenes* und die Zusammenfassung nach dem Import
  (``regrouped``, ``orphans``); gelöscht wird nichts. Eine Änderung innerhalb einer weiter bestehenden Gruppe bemerkt der Import nicht.

Dazu (Paket 2.3.2, Teil 2): Der Import aus der WISO-Datenbank ist für einen **leeren Bestand** gedacht. Ist ``Bookings`` nicht
leer (``Database.wiso_import_stock``), zeigt das Formular einen Hinweis und ein Pflicht-Kästchen (``confirm_existing``), und
``handlers.handle_wiso_fdb_import`` importiert ohne Bestätigung nichts. ``Database.import_wiso_fdb`` selbst prüft das nicht.

Alle Namen, Nummern und Beträge sind erfunden; die Datenbanken baut ``tests/fdb_builder.py``.
"""
import inspect
import json
import os
import random
from contextlib import closing
from urllib.parse import parse_qs, quote, urlsplit

import pytest

import userctx
from db import Database
from db.wiso_fdb_import import MAX_ORPHANED_ROWS
from importers.wiso_fdb import read_wiso_database
from server import handlers
from server.pages_miscellaneous import (
    STOCK_CONFIRM_LABEL, STOCK_NOTICE_BOOKINGS, STOCK_NOTICE_WISO_ROWS, PageMiscellaneous, _import_notices)
from tests.entry_form_helpers import bank_account, csv_export, execute, query, wiso_database, wiso_row
from tests.http_client import get, own_origin, post
from tests.test_wiso_adoption_report import NODE, _fdb_script, _shown_messages
from tests.test_wiso_fdb_subaccounts_afa import CAR, JUNE, _afa_bookings, _afa_records, _fdb, _plan, _statuses
from tests.test_wiso_fdb_subaccounts_afa import run as run_with_assets


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Gruppen-Bank', 1800)
    return tmp_db


def _record(group, account, counter, gross, ref, text=None, date='2026-01-10', **extra):
    """Ein Satz der WISO-Datenbank (SKR03-Nummern) in der Quellgruppe ``group``."""
    return wiso_row(account, counter, gross, ref, text, ACCOUNTINGID=group,
                    ACCOUNTING_DATE=f'{date} 00:00:00', **extra)


def _sample(first):
    """Fünf Sätze in vier Quellgruppen ``first`` bis ``first + 3``: Die letzten beiden sind ein Beleg (eine Gruppe)."""
    return [_record(first, 4980, 1200, 119.0, 'S-1', date='2026-01-10', AMOUNTNET=100.0, TAXRATE=19.0),
            _record(first + 1, 4980, 1200, 59.5, 'S-2', date='2026-01-15', AMOUNTNET=50.0, TAXRATE=19.0),
            _record(first + 2, 1200, 8400, 500.0, 'S-3', date='2026-02-01'),
            _record(first + 3, 4980, 1200, 20.0, 'S-4', date='2026-02-10'),
            _record(first + 3, 4980, 1200, 30.0, 'S-4', date='2026-02-10')]


def _import(db, path, **kwargs):
    result = db.import_wiso_fdb(str(path), with_assets=False, with_invoices=False, **kwargs)
    assert result['errors'] == [] and result['blocker'] == [], result
    return result


def _groups(db):
    """Die Quellgruppen aller Buchungssätze nach ID."""
    return [group for (group,) in query(db, "SELECT SourceGroup FROM Bookings WHERE BookingType = 'entry' ORDER BY ID")]


def _without(db, *names):
    """Alle Spalten aller Buchungen nach ID – außer ``names``."""
    with closing(db._get_connection()) as conn:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(Bookings)') if row[1] not in names]
        return conn.execute(f'SELECT {", ".join(columns)} FROM Bookings ORDER BY ID').fetchall()


def _count(db):
    return query(db, 'SELECT COUNT(*) FROM Bookings')[0][0]


def _write_result(result):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), 'w', encoding='utf-8') as handle:
        json.dump(result, handle)


def _query_of(location):
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


#: Die Zuordnung hängt nicht von der Reihenfolge der Sätze in der Datei ab: Jeder dieser Tests läuft in beiden Reihenfolgen.
ORDERS = pytest.mark.parametrize('reverse', [False, True], ids=['file-order', 'reversed-file-order'])


def _ordered(rows, reverse):
    return rows[::-1] if reverse else list(rows)


# ═════════════════════════════════════════════════════════════════════════════
# 1 – Umhängen: derselbe Satz unter neuer Quellgruppe
# ═════════════════════════════════════════════════════════════════════════════

def test_a_satz_that_wiso_created_again_gets_the_new_group_and_is_not_created_a_second_time(db, tmp_path):
    old, new = wiso_database(tmp_path / 'a.fdb', _sample(801)), wiso_database(tmp_path / 'b.fdb', _sample(901))
    first = _import(db, old)
    assert (first['imported'], first['skipped'], first['regrouped_rows'], first['orphaned_rows_count']) == (5, 0, 0, 0)
    assert _groups(db) == ['801', '802', '803', '804', '804']
    stored = _without(db, 'SourceGroup')

    second = _import(db, new)

    assert (second['imported'], second['skipped'], second['regrouped_rows'], second['regrouped_text_differs']) == (0, 5, 5, 0)
    assert (second['orphaned_rows_count'], second['orphaned_rows']) == (0, [])
    assert (second['adopted'], second['possible_duplicates_count'], second['form_conflicts_count']) == (0, 0, 0)
    assert _groups(db) == ['901', '902', '903', '904', '904']
    assert _without(db, 'SourceGroup') == stored                      # jede andere Spalte jeder Zeile, auch die ID

    third = _import(db, new)                                            # ein zweiter Lauf mit derselben Datei
    assert (third['imported'], third['skipped'], third['regrouped_rows']) == (0, 5, 0)
    assert _groups(db) == ['901', '902', '903', '904', '904'] and _without(db, 'SourceGroup') == stored

    back = _import(db, old)                                             # und zurück: Die Zeilen hängen wieder um
    assert (back['imported'], back['skipped'], back['regrouped_rows']) == (0, 5, 5)
    assert _groups(db) == ['801', '802', '803', '804', '804'] and _without(db, 'SourceGroup') == stored

    again = _import(db, new)                                            # A → B → B → A → B: weiter stabil
    assert (again['imported'], again['skipped'], again['regrouped_rows'], again['orphaned_rows_count']) == (0, 5, 5, 0)
    assert _groups(db) == ['901', '902', '903', '904', '904'] and _without(db, 'SourceGroup') == stored


def test_a_first_import_and_a_repeat_of_the_same_database_count_as_before(db, tmp_path):
    path = wiso_database(tmp_path / 'a.fdb', _sample(801))

    first = _import(db, path)
    repeat = _import(db, path)

    assert (first['imported'], first['skipped'], first['regrouped_rows'], first['regrouped_text_differs']) == (5, 0, 0, 0)
    assert (first['orphaned_rows_count'], first['orphaned_rows']) == (0, [])
    assert (repeat['imported'], repeat['skipped'], repeat['regrouped_rows'], repeat['regrouped_text_differs']) == (0, 5, 0, 0)
    assert (repeat['orphaned_rows_count'], repeat['orphaned_rows']) == (0, [])
    assert _count(db) == 5


# ═════════════════════════════════════════════════════════════════════════════
# 2 – Geänderte Fassung: neu angelegt, die alten Zeilen bleiben und werden gemeldet
# ═════════════════════════════════════════════════════════════════════════════

def _months(first, amounts, tag, text=None):
    """Je Monat (Januar bis März) Sätze mit diesen Beträgen; Beleg-Nr. und Betrag unterscheiden die Fassungen."""
    rows = []
    for month in (1, 2, 3):
        for number, amount in enumerate(amounts, start=1):
            rows.append(_record(first + len(rows), 4980, 1200, amount, f'{tag}{month}-{number}', text,
                                date=f'2026-0{month}-05'))
    return rows


def test_a_changed_version_is_created_new_and_the_old_rows_are_reported_not_deleted(db, tmp_path):
    """Aus drei Sätzen je Monat wurden zwei andere: Keine Zeile passt, die zwei neuen werden angelegt, die drei alten stehen
    weiter in den Büchern und im Ergebnis."""
    _import(db, wiso_database(tmp_path / 'a.fdb', _months(810, (10.0, 20.0, 30.0), 'A')))
    old = _without(db)

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _months(910, (25.0, 35.0), 'B')))

    assert (result['imported'], result['skipped'], result['regrouped_rows']) == (6, 0, 0)
    assert result['orphaned_rows_count'] == 9 and len(result['orphaned_rows']) == 9
    expected = []
    for month in (1, 2, 3):
        for item, amount in enumerate((10.0, 20.0, 30.0), start=1):
            expected.append({'id': len(expected) + 1, 'date': f'2026-0{month}-05', 'amount': -amount, 'account': 6850,
                             'counter_account': 1800, 'text': f'Posten A{month}-{item}'})
    assert result['orphaned_rows'] == expected
    assert _count(db) == 15 and _without(db)[:9] == old                  # gelöscht und geändert ist nichts


def test_the_page_names_the_orphaned_rows_with_a_link_and_masks_every_value(db, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    rows = _months(810, (10.0, 20.0, 30.0), 'A')
    rows[0]['ACCOUNTING_TEXT'] = '<b>fett</b> & Co'
    _import(db, wiso_database(tmp_path / 'a.fdb', rows))
    path = wiso_database(tmp_path / 'b.fdb', _months(910, (25.0, 35.0), 'B'))

    status, location = handlers.handle_wiso_fdb_import(db, {'fdb_path': [path], 'confirm_existing': ['1']})

    assert status == 303 and _query_of(location)['wiso_fdb'] == 'ok'
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), encoding='utf-8') as handle:
        stored = json.load(handle)
    assert stored['orphaned_rows_count'] == 9 and len(stored['orphaned_rows']) == 9
    page = PageMiscellaneous(db)
    assert '9 Buchungen aus einem früheren Import stehen nicht mehr in der WISO-Datenbank (dort gelöscht oder mit ' \
           'anderem Inhalt neu angelegt). Sie zählen in den Auswertungen weiter mit &ndash; bitte prüfen und löschen, was ' \
           'doppelt ist.' in page
    assert 'Nicht mehr in WISO vorhandene Buchungen &ndash; 9' in page
    for booking_id in range(1, 10):
        assert f'<a href="/transactions/edit?id={booking_id}">{booking_id}</a>' in page
    assert '&lt;b&gt;fett&lt;/b&gt; &amp; Co' in page and '<b>fett</b>' not in page
    assert 'Genannt werden die ersten' not in page                       # alle neun stehen in der Liste


def test_the_page_says_nothing_about_orphans_when_there_are_none(db, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    path = wiso_database(tmp_path / 'a.fdb', _sample(801))
    handlers.handle_wiso_fdb_import(db, {'fdb_path': [path]})
    handlers.handle_wiso_fdb_import(db, {'fdb_path': [path], 'confirm_existing': ['1']})

    page = PageMiscellaneous(db)

    assert 'Nicht mehr in WISO vorhandene Buchungen' not in page and 'neu angelegte WISO-Buchungen' not in page


# ═════════════════════════════════════════════════════════════════════════════
# 3 – Teilweise: je Satz der richtige Weg
# ═════════════════════════════════════════════════════════════════════════════

def test_each_satz_takes_its_own_way_unchanged_created_again_changed_and_new(db, tmp_path):
    unchanged = _record(801, 4980, 1200, 10.0, 'P-1', date='2026-03-01')
    again_old = _record(802, 4980, 1200, 20.0, 'P-2', date='2026-03-02')
    changed_old = _record(803, 4980, 1200, 30.0, 'P-3', date='2026-03-03')
    _import(db, wiso_database(tmp_path / 'a.fdb', [unchanged, again_old, changed_old]))
    stored = _without(db, 'SourceGroup')

    again_new = _record(902, 4980, 1200, 20.0, 'P-2', date='2026-03-02')            # neu angelegt: neue Gruppe, derselbe Inhalt
    changed_new = _record(903, 4980, 1200, 31.0, 'P-3', date='2026-03-03')          # geändert: anderer Betrag
    brand_new = _record(904, 4980, 1200, 40.0, 'P-4', date='2026-03-04')            # nie importiert
    result = _import(db, wiso_database(tmp_path / 'b.fdb', [unchanged, again_new, changed_new, brand_new]))

    assert (result['imported'], result['skipped'], result['regrouped_rows']) == (2, 2, 1)
    assert [row['id'] for row in result['orphaned_rows']] == [3] and result['orphaned_rows_count'] == 1
    assert _groups(db) == ['801', '902', '803', '903', '904']
    assert _without(db, 'SourceGroup')[:3] == stored                                  # die drei alten Zeilen bleiben, wie sie waren


# ═════════════════════════════════════════════════════════════════════════════
# 4 – Gleiche Sätze: jede Zeile höchstens einem Satz, jeder Satz höchstens eine Zeile
# ═════════════════════════════════════════════════════════════════════════════

def _alike(*groups):
    """Gleiche Sätze (Datum, Konten, Betrag, Beleg-Nr. und Text), je einer in einer eigenen Quellgruppe."""
    return [_record(group, 4980, 1200, 25.0, 'G-1', date='2026-04-01') for group in groups]


@ORDERS
def test_two_alike_satze_created_again_take_two_rows_and_create_none(db, tmp_path, reverse):
    """Gleiche Sätze bekommen die Zeilen in fester Ordnung (nach ihrer Gruppe, die Zeilen nach ID) – nicht in der Reihenfolge,
    in der die Datei sie nennt."""
    _import(db, wiso_database(tmp_path / 'a.fdb', _alike(801, 802)))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _ordered(_alike(901, 902), reverse)))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (0, 2, 0)
    assert _groups(db) == ['901', '902'] and _count(db) == 2


@ORDERS
def test_three_old_rows_against_two_new_satze_take_two_and_leave_one_orphaned(db, tmp_path, reverse):
    _import(db, wiso_database(tmp_path / 'a.fdb', _alike(801, 802, 803)))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _ordered(_alike(901, 902), reverse)))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (0, 2, 1)
    assert [row['id'] for row in result['orphaned_rows']] == [3]
    assert _groups(db) == ['901', '902', '803'] and _count(db) == 3


def test_two_old_rows_against_three_new_satze_take_two_and_create_one(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', _alike(801, 802)))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _alike(901, 902, 903)))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 2, 0)
    assert _groups(db) == ['901', '902', '903'] and _count(db) == 3


# ═════════════════════════════════════════════════════════════════════════════
# 5 – Text und Steuersatz entscheiden unter mehreren Kandidaten
# ═════════════════════════════════════════════════════════════════════════════

def test_a_changed_text_still_finds_its_row_and_the_text_of_the_app_stays(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 1200, 19.0, 'X-1', 'Alter Text')]))
    stored = _without(db, 'SourceGroup')

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 19.0, 'X-1', 'Neuer Text')]))

    assert (result['imported'], result['regrouped_rows'], result['regrouped_text_differs']) == (0, 1, 1)
    assert _without(db, 'SourceGroup') == stored and _groups(db) == ['901']
    assert query(db, 'SELECT Text FROM Bookings') == [('Alter Text',)]


@pytest.mark.parametrize('stored, text', [('beta', 'beta'), ('BETA', 'beta'), (' beta  ', 'beta'), ('be  ta', 'be ta'),
                                          ('bet…a', 'bet a')])
def test_among_two_candidates_the_one_with_the_same_text_wins(db, tmp_path, stored, text):
    """Zwei sonst gleiche Zeilen: Der Satz nimmt die mit gleichem Text (Leerraum und Groß-/Kleinschreibung zählen nicht, das
    Auslassungszeichen gilt als Leerraum – wie bei der Übernahme aus dem CSV-Import), auch wenn die andere die kleinere ID
    hat. Der Leser faltet Leerraum schon beim Lesen; der Bestand trägt ihn wie ein CSV-Import oder eine Eingabe von Hand."""
    old = [_record(801, 4980, 1200, 19.0, 'X-2', 'alpha'), _record(802, 4980, 1200, 19.0, 'X-2', 'beta')]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))
    execute(db, 'UPDATE Bookings SET Text = ? WHERE ID = 2', (stored,))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 19.0, 'X-2', text)]))

    assert (result['imported'], result['regrouped_rows'], result['regrouped_text_differs']) == (0, 1, 0)
    assert _groups(db) == ['801', '901'] and [row['id'] for row in result['orphaned_rows']] == [1]
    assert query(db, 'SELECT Text FROM Bookings WHERE ID = 2') == [(stored,)]              # der Text der App bleibt


def test_a_text_with_a_latin1_control_character_equals_itself_like_in_the_csv_adoption(db, tmp_path):
    """Die WISO-Datenbank liefert „€“ als Byte 0x80, das als latin-1 gelesen U+0080 ergibt; der Satz wird für den Vergleich wie bei
    der Übernahme aus dem CSV-Import nach cp1252 umgesetzt. Die Zeile eines früheren Imports derselben Datenbank trägt den Text
    unverändert und muss sich dabei noch gleichen – sonst gälte derselbe Text als geändert."""
    text = 'Preis 5 \x80 netto'
    old = [_record(801, 4980, 1200, 19.0, 'X-6', 'anders'), _record(802, 4980, 1200, 19.0, 'X-6', text)]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 19.0, 'X-6', text)]))

    assert (result['regrouped_rows'], result['regrouped_text_differs']) == (1, 0)
    assert _groups(db) == ['801', '901'] and query(db, 'SELECT Text FROM Bookings WHERE ID = 2') == [(text,)]


def test_the_text_of_an_adopted_csv_row_equals_the_text_of_the_satz_in_the_cp1252_reading(db, tmp_path):
    """Eine Zeile, die der Import aus einer CSV-Zeile übernommen hat, trägt deren Text (cp1252: „€“): Er gleicht dem Text des
    Satzes mit Byte 0x80."""
    old = [_record(801, 4980, 1200, 19.0, 'X-7', 'anders'), _record(802, 4980, 1200, 19.0, 'X-7', 'Preis 5 \x80 netto')]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))
    execute(db, 'UPDATE Bookings SET Text = ? WHERE ID = 2', ('Preis 5 € netto',))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 19.0, 'X-7', 'Preis 5 \x80 netto')]))

    assert (result['regrouped_rows'], result['regrouped_text_differs']) == (1, 0) and _groups(db) == ['801', '901']


def test_the_tax_rate_is_a_must_and_the_smallest_id_decides_among_the_rows_with_the_same_rate(db, tmp_path):
    """Ein anderer Steuersatz ist nie ein Treffer – auch nicht für die Zeile mit der kleinsten ID. Unter den Zeilen mit dem
    Satz des WISO-Satzes (hier die Sätze mit abweichendem Text) entscheidet die kleinste ID."""
    old = [_record(801, 4980, 1200, 107.0, 'X-3', 'eins', AMOUNTNET=89.92, TAXRATE=19.0),
           _record(802, 4980, 1200, 107.0, 'X-3', 'zwei', AMOUNTNET=100.0, TAXRATE=7.0),
           _record(803, 4980, 1200, 107.0, 'X-3', 'drei', AMOUNTNET=100.0, TAXRATE=7.0)]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))
    new = [_record(901, 4980, 1200, 107.0, 'X-3', 'neu', AMOUNTNET=100.0, TAXRATE=7.0)]

    result = _import(db, wiso_database(tmp_path / 'b.fdb', new))

    assert (result['imported'], result['regrouped_rows'], result['regrouped_text_differs']) == (0, 1, 1)
    assert _groups(db) == ['801', '901', '803']                           # die erste Zeile mit 7 %, nicht die ID 1 mit 19 %


def test_a_changed_tax_rate_is_no_match_the_satz_is_created_with_its_own_rate_and_tax(db, tmp_path):
    """In WISO wurde der Satz von 19 auf 7 % geändert: Der Satz ist neu (mit 7 % und seinem Steuerbetrag), die alte Zeile
    bleibt verwaist und steht in der Liste – auch bei gleicher Beleg-Nr., gleichem Text und gleichem Betrag."""
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 1200, 119.0, 'X-4', AMOUNTNET=100.0, TAXRATE=19.0)]))
    stored = _without(db, 'SourceGroup', 'DatevKey')

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [
        _record(901, 4980, 1200, 119.0, 'X-4', AMOUNTNET=111.21, TAXRATE=7.0, DATEVVATKEY=402)], key_column=True))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 0, 1)
    assert (result['datev_keys_filled'], result['datev_key_rate_mismatches']) == (0, 0)
    assert query(db, 'SELECT SourceGroup, TaxRate, TaxAmount, DatevKey FROM Bookings ORDER BY ID') == [
        ('801', 0.19, -190000, None), ('901', 0.07, -77900, 402)]
    assert _without(db, 'SourceGroup', 'DatevKey')[:1] == stored


def test_a_tax_rate_of_nothing_and_of_zero_percent_are_the_same(db, tmp_path):
    """Wie bei der wiedererkannten Zeile einer bekannten Gruppe: kein Satz und 0 % gelten als gleich."""
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 1200, 50.0, 'X-8')]))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 50.0, 'X-8', AMOUNTNET=50.0, TAXRATE=0.0)]))

    assert (result['imported'], result['regrouped_rows']) == (0, 1)


# ═════════════════════════════════════════════════════════════════════════════
# 6 – BU-Schlüssel wie bei einer wiedererkannten Zeile einer bekannten Gruppe
# ═════════════════════════════════════════════════════════════════════════════

def _keys(db):
    return dict(query(db, "SELECT DocumentNumber, DatevKey FROM Bookings WHERE BookingType = 'entry'"))


def test_the_key_goes_to_a_regrouped_row_where_it_is_empty(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 1200, 119.0, 'B-1', AMOUNTNET=100.0, TAXRATE=19.0)]))
    assert _keys(db) == {'B-1': None}
    before = _without(db, 'SourceGroup', 'DatevKey')
    new = [_record(901, 4980, 1200, 119.0, 'B-1', AMOUNTNET=100.0, TAXRATE=19.0, DATEVVATKEY=401)]

    result = _import(db, wiso_database(tmp_path / 'b.fdb', new, key_column=True))

    assert (result['imported'], result['regrouped_rows']) == (0, 1)
    assert (result['datev_keys_filled'], result['datev_key_rate_mismatches']) == (1, 0)
    assert _keys(db) == {'B-1': 401} and _without(db, 'SourceGroup', 'DatevKey') == before


@ORDERS
def test_two_alike_satze_of_different_rates_get_the_row_and_the_key_of_their_own_rate(db, tmp_path, reverse):
    """Der Steuersatz ist Pflicht: Der Schlüssel eines Satzes hängt nie an der Zeile eines anderen Satzes – gleich, in welcher
    Reihenfolge die Datei die Sätze nennt."""
    old = [_record(801, 4980, 1200, 119.0, 'B-2', AMOUNTNET=100.0, TAXRATE=19.0),
           _record(802, 4980, 1200, 119.0, 'B-2', AMOUNTNET=111.21, TAXRATE=7.0)]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))
    new = [_record(901, 4980, 1200, 119.0, 'B-2', AMOUNTNET=100.0, TAXRATE=19.0, DATEVVATKEY=401),
           _record(902, 4980, 1200, 119.0, 'B-2', AMOUNTNET=111.21, TAXRATE=7.0, DATEVVATKEY=402)]

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _ordered(new, reverse), key_column=True))

    assert (result['imported'], result['regrouped_rows'], result['datev_keys_filled']) == (0, 2, 2)
    assert result['datev_key_rate_mismatches'] == 0
    assert query(db, 'SELECT TaxRate, SourceGroup, DatevKey FROM Bookings ORDER BY ID') == [
        (0.19, '901', 401), (0.07, '902', 402)]


def test_a_key_that_is_there_stays_on_a_regrouped_row(db, tmp_path):
    old = [_record(801, 4980, 1200, 119.0, 'B-3', AMOUNTNET=100.0, TAXRATE=19.0, DATEVVATKEY=402)]
    _import(db, wiso_database(tmp_path / 'a.fdb', old, key_column=True))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [dict(old[0], ACCOUNTINGID=901, DATEVVATKEY=401)], key_column=True))

    assert (result['regrouped_rows'], result['datev_keys_filled'], result['datev_key_rate_mismatches']) == (1, 0, 0)
    assert _keys(db) == {'B-3': 402}


# ═════════════════════════════════════════════════════════════════════════════
# 7 – Was nicht genau gleicht, wird nicht umgehängt
# ═════════════════════════════════════════════════════════════════════════════

def test_a_row_the_user_changed_is_no_match_the_satz_is_created_and_the_old_row_reported(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', _sample(801)[:3]))
    row_id = query(db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'S-1'")[0][0]
    db.update_booking_fields(row_id, coa_id=db.get_coa_id_by_account_number(6815))
    before = _without(db)

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _sample(901)[:3]))

    assert (result['imported'], result['regrouped_rows']) == (1, 2)
    assert result['orphaned_rows_count'] == 1
    assert [(row['id'], row['account'], row['counter_account']) for row in result['orphaned_rows']] == [(row_id, 6815, 1800)]
    assert _without(db)[:3][0] == before[0] and _count(db) == 4


@pytest.mark.parametrize('field, change', [
    ('date', dict(date='2026-01-11')), ('amount', dict(gross=118.0)), ('account', dict(account=8400)),
    ('counter', dict(counter=1360)), ('document', dict(ref='D-2'))])
def test_a_difference_in_date_amount_account_counter_account_or_document_number_is_no_match(db, tmp_path, field, change):
    fields = dict(account=4980, counter=1200, gross=119.0, ref='D-1', date='2026-01-10')
    old = _record(801, fields['account'], fields['counter'], fields['gross'], fields['ref'], date=fields['date'])
    _import(db, wiso_database(tmp_path / 'a.fdb', [old]))
    fields.update(change)

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [
        _record(901, fields['account'], fields['counter'], fields['gross'], fields['ref'], date=fields['date'])]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 0, 1), field
    assert _groups(db) == ['801', '901']


def test_blanks_in_the_document_number_do_not_count_and_an_empty_one_matches_an_empty_one(db, tmp_path):
    old = [_record(801, 4980, 1200, 10.0, 'RE 12'), _record(802, 4980, 1200, 11.0, '')]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))
    execute(db, "UPDATE Bookings SET DocumentNumber = 'RE  12' WHERE ID = 1")             # wie ein Textimport sie schreibt

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 10.0, 'RE 12'),
                                                          _record(902, 4980, 1200, 11.0, '')]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (0, 2, 0)
    assert query(db, 'SELECT DocumentNumber FROM Bookings ORDER BY ID') == [('RE  12',), (None,)]       # der Bestand bleibt


# ── Eine Beleg-Nr., die in WISO nachgetragen wurde ───────────────────────────

def _numbered(group, ref, text='Rechnung Maler', gross=119.0, **extra):
    return _record(group, 4980, 1200, gross, ref, text, date='2026-05-04', **extra)


def test_a_document_number_added_in_wiso_still_finds_the_row_without_one_and_leaves_it_empty(db, tmp_path):
    """Die alte Zeile trägt keine Beleg-Nr., der neu angelegte Satz eine: Bei gleichem, nicht leerem Text passt sie. Nur
    ``SourceGroup`` (und, wo er fehlt und der Satz gleicht, der BU-Schlüssel) ändert sich – die Beleg-Nr. der Zeile bleibt leer."""
    _import(db, wiso_database(tmp_path / 'a.fdb', [_numbered(801, '', AMOUNTNET=100.0, TAXRATE=19.0)]))
    assert query(db, 'SELECT DocumentNumber, Text FROM Bookings') == [(None, 'Rechnung Maler')]
    stored = _without(db, 'SourceGroup', 'DatevKey')
    new = wiso_database(tmp_path / 'b.fdb', [_numbered(901, 'R-77', AMOUNTNET=100.0, TAXRATE=19.0, DATEVVATKEY=401)],
                        key_column=True)

    result = _import(db, new)

    assert (result['imported'], result['regrouped_rows'], result['regrouped_text_differs']) == (0, 1, 0)
    assert (result['orphaned_rows_count'], result['datev_keys_filled'], result['datev_key_rate_mismatches']) == (0, 1, 0)
    assert query(db, 'SELECT DocumentNumber, SourceGroup, DatevKey FROM Bookings') == [(None, '901', 401)]
    assert _without(db, 'SourceGroup', 'DatevKey') == stored
    again = _import(db, new)                                     # die Gruppe ist jetzt bekannt: nichts mehr zu tun
    assert (again['imported'], again['skipped'], again['regrouped_rows']) == (0, 1, 0)
    assert query(db, 'SELECT DocumentNumber FROM Bookings') == [(None,)]


def test_a_document_number_added_in_wiso_needs_the_same_text(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', [_numbered(801, '')]))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_numbered(901, 'R-77', text='Rechnung Schreiner')]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 0, 1)
    assert _groups(db) == ['801', '901']


def test_a_document_number_added_in_wiso_needs_a_text_that_is_not_empty(db, tmp_path):
    """Zwei leere Texte beweisen nichts."""
    _import(db, wiso_database(tmp_path / 'a.fdb', [_numbered(801, '', ACCOUNTING_TEXT='')]))
    assert query(db, 'SELECT Text FROM Bookings') == [('',)]

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_numbered(901, 'R-77', ACCOUNTING_TEXT='')]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 0, 1)


def test_a_row_with_a_document_number_does_not_match_a_satz_without_one(db, tmp_path):
    """Der umgekehrte Fall passt nicht: Die App-Zeile trägt eine Beleg-Nr., der Satz von WISO keine."""
    _import(db, wiso_database(tmp_path / 'a.fdb', [_numbered(801, 'R-77')]))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_numbered(901, '')]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 0, 1)
    assert _groups(db) == ['801', '901']


def test_two_different_document_numbers_never_match_even_with_the_same_text(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', [_numbered(801, 'R-77')]))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_numbered(901, 'R-78')]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 0, 1)


def test_among_rows_without_a_document_number_only_the_one_with_the_same_text_is_a_candidate(db, tmp_path):
    old = [_numbered(801, '', text='Rechnung Elektriker'), _numbered(802, '', text='Rechnung Maler')]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_numbered(901, 'R-77')]))

    assert (result['imported'], result['regrouped_rows'], result['regrouped_text_differs']) == (0, 1, 0)
    assert _groups(db) == ['801', '901'] and [row['id'] for row in result['orphaned_rows']] == [1]       # nicht die kleinere ID


@ORDERS
def test_the_same_document_number_wins_against_a_row_without_one(db, tmp_path, reverse):
    """Die gleiche Beleg-Nr. geht vor – auch gegen die kleinere ID."""
    old = [_numbered(801, ''), _numbered(802, 'R-77')]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _ordered([_numbered(901, 'R-77')], reverse)))

    assert (result['imported'], result['regrouped_rows'], result['regrouped_text_differs']) == (0, 1, 0)
    assert _groups(db) == ['801', '901'] and [row['id'] for row in result['orphaned_rows']] == [1]


def test_the_same_document_number_wins_even_when_its_text_differs(db, tmp_path):
    """Durchgang 1 (gleiche Beleg-Nr., der Text darf abweichen) kommt vor Durchgang 3 (Beleg-Nr. in WISO nachgetragen, gleicher
    Text): Die Zeile mit der gleichen Nummer gewinnt, ihr Text bleibt, und die Abweichung wird gezählt."""
    old = [_numbered(801, '', text='Rechnung Maler'), _numbered(802, 'R-77', text='Rechnung Malermeister')]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_numbered(901, 'R-77', text='Rechnung Maler')]))

    assert (result['imported'], result['regrouped_rows'], result['regrouped_text_differs']) == (0, 1, 1)
    assert _groups(db) == ['801', '901'] and query(db, 'SELECT Text FROM Bookings WHERE ID = 2') == [('Rechnung Malermeister',)]


def test_a_document_number_added_in_wiso_counts_like_any_regrouping_in_the_notices(db, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    _import(db, wiso_database(tmp_path / 'a.fdb', [_numbered(801, '')]))

    handlers.handle_wiso_fdb_import(db, {'fdb_path': [wiso_database(tmp_path / 'b.fdb', [_numbered(901, 'R-77')])],
                                         'confirm_existing': ['1']})

    page = PageMiscellaneous(db)
    assert '1 neu angelegte WISO-Buchungen wurden wiedererkannt und nicht doppelt angelegt.' in page
    assert 'Nicht mehr in WISO vorhandene Buchungen' not in page


def test_a_satz_of_a_known_group_never_takes_an_orphaned_row(db, tmp_path):
    """Nur eine **unbekannte** Quellgruppe sucht unter den verwaisten Zeilen. Ein Satz einer bekannten Gruppe, dessen Zeile der
    Nutzer geändert hat, wird nicht neu angelegt und nimmt nie eine fremde Zeile."""
    old = [_record(801, 4980, 1200, 25.0, 'K-1'), _record(802, 4980, 1200, 25.0, 'K-1')]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))
    execute(db, 'UPDATE Bookings SET COA_ID = ? WHERE ID = 1', (db.get_coa_id_by_account_number(6815),))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [old[0]]))                     # Gruppe 801 ist bekannt

    assert (result['imported'], result['skipped'], result['regrouped_rows']) == (0, 1, 0)
    assert _groups(db) == ['801', '802'] and result['orphaned_rows_count'] == 1


def test_a_satz_without_a_group_takes_no_part(db, tmp_path):
    """Ohne ``ACCOUNTINGID`` gibt es keine neue Gruppe zu setzen: Der Satz geht den bisherigen Weg."""
    old = _record(801, 4980, 1200, 25.0, 'N-1')
    _import(db, wiso_database(tmp_path / 'a.fdb', [old]))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [dict(old, ACCOUNTINGID=None)]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 0, 1)


def test_a_deviating_form_of_the_same_amount_is_only_reported(db, tmp_path):
    """Wie bei einer bekannten Gruppe: Zählt ein Konto inzwischen als Zahlungskonto, kann die Form des Satzes abweichen. Die Zeile
    bleibt, wie sie ist, und die abweichende Form wird nur gemeldet."""
    chart = ((1210, 1810, 'Zweite Bank'),)
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 1210, 8400, 100.0, 'V-1')], extra_chart=chart))
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings') == [(1000000, 'transfer')]
    bank_account(db, 'Zweite Bank', 1810)

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 1210, 8400, 100.0, 'V-1')], extra_chart=chart))

    assert (result['imported'], result['regrouped_rows'], result['form_conflicts_count']) == (0, 1, 1)
    assert (result['form_conflicts'][0]['stored'], result['form_conflicts'][0]['fresh']) == ('transfer', 'liquid_first')
    assert query(db, 'SELECT Amount, EntryForm, SourceGroup FROM Bookings') == [(1000000, 'transfer', '901')]


def test_a_mirror_of_the_stock_is_counted_not_changed_when_it_gets_the_new_group(db, tmp_path):
    """Wie bei einer bekannten Gruppe: Ein Spiegel des Bestands (``mirror_legacy``) bleibt in seiner alten Lesart und wird in
    ``legacy_mirrors`` gezählt – kein Import ändert die Form einer vorhandenen Zeile."""
    chart = ((1210, 1810, 'Zweite Bank'),)
    bank_account(db, 'Zweite Bank', 1810)
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 1210, 1200, 100.0, 'M-1')], extra_chart=chart))
    execute(db, "UPDATE Bookings SET EntryForm = 'mirror_legacy'")
    stored = _without(db, 'SourceGroup')

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 1210, 1200, 100.0, 'M-1')], extra_chart=chart))

    assert (result['imported'], result['regrouped_rows'], result['legacy_mirrors'], result['form_conflicts_count']) == (0, 1, 1, 0)
    assert _without(db, 'SourceGroup') == stored and _groups(db) == ['901']


# ═════════════════════════════════════════════════════════════════════════════
# A – Die Zuordnung steht vor der Hauptschleife fest und hängt nicht von der Reihenfolge der Datei ab
# ═════════════════════════════════════════════════════════════════════════════

@ORDERS
def test_the_row_goes_to_the_satz_with_its_own_sign_in_either_order_of_the_file(db, tmp_path, reverse):
    """Der Bestand trägt +100 (alte Gruppe), die Datei nennt −100 und +100 unter neuen Gruppen, am selben Tag, auf denselben Konten
    und mit derselben Beleg-Nr.: Die Zeile +100 gehört dem Satz +100, der Satz −100 wird neu angelegt – die Summe bleibt 0."""
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 8400, 100.0, 'V-1', date='2026-06-01')]))
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings') == [(1000000, 'transfer')]      # keine liquide Seite: der Rohbetrag
    plus = _record(901, 4980, 8400, 100.0, 'V-1', date='2026-06-01')
    minus = _record(902, 4980, 8400, -100.0, 'V-1', date='2026-06-01')

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _ordered([minus, plus], reverse)))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 1, 0)
    assert query(db, 'SELECT Amount, SourceGroup FROM Bookings ORDER BY ID') == [(1000000, '901'), (-1000000, '902')]
    assert query(db, 'SELECT SUM(Amount) FROM Bookings') == [(0,)]


def test_a_row_of_the_opposite_sign_is_no_match_outside_the_mirrors_of_the_stock(db, tmp_path):
    """Kein Rückfall auf den Absolutbetrag: Der Satz −100 findet die Zeile +100 (Form ``transfer``) nicht – er wird neu angelegt,
    die alte Zeile bleibt verwaist."""
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 8400, 100.0, 'V-2')]))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 8400, -100.0, 'V-2')]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 0, 1)
    assert query(db, 'SELECT Amount, EntryForm, SourceGroup FROM Bookings ORDER BY ID') == [
        (1000000, 'transfer', '801'), (-1000000, 'transfer', '901')]


@pytest.mark.parametrize('text, matches', [('Telefon', True), (' TELEFON ', True), ('Strom', False), ('', False)])
def test_without_a_document_number_on_either_side_only_the_same_non_empty_text_matches(db, tmp_path, text, matches):
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 1200, 20.0, '', 'Telefon')]))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 20.0, '', ACCOUNTING_TEXT=text)]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == ((0, 1, 0) if matches else (1, 0, 1))


def test_two_empty_texts_prove_nothing_without_a_document_number(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 1200, 20.0, '', ACCOUNTING_TEXT='')]))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 20.0, '', ACCOUNTING_TEXT='')]))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 0, 1)


@ORDERS
def test_satze_without_a_document_number_take_the_rows_of_their_own_text_in_either_order(db, tmp_path, reverse):
    old = [_record(801, 4980, 1200, 20.0, '', 'alpha'), _record(802, 4980, 1200, 20.0, '', 'beta')]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))
    new = [_record(901, 4980, 1200, 20.0, '', 'beta'), _record(902, 4980, 1200, 20.0, '', 'alpha')]

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _ordered(new, reverse)))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (0, 2, 0)
    assert query(db, 'SELECT Text, SourceGroup FROM Bookings ORDER BY ID') == [('alpha', '902'), ('beta', '901')]


@ORDERS
def test_the_satz_without_a_document_number_gets_the_only_row_before_the_one_with_a_number(db, tmp_path, reverse):
    """Im Bestand nur EINE Zeile ohne Beleg-Nr.; die Datei hat zwei Sätze mit gleichem Text: einen ohne Beleg-Nr. und „RE1“. Die
    Zeile gehört dem Satz ohne Beleg-Nr. (Durchgang 2 vor 3), „RE1“ wird neu angelegt – in beiden Reihenfolgen."""
    _import(db, wiso_database(tmp_path / 'a.fdb', [_numbered(801, '')]))
    new = [_numbered(901, 'RE1'), _numbered(902, '')]

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _ordered(new, reverse)))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (1, 1, 0)
    assert query(db, 'SELECT SourceGroup, DocumentNumber FROM Bookings ORDER BY ID') == [('902', None), ('901', 'RE1')]


@ORDERS
def test_rows_of_the_same_text_are_given_out_before_rows_of_another_text(db, tmp_path, reverse):
    """Gleiche Beleg-Nr., der Text darf abweichen – aber die Zeile mit gleichem Text geht vor, auch wenn der Satz, der sie
    braucht, in der Ordnung später kommt: So weichen nur so wenig Texte ab wie nötig."""
    old = [_record(801, 4980, 1200, 20.0, 'P-1', 'b'), _record(802, 4980, 1200, 20.0, 'P-1', 'c')]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))
    new = [_record(901, 4980, 1200, 20.0, 'P-1', 'a2'), _record(902, 4980, 1200, 20.0, 'P-1', 'b')]

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _ordered(new, reverse)))

    assert (result['imported'], result['regrouped_rows'], result['regrouped_text_differs']) == (0, 2, 1)
    assert query(db, 'SELECT Text, SourceGroup FROM Bookings ORDER BY ID') == [('b', '902'), ('c', '901')]


def _contested():
    """Alte Zeilen und neue Sätze, die um dieselben Zeilen streiten: gleiches Datum, gleiche Konten, gleicher Betrag."""
    old = [_record(801, 4980, 1200, 10.0, 'N-1', 'a'), _record(802, 4980, 1200, 10.0, 'N-1', 'b'),
           _record(803, 4980, 1200, 10.0, 'N-1', 'c'), _record(804, 4980, 1200, 10.0, '', 't1'),
           _record(805, 4980, 1200, 10.0, '', 't2'), _record(806, 4980, 1200, 10.0, '', 't3'),
           _record(807, 4980, 1200, 10.0, 'N-2', 'x'), _record(808, 4980, 1200, 10.0, 'N-2', 'x')]
    new = [_record(901, 4980, 1200, 10.0, 'N-1', 'c'), _record(902, 4980, 1200, 10.0, 'N-1', 'a'),
           _record(903, 4980, 1200, 10.0, 'N-1', 'zz'), _record(904, 4980, 1200, 10.0, '', 't2'),
           _record(905, 4980, 1200, 10.0, '', 't9'), _record(906, 4980, 1200, 10.0, 'N-3', 't3'),
           _record(907, 4980, 1200, 10.0, 'N-2', 'x'), _record(908, 4980, 1200, 10.0, 'N-2', 'y')]
    # zwei gleiche Sätze um zwei gleiche Zeilen, zwei verschiedene Sätze um EINE Zeile: Die Ordnung nach Inhalt entscheidet
    old += [_record(809, 4980, 1200, 10.0, 'N-4', 'k'), _record(810, 4980, 1200, 10.0, 'N-4', 'k'),
            _record(811, 4980, 1200, 10.0, 'N-5', 'z')]
    new += [_record(909, 4980, 1200, 10.0, 'N-4', 'k'), _record(910, 4980, 1200, 10.0, 'N-4', 'k'),
            _record(911, 4980, 1200, 10.0, 'N-5', 'p'), _record(912, 4980, 1200, 10.0, 'N-5', 'q')]
    return old, new


def test_the_assignment_is_the_same_for_every_order_of_the_file(tmp_path):
    """Dieselben Sätze in sieben Reihenfolgen (die der Datei, die umgekehrte, fünf gemischte) ergeben dieselbe Zuordnung: Die
    Zeilen 1 bis 11 tragen danach dieselben Gruppen, die Zähler gleichen sich. Erwartet ist die Zuordnung nach den Durchgängen:
    gleiche Beleg-Nr. und Text (1), gleiche Beleg-Nr. mit abweichendem Text (1), beide ohne Beleg-Nr. (2), Beleg-Nr. nachgetragen
    (3); wo Sätze um dieselbe Zeile streiten, entscheidet die Ordnung nach Inhalt (Datum, Konten, Betrag, Beleg-Nr., Text, …, Gruppe)."""
    old, new = _contested()
    orders = [new, new[::-1]] + [random.Random(seed).sample(new, len(new)) for seed in range(1, 6)]
    outcomes = []
    for number, rows in enumerate(orders):
        database = Database(db_name=str(tmp_path / f'run{number}.db'))
        bank_account(database, 'Gruppen-Bank', 1800)
        _import(database, wiso_database(tmp_path / f'old{number}.fdb', old))
        result = _import(database, wiso_database(tmp_path / f'new{number}.fdb', rows))
        outcomes.append((
            (result['imported'], result['skipped'], result['regrouped_rows'], result['regrouped_text_differs'],
             result['orphaned_rows_count']),
            query(database, 'SELECT ID, SourceGroup FROM Bookings WHERE ID <= 11 ORDER BY ID'),
            sorted(group for (group,) in query(database, 'SELECT SourceGroup FROM Bookings'))))

    assert all(outcome == outcomes[0] for outcome in outcomes), outcomes
    counters, old_rows, groups = outcomes[0]
    assert counters == (2, 10, 10, 3, 1)
    assert old_rows == [(1, '902'), (2, '903'), (3, '901'), (4, '804'), (5, '904'), (6, '906'), (7, '907'), (8, '908'),
                        (9, '909'), (10, '910'), (11, '911')]
    assert groups == ['804', '901', '902', '903', '904', '905', '906', '907', '908', '909', '910', '911', '912']


@pytest.mark.parametrize('ref', ['M-2', ''])
def test_a_mirror_of_the_stock_with_the_opposite_sign_gets_the_new_group_and_stays_as_it_is(db, tmp_path, ref):
    """Nur der Altbestand: Die Zeile trägt die Form ``mirror_legacy`` und den umgekehrten Betrag (die Lesart des Betrags auf dem
    Gegenkonto). Sie hängt um und wird als alter Spiegel gezählt; Betrag und Form bleiben."""
    chart = ((1210, 1810, 'Zweite Bank'),)
    bank_account(db, 'Zweite Bank', 1810)
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 1210, 1200, 100.0, ref)], extra_chart=chart))
    execute(db, "UPDATE Bookings SET EntryForm = 'mirror_legacy', Amount = -Amount")
    stored = _without(db, 'SourceGroup')

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 1210, 1200, 100.0, ref)], extra_chart=chart))

    assert (result['imported'], result['regrouped_rows'], result['legacy_mirrors'], result['form_conflicts_count']) == (0, 1, 1, 0)
    assert _without(db, 'SourceGroup') == stored and _groups(db) == ['901']
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings') == [(-1000000, 'mirror_legacy')]


@ORDERS
def test_the_mirror_stays_when_the_file_has_a_satz_with_the_amount_of_the_row(db, tmp_path, reverse):
    """Dieselbe Sperre wie bei der Übernahme aus dem CSV-Import: Nennt die Datei selbst einen Satz mit dem Betrag der Zeile (hier
    mit anderem Steuersatz, er trifft sie also nicht), gehört die Zeile ihm – der Satz mit dem umgekehrten Betrag bekommt sie nicht."""
    chart = ((1210, 1810, 'Zweite Bank'),)
    bank_account(db, 'Zweite Bank', 1810)
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 1210, 1200, 100.0, 'M-3')], extra_chart=chart))
    execute(db, "UPDATE Bookings SET EntryForm = 'mirror_legacy', Amount = -Amount")
    new = [_record(901, 1210, 1200, 100.0, 'M-3'),
           _record(902, 1210, 1200, -100.0, 'M-3', AMOUNTNET=84.03, TAXRATE=19.0)]

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _ordered(new, reverse), extra_chart=chart))

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (2, 0, 1)
    assert sorted(_groups(db)) == ['801', '901', '902']


def test_a_mirror_with_a_deviating_tax_rate_or_form_is_no_flipped_match(db, tmp_path):
    """Der umgekehrte Betrag gilt nur für ``mirror_legacy`` – jede andere Form (hier ``mirror_coa``) bleibt ohne Rückfall – und
    nie bei einem anderen Steuersatz."""
    chart = ((1210, 1810, 'Zweite Bank'),)
    bank_account(db, 'Zweite Bank', 1810)
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 1210, 1200, 100.0, 'M-4')], extra_chart=chart))
    execute(db, 'UPDATE Bookings SET Amount = -Amount')                      # umgekehrt, aber noch ``mirror_coa``

    other_form = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 1210, 1200, 100.0, 'M-4')], extra_chart=chart))
    assert (other_form['imported'], other_form['regrouped_rows']) == (1, 0)

    execute(db, "UPDATE Bookings SET EntryForm = 'mirror_legacy' WHERE SourceGroup = '801'")
    other_rate = _import(db, wiso_database(tmp_path / 'c.fdb', [
        _record(902, 1210, 1200, 100.0, 'M-4', AMOUNTNET=84.03, TAXRATE=19.0)], extra_chart=chart))
    assert (other_rate['imported'], other_rate['regrouped_rows']) == (1, 0)


# ═════════════════════════════════════════════════════════════════════════════
# 8 – AfA
# ═════════════════════════════════════════════════════════════════════════════

def _afa_in_groups(groups):
    """Die AfA-Sätze der Anlage ``CAR`` in diesen Quellgruppen (je Planjahr eine); ``None`` lässt die Gruppe der Vorlage."""
    records = _afa_records(CAR)
    for record, group in zip(records, groups):
        if group is not None:
            record['ACCOUNTINGID'] = group
    return records


def test_an_afa_satz_of_a_booked_year_created_again_is_regrouped_not_booked_a_second_time(db_with_coa, tmp_path):
    old = _fdb(tmp_path / 'a.fdb', _afa_in_groups([None] * 5), assets=[CAR])
    run_with_assets(db_with_coa, old, today=JUNE)
    assert [row[0] for row in _afa_bookings(db_with_coa)] == ['2024-12-31', '2025-12-31']
    plan, stored = _plan(db_with_coa), _without(db_with_coa, 'SourceGroup')

    new = _fdb(tmp_path / 'b.fdb', _afa_in_groups([901, 902, 903, 904, 905]), assets=[CAR])
    result = run_with_assets(db_with_coa, new, today=JUNE)

    assert (result['imported'], result['regrouped_rows'], result['orphaned_rows_count']) == (0, 2, 0)
    assert (result['future_depreciations_skipped'], result['depreciation_conflicts']) == (3, 0)
    assert [tuple(row) for row in _afa_bookings(db_with_coa)] == [('2024-12-31', '901'), ('2025-12-31', '902')]
    assert _plan(db_with_coa) == plan                                      # Planjahre und Beträge: wie vorher
    assert _statuses(db_with_coa) == ['booked', 'booked', 'planned', 'planned', 'planned']
    assert _without(db_with_coa, 'SourceGroup') == stored


def test_a_year_booked_in_the_app_stays_a_conflict_for_a_satz_created_again_by_wiso(db_with_coa, tmp_path):
    """Hat die App das Jahr gebucht, gibt es keine Zeile mit Quellgruppe dafür: Der neu angelegte Satz bleibt ein Konflikt."""
    old = _fdb(tmp_path / 'a.fdb', _afa_in_groups([None] * 5), assets=[CAR])
    run_with_assets(db_with_coa, old, today=JUNE)
    asset_id = query(db_with_coa, 'SELECT ID FROM Assets')[0][0]
    db_with_coa.book_depreciation(asset_id, 2026, coa_id_expense=db_with_coa.get_coa_id_by_account_number(6222))
    plan = _plan(db_with_coa)

    new = _fdb(tmp_path / 'b.fdb', _afa_in_groups([901, 902, 903, 904, 905]), assets=[CAR])
    result = run_with_assets(db_with_coa, new, today='2027-01-15')

    assert (result['regrouped_rows'], result['imported'], result['depreciation_conflicts']) == (2, 0, 1)
    assert _plan(db_with_coa) == plan


def test_future_afa_satze_do_not_make_their_groups_orphans_and_do_not_hide_the_regrouping_of_the_others(db_with_coa, tmp_path):
    """Bestand wie aus 2.2.4: alle fünf AfA-Sätze stehen in den Büchern, auch die künftigen. Die Datei nennt die Jahre 2024 und
    2025 mit neuer Gruppe, die künftigen mit ihrer alten: Deren Zeilen sind nicht verwaist (die Gruppe steht noch in der
    Datei), sie bleiben als „behalten“, und nur die beiden anderen hängen um."""
    old = _fdb(tmp_path / 'a.fdb', _afa_in_groups([None] * 5), assets=[CAR])
    run_with_assets(db_with_coa, old, today='2099-01-01')
    assert len(_afa_bookings(db_with_coa)) == 5 and _statuses(db_with_coa) == ['booked'] * 5
    plan, stored = _plan(db_with_coa), _without(db_with_coa, 'SourceGroup')

    new = _fdb(tmp_path / 'b.fdb', _afa_in_groups([901, 902, None, None, None]), assets=[CAR])
    result = run_with_assets(db_with_coa, new, today=JUNE)

    assert (result['imported'], result['regrouped_rows'], result['future_depreciations_kept']) == (0, 2, 3)
    assert (result['orphaned_rows_count'], result['depreciation_conflicts']) == (0, 0)
    assert [row[1] for row in _afa_bookings(db_with_coa)] == ['901', '902', '703', '704', '705']
    assert _plan(db_with_coa) == plan and _without(db_with_coa, 'SourceGroup') == stored


def test_a_future_afa_satz_created_again_finds_the_row_of_an_older_import_too(db_with_coa, tmp_path):
    """Dieselbe Regel wie für eine bekannte Gruppe: Die Zeile eines künftigen Satzes aus einem Import bis 2.2.4 bleibt in den
    Büchern, ihr Jahr gilt als gebucht – auch wenn WISO den Satz unter neuer Gruppe geschrieben hat."""
    old = _fdb(tmp_path / 'a.fdb', _afa_in_groups([None] * 5), assets=[CAR])
    run_with_assets(db_with_coa, old, today='2099-01-01')
    plan, stored = _plan(db_with_coa), _without(db_with_coa, 'SourceGroup')

    new = _fdb(tmp_path / 'b.fdb', _afa_in_groups([901, 902, 903, 904, 905]), assets=[CAR])
    result = run_with_assets(db_with_coa, new, today=JUNE)

    assert (result['imported'], result['regrouped_rows'], result['future_depreciations_kept']) == (0, 5, 3)
    assert (result['future_depreciations_skipped'], result['orphaned_rows_count']) == (0, 0)
    assert _plan(db_with_coa) == plan and _without(db_with_coa, 'SourceGroup') == stored


# ═════════════════════════════════════════════════════════════════════════════
# 9 – Verknüpfung zur Bankbewegung bleibt
# ═════════════════════════════════════════════════════════════════════════════

def test_a_row_under_a_bank_movement_keeps_its_parent_when_it_gets_the_new_group(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', _sample(801)[:2]))
    account_id = query(db, 'SELECT ID FROM Accounts')[0][0]
    execute(db, "INSERT INTO Bookings (DateBooking, Account_ID, Amount, Text, BookingType) "
                "VALUES ('2026-01-10', ?, -1190000, 'Bankbewegung', 'bank')", (account_id,))
    bank_id = query(db, "SELECT ID FROM Bookings WHERE BookingType = 'bank'")[0][0]
    execute(db, 'UPDATE Bookings SET ParentBooking_ID = ? WHERE DocumentNumber = ?', (bank_id, 'S-1'))
    stored = _without(db, 'SourceGroup')

    result = _import(db, wiso_database(tmp_path / 'b.fdb', _sample(901)[:2]))

    assert (result['imported'], result['regrouped_rows']) == (0, 2)
    assert query(db, "SELECT ParentBooking_ID, SourceGroup FROM Bookings WHERE DocumentNumber = 'S-1'") == [(bank_id, '901')]
    assert _without(db, 'SourceGroup') == stored


# ═════════════════════════════════════════════════════════════════════════════
# 10 – CSV-Übernahme bleibt, wie sie war – und kommt nach dem Umhängen
# ═════════════════════════════════════════════════════════════════════════════

def test_a_csv_booking_without_a_group_is_still_taken_over(db, tmp_path):
    assert db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'S-1', '', '10.01.2026', 'Posten S-1')))['imported'] == 1
    path = wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 1200, 119.0, 'S-1', date='2026-01-10')])

    result = _import(db, path)

    assert (result['imported'], result['adopted'], result['regrouped_rows'], result['orphaned_rows_count']) == (0, 1, 0, 0)
    assert _groups(db) == ['801']


def test_the_orphaned_row_is_taken_before_a_csv_row_and_the_csv_row_stays_as_it_is(db, tmp_path):
    """Ein Satz mit unbekannter Gruppe sucht erst unter den verwaisten Zeilen, dann in der Übernahme aus dem CSV-Import."""
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 1200, 119.0, 'S-1', date='2026-01-10')]))
    execute(db, "INSERT INTO Bookings (DateBooking, COA_ID, CounterCOA_ID, Amount, Text, DocumentNumber, BookingType, EntryForm) "
                "SELECT DateBooking, COA_ID, CounterCOA_ID, Amount, Text, DocumentNumber, BookingType, EntryForm "
                "FROM Bookings WHERE ID = 1")                                             # dieselbe Buchung, ohne Quellgruppe

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 119.0, 'S-1', date='2026-01-10')]))

    assert (result['imported'], result['adopted'], result['regrouped_rows']) == (0, 0, 1)
    assert _groups(db) == ['901', None]


# ═════════════════════════════════════════════════════════════════════════════
# 11 – Verwaiste Zeilen: Liste, Grenze, Seite
# ═════════════════════════════════════════════════════════════════════════════

def test_the_orphaned_rows_are_listed_by_date_and_id_up_to_50_and_counted_in_full(db, tmp_path):
    count = MAX_ORPHANED_ROWS + 10
    old = [_record(801 + number, 4980, 1200, 10.0 + number, f'L-{number:02d}',
                   date=f'2026-0{3 - number // 28}-{28 - number % 28:02d}') for number in range(count)]
    _import(db, wiso_database(tmp_path / 'a.fdb', old))

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 1.0, 'Z-1', date='2027-01-01')]))

    assert MAX_ORPHANED_ROWS == 50
    assert result['orphaned_rows_count'] == count and len(result['orphaned_rows']) == MAX_ORPHANED_ROWS
    expected = [row[0] for row in query(db, "SELECT ID FROM Bookings WHERE SourceGroup <> '901' ORDER BY DateBooking, ID")]
    assert [row['id'] for row in result['orphaned_rows']] == expected[:MAX_ORPHANED_ROWS]
    assert set(result['orphaned_rows'][0]) == {'id', 'date', 'amount', 'account', 'counter_account', 'text'}
    json.dumps(result)                                                                    # die Ergebnisdatei bleibt schreibbar
    notices = _import_notices(result)
    assert f'Nicht mehr in WISO vorhandene Buchungen &ndash; {count}' in notices
    assert f'Genannt werden die ersten {MAX_ORPHANED_ROWS}.' in notices
    assert notices.count('<a href="/transactions/edit?id=') == MAX_ORPHANED_ROWS


def test_a_row_without_a_counter_account_or_a_known_account_is_listed_with_empty_numbers(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', [_record(801, 4980, 1200, 10.0, 'E-1')]))
    execute(db, 'UPDATE Bookings SET CounterCOA_ID = NULL, COA_ID = NULL WHERE ID = 1')

    result = _import(db, wiso_database(tmp_path / 'b.fdb', [_record(901, 4980, 1200, 99.0, 'E-2')]))

    assert [(row['account'], row['counter_account']) for row in result['orphaned_rows']] == [(None, None)]
    json.dumps(result)
    assert '<td></td><td></td><td>Posten E-1</td>' in _import_notices(result)


def test_the_notices_name_the_regrouped_rows_and_the_changed_texts():
    assert '<p>ℹ️ 3 neu angelegte WISO-Buchungen wurden wiedererkannt und nicht doppelt angelegt.</p>' in \
           _import_notices({'regrouped_rows': 3})
    assert ('3 neu angelegte WISO-Buchungen wurden wiedererkannt und nicht doppelt angelegt, davon 1 mit geändertem Text '
            '(der Text der App bleibt).') in _import_notices({'regrouped_rows': 3, 'regrouped_text_differs': 1})
    assert 'davon' not in _import_notices({'regrouped_rows': 3, 'regrouped_text_differs': 0})
    assert 'geändertem Text' not in _import_notices({'regrouped_text_differs': 2})              # ohne Umgehängte nichts


@pytest.mark.parametrize('value', [0, -1, None, True, '5', 1.5, [3], {'a': 1}])
def test_the_notices_ignore_counts_that_are_no_positive_whole_numbers(value):
    assert _import_notices({'regrouped_rows': value, 'regrouped_text_differs': value, 'orphaned_rows_count': value}) == ''


@pytest.mark.parametrize('rows', [['text'], [None], [5], [[1, 2]], 'abc', 5, {'a': 1}, None, True])
def test_a_broken_list_of_orphaned_rows_does_not_stop_the_page(rows):
    notices = _import_notices({'orphaned_rows_count': 2, 'orphaned_rows': rows})
    assert 'Nicht mehr in WISO vorhandene Buchungen &ndash; 2' in notices and '<a href' not in notices


def test_hostile_values_in_the_result_file_are_masked_and_only_a_whole_number_becomes_a_link():
    hostile = '<img src=x onerror=alert(1)>'
    notices = _import_notices({'orphaned_rows_count': 3, 'orphaned_rows': [
        {'id': hostile, 'date': hostile, 'amount': hostile, 'account': hostile, 'counter_account': hostile, 'text': hostile},
        {'id': None, 'date': None, 'amount': None, 'account': None, 'counter_account': None, 'text': None},
        {'id': True, 'amount': [1, 2], 'text': {'a': 1}}]})
    assert '<img' not in notices and hostile not in notices and '&lt;img src=x onerror=alert(1)&gt;' in notices
    assert '<a href' not in notices                                  # weder ein Text noch ein Wahrheitswert wird zum Link
    assert '[1, 2]' in notices


def test_the_count_is_shown_from_the_list_when_the_file_names_none():
    notices = _import_notices({'orphaned_rows': [{'id': 7, 'date': '2026-01-02', 'amount': 1.5, 'text': 'x'}]})
    assert 'Nicht mehr in WISO vorhandene Buchungen &ndash; 1' in notices and 'Genannt werden' not in notices


# ═════════════════════════════════════════════════════════════════════════════
# C – Die Zusammenfassung nach dem Import nennt das Umhängen und die verwaisten Zeilen
# ═════════════════════════════════════════════════════════════════════════════

def test_the_redirect_names_the_regrouped_and_the_orphaned_rows(db, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    _import(db, wiso_database(tmp_path / 'a.fdb', _sample(801)))
    new = _sample(901)[:3] + [_record(950, 4980, 1200, 77.0, 'Z-9', date='2026-05-05')]

    params = _query_of(_location(db, wiso_database(tmp_path / 'b.fdb', new), confirm_existing='1'))

    assert (params['imported'], params['skipped'], params['regrouped'], params['orphans']) == ('1', '3', '3', '2')


def test_the_message_script_reads_the_new_counters_as_numbers_and_names_them_only_above_zero(db):
    script = _fdb_script(PageMiscellaneous(db))
    assert "parseInt(p.get('regrouped') || '0')" in script and "parseInt(p.get('orphans') || '0')" in script
    assert "if (rg > 0) msg += ', ' + rg + ' neu angelegte Buchungen wiedererkannt'" in script
    assert "if (op > 0) msg += ', ' + op + ' Buchungen nicht mehr in WISO vorhanden" in script
    assert "(missing || hints || op) ? '#fff3cd' : '#d4edda'" in script          # Verwaiste Zeilen: Warnfarbe


@pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – der statische Test deckt es ab')
def test_the_message_names_the_regrouped_and_the_orphaned_rows(db, tmp_path):
    script = _fdb_script(PageMiscellaneous(db))
    base = '?wiso_fdb=ok&imported=0&skipped=5&assets=0&afa=0'
    (message,) = _shown_messages(script, base + '&regrouped=5&orphans=2', tmp_path)
    assert '0 Buchungssätze, 5 übersprungen' in message
    assert ', 5 neu angelegte Buchungen wiedererkannt' in message
    assert ', 2 Buchungen nicht mehr in WISO vorhanden – siehe Liste unten' in message
    for search in (base + '&regrouped=0&orphans=0', base):
        (quiet,) = _shown_messages(script, search, tmp_path)
        assert 'wiedererkannt' not in quiet and 'nicht mehr in WISO' not in quiet


@pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – der statische Test deckt es ab')
@pytest.mark.parametrize('name', ['regrouped', 'orphans'])
@pytest.mark.parametrize('value', ['<script>alert(1)</script>', '5<img src=x onerror=alert(1)>', '"><b>x</b>', '-3', ''])
def test_the_message_takes_no_raw_value_for_the_new_counters(db, tmp_path, name, value):
    """Nur eine Zahl größer 0 wird genannt; alles andere aus der Adresse erscheint nicht in der Meldung."""
    script = _fdb_script(PageMiscellaneous(db))
    (message,) = _shown_messages(
        script, f'?wiso_fdb=ok&imported=0&skipped=0&assets=0&afa=0&{name}=' + quote(value), tmp_path)
    for fragment in ('<script>', '<img', '<b>', 'onerror', 'alert'):
        assert fragment not in message, message
    named = ' neu angelegte Buchungen wiedererkannt' in message or ' Buchungen nicht mehr in WISO vorhanden' in message
    assert named is value.startswith('5')


# ═════════════════════════════════════════════════════════════════════════════
# 12 – Replay-Log
# ═════════════════════════════════════════════════════════════════════════════

def test_the_regrouping_is_in_the_replay_log_with_its_own_description(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', _sample(801)[:2]))
    _import(db, wiso_database(tmp_path / 'b.fdb', _sample(901)[:2]))

    with open(os.path.join(userctx.user_data_dir(), 'sql_operations.sql'), encoding='utf-8') as handle:
        statements = [line for line in handle.read().splitlines() if line.startswith('UPDATE Bookings SET SourceGroup')]
    assert statements == ["UPDATE Bookings SET SourceGroup = '901' WHERE ID = 1;",
                          "UPDATE Bookings SET SourceGroup = '902' WHERE ID = 2;"]
    with open(os.path.join(userctx.user_data_dir(), 'sql_operations.log'), encoding='utf-8') as handle:
        assert handle.read().count('Description: WISO-Datenbank-Import: neu angelegten Satz wiedererkannt') == 2


def test_a_failure_in_the_run_leaves_no_half_regrouped_stock_and_no_log_entry(db, tmp_path, monkeypatch):
    """Umhängen und Neuanlegen laufen in EINER Transaktion: Bricht der Lauf ab, ist nichts umgehängt – und nichts im Protokoll."""
    _import(db, wiso_database(tmp_path / 'a.fdb', _sample(801)))
    stored = _without(db)
    new = wiso_database(tmp_path / 'b.fdb', _sample(901)[:2] + [_record(950, 4980, 1200, 77.0, 'Z-9', date='2026-05-05')])
    original = Database._minor_opt
    calls = []

    def fail_on_the_first_new_row(value):
        calls.append(value)
        raise RuntimeError('Festplatte voll')

    with monkeypatch.context() as patch:
        patch.setattr(Database, '_minor_opt', staticmethod(fail_on_the_first_new_row))
        with pytest.raises(RuntimeError, match='Festplatte voll'):
            db.import_wiso_fdb(str(new), with_assets=False, with_invoices=False)

    assert calls and Database._minor_opt is original
    assert _without(db) == stored
    log = os.path.join(userctx.user_data_dir(), 'sql_operations.sql')
    assert not os.path.exists(log) or 'SourceGroup = \'901\'' not in open(log, encoding='utf-8').read()


# ═════════════════════════════════════════════════════════════════════════════
# Der Import liest immer die ganze Datenbank
# ═════════════════════════════════════════════════════════════════════════════

def test_the_import_has_no_way_to_read_a_part_of_the_database(tmp_path):
    """Die Waisenlogik nimmt an, dass die Datei die ganze WISO-Datenbank ist: Ein Lauf über einen Teil (Jahresfilter o. Ä.) würde
    jede Zeile außerhalb als verwaist melden. Gibt es einmal ein solches Argument, gehört die Logik dort abgeschaltet."""
    assert list(inspect.signature(Database.import_wiso_fdb).parameters) == [
        'self', 'path', 'standard_chart_path', 'with_assets', 'with_invoices', 'today']
    rows = [_record(801 + number, 4980, 1200, 10.0 + number, f'W-{number}', date=f'{2020 + number}-06-01') for number in range(6)]
    assert len(read_wiso_database(wiso_database(tmp_path / 'a.fdb', rows)).bookings) == 6      # jedes Jahr, kein Filter


# ═════════════════════════════════════════════════════════════════════════════
# Teil 2 – Import in einen vorhandenen Bestand: Warnung und ausdrückliche Bestätigung
# ═════════════════════════════════════════════════════════════════════════════

def test_the_stock_counts_all_rows_and_the_rows_of_an_import_of_the_wiso_database(db, tmp_path):
    assert db.wiso_import_stock() == {'bookings': 0, 'wiso_rows': 0}
    assert db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'C-1', '')))['imported'] == 1
    assert db.wiso_import_stock() == {'bookings': 1, 'wiso_rows': 0}                    # eine Zeile ohne Quellgruppe
    execute(db, "INSERT INTO Bookings (DateBooking, Amount, BookingType, SourceGroup) VALUES ('2026-01-02', 5, 'entry', '')")
    execute(db, "INSERT INTO Bookings (DateBooking, Amount, BookingType, SourceGroup) VALUES ('2026-01-02', 5, 'bank', '77')")
    assert db.wiso_import_stock() == {'bookings': 3, 'wiso_rows': 0}      # eine leere Gruppe und eine Bankzeile zählen nicht mit

    _import(db, wiso_database(tmp_path / 'a.fdb', _sample(801)))

    assert db.wiso_import_stock() == {'bookings': 8, 'wiso_rows': 5}


def test_the_stock_is_only_read_never_written(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', _sample(801)))
    before = _without(db)
    db.wiso_import_stock()
    assert _without(db) == before


def test_the_form_has_no_hint_and_no_box_for_an_empty_stock(db):
    page = PageMiscellaneous(db)
    assert 'confirm_existing' not in page and STOCK_CONFIRM_LABEL not in page and 'Es gibt bereits' not in page
    assert 'name="fdb_path"' in page and 'Aus WISO-Datenbank importieren' in page


def test_the_form_names_the_number_of_bookings_and_asks_for_a_confirmation(db):
    assert db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'C-1', ''), ('6850', '1800', '59,50', 'C-2', '')))['imported'] == 2

    page = PageMiscellaneous(db)

    assert STOCK_NOTICE_BOOKINGS.format(n=2) in page and 'Es gibt bereits 2 Buchungen. Der Import ist für einen leeren Bestand gedacht' in page
    assert 'aus einem Import der WISO-Datenbank' not in page
    assert ('<input type="checkbox" name="confirm_existing" value="1" required>'
            f'&nbsp;{STOCK_CONFIRM_LABEL}</label>') in page
    form = page.split('action="/wiso/fdb-import"')[1].split('</form>')[0]
    assert form.index('name="confirm_existing"') < form.index('<button type="submit"')       # über dem Absenden
    assert form.index('Es gibt bereits 2 Buchungen') < form.index('name="confirm_existing"')


def test_the_form_names_the_rows_of_an_earlier_import_of_the_wiso_database(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'a.fdb', _sample(801)))
    db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'C-1', '')))               # dazu ein Satz ohne Quellgruppe

    page = PageMiscellaneous(db)

    assert STOCK_NOTICE_WISO_ROWS.format(n=5) in page
    assert 'Es gibt bereits 5 Buchungen aus einem Import der WISO-Datenbank. Ein weiterer Import erkennt sie wieder' in page
    assert 'Der Import ist für einen leeren Bestand gedacht' not in page                  # nur der Hinweis zu den WISO-Zeilen
    assert ('Buchungen, die es in WISO nicht mehr gibt, stehen danach in einer Liste und müssen von Hand bereinigt werden; '
            'in WISO nachträglich geänderte Beträge übernimmt der Import nicht.') in page
    assert page.count('name="confirm_existing"') == 1 and ' required>' in page


def test_the_hint_is_masked_and_uses_existing_classes_only(db):
    db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'C-1', '')))
    page = PageMiscellaneous(db)
    block = page.split('Es gibt bereits')[0].rsplit('<p', 1)[1]
    assert block.startswith(' class="warnColor">')                                           # schon vorhanden in buch.css
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'buch.css'), encoding='utf-8') as css:
        assert '.warnColor' in css.read()


def _location(db, path, **post_data):
    post_data = {'fdb_path': [str(path)], **{key: [value] for key, value in post_data.items()}}
    status, location = handlers.handle_wiso_fdb_import(db, post_data)
    assert status == 303 and location.startswith('/miscellaneous?')
    return location


def test_an_empty_stock_imports_without_a_confirmation(db, tmp_path):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)

    location = _location(db, wiso_database(tmp_path / 'a.fdb', _sample(801)))

    params = _query_of(location)
    assert (params['wiso_fdb'], params['imported']) == ('ok', '5') and _count(db) == 5


@pytest.mark.parametrize('stock', ['without_wiso_rows', 'with_wiso_rows'])
def test_without_a_confirmation_an_existing_stock_is_not_imported(db, tmp_path, stock):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    if stock == 'with_wiso_rows':
        _import(db, wiso_database(tmp_path / 'first.fdb', _sample(801)))
    else:
        db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'C-1', '')))
    before = _without(db)
    result_file = os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json')
    path = wiso_database(tmp_path / 'again.fdb', _sample(901) + [_record(990, 4980, 1200, 3.0, 'N-9', date='2026-06-06')])

    location = _location(db, path)

    assert _query_of(location) == {'wiso_fdb': 'error', 'msg': handlers.WISO_FDB_STOCK_REFUSED}
    assert handlers.WISO_FDB_STOCK_REFUSED == 'Der Bestand ist nicht leer – bitte den Import ausdrücklich bestätigen.'
    assert _without(db) == before and not os.path.exists(result_file)          # nichts importiert, nichts umgehängt, nichts gemeldet


@pytest.mark.parametrize('value', ['', '0', 'on', 'true', 'ja', '2', ' 1', '1 '])
def test_only_the_value_1_confirms(db, tmp_path, value):
    db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'C-1', '')))
    before = _without(db)

    location = _location(db, wiso_database(tmp_path / 'a.fdb', _sample(801)), confirm_existing=value)

    assert _query_of(location)['wiso_fdb'] == 'error' and _without(db) == before


def test_the_refusal_comes_before_any_look_at_the_file(db, tmp_path):
    """Auch ein Pfad, den es nicht gibt, ändert nichts an der Meldung: Geprüft wird der Bestand, bevor die Datei gesucht wird."""
    db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'C-1', '')))

    location = _location(db, tmp_path / 'weg.fdb')

    assert _query_of(location)['msg'] == handlers.WISO_FDB_STOCK_REFUSED


@pytest.mark.parametrize('stock', ['without_wiso_rows', 'with_wiso_rows'])
def test_with_the_confirmation_an_existing_stock_is_imported_and_the_result_is_reported(db, tmp_path, stock):
    os.makedirs(userctx.user_data_dir(), exist_ok=True)
    if stock == 'with_wiso_rows':
        _import(db, wiso_database(tmp_path / 'first.fdb', _sample(801)))
        expected = {'imported': '1', 'skipped': '5'}                               # fünf wiedererkannt (umgehängt), einer neu
    else:
        db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'C-1', '')))
        expected = {'imported': '6', 'skipped': '0'}
    path = wiso_database(tmp_path / 'again.fdb', _sample(901) + [_record(990, 4980, 1200, 3.0, 'N-9', date='2026-06-06')])

    location = _location(db, path, confirm_existing='1')

    params = _query_of(location)
    assert params['wiso_fdb'] == 'ok' and {key: params[key] for key in expected} == expected
    with open(os.path.join(userctx.user_data_dir(), 'wiso_fdb_result.json'), encoding='utf-8') as handle:
        assert json.load(handle)['regrouped_rows'] == (5 if stock == 'with_wiso_rows' else 0)


def test_the_database_method_itself_asks_for_no_confirmation(db, tmp_path):
    """Tests, Demodaten und Skripte rufen ``Database.import_wiso_fdb`` direkt auf: Es prüft den Bestand nicht."""
    db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'C-1', '')))
    result = db.import_wiso_fdb(wiso_database(tmp_path / 'a.fdb', _sample(801)), with_assets=False, with_invoices=False)
    assert result['errors'] == [] and result['imported'] == 5


def test_over_http_the_import_needs_the_confirmation_once_the_stock_is_not_empty(server_port, auto_login, tmp_path):
    db = auto_login.db
    assert db.wiso_import_stock() == {'bookings': 0, 'wiso_rows': 0}
    page = get(server_port, '/miscellaneous')
    assert page.status == 200 and 'confirm_existing' not in page.text
    path = wiso_database(tmp_path / 'a.fdb', _sample(801))
    form = {'fdb_path': path, 'with_assets': '1', 'with_invoices': '1'}
    headers = {'Origin': own_origin(server_port)}

    first = post(server_port, '/wiso/fdb-import', headers=headers, csrf=auto_login.csrf, form=form)
    assert first.status == 303 and _query_of(first.headers['Location'])['wiso_fdb'] == 'ok'     # leerer Bestand: ohne Bestätigung
    assert _count(db) == 5

    page = get(server_port, '/miscellaneous')
    assert 'name="confirm_existing"' in page.text and 'Es gibt bereits 5 Buchungen aus einem Import' in page.text

    refused = post(server_port, '/wiso/fdb-import', headers=headers, csrf=auto_login.csrf, form=form)
    assert refused.status == 303
    assert _query_of(refused.headers['Location']) == {'wiso_fdb': 'error', 'msg': handlers.WISO_FDB_STOCK_REFUSED}
    assert _count(db) == 5

    confirmed = post(server_port, '/wiso/fdb-import', headers=headers, csrf=auto_login.csrf,
                     form={**form, 'confirm_existing': '1'})
    params = _query_of(confirmed.headers['Location'])
    assert (params['wiso_fdb'], params['imported'], params['skipped']) == ('ok', '0', '5') and _count(db) == 5
