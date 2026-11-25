# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Masken von Rechnung und Angebot machen sichtbar, wenn eine Position vom gespeicherten Artikel abweicht (``server/pages_invoice.py``: ``ARTICLE_MARK_JS``, ``buch.css``).

Der Fehlfall: Eine kopierte Rechnung trug eine Position, deren Netto-Einzelpreis versehentlich mit der Stundenzahl überschrieben war (8 statt 85,00 € je Stunde) – niemand sah es,
die Rechnung ging hinaus. Jetzt fällt so eine Position auf: das Feld, das abweicht (Einheit, Netto-Einzelpreis, Steuersatz), bekommt einen Warnton und den Tooltip „Artikel: …“, eine
Position, die zu keinem Artikel gehört, einen schmalen Rand links und ein „+“, das aus ihr einen NEUEN Artikel anlegt (``POST /articles/quick-add``,
``tests/test_article_quick_add.py``). An einer Zeile mit Artikel gibt es das „+“ nicht: Es übernähme den Tippfehler als neuen Artikel und schaltete die Warnung ab. **Es ist reine Anzeige:** Speichern, Payload, Sperre und Summen bleiben, wie sie waren (``tests/test_mixed_tax_mask.py``,
``tests/test_tax_entries_mask.py`` halten den Payload fest) – die Markierung liest die Zeilen und schreibt nur Klassen und Tooltips.

Geprüft wird in drei Schichten:

* ``artFind`` und ``artMark`` – die echten Skripte, aus der Seite geschnitten – in Node auf der DOM-Attrappe von ``tests/test_mixed_tax_mask.py``: der Artikel einer Zeile (die ID gilt, solange
  die Beschreibung noch mit dem Namen dieses Artikels beginnt, sonst Präfix mit Wortgrenze, der längste Name, derselbe Name zweimal: die kleinste ID, kurze Namen, Schreibweise und Leerraum; die Namen
  werden einmal vorbereitet, nicht je Zeile und Tastendruck), was markiert wird und was nie (der Preis ab einem halben Cent Unterschied, in ganzen Zahlen gerechnet), das Verschwinden der Markierung,
  das „+“ nur an einer Zeile ohne Artikel und sein Weg bis zur Antwort des Servers, die Ereignisse (``input``/``change`` bis zur Tabelle, anders als die Attrappe es sonst tut), die gesperrte
  Rechnung, der unveränderte Payload;
* das HTML: der Block steht in beiden Masken genau einmal, im Block der Steuersatz-Bausteine; die Seite schreibt keine Markierung selbst; ein feindlicher Artikelname bricht nichts;
* statisch: nur ``var`` und ``function``, Präfix ``art``, kein ``innerHTML``, keine Texte und keine Sätze im Quelltext, das Stylesheet kennt die Regeln.

Die Tests mit Node überspringen sich ohne Node. Alle Namen, Einheiten und Preise sind erfundene Testwerte.
"""
import inspect
import json
import pathlib
import re

import pytest

import server.handlers as handlers
from server import pages_invoice
from server.pages import script_json
from server.pages_invoice import ARTICLE_MARK_JS, ARTICLE_TEXTS, PageInvoice, mixed_tax_script
from server.pages_quote import PageQuote
from tests.document_helpers import add_contacts
from tests.dom_helpers import Dom, assert_same_structure, data_block
from tests.invoice_world import build_world, force_status
from tests.test_mixed_tax_mask import KINDS, NO_NODE, SAVE_FUNCTION, extract_function, make_doc, page_of, page_script, run_mask, sent_payload


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    return build_world(tmp_db)


def row_item(position, description, price, rate=19, unit='Std.', quantity=1):
    """Eine Position, wie die Maske sie schickt (Satz als Prozentzahl)."""
    return {'position': position, 'quantity': quantity, 'unit': unit, 'description': description, 'unitPrice': price, 'taxRate': rate}


def add_articles(world, *rows):
    """Artikel ``(Name, Einheit, Preis, Satz)`` anlegen; liefert ``{Name: ID}``."""
    for name, unit, price, rate in rows:
        world.db.insert_article(name, unit, price, rate)
    return {row[1]: row[0] for row in world.db.fetch_articles()}


CONSULTING = ('Beratung', 'Std.', 85.0, 19)
TRAINING = ('Schulung', 'Std.', 120.0, 19)
BOOK = ('Fachbuch', 'Stk.', 24.9, 7)


MARKERS = ('itemDeviates', 'itemNoArticle', 'itemCanSave', 'itemAddArticle', 'data-article-id')


def assert_no_marker_in_markup(html):
    markup = re.sub(r'<script\b.*?</script>', '', html, flags=re.S | re.I)
    for marker in MARKERS:
        assert marker not in markup, marker


# ── Der Prüfstand: Hilfen für die Programme, die in der Attrappe laufen ──────
# ``bubble`` löst ein Ereignis aus wie der Browser: erst das Ziel (Zuhörer und ``on<Ereignis>``), dann – anders als ``fire`` der Attrappe – jeder Vorfahre bis zur Wurzel (so kommt die
# Ereignis-Delegation an der Tabelle an). ``look`` fasst eine Zeile zusammen: die Felder mit ``itemDeviates`` und ihr Tooltip, die Klassen der Zeile (ohne ``invoice-item-row``, sortiert)
# und die Zahl der „+“.

JS = r"""
var asked = [], notes = [], calls = [];
appConfirm = function (text, ok) { asked.push(text); ok(); };
function appMsg(text, type) { notes.push([text, type]); }
var plainFetch = fetch;
fetch = function (url, options) { calls.push({url: url, method: options.method, headers: options.headers}); return plainFetch(url, options); };
function bubble(el, type) {
  fire(el, type);
  for (var node = el.parentNode; node; node = node.parentNode)
    ((node.listeners && node.listeners[type]) || []).forEach(function (fn) { fn.call(node, {type: type, target: el}); });
}
function typed(el, value) { el.value = value; bubble(el, 'input'); }
function ticked(id, on) { var el = $(id); el.checked = on; bubble(el, 'change'); }
function picked(row, key) { var s = row.querySelector('.item-tax'); s.value = key; bubble(s, 'change'); }
function afterAll(fn) { var p = Promise.resolve(); for (var i = 0; i < 20; i++) p = p.then(function () {}); return p.then(fn); }
var FIELDS = {unit: '.item-unit', price: '.item-price', tax: '.item-tax', quantity: '.item-quantity', description: '.item-description', first: 'td:first-child'};
function look(row) {
  var out = {deviating: {}, classes: row.className.split(/\s+/).filter(function (c) { return c && c !== 'invoice-item-row'; }).sort(),
             buttons: row.querySelectorAll('.itemAddArticle').length};
  Object.keys(FIELDS).forEach(function (name) {
    var el = row.querySelector(FIELDS[name]);
    if (el.classList.contains('itemDeviates')) out.deviating[name] = el.title;
  });
  return out;
}
function looks() { return rows().map(look); }
"""


def run(tmp_path, kind, html, program, **kwargs):
    return run_mask(tmp_path, html, kind, JS + program, **kwargs)


def looks_of(tmp_path, kind, html, program="artMark();"):
    """Die Zusammenfassung jeder Zeile (``look``), nachdem ``program`` gelaufen ist."""
    return run(tmp_path, kind, html, program + "emit('looks', looks());")['looks']


# ── 1. artFind: der Artikel einer Zeile ──────────────────────────────────────

ARTICLES_JS = r"""
articlesData = {
  '1': {id: 1, name: 'Beratung', unit: 'Std.', price: 85, taxRate: 19, mixRate: '19', description: ''},
  '2': {id: 2, name: 'Beratung Plus', unit: 'Std.', price: 95, taxRate: 19, mixRate: '19', description: ''},
  '3': {id: 3, name: 'Fachbuch', unit: 'Stk.', price: 24.9, taxRate: 7, mixRate: '7', description: ''},
  '4': {id: 4, name: 'fachbuch ', unit: 'Stk.', price: 12, taxRate: 7, mixRate: '7', description: ''},
  '5': {id: 5, name: 'AB', unit: 'Stk.', price: 1, taxRate: 19, mixRate: '19', description: ''},
  '6': {id: 6, name: 'Überprüfung', unit: 'Std.', price: 60, taxRate: 19, mixRate: '19', description: ''}
};
function find(text, id) {
  var row = document.createElement('tr');
  var input = document.createElement('input');
  input.className = 'item-description';
  input.value = text;
  row.appendChild(input);
  if (id !== undefined) row.setAttribute('data-article-id', String(id));
  var found = artFind(row);
  return found ? found.id : null;
}
"""

FIND_CASES = [
    # (Beschreibung, data-article-id, erwartete ID)
    ('Beratung', None, 1), ('Beratung Q3', None, 1), ('Beratung (Std.)', None, 1), ('Beratung-Paket', None, 1), ('Beratung, vor Ort', None, 1),
    ('Beratungspaket', None, None), ('Beratung2', None, None), ('Beratungen', None, None), ('Beratun', None, None),
    ('Beratung Plus', None, 2), ('Beratung Plus Q3', None, 2), ('Beratung Pluss', None, 1),          # der längste Name, aber nur mit Wortgrenze
    ('Fachbuch', None, 3), ('FACHBUCH Heft', None, 3), ('fachbuch ', None, 3),                       # derselbe normalisierte Name zweimal (Schreibweise und Randleerraum zählen nicht): die kleinste ID
    ('AB', None, None), ('AB Test', None, None),                                                     # ein Name unter 3 Zeichen nimmt am Namensvergleich nicht teil
    ('  beratung    PLUS  ', None, 2), ('BERATUNG\tplus', None, 2),                                  # Schreibweise und Leerraum
    ('ÜBERPRÜFUNG Haus', None, 6), ('überprüfungen', None, None),                                    # Umlaute über toLowerCase
    ('Beratung\u0301', None, None),                                                                  # ein Akzent gehört zum Buchstaben: keine Wortgrenze
    ('', None, None), ('   ', None, None), ('Fahrtkosten', None, None),
    # Die ID der Zeile gilt, solange die Beschreibung noch mit dem Namen DIESES Artikels beginnt (dieselbe Wortgrenzenregel) – auch bei kurzem Namen und auch, wenn ein längerer Name passte
    ('AB', 5, 5), ('AB Zeile', 5, 5), ('Beratung', 1, 1), ('beratung - Details', 1, 1), ('Beratung Plus Q3', 1, 1),
    # Sonst fällt die Zeile auf die Namenssuche zurück: Die Beschreibung gehört jetzt zu einem anderen Artikel, zu keinem, oder der Name steht dort nicht mehr als Wort
    ('Beratung', 3, 1), ('Fachbuch Q3', 1, 3), ('Fahrtkosten', 5, None), ('ABC', 5, None), ('Beratungspaket', 1, None), ('', 1, None),
    ('Beratung', 99, 1), ('Fahrtkosten', 99, None),                                                  # eine ID ohne Artikel gilt nicht
]


@NO_NODE
def test_artFind_gives_the_article_by_its_id_while_the_description_starts_with_its_name_else_by_the_longest_name_with_a_word_boundary(world, tmp_path):
    cases = [[text, article_id, expected] for text, article_id, expected in FIND_CASES]
    program = ARTICLES_JS + f"var cases = {json.dumps(cases)}; emit('got', cases.map(function (c) {{ return find(c[0], c[1] === null ? undefined : c[1]); }}));"

    out = run(tmp_path, 'invoice', page_of('invoice', world), program)

    assert [(text, article_id, got) for (text, article_id, _), got in zip(FIND_CASES, out['got'])] == [(text, article_id, expected) for text, article_id, expected in FIND_CASES]


@NO_NODE
def test_artFind_changes_nothing(world, tmp_path):
    program = ARTICLES_JS + r"""
      var before = JSON.stringify(articlesData);
      var row = document.createElement('tr'); var input = document.createElement('input'); input.className = 'item-description'; input.value = 'Beratung Plus Q3';
      row.appendChild(input); row.setAttribute('data-article-id', '3');
      var attributes = JSON.stringify(row.attrs); var html = row.innerHTML;
      artFind(row); row.removeAttribute('data-article-id'); artFind(row); row.setAttribute('data-article-id', '3');
      emit('same', [JSON.stringify(articlesData) === before, JSON.stringify(row.attrs) === attributes, row.innerHTML === html, input.value]);
    """

    assert run(tmp_path, 'invoice', page_of('invoice', world), program)['same'] == [True, True, True, 'Beratung Plus Q3']


@NO_NODE
def test_artFind_takes_the_smallest_id_among_articles_with_the_same_name_whatever_the_order(world, tmp_path):
    """Die Schlüssel sind hier keine Zahlen: Sie behalten die Reihenfolge des Einfügens (bei Zahlen ordnet JavaScript sie aufsteigend) – die kleinste ID gewinnt trotzdem."""
    program = r"""
      articlesData = {
        'x': {id: 9, name: 'Workshop', unit: 'Psch.', price: 500, taxRate: 19, mixRate: '19', description: ''},
        'y': {id: 3, name: 'workshop ', unit: 'Psch.', price: 450, taxRate: 19, mixRate: '19', description: ''},
        'z': {id: 7, name: 'WORKSHOP', unit: 'Psch.', price: 400, taxRate: 19, mixRate: '19', description: ''}
      };
      function find(text) { var row = document.createElement('tr'), input = document.createElement('input'); input.className = 'item-description'; input.value = text; row.appendChild(input);
                            var found = artFind(row); return found ? found.id : null; }
      emit('got', [find('Workshop'), find('workshop Q3'), find('  WORKSHOP  ')]);
    """

    assert run(tmp_path, 'invoice', page_of('invoice', world), program)['got'] == [3, 3, 3]


@NO_NODE
def test_artFind_agrees_with_a_plain_search_over_every_article_for_many_generated_texts(world, tmp_path):
    """Gegenprobe: das Ergebnis des vorbereiteten Namensverzeichnisses gegen die einfache Suche (jeden Artikel einzeln prüfen: Präfix, Wortgrenze, der längste Name, dann die kleinste ID).
    Die Namen stammen aus einem kleinen Alphabet, damit sich viele Präfixe überlappen; zwei verschiedene Namen gleicher Länge können nie beide Präfix desselben Textes sein."""
    program = r"""
      var seed = 12345;
      function random(n) { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n; }
      var alphabet = ['a', 'b', 'ab', ' ', ' ', '-', '1', 'Ä', 'é', 'A', '  '];
      function piece(length) { var out = ''; for (var i = 0; i < length; i++) out += alphabet[random(alphabet.length)]; return out; }
      articlesData = {};
      var names = [];
      for (var n = 0; n < 300; n++) { var name = piece(2 + random(7)); names.push(name); articlesData[String(1000 - n * 3)] = {id: 1000 - n * 3, name: name, unit: 'Std.', price: 1, taxRate: 19, mixRate: '19', description: ''}; }
      function plain(text) {
        var best = null, bestLength = -1;
        Object.keys(articlesData).forEach(function (key) {
          var article = articlesData[key], name = artNorm(article.name);
          if (Array.from(name).length < 3 || text.indexOf(name) !== 0 || artWordStart(text.slice(name.length))) return;
          if (name.length > bestLength || (name.length === bestLength && article.id < best.id)) { best = article; bestLength = name.length; }
        });
        return best ? best.id : null;
      }
      var checked = 0, found = 0, wrong = [];
      for (var t = 0; t < 3000; t++) {
        var text = random(3) ? names[random(names.length)] + piece(random(5)) : piece(random(12));
        var row = document.createElement('tr'), input = document.createElement('input'); input.className = 'item-description'; input.value = text; row.appendChild(input);
        var got = artFind(row), want = plain(artNorm(text));
        checked++; if (want !== null) found++;
        if ((got ? got.id : null) !== want) wrong.push([text, got ? got.id : null, want]);
      }
      emit('result', {checked: checked, found: found, wrong: wrong.slice(0, 5)});
    """

    result = run(tmp_path, 'invoice', page_of('invoice', world), program)['result']

    assert result['wrong'] == [] and result['checked'] == 3000
    assert result['found'] > 300                                                                                      # die Gegenprobe prüft auch Treffer, nicht nur „kein Artikel“


@NO_NODE
def test_the_names_are_prepared_once_and_not_per_row_and_keystroke_and_a_new_article_is_picked_up(world, tmp_path):
    """Ziel: 5000 Artikel und 200 Zeilen bleiben bei jedem Tastendruck weit unter 50 ms. Gemessen wird nicht die Zeit, sondern die Zahl der Normalisierungen (``artNorm``): einmal je Artikel
    beim Aufbau des Verzeichnisses, danach nur je Zeile – nicht je Zeile und Artikel (das wären eine Million)."""
    doc_id = make_doc('invoice', world, [row_item(n + 1, f'Freier Text {n}' if n % 2 == 0 else f'Beratung Variante {n * 10} Zusatz', 12.0) for n in range(200)], mixed=False)
    program = r"""
      var words = ['Beratung', 'Schulung', 'Wartung', 'Lizenz', 'Installation', 'Support', 'Material', 'Fahrt', 'Pauschale', 'Analyse'];
      articlesData = {};
      for (var n = 0; n < 5000; n++) articlesData[String(100000 + n)] = {id: 100000 + n, name: words[n % 10] + ' Variante ' + n, unit: 'Std.', price: 12, taxRate: 19, mixRate: '19', description: ''};
      var calls = 0, plainNorm = artNorm;
      artNorm = function (text) { calls++; return plainNorm(text); };
      artMark(); var first = calls;
      artMark(); artMark(); var again = (calls - first) / 2;
      var found = rows().filter(function (row) { return row.classList.contains('itemNoArticle'); }).length;
      emit('counts', {rows: rows().length, articles: Object.keys(articlesData).length, first: first, again: again, withoutArticle: found});
    """

    counts = run(tmp_path, 'invoice', page_of('invoice', world, doc_id), program)['counts']

    assert counts['rows'] == 200 and counts['articles'] == 5000
    assert counts['first'] <= 5000 + 3 * 200                                                                          # die Namen einmal, dazu je Zeile ein paar Aufrufe
    assert counts['again'] <= 3 * 200                                                                                 # beim nächsten Tastendruck keine Artikelname mehr
    assert 0 < counts['withoutArticle'] < 200                                                                         # die Namenssuche hat gearbeitet: ein Teil der Zeilen findet seinen Artikel


@NO_NODE
def test_a_new_article_of_the_plus_is_found_by_the_other_rows_by_name(world, tmp_path):
    """Das Verzeichnis der Namen wird neu aufgebaut, wenn ein Artikel über „+“ dazukommt (er steht dann im selben ``articlesData``-Objekt, nicht in einem neuen)."""
    doc_id = make_doc('invoice', world, [row_item(1, 'Fahrtkosten', 30.0), row_item(2, 'Fahrtkosten Q4', 30.0)], mixed=False)
    answer = {'success': True, 'article': {'id': 7, 'name': 'Fahrtkosten', 'unit': 'Std.', 'price': 30.0, 'taxRate': 19.0, 'mixRate': '19', 'description': ''}}
    program = ("artMark(); emit('before', looks()); fire(rows()[0].querySelector('.itemAddArticle'), 'click'); "
               "afterAll(function () { emit('after', looks()); });")

    out = run(tmp_path, 'invoice', page_of('invoice', world, doc_id), program, answer=answer)

    assert [look['classes'] for look in out['before']] == [['itemCanSave', 'itemNoArticle']] * 2
    assert out['after'] == [{'deviating': {}, 'classes': [], 'buttons': 1}] * 2                                       # die zweite Zeile findet den neuen Artikel über ihren Namen


# ── 2. artMark: was abweicht, wird markiert – und nur das ────────────────────

@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_owners_case_a_price_typed_into_the_wrong_field_marks_the_price_field_only_and_names_the_article_price(world, tmp_path, kind):
    """Artikel „Beratung“ 85,00 € je Stunde, die Zeile trägt 8: Nur der Einzelpreis fällt auf, der Tooltip nennt den Preis des Artikels."""
    add_articles(world, CONSULTING)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 8.0)], mixed=False)

    (look,) = looks_of(tmp_path, kind, page_of(kind, world, doc_id))

    assert look == {'deviating': {'price': 'Artikel: 85,00'}, 'classes': [], 'buttons': 1}                           # kein itemCanSave: An dieser Zeile steht kein „+“


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_row_that_matches_its_article_is_marked_nowhere_and_offers_no_plus(world, tmp_path, kind):
    add_articles(world, CONSULTING, TRAINING, BOOK)
    plain = make_doc(kind, world, [row_item(1, 'Beratung', 85.0), row_item(2, 'Schulung Projekt Alpha', 120.0)], mixed=False)
    mixed = make_doc(kind, world, [row_item(1, 'Beratung', 85.0), row_item(2, 'Fachbuch', 24.9, 7, 'Stk.')], mixed=True, number='26R101' if kind == 'invoice' else '26A101')

    for doc_id in (plain, mixed):
        looks = looks_of(tmp_path, kind, page_of(kind, world, doc_id))
        assert looks == [{'deviating': {}, 'classes': [], 'buttons': 1}] * 2, doc_id


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_each_field_is_compared_on_its_own_unit_price_and_the_rate(world, tmp_path, kind):
    add_articles(world, CONSULTING)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 85.0, 19, 'Stk.')], mixed=False)
    html = page_of(kind, world, doc_id)

    programs = {
        'unit': "artMark();",                                                                                      # Stk. statt Std.
        'price': "typed(rows()[0].querySelector('.item-unit'), 'std.'); typed(rows()[0].querySelector('.item-price'), '84.99');",
        'none': "typed(rows()[0].querySelector('.item-unit'), '  STD. ');",                                        # Schreibweise und Randleerraum zählen nicht
    }
    got = {name: looks_of(tmp_path, kind, html, program)[0] for name, program in programs.items()}

    assert got['unit'] == {'deviating': {'unit': 'Artikel: Std.'}, 'classes': [], 'buttons': 1}
    assert got['price'] == {'deviating': {'price': 'Artikel: 85,00'}, 'classes': [], 'buttons': 1}
    assert got['none'] == {'deviating': {}, 'classes': [], 'buttons': 1}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_price_deviates_from_half_a_cent_on_and_the_title_shows_two_decimals_with_a_comma(world, tmp_path, kind):
    add_articles(world, ('Fachbuch', 'Stk.', 1234.5, 19))
    doc_id = make_doc(kind, world, [row_item(1, 'Fachbuch', 1234.5, 19, 'Stk.')], mixed=False)
    html = page_of(kind, world, doc_id)
    prices = ('1234.5', '1234.50', '1234.504', '1234.496', '1234.505', '1234.495', '1234.51', '1234.49', '0', '')

    got = {price: looks_of(tmp_path, kind, html, f"typed(rows()[0].querySelector('.item-price'), '{price}');")[0]['deviating'] for price in prices}

    for price in ('1234.5', '1234.50', '1234.504', '1234.496'):                                                       # weniger als ein halber Cent Unterschied: keine Abweichung
        assert got[price] == {}, price
    for price in ('1234.505', '1234.495', '1234.51', '1234.49', '0', ''):                                             # ab einem halben Cent; leer rechnet die Zeile als 0
        assert got[price] == {'price': 'Artikel: 1.234,50'}, price                                                    # Tausenderpunkt, Komma, zwei Stellen


PRICE_CASES = [
    # (Feld der Zeile, Preis des Artikels, weicht ab?) – der Unterschied ist EXAKT mindestens 0,005, in Dezimalzahlen gerechnet
    ('85', 85, False), ('85.0', 85, False), ('85.004', 85, False), ('85.0049999', 85, False), ('84.996', 85, False),
    ('85.005', 85, True), ('84.995', 85, True), ('85.01', 85, True), ('84.99', 85, True), ('', 85, True), ('0', 85, True), ('abc', 85, True),
    ('8.5e1', 85, False), ('8.5001e1', 85, False), ('8.5005e1', 85, True), ('850e-1', 85, False),                      # Schreibweise mit Exponent
    ('85.005', 85.01, True), ('85.0051', 85.01, False), ('85.015', 85.01, True), ('85.01', 85.01, False),                # 85,01 − 85,005 ist als Gleitkomma kleiner als 0,005
    ('0.28', 0.285, True), ('0.2849', 0.285, False), ('0.29', 0.285, True), ('0.285', 0.285, False),                    # 0,285 − 0,28 ebenso
    ('1.005', 1.01, True), ('1.0051', 1.01, False), ('1.01', 1.01, False),
    ('', 0, False), ('0', 0, False), ('0.004', 0, False), ('0.005', 0, True), ('-0.005', 0, True), ('-0.004', 0, False),
    ('0.0079', 0.0079, False), ('0.0129', 0.0079, True), ('0.0128', 0.0079, False),                                       # ein Artikelpreis mit vier Stellen
    ('1e-7', 0, False), ('1e3', 1000, False), ('1e3', 1000.01, True),
    ('999999999.995', 999999999.99, True), ('999999999.994', 999999999.99, False),
]


@NO_NODE
def test_the_price_is_compared_exactly_in_decimals_deviating_from_half_a_cent_on_without_the_floating_point_trap(world, tmp_path):
    """„Abweichend genau dann, wenn |Zeile − Artikel| ≥ 0,005“ – mit Dezimalzahlen gerechnet: Der Text des Felds wird Ziffer für Ziffer gelesen (``85.005`` gegen 85,01: als Gleitkomma ist die
    Differenz 0,00499…, in Dezimalzahlen genau 0,005). Ein leeres oder ungültiges Feld gilt als 0, wie die Summenrechnung der Maske es liest."""
    program = f"var cases = {json.dumps(PRICE_CASES)}; emit('got', cases.map(function (c) {{ return artPriceDiffers(c[0], c[1]); }}));"

    out = run(tmp_path, 'invoice', page_of('invoice', world), program)

    assert [(row, article, got) for (row, article, _), got in zip(PRICE_CASES, out['got'])] == [(row, article, expected) for row, article, expected in PRICE_CASES]


ROUND_CASES = [
    # (Feld der Zeile, der Preis, den das „+“ zeigt und schickt) – auf zwei Stellen, ab der Hälfte aufgerundet, EINMAL und exakt
    ('85', 85), ('85.5', 85.5), ('1234.5', 1234.5), ('0.1', 0.1), ('0.29', 0.29),
    ('1.005', 1.01), ('1.00495', 1), ('12.3449999', 12.34), ('2.675', 2.68), ('0.285', 0.29), ('0.125', 0.13), ('0.124', 0.12), ('2.34495', 2.34), ('0.00495', 0), ('0.005', 0.01),
    ('', 0), ('abc', 0), ('0', 0), ('1e2', 100), ('1.5e-2', 0.02), ('1.4e-2', 0.01), ('8.5005e1', 85.01), ('-1.005', -1.01), ('999999999.995', 1000000000), ('0.0079', 0.01),
]


@NO_NODE
def test_the_price_for_the_plus_is_rounded_once_to_two_decimals_and_exactly(world, tmp_path):
    program = f"var cases = {json.dumps(ROUND_CASES)}; emit('got', cases.map(function (c) {{ return artPrice(c[0]); }}));"

    out = run(tmp_path, 'invoice', page_of('invoice', world), program)

    assert [(field, got) for (field, _), got in zip(ROUND_CASES, out['got'])] == [(field, price) for field, price in ROUND_CASES]


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_plus_shows_and_sends_the_same_rounded_price(world, tmp_path, kind):
    """Die Rückfrage und die Anfrage nehmen EINE Funktion (``artPrice``): Was die Rückfrage zeigt, geht ab – nicht der Wert des Felds."""
    doc_id = make_doc(kind, world, [row_item(1, 'Messgerät Miete', 30.0)], mixed=False)
    html = page_of(kind, world, doc_id)

    got = {}
    for field in ('1.005', '12.3449999', '1.00495', '0.125', '85', ''):
        program = f"artMark(); typed(rows()[0].querySelector('.item-price'), '{field}'); fire(rows()[0].querySelector('.itemAddArticle'), 'click'); emit('asked', asked);"
        out = run(tmp_path, kind, html, program, answer={'success': False, 'error': 'x'})
        got[field] = (out['asked'][0].split(' · ')[2], out['sent'][0]['body']['price'])

    assert got == {'1.005': ('1,01 €', 1.01), '12.3449999': ('12,34 €', 12.34), '1.00495': ('1,00 €', 1), '0.125': ('0,13 €', 0.13), '85': ('85,00 €', 85), '': ('0,00 €', 0)}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_quantity_and_description_are_never_marked(world, tmp_path, kind):
    add_articles(world, BOOK)
    doc_id = make_doc(kind, world, [row_item(1, 'Fachbuch – Projekt Alpha, Teil 2', 8.0, 19, 'Std.', quantity=99)], mixed=False)

    (look,) = looks_of(tmp_path, kind, page_of(kind, world, doc_id))

    assert set(look['deviating']) == {'unit', 'price', 'first'}                                                      # abweichend: Einheit, Preis, Satz (Kopf) – Menge und Beschreibung nie


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_with_a_rate_per_position_the_select_of_the_row_is_compared_with_the_rate_of_the_article_and_titled_with_the_option(world, tmp_path, kind):
    add_articles(world, CONSULTING)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 85.0, 19), row_item(2, 'Beratung', 85.0, 7)], mixed=True)

    first, second = looks_of(tmp_path, kind, page_of(kind, world, doc_id))

    assert first == {'deviating': {}, 'classes': [], 'buttons': 1}
    assert second == {'deviating': {'tax': 'Artikel: 19 %'}, 'classes': [], 'buttons': 1}                          # das Auswahlfeld, nicht die Positionsnummer


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_without_the_checkbox_the_rate_of_the_document_is_compared_and_the_position_number_cell_is_marked(world, tmp_path, kind):
    add_articles(world, BOOK)
    doc_id = make_doc(kind, world, [row_item(1, 'Fachbuch', 24.9, 19, 'Stk.')], mixed=False)

    (look,) = looks_of(tmp_path, kind, page_of(kind, world, doc_id))

    assert look == {'deviating': {'first': 'Artikel: 7 % – Beleg: 19 %'}, 'classes': [], 'buttons': 1}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_rate_follows_the_checkbox_the_head_mark_moves_to_the_select_and_back(world, tmp_path, kind):
    add_articles(world, BOOK)
    doc_id = make_doc(kind, world, [row_item(1, 'Fachbuch', 24.9, 19, 'Stk.')], mixed=False)
    program = ("artMark(); emit('off', looks()); ticked('mixed_tax', true); emit('on', looks()); "
               "picked(rows()[0], '7'); emit('fixed', looks()); ticked('mixed_tax', false); emit('back', looks());")

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program)

    assert out['off'][0]['deviating'] == {'first': 'Artikel: 7 % – Beleg: 19 %'}
    assert out['on'][0]['deviating'] == {'tax': 'Artikel: 7 %'}                                                      # mit dem Häkchen zählt die Auswahl der Zeile
    assert out['fixed'][0] == {'deviating': {}, 'classes': [], 'buttons': 1}
    assert out['back'][0]['deviating'] == {'first': 'Artikel: 7 % – Beleg: 19 %'}                                    # ohne Häkchen wieder der Satz des Belegs (das Auswahlfeld liegt versteckt)


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_without_a_tax_display_there_is_no_tax_mark_at_all(world, tmp_path, kind):
    """§ 19: Ohne Steuerausweis gibt es keinen Steuersatz, der abweichen könnte – weder im Kopf noch in der Zeile."""
    add_articles(world, BOOK)
    doc_id = make_doc(kind, world, [row_item(1, 'Fachbuch', 24.9, 0, 'Stk.')], mixed=False, showTax=False)
    program = ("artMark(); emit('start', looks()); ticked('show_tax', true); typed($('tax_rate'), '19'); emit('shown', looks()); "
               "ticked('show_tax', false); emit('hidden', looks());")

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program)

    assert out['start'][0]['deviating'] == {} and out['hidden'][0]['deviating'] == {}
    assert out['shown'][0]['deviating'] == {'first': 'Artikel: 7 % – Beleg: 19 %'}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_an_article_whose_rate_cannot_be_read_marks_no_rate(world, tmp_path, kind):
    add_articles(world, ('Fachbuch', 'Stk.', 24.9, 'abc'))
    doc_id = make_doc(kind, world, [row_item(1, 'Fachbuch', 24.9, 19, 'Stk.')], mixed=False)

    (look,) = looks_of(tmp_path, kind, page_of(kind, world, doc_id), "artMark(); ticked('mixed_tax', true); artMark();")

    assert look == {'deviating': {}, 'classes': [], 'buttons': 1}                                                    # nichts wird behauptet, was sich nicht lesen lässt


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_when_the_deviation_goes_away_class_and_title_go_with_it_and_the_select_gets_its_own_title_back(world, tmp_path, kind):
    """Das Auswahlfeld des Satzes trägt heute schon einen Tooltip (``mixTotals``: die Bezeichnung der Option) – er darf nicht verloren gehen; die übrigen Felder haben keinen."""
    add_articles(world, CONSULTING, BOOK)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 85.0, 19, 'Stk.'), row_item(2, 'Fachbuch', 24.9, 7, 'Stk.')], mixed=True)
    program = r"""
      var row = rows()[0], select = row.querySelector('.item-tax'), unit = row.querySelector('.item-unit'), price = row.querySelector('.item-price');
      var baseTitle = select.title;
      select.value = '7';                                                               // wie ein Nutzer, aber ohne Ereignis: artMark liest nur
      artMark(); emit('marked', [look(row), select.title, unit.title || '']);
      select.value = '19'; unit.value = 'Std.';                                         // ohne calculateTotals: Hier stellt artMark den Tooltip selbst wieder her
      artMark(); emit('fixed', [look(row), select.title, unit.title || '', price.title || '', baseTitle]);
    """

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program)

    marked, fixed = out['marked'], out['fixed']
    assert marked[0] == {'deviating': {'tax': 'Artikel: 19 %', 'unit': 'Artikel: Std.'}, 'classes': [], 'buttons': 1}
    assert marked[1:] == ['Artikel: 19 %', 'Artikel: Std.']
    assert fixed[0] == {'deviating': {}, 'classes': [], 'buttons': 1}
    assert fixed[1:4] == ['19 %', '', ''] and fixed[4] == '19 %'                                                     # die Bezeichnung der gewählten Option, sonst kein Tooltip


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_marking_twice_gives_the_same_state(world, tmp_path, kind):
    add_articles(world, CONSULTING)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 8.0), row_item(2, 'Fahrtkosten', 30.0)], mixed=False)

    out = run(tmp_path, kind, page_of(kind, world, doc_id), "artMark(); emit('once', looks()); artMark(); artMark(); emit('thrice', looks());")

    assert out['once'] == out['thrice']
    assert all(look['buttons'] == 1 for look in out['thrice'])


# ── 3. Eine Zeile ohne Artikel ───────────────────────────────────────────────

@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_row_without_an_article_gets_the_left_border_class_and_the_plus_but_no_field_mark(world, tmp_path, kind):
    add_articles(world, CONSULTING)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 85.0), row_item(2, 'Fahrtkosten', 30.0, 19, 'km')], mixed=False)

    first, second = looks_of(tmp_path, kind, page_of(kind, world, doc_id))

    assert first == {'deviating': {}, 'classes': [], 'buttons': 1}
    assert second == {'deviating': {}, 'classes': ['itemCanSave', 'itemNoArticle'], 'buttons': 1}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_plus_is_offered_only_to_a_row_without_an_article_never_to_one_that_deviates_from_its_article(world, tmp_path, kind):
    """Eine Zeile mit Artikel, die abweicht, bekommt kein „+“: Es übernähme den Tippfehler als neuen Artikel und schaltete die Warnung ab. Sichtbar ist es nur mit ``itemCanSave`` (``buch.css``)."""
    add_articles(world, CONSULTING, BOOK)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 8.0, 19, 'Stk.'), row_item(2, 'Fachbuch', 24.9, 19, 'Stk.'), row_item(3, 'Fahrtkosten', 30.0)], mixed=False)

    deviating, rate_only, without = looks_of(tmp_path, kind, page_of(kind, world, doc_id))

    assert set(deviating['deviating']) == {'unit', 'price'} and 'itemCanSave' not in deviating['classes'] and deviating['classes'] == []
    assert set(rate_only['deviating']) == {'first'} and rate_only['classes'] == []
    assert without == {'deviating': {}, 'classes': ['itemCanSave', 'itemNoArticle'], 'buttons': 1}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_plus_does_nothing_on_a_row_with_an_article_even_if_it_is_clicked(world, tmp_path, kind):
    """Auch wenn das Stylesheet fehlt und das „+“ überall steht: An einer Zeile mit Artikel fragt es nicht und schickt nichts."""
    add_articles(world, CONSULTING)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 8.0)], mixed=False)
    program = "artMark(); fire(rows()[0].querySelector('.itemAddArticle'), 'click'); emit('asked', asked); emit('looks', looks());"

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program)

    assert out['asked'] == [] and out['sent'] == [] and out['messages'] == []
    assert out['looks'][0]['deviating'] == {'price': 'Artikel: 85,00'}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_row_inserted_from_the_search_keeps_its_article_only_while_the_description_still_starts_with_its_name(world, tmp_path, kind):
    """Die ID einer Zeile gilt nur, solange die Beschreibung noch mit dem Namen dieses Artikels beginnt: Wird sie auf einen anderen Artikel umgeschrieben, vergleicht die Zeile mit diesem –
    sonst stünde die Zeile weiter für den alten Artikel (Preis und Einheit passten ja noch) und die Abweichung bliebe unbemerkt."""
    ids = add_articles(world, CONSULTING, BOOK)
    program = f"""
      addArticleRow('{ids['Beratung']}'); var row = rows()[1], description = row.querySelector('.item-description');
      emit('inserted', look(row));
      typed(description, 'Fachbuch Q4'); emit('other', look(row));
      typed(description, 'Fahrtkosten'); emit('none', look(row));
      typed(description, 'Beratung Q4'); emit('back', look(row));
      emit('id', row.getAttribute('data-article-id'));
    """

    out = run(tmp_path, kind, page_of(kind, world), program)

    assert out['inserted'] == {'deviating': {}, 'classes': [], 'buttons': 1}
    assert out['other'] == {'deviating': {'unit': 'Artikel: Stk.', 'price': 'Artikel: 24,90', 'first': 'Artikel: 7 % – Beleg: 19 %'}, 'classes': [], 'buttons': 1}
    assert out['none'] == {'deviating': {}, 'classes': ['itemCanSave', 'itemNoArticle'], 'buttons': 1}
    assert out['back'] == out['inserted'] and out['id'] == str(ids['Beratung'])                                       # die ID blieb an der Zeile; sie gilt wieder, sobald der Name wieder dort steht


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_an_empty_description_marks_nothing_even_with_an_article_id_and_the_price_of_nothing(world, tmp_path, kind):
    add_articles(world, CONSULTING)
    program = ("artMark(); emit('fresh', looks()); "
               "var row = rows()[0]; row.setAttribute('data-article-id', '1'); typed(row.querySelector('.item-price'), '8'); typed(row.querySelector('.item-description'), '   '); "
               "emit('blank', looks());")

    out = run(tmp_path, kind, page_of(kind, world), program)

    assert out['fresh'] == [{'deviating': {}, 'classes': [], 'buttons': 1}]
    assert out['blank'] == [{'deviating': {}, 'classes': [], 'buttons': 1}]


@NO_NODE
def test_without_any_article_every_named_row_is_a_row_without_an_article(world, tmp_path):
    """Bei leerem Artikelstamm gibt es zu keiner Zeile einen Artikel: Jede Zeile mit Beschreibung trägt den Rand (das „+“ legt den ersten Artikel an)."""
    doc_id = make_doc('invoice', world, [row_item(1, 'Beratung', 85.0), row_item(2, 'Fahrtkosten', 30.0)], mixed=False)

    first, second = looks_of(tmp_path, 'invoice', page_of('invoice', world, doc_id))

    assert first['classes'] == second['classes'] == ['itemCanSave', 'itemNoArticle']


# ── 4. Das „+“ ───────────────────────────────────────────────────────────────

@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_every_row_gets_one_plus_in_its_last_cell_behind_the_cross_and_marking_again_adds_none(world, tmp_path, kind):
    add_articles(world, CONSULTING)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 85.0), row_item(2, 'Fahrtkosten', 30.0)], mixed=False)
    program = r"""
      artMark(); artMark();
      emit('cells', rows().map(function (row) {
        var cell = row.querySelector('td.no-pdf'), buttons = cell.querySelectorAll('button'), plus = row.querySelector('.itemAddArticle');
        return {count: row.querySelectorAll('.itemAddArticle').length, inLastCell: plus.parentNode === cell, buttons: buttons.length,
                firstIsCross: (buttons[0].getAttribute('onclick') || '').indexOf('removeRow') === 0, plus: [plus.type, plus.title, plus.textContent, plus.className]};
      }));
    """

    cells = run(tmp_path, kind, page_of(kind, world, doc_id), program)['cells']

    assert cells == [{'count': 1, 'inLastCell': True, 'buttons': 2, 'firstIsCross': True,
                      'plus': ['button', 'Als neuen Artikel speichern', '+', 'itemAddArticle']}] * 2


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_new_and_removed_rows_are_marked_by_the_functions_that_add_and_remove_them(world, tmp_path, kind):
    ids = add_articles(world, CONSULTING, BOOK)
    program = (f"artMark(); addFreeRow(); typed(rows()[1].querySelector('.item-description'), 'Fahrtkosten'); emit('free', looks()); "
               f"addArticleRow('{ids['Fachbuch']}'); emit('article', looks()); "
               "removeRow(rows()[1].querySelector('button')); emit('removed', looks());")

    out = run(tmp_path, kind, page_of(kind, world), program)

    assert [look['buttons'] for look in out['free']] == [1, 1]                                                       # die neue Zeile bekommt ihr „+“ (addFreeRow ruft artMark)
    assert out['free'][1]['classes'] == ['itemCanSave', 'itemNoArticle']
    # eine Zeile aus dem Artikelfeld trägt die ID: Der Artikel ist bekannt, nur der Satz des Belegs (19 %) weicht vom Artikel (7 %) ab
    assert out['article'][2] == {'deviating': {'first': 'Artikel: 7 % – Beleg: 19 %'}, 'classes': [], 'buttons': 1}
    assert [look['buttons'] for look in out['removed']] == [1, 1] and out['removed'][1]['deviating'] == {'first': 'Artikel: 7 % – Beleg: 19 %'}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_plus_asks_first_and_then_sends_the_row_as_a_new_article_with_a_post(world, tmp_path, kind):
    doc_id = make_doc(kind, world, [row_item(1, '  Beratung   Plus  Q3 ', 85.0, 19, ' Std. ')], mixed=False)
    answer = {'success': True, 'article': {'id': 7, 'name': 'Beratung Plus Q3', 'unit': 'Std.', 'price': 85.0, 'taxRate': 19.0, 'mixRate': '19', 'description': ''}}
    program = "artMark(); fire(rows()[0].querySelector('.itemAddArticle'), 'click'); emit('asked', asked); emit('calls', calls);"

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program, answer=answer)

    assert out['asked'] == ['Als neuen Artikel speichern? Beratung Plus Q3 · Std. · 85,00 € · 19 %']                  # als Text (appConfirm setzt textContent)
    assert [(call['url'], call['method'], call['headers']) for call in out['calls']] == [('/articles/quick-add', 'POST', {'Content-Type': 'application/json'})]
    (sent,) = out['sent']
    assert sent['url'] == '/articles/quick-add' and sent['body'] == {'name': 'Beratung Plus Q3', 'unit': 'Std.', 'price': 85, 'taxRate': 19}
    assert list(sent['body']) == ['name', 'unit', 'price', 'taxRate']


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_plus_sends_the_rate_of_the_row_the_rate_of_the_head_or_0_without_a_tax_display(world, tmp_path, kind):
    doc_id = make_doc(kind, world, [row_item(1, 'Fahrtkosten', 30.0, 19, 'km'), row_item(2, 'Schulung', 120.0, 7, 'Std.')], mixed=True)
    html = page_of(kind, world, doc_id)
    plus = "fire(rows()[1].querySelector('.itemAddArticle'), 'click');"

    got = {}
    for name, program in {'mixed': "artMark();" + plus,
                          'head': "artMark(); ticked('mixed_tax', false); typed($('tax_rate'), '5.5');" + plus,
                          'none': "artMark(); ticked('show_tax', false);" + plus}.items():
        out = run(tmp_path, kind, html, program + "emit('asked', asked);", answer={'success': False, 'error': 'x'})
        got[name] = (out['sent'][0]['body']['taxRate'], out['asked'][0].rsplit(' · ', 1)[1])

    assert got == {'mixed': (7, '7 %'), 'head': (5.5, '5,5 %'), 'none': (0, '0 %')}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_name_is_the_description_collapsed_and_cut_to_200_characters_and_the_unit_the_trimmed_field(world, tmp_path, kind):
    doc_id = make_doc(kind, world, [row_item(1, 'Fahrtkosten', 30.0, 19, 'km')], mixed=False)
    program = ("artMark(); var row = rows()[0]; typed(row.querySelector('.item-description'), '  a  ' + 'b'.repeat(300) + '  '); "
               "fire(row.querySelector('.itemAddArticle'), 'click');")

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program, answer={'success': False, 'error': 'x'})

    body = out['sent'][0]['body']
    assert body['name'] == 'a ' + 'b' * 198 and len(body['name']) == 200 and body['unit'] == 'km'


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_declining_the_question_sends_nothing(world, tmp_path, kind):
    doc_id = make_doc(kind, world, [row_item(1, 'Fahrtkosten', 30.0)], mixed=False)
    program = "artMark(); appConfirm = function (text, ok) { asked.push(text); }; fire(rows()[0].querySelector('.itemAddArticle'), 'click'); emit('asked', asked);"

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program)

    assert len(out['asked']) == 1 and out['sent'] == [] and out['messages'] == []


SENT_ARTICLE = "{id: 7, name: 'Fahrtkosten', unit: 'Std.', price: 30, taxRate: 19, mixRate: '19', description: ''}"


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_after_the_server_has_created_the_article_the_row_knows_it_and_the_marks_go(world, tmp_path, kind):
    doc_id = make_doc(kind, world, [row_item(1, 'Fahrtkosten', 30.0)], mixed=False)
    answer = {'success': True, 'article': {'id': 7, 'name': 'Fahrtkosten', 'unit': 'Std.', 'price': 30.0, 'taxRate': 19.0, 'mixRate': '19', 'description': ''}}
    program = ("artMark(); emit('before', looks()); fire(rows()[0].querySelector('.itemAddArticle'), 'click'); "
               "afterAll(function () { emit('after', looks()); emit('notes', notes); emit('stored', [Object.keys(articlesData), rows()[0].getAttribute('data-article-id')]); });")

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program, answer=answer)

    assert out['before'][0]['classes'] == ['itemCanSave', 'itemNoArticle']
    assert out['after'] == [{'deviating': {}, 'classes': [], 'buttons': 1}]                                          # die Zeile hat ihren Artikel, nichts weicht ab, das „+“ ist weg
    assert out['notes'] == [['Artikel gespeichert.', 'success']] and out['stored'] == [['7'], '7']


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_refusal_of_the_server_is_shown_as_it_is_and_the_row_stays_as_it_was(world, tmp_path, kind):
    doc_id = make_doc(kind, world, [row_item(1, 'Fahrtkosten', 30.0)], mixed=False)
    answer = {'success': False, 'error': 'Einen Artikel mit dieser Bezeichnung gibt es schon.'}
    program = ("artMark(); fire(rows()[0].querySelector('.itemAddArticle'), 'click'); "
               "afterAll(function () { emit('after', looks()); emit('notes', notes); emit('id', rows()[0].getAttribute('data-article-id')); });")

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program, answer=answer)

    assert out['notes'] == [['Einen Artikel mit dieser Bezeichnung gibt es schon.', 'error']]
    assert out['after'][0]['classes'] == ['itemCanSave', 'itemNoArticle'] and out['id'] is None


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('failure', [
    pytest.param("function () { return Promise.reject(new Error('Netz weg')); }", id='Netzfehler'),
    pytest.param("function () { return Promise.resolve({json: function () { return Promise.reject(new Error('kein JSON')); }}); }", id='keine-JSON-Antwort'),
    pytest.param("function () { return Promise.resolve({json: function () { return Promise.resolve(null); }}); }", id='leere-Antwort'),
    pytest.param("function () { return Promise.resolve({json: function () { return Promise.resolve({success: true}); }}); }", id='Erfolg-ohne-Artikel'),
])
def test_a_failed_request_shows_a_fixed_sentence_never_the_text_of_the_error(world, tmp_path, kind, failure):
    doc_id = make_doc(kind, world, [row_item(1, 'Fahrtkosten', 30.0)], mixed=False)
    program = (f"artMark(); fetch = {failure}; fire(rows()[0].querySelector('.itemAddArticle'), 'click'); "
               "afterAll(function () { emit('notes', notes); emit('id', rows()[0].getAttribute('data-article-id')); });")

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program)

    assert out['notes'] == [[ARTICLE_TEXTS['failed'], 'error']] and out['id'] is None
    assert 'Netz weg' not in json.dumps(out) and 'kein JSON' not in json.dumps(out)


# ── 5. Ereignisse: bis zur Tabelle ───────────────────────────────────────────

@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_typing_in_a_row_marks_and_unmarks_through_the_events_that_reach_the_table(world, tmp_path, kind):
    add_articles(world, CONSULTING)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 85.0)], mixed=False)
    program = r"""
      var row = rows()[0], price = row.querySelector('.item-price'), unit = row.querySelector('.item-unit'), description = row.querySelector('.item-description');
      emit('start', look(row));
      typed(price, '8'); emit('price', look(row));
      typed(price, '85'); emit('priceBack', look(row));
      typed(unit, 'Stk.'); emit('unit', look(row));
      typed(unit, 'Std.'); emit('unitBack', look(row));
      typed(description, 'Fahrtkosten'); emit('other', look(row));
      typed(description, 'Beratung Q3'); emit('otherBack', look(row));
    """

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program)

    assert out['start'] == {'deviating': {}, 'classes': [], 'buttons': 0}                                           # bevor die Seite markiert hat (das tut ihr Skript am Ende), steht nichts da
    assert out['price']['deviating'] == {'price': 'Artikel: 85,00'} and out['price']['buttons'] == 1                # der erste Ereignis-Aufruf legt das „+“ an
    assert out['priceBack'] == out['unitBack'] == out['otherBack'] == {'deviating': {}, 'classes': [], 'buttons': 1}
    assert out['unit']['deviating'] == {'unit': 'Artikel: Std.'}
    assert out['other'] == {'deviating': {}, 'classes': ['itemCanSave', 'itemNoArticle'], 'buttons': 1}


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_the_select_the_checkboxes_and_the_head_choice_also_reach_it(world, tmp_path, kind):
    """Die Auswahl in der Zeile, „Steuersatz je Position“, „Umsatzsteuer ausweisen“ und das Feld „Mehrwertsteuer“ im Kopf liegen in der Tabelle: ihre Ereignisse erreichen sie."""
    add_articles(world, BOOK)
    doc_id = make_doc(kind, world, [row_item(1, 'Fachbuch', 24.9, 19, 'Stk.')], mixed=False)
    program = r"""
      var head = $('tax_choice');
      emit('start', looks()[0].deviating);
      head.value = '7'; bubble(head, 'change'); emit('head7', looks()[0].deviating);
      head.value = '19'; bubble(head, 'change'); emit('head19', looks()[0].deviating);
      ticked('show_tax', false); emit('noTax', looks()[0].deviating);
    """

    out = run(tmp_path, kind, page_of(kind, world, doc_id), "artMark();" + program)

    assert out['start'] == out['head19'] == {'first': 'Artikel: 7 % – Beleg: 19 %'}
    assert out['head7'] == out['noTax'] == {}


# ── 6. Gesperrte Rechnung: nichts ────────────────────────────────────────────

@NO_NODE
@pytest.mark.parametrize('status', ['sent', 'overdue', 'paid', 'cancelled'])
def test_a_locked_invoice_is_never_marked_and_gets_no_plus(world, tmp_path, status):
    add_articles(world, CONSULTING)
    invoice_id = make_doc('invoice', world, [row_item(1, 'Beratung', 8.0), row_item(2, 'Fahrtkosten', 30.0)], mixed=False)
    force_status(world.db, invoice_id, status)
    html = page_of('invoice', world, invoice_id)
    assert Dom(html).one('input', id='invoice_locked')['value'] == 'true'

    program = "artMark(); emit('marked', looks()); typed(rows()[0].querySelector('.item-price'), '1'); ticked('show_tax', false); emit('typed', looks());"
    out = run(tmp_path, 'invoice', html, program)

    assert out['marked'] == out['typed'] == [{'deviating': {}, 'classes': [], 'buttons': 0}] * 2


@NO_NODE
def test_an_open_invoice_is_marked_but_the_same_invoice_locked_is_not(world, tmp_path):
    add_articles(world, CONSULTING)
    invoice_id = make_doc('invoice', world, [row_item(1, 'Beratung', 8.0)], mixed=False)

    open_look = looks_of(tmp_path, 'invoice', page_of('invoice', world, invoice_id))
    force_status(world.db, invoice_id, 'sent')
    locked_look = looks_of(tmp_path, 'invoice', page_of('invoice', world, invoice_id))

    assert open_look[0]['deviating'] == {'price': 'Artikel: 85,00'} and locked_look[0] == {'deviating': {}, 'classes': [], 'buttons': 0}


def test_the_server_marks_no_row_itself_locked_or_not(world):
    add_articles(world, CONSULTING)
    invoice_id = make_doc('invoice', world, [row_item(1, 'Beratung', 8.0)], mixed=False)
    for status in ('draft', 'sent'):
        force_status(world.db, invoice_id, status)
        assert_no_marker_in_markup(page_of('invoice', world, invoice_id))


# ── 7. Reine Anzeige: der Payload bleibt, wie er war ─────────────────────────

@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_marking_and_the_plus_change_nothing_that_is_saved(world, tmp_path, kind):
    add_articles(world, CONSULTING, BOOK)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 8.0, 19, 'Stk.'), row_item(2, 'Fahrtkosten', 30.0, 7, 'km'), row_item(3, 'Fachbuch', 24.9, 19, 'Stk.')],
                      mixed=True)
    html = page_of(kind, world, doc_id)
    save = f"{SAVE_FUNCTION[kind]}();"

    plain = sent_payload(run(tmp_path, kind, html, save), kind)
    marked = sent_payload(run(tmp_path, kind, html, "artMark(); typed(rows()[0].querySelector('.item-price'), '8'); typed(rows()[1].querySelector('.item-unit'), 'km'); " + save), kind)

    assert marked == plain
    assert all(set(item) <= {'position', 'quantity', 'unit', 'description', 'unitPrice', 'totalPrice', 'taxRate'} for item in plain['items'])      # kein Schlüssel kommt dazu (keine Artikel-ID)
    assert [i['unitPrice'] for i in plain['items']] == [8, 30, 24.9]


# ── 8. Das HTML ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('kind', KINDS)
def test_the_block_is_in_both_masks_exactly_once_inside_the_shared_script_block_before_the_script_of_the_page(world, kind):
    add_articles(world, CONSULTING)
    html = page_of(kind, world)

    blocks = [s for s in Dom(html).scripts if 'function artMark(' in s]
    assert len(blocks) == 1 and html.count('function artMark(') == html.count('function artFind(') == 1
    assert 'function mixOn(' in blocks[0] and blocks[0].strip() == mixed_tax_script().replace('<script>', '').replace('</script>', '').strip()
    assert html.index('function artMark(') < html.index(f'function {SAVE_FUNCTION[kind]}(')
    assert '__' not in blocks[0]                                                                                      # kein Platzhalter bleibt stehen


@pytest.mark.parametrize('kind', KINDS)
def test_the_scripts_of_the_masks_call_artMark_after_loading_and_after_adding_and_removing_a_row(world, kind):
    _dom, main, _mix = page_script(page_of(kind, world), kind)

    for name in ('addFreeRow', 'addArticleRow', 'removeRow'):
        assert 'artMark();' in extract_function(main, name), name
    assert main.count('artMark();') == 4                                                                              # die drei Funktionen und einmal nach dem Laden
    assert re.search(r'calculateTotals\(\);\s*artMark\(\);\s*(?://[^\n]*\s*)?updateOwnCompany\(\);', main)           # nach dem ersten Rechnen beim Laden (articlesData steht dann längst da)
    assert main.index('const articlesData') < main.index('artMark();')


@pytest.mark.parametrize('kind', KINDS)
def test_the_server_writes_none_of_the_marker_classes_and_no_article_id_into_the_rows(world, kind):
    ids = add_articles(world, CONSULTING)
    doc_id = make_doc(kind, world, [row_item(1, 'Beratung', 8.0)], mixed=False)

    assert_no_marker_in_markup(page_of(kind, world))
    assert_no_marker_in_markup(page_of(kind, world, doc_id))
    assert ids                                                                                                        # der Artikel steht nur als Datenblock in der Seite


HOSTILE = '</script><img src=x onerror=alert(1)>"\'<b>fett</b> &amp; <!-- x -->'


@pytest.mark.parametrize('page', [PageInvoice, PageQuote], ids=['Rechnung', 'Angebot'])
def test_a_hostile_article_name_breaks_neither_the_block_nor_the_structure_of_the_page(tmp_db, page):
    add_contacts(tmp_db)
    tmp_db.insert_article(HOSTILE, 'Std.', 85.0, 19, HOSTILE)
    hostile = page(tmp_db, {}, None)
    tmp_db.update_article(tmp_db.fetch_articles()[0][0], 'Harmlos', 'Std.', 85.0, 19, 'Harmlos')
    benign = page(tmp_db, {}, None)

    assert '</script><img' not in hostile
    assert_same_structure(hostile, benign)
    assert [s for s in Dom(hostile).scripts if 'function artMark(' in s] == [s for s in Dom(benign).scripts if 'function artMark(' in s]      # der Block kennt keinen Artikel
    (article,) = data_block(Dom(hostile), 'articlesData').values()
    assert article['name'] == HOSTILE


@NO_NODE
@pytest.mark.parametrize('kind', KINDS)
def test_a_hostile_name_stays_text_in_the_question_the_request_and_the_dom(world, tmp_path, kind):
    world.db.insert_article(HOSTILE, 'Std.', 85.0, 19, '')
    other = HOSTILE.replace('fett', 'fetter')
    doc_id = make_doc(kind, world, [row_item(1, HOSTILE + ' Q3', 8.0), row_item(2, other, 30.0, 19, 'km')], mixed=False)
    program = ("function count() { return ['[src=x]', 'b', 'script', 'img'].map(function (tag) { return document.querySelectorAll(tag).length; }); } var before = count(); "
               "artMark(); emit('looks', looks()); fire(rows()[1].querySelector('.itemAddArticle'), 'click'); emit('asked', asked); emit('nodes', [before, count()]);")

    out = run(tmp_path, kind, page_of(kind, world, doc_id), program, answer={'success': False, 'error': 'x'})

    collapsed = re.sub(r'\s+', ' ', other)
    assert out['looks'][0]['deviating'] == {'price': 'Artikel: 85,00'}                                               # der Artikel wird auch mit so einem Namen gefunden
    assert out['asked'] == ['Als neuen Artikel speichern? ' + collapsed + ' · km · 30,00 € · 19 %']
    assert out['sent'][0]['body']['name'] == collapsed
    before, after = out['nodes']
    assert before == after and before[0] == 0                                                                        # nichts wurde als Markup gelesen: keine Knoten dazu, kein <img src=x>


# ── 9. Statisch: der Quelltext des Blocks, die Texte, das Stylesheet ─────────

CODE = re.sub(r'//[^\n]*', '', ARTICLE_MARK_JS)                                                                       # die Kommentare zählen nicht


def test_the_block_uses_only_var_and_function_with_the_prefix_art_and_sets_values_only_as_values_or_text():
    assert not re.search(r'(?<![A-Za-z0-9_$.])(const|let)\s', CODE)
    assert '=>' not in CODE and '`' not in CODE and '${' not in CODE
    for name in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'Function(', 'dataset', 'localStorage', 'XMLHttpRequest'):
        assert name not in ARTICLE_MARK_JS, name
    assert '</script' not in ARTICLE_MARK_JS.lower()
    assert 'textContent' in CODE and 'createElement' in CODE and '.title =' in CODE
    assert '.style' not in CODE and "'style'" not in CODE and '"style"' not in CODE                                  # kein Inline-Stil: Die Optik steht in buch.css
    names = set(re.findall(r'function (\w+)\(', CODE))
    assert names == {'artNorm', 'artLow', 'artWordStart', 'artStarts', 'artNames', 'artFind', 'artDecimal', 'artScale', 'artPriceDiffers', 'artPrice', 'artRateText',
                     'artArticleRate', 'artOptionLabel', 'artSet', 'artDeviations', 'artMarkRow', 'artButton', 'artMark', 'artRowRate', 'artAdd', 'artSend', 'artInit'}
    assert all(name.startswith('art') for name in names)
    assert set(re.findall(r'(?<![\w.])var (\w+)', CODE)) >= {'ART_TEXTS'}


def test_no_rate_no_text_and_no_article_is_named_in_the_source():
    assert not re.search(r'(?<![\d.])(19|7|5\.5)(?![\d.])', CODE), 'ein Satz steht im Quelltext des Skripts'
    for text in ARTICLE_TEXTS.values():
        assert text not in CODE, text                                                                                 # die Texte kommen vom Server (``ART_TEXTS``), nicht aus dem Quelltext
    assert ARTICLE_MARK_JS.count('__ART_TEXTS__') == 1 and '/articles/quick-add' in CODE


def test_the_texts_reach_the_script_only_through_script_json_and_never_by_replacing_over_a_page():
    source = inspect.getsource(mixed_tax_script)
    assert "ARTICLE_MARK_JS.replace('__ART_TEXTS__', script_json(ARTICLE_TEXTS))" in source and 'json.dumps' not in source
    block = mixed_tax_script()
    assert block.count('<script>') == 1 and block.count('</script>') == 1 and '__ART_TEXTS__' not in block
    assert f'var ART_TEXTS = {script_json(ARTICLE_TEXTS)};' in block
    assert set(ARTICLE_TEXTS) == set(re.findall(r'ART_TEXTS\.(\w+)', CODE))                                           # jeder Text wird gebraucht, keiner fehlt


def test_the_stylesheet_has_the_rules_and_wins_against_the_white_sheet_of_the_form_in_both_color_schemes():
    sheet = (pathlib.Path(pages_invoice.__file__).resolve().parent.parent / 'buch.css').read_text(encoding='utf-8')

    def rule(selector):
        found = re.search(r'(?m)^\s*' + re.escape(selector) + r'\s*\{([^}]*)\}', sheet)
        assert found, selector
        return found.group(1)

    assert 'background-color' in rule('.invoice-items .itemDeviates') and '!important' in rule('.invoice-items .itemDeviates')       # gegen ``.invoice-container *``
    assert 'border-color' in rule('.invoice-items input.itemDeviates, .invoice-items select.itemDeviates')
    assert 'border-left' in rule('.invoice-items .itemNoArticle > td:first-child')
    assert 'visibility: hidden' in rule('.invoice-items .itemAddArticle') and 'visibility: visible' in rule('.invoice-items .itemCanSave .itemAddArticle')   # der Platz bleibt reserviert
    assert 'white-space: nowrap' in rule('.invoice-items tbody td.no-pdf')                                                                                    # ✕ und „+“ in einer Zeile
    # das Blatt ist in beiden Farbschemata weiß (``.invoice-container``): Die Regeln stehen außerhalb der Farbschema-Blöcke und gelten für beide
    for block in re.findall(r'@media \(prefers-color-scheme: (?:dark|light)\) \{.*?\n\}', sheet, flags=re.S):
        assert 'itemDeviates' not in block and 'itemAddArticle' not in block
