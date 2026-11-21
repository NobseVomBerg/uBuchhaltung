# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""„Zuordnung neu aufbauen“ (2.5.13): ``Database.rebuild_invoice_payments`` und ``POST /invoice/rebuild-payments``.

Zuordnungen älterer Versionen haben oft keine Buchungssätze, eine als Kind angehängte Forderung, doppelte Zeilen oder einen
offenen Rest, der nicht passt. Die Reparatur macht je Rechnung nur vier Dinge – doppelte Zuordnungen zusammenfassen, die
angehängte Forderung der App lösen, eine Zuordnung ohne eigene Sätze mit den heutigen Bausteinen neu schreiben, den offenen
Betrag neu rechnen – und **löscht und ändert keinen Buchungssatz**. Hier steht, was dazugehört:

* die positiven Fälle, jeder mit denselben Prüfungen (``rebuild_and_verify``): Der Probelauf lässt jede Tabelle samt
  ``sqlite_sequence`` unverändert und schreibt nichts ins Replay-Log; zwei Probeläufe ergeben dieselbe Prüfsumme; die Ausführung
  heilt den Befund und bewegt die EÜR genau um das, was der Plan ankündigt; die neuen Sätze tragen Marke und Datum der Bewegung;
  Sätze mit Quellklammer bleiben; ein zweiter Probelauf findet nichts mehr; das Replay-Log folgt dem Commit;
* die Steuer: Mehrere Zuordnungen einer Rechnung werden in zwei Phasen neu gebucht (erst alle entfernen, dann in zeitlicher Folge) –
  jede Zuordnung bekommt dieselbe Steuer wie beim Zuordnen nacheinander, und passt eine nicht, bleibt sie, und die übrigen werden
  trotzdem gebaut;
* die Fälle, die nur gemeldet werden (nichts geschrieben): jede Bedingung von R1 einzeln, Storno-Paare, falscher Status;
* **nichts wird doppelt gebucht:** Steht zur Rechnung schon etwas an anderer Stelle in den Büchern (Sätze mit ihrer Nummer
  eigenständig, auf der Kasse, unter einer fremden Bewegung, mit oder ohne Quellklammer, in anderer Schreibweise, auch mit
  Tabulator oder Zeilenumbruch am Rand), baut C nichts auf (``elsewhere``) – ebenso, wenn die Sätze unter den zugeordneten Bewegungen
  mehr Erlös buchen, als die übrigen Zuordnungen erklären (``overbooked`` je Rechnung, ``overbooked_movement`` je Bewegung) – EÜR
  und Salden bleiben –, und ein zweites Netz nimmt alles zurück, wenn die Prüfliste danach eine neue Kategorie nennt;
* der Schutz: Prüfsumme (geänderter Bestand, fehlende Bestätigung; der Tag nur bei Dubletten), kein Konto wird angelegt, der
  Probelauf lässt weder eine Sperre noch einen Zähler zurück – auch nicht nach einer Ablehnung oder einer Ausnahme –, und ein
  Baustein, der einen vorhandenen Buchungssatz verändern würde, wird zurückgenommen;
* gesunde Bestände (kein Plan, keine Änderung, auch kein ``UpdatedAt``), die Route (nur POST, Token, Herkunft, strenge Werte,
  feste Fehlersätze), die Seite und ihr Skript (Node, nur ``textContent``).

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import json
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

import pytest

from db import Database
from db.invoice_payments import NO_REVENUE_COA, NOT_A_REVENUE_COA, PAYMENT_MARK
from db.invoice_repair import (REBUILD_CHANGED_MESSAGE, REBUILD_CONFIRM_MISSING, REBUILD_DONE_MESSAGE,
                               REBUILD_NOTHING_MESSAGE, REBUILD_SKIP_REASONS, REBUILD_UNSAFE_MESSAGE, REBUILD_WARNING,
                               RebuildCandidate, _EDGE_WHITESPACE, _SAME_NUMBER_SQL)
from server.handlers import REBUILD_FAILED, rebuild_plan_payload
from server.pages_invoice import REBUILD_CONFIRM_QUESTION, PageInvoice
from server.pages_miscellaneous import PageMiscellaneous
from tests.document_helpers import post_json
from tests.dom_helpers import Dom
from tests.http_client import FOREIGN_ORIGIN, get, send
from tests.test_invoice_payment_assignment import (RANGE, _account, _adopt, _assign, _balance, _bank, _children, _coa, _euer,
                                                   _exec, _invoice, _invoice_state, _raw, _revenue_account, _sent,
                                                   _synthetic_books, _wiso_like_books)
from tests.test_invoice_reissue import paid_scenario
from tests.test_invoice_storno import cancel, pay, world, sent as world_sent  # noqa: F401  (world: Fixture)
from tests.invoice_world import set_invoice_status

FIXED_TODAY = '2026-06-30'          # ein fester Tag für den Status – nie die Uhr (die Suite läuft Minuten)
ROUTE = '/invoice/rebuild-payments'
INVALID_REQUEST = {'success': False, 'error': 'Ungültige Anfrage.'}
HOSTILE = '<img src=x onerror=1>'


# ── Hilfen ───────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_db):
    return tmp_db


@pytest.fixture
def acct(db):
    return _account(db)


class Log:
    """Was die Datenbankschicht ins Replay-Log schreiben will (``(SQL, Parameter, Beschreibung)``) und wie viele Buchungen ein
    ANDERER Leser in diesem Augenblick sieht – so zeigt sich, dass das Log erst nach dem Commit geschrieben wird."""

    def __init__(self):
        self.entries, self.visible = [], []

    def clear(self):
        self.entries.clear()
        self.visible.clear()


@pytest.fixture
def logged(monkeypatch):
    log = Log()

    def record(self, sql, params, description):
        log.entries.append((sql, params, description))
        log.visible.append(_raw(self, 'SELECT COUNT(*) FROM Bookings')[0][0])

    monkeypatch.setattr(Database, '_log_sql', record)
    return log


def all_tables(db):
    """Alle Tabellen der Datenbank samt ``sqlite_sequence``, Zeile für Zeile (nach ``rowid``) – jede Spalte zählt."""
    con = sqlite3.connect(db.db_name)
    try:
        names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {name: con.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names}
    finally:
        con.close()


def lock_is_free(db):
    """Eine zweite Verbindung nimmt die Schreibsperre sofort (kein Warten): Der Probelauf hat sie nicht behalten."""
    con = sqlite3.connect(db.db_name, timeout=0)
    try:
        con.execute('BEGIN IMMEDIATE')
        con.rollback()
    finally:
        con.close()


def dry(db, invoice_id, **kw):
    return db.rebuild_invoice_payments(invoice_id, today=FIXED_TODAY, **kw)


def run(db, invoice_id, plan, **kw):
    return db.rebuild_invoice_payments(invoice_id, dry_run=False, confirmed_check=plan.check, today=FIXED_TODAY, **kw)


def announced(plan, vat=3806):
    """Was der Plan der EÜR ankündigt: Erlöskonto → netto, ``vat`` → Umsatzsteuer (Euro, auf den Cent gerundet)."""
    result = defaultdict(int)
    for effect in plan.euer:
        result[effect.account] += effect.net
        result[vat] += effect.tax
    return {account: round(minor / 10000, 2) for account, minor in result.items() if minor}


def delta(before, after):
    """Was sich zwischen zwei ``_euer``-Ständen geändert hat."""
    return {account: round(after.get(account, 0) - before.get(account, 0), 2)
            for account in set(before) | set(after) if round(after.get(account, 0) - before.get(account, 0), 2)}


def kinds(plan):
    return [action.kind for action in plan.actions]


def imported_rows(db):
    return _raw(db, "SELECT * FROM Bookings WHERE SourceGroup IS NOT NULL AND SourceGroup <> '' ORDER BY ID")


def rebuild_and_verify(db, invoice_id, logged, expected_kinds, logs=True):
    """Probelauf, Ausführung und alles, was für jeden positiven Fall gilt (siehe Modul). Liefert den Plan. ``logs``: Die
    Ausführung schreibt ins Replay-Log – nur das Neuberechnen des offenen Betrags tut es nicht (wie ``_recalc_invoice``)."""
    max_id = _raw(db, 'SELECT COALESCE(MAX(ID), 0) FROM Bookings')[0][0]
    before, euer_before, imported = all_tables(db), _euer(db), imported_rows(db)
    logged.clear()
    first = dry(db, invoice_id)
    assert (first.ok, first.code, first.executed, first.message, first.needs_revenue_coa) == (True, 200, False, None, False)
    plan = first.plan
    assert kinds(plan) == expected_kinds
    assert all_tables(db) == before and logged.entries == []                      # (a) der Probelauf schreibt nichts
    assert dry(db, invoice_id).plan.check == plan.check and all_tables(db) == before   # (b) dieselbe Prüfsumme

    result = run(db, invoice_id, plan)
    assert (result.ok, result.code, result.executed, result.message) == (True, 200, True, REBUILD_DONE_MESSAGE)
    assert [(f['category'], f['text']) for f in db.find_payment_inconsistencies(invoice_id)] == plan.findings_after   # (c)
    assert delta(euer_before, _euer(db)) == announced(plan)                       # (d) die EÜR bewegt sich wie angekündigt
    for row_id, status, day, parent in _raw(db, 'SELECT ID, Status, DateBooking, ParentBooking_ID FROM Bookings WHERE ID > ?',
                                            max_id):                              # (e) Marke und Datum der Bewegung
        assert (status, day) == (PAYMENT_MARK, _raw(db, 'SELECT DateBooking FROM Bookings WHERE ID = ?', parent)[0][0])
    assert imported_rows(db) == imported                                          # (f) Sätze mit Quellklammer bleiben
    assert dry(db, invoice_id).plan.actions == []                                 # (g) danach gibt es nichts mehr zu tun
    final = _raw(db, 'SELECT COUNT(*) FROM Bookings')[0][0]
    assert bool(logged.entries) is logs and set(logged.visible) <= {final}        # (h) das Log folgt dem Commit
    return plan


def linked_case(db, acct, number='R001', gross=119.00, bank_amount=None, date='2026-02-01'):
    """Eine Rechnung, deren Zuordnung der alte Verknüpfungsweg angelegt hat: nur die Zeile, keine Buchungssätze."""
    inv = _sent(db, number, gross)
    bank = _bank(db, acct, gross if bank_amount is None else bank_amount, date=date)
    db.link_invoice_to_transaction(inv, bank, gross)
    return inv, bank


def drop_unique_index(db):
    """Dubletten gibt es nur in einem Bestand aus der Zeit vor dem eindeutigen Index."""
    _exec(db, 'DROP INDEX IF EXISTS ux_invoicepayments_invoice_booking')


def raw_payment(db, invoice_id, bank_id, minor, day='2026-02-01', notes=None):
    _exec(db, 'INSERT INTO InvoicePayments (InvoiceID, BookingID, Amount, PaymentDate, Notes) VALUES (?, ?, ?, ?, ?)',
          invoice_id, bank_id, minor, day, notes)
    return _raw(db, 'SELECT MAX(ID) FROM InvoicePayments')[0][0]


def unmarked_pair(db, bank, number, gross=119.00, tax=19.00, day='2026-02-01'):
    """„Bank an Debitoren“ und „Wartekonto an Erlöskonto“ ohne Marke unter der Bewegung (so schrieb es eine ältere Version)."""
    db.insert_booking(day, gross, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000), booking_type='entry',
                      parent_booking_id=bank, document_number=number, text=f'Zahlung {number}')
    db.insert_booking(day, gross, coa_id=_coa(db, 4405), counter_coa_id=_coa(db, 4400), booking_type='entry',
                      parent_booking_id=bank, document_number=number, tax_rate=0.19, tax_amount=tax,
                      text=f'Umb. {number}')


def revenue_row(db, bank, number, gross, tax, day='2026-03-01'):
    """Der Einzelsatz „Erlöskonto an Bank“ unter einer Bewegung, mit der Nummer der Rechnung und ohne Marke (so schrieb es der
    Altbestand „Zahlung verbuchen mit Konto“): Er bucht ``gross`` Erlös. Liefert seine ID."""
    return db.insert_booking(day, gross, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1800), booking_type='entry',
                             parent_booking_id=bank, document_number=number, tax_rate=0.19, tax_amount=tax, text=f'Erlös {number}')


def nothing_happens(db, invoice_id, skipped, expected_kinds=(), execute=True):
    """Der Plan nennt genau diese Gründe (``skipped``: Texte) und diese Aktionen, und weder Probelauf noch Ausführung ändern
    irgendetwas – ``recalc`` ausgenommen, wo es in ``expected_kinds`` steht (dann ändert die Ausführung nur die Rechnung)."""
    before = all_tables(db)
    plan = dry(db, invoice_id).plan
    assert plan.skipped == skipped and kinds(plan) == list(expected_kinds)
    assert all_tables(db) == before
    if execute and not expected_kinds:
        result = run(db, invoice_id, plan)
        assert (result.ok, result.executed, result.message) == (True, False, REBUILD_NOTHING_MESSAGE)
        assert all_tables(db) == before
    return plan


def text_of(key, **names):
    return REBUILD_SKIP_REASONS[key].format(**names)


def rows_without_ids(db):
    """Alle Buchungssätze und Zuordnungen, Spalte für Spalte, ohne ihre IDs (und ohne ``CreatedAt``) – nach Inhalt sortiert. Neu
    geschriebene Zeilen bekommen andere IDs als die zuerst geschriebenen; verglichen wird, was in ihnen steht."""
    con = sqlite3.connect(db.db_name)
    try:
        cursor = con.execute('SELECT * FROM Bookings')
        keep = [index for index, column in enumerate(cursor.description) if column[0] != 'ID']
        bookings = sorted((tuple(row[index] for index in keep) for row in cursor), key=repr)
        payments = sorted(con.execute('SELECT InvoiceID, BookingID, Amount, PaymentDate, Notes FROM InvoicePayments'), key=repr)
    finally:
        con.close()
    return bookings, payments


def assert_elsewhere_stops_the_rebuild(db, invoice_id):
    """Der Erlös der Rechnung steht schon an anderer Stelle: Der Plan nennt genau den Grund ``elsewhere`` und keine Aktion
    ``rebuild``, Probelauf und Ausführung mit der Prüfsumme ändern keine Tabelle, und EÜR und die Salden von 4400 und 10000 bleiben,
    wie sie sind – nichts wird ein zweites Mal gebucht. Liefert den Plan."""
    before, euer, balances = all_tables(db), _euer(db), (_balance(db, 4400), _balance(db, 10000))
    plan = dry(db, invoice_id).plan
    assert 'rebuild' not in kinds(plan) and plan.skipped == [text_of('elsewhere')] and plan.rows == []
    assert all_tables(db) == before
    result = run(db, invoice_id, plan)
    assert (result.ok, result.code, result.executed) == (True, 200, False)
    assert all_tables(db) == before                                       # Bookings und InvoicePayments – und alles andere
    assert (_euer(db), (_balance(db, 4400), _balance(db, 10000))) == (euer, balances)
    return plan


# ── Die positiven Fälle ──────────────────────────────────────────────────────

def test_assignment_without_rows_is_rebuilt(db, acct, logged):
    inv, bank = linked_case(db, acct)
    assert _invoice_state(db, inv) == ('paid', 0) and _children(db, bank) == []

    plan = rebuild_and_verify(db, inv, logged, ['rebuild'])

    assert _children(db, bank) == [
        (1800, 10000, Decimal('119'), None, 'Zahlung zu Re. R001', PAYMENT_MARK),
        (4405, 4400, Decimal('119'), Decimal('19'), 'Umb. zu Re. R001', PAYMENT_MARK)]
    assert _invoice_state(db, inv) == ('paid', 0)
    assert announced(plan) == {4400: 100.0, 3806: 19.0}
    assert plan.skipped == [] and (plan.status_before, plan.status_after, plan.due_before, plan.due_after) == ('paid', 'paid', 0, 0)
    assert [(row.account, row.counter_account, row.amount, row.tax, row.form, row.euer_account) for row in plan.rows] == [
        (1800, 10000, 1190000, None, 'liquid_first', None), (4405, 4400, 1190000, 190000, 'transfer', 4400)]
    assert plan.actions[0].text == ('Zuordnung #1 (119,00 €) zu Bewegung #2 vom 2026-02-01 wird mit den Sätzen „Bank an '
                                    'Debitoren“ und „Wartekonto an Erlöskonto“ neu geschrieben.')
    assert [category for category, _text in plan.findings_before] == ['no_settlement', 'transfer_mismatch']
    assert plan.findings_after == []


def test_the_replay_log_names_what_was_written_and_follows_the_commit(db, acct, logged):
    inv, bank = linked_case(db, acct)
    old_id = _raw(db, 'SELECT ID FROM InvoicePayments')[0][0]
    logged.clear()
    plan = dry(db, inv).plan
    assert logged.entries == []

    assert run(db, inv, plan).executed is True

    assert [(sql.split(' (')[0].split(' WHERE')[0], description) for sql, _params, description in logged.entries] == [
        ('DELETE FROM InvoicePayments', 'Rebuild: remove assignment'),
        ('INSERT INTO InvoicePayments', 'Link invoice to booking'),
        ('INSERT INTO Bookings', 'Payment child (bank to debtor)'),
        ('INSERT INTO Bookings', 'Payment child (open item to revenue)')]
    assert logged.entries[0][1] == (old_id,) and logged.entries[1][1] == (inv, bank, 1190000, '2026-02-01')
    assert set(logged.visible) == {_raw(db, 'SELECT COUNT(*) FROM Bookings')[0][0]}       # erst nach dem Commit


@pytest.mark.parametrize('marked', [True, False], ids=['R1-markiert', 'R1-ohne-Marke'])
def test_collective_payment_second_invoice_is_rebuilt(db, acct, logged, marked):
    """Bei einer Sammelzahlung bekam nur die erste Rechnung ihre Sätze: Die zweite bekommt ihre, die erste bleibt, wie sie ist."""
    bank = _bank(db, acct, 357.00)
    r1, r2 = _sent(db, 'R001', 119.00), _sent(db, 'R002', 238.00)
    if marked:
        _assign(db, r1, bank)
    else:
        unmarked_pair(db, bank, 'R001')
        db.link_invoice_to_transaction(r1, bank, 119.00)
    db.link_invoice_to_transaction(r2, bank, 238.00)
    r1_rows = _raw(db, "SELECT * FROM Bookings WHERE DocumentNumber = 'R001' ORDER BY ID")
    r1_payment = _raw(db, 'SELECT * FROM InvoicePayments WHERE InvoiceID = ?', r1)

    plan = rebuild_and_verify(db, r2, logged, ['rebuild'])

    assert announced(plan) == {4400: 200.0, 3806: 38.0}
    assert _raw(db, "SELECT * FROM Bookings WHERE DocumentNumber = 'R001' ORDER BY ID") == r1_rows
    assert _raw(db, 'SELECT * FROM InvoicePayments WHERE InvoiceID = ?', r1) == r1_payment
    assert dry(db, r1).plan.actions == [] and _invoice_state(db, r2) == ('paid', 0)
    assert db.get_booking_unallocated_amount(bank) == 0 and db.find_unbalanced_splits(*RANGE) == []


def test_attached_receivable_is_detached_and_the_payment_rebuilt(db, acct, logged):
    inv, bank = linked_case(db, acct)
    receivable = db.get_receivable_booking(inv)
    _adopt(db, receivable, bank)                    # der alte Auto-Abgleich: die Forderung hängt als Kind an der Bewegung
    stored = _raw(db, 'SELECT * FROM Bookings WHERE ID = ?', receivable)[0]
    columns = [row[1] for row in _raw(db, 'PRAGMA table_info(Bookings)')]

    plan = rebuild_and_verify(db, inv, logged, ['detach', 'rebuild'])

    now = _raw(db, 'SELECT * FROM Bookings WHERE ID = ?', receivable)[0]
    changed = {name for name, was, has in zip(columns, stored, now) if was != has}
    assert changed == {'ParentBooking_ID', 'Status'} and (now[columns.index('ParentBooking_ID')], now[columns.index('Status')]) == (None, None)
    assert plan.actions[0].text == (f'Die Forderung #{receivable} wird von der Bankbewegung #{bank} gelöst; Datum, Konten und '
                                    'Betrag bleiben.')
    assert db.get_receivable_booking(inv) == receivable and len(_children(db, bank)) == 2


def test_attached_receivable_without_assignment_is_only_detached(db, acct, logged):
    inv = _sent(db, 'R001', 119.00)
    bank = _bank(db, acct, 119.00)
    receivable = db.get_receivable_booking(inv)
    _adopt(db, receivable, bank)
    assert [f['category'] for f in db.find_payment_inconsistencies(inv)] == ['receivable_attached']

    plan = rebuild_and_verify(db, inv, logged, ['detach'])

    assert announced(plan) == {} and plan.rows == [] and plan.euer == []                 # keine Wirkung in der EÜR
    assert _raw(db, 'SELECT ParentBooking_ID, Status FROM Bookings WHERE ID = ?', receivable) == [(None, None)]
    assert _invoice_state(db, inv) == ('sent', 119)


def test_an_attached_receivable_under_a_healthy_assignment_is_only_detached(db, acct, logged):
    inv = _sent(db, 'R001', 119.00)
    bank = _bank(db, acct, 119.00)
    _assign(db, inv, bank)
    receivable = db.get_receivable_booking(inv)
    _adopt(db, receivable, bank)
    rows = _raw(db, 'SELECT * FROM Bookings WHERE ParentBooking_ID = ? AND Status = ? ORDER BY ID', bank, PAYMENT_MARK)

    plan = rebuild_and_verify(db, inv, logged, ['detach'])

    assert announced(plan) == {} and plan.rows == []
    assert _raw(db, 'SELECT * FROM Bookings WHERE ParentBooking_ID = ? AND Status = ? ORDER BY ID', bank, PAYMENT_MARK) == rows
    assert _raw(db, 'SELECT ParentBooking_ID, Status FROM Bookings WHERE ID = ?', receivable) == [(None, None)]


def test_duplicates_with_unmarked_rows_are_merged(db, acct, logged):
    inv = _sent(db, 'R001', 119.00)
    bank = _bank(db, acct, 119.00)
    unmarked_pair(db, bank, 'R001')
    drop_unique_index(db)
    first, second = (raw_payment(db, inv, bank, 1190000), raw_payment(db, inv, bank, 1190000))
    assert {f['category'] for f in db.find_payment_inconsistencies(inv)} == {
        'duplicate_payment', 'amount_due_mismatch', 'transfer_mismatch'}       # 238 zugeordnet, 119 umgebucht
    rows_before = _raw(db, 'SELECT * FROM Bookings ORDER BY ID')

    plan = rebuild_and_verify(db, inv, logged, ['dedupe', 'recalc'])

    assert [row[0] for row in _raw(db, 'SELECT ID FROM InvoicePayments')] == [first]          # die kleinste ID bleibt
    assert _raw(db, 'SELECT * FROM Bookings ORDER BY ID') == rows_before                      # kein Satz angefasst
    assert _invoice_state(db, inv) == ('paid', 0) and announced(plan) == {} and plan.rows == []
    assert plan.actions[0].text == (f'Doppelte Zuordnung #{second} zu Bewegung #{bank} (119,00 €) wird entfernt; '
                                    f'Zuordnung #{first} bleibt.')
    assert plan.actions[1].text == 'Offener Betrag 119,00 € → 0,00 €; Status sent → paid'
    assert db.find_payment_inconsistencies(inv) == []


def test_duplicates_without_rows_are_merged_and_rebuilt(db, acct, logged):
    inv = _sent(db, 'R-E', 238.00)
    bank = _bank(db, acct, 238.00, date='2026-02-05')
    drop_unique_index(db)
    first = raw_payment(db, inv, bank, 1190000, '2026-02-05')
    raw_payment(db, inv, bank, 1190000, '2026-02-05')

    plan = rebuild_and_verify(db, inv, logged, ['dedupe', 'rebuild', 'recalc'])

    assert announced(plan) == {4400: 100.0, 3806: 19.0}
    assert _invoice_state(db, inv) == ('partial_payment', 119)
    assert (plan.status_before, plan.status_after, plan.due_before, plan.due_after) == ('sent', 'partial_payment', 2380000, 1190000)
    assert _raw(db, 'SELECT Amount FROM InvoicePayments') == [(1190000,)] and first not in [r[0] for r in _raw(db, 'SELECT ID FROM InvoicePayments')]
    assert db.find_payment_inconsistencies(inv) == []                           # auch der Index könnte jetzt wieder angelegt werden


def test_amount_due_only_is_recalculated_and_status_kept(db, acct, logged):
    inv = _sent(db, 'R-G', 119.00)
    _exec(db, 'UPDATE Invoices SET AmountDue = 5 WHERE ID = ?', inv)

    plan = rebuild_and_verify(db, inv, logged, ['recalc'], logs=False)

    assert _invoice_state(db, inv) == ('sent', 119)
    assert plan.actions[0].text == 'Offener Betrag 0,00 € → 119,00 €'                 # 5 Minor Units = ein Zwanzigstel Cent
    assert (plan.status_before, plan.status_after) == ('sent', 'sent') and plan.rows == []


def test_manual_paid_status_survives_a_rebuild(db, acct, logged):
    """Eine von Hand auf „bezahlt“ gesetzte Rechnung (Skonto) bleibt es – die Bausteine rechnen den Status zwischendurch neu,
    die Reparatur stellt ihn wieder her."""
    inv = _sent(db, 'R001', 119.00)
    bank = _bank(db, acct, 119.00)
    db.link_invoice_to_transaction(inv, bank, 100.00)
    _exec(db, "UPDATE Invoices SET Status = 'paid' WHERE ID = ?", inv)
    assert _invoice_state(db, inv) == ('paid', 19)

    plan = rebuild_and_verify(db, inv, logged, ['rebuild'])

    assert _invoice_state(db, inv) == ('paid', 19)
    assert [(row.account, row.amount, row.tax) for row in plan.rows] == [(1800, 1000000, None), (4405, 1000000, 159700)]
    assert announced(plan) == {4400: 84.03, 3806: 15.97}


def test_the_note_of_a_rebuilt_assignment_is_kept(db, acct, logged):
    inv, bank = linked_case(db, acct)
    _exec(db, "UPDATE InvoicePayments SET Notes = 'Skonto zugesagt'")

    rebuild_and_verify(db, inv, logged, ['rebuild'])

    assert _raw(db, 'SELECT Notes FROM InvoicePayments') == [('Skonto zugesagt',)]
    assert ('UPDATE InvoicePayments SET Notes = ? WHERE InvoiceID = ? AND BookingID = ?', ('Skonto zugesagt', inv, bank),
            'Rebuild: keep note of the assignment') in logged.entries


def test_two_assignments_of_one_invoice_are_rebuilt_one_after_the_other(db, acct, logged):
    """Der Steuer-Rest geht auf den Cent auf und landet wie beim Zuordnen nacheinander: Die ersten beiden Zuordnungen tragen ihren
    anteiligen Betrag (5,32 €), die dritte – sie schließt die Rechnung – bekommt, was fehlt (5,33 €). Alle Zuordnungen werden erst
    entfernt und dann in zeitlicher Folge neu gebucht; würde jede einzeln entfernt und sofort gebucht, bekäme die zuerst gebaute den
    Rest."""
    inv = _sent(db, 'R-TP', 100.00)
    banks = [_bank(db, acct, amount, date=f'2026-02-0{n}') for n, amount in enumerate((33.33, 33.33, 33.34), start=1)]
    for bank, amount in zip(banks, (33.33, 33.33, 33.34)):
        db.link_invoice_to_transaction(inv, bank, amount)

    plan = rebuild_and_verify(db, inv, logged, ['rebuild', 'rebuild', 'rebuild'])

    assert announced(plan) == {4400: 84.03, 3806: 15.97}
    assert sum(row.tax or 0 for row in plan.rows) == 159700
    assert [row.tax for row in plan.rows if row.tax is not None] == [53200, 53200, 53300]        # je Satz, nicht nur die Summe
    assert _invoice_state(db, inv) == ('paid', 0)


def test_a_zero_percent_invoice_gets_its_rows_without_tax(db, acct, logged):
    inv = _sent(db, 'R-0', 100.00, rate=0)
    bank = _bank(db, acct, 100.00)
    db.link_invoice_to_transaction(inv, bank, 100.00)

    plan = rebuild_and_verify(db, inv, logged, ['rebuild'])

    assert [(row.account, row.counter_account, row.tax, row.tax_rate) for row in plan.rows] == [
        (1800, 10000, None, None), (4340, 4185, None, None)]
    assert announced(plan) == {4185: 100.0}


def test_a_rebuilt_assignment_leaves_the_other_invoices_of_the_customer_alone(db, acct, logged):
    other = _sent(db, 'R-ANDERE', 50.00)
    _assign(db, other, _bank(db, acct, 50.00, date='2026-01-15'))
    inv, bank = linked_case(db, acct)
    other_state = (_raw(db, "SELECT * FROM Bookings WHERE DocumentNumber = 'R-ANDERE' ORDER BY ID"),
                   _raw(db, 'SELECT * FROM InvoicePayments WHERE InvoiceID = ?', other),
                   _raw(db, 'SELECT * FROM Invoices WHERE ID = ?', other))

    rebuild_and_verify(db, inv, logged, ['rebuild'])

    assert (_raw(db, "SELECT * FROM Bookings WHERE DocumentNumber = 'R-ANDERE' ORDER BY ID"),
            _raw(db, 'SELECT * FROM InvoicePayments WHERE InvoiceID = ?', other),
            _raw(db, 'SELECT * FROM Invoices WHERE ID = ?', other)) == other_state


# ── Die Steuer wie beim Zuordnen nacheinander ────────────────────────────────

@pytest.mark.parametrize('gross, parts, taxes', [
    pytest.param(100.00, (33.33, 33.33, 33.34), (53200, 53200, 53300), id='100-Euro-in-drei-Teilen'),
    pytest.param(10.00, (0.03, 0.03, 9.94), (0, 0, 16000), id='10-Euro-mit-Centbruchteilen'),
])
def test_every_rebuilt_assignment_carries_the_tax_of_assigning_one_after_the_other(db, acct, logged, gross, parts, taxes):
    """100 € zu 19 % (Steuer 15,97 €) in drei Teilen: Zuordnen nacheinander gibt 5,32 / 5,32 / 5,33 €, bei 10 € (Steuer 1,60 €) in
    0,03 / 0,03 / 9,94 € gibt es 0,00 / 0,00 / 1,60 €. Der Neuaufbau schreibt je Satz dasselbe – nicht nur dieselbe Summe."""
    inv = _sent(db, 'R-TP', gross)
    banks = [_bank(db, acct, amount, date=f'2026-02-0{n}') for n, amount in enumerate(parts, start=1)]
    for bank, amount in zip(banks, parts):
        db.link_invoice_to_transaction(inv, bank, amount)

    plan = rebuild_and_verify(db, inv, logged, ['rebuild'] * 3)

    assert [row.tax for row in plan.rows if row.tax is not None] == list(taxes)
    assert [tax for (tax,) in _raw(db, 'SELECT TaxAmount FROM Bookings WHERE Status = ? AND TaxAmount IS NOT NULL ORDER BY ID',
                                   PAYMENT_MARK)] == list(taxes)
    assert _invoice_state(db, inv) == ('paid', 0)


def test_the_order_follows_the_date_of_the_movements_and_not_the_previous_payment_date(db, acct):
    """Neu gebucht wird in der Reihenfolge der Bankbewegungen (ihr Datum, dann die ID der Zuordnung) – das Datum, das jede neue
    Zuordnung als Zahlungsdatum bekommt. Ein bisheriges Zahlungsdatum, das eine andere Reihenfolge nennt, ändert nichts: Die Steuer
    steht in jedem Satz wie nach dem Zuordnen in der Folge der Bewegungen (7,99 € und 7,98 €)."""
    inv = _sent(db, 'R-ORD', 100.00)
    b1, b2 = _bank(db, acct, 50.00, date='2026-02-01'), _bank(db, acct, 50.00, date='2026-03-01')
    _assign(db, inv, b1, 50.00)
    _assign(db, inv, b2, 50.00)
    taxes = "SELECT TaxAmount FROM Bookings WHERE Status = ? AND TaxAmount IS NOT NULL ORDER BY ParentBooking_ID"
    assert _raw(db, taxes, PAYMENT_MARK) == [(79900,), (79800,)]
    _exec(db, "UPDATE InvoicePayments SET PaymentDate = '2026-04-01' WHERE BookingID = ?", b1)   # das bisherige Datum kehrt sie um
    _exec(db, 'DELETE FROM Bookings WHERE Status = ?', PAYMENT_MARK)

    plan = dry(db, inv).plan
    assert [action.booking_id for action in plan.actions] == [b1, b2]
    assert run(db, inv, plan).executed is True

    assert _raw(db, taxes, PAYMENT_MARK) == [(79900,), (79800,)]
    assert _raw(db, 'SELECT BookingID, PaymentDate FROM InvoicePayments ORDER BY BookingID') == [(b1, '2026-02-01'), (b2, '2026-03-01')]


def test_movements_of_the_same_day_are_rebuilt_in_the_order_of_their_assignments(db, acct):
    """Zwei Bewegungen desselben Tages: Die Zuordnung mit der kleineren ID kommt zuerst, die andere schließt die Rechnung und bekommt
    den Rest der Steuer – gleich, in welcher Reihenfolge die Bewegungen angelegt wurden."""
    inv = _sent(db, 'R-DAY', 100.00)
    later, earlier = _bank(db, acct, 50.00, date='2026-02-01'), _bank(db, acct, 50.00, date='2026-02-01')
    db.link_invoice_to_transaction(inv, earlier, 50.00)
    db.link_invoice_to_transaction(inv, later, 50.00)

    plan = dry(db, inv).plan
    assert [action.booking_id for action in plan.actions] == [earlier, later]
    assert run(db, inv, plan).executed is True

    assert dict(_raw(db, "SELECT ParentBooking_ID, TaxAmount FROM Bookings WHERE Status = ? AND TaxAmount IS NOT NULL",
                     PAYMENT_MARK)) == {earlier: 79900, later: 79800}


def test_a_candidate_in_the_middle_that_does_not_fit_is_skipped_and_the_others_are_rebuilt(db, acct, logged):
    """Die mittlere Zuordnung (119,00 €) ist größer als ihre Bewegung (100,00 €): Der erste Durchgang geht ganz zurück, sie bleibt mit
    ihrer ID und ihrer Zeile liegen, und die erste und die dritte werden im zweiten Durchgang trotzdem gebaut – ohne etwas vom
    zurückgenommenen Durchgang im Replay-Log."""
    inv = _sent(db, 'R-MID', 300.00)
    banks = [_bank(db, acct, bank_amount, date=f'2026-02-0{n}') for n, bank_amount in enumerate((100.00, 100.00, 81.00), start=1)]
    for bank, amount in zip(banks, (100.00, 119.00, 81.00)):
        db.link_invoice_to_transaction(inv, bank, amount)
    first, middle, last = _raw(db, 'SELECT * FROM InvoicePayments ORDER BY ID')

    plan = rebuild_and_verify(db, inv, logged, ['rebuild', 'rebuild'])

    assert plan.skipped == [text_of('not_free', payment=middle[0], booking=banks[1])]
    assert [(action.payment_id, action.booking_id) for action in plan.actions] == [(first[0], banks[0]), (last[0], banks[2])]
    assert _raw(db, 'SELECT * FROM InvoicePayments WHERE ID = ?', middle[0]) == [middle]          # dieselbe Zeile, dieselbe ID
    assert [len(_children(db, bank)) for bank in banks] == [2, 0, 2]
    assert [description for _sql, _params, description in logged.entries] == (
        ['Rebuild: remove assignment'] * 2 + ['Link invoice to booking', 'Payment child (bank to debtor)',
                                              'Payment child (open item to revenue)'] * 2)
    assert [params for sql, params, _description in logged.entries if sql.startswith('DELETE')] == [(first[0],), (last[0],)]
    # die übergangene Zuordnung zählt als bezahlt: Ihr anteiliger Betrag (19,00 €) fehlt in der Steuer der neuen Sätze
    assert sum(row.tax for row in plan.rows if row.tax is not None) == 479000 - 190000


@pytest.mark.parametrize('old, tail', [
    pytest.param('2026-02-15', ' (bisher 2026-02-15).', id='ISO-Datum'),
    pytest.param('15.02.2026', '.', id='anderes-Format'),
    pytest.param('2026-02-15 10:00:00', '.', id='mit-Uhrzeit'),
    pytest.param(HOSTILE, '.', id='Markup'),
])
def test_a_payment_date_that_differs_from_the_movement_is_named_in_the_action(db, acct, logged, old, tail):
    """Die neue Zuordnung trägt das Datum der Bankbewegung (``_assign_in_tx``). Weicht das bisherige Zahlungsdatum ab, sagt es der
    Text der Aktion – das alte Datum nur, wenn es ein ISO-Datum ist, nie ein anderer Wert aus der Datenbank."""
    inv, bank = linked_case(db, acct)
    _exec(db, 'UPDATE InvoicePayments SET PaymentDate = ?', old)

    plan = rebuild_and_verify(db, inv, logged, ['rebuild'])

    assert plan.actions[0].text == (
        f'Zuordnung #1 (119,00 €) zu Bewegung #{bank} vom 2026-02-01 wird mit den Sätzen „Bank an Debitoren“ und „Wartekonto an '
        'Erlöskonto“ neu geschrieben. Das Zahlungsdatum der Zuordnung wird das Datum der Bankbewegung' + tail)
    assert (old in plan.actions[0].text) is tail.startswith(' (bisher')              # nur ein ISO-Datum wird genannt
    assert _raw(db, 'SELECT PaymentDate FROM InvoicePayments') == [('2026-02-01',)]


# ── Storno-Paare: nur der offene Betrag ──────────────────────────────────────

def corrupted_pair(world):
    """R1 (50,00 € gezahlt) ist storniert, die Erstattung ist zugeordnet; dann wird der offene Rest verfälscht und der
    Zahlungssatz der Zahlung per SQL gelöscht – ein Bestand, in dem die Zuordnung keine Sätze hat."""
    db = world.db
    r1 = world_sent(world, 'T-100')
    booking = pay(world, r1, 50.0)
    storno = cancel(world, r1, '2026-07-02')
    out = db.insert_booking('2026-07-10', -50.0, booking_type='bank', account_id=world.bank, recipient_client='Malermeister Pinsel')
    assert db.assign_storno_refund(storno.storno_id, out).ok
    return SimpleNamespace(db=db, r1=r1, s1=storno.storno_id, booking=booking, out=out)


def test_amount_due_of_a_storno_pair_is_recalculated_only(world):
    s = corrupted_pair(world)
    db = s.db
    _exec(db, "DELETE FROM Bookings WHERE ParentBooking_ID = ? AND Status = 'invoice_payment' AND DocumentNumber = 'T-100'",
          s.booking)
    _exec(db, 'UPDATE Invoices SET AmountDue = 1 WHERE ID = ?', s.r1)
    assert 'no_settlement' in {f['category'] for f in db.find_payment_inconsistencies(s.r1)}
    tables_before = all_tables(db)
    bookings_before, payments_before = tables_before['Bookings'], tables_before['InvoicePayments']

    plan = dry(db, s.r1).plan
    assert kinds(plan) == ['recalc'] and plan.skipped == [text_of('storno_pair')]
    assert all_tables(db) == tables_before                                              # der Probelauf schreibt nichts
    result = run(db, s.r1, plan)

    assert (result.ok, result.executed) == (True, True)
    after = all_tables(db)
    assert after['Bookings'] == bookings_before and after['InvoicePayments'] == payments_before
    assert [name for name in tables_before if after[name] != tables_before[name]] == ['Invoices']
    assert _raw(db, 'SELECT Status, AmountDue FROM Invoices WHERE ID = ?', s.r1) == [('cancelled', 690000)]
    assert 'no_settlement' in [category for category, _text in plan.findings_after]


def test_a_storno_pair_with_a_refund_has_nothing_to_rebuild(world):
    s = corrupted_pair(world)
    assert s.db.find_payment_inconsistencies() == []
    for invoice_id in (s.r1, s.s1):
        plan = nothing_happens(s.db, invoice_id, [text_of('storno_pair')])
        assert plan.actions == [] and plan.rows == []


# ── Nur gemeldet: jede Bedingung einzeln ─────────────────────────────────────

def test_payment_on_an_entry_is_only_reported(db, acct):
    inv = _sent(db, 'R-D', 119.00)
    entry = db.insert_booking('2026-02-04', 119.00, coa_id=_coa(db, 4400), booking_type='entry')
    db.link_invoice_to_transaction(inv, entry, 119.00)
    payment = _raw(db, 'SELECT ID FROM InvoicePayments')[0][0]

    plan = nothing_happens(db, inv, [text_of('not_bank', payment=payment)])

    assert [category for category, _text in plan.findings_after] == ['payment_on_entry']


def test_an_assignment_on_an_outgoing_payment_is_only_reported(db, acct):
    inv = _sent(db, 'R-F', 119.00)
    db.link_invoice_to_transaction(inv, _bank(db, acct, -119.00, date='2026-02-06'), 119.00)
    payment = _raw(db, 'SELECT ID FROM InvoicePayments')[0][0]

    plan = nothing_happens(db, inv, [text_of('outgoing', payment=payment)])

    assert 'outgoing_payment' in [category for category, _text in plan.findings_after]


def test_an_unsent_invoice_is_only_reported(db, acct):
    inv = _sent(db, 'R-H', 119.00, status='finalized')
    assert [f['category'] for f in db.find_payment_inconsistencies(inv)] == ['status_unsent']

    plan = nothing_happens(db, inv, [text_of('status')])

    assert [category for category, _text in plan.findings_after] == ['status_unsent']


def test_a_credit_note_without_a_positive_amount_is_only_reported(db, acct):
    inv = _invoice(db, 'G-1', -50.00, rate=0.19, status='sent')
    nothing_happens(db, inv, [text_of('amount')])


def test_a_row_without_a_mark_and_without_an_assignment_is_left_alone(db, acct):
    inv = _sent(db, 'R-C', 119.00)
    bank = _bank(db, acct, 119.00, date='2026-02-03')
    db.insert_booking('2026-02-03', 119.00, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000), booking_type='entry',
                      parent_booking_id=bank, text='Zahlung zu Re. R-C', document_number='R-C')

    plan = nothing_happens(db, inv, [])

    assert [category for category, _text in plan.findings_after] == ['orphan_payment_rows']


def test_a_marked_row_without_an_assignment_is_left_alone(db, acct):
    """Ob die Zuordnung oder die Sätze verloren gingen, weiß die App nicht: Sie rät nicht."""
    inv = _sent(db, 'R001', 119.00)
    _assign(db, inv, _bank(db, acct, 119.00))
    _exec(db, 'DELETE FROM InvoicePayments')

    plan = nothing_happens(db, inv, [], ['recalc'], execute=False)

    assert 'orphan_payment_rows' in [category for category, _text in plan.findings_after]
    assert [row for row in _raw(db, 'SELECT Status FROM Bookings WHERE Status = ?', PAYMENT_MARK)] == [(PAYMENT_MARK,)] * 2


def test_a_missing_receivable_is_only_reported(db, acct):
    inv = _sent(db, 'R-NOREC', 119.00)
    _assign(db, inv, _bank(db, acct, 119.00))
    db.delete_transaction(db.get_receivable_booking(inv))

    plan = nothing_happens(db, inv, [])

    assert [category for category, _text in plan.findings_after] == ['settlement_without_receivable']


def test_a_wiso_like_receivable_is_not_the_apps_and_nothing_is_written(db, acct):
    """Sätze eines Imports und eine Forderung, die die App nicht gebucht hat, sind nie ihre: nur gemeldet. Mit der Quellklammer
    am Satz bleibt die Überweisung ganz außen vor, ohne sie nennt der Plan den Grund."""
    banked, cash, nameless, row = _wiso_like_books(db, acct)
    for inv in (banked, cash, nameless):
        assert dry(db, inv).plan.actions == [] and dry(db, inv).plan.skipped == []
    _exec(db, 'UPDATE Bookings SET SourceGroup = NULL WHERE ID = ?', row)

    nothing_happens(db, nameless, [text_of('receivable')])


def test_a_hand_made_row_without_a_number_under_the_movement_is_not_touched(db, acct):
    inv = _sent(db, 'R001', 119.00)
    bank = _bank(db, acct, 119.00)
    db.insert_booking('2026-02-01', 119.00, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1800), booking_type='entry',
                      parent_booking_id=bank, text='von Hand')
    db.link_invoice_to_transaction(inv, bank, 119.00)

    nothing_happens(db, inv, [text_of('foreign_rows', booking=bank)])


def test_rows_of_a_row_that_belongs_to_a_foreign_invoice_stop_the_rebuild(db, acct):
    """Ein Satz unter der Bewegung mit der Nummer einer Rechnung, der die Bewegung NICHT zugeordnet ist, gehört niemandem hier."""
    inv = _sent(db, 'R001', 119.00)
    _sent(db, 'R-FREMD', 50.00)
    bank = _bank(db, acct, 119.00)
    unmarked_pair(db, bank, 'R-FREMD', 50.00, 7.98)
    db.link_invoice_to_transaction(inv, bank, 119.00)

    nothing_happens(db, inv, [text_of('foreign_rows', booking=bank)])


def test_rows_of_the_same_invoice_under_the_movement_are_never_rewritten(db, acct):
    """Eigene Sätze (hier nur ein Einzelsatz, ohne „Bank an Debitoren“) werden weder ergänzt noch umgeschrieben."""
    inv = _sent(db, 'R001', 119.00)
    bank = _bank(db, acct, 119.00)
    db.insert_booking('2026-02-01', 119.00, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1800), booking_type='entry',
                      parent_booking_id=bank, document_number='R001', text='Einzelsatz')
    db.link_invoice_to_transaction(inv, bank, 119.00)
    assert 'no_settlement' in {f['category'] for f in db.find_payment_inconsistencies(inv)}

    nothing_happens(db, inv, [text_of('own_rows', booking=bank)])


def test_a_bank_account_without_an_skr_number_is_only_reported(db):
    account = _account(db, name='Konto ohne Nummer', skr=None)
    inv, bank = linked_case(db, account)

    nothing_happens(db, inv, [text_of('no_skr', booking=bank)])


def test_two_different_amounts_for_one_pair_are_only_reported(db, acct):
    inv = _sent(db, 'R001', 119.00)
    bank = _bank(db, acct, 119.00)
    drop_unique_index(db)
    raw_payment(db, inv, bank, 500000)
    raw_payment(db, inv, bank, 690000)
    _exec(db, 'UPDATE Invoices SET AmountDue = 0 WHERE ID = ?', inv)

    plan = nothing_happens(db, inv, [text_of('duplicate_amounts', booking=bank)])

    assert 'duplicate_payment' in [category for category, _text in plan.findings_after]


def test_an_amount_with_a_fraction_of_a_cent_is_only_reported(db, acct):
    inv = _sent(db, 'R001', 120.00)
    bank = _bank(db, acct, 120.00)
    payment = raw_payment(db, inv, bank, 1190050)                             # 119,005 €
    _exec(db, 'UPDATE Invoices SET AmountDue = 9950 WHERE ID = ?', inv)

    nothing_happens(db, inv, [text_of('cent', payment=payment, booking=bank)])


def test_an_assignment_larger_than_its_movement_keeps_its_id_and_is_reported(db, acct):
    """Bank 100,00 €, Zuordnung 119,00 €: Neu buchen ginge nur über 100,00 €. Der Savepoint nimmt Zuordnung samt Zeile zurück."""
    inv, bank = linked_case(db, acct, bank_amount=100.00)
    stored = _raw(db, 'SELECT * FROM InvoicePayments')
    payment = stored[0][0]

    plan = nothing_happens(db, inv, [text_of('not_free', payment=payment, booking=bank)])

    assert _raw(db, 'SELECT * FROM InvoicePayments') == stored and plan.rows == []


def test_a_candidate_that_does_not_fit_is_rolled_back_and_the_next_one_is_still_rebuilt(db, acct, logged):
    """Die erste Zuordnung (119,00 €) ist größer als ihre Bewegung (100,00 €): Sie bleibt mit ihrer ID und ihrer Zeile liegen, ihre
    Einträge fehlen im Log; die zweite (19,00 €) wird trotzdem neu gebaut."""
    inv = _sent(db, 'R001', 238.00)
    small, large = _bank(db, acct, 100.00, date='2026-02-01'), _bank(db, acct, 19.00, date='2026-02-02')
    db.link_invoice_to_transaction(inv, small, 119.00)
    db.link_invoice_to_transaction(inv, large, 19.00)
    first, second = _raw(db, 'SELECT * FROM InvoicePayments ORDER BY ID')

    plan = rebuild_and_verify(db, inv, logged, ['rebuild'])

    assert plan.skipped == [text_of('not_free', payment=first[0], booking=small)]
    assert plan.actions[0].payment_id == second[0] and plan.actions[0].booking_id == large
    assert _raw(db, 'SELECT * FROM InvoicePayments WHERE ID = ?', first[0]) == [first]           # dieselbe Zeile, dieselbe ID
    assert _children(db, small) == [] and len(_children(db, large)) == 2
    assert [d for _s, _p, d in logged.entries] == ['Rebuild: remove assignment', 'Link invoice to booking',
                                                   'Payment child (bank to debtor)', 'Payment child (open item to revenue)']
    assert logged.entries[0][1] == (second[0],)                   # nichts vom zurückgenommenen Versuch steht im Log


def test_marked_rows_of_another_assigned_invoice_do_not_block_the_rebuild(db, acct, logged):
    bank = _bank(db, acct, 200.00)
    first, second = _sent(db, 'R001', 119.00), _sent(db, 'R002', 81.00)
    _assign(db, first, bank)
    db.link_invoice_to_transaction(second, bank, 81.00)

    plan = rebuild_and_verify(db, second, logged, ['rebuild'])

    assert announced(plan) == {4400: round(81 / 1.19, 2), 3806: round(81 - round(81 / 1.19, 2), 2)}


def test_rows_with_a_source_group_under_the_movement_stop_the_rebuild(db, acct):
    """Sätze eines Imports (``SourceGroup``) gehören nie der App: Unter so einer Bewegung wird nichts neu geschrieben – auch dann
    nicht, wenn alles andere passt (die Sätze gehören einer anderen zugeordneten Rechnung)."""
    bank = _bank(db, acct, 357.00)
    r1, r2 = _sent(db, 'R001', 119.00), _sent(db, 'R002', 238.00)
    _assign(db, r1, bank)
    _exec(db, "UPDATE Bookings SET SourceGroup = 'WISO-1' WHERE ParentBooking_ID = ?", bank)
    db.link_invoice_to_transaction(r2, bank, 238.00)
    assert db.find_payment_inconsistencies(r2) == []                       # die Prüfliste lässt Bewegungen mit Import-Sätzen aus

    nothing_happens(db, r2, [])


def test_an_attached_receivable_of_an_import_is_never_detached(db, acct):
    inv = _sent(db, 'R001', 119.00)
    bank = _bank(db, acct, 119.00)
    receivable = db.get_receivable_booking(inv)
    _exec(db, "UPDATE Bookings SET SourceGroup = 'WISO-1' WHERE ID = ?", receivable)
    _adopt(db, receivable, bank)
    assert [f['category'] for f in db.find_payment_inconsistencies(inv)] == ['receivable_attached']

    nothing_happens(db, inv, [])

    assert _raw(db, 'SELECT ParentBooking_ID, Status, SourceGroup FROM Bookings WHERE ID = ?', receivable) == [(bank, 'resolved', 'WISO-1')]


def test_two_receivables_of_one_invoice_stop_the_rebuild(db, acct):
    inv, bank = linked_case(db, acct)
    receivable = db.get_receivable_booking(inv)
    _exec(db, 'INSERT INTO Bookings (DateBooking, RecipientClient, COA_ID, CounterCOA_ID, Amount, Currency, TaxRate, TaxAmount, Text, '
              "DocumentNumber, BookingType, AutoMirror, EntryForm) SELECT DateBooking, RecipientClient, COA_ID, CounterCOA_ID, Amount, "
              "Currency, TaxRate, TaxAmount, Text, DocumentNumber, BookingType, AutoMirror, EntryForm FROM Bookings WHERE ID = ?", receivable)

    nothing_happens(db, inv, [text_of('receivable')])


def test_without_a_receivable_there_is_no_finding_and_nothing_to_rebuild(db, acct):
    """Ein Steuersatz ohne Wartekonto bucht beim Versenden keine Forderung – und die Reparatur braucht dafür kein Erlöskonto."""
    inv = _sent(db, 'R-5', 105.00, rate=0.05)
    db.link_invoice_to_transaction(inv, _bank(db, acct, 105.00), 105.00)
    assert db.find_payment_inconsistencies(inv) == [] and db.get_receivable_booking(inv) is None
    accounts = _raw(db, 'SELECT * FROM ChartOfAccounts ORDER BY ID')

    nothing_happens(db, inv, [])

    assert _raw(db, 'SELECT * FROM ChartOfAccounts ORDER BY ID') == accounts


def test_the_repair_of_one_invoice_on_a_large_stock_finds_exactly_that_one_defect(db, acct):
    """1500 Rechnungen, 1500 Bewegungen, 400 Zuordnungen, ein verlorener Satz: Der Plan nennt genau die eine Zuordnung, nichts
    sonst – die anderen Rechnungen, ihre Sätze und der Rest des Bestands stören nicht. (Keine Zeitgrenze: Der Test hängt nicht an der
    Geschwindigkeit der Maschine.)"""
    _synthetic_books(db, acct, invoices=1500, banks=1500, assigned=400)
    _exec(db, "DELETE FROM Bookings WHERE DocumentNumber = 'R00007' AND Status = 'invoice_payment'")
    inv = _raw(db, "SELECT ID FROM Invoices WHERE InvoiceNumber = 'R00007'")[0][0]

    plan = dry(db, inv).plan

    assert kinds(plan) == ['rebuild'] and plan.skipped == []


# ── Nichts wird doppelt gebucht: Sätze der Rechnung an anderer Stelle ────────
#
# Jeder Fall: Rechnung 119 € (19 %) mit Forderung der App, die Zuordnung ohne Sätze hängt an einer Bewegung – und Zahlung oder Erlös
# der Rechnung stehen schon an anderer Stelle in den Büchern. Neu aufgebaut stünde der Erlös zweimal da: Der Plan nennt den Grund
# ``elsewhere`` und baut nichts (``assert_elsewhere_stops_the_rebuild``).

def test_marked_rows_under_another_movement_stop_the_rebuild(db, acct):
    """Zahlung und Erlös stehen unter einer ANDEREN Bewegung (B1: Die Zuordnung dort wurde entfernt, die Sätze blieben)."""
    inv = _sent(db, 'R-30', 119.00)
    b1 = _bank(db, acct, 119.00, date='2026-03-01')
    _assign(db, inv, b1)
    _exec(db, 'DELETE FROM InvoicePayments')
    db.link_invoice_to_transaction(inv, _bank(db, acct, 119.00, date='2026-03-02'), 119.00)
    assert 'orphan_payment_rows' in {f['category'] for f in db.find_payment_inconsistencies(inv)}
    assert _euer(db)[4400] == 100.0                                              # der Erlös steht schon in der EÜR

    assert_elsewhere_stops_the_rebuild(db, inv)


def test_a_single_unmarked_revenue_row_under_another_movement_stops_the_rebuild(db, acct):
    """Der Einzelsatz „Erlöskonto an Bank“ mit der Rechnungsnummer, ohne Marke (Altbestand „Zahlung verbuchen mit Konto“)."""
    inv = _sent(db, 'R-31', 119.00)
    b1 = _bank(db, acct, 119.00, date='2026-03-01')
    db.insert_booking('2026-03-01', 119.00, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1800), booking_type='entry',
                      parent_booking_id=b1, document_number='R-31', tax_rate=0.19, tax_amount=19.00, text='Erlös')
    db.link_invoice_to_transaction(inv, _bank(db, acct, 119.00, date='2026-03-02'), 119.00)
    assert _euer(db)[4400] == 100.0

    assert_elsewhere_stops_the_rebuild(db, inv)


def test_a_standalone_cash_row_with_the_number_stops_the_rebuild(db, acct):
    """Die Rechnung wurde bar bezahlt und als eigenständiger Satz „Erlöskonto an Kasse“ gebucht; später kam eine Bankbewegung."""
    inv = _sent(db, 'R-32', 119.00)
    db.insert_booking('2026-03-01', 119.00, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1600), booking_type='entry',
                      document_number='R-32', tax_rate=0.19, tax_amount=19.00, text='bar bezahlt')
    db.link_invoice_to_transaction(inv, _bank(db, acct, 119.00, date='2026-03-02'), 119.00)
    assert _euer(db)[4400] == 100.0

    assert_elsewhere_stops_the_rebuild(db, inv)


def test_standalone_rows_without_a_source_group_stop_the_rebuild(db, acct):
    """„Bank an Debitoren“ und „Wartekonto an Erlöskonto“ als eigenständige Sätze (aus einem Textimport oder von Hand)."""
    inv = _sent(db, 'R-33', 119.00)
    db.insert_booking('2026-03-01', 119.00, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000), booking_type='entry',
                      document_number='R-33', text='Zahlung R-33')
    db.insert_booking('2026-03-01', 119.00, coa_id=_coa(db, 4405), counter_coa_id=_coa(db, 4400), booking_type='entry',
                      document_number='R-33', tax_rate=0.19, tax_amount=19.00, text='Umb. R-33')
    db.link_invoice_to_transaction(inv, _bank(db, acct, 119.00, date='2026-03-02'), 119.00)
    assert _balance(db, 4400) == Decimal(-119)                                   # der Erlös steht schon auf dem Erlöskonto

    assert_elsewhere_stops_the_rebuild(db, inv)


def test_standalone_rows_with_a_source_group_stop_the_rebuild(db, acct):
    """Sätze eines Imports (``SourceGroup``), die der Nutzer im Editor von der Bewegung abgehängt hat: Sie bleiben eigenständig
    stehen – und gehören trotzdem zur Rechnung."""
    inv = _sent(db, 'R-35', 119.00)
    bank = _bank(db, acct, 119.00, date='2026-03-01')
    settlement = db.insert_booking('2026-03-01', 119.00, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000),
                                   booking_type='entry', parent_booking_id=bank, document_number='R-35')
    transfer = db.insert_booking('2026-03-01', 119.00, coa_id=_coa(db, 4405), counter_coa_id=_coa(db, 4400),
                                 booking_type='entry', parent_booking_id=bank, document_number='R-35', tax_rate=0.19,
                                 tax_amount=19.00)
    for row in (settlement, transfer):
        _exec(db, "UPDATE Bookings SET SourceGroup = 'WISO-8' WHERE ID = ?", row)
    db.link_invoice_to_transaction(inv, bank, 119.00)
    db.remove_split_row(settlement)
    db.remove_split_row(transfer)
    assert _raw(db, "SELECT ParentBooking_ID FROM Bookings WHERE SourceGroup = 'WISO-8' ORDER BY ID") == [(None,), (None,)]
    assert _children(db, bank) == []

    assert_elsewhere_stops_the_rebuild(db, inv)


@pytest.mark.parametrize('spelling', [' r-30 ', 'r-30', 'R-30  '], ids=['klein-mit-Leerraum', 'klein', 'Leerraum-hinten'])
def test_the_number_in_another_spelling_on_a_standalone_row_stops_the_rebuild(db, acct, spelling):
    """Verglichen wird die Nummer ohne Leerraum am Rand und ohne Rücksicht auf die Schreibweise. Die Prüfliste sieht so einen Satz
    nicht (sie ordnet über die genaue Nummer zu) – nur dieser Vergleich fängt ihn."""
    inv = _sent(db, 'R-30', 119.00)
    db.insert_booking('2026-03-01', 119.00, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1600), booking_type='entry',
                      document_number=spelling, tax_rate=0.19, tax_amount=19.00, text='Erlös')
    db.link_invoice_to_transaction(inv, _bank(db, acct, 119.00, date='2026-03-02'), 119.00)
    assert 'orphan_payment_rows' not in {f['category'] for f in db.find_payment_inconsistencies(inv)}
    assert _euer(db)[4400] == 100.0

    assert_elsewhere_stops_the_rebuild(db, inv)


def test_an_orphan_finding_of_the_check_list_alone_stops_the_rebuild(db, acct, monkeypatch):
    """Das Netz für Sätze, die die Prüfliste der Rechnung zurechnet. Mit echten Sätzen lässt sich ein Fall, den der Vergleich der
    Nummer nicht schon findet, nicht bauen – die Prüfliste ordnet über dieselbe Nummer zu –, deshalb meldet hier eine erweiterte
    Prüfliste den Befund ``orphan_payment_rows``."""
    inv, bank = linked_case(db, acct)
    real = Database._payment_findings

    def with_orphan(self, conn, invoice_id):
        return real(self, conn, invoice_id) + [{'category': 'orphan_payment_rows', 'invoice_id': invoice_id,
                                                'invoice_number': 'R001', 'text': 'Zahlungssatz #99 hat keine Zuordnung.'}]

    monkeypatch.setattr(Database, '_payment_findings', with_orphan)

    plan = assert_elsewhere_stops_the_rebuild(db, inv)

    assert 'orphan_payment_rows' in [category for category, _text in plan.findings_before]


def test_rows_under_a_movement_the_invoice_is_assigned_to_do_not_stop_the_rebuild(db, acct, logged):
    """Das Gegenstück: Die Sätze der Rechnung stehen unter B1, und die Rechnung ist B1 zugeordnet (gesund). B2 hängt ohne Sätze an
    ihr – B2 wird aufgebaut, B1 bleibt, wie es ist."""
    inv = _sent(db, 'R-40', 119.00)
    b1, b2 = _bank(db, acct, 50.00, date='2026-03-01'), _bank(db, acct, 69.00, date='2026-03-02')
    _assign(db, inv, b1, 50.00)
    db.link_invoice_to_transaction(inv, b2, 69.00)
    healthy = (_raw(db, 'SELECT * FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', b1),
               _raw(db, 'SELECT * FROM InvoicePayments WHERE BookingID = ?', b1))

    plan = rebuild_and_verify(db, inv, logged, ['rebuild'])

    assert plan.skipped == [] and [action.booking_id for action in plan.actions] == [b2]
    assert (_raw(db, 'SELECT * FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', b1),
            _raw(db, 'SELECT * FROM InvoicePayments WHERE BookingID = ?', b1)) == healthy
    assert len(_children(db, b2)) == 2


# ── Nichts wird doppelt gebucht: mehr Erlös gebucht, als die Zuordnungen erklären ─────────────────────
#
# Hier stehen alle Sätze mit der Nummer der Rechnung unter Bewegungen, denen sie zugeordnet ist – ``elsewhere`` greift nicht. Sie
# buchen aber mehr Erlös, als die übrigen Zuordnungen erklären (je Rechnung: ``overbooked``) oder als die Zuordnungen der anderen
# Rechnungen unter derselben Bewegung (``overbooked_movement``): Neu aufgebaut stünde der Erlös zweimal da. Der Plan nennt den Grund,
# baut nichts, und Probelauf wie Ausführung ändern keine Tabelle.

def test_a_row_that_books_more_than_its_assignment_explains_stops_the_rebuild(db, acct):
    """Die Rechnung (119,00 €, 19 %) ist B1 (119,00 €) nur mit 50,00 € zugeordnet, unter B1 steht aber der Einzelsatz „Erlöskonto an
    Bank“ über die ganzen 119,00 €; B2 (69,00 €) hängt ohne Sätze an ihr. Der Satz steht unter einer zugeordneten Bewegung
    (``elsewhere`` greift nicht), bucht aber 119,00 € Erlös gegen 50,00 € Zuordnung: B2 neu zu bauen ließe 119,00 + 69,00 € Erlös
    zu einer Rechnung über 119,00 € stehen. Der Plan nennt ``overbooked`` und baut nichts."""
    inv = _sent(db, 'R-50', 119.00)
    b1 = _bank(db, acct, 119.00, date='2026-03-01')
    revenue_row(db, b1, 'R-50', 119.00, 19.00)
    db.link_invoice_to_transaction(inv, b1, 50.00)
    db.link_invoice_to_transaction(inv, _bank(db, acct, 69.00, date='2026-03-02'), 69.00)
    assert 'orphan_payment_rows' not in {f['category'] for f in db.find_payment_inconsistencies(inv)}
    euer = _euer(db)
    assert euer[4400] == 100.0                                                   # der Erlös steht schon in der EÜR

    plan = nothing_happens(db, inv, [text_of('own_rows', booking=b1), text_of('overbooked')])

    assert plan.rows == [] and _euer(db) == euer


def test_duplicates_with_their_own_rows_are_merged_and_nothing_is_rebuilt_on_top(db, acct, logged):
    """B1 (100,00 €) ist der Rechnung (119,00 €) zweimal mit 50,00 € zugeordnet (ein Bestand aus der Zeit vor dem eindeutigen Index),
    jede Zuordnung mit ihrem eigenen Satzpaar; B2 (69,00 €) hängt ohne Sätze an ihr. A fasst die doppelte Zuordnung zusammen – die
    Sätze bleiben, keiner wird gelöscht –, danach erklärt nur noch eine Zuordnung (50,00 €) die Sätze, die 100,00 € Erlös buchen: B2
    bekäme eine Steuer, die zu keiner Zuordnung nacheinander passt (3,04 € statt 11,02 €). Der Plan fasst zusammen und nennt
    ``overbooked``; B2 bleibt, wie es ist."""
    inv = _sent(db, 'R-53', 119.00)
    b1, b2 = _bank(db, acct, 100.00, date='2026-03-01'), _bank(db, acct, 69.00, date='2026-03-02')
    _assign(db, inv, b1, 50.00)
    db.link_invoice_to_transaction(inv, b2, 69.00)
    drop_unique_index(db)
    columns = ', '.join(row[1] for row in _raw(db, 'PRAGMA table_info(Bookings)') if row[1] != 'ID')
    _exec(db, f'INSERT INTO Bookings ({columns}) SELECT {columns} FROM Bookings WHERE ParentBooking_ID = ?', b1)
    raw_payment(db, inv, b1, 500000, day='2026-03-01')                            # dieselbe Zuordnung noch einmal
    assert len(_children(db, b1)) == 4 and _children(db, b2) == [] and _invoice_state(db, inv) == ('paid', 0)
    rows_before = _raw(db, 'SELECT * FROM Bookings ORDER BY ID')

    plan = rebuild_and_verify(db, inv, logged, ['dedupe'])

    assert plan.skipped == [text_of('overbooked')] and plan.rows == []
    assert _raw(db, 'SELECT COUNT(*) FROM InvoicePayments') == [(2,)]              # eine Zuordnung weniger
    assert _raw(db, 'SELECT * FROM Bookings ORDER BY ID') == rows_before          # kein Satz gelöscht, geändert oder neu
    assert _children(db, b2) == []
    assert text_of('overbooked') in dry(db, inv).plan.skipped                      # und auch danach baut niemand B2 auf


def test_a_collective_row_that_covers_more_than_the_other_invoice_stops_the_rebuild(db, acct):
    """Die Sammelzahlung B (238,00 €) ist R-61 und R-62 (je 119,00 €) zugeordnet; unter B steht nur die Umbuchung „Wartekonto an
    Erlöskonto“ über 238,00 € mit der Nummer R-61. Für R-62 sieht es aus wie ein Satz einer anderen zugeordneten Rechnung – er bucht
    aber 238,00 € Erlös gegen die 119,00 €, die R-61 dort zugeordnet sind. R-62 aufzubauen ließe 238,00 + 119,00 € Erlös stehen: Der
    Plan nennt ``overbooked_movement`` und baut nichts. (Das Gegenstück, die gesunde Sammelzahlung mit dem Satzpaar über 119,00 €, ist
    ``test_collective_payment_second_invoice_is_rebuilt``.)"""
    r1, r2 = _sent(db, 'R-61', 119.00), _sent(db, 'R-62', 119.00)
    bank = _bank(db, acct, 238.00, date='2026-03-01')
    db.insert_booking('2026-03-01', 238.00, coa_id=_coa(db, 4405), counter_coa_id=_coa(db, 4400), booking_type='entry',
                      parent_booking_id=bank, document_number='R-61', tax_rate=0.19, tax_amount=38.00, text='Umb. R-61 und R-62')
    db.link_invoice_to_transaction(r1, bank, 119.00)
    db.link_invoice_to_transaction(r2, bank, 119.00)
    euer = _euer(db)
    assert euer[4400] == 200.0                                                   # R-61 trägt schon den Erlös beider Rechnungen

    plan = nothing_happens(db, r2, [text_of('overbooked_movement', booking=bank)])

    assert plan.rows == [] and _euer(db) == euer


def test_a_row_that_books_what_its_assignment_explains_does_not_stop_the_rebuild(db, acct, logged):
    """Das Gegenstück zum Einzelsatz über die ganzen 119,00 €: B1 (119,00 €) trägt den Einzelsatz „Erlöskonto an Bank“ über 50,00 € und
    ist der Rechnung mit 50,00 € zugeordnet – gebucht und erklärt ist gleich viel. B2 (69,00 €) hängt ohne Sätze an ihr und wird
    aufgebaut, B1 bleibt, wie es ist."""
    inv = _sent(db, 'R-51', 119.00)
    b1 = _bank(db, acct, 119.00, date='2026-03-01')
    revenue_row(db, b1, 'R-51', 50.00, 7.98)
    db.link_invoice_to_transaction(inv, b1, 50.00)
    b2 = _bank(db, acct, 69.00, date='2026-03-02')
    db.link_invoice_to_transaction(inv, b2, 69.00)
    healthy = _raw(db, 'SELECT * FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', b1)

    plan = rebuild_and_verify(db, inv, logged, ['rebuild'])

    assert plan.skipped == [text_of('own_rows', booking=b1)] and [action.booking_id for action in plan.actions] == [b2]
    assert announced(plan) == {4400: 57.98, 3806: 11.02}                         # die Steuer des schließenden Teils: der Rest
    assert _raw(db, 'SELECT * FROM Bookings WHERE ParentBooking_ID = ? ORDER BY ID', b1) == healthy and len(_children(db, b2)) == 2


def test_a_row_with_an_amount_that_is_no_number_stops_the_rebuild(db, acct):
    """Der Betrag eines Satzes mit Erlöskonto und der Nummer der Rechnung ist keine Zahl (``'abc'`` – nur die SQL-Konsole schreibt so
    etwas): Wie viel Erlös er bucht, ist unbekannt. Der Plan nennt ``overbooked`` und baut nichts."""
    inv = _sent(db, 'R-52', 119.00)
    b1 = _bank(db, acct, 119.00, date='2026-03-01')
    row = revenue_row(db, b1, 'R-52', 50.00, 7.98)
    db.link_invoice_to_transaction(inv, b1, 50.00)
    db.link_invoice_to_transaction(inv, _bank(db, acct, 69.00, date='2026-03-02'), 69.00)
    _exec(db, "UPDATE Bookings SET Amount = 'abc' WHERE ID = ?", row)
    assert _raw(db, 'SELECT typeof(Amount) FROM Bookings WHERE ID = ?', row) == [('text',)]

    plan = nothing_happens(db, inv, [text_of('own_rows', booking=b1), text_of('overbooked')])

    assert plan.rows == []


@pytest.mark.parametrize('spelling', ['R-30\t', '\nR-30', 'R-30 ', ' r-30 ', '\u00a0R-30'],
                         ids=['Tabulator-hinten', 'Zeilenumbruch-vorn', 'Leerzeichen-hinten', 'klein-mit-Leerzeichen',
                              'geschütztes-Leerzeichen-vorn'])
def test_whitespace_at_the_edge_of_the_number_is_taken_as_str_strip_takes_it(db, acct, spelling):
    """Ein eigenständiger Satz mit der Nummer der Rechnung gehört zu ihr, auch wenn am Rand Leerraum steht – Tabulator,
    Zeilenumbruch, geschütztes Leerzeichen, alles, was ``str.strip()`` entfernt (SQLites ``TRIM`` ohne Zeichenliste nähme nur das
    Leerzeichen). Die Prüfliste sieht so einen Satz nicht: Nur dieser Vergleich fängt ihn, und der Plan nennt ``elsewhere``."""
    inv = _sent(db, 'R-30', 119.00)
    db.insert_booking('2026-03-01', 119.00, coa_id=_coa(db, 4400), counter_coa_id=_coa(db, 1600), booking_type='entry',
                      document_number='X', tax_rate=0.19, tax_amount=19.00, text='Erlös')
    _exec(db, "UPDATE Bookings SET DocumentNumber = ? WHERE DocumentNumber = 'X'", spelling)       # roh, so wie hier geschrieben
    assert _raw(db, "SELECT DocumentNumber FROM Bookings WHERE Text = 'Erlös'") == [(spelling,)]
    db.link_invoice_to_transaction(inv, _bank(db, acct, 119.00, date='2026-03-02'), 119.00)
    assert 'orphan_payment_rows' not in {f['category'] for f in db.find_payment_inconsistencies(inv)}
    assert _euer(db)[4400] == 100.0

    assert_elsewhere_stops_the_rebuild(db, inv)


def test_the_edge_whitespace_is_exactly_what_str_strip_removes():
    """``_EDGE_WHITESPACE`` ist ein Literal (nichts wird beim Import gerechnet) und enthält genau die Zeichen, die ``str.strip()``
    am Rand entfernt – jedes einmal."""
    assert sorted(_EDGE_WHITESPACE) == sorted(chr(code) for code in range(0x110000) if chr(code).isspace())


def test_the_number_comparison_in_sql_takes_every_edge_whitespace_character_on_both_sides():
    """Der SQL-Baustein ``_SAME_NUMBER_SQL`` entfernt jedes dieser Zeichen links wie rechts – auch die mehrbytigen – und
    vergleicht ohne Rücksicht auf die Groß-/Kleinschreibung."""
    con = sqlite3.connect(':memory:')
    try:
        con.execute('CREATE TABLE Bookings (DocumentNumber TEXT)')
        con.executemany('INSERT INTO Bookings VALUES (?)', [(f'{char}R-30{char}',) for char in _EDGE_WHITESPACE])
        con.execute("INSERT INTO Bookings VALUES ('R-300')")                       # eine andere Nummer zählt nie
        found = con.execute(f'SELECT COUNT(*) FROM Bookings WHERE {_SAME_NUMBER_SQL}',
                            (_EDGE_WHITESPACE, ' r-30\t', _EDGE_WHITESPACE)).fetchone()[0]
    finally:
        con.close()
    assert found == len(_EDGE_WHITESPACE)


def test_the_revenue_of_rows_counts_the_amount_of_the_rows_with_a_revenue_account_once():
    """``_revenue_booked``, die reine Rechnung: Konten-ID → Nummer sind 1 = 1800 (Bank), 2 = 10000 (Debitoren), 3 = 4405
    (Wartekonto), 4 = 4400 und 5 = 4300 (Erlöse). Das Satzpaar der App zählt einmal seinen Betrag, „Bank an Debitoren“ allein nichts,
    der Betrag zählt ohne Vorzeichen; ein Betrag, der keine ganze Zahl ist, ergibt ``None`` – nur bei Sätzen, die Erlös buchen."""
    numbers = {1: 1800, 2: 10000, 3: 4405, 4: 4400, 5: 4300, 6: 4410}
    waiting = {3}
    revenue = Database._revenue_booked

    assert revenue([(1, 2, 1190000), (3, 4, 1190000)], numbers, waiting) == 1190000      # „Bank an Debitoren“ + „Wartekonto an Erlös“
    assert revenue([(4, 1, 500000)], numbers, waiting) == 500000                         # der Einzelsatz „Erlöskonto an Bank“
    assert revenue([(1, 2, 1190000)], numbers, waiting) == 0                             # „Bank an Debitoren“ allein
    assert revenue([], numbers, waiting) == 0
    assert revenue([(3, 4, 100), (4, 1, 200), (1, 2, 999)], numbers, waiting) == 300     # die Summe der Sätze mit Erlöskonto
    assert revenue([(3, 4, -1190000)], numbers, waiting) == 1190000                      # ohne Vorzeichen
    assert revenue([(4, 5, 100)], numbers, waiting) == 100                               # beide Seiten Erlöskonten: einmal
    assert revenue([(6, 1, 100)], numbers, waiting) == 100 and revenue([(6, 1, 100)], numbers, {3, 6}) == 0   # Wartekonto nach ID
    assert revenue([(None, 99, 100)], numbers, waiting) == 0                             # unbekannte Konten
    assert revenue([(4, 1, 'abc')], numbers, waiting) is None
    assert revenue([(4, 1, True)], numbers, waiting) is None
    assert revenue([(4, 1, None)], numbers, waiting) is None
    assert revenue([(4, 1, 100.5)], numbers, waiting) is None
    assert revenue([(4, 1, 100.0)], numbers, waiting) == 100                             # eine ganze Kommazahl gilt als ganze Zahl
    assert revenue([(1, 2, 'abc')], numbers, waiting) == 0                               # kein Erlöskonto: der Betrag zählt nicht


# ── Die Prüfsumme ────────────────────────────────────────────────────────────

def test_the_check_does_not_depend_on_the_ids_of_the_new_rows_or_on_the_clock(db, acct):
    inv, bank = linked_case(db, acct)
    first = dry(db, inv).plan.check
    spare = db.insert_booking('2026-03-01', 5.00, booking_type='bank', account_id=acct)
    _exec(db, 'DELETE FROM Bookings WHERE ID = ?', spare)                       # die neuen Sätze bekämen höhere IDs
    _exec(db, "UPDATE Invoices SET UpdatedAt = '2020-01-01 00:00:00' WHERE ID = ?", inv)

    assert dry(db, inv).plan.check == first

    plan = dry(db, inv).plan
    assert run(db, inv, plan).executed is True                                  # und die Ausführung nimmt dieselbe Prüfsumme

def test_a_changed_stock_between_the_trial_run_and_the_execution_is_refused_with_the_new_plan(db, acct):
    inv, bank = linked_case(db, acct)
    plan = dry(db, inv).plan
    _exec(db, "UPDATE Bookings SET RecipientClient = 'Anderer Empfänger' WHERE ID = ?", bank)
    before = all_tables(db)

    result = run(db, inv, plan)

    assert (result.ok, result.code, result.executed, result.message) == (False, 409, False, REBUILD_CHANGED_MESSAGE)
    assert result.plan.check != plan.check and kinds(result.plan) == ['rebuild']
    assert all_tables(db) == before
    assert run(db, inv, result.plan).executed is True                           # mit der neuen Prüfsumme geht es
    assert _raw(db, 'SELECT RecipientClient FROM Bookings WHERE ParentBooking_ID = ?', bank) == [('Anderer Empfänger',)] * 2


def test_the_check_covers_the_new_rows_and_the_tax_date(db, acct):
    inv, bank = linked_case(db, acct)
    base = dry(db, inv).plan.check
    _exec(db, "UPDATE Bookings SET DateTax = '2026-03-01' WHERE ID = ?", bank)
    assert dry(db, inv).plan.check != base                                       # das Steuerdatum der Bewegung steht in den neuen Sätzen
    assert dry(db, inv).plan.check == dry(db, inv).plan.check


def test_the_day_is_part_of_the_check_only_when_duplicates_are_merged(db, acct):
    """Nur das Zusammenfassen von Dubletten rechnet den Status mit dem Tag neu (``next_payment_status``): Nur dann hängt das
    Ergebnis vom Tag ab, und nur dann geht er in die Prüfsumme ein. Sonst bliebe eine Bestätigung am nächsten Tag ohne Grund
    ungültig."""
    inv, bank = linked_case(db, acct)
    assert db.rebuild_invoice_payments(inv, today='2026-06-30').plan.check == db.rebuild_invoice_payments(
        inv, today='2026-07-01').plan.check                                      # ohne Dubletten: derselbe Plan an jedem Tag

    twice = _sent(db, 'R-E', 238.00)
    twin_bank = _bank(db, acct, 238.00, date='2026-02-05')
    drop_unique_index(db)
    raw_payment(db, twice, twin_bank, 1190000, '2026-02-05')
    raw_payment(db, twice, twin_bank, 1190000, '2026-02-05')
    first, second = (db.rebuild_invoice_payments(twice, today=day).plan for day in ('2026-06-30', '2026-07-01'))
    assert 'dedupe' in kinds(first) and first.check != second.check              # mit Dubletten: der Tag steht in der Prüfsumme
    assert dry(db, twice).plan.check == dry(db, twice).plan.check


def test_the_execution_needs_a_confirmation(db, acct):
    inv, bank = linked_case(db, acct)
    before = all_tables(db)

    result = db.rebuild_invoice_payments(inv, dry_run=False, today=FIXED_TODAY)

    assert (result.ok, result.code, result.executed, result.message) == (False, 409, False, REBUILD_CONFIRM_MISSING)
    assert kinds(result.plan) == ['rebuild'] and all_tables(db) == before
    wrong = db.rebuild_invoice_payments(inv, dry_run=False, confirmed_check='0' * 64, today=FIXED_TODAY)
    assert (wrong.code, wrong.message, wrong.executed) == (409, REBUILD_CHANGED_MESSAGE, False) and all_tables(db) == before


def test_an_unknown_invoice_and_a_quote_are_404(db, acct):
    quote = _invoice(db, 'A-1', 119.00, document_type='quote', status='draft')
    for invoice_id in (99999, quote):
        result = dry(db, invoice_id)
        assert (result.ok, result.code, result.plan) == (False, 404, None) and result.message == 'Rechnung nicht gefunden'


def test_the_probe_run_is_the_execution_without_the_commit_and_leaves_no_counter_behind(db, acct):
    inv, bank = linked_case(db, acct)
    sequence = _raw(db, 'SELECT * FROM sqlite_sequence ORDER BY name')
    tables_before = all_tables(db)

    dry(db, inv)

    assert all_tables(db) == tables_before and _raw(db, 'SELECT * FROM sqlite_sequence ORDER BY name') == sequence
    seq = dict(sequence)
    new_id = db.insert_booking('2026-03-01', 5.00, booking_type='bank', account_id=acct)
    assert new_id == seq['Bookings'] + 1                                           # dieselbe ID wie ohne Probelauf


def test_the_probe_run_never_leaves_the_database_locked(db, acct, monkeypatch):
    """Nach dem Probelauf, nach jeder Ablehnung und nach einer Ausnahme mitten in der Neubuchung nimmt eine zweite Verbindung
    sofort die Schreibsperre."""
    inv, bank = linked_case(db, acct)
    plan = dry(db, inv).plan
    lock_is_free(db)
    assert db.rebuild_invoice_payments(inv, dry_run=False, today=FIXED_TODAY).code == 409           # keine Bestätigung
    lock_is_free(db)
    assert run(db, inv, SimpleNamespace(check='1' * 64)).code == 409                              # veraltet
    lock_is_free(db)
    assert dry(db, 99999).code == 404
    lock_is_free(db)
    before = all_tables(db)

    def boom(*args, **kwargs):
        raise RuntimeError('mitten in der Neubuchung')

    for name in ('_assign_in_tx', '_unassign', '_payment_findings'):
        with monkeypatch.context() as patch:
            patch.setattr(Database, name, boom)
            for dry_run in (True, False):
                with pytest.raises(RuntimeError, match='mitten'):
                    db.rebuild_invoice_payments(inv, dry_run=dry_run, confirmed_check=plan.check, today=FIXED_TODAY)
                lock_is_free(db)
                assert all_tables(db) == before
    assert run(db, inv, plan).executed is True                                   # und danach geht es ganz normal


def test_a_missing_revenue_account_is_refused_before_anything_is_written_and_none_is_created(db, acct):
    inv = _sent(db, 'R-0', 100.00, rate=0)
    db.link_invoice_to_transaction(inv, _bank(db, acct, 100.00), 100.00)
    _exec(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4185')
    accounts, before = _raw(db, 'SELECT * FROM ChartOfAccounts ORDER BY ID'), all_tables(db)

    for kw in ({}, {'revenue_coa_id': ''}):
        result = dry(db, inv, **kw)
        assert (result.ok, result.code, result.message, result.plan, result.needs_revenue_coa) == (
            False, 400, NO_REVENUE_COA, None, True)
    assert _raw(db, 'SELECT * FROM ChartOfAccounts ORDER BY ID') == accounts and all_tables(db) == before       # 4185 bleibt weg
    lock_is_free(db)

    wrong = dry(db, inv, revenue_coa_id=_coa(db, 1800))                            # ein Bankkonto ist kein Erlöskonto
    assert (wrong.code, wrong.message, wrong.needs_revenue_coa) == (400, NOT_A_REVENUE_COA, False)
    assert dry(db, inv, revenue_coa_id='abc').message == NOT_A_REVENUE_COA
    assert all_tables(db) == before

    chosen = _revenue_account(db, 4186, 'Erlöse 0 % Test')
    accounts, before = _raw(db, 'SELECT * FROM ChartOfAccounts ORDER BY ID'), all_tables(db)
    result = dry(db, inv, revenue_coa_id=chosen)
    assert result.ok and result.plan.revenue_coa == chosen and all_tables(db) == before
    assert [(row.account, row.counter_account, row.tax) for row in result.plan.rows] == [(1800, 10000, None), (4340, 4186, None)]
    assert run(db, inv, result.plan, revenue_coa_id=chosen).executed is True
    assert _raw(db, 'SELECT COUNT(*) FROM ChartOfAccounts WHERE AccountNumber = 4185') == [(0,)]


def test_a_chosen_revenue_account_counts_only_when_none_is_stored(db, acct):
    inv, bank = linked_case(db, acct)                                            # 19 %: 4400 ist hinterlegt
    other = _revenue_account(db, 4410, 'Erlöse Test')

    plan = dry(db, inv, revenue_coa_id=other).plan

    assert [row.counter_account for row in plan.rows] == [10000, 4400] and plan.revenue_coa == _coa(db, 4400)


# ── Der Schutz: kein vorhandener Buchungssatz ändert sich ────────────────────

def test_a_resolved_receivable_that_is_not_covered_is_never_changed(db, acct):
    """``_unassign`` setzt eine aufgelöste, nicht ausgeglichene Forderung zurück – ein Eingriff in einen vorhandenen Satz. Die
    Reparatur nimmt alles zurück und sagt es (409)."""
    inv, bank = linked_case(db, acct)
    receivable = db.get_receivable_booking(inv)
    _exec(db, "UPDATE Bookings SET Status = 'resolved' WHERE ID = ?", receivable)
    before = all_tables(db)

    for result in (dry(db, inv), db.rebuild_invoice_payments(inv, dry_run=False, confirmed_check='a' * 64, today=FIXED_TODAY)):
        assert (result.ok, result.code, result.message, result.plan, result.executed) == (
            False, 409, REBUILD_UNSAFE_MESSAGE, None, False)
        assert all_tables(db) == before
        lock_is_free(db)


def test_a_building_block_that_deletes_or_changes_a_row_is_rolled_back(db, acct, monkeypatch):
    inv, bank = linked_case(db, acct)
    receivable = db.get_receivable_booking(inv)
    before = all_tables(db)
    original = Database._assign_in_tx

    def deleting(self, tx, invoice_id, booking_id, requested, ctx, allow_cancelled=False):
        result = original(self, tx, invoice_id, booking_id, requested, ctx, allow_cancelled)
        tx.conn.execute('DELETE FROM Bookings WHERE ID = ?', (receivable,))
        return result

    def altering(self, tx, invoice_id, booking_id, requested, ctx, allow_cancelled=False):
        result = original(self, tx, invoice_id, booking_id, requested, ctx, allow_cancelled)
        tx.conn.execute("UPDATE Bookings SET Text = 'geändert' WHERE ID = ?", (bank,))
        return result

    for replacement in (deleting, altering):
        with monkeypatch.context() as patch:
            patch.setattr(Database, '_assign_in_tx', replacement)
            result = dry(db, inv)
            assert (result.ok, result.code, result.message) == (False, 409, REBUILD_UNSAFE_MESSAGE)
            assert db.rebuild_invoice_payments(inv, dry_run=False, confirmed_check='b' * 64, today=FIXED_TODAY).code == 409
            assert all_tables(db) == before


def test_a_new_finding_after_the_writing_takes_everything_back(db, acct, logged, monkeypatch, capsys):
    """Das zweite Netz: Schlüge eine Kandidatin trotz der Sätze an anderer Stelle durch (hier erzwungen), nennt die Prüfliste nach
    dem Schreiben eine Kategorie, die sie vorher nicht nannte – ``transfer_mismatch``: zugeordnet sind 119,00 €, auf das Erlöskonto
    umgebucht 238,00 €, der Erlös stünde doppelt da. Die Reparatur nimmt alles zurück (409, keine Zeile, kein Log, keine Sperre); die
    Kategorie steht im Server-Log, nie in der Antwort."""
    inv = _sent(db, 'R-30', 119.00)
    b1 = _bank(db, acct, 119.00, date='2026-03-01')
    _assign(db, inv, b1)
    _exec(db, 'DELETE FROM InvoicePayments')                                     # die Sätze unter B1 bleiben, ohne Zuordnung
    b2 = _bank(db, acct, 119.00, date='2026-03-02')
    db.link_invoice_to_transaction(inv, b2, 119.00)
    assert 'transfer_mismatch' not in {f['category'] for f in db.find_payment_inconsistencies(inv)}

    def forced(self, conn, invoice_id, number, payments, *rest):
        payment_id, booking_id, amount, notes, pay_date = payments[0]
        bank_date, account_id = conn.execute('SELECT DateBooking, Account_ID FROM Bookings WHERE ID = ?', (booking_id,)).fetchone()
        return [RebuildCandidate(payment_id, booking_id, amount, notes, pay_date, bank_date, account_id)]

    monkeypatch.setattr(Database, '_rebuild_candidates', forced)
    # die Summenregel je Rechnung hielte die erzwungene Kandidatin schon auf – hier geht es um das Netz dahinter
    monkeypatch.setattr(Database, '_overbooked', lambda *args: False)
    before = all_tables(db)
    logged.clear()
    capsys.readouterr()

    for result in (dry(db, inv), db.rebuild_invoice_payments(inv, dry_run=False, confirmed_check='c' * 64, today=FIXED_TODAY)):
        assert (result.ok, result.code, result.message, result.plan, result.executed) == (
            False, 409, REBUILD_UNSAFE_MESSAGE, None, False)
        assert 'transfer_mismatch' not in result.message
        assert all_tables(db) == before and logged.entries == []
        lock_is_free(db)
    output = capsys.readouterr()
    assert [line for line in (output.out + output.err).splitlines() if f'Rechnung {inv}' in line and 'transfer_mismatch' in line]


# ── Gesunde Bestände: kein Plan, keine Änderung ──────────────────────────────

def healthy_normal(db, acct):
    inv = _sent(db, 'R001', 119.00)
    _assign(db, inv, _bank(db, acct, 119.00))
    return [inv]


def healthy_collective(db, acct):
    bank = _bank(db, acct, 357.00)
    return [_assign_pair(db, number, gross, bank) for number, gross in (('R001', 119.00), ('R002', 238.00))]


def _assign_pair(db, number, gross, bank):
    inv = _sent(db, number, gross)
    _assign(db, inv, bank)
    return inv


def healthy_partial(db, acct):
    inv = _sent(db, 'R-TP', 100.00)
    for n, amount in enumerate((33.33, 33.33, 33.34), start=1):
        _assign(db, inv, _bank(db, acct, amount, date=f'2026-02-0{n}'))
    return [inv]


def healthy_open(db, acct):
    return [_sent(db, 'R-OFFEN', 119.00)]


@pytest.mark.parametrize('build', [healthy_normal, healthy_collective, healthy_partial, healthy_open],
                         ids=['Normalfall', 'Sammelzahlung', 'Teilzahlungen', 'offen'])
def test_a_healthy_stock_has_nothing_to_rebuild_and_nothing_changes(db, acct, build):
    invoices = build(db, acct)
    assert db.find_payment_inconsistencies() == []
    before = all_tables(db)
    for inv in invoices:
        plan = dry(db, inv).plan
        assert plan.actions == [] and plan.skipped == [] and plan.rows == [] and plan.findings_after == plan.findings_before == []
        result = run(db, inv, plan)
        assert (result.ok, result.executed, result.message) == (True, False, REBUILD_NOTHING_MESSAGE)
    assert all_tables(db) == before                                              # auch UpdatedAt ändert sich nie


@pytest.mark.parametrize('build', [healthy_normal, healthy_collective, healthy_partial, healthy_open],
                         ids=['Normalfall', 'Sammelzahlung', 'Teilzahlungen', 'offen'])
def test_the_rebuild_writes_what_assigning_one_after_the_other_wrote(db, acct, build):
    """Der Eigenschaftstest: Ein gesunder Bestand entsteht durch Zuordnen nacheinander. Gehen die Sätze aller Zuordnungen verloren
    (per SQL entfernt), baut die Reparatur sie neu – und jede Spalte jedes Satzes ist dieselbe wie zuvor (die IDs ausgenommen, bei den
    Zuordnungen auch ``CreatedAt``)."""
    invoices = build(db, acct)
    reference = rows_without_ids(db)
    _exec(db, 'DELETE FROM Bookings WHERE Status = ?', PAYMENT_MARK)

    for inv in invoices:
        plan = dry(db, inv).plan
        assert set(kinds(plan)) <= {'rebuild'}
        assert run(db, inv, plan).executed is bool(plan.actions)

    assert rows_without_ids(db) == reference


@pytest.mark.parametrize('lost', [(0,), (1,), (2,), (0, 1), (0, 2), (1, 2), (0, 1, 2)],
                         ids=lambda lost: 'ohne-Sätze-' + '+'.join(map(str, lost)))
def test_an_invoice_with_some_healthy_assignments_gets_the_same_taxes_too(db, acct, lost):
    """Verlieren nur einige der drei Teilzahlungen ihre Sätze, bleiben die übrigen, wie sie sind, und zählen beim Neuaufbau als
    bezahlt: Auch dann steht in jedem Satz dasselbe wie nach dem Zuordnen nacheinander."""
    inv = healthy_partial(db, acct)[0]
    banks = [bank for (bank,) in _raw(db, 'SELECT BookingID FROM InvoicePayments ORDER BY PaymentDate, ID')]
    reference = rows_without_ids(db)
    for index in lost:
        _exec(db, 'DELETE FROM Bookings WHERE ParentBooking_ID = ? AND Status = ?', banks[index], PAYMENT_MARK)

    plan = dry(db, inv).plan
    assert kinds(plan) == ['rebuild'] * len(lost) and plan.skipped == []
    assert run(db, inv, plan).executed is True

    assert rows_without_ids(db) == reference


def test_a_replacement_invoice_after_the_move_of_the_payments_has_nothing_to_rebuild(world, monkeypatch):
    s = paid_scenario(world, monkeypatch)
    assert set_invoice_status(s.r2, 'sent')[0] == 200                           # die Zahlungen ziehen auf die Ersatzrechnung um
    assert world.db.find_payment_inconsistencies() == []
    before = all_tables(world.db)
    for invoice_id in (s.r1, s.s1, s.r2):
        plan = dry(world.db, invoice_id).plan
        assert plan.actions == [] and plan.rows == []
        assert run(world.db, invoice_id, plan).executed is False
    assert all_tables(world.db) == before


# ── Die Route ────────────────────────────────────────────────────────────────

def repair(port, login, payload, **kw):
    return post_json(port, ROUTE, payload, login.csrf, **kw)


def http_case(login):
    db = login.db
    acct = _account(db)
    inv, bank = linked_case(db, acct)
    return db, inv, bank


def test_get_on_the_route_is_405_and_changes_nothing(server_port, auto_login):
    db, inv, bank = http_case(auto_login)
    check = dry(db, inv).plan.check                                 # selbst mit der richtigen Prüfsumme führt ein GET nichts aus
    before = all_tables(db)
    for query in ('', f'?invoiceId={inv}', f'?invoiceId={inv}&execute=true', f'?invoiceId={inv}&execute=true&check={check}'):
        resp = get(server_port, ROUTE + query)
        assert resp.status == 405 and resp.headers['Allow'] == 'POST'
    assert all_tables(db) == before


def test_the_route_needs_the_token_and_the_own_origin(server_port, auto_login):
    db, inv, bank = http_case(auto_login)
    before = all_tables(db)
    body = json.dumps({'invoiceId': inv})
    for headers in ({}, {'X-CSRF-Token': ''}, {'X-CSRF-Token': 'falsch'}):
        assert send(server_port, 'POST', ROUTE, {'Content-Type': 'application/json', **headers}, body).status == 403
    assert repair(server_port, auto_login, {'invoiceId': inv}, headers={'Sec-Fetch-Site': 'cross-site'}).status == 403
    assert repair(server_port, auto_login, {'invoiceId': inv}, headers={'Origin': FOREIGN_ORIGIN}).status == 403
    assert all_tables(db) == before


BAD_VALUES = [None, '', 0, -1, 1.5, 1.0, '01', ' 1', '1.0', True, [1], {}, 2 ** 63, '9' * 30, 'abc']


def test_unusable_values_are_400_and_change_nothing(server_port, auto_login):
    db, inv, bank = http_case(auto_login)
    plan = dry(db, inv).plan
    before = all_tables(db)
    cases = []
    for bad in BAD_VALUES:
        cases.append({'invoiceId': bad})
    cases.append({})
    for bad in (None, 0, 1, 'true', 'false', [True], {}):
        cases.append({'invoiceId': inv, 'execute': bad})
    for bad in (None, '', 'abc', 'A' * 64, plan.check.upper(), plan.check[:-1], plan.check + '0', 7, [plan.check]):
        cases.append({'invoiceId': inv, 'execute': True, 'check': bad})
    cases.append({'invoiceId': inv, 'execute': True})
    cases.append({'invoiceId': inv, 'check': plan.check})                          # check ohne execute
    cases.append({'invoiceId': inv, 'execute': False, 'check': plan.check})
    for bad in (0, -1, 1.5, 1.0, '01', ' 1', True, [1], {}, 2 ** 63, 'abc'):
        cases.append({'invoiceId': inv, 'revenueCoaId': bad})
    for payload in cases:
        resp = repair(server_port, auto_login, payload)
        assert (resp.status, json.loads(resp.text)) == (400, INVALID_REQUEST), payload
    for body in (b'{', b'', b'[]', b'null', b'"text"', b'42'):
        resp = post_json(server_port, ROUTE, body, auto_login.csrf)
        assert (resp.status, json.loads(resp.text)) == (400, INVALID_REQUEST), body
    assert all_tables(db) == before


def test_an_unknown_invoice_and_a_quote_are_404_over_http(server_port, auto_login):
    db, inv, bank = http_case(auto_login)
    quote = _invoice(db, 'A-1', 119.00, document_type='quote', status='draft')
    before = all_tables(db)
    for invoice_id in (99999, quote):
        resp = repair(server_port, auto_login, {'invoiceId': invoice_id})
        assert resp.status == 404 and json.loads(resp.text) == {
            'success': False, 'executed': False, 'needsRevenueCoa': False, 'error': 'Rechnung nicht gefunden'}
    assert all_tables(db) == before


def test_the_trial_run_over_http_answers_with_the_plan_and_changes_nothing(server_port, auto_login):
    db, inv, bank = http_case(auto_login)
    before = all_tables(db)

    resp = repair(server_port, auto_login, {'invoiceId': inv})

    assert resp.status == 200 and resp.headers['Content-Type'].startswith('application/json')
    body = json.loads(resp.text)
    assert (body['success'], body['executed'], body['needsRevenueCoa']) == (True, False, False) and 'error' not in body
    assert 'message' not in body
    plan = body['plan']
    assert re.fullmatch('[0-9a-f]{64}', plan['check']) and plan['nothingToDo'] is False
    assert plan['actions'] == [{'kind': 'rebuild', 'text': dry(db, inv).plan.actions[0].text}]
    assert plan['rows'][1] == {'date': '2026-02-01', 'account': '4405', 'counterAccount': '4400', 'amount': '119,00 €',
                               'tax': '19,00 €', 'text': 'Umb. zu Re. R001', 'euer': True}
    assert plan['euer'] == [{'date': '2026-02-01', 'account': '4400', 'net': '100,00 €', 'tax': '19,00 €'}]
    assert plan['invoice'] == {'statusBefore': 'Bezahlt', 'statusAfter': 'Bezahlt', 'dueBefore': '0,00 €', 'dueAfter': '0,00 €'}
    assert plan['skipped'] == [] and plan['remaining'] == [] and plan['warning'] == REBUILD_WARNING
    assert all_tables(db) == before


def test_a_wrong_check_is_409_with_the_fresh_plan_and_the_right_one_executes(server_port, auto_login):
    db, inv, bank = http_case(auto_login)
    check = json.loads(repair(server_port, auto_login, {'invoiceId': inv}).text)['plan']['check']
    before = all_tables(db)

    wrong = repair(server_port, auto_login, {'invoiceId': inv, 'execute': True, 'check': '0' * 64})
    body = json.loads(wrong.text)
    assert wrong.status == 409 and (body['success'], body['executed'], body['error']) == (False, False, REBUILD_CHANGED_MESSAGE)
    assert body['plan']['check'] == check and all_tables(db) == before

    right = repair(server_port, auto_login, {'invoiceId': inv, 'execute': True, 'check': check})
    body = json.loads(right.text)
    assert right.status == 200 and (body['success'], body['executed'], body['message']) == (True, True, REBUILD_DONE_MESSAGE)
    assert [(row[0], row[1]) for row in _raw(db, 'SELECT ParentBooking_ID, Status FROM Bookings WHERE ParentBooking_ID = ?', bank)] == [
        (bank, PAYMENT_MARK)] * 2
    again = json.loads(repair(server_port, auto_login, {'invoiceId': inv}).text)
    assert again['plan']['nothingToDo'] is True and again['plan']['actions'] == []


def test_a_missing_revenue_account_over_http(server_port, auto_login):
    db = auto_login.db
    acct = _account(db)
    inv = _sent(db, 'R-0', 100.00, rate=0)
    db.link_invoice_to_transaction(inv, _bank(db, acct, 100.00), 100.00)
    _exec(db, 'DELETE FROM ChartOfAccounts WHERE AccountNumber = 4185')
    before = all_tables(db)

    resp = repair(server_port, auto_login, {'invoiceId': inv})

    assert resp.status == 400 and json.loads(resp.text) == {
        'success': False, 'executed': False, 'needsRevenueCoa': True, 'error': NO_REVENUE_COA}
    bad = repair(server_port, auto_login, {'invoiceId': inv, 'revenueCoaId': _coa(db, 1800)})
    assert (bad.status, json.loads(bad.text)['error']) == (400, NOT_A_REVENUE_COA)
    chosen = _revenue_account(db, 4186, 'Erlöse 0 % Test')
    ok = repair(server_port, auto_login, {'invoiceId': inv, 'revenueCoaId': chosen})
    assert ok.status == 200 and json.loads(ok.text)['plan']['rows'][1]['counterAccount'] == '4186'
    assert _raw(db, 'SELECT COUNT(*) FROM ChartOfAccounts WHERE AccountNumber = 4185') == [(0,)]


SECRET = 'geheim /srv/x/buch.db'


def test_an_unexpected_error_is_a_500_with_a_fixed_sentence_and_the_text_stays_in_the_log(server_port, auto_login, monkeypatch,
                                                                                           capsys):
    db, inv, bank = http_case(auto_login)
    before = all_tables(db)

    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(Database, 'rebuild_invoice_payments', boom)
    resp = repair(server_port, auto_login, {'invoiceId': inv})

    assert (resp.status, json.loads(resp.text)) == (500, {'success': False, 'error': REBUILD_FAILED})
    assert 'geheim' not in resp.text and '/srv' not in resp.text and 'sqlite' not in resp.text
    assert SECRET in capsys.readouterr().err                                      # aber im Server-Log
    assert all_tables(db) == before


def test_the_route_is_in_the_post_only_json_sets():
    from server import app
    assert ROUTE in app._JSON_PATHS and ROUTE in app._POST_ONLY_JSON_PATHS and ROUTE in app._JSON_ANSWER_ROUTES


def test_the_plan_payload_names_the_status_and_never_a_raw_key(db, acct):
    inv = _sent(db, 'R-E', 238.00)
    bank = _bank(db, acct, 238.00)
    drop_unique_index(db)
    raw_payment(db, inv, bank, 1190000)
    raw_payment(db, inv, bank, 1190000)

    payload = rebuild_plan_payload(dry(db, inv).plan)

    assert [action['kind'] for action in payload['actions']] == ['dedupe', 'rebuild', 'recalc']
    assert payload['actions'][-1]['text'] == 'Offener Betrag 238,00 € → 119,00 €; Status Versendet → Teilzahlung'
    assert payload['invoice'] == {'statusBefore': 'Versendet', 'statusAfter': 'Teilzahlung', 'dueBefore': '238,00 €',
                                  'dueAfter': '119,00 €'}
    assert json.dumps(payload).count('partial_payment') == 0


# ── Die Seite ────────────────────────────────────────────────────────────────

def buttons(html):
    return [attrs.get('onclick') for tag, attrs in Dom(html).tags if tag == 'button']


def test_the_button_appears_only_with_a_finding(db, acct):
    healthy = _sent(db, 'R-GUT', 119.00)
    _assign(db, healthy, _bank(db, acct, 119.00))
    broken, _bank_id = linked_case(db, acct, number='R-DEFEKT', date='2026-02-03')

    good_page, bad_page = PageInvoice(db, {}, healthy), PageInvoice(db, {}, broken)

    assert f'rebuildPayments({broken}, false)' in buttons(bad_page)
    assert 'rebuildPayments' not in good_page and 'rebuildPreview' not in good_page
    dom = Dom(bad_page)
    assert dom.one('div', id='rebuildPreview')['class'] == 'no-pdf'
    assert [a['class'] for t, a in dom.tags if t == 'button' and a.get('onclick') == f'rebuildPayments({broken}, false)'] == [
        'coloredButton btn-sm bg-blue']
    assert 'Zuordnung prüfen und neu aufbauen' in bad_page


def test_the_credit_note_page_has_no_button(world):
    s = corrupted_pair(world)
    _exec(world.db, "DELETE FROM Bookings WHERE DocumentNumber = '26R001' AND ParentBooking_ID IS NULL")   # die Gegenbuchung fehlt
    assert 'storno_counter_mismatch' in {f['category'] for f in world.db.find_payment_inconsistencies(s.s1)}

    storno_page, original_page = PageInvoice(world.db, {}, s.s1), PageInvoice(world.db, {}, s.r1)

    assert 'Zahlungszuordnung prüfen' in storno_page and 'rebuildPayments' not in storno_page
    assert f'rebuildPayments({s.r1}, false)' in buttons(original_page)           # die stornierte Rechnung zeigt ihn – der Plan nennt das Paar


def test_the_choice_of_the_revenue_account_appears_only_without_a_stored_one(db, acct):
    stored = _sent(db, 'R-19', 119.00)
    db.link_invoice_to_transaction(stored, _bank(db, acct, -119.00), 119.00)         # ein Befund (Ausgang), 19 %: 4400 ist hinterlegt
    unlisted = _sent(db, 'R-5', 105.00, rate=0.05)
    _exec(db, 'UPDATE Invoices SET AmountDue = 7 WHERE ID = ?', unlisted)           # ein Befund; 5 %: kein Erlöskonto hinterlegt

    with_stored, without = Dom(PageInvoice(db, {}, stored)), Dom(PageInvoice(db, {}, unlisted))

    assert [a.get('id') for a in with_stored.all('select') if a.get('name') == 'revenue_coa_id'] == []
    ids = [a.get('id') for a in without.all('select') if a.get('name') == 'revenue_coa_id']
    assert sorted(ids) == ['rebuildRevenueCoa', 'revenueCoa']                      # die Zahlung hat ihre eigene Auswahl
    assert len(ids) == len(set(ids))                                               # keine ID doppelt
    assert 'Für den Neuaufbau ist kein Erlöskonto hinterlegt – bitte wählen.' in PageInvoice(db, {}, unlisted)


def test_the_sentence_on_the_miscellaneous_page_names_the_rebuild(db, acct):
    linked_case(db, acct)

    html = PageMiscellaneous(db, check_payments=True)

    assert 'auf der Seite der Rechnung lässt sich die Zuordnung prüfen und, wo das sicher geht, nach einem Probelauf neu aufbauen' in html
    assert 'die Rechnung öffnen und die Buchungen prüfen' not in html


# ── Das Skript im Browser ────────────────────────────────────────────────────

def page_script(db, inv):
    (script,) = [text for text in Dom(PageInvoice(db, {}, inv)).scripts if 'function rebuildPayments(' in text]
    return script


def test_the_script_sets_no_markup_and_uses_only_var_and_function(db, acct):
    inv, _bank_id = linked_case(db, acct)
    script = page_script(db, inv)
    for forbidden in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'Function(', '=>', 'javascript:'):
        assert forbidden not in script, forbidden
    assert not re.search(r'(?<![A-Za-z0-9_$.])(const|let)\s', script)
    assert 'textContent' in script and '</script' not in script.lower() and '__CONFIRM__' not in script
    assert json.dumps(REBUILD_CONFIRM_QUESTION, ensure_ascii=False) in script
    assert "'<" not in script and '"<' not in script                              # kein HTML aus Zeichenketten


NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die Textprüfungen derselben Datei decken das Wichtigste ab')

HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [, , sourceFile, scenarioFile] = process.argv;
const scenario = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));

class Element {
  constructor(tag) {
    this.tagName = tag; this.className = ''; this.children = []; this.attributes = {}; this.listeners = {}; this.ownText = '';
  }
  get textContent() { return this.ownText; }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  set innerHTML(value) { throw new Error('innerHTML wurde gesetzt'); }
  set outerHTML(value) { throw new Error('outerHTML wurde gesetzt'); }
  insertAdjacentHTML() { throw new Error('insertAdjacentHTML wurde aufgerufen'); }
}
function dump(node) {
  return {tag: node.tagName, text: node.ownText, className: node.className, attributes: node.attributes,
          children: node.children.map(dump)};
}
function walk(node, visit) { visit(node); node.children.forEach(child => walk(child, visit)); }

const box = new Element('div');
box.attributes['data-check'] = scenario.initialCheck;
const field = scenario.field === null ? null : {value: scenario.field};
const fetches = [], msgs = [], confirms = [], timers = [];
let reloads = 0;
const responses = scenario.responses.slice();
const sandbox = {
  document: {
    getElementById: id => (id === 'rebuildPreview' ? box : (id === 'rebuildRevenueCoa' ? field : null)),
    createElement: tag => new Element(tag),
  },
  fetch: (url, options) => {
    fetches.push({url, method: options.method, headers: options.headers, body: options.body});
    const next = responses.shift();
    return Promise.resolve({status: next.status, json: () => Promise.resolve(next.body)});
  },
  showMessage: (text, type) => msgs.push([text, type]),
  appConfirm: (text, onOk) => { confirms.push(text); if (scenario.confirm) onOk(); },
  setTimeout: (fn, ms) => { timers.push(ms); fn(); },
  location: {reload: () => { reloads += 1; }},
  Number, JSON,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourceFile, 'utf8'), sandbox);
const wait = () => new Promise(resolve => setTimeout(resolve, 25));
function findButton(label) {
  let found = null;
  walk(box, node => { if (!found && node.listeners.click && node.ownText === label) found = node; });
  return found;
}
(async () => {
  vm.runInContext(scenario.call, sandbox); await wait();
  for (const label of scenario.clicks) {
    const target = findButton(label);
    if (!target) throw new Error('nichts zum Anklicken: ' + label);
    target.listeners.click({}); await wait();
  }
  console.log(JSON.stringify({fetches, msgs, confirms, timers, reloads, tree: dump(box)}));
})();
"""


def run_script(tmp_path, script, **scenario):
    """Das Skript der Seite in Node, mit einer kleinen DOM-Attrappe (``createElement``, ``textContent``, ``setAttribute``,
    ``fetch`` mit festen Antworten, ``showMessage``, ``appConfirm``); ``innerHTML`` und Verwandte werfen. Liefert Anfragen,
    Meldungen, Rückfragen und den Baum des Kastens."""
    scenario = {'call': 'rebuildPayments(7, false)', 'clicks': [], 'responses': [], 'field': None, 'confirm': True,
                'initialCheck': None, **scenario}
    harness, source, data = (tmp_path / name for name in ('harness.js', 'page.js', 'scenario.json'))
    harness.write_text(HARNESS, encoding='utf-8')
    source.write_text(script, encoding='utf-8')
    data.write_text(json.dumps(scenario), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(source), str(data)], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def walk_tree(node):
    yield node
    for child in node['children']:
        yield from walk_tree(child)


def answer(status, body):
    return {'status': status, 'body': body}


@pytest.fixture
def script_world(db, acct):
    inv, _bank_id = linked_case(db, acct)
    plan = dry(db, inv).plan
    payload = rebuild_plan_payload(plan)
    healthy = _sent(db, 'R-GUT', 119.00)
    nothing = rebuild_plan_payload(dry(db, healthy).plan)
    attached = _sent(db, 'R-LOESEN', 119.00)                                      # nur die angehängte Forderung wird gelöst
    _adopt(db, db.get_receivable_booking(attached), _bank(db, acct, 119.00, date='2026-03-01'))
    detach_only = rebuild_plan_payload(dry(db, attached).plan)
    return SimpleNamespace(db=db, inv=inv, script=page_script(db, inv), payload=payload, nothing=nothing, plan=plan,
                           detach_only=detach_only)


def success(payload, **extra):
    return {'success': True, 'executed': False, 'needsRevenueCoa': False, 'plan': payload, **extra}


@NO_NODE
def test_the_script_draws_the_plan_only_as_text_and_sends_the_check_of_the_drawn_plan(tmp_path, script_world):
    payload = json.loads(json.dumps(script_world.payload))
    payload['actions'][0]['text'] = HOSTILE
    payload['rows'][0]['text'] = HOSTILE
    payload['skipped'] = [HOSTILE]
    payload['remaining'] = ['"><script>alert(1)</script>']
    payload['invoice']['statusBefore'] = HOSTILE
    executed = {'success': True, 'executed': True, 'needsRevenueCoa': False, 'message': REBUILD_DONE_MESSAGE}

    out = run_script(tmp_path, script_world.script, clicks=['Neu aufbauen'],
                     responses=[answer(200, success(payload)), answer(200, executed)])

    nodes = list(walk_tree(out['tree']))
    assert {node['tag'] for node in nodes} <= {'div', 'h4', 'p', 'ul', 'li', 'table', 'tr', 'th', 'td', 'button'}       # kein img, script …
    texts = [node['text'] for node in nodes]
    assert HOSTILE in texts and '"><script>alert(1)</script>' in texts                                                 # als Text, nie Markup
    assert 'Probelauf – es wurde nichts geändert' in texts and REBUILD_WARNING in texts
    assert out['tree']['attributes'] == {'data-check': payload['check']}
    (button,) = [node for node in nodes if node['tag'] == 'button']
    assert (button['text'], button['className'], button['attributes']) == ('Neu aufbauen', 'coloredButton btn-sm bg-green', {'type': 'button'})
    assert out['confirms'] == [REBUILD_CONFIRM_QUESTION]
    first, second = out['fetches']
    assert (first['url'], first['method'], first['headers']) == (ROUTE, 'POST', {'Content-Type': 'application/json'})
    assert json.loads(first['body']) == {'invoiceId': 7, 'execute': False}
    assert json.loads(second['body']) == {'invoiceId': 7, 'execute': True, 'check': payload['check']}               # die Prüfsumme des gezeichneten Plans
    assert out['msgs'] == [[REBUILD_DONE_MESSAGE, 'info']] and out['timers'] == [1500] and out['reloads'] == 1


@NO_NODE
def test_the_script_offers_the_rebuild_only_with_actions(tmp_path, script_world):
    out = run_script(tmp_path, script_world.script, responses=[answer(200, success(script_world.nothing))])

    nodes = list(walk_tree(out['tree']))
    assert not [node for node in nodes if node['tag'] == 'button']
    assert 'Es gibt nichts, was sich automatisch neu aufbauen lässt.' in [node['text'] for node in nodes]
    assert REBUILD_WARNING not in [node['text'] for node in nodes] and len(out['fetches']) == 1
    assert out['tree']['attributes'] == {'data-check': script_world.nothing['check']}
    assert [node['text'] for node in nodes if node['tag'] == 'h4'] == ['Probelauf – es wurde nichts geändert']


@NO_NODE
def test_the_script_shows_the_warning_only_with_new_rows_and_the_button_with_actions(tmp_path, script_world):
    """Der Hinweis zu EÜR und Umsatzsteuer gehört zu neuen Buchungssätzen: Ein Plan, der nur eine Forderung löst, hat keine Zeilen und
    keinen Hinweis – den Knopf bekommt er, denn es gibt eine Aktion."""
    detach_only = script_world.detach_only
    assert detach_only['rows'] == [] and detach_only['nothingToDo'] is False and detach_only['warning'] == REBUILD_WARNING

    with_rows = run_script(tmp_path, script_world.script, responses=[answer(200, success(script_world.payload))])
    nodes = list(walk_tree(with_rows['tree']))
    assert script_world.payload['rows'] and REBUILD_WARNING in [node['text'] for node in nodes]
    assert [node['text'] for node in nodes if node['tag'] == 'button'] == ['Neu aufbauen']

    without_rows = run_script(tmp_path, script_world.script, responses=[answer(200, success(detach_only))])
    nodes = list(walk_tree(without_rows['tree']))
    assert REBUILD_WARNING not in [node['text'] for node in nodes]
    assert [node['text'] for node in nodes if node['tag'] == 'button'] == ['Neu aufbauen']
    assert 'Neue Buchungssätze' not in [node['text'] for node in nodes if node['tag'] == 'h4']


@NO_NODE
def test_the_script_redraws_a_409_and_sends_the_new_check_afterwards(tmp_path, script_world):
    old, new = dict(script_world.payload), dict(script_world.payload, check='f' * 64)
    stale = {'success': False, 'executed': False, 'needsRevenueCoa': False, 'error': REBUILD_CHANGED_MESSAGE, 'plan': new}
    executed = {'success': True, 'executed': True, 'needsRevenueCoa': False, 'message': REBUILD_DONE_MESSAGE}

    out = run_script(tmp_path, script_world.script, clicks=['Neu aufbauen', 'Neu aufbauen'],
                     responses=[answer(200, success(old)), answer(409, stale), answer(200, executed)])

    assert [json.loads(fetch['body']).get('check') for fetch in out['fetches']] == [None, old['check'], 'f' * 64]
    assert out['msgs'] == [[REBUILD_CHANGED_MESSAGE, 'warn'], [REBUILD_DONE_MESSAGE, 'info']]
    assert out['tree']['attributes'] == {'data-check': 'f' * 64}


@NO_NODE
def test_the_script_shows_a_refusal_and_a_missing_revenue_account_as_messages(tmp_path, script_world):
    needs = {'success': False, 'executed': False, 'needsRevenueCoa': True, 'error': NO_REVENUE_COA}
    out = run_script(tmp_path, script_world.script, field='12', responses=[answer(400, needs)])
    assert out['msgs'] == [[NO_REVENUE_COA, 'error']] and out['tree']['children'] == []
    assert json.loads(out['fetches'][0]['body']) == {'invoiceId': 7, 'execute': False, 'revenueCoaId': 12}
    refused = {'success': False, 'executed': False, 'needsRevenueCoa': False, 'error': 'Rechnung nicht gefunden'}
    out = run_script(tmp_path, script_world.script, responses=[answer(404, refused)])
    assert out['msgs'] == [['Fehler: Rechnung nicht gefunden', 'error']]
    assert 'revenueCoaId' not in json.loads(out['fetches'][0]['body'])
    out = run_script(tmp_path, script_world.script, field='', responses=[answer(500, {'success': False, 'error': REBUILD_FAILED})])
    assert out['msgs'] == [['Fehler: ' + REBUILD_FAILED, 'error']] and 'revenueCoaId' not in json.loads(out['fetches'][0]['body'])


@NO_NODE
def test_the_script_parses(tmp_path, script_world):
    source = tmp_path / 'rebuild.js'
    source.write_text(script_world.script, encoding='utf-8')
    done = subprocess.run([NODE, '--check', str(source)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
