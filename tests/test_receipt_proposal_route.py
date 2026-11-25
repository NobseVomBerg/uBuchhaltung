# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Buchungsvorschlag aus einem gelesenen Beleg (2.10.1): Datenbank, Handler, Route, Seite und Skript.

Am verknüpften Beleg einer Bankbewegung steht der Knopf „Als Buchungssatz“. Er fragt ``GET /transactions/receipt-proposal?id=<Bankbewegung>&document=<Beleg>``
und trägt das Ergebnis in eine NEUE Zeile des leeren Buchungssatz-Editors ein – gespeichert wird nichts; das geschieht allein mit „Aktualisieren“ über
den bestehenden Weg. Die Regel selbst steht rein in ``db/receipt_proposal.py`` (``tests/test_receipt_proposal_pure.py``).

* Die Datenbank (``recipient_history_groups``, ``vat_history_groups``, ``receipt_proposal_inputs``): rein lesend; die Historie des Empfängers nur
  über die exakte Abfrage von ``find_history_suggestion`` – der Rückgriff über das längste Wort bleibt dort –, die des Absenders nur über die
  bereinigte USt-IdNr. und nur Belege der Art ``receipt``; die Eingaben nie mit ``Text`` oder ``Raw`` der Lesung. ``find_history_suggestion`` selbst
  liefert nach dem Refactor dieselben Daten wie vorher, dazu je Satz ``payment``.
* Der Handler (``handlers.handle_receipt_proposal``) und die Route (``app._SEARCH_PATHS``, wie die Suchen): GET, rein lesend, nur von den eigenen
  Seiten, nie aus einem Cache; die IDs über ``_id_param``; nie ein Absender, eine Rechnungsnummer, ein Dateiname, ein Pfad oder ein Text des Belegs
  in der Antwort; ein unerwarteter Fehler ist ein fester Satz.
* Die Seite und das Skript: Der Knopf steht nur in der Bankmaske, am Beleg der Art ``receipt``; das Skript füllt nur einen leeren Editor und nur
  mit der Antwort zur geöffneten Maske (Node-Läufe unten; ohne Node entfallen nur diese).

Alle Namen, Nummern, USt-IdNrn. und Beträge sind erfundene Testwerte; Daten stehen fest im Test (kein „heute“).
"""
import json
import os
import shutil
import subprocess
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import receipt_reading
import userctx
from db import Database, document_readings, receipt_proposal
from db.invoice_payments import PAYMENT_MARK
from db.receipt_proposal import (
    DONE_TEXT, HINT_ACCOUNT_CONFLICT, HINT_ACCOUNT_HIDDEN, HINT_ACCOUNT_KIND, HINT_NO_ACCOUNT, HINT_TAX, HINT_TAX_RATE, REASON_ACCOUNT_RECIPIENT,
    REASON_ACCOUNT_SENDER, REASON_AMOUNT, REASON_TAX, REASON_TAX_NONE, REFUSE_AMOUNT, REFUSE_CURRENCY, REFUSE_DIRECTION, REFUSE_HAS_ROWS,
    REFUSE_NO_READING, REFUSE_OUTDATED)
from server import app as server_app
from server import handlers, pages_transactions, receipt_files, receipt_links
from server.pages_transactions import TX_LINK_JS, PageTransactions
from tests import installation
from tests.dom_helpers import Dom
from tests.entry_form_helpers import bank_account, coa, execute, query
from tests.entry_form_roundtrip import add_row, open_form, snapshot, submit
from tests.http_client import FOREIGN_ORIGIN, get, own_origin, post
from tests.test_receipt_links import script_data, walk
from tests.test_receipt_upload import demo_site
from tools import mcp_ubuchhaltung as mcp

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – nur die Läufe des Skripts entfallen')

DAY = '2026-03-04'                          # das Datum der Bewegung und des Belegs in den Tests
GROSS = 1190000                             # 119,00 EUR in Minor Units (money.SCALE = 4)
NET, TAX = 1000000, 190000
FILE = 'Belege/2026/B-1_beleg.pdf'
VAT = 'DE123456789'                         # eine erfundene USt-IdNr. (Whitelist von tests/check_anonymized.py)
OTHER_VAT = 'DE987654321'
TAX_NUMBER = '12/345/67890'                 # eine Steuernummer: keine USt-IdNr.
SUPPLIER = 'Muster Bürobedarf GmbH'
HOSTILE = '</script><img src=x onerror=1> Absender Probe GmbH RE-2026-17'
SECRET_TEXT = 'GEHEIMER-TEXT-DER-DATEI Rechnung 4711'


# ── Hilfen ───────────────────────────────────────────────────────────────────
def reading(**over):
    """Eine Lesung (Schlüssel = Spalten von ``DocumentReadings``) über 119,00 EUR mit einem Satz von 19 %; ``over`` überschreibt Felder."""
    row = {'Status': 'ok', 'Source': 'text', 'ReaderVersion': receipt_reading.READER_VERSION, 'ReadAt': '2026-03-05 10:00:00', 'DocDate': DAY,
           'GrossMinor': GROSS, 'NetMinor': NET, 'TaxMinor': TAX, 'TaxRate': 0.19, 'Currency': 'EUR', 'SenderName': None,
           'SenderVatId': None, 'InvoiceNumber': None, 'Kind': 'invoice', 'Raw': None, 'Text': None}
    row.update(over)
    return row


def add_receipt(db, number='B-1', date=DAY, path=FILE, filename='beleg.pdf', info=''):
    """Einen Beleg anlegen; liefert seine ID."""
    db.insert_receipt(number, date, filename, path, info)
    return db.fetch_receipts()[-1][0]


def save_reading(db, receipt, **over):
    assert db.save_document_reading(receipt, FILE, reading(**over))


def add_bank(db, acct=None, date=DAY, amount='-119.00', recipient=SUPPLIER, **fields):
    """Eine Bankbewegung (ohne Sätze); liefert ihre ID."""
    return db.insert_booking(date, amount, account_id=acct, booking_type='bank', recipient_client=recipient, **fields)


def add_child(db, bank, number, amount='-119.00', *, date=None, recipient=SUPPLIER, **fields):
    """Ein Satz unter der Bankbewegung auf dem Konto ``number`` („Konto an Bank“); liefert seine ID."""
    date = date or query(db, 'SELECT DateBooking FROM Bookings WHERE ID = ?', (bank,))[0][0]
    return db.insert_booking(date, amount, coa_id=coa(db, number), counter_coa_id=coa(db, 1800), recipient_client=recipient,
                             booking_type='entry', parent_booking_id=bank, **fields)


def past(db, acct, date, number, *, recipient=SUPPLIER, amount='-119.00', receipt=None, children=None):
    """Eine frühere Bankbewegung mit EINEM Satz auf dem Konto ``number`` (oder mit ``children``: einer Liste von ``(Konto, Betrag)``), wenn ``receipt``
    gegeben ist mit diesem Beleg (Art ``receipt``) verknüpft; liefert die ID der Bankbewegung."""
    bank = add_bank(db, acct, date, amount, recipient)
    for child_number, child_amount in (children or [(number, amount)]):
        add_child(db, bank, child_number, child_amount, date=date, recipient=recipient)
    if receipt is not None:
        db.link_receipt_to_booking(bank, receipt)
    return bank


@pytest.fixture
def db(tmp_db):
    return tmp_db


@pytest.fixture
def acct(db):
    return bank_account(db, 'Probe-Bank', 1800)


def numbers_of(db, groups):
    """Die Konten (Kontonummern) einer Historie: je Gruppe die Nummern ihrer Sätze."""
    names = db.get_coa_id_to_number_map()
    return [[names[row['coa_id']] for row in group] for group in groups]


# ── find_history_suggestion: dieselben Daten wie vor dem Refactor, dazu ``payment`` ───────────────
def history_world(db, acct):
    """Dieselben Daten wie in dem Lauf vor dem Refactor (Szenarien: exakt, Rückgriff über das längste Wort, Split mit Umbuchung, Spiegel, Einnahme mit
    dem Geldkonto vorn, ausgeschlossene eigene Buchung, Bankbewegung mit eigenem Konto ohne Sätze, Empfänger nur am Elternteil)."""
    bank_coa = coa(db, 1800)
    ids = {}
    ids['o2'] = db.insert_booking('2026-01-15', -29.99, coa_id=coa(db, 6805), recipient_client='O2 Germany GmbH', tax_rate=0.19, tax_amount=-4.79,
                                  text='Rechnung', document_number='R-1', booking_type='entry')
    ids['telefonica'] = db.insert_booking('2026-01-16', -12.00, coa_id=coa(db, 6815), recipient_client='Telefonica Germany GmbH & Co. OHG',
                                          booking_type='entry')
    ids['split'] = db.insert_booking('2026-01-02', -29.99, account_id=acct, booking_type='bank', recipient_client='Split Partner')
    db.insert_booking('2026-01-02', -15.00, coa_id=coa(db, 6805), counter_coa_id=bank_coa, tax_rate=0.19, tax_amount=-2.39,
                      recipient_client='Split Partner', text='Anteil Betrieb', booking_type='entry', parent_booking_id=ids['split'])
    db.insert_booking('2026-01-02', 7.50, coa_id=coa(db, 2100), counter_coa_id=coa(db, 6805), recipient_client='Split Partner',
                      text='Privatanteil', booking_type='entry', parent_booking_id=ids['split'])
    db.insert_booking('2026-01-02', -14.99, coa_id=coa(db, 2100), counter_coa_id=bank_coa, recipient_client='Split Partner',
                      booking_type='entry', parent_booking_id=ids['split'])
    ids['mirror'] = db.insert_booking('2026-01-03', -50.00, account_id=acct, booking_type='bank', recipient_client='Bueroshop')
    db.insert_booking('2026-01-03', -50.00, coa_id=bank_coa, counter_coa_id=bank_coa, booking_type='entry', parent_booking_id=ids['mirror'])
    db.insert_booking('2026-01-03', -50.00, coa_id=coa(db, 6815), counter_coa_id=bank_coa, booking_type='entry', parent_booking_id=ids['mirror'])
    ids['income'] = db.insert_booking('2026-01-04', 1190.00, account_id=acct, booking_type='bank', recipient_client='Kunde AG')
    db.insert_booking('2026-01-04', 1190.00, coa_id=bank_coa, counter_coa_id=coa(db, 4400), booking_type='entry', parent_booking_id=ids['income'])
    ids['older'] = db.insert_booking('2026-01-05', -10.00, coa_id=coa(db, 6815), recipient_client='Zweimal GmbH', booking_type='entry')
    ids['newer'] = db.insert_booking('2026-02-05', -10.00, coa_id=coa(db, 6805), recipient_client='Zweimal GmbH', booking_type='entry')
    ids['only_bank'] = db.insert_booking('2026-01-06', -5.00, account_id=acct, coa_id=coa(db, 6815), booking_type='bank', recipient_client='Nur Bank')
    ids['parent'] = db.insert_booking('2026-01-07', -20.00, account_id=acct, booking_type='bank', recipient_client='Nur Eltern')
    db.insert_booking('2026-01-07', -20.00, coa_id=coa(db, 6815), counter_coa_id=bank_coa, booking_type='entry', parent_booking_id=ids['parent'])
    return ids


def line(db, number, amount, *, counter=None, nobank=False, rate=None, tax=None, text='', docnr='', payment=False):
    return {'coa_id': coa(db, number), 'counter_coa_id': None if counter is None else coa(db, counter), 'nobank': nobank, 'amount': Decimal(amount),
            'tax_rate': rate, 'tax_amount': None if tax is None else Decimal(tax), 'text': text, 'document_number': docnr, 'payment': payment}


def hit(source, date, recipient, *rows):
    return {'source_id': source, 'date': date, 'recipient': recipient, 'is_split': len(rows) > 1, 'rows': list(rows)}


def test_the_history_suggestion_gives_the_same_data_as_before_the_refactor_and_now_the_key_payment(db, acct):
    ids = history_world(db, acct)
    split = hit(ids['split'], '2026-01-02', 'Split Partner',
                line(db, 6805, '-15.00', rate=0.19, tax='-2.39', text='Anteil Betrieb'),
                line(db, 2100, '7.50', counter=6805, nobank=True, text='Privatanteil'), line(db, 2100, '-14.99'))
    o2 = hit(ids['o2'], '2026-01-15', 'O2 Germany GmbH', line(db, 6805, '-29.99', rate=0.19, tax='-4.79', text='Rechnung', docnr='R-1'))
    telefonica = hit(ids['telefonica'], '2026-01-16', 'Telefonica Germany GmbH & Co. OHG', line(db, 6815, '-12.00'))
    other = ids['split']                                    # eine unbeteiligte Bewegung als Ausschluss
    expected = [
        # Exakt: Leerraum und Schreibweise zählen nicht
        (('O2 Germany GmbH', other), o2), (('  o2   germany gmbh ', other), o2), (('O2GERMANYGMBH', other), o2),
        # Rückgriff über das längste Wort (ab vier Zeichen): der jüngste Treffer nach Datum
        (('TELEFONICA GERMANY', other), telefonica), (('Telefonica', other), telefonica), (('Germany', other), telefonica),
        (('Gmbh', other), hit(ids['newer'], '2026-02-05', 'Zweimal GmbH', line(db, 6805, '-10.00'))),
        # Split mit Umbuchung, Spiegel ausgeblendet, Einnahme (das Zweckkonto, nicht das Bankkonto), Empfänger nur am Elternteil
        (('Split Partner', None), split), (('Split Partner', 999999), split), (('Split Partner', ids['split']), None),
        (('Bueroshop', None), hit(ids['mirror'], '2026-01-03', 'Bueroshop', line(db, 6815, '-50.00'))),
        (('Kunde AG', None), hit(ids['income'], '2026-01-04', 'Kunde AG', line(db, 4400, '1190.00'))),
        (('Nur Eltern', None), hit(ids['parent'], '2026-01-07', 'Nur Eltern', line(db, 6815, '-20.00'))),
        # die eigene Buchung wird ausgeschlossen
        (('Zweimal GmbH', ids['newer']), hit(ids['older'], '2026-01-05', 'Zweimal GmbH', line(db, 6815, '-10.00'))),
        (('Zweimal GmbH', ids['older']), hit(ids['newer'], '2026-02-05', 'Zweimal GmbH', line(db, 6805, '-10.00'))),
        # nichts: eine Bankbewegung mit Konto, aber ohne Sätze, unbekannt, leer, zu kurz für den Rückgriff
        (('Nur Bank', None), None), (('Nur Bank', other), None), (('Unbekannt XY', None), None), (('', None), None), ((None, None), None),
        (('ab', None), None),
        # ohne Ausschluss (-1) trifft ``COALESCE(ParentBooking_ID, -1) != -1`` keinen eigenständigen Satz – wie vorher
        (('O2 Germany GmbH', None), None), (('Germany', None), None), (('Zweimal GmbH', None), None)]
    for (recipient, exclude), want in expected:
        got = db.find_history_suggestion(recipient, exclude_booking_id=exclude)
        assert got == want, (recipient, exclude)
        assert (got is None) or all(set(row) == set(line(db, 6805, '0')) for row in got['rows'])


def test_a_payment_row_of_the_history_carries_the_key_payment(db, acct):
    bank = past(db, acct, '2026-01-02', 6815, recipient='Zahler GmbH')
    marked = query(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', (bank,))[0][0]
    execute(db, 'UPDATE Bookings SET Status = ? WHERE ID = ?', (PAYMENT_MARK, marked))
    got = db.find_history_suggestion('Zahler GmbH', exclude_booking_id=999999)
    assert [row['payment'] for row in got['rows']] == [True]
    past(db, acct, '2026-01-03', 6815, recipient='Anderer GmbH')
    assert [row['payment'] for row in db.find_history_suggestion('Anderer GmbH', exclude_booking_id=999999)['rows']] == [False]


# ── recipient_history_groups ────────────────────────────────────────────────
def test_the_recipient_history_finds_the_exact_recipient_but_never_a_similar_one(db, acct):
    past(db, acct, '2026-01-10', 6815, recipient='Muster Bürobedarf GmbH')
    me = add_bank(db, acct, '2026-03-04', recipient='Muster Bürobedarf')
    assert numbers_of(db, db.recipient_history_groups('Muster Bürobedarf GmbH', me, 3)) == [[6815]]
    assert db.recipient_history_groups('Muster Bürobedarf', me, 3) == []                      # kein Rückgriff über das längste Wort
    assert db.find_history_suggestion('Muster Bürobedarf', exclude_booking_id=me)['recipient'] == 'Muster Bürobedarf GmbH'        # den gibt es nur dort
    assert numbers_of(db, db.recipient_history_groups('  MUSTER   BürOBEDARF gmbh ', me, 3)) == [[6815]]       # Leerraum und ASCII-Schreibweise zählen nicht


def test_an_empty_recipient_has_no_history(db, acct):
    past(db, acct, '2026-01-10', 6815, recipient='Muster Bürobedarf GmbH')
    me = add_bank(db, acct)
    assert db.recipient_history_groups('', me, 3) == [] and db.recipient_history_groups(None, me, 3) == []
    assert db.recipient_history_groups('   ', me, 3) == []


def test_the_history_is_the_youngest_first_and_at_most_depth_groups(db, acct):
    for number, date in ((4400, '2026-01-10'), (6805, '2026-02-10'), (6815, '2026-03-01'), (2100, '2026-01-20')):
        past(db, acct, date, number)
    me = add_bank(db, acct, '2026-03-04')
    assert numbers_of(db, db.recipient_history_groups(SUPPLIER, me, 10)) == [[6815], [6805], [2100], [4400]]
    assert numbers_of(db, db.recipient_history_groups(SUPPLIER, me, 3)) == [[6815], [6805], [2100]]
    assert numbers_of(db, db.recipient_history_groups(SUPPLIER, me, 1)) == [[6815]]


def test_a_split_is_one_group_and_a_standalone_entry_is_one_group_too(db, acct):
    past(db, acct, '2026-01-10', None, children=[(6815, '-60.00'), (6805, '-59.00')])
    db.insert_booking('2026-02-01', -5.00, coa_id=coa(db, 4400), recipient_client=SUPPLIER, booking_type='entry')
    me = add_bank(db, acct, '2026-03-04')
    groups = db.recipient_history_groups(SUPPLIER, me, 5)
    assert numbers_of(db, groups) == [[4400], [6815, 6805]]
    assert all(set(row) >= {'coa_id', 'nobank', 'payment'} for group in groups for row in group)


def test_the_booking_itself_and_its_rows_are_not_part_of_the_history(db, acct):
    past(db, acct, '2026-01-10', 6815)
    me = past(db, acct, '2026-03-04', 4400)                       # die Bewegung selbst hat schon einen Satz
    assert numbers_of(db, db.recipient_history_groups(SUPPLIER, me, 3)) == [[6815]]
    assert numbers_of(db, db.recipient_history_groups(SUPPLIER, 999999, 3)) == [[4400], [6815]]


def test_a_mirror_a_transfer_and_a_payment_row_are_rows_the_rule_can_see(db, acct):
    bank_coa = coa(db, 1800)
    mirror = add_bank(db, acct, '2026-01-02')
    db.insert_booking('2026-01-02', -119.00, coa_id=bank_coa, counter_coa_id=bank_coa, booking_type='entry', parent_booking_id=mirror,
                      recipient_client=SUPPLIER)
    only_mirror = numbers_of(db, db.recipient_history_groups(SUPPLIER, 999999, 3))
    assert only_mirror == []                                         # ein reiner Spiegel gibt keine Zeile: die Gruppe ist leer und zählt nicht
    transfer = add_bank(db, acct, '2026-01-03')
    db.insert_booking('2026-01-03', 7.50, coa_id=coa(db, 2100), counter_coa_id=coa(db, 6805), recipient_client=SUPPLIER, booking_type='entry',
                      parent_booking_id=transfer)
    paid = add_bank(db, acct, '2026-01-04')
    marked = add_child(db, paid, 6815, date='2026-01-04')
    execute(db, 'UPDATE Bookings SET Status = ? WHERE ID = ?', (PAYMENT_MARK, marked))
    groups = db.recipient_history_groups(SUPPLIER, 999999, 3)
    assert [[(row['nobank'], row['payment']) for row in group] for group in groups] == [[(False, True)], [(True, False)]]


def test_the_history_never_reads_more_than_the_scan_limit_of_hits(db, acct, monkeypatch):
    monkeypatch.setattr(receipt_proposal, 'HISTORY_SCAN', 2)
    for number, date in ((4400, '2026-01-10'), (6805, '2026-02-10'), (6815, '2026-03-01')):
        past(db, acct, date, number)
    me = add_bank(db, acct, '2026-03-04')
    assert numbers_of(db, db.recipient_history_groups(SUPPLIER, me, 10)) == [[6815], [6805]]


# ── vat_history_groups ──────────────────────────────────────────────────────
def a_receipt(db, number, vat=VAT, **over):
    receipt = add_receipt(db, number, path=f'Belege/2026/{number}_beleg.pdf')
    assert db.save_document_reading(receipt, f'Belege/2026/{number}_beleg.pdf', reading(SenderVatId=vat, **over))
    return receipt


def test_the_sender_history_finds_earlier_receipts_with_the_same_vat_id(db, acct):
    first, second = a_receipt(db, 'B-1'), a_receipt(db, 'B-2')
    past(db, acct, '2026-01-10', 6815, recipient='Erster Zahler', receipt=first)
    past(db, acct, '2026-02-10', 6805, recipient='Zweiter Zahler', receipt=second)
    current = a_receipt(db, 'B-3')
    me = add_bank(db, acct, '2026-03-04', recipient='Dritter Zahler')
    db.link_receipt_to_booking(me, current)
    groups = db.vat_history_groups(VAT, current, me, 3)
    assert numbers_of(db, groups) == [[6805], [6815]]                  # die jüngste zuerst; der Empfänger der Bank spielt keine Rolle


def test_the_vat_id_is_compared_in_its_cleaned_form(db, acct):
    first, second = a_receipt(db, 'B-1', vat='DE 123 456 789'), a_receipt(db, 'B-2', vat=' de-123.456.789 ')
    past(db, acct, '2026-01-10', 6815, receipt=first)
    past(db, acct, '2026-02-10', 6815, receipt=second)
    assert numbers_of(db, db.vat_history_groups(VAT, 999999, 999999, 3)) == [[6815], [6815]]


def test_another_vat_id_a_tax_number_and_an_unusable_reading_give_no_source(db, acct):
    other = a_receipt(db, 'B-1', vat=OTHER_VAT)
    number = a_receipt(db, 'B-2', vat=TAX_NUMBER)
    no_text = a_receipt(db, 'B-3', Status='no_text')
    empty = a_receipt(db, 'B-4', vat=None)
    blank = a_receipt(db, 'B-5', vat='')
    for date, receipt in (('2026-01-10', other), ('2026-01-11', number), ('2026-01-12', no_text), ('2026-01-13', empty), ('2026-01-14', blank)):
        past(db, acct, date, 6815, receipt=receipt)
    assert db.vat_history_groups(VAT, 999999, 999999, 3) == []
    assert db.vat_history_groups(TAX_NUMBER, 999999, 999999, 3) == []                      # eine Steuernummer ist keine Quelle, auch nicht für sich selbst
    assert numbers_of(db, db.vat_history_groups(OTHER_VAT, 999999, 999999, 3)) == [[6815]]


def test_only_a_link_of_the_kind_receipt_counts_and_the_own_receipt_never(db, acct):
    contract, invoice, plain, own = (a_receipt(db, f'B-{n}') for n in (1, 2, 3, 4))
    for date, receipt, kind in (('2026-01-10', contract, 'contract'), ('2026-01-11', invoice, 'invoice'), ('2026-01-12', plain, 'receipt'),
                                ('2026-01-13', own, 'receipt')):
        bank = past(db, acct, date, 6815)
        db.link_receipt_to_booking(bank, receipt, kind)
    groups = db.vat_history_groups(VAT, own, 999999, 5)
    assert numbers_of(db, groups) == [[6815]]                                # nur der gewöhnliche Beleg: nicht Vertrag, nicht Rechnung, nicht der eigene


def test_the_current_booking_and_its_rows_are_never_part_of_the_sender_history(db, acct):
    earlier = a_receipt(db, 'B-1')
    past(db, acct, '2026-01-10', 6815, receipt=earlier)
    also = a_receipt(db, 'B-2')
    me = past(db, acct, '2026-03-04', 4400)
    db.link_receipt_to_booking(me, also)                                      # derselbe Absender, aber an der Bewegung selbst
    child = query(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', (me,))[0][0]
    third = a_receipt(db, 'B-3')
    db.link_receipt_to_booking(child, third)                                  # und an ihrem Satz
    assert numbers_of(db, db.vat_history_groups(VAT, 999999, me, 5)) == [[6815]]


def test_a_receipt_on_a_row_makes_the_group_of_its_bank_movement(db, acct):
    receipt = a_receipt(db, 'B-1')
    bank = past(db, acct, '2026-01-10', None, children=[(6815, '-60.00'), (6805, '-59.00')])
    child = query(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', (bank,))[0][0]
    db.link_receipt_to_booking(child, receipt)
    solo = db.insert_booking('2026-02-01', -5.00, coa_id=coa(db, 4400), recipient_client='Egal', booking_type='entry')
    other = a_receipt(db, 'B-2')
    db.link_receipt_to_booking(solo, other)
    assert numbers_of(db, db.vat_history_groups(VAT, 999999, 999999, 5)) == [[4400], [6815, 6805]]


def test_the_sender_history_is_the_youngest_first_and_at_most_depth_groups(db, acct):
    for number, (account, date) in enumerate(((4400, '2026-01-10'), (6805, '2026-02-10'), (6815, '2026-03-01'), (2100, '2026-01-20')), start=1):
        past(db, acct, date, account, receipt=a_receipt(db, f'B-{number}'))
    assert numbers_of(db, db.vat_history_groups(VAT, 999999, 999999, 3)) == [[6815], [6805], [2100]]
    assert numbers_of(db, db.vat_history_groups(VAT, 999999, 999999, 1)) == [[6815]]


def test_the_sender_history_looks_at_no_more_than_the_newest_receipts(db, acct, monkeypatch):
    monkeypatch.setattr(document_readings, 'VAT_DOCS_MAX', 2)
    for number, account in enumerate((4400, 6805, 6815), start=1):
        past(db, acct, f'2026-0{number}-10', account, receipt=a_receipt(db, f'B-{number}'))
    assert numbers_of(db, db.vat_history_groups(VAT, 999999, 999999, 10)) == [[6815], [6805]]        # der älteste Beleg (4400) fällt weg


def test_a_vat_id_that_nobody_carries_gives_no_groups_and_no_query_for_bookings(db, acct, monkeypatch):
    a_receipt(db, 'B-1', vat=OTHER_VAT)
    statements = []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    assert db.vat_history_groups(VAT, 999999, 999999, 3) == []
    assert statements and not [statement for statement in statements if 'BookingDocuments' in statement]


# ── receipt_proposal_inputs ─────────────────────────────────────────────────
READING_KEYS = {'Status', 'ReaderVersion', 'GrossMinor', 'NetMinor', 'TaxMinor', 'TaxRate', 'Currency', 'Kind', 'SenderVatId'}


def test_the_inputs_of_a_bank_movement_with_a_linked_receipt(db, acct):
    receipt = add_receipt(db)
    save_reading(db, receipt, SenderName=HOSTILE, InvoiceNumber=HOSTILE, Text=SECRET_TEXT, Raw=json.dumps({'fields': {'x': SECRET_TEXT}}))
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    got = db.receipt_proposal_inputs(bank, receipt)
    assert set(got) == {'bank', 'has_children', 'document', 'linked', 'reading'}
    assert got['bank'] == (-GROSS, 'EUR', SUPPLIER)
    assert (got['has_children'], got['document'], got['linked']) == (False, True, True)
    assert set(got['reading']) == READING_KEYS                                 # nie ``Text``, ``Raw``, Absender oder Nummer
    assert got['reading']['GrossMinor'] == GROSS and got['reading']['TaxRate'] == 0.19 and got['reading']['Kind'] == 'invoice'
    assert SECRET_TEXT not in repr(got) and HOSTILE not in repr(got)


def test_the_inputs_read_only_and_never_the_text_or_the_raw_data(db, acct, monkeypatch):
    receipt = add_receipt(db)
    save_reading(db, receipt, Text=SECRET_TEXT, Raw='{}')
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    statements = []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    db.receipt_proposal_inputs(bank, receipt)
    monkeypatch.setattr(Database, '_get_connection', real)
    assert statements and all(statement.lstrip().upper().startswith('SELECT') for statement in statements), statements
    for statement in statements:
        assert 'Raw' not in statement and 'Text' not in statement, statement
        assert 'SenderName' not in statement and 'InvoiceNumber' not in statement and 'Path' not in statement and 'Filename' not in statement


@pytest.mark.parametrize('what', ['unknown', 'entry'])
def test_something_that_is_no_bank_movement_has_no_bank_data(db, acct, what):
    receipt = add_receipt(db)
    save_reading(db, receipt)
    if what == 'entry':
        booking = db.insert_booking(DAY, -119.00, booking_type='entry', recipient_client=SUPPLIER)
        db.link_receipt_to_booking(booking, receipt)
    else:
        booking = 999999
    got = db.receipt_proposal_inputs(booking, receipt)
    assert got['bank'] is None and got['document'] is True and got['reading'] is not None


def test_a_row_under_the_movement_counts_also_a_locked_payment_row(db, acct):
    receipt = add_receipt(db)
    free = add_bank(db, acct)
    paid = add_bank(db, acct, '2026-03-05')
    marked = add_child(db, paid, 6815)
    execute(db, 'UPDATE Bookings SET Status = ? WHERE ID = ?', (PAYMENT_MARK, marked))
    assert db.receipt_proposal_inputs(free, receipt)['has_children'] is False
    assert db.receipt_proposal_inputs(paid, receipt)['has_children'] is True
    plain = add_bank(db, acct, '2026-03-06')
    add_child(db, plain, 6815)
    assert db.receipt_proposal_inputs(plain, receipt)['has_children'] is True


def test_only_a_link_of_the_kind_receipt_to_the_movement_itself_is_linked(db, acct):
    receipt = add_receipt(db)
    bank = add_bank(db, acct)
    assert db.receipt_proposal_inputs(bank, receipt)['linked'] is False
    for kind in ('contract', 'invoice', 'other'):
        db.link_receipt_to_booking(bank, receipt, kind)
        assert db.receipt_proposal_inputs(bank, receipt)['linked'] is False, kind
        assert db.unlink_receipt_from_booking(bank, receipt) is True
    db.link_receipt_to_booking(bank, receipt)
    assert db.receipt_proposal_inputs(bank, receipt)['linked'] is True
    child = add_child(db, bank, 6815)
    other = add_receipt(db, 'B-2')
    db.link_receipt_to_booking(child, other)
    assert db.receipt_proposal_inputs(bank, other)['linked'] is False                            # an einem Satz hängt er, nicht an der Bewegung


def test_an_unknown_receipt_has_no_document_no_link_and_no_reading(db, acct):
    bank = add_bank(db, acct)
    got = db.receipt_proposal_inputs(bank, 999999)
    assert got['bank'] == (-GROSS, 'EUR', SUPPLIER)
    assert (got['document'], got['linked'], got['reading']) == (False, False, None)
    receipt = add_receipt(db)
    assert db.receipt_proposal_inputs(bank, receipt)['reading'] is None                             # ein Beleg ohne Lesung


def test_the_amount_and_the_currency_of_the_bank_movement_come_as_stored(db, acct):
    bank = add_bank(db, acct, amount='-119.00', currency='USD')
    assert db.receipt_proposal_inputs(bank, 1)['bank'] == (-GROSS, 'USD', SUPPLIER)
    no_recipient = add_bank(db, acct, recipient='')
    assert db.receipt_proposal_inputs(no_recipient, 1)['bank'][2] == ''


# ── Der Handler ─────────────────────────────────────────────────────────────
def a_world(db, acct):
    """Eine Bankbewegung über −119,00 EUR an einen bekannten Empfänger mit verknüpftem Beleg (Lesung: eine Rechnung über 119,00 EUR mit 19 %) und zwei früheren Bewegungen
    desselben Empfängers, beide auf dem Konto 6815. Liefert ``(Bankbewegung, Beleg, ID des Kontos 6815)``."""
    past(db, acct, '2026-01-10', 6815)
    past(db, acct, '2026-02-10', 6815)
    receipt = add_receipt(db)
    save_reading(db, receipt)
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    return bank, receipt, coa(db, 6815)


def propose(db, bank, receipt):
    status, body = handlers.handle_receipt_proposal(db, bank, receipt)
    return status, json.loads(body)


def answer(db, bank, receipt):
    """Die Antwort einer gelungenen Anfrage (200, ``success``)."""
    status, data = propose(db, bank, receipt)
    assert status == 200 and data['success'] is True and set(data) == {'success', 'ok', 'message', 'reasons', 'hints', 'row'}, (status, data)
    return data


def test_a_full_hit_gives_the_amount_of_the_bank_the_tax_of_the_reading_and_the_account_of_the_history(db, acct):
    bank, receipt, account = a_world(db, acct)
    assert answer(db, bank, receipt) == {
        'success': True, 'ok': True, 'message': DONE_TEXT, 'hints': [],
        'reasons': [REASON_AMOUNT, REASON_TAX.format(rate='19'), REASON_ACCOUNT_RECIPIENT],
        'row': {'amount': '-119.00', 'coa_id': account, 'tax_rate': '19', 'tax_amount': '-19.00'}}


def test_the_row_has_the_keys_of_the_suggestion_of_the_history_so_that_the_same_script_takes_them(db, acct):
    bank, receipt, _account = a_world(db, acct)
    row = answer(db, bank, receipt)['row']
    status, body = handlers.handle_transaction_suggest(db, {'id': ['0'], 'recipient': [SUPPLIER]})
    history_row = json.loads(body)['rows'][0]
    assert status == 200 and {'amount', 'coa_id', 'tax_rate', 'tax_amount'} == set(row) <= set(history_row)


def test_a_credit_note_with_an_incoming_payment_is_a_positive_row_with_a_positive_tax(db, acct):
    past(db, acct, '2026-01-10', 4400, amount='119.00')
    past(db, acct, '2026-02-10', 4400, amount='119.00')
    receipt = add_receipt(db)
    save_reading(db, receipt, Kind='credit_note')
    bank = add_bank(db, acct, amount='119.00')
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['ok'] is True and data['row'] == {'amount': '119.00', 'coa_id': coa(db, 4400), 'tax_rate': '19', 'tax_amount': '19.00'}


def test_the_reduced_rate_with_a_decimal_place_is_shown_with_a_point_in_the_row_and_a_comma_in_the_reason(db, acct):
    db.insert_tax_rate(label='5,5 % (erfunden)', bp=550, revenue_coa_id=coa(db, 4400))                 # ein Satz der Stammdaten: nur so wird er übernommen
    receipt = add_receipt(db)
    save_reading(db, receipt, TaxRate=0.055, NetMinor=2000000, TaxMinor=110000, GrossMinor=2110000)
    bank = add_bank(db, acct, amount='-211.00')
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['row']['tax_rate'] == '5.5' and data['row']['tax_amount'] == '-11.00' and data['row']['amount'] == '-211.00'
    assert REASON_TAX.format(rate='5,5') in data['reasons']


def test_a_reading_without_tax_gives_the_row_without_a_rate_and_an_amount(db, acct):
    receipt = add_receipt(db)
    save_reading(db, receipt, TaxRate=0.0, NetMinor=GROSS, TaxMinor=0)
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['row']['tax_rate'] == '' and data['row']['tax_amount'] == '' and REASON_TAX_NONE in data['reasons'] and HINT_TAX not in data['hints']


def test_several_rates_give_the_amount_without_a_tax_and_the_hint(db, acct):
    receipt = add_receipt(db)
    save_reading(db, receipt, TaxRate=None)
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['ok'] is True and data['row'] == {'amount': '-119.00', 'coa_id': '', 'tax_rate': '', 'tax_amount': ''}
    assert data['hints'] == [HINT_TAX, HINT_NO_ACCOUNT]


@pytest.mark.parametrize('over', [{'TaxMinor': TAX + 200, 'GrossMinor': GROSS}, {'NetMinor': None}, {'TaxRate': 1.5}, {'TaxMinor': None}])
def test_a_reading_whose_checks_fail_gives_no_tax(db, acct, over):
    receipt = add_receipt(db)
    save_reading(db, receipt, **over)
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['ok'] is True and data['row']['tax_rate'] == '' and data['row']['tax_amount'] == '' and HINT_TAX in data['hints']


def test_one_earlier_booking_is_no_ground_for_an_account(db, acct):
    past(db, acct, '2026-01-10', 6815)
    receipt = add_receipt(db)
    save_reading(db, receipt)
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['row']['coa_id'] == '' and data['hints'] == [HINT_NO_ACCOUNT] and data['row']['tax_rate'] == '19'


def test_two_earlier_bookings_with_different_accounts_are_no_ground_either(db, acct):
    past(db, acct, '2026-01-10', 6815)
    past(db, acct, '2026-02-10', 6805)
    receipt = add_receipt(db)
    save_reading(db, receipt)
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['row']['coa_id'] == '' and data['hints'] == [HINT_NO_ACCOUNT]


def test_a_recipient_that_differs_a_bit_gives_no_account(db, acct):
    """Kein Rückgriff über Schlagworte: „Muster Bürobedarf“ ist nicht „Muster Bürobedarf GmbH“."""
    past(db, acct, '2026-01-10', 6815)
    past(db, acct, '2026-02-10', 6815)
    receipt = add_receipt(db)
    save_reading(db, receipt)
    bank = add_bank(db, acct, recipient='Muster Bürobedarf')
    db.link_receipt_to_booking(bank, receipt)
    assert answer(db, bank, receipt)['row']['coa_id'] == ''


def test_the_account_of_the_vat_id_serves_when_the_recipient_is_new(db, acct):
    for number, date in ((1, '2026-01-10'), (2, '2026-02-10')):
        past(db, acct, date, 6805, recipient=f'Anderer Zahler {number}', receipt=a_receipt(db, f'B-{number}'))
    receipt = a_receipt(db, 'B-3')
    bank = add_bank(db, acct, recipient='Ganz Neuer Zahler')
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['row']['coa_id'] == coa(db, 6805) and REASON_ACCOUNT_SENDER in data['reasons'] and REASON_ACCOUNT_RECIPIENT not in data['reasons']


def test_both_sources_with_the_same_account_give_both_reasons(db, acct):
    for number, date in ((1, '2026-01-10'), (2, '2026-02-10')):
        past(db, acct, date, 6815, receipt=a_receipt(db, f'B-{number}'))
    receipt = a_receipt(db, 'B-3')
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['row']['coa_id'] == coa(db, 6815) and data['reasons'][-2:] == [REASON_ACCOUNT_RECIPIENT, REASON_ACCOUNT_SENDER]


def test_two_steady_sources_with_different_accounts_give_no_account_and_the_conflict(db, acct):
    for number, date in ((1, '2026-01-10'), (2, '2026-02-10')):
        past(db, acct, date, 6815)                                                 # derselbe Empfänger: 6815
        past(db, acct, date, 6805, recipient=f'Anderer Zahler {number}', receipt=a_receipt(db, f'B-{number}'))    # dieselbe USt-IdNr.: 6805
    receipt = a_receipt(db, 'B-3')
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['row']['coa_id'] == '' and data['hints'] == [HINT_ACCOUNT_CONFLICT]


def test_the_vat_history_ignores_the_own_receipt_and_the_own_booking(db, acct):
    receipt = a_receipt(db, 'B-1')
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    past(db, acct, '2026-01-10', 6805, recipient='Anderer Zahler', receipt=a_receipt(db, 'B-2'))
    assert answer(db, bank, receipt)['row']['coa_id'] == ''                       # nur EINE frühere Gruppe: der eigene Beleg und die eigene Bewegung zählen nicht mit


def test_a_hidden_account_is_dropped_with_the_hint_and_without_its_reason(db, acct):
    bank, receipt, account = a_world(db, acct)
    execute(db, 'UPDATE ChartOfAccounts SET ShowInMenu = 0 WHERE ID = ?', (account,))
    data = answer(db, bank, receipt)
    assert data['ok'] is True and data['row']['coa_id'] == '' and data['hints'] == [HINT_ACCOUNT_HIDDEN]
    assert REASON_ACCOUNT_RECIPIENT not in data['reasons'] and data['reasons'] == [REASON_AMOUNT, REASON_TAX.format(rate='19')]
    assert data['row']['tax_rate'] == '19' and data['row']['amount'] == '-119.00'                 # Steuer und Betrag bleiben


def test_the_hidden_hint_comes_after_the_hints_of_the_tax(db, acct):
    bank, receipt, account = a_world(db, acct)
    save_reading(db, receipt, TaxRate=None)
    execute(db, 'UPDATE ChartOfAccounts SET ShowInMenu = 0 WHERE ID = ?', (account,))
    assert answer(db, bank, receipt)['hints'] == [HINT_TAX, HINT_ACCOUNT_HIDDEN]


def test_the_account_of_an_account_that_is_still_shown_stays(db, acct):
    bank, receipt, account = a_world(db, acct)
    execute(db, 'UPDATE ChartOfAccounts SET ShowInMenu = 0 WHERE ID = ?', (coa(db, 6805),))      # ein anderes Konto ausgeblendet
    assert answer(db, bank, receipt)['row']['coa_id'] == account


# ── Das Konto muss ein Aufwands- oder Erlöskonto sein ────────────────────────
def with_history_on(db, acct, number, **over):
    """Eine Bankbewegung über −119,00 EUR mit verknüpftem Beleg (Rechnung, 19 %) und zwei früheren Bewegungen desselben Empfängers auf dem Konto ``number``."""
    past(db, acct, '2026-01-10', number)
    past(db, acct, '2026-02-10', number)
    receipt = add_receipt(db)
    save_reading(db, receipt, **over)
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    return bank, receipt


@pytest.mark.parametrize('number', [
    pytest.param(2100, id='Privatentnahmen'), pytest.param(1460, id='Verrechnungskonto'), pytest.param(1406, id='Vorsteuer-19'),
    pytest.param(1401, id='Vorsteuer-7'), pytest.param(520, id='Anlagekonto-PKW'), pytest.param(670, id='Anlagekonto-GWG'),
    pytest.param(4405, id='Wartekonto-19'), pytest.param(4345, id='Wartekonto-7'), pytest.param(4340, id='Wartekonto-0'),
    pytest.param(4736, id='Skonto-19'), pytest.param(4731, id='Skonto-7'), pytest.param(4730, id='Skonto-0')])
def test_an_account_that_is_no_expense_or_revenue_account_is_never_proposed_the_hint_says_so(db, acct, number):
    """Ein Privat-, Verrechnungs-, Steuer-, Anlage-, Warte- oder Skonto-Konto aus der Historie gäbe mit der Steuer des Belegs eine Buchung, die die EÜR anders liest
    (Vorsteuer auf 1406 verschwindet, die Steuer auf einem Privatkonto zählt doppelt): Das Konto bleibt leer, Betrag und Steuer bleiben."""
    bank, receipt = with_history_on(db, acct, number)
    data = answer(db, bank, receipt)
    assert data['ok'] is True and data['row'] == {'amount': '-119.00', 'coa_id': '', 'tax_rate': '19', 'tax_amount': '-19.00'}
    assert data['hints'] == [HINT_ACCOUNT_KIND]
    assert data['reasons'] == [REASON_AMOUNT, REASON_TAX.format(rate='19')]                  # die Begründung des Kontos fällt mit dem Konto weg


@pytest.mark.parametrize('number', [6815, 6310, 6805, 4400, 4300, 4185])
def test_an_expense_or_revenue_account_is_proposed(db, acct, number):
    bank, receipt = with_history_on(db, acct, number)
    data = answer(db, bank, receipt)
    assert data['row']['coa_id'] == coa(db, number) and data['hints'] == [] and REASON_ACCOUNT_RECIPIENT in data['reasons']


def test_the_kind_of_the_account_is_checked_for_the_sender_source_too(db, acct):
    for number, date in ((1, '2026-01-10'), (2, '2026-02-10')):
        past(db, acct, date, 1460, recipient=f'Anderer Zahler {number}', receipt=a_receipt(db, f'B-{number}'))
    receipt = a_receipt(db, 'B-3')
    bank = add_bank(db, acct, recipient='Ganz Neuer Zahler')
    db.link_receipt_to_booking(bank, receipt)
    data = answer(db, bank, receipt)
    assert data['row']['coa_id'] == '' and data['hints'] == [HINT_ACCOUNT_KIND] and REASON_ACCOUNT_SENDER not in data['reasons']


def test_the_kind_is_checked_before_the_hidden_state_and_there_is_one_hint_only(db, acct):
    bank, receipt = with_history_on(db, acct, 2100)
    execute(db, 'UPDATE ChartOfAccounts SET ShowInMenu = 0 WHERE ID = ?', (coa(db, 2100),))
    assert answer(db, bank, receipt)['hints'] == [HINT_ACCOUNT_KIND]


def test_the_hint_of_the_kind_stands_behind_the_hint_of_the_tax(db, acct):
    bank, receipt = with_history_on(db, acct, 2100, TaxRate=None)
    assert answer(db, bank, receipt)['hints'] == [HINT_TAX, HINT_ACCOUNT_KIND]


# ── Die eigene USt-IdNr. ist keine Quelle ────────────────────────────────────
def own_company(db, vat=VAT):
    db.insert_contact(contact_type='own', entity_type='company', display_name='Probe Eigen', company_name='Probe Eigen GmbH', tax_id=vat)


def own_invoices(db, acct):
    """Zwei Ausgangsrechnungen der eigenen Firma (die eigene USt-IdNr. als Absender) an verschiedene Kunden, bezahlt und auf 4300 gebucht, und ein weiterer Beleg mit
    derselben Angabe an einer Eingangszahlung eines dritten Kunden. Liefert ``(Bankbewegung, Beleg)`` des dritten."""
    for number, (date, customer) in enumerate((('2026-01-10', 'Kunde Eins'), ('2026-02-10', 'Kunde Zwei')), start=1):
        past(db, acct, date, 4300, recipient=customer, amount='119.00', receipt=a_receipt(db, f'A-{number}', Kind='invoice'))
    receipt = a_receipt(db, 'G-1', Kind='credit_note')
    bank = add_bank(db, acct, '2026-03-04', '119.00', 'Kunde Drei')
    db.link_receipt_to_booking(bank, receipt)
    return bank, receipt


def test_without_an_own_company_the_vat_id_of_the_sender_pulls_the_history_of_the_documents(db, acct):
    """Die Kontrolle zu den folgenden Tests: Ohne eigene Firma gilt die Angabe wie jede."""
    bank, receipt = own_invoices(db, acct)
    data = answer(db, bank, receipt)
    assert data['row']['coa_id'] == coa(db, 4300) and REASON_ACCOUNT_SENDER in data['reasons']


@pytest.mark.parametrize('own_vat', ['DE123456789', 'USt-IdNr. DE-123 456 789', ' de 123.456.789 '])
def test_the_vat_id_of_the_own_company_is_no_source_in_whatever_spelling(db, acct, monkeypatch, own_vat):
    """Die eigene USt-IdNr. steht auf jeder eigenen Ausgangsrechnung: Als Quelle zöge sie die Konten fremder Kunden heran. Verglichen wird bereinigt."""
    own_company(db, own_vat)
    bank, receipt = own_invoices(db, acct)

    def boom(*args, **kwargs):
        raise AssertionError('die Historie zur eigenen USt-IdNr. wird nicht gelesen')

    monkeypatch.setattr(Database, 'vat_history_groups', boom)
    data = answer(db, bank, receipt)
    assert data['ok'] is True and data['row']['coa_id'] == '' and data['hints'] == [HINT_NO_ACCOUNT]
    assert REASON_ACCOUNT_SENDER not in data['reasons'] and data['row']['tax_rate'] == '19'


def test_the_reading_may_spell_the_own_vat_id_differently(db, acct):
    own_company(db, 'DE123456789')
    for number, (date, customer) in enumerate((('2026-01-10', 'Kunde Eins'), ('2026-02-10', 'Kunde Zwei')), start=1):
        past(db, acct, date, 4300, recipient=customer, amount='119.00', receipt=a_receipt(db, f'A-{number}', vat='DE 123 456 789'))
    receipt = a_receipt(db, 'G-1', vat='de-123-456-789', Kind='credit_note')
    bank = add_bank(db, acct, '2026-03-04', '119.00', 'Kunde Drei')
    db.link_receipt_to_booking(bank, receipt)
    assert answer(db, bank, receipt)['row']['coa_id'] == ''


def test_another_vat_id_is_still_a_source_when_an_own_company_exists(db, acct):
    own_company(db, OTHER_VAT)
    bank, receipt = own_invoices(db, acct)
    assert answer(db, bank, receipt)['row']['coa_id'] == coa(db, 4300)


def test_the_history_of_the_recipient_still_serves_when_the_vat_id_is_the_own_one(db, acct):
    own_company(db)
    past(db, acct, '2026-01-10', 6815, recipient='Kunde Drei', amount='119.00')
    past(db, acct, '2026-02-10', 6815, recipient='Kunde Drei', amount='119.00')
    bank, receipt = own_invoices(db, acct)
    data = answer(db, bank, receipt)
    assert data['row']['coa_id'] == coa(db, 6815) and data['reasons'][-1] == REASON_ACCOUNT_RECIPIENT


# ── Der Satz muss einer der Buchhaltung sein ─────────────────────────────────
def with_rate(db, acct, rate, net, tax):
    """Eine Bankbewegung über den Bruttobetrag mit verknüpftem Beleg, dessen Lesung genau diesen einen Satz trägt, und der Historie auf 6815."""
    past(db, acct, '2026-01-10', 6815)
    past(db, acct, '2026-02-10', 6815)
    receipt = add_receipt(db)
    save_reading(db, receipt, TaxRate=rate, NetMinor=net, TaxMinor=tax, GrossMinor=net + tax)
    bank = add_bank(db, acct, amount=f'-{(net + tax) // 10000}.00')
    db.link_receipt_to_booking(bank, receipt)
    return bank, receipt


@pytest.mark.parametrize('rate, net, tax, shown', [
    pytest.param(0.2, 1000000, 200000, '20', id='20'), pytest.param(0.21, 1000000, 210000, '21', id='21'),
    pytest.param(0.055, 2000000, 110000, '5,5', id='5,5-ohne-Eintrag'), pytest.param(0.1, 1000000, 100000, '10', id='10-ohne-Eintrag')])
def test_a_rate_that_is_no_rate_of_the_books_is_not_taken_and_the_hint_names_it(db, acct, rate, net, tax, shown):
    bank, receipt = with_rate(db, acct, rate, net, tax)
    data = answer(db, bank, receipt)
    assert data['ok'] is True and data['row'] == {'amount': f'-{(net + tax) // 10000}.00', 'coa_id': coa(db, 6815), 'tax_rate': '', 'tax_amount': ''}
    assert data['hints'] == [HINT_TAX_RATE.format(rate=shown)] and HINT_TAX not in data['hints']
    assert data['reasons'] == [REASON_AMOUNT, REASON_ACCOUNT_RECIPIENT]


@pytest.mark.parametrize('rate, net, tax, shown', [
    pytest.param(0.19, 1000000, 190000, '19', id='19'), pytest.param(0.07, 1000000, 70000, '7', id='7'),
    pytest.param(0.16, 1000000, 160000, '16', id='16'), pytest.param(0.05, 1000000, 50000, '5', id='5')])
def test_the_rates_with_fixed_tax_accounts_are_taken_without_an_entry_of_the_master_data(db, acct, rate, net, tax, shown):
    bank, receipt = with_rate(db, acct, rate, net, tax)
    data = answer(db, bank, receipt)
    assert data['row']['tax_rate'] == shown and data['row']['tax_amount'] == f'-{tax // 10000}.00' and data['hints'] == []
    assert REASON_TAX.format(rate=shown) in data['reasons']


def test_a_rate_of_the_master_data_is_taken(db, acct):
    db.insert_tax_rate(label='5,5 % (erfunden)', bp=550, revenue_coa_id=coa(db, 4400))
    bank, receipt = with_rate(db, acct, 0.055, 2000000, 110000)
    data = answer(db, bank, receipt)
    assert data['row'] == {'amount': '-211.00', 'coa_id': coa(db, 6815), 'tax_rate': '5.5', 'tax_amount': '-11.00'} and data['hints'] == []
    assert REASON_TAX.format(rate='5,5') in data['reasons']
    other = with_rate(db, acct, 0.2, 1000000, 200000)
    assert answer(db, *other)['hints'] == [HINT_TAX_RATE.format(rate='20')]                  # der Eintrag öffnet nur seinen eigenen Satz


def test_an_entry_that_is_switched_off_still_counts_as_a_rate_of_the_books_but_an_invalid_one_does_not(db, acct):
    """Wie in den Auswertungen: Das Abschalten nimmt einem Satz nicht seine Geltung; eine Zeile, die die Regeln eines Eintrags nicht erfüllt (per SQL geschrieben),
    ist kein Satz."""
    db.insert_tax_rate(label='5,5 % (erfunden)', bp=550, revenue_coa_id=coa(db, 4400), active=False)
    bank, receipt = with_rate(db, acct, 0.055, 2000000, 110000)
    assert answer(db, bank, receipt)['row']['tax_rate'] == '5.5'
    execute(db, "INSERT INTO TaxRates (Label, RateBp, Kind, Active, SortOrder) VALUES ('kaputt', 2000, 'unbekannt', 1, 90)")
    execute(db, "INSERT INTO TaxRates (Label, RateBp, Kind, Active, SortOrder) VALUES ('kaputt', 2100, 'standard', 1, 91)")     # ohne Erlöskonto: ungültig
    assert [entry.valid for entry in db.fetch_tax_rates() if entry.bp in (2000, 2100)] == [False, False]
    for rate, net, tax in ((0.2, 1000000, 200000), (0.21, 1000000, 210000)):
        bank, receipt = with_rate(db, acct, rate, net, tax)
        data = answer(db, bank, receipt)
        assert data['row']['tax_rate'] == '' and data['hints'][0].startswith('Steuer nicht übernommen: Der gelesene Satz')


def test_a_reading_without_tax_needs_no_entry(db, acct):
    receipt = add_receipt(db)
    save_reading(db, receipt, TaxRate=0.0, NetMinor=GROSS, TaxMinor=0)
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    execute(db, 'DELETE FROM TaxRates')
    data = answer(db, bank, receipt)
    assert data['row']['tax_rate'] == '' and REASON_TAX_NONE in data['reasons'] and HINT_TAX_RATE.format(rate='0') not in data['hints']


def test_a_bank_movement_with_a_row_is_not_proposed_to(db, acct):
    bank, receipt, _account = a_world(db, acct)
    add_child(db, bank, 4400)
    data = answer(db, bank, receipt)
    assert (data['ok'], data['message'], data['row'], data['reasons'], data['hints']) == (False, REFUSE_HAS_ROWS, None, [], [])


def test_a_bank_movement_with_a_locked_payment_row_is_not_proposed_to(db, acct):
    bank, receipt, _account = a_world(db, acct)
    marked = add_child(db, bank, 4400)
    execute(db, 'UPDATE Bookings SET Status = ? WHERE ID = ?', (PAYMENT_MARK, marked))
    assert answer(db, bank, receipt)['message'] == REFUSE_HAS_ROWS


def test_a_discount_is_not_proposed_to(db, acct):
    bank, receipt, _account = a_world(db, acct)
    execute(db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', (-1166200, bank))
    data = answer(db, bank, receipt)
    assert (data['ok'], data['message'], data['row']) == (False, REFUSE_AMOUNT, None)


def test_the_wrong_direction_is_not_proposed_to(db, acct):
    bank, receipt, _account = a_world(db, acct)
    execute(db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', (GROSS, bank))
    assert answer(db, bank, receipt)['message'] == REFUSE_DIRECTION


def test_a_foreign_currency_is_not_proposed_to(db, acct):
    bank, receipt, _account = a_world(db, acct)
    execute(db, "UPDATE Bookings SET Currency = 'USD' WHERE ID = ?", (bank,))
    assert answer(db, bank, receipt)['message'] == REFUSE_CURRENCY


@pytest.mark.parametrize('over, sentence', [
    pytest.param({'Status': 'no_text'}, REFUSE_NO_READING, id='kein-Text'),
    pytest.param({'Currency': 'USD'}, REFUSE_NO_READING, id='Fremdwaehrung'),
    pytest.param({'GrossMinor': None}, REFUSE_NO_READING, id='ohne-Brutto'),
    pytest.param({'ReaderVersion': 0}, REFUSE_OUTDATED, id='aeltere-Regeln')])
def test_a_reading_that_cannot_be_used_is_not_proposed_to(db, acct, over, sentence):
    bank, receipt, _account = a_world(db, acct)
    save_reading(db, receipt, **over)
    data = answer(db, bank, receipt)
    assert (data['ok'], data['message'], data['row']) == (False, sentence, None)


def test_a_receipt_without_a_reading_is_not_proposed_to(db, acct):
    bank = add_bank(db, acct)
    receipt = add_receipt(db)
    db.link_receipt_to_booking(bank, receipt)
    assert answer(db, bank, receipt)['message'] == REFUSE_NO_READING


def test_a_refusal_asks_nothing_of_the_history(db, acct, monkeypatch):
    bank, receipt, _account = a_world(db, acct)
    add_child(db, bank, 4400)

    def boom(*args, **kwargs):
        raise AssertionError('die Historie wird nur gelesen, wenn es einen Vorschlag gibt')

    monkeypatch.setattr(Database, 'recipient_history_groups', boom)
    monkeypatch.setattr(Database, 'vat_history_groups', boom)
    monkeypatch.setattr(Database, 'own_identity', boom)
    monkeypatch.setattr(Database, 'fetch_tax_rates', boom)
    assert answer(db, bank, receipt)['message'] == REFUSE_HAS_ROWS


@pytest.mark.parametrize('how', ['unlinked', 'contract', 'invoice', 'other'])
def test_a_receipt_that_is_not_linked_as_a_receipt_is_404_with_the_fixed_sentence(db, acct, how):
    bank = add_bank(db, acct)
    receipt = add_receipt(db)
    save_reading(db, receipt)
    if how != 'unlinked':
        db.link_receipt_to_booking(bank, receipt, how)
    status, data = propose(db, bank, receipt)
    assert (status, data) == (404, {'success': False, 'error': receipt_links.PROPOSAL_NOT_LINKED})


def test_a_receipt_that_hangs_on_another_bank_movement_is_not_linked_to_this_one(db, acct):
    _bank, receipt, _account = a_world(db, acct)
    other = add_bank(db, acct, '2026-03-05')
    status, data = propose(db, other, receipt)
    assert (status, data) == (404, {'success': False, 'error': receipt_links.PROPOSAL_NOT_LINKED})


def test_an_entry_and_an_unknown_booking_are_404_with_the_fixed_sentence(db, acct):
    receipt = add_receipt(db)
    save_reading(db, receipt)
    entry = db.insert_booking(DAY, -119.00, booking_type='entry', recipient_client=SUPPLIER, coa_id=coa(db, 6815))
    db.link_receipt_to_booking(entry, receipt)
    for booking in (entry, 999999, -1, 0):
        status, data = propose(db, booking, receipt)
        assert (status, data) == (404, {'success': False, 'error': receipt_links.PROPOSAL_NO_BANK}), booking


def test_an_unknown_receipt_is_404_with_the_sentence_of_the_receipts(db, acct):
    bank = add_bank(db, acct)
    status, data = propose(db, bank, 999999)
    assert (status, data) == (404, {'success': False, 'error': receipt_files.NOT_FOUND})
    assert propose(db, bank, -1)[0] == 404


def test_the_fixed_sentences_are_the_planned_ones():
    assert receipt_links.PROPOSAL_NO_BANK == 'Die Bankbewegung wurde nicht gefunden.'
    assert receipt_links.PROPOSAL_NOT_LINKED == 'Dieser Beleg ist nicht mit dieser Bankbewegung verknüpft.'
    assert receipt_links.PROPOSAL_FAILED == 'Der Vorschlag konnte nicht gelesen werden – Einzelheiten stehen im Server-Log.'
    assert receipt_files.NOT_FOUND == 'Diesen Beleg gibt es nicht.'


def test_nothing_of_the_receipt_and_nothing_of_the_booking_is_in_the_answer(db, acct):
    """Weder Absender, Rechnungsnummer, Dateiname, Pfad, Info noch der Text der Lesung – und auch nicht der Empfänger oder der Text der Bankbewegung."""
    secret_path = 'Belege/2026/geheime-ablage_beleg.pdf'
    past(db, acct, '2026-01-10', 6815)
    past(db, acct, '2026-02-10', 6815)
    receipt = add_receipt(db, number='26B-GEHEIM', filename='geheime-ablage.pdf', path=secret_path, info='GEHEIME INFO')
    raw = json.dumps({'fields': {'gross_minor': {'value': GROSS, 'source': 'rule', 'quote': SECRET_TEXT, 'page': 1}}, 'checks': {'sum': 'ok'}})
    assert db.save_document_reading(receipt, secret_path, reading(SenderName=HOSTILE, InvoiceNumber=HOSTILE, Text=SECRET_TEXT + '<script>', Raw=raw,
                                                                  SenderVatId=VAT))
    bank = add_bank(db, acct, text='Verwendungszweck GEHEIMER-ZWECK')
    db.link_receipt_to_booking(bank, receipt)
    status, body = handlers.handle_receipt_proposal(db, bank, receipt)
    assert status == 200 and json.loads(body)['ok'] is True
    for text in (HOSTILE, 'Absender', 'RE-2026-17', 'GEHEIM', 'geheime-ablage', 'Belege', 'beleg.pdf', 'script', 'fields', 'quote', userctx.DATA_ROOT,
                 SUPPLIER, 'Verwendungszweck', '26B-GEHEIM', 'Traceback', VAT):
        assert text not in body, text


def test_the_answer_is_read_only_the_database_and_the_replay_log_stay_as_they_were(db, acct, monkeypatch):
    bank, receipt, _account = a_world(db, acct)
    before = (installation.dump(db.db_name), installation.tree(os.path.dirname(db.db_name)), installation.tree(userctx.DATA_ROOT))
    statements = []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    assert answer(db, bank, receipt)['ok'] is True
    assert propose(db, 999999, receipt)[0] == 404
    monkeypatch.setattr(Database, '_get_connection', real)
    assert statements and all(statement.lstrip().upper().startswith('SELECT') for statement in statements), statements
    assert (installation.dump(db.db_name), installation.tree(os.path.dirname(db.db_name)), installation.tree(userctx.DATA_ROOT)) == before


@pytest.mark.parametrize('broken', ['receipt_proposal_inputs', 'recipient_history_groups', 'fetch_chart_of_accounts'])
def test_an_unexpected_error_is_a_fixed_sentence_and_its_text_stays_in_the_server_log(db, acct, monkeypatch, capsys, broken):
    bank, receipt, _account = a_world(db, acct)
    secret = 'geheimer Fehlertext /home/niemand/buch.db sqlite3'

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(Database, broken, boom)
    status, body = handlers.handle_receipt_proposal(db, bank, receipt)
    assert (status, json.loads(body)) == (500, {'success': False, 'error': receipt_links.PROPOSAL_FAILED})
    for text in ('geheim', '/home', 'sqlite3', 'RuntimeError', 'Traceback'):
        assert text not in body, text
    assert secret in capsys.readouterr().err                                    # der volle Fehler steht im Server-Log


# ── Die Route ───────────────────────────────────────────────────────────────
PATH = '/transactions/receipt-proposal'
NO_BANK = {'success': False, 'error': receipt_links.PROPOSAL_NO_BANK}


@pytest.fixture
def world(auto_login):
    """Die Welt von ``a_world`` in der Datenbank des angemeldeten Benutzers."""
    db = auto_login.db
    acct = bank_account(db, 'Probe-Bank', 1800)
    bank, receipt, account = a_world(db, acct)
    return SimpleNamespace(db=db, acct=acct, bank=bank, receipt=receipt, account=account, csrf=auto_login.csrf)


def ask(port, bank, receipt, headers=None, extra=''):
    return get(port, f'{PATH}?id={bank}&document={receipt}{extra}', headers)


def test_the_route_is_a_search_route_and_not_in_the_diagnosis_server():
    assert PATH in server_app._SEARCH_PATHS and PATH in server_app._JSON_ANSWER_ROUTES        # auch die 503 während einer Wiederherstellung ist JSON
    for other in (server_app._JSON_PATHS, server_app._POST_ONLY_JSON_PATHS, server_app._ACTION_PATHS, server_app._GENERATE_PATHS, server_app._UPLOAD_PATHS):
        assert PATH not in other                                                                # nichts davon ist ein POST
    assert PATH not in mcp._PAGE_ALLOWLIST
    with pytest.raises(ValueError, match=r'Seite nicht freigegeben'):
        mcp.check_page_path(f'{PATH}?id=1&document=1')


def test_the_route_answers_the_proposal_of_the_handler_as_json(server_port, world):
    resp = ask(server_port, world.bank, world.receipt)
    assert resp.status == 200 and resp.headers['Content-Type'] == 'application/json'
    assert json.loads(resp.text) == {
        'success': True, 'ok': True, 'message': DONE_TEXT, 'hints': [],
        'reasons': [REASON_AMOUNT, REASON_TAX.format(rate='19'), REASON_ACCOUNT_RECIPIENT],
        'row': {'amount': '-119.00', 'coa_id': world.account, 'tax_rate': '19', 'tax_amount': '-19.00'}}


def test_the_status_of_a_refusal_is_200_and_of_an_unknown_thing_404(server_port, world):
    add_child(world.db, world.bank, 4400)
    refused = ask(server_port, world.bank, world.receipt)
    assert (refused.status, json.loads(refused.text)['message']) == (200, REFUSE_HAS_ROWS)
    unknown = ask(server_port, 999999, world.receipt)
    assert (unknown.status, json.loads(unknown.text)) == (404, NO_BANK)
    no_document = ask(server_port, world.bank, 999999)
    assert (no_document.status, json.loads(no_document.text)) == (404, {'success': False, 'error': receipt_files.NOT_FOUND})
    other = ask(server_port, 1, world.receipt)                                                 # eine Bankbewegung, an der der Beleg nicht hängt
    assert (other.status, json.loads(other.text)) == (404, {'success': False, 'error': receipt_links.PROPOSAL_NOT_LINKED})


@pytest.mark.parametrize('query', ['', '?id=', '?id=abc', '?id=1.5', '?id=0', '?id=-1', f'?id={2 ** 63}', f'?id={"9" * 30}', '?id=%00', '?other=1',
                                   '?document=1'])
def test_a_missing_or_unusable_bank_id_answers_like_an_unknown_one(server_port, world, query):
    """``_id_param``: Fehlt die ID oder ist sie keine Ganzzahl in 64 Bit, gilt ``-1`` – nie ein 500 und nie ein Rückfall auf eine vorhandene Bankbewegung
    (es gibt hier eine mit der ID 1)."""
    resp = get(server_port, PATH + query)
    assert (resp.status, json.loads(resp.text)) == (404, NO_BANK)
    assert resp.headers['Content-Type'] == 'application/json'


@pytest.mark.parametrize('value', ['', 'abc', '1.5', '0', '-1', str(2 ** 63), '9' * 30, '%00'])
def test_a_missing_or_unusable_receipt_id_answers_like_an_unknown_one(server_port, world, value):
    resp = get(server_port, f'{PATH}?id={world.bank}&document={value}')
    assert (resp.status, json.loads(resp.text)) == (404, {'success': False, 'error': receipt_files.NOT_FOUND})
    assert get(server_port, f'{PATH}?id={world.bank}').status == 404


@pytest.mark.parametrize('headers', [
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, id='cross-site'),
    pytest.param({'Sec-Fetch-Site': 'same-site'}, id='same-site'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, id='fremder-Origin'),
])
def test_a_request_from_a_foreign_page_is_403_and_names_no_data(server_port, world, headers):
    resp = ask(server_port, world.bank, world.receipt, headers)
    assert (resp.status, json.loads(resp.text)) == (403, {'success': False, 'error': receipt_links.SUGGEST_FOREIGN})
    assert resp.headers['Content-Type'] == 'application/json'
    for text in ('Betrag', 'row', 'reasons', 'amount', '119'):
        assert text not in resp.text, text


def test_the_own_page_may_ask(server_port, world):
    for headers in ({'Sec-Fetch-Site': 'same-origin'}, {'Sec-Fetch-Site': 'none'}, {'Origin': own_origin(server_port)}, {}):
        resp = ask(server_port, world.bank, world.receipt, headers)
        assert resp.status == 200 and json.loads(resp.text)['ok'] is True, headers


def test_the_answers_are_never_cached_even_when_the_address_ends_like_a_static_file(server_port, world):
    answers = [ask(server_port, world.bank, world.receipt), ask(server_port, 999999, world.receipt),
               ask(server_port, world.bank, world.receipt, {'Origin': FOREIGN_ORIGIN}),
               ask(server_port, world.bank, world.receipt, extra='&x=.png'), ask(server_port, world.bank, world.receipt, extra='&x=.css'),
               get(server_port, PATH + '?id=abc'), get(server_port, PATH)]
    assert [resp.status for resp in answers] == [200, 404, 403, 200, 200, 404, 404]
    for resp in answers:
        assert resp.headers.get_all('Cache-Control') == ['no-store']


def test_only_get_asks_a_post_never_answers_with_a_proposal(server_port, world):
    resp = post(server_port, f'{PATH}?id={world.bank}&document={world.receipt}', headers={'Origin': own_origin(server_port)}, csrf=world.csrf)
    assert resp.status != 200 and 'reasons' not in resp.text and 'DONE' not in resp.text and DONE_TEXT not in resp.text


def test_the_route_writes_nothing_and_only_selects(server_port, world, monkeypatch):
    db = world.db
    before = (installation.dump(db.db_name), installation.tree(userctx.DATA_ROOT))
    statements = []
    real = Database._get_connection

    def traced(self):
        connection = real(self)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(Database, '_get_connection', traced)
    assert ask(server_port, world.bank, world.receipt).status == 200
    assert ask(server_port, world.bank, world.receipt).status == 200
    assert ask(server_port, 999999, world.receipt).status == 404
    monkeypatch.setattr(Database, '_get_connection', real)
    assert statements and all(statement.lstrip().upper().startswith('SELECT') for statement in statements), statements
    assert (installation.dump(db.db_name), installation.tree(userctx.DATA_ROOT)) == before        # kein Eintrag, keine Datei, kein Replay-Log


def test_the_answer_of_the_route_never_names_a_path_a_file_name_a_number_or_the_text_of_the_file(server_port, auto_login):
    db = auto_login.db
    acct = bank_account(db, 'Probe-Bank', 1800)
    past(db, acct, '2026-01-10', 6815)
    past(db, acct, '2026-02-10', 6815)
    receipt = add_receipt(db, number='26B-GEHEIM', filename='geheime-ablage.pdf', info='GEHEIME INFO')
    save_reading(db, receipt, Text=SECRET_TEXT, SenderName=HOSTILE, InvoiceNumber=HOSTILE)
    bank = add_bank(db, acct)
    db.link_receipt_to_booking(bank, receipt)
    resp = ask(server_port, bank, receipt)
    assert json.loads(resp.text)['ok'] is True
    for text in (SECRET_TEXT, 'GEHEIM', 'geheime-ablage', 'Belege', 'B-1_beleg', '26B-GEHEIM', userctx.DATA_ROOT, 'Traceback', 'Path', 'Filename', HOSTILE,
                 'Absender', SUPPLIER):
        assert text not in resp.text, text


def test_in_demo_mode_the_route_answers_like_everywhere(server_port, monkeypatch):
    demo_site(monkeypatch)
    db = installation.open_db('demo')
    acct = bank_account(db, 'Probe-Bank', 1800)
    bank, receipt, _account = a_world(db, acct)
    resp = ask(server_port, bank, receipt)
    assert resp.status == 200 and json.loads(resp.text)['ok'] is True
    assert ask(server_port, 999999, receipt).status == 404


def test_an_unexpected_error_of_the_route_is_a_fixed_sentence(server_port, world, monkeypatch, capsys):
    secret = 'geheimer Fehlertext /home/niemand/buch.db sqlite3'
    real = Database.receipt_proposal_inputs

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(Database, 'receipt_proposal_inputs', boom)
    resp = ask(server_port, world.bank, world.receipt)
    assert (resp.status, json.loads(resp.text)) == (500, {'success': False, 'error': receipt_links.PROPOSAL_FAILED})
    assert resp.headers['Content-Type'] == 'application/json'
    for text in ('geheim', '/home', 'sqlite3', 'RuntimeError', 'Traceback'):
        assert text not in resp.text, text
    assert secret in capsys.readouterr().err
    monkeypatch.setattr(Database, 'receipt_proposal_inputs', real)
    assert ask(server_port, world.bank, world.receipt).status == 200                                 # der Server antwortet weiter


# ── Die Vorschläge aus der Historie (🪄): dieselbe Härtung ───────────────────
SUGGEST = '/transactions/suggest'


def test_the_suggestion_of_the_history_is_a_search_route_too():
    assert SUGGEST in server_app._SEARCH_PATHS and SUGGEST in server_app._JSON_ANSWER_ROUTES
    for other in (server_app._JSON_PATHS, server_app._POST_ONLY_JSON_PATHS, server_app._ACTION_PATHS, server_app._GENERATE_PATHS, server_app._UPLOAD_PATHS):
        assert SUGGEST not in other
    assert SUGGEST not in mcp._PAGE_ALLOWLIST


def test_the_suggestion_of_the_history_still_answers_as_before(server_port, world):
    resp = get(server_port, f'{SUGGEST}?id=0&recipient=' + quote(SUPPLIER, safe=''))
    data = json.loads(resp.text)
    assert resp.status == 200 and data['ok'] is True and [row['coa_id'] for row in data['rows']] == [world.account]
    by_id = get(server_port, f'{SUGGEST}?id={world.bank}')                           # der Empfänger kommt dann aus der gespeicherten Buchung
    assert by_id.status == 200 and json.loads(by_id.text)['ok'] is True and json.loads(by_id.text)['recipient'] == SUPPLIER


@pytest.mark.parametrize('headers', [
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, id='cross-site'),
    pytest.param({'Sec-Fetch-Site': 'same-site'}, id='same-site'),
    pytest.param({'Origin': FOREIGN_ORIGIN}, id='fremder-Origin')])
def test_the_suggestion_of_the_history_from_a_foreign_page_is_403(server_port, world, headers):
    resp = get(server_port, f'{SUGGEST}?id={world.bank}&recipient=x', headers)
    assert (resp.status, json.loads(resp.text)) == (403, {'success': False, 'error': receipt_links.SUGGEST_FOREIGN})
    for text in ('rows', 'document_nr', SUPPLIER):
        assert text not in resp.text, text
    own = get(server_port, f'{SUGGEST}?id={world.bank}&recipient=x', {'Sec-Fetch-Site': 'same-origin'})
    assert own.status == 200


def test_the_suggestion_of_the_history_is_never_cached(server_port, world):
    answers = [get(server_port, f'{SUGGEST}?id={world.bank}'), get(server_port, f'{SUGGEST}?id={world.bank}&x=.png'),
               get(server_port, f'{SUGGEST}?id=abc'), get(server_port, f'{SUGGEST}?id={world.bank}', {'Origin': FOREIGN_ORIGIN})]
    assert [resp.status for resp in answers] == [200, 200, 200, 403]
    for resp in answers:
        assert resp.headers.get_all('Cache-Control') == ['no-store']


@pytest.mark.parametrize('value', ['', 'abc', '1.5', str(2 ** 63), '9' * 30, '%00', '-1', '0'])
def test_an_unusable_id_of_the_suggestion_answers_like_an_unknown_one(server_port, world, value):
    """Vorher: ``int(…)`` – eine Zahl, die in keine 64 Bit passt, ergab einen Fehler bei der Abfrage und eine 500."""
    recipient = '&recipient=' + quote(SUPPLIER, safe='')
    unknown = get(server_port, f'{SUGGEST}?id=999999{recipient}')
    resp = get(server_port, f'{SUGGEST}?id={value}{recipient}')
    assert resp.status == unknown.status == 200 and json.loads(resp.text) == json.loads(unknown.text)
    assert get(server_port, f'{SUGGEST}{recipient.replace("&", "?", 1)}').text == unknown.text         # ohne ID ebenso


def test_the_recipient_of_the_suggestion_is_limited_to_2000_characters(server_port, world):
    """Ein gespeicherter Empfänger kann lang sein (Zeilen eines PDF-Auszugs, verbunden): 300 Zeichen ließen die Maske dort mit einer Fehlermeldung zurück."""
    assert handlers.SUGGEST_RECIPIENT_MAX == 2000
    assert handlers.SUGGEST_RECIPIENT_TOO_LONG == 'Der Empfänger ist zu lang (höchstens 2000 Zeichen).'
    for length in (300, 301, 1000):
        resp = get(server_port, f'{SUGGEST}?id=0&recipient={"A" * length}')
        assert resp.status == 200 and json.loads(resp.text)['ok'] is False and 'zu lang' not in resp.text            # eine Antwort der Suche, kein 400
    exact = 'A' * 2000
    resp = get(server_port, f'{SUGGEST}?id=0&recipient={exact}')
    assert resp.status == 200 and json.loads(resp.text)['ok'] is False and 'zu lang' not in resp.text
    long = get(server_port, f'{SUGGEST}?id=0&recipient={"A" * 2001}')
    assert (long.status, json.loads(long.text)) == (400, {'ok': False, 'message': handlers.SUGGEST_RECIPIENT_TOO_LONG})
    assert get(server_port, f'{SUGGEST}?id=0&recipient=%20{exact}%20').status == 200                    # ohne Leerraum am Rand gezählt
    wide = get(server_port, f'{SUGGEST}?id=0&recipient=' + quote('€' * 2000, safe=''))                    # 2000 Zeichen, aber 18 000 Bytes in der Adresse
    assert wide.status == 200 and 'zu lang' not in wide.text
    status, body = handlers.handle_transaction_suggest(world.db, {'id': ['0'], 'recipient': ['B' * 2001]})
    assert (status, json.loads(body)) == (400, {'ok': False, 'message': handlers.SUGGEST_RECIPIENT_TOO_LONG})


# ── Die Seite: der Knopf steht nur in der Bankmaske ─────────────────────────
def mask(db, booking):
    return PageTransactions(db, edit_transaction_id=booking, date_from='2026-01-01', date_to='2026-12-31')


EXPECTED_TEXTS = {'hasRows': REFUSE_HAS_ROWS, 'hidden': HINT_ACCOUNT_HIDDEN, 'busy': 'Vorschlag wird gelesen …', 'button': 'Als Buchungssatz',
                  'title': 'Aus der Lesung eine Zeile im Buchungssatz-Editor vorbefüllen (speichert nicht)'}


def test_the_texts_of_the_button_are_the_planned_fixed_sentences():
    assert pages_transactions.PROPOSE_TEXTS == EXPECTED_TEXTS


def test_the_mask_of_a_bank_movement_carries_the_texts_of_the_button_as_data_and_the_script(db, acct):
    bank, _receipt, _account = a_world(db, acct)
    html = mask(db, bank)
    assert script_data(html, 'TX_PROPOSE') == EXPECTED_TEXTS
    assert script_data(html, 'TX_LINK_BOOKING_ID') == bank
    assert 'function txPropose(' in TX_LINK_JS and '/transactions/receipt-proposal' in TX_LINK_JS and 'txPropose(' in html
    assert html.count('var TX_PROPOSE = ') == 1
    assert Dom(html).all('img') == []


def test_every_other_mask_has_no_button_the_data_is_null(db, acct):
    bank, receipt, _account = a_world(db, acct)
    child = add_child(db, bank, 6815)
    entry = db.insert_booking(DAY, -119.00, booking_type='entry', recipient_client=SUPPLIER, coa_id=coa(db, 6815))
    paid = add_bank(db, acct, '2026-03-05')
    marked = add_child(db, paid, 6815)
    execute(db, 'UPDATE Bookings SET Status = ? WHERE ID = ?', (PAYMENT_MARK, marked))
    for booking in (child, entry, marked):
        db.link_receipt_to_booking(booking, receipt)
        html = mask(db, booking)
        assert script_data(html, 'TX_PROPOSE') is None, booking
        assert 'var TX_PROPOSE = null;' in html


def test_a_new_booking_has_no_block_of_receipts_and_so_no_button(db):
    html = PageTransactions(db, date_from='2026-01-01', date_to='2026-12-31')
    assert 'TX_PROPOSE' not in html and 'txPropose(' not in html and 'Belege lassen sich zuordnen, sobald die Buchung gespeichert ist.' in html


def test_the_mask_carries_nothing_from_the_reading_of_the_receipt(db, acct):
    bank, receipt, _account = a_world(db, acct)
    save_reading(db, receipt, SenderName=HOSTILE, InvoiceNumber=HOSTILE, Text=SECRET_TEXT, Raw='{}')
    html = mask(db, bank)
    for text in (SECRET_TEXT, 'GEHEIMER', 'Absender Probe', 'RE-2026-17', 'onerror'):
        assert text not in html, text


def test_the_button_adds_no_field_to_the_form_and_opening_and_saving_unchanged_changes_nothing(db, acct):
    bank, _receipt, _account = a_world(db, acct)
    other = add_bank(db, acct, '2026-03-07')
    plain, _options = open_form(db, other)
    linked, _options = open_form(db, bank)
    assert set(linked) == set(plain) and not [name for name in linked if 'propos' in name or name == 'document']
    before = snapshot(db)
    submit(db, linked)
    assert snapshot(db) == before


def test_the_row_of_the_answer_saves_through_the_existing_way_and_balances_the_bank_movement(db, acct):
    """Das Skript trägt die Antwort in eine NEUE Zeile des Editors ein; gespeichert wird allein mit der Maske, über den bestehenden Weg."""
    bank, receipt, account = a_world(db, acct)
    row = answer(db, bank, receipt)['row']
    post, _options = open_form(db, bank)
    assert post['split_id'] == ['']                                                   # nur die Vorlagenzeile: der Editor ist leer
    filled = add_row(post, split_amount=row['amount'], split_coa=row['coa_id'], split_tax_rate=row['tax_rate'], split_tax_amount=row['tax_amount'],
                     split_text='Verwendungszweck Probe', split_docnr='')
    submit(db, filled)
    stored = query(db, 'SELECT Amount, COA_ID, TaxRate, TaxAmount, Text FROM Bookings WHERE ParentBooking_ID = ?', (bank,))
    assert stored == [(-GROSS, account, 0.19, -TAX, 'Verwendungszweck Probe')]
    assert db.find_unbalanced_splits('2026-01-01', '2026-12-31') == []
    after = answer(db, bank, receipt)                                                  # jetzt hat die Bewegung einen Satz: kein Vorschlag mehr
    assert after['ok'] is False and after['message'] == REFUSE_HAS_ROWS


# ── Das Skript im Browser (Node, ohne Browser) ──────────────────────────────
HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const scenario = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

class Node {}
class TextNode extends Node { constructor(text) { super(); this.tagName = '#text'; this.ownText = text; } }
class Element extends Node {
  constructor(tag) {
    super();
    this.tagName = tag; this.className = ''; this.type = ''; this.href = ''; this.target = ''; this.rel = '';
    this.value = ''; this.children = []; this.listeners = {}; this.ownText = ''; this.dataset = {}; this.title = '';
  }
  get textContent() { return this.ownText; }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  set innerHTML(value) { throw new Error('innerHTML wurde gesetzt'); }
  set outerHTML(value) { throw new Error('outerHTML wurde gesetzt'); }
  insertAdjacentHTML() { throw new Error('insertAdjacentHTML wurde aufgerufen'); }
}
function dump(node) {
  if (node instanceof TextNode) return {tag: '#text', text: node.ownText};
  return {tag: node.tagName, text: node.ownText, className: node.className, title: node.title, children: node.children.map(dump)};
}
function walk(node, visit) { visit(node); (node.children || []).forEach(child => walk(child, visit)); }

// Ein Auswahlfeld wie im Browser: Ein Wert, den es unter den Optionen nicht gibt, wählt nichts (``value`` ist dann leer, ``selectedOptions`` leer)
class Select {
  constructor(values) { this.values = values; this.index = 0; this.title = ''; }
  get value() { return this.index < 0 ? '' : this.values[this.index]; }
  set value(v) { this.index = this.values.indexOf(String(v)); }
  get selectedOptions() { return this.index < 0 ? [] : [{text: 'Konto ' + this.values[this.index]}]; }
}
const table = {insertBefore: (row, before) => { rows.splice(rows.indexOf(before), 0, row); }};
function makeRow(name, classes, values) {
  const row = {name, className: classes.join(' '), style: {}, dataset: {}, parentNode: table,
    fields: {'.splitAmount': {value: ''}, '[name=split_coa]': new Select(scenario.options), '[name=split_counter_coa]': {value: ''},
             '[name=split_tax_rate]': {value: ''}, '[name=split_tax_amount]': {value: ''}, '[name=split_text]': {value: ''},
             '[name=split_docnr]': {value: ''}},
    querySelector: selector => row.fields[selector], cloneNode: () => makeRow('new', ['splitTemplate']),
    remove: () => { rows.splice(rows.indexOf(row), 1); }};
  Object.entries(values || {}).forEach(([selector, value]) => { row.fields[selector].value = value; });
  return row;
}
const template = makeRow('template', ['splitTemplate']);
const rows = scenario.rows.map(spec => makeRow(spec.name, spec.classes, spec.values)).concat([template]);
function classesOf(row) { return row.className.split(' '); }
// querySelectorAll wie ein Browser für ``#splitTable tr.splitRow``, mit oder ohne ``:not(.klasse)``
function select(selector) {
  const excluded = (selector.match(/:not\(\.(\w+)\)/) || [])[1];
  return rows.filter(row => classesOf(row).includes('splitRow') && !(excluded && classesOf(row).includes(excluded)));
}

const byId = {};
const gone = new Set();     // Elemente, die es nicht mehr gibt (getElementById liefert null): die Maske wurde ausgetauscht
(scenario.gone || []).forEach(id => gone.add(id));
const form = scenario.documentNr === null ? null : {elements: {document_nr: {value: scenario.documentNr}}};
const fetches = [], msgs = [];
const responses = scenario.responses.slice();
function element(id) {
  return byId[id] || (byId[id] = Object.assign(new Element('div'), {dataset: Object.assign({}, (scenario.datasets || {})[id] || {})}));
}
const sandbox = {
  Node,
  document: {
    getElementById: id => (gone.has(id) ? null : element(id)),
    createElement: tag => new Element(tag),
    createTextNode: text => new TextNode(text),
    querySelectorAll: selector => select(selector),
    querySelector: selector => {
      if (selector === '#splitTable tr.splitTemplate') return template;
      if (selector === 'form[action="/transactions/add"]') return form;
      if (selector === 'form [name=document_nr]') return form ? form.elements.document_nr : null;
      return null;
    },
  },
  fetch: (url, options) => {
    fetches.push({url, method: (options && options.method) || 'GET'});
    const next = scenario.networkError ? (scenario.errorEffects || {}) : responses.shift();
    // Was auf der Seite geschieht, BEVOR die Antwort eintrifft (oder der Fehler): eine andere oder neue Buchung wird geöffnet (das data-Attribut ändert
    // sich oder das Element fehlt), oder der Nutzer legt eine Zeile im Editor an
    Object.entries(next.dom || {}).forEach(([id, change]) => {
      if (change === null) gone.add(id); else Object.assign(element(id).dataset, change);
    });
    if (next.addRow) rows.splice(rows.length - 1, 0, makeRow('typed', ['splitRow']));
    if (scenario.networkError) return Promise.reject(vm.runInContext('new TypeError("offline")', sandbox));
    return Promise.resolve({ok: next.ok, status: next.status, text: () => Promise.resolve(next.body)});
  },
  appMsg: (text, type) => msgs.push([text, type]),
  appConfirm: (text, onOk) => onOk(),
};
vm.createContext(sandbox);
scenario.sources.forEach(source => vm.runInContext(source, sandbox));

function findClickable(label) {
  let found = null;
  Object.values(byId).forEach(root => walk(root, node => {
    if (!found && node.listeners && node.listeners.click && node.ownText === label) found = node;
  }));
  return found;
}
const wait = () => new Promise(resolve => setTimeout(resolve, 25));
(async () => {
  if (scenario.call) { vm.runInContext(scenario.call, sandbox); await wait(); }
  for (const label of scenario.clicks) {
    const target = findClickable(label);
    if (!target) throw new Error('nichts zum Anklicken: ' + label);
    target.listeners.click({preventDefault() {}});
    await wait();
  }
  const note = element('suggestNote');
  console.log(JSON.stringify({
    fetches, msgs, note: {text: note.ownText, className: note.className}, summary: element('splitSummary').ownText,
    rows: rows.map(row => ({name: row.name, className: row.className, dataset: row.dataset,
                            values: Object.fromEntries(Object.entries(row.fields).map(([selector, field]) => [selector, field.value]))})),
    tree: dump(element('txReceipts')),
  }));
})();
"""

BANK_TEXT = 'Verwendungszweck Probe 4711'
OPTIONS = ['', '406815', '404400']


def blocks_of(html, *markers):
    """Die Skriptblöcke der Seite, in denen je einer der Marker steht (in der Reihenfolge der Marker; je Marker genau einer)."""
    found = []
    for marker in markers:
        (block,) = [text for text in Dom(html).scripts if marker in text]
        found.append(block)
    return found


def run_script(tmp_path, html, rows=(), responses=(), call=None, clicks=(), documentNr='', gone=(), networkError=False, with_suggest=True, **scenario):
    """Die Skripte der Bankmaske (Editor, Vorschlag, Belege) in Node ausführen – mit einer kleinen DOM-Attrappe: Zeilen des Editors, Auswahl der Konten,
    ``fetch`` mit festen Antworten, ``appMsg``. ``innerHTML``, ``outerHTML`` und ``insertAdjacentHTML`` werfen."""
    markers = ['function splitRows()'] + (['function suggestAppendRow('] if with_suggest else []) + ['function txPropose(']
    scenario = {'sources': blocks_of(html, *markers), 'rows': list(rows), 'options': OPTIONS, 'responses': list(responses), 'call': call,
                'clicks': list(clicks), 'documentNr': documentNr, 'gone': list(gone), 'networkError': networkError,
                'datasets': {'txReceipts': {'booking': str(script_data(html, 'TX_LINK_BOOKING_ID'))}}, **scenario}
    harness, data = tmp_path / 'harness.js', tmp_path / 'scenario.json'
    harness.write_text(HARNESS, encoding='utf-8')
    data.write_text(json.dumps(scenario), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(data)], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def reply(payload, status=200, **effects):
    return {'ok': 200 <= status < 300, 'status': status, 'body': payload if isinstance(payload, str) else json.dumps(payload), **effects}


def full_answer(account=406815, **over):
    row = {'amount': '-119.00', 'coa_id': account, 'tax_rate': '19', 'tax_amount': '-19.00'}
    data = {'success': True, 'ok': True, 'message': DONE_TEXT, 'reasons': [REASON_AMOUNT, REASON_TAX.format(rate='19'), REASON_ACCOUNT_RECIPIENT],
            'hints': [], 'row': row}
    data.update(over)
    return data


@pytest.fixture
def bank_mask(db, acct):
    """Die Maske einer Bankbewegung (Verwendungszweck ``BANK_TEXT``) mit einem verknüpften Beleg der Art ``receipt`` und einem der Art ``contract``."""
    bank = add_bank(db, acct, text=BANK_TEXT)
    receipt, contract = add_receipt(db, 'B-1'), add_receipt(db, 'B-2')
    db.link_receipt_to_booking(bank, receipt)
    db.link_receipt_to_booking(bank, contract, 'contract')
    return SimpleNamespace(db=db, bank=bank, receipt=receipt, contract=contract, html=mask(db, bank))


LOCKED = {'name': 'locked', 'classes': ['splitRow', 'splitLocked'], 'values': {'.splitAmount': '-5.00', '[name=split_text]': 'Zahlung'}}
TYPED = {'name': 'typed', 'classes': ['splitRow'], 'values': {'.splitAmount': '-1.00'}}


@NO_NODE
def test_the_button_stands_at_a_receipt_of_the_kind_receipt_only_between_receipt_and_release(tmp_path, bank_mask):
    out = run_script(tmp_path, bank_mask.html)
    (table,) = [node for node in walk(out['tree']) if node['tag'] == 'table']
    body = [row for row in table['children'] if row['children'][0]['tag'] == 'td']
    assert len(body) == 2
    actions = {row['children'][0]['text']: [child for child in row['children'][4]['children'][0]['children'] if child['tag'] != '#text'] for row in body}
    assert [(node['tag'], node['text']) for node in actions['B-1']] == [('a', 'Beleg'), ('button', 'Als Buchungssatz'), ('button', 'Lösen')]
    assert [(node['tag'], node['text']) for node in actions['B-2']] == [('a', 'Beleg'), ('button', 'Lösen')]               # keiner für einen Vertrag
    button = actions['B-1'][1]
    assert (button['className'], button['title']) == ('coloredButton btn-sm bg-blue', EXPECTED_TEXTS['title'])
    assert out['fetches'] == [] and out['msgs'] == []                                                                     # nichts geschieht ohne Klick


@NO_NODE
def test_the_mask_of_an_entry_has_no_button(tmp_path, db, acct):
    entry = db.insert_booking(DAY, -119.00, booking_type='entry', recipient_client=SUPPLIER, coa_id=coa(db, 6815))
    db.link_receipt_to_booking(entry, add_receipt(db, 'B-1'))
    html = mask(db, entry)
    sources = [text for text in Dom(html).scripts if 'txReceiptsRender' in text]
    assert len(sources) == 1
    out = run_script_without_editor(tmp_path, html)
    (table,) = [node for node in walk(out['tree']) if node['tag'] == 'table']
    texts = [node['text'] for node in walk(table) if node['tag'] == 'button']
    assert texts == ['Lösen']


def run_script_without_editor(tmp_path, html):
    """Die Seite einer Buchung ohne Editor (kein Skript des Editors, kein Skript des Vorschlags für Zeilen): nur der Block der Belege."""
    scenario = {'sources': blocks_of(html, 'function txPropose('), 'rows': [], 'options': OPTIONS, 'responses': [], 'call': None, 'clicks': [],
                'documentNr': '', 'gone': [], 'networkError': False, 'datasets': {'txReceipts': {'booking': str(script_data(html, 'TX_LINK_BOOKING_ID'))}}}
    harness, data = tmp_path / 'harness.js', tmp_path / 'scenario.json'
    harness.write_text(HARNESS, encoding='utf-8')
    data.write_text(json.dumps(scenario), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(data)], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@NO_NODE
@pytest.mark.parametrize('rows', [[TYPED], [LOCKED], [LOCKED, TYPED]], ids=['Zeile', 'gesperrte-Zeile', 'beide'])
def test_an_editor_that_has_rows_gets_no_request_and_the_note_and_is_left_as_it_was(tmp_path, bank_mask, rows):
    out = run_script(tmp_path, bank_mask.html, rows=rows, call=f'txPropose({bank_mask.receipt})')
    assert out['fetches'] == [] and out['msgs'] == []
    assert out['note'] == {'text': REFUSE_HAS_ROWS, 'className': 'warnColor'}
    assert [(row['name'], row['values']['.splitAmount']) for row in out['rows']] == [(spec['name'], spec['values']['.splitAmount']) for spec in rows] + [('template', '')]
    for row, spec in zip(out['rows'], rows):
        assert row['values']['[name=split_text]'] == spec['values'].get('[name=split_text]', '') and row['className'] == ' '.join(spec['classes'])


@NO_NODE
def test_a_click_on_the_button_asks_the_route_with_both_ids_and_fills_exactly_one_new_row(tmp_path, bank_mask):
    out = run_script(tmp_path, bank_mask.html, clicks=['Als Buchungssatz'], documentNr='ER-7', responses=[reply(full_answer())])
    assert out['fetches'] == [{'url': f'/transactions/receipt-proposal?id={bank_mask.bank}&document={bank_mask.receipt}', 'method': 'GET'}]
    assert out['msgs'] == []
    assert [row['name'] for row in out['rows']] == ['new', 'template']                                   # genau eine neue Zeile, vor der Vorlage
    new = out['rows'][0]
    assert new['className'] == 'splitRow' and new['values'] == {
        '.splitAmount': '-119.00', '[name=split_coa]': '406815', '[name=split_counter_coa]': '', '[name=split_tax_rate]': '19',
        '[name=split_tax_amount]': '-19.00', '[name=split_text]': BANK_TEXT, '[name=split_docnr]': 'ER-7'}
    assert new['dataset'] == {}                                                                          # keine Umbuchung
    assert out['note'] == {'text': ' · '.join([DONE_TEXT, REASON_AMOUNT, REASON_TAX.format(rate='19'), REASON_ACCOUNT_RECIPIENT]), 'className': 'successColor'}
    assert '✓' in out['summary']                                                                         # die Summenzeile rechnet: −119,00 von −119,00
    template = out['rows'][1]
    assert template['className'] == 'splitTemplate' and set(template['values'].values()) == {''}        # die Vorlage bleibt unberührt


@NO_NODE
def test_the_number_of_the_row_is_the_field_of_the_mask_trimmed_and_empty_stays_empty(tmp_path, bank_mask):
    for field, expected in (('  ER-7 ', 'ER-7'), ('', ''), ('   ', ''), (None, '')):
        out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', documentNr=field, responses=[reply(full_answer())])
        assert out['rows'][0]['values']['[name=split_docnr]'] == expected, field


@NO_NODE
def test_the_fetch_is_a_plain_get_and_the_ids_are_encoded(tmp_path, bank_mask):
    out = run_script(tmp_path, bank_mask.html, call="txPropose('7&x=1')", responses=[reply(full_answer())])
    assert out['fetches'] == [{'url': f'/transactions/receipt-proposal?id={bank_mask.bank}&document=7%26x%3D1', 'method': 'GET'}]


@NO_NODE
def test_a_refusal_changes_only_the_note(tmp_path, bank_mask):
    answer_ = full_answer(ok=False, message=REFUSE_AMOUNT, reasons=[], hints=[], row=None)
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[reply(answer_)])
    assert out['note'] == {'text': REFUSE_AMOUNT, 'className': 'warnColor'}
    assert [row['name'] for row in out['rows']] == ['template'] and out['msgs'] == []


@NO_NODE
def test_the_hints_of_the_answer_turn_the_note_into_a_warning_and_stay_in_it(tmp_path, bank_mask):
    answer_ = full_answer(hints=[HINT_TAX, HINT_NO_ACCOUNT], reasons=[REASON_AMOUNT], row={'amount': '-119.00', 'coa_id': '', 'tax_rate': '', 'tax_amount': ''})
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[reply(answer_)])
    assert out['note'] == {'text': ' · '.join([DONE_TEXT, REASON_AMOUNT, HINT_TAX, HINT_NO_ACCOUNT]), 'className': 'warnColor'}
    assert out['rows'][0]['values']['[name=split_coa]'] == '' and out['rows'][0]['values']['.splitAmount'] == '-119.00'
    assert out['rows'][0]['values']['[name=split_tax_rate]'] == '' and out['rows'][0]['values']['[name=split_tax_amount]'] == ''


@NO_NODE
def test_the_real_answer_for_an_account_of_the_wrong_kind_leaves_the_field_empty_and_turns_the_note_into_a_warning(tmp_path, db, acct):
    """Der Handler verwirft ein Privatkonto aus der Historie: Die Maske trägt Betrag und Steuer ein, lässt das Konto leer und zeigt den festen Hinweis in Warnfarbe."""
    past(db, acct, '2026-01-10', 2100)
    past(db, acct, '2026-02-10', 2100)
    receipt = add_receipt(db)
    save_reading(db, receipt)
    bank = add_bank(db, acct, text=BANK_TEXT)
    db.link_receipt_to_booking(bank, receipt)
    status, body = handlers.handle_receipt_proposal(db, bank, receipt)
    assert status == 200 and json.loads(body)['hints'] == [HINT_ACCOUNT_KIND]
    out = run_script(tmp_path, mask(db, bank), call=f'txPropose({receipt})', responses=[reply(body)])
    new = out['rows'][0]['values']
    assert (new['.splitAmount'], new['[name=split_coa]'], new['[name=split_tax_rate]'], new['[name=split_tax_amount]']) == ('-119.00', '', '19', '-19.00')
    assert out['note']['className'] == 'warnColor' and out['note']['text'].endswith(' · ' + HINT_ACCOUNT_KIND) and out['msgs'] == []


@NO_NODE
def test_an_account_that_the_selection_does_not_offer_leaves_the_field_empty_and_adds_the_hint(tmp_path, bank_mask):
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[reply(full_answer(account=999999))])
    new = out['rows'][0]
    assert new['values']['[name=split_coa]'] == '' and new['values']['.splitAmount'] == '-119.00'
    assert out['note']['className'] == 'warnColor' and out['note']['text'].endswith(' · ' + HINT_ACCOUNT_HIDDEN)


@NO_NODE
def test_an_answer_for_another_mask_does_nothing(tmp_path, bank_mask):
    for effects in ({'dom': {'txReceipts': {'booking': '99'}}}, {'dom': {'txReceipts': None}}):
        out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[reply(full_answer(), **effects)])
        assert [row['name'] for row in out['rows']] == ['template'] and out['msgs'] == []
        assert out['note']['text'] == EXPECTED_TEXTS['busy']                                              # die Antwort fasst die Meldung nicht an


@NO_NODE
def test_a_row_that_appears_while_the_request_runs_is_never_overwritten_or_added_to(tmp_path, bank_mask):
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[reply(full_answer(), addRow=True)])
    assert [row['name'] for row in out['rows']] == ['typed', 'template']
    assert out['rows'][0]['values']['.splitAmount'] == '' and out['note'] == {'text': REFUSE_HAS_ROWS, 'className': 'warnColor'}


@NO_NODE
def test_a_network_error_and_an_error_of_the_server_are_shown_as_a_message_and_the_note_is_cleared(tmp_path, bank_mask):
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', networkError=True)
    assert out['msgs'] == [['Die Verbindung zum Server ist fehlgeschlagen.', 'error']] and out['note']['text'] == ''
    assert [row['name'] for row in out['rows']] == ['template']
    failed = reply({'success': False, 'error': receipt_links.PROPOSAL_FAILED}, status=500)
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[failed])
    assert out['msgs'] == [[receipt_links.PROPOSAL_FAILED, 'error']] and out['note']['text'] == ''
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[reply('kein JSON', status=503)])
    assert out['msgs'] == [['Der Server hat die Anfrage abgelehnt (Status 503).', 'error']]


@NO_NODE
def test_an_error_that_arrives_for_another_mask_is_not_shown_and_leaves_the_note_alone(tmp_path, bank_mask):
    """Die Maske wurde ausgetauscht, bevor der Fehler eintraf: Die Meldung gehörte zu einer Buchung, die nicht mehr offen ist – weder ``appMsg`` noch die Notiz."""
    other = {'dom': {'txReceipts': {'booking': '99'}}}
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', networkError=True, errorEffects=other)
    assert out['msgs'] == [] and out['note']['text'] == EXPECTED_TEXTS['busy'] and [row['name'] for row in out['rows']] == ['template']
    failed = reply({'success': False, 'error': receipt_links.PROPOSAL_FAILED}, status=500, **other)
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[failed])
    assert out['msgs'] == [] and out['note']['text'] == EXPECTED_TEXTS['busy']
    rejected = reply('kein JSON', status=503, dom={'txReceipts': None})                              # die Maske ist ganz weg
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[rejected])
    assert out['msgs'] == [] and out['note']['text'] == EXPECTED_TEXTS['busy']


@NO_NODE
def test_hostile_values_of_the_answer_become_text_never_markup(tmp_path, bank_mask):
    answer_ = full_answer(message=HOSTILE, reasons=['<img src=x onerror=1>'], hints=['"><script>alert(1)</script>'])
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', responses=[reply(answer_)])
    assert out['note']['text'] == ' · '.join([HOSTILE, '<img src=x onerror=1>', '"><script>alert(1)</script>'])    # Text; ein innerHTML würfe im Lauf


@NO_NODE
def test_without_the_script_of_the_suggestion_or_without_an_editor_nothing_happens(tmp_path, bank_mask):
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', with_suggest=False, responses=[reply(full_answer())])
    assert out['fetches'] == [] and out['msgs'] == []
    out = run_script(tmp_path, bank_mask.html, call=f'txPropose({bank_mask.receipt})', gone=['splitTable'], responses=[reply(full_answer())])
    assert out['fetches'] == [] and out['msgs'] == []


def test_the_script_of_the_suggestion_keeps_one_function_that_makes_the_row_and_the_history_uses_it_too(db, acct):
    """Die Zeile des Vorschlags aus der Historie (🪄) und die des Buchungsvorschlags entstehen an EINER Stelle: ``suggestAppendRow``."""
    bank, _receipt, _account = a_world(db, acct)
    (block,) = blocks_of(mask(db, bank), 'function suggestAppendRow(')
    assert block.count('function suggestAppendRow(') == 1 and block.count('tpl.cloneNode(true)') == 1
    fill = block[block.index('function suggestFillSplit('):block.index('function suggestAppendRow(')]
    assert 'suggestAppendRow(' in fill and 'cloneNode' not in fill
    # sie steht HINTER ``suggestFillSplit``, als letzte Funktion des Blocks: Der Lauf in test_split_editor_payment_rows_locked.py nimmt den Block von dort bis zum Ende
    assert block.rstrip().endswith('}') and block.rindex('function ') == block.index('function suggestAppendRow(')
