# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Steuerbetrag dreht mit dem Betrag, wenn die Form eines Satzes auf die andere Seite kippt.

Ändern sich Konto oder Gegenkonto eines Satzes so, dass sich der Betrag auf die andere Seite bezieht
(``db.matching.reform``), wird der Betrag negiert – Soll/Haben bleibt. Der Steuerbetrag trägt das Vorzeichen des
Betrags (die EÜR rechnet Netto = Betrag − Steuer) und dreht deshalb mit, sofern ihn der Aufrufer nicht ändert; ein
geändertes (auch geleertes) Feld gilt wie eingegeben, ein Satz ohne Steuer bleibt ohne. Das gilt für jeden Weg, der den
Betrag so negiert: ``update_booking_fields`` (Maske, Editor), ``update_booking``, ``backfill_booking_fields`` und den
Tabellen-Export des WISO-Imports – an EINER Stelle (``Database._form_after_accounts_change``).

Alle Namen und Werte sind erfunden.
"""
import re
from decimal import Decimal

import pytest

from db import Database
from db.matching import (
    FORM_LIQUID_FIRST, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_PURPOSE_FIRST, FORM_TRANSFER,
)
from tests.entry_form_helpers import (
    bank_account, coa, csv_export, datev_rows, euer, minor, query,
)
from tests.entry_form_roundtrip import open_form, submit

BELEG = 'ST-1'


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Steuer-Bank', 1800)
    return tmp_db


def _bank(db, amount='-119.00'):
    giro = next(a[0] for a in db.fetch_accounts() if a[1] == 'Steuer-Bank')
    return db.insert_booking('2026-01-02', Decimal(amount), account_id=giro, booking_type='bank')


def _mirror_under_a_bank_movement(db, key='401'):
    """CSV „1460;1800;119,00“ (Steuerschlüssel 401 = 19 %): ein Spiegel ``mirror_coa`` mit +119,00 und Steuer
    +19,00, vom Abgleich an eine Bankbewegung über −119,00 gehängt."""
    db.import_wiso_csv(csv_export(('1460', '1800', '119,00', BELEG, key)))
    (entry,) = [row[0] for row in query(db, 'SELECT ID FROM Bookings WHERE DocumentNumber = ?', (BELEG,))]
    bank = _bank(db)
    assert db.link_bank_to_entries()['linked'] == 1
    return entry, bank


def _stored(db, booking_id):
    return query(db, 'SELECT Amount, TaxAmount, EntryForm FROM Bookings WHERE ID = ?', (booking_id,))[0]


def _mask(db, entry, **changes):
    """Die Buchungsmaske des Satzes, wie der Browser sie abschickt, mit geänderten Feldern."""
    post, _options = open_form(db, entry)
    for name, value in changes.items():
        post[name] = [str(value)]
    submit(db, post)


def _editor(db, bank, entry, account_number):
    """Die Zeile des Satzes im Buchungssatz-Editor auf ein anderes Zweckkonto stellen und speichern."""
    post, _options = open_form(db, bank)
    post['split_coa'][post['split_id'].index(str(entry))] = str(coa(db, account_number))
    submit(db, post)


def _start(db):
    entry, bank = _mirror_under_a_bank_movement(db)
    assert _stored(db, entry) == (minor('119.00'), minor('19.00'), FORM_MIRROR_COA)
    assert euer(db) == {}                                  # ein Übertrag zwischen Geldkonten
    return entry, bank


EXPECTED_AFTER = {6815: Decimal('-100.00'), 1406: Decimal('-19.00')}


# ── die Reproduktion: Konto auf 6815 ──────────────────────────────────────────

def test_the_mask_turns_the_tax_amount_with_the_amount(db):
    entry, _bank_id = _start(db)

    _mask(db, entry, coa_id=coa(db, 6815))

    assert _stored(db, entry) == (minor('-119.00'), minor('-19.00'), FORM_PURPOSE_FIRST)
    assert euer(db) == EXPECTED_AFTER                      # nicht 6815 −138,00 und 1406 +19,00
    assert datev_rows(db)[BELEG] == ('119,00', 'S', '6815', '1800', '401')


def test_the_editor_turns_the_tax_amount_with_the_amount(db):
    entry, bank = _start(db)

    _editor(db, bank, entry, 6815)

    assert _stored(db, entry) == (minor('-119.00'), minor('-19.00'), FORM_PURPOSE_FIRST)
    assert euer(db) == EXPECTED_AFTER
    assert datev_rows(db)[BELEG] == ('119,00', 'S', '6815', '1800', '401')


def test_the_way_back_keeps_amount_and_tax_amount_in_step(db):
    """Zurück auf 1460: wieder ein Spiegel. Er bezieht seinen Betrag jetzt auf das Gegenkonto
    (``mirror_counter``, die Konvention der Maske) – dieselbe Buchung wie das ``mirror_coa`` +119,00 davor, mit
    −119,00; die Steuer geht mit dem Betrag, bleibt also −19,00. Die EÜR zählt den Übertrag wieder nicht."""
    entry, _bank_id = _start(db)
    _mask(db, entry, coa_id=coa(db, 6815))

    _mask(db, entry, coa_id=coa(db, 1460))

    assert _stored(db, entry) == (minor('-119.00'), minor('-19.00'), FORM_MIRROR_COUNTER)
    assert euer(db) == {}


def test_a_second_flip_turns_amount_and_tax_amount_again(db):
    """Auch ein zweites Kippen (Zahlung → Umbuchung, hier über das Gegenkonto) dreht beide."""
    entry, _bank_id = _start(db)
    _mask(db, entry, coa_id=coa(db, 6815))                  # Spiegel → Zahlung
    assert _stored(db, entry)[:2] == (minor('-119.00'), minor('-19.00'))

    db.update_booking_fields(entry, counter_coa_id=coa(db, 4400))           # Zahlung → Umbuchung „6815 an 4400“

    assert _stored(db, entry) == (minor('119.00'), minor('19.00'), FORM_TRANSFER)


def test_a_tax_amount_the_user_changed_counts_as_entered(db):
    """Der Nutzer ändert den Steuerbetrag beim Kippen mit (hier: Vorsteuer 10,00 statt 19,00)."""
    entry, _bank_id = _start(db)

    _mask(db, entry, coa_id=coa(db, 6815), tax_amount='-10.00')

    assert _stored(db, entry) == (minor('-119.00'), minor('-10.00'), FORM_PURPOSE_FIRST)
    assert euer(db) == {6815: Decimal('-109.00'), 1406: Decimal('-10.00')}


def test_a_tax_amount_the_user_cleared_stays_cleared(db):
    entry, _bank_id = _start(db)

    _mask(db, entry, coa_id=coa(db, 6815), tax_amount='')

    assert _stored(db, entry) == (minor('-119.00'), None, FORM_PURPOSE_FIRST)
    assert euer(db) == {6815: Decimal('-119.00')}


def test_an_entry_without_tax_stays_without(db):
    entry, _bank_id = _mirror_under_a_bank_movement(db, key='')
    assert _stored(db, entry)[1] is None

    _mask(db, entry, coa_id=coa(db, 6815))

    assert _stored(db, entry) == (minor('-119.00'), None, FORM_PURPOSE_FIRST)
    assert euer(db) == {6815: Decimal('-119.00')}


def test_a_changed_amount_is_not_negated_and_the_tax_amount_is_as_entered(db):
    entry, _bank_id = _start(db)

    _mask(db, entry, coa_id=coa(db, 6815), amount='-59.50', tax_amount='-9.50')

    assert _stored(db, entry) == (minor('-59.50'), minor('-9.50'), FORM_PURPOSE_FIRST)


def test_a_save_without_a_change_of_accounts_leaves_the_tax_amount(db):
    entry, _bank_id = _start(db)

    _mask(db, entry)

    assert _stored(db, entry) == (minor('119.00'), minor('19.00'), FORM_MIRROR_COA)


# ── die übrigen Aufrufer des Kippens ───────────────────────────────────────────

def test_update_booking_turns_the_tax_amount_with_the_amount(db):
    entry, _bank_id = _start(db)

    db.update_booking(entry, '2026-01-02', Decimal('119.00'), coa_id=coa(db, 6815),
                      counter_coa_id=coa(db, 1800), tax_rate=0.19, tax_amount=Decimal('19.00'),
                      document_number=BELEG, text='Posten')

    assert _stored(db, entry) == (minor('-119.00'), minor('-19.00'), FORM_PURPOSE_FIRST)


def test_update_booking_takes_a_changed_tax_amount_as_entered(db):
    entry, _bank_id = _start(db)

    db.update_booking(entry, '2026-01-02', Decimal('119.00'), coa_id=coa(db, 6815),
                      counter_coa_id=coa(db, 1800), tax_rate=0.19, tax_amount=Decimal('-10.00'),
                      document_number=BELEG, text='Posten')

    assert _stored(db, entry) == (minor('-119.00'), minor('-10.00'), FORM_PURPOSE_FIRST)


def test_update_booking_fields_turns_the_tax_amount_when_the_caller_gives_none(db):
    entry, _bank_id = _start(db)

    db.update_booking_fields(entry, coa_id=coa(db, 6815))

    assert _stored(db, entry) == (minor('-119.00'), minor('-19.00'), FORM_PURPOSE_FIRST)


def _entry_without_accounts(db, tax=True):
    """Ein Satz ohne Konten, −10,00 (mit Steuer −1,60): Zweckkonto/Gegenkonto leer, also ``purpose_first``."""
    return db.insert_booking('2026-01-02', Decimal('-10.00'), booking_type='entry', document_number='TB-2',
                             tax_rate=0.19 if tax else None,
                             tax_amount=Decimal('-1.60') if tax else None)


def test_backfill_turns_the_stored_tax_amount_with_the_amount(db):
    """Bekommt ein Satz sein Konto erst nachträglich (hier ein Geldkonto: aus „Zweckkonto vorne“ wird „Geldkonto
    vorne“), dreht der Betrag – und mit ihm der gespeicherte Steuerbetrag."""
    entry = _entry_without_accounts(db)
    assert _stored(db, entry) == (minor('-10.00'), minor('-1.60'), FORM_PURPOSE_FIRST)

    db.backfill_booking_fields(entry, coa_id=coa(db, 1600))

    assert _stored(db, entry) == (minor('10.00'), minor('1.60'), FORM_LIQUID_FIRST)


def test_backfill_fills_a_missing_tax_amount_as_given(db):
    entry = _entry_without_accounts(db, tax=False)

    db.backfill_booking_fields(entry, coa_id=coa(db, 1600), tax_amount=Decimal('1.60'))

    assert _stored(db, entry) == (minor('10.00'), minor('1.60'), FORM_LIQUID_FIRST)


def test_backfill_does_not_touch_the_tax_amount_when_the_amount_stays(db):
    entry = _entry_without_accounts(db)

    db.backfill_booking_fields(entry, coa_id=coa(db, 6815))             # Zweckkonto: keine Seite kippt

    assert _stored(db, entry) == (minor('-10.00'), minor('-1.60'), FORM_PURPOSE_FIRST)


TABLE_HEADER = 'Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr.;Betrag\n'


def test_the_table_export_turns_the_tax_amount_when_it_fills_the_account(db):
    """Der Tabellen-Export des WISO-Imports trägt das Konto aus der Kategorie nach (hier die Kasse): Die Form kippt,
    der Betrag dreht – und der Steuerbetrag mit."""
    entry = _entry_without_accounts(db)

    result = db.import_wiso_csv((TABLE_HEADER + '02.01.2026;Empfänger;Zweck;Kasse;TB-2;-10,00\n')
                                .encode('cp1252'))

    assert result['updated'] == 1 and not result['errors'], result
    assert query(db, 'SELECT COA_ID FROM Bookings WHERE ID = ?', (entry,)) == [(coa(db, 1600),)]
    assert _stored(db, entry) == (minor('10.00'), minor('1.60'), FORM_LIQUID_FIRST)


# ── das Protokoll ──────────────────────────────────────────────────────────────

def test_the_replay_log_carries_the_tax_amount_with_the_amount(db, monkeypatch):
    """Die ganze Änderung steht als eine Anweisung im Protokoll – Konto, Form, negierter Betrag UND Steuerbetrag."""
    entry, _bank_id = _start(db)
    logged = []
    monkeypatch.setattr(Database, '_log_sql', lambda self, sql, params, text: logged.append((sql, params)))

    _mask(db, entry, coa_id=coa(db, 6815))

    (sql, params), = [(sql, params) for sql, params in logged if 'Amount=?' in sql and params[-1] == entry]
    values = dict(zip(re.findall(r'(\w+)=\?', sql.split(' WHERE')[0]), params))
    assert (values['Amount'], values['TaxAmount'], values['EntryForm']) == (
        minor('-119.00'), minor('-19.00'), FORM_PURPOSE_FIRST)
