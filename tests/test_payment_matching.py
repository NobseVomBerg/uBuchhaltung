# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zahlungsvorschläge, 2.8.1: die reine Rangfolge ``db/payment_matching.py`` – Zahlungseingang ↔ offene Rechnung.

Rein: kein Datenbankzugriff, keine Uhr. Geprüft wird

* jede Regel einzeln: die Rechnungsnummer im Text (die vier Schreibweisen aus der Praxis, Zeilenumbruch, Groß-/Kleinschreibung, die
  Ziffernsperre an dem Rand, an dem die Nummer mit einer Ziffer beginnt oder endet, zu kurze und zu lange Nummern), **Trennzeichen zwischen zwei
  Ziffern nur dort, wo die Nummer selbst eines trägt** (``26001`` steht nicht in dem Betrag ``26.001,00``, ``202601`` nicht in dem Datum
  ``2026-01-15``), Betrag und Kunde, die Regel „NUMMER oder (BETRAG und KUNDE)“ – ein gleicher Betrag allein ist nie ein Vorschlag –, Punkte und
  Begründungen in fester Reihenfolge, die Datumsregel (mehr als 7 Tage vor dem Rechnungsdatum nur mit Nummer, ein unlesbares Datum steht hinten);
* **nur, was von der Bank kommt, zählt:** Die Nummer wird in Verwendungszweck und Empfänger gesucht, nie in der Beleg-Nr. der Bankzeile, und „Kunde
  passt“ gibt es nur über gemeinsame Namenswörter von Empfänger und Kundenname, nie über den Kontakt der Bankzeile – eine Zuordnung trägt Beleg-Nr.
  und Kontakt der Rechnung in die Bankzeile ein, und beides bleibt nach dem Lösen stehen (der Fall dazu mit den echten Wegen steht in
  ``tests/test_payment_suggestions.py``);
* die Rangfolge (``rank_invoices``, ``rank_movements``: höchstens fünf, nach Punkten, Abstand der Daten, ID) und die Eins-zu-eins-Liste
  (``pair_up``: jede Bewegung und jede Rechnung höchstens einmal, zwei gleich hohe Rechnungen und Zahlungen eines Kunden paaren sich nach dem
  Datum, ``ambiguous`` bei gleicher Punktzahl, unabhängig von der Reihenfolge der Eingabe, ``linked`` schließt Paare aus);
* **das Signal SKONTO (2.9.0):** Der freie Rest der Bewegung ist der offene Betrag nach Abzug des Skontos (``skonto_open_minor`` der Rechnung) – wie BETRAG nie allein ein Vorschlag, mit KUNDE einer,
  Punkte wie BETRAG, die Begründung „Betrag nach Skonto stimmt“, das Merkmal ``skonto`` am Vorschlag, kein ``REASON_PARTIAL`` mit Nummer; ein Wert, der kein Skonto sein kann, gibt kein Signal;
* **eindeutig (2.9.2, ``is_certain``):** Die Rechnungsnummer steht im Text UND der Betrag stimmt, das Paar ist nicht mehrdeutig und kein Skonto-Vorschlag – nichts anderes; Grundlage von „Alle
  eindeutigen zuordnen“ (die Oberfläche steht in ``tests/test_payment_suggestions.py``);
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
    AMBIGUOUS_TEXT, DAYS_BEFORE_INVOICE, MAX_NUMBER_LENGTH, MAX_SUGGESTIONS, POINTS_AMOUNT, POINTS_CUSTOMER, POINTS_NUMBER, POINTS_SKONTO, REASON_AMOUNT,
    REASON_CUSTOMER, REASON_NUMBER, REASON_PARTIAL, REASON_SEPARATOR, REASON_SKONTO, PaymentSuggestion, is_certain, pair_up, rank_invoices, rank_movements, reason_text)
from db.receipt_matching import MAX_FIELD_LENGTH

DAY = '2026-03-04'                          # das Rechnungsdatum der Tests
OPEN = 1190000                              # 119,00 EUR in Minor Units (money.SCALE = 4)
DISCOUNT = 23800                            # 2 % Skonto auf 119,00 EUR: 2,38 EUR
AFTER_DISCOUNT = OPEN - DISCOUNT            # was der Kunde zahlt, wenn er das Skonto abzieht: 116,62 EUR
BUYER = 'Käufer AG'                         # der Empfänger einer Bewegung und der Kunde einer Rechnung, die ein Namenswort teilen: „Kunde passt“
OTHER = 'Müller KG'                         # ein anderer Kunde: kein gemeinsames Namenswort mit BUYER


# ── Hilfen ───────────────────────────────────────────────────────────────────

def shifted(days, start=DAY):
    """Das Datum ``days`` Tage nach (negativ: vor) ``start`` als ISO-Text."""
    return (datetime.date.fromisoformat(start) + datetime.timedelta(days=days)).isoformat()


def movement(ident=1, amount=OPEN, date=DAY, text='', recipient='', number='', contact=None):
    """Eine Bewegung (Zahlungseingang mit freiem Rest ``amount``), ohne weitere Merkmale."""
    return {'id': ident, 'date': date, 'amount_minor': amount, 'recipient': recipient, 'text': text, 'document_number': number,
            'contact_id': contact}


def invoice(ident=1, number='R26001', open_minor=OPEN, date=DAY, customer=None, name='', skonto_open=None):
    """Eine offene Rechnung, ohne weitere Merkmale. ``skonto_open``: ``skonto_open_minor`` – der Schlüssel steht nur, wenn er genannt ist (ohne ihn gibt es kein Skonto-Signal)."""
    row = {'id': ident, 'number': number, 'date': date, 'open_minor': open_minor, 'customer_id': customer, 'customer_name': name}
    if skonto_open is not None:
        row['skonto_open_minor'] = skonto_open
    return row


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
    # höchstens EIN Trennzeichen an einer Fuge mit einem Buchstaben: Leerraum, Bindestrich, Unterstrich, Schrägstrich, Punkt
    ('R26003', 'R 26003'), ('R26003', 'R-26003'), ('R26003', 'R_26003'), ('R26003', 'R/26003'), ('R26003', 'R.26003'),
    ('R26001', 'R\n26001'), ('R26001', 'R\xa026001'),                      # der Zeilenumbruch einer Bank, ein geschütztes Leerzeichen
    ('26R001', '26 R001'), ('26R001', '26-R-001'), ('R26A01', 'R 26 A 01'),   # Buchstaben mitten in der Nummer: beide Fugen daneben sind offen
    # die Nummer, wie sie im Buch steht, bestimmt die Fugen zwischen zwei Ziffern: Dort steht ein Trennzeichen, wo sie selbst eines trägt
    ('R26 001', 'R26001'), ('R26 001', 'R26 001'), ('R26 001', 'R26-001'), ('R26 001', 'R26\n001'), ('R26 001', 'R 26 001'),
    # ein Buchstabe oder Satzzeichen davor und dahinter stört nicht, ein Datum hinter der Nummer auch nicht
    ('R26003', 'RGNR26003'), ('R26003', 'RE-Nr.R26003.KD-Nr.'), ('R26003', 'R26003 12.11.2026'), ('R26001', 'R26001A'), ('R26001', 'ER26001'),
    # beginnt die Nummer mit einem Buchstaben, bleibt der Rand davor offen (auch eine Ziffer davor stört nicht): ``1R26001`` trifft ``R26001``
    ('R26001', '1R26001'), ('R26001', 'Kd 12R26001'),
    # eine Nummer mit eigenen Trennzeichen: nur ihre Zeichen zählen
    ('RE-2026/0042', 'RE20260042'), ('RE-2026/0042', 're 2026 0042 bitte'), ('RE-2026/0042', 'RE-2026-0042'), ('RE-2026/0042', 'Rechnung RE_2026_0042'),
    ('RE-2026/0042', 'RE 2026 0042'), ('RE-2026/0042', 're2026-0042'), ('RE-2026/0042', 'RE-2026/0042'),
    ('2026-0042', '2026-0042'), ('2026-0042', '2026 0042'), ('2026-0042', '2026/0042'), ('2026-0042', '20260042'),     # an ihrer eigenen Fuge
    ('26/001', '26/001'), ('26/001', '26 001'), ('26/001', '26-001'), ('26/001', '26001'),
    ('20260042', 'Rechnung 20260042-01'), ('20260042', 'Nr.20260042'), ('20260042', 'NR20260042'), ('20260042', '20260042'),
    ('26R001', 'Zahlung 26R001'), ('26R001', 'RG26R001'), ('26R001', '26R001/2026'),
    ('R2026', 'R2026'),                                      # fünf Zeichen mit einer Ziffer: brauchbar
]
MISSES = [
    # ein Stück einer längeren Ziffernfolge: an dem Rand, an dem die Nummer mit einer Ziffer endet bzw. beginnt, schließt keine Ziffer an
    ('R26001', 'R260010'), ('R26001', 'R2600100'), ('R26001', 'R26 0010'),
    ('26R001', '26R0010'), ('26R001', '126R001'), ('20260042', '120260042'), ('20260042', '202600421'),
    # mehr als ein Trennzeichen oder ein anderes Zeichen an einer Fuge mit einem Buchstaben
    ('R26003', 'R  26003'), ('R26003', 'R--26003'), ('R26003', 'R - 26003'), ('R26003', 'R,26003'), ('R26003', 'R:26003'),
    ('R26003', 'R+26003'), ('R26003', 'R#26003'), ('R26003', 'R2ä6003'),
    # zwischen zwei Ziffern ein Trennzeichen, das die Nummer selbst nicht trägt: ein Umbruch mitten in den Ziffern, ein Leerzeichen, ein Gedankenstrich
    ('R26001', 'R26 001'), ('R26001', 'R2-6-0-0-1'), ('R26001', 'R260\n01'), ('R26001', 'R26\xa0001'), ('R26003', 'R260  03'), ('R26003', 'R26 003'),
    ('20260042', '2026-0042'), ('26R001', '2 6R001'), ('26R001', '26R0 01'),
    # … und eine Nummer mit eigenen Fugen trifft nur an diesen, nicht an anderen und nicht ohne Trennzeichen verdoppelt
    ('R26 001', 'R2 6001'), ('R26 001', 'R260 01'), ('R26 001', 'R2-6-0-0-1'), ('R26 001', 'R26  001'), ('R26 001', 'R26,001'),
    ('RE-2026/0042', 'RE 20 260042'), ('RE-2026/0042', 'RE 202 60042'), ('RE-2026/0042', 'RE 2026  0042'), ('RE-2026/0042', 'RE 2026,0042'),
    ('2026-0042', '20.260.042'), ('2026-0042', '202 60042'), ('2026-0042', '2026  0042'), ('2026-0042', '2-0-2-6-0-0-4-2'), ('2026-0042', '2026-00421'),
    ('26/001', '2 6001'), ('26/001', '260 01'), ('26/001', '26 0 01'), ('26/001', '260012'),
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


@pytest.mark.parametrize('field', ['text', 'recipient'])
def test_the_number_may_stand_in_the_text_or_the_recipient(field):
    found = rank_invoices(movement(amount=1, **{field: 'Zahlung R26001'}), [invoice()])
    assert [s.invoice_id for s in found] == [1]


def test_the_number_in_the_document_number_of_the_bank_row_is_no_evidence():
    """Eine Zuordnung trägt die Rechnungsnummer in die Beleg-Nr. der Bankzeile ein (``handlers._backfill_booking_skr``), und sie bleibt nach dem
    Entfernen der Zuordnung dort stehen: Eine falsch zugeordnete und wieder gelöste Rechnung wäre sonst der stärkste Vorschlag (mit der Begründung
    „Rechnungsnummer im Verwendungszweck“, obwohl sie dort nicht steht). Gelesen wird nur, was von der Bank kommt: Verwendungszweck und Empfänger."""
    for number in ('R26001', 'Zahlung R26001', 'RE-Nr. R26001.KD-Nr.', 'r 26001'):
        assert rank_invoices(movement(amount=1, number=number), [invoice()]) == [], number
        assert rank_movements(invoice(), [movement(amount=1, number=number)]) == [], number
        assert pair_up([movement(amount=1, number=number)], [invoice()]) == [], number
    assert rank_invoices(movement(number='R26001'), [invoice()]) == []                  # auch mit gleichem Betrag nicht: der Betrag allein beweist nichts
    assert rank_invoices(movement(number='R26001', contact=7), [invoice(customer=7)]) == []
    # mit Betrag und Kunde ist es ein Vorschlag – die Nummer in der Beleg-Nr. gibt ihm weder Punkte noch die Begründung
    found = rank_invoices(movement(number='R26001', recipient=BUYER), [invoice(name=BUYER)])
    assert [(s.reasons, s.score) for s in found] == [((REASON_AMOUNT, REASON_CUSTOMER), POINTS_AMOUNT + POINTS_CUSTOMER)]
    # steht sie auch im Verwendungszweck, zählt der Verwendungszweck – und nur er
    found = rank_invoices(movement(number='R26001', text='Zahlung R26001', recipient=BUYER), [invoice(name=BUYER)])
    assert [(s.reasons, s.score) for s in found] == [((REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER), 180)]


def test_the_parts_of_a_number_in_two_fields_are_not_the_number():
    assert not rank_invoices(movement(amount=1, text='R260', recipient='01'), [invoice()])
    assert not rank_invoices(movement(amount=1, text='Zahlung R260', recipient='01 Käufer'), [invoice()])
    assert not rank_invoices(movement(amount=1, text='R260', number='01'), [invoice()])          # die Beleg-Nr. wird nicht gelesen: auch nicht als Rest einer Nummer


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


# ── NUMMER: Trennzeichen zwischen zwei Ziffern nur an den Fugen der Nummer ───

# Ein Betrag, ein Datum, eine Folge von Zahlen: Eine Ziffernfolge mit Trennzeichen ist nicht die Rechnungsnummer, die selbst keine Trennzeichen trägt. Ohne
# diese Regel stand eine Nummer wie ``26001`` in jedem Betrag ``26.001,00`` und ``202601`` in jedem Datum ``2026-01-15`` (und ``R26 003`` hätte ``R26003`` getroffen).
NOT_IN_AMOUNTS_AND_DATES = [
    ('26001', 'Rechnung über 26.001,00 EUR'), ('26001', '26.001,00'), ('26001', 'Summe 26,001'), ('26001', 'Betrag 26 001'), ('26001', '26-001'),
    ('10001', 'Überweisung 10.001,50 EUR'), ('10001', '10.001,50'), ('10001', '1.0001'),
    ('202601', 'Datum 2026-01-15'), ('202601', 'vom 2026-01'), ('202601', 'Monat 2026/01'), ('202601', '2026 01'), ('202601', '2026.01.'),
    ('260120', 'am 26.01.20'), ('260120', '26-01-20'), ('260120', '26/01/20'), ('260120', '26 01 20'),
]
IN_PLAIN_FORM = [
    ('26001', 'Rechnung 26001'), ('26001', 'Nr.26001'), ('26001', 'Rechnung 26001 vom 12.11.2026'), ('26001', '26001.'),
    ('10001', 'Rechnung 10001'), ('202601', 'Rechnung 202601'), ('202601', 'Rechnung 202601-A'), ('260120', 'RG 260120 bezahlt'),
]


@pytest.mark.parametrize('number, text', NOT_IN_AMOUNTS_AND_DATES)
def test_a_number_without_separators_is_not_in_an_amount_or_a_date(number, text):
    assert not found_by_text(number, text)


@pytest.mark.parametrize('number, text', IN_PLAIN_FORM)
def test_a_number_without_separators_is_in_its_plain_form(number, text):
    assert found_by_text(number, text)


def test_a_separator_inside_the_digits_hits_only_where_the_number_carries_one():
    """``R26 003`` – ein Umbruch mitten in den Ziffern – trifft ``R26003`` nicht mehr: Die Nummer trägt dort kein Trennzeichen. Trägt sie eines (so, wie sie im
    Buch steht), trifft es – und nur dort."""
    assert not found_by_text('R26003', 'R26 003') and not found_by_text('R26003', 'R26\n003') and not found_by_text('R26003', 'R2-6003')
    assert found_by_text('R 26003', 'R26003') and found_by_text('R 26003', 'R-26003') and not found_by_text('R 26003', 'R26 003')   # die Fuge mit dem Buchstaben
    assert found_by_text('R26 003', 'R26 003') and found_by_text('R26 003', 'R26\n003') and found_by_text('R26 003', 'R26003')
    assert not found_by_text('R26 003', 'R2 6003') and not found_by_text('R26 003', 'R260 03')
    assert found_by_text('RE-2026/0042', 'RE 2026 0042') and found_by_text('RE-2026/0042', 're2026-0042')
    assert not found_by_text('RE-2026/0042', 'RE 20 260042') and not found_by_text('RE-2026/0042', 'RE 202 60042')


def test_the_joints_of_the_number_hold_for_every_ranking_function():
    """Die Fugen gelten für jede Rangfolge – ``rank_invoices``, ``rank_movements`` und ``pair_up`` lesen dieselbe Nummer."""
    for number, text, expected in (('2026-0042', 'Zahlung 2026 0042', True), ('2026-0042', 'Zahlung 20.260.042', False),
                                   ('20260042', 'Zahlung 2026 0042', False), ('20260042', 'Zahlung 20260042', True)):
        paid, billed = movement(amount=1, text=text), invoice(number=number)
        assert bool(rank_invoices(paid, [billed])) is expected, (number, text)
        assert bool(rank_movements(billed, [paid])) is expected, (number, text)
        assert bool(pair_up([paid], [billed])) is expected, (number, text)


def test_two_invoices_with_the_same_characters_but_another_raw_form_do_not_share_a_pattern():
    """Der Ausdruck einer Nummer wird zwischengespeichert (``lru_cache``): Der Schlüssel muss die Fugen tragen, nicht nur die Zeichen – ``R26001`` und ``R26 001``
    ergeben dieselbe normalisierte Nummer, aber nicht dasselbe Muster. Gleich, welche Rechnung zuerst gelesen wird."""
    plain, spaced = invoice(ident=1, number='R26001'), invoice(ident=2, number='R26 001')
    for invoices in ([plain, spaced], [spaced, plain]):
        for text, expected in (('Zahlung R26 001', [2]), ('Zahlung R26001', [1, 2]), ('Zahlung R2 6001', [])):
            found = rank_invoices(movement(amount=1, text=text), invoices)
            assert sorted(s.invoice_id for s in found) == expected, (text, [i['id'] for i in invoices])


def test_an_amount_that_looks_like_the_number_is_still_an_amount():
    """Eine Rechnung 26001 über 119,00 € und ein Zahlungseingang von 26.001,00 € eines Fremden: Der Betrag im Text ist nicht die Rechnungsnummer."""
    assert rank_invoices(movement(amount=260010000, text='Zahlung 26.001,00 EUR', recipient='Fremde GmbH'), [invoice(number='26001')]) == []


# ── BETRAG und KUNDE ─────────────────────────────────────────────────────────

def test_an_equal_amount_alone_is_no_suggestion():
    assert rank_invoices(movement(), [invoice()]) == []
    assert rank_invoices(movement(recipient=BUYER), [invoice(name=OTHER)]) == []           # ein anderer Kunde
    assert rank_invoices(movement(recipient='Müller'), [invoice(name='Meier GmbH')]) == []
    assert rank_movements(invoice(), [movement()]) == []


def test_a_customer_alone_is_no_suggestion_either():
    assert rank_invoices(movement(amount=1, recipient=BUYER), [invoice(name=BUYER)]) == []
    assert rank_invoices(movement(amount=1, recipient='Käufer Handels GmbH'), [invoice(name='Handels-Käufer AG')]) == []


def test_amount_and_customer_by_a_shared_name_word_is_a_suggestion():
    found = rank_invoices(movement(recipient=BUYER), [invoice(name=BUYER)])
    assert [(s.reasons, s.score) for s in found] == [((REASON_AMOUNT, REASON_CUSTOMER), POINTS_AMOUNT + POINTS_CUSTOMER)]
    found = rank_invoices(movement(recipient='Käufer Handels GmbH'), [invoice(name='Handels-Käufer AG')])
    assert [s.reasons for s in found] == [(REASON_AMOUNT, REASON_CUSTOMER)]
    assert [s.reasons for s in rank_invoices(movement(recipient='Abc Handel'), [invoice(name='Xyz Handel e. K.')])] == [(REASON_AMOUNT, REASON_CUSTOMER)]


def test_the_contact_of_the_bank_row_is_never_the_customer():
    """Eine Zuordnung trägt den Kunden der Rechnung als Kontakt in die Bankzeile ein (``handlers._backfill_booking_skr``), und er bleibt nach dem Entfernen
    stehen: Der Kontakt der Bewegung beweist nichts. Gleicher Betrag und gleicher Kontakt allein sind kein Vorschlag – „Kunde passt“ gibt es nur über
    gemeinsame Namenswörter von Empfänger und Kundenname."""
    assert rank_invoices(movement(contact=7), [invoice(customer=7)]) == []
    assert rank_movements(invoice(customer=7), [movement(contact=7)]) == []
    assert pair_up([movement(contact=7)], [invoice(customer=7)]) == []
    # ein Kontakt ändert an einem Vorschlag nichts: nicht, wenn er mit dem Kunden übereinstimmt, und nicht, wenn er abweicht
    plain = rank_invoices(movement(recipient=BUYER), [invoice(name=BUYER)])
    assert [(s.reasons, s.score) for s in plain] == [((REASON_AMOUNT, REASON_CUSTOMER), 80)]
    assert rank_invoices(movement(recipient=BUYER, contact=7), [invoice(name=BUYER, customer=7)]) == plain
    assert rank_invoices(movement(recipient=BUYER, contact=8), [invoice(name=BUYER, customer=7)]) == plain
    assert rank_invoices(movement(recipient=OTHER, contact=7), [invoice(name=BUYER, customer=7)]) == []            # der gleiche Kontakt rettet keinen anderen Namen
    # mit der Nummer im Verwendungszweck bleiben es die Punkte der Nummer und des Betrags: Der Kontakt gibt keine
    found = rank_invoices(movement(text='R26001', contact=7), [invoice(customer=7)])
    assert [(s.reasons, s.score) for s in found] == [((REASON_NUMBER, REASON_AMOUNT), POINTS_NUMBER + POINTS_AMOUNT)]


def test_a_missing_name_is_never_the_same_customer_as_a_missing_name():
    assert rank_invoices(movement(recipient=''), [invoice(name='')]) == []
    assert rank_invoices(movement(recipient=None), [invoice(name=None)]) == []
    assert rank_invoices(movement(recipient=''), [invoice(name=BUYER)]) == [] and rank_invoices(movement(recipient=BUYER), [invoice(name='')]) == []
    assert rank_invoices(movement(contact=None), [invoice(customer=None)]) == []             # fehlende Kontakte gelten ohnehin nicht
    assert rank_invoices(movement(contact=0), [invoice(customer=None)]) == []


@pytest.mark.parametrize('recipient, name', [
    ('Rechnung Zahlung GmbH', 'Müller Rechnung GmbH'),                  # nur Wörter aus ``STOP_WORDS``
    ('Abc AG', 'Abc AG'),                                               # unter vier Zeichen zählt kein Wort
    ('', 'Käufer AG'), ('Käufer AG', ''), ('', ''), ('12345 6789', '12345 6789'),  # nichts, oder nur Zahlen
], ids=['Stoppwörter', 'kurz', 'leer-links', 'leer-rechts', 'leer-beide', 'Zahlen'])
def test_stop_words_short_words_numbers_and_empty_names_are_no_customer(recipient, name):
    assert rank_invoices(movement(recipient=recipient), [invoice(name=name)]) == []


def test_the_name_words_come_from_the_recipient_not_from_the_text():
    assert rank_invoices(movement(text='Käufer AG', recipient=''), [invoice(name='Käufer AG')]) == []
    assert rank_invoices(movement(number='Käufer AG', recipient=''), [invoice(name='Käufer AG')]) == []         # und nicht aus der Beleg-Nr.


@pytest.mark.parametrize('kwargs, reasons, score', [
    ({'text': 'R26001', 'recipient': BUYER}, (REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER), 180),
    ({'text': 'R26001'}, (REASON_NUMBER, REASON_AMOUNT), 150),
    ({'text': 'R26001', 'amount': 500, 'recipient': BUYER}, (REASON_NUMBER, REASON_CUSTOMER, REASON_PARTIAL), 130),
    ({'text': 'R26001', 'amount': 500}, (REASON_NUMBER, REASON_PARTIAL), 100),
    ({'recipient': BUYER}, (REASON_AMOUNT, REASON_CUSTOMER), 80),
], ids=['alles', 'Nummer+Betrag', 'Nummer+Kunde', 'nur Nummer', 'Betrag+Kunde'])
def test_points_and_reasons_come_in_a_fixed_order(kwargs, reasons, score):
    found = rank_invoices(movement(**kwargs), [invoice(name=BUYER)])
    assert [(s.reasons, s.score) for s in found] == [(reasons, score)]


def test_a_number_with_another_amount_names_the_partial_payment_a_number_with_the_amount_does_not():
    assert REASON_PARTIAL in rank_invoices(movement(amount=500, text='R26001'), [invoice()])[0].reasons
    assert REASON_PARTIAL not in rank_invoices(movement(text='R26001'), [invoice()])[0].reasons
    assert REASON_PARTIAL not in rank_invoices(movement(recipient=BUYER), [invoice(name=BUYER)])[0].reasons


def test_the_amount_is_compared_to_the_minor_value():
    assert pair_ids(rank_invoices(movement(amount=OPEN, recipient=BUYER), [invoice(name=BUYER)])) == [(1, 1)]      # Kontrolle: derselbe Betrag ist ein Vorschlag
    assert rank_invoices(movement(amount=OPEN + 1, recipient=BUYER), [invoice(name=BUYER)]) == []
    assert rank_invoices(movement(amount=OPEN - 1, recipient=BUYER), [invoice(name=BUYER)]) == []


# ── SKONTO: der Betrag nach Abzug des Skontos ───────────────────────────────

def test_the_skonto_constants_and_the_sentence_are_the_planned_ones():
    assert POINTS_SKONTO == POINTS_AMOUNT == 50
    assert REASON_SKONTO == 'Betrag nach Skonto stimmt'
    assert (OPEN, DISCOUNT, AFTER_DISCOUNT) == (1190000, 23800, 1166200)


def test_the_amount_after_the_discount_and_the_customer_are_a_suggestion_that_carries_the_skonto_flag():
    found = rank_invoices(movement(amount=AFTER_DISCOUNT, recipient=BUYER), [invoice(name=BUYER, skonto_open=AFTER_DISCOUNT)])
    assert [(s.reasons, s.score, s.skonto) for s in found] == [((REASON_SKONTO, REASON_CUSTOMER), POINTS_SKONTO + POINTS_CUSTOMER, True)]
    assert reason_text(found[0]) == 'Betrag nach Skonto stimmt · Kunde passt'
    assert [(s.reasons, s.skonto) for s in rank_movements(invoice(name=BUYER, skonto_open=AFTER_DISCOUNT), [movement(amount=AFTER_DISCOUNT, recipient=BUYER)])] == [
        ((REASON_SKONTO, REASON_CUSTOMER), True)]
    assert pair_ids(pair_up([movement(amount=AFTER_DISCOUNT, recipient=BUYER)], [invoice(name=BUYER, skonto_open=AFTER_DISCOUNT)])) == [(1, 1)]


def test_the_amount_after_the_discount_alone_is_no_suggestion_either():
    assert rank_invoices(movement(amount=AFTER_DISCOUNT), [invoice(skonto_open=AFTER_DISCOUNT)]) == []
    assert rank_invoices(movement(amount=AFTER_DISCOUNT, recipient=BUYER), [invoice(name=OTHER, skonto_open=AFTER_DISCOUNT)]) == []        # ein anderer Kunde
    assert rank_movements(invoice(skonto_open=AFTER_DISCOUNT), [movement(amount=AFTER_DISCOUNT)]) == []
    assert pair_up([movement(amount=AFTER_DISCOUNT)], [invoice(skonto_open=AFTER_DISCOUNT)]) == []


def test_without_a_skonto_open_amount_the_invoice_has_no_skonto_signal():
    """Fehlt der Schlüssel (eine Rechnung ohne Skonto, ein Beleg mit mehreren Sätzen, mit Stammdaten …), ist der Betrag nach 2 % nur irgendein Betrag."""
    assert rank_invoices(movement(amount=AFTER_DISCOUNT, recipient=BUYER), [invoice(name=BUYER)]) == []
    assert rank_invoices(movement(amount=AFTER_DISCOUNT, recipient=BUYER), [{**invoice(name=BUYER), 'skonto_open_minor': None}]) == []


@pytest.mark.parametrize('value', [0, -5, -AFTER_DISCOUNT, OPEN, OPEN + 1, 1.5, '1166200', True, False, [AFTER_DISCOUNT], b'x'],
                         ids=lambda value: stable_id(value))
def test_a_value_that_cannot_be_a_discounted_amount_gives_no_signal(value):
    """Ein Betrag, der kein offener Betrag abzüglich eines Skontos sein kann (nicht größer 0, nicht kleiner als der offene Betrag, keine ganze Zahl, ein Wahrheitswert), gibt kein Signal –
    auch nicht, wenn die Bewegung genau diesen Betrag trägt."""
    row = {**invoice(name=BUYER), 'skonto_open_minor': value}
    assert rank_invoices(movement(amount=AFTER_DISCOUNT, recipient=BUYER), [row]) == []
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        assert all(not s.skonto for s in rank_invoices(movement(amount=value, recipient=BUYER), [row]))


def test_a_bool_is_no_whole_number_for_the_skonto_value_even_where_it_equals_the_amount():
    """``True == 1``: Hätte die Bewegung den Betrag 1, träfe ein Wahrheitswert als ``skonto_open_minor`` sie – er zählt nicht (``is_int``)."""
    assert rank_invoices(movement(amount=1, recipient=BUYER), [{**invoice(name=BUYER, open_minor=OPEN), 'skonto_open_minor': True}]) == []


def test_the_full_amount_stays_the_amount_signal_and_is_no_skonto():
    found = rank_invoices(movement(amount=OPEN, recipient=BUYER), [invoice(name=BUYER, skonto_open=AFTER_DISCOUNT)])
    assert [(s.reasons, s.score, s.skonto) for s in found] == [((REASON_AMOUNT, REASON_CUSTOMER), POINTS_AMOUNT + POINTS_CUSTOMER, False)]


def test_with_the_number_the_reasons_come_in_the_fixed_order_and_name_no_partial_payment():
    found = rank_invoices(movement(amount=AFTER_DISCOUNT, text='R26001', recipient=BUYER), [invoice(name=BUYER, skonto_open=AFTER_DISCOUNT)])
    assert [(s.reasons, s.score, s.skonto) for s in found] == [
        ((REASON_NUMBER, REASON_SKONTO, REASON_CUSTOMER), POINTS_NUMBER + POINTS_SKONTO + POINTS_CUSTOMER, True)]
    assert REASON_PARTIAL not in found[0].reasons
    found = rank_invoices(movement(amount=AFTER_DISCOUNT, text='R26001'), [invoice(skonto_open=AFTER_DISCOUNT)])
    assert [(s.reasons, s.score, s.skonto) for s in found] == [((REASON_NUMBER, REASON_SKONTO), POINTS_NUMBER + POINTS_SKONTO, True)]       # die Nummer trägt den Vorschlag allein


def test_a_number_with_another_amount_stays_a_partial_payment_without_the_skonto_flag():
    found = rank_invoices(movement(amount=500, text='R26001', recipient=BUYER), [invoice(name=BUYER, skonto_open=AFTER_DISCOUNT)])
    assert [(s.reasons, s.skonto) for s in found] == [((REASON_NUMBER, REASON_CUSTOMER, REASON_PARTIAL), False)]


def test_the_date_rule_holds_for_the_skonto_signal_too():
    early = movement(amount=AFTER_DISCOUNT, recipient=BUYER, date=shifted(-DAYS_BEFORE_INVOICE - 1))
    assert rank_invoices(early, [invoice(name=BUYER, skonto_open=AFTER_DISCOUNT)]) == []
    assert rank_invoices({**early, 'text': 'R26001'}, [invoice(name=BUYER, skonto_open=AFTER_DISCOUNT)])[0].skonto is True            # mit der Nummer schon
    assert rank_invoices(movement(amount=AFTER_DISCOUNT, recipient=BUYER, date=shifted(-DAYS_BEFORE_INVOICE)), [invoice(name=BUYER, skonto_open=AFTER_DISCOUNT)])


def test_each_invoice_is_found_once_under_either_amount_and_a_linked_pair_never():
    invoices = [invoice(name=BUYER, skonto_open=AFTER_DISCOUNT)]
    assert len(rank_invoices(movement(amount=OPEN, recipient=BUYER), invoices)) == 1                  # der Index nach dem offenen Betrag
    assert len(rank_invoices(movement(amount=AFTER_DISCOUNT, recipient=BUYER), invoices)) == 1        # … und nach dem Betrag nach Skonto
    assert len(pair_up([movement(amount=AFTER_DISCOUNT, recipient=BUYER)], invoices)) == 1
    assert rank_invoices(movement(amount=AFTER_DISCOUNT, recipient=BUYER), invoices, linked=frozenset({(1, 1)})) == []
    assert pair_up([movement(amount=AFTER_DISCOUNT, recipient=BUYER)], invoices, linked=frozenset({(1, 1)})) == []


def test_an_amount_that_is_the_open_amount_of_one_invoice_and_the_discounted_amount_of_another_is_ambiguous():
    """Zwei Rechnungen eines Kunden: die eine mit dem offenen Betrag 116,62, die andere mit dem Skonto-Betrag 116,62 – gleiche Punkte (50 + 30): mehrdeutig, und die Übersicht nimmt die zeitlich nächste."""
    invoices = [invoice(ident=1, number='K-1001', open_minor=AFTER_DISCOUNT, date='2026-02-01', name=BUYER),
                invoice(ident=2, number='K-1002', open_minor=OPEN, date='2026-03-01', name=BUYER, skonto_open=AFTER_DISCOUNT)]
    found = rank_invoices(movement(amount=AFTER_DISCOUNT, date='2026-03-10', recipient=BUYER), invoices)
    assert [(s.invoice_id, s.score, s.skonto) for s in found] == [(2, 80, True), (1, 80, False)]        # gleiche Punkte: die zeitlich nähere zuerst
    result = pair_up([movement(amount=AFTER_DISCOUNT, date='2026-03-10', recipient=BUYER)], invoices)
    assert [(s.invoice_id, s.ambiguous, s.skonto) for s in result] == [(2, True, True)]                  # ``dataclasses.replace`` behält das Merkmal


def test_two_equal_invoices_with_a_discount_pair_by_the_date_and_keep_the_flag():
    invoices = [invoice(ident=1, number='K-1001', date='2026-01-01', name=BUYER, skonto_open=AFTER_DISCOUNT),
                invoice(ident=2, number='K-1002', date='2026-02-01', name=BUYER, skonto_open=AFTER_DISCOUNT)]
    movements = [movement(ident=1, amount=AFTER_DISCOUNT, date='2026-01-10', recipient=BUYER), movement(ident=2, amount=AFTER_DISCOUNT, date='2026-02-10', recipient=BUYER)]
    result = pair_up(movements, invoices)
    assert [(s.movement_id, s.invoice_id, s.ambiguous, s.skonto) for s in result] == [(1, 1, True, True), (2, 2, True, True)]


def test_the_discount_signal_is_fast_enough_for_a_page_too():
    invoices = [invoice(ident=n, number=f'26R{n:05d}', open_minor=1000 * n, name=f'Firma{n % 50:02d}', skonto_open=1000 * n - 20) for n in range(1, 501)]
    movements = [movement(ident=n, amount=1000 * n - 20, recipient=f'Firma{n % 50:02d}', text='Zahlung') for n in range(1, 501)]
    started = time.perf_counter()
    result = pair_up(movements, invoices)
    assert time.perf_counter() - started < 3
    assert len(result) == 500 and all(s.invoice_id == s.movement_id and s.skonto for s in result)


# ── DATUM ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('days, expected', [(-30, False), (-8, False), (-7, True), (-1, True), (0, True), (3, True), (400, True)])
def test_a_payment_more_than_seven_days_before_the_invoice_is_no_suggestion_without_the_number(days, expected):
    found = rank_invoices(movement(date=shifted(days), recipient=BUYER), [invoice(name=BUYER)])
    assert bool(found) is expected
    assert (found[0].day_distance if found else days) == days


@pytest.mark.parametrize('days', [-8, -60, -365])
def test_a_payment_long_before_the_invoice_is_still_a_suggestion_with_the_number(days):
    found = rank_invoices(movement(date=shifted(days), text='Vorauszahlung R26001', recipient=BUYER), [invoice(name=BUYER)])
    assert [(s.reasons, s.day_distance) for s in found] == [((REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER), days)]


@pytest.mark.parametrize('bad', [None, '', 'gestern', '2026-13-40', '2026-02-30', '04.03.2026', 20260304, 4.5, b'2026-03-04', ['2026-03-04']], ids=stable_id)
def test_an_unreadable_date_is_no_error_and_stands_last(bad):
    near = movement(ident=1, date=shifted(2), recipient=BUYER)
    broken = movement(ident=2, date=bad, recipient=BUYER)
    found = rank_invoices(broken, [invoice(name=BUYER)])
    assert [(s.movement_id, s.day_distance) for s in found] == [(2, None)]           # kein Datum: die Datumsregel gilt nicht, der Vorschlag bleibt
    found = rank_movements(invoice(name=BUYER), [broken, near])
    assert [s.movement_id for s in found] == [1, 2]                                    # … und steht hinter dem mit lesbarem Datum
    found = rank_movements(invoice(name=BUYER, date=bad), [broken, near])
    assert [(s.movement_id, s.day_distance) for s in found] == [(1, None), (2, None)]  # beide ohne Abstand: nach der ID


# ── Die Rangfolge ────────────────────────────────────────────────────────────

def test_more_points_come_first_then_the_nearer_date_then_the_id():
    invoices = [
        invoice(ident=2, number='K-7777', date=shifted(-5), name=BUYER),                              # Betrag + Kunde (80), 5 Tage Abstand
        invoice(ident=4, number='K-9999', date=shifted(-2), name=BUYER),                              # Betrag + Kunde (80), 2 Tage
        invoice(ident=3, number='K-8888', date=shifted(-2), name=BUYER),                              # dasselbe, kleinere ID
        invoice(ident=6, number='K-6666', date=shifted(-1), name=BUYER),                              # Betrag + Kunde (80), 1 Tag
        invoice(ident=9, number='R26001', date=shifted(-40), name=BUYER, open_minor=1),               # Nummer + Kunde (130): 40 Tage, aber mehr Punkte
    ]
    found = rank_invoices(movement(text='Danke R26001', recipient=BUYER), invoices)
    assert [(s.invoice_id, s.score) for s in found] == [(9, 130), (6, 80), (3, 80), (4, 80), (2, 80)]       # Abstand, nicht die ID, bestimmt die Reihenfolge


def test_rank_movements_orders_by_points_then_date_then_the_id_of_the_movement():
    movements = [
        movement(ident=3, date=shifted(5), recipient=BUYER), movement(ident=5, date=shifted(2), recipient=BUYER), movement(ident=4, date=shifted(2), recipient=BUYER),
        movement(ident=6, date=shifted(1), recipient=BUYER), movement(ident=8, date=shifted(30), amount=1, text='R26001'),
    ]
    found = rank_movements(invoice(name=BUYER), movements)
    assert [(s.movement_id, s.score) for s in found] == [(8, 100), (6, 80), (4, 80), (5, 80), (3, 80)]


def test_at_most_five_suggestions_come_back():
    invoices = [invoice(ident=n, number=f'K-{n}000', name=BUYER) for n in range(1, 9)]
    found = rank_invoices(movement(recipient=BUYER), invoices)
    assert [s.invoice_id for s in found] == [1, 2, 3, 4, 5] and len(found) == MAX_SUGGESTIONS
    movements = [movement(ident=n, recipient=BUYER) for n in range(1, 9)]
    assert [s.movement_id for s in rank_movements(invoice(name=BUYER), movements)] == [1, 2, 3, 4, 5]


def test_the_best_five_are_chosen_by_the_ranking_not_by_the_order_of_the_input():
    invoices = [invoice(ident=n, number=f'K-{n}000', name=BUYER, date=shifted(n - 9)) for n in range(1, 9)]      # die ID 8 liegt dem Datum am nächsten
    random.Random(7).shuffle(invoices)
    assert [s.invoice_id for s in rank_invoices(movement(recipient=BUYER), invoices)] == [8, 7, 6, 5, 4]


def test_the_distance_counts_in_both_directions():
    before = invoice(ident=3, number='K-3000', date=shifted(-3), name=BUYER)
    after = invoice(ident=4, number='K-4000', date=shifted(+3), name=BUYER)
    farther = invoice(ident=1, number='K-1000', date=shifted(+4), name=BUYER)
    found = rank_invoices(movement(recipient=BUYER), [farther, after, before])
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
    invoices = [invoice(ident=1, number='K-1001', date='2026-01-01', name=BUYER), invoice(ident=2, number='K-1002', date='2026-02-01', name=BUYER)]
    movements = [movement(ident=1, date='2026-01-10', recipient=BUYER), movement(ident=2, date='2026-02-10', recipient=BUYER)]
    return movements, invoices


def test_two_equal_invoices_and_two_equal_payments_pair_by_the_date():
    movements, invoices = two_and_two()
    result = pair_up(movements, invoices)
    assert pair_ids(result) == [(1, 1), (2, 2)]                                         # jede Rechnung bekommt die zeitlich nächste Zahlung
    assert [s.ambiguous for s in result] == [True, True]                                # … aber es wären mehrere möglich gewesen


def test_the_pairing_does_not_depend_on_the_order_of_the_input():
    movements, invoices = two_and_two()
    movements.append(movement(ident=3, date='2026-03-20', recipient=BUYER))
    invoices.append(invoice(ident=3, number='K-1003', date='2026-03-01', name=BUYER))
    expected = pair_up(movements, invoices)
    assert pair_ids(expected) == [(1, 1), (2, 2), (3, 3)]
    for permuted_movements in itertools.permutations(movements):
        for permuted_invoices in itertools.permutations(invoices):
            assert pair_up(list(permuted_movements), list(permuted_invoices)) == expected


def test_every_movement_and_every_invoice_is_used_at_most_once():
    invoices = [invoice(ident=n, number=f'K-{n}000', name=BUYER, date=shifted(-n)) for n in range(1, 6)]
    movements = [movement(ident=n, recipient=BUYER, date=shifted(n)) for n in range(1, 4)]
    result = pair_up(movements, invoices)
    assert len(result) == 3
    assert len({s.movement_id for s in result}) == 3 and len({s.invoice_id for s in result}) == 3
    result = pair_up([movement(ident=n, recipient=BUYER) for n in range(1, 6)], [invoice(name=BUYER)])
    assert len(result) == 1                                                             # eine Rechnung nimmt nur eine Zahlung


def test_a_number_beats_a_pair_that_only_has_the_amount_and_the_customer():
    invoices = [invoice(ident=1, number='K-1001', date=shifted(-1), name=BUYER), invoice(ident=2, number='R26002', date=shifted(-1), name=BUYER)]
    movements = [movement(ident=1, recipient=BUYER, text='Rechnung R26002'), movement(ident=2, recipient=BUYER)]
    result = pair_up(movements, invoices)
    assert [(s.movement_id, s.invoice_id, s.score) for s in result] == [(1, 2, 180), (2, 1, 80)]
    # Bewegung 2 hätte auch zur Rechnung 2 gepasst (80 Punkte, aber an Bewegung 1 vergeben): mehrdeutig; das Paar mit der Nummer ist es nicht
    assert [s.ambiguous for s in result] == [False, True]


def test_ambiguous_means_another_pair_with_the_same_points_for_the_movement_or_for_the_invoice():
    # eine Bewegung, zwei Rechnungen mit gleichen Punkten: die genommene Zeile ist mehrdeutig, auch wenn die zweite Rechnung leer ausgeht
    result = pair_up([movement(recipient=BUYER)], [invoice(ident=1, number='K-1001', name=BUYER), invoice(ident=2, number='K-1002', name=BUYER)])
    assert [(s.movement_id, s.invoice_id, s.ambiguous) for s in result] == [(1, 1, True)]
    # eine Rechnung, zwei Bewegungen
    result = pair_up([movement(ident=1, recipient=BUYER), movement(ident=2, recipient=BUYER)], [invoice(name=BUYER)])
    assert [(s.movement_id, s.invoice_id, s.ambiguous) for s in result] == [(1, 1, True)]
    # eine zweite Rechnung mit anderen Punkten macht die erste nicht mehrdeutig
    result = pair_up([movement(recipient=BUYER, text='R26001')], [invoice(ident=1, number='R26001', name=BUYER), invoice(ident=2, number='K-1002', name=BUYER)])
    assert [(s.invoice_id, s.score, s.ambiguous) for s in result] == [(1, 180, False)]
    # ein einziges Paar
    assert [s.ambiguous for s in pair_up([movement(recipient=BUYER)], [invoice(name=BUYER)])] == [False]


def test_a_linked_pair_is_never_a_suggestion_in_the_overview():
    movements = [movement(ident=1, text='R26001', recipient=BUYER)]
    invoices = [invoice(ident=1, number='R26001', name=BUYER), invoice(ident=2, number='K-1002', name=BUYER)]
    assert pair_ids(pair_up(movements, invoices)) == [(1, 1)]
    assert pair_ids(pair_up(movements, invoices, linked=frozenset({(1, 1)}))) == [(1, 2)]        # die Bewegung geht an die andere Rechnung
    assert pair_up(movements, invoices[:1], linked=frozenset({(1, 1)})) == []


def test_the_result_is_sorted_by_points_then_the_distance_then_the_ids():
    invoices = [invoice(ident=1, number='K-1001', date=shifted(-10), name=BUYER),
                invoice(ident=2, number='R26002', date=shifted(-1), name=OTHER, open_minor=999)]
    movements = [movement(ident=1, recipient=BUYER), movement(ident=2, text='R26002', amount=1)]
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
    good_movement = movement(text='R26001', recipient=BUYER, number='R26001', contact=7)         # auch Beleg-Nr. und Kontakt: Sie werden nicht gelesen, dürfen aber nie stören
    good_invoice = invoice(name=BUYER, customer=7)
    for key in MOVEMENT_KEYS:                                       # ein kaputtes Feld, die übrigen heil – und alle zugleich kaputt
        for broken in (dict(good_movement, **{key: value}), dict.fromkeys(MOVEMENT_KEYS, value)):
            for result in (rank_invoices(broken, [good_invoice]), rank_movements(good_invoice, [broken]), pair_up([broken], [good_invoice])):
                assert isinstance(result, list)
    for key in INVOICE_KEYS:
        for broken in (dict(good_invoice, **{key: value}), dict.fromkeys(INVOICE_KEYS, value)):
            for result in (rank_invoices(good_movement, [broken]), rank_movements(broken, [good_movement]), pair_up([good_movement], [broken])):
                assert isinstance(result, list)


@pytest.mark.parametrize('value', [None, '', 'text', 1.5, 1.0, True, b'bytes', [], float('nan')], ids=stable_id)
def test_a_row_without_a_whole_id_is_no_candidate(value):
    good_invoice, good_movement = invoice(name=BUYER), movement(recipient=BUYER)
    bad_id, bad_invoice_id = dict(good_movement, id=value), dict(good_invoice, id=value)
    assert pair_ids(rank_invoices(good_movement, [good_invoice])) == [(1, 1)]                    # Kontrolle: Die heilen Zeilen sind ein Vorschlag – nur die ID nimmt ihn
    assert rank_invoices(bad_id, [good_invoice]) == [] and rank_movements(good_invoice, [bad_id]) == []
    assert rank_invoices(good_movement, [bad_invoice_id]) == [] and rank_movements(bad_invoice_id, [good_movement]) == []
    assert pair_up([bad_id], [good_invoice]) == [] and pair_up([good_movement], [bad_invoice_id]) == []


@pytest.mark.parametrize('value', [None, '', 'text', 0, -5, 1.5, 1190000.0, True, b'bytes', [], float('nan')], ids=stable_id)
def test_an_amount_that_is_no_positive_whole_number_makes_the_row_no_candidate(value):
    good_invoice, good_movement = invoice(name=BUYER), movement(recipient=BUYER)
    bad_amount, bad_open = dict(good_movement, amount_minor=value), dict(good_invoice, open_minor=value)
    assert pair_ids(rank_invoices(good_movement, [good_invoice])) == [(1, 1)]                    # Kontrolle: Die heilen Zeilen sind ein Vorschlag – nur der Betrag nimmt ihn
    assert pair_ids(rank_invoices(dict(good_movement, text='R26001'), [good_invoice])) == [(1, 1)]
    assert rank_invoices(bad_amount, [good_invoice]) == [] and rank_movements(good_invoice, [bad_amount]) == []
    assert rank_invoices(good_movement, [bad_open]) == [] and rank_movements(bad_open, [good_movement]) == []
    assert pair_up([bad_amount], [good_invoice]) == [] and pair_up([good_movement], [bad_open]) == []
    # auch mit der Nummer im Text: ohne brauchbaren Betrag lässt sich nichts zuordnen
    assert rank_invoices(dict(good_movement, text='R26001', amount_minor=value), [good_invoice]) == []


@pytest.mark.parametrize('amount', [0, -1, -OPEN])
def test_a_payment_or_an_invoice_without_a_positive_rest_is_no_candidate(amount):
    assert pair_ids(rank_invoices(movement(recipient=BUYER), [invoice(name=BUYER)])) == [(1, 1)]      # Kontrolle: Mit einem positiven Rest wäre es ein Vorschlag
    assert rank_invoices(movement(amount=amount, recipient=BUYER), [invoice(open_minor=amount, name=BUYER)]) == []
    assert rank_invoices(movement(amount=amount, text='R26001'), [invoice()]) == []
    assert rank_invoices(movement(text='R26001'), [invoice(open_minor=amount)]) == []


@pytest.mark.parametrize('row', [None, 5, 'zeile', [], (1, 2), object()], ids=stable_id)
def test_a_row_that_is_no_dict_is_skipped(row):
    assert rank_invoices(movement(recipient=BUYER), [row, invoice(name=BUYER)])[0].invoice_id == 1
    assert rank_movements(invoice(name=BUYER), [row, movement(recipient=BUYER)])[0].movement_id == 1
    assert rank_invoices(row, [invoice(name=BUYER)]) == [] and rank_movements(row, [movement(recipient=BUYER)]) == []
    assert pair_ids(pair_up([row, movement(recipient=BUYER)], [row, invoice(name=BUYER)])) == [(1, 1)]


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
    ('R2600' + '\n') * 700, 'RE 20 260042 ' * 700, '26.001,00 ' * 900, '2026 0042-' * 800,
], ids=['Bindestriche', 'Leerzeichen', 'Strichreihe', 'Wiederholungen', 'Sperrtreffer', 'Doppelleerraum', 'Ziffern-davor', 'Zeilenumbrüche',
        'Fugen-Fehlgriffe', 'Beträge', 'Nummernreihe'])
def test_a_long_hostile_text_is_read_in_linear_time(text):
    invoices = [invoice(), invoice(ident=2, number='26R001'), invoice(ident=3, number='RE-2026/0042'), invoice(ident=4, number='R' + '1' * 39),
                invoice(ident=5, number='26001'), invoice(ident=6, number='2026-0042'), invoice(ident=7, number='20260042')]
    started = time.perf_counter()
    for _ in range(20):
        rank_invoices(movement(amount=1, text=text, number=text, recipient=text), invoices)
    assert time.perf_counter() - started < 5                                           # gemessen: Bruchteile einer Sekunde


def test_five_hundred_by_five_hundred_is_fast_enough_for_a_page():
    invoices = [invoice(ident=n, number=f'26R{n:05d}', open_minor=1000 * n, name=f'Firma{n % 50:02d}') for n in range(1, 501)]
    filler = 'SEPA-Überweisung Verwendungszweck Vielen Dank für Ihren Auftrag mit freundlichen Grüßen Musterfirma Referenz 20261112 Mandat 4711 '
    movements = [movement(ident=n, amount=1000 * n if n % 2 else 777, recipient=f'Firma{n % 50:02d}', text=filler * 2 + f'Rechnungs-Nr 26R{n:05d} Danke')
                 for n in range(1, 501)]
    started = time.perf_counter()
    result = pair_up(movements, invoices)
    assert time.perf_counter() - started < 3                                            # gemessen: unter 0,2 s – die Grenze ist großzügig
    assert len(result) == 500 and all(s.invoice_id == s.movement_id for s in result)   # jede Zahlung findet ihre Nummer


# ── Ein Punkt zwischen zwei Ziffern gilt nur, wo die Nummer selbst einen trägt ──
@pytest.mark.parametrize('raw, text, found', [
    ('26/001', 'Miete 26.001,00 EUR', False),          # die Fuge der Nummer nimmt keinen Tausenderpunkt
    ('26/001', 'Rechnung 26/001', True),
    ('26/001', 'Rechnung 26-001 danke', True),
    ('26/001', 'Rechnung 26 001', True),
    ('2026-0042', 'Betrag 2026.0042', False),
    ('26.001', 'Rechnung 26.001', True),               # die Nummer trägt den Punkt selbst
    ('26.001', 'Rechnung 26/001', True),
], ids=['betrag', 'schraegstrich', 'bindestrich', 'leerraum', 'punkt-statt-strich', 'eigener-punkt', 'eigener-punkt-anders'])
def test_a_dot_between_two_digits_counts_only_where_the_number_itself_has_one(raw, text, found):
    from db.payment_matching import _number_parts, _number_pattern
    number, joints = _number_parts(raw)
    assert (_number_pattern(number, joints).search(text) is not None) is found


# ── EINDEUTIG: was „Alle eindeutigen zuordnen“ zuordnen darf ─────────────────

def a_suggestion(reasons, **kwargs):
    return PaymentSuggestion(1, 2, 180, tuple(reasons), 0, **kwargs)


@pytest.mark.parametrize('reasons, kwargs, expected', [
    ((REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER), {}, True),
    ((REASON_NUMBER, REASON_AMOUNT), {}, True),                                              # der Kunde ist nicht verlangt
    ((REASON_AMOUNT, REASON_CUSTOMER), {}, False),                                           # Betrag und Kunde ohne Nummer
    ((REASON_NUMBER, REASON_CUSTOMER, REASON_PARTIAL), {}, False),                           # die Nummer mit abweichendem Betrag (Teil- oder Überzahlung)
    ((REASON_NUMBER, REASON_SKONTO, REASON_CUSTOMER), {'skonto': True}, False),              # der Betrag nach Skonto
    ((REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER), {'skonto': True}, False),              # das Merkmal allein schließt aus
    ((REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER), {'ambiguous': True}, False),           # mehrdeutig
    ((REASON_NUMBER,), {}, False),
    ((REASON_AMOUNT,), {}, False),
    ((), {}, False),
], ids=['Nummer-Betrag-Kunde', 'Nummer-Betrag', 'ohne-Nummer', 'Teilzahlung', 'Skonto-Begruendung', 'Skonto-Merkmal', 'mehrdeutig', 'nur-Nummer', 'nur-Betrag', 'leer'])
def test_only_the_number_and_the_amount_make_a_suggestion_certain(reasons, kwargs, expected):
    assert is_certain(a_suggestion(reasons, **kwargs)) is expected


def test_the_suggestions_of_the_ranking_are_certain_only_with_the_number_and_the_amount():
    number_and_amount = rank_invoices(movement(text='Rechnung R26001'), [invoice()])[0]
    assert number_and_amount.reasons == (REASON_NUMBER, REASON_AMOUNT) and is_certain(number_and_amount) is True
    with_customer = rank_invoices(movement(text='Rechnung R26001', recipient=BUYER), [invoice(name=BUYER)])[0]
    assert with_customer.reasons == (REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER) and is_certain(with_customer) is True
    amount_and_customer = rank_invoices(movement(recipient=BUYER), [invoice(name=BUYER)])[0]
    assert amount_and_customer.reasons == (REASON_AMOUNT, REASON_CUSTOMER) and is_certain(amount_and_customer) is False
    partial = rank_invoices(movement(amount=500, text='Rechnung R26001'), [invoice()])[0]
    assert REASON_PARTIAL in partial.reasons and is_certain(partial) is False
    after_discount = rank_invoices(movement(amount=AFTER_DISCOUNT, text='Rechnung R26001'), [invoice(skonto_open=AFTER_DISCOUNT)])[0]
    assert after_discount.skonto and is_certain(after_discount) is False


def test_a_pair_of_the_overview_is_certain_unless_it_is_ambiguous():
    movements = [movement(ident=1, text='Rechnung R26001'), movement(ident=2, text='Rechnung R26002', recipient=BUYER)]
    invoices = [invoice(ident=1, number='R26001'), invoice(ident=2, number='R26002', name=BUYER)]
    assert [is_certain(s) for s in pair_up(movements, invoices)] == [True, True]
    # zwei gleich hohe Zahlungen nennen dieselbe Rechnung: Sie bekommt eine, und das Paar ist mehrdeutig – dann ist es nie eindeutig
    twins = pair_up([movement(ident=1, text='R26001'), movement(ident=2, text='R26001')], [invoice()])
    assert [(s.movement_id, s.ambiguous, is_certain(s)) for s in twins] == [(1, True, False)]
    # Betrag und Kunde ohne die Nummer im Text: ein Vorschlag, aber nicht eindeutig
    assert [is_certain(s) for s in pair_up([movement(recipient=BUYER)], [invoice(name=BUYER)])] == [False]


def test_a_certain_suggestion_needs_the_text_of_the_bank_not_the_document_number_of_the_bank_row():
    """Die Beleg-Nr. der Bankzeile zählt nie (``test_the_number_in_the_document_number_of_the_bank_row_is_no_evidence``) – also macht sie auch nichts eindeutig."""
    found = rank_invoices(movement(recipient=BUYER, number='R26001'), [invoice(name=BUYER)])
    assert [(s.reasons, is_certain(s)) for s in found] == [((REASON_AMOUNT, REASON_CUSTOMER), False)]
