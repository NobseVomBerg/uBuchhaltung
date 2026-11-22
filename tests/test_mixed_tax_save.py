# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Speichern von Rechnung und Angebot mit Steuersatz je Position (2.7.0, ``server/handlers.py``: ``_tax_fields``).

Eine Funktion prüft die Steuerangaben des Formulars für BEIDE Speicherwege (``handle_invoice_save``, ``handle_quote_save``), nach
drei Regeln in dieser Reihenfolge – dazu die Angabe ``taxMode`` (fehlt/leer oder ``'mixed'``; alles andere ist ungültig):

1. Ohne Steuerausweis (``showTax`` falsch): ``taxMode`` ``'mixed'`` wird abgelehnt; sonst Kopf −1 und jede Position bekommt den Satz 0.
2. Mit Steuerausweis, ohne ``taxMode`` (das, was die Maske heute schickt): Weicht der Satz einer Position vom Satz des Kopfs ab, wird
   abgelehnt; sonst bleibt alles, wie es war – Zeile für Zeile.
3. ``taxMode`` ``'mixed'``: Jede Position braucht einen eigenen Satz aus ``tax_groups.MIXED_TAX_RATES`` (19 % und 7 %). Tragen alle
   denselben, ist es ein gewöhnlicher Beleg (Kopf = dieser Satz, Kennzeichen NULL); bei mindestens zwei verschiedenen ist der Kopf der
   höchste Satz als Platzhalter und ``Invoices.MixedTaxRates`` = 1.

Jede Ablehnung kommt mit festem Klartext vor dem ersten Schreiben: nichts gespeichert, keine Rechnungsnummer verbraucht. Der Beleg mit
einem Satz verhält sich wie vorher (Eigenschaftstest gegen die Rechenweise von 2.6.8; die Schnappschüsse stehen in
``tests/test_single_rate_snapshots.py``). Die Kopierwege – Angebot → Rechnung, Stornorechnung, Ersatzrechnung – nehmen das Kennzeichen
und die Sätze der Positionen mit.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import contextlib
import copy
import datetime
import json
import pathlib
import sqlite3
from decimal import Decimal

import pytest

import server.handlers as handlers
from db.invoice_lock import LOCKED_MESSAGE, invoice_row
from db.invoice_storno import InvoiceStornoMixin
from money import to_minor
from server.handlers import (TAX_ITEM_DIFFERS, TAX_ITEM_MISSING, TAX_ITEM_NOT_ALLOWED, TAX_MODE_INVALID,
                             TAX_PER_ITEM_WITHOUT_TAX)
from tax_groups import MIXED_TAX_RATES
from tests.invoice_world import (ITEMS, build_world, columns, force_status, make_invoice, make_payload, query_rows, save,
                                 save_raw, snapshot)


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return build_world(tmp_db)


def item(position, description, price, rate, quantity=1):
    """Eine Position, wie die Maske sie schickt (Satz als Prozentzahl)."""
    return {'position': position, 'quantity': quantity, 'unit': 'C62', 'description': description, 'unitPrice': price,
            'taxRate': rate}


def mixed(world, items, **over):
    """Das Formular mit dem Häkchen „Steuersatz je Position“: ``taxMode`` ``'mixed'``, die Sätze stehen an den Positionen."""
    return make_payload(world, items=items, taxMode='mixed', **over)


def stored_items(db, invoice_id):
    return query_rows(db, 'SELECT Position, Description, TotalNet, TaxRate FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position',
                      invoice_id)


def head(db, invoice_id):
    """Was der Kopf an Steuerangaben speichert: Satz, Kennzeichen und die Summen (Minor Units)."""
    row = columns(db, invoice_id)
    return {key: row[key] for key in ('TaxRate', 'MixedTaxRates', 'SumNet', 'TaxAmount', 'SumGross', 'AmountDue')}


def minor(netto, steuer, brutto):
    return {'SumNet': to_minor(netto), 'TaxAmount': to_minor(steuer), 'SumGross': to_minor(brutto), 'AmountDue': to_minor(brutto)}


def refused(payload):
    """Das Speichern lehnt ab: ``(Klartext, Statuscode)``; nichts darf sich geändert haben (prüft der Aufrufer)."""
    resp = save_raw(payload)
    answer = json.loads(resp)
    assert answer['success'] is False, answer
    return answer['error'], resp.status


# ── Zwei Sätze in einem Beleg ────────────────────────────────────────────────

def test_one_position_at_19_and_one_at_7_make_a_mixed_invoice_with_the_tax_of_each_group(world):
    """100,00 € zu 19 % und 50,00 € zu 7 %: netto 150,00, Steuer 19,00 + 3,50 = 22,50, brutto 172,50."""
    answer = save(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]))

    assert answer['success'] is True
    stored = head(world.db, answer['invoice_id'])
    assert stored == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('150.00', '22.50', '172.50')}
    assert [(r['Position'], r['TotalNet'], r['TaxRate']) for r in stored_items(world.db, answer['invoice_id'])] == [
        (1, to_minor('100.00'), 19.0), (2, to_minor('50.00'), 7.0)]                       # jede Position mit ihrem Satz


def test_the_head_is_the_highest_rate_whatever_the_order_of_the_positions(world):
    answer = save(mixed(world, [item(1, 'Fachbuch', 50.0, 7), item(2, 'Beratung', 100.0, 19), item(3, 'Heft', 10.0, 7)]))

    stored = head(world.db, answer['invoice_id'])
    assert (stored['TaxRate'], stored['MixedTaxRates']) == (0.19, 1)
    assert stored['SumNet'] == to_minor('160.00') and stored['TaxAmount'] == to_minor('19.00') + to_minor('4.20')


def test_the_tax_is_rounded_per_group_not_per_position(world):
    """Drei Positionen zu 7 % über je 0,10 €: Die Gruppe hat 0,30 € netto und 0,021 € Steuer = 0,02 € – je Position gerundet wären es 0,03 €."""
    answer = save(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Heft A', 0.10, 7), item(3, 'Heft B', 0.10, 7),
                                item(4, 'Heft C', 0.10, 7)]))

    stored = head(world.db, answer['invoice_id'])
    assert stored['MixedTaxRates'] == 1
    assert stored['TaxAmount'] == to_minor('19.00') + to_minor('0.02')
    assert stored['SumGross'] == to_minor('100.30') + stored['TaxAmount']


def test_all_positions_at_7_percent_make_an_ordinary_invoice_even_in_mixed_mode(world):
    """Tragen alle Positionen denselben Satz, ist es ein gewöhnlicher Beleg: Kopf 0,07, kein Kennzeichen. Rundung je Gruppe: 0,02 €."""
    answer = save(mixed(world, [item(1, 'Heft A', 0.10, 7), item(2, 'Heft B', 0.10, 7), item(3, 'Heft C', 0.10, 7)]))

    assert head(world.db, answer['invoice_id']) == {'TaxRate': 0.07, 'MixedTaxRates': None, **minor('0.30', '0.02', '0.32')}
    assert [r['TaxRate'] for r in stored_items(world.db, answer['invoice_id'])] == [7.0, 7.0, 7.0]


def test_all_positions_at_19_percent_in_mixed_mode_also_make_an_ordinary_invoice(world):
    answer = save(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Material', 50.5, 19)]))

    assert head(world.db, answer['invoice_id']) == {'TaxRate': 0.19, 'MixedTaxRates': None, **minor('150.50', '28.60', '179.10')}


def test_the_rate_of_a_position_is_stored_as_the_form_sends_it(world):
    """Je Position steht, was die Maske schickt – nicht der normalisierte Wert (``Decimal('10').normalize()`` ist ``1E+1``)."""
    answer = save(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7.0)]))

    with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:
        rows = con.execute('SELECT TaxRate, typeof(TaxRate) FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position',
                           (answer['invoice_id'],)).fetchall()
    assert rows == [(19.0, 'real'), (7.0, 'real')]


def test_a_rate_written_as_a_fraction_counts_as_the_same_rate(world):
    """0,19 und 19 sind derselbe Satz (``tax_groups.rate_pct``); gespeichert wird, was kam."""
    answer = save(mixed(world, [item(1, 'Beratung', 100.0, 0.19), item(2, 'Fachbuch', 50.0, 7)]))

    stored = head(world.db, answer['invoice_id'])
    assert (stored['MixedTaxRates'], stored['TaxRate'], stored['TaxAmount']) == (1, 0.19, to_minor('22.50'))
    assert [r['TaxRate'] for r in stored_items(world.db, answer['invoice_id'])] == [0.19, 7.0]


def test_the_allowed_rates_are_one_list_and_the_code_does_not_assume_two(world):
    """Die erlaubten Sätze stehen an EINER Stelle; die Gruppen rechnen für beliebig viele Sätze (kein Satz „fest“ im Handler)."""
    assert MIXED_TAX_RATES == (Decimal('19'), Decimal('7'))
    source = pathlib.Path(handlers.__file__).read_text(encoding='utf-8')
    body = source[source.index('def _tax_fields'):source.index('def handle_invoice_save')]
    assert 'MIXED_TAX_RATES' in body
    for fixed in ("Decimal('19')", "Decimal('7')", '== 19', '== 7', '19 and 7', '[19, 7]', '(19, 7)'):
        assert fixed not in body, fixed


# ── Von gemischt zurück auf einen Satz (Entwurf) ─────────────────────────────

def test_a_mixed_draft_that_goes_back_to_one_rate_loses_the_flag(world):
    first = save(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]))
    invoice_id = first['invoice_id']
    assert head(world.db, invoice_id)['MixedTaxRates'] == 1

    again = save(make_payload(world, invoiceId=invoice_id, invoiceNumber=columns(world.db, invoice_id)['InvoiceNumber'],
                              items=copy.deepcopy(ITEMS)))                    # die Maske ohne Häkchen: ein Satz, 19 % an jeder Position

    assert again == {'success': True, 'invoice_id': invoice_id}
    assert head(world.db, invoice_id) == {'TaxRate': 0.19, 'MixedTaxRates': None, **minor('250.50', '47.60', '298.10')}
    assert [r['TaxRate'] for r in stored_items(world.db, invoice_id)] == [19.0, 19.0]       # die Positionen sind ersetzt


def test_a_draft_with_one_rate_can_become_a_mixed_one(world):
    invoice_id = make_invoice(world)
    assert head(world.db, invoice_id)['MixedTaxRates'] is None

    number = columns(world.db, invoice_id)['InvoiceNumber']
    answer = save(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)], invoiceId=invoice_id,
                        invoiceNumber=number))

    assert answer == {'success': True, 'invoice_id': invoice_id}
    assert head(world.db, invoice_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('150.00', '22.50', '172.50')}


def test_a_mixed_draft_that_goes_down_to_7_percent_only_keeps_the_placeholder_out(world):
    invoice_id = save(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]))['invoice_id']
    number = columns(world.db, invoice_id)['InvoiceNumber']

    save(mixed(world, [item(1, 'Fachbuch', 50.0, 7)], invoiceId=invoice_id, invoiceNumber=number))

    assert head(world.db, invoice_id) == {'TaxRate': 0.07, 'MixedTaxRates': None, **minor('50.00', '3.50', '53.50')}


# ── Regel 1: ohne Steuerausweis ──────────────────────────────────────────────

def test_without_the_tax_shown_a_mixed_form_is_refused(world):
    before = snapshot(world.db)

    text, status = refused(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)], showTax=False, taxRate=0))

    assert text == TAX_PER_ITEM_WITHOUT_TAX == 'Ohne Umsatzsteuerausweis gibt es keinen Steuersatz je Position.'
    assert status == 200 and snapshot(world.db) == before


def test_without_the_tax_shown_every_position_gets_rate_0_and_the_head_is_minus_1(world):
    """Die Maske schickt dort schon 0; was ein anderer Client an Sätzen mitschickt, nimmt der Server nicht an."""
    mask = save(make_payload(world, invoiceNumber='26R200', showTax=False, taxRate=0,
                             items=[item(1, 'Beratung', 100.0, 0), item(2, 'Fachbuch', 50.0, 0)]))
    odd = save(make_payload(world, invoiceNumber='26R201', showTax=False, taxRate=0.19,
                            items=[item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]))

    for answer in (mask, odd):
        stored = head(world.db, answer['invoice_id'])
        assert stored == {'TaxRate': -1.0, 'MixedTaxRates': None, **minor('150.00', '0.00', '150.00')}
        assert [r['TaxRate'] for r in stored_items(world.db, answer['invoice_id'])] == [0.0, 0.0]


def test_without_the_tax_shown_the_positions_without_a_rate_get_0_too(world):
    answer = save(make_payload(world, showTax=False, taxRate=0,
                               items=[{'position': 1, 'quantity': 1, 'description': 'Beratung', 'unitPrice': 100.0}]))

    assert head(world.db, answer['invoice_id']) == {'TaxRate': -1.0, 'MixedTaxRates': None, **minor('100.00', '0.00', '100.00')}
    assert [r['TaxRate'] for r in stored_items(world.db, answer['invoice_id'])] == [0.0]


# ── Regel 2: das heutige Formular ────────────────────────────────────────────

def test_a_position_that_differs_from_the_head_is_refused_without_the_mode(world):
    before = snapshot(world.db)

    text, status = refused(make_payload(world, items=[item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]))

    assert text == TAX_ITEM_DIFFERS.format(n=2) == 'Position 2: Der Steuersatz weicht vom Steuersatz des Belegs ab.'
    assert status == 200 and snapshot(world.db) == before                      # nichts gespeichert, keine Nummer verbraucht


def test_the_first_position_that_differs_is_named(world):
    text, _status = refused(make_payload(world, items=[item(1, 'A', 1.0, 19), item(2, 'B', 1.0, 19), item(3, 'C', 1.0, 7),
                                                        item(4, 'D', 1.0, 0)]))

    assert text == 'Position 3: Der Steuersatz weicht vom Steuersatz des Belegs ab.'


@pytest.mark.parametrize('head_rate, rates', [
    (0.19, [19, 19]), (19, [19, 19]), (0.07, [7, 7]), (0.0, [0, 0]), (0, [0, 0]), (0.05, [5, 5]), (0.16, [16, 16]),
    (0.055, [5.5, 5.5]), ('0.19', ['19', 19]), (0.19, [0.19, 19])], ids=lambda value: json.dumps(value))
def test_every_shape_the_form_sends_today_passes_the_check(world, head_rate, rates):
    items = [item(position, f'Posten {position}', 10.0, rate) for position, rate in enumerate(rates, 1)]

    answer = save(make_payload(world, taxRate=head_rate, items=items))

    assert answer['success'] is True, answer
    assert head(world.db, answer['invoice_id'])['MixedTaxRates'] is None


def test_the_head_rate_is_stored_as_sent_not_normalised(world):
    """Wie heute: ``taxRate`` des Kopfs und der Positionen stehen so in der Datenbank, wie das Formular sie schickt."""
    for number, rate_head, rate_item in (('26R301', 0.19, 19), ('26R302', 19, 19), ('26R303', 0.07, 7), ('26R304', 0.055, 5.5)):
        answer = save(make_payload(world, invoiceNumber=number, taxRate=rate_head, items=[item(1, 'Beratung', 100.0, rate_item)]))
        assert columns(world.db, answer['invoice_id'])['TaxRate'] == float(rate_head)
        assert [r['TaxRate'] for r in stored_items(world.db, answer['invoice_id'])] == [float(rate_item)]


# ── Regel 3: jede Position braucht einen erlaubten Satz ──────────────────────

@pytest.mark.parametrize('missing', [None, ''], ids=['null', 'leer'])
def test_a_mixed_form_needs_a_rate_at_every_position(world, missing):
    before = snapshot(world.db)
    second = {'position': 2, 'quantity': 1, 'description': 'Fachbuch', 'unitPrice': 50.0, 'taxRate': missing}

    text, status = refused(mixed(world, [item(1, 'Beratung', 100.0, 19), second]))

    assert text == TAX_ITEM_MISSING.format(n=2) == 'Position 2: Bitte einen Steuersatz wählen.'
    assert status == 200 and snapshot(world.db) == before


def test_a_mixed_form_without_the_key_is_refused_too_and_the_head_does_not_fill_it_in(world):
    before = snapshot(world.db)

    text, _status = refused(mixed(world, [{'position': 1, 'quantity': 1, 'description': 'Beratung', 'unitPrice': 100.0},
                                          item(2, 'Fachbuch', 50.0, 7)]))

    assert text == 'Position 1: Bitte einen Steuersatz wählen.' and snapshot(world.db) == before


@pytest.mark.parametrize('rate, shown', [(5, '5'), (16, '16'), (0, '0'), (10, '10'), (5.5, '5.5'), (20, '20'), (100, '100'), (1, '1'),
                                         (0.5, '50'), (12.34, '12.34'), (19.5, '19.5'), (7.01, '7.01'), (1000000, '1000000')])
def test_a_rate_outside_the_allowed_ones_is_refused_with_its_name(world, rate, shown):
    """Der Satz steht in der Meldung, wie die Seite Prozentzahlen zeigt – ohne Exponent (``Decimal('10').normalize()`` ist ``1E+1``)."""
    before = snapshot(world.db)

    text, status = refused(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, rate)]))

    assert text == TAX_ITEM_NOT_ALLOWED.format(n=2, rate=shown)
    assert text == f'Position 2: Der Steuersatz {shown} % ist in einem Beleg mit Steuersatz je Position nicht wählbar.'
    assert 'E+' not in text and status == 200 and snapshot(world.db) == before


def test_the_first_position_without_an_allowed_rate_is_named_even_if_a_later_one_misses_its_rate(world):
    text, _status = refused(mixed(world, [item(1, 'A', 1.0, 19), item(2, 'B', 1.0, 5),
                                          {'position': 3, 'quantity': 1, 'description': 'C', 'unitPrice': 1.0}]))

    assert text == 'Position 2: Der Steuersatz 5 % ist in einem Beleg mit Steuersatz je Position nicht wählbar.'


def test_a_rate_that_is_no_number_is_refused_without_the_text_of_the_exception(world):
    before = snapshot(world.db)

    text, _status = refused(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 'sieben')]))

    assert text.startswith('Speichern fehlgeschlagen: ') and text.endswith('es wurde nichts gespeichert.')
    for leak in ('Decimal', 'InvalidOperation', 'Traceback', 'sieben'):
        assert leak not in text
    assert snapshot(world.db) == before


# ── Die Angabe taxMode ───────────────────────────────────────────────────────

@pytest.mark.parametrize('mode', ['MIXED', 'Mixed', 'single', 'x', ' mixed', 'mixed ', 1, 0, True, False, [], ['mixed'], {}, 1.5],
                         ids=lambda value: json.dumps(value))
@pytest.mark.parametrize('show_tax', [True, False], ids=['mit-Steuer', 'ohne-Steuer'])
def test_every_other_value_of_the_mode_is_refused_with_a_fixed_sentence(world, mode, show_tax):
    before = snapshot(world.db)
    rate = 0.19 if show_tax else 0

    text, status = refused(make_payload(world, taxMode=mode, showTax=show_tax, taxRate=rate))

    assert text == TAX_MODE_INVALID == 'Ungültige Angabe zum Steuersatz.'
    assert status == 200 and snapshot(world.db) == before


@pytest.mark.parametrize('mode', [None, ''], ids=['null', 'leer'])
def test_a_missing_or_empty_mode_is_the_form_of_today(world, mode):
    answer = save(make_payload(world, taxMode=mode))

    assert head(world.db, answer['invoice_id'])['MixedTaxRates'] is None


def test_the_mode_is_checked_before_the_rules_of_the_rates(world):
    """Ein ungültiger Modus wird zuerst abgelehnt – auch wenn eine Position vom Kopf abwiche."""
    text, _status = refused(make_payload(world, taxMode='x', items=[item(1, 'A', 1.0, 19), item(2, 'B', 1.0, 7)]))

    assert text == TAX_MODE_INVALID


# ── Der Beleg mit einem Satz bleibt, wie er war ──────────────────────────────

def _before_2_7_0(data):
    """Was ``handlers._invoice_fields`` bis 2.6.8 aus den Steuerangaben des Formulars machte: Kopfsatz, Sätze der Positionen und die
    Summen (aus dem Code von damals kopiert)."""
    tax_rate = data.get('taxRate')
    show_tax = data.get('showTax', True)
    items = data['items']
    net, tax, gross, line_nets = handlers.recompute_invoice_totals(items, handlers._rate_to_pct(tax_rate if show_tax else 0))
    return (tax_rate if show_tax else -1, [i.get('taxRate', tax_rate) for i in items], net, tax, gross, line_nets)


def _shapes():
    """Die Formulare, die die Maske (und der Erzeuger der Demodaten) schicken: ein Satz je Beleg."""
    for rate in (19, 7, 0, 5, 16, 5.5):
        for count in (1, 2, 5):
            items = [item(n, f'Posten {n}', 12.34 * n + 0.01, rate, quantity=n) for n in range(1, count + 1)]
            yield f'{rate}-x{count}', {'taxRate': rate / 100, 'showTax': True, 'items': items}
    yield 'Prozent-im-Kopf', {'taxRate': 19, 'showTax': True, 'items': [item(1, 'A', 9.99, 19), item(2, 'B', 0.10, 19)]}
    yield 'ohne-Satz-an-den-Positionen', {'taxRate': 0.19, 'showTax': True,
                                          'items': [{'position': 1, 'quantity': 3, 'description': 'A', 'unitPrice': 0.10},
                                                    {'position': 2, 'quantity': 1, 'description': 'B', 'unitPrice': 5.0}]}
    yield 'ohne-Steuerausweis', {'taxRate': 0, 'showTax': False, 'items': [item(1, 'A', 99.99, 0), item(2, 'B', 0.01, 0)]}
    yield 'ohne-Steuerausweis-ohne-Satz', {'taxRate': 0, 'showTax': False,
                                           'items': [{'position': 1, 'quantity': 1, 'description': 'A', 'unitPrice': 99.99}]}
    yield 'showTax-fehlt', {'taxRate': 0.07, 'items': [item(1, 'A', 9.99, 7)]}


@pytest.mark.parametrize('key, shape', list(_shapes()), ids=[key for key, _shape in _shapes()])
def test_a_form_with_one_rate_gives_exactly_what_it_gave_before_2_7_0(world, key, shape):
    data = make_payload(world, **copy.deepcopy(shape))
    expected = _before_2_7_0(data)

    invoice_data, item_rows = handlers._invoice_fields(world.db, data, None)

    assert (invoice_data['tax_rate'], [row['tax_rate'] for row in item_rows], invoice_data['sum_net'], invoice_data['tax_amount'],
            invoice_data['sum_gross'], [row['total_net'] for row in item_rows]) == expected
    assert invoice_data['mixed_tax_rates'] is None
    assert [type(row['tax_rate']) for row in item_rows] == [type(rate) for rate in expected[1]]      # auch die Art des Werts bleibt


@pytest.mark.parametrize('key, shape', list(_shapes()), ids=[key for key, _shape in _shapes()])
def test_a_form_with_one_rate_is_saved_to_the_same_rows_as_before_2_7_0(world, key, shape):
    answer = save(make_payload(world, **copy.deepcopy(shape)))
    assert answer['success'] is True, answer
    expected = _before_2_7_0(make_payload(world, **copy.deepcopy(shape)))

    stored = columns(world.db, answer['invoice_id'])
    assert stored['TaxRate'] == float(expected[0]) and stored['MixedTaxRates'] is None
    assert [r['TaxRate'] for r in stored_items(world.db, answer['invoice_id'])] == [float(rate) for rate in expected[1]]
    assert (stored['SumNet'], stored['TaxAmount'], stored['SumGross']) == (to_minor(expected[2]), to_minor(expected[3]), to_minor(expected[4]))


# ── Eine versendete Rechnung ─────────────────────────────────────────────────

def test_changing_the_rate_of_one_position_of_a_sent_invoice_is_refused_by_the_lock(world):
    """Eine versendete Rechnung schickt keine Positionen, und was sie schickt, ändert sie nicht: Der Satz einer Position ist gesperrt
    (``_items_differ`` erkennt ihn). Das Formular mit Häkchen „Steuersatz je Position“ wäre eine Änderung → 409, nichts geschrieben."""
    invoice_id = make_invoice(world, 'sent')
    before = snapshot(world.db)

    resp = save_raw(mixed(world, [{**ITEMS[0], 'taxRate': 7}, ITEMS[1]], invoiceId=invoice_id, documentVersion=1))

    answer = json.loads(resp)
    assert resp.status == 409 and answer['success'] is False and answer['error'].startswith(LOCKED_MESSAGE)
    assert snapshot(world.db) == before


def test_a_sent_invoice_resubmitted_as_it_is_changes_nothing_and_a_deviating_position_needs_the_mode(world):
    """Dasselbe Formular, unverändert zurückgeschickt, ändert nichts. Weicht eine Position vom Kopf ab, lehnt schon die Prüfung der
    Sätze ab (200 mit Klartext, nichts geschrieben) – ein Satz je Position verlangt das Häkchen (``taxMode``), und dann greift die
    Sperre (siehe oben)."""
    invoice_id = make_invoice(world, 'sent')
    before = snapshot(world.db)

    unchanged = save_raw(make_payload(world, invoiceId=invoice_id, documentVersion=1))
    assert json.loads(unchanged) == {'success': True, 'invoice_id': invoice_id} and snapshot(world.db) == before
    differs = save_raw(make_payload(world, invoiceId=invoice_id, documentVersion=1,
                                    items=[ITEMS[0], {**ITEMS[1], 'taxRate': 7}]))
    assert json.loads(differs) == {'success': False, 'error': TAX_ITEM_DIFFERS.format(n=2)} and snapshot(world.db) == before


def test_a_sent_invoice_takes_its_open_fields_without_touching_the_flag(world):
    invoice_id = make_invoice(world, 'sent')

    answer = save({'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'})

    assert answer['success'] is True
    stored = columns(world.db, invoice_id)
    assert stored['MixedTaxRates'] is None and stored['OrderNumber'] == 'B-99'


# ── Das Angebot ──────────────────────────────────────────────────────────────

def quote_payload(world, **over):
    payload = {'quoteNumber': '26A100', 'quoteDate': '2026-06-07', 'validUntil': '2026-07-07', 'customerId': world.cust,
               'ownCompanyId': world.own, 'taxRate': 0.19, 'status': 'draft', 'items': copy.deepcopy(ITEMS),
               'introText': 'Guten Tag', 'closingText': 'Mit freundlichen Grüßen'}
    payload.update(over)
    return payload


def save_quote(payload):
    return json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))


def test_a_quote_can_carry_a_rate_per_position(world):
    answer = save_quote(quote_payload(world, taxMode='mixed', items=[item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]))

    assert answer['success'] is True
    assert head(world.db, answer['quote_id']) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('150.00', '22.50', '172.50')}
    assert [(r['TaxRate'], r['TotalNet']) for r in stored_items(world.db, answer['quote_id'])] == [
        (19.0, to_minor('100.00')), (7.0, to_minor('50.00'))]


def test_a_quote_gets_the_same_rounding_and_the_same_head_as_an_invoice(world):
    answer = save_quote(quote_payload(world, taxMode='mixed', items=[
        item(1, 'Beratung', 100.0, 19), item(2, 'Heft A', 0.10, 7), item(3, 'Heft B', 0.10, 7), item(4, 'Heft C', 0.10, 7)]))
    invoice = save(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Heft A', 0.10, 7), item(3, 'Heft B', 0.10, 7),
                                 item(4, 'Heft C', 0.10, 7)]))

    assert head(world.db, answer['quote_id']) == head(world.db, invoice['invoice_id'])


def test_a_quote_updated_from_one_rate_to_a_mixed_one_and_back_writes_the_flag_each_time(world):
    quote_id = save_quote(quote_payload(world))['quote_id']
    assert head(world.db, quote_id)['MixedTaxRates'] is None

    save_quote(quote_payload(world, quoteId=quote_id, taxMode='mixed',
                             items=[item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]))
    assert head(world.db, quote_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('150.00', '22.50', '172.50')}
    assert [r['TaxRate'] for r in stored_items(world.db, quote_id)] == [19.0, 7.0]

    save_quote(quote_payload(world, quoteId=quote_id))                           # zurück auf einen Satz
    assert head(world.db, quote_id) == {'TaxRate': 0.19, 'MixedTaxRates': None, **minor('250.50', '47.60', '298.10')}
    assert [r['TaxRate'] for r in stored_items(world.db, quote_id)] == [19.0, 19.0]


QUOTE_REFUSALS = {
    'ohne-Steuerausweis': (dict(showTax=False, taxRate=0, taxMode='mixed',
                                items=[item(1, 'A', 1.0, 19), item(2, 'B', 1.0, 7)]), TAX_PER_ITEM_WITHOUT_TAX),
    'Satz-weicht-ab': (dict(items=[item(1, 'A', 1.0, 19), item(2, 'B', 1.0, 7)]), TAX_ITEM_DIFFERS.format(n=2)),
    'Satz-fehlt': (dict(taxMode='mixed', items=[item(1, 'A', 1.0, 19), {'position': 2, 'quantity': 1, 'description': 'B',
                                                                        'unitPrice': 1.0}]), TAX_ITEM_MISSING.format(n=2)),
    'Satz-nicht-erlaubt': (dict(taxMode='mixed', items=[item(1, 'A', 1.0, 19), item(2, 'B', 1.0, 5)]),
                           TAX_ITEM_NOT_ALLOWED.format(n=2, rate='5')),
    'Modus-ungueltig': (dict(taxMode='gemischt'), TAX_MODE_INVALID),
}


@pytest.mark.parametrize('case', QUOTE_REFUSALS)
def test_a_quote_is_refused_with_the_same_sentences_and_nothing_is_saved(world, case):
    over, sentence = QUOTE_REFUSALS[case]
    before = snapshot(world.db)

    answer = save_quote(quote_payload(world, **over))

    assert answer == {'success': False, 'error': sentence}
    assert snapshot(world.db) == before


@pytest.mark.parametrize('case', QUOTE_REFUSALS)
def test_a_refused_update_leaves_the_stored_quote_as_it_was(world, case):
    quote_id = save_quote(quote_payload(world))['quote_id']
    over, sentence = QUOTE_REFUSALS[case]
    before = snapshot(world.db)

    answer = save_quote(quote_payload(world, quoteId=quote_id, **over))

    assert answer == {'success': False, 'error': sentence}
    assert snapshot(world.db) == before


# ── Die Kopierwege ───────────────────────────────────────────────────────────

def test_a_quote_with_a_rate_per_position_becomes_an_invoice_with_the_flag_and_the_rates(world):
    quote_id = save_quote(quote_payload(world, taxMode='mixed', status='accepted',
                                        items=[item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]))['quote_id']

    invoice_id = world.db.convert_quote_to_invoice(quote_id)

    assert head(world.db, invoice_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('150.00', '22.50', '172.50')}
    assert [(r['TaxRate'], r['TotalNet']) for r in stored_items(world.db, invoice_id)] == [
        (19.0, to_minor('100.00')), (7.0, to_minor('50.00'))]
    assert columns(world.db, invoice_id)['SourceQuoteId'] == quote_id


def test_a_quote_with_one_rate_becomes_an_invoice_without_the_flag(world):
    quote_id = save_quote(quote_payload(world, status='accepted'))['quote_id']

    invoice_id = world.db.convert_quote_to_invoice(quote_id)

    assert head(world.db, invoice_id)['MixedTaxRates'] is None


def test_a_value_in_the_flag_column_that_is_not_the_number_1_does_not_stop_the_conversion(world):
    """Die Spalte lässt sich per SQL-Konsole beschreiben: Alles außer 1 heißt „ein Satz“ und wird so kopiert (NULL), ohne Fehler."""
    quote_id = save_quote(quote_payload(world, status='accepted'))['quote_id']
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:
        con.execute("UPDATE Invoices SET MixedTaxRates = 'x' WHERE ID = ?", (quote_id,))
        con.commit()

    invoice_id = world.db.convert_quote_to_invoice(quote_id)

    assert head(world.db, invoice_id)['MixedTaxRates'] is None


def _mixed_invoice(world):
    return save(mixed(world, [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]))['invoice_id']


def test_the_credit_note_of_a_mixed_invoice_is_mixed_with_the_same_rates_and_negated_sums(world):
    """Die App storniert einen Beleg mit Steuersatz je Position in dieser Version noch nicht (``cancel_invoice`` lehnt ab, siehe
    ``tests/test_mixed_tax_locks.py``) – hier geht es allein um die Kopie: Kopf (``_storno_head``) und Positionen (``_copy_items``),
    die der Weg von ``cancel_invoice`` aufruft, werden mit einem Beleg ausprobiert, den die Datenbankschicht in den Status
    „versendet“ bringt."""
    invoice_id = _mixed_invoice(world)
    force_status(world.db, invoice_id, 'sent')
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as conn:
        row = invoice_row(conn, invoice_id)

    head_of_credit = InvoiceStornoMixin._storno_head(row, '26R900', '2026-07-01')
    assert head_of_credit['mixed_tax_rates'] == 1 and head_of_credit['tax_rate'] == 0.19
    with world.db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        credit_id = world.db.insert_invoice(head_of_credit, tx=tx)
        world.db._copy_items(tx.conn, tx, invoice_id, credit_id, negate=True)

    assert head(world.db, credit_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('-150.00', '-22.50', '-172.50')}
    assert [(r['TaxRate'], r['TotalNet']) for r in stored_items(world.db, credit_id)] == [
        (19.0, to_minor('-100.00')), (7.0, to_minor('-50.00'))]


def test_the_replacement_of_a_cancelled_mixed_invoice_is_mixed_too(world):
    """``create_replacement_draft`` bucht nichts und bleibt erlaubt: Der Entwurf trägt das Kennzeichen und die Sätze. Die stornierte
    Rechnung samt Stornorechnung stellt der Test über die Datenbankschicht her (der Weg der App lehnt ab, siehe oben)."""
    invoice_id = _mixed_invoice(world)
    force_status(world.db, invoice_id, 'cancelled')
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as conn:
        row = invoice_row(conn, invoice_id)
    credit_head = InvoiceStornoMixin._storno_head(row, '26R900', '2026-07-01')
    with world.db.transaction() as tx:
        tx.conn.execute('BEGIN IMMEDIATE')
        credit_id = world.db.insert_invoice(credit_head, tx=tx)
        tx.conn.execute("UPDATE Invoices SET InvoiceKind = 'storno', CancelsInvoiceId = ? WHERE ID = ?", (invoice_id, credit_id))

    reissue = world.db.create_replacement_draft(invoice_id, today=datetime.date(2026, 7, 2))

    assert reissue.ok, reissue.message
    assert head(world.db, reissue.invoice_id) == {'TaxRate': 0.19, 'MixedTaxRates': 1, **minor('150.00', '22.50', '172.50')}
    assert [(r['TaxRate'], r['TotalNet']) for r in stored_items(world.db, reissue.invoice_id)] == [
        (19.0, to_minor('100.00')), (7.0, to_minor('50.00'))]
    assert columns(world.db, reissue.invoice_id)['ReplacesInvoiceId'] == invoice_id


def test_the_credit_note_and_the_replacement_of_an_invoice_with_one_rate_carry_no_flag(world):
    invoice_id = make_invoice(world, 'sent')
    storno = world.db.cancel_invoice(invoice_id, '2026-07-01', 'anna', today=datetime.date(2026, 7, 2))
    assert storno.ok, storno.message
    reissue = world.db.create_replacement_draft(invoice_id, today=datetime.date(2026, 7, 2))
    assert reissue.ok, reissue.message

    assert head(world.db, storno.storno_id)['MixedTaxRates'] is None
    assert head(world.db, reissue.invoice_id)['MixedTaxRates'] is None
