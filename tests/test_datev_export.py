# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Tests für den DATEV-Export, speziell die Ableitung des BU-/Steuerschlüssels
aus Steuersatz + Soll/Haben (datev._taxrate_to_bu) sowie ein End-to-End-Export
über mehrere Buchungen. Der Steuerschlüssel wird NICHT gespeichert, sondern beim
Export aus dem Steuersatz und der Buchungsrichtung abgeleitet. Bei einer Zahlung ist
das die Richtung der Geldbewegung (Vorzeichen), bei einer Umbuchung mit Erlös- oder
Debitorenkonto immer die Umsatzsteuer-Richtung (datev._bu_direction).

Inhalte (Beträge/Belegnummern) sind zufällig/anonymisiert.
"""
import csv as _csv
import io
import random
import uuid

import pytest

import server.handlers as handlers
import userctx
from export import datev
from export.datev import _taxrate_to_bu
from tests.invoice_world import build_world, make_invoice, set_invoice_status

_C_BU = 8  # Spaltenindex BU-Schlüssel im DATEV-Buchungsstapel


def test_taxrate_to_bu_mapping():
    # Ausgabe/Vorsteuer (Soll)
    assert _taxrate_to_bu(0.19, 'S') == '9'
    assert _taxrate_to_bu(0.07, 'S') == '8'
    # Einnahme/Umsatzsteuer (Haben)
    assert _taxrate_to_bu(0.19, 'H') == '3'
    assert _taxrate_to_bu(0.07, 'H') == '2'
    # 0 % bzw. kein Schlüssel → '0' (z.B. WISO-Schlüssel 490)
    assert _taxrate_to_bu(0.0, 'S') == '0'
    assert _taxrate_to_bu(0.0, 'H') == '0'
    assert _taxrate_to_bu(None, 'S') == '0'


@pytest.mark.parametrize('rate, sh, key', [
    # Ausgabe/Vorsteuer
    (0.19, 'S', '9'), (0.07, 'S', '8'), (0.16, 'S', '7'), (0.05, 'S', '6'),
    # Einnahme/Umsatzsteuer
    (0.19, 'H', '3'), (0.07, 'H', '2'), (0.16, 'H', '5'), (0.05, 'H', '4'),
])
def test_taxrate_to_bu_has_a_key_for_each_of_the_four_rates(rate, sh, key):
    """19 %, 7 % und die befristeten Sätze 16 % und 5 % (2020): je ein eigener Schlüssel je
    Richtung. Der Satz steht als Bruch (0.19) oder als Prozentzahl (19) in den Büchern."""
    assert _taxrate_to_bu(rate, sh) == key
    assert _taxrate_to_bu(rate * 100, sh) == key
    assert _taxrate_to_bu(round(rate * 100), sh) == key


def test_taxrate_to_bu_does_not_mistake_16_percent_for_7_percent():
    """Die Schwellen ``>= 0.19`` / ``>= 0.07`` gaben für 16 % den Schlüssel für 7 % und für 5 % gar
    keinen."""
    for sh in ('S', 'H'):
        assert _taxrate_to_bu(0.16, sh) != _taxrate_to_bu(0.07, sh)
        assert _taxrate_to_bu(0.16, sh) != _taxrate_to_bu(0.19, sh)
        assert _taxrate_to_bu(0.05, sh) not in ('0', _taxrate_to_bu(0.07, sh))
    assert (_taxrate_to_bu(0.16, 'H'), _taxrate_to_bu(0.05, 'H')) == ('5', '4')


@pytest.mark.parametrize('rate', [0.107, 0.1899, 0.0701, 0.25, 0.01, 0.2, 0.0, 0, -1, None, 100])
@pytest.mark.parametrize('sh', ['S', 'H'])
def test_taxrate_to_bu_gives_no_key_to_an_unknown_rate(rate, sh):
    """Ein Satz, der nicht genau einer der vier ist, bekommt '0' – nie den Schlüssel eines
    anderen Satzes."""
    assert _taxrate_to_bu(rate, sh) == '0'


def _bu_values(csv_bytes):
    """BU-Schlüssel aller Datenzeilen aus dem DATEV-CSV extrahieren."""
    text = csv_bytes.decode('cp1252')
    lines = [ln for ln in text.split('\r\n') if ln]
    # Zeile 0 = EXTF-Kopf, Zeile 1 = Spaltenüberschriften, ab Zeile 2 = Daten
    out = []
    for ln in lines[2:]:
        fields = next(_csv.reader([ln], delimiter=';', quotechar='"'))
        if len(fields) > _C_BU:
            out.append(fields[_C_BU])
    return out


def test_export_derives_bu_per_booking(db_with_coa):
    db = db_with_coa
    coa = {r[2]: r[0] for r in db.fetch_chart_of_accounts()}
    doc1 = f"D{random.randint(1000, 9999)}"
    doc2 = f"D{random.randint(1000, 9999)}"
    gross19 = round(random.uniform(50, 500), 2)
    net0 = round(random.uniform(50, 500), 2)

    # Ausgabe 19 % (negativ → Soll → BU '9')
    db.insert_booking(date_booking='2025-07-03', amount=-gross19,
                      coa_id=coa.get(6815), counter_coa_id=coa.get(1460),
                      tax_rate=0.19, document_number=doc1, booking_type='entry')
    # Ausgabe ohne Steuer (0 % → BU '0')
    db.insert_booking(date_booking='2025-07-03', amount=-net0,
                      coa_id=coa.get(6815), counter_coa_id=coa.get(1460),
                      tax_rate=0.0, document_number=doc2, booking_type='entry')

    bookings = db.fetch_bookings_range('2025-07-01', '2025-07-31')
    coa_map = db.get_coa_id_to_number_map()
    csv_bytes, exported_ids = datev.export_to_datev(
        bookings, coa_map, '2025-07-01', '2025-07-31')

    assert len(exported_ids) == 2
    bu = _bu_values(csv_bytes)
    assert '9' in bu  # 19 % Ausgabe
    assert '0' in bu  # 0 %


# ── Umbuchung mit Erlösseite: immer die Umsatzsteuer-Richtung ─────────────────
# Eine Umbuchung („transfer“) hat keine Geldseite: Ihr Vorzeichen sagt nur, welche Seite im Soll steht. Die
# Gegenbuchung einer Stornorechnung („Debitoren 10000 an Wartekonto 4405“, negativ) bekam deshalb den
# Vorsteuer-Schlüssel und drehte die Forderung im Stapel nicht zurück. Zahlungssätze bleiben beim Vorzeichen.

_C_SH, _C_KONTO, _C_GEGENKONTO, _C_BELEG = 1, 6, 7, 10

#: COA-ID → Kontonummer der synthetischen Zeilen (wie ``db.get_coa_id_to_number_map``)
NUMBERS = {1: 10000, 2: 4405, 3: 4400, 4: 6222, 5: 520, 6: 1800}
REVENUE_SIDE = (1, 2, 3)         # Debitoren, Wartekonto, Erlöskonto
OTHER = (4, 5, 6)                # Abschreibung, Anlagekonto, Bank


def _row(number, coa_id, counter_coa_id, amount, rate, form=None):
    """Ein Buchungstupel wie ``SELECT * FROM Bookings`` – nur mit den Feldern, die der Export liest; das Belegfeld
    ``B<number>`` trägt die Zeile im Stapel."""
    row = [None] * 23
    row[0], row[1], row[8], row[9], row[11] = number, '2026-07-01', coa_id, counter_coa_id, amount
    row[12], row[13], row[15], row[16], row[22] = 'EUR', rate, f'Satz {number}', f'B{number}', form
    return tuple(row)


def _batch(rows, numbers=None, liquid_coa_ids=None):
    """{Belegfeld 1: (S/H, Konto, Gegenkonto, BU)} aus dem Stapel, den ``export_to_datev`` zu diesen Zeilen schreibt."""
    csv_bytes, exported = datev.export_to_datev(
        rows, NUMBERS if numbers is None else numbers, '2026-07-01', '2026-07-31', liquid_coa_ids=liquid_coa_ids)
    assert exported == [row[0] for row in rows]
    batch = {}
    for line in [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]:
        fields = next(_csv.reader([line], delimiter=';', quotechar='"'))
        batch[fields[_C_BELEG]] = (fields[_C_SH], fields[_C_KONTO], fields[_C_GEGENKONTO], fields[_C_BU])
    return batch


@pytest.mark.parametrize('rate, key', [(0.19, '3'), (0.07, '2'), (0.16, '5'), (0.05, '4')])
def test_the_counter_booking_of_a_receivable_has_the_same_tax_key_and_the_opposite_marker(rate, key):
    """Forderung F („Debitoren an Wartekonto“, +119) und Gegenbuchung F′ (−119): derselbe BU-Schlüssel, entgegengesetztes
    Soll/Haben – F′ dreht F im Stapel zurück (früher bekam F′ den Vorsteuer-Schlüssel, F den Umsatzsteuer-Schlüssel)."""
    batch = _batch([_row(1, 1, 2, 119.0, rate, 'transfer'), _row(2, 1, 2, -119.0, rate, 'transfer')])
    assert batch['B1'] == ('S', '10000', '4405', key)
    assert batch['B2'] == ('H', '10000', '4405', key)


@pytest.mark.parametrize('coa, counter', [(a, b) for a in (*REVENUE_SIDE, *OTHER) for b in (*REVENUE_SIDE, *OTHER)
                                          if a != b and (a in REVENUE_SIDE or b in REVENUE_SIDE)])
@pytest.mark.parametrize('amount', [119.0, -119.0])
def test_a_transfer_with_a_revenue_side_always_gets_the_vat_key(coa, counter, amount):
    """Eine Umbuchung mit Erlös-, Wartekonto oder Debitoren auf einer der beiden Seiten bekommt – positiv wie
    negativ – den Umsatzsteuer-Schlüssel (19 % = 3); das Soll/Haben-Kennzeichen folgt weiter dem Vorzeichen."""
    batch = _batch([_row(1, coa, counter, amount, 0.19, 'transfer')])
    assert batch['B1'] == ('S' if amount > 0 else 'H', str(NUMBERS[coa]), str(NUMBERS[counter]), '3')


@pytest.mark.parametrize('coa, counter', [(a, b) for a in OTHER for b in OTHER if a != b])
@pytest.mark.parametrize('amount, key', [(1000.0, '3'), (-1000.0, '9')])
def test_a_transfer_without_a_revenue_side_keeps_the_sign_rule(coa, counter, amount, key):
    """Aufwand an Anlagekonto (AfA) und andere Umbuchungen ohne Erlösseite: allein das Vorzeichen, wie bisher."""
    assert _batch([_row(1, coa, counter, amount, 0.19, 'transfer')])['B1'][3] == key


@pytest.mark.parametrize('form', ['purpose_first', 'liquid_first', 'mirror_coa', 'mirror_counter', 'mirror_legacy'])
@pytest.mark.parametrize('coa, counter', [(3, 6), (6, 3)])
@pytest.mark.parametrize('amount, key', [(119.0, '3'), (-119.0, '9')])
def test_payments_and_mirrors_keep_the_direction_of_the_money_even_with_a_revenue_account(form, coa, counter, amount,
                                                                                         key):
    """Nur die Umbuchung ändert die Regel: Zahlungssätze (Geldkonto vorne oder hinten) und Spiegel schlüsseln weiter
    nach der Richtung der Geldbewegung – eine Erstattung (negativ) auf ein Erlöskonto bleibt Vorsteuer-Richtung."""
    assert _batch([_row(1, coa, counter, amount, 0.19, form)])['B1'][3] == key


@pytest.mark.parametrize('number, revenue', [
    (0, False), (3999, False), (4000, True), (4400, True), (4999, True), (5000, False), (6222, False),
    (9999, False), (10000, True), (10001, True), (69999, True), (70000, False)])
@pytest.mark.parametrize('side', ['coa', 'counter'])
def test_the_revenue_side_are_the_accounts_4000_to_4999_and_10000_to_69999(number, revenue, side):
    numbers = {1: number, 2: 6222}                         # die andere Seite ist nie eine Erlösseite
    row = _row(1, 1, 2, -50.0, 0.19, 'transfer') if side == 'coa' else _row(1, 2, 1, -50.0, 0.19, 'transfer')
    assert _batch([row], numbers)['B1'][3] == ('3' if revenue else '9')


@pytest.mark.parametrize('written, revenue', [
    (4405, True), ('4405', True), ('10000', True), ('1800', False), ('4405a', False), ('abc', False), ('', False),
    (None, False)])
def test_account_numbers_may_be_numbers_or_text_and_non_numeric_ones_do_not_count(written, revenue):
    batch = _batch([_row(1, 1, 2, -50.0, 0.19, 'transfer')], {1: written, 2: 6222})
    assert batch['B1'][3] == ('3' if revenue else '9')


def test_a_missing_account_does_not_count_and_does_not_fail():
    batch = _batch([_row(1, 99, None, -50.0, 0.19, 'transfer'), _row(2, None, 98, -50.0, 0.19, 'transfer')])
    assert [batch['B1'][3], batch['B2'][3]] == ['9', '9']


@pytest.mark.parametrize('rate, expected', [(None, '0'), (0.0, '0'), (0.107, '0'), (0.25, '0')])
def test_a_transfer_without_a_known_tax_rate_has_no_key_whatever_the_accounts(rate, expected):
    for amount in (119.0, -119.0):
        assert _batch([_row(1, 1, 2, amount, rate, 'transfer')])['B1'][3] == expected


def test_a_row_without_a_stored_form_is_read_with_the_payment_accounts_when_they_are_given():
    """Eine Zeile ohne Form (per Roh-SQL angelegt) wird mit den Zahlungskonten eingeordnet: Sind beide Seiten keine,
    ist sie eine Umbuchung; ohne Zahlungskonten (``None``) gilt allein das Vorzeichen."""
    receivable = _row(1, 1, 2, -119.0, 0.19)                # 10000 an 4405, keine Form
    assert _batch([receivable], liquid_coa_ids={6})['B1'][3] == '3'
    assert _batch([receivable], liquid_coa_ids=None)['B1'][3] == '9'
    payment = _row(1, 3, 6, -119.0, 0.19)                   # 4400 an 1800 mit 1800 als Zahlungskonto: eine Zahlung
    assert _batch([payment], liquid_coa_ids={6})['B1'][3] == '9'


def test_the_stored_form_wins_over_the_payment_accounts():
    assert _batch([_row(1, 1, 2, -119.0, 0.19, 'transfer')], liquid_coa_ids={1, 2})['B1'][3] == '3'
    assert _batch([_row(1, 1, 2, -119.0, 0.19, 'purpose_first')], liquid_coa_ids=set())['B1'][3] == '9'


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user('anna')
    world = build_world(tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return world


@pytest.mark.parametrize('rate, key', [(19, '3'), (7, '2')])
def test_a_sent_and_cancelled_invoice_gets_the_same_tax_key_on_receivable_and_counter_booking(world, rate, key):
    """Über den echten Weg – versenden, stornieren, Export über den Handler: Forderung und Gegenbuchung der
    Stornorechnung stehen mit gleichem BU-Schlüssel und entgegengesetztem Soll/Haben im Stapel."""
    items = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung', 'unitPrice': 100.0,
              'taxRate': rate}]
    invoice_id = make_invoice(world, 'draft', number='T-100', items=items, taxRate=rate / 100)
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    storno = world.db.cancel_invoice(invoice_id, '2026-07-01', 'anna')
    assert storno.ok and storno.counter_booked, storno

    csv_bytes, _name = handlers.handle_datev_export(world.db, {'date_from': ['2026-01-01'], 'date_to': ['2026-12-31']})

    assert isinstance(csv_bytes, bytes), csv_bytes
    rows = {}
    for line in [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]:
        fields = next(_csv.reader([line], delimiter=';', quotechar='"'))
        rows[fields[_C_BELEG]] = fields
    assert set(rows) == {'T-100', storno.storno_number}
    receivable, counter = rows['T-100'], rows[storno.storno_number]
    assert (receivable[_C_KONTO], counter[_C_KONTO]) == ('10000', '10000')
    gross = {19: '119,00', 7: '107,00'}[rate]
    assert receivable[0] == counter[0] == gross
    assert (receivable[_C_SH], counter[_C_SH]) == ('S', 'H')
    assert receivable[_C_BU] == counter[_C_BU] == key
