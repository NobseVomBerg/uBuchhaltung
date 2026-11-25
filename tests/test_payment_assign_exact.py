# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Zahlung zuordnen mit ``exact`` (2.9.2): nur genau der angeforderte Betrag.

„Alle eindeutigen zuordnen“ schickt je Zeile den Betrag, den der Kasten zeigt (``amount_paid``), mit ``exact: true``. Die Zuordnung kappt sonst auf den Rest der Rechnung und den freien Rest der
Bewegung: Ein veralteter Kasten – inzwischen sind 50,00 € der Rechnung anders zugeordnet – ordnete still weniger zu, als er zeigt, und ließe einen Rest auf der Bewegung zurück. Mit ``exact``
lehnt ``_assign_in_tx`` IN der Transaktion ab, wenn der Betrag, der zugeordnet würde, nicht der angeforderte ist (``PAYMENT_STATE_CHANGED``, eine gewöhnliche Ablehnung der Route, nichts geschrieben).

Geprüft wird die Datenbankschicht (``assign_invoice_payment(…, exact=True)``) und die Route (``POST /invoice/link-payment``: ``exact`` streng als Wahrheitswert, nur zusammen mit ``amount_paid``,
nie an einer Stornorechnung); ohne ``exact`` bleibt alles, wie es war. Alle Namen, Nummern und Beträge sind erfunden; Daten stehen fest im Test.
"""
import datetime
import json

import pytest

from db.invoice_payments import NOTHING_TO_ASSIGN_MESSAGE, PAYMENT_STATE_CHANGED
from server import handlers
from server.handlers import handle_link_invoice_payment, link_booking_to_invoice_capped
from tests.document_helpers import state

TABLES = ('Bookings', 'InvoicePayments', 'Invoices', 'ChartOfAccounts')
INVALID = 'Ungültige Parameter'
TODAY = datetime.date(2026, 12, 31)


@pytest.fixture
def db(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda: tmp_db)               # die Route liest die Datenbank des Tests
    return tmp_db


def _account(db):
    if not [a for a in db.fetch_accounts() if a[1] == 'Exakt-Bank']:
        db.insert_account('Exakt-Bank', 'Ich', 'DE00EXAKT', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    return [a for a in db.fetch_accounts() if a[1] == 'Exakt-Bank'][0][0]


def _invoice(db, number='EX-1', gross=119.00):
    """Eine versendete Rechnung (19 %) samt Forderungsbuchung, wie beim Versenden."""
    net = round(gross / 1.19, 2)
    invoice_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-03-04', 'payment_due_date': '2099-12-31', 'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19, 'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross,
        'amount_due': gross, 'status': 'sent'})
    db.ensure_receivable_booking(invoice_id)
    return invoice_id


def _bank(db, amount, date='2026-03-10'):
    return db.insert_booking(date, amount, account_id=_account(db), booking_type='bank', recipient_client='Käufer AG')


def _amounts(db, invoice_id):
    """Die Zuordnungen einer Rechnung als ``[(ID der Bewegung, Betrag)]`` in Euro (``get_invoice_payments``)."""
    return [(row[2], float(row[3])) for row in db.get_invoice_payments(invoice_id)]


def _body(**fields):
    return json.dumps(fields).encode()


# ── Die Datenbankschicht ─────────────────────────────────────────────────────

def test_the_constant_is_the_planned_sentence():
    assert PAYMENT_STATE_CHANGED == 'Der Stand hat sich geändert – bitte die Seite neu laden.'


def test_exact_assigns_the_requested_amount_when_nothing_changed(db):
    invoice_id, bank = _invoice(db), _bank(db, 119.00)
    result = db.assign_invoice_payment(invoice_id, bank, 119.00, exact=True)
    assert result.ok and result.message is None
    assert _amounts(db, invoice_id) == [(bank, 119.0)]


def test_exact_with_a_part_amount_that_fits_assigns_just_that_part(db):
    """``exact`` heißt „genau der angeforderte Betrag“, nicht „der ganze Rest“: Ein Teilbetrag, der ganz zuzuordnen ist, geht."""
    invoice_id, bank = _invoice(db), _bank(db, 119.00)
    assert db.assign_invoice_payment(invoice_id, bank, 50.00, exact=True).ok
    assert _amounts(db, invoice_id) == [(bank, 50.0)]


def test_without_exact_the_amount_is_capped_as_before(db):
    """Der Gegenstand der Änderung: Ohne ``exact`` ordnet ein veralteter Betrag still weniger zu (der Rest der Rechnung nach einer anderen Zahlung)."""
    invoice_id, other, stale = _invoice(db), _bank(db, 50.00), _bank(db, 119.00, '2026-03-11')
    assert db.assign_invoice_payment(invoice_id, other, 50.00).ok
    assert db.assign_invoice_payment(invoice_id, stale, 119.00).ok
    assert _amounts(db, invoice_id) == [(other, 50.0), (stale, 69.0)]


def test_exact_refuses_when_the_invoice_was_paid_in_part_meanwhile_and_writes_nothing(db):
    invoice_id, other, stale = _invoice(db), _bank(db, 50.00), _bank(db, 119.00, '2026-03-11')
    assert db.assign_invoice_payment(invoice_id, other, 50.00).ok                      # inzwischen anders zugeordnet: Der Kasten zeigt noch 119,00 €
    before = state(db, *TABLES)

    result = db.assign_invoice_payment(invoice_id, stale, 119.00, exact=True)

    assert (result.ok, result.message, result.payment_id) == (False, PAYMENT_STATE_CHANGED, None)
    assert state(db, *TABLES) == before                                                  # kein Satz, keine Zuordnung, kein Konto
    assert db.assign_invoice_payment(invoice_id, stale, 69.00, exact=True).ok          # der Betrag, der jetzt zuzuordnen ist, geht


def test_exact_refuses_when_the_free_rest_of_the_movement_shrank_meanwhile(db):
    invoice_id, other_invoice = _invoice(db), _invoice(db, 'EX-2', 50.00)
    bank = _bank(db, 119.00)
    assert db.assign_invoice_payment(other_invoice, bank, 50.00).ok                    # 50,00 € der Bewegung gehören inzwischen einer anderen Rechnung
    before = state(db, *TABLES)

    result = db.assign_invoice_payment(invoice_id, bank, 119.00, exact=True)

    assert (result.ok, result.message) == (False, PAYMENT_STATE_CHANGED) and state(db, *TABLES) == before


def test_exact_keeps_the_known_reasons_for_a_paid_invoice_and_an_exhausted_movement(db):
    """Was schon vorher abgelehnt wurde, nennt weiter seinen Grund – nicht den neuen Satz."""
    invoice_id, bank = _invoice(db), _bank(db, 119.00)
    other = _bank(db, 119.00, '2026-03-11')
    assert db.assign_invoice_payment(invoice_id, other, 119.00).ok                     # die Rechnung ist bezahlt
    before = state(db, *TABLES)
    result = db.assign_invoice_payment(invoice_id, bank, 119.00, exact=True)
    assert (result.ok, result.message) == (False, 'Die Rechnung ist bereits bezahlt.') and state(db, *TABLES) == before
    second, used = _invoice(db, 'EX-2'), _bank(db, 119.00, '2026-03-12')
    assert db.assign_invoice_payment(second, used, 119.00).ok                          # die Bewegung ist ausgeschöpft
    third = _invoice(db, 'EX-3')
    before = state(db, *TABLES)
    result = db.assign_invoice_payment(third, used, 119.00, exact=True)
    assert (result.ok, result.message) == (False, NOTHING_TO_ASSIGN_MESSAGE) and state(db, *TABLES) == before


def test_exact_needs_an_amount_at_the_database_layer(db):
    invoice_id, bank = _invoice(db), _bank(db, 119.00)
    before = state(db, *TABLES)
    with pytest.raises(ValueError):
        db.assign_invoice_payment(invoice_id, bank, exact=True)
    assert state(db, *TABLES) == before


def test_the_capped_helper_hands_exact_on(db):
    invoice_id, other, stale = _invoice(db), _bank(db, 50.00), _bank(db, 119.00, '2026-03-11')
    assert link_booking_to_invoice_capped(db, invoice_id, other, 50.00) == (True, None)
    before = state(db, *TABLES)
    assert link_booking_to_invoice_capped(db, invoice_id, stale, 119.00, exact=True) == (False, PAYMENT_STATE_CHANGED)
    assert state(db, *TABLES) == before


# ── Die Route ────────────────────────────────────────────────────────────────

def test_the_route_assigns_the_shown_amount_with_exact(db):
    invoice_id, bank = _invoice(db), _bank(db, 119.00)
    code, location = handle_link_invoice_payment(_body(invoice_id=invoice_id, transaction_id=bank, amount_paid=119.0, exact=True))
    assert (code, location) == (200, f'/invoice/view?id={invoice_id}') and _amounts(db, invoice_id) == [(bank, 119.0)]


def test_the_route_answers_a_stale_amount_with_the_sentence_and_writes_nothing(db):
    invoice_id, other, stale = _invoice(db), _bank(db, 50.00), _bank(db, 119.00, '2026-03-11')
    assert db.assign_invoice_payment(invoice_id, other, 50.00).ok
    before = state(db, *TABLES)
    code, message = handle_link_invoice_payment(_body(invoice_id=invoice_id, transaction_id=stale, amount_paid=119.0, exact=True))
    assert (code, message) == (400, PAYMENT_STATE_CHANGED) and state(db, *TABLES) == before


def test_without_exact_the_route_behaves_as_before(db):
    invoice_id, other, stale = _invoice(db), _bank(db, 50.00), _bank(db, 119.00, '2026-03-11')
    assert db.assign_invoice_payment(invoice_id, other, 50.00).ok
    assert handle_link_invoice_payment(_body(invoice_id=invoice_id, transaction_id=stale, amount_paid=119.0))[0] == 200            # gekappt auf 69,00 €
    assert _amounts(db, invoice_id) == [(other, 50.0), (stale, 69.0)]
    invoice_id, bank = _invoice(db, 'EX-9'), _bank(db, 119.00, '2026-03-12')
    assert handle_link_invoice_payment(_body(invoice_id=invoice_id, transaction_id=bank, exact=False))[0] == 200                   # ohne Betrag: so viel wie möglich
    assert _amounts(db, invoice_id) == [(bank, 119.0)]


@pytest.mark.parametrize('fields', [
    {'exact': True},                                              # ohne Betrag
    {'exact': True, 'amount_paid': None},
    {'exact': True, 'amount_paid': ''},
    {'exact': 'true', 'amount_paid': 119.0},                      # als Text
    {'exact': 'yes', 'amount_paid': 119.0},
    {'exact': 1, 'amount_paid': 119.0},                           # als Zahl
    {'exact': 0, 'amount_paid': 119.0},
    {'exact': None, 'amount_paid': 119.0},
    {'exact': [], 'amount_paid': 119.0},
    {'exact': {}, 'amount_paid': 119.0},
], ids=['ohne-Betrag', 'Betrag-null', 'Betrag-leer', 'Text-true', 'Text-yes', 'Zahl-1', 'Zahl-0', 'null', 'Liste', 'Objekt'])
def test_exact_is_read_strictly_and_only_together_with_an_amount(db, fields):
    invoice_id, bank = _invoice(db), _bank(db, 119.00)
    before = state(db, *TABLES)
    code, message = handle_link_invoice_payment(_body(invoice_id=invoice_id, transaction_id=bank, **fields))
    assert (code, message) == (400, INVALID) and state(db, *TABLES) == before


def test_exact_on_a_cancellation_invoice_is_refused_before_anything_is_written(db):
    invoice_id = _invoice(db)
    storno = db.cancel_invoice(invoice_id, '2026-07-02', 'anna', today=TODAY)
    assert storno.ok
    outgoing = db.insert_booking('2026-07-10', -119.00, account_id=_account(db), booking_type='bank', recipient_client='Käufer AG')
    before = state(db, *TABLES)
    code, message = handle_link_invoice_payment(_body(invoice_id=storno.storno_id, transaction_id=outgoing, amount_paid=10.0, exact=True))
    assert (code, message) == (400, INVALID) and state(db, *TABLES) == before


def test_over_http_the_stale_amount_is_an_ordinary_refusal(server_port, auto_login):
    from tests.document_helpers import post_json
    from tests.http_client import own_origin
    db = auto_login.db
    invoice_id, other, stale = _invoice(db), _bank(db, 50.00), _bank(db, 119.00, '2026-03-11')
    assert db.assign_invoice_payment(invoice_id, other, 50.00).ok
    before = state(db, *TABLES)
    answer = post_json(server_port, '/invoice/link-payment', {'invoice_id': invoice_id, 'transaction_id': stale, 'amount_paid': 119.0, 'exact': True},
                       auto_login.csrf, headers={'Origin': own_origin(server_port)})
    assert (answer.status, json.loads(answer.text)) == (400, {'success': False, 'error': PAYMENT_STATE_CHANGED}) and state(db, *TABLES) == before
    answer = post_json(server_port, '/invoice/link-payment', {'invoice_id': invoice_id, 'transaction_id': stale, 'amount_paid': 69.0, 'exact': True},
                       auto_login.csrf, headers={'Origin': own_origin(server_port)})
    assert (answer.status, json.loads(answer.text)) == (200, {'success': True})
