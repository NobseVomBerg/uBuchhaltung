# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zahlungsvorschläge, 2.8.1: Zahlungseingang ↔ offene Rechnung – Datenbankschicht, Seiten und Fehlerweg.

Nach dem Einlesen eines Kontoauszugs stehen Zahlungseingänge „offen“ in der Buchungsliste, obwohl im Verwendungszweck die Rechnungsnummer steht.
Die Seite schlägt jetzt vor, was zusammengehört – **es wird nur vorgeschlagen: Zugeordnet wird allein auf Klick, über die bestehenden Wege
(``POST /invoice/link-payment``, das Feld ``invoice_id`` der Maske), nichts wird vorausgewählt.** Die Rangfolge steht rein in
``tests/test_payment_matching.py``; hier:

* die Datenbankschicht (``Database.suggest_invoice_payments``, ``suggest_invoices_for_booking``, ``suggest_bookings_for_invoice``): rein lesend, offene
  Rechnungen und Bewegungen ohne Kind und Zuordnung (EINE Abfrage, die die Zuordnungen einmal liest), der freie Rest einer einzelnen Bewegung nach
  ``_free_minor_sql``, schon zugeordnete Paare fehlen, kaputte Werte sind kein Kandidat und kein Fehler, die Übersicht ist nicht an den Zeitraum der
  Seite gebunden; **nur Euro** (eine Rechnung oder Bewegung in einer anderen Währung ist nie Teil eines Vorschlags);
* **nur, was von der Bank kommt, zählt:** Beleg-Nr. und Kontakt einer Bankzeile trägt eine Zuordnung dort ein und lässt sie beim Entfernen stehen – sie
  sind kein Beweis. Der Fall dazu über die echten Wege (falsch zuordnen, wieder entfernen: danach ist der Vorschlag wieder der erste) und der Kundenname
  (die Firma, sonst der Name – nicht der Ansprechpartner neben der Firma);
* der Kasten „Zahlungseingänge zu offenen Rechnungen“ auf der Seite *Buchungen* (nur mit Vorschlägen, links über der Liste, maskiert, höchstens 20
  Zeilen und „… und n weitere“) und sein Skript (``payAssign``: der bestehende Weg, bei Erfolg neu laden, bei einem Fehler die Meldung und die
  Zeile bleibt);
* die Maske einer Bankbewegung (Vorschläge zuerst in der Auswahl „Rechnung“, gewählt bleibt „-- Keine Rechnung --“, „Übernehmen“ setzt nur den Wert;
  neue Buchung und Vorbelegung ``from_invoice`` bleiben, wie sie waren; öffnen und unverändert speichern ändert nichts);
* der Fehlerweg: Schlägt die Zuordnung nach dem Speichern fehl, bleibt der Nutzer bei der Buchung (``/transactions/edit?id=…&err=…``);
* die Rechnungsseite („Vorhandene Buchung verknüpfen“: Vorschläge zuerst, Begründung klein unter dem Auftraggeber, der Haken bei gleichem Betrag bleibt);
* **Skonto (2.9.0, ganz unten):** Die Abfrage der offenen Rechnungen liest die Angaben, die über ein Skonto entscheiden (``skonto_open_minor``, ``skonto_limit_minor``); der Betrag nach Abzug
  des Skontos ist mit dem Kunden ein Vorschlag (``suggestion.skonto``); eine Rechnung ohne Skonto, mit einer Gruppe oder einem Kopfsatz ohne Skonto-Konto (nicht 19, 7 und 0 %), mit einem Erlöskonto der
  Stammdaten, das selbst ein Skonto-Konto ist, oder mit Positionen, die nicht zu den Summen passen, hat keines (mehrere Sätze und Sätze aus den Stammdaten gehen); kaputte Werte
  sind kein Fehler; ``skonto_offer`` und ``skonto_limits`` lesen nur; die Abfrage wächst nicht schneller als die Zahl der Rechnungen. Die Oberfläche steht in ``tests/test_skonto_pages.py``.

Alle Namen, Nummern und Beträge sind erfundene Testwerte; Daten stehen fest im Test (kein „heute“).
"""
import json
import re
import shutil
import sqlite3
import subprocess
import urllib.parse
from contextlib import closing
from decimal import Decimal
from html import unescape

import pytest

from db import Database
from db.invoice_payments import (MIXED_GROUPS_MISMATCH, PAYABLE_STATUSES, SKONTO_ACCOUNT_NUMBERS, SKONTO_GROUP_RATE, SKONTO_NO_TERMS, SKONTO_RATE, SKONTO_REVENUE_DISCOUNT,
                                 STORNO_KIND)
from db.payment_matching import AMBIGUOUS_TEXT, MAX_SUGGESTIONS, REASON_AMOUNT, REASON_CUSTOMER, REASON_NUMBER
from db.payment_suggestions import _OPEN_INVOICES, _UNBOOKED_MOVEMENTS, MAX_INVOICES, MAX_MOVEMENTS, PaymentOverview
from money import fmt_eur, from_minor, to_minor
from server import handlers
from server.handlers import PAYMENT_NOT_ASSIGNED, handle_add_transaction
from server.pages import script_json
from server.pages_invoice import PageInvoice
from server.pages_transactions import PAY_BADGE_LIMIT, PAY_FAILED, PAY_SUGGEST_JS, PAY_SUGGEST_LIMIT, PageTransactions
from tests.document_helpers import post_json, state, table_rows
from tests.dom_helpers import Dom, app_msg_arguments, assert_same_structure
from tests.entry_form_roundtrip import differences, open_form, snapshot
from tests.http_client import get, own_origin
from tax_groups import rate_groups

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – nur die Läufe des Skripts entfallen')

DAY = '2026-03-04'                          # das Rechnungsdatum der Tests
NUMBER_AND_NAME = 'Rechnungsnummer im Verwendungszweck · Betrag stimmt · Kunde passt'
AMOUNT_AND_NAME = 'Betrag stimmt · Kunde passt'
HOSTILE = '</script><img src=x onerror=alert(1)>'
INVOICE_SELECT = re.compile(r'<select name="invoice_id">.*?</select>', re.S)


# ── Hilfen ───────────────────────────────────────────────────────────────────

def bank_account(db, name='Zuordnungs-Bank', skr=1800):
    if not [a for a in db.fetch_accounts() if a[1] == name]:
        db.insert_account(name, 'Ich', 'DE00ZUORD', 'BIC', 'Bank', is_cash=0, skr_account=skr)
    return [a for a in db.fetch_accounts() if a[1] == name][0][0]


def customer(db, name='Käufer AG'):
    db.insert_contact(contact_type='customer', display_name=name, company_name=name)
    with closing(sqlite3.connect(db.db_name)) as conn:
        return conn.execute('SELECT ID FROM Contacts WHERE DisplayName = ?', (name,)).fetchone()[0]


def add_invoice(db, number, gross, *, name='Käufer AG', person='Kim Käufer', customer_id=None, date=DAY, status='sent', **extra):
    """Eine Rechnung (19 %, ``gross`` brutto, nichts bezahlt); bei ``sent`` mit der Forderungsbuchung wie beim Versenden."""
    net = round(gross / 1.19, 2)
    data = {'invoice_number': number, 'invoice_date': date, 'payment_due_date': '2099-12-31', 'seller_name': 'Vera Verkäufer',
            'seller_company': 'Verkäufer GmbH', 'buyer_name': person, 'buyer_company': name, 'customer_id': customer_id, 'tax_rate': 0.19,
            'sum_net': net, 'tax_amount': round(gross - net, 2), 'sum_gross': gross, 'amount_due': gross, 'status': status}
    data.update(extra)
    invoice_id = db.insert_invoice(data)
    if status == 'sent':
        db.ensure_receivable_booking(invoice_id)
    return invoice_id


def add_payment(db, account, amount, date='2026-03-10', recipient='Käufer AG', text='', **fields):
    """Ein Zahlungseingang (Bankbewegung) ohne Kind und ohne Zuordnung."""
    return db.insert_booking(date, amount, account_id=account, booking_type='bank', recipient_client=recipient, text=text, **fields)


def run_sql(db, sql, *args):
    with closing(sqlite3.connect(db.db_name)) as conn:
        conn.execute(sql, args)
        conn.commit()


def a_world(db):
    """Zwei offene Rechnungen und zwei Zahlungseingänge, die Nummern stehen im Verwendungszweck (zwei der Schreibweisen aus der Praxis). Liefert die
    IDs als Wörterbuch: ``account``, ``buyer``, ``miller`` (Kontakte), ``one``, ``two`` (Rechnungen), ``first``, ``second`` (Zahlungen)."""
    account = bank_account(db)
    buyer, miller = customer(db, 'Käufer AG'), customer(db, 'Müller GmbH')
    one = add_invoice(db, 'R26001', 119.00, name='Käufer AG', customer_id=buyer)
    two = add_invoice(db, 'R26002', 238.00, name='Müller GmbH', person='Mia Müller', customer_id=miller)
    first = add_payment(db, account, 119.00, '2026-03-10', 'Käufer AG', 'Rechnungs-Nr R26001')
    second = add_payment(db, account, 238.00, '2026-03-11', 'Müller GmbH', 'RE-Nr. R26002.KD-Nr. K-0001')
    return {'account': account, 'buyer': buyer, 'miller': miller, 'one': one, 'two': two, 'first': first, 'second': second}


def overview_pairs(db, limit=20):
    return [(m['id'], i['id']) for _s, m, i in db.suggest_invoice_payments(limit).items]


def period_page(db):
    return PageTransactions(db, date_from='2026-01-01', date_to='2026-12-31')


def box_of(page):
    """Der Inhalt des Kastens „Zahlungseingänge zu offenen Rechnungen“ (``#paySuggest``) einer Seite *Buchungen*."""
    _head, _, rest = page.partition('<div id="paySuggest">')
    assert rest, 'kein Container #paySuggest'
    return rest.split('</div><script>var PAY_FAILED', 1)[0]


def box_rows(page):
    """Die Zeilen des Kastens als Listen von Zellentexten, ohne die Kopfzeile."""
    box = box_of(page)
    return table_rows(box)[1:] if box.strip() else []


def option_labels(html):
    """Die Optionen der Auswahl „Rechnung“: ``[(Wert, Text, ausgewählt)]``."""
    select = INVOICE_SELECT.search(html)
    assert select, 'keine Auswahl „Rechnung“'
    return [(value, re.sub(r'\s+', ' ', unescape(text)).strip(), 'selected' in before + after)
            for before, value, after, text in re.findall(r'<option([^>]*?)value="([^"]*)"([^>]*)>(.*?)</option>', select.group(0), re.S)]


# ── Die Datenbankschicht: die Übersicht ──────────────────────────────────────

def test_the_limits_are_the_planned_ones():
    assert (MAX_INVOICES, MAX_MOVEMENTS, MAX_SUGGESTIONS, PAY_SUGGEST_LIMIT) == (500, 500, 5, 20)


def test_the_overview_pairs_each_payment_with_the_invoice_named_in_its_text(tmp_db):
    w = a_world(tmp_db)
    overview = tmp_db.suggest_invoice_payments()
    assert isinstance(overview, PaymentOverview) and overview.more == 0
    assert [(s.movement_id, s.invoice_id, s.score) for s, _m, _i in overview.items] == [(w['first'], w['one'], 180), (w['second'], w['two'], 180)]
    suggestion, movement, invoice = overview.items[0]
    assert suggestion.reasons == (REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER) and suggestion.ambiguous is False
    assert (movement['recipient'], movement['amount_minor'], movement['total_minor']) == ('Käufer AG', 1190000, 1190000)
    assert (invoice['number'], invoice['label'], invoice['open_minor']) == ('R26001', 'Käufer AG', 1190000)


def test_the_overview_is_independent_of_any_period(tmp_db):
    account = bank_account(tmp_db)
    old = add_invoice(tmp_db, 'R22001', 119.00, customer_id=customer(tmp_db), date='2022-01-10')
    payment = add_payment(tmp_db, account, 119.00, '2022-01-20', 'Käufer AG', 'Re.Nr. R22001')
    assert overview_pairs(tmp_db) == [(payment, old)]
    page = period_page(tmp_db)                                                          # ein ganz anderer Zeitraum
    assert [row[3] for row in box_rows(page)] == ['R22001'] and 'aus allen Zeiträumen' in box_of(page)


def test_only_a_payment_without_children_and_without_an_assignment_is_in_the_overview(tmp_db):
    w = a_world(tmp_db)
    other = add_payment(tmp_db, w['account'], 50.00, '2026-03-12', 'Käufer AG', 'Rechnung R26001')                # eine zweite Zahlung zu R26001
    assert overview_pairs(tmp_db) == [(w['first'], w['one']), (w['second'], w['two'])]                             # die Rechnung 1 nimmt nur die erste
    child = tmp_db.insert_booking('2026-03-10', 119.00, booking_type='entry', parent_booking_id=w['first'],
                                  coa_id=tmp_db.get_coa_id_by_account_number(4400), counter_coa_id=tmp_db.get_coa_id_by_account_number(1800))
    assert overview_pairs(tmp_db) == [(w['second'], w['two']), (other, w['one'])]                                  # die Zahlung 1 hat ein Kind: nicht mehr offen
    run_sql(tmp_db, 'DELETE FROM Bookings WHERE ID = ?', child)
    assert (w['first'], w['one']) in overview_pairs(tmp_db)
    assert tmp_db.assign_invoice_payment(w['one'], w['first']).ok
    assert overview_pairs(tmp_db) == [(w['second'], w['two'])]                                                     # zugeordnet (und kontiert): kein Vorschlag mehr


def test_a_payment_with_an_assignment_but_without_children_is_not_in_the_overview_either(tmp_db):
    """Eine Zuordnung ohne Buchungssätze (so schreibt sie der Import der WISO-Datenbank) nimmt der Bewegung den freien Rest, ohne dass sie ein Kind hat."""
    w = a_world(tmp_db)
    settled = add_invoice(tmp_db, 'R25009', 119.00, customer_id=w['buyer'], status='paid', amount_due=0.00)               # eine andere, schon bezahlte Rechnung
    run_sql(tmp_db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)', settled, w['first'], 1190000, '2026-03-10')
    assert tmp_db.get_booking_unallocated_amount(w['first']) == 0                      # nach der Formel ist nichts frei
    assert overview_pairs(tmp_db) == [(w['second'], w['two'])]                         # die Zahlung 1 passte sonst zur offenen Rechnung 1 (Nummer im Text)
    assert tmp_db.suggest_invoices_for_booking(w['first']) == []


def test_what_is_no_payment_is_never_offered(tmp_db):
    w = a_world(tmp_db)
    add_payment(tmp_db, w['account'], -119.00, '2026-03-12', 'Käufer AG', 'Rechnungs-Nr R26001')                   # ein Ausgang
    tmp_db.insert_booking('2026-03-12', 119.00, booking_type='entry', recipient_client='Käufer AG', text='Rechnungs-Nr R26001')    # keine Bankbewegung
    add_payment(tmp_db, w['account'], 0.00, '2026-03-12', 'Käufer AG', 'Rechnungs-Nr R26001')                      # ohne Betrag
    assert overview_pairs(tmp_db) == [(w['first'], w['one']), (w['second'], w['two'])]


@pytest.mark.parametrize('status, offered', [('draft', False), ('finalized', False), ('sent', True), ('overdue', True), ('partial_payment', True),
                                              ('partial', True), ('paid', False), ('cancelled', False)])
def test_only_an_invoice_in_a_payable_status_is_offered(tmp_db, status, offered):
    account = bank_account(tmp_db)
    invoice_id = add_invoice(tmp_db, 'R26001', 119.00, customer_id=customer(tmp_db), status=status)
    payment = add_payment(tmp_db, account, 119.00, '2026-03-10', 'Käufer AG', 'R26001')
    assert (overview_pairs(tmp_db) == [(payment, invoice_id)]) is offered
    assert bool(tmp_db.suggest_invoices_for_booking(payment)) is offered
    assert bool(tmp_db.suggest_bookings_for_invoice(invoice_id)) is offered


def test_a_quote_a_credit_note_and_an_invoice_with_nothing_open_are_never_offered(tmp_db):
    account = bank_account(tmp_db)
    buyer = customer(tmp_db)
    quote = add_invoice(tmp_db, 'A26001', 119.00, customer_id=buyer, document_type='quote')
    storno = add_invoice(tmp_db, 'R26002', 119.00, customer_id=buyer)
    run_sql(tmp_db, "UPDATE Invoices SET InvoiceKind = 'storno' WHERE ID = ?", storno)
    settled = add_invoice(tmp_db, 'R26003', 119.00, customer_id=buyer, amount_due=0.00)
    for number in ('A26001', 'R26002', 'R26003'):
        add_payment(tmp_db, account, 119.00, '2026-03-10', 'Käufer AG', f'Zahlung {number}')
    assert overview_pairs(tmp_db) == []
    for invoice_id in (quote, storno, settled):
        assert tmp_db.suggest_bookings_for_invoice(invoice_id) == []
    assert tmp_db.suggest_bookings_for_invoice(10 ** 9) == [] and tmp_db.suggest_invoices_for_booking(10 ** 9) == []


def test_the_open_amount_is_the_one_the_mask_shows(tmp_db):
    """Der offene Betrag einer Teilzahlung ist ``AmountDue``, nicht das Brutto: Eine Zahlung über den Rest passt, eine über das Brutto nicht."""
    account = bank_account(tmp_db)
    invoice_id = add_invoice(tmp_db, 'R26001', 119.00, customer_id=customer(tmp_db), status='partial_payment', amount_due=69.00)
    add_payment(tmp_db, account, 119.00, '2026-03-10', 'Käufer AG')
    rest = add_payment(tmp_db, account, 69.00, '2026-03-11', 'Käufer AG')
    assert overview_pairs(tmp_db) == [(rest, invoice_id)]
    assert [(s.movement_id, s.reasons) for s, _m in tmp_db.suggest_bookings_for_invoice(invoice_id)] == [(rest, (REASON_AMOUNT, REASON_CUSTOMER))]


def test_the_limit_and_the_number_of_further_suggestions(tmp_db):
    account = bank_account(tmp_db)
    buyer = customer(tmp_db)
    for n in range(1, 26):
        add_invoice(tmp_db, f'R30{n:03d}', 100.00 + n, customer_id=buyer)
        add_payment(tmp_db, account, 100.00 + n, f'2026-03-{n:02d}', 'Käufer AG', f'Rechnungs-Nr R30{n:03d}')
    overview = tmp_db.suggest_invoice_payments()
    assert (len(overview.items), overview.more) == (20, 5)
    assert (len(tmp_db.suggest_invoice_payments(3).items), tmp_db.suggest_invoice_payments(3).more) == (3, 22)
    assert (tmp_db.suggest_invoice_payments(0).items, tmp_db.suggest_invoice_payments(0).more) == ([], 25)
    assert (len(tmp_db.suggest_invoice_payments(100).items), tmp_db.suggest_invoice_payments(100).more) == (25, 0)


def test_two_equal_invoices_and_two_equal_payments_of_a_customer_pair_by_date_and_say_so(tmp_db):
    account = bank_account(tmp_db)
    buyer = customer(tmp_db)
    january = add_invoice(tmp_db, 'R26001', 119.00, customer_id=buyer, date='2026-01-01')
    february = add_invoice(tmp_db, 'R26002', 119.00, customer_id=buyer, date='2026-02-01')
    late = add_payment(tmp_db, account, 119.00, '2026-02-10', 'Käufer AG')                           # beide ohne Nummer im Text
    early = add_payment(tmp_db, account, 119.00, '2026-01-10', 'Käufer AG')
    overview = tmp_db.suggest_invoice_payments()
    # beide Paare liegen 9 Tage auseinander: bei gleichem Abstand entscheidet die ID der Bewegung (die spätere Zahlung wurde zuerst angelegt)
    assert [(m['id'], i['id'], s.ambiguous) for s, m, i in overview.items] == [(late, february, True), (early, january, True)]
    assert [row[6] for row in box_rows(period_page(tmp_db))] == [f'{AMOUNT_AND_NAME} · {AMBIGUOUS_TEXT}'] * 2


# ── Die Datenbankschicht: die einzelne Bewegung und die einzelne Rechnung ────

def test_the_suggestions_for_one_payment_come_in_the_ranking_with_their_reasons(tmp_db):
    w = a_world(tmp_db)
    near = add_invoice(tmp_db, 'R26003', 119.00, name='Käufer AG', customer_id=w['buyer'], date='2026-03-09')           # Betrag + Kunde, ohne Nummer
    found = tmp_db.suggest_invoices_for_booking(w['first'])
    assert [(s.invoice_id, s.score, s.reasons) for s, _i in found] == [
        (w['one'], 180, (REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER)), (near, 80, (REASON_AMOUNT, REASON_CUSTOMER))]
    assert [i['number'] for _s, i in found] == ['R26001', 'R26003']
    assert [(s.movement_id, s.invoice_id) for s, _m in tmp_db.suggest_bookings_for_invoice(w['one'])] == [(w['first'], w['one'])]


def test_a_single_payment_has_suggestions_only_with_a_positive_free_rest(tmp_db):
    w = a_world(tmp_db)
    assert tmp_db.suggest_invoices_for_booking(w['first'])
    assert tmp_db.assign_invoice_payment(w['one'], w['first']).ok                        # voll zugeordnet: kein freier Rest
    assert tmp_db.suggest_invoices_for_booking(w['first']) == []
    outgoing = add_payment(tmp_db, w['account'], -119.00, '2026-03-12', 'Käufer AG', 'R26002')
    entry = tmp_db.insert_booking('2026-03-12', 238.00, booking_type='entry', recipient_client='Müller GmbH', text='R26002')
    assert tmp_db.suggest_invoices_for_booking(outgoing) == [] and tmp_db.suggest_invoices_for_booking(entry) == []


def test_the_free_rest_of_a_single_payment_follows_the_formula_after_a_partial_assignment(tmp_db):
    """Eine Bewegung über 300,00 € zahlt R26001 (119,00 €): Die Rechnung 2 über den Rest von 181,00 € passt zum freien Rest, die schon
    zugeordnete Rechnung 1 wird nie vorgeschlagen. Die Übersicht liest nur Bewegungen ohne Kind und ohne Zuordnung: Sie fehlt dort."""
    account = bank_account(tmp_db)
    buyer = customer(tmp_db)
    paid = add_invoice(tmp_db, 'R26001', 119.00, customer_id=buyer)
    open_one = add_invoice(tmp_db, 'R26002', 181.00, customer_id=buyer)
    payment = add_payment(tmp_db, account, 300.00, '2026-03-10', 'Käufer AG', 'Rechnungs-Nr R26001 und R26002')
    assert tmp_db.assign_invoice_payment(paid, payment).ok
    found = tmp_db.suggest_invoices_for_booking(payment)
    assert [(s.invoice_id, s.reasons) for s, _i in found] == [(open_one, (REASON_NUMBER, REASON_AMOUNT, REASON_CUSTOMER))]
    assert overview_pairs(tmp_db) == []


def test_a_pair_that_is_already_linked_is_not_suggested_although_both_still_have_a_rest(tmp_db):
    account = bank_account(tmp_db)
    buyer = customer(tmp_db)
    first = add_invoice(tmp_db, 'R26001', 119.00, customer_id=buyer)
    second = add_invoice(tmp_db, 'R26002', 250.00, customer_id=buyer)
    payment = add_payment(tmp_db, account, 300.00, '2026-03-10', 'Käufer AG', 'Rechnungs-Nr R26001')
    assert tmp_db.assign_invoice_payment(first, payment, 50.00).ok                      # 69,00 € der Rechnung offen, 250,00 € der Bewegung frei
    assert [(i, status) for i, status in [(first, tmp_db.get_invoice_by_id(first)[40])]] == [(first, 'partial_payment')]
    found = tmp_db.suggest_invoices_for_booking(payment)
    assert [(s.invoice_id, s.reasons) for s, _i in found] == [(second, (REASON_AMOUNT, REASON_CUSTOMER))]    # die Nummer von R26001 steht im Text – und zählt nicht


def test_a_suggestion_names_at_most_five_invoices_and_five_payments(tmp_db):
    account = bank_account(tmp_db)
    buyer = customer(tmp_db)
    for n in range(1, 9):
        add_invoice(tmp_db, f'K-{n}000', 119.00, customer_id=buyer, date=f'2026-03-0{n}')
    payment = add_payment(tmp_db, account, 119.00, '2026-03-10', 'Käufer AG')
    assert len(tmp_db.suggest_invoices_for_booking(payment)) == 5
    invoice_id = tmp_db.fetch_invoices()[0][0]
    for n in range(1, 9):
        add_payment(tmp_db, account, 119.00, f'2026-03-1{n}', 'Käufer AG')
    assert len(tmp_db.suggest_bookings_for_invoice(invoice_id)) == 5


def test_the_reads_change_nothing(tmp_db):
    w = a_world(tmp_db)
    before = state(tmp_db, 'Bookings', 'Invoices', 'InvoicePayments', 'InvoiceCorrections')
    tmp_db.suggest_invoice_payments()
    tmp_db.suggest_invoices_for_booking(w['first'])
    tmp_db.suggest_bookings_for_invoice(w['one'])
    assert state(tmp_db, 'Bookings', 'Invoices', 'InvoicePayments', 'InvoiceCorrections') == before


def test_a_value_that_is_no_number_is_no_candidate_and_no_error(tmp_db):
    w = a_world(tmp_db)
    broken_payment = add_payment(tmp_db, w['account'], 119.00, '2026-03-12', 'Käufer AG', 'R26001')
    broken_invoice = add_invoice(tmp_db, 'R26009', 119.00, customer_id=w['buyer'])
    odd_date = add_payment(tmp_db, w['account'], 119.00, 'gestern', 'Käufer AG', 'Zahlung R26009')
    run_sql(tmp_db, "UPDATE Bookings SET Amount = 'viel' WHERE ID = ?", broken_payment)
    run_sql(tmp_db, 'UPDATE Bookings SET Amount = 1190000.5 WHERE ID = ?', w['second'])
    run_sql(tmp_db, "UPDATE Invoices SET AmountDue = 'offen' WHERE ID = ?", w['two'])
    run_sql(tmp_db, "UPDATE Invoices SET InvoiceDate = 'neulich', BuyerCompany = '' WHERE ID = ?", broken_invoice)
    run_sql(tmp_db, 'UPDATE Bookings SET Text = NULL, RecipientClient = NULL WHERE ID = ?', w['first'])
    pairs = overview_pairs(tmp_db)                                                           # kein Fehler
    assert (odd_date, broken_invoice) in pairs and (w['second'], w['two']) not in pairs and (broken_payment, w['one']) not in pairs
    assert tmp_db.suggest_invoices_for_booking(broken_payment) == [] and tmp_db.suggest_invoices_for_booking(w['second']) == []
    assert tmp_db.suggest_bookings_for_invoice(w['two']) == []
    assert [s.movement_id for s, _m in tmp_db.suggest_bookings_for_invoice(broken_invoice)] == [odd_date]


def test_values_that_the_pages_read_anyway_are_no_problem_for_the_suggestions_on_them(tmp_db):
    """Ein unlesbares Datum, fehlender Text und Empfänger und ein leerer Firmenname stören weder die Vorschläge noch die Seiten."""
    w = a_world(tmp_db)
    odd_date = add_payment(tmp_db, w['account'], 119.00, 'gestern', 'Käufer AG', 'Zahlung R26009')
    odd_invoice = add_invoice(tmp_db, 'R26009', 119.00, customer_id=w['buyer'])
    run_sql(tmp_db, "UPDATE Invoices SET InvoiceDate = 'neulich', BuyerCompany = '' WHERE ID = ?", odd_invoice)
    run_sql(tmp_db, 'UPDATE Bookings SET Text = NULL, RecipientClient = NULL WHERE ID = ?', w['first'])
    assert (odd_date, odd_invoice) in overview_pairs(tmp_db)
    assert 'R26009' in [row[3] for row in box_rows(period_page(tmp_db))]
    PageTransactions(tmp_db, edit_transaction_id=w['first'])
    PageTransactions(tmp_db, edit_transaction_id=odd_date)
    assert str(odd_date) in str(candidate_buttons(PageInvoice(tmp_db, {}, odd_invoice)))


def test_the_candidates_are_the_newest_first_and_limited(tmp_db):
    account = bank_account(tmp_db)
    buyer = customer(tmp_db)
    for n in range(1, 4):
        add_invoice(tmp_db, f'R4{n:04d}', 100.00 + n, customer_id=buyer, date=f'2026-03-0{n}')
        add_payment(tmp_db, account, 100.00 + n, f'2026-03-1{n}', 'Käufer AG', f'R4{n:04d}')
    run_sql(tmp_db, "UPDATE Bookings SET DateBooking = '2019-01-01' WHERE ID = (SELECT MIN(ID) FROM Bookings WHERE BookingType = 'bank')")
    with closing(tmp_db._get_connection()) as conn:
        assert [m['date'] for m in tmp_db._unbooked_movements(conn)] == ['2026-03-13', '2026-03-12', '2019-01-01']
        assert [m['date'] for m in tmp_db._unbooked_movements(conn, 2)] == ['2026-03-13', '2026-03-12']                # die jüngsten zuerst
        assert [i['number'] for i in tmp_db._open_invoices(conn, 2)] == ['R40003', 'R40002']


# ── Nur was von der Bank kommt, zählt ────────────────────────────────────────

def lone_world(db, number=None, with_contact=False, recipient='Jemand Anderes', text='Spende'):
    """Ein Bankkonto, der Kunde „Käufer AG“ mit der offenen Rechnung R26001 über 119,00 € und ein Zahlungseingang über 119,00 € ohne Rechnungsnummer im
    Verwendungszweck, von ``recipient``. Die Bankzeile trägt, was eine Zuordnung dort zurücklässt: die Beleg-Nr. (``number``) und den Kontakt des Kunden
    (``with_contact``). Liefert ``account``, ``buyer``, ``invoice``, ``payment``."""
    account = bank_account(db)
    buyer = customer(db, 'Käufer AG')
    invoice_id = add_invoice(db, 'R26001', 119.00, name='Käufer AG', customer_id=buyer)
    payment = add_payment(db, account, 119.00, '2026-03-10', recipient, text, document_number=number, contact_id=buyer if with_contact else None)
    return {'account': account, 'buyer': buyer, 'invoice': invoice_id, 'payment': payment}


def bank_row_trace(db, booking_id):
    """Was die Bankzeile an Kontakt und Beleg-Nr. trägt: ``(Contact_ID, DocumentNumber)``."""
    with closing(sqlite3.connect(db.db_name)) as conn:
        return conn.execute('SELECT Contact_ID, DocumentNumber FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()


def assert_nowhere_suggested(db, w):
    """Weder die Übersicht (Daten und Kasten) noch die Maske der Bewegung noch die Rechnungsseite nennen einen Vorschlag zu dieser Bewegung und Rechnung."""
    assert overview_pairs(db) == [] and box_rows(period_page(db)) == []
    assert db.suggest_invoices_for_booking(w['payment']) == [] and db.suggest_bookings_for_invoice(w['invoice']) == []
    mask = PageTransactions(db, edit_transaction_id=w['payment'])
    assert 'invoiceSuggestion' not in mask and not any('Vorschlag' in text for _v, text, _s in option_labels(mask))
    assert [value for value, _t, _s in option_labels(mask)] == ['', str(w['invoice'])]            # die Rechnung steht weiter in der Auswahl – ohne Vorschlag
    page = PageInvoice(db, {}, w['invoice'])
    assert 'Betrag stimmt' not in page and 'Kunde passt' not in page and 'Rechnungsnummer im Verwendungszweck' not in page


@pytest.mark.parametrize('number, with_contact', [('R26001', False), ('Rechnung R26001 bezahlt', False), (None, True), ('R26001', True)],
                         ids=['Beleg-Nr.', 'Beleg-Nr. mit Text', 'Kontakt', 'beides'])
def test_the_document_number_and_the_contact_of_the_bank_row_are_no_evidence(tmp_db, number, with_contact):
    """Eine Zuordnung trägt Beleg-Nr. und Kontakt der Rechnung in die Bankzeile ein (``handlers._backfill_booking_skr``), und beides bleibt nach dem Entfernen
    stehen: Es stammt nicht von der Bank. Gleicher Betrag und eine Bankzeile, die die Nummer der Rechnung oder ihren Kunden als Kontakt trägt, sind ohne
    Namen und Nummer im Verwendungszweck und im Empfänger kein Vorschlag – auf keiner der Seiten."""
    w = lone_world(tmp_db, number, with_contact)
    assert bank_row_trace(tmp_db, w['payment']) == (w['buyer'] if with_contact else None, number)          # Voraussetzung: Die Zeile trägt, was der Test sagt
    assert_nowhere_suggested(tmp_db, w)


def test_the_document_number_and_the_contact_of_the_bank_row_add_nothing_to_a_suggestion(tmp_db):
    """Passt der Empfänger der Bankzeile zum Kunden, ist es ein Vorschlag – wegen Betrag und Name, mit genau diesen Punkten und Begründungen: Die Nummer in der
    Beleg-Nr. gibt weder die 100 Punkte noch die Begründung „Rechnungsnummer im Verwendungszweck“."""
    w = lone_world(tmp_db, 'R26001', True, recipient='Käufer AG')
    reasons = (REASON_AMOUNT, REASON_CUSTOMER)
    assert [(i['id'], s.score, s.reasons) for s, i in tmp_db.suggest_invoices_for_booking(w['payment'])] == [(w['invoice'], 80, reasons)]
    assert [(m['id'], s.score, s.reasons) for s, m in tmp_db.suggest_bookings_for_invoice(w['invoice'])] == [(w['payment'], 80, reasons)]
    assert [(m['id'], i['id'], s.score, s.reasons) for s, m, i in tmp_db.suggest_invoice_payments().items] == [(w['payment'], w['invoice'], 80, reasons)]
    assert [row[6] for row in box_rows(period_page(tmp_db))] == [AMOUNT_AND_NAME]
    page = PageTransactions(tmp_db, edit_transaction_id=w['payment'])
    assert f'Vorschlag: Rechnung R26001 ({AMOUNT_AND_NAME}) ' in page and NUMBER_AND_NAME not in page


# ── Der Fall der Prüfung: falsch zugeordnet und wieder gelöst ────────────────

def examination_world(db):
    """Kunde A („Käufer AG“) mit der offenen Rechnung 1 über 119,00 €, Kunde B („Müller GmbH“) mit der offenen Rechnung 2 über denselben Betrag und ein
    Zahlungseingang von „Käufer AG“ über 119,00 € ohne Rechnungsnummer im Verwendungszweck."""
    account = bank_account(db)
    buyer, miller = customer(db, 'Käufer AG'), customer(db, 'Müller GmbH')
    one = add_invoice(db, 'R26001', 119.00, name='Käufer AG', person='Kim Käufer', customer_id=buyer)
    two = add_invoice(db, 'R26002', 119.00, name='Müller GmbH', person='Mia Müller', customer_id=miller)
    payment = add_payment(db, account, 119.00, '2026-03-10', 'Käufer AG', 'Vielen Dank für den Auftrag')
    return {'account': account, 'buyer': buyer, 'miller': miller, 'one': one, 'two': two, 'payment': payment}


def surfaces(db, w):
    """Was die Datenbankschicht und die Seiten (Kasten, Maske der Bewegung, Seiten der beiden Rechnungen) vorschlagen – zum Vergleichen vorher/nachher."""
    mask = PageTransactions(db, edit_transaction_id=w['payment'])
    pages = {name: PageInvoice(db, {}, w[name]) for name in ('one', 'two')}
    return {
        'overview': [(m['id'], i['id'], s.score, s.reasons, s.ambiguous) for s, m, i in db.suggest_invoice_payments().items],
        'for the payment': [(i['id'], s.score, s.reasons) for s, i in db.suggest_invoices_for_booking(w['payment'])],
        'for invoice 1': [(m['id'], s.score, s.reasons) for s, m in db.suggest_bookings_for_invoice(w['one'])],
        'for invoice 2': [(m['id'], s.score, s.reasons) for s, m in db.suggest_bookings_for_invoice(w['two'])],
        'box': box_rows(period_page(db)),
        'mask options': [(value, text) for value, text, _selected in option_labels(mask)],
        'mask line': re.findall(r'<div class="hint" id="invoiceSuggestion">Vorschlag: Rechnung (\S+) \((.*?)\) ', mask),
        'invoice pages': {name: (candidate_buttons(page), re.findall(r'<td style="padding:6px 8px;">[^<]*<div class="hint">(.*?)</div>', page))
                          for name, page in pages.items()},
    }


def assert_the_first_invoice_is_the_suggestion(w, state):
    """Der Vorschlag ist Rechnung 1 (Betrag und Name, 80 Punkte, Begründung „Betrag stimmt · Kunde passt“) auf jeder Seite – und Rechnung 2 ist keiner."""
    reasons = (REASON_AMOUNT, REASON_CUSTOMER)
    assert state['overview'] == [(w['payment'], w['one'], 80, reasons, False)]
    assert state['for the payment'] == [(w['one'], 80, reasons)] and state['for invoice 1'] == [(w['payment'], 80, reasons)]
    assert state['for invoice 2'] == []
    assert [row[3] for row in state['box']] == ['R26001'] and [row[6] for row in state['box']] == [AMOUNT_AND_NAME]
    assert [value for value, text in state['mask options'] if 'Vorschlag' in text] == [str(w['one'])]
    assert state['mask line'] == [('R26001', AMOUNT_AND_NAME)]
    assert state['invoice pages']['one'][1] == [AMOUNT_AND_NAME] and state['invoice pages']['two'][1] == []


def test_a_wrong_assignment_that_was_removed_again_leaves_the_first_suggestion_over_the_routes(server_port, auto_login):
    """Der Fall der Prüfung: Eine Zahlung ohne Nummer im Text passt nach Betrag und Name zu Rechnung 1. Wird sie über die Route der Seite FALSCH Rechnung 2 zugeordnet
    und die Zuordnung wieder entfernt, bleiben in der Bankzeile der Kontakt und die Beleg-Nr. der Rechnung 2 stehen – und früher war Rechnung 2 danach der stärkste
    Vorschlag („Rechnungsnummer im Verwendungszweck“, 180 Punkte), obwohl sie dort nicht steht. Jetzt ist es wieder Rechnung 1 mit denselben Punkten und derselben
    Begründung wie vorher, auf allen Seiten."""
    db = auto_login.db
    w = examination_world(db)
    before = surfaces(db, w)
    assert_the_first_invoice_is_the_suggestion(w, before)
    headers = {'Origin': own_origin(server_port)}
    answer = post_json(server_port, '/invoice/link-payment', {'invoice_id': w['two'], 'transaction_id': w['payment']}, auto_login.csrf, headers=headers)
    assert (answer.status, json.loads(answer.text)) == (200, {'success': True})                  # der Nutzer ordnet die falsche Rechnung zu …
    assert overview_pairs(db) == [] and db.suggest_invoices_for_booking(w['payment']) == []       # … die Zahlung ist vergeben
    payment_id = db.get_invoice_payments(w['two'])[0][0]
    answer = post_json(server_port, '/invoice/delete-payment', {'payment_id': payment_id}, auto_login.csrf, headers=headers)
    assert (answer.status, json.loads(answer.text)) == (200, {'success': True})                  # … und löst sie wieder
    assert db.get_invoice_payments(w['two']) == [] and db.get_booking_allocations(w['payment']) == []
    assert bank_row_trace(db, w['payment']) == (w['miller'], 'R26002')                           # Voraussetzung: Die Zuordnung hat Kontakt und Beleg-Nr. hinterlassen
    after = surfaces(db, w)
    assert_the_first_invoice_is_the_suggestion(w, after)
    assert after == before


def test_a_wrong_assignment_through_the_mask_that_was_removed_again_leaves_the_first_suggestion(tmp_db):
    """Derselbe Fall über die Maske der Bankbewegung: Der Nutzer wählt dort die falsche Rechnung (Feld ``invoice_id``) und speichert."""
    w = examination_world(tmp_db)
    before = surfaces(tmp_db, w)
    assert_the_first_invoice_is_the_suggestion(w, before)
    post, _adopt = open_form(tmp_db, w['payment'])
    post['invoice_id'] = [str(w['two'])]
    assert handle_add_transaction(tmp_db, post) == (303, f'/invoice/edit?id={w["two"]}')
    assert overview_pairs(tmp_db) == []
    tmp_db.unassign_invoice_payment(tmp_db.get_invoice_payments(w['two'])[0][0])
    assert bank_row_trace(tmp_db, w['payment']) == (w['miller'], 'R26002')
    after = surfaces(tmp_db, w)
    assert_the_first_invoice_is_the_suggestion(w, after)
    assert after == before


def test_the_other_invoice_stays_no_suggestion_while_the_first_is_taken(tmp_db):
    """Ist Rechnung 1 schon zugeordnet, gibt es für diese Zahlung keinen Vorschlag mehr – Rechnung 2 wird nicht zum Ersatz: Name und Kunde gehören ihr nicht."""
    w = examination_world(tmp_db)
    assert tmp_db.assign_invoice_payment(w['one'], w['payment']).ok
    assert overview_pairs(tmp_db) == [] and tmp_db.suggest_invoices_for_booking(w['payment']) == []
    assert tmp_db.suggest_bookings_for_invoice(w['two']) == []


# ── Der Kundenname: die Firma, sonst der Name ────────────────────────────────

def test_the_customer_name_of_an_open_invoice_is_the_company_or_else_the_name(tmp_db):
    add_invoice(tmp_db, 'R26001', 119.00, name='Alpha Bau GmbH', person='Katharina Müller')            # Firma und Ansprechpartner
    add_invoice(tmp_db, 'R26002', 238.00, name='', person='Mia Schmidt')                                # ohne Firma
    add_invoice(tmp_db, 'R26003', 357.00, name='  Beta Handel AG  ', person='Otto Meier')               # Leerraum am Rand zählt nicht
    with closing(tmp_db._get_connection()) as conn:
        found = {i['number']: (i['customer_name'], i['label']) for i in tmp_db._open_invoices(conn)}
    assert found == {'R26001': ('Alpha Bau GmbH', 'Alpha Bau GmbH'), 'R26002': ('Mia Schmidt', 'Mia Schmidt'), 'R26003': ('Beta Handel AG', 'Beta Handel AG')}


def test_the_name_of_a_contact_person_beside_a_company_is_no_customer(tmp_db):
    """Der Vor- oder Nachname des Ansprechpartners ist kein Beweis für den Kunden: Ein Empfänger, der nur ihn teilt, ergibt kein „Kunde passt“ – mit einem Wort der Firma
    schon."""
    account = bank_account(tmp_db)
    company = add_invoice(tmp_db, 'R26001', 119.00, name='Alpha Bau GmbH', person='Katharina Müller')
    by_surname = add_payment(tmp_db, account, 119.00, '2026-03-10', 'Müller Elektro', 'Danke')
    by_first_name = add_payment(tmp_db, account, 119.00, '2026-03-11', 'Katharina Elektro', 'Danke')
    for payment in (by_surname, by_first_name):
        assert tmp_db.suggest_invoices_for_booking(payment) == []
    assert overview_pairs(tmp_db) == [] and tmp_db.suggest_bookings_for_invoice(company) == [] and box_rows(period_page(tmp_db)) == []
    by_company = add_payment(tmp_db, account, 119.00, '2026-03-12', 'Alpha Elektro', 'Danke')
    assert [(i['id'], s.reasons) for s, i in tmp_db.suggest_invoices_for_booking(by_company)] == [(company, (REASON_AMOUNT, REASON_CUSTOMER))]
    assert [m['id'] for _s, m in tmp_db.suggest_bookings_for_invoice(company)] == [by_company]
    assert overview_pairs(tmp_db) == [(by_company, company)]


def test_without_a_company_the_name_of_the_buyer_is_the_customer(tmp_db):
    account = bank_account(tmp_db)
    private = add_invoice(tmp_db, 'R26002', 238.00, name='', person='Mia Schmidt')
    payment = add_payment(tmp_db, account, 238.00, '2026-03-10', 'Schmidt Haustechnik', 'Danke')
    found = tmp_db.suggest_invoices_for_booking(payment)
    assert [(i['id'], i['label'], s.reasons) for s, i in found] == [(private, 'Mia Schmidt', (REASON_AMOUNT, REASON_CUSTOMER))]
    assert overview_pairs(tmp_db) == [(payment, private)]
    assert [row[4] for row in box_rows(period_page(tmp_db))] == ['Mia Schmidt']


# ── Nur Euro ─────────────────────────────────────────────────────────────────

def euro_world(db, invoice_currency, payment_currency):
    """Eine Rechnung R26001 über 119,00 € („Käufer AG“) und ein Zahlungseingang von „Käufer AG“ über 119,00 € mit der Nummer im Verwendungszweck, mit diesen
    Währungsangaben (roh gespeichert, ``None`` ist NULL)."""
    account = bank_account(db)
    buyer = customer(db)
    invoice_id = add_invoice(db, 'R26001', 119.00, customer_id=buyer)
    payment = add_payment(db, account, 119.00, '2026-03-10', 'Käufer AG', 'Rechnungs-Nr R26001')
    run_sql(db, 'UPDATE Invoices SET Currency = ? WHERE ID = ?', invoice_currency, invoice_id)
    run_sql(db, 'UPDATE Bookings SET Currency = ? WHERE ID = ?', payment_currency, payment)
    return {'account': account, 'buyer': buyer, 'invoice': invoice_id, 'payment': payment}


def suggested_on(db, w):
    """Ob das Paar auf jeder Fläche vorgeschlagen wird: Übersicht, Kasten, einzelne Bewegung, einzelne Rechnung, Maske (Zeile und Auswahl), Rechnungsseite."""
    mask = PageTransactions(db, edit_transaction_id=w['payment'])
    page = PageInvoice(db, {}, w['invoice'])
    return {
        'overview': overview_pairs(db) == [(w['payment'], w['invoice'])],
        'box': [row[3] for row in box_rows(period_page(db))] == ['R26001'],
        'for the payment': bool(db.suggest_invoices_for_booking(w['payment'])),
        'for the invoice': bool(db.suggest_bookings_for_invoice(w['invoice'])),
        'mask line': 'id="invoiceSuggestion"' in mask,
        'mask option': any('Vorschlag' in text for _v, text, _s in option_labels(mask)),
        'invoice page': f'<div class="hint">{NUMBER_AND_NAME}</div>' in page,
    }


@pytest.mark.parametrize('invoice_currency, payment_currency, expected', [
    ('EUR', 'EUR', True), ('eur', 'EUR', True), (' EUR ', 'EUR', True), ('Eur', 'EUR', True), ('', 'EUR', True), (None, 'EUR', True),
    ('EUR', 'eur', True), ('EUR', ' EUR ', True), ('EUR', '', True), ('EUR', None, True), (None, None, True),
    ('USD', 'EUR', False), ('usd', 'EUR', False), (' USD ', 'EUR', False), ('CHF', 'EUR', False), ('EURO', 'EUR', False), ('€', 'EUR', False),
    ('EUR', 'USD', False), ('EUR', 'usd', False), ('EUR', 'GBP', False), ('USD', 'USD', False),
])
def test_only_euro_invoices_and_payments_are_part_of_a_suggestion(tmp_db, invoice_currency, payment_currency, expected):
    """Eine Rechnung oder Bewegung in einer anderen Währung als leer/``EUR`` (ohne Rücksicht auf Schreibweise und Leerzeichen am Rand) ist nie Teil eines
    Vorschlags – auf keiner Fläche; das Paar mit gleichem Betrag, Namen und der Nummer im Text wäre sonst ein 180-Punkte-Vorschlag."""
    w = euro_world(tmp_db, invoice_currency, payment_currency)
    found = suggested_on(tmp_db, w)
    assert set(found.values()) == {expected}, found


@pytest.mark.parametrize('invoice_currency, payment_currency', [('USD', 'EUR'), ('EUR', 'USD')], ids=['Rechnung-USD', 'Bewegung-USD'])
def test_a_pair_in_a_foreign_currency_stays_in_the_old_lists_of_the_pages_just_without_a_suggestion(tmp_db, invoice_currency, payment_currency):
    """Die Auswahl der Maske und die Liste der Rechnungsseite zeigen weiter alles, was offen ist – nur ohne Vorschlag und ohne Begründung."""
    w = euro_world(tmp_db, invoice_currency, payment_currency)
    mask = PageTransactions(tmp_db, edit_transaction_id=w['payment'])
    assert [value for value, _t, _s in option_labels(mask)] == ['', str(w['invoice'])]
    assert candidate_buttons(PageInvoice(tmp_db, {}, w['invoice'])) == [w['payment']]


# ── Bewegungen ohne Kind und ohne Zuordnung: EINE Abfrage ────────────────────

def unbooked_ids(db):
    with closing(db._get_connection()) as conn:
        return [m['id'] for m in db._unbooked_movements(conn)]


def mixed_movements(db):
    """Eine Bestandsprobe: Bewegungen mit und ohne Kind, mit und ohne Zuordnung (eine ohne Sätze, wie der Import der WISO-Datenbank sie schreibt), mit
    Betrag ≤ 0, mit einem Betrag, der keine ganze Zahl ist, und eine Buchung, die keine Bankbewegung ist. Liefert die IDs je Art."""
    account = bank_account(db)
    invoice_id = add_invoice(db, 'R26001', 119.00, customer_id=customer(db))
    other_invoice = add_invoice(db, 'R26002', 59.00, name='Müller GmbH', customer_id=customer(db, 'Müller GmbH'))
    ids = {'free': add_payment(db, account, 10.00, '2026-03-01'), 'free too': add_payment(db, account, 11.00, '2026-03-05')}
    ids['with child'] = add_payment(db, account, 20.00, '2026-03-02')
    db.insert_booking('2026-03-02', 20.00, booking_type='entry', parent_booking_id=ids['with child'],
                      coa_id=db.get_coa_id_by_account_number(4400), counter_coa_id=db.get_coa_id_by_account_number(1800))
    ids['assigned'] = add_payment(db, account, 119.00, '2026-03-03')
    run_sql(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)', invoice_id, ids['assigned'], 1190000, '2026-03-03')
    ids['assigned elsewhere'] = add_payment(db, account, 59.00, '2026-03-04')
    run_sql(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)', other_invoice, ids['assigned elsewhere'], 590000, '2026-03-04')
    ids['child and assignment'] = add_payment(db, account, 30.00, '2026-03-06')
    db.insert_booking('2026-03-06', 30.00, booking_type='entry', parent_booking_id=ids['child and assignment'],
                      coa_id=db.get_coa_id_by_account_number(4400), counter_coa_id=db.get_coa_id_by_account_number(1800))
    run_sql(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)', invoice_id, ids['child and assignment'], 300000, '2026-03-06')
    ids['outgoing'] = add_payment(db, account, -12.00, '2026-03-07')
    ids['zero'] = add_payment(db, account, 0.00, '2026-03-08')
    ids['text amount'] = add_payment(db, account, 13.00, '2026-03-09')
    run_sql(db, "UPDATE Bookings SET Amount = 'viel' WHERE ID = ?", ids['text amount'])
    ids['fraction'] = add_payment(db, account, 14.00, '2026-03-10')
    run_sql(db, 'UPDATE Bookings SET Amount = 140000.5 WHERE ID = ?', ids['fraction'])
    ids['no bank row'] = db.insert_booking('2026-03-11', 15.00, booking_type='entry', recipient_client='Käufer AG')
    return ids


def test_a_movement_with_a_child_or_an_assignment_is_not_unbooked(tmp_db):
    """Bewegung mit Kind fehlt, Bewegung mit Zuordnung fehlt (auch eine ohne Sätze, auch eine an einer anderen Rechnung), eine ohne beides ist da – jüngste zuerst."""
    ids = mixed_movements(tmp_db)
    assert unbooked_ids(tmp_db) == [ids['free too'], ids['free']]
    for name in ('with child', 'assigned', 'assigned elsewhere', 'child and assignment', 'outgoing', 'zero', 'text amount', 'fraction', 'no bank row'):
        assert ids[name] not in unbooked_ids(tmp_db), name


PREVIOUS_UNBOOKED_MOVEMENTS = '''
    SELECT b.ID, b.DateBooking, b.RecipientClient, b.Text, b.DocumentNumber, b.Contact_ID, b.Amount, b.Amount
    FROM Bookings b
    WHERE b.BookingType = 'bank' AND typeof(b.Amount) = 'integer' AND b.Amount > 0
      AND CASE WHEN EXISTS (SELECT 1 FROM Bookings k WHERE k.ParentBooking_ID = b.ID) THEN 0
               ELSE NOT EXISTS (SELECT 1 FROM InvoicePayments ip WHERE ip.BookingID = b.ID) END
    ORDER BY b.DateBooking DESC, b.ID DESC
    LIMIT ?'''


def test_the_single_read_of_the_assignments_gives_the_rows_of_the_version_before(tmp_db):
    """Die Zuordnungen werden einmal gelesen (``NOT IN``) statt je Bewegung (``CASE`` mit ``NOT EXISTS``): in Euro dieselben Zeilen in derselben Reihenfolge, auch mit
    dem ``LIMIT``."""
    mixed_movements(tmp_db)
    with closing(sqlite3.connect(tmp_db.db_name)) as conn:
        for limit in (500, 1):
            assert conn.execute(_UNBOOKED_MOVEMENTS, (limit,)).fetchall() == conn.execute(PREVIOUS_UNBOOKED_MOVEMENTS, (limit,)).fetchall()
        assert len(conn.execute(_UNBOOKED_MOVEMENTS, (500,)).fetchall()) == 2


def test_the_assignments_are_read_once_and_not_once_per_movement(tmp_db):
    """``InvoicePayments`` hat keinen Index auf ``BookingID``: Eine Unterabfrage je Bewegung läse die ganze Tabelle für jede unkontierte Bewegung neu (gemessen an 20 000
    Eingängen und 3 700 Zuordnungen: 1,2 s statt 28 ms). Gezählt werden die Schritte der Datenbank, nicht die Zeit: Sie wachsen mit der Zahl der Zeilen, nicht mit ihrem
    Produkt (3 000 Bewegungen und 1 800 Zuordnungen: gemessen rund 80 000 Schritte, die Fassung davor gut 15 Millionen; die Grenze liegt bei 1,5 Millionen)."""
    account = bank_account(tmp_db)
    invoice_id = add_invoice(tmp_db, 'R26001', 119.00, customer_id=customer(tmp_db))
    with closing(sqlite3.connect(tmp_db.db_name)) as conn:
        conn.executemany('INSERT INTO Bookings (DateBooking, Amount, BookingType, Account_ID) VALUES (?, ?, ?, ?)',
                         [(f'2026-03-{n % 28 + 1:02d}', 10000 + n, 'bank', account) for n in range(3000)])
        first = conn.execute("SELECT MIN(ID) FROM Bookings WHERE BookingType = 'bank'").fetchone()[0]
        conn.executemany('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, ?, ?, ?)',
                         [(invoice_id, first + n, 1, '2026-03-01') for n in range(1800)])
        conn.commit()
        ticks = []

        def tick():
            ticks.append(1)
            return 0                                              # 0 = weiterrechnen
        conn.set_progress_handler(tick, 1000)                     # alle 1 000 Schritte der Datenbank einmal
        rows = conn.execute(_UNBOOKED_MOVEMENTS, (MAX_MOVEMENTS,)).fetchall()
        conn.set_progress_handler(None, 0)
    assert len(rows) == MAX_MOVEMENTS and all(first + 1800 <= row[0] for row in rows)       # die ersten 1 800 sind vergeben
    assert len(ticks) * 1000 < 1_500_000, f'{len(ticks) * 1000} Schritte'


def test_a_null_in_the_assignments_cannot_empty_the_list():
    """Das ``NOT IN`` einer Unterabfrage mit einem NULL ergibt für jede Zeile NULL: Die Liste wäre leer. Die Abfrage nimmt NULL deshalb aus der Unterabfrage. Auf der Tabelle der
    App kann es kein NULL geben (``BookingID`` ist ``NOT NULL``, siehe der nächste Test) – geprüft wird die Abfrage selbst, auf einer kleinen Tabelle ohne die Sperre."""
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.executescript('''
            CREATE TABLE Bookings (ID INTEGER PRIMARY KEY, DateBooking TEXT, RecipientClient TEXT, Text TEXT, DocumentNumber TEXT, Contact_ID INTEGER,
                                   Amount, BookingType TEXT, Currency TEXT, ParentBooking_ID INTEGER);
            CREATE TABLE InvoicePayments (ID INTEGER PRIMARY KEY, InvoiceID INTEGER, BookingID INTEGER);
            INSERT INTO Bookings (ID, DateBooking, Amount, BookingType) VALUES (1, '2026-03-01', 100, 'bank'), (2, '2026-03-02', 200, 'bank'), (3, '2026-03-03', 300, 'bank');
            INSERT INTO InvoicePayments (InvoiceID, BookingID) VALUES (1, 1), (1, NULL), (2, NULL);''')
        assert [row[0] for row in conn.execute(_UNBOOKED_MOVEMENTS, (500,))] == [3, 2]


def test_the_table_of_the_app_has_no_null_booking_id_in_its_assignments(tmp_db):
    """Der Grund, warum es keine Zeile mit NULL zu prüfen gibt: Das Schema verlangt ``BookingID`` (``NOT NULL``) – die Zeile ließe sich nicht anlegen."""
    invoice_id = add_invoice(tmp_db, 'R26001', 119.00, customer_id=customer(tmp_db))
    with closing(sqlite3.connect(tmp_db.db_name)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate) VALUES (?, NULL, 1, ?)', (invoice_id, '2026-03-01'))


# ── Der Kasten auf der Seite Buchungen ───────────────────────────────────────

def test_without_suggestions_there_is_no_box(tmp_db):
    heading = '<h3>Zahlungseingänge zu offenen Rechnungen</h3>'
    page = period_page(tmp_db)
    assert box_of(page).strip() == '' and heading not in page
    bank_account(tmp_db)
    add_invoice(tmp_db, 'R26001', 119.00, customer_id=customer(tmp_db))                          # eine Rechnung, aber keine Zahlung dazu
    page = period_page(tmp_db)
    assert box_of(page).strip() == '' and heading not in page


def test_the_box_shows_one_row_per_suggestion_with_the_planned_cells(tmp_db):
    w = a_world(tmp_db)
    page = period_page(tmp_db)
    assert box_rows(page) == [
        ['2026-03-10', 'Käufer AG', '119,00 €', 'R26001', 'Käufer AG', '119,00 €', NUMBER_AND_NAME, 'Zuordnen'],
        ['2026-03-11', 'Müller GmbH', '238,00 €', 'R26002', 'Müller GmbH', '238,00 €', NUMBER_AND_NAME, 'Zuordnen']]
    box = box_of(page)
    assert table_rows(box)[0] == ['Datum', 'Auftraggeber', 'Betrag (frei)', 'Rechnung', 'Kunde', 'offen', 'Begründung', '']
    assert '<h3>Zahlungseingänge zu offenen Rechnungen</h3>' in box and 'Vorschläge aus allen Zeiträumen' in box
    assert f'<a href="/invoice/edit?id={w["one"]}">R26001</a>' in box                            # die Nummer ist ein Link auf die Rechnung
    assert f'onclick="payAssign({w["one"]}, {w["first"]}, this)"' in box and f'onclick="payAssign({w["two"]}, {w["second"]}, this)"' in box
    assert '… und' not in box and 'selected' not in box


def test_the_box_stands_left_above_the_list_and_not_in_the_area_that_the_mask_replaces(tmp_db):
    w = a_world(tmp_db)
    page = period_page(tmp_db)
    box = page.index('<div id="paySuggest">')
    assert page.index('<div class="gridLeftCol"') < page.index('id="importPreview"') < box < page.index("<table id='transactionsTable'>")
    assert box > page.index('<!-- Ende gridRightCol -->')                                          # nicht im rechten Bereich (openEditForm)
    assert page.count('id="paySuggest"') == 1
    edit = PageTransactions(tmp_db, edit_transaction_id=w['first'])
    assert 'id="paySuggest"' not in edit.split('<!-- Ende gridRightCol -->')[0]                    # die Maske, die openEditForm einsetzt, trägt ihn nie


def test_at_most_twenty_rows_and_the_rest_as_a_count(tmp_db):
    account = bank_account(tmp_db)
    buyer = customer(tmp_db)
    for n in range(1, 24):
        add_invoice(tmp_db, f'R30{n:03d}', 100.00 + n, customer_id=buyer)
        add_payment(tmp_db, account, 100.00 + n, f'2026-03-{n:02d}', 'Käufer AG', f'R30{n:03d}')
    box = box_of(period_page(tmp_db))
    assert len(table_rows(box)) - 1 == 20 and '<p class="hint">… und 3 weitere</p>' in box


def test_every_value_in_the_box_is_masked(tmp_db):
    """Ein Auftraggeber, ein Kunde und eine Rechnungsnummer mit Markup bleiben Text; der Aufbau der Seite ändert sich nicht."""
    def build(db, name, recipient):
        account = bank_account(db)
        buyer = customer(db, 'Käufer AG')
        invoice_id = add_invoice(db, 'R26001', 119.00, name=name, person=name, customer_id=buyer)
        add_payment(db, account, 119.00, '2026-03-10', recipient, 'Rechnungs-Nr R26001')
        return period_page(db), invoice_id
    hostile_page, invoice_id = build(tmp_db, f'Käufer {HOSTILE}', f'Käufer {HOSTILE}')
    benign_page, _ = build(Database(db_name=tmp_db.db_name + '.benign'), 'Käufer AG', 'Käufer AG')
    assert_same_structure(hostile_page, benign_page)
    assert '<img src=x' not in hostile_page and HOSTILE not in hostile_page and Dom(hostile_page).all('img') == []
    assert [row[1] for row in box_rows(hostile_page)] == [f'Käufer {HOSTILE}'] and [row[4] for row in box_rows(hostile_page)] == [f'Käufer {HOSTILE}']
    assert f'<a href="/invoice/edit?id={invoice_id}">R26001</a>' in hostile_page


def test_the_box_refreshes_with_the_table_after_an_import(tmp_db):
    page = period_page(tmp_db)
    refresh = page[page.index('function refreshTable()'):page.index('function openEditForm(')]
    assert "doc.getElementById('paySuggest')" in refresh and "document.getElementById('paySuggest')" in refresh
    assert 'curSuggest.innerHTML = freshSuggest.innerHTML' in refresh and 'if (freshSuggest && curSuggest)' in refresh      # nur, wenn beide da sind


FUNCTIONS = ['payReload', 'payLock', 'payLink', 'payAssign', 'payAssignRows', 'payAssignAll']       # was das Skript des Kastens an Funktionen führt (2.9.2: Sammelknopf mit Hilfsfunktionen)


def test_the_script_of_the_box_has_only_var_and_function_and_nothing_that_sets_markup():
    """Wie bei den übrigen Skripten der Seiten nur ``var`` und ``function`` (kein ``let``/``const``) und außerhalb der Funktionen nichts Globales; in den Funktionen gibt es
    lokale ``var`` (die Adresse für das Neuladen ohne ``err``, die Knöpfe, die Zeilen) und keine verschachtelte benannte Funktion."""
    declarations = re.findall(r'^[ \t]*(var|let|const|function)\s+(\w+)', PAY_SUGGEST_JS, re.M)
    assert re.findall(r'^(var|let|const|function)\s+(\w+)', PAY_SUGGEST_JS, re.M) == [('function', name) for name in FUNCTIONS]       # nichts steht außerhalb der Funktionen
    assert [name for kind, name in declarations if kind == 'function'] == FUNCTIONS                  # innen nur ``var``: keine verschachtelte benannte Funktion …
    assert {kind for kind, _name in declarations} <= {'var', 'function'}                            # … und nie ``let`` oder ``const``
    assert 'innerHTML' not in PAY_SUGGEST_JS and 'insertAdjacentHTML' not in PAY_SUGGEST_JS and 'document.write' not in PAY_SUGGEST_JS
    assert 'outerHTML' not in PAY_SUGGEST_JS and 'eval(' not in PAY_SUGGEST_JS


def test_the_page_carries_the_failure_sentence_only_as_json_in_its_own_script_block(tmp_db):
    page = period_page(tmp_db)
    assert f'var PAY_FAILED = {script_json(PAY_FAILED)};' in page and PAY_FAILED == 'Die Zahlung konnte nicht zugeordnet werden.'
    scripts = Dom(page).scripts
    assert len([text for text in scripts if 'function renderCard(' in text]) == 1        # das Skript der Ablage bleibt eines
    assert len([text for text in scripts if 'function payAssign(' in text]) == 1 and 'function renderCard(' not in [t for t in scripts if 'payAssign' in t][0]


# ── Das Skript des Kastens (Node, ohne Browser) ──────────────────────────────

HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const calls = {fetch: [], msg: [], reload: 0, navigations: []};
const button = {disabled: false};
const scenario = input.scenario;
// window.location wie im Browser: Pfad und Query der Seite; ein Zuweisen von href ist eine Navigation (hier nur festgehalten), reload() ein Neuladen derselben Adresse
const location = {pathname: scenario.pathname, search: scenario.search, reload: () => { calls.reload += 1; }};
Object.defineProperty(location, 'href', {set: value => { calls.navigations.push(value); }});
const context = {
  window: {location},
  URLSearchParams,
  fetch: (url, init) => {
    calls.fetch.push({url, method: init.method, headers: init.headers, body: init.body, disabledWhileSending: button.disabled});
    if (scenario.reject) return Promise.reject(new Error('Netz'));
    return Promise.resolve({json: () => scenario.badJson ? Promise.reject(new Error('kein JSON')) : Promise.resolve(scenario.data)});
  },
  appMsg: (text, type) => calls.msg.push([text, type]),
  JSON, Promise,
};
vm.createContext(context);
vm.runInContext(input.source, context);
context.payAssign(12, 34, button);
setTimeout(() => { calls.disabledAtEnd = button.disabled; console.log(JSON.stringify(calls)); }, 30);
"""

PAGE = {'pathname': '/transactions', 'search': '?from=2026-01-01&to=2026-12-31'}            # die Adresse der Seite *Buchungen*, auf der der Kasten steht


def run_script(scenario):
    source = f'var PAY_FAILED = {script_json(PAY_FAILED)};\n{PAY_SUGGEST_JS}'
    result = subprocess.run([NODE, '-e', HARNESS], input=json.dumps({'source': source, 'scenario': {**PAGE, **scenario}}), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@NO_NODE
def test_assign_calls_the_existing_route_with_both_ids_and_no_amount_and_loads_the_page_again_on_success():
    calls = run_script({'data': {'success': True}})
    assert len(calls['fetch']) == 1
    fetched = calls['fetch'][0]
    assert (fetched['url'], fetched['method']) == ('/invoice/link-payment', 'POST')
    assert fetched['headers'] == {'Content-Type': 'application/json'} and json.loads(fetched['body']) == {'invoice_id': 12, 'transaction_id': 34}
    assert fetched['disabledWhileSending'] is True                 # ein Doppelklick ordnet nicht doppelt zu
    assert calls['navigations'] == ['/transactions?from=2026-01-01&to=2026-12-31'] and calls['msg'] == [] and calls['disabledAtEnd'] is True
    assert calls['reload'] == 0                                    # nicht ``reload()``: Das behielte eine alte Meldung (``err``) in der Adresse


@NO_NODE
@pytest.mark.parametrize('search, target', [
    ('', '/transactions'),                                                                               # nichts in der Adresse: der Pfad allein
    ('?from=2026-01-01&to=2026-12-31', '/transactions?from=2026-01-01&to=2026-12-31'),                  # ohne ``err`` bleibt alles
    ('?from=2026-01-01&to=2026-12-31&err=Alte+Meldung', '/transactions?from=2026-01-01&to=2026-12-31'),   # eine alte Meldung fällt weg, der Zeitraum bleibt
    ('?err=Die+Zahlung+konnte+nicht+zugeordnet+werden.', '/transactions'),                              # nur ``err``: keine Query, kein ``?``
    ('?err=a&from=2026-01-01&err=b', '/transactions?from=2026-01-01'),                                  # jedes ``err``
    ('?acct=1&acct=2&err=x&to=2026-12-31', '/transactions?acct=1&acct=2&to=2026-12-31'),                # eine Liste (wiederholter Schlüssel) bleibt, auch die Reihenfolge
    ('?error=x&errx=1&xerr=2&from=2026-01-01', '/transactions?error=x&errx=1&xerr=2&from=2026-01-01'),   # nur der Schlüssel ``err`` selbst, nicht ähnliche
    ('?note=%C3%A4%C3%B6&err=x', '/transactions?note=%C3%A4%C3%B6'),                                    # Umlaute bleiben prozentkodiert
], ids=['ohne-Adresse', 'ohne-err', 'mit-err', 'nur-err', 'zwei-err', 'Liste', 'ähnliche-Schlüssel', 'Umlaute'])
def test_after_success_the_page_loads_again_at_its_path_and_query_without_err(search, target):
    calls = run_script({'data': {'success': True}, 'search': search})
    assert calls['navigations'] == [target] and calls['reload'] == 0 and calls['msg'] == []


@NO_NODE
def test_after_success_the_path_of_the_page_is_kept():
    calls = run_script({'data': {'success': True}, 'pathname': '/transactions/', 'search': '?err=x'})
    assert calls['navigations'] == ['/transactions/']


@NO_NODE
def test_assign_shows_the_reason_of_the_answer_as_an_error_and_keeps_the_row_usable():
    calls = run_script({'data': {'success': False, 'error': 'Die Rechnung ist nicht (mehr) zahlbar.'}})
    assert calls['msg'] == [['Die Rechnung ist nicht (mehr) zahlbar.', 'error']] and calls['navigations'] == [] and calls['reload'] == 0
    assert calls['disabledAtEnd'] is False
    calls = run_script({'data': {'success': False}})
    assert calls['msg'] == [[PAY_FAILED, 'error']] and calls['navigations'] == [] and calls['reload'] == 0 and calls['disabledAtEnd'] is False


@NO_NODE
@pytest.mark.parametrize('scenario', [{'reject': True}, {'badJson': True}], ids=['kein-Netz', 'kein-JSON'])
def test_assign_names_a_failure_without_an_answer_with_the_fixed_sentence(scenario):
    calls = run_script(scenario)
    assert calls['msg'] == [[PAY_FAILED, 'error']] and calls['navigations'] == [] and calls['reload'] == 0 and calls['disabledAtEnd'] is False


@NO_NODE
def test_the_script_of_the_box_parses_in_node(tmp_path):
    source = tmp_path / 'pay_assign.js'
    source.write_text(PAY_SUGGEST_JS, encoding='utf-8')
    done = subprocess.run([NODE, '--check', str(source)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


# ═════════════════════════════════════════════════════════════════════════════
# „Alle eindeutigen zuordnen“ und das Abzeichen „Rechnung?“ (2.9.2)
# ═════════════════════════════════════════════════════════════════════════════

ALL_BUTTON = re.compile(r'<button type="button" class="coloredButton btn-sm bg-green" onclick="payAssignAll\(this\)">(.*?)</button>')
CERTAIN_SENTENCE = 'Eindeutig: Rechnungsnummer im Verwendungszweck und Betrag stimmt.'
BADGE = re.compile(r"<span class='badge bg-blue' title='([^']*)'>Rechnung\?</span>")
OPEN_BADGE = "<span class='badge bg-orange' title='Noch nicht verbucht'>offen</span>"


def box_row_tags(page):
    """Die Attribute der ``<tr>`` der Zeilen des Kastens (ohne Kopfzeile), in der Reihenfolge der Zeilen."""
    return [attrs for tag, attrs in Dom(box_of(page)).tags if tag == 'tr' and 'data-invoice' in attrs]


def list_rows(page):
    """Die Zeilen der Liste (``#transactionsTable``) als ``{ID der Bankbewegung: HTML der Zeile}`` – die ID steht im Aufruf ``openEditForm(<ID>)``."""
    table = page.split("<table id='transactionsTable'>", 1)[1].split('</table>', 1)[0]
    rows = {}
    for chunk in re.findall(r"<tr class='transaction-row.*?</tr>", table, re.S):
        found = re.search(r'openEditForm\((\d+)\)', chunk)
        if found:
            rows[int(found.group(1))] = chunk
    return rows


def badge_titles(row):
    return [unescape(title) for title in BADGE.findall(row)]


def mixed_world(db):
    """Zwei eindeutige Zeilen (Nummer und Betrag) und vier, die es nicht sind: Betrag und Kunde ohne Nummer, die Nummer mit abweichendem Betrag, der Betrag nach Skonto mit der Nummer,
    zwei gleich hohe Zahlungen mit derselben Nummer (mehrdeutig). Liefert die Rechnungen als Wörterbuch."""
    account = bank_account(db)
    ids = {}
    for key, number, gross, name, extra in (('number', 'R26001', 119.00, 'Käufer AG', {}), ('second', 'R26002', 238.00, 'Müller GmbH', {}),
                                            ('amount', 'R26003', 357.00, 'Schmidt GmbH', {}), ('partial', 'R26004', 476.00, 'Bauer KG', {}),
                                            ('skonto', 'R26005', 595.00, 'Weber AG', TERMS), ('twin', 'R26006', 714.00, 'Fischer OHG', {})):
        ids[key] = add_invoice(db, number, gross, name=name, person='Pat Person', customer_id=customer(db, name), **extra)
    add_payment(db, account, 119.00, '2026-03-10', 'Käufer AG', 'Rechnung R26001')
    add_payment(db, account, 238.00, '2026-03-10', 'Müller GmbH', 'Rechnung R26002')
    add_payment(db, account, 357.00, '2026-03-10', 'Schmidt GmbH', 'Danke')                         # nur Betrag und Kunde
    add_payment(db, account, 100.00, '2026-03-10', 'Bauer KG', 'Rechnung R26004')                   # die Nummer, aber ein anderer Betrag
    add_payment(db, account, 583.10, '2026-03-10', 'Weber AG', 'Rechnung R26005')                   # die Nummer und der Betrag nach Abzug des Skontos
    add_payment(db, account, 714.00, '2026-03-10', 'Fischer OHG', 'Rechnung R26006')               # zwei Zahlungen für dieselbe Rechnung
    add_payment(db, account, 714.00, '2026-03-11', 'Fischer OHG', 'Rechnung R26006')
    return ids


def test_every_row_of_the_box_carries_the_ids_as_numbers_and_a_certain_one_the_mark(tmp_db):
    w = a_world(tmp_db)
    assert box_row_tags(period_page(tmp_db)) == [
        {'data-invoice': str(w['one']), 'data-booking': str(w['first']), 'data-amount': '119.00', 'data-certain': '1'},
        {'data-invoice': str(w['two']), 'data-booking': str(w['second']), 'data-amount': '238.00', 'data-certain': '1'}]


def test_with_two_certain_rows_the_box_offers_the_button_for_all_of_them_and_the_sentence_in_the_hint(tmp_db):
    a_world(tmp_db)
    box = box_of(period_page(tmp_db))
    assert [unescape(label) for label in ALL_BUTTON.findall(box)] == ['Alle eindeutigen zuordnen (2)']
    assert box.index('payAssignAll(this)') < box.index('<table')                                   # über der Tabelle
    hint = unescape(re.search(r'<p class="hint">(.*?)</p>', box, re.S).group(1))
    assert hint == f'Vorschläge aus allen Zeiträumen, nicht nur aus dem gewählten. Zugeordnet wird erst mit „Zuordnen“. {CERTAIN_SENTENCE}'


def test_with_one_certain_row_there_is_no_button_and_no_sentence_but_the_row_keeps_its_mark(tmp_db):
    w = a_world(tmp_db)
    run_sql(tmp_db, 'UPDATE Bookings SET Text = ? WHERE ID = ?', 'Danke', w['second'])             # ohne die Nummer: Betrag und Kunde, nicht eindeutig
    page = period_page(tmp_db)
    assert [tag.get('data-certain') for tag in box_row_tags(page)] == ['1', None]
    box = box_of(page)
    assert 'payAssignAll' not in box and 'Alle eindeutigen' not in box and CERTAIN_SENTENCE not in box


def test_without_a_certain_row_there_is_no_button_and_no_mark(tmp_db):
    w = a_world(tmp_db)
    for payment in (w['first'], w['second']):
        run_sql(tmp_db, 'UPDATE Bookings SET Text = ? WHERE ID = ?', 'Danke', payment)
    page = period_page(tmp_db)
    assert [tag.get('data-certain') for tag in box_row_tags(page)] == [None, None] and len(box_rows(page)) == 2
    assert 'payAssignAll' not in box_of(page) and CERTAIN_SENTENCE not in box_of(page)


def test_only_the_number_with_the_amount_is_certain_never_an_ambiguous_a_discount_or_an_amount_only_row(tmp_db):
    ids = mixed_world(tmp_db)
    page = period_page(tmp_db)
    tags = {int(tag['data-invoice']): tag for tag in box_row_tags(page)}
    assert set(tags) == set(ids.values())                                                           # alle sechs sind Vorschläge …
    assert {key for key, invoice_id in ids.items() if tags[invoice_id].get('data-certain') == '1'} == {'number', 'second'}      # … eindeutig sind nur zwei
    box = box_of(page)
    assert [unescape(label) for label in ALL_BUTTON.findall(box)] == ['Alle eindeutigen zuordnen (2)']
    assert 'mehrere möglich' in box and 'Mit Skonto zuordnen' in box and 'Betrag weicht ab' in box   # die Zeilen, die nicht eindeutig sind, bleiben einzeln zu entscheiden


def test_the_button_counts_the_certain_rows_of_the_box_not_those_behind_the_limit(tmp_db):
    account, buyer = bank_account(tmp_db), customer(tmp_db)
    for n in range(1, 24):
        add_invoice(tmp_db, f'R30{n:03d}', 100.00 + n, customer_id=buyer)
        add_payment(tmp_db, account, 100.00 + n, f'2026-03-{n:02d}', 'Käufer AG', f'R30{n:03d}')
    page = period_page(tmp_db)
    assert len(box_row_tags(page)) == 20 and all(tag.get('data-certain') == '1' for tag in box_row_tags(page))
    assert [unescape(label) for label in ALL_BUTTON.findall(box_of(page))] == ['Alle eindeutigen zuordnen (20)'] and '… und 3 weitere' in box_of(page)


def test_a_row_carries_the_free_amount_of_the_movement_in_the_plain_format_the_route_reads(tmp_db):
    """``data-amount``: der Betrag, den der Kasten zeigt – der freie Rest der Bewegung, nicht der offene Betrag der Rechnung (hier der Betrag nach Skonto); ``payAssignAll`` schickt ihn als ``amount_paid``."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    (tag,) = box_row_tags(period_page(tmp_db))
    assert (tag['data-invoice'], tag['data-booking'], tag['data-amount']) == (str(invoice_id), str(payment), '116.62') and 'data-certain' not in tag


def test_a_second_candidate_with_the_amount_takes_the_certain_mark_from_the_row(tmp_db):
    """Eine zweite offene Rechnung desselben Kunden mit demselben Betrag: Die Zahlung mit der Nummer hätte auch zu ihr gepasst – nicht eindeutig, also kein Sammelknopf (es bleibt nur eine eindeutige Zeile)."""
    w = a_world(tmp_db)
    add_invoice(tmp_db, 'R26009', 119.00, name='Käufer AG', customer_id=w['buyer'])
    page = period_page(tmp_db)
    assert [(tag['data-booking'], tag.get('data-certain')) for tag in box_row_tags(page)] == [(str(w['first']), None), (str(w['second']), '1')]
    assert 'payAssignAll' not in box_of(page) and CERTAIN_SENTENCE not in box_of(page)


def test_a_text_that_names_a_longer_number_takes_the_certain_mark_from_the_row(tmp_db):
    """``R26001`` steht auch in ``R26001A``: Nennt der Verwendungszweck die längere Nummer, ist die Zahlung für die kürzere Rechnung nicht eindeutig."""
    account, buyer = bank_account(tmp_db), customer(tmp_db)
    add_invoice(tmp_db, 'R26001', 119.00, customer_id=buyer)
    add_invoice(tmp_db, 'R26001A', 238.00, customer_id=buyer)
    add_payment(tmp_db, account, 119.00, '2026-03-10', 'Käufer AG', 'Rechnung R26001A')
    page = period_page(tmp_db)
    assert [tag.get('data-certain') for tag in box_row_tags(page)] == [None] and 'payAssignAll' not in box_of(page)


def test_the_purpose_of_the_movement_is_the_title_of_the_payer_cell_masked_and_cut_at_300_characters(tmp_db):
    account, buyer = bank_account(tmp_db), customer(tmp_db)
    add_invoice(tmp_db, 'R26001', 119.00, customer_id=buyer)
    purpose = f'Rechnung R26001 "><img src=x onerror=alert(1)> & \'Rest\' ' + 'x' * 400
    add_payment(tmp_db, account, 119.00, '2026-03-10', 'Käufer AG', purpose)
    page = period_page(tmp_db)
    box = box_of(page)
    (title,) = re.findall(r'<td title="([^"]*)">Käufer AG</td>', box)
    assert unescape(title) == purpose[:300] and len(unescape(title)) == 300
    assert '<img src=x' not in box and Dom(page).all('img') == [] and '"><img' not in box              # nichts davon bricht das Attribut auf
    assert all('onerror' not in attrs for _tag, attrs in Dom(box).tags)


def test_a_movement_without_a_purpose_has_no_title_on_the_payer_cell(tmp_db):
    w = a_world(tmp_db)
    run_sql(tmp_db, 'UPDATE Bookings SET Text = NULL WHERE ID = ?', w['first'])
    run_sql(tmp_db, "UPDATE Bookings SET Text = '   ' WHERE ID = ?", w['second'])
    box = box_of(period_page(tmp_db))
    assert '<td>Käufer AG</td>' in box and '<td>Müller GmbH</td>' in box and 'title="' not in box.split('<table')[1].split('<button')[0]


# ── Das Skript „Alle eindeutigen zuordnen“ (Node, ohne Browser) ──────────────

ALL_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const scenario = input.scenario;
const calls = {fetch: [], msg: [], confirm: [], navigations: [], removed: [], inFlight: 0, maxInFlight: 0, buttonRemoved: false, refreshed: 0};
const location = {pathname: scenario.pathname, search: scenario.search};
Object.defineProperty(location, 'href', {set: value => { calls.navigations.push(value); }});
const tbody = {removeChild: row => { calls.removed.push(row.index); }};
const rows = scenario.rows.map(([invoice, booking, amount], index) => ({index, parentNode: tbody,
  getAttribute(name) { return {'data-invoice': String(invoice), 'data-booking': String(booking), 'data-amount': amount}[name]; }}));
const allButton = {disabled: false, textContent: scenario.label, parentNode: {removeChild: node => { calls.buttonRemoved = node === allButton; }}};
const buttons = [...rows.map(() => ({disabled: false})), allButton];
const box = {querySelectorAll: selector => (selector === 'tr[data-certain="1"]' ? rows : selector === 'button' ? buttons : [])};
const context = {
  window: {location}, URLSearchParams, JSON, Promise, parseInt, parseFloat, String, setTimeout,
  document: {getElementById: id => (id === 'paySuggest' ? box : null)},
  appConfirm: (text, onOk) => { calls.confirm.push(text); if (scenario.confirm !== false) { onOk(); } },
  appMsg: (text, type) => calls.msg.push([text, type]),
  fetch: (url, init) => {
    const answer = scenario.answers[calls.fetch.length];
    calls.fetch.push({url, method: init.method, headers: init.headers, body: init.body, allDisabled: buttons.every(b => b.disabled)});
    calls.maxInFlight = Math.max(calls.maxInFlight, ++calls.inFlight);
    return new Promise((resolve, reject) => setTimeout(() => {
      calls.inFlight -= 1;
      if (answer === 'reject') { reject(new Error('Netz')); return; }
      resolve({json: () => (answer === 'badJson' ? Promise.reject(new Error('kein JSON')) : Promise.resolve(answer))});
    }, 2));
  },
};
if (scenario.refresh) { context.refreshTable = () => { calls.refreshed += 1; }; }       // die Seite *Buchungen* kennt ``refreshTable``, das Skript allein nicht
vm.createContext(context);
vm.runInContext(input.source, context);
context.payAssignAll(allButton);
// fertig, wenn alle erwarteten Anfragen beantwortet sind und danach kurz nichts mehr geschieht (nicht nach einer festen Zeit: Ein belasteter Rechner bräuchte länger)
const expected = scenario.confirm === false ? 0 : rows.length;
let quiet = 0;
const finish = () => { calls.disabledAtEnd = buttons.map(b => b.disabled); calls.label = allButton.textContent; console.log(JSON.stringify(calls)); process.exit(0); };
setInterval(() => { quiet = calls.fetch.length === expected && calls.inFlight === 0 ? quiet + 1 : 0; if (quiet >= 4) { finish(); } }, 5);
setTimeout(finish, 20000);
"""

OK_ANSWER = {'success': True}
THREE = [[11, 21, '116.62'], [12, 22, '238.00'], [13, 23, '1190.50']]             # (Rechnung, Bewegung, ``data-amount``) der Zeilen des Kastens


def run_all(rows, answers, **scenario):
    source = f'var PAY_FAILED = {script_json(PAY_FAILED)};\n{PAY_SUGGEST_JS}'
    scenario = {**PAGE, 'rows': rows, 'answers': answers, 'label': f'Alle eindeutigen zuordnen ({len(rows)})', **scenario}
    result = subprocess.run([NODE, '-e', ALL_HARNESS], input=json.dumps({'source': source, 'scenario': scenario}), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@NO_NODE
def test_assign_all_asks_first_and_then_assigns_one_row_after_the_other_over_the_existing_route_and_loads_the_page_again():
    calls = run_all(THREE, [OK_ANSWER] * 3)
    assert calls['confirm'] == ['3 Zahlungseingänge den genannten Rechnungen zuordnen?']
    assert [(fetched['url'], fetched['method']) for fetched in calls['fetch']] == [('/invoice/link-payment', 'POST')] * 3
    assert [json.loads(fetched['body']) for fetched in calls['fetch']] == [
        {'invoice_id': 11, 'transaction_id': 21, 'amount_paid': 116.62, 'exact': True}, {'invoice_id': 12, 'transaction_id': 22, 'amount_paid': 238, 'exact': True},
        {'invoice_id': 13, 'transaction_id': 23, 'amount_paid': 1190.5, 'exact': True}]                       # der gezeigte Betrag, nur genau dieser – nie ``skonto``, in der Reihenfolge der Zeilen
    assert all(fetched['headers'] == {'Content-Type': 'application/json'} for fetched in calls['fetch'])
    assert calls['maxInFlight'] == 1                                                                # nacheinander: Die nächste Anfrage beginnt erst nach der Antwort
    assert all(fetched['allDisabled'] for fetched in calls['fetch'])                                # alle Knöpfe des Kastens sind gesperrt, solange es läuft
    assert calls['navigations'] == ['/transactions?from=2026-01-01&to=2026-12-31'] and calls['msg'] == [] and calls['removed'] == []      # neu laden wie ``payAssign``
    assert calls['refreshed'] == 0                                                                  # bei Erfolg lädt die ganze Seite neu


@NO_NODE
def test_assign_all_drops_an_old_message_from_the_address_when_it_loads_the_page_again():
    calls = run_all(THREE[:2], [OK_ANSWER] * 2, search='?from=2026-01-01&err=Alte+Meldung')
    assert calls['navigations'] == ['/transactions?from=2026-01-01']


@NO_NODE
def test_assign_all_does_nothing_when_the_question_is_not_confirmed():
    calls = run_all(THREE[:2], [OK_ANSWER] * 2, confirm=False)
    assert calls['confirm'] == ['2 Zahlungseingänge den genannten Rechnungen zuordnen?']
    assert calls['fetch'] == [] and calls['msg'] == [] and calls['navigations'] == [] and calls['disabledAtEnd'] == [False] * 3


@NO_NODE
def test_assign_all_without_certain_rows_does_not_even_ask():
    calls = run_all([], [])
    assert calls['confirm'] == [] and calls['fetch'] == [] and calls['msg'] == [] and calls['navigations'] == []


@NO_NODE
def test_assign_all_tries_every_row_removes_the_assigned_ones_frees_the_buttons_and_names_the_first_reason():
    calls = run_all(THREE + [[14, 24, '10.00']],
                    [OK_ANSWER, {'success': False, 'error': 'Die Rechnung ist nicht (mehr) zahlbar.'}, OK_ANSWER, {'success': False, 'error': 'Das Bankkonto hat keine SKR-Nummer.'}])
    assert len(calls['fetch']) == 4 and calls['maxInFlight'] == 1                                   # ein Fehler hält die übrigen nicht auf
    assert calls['removed'] == [0, 2]                                                               # nur die gelungenen Zeilen verschwinden
    assert calls['msg'] == [['2 zugeordnet, 2 nicht: Die Rechnung ist nicht (mehr) zahlbar.', 'error']]       # die erste Meldung, nur als Text
    assert calls['navigations'] == [] and calls['disabledAtEnd'] == [False] * 5                      # die Knöpfe sind wieder frei
    assert calls['label'] == 'Alle eindeutigen zuordnen (2)' and calls['buttonRemoved'] is False      # übrig sind die beiden Zeilen, die nicht gingen: der Knopf zählt sie neu


@NO_NODE
def test_assign_all_refreshes_the_list_and_the_box_after_a_partial_failure_when_the_page_has_the_function():
    """Die Liste und die Abzeichen sind danach frisch (``refreshTable`` holt die Seite neu); ohne die Funktion – das Skript allein – bleibt es bei den lokalen Änderungen."""
    failure = {'success': False, 'error': 'Der Stand hat sich geändert – bitte die Seite neu laden.'}
    with_it = run_all(THREE, [OK_ANSWER, failure, OK_ANSWER], refresh=True)
    assert with_it['refreshed'] == 1 and with_it['msg'] == [['2 zugeordnet, 1 nicht: Der Stand hat sich geändert – bitte die Seite neu laden.', 'error']]
    assert with_it['navigations'] == [] and with_it['removed'] == [0, 2]
    without = run_all(THREE, [OK_ANSWER, failure, OK_ANSWER])
    assert without['refreshed'] == 0 and without['msg'] == with_it['msg'] and without['removed'] == [0, 2]
    nothing = run_all(THREE[:2], [failure, failure], refresh=True)
    assert nothing['refreshed'] == 1 and nothing['msg'][0][0].startswith('0 zugeordnet, 2 nicht')


@NO_NODE
@pytest.mark.parametrize('answer', ['reject', 'badJson', {'success': False}, None], ids=['kein-Netz', 'kein-JSON', 'ohne-Grund', 'null'])
def test_assign_all_names_a_failure_without_a_reason_with_the_fixed_sentence(answer):
    calls = run_all(THREE[:2], [answer, OK_ANSWER])
    assert calls['msg'] == [[f'1 zugeordnet, 1 nicht: {PAY_FAILED}', 'error']] and calls['removed'] == [1] and calls['navigations'] == []


@NO_NODE
def test_assign_all_takes_the_button_away_when_fewer_than_two_rows_are_left():
    """Ein einzelner Rest hat seinen eigenen „Zuordnen“-Knopf – der Sammelknopf gälte nur bis zu zwei Zeilen, wie ihn die Seite zeigt."""
    calls = run_all(THREE, [OK_ANSWER, {'success': False, 'error': 'Die Rechnung ist nicht (mehr) zahlbar.'}, OK_ANSWER])
    assert calls['removed'] == [0, 2] and calls['buttonRemoved'] is True and calls['label'] == 'Alle eindeutigen zuordnen (3)'
    assert calls['msg'] == [['2 zugeordnet, 1 nicht: Die Rechnung ist nicht (mehr) zahlbar.', 'error']] and calls['disabledAtEnd'][:3] == [False] * 3


@NO_NODE
def test_assign_all_with_nothing_assigned_says_so_and_keeps_every_row():
    calls = run_all(THREE[:2], [{'success': False, 'error': 'Erster Grund'}, {'success': False, 'error': 'Zweiter Grund'}])
    assert calls['msg'] == [['0 zugeordnet, 2 nicht: Erster Grund', 'error']] and calls['removed'] == [] and calls['disabledAtEnd'] == [False] * 3


@NO_NODE
def test_the_script_of_the_box_still_parses_in_node_with_the_new_functions(tmp_path):
    source = tmp_path / 'pay_all.js'
    source.write_text(PAY_SUGGEST_JS, encoding='utf-8')
    done = subprocess.run([NODE, '--check', str(source)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


# ── Das Abzeichen „Rechnung?“ an einer offenen Bankbewegung der Liste ────────

def test_an_open_movement_with_a_suggestion_carries_the_badge_beside_open(tmp_db):
    w = a_world(tmp_db)
    rows = list_rows(period_page(tmp_db))
    assert badge_titles(rows[w['first']]) == ['Passt zur offenen Rechnung R26001'] and badge_titles(rows[w['second']]) == ['Passt zur offenen Rechnung R26002']
    assert OPEN_BADGE + "<span class='badge bg-blue'" in rows[w['first']]                          # zusätzlich zu „offen“, nicht an seiner Stelle


def test_a_movement_without_a_suggestion_has_no_badge(tmp_db):
    w = a_world(tmp_db)
    alone = add_payment(tmp_db, w['account'], 77.00, '2026-03-12', 'Fremde GmbH', 'Danke')
    rows = list_rows(period_page(tmp_db))
    assert BADGE.findall(rows[alone]) == [] and OPEN_BADGE in rows[alone]


def test_an_assigned_movement_and_one_with_a_child_have_no_badge(tmp_db):
    w = a_world(tmp_db)
    assert tmp_db.assign_invoice_payment(w['one'], w['first']).ok
    tmp_db.insert_booking('2026-03-11', 100.00, booking_type='entry', parent_booking_id=w['second'], coa_id=tmp_db.get_coa_id_by_account_number(6815))
    rows = list_rows(period_page(tmp_db))
    assert BADGE.findall(rows[w['first']]) == [] and BADGE.findall(rows[w['second']]) == []
    assert 'Buchungssätze unvollständig' in rows[w['second']]                                       # die zweite ist „offen“ mit Kind – das Abzeichen gilt nur Zeilen ohne Kind


def test_the_badges_cover_every_pair_of_the_overview_not_only_the_rows_of_the_box(tmp_db):
    account, buyer = bank_account(tmp_db), customer(tmp_db)
    payments = []
    for n in range(1, 24):
        add_invoice(tmp_db, f'R30{n:03d}', 100.00 + n, customer_id=buyer)
        payments.append(add_payment(tmp_db, account, 100.00 + n, f'2026-03-{n:02d}', 'Käufer AG', f'R30{n:03d}'))
    page = period_page(tmp_db)
    rows = list_rows(page)
    assert len(box_row_tags(page)) == 20                                                             # der Kasten zeigt die ersten zwanzig
    assert [badge_titles(rows[payment]) for payment in payments] == [[f'Passt zur offenen Rechnung R30{n:03d}'] for n in range(1, 24)]     # auch die drei hinter dem Kasten


def test_the_overview_is_read_once_per_page_with_the_larger_limit(tmp_db, monkeypatch):
    w = a_world(tmp_db)
    limits = []
    original = Database.suggest_invoice_payments

    def counting(self, limit=20):
        limits.append(limit)
        return original(self, limit)
    monkeypatch.setattr(Database, 'suggest_invoice_payments', counting)
    period_page(tmp_db)
    assert limits == [PAY_BADGE_LIMIT] and PAY_BADGE_LIMIT == 500                                    # EINMAL für den Kasten und die Abzeichen
    PageTransactions(tmp_db, edit_transaction_id=w['first'])                                         # die Seite mit der Maske liest sie ebenfalls einmal
    assert limits == [PAY_BADGE_LIMIT] * 2


def test_a_failing_query_gives_neither_box_nor_badges_and_the_page_stands(tmp_db, monkeypatch, capsys):
    w = a_world(tmp_db)

    def failing(self, limit=20):
        raise RuntimeError('Abfrage kaputt')
    monkeypatch.setattr(Database, 'suggest_invoice_payments', failing)
    page = period_page(tmp_db)
    assert box_of(page).strip() == '' and '<h3>Zahlungseingänge zu offenen Rechnungen</h3>' not in page and BADGE.findall(page) == []
    assert OPEN_BADGE in list_rows(page)[w['first']]                                                 # die Liste steht, die Zeile ist „offen“
    assert 'Abfrage kaputt' in capsys.readouterr().err and 'Abfrage kaputt' not in page             # der Fehler nur im Server-Log


def test_the_number_of_the_invoice_in_the_badge_is_masked(tmp_db):
    hostile_number = "R1' onmouseover='alert(1)' x='" + HOSTILE

    def build(db, number):
        account, buyer = bank_account(db), customer(db, 'Käufer AG')
        add_invoice(db, number, 119.00, customer_id=buyer)
        payment = add_payment(db, account, 119.00, '2026-03-10', 'Käufer AG', 'Danke')           # Betrag und Kunde: die Nummer muss nicht im Text stehen
        return period_page(db), payment
    hostile_page, payment = build(tmp_db, hostile_number)
    benign_page, _ = build(Database(db_name=tmp_db.db_name + '.benign'), 'R26001')
    assert_same_structure(hostile_page, benign_page)
    assert not any(name.startswith('on') and 'alert(' in (value or '') for _tag, attrs in Dom(hostile_page).tags for name, value in attrs.items())
    assert '<img src=x' not in hostile_page and Dom(hostile_page).all('img') == [] and HOSTILE not in hostile_page
    assert badge_titles(list_rows(hostile_page)[payment]) == [f'Passt zur offenen Rechnung {hostile_number}']


def test_the_badge_stands_in_the_table_that_refresh_table_copies_after_an_import(tmp_db):
    a_world(tmp_db)
    page = period_page(tmp_db)
    table = page.split("<table id='transactionsTable'>", 1)[1].split('</table>', 1)[0]
    assert len(BADGE.findall(table)) == 2 and len(BADGE.findall(page)) == 2                         # nur in der Tabelle, die der Weg ``refreshTable()`` austauscht
    refresh = page[page.index('function refreshTable()'):page.index('function openEditForm(')]
    assert "doc.getElementById('transactionsTable')" in refresh and 'cur.innerHTML = fresh.innerHTML' in refresh


def test_the_badge_is_only_a_hint_looking_at_the_page_assigns_nothing(tmp_db):
    w = a_world(tmp_db)
    before = state(tmp_db, 'Bookings', 'Invoices', 'InvoicePayments')
    period_page(tmp_db)
    PageTransactions(tmp_db, edit_transaction_id=w['first'])
    assert state(tmp_db, 'Bookings', 'Invoices', 'InvoicePayments') == before


# ── Der bestehende Weg: Zuordnen über die Route ──────────────────────────────

def test_assigning_over_the_route_makes_the_suggestion_disappear(server_port, auto_login):
    db = auto_login.db
    w = a_world(db)
    page = get(server_port, '/transactions?from=2026-01-01&to=2026-12-31').text
    assert len(box_rows(page)) == 2
    answer = post_json(server_port, '/invoice/link-payment', {'invoice_id': w['one'], 'transaction_id': w['first']}, auto_login.csrf,
                       headers={'Origin': own_origin(server_port)})
    assert (answer.status, json.loads(answer.text)) == (200, {'success': True})
    assert db.get_invoice_payments(w['one'])[0][3] == 119 and db.get_booking_allocations(w['first'])
    page = get(server_port, '/transactions?from=2026-01-01&to=2026-12-31').text
    assert [row[3] for row in box_rows(page)] == ['R26002']
    assert overview_pairs(db) == [(w['second'], w['two'])]


def test_nothing_is_assigned_by_looking_at_the_pages(server_port, auto_login):
    db = auto_login.db
    w = a_world(db)
    before = state(db, 'Bookings', 'Invoices', 'InvoicePayments')
    for path in ('/transactions?from=2026-01-01&to=2026-12-31', f'/transactions/edit?id={w["first"]}', f'/invoice/edit?id={w["one"]}', '/invoice'):
        assert get(server_port, path).status == 200
    assert state(db, 'Bookings', 'Invoices', 'InvoicePayments') == before


# ── Die Maske der Bankbewegung ───────────────────────────────────────────────

def test_the_mask_lists_the_suggestions_first_and_selects_nothing(tmp_db):
    w = a_world(tmp_db)
    fillers = [add_invoice(tmp_db, f'R26{n:03d}', 50.00 + n, customer_id=w['buyer'], date=f'2026-02-{n:02d}') for n in range(11, 14)]
    page = PageTransactions(tmp_db, edit_transaction_id=w['first'])
    options = option_labels(page)
    assert options[0] == ('', '-- Keine Rechnung --', False)
    assert options[1][:2] == (str(w['one']), f'R26001 — Käufer AG — offen 119,00 € — Vorschlag: {NUMBER_AND_NAME}')
    rest = options[2:]
    assert sorted(value for value, _t, _s in rest) == sorted(str(i) for i in [w['two'], *fillers])
    assert not any('Vorschlag' in text for _v, text, _s in rest)                                                       # die übrigen ohne Zusatz
    assert not any(selected for _v, _t, selected in options)                                                         # nichts ist gewählt
    assert 'selected' not in INVOICE_SELECT.search(page).group(0)


def test_the_remaining_invoices_keep_their_old_order_and_labels(tmp_db):
    w = a_world(tmp_db)
    expected = [(str(row[0]), f"{row[1]} — {row[15] or row[14]} — offen {fmt_eur(row[39])}") for row in tmp_db.get_open_invoices()]
    with_suggestion = [(v, t) for v, t, _s in option_labels(PageTransactions(tmp_db, edit_transaction_id=w['first']))]
    other_suggestion = [(v, t) for v, t, _s in option_labels(PageTransactions(tmp_db, edit_transaction_id=w['second']))]
    assert with_suggestion[2:] == [pair for pair in expected if pair[0] != str(w['one'])]
    assert other_suggestion[2:] == [pair for pair in expected if pair[0] != str(w['two'])]


def test_the_suggestion_line_names_the_best_invoice_and_takes_over_only_the_value(tmp_db):
    w = a_world(tmp_db)
    page = PageTransactions(tmp_db, edit_transaction_id=w['first'])
    line = re.search(r'<div class="hint" id="invoiceSuggestion">(.*?)</div>', page, re.S).group(1)
    assert line.startswith(f'Vorschlag: Rechnung R26001 ({NUMBER_AND_NAME}) ')
    button = Dom(line).one('button')
    assert button['type'] == 'button' and button['onclick'] == f"this.form.elements['invoice_id'].value = '{w['one']}'"
    assert line.rstrip().endswith('Übernehmen</button>')
    start = page.index('<form method="POST" action="/transactions/add">')
    assert 'id="invoiceSuggestion"' in page[start:page.index('</form>', start)]                  # im Formular, damit this.form das Auswahlfeld findet


def test_without_a_suggestion_the_mask_has_no_line_and_no_addition(tmp_db):
    w = a_world(tmp_db)
    lone = add_payment(tmp_db, w['account'], 77.00, '2026-03-12', 'Jemand Anderes', 'Spende')
    page = PageTransactions(tmp_db, edit_transaction_id=lone)
    assert 'invoiceSuggestion' not in page and 'Vorschlag' not in INVOICE_SELECT.search(page).group(0)
    assert option_labels(page)[0] == ('', '-- Keine Rechnung --', False)


def test_a_new_booking_and_the_prefill_from_an_invoice_stay_as_they_were(tmp_db):
    w = a_world(tmp_db)
    new = PageTransactions(tmp_db)
    assert 'invoiceSuggestion' not in new and 'Vorschlag' not in INVOICE_SELECT.search(new).group(0)
    assert not any(selected for _v, _t, selected in option_labels(new))
    prefilled = PageTransactions(tmp_db, from_invoice=w['one'])
    assert [(value, selected) for value, _t, selected in option_labels(prefilled) if selected] == [(str(w['one']), True)]       # die Vorbelegung bleibt gewählt
    assert 'invoiceSuggestion' not in prefilled and 'Vorschlag' not in INVOICE_SELECT.search(prefilled).group(0)


def test_a_booking_that_is_no_incoming_payment_has_no_invoice_field_and_no_suggestion(tmp_db):
    w = a_world(tmp_db)
    outgoing = add_payment(tmp_db, w['account'], -119.00, '2026-03-12', 'Käufer AG', 'Rechnungs-Nr R26001')
    page = PageTransactions(tmp_db, edit_transaction_id=outgoing)
    assert 'name="invoice_id"' not in page and 'invoiceSuggestion' not in page


def test_opening_the_mask_and_saving_it_unchanged_assigns_nothing(tmp_db):
    w = a_world(tmp_db)
    before = snapshot(tmp_db)
    post, _adopt = open_form(tmp_db, w['first'])
    assert post['invoice_id'] == ['']                                  # was der Browser abschickt: „-- Keine Rechnung --“
    status, location = handle_add_transaction(tmp_db, post)
    assert (status, location) == (303, '/transactions')
    assert differences(before, snapshot(tmp_db)) == [] and tmp_db.get_invoice_payments(w['one']) == []


def test_taking_over_the_suggestion_and_saving_assigns_it_through_the_existing_way(tmp_db):
    w = a_world(tmp_db)
    post, _adopt = open_form(tmp_db, w['first'])
    post['invoice_id'] = [str(w['one'])]                               # „Übernehmen“ setzt nur diesen Wert; zugeordnet wird beim Aktualisieren
    status, location = handle_add_transaction(tmp_db, post)
    assert (status, location) == (303, f'/invoice/edit?id={w["one"]}')                                  # der Erfolgsweg bleibt
    assert [row[2] for row in tmp_db.get_invoice_payments(w['one'])] == [w['first']]


# ── Der Fehlerweg: die Zuordnung scheitert nach dem Speichern ────────────────

def failing_save(db, w):
    """Die Maske der Zahlung 1 mit der Rechnung 1 gewählt – und die Rechnung wird zwischen Öffnen und Speichern storniert: Die Zuordnung scheitert."""
    post, _adopt = open_form(db, w['first'])
    post['invoice_id'] = [str(w['one'])]
    post['text'] = ['Geänderter Text']                                                   # die Buchung selbst lässt sich speichern
    run_sql(db, "UPDATE Invoices SET Status = 'cancelled' WHERE ID = ?", w['one'])
    return handle_add_transaction(db, post)


def test_the_fixed_sentence_of_the_failing_way_is_the_planned_one():
    assert PAYMENT_NOT_ASSIGNED == 'Die Buchung ist gespeichert, die Zahlung wurde aber nicht zugeordnet: {reason}'


def test_a_failing_assignment_keeps_the_user_at_the_booking_with_the_reason(tmp_db):
    w = a_world(tmp_db)
    status, location = failing_save(tmp_db, w)
    assert status == 303
    path, _, query = location.partition('?')
    assert path == '/transactions/edit' and query.startswith(f'id={w["first"]}&err=')
    err = query.split('&err=', 1)[1]
    assert re.fullmatch(r'[A-Za-z0-9%_.~-]+', err)                                       # prozentkodiert, nichts Rohes (``quote(…, safe='')``)
    text = urllib.parse.unquote(err)
    reason = text.split(': ', 1)[1]
    assert text == PAYMENT_NOT_ASSIGNED.format(reason=reason) and len(reason) > 10        # mit dem Grund der Zuordnung
    assert tmp_db.get_booking_by_id(w['first'])[15] == 'Geänderter Text'                 # die Buchung ist gespeichert
    assert tmp_db.get_invoice_payments(w['one']) == []                                   # die Zahlung nicht zugeordnet


def test_a_failing_assignment_of_an_unexpected_kind_names_the_fixed_sentence(tmp_db, monkeypatch):
    w = a_world(tmp_db)

    def explode(*args, **kwargs):
        raise RuntimeError('geheimer Pfad /srv/buch.db')
    monkeypatch.setattr(handlers, 'link_booking_to_invoice_capped', explode)
    post, _adopt = open_form(tmp_db, w['first'])
    post['invoice_id'] = [str(w['one'])]
    status, location = handle_add_transaction(tmp_db, post)
    assert status == 303 and 'geheimer' not in urllib.parse.unquote(location)
    assert urllib.parse.unquote(location.split('&err=', 1)[1]) == PAYMENT_NOT_ASSIGNED.format(reason='Die Zahlung konnte nicht zugeordnet werden.')


def test_the_mask_page_shows_the_reason_over_http(server_port, auto_login):
    db = auto_login.db
    w = a_world(db)
    status, location = failing_save(db, w)
    page = get(server_port, location)
    assert (status, page.status) == (303, 200)
    text = urllib.parse.unquote(location.split('&err=', 1)[1])
    assert app_msg_arguments(Dom(page.text)) == [text, 'error']
    assert f'name="transaction_id" value="{w["first"]}"' in page.text and 'Geänderter Text' in page.text      # die Maske der Buchung, nicht die Rechnungsseite


def test_a_hostile_reason_in_the_address_stays_text_and_the_page_keeps_its_structure(server_port, auto_login):
    w = a_world(auto_login.db)
    benign = get(server_port, f'/transactions/edit?id={w["first"]}&err=Normale+Meldung')
    hostile = get(server_port, f'/transactions/edit?id={w["first"]}&err={urllib.parse.quote(HOSTILE, safe="")}')
    assert hostile.status == 200
    assert_same_structure(hostile.text, benign.text)
    assert '</script><img' not in hostile.text
    assert app_msg_arguments(Dom(hostile.text)) == [HOSTILE, 'error']
    with pytest.raises(AssertionError):                                                    # ohne err keine Meldung beim Laden
        app_msg_arguments(Dom(get(server_port, f'/transactions/edit?id={w["first"]}').text))


def test_the_error_way_of_the_list_page_still_works(server_port, auto_login):
    assert app_msg_arguments(Dom(get(server_port, '/transactions?err=Normale+Meldung').text)) == ['Normale Meldung', 'error']


# ── Die Rechnungsseite ───────────────────────────────────────────────────────

def candidate_buttons(page):
    """Die IDs der Bewegungen in der Liste „Vorhandene Buchung verknüpfen“, in der Reihenfolge der Zeilen."""
    return [int(n) for n in re.findall(r'onclick="linkPayment\((\d+), this\)"', page)]


def test_the_invoice_page_lists_the_suggestions_first_with_the_reason_under_the_payer(tmp_db):
    w = a_world(tmp_db)
    later = add_payment(tmp_db, w['account'], 20.00, '2026-03-20', 'Käufer AG', 'Irgendwas')              # jünger und vom selben Kunden, aber ohne Vorschlag
    page = PageInvoice(tmp_db, {}, w['one'])
    buttons = candidate_buttons(page)
    assert buttons[0] == w['first'] and set(buttons) == {w['first'], later, w['second']} and buttons.index(w['first']) < buttons.index(later)
    row = page[page.index('<h4>Vorhandene Buchung verknüpfen</h4>'):]
    first_row = row[row.index('<tr>', row.index('</tr>')):row.index(f'linkPayment({w["first"]}, this)')]
    assert f'Käufer AG<div class="hint">{NUMBER_AND_NAME}</div>' in first_row               # die Begründung klein unter dem Auftraggeber
    assert '119,00 € ✓' in first_row                                                         # der Haken bei gleichem Betrag bleibt
    others = row[row.index(f'linkPayment({w["first"]}, this)'):]
    assert 'class="hint">Rechnungsnummer' not in others and '20,00 €' in others                # die übrigen wie bisher, ohne Begründung


def test_the_invoice_page_keeps_the_old_list_when_nothing_is_suggested(tmp_db):
    w = a_world(tmp_db)
    lone = add_invoice(tmp_db, 'R26050', 55.00, name='Fremde GmbH', customer_id=customer(tmp_db, 'Fremde GmbH'))
    page = PageInvoice(tmp_db, {}, lone)
    assert candidate_buttons(page) == [w['second'], w['first']]                              # alle Zahlungen mit freiem Rest, jüngste zuerst, wie bisher
    assert 'class="hint">Rechnungsnummer' not in page and 'Betrag stimmt' not in page


def test_the_invoice_page_lists_no_payment_twice_and_none_that_is_assigned(tmp_db):
    w = a_world(tmp_db)
    assert tmp_db.assign_invoice_payment(w['one'], w['first']).ok
    other = add_invoice(tmp_db, 'R26003', 119.00, name='Käufer AG', customer_id=w['buyer'])
    duplicate = add_payment(tmp_db, w['account'], 119.00, '2026-03-13', 'Käufer AG', 'Zahlung')           # vorgeschlagen UND in der alten Liste
    buttons = candidate_buttons(PageInvoice(tmp_db, {}, other))
    assert w['first'] not in buttons and buttons.count(duplicate) == 1 and len(buttons) == len(set(buttons))
    assert buttons[0] == duplicate


def test_every_value_on_the_invoice_list_is_masked(tmp_db):
    account = bank_account(tmp_db)
    invoice_id = add_invoice(tmp_db, 'R26001', 119.00, customer_id=customer(tmp_db))
    add_payment(tmp_db, account, 119.00, '2026-03-10', f'Käufer {HOSTILE}', 'Rechnungs-Nr R26001')
    page = PageInvoice(tmp_db, {}, invoice_id)
    assert '<img src=x' not in page and [tag for tag in Dom(page).all('img') if tag.get('src') == 'x'] == []        # (das Firmenlogo der Seite ist ein img)
    assert 'Käufer &lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;<div class="hint">' in page


# ═════════════════════════════════════════════════════════════════════════════
# Skonto (2.9.0): der offene Betrag nach Abzug des Skontos
# ═════════════════════════════════════════════════════════════════════════════

TERMS = {'skonto_days': 14, 'skonto_percent': 2.0}                  # 2 % in 14 Tagen: auf 119,00 € sind das 2,38 €
AFTER = 1166200                                                       # 116,62 € in Minor Units: was der Kunde nach Abzug des Skontos zahlt
LIMIT = 23800                                                         # das Skonto: 2,38 €


def open_rows(db):
    """Die offenen Rechnungen als Wörterbücher der Abfrage – nach Rechnungsnummer."""
    with closing(db._get_connection()) as conn:
        return {row['number']: row for row in db._open_invoices(conn)}


def discount_world(db, **extra):
    """Eine Rechnung R26001 (119,00 €, 19 %, Skonto 2 % in 14 Tagen) und ihr Kunde; ``extra`` ändert die Rechnung (``skonto_days=None``, ``tax_rate=0.16`` …). ``mixed_tax_rates=1`` setzt das
    Kennzeichen „Steuersatz je Position“ erst nach dem Anlegen (die Forderung eines solchen Belegs bucht je Satz und bräuchte Positionen – hier geht es nur um das Kennzeichen)."""
    mixed = extra.pop('mixed_tax_rates', None)
    account, buyer = bank_account(db), customer(db)
    invoice_id = add_invoice(db, 'R26001', 119.00, customer_id=buyer, **{**TERMS, **extra})
    if mixed is not None:
        run_sql(db, 'UPDATE Invoices SET MixedTaxRates = ? WHERE ID = ?', mixed, invoice_id)
    return account, buyer, invoice_id


def mixed_discount_world(db, parts=(('19', '100.00'), ('7', '50.00')), snapshots=None, **extra):
    """Eine Rechnung R26001 mit Steuersatz je Position (``parts``: Satz in Prozent und Netto in Euro, Kennzeichen 1, Summen aus ``tax_groups.rate_groups`` wie beim Speichern der App), Skonto 2 % in
    14 Tagen, versendet (ohne Forderung – hier geht es nur um die Angaben zum Skonto). ``snapshots``: ``{Position (1…): Spalten der Momentaufnahme}`` für ``insert_invoice_item``; ``extra`` ändert
    die Rechnung. Standard: 19 % × 100,00 und 7 % × 50,00, brutto 172,50, Skonto 3,45 €."""
    groups = rate_groups([(Decimal(rate), to_minor(Decimal(net))) for rate, net in parts])
    net, tax = sum(group.net for group in groups), sum(group.tax for group in groups)
    account, buyer = bank_account(db), customer(db)
    gross = from_minor(net + tax)
    data = {**TERMS, 'sum_net': from_minor(net), 'tax_amount': from_minor(tax), 'sum_gross': gross, 'amount_due': gross, 'mixed_tax_rates': 1, **extra}
    invoice_id = add_invoice(db, 'R26001', float(gross), customer_id=buyer, status='draft', **data)
    for position, (rate, net_euro) in enumerate(parts, 1):
        db.insert_invoice_item({'invoice_id': invoice_id, 'position': position, 'description': 'Anstrich', 'quantity': 1, 'unit': 'Stk.', 'price_per_unit': float(net_euro),
                                'total_net': float(net_euro), 'tax_category': 'S', 'tax_rate': float(rate), **((snapshots or {}).get(position) or {})})
    run_sql(db, "UPDATE Invoices SET Status = 'sent' WHERE ID = ?", invoice_id)
    return account, buyer, invoice_id


def test_an_open_invoice_with_discount_terms_has_the_open_amount_after_the_discount(tmp_db):
    account, _buyer, _invoice = discount_world(tmp_db)
    row = open_rows(tmp_db)['R26001']
    assert (row['open_minor'], row['skonto_open_minor'], row['skonto_limit_minor']) == (1190000, AFTER, LIMIT)


def test_the_amount_after_the_discount_and_the_customer_are_a_suggestion_with_the_skonto_flag(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    ((pair, row),) = tmp_db.suggest_invoices_for_booking(payment)
    assert (pair.invoice_id, pair.skonto, pair.reasons) == (invoice_id, True, ('Betrag nach Skonto stimmt', 'Kunde passt'))
    assert (row['open_minor'], row['skonto_open_minor'], row['skonto_limit_minor']) == (1190000, AFTER, LIMIT)
    ((pair, _movement, _invoice),) = tmp_db.suggest_invoice_payments().items                          # die Übersicht der Seite Buchungen ebenso
    assert (pair.movement_id, pair.invoice_id, pair.skonto) == (payment, invoice_id, True)
    ((pair, _movement),) = tmp_db.suggest_bookings_for_invoice(invoice_id)                           # und die Rechnungsseite
    assert (pair.movement_id, pair.skonto, pair.reasons) == (payment, True, ('Betrag nach Skonto stimmt', 'Kunde passt'))


def test_the_amount_after_the_discount_alone_is_no_suggestion_and_the_full_amount_stays_the_full_amount(tmp_db):
    account, _buyer, _invoice = discount_world(tmp_db)
    alone = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Fremde GmbH', 'Danke')               # der Betrag, aber ein anderer Kunde
    assert tmp_db.suggest_invoices_for_booking(alone) == [] and tmp_db.suggest_invoice_payments().items == []
    full = add_payment(tmp_db, account, 119.00, '2026-03-11', 'Käufer AG', 'Danke')
    ((pair, _row),) = tmp_db.suggest_invoices_for_booking(full)
    assert (pair.skonto, pair.reasons) == (False, ('Betrag stimmt', 'Kunde passt'))


@pytest.mark.parametrize('extra', [
    {'skonto_days': None, 'skonto_percent': None}, {'skonto_days': 14, 'skonto_percent': None}, {'skonto_days': 400},
    {'mixed_tax_rates': 1}, {'tax_rate': 0.16}, {'tax_rate': 0.055},
], ids=['ohne-Bedingungen', 'nur-Tage', 'Frist-zu-lang', 'Satz-je-Position-ohne-Positionen', '16-Prozent', '5,5-Prozent'])
def test_an_invoice_that_allows_no_discount_has_no_skonto_value_and_no_skonto_suggestion(tmp_db, extra):
    account, _buyer, _invoice = discount_world(tmp_db, **extra)
    row = open_rows(tmp_db)['R26001']
    assert (row['skonto_open_minor'], row['skonto_limit_minor']) == (None, None)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    assert tmp_db.suggest_invoices_for_booking(payment) == []


@pytest.mark.parametrize('columns, has_skonto', [
    ({'tax_rate_bp': 1900}, True), ({'tax_rate_bp': 1900, 'revenue_coa_id': 'own'}, True), ({'tax_rate_bp': 1900, 'revenue_coa_id': 'discount'}, False),
], ids=['Satz-aus-den-Stammdaten', 'eigenes-Erlöskonto', 'Erlöskonto-ist-ein-Skonto-Konto'])
def test_an_invoice_with_a_position_from_the_master_data_has_a_skonto_value_unless_its_revenue_account_is_a_discount_account(tmp_db, columns, has_skonto):
    """Eine Position mit der Momentaufnahme eines Steuersatzes (``TaxRateBp``), auch mit einem Erlöskonto aus den Stammdaten: Das Zuordnen mit Skonto geht (``tests/test_skonto_mixed_booking.py``),
    also schlägt die Seite es vor – außer das Erlöskonto ist selbst ein Skonto-Konto (``SKONTO_REVENUE_DISCOUNT``)."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    with closing(sqlite3.connect(tmp_db.db_name)) as conn:
        numbers = {'own': 4400, 'discount': 4736}
        coa = None if 'revenue_coa_id' not in columns else conn.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?', (numbers[columns['revenue_coa_id']],)).fetchone()[0]
    tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 1, 'description': 'Anstrich', 'quantity': 1, 'unit': 'Stk.', 'price_per_unit': 100.0,
                                'total_net': 100.0, 'tax_category': 'S', 'tax_rate': 0.19, **columns, 'revenue_coa_id': coa})
    row = open_rows(tmp_db)['R26001']
    assert (row['skonto_open_minor'], row['skonto_limit_minor']) == ((AFTER, LIMIT) if has_skonto else (None, None))
    found = tmp_db.suggest_invoices_for_booking(add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke'))
    if has_skonto:
        ((pair, _row),) = found
        assert pair.skonto is True
    else:
        assert found == []


def test_the_rows_of_a_half_snapshot_are_not_read_by_the_suggestions(tmp_db):
    """Bekannte Grenze: Ein Erlöskonto aus den Stammdaten ohne den Satz dazu (die halbe Momentaufnahme, ein Widerspruch) sieht erst das Zuordnen (``TAX_ENTRY_CONTRADICTION``) – der Vorschlag erscheint,
    der Klick lehnt mit einem festen Satz ab."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 1, 'description': 'Anstrich', 'quantity': 1, 'unit': 'Stk.', 'price_per_unit': 100.0,
                                'total_net': 100.0, 'tax_category': 'S', 'tax_rate': 0.19, 'tax_rate_bp': None, 'revenue_coa_id': 404400})
    assert open_rows(tmp_db)['R26001']['skonto_open_minor'] == AFTER


def test_a_plain_position_without_a_snapshot_does_not_stop_the_discount(tmp_db):
    _account, _buyer, invoice_id = discount_world(tmp_db)
    tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 1, 'description': 'Anstrich', 'quantity': 1, 'unit': 'Stk.', 'price_per_unit': 100.0,
                                'total_net': 100.0, 'tax_category': 'S', 'tax_rate': 0.19})
    assert open_rows(tmp_db)['R26001']['skonto_open_minor'] == AFTER


def test_the_open_amount_after_a_part_payment_is_the_rest_less_the_discount(tmp_db):
    """119,00 €, davon 50,00 € bezahlt: offen 69,00 €, das Skonto der Rechnung bleibt 2,38 € (es hängt am Brutto) – der Betrag nach Skonto ist 66,62 €."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    assert tmp_db.assign_invoice_payment(invoice_id, add_payment(tmp_db, account, 50.00, '2026-03-06', 'Käufer AG', 'Anzahlung')).ok
    row = open_rows(tmp_db)['R26001']
    assert (row['open_minor'], row['skonto_open_minor'], row['skonto_limit_minor']) == (690000, 690000 - LIMIT, LIMIT)
    payment = add_payment(tmp_db, account, 66.62, '2026-03-10', 'Käufer AG', 'Rest')
    ((pair, _row),) = tmp_db.suggest_invoices_for_booking(payment)
    assert pair.skonto is True


def test_a_rest_that_is_not_above_the_discount_has_the_limit_but_no_discounted_amount(tmp_db):
    """117,00 € bezahlt, offen 2,00 €: Das Skonto (2,38 €) ist nicht kleiner als der offene Betrag – kein Betrag nach Skonto (er wäre 0 oder negativ), aber ein Rest unter dem Skonto lässt sich ausbuchen."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    assert tmp_db.assign_invoice_payment(invoice_id, add_payment(tmp_db, account, 117.00, '2026-03-06', 'Käufer AG', 'fast alles')).ok
    row = open_rows(tmp_db)['R26001']
    assert (row['open_minor'], row['skonto_open_minor'], row['skonto_limit_minor']) == (20000, None, LIMIT)


@pytest.mark.parametrize('column, value', [
    ('SkontoPercent', 'zwei'), ('SkontoDays', 'vierzehn'), ('SkontoPercent', 2.005), ('TaxRate', 'neunzehn'), ('SumGross', 'viel'),
], ids=['Prozent-Text', 'Tage-Text', 'drei-Stellen', 'Satz-Text', 'Brutto-Text'])
def test_a_value_that_cannot_be_read_gives_no_skonto_and_no_error(tmp_db, column, value):
    """Die Spalten lassen sich per SQL-Konsole mit allem beschreiben: Die Seiten hängen nie an einer solchen Rechnung."""
    account, _buyer, invoice_id = discount_world(tmp_db)
    run_sql(tmp_db, f'UPDATE Invoices SET {column} = ? WHERE ID = ?', value, invoice_id)
    row = open_rows(tmp_db)['R26001']
    assert row['skonto_open_minor'] is None and row['skonto_limit_minor'] is None
    assert tmp_db.suggest_invoices_for_booking(add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')) == []
    assert tmp_db.skonto_limits() == {} and not tmp_db.skonto_offer(invoice_id).limit          # ein Skonto von 0 (Brutto unlesbar) ist keines: ``limit`` 0 oder fehlend
    assert tmp_db.suggest_invoice_payments().items == []


def test_the_flag_for_a_rate_per_position_is_read_strictly_only_the_whole_number_one_counts(tmp_db):
    """Wie überall (``has_mixed_rates``): Text und 0 heißen „ein Satz für den ganzen Beleg“ – das Skonto bleibt möglich; die ganze Zahl 1 macht ihn zum Beleg mit Steuersatz je Position – hier ohne
    Positionen, also ohne lesbare Gruppen: kein Skonto."""
    _account, _buyer, invoice_id = discount_world(tmp_db)
    for value, allowed in (('ja', True), (0, True), (1, False)):
        run_sql(tmp_db, 'UPDATE Invoices SET MixedTaxRates = ? WHERE ID = ?', value, invoice_id)
        assert (open_rows(tmp_db)['R26001']['skonto_open_minor'] is not None) is allowed, value


def test_a_gross_that_is_not_a_whole_number_never_gives_a_limit_of_zero_a_value(tmp_db):
    """Ein Brutto von 0 (oder keines) ergäbe ein Skonto von 0: Es gibt dann keine Grenze und keinen Betrag nach Skonto."""
    _account, _buyer, invoice_id = discount_world(tmp_db)
    run_sql(tmp_db, 'UPDATE Invoices SET SumGross = 0 WHERE ID = ?', invoice_id)
    row = open_rows(tmp_db)['R26001']
    assert (row['skonto_open_minor'], row['skonto_limit_minor']) == (None, None)


# ── Rechnungen mit Steuersatz je Position (Stufe 2) ─────────────────────────

def _snapshot(pct, coa=None):
    """Die Momentaufnahme eines Eintrags der Stammdaten mit Satz ``pct`` (Art ``'S'``) und, wo es eines gibt, dem Erlöskonto ``coa``."""
    return {'tax_category': 'S', 'tax_rate_bp': int(Decimal(pct) * 100), 'tax_exempt_reason': None, 'revenue_coa_id': coa}


def _coa_of(db, number):
    with closing(sqlite3.connect(db.db_name)) as conn:
        return conn.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?', (number,)).fetchone()[0]


def test_a_rate_per_position_with_19_and_7_percent_has_the_discount_over_the_whole_gross(tmp_db):
    """19 % × 100,00 und 7 % × 50,00: brutto 172,50, Skonto 2 % = 3,45 € – die Rechnung lässt es zu (der Kopfsatz ist nur ein Platzhalter)."""
    account, _buyer, invoice_id = mixed_discount_world(tmp_db)
    row = open_rows(tmp_db)['R26001']
    assert (row['open_minor'], row['skonto_open_minor'], row['skonto_limit_minor']) == (1725000, 1725000 - 34500, 34500)
    payment = add_payment(tmp_db, account, 169.05, '2026-03-10', 'Käufer AG', 'Danke')
    ((pair, found),) = tmp_db.suggest_invoices_for_booking(payment)
    assert (pair.invoice_id, pair.skonto, pair.reasons) == (invoice_id, True, ('Betrag nach Skonto stimmt', 'Kunde passt'))
    ((pair, _movement, _invoice),) = tmp_db.suggest_invoice_payments().items
    assert (pair.movement_id, pair.invoice_id, pair.skonto) == (payment, invoice_id, True)
    assert tmp_db.skonto_limits() == {invoice_id: 34500} and tmp_db.skonto_offer(invoice_id) == (None, 34500, 14, Decimal('2.00'))


def test_the_head_rate_of_a_rate_per_position_never_counts(tmp_db):
    """Der Kopfsatz so einer Rechnung ist nur der Platzhalter (der höchste Satz): Auch 16 % im Kopf macht das Skonto nicht unmöglich – es zählen die Gruppen."""
    _account, _buyer, invoice_id = mixed_discount_world(tmp_db, tax_rate=0.16)
    assert open_rows(tmp_db)['R26001']['skonto_limit_minor'] == 34500 and tmp_db.skonto_offer(invoice_id).error is None


def test_a_rate_per_position_with_a_tax_free_group_from_the_master_data_has_the_discount(tmp_db):
    """19 %, 7 % und steuerfrei (Momentaufnahme mit einem Erlöskonto, hier 4400): brutto 272,50, Skonto 5,45 €."""
    _account, _buyer, invoice_id = mixed_discount_world(
        tmp_db, parts=(('19', '100.00'), ('7', '50.00'), ('0', '100.00')),
        snapshots={3: {'tax_category': 'E', 'tax_rate_bp': 0, 'tax_exempt_reason': 'erfundener Grund', 'revenue_coa_id': _coa_of(tmp_db, 4400)}})
    row = open_rows(tmp_db)['R26001']
    assert (row['skonto_open_minor'], row['skonto_limit_minor']) == (2725000 - 54500, 54500)
    assert tmp_db.skonto_offer(invoice_id).limit == 54500


@pytest.mark.parametrize('parts, snapshots, shown', [
    ((('19', '100.00'), ('5.5', '200.00')), {2: _snapshot('5.5')}, '5,5'),
    ((('19', '100.00'), ('16', '100.00')), None, '16'),
    ((('19', '100.00'), ('5', '100.00')), None, '5'),
], ids=['5,5-Prozent', '16-Prozent', '5-Prozent'])
def test_a_rate_per_position_with_a_group_without_a_discount_account_has_no_skonto_and_names_its_rate(tmp_db, parts, snapshots, shown):
    account, _buyer, invoice_id = mixed_discount_world(tmp_db, parts, snapshots)
    row = open_rows(tmp_db)['R26001']
    assert (row['skonto_open_minor'], row['skonto_limit_minor']) == (None, None)
    assert tmp_db.skonto_offer(invoice_id) == (SKONTO_GROUP_RATE.format(rate=shown), None, None, None) and tmp_db.skonto_limits() == {}
    assert tmp_db.suggest_invoices_for_booking(add_payment(tmp_db, account, 100.00, '2026-03-10', 'Käufer AG', 'Danke')) == []


@pytest.mark.parametrize('parts, snapshots', [
    ((('19', '100.00'), ('5.5', '200.00')), {2: _snapshot('5.5')}),
    ((('19', '100.00'), ('16', '100.00')), None),
], ids=['5,5-Prozent', '16-Prozent'])
def test_a_rate_per_position_without_discount_terms_has_no_skonto_and_names_no_rate(tmp_db, parts, snapshots):
    """Hat die Rechnung keine Bedingungen, kommt ``SKONTO_NO_TERMS`` vor dem Satz einer Gruppe: Die Rechnungsseite sagt dazu nichts."""
    _account, _buyer, invoice_id = mixed_discount_world(tmp_db, parts, snapshots, skonto_days=None, skonto_percent=None)
    assert open_rows(tmp_db)['R26001']['skonto_limit_minor'] is None
    assert tmp_db.skonto_offer(invoice_id) == (SKONTO_NO_TERMS, None, None, None)


def test_a_rate_per_position_with_positions_that_do_not_match_its_sums_has_no_skonto(tmp_db):
    _account, _buyer, invoice_id = mixed_discount_world(tmp_db)
    run_sql(tmp_db, 'UPDATE Invoices SET SumNet = 9990000 WHERE ID = ?', invoice_id)
    assert open_rows(tmp_db)['R26001']['skonto_limit_minor'] is None
    assert tmp_db.skonto_offer(invoice_id) == (MIXED_GROUPS_MISMATCH, None, None, None)


def test_a_rate_per_position_whose_positions_cannot_be_read_has_no_skonto_and_no_error(tmp_db):
    _account, _buyer, invoice_id = mixed_discount_world(tmp_db)
    run_sql(tmp_db, "UPDATE InvoiceItems SET TaxRate = 'sieben' WHERE InvoiceId = ? AND Position = 2", invoice_id)
    assert open_rows(tmp_db)['R26001']['skonto_limit_minor'] is None
    assert tmp_db.skonto_offer(invoice_id).error == MIXED_GROUPS_MISMATCH


@pytest.mark.parametrize('number', sorted(SKONTO_ACCOUNT_NUMBERS))
def test_a_rate_per_position_with_a_discount_account_as_a_revenue_account_has_no_skonto(tmp_db, number):
    """Die Erstattung und die Rücknahme beim Stornieren verlassen sich darauf, dass kein Erlöskonto ein Skonto-Konto ist (``SKONTO_REVENUE_DISCOUNT``): Dann gibt es das Skonto nicht."""
    _account, _buyer, invoice_id = mixed_discount_world(tmp_db, snapshots={2: _snapshot('7', _coa_of(tmp_db, number))})
    assert open_rows(tmp_db)['R26001']['skonto_limit_minor'] is None
    assert tmp_db.skonto_offer(invoice_id) == (SKONTO_REVENUE_DISCOUNT, None, None, None)


def test_the_groups_of_all_the_open_invoices_are_read_with_one_query(tmp_db):
    """Je Übersicht EINE Abfrage über die Positionen der Rechnungen mit dem Kennzeichen – nicht je Rechnung eine (``MAX_INVOICES`` je Abfrage)."""
    for number in range(30):
        add_invoice(tmp_db, f'M{number:05d}', 172.50, customer_id=customer(tmp_db, f'Kunde {number}'), status='draft', **{**TERMS, 'sum_net': 150.0, 'tax_amount': 22.5, 'mixed_tax_rates': 1})
    with closing(sqlite3.connect(tmp_db.db_name)) as conn:
        ids = [row[0] for row in conn.execute('SELECT ID FROM Invoices')]
        conn.executemany('INSERT INTO InvoiceItems (InvoiceId, Position, Description, Quantity, PricePerUnit, TotalNet, TaxRate) VALUES (?, ?, ?, 1, 1, ?, ?)',
                         [(invoice_id, position, 'x', net, rate) for invoice_id in ids for position, net, rate in ((1, 1000000, 19.0), (2, 500000, 7.0))])
        conn.execute("UPDATE Invoices SET Status = 'sent'")
        conn.commit()
    statements = []
    conn = tmp_db._get_connection()
    try:
        conn.set_trace_callback(statements.append)
        rows = tmp_db._open_invoices(conn)
    finally:
        conn.close()
    assert len(rows) == 30 and {row['skonto_limit_minor'] for row in rows} == {34500}
    assert len([statement for statement in statements if 'InvoiceItems' in statement and 'TotalNet' in statement]) == 1


# ── skonto_offer und skonto_limits ───────────────────────────────────────────

def test_the_offer_of_an_invoice_names_the_limit_the_days_and_the_percent(tmp_db):
    _account, _buyer, invoice_id = discount_world(tmp_db)
    offer = tmp_db.skonto_offer(invoice_id)
    assert (offer.error, offer.limit, offer.days, offer.percent) == (None, LIMIT, 14, Decimal('2.00'))


@pytest.mark.parametrize('extra, sentence', [
    ({'skonto_days': None, 'skonto_percent': None}, SKONTO_NO_TERMS), ({'mixed_tax_rates': 1}, MIXED_GROUPS_MISMATCH), ({'tax_rate': 0.16}, SKONTO_RATE),
    ({'skonto_days': None, 'skonto_percent': None, 'tax_rate': 0.16}, SKONTO_NO_TERMS), ({'skonto_days': None, 'skonto_percent': None, 'mixed_tax_rates': 1}, SKONTO_NO_TERMS),
], ids=['ohne-Bedingungen', 'Satz-je-Position-ohne-Positionen', '16-Prozent', 'ohne-Bedingungen-und-16-Prozent', 'ohne-Bedingungen-und-Satz-je-Position-ohne-Positionen'])
def test_the_offer_of_an_invoice_that_allows_no_discount_names_the_fixed_sentence(tmp_db, extra, sentence):
    _account, _buyer, invoice_id = discount_world(tmp_db, **extra)
    offer = tmp_db.skonto_offer(invoice_id)
    assert (offer.error, offer.limit, offer.days, offer.percent) == (sentence, None, None, None)


def test_the_offer_of_an_invoice_with_a_position_from_the_master_data_is_there_unless_its_revenue_account_is_a_discount_account(tmp_db):
    _account, _buyer, invoice_id = discount_world(tmp_db)
    tmp_db.insert_invoice_item({'invoice_id': invoice_id, 'position': 1, 'description': 'Anstrich', 'quantity': 1, 'unit': 'Stk.', 'price_per_unit': 100.0,
                                'total_net': 100.0, 'tax_category': 'S', 'tax_rate': 0.19, 'tax_rate_bp': 1900})
    assert tmp_db.skonto_offer(invoice_id).error is None and tmp_db.skonto_offer(invoice_id).limit == LIMIT
    run_sql(tmp_db, 'UPDATE InvoiceItems SET RevenueCoaId = (SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 4731) WHERE InvoiceId = ?', invoice_id)
    assert tmp_db.skonto_offer(invoice_id) == (SKONTO_REVENUE_DISCOUNT, None, None, None)


def test_the_offer_knows_no_deadline_and_no_status(tmp_db):
    """Die Frist wird nie geprüft; und auch ein Entwurf (nicht zahlbar) bekommt sein Angebot – die Seite zeigt es nur, wo sich zahlen lässt."""
    _account, _buyer, invoice_id = discount_world(tmp_db)
    run_sql(tmp_db, "UPDATE Invoices SET InvoiceDate = '2000-01-01', Status = 'draft' WHERE ID = ?", invoice_id)
    assert tmp_db.skonto_offer(invoice_id).limit == LIMIT


def test_the_offer_of_an_unknown_invoice_is_no_terms(tmp_db):
    assert tmp_db.skonto_offer(99999) == (SKONTO_NO_TERMS, None, None, None)


def test_the_limits_are_the_discounts_of_the_open_invoices_that_allow_one(tmp_db):
    account, buyer, with_discount = discount_world(tmp_db)
    add_invoice(tmp_db, 'R26002', 238.00, customer_id=buyer)                                           # ohne Skonto-Bedingungen
    add_invoice(tmp_db, 'R26003', 119.00, customer_id=buyer, status='draft', **TERMS)                  # kein zahlbarer Status
    add_invoice(tmp_db, 'R26004', 107.00, customer_id=buyer, tax_rate=0.07, **TERMS)                   # 7 %: 2 % von 107,00 € sind 2,14 €
    ids = {row['number']: row['id'] for row in open_rows(tmp_db).values()}
    assert tmp_db.skonto_limits() == {with_discount: LIMIT, ids['R26004']: 21400} and 'R26003' not in ids


def test_the_reads_for_the_discount_change_nothing(tmp_db):
    account, _buyer, invoice_id = discount_world(tmp_db)
    payment = add_payment(tmp_db, account, 116.62, '2026-03-10', 'Käufer AG', 'Danke')
    before = state(tmp_db, 'Bookings', 'Invoices', 'InvoicePayments', 'ChartOfAccounts')
    tmp_db.skonto_offer(invoice_id), tmp_db.skonto_limits(), tmp_db.suggest_invoices_for_booking(payment), tmp_db.suggest_invoice_payments()
    assert state(tmp_db, 'Bookings', 'Invoices', 'InvoicePayments', 'ChartOfAccounts') == before


# ── Laufzeit der Abfrage ─────────────────────────────────────────────────────

# Die Abfrage der offenen Rechnungen, wie sie vor 2.9.0 war (nur die sieben Spalten der Vorschläge, ohne die Angaben zum Skonto) – der Maßstab für „nicht teurer als“
_OPEN_INVOICES_BEFORE = _OPEN_INVOICES.replace(
    _OPEN_INVOICES[_OPEN_INVOICES.index('SELECT i.ID'):_OPEN_INVOICES.index('FROM Invoices i')],
    "SELECT i.ID, i.InvoiceNumber, i.InvoiceDate, COALESCE(i.AmountDue, i.SumGross, 0), i.CustomerId, i.BuyerCompany, i.BuyerName\n    ")


def _steps_of_the_open_invoices(db, count):
    """So viele Schritte brauchen die Abfrage der offenen Rechnungen von heute und die von vor 2.9.0 (``_OPEN_INVOICES_BEFORE``) bei ``count`` Rechnungen mit je drei Positionen (eine mit der
    Momentaufnahme eines Stammdaten-Satzes): ``(heute, vorher)``. Gezählt werden die Schritte der Datenbank (``set_progress_handler``), nicht die Zeit."""
    with closing(sqlite3.connect(db.db_name)) as conn:
        conn.executemany(
            'INSERT INTO Invoices (InvoiceNumber, InvoiceDate, SellerName, SellerCompany, BuyerName, BuyerCompany, TaxRate, SumNet, TaxAmount, SumGross, AmountDue, Status, '
            "DocumentType, SkontoDays, SkontoPercent) VALUES (?, '2026-03-04', 'a', 'b', 'c', 'd', 0.19, 1000000, 190000, 1190000, 1190000, 'sent', 'invoice', 14, 2.0)",
            [(f'S{n:05d}',) for n in range(count)])
        ids = [row[0] for row in conn.execute('SELECT ID FROM Invoices')]
        conn.executemany(
            'INSERT INTO InvoiceItems (InvoiceId, Position, Description, Quantity, PricePerUnit, TotalNet, TaxRate, TaxRateBp) VALUES (?, ?, ?, 1, 1, 1, 0.19, ?)',
            [(invoice_id, position, 'x', 1900 if position == 3 else None) for invoice_id in ids for position in (1, 2, 3)])
        conn.commit()
        # die Kontonummern der Skonto-Konten stehen als Platzhalter nur in der Abfrage von heute (im ``EXISTS`` der letzten Spalte), nicht in der von vor 2.9.0
        params = [STORNO_KIND, *PAYABLE_STATUSES, count + 10]
        steps = []
        for sql, sql_params in ((_OPEN_INVOICES, [*sorted(SKONTO_ACCOUNT_NUMBERS), *params]), (_OPEN_INVOICES_BEFORE, params)):
            ticks = []

            def tick():
                ticks.append(1)
                return 0                                                   # 0 = weiterrechnen
            conn.set_progress_handler(tick, 100)                           # alle 100 Schritte einmal
            rows = conn.execute(sql + ' ORDER BY i.InvoiceDate DESC, i.ID DESC LIMIT ?', sql_params).fetchall()
            conn.set_progress_handler(None, 0)
            assert len(rows) == count
            steps.append(len(ticks) * 100)
    return tuple(steps)


def test_the_query_of_the_open_invoices_costs_a_small_multiple_of_the_one_before_and_grows_with_their_number(tmp_db):
    """Das ``EXISTS`` je Rechnung (Satz oder Erlöskonto aus den Stammdaten an einer Position) läuft über den Index auf ``InvoiceItems.InvoiceId`` – ohne ihn las es je Rechnung alle Positionen (hier 400 × 1 200
    Zeilen). Gemessen an 400 Rechnungen mit je drei Positionen: knapp das Doppelte der Abfrage von vor 2.9.0, die Grenze liegt bei dem Dreifachen und bei 200 Schritten je Rechnung."""
    now, before = _steps_of_the_open_invoices(tmp_db, 400)
    assert now < before * 3, (now, before)
    assert now < 400 * 200, now
