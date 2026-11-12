# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Das Formular einer versendeten Rechnung überschreibt nichts still (2.1.2).

Bei einer versendeten Rechnung bleiben Begleittext, Anschrift, Bestellnummer, Leitweg-ID und
Leistungsdatum änderbar. Früher setzte die Maske beim Laden die Leitweg-ID auf den heutigen Wert der
Stammdaten (bei einem Kunden außerhalb der Auswahl auf leer) und jedes Speichern holte die heutige
Anschrift. Jetzt:

* Die Maske zeigt die **gespeicherten** Werte (Anschrift der Rechnung, Leitweg-ID) und holt beim Laden
  nichts aus den Stammdaten.
* Geschickt und geschrieben wird nur, was der Nutzer geändert hat; ein unverändertes Speichern ändert
  keine Spalte (auch nicht den Leerwert an die Stelle von ``NULL``).
* Leitweg-ID und Bestellnummer sind normale Eingabefelder.
* Die Anschrift wird nur auf ausdrücklichen Wunsch neu aus den Stammdaten **desselben** Kunden
  übernommen: Kästchen „Anschrift aus den Stammdaten übernehmen“ (``refreshAddress``), das alt → neu
  zeigt und nur erscheint, wenn die Stammdaten abweichen.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import contextlib
import sqlite3
from html import unescape

import pytest

import server.handlers as handlers
from server.pages_invoice import PageInvoice
from tests.dom_helpers import Dom
from tests.invoice_world import (build_world, columns, finance, force_status, make_invoice, make_payload,
                                 save, save_raw, snapshot)

HINT = ('Änderbar sind Begleittext, Anschrift, Bestellnummer, Leitweg-ID und Leistungsdatum. '
        'Das abgelegte Dokument (PDF/XML) bleibt dabei unverändert.')


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return build_world(tmp_db)


def sql(w, statement, *args):
    with contextlib.closing(sqlite3.connect(w.db.db_name)) as con:
        con.execute(statement, args)
        con.commit()


def imported_invoice(w):
    """Eine Rechnung, wie ein Import sie hinterlässt: Kunde, der nicht zur Auswahl gehört (Lieferant),
    kein Begleittext, keine Bestellnummer, kein Leitweg, kein Leistungsdatum."""
    supplier = w.db.insert_contact(contact_type='supplier', entity_type='company',
                                   display_name='Altlieferant GmbH', company_name='Altlieferant GmbH',
                                   street='Altweg 3', postal_code='50667', city='Köln',
                                   email='alt@example.com')
    invoice_id = make_invoice(w, 'draft', number='IMP-1', customerId=supplier)
    sql(w, 'UPDATE Invoices SET PaymentTerms = NULL, OrderNumber = NULL, BuyerReference = NULL, '
           'BuyerRouteID = NULL, DeliveryDate = NULL, Status = ? WHERE ID = ?', 'sent', invoice_id)
    return invoice_id, supplier


def change_customer_master(w):
    w.db.update_contact(w.cust, contact_type='customer', entity_type='company',
                        display_name='Malermeister Pinsel', company_name='Malermeister Pinsel GmbH',
                        customer_number='K-100', street='Pinselallee 7', postal_code='12345',
                        city='Farbstadt', email='info@example.com')


# ── Ein unverändertes Speichern ändert keine Spalte ────────────────────────────

UNCHANGED_SHAPES = {
    'nur-die-ID': lambda stored: {},
    'alle-vier-wie-gespeichert': lambda stored: {
        'paymentTerms': stored['PaymentTerms'], 'orderNumber': stored['OrderNumber'],
        'buyerReference': stored['BuyerReference'], 'deliveryDate': stored['DeliveryDate']},
    'Leerwerte-fuer-NULL': lambda stored: {
        'paymentTerms': '', 'orderNumber': None, 'buyerReference': None, 'deliveryDate': None},
}


@pytest.mark.parametrize('shape', list(UNCHANGED_SHAPES))
def test_an_unchanged_save_of_an_imported_invoice_changes_no_column(world, shape):
    invoice_id, supplier = imported_invoice(world)
    before = columns(world.db, invoice_id)
    form = UNCHANGED_SHAPES[shape](before)
    if shape == 'Leerwerte-fuer-NULL':
        assert all(before[c] is None for c in ('PaymentTerms', 'OrderNumber', 'BuyerReference',
                                               'BuyerRouteID', 'DeliveryDate'))
    assert save({'invoiceId': invoice_id, **form}) == {'success': True, 'invoice_id': invoice_id}
    assert columns(world.db, invoice_id) == before                       # jede Spalte, auch UpdatedAt
    assert before['CustomerId'] == supplier


def test_an_unchanged_save_of_a_sent_invoice_changes_no_column_although_the_master_data_moved_on(world):
    invoice_id = make_invoice(world, 'sent')
    before = columns(world.db, invoice_id)
    change_customer_master(world)
    world.db.update_contact(world.cust, contact_type='customer', entity_type='company',
                            display_name='Malermeister Pinsel', company_name='Malermeister Pinsel GmbH',
                            customer_number='K-100', street='Pinselallee 7', postal_code='12345',
                            city='Farbstadt', email='info@example.com', buyer_route_id='991-00000-00')
    assert save({'invoiceId': invoice_id}) == {'success': True, 'invoice_id': invoice_id}
    assert columns(world.db, invoice_id) == before
    assert save(make_payload(world, invoiceId=invoice_id)) == {'success': True, 'invoice_id': invoice_id}
    assert columns(world.db, invoice_id) == before                       # auch das komplette Formular, unverändert


def test_only_what_changed_is_written(world):
    invoice_id = make_invoice(world, 'sent')
    before = columns(world.db, invoice_id)
    assert save({'invoiceId': invoice_id, 'orderNumber': 'B-4711'})['success'] is True
    after = columns(world.db, invoice_id)
    assert {k: (before[k], after[k]) for k in before if before[k] != after[k]} == {
        'OrderNumber': ('B-17', 'B-4711')}


def test_a_change_that_only_adds_a_line_break_is_still_written(world):
    """Verglichen wird genau: Ein Zeilenumbruch mehr im Begleittext ist eine Änderung."""
    invoice_id = make_invoice(world, 'sent')
    assert save({'invoiceId': invoice_id, 'paymentTerms': 'Zahlbar in 14 Tagen\n'})['success'] is True
    assert columns(world.db, invoice_id)['PaymentTerms'] == 'Zahlbar in 14 Tagen\n'


def test_the_leitweg_id_and_the_order_number_are_normal_inputs(world):
    invoice_id = make_invoice(world, 'sent')
    assert save({'invoiceId': invoice_id, 'buyerReference': '991-12345-67', 'orderNumber': 'B-9'})['success']
    stored = columns(world.db, invoice_id)
    assert (stored['BuyerReference'], stored['BuyerRouteID'], stored['OrderNumber']) == ('991-12345-67',
                                                                                     '991-12345-67', 'B-9')
    assert save({'invoiceId': invoice_id, 'buyerReference': None, 'orderNumber': None})['success']
    stored = columns(world.db, invoice_id)
    assert (stored['BuyerReference'], stored['BuyerRouteID'], stored['OrderNumber']) == (None, None, None)


def test_the_complete_form_of_a_sent_invoice_does_not_pull_the_address_either(world):
    """Ein älterer Client schickt das komplette Formular: Die Anschrift bleibt trotzdem, wie sie steht."""
    invoice_id = make_invoice(world, 'sent')
    before = columns(world.db, invoice_id)
    change_customer_master(world)
    assert save(make_payload(world, invoiceId=invoice_id, orderNumber='B-5'))['success'] is True
    after = columns(world.db, invoice_id)
    assert {k for k in before if before[k] != after[k]} == {'OrderNumber'}


def test_the_address_request_takes_only_the_master_data_of_the_invoices_own_customer(world):
    invoice_id = make_invoice(world, 'sent')
    locked = finance(world.db, invoice_id)
    world.db.update_contact(world.cust2, contact_type='customer', entity_type='company',
                            display_name='Bau AG', company_name='Bau AG', customer_number='K-200',
                            street='Anderswo 1', postal_code='99999', city='Nirgendwo',
                            email='bau@example.com')
    assert save({'invoiceId': invoice_id, 'refreshAddress': True})['success'] is True
    stored = columns(world.db, invoice_id)
    assert (stored['BuyerStreet'], stored['BuyerCity']) == ('Kundenweg 5', 'Kundenhausen')   # der Kunde 2 zählt nicht
    assert finance(world.db, invoice_id) == locked


def test_the_address_request_is_only_taken_at_its_word(world):
    invoice_id = make_invoice(world, 'sent')
    change_customer_master(world)
    before = columns(world.db, invoice_id)
    for fake in ('true', 1, 'ja', None, False):
        assert save({'invoiceId': invoice_id, 'refreshAddress': fake})['success'] is True
        assert columns(world.db, invoice_id) == before


# ── Die Maske zeigt, was gespeichert ist ──────────────────────────────────────

ADDRESS = ('<div id="customer_address_display" style="min-height: 80px; white-space: pre-line;">'
           '{}</div>')


def page(w, invoice_id):
    return PageInvoice(w.db, {}, invoice_id)


def test_the_page_of_a_sent_invoice_shows_the_stored_address_and_leitweg_id(world):
    invoice_id = make_invoice(world, 'sent')
    change_customer_master(world)
    world.db.update_contact(world.cust, contact_type='customer', entity_type='company',
                            display_name='Malermeister Pinsel', company_name='Malermeister Pinsel GmbH',
                            customer_number='K-100', street='Pinselallee 7', postal_code='12345',
                            city='Farbstadt', email='info@example.com', buyer_route_id='991-00000-00')
    html = page(world, invoice_id)
    assert ADDRESS.format('Malermeister Pinsel\nKundenweg 5\n54321 Kundenhausen') in html
    leitweg = Dom(html).one('input', id='buyer_route_id')
    assert leitweg['value'] == '04011000-1234512345-35'                   # gespeichert, nicht der Stammdatenwert
    assert 'readonly' not in leitweg and 'disabled' not in leitweg        # ein normales Eingabefeld
    assert Dom(html).one('input', id='order_number').get('disabled') is None
    assert Dom(html).one('input', id='invoice_locked')['value'] == 'true'


def test_the_page_offers_the_master_data_address_only_when_it_differs(world):
    invoice_id = make_invoice(world, 'sent')
    assert 'id="refresh_address"' not in page(world, invoice_id)          # Stammdaten = gespeicherte Anschrift
    change_customer_master(world)
    html = page(world, invoice_id)
    dom = Dom(html)
    box = dom.one('input', id='refresh_address')
    assert box['type'] == 'checkbox' and 'checked' not in box             # nie von allein angekreuzt
    text = unescape(html)
    assert 'Anschrift aus den Stammdaten übernehmen' in text
    assert 'Malermeister Pinsel, Kundenweg 5, 54321 Kundenhausen → Malermeister Pinsel GmbH, ' \
           'Pinselallee 7, 12345 Farbstadt' in text                       # alt → neu


def test_an_imported_invoice_with_a_customer_outside_the_selection_shows_its_values(world):
    invoice_id, supplier = imported_invoice(world)
    sql(world, "UPDATE Invoices SET BuyerReference = '04011000-1', BuyerRouteID = '04011000-1', "
               "OrderNumber = 'B-1' WHERE ID = ?", invoice_id)
    html = page(world, invoice_id)
    assert ADDRESS.format('Altlieferant GmbH\nAltweg 3\n50667 Köln') in html
    dom = Dom(html)
    assert dom.one('input', id='buyer_route_id')['value'] == '04011000-1'
    assert 'id="refresh_address"' not in html                              # Stammdaten und Anschrift sind gleich
    customer_select = [a for a in dom.all('select') if a.get('id') == 'customer_select']
    assert customer_select and 'disabled' in customer_select[0]


def test_the_page_of_a_cancelled_invoice_offers_no_address_refresh(world):
    invoice_id = make_invoice(world, 'cancelled')
    change_customer_master(world)
    html = page(world, invoice_id)
    assert 'id="refresh_address"' not in html
    assert 'disabled' in Dom(html).one('input', id='buyer_route_id')


def test_the_hint_of_a_sent_invoice_is_honest_about_the_document(world):
    text = unescape(page(world, make_invoice(world, 'sent')))
    assert HINT in text
    assert 'wird beim Speichern aus den Stammdaten' not in text            # der alte Satz ist weg


def test_the_page_of_a_draft_still_follows_the_customer(world):
    invoice_id = make_invoice(world, 'draft')
    html = page(world, invoice_id)
    leitweg = Dom(html).one('input', id='buyer_route_id')
    assert 'readonly' in leitweg                                           # im Entwurf folgt sie dem Kunden
    assert ADDRESS.format('') in html and 'id="refresh_address"' not in html


# ── Das Skript der Maske ─────────────────────────────────────────────────────

def script_of(world):
    html = page(world, make_invoice(world, 'sent'))
    return next(s for s in Dom(html).scripts if 'function saveInvoice' in s)


def test_the_script_does_not_overwrite_the_stored_values_of_a_sent_invoice(world):
    script = script_of(world)
    guard = script.index("document.getElementById('invoice_locked').value === 'true'", script.index(
        'function updateCustomerAddress'))
    assert guard < script.index('buyerRouteIdField.value = customer.buyer_route_id')   # vor jedem Überschreiben
    assert 'return;' in script[guard:script.index('buyerRouteIdField.value = customer.buyer_route_id')]
    # Das Leistungsdatum wird bei einer versendeten Rechnung nicht vorbelegt
    assert "document.getElementById('invoice_locked').value !== 'true') del.value = inv.value" in script


def test_the_script_sends_only_what_the_user_changed(world):
    script = script_of(world)
    assert 'const initialOpenFields' in script
    assert "if ((current[key] || '') !== (initialOpenFields[key] || '')) changes[key] = current[key];" in script
    assert "if (refresh && refresh.checked) changes.refreshAddress = true;" in script
    # kein unbedingtes Mitschicken der vier Felder mehr
    assert "paymentTerms: document.getElementById('payment_terms').value,\n                    orderNumber" \
           not in script.split('const changes')[1].split('sendInvoice(changes')[0]
