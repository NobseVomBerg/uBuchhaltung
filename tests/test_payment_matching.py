# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zahlungsvorschläge, 2.8.1: die reine Rangfolge ``db/payment_matching.py`` – Zahlungseingang ↔ offene Rechnung.

Rein: kein Datenbankzugriff, keine Uhr. Geprüft wird

* jede Regel einzeln: die Rechnungsnummer im Text (die vier Schreibweisen aus der Praxis, Trennzeichen, Zeilenumbruch, Groß-/Kleinschreibung, die
  Ziffernsperre an dem Rand, an dem die Nummer mit einer Ziffer beginnt oder endet, zu kurze und zu lange Nummern), Betrag und Kunde, die Regel
  „NUMMER oder (BETRAG und KUNDE)“ – ein gleicher Betrag allein ist nie ein Vorschlag –, Punkte und Begründungen in fester Reihenfolge, die
  Datumsregel (mehr als 7 Tage vor dem Rechnungsdatum nur mit Nummer, ein unlesbares Datum steht hinten);
* die Rangfolge (``rank_invoices``, ``rank_movements``: höchstens fünf, nach Punkten, Abstand der Daten, ID) und die Eins-zu-eins-Liste
  (``pair_up``: jede Bewegung und jede Rechnung höchstens einmal, zwei gleich hohe Rechnungen und Zahlungen eines Kunden paaren sich nach dem
  Datum, ``ambiguous`` bei gleicher Punktzahl, unabhängig von der Reihenfolge der Eingabe, ``linked`` schließt Paare aus);
* kaputte Werte (kein Fehler, die Zeile ist kein Kandidat) und die Laufzeit bei langem Text.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; Daten stehen fest im Test (kein „heute“).
"""
import datetime
import itertools
import random
import time

import pytest

from db import payment_matching
from db.payment_matching import (
    AMBIGUOUS_TEXT, DAYS_BEFORE_INVOICE, MAX_NUMBER_LENGTH, MAX_SUGGESTIONS, POINTS_AMOUNT, POINTS_CUSTOMER, POINTS_NUMBER, REASON_AMOUNT,
    REASON_CUSTOMER, REASON_NUMBER, REASON_PARTIAL, REASON_SEPARATOR, PaymentSuggestion, pair_up, rank_invoices, rank_movements, reason_text)
from db.receipt_matching import MAX_FIELD_LENGTH

DAY = '2026-03-04'                          # das Rechnungsdatum der Tests
OPEN = 1190000                              # 119,00 EUR in Minor Units (money.SCALE = 4)


# ── Hilfen ───────────────────────────────────────────────────────────────────

def shifted(days, start=DAY):
    """Das Datum ``days`` Tage nach (negativ: vor) ``start`` als ISO-Text."""
    return (datetime.date.fromisoformat(start) + datetime.timedelta(days=days)).isoformat()


def movement(ident=1, amount=OPEN, date=DAY, text='', recipient='', number='', contact=None):
    """Eine Bewegung (Zahlungseingang mit freiem Rest ``amount``), ohne weitere Merkmale."""
    return {'id': ident, 'date': date, 'amount_minor': amount, 'recipient': recipient, 'text': text, 'document_number': number,
            'contact_id': contact}


def invoice(ident=1, number='R26001', open_minor=OPEN, date=DAY, customer=None, name=''):
    """Eine offene Rechnung, ohne weitere Merkmale."""
    return {'id': ident, 'number': number, 'date': date, 'open_minor': open_minor, 'customer_id': customer, 'customer_name': name}


def found_by_text(number, text):
    """Findet ``rank_invoices`` die Rechnung mit dieser Nummer in diesem Text? Der Betrag passt bewusst nicht, es gibt keinen Kunden: Ein Vorschlag
    entsteht allein durch die Nummer."""
    return bool(rank_invoices(movement(amount=1, text=text), [invoice(number=number)]))


def pair_ids(suggestions):
    return [(s.movement_id, s.invoice_id) for s in suggestions]


def stable_id(value):
    """Die ID eines Testfalls: ``repr`` – nur ein ``object()`` bekäme seine Adresse hinein, und die ist in jedem xdist-Prozess eine andere."""
    return '<object>' if type(value) is object else repr(value)


# ── Die festgelegten Werte ───────────────────────────────────────────────────

def test_the_constants_and_the_fixed_sentences_are_the_planned_ones():
    assert (MAX_SUGGESTIONS, DAYS_BEFORE_INVOICE, POINTS_NUMBER, POINTS_AMOUNT, POINTS_CUSTOMER) == (5, 7, 100, 50, 30)
    assert REASON_NUMBER == 'Rechnungsnummer im Verwendungszweck'
    assert REASON_AMOUNT == 'Betrag stimmt'
    assert REASON_CUSTOMER == 'Kunde passt'
    assert REASON_PARTIAL == 'Betrag weicht ab (Teil- oder Überzahlung)'
    assert AMBIGUOUS_TEXT == 'mehrere möglich – bitte prüfen'
    assert REASON_SEPARATOR == ' · '


def test_the_module_is_pure_no_database_and_no_clock():
    with open(payment_matching.__file__, encoding='utf-8') as handle:
        source = handle.read()
    for forbidden in ('sqlite3', 'userctx', 'import server', 'from server', 'datetime.date.today', 'datetime.datetime.now', 'time.time('):
        assert forbidden not in source, forbidden


# ── NUMMER: die Rechnungsnummer im Text ──────────────────────────────────────

@pytest.mark.parametrize('number, text', [
    ('R26001', 'Rechnungs-Nr R26001'),
    ('R26002', 'RE-Nr. R26002.KD-Nr. K-0001'),
    ('R26003', 'Re.Nr. R26003'),
    ('R26004', 'Rechnungs-Nr. R26004'),
], ids=['Rechnungs-Nr', 'RE-Nr.+KD-Nr.', 'Re.Nr.', 'Rechnungs-Nr.'])
def test_the_spellings_from_practice_find_their_invoice(number, text):
    found = rank_invoices(movement(amount=1, text=text), [invoice(number=number)])
    assert [(s.movement_id, s.invoice_id, s.reasons, s.score) for s in found] == [(1, 1, (REASON_NUMBER, REASON_PARTIAL), POINTS_NUMBER)]


HITS = [
    ('R26001', 'R26001'), ('R26001', 'r26001'), ('R26001', 'RECHNUNG R26001 VOM 12.11.2026'), ('R26001', '(R26001)'),
    # höchstens EIN Trennzeichen zwischen zwei Zeichen der Nummer: Leerraum, Bindestrich, Unterstrich, Schrägstrich, Punkt
    ('R26003', 'R 26003'), ('R26003', 'R-26003'), ('R26003', 'R_26003'), ('R26003', 'R/26003'), ('R26003', 'R.26003'),
    ('R26001', 'R26 001'), ('R26001', 'R2-6-0-0-1'), ('R26001', 'R260\n01'), ('R26001', 'R26 001'),
    # ein Buchstabe oder Satzzeichen davor und dahinter stört nicht, ein Datum hinter der Nummer auch nicht
    ('R26003', 'RGNR26003'), ('R26003', 'RE-Nr.R26003.KD-Nr.'), ('R26003', 'R26003 12.11.2026'), ('R26001', 'R26001A'), ('R26001', 'ER26001'),
    # beginnt die Nummer mit einem Buchstaben, bleibt der Rand davor offen (auch eine Ziffer davor stört nicht): ``1R26001`` trifft ``R26001``
    ('R26001', '1R26001'), ('R26001', 'Kd 12R26001'),
    # eine Nummer mit eigenen Trennzeichen: nur ihre Zeichen zählen
    ('RE-2026/0042', 'RE20260042'), ('RE-2026/0042', 're 2026 0042 bitte'), ('RE-2026/0042', 'RE-2026-0042'), ('RE-2026/0042', 'Rechnung RE_2026_0042'),
    ('20260042', '2026-0042'), ('20260042', 'Rechnung 20260042-01'), ('20260042', 'Nr.20260042'), ('20260042', 'NR20260042'),
    ('26R001', 'Zahlung 26R001'), ('26R001', 'RG26R001'), ('26R001', '26R001/2026'),
    ('R2026', 'R2026'),                                      # fünf Zeichen mit einer Ziffer: brauchbar
]
MISSES = [
    # ein Stück einer längeren Ziffernfolge: an dem Rand, an dem die Nummer mit einer Ziffer endet bzw. beginnt, schließt keine Ziffer an
    ('R26001', 'R260010'), ('R26001', 'R2600100'), ('R26001', 'R26 0010'),
    ('26R001', '26R0010'), ('26R001', '126R001'), ('20260042', '120260042'), ('20260042', '202600421'),
    # mehr als ein Trennzeichen oder ein anderes Zeichen zwischen den Zeichen der Nummer
    ('R26003', 'R  26003'), ('R26003', 'R--26003'), ('R26003', 'R - 26003'), ('R26003', 'R260  03'), ('R26003', 'R,26003'), ('R26003', 'R:26003'),
    ('R26003', 'R+26003'), ('R26003', 'R#26003'), ('R26003', 'R2ä6003'),
    # nicht die Nummer
    ('R26001', 'R26002'), ('R26001', 'R2600'), ('R26001', 'R'), ('R26001', ''), ('R26001', '26001'), ('R26001', 'Rechnung Nr. 26001'),
    ('R26001', 'R２６００１'),                                # Ziffern anderer Schriften sind keine
    # nicht brauchbar als Beweis: zu kurz (normalisiert unter 4 Zeichen, rein numerisch unter 5) oder ohne Ziffer
    ('R2', 'R2'), ('R26', 'Rechnung R26 bezahlt'), ('2026', 'Zahlung 2026'), ('2026', 'Rechnung 2026'), ('RECHNUNG', 'Rechnung'), ('', 'Rechnung'),
    ('---', '---'), (None, 'Rechnung R26001'),
]


@pytest.mark.parametrize('number, text', HITS)
def test_the_number_is_found(number, text):
    assert found_by_text(number, text)


@pytest.mark.parametrize('number, text', MISSES)
def test_the_number_is_not_found(number, text):
    assert not found_by_text(number, text)


def test_the_number_may_stand_in_the_text_the_document_number_or_the_recipient():
    for field in ('text', 'number', 'recipient'):
        found = rank_invoices(movement(amount=1, **{field: 'Zahlung R26001'}), [invoice()])
        assert [s.invoice_id for s in found] == [1], field


def test_the_parts_of_a_number_in_two_fields_are_not_the_number():
    assert not rank_invoices(movement(amount=1, text='R260', number='01'), [invoice()])
    assert not rank_invoices(movement(amount=1, text='Zahlung R260', recipient='01 Käufer'), [invoice()])


def test_the_digit_rule_holds_at_the_edge_where_the_number_itself_has_a_digit():
    """Die Ziffernsperre gilt an dem Rand, an dem die Nummer mit einer Ziffer beginnt bzw. endet; mit einem Buchstaben an einem Rand ist dieser offen."""
    assert not found_by_text('26001', '126001') and not found_by_text('26001', '260011')
    assert found_by_text('26001', 'x26001y') and found_by_text('26001', '26001.') and found_by_text('26001', '.26001')
    assert found_by_text('26R001', 'a26R001b') and not found_by_text('26R001', '126R001') and not found_by_text('26R001', '26R0011')
    assert found_by_text('26R001A', '26R001A5') and not found_by_text('26R001A', '126R001A')      # endet mit einem Buchstaben: hinten offen
    assert found_by_text('R26001', '9R26001') and not found_by_text('R26001', 'R260019')          # beginnt mit einem: vorn offen, hinten gesperrt


def test_a_number_longer_than_the_limit_is_no_number():
    fits = 'R' + '1' * (MAX_NUMBER_LENGTH - 1)
    too_long = 'R' + '1' * MAX_NUMBER_LENGTH
    assert found_by_text(fits, f'Zahlung {fits}') and not found_by_text(too_long, f'Zahlung {too_long}')


def test_a_number_behind_the_cap_of_a_field_is_not_read():
    inside = 'x' * (MAX_FIELD_LENGTH - 7) + ' R26001'                     # endet genau an der Grenze
    behind = 'x' * (MAX_FIELD_LENGTH - 3) + ' R26001'                     # die Nummer reicht über die Grenze hinaus
    assert len(inside) == MAX_FIELD_LENGTH and found_by_text('R26001', inside) and not found_by_text('R26001', behind)


def test_the_number_of_the_invoice_is_read_as_it_stands_in_the_book():
    """Die Nummer an der Rechnung darf selbst Leerraum, Trenner und Großschreibung tragen."""
    assert found_by_text('  re-2026/0042 ', 'Zahlung RE20260042')
    assert found_by_text('RE 2026 0042', 'Zahlung re-2026-0042')


# ── BETRAG und KUNDE ─────────────────────────────────────────────────────────

def test_an_equal_amount_alone_is_no_suggestion():
    assert rank_invoices(movement(), [invoice()]) == []
    assert rank_invoices(movement(contact=7), [invoice(customer=8)]) == []           # ein anderer Kunde
    assert rank_invoices(movement(recipient='Müller'), [invoice(name='Meier GmbH')]) == []
    assert rank_movements(invoice(), [movement()]) == []


def test_a_customer_alone_is_no_suggestion_either():
    assert rank_invoices(movement(amount=1, contact=7), [invoice(customer=7)]) == []
    assert rank_invoices(movement(amount=1, recipient='Käufer AG'), [invoice(name='Käufer AG')]) == []


def test_amount_and_customer_by_the_contact_is_a_suggestion():
    found = rank_invoices(movement(contact=7), [invoice(customer=7)])
    assert [(s.reasons, s.score) for s in found] == [((REASON_AMOUNT, REASON_CUSTOMER), POINTS_AMOUNT + POINTS_CUSTOMER)]


def test_amount_and_customer_by_a_shared_name_word_is_a_suggestion():
    found = rank_invoices(movement(recipient='Käufer Handels GmbH'), [invoice(name='Handels-Käufer AG')])
    assert [s.reasons for s in found] == [(REASON_AMOUNT, REASON_CUSTOMER)]
    assert [s.reasons for s in rank_invoices(movement(recipient='Abc Handel'), [invoice(name='Xyz Handel e. K.')])] == [(REASON_AMOUNT, REASON_CUSTOMER)]


def test_a_missing_contact_is_never_the_same_customer_as_a_missing_customer():
    assert rank_invoices(movement(contact=None), [invoice(customer=None)]) == []
    assert rank_invoices(movement(contact=0), [invoice(customer=None)]) == []


@pytest.mark.parametrize('recipient, name', [
    ('Rechnung Zahlung GmbH', 'Müller Rechnung GmbH'),                  # nur Wörter aus ``STOP_WORDS``
    ('Abc AG', 'Abc AG'),                                               # unter vier Zeichen zählt kein Wort
    ('', 'Käufer AG'), ('Käufer AG', ''), ('12345 6789', '12345 6789'),  # nichts, oder nur Zahlen
], ids=['Stoppwörter', 'kurz', 'leer-links', 'leer-rechts', 'Zahlen'])
def test_stop_words_short_words_numbers_and_empty_names_are_no_customer(recipient, name):
    assert rank_invoices(movement(recipient=recipient), [invoice(name=name)]) == []


def test_the_name_words_come_from_the_recipient_not_from_the_text():
    assert rank_invoices(movement(text='Käufer AG', recipient=''), [invoice(name='Käufer AG')]) == []


@pytest.mark.parametrize('kwargs, reasons, score', [
    ({'text': 'R26001', 'contact': 7}, (REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER), 180),
    ({'text': 'R26001'}, (REASON_NUMBER, REASON_AMOUNT), 150),
    ({'text': 'R26001', 'amount': 500, 'contact': 7}, (REASON_NUMBER, REASON_CUSTOMER, REASON_PARTIAL), 130),
    ({'text': 'R26001', 'amount': 500}, (REASON_NUMBER, REASON_PARTIAL), 100),
    ({'contact': 7}, (REASON_AMOUNT, REASON_CUSTOMER), 80),
], ids=['alles', 'Nummer+Betrag', 'Nummer+Kunde', 'nur Nummer', 'Betrag+Kunde'])
def test_points_and_reasons_come_in_a_fixed_order(kwargs, reasons, score):
    found = rank_invoices(movement(**kwargs), [invoice(customer=7)])
    assert [(s.reasons, s.score) for s in found] == [(reasons, score)]


def test_a_number_with_another_amount_names_the_partial_payment_a_number_with_the_amount_does_not():
    assert REASON_PARTIAL in rank_invoices(movement(amount=500, text='R26001'), [invoice()])[0].reasons
    assert REASON_PARTIAL not in rank_invoices(movement(text='R26001'), [invoice()])[0].reasons
    assert REASON_PARTIAL not in rank_invoices(movement(contact=7), [invoice(customer=7)])[0].reasons


def test_the_amount_is_compared_to_the_minor_value():
    assert rank_invoices(movement(amount=OPEN + 1, contact=7), [invoice(customer=7)]) == []
    assert rank_invoices(movement(amount=OPEN - 1, contact=7), [invoice(customer=7)]) == []


# ── DATUM ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('days, expected', [(-30, False), (-8, False), (-7, True), (-1, True), (0, True), (3, True), (400, True)])
def test_a_payment_more_than_seven_days_before_the_invoice_is_no_suggestion_without_the_number(days, expected):
    found = rank_invoices(movement(date=shifted(days), contact=7), [invoice(customer=7)])
    assert bool(found) is expected
    assert (found[0].day_distance if found else days) == days


@pytest.mark.parametrize('days', [-8, -60, -365])
def test_a_payment_long_before_the_invoice_is_still_a_suggestion_with_the_number(days):
    found = rank_invoices(movement(date=shifted(days), text='Vorauszahlung R26001', contact=7), [invoice(customer=7)])
    assert [(s.reasons, s.day_distance) for s in found] == [((REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER), days)]


@pytest.mark.parametrize('bad', [None, '', 'gestern', '2026-13-40', '2026-02-30', '04.03.2026', 20260304, 4.5, b'2026-03-04', ['2026-03-04']], ids=stable_id)
def test_an_unreadable_date_is_no_error_and_stands_last(bad):
    near = movement(ident=1, date=shifted(2), contact=7)
    broken = movement(ident=2, date=bad, contact=7)
    found = rank_invoices(broken, [invoice(customer=7)])
    assert [(s.movement_id, s.day_distance) for s in found] == [(2, None)]           # kein Datum: die Datumsregel gilt nicht, der Vorschlag bleibt
    found = rank_movements(invoice(customer=7), [broken, near])
    assert [s.movement_id for s in found] == [1, 2]                                    # … und steht hinter dem mit lesbarem Datum
    found = rank_movements(invoice(customer=7, date=bad), [broken, near])
    assert [(s.movement_id, s.day_distance) for s in found] == [(1, None), (2, None)]  # beide ohne Abstand: nach der ID


# ── Die Rangfolge ────────────────────────────────────────────────────────────

def test_more_points_come_first_then_the_nearer_date_then_the_id():
    invoices = [
        invoice(ident=2, number='K-7777', date=shifted(-5), customer=7),                              # Betrag + Kunde (80), 5 Tage Abstand
        invoice(ident=4, number='K-9999', date=shifted(-2), customer=7),                              # Betrag + Kunde (80), 2 Tage
        invoice(ident=3, number='K-8888', date=shifted(-2), customer=7),                              # dasselbe, kleinere ID
        invoice(ident=6, number='K-6666', date=shifted(-1), customer=7),                              # Betrag + Kunde (80), 1 Tag
        invoice(ident=9, number='R26001', date=shifted(-40), customer=7, open_minor=1),               # Nummer + Kunde (130): 40 Tage, aber mehr Punkte
    ]
    found = rank_invoices(movement(text='Danke R26001', contact=7), invoices)
    assert [(s.invoice_id, s.score) for s in found] == [(9, 130), (6, 80), (3, 80), (4, 80), (2, 80)]       # Abstand, nicht die ID, bestimmt die Reihenfolge


def test_rank_movements_orders_by_points_then_date_then_the_id_of_the_movement():
    movements = [
        movement(ident=3, date=shifted(5), contact=7), movement(ident=5, date=shifted(2), contact=7), movement(ident=4, date=shifted(2), contact=7),
        movement(ident=6, date=shifted(1), contact=7), movement(ident=8, date=shifted(30), amount=1, text='R26001'),
    ]
    found = rank_movements(invoice(customer=7), movements)
    assert [(s.movement_id, s.score) for s in found] == [(8, 100), (6, 80), (4, 80), (5, 80), (3, 80)]


def test_at_most_five_suggestions_come_back():
    invoices = [invoice(ident=n, number=f'K-{n}000', customer=7) for n in range(1, 9)]
    found = rank_invoices(movement(contact=7), invoices)
    assert [s.invoice_id for s in found] == [1, 2, 3, 4, 5] and len(found) == MAX_SUGGESTIONS
    movements = [movement(ident=n, contact=7) for n in range(1, 9)]
    assert [s.movement_id for s in rank_movements(invoice(customer=7), movements)] == [1, 2, 3, 4, 5]


def test_the_best_five_are_chosen_by_the_ranking_not_by_the_order_of_the_input():
    invoices = [invoice(ident=n, number=f'K-{n}000', customer=7, date=shifted(n - 9)) for n in range(1, 9)]      # die ID 8 liegt dem Datum am nächsten
    random.Random(7).shuffle(invoices)
    assert [s.invoice_id for s in rank_invoices(movement(contact=7), invoices)] == [8, 7, 6, 5, 4]


def test_the_distance_counts_in_both_directions():
    before = invoice(ident=3, number='K-3000', date=shifted(-3), customer=7)
    after = invoice(ident=4, number='K-4000', date=shifted(+3), customer=7)
    farther = invoice(ident=1, number='K-1000', date=shifted(+4), customer=7)
    found = rank_invoices(movement(contact=7), [farther, after, before])
    assert [(s.invoice_id, s.day_distance) for s in found] == [(3, 3), (4, -3), (1, -4)]        # Buchungsdatum minus Rechnungsdatum; gleich weit: die ID


def test_a_pair_that_is_already_linked_is_never_a_suggestion():
    invoices = [invoice(ident=1), invoice(ident=2, number='R26002')]
    found = rank_invoices(movement(amount=1, text='R26001 und R26002'), invoices, linked=frozenset({(1, 1)}))
    assert [s.invoice_id for s in found] == [2]
    found = rank_movements(invoice(), [movement(ident=1, amount=1, text='R26001'), movement(ident=2, amount=1, text='R26001')],
                           linked=frozenset({(1, 1)}))
    assert [s.movement_id for s in found] == [2]


# ── pair_up: die Eins-zu-eins-Liste ──────────────────────────────────────────

def two_and_two():
    """Ein Kunde, zwei gleich hohe Rechnungen (1. Januar, 1. Februar) und zwei gleich hohe Zahlungen (10. Januar, 10. Februar), ohne Nummer im Text."""
    invoices = [invoice(ident=1, number='K-1001', date='2026-01-01', customer=7), invoice(ident=2, number='K-1002', date='2026-02-01', customer=7)]
    movements = [movement(ident=1, date='2026-01-10', contact=7), movement(ident=2, date='2026-02-10', contact=7)]
    return movements, invoices


def test_two_equal_invoices_and_two_equal_payments_pair_by_the_date():
    movements, invoices = two_and_two()
    result = pair_up(movements, invoices)
    assert pair_ids(result) == [(1, 1), (2, 2)]                                         # jede Rechnung bekommt die zeitlich nächste Zahlung
    assert [s.ambiguous for s in result] == [True, True]                                # … aber es wären mehrere möglich gewesen


def test_the_pairing_does_not_depend_on_the_order_of_the_input():
    movements, invoices = two_and_two()
    movements.append(movement(ident=3, date='2026-03-20', contact=7))
    invoices.append(invoice(ident=3, number='K-1003', date='2026-03-01', customer=7))
    expected = pair_up(movements, invoices)
    assert pair_ids(expected) == [(1, 1), (2, 2), (3, 3)]
    for permuted_movements in itertools.permutations(movements):
        for permuted_invoices in itertools.permutations(invoices):
            assert pair_up(list(permuted_movements), list(permuted_invoices)) == expected


def test_every_movement_and_every_invoice_is_used_at_most_once():
    invoices = [invoice(ident=n, number=f'K-{n}000', customer=7, date=shifted(-n)) for n in range(1, 6)]
    movements = [movement(ident=n, contact=7, date=shifted(n)) for n in range(1, 4)]
    result = pair_up(movements, invoices)
    assert len(result) == 3
    assert len({s.movement_id for s in result}) == 3 and len({s.invoice_id for s in result}) == 3
    result = pair_up([movement(ident=n, contact=7) for n in range(1, 6)], [invoice(customer=7)])
    assert len(result) == 1                                                             # eine Rechnung nimmt nur eine Zahlung


def test_a_number_beats_a_pair_that_only_has_the_amount_and_the_customer():
    invoices = [invoice(ident=1, number='K-1001', date=shifted(-1), customer=7), invoice(ident=2, number='R26002', date=shifted(-1), customer=7)]
    movements = [movement(ident=1, contact=7, text='Rechnung R26002'), movement(ident=2, contact=7)]
    result = pair_up(movements, invoices)
    assert [(s.movement_id, s.invoice_id, s.score) for s in result] == [(1, 2, 180), (2, 1, 80)]
    # Bewegung 2 hätte auch zur Rechnung 2 gepasst (80 Punkte, aber an Bewegung 1 vergeben): mehrdeutig; das Paar mit der Nummer ist es nicht
    assert [s.ambiguous for s in result] == [False, True]


def test_ambiguous_means_another_pair_with_the_same_points_for_the_movement_or_for_the_invoice():
    # eine Bewegung, zwei Rechnungen mit gleichen Punkten: die genommene Zeile ist mehrdeutig, auch wenn die zweite Rechnung leer ausgeht
    result = pair_up([movement(contact=7)], [invoice(ident=1, number='K-1001', customer=7), invoice(ident=2, number='K-1002', customer=7)])
    assert [(s.movement_id, s.invoice_id, s.ambiguous) for s in result] == [(1, 1, True)]
    # eine Rechnung, zwei Bewegungen
    result = pair_up([movement(ident=1, contact=7), movement(ident=2, contact=7)], [invoice(customer=7)])
    assert [(s.movement_id, s.invoice_id, s.ambiguous) for s in result] == [(1, 1, True)]
    # eine zweite Rechnung mit anderen Punkten macht die erste nicht mehrdeutig
    result = pair_up([movement(contact=7, text='R26001')], [invoice(ident=1, number='R26001', customer=7), invoice(ident=2, number='K-1002', customer=7)])
    assert [(s.invoice_id, s.score, s.ambiguous) for s in result] == [(1, 180, False)]
    # ein einziges Paar
    assert [s.ambiguous for s in pair_up([movement(contact=7)], [invoice(customer=7)])] == [False]


def test_a_linked_pair_is_never_a_suggestion_in_the_overview():
    movements = [movement(ident=1, text='R26001', contact=7)]
    invoices = [invoice(ident=1, number='R26001', customer=7), invoice(ident=2, number='K-1002', customer=7)]
    assert pair_ids(pair_up(movements, invoices)) == [(1, 1)]
    assert pair_ids(pair_up(movements, invoices, linked=frozenset({(1, 1)}))) == [(1, 2)]        # die Bewegung geht an die andere Rechnung
    assert pair_up(movements, invoices[:1], linked=frozenset({(1, 1)})) == []


def test_the_result_is_sorted_by_points_then_the_distance_then_the_ids():
    invoices = [invoice(ident=1, number='K-1001', date=shifted(-10), customer=7),
                invoice(ident=2, number='R26002', date=shifted(-1), customer=8, open_minor=999)]
    movements = [movement(ident=1, contact=7), movement(ident=2, text='R26002', amount=1)]
    assert pair_ids(pair_up(movements, invoices)) == [(2, 2), (1, 1)]


def test_the_empty_inputs_have_no_pairs():
    assert pair_up([], []) == [] and pair_up([movement()], []) == [] and pair_up([], [invoice()]) == []
    assert pair_up(None, None) == [] and rank_invoices(movement(), None) == [] and rank_movements(invoice(), None) == []


def test_reason_text_joins_the_reasons_and_names_an_ambiguous_pair():
    plain = PaymentSuggestion(1, 2, 180, (REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER), 3)
    assert reason_text(plain) == 'Rechnungsnummer im Verwendungszweck · Betrag stimmt · Kunde passt'
    ambiguous = PaymentSuggestion(1, 2, 80, (REASON_AMOUNT, REASON_CUSTOMER), None, True)
    assert reason_text(ambiguous) == 'Betrag stimmt · Kunde passt · mehrere möglich – bitte prüfen'


# ── Kaputte Werte ────────────────────────────────────────────────────────────

GARBAGE = [None, '', 'text', 0, -5, 1.5, True, False, b'bytes', [], {}, ['a'], {'k': 'v'}, object(), float('nan'), float('inf'), 10 ** 40, '\x00 ']
MOVEMENT_KEYS = ('id', 'date', 'amount_minor', 'recipient', 'text', 'document_number', 'contact_id')
INVOICE_KEYS = ('id', 'number', 'date', 'open_minor', 'customer_id', 'customer_name')


@pytest.mark.parametrize('value', GARBAGE, ids=stable_id)
def test_broken_values_in_any_field_never_raise(value):
    for key in MOVEMENT_KEYS:                                       # ein kaputtes Feld, die übrigen heil – und alle zugleich kaputt
        for broken in (dict(movement(text='R26001', contact=7), **{key: value}), dict.fromkeys(MOVEMENT_KEYS, value)):
            for result in (rank_invoices(broken, [invoice(customer=7)]), rank_movements(invoice(customer=7), [broken]),
                           pair_up([broken], [invoice(customer=7)])):
                assert isinstance(result, list)
    for key in INVOICE_KEYS:
        for broken in (dict(invoice(customer=7), **{key: value}), dict.fromkeys(INVOICE_KEYS, value)):
            for result in (rank_invoices(movement(text='R26001', contact=7), [broken]), rank_movements(broken, [movement(text='R26001', contact=7)]),
                           pair_up([movement(text='R26001', contact=7)], [broken])):
                assert isinstance(result, list)


@pytest.mark.parametrize('value', [None, '', 'text', 1.5, 1.0, True, b'bytes', [], float('nan')], ids=stable_id)
def test_a_row_without_a_whole_id_is_no_candidate(value):
    good_invoice, good_movement = invoice(customer=7), movement(contact=7)
    bad_id, bad_invoice_id = dict(good_movement, id=value), dict(good_invoice, id=value)
    assert rank_invoices(bad_id, [good_invoice]) == [] and rank_movements(good_invoice, [bad_id]) == []
    assert rank_invoices(good_movement, [bad_invoice_id]) == [] and rank_movements(bad_invoice_id, [good_movement]) == []
    assert pair_up([bad_id], [good_invoice]) == [] and pair_up([good_movement], [bad_invoice_id]) == []


@pytest.mark.parametrize('value', [None, '', 'text', 0, -5, 1.5, 1190000.0, True, b'bytes', [], float('nan')], ids=stable_id)
def test_an_amount_that_is_no_positive_whole_number_makes_the_row_no_candidate(value):
    good_invoice, good_movement = invoice(customer=7), movement(contact=7)
    bad_amount, bad_open = dict(good_movement, amount_minor=value), dict(good_invoice, open_minor=value)
    assert rank_invoices(bad_amount, [good_invoice]) == [] and rank_movements(good_invoice, [bad_amount]) == []
    assert rank_invoices(good_movement, [bad_open]) == [] and rank_movements(bad_open, [good_movement]) == []
    assert pair_up([bad_amount], [good_invoice]) == [] and pair_up([good_movement], [bad_open]) == []
    # auch mit der Nummer im Text: ohne brauchbaren Betrag lässt sich nichts zuordnen
    assert rank_invoices(dict(good_movement, text='R26001', amount_minor=value), [good_invoice]) == []


@pytest.mark.parametrize('amount', [0, -1, -OPEN])
def test_a_payment_or_an_invoice_without_a_positive_rest_is_no_candidate(amount):
    assert rank_invoices(movement(amount=amount, contact=7), [invoice(open_minor=amount, customer=7)]) == []
    assert rank_invoices(movement(amount=amount, text='R26001'), [invoice()]) == []
    assert rank_invoices(movement(text='R26001'), [invoice(open_minor=amount)]) == []


@pytest.mark.parametrize('row', [None, 5, 'zeile', [], (1, 2), object()], ids=stable_id)
def test_a_row_that_is_no_dict_is_skipped(row):
    assert rank_invoices(movement(contact=7), [row, invoice(customer=7)])[0].invoice_id == 1
    assert rank_movements(invoice(customer=7), [row, movement(contact=7)])[0].movement_id == 1
    assert rank_invoices(row, [invoice(customer=7)]) == [] and rank_movements(row, [movement(contact=7)]) == []
    assert pair_ids(pair_up([row, movement(contact=7)], [row, invoice(customer=7)])) == [(1, 1)]


def test_missing_keys_are_no_error():
    assert rank_invoices({'id': 1, 'amount_minor': OPEN}, [{'id': 1, 'open_minor': OPEN}]) == []
    found = rank_invoices({'id': 1, 'amount_minor': OPEN, 'text': 'R26001'}, [{'id': 1, 'open_minor': OPEN, 'number': 'R26001'}])
    assert [s.invoice_id for s in found] == [1]


def test_a_value_that_is_no_text_is_read_as_its_text():
    assert [s.invoice_id for s in rank_invoices(movement(amount=1, text=26001), [invoice(number=26001)])] == [1]
    assert [s.invoice_id for s in rank_invoices(movement(amount=1, text=['R26001']), [invoice()])] == [1]


# ── Laufzeit ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'R-2-6-0-0-' * 400, 'R 2 6 0 0 ' * 400, 'R' + '-' * 5000, 'R26001' * 1000, 'R26001 0 ' * 500, ('R' + ' ' * 2) * 1000, '1' * 9000 + 'R26001',
    ('R2600' + '\n') * 700,
], ids=['Bindestriche', 'Leerzeichen', 'Strichreihe', 'Wiederholungen', 'Sperrtreffer', 'Doppelleerraum', 'Ziffern-davor', 'Zeilenumbrüche'])
def test_a_long_hostile_text_is_read_in_linear_time(text):
    invoices = [invoice(), invoice(ident=2, number='26R001'), invoice(ident=3, number='RE-2026/0042'), invoice(ident=4, number='R' + '1' * 39)]
    started = time.perf_counter()
    for _ in range(20):
        rank_invoices(movement(amount=1, text=text, number=text, recipient=text), invoices)
    assert time.perf_counter() - started < 5                                           # gemessen: Bruchteile einer Sekunde


def test_five_hundred_by_five_hundred_is_fast_enough_for_a_page():
    invoices = [invoice(ident=n, number=f'26R{n:05d}', open_minor=1000 * n, customer=n % 50) for n in range(1, 501)]
    filler = 'SEPA-Überweisung Verwendungszweck Vielen Dank für Ihren Auftrag mit freundlichen Grüßen Musterfirma Referenz 20261112 Mandat 4711 '
    movements = [movement(ident=n, amount=1000 * n if n % 2 else 777, contact=n % 50, text=filler * 2 + f'Rechnungs-Nr 26R{n:05d} Danke')
                 for n in range(1, 501)]
    started = time.perf_counter()
    result = pair_up(movements, invoices)
    assert time.perf_counter() - started < 3                                            # gemessen: unter 0,2 s – die Grenze ist großzügig
    assert len(result) == 500 and all(s.invoice_id == s.movement_id for s in result)   # jede Zahlung findet ihre Nummer
