# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Importe tragen den BU-Schlüssel ein (``Bookings.DatevKey``, seit 2.3.0).

* **WISO-Datenbank:** ``DATEVVATKEY`` wird gelesen (eine ganze Zahl von 1 bis 9999; leer, 0 und alles andere ist
  keiner, fehlt die Spalte, auch). Ein neuer Satz bekommt ihn; die Übernahme einer CSV-Buchung füllt ihn nur, wo er
  fehlt; der Re-Import trägt ihn an einer wiedererkannten Zeile einer bekannten Quellgruppe nach – nur diese Spalte,
  nur wo sie leer ist, nur bei genauem Treffer (``datev_keys_filled``). **An eine vorhandene Zeile geht er nur, wenn sie
  denselben Steuersatz trägt wie der WISO-Satz** (bei der Übernahme gilt der Satz nach dem UPDATE); sonst bleibt er leer,
  und der Fall zählt in ``datev_key_rate_mismatches``.
* **Textimport:** ``SCHLUESSEL`` (1–9999) steht am Satz; der Steuersatz kommt zuerst aus der Tabelle der Regeln
  (401 im zweiten Halbjahr 2020: 16 %), sonst aus ``TaxKeys``; die stille 19 % bei „4405 an 4400“ gilt nur ohne Schlüssel.
  Der Tabellen-Export, der ein leeres Konto füllt, lässt den Schlüssel fallen (``_datev_key_stale``).
* **Berater- und Mandantennummer** stehen in ``SYS_FINACCSYSTEMS`` (Satz „Datev-Format“): Die App liest nur
  ``NAME``, ``CONSULTANT_NO`` und ``CLIENT_NO`` und trägt sie nur ein, wenn beide Felder der eigenen Firma leer sind –
  erst nachdem die Buchungen geschrieben sind; ein Lesefehler der Tabelle bricht den Import nicht ab (Hinweis).

Alle Namen, Nummern und Beträge sind erfunden.
"""
import json
import os
from contextlib import closing

import pytest

import userctx
from db import Database, datev_keys
from importers.wiso_fdb import WisoDatabase
from server.pages_miscellaneous import _import_notices
from tests.entry_form_helpers import (
    bank_account, csv_export, datev_rows, execute, query, wiso_database, wiso_row)

PASSWORD = 'geheim-0815'
SYSTEMS = [
    {'NAME': 'Standard-ASCII', 'CONSULTANT_NO': '1111', 'CLIENT_NO': '22', 'USERPASSWORD': ''},
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '4711', 'CLIENT_NO': '12', 'USERPASSWORD': ''},
    {'NAME': 'Andere Schnittstelle', 'CONSULTANT_NO': '3141592', 'CLIENT_NO': '27182', 'USERPASSWORD': PASSWORD},
]


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Schlüssel-Bank', 1800)
    return tmp_db


def _keys(db):
    """{Belegnummer: BU-Schlüssel} aller Buchungssätze."""
    return dict(query(db, "SELECT DocumentNumber, DatevKey FROM Bookings WHERE BookingType = 'entry'"))


def _rates(db):
    """{Belegnummer: Steuersatz} aller Buchungssätze."""
    return dict(query(db, "SELECT DocumentNumber, TaxRate FROM Bookings WHERE BookingType = 'entry'"))


def _rated(ref, key=None, rate=19.0, gross=119.0, **extra):
    """Ein Satz der WISO-Datenbank mit Steuersatz (in Prozent) und, wenn genannt, BU-Schlüssel."""
    row = wiso_row(4980, 1200, gross, ref, AMOUNTNET=round(gross / (1 + rate / 100), 2), TAXRATE=rate, **extra)
    if key is not None:
        row['DATEVVATKEY'] = key
    return row


def _without_key(db):
    """Alle Spalten aller Buchungen außer ``DatevKey``."""
    with closing(db._get_connection()) as conn:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(Bookings)') if row[1] != 'DatevKey']
        return conn.execute(f'SELECT {", ".join(columns)} FROM Bookings ORDER BY ID').fetchall()


def _import(db, path, **kwargs):
    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False, **kwargs)
    assert result['errors'] == [] and result['blocker'] == [], result
    return result


# ── Der Leser ────────────────────────────────────────────────────────────────

def _read(path):
    with WisoDatabase(str(path)) as wiso:
        return wiso.bookings(), wiso


@pytest.mark.parametrize('stored, expected', [(401, 401), (402, 402), (7, 7), (490, 490), (1, 1), (9999, 9999),
                                              (0, None), (None, None), (-1, None), (10000, None), (32767, None)])
def test_the_reader_takes_the_key_of_a_booking_as_a_whole_number_from_1_to_9999(tmp_path, stored, expected):
    path = wiso_database(tmp_path / 'db.fdb', [wiso_row(4980, 1200, 119.0, 'K-1', DATEVVATKEY=stored)], key_column=True)
    bookings, _wiso = _read(path)
    assert [booking.datev_key for booking in bookings] == [expected]


def test_a_database_without_the_column_gives_no_key_and_a_hint_but_no_blocker(tmp_path):
    path = wiso_database(tmp_path / 'db.fdb', [wiso_row(4980, 1200, 119.0, 'K-1', DATEVVATKEY=401)])
    bookings, wiso = _read(path)
    assert [booking.datev_key for booking in bookings] == [None]
    with wiso:
        blocker, hints = wiso.check()
    assert blocker == [] and any('DATEVVATKEY' in hint for hint in hints)


def test_a_database_with_the_column_has_no_hint_about_it(tmp_path):
    path = wiso_database(tmp_path / 'db.fdb', [wiso_row(4980, 1200, 119.0, 'K-1')], key_column=True)
    with WisoDatabase(str(path)) as wiso:
        blocker, hints = wiso.check()
    assert blocker == [] and not any('DATEVVATKEY' in hint for hint in hints)


# ── Der Import legt den Satz mit dem Schlüssel an ────────────────────────────

def test_a_new_booking_gets_the_key_of_the_database(db, tmp_path):
    path = wiso_database(tmp_path / 'db.fdb', [
        wiso_row(4980, 1200, 119.0, 'K-1', DATEVVATKEY=401), wiso_row(4980, 1200, 59.5, 'K-2', DATEVVATKEY=490),
        wiso_row(4980, 1200, 10.0, 'K-3'), wiso_row(4980, 1200, 20.0, 'K-4', DATEVVATKEY=0)], key_column=True)

    result = _import(db, path)

    assert result['imported'] == 4 and result['datev_keys_filled'] == 0
    assert _keys(db) == {'K-1': 401, 'K-2': 490, 'K-3': None, 'K-4': None}


def test_a_database_without_the_column_imports_without_keys(db, tmp_path):
    path = wiso_database(tmp_path / 'db.fdb', [wiso_row(4980, 1200, 119.0, 'K-1', DATEVVATKEY=401)])
    assert _import(db, path)['imported'] == 1
    assert _keys(db) == {'K-1': None}


# ── Re-Import: nur die leere Spalte, nur bei genauem Treffer ─────────────────

ROWS = [wiso_row(4980, 1200, 119.0, 'R-1', ACCOUNTINGID=801), wiso_row(4980, 1200, 59.5, 'R-2', ACCOUNTINGID=802),
        wiso_row(1200, 8400, 100.0, 'R-3', ACCOUNTINGID=803)]


def _with_keys(*keys):
    return [dict(row, DATEVVATKEY=key) for row, key in zip(ROWS, keys)]


def test_the_reimport_fills_an_empty_key_and_changes_nothing_else(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'old.fdb', ROWS))                      # eine ältere Datei ohne Schlüssel
    assert _keys(db) == {'R-1': None, 'R-2': None, 'R-3': None}
    before = _without_key(db)

    result = _import(db, wiso_database(tmp_path / 'new.fdb', _with_keys(401, 490, 101), key_column=True))

    assert (result['imported'], result['datev_keys_filled']) == (0, 3)
    assert _keys(db) == {'R-1': 401, 'R-2': 490, 'R-3': 101}
    assert _without_key(db) == before                                             # jede andere Spalte, jede Zeile

    again = _import(db, wiso_database(tmp_path / 'new2.fdb', _with_keys(401, 490, 101), key_column=True))
    assert (again['imported'], again['datev_keys_filled']) == (0, 0)             # ein zweiter Lauf findet nichts mehr


def test_the_reimport_never_changes_a_key_that_is_there(db, tmp_path):
    path = wiso_database(tmp_path / 'db.fdb', _with_keys(401, 490, 101), key_column=True)
    _import(db, path)
    execute(db, "UPDATE Bookings SET DatevKey = 402 WHERE DocumentNumber = 'R-1'")
    before = _without_key(db)

    result = _import(db, wiso_database(tmp_path / 'other.fdb', _with_keys(7, 100, 5), key_column=True))

    assert result['datev_keys_filled'] == 0
    assert _keys(db) == {'R-1': 402, 'R-2': 490, 'R-3': 101} and _without_key(db) == before


def test_the_reimport_gives_a_row_the_user_changed_nothing(db, tmp_path):
    """Eine Zeile, deren Konto der Nutzer geändert hat, ist kein genauer Treffer: Sie bekommt keinen Schlüssel."""
    _import(db, wiso_database(tmp_path / 'old.fdb', ROWS))
    row = query(db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'R-1'")[0][0]
    db.update_booking_fields(row, coa_id=db.get_coa_id_by_account_number(6815))
    before = _without_key(db)

    result = _import(db, wiso_database(tmp_path / 'new.fdb', _with_keys(401, 490, 101), key_column=True))

    assert result['datev_keys_filled'] == 2 and result['imported'] == 0
    assert _keys(db) == {'R-1': None, 'R-2': 490, 'R-3': 101} and _without_key(db) == before


def test_the_reimport_fills_a_key_in_the_replay_log(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'old.fdb', ROWS))
    _import(db, wiso_database(tmp_path / 'new.fdb', _with_keys(401, None, None), key_column=True))
    with open(os.path.join(userctx.user_data_dir(), 'sql_operations.sql'), encoding='utf-8') as handle:
        statements = [line for line in handle.read().splitlines() if line.startswith('UPDATE Bookings SET DatevKey')]
    assert len(statements) == 1 and 'DatevKey IS NULL' in statements[0] and '401' in statements[0]


def test_the_reimport_without_the_column_fills_nothing(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'old.fdb', ROWS))
    result = _import(db, wiso_database(tmp_path / 'again.fdb', ROWS))
    assert result['datev_keys_filled'] == 0 and _keys(db) == {'R-1': None, 'R-2': None, 'R-3': None}


# ── Re-Import: der Schlüssel geht nur an eine Zeile mit dem Steuersatz des WISO-Satzes ──────────────────

def test_the_reimport_does_not_give_a_key_to_a_row_whose_rate_the_user_changed(db, tmp_path):
    """Der Schlüssel gehört zu dem Satz, für den WISO ihn geschrieben hat: Ändert der Nutzer den Satz (der Schlüssel
    fällt dabei), trägt der Re-Import ihn nicht wieder ein – der Export leitet ihn aus dem Satz der App ab."""
    rows = [_rated('S-1', 401, ACCOUNTINGID=811)]
    _import(db, wiso_database(tmp_path / 'first.fdb', rows, key_column=True))
    assert _keys(db) == {'S-1': 401} and _rates(db) == {'S-1': 0.19}
    row = query(db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'S-1'")[0][0]
    db.update_booking_fields(row, tax_rate=0.07)
    assert _keys(db) == {'S-1': None} and _rates(db) == {'S-1': 0.07}             # der Schlüssel fiel mit dem Satz
    before = _without_key(db)

    result = _import(db, wiso_database(tmp_path / 'again.fdb', rows, key_column=True))

    assert (result['imported'], result['datev_keys_filled'], result['datev_key_rate_mismatches']) == (0, 0, 1)
    assert _keys(db) == {'S-1': None} and _without_key(db) == before
    assert datev_rows(db)['S-1'][4] == '402'                       # der Export leitet ihn ab: 7 % auf einem Aufwandskonto
    again = _import(db, wiso_database(tmp_path / 'third.fdb', rows, key_column=True))
    assert (again['datev_keys_filled'], again['datev_key_rate_mismatches']) == (0, 1)      # jedes Mal gemeldet, nie gefüllt


def test_the_reimport_gives_a_key_to_a_row_with_the_same_rate_and_counts_no_mismatch(db, tmp_path):
    """Der Gutfall bleibt: gleicher Satz – auch 7 % und „kein Satz“ auf beiden Seiten –, der Schlüssel kommt dazu."""
    old = [_rated('G-1', ACCOUNTINGID=821), _rated('G-2', rate=7.0, ACCOUNTINGID=822),
           wiso_row(4980, 1200, 10.0, 'G-3', ACCOUNTINGID=823)]
    _import(db, wiso_database(tmp_path / 'old.fdb', old))
    assert _keys(db) == {'G-1': None, 'G-2': None, 'G-3': None} and _rates(db) == {'G-1': 0.19, 'G-2': 0.07, 'G-3': None}

    new = [dict(old[0], DATEVVATKEY=401), dict(old[1], DATEVVATKEY=402), dict(old[2], DATEVVATKEY=490)]
    result = _import(db, wiso_database(tmp_path / 'new.fdb', new, key_column=True))

    assert (result['imported'], result['datev_keys_filled'], result['datev_key_rate_mismatches']) == (0, 3, 0)
    assert _keys(db) == {'G-1': 401, 'G-2': 402, 'G-3': 490}


@pytest.mark.parametrize('reversed_order', [False, True])
def test_two_alike_rows_of_a_group_get_the_key_of_their_own_rate(db, tmp_path, reversed_order):
    """Zwei Sätze einer Gruppe, die sich in Datum, Konten und Betrag gleichen und nur im Satz unterscheiden: Jeder
    bekommt den Schlüssel seines Satzes – gleich, in welcher Reihenfolge die Datei sie nennt."""
    full, reduced = _rated('C-1', ACCOUNTINGID=830), _rated('C-1', rate=7.0, ACCOUNTINGID=830)
    _import(db, wiso_database(tmp_path / 'old.fdb', [full, reduced]))          # Zeilen in der Reihenfolge 19 %, 7 %
    assert query(db, "SELECT TaxRate FROM Bookings ORDER BY ID") == [(0.19,), (0.07,)]

    keyed = [dict(full, DATEVVATKEY=401), dict(reduced, DATEVVATKEY=402)]
    result = _import(db, wiso_database(tmp_path / 'new.fdb', keyed[::-1] if reversed_order else keyed, key_column=True))

    assert (result['imported'], result['datev_keys_filled'], result['datev_key_rate_mismatches']) == (0, 2, 0)
    assert query(db, "SELECT TaxRate, DatevKey FROM Bookings ORDER BY ID") == [(0.19, 401), (0.07, 402)]


@pytest.mark.parametrize('reversed_order', [False, True])
def test_a_key_never_goes_to_the_row_of_another_rate(db, tmp_path, reversed_order):
    """Hat der Nutzer den Satz einer der beiden Zeilen geändert (beide tragen jetzt 7 %), bekommt die Zeile, die nicht mehr
    zu ihrem Satz passt, keinen Schlüssel – und nie den des anderen: Der Schlüssel für 19 % wird nirgends eingetragen."""
    full, reduced = _rated('C-2', ACCOUNTINGID=831), _rated('C-2', rate=7.0, ACCOUNTINGID=831)
    _import(db, wiso_database(tmp_path / 'old.fdb', [full, reduced]))
    first = query(db, "SELECT ID FROM Bookings ORDER BY ID")[0][0]
    db.update_booking_fields(first, tax_rate=0.07)                                  # beide Zeilen: 7 %

    keyed = [dict(full, DATEVVATKEY=401), dict(reduced, DATEVVATKEY=402)]
    result = _import(db, wiso_database(tmp_path / 'new.fdb', keyed[::-1] if reversed_order else keyed, key_column=True))

    assert result['imported'] == 0 and result['datev_keys_filled'] == 1 and result['datev_key_rate_mismatches'] == 1
    stored = [key for (key,) in query(db, "SELECT DatevKey FROM Bookings ORDER BY ID")]
    assert 401 not in stored and sorted(key for key in stored if key) == [402]


def test_a_row_that_carries_no_rate_and_a_wiso_set_with_a_rate_do_not_match(db, tmp_path):
    _import(db, wiso_database(tmp_path / 'old.fdb', [wiso_row(4980, 1200, 119.0, 'N-1', ACCOUNTINGID=840)]))
    keyed = [_rated('N-1', 401, ACCOUNTINGID=840)]

    result = _import(db, wiso_database(tmp_path / 'new.fdb', keyed, key_column=True))

    assert (result['datev_keys_filled'], result['datev_key_rate_mismatches']) == (0, 1) and _keys(db) == {'N-1': None}


# ── Übernahme einer CSV-Buchung ──────────────────────────────────────────────

def test_a_csv_booking_taken_over_by_the_database_keeps_its_key_and_gets_one_where_it_has_none(db, tmp_path):
    csv = csv_export(('6850', '1800', '119,00', 'AD-1', '401'), ('6850', '1800', '59,50', 'AD-2', ''))
    assert db.import_wiso_csv(csv)['imported'] == 2
    assert _keys(db) == {'AD-1': 401, 'AD-2': None}
    path = wiso_database(tmp_path / 'db.fdb', [
        wiso_row(4980, 1200, 119.0, 'AD-1', DATEVVATKEY=490, AMOUNTNET=100.0, TAXRATE=19.0),
        wiso_row(4980, 1200, 59.5, 'AD-2', DATEVVATKEY=402, AMOUNTNET=50.0, TAXRATE=19.0)], key_column=True)

    result = _import(db, path)

    assert (result['imported'], result['adopted']) == (0, 2)
    assert _keys(db) == {'AD-1': 401, 'AD-2': 402}            # der Schlüssel der CSV bleibt, der fehlende kommt dazu


def _csv_booking(db, ref='AD-1', key='', **fields):
    """Eine Buchung des Textimports (6850 an 1800, 119,00), dazu ``fields`` als rohe Änderung (z. B. ``TaxRate = 0.0``)."""
    assert db.import_wiso_csv(csv_export(('6850', '1800', '119,00', ref, key)))['imported'] == 1
    if fields:
        execute(db, 'UPDATE Bookings SET ' + ', '.join(f'{name} = ?' for name in fields) + ' WHERE DocumentNumber = ?',
                (*fields.values(), ref))


def test_the_adoption_gives_no_key_to_a_booking_with_another_rate(db, tmp_path):
    """Eine Buchung aus einem Textimport vor 2.3.0, deren Schlüssel 121 als „steuerfrei“ (0 %) gelesen wurde und dem der
    Schlüssel fehlt: Der WISO-Satz sagt 19 % und 121 – der Schlüssel gehört nicht an eine Zeile mit Satz 0."""
    _csv_booking(db, key='', TaxRate=0.0, TaxAmount=0)
    path = wiso_database(tmp_path / 'db.fdb', [_rated('AD-1', 121)], key_column=True)

    result = _import(db, path)

    assert (result['imported'], result['adopted'], result['datev_key_rate_mismatches']) == (0, 1, 1)
    assert _keys(db) == {'AD-1': None} and _rates(db) == {'AD-1': 0.0}            # Satz und Schlüssel unverändert
    assert datev_rows(db)['AD-1'][4] == '0'                                        # Satz 0: der Export gibt keinen Schlüssel


def test_the_adoption_gives_the_key_and_the_rate_together_when_the_booking_has_neither(db, tmp_path):
    """Das UPDATE der Übernahme füllt den Satz, wenn Satz und Steuerbetrag leer sind: Dann trägt die Zeile danach den Satz
    des WISO-Satzes, und der Schlüssel darf mit."""
    _csv_booking(db, key='')
    assert _rates(db) == {'AD-1': None}
    path = wiso_database(tmp_path / 'db.fdb', [_rated('AD-1', 401)], key_column=True)

    result = _import(db, path)

    assert (result['adopted'], result['datev_key_rate_mismatches']) == (1, 0)
    assert _keys(db) == {'AD-1': 401} and _rates(db) == {'AD-1': 0.19}


def test_the_adoption_gives_the_key_to_a_booking_with_the_same_rate(db, tmp_path):
    _csv_booking(db, key='', TaxRate=0.19, TaxAmount=-190000)
    path = wiso_database(tmp_path / 'db.fdb', [_rated('AD-1', 401)], key_column=True)

    result = _import(db, path)

    assert (result['adopted'], result['datev_key_rate_mismatches']) == (1, 0) and _keys(db) == {'AD-1': 401}


def test_the_adoption_keeps_a_key_the_booking_has_and_counts_no_mismatch_for_it(db, tmp_path):
    _csv_booking(db, key='402')                                                    # Schlüssel und Satz 7 % aus der CSV
    path = wiso_database(tmp_path / 'db.fdb', [_rated('AD-1', 401)], key_column=True)

    result = _import(db, path)

    assert (result['adopted'], result['datev_key_rate_mismatches']) == (1, 0)
    assert _keys(db) == {'AD-1': 402} and _rates(db) == {'AD-1': 0.07}


def test_the_adoption_without_a_key_in_the_database_counts_nothing(db, tmp_path):
    _csv_booking(db, key='', TaxRate=0.0, TaxAmount=0)
    result = _import(db, wiso_database(tmp_path / 'db.fdb', [_rated('AD-1')], key_column=True))
    assert (result['adopted'], result['datev_key_rate_mismatches']) == (1, 0) and _keys(db) == {'AD-1': None}


# ── Textimport ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('key, stored', [('401', 401), ('490', 490), ('1', 1), ('9999', 9999), ('181', 181),
                                         ('0', None), ('', None), ('abc', None), ('401x', None), ('10000', None),
                                         ('0401', None), ('-401', None), ('4 01', None), ('401.0', None)])
def test_the_text_import_stores_a_whole_number_from_1_to_9999(db, key, stored):
    db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'T-1', key)))
    assert _keys(db) == {'T-1': stored}


@pytest.mark.parametrize('key, date, rate', [
    ('401', '15.08.2020', 0.16), ('401', '30.06.2020', 0.19), ('401', '01.07.2020', 0.16), ('401', '31.12.2020', 0.16),
    ('401', '01.01.2021', 0.19), ('402', '15.08.2020', 0.05), ('402', '15.08.2026', 0.07),
    ('7', '15.08.2020', 0.19), ('7', '15.08.2026', 0.16), ('6', '15.08.2020', 0.07), ('6', '15.08.2026', 0.05),
    ('101', '15.08.2020', 0.16), ('102', '15.08.2020', 0.05), ('5', '15.08.2020', 0.19), ('4', '15.08.2020', 0.07),
    ('5', '15.08.2026', 0.16), ('121', '15.08.2026', 0.19), ('121', '15.08.2020', 0.16), ('122', '15.08.2026', 0.07),
    ('3', '15.08.2020', 0.19), ('9', '15.08.2020', 0.19), ('2', '15.08.2020', 0.07), ('8', '15.08.2026', 0.07)])
def test_the_text_import_takes_the_rate_of_the_key_from_the_rules_with_the_swap_of_2020(db, key, date, rate):
    result = db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'T-1', key, date)))
    assert result['imported'] == 1 and result['unknown_tax_keys'] == []
    (stored,) = query(db, "SELECT TaxRate, TaxAmount FROM Bookings WHERE DocumentNumber = 'T-1'")
    assert stored[0] == pytest.approx(rate)
    assert stored[1] == -round(119 - 119 / (1 + rate), 2) * 10000       # die Steuer folgt dem Satz


@pytest.mark.parametrize('key', ['100', '120', '400', '490'])
def test_the_text_import_knows_the_keys_without_a_rate(db, key):
    """Seit 2.3.0 stehen 100, 120 und 490 in TaxKeys (ohne Satz bzw. mit 0): bekannt, nicht unbekannt."""
    result = db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'T-1', key)))
    assert result['unknown_tax_keys'] == []
    assert query(db, "SELECT TaxRate FROM Bookings WHERE DocumentNumber = 'T-1'")[0][0] in (None, 0.0)
    assert _keys(db) == {'T-1': int(key)}


def test_the_text_import_does_not_touch_existing_rows_on_a_repeat(db):
    lines = csv_export(('6850', '1800', '119,00', 'T-1', '401'), ('6850', '1800', '59,50', 'T-2', '490'))
    db.import_wiso_csv(lines)
    execute(db, "UPDATE Bookings SET DatevKey = 402 WHERE DocumentNumber = 'T-1'")
    before = query(db, 'SELECT * FROM Bookings ORDER BY ID')

    again = db.import_wiso_csv(lines)

    assert (again['imported'], again['skipped']) == (0, 2)
    assert query(db, 'SELECT * FROM Bookings ORDER BY ID') == before


def _wait(db, key):
    """Eine Umbuchung „4405 an 4400“ aus dem Textimport mit diesem Schlüssel: (Ergebnis, Steuersatz, Schlüssel)."""
    result = db.import_wiso_csv(csv_export(('4405', '4400', '119,00', 'W-1', key)))
    assert result['imported'] == 1
    (rate, stored), = query(db, "SELECT TaxRate, DatevKey FROM Bookings WHERE DocumentNumber = 'W-1'")
    return result, rate, stored


@pytest.mark.parametrize('key', ['', '0'])
def test_a_transfer_from_4405_to_4400_without_a_key_gets_19_percent_as_before(db, key):
    result, rate, stored = _wait(db, key)
    assert (rate, stored, result['unknown_tax_keys']) == (0.19, None, [])


@pytest.mark.parametrize('key, stored, unknown', [('490', 490, []), ('400', 400, []), ('100', 100, []), ('181', 181, ['181'])])
def test_a_transfer_from_4405_to_4400_with_a_key_keeps_what_the_key_says(db, key, stored, unknown):
    """Ein Schlüssel ohne Satz (490, 400) oder ein unbekannter bekommt nicht stillschweigend 19 %: Der Schlüssel steht am
    Satz, ein unbekannter wird gezählt wie bei jeder anderen Zeile."""
    result, rate, kept = _wait(db, key)
    assert rate in (None, 0.0) and rate != 0.19 and kept == stored
    assert [row['schluessel'] for row in result['unknown_tax_keys']] == unknown


def test_a_transfer_from_4405_to_4400_with_a_key_of_a_rate_takes_that_rate(db):
    assert _wait(db, '402')[1:] == (0.07, 402)


def test_the_table_export_that_fills_an_empty_account_drops_the_key(db):
    """Eine CSV-Zeile mit einem Konto, das der Kontenrahmen nicht kennt, und Schlüssel 401: Setzt der Tabellen-Export aus
    der Kategorie ein Erlöskonto, passt der Schlüssel nicht mehr zum Satz – er fällt wie in ``update_booking_fields``."""
    assert db.import_wiso_csv(csv_export(('9999', '1800', '119,00', 'T-1', '401')))['missing_coa'] == [9999]
    assert _keys(db) == {'T-1': 401} and query(db, "SELECT COA_ID FROM Bookings")[0][0] is None
    name = query(db, "SELECT Name FROM ChartOfAccounts WHERE AccountNumber = 4400")[0][0]
    table = ('Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;opt. Beleg Nr.;Betrag\n'
             f'02.01.2026;Beispiel-Kunde;;Posten T-1;{name};T-1;119,00\n')

    result = db.import_wiso_csv(table.encode('utf-8'))

    assert result['format'] == 'table' and result['updated'] == 1
    assert query(db, "SELECT COA_ID FROM Bookings")[0][0] == db.get_coa_id_by_account_number(4400)
    assert _keys(db) == {'T-1': None}


def test_the_table_export_that_fills_an_empty_account_of_a_row_without_a_key_changes_no_key(db):
    db.import_wiso_csv(csv_export(('9999', '1800', '119,00', 'T-2', '')))
    name = query(db, "SELECT Name FROM ChartOfAccounts WHERE AccountNumber = 4400")[0][0]
    table = ('Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;opt. Beleg Nr.;Betrag\n'
             f'02.01.2026;Beispiel-Kunde;;Posten T-2;{name};T-2;119,00\n')
    assert db.import_wiso_csv(table.encode('utf-8'))['updated'] == 1 and _keys(db) == {'T-2': None}


def test_the_table_export_leaves_the_key_alone(db):
    db.import_wiso_csv(csv_export(('6850', '1800', '119,00', 'T-1', '401')))
    before = _keys(db)
    table = ('Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;opt. Beleg Nr.;Betrag\n'
             '02.01.2026;Beispiel-Lieferant;;Posten T-1;;T-1;-119,00\n')
    result = db.import_wiso_csv(table.encode('utf-8'))
    assert result['format'] == 'table' and result['updated'] == 1
    assert _keys(db) == before == {'T-1': 401}


# ── Berater- und Mandantennummer aus der WISO-Datenbank ──────────────────────

def _own(db, **numbers):
    return db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Probe GmbH',
                             company_name='Eigene Probe GmbH', **numbers)


def _wiso(tmp_path, systems=SYSTEMS, **kwargs):
    return wiso_database(tmp_path / 'db.fdb', [wiso_row(4980, 1200, 119.0, 'N-1')], systems=systems, **kwargs)


def test_the_reader_takes_the_numbers_of_the_datev_format_row_only(tmp_path):
    with WisoDatabase(str(_wiso(tmp_path))) as wiso:
        assert wiso.datev_numbers() == ('4711', '12')
        assert wiso.read().datev_numbers == ('4711', '12')


@pytest.mark.parametrize('system', [
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '123', 'CLIENT_NO': '12', 'USERPASSWORD': ''},          # zu kurz
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '12345678', 'CLIENT_NO': '12', 'USERPASSWORD': ''},     # zu lang
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '4711', 'CLIENT_NO': '123456', 'USERPASSWORD': ''},
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '47x1', 'CLIENT_NO': '12', 'USERPASSWORD': ''},
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '4711;', 'CLIENT_NO': '12', 'USERPASSWORD': ''},
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '4711', 'CLIENT_NO': '', 'USERPASSWORD': ''},
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': None, 'CLIENT_NO': None, 'USERPASSWORD': ''},
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '1000', 'CLIENT_NO': '12', 'USERPASSWORD': ''},         # unter 1001
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '0999', 'CLIENT_NO': '12', 'USERPASSWORD': ''},         # führende Null
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '04711', 'CLIENT_NO': '12', 'USERPASSWORD': ''},
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '4711', 'CLIENT_NO': '0', 'USERPASSWORD': ''},          # Mandant 0
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '4711', 'CLIENT_NO': '00', 'USERPASSWORD': ''},
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '4711', 'CLIENT_NO': '012', 'USERPASSWORD': ''},
    {'NAME': 'Datev-Format', 'CONSULTANT_NO': '4711', 'CLIENT_NO': '100000', 'USERPASSWORD': ''},
    {'NAME': 'DATEV-Format', 'CONSULTANT_NO': '4711', 'CLIENT_NO': '12', 'USERPASSWORD': ''},          # anderer Name
    {'NAME': 'Datev-Format 2', 'CONSULTANT_NO': '4711', 'CLIENT_NO': '12', 'USERPASSWORD': ''}])
def test_the_reader_ignores_a_row_without_two_valid_numbers_or_with_another_name(tmp_path, system):
    with WisoDatabase(str(_wiso(tmp_path, systems=[system]))) as wiso:
        assert wiso.datev_numbers() is None


@pytest.mark.parametrize('consultant, client', [('1001', '1'), ('9999999', '99999'), ('1234567', '12345')])
def test_the_reader_takes_the_ends_of_the_ranges(tmp_path, consultant, client):
    system = {'NAME': 'Datev-Format', 'CONSULTANT_NO': consultant, 'CLIENT_NO': client, 'USERPASSWORD': ''}
    with WisoDatabase(str(_wiso(tmp_path, systems=[system]))) as wiso:
        assert wiso.datev_numbers() == (consultant, client)


def test_numbers_with_blanks_at_the_edges_are_read_and_internal_blanks_are_not(tmp_path):
    ok = {'NAME': 'Datev-Format', 'CONSULTANT_NO': ' 4711 ', 'CLIENT_NO': '12 ', 'USERPASSWORD': ''}
    with WisoDatabase(str(_wiso(tmp_path, systems=[ok]))) as wiso:
        assert wiso.datev_numbers() == ('4711', '12')
    bad = {'NAME': 'Datev-Format', 'CONSULTANT_NO': '47 11', 'CLIENT_NO': '12', 'USERPASSWORD': ''}
    with WisoDatabase(str(_wiso(tmp_path, systems=[bad]))) as wiso:
        assert wiso.datev_numbers() is None


def test_without_the_table_there_are_no_numbers_and_only_a_hint(tmp_path):
    path = wiso_database(tmp_path / 'db.fdb', [wiso_row(4980, 1200, 119.0, 'N-1')])
    with WisoDatabase(str(path)) as wiso:
        assert wiso.datev_numbers() is None and wiso.read().datev_numbers is None
        blocker, hints = wiso.check()
    assert blocker == [] and any('SYS_FINACCSYSTEMS' in hint for hint in hints)


def _broken_table(monkeypatch, secret=PASSWORD):
    """Die Tabelle der Fibu-Schnittstellen liefert erst einen anderen Satz mit Zugangsdaten und wirft dann beim Lesen
    einen Fehler, dessen Text einen Wert der Tabelle trägt (ein Dekodier- oder Lesefehler)."""
    real = WisoDatabase._safe_rows

    def broken(self, table, blobs=False):
        if table != 'SYS_FINACCSYSTEMS':
            return real(self, table, blobs)

        def rows():
            yield {'NAME': 'Standard-ASCII', 'CONSULTANT_NO': '1111', 'CLIENT_NO': '22', 'USERPASSWORD': secret}
            raise UnicodeDecodeError('latin-1', secret.encode(), 0, 1, f'kaputt {secret}')
        return rows()
    monkeypatch.setattr(WisoDatabase, '_safe_rows', broken)


def test_a_failing_read_of_the_table_gives_no_numbers_and_says_so_without_a_value(tmp_path, monkeypatch):
    _broken_table(monkeypatch)
    with WisoDatabase(str(_wiso(tmp_path))) as wiso:
        assert wiso.datev_numbers() is None
        data = wiso.read()
    assert data.datev_numbers is None and data.datev_numbers_unreadable is True
    assert PASSWORD not in repr(data) and '1111' not in repr(data)


def test_a_failing_read_does_not_stop_the_import_and_leaves_a_hint(db, tmp_path, monkeypatch):
    _own(db)
    _broken_table(monkeypatch)

    result = _import(db, _wiso(tmp_path))                          # kein Fehler, kein Blocker

    assert result['imported'] == 1 and result['datev_numbers_set'] == 0 and db.get_datev_numbers() == (None, None)
    assert any('SYS_FINACCSYSTEMS' in hint and 'nicht' in hint for hint in result['hints'])
    assert PASSWORD not in json.dumps(result, default=str) and '1111' not in json.dumps(result, default=str)
    with closing(db._get_connection()) as conn:
        dump = '\n'.join(conn.iterdump())
    assert PASSWORD not in dump and 'kaputt' not in dump
    log_dir = userctx.user_data_dir()
    for name in os.listdir(log_dir):
        path = os.path.join(log_dir, name)
        if os.path.isfile(path):
            with open(path, encoding='utf-8', errors='replace') as handle:
                content = handle.read()
            assert PASSWORD not in content and 'kaputt' not in content


def test_a_readable_table_leaves_no_hint_about_a_failing_read(db, tmp_path):
    _own(db)
    result = _import(db, _wiso(tmp_path))
    assert result['datev_numbers_set'] == 1 and not any('SYS_FINACCSYSTEMS' in hint for hint in result['hints'])
    with WisoDatabase(str(_wiso(tmp_path))) as wiso:
        assert wiso.read().datev_numbers_unreadable is False


def test_the_numbers_are_written_only_after_the_bookings(db, tmp_path, monkeypatch):
    _own(db)
    seen = []
    real = Database._fill_datev_numbers

    def spy(self, numbers):
        seen.append(query(db, "SELECT COUNT(*) FROM Bookings WHERE BookingType = 'entry'")[0][0])
        return real(self, numbers)
    monkeypatch.setattr(Database, '_fill_datev_numbers', spy)

    result = _import(db, _wiso(tmp_path))

    assert seen == [1] and result['datev_numbers_set'] == 1 and db.get_datev_numbers() == ('4711', '12')


def test_a_failing_import_of_the_bookings_leaves_the_master_data_unchanged(db, tmp_path, monkeypatch):
    _own(db)

    def boom(self, *args, **kwargs):
        raise RuntimeError('Abbruch beim Schreiben der Buchungen')
    monkeypatch.setattr(Database, '_insert_wiso_bookings', boom)

    with pytest.raises(RuntimeError, match='Abbruch'):
        db.import_wiso_fdb(str(_wiso(tmp_path)), with_assets=False, with_invoices=False)

    assert db.get_datev_numbers() == (None, None)
    assert query(db, 'SELECT DatevConsultantNo, DatevClientNo FROM CompanyDetails WHERE ContactID = 1') == [(None, None)]
    assert query(db, "SELECT COUNT(*) FROM Bookings WHERE BookingType = 'entry'")[0][0] == 0


def test_the_import_fills_the_empty_numbers_of_the_own_company(db, tmp_path):
    _own(db)
    assert db.get_datev_numbers() == (None, None)

    result = _import(db, _wiso(tmp_path))

    assert result['datev_numbers_set'] == 1 and db.get_datev_numbers() == ('4711', '12')
    again = _import(db, _wiso(tmp_path))
    assert again['datev_numbers_set'] == 0 and db.get_datev_numbers() == ('4711', '12')    # beim zweiten Mal nichts mehr


@pytest.mark.parametrize('present', [{'datev_consultant_no': '9999', 'datev_client_no': '77'},
                                     {'datev_consultant_no': '9999'}, {'datev_client_no': '77'}])
def test_a_number_the_user_entered_stays(db, tmp_path, present):
    _own(db, **present)
    before = query(db, 'SELECT DatevConsultantNo, DatevClientNo FROM CompanyDetails WHERE ContactID = 1')

    result = _import(db, _wiso(tmp_path))

    assert result['datev_numbers_set'] == 0
    assert query(db, 'SELECT DatevConsultantNo, DatevClientNo FROM CompanyDetails WHERE ContactID = 1') == before


def test_without_an_own_company_nothing_happens(db, tmp_path):
    result = _import(db, _wiso(tmp_path))
    assert result['datev_numbers_set'] == 0 and db.get_datev_numbers() == (None, None)


def test_a_customer_does_not_get_the_numbers(db, tmp_path):
    db.insert_contact(contact_type='customer', entity_type='company', display_name='Kunde Probe', company_name='Kunde')
    assert _import(db, _wiso(tmp_path))['datev_numbers_set'] == 0
    assert query(db, 'SELECT DatevConsultantNo, DatevClientNo FROM CompanyDetails') == [(None, None)]


def test_an_own_person_has_no_company_details_and_gets_nothing(db, tmp_path):
    db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe', first_name='Erika',
                      last_name='Probe')
    assert _import(db, _wiso(tmp_path))['datev_numbers_set'] == 0 and db.get_datev_numbers() == (None, None)


def test_no_other_value_of_the_table_leaves_the_reader(db, tmp_path):
    """Die Tabelle trägt Zugangsdaten: Weder das Ergebnis des Imports noch die Datenbank noch das Protokoll
    kennen sie."""
    _own(db)
    result = _import(db, _wiso(tmp_path))

    assert PASSWORD not in json.dumps(result, default=str) and '3141592' not in json.dumps(result, default=str)
    with closing(db._get_connection()) as conn:
        dump = '\n'.join(conn.iterdump())
    assert PASSWORD not in dump and '3141592' not in dump and '27182' not in dump      # auch nicht die Nummern anderer Sätze
    log_dir = userctx.user_data_dir()
    for name in os.listdir(log_dir):
        path = os.path.join(log_dir, name)
        if os.path.isfile(path) and name.startswith('sql_operations'):
            with open(path, encoding='utf-8', errors='replace') as handle:
                content = handle.read()
            assert PASSWORD not in content and '3141592' not in content


def test_the_read_result_carries_nothing_but_the_two_numbers_of_the_table(tmp_path):
    with WisoDatabase(str(_wiso(tmp_path))) as wiso:
        data = wiso.read()
    assert PASSWORD not in repr(data) and '3141592' not in repr(data) and data.datev_numbers == ('4711', '12')


# ── Die Hinweise auf der Seite ───────────────────────────────────────────────

def test_the_page_notes_the_filled_keys_and_numbers():
    html = _import_notices({'datev_keys_filled': 3, 'datev_numbers_set': 1})
    assert '3 schon importierte Buchungen' in html and 'Berater- und Mandantennummer' in html


def test_the_page_notes_the_keys_that_did_not_fit_the_rate():
    html = _import_notices({'datev_key_rate_mismatches': 4})
    assert ('4 Buchungen tragen in der App einen anderen Steuersatz als in WISO' in html
            and 'ihr DATEV-Schlüssel wurde nicht übernommen, der Export leitet ihn aus dem Satz der App ab.' in html)


@pytest.mark.parametrize('value', [0, None, '3', True, -1, 2.5, []])
def test_the_page_says_nothing_about_the_rate_without_a_valid_count(value):
    assert 'anderen Steuersatz' not in _import_notices({'datev_key_rate_mismatches': value})


@pytest.mark.parametrize('result', [{}, {'datev_keys_filled': 0, 'datev_numbers_set': 0},
                                    {'datev_keys_filled': '5', 'datev_numbers_set': True},
                                    {'datev_keys_filled': -1, 'datev_numbers_set': None}])
def test_the_page_says_nothing_without_a_valid_count(result):
    html = _import_notices(result)
    assert 'BU-Schlüssel' not in html and 'Berater- und Mandantennummer' not in html


def test_keys_of_the_module_are_the_ones_the_importers_use():
    assert datev_keys.parse_key('401') == 401 and datev_keys.consultant_no('4711') == '4711'
