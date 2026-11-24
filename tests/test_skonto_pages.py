# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Skonto bei der Zahlungszuordnung: Route, Buchungsmaske, Rechnungsseite und Kasten der Seite *Buchungen* (2.9.0).

Die Datenbankschicht bucht ein Skonto (``assign_invoice_payment(…, skonto=True)``, ``tests/test_skonto_booking.py``); hier steht, wie die Oberfläche dorthin kommt –
**nichts wird ohne Klick gebucht, nichts ist vorausgewählt:** Der Haken „Rest … als Skonto ausbuchen“ ist beim Laden nie angehakt, „Übernehmen“ eines Vorschlags setzt ihn nie, und
der Knopf „Mit Skonto zuordnen“ im Kasten ist der eine Klick, der bucht. Geprüft wird

* **die Route** ``POST /invoice/link-payment``: ``skonto`` fehlt = nein, sonst genau ``true``/``false`` (alles andere 400 „Ungültige Parameter“, nichts geschrieben); an einer
  Stornorechnung ist ``true`` 400 ``SKONTO_REFUND``; jede Ablehnung der Datenbankschicht kommt als fester Satz zurück, ohne Schreiben;
* **die Buchungsmaske** (``handle_add_transaction``): Feld ``skonto`` streng (fehlt = nein, ``1`` = ja, sonst 400), bei einer NEUEN Bewegung lehnt die Vorprüfung VOR dem Schreiben ab (es
  entsteht keine Bankbewegung), bei einer vorhandenen bleibt der Nutzer bei der gespeicherten Buchung (``PAYMENT_NOT_ASSIGNED``); öffnen und unverändert speichern ändert nichts;
* **die Seiten:** der Haken nur, wo die Rechnung ein Skonto zulässt (die Zeile „Skonto: 2 % = 2,38 € bis …“ mit der Frist, bei abgelaufener Frist der Zusatz, der feste Satz bei einer Rechnung mit
  mehreren Sätzen, Stammdaten oder einem Satz ohne Skonto-Konto), jeder Wert maskiert, die Zahlungstabelle mit „+ Skonto“ und der Summenzeile, der Haken der Maske nur mit einer Option, die ein
  Skonto kennt;
* **die Skripte** – die echten, aus den Seiten geschnitten – in Node auf einer DOM-Attrappe (``tests/test_mixed_tax_mask.HARNESS``): wann der Haken erscheint und verschwindet (und dabei den Haken
  zurücknimmt), was ``linkPayment`` und ``payAssign`` schicken; statisch nur ``var`` und ``function`` mit dem Präfix ``sko``;
* **die Antwort des Stornierens** nennt in ``hint``, dass das Skonto zurückgenommen wurde – nur, wenn es eines gab.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; Daten stehen fest im Test (kein „heute“ – Fristen liegen weit zurück bzw. weit voraus).
"""
import datetime
import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import urllib.parse
from contextlib import closing
from html import unescape

import pytest

import server.handlers as handlers
import server.pages_invoice as pages_invoice
import server.pages_transactions as pages_transactions
from db.invoice_payments import (SKONTO_MIXED, SKONTO_NO_REST, SKONTO_NO_TERMS, SKONTO_RATE, SKONTO_REFUND, SKONTO_TAX_ENTRY, SKONTO_TOO_LARGE)
from money import fmt_eur
from server.handlers import INVALID_REQUEST, PAYMENT_NOT_ASSIGNED, SKONTO_TAKEN_BACK_HINT, handle_add_transaction, handle_link_invoice_payment
from server.pages import script_json
from server.pages_invoice import PageInvoice, SKONTO_CHECK_TITLE, SKONTO_EXPIRED, SKONTO_INVOICE_JS, SKONTO_JS, skonto_check_html
from server.pages_transactions import (PAY_SKONTO_BUTTON, PAY_SKONTO_HINT, PAY_SKONTO_TITLE, PAY_SUGGEST_JS, PageTransactions, SKONTO_MASK_JS)
from tests import installation
from tests.document_helpers import post_json, state
from tests.dom_helpers import Dom
from tests.entry_form_roundtrip import differences, open_form, snapshot, submit
from tests.http_client import own_origin, post
from tests.test_invoice_storno import at, sent as world_sent, world  # noqa: F401  (world: Fixture mit den Routen der Oberfläche)
from tests.test_mixed_tax_mask import HARNESS, extract_function
from tests.test_payment_suggestions import (TERMS, add_invoice, add_payment, bank_account, box_rows, customer, discount_world, period_page, run_sql)

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die Läufe der Skripte entfallen, das HTML und die Aufbauprüfungen decken das Wichtigste ab')

HOSTILE = '</script><img src=x onerror=alert(1)>'
TABLES = ('Bookings', 'Invoices', 'InvoicePayments', 'ChartOfAccounts', 'InvoiceItems')


# ── Hilfen ───────────────────────────────────────────────────────────────────

def raw(db, sql, *params):
    with closing(sqlite3.connect(db.db_name)) as conn:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows


def link(payload):
    """``POST /invoice/link-payment`` ohne HTTP: Statuscode und Text des Handlers."""
    return handle_link_invoice_payment(json.dumps(payload).encode())


def assigned(db, invoice_id):
    """Die Zuordnungen einer Rechnung: (Geld, Skonto) in Minor Units."""
    return raw(db, 'SELECT Amount, SkontoAmount FROM InvoicePayments WHERE InvoiceID = ? ORDER BY ID', invoice_id)


def status_of(db, invoice_id):
    ((status, due),) = raw(db, 'SELECT Status, AmountDue FROM Invoices WHERE ID = ?', invoice_id)
    return status, due


@pytest.fixture
def routes(tmp_db, monkeypatch):
    """Die Handler arbeiten auf der Testdatenbank (``handlers.Database()``) – wie die Routen im Betrieb."""
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return tmp_db


def new_form(account, amount, invoice_id=None, date='2026-03-10', **extra):
    """Die Felder der Buchungsmaske für eine NEUE Bankbewegung (wie ein Browser sie schickt); ``extra`` ergänzt oder ersetzt Felder (``skonto=['1']``)."""
    post = {'transaction_id': ['0'], 'date': [date], 'amount': [str(amount)], 'account': [str(account)], 'recipient': ['Käufer AG'], 'text': ['Danke'],
            'invoice_id': ['' if invoice_id is None else str(invoice_id)], 'currency': ['EUR']}
    post.update(extra)
    return post


def mask_post(db, booking_id, **extra):
    """Die Maske einer vorhandenen Bankbewegung, geöffnet und abgeschickt wie im Browser, mit ``extra`` (``invoice_id``, ``skonto``)."""
    post, _options = open_form(db, booking_id)
    post.update({key: value for key, value in extra.items()})
    return post


# ═════════════════════════════════════════════════════════════════════════════
# Die Route POST /invoice/link-payment
# ═════════════════════════════════════════════════════════════════════════════

def test_without_the_field_the_route_assigns_what_it_always_did(routes):
    account, _buyer, invoice_id = discount_world(routes)
    payment = add_payment(routes, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    code, _text = link({'invoice_id': invoice_id, 'transaction_id': payment})

    assert code == 200
    assert assigned(routes, invoice_id) == [(1166200, None)] and status_of(routes, invoice_id) == ('partial_payment', 23800)       # eine Teilzahlung: der Rest bleibt offen


def test_false_is_the_same_as_the_missing_field(routes):
    account, _buyer, invoice_id = discount_world(routes)
    payment = add_payment(routes, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    assert link({'invoice_id': invoice_id, 'transaction_id': payment, 'skonto': False})[0] == 200
    assert assigned(routes, invoice_id) == [(1166200, None)]


def test_true_assigns_the_payment_and_books_the_rest_as_the_discount(routes):
    account, _buyer, invoice_id = discount_world(routes)
    payment = add_payment(routes, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    code, text = link({'invoice_id': invoice_id, 'transaction_id': payment, 'skonto': True})

    assert (code, text) == (200, f'/invoice/view?id={invoice_id}')
    assert assigned(routes, invoice_id) == [(1166200, 23800)] and status_of(routes, invoice_id) == ('paid', 0)
    assert [row[0] for row in raw(routes, "SELECT c.AccountNumber FROM Bookings b JOIN ChartOfAccounts c ON c.ID = b.COA_ID WHERE b.Text LIKE 'Skonto%'")] == [4736]


@pytest.mark.parametrize('value', ['true', 'false', '1', '0', 1, 0, None, [], [True], {}, 'ja', ''], ids=lambda value: repr(value))
def test_any_other_value_is_a_bad_request_and_nothing_is_written(routes, value):
    account, _buyer, invoice_id = discount_world(routes)
    payment = add_payment(routes, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    before = installation.dump(routes.db_name)

    assert link({'invoice_id': invoice_id, 'transaction_id': payment, 'skonto': value}) == (400, 'Ungültige Parameter')
    assert installation.dump(routes.db_name) == before


@pytest.mark.parametrize('terms, sentence', [
    ({'skonto_days': None, 'skonto_percent': None}, SKONTO_NO_TERMS), ({'mixed_tax_rates': 1}, SKONTO_MIXED), ({'tax_rate': 0.16}, SKONTO_RATE),
], ids=['ohne-Bedingungen', 'Satz-je-Position', '16-Prozent'])
def test_a_refusal_of_the_database_layer_comes_back_as_its_fixed_sentence_and_nothing_is_written(routes, terms, sentence):
    account, _buyer, invoice_id = discount_world(routes, **terms)
    payment = add_payment(routes, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    before = installation.dump(routes.db_name)

    assert link({'invoice_id': invoice_id, 'transaction_id': payment, 'skonto': True}) == (400, sentence)
    assert installation.dump(routes.db_name) == before


def test_an_invoice_from_the_master_data_comes_back_with_its_sentence(routes):
    account, _buyer, invoice_id = discount_world(routes)
    routes.insert_invoice_item({'invoice_id': invoice_id, 'position': 1, 'description': 'Anstrich', 'quantity': 1, 'unit': 'Stk.', 'price_per_unit': 100.0,
                                'total_net': 100.0, 'tax_category': 'S', 'tax_rate': 0.19, 'tax_rate_bp': 1900})
    payment = add_payment(routes, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    assert link({'invoice_id': invoice_id, 'transaction_id': payment, 'skonto': True}) == (400, SKONTO_TAX_ENTRY)


def test_a_rest_that_is_too_big_or_missing_names_both_amounts_and_writes_nothing(routes):
    account, _buyer, invoice_id = discount_world(routes)
    too_small = add_payment(routes, account, 100.00, '2026-03-10', 'Käufer AG', 'Danke')
    before = installation.dump(routes.db_name)

    code, text = link({'invoice_id': invoice_id, 'transaction_id': too_small, 'skonto': True})

    assert (code, text) == (400, SKONTO_TOO_LARGE.format(rest=fmt_eur(19), skonto=fmt_eur('2.38')))
    assert installation.dump(routes.db_name) == before
    full = add_payment(routes, account, 119.00, '2026-03-11', 'Käufer AG', 'Danke')
    assert link({'invoice_id': invoice_id, 'transaction_id': full, 'skonto': True}) == (400, SKONTO_NO_REST)


def test_the_deadline_is_never_checked_by_the_route(routes):
    """Die Rechnung vom 04.03.2026 mit 14 Tagen Frist: bezahlt am 30.12.2026 – das Skonto wird trotzdem gebucht (die Frist wird gezeigt, nie erzwungen)."""
    account, _buyer, invoice_id = discount_world(routes)
    payment = add_payment(routes, account, 116.62, '2026-12-30', 'Käufer AG', 'Danke')
    assert link({'invoice_id': invoice_id, 'transaction_id': payment, 'skonto': True})[0] == 200
    assert assigned(routes, invoice_id) == [(1166200, 23800)]


def test_a_credit_note_takes_no_discount_and_nothing_is_written(routes):
    account, _buyer, invoice_id = discount_world(routes)
    paid = add_payment(routes, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    assert link({'invoice_id': invoice_id, 'transaction_id': paid})[0] == 200
    storno = routes.cancel_invoice(invoice_id, '2026-04-01', 'anna')
    assert storno.ok, storno.message
    refund = routes.insert_booking('2026-04-05', -116.62, account_id=account, booking_type='bank', recipient_client='Käufer AG')
    before = installation.dump(routes.db_name)

    assert link({'invoice_id': storno.storno_id, 'transaction_id': refund, 'skonto': True}) == (400, SKONTO_REFUND)
    assert installation.dump(routes.db_name) == before
    assert SKONTO_REFUND == 'Skonto gibt es nur bei einer Zahlung, nicht bei einer Erstattung.'
    # ohne ``skonto`` (und mit ``false``) ist es die Erstattung wie immer
    assert link({'invoice_id': storno.storno_id, 'transaction_id': refund, 'skonto': False})[0] == 200
    assert routes.get_storno_credit(storno.storno_id) == 0


def test_the_route_over_http_books_the_discount_with_the_token_and_the_origin(server_port, auto_login):
    db = auto_login.db
    account, _buyer, invoice_id = discount_world(db)
    payment = add_payment(db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    answer = post_json(server_port, '/invoice/link-payment', {'invoice_id': invoice_id, 'transaction_id': payment, 'skonto': True}, auto_login.csrf,
                       headers={'Origin': own_origin(server_port)})

    assert (answer.status, json.loads(answer.text)) == (200, {'success': True})
    assert assigned(db, invoice_id) == [(1166200, 23800)]
    bad = post_json(server_port, '/invoice/link-payment', {'invoice_id': invoice_id, 'transaction_id': payment, 'skonto': 'true'}, auto_login.csrf,
                    headers={'Origin': own_origin(server_port)})
    assert (bad.status, json.loads(bad.text)) == (400, {'success': False, 'error': 'Ungültige Parameter'})


def test_the_mask_over_http_books_the_discount_and_refuses_a_value_that_is_no_one(server_port, auto_login):
    """Über den echten Server: ein unbekannter Wert ist 400 als Klartext im Body (nie im ``Location``-Kopf), und es entsteht keine Bankbewegung; mit ``1`` wird gebucht."""
    db = auto_login.db
    account, _buyer, invoice_id = discount_world(db)
    headers = {'Origin': own_origin(server_port)}
    form = {'transaction_id': '0', 'date': '2026-03-10', 'amount': '116.62', 'account': str(account), 'recipient': 'Käufer AG', 'text': 'Danke',
            'invoice_id': str(invoice_id), 'currency': 'EUR'}

    bad = post(server_port, '/transactions/add', headers=headers, form={**form, 'skonto': 'true'}, csrf=auto_login.csrf)

    assert (bad.status, bad.text) == (400, INVALID_REQUEST) and 'Location' not in bad.headers
    assert raw(db, "SELECT COUNT(*) FROM Bookings WHERE BookingType = 'bank'") == [(0,)]
    good = post(server_port, '/transactions/add', headers=headers, form={**form, 'skonto': '1'}, csrf=auto_login.csrf)
    assert (good.status, good.headers['Location']) == (303, f'/invoice/edit?id={invoice_id}')
    assert assigned(db, invoice_id) == [(1166200, 23800)]


# ═════════════════════════════════════════════════════════════════════════════
# Die Buchungsmaske (handle_add_transaction)
# ═════════════════════════════════════════════════════════════════════════════

def test_a_new_booking_with_the_tick_is_created_and_assigned_with_the_discount(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)

    status, location = handle_add_transaction(tmp_db, new_form(account, '116.62', invoice_id, skonto=['1']))

    assert (status, location) == (303, f'/invoice/edit?id={invoice_id}')
    assert assigned(tmp_db, invoice_id) == [(1166200, 23800)] and status_of(tmp_db, invoice_id) == ('paid', 0)


def test_a_new_booking_without_the_tick_is_a_part_payment_as_before(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)

    assert handle_add_transaction(tmp_db, new_form(account, '116.62', invoice_id)) == (303, f'/invoice/edit?id={invoice_id}')
    assert assigned(tmp_db, invoice_id) == [(1166200, None)] and status_of(tmp_db, invoice_id) == ('partial_payment', 23800)


@pytest.mark.parametrize('amount, sentence', [
    ('100.00', SKONTO_TOO_LARGE.format(rest=fmt_eur(19), skonto=fmt_eur('2.38'))), ('119.00', SKONTO_NO_REST), ('130.00', SKONTO_NO_REST),
], ids=['Rest-zu-groß', 'kein-Rest', 'Überzahlung'])
def test_a_new_booking_that_cannot_close_with_a_discount_is_refused_before_anything_is_written(tmp_db, amount, sentence):
    """Die Vorprüfung (``payment_precheck``) lehnt vor dem Anlegen ab: Es entsteht keine Bankbewegung, kein Satz, kein Konto."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    before = installation.dump(tmp_db.db_name)

    assert handle_add_transaction(tmp_db, new_form(account, amount, invoice_id, skonto=['1'])) == (400, sentence)
    assert installation.dump(tmp_db.db_name) == before
    assert raw(tmp_db, "SELECT COUNT(*) FROM Bookings WHERE BookingType = 'bank'") == [(0,)]


@pytest.mark.parametrize('terms, sentence', [
    ({'skonto_days': None, 'skonto_percent': None}, SKONTO_NO_TERMS), ({'mixed_tax_rates': 1}, SKONTO_MIXED), ({'tax_rate': 0.16}, SKONTO_RATE),
], ids=['ohne-Bedingungen', 'Satz-je-Position', '16-Prozent'])
def test_a_new_booking_for_an_invoice_without_a_discount_is_refused_before_anything_is_written(tmp_db, terms, sentence):
    account, _buyer, invoice_id = discount_world(tmp_db, **terms)
    before = installation.dump(tmp_db.db_name)

    assert handle_add_transaction(tmp_db, new_form(account, '116.62', invoice_id, skonto=['1'])) == (400, sentence)
    assert installation.dump(tmp_db.db_name) == before


@pytest.mark.parametrize('value', [[''], ['0'], ['true'], ['on'], ['yes'], ['2'], [' 1'], ['1', '1'], ['1', ''], []], ids=repr)
def test_any_other_value_of_the_field_is_a_bad_request_and_nothing_is_saved(tmp_db, value):
    """Streng: fehlt das Feld, ist es „nein“; sonst gilt genau EIN Wert, und er ist ``1``. Ein unbekannter Wert wird nie als Zustimmung gelesen – und nie still als „nein“ übergangen."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    before = installation.dump(tmp_db.db_name)

    assert handle_add_transaction(tmp_db, new_form(account, '116.62', invoice_id, skonto=value)) == (400, INVALID_REQUEST)
    assert installation.dump(tmp_db.db_name) == before


def test_the_field_without_an_invoice_does_nothing(tmp_db):
    """Der Haken gehört zur gewählten Rechnung: Ohne sie (er ist dann versteckt und abgehakt) wird nichts zugeordnet – eine Bewegung entsteht wie immer."""
    account, _buyer, invoice_id = discount_world(tmp_db)

    assert handle_add_transaction(tmp_db, new_form(account, '116.62', None, skonto=['1'])) == (303, '/transactions')
    assert assigned(tmp_db, invoice_id) == [] and raw(tmp_db, "SELECT COUNT(*) FROM Bookings WHERE BookingType = 'bank'") == [(1,)]


def test_an_existing_booking_is_saved_and_assigned_with_the_discount(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    status, location = handle_add_transaction(tmp_db, mask_post(tmp_db, payment, invoice_id=[str(invoice_id)], skonto=['1']))

    assert (status, location) == (303, f'/invoice/edit?id={invoice_id}')
    assert assigned(tmp_db, invoice_id) == [(1166200, 23800)] and status_of(tmp_db, invoice_id) == ('paid', 0)


def test_an_existing_booking_whose_assignment_fails_stays_saved_and_names_the_reason(tmp_db):
    """Bei einer vorhandenen Bewegung wird erst gespeichert, dann zugeordnet: Scheitert das Zuordnen (hier: der Rest ist zu groß), bleibt der Nutzer bei der Buchung – mit dem Grund."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 100.00, '2026-03-10', 'Käufer AG', 'Danke')

    status, location = handle_add_transaction(tmp_db, mask_post(tmp_db, payment, invoice_id=[str(invoice_id)], skonto=['1'], text=['Geändert']))

    reason = SKONTO_TOO_LARGE.format(rest=fmt_eur(19), skonto=fmt_eur('2.38'))
    assert status == 303 and location.startswith(f'/transactions/edit?id={payment}&err=')
    assert urllib.parse.unquote(location.split('err=', 1)[1]) == PAYMENT_NOT_ASSIGNED.format(reason=reason)
    assert assigned(tmp_db, invoice_id) == [] and raw(tmp_db, 'SELECT Text FROM Bookings WHERE ID = ?', payment) == [('Geändert',)]


def test_opening_the_mask_and_saving_it_unchanged_changes_nothing_and_assigns_nothing(tmp_db):
    """Die Maske mit dem versteckten Haken: Ein Browser schickt das Feld nur mit angehaktem Haken – beim Laden ist er nie angehakt."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    post, _options = open_form(tmp_db, payment)
    assert 'skonto' not in post and post['invoice_id'] == ['']

    before = snapshot(tmp_db)
    submit(tmp_db, post)

    assert differences(before, snapshot(tmp_db)) == [] and assigned(tmp_db, invoice_id) == []


def test_a_mask_whose_booking_is_partly_used_by_another_assignment_books_with_the_free_rest(tmp_db):
    """500,00 €: 383,38 € gehören einer anderen Rechnung, frei sind 116,62 € – Zuordnen (ohne Betrag) nimmt den freien Rest, und der Rest nach ihm ist das Skonto."""
    account, buyer, invoice_id = discount_world(tmp_db)
    other = add_invoice(tmp_db, 'R26002', 383.38, customer_id=buyer)
    payment = add_payment(tmp_db, account, 500.00, '2026-03-10', 'Käufer AG', 'Sammelzahlung')
    assert tmp_db.assign_invoice_payment(other, payment).ok

    status, _location = handle_add_transaction(tmp_db, mask_post(tmp_db, payment, invoice_id=[str(invoice_id)], skonto=['1']))

    assert status == 303 and assigned(tmp_db, invoice_id) == [(1166200, 23800)] and status_of(tmp_db, other) == ('paid', 0)


# ═════════════════════════════════════════════════════════════════════════════
# Die Buchungsmaske: der Haken
# ═════════════════════════════════════════════════════════════════════════════

def the_row(page, row_id):
    """Die Attribute der versteckten Zeile des Hakens (``<div id=…>``) – es gibt genau eine mit dieser ``id``."""
    return Dom(page).one('div', id=row_id)


def test_the_mask_of_a_booking_offers_the_tick_hidden_and_never_ticked(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    page = PageTransactions(tmp_db, edit_transaction_id=payment)

    row = the_row(page, 'skontoRow')
    assert 'hidden' in row and row['data-used'] == '0.00'
    box = Dom(page).one('input', id='skontoBox')
    assert (box['type'], box['name'], box['value'], box['autocomplete']) == ('checkbox', 'skonto', '1', 'off')
    assert 'checked' not in box                                                             # nie vorgewählt
    assert 'Rest <span id="skontoRest"></span> als Skonto ausbuchen</label>' in page
    assert f'title="{SKONTO_CHECK_TITLE}"' in page


def test_the_options_that_allow_a_discount_carry_the_open_amount_and_the_discount(tmp_db):
    account, buyer, invoice_id = discount_world(tmp_db)
    plain = add_invoice(tmp_db, 'R26002', 238.00, customer_id=buyer)                         # ohne Bedingungen: keine Angaben
    seven = add_invoice(tmp_db, 'R26003', 107.00, customer_id=buyer, tax_rate=0.07, **TERMS)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    options = {option['value']: option for tag, option in Dom(PageTransactions(tmp_db, edit_transaction_id=payment)).tags if tag == 'option' and 'value' in option}

    assert (options[str(invoice_id)]['data-skonto-open'], options[str(invoice_id)]['data-skonto-limit']) == ('119.00', '2.38')
    assert (options[str(seven)]['data-skonto-open'], options[str(seven)]['data-skonto-limit']) == ('107.00', '2.14')
    assert 'data-skonto-open' not in options[str(plain)] and 'data-skonto-limit' not in options[str(plain)]
    assert 'data-skonto-open' not in options['']                                              # „-- Keine Rechnung --“


def test_a_part_paid_invoice_carries_its_open_amount_and_the_discount_of_the_gross(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    assert tmp_db.assign_invoice_payment(invoice_id, add_payment(tmp_db, account, 50.00, '2026-03-06', 'Käufer AG', 'Anzahlung')).ok
    payment = add_payment(tmp_db, account, 66.62, '2026-03-10', 'Käufer AG', 'Rest')

    options = {option['value']: option for tag, option in Dom(PageTransactions(tmp_db, edit_transaction_id=payment)).tags if tag == 'option' and 'value' in option}

    assert (options[str(invoice_id)]['data-skonto-open'], options[str(invoice_id)]['data-skonto-limit']) == ('69.00', '2.38')


def test_without_a_discount_anywhere_the_mask_has_no_tick_and_no_script(tmp_db):
    account, buyer, _invoice = discount_world(tmp_db, skonto_days=None, skonto_percent=None)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    page = PageTransactions(tmp_db, edit_transaction_id=payment)

    assert 'skontoRow' not in page and 'skontoBox' not in page and 'skoMask' not in page and 'name="skonto"' not in page


def test_a_booking_that_cannot_be_assigned_has_no_tick(tmp_db):
    """Eine Bankbewegung ohne freien Rest oder ein Ausgang: keine Auswahl „Rechnung“, also auch kein Haken."""
    account, _buyer, _invoice = discount_world(tmp_db)
    outgoing = add_payment(tmp_db, account, -116.62, '2026-03-10', 'Käufer AG', 'Erstattung')

    page = PageTransactions(tmp_db, edit_transaction_id=outgoing)

    assert 'skontoRow' not in page and 'select name="invoice_id"' not in page


def test_a_new_booking_has_the_tick_too_and_it_is_hidden_until_the_amount_fits(tmp_db):
    discount_world(tmp_db)

    page = PageTransactions(tmp_db)

    assert 'hidden' in the_row(page, 'skontoRow') and the_row(page, 'skontoRow')['data-used'] == '0.00'
    assert 'checked' not in Dom(page).one('input', id='skontoBox')


def test_the_row_of_a_partly_used_booking_carries_what_the_others_use(tmp_db):
    account, buyer, _invoice = discount_world(tmp_db)
    other = add_invoice(tmp_db, 'R26002', 383.38, customer_id=buyer)
    payment = add_payment(tmp_db, account, 500.00, '2026-03-10', 'Käufer AG', 'Sammelzahlung')
    assert tmp_db.assign_invoice_payment(other, payment).ok

    assert the_row(PageTransactions(tmp_db, edit_transaction_id=payment), 'skontoRow')['data-used'] == '383.38'


def test_taking_over_the_suggestion_does_not_set_the_tick(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    page = PageTransactions(tmp_db, edit_transaction_id=payment)

    line = page[page.index('<div class="hint" id="invoiceSuggestion">'):]
    line = line[:line.index('</div>') + 6]
    assert 'Betrag nach Skonto stimmt' in line
    assert "onclick=\"this.form.elements['invoice_id'].value = '%d'\"" % invoice_id in line          # der Knopf setzt nur den Wert der Auswahl
    assert 'skonto' not in line.replace('Betrag nach Skonto stimmt', '').lower()                       # … und nichts am Haken
    assert 'checked' not in Dom(page).one('input', id='skontoBox')


def test_the_mask_values_stay_masked_with_hostile_texts(tmp_db):
    """Die Optionen tragen nur Zahlen aus der Datenbank (Betrag, Skonto) und den maskierten Namen; der Aufbau der Seite hängt von keinem Text ab."""
    account, buyer, invoice_id = discount_world(tmp_db)
    run_sql(tmp_db, 'UPDATE Invoices SET BuyerCompany = ? WHERE ID = ?', f'Käufer {HOSTILE}', invoice_id)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', f'Käufer {HOSTILE}', 'Danke')

    page = PageTransactions(tmp_db, edit_transaction_id=payment)

    assert '<img src=x' not in page and [tag for tag in Dom(page).all('img') if tag.get('src') == 'x'] == []
    assert '&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;' in page


# ═════════════════════════════════════════════════════════════════════════════
# Die Rechnungsseite
# ═════════════════════════════════════════════════════════════════════════════

def info_of(page):
    """Der Absatz mit den Skonto-Angaben (``paySkontoInfo``): (Text, Attribute) – oder ``None``."""
    found = re.search(r'<p class="hint" id="paySkontoInfo"([^>]*)>(.*?)</p>', page, re.S)
    if not found:
        return None
    return unescape(found.group(2)), dict(re.findall(r'([\w-]+)="([^"]*)"', found.group(1)))


def test_the_invoice_page_names_what_the_invoice_provides_with_the_deadline(tmp_db):
    """2 % von 119,00 € sind 2,38 €; die Frist: Rechnungsdatum plus 14 Tage – liegt sie weit voraus, steht kein Zusatz da."""
    _account, _buyer, invoice_id = discount_world(tmp_db, date='2099-03-04')

    text, attributes = info_of(PageInvoice(tmp_db, {}, invoice_id))

    assert text == 'Skonto: 2 % = 2,38 € bis 18.03.2099'
    assert (attributes['data-open'], attributes['data-limit']) == ('119.00', '2.38')


def test_an_expired_deadline_is_named_and_forbids_nothing(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db, date='2000-03-04')
    add_payment(tmp_db, account, 116.62, '2000-03-10', 'Käufer AG', 'Danke')

    page = PageInvoice(tmp_db, {}, invoice_id)

    assert info_of(page)[0] == 'Skonto: 2 % = 2,38 € bis 18.03.2000 (Frist abgelaufen)' and SKONTO_EXPIRED == ' (Frist abgelaufen)'
    assert 'id="paySkonto_' in page and 'hidden' in the_row(page, 'paySkontoRow_2')                  # das Häkchen gibt es trotzdem


def test_the_last_day_of_the_deadline_is_not_yet_expired(tmp_db, monkeypatch):
    """„Frist abgelaufen“ erst NACH dem letzten Tag: Heute ist der letzte Tag (Rechnungsdatum + 14) – die Frist läuft noch."""
    today = datetime.date.today()
    _account, _buyer, invoice_id = discount_world(tmp_db, date=(today - datetime.timedelta(days=14)).isoformat())
    text = info_of(PageInvoice(tmp_db, {}, invoice_id))[0]
    assert text.endswith(f'bis {today.strftime("%d.%m.%Y")}')


def test_a_percent_with_decimals_is_named_with_a_comma(tmp_db):
    _account, _buyer, invoice_id = discount_world(tmp_db, skonto_days=10, skonto_percent=2.5, date='2099-03-04')
    assert info_of(PageInvoice(tmp_db, {}, invoice_id))[0] == 'Skonto: 2,5 % = 2,98 € bis 14.03.2099'            # 2,5 % von 119,00 € sind 2,975 € → 2,98 €


def test_without_discount_terms_the_page_names_nothing_and_has_no_tick(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db, skonto_days=None, skonto_percent=None)
    add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    page = PageInvoice(tmp_db, {}, invoice_id)

    assert info_of(page) is None and 'id="paySkonto' not in page and 'skoInvoiceInit' not in page            # (das Formular oben hat sein eigenes Feld „Skonto:“)
    assert 'Vorhandene Buchung verknüpfen' in page                                                    # die Liste selbst bleibt


@pytest.mark.parametrize('terms, sentence', [
    ({'mixed_tax_rates': 1}, SKONTO_MIXED), ({'tax_rate': 0.16}, SKONTO_RATE),
], ids=['Satz-je-Position', '16-Prozent'])
def test_an_invoice_with_terms_but_without_a_discount_account_names_the_fixed_sentence_and_has_no_tick(tmp_db, terms, sentence):
    account, _buyer, invoice_id = discount_world(tmp_db, **terms)
    add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    page = PageInvoice(tmp_db, {}, invoice_id)

    assert f'<p class="hint">{sentence}</p>' in page
    assert 'id="paySkonto' not in page and 'skoInvoiceInit' not in page


def test_an_invoice_from_the_master_data_names_its_sentence(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 1, 'description': 'Anstrich', 'quantity': 1, 'unit': 'Stk.', 'price_per_unit': 100.0,
                                'total_net': 100.0, 'tax_category': 'S', 'tax_rate': 0.19, 'tax_rate_bp': 1900})
    add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    assert f'<p class="hint">{SKONTO_TAX_ENTRY}</p>' in PageInvoice(tmp_db, {}, invoice_id)


def test_each_candidate_has_a_hidden_tick_that_is_never_ticked(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    one = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    two = add_payment(tmp_db, account, 20.00, '2026-03-12', 'Käufer AG', 'Anderes')

    page = PageInvoice(tmp_db, {}, invoice_id)

    for payment, free in ((one, '116.62'), (two, '20.00')):
        row = the_row(page, f'paySkontoRow_{payment}')
        assert 'hidden' in row and row['data-free'] == free
        box = Dom(page).one('input', id=f'paySkonto_{payment}')
        assert box['type'] == 'checkbox' and box['autocomplete'] == 'off' and 'checked' not in box and 'name' not in box        # nie vorgewählt, kein Formularfeld
        assert Dom(page).one('input', id=f'payAmount_{payment}')['oninput'] == f'skoInvoiceRow({payment})'
        assert f'Rest <span id="paySkontoRest_{payment}"></span> als Skonto ausbuchen</label>' in page
    assert sorted(int(n) for n in re.findall(r'onclick="linkPayment\((\d+), this\)"', page)) == sorted([one, two])                 # die Knöpfe bleiben, wie sie waren
    (rows,) = re.findall(r'var SKO_ROWS = (\[[^\]]*\]);', page)
    assert sorted(json.loads(rows)) == sorted([one, two]) and page.count('skoInvoiceInit();') == 1


def test_the_part_payment_of_a_discount_shows_under_its_amount_and_in_the_totals(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    assert tmp_db.assign_invoice_payment(invoice_id, payment, None, None, True).ok

    page = PageInvoice(tmp_db, {}, invoice_id)

    assert '116,62 €<div class="hint">+ Skonto 2,38 €</div>' in page
    table = page[page.index('Gezahlt gesamt'):page.index('</table>', page.index('Gezahlt gesamt'))]
    assert re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', table)).strip() == 'Gezahlt gesamt 116,62 € Skonto 2,38 € Noch offen 0,00 €'


def test_a_payment_without_a_discount_shows_no_skonto_line(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    assert tmp_db.assign_invoice_payment(invoice_id, add_payment(tmp_db, account, 50.00, '2026-03-10', 'Käufer AG', 'Anzahlung')).ok

    page = PageInvoice(tmp_db, {}, invoice_id)

    assert '+ Skonto' not in page and '>Skonto<' not in page
    assert 'Gezahlt gesamt' in page and 'Noch offen' in page


def test_two_payments_one_with_the_discount_show_the_discount_only_at_the_closing_one(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    assert tmp_db.assign_invoice_payment(invoice_id, add_payment(tmp_db, account, 50.00, '2026-03-06', 'Käufer AG', 'Anzahlung')).ok
    assert tmp_db.assign_invoice_payment(invoice_id, add_payment(tmp_db, account, 66.62, '2026-03-10', 'Käufer AG', 'Rest'), None, None, True).ok

    page = PageInvoice(tmp_db, {}, invoice_id)

    assert page.count('+ Skonto 2,38 €') == 1
    assert '50,00 €</td>' in page and '66,62 €<div class="hint">+ Skonto 2,38 €</div>' in page


def test_a_paid_invoice_has_no_tick_and_no_line(tmp_db):
    """Nichts mehr offen: keine Zahlungs-Aktionen – und damit weder die Zeile „Skonto: …“ noch ein Häkchen."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    assert tmp_db.assign_invoice_payment(invoice_id, add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke'), None, None, True).ok

    page = PageInvoice(tmp_db, {}, invoice_id)

    assert info_of(page) is None and 'id="paySkonto' not in page


def test_a_date_that_is_no_date_names_no_deadline_and_no_error(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    run_sql(tmp_db, 'UPDATE Invoices SET InvoiceDate = ? WHERE ID = ?', 'im März', invoice_id)
    add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    assert info_of(PageInvoice(tmp_db, {}, invoice_id))[0] == 'Skonto: 2 % = 2,38 €'


def test_every_value_on_the_invoice_page_stays_masked(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    add_payment(tmp_db, account, 116.62, '2026-03-10', f'Käufer {HOSTILE}', 'Danke')

    page = PageInvoice(tmp_db, {}, invoice_id)

    assert '<img src=x' not in page and [tag for tag in Dom(page).all('img') if tag.get('src') == 'x'] == []
    assert 'Käufer &lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;' in page


# ═════════════════════════════════════════════════════════════════════════════
# Der Kasten „Zahlungseingänge zu offenen Rechnungen“
# ═════════════════════════════════════════════════════════════════════════════

def test_a_suggestion_with_the_discounted_amount_gets_the_second_button(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')

    page = period_page(tmp_db)

    (row,) = box_rows(page)
    assert row[-2] == 'Betrag nach Skonto stimmt · Kunde passt' and row[-1] == f'Zuordnen {PAY_SKONTO_BUTTON}'
    assert f'onclick="payAssign({invoice_id}, {payment}, this)">Zuordnen</button>' in page                       # „Zuordnen“ bleibt: die Teilzahlung wie bisher
    assert f'onclick="payAssign({invoice_id}, {payment}, this, true)">{PAY_SKONTO_BUTTON}</button>' in page
    assert f'title="{PAY_SKONTO_TITLE}"' in page and PAY_SKONTO_HINT in page
    assert PAY_SKONTO_BUTTON == 'Mit Skonto zuordnen'


def test_a_suggestion_without_the_discount_signal_keeps_one_button_and_the_old_hint(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 119.00, '2026-03-10', 'Käufer AG', 'Danke')

    page = period_page(tmp_db)

    (row,) = box_rows(page)
    assert row[-2:] == ['Betrag stimmt · Kunde passt', 'Zuordnen']
    assert 'this, true' not in page and PAY_SKONTO_BUTTON not in page and PAY_SKONTO_HINT not in page
    assert '<p class="hint">Vorschläge aus allen Zeiträumen, nicht nur aus dem gewählten. Zugeordnet wird erst mit „Zuordnen“.</p>' in page


def test_only_the_rows_with_the_signal_have_the_second_button(tmp_db):
    account, buyer, invoice_id = discount_world(tmp_db)
    other = add_invoice(tmp_db, 'R26002', 238.00, name='Müller GmbH', person='Mia Müller', customer_id=customer(tmp_db, 'Müller GmbH'))
    with_signal = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    without = add_payment(tmp_db, account, 238.00, '2026-03-11', 'Müller GmbH', 'Rechnungs-Nr R26002')

    page = period_page(tmp_db)

    assert page.count('this, true)') == 1 and f'payAssign({invoice_id}, {with_signal}, this, true)' in page
    assert f'payAssign({other}, {without}, this)' in page and f'payAssign({other}, {without}, this, true)' not in page


def test_nothing_is_assigned_by_looking_at_the_pages_with_a_discount_on_offer(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    before = state(tmp_db, *TABLES)

    period_page(tmp_db), PageInvoice(tmp_db, {}, invoice_id), PageTransactions(tmp_db, edit_transaction_id=payment), PageTransactions(tmp_db)

    assert state(tmp_db, *TABLES) == before


def test_the_script_of_the_box_stays_a_single_function_with_only_var_inside():
    declarations = re.findall(r'^[ \t]*(var|let|const|function)\s+(\w+)', PAY_SUGGEST_JS, re.M)
    assert re.findall(r'^(var|let|const|function)\s+(\w+)', PAY_SUGGEST_JS, re.M) == [('function', 'payAssign')]
    assert declarations[0] == ('function', 'payAssign') and {kind for kind, _name in declarations[1:]} <= {'var'}
    assert 'innerHTML' not in PAY_SUGGEST_JS and 'eval(' not in PAY_SUGGEST_JS and 'outerHTML' not in PAY_SUGGEST_JS


BOX_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const calls = {fetch: [], msg: [], navigations: []};
const buttons = [{disabled: false}, {disabled: false}];
const cell = {querySelectorAll: tag => tag === 'button' ? buttons : []};
buttons.forEach(b => { b.parentNode = cell; });
const scenario = input.scenario;
const location = {pathname: '/transactions', search: '?from=2026-01-01&to=2026-12-31', reload: () => {}};
Object.defineProperty(location, 'href', {set: value => { calls.navigations.push(value); }});
const context = {
  window: {location}, URLSearchParams,
  fetch: (url, init) => {
    calls.fetch.push({url, method: init.method, body: init.body, disabledWhileSending: buttons.map(b => b.disabled)});
    return Promise.resolve({json: () => Promise.resolve(scenario.data)});
  },
  appMsg: (text, type) => calls.msg.push([text, type]),
  JSON, Promise,
};
vm.createContext(context);
vm.runInContext(input.source, context);
if (scenario.noParent) buttons.forEach(b => { delete b.parentNode; });
context.payAssign(12, 34, buttons[scenario.which], ...(scenario.skonto === undefined ? [] : [scenario.skonto]));
setTimeout(() => { calls.disabledAtEnd = buttons.map(b => b.disabled); console.log(JSON.stringify(calls)); }, 30);
"""


def run_box(**scenario):
    source = f'var PAY_FAILED = {script_json("Die Zahlung konnte nicht zugeordnet werden.")};\n{PAY_SUGGEST_JS}'
    result = subprocess.run([NODE, '-e', BOX_HARNESS], input=json.dumps({'source': source, 'scenario': {'data': {'success': True}, 'which': 0, **scenario}}),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@NO_NODE
def test_the_discount_button_sends_the_flag_and_nothing_else_changes():
    calls = run_box(which=1, skonto=True)
    (fetched,) = calls['fetch']
    assert (fetched['url'], fetched['method']) == ('/invoice/link-payment', 'POST')
    assert json.loads(fetched['body']) == {'invoice_id': 12, 'transaction_id': 34, 'skonto': True}
    assert fetched['disabledWhileSending'] == [True, True]                                            # beide Knöpfe der Zeile, solange die Anfrage läuft
    assert calls['navigations'] == ['/transactions?from=2026-01-01&to=2026-12-31'] and calls['msg'] == []


@NO_NODE
@pytest.mark.parametrize('flag', [None, False, 'true', 1, 'yes'], ids=['fehlt', 'false', 'Text', 'Zahl', 'ja'])
def test_the_plain_button_and_every_other_value_send_no_flag(flag):
    """Nur der Wahrheitswert ``true`` schickt das Skonto – „Zuordnen“ nie."""
    scenario = {'which': 0} if flag is None else {'which': 0, 'skonto': flag}
    (fetched,) = run_box(**scenario)['fetch']
    assert json.loads(fetched['body']) == {'invoice_id': 12, 'transaction_id': 34}


@NO_NODE
def test_a_failure_frees_both_buttons_and_shows_the_reason():
    calls = run_box(which=1, skonto=True, data={'success': False, 'error': 'Der Rest nach dieser Zahlung ist zu groß.'})
    assert calls['msg'] == [['Der Rest nach dieser Zahlung ist zu groß.', 'error']] and calls['navigations'] == []
    assert calls['disabledAtEnd'] == [False, False]


@NO_NODE
def test_without_a_row_around_the_button_the_script_still_works():
    """Wie in den bestehenden Läufen von ``tests/test_payment_suggestions.py``: ein einzelner Knopf ohne ``parentNode`` – nur er wird gesperrt."""
    calls = run_box(which=0, noParent=True)
    assert calls['fetch'][0]['disabledWhileSending'] == [True, False] and calls['navigations'] == ['/transactions?from=2026-01-01&to=2026-12-31']


# ═════════════════════════════════════════════════════════════════════════════
# Die Skripte der Seiten (Node, DOM-Attrappe)
# ═════════════════════════════════════════════════════════════════════════════

def stripped(page):
    """Die Seite ohne Skript- und Stilblöcke – das DOM, aus dem die Attrappe gebaut wird."""
    return re.sub(r'<(script|style)\b.*?</\1>', '', page, flags=re.S | re.I)


def run_page(tmp_path, html, sources, boot, program):
    """Das HTML als DOM-Attrappe, dazu ``sources`` (die echten Skripte), ``boot`` (was die Seite beim Laden tut) und ``program`` (JavaScript mit ``emit``, ``fire``, ``type``, ``tick``, ``$``)."""
    harness, data = tmp_path / 'harness.js', tmp_path / 'scenario.json'
    harness.write_text(HARNESS, encoding='utf-8')
    data.write_text(json.dumps({'html': stripped(html), 'sources': sources, 'boot': boot, 'program': program, 'answer': None}), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(data)], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


INVOICE_PROGRAM = """
const state = id => ({hidden: $('paySkontoRow_' + id).hidden, ticked: $('paySkonto_' + id).checked, rest: $('paySkontoRest_' + id).textContent});
emit('initial', {one: state(ONE), two: state(TWO), three: state(THREE)});
const amount = (id, value) => { type($('payAmount_' + id), value); return state(id); };
emit('typed', {
  hundred: amount(THREE, '100.00'), exact: amount(THREE, '116.62'), cent_more: amount(THREE, '116.63'), full: amount(THREE, '119.00'), over: amount(THREE, '125.00'),
  edge: amount(THREE, '116.62'), zero: amount(THREE, '0'), empty: amount(THREE, ''), minus: amount(THREE, '-5'), seventeen: amount(THREE, '117.00'), big_rest: amount(THREE, '116.61'),
});
$('paySkonto_' + THREE).checked = true;
type($('payAmount_' + THREE), '100.00');
emit('after_ticked', state(THREE));
"""


def invoice_world(tmp_db):
    """Die Rechnung (119,00 €, Skonto 2,38 €, Frist weit voraus) mit drei Bewegungen: 116,62 € (genau nach Skonto), 20,00 € und 200,00 € frei."""
    account, _buyer, invoice_id = discount_world(tmp_db, date='2099-03-04')
    one = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    two = add_payment(tmp_db, account, 20.00, '2026-03-12', 'Käufer AG', 'Anderes')
    three = add_payment(tmp_db, account, 200.00, '2026-03-13', 'Käufer AG', 'Sammelzahlung')
    return invoice_id, one, two, three


@NO_NODE
def test_the_invoice_page_shows_the_tick_only_where_the_rest_fits_and_takes_the_tick_back(tmp_db, tmp_path):
    invoice_id, one, two, three = invoice_world(tmp_db)
    page = PageInvoice(tmp_db, {}, invoice_id)

    out = run_page(tmp_path, page, [SKONTO_JS + SKONTO_INVOICE_JS, f'var SKO_ROWS = [{one}, {two}, {three}]; var ONE = {one}, TWO = {two}, THREE = {three};'],
                   'skoInvoiceInit();', INVOICE_PROGRAM)

    hidden, shown = {'hidden': True, 'ticked': False, 'rest': ''}, lambda rest: {'hidden': False, 'ticked': False, 'rest': rest}
    # beim Laden: 119,00 − 116,62 = 2,38 ≤ 2,38 (sichtbar, nie vorgewählt) · 119,00 − 20,00 = 99,00: zu groß · 200,00 frei, vorbelegt mit 119,00: kein Rest
    assert out['initial'] == {'one': shown('2,38 €'), 'two': hidden, 'three': hidden}
    typed = out['typed']
    assert typed['hundred'] == hidden                                                                  # Rest 19,00 > 2,38
    assert typed['exact'] == typed['edge'] == shown('2,38 €')                                          # die Grenze selbst gilt
    assert typed['cent_more'] == shown('2,37 €')                                                       # ein Cent mehr gezahlt: Rest 2,37
    assert typed['seventeen'] == shown('2,00 €') and typed['big_rest'] == hidden                       # 116,61 € lassen 2,39 € Rest: über dem Skonto
    assert typed['full'] == typed['over'] == hidden                                                    # kein Rest (auch nicht bei einer Überzahlung)
    assert typed['zero'] == typed['empty'] == typed['minus'] == hidden
    assert out['after_ticked'] == hidden                                                               # mit der Sichtbarkeit fällt der Haken


@NO_NODE
def test_the_amount_is_capped_by_the_free_rest_of_the_movement(tmp_db, tmp_path):
    """Wie beim Zuordnen: Der Betrag der Zahlung ist höchstens der freie Rest der Bewegung – 130,00 € auf einer Bewegung mit 116,62 € frei sind 116,62 €, der Rest 2,38 €."""
    invoice_id, one, two, three = invoice_world(tmp_db)
    program = """
type($('payAmount_' + ONE), '130.00');
emit('typed', {hidden: $('paySkontoRow_' + ONE).hidden, rest: $('paySkontoRest_' + ONE).textContent});
"""
    out = run_page(tmp_path, PageInvoice(tmp_db, {}, invoice_id), [SKONTO_JS + SKONTO_INVOICE_JS, f'var SKO_ROWS = [{one}, {two}, {three}]; var ONE = {one};'], 'skoInvoiceInit();', program)
    assert out['typed'] == {'hidden': False, 'rest': '2,38 €'}


@NO_NODE
def test_a_part_paid_invoice_counts_with_its_open_amount(tmp_db, tmp_path):
    account, _buyer, invoice_id = discount_world(tmp_db, date='2099-03-04')
    assert tmp_db.assign_invoice_payment(invoice_id, add_payment(tmp_db, account, 50.00, '2026-03-06', 'Käufer AG', 'Anzahlung')).ok
    rest = add_payment(tmp_db, account, 66.62, '2026-03-10', 'Käufer AG', 'Rest')
    program = "emit('initial', {hidden: $('paySkontoRow_' + ID).hidden, rest: $('paySkontoRest_' + ID).textContent});"

    out = run_page(tmp_path, PageInvoice(tmp_db, {}, invoice_id), [SKONTO_JS + SKONTO_INVOICE_JS, f'var SKO_ROWS = [{rest}]; var ID = {rest};'], 'skoInvoiceInit();', program)

    assert out['initial'] == {'hidden': False, 'rest': '2,38 €'}                                       # offen 69,00 − 66,62


def link_payment_payload(tmp_path, page, booking_id, ticked):
    """Was ``linkPayment`` der Seite abschickt: die echte Funktion, aus dem Skript der Seite geschnitten."""
    (main,) = [script for script in Dom(page).scripts if 'function linkPayment(' in script]
    sources = [extract_function(main, 'revenueCoaChoice'), extract_function(main, 'linkPayment')]
    program = f"""
{'$("paySkonto_' + str(booking_id) + '").checked = true;' if ticked else ''}
linkPayment({booking_id}, {{disabled: false}});
"""
    out = run_page(tmp_path, page, sources, '', program)
    (sent,) = out['sent']
    return sent['body']


@NO_NODE
def test_link_payment_sends_the_flag_only_with_the_tick(tmp_db, tmp_path):
    invoice_id, one, _two, _three = invoice_world(tmp_db)
    page = PageInvoice(tmp_db, {}, invoice_id)

    assert link_payment_payload(tmp_path, page, one, ticked=False) == {'invoice_id': invoice_id, 'transaction_id': one, 'amount_paid': 116.62}
    assert link_payment_payload(tmp_path, page, one, ticked=True) == {'invoice_id': invoice_id, 'transaction_id': one, 'amount_paid': 116.62, 'skonto': True}


@NO_NODE
def test_link_payment_on_a_page_without_a_discount_sends_no_flag(tmp_db, tmp_path):
    account, _buyer, invoice_id = discount_world(tmp_db, skonto_days=None, skonto_percent=None)
    one = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    page = PageInvoice(tmp_db, {}, invoice_id)

    assert link_payment_payload(tmp_path, page, one, ticked=False) == {'invoice_id': invoice_id, 'transaction_id': one, 'amount_paid': 116.62}


MASK_PROGRAM = """
const state = () => ({hidden: $('skontoRow').hidden, ticked: $('skontoBox').checked, rest: $('skontoRest').textContent});
const choose = value => { const s = document.querySelector('select[name="invoice_id"]'); s.value = value; fire(s, 'change'); return state(); };
const amount = value => { type($('amount'), value); return state(); };
emit('initial', state());
emit('chosen', choose(String(INVOICE)));
emit('typed', {hundred: amount('100.00'), exact: amount('116.62'), seventeen: amount('117.00'), full: amount('119.00'), empty: amount(''), big: amount('116.61')});
amount('116.62');
$('skontoBox').checked = true;
emit('ticked', state());
emit('unchosen', choose(''));
emit('chosen_plain', choose(String(PLAIN)));
emit('chosen_again', choose(String(INVOICE)));
"""


@NO_NODE
def test_the_mask_shows_the_tick_for_a_chosen_invoice_with_a_discount_and_a_fitting_amount(tmp_db, tmp_path):
    account, buyer, invoice_id = discount_world(tmp_db)
    plain = add_invoice(tmp_db, 'R26002', 238.00, customer_id=buyer)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    page = PageTransactions(tmp_db, edit_transaction_id=payment)

    out = run_page(tmp_path, page, [SKONTO_JS, SKONTO_MASK_JS, f'var INVOICE = {invoice_id}, PLAIN = {plain};'], 'skoMaskInit();', MASK_PROGRAM)

    assert out['initial'] == {'hidden': True, 'ticked': False, 'rest': ''}                             # beim Laden: keine Rechnung gewählt, nichts vorgewählt
    assert out['chosen'] == {'hidden': False, 'ticked': False, 'rest': '2,38 €'}                       # 119,00 − 116,62; der Haken erscheint, aber nicht angehakt
    typed = out['typed']
    assert typed['hundred'] == {'hidden': True, 'ticked': False, 'rest': ''} and typed['full'] == typed['empty'] == typed['big'] == typed['hundred']
    assert typed['exact'] == {'hidden': False, 'ticked': False, 'rest': '2,38 €'} and typed['seventeen'] == {'hidden': False, 'ticked': False, 'rest': '2,00 €'}
    assert out['ticked'] == {'hidden': False, 'ticked': True, 'rest': '2,38 €'}
    assert out['unchosen'] == {'hidden': True, 'ticked': False, 'rest': ''}                            # keine Rechnung: weg, und der Haken fällt
    assert out['chosen_plain'] == {'hidden': True, 'ticked': False, 'rest': ''}                        # eine Rechnung ohne Skonto: nie
    assert out['chosen_again'] == {'hidden': False, 'ticked': False, 'rest': '2,38 €'}                 # und er kommt nie angehakt zurück


@NO_NODE
def test_the_mask_counts_with_the_free_rest_of_a_partly_used_booking(tmp_db, tmp_path):
    account, buyer, invoice_id = discount_world(tmp_db)
    other = add_invoice(tmp_db, 'R26002', 383.38, customer_id=buyer)
    payment = add_payment(tmp_db, account, 500.00, '2026-03-10', 'Käufer AG', 'Sammelzahlung')
    assert tmp_db.assign_invoice_payment(other, payment).ok
    program = """
const s = document.querySelector('select[name="invoice_id"]'); s.value = String(INVOICE); fire(s, 'change');
emit('chosen', {hidden: $('skontoRow').hidden, rest: $('skontoRest').textContent});
type($('amount'), '400.00');
emit('smaller', {hidden: $('skontoRow').hidden});
"""

    out = run_page(tmp_path, PageTransactions(tmp_db, edit_transaction_id=payment), [SKONTO_JS, SKONTO_MASK_JS, f'var INVOICE = {invoice_id};'], 'skoMaskInit();', program)

    assert out['chosen'] == {'hidden': False, 'rest': '2,38 €'}                                         # 500,00 − 383,38 = 116,62 frei: der Rest ist 2,38 €
    assert out['smaller'] == {'hidden': True}                                                           # 400,00 − 383,38 = 16,62 frei: der Rest wäre 102,38 €


@NO_NODE
def test_a_new_booking_prefilled_from_an_invoice_starts_hidden_and_shows_when_the_amount_is_lowered(tmp_db, tmp_path):
    """„Zahlung verbuchen“ auf der Rechnungsseite: Die Maske ist mit der Rechnung und dem offenen Betrag vorbelegt – der Rest ist 0, der Haken versteckt; mit 116,62 € erscheint er."""
    _account, _buyer, invoice_id = discount_world(tmp_db)
    page = PageTransactions(tmp_db, from_invoice=invoice_id)
    program = """
emit('initial', {hidden: $('skontoRow').hidden, ticked: $('skontoBox').checked, selected: document.querySelector('select[name="invoice_id"]').value});
type($('amount'), '116.62');
emit('lowered', {hidden: $('skontoRow').hidden, ticked: $('skontoBox').checked, rest: $('skontoRest').textContent});
"""

    out = run_page(tmp_path, page, [SKONTO_JS, SKONTO_MASK_JS], 'skoMaskInit();', program)

    assert out['initial'] == {'hidden': True, 'ticked': False, 'selected': str(invoice_id)}
    assert out['lowered'] == {'hidden': False, 'ticked': False, 'rest': '2,38 €'}


@NO_NODE
def test_taking_over_the_suggestion_shows_the_tick_but_does_not_tick_it(tmp_db, tmp_path):
    """„Übernehmen“ setzt den Wert der Auswahl, ohne dass der Browser ein Änderungsereignis auslöst – das Skript hört deshalb auf den Knopf, und der Haken erscheint (nie angehakt)."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    page = PageTransactions(tmp_db, edit_transaction_id=payment)
    program = """
const state = () => ({hidden: $('skontoRow').hidden, ticked: $('skontoBox').checked, rest: $('skontoRest').textContent});
const select = document.querySelector('select[name="invoice_id"]'), take = document.querySelector('#invoiceSuggestion button');
emit('initial', {value: select.value, state: state()});
// Der Browser ruft zuerst das Attribut onclick (es hängt am Knopf, seit die Seite gelesen wurde), dann die Zuhörer; die Attrappe macht es umgekehrt – also von Hand in der Reihenfolge des Browsers
take.form = {elements: {invoice_id: select}};
new Function(take.getAttribute('onclick')).call(take);
(take.listeners.click || []).forEach(fn => fn.call(take, {type: 'click', target: take}));
emit('taken', {value: select.value, state: state()});
"""

    out = run_page(tmp_path, page, [SKONTO_JS, SKONTO_MASK_JS], 'skoMaskInit();', program)

    assert out['initial'] == {'value': '', 'state': {'hidden': True, 'ticked': False, 'rest': ''}}
    assert out['taken'] == {'value': str(invoice_id), 'state': {'hidden': False, 'ticked': False, 'rest': '2,38 €'}}


@NO_NODE
def test_the_euro_format_of_the_script_is_the_german_one(tmp_path):
    program = """
emit('format', [skoEuro(238), skoEuro(5), skoEuro(100), skoEuro(123456), skoEuro(100000000), skoEuro(-5), skoEuro(0)]);
emit('cents', [skoCents('2.38'), skoCents('116.62'), skoCents('0.1'), skoCents('19'), skoCents(''), skoCents(null), skoCents('abc'), skoCents('1e2')]);
emit('rest', [skoRest(11900, 238, 11662), skoRest(11900, 238, 11900), skoRest(11900, 238, 12000), skoRest(11900, 238, 0), skoRest(11900, 238, -5), skoRest(11900, 0, 11900)]);
"""
    out = run_page(tmp_path, '<div></div>', [SKONTO_JS], '', program)
    assert out['format'] == ['2,38 €', '0,05 €', '1,00 €', '1.234,56 €', '1.000.000,00 €', '-0,05 €', '0,00 €']
    assert out['cents'] == [238, 11662, 10, 1900, 0, 0, 0, 10000]
    assert out['rest'] == [238, 0, 0, 0, 0, 0]


# ── statisch ─────────────────────────────────────────────────────────────────

SCRIPTS = {'SKONTO_JS': SKONTO_JS, 'SKONTO_INVOICE_JS': SKONTO_INVOICE_JS, 'SKONTO_MASK_JS': SKONTO_MASK_JS}


@pytest.mark.parametrize('name', sorted(SCRIPTS))
def test_the_scripts_declare_only_var_and_function_with_the_prefix_sko_and_set_no_markup(name):
    source = SCRIPTS[name]
    declared = re.findall(r'^[ \t]*(var|let|const|function)\s+(\w+)', source, re.M)
    assert declared and {kind for kind, _ in declared} <= {'var', 'function'}, declared
    assert all(identifier.startswith('sko') for kind, identifier in re.findall(r'^(function)\s+(\w+)', source, re.M)), declared
    for forbidden in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function'):
        assert forbidden not in source, forbidden
    assert re.findall(r'^(var|let|const)\s+', source, re.M) == []                                      # nichts Globales außerhalb der Funktionen


@NO_NODE
@pytest.mark.parametrize('name', sorted(SCRIPTS))
def test_the_scripts_parse_in_node(tmp_path, name):
    source = tmp_path / f'{name}.js'
    source.write_text(SKONTO_JS + SCRIPTS[name] if name != 'SKONTO_JS' else SKONTO_JS, encoding='utf-8')
    done = subprocess.run([NODE, '--check', str(source)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_the_tick_html_carries_no_value_of_the_database_and_hides_itself():
    html = skonto_check_html('paySkontoRow_7', 'paySkonto_7', 'paySkontoRest_7', data=' data-free="1.00"')
    assert html == ('<div id="paySkontoRow_7" data-free="1.00" hidden><label class="hint" title="Der Rest der Rechnung nach dieser Zahlung wird als Skonto ausgebucht – nichts '
                    'geschieht ohne dieses Häkchen."><input type="checkbox" id="paySkonto_7" autocomplete="off"> Rest <span id="paySkontoRest_7"></span> als Skonto ausbuchen</label></div>')
    assert 'name=' not in html and 'checked' not in html
    assert 'name="skonto" value="1"' in skonto_check_html('a', 'b', 'c', name='skonto')


def test_the_pages_hold_no_placeholder_for_the_new_script_blocks():
    """Platzhalter nur in konstanten Skriptblöcken – die neuen Blöcke haben keine, und über die fertige Seite wird nichts ersetzt."""
    for source in SCRIPTS.values():
        assert not re.search(r'__[A-Z][A-Z0-9_]*__', source)
    for module in (pages_invoice, pages_transactions):
        text = pathlib.Path(module.__file__).read_text(encoding='utf-8')
        assert not re.search(r'^\s*s\s*=\s*(s\.replace|re\.sub)\(', text, re.M), module.__name__


# ═════════════════════════════════════════════════════════════════════════════
# Stornieren: die Antwort nennt die Rücknahme des Skontos
# ═════════════════════════════════════════════════════════════════════════════

def cancel_body(day='2026-07-05', invoice_id=None):
    code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': invoice_id, 'date': day}).encode())
    return code, json.loads(text)


def test_the_cancel_answer_names_the_discount_that_was_taken_back(world, monkeypatch):
    db = world.db
    invoice_id = world_sent(world, 'T-100')                                                            # 119,00 €, 19 %, Skonto 2 % in 7 Tagen
    booking = db.insert_booking('2026-07-01', 116.62, booking_type='bank', account_id=world.bank, recipient_client='Malermeister Pinsel')
    assert handlers.link_booking_to_invoice_capped(db, invoice_id, booking, 116.62, None, True) == (True, None)
    at(monkeypatch, '2026-07-10')

    code, body = cancel_body(invoice_id=invoice_id)

    assert code == 200 and body['success'] is True and body['hint'] == SKONTO_TAKEN_BACK_HINT
    assert SKONTO_TAKEN_BACK_HINT == 'Das Skonto der Zahlung wurde mit dem Storno zurückgenommen – das Guthaben des Kunden ist das gezahlte Geld.'
    assert raw(db, 'SELECT SkontoAmount FROM InvoicePayments WHERE InvoiceID = ?', invoice_id) == [(None,)]


def test_a_payment_without_a_discount_gets_no_hint(world, monkeypatch):
    db = world.db
    invoice_id = world_sent(world, 'T-100')
    booking = db.insert_booking('2026-07-01', 116.62, booking_type='bank', account_id=world.bank, recipient_client='Malermeister Pinsel')
    assert handlers.link_booking_to_invoice_capped(db, invoice_id, booking, 116.62) == (True, None)            # eine Teilzahlung
    at(monkeypatch, '2026-07-10')

    code, body = cancel_body(invoice_id=invoice_id)

    assert code == 200 and 'hint' not in body


def test_an_invoice_without_any_payment_gets_no_hint(world, monkeypatch):
    invoice_id = world_sent(world, 'T-100')
    at(monkeypatch, '2026-07-10')

    code, body = cancel_body(invoice_id=invoice_id)

    assert code == 200 and 'hint' not in body


def test_both_sentences_stand_in_one_hint_when_there_was_no_receivable_either(world, monkeypatch):
    from db.invoice_storno import NO_COUNTER_BOOKING_HINT
    db = world.db
    invoice_id = db.insert_invoice({
        'invoice_number': 'T-200', 'invoice_date': '2026-06-01', 'payment_due_date': '2099-12-31', 'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19, 'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0, 'amount_due': 119.0,
        'status': 'sent', 'skonto_days': 14, 'skonto_percent': 2.0})                                      # versendet, aber ohne Forderung der App
    booking = db.insert_booking('2026-07-01', 116.62, booking_type='bank', account_id=world.bank, recipient_client='Käufer AG')
    assert handlers.link_booking_to_invoice_capped(db, invoice_id, booking, 116.62, None, True) == (True, None)
    at(monkeypatch, '2026-07-10')

    code, body = cancel_body(invoice_id=invoice_id)

    assert code == 200 and body['hint'] == f'{NO_COUNTER_BOOKING_HINT} {SKONTO_TAKEN_BACK_HINT}'


def test_a_refused_cancellation_names_the_reason_and_no_hint(world, monkeypatch):
    """Ist die Skonto-Buchung geändert, lehnt das Stornieren ab – 409 mit dem Grund, ohne den Hinweis auf eine Rücknahme."""
    db = world.db
    invoice_id = world_sent(world, 'T-100')
    booking = db.insert_booking('2026-07-01', 116.62, booking_type='bank', account_id=world.bank, recipient_client='Malermeister Pinsel')
    assert handlers.link_booking_to_invoice_capped(db, invoice_id, booking, 116.62, None, True) == (True, None)
    raw(db, 'UPDATE Bookings SET Amount = -20000 WHERE Text LIKE ?', 'Skonto%')
    at(monkeypatch, '2026-07-10')

    code, body = cancel_body(invoice_id=invoice_id)

    assert code == 409 and body['success'] is False and 'hint' not in body
