# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die WISO-Importe und die Form des Buchungssatzes (``Bookings.EntryForm``).

Beide Importe schreiben die Form mit, wie sie das Vorzeichen gesetzt haben: Konto liquide →
``liquid_first``, Gegenkonto liquide → ``purpose_first``, beide → Spiegel mit dem Rohbetrag
(``mirror_coa``), keine → ``transfer``. Dazu:

* ``negative_gross`` und ``stock_range_accounts`` im Ergebnis (und auf der Seite *Verschiedenes*):
  ein Storno wird nicht vorzeichenrichtig gelesen, und Konten 1000–1099 sind in SKR04 Vorräte;
* der Weg „erst CSV, dann Datenbank“: Ein übernommener Spiegel des Bestands (``mirror_legacy``)
  bekommt seine Form, auch einer mit umgekehrtem Vorzeichen (``mirror_counter``);
* der Re-Import: Ein Spiegel des Bestands bekommt aus dem Rohbetrag seine Form
  (``forms_stamped``), eine abweichende andere Form wird nur gemeldet (``form_conflicts``);
* der Re-Import des Textexports erkennt einen vorzeichenverkehrten Spiegel des Bestands und legt
  keinen zweiten an.

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
    bank_account, coa, csv_export, execute, minor, query, wiso_database, wiso_row,
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
        ('6815', '', '5,00', 'GL', ''),                  # Gegenkonto leer: Zweckkonto vorne
        ('', '4400', '5,00', 'VL', ''),                  # Konto leer: Geldkonto vorne (leer)
    ))

    assert result['imported'] == 7 and not result['errors'], result
    assert _forms(db) == {
        'LF': (minor('119.00'), FORM_LIQUID_FIRST), 'PF': (minor('-59.50'), FORM_PURPOSE_FIRST),
        'MI': (minor('100.00'), FORM_MIRROR_COA), 'TR': (minor('119.00'), FORM_TRANSFER),
        'T2': (minor('10.00'), FORM_TRANSFER), 'GL': (minor('5.00'), FORM_PURPOSE_FIRST),
        'VL': (minor('5.00'), FORM_LIQUID_FIRST)}


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

    assert (again['imported'], again['skipped'], again['forms_stamped']) == (0, 2, 0)
    assert _forms(db) == before


# ── Textexport: negative Bruttobeträge und Konten 1000–1099 ─────────────────

def test_csv_counts_negative_gross_rows_with_exactly_one_liquid_side(db):
    result = db.import_wiso_csv(csv_export(
        ('6815', '1800', '-119,00', 'N1', KEY19),        # Storno einer Ausgabe: gezählt
        ('1800', '4400', '-50,00', 'N2', KEY19),         # Erstattung einer Einnahme: gezählt
        ('6815', '1800', '119,00', 'P1', KEY19),         # positiv: nicht
        ('4405', '4400', '-119,00', 'N3', KEY19),        # keine liquide Seite: Vorzeichen bleibt
        ('1600', '1800', '-100,00', 'N4', ''),           # Spiegel: Rohbetrag bleibt
    ))

    assert result['negative_gross'] == 2
    forms = _forms(db)
    assert forms['N1'] == (minor('-119.00'), FORM_PURPOSE_FIRST)      # das Vorzeichen der Geldbewegung
    assert forms['N2'] == (minor('50.00'), FORM_LIQUID_FIRST)
    assert forms['N3'] == (minor('-119.00'), FORM_TRANSFER)
    assert forms['N4'] == (minor('-100.00'), FORM_MIRROR_COA)


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


def test_csv_reimport_recognises_a_legacy_mirror_with_the_opposite_sign(db):
    """Die Datei sagt 1600 an 1800, +100 (Konto im Soll); der Bestand führt den Spiegel mit −100
    (Richtung auf dem Gegenkonto). Das ist dieselbe Buchung: Der Import legt keine zweite an, und die
    vorhandene Zeile bekommt die Form, die zu ihrem Betrag passt."""
    twin = _legacy_mirror(db, 'TW-1', 1600, 1800, -100)

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['forms_stamped']) == (0, 1, 1)
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(1,)]
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings WHERE ID = ?', (twin,)) == [
        (minor('-100.00'), FORM_MIRROR_COUNTER)]


def test_csv_reimport_does_not_double_a_legacy_mirror_with_the_same_sign(db):
    """Der Normalfall nach der Migration: Die Zeile steht mit dem Betrag der Datei im Bestand
    (``mirror_legacy``). Sie ist ein gewöhnliches Duplikat – nichts wird angelegt, nichts gestempelt
    (ob ihr Betrag auf dem Konto oder dem Gegenkonto steht, weiß die Datei hier nicht besser)."""
    twin = _legacy_mirror(db, 'TW-1', 1600, 1800, 100)

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['forms_stamped']) == (0, 1, 0)
    assert query(db, 'SELECT COUNT(*), MAX(EntryForm) FROM Bookings') == [(1, FORM_MIRROR_LEGACY)]
    assert query(db, 'SELECT ID FROM Bookings') == [(twin,)]


def test_csv_reimport_writes_the_stamp_into_the_sql_log(db):
    twin = _legacy_mirror(db, 'TW-1', 1600, 1800, -100)
    before = len(_sql_log(db))

    db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', '')))

    assert _sql_log(db)[before:] == [f"UPDATE Bookings SET EntryForm = 'mirror_counter' WHERE ID = {twin};"]


def test_csv_reimport_uses_each_twin_once(db):
    _legacy_mirror(db, 'TW-1', 1600, 1800, -100)

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', ''),
                                           ('1600', '1800', '100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['forms_stamped']) == (1, 1, 1)


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

    assert result['forms_stamped'] == 0, why
    assert result['imported'] == 1, why
    assert query(db, 'SELECT Amount, EntryForm FROM Bookings WHERE ID = ?', (existing,)) == before


def test_csv_reimport_does_not_take_a_twin_that_the_file_claims_itself(db):
    """Die Datei enthält die Buchung und ihr Gegenstück (+100 und −100). Der Bestand führt −100: Das ist
    das Duplikat der −100-Zeile, nicht der Zwilling der +100-Zeile."""
    _legacy_mirror(db, 'TW-1', 1600, 1800, -100)

    result = db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'TW-1', ''),
                                           ('1600', '1800', '-100,00', 'TW-1', '')))

    assert (result['imported'], result['skipped'], result['forms_stamped']) == (1, 1, 0)
    assert query(db, 'SELECT EntryForm FROM Bookings ORDER BY ID') == [
        (FORM_MIRROR_LEGACY,), (FORM_MIRROR_COA,)]


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


def test_fdb_counts_negative_gross_rows_with_exactly_one_liquid_side(db, tmp_path):
    rows = FDB_ROWS + [wiso_row(1000, 1200, -5.0, 'FD-SP'),          # Spiegel: nicht gezählt
                       wiso_row(8400, 4980, -6.0, 'FD-NT')]           # keine liquide Seite: nicht gezählt
    assert _import_fdb(db, _fdb(tmp_path, rows))['negative_gross'] == 1


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
    assert (result['forms_stamped'], result['form_conflicts'], result['form_conflicts_count']) == \
        (0, [], 0)


# ── WISO-Datenbank: Re-Import schreibt die Form fest ─────────────────────────

def _as_legacy(db, *documents):
    """Die Sätze, als hätte sie 2.0.x geschrieben und die Migration ``mirror_legacy`` daraus gemacht."""
    for document in documents:
        execute(db, 'UPDATE Bookings SET EntryForm = ? WHERE DocumentNumber = ?',
                (FORM_MIRROR_LEGACY, document))


def test_fdb_reimport_stamps_the_legacy_mirrors_from_the_raw_amount(db, tmp_path):
    path = _fdb(tmp_path, FDB_ROWS)
    _import_fdb(db, path)
    _as_legacy(db, 'FD-AB', 'FD-EZ', 'FD-VK')
    before = query(db, 'SELECT COUNT(*) FROM Bookings')

    again = _import_fdb(db, path)

    assert (again['imported'], again['skipped'], again['forms_stamped']) == (0, len(FDB_ROWS), 3)
    assert again['form_conflicts_count'] == 0
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == before
    forms = _forms(db)
    assert [forms[doc][1] for doc in ('FD-AB', 'FD-EZ', 'FD-VK')] == [FORM_MIRROR_COA] * 3
    assert forms['FD-AB'][0] == minor('90.00')                      # der Betrag bleibt

    third = _import_fdb(db, path)
    assert third['forms_stamped'] == 0                              # nichts mehr festzuschreiben


def test_fdb_reimport_stamps_a_mirror_with_the_opposite_sign_as_mirror_counter(db, tmp_path):
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'FD-AB')])
    _import_fdb(db, path)
    _as_legacy(db, 'FD-AB')
    execute(db, "UPDATE Bookings SET Amount = ? WHERE DocumentNumber = 'FD-AB'", (minor('-90.00'),))

    again = _import_fdb(db, path)

    assert again['forms_stamped'] == 1
    assert _forms(db)['FD-AB'] == (minor('-90.00'), FORM_MIRROR_COUNTER)


def test_fdb_reimport_writes_each_stamp_into_the_sql_log(db, tmp_path):
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'FD-AB')])
    _import_fdb(db, path)
    _as_legacy(db, 'FD-AB')
    before = len(_sql_log(db))

    _import_fdb(db, path)

    new_lines = _sql_log(db)[before:]
    assert [line for line in new_lines if line.startswith('UPDATE Bookings SET EntryForm')] == [
        "UPDATE Bookings SET EntryForm = 'mirror_coa' WHERE ID = %d;" % query(
            db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'FD-AB'")[0][0]]


def test_fdb_reimport_reports_a_different_form_and_changes_nothing(db, tmp_path):
    """Gespeichert ist „Umbuchung“, der frische Import würde „Zweckkonto vorne“ schreiben (das Konto
    war damals kein Geldkonto). Das ist ein Befund, keine Korrektur."""
    path = _fdb(tmp_path, [wiso_row(4980, 1200, 30.0, 'FD-BA')])
    _import_fdb(db, path)
    execute(db, "UPDATE Bookings SET EntryForm = ?, Amount = ? WHERE DocumentNumber = 'FD-BA'",
            (FORM_TRANSFER, minor('30.00')))

    again = _import_fdb(db, path)

    assert (again['imported'], again['forms_stamped'], again['form_conflicts_count']) == (0, 0, 1)
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

    assert (again['forms_stamped'], again['form_conflicts_count']) == (0, 1)
    assert _forms(db)['FD-OLD'][1] == FORM_MIRROR_LEGACY


def test_fdb_reimport_does_not_report_two_mirrors_that_disagree_only_in_their_convention(db, tmp_path):
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'FD-AB')])
    _import_fdb(db, path)
    execute(db, "UPDATE Bookings SET EntryForm = ? WHERE DocumentNumber = 'FD-AB'",
            (FORM_MIRROR_COUNTER,))

    again = _import_fdb(db, path)

    assert (again['forms_stamped'], again['form_conflicts_count']) == (0, 0)
    assert _forms(db)['FD-AB'][1] == FORM_MIRROR_COUNTER


# ── WISO-Datenbank nach dem Textexport: Übernahme ─────────────────────────────

def test_adoption_gives_a_legacy_mirror_its_form(db, tmp_path):
    db.import_wiso_csv(csv_export(('1600', '1800', '90,00', 'AD-1', ''),
                                  ('1800', '4400', '119,00', 'AD-2', KEY19),
                                  ('6850', '1800', '59,50', 'AD-3', KEY19)))
    _as_legacy(db, 'AD-1')
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'AD-1', text='Posten AD-1'),
                           wiso_row(1200, 8400, 119.0, 'AD-2', text='Posten AD-2'),
                           wiso_row(4980, 1200, 59.5, 'AD-3', text='Posten AD-3')])

    result = _import_fdb(db, path)

    assert (result['imported'], result['adopted'], result['forms_stamped']) == (0, 3, 1)
    assert query(db, 'SELECT COUNT(*) FROM Bookings') == [(3,)]
    assert [form for _amount, form in _forms(db).values()] == [
        FORM_MIRROR_COA, FORM_LIQUID_FIRST, FORM_PURPOSE_FIRST]


def test_adoption_takes_a_legacy_mirror_with_the_opposite_sign_and_marks_it_mirror_counter(db, tmp_path):
    """E4: Konto, Gegenkonto, Beleg-Nr. und Text gleich, nur das Vorzeichen abweicht – übernommen
    wird die vorhandene Zeile (Betrag bleibt), sie wird ``mirror_counter``."""
    twin = _legacy_mirror(db, 'AD-1', 1600, 1800, -90)
    path = _fdb(tmp_path, [wiso_row(1000, 1200, 90.0, 'AD-1', text='Posten AD-1')])

    result = _import_fdb(db, path)

    assert (result['imported'], result['adopted'], result['forms_stamped']) == (0, 1, 1)
    assert query(db, 'SELECT Amount, EntryForm, SourceGroup FROM Bookings WHERE ID = ?', (twin,)) == [
        (minor('-90.00'), FORM_MIRROR_COUNTER, '701')]
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
        (minor('-90.00'), FORM_MIRROR_COA, '799')]             # die Zeile gehört zur −90-Buchung


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


# ── die Seite „Verschiedenes“ ────────────────────────────────────────────────

NOTICE_STOCK = ('Konten 1000&ndash;1099 sind im SKR04 Vorräte; ältere Versionen haben sie als Kasse '
                'gelesen &ndash; bitte prüfen, ob hier eine Kasse gemeint ist.')


def _store(name, result):
    with open(os.path.join(userctx.user_data_dir(), name), 'w', encoding='utf-8') as handle:
        json.dump(result, handle)


@pytest.mark.parametrize('name', ['wiso_import_result.json', 'wiso_fdb_result.json'])
def test_page_names_negative_gross_and_the_old_cash_range_for_both_imports(db, name):
    _store(name, {'negative_gross': 3, 'stock_range_accounts': [
        {'nummer': 1050, 'bezeichnung': '<b>Vorrat</b>', 'anzahl': 4}]})

    html = PageMiscellaneous(db)

    assert 'Negative Bruttobeträge &ndash; 3 Sätze' in html
    assert 'Stornos und Gutschriften' in html and 'als normale Buchung gelesen werden' in html
    assert NOTICE_STOCK in html
    assert '<td>1050</td><td>&lt;b&gt;Vorrat&lt;/b&gt;</td><td>4</td>' in html     # maskiert
    assert '<b>Vorrat</b>' not in html


@pytest.mark.parametrize('name', ['wiso_import_result.json', 'wiso_fdb_result.json'])
def test_page_shows_no_notice_without_a_reason(db, name):
    _store(name, {'negative_gross': 0, 'stock_range_accounts': [], 'forms_stamped': 0,
                  'form_conflicts': [], 'imported': 1})
    html = PageMiscellaneous(db)
    assert 'Negative Bruttobeträge' not in html and NOTICE_STOCK not in html


@pytest.mark.parametrize('name', ['wiso_import_result.json', 'wiso_fdb_result.json'])
def test_page_survives_a_hand_edited_result_file(db, name):
    _store(name, {'negative_gross': 'viel', 'stock_range_accounts': 'x', 'forms_stamped': [],
                  'form_conflicts': {'a': 1}})
    _store(name, {'negative_gross': True, 'stock_range_accounts': [1, None, 'x'],
                  'forms_stamped': -3, 'form_conflicts': [1, 'x', None]})
    assert 'Negative Bruttobeträge' not in PageMiscellaneous(db)


def test_page_lists_the_conflicts_and_the_stamped_mirrors_of_the_database_import(db):
    _store('wiso_fdb_result.json', {
        'forms_stamped': 2, 'form_conflicts_count': 1,
        'form_conflicts': [{'id': 7, 'date': '2026-01-02', 'amount': 30.0, 'text': '<i>Text</i>',
                            'stored': 'transfer', 'fresh': 'purpose_first'}]})

    html = PageMiscellaneous(db)

    assert '2 Spiegelbuchungen aus einem früheren Import haben ihre Form bekommen' in html
    assert 'Abweichende Form (1)' in html
    assert '<td>transfer</td><td>purpose_first</td>' in html
    assert '&lt;i&gt;Text&lt;/i&gt;' in html and '<i>Text</i>' not in html
