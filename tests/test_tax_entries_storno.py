# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Stornieren eines Belegs mit EINEM Steuersatz aus den Stammdaten (2.8.0, Etappe 3: ``db/invoice_storno.py``, ``cancel_invoice``).

Eine Position trägt die Momentaufnahme eines Eintrags (``InvoiceItems.TaxRateBp`` ≠ NULL; dazu Art ``'E'`` bei steuerfrei, Grund und Erlöskonto). Für so einen Beleg liest das
Stornieren die Momentaufnahme mit denselben Bausteinen wie das Versenden (``_send_key``, ``_single_snapshot``, ``_entry_receivable_accounts``) – nie ``_tax_rate_key``, der den
Satz auf ganze Prozent rundet:

* **Das Wartekonto ist das des genauen Satzes:** ein steuerfreier Beleg bucht die Gegenbuchung der Forderung über 4340 (ohne Satz und ohne Steuer), 19 % über 4405. Ein Satz ohne
  Wartekonto – 7,4 % (gerundet 7 → 4345), 6,6 %, 19,25 %, 18,6 % (gerundet 19 → 4405) – hat beim Versenden keine Forderung gebucht, und das Stornieren legt das Konto des
  gerundeten Satzes NICHT an und bucht keine Gegenbuchung.
* **Widersprüche lehnen ab** (``MixedNotBookable`` → 409 mit dem festen Satz ``TAX_ENTRY_CONTRADICTION``), in der Vorprüfung UND in der Transaktion: zwei Gründe oder zwei Konten in einer Gruppe,
  ein Satz in Hundertstel Prozent als Text, negativ oder über 100 %, ein Kopfsatz, der nicht der Satz der Momentaufnahme ist, teils Momentaufnahme, teils keine. Vor dem Auflösen der
  Konten, nichts geschrieben – keine Zeile, kein Zähler (``sqlite_sequence``), keine Nummer des Nummernkreises, kein Konto.
* **Der Schlüssel** der Vorbereitung trägt die Momentaufnahme (``('entry', …)``, wie beim Versenden): Ändert sie sich zwischen Vorprüfung und Transaktion, lehnt ``CHANGED_MESSAGE`` ab.
* **Ohne Momentaufnahme bleibt alles, wie es war** (der ganze Bestand): ``_tax_rate_key``, Zeichen für Zeichen – dazu die Storno-Tests und ``tests/test_single_rate_snapshots.py``.
  Ein Beleg mit Steuersatz je Position (``MixedTaxRates``) ist nicht betroffen (``_checked_mixed_groups`` prüft dort schon).

Alle Namen, Gründe, Konten und Beträge sind erfunden; ein „heute“ steht nie in einer Modulkonstante (``CLOCK`` ist der festgesetzte Tag des Servers).
"""
import contextlib
import datetime
import sqlite3

import pytest

from db.invoice_lock import CHANGED_MESSAGE, NO_WAITING_HINT
from db.invoice_payments import TAX_ENTRY_CONTRADICTION
from tax_groups import STANDARD
from tests.invoice_world import columns, query_rows, run_sql
from tests.test_mixed_tax_bookings import receivables
from tests.test_mixed_tax_locks import chart, drop_accounts
from tests.test_tax_entries_save import REASON, entries, form, free_snapshot, item, saved, snapshot_columns, world   # noqa: F401  (``world`` und ``entries`` sind Fixtures)

USER = 'anna'
CLOCK = datetime.date(2026, 7, 31)                        # der Tag, den der Server für „heute“ hält (``today=`` von ``cancel_invoice``)
STORNO_DAY = '2026-07-02'


# ── Hilfen ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def everything(db):
    """Der ganze Bestand: jede Zeile jeder Tabelle, auch ``sqlite_sequence`` (die Zähler) – so zeigt sich jede Spur eines Schreibens, auch ein verbrauchter Zähler."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {name: con.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names}


def draft(world, items, number, **over):
    """Ein Entwurf, gespeichert über den Handler (mit ``taxRateId`` im Kopf, wenn ``over`` ihn nennt)."""
    return saved('invoice', form('invoice', world, items, invoiceNumber=number, **over))


def send(world, invoice_id):
    """Versenden über die Datenbankschicht (die Route erzeugt danach nur Dokumente); liefert das Ergebnis, das ``ok`` ist."""
    change = world.db.set_invoice_status(invoice_id, 'sent', USER)
    assert change.ok, change.message
    return change


def cancel(world, invoice_id):
    return world.db.cancel_invoice(invoice_id, STORNO_DAY, USER, today=CLOCK)


def counter_bookings(db, number):
    """Die Buchungssätze mit der Beleg-Nr. der Stornorechnung: ``(Konto, Gegenkonto, Betrag, Steuer, Satz, Form)`` – Beträge in Cent."""
    return [(r['coa'], r['counter'], r['Amount'] // 100, None if r['TaxAmount'] is None else r['TaxAmount'] // 100, r['TaxRate'], r['EntryForm']) for r in query_rows(
        db, 'SELECT c.AccountNumber AS coa, g.AccountNumber AS counter, b.Amount, b.TaxAmount, b.TaxRate, b.EntryForm FROM Bookings b '
            'LEFT JOIN ChartOfAccounts c ON c.ID = b.COA_ID LEFT JOIN ChartOfAccounts g ON g.ID = b.CounterCOA_ID '
            "WHERE b.DocumentNumber = ? AND b.BookingType = 'entry' ORDER BY b.ID", number)]


def exempt_invoice(world, entries, number='F-100', positions=1):
    """Ein ganz steuerfreier Beleg (Kopf 0, Momentaufnahme des Eintrags ``free``) mit 1 oder 2 Positionen zu je 100,00 bzw. 50,00 €, versendet – seine Forderung steht auf 4340."""
    items = [item(1, 'Nachhilfe', 100.0, 0)] if positions == 1 else [item(1, 'Nachhilfe A', 50.0, 0), item(2, 'Nachhilfe B', 50.0, 0)]
    invoice_id = draft(world, items, number, taxRate=0, taxRateId=entries.free)
    send(world, invoice_id)
    return invoice_id


# ═════════════════════════════════════════════════════════════════════════════
# Ganz steuerfrei: die Gegenbuchung über 4340, ohne Satz und ohne Steuer
# ═════════════════════════════════════════════════════════════════════════════

def test_cancelling_a_completely_tax_free_invoice_books_the_counter_booking_on_4340_without_a_rate(world, entries):
    invoice_id = exempt_invoice(world, entries)
    assert receivables(world.db, 'F-100')[0][:6] == (10000, 4340, 10000, None, None, 'transfer')

    storno = cancel(world, invoice_id)

    assert storno.ok is True and storno.counter_booked is True, storno
    assert counter_bookings(world.db, storno.storno_number) == [(10000, 4340, -10000, None, None, 'transfer')]            # Satz NULL, keine Steuer, negativ
    assert snapshot_columns(world.db, storno.storno_id) == [free_snapshot(entries)]                                       # die Stornorechnung trägt die Momentaufnahme mit
    assert columns(world.db, invoice_id)['Status'] == 'cancelled' and world.db.find_payment_inconsistencies() == []


def test_cancelling_an_invoice_whose_entry_rate_has_a_waiting_account_uses_the_account_of_exactly_that_rate(world, entries):
    """19 % mit Eintrag (Sonderkonto): das Wartekonto ist das der 19 % (4405) – wie beim Versenden."""
    invoice_id = draft(world, [item(1, 'Beratung', 100.0, 19)], 'S-19', taxRate=0.19, taxRateId=entries.nineteen_special)
    send(world, invoice_id)
    assert [row[:3] for row in receivables(world.db, 'S-19')] == [(10000, 4405, 11900)]

    storno = cancel(world, invoice_id)

    assert storno.ok is True and storno.counter_booked is True
    assert counter_bookings(world.db, storno.storno_number) == [(10000, 4405, -11900, -1900, 0.19, 'transfer')]
    assert world.db.find_payment_inconsistencies() == []


# ═════════════════════════════════════════════════════════════════════════════
# Ein Satz ohne Wartekonto: kein Konto, keine Gegenbuchung – auch wenn das gerundete Konto fehlt
# ═════════════════════════════════════════════════════════════════════════════

ROUNDED = [pytest.param(740, '7,4 %', 4345, id='7,4-prozent-wird-7'), pytest.param(660, '6,6 %', 4345, id='6,6-prozent-wird-7'),
           pytest.param(1925, '19,25 %', 4405, id='19,25-prozent-wird-19'), pytest.param(1860, '18,6 %', 4405, id='18,6-prozent-wird-19')]


@pytest.mark.parametrize('bp, label, rounded', ROUNDED)
def test_cancelling_a_rate_without_a_waiting_account_creates_neither_the_account_of_the_rounded_rate_nor_a_counter_booking(world, entries, bp, label, rounded):
    """``_tax_rate_key`` rundet auf ganze Prozent und nähme das Wartekonto der 7 % (4345) bzw. 19 % (4405). Für einen Beleg mit Momentaufnahme gilt der genaue Satz: Er hat keines, das
    Versenden hat keine Forderung gebucht, und das Stornieren legt das gerundete Konto – vorher aus dem Kontenrahmen genommen – nicht an und bucht nichts."""
    entry_id = world.db.insert_tax_rate(label=f'Satz {label} (erfunden)', bp=bp, kind=STANDARD, revenue_coa_id=entries.coa[4150])
    invoice_id = draft(world, [item(1, 'Beratung', 100.0, bp / 100)], 'R-1', taxRate=bp / 10000, taxRateId=entry_id)
    change = send(world, invoice_id)
    assert change.payment_hint == NO_WAITING_HINT.format(rate=label) and receivables(world.db, 'R-1') == []
    drop_accounts(world.db, 4345, 4405)                                                              # entstünde eines neu, zeigte es der Kontenrahmen
    accounts_before = chart(world.db)
    assert world.db._tax_rate_key(bp / 10000) == (7 if rounded == 4345 else 19)

    storno = cancel(world, invoice_id)

    assert storno.ok is True and storno.counter_booked is False, storno
    assert chart(world.db) == accounts_before                                                        # kein Konto angelegt
    assert counter_bookings(world.db, storno.storno_number) == []
    assert snapshot_columns(world.db, storno.storno_id) == [('S', bp, None, entries.coa[4150])]      # die Stornorechnung trägt die Momentaufnahme mit
    assert world.db.find_payment_inconsistencies() == []


@pytest.mark.parametrize('bp, label, rounded', ROUNDED)
def test_the_same_rate_without_a_snapshot_keeps_the_rounded_account_as_before(world, bp, label, rounded):
    """Ohne Momentaufnahme (der Bestand: der Satz im Kopf, ohne Eintrag) bleibt es bei ``_tax_rate_key`` – Forderung und Gegenbuchung auf dem Konto des gerundeten Satzes, Zeichen
    für Zeichen wie vor 2.8.0."""
    invoice_id = draft(world, [item(1, 'Beratung', 100.0, bp / 100)], 'P-1', taxRate=bp / 10000)
    send(world, invoice_id)
    assert [row[:2] for row in receivables(world.db, 'P-1')] == [(10000, rounded)]

    storno = cancel(world, invoice_id)

    assert storno.ok is True and storno.counter_booked is True
    assert counter_bookings(world.db, storno.storno_number)[0][:2] == (10000, rounded)


@pytest.mark.parametrize('rate, account', [(19, 4405), (7, 4345), (0, 4340)])
def test_a_plain_invoice_is_cancelled_exactly_as_before_and_its_key_is_the_rate_key(world, rate, account):
    invoice_id = draft(world, [item(1, 'Beratung', 100.0, rate)], 'P-2', taxRate=rate / 100)
    send(world, invoice_id)
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as conn:
        assert world.db._send_key(conn, invoice_id, rate / 100, False) == (world.db._tax_rate_key(rate / 100), None)          # ohne Momentaufnahme: der Schlüssel von früher

    storno = cancel(world, invoice_id)

    assert storno.ok is True and storno.counter_booked is True
    assert counter_bookings(world.db, storno.storno_number)[0][:2] == (10000, account)


def test_an_invoice_without_the_tax_shown_whose_rows_carry_the_category_e_is_cancelled_as_before(world):
    """Das Beispiel eines Belegs nach § 19 trägt die Art ``'E'`` an den Positionen, ohne einen Satz in Hundertstel Prozent: keine Momentaufnahme, kein Widerspruch – das Stornieren
    bucht die Gegenbuchung wie bisher über 4340."""
    invoice_id = draft(world, [item(1, 'Beratung', 100.0, 0)], 'K-1', taxRate=0, showTax=False)
    run_sql(world.db, "UPDATE InvoiceItems SET TaxCategory = 'E' WHERE InvoiceId = ?", invoice_id)
    send(world, invoice_id)

    storno = cancel(world, invoice_id)

    assert storno.ok is True and storno.counter_booked is True
    assert counter_bookings(world.db, storno.storno_number)[0][:2] == (10000, 4340)


# ═════════════════════════════════════════════════════════════════════════════
# Widersprüche: 409 mit dem festen Satz, vor dem Auflösen der Konten, nichts geschrieben
# ═════════════════════════════════════════════════════════════════════════════

CONTRADICTIONS = {
    'zwei-gruende-in-einer-gruppe': lambda w, e, i: run_sql(w.db, 'UPDATE InvoiceItems SET TaxExemptReason = ? WHERE InvoiceId = ? AND Position = 2', 'ein anderer Grund', i),
    'zwei-erloeskonten-in-einer-gruppe': lambda w, e, i: run_sql(w.db, 'UPDATE InvoiceItems SET RevenueCoaId = ? WHERE InvoiceId = ? AND Position = 2', e.coa[4150], i),
    'satz-in-hundertstel-prozent-als-text': lambda w, e, i: run_sql(w.db, 'UPDATE InvoiceItems SET TaxRateBp = ? WHERE InvoiceId = ? AND Position = 2', 'abc', i),
    'satz-in-hundertstel-prozent-negativ': lambda w, e, i: run_sql(w.db, 'UPDATE InvoiceItems SET TaxRateBp = ? WHERE InvoiceId = ? AND Position = 2', -100, i),
    'satz-in-hundertstel-prozent-ueber-100-prozent': lambda w, e, i: run_sql(w.db, 'UPDATE InvoiceItems SET TaxRateBp = ? WHERE InvoiceId = ? AND Position = 2', 10001, i),
    'kopf-weicht-vom-satz-der-momentaufnahme-ab': lambda w, e, i: run_sql(w.db, 'UPDATE Invoices SET TaxRate = ? WHERE ID = ?', 0.19, i),
    'kopf-ohne-steuerausweis': lambda w, e, i: run_sql(w.db, 'UPDATE Invoices SET TaxRate = ? WHERE ID = ?', -1, i),
    'teils-mit-teils-ohne-momentaufnahme': lambda w, e, i: run_sql(w.db, 'UPDATE InvoiceItems SET TaxRateBp = NULL WHERE InvoiceId = ? AND Position = 2', i),
    'steuerfreie-art-mit-satz-ueber-0': lambda w, e, i: run_sql(w.db, 'UPDATE InvoiceItems SET TaxRateBp = ? WHERE InvoiceId = ?', 700, i),
    'zwei-saetze-mit-momentaufnahme-ohne-kennzeichen': lambda w, e, i: run_sql(
        w.db, "UPDATE InvoiceItems SET TaxCategory = 'S', TaxRateBp = ?, TaxRate = ? WHERE InvoiceId = ? AND Position = 2", 550, 5.5, i),
}


@pytest.mark.parametrize('case', CONTRADICTIONS)
def test_cancelling_an_invoice_with_a_contradiction_is_refused_with_the_fixed_sentence_and_nothing_is_written(world, entries, case):
    """Nur nach einem Eingriff an der App vorbei (SQL-Konsole, zurückgespielte Sicherung) – das Versenden hätte sie nie zugelassen. Auch der Weg, der heute ``_tax_rate_key`` fragte,
    lehnt ab: Der Kopfsatz 0,19 über einem steuerfreien Beleg hätte die Gegenbuchung „10000 an 4405“ über −100,00 mit Satz 0,19 gebucht."""
    invoice_id = exempt_invoice(world, entries, positions=2)
    CONTRADICTIONS[case](world, entries, invoice_id)
    drop_accounts(world.db, 4345, 4405)                                  # die Konten, die der gerundete Satz träfe: Legte ein Weg sie neu an, zeigte es der Bestand
    before = everything(world.db)

    storno = cancel(world, invoice_id)

    assert (storno.ok, storno.code, storno.message) == (False, 409, TAX_ENTRY_CONTRADICTION)
    assert (storno.storno_id, storno.storno_number, storno.counter_booked) == (None, None, False)
    assert everything(world.db) == before                                # jede Tabelle, auch ``sqlite_sequence`` und die Nummernkreise, und der Kontenrahmen


def test_a_contradiction_that_appears_after_the_preparation_is_refused_by_the_transaction(world, entries, monkeypatch):
    """Zwischen Vorprüfung und Transaktion (Konten auflösen) ändert jemand den Kopfsatz: Die Transaktion liest den Stand neu und lehnt mit demselben Satz ab – nichts geschrieben,
    keine Nummer verbraucht (außer dem, was der Eingriff selbst geändert hat)."""
    invoice_id = exempt_invoice(world, entries)
    real = world.db._entry_receivable_accounts

    def prepared_then_tampered(pct):
        found = real(pct)
        run_sql(world.db, 'UPDATE Invoices SET TaxRate = ? WHERE ID = ?', 0.19, invoice_id)
        return found

    monkeypatch.setattr(world.db, '_entry_receivable_accounts', prepared_then_tampered)
    before, head_before = everything(world.db), columns(world.db, invoice_id)

    storno = cancel(world, invoice_id)

    after = everything(world.db)
    assert (storno.ok, storno.code, storno.message) == (False, 409, TAX_ENTRY_CONTRADICTION)
    assert columns(world.db, invoice_id) == {**head_before, 'TaxRate': 0.19}              # nur der Eingriff selbst
    assert {name: rows for name, rows in after.items() if name != 'Invoices'} == {name: rows for name, rows in before.items() if name != 'Invoices'}
    assert len(after['Invoices']) == len(before['Invoices']) == 1                          # keine Stornorechnung


# ═════════════════════════════════════════════════════════════════════════════
# Der Schlüssel der Vorbereitung trägt die Momentaufnahme
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('column, value', [('TaxExemptReason', 'ein anderer Grund'), ('RevenueCoaId', 4150)], ids=['anderer-grund', 'anderes-erloeskonto'])
def test_a_snapshot_changed_between_the_preparation_and_the_transaction_is_caught_by_the_key(world, entries, monkeypatch, column, value):
    """Ein gleichzeitig neu gespeicherter Beleg: Die Sätze sind dieselben, die Momentaufnahme nicht – „Die Rechnung wurde inzwischen geändert“, nichts geschrieben."""
    invoice_id = exempt_invoice(world, entries)
    new = entries.coa[value] if column == 'RevenueCoaId' else value
    real = world.db._entry_receivable_accounts
    statements = {'TaxExemptReason': 'UPDATE InvoiceItems SET TaxExemptReason = ? WHERE InvoiceId = ?', 'RevenueCoaId': 'UPDATE InvoiceItems SET RevenueCoaId = ? WHERE InvoiceId = ?'}

    def prepared_then_resaved(pct):
        found = real(pct)
        run_sql(world.db, statements[column], new, invoice_id)
        return found

    monkeypatch.setattr(world.db, '_entry_receivable_accounts', prepared_then_resaved)
    before = everything(world.db)

    storno = cancel(world, invoice_id)

    after = everything(world.db)
    assert (storno.ok, storno.code, storno.message) == (False, 409, CHANGED_MESSAGE)
    assert {name: rows for name, rows in after.items() if name != 'InvoiceItems'} == {name: rows for name, rows in before.items() if name != 'InvoiceItems'}
    assert columns(world.db, invoice_id)['Status'] == 'sent'


def test_the_key_of_the_cancellation_is_the_key_of_sending(world, entries):
    """Dieselbe Funktion (``_send_key``) liest den Stand für das Versenden und das Stornieren: ein Beleg mit Momentaufnahme hat einen Schlüssel ``('entry', …)``, ein Beleg ohne den Satzschlüssel."""
    with_entry = exempt_invoice(world, entries)
    plain = draft(world, [item(1, 'Beratung', 100.0, 19)], 'P-3', taxRate=0.19)

    with contextlib.closing(sqlite3.connect(world.db.db_name)) as conn:
        entry_key, entry_detail = world.db._send_key(conn, with_entry, 0.0, False)
        plain_key, plain_detail = world.db._send_key(conn, plain, 0.19, False)

    assert entry_key[0] == 'entry' and entry_key[2] == entry_detail and entry_detail[1].exempt and entry_detail[1].reason == REASON
    assert (plain_key, plain_detail) == (19, None)
