# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Form eines Buchungssatzes: Einordnung, EÜR und DATEV-Export lesen sie gleich.

Ein Buchungssatz hat ein Konto vorne (``COA_ID``) und ein Gegenkonto
(``CounterCOA_ID``). Bei einer Zahlung trägt der Betrag die Richtung der
Geldbewegung (Einnahme +), egal auf welcher Seite das Zweckkonto steht. Es gibt
drei Formen:

* Zweckkonto vorne (Erlös/Aufwand; Gegenkonto = Geldkonto oder leer),
* Geldkonto vorne (v. a. aus dem WISO-Import),
* Umbuchung (beide Seiten kontiert, keine liquide, z. B. 4405 → 4400).

``db.matching.classify_entry`` ordnet das an EINER Stelle ein – beim Schreiben; seit 2.1.0 steht
das Ergebnis als Form am Satz (``Bookings.EntryForm``, siehe test_entry_form_*.py), und die
Auswertungen lesen sie. Eine Zeile ohne Form (per Roh-SQL angelegt) wird weiter mit der Kontenliste
von heute eingeordnet. Geprüft werden die Wahrheitstabelle, die EÜR (``get_euer_data``: ein Satz ohne
liquide Gegenseite bleibt eine Zahlung) und das Soll/Haben-Kennzeichen des DATEV-Exports (hängt am
Feld „Konto“; der BU-Schlüssel folgt der Richtung der Geldbewegung).

Beträge werden exakt als Decimal bzw. in der DATEV-Schreibweise verglichen.
"""
import csv
from contextlib import closing
from decimal import Decimal
from itertools import product

import pytest

from db.matching import (
    ENTRY_EMPTY, ENTRY_LIQUID_FIRST, ENTRY_MIRROR, ENTRY_PURPOSE_FIRST, ENTRY_TRANSFER,
    classify_entry, is_bank_effective,
)
from export import datev
from server.handlers import handle_add_transaction, handle_datev_export


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _bank_account(db, name, skr):
    db.insert_account(name, 'Owner', 'DE00ENTRYFORM', 'BIC', 'Bank', is_cash=0, skr_account=skr)
    return next(a[0] for a in db.fetch_accounts() if a[1] == name)


def _coa(db, number):
    """COA-ID zur Kontonummer; ``None`` bleibt ``None`` (leere Seite des Satzes)."""
    return None if number is None else db.get_coa_id_by_account_number(number)


@pytest.fixture
def bank_db(tmp_db):
    """Frische Datenbank mit zwei eigenen Bankkonten (SKR 1800 und 1810)."""
    _bank_account(tmp_db, 'Form-Bank-Eins', 1800)
    _bank_account(tmp_db, 'Form-Bank-Zwei', 1810)
    return tmp_db


def _bank_movement(db, coa_nr, counter_nr, amount, rate=None, tax=None, date='2026-03-01'):
    """Bankbewegung auf 'Form-Bank-Eins' mit EINEM Buchungssatz (COA, Gegenkonto)."""
    acct = next(a[0] for a in db.fetch_accounts() if a[1] == 'Form-Bank-Eins')
    bank_id = db.insert_booking(date, Decimal(amount), account_id=acct, booking_type='bank')
    child_id = db.insert_booking(
        date, Decimal(amount), coa_id=_coa(db, coa_nr), counter_coa_id=_coa(db, counter_nr),
        tax_rate=rate, tax_amount=None if tax is None else Decimal(tax),
        booking_type='entry', parent_booking_id=bank_id)
    return bank_id, child_id


def _euer(db):
    """EÜR 2026 als {Kontonummer: Betrag}; ``get_euer_data`` liefert float, hier exakt als Decimal."""
    return {nr: Decimal(str(total))
            for nr, _name, total in db.get_euer_data('2026-01-01', '2026-12-31')}


def _decimals(expected):
    return {nr: Decimal(value) for nr, value in expected.items()}


# ── A. Einordnung: classify_entry ────────────────────────────────────────────

GELDKONTO, ZWECKKONTO = 10, 20
LIQUIDE = {GELDKONTO, 11}               # 11: ein zweites Geldkonto, das hier nirgends vorkommt
ROLLEN = {'leer': None, 'geld': GELDKONTO, 'zweck': ZWECKKONTO}

# alle 3 x 3 Kombinationen von (COA, Gegenkonto) ∈ {leer, Geldkonto, Zweckkonto}
FORM_JE_KOMBINATION = {
    ('leer', 'leer'): ENTRY_EMPTY,
    ('leer', 'geld'): ENTRY_EMPTY,
    ('leer', 'zweck'): ENTRY_LIQUID_FIRST,
    ('geld', 'leer'): ENTRY_LIQUID_FIRST,
    ('geld', 'geld'): ENTRY_MIRROR,
    ('geld', 'zweck'): ENTRY_LIQUID_FIRST,
    ('zweck', 'leer'): ENTRY_PURPOSE_FIRST,
    ('zweck', 'geld'): ENTRY_PURPOSE_FIRST,
    ('zweck', 'zweck'): ENTRY_TRANSFER,
}
KOMBINATIONEN = [pytest.param(coa, counter, id=f'{coa}-{counter}')
                 for coa, counter in FORM_JE_KOMBINATION]


def test_truth_table_covers_all_nine_combinations():
    assert set(FORM_JE_KOMBINATION) == set(product(ROLLEN, ROLLEN))
    assert len(FORM_JE_KOMBINATION) == 9


def test_entry_kinds_are_five_distinct_values():
    kinds = {ENTRY_EMPTY, ENTRY_LIQUID_FIRST, ENTRY_MIRROR, ENTRY_PURPOSE_FIRST, ENTRY_TRANSFER}
    assert len(kinds) == 5


@pytest.mark.parametrize('coa, counter', KOMBINATIONEN)
def test_classify_entry_truth_table(coa, counter):
    assert classify_entry(ROLLEN[coa], ROLLEN[counter], LIQUIDE) == FORM_JE_KOMBINATION[(coa, counter)]


@pytest.mark.parametrize('coa, counter', KOMBINATIONEN)
def test_only_transfer_is_not_bank_effective(coa, counter):
    """``is_bank_effective`` und ``classify_entry`` sehen denselben Satz gleich: Nicht
    bankwirksam ist genau die Umbuchung (beide Seiten kontiert, keine liquide)."""
    kind = classify_entry(ROLLEN[coa], ROLLEN[counter], LIQUIDE)
    assert is_bank_effective(ROLLEN[coa], ROLLEN[counter], LIQUIDE) == (kind != ENTRY_TRANSFER)


def test_transfer_is_the_only_form_that_is_not_bank_effective():
    not_effective = [pair for pair in FORM_JE_KOMBINATION
                     if not is_bank_effective(ROLLEN[pair[0]], ROLLEN[pair[1]], LIQUIDE)]
    assert not_effective == [('zweck', 'zweck')]


# ── B. EÜR ───────────────────────────────────────────────────────────────────
# Wie ein Satz kontiert ist, ändert die EÜR nicht: Das Zweckkonto ist die nicht-liquide
# Seite, der Betrag trägt die Richtung der Geldbewegung. T26: Mit leerem Gegenkonto lief
# der Satz früher in den Umbuchungs-Zweig (umgedrehtes Vorzeichen, keine USt/Vorsteuer).

# (Zweckkonto, Betrag, Satz, Steuer, erwartete EÜR-Zeilen: Kontonummer -> Betrag)
ZAHLUNGEN = [
    pytest.param(4400, '119.00', 0.19, '19.00', {4400: '100.00', 3806: '19.00'},
                 id='einnahme-19'),
    pytest.param(6815, '-59.50', 0.19, '-9.50', {6815: '-50.00', 1406: '-9.50'},
                 id='ausgabe-19'),
    pytest.param(4300, '107.00', 0.07, '7.00', {4300: '100.00', 3806: '7.00'},
                 id='einnahme-7'),
    pytest.param(6815, '-107.00', 0.07, '-7.00', {6815: '-100.00', 1401: '-7.00'},
                 id='ausgabe-7'),
]

# (COA, Gegenkonto) eines Satzes zum Zweckkonto z und dem Bankkonto b
FORMEN = {
    'zweckkonto-vorne': lambda z, b: (z, b),
    'geldkonto-vorne': lambda z, b: (b, z),
    'gegenkonto-leer': lambda z, b: (z, None),
    'vorne-leer': lambda z, b: (None, z),
}


@pytest.mark.parametrize('form', FORMEN)
@pytest.mark.parametrize('zweck, amount, rate, tax, expected', ZAHLUNGEN)
def test_euer_reads_a_payment_the_same_in_every_form(bank_db, form, zweck, amount, rate, tax,
                                                     expected):
    coa, counter = FORMEN[form](zweck, 1800)
    _bank_movement(bank_db, coa, counter, amount, rate, tax)
    assert _euer(bank_db) == _decimals(expected)


# Umbuchung und Privatanteil (beide Seiten kontiert, keine liquide) bleiben, wie sie waren:
# Die Werte sind am Stand vor T26 (Parent des Fix-Commits) gemessen und ändern sich mit der
# Korrektur nicht. Wo eine Seite 4405 (Wartekonto) ist, fehlt sie in der EÜR (neutral).
# (COA, Gegenkonto, Betrag, Satz, Steuer, erwartete EÜR-Zeilen)
UMBUCHUNGEN = [
    pytest.param(4405, 4400, '119.00', 0.19, '19.00', {4400: '100.00', 3806: '19.00'},
                 id='zahlungs-umbuchung-4405-4400'),
    pytest.param(4405, 4400, '1190.00', None, None, {4400: '1190.00'},
                 id='zahlungs-umbuchung-ohne-steuer'),
    pytest.param(4405, 4400, '-119.00', 0.19, '-19.00', {4400: '-100.00', 3806: '-19.00'},
                 id='zahlungs-umbuchung-storno'),
    pytest.param(6805, 2100, '-7.50', None, None, {6805: '7.50', 2100: '-7.50'},
                 id='privatanteil-6805-an-2100'),
    pytest.param(2100, 6805, '7.50', None, None, {6805: '7.50', 2100: '-7.50'},
                 id='privatanteil-2100-von-6805'),
]


@pytest.mark.parametrize('coa, counter, amount, rate, tax, expected', UMBUCHUNGEN)
def test_euer_transfer_rows_stay_as_before(bank_db, coa, counter, amount, rate, tax, expected):
    _bank_movement(bank_db, coa, counter, amount, rate, tax)
    assert _euer(bank_db) == _decimals(expected)


def test_euer_private_share_inside_a_receipt(bank_db):
    """Strom-Muster aus dem Beleg: zwei betriebliche Zahlungen und ein Privatanteil
    (2100 von 6805) unter EINER Bankbewegung."""
    acct = next(a[0] for a in bank_db.fetch_accounts() if a[1] == 'Form-Bank-Eins')
    bank_id = bank_db.insert_booking('2026-02-01', Decimal('-29.99'), account_id=acct,
                                     booking_type='bank')
    for amount in ('-15.00', '-14.99'):
        bank_db.insert_booking('2026-02-01', Decimal(amount), coa_id=_coa(bank_db, 6805),
                               counter_coa_id=_coa(bank_db, 1800), booking_type='entry',
                               parent_booking_id=bank_id)
    bank_db.insert_booking('2026-02-01', Decimal('7.50'), coa_id=_coa(bank_db, 2100),
                           counter_coa_id=_coa(bank_db, 6805), booking_type='entry',
                           parent_booking_id=bank_id)
    assert _euer(bank_db) == {6805: Decimal('-22.49'), 2100: Decimal('-7.50')}


@pytest.mark.parametrize('coa, counter', [
    pytest.param(1800, 1810, id='spiegel-bank-an-bank'),
    pytest.param(1800, 1800, id='spiegel-gleiche-bank'),
    pytest.param(None, None, id='leer'),
    pytest.param(None, 1800, id='vorne-leer-hinten-geldkonto'),
    pytest.param(1800, None, id='geldkonto-vorne-hinten-leer'),
])
def test_euer_ignores_rows_without_purpose_account(bank_db, coa, counter):
    _bank_movement(bank_db, coa, counter, '500.00')
    assert _euer(bank_db) == {}


def _mask_post(account_id, coa_id, amount, rate, tax, transaction_id=0):
    post = {'transaction_id': [str(transaction_id)], 'date': ['2026-03-01'],
            'amount': [amount], 'account': [str(account_id)], 'currency': ['EUR'],
            'coa_id': [str(coa_id)], 'tax_rate': [f'{rate * 100:g}'], 'tax_amount': [tax]}
    return post


@pytest.fixture
def bank_without_skr(tmp_db):
    """Ein Bankkonto ohne SKR-Nummer: seine Buchungssätze haben kein Gegenkonto."""
    acct = _bank_account(tmp_db, 'Ohne-SKR', None)
    assert tmp_db.get_account_by_id(acct)[7] is None          # SKRAccount
    # Das Bankkonto trägt nichts bei; es bleibt allein die Standard-Kasse mit 1600
    assert tmp_db.get_bank_coa_ids() == {_coa(tmp_db, 1600)}
    return acct


@pytest.mark.parametrize('zweck, amount, rate, tax, expected', ZAHLUNGEN)
def test_euer_bank_without_skr_saved_through_the_mask_new_booking(tmp_db, bank_without_skr,
                                                                  zweck, amount, rate, tax,
                                                                  expected):
    coa = _coa(tmp_db, zweck)
    status, _location = handle_add_transaction(
        tmp_db, _mask_post(bank_without_skr, coa, amount, rate, tax))
    assert status == 303
    bank = next(b for b in tmp_db.fetch_bookings_grouped('2026-01-01', '2026-12-31')
                if b['type'] == 'bank')['booking'][0]
    kids = tmp_db.get_child_bookings_for_bank(bank)
    assert [(k[1], k[2]) for k in kids] == [(coa, None)]      # Zweckkonto vorne, Gegenkonto leer
    assert _euer(tmp_db) == _decimals(expected)


@pytest.mark.parametrize('zweck, amount, rate, tax, expected', ZAHLUNGEN)
def test_euer_bank_without_skr_saved_through_the_mask_existing_booking(tmp_db, bank_without_skr,
                                                                       zweck, amount, rate, tax,
                                                                       expected):
    coa = _coa(tmp_db, zweck)
    bank = tmp_db.insert_booking('2026-03-01', Decimal(amount), account_id=bank_without_skr,
                                 booking_type='bank')
    status, _location = handle_add_transaction(
        tmp_db, _mask_post(bank_without_skr, coa, amount, rate, tax, transaction_id=bank))
    assert status == 303
    kids = tmp_db.get_child_bookings_for_bank(bank)
    assert [(k[1], k[2]) for k in kids] == [(coa, None)]
    assert _euer(tmp_db) == _decimals(expected)


def test_euer_receipt_without_bank_account_counts_as_payment_once_linked(bank_db):
    """Ein ohne Bankkonto erfasster Beleg (Zweckkonto vorne, Gegenkonto leer) ist in der EÜR
    erst da, wenn der Abgleich ihn mit der Bankbewegung verknüpft – dann als Zahlung."""
    coa = _coa(bank_db, 6815)
    receipt = bank_db.insert_booking('2026-03-05', Decimal('-59.50'), coa_id=coa, tax_rate=0.19,
                                     tax_amount=Decimal('-9.50'), text='Kopierpapier',
                                     document_number='ER-77', booking_type='entry')
    acct = next(a[0] for a in bank_db.fetch_accounts() if a[1] == 'Form-Bank-Eins')
    bank = bank_db.insert_booking('2026-03-05', Decimal('-59.50'), account_id=acct,
                                  text='Kopierpapier', booking_type='bank')
    assert _euer(bank_db) == {}

    assert bank_db.link_bank_to_entries()['linked'] == 1
    assert bank_db.get_booking_by_id(receipt)[18] == bank      # ParentBooking_ID
    assert bank_db.get_booking_by_id(receipt)[9] is None       # Gegenkonto bleibt leer
    assert _euer(bank_db) == {6815: Decimal('-50.00'), 1406: Decimal('-9.50')}


# ── C. DATEV: Soll/Haben folgt der Form, der BU-Schlüssel der Geldrichtung ───
# Das Kennzeichen bezieht sich auf das Feld „Konto“ (= COA):
#   Geldkonto vorne und Umbuchung: positiver Betrag = S, negativer = H
#   Zweckkonto vorne (auch mit leerem Gegenkonto), Spiegel, leer: positiv = H, negativ = S
# BU: nur Steuersatz und Vorzeichen des Betrags (Einnahme 19 % = 3, 7 % = 2;
#     Ausgabe 19 % = 9, 7 % = 8; sonst 0).
_SPALTE_SH, _SPALTE_KONTO, _SPALTE_GEGENKONTO, _SPALTE_BU, _SPALTE_BELEG = 1, 6, 7, 8, 10

# (id, COA, Gegenkonto, Betrag, Steuersatz, (Umsatz, S/H, Konto, Gegenkonto, BU))
DATEV_FAELLE = [
    ('zweckkonto-vorne-einnahme', 4400, 1800, '119.00', 0.19, ('119,00', 'H', '4400', '1800', '3')),
    ('zweckkonto-vorne-ausgabe', 6815, 1800, '-59.50', 0.19, ('59,50', 'S', '6815', '1800', '9')),
    ('geldkonto-vorne-einnahme', 1800, 4400, '119.00', 0.19, ('119,00', 'S', '1800', '4400', '3')),
    ('geldkonto-vorne-ausgabe', 1800, 6815, '-59.50', 0.19, ('59,50', 'H', '1800', '6815', '9')),
    ('gegenkonto-leer-einnahme', 4400, None, '119.00', 0.19, ('119,00', 'H', '4400', '', '3')),
    ('gegenkonto-leer-ausgabe', 6815, None, '-59.50', 0.19, ('59,50', 'S', '6815', '', '9')),
    ('vorne-leer-ausgabe', None, 6815, '-59.50', 0.19, ('59,50', 'H', '', '6815', '9')),
    ('umbuchung-positiv', 4405, 4400, '119.00', 0.19, ('119,00', 'S', '4405', '4400', '3')),
    ('umbuchung-negativ', 4405, 4400, '-119.00', 0.19, ('119,00', 'H', '4405', '4400', '9')),
    ('privatanteil', 2100, 6805, '7.50', None, ('7,50', 'S', '2100', '6805', '0')),
    ('privatanteil-negativ', 6805, 2100, '-7.50', None, ('7,50', 'H', '6805', '2100', '0')),
    ('spiegel-positiv', 1800, 1810, '500.00', None, ('500,00', 'H', '1800', '1810', '0')),
    ('spiegel-negativ', 1800, 1810, '-500.00', None, ('500,00', 'S', '1800', '1810', '0')),
    ('leer-positiv', None, None, '10.00', None, ('10,00', 'H', '', '', '0')),
    ('leer-negativ', None, None, '-10.00', None, ('10,00', 'S', '', '', '0')),
    ('zweckkonto-vorne-einnahme-7', 4300, 1800, '107.00', 0.07, ('107,00', 'H', '4300', '1800', '2')),
    ('zweckkonto-vorne-ausgabe-7', 6815, 1800, '-107.00', 0.07, ('107,00', 'S', '6815', '1800', '8')),
    ('geldkonto-vorne-einnahme-7', 1800, 4300, '107.00', 0.07, ('107,00', 'S', '1800', '4300', '2')),
    ('geldkonto-vorne-ausgabe-7', 1800, 6815, '-107.00', 0.07, ('107,00', 'H', '1800', '6815', '8')),
    ('ohne-steuer', 6815, 1800, '-10.00', 0.0, ('10,00', 'S', '6815', '1800', '0')),
    ('betrag-null-zweckkonto-vorne', 4400, 1800, '0.00', 0.19, ('0,00', 'H', '4400', '1800', '3')),
    ('betrag-null-geldkonto-vorne', 1800, 4400, '0.00', 0.19, ('0,00', 'S', '1800', '4400', '3')),
]
DATEV_PARAMS = [pytest.param(*fall[1:], id=fall[0]) for fall in DATEV_FAELLE]


def _book_entry(db, doc, coa_nr, counter_nr, amount, rate):
    """Eigenständiger Buchungssatz (ohne Bankbewegung); ``doc`` trägt die Zeile im Export."""
    return db.insert_booking('2026-03-10', Decimal(amount), coa_id=_coa(db, coa_nr),
                             counter_coa_id=_coa(db, counter_nr), tax_rate=rate,
                             document_number=doc, text=f'Fall {doc}', booking_type='entry')


def _datev_rows(csv_bytes):
    """{Belegfeld 1: alle Spalten} aus dem Buchungsstapel (Zeile 1 = EXTF-Kopf, 2 = Überschriften)."""
    lines = [ln for ln in csv_bytes.decode('cp1252').split('\r\n') if ln]
    rows = {}
    for line in lines[2:]:
        fields = next(csv.reader([line], delimiter=';', quotechar='"'))
        rows[fields[_SPALTE_BELEG]] = fields
    return rows


def _feld(fields):
    return (fields[0], fields[_SPALTE_SH], fields[_SPALTE_KONTO], fields[_SPALTE_GEGENKONTO],
            fields[_SPALTE_BU])


def _export(db, with_payment_ids=True):
    bookings = db.fetch_bookings_range('2026-03-01', '2026-03-31')
    csv_bytes, exported = datev.export_to_datev(
        bookings, db.get_coa_id_to_number_map(), '2026-03-01', '2026-03-31',
        liquid_coa_ids=db.get_payment_coa_ids() if with_payment_ids else None)
    assert exported == [b[0] for b in bookings]
    return _datev_rows(csv_bytes)


def _ohne_formen(db):
    """Die Zeilen wie per Roh-SQL angelegt: ohne Form. Nur dann ordnet der Export mit den Zahlungskonten
    ein, die er bekommt – ohne sie gilt allein das Vorzeichen."""
    with closing(db._get_connection()) as conn:
        conn.execute('UPDATE Bookings SET EntryForm = NULL')
        conn.commit()


def _alle_faelle_buchen(db):
    for number, (_id, coa, counter, amount, rate, _expected) in enumerate(DATEV_FAELLE, start=1):
        _book_entry(db, f'DV{number:02d}', coa, counter, amount, rate)


@pytest.mark.parametrize('coa, counter, amount, rate, expected', DATEV_PARAMS)
def test_datev_marker_follows_the_form_of_the_entry(bank_db, coa, counter, amount, rate, expected):
    _book_entry(bank_db, 'DV01', coa, counter, amount, rate)
    assert _feld(_export(bank_db)['DV01']) == expected


@pytest.mark.parametrize('coa, counter, amount, rate, expected', DATEV_PARAMS)
def test_datev_without_liquid_ids_keeps_the_sign_rule(bank_db, coa, counter, amount, rate,
                                                      expected):
    """Eine Zeile ohne Form und ohne ``liquid_coa_ids`` (None): Es gilt allein das Vorzeichen,
    positiv = H, negativ = S."""
    _book_entry(bank_db, 'DV01', coa, counter, amount, rate)
    _ohne_formen(bank_db)
    umsatz, sh, konto, gegenkonto, bu = _feld(_export(bank_db, with_payment_ids=False)['DV01'])
    assert sh == ('H' if Decimal(amount) >= 0 else 'S')
    assert (umsatz, konto, gegenkonto, bu) == (expected[0], *expected[2:])


def test_datev_empty_liquid_set_is_not_the_legacy_rule(bank_db):
    """Nur ``None`` wählt die reine Vorzeichen-Regel. Eine leere Menge heißt „es gibt keine
    Geldkonten“: „6815 / 1800, −59,50“ ist dann – ohne gespeicherte Form – eine Umbuchung (H),
    keine Ausgabe (S)."""
    _book_entry(bank_db, 'DV01', 6815, 1800, '-59.50', 0.19)
    _ohne_formen(bank_db)
    bookings = bank_db.fetch_bookings_range('2026-03-01', '2026-03-31')

    csv_bytes, _exported = datev.export_to_datev(
        bookings, bank_db.get_coa_id_to_number_map(), '2026-03-01', '2026-03-31',
        liquid_coa_ids=set())

    assert _feld(_datev_rows(csv_bytes)['DV01']) == ('59,50', 'H', '6815', '1800', '9')


def test_datev_tax_key_is_the_same_with_and_without_liquid_ids(bank_db):
    """Der BU-Schlüssel hängt nur an Steuersatz und Betragsvorzeichen: In jeder Zeile ändert
    sich allein das Soll/Haben-Kennzeichen (Spalte 1), und zwar genau bei den Formen, in
    denen das Kennzeichen der Vorzeichen-Regel widerspricht."""
    _alle_faelle_buchen(bank_db)
    _ohne_formen(bank_db)
    mit = _export(bank_db)
    ohne = _export(bank_db, with_payment_ids=False)

    assert mit.keys() == ohne.keys() and len(mit) == len(DATEV_FAELLE)
    geaendert = set()
    for doc, fields in mit.items():
        other = ohne[doc]
        assert fields[_SPALTE_BU] == other[_SPALTE_BU], doc
        assert [v for i, v in enumerate(fields) if i != _SPALTE_SH] == \
               [v for i, v in enumerate(other) if i != _SPALTE_SH], doc
        if fields[_SPALTE_SH] != other[_SPALTE_SH]:
            geaendert.add(doc)

    erwartet = {f'DV{number:02d}'
                for number, (_id, _coa, _counter, amount, _rate, expected)
                in enumerate(DATEV_FAELLE, start=1)
                if expected[1] != ('H' if Decimal(amount) >= 0 else 'S')}
    assert geaendert == erwartet
    # Geldkonto vorne (5, davon einmal mit Betrag 0), vorne leer (1), Umbuchung (2), Privatanteil (2)
    assert len(erwartet) == 10


# (Form, Betrag, erwartetes Kennzeichen am Feld „Konto“): Die gespeicherte Form entscheidet, welche Seite
# bei positivem Betrag im Soll steht – nicht die Liste der Geldkonten und nicht das Vorzeichen allein.
FORM_KENNZEICHEN = [
    ('purpose_first', '100.00', 'H'), ('purpose_first', '-100.00', 'S'),
    ('liquid_first', '100.00', 'S'), ('liquid_first', '-100.00', 'H'),
    ('transfer', '100.00', 'S'), ('transfer', '-100.00', 'H'),
    ('mirror_coa', '100.00', 'S'), ('mirror_coa', '-100.00', 'H'),
    ('mirror_counter', '100.00', 'H'), ('mirror_counter', '-100.00', 'S'),
    ('mirror_legacy', '100.00', 'H'), ('mirror_legacy', '-100.00', 'S'),
]


@pytest.mark.parametrize('form, amount, expected', FORM_KENNZEICHEN)
@pytest.mark.parametrize('with_payment_ids', [True, False])
def test_datev_marker_follows_the_stored_form_whatever_the_accounts_look_like(
        bank_db, form, amount, expected, with_payment_ids):
    """Dieselben Konten (6815 / 4400: für die Kontenliste eine Umbuchung), nur die Form wechselt."""
    bank_db.insert_booking('2026-03-10', Decimal(amount), coa_id=_coa(bank_db, 6815),
                           counter_coa_id=_coa(bank_db, 4400), document_number='DV01',
                           booking_type='entry', entry_form=form)

    assert _feld(_export(bank_db, with_payment_ids=with_payment_ids)['DV01'])[1] == expected


@pytest.mark.parametrize('form, amount, expected', [
    ('mirror_coa', '500.00', 'S'), ('mirror_coa', '-500.00', 'H'),
    ('mirror_counter', '500.00', 'H'), ('mirror_counter', '-500.00', 'S'),
])
def test_datev_marker_of_a_mirror_between_two_own_banks(bank_db, form, amount, expected):
    """Spiegel Bank an Bank: ``mirror_coa`` (Betrag auf dem Konto, so schreiben ihn die Importe) hat
    das Konto bei positivem Betrag im Soll, ``mirror_counter`` (so schreibt ihn die Maske) das
    Gegenkonto."""
    bank_db.insert_booking('2026-03-10', Decimal(amount), coa_id=_coa(bank_db, 1800),
                           counter_coa_id=_coa(bank_db, 1810), document_number='DV01',
                           booking_type='entry', entry_form=form)

    assert _feld(_export(bank_db)['DV01'])[1] == expected


def test_datev_handler_delivers_the_new_markers(bank_db):
    """Über den echten Handler: Er reicht die Zahlungskonten an den Export weiter."""
    _alle_faelle_buchen(bank_db)
    result = handle_datev_export(bank_db, {'date_from': ['2026-03-01'], 'date_to': ['2026-03-31']})
    assert isinstance(result[0], bytes), result
    rows = _datev_rows(result[0])
    assert len(rows) == len(DATEV_FAELLE)
    for number, (fall_id, _coa_nr, _counter, _amount, _rate, expected) in enumerate(
            DATEV_FAELLE, start=1):
        assert _feld(rows[f'DV{number:02d}']) == expected, fall_id


def test_datev_fresh_database_clearing_account_is_a_payment_account(tmp_db):
    """Frische Datenbank: Die Standard-Kasse führt 1600, das Verrechnungskonto 1460 ist kein
    eigenes Konto und trotzdem ein Zahlungskonto. Mit ``get_bank_coa_ids`` allein wäre
    „6815 / 1460“ eine Umbuchung und bekäme H."""
    assert tmp_db.get_bank_coa_ids() == {_coa(tmp_db, 1600)}
    _book_entry(tmp_db, 'DV01', 6815, 1460, '-18.35', 0.19)
    _book_entry(tmp_db, 'DV02', 1460, 4400, '50.00', None)
    _book_entry(tmp_db, 'DV03', 4400, 1460, '50.00', None)
    result = handle_datev_export(tmp_db, {'date_from': ['2026-03-01'], 'date_to': ['2026-03-31']})
    assert isinstance(result[0], bytes), result
    rows = _datev_rows(result[0])
    assert _feld(rows['DV01']) == ('18,35', 'S', '6815', '1460', '9')    # Zweckkonto vorne, Kasse hinten
    assert _feld(rows['DV02']) == ('50,00', 'S', '1460', '4400', '0')    # Kasse vorne, Einnahme
    assert _feld(rows['DV03']) == ('50,00', 'H', '4400', '1460', '0')    # Zweckkonto vorne, Einnahme


@pytest.mark.parametrize('zweck, amount, rate, tax, expected', [
    pytest.param(4400, '119.00', 0.19, '19.00', ('119,00', 'H', '4400', '', '3'), id='einnahme'),
    pytest.param(6815, '-59.50', 0.19, '-9.50', ('59,50', 'S', '6815', '', '9'), id='ausgabe'),
])
def test_datev_bank_without_skr_saved_through_the_mask(tmp_db, bank_without_skr, zweck, amount,
                                                       rate, tax, expected):
    """Von der Maske bis in den Stapel: Die Bankbewegung ohne SKR-Nummer bekommt einen Satz mit
    Zweckkonto vorne und leerem Gegenkonto – im Export steht die Einnahme im Haben, die Ausgabe
    im Soll des Zweckkontos."""
    post = _mask_post(bank_without_skr, _coa(tmp_db, zweck), amount, rate, tax)
    assert handle_add_transaction(tmp_db, post)[0] == 303

    result = handle_datev_export(tmp_db, {'date_from': ['2026-03-01'], 'date_to': ['2026-03-31']})

    assert isinstance(result[0], bytes), result
    assert [_feld(fields) for fields in _datev_rows(result[0]).values()] == [expected]


# ── C. get_payment_coa_ids ───────────────────────────────────────────────────

def _numbers(db, coa_ids):
    by_id = db.get_coa_id_to_number_map()
    return {by_id[coa_id] for coa_id in coa_ids}


def test_payment_ids_on_a_fresh_database_hold_the_standard_cash_and_the_clearing_account(tmp_db):
    assert tmp_db.get_bank_coa_ids() == {_coa(tmp_db, 1600)}     # die Standard-Kasse führt 1600
    numbers = _numbers(tmp_db, tmp_db.get_payment_coa_ids())
    assert {1460, 1600} <= numbers                               # 1600 über das eigene Konto
    assert numbers <= {1460, 1600}


def test_payment_ids_hold_own_banks_and_the_clearing_account_but_no_purpose_accounts(bank_db):
    bank_ids = bank_db.get_bank_coa_ids()
    payment_ids = bank_db.get_payment_coa_ids()
    assert bank_ids <= payment_ids
    assert _numbers(bank_db, bank_ids) == {1800, 1810, 1600}      # die Standard-Kasse führt 1600
    numbers = _numbers(bank_db, payment_ids)
    assert {1800, 1810, 1460} <= numbers
    # keine Erlös-, Aufwand-, Steuer-, Privat- oder Forderungskonten
    assert numbers.isdisjoint({4400, 4300, 4405, 6815, 6805, 1401, 1406, 3806, 2100, 10000})
    assert numbers <= {1800, 1810, 1460, 1600}


def test_payment_ids_do_not_cover_the_old_cash_range_1000_to_1099(tmp_db):
    """Bis 2.0.x galt der SKR03-Kassenbereich als Geldkonto; in SKR04 sind 1000–1099 Vorräte. Seit
    2.1.0 gehört der Bereich nicht mehr dazu – was frühere Versionen damit importiert haben, hält die
    Form des Bestands fest (test_entry_form_migration.py)."""
    for number in (999, 1000, 1050, 1099, 1100):
        assert tmp_db.insert_chart_of_accounts(4, number, f'Konto {number}', '')
    numbers = _numbers(tmp_db, tmp_db.get_payment_coa_ids())
    assert numbers.isdisjoint({999, 1000, 1050, 1099, 1100})


def test_payment_ids_count_1600_through_the_own_cash_account_not_through_a_range(tmp_db):
    """Die Kasse der App (SKR04 1600) zählt wie die Bankkonten über ihr eigenes Konto: Auch ein
    Konto im Bereich 1600–1699 ohne eigenes Konto dazu ist kein Zahlungskonto."""
    for number in (1620, 1650, 1699):
        assert tmp_db.insert_chart_of_accounts(4, number, f'Kasse {number}', '')
    assert _numbers(tmp_db, tmp_db.get_bank_coa_ids()) == {1600}
    numbers = _numbers(tmp_db, tmp_db.get_payment_coa_ids())
    assert 1600 in numbers
    assert numbers.isdisjoint({1620, 1650, 1699})


def test_payment_ids_leave_out_1600_when_the_cash_account_has_no_skr_number(tmp_db):
    """Hat die Kasse keine SKR-Nummer (ältere Datenbank), ist 1600 – obwohl im Kontenrahmen –
    kein Zahlungskonto; das Verrechnungskonto 1460 bleibt eins."""
    cash = next(a for a in tmp_db.fetch_accounts() if a[6] == 1)         # IsCash
    tmp_db.update_account(cash[0], cash[1], cash[2], cash[3], cash[4], cash[5], skr_account=None)
    assert tmp_db.get_account_by_id(cash[0])[7] is None                  # SKRAccount
    assert tmp_db.get_coa_id_by_account_number(1600) is not None         # im Kontenrahmen vorhanden
    assert tmp_db.get_bank_coa_ids() == set()
    numbers = _numbers(tmp_db, tmp_db.get_payment_coa_ids())
    assert 1600 not in numbers
    assert 1460 in numbers
