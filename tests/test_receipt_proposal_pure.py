# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Buchungsvorschlag aus einem gelesenen Beleg (2.10.1): die reine Regel in ``db/receipt_proposal.py`` – ohne Datenbank.

Vorgeschlagen wird eine Zeile für den Buchungssatz-Editor einer Bankbewegung; gespeichert wird nichts. Aus dem Beleg kommen nur Steuersatz und
Steuerbetrag (``proposal_tax``: ein Satz, Summen- und Satzprobe, das Vorzeichen des Betrags der Bank); der Betrag ist der der Bankbewegung, das
Konto kommt nur aus einer Historie, die sich einig ist (``steady_account``). Was sich nicht sicher sagen lässt, bleibt leer und bekommt einen festen
Hinweissatz – nie ein Absender, eine Nummer oder ein Text aus dem Beleg.

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import ast
import dataclasses
import inspect
from decimal import Decimal

import pytest

from db import receipt_proposal as rp
from db.receipt_proposal import (
    CENT, DONE_TEXT, HINT_ACCOUNT_CONFLICT, HINT_NO_ACCOUNT, HINT_TAX, HISTORY_AGREE_MIN, HISTORY_DEPTH, REASON_ACCOUNT_RECIPIENT,
    REASON_ACCOUNT_SENDER, REASON_AMOUNT, REASON_TAX, REASON_TAX_NONE, REFUSE_AMOUNT, REFUSE_BANK_AMOUNT, REFUSE_CURRENCY,
    REFUSE_DIRECTION, REFUSE_HAS_ROWS, REFUSE_NO_READING, REFUSE_OUTDATED, Proposal, build_proposal, choose_account, proposal_refusal,
    proposal_tax, rate_percent_text, steady_account)

VERSION = 1                                    # ``READER_VERSION`` der Tests: fest, damit eine Änderung des Lesers diese Tests nicht berührt
GROSS = 1190000                                # 119,00 EUR in Minor Units (money.SCALE = 4)
NET, TAX = 1000000, 190000                     # 100,00 und 19,00


def reading(**over):
    """Eine Lesung (Spalten von ``DocumentReadings``) über 119,00 EUR mit einem Satz von 19 %; ``over`` überschreibt Felder."""
    row = {'Status': 'ok', 'ReaderVersion': VERSION, 'GrossMinor': GROSS, 'NetMinor': NET, 'TaxMinor': TAX, 'TaxRate': 0.19,
           'Currency': 'EUR', 'Kind': 'invoice', 'SenderVatId': None}
    row.update(over)
    return row


def row(coa=4400, nobank=False, payment=False):
    return {'coa_id': coa, 'nobank': nobank, 'payment': payment}


def group(*coas):
    """Eine Gruppe der Historie: ein Satz je Konto."""
    return [row(coa) for coa in coas]


def propose(reading_row=None, bank_amount=-GROSS, bank_currency='EUR', has_children=False, recipient=(), sender=()):
    reading_row = reading() if reading_row is None else reading_row
    return build_proposal(reading_row, VERSION, bank_amount, bank_currency, has_children, list(recipient), list(sender))


# ── Der Bau des Moduls ───────────────────────────────────────────────────────
def test_the_constants_are_the_planned_ones():
    assert (HISTORY_DEPTH, HISTORY_AGREE_MIN, rp.HISTORY_SCAN, CENT) == (3, 2, 50, 100)


def test_the_module_is_pure_it_imports_neither_a_database_nor_a_clock():
    """Rein: kein Datenbankzugriff, keine Uhr – wie ``db/receipt_matching.py``."""
    tree = ast.parse(inspect.getsource(rp))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(('.' * node.level) + (node.module or ''))
    assert imported <= {'dataclasses', 'decimal', 'money', '.receipt_matching'}, imported


def test_a_proposal_is_immutable():
    proposal = propose()
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposal.coa_id = 1


# ── rate_percent_text ────────────────────────────────────────────────────────
@pytest.mark.parametrize('rate, point, comma', [
    (0.19, '19', '19'), (0.07, '7', '7'), (0.055, '5.5', '5,5'), (Decimal('0.19'), '19', '19'), (0.1, '10', '10'), (0.0, '0', '0'),
    (1, '100', '100'), (0.107, '10.7', '10,7'), (None, '', '')])
def test_the_percent_text_of_a_rate(rate, point, comma):
    assert rate_percent_text(rate) == point
    assert rate_percent_text(rate, ',') == comma


# ── proposal_tax ─────────────────────────────────────────────────────────────
def test_a_single_rate_with_both_checks_gives_the_rate_and_the_tax_without_a_sign():
    assert proposal_tax(reading()) == (Decimal('0.19'), TAX)


def test_the_reduced_rate_and_a_rate_with_a_decimal_place_work_too():
    assert proposal_tax(reading(TaxRate=0.07, NetMinor=NET, TaxMinor=70000, GrossMinor=1070000)) == (Decimal('0.07'), 70000)
    found = proposal_tax(reading(TaxRate=0.055, NetMinor=2000000, TaxMinor=110000, GrossMinor=2110000))
    assert found == (Decimal('0.055'), 110000)
    assert (rate_percent_text(found[0]), rate_percent_text(found[0], ',')) == ('5.5', '5,5')


def test_a_rate_of_two_decimal_places_in_percent_is_still_a_rate():
    assert proposal_tax(reading(TaxRate=0.1925, NetMinor=NET, TaxMinor=192500, GrossMinor=1192500)) == (Decimal('0.1925'), 192500)


def test_the_rate_test_allows_one_cent_but_not_two():
    one_cent = {'NetMinor': NET, 'TaxMinor': TAX + CENT, 'GrossMinor': GROSS + CENT}
    two_cent = {'NetMinor': NET, 'TaxMinor': TAX + 2 * CENT, 'GrossMinor': GROSS + 2 * CENT}
    assert proposal_tax(reading(**one_cent)) == (Decimal('0.19'), TAX + CENT)
    assert proposal_tax(reading(**{'NetMinor': NET, 'TaxMinor': TAX - CENT, 'GrossMinor': GROSS - CENT})) == (Decimal('0.19'), TAX - CENT)
    assert proposal_tax(reading(**two_cent)) is None


@pytest.mark.parametrize('over', [
    pytest.param({'TaxRate': None}, id='mehrere-Saetze-kein-Satz'),
    pytest.param({'GrossMinor': GROSS + 10000}, id='Summe-stimmt-nicht'),
    pytest.param({'TaxMinor': TAX + 200, 'GrossMinor': GROSS + 200}, id='Satzprobe-zwei-Cent-daneben'),
    pytest.param({'NetMinor': None}, id='Netto-fehlt'),
    pytest.param({'TaxMinor': None}, id='Steuer-fehlt'),
    pytest.param({'GrossMinor': None}, id='Brutto-fehlt'),
    pytest.param({'GrossMinor': -GROSS, 'NetMinor': -NET, 'TaxMinor': -TAX}, id='negativer-Betrag'),
    pytest.param({'GrossMinor': 0, 'NetMinor': 0, 'TaxMinor': 0}, id='Brutto-null'),
    pytest.param({'NetMinor': -NET, 'TaxMinor': GROSS + NET}, id='Netto-negativ'),
    pytest.param({'TaxRate': True}, id='bool-als-Satz'),
    pytest.param({'TaxRate': 1.5}, id='Satz-1-5'),
    pytest.param({'TaxRate': -0.19}, id='negativer-Satz'),
    pytest.param({'TaxRate': 0.19255, 'NetMinor': NET, 'TaxMinor': 192550, 'GrossMinor': 1192550}, id='drei-Nachkommastellen-in-Prozent'),
    pytest.param({'TaxRate': '0.19'}, id='Satz-als-Text'),
    pytest.param({'TaxRate': float('nan')}, id='Satz-nan'),
    pytest.param({'TaxRate': float('inf')}, id='Satz-inf'),
    pytest.param({'GrossMinor': '1190000'}, id='Brutto-als-Text'),
    pytest.param({'NetMinor': 1000000.0}, id='Netto-als-Kommazahl'),
    pytest.param({'TaxMinor': True}, id='Steuer-als-bool'),
    pytest.param({'NetMinor': 10 ** 30, 'GrossMinor': 10 ** 30 + TAX}, id='Betrag-ueber-jeder-Grenze'),
])
def test_everything_that_is_not_certain_gives_no_tax(over):
    assert proposal_tax(reading(**over)) is None


def test_a_rate_of_zero_with_no_tax_means_without_tax_and_with_tax_it_means_nothing():
    assert proposal_tax(reading(TaxRate=0, NetMinor=GROSS, TaxMinor=0)) == (Decimal(0), 0)
    assert proposal_tax(reading(TaxRate=0.0, NetMinor=GROSS, TaxMinor=0)) == (Decimal(0), 0)
    assert proposal_tax(reading(TaxRate=0, NetMinor=GROSS - 10000, TaxMinor=10000)) is None


@pytest.mark.parametrize('value', [None, 'Rechnung', [], 5, ()])
def test_a_reading_that_is_no_row_gives_no_tax_and_never_raises(value):
    assert proposal_tax(value) is None


class Spy(dict):
    """Eine Lesung, die mitschreibt, welche Spalten gelesen werden."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)


HOSTILE = '<script>alert(1)</script> Absender Probe GmbH RE-2026-17'


def test_the_tax_reads_only_its_four_columns():
    spy = Spy(reading(Raw=HOSTILE, Text=HOSTILE, SenderName=HOSTILE, InvoiceNumber=HOSTILE))
    assert proposal_tax(spy) is not None
    assert spy.read == {'TaxRate', 'NetMinor', 'TaxMinor', 'GrossMinor'}


def test_the_whole_proposal_never_reads_the_text_the_raw_data_the_sender_or_the_number():
    spy = Spy(reading(Raw=HOSTILE, Text=HOSTILE, SenderName=HOSTILE, InvoiceNumber=HOSTILE))
    proposal = propose(spy, recipient=[group(4400), group(4400)])
    assert proposal.ok and not {'Raw', 'Text', 'SenderName', 'InvoiceNumber'} & spy.read
    assert 'script' not in repr(proposal) and 'Probe' not in repr(proposal) and 'RE-2026' not in repr(proposal)


# ── steady_account ───────────────────────────────────────────────────────────
def test_two_groups_with_the_same_account_are_steady():
    assert steady_account([group(4400), group(4400)]) == 4400


def test_three_groups_with_the_same_account_are_steady():
    assert steady_account([group(4400), group(4400), group(4400)]) == 4400


def test_only_the_youngest_three_groups_count():
    assert steady_account([group(4400), group(4400), group(4400), group(6815)]) == 4400
    assert steady_account([group(4400), group(4400), group(6815), group(4400)]) is None


def test_the_agreement_is_needed_between_all_considered_groups():
    assert steady_account([group(4400), group(4400), group(6815)]) is None
    assert steady_account([group(4400), group(6815)]) is None


def test_one_group_is_not_enough():
    assert steady_account([]) is None
    assert steady_account([group(4400)]) is None


def test_empty_groups_are_skipped_and_do_not_count():
    assert steady_account([[], group(4400), [], group(4400)]) == 4400
    assert steady_account([[], [], group(4400)]) is None
    assert steady_account([[], group(4400), group(4400), [], group(6815)]) is None                  # die leere zählt nicht als eine der drei
    assert steady_account([[], group(4400), group(4400), [], group(4400), group(6815)]) == 4400


@pytest.mark.parametrize('odd', [
    pytest.param(group(4400, 6815), id='Split'),
    pytest.param([row(nobank=True)], id='Umbuchung'),
    pytest.param([row(payment=True)], id='Zahlungssatz'),
    pytest.param([row(coa='4400')], id='Konto-als-Text'),
    pytest.param([row(coa=True)], id='Konto-als-bool'),
    pytest.param([row(coa=None)], id='ohne-Konto'),
    pytest.param([row(coa=4400.0)], id='Konto-als-Kommazahl'),
    pytest.param(['Satz'], id='Zeile-ist-kein-dict'),
])
def test_a_group_that_is_not_one_plain_row_that_moves_money_makes_the_whole_unsteady(odd):
    assert steady_account([group(4400), group(4400), odd]) is None
    assert steady_account([odd, group(4400)]) is None
    assert steady_account([group(4400), odd, group(4400)]) is None


def test_a_group_after_the_third_may_be_odd_without_harm():
    assert steady_account([group(4400), group(4400), group(4400), group(4400, 6815), [row(payment=True)]]) == 4400


# ── choose_account ───────────────────────────────────────────────────────────
STEADY = [group(4400), group(4400)]
CONFLICT = [group(6815), group(6815)]
UNSTEADY = [group(4400)]


def test_only_the_recipient_history_names_the_account():
    assert choose_account(STEADY, []) == (4400, (REASON_ACCOUNT_RECIPIENT,), ())
    assert choose_account(STEADY, UNSTEADY) == (4400, (REASON_ACCOUNT_RECIPIENT,), ())


def test_only_the_sender_history_names_the_account():
    assert choose_account([], STEADY) == (4400, (REASON_ACCOUNT_SENDER,), ())
    assert choose_account(UNSTEADY, STEADY) == (4400, (REASON_ACCOUNT_SENDER,), ())


def test_both_histories_agreeing_give_both_reasons():
    assert choose_account(STEADY, [group(4400), group(4400), group(4400)]) == (4400, (REASON_ACCOUNT_RECIPIENT, REASON_ACCOUNT_SENDER), ())


def test_both_histories_steady_but_different_give_no_account_and_the_conflict():
    assert choose_account(STEADY, CONFLICT) == (None, (), (HINT_ACCOUNT_CONFLICT,))


def test_no_steady_history_gives_no_account_and_the_hint():
    assert choose_account([], []) == (None, (), (HINT_NO_ACCOUNT,))
    assert choose_account(UNSTEADY, UNSTEADY) == (None, (), (HINT_NO_ACCOUNT,))
    assert choose_account([group(4400), group(6815)], [group(1), group(2)]) == (None, (), (HINT_NO_ACCOUNT,))


# ── proposal_refusal ─────────────────────────────────────────────────────────
def refusal(reading_row=None, bank_amount=-GROSS, bank_currency='EUR', has_children=False, version=VERSION):
    return proposal_refusal(reading() if reading_row is None else reading_row, version, bank_amount, bank_currency, has_children)


def test_a_fitting_invoice_with_an_outgoing_payment_is_not_refused():
    assert refusal() is None


def test_a_fitting_credit_note_with_an_incoming_payment_is_not_refused():
    assert refusal(reading(Kind='credit_note'), bank_amount=GROSS) is None


def test_a_bank_movement_that_has_rows_is_refused_first():
    assert refusal(has_children=True) == REFUSE_HAS_ROWS
    assert refusal(None, bank_amount=0, bank_currency='USD', has_children=True) == REFUSE_HAS_ROWS      # vor allem anderen
    assert proposal_refusal(None, VERSION, None, 'USD', True) == REFUSE_HAS_ROWS


@pytest.mark.parametrize('currency, refused', [
    ('USD', True), ('CHF', True), (' usd ', True), ('EURO', True), ('EUR', False), (' eur ', False), ('', False), (None, False)])
def test_the_currency_of_the_bank_movement_must_be_euro_or_empty(currency, refused):
    assert (refusal(bank_currency=currency) == REFUSE_CURRENCY) is refused


def test_the_currency_comes_before_the_amount():
    assert refusal(bank_amount=0, bank_currency='USD') == REFUSE_CURRENCY


@pytest.mark.parametrize('amount', [0, None, '-119.00', -1190000.0, True, False, [], 'x'])
def test_an_amount_that_is_not_a_whole_number_or_is_zero_is_refused(amount):
    assert refusal(bank_amount=amount) == REFUSE_BANK_AMOUNT


def test_the_bank_amount_comes_before_the_reading():
    assert refusal(None, bank_amount=0) == REFUSE_BANK_AMOUNT
    assert proposal_refusal(None, VERSION, 0, 'EUR', False) == REFUSE_BANK_AMOUNT


@pytest.mark.parametrize('value', [
    pytest.param(None, id='keine-Lesung'),
    pytest.param(reading(Status='no_text'), id='kein-Text'),
    pytest.param(reading(Status='failed'), id='fehlgeschlagen'),
    pytest.param(reading(Status='empty'), id='nichts-gefunden'),
    pytest.param(reading(Currency='USD'), id='Fremdwaehrung'),
    pytest.param(reading(Currency='chf'), id='Fremdwaehrung-klein'),
    pytest.param(reading(GrossMinor=None), id='ohne-Brutto'),
    pytest.param(reading(GrossMinor=0), id='Brutto-null'),
    pytest.param(reading(GrossMinor='1190000'), id='Brutto-als-Text'),
    pytest.param('Rechnung', id='keine-Zeile'),
])
def test_a_reading_that_cannot_be_used_is_refused(value):
    assert proposal_refusal(value, VERSION, -GROSS, 'EUR', False) == REFUSE_NO_READING


@pytest.mark.parametrize('currency', ['', None, ' ', 'EUR', ' eur '])
def test_an_empty_currency_of_the_reading_is_no_obstacle(currency):
    assert refusal(reading(Currency=currency)) is None


@pytest.mark.parametrize('version, outdated', [(0, True), (None, True), ('1', True), (True, True), (1.0, True), (1, False), (2, False)])
def test_a_reading_of_older_rules_is_refused(version, outdated):
    assert (refusal(reading(ReaderVersion=version)) == REFUSE_OUTDATED) is outdated


def test_the_reading_comes_before_the_version_and_the_version_before_the_amount():
    assert refusal(reading(ReaderVersion=0, GrossMinor=GROSS + 1)) == REFUSE_OUTDATED
    assert refusal(reading(Status='failed', ReaderVersion=0)) == REFUSE_NO_READING


def test_a_gross_that_differs_from_the_bank_amount_is_refused_for_discount_and_partial_payment():
    assert refusal(bank_amount=-1166200) == REFUSE_AMOUNT                          # Skonto: 119,00 gegen 116,62
    assert refusal(bank_amount=-GROSS - 100) == REFUSE_AMOUNT                      # ein Cent mehr
    assert refusal(bank_amount=-GROSS + 100) == REFUSE_AMOUNT                      # ein Cent weniger
    assert refusal(bank_amount=-600000) == REFUSE_AMOUNT                           # Teilzahlung


def test_the_amount_comes_before_the_direction():
    assert refusal(bank_amount=GROSS + 100) == REFUSE_AMOUNT                       # falsche Richtung UND falscher Betrag


def test_an_invoice_with_an_incoming_payment_and_a_credit_note_with_an_outgoing_one_are_refused():
    assert refusal(bank_amount=GROSS) == REFUSE_DIRECTION
    assert refusal(reading(Kind='credit_note'), bank_amount=-GROSS) == REFUSE_DIRECTION
    assert refusal(reading(Kind='invoice'), bank_amount=GROSS) == REFUSE_DIRECTION


@pytest.mark.parametrize('kind, amount, fits', [
    (None, -GROSS, True), ('', -GROSS, True), ('invoice', -GROSS, True), (None, GROSS, False), ('credit_note', GROSS, True),
    ('credit_note', -GROSS, False), ('unbekannt', GROSS, True), ('unbekannt', -GROSS, True)])
def test_the_direction_follows_the_kind_of_the_reading_like_the_matching_of_receipts_to_bookings(kind, amount, fits):
    assert (refusal(reading(Kind=kind), bank_amount=amount) is None) is fits


# ── build_proposal ───────────────────────────────────────────────────────────
def test_a_full_hit_an_invoice_with_an_outgoing_payment():
    proposal = propose(recipient=[group(4400), group(4400)])
    assert proposal == Proposal(
        ok=True, message=DONE_TEXT, amount_minor=-GROSS, tax_rate=Decimal('0.19'), tax_minor=-TAX, coa_id=4400,
        reasons=(REASON_AMOUNT, REASON_TAX.format(rate='19'), REASON_ACCOUNT_RECIPIENT), hints=())


def test_the_tax_takes_the_sign_of_the_bank_amount_and_a_credit_note_with_incoming_money_is_positive():
    credit = propose(reading(Kind='credit_note'), bank_amount=GROSS, recipient=[group(4400), group(4400)])
    assert (credit.ok, credit.amount_minor, credit.tax_minor) == (True, GROSS, TAX)
    invoice = propose(bank_amount=-GROSS)
    assert (invoice.ok, invoice.amount_minor, invoice.tax_minor) == (True, -GROSS, -TAX)


def test_the_amount_is_the_one_of_the_bank_movement_never_the_one_of_the_reading():
    assert propose(bank_amount=-GROSS).amount_minor == -GROSS
    assert propose(reading(Kind='credit_note'), bank_amount=GROSS).amount_minor == GROSS


def test_a_reading_with_several_rates_gives_the_amount_without_a_tax_and_the_hint():
    proposal = propose(reading(TaxRate=None))
    assert (proposal.ok, proposal.amount_minor, proposal.tax_rate, proposal.tax_minor) == (True, -GROSS, None, None)
    assert proposal.reasons == (REASON_AMOUNT,) and proposal.hints == (HINT_TAX, HINT_NO_ACCOUNT)


@pytest.mark.parametrize('over', [
    pytest.param({'GrossMinor': GROSS + 10000}, id='Summenprobe'),
    pytest.param({'NetMinor': None}, id='Netto-fehlt'),
    pytest.param({'TaxMinor': TAX + 200, 'GrossMinor': GROSS + 200}, id='Satzprobe')])
def test_a_failed_check_gives_the_amount_without_a_tax_and_the_hint(over):
    over = {'GrossMinor': GROSS, **over}
    proposal = propose(reading(**over), bank_amount=-over['GrossMinor'])
    assert proposal.ok and (proposal.tax_rate, proposal.tax_minor) == (None, None)
    assert HINT_TAX in proposal.hints and not [reason for reason in proposal.reasons if reason.startswith('Steuer')]


def test_a_rate_of_zero_is_without_tax_the_rate_and_the_amount_stay_empty():
    proposal = propose(reading(TaxRate=0, NetMinor=GROSS, TaxMinor=0), recipient=[group(4400), group(4400)])
    assert (proposal.ok, proposal.tax_rate, proposal.tax_minor) == (True, None, None)
    assert proposal.reasons == (REASON_AMOUNT, REASON_TAX_NONE, REASON_ACCOUNT_RECIPIENT) and proposal.hints == ()


def test_the_reason_of_the_rate_names_it_with_a_comma():
    proposal = propose(reading(TaxRate=0.055, NetMinor=2000000, TaxMinor=110000, GrossMinor=2110000), bank_amount=-2110000)
    assert REASON_TAX.format(rate='5,5') in proposal.reasons and proposal.tax_rate == Decimal('0.055') and proposal.tax_minor == -110000


def test_a_refusal_is_a_proposal_with_nothing_in_it():
    for kwargs, expected in [({'has_children': True}, REFUSE_HAS_ROWS), ({'bank_currency': 'USD'}, REFUSE_CURRENCY),
                             ({'bank_amount': 0}, REFUSE_BANK_AMOUNT), ({'bank_amount': -1}, REFUSE_AMOUNT), ({'bank_amount': GROSS}, REFUSE_DIRECTION)]:
        assert propose(recipient=[group(4400), group(4400)], **kwargs) == Proposal(False, expected, None, None, None, None, (), ())
    assert propose(reading(Status='no_text')) == Proposal(False, REFUSE_NO_READING, None, None, None, None, (), ())
    assert propose(reading(ReaderVersion=0)) == Proposal(False, REFUSE_OUTDATED, None, None, None, None, (), ())


def test_the_account_comes_from_the_histories_and_the_hints_follow_the_order_tax_then_account():
    proposal = propose(reading(TaxRate=None), recipient=[group(4400), group(4400)], sender=[group(6815), group(6815)])
    assert proposal.coa_id is None and proposal.hints == (HINT_TAX, HINT_ACCOUNT_CONFLICT)
    proposal = propose(sender=[group(6815), group(6815)])
    assert proposal.coa_id == 6815 and proposal.reasons == (REASON_AMOUNT, REASON_TAX.format(rate='19'), REASON_ACCOUNT_SENDER)


def test_every_text_of_a_proposal_comes_from_the_constants_of_the_module():
    allowed = {value for name, value in vars(rp).items() if isinstance(value, str) and name.startswith(('REFUSE_', 'REASON_', 'HINT_', 'DONE_'))}
    allowed |= {REASON_TAX.format(rate=text) for text in ('19', '7', '5,5', '10,7')}
    assert REASON_TAX in allowed and len(allowed) >= 18
    seen = set()
    for over, amount, kwargs in [
            ({}, -GROSS, {}), ({'TaxRate': None}, -GROSS, {}), ({'TaxRate': 0, 'NetMinor': GROSS, 'TaxMinor': 0}, -GROSS, {}),
            ({'Kind': 'credit_note'}, GROSS, {}), ({}, -GROSS, {'recipient': [group(4400)] * 2}), ({}, -GROSS, {'sender': [group(4400)] * 2}),
            ({}, -GROSS, {'recipient': [group(4400)] * 2, 'sender': [group(4400)] * 2}),
            ({}, -GROSS, {'recipient': [group(4400)] * 2, 'sender': [group(6815)] * 2}), ({}, -GROSS, {'has_children': True}),
            ({}, -GROSS, {'bank_currency': 'USD'}), ({}, 0, {}), ({'Status': 'no_text'}, -GROSS, {}), ({'ReaderVersion': 0}, -GROSS, {}),
            ({}, -1, {}), ({}, GROSS, {}), ({'TaxRate': 0.07, 'TaxMinor': 70000, 'GrossMinor': 1070000}, -1070000, {}),
            ({'TaxRate': 0.055, 'NetMinor': 2000000, 'TaxMinor': 110000, 'GrossMinor': 2110000}, -2110000, {})]:
        proposal = propose(reading(**over), bank_amount=amount, **kwargs)
        texts = {proposal.message, *proposal.reasons, *proposal.hints}
        seen |= texts
        assert texts <= allowed, texts - allowed
    assert {DONE_TEXT, HINT_TAX, REASON_AMOUNT, HINT_NO_ACCOUNT, HINT_ACCOUNT_CONFLICT, REFUSE_AMOUNT} <= seen


def test_the_sentences_name_no_brand_and_give_no_advice():
    for name, value in vars(rp).items():
        if isinstance(value, str) and name.startswith(('REFUSE_', 'REASON_', 'HINT_', 'DONE_')):
            for word in ('WISO', 'DATEV', 'ELSTER', 'GoBD', 'steuerlich', 'absetzbar', 'Vorsteuerabzug'):
                assert word.lower() not in value.lower(), (name, word)
