# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Belege mit Steuersatz je Position im PDF und in der E-Rechnung (2.7.0, ``export/pdf_document.py``, ``export/einvoice_model.py``).

Trägt ein Beleg das Kennzeichen ``Invoices.MixedTaxRates`` = 1, druckt sein PDF (Rechnung, Stornorechnung, Angebot) zwischen Bezeichnung und
Einzelpreis die Spalte „USt“ (je Position ihr Satz) und im Summenblock unter „Summe netto“ eine Zeile je Satzgruppe („zzgl. 7 % MwSt. auf
50,00 EUR:“ mit der Steuer der Gruppe) – nie die eine Zeile mit der Gesamtsteuer und dem Satz im Kopf, der bei so einem Beleg nur ein
Platzhalter ist. Die Gruppen kommen aus EINER Stelle (``tax_groups.item_groups``) und sind auf den Cent die der E-Rechnung (``build_einvoice``)
und, summiert, die gespeicherten Summen des Belegs. Ein Beleg mit einem Satz druckt Zeichen für Zeichen wie vorher (die Schnappschüsse in
``tests/test_single_rate_snapshots.py`` und die Golden-Dateien in ``tests/test_golden_invoice_outputs.py``); das Kennzeichen entscheidet, nie
die Sätze der Positionen.

Der Code rechnet für beliebig viele Satzgruppen: Die erlaubten Sätze stehen allein in ``tax_groups.MIXED_TAX_RATES`` (heute 19 und 7 %), die Tests
erweitern sie um 5,5 % und prüfen drei Gruppen samt Seitenumbruch.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import collections
import contextlib
import copy
import datetime
import hashlib
import importlib.util
import inspect
import io
import json
import os
import random
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal

import pdfplumber
import pytest

import server.handlers as handlers
import userctx
from export import einvoice_model, pdf_document as D
from export.einvoice_model import build_einvoice, per_item_rates
from export.pdf_invoice import generate_invoice_pdf
from export.pdf_quote import generate_quote_pdf
from export.xrechnung_invoice import XRechnungGenerator
from money import format_de, from_minor, to_minor
from tax_groups import MIXED_TAX_RATES, RateGroup, item_groups, item_rate, rate_groups
from tests.document_helpers import set_column
from tests.invoice_world import build_world, columns, make_payload, query_rows, save
from tests.single_rate_helpers import cii_facts, content_streams, embedded_xml, ubl_facts

USER = 'anna'
FIVE_AND_A_HALF = Decimal('5.5')
HEADER = 'Pos. Menge Einheit Bezeichnung USt Einzelpreis Gesamt'
HEADER_SINGLE_RATE = 'Pos. Menge Einheit Bezeichnung Einzelpreis Gesamt'


@pytest.fixture
def world(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    monkeypatch.chdir(tmp_path)
    userctx.set_user(USER)
    return build_world(tmp_db)


@pytest.fixture
def three_rates(monkeypatch):
    """Die erlaubten Sätze um 5,5 % erweitert – so, wie eine spätere Version sie erweitern würde (``tax_groups.MIXED_TAX_RATES``)."""
    monkeypatch.setattr(handlers, 'MIXED_TAX_RATES', (*MIXED_TAX_RATES, FIVE_AND_A_HALF))


# ── Belege bauen ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def item(position, description, price, rate, quantity=1, unit='C62'):
    """Eine Position, wie die Maske sie schickt (Satz als Prozentzahl)."""
    return {'position': position, 'quantity': quantity, 'unit': unit, 'description': description, 'unitPrice': price, 'taxRate': rate}


def two_rates():
    """100,00 € zu 19 % und 50,00 € zu 7 %: netto 150,00, Steuer 19,00 + 3,50 = 22,50, brutto 172,50."""
    return [item(1, 'Beratung', 50.0, 19, quantity=2, unit='HUR'), item(2, 'Fachbuch', 50.0, 7)]


def many_positions():
    """Fünf Positionen in zwei Gruppen mit krummen Beträgen: 19 %: 199,98 € → 38,00 € (37,9962); 7 %: 12,54 € → 0,88 € (0,8778)."""
    return [item(1, 'Beratung', 99.99, 19), item(2, 'Fachbuch', 12.34, 7), item(3, 'Heft', 0.10, 7), item(4, 'Heft', 0.10, 7),
            item(5, 'Reise', 33.33, 19, quantity=3)]


def mixed_invoice(world, items, number='26R200', **over):
    """Eine Rechnung mit Steuersatz je Position über den Weg der Maske (``handle_invoice_save``, ``taxMode: 'mixed'``)."""
    answer = save(make_payload(world, invoiceNumber=number, items=copy.deepcopy(items), taxMode='mixed', **over))
    assert answer['success'] is True, answer
    return answer['invoice_id']


def mixed_quote(world, items, number='26A200', **over):
    payload = {'quoteNumber': number, 'quoteDate': '2026-06-07', 'customerId': world.cust, 'ownCompanyId': world.own,
               'validUntil': '2026-07-07', 'introText': 'Guten Tag', 'closingText': 'Mit freundlichen Grüßen', 'taxRate': 0.19,
               'taxMode': 'mixed', 'items': copy.deepcopy(items), **over}
    answer = json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))
    assert answer['success'] is True, answer
    return answer['quote_id']


def _insert_row(db, table, row):
    """Eine Zeile als Kopie einfügen (die Spaltennamen stammen aus der Tabelle, die Werte gehen als Parameter)."""
    row = {key: value for key, value in row.items() if key != 'ID'}
    names = ', '.join(row)
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        cursor = con.execute(f'INSERT INTO {table} ({names}) VALUES ({", ".join("?" * len(row))})', list(row.values()))
        con.commit()
        return cursor.lastrowid


def make_credit_note(db, original_id, number='26S200'):
    """Die Stornorechnung zu ``original_id`` in der Datenbankschicht, so, wie ``cancel_invoice`` sie schreibt: Kopf kopiert, Summen exakt negiert,
    Positionen mit negativer Menge und positivem Einzelpreis. Der Weg der App lehnt das Stornieren eines Belegs mit Steuersatz je Position in dieser
    Version ab (``MIXED_NOT_BOOKABLE``: die Gegenbuchung der Forderung gibt es noch nicht) – deshalb von Hand."""
    stored = columns(db, original_id)
    head = {**stored, 'InvoiceNumber': number, 'InvoiceKind': 'storno', 'CancelsInvoiceId': original_id, 'Status': 'sent',
            'SumNet': -stored['SumNet'], 'TaxAmount': -stored['TaxAmount'], 'SumGross': -stored['SumGross'], 'AmountDue': -stored['SumGross'],
            'PaymentTerms': 'Der Betrag wird verrechnet bzw. erstattet.', 'PaymentDueDate': None, 'SkontoDays': None, 'SkontoPercent': None,
            'PDFPath': None, 'XMLPath': None, 'DocumentVersion': None}
    new_id = _insert_row(db, 'Invoices', head)
    for position in query_rows(db, 'SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', original_id):
        _insert_row(db, 'InvoiceItems', {**position, 'InvoiceId': new_id, 'Quantity': -position['Quantity'], 'TotalNet': -position['TotalNet']})
    return new_id


def invoice_pdf(world, invoice_id):
    pdf, _path = generate_invoice_pdf(world.db, invoice_id)
    assert pdf
    return pdf


def quote_pdf(world, quote_id):
    pdf, _path = generate_quote_pdf(world.db, quote_id)
    assert pdf
    return pdf


# ── Der Inhalt eines PDF, wie ihn die vorhandenen Tests herausziehen (pdfplumber: Wörter mit Position) ──────────────────────────

def page_rows(pdf, page=0):
    """Die Zeilen einer Seite von oben nach unten: ``(Text, Wörter)`` – die Wörter einer Zeile von links nach rechts (``pdfplumber``-Wörter
    mit ``x0``, ``x1``, ``top``, ``bottom``). Wörter mit ähnlicher Höhe (bis 3 pt) gehören zu einer Zeile: Die 11 pt von „Gesamtbetrag:“ und
    die 10 pt des Betrags daneben sitzen auf einer Grundlinie, aber nicht auf gleicher Oberkante."""
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        words = sorted(document.pages[page].extract_words(), key=lambda w: (w['top'], w['x0']))
    rows = []
    for word in words:
        if rows and abs(word['top'] - rows[-1][0]) <= 3:
            rows[-1][1].append(word)
        else:
            rows.append([word['top'], [word]])
    result = []
    for _top, row in rows:
        row = sorted(row, key=lambda w: w['x0'])
        result.append((' '.join(w['text'] for w in row), row))
    return result


def page_lines(pdf, page=0):
    return [text for text, _words in page_rows(pdf, page)]


def page_count(pdf):
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        return len(document.pages)


def block_page(pdf):
    """Auf welcher Seite (ab 0) steht „Summe netto:“ – der Summenblock bleibt zusammen, also steht dort auch der Rest."""
    for page in range(page_count(pdf)):
        if any(text.startswith('Summe netto:') for text in page_lines(pdf, page)):
            return page
    raise AssertionError('kein Summenblock im PDF')


def table_lines(pdf, count, header=HEADER):
    """Die ``count`` Zeilen ab der Kopfzeile der Positionstabelle (``header``: mit der Spalte „USt“ oder ohne)."""
    lines = page_lines(pdf)
    start = lines.index(header)
    return lines[start:start + count]


# ── item_groups: die EINE Stelle für die Satzgruppen ─────────────────────────────────────────────────────────────────────────────

def test_item_groups_gives_one_group_per_rate_with_the_tax_of_its_net_sum_highest_rate_first():
    groups = item_groups([(7, to_minor('50.00')), (19, to_minor('100.00'))])

    assert groups == [RateGroup(Decimal('19'), to_minor('100.00'), to_minor('19.00'), to_minor('119.00')),
                      RateGroup(Decimal('7'), to_minor('50.00'), to_minor('3.50'), to_minor('53.50'))]


def test_the_rate_of_a_position_is_a_fraction_or_a_percentage_and_means_the_same_group():
    """Die Datenbank führt den Satz teils als Bruch (0,19), teils als Prozentzahl (19); als Text oder ``Decimal`` ebenso."""
    groups = item_groups([(0.19, 1000000), (19, 1000000), ('19', 1000000), ('0.19', 1000000), (Decimal('19.00'), 1000000), (0.07, 500000)])

    assert [(g.pct, g.net) for g in groups] == [(Decimal('19'), 5000000), (Decimal('7'), 500000)]


def test_the_tax_is_rounded_once_per_group_from_the_sum_not_per_position():
    """Drei Positionen zu 7 % über je 0,10 €: Die Gruppe hat 0,30 € netto und 0,021 € Steuer = 0,02 € – je Position gerundet wären es 0,03 €."""
    (group,) = item_groups([(7, to_minor('0.10'))] * 3)

    assert (group.net, group.tax, group.gross) == (to_minor('0.30'), to_minor('0.02'), to_minor('0.32'))


def test_any_number_of_groups_and_any_rate_five_and_a_half_percent_included():
    pairs = [(19, to_minor('100.00')), (7, to_minor('50.00')), (FIVE_AND_A_HALF, to_minor('33.33')), (Decimal('10.7'), to_minor('200.00')),
             (0, to_minor('12.00'))]

    groups = item_groups(pairs)

    assert [g.pct for g in groups] == [Decimal('19'), Decimal('10.7'), Decimal('7'), Decimal('5.5'), Decimal('0')]       # absteigend
    assert [g.tax for g in groups] == [to_minor('19.00'), to_minor('21.40'), to_minor('3.50'), to_minor('1.83'), 0]     # 33,33 × 5,5 % = 1,83315
    assert all(g.gross == g.net + g.tax for g in groups)


def test_a_position_without_a_rate_takes_the_default_and_without_a_default_the_call_does_not_guess():
    groups = item_groups([(None, to_minor('10.00')), ('', to_minor('20.00')), (7, to_minor('5.00'))], default=Decimal('19'))

    assert [(g.pct, g.net) for g in groups] == [(Decimal('19'), to_minor('30.00')), (Decimal('7'), to_minor('5.00'))]
    assert item_rate(None, Decimal('7')) == Decimal('7') and item_rate('', Decimal('7')) == Decimal('7')
    assert item_rate(19) == Decimal('19') and item_rate(0.07) == Decimal('7')
    with pytest.raises(Exception):
        item_groups([(None, to_minor('10.00'))])                  # kein Satz und keine Vorgabe: kein stilles Raten


def test_a_group_with_the_net_sum_0_stays_and_no_positions_give_no_groups():
    groups = item_groups([(19, 0), (7, to_minor('10.00'))])

    assert [(g.pct, g.net, g.tax) for g in groups] == [(Decimal('19'), 0, 0), (Decimal('7'), to_minor('10.00'), to_minor('0.70'))]
    assert item_groups([]) == []


def test_the_groups_of_a_credit_note_are_the_negated_groups_of_the_invoice():
    """Die Stornorechnung trägt ihre Netto-Summen negativ; die Steuer rundet kaufmännisch (halb weg von null) – spiegelbildlich."""
    pairs = [(19, to_minor('100.00')), (7, to_minor('0.50')), (7, to_minor('0.50')), (7, to_minor('0.50')), (FIVE_AND_A_HALF, to_minor('0.09'))]

    positive = item_groups(pairs)
    negative = item_groups([(rate, -net) for rate, net in pairs])

    assert [(g.pct, -g.net, -g.tax, -g.gross) for g in negative] == [(g.pct, g.net, g.tax, g.gross) for g in positive]


def test_the_groups_add_up_to_what_saving_a_document_stores_for_any_mix_of_positions():
    """Eigenschaftsprüfung: Für zufällige Positionen (feste Folge der Zufallszahlen) sind Netto, Steuer und Brutto der Gruppen genau die Summen von
    ``handlers.recompute_invoice_totals`` – das, was ein Beleg beim Speichern ablegt."""
    chance = random.Random(270)
    for _ in range(200):
        rows = [{'quantity': chance.choice([1, 2, 3, 0.5, 7, 12.25]), 'unitPrice': round(chance.uniform(0.01, 999.99), 2),
                 'taxRate': chance.choice([19, 7, 5.5, 10.7, 0.19, 0.07])} for _ in range(chance.randint(1, 8))]
        net, tax, gross, line_nets = handlers.recompute_invoice_totals(rows, 19)

        groups = item_groups([(row['taxRate'], to_minor(line)) for row, line in zip(rows, line_nets)])

        assert (sum(g.net for g in groups), sum(g.tax for g in groups), sum(g.gross for g in groups)) == (
            to_minor(net), to_minor(tax), to_minor(gross)), rows


# ── Das PDF einer Rechnung ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_invoice_pdf_shows_the_column_ust_and_one_tax_row_per_group(world):
    pdf = invoice_pdf(world, mixed_invoice(world, two_rates()))

    assert table_lines(pdf, 7) == [
        HEADER,
        '1 2,00 Std. Beratung 19 % 50,00 EUR 100,00 EUR',
        '2 1,00 Stk. Fachbuch 7 % 50,00 EUR 50,00 EUR',
        'Summe netto: 150,00 EUR',
        'zzgl. 19 % MwSt. auf 100,00 EUR: 19,00 EUR',
        'zzgl. 7 % MwSt. auf 50,00 EUR: 3,50 EUR',
        'Gesamtbetrag: 172,50 EUR']


def test_the_invoice_pdf_never_prints_the_old_single_tax_row_with_the_placeholder_rate_of_the_head(world):
    """Der Satz im Kopf ist bei so einem Beleg der höchste Satz (19 %) und nur ein Platzhalter: Die heutige Zeile „zzgl. 19% MwSt.: 22,50 EUR“
    (die Gesamtsteuer unter dem Satz einer Gruppe) darf nirgends stehen."""
    iid = mixed_invoice(world, two_rates())
    assert columns(world.db, iid)['TaxRate'] == 0.19

    lines = page_lines(invoice_pdf(world, iid))

    assert not any(re.search(r'zzgl\. [\d,.]+% MwSt\.:', line) for line in lines)
    assert not any('22,50' in line for line in lines), 'die Gesamtsteuer steht nur als Summe der Gruppen im Beleg, nicht als eine Zeile'


def test_the_tax_rows_are_the_tax_of_each_group_and_add_up_to_the_stored_tax_and_gross(world):
    iid = mixed_invoice(world, many_positions())
    stored = columns(world.db, iid)

    lines = page_lines(invoice_pdf(world, iid))

    assert [line for line in lines if line.startswith('zzgl.')] == ['zzgl. 19 % MwSt. auf 199,98 EUR: 38,00 EUR',
                                                                    'zzgl. 7 % MwSt. auf 12,54 EUR: 0,88 EUR']
    assert [line for line in lines if line.startswith('Summe netto:') or line.startswith('Gesamtbetrag:')] == [
        f'Summe netto: {format_de(stored["SumNet"])} EUR', f'Gesamtbetrag: {format_de(stored["SumGross"])} EUR']
    assert stored['TaxAmount'] == to_minor('38.00') + to_minor('0.88') and stored['SumGross'] == stored['SumNet'] + stored['TaxAmount']


def test_the_credit_note_prints_the_stored_negative_amounts_with_negative_groups_and_embeds_the_positive_xml(world):
    original = mixed_invoice(world, two_rates())
    storno = make_credit_note(world.db, original)

    pdf = invoice_pdf(world, storno)

    lines = page_lines(pdf)
    assert table_lines(pdf, 7) == [
        HEADER,
        '1 -2,00 Std. Beratung 19 % 50,00 EUR -100,00 EUR',
        '2 -1,00 Stk. Fachbuch 7 % 50,00 EUR -50,00 EUR',
        'Summe netto: -150,00 EUR',
        'zzgl. 19 % MwSt. auf -100,00 EUR: -19,00 EUR',
        'zzgl. 7 % MwSt. auf -50,00 EUR: -3,50 EUR',
        'Gesamtbetrag: -172,50 EUR']
    assert any(line.endswith('Stornorechnung') for line in lines) and any('zu Rechnung Nr. 26R200' in line for line in lines)      # Titel, Bezug
    cii = cii_facts(embedded_xml(pdf))                                    # im XML stehen die Beträge positiv (Typ 381)
    assert cii['type'] == '381'
    assert sorted((Decimal(t['rate']), Decimal(t['basis']), Decimal(t['amount'])) for t in cii['tax']) == [
        (Decimal('7'), Decimal('50.00'), Decimal('3.50')), (Decimal('19'), Decimal('100.00'), Decimal('19.00'))]


# ── Das PDF eines Angebots ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_quote_pdf_prints_the_same_column_and_rows(world):
    pdf = quote_pdf(world, mixed_quote(world, two_rates()))

    assert table_lines(pdf, 7) == [
        HEADER,
        '1 2,00 Std. Beratung 19 % 50,00 EUR 100,00 EUR',
        '2 1,00 Stk. Fachbuch 7 % 50,00 EUR 50,00 EUR',
        'Summe netto: 150,00 EUR',
        'zzgl. 19 % MwSt. auf 100,00 EUR: 19,00 EUR',
        'zzgl. 7 % MwSt. auf 50,00 EUR: 3,50 EUR',
        'Gesamtbetrag: 172,50 EUR']
    assert not any(re.search(r'zzgl\. [\d,.]+% MwSt\.:', line) for line in page_lines(pdf))


def test_a_quote_turned_into_an_invoice_keeps_the_flag_and_prints_the_columns(world):
    """Angebot → Rechnung kopiert das Kennzeichen und die Sätze der Positionen (``convert_quote_to_invoice``); das PDF der Rechnung folgt ihnen."""
    quote_id = mixed_quote(world, two_rates())

    invoice_id = world.db.convert_quote_to_invoice(quote_id, today=datetime.date(2026, 6, 8))

    lines = page_lines(invoice_pdf(world, invoice_id))
    assert HEADER in lines and 'zzgl. 7 % MwSt. auf 50,00 EUR: 3,50 EUR' in lines


# ── Schnappschüsse: die Seiten eines Belegs mit Steuersatz je Position ───────────────────────────────────────────────────────────

# SHA-256 der Content-Streams (alle Seiten, wie ``tests/single_rate_helpers.pdf_facts`` sie hasht) – am Stand von 2.7.0 genommen. Ändert sich ein
# Zeichen der Seite, ist der Code zu ändern, nie die Konstante: Sie hält Spalte, Zeilen und Abstände fest (neu nehmen nur mit Absicht und Ansicht des PDF)
INVOICE_SNAPSHOT = 'd3b4b15c86ebdaf01b51d7fcad3367a3374e26906db5cf26926249f15aa0d1dd'
QUOTE_SNAPSHOT = '67fabe200534a4e27b58247236f38a462ddd01012013d2ec75dbfe435db642e9'
CREDIT_NOTE_SNAPSHOT = 'cca8d397dd0d8c882d5c5b2bef7af183f1325a48634f7e9f511202bf807634db'


def pages_hash(pdf):
    return hashlib.sha256(b'\n-- Seite --\n'.join(content_streams(pdf))).hexdigest()


def test_the_pages_of_an_invoice_with_two_rates_are_what_they_were_at_2_7_0(world):
    assert pages_hash(invoice_pdf(world, mixed_invoice(world, two_rates()))) == INVOICE_SNAPSHOT


def test_the_pages_of_a_quote_with_two_rates_are_what_they_were_at_2_7_0(world):
    assert pages_hash(quote_pdf(world, mixed_quote(world, two_rates()))) == QUOTE_SNAPSHOT


def test_the_pages_of_a_credit_note_with_two_rates_are_what_they_were_at_2_7_0(world):
    assert pages_hash(invoice_pdf(world, make_credit_note(world.db, mixed_invoice(world, two_rates())))) == CREDIT_NOTE_SNAPSHOT


# ── Drei Gruppen: nichts ist auf zwei Sätze zugeschnitten ────────────────────────────────────────────────────────────────────────

def test_three_rates_give_three_groups_each_with_its_row_and_the_rate_5_5_percent_with_a_comma(world, three_rates):
    iid = mixed_invoice(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7), item(3, 'Reisekosten', 40.0, 5.5)])
    assert columns(world.db, iid)['MixedTaxRates'] == 1

    pdf = invoice_pdf(world, iid)

    assert table_lines(pdf, 9) == [
        HEADER,
        '1 1,00 Stk. Beratung 19 % 100,00 EUR 100,00 EUR',
        '2 1,00 Stk. Fachbuch 7 % 50,00 EUR 50,00 EUR',
        '3 1,00 Stk. Reisekosten 5,5 % 40,00 EUR 40,00 EUR',
        'Summe netto: 190,00 EUR',
        'zzgl. 19 % MwSt. auf 100,00 EUR: 19,00 EUR',
        'zzgl. 7 % MwSt. auf 50,00 EUR: 3,50 EUR',
        'zzgl. 5,5 % MwSt. auf 40,00 EUR: 2,20 EUR',
        'Gesamtbetrag: 214,70 EUR']


def test_the_totals_block_grows_by_one_row_per_group_and_moves_to_the_next_page_when_it_no_longer_fits(world, three_rates):
    """Der Summenblock bleibt zusammen und wird je Gruppe um eine Zeile höher: Mit so vielen Positionen, dass er mit drei Gruppen gerade nicht mehr auf die
    erste Seite passt, steht er mit zwei Gruppen noch dort – die dritte Gruppe schiebt ihn weiter. Nichts steht unter dem unteren Rand, nichts fehlt."""
    rates = [19, 7, 5.5]

    def pdf_with(count, kinds, number):
        positions = [item(n, f'Position {n}', 1.0, kinds[n % len(kinds)]) for n in range(1, count + 1)]
        return invoice_pdf(world, mixed_invoice(world, positions, number=number))

    first = next(count for count in range(18, 60) if block_page(pdf_with(count, rates, f'26R3{count}')) == 1)
    fits = pdf_with(first - 1, rates, f'26R4{first}')
    moves = pdf_with(first, rates, f'26R5{first}')
    two_groups = pdf_with(first, rates[:2], f'26R6{first}')

    assert block_page(fits) == 0 and block_page(moves) == 1 and block_page(two_groups) == 0
    assert page_count(moves) == 2
    kinds = collections.Counter(Decimal(str(rates[n % 3])) for n in range(1, first + 1))             # Positionen je Satz (jede 1,00 € netto)
    expected = [f'Summe netto: {format_de(to_minor(first))} EUR']
    for rate in sorted(kinds, reverse=True):
        net = Decimal(kinds[rate])
        tax = (net * rate / 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        expected.append(f'zzgl. {D._fmt_rate(rate)} MwSt. auf {format_de(to_minor(net))} EUR: {format_de(to_minor(tax))} EUR')
    on_second = page_lines(moves, 1)
    assert on_second[:4] == expected                                                                  # der Block, vollständig, oben auf der Folgeseite
    assert any(line.startswith('Gesamtbetrag:') for line in on_second)
    on_first = page_lines(moves, 0)
    assert sum(1 for line in on_first if re.fullmatch(r'\d+ 1,00 Stk\. Position \d+ (?:19|7|5,5) % 1,00 EUR 1,00 EUR', line)) == first     # alle Positionen
    assert not any(line.startswith(('Summe netto:', 'zzgl.', 'Gesamtbetrag:')) for line in on_first)
    for page in range(2):                                                                             # nichts im Inhalt unter dem unteren Rand (y ab Oberkante: 842 − 120)
        with pdfplumber.open(io.BytesIO(moves)) as document:
            body = [w for w in document.pages[page].extract_words() if w['top'] < 740]
        assert all(w['bottom'] <= 842 - 120 + 3 for w in body)


_TEXT_OP = re.compile(r'^1 0 0 1 (-?[0-9.]+) (-?[0-9.]+) Tm \((.*)\) Tj$')
_RULE_OP = re.compile(r'^[0-9.]+ (-?[0-9.]+) m [0-9.]+ -?[0-9.]+ l S$')
FOOTER_RULE_Y = 74.0                  # die Linie über der Fußzeile, die jede Seite trägt (``draw_footer_columns``: ``y_top`` 66 + 8)


def _texts(ops):
    """``(x, y, Text)`` jeder Textstelle einer Seite (die Operatoren ``1 0 0 1 x y Tm (Text) Tj``)."""
    return [(float(m.group(1)), float(m.group(2)), m.group(3)) for m in map(_TEXT_OP.match, ops) if m]


def _groups(number):
    """``number`` Gruppen mit verschiedenen Sätzen (19, 18, 17 …) – die Zahl der Gruppen ist hier das Maß."""
    return rate_groups([(Decimal(19 - n), to_minor('10.00')) for n in range(number)])


def _draw(count, groups, start=586):
    """Die Tabelle mit ``count`` Positionen und den Gruppen auf einem ``PageFlow`` ohne Fußzeile; Rückgabe: ``(Seiten, PageFlow)``. ``start``: die Höhe,
    auf der die Tabelle bei einem Beleg mit Adresse und vier Kopfzeilen beginnt."""
    flow = D.PageFlow([[], [], []])
    flow.y = start
    positions = [{'pos': n, 'quantity': 1, 'unit': 'Stk.', 'description': f'Position {n}', 'price': 1, 'total': 1,
                  'rate': groups[n % len(groups)].pct} for n in range(1, count + 1)]
    net = sum(group.net for group in groups)
    tax = sum(group.tax for group in groups)
    D.draw_item_table(flow, positions, tax_rate_pct=19, sum_net=from_minor(net), tax_amount=from_minor(tax), sum_gross=from_minor(net + tax),
                      tax_groups=groups)
    return flow.finish(), flow


@pytest.mark.parametrize('number', [1, 2, 3, 5, 8])
def test_the_totals_block_never_splits_and_nothing_stands_below_the_bottom_margin_for_any_number_of_groups(number):
    for count in range(0, 70):
        pages, flow = _draw(count, _groups(number))
        block_pages = [index for index, ops in enumerate(pages) if any(text.startswith('Summe netto:') for _x, _y, text in _texts(ops))]
        assert len(block_pages) == 1, (number, count)
        on_page = [text for _x, _y, text in _texts(pages[block_pages[0]])]
        assert sum(1 for text in on_page if text.startswith('zzgl.')) == number and 'Gesamtbetrag:' in on_page, (number, count)
        for ops in pages:
            on_it = [text for _x, _y, text in _texts(ops)]
            assert all(y >= flow.bottom for _x, y, _text in _texts(ops)), (number, count)
            assert ('USt' in on_it) == ('Pos.' in on_it), (number, count)                        # jede Seite mit der Tabelle trägt die Kopfzeile samt „USt“
            rules = [float(m.group(1)) for m in map(_RULE_OP.match, ops) if m]
            assert FOOTER_RULE_Y in rules and all(y >= flow.bottom for y in rules if y != FOOTER_RULE_Y), (number, count)


def test_each_further_group_adds_one_row_of_14_pt_and_a_single_group_is_laid_out_like_the_single_rate_block():
    def block(number):
        pages, flow = _draw(3, _groups(number))
        marks = {text: y for _x, y, text in _texts(pages[-1]) if text in ('Summe netto:', 'Gesamtbetrag:')}
        return marks, flow.y

    blocks = [block(number) for number in range(1, 7)]
    for (before, y_before), (after, y_after) in zip(blocks, blocks[1:]):
        assert before['Gesamtbetrag:'] - after['Gesamtbetrag:'] == 14 and y_before - y_after == 14
        assert before['Summe netto:'] == after['Summe netto:']

    flow = D.PageFlow([[], [], []])                                       # derselbe Beleg mit einem Satz: dieselbe Höhe, an der der Text weitergeht
    flow.y = 586
    D.draw_item_table(flow, [{'pos': n, 'quantity': 1, 'unit': 'Stk.', 'description': f'Position {n}', 'price': 1, 'total': 1} for n in range(1, 4)],
                      tax_rate_pct=19, sum_net=10, tax_amount=1.9, sum_gross=11.9)
    assert flow.y == blocks[0][1]


# ── Breiten ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_column_ust_holds_the_widest_rate_with_room_to_spare():
    for rate in (*MIXED_TAX_RATES, FIVE_AND_A_HALF, Decimal('10.7'), Decimal('12.34'), Decimal('99.99'), Decimal('100')):
        assert D.text_width(D._fmt_rate(rate), 9) + 5 <= D._RATE_COL_W, rate
    assert D.text_width('USt', 10, '/F2') <= D._RATE_COL_W


def test_the_rate_is_written_with_a_comma_and_without_superfluous_zeros():
    rates = (19, Decimal('7'), Decimal('5.5'), Decimal('10.70'), Decimal('1E+2'), 19.0, 0, 5.5)

    assert [D._fmt_rate(rate) for rate in rates] == ['19 %', '7 %', '5,5 %', '10,7 %', '100 %', '19 %', '0 %', '5,5 %']


def test_the_description_wraps_before_the_column_ust_and_the_column_leaves_room_to_the_price():
    limit = D.COL_RATE_R - D._RATE_COL_W - D.COL_DESC
    text = 'Schulungsunterlagen mit einer ziemlich langen Bezeichnung, die in der schmaleren Spalte mehrfach umbrechen muss ' * 3

    lines = D._wrap_description(text, limit, 9)

    assert len(lines) > 3 and all(D.text_width(line, 9) <= limit for line in lines)
    assert D.COL_DESC + limit + D._RATE_COL_W == D.COL_RATE_R                                       # die Bezeichnung endet links der Spalte
    assert D.COL_RATE_R == D.COL_PRICE_R - 65                                                       # und die Spalte dort, wo die Bezeichnung sonst endete


def test_no_two_words_of_a_row_overlap_and_the_rate_sits_right_aligned_under_its_header(world):
    """Gemessen am erzeugten PDF mit der längsten Bezeichnung und dem breitesten Einzelpreis (99.999,99 EUR): Kein Wort einer Zeile überlappt das nächste –
    weder Menge, Einheit, Bezeichnung noch Einzelpreis die Spalte „USt“ –, und Kopf und Sätze teilen den rechten Rand. (Die Schrift im PDF ist die
    eingebettete Liberation Sans: Wortränder weichen von den Helvetica-Werten um höchstens 2 pt ab.)"""
    long_text = 'Schulungsunterlagen mit einer ziemlich langen Bezeichnung, die in der schmaleren Spalte mehrfach umbrechen muss, ' * 2
    wide_words = ' '.join(['WWWWWWWWW'] * 4)       # je 76 pt: bis zur alten Grenze (240 pt) passen drei Wörter in eine Zeile, bis zur neuen (200 pt) nur zwei
    pdf = invoice_pdf(world, mixed_invoice(world, [item(1, long_text, 99999.99, 19), item(2, wide_words, 12.0, 7), item(3, 'Heft', 5.5, 7)]))
    tolerance = 2

    rows = page_rows(pdf)
    for text, words in rows:
        for before, after in zip(words, words[1:]):
            assert after['x0'] >= before['x1'] - 0.5, (text, before['text'], after['text'])
    header = next(words for text, words in rows if text == HEADER)
    header_right = next(w for w in header if w['text'] == 'USt')['x1']
    assert abs(header_right - D.COL_RATE_R) <= tolerance
    item_rows = [words for text, words in rows if re.match(r'^[123] [\d,]+ Stk\. ', text)]
    assert len(item_rows) == 3
    for words in item_rows:
        percent = next(w for w in words if w['text'] == '%')
        price = next(w for w in words if re.fullmatch(r'99\.999,99|12,00|5,50', w['text']))
        assert abs(percent['x1'] - header_right) <= tolerance                                        # rechtsbündig unter „USt“
        assert price['x0'] - percent['x1'] >= 3                                                      # der Einzelpreis beginnt rechts davon
    texts = [text for text, _words in rows]
    table = [words for _text, words in rows[texts.index(HEADER) + 1:texts.index(next(t for t in texts if t.startswith('Summe netto:')))]]
    descriptions = [w for words in table for w in words if D.COL_DESC - 1 <= w['x0'] < D.COL_RATE_R - 33]       # links der Sätze (die beginnen ab 33 pt vor dem Rand)
    assert len(table) > 4 and descriptions                                                                    # die lange Bezeichnung hat mehrere Zeilen
    assert all(w['x1'] <= D.COL_RATE_R - D._RATE_COL_W + tolerance for w in descriptions)                    # die Bezeichnung hört vor der Spalte auf


def test_no_label_of_a_tax_row_runs_into_its_amount_for_the_largest_amounts_the_columns_hold():
    amount = f'{format_de(to_minor("9999999.99"))} EUR'
    for rate in (19, Decimal('10.7'), Decimal('99.99')):
        label = f'zzgl. {D._fmt_rate(rate)} MwSt. auf {amount}:'
        assert D.COL_DESC + D.text_width(label, 10) + 8 <= D.COL_TOTAL_R - D.text_width(amount, 10, '/F2'), label


def test_the_label_of_a_tax_row_ends_before_its_amount_in_the_pdf(world, three_rates):
    """Gemessen am erzeugten PDF mit Netto-Summen je Gruppe von rund 9 Millionen Euro: je Steuerzeile der Abstand zwischen Ende der Bezeichnung und Anfang
    des Betrags."""
    pdf = invoice_pdf(world, mixed_invoice(world, [item(1, 'Maschine', 99999.99, 19, quantity=90), item(2, 'Lieferung', 99999.99, 5.5, quantity=90),
                                                   item(3, 'Fachbuch', 99999.99, 7, quantity=90)]))

    rows = [words for text, words in page_rows(pdf) if text.startswith('zzgl.')]

    assert len(rows) == 3
    for words in rows:
        assert words[-2]['x0'] - words[-3]['x1'] >= 8, [w['text'] for w in words]                     # Betrag und „EUR“ am Ende, davor das Ende der Bezeichnung


# ── Der Kopfsatz ist ein Platzhalter, das Kennzeichen entscheidet ────────────────────────────────────────────────────────────────

def test_a_document_without_the_flag_prints_as_always_even_if_its_positions_carry_two_rates(world):
    """Das Kennzeichen entscheidet, nie die Sätze der Positionen: Bestand (Importe, frühere Formularstände) mit abweichenden Positionssätzen ohne Kennzeichen
    druckt die eine Zeile mit der Gesamtsteuer und dem Satz im Kopf, ohne Spalte „USt“ – genau wie vor 2.7.0. In der Datenbankschicht gebaut (die Maske
    schickt so etwas seit 2.7.0 nicht mehr: ``_tax_fields`` lehnt es ab)."""
    iid = mixed_invoice(world, two_rates())
    set_column(world.db, iid, 'MixedTaxRates', None)

    pdf = invoice_pdf(world, iid)

    assert table_lines(pdf, 6, HEADER_SINGLE_RATE) == [
        HEADER_SINGLE_RATE,
        '1 2,00 Std. Beratung 50,00 EUR 100,00 EUR',
        '2 1,00 Stk. Fachbuch 50,00 EUR 50,00 EUR',
        'Summe netto: 150,00 EUR',
        'zzgl. 19% MwSt.: 22,50 EUR',
        'Gesamtbetrag: 172,50 EUR']
    assert not any('MwSt. auf' in line for line in page_lines(pdf))


@pytest.mark.parametrize('value', [0, 2, 'ja', -1])
def test_only_the_whole_number_1_is_the_flag_everything_else_in_the_column_prints_as_one_rate(world, value):
    iid = mixed_invoice(world, two_rates())
    set_column(world.db, iid, 'MixedTaxRates', value)

    lines = page_lines(invoice_pdf(world, iid))

    assert HEADER_SINGLE_RATE in lines and 'zzgl. 19% MwSt.: 22,50 EUR' in lines and not any('MwSt. auf' in line for line in lines)


def test_the_quote_without_the_flag_prints_as_always(world):
    qid = mixed_quote(world, two_rates())
    set_column(world.db, qid, 'MixedTaxRates', None)

    lines = page_lines(quote_pdf(world, qid))

    assert HEADER_SINGLE_RATE in lines and 'zzgl. 19% MwSt.: 22,50 EUR' in lines


def test_a_document_without_the_tax_shown_never_gets_the_groups_even_with_the_flag(world):
    """Ein Beleg mit Steuersatz je Position ist nie § 19 (``showTax`` falsch lehnt die Maske ab); steht in der Datenbank trotzdem beides (SQL-Konsole), gilt der
    Kopf −1: kein Steuerausweis und der Hinweis nach § 19 – wie in der E-Rechnung (Kategorie E), nicht halb die Spalte und halb keine Summen."""
    iid = mixed_invoice(world, two_rates())
    set_column(world.db, iid, 'TaxRate', -1)

    lines = page_lines(invoice_pdf(world, iid))

    assert HEADER_SINGLE_RATE in lines
    assert not any(line.startswith(('Summe netto:', 'zzgl.')) for line in lines)
    assert any('§ 19 UStG' in line for line in lines) and any(line.startswith('Gesamtbetrag:') for line in lines)


def _lay_out(items, **table):
    flow = D.PageFlow([[], [], []])
    flow.y = 586
    D.draw_item_table(flow, copy.deepcopy(items), tax_rate_pct=19, sum_net=100, tax_amount=19, sum_gross=119, **table)
    return flow.finish(), flow.y


def test_the_block_of_a_flagged_document_with_one_group_differs_from_the_single_rate_block_only_in_the_column_and_the_label():
    """Mit dem Kennzeichen und genau einer Gruppe: dieselben Zeilen, dieselben Abstände – nur die Spalte „USt“ kommt dazu, und die Steuerzeile nennt ihre Basis."""
    position = {'pos': 1, 'quantity': 1, 'unit': 'Stk.', 'description': 'Beratung', 'price': 100, 'total': 100}
    group = rate_groups([(Decimal('19'), to_minor('100.00'))])

    plain_pages, plain_y = _lay_out([position])
    flagged_pages, flagged_y = _lay_out([{**position, 'rate': Decimal('19')}], tax_groups=group)

    assert plain_y == flagged_y
    assert sorted({y for _x, y, _text in _texts(plain_pages[0])}) == sorted({y for _x, y, _text in _texts(flagged_pages[0])})
    assert [t for _x, _y, t in _texts(plain_pages[0]) if 'MwSt' in t] == ['zzgl. 19% MwSt.:']
    assert [t for _x, _y, t in _texts(flagged_pages[0]) if 'MwSt' in t] == ['zzgl. 19 % MwSt. auf 100,00 EUR:']


def test_without_groups_or_without_the_tax_shown_the_table_is_what_it_was_character_for_character():
    positions = [{'pos': 1, 'quantity': 2, 'unit': 'Std.', 'description': 'Beratung; Einrichtung', 'price': 50, 'total': 100, 'rate': Decimal('19')}]
    groups = rate_groups([(Decimal('19'), to_minor('100.00'))])

    assert _lay_out(positions) == _lay_out(positions, tax_groups=None)
    assert _lay_out(positions, show_tax=False) == _lay_out(positions, show_tax=False, tax_groups=groups)       # kein Steuerausweis: die Gruppen gelten nicht
    assert _lay_out(positions) != _lay_out(positions, tax_groups=groups)                                       # mit Gruppen ist es ein anderes Blatt


# ── Satzgruppen von PDF und E-Rechnung sind dieselben ────────────────────────────────────────────────────────────────────────────

def test_the_groups_of_the_pdf_are_the_groups_of_the_einvoice_and_the_stored_sums(world):
    iid = mixed_invoice(world, many_positions())
    invoice, positions = world.db.get_invoice_by_id(iid), world.db.get_invoice_items(iid)

    printed = per_item_rates(invoice, positions, invoice[35])
    model = build_einvoice(world.db, invoice, positions)

    assert sorted((g.pct, g.net, g.tax) for g in printed.groups) == sorted((t.rate, t.basis, t.amount) for t in model.tax_breakdown)
    stored = columns(world.db, iid)
    assert (sum(g.net for g in printed.groups), sum(g.tax for g in printed.groups), sum(g.gross for g in printed.groups)) == (
        stored['SumNet'], stored['TaxAmount'], stored['SumGross'])
    assert printed.item_rates == [Decimal('19'), Decimal('7'), Decimal('7'), Decimal('7'), Decimal('19')]
    assert [line.tax_rate for line in model.lines] == printed.item_rates


def test_a_position_without_a_rate_gets_the_rate_of_the_head_in_the_pdf_and_in_the_einvoice(world):
    """Eine Position ohne eigenen Satz (der Eingriff per SQL-Konsole; die Spalte lässt ``''`` zu) gilt in der E-Rechnung als Position mit dem Satz im Kopf –
    im PDF ebenso, damit beide dieselben Gruppen haben."""
    iid = mixed_invoice(world, two_rates())
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:
        con.execute("UPDATE InvoiceItems SET TaxRate = '' WHERE InvoiceId = ? AND Position = 2", (iid,))
        con.commit()
    invoice, positions = world.db.get_invoice_by_id(iid), world.db.get_invoice_items(iid)

    printed = per_item_rates(invoice, positions, invoice[35])
    model = build_einvoice(world.db, invoice, positions)

    assert printed.item_rates == [Decimal('19'), Decimal('19')]
    assert sorted((g.pct, g.net, g.tax) for g in printed.groups) == sorted((t.rate, t.basis, t.amount) for t in model.tax_breakdown)


def test_a_difference_to_the_stored_sums_is_printed_anyway_with_one_line_in_the_server_log(world, capsys):
    iid = mixed_invoice(world, two_rates(), number='26R210')
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:                          # ein Eingriff an der App vorbei
        con.execute('UPDATE Invoices SET TaxAmount = ?, SumGross = ? WHERE ID = ?', (to_minor('22.00'), to_minor('172.00'), iid))
        con.commit()
    capsys.readouterr()

    pdf = invoice_pdf(world, iid)

    lines = page_lines(pdf)
    assert 'zzgl. 19 % MwSt. auf 100,00 EUR: 19,00 EUR' in lines and 'zzgl. 7 % MwSt. auf 50,00 EUR: 3,50 EUR' in lines      # gedruckt wird, was die Positionen ergeben
    logged = [line for line in capsys.readouterr().out.splitlines() if 'weichen von den gespeicherten Summen' in line]
    assert len(logged) == 1 and '26R210' in logged[0] and 'Steuer 22,50' in logged[0] and 'Steuer 22,00' in logged[0]


def test_nothing_is_logged_when_the_groups_match_the_stored_sums(world, capsys):
    iid = mixed_invoice(world, two_rates(), number='26R211')
    qid = mixed_quote(world, two_rates(), number='26A211')
    capsys.readouterr()

    invoice_pdf(world, iid)
    quote_pdf(world, qid)

    assert 'weichen von den gespeicherten Summen' not in capsys.readouterr().out


def test_per_item_rates_is_none_without_the_flag_and_does_not_read_the_positions(world):
    """Der Beleg mit einem Satz bekommt weder Sätze noch Gruppen: Die Positionen werden gar nicht erst angesehen."""
    iid = mixed_invoice(world, two_rates())
    invoice = world.db.get_invoice_by_id(iid)
    flagless = invoice[:55] + (None,) + invoice[56:]

    assert per_item_rates(flagless, object(), 0.19) is None                                      # ``object()`` ließe jeden Zugriff scheitern
    assert per_item_rates(invoice[:55], object(), 0.19) is None                                  # eine Zeile ohne Spalte 55 hat das Kennzeichen nicht
    assert per_item_rates(invoice, world.db.get_invoice_items(iid), 0.19) is not None


def test_the_code_assumes_neither_two_rates_nor_the_rates_19_and_7():
    """Die erlaubten Sätze stehen an EINER Stelle (``tax_groups.MIXED_TAX_RATES``); was Gruppen bildet und druckt, kennt keinen festen Satz und keine feste
    Zahl von Gruppen."""
    sources = [inspect.getsource(D.draw_item_table), inspect.getsource(D._fmt_rate), inspect.getsource(per_item_rates),
               inspect.getsource(item_groups), inspect.getsource(item_rate)]
    for source in sources:
        for fixed in ("Decimal('19')", "Decimal('7')", '== 19', '== 7', '19 and 7', '[19, 7]', '(19, 7)', 'in (19', 'in (7', 'len(tax_groups) == ',
                      'len(groups) == '):
            assert fixed not in source, fixed


# ── Die E-Rechnung eines Belegs mit Steuersatz je Position ───────────────────────────────────────────────────────────────────────

def _local(element):
    return element.tag.rsplit('}', 1)[-1]


def _descendants(root, name):
    return [element for element in root.iter() if _local(element) == name]


def _child_text(element, name):
    return next(child.text for child in element if _local(child) == name)


def _round_cent(value):
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def check_the_rules_of_the_norm(groups, line_nets, line_rates, tax_total, line_total, tax_basis, grand_total):
    """Die Regeln der Norm am erzeugten XML: Steuer je Gruppe = Basis × Satz auf den Cent (BR-CO-17), Basis je Gruppe = Summe der Positionen dieses Satzes,
    Steuer des Belegs = Summe der Gruppen (BR-CO-14), Summe der Positionen = Netto (BR-CO-10), Brutto = Netto + Steuer (BR-CO-15)."""
    for rate, basis, amount in groups:
        assert amount == _round_cent(basis * rate / 100), (rate, basis, amount)
        assert basis == sum(net for net, line_rate in zip(line_nets, line_rates) if line_rate == rate)
    assert tax_total == sum(amount for _r, _b, amount in groups)
    assert tax_basis == sum(basis for _r, basis, _a in groups) == line_total == sum(line_nets)
    assert grand_total == tax_basis + tax_total


def test_the_embedded_cii_of_a_mixed_invoice_has_two_tax_groups_and_follows_the_rules_of_the_norm(world):
    iid = mixed_invoice(world, many_positions())
    stored = columns(world.db, iid)
    cii = embedded_xml(invoice_pdf(world, iid))
    root = ET.fromstring(cii)
    facts = cii_facts(cii)

    groups = sorted((Decimal(t['rate']), Decimal(t['basis']), Decimal(t['amount'])) for t in facts['tax'])
    assert groups == [(Decimal('7'), Decimal('12.54'), Decimal('0.88')), (Decimal('19'), Decimal('199.98'), Decimal('38.00'))]
    lines = _descendants(root, 'IncludedSupplyChainTradeLineItem')
    line_rates = [Decimal(_child_text(_descendants(line, 'ApplicableTradeTax')[0], 'RateApplicablePercent')) for line in lines]
    line_nets = [Decimal(_child_text(_descendants(line, 'SpecifiedTradeSettlementLineMonetarySummation')[0], 'LineTotalAmount')) for line in lines]
    assert line_rates == [Decimal('19'), Decimal('7'), Decimal('7'), Decimal('7'), Decimal('19')]                             # je Position ihr Satz
    totals = facts['totals']
    check_the_rules_of_the_norm(groups, line_nets, line_rates, Decimal(totals['TaxTotalAmount']), Decimal(totals['LineTotalAmount']),
                                Decimal(totals['TaxBasisTotalAmount']), Decimal(totals['GrandTotalAmount']))
    assert (to_minor(totals['TaxBasisTotalAmount']), to_minor(totals['TaxTotalAmount']), to_minor(totals['GrandTotalAmount'])) == (
        stored['SumNet'], stored['TaxAmount'], stored['SumGross'])                                                     # und die gespeicherten Summen des Belegs


def test_the_ubl_of_a_mixed_invoice_has_two_tax_subtotals_and_follows_the_rules_of_the_norm(world):
    iid = mixed_invoice(world, many_positions())
    stored = columns(world.db, iid)
    xml = XRechnungGenerator().generate_xml(world.db.get_invoice_by_id(iid), world.db.get_invoice_items(iid)).encode('utf-8')
    root = ET.fromstring(xml)
    facts = ubl_facts(xml)

    groups = sorted((Decimal(t['Percent']), Decimal(t['basis']), Decimal(t['amount'])) for t in facts['tax'])
    assert groups == [(Decimal('7'), Decimal('12.54'), Decimal('0.88')), (Decimal('19'), Decimal('199.98'), Decimal('38.00'))]
    assert all(t['ID'] == 'S' for t in facts['tax'])
    lines = _descendants(root, 'InvoiceLine')
    line_rates = [Decimal(_child_text(_descendants(line, 'ClassifiedTaxCategory')[0], 'Percent')) for line in lines]
    line_nets = [Decimal(_child_text(line, 'LineExtensionAmount')) for line in lines]
    assert line_rates == [Decimal('19'), Decimal('7'), Decimal('7'), Decimal('7'), Decimal('19')]
    tax_total = next(Decimal(_child_text(element, 'TaxAmount')) for element in root if _local(element) == 'TaxTotal')
    totals = facts['totals']
    check_the_rules_of_the_norm(groups, line_nets, line_rates, tax_total, Decimal(totals['LineExtensionAmount']), Decimal(totals['TaxExclusiveAmount']),
                                Decimal(totals['TaxInclusiveAmount']))
    assert (to_minor(totals['TaxExclusiveAmount']), to_minor(tax_total), to_minor(totals['TaxInclusiveAmount'])) == (
        stored['SumNet'], stored['TaxAmount'], stored['SumGross'])


def test_three_groups_in_the_einvoice_too(world, three_rates):
    iid = mixed_invoice(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7), item(3, 'Reisekosten', 40.0, 5.5)])

    facts = cii_facts(embedded_xml(invoice_pdf(world, iid)))

    assert sorted((Decimal(t['rate']), Decimal(t['basis']), Decimal(t['amount'])) for t in facts['tax']) == [
        (Decimal('5.5'), Decimal('40.00'), Decimal('2.20')), (Decimal('7'), Decimal('50.00'), Decimal('3.50')),
        (Decimal('19'), Decimal('100.00'), Decimal('19.00'))]
    assert facts['totals']['TaxTotalAmount'] == '24.70' and facts['totals']['GrandTotalAmount'] == '214.70'


def test_a_mixed_invoice_has_no_gap_beyond_the_ones_of_any_invoice(world):
    """Der Entwurf aus der Maske braucht nichts Besonderes: Die Lücken der E-Rechnung hängen nicht von den Steuersätzen ab."""
    mixed = mixed_invoice(world, two_rates())
    plain = save(make_payload(world, invoiceNumber='26R299'))['invoice_id']

    def gaps(invoice_id):
        model = build_einvoice(world.db, world.db.get_invoice_by_id(invoice_id), world.db.get_invoice_items(invoice_id))
        return einvoice_model.gap_texts(einvoice_model.einvoice_gaps(model))

    assert gaps(mixed) == gaps(plain)


# ── Die Beispiele der E-Rechnung (tools/make_einvoice_samples.py) ────────────────────────────────────────────────────────────────

def _samples_module():
    """Das Werkzeug des CI-Jobs ``einvoice`` (kein Paket: wie die anderen Skripte unter ``tools/`` über den Pfad geladen)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'make_einvoice_samples.py')
    spec = importlib.util.spec_from_file_location('make_einvoice_samples', path)
    module = importlib.util.module_from_spec(spec)
    search_path = list(sys.path)                           # das Skript stellt beim Laden den Projektordner voran
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = search_path
    return module


def test_the_normal_case_of_the_samples_is_never_a_document_with_the_flag_whose_head_is_only_a_placeholder(world):
    plain = save(make_payload(world, invoiceNumber='26R231', invoiceDate='2026-06-08'))['invoice_id']
    mixed = mixed_invoice(world, two_rates(), number='26R230', invoiceDate='2026-06-12')

    assert columns(world.db, mixed)['TaxRate'] == 0.19                   # der Platzhalter sähe wie ein Satz von 19 % aus …
    assert world.db.fetch_invoices()[0][0] == mixed                      # … und der Beleg ist der jüngste dieser Art
    assert _samples_module()._normal_invoice(world.db)[0] == plain


def test_the_copy_of_the_samples_takes_the_flag_along_with_the_rates_of_the_positions(world):
    samples = _samples_module()
    mixed = mixed_invoice(world, two_rates())
    plain = save(make_payload(world, invoiceNumber='26R232'))['invoice_id']
    sql = 'SELECT Position, Quantity, PricePerUnit, TotalNet, TaxRate FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position'

    clone = samples._clone_for_correction(world.db, mixed, 'KP-1')
    plain_clone = samples._clone_for_correction(world.db, plain, 'KP-2')

    assert columns(world.db, clone)['MixedTaxRates'] == 1                 # nie zwei Sätze ohne Kennzeichen
    assert query_rows(world.db, sql, clone) == query_rows(world.db, sql, mixed) and [r['TaxRate'] for r in query_rows(world.db, sql, clone)] == [19.0, 7.0]
    stored, copied = columns(world.db, mixed), columns(world.db, clone)
    assert [copied[key] for key in ('SumNet', 'TaxAmount', 'SumGross', 'TaxRate')] == [stored[key] for key in ('SumNet', 'TaxAmount', 'SumGross', 'TaxRate')]
    assert columns(world.db, plain_clone)['MixedTaxRates'] is None        # ein Beleg mit einem Satz bleibt ohne


def test_the_mixed_sample_is_a_draft_from_the_form_with_two_groups_and_no_gap_in_the_einvoice(world):
    samples = _samples_module()
    template = save(make_payload(world, invoiceNumber='26R233'))['invoice_id']

    draft = samples._mixed_rates_draft(world.db, template)

    stored = columns(world.db, draft)
    assert (stored['Status'], stored['MixedTaxRates'], stored['BuyerRouteID']) == ('draft', 1, samples.LEITWEG_ID)
    invoice, positions = world.db.get_invoice_by_id(draft), world.db.get_invoice_items(draft)
    assert [(g.pct, g.net, g.tax) for g in per_item_rates(invoice, positions, invoice[35]).groups] == [
        (Decimal('19'), to_minor('310.00'), to_minor('58.90')), (Decimal('7'), to_minor('49.80'), to_minor('3.49'))]
    assert (stored['SumNet'], stored['TaxAmount'], stored['SumGross']) == (to_minor('359.80'), to_minor('62.39'), to_minor('422.19'))
    assert einvoice_model.gap_texts(einvoice_model.einvoice_gaps(build_einvoice(world.db, invoice, positions))) == []
