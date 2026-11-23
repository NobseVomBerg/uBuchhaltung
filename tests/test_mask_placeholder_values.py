# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Werte aus der Datenbank, die wie ein Platzhalter der Masken aussehen (2.8.2, Befund N2: ``server/pages_invoice.py``, ``server/pages_quote.py``).

Bis 2.8.1 ersetzten die Masken von Rechnung und Angebot drei Platzhalter (``__TAX_PCT__``, ``__SHOW_TAX_CHECKED__``, ``__TAX_PREFILL_NOTE__``) per ``str.replace`` über die FERTIGE
Seite – nachdem die Bezeichnungen und Befreiungsgründe der Steuersätze, die Kunden, Artikel, Positionen und Texte schon darin standen. Ein Wert, der so einen Text enthielt, wurde
mitersetzt; beim Hinweis zur Vorbelegung (festes HTML mit Anführungszeichen) brach er das Attribut (``data-reason``) und das JSON im Skriptblock (``MIX_RATES``): Die Maske war
unbenutzbar. Jetzt stehen die drei Werte beim Aufbau an ihrer Stelle im Formular, und über die fertige Seite läuft keine Ersetzung mehr.

Geprüft wird je Seite – Rechnung und Angebot; neuer Beleg, neuer Beleg nach einer Rechnung ohne Steuerausweis (die Maske sagt, dass sie ohne Umsatzsteuer vorbelegt ist), geladener Beleg
mit und ohne Steuerausweis –, in einer Welt, in der Steuersatz-Eintrag, Kunde, Artikel, eigene Firma, Positionen und Texte JEDEN Platzhalter der beiden Seiten enthalten:

* **Gegen eine Seite mit harmlosen Texten:** Dieselbe Welt noch einmal, mit denselben Texten, in denen die Unterstriche eines Platzhalters durch Bindestriche ersetzt sind. Ersetzt man in
  der ersten Seite die feindlichen Texte durch die harmlosen, ist sie Zeichen für Zeichen die zweite: Kein Wert wurde angefasst, und nichts anderes hat sich verschoben. (Das Gegenstück
  zur alten Ersetzung über die ganze Seite: Sie machte aus dem feindlichen Text eine andere Seite.)
* Dieselben Elemente und Skriptblöcke (``assert_same_structure``) und lesbares JSON in jedem Datenblock und in der Liste der Steuersätze.
* Die drei Werte stehen weiter dort, wo die Ersetzung sie hinsetzte (Häkchen, Satz im Feld, Hinweis der Vorbelegung). Dass eine Seite mit harmlosen Texten Zeichen für Zeichen die von
  2.8.1 ist, ist eine Sache des Vergleichs alt gegen neu (im Bericht der Änderung), nicht dieser Datei: Sie führt keine Schnappschüsse.

Alle Namen, Gründe, Konten und Beträge sind erfunden.
"""
import json
import pathlib
import re
from types import SimpleNamespace

import pytest

import server.handlers as handlers
import userctx
from db import Database
from server import pages_invoice, pages_quote
from server.pages_invoice import PageInvoice
from server.pages_quote import PageQuote
from tax_groups import EXEMPT, MAX_LABEL_LENGTH, MAX_REASON_LENGTH, STANDARD
from tests.dom_helpers import Dom, assert_same_structure, data_block
from tests.test_tax_entries_mask import mask_list

# Die drei, die ``str.replace`` bis 2.8.1 über die ganze Seite ersetzte (ihre Namen stehen im Quelltext nicht mehr) – dazu jeder Platzhalter, der heute noch in den Quelltexten der
# beiden Seiten steht: Sie werden dort in Skripte eingesetzt, und kein Wert aus der Datenbank soll sie je treffen
REPLACED_UNTIL_2_8_1 = ('__TAX_PCT__', '__SHOW_TAX_CHECKED__', '__TAX_PREFILL_NOTE__')
PLACEHOLDER = re.compile(r'__[A-Z][A-Z0-9_]*__')


def placeholders():
    found = set(REPLACED_UNTIL_2_8_1)
    for module in (pages_invoice, pages_quote):
        found.update(PLACEHOLDER.findall(pathlib.Path(module.__file__).read_text(encoding='utf-8')))
    return sorted(found)


def harmless(text):
    """Derselbe Text ohne Platzhalter: die Unterstriche eines ``__NAME__`` werden zu Bindestrichen."""
    return text.replace('__', '--')


# Je Feld ein eigener Anfang, damit ein Fund in der Seite zu genau einem Feld gehört; Bezeichnung (60 Zeichen) und Grund (300 Zeichen) sind begrenzt, darum tragen sie nur die drei Platzhalter,
# die bis 2.8.1 über die ganze Seite ersetzt wurden
ALL = 'ALLE ' + ' '.join(placeholders())
LABEL_A = 'EINS ' + ' '.join(REPLACED_UNTIL_2_8_1)
LABEL_B = 'ZWEI ' + ' '.join(reversed(REPLACED_UNTIL_2_8_1))
REASON = 'GRUND ' + ' '.join(REPLACED_UNTIL_2_8_1)
HOSTILE_TEXTS = (ALL, LABEL_A, LABEL_B, REASON)


def test_no_page_builder_replaces_text_over_its_finished_page():
    """Die Bauart des Fehlers, statisch: Ein Aufbau, der die Seite fertig zusammensetzt (``s``) und dann einen Text darin ersetzt (``s = s.replace(…)``, ``s = re.sub(…)``), trifft auch jeden Wert
    aus der Datenbank, der so aussieht. Platzhalter gibt es nur noch in den Skriptblöcken, die ohne Daten gebaut sind (die Konstanten ``…_SCRIPT``, ``MIXED_TAX_JS``) – ersetzt wird dort, bevor
    ein Wert aus der Datenbank dazukommt."""
    for module in (pages_invoice, pages_quote):
        source = pathlib.Path(module.__file__).read_text(encoding='utf-8')
        assert not re.search(r'^\s*s\s*=\s*(s\.replace|re\.sub)\(', source, re.M), module.__name__


def test_the_hostile_texts_fit_their_fields_and_hold_every_placeholder_of_the_two_pages():
    assert len(LABEL_A) <= MAX_LABEL_LENGTH and len(LABEL_B) <= MAX_LABEL_LENGTH and len(REASON) <= MAX_REASON_LENGTH
    assert set(REPLACED_UNTIL_2_8_1) <= set(placeholders())
    for placeholder in ('__MIX_RATES__', '__QUESTION__', '__DATE_FIELD__', '__CONFIRM__', '__INVOICE_ID__'):             # die Platzhalter der Skripte, die heute noch im Quelltext stehen
        assert placeholder in ALL
    assert not [text for text in HOSTILE_TEXTS if any(text != other and text in other for other in HOSTILE_TEXTS)]
    assert all(harmless(text) != text and '__' not in harmless(text) for text in HOSTILE_TEXTS)


def free_revenue_account(db, number):
    if db.get_coa_id_by_account_number(number) is None:
        db.insert_chart_of_accounts(4, number, f'Erlöse (erfundenes Testkonto {number})', 'Betriebliche Erträge')
    return db.get_coa_id_by_account_number(number)


def build_pages(db, monkeypatch, texts):
    """Die Welt mit ``texts`` (``ALL``, ``LABEL_A``, ``LABEL_B``, ``REASON`` – die feindlichen oder die harmlosen) in jedem Feld, das in eine Maske kommt, und die acht Seiten daraus. Die
    Reihenfolge ist fest (IDs und Zustand der Welt gleichen sich in beiden Läufen): neue Belege, geladene Belege, dann eine Rechnung ohne Steuerausweis, nach der die neuen Belege den Hinweis
    der Vorbelegung tragen."""
    every, label_a, label_b, reason = texts
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: db)

    def contact(kind, name, **extra):
        return db.insert_contact(contact_type=kind, entity_type='company', display_name=name, company_name=name, email='info@example.com', **extra)

    own = contact('own', f'Eigene Firma {every}', street=f'Hauptstr. {every}', postal_code='10115', city='Berlin', phone='030 1234567', tax_id='DE123456789')
    customer = contact('customer', f'Kunde {every}', customer_number='K-100', street=f'Kundenweg {every}', postal_code='54321', city='Kundenhausen')
    db.insert_account('Giro', 'Eigene Firma GmbH', 'DE89370400440532013000', 'TESTDE1X', 'Testbank AG', is_cash=0, skr_account=1800)
    bank = {account[1]: account[0] for account in db.fetch_accounts()}['Giro']
    db.insert_article(f'Artikel {every}', 'Stk.', 10.0, 19, f'Beschreibung {every}')
    account = free_revenue_account(db, 4150)
    reduced = db.insert_tax_rate(label=label_a, bp=550, kind=STANDARD, revenue_coa_id=account)
    db.insert_tax_rate(label=label_b, bp=0, kind=EXEMPT, reason=reason, revenue_coa_id=account)
    userctx.set_user('anna')

    def invoice(number, **over):
        payload = {'invoiceNumber': number, 'invoiceDate': '2026-06-07', 'deliveryDate': '2026-06-05', 'customerId': customer, 'ownCompanyId': own, 'bankAccountId': bank,
                   'dueDate': '2026-06-21', 'paymentTerms': f'Zahlbar {every}', 'orderNumber': f'B-{every}', 'discountPercentage': None, 'discountDays': None,
                   'items': [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': f'Posten {every}', 'unitPrice': 10.0, 'taxRate': 5.5}], 'taxRate': 0.055,
                   'taxRateId': reduced, **over}
        answer = json.loads(handlers.handle_invoice_save(json.dumps(payload).encode()))
        assert answer['success'] is True, answer
        return answer['invoice_id']

    def quote(number, **over):
        payload = {'quoteNumber': number, 'quoteDate': '2026-06-07', 'validUntil': '2026-07-07', 'customerId': customer, 'ownCompanyId': own, 'status': 'draft',
                   'introText': f'Guten Tag {every}', 'closingText': f'Mit freundlichen Grüßen {every}',
                   'items': [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': f'Posten {every}', 'unitPrice': 10.0, 'taxRate': 5.5}], 'taxRate': 0.055,
                   'taxRateId': reduced, **over}
        answer = json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))
        assert answer['success'] is True, answer
        return answer['quote_id']

    without_tax = dict(showTax=False, taxRate=0, taxRateId=None, items=[{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': f'Posten {every}', 'unitPrice': 10.0, 'taxRate': 0}])
    pages = {'invoice new': PageInvoice(db, {}, None), 'quote new': PageQuote(db, {}, None)}
    pages['invoice loaded'] = PageInvoice(db, {}, invoice('26R100'))
    pages['quote loaded'] = PageQuote(db, {}, quote('26A100'))
    notax_invoice = invoice('26R101', **without_tax)
    notax_quote = quote('26A101', **without_tax)
    pages['invoice loaded without tax'] = PageInvoice(db, {}, notax_invoice)
    pages['quote loaded without tax'] = PageQuote(db, {}, notax_quote)
    pages['invoice new after no tax'] = PageInvoice(db, {}, None)
    pages['quote new after no tax'] = PageQuote(db, {}, None)
    return pages


@pytest.fixture
def pages(tmp_path, monkeypatch):
    """Die Seiten in der feindlichen und in der harmlosen Welt – zwei Datenbanken, derselbe Aufbau."""
    hostile = build_pages(Database(db_name=str(tmp_path / 'hostile.db')), monkeypatch, HOSTILE_TEXTS)
    benign = build_pages(Database(db_name=str(tmp_path / 'benign.db')), monkeypatch, tuple(harmless(text) for text in HOSTILE_TEXTS))
    return SimpleNamespace(hostile=hostile, benign=benign)


PAGE_NAMES = ['invoice new', 'quote new', 'invoice new after no tax', 'quote new after no tax', 'invoice loaded', 'quote loaded', 'invoice loaded without tax',
              'quote loaded without tax']


def unharmed(page):
    """Die Seite mit den harmlosen statt der feindlichen Texte: jeder Platzhalter der Seite ist ein Wert aus der Datenbank (in der harmlosen Seite steht kein ``__``), und eine Liste
    kürzt einen langen Namen mitten im Platzhalter – so greift die Abbildung auf jedes Stück, nicht nur auf die ganzen Texte."""
    return harmless(page)


@pytest.mark.parametrize('name', PAGE_NAMES)
def test_a_value_that_looks_like_a_placeholder_stays_untouched_and_nothing_else_moves(pages, name):
    hostile, benign = pages.hostile[name], pages.benign[name]

    assert '__' not in benign, f'{name}: die Seite trägt selbst ein „__“ – die Abbildung der Texte ist dann keine mehr'
    for text in HOSTILE_TEXTS:
        assert text in hostile, f'{name}: der Text steht nicht in der Seite – der Vergleich wäre leer ({text[:12]})'
        assert harmless(text) in benign
    assert unharmed(hostile) == benign, name


def unharmed_json(value):
    """Ein gelesener JSON-Wert mit den harmlosen statt der feindlichen Texte (die Texte haben kein Zeichen, das JSON anders schriebe)."""
    return json.loads(unharmed(json.dumps(value, ensure_ascii=False)))


@pytest.mark.parametrize('name', PAGE_NAMES)
def test_the_page_has_the_structure_it_has_with_harmless_texts_and_every_json_block_reads(pages, name):
    hostile, benign = pages.hostile[name], pages.benign[name]

    assert_same_structure(hostile, benign)
    for block in ('ownCompaniesData', 'customersData', 'articlesData'):
        assert unharmed_json(data_block(Dom(hostile), block)) == data_block(Dom(benign), block), block      # ``data_block`` liest mit ``json.loads``: ein gebrochenes JSON wirft dort
    assert unharmed_json(mask_list(hostile)) == mask_list(benign)                                          # die Liste der Steuersätze (``MIX_RATES``) im Skriptblock der Maske
    assert any(choice['label'] == LABEL_A for choice in mask_list(hostile))                                 # und sie trägt die Bezeichnung unverändert


# Die drei Werte, die die Ersetzung setzte: Häkchen „Umsatzsteuer ausweisen“, Satz im Feld (verstecktes Feld ``#tax_rate``), Hinweis „Vorbelegt wie die letzte Rechnung …“
EXPECTED = {
    'new': (True, '19', False),
    'new after no tax': (False, '19', True),
    'loaded': (True, '5.5', False),
    'loaded without tax': (False, '19', False),
}


@pytest.mark.parametrize('name', PAGE_NAMES)
def test_the_three_values_of_the_form_stand_where_the_replacement_put_them(pages, name):
    checked, rate, note = EXPECTED[name.partition(' ')[2]]

    for page in (pages.hostile[name], pages.benign[name]):
        dom = Dom(page)
        assert ('checked' in dom.one('input', id='show_tax')) is checked, name
        assert dom.one('input', id='tax_rate')['value'] == rate, name
        assert len([attrs for attrs in dom.all('p') if attrs.get('id') == 'tax_prefill_note']) == (1 if note else 0), name
