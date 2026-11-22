# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Das Häkchen „Steuersatz je Position“ in den Masken von Rechnung und Angebot (2.7.0, ``server/pages_invoice.py``, ``server/pages_quote.py``).

Mit dem Häkchen bekommt die Positionstabelle die Spalte „USt“ (ein Auswahlfeld je Zeile), und der Summenblock zeigt je Satz eine Zeile; das
Speichern schickt dann ``taxMode: 'mixed'`` und je Position ihren Satz (``handlers._tax_fields``). **Ohne das Häkchen verhält sich die Maske wie
vorher:** dasselbe Aussehen und genau der Payload, den sie bisher geschickt hat – kein ``taxMode``, je Position der Satz des Felds
„Mehrwertsteuer“. Die wählbaren Sätze kommen vom Server (``tax_groups.MIXED_TAX_RATES``, als JSON im Skriptblock); im Quelltext der Maske steht
kein Satz und keine Zahl von Sätzen.

Geprüft wird in vier Schichten:

* das HTML, das der Server schreibt (Häkchen, Spalte, vorgewählter Satz je Zeile, gesperrt, ein Satz außerhalb der Liste, alles maskiert);
* das Skript der Seite – das echte, aus der Seite geschnitten – in Node, auf einer kleinen DOM-Attrappe, die aus dem echten HTML der Seite
  gebaut wird (``HARNESS``): der Payload mit und ohne Häkchen, die Summenzeilen je Satz, die Rundung je Gruppe (gegen ``money``), neue Zeilen,
  das Häkchen selbst;
* statisch: kein neues ``innerHTML`` mit Daten, im neuen Skript nur ``var`` und ``function``, ein Block für beide Masken;
* der Rundlauf über HTTP: Beleg speichern, Seite laden, unverändert speichern – dieselben Datenbankzeilen –, Angebot → Rechnung.

Die Tests mit Node überspringen sich ohne Node. Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import datetime
import inspect
import json
import pathlib
import re
import shutil
import subprocess
from decimal import Decimal
from html.parser import HTMLParser

import pytest

import server.handlers as handlers
import tax_groups
from money import round_minor, tax_from_net
from server import pages_invoice
from server.pages_invoice import (MIXED_TAX_JS, PageInvoice, mixed_default_key, mixed_rate_choices, mixed_select_html,
                                  mixed_tax_script, rate_key)
from server.pages_quote import PageQuote
from tests.document_helpers import post_json
from tests.dom_helpers import Dom, data_block
from tests.http_client import get, own_origin, post
from tests.invoice_world import build_world, columns, force_status, make_payload, query_rows, run_sql, save

NODE = shutil.which('node')
NO_NODE = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die Prüfungen des HTML und die statischen Prüfungen '
                                                  'derselben Datei decken das Wichtigste ab')

KINDS = ['invoice', 'quote']
EXTENDED = (Decimal('19'), Decimal('7'), Decimal('5.5'))          # die Liste um einen Satz mit Nachkommastelle erweitert


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return build_world(tmp_db)


@pytest.fixture
def extended_rates(monkeypatch):
    """Die wählbaren Sätze um 5,5 % erweitert – an der EINEN Stelle (``tax_groups``) und dort, wo ``handlers`` sie beim Import festgehalten hat."""
    monkeypatch.setattr(tax_groups, 'MIXED_TAX_RATES', EXTENDED)
    monkeypatch.setattr(handlers, 'MIXED_TAX_RATES', EXTENDED)
    return EXTENDED


def item(position, description, price, rate, quantity=1):
    """Eine Position, wie die Maske sie schickt (Satz als Prozentzahl)."""
    return {'position': position, 'quantity': quantity, 'unit': 'Stk.', 'description': description, 'unitPrice': price, 'taxRate': rate}


TWO_RATES = [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 7)]
ONE_RATE = [item(1, 'Beratung', 100.0, 19), item(2, 'Material', 50.0, 19)]


def quote_payload(world, **over):
    payload = {'quoteNumber': '26A100', 'quoteDate': '2026-06-07', 'validUntil': '2026-07-07', 'customerId': world.cust,
               'ownCompanyId': world.own, 'taxRate': 0.19, 'status': 'draft', 'items': ONE_RATE,
               'introText': 'Guten Tag', 'closingText': 'Mit freundlichen Grüßen'}
    payload.update(over)
    return payload


def make_doc(kind, world, items, mixed=True, number=None, **over):
    """Einen Beleg über den Speicherweg der Maske anlegen (``handle_invoice_save`` bzw. ``handle_quote_save``) und seine ID liefern."""
    mode = {'taxMode': 'mixed'} if mixed else {}
    if kind == 'invoice':
        answer = save(make_payload(world, items=items, invoiceNumber=number or '26R100', **mode, **over))
        assert answer['success'] is True, answer
        return answer['invoice_id']
    answer = json.loads(handlers.handle_quote_save(
        json.dumps(quote_payload(world, items=items, quoteNumber=number or '26A100', **mode, **over)).encode()))
    assert answer['success'] is True, answer
    return answer['quote_id']


def page_of(kind, world, doc_id=None):
    return PageInvoice(world.db, {}, doc_id) if kind == 'invoice' else PageQuote(world.db, {}, doc_id)


def set_item_rate(world, doc_id, position, value):
    """Den Satz einer Position an der App vorbei setzen (SQL-Konsole, Import, ältere Version)."""
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRate = ? WHERE InvoiceId = ? AND Position = ?', value, doc_id, position)


# ── Das HTML lesen ───────────────────────────────────────────────────────────

class Rows(HTMLParser):
    """Die Zeilen der Positionstabelle einer Seite: je Zeile das Auswahlfeld „USt“ mit seinen Optionen ``(Wert, Beschriftung, gewählt)`` und
    ob es gesperrt ist – so, wie ein Browser das Markup liest."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._select, self._option = [], None, None, None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get('class') or '').split()
        if tag == 'tr' and 'invoice-item-row' in classes:
            self._row = {'select': None, 'cell': None}
            self.rows.append(self._row)
        elif tag == 'td' and 'mixedTaxCol' in classes and self._row is not None:
            self._row['cell'] = a
        elif tag == 'select' and 'item-tax' in classes and self._row is not None:
            self._select = {'disabled': 'disabled' in a, 'options': [], 'attrs': a}
            self._row['select'] = self._select
        elif tag == 'option' and self._select is not None:
            self._option = [a.get('value'), '', 'selected' in a]
            self._select['options'].append(self._option)

    def handle_data(self, data):
        if self._option is not None:
            self._option[1] += data

    def handle_endtag(self, tag):
        if tag == 'option':
            self._option = None
        elif tag == 'select':
            self._select = None


def rows_of(html):
    return Rows(html).rows


def values(select):
    return [value for value, _label, _selected in select['options']]


def chosen(select):
    """Die gewählten Werte eines Auswahlfelds – genau einer, sonst ist es kaputt."""
    picked = [value for value, _label, selected in select['options'] if selected]
    assert len(picked) == 1, select
    return picked[0]


def rates_of(html):
    return [chosen(row['select']) for row in rows_of(html)]


def box_of(html):
    return Dom(html).one('input', id='mixed_tax')


def table_classes(html):
    return re.search(r'<table class="([^"]*)" id="(?:invoice|quote)_table">', html).group(1).split()


# ── Der Prüfstand für das Skript: Node mit einer DOM-Attrappe ────────────────
# Gebaut wird sie aus dem HTML der Seite (ohne die Skriptblöcke): Elemente mit Attributen, ``value``/``checked``/``disabled``, ``classList``,
# ``querySelector(All)`` (Tag, Klasse, ``#id``, ``[attr]``, ``:first-child``, Nachfahren), ``insertBefore``, ``innerHTML`` (für die festen
# Zeilenvorlagen der Seite) und ``textContent``. ``fire`` löst die Ereignisse aus wie ein Klick: erst die Zuhörer, dann das Attribut ``on<Ereignis>``.

HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const scenario = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

const VOID = new Set(['input', 'br', 'hr', 'img', 'meta', 'link']);
const ENTITIES = {'&nbsp;': ' ', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#x27;': "'", '&#39;': "'", '&amp;': '&'};
const decode = text => text.replace(/&(?:nbsp|lt|gt|quot|#x27|#39|amp);/g, m => ENTITIES[m]);

class TextNode {
  constructor(data) { this.data = data; this.parentNode = null; }
  get textContent() { return this.data; }
}

class El {
  constructor(tag) {
    this.tag = tag; this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.parentNode = null;
    this.listeners = {}; this.style = {}; this._value = undefined; this.checked = false; this.disabled = false; this.hidden = false;
  }
  get id() { return this.attrs.id || ''; }
  get className() { return this.attrs['class'] || ''; }
  set className(v) { this.attrs['class'] = String(v); }
  get classList() {
    const el = this, names = () => el.className.split(/\s+/).filter(Boolean);
    return {
      add(n) { if (!names().includes(n)) el.className = names().concat([n]).join(' '); },
      remove(n) { el.className = names().filter(x => x !== n).join(' '); },
      contains(n) { return names().includes(n); },
    };
  }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  hasAttribute(n) { return n in this.attrs; }
  removeAttribute(n) { delete this.attrs[n]; }
  get colSpan() { return Number(this.attrs.colspan || 1); }
  set colSpan(v) { this.attrs.colspan = String(v); }
  get options() { return this.children.filter(c => c.tag === 'option'); }
  get selectedIndex() { const v = this.value; return this.options.findIndex(o => o.value === v); }
  get value() {
    if (this.tag === 'option') return 'value' in this.attrs ? this.attrs.value : this.textContent;
    if (this.tag === 'select') {
      const options = this.options;
      if (this._value === undefined) return options.length ? options[0].value : '';
      const match = options.find(o => o.value === this._value);
      return match ? match.value : '';
    }
    return this._value === undefined ? '' : this._value;
  }
  set value(v) { if (this.tag === 'option') this.attrs.value = String(v); else this._value = String(v); }
  get textContent() { return this.children.map(c => c.textContent).join(''); }
  set textContent(v) {
    this.children.forEach(c => { c.parentNode = null; });
    this.children = [];
    if (String(v) !== '') this.appendChild(new TextNode(String(v)));
  }
  get innerHTML() { return this.children.map(serialize).join(''); }
  set innerHTML(html) {
    this.children.forEach(c => { c.parentNode = null; });
    this.children = [];
    parseInto(this, html);
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this; this.children.push(child); return child;
  }
  insertBefore(child, ref) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(child); else this.children.splice(i, 0, child);
    return child;
  }
  removeChild(child) {
    const i = this.children.indexOf(child);
    if (i >= 0) this.children.splice(i, 1);
    child.parentNode = null; return child;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  closest(selector) {
    const parsed = parseCompound(selector);
    for (let el = this; el instanceof El; el = el.parentNode) if (matches(el, parsed)) return el;
    return null;
  }
  querySelectorAll(selector) { return queryAll(this, selector); }
  querySelector(selector) { return queryAll(this, selector)[0] || null; }
}

function serialize(node) {
  if (node instanceof TextNode) return node.data.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const attrs = Object.entries(node.attrs).map(([n, v]) => ' ' + n + '="' + v.replace(/"/g, '&quot;') + '"').join('');
  return VOID.has(node.tag) ? '<' + node.tag + attrs + '>' : '<' + node.tag + attrs + '>' + node.children.map(serialize).join('') + '</' + node.tag + '>';
}

const PART = /[.#][\w-]+|\[[\w-]+(?:=[^\]]+)?\]|:first-child/g;
function parseCompound(text) {
  const m = /^([a-zA-Z][a-zA-Z0-9]*)?((?:[.#][\w-]+|\[[\w-]+(?:=[^\]]+)?\]|:first-child)*)$/.exec(text);
  if (!m) throw new Error('Selektor nicht unterstützt: ' + text);
  const parsed = {tag: m[1] ? m[1].toLowerCase() : null, classes: [], id: null, attrs: [], first: false};
  (m[2].match(PART) || []).forEach(token => {
    if (token[0] === '.') parsed.classes.push(token.slice(1));
    else if (token[0] === '#') parsed.id = token.slice(1);
    else if (token === ':first-child') parsed.first = true;
    else {
      const a = /^\[([\w-]+)(?:=([^\]]+))?\]$/.exec(token);
      parsed.attrs.push([a[1], a[2] === undefined ? null : a[2].replace(/^["']|["']$/g, '')]);
    }
  });
  return parsed;
}
function matches(el, p) {
  if (!(el instanceof El)) return false;
  if (p.tag && el.tag !== p.tag) return false;
  if (p.id && el.id !== p.id) return false;
  if (!p.classes.every(c => el.classList.contains(c))) return false;
  if (!p.attrs.every(([n, v]) => el.hasAttribute(n) && (v === null || el.getAttribute(n) === v))) return false;
  if (p.first && !(el.parentNode && el.parentNode.children.filter(c => c instanceof El)[0] === el)) return false;
  return true;
}
function queryAll(root, selector) {
  const chain = selector.trim().split(/\s+/).map(parseCompound), last = chain[chain.length - 1], found = [];
  const ancestorsMatch = el => {
    let i = chain.length - 2;
    for (let node = el.parentNode; i >= 0 && node; node = node.parentNode) if (matches(node, chain[i])) i--;
    return i < 0;
  };
  (function walk(node) {
    node.children.forEach(child => {
      if (child instanceof El) {
        if (matches(child, last) && ancestorsMatch(child)) found.push(child);
        walk(child);
      }
    });
  })(root);
  return found;
}

function parseInto(parent, html) {
  const token = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>"']+))?)*)\s*(\/?)>|([^<]+|<)/g;
  const stack = [parent], fresh = [];
  let m;
  while ((m = token.exec(html)) !== null) {
    const top = stack[stack.length - 1];
    if (m[0].startsWith('<!--')) continue;
    if (m[2] !== undefined) {
      const tag = m[2].toLowerCase();
      if (m[1] === '/') {
        for (let i = stack.length - 1; i > 0; i--) if (stack[i].tag === tag) { stack.length = i; break; }
      } else {
        const el = new El(tag);
        const attr = /([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+)))?/g;
        let a;
        while ((a = attr.exec(m[3])) !== null) el.attrs[a[1]] = decode(a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '');
        el.checked = 'checked' in el.attrs; el.disabled = 'disabled' in el.attrs; el.hidden = 'hidden' in el.attrs;
        if (tag === 'input' && 'value' in el.attrs) el._value = el.attrs.value;
        top.appendChild(el); fresh.push(el);
        if (!VOID.has(tag) && m[4] !== '/') stack.push(el);
      }
    } else if (m[5] !== undefined) {
      top.appendChild(new TextNode(decode(m[5])));
    }
  }
  fresh.forEach(el => {
    if (el.tag === 'textarea') el._value = el.textContent;
    if (el.tag === 'select') { const picked = el.options.find(o => 'selected' in o.attrs); if (picked) el._value = picked.value; }
  });
}

const root = new El('#document');
parseInto(root, scenario.html);
const find = id => { let hit = null; (function walk(node) { node.children.forEach(c => { if (c instanceof El) { if (!hit && c.id === id) hit = c; walk(c); } }); })(root); return hit; };
const document = {
  getElementById: find,
  querySelector: s => root.querySelector(s), querySelectorAll: s => root.querySelectorAll(s),
  createElement: tag => new El(String(tag).toLowerCase()), createTextNode: text => new TextNode(String(text)),
};

const out = {messages: []}, sent = [];
const clone = value => JSON.parse(JSON.stringify(value === undefined ? null : value));
const sandbox = {
  document, console, window: {location: {href: ''}}, sent,
  emit: (name, value) => { out[name] = clone(value); },
  showMessage: (text, type) => out.messages.push([text, type]),
  showConfirm: (text, ok) => ok(), appConfirm: (text, ok) => ok(),
  fetch: (url, options) => {
    sent.push({url, body: JSON.parse(options.body)});
    return scenario.answer === null ? new Promise(() => {}) : Promise.resolve({json: () => Promise.resolve(scenario.answer)});
  },
};
const context = vm.createContext(sandbox);
const run = code => vm.runInContext(code, context);
sandbox.fire = (el, type) => {
  (el.listeners[type] || []).forEach(fn => fn.call(el, {type, target: el}));
  const inline = el.getAttribute('on' + type);
  if (inline) run(inline);
};
sandbox.$ = id => document.getElementById(id);
sandbox.rows = () => document.querySelectorAll('.invoice-item-row');
sandbox.tick = (id, checked) => { const el = document.getElementById(id); el.checked = checked; sandbox.fire(el, 'change'); };
sandbox.type = (el, value) => { el.value = value; sandbox.fire(el, 'input'); };
sandbox.choose = (row, key) => { const s = row.querySelector('.item-tax'); s.value = key; sandbox.fire(s, 'change'); };
sandbox.snap = () => {
  const text = id => document.getElementById(id).textContent, box = document.getElementById('mixed_tax');
  return {
    sumNet: text('sum_net'), taxAmount: text('tax_amount'), sumGross: text('sum_gross'), taxBase: text('tax_base'),
    rateRows: document.querySelectorAll('.mixRateRow').map(r => [r.children[0].textContent, r.children[1].textContent]),
    taxRowShown: document.getElementById('tax_row').style.display !== 'none',
    tableOn: document.querySelector('.invoice-items').classList.contains('mixedTaxOn'),
    spans: document.querySelectorAll('tfoot td[colspan]').map(td => td.colSpan),
    box: box ? {checked: box.checked, disabled: box.disabled} : null,
    selects: document.querySelectorAll('.item-tax').map(s => s.value),
    selectsDisabled: document.querySelectorAll('.item-tax').map(s => s.disabled),
    options: document.querySelectorAll('.item-tax').map(s => s.options.map(o => [o.value, o.textContent])),
  };
};

(async () => {
  run('var rowCounter = 1;');
  scenario.sources.forEach(run);
  run(scenario.boot);
  run(scenario.program);
  await new Promise(resolve => setTimeout(resolve, 20));       // Antworten von fetch laufen lassen
  out.sent = sent;
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e.stack || e); process.exit(1); });
"""


def extract_function(script, name):
    """Der Quelltext von ``function <name>(…) { … }`` aus einem Skript – die Klammern werden gezählt, Zeichenketten und Kommentare übersprungen."""
    start = script.index(f'function {name}(')
    i = script.index('{', script.index(')', start))
    depth = 0
    while True:
        ch = script[i]
        if ch in '\'"`':
            i += 1
            while script[i] != ch:
                i += 2 if script[i] == '\\' else 1
        elif script.startswith('//', i):
            i = script.index('\n', i)
            continue
        elif script.startswith('/*', i):
            i = script.index('*/', i) + 1
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return script[start:i + 1]
        i += 1


PAGE_FUNCTIONS = ('fmtDe', 'parseDe', 'calculateRowTotal', 'calculateTotals', 'attachCalculationListeners', 'addFreeRow', 'addArticleRow',
                  'removeRow', 'renumberRows')
SAVE_FUNCTION = {'invoice': 'saveInvoice', 'quote': 'saveQuote'}

# Was die Seite beim Laden verdrahtet (die Zeilen stehen im Skript der Seite; ``page_script`` prüft, dass sie noch dort stehen)
WIRING = ("document.querySelectorAll('.invoice-item-row').forEach(attachCalculationListeners);",
          "document.getElementById('tax_rate').addEventListener('input', calculateTotals);")

# Die Werte, die das Skript der Seite beim Laden aus den Stammdaten einsetzt (``updateCustomerAddress``) – die Attrappe führt es nicht aus
INVOICE_BOOT = """
var initialOpenFields = {paymentTerms: $('payment_terms').value, orderNumber: $('order_number').value,
                         buyerReference: $('buyer_route_id').value, deliveryDate: $('delivery_date').value};
"""


def page_script(html, kind):
    """Das Skript der Maske (der Block mit ``saveInvoice`` bzw. ``saveQuote``) und der gemeinsame Block mit den Bausteinen für den Satz je Position."""
    dom = Dom(html)
    (main,) = [s for s in dom.scripts if f'function {SAVE_FUNCTION[kind]}(' in s]
    (mix,) = [s for s in dom.scripts if 'function mixOn(' in s]
    for line in WIRING:
        assert line in main, line
    return dom, main, mix


def run_mask(tmp_path, html, kind, program, answer=None, real_send=False):
    """Die Maske in Node: das HTML der Seite als DOM, der Block mit den Bausteinen und die Funktionen der Seite (``PAGE_FUNCTIONS``, das Speichern), dazu
    ``program`` (JavaScript, mit ``emit``, ``fire``, ``tick``, ``type``, ``choose``, ``rows``, ``snap`` …). ``answer``: was ``fetch`` antwortet (ohne:
    keine Antwort). Liefert, was das Programm mit ``emit`` ausgegeben hat, die Meldungen (``messages``) und alles, was die Maske abgeschickt hat
    (``sent``: bei der Rechnung ``sendInvoice(daten, …)`` – es sei denn ``real_send`` –, beim Angebot jede Anfrage an ``fetch``)."""
    dom, main, mix = page_script(html, kind)
    body = re.sub(r'<(script|style)\b.*?</\1>', '', html, flags=re.S | re.I)
    functions = [extract_function(main, name) for name in PAGE_FUNCTIONS + (SAVE_FUNCTION[kind],)]
    prelude = 'var articlesData = ' + json.dumps(data_block(dom, 'articlesData')) + ';'
    boot = '\n'.join(WIRING) + '\ncalculateTotals();\n'
    sources = [mix, prelude] + functions
    if kind == 'invoice':
        boot += INVOICE_BOOT
        if real_send:
            sources.append(extract_function(main, 'sendInvoice'))
        else:
            sources.append('function sendInvoice(data) { sent.push(JSON.parse(JSON.stringify(data))); }')
    scenario = {'html': body, 'sources': sources, 'boot': boot, 'program': program, 'answer': answer}
    harness, data = tmp_path / 'harness.js', tmp_path / 'scenario.json'
    harness.write_text(HARNESS, encoding='utf-8')
    data.write_text(json.dumps(scenario), encoding='utf-8')
    done = subprocess.run([NODE, str(harness), str(data)], capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def sent_payload(out, kind):
    """Die Daten, die das Speichern der Maske an den Server schickt."""
    assert len(out['sent']) == 1, out['messages']
    return out['sent'][0] if kind == 'invoice' else out['sent'][0]['body']


def stylesheet():
    return (pathlib.Path(pages_invoice.__file__).resolve().parent.parent / 'buch.css').read_text(encoding='utf-8')


# ── 1. Das HTML des Servers ──────────────────────────────────────────────────

@pytest.mark.parametrize('kind', KINDS)
def test_a_new_document_offers_the_checkbox_and_the_column_but_starts_without_a_rate_per_position(world, kind):
    html = page_of(kind, world)

    box = box_of(html)
    assert 'checked' not in box and 'disabled' not in box and box['onchange'] == 'mixToggle()'
    assert '<th class="mixedTaxCol">USt</th>' in html and ' Steuersatz je Position</label>' in html
    assert table_classes(html) == ['invoice-items']                                  # die Spalte bleibt aus: kein mixedTaxOn
    (row,) = rows_of(html)                                                           # die eine leere Zeile trägt das Auswahlfeld schon
    assert row['select']['options'] == [['19', '19 %', True], ['7', '7 %', False]]
    assert 'disabled' not in row['select']['attrs']


@pytest.mark.parametrize('kind', KINDS)
def test_the_checkbox_sits_in_the_cell_below_the_tax_checkbox_inside_the_row_the_print_leaves_out(world, kind):
    html = page_of(kind, world)

    cell = html[html.rindex('<tr class="no-pdf">', 0, html.index('id="show_tax"')):html.index('id="tax_row"')]
    assert 'id="show_tax"' in cell and 'id="mixed_tax"' in cell and cell.index('id="show_tax"') < cell.index('id="mixed_tax"')
    assert cell.count('<td') == 1                                                    # dieselbe Zelle, keine eigene Zeile
    assert html.index('id="show_tax"') < html.index('id="mixed_tax"') < html.index('id="tax_row"')


@pytest.mark.parametrize('kind', KINDS)
def test_the_new_markup_carries_no_inline_style_and_the_stylesheet_has_the_rules(world, kind):
    html = page_of(kind, world)

    for tag in re.findall(r'<(?:th|td|select)\b[^>]*(?:mixedTaxCol|item-tax)[^>]*>', html):
        assert 'style=' not in tag, tag
    assert 'style=' not in re.search(r'<label><input type="checkbox" id="mixed_tax".*?</label>', html, re.S).group(0)
    sheet = stylesheet()
    assert re.search(r'\.invoice-items \.mixedTaxCol\s*\{ display: none; \}', sheet)
    assert re.search(r'\.invoice-items\.mixedTaxOn \.mixedTaxCol\s*\{ display: table-cell; \}', sheet)


@pytest.mark.parametrize('kind', KINDS)
def test_a_document_with_one_rate_loads_without_the_checkbox_and_each_row_with_its_stored_rate(world, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    html = page_of(kind, world, doc_id)

    assert 'checked' not in box_of(html) and table_classes(html) == ['invoice-items']
    assert rates_of(html) == ['19', '19']


@pytest.mark.parametrize('kind', KINDS)
def test_a_mixed_draft_loads_with_the_checkbox_on_the_column_shown_and_each_row_with_its_rate(world, kind):
    doc_id = make_doc(kind, world, TWO_RATES + [item(3, 'Heft', 5.0, 7)])

    html = page_of(kind, world, doc_id)

    box = box_of(html)
    assert 'checked' in box and 'disabled' not in box
    assert table_classes(html) == ['invoice-items', 'mixedTaxOn']
    assert rates_of(html) == ['19', '7', '7']
    assert all(values(row['select']) == ['19', '7'] for row in rows_of(html))


@pytest.mark.parametrize('kind', KINDS)
def test_a_document_without_tax_has_the_checkbox_off_and_locked(world, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False, showTax=False)

    html = page_of(kind, world, doc_id)

    box = box_of(html)
    assert 'checked' not in box and 'disabled' in box
    assert table_classes(html) == ['invoice-items']


def test_a_sent_mixed_invoice_shows_the_state_but_everything_is_locked(world):
    invoice_id = make_doc('invoice', world, TWO_RATES)
    force_status(world.db, invoice_id, 'sent')

    html = page_of('invoice', world, invoice_id)

    box = box_of(html)
    assert 'checked' in box and 'disabled' in box
    assert table_classes(html) == ['invoice-items', 'mixedTaxOn']
    rows = rows_of(html)
    assert [chosen(row['select']) for row in rows] == ['19', '7'] and all(row['select']['disabled'] for row in rows)


def test_a_sent_invoice_with_one_rate_has_the_checkbox_off_and_locked_and_the_selects_locked(world):
    invoice_id = make_doc('invoice', world, ONE_RATE, mixed=False)
    force_status(world.db, invoice_id, 'sent')

    html = page_of('invoice', world, invoice_id)

    assert 'checked' not in box_of(html) and 'disabled' in box_of(html)
    assert table_classes(html) == ['invoice-items'] and all(row['select']['disabled'] for row in rows_of(html))


def test_the_copy_of_a_mixed_invoice_opens_as_a_new_mixed_draft(world):
    invoice_id = make_doc('invoice', world, TWO_RATES)

    html = PageInvoice(world.db, {}, None, copy_from=invoice_id)

    assert 'checked' in box_of(html) and 'disabled' not in box_of(html)
    assert rates_of(html) == ['19', '7'] and table_classes(html) == ['invoice-items', 'mixedTaxOn']
    assert Dom(html).one('input', id='invoice_id')['value'] == ''                    # eine Kopie ist eine neue Rechnung


def test_the_copy_of_a_sent_mixed_invoice_is_open_for_editing(world):
    invoice_id = make_doc('invoice', world, TWO_RATES)
    force_status(world.db, invoice_id, 'sent')

    html = PageInvoice(world.db, {}, None, copy_from=invoice_id)

    assert 'checked' in box_of(html) and 'disabled' not in box_of(html)
    assert rates_of(html) == ['19', '7'] and not any(row['select']['disabled'] for row in rows_of(html))


def test_the_draft_of_a_replacement_invoice_carries_the_state_like_any_draft(world):
    invoice_id = make_doc('invoice', world, TWO_RATES)
    run_sql(world.db, 'UPDATE Invoices SET ReplacesInvoiceId = ? WHERE ID = ?', invoice_id, invoice_id)

    html = page_of('invoice', world, invoice_id)

    assert 'checked' in box_of(html) and rates_of(html) == ['19', '7']


@pytest.mark.parametrize('stored, expected', [(0.07, '7'), (7, '7'), ('7', '7'), ('7.0', '7'), (0.19, '19'), (19.0, '19'), ('19', '19')])
def test_a_stored_rate_in_any_spelling_selects_the_same_option(world, stored, expected):
    """Gespeichert ist der Satz einer Position mal als Bruch, mal als Prozentzahl, auch als Text – die Auswahl zeigt immer dieselbe Option."""
    invoice_id = make_doc('invoice', world, TWO_RATES)
    set_item_rate(world, invoice_id, 2, stored)

    assert rates_of(page_of('invoice', world, invoice_id)) == ['19', expected]


@pytest.mark.parametrize('kind', KINDS)
def test_a_rate_outside_the_list_shows_as_its_own_chosen_option_and_nothing_is_reinterpreted(world, kind):
    doc_id = make_doc(kind, world, TWO_RATES)
    set_item_rate(world, doc_id, 2, 5.5)                                              # kein wählbarer Satz

    html = page_of(kind, world, doc_id)

    first, second = (row['select'] for row in rows_of(html))
    assert first['options'] == [['19', '19 %', True], ['7', '7 %', False]]            # die andere Zeile bleibt, wie sie war
    assert second['options'] == [['19', '19 %', False], ['7', '7 %', False], ['5.5', '5,5 %', True]]


def test_an_unreadable_stored_rate_shows_an_empty_choice_and_never_the_text(world):
    invoice_id = make_doc('invoice', world, TWO_RATES)
    set_item_rate(world, invoice_id, 2, '<img src=x onerror=alert(1)>')

    html = page_of('invoice', world, invoice_id)

    (_, second) = rows_of(html)
    assert second['select']['options'][0] == ['', '– wählen –', True]
    assert [chosen(row['select']) for row in rows_of(html)] == ['19', '']
    assert '<img src=x' not in html and 'onerror=alert(1)' not in html


@pytest.mark.parametrize('bad', ['nan', 'inf', '-inf', 'abc', '1e999', ''])
def test_a_rate_that_is_no_number_never_breaks_the_page(world, bad):
    invoice_id = make_doc('invoice', world, TWO_RATES)
    set_item_rate(world, invoice_id, 2, bad)

    html = page_of('invoice', world, invoice_id)

    assert len(rows_of(html)) == 2 and chosen(rows_of(html)[0]['select']) == '19'


def test_a_rate_that_cannot_be_read_makes_the_mask_ask_instead_of_guessing():
    assert rate_key('abc') is None and rate_key(None) is None and rate_key('') is None and rate_key(float('inf')) is None
    assert mixed_select_html(None).startswith('<select class="item-tax"><option value="" selected>– wählen –</option>')
    assert mixed_select_html('abc', ' disabled').startswith('<select class="item-tax" disabled>')


# ── Die Liste der Sätze kommt vom Server, nirgends steht ein Satz im Quelltext ──

def test_the_choices_follow_tax_groups_in_order_with_a_decimal_comma(monkeypatch):
    assert mixed_rate_choices() == [('19', '19 %'), ('7', '7 %')]
    monkeypatch.setattr(tax_groups, 'MIXED_TAX_RATES', (Decimal('7'), Decimal('5.50'), Decimal('19'), Decimal('0')))

    assert mixed_rate_choices() == [('7', '7 %'), ('5.5', '5,5 %'), ('19', '19 %'), ('0', '0 %')]


@pytest.mark.parametrize('head, expected', [(19, '19'), (7, '7'), (0.07, '7'), ('19', '19'), (16, '19'), (None, '19'), ('abc', '19'), (5.5, '19')])
def test_a_new_row_takes_the_rate_of_the_document_if_the_list_knows_it_else_the_first(head, expected):
    assert mixed_default_key(head) == expected


def test_a_new_row_takes_the_first_rate_of_the_list_when_the_document_rate_is_not_in_it(monkeypatch):
    monkeypatch.setattr(tax_groups, 'MIXED_TAX_RATES', (Decimal('7'), Decimal('19')))
    assert mixed_default_key(16) == '7' and mixed_default_key(19) == '19'
    monkeypatch.setattr(tax_groups, 'MIXED_TAX_RATES', ())
    assert mixed_default_key(19) == '' and mixed_select_html(19).startswith('<select class="item-tax"><option value="19" selected>')


@pytest.mark.parametrize('kind', KINDS)
def test_with_a_list_extended_by_5_5_the_mask_shows_three_options_and_the_script_gets_all_three(world, extended_rates, kind):
    html = page_of(kind, world)

    (row,) = rows_of(html)
    assert row['select']['options'] == [['19', '19 %', True], ['7', '7 %', False], ['5.5', '5,5 %', False]]
    _dom, _main, mix = page_script(html, kind)
    listed = json.loads(re.search(r'var MIX_RATES = (.*?);\n', mix, re.S).group(1))
    assert listed == [{'value': '19', 'label': '19 %'}, {'value': '7', 'label': '7 %'}, {'value': '5.5', 'label': '5,5 %'}]
    assert listed == [{'value': value, 'label': label} for value, label in mixed_rate_choices()]


@pytest.mark.parametrize('kind', KINDS)
def test_with_the_extended_list_a_document_at_5_5_saves_and_loads_without_any_odd_option(world, extended_rates, kind):
    """Nichts ist fest verdrahtet: Kommt 5,5 % in die Liste, lässt sich der Beleg speichern, und die Maske zeigt den Satz als gewöhnliche Option."""
    doc_id = make_doc(kind, world, [item(1, 'Beratung', 100.0, 19), item(2, 'Heft', 10.0, 5.5)])

    html = page_of(kind, world, doc_id)

    assert 'checked' in box_of(html) and rates_of(html) == ['19', '5.5']
    assert all(values(row['select']) == ['19', '7', '5.5'] for row in rows_of(html))


def test_no_rate_and_no_count_of_rates_is_named_in_the_source_of_the_mask_pieces():
    code = re.sub(r'//[^\n]*', '', MIXED_TAX_JS)                                     # die Kommentare zählen nicht
    assert not re.search(r'(?<![\d.])(19|7|5\.5)(?![\d.])', code), 'ein Satz steht im Quelltext des Skripts'
    assert 'MIX_RATES.length' in code and 'MIX_RATES[0]' in code                      # nur „der erste der Liste“ und Schleifen über alle
    assert not re.search(r'MIX_RATES\[[1-9]\]|MIX_RATES\.length\s*(===?|[<>]=?)\s*\d', code)
    source = ''.join(inspect.getsource(f) for f in (mixed_rate_choices, mixed_default_key, mixed_select_html, mixed_tax_script))
    assert not re.search(r'(?<![\w.])(19|7)(?![\w.])', re.sub(r'#[^\n]*|""".*?"""', '', source, flags=re.S)), 'ein Satz steht im Quelltext der Seite'


def test_the_select_escapes_value_and_label_of_every_option():
    source = inspect.getsource(mixed_select_html)
    assert '_html.escape(value, quote=True)' in source and '_html.escape(label)' in source


def test_the_list_reaches_the_script_only_through_script_json():
    source = inspect.getsource(mixed_tax_script)
    assert 'script_json(' in source and 'json.dumps' not in source
    assert mixed_tax_script().count('<script>') == 1 and mixed_tax_script().count('</script>') == 1     # kein Wert beendet den Block


@pytest.mark.parametrize('kind', KINDS)
def test_the_articles_carry_their_rate_as_a_choice_value_for_the_article_rows(world, kind):
    for name, rate in (('Beratung', 19), ('Fachbuch', 7), ('Bruch', 0.07), ('Kurios', 5.5), ('Frei', 0), ('Text', 'abc')):
        world.db.insert_article(name, unit_price=10, tax_rate=rate)

    articles = data_block(Dom(page_of(kind, world)), 'articlesData')

    assert {a['name']: a['mixRate'] for a in articles.values()} == {
        'Beratung': '19', 'Fachbuch': '7', 'Bruch': '7', 'Kurios': '5.5', 'Frei': '0', 'Text': None}
    assert {a['name']: a['taxRate'] for a in articles.values()}['Fachbuch'] == 7           # die bisherige Angabe bleibt unverändert


# ── 2. Das Skript der Seite (Node) ───────────────────────────────────────────

def expected_payload(world, doc_id, **over):
    """Der Payload, den die Maske vor dem Häkchen für den Beleg ``make_payload`` (ohne ``taxMode``) schickte – festgehalten, nicht aus dem heutigen Skript abgeleitet."""
    payload = {
        'invoiceNumber': '26R100', 'numberSuggestion': None, 'invoiceDate': '2026-06-07', 'customerId': world.cust,
        'customerNumber': 'K-100', 'ownCompanyId': world.own, 'buyerReference': '04011000-1234512345-35',
        'paymentTerms': 'Zahlbar in 14 Tagen', 'paymentTermsDays': 14, 'dueDate': '2026-06-21', 'bankAccountId': world.bank,
        'netAmount': 150.0, 'taxRate': 0.19, 'showTax': True, 'taxAmount': 28.5, 'grossAmount': 178.5, 'currency': 'EUR',
        'items': [
            {'position': 1, 'quantity': 1, 'unit': 'Stk.', 'description': 'Beratung', 'unitPrice': 100, 'totalPrice': 100, 'taxRate': 19},
            {'position': 2, 'quantity': 1, 'unit': 'Stk.', 'description': 'Material', 'unitPrice': 50, 'totalPrice': 50, 'taxRate': 19}],
        'orderNumber': 'B-17', 'deliveryDate': '2026-06-05', 'discountPercentage': 2, 'discountDays': 7, 'invoiceId': doc_id}
    payload.update(over)
    return payload


@NO_NODE
def test_without_the_checkbox_the_invoice_sends_exactly_the_payload_of_before(world, tmp_path):
    invoice_id = make_doc('invoice', world, ONE_RATE, mixed=False)

    out = run_mask(tmp_path, page_of('invoice', world, invoice_id), 'invoice', "$('customer_number').value = 'K-100'; saveInvoice();")

    payload = sent_payload(out, 'invoice')
    assert payload == expected_payload(world, invoice_id)
    assert 'taxMode' not in payload and list(payload) == list(expected_payload(world, invoice_id))     # auch die Reihenfolge der Schlüssel


@NO_NODE
def test_without_the_checkbox_the_quote_sends_exactly_the_payload_of_before(world, tmp_path):
    quote_id = make_doc('quote', world, ONE_RATE, mixed=False)

    out = run_mask(tmp_path, page_of('quote', world, quote_id), 'quote', 'saveQuote();')

    payload = sent_payload(out, 'quote')
    assert payload == {
        'quoteNumber': '26A100', 'quoteDate': '2026-06-07', 'customerId': world.cust, 'ownCompanyId': world.own, 'validUntil': '2026-07-07',
        'introText': 'Guten Tag', 'closingText': 'Mit freundlichen Grüßen', 'taxRate': 0.19, 'showTax': True, 'status': 'draft',
        'items': [{'position': 1, 'quantity': 1, 'unit': 'Stk.', 'description': 'Beratung', 'unitPrice': 100, 'taxRate': 19},
                  {'position': 2, 'quantity': 1, 'unit': 'Stk.', 'description': 'Material', 'unitPrice': 50, 'taxRate': 19}],
        'quoteId': quote_id}
    assert list(payload) == ['quoteNumber', 'quoteDate', 'customerId', 'ownCompanyId', 'validUntil', 'introText', 'closingText', 'taxRate',
                             'showTax', 'status', 'items', 'quoteId']


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_without_the_checkbox_the_rate_field_goes_to_every_position_whatever_the_hidden_selects_show(world, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind,
                   f"choose(rows()[1], '7'); type($('tax_rate'), '7'); {SAVE_FUNCTION[kind]}();")

    payload = sent_payload(out, kind)
    assert [i['taxRate'] for i in payload['items']] == [7, 7] and payload['taxRate'] == 0.07 and 'taxMode' not in payload


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_with_the_checkbox_the_payload_carries_taxMode_and_the_rates_of_the_selects(world, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind,
                   f"tick('mixed_tax', true); choose(rows()[1], '7'); {SAVE_FUNCTION[kind]}();")

    payload = sent_payload(out, kind)
    assert payload['taxMode'] == 'mixed'
    assert [i['taxRate'] for i in payload['items']] == [19, 7]                       # Zahlen in Prozent, wie heute der Satz der Position
    assert payload['taxRate'] == 0.19 and payload['showTax'] is True                 # der Kopf bleibt, wie die Maske ihn heute schickt


@NO_NODE
def test_the_payload_with_the_checkbox_differs_from_the_one_without_only_in_taxMode_the_rates_and_the_sums(world, tmp_path):
    invoice_id = make_doc('invoice', world, ONE_RATE, mixed=False)
    html = page_of('invoice', world, invoice_id)
    plain = sent_payload(run_mask(tmp_path, html, 'invoice', 'saveInvoice();'), 'invoice')

    mixed = sent_payload(run_mask(tmp_path, html, 'invoice', "tick('mixed_tax', true); choose(rows()[1], '7'); saveInvoice();"), 'invoice')

    assert mixed.pop('taxMode') == 'mixed'
    assert (mixed.pop('taxAmount'), mixed.pop('grossAmount')) == (22.5, 172.5) and (plain.pop('taxAmount'), plain.pop('grossAmount')) == (28.5, 178.5)
    assert [i.pop('taxRate') for i in mixed['items']] == [19, 7] and [i.pop('taxRate') for i in plain['items']] == [19, 19]
    assert mixed == plain                                                              # sonst nichts


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_loaded_mixed_draft_sends_its_rates_back_unchanged(world, tmp_path, kind):
    doc_id = make_doc(kind, world, TWO_RATES + [item(3, 'Heft', 5.0, 7)])

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, f'{SAVE_FUNCTION[kind]}();')

    payload = sent_payload(out, kind)
    assert payload['taxMode'] == 'mixed' and [i['taxRate'] for i in payload['items']] == [19, 7, 7]


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_row_without_a_readable_rate_sends_null_so_the_server_asks(world, tmp_path, kind):
    doc_id = make_doc(kind, world, TWO_RATES)
    set_item_rate(world, doc_id, 2, 'abc')

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, f'{SAVE_FUNCTION[kind]}();')

    assert [i['taxRate'] for i in sent_payload(out, kind)['items']] == [19, None]


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_summary_shows_one_line_per_rate_with_the_tax_of_each_group(world, tmp_path, kind):
    """100,00 € zu 19 % und 50,00 € zu 7 %: 19,00 € und 3,50 €, netto 150,00 €, brutto 172,50 €."""
    doc_id = make_doc(kind, world, TWO_RATES)

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, "emit('state', snap());")

    state = out['state']
    assert state['rateRows'] == [['Mehrwertsteuer 19 % auf 100,00 € netto:', '19,00 €'], ['Mehrwertsteuer 7 % auf 50,00 € netto:', '3,50 €']]
    assert (state['sumNet'], state['taxAmount'], state['sumGross'], state['taxBase']) == ('150,00 €', '22,50 €', '172,50 €', '150,00')
    assert state['taxRowShown'] is False and state['tableOn'] is True
    assert state['spans'] == [8, 6, 8, 6, 6, 5, 6]                                    # die festen Summenzeilen sind um die Spalte breiter, die Zeilen je Satz auch


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_tax_is_rounded_per_group_never_per_position(world, tmp_path, kind):
    """Drei Positionen zu 7 % über je 0,10 €: Die Gruppe hat 0,30 € netto und 0,021 € Steuer = 0,02 € – je Position gerundet wären es 0,03 €."""
    doc_id = make_doc(kind, world, [item(1, 'Beratung', 100.0, 19), item(2, 'Heft A', 0.10, 7), item(3, 'Heft B', 0.10, 7),
                                    item(4, 'Heft C', 0.10, 7)])

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, "emit('state', snap());")

    state = out['state']
    assert state['rateRows'] == [['Mehrwertsteuer 19 % auf 100,00 € netto:', '19,00 €'], ['Mehrwertsteuer 7 % auf 0,30 € netto:', '0,02 €']]
    assert (state['sumNet'], state['taxAmount'], state['sumGross']) == ('100,30 €', '19,02 €', '119,32 €')


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_display_agrees_with_what_the_server_stores(world, tmp_path, kind):
    """Die Anzeige ist nur die Anzeige – aber für diese Beträge sagt sie dasselbe wie der Server: die Summen im Beleg sind die Summen auf dem Bildschirm."""
    doc_id = make_doc(kind, world, [item(1, 'Beratung', 33.33, 19, quantity=3), item(2, 'Heft', 0.10, 7, quantity=3), item(3, 'Fachbuch', 12.35, 7)])
    stored = columns(world.db, doc_id)

    state = run_mask(tmp_path, page_of(kind, world, doc_id), kind, "emit('state', snap());")['state']

    def euro(minor):
        return f'{minor / 10000:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.') + ' €'
    assert (state['sumNet'], state['taxAmount'], state['sumGross']) == (euro(stored['SumNet']), euro(stored['TaxAmount']), euro(stored['SumGross']))
    assert (state['sumNet'], state['taxAmount'], state['sumGross']) == ('112,64 €', '19,89 €', '132,53 €')


@NO_NODE
def test_without_the_checkbox_the_summary_is_the_one_line_of_before(world, tmp_path):
    invoice_id = make_doc('invoice', world, ONE_RATE, mixed=False)

    state = run_mask(tmp_path, page_of('invoice', world, invoice_id), 'invoice', "emit('state', snap());")['state']

    assert state['rateRows'] == [] and state['taxRowShown'] is True and state['tableOn'] is False
    assert (state['sumNet'], state['taxAmount'], state['sumGross']) == ('150,00 €', '28,50 €', '178,50 €')
    assert state['spans'] == [7, 5, 7, 4, 5]                                         # die Tabelle hat wie vorher sieben Spalten
    assert state['box'] == {'checked': False, 'disabled': False}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_switching_the_checkbox_off_again_brings_back_the_one_tax_line_and_the_old_spans(world, tmp_path, kind):
    doc_id = make_doc(kind, world, TWO_RATES)

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, "emit('on', snap()); tick('mixed_tax', false); emit('off', snap());")

    assert out['on']['rateRows'] and out['on']['tableOn'] is True
    off = out['off']
    assert off['rateRows'] == [] and off['taxRowShown'] is True and off['tableOn'] is False and off['spans'] == [7, 5, 7, 4, 5]
    assert (off['sumNet'], off['taxAmount'], off['sumGross']) == ('150,00 €', '28,50 €', '178,50 €')      # ein Satz (der im Feld) auf alles, wie heute
    assert off['box'] == {'checked': False, 'disabled': False}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_without_tax_the_checkbox_is_off_and_locked_and_nothing_is_sent_as_mixed(world, tmp_path, kind):
    doc_id = make_doc(kind, world, TWO_RATES)
    program = (f"tick('show_tax', false); emit('state', snap()); {SAVE_FUNCTION[kind]}(); "
               "tick('show_tax', true); emit('again', snap());")

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, program)

    state = out['state']
    assert state['box'] == {'checked': False, 'disabled': True} and state['tableOn'] is False and state['rateRows'] == []
    assert (state['sumNet'], state['taxAmount'], state['sumGross']) == ('150,00 €', '0,00 €', '150,00 €')
    payload = sent_payload(out, kind)
    assert 'taxMode' not in payload and payload['showTax'] is False and [i['taxRate'] for i in payload['items']] == [0, 0]
    assert out['again']['box'] == {'checked': False, 'disabled': False} and out['again']['tableOn'] is False   # wieder wählbar, aber nicht von selbst an


@NO_NODE
def test_a_locked_mixed_invoice_shows_the_groups_and_sends_only_the_open_fields(world, tmp_path):
    invoice_id = make_doc('invoice', world, TWO_RATES)
    force_status(world.db, invoice_id, 'sent')
    program = "emit('state', snap()); $('order_number').value = 'B-99'; saveInvoice();"

    out = run_mask(tmp_path, page_of('invoice', world, invoice_id), 'invoice', program)

    state = out['state']
    assert state['box'] == {'checked': True, 'disabled': True} and state['selectsDisabled'] == [True, True]
    assert state['rateRows'] == [['Mehrwertsteuer 19 % auf 100,00 € netto:', '19,00 €'], ['Mehrwertsteuer 7 % auf 50,00 € netto:', '3,50 €']]
    assert sent_payload(out, 'invoice') == {'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'}   # keine Positionen, kein taxMode


@NO_NODE
def test_a_locked_invoice_with_one_rate_keeps_its_one_line_and_the_column_hidden(world, tmp_path):
    invoice_id = make_doc('invoice', world, ONE_RATE, mixed=False)
    force_status(world.db, invoice_id, 'sent')

    state = run_mask(tmp_path, page_of('invoice', world, invoice_id), 'invoice', "emit('state', snap());")['state']

    assert state['box'] == {'checked': False, 'disabled': True} and state['tableOn'] is False and state['rateRows'] == []
    assert (state['sumNet'], state['taxAmount'], state['sumGross']) == ('150,00 €', '28,50 €', '178,50 €')


# ── Neue Zeilen, Artikel und das Häkchen selbst ──────────────────────────────

@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_new_row_gets_the_cell_with_the_same_choices_as_the_rows_of_the_server(world, tmp_path, kind):
    out = run_mask(tmp_path, page_of(kind, world), kind, "addFreeRow(); emit('state', snap());")

    served, added = out['state']['options']
    assert added == served == [['19', '19 %'], ['7', '7 %']]
    assert out['state']['selects'] == ['19', '19']                                   # der Satz im Feld „Mehrwertsteuer“ ist die Vorgabe


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_new_cell_sits_before_the_price_and_the_row_keeps_its_other_cells(world, tmp_path, kind):
    program = ("addFreeRow(); emit('cells', rows()[1].children.filter(td => td.tag).map("
               "td => td.className || td.children.filter(c => c.tag).map(c => c.className).join('+')));")

    out = run_mask(tmp_path, page_of(kind, world), kind, program)

    assert out['cells'] == ['', 'item-quantity', 'item-unit', 'item-description', 'mixedTaxCol', 'item-price', 'item-total', 'no-pdf']


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_new_row_takes_the_rate_of_the_field_if_the_list_knows_it_else_the_first(world, tmp_path, extended_rates, kind):
    program = ("var got = []; ['7', '5.5', '16', '0.07', 'abc', '19'].forEach(function (text) {"
               "  type($('tax_rate'), text); addFreeRow(); got.push(rows()[rows().length - 1].querySelector('.item-tax').value); });"
               "emit('got', got);")

    out = run_mask(tmp_path, page_of(kind, world), kind, program)

    assert out['got'] == ['7', '5.5', '19', '7', '19', '19']                         # 16 und „abc“ kennt die Liste nicht: der erste Satz


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_an_article_row_takes_the_rate_of_the_article_if_the_list_knows_it_else_it_acts_like_a_new_row(world, tmp_path, extended_rates, kind):
    for name, rate in (('Fachbuch', 7), ('Kurios', 5.5), ('Sechzehn', 16), ('Bruch', 0.07), ('Text', 'abc')):
        world.db.insert_article(name, unit_price=10, tax_rate=rate)
    html = page_of(kind, world)
    by_name = {a['name']: key for key, a in data_block(Dom(html), 'articlesData').items()}
    program = ("var got = []; type($('tax_rate'), '7'); " + ''.join(
        f"addArticleRow('{by_name[name]}'); got.push(rows()[rows().length - 1].querySelector('.item-tax').value);"
        for name in ('Fachbuch', 'Kurios', 'Sechzehn', 'Bruch', 'Text')) + "emit('got', got);")

    out = run_mask(tmp_path, html, kind, program)

    assert out['got'] == ['7', '5.5', '7', '7', '7']                                 # was die Liste nicht kennt, nimmt den Satz des Felds (7)


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_choices_of_a_new_row_come_from_the_list_of_the_server(world, tmp_path, extended_rates, kind):
    out = run_mask(tmp_path, page_of(kind, world), kind, "addFreeRow(); emit('state', snap());")

    assert out['state']['options'] == [[['19', '19 %'], ['7', '7 %'], ['5.5', '5,5 %']]] * 2


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_choosing_a_rate_updates_the_summary_and_a_removed_row_leaves_its_group(world, tmp_path, kind):
    doc_id = make_doc(kind, world, TWO_RATES)
    program = ("choose(rows()[1], '19'); emit('both19', snap());"
               "choose(rows()[1], '7'); removeRow(rows()[1].querySelector('button')); emit('removed', snap());")

    out = run_mask(tmp_path, page_of(kind, world, doc_id), kind, program)

    assert out['both19']['rateRows'] == [['Mehrwertsteuer 19 % auf 150,00 € netto:', '28,50 €']] and out['both19']['sumGross'] == '178,50 €'
    assert out['removed']['rateRows'] == [['Mehrwertsteuer 19 % auf 100,00 € netto:', '19,00 €']] and out['removed']['sumGross'] == '119,00 €'


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_switching_on_gives_every_row_the_user_did_not_choose_the_default_and_keeps_the_chosen(world, tmp_path, extended_rates, kind):
    program = ("addFreeRow(); addFreeRow(); choose(rows()[1], '5.5');"                 # die mittlere Zeile hat der Nutzer selbst gewählt
               "type($('tax_rate'), '7'); tick('mixed_tax', true); emit('on', snap());"
               "tick('mixed_tax', false); type($('tax_rate'), '19'); tick('mixed_tax', true); emit('again', snap());")

    out = run_mask(tmp_path, page_of(kind, world), kind, program)

    assert out['on']['selects'] == ['7', '5.5', '7']                                 # die anderen beiden folgen dem Feld „Mehrwertsteuer“ (7)
    assert out['again']['selects'] == ['19', '5.5', '19']                            # beim erneuten Einschalten wieder, die gewählte bleibt


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_loaded_mixed_draft_keeps_its_rates_when_the_checkbox_is_switched_off_and_on_again(world, tmp_path, kind):
    doc_id = make_doc(kind, world, TWO_RATES)
    program = "tick('mixed_tax', false); type($('tax_rate'), '7'); tick('mixed_tax', true); emit('state', snap());"

    state = run_mask(tmp_path, page_of(kind, world, doc_id), kind, program)['state']

    assert state['selects'] == ['19', '7']                                           # nichts wird still umgedeutet


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_loaded_document_with_one_rate_follows_the_rate_field_when_switched_on(world, tmp_path, kind):
    doc_id = make_doc(kind, world, ONE_RATE, mixed=False)

    state = run_mask(tmp_path, page_of(kind, world, doc_id), kind,
                     "type($('tax_rate'), '7'); tick('mixed_tax', true); emit('state', snap());")['state']

    assert state['selects'] == ['7', '7'] and state['rateRows'] == [['Mehrwertsteuer 7 % auf 150,00 € netto:', '10,50 €']]


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_rate_outside_the_list_stays_chosen_and_is_sent_so_the_server_says_no_and_the_mask_shows_it(world, tmp_path, kind):
    """Die Maske deutet nichts um: Der Satz bleibt gewählt und geht mit – der Server lehnt ab (``Position n: …``), und die Maske zeigt den Satz."""
    doc_id = make_doc(kind, world, TWO_RATES)
    set_item_rate(world, doc_id, 2, 5.5)
    html = page_of(kind, world, doc_id)
    sent = sent_payload(run_mask(tmp_path, html, kind, f'{SAVE_FUNCTION[kind]}();'), kind)
    assert sent['taxMode'] == 'mixed' and [i['taxRate'] for i in sent['items']] == [19, 5.5]

    save_handler = handlers.handle_invoice_save if kind == 'invoice' else handlers.handle_quote_save
    answer = json.loads(save_handler(json.dumps(sent).encode()))
    out = run_mask(tmp_path, html, kind, f'{SAVE_FUNCTION[kind]}();', answer=answer, real_send=True)

    assert answer['success'] is False
    assert answer['error'] == 'Position 2: Der Steuersatz 5,5 % ist in einem Beleg mit Steuersatz je Position nicht wählbar.'
    assert out['messages'] == [['Fehler beim Speichern: ' + answer['error'], 'error']]


@NO_NODE
def test_the_rounding_of_the_display_is_the_one_of_the_server(world, tmp_path):
    """``mixTax`` gegen ``money.tax_from_net`` und ``round_minor``: viele Nettosummen (auch negative) und Sätze, auf den Cent gleich."""
    nets = list(range(-150, 600)) + [999, 1000, 1001, 4999, 5000, 5001, 99999, 123456, 1234567, 99999999, 123456789, 99999999999]
    rates = ['19', '7', '5.5', '0', '16', '5', '10.5', '2.25', '100']
    program = (f"var nets = {json.dumps(nets)}, rates = {json.dumps(rates)}, got = {{}};"
               "rates.forEach(function (r) { got[r] = nets.map(function (n) { return mixTax(n, parseFloat(r)); }); }); emit('got', got);")

    out = run_mask(tmp_path, page_of('invoice', world), 'invoice', program)

    for rate in rates:
        expected = [round_minor(tax_from_net(net * 100, Decimal(rate)), 2) // 100 for net in nets]
        assert out['got'][rate] == expected, rate


# ── 3. Statisch: kein neues innerHTML mit Daten, nur var und function ────────

# Die bekannten Stellen mit ``innerHTML`` in den Skripten der Masken: feste Vorlagen ohne Daten (``${rowCounter}`` ist eine Zahl), eine leere Anzeige,
# die ``<strong>``-Zeile des Gesamtbetrags (eine formatierte Zahl) und – beim Angebot – das Lesen der Textfelder. Kommt eine hinzu, ist zu klären, ob sie
# Daten in Markup setzt.
KNOWN_INNER_HTML = {
    'invoice': ["addressDisplay.innerHTML = '';", 'newRow.innerHTML = `', 'newRow.innerHTML = `',
                "document.getElementById('sum_gross').innerHTML = '<strong>' + fmtDe(sumGross) + ' €</strong>';"],
    'quote': ["if (!id) { disp.innerHTML=''; num.value=''; return; }", 'r.innerHTML = `', 'r.innerHTML = `',
              "document.getElementById('sum_gross').innerHTML = '<strong>' + fmtDe(net+tax) + ' €</strong>';",
              "introText: document.getElementById('intro_text_editor').innerHTML || null,",
              "closingText: document.getElementById('closing_text_editor').innerHTML || null,"],
}


@pytest.mark.parametrize('kind', KINDS)
def test_the_masks_set_no_new_markup_the_known_templates_carry_no_data(world, kind):
    _dom, main, mix = page_script(page_of(kind, world), kind)

    lines = [line.strip() for line in main.splitlines() if 'innerHTML' in line]
    assert lines == KNOWN_INNER_HTML[kind]
    for name in ('addFreeRow', 'addArticleRow'):
        template = re.search(r'innerHTML = `(.*?)`', extract_function(main, name), re.S).group(1)
        assert re.findall(r'\$\{[^}]*\}', template) == ['${rowCounter}']                 # nur die laufende Nummer, keine Daten
    for name in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'Function('):
        assert name not in mix, name


def test_the_new_script_uses_only_var_and_function_and_sets_values_only_as_values_or_text():
    code = re.sub(r'//[^\n]*', '', MIXED_TAX_JS)
    assert not re.search(r'(?<![A-Za-z0-9_$.])(const|let)\s', code)
    assert '=>' not in code and '`' not in code and '${' not in code
    assert 'textContent' in code and 'createElement' in code and '.value' in code
    assert '</script' not in MIXED_TAX_JS.lower()
    assert set(re.findall(r'function (\w+)\(', code)) == {
        'mixOn', 'mixKey', 'mixKnown', 'mixDefaultRate', 'mixArticleRate', 'mixAddCell', 'mixAttach', 'mixToggle', 'mixSync', 'mixRoundDiv',
        'mixTax', 'mixLabel', 'mixGroups', 'mixRateRow', 'mixTotals', 'mixRowRate', 'mixInit'}          # alle mit dem Präfix mix


@pytest.mark.parametrize('kind', KINDS)
def test_the_two_masks_share_one_script_block_and_do_not_copy_it(world, kind):
    html = page_of(kind, world)

    blocks = [s for s in Dom(html).scripts if 'function mixOn(' in s]
    assert len(blocks) == 1
    assert blocks[0].strip() == mixed_tax_script().replace('<script>', '').replace('</script>', '').strip()
    assert html.index('function mixOn(') < html.index(f'function {SAVE_FUNCTION[kind]}(')       # er steht vor dem Skript der Seite


# ── 4. Rundlauf über HTTP ────────────────────────────────────────────────────

@pytest.fixture
def books(auto_login):
    w = build_world(auto_login.db)
    w.csrf = auto_login.csrf
    return w


def stored_state(db, doc_id):
    """Alles, was ein Speichern der Maske am Beleg schreiben kann: die Zeile ohne ihren Zeitstempel und die Positionen ohne ihre IDs."""
    head = {k: v for k, v in columns(db, doc_id).items() if k != 'UpdatedAt'}
    items = query_rows(db, 'SELECT Position, Description, Quantity, Unit, PricePerUnit, TotalNet, TaxCategory, TaxRate, typeof(TaxRate) AS TaxType '
                           'FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', doc_id)
    return head, items


def over_http(server_port, books, kind, doc_items, mixed, **over):
    """Einen Beleg über den Server anlegen (wie die Maske); liefert seine ID."""
    mode = {'taxMode': 'mixed'} if mixed else {}
    if kind == 'invoice':
        answer = json.loads(post_json(server_port, '/invoice/save', make_payload(books, items=doc_items, **mode, **over), books.csrf).text)
        return answer['invoice_id']
    answer = json.loads(post_json(server_port, '/quote/save', quote_payload(books, items=doc_items, **mode, **over), books.csrf).text)
    return answer['quote_id']


@NO_NODE
@pytest.mark.parametrize('mixed', [True, False])
@pytest.mark.parametrize('kind', KINDS)
def test_over_http_a_draft_survives_opening_and_saving_unchanged_with_and_without_a_rate_per_position(server_port, books, tmp_path, kind, mixed):
    """Beleg speichern, die Seite laden, in der Maske nichts ändern, speichern: dieselben Datenbankzeilen – auch die Sätze der Positionen."""
    doc_id = over_http(server_port, books, kind, TWO_RATES if mixed else ONE_RATE, mixed)
    before = stored_state(books.db, doc_id)
    assert (before[0]['MixedTaxRates'] == 1) is mixed

    page = get(server_port, f'/{kind}?id={doc_id}')
    assert page.status == 200
    out = run_mask(tmp_path, page.text, kind, f'{SAVE_FUNCTION[kind]}();')
    payload = sent_payload(out, kind)
    assert ('taxMode' in payload) is mixed
    path = '/invoice/save' if kind == 'invoice' else '/quote/save'
    answer = post_json(server_port, path, payload, books.csrf)

    assert answer.status == 200 and json.loads(answer.text)['success'] is True, answer.text
    assert stored_state(books.db, doc_id) == before


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_over_http_the_checkbox_turns_a_draft_with_one_rate_into_a_mixed_one_and_back(server_port, books, tmp_path, kind):
    doc_id = over_http(server_port, books, kind, ONE_RATE, mixed=False)
    path = '/invoice/save' if kind == 'invoice' else '/quote/save'

    page = get(server_port, f'/{kind}?id={doc_id}').text
    payload = sent_payload(run_mask(tmp_path, page, kind, f"tick('mixed_tax', true); choose(rows()[1], '7'); {SAVE_FUNCTION[kind]}();"), kind)
    assert json.loads(post_json(server_port, path, payload, books.csrf).text)['success'] is True
    head, items = stored_state(books.db, doc_id)
    assert (head['MixedTaxRates'], head['TaxRate'], head['SumNet'], head['TaxAmount'], head['SumGross']) == (1, 0.19, 1500000, 225000, 1725000)
    assert [(i['TaxRate'], i['TaxType']) for i in items] == [(19.0, 'real'), (7.0, 'real')]

    page = get(server_port, f'/{kind}?id={doc_id}').text
    assert 'checked' in box_of(page) and rates_of(page) == ['19', '7']                # die Maske zeigt, was gespeichert ist
    payload = sent_payload(run_mask(tmp_path, page, kind, f"tick('mixed_tax', false); {SAVE_FUNCTION[kind]}();"), kind)
    assert 'taxMode' not in payload and [i['taxRate'] for i in payload['items']] == [19, 19]
    assert json.loads(post_json(server_port, path, payload, books.csrf).text)['success'] is True
    head, items = stored_state(books.db, doc_id)
    assert (head['MixedTaxRates'], head['TaxRate'], head['SumNet'], head['TaxAmount'], head['SumGross']) == (None, 0.19, 1500000, 285000, 1785000)


@pytest.mark.parametrize('kind', KINDS)
def test_over_http_a_rate_outside_the_list_is_refused_with_the_sentence_and_nothing_changes(server_port, books, kind):
    doc_id = over_http(server_port, books, kind, TWO_RATES, mixed=True)
    set_item_rate(books, doc_id, 2, 5.5)
    before = stored_state(books.db, doc_id)
    page = get(server_port, f'/{kind}?id={doc_id}').text
    assert chosen(rows_of(page)[1]['select']) == '5.5'

    items = [item(1, 'Beratung', 100.0, 19), item(2, 'Fachbuch', 50.0, 5.5)]
    payload = (make_payload(books, items=items, taxMode='mixed', invoiceId=doc_id) if kind == 'invoice'
               else quote_payload(books, items=items, taxMode='mixed', quoteId=doc_id))
    answer = post_json(server_port, '/invoice/save' if kind == 'invoice' else '/quote/save', payload, books.csrf)

    assert json.loads(answer.text) == {'success': False, 'error': 'Position 2: Der Steuersatz 5,5 % ist in einem Beleg mit Steuersatz je Position nicht wählbar.'}
    assert stored_state(books.db, doc_id) == before


@NO_NODE
def test_over_http_a_mixed_quote_becomes_an_invoice_that_shows_the_rates(server_port, books, tmp_path):
    year = datetime.date.today().year
    books.db.insert_number_range('invoice', year, 'R', current_number=0)
    quote_id = over_http(server_port, books, 'quote', TWO_RATES, mixed=True, status='accepted')

    converted = post(server_port, '/quote/convert', form={'quote_id': str(quote_id)}, csrf=books.csrf, headers={'Origin': own_origin(server_port)})

    assert converted.status == 303 and converted.headers['Location'].startswith('/invoice?id=')
    page = get(server_port, converted.headers['Location'])
    assert page.status == 200
    assert 'checked' in box_of(page.text) and rates_of(page.text) == ['19', '7'] and table_classes(page.text) == ['invoice-items', 'mixedTaxOn']
    state = run_mask(tmp_path, page.text, 'invoice', "emit('state', snap());")['state']
    assert state['rateRows'] == [['Mehrwertsteuer 19 % auf 100,00 € netto:', '19,00 €'], ['Mehrwertsteuer 7 % auf 50,00 € netto:', '3,50 €']]
    assert state['sumGross'] == '172,50 €'


def test_over_http_a_quote_with_one_rate_becomes_an_invoice_without_the_checkbox(server_port, books):
    year = datetime.date.today().year
    books.db.insert_number_range('invoice', year, 'R', current_number=0)
    quote_id = over_http(server_port, books, 'quote', ONE_RATE, mixed=False, status='accepted')

    converted = post(server_port, '/quote/convert', form={'quote_id': str(quote_id)}, csrf=books.csrf, headers={'Origin': own_origin(server_port)})

    page = get(server_port, converted.headers['Location'])
    assert 'checked' not in box_of(page.text) and rates_of(page.text) == ['19', '19'] and table_classes(page.text) == ['invoice-items']
