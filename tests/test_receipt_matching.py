# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zu einem gelesenen Beleg die passenden Buchungen vorschlagen (2.6.0) – nur lesen, nichts verknüpfen.

Drei Teile:

* ``db/receipt_matching.py`` (rein): Fenster, Normalisierung, Namenswörter und die Rangfolge ``rank_candidates`` – Betrag ohne Vorzeichen
  als Voraussetzung, Punkte für Rechnungsnummer, Name und Datum, verknüpfte Buchungen zuletzt, höchstens fünf, stabil bei Gleichstand,
  schiefe Werte werfen nie, die Laufzeit hängt nicht von Kandidaten × Belegtext ab.
* ``Database.receipt_match_candidates``: EINE lesende Abfrage mit allen Ausschlüssen (falscher Betrag, außerhalb des Fensters, Kind einer
  Bankbewegung, Zahlungssatz, Spiegel, schon mit diesem Beleg verknüpft), ``linked`` an der Zeile und über ein Kind, ``row`` in der Form
  von ``search_bookings``.
* ``Database.suggest_bookings_for_receipt``: Rundlauf mit gespeicherter Lesung, das Fenster aus dem gelesenen Datum, sonst dem Belegdatum.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; Daten stehen fest im Test (kein „heute“).
"""
import datetime
import sqlite3
import time

import pytest

from db import Database
from db import receipt_matching
from db.bookings import RELATION_DEFAULT
from db.document_readings import READING_COLUMNS
from db.invoice_payments import PAYMENT_MARK
from db.matching import MIRROR_FORMS
from db.receipt_matching import (
    MAX_CANDIDATES, MAX_FIELD_LENGTH, MAX_SUGGESTIONS, MIN_NUMBER_LENGTH, MIN_NUMERIC_NUMBER_LENGTH, MIN_WORD_LENGTH,
    REASON_AMOUNT, REASON_DIRECTION, REASON_LINKED, REASON_NAME, REASON_NUMBER, REASON_SAME_DAY, STOP_WORDS, WINDOW_DAYS_AFTER,
    WINDOW_DAYS_BEFORE, Suggestion, candidate_window, direction_fits, name_words, normalize_number, rank_candidates, usable_gross)
from money import to_minor
from server.receipt_links import booking_payload, suggestions_payload
from tests import installation
from tests.document_helpers import add_document
from tests.entry_form_helpers import bank_account, coa, query

DAY = '2026-03-04'                          # das Belegdatum der Tests
WINDOW = ('2026-02-25', '2026-06-02')       # sein Fenster: 7 Tage davor bis 90 Tage danach
GROSS = 1190000                             # 119,00 EUR in Minor Units (money.SCALE = 4)
FILE = 'Belege/2026/B-1_beleg.pdf'


# ── Hilfen ───────────────────────────────────────────────────────────────────

def shifted(days, start=DAY):
    """Das Datum ``days`` Tage nach (negativ: vor) ``start`` als ISO-Text."""
    return (datetime.date.fromisoformat(start) + datetime.timedelta(days=days)).isoformat()


def reading(**over):
    """Eine Lesung (Schlüssel = Spalten von ``DocumentReadings``) über 119,00 EUR; ``over`` überschreibt Felder."""
    row = {'Status': 'ok', 'Source': 'text', 'ReaderVersion': 1, 'ReadAt': '2026-03-05 10:00:00', 'DocDate': DAY,
           'GrossMinor': GROSS, 'NetMinor': None, 'TaxMinor': None, 'TaxRate': None, 'Currency': 'EUR', 'SenderName': None,
           'SenderVatId': None, 'InvoiceNumber': None, 'Kind': 'invoice', 'Raw': None, 'Text': None}
    row.update(over)
    return row


def candidate(booking_id=1, date=DAY, amount=-GROSS, **over):
    """Ein Kandidat für ``rank_candidates`` (die Ausgabe von 119,00 EUR am Belegdatum), ohne weitere Merkmale."""
    row = {'id': booking_id, 'date': date, 'amount': amount, 'text': '', 'recipient': '', 'number': '', 'contact_name': '',
           'linked': False}
    row.update(over)
    return row


def rank(reading_row, candidates, doc_date=DAY):
    return rank_candidates(reading_row, candidates, doc_date)


def ids(suggestions):
    return [suggestion.booking_id for suggestion in suggestions]


def add_receipt(db, number='B-1', date=DAY, path=FILE):
    """Einen Beleg anlegen; liefert seine ID."""
    db.insert_receipt(number, date, 'beleg.pdf', path, '')
    return db.fetch_receipts()[-1][0]


def add_booking(db, date_booking=DAY, amount='-119.00', **fields):
    """Eine Buchung (ein eigenständiger Satz, wenn nicht anders genannt); liefert ihre ID."""
    return db.insert_booking(date_booking, amount, **fields)


def add_bank(db, date_booking=DAY, amount='-119.00', **fields):
    return db.insert_booking(date_booking, amount, booking_type='bank', **fields)


def run_sql(db, sql, *args):
    conn = sqlite3.connect(db.db_name)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def candidates_of(db, receipt, *, gross=GROSS, window=WINDOW, **options):
    return db.receipt_match_candidates(receipt, gross, *window, **options)


def candidate_ids(db, receipt, **options):
    return [found['id'] for found in candidates_of(db, receipt, **options)]


# ── Die festgelegten Werte ───────────────────────────────────────────────────

def test_the_constants_and_the_values_the_query_relies_on_are_the_planned_ones():
    assert (WINDOW_DAYS_BEFORE, WINDOW_DAYS_AFTER, MAX_SUGGESTIONS, MIN_NUMBER_LENGTH, MAX_CANDIDATES) == (7, 90, 5, 4, 200)
    assert MIN_NUMERIC_NUMBER_LENGTH == 5 and MIN_WORD_LENGTH == 4 and MAX_FIELD_LENGTH == 2000
    assert RELATION_DEFAULT == 'receipt' and PAYMENT_MARK == 'invoice_payment'          # woran die Abfrage „Beleg“ und „Zahlungssatz“ erkennt
    assert set(MIRROR_FORMS) == {'mirror_coa', 'mirror_counter', 'mirror_legacy'}


def test_the_reasons_are_fixed_german_sentences():
    assert [REASON_AMOUNT, REASON_NUMBER, REASON_NAME, REASON_SAME_DAY, REASON_DIRECTION, REASON_LINKED] == [
        'Betrag stimmt', 'Rechnungsnummer im Buchungstext', 'Name passt', 'am Belegdatum', 'Zahlungsrichtung passt nicht zur Belegart',
        'bereits mit einem Beleg verknüpft']


# ── candidate_window ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('day, window', [
    (DAY, WINDOW), ('2026-01-05', ('2025-12-29', '2026-04-05')), ('2024-02-29', ('2024-02-22', '2024-05-29')),
    ('1900-01-01', ('1899-12-25', '1900-04-01')), ('2200-12-31', ('2200-12-24', '2201-03-31'))])
def test_the_window_runs_from_seven_days_before_to_ninety_days_after(day, window):
    assert candidate_window(day) == window


@pytest.mark.parametrize('value', [None, '', '2026-13-40', '2026-02-30', '2026-3-4', '20260304', ' 2026-03-04', '2026-03-04 ',
                                   '2026-03-04T10:00:00', '2026-03-04 10:00', '1899-12-31', '2201-01-01', '２０２６-03-04',
                                   20260304, datetime.date(2026, 3, 4), b'2026-03-04'], ids=repr)
def test_anything_but_a_strict_iso_date_has_no_window(value):
    assert candidate_window(value) is None


# ── normalize_number / name_words ────────────────────────────────────────────

def test_numbers_compare_without_case_separators_and_blanks():
    assert normalize_number('RE-2026/0042') == normalize_number('re 2026 0042') == normalize_number(' Re–2026.0042 ') == 're20260042'
    assert normalize_number('Straße 5') == 'strasse5'                               # casefold: ß wird ss
    assert normalize_number('Ä-Ö-Ü 12') == '12'                                     # nur [a-z0-9] bleibt
    assert normalize_number('') == normalize_number('---') == normalize_number(None) == ''
    assert normalize_number(4711) == '4711'                                         # eine Zahl statt Text wirft nicht


def test_name_words_keep_words_of_four_letters_and_digits_and_drop_the_rest():
    assert name_words('Müller & Söhne GmbH') == {'müller', 'söhne'}
    assert name_words('Mercedes-Benz Niederlassung') == {'mercedes', 'benz', 'niederlassung'}
    assert name_words('Firma_Probe') == {'firma', 'probe'}                          # der Unterstrich trennt
    assert name_words('Straße') == {'strasse'}                                      # casefold
    assert name_words('abc abcd') == {'abcd'} and MIN_WORD_LENGTH == 4              # drei Zeichen zählen nicht, vier schon
    assert name_words('Rechnung2026 a1b2 1234 20260304 12,50') == {'rechnung2026', 'a1b2'}    # reine Zahlen fallen weg
    assert name_words(None) == name_words('') == name_words(5) == frozenset()


def test_stop_words_and_legal_forms_are_no_names():
    text = 'GmbH mbH OHG GbR Inc Ltd Rechnung Rechnungsnummer Kunde Kundennummer Betrag Datum Zahlung Lastschrift Überweisung ' \
           'UEBERWEISUNG Gutschrift SEPA IBAN Vielen Dank'
    assert name_words(text) == frozenset()
    assert name_words(text + ' Pinsel') == {'pinsel'}
    assert all(word == word.casefold() for word in STOP_WORDS)                      # so, wie ``name_words`` sie vergleicht


# ── rank_candidates: Voraussetzungen ─────────────────────────────────────────

@pytest.mark.parametrize('over', [
    {'GrossMinor': None}, {'GrossMinor': 0}, {'GrossMinor': '1190000'}, {'GrossMinor': 1190000.0}, {'GrossMinor': True},
    {'Currency': 'USD'}, {'Currency': 'usd'}, {'Currency': 'CHF'},
    {'Status': 'empty'}, {'Status': 'partial'}, {'Status': None}, {'Status': 'OK'}], ids=repr)
def test_a_reading_that_cannot_be_searched_gives_no_suggestions(over):
    row = reading(**over)
    assert usable_gross(row) is None
    assert rank(row, [candidate()]) == []


@pytest.mark.parametrize('row', [None, [], 'text', 5], ids=repr)
def test_no_reading_gives_no_suggestions(row):
    assert usable_gross(row) is None and rank(row, [candidate()]) == []


@pytest.mark.parametrize('currency', [None, '', 'EUR', 'eur', ' EUR '], ids=repr)
def test_an_empty_or_euro_currency_can_be_searched_and_the_sign_of_the_gross_does_not_matter(currency):
    assert usable_gross(reading(Currency=currency)) == GROSS
    assert usable_gross(reading(Currency=currency, GrossMinor=-GROSS)) == -GROSS       # Gutschrift: das Vorzeichen bleibt


def test_only_the_amount_without_its_sign_has_to_match():
    wrong = [candidate(1, amount=GROSS + 1), candidate(2, amount=-GROSS + 1), candidate(3, amount=0), candidate(4, amount=GROSS * 10)]
    right = [candidate(5, amount=-GROSS), candidate(6, amount=GROSS)]                # eine Ausgabe wie eine Einnahme von 119,00
    assert ids(rank(reading(), wrong + right)) == [5, 6]
    assert ids(rank(reading(GrossMinor=-GROSS), wrong + right)) == [5, 6]            # und umgekehrt: ein negativer Beleg passt zu +119,00
    assert rank(reading(), wrong) == []


def test_the_amount_alone_is_a_precondition_not_points():
    assert rank(reading(), [candidate(date=None)], None) == [Suggestion(1, 0, (REASON_AMOUNT,), False, None)]


# ── rank_candidates: Datum ───────────────────────────────────────────────────

@pytest.mark.parametrize('delta, points, reason', [
    (0, 20, 'am Belegdatum'), (1, 20, '1 Tag nach dem Belegdatum'), (2, 20, '2 Tage nach dem Belegdatum'),
    (3, 19, '3 Tage nach dem Belegdatum'), (30, 10, '30 Tage nach dem Belegdatum'), (59, 1, '59 Tage nach dem Belegdatum'),
    (60, 0, '60 Tage nach dem Belegdatum'), (75, 0, '75 Tage nach dem Belegdatum'), (90, 0, '90 Tage nach dem Belegdatum'),
    (-1, 20, '1 Tag vor dem Belegdatum'), (-2, 20, '2 Tage vor dem Belegdatum'), (-5, 19, '5 Tage vor dem Belegdatum'),
    (-7, 18, '7 Tage vor dem Belegdatum')])
def test_a_booking_inside_the_window_scores_by_its_distance_to_the_date_of_the_receipt(delta, points, reason):
    assert rank(reading(), [candidate(date=shifted(delta))]) == [Suggestion(1, points, (REASON_AMOUNT, reason), False, delta)]


@pytest.mark.parametrize('delta', [-8, -30, 91, 400])
def test_outside_the_window_there_are_no_points_and_no_date_reason_but_the_distance_is_known(delta):
    assert rank(reading(), [candidate(date=shifted(delta))]) == [Suggestion(1, 0, (REASON_AMOUNT,), False, delta)]


def test_the_edges_of_the_window_are_minus_seven_and_plus_ninety_days():
    inside = [candidate(1, date=shifted(-WINDOW_DAYS_BEFORE)), candidate(2, date=shifted(WINDOW_DAYS_AFTER))]
    outside = [candidate(3, date=shifted(-WINDOW_DAYS_BEFORE - 1)), candidate(4, date=shifted(WINDOW_DAYS_AFTER + 1))]
    got = {suggestion.booking_id: suggestion for suggestion in rank(reading(), inside + outside)}
    assert [len(got[number].reasons) for number in (1, 2, 3, 4)] == [2, 2, 1, 1]      # nur die ersten beiden tragen eine Datumsbegründung
    assert candidate_window(DAY) == (shifted(-WINDOW_DAYS_BEFORE), shifted(WINDOW_DAYS_AFTER))   # dieselben Grenzen wie das Fenster der Abfrage


@pytest.mark.parametrize('doc_date', [None, '', '2026-13-40', '2026-3-4', 20260304, datetime.date(2026, 3, 4)], ids=repr)
def test_without_a_valid_date_of_the_receipt_there_are_no_date_points(doc_date):
    assert rank(reading(), [candidate()], doc_date) == [Suggestion(1, 0, (REASON_AMOUNT,), False, None)]


@pytest.mark.parametrize('booked', [None, '', '2026-13-40', '2026-3-4', '04.03.2026', 20260304, 4.5, datetime.date(2026, 3, 4)], ids=repr)
def test_without_a_valid_date_of_the_booking_there_are_no_date_points(booked):
    assert rank(reading(), [candidate(date=booked)]) == [Suggestion(1, 0, (REASON_AMOUNT,), False, None)]


# ── rank_candidates: Rechnungsnummer ─────────────────────────────────────────

def test_the_invoice_number_counts_in_the_text_the_booking_number_or_the_recipient_whatever_the_separators():
    row = reading(InvoiceNumber='RE-2026/0042')
    for field in ('text', 'number', 'recipient'):
        found = rank(row, [candidate(**{field: 'Zahlung re 2026 0042 danke'})], None)
        assert found == [Suggestion(1, 100, (REASON_AMOUNT, REASON_NUMBER), False, None)], field
    assert rank(row, [candidate(text='RE.2026.0042', number='re20260042', recipient='RE 2026 0042')], None)[0].score == 100   # einmal, nicht dreimal
    assert rank(row, [candidate(text='Zahlung RE-2026-0043')], None)[0].score == 0
    assert rank(row, [candidate(contact_name='RE-2026/0042')], None)[0].score == 0         # der Kontaktname trägt keine Nummer


def test_a_number_without_a_digit_or_shorter_than_four_characters_proves_nothing():
    assert MIN_NUMBER_LENGTH == 4
    assert rank(reading(InvoiceNumber='R-12'), [candidate(text='Zahlung r12')], None)[0].score == 0          # nur drei Zeichen
    assert rank(reading(InvoiceNumber='R-123'), [candidate(text='Zahlung r123')], None)[0].score == 100      # vier genügen
    assert rank(reading(InvoiceNumber='ABC-DEF'), [candidate(text='Zahlung abcdef')], None)[0].score == 0    # keine Ziffer
    for empty in (None, '', '---', '  '):
        assert rank(reading(InvoiceNumber=empty), [candidate(text='Zahlung 2026')], None)[0].score == 0


@pytest.mark.parametrize('number, text, points', [
    ('2026', 'Zahlung vom 04.03.2026', 0), ('2026', 'Rechnung 2026', 0),               # rein numerisch und nur vier Ziffern: kein Beweis
    ('1234', 'Rechnung 1234', 0), ('0000', 'Zahlung 0000', 0),
    ('20260', 'Rechnung 20260', 100), ('12345', 'Zahlung 12345 vielen Dank', 100),     # fünf Ziffern genügen
    ('2026-1', 'Zahlung 2026 1', 100), ('2026/0042', 'Zahlung 20260042', 100),         # Trenner zählen nicht: normalisiert fünf und mehr Ziffern
    ('R2026', 'Zahlung r2026', 100), ('R-202', 'Zahlung R 202', 100),                  # mit Buchstaben genügen vier Zeichen
    ('A-12B', 'Zahlung a12b', 100), ('R-12', 'Zahlung r12', 0)])                       # … aber nicht drei
def test_a_purely_numeric_number_needs_five_digits_and_one_with_letters_four_characters(number, text, points):
    assert (MIN_NUMBER_LENGTH, MIN_NUMERIC_NUMBER_LENGTH) == (4, 5)
    assert rank(reading(InvoiceNumber=number), [candidate(text=text)], None)[0].score == points


def test_a_year_in_the_purpose_does_not_make_every_receipt_numbered_with_that_year_a_match():
    """Fehlfall: Die Nummer ``2026`` stand als Beweis in jedem Verwendungszweck mit einer Jahreszahl."""
    purposes = ['Zahlung vom 04.03.2026', 'Lastschrift 2026-03 Strom', 'Beitrag 2026', 'REF 12026X']
    found = rank(reading(InvoiceNumber='2026'), [candidate(number, text=purpose) for number, purpose in enumerate(purposes, 1)], None)
    assert [suggestion.score for suggestion in found] == [0, 0, 0, 0]
    assert all(REASON_NUMBER not in suggestion.reasons for suggestion in found)


def test_the_booking_text_is_read_only_up_to_two_thousand_characters():
    row = reading(InvoiceNumber='RE-2026/0042')
    number = ' RE-2026/0042'
    ends_at_the_edge = 'x' * (MAX_FIELD_LENGTH - len(number)) + number                 # genau 2000 Zeichen: die Nummer liegt ganz darin
    straddles_the_edge = 'x' * (MAX_FIELD_LENGTH - 5) + number                         # der Rand schneidet die Nummer durch
    behind_the_edge = 'x' * MAX_FIELD_LENGTH + number
    assert rank(row, [candidate(text=ends_at_the_edge)], None)[0].score == 100
    assert rank(row, [candidate(text=straddles_the_edge)], None)[0].score == 0
    assert rank(row, [candidate(text=behind_the_edge)], None)[0].score == 0


# ── rank_candidates: Name ────────────────────────────────────────────────────

def test_one_common_name_word_scores_thirty_and_two_or_more_forty():
    row = reading(SenderName='Lieferant Probe GmbH')
    one = rank(row, [candidate(recipient='LIEFERANT Beispiel AG')], None)
    two = rank(row, [candidate(recipient='Probe Lieferant')], None)
    many = rank(reading(SenderName='Lieferant Probe Malerbetrieb Pinsel'), [candidate(recipient='Pinsel Malerbetrieb Probe Lieferant')], None)
    assert one == [Suggestion(1, 30, (REASON_AMOUNT, REASON_NAME), False, None)]
    assert two == [Suggestion(1, 40, (REASON_AMOUNT, REASON_NAME), False, None)]
    assert many[0].score == 40                                                      # nicht mehr als 40, wie viele es auch sind


def test_the_name_of_the_booking_is_the_recipient_and_the_name_of_its_contact_and_the_receipt_counts_with_its_text_too():
    row = reading(SenderName='Lieferant Probe GmbH')
    assert rank(row, [candidate(contact_name='Probe Lieferant')], None)[0].score == 40
    assert rank(row, [candidate(recipient='Lieferant', contact_name='Probe')], None)[0].score == 40
    assert rank(row, [candidate(text='Probe Lieferant')], None)[0].score == 0                # der Buchungstext ist kein Name
    from_text = reading(SenderName=None, Text='Rechnung von Pinsel Malerbetrieb, Zahlbar in 14 Tagen')
    assert rank(from_text, [candidate(recipient='Malerbetrieb Pinsel')], None)[0].score == 40


def test_stop_words_short_words_and_numbers_are_no_name():
    row = reading(SenderName='Rechnung Zahlung GmbH Dr. AG 12345', Text='Betrag Datum Überweisung SEPA 2026')
    assert rank(row, [candidate(recipient='GmbH Rechnung Zahlung Dr AG 12345 Betrag Datum Überweisung SEPA 2026')], None)[0].score == 0
    assert rank(reading(SenderName=None, Text=None), [candidate(recipient='Lieferant Probe')], None)[0].score == 0   # nichts zu vergleichen


def test_the_signals_add_up():
    row = reading(SenderName='Lieferant Probe GmbH', InvoiceNumber='RE-2026-17')
    booking = candidate(date=shifted(6), recipient='Lieferant Probe GmbH', text='Zahlung RE-2026-17')
    assert rank(row, [booking]) == [Suggestion(
        1, 100 + 40 + 18, (REASON_AMOUNT, REASON_NUMBER, REASON_NAME, '6 Tage nach dem Belegdatum'), False, 6)]


# ── rank_candidates: Reihenfolge ─────────────────────────────────────────────

def test_a_booking_that_already_carries_a_receipt_comes_last_and_says_so():
    row = reading(SenderName='Lieferant Probe GmbH', InvoiceNumber='RE-2026-17')
    best_but_linked = candidate(1, recipient='Lieferant Probe', text='RE-2026-17', linked=True)
    plain = candidate(2, date=shifted(200))
    some = candidate(3, recipient='Lieferant Beispiel')
    found = rank(row, [best_but_linked, plain, some])
    assert ids(found) == [3, 2, 1]
    assert found[-1].linked is True and found[-1].reasons[-1] == REASON_LINKED and found[-1].score == 100 + 40 + 20
    assert [suggestion.linked for suggestion in found[:2]] == [False, False]
    assert REASON_LINKED not in found[0].reasons + found[1].reasons


# ── rank_candidates: Zahlungsrichtung ────────────────────────────────────────

@pytest.mark.parametrize('kind, amount, fits', [
    ('invoice', -GROSS, True), ('invoice', GROSS, False), (None, -GROSS, True), (None, GROSS, False), ('', -GROSS, True), ('', GROSS, False),
    ('credit_note', GROSS, True), ('credit_note', -GROSS, False),
    ('etwas Anderes', GROSS, True), ('etwas Anderes', -GROSS, True),                      # eine unbekannte Art schließt nichts aus
    ('invoice', None, True), ('invoice', '119', True), ('invoice', 1.5, True), ('credit_note', True, True)], ids=repr)
def test_the_kind_of_the_receipt_expects_a_direction_and_what_is_no_number_fits(kind, amount, fits):
    assert direction_fits(kind, amount) is fits


@pytest.mark.parametrize('kind', ['invoice', None, ''], ids=repr)
def test_an_invoice_or_a_receipt_without_a_kind_expects_an_outgoing_payment(kind):
    found = rank(reading(Kind=kind), [candidate(1, amount=GROSS), candidate(2, amount=-GROSS)])
    assert ids(found) == [2, 1] and [suggestion.mismatch for suggestion in found] == [False, True]
    assert found[0].reasons == (REASON_AMOUNT, REASON_SAME_DAY)
    assert found[1].reasons == (REASON_AMOUNT, REASON_SAME_DAY, REASON_DIRECTION) and found[1].score == found[0].score == 20


@pytest.mark.parametrize('gross', [GROSS, -GROSS], ids=repr)
def test_a_credit_note_expects_an_incoming_payment_whatever_the_sign_of_its_gross(gross):
    found = rank(reading(Kind='credit_note', GrossMinor=gross), [candidate(1, amount=-GROSS), candidate(2, amount=GROSS)])
    assert ids(found) == [2, 1] and [suggestion.mismatch for suggestion in found] == [False, True]
    assert REASON_DIRECTION not in found[0].reasons and found[1].reasons[-1] == REASON_DIRECTION


def test_an_unknown_kind_marks_no_direction_as_wrong():
    found = rank(reading(Kind='sonstiges'), [candidate(1, amount=GROSS), candidate(2, amount=-GROSS)])
    assert ids(found) == [1, 2] and [suggestion.mismatch for suggestion in found] == [False, False]
    assert all(REASON_DIRECTION not in suggestion.reasons for suggestion in found)


def test_a_wrong_direction_comes_after_every_suggestion_with_the_right_one_whatever_the_points():
    row = reading(SenderName='Lieferant Probe GmbH', InvoiceNumber='RE-2026-17')
    best_but_wrong = candidate(1, amount=GROSS, recipient='Lieferant Probe', text='RE-2026-17')       # 100 + 40 + 20 Punkte
    poor_but_right = candidate(2, date=shifted(200))                                                     # keine Punkte
    found = rank(row, [best_but_wrong, poor_but_right])
    assert ids(found) == [2, 1] and (found[0].score, found[1].score) == (0, 160)
    assert found[1].reasons == (REASON_AMOUNT, REASON_NUMBER, REASON_NAME, REASON_SAME_DAY, REASON_DIRECTION) and found[1].mismatch is True


def test_a_linked_booking_stays_behind_even_one_with_the_wrong_direction_and_says_both():
    found = rank(reading(), [candidate(1, linked=True), candidate(2, amount=GROSS), candidate(3)])
    assert ids(found) == [3, 2, 1]                                                     # (verknüpft, Richtung passt nicht, −Punkte, …)
    assert [(suggestion.linked, suggestion.mismatch) for suggestion in found] == [(False, False), (False, True), (True, False)]
    both = rank(reading(), [candidate(1, amount=GROSS, linked=True)])[0]
    assert both.reasons == (REASON_AMOUNT, REASON_SAME_DAY, REASON_DIRECTION, REASON_LINKED) and (both.linked, both.mismatch) == (True, True)


def test_at_most_five_come_back_and_a_wrong_direction_only_fills_the_places_that_are_left():
    wrong = [candidate(number, amount=GROSS) for number in range(1, 5)]                  # vier mit falscher Richtung und den besten Punkten
    right = [candidate(number, date=shifted(100)) for number in range(5, 9)]             # vier mit richtiger Richtung, ohne Punkte
    found = rank(reading(), wrong + right)
    assert ids(found) == [5, 6, 7, 8, 1] and [suggestion.mismatch for suggestion in found] == [False] * 4 + [True]
    assert ids(rank(reading(), wrong)) == [1, 2, 3, 4] and ids(rank(reading(), wrong + right[:2])) == [5, 6, 1, 2, 3]


def test_the_direction_is_no_field_of_the_json_form():
    """Die Seite bekommt ``booking``, ``reasons`` und ``linked`` – die Richtung steht nur in den Begründungen."""
    found = rank(reading(), [candidate(1, amount=GROSS)])
    row = (1, DAY, GROSS, 'EUR', '', '', '', 'entry', None)
    assert suggestions_payload([(found[0], {'row': row})]) == [{
        'booking': booking_payload(row), 'reasons': [REASON_AMOUNT, REASON_SAME_DAY, REASON_DIRECTION], 'linked': False}]


def test_the_best_five_come_back_best_first():
    row = reading(SenderName='Lieferant Probe GmbH')
    many = [candidate(number, date=shifted(number * 3)) for number in range(1, 9)]        # je weiter weg, desto weniger Punkte
    found = rank(row, many)
    assert len(found) == MAX_SUGGESTIONS == 5 and ids(found) == [1, 2, 3, 4, 5]
    assert [suggestion.score for suggestion in found] == sorted((suggestion.score for suggestion in found), reverse=True)


def test_a_tie_goes_to_the_nearer_date_and_then_to_the_lower_id():
    row = reading()
    #  Δ=+2, +1, −1 bringen alle 20 Punkte: erst der kleinere Abstand, bei gleichem Abstand die kleinere ID
    found = rank(row, [candidate(5, date=shifted(2)), candidate(6, date=shifted(1)), candidate(4, date=shifted(-1))])
    assert [suggestion.score for suggestion in found] == [20, 20, 20] and ids(found) == [4, 6, 5]
    same = [candidate(number) for number in (9, 3, 7, 1)]
    assert ids(rank(row, same)) == [1, 3, 7, 9] and ids(rank(row, list(reversed(same)))) == [1, 3, 7, 9]


def test_without_a_known_distance_a_booking_follows_one_with_a_distance_at_equal_points():
    found = rank(reading(), [candidate(1, date=None), candidate(2, date=shifted(400))])        # beide 0 Punkte
    assert ids(found) == [2, 1] and [suggestion.day_distance for suggestion in found] == [400, None]


# ── rank_candidates: schiefe Werte ───────────────────────────────────────────

def test_odd_values_never_raise_the_signal_just_drops():
    row = reading(SenderName=5, InvoiceNumber=47110, Text=None, DocDate='2026-13-40')
    odd = [
        candidate(1, date=None, text=None, recipient=47110, number=47110, contact_name=None, linked=None),
        candidate(2, date='2026-13-40', text=5, recipient=b'blob', number=['x'], contact_name=4.5, linked='ja'),
        candidate(3, date=20260304, text={'a': 1}),
        {'id': 4, 'amount': -GROSS},                                                  # alle anderen Schlüssel fehlen
        {'id': 'x', 'amount': -GROSS}, {'id': None, 'amount': -GROSS}, {'id': True, 'amount': -GROSS},   # keine ID: fällt weg
        {'id': 5, 'amount': '1190000'}, {'id': 6, 'amount': None}, {'id': 7, 'amount': 1190000.0}, {'id': 8}, {},   # kein Betrag: fällt weg
        None, 'text', 5, ['a']]
    found = rank(row, odd, '2026-13-40')
    assert ids(found) == [1, 3, 4, 2]                                                 # die verknüpfte (2) zuletzt, sonst nach Punkten, dann ID
    assert found[0].score == 100 and found[0].reasons == (REASON_AMOUNT, REASON_NUMBER)          # 47110 steht als Zahl in der Beleg-Nr. der Buchung
    assert [suggestion.linked for suggestion in found] == [False, False, False, True]
    assert all(suggestion.day_distance is None for suggestion in found)
    assert rank(reading(GrossMinor=None), odd) == [] and rank({}, odd) == [] and rank({'Status': 'ok'}, odd) == []
    assert rank(reading(), []) == [] and rank(reading(), iter([])) == []


# ── rank_candidates: Laufzeit ────────────────────────────────────────────────

def test_ten_thousand_candidates_and_a_long_receipt_text_take_well_under_a_second():
    document_text = ' '.join(f'Position{number} Pinsel Malerbetrieb Lieferung{number}' for number in range(1, 1500))[:20000]
    assert len(document_text) == 20000
    row = reading(SenderName='Pinsel Malerbetrieb GmbH', InvoiceNumber='RE-2026/0042', Text=document_text)
    pool = [candidate(number, date=shifted(number % 120 - 10), text=f'Überweisung Rechnung {number} Verwendungszweck Material',
                      recipient=f'Zulieferer {number} GmbH', number=f'B-{number}', contact_name='Zulieferer Probe')
            for number in range(1, 10001)]
    pool[9000]['text'] = 'Zahlung RE-2026/0042'
    pool[7000]['recipient'] = 'Malerbetrieb Pinsel'
    started = time.perf_counter()
    found = rank(row, pool)
    elapsed = time.perf_counter() - started
    assert len(found) == MAX_SUGGESTIONS and found[0].booking_id == 9001 and found[0].score >= 100
    assert elapsed < 1.0, f'{elapsed:.2f} s für 10 000 Kandidaten'


def test_the_word_set_and_the_number_of_the_receipt_are_built_once_and_candidate_text_is_capped(monkeypatch):
    seen = {'document_words': 0, 'document_number': 0, 'longest_words': 0, 'longest_number': 0}
    real_words, real_number = receipt_matching.name_words, receipt_matching.normalize_number
    document_text = 'Pinsel ' * 3000

    def words(text):
        if len(text) > 10000:
            seen['document_words'] += 1
        else:
            seen['longest_words'] = max(seen['longest_words'], len(text))
        return real_words(text)

    def number(text):
        if text == 'RE-2026/0042':
            seen['document_number'] += 1
        else:
            seen['longest_number'] = max(seen['longest_number'], len(text))
        return real_number(text)

    monkeypatch.setattr(receipt_matching, 'name_words', words)
    monkeypatch.setattr(receipt_matching, 'normalize_number', number)
    huge = 'Lieferant ' * 500_000
    pool = [candidate(n, text=huge, recipient=huge, number=huge, contact_name=huge) for n in range(1, 40)]
    found = rank(reading(InvoiceNumber='RE-2026/0042', Text=document_text), pool)
    assert len(found) == MAX_SUGGESTIONS
    assert seen['document_words'] == 1 and seen['document_number'] == 1               # nie eine Schleife über den Belegtext je Kandidat
    assert seen['longest_number'] == MAX_FIELD_LENGTH                                 # Text, Beleg-Nr. und Empfänger: auf 2000 gekappt
    assert seen['longest_words'] == 2 * MAX_FIELD_LENGTH + 1                          # Empfänger + ' ' + Name: jeder für sich gekappt


# ── receipt_match_candidates ─────────────────────────────────────────────────

def test_the_query_only_selects_on_one_connection_that_it_closes_and_writes_nothing(tmp_db, monkeypatch):
    receipt = add_receipt(tmp_db)
    add_booking(tmp_db)
    before = installation.dump(tmp_db.db_name)
    statements, connections = [], []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    found = candidates_of(tmp_db, receipt)
    monkeypatch.undo()

    assert len(found) == 1
    # davor liest der Baustein der Forderung (``_receivable_sql``) das Debitorenkonto und die Zahlungskonten: alles SELECTs
    assert statements and all(statement.lstrip().upper().startswith('SELECT') for statement in statements)
    assert sum('FROM Bookings b' in statement for statement in statements) == 1             # die eigentliche Abfrage läuft einmal
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):                                            # geschlossen
        connections[0].execute('SELECT 1')
    assert installation.dump(tmp_db.db_name) == before


def test_a_bank_movement_and_a_standalone_entry_are_candidates_whatever_the_sign_oldest_first(tmp_db):
    receipt = add_receipt(tmp_db)
    late = add_booking(tmp_db, date_booking='2026-03-20', amount='119.00')            # Einnahme
    bank = add_bank(tmp_db, date_booking='2026-03-10')                                # Ausgabe
    tie_b = add_booking(tmp_db, date_booking='2026-03-10', amount='-119.00')
    early = add_booking(tmp_db, date_booking='2026-03-01', amount='-119.00')
    assert candidate_ids(tmp_db, receipt) == [early, bank, tie_b, late]               # nach Datum, dann nach ID
    assert candidate_ids(tmp_db, receipt, gross=-GROSS) == [early, bank, tie_b, late]  # das Vorzeichen des Belegs spielt keine Rolle


def test_a_bank_movement_with_entries_is_a_candidate_but_its_entries_are_not(tmp_db):
    receipt = add_receipt(tmp_db)
    bank = add_bank(tmp_db, amount='-119.00')
    add_booking(tmp_db, amount='-119.00', parent_booking_id=bank)                     # Satz mit demselben Betrag
    add_booking(tmp_db, amount='-100.00', parent_booking_id=bank)
    assert candidate_ids(tmp_db, receipt) == [bank]
    assert candidate_ids(tmp_db, receipt, gross=to_minor('100.00')) == []             # der Teilbetrag eines Satzes ist keiner


def _wrong_amount(db, receipt):
    return add_booking(db, amount='-118.99')


def _before_window(db, receipt):
    return add_booking(db, date_booking=shifted(-8))


def _after_window(db, receipt):
    return add_booking(db, date_booking=shifted(91))


def _child_of_a_bank_movement(db, receipt):
    return add_booking(db, parent_booking_id=add_bank(db, amount='-300.00'))


def _payment_entry(db, receipt):
    booking = add_booking(db)
    run_sql(db, 'UPDATE Bookings SET Status = ? WHERE ID = ?', PAYMENT_MARK, booking)
    return booking


def _mirror(form):
    def build(db, receipt):
        return add_booking(db, entry_form=form)
    return build


def _auto_mirror(db, receipt):
    return add_booking(db, auto_mirror=True)


def _linked_to_this_receipt(db, receipt):
    booking = add_booking(db)
    db.link_booking_to_document(booking, receipt, 'receipt')
    return booking


def _linked_to_this_receipt_as_invoice(db, receipt):
    booking = add_bank(db)
    db.link_booking_to_document(booking, receipt, 'invoice')                          # gleich welche Art: dieser Beleg ist schon dran
    return booking


def _on_an_entry_of_the_bank_movement(kind):
    def build(db, receipt):
        bank = add_bank(db)
        db.link_booking_to_document(add_booking(db, parent_booking_id=bank), receipt, kind)      # DIESER Beleg hängt an einem Satz
        return bank                                                                              # … also fehlt die Bankbewegung
    return build


def _sent_invoice(db, number, **over):
    """Eine versendete Rechnung (119,00 brutto, wenn ``over`` nichts anderes sagt) – ohne Kontakte: die Forderung braucht keine."""
    return add_document(db, number, 'invoice', status='sent', **over)


def _receivable_of_an_invoice(db, receipt):
    """Die Forderung, wie die App sie bucht: „Debitoren an Wartekonto“, +119,00, Form ``transfer``."""
    return db.ensure_receivable_booking(_sent_invoice(db, 'R-100'))


def _receivable_of_a_credit_note(db, receipt):
    """Die Forderung einer Gutschrift: „Debitoren an Wartekonto“ mit −119,00 und der Nummer einer Rechnung mit negativem Brutto –
    so bucht die App auch die Gegenbuchung einer Stornorechnung."""
    credit_note = _sent_invoice(db, 'G-100', sum_net=-100.0, tax_amount=-19.0, sum_gross=-119.0, amount_due=-119.0)
    return db.ensure_receivable_booking(credit_note)


def _counter_booking_of_a_storno_invoice(db, receipt):
    """Die Gegenbuchung einer echten Stornorechnung (``cancel_invoice``, am 01.04.): „Debitoren an Wartekonto“ mit −119,00 und der
    Nummer der Stornorechnung. Die Forderung der stornierten Rechnung steht daneben (+119,00 am Belegdatum)."""
    invoice = _sent_invoice(db, 'R-100')
    db.ensure_receivable_booking(invoice)
    storno = db.cancel_invoice(invoice, '2026-04-01', 'tester', today=datetime.date(2026, 7, 1))
    assert storno.ok and storno.counter_booked, storno
    ((counter,),) = query(db, 'SELECT ID FROM Bookings WHERE DocumentNumber = ?', (storno.storno_number,))
    return counter


def _receivable_the_other_way_round(db, receipt):
    """Das Debitorenkonto auf der Gegenseite, der Betrag negativ (Vorzeichenregel der Umbuchungen)."""
    return add_booking(db, amount='-119.00', coa_id=coa(db, 4405), counter_coa_id=coa(db, 10000), entry_form='transfer',
                       document_number='R-101')


def _receivable_resolved(db, receipt):
    booking = _receivable_of_an_invoice(db, receipt)
    run_sql(db, "UPDATE Bookings SET Status = 'resolved' WHERE ID = ?", booking)               # ``resolved`` rettet eine Forderung nicht
    return booking


EXCLUSIONS = {
    'wrong amount': _wrong_amount, 'before the window': _before_window, 'after the window': _after_window,
    'entry of a bank movement': _child_of_a_bank_movement, 'payment entry': _payment_entry, 'mirror_coa': _mirror('mirror_coa'),
    'mirror_counter': _mirror('mirror_counter'), 'mirror_legacy': _mirror('mirror_legacy'), 'marked mirror': _auto_mirror,
    'linked to this receipt': _linked_to_this_receipt, 'linked to this receipt as an invoice': _linked_to_this_receipt_as_invoice,
    'this receipt on an entry of the bank movement': _on_an_entry_of_the_bank_movement('receipt'),
    'this receipt on an entry of the bank movement as an invoice': _on_an_entry_of_the_bank_movement('invoice'),
    'receivable of an invoice': _receivable_of_an_invoice, 'receivable of a credit note': _receivable_of_a_credit_note,
    'counter booking of a storno invoice': _counter_booking_of_a_storno_invoice,
    'receivable the other way round': _receivable_the_other_way_round, 'receivable that is resolved': _receivable_resolved,
}


@pytest.mark.parametrize('case', list(EXCLUSIONS))
def test_every_exclusion_on_its_own(tmp_db, case):
    receipt = add_receipt(tmp_db)
    keep = add_booking(tmp_db)
    gone = EXCLUSIONS[case](tmp_db, receipt)
    found = candidate_ids(tmp_db, receipt)
    assert keep in found and gone not in found


def test_what_looks_similar_but_is_none_of_these_stays_a_candidate(tmp_db):
    receipt = add_receipt(tmp_db)
    other = add_receipt(tmp_db, 'B-2', path='Belege/2026/B-2_beleg.pdf')
    wanted = []
    for form in ('purpose_first', 'liquid_first', 'transfer'):                        # jede andere Form als ein Spiegel
        wanted.append(add_booking(tmp_db, entry_form=form))
    no_form = add_booking(tmp_db)
    run_sql(tmp_db, 'UPDATE Bookings SET EntryForm = NULL WHERE ID = ?', no_form)    # eine Zeile ohne Form (Roh-SQL) gilt nicht als Spiegel
    wanted.append(no_form)
    resolved = add_booking(tmp_db)
    run_sql(tmp_db, "UPDATE Bookings SET Status = 'resolved' WHERE ID = ?", resolved)
    wanted.append(resolved)
    linked_elsewhere = add_booking(tmp_db)
    tmp_db.link_booking_to_document(linked_elsewhere, other, 'receipt')               # ein anderer Beleg hindert nicht
    wanted.append(linked_elsewhere)
    wanted.append(add_bank(tmp_db))                                                   # eine Bankzeile hat nie eine Form
    assert candidate_ids(tmp_db, receipt) == wanted


def test_what_looks_like_a_receivable_but_is_none_stays_a_candidate(tmp_db):
    receipt = add_receipt(tmp_db)
    bank_account(tmp_db, 'Giro', 1800)                                                       # 1800 wird ein Zahlungskonto
    payment = add_booking(tmp_db, amount='119.00', coa_id=coa(tmp_db, 1800), counter_coa_id=coa(tmp_db, 10000),
                          entry_form='liquid_first')                                         # „Bank an Debitoren“: 10000 im Haben
    reduction = add_booking(tmp_db, amount='119.00', coa_id=coa(tmp_db, 4400), counter_coa_id=coa(tmp_db, 10000),
                            entry_form='transfer')                                           # „… an 10000“, positiv: Erlösminderung, Skonto
    no_credit_note = add_booking(tmp_db, amount='-119.00', coa_id=coa(tmp_db, 10000), counter_coa_id=coa(tmp_db, 4400),
                                 entry_form='transfer', document_number='X-9')               # 10000 negativ, aber keine Gutschrift dazu
    assert candidate_ids(tmp_db, receipt) == [payment, reduction, no_credit_note]


def test_the_receivable_condition_is_the_building_block_of_the_matching_module_and_its_parameters_stay_in_place(tmp_db, monkeypatch):
    """Die Abfrage setzt ``Database._receivable_sql`` mit dem Alias ``b`` ein – keine eigene Kopie der Bedingung – und hängt dessen
    Parameter an die richtige Stelle der positionellen Parameter (davor Betrag, Marke und Formen, danach Beleg, Fenster, Grenze)."""
    receipt = add_receipt(tmp_db)
    keep = add_booking(tmp_db, text='bleibt')
    add_booking(tmp_db, text='Forderung A')
    add_booking(tmp_db, text='Forderung B')
    mine = add_booking(tmp_db, text='dieser Beleg hängt dran')
    tmp_db.link_booking_to_document(mine, receipt, 'receipt')
    add_booking(tmp_db, text='zu früh', date_booking=shifted(-8))
    asked = []

    def condition(self, cursor, alias=''):
        asked.append(alias)
        return '(b.Text = ? OR b.Text = ?)', ['Forderung A', 'Forderung B']

    monkeypatch.setattr(Database, '_receivable_sql', condition)
    assert candidate_ids(tmp_db, receipt, limit=5) == [keep] and asked == ['b']


def test_a_database_without_the_debtor_account_still_answers(tmp_db):
    receipt = add_receipt(tmp_db)
    booking = add_booking(tmp_db)
    run_sql(tmp_db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 10000')                # ``receivable_condition`` ist dann ``0``
    assert candidate_ids(tmp_db, receipt) == [booking]


def test_the_window_includes_both_days_and_a_missing_bound_drops_that_side(tmp_db):
    receipt = add_receipt(tmp_db)
    first = add_booking(tmp_db, date_booking='2026-02-25')
    middle = add_booking(tmp_db, date_booking='2026-04-01')
    last = add_booking(tmp_db, date_booking='2026-06-02')
    far_before = add_booking(tmp_db, date_booking='2026-02-24')
    far_after = add_booking(tmp_db, date_booking='2026-06-03')
    assert candidate_ids(tmp_db, receipt) == [first, middle, last]
    assert candidate_ids(tmp_db, receipt, window=(None, '2026-06-02')) == [far_before, first, middle, last]
    assert candidate_ids(tmp_db, receipt, window=('2026-02-25', None)) == [first, middle, last, far_after]
    assert candidate_ids(tmp_db, receipt, window=(None, None)) == [far_before, first, middle, last, far_after]


def test_linked_is_set_on_the_row_itself_or_through_one_of_its_entries_for_a_receipt_only(tmp_db):
    receipt = add_receipt(tmp_db)
    other = add_receipt(tmp_db, 'B-2', path='Belege/2026/B-2_beleg.pdf')
    free = add_booking(tmp_db)
    itself = add_booking(tmp_db)
    tmp_db.link_booking_to_document(itself, other, 'receipt')
    bank = add_bank(tmp_db)
    entry = add_booking(tmp_db, amount='-119.00', parent_booking_id=bank)
    tmp_db.link_booking_to_document(entry, other, 'receipt')                          # nur ein Kind trägt den Beleg
    contract = add_booking(tmp_db)
    tmp_db.link_booking_to_document(contract, other, 'contract')                      # ein Vertrag ist kein Beleg
    invoice = add_bank(tmp_db)
    tmp_db.link_booking_to_document(invoice, other, 'invoice')
    both = add_bank(tmp_db)
    tmp_db.link_booking_to_document(both, other, 'receipt')
    tmp_db.link_booking_to_document(add_booking(tmp_db, parent_booking_id=both), other, 'receipt')
    linked = {found['id']: found['linked'] for found in candidates_of(tmp_db, receipt)}
    assert linked == {free: False, itself: True, bank: True, contract: False, invoice: False, both: True}
    assert all(type(value) is bool for value in linked.values())


def test_a_bank_movement_whose_entry_carries_this_receipt_is_no_candidate_but_one_with_another_receipt_stays_and_is_linked(tmp_db):
    receipt = add_receipt(tmp_db)
    other = add_receipt(tmp_db, 'B-2', path='Belege/2026/B-2_beleg.pdf')
    bank = add_bank(tmp_db)
    entry = add_booking(tmp_db, parent_booking_id=bank)
    elsewhere = add_bank(tmp_db)                                                             # eine zweite Bankbewegung, unberührt
    tmp_db.link_receipt_to_booking(entry, other)                                             # ein ANDERER Beleg am Satz
    assert {item['id']: item['linked'] for item in candidates_of(tmp_db, receipt)} == {bank: True, elsewhere: False}
    tmp_db.link_receipt_to_booking(entry, receipt)                                           # DIESER Beleg am Satz
    assert candidate_ids(tmp_db, receipt) == [elsewhere]                                     # die Bankbewegung fehlt
    assert candidate_ids(tmp_db, other) == [elsewhere]                                       # … und für den anderen Beleg ebenso
    assert tmp_db.unlink_receipt_from_booking(entry, receipt) is True
    assert candidate_ids(tmp_db, receipt) == [bank, elsewhere]                               # gelöst: wieder ein Kandidat
    assert [item['linked'] for item in candidates_of(tmp_db, receipt)] == [True, False]


def test_the_contact_name_is_the_display_name_and_without_one_the_company_name(tmp_db):
    receipt = add_receipt(tmp_db)
    insert = tmp_db.insert_contact
    contacts = {
        'named': insert(contact_type='supplier', entity_type='company', display_name=' Lieferant Probe GmbH ',
                        company_name='Lieferant Probe Firma'),
        'company only': insert(contact_type='supplier', entity_type='company', company_name='Ohne Anzeigename GmbH'),
        'blank name': insert(contact_type='supplier', entity_type='company', display_name='   ', company_name='Leerer Name GmbH'),
        'person': insert(contact_type='supplier', entity_type='person', first_name='Erika', last_name='Probe'),
        'nameless company': insert(contact_type='supplier', entity_type='company')}
    bookings = {key: add_booking(tmp_db, contact_id=contact) for key, contact in contacts.items()}
    bookings['no contact'] = add_booking(tmp_db)
    found = candidates_of(tmp_db, receipt)
    assert {item['id']: item['contact_name'] for item in found} == {
        bookings['named']: 'Lieferant Probe GmbH',                       # der Anzeigename gewinnt, ohne Leerraum am Rand
        bookings['company only']: 'Ohne Anzeigename GmbH',               # ohne Anzeigenamen: der Firmenname
        bookings['blank name']: 'Leerer Name GmbH',                      # nur Leerraum gilt als leer
        bookings['person']: '', bookings['nameless company']: '', bookings['no contact']: ''}
    assert len(found) == len(bookings) == 6                              # keine Buchung doppelt


def test_a_contact_has_at_most_one_company_row_so_the_join_cannot_double_a_booking(tmp_db):
    contact = tmp_db.insert_contact(contact_type='supplier', entity_type='company', company_name='Eine Firma GmbH')
    conn = sqlite3.connect(tmp_db.db_name)
    try:
        primary_key = [row[1] for row in conn.execute('PRAGMA table_info(CompanyDetails)') if row[5]]
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            conn.execute("INSERT INTO CompanyDetails (ContactID, CompanyName) VALUES (?, 'Zweite Zeile')", (contact,))
    finally:
        conn.close()
    assert primary_key == ['ContactID']


def test_a_candidate_carries_what_the_ranking_needs_and_the_row_of_search_bookings(tmp_db):
    receipt = add_receipt(tmp_db)
    contact = tmp_db.insert_contact(contact_type='supplier', entity_type='company', display_name='Lieferant Probe GmbH')
    bank = add_bank(tmp_db, date_booking='2026-03-10', recipient_client='Lieferant Probe', text='Zahlung RE-2026-17',
                    document_number='ER-7', contact_id=contact)
    entry = add_booking(tmp_db, date_booking='2026-03-11', amount='119.00', recipient_client='Kunde Probe', text='Einnahme')
    found = {item['id']: item for item in candidates_of(tmp_db, receipt)}
    searched = {row[0]: row for row in tmp_db.search_bookings('', None, None, 50)}
    assert set(found) == {bank, entry}
    assert found[bank] == {'id': bank, 'date': '2026-03-10', 'amount': -GROSS, 'text': 'Zahlung RE-2026-17', 'recipient': 'Lieferant Probe',
                           'number': 'ER-7', 'contact_name': 'Lieferant Probe GmbH', 'linked': False, 'row': searched[bank]}
    assert found[entry]['row'] == searched[entry] and found[entry]['amount'] == GROSS and found[entry]['number'] is None
    for item in found.values():
        assert type(item['row']) is tuple and len(item['row']) == 9 and item['row'][0] == item['id']
    payload = booking_payload(found[bank]['row'])                                      # die Seite braucht dafür nichts anderes
    assert (payload['id'], payload['kind'], payload['recipient'], payload['documentNumber']) == (bank, 'bank', 'Lieferant Probe', 'ER-7')
    assert booking_payload(found[entry]['row'])['kind'] == 'entry'


def test_the_limit_is_clamped_to_one_up_to_the_maximum(tmp_db):
    receipt = add_receipt(tmp_db)
    first, second, third = (add_booking(tmp_db, date_booking=shifted(number)) for number in (1, 2, 3))
    assert candidate_ids(tmp_db, receipt, limit=2) == [first, second]
    assert candidate_ids(tmp_db, receipt, limit=1) == [first]
    assert candidate_ids(tmp_db, receipt, limit=0) == [first] and candidate_ids(tmp_db, receipt, limit=-5) == [first]
    assert candidate_ids(tmp_db, receipt) == [first, second, third]
    conn = tmp_db._get_connection()
    try:
        conn.executemany("INSERT INTO Bookings (DateBooking, Amount, BookingType, EntryForm) VALUES (?, ?, 'entry', 'purpose_first')",
                         [(shifted(4), -GROSS)] * (MAX_CANDIDATES + 5))
        conn.commit()
    finally:
        conn.close()
    assert len(candidates_of(tmp_db, receipt)) == MAX_CANDIDATES
    assert len(candidates_of(tmp_db, receipt, limit=10 ** 9)) == MAX_CANDIDATES
    assert len(candidates_of(tmp_db, receipt, limit=50)) == 50


def test_values_only_go_in_as_parameters(tmp_db):
    receipt = add_receipt(tmp_db)
    right = add_booking(tmp_db)
    add_booking(tmp_db, amount='-118.99')                                              # fiele durch ein eingeschleustes OR 1=1 mit heraus
    hostile = "2026-01-01' OR 1=1 --"
    assert candidate_ids(tmp_db, receipt, window=(hostile, None)) == [right]
    assert candidate_ids(tmp_db, receipt, window=(None, "2026-12-31' OR 1=1 --")) == [right]
    assert [found['id'] for found in tmp_db.receipt_match_candidates('1 OR 1=1', GROSS, None, None)] == [right]


# ── suggest_bookings_for_receipt ─────────────────────────────────────────────

def test_the_reading_of_the_tests_has_exactly_the_columns_of_the_table():
    assert set(reading()) == set(READING_COLUMNS)


def save_reading(db, receipt, **over):
    row = reading(**over)
    assert db.save_document_reading(receipt, FILE, row)
    return row


def a_world(db):
    """Ein Beleg vom 04.03.2026 über 119,00 EUR mit Lesung (eine Rechnung: erwartet wird ein Ausgang) und fünf Buchungen: Bank mit
    Rechnungsnummer und Namen, ein Satz am Folgetag, einer am Rand des Fensters, zwei außerhalb (Betrag, Datum). Liefert ``(Beleg, [IDs
    in der erwarteten Reihenfolge])``."""
    receipt = add_receipt(db)
    save_reading(db, receipt, SenderName='Lieferant Probe GmbH', InvoiceNumber='RE-2026-17')
    bank = add_bank(db, date_booking='2026-03-10', recipient_client='Lieferant Probe GmbH', text='Zahlung RE-2026-17')
    entry = add_booking(db, date_booking='2026-03-05', amount='-119.00', text='Sonstiges')
    edge = add_booking(db, date_booking='2026-02-25', text='Rand')
    add_booking(db, date_booking='2026-03-05', amount='-118.00', text='falscher Betrag')
    add_bank(db, date_booking='2026-07-01', text='zu spät')
    add_bank(db, date_booking='2026-02-24', text='zu früh')
    return receipt, [bank, entry, edge]


def test_a_saved_reading_gives_ranked_suggestions_with_their_candidates(tmp_db):
    receipt, expected = a_world(tmp_db)
    found = tmp_db.suggest_bookings_for_receipt(receipt)
    assert [suggestion.booking_id for suggestion, _candidate in found] == expected
    assert [(suggestion.score, suggestion.day_distance) for suggestion, _candidate in found] == [(158, 6), (20, 1), (18, -7)]
    bank_suggestion = found[0][0]
    assert bank_suggestion == Suggestion(expected[0], 158, (REASON_AMOUNT, REASON_NUMBER, REASON_NAME, '6 Tage nach dem Belegdatum'),
                                         False, 6)
    for suggestion, item in found:
        assert item['id'] == suggestion.booking_id and item['row'][0] == suggestion.booking_id
        assert booking_payload(item['row'])['id'] == suggestion.booking_id           # ``row`` geht unverändert in die Anzeige


def test_the_company_name_of_a_contact_without_a_display_name_counts_as_a_name_in_the_ranking(tmp_db):
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt, SenderName='Ohne Anzeigename GmbH')
    contact = tmp_db.insert_contact(contact_type='supplier', entity_type='company', company_name='Ohne Anzeigename GmbH')
    booking = add_booking(tmp_db, contact_id=contact)                                  # kein Empfänger, nur der Kontakt
    ((suggestion, found),) = tmp_db.suggest_bookings_for_receipt(receipt)
    assert found['id'] == booking and found['contact_name'] == 'Ohne Anzeigename GmbH'
    assert suggestion.reasons == (REASON_AMOUNT, REASON_NAME, REASON_SAME_DAY) and suggestion.score == 40 + 20      # zwei Wörter, am Belegdatum


def test_the_receivable_of_an_own_invoice_is_not_suggested_for_a_receipt_of_the_same_amount(tmp_db):
    """Die Forderung (119,00 am Belegdatum) bekäme 20 Punkte und stünde an zweiter Stelle – sie ist gar kein Kandidat."""
    receipt, expected = a_world(tmp_db)
    receivable = _receivable_of_an_invoice(tmp_db, receipt)
    credit_note = _receivable_of_a_credit_note(tmp_db, receipt)
    found = tmp_db.suggest_bookings_for_receipt(receipt)
    assert [suggestion.booking_id for suggestion, _candidate in found] == expected
    assert receivable not in expected and credit_note not in expected


def test_a_receipt_that_hangs_on_an_entry_of_a_bank_movement_gets_no_suggestions(tmp_db):
    receipt, (bank, entry, edge) = a_world(tmp_db)
    child = add_booking(tmp_db, date_booking='2026-03-10', parent_booking_id=bank)        # ein Satz unter der Bankbewegung
    tmp_db.link_receipt_to_booking(child, receipt)                                        # der Nutzer hat den Beleg am Satz verknüpft
    assert tmp_db.suggest_bookings_for_receipt(receipt) == []


def test_suggesting_writes_nothing_and_only_selects(tmp_db, monkeypatch):
    receipt, _expected = a_world(tmp_db)
    before = installation.dump(tmp_db.db_name)
    statements = []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    assert tmp_db.suggest_bookings_for_receipt(receipt)
    monkeypatch.undo()
    assert statements and all(statement.lstrip().upper().startswith('SELECT') for statement in statements)
    assert installation.dump(tmp_db.db_name) == before


def test_a_booking_with_another_receipt_follows_last_and_the_receipt_of_its_own_link_gets_no_suggestions_at_all(tmp_db):
    receipt, (bank, entry, edge) = a_world(tmp_db)
    other = add_receipt(tmp_db, 'B-2', path='Belege/2026/B-2_beleg.pdf')
    tmp_db.link_receipt_to_booking(entry, other)                                       # dieser Satz trägt schon einen anderen Beleg
    assert [s.booking_id for s, _c in tmp_db.suggest_bookings_for_receipt(receipt)] == [bank, edge, entry]
    assert tmp_db.suggest_bookings_for_receipt(receipt)[-1][0].reasons[-1] == REASON_LINKED
    assert tmp_db.receipt_is_linked(receipt) is False and tmp_db.receipt_is_linked(other) is True   # die Verknüpfung des anderen Belegs zählt nicht
    assert tmp_db.suggest_bookings_for_receipt(other) == []                            # … sie nimmt ihm selbst die Vorschläge
    tmp_db.link_receipt_to_booking(bank, receipt)                                      # zugeordnet: keine Vorschläge mehr
    assert tmp_db.suggest_bookings_for_receipt(receipt) == []


# ── Vorschläge nur, solange der Beleg an keiner Buchung hängt ────────────────

def test_a_receipt_that_hangs_on_any_booking_gets_no_suggestions_whatever_the_kind_of_the_link_and_gets_them_back_after_unlinking(tmp_db):
    receipt, expected = a_world(tmp_db)
    unrelated = add_booking(tmp_db, date_booking='2025-01-01', amount='-5.00', text='ganz woanders')   # nicht einmal ein Kandidat
    suggested = lambda: [s.booking_id for s, _c in tmp_db.suggest_bookings_for_receipt(receipt)]       # noqa: E731
    assert suggested() == expected and tmp_db.receipt_is_linked(receipt) is False
    for relation in ('receipt', 'invoice', 'contract', 'other'):
        tmp_db.link_receipt_to_booking(unrelated, receipt, relation)
        assert tmp_db.receipt_is_linked(receipt) is True and suggested() == [], relation
        assert tmp_db.unlink_receipt_from_booking(unrelated, receipt) is True
        assert tmp_db.receipt_is_linked(receipt) is False and suggested() == expected, relation          # gelöst: sie kommen wieder
    first, second = expected[:2]
    tmp_db.link_receipt_to_booking(first, receipt)
    tmp_db.link_receipt_to_booking(second, receipt)
    tmp_db.unlink_receipt_from_booking(first, receipt)
    assert suggested() == []                                                           # noch eine Verknüpfung: weiter keine
    tmp_db.unlink_receipt_from_booking(second, receipt)
    assert suggested() == expected                                                     # erst nach dem Lösen der letzten


def test_the_link_is_checked_on_the_connection_that_reads_the_receipt_before_any_candidate_is_read(tmp_db, monkeypatch):
    receipt, _expected = a_world(tmp_db)
    tmp_db.link_receipt_to_booking(add_booking(tmp_db, amount='-5.00'), receipt)
    monkeypatch.setattr(Database, 'receipt_match_candidates', lambda *args, **kwargs: pytest.fail('keine Abfrage für einen verknüpften Beleg'))
    opened = []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        statements = []
        connection.set_trace_callback(statements.append)
        opened.append(statements)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    assert tmp_db.suggest_bookings_for_receipt(receipt) == []
    monkeypatch.setattr(Database, '_get_connection', real)
    (statements,) = opened                                                             # EINE Verbindung: der Beleg und seine Verknüpfung
    assert len(statements) == 2 and 'FROM Documents' in statements[0] and 'FROM BookingDocuments' in statements[1]
    assert all(statement.lstrip().upper().startswith('SELECT') for statement in statements)


def test_an_unknown_receipt_is_not_linked_and_the_check_only_selects(tmp_db, monkeypatch):
    receipt, _expected = a_world(tmp_db)
    assert tmp_db.receipt_is_linked(receipt + 100) is False and tmp_db.receipt_is_linked(None) is False
    before = installation.dump(tmp_db.db_name)
    statements = []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    assert tmp_db.receipt_is_linked(receipt) is False
    monkeypatch.setattr(Database, '_get_connection', real)
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith('SELECT')
    assert installation.dump(tmp_db.db_name) == before


# ── Zahlungsrichtung, mit gespeicherter Lesung ───────────────────────────────

def test_the_direction_of_the_kind_decides_the_order_of_a_saved_reading(tmp_db):
    invoice = add_receipt(tmp_db)
    save_reading(tmp_db, invoice, Kind='invoice')
    credit = add_receipt(tmp_db, 'B-2')
    save_reading(tmp_db, credit, Kind='credit_note')
    income = add_booking(tmp_db, date_booking=DAY, amount='119.00', text='Einnahme')                  # der bessere Treffer: am Belegdatum
    expense = add_booking(tmp_db, date_booking=shifted(30), amount='-119.00', text='Ausgabe')
    paid = tmp_db.suggest_bookings_for_receipt(invoice)
    assert [s.booking_id for s, _c in paid] == [expense, income]
    assert [s.mismatch for s, _c in paid] == [False, True] and paid[1][0].reasons[-1] == REASON_DIRECTION
    assert REASON_DIRECTION not in paid[0][0].reasons
    refunded = tmp_db.suggest_bookings_for_receipt(credit)
    assert [s.booking_id for s, _c in refunded] == [income, expense]
    assert [s.mismatch for s, _c in refunded] == [False, True] and refunded[1][0].reasons[-1] == REASON_DIRECTION


def test_the_window_comes_from_the_read_date_not_from_the_date_of_the_receipt(tmp_db):
    receipt = add_receipt(tmp_db, date='2026-06-01')                                   # Belegdatum: ein ganz anderes Fenster
    save_reading(tmp_db, receipt, DocDate='2026-03-04')
    inside_read = add_booking(tmp_db, date_booking='2026-03-01')                       # im Fenster des gelesenen Datums
    inside_receipt = add_booking(tmp_db, date_booking='2026-08-30')                    # nur im Fenster des Belegdatums
    found = tmp_db.suggest_bookings_for_receipt(receipt)
    assert [suggestion.booking_id for suggestion, _candidate in found] == [inside_read]
    assert found[0][0].day_distance == -3 and inside_receipt not in [s.booking_id for s, _c in found]


@pytest.mark.parametrize('read_date', [None, '', '2026-13-40', '04.03.2026'], ids=repr)
def test_without_a_usable_read_date_the_date_of_the_receipt_is_the_fallback(tmp_db, read_date):
    receipt = add_receipt(tmp_db, date='2026-06-01')
    save_reading(tmp_db, receipt, DocDate=read_date)
    before_window = add_booking(tmp_db, date_booking='2026-03-01')
    inside = add_booking(tmp_db, date_booking='2026-06-05')
    found = tmp_db.suggest_bookings_for_receipt(receipt)
    assert [suggestion.booking_id for suggestion, _candidate in found] == [inside] and before_window not in [s.booking_id for s, _c in found]
    assert found[0][0].day_distance == 4 and found[0][0].reasons[-1] == '4 Tage nach dem Belegdatum'


@pytest.mark.parametrize('receipt_date', [None, '', 'kein Datum', '2026-13-40'], ids=repr)
def test_without_any_date_there_is_no_window_and_no_suggestion(tmp_db, receipt_date):
    receipt = add_receipt(tmp_db, date=receipt_date)
    save_reading(tmp_db, receipt, DocDate=None)
    add_booking(tmp_db)
    assert tmp_db.suggest_bookings_for_receipt(receipt) == []


def test_an_unknown_receipt_or_one_without_a_reading_has_no_suggestions(tmp_db):
    receipt = add_receipt(tmp_db)
    add_booking(tmp_db)
    assert tmp_db.suggest_bookings_for_receipt(receipt) == []                          # keine Lesung
    assert tmp_db.suggest_bookings_for_receipt(receipt + 100) == []                    # den Beleg gibt es nicht
    assert tmp_db.suggest_bookings_for_receipt(None) == []


@pytest.mark.parametrize('over', [{'Status': 'empty'}, {'Status': 'partial'}, {'GrossMinor': None}, {'GrossMinor': 0},
                                  {'Currency': 'USD'}], ids=repr)
def test_a_reading_that_cannot_be_searched_has_no_suggestions_and_no_query_is_run(tmp_db, over, monkeypatch):
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt, **over)
    add_booking(tmp_db)
    monkeypatch.setattr(Database, 'receipt_match_candidates', lambda *args, **kwargs: pytest.fail('keine Abfrage ohne brauchbaren Betrag'))
    assert tmp_db.suggest_bookings_for_receipt(receipt) == []


def test_a_receipt_over_a_negative_gross_finds_the_positive_booking_and_at_most_five_come_back(tmp_db):
    receipt = add_receipt(tmp_db)
    save_reading(tmp_db, receipt, GrossMinor=-GROSS, Kind='credit_note')               # eine Gutschrift: erwartet wird ein Eingang
    booked = [add_booking(tmp_db, date_booking=shifted(number), amount='119.00') for number in range(1, 9)]
    assert [s.booking_id for s, _c in tmp_db.suggest_bookings_for_receipt(receipt)] == booked[:5]
