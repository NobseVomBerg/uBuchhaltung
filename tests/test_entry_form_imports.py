# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die WISO-Importe und die Form des Buchungssatzes (``Bookings.EntryForm``).

Beide Importe schreiben die Form mit, wie sie das Vorzeichen gesetzt haben: Konto liquide →
``liquid_first``, Gegenkonto liquide → ``purpose_first``, beide → Spiegel mit dem Rohbetrag
(``mirror_coa``), keine → ``transfer`` (WISO meint „Konto im Soll“, auch bei leerem oder
unbekanntem Gegenkonto). Dazu:

* ``negative_gross`` und ``stock_range_accounts`` im Ergebnis (und auf der Seite *Verschiedenes*):
  ein negativer Brutto fällt aus WISOs Muster (Wächter), und Konten 1000–1099 sind in SKR04 Vorräte;
* ``negative_gross`` zählt jeden Satz mit negativem Brutto und genau einer liquiden Seite;
* kein Import ändert die Form einer vorhandenen Zeile: Ein Spiegel des Bestands (``mirror_legacy``)
  bleibt, wie er ist – auch wenn der Re-Import ihn wiedererkennt, mit gleichem oder umgekehrtem
  Vorzeichen, oder die Übernahme ihn aufnimmt (``legacy_mirrors`` zählt ihn); eine abweichende andere
  Form wird nur gemeldet (``form_conflicts``);
* der Weg „erst CSV, dann Datenbank“: Ein Spiegel mit umgekehrtem Vorzeichen wird übernommen und
  nicht doppelt angelegt;
* der Re-Import des Textexports erkennt einen vorzeichenverkehrten Spiegel des Bestands und legt
  keinen zweiten an;
* Zeilen auf Konten 1000–1099, die eine ältere Version mit ihrer Lesart (diese Konten als Kasse, also
  mit einem anderen Betrag) gespeichert hat, erkennt der Re-Import des Textexports und die Übernahme der
  Datenbank wieder (``legacy_stock_rows``): kein zweiter Satz, die vorhandene Zeile bleibt.

Alle Namen und Werte sind erfunden.
"""
import json
import os
from decimal import Decimal

import pytest

import userctx
from db.matching import (
    FORM_LIQUID_FIRST, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY,
    FORM_PURPOSE_FIRST, FORM_TRANSFER,
)
from server.pages_miscellaneous import PageMiscellaneous
from tests.entry_form_helpers import (
    D1, D2, bank_account, coa, csv_export, datev_rows, euer, execute, minor, query, wiso_database,
    wiso_row,
)

KEY19 = '401'


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Form-Bank-Eins', 1800)
    return tmp_db


def _forms(db):
    """{Belegnummer: (Betrag in Minor Units, Form)} der Sätze mit Beleg-Nr."""
    return {row[0]: (row[1], row[2]) for row in query(
        db, "SELECT DocumentNumber, Amount, EntryForm FROM Bookings WHERE BookingType = 'entry' "
            "AND DocumentNumber IS NOT NULL ORDER BY ID")}


def _fdb(tmp_path, rows, name='DB1.FDB', extra_chart=()):
    return wiso_database(tmp_path / name, rows, extra_chart)


def _import_fdb(db, path):
    result = db.import_wiso_fdb(path, with_assets=False, with_invoices=False)
    assert not result['errors'], result['errors']
    return result


def _sql_log(db):
    path = os.path.join(userctx.user_data_dir(), 'sql_operations.sql')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as handle:
        return handle.read().splitlines()


# ── Textexport (CSV): die Form ───────────────────────────────────────────────

def test_csv_writes_the_form_the_sign_was_set_with(db):
    db.insert_chart_of_accounts(4, 1650, 'Nebenkasse 50', 'Test')
    result = db.import_wiso_csv(csv_export(
        ('1800', '4400', '119,00', 'LF', KEY19),         # Konto liquide: Einnahme
        ('6815', '1800', '59,50', 'PF', KEY19),          # Gegenkonto liquide: Ausgabe
        ('1600', '1800', '100,00', 'MI', ''),            # beide liquide: Spiegel, Rohbetrag
        ('4405', '4400', '119,00', 'TR', KEY19),         # keine liquide: Umbuchung
        ('6815', '1650', '10,00', 'T2', ''),             # 1650 ist kein Geldkonto: Umbuchung
        ('6815', '', '5,00', 'GL', ''),                  # keine Seite liquide, Gegenkonto leer
        ('', '4400', '5,00', 'VL', ''),                  # keine Seite liquide, Konto leer
    ))

    assert result['imported'] == 7 and not result['errors'], result
    assert _forms(db) == {
        'LF': (minor('119.00'), FORM_LIQUID_FIRST), 'PF': (minor('-59.50'), FORM_PURPOSE_FIRST),
        'MI': (minor('100.00'), FORM_MIRROR_COA), 'TR': (minor('119.00'), FORM_TRANSFER),
        'T2': (minor('10.00'), FORM_TRANSFER), 'GL': (minor('5.00'), FORM_TRANSFER),
        'VL': (minor('5.00'), FORM_TRANSFER)}


def test_csv_an_account_missing_in_the_chart_is_no_side_of_the_entry(db):
    """Ein Konto, das es im Kontenrahmen nicht gibt, hat keine COA-ID – der Satz bleibt eine Zahlung."""
    result = db.import_wiso_csv(csv_export(('6815', '1800', '59,50', 'XX', KEY19),
                                           ('9999', '1800', '10,00', 'YY', '')))
    assert result['missing_coa'] == [9999]
    assert _forms(db)['YY'] == (minor('-10.00'), FORM_PURPOSE_FIRST)


def test_csv_reimport_creates_nothing_new(db):
    lines = csv_export(('1600', '1800', '100,00', 'MI', ''), ('6815', '1800', '59,50', 'PF', KEY19))
    db.import_wiso_csv(lines)
    before = _forms(db)

    again = db.import_wiso_csv(lines)

    assert (again['imported'], again['skipped'], again['legacy_mirrors']) == (0, 2, 0)
    assert _forms(db) == before


# ── Textexport: negative Bruttobeträge und Konten 1000–1099 ─────────────────

def test_csv_counts_every_negative_gross_row_with_exactly_one_liquid_side(db):
    """Dort bestimmt die Geldseite das Vorzeichen – ob die Zahl dabei ihr Vorzeichen ändert oder
    behält. WISO schreibt den Brutto nicht negativ; jede solche Zeile gehört in die Meldung."""
    result = db.import_wiso_csv(csv_export(
        ('6815', '1800', '-119,00', 'N1', KEY19),        # Storno Aufwand: bleibt −119 – gezählt
        ('1800', '4400', '-50,00', 'N2', KEY19),         # Storno Erlös: wird +50 – gezählt
        ('6815', '1800', '119,00', 'P1', KEY19),         # positiv: nicht
        ('4405', '4400', '-119,00', 'N3', KEY19),        # keine liquide Seite: Rohbetrag, nicht gezählt
        ('1600', '1800', '-100,00', 'N4', ''),           # zwei liquide Seiten: Rohbetrag, nicht gezählt
    ))

    assert result['negative_gross'] == 2
    forms = _forms(db)
    assert forms['N1'] == (minor('-119.00'), FORM_PURPOSE_FIRST)      # das Vorzeichen der Geldbewegung
    assert forms['N2'] == (minor('50.00'), FORM_LIQUID_FIRST)
    assert forms['N3'] == (minor('-119.00'), FORM_TRANSFER)
    assert forms['N4'] == (minor('-100.00'), FORM_MIRROR_COA)


def test_csv_does_not_count_a_negative_gross_row_without_a_liquid_side(db):
    result = db.import_wiso_csv(csv_export(('4405', '4400', '-119,00', 'N3', KEY19)))
    assert result['negative_gross'] == 0


def test_csv_reports_accounts_in_the_old_cash_range(db):
    db.insert_chart_of_accounts(4, 1050, 'Vorrat Bürobedarf', 'Test')
    db.insert_chart_of_accounts(4, 1099, 'Vorrat Sonstiges', 'Test')
    result = db.import_wiso_csv(csv_export(
        ('6815', '1050', '10,00', 'S1', ''), ('1050', '4400', '20,00', 'S2', ''),
        ('6815', '1099', '30,00', 'S3', ''), ('1050', '1050', '1,00', 'S4', ''),
        ('6815', '1800', '59,50', 'S5', KEY19)))

    assert result['stock_range_accounts'] == [
        {'nummer': 1050, 'bezeichnung': 'Vorrat Bürobedarf', 'anzahl': 3},   # S4 zählt einmal
        {'nummer': 1099, 'bezeichnung': 'Vorrat Sonstiges', 'anzahl': 1}]
    # und sie sind kein Geldkonto mehr: der Betrag bleibt, wie die Datei ihn nennt
    assert _forms(db)['S1'] == (minor('10.00'), FORM_TRANSFER)
    assert _forms(db)['S2'] == (minor('20.00'), FORM_TRANSFER)


def test_csv_without_notable_rows_reports_nothing(db):
    result = db.import_wiso_csv(csv_export(('6815', '1800', '59,50', 'OK', KEY19)))
    assert (result['negative_gross'], result['stock_range_accounts']) == (0, [])


# ── Textexport: ein vorzeichenverkehrter Spiegel des Bestands ────────────────

def _legacy_mirror(db, document, coa_nr, counter_nr, amount, date='2026-01-02'):
    """Ein Spiegel des Bestands: wie nach der Migration, ``mirror_legacy``."""
    return db.insert_booking(date, amount, coa_id=coa(db, coa_nr), counter_coa_id=coa(db, counter_nr),
                             document_number=document, text=f'Posten {document}',
                             booking_type='entry', entry_form=FORM_MIRROR_LEGACY)


def test_csv_reimport_recognises_a_legacy_mirror_with_the_opposite_sign_and_leaves_it_alone(db):
    """Die Datei sagt 1600 an 1800, +100 (Konto im Soll); der Bestand führt den Spiegel mit −100
    (Richtung auf dem Gegenkonto). Das ist dieselbe Buchung: Der Import legt keine zweite an – und
    die vorhandene Zeile bleibt, wie sie ist (``mirror_legacy``, ihr Betrag auch): welche Lesart sie
    hat, entscheidet nicht der Import."""
    twin = _legacy_mirror(db, 'TW-1', 1600, 1800, -100)
    before = query(db, 'SELECT * FROM Bookings WHERE ID = ?', (twin,))

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['legacy_mirrors']) == (0, 1, 1)
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(1,)]
    assert query(db, 'SELECT * FROM Bookings WHERE ID = ?', (twin,)) == before
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings WHERE ID = ?', (twin,)) == [
        (minor('-100.00'), FORM_MIRROR_LEGACY)]


def test_csv_reimport_counts_a_legacy_mirror_with_the_same_sign_and_leaves_it_alone(db):
    """Der Normalfall nach der Migration: Die Zeile steht mit dem Betrag der Datei im Bestand
    (``mirror_legacy``). Sie ist ein gewöhnliches Duplikat – nichts wird angelegt, nichts geändert; die
    Meldung zählt sie als älteren Übertrag in der alten Lesart."""
    twin = _legacy_mirror(db, 'TW-1', 1600, 1800, 100)

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['legacy_mirrors']) == (0, 1, 1)
    assert query(db, 'SELECT COUNT(*), MAX(EntryForm) FROM Bookings') == [(1, FORM_MIRROR_LEGACY)]
    assert query(db, 'SELECT ID FROM Bookings') == [(twin,)]


def test_csv_reimport_counts_a_legacy_mirror_once_however_often_the_file_names_it(db):
    _legacy_mirror(db, 'TW-1', 1600, 1800, 100)

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', ''),
                                           ('1600', '1800', '100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['legacy_mirrors']) == (1, 1, 1)


def test_csv_reimport_does_not_count_a_mirror_that_is_no_legacy_one(db):
    db.insert_booking('2026-01-02', 100, coa_id=coa(db, 1600), counter_coa_id=coa(db, 1800),
                      document_number='TW-1', text='Posten TW-1', booking_type='entry',
                      entry_form=FORM_MIRROR_COA)

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['legacy_mirrors']) == (0, 1, 0)


def test_csv_reimport_writes_nothing_into_the_sql_log_for_a_legacy_mirror(db):
    _legacy_mirror(db, 'TW-1', 1600, 1800, -100)
    before = len(_sql_log(db))

    db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', '')))

    assert _sql_log(db)[before:] == []


def test_csv_reimport_uses_each_twin_once(db):
    twin = _legacy_mirror(db, 'TW-1', 1600, 1800, -100)

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', ''),
                                           ('1600', '1800', '100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['legacy_mirrors']) == (1, 1, 1)
    assert query(db, 'SELECT EntryForm FROM Bookings WHERE ID = ?', (twin,)) == [(FORM_MIRROR_LEGACY,)]


@pytest.mark.parametrize('why, setup, line', [
    ('anderes-gegenkonto', lambda db: _legacy_mirror(db, 'TW-2', 1600, 1810, -100),
     ('1600', '1800', '100,00', 'TW-2', '')),
    ('anderes-datum', lambda db: _legacy_mirror(db, 'TW-2', 1600, 1800, -100, date='2026-01-03'),
     ('1600', '1800', '100,00', 'TW-2', '')),
    ('andere-beleg-nr', lambda db: _legacy_mirror(db, 'TW-3', 1600, 1800, -100),
     ('1600', '1800', '100,00', 'TW-2', '')),
    ('anderer-betrag', lambda db: _legacy_mirror(db, 'TW-2', 1600, 1800, -90),
     ('1600', '1800', '100,00', 'TW-2', '')),
    ('andere-form', lambda db: db.insert_booking(
        '2026-01-02', -100, coa_id=coa(db, 1600), counter_coa_id=coa(db, 1800),
        document_number='TW-2', booking_type='entry', entry_form=FORM_MIRROR_COUNTER),
     ('1600', '1800', '100,00', 'TW-2', '')),
    ('keine-beleg-nr', lambda db: _legacy_mirror(db, '', 1600, 1800, -100),
     ('1600', '1800', '100,00', '', '')),
    ('nur-eine-seite-liquide', lambda db: db.insert_booking(
        '2026-01-02', 100, coa_id=coa(db, 6815), counter_coa_id=coa(db, 1800),
        document_number='TW-2', booking_type='entry', entry_form=FORM_MIRROR_LEGACY),
     ('6815', '1800', '100,00', 'TW-2', '')),
])
def test_csv_reimport_leaves_everything_else_alone(db, why, setup, line):
    """Im Zweifel wird neu angelegt: Jede Abweichung in Beleg, Datum, Konto, Gegenkonto, Betrag oder
    Form des vorhandenen Satzes verhindert die Wiedererkennung – und nichts am Bestand ändert sich."""
    existing = setup(db)
    before = query(db, 'SELECT Amount, EntryForm FROM Bookings WHERE ID = ?', (existing,))

    result = db.import_wiso_csv(csv_export(line))

    assert result['legacy_mirrors'] == 0, why
    assert result['imported'] == 1, why
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings WHERE ID = ?', (existing,)) == before


def test_csv_reimport_does_not_take_a_twin_that_the_file_claims_itself(db):
    """Die Datei enthält die Buchung und ihr Gegenstück (+100 und −100). Der Bestand führt −100: Das ist
    das Duplikat der −100-Zeile, nicht der Zwilling der +100-Zeile."""
    _legacy_mirror(db, 'TW-1', 1600, 1800, -100)

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', ''),
                                           ('1600', '1800', '-100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['legacy_mirrors']) == (1, 1, 1)
    assert query(db, 'SELECT EntryForm FROM Bookings ORDER BY ID') == [
        (FORM_MIRROR_LEGACY,), (FORM_MIRROR_COA,)]


# ── Zeilen auf Konten 1000–1099 aus einer älteren Version ──────────────────────

def _stock_state(db):
    """Alles, was sich durch einen Re-Import nicht ändern darf: jede Spalte jeder Buchung, DATEV-Zeilen,
    EÜR und Dashboard."""
    totals = db.get_dashboard_totals(D1, D2)
    return (query(db, 'SELECT * FROM Bookings ORDER BY ID'), datev_rows(db), euer(db),
            (totals['income'], totals['expense']))


def _stored_by_2_0_x(db, kind, with_account=True, expense=6815, counter_missing=False):
    """Eine Zeile, wie 2.0.x sie aus dem Textexport gespeichert hat (Konten 1000–1099 galten als Kasse) und
    wie die Migration sie festgeschrieben hat. ``kind``: 'counter' – „6815;1050;10“ (Gegenkonto in 1000–1099:
    Abgang −10, ``purpose_first``), 'account' – „1050;1800;30“ (beide Seiten liquide: Rohbetrag +30,
    ``mirror_legacy``)."""
    if with_account:
        db.insert_chart_of_accounts(4, 1050, 'Vorrat Bürobedarf', 'Test')
    stock = coa(db, 1050)
    if kind == 'counter':
        return db.insert_booking('2026-01-02', Decimal('-10.00'), coa_id=coa(db, expense),
                                 counter_coa_id=None if counter_missing else stock,
                                 document_number='LS-1', text='Posten LS-1', booking_type='entry',
                                 entry_form=FORM_PURPOSE_FIRST)
    return db.insert_booking('2026-01-03', Decimal('30.00'), coa_id=stock, counter_coa_id=coa(db, 1800),
                             document_number='LS-2', text='Posten LS-2', booking_type='entry',
                             entry_form=FORM_MIRROR_LEGACY)


STOCK_LINES = {'counter': ('6815', '1050', '10,00', 'LS-1', '', '02.01.2026'),
               'account': ('1050', '1800', '30,00', 'LS-2', '', '03.01.2026')}


@pytest.mark.parametrize('with_account', [True, False], ids=['konto-im-rahmen', 'konto-fehlt'])
@pytest.mark.parametrize('kind', ['counter', 'account'])
def test_csv_reimport_does_not_double_a_row_an_older_version_stored_on_an_account_in_1000_1099(
        db, kind, with_account):
    """Die Zeile steht mit dem Betrag der alten Regel im Bestand (−10 bzw. +30). Der Import würde sie heute
    mit einem anderen Betrag schreiben (+10 Umbuchung bzw. −30 Zweckkonto vorne) und fände sie nicht
    wieder: sie stünde doppelt im Export. Stattdessen ist sie ein Duplikat – die vorhandene Zeile bleibt
    Spalte für Spalte, EÜR, DATEV und Dashboard ändern sich nicht, der Zähler stimmt."""
    _stored_by_2_0_x(db, kind, with_account)
    before = _stock_state(db)

    result = db.import_wiso_csv(csv_export(STOCK_LINES[kind]))

    assert (result['imported'], result['skipped'], result['legacy_stock_rows']) == (0, 1, 1)
    assert result['stock_range_accounts'][0]['nummer'] == 1050              # die Meldung bleibt
    assert _stock_state(db) == before
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(1,)]


def test_csv_import_on_a_fresh_database_writes_the_stock_rows_as_usual(db):
    """Kein Fehltreffer: Ohne Altzeile legt der Import beide Zeilen an (Umbuchung bzw. Zweckkonto vorne)."""
    db.insert_chart_of_accounts(4, 1050, 'Vorrat Bürobedarf', 'Test')

    result = db.import_wiso_csv(csv_export(STOCK_LINES['counter'], STOCK_LINES['account']))

    assert (result['imported'], result['skipped'], result['legacy_stock_rows']) == (2, 0, 0)
    assert _forms(db) == {'LS-1': (minor('10.00'), FORM_TRANSFER),
                          'LS-2': (minor('-30.00'), FORM_PURPOSE_FIRST)}


def test_csv_reimport_of_a_fresh_import_counts_no_legacy_stock_rows(db):
    db.insert_chart_of_accounts(4, 1050, 'Vorrat Bürobedarf', 'Test')
    lines = csv_export(STOCK_LINES['counter'], STOCK_LINES['account'])
    db.import_wiso_csv(lines)
    before = _stock_state(db)

    again = db.import_wiso_csv(lines)

    assert (again['imported'], again['skipped'], again['legacy_stock_rows']) == (0, 2, 0)
    assert _stock_state(db) == before


def test_csv_reimport_creates_only_the_row_the_older_version_never_had(db):
    """Zwei echte gleiche Sätze in der Datei, eine Altzeile im Bestand: Die Altzeile ist das Duplikat
    EINER Zeile, die andere ist neu."""
    old = _stored_by_2_0_x(db, 'counter')
    line = STOCK_LINES['counter']

    result = db.import_wiso_csv(csv_export(line, line))

    assert (result['imported'], result['skipped'], result['legacy_stock_rows']) == (1, 1, 1)
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(2,)]
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings WHERE ID = ?', (old,)) == [
        (minor('-10.00'), FORM_PURPOSE_FIRST)]
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings WHERE ID != ?', (old,)) == [
        (minor('10.00'), FORM_TRANSFER)]


def test_csv_reimport_leaves_a_row_alone_that_another_row_of_the_file_claims_with_its_own_amount(db):
    """Die Datei hat auch „6815;1800;10“ (Abgang −10): Mit dem Betrag der Altzeile gehört sie dieser Zeile.
    Eine zweite Zeile mit demselben Betrag bekommt sie nicht noch einmal."""
    _stored_by_2_0_x(db, 'counter')

    result = db.import_wiso_csv(csv_export(('6815', '1050', '10,00', 'LS-1', ''),
                                           ('6815', '1800', '10,00', 'LS-1', '')))

    assert (result['imported'], result['skipped'], result['legacy_stock_rows']) == (1, 1, 0)


@pytest.mark.parametrize('why, existing, line', [
    ('anderes-datum', dict(date='2026-01-05'), STOCK_LINES['counter']),
    ('andere-beleg-nr', dict(document='LS-9'), STOCK_LINES['counter']),
    ('anderes-gegenkonto', dict(counter=1810), STOCK_LINES['counter']),
    ('anderes-konto', dict(coa=4400), STOCK_LINES['counter']),
    ('anderer-betrag', dict(amount='-12.00'), STOCK_LINES['counter']),
    ('betrag-der-neuen-regel', dict(amount='10.00'), STOCK_LINES['counter']),
])
def test_csv_reimport_needs_everything_to_match_for_a_row_of_an_older_version(db, why, existing, line):
    """Im Zweifel wird neu angelegt: Datum, Beleg-Nr., Konto, Gegenkonto und der Betrag der alten Regel
    müssen alle stimmen."""
    db.insert_chart_of_accounts(4, 1050, 'Vorrat Bürobedarf', 'Test')
    row = dict(date='2026-01-02', document='LS-1', coa=6815, counter=1050, amount='-10.00')
    row.update(existing)
    old = db.insert_booking(row['date'], Decimal(row['amount']), coa_id=coa(db, row['coa']),
                            counter_coa_id=coa(db, row['counter']), document_number=row['document'],
                            text='Posten', booking_type='entry', entry_form=FORM_PURPOSE_FIRST)
    before = query(db, 'SELECT * FROM Bookings WHERE ID = ?', (old,))

    result = db.import_wiso_csv(csv_export(line))

    # „betrag-der-neuen-regel“ ist ein gewöhnliches Duplikat (Schlüssel ohne Gegenkonto): kein neuer Satz,
    # aber auch nichts, das die Altzeilen-Regel zählt
    assert result['legacy_stock_rows'] == 0, why
    assert query(db, 'SELECT * FROM Bookings WHERE ID = ?', (old,)) == before
    assert result['imported'] == (0 if why == 'betrag-der-neuen-regel' else 1), why


def test_csv_reimport_does_not_look_for_an_older_amount_when_the_amount_is_the_same(db):
    """„1050;4400;20“: Konto in 1000–1099 und Rohbetrag – 2.0.x und heute geben denselben Betrag (+20). Die
    gewöhnliche Duplikatsuche entscheidet, und die Altzeile-Regel zählt nichts."""
    db.insert_chart_of_accounts(4, 1050, 'Vorrat Bürobedarf', 'Test')
    old = db.insert_booking('2026-01-02', Decimal('20.00'), coa_id=coa(db, 1050),
                            counter_coa_id=coa(db, 4400), document_number='LS-3', text='Posten LS-3',
                            booking_type='entry', entry_form=FORM_LIQUID_FIRST)

    result = db.import_wiso_csv(csv_export(('1050', '4400', '20,00', 'LS-3', '', '02.01.2026')))

    assert (result['imported'], result['skipped'], result['legacy_stock_rows']) == (0, 1, 0)
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(1,)]
    assert old


def test_csv_reimport_finds_an_older_row_without_a_document_number_too(db):
    db.insert_chart_of_accounts(4, 1050, 'Vorrat Bürobedarf', 'Test')
    old = db.insert_booking('2026-01-02', Decimal('-10.00'), coa_id=coa(db, 6815),
                            counter_coa_id=coa(db, 1050), document_number=None, text='Ohne Beleg',
                            booking_type='entry', entry_form=FORM_PURPOSE_FIRST)
    before = query(db, 'SELECT * FROM Bookings WHERE ID = ?', (old,))

    result = db.import_wiso_csv(csv_export(('6815', '1050', '10,00', '', '', '02.01.2026')))

    assert (result['imported'], result['skipped'], result['legacy_stock_rows']) == (0, 1, 1)
    assert query(db, 'SELECT * FROM Bookings WHERE ID = ?', (old,)) == before


# ── Tabellen-Export: ein Konto wird erst hier gefüllt ────────────────────────

TABLE_HEADER = 'Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr.;Betrag\n'


def test_the_table_export_derives_the_form_again_when_it_fills_the_account(db):
    """Ein Satz ohne Konto vorne (nur das Gegenkonto 4400) bekommt sein Konto aus der Kategorie: aus
    „Konto vorne leer“ wird eine Umbuchung zwischen zwei Zweckkonten – die Form folgt."""
    entry = db.insert_booking('2026-01-02', -10, counter_coa_id=coa(db, 4400), document_number='TB-1',
                              booking_type='entry')
    assert query(db, 'SELECT EntryForm FROM Bookings WHERE ID = ?', (entry,)) == [(FORM_LIQUID_FIRST,)]

    result = db.import_wiso_csv((TABLE_HEADER + '02.01.2026;Empfänger;Zweck;Bürobedarf;TB-1;-10,00\n')
                                .encode('cp1252'))

    assert result['updated'] == 1 and not result['errors'], result
    assert query(db, 'SELECT COA_ID, Amount, EntryForm FROM Bookings WHERE ID = ?', (entry,)) == [
        (coa(db, 6815), minor('-10.00'), FORM_TRANSFER)]


# ── WISO-Datenbank: die Form ─────────────────────────────────────────────────

FDB_ROWS = [
    wiso_row(4980, 1000, 21.40, 'FD-KA'),            # Ausgabe über die Kasse (SKR03 1000 → 1600)
    wiso_row(1000, 8400, 60.0, 'FD-KE'),             # Einnahme über die Kasse
    wiso_row(1000, 1200, 90.0, 'FD-AB'),             # Abhebung: Spiegel
    wiso_row(1200, 1000, 45.0, 'FD-EZ'),             # Einzahlung: Spiegel
    wiso_row(1360, 1200, 55.0, 'FD-VK'),             # Verrechnungskonto an Bank: Spiegel
    wiso_row(8400, 4980, 12.0, 'FD-TR'),             # keine liquide Seite: Umbuchung
    wiso_row(4980, 1200, -30.0, 'FD-NG'),            # negativer Brutto bei einer liquiden Seite
]


def test_fdb_writes_the_form_the_sign_was_set_with(db, tmp_path):
    result = _import_fdb(db, _fdb(tmp_path, FDB_ROWS))

    assert result['imported'] == len(FDB_ROWS)
    assert _forms(db) == {
        'FD-KA': (minor('-21.40'), FORM_PURPOSE_FIRST), 'FD-KE': (minor('60.00'), FORM_LIQUID_FIRST),
        'FD-AB': (minor('90.00'), FORM_MIRROR_COA), 'FD-EZ': (minor('45.00'), FORM_MIRROR_COA),
        'FD-VK': (minor('55.00'), FORM_MIRROR_COA), 'FD-TR': (minor('12.00'), FORM_TRANSFER),
        'FD-NG': (minor('-30.00'), FORM_PURPOSE_FIRST)}


def test_fdb_counts_every_negative_gross_row_with_exactly_one_liquid_side(db, tmp_path):
    rows = FDB_ROWS + [wiso_row(1200, 8400, -25.0, 'FD-NK'),         # Storno Erlös: wird +25, gezählt
                       wiso_row(1000, 1200, -5.0, 'FD-SP'),          # zwei liquide Seiten: nicht gezählt
                       wiso_row(8400, 4980, -6.0, 'FD-NT')]          # keine liquide Seite: nicht gezählt

    result = _import_fdb(db, _fdb(tmp_path, rows))

    assert result['negative_gross'] == 2                              # FD-NG (Storno Aufwand) und FD-NK
    forms = _forms(db)
    assert forms['FD-NG'] == (minor('-30.00'), FORM_PURPOSE_FIRST)   # bleibt negativ: gezählt trotzdem
    assert forms['FD-NK'] == (minor('25.00'), FORM_LIQUID_FIRST)
    assert forms['FD-SP'] == (minor('-5.00'), FORM_MIRROR_COA)
    assert forms['FD-NT'] == (minor('-6.00'), FORM_TRANSFER)


def test_fdb_does_not_count_a_negative_gross_row_without_a_liquid_side(db, tmp_path):
    result = _import_fdb(db, _fdb(tmp_path, [wiso_row(8400, 4980, -6.0, 'FD-NT')]))
    assert result['negative_gross'] == 0


def test_fdb_without_a_liquid_side_puts_the_account_in_the_debit_even_with_an_unknown_counter(db, tmp_path):
    """WISO meint „Konto im Soll“: keine Seite ein Zahlungskonto → ``transfer`` mit dem Rohbetrag, auch
    wenn das Gegenkonto im Kontenrahmen fehlt (nicht ``purpose_first``: dort stünde das Gegenkonto im Soll)."""
    result = _import_fdb(db, _fdb(tmp_path, [wiso_row(4980, 9999, 10.0, 'FD-A6')]))

    assert 9999 in result['missing_coa']
    assert _forms(db)['FD-A6'] == (minor('10.00'), FORM_TRANSFER)
    assert datev_rows(db)['FD-A6'][:3] == ('10,00', 'S', '6850')


def test_fdb_reports_accounts_in_the_old_cash_range(db, tmp_path):
    result = _import_fdb(db, _fdb(tmp_path, [
        wiso_row(4980, 1040, 10.0, 'FD-S1'), wiso_row(1040, 8400, 20.0, 'FD-S2'),
        wiso_row(4980, 1200, 30.0, 'FD-OK')], extra_chart=[(1040, 1050, 'Vorrat Bürobedarf')]))

    assert result['stock_range_accounts'] == [
        {'nummer': 1050, 'bezeichnung': 'Vorrat Bürobedarf', 'anzahl': 2}]
    assert _forms(db)['FD-S1'] == (minor('10.00'), FORM_TRANSFER)        # kein Geldkonto mehr


def test_fdb_without_notable_rows_reports_nothing(db, tmp_path):
    result = _import_fdb(db, _fdb(tmp_path, [wiso_row(4980, 1200, 30.0, 'FD-OK')]))
    assert (result['negative_gross'], result['stock_range_accounts']) == (0, [])
    assert (result['legacy_mirrors'], result['form_conflicts'], result['form_conflicts_count']) == \
        (0, [], 0)


# ── WISO-Datenbank: der Re-Import ändert keine Form ───────────────────────────

def _as_legacy(db, *documents):
    """Die Sätze, als hätte sie 2.0.x geschrieben und die Migration ``mirror_legacy`` daraus gemacht."""
    for document in documents:
        execute(db, 'UPDATE Bookings SET EntryForm = ? WHERE DocumentNumber = ?',
                (FORM_MIRROR_LEGACY, document))


def test_fdb_reimport_counts_the_legacy_mirrors_and_changes_nothing(db, tmp_path):
    path = _fdb(tmp_path, FDB_ROWS)
    _import_fdb(db, path)
    _as_legacy(db, 'FD-AB', 'FD-EZ', 'FD-VK')
    before = query(db, 'SELECT * FROM Bookings ORDER BY ID')

    again = _import_fdb(db, path)

    assert (again['imported'], again['skipped'], again['legacy_mirrors']) == (0, len(FDB_ROWS), 3)
    assert again['form_conflicts_count'] == 0
    assert query(db, 'SELECT * FROM Bookings ORDER BY ID') == before          # keine Zeile verändert
    assert [_forms(db)[doc][1] for doc in ('FD-AB', 'FD-EZ', 'FD-VK')] == [FORM_MIRROR_LEGACY] * 3

    assert _import_fdb(db, path)['legacy_mirrors'] == 3         # und beim nächsten Mal dasselbe


def test_fdb_reimport_leaves_a_mirror_with_the_opposite_sign_alone(db, tmp_path):
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'FD-AB')])
    _import_fdb(db, path)
    _as_legacy(db, 'FD-AB')
    execute(db, "UPDATE Bookings SET Amount = ? WHERE DocumentNumber = 'FD-AB'", (minor('-90.00'),))

    again = _import_fdb(db, path)

    assert again['legacy_mirrors'] == 1
    assert _forms(db)['FD-AB'] == (minor('-90.00'), FORM_MIRROR_LEGACY)


def test_fdb_reimport_writes_no_form_into_the_sql_log(db, tmp_path):
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'FD-AB')])
    _import_fdb(db, path)
    _as_legacy(db, 'FD-AB')
    before = len(_sql_log(db))

    _import_fdb(db, path)

    assert [line for line in _sql_log(db)[before:] if 'EntryForm' in line] == []


def test_fdb_reimport_reports_a_different_form_and_changes_nothing(db, tmp_path):
    """Gespeichert ist „Umbuchung“, der frische Import würde „Zweckkonto vorne“ schreiben (das Konto
    war damals kein Geldkonto). Das ist ein Befund, keine Korrektur."""
    path = _fdb(tmp_path, [wiso_row(4980, 1200, 30.0, 'FD-BA')])
    _import_fdb(db, path)
    execute(db, "UPDATE Bookings SET EntryForm = ?, Amount = ? WHERE DocumentNumber = 'FD-BA'",
            (FORM_TRANSFER, minor('30.00')))

    again = _import_fdb(db, path)

    assert (again['imported'], again['legacy_mirrors'], again['form_conflicts_count']) == (0, 0, 1)
    (conflict,) = again['form_conflicts']
    assert (conflict['stored'], conflict['fresh'], conflict['date'], conflict['amount']) == (
        FORM_TRANSFER, FORM_PURPOSE_FIRST, '2026-01-02', 30.0)
    assert _forms(db)['FD-BA'] == (minor('30.00'), FORM_TRANSFER)


def test_fdb_reimport_reports_a_legacy_mirror_the_fresh_import_would_not_write_as_one(db, tmp_path):
    """Ein Spiegel des Bestands auf einem Konto, das heute kein Geldkonto mehr ist (1050)."""
    path = _fdb(tmp_path, [wiso_row(1000, 1040, 10.0, 'FD-OLD')],
                extra_chart=[(1040, 1050, 'Vorrat Bürobedarf')])
    _import_fdb(db, path)
    execute(db, "UPDATE Bookings SET EntryForm = ? WHERE DocumentNumber = 'FD-OLD'",
            (FORM_MIRROR_LEGACY,))

    again = _import_fdb(db, path)

    assert (again['legacy_mirrors'], again['form_conflicts_count']) == (0, 1)
    assert _forms(db)['FD-OLD'][1] == FORM_MIRROR_LEGACY


def test_fdb_reimport_does_not_report_two_mirrors_that_disagree_only_in_their_convention(db, tmp_path):
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'FD-AB')])
    _import_fdb(db, path)
    execute(db, "UPDATE Bookings SET EntryForm = ? WHERE DocumentNumber = 'FD-AB'",
            (FORM_MIRROR_COUNTER,))

    again = _import_fdb(db, path)

    assert (again['legacy_mirrors'], again['form_conflicts_count']) == (0, 0)
    assert _forms(db)['FD-AB'][1] == FORM_MIRROR_COUNTER


# ── WISO-Datenbank nach dem Textexport: Übernahme ─────────────────────────────

def test_adoption_takes_a_legacy_mirror_and_leaves_its_form(db, tmp_path):
    db.import_wiso_csv(csv_export(('1600', '1800', '90,00', 'AD-1', ''),
                                  ('1800', '4400', '119,00', 'AD-2', KEY19),
                                  ('6850', '1800', '59,50', 'AD-3', KEY19)))
    _as_legacy(db, 'AD-1')
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'AD-1', text='Posten AD-1'),
                           wiso_row(1200, 8400, 119.0, 'AD-2', text='Posten AD-2'),
                           wiso_row(4980, 1200, 59.5, 'AD-3', text='Posten AD-3')])

    result = _import_fdb(db, path)

    assert (result['imported'], result['adopted'], result['legacy_mirrors']) == (0, 3, 1)
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(3,)]
    assert [form for _amount, form in _forms(db).values()] == [
        FORM_MIRROR_LEGACY, FORM_LIQUID_FIRST, FORM_PURPOSE_FIRST]


def test_adoption_takes_a_legacy_mirror_with_the_opposite_sign_and_leaves_it_alone(db, tmp_path):
    """E4: Konto, Gegenkonto, Beleg-Nr. und Text gleich, nur das Vorzeichen abweicht – übernommen
    wird die vorhandene Zeile (nur die Quellgruppe kommt dazu): Betrag und Form bleiben."""
    twin = _legacy_mirror(db, 'AD-1', 1600, 1800, -90)
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'AD-1', text='Posten AD-1')])

    result = _import_fdb(db, path)

    assert (result['imported'], result['adopted'], result['legacy_mirrors']) == (0, 1, 1)
    assert query(db, 'SELECT Amount, EntryForm, SourceGroup FROM Bookings WHERE ID = ?', (twin,)) == [
        (minor('-90.00'), FORM_MIRROR_LEGACY, '701')]
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(1,)]


@pytest.mark.parametrize('why, existing, row', [
    ('anderer-text', dict(document='AD-1', amount=-90, text='Anderer Text'),
     wiso_row(1000, 1200, 90.0, 'AD-1', text='Posten AD-1')),
    ('anderes-gegenkonto', dict(document='AD-1', amount=-90, counter=1810),
     wiso_row(1000, 1200, 90.0, 'AD-1', text='Posten AD-1')),
    ('keine-beleg-nr-gleich', dict(document='AD-2', amount=-90),
     wiso_row(1000, 1200, 90.0, 'AD-1', text='Posten AD-1')),
    ('leerer-text', dict(document='AD-1', amount=-90, text=''),
     wiso_row(1000, 1200, 90.0, 'AD-1', text='')),
    ('andere-form', dict(document='AD-1', amount=-90, form=FORM_MIRROR_COUNTER),
     wiso_row(1000, 1200, 90.0, 'AD-1', text='Posten AD-1')),
])
def test_adoption_of_the_opposite_sign_needs_everything_to_match(db, tmp_path, why, existing, row):
    """Im Zweifel neu anlegen und melden: Eine einzige Abweichung, und die Zeile wird nicht übernommen."""
    text = existing.get('text', f"Posten {existing['document']}")
    twin = db.insert_booking('2026-01-02', existing['amount'], coa_id=coa(db, 1600),
                             counter_coa_id=coa(db, existing.get('counter', 1800)),
                             document_number=existing['document'], text=text, booking_type='entry',
                             entry_form=existing.get('form', FORM_MIRROR_LEGACY))
    before = query(db, 'SELECT * FROM Bookings WHERE ID = ?', (twin,))

    result = _import_fdb(db, _fdb(tmp_path, [row]))

    assert (result['adopted'], result['imported']) == (0, 1), why
    assert query(db, 'SELECT * FROM Bookings WHERE ID = ?', (twin,)) == before


def test_adoption_of_the_opposite_sign_is_refused_when_the_file_carries_the_counterpart(db, tmp_path):
    """Die Datenbank enthält +90 und −90 (Buchung und Storno). Der Bestand führt −90: Das ist die
    Zeile der −90-Buchung, nicht der Zwilling der +90-Buchung."""
    twin = _legacy_mirror(db, 'AD-1', 1600, 1800, -90)
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'AD-1', text='Posten AD-1'),
                           wiso_row(1000, 1200, -90.0, 'AD-1', text='Posten AD-1',
                                    ACCOUNTINGID=799)])

    result = _import_fdb(db, path)

    assert (result['adopted'], result['imported']) == (1, 1)
    assert query(db, 'SELECT Amount, EntryForm, SourceGroup FROM Bookings WHERE ID = ?', (twin,)) == [
        (minor('-90.00'), FORM_MIRROR_LEGACY, '799')]          # die Zeile gehört zur −90-Buchung


def test_adoption_that_fills_a_missing_counter_account_takes_the_form_of_the_import(db, tmp_path):
    """Die CSV-Zeile hatte kein Gegenkonto (6850 / –: Zweckkonto vorne). Die Übernahme ergänzt es
    (4400, kein Geldkonto): Die Konten des Satzes ändern sich, also gilt die Form des Imports."""
    entry = db.insert_booking('2026-01-02', Decimal('59.50'), coa_id=coa(db, 6850),
                              document_number='AD-4', text='Posten AD-4', booking_type='entry')
    assert query(db, 'SELECT EntryForm FROM Bookings WHERE ID = ?', (entry,)) == [(FORM_PURPOSE_FIRST,)]
    path = _fdb(tmp_path, [wiso_row(4980, 8400, 59.5, 'AD-4', text='Posten AD-4')])

    result = _import_fdb(db, path)

    assert (result['adopted'], result['imported']) == (1, 0)
    assert query(db, 'SELECT COA_ID, CounterCOA_ID, EntryForm FROM Bookings WHERE ID = ?', (entry,)) == [
        (coa(db, 6850), coa(db, 4400), FORM_TRANSFER)]
    assert result['form_conflicts_count'] == 0


def test_a_row_adopted_without_a_form_takes_the_form_of_the_import(db, tmp_path):
    db.import_wiso_csv(csv_export(('6850', '1800', '59,50', 'AD-3', KEY19)))
    execute(db, 'UPDATE Bookings SET EntryForm = NULL')

    _import_fdb(db, _fdb(tmp_path, [wiso_row(4980, 1200, 59.5, 'AD-3', text='Posten AD-3')]))

    assert _forms(db)['AD-3'] == (minor('-59.50'), FORM_PURPOSE_FIRST)


# ── Zeilen auf Konten 1000–1099: die Übernahme CSV → Datenbank ─────────────────

FDB_STOCK_CHART = [(1040, 1050, 'Vorrat Bürobedarf')]
FDB_STOCK_ROWS = {
    'counter': lambda group=701: wiso_row(4980, 1040, 10.0, 'LS-1', text='Posten LS-1',
                                          ACCOUNTINGID=group),                      # 6850 an 1050
    'account': lambda group=702: wiso_row(1040, 1200, 30.0, 'LS-2', text='Posten LS-2',
                                          ACCOUNTING_DATE='2026-01-03 00:00:00',
                                          ACCOUNTINGID=group),                      # 1050 an 1800
}


def _fdb_stock(tmp_path, *rows):
    return _fdb(tmp_path, list(rows), extra_chart=FDB_STOCK_CHART)


def _stock_effects(db):
    """Was die Auswertungen aus dem Bestand lesen (die Quellgruppe, die die Übernahme setzt, gehört nicht
    dazu)."""
    totals = db.get_dashboard_totals(D1, D2)
    return datev_rows(db), euer(db), (totals['income'], totals['expense'])


@pytest.mark.parametrize('kind', ['counter', 'account'])
def test_adoption_takes_a_csv_row_an_older_version_stored_on_an_account_in_1000_1099(db, tmp_path, kind):
    """Die CSV-Zeile steht mit dem Betrag der alten Regel (−10 bzw. +30) im Bestand. Der Datenbank-Import
    würde den Satz heute mit einem anderen Betrag schreiben und fände sie nicht: Dubletten im Export.
    Stattdessen übernimmt er sie – Betrag und Form bleiben, nur die Quellgruppe kommt dazu."""
    old = _stored_by_2_0_x(db, kind, expense=6850)
    before_effects = _stock_effects(db)
    before = query(db, 'SELECT COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, Text, EntryForm, '
                       'DocumentNumber, DateBooking FROM Bookings WHERE ID = ?', (old,))

    result = _import_fdb(db, _fdb_stock(tmp_path, FDB_STOCK_ROWS[kind]()))

    assert (result['imported'], result['adopted'], result['legacy_stock_rows']) == (0, 1, 1)
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(1,)]
    assert query(db, 'SELECT COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, Text, EntryForm, '
                     'DocumentNumber, DateBooking FROM Bookings WHERE ID = ?', (old,)) == before
    assert query(db, 'SELECT SourceGroup FROM Bookings WHERE ID = ?', (old,)) == [
        ('701' if kind == 'counter' else '702',)]
    assert _stock_effects(db) == before_effects
    assert result['stock_range_accounts'][0]['nummer'] == 1050


@pytest.mark.parametrize('kind', ['counter', 'account'])
def test_a_reimport_after_such_an_adoption_creates_nothing(db, tmp_path, kind):
    _stored_by_2_0_x(db, kind, expense=6850)
    path = _fdb_stock(tmp_path, FDB_STOCK_ROWS[kind]())
    _import_fdb(db, path)
    before = query(db, 'SELECT * FROM Bookings ORDER BY ID')

    again = _import_fdb(db, path)

    assert (again['imported'], again['skipped']) == (0, 1)
    assert query(db, 'SELECT * FROM Bookings ORDER BY ID') == before


def test_the_database_import_on_a_fresh_database_writes_the_stock_rows_as_usual(db, tmp_path):
    result = _import_fdb(db, _fdb_stock(tmp_path, FDB_STOCK_ROWS['counter'](),
                                        FDB_STOCK_ROWS['account']()))

    assert (result['imported'], result['adopted'], result['legacy_stock_rows']) == (2, 0, 0)
    assert _forms(db) == {'LS-1': (minor('10.00'), FORM_TRANSFER),
                          'LS-2': (minor('-30.00'), FORM_PURPOSE_FIRST)}


def test_the_database_import_creates_only_the_satz_the_older_version_never_had(db, tmp_path):
    """Zwei echte gleiche Sätze in der Datenbank, eine Altzeile im Bestand: Die Altzeile gehört EINEM
    Satz, der andere ist neu."""
    old = _stored_by_2_0_x(db, 'counter', expense=6850)

    result = _import_fdb(db, _fdb_stock(tmp_path, FDB_STOCK_ROWS['counter'](701),
                                        FDB_STOCK_ROWS['counter'](709)))

    assert (result['imported'], result['adopted'], result['legacy_stock_rows']) == (1, 1, 1)
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings WHERE ID = ?', (old,)) == [
        (minor('-10.00'), FORM_PURPOSE_FIRST)]
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings WHERE ID != ?', (old,)) == [
        (minor('10.00'), FORM_TRANSFER)]


@pytest.mark.parametrize('why, existing_changes', [
    ('anderer-text', dict(text='Anderer Text')),
    ('leerer-text', dict(text='')),
    ('andere-beleg-nr', dict(document='LS-9')),
    ('anderes-gegenkonto', dict(counter=1810)),
    ('anderer-betrag', dict(amount='-12.00')),
    ('anderes-datum', dict(date='2026-01-09')),
])
def test_the_database_import_adopts_an_older_row_only_when_the_protection_rules_hold(
        db, tmp_path, why, existing_changes):
    """Dieselben Schutzregeln wie sonst: Text gleich und nicht leer, Beleg-Nr., Datum, Konto, Gegenkonto
    (und der Betrag der alten Regel). Im Zweifel wird neu angelegt."""
    db.insert_chart_of_accounts(4, 1050, 'Vorrat Bürobedarf', 'Test')
    row = dict(date='2026-01-02', document='LS-1', counter=1050, amount='-10.00', text='Posten LS-1')
    row.update(existing_changes)
    old = db.insert_booking(row['date'], Decimal(row['amount']), coa_id=coa(db, 6850),
                            counter_coa_id=coa(db, row['counter']), document_number=row['document'],
                            text=row['text'], booking_type='entry', entry_form=FORM_PURPOSE_FIRST)
    before = query(db, 'SELECT * FROM Bookings WHERE ID = ?', (old,))

    result = _import_fdb(db, _fdb_stock(tmp_path, FDB_STOCK_ROWS['counter']()))

    assert (result['adopted'], result['imported'], result['legacy_stock_rows']) == (0, 1, 0), why
    assert query(db, 'SELECT * FROM Bookings WHERE ID = ?', (old,)) == before


def test_the_adoption_of_an_older_row_fills_nothing(db, tmp_path):
    """Hat die Altzeile kein Gegenkonto (es fehlte im Kontenrahmen), bleibt sie so: Ein neues Konto
    verlangte eine Form, die zum Betrag der alten Regel passt – die Übernahme setzt nur die Quellgruppe."""
    old = _stored_by_2_0_x(db, 'counter', expense=6850, counter_missing=True)
    before = query(db, 'SELECT COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, EntryForm FROM Bookings '
                       'WHERE ID = ?', (old,))

    result = _import_fdb(db, _fdb_stock(tmp_path, FDB_STOCK_ROWS['counter']()))

    assert (result['adopted'], result['imported'], result['legacy_stock_rows']) == (1, 0, 1)
    assert query(db, 'SELECT COA_ID, CounterCOA_ID, Amount, TaxRate, TaxAmount, EntryForm FROM Bookings '
                     'WHERE ID = ?', (old,)) == before


def test_the_database_import_leaves_a_row_alone_that_another_satz_claims_with_its_own_amount(db, tmp_path):
    """Der Datenbank enthält auch „6850 an 1200“ (Abgang −10): Mit dem Betrag der Altzeile gehört sie diesem
    Satz, und der mit dem Konto 1040 bekommt sie nicht."""
    old = _stored_by_2_0_x(db, 'counter', expense=6850)

    result = _import_fdb(db, _fdb_stock(tmp_path, FDB_STOCK_ROWS['counter'](),
                                        wiso_row(4980, 1200, 10.0, 'LS-1', text='Posten LS-1',
                                                 ACCOUNTINGID=705)))

    assert result['legacy_stock_rows'] == 0
    assert query(db, 'SELECT Amount FROM Bookings WHERE ID = ?', (old,)) == [(minor('-10.00'),)]


# ── die Seite „Verschiedenes“ ────────────────────────────────────────────────

NOTICE_STOCK = ('Konten 1000&ndash;1099 sind im SKR04 Vorräte; ältere Versionen haben sie als Kasse '
                'gelesen &ndash; bitte prüfen, ob hier eine Kasse gemeint ist. Buchungen gegen diese '
                'Konten werden als Umbuchung gelesen und erscheinen nicht in der EÜR.')
NOTICE_LEGACY = ('ältere Überträge sind noch nach der alten Lesart gespeichert; ihre Form und ihr '
                 'Betrag wurden nicht verändert.')
NOTICE_STOCK_ROWS = ('Buchungen auf Konten 1000&ndash;1099 stammen aus einer älteren Version und sind nach '
                     'deren Lesart gespeichert; ihre Form und ihr Betrag wurden nicht verändert, und sie '
                     'wurden nicht erneut angelegt.')


def _store(name, result):
    with open(os.path.join(userctx.user_data_dir(), name), 'w', encoding='utf-8') as handle:
        json.dump(result, handle)


@pytest.mark.parametrize('name', ['wiso_import_result.json', 'wiso_fdb_result.json'])
def test_page_names_negative_gross_and_the_old_cash_range_for_both_imports(db, name):
    _store(name, {'negative_gross': 3, 'stock_range_accounts': [
        {'nummer': 1050, 'bezeichnung': '<b>Vorrat</b>', 'anzahl': 4}]})

    html = PageMiscellaneous(db)

    assert 'Negative Bruttobeträge &ndash; 3 Sätze' in html
    assert 'Stornos und Erstattungen stehen dort mit vertauschten Konten' in html
    assert 'als normale Buchung gelesen werden' not in html
    assert NOTICE_STOCK in html
    assert '<td>1050</td><td>&lt;b&gt;Vorrat&lt;/b&gt;</td><td>4</td>' in html     # maskiert
    assert '<b>Vorrat</b>' not in html


@pytest.mark.parametrize('name', ['wiso_import_result.json', 'wiso_fdb_result.json'])
def test_page_shows_no_notice_without_a_reason(db, name):
    _store(name, {'negative_gross': 0, 'stock_range_accounts': [], 'legacy_mirrors': 0,
                  'legacy_stock_rows': 0, 'form_conflicts': [], 'imported': 1})
    html = PageMiscellaneous(db)
    assert 'Negative Bruttobeträge' not in html and NOTICE_STOCK not in html
    assert NOTICE_LEGACY not in html and NOTICE_STOCK_ROWS not in html


@pytest.mark.parametrize('name', ['wiso_import_result.json', 'wiso_fdb_result.json'])
def test_page_survives_a_hand_edited_result_file(db, name):
    _store(name, {'negative_gross': 'viel', 'stock_range_accounts': 'x', 'legacy_mirrors': [],
                  'form_conflicts': {'a': 1}})
    _store(name, {'negative_gross': True, 'stock_range_accounts': [1, None, 'x'],
                  'legacy_mirrors': -3, 'legacy_stock_rows': 'viele', 'form_conflicts': [1, 'x', None]})
    html = PageMiscellaneous(db)
    assert 'Negative Bruttobeträge' not in html and NOTICE_LEGACY not in html
    assert NOTICE_STOCK_ROWS not in html


@pytest.mark.parametrize('name', ['wiso_import_result.json', 'wiso_fdb_result.json'])
def test_page_names_the_stock_rows_an_older_version_stored(db, name):
    _store(name, {'legacy_stock_rows': 3})
    html = PageMiscellaneous(db)
    assert f'3 {NOTICE_STOCK_ROWS}' in html

    _store(name, {'legacy_stock_rows': 0})
    assert NOTICE_STOCK_ROWS not in PageMiscellaneous(db)


@pytest.mark.parametrize('name', ['wiso_import_result.json', 'wiso_fdb_result.json'])
def test_page_names_the_older_transfers_that_stay_in_the_old_reading(db, name):
    _store(name, {'legacy_mirrors': 2})

    html = PageMiscellaneous(db)

    assert f'2 {NOTICE_LEGACY}' in html


def test_page_lists_the_conflicts_of_the_database_import(db):
    _store('wiso_fdb_result.json', {
        'legacy_mirrors': 0, 'form_conflicts_count': 1,
        'form_conflicts': [{'id': 7, 'date': '2026-01-02', 'amount': 30.0, 'text': '<i>Text</i>',
                            'stored': 'transfer', 'fresh': 'purpose_first'}]})

    html = PageMiscellaneous(db)

    assert 'Abweichende Form (1)' in html
    assert '<td>transfer</td><td>purpose_first</td>' in html
    assert '&lt;i&gt;Text&lt;/i&gt;' in html and '<i>Text</i>' not in html
    assert NOTICE_LEGACY not in html
    assert 'sonst wurde nichts geändert' not in html
