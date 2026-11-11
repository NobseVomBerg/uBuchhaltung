# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""EÜR, DATEV-Export, Dashboard und Split-Summe lesen die gespeicherte Form (``Bookings.EntryForm``).

Die Form steht beim Schreiben fest. Was ein Leser aus ihr macht:

* EÜR: Zweckkonto aus der Form (Geldkonto vorne → Gegenkonto, Zweckkonto vorne → Konto, Umbuchung →
  beide Seiten, Spiegel → nichts);
* DATEV: Soll/Haben folgt der Seite, die bei positivem Betrag im Soll steht;
* Dashboard: Ein Spiegel mit bekannter Lesart zählt aus Sicht der Kasse;
* Split-Summe: Ein Spiegel wirkt nach seiner Position auf dem Konto der Bankbewegung.

Dazu die Gegenprobe: Ändert sich die Liste der Geldkonten NACH dem Schreiben, lesen die Auswertungen
eine Zeile mit Form unverändert – nur eine Zeile ohne Form (Roh-SQL) folgt der Liste von heute.
Die Fälle sind die der Vorher/Nachher-Tabelle von T79 (neuer Import in eine frische Datenbank).
Alle Namen und Werte sind erfunden.
"""
from decimal import Decimal

import pytest

from db.matching import (
    FORM_LIQUID_FIRST, FORM_MIRROR_COA, FORM_MIRROR_COUNTER, FORM_MIRROR_LEGACY,
    FORM_PURPOSE_FIRST, FORM_TRANSFER,
)
from server.handlers import handle_add_transaction
from tests.entry_form_helpers import (
    D1, D2, bank_account, cash_account_id, coa, csv_export, datev_rows, euer, execute, form_of,
    minor, numbers, query,
)

KEY19 = '401'                    # BU-Schlüssel „19 %“ des Exports


@pytest.fixture
def db(tmp_db):
    bank_account(tmp_db, 'Form-Bank-Eins', 1800)
    return tmp_db


def _bank_id(db):
    return next(a[0] for a in db.fetch_accounts() if a[1] == 'Form-Bank-Eins')


def _by_ref(db):
    """{Belegnummer: (Betrag in Minor Units, Form)} der importierten Sätze."""
    return {row[0]: (row[1], row[2]) for row in query(
        db, "SELECT DocumentNumber, Amount, EntryForm FROM Bookings WHERE BookingType = 'entry' "
            "ORDER BY ID")}


# ── Vorher/Nachher (Tabelle 4): neuer Import in eine frische Datenbank, Bank 1800 ────────────────

def test_withdrawal_from_the_bank_into_the_cash_account_is_a_debit_on_the_cash_account(db):
    """1600 an 1800, 100: Betrag +100, ``mirror_coa``, DATEV **S** 1600 (früher H), Kasse +100."""
    db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'AB-1', '')))

    assert _by_ref(db)['AB-1'] == (minor('100.00'), FORM_MIRROR_COA)
    assert datev_rows(db)['AB-1'] == ('100,00', 'S', '1600', '1800', '0')
    totals = db.get_dashboard_totals(D1, D2)
    assert (totals['income'], totals['expense']) == (100.0, 0)


def test_deposit_from_the_cash_account_onto_the_bank_is_a_debit_on_the_bank(db):
    """1800 an 1600, 100: Betrag +100, ``mirror_coa``, DATEV **S** 1800 (früher H), Kasse **−100**
    (früher +100 als Einnahme)."""
    db.import_wiso_csv(csv_export(('1800', '1600', '100,00', 'EZ-1', '')))

    assert _by_ref(db)['EZ-1'] == (minor('100.00'), FORM_MIRROR_COA)
    assert datev_rows(db)['EZ-1'] == ('100,00', 'S', '1800', '1600', '0')
    totals = db.get_dashboard_totals(D1, D2)
    assert (totals['income'], totals['expense']) == (0, -100.0)


def test_dashboard_months_follow_the_cash_perspective_too(db):
    db.import_wiso_csv(csv_export(('1600', '1800', '100,00', 'AB-1', ''),
                                  ('1800', '1600', '40,00', 'EZ-1', '')))

    month = next(m for m in db.get_dashboard_monthly(D1, D2) if m['year_month'] == '2026-01')

    assert (month['income'], month['expense']) == (100.0, -40.0)


def test_a_mirror_in_the_clearing_account_balances_against_its_bank_movement(db):
    """1460 an 1800, 100 mit der Bankbewegung −100: Der Spiegel steht mit dem Gegenkonto auf dem
    Konto der Bank – sein Beitrag ist −100 und die Aufteilung geht auf (``mirror_coa``). Als
    ``mirror_legacy`` (Altbestand) zählt der Rohbetrag +100: Rest −200, wie in 2.0.x."""
    db.import_wiso_csv(csv_export(('1460', '1800', '100,00', 'VK-1', '')))
    bank = db.insert_booking('2026-01-02', Decimal('-100.00'), account_id=_bank_id(db),
                             booking_type='bank')
    assert db.link_bank_to_entries()['linked'] == 1
    assert datev_rows(db)['VK-1'] == ('100,00', 'S', '1460', '1800', '0')
    assert db.find_unbalanced_splits(D1, D2) == []

    execute(db, "UPDATE Bookings SET EntryForm = ? WHERE DocumentNumber = 'VK-1'",
            (FORM_MIRROR_LEGACY,))

    (unbalanced,) = db.find_unbalanced_splits(D1, D2)
    assert (unbalanced[0], unbalanced[3]) == (bank, Decimal('-200'))
    assert datev_rows(db)['VK-1'][1] == 'H'                   # die Lesart von 2.0.x


def test_the_mask_withdrawal_stays_as_it_was(db):
    """Abhebung über die Maske: Kind 1600/1800, −100, ``mirror_counter``, DATEV S 1600."""
    post = {'transaction_id': ['0'], 'date': ['2026-01-02'], 'amount': ['-100.00'],
            'currency': ['EUR'], 'account': [str(_bank_id(db))], 'coa_id': [str(coa(db, 1600))],
            'document_nr': ['MA-1']}
    assert handle_add_transaction(db, post)[0] == 303

    assert _by_ref(db)['MA-1'] == (minor('-100.00'), FORM_MIRROR_COUNTER)
    assert datev_rows(db)['MA-1'] == ('100,00', 'S', '1600', '1800', '0')
    assert db.find_unbalanced_splits(D1, D2) == []


def test_an_expense_paid_from_the_cash_account_is_the_same_in_every_version(db):
    """6815 an 1600 mit der Kasse 1600: −18,35, ``purpose_first``, DATEV S 6815, BU 9."""
    db.import_wiso_csv(csv_export(('6815', '1600', '18,35', 'KA-1', KEY19)))

    assert _by_ref(db)['KA-1'] == (minor('-18.35'), FORM_PURPOSE_FIRST)
    assert datev_rows(db)['KA-1'] == ('18,35', 'S', '6815', '1600', '9')
    assert db.get_dashboard_totals(D1, D2)['expense'] == -18.35
    assert euer(db)[6815] == Decimal('-15.42')


def test_the_same_expense_with_a_cash_account_without_a_number_is_a_transfer(db):
    """Kasse ohne SKR-Nummer: 1600 ist kein Geldkonto, ``transfer`` – BU-Schlüssel und EÜR sind
    eigene Punkte (``docs/aufgaben.md``) und hier nur festgehalten."""
    cash = cash_account_id(db)
    db.update_account(cash, 'Kasse', '', '', '', '', skr_account=None)

    db.import_wiso_csv(csv_export(('6815', '1600', '18,35', 'KA-2', KEY19)))

    assert _by_ref(db)['KA-2'] == (minor('18.35'), FORM_TRANSFER)
    assert datev_rows(db)['KA-2'] == ('18,35', 'S', '6815', '1600', '3')
    assert euer(db) == {}


def test_an_account_created_after_the_import_does_not_turn_the_marker_around(db):
    """6815 an 1800 wird importiert, bevor es das Bankkonto 1800 gibt: ``transfer``, +119, S 6815.
    Wird das Konto später angelegt (und mit der Bankbewegung verknüpft), bleibt es bei **S 6815** –
    früher kippte das Kennzeichen auf H, weil 1800 plötzlich ein Geldkonto war."""
    execute(db, 'DELETE FROM Accounts WHERE ID = ?', (_bank_id(db),))
    assert coa(db, 1800) not in db.get_bank_coa_ids()

    db.import_wiso_csv(csv_export(('6815', '1800', '119,00', 'SP-1', KEY19)))
    assert _by_ref(db)['SP-1'] == (minor('119.00'), FORM_TRANSFER)
    assert datev_rows(db)['SP-1'][1] == 'S'

    new_bank = bank_account(db, 'Form-Bank-Eins', 1800)
    assert coa(db, 1800) in db.get_payment_coa_ids()
    db.insert_booking('2026-01-02', Decimal('-119.00'), account_id=new_bank, booking_type='bank')
    db.link_bank_to_entries()

    assert datev_rows(db)['SP-1'][:4] == ('119,00', 'S', '6815', '1800')


def test_a_negative_gross_stays_as_it_was_and_is_counted(db):
    """Storno 6815 an 1800, brutto −119: Der Import nimmt das Vorzeichen der Geldbewegung (−119, wie
    bisher) und meldet die Zeile; ein Storno wird so wie eine normale Buchung gelesen."""
    result = db.import_wiso_csv(csv_export(('6815', '1800', '-119,00', 'ST-1', KEY19),
                                           ('6815', '1800', '119,00', 'AU-1', KEY19)))

    assert result['negative_gross'] == 1
    assert _by_ref(db)['ST-1'] == (minor('-119.00'), FORM_PURPOSE_FIRST)
    assert _by_ref(db)['AU-1'] == (minor('-119.00'), FORM_PURPOSE_FIRST)


# ── die Matrix aus der Analyse: Kasse mit 1600 (S1), ohne Nummer (S3), mit dem Verrechnungskonto (S4) ──
# (Konto, Gegenkonto, Brutto, Beleg) → je Kasse (Betrag, Form, Kennzeichen am Feld „Konto“, BU).
# Das Kennzeichen ist in jeder Zeile das richtige: Das Feld „Konto“ steht bei der Ausgabe (Zweckkonto
# vorne) im Soll, beim Übertrag im Soll des Kontos, das das Geld bekommt.
MATRIX = [
    (('6815', '1800', '119,00', 'R1'), {
        1600: ('-119.00', FORM_PURPOSE_FIRST, 'S', '9'), None: ('-119.00', FORM_PURPOSE_FIRST, 'S', '9'),
        1460: ('-119.00', FORM_PURPOSE_FIRST, 'S', '9')}),
    (('1600', '1800', '100,00', 'R2'), {                          # Abhebung
        1600: ('100.00', FORM_MIRROR_COA, 'S', '0'), None: ('-100.00', FORM_PURPOSE_FIRST, 'S', '0'),
        1460: ('-100.00', FORM_PURPOSE_FIRST, 'S', '0')}),
    (('1800', '1600', '100,00', 'R3'), {                          # Bareinzahlung
        1600: ('100.00', FORM_MIRROR_COA, 'S', '0'), None: ('100.00', FORM_LIQUID_FIRST, 'S', '0'),
        1460: ('100.00', FORM_LIQUID_FIRST, 'S', '0')}),
    (('1460', '1800', '100,00', 'R4'), {                          # Verrechnungskonto an Bank
        1600: ('100.00', FORM_MIRROR_COA, 'S', '0'), None: ('100.00', FORM_MIRROR_COA, 'S', '0'),
        1460: ('100.00', FORM_MIRROR_COA, 'S', '0')}),
    (('1800', '1810', '50,00', 'R5'), {                           # Bank an Bank
        1600: ('50.00', FORM_MIRROR_COA, 'S', '0'), None: ('50.00', FORM_MIRROR_COA, 'S', '0'),
        1460: ('50.00', FORM_MIRROR_COA, 'S', '0')}),
    (('6815', '1600', '18,35', 'R6'), {                           # Ausgabe aus der Kasse
        1600: ('-18.35', FORM_PURPOSE_FIRST, 'S', '9'), None: ('18.35', FORM_TRANSFER, 'S', '3'),
        1460: ('18.35', FORM_TRANSFER, 'S', '3')}),
    (('6815', '1800', '-119,00', 'R7'), {                         # Storno: Vorzeichen der Geldbewegung
        1600: ('-119.00', FORM_PURPOSE_FIRST, 'S', '9'), None: ('-119.00', FORM_PURPOSE_FIRST, 'S', '9'),
        1460: ('-119.00', FORM_PURPOSE_FIRST, 'S', '9')}),
]


@pytest.mark.parametrize('cash', [1600, None, 1460], ids=['s1-kasse-1600', 's3-kasse-ohne-skr',
                                                           's4-kasse-1460'])
def test_import_matrix_gives_the_right_marker_in_every_cash_variant(tmp_path, cash):
    from db import Database
    db = Database(db_name=str(tmp_path / 'matrix.db'))
    bank_account(db, 'Matrix-Bank', 1800)
    bank_account(db, 'Matrix-Bank-Zwei', 1810)
    if cash != 1600:
        db.update_account(cash_account_id(db), 'Kasse', '', '', '', '', skr_account=cash)
    db.import_wiso_csv(csv_export(*[(konto, gegen, brutto, ref, KEY19 if konto == '6815' else '')
                                    for (konto, gegen, brutto, ref), _expected in MATRIX]))

    stored = _by_ref(db)
    rows = datev_rows(db)
    for (konto, gegen, _brutto, ref), expected in MATRIX:
        amount, form, marker, bu = expected[cash]
        assert stored[ref] == (minor(amount), form), (konto, gegen, cash)
        assert rows[ref][1:] == (marker, konto, gegen, bu), (konto, gegen, cash)


# ── die Form steht fest, die Kontenliste ändert sie nicht ────────────────────

def _child_payment(db):
    bank = db.insert_booking('2026-01-02', Decimal('-119.00'), account_id=_bank_id(db),
                             booking_type='bank')
    child = db.insert_booking('2026-01-02', Decimal('-119.00'), coa_id=coa(db, 6815),
                              counter_coa_id=coa(db, 1800), tax_rate=0.19,
                              tax_amount=Decimal('-19.00'), document_number='ZB-1',
                              booking_type='entry', parent_booking_id=bank)
    return bank, child


def test_changing_the_number_of_a_bank_account_does_not_change_how_a_booked_row_reads(db):
    """Zweckkonto vorne, Bank hinten: EÜR und DATEV lesen die Ausgabe. Hängt man das Bankkonto
    später auf eine andere Nummer (am Schutz der Kontenverwaltung vorbei), ist 1800 kein Geldkonto
    mehr – die Zeile bleibt eine Zahlung."""
    _bank, child = _child_payment(db)
    before = (euer(db), datev_rows(db)['ZB-1'])
    assert before[0] == {6815: Decimal('-100.00'), 1406: Decimal('-19.00')}
    assert before[1] == ('119,00', 'S', '6815', '1800', '9')

    db.update_account(_bank_id(db), 'Form-Bank-Eins', 'Owner', 'DE0001', 'BIC', 'Bank',
                      skr_account=1900)
    assert coa(db, 1800) not in db.get_payment_coa_ids()

    assert (euer(db), datev_rows(db)['ZB-1']) == before
    assert form_of(db, child) == FORM_PURPOSE_FIRST


@pytest.mark.parametrize('skr_after', [None, 1460, 1610])
def test_the_cash_account_gaining_or_losing_its_number_does_not_change_a_booked_row(db, skr_after):
    """Zweckkonto vorne, Kasse 1600 hinten, Kind einer Girobewegung: Verliert die Kasse später ihre
    Nummer (oder bekommt eine andere), bleibt die Ausgabe in EÜR und DATEV eine Ausgabe – die Form
    wurde geschrieben, als 1600 noch Geldkonto war."""
    bank = db.insert_booking('2026-01-02', Decimal('-119.00'), account_id=_bank_id(db),
                             booking_type='bank')
    db.insert_booking('2026-01-02', Decimal('-119.00'), coa_id=coa(db, 6815),
                      counter_coa_id=coa(db, 1600), tax_rate=0.19, tax_amount=Decimal('-19.00'),
                      document_number='KA-9', booking_type='entry', parent_booking_id=bank)
    before = (euer(db), datev_rows(db)['KA-9'])
    assert before[0] == {6815: Decimal('-100.00'), 1406: Decimal('-19.00')}
    assert before[1] == ('119,00', 'S', '6815', '1600', '9')

    db.update_account(cash_account_id(db), 'Kasse', '', '', '', '', skr_account=skr_after)

    assert (euer(db), datev_rows(db)['KA-9']) == before


def test_a_row_without_a_form_follows_the_list_of_today(db):
    """Dieselbe Zeile ohne Form (Roh-SQL): Mit dem alten Bankkonto eine Ausgabe; ohne es eine
    Umbuchung mit umgekehrtem Vorzeichen – so war es vor der Form."""
    _bank, child = _child_payment(db)
    execute(db, 'UPDATE Bookings SET EntryForm = NULL WHERE ID = ?', (child,))
    assert euer(db)[6815] == Decimal('-100.00')

    db.update_account(_bank_id(db), 'Form-Bank-Eins', 'Owner', 'DE0001', 'BIC', 'Bank',
                      skr_account=1900)

    assert datev_rows(db)['ZB-1'][1] == 'H'
    assert euer(db)[6815] == Decimal('100.00')


def test_a_stored_transfer_stays_a_transfer_when_a_counter_account_becomes_a_money_account(db):
    """6815 an 1050 wurde als Umbuchung geschrieben (1050 ist kein Geldkonto). Wird später ein
    Konto mit dieser Nummer eröffnet, ändert sich die Zeile nicht."""
    execute(db, "INSERT INTO ChartOfAccounts (ID, Framework, AccountNumber, Name, Description) "
                "VALUES (401050, 4, 1050, 'Vorräte', '')")
    db.import_wiso_csv(csv_export(('6815', '1050', '10,00', 'VR-1', '')))
    assert _by_ref(db)['VR-1'] == (minor('10.00'), FORM_TRANSFER)
    before = datev_rows(db)['VR-1']

    bank_account(db, 'Form-Bank-Drei', 1050)

    assert datev_rows(db)['VR-1'] == before
    assert form_of(db, query(db, "SELECT ID FROM Bookings WHERE DocumentNumber = 'VR-1'")[0][0]) \
        == FORM_TRANSFER


# ── Dashboard: nur die Spiegel mit bekannter Lesart zählen aus Sicht der Kasse ─

@pytest.mark.parametrize('form, coa_nr, counter_nr, amount, income, expense', [
    # mirror_coa: Betrag auf dem Konto (positiv = Konto im Soll)
    (FORM_MIRROR_COA, 1600, 1800, '100.00', 100.0, 0),            # Kasse im Soll: +100
    (FORM_MIRROR_COA, 1800, 1600, '100.00', 0, -100.0),           # Kasse im Haben: −100
    (FORM_MIRROR_COA, 1600, 1800, '-100.00', 0, -100.0),
    # mirror_counter: Betrag auf dem Gegenkonto (so schreibt ihn die Buchungsmaske)
    (FORM_MIRROR_COUNTER, 1800, 1600, '100.00', 100.0, 0),
    (FORM_MIRROR_COUNTER, 1600, 1800, '100.00', 0, -100.0),
    # Altbestand und alles andere: der Betrag, wie er ist
    (FORM_MIRROR_LEGACY, 1600, 1800, '100.00', 100.0, 0),
    (FORM_MIRROR_LEGACY, 1800, 1600, '100.00', 100.0, 0),
    (None, 1800, 1600, '100.00', 100.0, 0),
])
def test_a_standalone_mirror_counts_from_the_view_of_the_cash_account(
        db, form, coa_nr, counter_nr, amount, income, expense):
    booking = db.insert_booking('2026-01-05', Decimal(amount), coa_id=coa(db, coa_nr),
                                counter_coa_id=coa(db, counter_nr), booking_type='entry',
                                entry_form=form or FORM_MIRROR_LEGACY)
    if form is None:
        execute(db, 'UPDATE Bookings SET EntryForm = NULL WHERE ID = ?', (booking,))

    totals = db.get_dashboard_totals(D1, D2)

    assert (totals['income'], totals['expense']) == (income, expense)


def test_a_mirror_that_does_not_touch_the_cash_account_keeps_its_amount_in_the_dashboard(db):
    """Nur über die Kasse als Konto gefunden (``Account_ID``), ohne ihr Konto im Satz: Der Betrag
    zählt, wie er ist."""
    db.insert_booking('2026-01-05', Decimal('100.00'), coa_id=coa(db, 1460),
                      counter_coa_id=coa(db, 1800), account_id=cash_account_id(db),
                      booking_type='entry', entry_form=FORM_MIRROR_COA)
    totals = db.get_dashboard_totals(D1, D2)
    assert (totals['income'], totals['expense']) == (100.0, 0)
