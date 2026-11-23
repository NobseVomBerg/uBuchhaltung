# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Masken von Rechnung und Angebot wählen den Steuersatz aus den Stammdaten (2.8.0, Etappe 6: ``server/pages_invoice.py``, ``server/pages_quote.py``).

Die Auswahl im Kopf („Mehrwertsteuer“, ``#tax_choice``) und in der Spalte „USt“ je Position kommt vom Server (``entry_choices``: die wählbaren Einträge der
Stammdaten – aktiv und gültig –, in deren Reihenfolge); im Quelltext des Skripts steht kein Satz, keine ID und keine Bezeichnung. Das versteckte Feld ``#tax_rate`` behält
seine ID und trägt den Satz.

* **Die Vorbelegung (19 und 7 Prozent ohne Erlöskonto) schickt den Payload von früher, Zeichen für Zeichen** – keine ID, auch wenn es weitere Einträge gibt
  (``legacy_expressible``). Jeder andere Eintrag (steuerfrei, mit Erlöskonto, mit Nachkommastellen im Satz) schickt ``taxRateId``: im Kopf als letzter Schlüssel des Kopfs,
  mit dem Häkchen „Steuersatz je Position“ als letzter Schlüssel der Position (``taxMode`` bleibt, einen Eintrag im Kopf gibt es mit dem Häkchen nie).
* **Ein geladener Beleg wählt den Eintrag, den seine Momentaufnahme schriebe** (Satz, Art, Grund, Erlöskonto – ``entry_snapshot``); gibt es ihn nicht mehr (gelöscht, abgeschaltet,
  geändert), steht die Option „gespeichert …“ da und schickt ``taxRateId: 0``: Der Server lehnt ab, bis der Nutzer neu wählt – nichts wird still umgedeutet. Ein Beleg ohne
  Momentaufnahme mit einem Satz, den kein Eintrag allein trägt (16 %), zeigt ihn als eigene Option und schickt keine ID. Das gilt für Bearbeiten, Kopie, Angebot → Rechnung und
  Ersatzrechnung.
* Die Anzeige rechnet eine Gruppe, deren Positionen einen Eintrag tragen, einstufig (0,09 € bei 5,5 % ergeben 0,00 €), jede andere wie immer; eine steuerfreie Gruppe heißt
  „steuerfrei“. Der Grund einer Steuerbefreiung steht als Text unter den Häkchen, jeder Wert aus der Datenbank maskiert.
* Die Skripte laufen in Node auf der DOM-Attrappe von ``tests/test_mixed_tax_mask.py`` (der Test überspringt sich ohne Node); was die Maske schickt, nimmt der Server an
  (``/invoice/save``, ``/quote/save``) und schreibt die erwartete Momentaufnahme.

Alle Namen, Gründe, Konten und Beträge sind erfunden.
"""
import datetime
import inspect
import json
import pathlib
import re
from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

import server.handlers as handlers
from db.invoice_payments import has_mixed_rates
from server import pages_invoice
from server.handlers import TAX_CHOICE_GONE, TAX_CHOICE_HEAD_GONE
from server.pages_invoice import (GONE_VALUE, MIXED_ENTRY_JS, MIXED_TAX_JS, PageInvoice, entry_choices, mixed_tax_script)
from tax_groups import EXEMPT, STANDARD, group_tax
from tests.dom_helpers import Dom, assert_same_structure, data_block
from tests.http_client import get
from tests.invoice_world import build_world, columns, force_status, query_rows, run_sql
from tests.test_mixed_tax_mask import KINDS, NO_NODE, ONE_RATE, SAVE_FUNCTION, expected_payload, item, make_doc, page_of, page_script, run_mask, sent_payload

REASON = 'Steuerfrei nach dem Testgesetz Paragraf 4 (erfundener Grund)'
OTHER_REASON = 'Steuerfrei nach dem zweiten Testgesetz Paragraf 5 (erfundener Grund)'
HOSTILE = '<script>alert(1)</script><img src=x onerror=alert(2)> "doppelt" \'einfach\' &amp; &lt;'
GONE_5_5 = '5,5 % – gespeichert, in den Stammdaten nicht mehr vorhanden'


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return build_world(tmp_db)


def account(db, number):
    """Das Erlöskonto ``number`` (COA-ID); führt der Standard-Kontenrahmen es nicht, entsteht es."""
    if db.get_coa_id_by_account_number(number) is None:
        db.insert_chart_of_accounts(4, number, f'Erlöse (erfundenes Testkonto {number})', 'Betriebliche Erträge')
    return db.get_coa_id_by_account_number(number)


@pytest.fixture
def entries(world):
    """Die Einträge: die Vorbelegung (19 und 7 Prozent ohne Konto), dazu ein steuerfreier mit Grund und Konto, einer mit 5,5 % und Konto und einer mit 19 % und eigenem Konto
    (der Satz der Vorbelegung, aber nicht mehr mit dem Payload von früher auszudrücken). Die Reihenfolge der Auswahl ist die der Stammdaten."""
    db = world.db
    coa = {number: account(db, number) for number in (4120, 4150, 4180)}
    preset = {entry.bp: entry.id for entry in db.fetch_tax_rates()}
    return SimpleNamespace(
        db=db, coa=coa, preset19=preset[1900], preset7=preset[700],
        free=db.insert_tax_rate(label='Steuerfrei (erfunden)', bp=0, kind=EXEMPT, reason=REASON, revenue_coa_id=coa[4120]),
        reduced=db.insert_tax_rate(label='Ermäßigt 5,5 % (erfunden)', bp=550, kind=STANDARD, revenue_coa_id=coa[4150]),
        special=db.insert_tax_rate(label='19 % mit Sonderkonto (erfunden)', bp=1900, kind=STANDARD, revenue_coa_id=coa[4180]))


def entry_item(position, description, price, rate, entry_id, **extra):
    """Eine Position, wie die Maske sie mit einem Eintrag schickt: ``taxRateId`` als letzter Schlüssel."""
    return {**item(position, description, price, rate, **extra), 'taxRateId': entry_id}


# ── Das HTML lesen ───────────────────────────────────────────────────────────

class Selects(HTMLParser):
    """Alle ``<select>`` einer Seite mit ihren Optionen: ``{'attrs': …, 'options': [{'value', 'label', 'selected', 'attrs'}]}`` – so, wie ein Browser das Markup liest."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.selects, self._select, self._option = [], None, None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'select':
            self._select = {'attrs': a, 'options': []}
            self.selects.append(self._select)
        elif tag == 'option' and self._select is not None:
            self._option = {'value': a.get('value'), 'label': '', 'selected': 'selected' in a, 'attrs': a}
            self._select['options'].append(self._option)

    def handle_data(self, data):
        if self._option is not None:
            self._option['label'] += data

    def handle_endtag(self, tag):
        if tag == 'option':
            self._option = None
        elif tag == 'select':
            self._select = None


def select_of(html, **attrs):
    """Das eine ``<select>``, dessen Attribute diese Werte haben (``id=…`` oder ``class=…``)."""
    found = [s for s in Selects(html).selects if all(s['attrs'].get(k) == v for k, v in attrs.items())]
    assert len(found) == 1, (attrs, len(found))
    return found[0]


def head_of(html):
    return select_of(html, id='tax_choice')


def row_selects(html):
    return [s for s in Selects(html).selects if s['attrs'].get('class') == 'item-tax']


def picked(select):
    """Der gewählte Wert eines Auswahlfelds – genau einer, sonst ist es kaputt."""
    chosen = [o['value'] for o in select['options'] if o['selected']]
    assert len(chosen) == 1, select
    return chosen[0]


def option(select, value):
    (found,) = [o for o in select['options'] if o['value'] == value]
    return found


def labels(select):
    return [(o['value'], o['label']) for o in select['options']]


def mask_list(html):
    """Die Liste ``MIX_RATES`` im Skriptblock der Maske als Python-Objekt."""
    mix = next(s for s in Dom(html).scripts if 'function mixOn(' in s)
    return json.loads(re.search(r'var MIX_RATES = (.*?);\n', mix, re.S).group(1))


def hidden_rate(html):
    return Dom(html).one('input', id='tax_rate')


def e(entry_id):
    """Der Wert der Option eines Eintrags, den der Satz allein nicht trägt."""
    return f'e{entry_id}'


# ═════════════════════════════════════════════════════════════════════════════
# 1. Die Auswahl kommt aus den Stammdaten
# ═════════════════════════════════════════════════════════════════════════════

def test_the_choices_are_the_active_valid_entries_in_the_order_of_the_stammdaten(world, entries):
    db = world.db
    inactive = db.insert_tax_rate(label='Abgeschaltet (erfunden)', bp=1000, kind=STANDARD, revenue_coa_id=entries.coa[4150], active=False)
    broken = db.insert_tax_rate(label='Kaputt (erfunden)', bp=1200, kind=STANDARD, revenue_coa_id=entries.coa[4150])
    run_sql(db, "UPDATE TaxRates SET Kind = 'bogus' WHERE ID = ?", broken)                  # an der App vorbei: eine Art, die es nicht gibt
    db.update_tax_rate(entries.reduced, label='Ermäßigt 5,5 % (erfunden)', bp=550, kind=STANDARD, revenue_coa_id=entries.coa[4150], sort_order=15)

    choices = entry_choices(db)

    assert [c.value for c in choices] == ['19', e(entries.reduced), '7', e(entries.free), e(entries.special)]     # nach SortOrder: 10, 15 (geändert), 20, 30, 50 – nicht nach der ID
    assert [c.label for c in choices] == ['19 %', 'Ermäßigt 5,5 % (erfunden)', '7 %', 'Steuerfrei (erfunden)', '19 % mit Sonderkonto (erfunden)']
    assert inactive not in [c.entry_id for c in choices] and broken not in [c.entry_id for c in choices]


def test_only_the_entries_the_old_payload_cannot_express_carry_their_id(world, entries):
    choices = {c.label: c for c in entry_choices(world.db)}

    assert (choices['19 %'].entry_id, choices['7 %'].entry_id) == (None, None)
    assert choices['Steuerfrei (erfunden)'].entry_id == entries.free and choices['Steuerfrei (erfunden)'].exempt is True
    assert choices['Steuerfrei (erfunden)'].reason == REASON and choices['Steuerfrei (erfunden)'].rate == '0'
    assert choices['Ermäßigt 5,5 % (erfunden)'].entry_id == entries.reduced and choices['Ermäßigt 5,5 % (erfunden)'].rate == '5.5'
    assert choices['19 % mit Sonderkonto (erfunden)'].entry_id == entries.special and choices['19 % mit Sonderkonto (erfunden)'].rate == '19'
    assert not any(c.exempt for c in (choices['19 %'], choices['Ermäßigt 5,5 % (erfunden)']))


@pytest.mark.parametrize('kind', KINDS)
def test_the_head_and_every_row_offer_the_same_options_and_the_script_gets_the_same_list(world, entries, kind):
    html = page_of(kind, world)

    expected = [('19', '19 %'), ('7', '7 %'), (e(entries.free), 'Steuerfrei (erfunden)'), (e(entries.reduced), 'Ermäßigt 5,5 % (erfunden)'),
                (e(entries.special), '19 % mit Sonderkonto (erfunden)')]
    (row,) = row_selects(html)
    assert labels(row) == labels(head_of(html)) == expected
    assert picked(head_of(html)) == picked(row) == '19'                                         # eine neue Rechnung beginnt mit der Vorbelegung 19 %
    assert [(c['value'], c['label']) for c in mask_list(html)] == expected


@pytest.mark.parametrize('kind', KINDS)
def test_the_options_of_the_preset_look_exactly_as_before_and_the_others_carry_their_data(world, entries, kind):
    html = page_of(kind, world)
    head = head_of(html)

    assert option(head, '19')['attrs'] == {'value': '19', 'selected': None} and option(head, '7')['attrs'] == {'value': '7'}         # nur der Wert: der Satz allein
    assert option(head, e(entries.free))['attrs'] == {'value': e(entries.free), 'data-rate': '0', 'data-entry': str(entries.free), 'data-exempt': '1',
                                                      'data-reason': REASON}
    assert option(head, e(entries.reduced))['attrs'] == {'value': e(entries.reduced), 'data-rate': '5.5', 'data-entry': str(entries.reduced)}
    listed = {c['value']: c for c in mask_list(html)}
    assert listed['19'] == {'value': '19', 'label': '19 %'}                                      # die Form von früher: Wert und Beschriftung
    assert listed[e(entries.free)] == {'value': e(entries.free), 'label': 'Steuerfrei (erfunden)', 'rate': '0', 'id': entries.free, 'exempt': True, 'reason': REASON}
    assert listed[e(entries.reduced)] == {'value': e(entries.reduced), 'label': 'Ermäßigt 5,5 % (erfunden)', 'rate': '5.5', 'id': entries.reduced}


@pytest.mark.parametrize('kind', KINDS)
def test_the_rate_field_keeps_its_id_as_a_hidden_field_and_the_choice_replaces_the_number_input(world, entries, kind):
    html = page_of(kind, world)

    field = hidden_rate(html)
    assert field['type'] == 'hidden' and field['value'] == '19'
    assert 'id="tax_rate" value="19"' in html                                                  # die Schreibweise, die andere Tests festhalten
    assert not [a for a in Dom(html).all('input') if a.get('type') == 'number' and a.get('id') == 'tax_rate']
    head = Dom(html).one('select', id='tax_choice')
    assert head['class'] == 'taxChoice' and head['onchange'] == 'mixHeadChange()' and 'disabled' not in head
    # in der Steuerzeile, vor „auf … netto“
    assert html.index('id="tax_row"') < html.index('id="tax_choice"') < html.index('id="tax_rate"') < html.index('id="tax_base"')


@pytest.mark.parametrize('kind', KINDS)
def test_the_head_choice_does_not_count_as_a_row_choice(world, entries, kind):
    """``.item-tax`` sind die Auswahlfelder der Zeilen; das Feld im Kopf trägt die Klasse nicht (sonst zählten Summen und Skript es als Zeile)."""
    html = page_of(kind, world)

    assert [s['attrs'].get('class') for s in Selects(html).selects if 'item-tax' in (s['attrs'].get('class') or '')] == ['item-tax']
    assert 'item-tax' not in head_of(html)['attrs']['class']


@pytest.mark.parametrize('kind', KINDS)
def test_the_note_and_the_link_to_the_stammdaten_sit_in_the_cell_of_the_checkboxes(world, entries, kind):
    html = page_of(kind, world)

    cell = html[html.rindex('<tr class="no-pdf">', 0, html.index('id="show_tax"')):html.index('id="tax_row"')]
    assert cell.count('<td') == 1                                                              # dieselbe Zelle, keine eigene Zeile (die Spannen der Tabelle bleiben)
    assert '<p class="hint" id="tax_note" hidden></p>' in cell
    assert '<a href="/masterdata/taxrates" target="_blank" rel="noopener">Steuersätze verwalten</a>' in cell
    assert html.index('id="mixed_tax"') < html.index('Steuersätze verwalten') < html.index('id="tax_note"') < html.index('id="tax_row"')     # der Link hinter dem Häkchen, der Hinweis darunter
    assert '</label> <small class="hint"><a href="/masterdata/taxrates"' in cell                 # in derselben Zeile wie das Häkchen


@pytest.mark.parametrize('kind', KINDS)
def test_without_any_entry_the_form_still_works_with_the_rate_as_its_own_option(world, entries, kind):
    run_sql(world.db, 'DELETE FROM TaxRates')                                                  # nur an der App vorbei möglich: sie lässt den letzten Eintrag stehen

    html = page_of(kind, world)

    assert labels(head_of(html)) == [('19', '19 %')] and picked(head_of(html)) == '19' and mask_list(html) == []
    assert hidden_rate(html)['value'] == '19'


# ── Maskiert: jeder Wert aus der Datenbank ──────────────────────────────────

@pytest.mark.parametrize('kind', KINDS)
def test_a_label_and_a_reason_with_markup_stay_text_in_the_page_and_the_list(world, entries, kind):
    benign = page_of(kind, world)
    world.db.update_tax_rate(entries.free, label=HOSTILE[:60], bp=0, kind=EXEMPT, reason=HOSTILE, revenue_coa_id=entries.coa[4120])
    world.db.update_tax_rate(entries.reduced, label='Ermäßigt "5,5" <b>', bp=550, kind=STANDARD, revenue_coa_id=entries.coa[4150])

    html = page_of(kind, world)

    assert_same_structure(html, benign)                                                         # kein Element, kein Skriptblock mehr, kein Ereignis-Attribut mit Code
    assert '<img src=x' not in html and '<script>alert(1)' not in html                         # nur maskiert: &lt;img …, nie ein Tag
    free, reduced = option(head_of(html), e(entries.free)), option(head_of(html), e(entries.reduced))
    assert free['label'] == HOSTILE[:60] and free['attrs']['data-reason'] == HOSTILE            # so, wie ein Browser es liest: der Text, unverändert
    assert reduced['label'] == 'Ermäßigt "5,5" <b>'
    listed = {c['value']: c for c in mask_list(html)}
    assert listed[e(entries.free)]['reason'] == HOSTILE and listed[e(entries.reduced)]['label'] == 'Ermäßigt "5,5" <b>'
    assert len(Dom(html).scripts) == len(Dom(benign).scripts)                                   # ein </script> im Wert beendet den Block nicht


def test_the_list_reaches_the_script_only_through_script_json_and_the_labels_are_never_in_its_source(world, entries):
    html = page_of('invoice', world)
    _dom, _main, mix = page_script(html, 'invoice')

    code = re.sub(r'//[^\n]*', '', MIXED_ENTRY_JS + MIXED_TAX_JS)
    for value in [e(entries.free), e(entries.reduced), e(entries.special), 'Steuerfrei', 'Sonderkonto', REASON, 'Ermäßigt', str(entries.free)]:
        assert value not in code, value
    assert not re.search(r'(?<![\d.])(19|7|5\.5)(?![\d.])', code), 'ein Satz steht im Quelltext des Skripts'
    assert not re.search(r'MIX_RATES\[[1-9]\]|MIX_RATES\.length\s*(===?|[<>]=?)\s*\d', code)
    assert '"1"' not in code and "'e" not in code                                              # keine ID und kein Optionswert eines Eintrags
    assert 'script_json(' in inspect.getsource(mixed_tax_script)
    assert Dom(html).scripts.count(mix) == 1


# ── Statisch: var und function, kein neues innerHTML, ein Block für beide Masken ──

def test_the_entry_script_uses_only_var_and_function_and_sets_values_only_as_values_or_text():
    code = re.sub(r'//[^\n]*', '', MIXED_ENTRY_JS)
    assert not re.search(r'(?<![A-Za-z0-9_$.])(const|let)\s', code)
    assert '=>' not in code and '`' not in code and '${' not in code
    assert 'textContent' in code and 'createElement' in code
    for name in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'Function(', 'dataset'):
        assert name not in MIXED_ENTRY_JS and name not in MIXED_TAX_JS, name                     # ``dataset`` kennt die Attrappe der Tests nicht – getAttribute genügt
    names = set(re.findall(r'function (\w+)\(', code))
    assert names == {'mixOptionRate', 'mixOptionEntry', 'mixOptionExempt', 'mixChosen', 'mixListed', 'mixHeadChosen', 'mixHeadEntry', 'mixRowEntry', 'mixHeadChange',
                     'mixNote', 'mixHeadTotals'}
    assert all(name.startswith('mix') for name in names)


@pytest.mark.parametrize('kind', KINDS)
def test_both_masks_share_one_script_block_with_the_entry_part_in_front(world, entries, kind):
    html = page_of(kind, world)

    blocks = [s for s in Dom(html).scripts if 'function mixOn(' in s]
    assert len(blocks) == 1
    assert blocks[0].strip() == mixed_tax_script(entry_choices(world.db)).replace('<script>', '').replace('</script>', '').strip()
    assert blocks[0].index('function mixOptionRate(') < blocks[0].index('function mixOn(')
    assert html.index('function mixOn(') < html.index(f'function {SAVE_FUNCTION[kind]}(')


def test_the_stylesheet_has_a_rule_for_the_head_choice_and_the_new_markup_has_no_inline_style(world, entries):
    sheet = (pathlib.Path(pages_invoice.__file__).resolve().parent.parent / 'buch.css').read_text(encoding='utf-8')
    assert re.search(r'\.invoice-items \.taxChoice\s*\{[^}]*max-width', sheet)
    for kind in KINDS:
        html = page_of(kind, world)
        assert 'style=' not in re.search(r'<select id="tax_choice".*?</select>', html, re.S).group(0)
        assert 'style=' not in re.search(r'<p class="hint" id="tax_note"[^>]*>', html).group(0)
        assert 'style=' not in re.search(r'<small class="hint"><a href="/masterdata/taxrates"[^>]*>', html).group(0)
        assert 'style=' not in re.search(r'<input type="hidden" id="tax_rate"[^>]*>', html).group(0)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Ein geladener Beleg wählt den Eintrag, den seine Momentaufnahme schriebe
# ═════════════════════════════════════════════════════════════════════════════

CLOCK = datetime.date(2026, 7, 1)
TWO_REDUCED = [item(1, 'Heft', 10.0, 5.5), item(2, 'Buch', 20.0, 5.5)]
TWO_FREE = [item(1, 'Nachhilfe', 80.0, 0), item(2, 'Material', 20.0, 0)]


def number(kind, count):
    """Die Nummer eines weiteren Belegs derselben Art (Rechnung ``26R…``, Angebot ``26A…``) – eine eigene Nummer je Beleg."""
    return f'26R{count}' if kind == 'invoice' else f'26A{count}'


def head_doc(kind, world, items, entry_id, rate, **over):
    """Ein Beleg mit einem Eintrag im Kopf (ohne Häkchen), so gespeichert, wie die Maske es schickt."""
    return make_doc(kind, world, items, mixed=False, taxRate=rate, taxRateId=entry_id, **over)


def mixed_doc(kind, world, entries, **over):
    """19 % (Vorbelegung), steuerfrei und 5,5 % in einem Beleg: die Positionen 2 und 3 tragen einen Eintrag."""
    items = [item(1, 'Beratung', 100.0, 19), entry_item(2, 'Nachhilfe', 80.0, 0, entries.free), entry_item(3, 'Heft', 10.0, 5.5, entries.reduced)]
    return make_doc(kind, world, items, mixed=True, **over)


def chosen_everywhere(html):
    """Was ein Beleg in allen Auswahlfeldern zeigt: ``(Kopf, [Zeilen])``."""
    return picked(head_of(html)), [picked(s) for s in row_selects(html)]


@pytest.mark.parametrize('kind', KINDS)
def test_a_document_with_an_entry_in_the_head_loads_with_that_entry_chosen_in_the_head_and_the_rows(world, entries, kind):
    doc_id = head_doc(kind, world, TWO_REDUCED, entries.reduced, 0.055)

    html = page_of(kind, world, doc_id)

    assert chosen_everywhere(html) == (e(entries.reduced), [e(entries.reduced)] * 2)
    assert hidden_rate(html)['value'] == '5.5'
    assert 'checked' not in Dom(html).one('input', id='mixed_tax')


@pytest.mark.parametrize('kind', KINDS)
def test_a_fully_exempt_document_loads_with_the_exempt_entry_and_the_rate_0(world, entries, kind):
    doc_id = head_doc(kind, world, TWO_FREE, entries.free, 0)

    html = page_of(kind, world, doc_id)

    assert chosen_everywhere(html) == (e(entries.free), [e(entries.free)] * 2)
    assert hidden_rate(html)['value'] == '0'


@pytest.mark.parametrize('kind', KINDS)
def test_the_snapshot_decides_among_entries_with_the_same_rate(world, entries, kind):
    """19 % ist ein Satz der Vorbelegung UND ein Eintrag mit eigenem Konto: Die Momentaufnahme (Satz, Art, Grund, Erlöskonto) sagt, welcher."""
    special = head_doc(kind, world, ONE_RATE, entries.special, 0.19)
    preset = head_doc(kind, world, ONE_RATE, entries.preset19, 0.19, number=number(kind, 101))
    plain = make_doc(kind, world, ONE_RATE, mixed=False, number=number(kind, 102))
    # Seit 2.8.2 schreibt der Server für die Vorbelegung keine Momentaufnahme mehr; ein Entwurf aus 2.8.0 oder 2.8.1 (über die Schnittstelle mit ihrer ID gespeichert) trägt noch eine
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRateBp = 1900 WHERE InvoiceId = ?', preset)

    assert picked(head_of(page_of(kind, world, special))) == e(entries.special)
    assert picked(head_of(page_of(kind, world, preset))) == '19'              # die Vorbelegung mit Momentaufnahme: die Option, die nur den Satz trägt
    assert picked(head_of(page_of(kind, world, plain))) == '19'               # ohne Momentaufnahme gilt nie ein Eintrag mit eigenem Konto


@pytest.mark.parametrize('kind', KINDS)
def test_a_mixed_document_loads_each_row_with_its_entry_and_the_head_with_the_highest_rate(world, entries, kind):
    doc_id = mixed_doc(kind, world, entries)

    html = page_of(kind, world, doc_id)

    assert chosen_everywhere(html) == ('19', ['19', e(entries.free), e(entries.reduced)])
    assert 'checked' in Dom(html).one('input', id='mixed_tax') and hidden_rate(html)['value'] == '19'


def test_a_mixed_document_whose_highest_rate_is_an_entry_shows_that_entry_in_the_head(world, entries):
    doc_id = make_doc('invoice', world, [entry_item(1, 'Heft', 10.0, 5.5, entries.reduced), entry_item(2, 'Nachhilfe', 80.0, 0, entries.free)], mixed=True)

    html = page_of('invoice', world, doc_id)

    assert chosen_everywhere(html) == (e(entries.reduced), [e(entries.reduced), e(entries.free)]) and hidden_rate(html)['value'] == '5.5'


def test_the_copy_of_a_document_with_entries_chooses_the_same_entries(world, entries):
    head = head_doc('invoice', world, TWO_FREE, entries.free, 0)
    mixed = mixed_doc('invoice', world, entries, number='26R101')

    assert chosen_everywhere(PageInvoice(world.db, {}, None, copy_from=head)) == (e(entries.free), [e(entries.free)] * 2)
    assert chosen_everywhere(PageInvoice(world.db, {}, None, copy_from=mixed)) == ('19', ['19', e(entries.free), e(entries.reduced)])
    assert Dom(PageInvoice(world.db, {}, None, copy_from=head)).one('input', id='invoice_id')['value'] == ''


def test_a_quote_converted_to_an_invoice_chooses_the_entries_of_the_quote(world, entries):
    quote = make_doc('quote', world, [entry_item(1, 'Nachhilfe', 80.0, 0, entries.free), item(2, 'Beratung', 100.0, 19)], mixed=True, status='accepted')

    invoice_id = world.db.convert_quote_to_invoice(quote, today=CLOCK)

    assert chosen_everywhere(PageInvoice(world.db, {}, invoice_id)) == ('19', [e(entries.free), '19'])


def test_the_draft_of_a_replacement_invoice_chooses_the_entries_of_the_cancelled_one(world, entries):
    original = head_doc('invoice', world, TWO_REDUCED, entries.reduced, 0.055)
    force_status(world.db, original, 'sent')
    assert world.db.cancel_invoice(original, '2026-07-01', 'anna', today=CLOCK).ok is True

    reissue = world.db.create_replacement_draft(original, 'anna', today=CLOCK)

    assert reissue.ok is True, reissue.message
    assert chosen_everywhere(PageInvoice(world.db, {}, reissue.invoice_id)) == (e(entries.reduced), [e(entries.reduced)] * 2)


# ── Der Eintrag gilt nicht mehr: „gespeichert …“, nichts wird still umgedeutet ─

def gone_option(html):
    """Die Option „gespeichert …“ im Kopf."""
    return option(head_of(html), GONE_VALUE)


def vanish(world, entries, how):
    """Der Eintrag ``entries.reduced`` ändert sich in den Stammdaten – so, dass seine Momentaufnahme nicht mehr passt."""
    db = world.db
    if how == 'deleted':
        db.delete_tax_rate(entries.reduced)
    elif how == 'inactive':
        db.set_tax_rate_active(entries.reduced, False)
    elif how == 'other account':
        db.update_tax_rate(entries.reduced, label='Ermäßigt 5,5 % (erfunden)', bp=550, kind=STANDARD, revenue_coa_id=entries.coa[4120])
    elif how == 'other rate':
        db.update_tax_rate(entries.reduced, label='Ermäßigt 6 % (erfunden)', bp=600, kind=STANDARD, revenue_coa_id=entries.coa[4150])
    else:
        raise AssertionError(how)


@pytest.mark.parametrize('how', ['deleted', 'inactive', 'other account', 'other rate'])
@pytest.mark.parametrize('kind', KINDS)
def test_an_entry_that_is_gone_or_changed_leaves_the_saved_state_chosen_as_its_own_option(world, entries, kind, how):
    doc_id = head_doc(kind, world, TWO_REDUCED, entries.reduced, 0.055)
    vanish(world, entries, how)

    html = page_of(kind, world, doc_id)

    assert chosen_everywhere(html) == (GONE_VALUE, [GONE_VALUE] * 2)
    for select in (head_of(html), *row_selects(html)):
        assert option(select, GONE_VALUE) == {'value': GONE_VALUE, 'label': GONE_5_5, 'selected': True,
                                              'attrs': {'value': GONE_VALUE, 'data-rate': '5.5', 'data-entry': '0', 'selected': None}}
    assert hidden_rate(html)['value'] == '5.5'                                               # der gespeicherte Satz bleibt im Feld


def test_the_label_of_an_entry_may_change_without_losing_the_document(world, entries):
    doc_id = head_doc('invoice', world, TWO_REDUCED, entries.reduced, 0.055)
    world.db.update_tax_rate(entries.reduced, label='Neu benannt (erfunden)', bp=550, kind=STANDARD, revenue_coa_id=entries.coa[4150], sort_order=5)

    html = page_of('invoice', world, doc_id)

    assert picked(head_of(html)) == e(entries.reduced) and option(head_of(html), e(entries.reduced))['label'] == 'Neu benannt (erfunden)'


@pytest.mark.parametrize('how', ['deleted', 'other reason'])
def test_an_exempt_entry_that_is_gone_keeps_its_stored_reason_on_the_gone_option(world, entries, how):
    doc_id = head_doc('invoice', world, TWO_FREE, entries.free, 0)
    if how == 'deleted':
        world.db.delete_tax_rate(entries.free)
    else:
        world.db.update_tax_rate(entries.free, label='Steuerfrei (erfunden)', bp=0, kind=EXEMPT, reason=OTHER_REASON, revenue_coa_id=entries.coa[4120])

    html = page_of('invoice', world, doc_id)

    gone = gone_option(html)
    assert picked(head_of(html)) == GONE_VALUE and gone['label'] == 'steuerfrei – gespeichert, in den Stammdaten nicht mehr vorhanden'
    assert gone['attrs'] == {'value': GONE_VALUE, 'data-rate': '0', 'data-entry': '0', 'data-exempt': '1', 'data-reason': REASON, 'selected': None}


def test_in_a_mixed_document_only_the_row_of_the_vanished_entry_shows_the_gone_option(world, entries):
    doc_id = mixed_doc('invoice', world, entries)
    world.db.delete_tax_rate(entries.free)

    html = page_of('invoice', world, doc_id)

    assert chosen_everywhere(html) == ('19', ['19', GONE_VALUE, e(entries.reduced)])
    first, second, third = row_selects(html)
    assert GONE_VALUE not in [v for v, _label in labels(first)] + [v for v, _label in labels(third)]
    assert option(second, GONE_VALUE)['label'] == 'steuerfrei – gespeichert, in den Stammdaten nicht mehr vorhanden'


@pytest.mark.parametrize('kind', KINDS)
def test_a_rate_no_entry_carries_alone_shows_as_its_own_option_without_an_id(world, entries, kind):
    """Ein Bestandsbeleg mit 16 %: Die Option zeigt den Satz, ohne Attribute – der Payload von früher, den der Server annimmt."""
    doc_id = make_doc(kind, world, [item(1, 'Beratung', 100.0, 16), item(2, 'Material', 50.0, 16)], mixed=False, taxRate=0.16)

    html = page_of(kind, world, doc_id)

    assert chosen_everywhere(html) == ('16', ['16', '16'])
    for select in (head_of(html), *row_selects(html)):
        assert option(select, '16') == {'value': '16', 'label': '16 %', 'selected': True, 'attrs': {'value': '16', 'selected': None}}
    assert hidden_rate(html)['value'] == '16'


@pytest.mark.parametrize('kind', KINDS)
def test_two_entries_the_preset_payload_carries_alike_are_both_offered_and_only_the_first_is_chosen(world, entries, kind):
    """Ein zweiter Eintrag mit 7 % ohne Konto: Der Payload von früher trägt beide gleich (kein Eintrag, nur der Satz) – beide stehen zur Wahl, gewählt ist genau eine Option."""
    world.db.insert_tax_rate(label='7 % (zweiter Eintrag, erfunden)', bp=700, kind=STANDARD)
    doc_id = make_doc(kind, world, [item(1, 'Heft', 10.0, 7), item(2, 'Buch', 20.0, 7)], mixed=False, taxRate=0.07)

    html = page_of(kind, world, doc_id)

    for select in (head_of(html), *row_selects(html)):
        sevens = [o for o in select['options'] if o['value'] == '7']
        assert [o['label'] for o in sevens] == ['7 %', '7 % (zweiter Eintrag, erfunden)']
        assert [o['selected'] for o in sevens] == [True, False] and sum(o['selected'] for o in select['options']) == 1
        assert all(o['attrs'].keys() <= {'value', 'selected'} for o in sevens)                # beide tragen nur den Satz


def test_an_item_whose_stored_snapshot_cannot_be_read_counts_as_one_without(world, entries):
    doc_id = head_doc('invoice', world, TWO_REDUCED, entries.reduced, 0.055)
    run_sql(world.db, "UPDATE InvoiceItems SET TaxRateBp = 'abc' WHERE InvoiceId = ?", doc_id)

    html = page_of('invoice', world, doc_id)

    assert chosen_everywhere(html) == ('5.5', ['5.5', '5.5'])                               # der Satz allein, als eigene Option: nichts wird geraten


def test_a_document_without_items_shows_its_rate_as_the_preset_or_its_own_option(world, entries):
    def bare(number, rate):
        return world.db.insert_invoice({'invoice_number': number, 'invoice_date': '2026-06-01', 'seller_name': 'S', 'seller_company': 'S GmbH',
                                        'buyer_name': 'B', 'buyer_company': 'B GmbH', 'tax_rate': rate, 'sum_net': 0, 'tax_amount': 0, 'sum_gross': 0,
                                        'amount_due': 0, 'status': 'draft', 'document_type': 'invoice'})

    seven, odd = bare('R-7', 0.07), bare('R-55', 0.055)

    assert chosen_everywhere(page_of('invoice', world, seven)) == ('7', ['7']) and hidden_rate(page_of('invoice', world, seven))['value'] == '7'
    assert chosen_everywhere(page_of('invoice', world, odd)) == ('5.5', ['19'])                    # die leere Zeile beginnt wie jede neue: 5,5 % kennt die Liste nicht, der erste Satz gilt


@pytest.mark.parametrize('kind', KINDS)
def test_a_document_without_tax_starts_the_head_with_the_default_and_keeps_the_rate_in_the_field(world, entries, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False, showTax=False)

    html = page_of(kind, world, doc_id)

    assert picked(head_of(html)) == '19' and hidden_rate(html)['value'] == '19'             # wie das Zahlenfeld vorher: Beim Einschalten gilt 19 %
    assert 'disabled' in Dom(html).one('input', id='mixed_tax')


def test_a_sent_invoice_shows_its_entries_everywhere_and_everything_is_locked(world, entries):
    doc_id = mixed_doc('invoice', world, entries)
    force_status(world.db, doc_id, 'sent')

    html = page_of('invoice', world, doc_id)

    assert chosen_everywhere(html) == ('19', ['19', e(entries.free), e(entries.reduced)])
    assert 'disabled' in head_of(html)['attrs'] and all('disabled' in s['attrs'] for s in row_selects(html))
    assert 'disabled' in hidden_rate(html)                                                   # das Feld der Sperre gehört dazu (wie das Zahlenfeld vorher)
    assert 'disabled' not in hidden_rate(page_of('invoice', world, mixed_doc('invoice', world, entries, number='26R101')))


# ── Ein neuer Beleg beginnt mit der Vorgabe, nicht mit einem Satz, den es nicht mehr zur Wahl gibt ──

@pytest.mark.parametrize('kind', KINDS)
def test_a_new_document_starts_with_the_first_choice_when_the_preset_19_is_off(world, entries, kind):
    world.db.set_tax_rate_active(entries.preset19, False)

    html = page_of(kind, world)

    assert [v for v, _label in labels(head_of(html))][:2] == ['7', e(entries.free)]
    assert picked(head_of(html)) == '7' and hidden_rate(html)['value'] == '7'
    (row,) = row_selects(html)
    assert picked(row) == '7'


@pytest.mark.parametrize('kind', KINDS)
def test_a_new_document_may_start_with_an_entry_that_carries_an_id(world, entries, kind):
    world.db.set_tax_rate_active(entries.preset19, False)
    world.db.set_tax_rate_active(entries.preset7, False)

    html = page_of(kind, world)

    assert picked(head_of(html)) == e(entries.free) and hidden_rate(html)['value'] == '0'
    (row,) = row_selects(html)
    assert picked(row) == e(entries.free)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Das Skript (Node): der Payload
# ═════════════════════════════════════════════════════════════════════════════

# Hilfen für die Programme, die in der Attrappe laufen: den Kopf wählen, den Stand der Maske auslesen
JS = r"""
function chooseHead(value) { var s = $('tax_choice'); s.value = value; fire(s, 'change'); }
function state() {
  var note = $('tax_note');
  return {
    sumNet: $('sum_net').textContent, taxAmount: $('tax_amount').textContent, sumGross: $('sum_gross').textContent,
    rateRows: document.querySelectorAll('.mixRateRow').map(function (r) { return [r.children[0].textContent, r.children[1].textContent]; }),
    head: $('tax_choice').value, rate: $('tax_rate').value, selects: document.querySelectorAll('.item-tax').map(function (s) { return s.value; }),
    note: {hidden: note.hidden, text: note.textContent, nodes: note.children.length},
  };
}
function optionsOf(select) {
  return select.options.map(function (o) {
    return [o.value, o.textContent, o.getAttribute('data-rate'), o.getAttribute('data-entry'), o.hasAttribute('data-exempt'), o.getAttribute('data-reason')];
  });
}
"""


def payload_of(tmp_path, kind, html, program):
    """Was das Speichern der Maske abschickt, nachdem ``program`` gelaufen ist (JavaScript mit den Hilfen von ``JS`` und der Attrappe)."""
    return sent_payload(run_mask(tmp_path, html, kind, JS + program), kind)


def state_of(tmp_path, kind, html, program=''):
    return run_mask(tmp_path, html, kind, JS + program + "emit('state', state());")['state']


def post_save(kind, payload):
    """Das Speichern, wie der Server es annimmt: die Antwort als Dict."""
    handler = handlers.handle_invoice_save if kind == 'invoice' else handlers.handle_quote_save
    return json.loads(handler(json.dumps(payload).encode()))


def stored_items(db, doc_id):
    return query_rows(db, 'SELECT Position, TotalNet, TaxCategory, TaxRate, TaxRateBp, TaxExemptReason, RevenueCoaId FROM InvoiceItems WHERE InvoiceId = ? '
                          'ORDER BY Position', doc_id)


def snapshot_columns(db, doc_id):
    """Nur die vier Spalten der Momentaufnahme je Position: ``(TaxCategory, TaxRateBp, TaxExemptReason, RevenueCoaId)``."""
    return [(r['TaxCategory'], r['TaxRateBp'], r['TaxExemptReason'], r['RevenueCoaId']) for r in stored_items(db, doc_id)]


NO_SNAPSHOT = ('S', None, None, None)


def doc_state(db, doc_id):
    """Alles, was ein Speichern der Maske am Beleg schreiben kann: jede Spalte der Zeile von ``Invoices`` (ohne ihren Zeitstempel ``UpdatedAt``) und jede Spalte der Positionen von
    ``InvoiceItems`` (ohne ihre ID, die jedes Speichern neu vergibt)."""
    positions = query_rows(db, 'SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', doc_id)
    return {k: v for k, v in columns(db, doc_id).items() if k != 'UpdatedAt'}, [{k: v for k, v in row.items() if k != 'ID'} for row in positions]


@NO_NODE
def test_with_the_preset_the_invoice_sends_exactly_the_payload_of_before_even_with_more_entries_around(world, entries, tmp_path):
    invoice_id = make_doc('invoice', world, ONE_RATE, mixed=False)

    payload = payload_of(tmp_path, 'invoice', page_of('invoice', world, invoice_id), "$('customer_number').value = 'K-100'; saveInvoice();")

    assert payload == expected_payload(world, invoice_id)
    assert 'taxRateId' not in payload and list(payload) == list(expected_payload(world, invoice_id))     # auch die Reihenfolge der Schlüssel


@NO_NODE
def test_with_the_preset_the_quote_sends_exactly_the_payload_of_before_even_with_more_entries_around(world, entries, tmp_path):
    quote_id = make_doc('quote', world, ONE_RATE, mixed=False)

    payload = payload_of(tmp_path, 'quote', page_of('quote', world, quote_id), 'saveQuote();')

    assert payload == {
        'quoteNumber': '26A100', 'quoteDate': '2026-06-07', 'customerId': world.cust, 'ownCompanyId': world.own, 'validUntil': '2026-07-07',
        'introText': 'Guten Tag', 'closingText': 'Mit freundlichen Grüßen', 'taxRate': 0.19, 'showTax': True, 'status': 'draft',
        'items': [{'position': 1, 'quantity': 1, 'unit': 'Stk.', 'description': 'Beratung', 'unitPrice': 100, 'taxRate': 19},
                  {'position': 2, 'quantity': 1, 'unit': 'Stk.', 'description': 'Material', 'unitPrice': 50, 'taxRate': 19}],
        'quoteId': quote_id}
    assert list(payload) == ['quoteNumber', 'quoteDate', 'customerId', 'ownCompanyId', 'validUntil', 'introText', 'closingText', 'taxRate', 'showTax', 'status', 'items',
                             'quoteId']


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_choosing_the_other_preset_rate_sends_no_id_either(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f"chooseHead('7'); {SAVE_FUNCTION[kind]}();")

    assert payload['taxRate'] == 0.07 and [i['taxRate'] for i in payload['items']] == [7, 7]
    assert 'taxRateId' not in payload and all('taxRateId' not in i for i in payload['items']) and 'taxMode' not in payload


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_an_entry_in_the_head_sends_its_id_as_the_last_key_of_the_head_and_the_rate_as_before(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f"chooseHead('{e(entries.reduced)}'); {SAVE_FUNCTION[kind]}();")

    assert payload['taxRateId'] == entries.reduced and list(payload)[-1] == 'taxRateId'
    assert payload['taxRate'] == 0.055 and [i['taxRate'] for i in payload['items']] == [5.5, 5.5]         # der Kopf als Bruch, die Positionen als Prozentzahl: wie heute
    assert 'taxMode' not in payload and all('taxRateId' not in i for i in payload['items'])                # die Positionen tragen ohne das Häkchen nie eine ID


@NO_NODE
def test_the_invoice_head_entry_comes_after_every_key_of_before_in_their_old_order(world, entries, tmp_path):
    invoice_id = make_doc('invoice', world, ONE_RATE, mixed=False)
    before = list(expected_payload(world, invoice_id))

    payload = payload_of(tmp_path, 'invoice', page_of('invoice', world, invoice_id),
                         f"$('customer_number').value = 'K-100'; chooseHead('{e(entries.reduced)}'); saveInvoice();")

    assert list(payload) == before + ['taxRateId']


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_fully_exempt_head_sends_the_rate_0_and_its_id(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, TWO_FREE, mixed=False, taxRate=0)

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f"chooseHead('{e(entries.free)}'); {SAVE_FUNCTION[kind]}();")

    assert payload['taxRate'] == 0 and [i['taxRate'] for i in payload['items']] == [0, 0] and payload['taxRateId'] == entries.free
    assert list(payload)[-1] == 'taxRateId'


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_19_percent_entry_with_its_own_account_sends_its_id_though_the_rate_is_the_preset(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f"chooseHead('{e(entries.special)}'); {SAVE_FUNCTION[kind]}();")

    assert payload['taxRate'] == 0.19 and payload['taxRateId'] == entries.special


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_document_saved_with_the_preset_and_its_id_sends_no_id_again_because_the_preset_needs_none(world, entries, tmp_path, kind):
    doc_id = head_doc(kind, world, ONE_RATE, entries.preset19, 0.19)

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f'{SAVE_FUNCTION[kind]}();')

    assert 'taxRateId' not in payload and payload['taxRate'] == 0.19


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_with_the_checkbox_an_entry_sends_its_id_as_the_last_key_of_its_position_and_the_head_none(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f"tick('mixed_tax', true); choose(rows()[1], '{e(entries.free)}'); {SAVE_FUNCTION[kind]}();")

    assert payload['taxMode'] == 'mixed' and 'taxRateId' not in payload
    first, second = payload['items']
    assert 'taxRateId' not in first and (second['taxRateId'], list(second)[-1]) == (entries.free, 'taxRateId')
    assert [first['taxRate'], second['taxRate']] == [19, 0]


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_loaded_mixed_document_sends_the_ids_of_its_entries_back_unchanged(world, entries, tmp_path, kind):
    doc_id = mixed_doc(kind, world, entries)

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f'{SAVE_FUNCTION[kind]}();')

    assert payload['taxMode'] == 'mixed' and 'taxRateId' not in payload
    assert [i.get('taxRateId') for i in payload['items']] == [None, entries.free, entries.reduced]
    assert [i['taxRate'] for i in payload['items']] == [19, 0, 5.5]


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_without_tax_no_id_goes_with_the_save_even_if_the_head_names_an_entry(world, entries, tmp_path, kind):
    doc_id = head_doc(kind, world, TWO_REDUCED, entries.reduced, 0.055)

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f"tick('show_tax', false); {SAVE_FUNCTION[kind]}();")

    assert payload['showTax'] is False and payload['taxRate'] == 0 and [i['taxRate'] for i in payload['items']] == [0, 0]
    assert 'taxRateId' not in payload and 'taxMode' not in payload and all('taxRateId' not in i for i in payload['items'])


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_without_tax_no_id_goes_with_a_mixed_save_either(world, entries, tmp_path, kind):
    doc_id = mixed_doc(kind, world, entries)

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f"tick('show_tax', false); {SAVE_FUNCTION[kind]}();")

    assert 'taxMode' not in payload and all('taxRateId' not in i for i in payload['items'])


@NO_NODE
def test_a_locked_invoice_sends_only_the_open_fields_never_an_entry(world, entries, tmp_path):
    invoice_id = mixed_doc('invoice', world, entries)
    force_status(world.db, invoice_id, 'sent')

    payload = payload_of(tmp_path, 'invoice', page_of('invoice', world, invoice_id), "$('order_number').value = 'B-99'; saveInvoice();")

    assert payload == {'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'}


# ── Der Eintrag gilt nicht mehr: ID 0, der Server lehnt ab – die Maske zeigt seinen Satz ──

@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_vanished_head_entry_sends_the_id_0_and_the_server_refuses_until_another_is_chosen(world, entries, tmp_path, kind):
    doc_id = head_doc(kind, world, TWO_REDUCED, entries.reduced, 0.055)
    world.db.delete_tax_rate(entries.reduced)
    html = page_of(kind, world, doc_id)
    sent = payload_of(tmp_path, kind, html, f'{SAVE_FUNCTION[kind]}();')
    before = doc_state(world.db, doc_id)

    assert sent['taxRateId'] == 0 and sent['taxRate'] == 0.055
    answer = post_save(kind, sent)
    assert answer == {'success': False, 'error': TAX_CHOICE_HEAD_GONE} and doc_state(world.db, doc_id) == before
    shown = run_mask(tmp_path, html, kind, JS + f'{SAVE_FUNCTION[kind]}();', answer=answer, real_send=True)
    assert shown['messages'] == [['Fehler beim Speichern: ' + TAX_CHOICE_HEAD_GONE, 'error']]

    fixed = payload_of(tmp_path, kind, html, f"chooseHead('{e(entries.free)}'); {SAVE_FUNCTION[kind]}();")
    assert fixed['taxRateId'] == entries.free


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_vanished_row_entry_sends_the_id_0_for_its_position_and_the_server_names_the_position(world, entries, tmp_path, kind):
    doc_id = mixed_doc(kind, world, entries)
    world.db.delete_tax_rate(entries.free)
    html = page_of(kind, world, doc_id)

    sent = payload_of(tmp_path, kind, html, f'{SAVE_FUNCTION[kind]}();')

    assert [i.get('taxRateId') for i in sent['items']] == [None, 0, entries.reduced]
    answer = post_save(kind, sent)
    assert answer == {'success': False, 'error': TAX_CHOICE_GONE.format(n=2)}
    shown = run_mask(tmp_path, html, kind, JS + f'{SAVE_FUNCTION[kind]}();', answer=answer, real_send=True)
    assert shown['messages'] == [['Fehler beim Speichern: ' + TAX_CHOICE_GONE.format(n=2), 'error']]


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_rate_no_entry_carries_alone_is_sent_without_an_id_and_the_server_takes_it(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, [item(1, 'Beratung', 100.0, 16), item(2, 'Material', 50.0, 16)], mixed=False, taxRate=0.16)

    sent = payload_of(tmp_path, kind, page_of(kind, world, doc_id), f'{SAVE_FUNCTION[kind]}();')

    assert sent['taxRate'] == 0.16 and 'taxRateId' not in sent and [i['taxRate'] for i in sent['items']] == [16, 16]
    assert post_save(kind, sent)['success'] is True


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_two_entries_of_one_rate_in_one_document_are_refused_by_the_server_and_the_mask_shows_it(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)
    html = page_of(kind, world, doc_id)
    program = JS + f"tick('mixed_tax', true); choose(rows()[1], '{e(entries.special)}'); {SAVE_FUNCTION[kind]}();"
    sent = sent_payload(run_mask(tmp_path, html, kind, program), kind)

    answer = post_save(kind, sent)

    assert [i.get('taxRateId') for i in sent['items']] == [None, entries.special]
    assert answer['success'] is False and answer['error'] == handlers.TAX_SAME_RATE_TWICE.format(n=2, rate='19 %')
    shown = run_mask(tmp_path, html, kind, program, answer=answer, real_send=True)
    assert shown['messages'] == [['Fehler beim Speichern: ' + answer['error'], 'error']]


# ── Neue Zeilen, Artikel und das Häkchen ─────────────────────────────────────

@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_new_row_gets_the_options_of_the_rows_of_the_server_with_all_their_attributes(world, entries, tmp_path, kind):
    out = run_mask(tmp_path, page_of(kind, world), kind, JS + "addFreeRow(); emit('options', document.querySelectorAll('.item-tax').map(optionsOf));")

    served, added = out['options']
    assert added == served
    assert [o[:2] for o in served] == [['19', '19 %'], ['7', '7 %'], [e(entries.free), 'Steuerfrei (erfunden)'], [e(entries.reduced), 'Ermäßigt 5,5 % (erfunden)'],
                                        [e(entries.special), '19 % mit Sonderkonto (erfunden)']]
    assert served[2] == [e(entries.free), 'Steuerfrei (erfunden)', '0', str(entries.free), True, REASON]
    assert served[0] == ['19', '19 %', None, None, False, None]                                # die Vorbelegung trägt keine Attribute


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_new_row_starts_like_the_head_also_with_an_entry_that_carries_an_id(world, entries, tmp_path, kind):
    program = ("addFreeRow(); chooseHead('" + e(entries.reduced) + "'); addFreeRow(); type($('tax_rate'), '7'); addFreeRow(); "
               "chooseHead('" + e(entries.free) + "'); addFreeRow(); emit('state', state());")

    state = run_mask(tmp_path, page_of(kind, world), kind, JS + program)['state']

    assert state['selects'] == ['19', '19', e(entries.reduced), '7', e(entries.free)]          # die Auswahl im Kopf gilt, solange das versteckte Feld ihren Satz trägt


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_new_row_takes_the_rate_of_the_hidden_field_if_a_preset_option_carries_it_else_the_first_of_the_list(world, entries, tmp_path, kind):
    """Das Feld, in das früher getippt wurde, bleibt die Quelle der Vorgabe: Eine Option, die nur den Satz trägt, gilt – 5,5 % (ein Eintrag mit eigenem Konto), 16 % und Text nicht."""
    program = ("var got = []; ['7', '5.5', '16', '0.07', 'abc', '19', '0'].forEach(function (text) {"
               "  type($('tax_rate'), text); addFreeRow(); got.push(rows()[rows().length - 1].querySelector('.item-tax').value); });"
               "emit('got', got);")

    out = run_mask(tmp_path, page_of(kind, world), kind, JS + program)

    assert out['got'] == ['7', '19', '19', '7', '19', '19', '19']                               # was die Liste nicht als Satz allein kennt, nimmt die erste Option


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_new_row_with_an_entry_chosen_sends_its_id(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)
    program = f"tick('mixed_tax', true); addFreeRow(); var row = rows()[2]; row.querySelector('.item-description').value = 'Neu'; choose(row, '{e(entries.reduced)}'); {SAVE_FUNCTION[kind]}();"

    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), program)

    assert [i.get('taxRateId') for i in payload['items']] == [None, None, entries.reduced] and payload['items'][2]['taxRate'] == 5.5


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_an_article_row_takes_its_rate_only_from_a_preset_option_else_it_acts_like_a_new_row(world, entries, tmp_path, kind):
    for name, rate in (('Fachbuch', 7), ('Kurios', 5.5), ('Frei', 0), ('Neunzehn', 19), ('Text', 'abc')):
        world.db.insert_article(name, unit_price=10, tax_rate=rate)
    html = page_of(kind, world)
    by_name = {a['name']: key for key, a in data_block(Dom(html), 'articlesData').items()}
    program = (JS + "var got = []; chooseHead('" + e(entries.reduced) + "');" + ''.join(
        f"addArticleRow('{by_name[name]}'); got.push(rows()[rows().length - 1].querySelector('.item-tax').value);"
        for name in ('Fachbuch', 'Kurios', 'Frei', 'Neunzehn', 'Text')) + "emit('got', got);")

    out = run_mask(tmp_path, html, kind, program)

    # 7 und 19 trägt die Vorbelegung allein; 5,5 und 0 hat nur ein Eintrag mit eigenem Konto (den wählt der Nutzer selbst), „abc“ ist kein Satz:
    # sie alle starten wie eine neue Zeile – mit dem Eintrag, den der Kopf gewählt hat
    assert out['got'] == ['7', e(entries.reduced), e(entries.reduced), '19', e(entries.reduced)]


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_switching_the_checkbox_on_gives_the_rows_the_head_entry_and_keeps_a_row_the_user_chose(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)
    program = (f"chooseHead('{e(entries.reduced)}'); choose(rows()[1], '7'); tick('mixed_tax', true); emit('on', state()); "
               f"tick('mixed_tax', false); chooseHead('{e(entries.free)}'); tick('mixed_tax', true); emit('again', state());")

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, JS + program)

    assert out['on']['selects'] == [e(entries.reduced), '7'] and out['again']['selects'] == [e(entries.free), '7']


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_head_choice_writes_its_rate_into_the_hidden_field_and_the_totals_follow(world, entries, tmp_path, kind):
    """150,00 € netto: 19 % = 28,50 €, 7 % = 10,50 €, 5,5 % = 8,25 €, steuerfrei = 0,00 €."""
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)
    program = ("emit('s19', state()); chooseHead('7'); emit('s7', state()); chooseHead('" + e(entries.reduced) + "'); emit('s55', state()); "
               "chooseHead('" + e(entries.free) + "'); emit('s0', state());")

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, JS + program)

    assert [(out[k]['rate'], out[k]['taxAmount'], out[k]['sumGross']) for k in ('s19', 's7', 's55', 's0')] == [
        ('19', '28,50 €', '178,50 €'), ('7', '10,50 €', '160,50 €'), ('5.5', '8,25 €', '158,25 €'), ('0', '0,00 €', '150,00 €')]
    assert out['s55']['head'] == e(entries.reduced)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Das Skript: Summenzeilen, Rundung, Hinweis
# ═════════════════════════════════════════════════════════════════════════════

@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_summary_names_a_tax_free_group_steuerfrei_instead_of_0_percent_and_shows_the_other_rates_as_before(world, entries, tmp_path, kind):
    doc_id = mixed_doc(kind, world, entries)

    state = state_of(tmp_path, kind, page_of(kind, world, doc_id))

    assert state['rateRows'] == [['Mehrwertsteuer 19 % auf 100,00 € netto:', '19,00 €'], ['Mehrwertsteuer 5,5 % auf 10,00 € netto:', '0,55 €'],
                                 ['Steuerfrei auf 80,00 € netto:', '0,00 €']]
    assert (state['sumNet'], state['taxAmount'], state['sumGross']) == ('190,00 €', '19,55 €', '209,55 €')
    stored = columns(world.db, doc_id)
    assert (stored['SumNet'], stored['TaxAmount'], stored['SumGross']) == (1900000, 195500, 2095500)        # der Server sagt dasselbe


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_display_rounds_a_group_with_entries_once_so_9_cent_at_5_5_percent_are_no_tax_like_the_server(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, [item(1, 'Beratung', 100.0, 19), entry_item(2, 'Kleinteil', 0.09, 5.5, entries.reduced)], mixed=True)

    state = state_of(tmp_path, kind, page_of(kind, world, doc_id))

    assert state['rateRows'] == [['Mehrwertsteuer 19 % auf 100,00 € netto:', '19,00 €'], ['Mehrwertsteuer 5,5 % auf 0,09 € netto:', '0,00 €']]
    assert (state['sumNet'], state['taxAmount'], state['sumGross']) == ('100,09 €', '19,00 €', '119,09 €')
    stored = columns(world.db, doc_id)
    assert (stored['SumNet'], stored['TaxAmount'], stored['SumGross']) == (1000900, 190000, 1190900)


@NO_NODE
def test_the_one_step_rounding_of_the_display_is_the_one_of_the_server_and_the_two_step_one_stays_as_it_was(world, tmp_path):
    """``mixTax(net, Satz, true)`` gegen ``tax_groups.group_tax(…, exact=True)``: viele Nettosummen (auch negative) und Sätze, auf den Cent gleich – und ohne den dritten Wert
    rechnet ``mixTax`` zweistufig, wie immer."""
    nets = list(range(-150, 600)) + [999, 1000, 1001, 4999, 5000, 5001, 99999, 123456, 1234567, 99999999, 123456789, 99999999999]
    rates = ['19', '7', '5.5', '0', '16', '5', '10.5', '2.25', '100', '19.25', '7.8', '1']
    program = (f"var nets = {json.dumps(nets)}, rates = {json.dumps(rates)}, one = {{}}, two = {{}};"
               "rates.forEach(function (r) { one[r] = nets.map(function (n) { return mixTax(n, parseFloat(r), true); });"
               "                              two[r] = nets.map(function (n) { return mixTax(n, parseFloat(r)); }); });"
               "emit('one', one); emit('two', two);")

    out = run_mask(tmp_path, page_of('invoice', world), 'invoice', JS + program)

    for rate in rates:
        assert out['one'][rate] == [group_tax(net * 100, Decimal(rate), exact=True) // 100 for net in nets], rate
        assert out['two'][rate] == [group_tax(net * 100, Decimal(rate)) // 100 for net in nets], rate
    assert out['one']['5.5'][nets.index(9)] == 0 and out['two']['5.5'][nets.index(9)] == 1             # 0,09 € bei 5,5 %: einstufig 0,00 €, zweistufig 0,01 €


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_with_an_entry_in_the_head_the_tax_line_rounds_once_too_and_with_the_preset_it_stays_as_it_was(world, entries, tmp_path, kind):
    """0,09 € bei 5,5 %: 0,00 €. 45,00 € bei 10,7 %: 4,815 € ergeben einstufig 4,82 € – die Gleitkomma-Rechnung der Steuerzeile (``calculateTotals``) zeigte 4,81 €."""
    odd = world.db.insert_tax_rate(label='Satz 10,7 % (erfunden)', bp=1070, kind=STANDARD, revenue_coa_id=entries.coa[4150])
    small = head_doc(kind, world, [item(1, 'Kleinteil', 0.09, 5.5)], entries.reduced, 0.055)
    halves = head_doc(kind, world, [item(1, 'Beratung', 45.0, 10.7)], odd, 0.107, number=number(kind, 101))
    plain = make_doc(kind, world, ONE_RATE, mixed=False, number=number(kind, 102))

    small_state = state_of(tmp_path, kind, page_of(kind, world, small))
    halves_state = state_of(tmp_path, kind, page_of(kind, world, halves))
    plain_state = state_of(tmp_path, kind, page_of(kind, world, plain))

    assert (small_state['sumNet'], small_state['taxAmount'], small_state['sumGross']) == ('0,09 €', '0,00 €', '0,09 €')
    assert (halves_state['sumNet'], halves_state['taxAmount'], halves_state['sumGross']) == ('45,00 €', '4,82 €', '49,82 €')
    for doc_id, expected in ((small, (900, 0, 900)), (halves, (450000, 48200, 498200))):
        stored = columns(world.db, doc_id)
        assert (stored['SumNet'], stored['TaxAmount'], stored['SumGross']) == expected                  # der Server sagt dasselbe
    assert (plain_state['taxAmount'], plain_state['sumGross']) == ('28,50 €', '178,50 €')               # die Vorbelegung: wie immer


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_display_of_documents_with_entries_agrees_with_what_the_server_stores(world, entries, tmp_path, kind):
    items = [entry_item(1, 'Beratung', 33.33, 5.5, entries.reduced, quantity=3), entry_item(2, 'Heft', 0.10, 5.5, entries.reduced, quantity=3),
             item(3, 'Fachbuch', 12.35, 19), entry_item(4, 'Nachhilfe', 80.0, 0, entries.free)]
    doc_id = make_doc(kind, world, items, mixed=True)
    stored = columns(world.db, doc_id)

    state = state_of(tmp_path, kind, page_of(kind, world, doc_id))

    def euro(minor):
        return f'{minor / 10000:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.') + ' €'
    assert (state['sumNet'], state['taxAmount'], state['sumGross']) == (euro(stored['SumNet']), euro(stored['TaxAmount']), euro(stored['SumGross']))


# ── Der Hinweis zum Grund einer Steuerbefreiung ──────────────────────────────

NOTE_PREFIX = 'Grund der Steuerbefreiung (steht so auf der Rechnung): '


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_note_shows_the_reason_of_a_tax_free_head_and_nothing_for_other_entries(world, entries, tmp_path, kind):
    free = head_doc(kind, world, TWO_FREE, entries.free, 0)
    reduced = head_doc(kind, world, TWO_REDUCED, entries.reduced, 0.055, number=number(kind, 101))

    free_state = state_of(tmp_path, kind, page_of(kind, world, free))
    reduced_state = state_of(tmp_path, kind, page_of(kind, world, reduced))

    assert free_state['note'] == {'hidden': False, 'text': NOTE_PREFIX + REASON, 'nodes': 1}
    assert reduced_state['note'] == {'hidden': True, 'text': '', 'nodes': 0}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_note_follows_the_choice_and_the_tax_checkbox(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)
    program = (f"emit('plain', state()); chooseHead('{e(entries.free)}'); emit('free', state()); chooseHead('7'); emit('seven', state()); "
               f"chooseHead('{e(entries.free)}'); tick('show_tax', false); emit('off', state()); tick('show_tax', true); emit('on', state());")

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, JS + program)

    assert out['plain']['note']['hidden'] is True and out['seven']['note']['hidden'] is True
    assert out['free']['note'] == {'hidden': False, 'text': NOTE_PREFIX + REASON, 'nodes': 1}
    assert out['off']['note']['hidden'] is True and out['off']['note']['text'] == ''            # ohne Umsatzsteuer steht kein Grund einer Steuerbefreiung auf der Rechnung
    assert out['on']['note']['text'] == NOTE_PREFIX + REASON


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_with_the_checkbox_the_note_lists_each_reason_of_the_chosen_rows_once(world, entries, tmp_path, kind):
    other = world.db.insert_tax_rate(label='Steuerfrei, andere Leistung (erfunden)', bp=0, kind=EXEMPT, reason=OTHER_REASON, revenue_coa_id=entries.coa[4120])
    doc_id = make_doc(kind, world, [item(1, 'Beratung', 100.0, 19), entry_item(2, 'Nachhilfe', 80.0, 0, entries.free), item(3, 'Heft', 10.0, 7)], mixed=True)
    program = (f"emit('one', state()); addFreeRow(); choose(rows()[3], '{e(other)}'); emit('two', state()); "
               f"addFreeRow(); choose(rows()[4], '{e(entries.free)}'); emit('dup', state()); choose(rows()[1], '19'); choose(rows()[3], '7'); choose(rows()[4], '7'); emit('none', state());")

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, JS + program)

    assert out['one']['note']['text'] == NOTE_PREFIX + REASON
    assert out['two']['note']['text'] == NOTE_PREFIX + REASON + ' | ' + OTHER_REASON
    assert out['dup']['note']['text'] == out['two']['note']['text']                              # derselbe Grund steht nur einmal da
    assert out['none']['note'] == {'hidden': True, 'text': '', 'nodes': 0}


@NO_NODE
def test_a_reason_with_markup_is_shown_as_text_and_never_becomes_an_element(world, entries, tmp_path):
    world.db.update_tax_rate(entries.free, label='Steuerfrei (erfunden)', bp=0, kind=EXEMPT, reason=HOSTILE, revenue_coa_id=entries.coa[4120])
    doc_id = head_doc('invoice', world, TWO_FREE, entries.free, 0)

    state = state_of(tmp_path, 'invoice', page_of('invoice', world, doc_id))

    assert state['note'] == {'hidden': False, 'text': NOTE_PREFIX + HOSTILE, 'nodes': 1}        # ein Textknoten: kein <img>, kein <script>


# ═════════════════════════════════════════════════════════════════════════════
# 5. Maske → Server: Was die Maske baut, nimmt der Server an und schreibt die erwartete Momentaufnahme
# ═════════════════════════════════════════════════════════════════════════════

def saved_through_the_mask(tmp_path, world, kind, doc_id, program):
    """Das Formular von ``doc_id`` öffnen, ``program`` laufen lassen, speichern, wie die Maske es schickt – und die Antwort des Servers."""
    payload = payload_of(tmp_path, kind, page_of(kind, world, doc_id), program)
    return payload, post_save(kind, payload)


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_over_the_mask_a_5_5_percent_head_entry_becomes_the_snapshot_on_every_position(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    _payload, answer = saved_through_the_mask(tmp_path, world, kind, doc_id, f"chooseHead('{e(entries.reduced)}'); {SAVE_FUNCTION[kind]}();")

    assert answer['success'] is True, answer
    assert snapshot_columns(world.db, doc_id) == [('S', 550, None, entries.coa[4150])] * 2
    assert [(r['TaxRate'], r['TaxRateBp']) for r in stored_items(world.db, doc_id)] == [(5.5, 550)] * 2
    assert columns(world.db, doc_id)['TaxRate'] == 0.055 and columns(world.db, doc_id)['MixedTaxRates'] is None


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_over_the_mask_a_tax_free_head_entry_makes_a_tax_free_document_with_reason_and_account(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, TWO_FREE, mixed=False, taxRate=0)

    _payload, answer = saved_through_the_mask(tmp_path, world, kind, doc_id, f"chooseHead('{e(entries.free)}'); {SAVE_FUNCTION[kind]}();")

    assert answer['success'] is True, answer
    assert snapshot_columns(world.db, doc_id) == [('E', 0, REASON, entries.coa[4120])] * 2
    stored = columns(world.db, doc_id)
    assert (stored['TaxRate'], stored['TaxAmount'], stored['SumNet'], stored['SumGross']) == (0.0, 0, 1000000, 1000000)


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_over_the_mask_19_tax_free_and_5_5_percent_make_a_flagged_document_with_two_snapshots(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)
    program = (f"tick('mixed_tax', true); addFreeRow(); choose(rows()[1], '{e(entries.free)}'); choose(rows()[2], '{e(entries.reduced)}'); "
               f"rows()[2].querySelector('.item-description').value = 'Heft'; rows()[2].querySelector('.item-price').value = '10'; calculateTotals(); {SAVE_FUNCTION[kind]}();")

    payload, answer = saved_through_the_mask(tmp_path, world, kind, doc_id, program)

    assert answer['success'] is True, answer
    assert [i.get('taxRateId') for i in payload['items']] == [None, entries.free, entries.reduced]
    assert snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT, ('E', 0, REASON, entries.coa[4120]), ('S', 550, None, entries.coa[4150])]
    stored = columns(world.db, doc_id)
    assert has_mixed_rates(stored['MixedTaxRates']) and (stored['SumNet'], stored['TaxAmount']) == (1600000, 190000 + 5500)


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_over_the_mask_a_19_percent_entry_with_its_own_account_carries_that_account(world, entries, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    _payload, answer = saved_through_the_mask(tmp_path, world, kind, doc_id, f"chooseHead('{e(entries.special)}'); {SAVE_FUNCTION[kind]}();")

    assert answer['success'] is True, answer
    assert snapshot_columns(world.db, doc_id) == [('S', 1900, None, entries.coa[4180])] * 2


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_over_the_mask_choosing_a_preset_again_drops_the_snapshot_like_the_form_of_before(world, entries, tmp_path, kind):
    doc_id = head_doc(kind, world, TWO_REDUCED, entries.reduced, 0.055)
    program = f"chooseHead('19'); {SAVE_FUNCTION[kind]}();"
    assert snapshot_columns(world.db, doc_id) == [('S', 550, None, entries.coa[4150])] * 2

    payload, answer = saved_through_the_mask(tmp_path, world, kind, doc_id, program)

    assert answer['success'] is True, answer
    assert 'taxRateId' not in payload and snapshot_columns(world.db, doc_id) == [NO_SNAPSHOT] * 2
    assert columns(world.db, doc_id)['TaxRate'] == 0.19


TWO_SEVEN = [item(1, 'Fachbuch', 50.0, 7), item(2, 'Heft', 10.01, 7)]


def mixed_preset_doc(kind, world, entries, **over):
    """Die Vorbelegung mit ihrer ID an den Positionen, daneben ein steuerfreier Eintrag: 19 %, steuerfrei und 7 % in einem Beleg (die Positionen 1 und 3 sind die Vorbelegung)."""
    items = [entry_item(1, 'Beratung', 100.0, 19, entries.preset19), entry_item(2, 'Nachhilfe', 80.0, 0, entries.free), entry_item(3, 'Fachbuch', 50.0, 7, entries.preset7)]
    return make_doc(kind, world, items, mixed=True, **over)


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('make', ['head reduced', 'head free', 'mixed', 'head preset 19', 'head preset 7', 'mixed with presets', 'head special'])
def test_over_the_mask_a_draft_survives_opening_and_saving_unchanged(world, entries, tmp_path, kind, make):
    """Beleg speichern, die Seite laden, nichts ändern, speichern: dieselben Zeilen – jede Spalte von ``Invoices`` und ``InvoiceItems`` außer dem Zeitstempel, auch die Momentaufnahmen.
    Das gilt auch für die Vorbelegung, die über ihre ID gespeichert wurde (2.8.2): Die Maske schickt für sie nie eine ID – der Server hat für sie nie eine Momentaufnahme geschrieben, es
    gibt nichts, was „Speichern“ wieder auf NULL setzen könnte (bis 2.8.1 stand dort ``TaxRateBp`` 1900, und es fiel beim ersten Speichern)."""
    doc_id = {'head reduced': lambda: head_doc(kind, world, TWO_REDUCED, entries.reduced, 0.055),
              'head free': lambda: head_doc(kind, world, TWO_FREE, entries.free, 0),
              'mixed': lambda: mixed_doc(kind, world, entries),
              'head preset 19': lambda: head_doc(kind, world, ONE_RATE, entries.preset19, 0.19),
              'head preset 7': lambda: head_doc(kind, world, TWO_SEVEN, entries.preset7, 0.07),
              'mixed with presets': lambda: mixed_preset_doc(kind, world, entries),
              'head special': lambda: head_doc(kind, world, ONE_RATE, entries.special, 0.19)}[make]()
    before = doc_state(world.db, doc_id)
    if 'preset' in make:                                                          # die Vorbelegung schreibt auch mit ID keine Momentaufnahme
        preset_rows = [row for row in before[1] if row['TaxRate'] in (19.0, 7.0)]
        assert preset_rows and all(row['TaxRateBp'] is None for row in preset_rows)

    _payload, answer = saved_through_the_mask(tmp_path, world, kind, doc_id, f'{SAVE_FUNCTION[kind]}();')

    assert answer['success'] is True, answer
    assert doc_state(world.db, doc_id) == before


@NO_NODE
def test_over_the_mask_a_vanished_entry_is_replaced_by_choosing_another_and_then_the_save_works(world, entries, tmp_path):
    doc_id = head_doc('invoice', world, TWO_REDUCED, entries.reduced, 0.055)
    world.db.delete_tax_rate(entries.reduced)

    _payload, refused = saved_through_the_mask(tmp_path, world, 'invoice', doc_id, 'saveInvoice();')
    _payload, answer = saved_through_the_mask(tmp_path, world, 'invoice', doc_id, f"chooseHead('{e(entries.special)}'); saveInvoice();")

    assert refused['success'] is False and answer['success'] is True, answer
    assert snapshot_columns(world.db, doc_id) == [('S', 1900, None, entries.coa[4180])] * 2


# ═════════════════════════════════════════════════════════════════════════════
# 6. Über HTTP
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def books(auto_login):
    w = build_world(auto_login.db)
    w.csrf = auto_login.csrf
    return w


@pytest.mark.parametrize('path', ['/invoice', '/quote'])
def test_over_http_the_pages_offer_the_choices_of_the_stammdaten(server_port, books, path):
    reduced = books.db.insert_tax_rate(label='Ermäßigt 5,5 % (erfunden)', bp=550, kind=STANDARD, revenue_coa_id=account(books.db, 4150))

    page = get(server_port, path)

    assert page.status == 200
    assert labels(head_of(page.text)) == [('19', '19 %'), ('7', '7 %'), (e(reduced), 'Ermäßigt 5,5 % (erfunden)')]
    assert 'id="tax_rate" value="19"' in page.text and picked(head_of(page.text)) == '19'

