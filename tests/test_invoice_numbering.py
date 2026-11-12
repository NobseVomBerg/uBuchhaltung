# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Rechnungsnummern (2.1.2): Das erste Speichern – auch als Entwurf – vergibt die Nummer.

Der Zähler des Nummernkreises rückt in der Transaktion des Speicherns vor
(``Database.allocate_invoice_number``): Scheitert das Speichern, ist die Nummer nicht verbraucht;
Statuswechsel zählen nie mit (Entwurf ↔ abgeschlossen verbrauchte früher eine Nummer je Wechsel).
Der Vorschlag der Maske (``peek_next_number``) ist genau die Nummer, die das Speichern vergibt – mit
Formatvorlage und Zusatz des Kreises, ohne eine schon belegte Nummer. Eine eigene Nummer hebt den
Zähler nur nach oben. Das Löschen eines Entwurfs gibt die letzte Nummer zurück, eine mittlere lässt
eine Lücke (``release_invoice_number``). Alle Namen und Nummern sind erfundene Testwerte.
"""
import json
import sqlite3
import threading

import pytest

import server.handlers as handlers
from tests.test_invoice_recompute import _make_contacts


def _payload(own_id, cust_id, **over):
    p = {
        'invoiceNumber': '26R100', 'invoiceDate': '2026-06-07', 'deliveryDate': '2026-06-05',
        'customerId': cust_id, 'ownCompanyId': own_id, 'taxRate': 0.19, 'status': 'draft',
        'items': [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung',
                   'unitPrice': 100.0, 'taxRate': 19}],
    }
    p.update(over)
    return p


def _save(payload):
    return json.loads(handlers.handle_invoice_save(json.dumps(payload).encode()))


def _set_status(db, invoice_id, status):
    code, body = handlers.handle_update_invoice_status(
        json.dumps({'invoice_id': invoice_id, 'status': status}).encode())
    return code, json.loads(body)


def _counter(db, letter='R', year=2026):
    return [r[5] for r in db.fetch_number_ranges('invoice') if r[2] == year and r[3] == letter][0]


def _number(db, invoice_id):
    return db.get_invoice_by_id(invoice_id)[1]


@pytest.fixture
def world(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    return tmp_db, own, cust


def _new_invoice(world, **over):
    """Neue Rechnung, wie die Maske sie anlegt: der Vorschlag des Nummernkreises, gespeichert als Entwurf."""
    db, own, cust = world
    proposal = db.peek_next_number('invoice', 2026)[0]
    resp = _save(_payload(own, cust, invoiceNumber=proposal, **over))
    assert resp['success'] is True, resp
    return resp['invoice_id'], proposal


# ── das erste Speichern vergibt die Nummer ───────────────────────────────────

def test_two_new_drafts_in_a_row_get_consecutive_numbers(world):
    db = world[0]
    first, first_number = _new_invoice(world)
    assert first_number == '26R001' and _counter(db) == 1
    second, second_number = _new_invoice(world)                    # früher: wieder 26R001 und „bereits vergeben“
    assert second_number == '26R002' and _counter(db) == 2
    assert [_number(db, first), _number(db, second)] == ['26R001', '26R002']


def test_the_proposal_is_the_number_the_save_hands_out(world):
    db = world[0]
    for _ in range(3):
        invoice_id, proposal = _new_invoice(world)
        assert _number(db, invoice_id) == proposal                 # keine Abweichung zwischen Maske und Speichern
        assert db.peek_next_number('invoice', 2026)[0] != proposal


def test_changing_the_status_never_counts(world):
    db, own, cust = world
    invoice_id, _ = _new_invoice(world)
    assert _counter(db) == 1
    for status in ('finalized', 'draft', 'finalized', 'draft', 'finalized'):
        assert _set_status(db, invoice_id, status)[0] == 200
        assert _counter(db) == 1                                   # früher: +1 bei jedem Wechsel nach „abgeschlossen“
    assert _set_status(db, invoice_id, 'sent')[0] == 200           # und Versenden zählt nicht mit
    assert _counter(db) == 1


def test_updating_an_invoice_does_not_touch_the_counter(world):
    db, own, cust = world
    invoice_id, number = _new_invoice(world)
    db.update_number_range(db.fetch_number_ranges('invoice')[0][0], 2026, 'R', '', 20, '', None)
    resp = _save(_payload(own, cust, invoiceNumber=number, invoiceId=invoice_id, deliveryDate='2026-06-06'))
    assert resp['success'] is True and _counter(db) == 20


def test_a_failed_save_does_not_use_the_number(world):
    db, own, cust = world
    bad = _payload(own, cust, invoiceNumber=db.peek_next_number('invoice', 2026)[0])
    bad['items'].append({'position': 2, 'quantity': 1, 'unit': 'C62', 'description': None,
                         'unitPrice': 5.0, 'taxRate': 19})
    assert _save(bad)['success'] is False
    assert _counter(db) == 0
    assert _new_invoice(world)[1] == '26R001'


# ── eigene Nummern ───────────────────────────────────────────────────────────

def test_an_own_number_lifts_the_counter_but_never_lowers_it(world):
    db, own, cust = world
    assert _save(_payload(own, cust, invoiceNumber='26R010'))['success'] is True
    assert _counter(db) == 10
    assert _save(_payload(own, cust, invoiceNumber='26R004'))['success'] is True    # nach unten: nie
    assert _counter(db) == 10
    assert db.peek_next_number('invoice', 2026)[0] == '26R011'


def test_an_own_number_outside_every_template_leaves_the_counters_alone(world):
    db, own, cust = world
    for number in ('RE-17', '26X050', '25R050', '26R5x'):
        assert _save(_payload(own, cust, invoiceNumber=number))['success'] is True
    assert _counter(db) == 0


def test_a_taken_own_number_is_reported_and_leaves_the_counter_alone(world):
    db, own, cust = world
    assert _save(_payload(own, cust, invoiceNumber='26R003'))['success'] is True
    again = _save(_payload(own, cust, invoiceNumber='26R003'))
    assert again == {'success': False, 'error': 'Diese Nummer ist bereits vergeben.'}
    assert _counter(db) == 3


# ── Formatvorlage und Zusatz ─────────────────────────────────────────────────

def test_template_and_suffix_of_the_range_are_used(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', prefix='_A', current_number=6,
                               number_format='{yyyy}-{l}{nn}{s}')
    assert tmp_db.peek_next_number('invoice', 2026)[0] == '2026-R07_A'      # früher: „26R_A007“
    resp = _save(_payload(own, cust, invoiceNumber='2026-R07_A'))
    assert resp['success'] is True and _counter(tmp_db) == 7
    assert tmp_db.peek_next_number('invoice', 2026)[0] == '2026-R08_A'
    # eine eigene Nummer in der Vorlage hebt den Zähler, eine ohne den Zusatz passt nicht
    assert _save(_payload(own, cust, invoiceNumber='2026-R15_A'))['success'] is True
    assert _counter(tmp_db) == 15
    assert _save(_payload(own, cust, invoiceNumber='2026-R30'))['success'] is True
    assert _counter(tmp_db) == 15


def test_the_proposal_skips_numbers_that_are_already_taken(world):
    db, own, cust = world
    db.update_number_range(db.fetch_number_ranges('invoice')[0][0], 2026, 'R', '', 1, '', None)
    _save(_payload(own, cust, invoiceNumber='26R002'))             # eigene Nummer: hebt den Zähler auf 2
    db.update_number_range(db.fetch_number_ranges('invoice')[0][0], 2026, 'R', '', 1, '', None)
    # Zähler (von Hand) zurückgesetzt: 26R002 ist belegt, der Vorschlag nimmt 26R003
    assert db.peek_next_number('invoice', 2026)[0] == '26R003'
    invoice_id, number = _new_invoice(world)
    assert number == '26R003' and _counter(db) == 3


def test_a_quote_number_counts_as_taken_too(world):
    db, own, cust = world
    db.insert_invoice({'invoice_number': '26R001', 'invoice_date': '2026-01-02', 'seller_name': 'S',
                       'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG',
                       'tax_rate': 0.19, 'sum_net': 1, 'tax_amount': 0.19, 'sum_gross': 1.19,
                       'amount_due': 1.19, 'status': 'draft', 'document_type': 'quote'})
    assert db.peek_next_number('invoice', 2026)[0] == '26R002'


# ── Kreis nach dem Jahr des Datums, fehlender Kreis ──────────────────────────

def test_the_range_follows_the_year_of_the_invoice_date(tmp_db):
    tmp_db.insert_number_range('invoice', 2025, 'R', current_number=40)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=2)
    with tmp_db.transaction() as tx:
        assert tmp_db.allocate_invoice_number(tx.conn, '2025-12-30') == '25R041'
        assert tmp_db.allocate_invoice_number(tx.conn, '2026-01-02') == '26R003'
    assert (_counter(tmp_db, year=2025), _counter(tmp_db, year=2026)) == (41, 3)


def test_without_any_range_the_first_number_creates_one(tmp_db):
    with tmp_db.transaction() as tx:
        assert tmp_db.allocate_invoice_number(tx.conn, '2026-03-04') == '26R001'
    assert _counter(tmp_db) == 1


def test_a_new_year_gets_a_range_with_letter_suffix_and_template_of_the_newest(tmp_db):
    tmp_db.insert_number_range('invoice', 2025, 'B', prefix='_X', current_number=9,
                               number_format='{yyyy}/{l}{nnn}{s}')
    with tmp_db.transaction() as tx:
        assert tmp_db.allocate_invoice_number(tx.conn, '2026-01-05') == '2026/B001_X'
    assert _counter(tmp_db, 'B', 2026) == 1 and _counter(tmp_db, 'B', 2025) == 9


def test_a_rolled_back_allocation_leaves_the_counter(tmp_db):
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    with pytest.raises(RuntimeError):
        with tmp_db.transaction() as tx:
            assert tmp_db.allocate_invoice_number(tx.conn, '2026-02-02') == '26R006'
            raise RuntimeError('Speichern scheitert')
    assert _counter(tmp_db) == 5


def test_concurrent_saves_never_hand_out_a_number_twice(tmp_db):
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=0)
    numbers, errors = [], []

    def save():
        try:
            with tmp_db.transaction() as tx:
                number = tmp_db.allocate_invoice_number(tx.conn, '2026-05-05')
                tmp_db.insert_invoice({
                    'invoice_number': number, 'invoice_date': '2026-05-05', 'seller_name': 'S',
                    'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG',
                    'tax_rate': 0.19, 'sum_net': 1, 'tax_amount': 0.19, 'sum_gross': 1.19,
                    'amount_due': 1.19, 'status': 'draft'}, tx=tx)
            numbers.append(number)
        except Exception as exc:                      # pragma: no cover - nur bei einem Fehler
            errors.append(exc)

    threads = [threading.Thread(target=save) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(numbers) == [f'26R{n:03d}' for n in range(1, 9)]
    assert _counter(tmp_db) == 8


# ── Angebot → Rechnung ───────────────────────────────────────────────────────

def _quote(db, number='26A001', status='accepted'):
    quote_id = db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-06-09', 'seller_name': 'S',
        'seller_company': 'S GmbH', 'buyer_name': 'B', 'buyer_company': 'B AG', 'tax_rate': 0.19,
        'sum_net': 100.0, 'tax_amount': 19.0, 'sum_gross': 119.0, 'amount_due': 119.0,
        'status': status, 'document_type': 'quote', 'valid_until': '2026-07-09'})
    db.insert_invoice_item({'invoice_id': quote_id, 'position': 1, 'description': 'Position 1',
                            'quantity': 1, 'unit': 'C62', 'price_per_unit': 100.0, 'total_net': 100.0,
                            'tax_rate': 0.19})
    return quote_id


def test_converting_a_quote_takes_the_next_invoice_number(tmp_db):
    import datetime
    year = datetime.date.today().year
    tmp_db.insert_number_range('invoice', year, 'R', current_number=40)
    tmp_db.insert_number_range('quote', year, 'A', current_number=3)
    quote_id = _quote(tmp_db)
    new_id = tmp_db.convert_quote_to_invoice(quote_id)
    assert _number(tmp_db, new_id) == f'{str(year)[2:]}R041'
    assert _counter(tmp_db, year=year) == 41
    assert [r[5] for r in tmp_db.fetch_number_ranges('quote')] == [3]      # der Angebotskreis bleibt
    assert _number(tmp_db, quote_id) == '26A001'


def test_converting_a_quote_after_a_draft_continues_the_sequence(tmp_db, monkeypatch):
    import datetime
    year = datetime.date.today().year
    short = str(year)[2:]
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', year, 'R', current_number=0)
    draft = _save(_payload(own, cust, invoiceNumber=tmp_db.peek_next_number('invoice', year)[0],
                           invoiceDate=f'{year}-06-07'))
    assert _number(tmp_db, draft['invoice_id']) == f'{short}R001'
    new_id = tmp_db.convert_quote_to_invoice(_quote(tmp_db))
    assert _number(tmp_db, new_id) == f'{short}R002'
    assert tmp_db.peek_next_number('invoice', year)[0] == f'{short}R003'


# ── Löschen gibt die letzte Nummer frei ──────────────────────────────────────

def test_deleting_the_last_draft_gives_its_number_back_a_middle_one_leaves_a_gap(world):
    db = world[0]
    ids = [_new_invoice(world)[0] for _ in range(3)]               # 26R001 … 26R003
    db.delete_draft_invoice(ids[1])                                # eine mittlere: Lücke, Zähler bleibt
    assert _counter(db) == 3 and db.peek_next_number('invoice', 2026)[0] == '26R004'
    db.delete_draft_invoice(ids[2])                                # die letzte: der Zähler geht zurück
    assert _counter(db) == 2 and db.peek_next_number('invoice', 2026)[0] == '26R003'
    assert _new_invoice(world)[1] == '26R003'


def test_releasing_a_number_needs_the_counter_to_stand_on_it(tmp_db):
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    con = sqlite3.connect(tmp_db.db_name)
    try:
        con.execute('BEGIN IMMEDIATE')
        assert tmp_db.release_invoice_number(con, '26R004') is False       # darunter: Lücke
        assert tmp_db.release_invoice_number(con, '26R007') is False       # darüber: nichts zu tun
        assert tmp_db.release_invoice_number(con, 'RE-5') is False         # nicht aus dem Kreis
        assert tmp_db.release_invoice_number(con, '26R005') is True
        con.commit()
    finally:
        con.close()
    assert _counter(tmp_db) == 4
