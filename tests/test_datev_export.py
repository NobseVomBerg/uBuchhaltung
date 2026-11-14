# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Tests für den DATEV-Export (seit 2.3.0 dem Stapel nachgebildet, den WISO Mein Büro schreibt).

* **BU-Schlüssel (Spalte 8):** Der an der Buchung gespeicherte Schlüssel (``Bookings.DatevKey``, Zeilenspalte 23)
  geht unverändert in den Stapel – auch 490, 100 oder 121 ohne Satz; nur eine ganze Zahl von 1 bis 9999 zählt.
  Wo keiner gespeichert ist, leitet der Export ihn nach der Art des Zweckkontos ab (``db.datev_keys``): Erlöskonto
  Umsatzsteuer, jedes andere Konto Vorsteuer – auch bei einer Erstattung –, mit den vertauschten Sätzen im zweiten
  Halbjahr 2020; ein Spiegel trägt keinen, die AfA („Aufwand an Anlagekonto“, von der Art Vorsteuer) nur deshalb keinen,
  weil sie keinen Steuersatz hat.
* **Rechnungskette** (Forderung, „Bank an Debitoren“, „Wartekonto an Erlöskonto“, Gegenbuchung einer
  Stornorechnung): Die Umbuchungen mit Debitor oder Wartekonto tragen keinen Schlüssel, aber in Spalte 40 den Satz;
  Leistungsdatum (114) nur an der Forderung, Land (119) an der ganzen Kette, Spalte 115 nie.
* **Belegfeld 1** ohne Leerzeichen und bis 36 Zeichen, **Vorlauf** mit Berater- und Mandantennummer der eigenen Firma.

Die Regeln selbst prüft ``test_datev_keys.py``; hier steht, wie der Export sie verwendet. Inhalte (Beträge,
Belegnummern, Nummern) sind erfunden.
"""
import csv as _csv

import pytest

import server.handlers as handlers
import userctx
from export import datev
from tests.document_helpers import add_document
from tests.entry_form_helpers import execute, query
from tests.invoice_world import build_world, make_invoice, set_invoice_status

_C_SH, _C_KONTO, _C_GEGENKONTO, _C_BU, _C_BELEG, _C_TEXT = 1, 6, 7, 8, 10, 13
_C_RATE, _C_LEISTUNG, _C_STEUERDATUM, _C_LAND = 40, 114, 115, 119
PLACEHOLDERS = ('70000', '99999')

#: COA-ID → Kontonummer der synthetischen Zeilen (wie ``db.get_coa_id_to_number_map``)
NUMBERS = {1: 10000, 2: 4405, 3: 4400, 4: 6222, 5: 520, 6: 1800, 7: 6815, 8: 4300, 9: 1810, 10: 2100, 11: 4640}
WAITING = (4405, 4345, 4340)
DEBTOR, WAIT, REVENUE, DEPRECIATION, ASSET, BANK, EXPENSE, REVENUE_7, BANK_TWO, PRIVATE, USE = range(1, 12)


def _row(number, coa_id, counter_coa_id, amount, rate, form=None, key=None, date='2026-07-01', doc=None):
    """Ein Buchungstupel wie ``SELECT * FROM Bookings`` (24 Spalten) – nur mit den Feldern, die der Export liest; das
    Belegfeld trägt die Zeile im Stapel (``B<number>``, wenn ``doc`` fehlt)."""
    row = [None] * 24
    row[0], row[1], row[8], row[9], row[11] = number, date, coa_id, counter_coa_id, amount
    row[12], row[13], row[15] = 'EUR', rate, f'Satz {number}'
    row[16] = f'B{number}' if doc is None else doc
    row[22], row[23] = form, key
    return tuple(row)


def _parse(csv_bytes):
    """(Vorlaufzeile, {Belegfeld 1: alle 124 Felder}) aus dem Stapel."""
    lines = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line]
    rows = {}
    for line in lines[2:]:
        fields = next(_csv.reader([line], delimiter=';', quotechar='"'))
        assert len(fields) == 124, line
        rows[fields[_C_BELEG]] = fields
    return lines[0], rows


def _export(rows, numbers=None, liquid=None, waiting=WAITING, invoices=None, consultant=None, client=None):
    csv_bytes, exported = datev.export_to_datev(
        rows, NUMBERS if numbers is None else numbers, '2026-07-01', '2026-07-31', liquid_coa_ids=liquid,
        waiting_numbers=waiting, invoices=invoices, consultant_no=consultant, client_no=client)
    assert exported == [row[0] for row in rows]
    return _parse(csv_bytes)[1]


def _one(row, **kwargs):
    """Die Felder der einen Zeile; ``kwargs`` wie bei ``_export``."""
    return _export([row], **kwargs)[row[16]]


def _bu(row, **kwargs):
    return _one(row, **kwargs)[_C_BU]


# ── Ein gespeicherter Schlüssel geht unverändert in den Stapel ────────────────

@pytest.mark.parametrize('key, rate, eu_rate', [
    (401, 0.19, ''), (402, 0.07, ''), (7, 0.19, ''), (6, 0.05, ''), (490, 0.0, ''), (490, None, ''), (490, 0.19, ''),
    (100, 0.0, ''), (100, None, ''), (400, None, ''), (181, 0.19, ''), (1, 0.19, ''), (9999, 0.19, ''),
    (101, 0.19, '19,00'), (102, 0.07, '7,00'), (5, 0.16, '16,00'), (4, 0.05, '5,00'),
    (121, 0.19, '19,00'), (122, 0.07, '7,00'), (101, None, ''), (101, 0.0, ''), (121, None, '')])
def test_a_stored_key_is_written_as_it_is_and_the_rate_only_follows_a_vat_key(key, rate, eu_rate):
    """Der Schlüssel der Quelle gilt, gleich welche Konten, welcher Satz und welches Datum – auch ein Schlüssel ohne
    Satz (490, 100). Spalte 40 trägt den Satz, wenn der Schlüssel ein Umsatzsteuer-Schlüssel ist und es einen Satz gibt."""
    fields = _one(_row(1, EXPENSE, BANK, -119.0, rate, 'purpose_first', key))
    assert fields[_C_BU] == str(key) and fields[_C_RATE] == eu_rate


@pytest.mark.parametrize('date, key, rate', [('2020-08-15', 401, 0.19), ('2020-08-15', 7, 0.16), ('2020-06-30', 7, 0.19),
                                             ('2021-01-01', 5, 0.19), ('2020-12-31', 101, 0.19)])
def test_a_stored_key_is_not_recomputed_from_the_date(date, key, rate):
    """Auch ein Schlüssel, der zum Satz des Datums nicht passt, bleibt, wie die Quelle ihn schrieb."""
    assert _bu(_row(1, EXPENSE, BANK, -119.0, rate, 'purpose_first', key, date=date)) == str(key)


@pytest.mark.parametrize('form', ['purpose_first', 'liquid_first', 'transfer', 'mirror_coa', 'mirror_counter',
                                  'mirror_legacy', None])
def test_a_stored_key_beats_every_form_even_a_mirror(form):
    assert _bu(_row(1, EXPENSE, BANK, -119.0, 0.19, form, 490), liquid={6}) == '490'


@pytest.mark.parametrize('stored', [0, '0', '', None, -1, 10000, 123456, 'abc', '401;1', '401"', '40\n1', 401.5, True,
                                    False, '0401', ' ', '-401', float('nan')])
def test_anything_but_a_whole_number_from_1_to_9999_counts_as_no_stored_key(stored):
    """Ein per SQL-Konsole gesetzter anderer Wert wird wie „nicht vorhanden“ behandelt – nie roh ausgegeben: Der
    Export leitet den Schlüssel ab, und die Zeile hat genau 124 Felder."""
    fields = _one(_row(1, EXPENSE, BANK, -119.0, 0.19, 'purpose_first', stored))
    assert fields[_C_BU] == '401'


def test_a_stored_key_may_be_stored_as_text_or_a_whole_float():
    assert _bu(_row(1, EXPENSE, BANK, -119.0, 0.19, 'purpose_first', '490')) == '490'
    assert _bu(_row(2, EXPENSE, BANK, -119.0, 0.19, 'purpose_first', 490.0)) == '490'


def test_short_rows_without_form_and_key_still_work():
    """Eine Zeile mit nur 17 Spalten (ohne Spalte 22 und 23): ohne Form und ohne Schlüssel wird abgeleitet."""
    row = _row(1, EXPENSE, BANK, -119.0, 0.19)[:17]
    assert len(row) == 17
    fields = _one(row, liquid={6})
    assert (fields[_C_SH], fields[_C_BU]) == ('S', '401')                         # ohne Form: mit den Zahlungskonten gelesen
    assert _one(row)[_C_BU] == '401'                                            # ohne Zahlungskonten: nach dem Vorzeichen
    assert _bu(_row(2, EXPENSE, BANK, 119.0, 0.19)[:17]) == '101'                # positiv: Umsatzsteuer
    assert _bu(_row(3, EXPENSE, BANK, -119.0, 0.19, 'purpose_first')[:23]) == '401'    # nur mit Form


# ── Ohne gespeicherten Schlüssel: nach der Art des Zweckkontos ───────────────

@pytest.mark.parametrize('rate, expense_key, revenue_key', [(0.19, '401', '101'), (0.07, '402', '102'),
                                                            (0.16, '7', '5'), (0.05, '6', '4')])
def test_the_four_rates_of_a_payment_follow_the_purpose_account(rate, expense_key, revenue_key):
    assert _bu(_row(1, EXPENSE, BANK, -119.0, rate, 'purpose_first')) == expense_key
    assert _bu(_row(2, REVENUE, BANK, 119.0, rate, 'purpose_first')) == revenue_key
    assert _bu(_row(3, BANK, EXPENSE, -119.0, rate, 'liquid_first')) == expense_key
    assert _bu(_row(4, BANK, REVENUE, 119.0, rate, 'liquid_first')) == revenue_key


def test_an_expense_with_19_percent_has_401_and_an_income_on_a_revenue_account_has_101():
    assert _bu(_row(1, EXPENSE, BANK, -119.0, 0.19, 'purpose_first')) == '401'
    assert _bu(_row(2, REVENUE, BANK, 119.0, 0.19, 'purpose_first')) == '101'


@pytest.mark.parametrize('amount', [119.0, -119.0])
@pytest.mark.parametrize('form, coa, counter, key', [
    ('purpose_first', EXPENSE, BANK, '401'), ('liquid_first', BANK, EXPENSE, '401'),
    ('purpose_first', REVENUE, BANK, '101'), ('liquid_first', BANK, REVENUE, '101')])
def test_the_sign_of_the_money_does_not_change_the_key_of_a_payment(form, coa, counter, key, amount):
    """Eine Erstattung (Geld in die andere Richtung) auf ein Aufwandskonto bleibt Vorsteuer (401), auf ein Erlöskonto
    Umsatzsteuer (101): Nur das S/H-Kennzeichen kehrt sich um."""
    assert _bu(_row(1, coa, counter, amount, 0.19, form)) == key


def test_a_refund_on_an_expense_account_keeps_the_input_tax_key_and_turns_the_marker():
    spent = _one(_row(1, EXPENSE, BANK, -119.0, 0.19, 'purpose_first'))
    refunded = _one(_row(2, EXPENSE, BANK, 119.0, 0.19, 'purpose_first'))
    assert (spent[_C_SH], spent[_C_BU]) == ('S', '401') and (refunded[_C_SH], refunded[_C_BU]) == ('H', '401')


@pytest.mark.parametrize('date, rate, expense_key, revenue_key', [
    ('2020-08-15', 0.16, '401', '101'), ('2020-08-15', 0.05, '402', '102'),
    ('2020-08-15', 0.19, '7', '5'), ('2020-08-15', 0.07, '6', '4'),
    ('2020-06-30', 0.19, '401', '101'), ('2020-06-30', 0.16, '7', '5'),
    ('2020-07-01', 0.16, '401', '101'), ('2020-12-31', 0.19, '7', '5'), ('2021-01-01', 0.19, '401', '101'),
    ('2021-01-01', 0.16, '7', '5')])
def test_the_swap_of_the_rates_from_july_to_december_2020(date, rate, expense_key, revenue_key):
    assert _bu(_row(1, EXPENSE, BANK, -119.0, rate, 'purpose_first', date=date)) == expense_key
    assert _bu(_row(2, REVENUE, BANK, 119.0, rate, 'purpose_first', date=date)) == revenue_key


@pytest.mark.parametrize('rate', [None, 0.0, 0, -1, 0.107, 0.25, 0.055])
@pytest.mark.parametrize('form, coa, counter', [('purpose_first', EXPENSE, BANK), ('purpose_first', REVENUE, BANK),
                                                ('liquid_first', BANK, EXPENSE), ('transfer', DEPRECIATION, ASSET)])
def test_without_a_rate_or_with_an_unknown_one_there_is_no_key(form, coa, counter, rate):
    assert _bu(_row(1, coa, counter, -119.0, rate, form)) == '0'


def test_the_depreciation_has_no_key_only_because_it_has_no_rate():
    """„Aufwand an Anlagekonto“ (AfA) ist von der Art Vorsteuer: ohne Steuersatz trägt sie keinen Schlüssel und keine
    Spalte 40, mit einem Satz (eine Umbuchung, die die Maske so schreibt) den Vorsteuer-Schlüssel."""
    fields = _one(_row(1, DEPRECIATION, ASSET, 600.0, None, 'transfer'))
    assert (fields[_C_BU], fields[_C_RATE]) == ('0', '')
    assert _bu(_row(2, DEPRECIATION, ASSET, 600.0, 0.19, 'transfer')) == '401'     # mit Satz: Vorsteuer


@pytest.mark.parametrize('form', ['mirror_coa', 'mirror_counter', 'mirror_legacy'])
@pytest.mark.parametrize('amount', [500.0, -500.0])
def test_a_mirror_has_no_key_even_with_a_rate(form, amount):
    fields = _one(_row(1, BANK, BANK_TWO, amount, 0.19, form))
    assert (fields[_C_BU], fields[_C_RATE]) == ('0', '')


def test_a_payment_without_a_purpose_account_follows_the_direction_of_the_money():
    assert _bu(_row(1, None, BANK, 119.0, 0.19, 'purpose_first')) == '101'
    assert _bu(_row(2, None, BANK, -119.0, 0.19, 'purpose_first')) == '401'
    assert _bu(_row(3, BANK, None, 119.0, 0.19, 'liquid_first')) == '101'
    assert _bu(_row(4, BANK, None, -119.0, 0.19, 'liquid_first')) == '401'


def test_a_payment_on_a_debtor_account_has_no_key_and_no_rate():
    """„Bank an Debitoren“ der Rechnungskette: der Betrag und das Konto, sonst nichts."""
    fields = _one(_row(1, BANK, DEBTOR, 119.0, None, 'liquid_first'))
    assert (fields[_C_BU], fields[_C_RATE], fields[_C_LEISTUNG], fields[_C_LAND]) == ('0', '', '', '')


def test_a_missing_account_does_not_count_and_does_not_fail():
    batch = _export([_row(1, 99, None, -50.0, 0.19, 'transfer'), _row(2, None, 98, -50.0, 0.19, 'transfer'),
                     _row(3, 99, None, -50.0, 0.19, 'purpose_first')])
    assert [batch['B1'][_C_BU], batch['B2'][_C_BU], batch['B3'][_C_BU]] == ['0', '0', '401']


@pytest.mark.parametrize('written, known', [(6815, True), ('6815', True), (' 6815 ', True), ('6815a', False),
                                            ('', False), (None, False), ('abc', False)])
def test_account_numbers_may_be_numbers_or_text_and_only_a_number_is_a_purpose_account(written, known):
    """Mit einer Zahl als Zweckkonto (Aufwand) gilt die Art des Kontos: Vorsteuer, auch bei einer Erstattung (+119);
    ohne eine (leer, ``None``, Text) entscheidet die Geldrichtung – die Einnahme wäre Umsatzsteuer."""
    numbers = {**NUMBERS, EXPENSE: written}
    assert _bu(_row(1, EXPENSE, BANK, 119.0, 0.19, 'purpose_first'), numbers=numbers) == ('401' if known else '101')


@pytest.mark.parametrize('coa, counter, amount, key', [
    (EXPENSE, BANK, -119.0, '401'), (BANK, EXPENSE, -119.0, '401'), (REVENUE, BANK, 119.0, '101')])
def test_a_row_without_a_stored_form_is_read_with_the_payment_accounts_when_they_are_given(coa, counter, amount, key):
    """Eine Zeile ohne Form (per Roh-SQL angelegt) wird mit den Zahlungskonten eingeordnet; ohne sie (``None``) gilt
    allein das Vorzeichen: positiv Umsatzsteuer, negativ Vorsteuer."""
    row = _row(1, coa, counter, amount, 0.19)
    assert _bu(row, liquid={6}) == key
    assert _bu(row, liquid=None) == ('101' if amount > 0 else '401')


def test_a_receivable_without_a_stored_form_is_the_invoice_chain_only_with_the_payment_accounts():
    receivable = _row(1, DEBTOR, WAIT, -119.0, 0.19)                  # 10000 an 4405, keine Form
    with_accounts, without = _one(receivable, liquid={6}), _one(receivable, liquid=None)
    assert (with_accounts[_C_BU], with_accounts[_C_RATE]) == ('0', '19,00')
    assert (without[_C_BU], without[_C_RATE]) == ('401', '')                  # allein das Vorzeichen: Ausgabe


def test_the_stored_form_wins_over_the_payment_accounts():
    assert _bu(_row(1, DEBTOR, WAIT, -119.0, 0.19, 'transfer'), liquid={1, 2}) == '0'      # Rechnungskette
    assert _bu(_row(2, DEBTOR, WAIT, -119.0, 0.19, 'purpose_first'), liquid=set()) == '0'  # Zweckkonto Debitor: keiner
    assert _bu(_row(3, EXPENSE, BANK, -119.0, 0.19, 'purpose_first'), liquid={7, 6}) == '401'


# ── Die Rechnungskette ───────────────────────────────────────────────────────

INVOICES = {'R1': ('2026-06-20', 'DE'), 'R2': ('2026-05-02', ''), 'R3': (None, 'fr'), 'R4': ('', 'Frankreich')}


def _chain(doc='R1', amount=119.0, rate=0.19, date='2026-07-01', key=None):
    """Die Sätze einer Rechnung samt Zahlung, wie die App sie schreibt (Forderung, „Bank an Debitoren“, „Wartekonto an
    Erlöskonto“, dazu der Einzelsatz „Erlöskonto an Bank“ einer Rechnung ohne Wartekonto)."""
    return [_row(1, DEBTOR, WAIT, amount, rate, 'transfer', key, date, doc + 'a'),
            _row(2, BANK, DEBTOR, amount, None, 'liquid_first', key, date, doc + 'b'),
            _row(3, WAIT, REVENUE, amount, rate, 'transfer', key, date, doc + 'c'),
            _row(4, REVENUE, BANK, amount, rate, 'purpose_first', key, date, doc + 'd')]


def _chain_lines(doc, invoices=INVOICES, amount=119.0, rate=0.19):
    """Die vier Sätze mit derselben Beleg-Nr. – als Liste der Felder je Zeile in der Reihenfolge der Sätze."""
    rows = [row[:16] + (doc,) + row[17:] for row in _chain(doc, amount, rate)]
    csv_bytes, _ = datev.export_to_datev(rows, NUMBERS, '2026-07-01', '2026-07-31', liquid_coa_ids={6},
                                         waiting_numbers=WAITING, invoices=invoices)
    return [next(_csv.reader([line], delimiter=';', quotechar='"'))
            for line in [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]]


def test_the_invoice_chain_carries_no_key_but_the_rate_in_column_40():
    forderung, zahlung, umbuchung, einzelsatz = _chain_lines('R1')
    for fields in (forderung, umbuchung):
        assert (fields[_C_BU], fields[_C_RATE]) == ('0', '19,00')                 # Rechnungskette: kein Schlüssel, Satz
    assert (zahlung[_C_BU], zahlung[_C_RATE]) == ('0', '')                        # „Bank an Debitoren“: nichts
    assert (einzelsatz[_C_BU], einzelsatz[_C_RATE]) == ('101', '19,00')           # Erlöskonto an Bank: Umsatzsteuer


@pytest.mark.parametrize('rate, text', [(0.19, '19,00'), (0.07, '7,00'), (0.16, '16,00'), (0.05, '5,00'),
                                        (0.107, '10,70')])
def test_the_rate_of_the_chain_has_two_decimals_and_a_comma(rate, text):
    forderung = _chain_lines('R1', rate=rate)[0]
    assert forderung[_C_RATE] == text


def test_the_invoice_chain_without_a_rate_has_no_column_40():
    forderung, _zahlung, umbuchung, _einzel = _chain_lines('R1', rate=None)
    assert (forderung[_C_RATE], umbuchung[_C_RATE]) == ('', '')


def test_the_chain_in_the_swap_period_still_carries_no_key_and_the_rate():
    forderung = _one(_row(1, DEBTOR, WAIT, 116.0, 0.16, 'transfer', date='2020-08-15'))
    assert (forderung[_C_BU], forderung[_C_RATE]) == ('0', '16,00')


def test_the_counter_booking_of_a_credit_note_is_the_receivable_with_the_opposite_marker():
    """Forderung F („Debitoren an Wartekonto“, +119) und Gegenbuchung F′ (−119): derselbe BU-Schlüssel (keiner), derselbe
    Satz, entgegengesetztes Soll/Haben – F′ dreht F im Stapel zurück."""
    batch = _export([_row(1, DEBTOR, WAIT, 119.0, 0.19, 'transfer'), _row(2, DEBTOR, WAIT, -119.0, 0.19, 'transfer')])
    assert [batch['B1'][i] for i in (_C_SH, _C_KONTO, _C_GEGENKONTO, _C_BU, _C_RATE)] == ['S', '10000', '4405', '0', '19,00']
    assert [batch['B2'][i] for i in (_C_SH, _C_KONTO, _C_GEGENKONTO, _C_BU, _C_RATE)] == ['H', '10000', '4405', '0', '19,00']


@pytest.mark.parametrize('coa, counter', [(DEBTOR, WAIT), (WAIT, DEBTOR), (WAIT, REVENUE), (REVENUE, WAIT),
                                          (DEBTOR, REVENUE), (REVENUE, DEBTOR), (DEBTOR, DEPRECIATION)])
@pytest.mark.parametrize('amount', [119.0, -119.0])
def test_a_transfer_with_a_debtor_or_a_waiting_account_is_the_chain_whatever_the_sign(coa, counter, amount):
    fields = _one(_row(1, coa, counter, amount, 0.19, 'transfer'))
    assert (fields[_C_BU], fields[_C_RATE], fields[_C_LAND]) == ('0', '19,00', 'DE')
    assert fields[_C_SH] == ('S' if amount > 0 else 'H')                          # das Kennzeichen folgt weiter dem Vorzeichen


@pytest.mark.parametrize('coa, counter, key', [(REVENUE, PRIVATE, '101'), (PRIVATE, USE, '101'), (EXPENSE, PRIVATE, '401'),
                                               (DEPRECIATION, ASSET, '401'), (PRIVATE, ASSET, '401')])
def test_another_transfer_follows_the_account_class_whatever_the_sign(coa, counter, key):
    """Eine Umbuchung ohne Debitor und Wartekonto: ein Erlöskonto auf einer Seite – Umsatzsteuer (die
    Privatentnahme 2100 an 4640), ein Aufwands- oder Anlagekonto – Vorsteuer."""
    for amount in (119.0, -119.0):
        fields = _one(_row(1, coa, counter, amount, 0.19, 'transfer'))
        assert (fields[_C_BU], fields[_C_LAND]) == (key, '')                      # keine Rechnungskette


def test_the_waiting_accounts_come_from_the_caller():
    """Ohne Wartekonten ist 4405 nur ein Erlöskonto: Die Umbuchung 4405 an 4400 trägt dann Umsatzsteuer, kein Land."""
    row = _row(1, WAIT, REVENUE, 119.0, 0.19, 'transfer')
    assert [_one(row)[i] for i in (_C_BU, _C_LAND)] == ['0', 'DE']
    assert [_one(row, waiting=())[i] for i in (_C_BU, _C_LAND)] == ['101', '']
    assert [_one(row, waiting=('4405',))[i] for i in (_C_BU, _C_LAND)] == ['0', 'DE']


def test_a_stored_key_on_the_chain_keeps_the_land_and_follows_the_key_for_the_rate():
    fields = _one(_row(1, DEBTOR, WAIT, 119.0, 0.19, 'transfer', 101))
    assert (fields[_C_BU], fields[_C_RATE], fields[_C_LAND]) == ('101', '19,00', 'DE')
    fields = _one(_row(2, DEBTOR, WAIT, 119.0, 0.19, 'transfer', 490))
    assert (fields[_C_BU], fields[_C_RATE], fields[_C_LAND]) == ('490', '', 'DE')


# ── Spalte 114: das Leistungsdatum nur an der Forderung ──────────────────────

def test_the_service_date_stands_only_at_the_receivable():
    forderung, zahlung, umbuchung, einzelsatz = _chain_lines('R1')
    assert forderung[_C_LEISTUNG] == '20062026'                                   # DeliveryDate der Rechnung, DDMMYYYY
    assert (zahlung[_C_LEISTUNG], umbuchung[_C_LEISTUNG], einzelsatz[_C_LEISTUNG]) == ('', '', '')


@pytest.mark.parametrize('doc, expected', [('R1', '20062026'), ('R2', '02052026'), ('R3', '01072026'),
                                           ('R4', '01072026'), ('RX', '01072026'), ('', '01072026')])
def test_the_service_date_is_the_delivery_date_of_the_invoice_else_the_booking_date(doc, expected):
    """Ohne Rechnung zur Beleg-Nr. und bei leerem oder fehlendem Leistungsdatum gilt das Buchungsdatum."""
    assert _one(_row(1, DEBTOR, WAIT, 119.0, 0.19, 'transfer', doc=doc), invoices=INVOICES)[_C_LEISTUNG] == expected


@pytest.mark.parametrize('invoices', [None, {}, {'R1': None}, {'R1': ('2026-06-20',)}, {'R1': 'x'}, {'R1': (1, 2, 3)}])
def test_without_usable_invoice_data_the_booking_date_and_the_land_de_are_used(invoices):
    fields = _one(_row(1, DEBTOR, WAIT, 119.0, 0.19, 'transfer', doc='R1'), invoices=invoices)
    assert (fields[_C_LEISTUNG], fields[_C_LAND]) == ('01072026', 'DE')


def test_the_counter_booking_of_a_credit_note_has_a_service_date_too():
    batch = _export([_row(1, DEBTOR, WAIT, -119.0, 0.19, 'transfer', doc='R1')], invoices=INVOICES)
    assert batch['R1'][_C_LEISTUNG] == '20062026'


@pytest.mark.parametrize('coa, counter, form', [
    (DEBTOR, REVENUE, 'transfer'), (WAIT, REVENUE, 'transfer'), (BANK, DEBTOR, 'liquid_first'),
    (DEBTOR, WAIT, 'purpose_first'), (DEBTOR, WAIT, 'liquid_first'), (DEBTOR, WAIT, 'mirror_coa'),
    (EXPENSE, BANK, 'purpose_first'), (DEBTOR, DEPRECIATION, 'transfer'), (WAIT, WAIT, 'transfer')])
def test_no_other_row_has_a_service_date(coa, counter, form):
    assert _one(_row(1, coa, counter, 119.0, 0.19, form, doc='R1'), invoices=INVOICES)[_C_LEISTUNG] == ''


def test_the_receivable_with_its_sides_swapped_has_the_service_date_too():
    """Debitor gegen Wartekonto, gleich auf welcher Seite (eine im Editor vertauschte Forderung)."""
    row = _row(1, WAIT, DEBTOR, 119.0, 0.19, 'transfer', doc='R1')
    assert _one(row, invoices=INVOICES)[_C_LEISTUNG] == '20062026'


def test_column_115_stays_empty_on_every_row():
    """WISO belegt „Datum Zuord. Steuerperiode“ nicht – auch nicht mit dem Datum des Exports."""
    rows = _chain('R1') + [_row(9, EXPENSE, BANK, -119.0, 0.19, 'purpose_first'),
                           _row(10, BANK, BANK_TWO, 5.0, None, 'mirror_coa')]
    batch = _export(rows, liquid={6, 9})
    assert len(batch) == 6 and all(fields[_C_STEUERDATUM] == '' for fields in batch.values())


# ── Spalte 119: das Land der Rechnungskette ──────────────────────────────────

@pytest.mark.parametrize('country, expected', [
    ('DE', 'DE'), ('de', 'DE'), ('D', 'DE'), ('d', 'DE'), ('Deutschland', 'DE'), ('DEUTSCHLAND', 'DE'), ('', 'DE'),
    (None, 'DE'), ('  ', 'DE'), ('FR', 'FR'), ('fr', 'FR'), (' at ', 'AT'), ('Ch', 'CH'),
    ('Frankreich', ''), ('FRA', ''), ('F', ''), ('1', ''), ('D1', ''), ('Ä', ''), ('ÄÖ', ''), ('D-', ''),
    ('DE";0', ''), ('DE;FR', ''), ('DE\n', 'DE'), ('Österreich', '')])
def test_the_land_of_the_chain_is_the_iso_code_of_the_invoice(country, expected):
    """Zwei Buchstaben → groß; leer, „D“ und „Deutschland“ → DE; alles andere → leer (nie ein roher Wert)."""
    fields = _one(_row(1, DEBTOR, WAIT, 119.0, 0.19, 'transfer', doc='R1'), invoices={'R1': ('2026-06-20', country)})
    assert fields[_C_LAND] == expected


def test_the_land_stands_on_the_receivable_and_the_transfer_but_not_on_the_payment():
    forderung, zahlung, umbuchung, einzelsatz = _chain_lines('R3')
    assert (forderung[_C_LAND], umbuchung[_C_LAND]) == ('FR', 'FR')
    assert (zahlung[_C_LAND], einzelsatz[_C_LAND]) == ('', '')


def test_a_row_without_an_invoice_gets_the_land_de():
    assert [fields[_C_LAND] for fields in _chain_lines('RX')][:3:2] == ['DE', 'DE']


def test_land_and_rate_of_other_rows_stay_empty():
    batch = _export([_row(1, EXPENSE, BANK, -119.0, 0.19, 'purpose_first'), _row(2, DEPRECIATION, ASSET, 600.0, None,
                                                                              'transfer')])
    assert all(fields[_C_LAND] == '' and fields[_C_RATE] == '' for fields in batch.values())


# ── Belegfeld 1 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('doc, expected', [
    ('R26003', 'R26003'), ('RE 2026 0001', 'RE20260001'), ('RE-2026 000 123 456', 'RE-2026000123456'),
    ('1234567890123', '1234567890123'), ('A,B_C', 'A,B_C'), ('', ''), (None, ''),
    ('x' * 36, 'x' * 36), ('y' * 40, 'y' * 36), ('Z' * 12 + ' ' + 'z' * 30, 'Z' * 12 + 'z' * 24)])
def test_belegfeld_1_has_no_blanks_is_not_cut_at_12_and_holds_up_to_36_characters(doc, expected):
    row = list(_row(1, EXPENSE, BANK, -1.0, None, 'purpose_first'))
    row[16] = doc
    csv_bytes, _ = datev.export_to_datev([row], NUMBERS, '2026-07-01', '2026-07-31')
    fields = next(_csv.reader([csv_bytes.decode('cp1252').split('\r\n')[2]], delimiter=';', quotechar='"'))
    assert fields[_C_BELEG] == expected and len(fields) == 124


def test_a_belegfeld_with_quotes_and_semicolons_stays_in_its_field():
    row = _row(1, EXPENSE, BANK, -1.0, None, 'purpose_first', doc='A";B\nC')
    csv_bytes, _ = datev.export_to_datev([row], NUMBERS, '2026-07-01', '2026-07-31')
    (line,) = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]
    fields = next(_csv.reader([line], delimiter=';', quotechar='"'))
    assert len(fields) == 124 and fields[_C_BELEG] == 'A";BC'


# ── Der Buchungstext ─────────────────────────────────────────────────────────

def _text_of(text):
    """(Feld 13 der Zeile, Stapel als Bytes) für eine Buchung mit diesem Buchungstext."""
    row = list(_row(1, EXPENSE, BANK, -1.0, None, 'purpose_first'))
    row[15] = text
    csv_bytes, _ = datev.export_to_datev([row], NUMBERS, '2026-07-01', '2026-07-31')
    line = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2]
    fields = next(_csv.reader([line], delimiter=';', quotechar='"'))
    assert len(fields) == 124
    return fields[_C_TEXT], csv_bytes


@pytest.mark.parametrize('text, expected', [
    ('Miete\r\nJuli', 'Miete  Juli'), ('a\nb', 'a b'), ('a\rb', 'a b'), ('a\tb', 'a b'), ('a\x00b', 'a b'),
    ('a\x0bb\x0cc', 'a b c'), ('a\x1fb\x7fc', 'a b c'), ('a\x85b', 'a b'), ('a\u2028b\u2029c', 'a b c'),
    ('\n', ' '), ('Straße; "Ä"', 'Straße; "Ä"'), ('', ''), (None, ''),
    ('x' * 59 + '\n' + 'Rest', 'x' * 59 + ' '), ('y' * 70, 'y' * 60), ('z' * 61 + '\r\n', 'z' * 60)])
def test_line_breaks_tabs_and_other_control_characters_in_the_booking_text_become_blanks(text, expected):
    """Ein Umbruch im Text bräche sonst die Zeile des Stapels: Jedes Steuerzeichen wird zu einem Leerzeichen, und zwar
    vor dem Kürzen auf 60 Zeichen."""
    field, csv_bytes = _text_of(text)
    assert field == expected
    assert csv_bytes.count(b'\r') == 3 and csv_bytes.count(b'\n') == 3          # Vorlauf, Überschrift und die eine Zeile


# ── Der Vorlauf ──────────────────────────────────────────────────────────────

def _header(**kwargs):
    csv_bytes, _ = datev.export_to_datev([_row(1, EXPENSE, BANK, -1.0, None, 'purpose_first')], NUMBERS,
                                         '2026-07-01', '2026-07-31', **kwargs)
    return csv_bytes.decode('cp1252').split('\r\n')[0].split(';')


def test_the_header_carries_the_numbers_of_the_own_company():
    fields = _header(consultant_no='1234567', client_no='12345')
    assert (fields[10], fields[11]) == ('1234567', '12345')
    assert fields[0:4] == ['"EXTF"', '700', '21', '"Buchungsstapel"'] and fields[13] == '4'
    assert (fields[14], fields[15]) == ('20260701', '20260731')


@pytest.mark.parametrize('kwargs', [
    {}, {'consultant_no': None, 'client_no': None}, {'consultant_no': '1234567'}, {'client_no': '12345'},
    {'consultant_no': '123', 'client_no': '12345'}, {'consultant_no': '12345678', 'client_no': '12345'},
    {'consultant_no': '1234567', 'client_no': '123456'}, {'consultant_no': '1234567', 'client_no': ''},
    {'consultant_no': '1234x67', 'client_no': '12345'}, {'consultant_no': '12345', 'client_no': '1 2'},
    {'consultant_no': '12345";0', 'client_no': '1'}, {'consultant_no': '12345\r\n', 'client_no': '1'},
    {'consultant_no': '12345', 'client_no': '1;2'}, {'consultant_no': -12345, 'client_no': 1},
    {'consultant_no': True, 'client_no': 1}, {'consultant_no': '1000', 'client_no': '1'},
    {'consultant_no': 1000, 'client_no': 1}, {'consultant_no': '0999', 'client_no': '1'},
    {'consultant_no': '01234', 'client_no': '1'}, {'consultant_no': '12345', 'client_no': '0'},
    {'consultant_no': '12345', 'client_no': '00001'}, {'consultant_no': '12345', 'client_no': '100000'},
    {'consultant_no': '10000000', 'client_no': '1'}, {'consultant_no': 0, 'client_no': 0}])
def test_without_two_valid_numbers_the_header_carries_the_placeholders(kwargs):
    fields = _header(**kwargs)
    assert (fields[10], fields[11]) == PLACEHOLDERS
    assert len(fields) == len(_header(consultant_no='12345', client_no='1'))      # nie ein zusätzliches Feld


def test_the_numbers_may_be_whole_numbers_too():
    assert tuple(_header(consultant_no=4711, client_no=7)[10:12]) == ('4711', '7')


@pytest.mark.parametrize('consultant, client', [('1001', '1'), ('9999999', '99999'), (1001, 1), (9999999, 99999)])
def test_the_ends_of_the_ranges_of_the_numbers_are_valid(consultant, client):
    assert tuple(_header(consultant_no=consultant, client_no=client)[10:12]) == (str(consultant), str(client))


def test_existing_calls_without_the_new_arguments_stay_valid():
    """Aufrufe wie vor 2.3.0: keine Wartekonten, keine Rechnungen, Platzhalter im Vorlauf."""
    csv_bytes, exported = datev.export_to_datev([_row(1, EXPENSE, BANK, -119.0, 0.19, 'purpose_first')], NUMBERS,
                                                '2026-07-01', '2026-07-31', {6})
    header, rows = _parse(csv_bytes)
    assert exported == [1] and tuple(header.split(';')[10:12]) == PLACEHOLDERS and rows['B1'][_C_BU] == '401'


# ── Ganze Zeilen ─────────────────────────────────────────────────────────────

def test_the_columns_that_stay_the_same_for_every_row():
    fields = _one(_row(1, EXPENSE, BANK, -119.0, 0.19, 'purpose_first', date='2026-07-03'))
    assert fields[0] == '119,00' and fields[2] == 'EUR' and fields[_C_KONTO] == '6815'
    assert fields[_C_GEGENKONTO] == '1800' and fields[9] == '0307' and fields[13] == 'Satz 1'
    assert [fields[i] for i in (4, 12, 14, 18, 105, 113)] == ['0,00', '0,00', '0', '0', '0', '0']


# ── Über den Handler (die Datenbank) ─────────────────────────────────────────

@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user('anna')
    world = build_world(tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return world


def _export_via_handler(db, date_from='2026-01-01', date_to='2026-12-31'):
    """(Vorlauf als Felder, alle Zeilen des Stapels als Felder – auch mehrere mit derselben Beleg-Nr.)."""
    csv_bytes, _name = handlers.handle_datev_export(db, {'date_from': [date_from], 'date_to': [date_to]})
    assert isinstance(csv_bytes, bytes), csv_bytes
    lines = [line for line in csv_bytes.decode('cp1252').split('\r\n') if line]
    rows = [next(_csv.reader([line], delimiter=';', quotechar='"')) for line in lines[2:]]
    assert all(len(fields) == 124 for fields in rows)
    return lines[0].split(';'), rows


def _by_accounts(rows):
    return {(fields[_C_KONTO], fields[_C_GEGENKONTO]): fields for fields in rows}


def _sent_invoice(world, rate=19, number='T-100', delivery='2026-06-05', **over):
    items = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung', 'unitPrice': 100.0,
              'taxRate': rate}]
    invoice_id = make_invoice(world, 'draft', number=number, items=items, taxRate=rate / 100,
                              deliveryDate=delivery, **over)
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    return invoice_id


@pytest.mark.parametrize('rate', [19, 7])
def test_a_sent_and_cancelled_invoice_has_the_receivable_and_the_counter_booking_with_the_same_key_and_rate(world, rate):
    """Über den echten Weg – versenden, stornieren, Export über den Handler: Forderung und Gegenbuchung der
    Stornorechnung stehen ohne BU-Schlüssel mit dem Satz in Spalte 40 und entgegengesetztem Soll/Haben im Stapel."""
    invoice_id = _sent_invoice(world, rate)
    storno = world.db.cancel_invoice(invoice_id, '2026-07-01', 'anna')
    assert storno.ok and storno.counter_booked, storno

    _header_fields, lines = _export_via_handler(world.db)

    rows = {fields[_C_BELEG]: fields for fields in lines}
    assert set(rows) == {'T-100', storno.storno_number}
    receivable, counter = rows['T-100'], rows[storno.storno_number]
    assert (receivable[_C_KONTO], counter[_C_KONTO]) == ('10000', '10000')
    gross = {19: '119,00', 7: '107,00'}[rate]
    assert receivable[0] == counter[0] == gross
    assert (receivable[_C_SH], counter[_C_SH]) == ('S', 'H')
    assert receivable[_C_BU] == counter[_C_BU] == '0'
    assert receivable[_C_RATE] == counter[_C_RATE] == f'{rate},00'
    assert receivable[_C_LAND] == counter[_C_LAND] == 'DE'
    # Leistungsdatum: das der Rechnung; die Stornorechnung trägt das ihrer Ursprungsrechnung
    assert receivable[_C_LEISTUNG] == counter[_C_LEISTUNG] == '05062026'
    assert receivable[_C_STEUERDATUM] == counter[_C_STEUERDATUM] == ''


def test_the_whole_chain_of_a_paid_invoice_through_the_handler(world):
    """Rechnung versenden, Zahlung zuordnen: Forderung, „Bank an Debitoren“ und „Wartekonto an Erlöskonto“ mit BU,
    Spalte 40, 114 und 119 – Leistungsdatum der Rechnung, nicht das Buchungsdatum."""
    db = world.db
    invoice_id = _sent_invoice(world, 19, delivery='2026-05-20')
    bank = db.insert_booking('2026-07-01', 119.0, booking_type='bank', account_id=world.bank)
    assert db.assign_invoice_payment(invoice_id, bank).ok

    _header_fields, lines = _export_via_handler(db)

    assert len(lines) == 3 and {fields[_C_BELEG] for fields in lines} == {'T-100'}
    rows = _by_accounts(lines)
    forderung, zahlung, umbuchung = rows[('10000', '4405')], rows[('1800', '10000')], rows[('4405', '4400')]
    assert [forderung[i] for i in (_C_BU, _C_RATE, _C_LEISTUNG, _C_LAND)] == ['0', '19,00', '20052026', 'DE']
    assert [zahlung[i] for i in (_C_BU, _C_RATE, _C_LEISTUNG, _C_LAND)] == ['0', '', '', '']
    assert [umbuchung[i] for i in (_C_BU, _C_RATE, _C_LEISTUNG, _C_LAND)] == ['0', '19,00', '', 'DE']
    assert forderung[9] == '0706' and zahlung[9] == umbuchung[9] == '0107'        # Belegdatum DDMM unverändert


def test_a_payment_without_a_receivable_is_one_row_with_the_vat_key_and_the_rate(world):
    """16 %: kein Wartekonto, keine Forderung – „Erlöskonto an Bank“ trägt den Umsatzsteuer-Schlüssel (16 % = 5) und den
    Satz; Leistungsdatum und Land gehören nur der Rechnungskette."""
    db = world.db
    invoice_id = _sent_invoice(world, 16)
    bank = db.insert_booking('2026-07-01', 116.0, booking_type='bank', account_id=world.bank)
    assert db.assign_invoice_payment(invoice_id, bank, revenue_coa_id=db.get_coa_id_by_account_number(4300)).ok

    _header_fields, lines = _export_via_handler(db)

    (fields,) = lines
    assert (fields[_C_KONTO], fields[_C_GEGENKONTO], fields[_C_SH]) == ('4300', '1800', 'H')
    assert [fields[i] for i in (_C_BU, _C_RATE, _C_LEISTUNG, _C_LAND)] == ['5', '16,00', '', '']


def test_a_key_stored_on_the_booking_goes_through_the_handler_into_the_batch_as_it_is(world):
    """Der in der Datenbank gespeicherte Schlüssel steht im Stapel, wie er ist – auch 490 ohne Satz, wo der Export sonst
    401 ableiten würde; die Buchung daneben ohne Schlüssel trägt den abgeleiteten. Der Export ändert ihn nicht."""
    db = world.db
    expense, bank = db.get_coa_id_by_account_number(6815), db.get_coa_id_by_account_number(1800)
    plain = db.insert_booking('2026-07-02', -119.0, coa_id=expense, counter_coa_id=bank, tax_rate=0.19,
                              document_number='E-1', entry_form='purpose_first')
    keyed = db.insert_booking('2026-07-03', -119.0, coa_id=expense, counter_coa_id=bank, tax_rate=0.19,
                              document_number='E-2', entry_form='purpose_first')
    execute(db, 'UPDATE Bookings SET DatevKey = ? WHERE ID = ?', (490, keyed))

    _header_fields, lines = _export_via_handler(db)

    rows = {fields[_C_BELEG]: fields for fields in lines}
    assert (rows['E-1'][_C_BU], rows['E-2'][_C_BU]) == ('401', '490')
    assert (rows['E-1'][_C_RATE], rows['E-2'][_C_RATE]) == ('', '')               # beides keine Umsatzsteuer-Schlüssel
    assert query(db, 'SELECT ID, DatevKey FROM Bookings WHERE ID IN (?, ?) ORDER BY ID', (plain, keyed)) == [
        (plain, None), (keyed, 490)]


def test_the_handler_passes_the_waiting_accounts_the_invoices_and_the_numbers(world, monkeypatch):
    seen = {}
    real = datev.export_to_datev

    def spy(bookings, coa_map, date_from, date_to, liquid_coa_ids=None, **kwargs):
        seen.update(kwargs)
        return real(bookings, coa_map, date_from, date_to, liquid_coa_ids, **kwargs)

    monkeypatch.setattr(datev, 'export_to_datev', spy)
    _sent_invoice(world, 19)
    own = world.db.get_contact_by_id(world.own)
    world.db.update_contact(world.own, contact_type='own', entity_type='company', display_name=own[3],
                            company_name=own[4], datev_consultant_no='4711', datev_client_no='12')

    header, _lines = _export_via_handler(world.db)

    assert set(seen['waiting_numbers']) >= {4345, 4405}
    assert seen['invoices']['T-100'] == ('2026-06-05', 'DE')
    assert (seen['consultant_no'], seen['client_no']) == ('4711', '12') and (header[10], header[11]) == ('4711', '12')


def test_without_numbers_the_handler_exports_the_placeholders(world):
    _sent_invoice(world, 19)
    header, _lines = _export_via_handler(world.db)
    assert (header[10], header[11]) == PLACEHOLDERS


@pytest.mark.parametrize('consultant, client', [('0999', '12'), ('1000', '12'), ('4711', '0'), ('4711', '012'),
                                                 ('12', '12'), ('4711', '100000'), ('47 11', '12')])
def test_numbers_that_are_stored_invalid_give_the_placeholders_through_the_handler(world, consultant, client):
    """Ein per SQL-Konsole gesetzter Wert außerhalb der Bereiche (oder mit führender Null) gilt als nicht eingetragen: Der
    Vorlauf trägt die Platzhalter, nie den rohen Wert."""
    _sent_invoice(world, 19)
    execute(world.db, 'UPDATE CompanyDetails SET DatevConsultantNo = ?, DatevClientNo = ? WHERE ContactID = ?',
            (consultant, client, world.own))

    header, _lines = _export_via_handler(world.db)

    assert (header[10], header[11]) == PLACEHOLDERS and world.db.get_datev_numbers() == (None, None)


def test_the_invoice_data_come_from_one_query_over_all_invoices(world):
    """Auch eine Rechnung außerhalb des Exportzeitraums liefert Leistungsdatum und Land (die Zahlung steht im
    späteren Zeitraum); ein Angebot zählt nicht."""
    _sent_invoice(world, 19, number='T-OLD', delivery='2025-03-03', invoiceDate='2025-03-04')
    add_document(world.db, 'A-1', 'quote', world.own, world.cust)
    add_document(world.db, 'T-NEU', 'invoice', world.own, world.cust, delivery_date=None, buyer_country='FR')

    data = world.db.get_datev_invoice_data()

    assert data['T-OLD'] == ('2025-03-03', 'DE') and data['T-NEU'] == (None, 'FR')
    assert 'A-1' not in data


def test_the_waiting_account_numbers_are_the_accounts_of_the_open_items_that_the_chart_holds(world):
    assert world.db.get_waiting_account_numbers() == sorted(
        number for number in (4340, 4345, 4405)
        if world.db.get_coa_id_by_account_number(number) is not None)
    assert {4345, 4405} <= set(world.db.get_waiting_account_numbers())
