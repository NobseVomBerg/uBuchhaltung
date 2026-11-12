# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Sperre versendeter Rechnungen und Statusübergänge (2.1.2, ``db/invoice_lock.py``).

* Bei einer versendeten Rechnung (versendet, überfällig, Teilzahlung, bezahlt) lehnt das Speichern jede
  Änderung an Nummer, Rechnungsdatum, Kunde, eigener Firma, Positionen, Steuer, Fälligkeit, Skonto,
  Bank und Währung ab (409, ein Klartext, nichts wird geschrieben – auch nicht die änderbaren Felder
  derselben Anfrage). Unverändert Zurückgeschicktes ist kein Verstoß, auch nicht in anderer Darstellung
  (Beträge in Minor Units, Einheiten wie das Formular sie zeigt). Zahlungshinweis, Anschrift, Bestellnummer,
  Leitweg-ID und Leistungsdatum bleiben änderbar – als Berichtigung mit neuer Fassung und Protokoll
  (test_invoice_correction.py); eine stornierte Rechnung ist ganz gesperrt.
* Status und ``AmountDue`` kommen nie vom Client.
* Die Übergangstabelle (``TRANSITIONS``) ist vollständig geprüft – jedes Paar, mit und ohne Zahlung –
  und die Folgen eines Übergangs: Versenden bucht die Forderung und erzeugt das Dokument, versendet ↔
  überfällig bucht und erzeugt nichts (auch bei einer Rechnung ohne Forderung der App, wie ein
  Import sie hinterlässt).
* Zwei gleichzeitige Anfragen laufen nacheinander: Prüfen und Schreiben sehen denselben Stand.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import contextlib
import copy
import json
import os
import re
import sqlite3
import threading
from html import unescape
from itertools import product

import pytest

import server.handlers as handlers
import userctx
from db.invoice_lock import (CHANGED_MESSAGE, CLOSED_MESSAGE, InvoiceLocked, LOCKED_MESSAGE,
                             LOCKED_PAID_HINT, LOCKED_UNPAID_HINT, NUMBER_KEPT_MESSAGE, STATUS_LABELS,
                             allowed_targets, canonical_status, check_changes, status_group)
from export.invoice_files import generate_invoice_documents
from server.pages_invoice import PageInvoice
from tests.document_helpers import post_json, row, stored_documents
from tests.dom_helpers import Dom
from tests.invoice_world import (GROSS, add_payment, booking_count, build_world, columns, finance,
                                 force_status, make_invoice, make_payload, save, save_raw,
                                 set_invoice_status, snapshot)

LOCKED = ['sent', 'overdue', 'partial_payment', 'partial', 'paid']


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return build_world(tmp_db)


ALL_STATUSES = ['draft', 'finalized', 'sent', 'overdue', 'partial_payment', 'paid', 'cancelled']


# ── Unverändert zurückgeschickt ist kein Verstoß ─────────────────────────────

def _display_units(p):                    # das Formular zeigt „C62“ als „Stk.“ und „HUR“ als „Std.“
    p['items'][0]['unit'], p['items'][1]['unit'] = 'Std.', 'Stk.'


def _plain_numbers(p):                    # Zahlen in anderer Darstellung, derselbe Wert
    p['items'][0].update(quantity=2.0, unitPrice=100)
    p['items'][1].update(quantity=1.0, unitPrice='50.50')


def _percent_rate(p):                     # 19 statt 0,19
    p['taxRate'] = 19


def _blanks(p):                           # Leerraum, Zeilenumbruch (ein Textfeld kennt keinen)
    p['items'][0]['description'] = ' Beratung \n'
    p['invoiceNumber'] = '26R100 '
    p.pop('currency', None)


def _wrong_sums(p):                       # Summen des Clients zählen nie
    p.update(netAmount=1, taxAmount=1, grossAmount=1)


RESUBMISSIONS = {
    'unveraendert': lambda p: None,
    'Einheiten-wie-angezeigt': _display_units,
    'Zahlen-anders-dargestellt': _plain_numbers,
    'Steuersatz-in-Prozent': _percent_rate,
    'Leerraum': _blanks,
    'falsche-Summen-des-Clients': _wrong_sums,
}


@pytest.mark.parametrize('status', LOCKED)
@pytest.mark.parametrize('change', list(RESUBMISSIONS.values()), ids=list(RESUBMISSIONS))
def test_resubmitting_the_same_values_in_any_display_is_saved_and_changes_nothing(world, status, change):
    invoice_id = make_invoice(world, status)
    before = snapshot(world.db)
    payload = make_payload(world, invoiceId=invoice_id)
    change(payload)
    resp = save_raw(payload)
    assert json.loads(resp) == {'success': True, 'invoice_id': invoice_id} and resp.status == 200
    assert snapshot(world.db) == before                 # Spalte für Spalte, Positionen, Zähler


# ── Jede gesperrte Angabe, jeder gesperrte Status ────────────────────────────

def _item(i, **kw):
    return lambda w, p: p['items'][i].update(**kw)


def _remove_second_item(w, p):
    del p['items'][1]


def _add_item(w, p):
    p['items'].append({'position': 3, 'quantity': 1, 'unit': 'C62', 'description': 'Zusatz',
                       'unitPrice': 10.0, 'taxRate': 19})


def _swap_items(w, p):
    p['items'].reverse()
    for position, item in enumerate(p['items'], 1):
        item['position'] = position


def _tax_rate(w, p):
    p['taxRate'] = 0.07
    for item in p['items']:
        item['taxRate'] = 7


LOCKED_CHANGES = {
    'Rechnungsnummer': lambda w, p: p.update(invoiceNumber='26R999'),
    'Rechnungsdatum': lambda w, p: p.update(invoiceDate='2026-06-08'),
    'Kunde': lambda w, p: p.update(customerId=w.cust2),
    'eigene-Firma': lambda w, p: p.update(ownCompanyId=w.own2),
    'Bank-gewechselt': lambda w, p: p.update(bankAccountId=w.bank2),
    'Bank-entfernt': lambda w, p: p.update(bankAccountId=None),
    'Faelligkeit': lambda w, p: p.update(dueDate='2026-07-01'),
    'Faelligkeit-entfernt': lambda w, p: p.update(dueDate=None),
    'Skonto-Prozent': lambda w, p: p.update(discountPercentage=3),
    'Skonto-Tage': lambda w, p: p.update(discountDays=14),
    'Skonto-entfernt': lambda w, p: p.update(discountPercentage=None, discountDays=None),
    'Steuersatz': _tax_rate,
    'Kleinunternehmer': lambda w, p: p.update(showTax=False),
    'Waehrung': lambda w, p: p.update(currency='USD'),
    'Preis': _item(0, unitPrice=101.0),
    'Menge': _item(0, quantity=3),
    'Bezeichnung': _item(1, description='Material (neu)'),
    'Einheit': _item(0, unit='Tag'),
    'Position-entfernt': _remove_second_item,
    'Position-dazu': _add_item,
    'Reihenfolge': _swap_items,
}


@pytest.mark.parametrize('status', LOCKED + ['cancelled'])
@pytest.mark.parametrize('change', list(LOCKED_CHANGES.values()), ids=list(LOCKED_CHANGES))
def test_a_locked_field_is_refused_with_409_and_nothing_of_the_request_is_written(world, status, change):
    invoice_id = make_invoice(world, status)
    before = snapshot(world.db)
    payload = make_payload(world, invoiceId=invoice_id)
    change(world, payload)
    # die änderbaren Felder derselben Anfrage werden mit abgelehnt: alles oder nichts
    payload.update(paymentTerms='Zahlbar sofort', orderNumber='B-99', deliveryDate='2026-06-01')
    resp = save_raw(payload)
    answer = json.loads(resp)
    assert resp.status == 409 and answer['success'] is False
    assert answer['error'].startswith(CLOSED_MESSAGE if status == 'cancelled' else LOCKED_MESSAGE)
    assert snapshot(world.db) == before


@pytest.mark.parametrize('payments, hint', [(0, LOCKED_UNPAID_HINT), (1, LOCKED_PAID_HINT)])
def test_the_refusal_names_the_way_back_only_when_there_is_one(world, payments, hint):
    invoice_id = make_invoice(world, 'sent')
    if payments:
        add_payment(world, invoice_id)
    answer = save(make_payload(world, invoiceId=invoice_id, invoiceNumber='26R999'))
    assert answer['error'] == f'{LOCKED_MESSAGE} {hint}'
    assert answer['error'].startswith('Versendete Rechnung: Beträge, Positionen, Nummer, Datum und '
                                      'Kunde lassen sich nicht mehr ändern. ')


def test_the_refusal_is_a_plain_text_without_internals(world):
    invoice_id = make_invoice(world, 'paid')
    text = save(make_payload(world, invoiceId=invoice_id, invoiceNumber='26R999'))['error']
    for leak in ('sqlite', 'Invoices', 'InvoiceLocked', 'Traceback', 'UPDATE'):
        assert leak not in text


# ── Was bei einer versendeten Rechnung änderbar bleibt ───────────────────────

OPEN_CHANGES = {
    'Zahlungshinweis': ({'paymentTerms': 'Zahlbar sofort, ohne Abzug'}, {'PaymentTerms': 'Zahlbar sofort, ohne Abzug'}),
    'Bestellnummer': ({'orderNumber': 'B-99'}, {'OrderNumber': 'B-99'}),
    'Leitweg-ID': ({'buyerReference': '991-99999-99'},
                   {'BuyerReference': '991-99999-99', 'BuyerRouteID': '991-99999-99'}),
    'Leistungsdatum': ({'deliveryDate': '2026-06-01'}, {'DeliveryDate': '2026-06-01'}),
}


@pytest.mark.parametrize('shape', ['komplettes-Formular', 'nur-die-aenderbaren-Felder'])
@pytest.mark.parametrize('status', LOCKED)
@pytest.mark.parametrize('group', OPEN_CHANGES)
def test_the_open_fields_of_a_sent_invoice_are_saved_and_the_rest_stays(world, group, status, shape):
    invoice_id = make_invoice(world, status)
    locked_before = finance(world.db, invoice_id)
    form, expected = OPEN_CHANGES[group]
    if shape == 'komplettes-Formular':
        payload = make_payload(world, invoiceId=invoice_id, documentVersion=1, **form)
    else:
        payload = {'invoiceId': invoice_id, 'documentVersion': 1, **form}
    resp = save_raw(payload)
    # Das Speichern einer versendeten Rechnung ist eine Berichtigung: Fassung 2 (Einzelheiten:
    # test_invoice_correction.py)
    assert json.loads(resp) == {'success': True, 'invoice_id': invoice_id, 'documentVersion': 2}
    assert resp.status == 200
    stored = columns(world.db, invoice_id)
    for column, value in expected.items():
        assert stored[column] == value
    assert finance(world.db, invoice_id) == locked_before
    if shape == 'nur-die-aenderbaren-Felder':               # was nicht mitkommt, bleibt, wie es war
        untouched = {'PaymentTerms': 'Zahlbar in 14 Tagen', 'OrderNumber': 'B-17',
                     'DeliveryDate': '2026-06-05'}
        for column, value in untouched.items():
            if column not in expected:
                assert stored[column] == value


def imported_invoice(w):
    """Eine Rechnung, wie ein Import sie hinterlässt und das Formular sie nicht darstellen kann: zwei
    Steuersätze, Einheiten als UN/ECE-Code, keine Fälligkeit, Summen aus der Quelle (um einen Cent anders
    gerundet, als die App rechnen würde)."""
    db = w.db
    invoice_id = db.insert_invoice({
        'invoice_number': 'IMP-1', 'invoice_date': '2026-02-03', 'delivery_date': '2026-02-01',
        'own_company_id': w.own, 'customer_id': w.cust, 'seller_name': 'Eigene Firma GmbH',
        'seller_company': 'Eigene Firma GmbH', 'buyer_name': 'Malermeister Pinsel',
        'buyer_company': 'Malermeister Pinsel', 'currency': 'EUR', 'tax_rate': 0.19,
        'sum_net': 200.0, 'tax_amount': 26.01, 'sum_gross': 226.01, 'amount_due': 226.01,
        'status': 'sent', 'payment_terms': 'Zahlbar sofort'})
    for position, (rate, unit) in enumerate(((19, 'HUR'), (7, 'C62')), 1):
        db.insert_invoice_item({'invoice_id': invoice_id, 'position': position, 'description': f'Posten {position}',
                                'quantity': 1, 'unit': unit, 'price_per_unit': 100.0, 'total_net': 100.0,
                                'tax_rate': rate})
    return invoice_id


def test_an_imported_invoice_takes_its_open_fields_although_the_form_could_not_reproduce_it(world):
    """Das Formular kennt einen Steuersatz je Rechnung und rechnet die Fälligkeit selbst vor: Es schickt für eine
    versendete Rechnung deshalb nur die änderbaren Felder, und die gehen durch."""
    invoice_id = imported_invoice(world)
    before = finance(world.db, invoice_id)
    assert save({'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99',
                 'paymentTerms': 'Zahlbar in 10 Tagen', 'deliveryDate': '2026-02-02',
                 'buyerReference': None}) == {'success': True, 'invoice_id': invoice_id, 'documentVersion': 2}
    stored = columns(world.db, invoice_id)
    assert (stored['OrderNumber'], stored['PaymentTerms'], stored['DeliveryDate']) == (
        'B-99', 'Zahlbar in 10 Tagen', '2026-02-02')
    assert finance(world.db, invoice_id) == before                      # Summen aus der Quelle, Sätze, Fälligkeit …


def test_the_complete_form_cannot_change_the_imported_figures_either(world):
    """Was das Formular aus dieser Rechnung machen würde (ein Satz, selbst gerechnete Summe, Fälligkeit),
    wäre eine Änderung der Finanzangaben und wird abgelehnt – auch das ist die Sperre."""
    invoice_id = imported_invoice(world)
    before = snapshot(world.db)
    payload = make_payload(world, invoiceId=invoice_id, invoiceNumber='IMP-1', invoiceDate='2026-02-03',
                           bankAccountId=None)
    resp = save_raw(payload)
    assert resp.status == 409 and json.loads(resp)['error'].startswith(LOCKED_MESSAGE)
    assert snapshot(world.db) == before


def _rename_customer(world):
    world.db.update_contact(world.cust, contact_type='customer', entity_type='company',
                            display_name='Malermeister Pinsel', company_name='Malermeister Pinsel GmbH',
                            customer_number='K-100', street='Pinselallee 7', postal_code='12345',
                            city='Farbstadt', email='info@example.com')


ADDRESS_COLUMNS = ('BuyerName', 'BuyerCompany', 'BuyerStreet', 'BuyerPostalCode', 'BuyerCity', 'BuyerCountry')


@pytest.mark.parametrize('shape', ['komplettes-Formular', 'nur-die-aenderbaren-Felder'])
def test_saving_a_sent_invoice_leaves_its_address_as_it_was(world, shape):
    """Die Anschrift ist eine Momentaufnahme vom Erstellen: Ein Speichern holt sie nicht still aus den
    Stammdaten (2.1.2) – auch dann nicht, wenn gleichzeitig ein anderes änderbares Feld geschrieben wird."""
    invoice_id = make_invoice(world, 'sent')
    locked_before = finance(world.db, invoice_id)
    address_before = {k: columns(world.db, invoice_id)[k] for k in ADDRESS_COLUMNS}
    _rename_customer(world)
    form = {'paymentTerms': 'Zahlbar sofort', 'documentVersion': 1}
    payload = (make_payload(world, invoiceId=invoice_id, **form) if shape == 'komplettes-Formular'
               else {'invoiceId': invoice_id, **form})
    assert save(payload)['success'] is True
    stored = columns(world.db, invoice_id)
    assert stored['PaymentTerms'] == 'Zahlbar sofort'                    # das Geschriebene kam an …
    assert {k: stored[k] for k in ADDRESS_COLUMNS} == address_before      # … die Anschrift blieb
    assert finance(world.db, invoice_id) == locked_before


@pytest.mark.parametrize('shape', ['komplettes-Formular', 'nur-die-aenderbaren-Felder'])
def test_the_address_follows_the_master_data_of_the_same_customer_on_explicit_request(world, shape):
    invoice_id = make_invoice(world, 'sent')
    locked_before = finance(world.db, invoice_id)
    _rename_customer(world)
    payload = (make_payload(world, invoiceId=invoice_id, refreshAddress=True, documentVersion=1)
               if shape == 'komplettes-Formular'
               else {'invoiceId': invoice_id, 'refreshAddress': True, 'documentVersion': 1})
    assert save(payload)['success'] is True
    stored = columns(world.db, invoice_id)
    assert (stored['BuyerCompany'], stored['BuyerStreet'], stored['BuyerPostalCode'],
            stored['BuyerCity']) == ('Malermeister Pinsel GmbH', 'Pinselallee 7', '12345', 'Farbstadt')
    assert finance(world.db, invoice_id) == locked_before
    assert stored['CustomerId'] == world.cust                            # derselbe Kunde, nie ein anderer


def test_the_address_request_of_a_cancelled_invoice_is_refused(world):
    invoice_id = make_invoice(world, 'cancelled')
    _rename_customer(world)
    before = snapshot(world.db)
    resp = save_raw({'invoiceId': invoice_id, 'refreshAddress': True})
    assert resp.status == 409 and json.loads(resp)['error'] == CLOSED_MESSAGE
    assert snapshot(world.db) == before


def test_changed_master_data_of_the_company_and_the_bank_do_not_alter_a_sent_invoice(world):
    """Absender, Bankverbindung und Käufer-USt-IdNr. sind aus gesperrten Angaben abgeleitet: Ein
    Speichern holt sie nicht neu aus den Stammdaten."""
    invoice_id = make_invoice(world, 'sent')
    seller_before = {k: v for k, v in columns(world.db, invoice_id).items()
                     if k.startswith(('Seller', 'Bank')) or k == 'BuyerVATID'}
    world.db.update_contact(world.own, contact_type='own', entity_type='company',
                            display_name='Eigene Firma GmbH', company_name='Umbenannt GmbH',
                            street='Neue Str. 3', postal_code='99999', city='Anderswo', phone='0',
                            email='neu@example.com', tax_id='DE987654321')
    assert save(make_payload(world, invoiceId=invoice_id))['success'] is True
    assert {k: v for k, v in columns(world.db, invoice_id).items()
            if k.startswith(('Seller', 'Bank')) or k == 'BuyerVATID'} == seller_before


@pytest.mark.parametrize('shape', ['komplettes-Formular', 'nur-die-aenderbaren-Felder'])
def test_a_cancelled_invoice_takes_no_change_at_all_but_an_unchanged_save(world, shape):
    invoice_id = make_invoice(world, 'cancelled')
    before = snapshot(world.db)

    def body(**form):
        return ({'invoiceId': invoice_id, **form} if shape == 'nur-die-aenderbaren-Felder'
                else make_payload(world, invoiceId=invoice_id, **form))

    for form in ({'orderNumber': 'B-99'}, {'paymentTerms': 'neu'}, {'deliveryDate': '2026-06-01'},
                 {'buyerReference': '991-99999-99'}):
        resp = save_raw(body(**form))
        assert resp.status == 409 and json.loads(resp)['error'] == CLOSED_MESSAGE
    assert snapshot(world.db) == before
    assert save(body()) == {'success': True, 'invoice_id': invoice_id}
    assert snapshot(world.db) == before


def test_a_reduced_save_is_not_a_way_to_save_a_draft(world):
    """Für einen Entwurf gilt das komplette Formular, mit den bisherigen Meldungen."""
    invoice_id = make_invoice(world, 'draft')
    before = snapshot(world.db)
    assert save({'invoiceId': invoice_id, 'orderNumber': 'B-99'}) == {'success': False,
                                                                       'error': 'Pflichtfelder fehlen'}
    without_items = make_payload(world, invoiceId=invoice_id, orderNumber='B-99')
    del without_items['items']
    assert save(without_items) == {'success': False, 'error': 'Mindestens eine Position erforderlich'}
    assert snapshot(world.db) == before


def test_a_reduced_save_of_an_unknown_invoice_is_refused(world):
    before = snapshot(world.db)
    assert save({'invoiceId': 99999, 'orderNumber': 'B-99'}) == {'success': False,
                                                                  'error': 'Pflichtfelder fehlen'}
    assert snapshot(world.db) == before


# ── Entwurf und abgeschlossen bleiben ganz bearbeitbar ───────────────────────

@pytest.mark.parametrize('status', ['draft', 'finalized'])
@pytest.mark.parametrize('change', list(LOCKED_CHANGES.values()), ids=list(LOCKED_CHANGES))
def test_a_draft_takes_every_change(world, status, change):
    invoice_id = make_invoice(world, status)
    payload = make_payload(world, invoiceId=invoice_id)
    change(world, payload)
    resp = save_raw(payload)
    assert json.loads(resp) == {'success': True, 'invoice_id': invoice_id} and resp.status == 200
    assert columns(world.db, invoice_id)['Status'] == status            # der Status bleibt


# ── Status und AmountDue kommen nie vom Client ───────────────────────────────

@pytest.mark.parametrize('sent, stored', [('draft', 'draft'), ('finalized', 'finalized'), ('sent', 'draft'),
                                          ('overdue', 'draft'), ('partial_payment', 'draft'),
                                          ('paid', 'draft'), ('cancelled', 'draft'), (None, 'draft'),
                                          (5, 'draft'), ('gibt-es-nicht', 'draft')])
def test_a_new_invoice_is_a_draft_or_finalized_and_owes_its_gross(world, sent, stored):
    payload = make_payload(world, amountDue=1, remainingAmount=1, pdfPath='/etc/passwd')
    payload['status'] = sent
    invoice_id = save(payload)['invoice_id']
    saved = columns(world.db, invoice_id)
    assert saved['Status'] == stored
    assert saved['AmountDue'] == saved['SumGross'] == GROSS            # 250,50 netto + 47,60 USt
    assert saved['PDFPath'] is None and saved['XMLPath'] is None


@pytest.mark.parametrize('status', ['draft', 'finalized'])
def test_saving_a_draft_ignores_a_status_from_the_client_and_recomputes_the_amount_due(world, status):
    invoice_id = make_invoice(world, status)
    payload = make_payload(world, invoiceId=invoice_id, amountDue=1, grossAmount=1)
    payload['status'] = 'paid'
    payload['items'][0]['quantity'] = 3
    assert save(payload)['success'] is True
    saved = columns(world.db, invoice_id)
    assert saved['Status'] == status
    assert saved['AmountDue'] == saved['SumGross'] == 4171000          # 350,50 netto + 66,60 USt


@pytest.mark.parametrize('claimed', ['draft', 'finalized', 'paid', 'cancelled', 'sent'])
def test_saving_a_sent_invoice_never_moves_status_or_amount_due(world, claimed):
    invoice_id = make_invoice(world, 'sent')
    add_payment(world, invoice_id, 100.0)
    world.db.recalc_invoice_payment_state(invoice_id)
    before = columns(world.db, invoice_id)
    assert before['Status'] == 'partial_payment' and before['AmountDue'] == GROSS - 1000000
    payload = make_payload(world, invoiceId=invoice_id, amountDue=0, grossAmount=1, netAmount=1)
    payload['status'] = claimed
    assert save(payload)['success'] is True
    after = columns(world.db, invoice_id)
    assert (after['Status'], after['AmountDue'], after['SumGross']) == (
        'partial_payment', GROSS - 1000000, before['SumGross'])


# ── Jeder Schreibweg geht über die Sperre ────────────────────────────────────

def test_the_database_layer_refuses_a_locked_change_too(world):
    """``update_invoice_fields`` ist die Stelle: auch wer die Datenbank ohne den Handler anspricht,
    ändert eine versendete Rechnung nicht."""
    db = world.db
    invoice_id = make_invoice(world, 'sent')
    before = snapshot(db)
    with pytest.raises(InvoiceLocked) as refused:
        with db.transaction() as tx:
            db.update_invoice_fields(tx, invoice_id, {'invoice_number': '26R999', 'order_number': 'B-99'})
    assert refused.value.fields == ['Rechnungsnummer'] and str(refused.value).startswith(LOCKED_MESSAGE)
    assert snapshot(db) == before
    # Das Änderbare ist eine Berichtigung: hier geht es nicht (keine neue Fassung, kein Protokoll) …
    with pytest.raises(InvoiceLocked) as changed:
        with db.transaction() as tx:
            db.update_invoice_fields(tx, invoice_id, {'order_number': 'B-99'})
    assert str(changed.value) == CHANGED_MESSAGE and snapshot(db) == before
    # … sondern nur über correct_invoice, das Fassung und Protokoll anlegt
    assert db.correct_invoice(invoice_id, {'order_number': 'B-99'}, 1, 'anna').corrected is True
    assert columns(db, invoice_id)['OrderNumber'] == 'B-99'


def test_the_database_layer_rejects_unknown_fields_and_other_documents(world):
    db = world.db
    invoice_id = make_invoice(world, 'draft')
    with pytest.raises(ValueError):
        with db.transaction() as tx:
            db.update_invoice_fields(tx, invoice_id, {'status': 'paid'})
    with pytest.raises(ValueError):
        with db.transaction() as tx:
            db.update_invoice_fields(tx, invoice_id, {'pdf_path': '/tmp/x.pdf'})
    quote_id = db.insert_invoice({
        'invoice_number': '26A001', 'invoice_date': '2026-06-09', 'seller_name': 'S',
        'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
        'sum_net': 1, 'tax_amount': 0.19, 'sum_gross': 1.19, 'amount_due': 1.19, 'status': 'draft',
        'document_type': 'quote'})
    with pytest.raises(handlers.DocumentNotFound):
        with db.transaction() as tx:
            db.update_invoice_fields(tx, quote_id, {'order_number': 'B-99'})


def test_check_changes_looks_only_at_what_it_is_given():
    row_ = {'Status': 'sent', 'InvoiceNumber': '26R100', 'CustomerId': 4, 'TaxRate': 0.19,
            'PaymentDueDate': '2026-06-21', 'SkontoDays': None, 'Currency': None}
    assert check_changes(row_, {}) == []
    assert check_changes(row_, {'order_number': 'B-99', 'payment_terms': 'neu'}) == []
    assert check_changes(row_, {'customer_id': 4, 'tax_rate': 19, 'skonto_days': 0, 'currency': 'EUR',
                                'payment_due_date': '2026-06-21'}) == []
    assert check_changes(row_, {'customer_id': 5, 'tax_rate': 0.07}) == ['Kunde', 'Steuersatz']
    assert check_changes({**row_, 'Status': 'draft'}, {'customer_id': 5}) == []
    assert check_changes({**row_, 'Status': 'cancelled'}, {'order_number': 'B-99'}) == ['Bestellnummer']


def test_copying_a_sent_invoice_opens_a_new_draft_and_leaves_the_source_alone(world):
    invoice_id = make_invoice(world, 'sent')
    before = snapshot(world.db)
    dom = Dom(PageInvoice(world.db, {}, None, copy_from=invoice_id))
    assert dom.one('input', id='invoice_id')['value'] == '' and dom.one('input', id='is_edit_mode')['value'] == 'false'
    assert dom.one('input', id='invoice_status_value')['value'] == 'draft'
    assert snapshot(world.db) == before                                  # Anzeigen ändert nichts
    copy = save(make_payload(world, invoiceNumber='26R101', status='sent'))
    assert columns(world.db, copy['invoice_id'])['Status'] == 'draft'
    assert columns(world.db, invoice_id)['Status'] == 'sent'


def test_converting_a_quote_creates_a_draft_that_is_editable(world):
    db = world.db
    quote_id = db.insert_invoice({
        'invoice_number': '26A001', 'invoice_date': '2026-06-09', 'seller_name': 'S',
        'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
        'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0, 'amount_due': 119.0,
        'status': 'accepted', 'document_type': 'quote', 'customer_id': world.cust,
        'own_company_id': world.own})
    db.insert_invoice_item({'invoice_id': quote_id, 'position': 1, 'description': 'Position', 'quantity': 1,
                            'unit': 'C62', 'price_per_unit': 100.0, 'total_net': 100.0, 'tax_rate': 0.19})
    new_id = db.convert_quote_to_invoice(quote_id)
    assert columns(db, new_id)['Status'] == 'draft'
    payload = make_payload(world, invoiceId=new_id, invoiceNumber=columns(db, new_id)['InvoiceNumber'])
    payload['items'] = [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Position',
                         'unitPrice': 120.0, 'taxRate': 19}]
    assert save(payload)['success'] is True                              # ein Entwurf nimmt jede Änderung


def _marked_pdf(db, invoice_id):
    """Die abgelegte PDF der Rechnung mit einer Markierung, an der sich Überschreiben zeigt."""
    result = generate_invoice_documents(db, invoice_id, overwrite_sent=True)
    assert result['error'] is None
    with open(result['pdf_path'], 'wb') as f:
        f.write(b'%PDF-1.4 abgelegte Fassung')
    return result['pdf_path']


@pytest.mark.parametrize('status', LOCKED + ['cancelled'])
def test_generating_never_overwrites_the_document_of_a_sent_invoice(world, status):
    invoice_id = make_invoice(world, 'draft')
    pdf = _marked_pdf(world.db, invoice_id)
    force_status(world.db, invoice_id, status)
    result = generate_invoice_documents(world.db, invoice_id)
    assert result['error'] is None and result['pdf_path'] == pdf
    with open(pdf, 'rb') as f:
        assert f.read() == b'%PDF-1.4 abgelegte Fassung'


@pytest.mark.parametrize('status', LOCKED + ['cancelled'])
def test_generating_fills_in_a_missing_document_of_a_sent_invoice(world, status):
    """Ein Import oder eine gescheiterte Erzeugung hat keine Datei hinterlassen: Dann darf sie entstehen."""
    invoice_id = make_invoice(world, status)
    assert stored_documents(userctx.DATA_ROOT) == {}
    result = generate_invoice_documents(world.db, invoice_id)
    assert result['error'] is None and os.path.isfile(result['pdf_path'])


def test_generating_still_overwrites_the_document_of_a_draft(world):
    invoice_id = make_invoice(world, 'draft')
    pdf = _marked_pdf(world.db, invoice_id)
    generate_invoice_documents(world.db, invoice_id)
    with open(pdf, 'rb') as f:
        content = f.read()
    assert content.startswith(b'%PDF') and content != b'%PDF-1.4 abgelegte Fassung'


# ── Die Übergangstabelle, vollständig ────────────────────────────────────────

# (von, nach) -> Bedingung an die zugeordneten Zahlungen; was hier fehlt, ist verboten
ANY, UNPAID, PAID = 'any', 'unpaid', 'paid'
ALLOWED = {
    ('draft', 'finalized'): ANY, ('finalized', 'draft'): ANY,
    ('draft', 'sent'): ANY, ('finalized', 'sent'): ANY,
    ('sent', 'overdue'): ANY, ('overdue', 'sent'): ANY,
    ('sent', 'draft'): UNPAID, ('sent', 'finalized'): UNPAID,
    ('overdue', 'draft'): UNPAID, ('overdue', 'finalized'): UNPAID,
    ('sent', 'paid'): PAID, ('overdue', 'paid'): PAID, ('partial_payment', 'paid'): PAID,
    ('sent', 'cancelled'): ANY, ('overdue', 'cancelled'): ANY,
    ('partial_payment', 'cancelled'): ANY, ('paid', 'cancelled'): ANY,
    ('partial_payment', 'sent'): UNPAID, ('partial_payment', 'overdue'): UNPAID,
    ('paid', 'sent'): UNPAID, ('paid', 'overdue'): UNPAID,
    ('cancelled', 'sent'): ANY, ('cancelled', 'overdue'): ANY,
}


def expected_to_pass(old, new, payments):
    old, new = canonical_status(old), canonical_status(new)
    if old == new:
        return True
    condition = ALLOWED.get((old, new))
    return (condition == ANY or (condition == UNPAID and not payments)
            or (condition == PAID and payments > 0))


@pytest.mark.parametrize('payments', [0, 1])
@pytest.mark.parametrize('old, new', list(product(ALL_STATUSES + ['partial'], ALL_STATUSES)))
def test_every_status_transition_follows_the_table(world, old, new, payments):
    invoice_id = make_invoice(world, 'draft')
    if payments:
        add_payment(world, invoice_id)
    force_status(world.db, invoice_id, old)
    before = snapshot(world.db)
    change = world.db.set_invoice_status(invoice_id, new)
    stored = columns(world.db, invoice_id)['Status']
    if expected_to_pass(old, new, payments):
        assert change.ok is True and change.message is None and change.code == 200
        assert (change.old, change.new) == (canonical_status(old), new)
        assert stored == (old if canonical_status(old) == new else new)   # derselbe Status: nichts geschrieben
    else:
        assert change.ok is False and change.message and change.code in (400, 409)
        assert stored == old                                              # nichts wurde geschrieben
        assert snapshot(world.db) == before
        assert new not in allowed_targets(old, payments)
    if expected_to_pass(old, new, payments) and canonical_status(old) != new:
        assert new in allowed_targets(old, payments)


@pytest.mark.parametrize('old, payments', list(product(ALL_STATUSES, [0, 1])))
def test_the_status_select_offers_exactly_the_allowed_targets(old, payments):
    offered = allowed_targets(old, payments)
    assert offered == [new for new in STATUS_LABELS if new != old and expected_to_pass(old, new, payments)]


def test_an_unknown_status_value_is_refused_and_an_unknown_current_status_is_locked(world):
    invoice_id = make_invoice(world, 'sent')
    assert set_invoice_status(invoice_id, 'gibt-es-nicht')[0] == 400
    force_status(world.db, invoice_id, 'seltsam')
    assert status_group('seltsam') == 'locked'
    assert save(make_payload(world, invoiceId=invoice_id, invoiceNumber='26R999'))['success'] is False
    assert set_invoice_status(invoice_id, 'draft')[0] == 409               # und kein Weg heraus per Klick


def test_a_refused_transition_names_the_reason(world):
    invoice_id = make_invoice(world, 'sent')
    add_payment(world, invoice_id)
    code, answer = set_invoice_status(invoice_id, 'draft')
    assert code == 409 and answer['success'] is False and 'Zahlungen' in answer['error']
    code, answer = set_invoice_status(make_invoice(world, 'sent', number='26R101'), 'paid')
    assert code == 400 and 'mindestens eine Zahlung' in answer['error']
    code, answer = set_invoice_status(make_invoice(world, 'draft', number='26R102'), 'cancelled')
    assert code == 409 and 'Entwurf' in answer['error']


def test_paid_without_any_payment_heals_to_sent_and_overdue_but_only_then(world):
    """Ein verwaister Zustand (bezahlt, aber keine Zahlung mehr) lässt sich zurücksetzen; hängt eine
    Zahlung daran, bestimmt die Zahlungslogik den Status."""
    lone = make_invoice(world, 'paid', number='26R101')
    assert set_invoice_status(lone, 'sent')[0] == 200 and columns(world.db, lone)['Status'] == 'sent'
    paid = make_invoice(world, 'sent', number='26R102')
    add_payment(world, paid, 119.0)
    world.db.recalc_invoice_payment_state(paid)
    assert columns(world.db, paid)['Status'] in ('partial_payment', 'paid')
    code, answer = set_invoice_status(paid, 'sent')
    assert code == 409 and 'Zahlungen' in answer['error']


# ── Die Folgen eines Übergangs ───────────────────────────────────────────────

def test_sending_books_the_receivable_and_creates_the_document(world):
    invoice_id = make_invoice(world, 'finalized')
    assert world.db.get_receivable_booking(invoice_id) is None and stored_documents(userctx.DATA_ROOT) == {}
    code, answer = set_invoice_status(invoice_id, 'sent')
    assert code == 200 and 'warning' not in answer
    assert world.db.get_receivable_booking(invoice_id) is not None
    assert len(stored_documents(userctx.DATA_ROOT)) >= 1
    assert columns(world.db, invoice_id)['PDFPath']


def test_sent_and_overdue_switch_without_booking_or_creating_anything(world):
    invoice_id = make_invoice(world, 'finalized')
    set_invoice_status(invoice_id, 'sent')
    receivable = world.db.get_receivable_booking(invoice_id)
    documents, bookings = stored_documents(userctx.DATA_ROOT), booking_count(world.db)
    for status in ('overdue', 'sent', 'overdue', 'sent'):
        assert set_invoice_status(invoice_id, status)[0] == 200
        assert columns(world.db, invoice_id)['Status'] == status
    assert world.db.get_receivable_booking(invoice_id) == receivable
    assert booking_count(world.db) == bookings and stored_documents(userctx.DATA_ROOT) == documents


@pytest.mark.parametrize('first', ['overdue', 'sent'])
def test_switching_an_imported_invoice_books_no_receivable_and_creates_no_document(world, first):
    """Ein Import (WISO) bringt seine Buchungen mit und hat weder Forderung der App noch Datei: Der
    Wechsel zwischen versendet und überfällig ändert daran nichts (früher entstand die Forderung erst dabei)."""
    invoice_id = make_invoice(world, 'sent')
    assert world.db.get_receivable_booking(invoice_id) is None
    bookings = booking_count(world.db)
    for status in (first, 'sent' if first == 'overdue' else 'overdue', 'sent'):
        assert set_invoice_status(invoice_id, status)[0] == 200
    assert world.db.get_receivable_booking(invoice_id) is None
    assert booking_count(world.db) == bookings and stored_documents(userctx.DATA_ROOT) == {}


def test_setting_sent_again_keeps_the_document_and_books_nothing(world):
    invoice_id = make_invoice(world, 'finalized')
    set_invoice_status(invoice_id, 'sent')
    pdf = columns(world.db, invoice_id)['PDFPath']
    with open(pdf, 'wb') as f:
        f.write(b'%PDF-1.4 versendete Fassung')
    bookings = booking_count(world.db)
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    with open(pdf, 'rb') as f:
        assert f.read() == b'%PDF-1.4 versendete Fassung'
    assert booking_count(world.db) == bookings


def test_taking_a_sent_invoice_back_removes_the_receivable_and_makes_it_editable_again(world):
    invoice_id = make_invoice(world, 'finalized')
    set_invoice_status(invoice_id, 'sent')
    assert world.db.get_receivable_booking(invoice_id) is not None
    assert save(make_payload(world, invoiceId=invoice_id, invoiceNumber='26R999'))['success'] is False
    assert set_invoice_status(invoice_id, 'draft')[0] == 200
    assert world.db.get_receivable_booking(invoice_id) is None
    # ein Entwurf nimmt wieder jede Änderung – nur die Nummer nicht: Der Kunde kennt sie (war schon versendet)
    changed_date = save(make_payload(world, invoiceId=invoice_id, dueDate='2026-07-01', orderNumber='B-18'))
    assert changed_date['success'] is True and columns(world.db, invoice_id)['PaymentDueDate'] == '2026-07-01'
    renumbered = save_raw(make_payload(world, invoiceId=invoice_id, invoiceNumber='26R999'))
    assert renumbered.status == 409 and json.loads(renumbered)['error'] == NUMBER_KEPT_MESSAGE
    assert columns(world.db, invoice_id)['InvoiceNumber'] == '26R100'


def test_with_a_payment_there_is_no_way_back_and_the_receivable_stays(world):
    invoice_id = make_invoice(world, 'finalized')
    set_invoice_status(invoice_id, 'sent')
    booking = world.db.insert_booking('2026-07-01', 50.0, booking_type='bank', account_id=world.bank)
    ok, err = handlers.link_booking_to_invoice_capped(world.db, invoice_id, booking, 50.0)
    assert ok, err
    receivable = world.db.get_receivable_booking(invoice_id)
    for target in ('draft', 'finalized'):
        code, answer = set_invoice_status(invoice_id, target)
        assert code == 409 and 'Zahlungen' in answer['error']
    assert columns(world.db, invoice_id)['Status'] == 'partial_payment'
    assert world.db.get_receivable_booking(invoice_id) == receivable


def test_cancelling_by_hand_keeps_todays_bookings(world):
    """Storno über den Status (bis zum Stornobeleg, 2.2.0): ohne Zahlung geht die Forderung, mit Zahlung
    bleibt sie stehen."""
    lone = make_invoice(world, 'finalized')
    set_invoice_status(lone, 'sent')
    assert set_invoice_status(lone, 'cancelled')[0] == 200
    assert world.db.get_receivable_booking(lone) is None
    paid = make_invoice(world, 'finalized', number='26R101')
    set_invoice_status(paid, 'sent')
    booking = world.db.insert_booking('2026-07-01', 50.0, booking_type='bank', account_id=world.bank)
    assert handlers.link_booking_to_invoice_capped(world.db, paid, booking, 50.0)[0]
    assert set_invoice_status(paid, 'cancelled')[0] == 200
    assert world.db.get_receivable_booking(paid) is not None


def test_reviving_a_cancelled_invoice_books_the_receivable_but_keeps_its_document(world):
    invoice_id = make_invoice(world, 'finalized')
    set_invoice_status(invoice_id, 'sent')
    pdf = columns(world.db, invoice_id)['PDFPath']
    with open(pdf, 'wb') as f:
        f.write(b'%PDF-1.4 versendete Fassung')
    set_invoice_status(invoice_id, 'cancelled')
    assert world.db.get_receivable_booking(invoice_id) is None
    assert set_invoice_status(invoice_id, 'sent')[0] == 200
    assert world.db.get_receivable_booking(invoice_id) is not None
    with open(pdf, 'rb') as f:
        assert f.read() == b'%PDF-1.4 versendete Fassung'                 # früher überschrieben


def test_a_failing_document_does_not_undo_the_status(world):
    invoice_id = make_invoice(world, 'finalized')
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:
        con.execute('DELETE FROM InvoiceItems WHERE InvoiceId = ?', (invoice_id,))
        con.commit()
    code, answer = set_invoice_status(invoice_id, 'sent')
    assert code == 200 and answer['success'] is True and 'warning' in answer
    assert columns(world.db, invoice_id)['Status'] == 'sent'


def test_the_status_of_a_quote_id_or_unknown_id_changes_nothing(world):
    db = world.db
    quote_id = db.insert_invoice({
        'invoice_number': '26A001', 'invoice_date': '2026-06-09', 'seller_name': 'S',
        'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
        'sum_net': 1, 'tax_amount': 0.19, 'sum_gross': 1.19, 'amount_due': 1.19, 'status': 'draft',
        'document_type': 'quote'})
    before = snapshot(db)
    assert set_invoice_status(quote_id, 'sent')[0] == 404
    assert set_invoice_status(99999, 'sent')[0] == 404
    assert db.set_invoice_status(quote_id, 'sent').code == 404
    assert snapshot(db) == before


# ── Gleichzeitige Anfragen ───────────────────────────────────────────────────

def _run_together(*jobs):
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def run(index, job):
        barrier.wait()
        results[index] = job()

    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_a_save_racing_the_sending_never_changes_a_sent_invoice(world):
    """Entweder gewinnt das Speichern (dann steht der neue Preis im versendeten Beleg), oder das
    Versenden (dann wird das Speichern abgelehnt und der alte Preis bleibt) – nie ein Mischzustand
    aus gespeichertem Kopf und alten Positionen."""
    outcomes = set()
    for round_ in range(14):
        invoice_id = make_invoice(world, 'finalized', number=f'26R{200 + round_}')
        payload = make_payload(world, invoiceId=invoice_id, invoiceNumber=f'26R{200 + round_}')
        payload['items'][0]['unitPrice'] = 999.0
        saved, sent = _run_together(lambda: save_raw(payload),
                                    lambda: set_invoice_status(invoice_id, 'sent'))
        assert sent[0] == 200
        stored = columns(world.db, invoice_id)
        with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:
            items = con.execute('SELECT PricePerUnit, TotalNet FROM InvoiceItems WHERE InvoiceId = ? '
                                'ORDER BY Position', (invoice_id,)).fetchall()
        assert stored['Status'] == 'sent'
        assert stored['SumNet'] == sum(total for _price, total in items)        # Kopf und Positionen passen
        assert stored['SumGross'] == stored['SumNet'] + stored['TaxAmount']
        if json.loads(saved)['success']:
            assert items[0][0] == 9990000
            outcomes.add('gespeichert')
        else:
            assert saved.status == 409 and items[0][0] == 1000000
            outcomes.add('abgelehnt')
    assert outcomes                                              # mindestens ein Ausgang, beide sind gültig


def test_two_saves_of_a_sent_invoice_racing_are_both_refused(world):
    invoice_id = make_invoice(world, 'sent')
    before = snapshot(world.db)
    first = make_payload(world, invoiceId=invoice_id, invoiceNumber='26R991')
    second = make_payload(world, invoiceId=invoice_id, invoiceNumber='26R992')
    answers = _run_together(lambda: save_raw(first), lambda: save_raw(second))
    assert [a.status for a in answers] == [409, 409]
    assert snapshot(world.db) == before


def test_two_sendings_racing_send_once(world):
    invoice_id = make_invoice(world, 'finalized')
    answers = _run_together(lambda: set_invoice_status(invoice_id, 'sent'),
                            lambda: set_invoice_status(invoice_id, 'sent'))
    assert [a[0] for a in answers] == [200, 200]
    assert world.db.get_receivable_booking(invoice_id) is not None
    assert columns(world.db, invoice_id)['Status'] == 'sent'
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:
        assert con.execute("SELECT COUNT(*) FROM Bookings WHERE DocumentNumber = '26R100'").fetchone()[0] == 1


# ── Über den Server ──────────────────────────────────────────────────────────

@pytest.fixture
def books(auto_login):
    w = build_world(auto_login.db)
    w.csrf = auto_login.csrf
    return w


def created_over_http(port, w, status):
    """Eine Rechnung über den Server anlegen (Datenbank des angemeldeten Benutzers) und in den Status setzen."""
    resp = post_json(port, '/invoice/save', make_payload(w), w.csrf)
    invoice_id = json.loads(resp.text)['invoice_id']
    force_status(w.db, invoice_id, status)
    return invoice_id


def test_over_http_a_locked_change_is_409_and_the_open_fields_are_200(server_port, books):
    invoice_id = created_over_http(server_port, books, 'sent')
    refused = post_json(server_port, '/invoice/save',
                        make_payload(books, invoiceId=invoice_id, invoiceNumber='26R999'), books.csrf)
    assert refused.status == 409 and refused.headers['Content-Type'].startswith('application/json')
    assert json.loads(refused.text)['error'].startswith(LOCKED_MESSAGE)
    saved = post_json(server_port, '/invoice/save',
                      {'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'}, books.csrf)
    assert saved.status == 200 and json.loads(saved.text) == {'success': True, 'invoice_id': invoice_id,
                                                              'documentVersion': 2}
    assert row(books.db, invoice_id)[23] == 'B-99'                       # Spalte 23 = OrderNumber


def test_over_http_a_refused_transition_is_409_with_the_reason(server_port, books):
    invoice_id = created_over_http(server_port, books, 'sent')
    refused = post_json(server_port, '/invoice/status', {'invoice_id': invoice_id, 'status': 'partial_payment'},
                        books.csrf)
    assert refused.status == 409 and json.loads(refused.text)['success'] is False
    assert json.loads(refused.text)['error']
    done = post_json(server_port, '/invoice/status', {'invoice_id': invoice_id, 'status': 'overdue'}, books.csrf)
    assert done.status == 200


# ── Die Maske ────────────────────────────────────────────────────────────────

FINANCE_IDS = ['invoice_date', 'invoice_number', 'own_company_select', 'customer_select',
               'bank_account_select', 'show_tax', 'tax_rate', 'payment_term_days', 'payment_due_date',
               'discount_percentage', 'discount_days']
OPEN_IDS = ['delivery_date', 'order_number', 'payment_terms']


def _by_id(dom, element_id):
    return [a for t, a in dom.tags if a.get('id') == element_id][0]


def _buttons(dom, onclick):
    return [a for t, a in dom.tags if t == 'button' and a.get('onclick') == onclick]


def _status_options(html):
    select = re.search(r'<select id="statusChangeSelect">(.*?)</select>', html, re.S).group(1)
    return re.findall(r'value="(\w+)"', select)


def test_the_form_of_a_sent_invoice_locks_the_finance_fields_and_keeps_the_open_ones(world):
    invoice_id = make_invoice(world, 'sent')
    html = PageInvoice(world.db, {}, invoice_id)
    dom = Dom(html)
    for element_id in FINANCE_IDS:
        assert 'disabled' in _by_id(dom, element_id), element_id
    for element_id in OPEN_IDS + ['buyer_route_id']:
        assert 'disabled' not in _by_id(dom, element_id), element_id
    rows_ = [a for t, a in dom.tags if t == 'input' and 'item-description' in (a.get('class') or '')]
    assert rows_ and all('disabled' in a for a in rows_)
    assert dom.one('input', id='invoice_locked')['value'] == 'true'
    assert 'Änderungen können trotzdem gespeichert werden' not in html
    assert 'k&ouml;nnen trotzdem gespeichert werden' not in html
    assert 'Nicht im Entwurfsstatus' not in html
    assert ('Diese Rechnung ist versendet. Änderbar sind Zahlungshinweis, Anschrift, Bestellnummer, Leitweg-ID und '
            'Leistungsdatum. Jede Änderung wird protokolliert; die bisherige Fassung bleibt als Datei erhalten, und es '
            'entsteht eine neue Fassung, die dem Kunden erneut zugehen muss.') in unescape(html)
    assert 'Betr&auml;ge, Positionen, Nummer, Datum und Kunde sind gesperrt' not in html     # der Text von 2.1.2
    assert len(_buttons(dom, 'saveInvoice()')) == 1                  # der Knopf bleibt: Zahlungshinweis & Co. sind änderbar
    assert '✏ Berichtigen</button>' in html and '💾 Speichern</button>' not in html


def test_the_form_of_a_draft_is_fully_editable_and_offers_deleting(world):
    invoice_id = make_invoice(world, 'draft')
    html = PageInvoice(world.db, {}, invoice_id)
    dom = Dom(html)
    for element_id in FINANCE_IDS + OPEN_IDS:
        assert 'disabled' not in _by_id(dom, element_id), element_id
    assert dom.one('input', id='invoice_locked')['value'] == 'false'
    assert "appConfirmPost('/invoice/delete?id=%d'" % invoice_id in html
    assert len(_buttons(dom, 'saveInvoice()')) == 1


@pytest.mark.parametrize('status', ['finalized', 'sent', 'overdue', 'partial_payment', 'paid', 'cancelled'])
def test_only_a_draft_offers_deleting_in_the_form(world, status):
    invoice_id = make_invoice(world, status)
    assert '/invoice/delete?id=' not in PageInvoice(world.db, {}, invoice_id).split('Ende gridLeftCol')[1]


def test_the_form_of_a_cancelled_invoice_is_locked_completely(world):
    invoice_id = make_invoice(world, 'cancelled')
    html = PageInvoice(world.db, {}, invoice_id)
    dom = Dom(html)
    for element_id in FINANCE_IDS + OPEN_IDS:
        assert 'disabled' in _by_id(dom, element_id), element_id
    assert _buttons(dom, 'saveInvoice()') == []                      # kein Speichern-Knopf
    assert 'Storniert: Die Rechnung l&auml;sst sich nicht mehr &auml;ndern.' in html


def test_the_copy_form_of_a_sent_invoice_is_an_open_new_draft(world):
    invoice_id = make_invoice(world, 'sent')
    dom = Dom(PageInvoice(world.db, {}, None, copy_from=invoice_id))
    for element_id in FINANCE_IDS + OPEN_IDS:
        assert 'disabled' not in _by_id(dom, element_id), element_id
    assert dom.one('input', id='invoice_locked')['value'] == 'false'


@pytest.mark.parametrize('status, payments, offered', [
    ('draft', 0, ['draft', 'finalized', 'sent']),
    ('finalized', 0, ['finalized', 'draft', 'sent']),
    ('sent', 0, ['sent', 'draft', 'finalized', 'overdue', 'cancelled']),
    ('overdue', 0, ['overdue', 'draft', 'finalized', 'sent', 'cancelled']),
    ('sent', 1, ['sent', 'overdue', 'paid', 'cancelled']),
    ('partial_payment', 1, ['partial_payment', 'paid', 'cancelled']),
    ('paid', 1, ['paid', 'cancelled']),
    ('cancelled', 0, ['cancelled', 'sent', 'overdue']),
])
def test_the_status_select_offers_only_the_allowed_targets(world, status, payments, offered):
    invoice_id = make_invoice(world, 'draft')
    if payments:
        add_payment(world, invoice_id)
    force_status(world.db, invoice_id, status)
    assert _status_options(PageInvoice(world.db, {}, invoice_id)) == offered


def test_the_form_script_sends_only_the_open_fields_of_a_locked_invoice(world):
    html = PageInvoice(world.db, {}, make_invoice(world, 'sent'))
    script = html.split('function saveInvoice()')[1].split('function generatePDF')[0]
    reduced = script.split("document.getElementById('invoice_locked').value === 'true'")[1].split('return;')[0]
    for key in ('invoiceId', 'paymentTerms', 'orderNumber', 'buyerReference', 'deliveryDate'):
        assert key in reduced
    for key in ('items', 'invoiceNumber', 'customerId', 'ownCompanyId', 'dueDate', 'status'):
        assert key not in reduced
    assert 'status: currentStatus' not in script                          # nie vom Client
