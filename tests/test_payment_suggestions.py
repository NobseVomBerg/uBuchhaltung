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
* die Rechnungsseite („Vorhandene Buchung verknüpfen“: Vorschläge zuerst, Begründung klein unter dem Auftraggeber, der Haken bei gleichem Betrag bleibt).

Alle Namen, Nummern und Beträge sind erfundene Testwerte; Daten stehen fest im Test (kein „heute“).
"""
import json
import re
import shutil
import sqlite3
import subprocess
import urllib.parse
from contextlib import closing
from html import unescape

import pytest

from db import Database
from db.payment_matching import AMBIGUOUS_TEXT, MAX_SUGGESTIONS, REASON_AMOUNT, REASON_CUSTOMER, REASON_NUMBER
from db.payment_suggestions import _UNBOOKED_MOVEMENTS, MAX_INVOICES, MAX_MOVEMENTS, PaymentOverview
from money import fmt_eur
from server import handlers
from server.handlers import PAYMENT_NOT_ASSIGNED, handle_add_transaction
from server.pages import script_json
from server.pages_invoice import PageInvoice
from server.pages_transactions import PAY_FAILED, PAY_SUGGEST_JS, PAY_SUGGEST_LIMIT, PageTransactions
from tests.document_helpers import post_json, state, table_rows
from tests.dom_helpers import Dom, app_msg_arguments, assert_same_structure
from tests.entry_form_roundtrip import differences, open_form, snapshot
from tests.http_client import get, own_origin

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


def test_the_script_of_the_box_has_only_var_and_function_and_nothing_that_sets_markup():
    """Wie bei den übrigen Skripten der Seiten nur ``var`` und ``function`` (kein ``let``/``const``) und außerhalb der Funktion nichts Globales; in der Funktion gibt es
    lokale ``var`` (die Adresse für das Neuladen ohne ``err``)."""
    declarations = re.findall(r'^[ \t]*(var|let|const|function)\s+(\w+)', PAY_SUGGEST_JS, re.M)
    assert re.findall(r'^(var|let|const|function)\s+(\w+)', PAY_SUGGEST_JS, re.M) == [('function', 'payAssign')]            # nichts steht außerhalb der Funktion
    assert declarations[0] == ('function', 'payAssign') and {kind for kind, _name in declarations[1:]} <= {'var'}         # innen nur ``var`` (kein let, const)
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
