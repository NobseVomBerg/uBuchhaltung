# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belege und Buchungen von beiden Seiten mit einer Suche verknüpfen (2.5.12).

Fehlfall: Die Maske einer Buchung zeigte „Verknüpfte Dokumente“ ohne Möglichkeit, einen Beleg zu suchen; auf der Seite eines Belegs
verlangte das Formular die rohe ID einer Buchung. Jetzt:

* ``Database.link_receipt_to_booking`` prüft, schreibt die Zeile und – **trägt die Buchung keine Beleg-Nr.** – die Nummer des Belegs
  in EINER Transaktion (Replay-Log erst nach dem Commit). Nur die verknüpfte Zeile (Kinder einer Bankbewegung erben nichts), nur bei einer
  Verknüpfung der Art „Beleg“, nie ein Zahlungssatz einer Rechnungszuordnung, nie eine Nummer, die eine Rechnung trägt – und **nie eine
  Nummer, die eine andere Buchung trägt oder der sie ähnelt** (die eine in der anderen enthalten; ausgenommen die eigene Familie): Die
  Nummer wirkt wie eine von Hand eingetragene, auch auf den automatischen Abgleich (dieselbe Nummer am selben Tag = ein Beleg; Stufe 4
  vergleicht Teilzeichenketten). Unterbleibt sie, sagt die Antwort es (``numberInUse``). Lösen lässt die Nummer stehen.
* ``POST /bookings/link-document`` und ``/bookings/unlink-document`` (JSON, IDs streng, 404 mit Klartext), ``GET /receipts/search`` und
  ``/transactions/search`` (rein lesend, Herkunft geprüft, nie ein Pfad in der Antwort, ohne Rücksicht auf die Schreibweise).
* Die Seite eines Belegs (``/receipts/edit?id=``) und die Maske tragen die Daten als JSON im Skriptblock; das Skript baut die Tabellen
  nur über ``textContent``/``createElement`` (Node-Läufe unten; ohne Node entfallen nur diese).

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.parse
from decimal import Decimal

import pytest

import userctx
from db import Database, DeleteRefused
from db.bookings import (BOOKING_GONE, RECEIPT_GONE, RELATION_DEFAULT, RELATION_LABELS, RELATION_TYPES, LinkRefused,
                         LinkResult)
from db.core import SEARCH_LIMIT_MAX
from server import handlers, receipt_links
from server.pages_receipts import RECEIPT_LINK_JS, PageReceiptEdit, PageReceipts
from server.pages_transactions import TX_LINK_JS, PageTransactions
from tests import installation
from tests.document_helpers import INVOICE_NUMBER, QUOTE_NUMBER, make_books, post_json, state
from tests.dom_helpers import Dom
from tests.entry_form_helpers import bank_account, coa, query
from tests.http_client import FOREIGN_ORIGIN, get, own_origin

DAY = '2026-03-04'
IBAN = 'DE89370400440532013000'
SECRET = 'geheimer Fehlertext /home/niemand/buch.db sqlite3'
PDF = b'%PDF-1.4\n% ein Beleg\n'
IMG = '<img src=x onerror=1>'
HOSTILE = '</script><img src=x onerror=1>'
LINK_ROUTES = ['/bookings/link-document', '/bookings/unlink-document']
INVALID_REQUEST = {'success': False, 'error': 'Ungültige Anfrage.'}


# ── Hilfen ───────────────────────────────────────────────────────────────────
def add_receipt(db, number, date=DAY, filename='beleg.pdf', path='', info=''):
    """Einen Beleg anlegen; liefert seine ID."""
    db.insert_receipt(number, date, filename, path, info)
    return db.fetch_receipts()[-1][0]


def add_booking(db, **fields):
    """Einen eigenständigen Buchungssatz (ohne Beleg-Nr., wenn nicht genannt); liefert seine ID."""
    fields.setdefault('date_booking', DAY)
    fields.setdefault('amount', 100)
    fields.setdefault('booking_type', 'entry')
    return db.insert_booking(**fields)


def raw(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql, args).fetchall()


def run_sql(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute(sql, args)
        con.commit()


def number_of(db, booking):
    return raw(db, 'SELECT DocumentNumber FROM Bookings WHERE ID = ?', booking)[0][0]


def set_number(db, booking, value):
    """Eine Beleg-Nr. roh schreiben – so, wie sie auch von Hand, per SQL-Konsole oder aus einem Altbestand dort stehen kann (ohne ``strip``)."""
    run_sql(db, 'UPDATE Bookings SET DocumentNumber = ? WHERE ID = ?', value, booking)


def links(db):
    return raw(db, 'SELECT Booking_ID, Document_ID, RelationType FROM BookingDocuments ORDER BY ID')


def assert_not_locked(db):
    """Die Datenbank ist frei: Eine zweite Verbindung bekommt die Schreibsperre sofort."""
    with contextlib.closing(sqlite3.connect(db.db_name, timeout=0.5)) as con:
        con.execute('BEGIN IMMEDIATE')
        con.rollback()


@pytest.fixture
def logged(monkeypatch):
    """Alles, was die Datenbankschicht ins Replay-Log schreiben will, als ``(SQL, Parameter, Beschreibung)`` – ohne Datei."""
    entries = []
    monkeypatch.setattr(Database, '_log_sql', lambda self, sql, params, description: entries.append((sql, params, description)))
    return entries


# ── Verknüpfen: die Nummer des Belegs ────────────────────────────────────────
def test_a_booking_without_a_number_takes_the_one_of_the_receipt_and_the_log_follows_the_commit(tmp_db, monkeypatch):
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db, recipient_client='Muster GmbH')
    seen = []

    def log(self, sql, params, description):
        seen.append((sql, params, description, number_of(tmp_db, booking), links(tmp_db)))     # was schon festgeschrieben ist

    monkeypatch.setattr(Database, '_log_sql', log)
    result = tmp_db.link_receipt_to_booking(booking, receipt)
    assert result == LinkResult(created=True, number_taken=True, document_number='26B001', number_in_use=False, has_entries=False)
    assert links(tmp_db) == [(booking, receipt, 'receipt')] and number_of(tmp_db, booking) == '26B001'
    # EIN Eintrag, erst nach dem Commit: Als er geschrieben wird, ist Zeile und Nummer schon in der Datenbank
    assert seen == [('UPDATE Bookings SET DocumentNumber=? WHERE ID=?', ('26B001', booking), 'Receipt link: document number',
                     '26B001', [(booking, receipt, 'receipt')])]


@pytest.mark.parametrize('existing', ['ER-7', ' ER-7 '])
def test_a_booking_with_a_number_keeps_it_and_is_still_linked(tmp_db, logged, existing):
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db, document_number=existing)
    before = state(tmp_db, 'Bookings')
    result = tmp_db.link_receipt_to_booking(booking, receipt)
    assert result == LinkResult(created=True, number_taken=False, document_number='ER-7', number_in_use=False, has_entries=False)
    assert state(tmp_db, 'Bookings') == before                                  # keine Spalte der Buchung hat sich geändert
    assert links(tmp_db) == [(booking, receipt, 'receipt')]
    assert logged == []                                                         # und es gibt nichts zu protokollieren


@pytest.mark.parametrize('existing', [None, '', '   '])
def test_a_number_of_nothing_but_whitespace_counts_as_empty(tmp_db, existing):
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db, document_number=existing)
    assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(True, True, '26B001', False, False)
    assert number_of(tmp_db, booking) == '26B001'


@pytest.mark.parametrize('number', [None, '', '   '])
def test_a_receipt_without_a_number_passes_nothing_on(tmp_db, logged, number):
    receipt = add_receipt(tmp_db, number)
    booking = add_booking(tmp_db)
    assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(True, False, '', False, False)
    assert number_of(tmp_db, booking) is None and links(tmp_db) == [(booking, receipt, 'receipt')]
    assert logged == []


def test_the_number_of_the_receipt_is_written_without_the_whitespace_around_it(tmp_db):
    receipt = add_receipt(tmp_db, '  26B001 ')
    booking = add_booking(tmp_db)
    assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(True, True, '26B001', False, False)
    assert number_of(tmp_db, booking) == '26B001'


def test_linking_again_is_idempotent_keeps_the_kind_of_the_first_link_and_an_unknown_kind_is_a_receipt(tmp_db):
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    # eine Verknüpfung der Art „Rechnung“ gibt die Nummer nicht weiter – und die erste Art bleibt auch beim zweiten Aufruf
    assert tmp_db.link_receipt_to_booking(booking, receipt, 'invoice') == LinkResult(True, False, '', False, False)
    assert tmp_db.link_receipt_to_booking(booking, receipt, 'receipt') == LinkResult(False, False, '', False, False)
    assert links(tmp_db) == [(booking, receipt, 'invoice')] and number_of(tmp_db, booking) is None   # eine Zeile, die Art der ersten bleibt
    other = add_receipt(tmp_db, '26B002')
    for kind in ('evil', '', None, 5, 'RECEIPT'):
        fresh = add_booking(tmp_db)
        tmp_db.link_receipt_to_booking(fresh, other, kind)
        assert links(tmp_db)[-1] == (fresh, other, RELATION_DEFAULT), kind
    assert set(RELATION_TYPES) == {'receipt', 'invoice', 'contract', 'other'} and set(RELATION_LABELS) == set(RELATION_TYPES)


def test_the_number_rule_also_applies_to_a_link_that_already_existed(tmp_db):
    """Ein Bestand, der über den älteren Weg (``link_booking_to_document``) mit der Art „Beleg“ verknüpft wurde, bekommt die Nummer beim
    erneuten Zuordnen; maßgeblich ist die gespeicherte Art (eine ältere Verknüpfung anderer Art gibt nichts weiter: siehe unten)."""
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    tmp_db.link_booking_to_document(booking, receipt)
    assert number_of(tmp_db, booking) is None                                   # der ältere Weg übernimmt nichts
    assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(
        created=False, number_taken=True, document_number='26B001', number_in_use=False, has_entries=False)
    assert links(tmp_db) == [(booking, receipt, 'receipt')] and number_of(tmp_db, booking) == '26B001'


def test_only_the_linked_row_gets_the_number_the_children_of_a_bank_movement_inherit_nothing(tmp_db):
    receipt = add_receipt(tmp_db, '26B001')
    other = add_receipt(tmp_db, '26B002')
    bank = add_booking(tmp_db, amount=-300, booking_type='bank')
    first = add_booking(tmp_db, amount=-100, booking_type='entry', parent_booking_id=bank, text='Satz A')
    second = add_booking(tmp_db, amount=-200, booking_type='entry', parent_booking_id=bank, text='Satz B')
    before = state(tmp_db, 'Bookings')['Bookings']
    taken = tmp_db.link_receipt_to_booking(bank, receipt)
    assert taken.number_taken is True and taken.has_entries is True                # die Meldung nennt, dass die Sätze ihre Nummern behalten
    after = state(tmp_db, 'Bookings')['Bookings']
    changed = [(old, new) for old, new in zip(before, after) if old != new]
    assert len(before) == len(after) == 3 and [new[0] for _, new in changed] == [bank]      # Spalte für Spalte: nur die Bankzeile
    old, new = changed[0]
    assert new[16] == '26B001' and old[:16] == new[:16] and old[17:] == new[17:]          # und nur ihre Beleg-Nr.
    # Ein Kind lässt sich selbst verknüpfen und bekommt dann seine eigene Nummer – die Bankzeile und das Geschwister bleiben
    assert tmp_db.link_receipt_to_booking(first, other) == LinkResult(True, True, '26B002', False, False)
    assert [number_of(tmp_db, row) for row in (bank, first, second)] == ['26B001', '26B002', None]


def test_the_number_of_a_payment_row_of_an_invoice_is_never_taken(tmp_db, logged):
    """Zahlungssätze (``Status='invoice_payment'``) tragen immer die Rechnungsnummer: Das Verknüpfen darf sie nicht belegen."""
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    run_sql(tmp_db, "UPDATE Bookings SET Status = 'invoice_payment', DocumentNumber = NULL WHERE ID = ?", booking)
    before = state(tmp_db, 'Bookings')
    assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(True, False, '', False, False)
    assert state(tmp_db, 'Bookings') == before and links(tmp_db) == [(booking, receipt, 'receipt')] and logged == []


def test_a_number_that_an_invoice_or_quote_carries_is_never_taken_but_any_other_is(auto_login):
    """Forderung und Zahlungssätze werden über die Rechnungsnummer gefunden: Das Zuordnen darf keine Buchung mit dieser Nummer erzeugen."""
    db = make_books(auto_login).db
    booking = add_booking(db)
    for number in (INVOICE_NUMBER, QUOTE_NUMBER):
        receipt = add_receipt(db, number)
        assert db.link_receipt_to_booking(booking, receipt) == LinkResult(True, False, '', False, False)
        assert number_of(db, booking) is None
    free = add_receipt(db, '26B009')                                              # Gegenprobe: jede andere Nummer wird übernommen
    assert db.link_receipt_to_booking(booking, free) == LinkResult(True, True, '26B009', False, False)
    assert number_of(db, booking) == '26B009'


# ── Verknüpfen: nie eine Nummer, die eine andere Buchung trägt oder der sie ähnelt ───────────────────────────
def test_two_standalone_rows_of_one_day_with_one_receipt_only_the_first_takes_the_number(tmp_db, logged):
    """Dieselbe Beleg-Nr. und dasselbe Datum an zwei eigenständigen Sätzen fasste der automatische Abgleich zu EINEM Beleg zusammen
    (Stufe 3): Der zweite Satz bekommt die Nummer nicht – verknüpft wird er trotzdem, und die Antwort sagt, warum."""
    receipt = add_receipt(tmp_db, '26B010')
    first = add_booking(tmp_db, amount=-50)
    second = add_booking(tmp_db, amount=-30)
    assert tmp_db.link_receipt_to_booking(first, receipt) == LinkResult(True, True, '26B010', False, False)
    assert tmp_db.link_receipt_to_booking(second, receipt) == LinkResult(True, False, '', True, False)
    assert [number_of(tmp_db, row) for row in (first, second)] == ['26B010', None]
    assert links(tmp_db) == [(first, receipt, 'receipt'), (second, receipt, 'receipt')]
    assert len(logged) == 1                                                       # nur die Übernahme des ersten steht im Replay-Log
    assert tmp_db.link_receipt_to_booking(second, receipt) == LinkResult(False, False, '', True, False)    # auch erneut: nichts ändert sich
    assert number_of(tmp_db, second) is None and len(logged) == 1


@pytest.mark.parametrize('other_number, receipt_number', [
    pytest.param('26B001', '26B001', id='gleich'),
    pytest.param('26B0010', '26B001', id='die-andere-enthaelt-die-Nummer-des-Belegs'),
    pytest.param('26B001', '26B0010', id='die-Nummer-des-Belegs-enthaelt-die-andere'),
    pytest.param('26B001_A', '26B001', id='mit-Anhang'),
    pytest.param('x26B001', '26B001', id='mit-Vorsatz'),
    pytest.param('26b001', '26B001', id='Schreibweise-der-anderen'),
    pytest.param('26B001', '26b001', id='Schreibweise-des-Belegs'),
    pytest.param('  26B001  ', '26B001', id='Leerraum-am-Rand-der-anderen'),
    pytest.param('26B001', '  26B001 ', id='Leerraum-am-Rand-des-Belegs'),
])
def test_a_number_that_another_booking_carries_or_resembles_is_not_taken(tmp_db, logged, other_number, receipt_number):
    other = add_booking(tmp_db)
    set_number(tmp_db, other, other_number)
    receipt = add_receipt(tmp_db, receipt_number)
    booking = add_booking(tmp_db)
    assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(True, False, '', True, False)
    assert number_of(tmp_db, booking) is None and links(tmp_db) == [(booking, receipt, 'receipt')] and logged == []
    assert number_of(tmp_db, other) == other_number                                 # die andere Buchung bleibt, wie sie war


@pytest.mark.parametrize('other_number', ['ER-7', '26B002', '26B01', '2026B', None, '', '   '])
def test_another_number_or_none_does_not_hinder_the_takeover(tmp_db, other_number):
    """Gegenprobe: Nur eine enthaltene Nummer zählt als ähnlich. Eine leere (auch nur aus Leerraum) nie – die leere Zeichenkette steckt in
    jeder Nummer."""
    other = add_booking(tmp_db)
    set_number(tmp_db, other, other_number)
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(True, True, '26B001', False, False)
    assert number_of(tmp_db, booking) == '26B001'


@pytest.mark.parametrize('stored', [2600010, b'2600010'], ids=['Ganzzahl', 'Blob'])
def test_a_number_that_is_not_stored_as_text_counts_like_its_text(tmp_db, stored):
    """Ein Altbestand oder die SQL-Konsole kann eine Zahl oder einen Blob als Beleg-Nr. tragen (die Spalte hat Textaffinität: Eine Zahl
    wird dort als Text gespeichert, ein Blob bleibt einer). ``TRIM``/``LOWER``/``INSTR`` lesen beides als Text; kein Fehler."""
    other = add_booking(tmp_db)
    set_number(tmp_db, other, stored)
    receipt = add_receipt(tmp_db, '260001')
    booking = add_booking(tmp_db)
    assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(True, False, '', True, False)
    assert number_of(tmp_db, booking) is None
    free = add_receipt(tmp_db, 'ER-9')                                              # Gegenprobe: eine unähnliche Nummer wird übernommen
    assert tmp_db.link_receipt_to_booking(booking, free) == LinkResult(True, True, 'ER-9', False, False)


def test_a_booking_that_carries_a_blob_as_its_number_counts_as_numbered_and_does_not_fail(tmp_db, logged):
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    set_number(tmp_db, booking, b'ER-7')
    assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(True, False, 'ER-7', False, False)
    assert logged == []


def test_a_bank_movement_may_take_a_number_its_own_entries_resemble(tmp_db):
    """Die eigene Familie – die Sätze der Buchung, ihre Bankbewegung, deren übrige Sätze – gehört zum selben Vorgang; der Abgleich fasst
    sie nicht an (er verknüpft nur Bankbewegungen ohne Kinder und Sätze ohne Bankbewegung)."""
    receipt = add_receipt(tmp_db, '26B001')
    bank = add_booking(tmp_db, amount=-80, booking_type='bank')
    child = add_booking(tmp_db, amount=-80, parent_booking_id=bank)
    set_number(tmp_db, child, '26B001_A')
    assert tmp_db.link_receipt_to_booking(bank, receipt) == LinkResult(True, True, '26B001', False, True)   # has_entries: die Sätze erben nichts
    assert [number_of(tmp_db, row) for row in (bank, child)] == ['26B001', '26B001_A']


def test_an_entry_may_take_the_number_its_bank_movement_carries_and_a_sibling_does_not_hinder(tmp_db):
    receipt = add_receipt(tmp_db, '26B001')
    bank = add_booking(tmp_db, amount=-80, booking_type='bank')
    first = add_booking(tmp_db, amount=-50, parent_booking_id=bank)
    second = add_booking(tmp_db, amount=-30, parent_booking_id=bank)
    set_number(tmp_db, bank, '26B001')
    set_number(tmp_db, second, '26B001_B')
    assert tmp_db.link_receipt_to_booking(first, receipt) == LinkResult(True, True, '26B001', False, False)
    assert [number_of(tmp_db, row) for row in (bank, first, second)] == ['26B001', '26B001', '26B001_B']


@pytest.mark.parametrize('carrier', ['bank', 'child'], ids=['die-fremde-Bankbewegung', 'ein-Satz-der-fremden-Bankbewegung'])
def test_the_same_number_at_a_foreign_bank_movement_or_its_entry_hinders(tmp_db, carrier):
    """Gegenstück: Die Ausnahme gilt nur für die eigene Familie – eine fremde Bankbewegung und deren Sätze sind andere Buchungen."""
    receipt = add_receipt(tmp_db, '26B001')
    foreign_bank = add_booking(tmp_db, amount=-80, booking_type='bank')
    foreign_child = add_booking(tmp_db, amount=-80, parent_booking_id=foreign_bank)
    own_bank = add_booking(tmp_db, amount=-20, booking_type='bank')
    own_child = add_booking(tmp_db, amount=-20, parent_booking_id=own_bank)
    free = add_booking(tmp_db, amount=-10)
    set_number(tmp_db, foreign_bank if carrier == 'bank' else foreign_child, '26B001')
    for booking in (own_child, own_bank, free):
        assert tmp_db.link_receipt_to_booking(booking, receipt) == LinkResult(True, False, '', True, booking == own_bank), booking
        assert number_of(tmp_db, booking) is None


@pytest.mark.parametrize('kind', ['invoice', 'contract', 'other'])
def test_a_link_of_any_other_kind_than_receipt_never_passes_the_number_on(tmp_db, logged, kind):
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    assert tmp_db.link_receipt_to_booking(booking, receipt, kind) == LinkResult(True, False, '', False, False)
    assert number_of(tmp_db, booking) is None and links(tmp_db) == [(booking, receipt, kind)] and logged == []
    other = add_booking(tmp_db)                                                     # Gegenprobe: dieselbe Nummer mit der Art „Beleg“
    assert tmp_db.link_receipt_to_booking(other, receipt, 'receipt') == LinkResult(True, True, '26B001', False, False)


@pytest.mark.parametrize('stored_kind', ['invoice', 'contract', 'other', None, 'fremd'])
def test_the_kind_of_a_link_that_already_exists_decides_not_the_one_asked_for(tmp_db, logged, stored_kind):
    """Eine Verknüpfung anderer Art (auch eine ohne oder mit unbekannter Art aus einem Altbestand) bleibt, wie sie ist – und gibt keine
    Nummer weiter, auch wenn der erneute Aufruf „receipt“ nennt."""
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    run_sql(tmp_db, 'INSERT INTO BookingDocuments (Booking_ID, Document_ID, RelationType) VALUES (?, ?, ?)', booking, receipt, stored_kind)
    assert tmp_db.link_receipt_to_booking(booking, receipt, 'receipt') == LinkResult(False, False, '', False, False)
    assert links(tmp_db) == [(booking, receipt, stored_kind)] and number_of(tmp_db, booking) is None and logged == []


def entry_on(db, amount, text):
    """Ein freier Ausgabensatz (Aufwand an Girokonto) – die Wirkung auf das Konto der Bewegung ist sein Betrag."""
    return db.insert_booking(DAY, Decimal(amount), coa_id=coa(db, 6325), counter_coa_id=coa(db, 1800), booking_type='entry', text=text)


def movement_on(db, giro, amount):
    return db.insert_booking(DAY, Decimal(amount), account_id=giro, booking_type='bank', text=f'Bewegung {amount}')


def children_of(db, bank):
    return [row[0] for row in query(db, 'SELECT ID FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', (bank,))]


def test_a_receipt_on_two_standalone_rows_does_not_make_the_matching_join_them_into_one_receipt(tmp_db):
    """Der Fall aus dem Review: Zwei Sätze (−50 und −30) desselben Tages, EIN Beleg an beiden. Trügen beide dessen Nummer, fasste der
    Abgleich sie als EINEN Beleg (−80) zusammen und hängte sie an die Bewegung über −80, statt −50 und −30 an ihre Bewegungen."""
    bank_account(tmp_db, 'Giro', 1800)
    giro = next(a[0] for a in tmp_db.fetch_accounts() if a[1] == 'Giro')
    entries = {amount: entry_on(tmp_db, amount, f'Satz {amount}') for amount in ('-50', '-30')}
    banks = {amount: movement_on(tmp_db, giro, amount) for amount in ('-80', '-50', '-30')}
    receipt = add_receipt(tmp_db, '26B010')
    for entry in entries.values():
        tmp_db.link_receipt_to_booking(entry, receipt)
    assert [number_of(tmp_db, entry) for entry in entries.values()] == ['26B010', None]
    assert tmp_db.link_bank_to_entries()['linked'] == 2
    assert {amount: children_of(tmp_db, bank) for amount, bank in banks.items()} == {
        '-80': [], '-50': [entries['-50']], '-30': [entries['-30']]}


def test_a_receipt_on_a_bank_movement_does_not_let_stage_4_pick_the_entry_of_a_similar_number(tmp_db):
    """Stufe 4 vergleicht die Nummer der Bankbewegung mit denen der Sätze als Teilzeichenkette: Hätte die Bewegung „26B001“ übernommen,
    obwohl ein Satz „26B0010“ (anderer Beleg) trägt, hängte sie an genau diesen Satz. Jetzt bleibt ihre Nummer leer."""
    bank_account(tmp_db, 'Giro', 1800)
    giro = next(a[0] for a in tmp_db.fetch_accounts() if a[1] == 'Giro')
    other = entry_on(tmp_db, '-119', 'Satz A')
    entry_on(tmp_db, '-119', 'Satz B')
    set_number(tmp_db, other, '26B0010')
    bank = movement_on(tmp_db, giro, '-119')
    assert tmp_db.link_receipt_to_booking(bank, add_receipt(tmp_db, '26B001')) == LinkResult(True, False, '', True, False)
    assert tmp_db.link_bank_to_entries()['linked'] == 0 and children_of(tmp_db, bank) == []


@pytest.mark.parametrize('booking_known, receipt_known, message', [
    pytest.param(False, True, BOOKING_GONE, id='Buchung-unbekannt'),
    pytest.param(True, False, RECEIPT_GONE, id='Beleg-unbekannt'),
    pytest.param(False, False, BOOKING_GONE, id='beides-unbekannt'),
])
def test_an_unknown_booking_or_receipt_is_refused_with_a_sentence_and_nothing_is_written(tmp_db, logged, booking_known,
                                                                                           receipt_known, message):
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    before = state(tmp_db, 'Bookings', 'BookingDocuments', 'Documents')
    with pytest.raises(LinkRefused) as refused:
        tmp_db.link_receipt_to_booking(booking if booking_known else 99999, receipt if receipt_known else 99999)
    assert str(refused.value) == message and isinstance(refused.value, ValueError)
    assert state(tmp_db, 'Bookings', 'BookingDocuments', 'Documents') == before and logged == []
    assert_not_locked(tmp_db)


def test_an_error_inside_the_transaction_leaves_no_link_no_number_and_no_log(tmp_db, logged, monkeypatch):
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    before = state(tmp_db, 'Bookings', 'BookingDocuments', 'Documents')

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(Database, 'update_booking_fields', boom)
    with pytest.raises(RuntimeError, match='geheimer'):
        tmp_db.link_receipt_to_booking(booking, receipt)
    assert state(tmp_db, 'Bookings', 'BookingDocuments', 'Documents') == before    # auch die eingefügte Zeile ist zurückgenommen
    assert links(tmp_db) == [] and number_of(tmp_db, booking) is None and logged == []
    assert_not_locked(tmp_db)


# ── Lösen, Zähler, Löschen ───────────────────────────────────────────────────
def test_unlinking_removes_exactly_that_link_and_the_number_stays(tmp_db):
    receipt = add_receipt(tmp_db, '26B001')
    other = add_receipt(tmp_db, '26B002')
    booking = add_booking(tmp_db)
    tmp_db.link_receipt_to_booking(booking, receipt)
    tmp_db.link_receipt_to_booking(booking, other)
    assert tmp_db.unlink_receipt_from_booking(booking, receipt) is True
    assert links(tmp_db) == [(booking, other, 'receipt')]                           # die zweite Verknüpfung bleibt
    assert number_of(tmp_db, booking) == '26B001'                                   # die Nummer bleibt, auch gleich lautend
    assert tmp_db.unlink_receipt_from_booking(booking, receipt) is False             # zweites Lösen: gab es nicht mehr
    assert tmp_db.unlink_receipt_from_booking(99999, 99999) is False
    assert links(tmp_db) == [(booking, other, 'receipt')]
    assert_not_locked(tmp_db)


@pytest.mark.parametrize('linked, counter_after', [
    pytest.param(True, 1, id='verknuepft-und-geloest-die-Buchung-traegt-die-Nummer-weiter'),
    pytest.param(False, 0, id='Gegenprobe-ohne-Verknuepfung-geht-der-Zaehler-zurueck'),
])
def test_the_counter_is_not_given_back_while_a_booking_still_carries_the_number(tmp_db, linked, counter_after):
    def counter():
        return raw(tmp_db, "SELECT CurrentNumber FROM NumberRanges WHERE Type = 'receipt_company'")[0][0]

    tmp_db.insert_number_range('receipt_company', 2026, 'B', current_number=0)
    receipt, number = tmp_db.insert_receipt_entry(None, DAY, 'a.pdf', '')
    assert number == '26B001' and counter() == 1
    booking = add_booking(tmp_db)
    if linked:
        assert tmp_db.link_receipt_to_booking(booking, receipt).number_taken is True
        assert counter() == 1                                                         # das Verknüpfen verbraucht keine Nummer
        assert tmp_db.unlink_receipt_from_booking(booking, receipt) is True
    tmp_db.delete_receipt(receipt)
    assert counter() == counter_after
    assert number_of(tmp_db, booking) == ('26B001' if linked else None)


def test_a_receipt_with_a_link_cannot_be_deleted_until_it_is_unlinked(tmp_db):
    receipt = add_receipt(tmp_db, '26B001')
    booking = add_booking(tmp_db)
    tmp_db.link_receipt_to_booking(booking, receipt)
    with pytest.raises(DeleteRefused, match=r'1 Buchung\.$'):
        tmp_db.delete_receipt(receipt)
    assert len(tmp_db.fetch_receipts()) == 1 and links(tmp_db) == [(booking, receipt, 'receipt')]
    tmp_db.unlink_receipt_from_booking(booking, receipt)
    tmp_db.delete_receipt(receipt)
    assert tmp_db.fetch_receipts() == [] and number_of(tmp_db, booking) == '26B001'


def test_the_reports_stay_the_same_when_bookings_take_a_number(db_with_coa):
    """EÜR, Dashboard, Split-Summe und AfA lesen die Beleg-Nr. nicht: Eine nachträglich gesetzte Nummer ändert sie nicht."""
    db = db_with_coa
    account = db.fetch_accounts()[0][0]
    bank = db.insert_booking(DAY, Decimal('-119.00'), account_id=account, recipient_client='Muster GmbH', booking_type='bank')
    child = db.insert_booking(DAY, Decimal('-119.00'), coa_id=db.get_coa_id_by_account_number(6815), tax_rate=0.19,
                              tax_amount=Decimal('-19.00'), booking_type='entry', parent_booking_id=bank)
    assert number_of(db, bank) is None and number_of(db, child) is None

    def reports():
        return db.get_euer_data('2026-01-01', '2026-12-31'), db.get_dashboard('2026-01-01', '2026-12-31')

    before = reports()
    assert before[0] and before[1][0]                                                 # es gibt etwas zu vergleichen
    assert db.link_receipt_to_booking(bank, add_receipt(db, '26B101')).number_taken is True
    assert db.link_receipt_to_booking(child, add_receipt(db, '26B102')).number_taken is True
    assert (number_of(db, bank), number_of(db, child)) == ('26B101', '26B102')
    assert reports() == before


# ── Suchen (Datenbankschicht) ────────────────────────────────────────────────
def find_receipts(db, q, limit=20):
    text, date_prefix, _amount = receipt_links.search_terms(q)
    return [row[0] for row in db.search_receipts(text, date_prefix, limit)]


def find_bookings(db, q, limit=20):
    text, date_prefix, amount = receipt_links.search_terms(q)
    return [row[0] for row in db.search_bookings(text, date_prefix, amount, limit)]


def test_the_search_ignores_the_spelling_even_for_umlauts_and_sharp_s(tmp_db):
    office = add_receipt(tmp_db, 'R-1', info='Büromaterial')
    street = add_receipt(tmp_db, 'R-2', filename='Hauptstraße 5.pdf')
    plain = add_receipt(tmp_db, 'R-3', info='Papier')
    assert find_receipts(tmp_db, 'BÜRO') == [office] and find_receipts(tmp_db, 'büro') == [office]
    assert find_receipts(tmp_db, 'STRASSE') == [street] and find_receipts(tmp_db, 'STRAßE') == [street]
    assert find_receipts(tmp_db, 'r-3') == [plain]                                    # Nummer, ohne Rücksicht auf die Schreibweise
    assert find_receipts(tmp_db, 'Nichts') == []
    owner = add_booking(tmp_db, recipient_client='Müller & Söhne', text='Straße', document_number='ER-7')
    assert find_bookings(tmp_db, 'MÜLLER') == [owner] and find_bookings(tmp_db, 'strasse') == [owner]
    assert find_bookings(tmp_db, 'er-7') == [owner]


def test_percent_and_underscore_are_ordinary_characters_in_the_search(tmp_db):
    percent = add_receipt(tmp_db, 'R-1', info='100% sicher')
    underscore = add_receipt(tmp_db, 'R-2', info='a_b')
    add_receipt(tmp_db, 'R-3', info='axb')
    assert find_receipts(tmp_db, '%') == [percent]                                    # LIKE '%%' fände alles
    assert find_receipts(tmp_db, '_') == [underscore]                                 # LIKE '%_%' ebenso
    assert find_receipts(tmp_db, 'a_b') == [underscore]                               # LIKE 'a_b' fände auch „axb“
    booking = add_booking(tmp_db, text='Rabatt 5% / a_b')
    add_booking(tmp_db, text='Rabatt 5 axb')
    assert find_bookings(tmp_db, '5%') == [booking] and find_bookings(tmp_db, 'a_b') == [booking]


def test_a_date_or_the_start_of_one_finds_the_receipts_and_bookings_of_that_time(tmp_db):
    march_late = add_receipt(tmp_db, 'R-1', date='2026-03-28')
    march = add_receipt(tmp_db, 'R-2', date='2026-03-04')
    april = add_receipt(tmp_db, 'R-3', date='2026-04-01')
    last_year = add_receipt(tmp_db, 'R-4', date='2025-03-04')
    assert find_receipts(tmp_db, '2026-03') == [march_late, march]                    # neueste zuerst
    assert find_receipts(tmp_db, '2026') == [april, march_late, march]
    assert find_receipts(tmp_db, '2026-03-04') == [march] and find_receipts(tmp_db, '2025') == [last_year]
    assert find_receipts(tmp_db, '2026-3') == [] and find_receipts(tmp_db, '20260304') == []    # kein Datum: nur ein Text
    booking = add_booking(tmp_db, date_booking='2026-05-17')
    other = add_booking(tmp_db, date_booking='2026-06-01')
    assert find_bookings(tmp_db, '2026-05') == [booking] and find_bookings(tmp_db, '2026-06-01') == [other]


@pytest.mark.parametrize('query', ['12,50', '-12.50', '12.5', '+12,5', ' 12,50 '])
def test_an_amount_finds_the_bookings_with_that_amount_whatever_the_sign(tmp_db, query):
    paid = add_booking(tmp_db, amount=Decimal('-12.50'))
    received = add_booking(tmp_db, amount=Decimal('12.50'))
    add_booking(tmp_db, amount=Decimal('12.51'))
    add_booking(tmp_db, amount=Decimal('1250'))
    assert sorted(find_bookings(tmp_db, query)) == [paid, received]


def test_the_search_term_is_read_as_text_date_and_amount_where_it_can_be():
    assert receipt_links.search_terms('Büro') == ('büro', None, None)
    assert receipt_links.search_terms('2026-03') == ('2026-03', '2026-03', None)      # „2026-03“ ist kein Betrag
    assert receipt_links.search_terms('12,50') == ('12,50', None, 125000)             # Minor Units: 1 € = 10000
    assert receipt_links.search_terms('-12.50')[2] == 125000
    assert receipt_links.search_terms('2026')[1:] == ('2026', 20260000)               # ein Jahr ist auch ein Betrag
    assert receipt_links.search_terms('abc1') == ('abc1', None, None)                 # mit Ziffer, aber kein Betrag
    assert receipt_links.search_terms('') == ('', None, None)
    assert receipt_links.search_terms('١٢')[2] is None                                # Ziffern anderer Schriften sind kein Betrag


def test_results_come_newest_first_and_the_limit_applies(tmp_db):
    ids = [add_receipt(tmp_db, f'R-{day}', date=f'2026-03-{day:02d}', info='Sammlung') for day in range(1, 11)]
    assert find_receipts(tmp_db, 'Sammlung', 3) == [ids[9], ids[8], ids[7]]
    assert find_receipts(tmp_db, '', 2) == [ids[9], ids[8]]                           # ohne Begriff: die jüngsten
    first = add_booking(tmp_db, date_booking='2026-03-04', text='gleicher Tag')
    second = add_booking(tmp_db, date_booking='2026-03-04', text='gleicher Tag')
    assert find_bookings(tmp_db, 'gleicher') == [second, first]                       # am selben Tag: die neuere ID zuerst


def test_the_database_layer_caps_the_limit_itself(tmp_db):
    """Auch ein direkter Aufruf mit einer großen Zahl liefert höchstens ``SEARCH_LIMIT_MAX``; ein Wert unter 1 ist keine Grenze."""
    assert SEARCH_LIMIT_MAX == 50
    for number in range(60):
        add_receipt(tmp_db, f'R-{number:02d}', date=f'2026-02-{number % 28 + 1:02d}')
        add_booking(tmp_db, text='Posten')
    assert len(tmp_db.search_receipts('', None, 10 ** 6)) == SEARCH_LIMIT_MAX
    assert len(tmp_db.search_bookings('', None, None, 10 ** 6)) == SEARCH_LIMIT_MAX
    assert len(tmp_db.search_bookings('posten', None, None, 10 ** 6)) == SEARCH_LIMIT_MAX
    for below in (0, -1, -10 ** 6):                                                    # SQLite läse LIMIT -1 als „ohne Grenze“
        assert len(tmp_db.search_receipts('', None, below)) == 1 and len(tmp_db.search_bookings('', None, None, below)) == 1
    assert len(tmp_db.search_receipts('r-', None, 7)) == 7


def test_the_searches_write_nothing(tmp_db):
    add_receipt(tmp_db, 'R-1', info='Büro')
    add_booking(tmp_db, text='Büro')
    before = installation.dump(tmp_db.db_name)
    for q in ('Büro', '2026', '', '12,50', '%'):
        find_receipts(tmp_db, q)
        find_bookings(tmp_db, q)
    assert installation.dump(tmp_db.db_name) == before


def test_the_payload_of_a_receipt_never_carries_the_path_and_names_the_state_of_the_file():
    row = (7, 'R-1', DAY, 'beleg.pdf', '/srv/alt/geheim/x.pdf', 'Info')
    assert receipt_links.receipt_payload(row) == {'id': 7, 'number': 'R-1', 'date': DAY, 'filename': 'beleg.pdf', 'info': 'Info',
                                                  'file': 'outside', 'inline': False}
    assert receipt_links.receipt_payload((7, None, None, None, '', None)) == {
        'id': 7, 'number': '', 'date': '', 'filename': '', 'info': '', 'file': 'none', 'inline': False}
    linked = receipt_links.receipt_payload(row + ('contract',))
    assert (linked['relation'], linked['relationLabel']) == ('contract', 'Vertrag') and 'geheim' not in json.dumps(linked)
    odd = receipt_links.receipt_payload(row + ('fremd',))
    assert (odd['relation'], odd['relationLabel']) == ('fremd', 'fremd')               # ein unbekannter Wert aus dem Bestand: als Text
    assert receipt_links.receipt_payload(row + (None,))['relationLabel'] == '–'


def test_the_payload_of_a_booking_formats_the_amount_cuts_long_texts_and_names_the_kind():
    bank = (1, DAY, -125000, 'EUR', 'Muster GmbH', 'Abschlag', 'ER-7', 'bank', None)
    child = (2, DAY, 10000, None, None, 'x' * 300, None, 'entry', 1)
    entry = (3, DAY, 'kein Betrag', 'USD', 'A', 'B', 'C', 'entry', None, 'invoice')
    assert receipt_links.booking_payload(bank) == {
        'id': 1, 'date': DAY, 'amount': '-12,50', 'currency': 'EUR', 'recipient': 'Muster GmbH', 'text': 'Abschlag',
        'documentNumber': 'ER-7', 'kind': 'bank', 'kindLabel': 'Bankbewegung'}
    payload = receipt_links.booking_payload(child)
    assert (payload['kind'], payload['kindLabel'], payload['amount'], payload['currency']) == (
        'child', 'Satz einer Bankbewegung', '1,00', 'EUR')
    assert payload['text'] == 'x' * (receipt_links.TEXT_PREVIEW - 1) + '…' and payload['recipient'] == ''
    payload = receipt_links.booking_payload(entry)
    assert (payload['kind'], payload['amount'], payload['currency'], payload['relationLabel']) == ('entry', '', 'USD', 'Rechnung')
    assert receipt_links.booking_payload((4, DAY, True, 'EUR', '', '', '', 'entry', None))['amount'] == ''    # kein bool als Betrag


# ── _json_positive_id ────────────────────────────────────────────────────────
@pytest.mark.parametrize('value', [1, '1', 2 ** 63 - 1, str(2 ** 63 - 1), 42, '42'])
def test_a_strict_id_accepts_a_positive_integer_or_its_canonical_ascii_text(value):
    assert handlers._json_positive_id({'bookingId': value}, 'bookingId') == int(value)


@pytest.mark.parametrize('value', [None, '', 0, '0', -1, '-1', 1.5, 1.0, '1.0', '01', ' 1', '1 ', '+1', 'abc', True, False, [1], {}, {'a': 1},
                                   2 ** 63, str(2 ** 63), '9' * 30, '٣', '１２', float('nan'), float('inf')])
def test_a_strict_id_refuses_everything_else(value):
    with pytest.raises(handlers.InvalidRequest):
        handlers._json_positive_id({'bookingId': value}, 'bookingId')
    with pytest.raises(handlers.InvalidRequest):
        handlers._json_positive_id({}, 'bookingId')                                       # fehlt


# ── Routen: Verknüpfen und Lösen ─────────────────────────────────────────────
def link(port, login, booking, receipt, **extra):
    return post_json(port, '/bookings/link-document', {'bookingId': booking, 'documentId': receipt, **extra}, login.csrf)


def unlink(port, login, booking, receipt):
    return post_json(port, '/bookings/unlink-document', {'bookingId': booking, 'documentId': receipt}, login.csrf)


def test_linking_over_http_answers_with_the_result_and_both_lists(server_port, auto_login):
    db = auto_login.db
    receipt = add_receipt(db, '26B001', filename='beleg.pdf', info='Strom')
    booking = add_booking(db, amount=Decimal('-12.50'), recipient_client='Stadtwerke Muster', text='Abschlag')
    resp = link(server_port, auto_login, booking, receipt)
    assert resp.status == 200 and resp.headers['Content-Type'].startswith('application/json')
    assert json.loads(resp.text) == {
        'success': True, 'created': True, 'numberTaken': True, 'documentNumber': '26B001', 'numberInUse': False, 'hasEntries': False,
        'stale': False,
        'documents': [{'id': receipt, 'number': '26B001', 'date': DAY, 'filename': 'beleg.pdf', 'info': 'Strom', 'file': 'none',
                       'inline': False, 'relation': 'receipt', 'relationLabel': 'Beleg'}],
        'bookings': [{'id': booking, 'date': DAY, 'amount': '-12,50', 'currency': 'EUR', 'recipient': 'Stadtwerke Muster',
                      'text': 'Abschlag', 'documentNumber': '26B001', 'kind': 'entry', 'kindLabel': 'Buchungssatz',
                      'relation': 'receipt', 'relationLabel': 'Beleg'}]}
    again = json.loads(link(server_port, auto_login, booking, receipt).text)           # zweimal: nichts Neues
    assert (again['success'], again['created'], again['numberTaken'], again['documentNumber']) == (True, False, False, '26B001')
    assert links(db) == [(booking, receipt, 'receipt')]


@pytest.mark.parametrize('route', LINK_ROUTES)
def test_unusable_ids_and_bodies_are_400_and_change_nothing(server_port, auto_login, route):
    db = auto_login.db
    receipt, booking = add_receipt(db, '26B001'), add_booking(db)
    db.link_booking_to_document(booking, receipt)                                       # damit auch das Lösen etwas verlieren könnte
    before = state(db, 'Bookings', 'BookingDocuments', 'Documents')
    bad_ids = [None, '', 0, -1, 1.5, 1.0, '01', ' 1', '1.0', True, [1], {}, 2 ** 63, '9' * 30, 'abc']
    for field in ('bookingId', 'documentId'):
        for bad in bad_ids:
            resp = post_json(server_port, route, {'bookingId': booking, 'documentId': receipt, field: bad}, auto_login.csrf)
            assert (resp.status, json.loads(resp.text)) == (400, INVALID_REQUEST), (field, bad)
        missing = {'bookingId': booking, 'documentId': receipt}
        del missing[field]
        assert post_json(server_port, route, missing, auto_login.csrf).status == 400, field
    for body in (b'{', b'', b'[]', b'null', b'"text"', b'42'):
        resp = post_json(server_port, route, body, auto_login.csrf)
        assert (resp.status, json.loads(resp.text)) == (400, INVALID_REQUEST), body
    assert state(db, 'Bookings', 'BookingDocuments', 'Documents') == before


def test_an_unknown_booking_or_receipt_is_a_404_with_the_sentence_and_nothing_changes(server_port, auto_login):
    db = auto_login.db
    receipt, booking = add_receipt(db, '26B001'), add_booking(db)
    before = state(db, 'Bookings', 'BookingDocuments', 'Documents')
    for args, sentence in (((99999, receipt), BOOKING_GONE), ((booking, 99999), RECEIPT_GONE), ((99999, 99999), BOOKING_GONE)):
        resp = link(server_port, auto_login, *args)
        assert (resp.status, json.loads(resp.text)) == (404, {'success': False, 'error': sentence}), args
    assert state(db, 'Bookings', 'BookingDocuments', 'Documents') == before


@pytest.mark.parametrize('sent, stored', [
    pytest.param({'relationType': 'contract'}, 'contract', id='Vertrag'),
    pytest.param({'relationType': 'invoice'}, 'invoice', id='Rechnung'),
    pytest.param({'relationType': 'evil'}, 'receipt', id='unbekannt'),
    pytest.param({'relationType': 5}, 'receipt', id='Zahl'),
    pytest.param({'relationType': None}, 'receipt', id='null'),
    pytest.param({'relationType': ['contract']}, 'receipt', id='Liste'),
    pytest.param({}, 'receipt', id='fehlt'),
])
def test_the_kind_of_the_link_comes_from_the_positive_list_or_is_a_receipt(server_port, auto_login, sent, stored):
    db = auto_login.db
    receipt, booking = add_receipt(db, '26B001'), add_booking(db)
    resp = link(server_port, auto_login, booking, receipt, **sent)
    assert resp.status == 200 and links(db) == [(booking, receipt, stored)]
    assert json.loads(resp.text)['documents'][0]['relation'] == stored


def test_an_unexpected_error_is_a_500_with_a_fixed_sentence_and_the_text_stays_in_the_log(server_port, auto_login, monkeypatch, capsys):
    db = auto_login.db
    receipt, booking = add_receipt(db, '26B001'), add_booking(db)
    db.link_receipt_to_booking(booking, receipt)

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(Database, 'link_receipt_to_booking', boom)
    resp = link(server_port, auto_login, booking, receipt)
    assert (resp.status, json.loads(resp.text)) == (500, {'success': False, 'error': receipt_links.LINK_FAILED})
    assert SECRET not in resp.text and 'sqlite3' not in resp.text and '/home' not in resp.text
    assert SECRET in capsys.readouterr().err                                              # aber im Server-Log
    monkeypatch.setattr(Database, 'unlink_receipt_from_booking', boom)
    resp = unlink(server_port, auto_login, booking, receipt)
    assert (resp.status, json.loads(resp.text)) == (500, {'success': False, 'error': receipt_links.UNLINK_FAILED})
    assert SECRET not in resp.text and SECRET in capsys.readouterr().err
    assert links(db) == [(booking, receipt, 'receipt')]                                   # nichts wurde gelöst


def test_unlinking_over_http_keeps_the_number_and_is_idempotent(server_port, auto_login):
    db = auto_login.db
    receipt, booking = add_receipt(db, '26B001'), add_booking(db)
    link(server_port, auto_login, booking, receipt)
    resp = unlink(server_port, auto_login, booking, receipt)
    assert resp.status == 200 and json.loads(resp.text) == {'success': True, 'removed': True, 'documents': [], 'bookings': [],
                                                            'stale': False}
    assert links(db) == [] and number_of(db, booking) == '26B001'
    again = json.loads(unlink(server_port, auto_login, booking, receipt).text)
    assert again['success'] is True and again['removed'] is False
    unknown = json.loads(unlink(server_port, auto_login, 99999, 99999).text)
    assert unknown == {'success': True, 'removed': False, 'documents': [], 'bookings': [], 'stale': False}


def test_the_answer_names_the_number_as_text_whatever_it_looks_like(server_port, auto_login):
    """``documentNumber`` kommt aus der Datenbank und bleibt Text; das Skript setzt ihn nur über ``textContent``/``.value``."""
    db = auto_login.db
    receipt, booking = add_receipt(db, IMG), add_booking(db)
    resp = link(server_port, auto_login, booking, receipt)
    assert resp.headers['Content-Type'].startswith('application/json')
    data = json.loads(resp.text)
    assert data['documentNumber'] == IMG and isinstance(data['documentNumber'], str)
    assert data['documents'][0]['number'] == IMG and data['bookings'][0]['documentNumber'] == IMG


def test_the_answer_says_when_the_number_was_not_taken_and_when_the_booking_is_a_bank_movement_with_entries(server_port, auto_login):
    db = auto_login.db
    first_receipt, second_receipt = add_receipt(db, '26B010'), add_receipt(db, '26B011')
    first, second = add_booking(db, amount=-50), add_booking(db, amount=-30)
    bank = add_booking(db, amount=-80, booking_type='bank')
    add_booking(db, amount=-80, parent_booking_id=bank)
    pairs = [(first, first_receipt), (second, first_receipt), (bank, first_receipt), (bank, second_receipt)]
    fields = ('created', 'numberTaken', 'documentNumber', 'numberInUse', 'hasEntries', 'stale')
    answers = [json.loads(link(server_port, auto_login, booking, receipt).text) for booking, receipt in pairs]
    got = [tuple(data[name] for name in fields) for data in answers]
    assert got == [
        (True, True, '26B010', False, False, False),        # übernommen
        (True, False, '', True, False, False),               # dieselbe Nummer trägt schon eine andere Buchung: nicht übernommen, gesagt
        (True, False, '', True, True, False),                # dito bei einer Bankbewegung mit Sätzen (hasEntries)
        (True, True, '26B011', False, True, False)]          # eine andere Nummer: übernommen – die Sätze der Bewegung erben sie nicht
    assert [number_of(db, row) for row in (first, second, bank)] == ['26B010', None, '26B011']


@pytest.mark.parametrize('route, broken', [
    pytest.param('link', 'linked_documents', id='Verknuepfen-Liste-der-Belege'),
    pytest.param('link', 'linked_bookings', id='Verknuepfen-Liste-der-Buchungen'),
    pytest.param('unlink', 'linked_documents', id='Loesen-Liste-der-Belege'),
    pytest.param('unlink', 'linked_bookings', id='Loesen-Liste-der-Buchungen'),
])
def test_unreadable_lists_after_the_commit_are_a_200_with_stale_and_the_link_stands(server_port, auto_login, monkeypatch, capsys,
                                                                                    route, broken):
    """Die Listen der Antwort werden NACH dem Commit gelesen: Scheitert das, ist die Verknüpfung (bzw. das Lösen) trotzdem gespeichert –
    eine 500 wäre falsch. Die Antwort bleibt 200 mit den Ergebnisfeldern, leeren Listen und ``stale``; der Text steht nur im Server-Log."""
    db = auto_login.db
    receipt, booking = add_receipt(db, '26B001'), add_booking(db)
    if route == 'unlink':
        db.link_receipt_to_booking(booking, receipt)

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(receipt_links, broken, boom)
    resp = (link if route == 'link' else unlink)(server_port, auto_login, booking, receipt)
    assert resp.status == 200 and resp.headers['Content-Type'].startswith('application/json')
    data = json.loads(resp.text)
    assert data['success'] is True and data['stale'] is True and data['documents'] == [] and data['bookings'] == []
    if route == 'link':
        assert (data['created'], data['numberTaken'], data['documentNumber']) == (True, True, '26B001')
        assert links(db) == [(booking, receipt, 'receipt')] and number_of(db, booking) == '26B001'
    else:
        assert data['removed'] is True and links(db) == [] and number_of(db, booking) == '26B001'
    assert SECRET not in resp.text and 'sqlite3' not in resp.text and '/home' not in resp.text
    assert SECRET in capsys.readouterr().err                                               # aber im Server-Log


# ── Routen: Suchen ───────────────────────────────────────────────────────────
def search(port, path, query, headers=None, extra=''):
    """Eine Suche, wie der Browser sie schickt: der Begriff prozentkodiert (``encodeURIComponent``)."""
    return get(port, f'{path}?q={urllib.parse.quote(query, safe="")}{extra}', headers)


def make_receipts(db, count=25):
    for day in range(1, count + 1):
        add_receipt(db, f'26B{day:03d}', date=f'2026-03-{day:02d}', filename=f'beleg-{day}.pdf', info='Sammlung')


def test_the_receipt_search_finds_number_file_name_and_info(server_port, auto_login):
    db = auto_login.db
    make_receipts(db, 5)
    odd = add_receipt(db, 'ER-7', date='2026-04-01', filename='Strom Januar.pdf', info='Büromaterial')
    for query, expected in (('ER-7', [odd]), ('er-7', [odd]), ('strom', [odd]), ('JANUAR', [odd]), ('BÜRO', [odd]),
                            ('26B003', [3]), ('beleg-4.pdf', [4])):
        resp = search(server_port, '/receipts/search', query)
        assert resp.status == 200 and resp.headers['Content-Type'].startswith('application/json'), query
        answer = json.loads(resp.text)
        assert answer['success'] is True and [r['id'] for r in answer['results']] == expected, query
    assert len(json.loads(search(server_port, '/receipts/search', 'Sammlung').text)['results']) == 5
    assert json.loads(search(server_port, '/receipts/search', 'gibt-es-nicht').text) == {'success': True, 'results': []}


def test_the_number_of_results_follows_limit_and_an_invalid_limit_means_not_given(server_port, auto_login):
    make_receipts(auto_login.db, 25)
    results = json.loads(search(server_port, '/receipts/search', 'Sammlung', extra='&limit=2').text)['results']
    assert [r['number'] for r in results] == ['26B025', '26B024']                          # neueste zuerst
    for limit in ('abc', '0', '999', '-1', '1.5', '', '51', '٣'):
        resp = search(server_port, '/receipts/search', 'Sammlung', extra=f'&limit={urllib.parse.quote(limit, safe="")}')
        assert len(json.loads(resp.text)['results']) == 20, limit                          # ungültig heißt „nicht angegeben“: der Standard
    assert len(json.loads(search(server_port, '/receipts/search', 'Sammlung').text)['results']) == 20
    assert len(json.loads(search(server_port, '/receipts/search', 'Sammlung', extra='&limit=50').text)['results']) == 25
    assert len(json.loads(search(server_port, '/receipts/search', 'Sammlung', extra='&limit=1').text)['results']) == 1


@pytest.mark.parametrize('route', ['/receipts/search', '/transactions/search'])
def test_a_too_long_search_term_is_a_400_with_the_sentence(server_port, auto_login, route):
    limit = receipt_links.SEARCH_MAX_LENGTH
    assert search(server_port, route, 'a' * limit).status == 200
    resp = search(server_port, route, 'a' * (limit + 1))
    assert (resp.status, json.loads(resp.text)) == (400, {'success': False, 'error': receipt_links.SEARCH_TOO_LONG})
    assert search(server_port, route, ' ' * 300 + 'a').status == 200                        # Leerraum am Rand zählt nicht mit


@pytest.mark.parametrize('route', ['/receipts/search', '/transactions/search'])
def test_a_search_from_a_foreign_page_is_a_403_and_the_own_page_may_ask(server_port, auto_login, route):
    add_receipt(auto_login.db, '26B001')
    for headers in ({'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}, {'Origin': FOREIGN_ORIGIN}):
        resp = search(server_port, route, 'a', headers)
        assert (resp.status, json.loads(resp.text)) == (403, {'success': False, 'error': receipt_links.SEARCH_FOREIGN}), headers
        assert '26B001' not in resp.text
    for headers in ({'Sec-Fetch-Site': 'same-origin'}, {'Origin': own_origin(server_port)}, {}):
        assert search(server_port, route, 'a', headers).status == 200, headers


def test_the_receipt_search_names_the_state_of_the_file_and_never_a_path(server_port, auto_login):
    db = auto_login.db
    folder = os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'Belege', '2026')
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, '26B001_rechnung.pdf'), 'wb') as handle:
        handle.write(PDF)
    with open(os.path.join(folder, '26B002_notiz.txt'), 'wb') as handle:
        handle.write(b'Notiz')
    ok = add_receipt(db, '26B001', path='Belege/2026/26B001_rechnung.pdf')
    text = add_receipt(db, '26B002', path='Belege/2026/26B002_notiz.txt')
    paper = add_receipt(db, '26B003', path='')
    outside = add_receipt(db, '26B004', path='/srv/alt/geheim/x.pdf')
    gone = add_receipt(db, '26B005', path='Belege/2026/weg.pdf')
    before = (installation.dump(db.db_name), installation.tree(os.path.dirname(db.db_name)))
    resp = search(server_port, '/receipts/search', '26B')
    assert resp.status == 200
    found = {r['id']: (r['file'], r['inline']) for r in json.loads(resp.text)['results']}
    assert found == {ok: ('ok', True), text: ('ok', False), paper: ('none', False), outside: ('outside', False),
                     gone: ('outside', False)}
    for secret in ('Belege/', '/srv', 'geheim', 'rechnung.pdf', 'notiz.txt', userctx.DATA_ROOT, 'Path'):
        assert secret not in resp.text, secret                                              # nie ein Pfad, auch nicht der Dateiname auf der Platte
    assert all(set(r) == {'id', 'number', 'date', 'filename', 'info', 'file', 'inline'} for r in json.loads(resp.text)['results'])
    assert (installation.dump(db.db_name), installation.tree(os.path.dirname(db.db_name))) == before      # rein lesend


def test_the_booking_search_finds_recipient_text_number_amount_and_date_and_names_the_kind(server_port, auto_login):
    db = auto_login.db
    bank = add_booking(db, date_booking='2026-03-04', amount=Decimal('-12.50'), recipient_client='Büro Muster',
                       text='Papier', document_number='ER-7', booking_type='bank')
    child = add_booking(db, date_booking='2026-03-04', amount=Decimal('-12.50'), text='Satz A', booking_type='entry',
                        parent_booking_id=bank)
    entry = add_booking(db, date_booking='2026-05-17', amount=Decimal('99.00'), recipient_client='Andere', text='frei')
    before = (installation.dump(db.db_name), installation.tree(os.path.dirname(db.db_name)))

    def found(query):
        resp = search(server_port, '/transactions/search', query)
        assert resp.status == 200 and resp.headers['Content-Type'].startswith('application/json'), query
        return [r['id'] for r in json.loads(resp.text)['results']]

    assert found('BÜRO') == [bank] and found('papier') == [bank] and found('er-7') == [bank]
    assert found('12,50') == [child, bank]                                                  # derselbe Tag: die neuere ID zuerst
    assert found('-12.5') == [child, bank] and found('2026-05') == [entry] and found('99') == [entry]
    assert found('Satz') == [child] and found('gibt-es-nicht') == []
    by_id = {r['id']: r for r in json.loads(search(server_port, '/transactions/search', '').text)['results']}
    assert {i: (r['kind'], r['kindLabel']) for i, r in by_id.items()} == {
        bank: ('bank', 'Bankbewegung'), child: ('child', 'Satz einer Bankbewegung'), entry: ('entry', 'Buchungssatz')}
    assert by_id[bank]['amount'] == '-12,50' and by_id[bank]['documentNumber'] == 'ER-7' and by_id[bank]['recipient'] == 'Büro Muster'
    assert (installation.dump(db.db_name), installation.tree(os.path.dirname(db.db_name))) == before      # rein lesend


@pytest.mark.parametrize('route', ['/receipts/search', '/transactions/search'])
def test_the_searches_are_never_cached_even_when_the_address_ends_like_a_static_file(server_port, auto_login, route):
    """Die Adresse endet auf den Suchbegriff: Eine Suche nach „.png“ sähe sonst wie eine statische Datei aus (``public``, einen Tag)."""
    add_receipt(auto_login.db, '26B001', filename='scan.png')
    for query in ('.png', '.js', '.css', 'scan'):
        resp = search(server_port, route, query)
        assert resp.status == 200 and resp.headers['Cache-Control'] == 'no-store', query


def test_the_words_of_sql_in_a_search_term_are_plain_text(server_port, auto_login):
    db = auto_login.db
    add_receipt(db, "'; DROP TABLE Documents; --", info='x')
    before = state(db, 'Documents')
    for query in ("'; DROP TABLE Documents; --", '"', '\\', ':t', '?', '%', '_', '\x00'):
        assert search(server_port, '/receipts/search', query).status == 200, query
        assert search(server_port, '/transactions/search', query).status == 200, query
    assert state(db, 'Documents') == before
    assert len(json.loads(search(server_port, '/receipts/search', 'DROP TABLE').text)['results']) == 1


# ── Seiten ───────────────────────────────────────────────────────────────────
def script_data(html, name):
    """Die Daten ``var <name> = <JSON>;`` aus einem Skriptblock der Seite – so, wie JavaScript sie läse."""
    for script in Dom(html).scripts:
        marker = f'var {name} = '
        if marker in script:
            return json.JSONDecoder().raw_decode(script, script.index(marker) + len(marker))[0]
    raise AssertionError(f'kein Datenblock {name}')


def test_the_page_of_a_receipt_carries_the_linked_bookings_as_data_and_no_old_route(tmp_db):
    receipt = add_receipt(tmp_db, HOSTILE, filename=IMG, info='Info')
    booking = add_booking(tmp_db, recipient_client=HOSTILE, text=IMG, amount=Decimal('-12.50'))
    tmp_db.link_booking_to_document(booking, receipt, 'contract')
    html = PageReceiptEdit(tmp_db, receipt)
    assert HOSTILE not in html and IMG not in html                                           # roh steht nichts davon im HTML
    assert script_data(html, 'RECEIPT_LINKED') == [{
        'id': booking, 'date': DAY, 'amount': '-12,50', 'currency': 'EUR', 'recipient': HOSTILE, 'text': IMG, 'documentNumber': '',
        'kind': 'entry', 'kindLabel': 'Buchungssatz', 'relation': 'contract', 'relationLabel': 'Vertrag'}]    # in den Daten unverändert
    assert script_data(html, 'RECEIPT_LINK_ID') == receipt
    assert '/documents/' not in html and 'documents/link' not in html and 'documents/unlink' not in html
    assert Dom(html).all('img') == []
    query = Dom(html).one('input', id='receiptBookingQuery')
    assert query['type'] == 'search' and query['maxlength'] == str(receipt_links.SEARCH_MAX_LENGTH) and 'name' not in query
    assert Dom(html).one('div', id='receiptLinks') == {'id': 'receiptLinks'}
    assert 'Verknüpfte Buchungen' in html and 'Buchung suchen' in html
    assert PageReceiptEdit(tmp_db, 99999) == 'Beleg nicht gefunden.' and PageReceiptEdit(tmp_db, -1) == 'Beleg nicht gefunden.'


def test_the_page_of_a_receipt_with_a_valid_file_offers_view_and_download_and_no_path(server_port, auto_login):
    db = auto_login.db
    folder = os.path.join(userctx.DATA_ROOT, 'users', installation.ADMIN, 'Belege', '2026')
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, '26B001_rechnung.pdf'), 'wb') as handle:
        handle.write(PDF)
    ok = add_receipt(db, '26B001', path='Belege/2026/26B001_rechnung.pdf')
    paper = add_receipt(db, '26B002', path='')
    outside = add_receipt(db, '26B003', path='/srv/alt/x.pdf')
    page = get(server_port, f'/receipts/edit?id={ok}').text
    assert f'<a href="/receipts/file?id={ok}&amp;inline=1" target="_blank" rel="noopener">Ansehen</a>' in page
    assert f"onclick=\"appDownload('/receipts/file?id={ok}')\">Herunterladen</a>" in page
    assert '26B001_rechnung.pdf' in page and 'Belege/2026' not in page
    for receipt, text in ((paper, 'keine Datei'), (outside, 'Datei nicht im Belegordner')):
        page = get(server_port, f'/receipts/edit?id={receipt}').text
        # (die Wörter stehen auch im Skript der Seite: geprüft wird das Markup der Zeile)
        assert text in page and 'rel="noopener">Ansehen</a>' not in page and '>Herunterladen</a>' not in page
        assert '/srv/alt' not in page


def test_the_list_of_receipts_shows_the_number_of_linked_bookings_and_a_link_to_the_page_of_the_receipt(tmp_db):
    one = add_receipt(tmp_db, 'R-1')
    three = add_receipt(tmp_db, 'R-2')
    none = add_receipt(tmp_db, 'R-3')
    tmp_db.link_booking_to_document(add_booking(tmp_db), one)
    for _ in range(3):
        tmp_db.link_booking_to_document(add_booking(tmp_db), three)
    assert tmp_db.receipt_link_counts() == {one: 1, three: 3}
    page = PageReceipts(tmp_db)
    assert '<th>Info</th><th>Buchungen</th><th>Aktionen</th>' in page
    counts = {}
    for chunk in re.findall(r'<tr class="receipt-row"(.*?)</tr>', page, re.S):
        receipt_id = int(re.search(r'data-id="(\d+)"', chunk).group(1))
        cells = re.findall(r'<td(?: [^>]*)?>(.*?)</td>', chunk, re.S)
        assert len(cells) == 8 and cells[3] == '–' and cells[4] == '–'                      # Index 3 „Datei“, 4 „Gelesen“ (2.6.0): beide ohne Datei
        counts[receipt_id] = cells[6]
    assert counts == {one: f'<a href="/receipts/edit?id={one}" title="Verknüpfte Buchungen ansehen und zuordnen">1</a>',
                      three: f'<a href="/receipts/edit?id={three}" title="Verknüpfte Buchungen ansehen und zuordnen">3</a>',
                      none: f'<a href="/receipts/edit?id={none}" title="Verknüpfte Buchungen ansehen und zuordnen">0</a>'}
    assert Dom(page).one('a', id='editLinks') == {'id': 'editLinks', 'href': '/receipts'}
    assert 'editLinksRow' in page


def test_the_mask_of_a_booking_has_the_receipts_after_the_form_and_no_field_in_it(db_with_coa):
    db = db_with_coa
    receipt = add_receipt(db, HOSTILE, filename=IMG, info='Info')
    booking = add_booking(db, amount=Decimal('100.00'))
    db.link_booking_to_document(booking, receipt, 'other')
    html = PageTransactions(db, edit_transaction_id=booking, date_from='2026-01-01', date_to='2026-12-31')
    start = html.index('<form method="POST" action="/transactions/add">')
    end = html.index('</form>', start)
    assert 'txReceipt' not in html[start:end] and 'TX_RECEIPTS' not in html[start:end]       # nichts davon im Formular …
    assert 'txReceipts' in html[end:] and 'txReceiptQuery' in html[end:]                      # … sondern danach, im selben Rahmen
    assert script_data(html, 'TX_RECEIPTS') == [{
        'id': receipt, 'number': HOSTILE, 'date': DAY, 'filename': IMG, 'info': 'Info', 'file': 'none', 'inline': False,
        'relation': 'other', 'relationLabel': 'Sonstiges'}]
    assert script_data(html, 'TX_LINK_BOOKING_ID') == booking
    # die Liste trägt die ID ihrer Buchung: Das Skript erkennt daran, ob eine Antwort noch zur geöffneten Maske gehört (``txStill``)
    assert Dom(html).one('div', id='txReceipts') == {'id': 'txReceipts', 'data-booking': str(booking)}
    query = Dom(html).one('input', id='txReceiptQuery')
    assert query['type'] == 'search' and query['maxlength'] == str(receipt_links.SEARCH_MAX_LENGTH) and 'name' not in query
    assert HOSTILE not in html and IMG not in html and Dom(html).all('img') == []
    assert '/documents/' not in html and 'Verknüpfte Dokumente' not in html
    new = PageTransactions(db, date_from='2026-01-01', date_to='2026-12-31')
    assert 'Belege lassen sich zuordnen, sobald die Buchung gespeichert ist.' in new and 'txReceiptQuery' not in new
    assert 'TX_RECEIPTS' not in new and '/bookings/link-document' not in new


def test_the_mask_shows_the_receipts_of_a_bank_movement_too(db_with_coa):
    db = db_with_coa
    account = db.fetch_accounts()[0][0]
    bank = db.insert_booking(DAY, Decimal('-30.00'), account_id=account, booking_type='bank')
    child = db.insert_booking(DAY, Decimal('-30.00'), booking_type='entry', parent_booking_id=bank)
    first, second = add_receipt(db, '26B001'), add_receipt(db, '26B002')
    db.link_booking_to_document(bank, first)
    db.link_booking_to_document(child, second)
    for booking, receipt in ((bank, first), (child, second)):
        html = PageTransactions(db, edit_transaction_id=booking, date_from='2026-01-01', date_to='2026-12-31')
        assert [d['id'] for d in script_data(html, 'TX_RECEIPTS')] == [receipt]


def test_the_scripts_of_the_widget_set_no_markup_and_use_only_var_and_function():
    for name, script in (('LINK_WIDGET_JS', receipt_links.LINK_WIDGET_JS), ('RECEIPT_LINK_JS', RECEIPT_LINK_JS), ('TX_LINK_JS', TX_LINK_JS)):
        for forbidden in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'javascript:', 'eval(', 'Function('):
            assert forbidden not in script, (name, forbidden)
        assert not re.search(r'(?<![A-Za-z0-9_$.])(const|let)\s', script), name             # die Maske spielt das Skript bei jedem Öffnen neu ein
    for script in (RECEIPT_LINK_JS, TX_LINK_JS):
        assert '</script' not in script.lower()
    assert "'<" not in TX_LINK_JS and '"<' not in TX_LINK_JS and "'<" not in RECEIPT_LINK_JS   # kein HTML aus Zeichenketten


# ── Das Skript im Browser (Node, ohne Browser) ───────────────────────────────
NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die Textprüfungen derselben Datei decken das Wichtigste ab')

HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, scenarioFile] = process.argv;
const scenario = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));

class Node {}
class TextNode extends Node {
  constructor(text) { super(); this.tagName = '#text'; this.ownText = text; }
}
class Element extends Node {
  constructor(tag) {
    super();
    this.tagName = tag; this.className = ''; this.type = ''; this.href = ''; this.target = ''; this.rel = '';
    this.value = ''; this.children = []; this.listeners = {}; this.ownText = ''; this.dataset = {};
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
  return {tag: node.tagName, text: node.ownText, className: node.className, type: node.type, href: node.href,
          target: node.target, rel: node.rel, children: node.children.map(dump)};
}
function walk(node, visit) { visit(node); (node.children || []).forEach(child => walk(child, visit)); }

const byId = {};
const gone = new Set();   // Elemente, die es nicht mehr gibt (getElementById liefert null): die Maske wurde ausgetauscht
const form = scenario.documentNr === null ? null : {elements: {document_nr: {value: scenario.documentNr}}};
const fetches = [], msgs = [], confirms = [], downloads = [];
const responses = scenario.responses.slice();
// Ein Element der Seite, mit den data-Attributen, die ihr HTML trägt (scenario.datasets: id -> {name: Wert})
function element(id) {
  return byId[id] || (byId[id] = Object.assign(new Element('div'), {
    value: id === scenario.queryField ? scenario.queryValue : '', dataset: Object.assign({}, (scenario.datasets || {})[id] || {})}));
}
const sandbox = {
  Node,
  document: {
    getElementById: id => (gone.has(id) ? null : element(id)),
    createElement: tag => new Element(tag),
    createTextNode: text => new TextNode(text),
    querySelector: selector => (selector === 'form[action="/transactions/add"]' ? form : null),
  },
  fetch: (url, options) => {
    fetches.push({url, method: (options && options.method) || 'GET', body: (options && options.body) || null});
    if (scenario.networkError) return Promise.reject(vm.runInContext('new TypeError("offline")', sandbox));
    const next = responses.shift();
    // Was auf der Seite geschieht, BEVOR die Antwort eintrifft (der Aufruf hat seine Daten da schon festgehalten): eine andere oder
    // neue Buchung wird geöffnet – das data-Attribut ändert sich (Wert) oder das Element fehlt (null)
    Object.entries(next.dom || {}).forEach(([id, change]) => {
      if (change === null) gone.add(id); else Object.assign(element(id).dataset, change);
    });
    return Promise.resolve({ok: next.ok, status: next.status, text: () => Promise.resolve(next.body)});
  },
  appMsg: (text, type) => msgs.push([text, type]),
  appConfirm: (text, onOk) => { confirms.push(text); if (scenario.confirm) onOk(); },
  appDownload: url => { downloads.push(url); return Promise.resolve('x'); },
};
if (scenario.splitDocnr !== null) sandbox.SPLIT_BANK_DOCNR = scenario.splitDocnr;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);

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
  console.log(JSON.stringify({
    fetches, msgs, confirms, downloads,
    documentNr: form ? form.elements.document_nr.value : null,
    splitDocnr: sandbox.SPLIT_BANK_DOCNR === undefined ? null : sandbox.SPLIT_BANK_DOCNR,
    tree: Object.fromEntries(Object.entries(byId).map(([id, element]) => [id, dump(element)])),
  }));
})();
"""


def run_page_script(tmp_path, script, **scenario):
    """Das Skript einer Seite in Node ausführen, mit einer kleinen DOM-Attrappe (Elemente mit ``dataset``, Text, ``fetch`` mit festen
    Antworten, ``appMsg``, ``appConfirm``, ``appDownload``); ``innerHTML``, ``outerHTML`` und ``insertAdjacentHTML`` werfen. Liefert den
    Zustand danach: die Bäume der Elemente mit ``id``, die Anfragen, die Meldungen und die Felder der Maske.

    ``datasets`` nennt die ``data-``Attribute der Elemente (so, wie die Seite sie schreibt); eine Antwort kann mit ``dom`` etwas auf der
    Seite ändern, bevor sie eintrifft (``answer(…, dom={'txReceipts': {'booking': '99'}})``; ``None`` statt der Werte: das Element
    fehlt)."""
    scenario = {'call': None, 'clicks': [], 'responses': [], 'queryField': None, 'queryValue': '', 'documentNr': None,
                'splitDocnr': None, 'confirm': True, 'networkError': False, 'datasets': {}, **scenario}
    harness, source, data = (tmp_path / name for name in ('harness.js', 'page.js', 'scenario.json'))
    harness.write_text(HARNESS, encoding='utf-8')
    source.write_text(script, encoding='utf-8')
    data.write_text(json.dumps(scenario), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(source), str(data)], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def walk(node):
    yield node
    for child in node.get('children', []):
        yield from walk(child)


def rows_of(tree):
    """Die Datenzeilen (ohne Kopfzeile) einer Tabelle im Baum: je Zeile die Zellen."""
    tables = [node for node in walk(tree) if node['tag'] == 'table']
    assert len(tables) == 1, 'keine einzelne Tabelle'
    return [row['children'] for row in tables[0]['children'] if row['tag'] == 'tr' and row['children'][0]['tag'] == 'td']


def answer(status, payload, dom=None):
    return {'ok': 200 <= status < 300, 'status': status, 'body': payload if isinstance(payload, str) else json.dumps(payload),
            'dom': dom or {}}


def script_of(html, marker):
    (script,) = [text for text in Dom(html).scripts if marker in text]
    return script


@pytest.fixture
def mask(tmp_db):
    """Eine Buchung mit einem verknüpften Beleg (feindliche Werte) und einem zweiten, noch nicht verknüpften; dazu die Maske."""
    linked = add_receipt(tmp_db, HOSTILE, filename=IMG, info='"><script>alert(1)</script>')
    free = add_receipt(tmp_db, '26B002', filename='<b>frei</b>.pdf', info="O'Brien & Söhne")
    booking = add_booking(tmp_db, recipient_client='Muster GmbH')
    tmp_db.link_booking_to_document(booking, linked)
    html = PageTransactions(tmp_db, edit_transaction_id=booking, date_from='2026-01-01', date_to='2026-12-31')
    box = Dom(html).one('div', id='txReceipts')                                         # wie die Seite es schreibt: data-booking
    return type('Mask', (), dict(db=tmp_db, linked=linked, free=free, booking=booking, script=script_of(html, 'txReceiptsRender'),
                                 datasets={'txReceipts': {'booking': box['data-booking']}}))


def run_mask(tmp_path, mask, **scenario):
    """Das Skript der Maske in Node – mit dem ``data-booking`` von ``#txReceipts``, das die Seite der Buchung trägt."""
    return run_page_script(tmp_path, script=mask.script, datasets=mask.datasets, **scenario)


def search_answer(db, q=''):
    status, body = handlers.handle_receipt_search(db, {'q': [q]})
    return answer(status, body)


@NO_NODE
def test_hostile_values_become_text_never_markup_in_the_receipts_of_the_mask(tmp_path, mask):
    out = run_mask(tmp_path, mask, call='txReceiptSearch()', queryField='txReceiptQuery', queryValue='x',
                   responses=[search_answer(mask.db)])
    tags = {node['tag'] for root in out['tree'].values() for node in walk(root)}
    assert tags <= {'div', 'table', 'tr', 'th', 'td', 'p', 'button', 'span', 'a', '#text'}, tags     # kein img, script, b …
    # die verknüpften Belege (beim Laden aufgebaut): Zellen mit den Werten als Text, dazu Beleg und Lösen
    (cells,) = rows_of(out['tree']['txReceipts'])
    assert [cell['text'] for cell in cells[:4]] == [HOSTILE, DAY, IMG, 'Beleg']
    actions = cells[4]['children'][0]
    assert [(node['tag'], node['text']) for node in actions['children'] if node['tag'] != '#text'] == [('a', 'Beleg'), ('button', 'Lösen')]
    link = next(node for node in actions['children'] if node['tag'] == 'a')
    assert (link['href'], link['target'], link['rel']) == (f'/receipts/edit?id={mask.linked}', '_blank', 'noopener')
    # die Treffer: Zelle für Zelle Text; der schon verknüpfte hat „zugeordnet“ statt des Knopfs, der andere den Knopf „Zuordnen“
    found = rows_of(out['tree']['txReceiptResults'])
    assert [[cell['text'] for cell in row[:4]] for row in found] == [
        ['26B002', DAY, '<b>frei</b>.pdf', "O'Brien & Söhne"], [HOSTILE, DAY, IMG, '"><script>alert(1)</script>']]
    free_action, linked_action = found[0][4]['children'][0], found[1][4]['children'][0]
    assert (free_action['tag'], free_action['text'], free_action['className']) == ('button', 'Zuordnen', 'coloredButton btn-sm bg-green')
    assert (linked_action['tag'], linked_action['text'], linked_action['className']) == ('span', 'zugeordnet', 'muted')
    assert out['fetches'] == [{'url': '/receipts/search?q=x', 'method': 'GET', 'body': None}] and out['msgs'] == []


@NO_NODE
@pytest.mark.parametrize('field, split, expected_field, expected_split', [
    pytest.param('', '', '26B002', '26B002', id='leeres-Feld-und-leere-Basis-ziehen-mit'),
    pytest.param('   ', '', '26B002', '26B002', id='nur-Leerraum-gilt-als-leer'),
    pytest.param('ER-7', 'ER-7', 'ER-7', 'ER-7', id='eingetipptes-Feld-bleibt'),
    pytest.param('', 'ER-7', '26B002', 'ER-7', id='Basis-der-Teilbuchungen-bleibt'),
    pytest.param(None, None, None, None, id='ohne-Feld-und-ohne-Basis-kein-Fehler'),
])
def test_the_number_of_the_receipt_fills_only_an_empty_field_of_the_mask(tmp_path, mask, field, split, expected_field, expected_split):
    hits = search_answer(mask.db, '26B002')
    status, body = handlers.handle_booking_link_document(
        mask.db, json.dumps({'bookingId': mask.booking, 'documentId': mask.free}).encode())       # so antwortet der Server
    assert status == 200 and json.loads(body)['numberTaken'] is True and number_of(mask.db, mask.booking) == '26B002'
    # Die Seite im Browser: Suche, Klick auf „Zuordnen“, diese Antwort, dann dieselbe Suche noch einmal
    out = run_mask(
        tmp_path, mask, call='txReceiptSearch()', clicks=['Zuordnen'], queryField='txReceiptQuery', queryValue='26B002',
        documentNr=field, splitDocnr=split, responses=[hits, answer(status, body), hits])
    assert out['documentNr'] == expected_field and out['splitDocnr'] == expected_split
    assert [fetch['url'] for fetch in out['fetches']] == ['/receipts/search?q=26B002', '/bookings/link-document', '/receipts/search?q=26B002']
    post = out['fetches'][1]
    assert post['method'] == 'POST' and json.loads(post['body']) == {'bookingId': mask.booking, 'documentId': mask.free}
    assert out['msgs'] == [['Zugeordnet – die Buchung trägt jetzt die Beleg-Nr. 26B002.', 'success']]
    (cells, *rest) = rows_of(out['tree']['txReceipts'])                                 # die Liste der Belege kommt aus der Antwort
    assert rest and {row[0]['text'] for row in [cells, *rest]} == {HOSTILE, '26B002'}


@NO_NODE
def test_a_number_that_was_not_taken_leaves_the_field_alone_and_a_hostile_number_is_only_a_value(tmp_path, mask):
    taken = {'success': True, 'created': True, 'numberTaken': False, 'documentNumber': 'ER-7', 'documents': [], 'bookings': []}
    out = run_mask(tmp_path, mask, call=f'txAttach({mask.free})', documentNr='', splitDocnr='',
                   responses=[answer(200, taken), answer(200, {'success': True, 'results': []})])
    assert out['documentNr'] == '' and out['splitDocnr'] == '' and out['msgs'] == [['Zugeordnet.', 'success']]
    hostile = {**taken, 'numberTaken': True, 'documentNumber': IMG}
    out = run_mask(tmp_path, mask, call=f'txAttach({mask.free})', documentNr='', splitDocnr=None,
                   responses=[answer(200, hostile), answer(200, {'success': True, 'results': []})])
    assert out['documentNr'] == IMG                                                      # ein Wert im Feld (``.value``), nie Markup
    assert out['msgs'] == [[f'Zugeordnet – die Buchung trägt jetzt die Beleg-Nr. {IMG}.', 'success']]   # und Text für ``appMsg``
    assert not [node for root in out['tree'].values() for node in walk(root) if node['tag'] in ('img', 'script')]
    existing = {**taken, 'created': False}
    out = run_mask(tmp_path, mask, call=f'txAttach({mask.free})', responses=[answer(200, existing),
                                                                             answer(200, {'success': True, 'results': []})])
    assert out['msgs'] == [['Die Verknüpfung bestand schon.', 'info']]


@NO_NODE
def test_unlinking_in_the_mask_asks_first_and_keeps_the_number_in_the_message(tmp_path, mask):
    done = {'success': True, 'removed': True, 'documents': [], 'bookings': []}
    out = run_mask(tmp_path, mask, clicks=['Lösen'], documentNr='ER-7', responses=[answer(200, done)])
    assert out['confirms'] == ['Verknüpfung lösen? Die Beleg-Nr. der Buchung bleibt.']
    post = out['fetches'][0]
    assert post['url'] == '/bookings/unlink-document' and json.loads(post['body']) == {'bookingId': mask.booking, 'documentId': mask.linked}
    assert out['msgs'] == [['Verknüpfung gelöst – die Beleg-Nr. der Buchung bleibt.', 'success']]
    assert out['documentNr'] == 'ER-7'                                                    # das Feld der Maske bleibt, wie es ist
    assert [n['text'] for n in walk(out['tree']['txReceipts']) if n['tag'] == 'p'] == ['Keine verknüpften Belege.']
    declined = run_mask(tmp_path, mask, clicks=['Lösen'], confirm=False, responses=[])
    assert declined['fetches'] == [] and declined['msgs'] == []                           # ohne Bestätigung geht nichts hinaus
    gone = {**done, 'removed': False}
    out = run_mask(tmp_path, mask, clicks=['Lösen'], responses=[answer(200, gone)])
    assert out['msgs'] == [['Die Verknüpfung bestand nicht (mehr).', 'info']]


@NO_NODE
@pytest.mark.parametrize('response, message', [
    pytest.param(answer(404, {'success': False, 'error': BOOKING_GONE}), BOOKING_GONE, id='404-mit-Klartext'),
    pytest.param(answer(500, {'success': False, 'error': receipt_links.LINK_FAILED}), receipt_links.LINK_FAILED, id='500-mit-festem-Satz'),
    pytest.param(answer(403, '<h1>403</h1>'), 'Der Server hat die Anfrage abgelehnt (Status 403).', id='kein-JSON'),
    pytest.param(answer(200, 'null'), 'Der Server hat die Anfrage abgelehnt (Status 200).', id='JSON-null'),
    pytest.param(answer(200, {'success': False}), 'Der Server hat die Anfrage abgelehnt (Status 200).', id='success-false-ohne-Text'),
    pytest.param(answer(503, {'success': False, 'ok': False, 'error': 'Bitte später.', 'errors': ['Bitte später.']}), 'Bitte später.',
                 id='503-waehrend-der-Wiederherstellung'),
])
def test_a_refusal_of_the_server_is_shown_as_an_error_and_changes_nothing_on_the_page(tmp_path, mask, response, message):
    out = run_mask(tmp_path, mask, call=f'txAttach({mask.free})', documentNr='', splitDocnr='', responses=[response])
    assert out['msgs'] == [[message, 'error']] and out['documentNr'] == '' and out['splitDocnr'] == ''
    (cells,) = rows_of(out['tree']['txReceipts'])                                         # die Liste ist, wie sie war
    assert cells[0]['text'] == HOSTILE and len(out['fetches']) == 1


@NO_NODE
def test_a_lost_connection_is_shown_in_words(tmp_path, mask):
    out = run_mask(tmp_path, mask, call=f'txAttach({mask.free})', networkError=True)
    assert out['msgs'] == [['Die Verbindung zum Server ist fehlgeschlagen.', 'error']]


NOT_TAKEN = 'Die Beleg-Nr. wurde nicht übernommen: Eine andere Buchung trägt bereits diese oder eine ähnliche Nummer.'


@NO_NODE
def test_the_message_after_linking_says_what_happened_to_the_number(tmp_path):
    """``linkDoneText`` (``LINK_WIDGET_JS``): übernommen (die Buchung; eine Bankbewegung mit Sätzen sagt, dass diese ihre Nummern
    behalten), nicht übernommen, weil eine andere Buchung dieselbe oder eine ähnliche Nummer trägt, oder nichts von beidem – jeweils auch,
    wenn es die Verknüpfung schon gab."""
    cases = [
        {'created': True, 'numberTaken': True, 'hasEntries': True, 'documentNumber': '26B001'},
        {'created': True, 'numberTaken': True, 'hasEntries': False, 'documentNumber': '26B001'},
        {'created': True, 'numberTaken': False, 'numberInUse': True},
        {'created': True},
        {'created': False, 'numberTaken': True, 'hasEntries': True, 'documentNumber': '26B001'},
        {'created': False, 'numberTaken': True, 'documentNumber': '26B001'},
        {'created': False, 'numberInUse': True},
        {'created': False},
    ]
    call = json.dumps(cases) + '.forEach(function (data) { appMsg(linkDoneText(data), "x"); });'
    out = run_page_script(tmp_path, receipt_links.LINK_WIDGET_JS, call=call)
    assert [text for text, _kind in out['msgs']] == [
        'Zugeordnet – die Bankbewegung trägt jetzt die Beleg-Nr. 26B001; ihre Buchungssätze behalten ihre eigenen Nummern.',
        'Zugeordnet – die Buchung trägt jetzt die Beleg-Nr. 26B001.',
        f'Zugeordnet. {NOT_TAKEN}',
        'Zugeordnet.',
        'Die Verknüpfung bestand schon – die Bankbewegung trägt jetzt die Beleg-Nr. 26B001; '
        'ihre Buchungssätze behalten ihre eigenen Nummern.',
        'Die Verknüpfung bestand schon – die Buchung trägt jetzt die Beleg-Nr. 26B001.',
        f'Die Verknüpfung bestand schon. {NOT_TAKEN}',
        'Die Verknüpfung bestand schon.']


KINDS = [
    pytest.param({'created': True, 'numberTaken': True, 'documentNumber': '26B002'}, 'success', id='uebernommen'),
    pytest.param({'created': True}, 'success', id='zugeordnet'),
    pytest.param({'created': True, 'numberInUse': True}, 'info', id='nicht-uebernommen-weil-in-Gebrauch'),
    pytest.param({'created': False}, 'info', id='bestand-schon'),
    pytest.param({'created': False, 'numberInUse': True}, 'info', id='bestand-schon-und-in-Gebrauch'),
]


@NO_NODE
@pytest.mark.parametrize('fields, kind', KINDS)
def test_a_number_that_was_not_taken_is_shown_as_a_hint_not_as_a_success_in_the_mask(tmp_path, mask, fields, kind):
    data = {'success': True, 'documents': [], 'bookings': [], **fields}
    out = run_mask(tmp_path, mask, call=f'txAttach({mask.free})', documentNr='', splitDocnr='',
                   responses=[answer(200, data), answer(200, {'success': True, 'results': []})])
    assert len(out['msgs']) == 1 and out['msgs'][0][1] == kind, out['msgs']


SWITCHED = [
    pytest.param({'txReceipts': {'booking': '999'}}, id='eine-andere-Buchung-ist-geoeffnet'),
    pytest.param({'txReceipts': None}, id='die-Maske-fehlt-eine-neue-Buchung-ist-geoeffnet'),
]


@NO_NODE
@pytest.mark.parametrize('dom', SWITCHED)
def test_an_answer_for_a_mask_that_was_replaced_touches_nothing_of_the_new_one(tmp_path, mask, dom):
    """Die Maske wird beim Öffnen einer anderen (oder neuen) Buchung ausgetauscht (``openEditForm``). Trifft die Antwort erst danach ein, darf
    sie weder die Liste noch das Feld Beleg-Nr. (noch die Basis der Teilbuchungen) noch die Meldung der neuen Maske anfassen."""
    taken = {'success': True, 'created': True, 'numberTaken': True, 'documentNumber': '26B002', 'documents': [], 'bookings': []}
    out = run_mask(tmp_path, mask, call=f'txAttach({mask.free})', documentNr='', splitDocnr='',
                   responses=[answer(200, taken, dom=dom), answer(200, {'success': True, 'results': []})])
    assert len(out['fetches']) == 1 and json.loads(out['fetches'][0]['body']) == {'bookingId': mask.booking, 'documentId': mask.free}
    assert out['msgs'] == [] and out['documentNr'] == '' and out['splitDocnr'] == ''
    (cells,) = rows_of(out['tree']['txReceipts'])                                         # die Liste ist, wie sie war
    assert cells[0]['text'] == HOSTILE                                                    # (und die eine Anfrage ist die Zuordnung: keine Suche danach)


@NO_NODE
@pytest.mark.parametrize('dom', SWITCHED)
def test_the_answer_to_unlinking_for_a_replaced_mask_touches_nothing_either(tmp_path, mask, dom):
    done = {'success': True, 'removed': True, 'documents': [], 'bookings': []}
    out = run_mask(tmp_path, mask, clicks=['Lösen'], documentNr='ER-7', responses=[answer(200, done, dom=dom)])
    assert len(out['fetches']) == 1 and json.loads(out['fetches'][0]['body']) == {'bookingId': mask.booking, 'documentId': mask.linked}
    assert out['msgs'] == [] and out['documentNr'] == 'ER-7'
    (cells,) = rows_of(out['tree']['txReceipts'])                                         # die Zeile steht noch da
    assert cells[0]['text'] == HOSTILE


@NO_NODE
@pytest.mark.parametrize('dom', SWITCHED)
def test_the_hits_of_a_search_for_a_replaced_mask_are_not_drawn(tmp_path, mask, dom):
    out = run_mask(tmp_path, mask, call='txReceiptSearch()', queryField='txReceiptQuery', queryValue='x',
                   responses=[answer(*handlers.handle_receipt_search(mask.db, {'q': ['']}), dom=dom)])
    assert out['msgs'] == [] and out['tree']['txReceiptResults']['children'] == []


@NO_NODE
def test_an_answer_that_names_the_same_booking_is_still_applied(tmp_path, mask):
    """Gegenprobe: Auch wenn die Maske zwischendurch neu eingespielt wurde, gilt die Antwort, solange sie dieselbe Buchung zeigt."""
    taken = {'success': True, 'created': True, 'numberTaken': True, 'documentNumber': '26B002', 'documents': [], 'bookings': []}
    again = {'txReceipts': {'booking': str(mask.booking)}}
    out = run_mask(tmp_path, mask, call=f'txAttach({mask.free})', documentNr='', splitDocnr='',
                   responses=[answer(200, taken, dom=again), answer(200, {'success': True, 'results': []})])
    assert out['documentNr'] == '26B002' and out['msgs'] == [['Zugeordnet – die Buchung trägt jetzt die Beleg-Nr. 26B002.', 'success']]


@NO_NODE
def test_a_saved_link_with_unreadable_lists_shows_a_hint_and_redraws_nothing_in_the_mask(tmp_path, mask):
    """``stale``: Die Verknüpfung ist gespeichert, der Server konnte die Listen danach nicht lesen – die (leeren) Listen der Antwort
    zeichnen nichts neu, ein Hinweis bittet um das Neuladen."""
    stale = {'success': True, 'created': True, 'numberTaken': True, 'documentNumber': '26B002', 'numberInUse': False,
             'hasEntries': False, 'documents': [], 'bookings': [], 'stale': True}
    out = run_mask(tmp_path, mask, call=f'txAttach({mask.free})', documentNr='', splitDocnr='',
                   responses=[answer(200, stale), answer(200, {'success': True, 'results': []})])
    assert out['msgs'] == [['Gespeichert – bitte die Seite neu laden.', 'info']] and len(out['fetches']) == 1    # keine Suche danach
    (cells,) = rows_of(out['tree']['txReceipts'])                                         # die Liste ist unverändert
    assert cells[0]['text'] == HOSTILE
    done = {'success': True, 'removed': True, 'documents': [], 'bookings': [], 'stale': True}
    out = run_mask(tmp_path, mask, clicks=['Lösen'], responses=[answer(200, done)])
    assert out['msgs'] == [['Gespeichert – bitte die Seite neu laden.', 'info']]
    (cells,) = rows_of(out['tree']['txReceipts'])
    assert cells[0]['text'] == HOSTILE


@NO_NODE
def test_a_receipt_with_a_file_offers_view_and_download_in_the_mask_and_the_download_goes_through_the_download_function(tmp_path, tmp_db):
    folder = os.path.join(userctx.DATA_ROOT, 'Belege', '2026')             # ohne angemeldeten Benutzer liegt alles unter der Datenwurzel
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, '26B001_rechnung.pdf'), 'wb') as handle:
        handle.write(PDF)
    receipt = add_receipt(tmp_db, '26B001', path='Belege/2026/26B001_rechnung.pdf')
    booking = add_booking(tmp_db)
    tmp_db.link_booking_to_document(booking, receipt)
    page = PageTransactions(tmp_db, edit_transaction_id=booking, date_from='2026-01-01', date_to='2026-12-31')
    assert script_data(page, 'TX_RECEIPTS')[0]['file'] == 'ok' and 'Belege/2026' not in page          # nie ein Pfad in der Seite
    out = run_page_script(tmp_path, script_of(page, 'txReceiptsRender'), clicks=['Herunterladen'])
    assert out['downloads'] == [f'/receipts/file?id={receipt}']
    (cells,) = rows_of(out['tree']['txReceipts'])
    nodes = [node for node in cells[4]['children'][0]['children'] if node['tag'] == 'a']
    assert [(node['text'], node['href'], node['target']) for node in nodes] == [
        ('Ansehen', f'/receipts/file?id={receipt}&inline=1', '_blank'), ('Herunterladen', '#', ''),
        ('Beleg', f'/receipts/edit?id={receipt}', '_blank')]
    assert 'Belege/2026' not in json.dumps(out)


@pytest.fixture
def receipt_page(tmp_db):
    """Ein Beleg mit einer verknüpften Buchung (feindliche Werte) und einer zweiten, noch nicht verknüpften; dazu die Seite des Belegs."""
    receipt = add_receipt(tmp_db, '26B001')
    linked = add_booking(tmp_db, recipient_client=IMG, text='<b>x</b>', amount=Decimal('-12.50'), document_number=HOSTILE)
    free = add_booking(tmp_db, recipient_client='Andere', text="O'Brien & Söhne", amount=Decimal('99.00'))
    tmp_db.link_booking_to_document(linked, receipt)
    html = PageReceiptEdit(tmp_db, receipt)
    return type('Page', (), dict(db=tmp_db, receipt=receipt, linked=linked, free=free, script=script_of(html, 'receiptLinksRender')))


@NO_NODE
def test_hostile_values_become_text_never_markup_on_the_page_of_a_receipt(tmp_path, receipt_page):
    status, body = handlers.handle_booking_search(receipt_page.db, {'q': ['']})
    out = run_page_script(tmp_path, receipt_page.script, call='receiptBookingSearch()', queryField='receiptBookingQuery', queryValue='x',
                          responses=[answer(status, body)])
    tags = {node['tag'] for root in out['tree'].values() for node in walk(root)}
    assert tags <= {'div', 'table', 'tr', 'th', 'td', 'p', 'button', 'span', 'a', '#text'}, tags
    (cells,) = rows_of(out['tree']['receiptLinks'])
    assert [cell['text'] for cell in cells[:6]] == [DAY, f'{IMG} · <b>x</b>', '-12,50 EUR', HOSTILE, 'Buchungssatz', 'Beleg']
    assert [(n['tag'], n['text'], n['href']) for n in walk(cells[6]) if n['tag'] in ('a', 'button')] == [
        ('a', 'Öffnen', f'/transactions/edit?id={receipt_page.linked}'), ('button', 'Lösen', '')]
    found = rows_of(out['tree']['receiptBookingResults'])
    assert {row[0]['text'] for row in found} == {DAY} and len(found) == 2
    by_text = {row[1]['text']: row for row in found}
    linked_action = by_text[f'{IMG} · <b>x</b>'][5]['children'][0]
    free_action = by_text["Andere · O'Brien & Söhne"][5]['children'][0]
    assert (linked_action['tag'], linked_action['text'], linked_action['className']) == ('span', 'zugeordnet', 'muted')
    assert (free_action['tag'], free_action['text']) == ('button', 'Zuordnen')
    assert out['fetches'] == [{'url': '/transactions/search?q=x', 'method': 'GET', 'body': None}]


@NO_NODE
def test_attaching_and_detaching_on_the_page_of_a_receipt_updates_the_table_and_says_what_happened(tmp_path, receipt_page):
    db = receipt_page.db
    attached = handlers.handle_booking_link_document(
        db, json.dumps({'bookingId': receipt_page.free, 'documentId': receipt_page.receipt}).encode())
    assert json.loads(attached[1])['numberTaken'] is True and number_of(db, receipt_page.free) == '26B001'
    db.unlink_receipt_from_booking(receipt_page.free, receipt_page.receipt)
    search_all = handlers.handle_booking_search(db, {'q': ['']})
    out = run_page_script(tmp_path, receipt_page.script, call='receiptBookingSearch()', clicks=['Zuordnen'],
                          queryField='receiptBookingQuery', queryValue='', responses=[answer(*search_all), answer(*attached), answer(*search_all)])
    assert [fetch['url'] for fetch in out['fetches']] == ['/transactions/search?q=', '/bookings/link-document', '/transactions/search?q=']
    assert json.loads(out['fetches'][1]['body']) == {'bookingId': receipt_page.free, 'documentId': receipt_page.receipt}
    assert out['msgs'] == [['Zugeordnet – die Buchung trägt jetzt die Beleg-Nr. 26B001.', 'success']]
    assert len(rows_of(out['tree']['receiptLinks'])) == 2                               # die Tabelle kommt aus der Antwort
    done = {'success': True, 'removed': True, 'documents': [], 'bookings': []}
    out = run_page_script(tmp_path, receipt_page.script, clicks=['Lösen'], responses=[answer(200, done)])
    assert out['confirms'] == ['Verknüpfung lösen? Die Beleg-Nr. der Buchung bleibt.']
    post = out['fetches'][0]
    assert post['url'] == '/bookings/unlink-document' and json.loads(post['body']) == {
        'bookingId': receipt_page.linked, 'documentId': receipt_page.receipt}
    assert out['msgs'] == [['Verknüpfung gelöst – die Beleg-Nr. der Buchung bleibt.', 'success']]
    assert [n['text'] for n in walk(out['tree']['receiptLinks']) if n['tag'] == 'p'] == ['Keine verknüpften Buchungen.']


@NO_NODE
@pytest.mark.parametrize('fields, kind', KINDS)
def test_a_number_that_was_not_taken_is_shown_as_a_hint_not_as_a_success_on_the_page_of_a_receipt(tmp_path, receipt_page, fields, kind):
    data = {'success': True, 'documents': [], 'bookings': [], **fields}
    out = run_page_script(tmp_path, receipt_page.script, call=f'receiptAttach({receipt_page.free})',
                          responses=[answer(200, data), answer(200, {'success': True, 'results': []})])
    assert len(out['msgs']) == 1 and out['msgs'][0][1] == kind, out['msgs']
    if fields.get('numberInUse'):
        assert out['msgs'][0][0].endswith(NOT_TAKEN)


@NO_NODE
def test_a_bank_movement_with_entries_says_so_on_the_page_of_a_receipt(tmp_path, receipt_page):
    data = {'success': True, 'created': True, 'numberTaken': True, 'documentNumber': '26B001', 'hasEntries': True, 'numberInUse': False,
            'documents': [], 'bookings': []}
    out = run_page_script(tmp_path, receipt_page.script, call=f'receiptAttach({receipt_page.free})',
                          responses=[answer(200, data), answer(200, {'success': True, 'results': []})])
    assert out['msgs'] == [['Zugeordnet – die Bankbewegung trägt jetzt die Beleg-Nr. 26B001; '
                            'ihre Buchungssätze behalten ihre eigenen Nummern.', 'success']]


@NO_NODE
def test_a_saved_link_with_unreadable_lists_shows_a_hint_and_redraws_nothing_on_the_page_of_a_receipt(tmp_path, receipt_page):
    stale = {'success': True, 'created': True, 'numberTaken': True, 'documentNumber': '26B001', 'documents': [], 'bookings': [],
             'stale': True}
    out = run_page_script(tmp_path, receipt_page.script, call=f'receiptAttach({receipt_page.free})',
                          responses=[answer(200, stale), answer(200, {'success': True, 'results': []})])
    assert out['msgs'] == [['Gespeichert – bitte die Seite neu laden.', 'info']] and len(out['fetches']) == 1   # keine Suche danach
    assert len(rows_of(out['tree']['receiptLinks'])) == 1                               # die Zeile der Seite steht noch da
    done = {'success': True, 'removed': True, 'documents': [], 'bookings': [], 'stale': True}
    out = run_page_script(tmp_path, receipt_page.script, clicks=['Lösen'], responses=[answer(200, done)])
    assert out['msgs'] == [['Gespeichert – bitte die Seite neu laden.', 'info']]
    assert len(rows_of(out['tree']['receiptLinks'])) == 1
