# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Fehlerpfade der Formular-Handler: Klartext statt weißer Seite (2.4.2).

Vorher wandelten mehrere Handler Formularwerte ungeschützt um (``int(post_data[…])``, ``float(post_data[…])``) oder gaben den
Text einer Ausnahme an den Nutzer zurück. Ergebnis: ein Serverfehler, eine weiße Seite (der Klartext eines Handlers stand im
``Location``-Kopf, der Body blieb leer) oder ein Python-/SQLite-Fehlertext in der Antwort.

* IDs aus Formularen nur über ``_form_id``: ungültig oder fehlend → 400 „Ungültige Anfrage.“, nichts geschrieben.
* Zahlenfelder über ``_form_int`` (ASCII-Ziffern, Bereich) und ``_finite_number``: ungültig → der Fehlerweg der Seite (303 mit
  ``err``/``error``/``msg``) – wo die Seite keinen hat, 400 mit Klartext. Nichts wird geschrieben.
* Keine Antwort enthält Fehlertext: ein fester Satz, der volle Fehler steht im Server-Log.
* Kontakte: doppelte Kundennummer oder doppeltes Kürzel sind ein Klartext (``ContactRefused``), kein 500.
* Artikel: eine Einheit oder ein Steuersatz außerhalb der festen Auswahl bleibt beim Speichern erhalten.
* Beleg löschen über die ID (die Belegnummer ist nicht eindeutig); ``link_booking_to_document`` unterscheidet eine vorhandene
  Verknüpfung (still) von einem Verweis ins Leere (Klartext).
* Wächter (statisch): in ``server/handlers.py`` kein ``int(post_data…)``/``float(post_data…)``, kein 5xx mit Ausnahmetext und (seit
  2.5.5) keine Weiterleitung (3xx), die den Text einer Ausnahme aus einem breiten ``except`` nennt – die Meldungen nach Sicherung,
  Wiederherstellung, DATEV-Export und WISO-Import (``/miscellaneous?…&msg=``) sind feste Sätze oder der Klartext einer eigenen
  Ablehnungsklasse (``tests/test_misc_messages.py``). Seit 2.5.7 ebenso in ``server/upload_handler.py``: Kein breites
  ``except … as e`` benutzt seine Ausnahme (außer in ``raise … from e`` und im Server-Log) – je Datei steht in der Antwort der
  Klartext einer eigenen Klasse oder ein fester Satz (``tests/test_upload_feedback.py``).

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import ast
import contextlib
import html as html_lib
import json
import os
import re
import sqlite3
import urllib.parse
import uuid
from types import SimpleNamespace

import pytest

import userctx
from db import Database
from db.contacts import ContactRefused
from server import handlers, receipt_files, upload_handler
from server.pages_masterdata import PageArticleEdit, PageArticles
from tests.document_helpers import make_books
from tests.dom_helpers import Dom
from tests.http_client import get, own_origin, post

DAY = '2026-03-04'
IBAN = 'DE89370400440532013000'
INVALID = 'Ungültige Anfrage.'
SECRET = 'geheimer Fehlertext /home/niemand/buch.db sqlite3'
LEAKS = ('Traceback', 'sqlite3', 'Error:', 'Errno', 'geheimer', '/home', '/tmp', '.py', '.db', '\\')

# Ganzzahlfelder: nie eine gültige Zahl (Text, nan, Exponent, Dezimalzahl, Ziffern anderer Schriften, Vorzeichen, Unterstrich,
# Leerraum in der Zahl, nur ein Minus, negativ, riesig)
BAD_INTEGERS = ['abc', 'nan', '1e9', '1.5', '٣', '１２', '²', '+5', '1_000', '1 2', '-', '-5', '9' * 30]
# IDs: dazu leer, 0, negativ und alles, was nicht in 64 Bit passt
BAD_IDS = ['abc', '', '0', '-1', '1.5', 'nan', '1e3', '٣', '１２', '9' * 30, str(2 ** 63)]
BAD_AMOUNTS = ['abc', 'nan', 'inf', '-inf', '1e12', '1e999', '1,5', '12.3.4']

YEAR_BAD = BAD_INTEGERS + ['', '1899', '3000', '0']
COUNTER_BAD = BAD_INTEGERS + ['1000000000']
LIFE_BAD = BAD_INTEGERS + ['0', '101']
PERCENT_BAD = BAD_INTEGERS + ['101', '-1']
SKR_BAD = BAD_INTEGERS + ['0', '100000']

TABLES = ('Documents', 'BookingDocuments', 'Bookings', 'Accounts', 'ChartOfAccounts', 'Contacts', 'ContactAddresses',
          'CompanyDetails', 'PersonDetails', 'ContactTypeLinks', 'PersonRoles', 'Articles', 'NumberRanges', 'Assets',
          'AssetDepreciations', 'AssetCategories')


# ── Hilfen ───────────────────────────────────────────────────────────────────
def snapshot(db, *tables):
    """Alle Zeilen der Tabellen (Vorgabe: alle, die die Handler berühren), roh – zum Vergleich vorher/nachher."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return {t: con.execute(f'SELECT * FROM {t} ORDER BY rowid').fetchall() for t in tables or TABLES}


def scalar(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql, args).fetchone()[0]


def assert_not_locked(db):
    """Nach einer Ablehnung hält keine Verbindung mehr die Schreibsperre."""
    con = sqlite3.connect(db.db_name, timeout=0.5)
    try:
        con.execute('BEGIN IMMEDIATE')
        con.rollback()
    finally:
        con.close()


def assert_no_leak(text):
    """Kein Python-/SQLite-Fehlertext, kein Ablauf (Traceback) und kein Dateipfad in einer Antwort."""
    for leak in LEAKS:
        assert leak not in text, (leak, text)


def shown_message(text):
    """Meldung und Art aus dem ``appMsg(…)``-Aufruf einer Seite. Die Meldung steht als JSON im Skriptblock, die Art je nach
    Seite in einfachen oder doppelten Anführungszeichen."""
    for script in Dom(text).scripts:
        found = re.search(r'function\(\)\{appMsg\(', script)
        if found:
            message, end = json.JSONDecoder().raw_decode(script, found.end())
            kind = re.match(r""",['"](\w+)['"]\);""", script[end:])
            assert kind, script[end:end + 40]
            return [message, kind.group(1)]
    raise AssertionError('kein appMsg-Aufruf in der Seite')


def q(message, safe=''):
    return urllib.parse.quote(message, safe=safe)


def form(**fields):
    """Ein Formular, wie ``parse_qs`` es liefert: je Feld eine Liste mit dem Text."""
    return {key: [str(value)] for key, value in fields.items()}


@pytest.fixture
def world(tmp_db):
    """Ein Bestand, an dem jeder Handler etwas findet: Beleg, Buchung, Bankkonto, Kontakt, Artikel, Nummernkreis, Anlage
    samt Kategorie. ``expense``/``anlage`` sind Konten des Kontenrahmens (COA-IDs)."""
    db = tmp_db
    db.insert_receipt('B-100', DAY, 'beleg.pdf', 'pfad/beleg.pdf', 'Info')
    db.insert_account('Probekonto', 'Inhaber', IBAN, 'TESTDEFFXXX', 'Probebank', is_cash=0, skr_account=1810)
    db.insert_article(name='Probeartikel', unit_price=10, tax_rate=19)
    db.insert_number_range('invoice', 2026, 'R', current_number=3)
    db.insert_asset_category('Probekategorie', 3)
    contact = db.insert_contact(contact_type='customer', entity_type='company', display_name='Muster GmbH',
                                company_name='Muster GmbH', customer_number='K-1', abbreviation='MUS')
    expense, anlage = db.get_coa_id_by_account_number(6220), db.get_coa_id_by_account_number(520)
    return SimpleNamespace(
        db=db, contact=contact, expense=expense, anlage=anlage,
        receipt=scalar(db, 'SELECT ID FROM Documents WHERE Number = ?', 'B-100'),
        booking=db.insert_booking(DAY, 10),
        account=scalar(db, 'SELECT ID FROM Accounts WHERE Name = ?', 'Probekonto'),
        article=scalar(db, 'SELECT ID FROM Articles WHERE Name = ?', 'Probeartikel'),
        range=db.fetch_number_ranges('invoice')[0][0],
        category=scalar(db, 'SELECT ID FROM AssetCategories WHERE Name = ?', 'Probekategorie'),
        asset=db.insert_asset('Rechner', DAY, 100, 3, coa_id=anlage))


# Die gültige Anfrage jedes Handlers; ein Test setzt ein Feld auf einen ungültigen Wert
def receipt_form(w):
    return form(id=w.receipt, number='B-200', date=DAY, filename='neu.pdf', path='pfad/neu.pdf', info='Neu')


def link_form(w):
    return form(booking_id=w.booking, document_id=w.receipt, relation_type='receipt')


def account_add_form(w):
    return form(name='Zweites Konto', holder='Inhaber', iban=IBAN, bic='TESTDEFFXXX', bank_name='Probebank',
                skr_account='1810')


def account_update_form(w):
    return {**account_add_form(w), 'id': [str(w.account)]}


def skr_add_form(w):
    return form(name='Probekonto', group='Gruppe', framework_nr=4, account=9901, private_share_percent=0)


def skr_update_form(w):
    return form(id=w.expense, framework_nr=4, account=6220, name='Neuer Name', group='Gruppe', private_share_percent=0)


def contact_add_form(w):
    return form(entity_type='company', contact_type='customer', company_name='Neue GmbH', customer_number='K-2')


def contact_update_form(w):
    return {**contact_add_form(w), 'contact_id': [str(w.contact)], 'customer_number': ['K-1']}


def article_form(w):
    return form(id=w.article, name='Probeartikel', unit='Stk.', unit_price='10.00', tax_rate='19', description='')


def range_add_form(w):
    return form(type='invoice', year=2027, letter='R', prefix='', current_number=0, description='',
                number_format='{yy}{l}{nnn}{s}')


def range_update_form(w):
    return {**range_add_form(w), 'id': [str(w.range)]}


def asset_add_form(w):
    return form(name='Neue Anlage', purchase_date=DAY, purchase_price='100', useful_life_years='3',
                depreciation_method='linear')


def asset_update_form(w):
    return {**asset_add_form(w), 'asset_id': [str(w.asset)]}


def depreciate_form(w):
    return form(asset_id=w.asset, year='2026', coa_id=w.expense, asset_coa_id=w.anlage)


def sale_form(w):
    return form(asset_id=w.asset, sale_date=DAY, sale_price='0')


def category_add_form(w):
    return form(name='Neue Kategorie', useful_life_years='3', depreciation_method='linear', coa_id='', notes='')


def category_update_form(w):
    return {**category_add_form(w), 'category_id': [str(w.category)]}


def transaction_form(w):
    return form(transaction_id='0', date=DAY, amount='-10.00', currency='EUR', recipient='Lieferant', text='Probe')


# ── die Helfer selbst ────────────────────────────────────────────────────────
@pytest.mark.parametrize('text, minimum, maximum, default, expected', [
    ('2026', 1900, 2999, None, 2026), ('  42 ', 0, 100, None, 42), ('007', 0, 100, None, 7), ('0', 0, 100, None, 0),
    ('100', 0, 100, None, 100), ('1900', 1900, 2999, None, 1900), ('2999', 1900, 2999, None, 2999),
    ('', 0, 100, None, None), ('', 0, 100, 5, 5), ('   ', 1, 100, 1, 1), (None, 0, 100, 3, 3),
    ('-5', -10, 10, None, -5), ('-0', -10, 10, None, 0), ('-11', -10, 10, None, None), ('11', -10, 10, None, None),
    ('101', 0, 100, None, None), ('-1', 0, 100, None, None), ('1899', 1900, 2999, None, None),
    ('3000', 1900, 2999, None, None),
])
def test_form_int_reads_whole_numbers_in_range(text, minimum, maximum, default, expected):
    assert handlers._form_int(text, minimum, maximum, default) == expected


@pytest.mark.parametrize('text', [v for v in BAD_INTEGERS if v != '-5'] + ['1e3', '2026.0', '٣٤', '1٣', '½', '0x10'])
def test_form_int_refuses_everything_that_is_not_ascii_digits(text):
    assert handlers._form_int(text, -10 ** 9, 10 ** 9) is None
    assert handlers._form_int(text, 0, 10 ** 9) is None
    assert handlers._form_int(text, 0, 10 ** 9, default=7) is None        # der Vorgabewert gilt nur für ein leeres Feld


def test_form_int_does_not_convert_a_huge_digit_string():
    assert handlers._form_int('9' * 5000, 0, 100) is None                 # kein int() über tausende Stellen


def test_form_int_takes_a_number_that_a_test_passes_instead_of_a_text():
    assert handlers._form_int(2026, 1900, 2999) == 2026
    assert handlers._form_int(0, 0, 100, default=5) == 5                  # wie ein leeres Feld (``0 or ''``)


def test_optional_ids_are_none_when_empty_the_id_when_valid_and_refused_as_a_whole_otherwise():
    assert handlers._optional_form_ids('', ' ', None, '12', ' 7 ') == (None, None, None, 12, 7)
    assert handlers._optional_form_ids() == ()
    for bad in BAD_IDS:
        if bad != '':
            assert handlers._optional_form_ids('1', bad, '2') is None, bad


# ── IDs: ungültig → 400, nichts geschrieben ──────────────────────────────────
REQUIRED_IDS = [
    pytest.param(handlers.handle_update_receipt, receipt_form, ['id'], id='Beleg ändern'),
    pytest.param(handlers.handle_link_document, link_form, ['booking_id', 'document_id'], id='Beleg verknüpfen'),
    pytest.param(handlers.handle_update_bankaccount, account_update_form, ['id'], id='Bankkonto ändern'),
    pytest.param(handlers.handle_update_skr, skr_update_form, ['id'], id='Konto des Kontenrahmens ändern'),
    pytest.param(handlers.handle_update_contact, contact_update_form, ['contact_id'], id='Kontakt ändern'),
    pytest.param(handlers.handle_update_article, article_form, ['id'], id='Artikel ändern'),
    pytest.param(handlers.handle_update_number_range, range_update_form, ['id'], id='Nummernkreis ändern'),
    pytest.param(handlers.handle_update_asset, asset_update_form, ['asset_id'], id='Anlage ändern'),
    pytest.param(handlers.handle_book_depreciation, depreciate_form, ['asset_id'], id='AfA buchen'),
    pytest.param(handlers.handle_asset_sale, sale_form, ['asset_id'], id='Anlage abgehen lassen'),
    pytest.param(handlers.handle_update_asset_category, category_update_form, ['category_id'], id='AfA-Kategorie ändern'),
]


@pytest.mark.parametrize('handler, build, fields', REQUIRED_IDS)
def test_a_required_id_that_is_not_an_id_is_refused_with_400_and_nothing_is_written(world, handler, build, fields):
    before = snapshot(world.db)
    for field in fields:
        for bad in BAD_IDS:
            post_data = build(world)
            post_data[field] = [bad]
            assert handler(world.db, post_data) == (400, INVALID), (field, bad)
            assert snapshot(world.db) == before, (field, bad)
        post_data = build(world)
        del post_data[field]                                              # das Feld fehlt ganz
        assert handler(world.db, post_data) == (400, INVALID), field
    assert snapshot(world.db) == before
    assert_not_locked(world.db)


def test_the_expense_account_of_a_depreciation_may_be_missing_garbage_is_a_400(world):
    """Fehlt das Aufwandskonto (leer, 0, ganz weggelassen), lehnt die Datenbank-Schicht mit ihrem Klartext ab und die Anlagenseite zeigt ihn
    (so war es vor 2.4.2 und ist es geblieben); was keine ID ist (Text, negativ, riesig), ist eine ungültige Anfrage."""
    db = world.db
    before = snapshot(db)
    expected = (303, where(f'/assets/view?id={world.asset}', 'Aufwandskonto fehlt'))
    for value in ('', '0', ' '):
        post_data = depreciate_form(world)
        post_data['coa_id'] = [value]
        assert handlers.handle_book_depreciation(db, post_data) == expected, value
    post_data = depreciate_form(world)
    del post_data['coa_id']
    assert handlers.handle_book_depreciation(db, post_data) == expected
    for bad in (b for b in BAD_IDS if b not in ('', '0')):
        post_data = depreciate_form(world)
        post_data['coa_id'] = [bad]
        assert handlers.handle_book_depreciation(db, post_data) == (400, INVALID), bad
    assert snapshot(db) == before


OPTIONAL_IDS = [
    pytest.param(handlers.handle_add_asset, asset_add_form,
                 ['asset_category_id', 'coa_id', 'supplier_id', 'document_id', 'parent_id'], id='Anlage anlegen'),
    pytest.param(handlers.handle_update_asset, asset_update_form,
                 ['asset_category_id', 'coa_id', 'supplier_id', 'document_id'], id='Anlage ändern'),
    pytest.param(handlers.handle_add_asset_category, category_add_form, ['coa_id'], id='AfA-Kategorie anlegen'),
    pytest.param(handlers.handle_update_asset_category, category_update_form, ['coa_id'], id='AfA-Kategorie ändern'),
    pytest.param(handlers.handle_book_depreciation, depreciate_form, ['asset_coa_id'], id='AfA buchen'),
    pytest.param(handlers.handle_add_contact, contact_add_form, ['company_contact_id'], id='Kontakt anlegen'),
    pytest.param(handlers.handle_update_contact, contact_update_form, ['company_contact_id'], id='Kontakt ändern'),
    pytest.param(handlers.handle_add_transaction, transaction_form,
                 ['account', 'contact_id', 'coa_id', 'invoice_id'], id='Buchung speichern'),
]


@pytest.mark.parametrize('handler, build, fields', OPTIONAL_IDS)
def test_an_optional_id_that_is_filled_but_not_an_id_is_refused_with_400(world, handler, build, fields):
    before = snapshot(world.db)
    for field in fields:
        for bad in (b for b in BAD_IDS if b != ''):                       # leer ist erlaubt: keine Angabe
            post_data = build(world)
            post_data[field] = [bad]
            assert handler(world.db, post_data) == (400, INVALID), (field, bad)
            assert snapshot(world.db) == before, (field, bad)
    assert_not_locked(world.db)


# ── Zahlenfelder: ungültig → die Meldung der Seite, nichts geschrieben ───────
def where(path, message, param='err'):
    return f'{path}{"&" if "?" in path else "?"}{param}={q(message)}'


NUMBER_FIELDS = [
    pytest.param(handlers.handle_add_number_range, range_add_form, 'year', YEAR_BAD,
                 lambda w: (303, where('/masterdata/numberranges', handlers.YEAR_ERROR)), id='Nummernkreis anlegen: Jahr'),
    pytest.param(handlers.handle_add_number_range, range_add_form, 'current_number', COUNTER_BAD,
                 lambda w: (303, where('/masterdata/numberranges', handlers.COUNTER_ERROR)),
                 id='Nummernkreis anlegen: Zählerstand'),
    pytest.param(handlers.handle_update_number_range, range_update_form, 'year', YEAR_BAD,
                 lambda w: (303, where(f'/masterdata/numberranges/edit?id={w.range}', handlers.YEAR_ERROR)),
                 id='Nummernkreis ändern: Jahr'),
    pytest.param(handlers.handle_update_number_range, range_update_form, 'current_number', COUNTER_BAD,
                 lambda w: (303, where(f'/masterdata/numberranges/edit?id={w.range}', handlers.COUNTER_ERROR)),
                 id='Nummernkreis ändern: Zählerstand'),
    pytest.param(handlers.handle_add_asset, asset_add_form, 'purchase_price', BAD_AMOUNTS,
                 lambda w: (400, handlers.AMOUNT_ERROR), id='Anlage anlegen: Anschaffungspreis'),
    pytest.param(handlers.handle_add_asset, asset_add_form, 'useful_life_years', LIFE_BAD,
                 lambda w: (400, handlers.USEFUL_LIFE_ERROR), id='Anlage anlegen: Nutzungsdauer'),
    pytest.param(handlers.handle_update_asset, asset_update_form, 'purchase_price', BAD_AMOUNTS,
                 lambda w: (303, where(f'/assets/view?id={w.asset}', handlers.AMOUNT_ERROR)),
                 id='Anlage ändern: Anschaffungspreis'),
    pytest.param(handlers.handle_update_asset, asset_update_form, 'useful_life_years', LIFE_BAD,
                 lambda w: (303, where(f'/assets/view?id={w.asset}', handlers.USEFUL_LIFE_ERROR)),
                 id='Anlage ändern: Nutzungsdauer'),
    pytest.param(handlers.handle_book_depreciation, depreciate_form, 'year', YEAR_BAD,
                 lambda w: (303, where(f'/assets/view?id={w.asset}', handlers.YEAR_ERROR)), id='AfA buchen: Jahr'),
    pytest.param(handlers.handle_asset_sale, sale_form, 'sale_price', BAD_AMOUNTS,
                 lambda w: (303, where(f'/assets/view?id={w.asset}', handlers.AMOUNT_ERROR)), id='Abgang: Erlös'),
    pytest.param(handlers.handle_add_asset_category, category_add_form, 'useful_life_years', LIFE_BAD,
                 lambda w: (303, where('/asset_categories', handlers.USEFUL_LIFE_ERROR)),
                 id='AfA-Kategorie anlegen: Nutzungsdauer'),
    pytest.param(handlers.handle_update_asset_category, category_update_form, 'useful_life_years', LIFE_BAD,
                 lambda w: (303, where('/asset_categories', handlers.USEFUL_LIFE_ERROR)),
                 id='AfA-Kategorie ändern: Nutzungsdauer'),
    pytest.param(handlers.handle_add_bankaccount, account_add_form, 'skr_account', SKR_BAD,
                 lambda w: (303, where('/masterdata/bankaccounts', handlers.SKR_ACCOUNT_ERROR, 'error')),
                 id='Bankkonto anlegen: SKR-Konto'),
    pytest.param(handlers.handle_update_bankaccount, account_update_form, 'skr_account', SKR_BAD,
                 lambda w: (303, where(f'/masterdata/bankaccounts/edit?id={w.account}', handlers.SKR_ACCOUNT_ERROR,
                                       'error')), id='Bankkonto ändern: SKR-Konto'),
    pytest.param(handlers.handle_add_skr, skr_add_form, 'private_share_percent', PERCENT_BAD,
                 lambda w: (303, f'/masterdata/skr?msg={q(handlers.PERCENT_ERROR)}&type=error'),
                 id='Konto des Kontenrahmens anlegen: Privatanteil'),
    pytest.param(handlers.handle_update_skr, skr_update_form, 'private_share_percent', PERCENT_BAD,
                 lambda w: (303, f'/masterdata/skr?msg={q(handlers.PERCENT_ERROR)}&type=error'),
                 id='Konto des Kontenrahmens ändern: Privatanteil'),
    pytest.param(handlers.handle_add_skr, skr_add_form, 'framework_nr', ['abc', '', 'nan', '1e9', '1.5', '٣', '9' * 30],
                 lambda w: (303, f'/masterdata/skr?msg={q("Rahmen-Nr. und Konto müssen Zahlen sein.")}&type=error'),
                 id='Konto des Kontenrahmens anlegen: Rahmen'),
    pytest.param(handlers.handle_add_skr, skr_add_form, 'account', ['abc', '', 'nan', '1e9', '1.5', '٣', '9' * 30],
                 lambda w: (303, f'/masterdata/skr?msg={q("Rahmen-Nr. und Konto müssen Zahlen sein.")}&type=error'),
                 id='Konto des Kontenrahmens anlegen: Konto'),
    pytest.param(handlers.handle_add_transaction, transaction_form, 'tax_rate', BAD_AMOUNTS,
                 lambda w: (400, 'Steuersatz und Steuerbetrag müssen Zahlen sein.'), id='Buchung speichern: Steuersatz'),
    pytest.param(handlers.handle_add_transaction, transaction_form, 'tax_amount', BAD_AMOUNTS,
                 lambda w: (400, 'Steuersatz und Steuerbetrag müssen Zahlen sein.'), id='Buchung speichern: Steuerbetrag'),
    pytest.param(handlers.handle_add_transaction, transaction_form, 'amount', BAD_AMOUNTS,
                 lambda w: (400, 'Der Betrag ist keine gültige Zahl.'), id='Buchung speichern: Betrag'),
    pytest.param(handlers.handle_update_article, article_form, 'unit_price', BAD_AMOUNTS,
                 lambda w: (303, where('/masterdata/articles', handlers.ARTICLE_NUMBER_ERROR, 'error')),
                 id='Artikel ändern: Preis'),
    pytest.param(handlers.handle_update_article, article_form, 'tax_rate', BAD_AMOUNTS,
                 lambda w: (303, where('/masterdata/articles', handlers.ARTICLE_NUMBER_ERROR, 'error')),
                 id='Artikel ändern: Steuersatz'),
]


@pytest.mark.parametrize('handler, build, field, bad_values, expected', NUMBER_FIELDS)
def test_an_invalid_number_gets_the_message_of_its_page_and_nothing_is_written(world, handler, build, field, bad_values,
                                                                              expected):
    before = snapshot(world.db)
    for bad in bad_values:
        post_data = build(world)
        post_data[field] = [bad]
        assert handler(world.db, post_data) == expected(world), (field, bad)
        assert snapshot(world.db) == before, (field, bad)
    assert_not_locked(world.db)


# ── gültige Eingaben verhalten sich wie bisher ───────────────────────────────
def columns(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql, args).fetchone()


ASSET_COLUMNS = 'SELECT PurchasePrice, UsefulLifeYears, COA_ID, AssetCategory_ID, DepreciationMethod FROM Assets WHERE ID = ?'


def test_empty_number_fields_keep_their_old_defaults(world):
    db = world.db
    # Nutzungsdauer leer = 1, Preis leer = 0 (wie ``or 1`` / ``or 0`` bisher): so, wie ein direkter Insert sie schreibt
    assert handlers.handle_add_asset(db, {**asset_add_form(world), 'purchase_price': [''], 'useful_life_years': ['']}) \
        == (303, '/assets')
    created = scalar(db, 'SELECT MAX(ID) FROM Assets')
    direct = db.insert_asset('Direkt', DAY, 0, 1)
    assert columns(db, ASSET_COLUMNS, created) == columns(db, ASSET_COLUMNS, direct)
    # Zählerstand leer = 0
    assert handlers.handle_add_number_range(db, {**range_add_form(world), 'year': ['2031'], 'current_number': ['']}) \
        == (303, '/masterdata/numberranges')
    assert scalar(db, 'SELECT CurrentNumber FROM NumberRanges WHERE Year = 2031') == 0
    # Privatanteil leer = 0, Nutzungsdauer der Kategorie leer = 1
    assert handlers.handle_add_skr(db, {**skr_add_form(world), 'private_share_percent': ['']}) \
        == (303, '/masterdata/skr?msg=Konto%20angelegt.&type=success')
    assert scalar(db, 'SELECT PrivateSharePercent FROM ChartOfAccounts WHERE ID = ?', 409901) == 0
    assert handlers.handle_add_asset_category(db, {**category_add_form(world), 'useful_life_years': ['']}) \
        == (303, '/asset_categories')
    assert scalar(db, 'SELECT UsefulLifeYears FROM AssetCategories WHERE Name = ?', 'Neue Kategorie') == 1


def test_valid_numbers_are_stored_as_before(world):
    db = world.db
    assert handlers.handle_add_number_range(db, {**range_add_form(world), 'year': ['2030'], 'current_number': ['12']}) \
        == (303, '/masterdata/numberranges')
    assert scalar(db, 'SELECT CurrentNumber FROM NumberRanges WHERE Year = 2030') == 12
    assert handlers.handle_update_number_range(
        db, {**range_update_form(world), 'year': ['2028'], 'current_number': ['7']}) == (303, '/masterdata/numberranges')
    assert scalar(db, 'SELECT Year FROM NumberRanges WHERE ID = ?', world.range) == 2028
    assert scalar(db, 'SELECT CurrentNumber FROM NumberRanges WHERE ID = ?', world.range) == 7
    assert handlers.handle_update_asset(
        db, {**asset_update_form(world), 'purchase_price': ['250.5'], 'useful_life_years': ['5'],
             'coa_id': [str(world.anlage)], 'asset_category_id': [str(world.category)]}) \
        == (303, f'/assets/edit?id={world.asset}')
    direct = db.insert_asset('Direkt', DAY, 250.5, 5, coa_id=world.anlage, asset_category_id=world.category)
    assert columns(db, ASSET_COLUMNS, world.asset) == columns(db, ASSET_COLUMNS, direct)
    assert handlers.handle_add_bankaccount(db, account_add_form(world)) == (303, '/masterdata/bankaccounts')
    assert scalar(db, 'SELECT SKRAccount FROM Accounts WHERE Name = ?', 'Zweites Konto') == 1810
    assert handlers.handle_add_bankaccount(db, {**account_add_form(world), 'name': ['Ohne SKR'], 'skr_account': ['']}) \
        == (303, '/masterdata/bankaccounts')
    assert scalar(db, 'SELECT SKRAccount FROM Accounts WHERE Name = ?', 'Ohne SKR') is None
    assert handlers.handle_update_receipt(db, receipt_form(world)) == (303, '/receipts')
    assert scalar(db, 'SELECT Number FROM Documents WHERE ID = ?', world.receipt) == 'B-200'
    assert handlers.handle_update_article(db, {**article_form(world), 'tax_rate': ['7'], 'unit_price': ['12.5']}) \
        == (303, '/masterdata/articles')
    article = db.get_article_by_id(world.article)
    assert (float(article[3]), article[4]) == (12.5, 7.0)


def test_a_valid_transaction_stores_tax_rate_and_amount_as_a_direct_insert_does(world):
    db = world.db
    assert handlers.handle_add_transaction(
        db, {**transaction_form(world), 'tax_rate': ['19'], 'tax_amount': ['-1.60'], 'text': ['über das Formular']}) \
        == (303, '/transactions')
    db.insert_booking(DAY, -10.0, tax_rate=0.19, tax_amount=-1.6, text='direkt')
    columns = 'Amount, TaxRate, TaxAmount'
    via_form = scalar(db, f"SELECT json_array({columns}) FROM Bookings WHERE Text = 'über das Formular'")
    direct = scalar(db, f"SELECT json_array({columns}) FROM Bookings WHERE Text = 'direkt'")
    assert via_form == direct
    # Steuerfelder leer: keine Steuer
    assert handlers.handle_add_transaction(db, {**transaction_form(world), 'text': ['ohne']}) == (303, '/transactions')
    assert scalar(db, "SELECT json_array(TaxRate, TaxAmount) FROM Bookings WHERE Text = 'ohne'") == '[null,null]'


def test_the_editor_of_the_bank_form_refuses_an_invalid_number_with_its_row(world):
    db = world.db
    db.insert_account('Girokonto', 'Inhaber', 'DE02200505501015871393', 'TESTDEFFXXX', 'Probebank', is_cash=0,
                      skr_account=1200)
    bank = scalar(db, 'SELECT ID FROM Accounts WHERE Name = ?', 'Girokonto')
    base = {**transaction_form(world), 'account': [str(bank)], 'split_id': ['', ''], 'split_text': ['a', 'b'],
            'split_counter_coa': ['', ''], 'split_amount': ['-4.00', '-6.00'], 'split_docnr': ['', ''],
            'split_coa': [str(world.expense), str(world.expense)], 'split_tax_rate': ['', ''], 'split_tax_amount': ['', '']}
    before = snapshot(db)
    for field, value, reason in [('split_amount', '1e999', 'Der Betrag ist keine gültige Zahl.'),
                                 ('split_amount', 'nan', 'Der Betrag ist keine gültige Zahl.'),
                                 ('split_tax_rate', 'x', 'Der Steuersatz ist keine gültige Zahl.'),
                                 ('split_tax_amount', '1,5', 'Der Steuerbetrag ist keine gültige Zahl.'),
                                 ('split_coa', '٣', 'Das Konto ist ungültig.'),
                                 ('split_coa', '0', 'Das Konto ist ungültig.'),
                                 ('split_counter_coa', 'abc', 'Das Gegenkonto ist ungültig.')]:
        post_data = {key: list(values) for key, values in base.items()}
        post_data[field][1] = value
        assert handlers.handle_add_transaction(db, post_data) == (400, f'Zeile 2: {reason}'), (field, value)
        assert snapshot(db) == before, (field, value)
    assert handlers.handle_add_transaction(db, {key: list(values) for key, values in base.items()}) \
        == (303, '/transactions')                                                  # Gegenprobe: ohne Fehler wird gebucht
    assert len(snapshot(db, 'Bookings')['Bookings']) == len(before['Bookings']) + 3


# ── kein Fehlertext in einer Antwort ─────────────────────────────────────────
def boom(*args, **kwargs):
    raise RuntimeError(SECRET)


FIXED_SENTENCES = [
    pytest.param(handlers.handle_add_transaction, transaction_form, 'insert_booking', 500,
                 'Die Buchung konnte nicht gespeichert werden. Einzelheiten stehen im Server-Log.',
                 id='Buchung speichern'),
    pytest.param(handlers.handle_link_document, link_form, 'link_booking_to_document', 500,
                 'Die Verknüpfung konnte nicht gespeichert werden. Einzelheiten stehen im Server-Log.',
                 id='Beleg verknüpfen'),
    pytest.param(handlers.handle_add_contact, contact_add_form, 'insert_contact', 500,
                 'Der Kontakt konnte nicht gespeichert werden. Einzelheiten stehen im Server-Log.', id='Kontakt anlegen'),
    pytest.param(handlers.handle_update_contact, contact_update_form, 'update_contact', 500,
                 'Der Kontakt konnte nicht gespeichert werden. Einzelheiten stehen im Server-Log.', id='Kontakt ändern'),
]


@pytest.mark.parametrize('handler, build, method, status, sentence', FIXED_SENTENCES)
def test_an_unexpected_error_is_a_fixed_sentence_and_the_text_stays_in_the_log(world, monkeypatch, capsys, handler, build,
                                                                              method, status, sentence):
    monkeypatch.setattr(Database, method, boom)
    result = handler(world.db, build(world))
    assert result == (status, sentence)
    assert_no_leak(result[1])
    assert SECRET in capsys.readouterr().err                              # der volle Fehler steht im Server-Log


def test_a_failing_import_is_a_fixed_sentence_in_the_json_answer(tmp_db, capsys):
    tmp_db.insert_account('Girokonto', 'Inhaber', IBAN, 'TESTDEFFXXX', 'Probebank', skr_account=1200)
    pending = os.path.join(userctx.user_data_dir(), 'pending_imports')
    os.makedirs(pending, exist_ok=True)
    import_id = 'pytest_' + uuid.uuid4().hex[:8]
    path = os.path.join(pending, f'{import_id}_test.json')
    transaction = {'date': DAY, 'amount': 'keine Zahl', 'reference': 'A-1', 'recipient': 'Partner',
                   'foreign_iban': 'DE00FOREIGN'}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'files': [{'filename': 'a.pdf', 'bank_code': 'VBR', 'iban': IBAN, 'document_date': DAY,
                              'transactions': [transaction]}], 'import_id': import_id}, f)
    try:
        status, body = handlers.handle_confirm_import(tmp_db, {'import_id': [import_id]})
    finally:
        if os.path.exists(path):
            os.remove(path)
    assert status == 500
    assert json.loads(body) == {'ok': False,
                                'error': 'Der Import ist fehlgeschlagen. Einzelheiten stehen im Server-Log.'}
    assert_no_leak(body)
    assert 'could not convert' not in body and 'keine Zahl' not in body
    assert 'could not convert' in capsys.readouterr().err                   # der volle Fehler steht im Server-Log


@pytest.mark.parametrize('failing, sentence', [
    ('insert_contact', 'Die Kontaktdaten konnten nicht gespeichert werden. Einzelheiten stehen im Server-Log.'),
    ('insert_account', 'Die Kontaktdaten sind gespeichert, aber das Bankkonto konnte nicht angelegt werden. '
                       'Einzelheiten stehen im Server-Log.'),
])
def test_the_first_setup_shows_a_fixed_sentence_when_saving_fails(tmp_db, monkeypatch, capsys, failing, sentence):
    monkeypatch.setattr(Database, failing, boom)
    status, page = handlers.handle_setup_save(tmp_db, form(company_name='Muster GmbH', iban=IBAN))
    assert status == 200
    assert sentence in html_lib.unescape(page) and SECRET not in page and 'geheimer' not in page
    assert SECRET in capsys.readouterr().err


# ── Kontakte: doppelte Nummer oder doppeltes Kürzel ──────────────────────────
CONTACT_TABLES = ('Contacts', 'ContactAddresses', 'CompanyDetails', 'PersonDetails', 'ContactTypeLinks', 'PersonRoles')
DUPLICATE_NUMBER = 'Die Kunden-/Kontaktnummer ist schon vergeben.'
DUPLICATE_ABBREVIATION = 'Das Kürzel ist schon vergeben.'
NOT_SAVED = 'Der Kontakt konnte nicht gespeichert werden.'


def test_the_database_layer_names_what_is_taken_and_leaves_nothing_behind(world):
    db = world.db
    before = snapshot(db, *CONTACT_TABLES)
    with pytest.raises(ContactRefused) as number:
        db.insert_contact(contact_type='customer', entity_type='company', company_name='Andere GmbH',
                          customer_number='K-1')
    with pytest.raises(ContactRefused) as abbreviation:
        db.insert_contact(contact_type='customer', entity_type='company', company_name='Andere GmbH', abbreviation='MUS')
    with pytest.raises(ContactRefused) as other:                          # ein Verweis ins Leere (die Firma gibt es nicht)
        db.insert_contact(contact_type='employee', entity_type='person', last_name='Probe', company_contact_id=99999)
    assert [str(e.value) for e in (number, abbreviation, other)] == [DUPLICATE_NUMBER, DUPLICATE_ABBREVIATION, NOT_SAVED]
    for refused in (number, abbreviation, other):
        assert isinstance(refused.value, ValueError) and isinstance(refused.value.__cause__, sqlite3.IntegrityError)
        assert_no_leak(str(refused.value))
    assert snapshot(db, *CONTACT_TABLES) == before
    assert_not_locked(db)


def test_updating_a_contact_to_a_taken_number_or_abbreviation_is_refused_with_a_message_and_changes_nothing(world):
    db = world.db
    other = db.insert_contact(contact_type='customer', entity_type='company', company_name='Zweite GmbH',
                              customer_number='K-2', abbreviation='ZWE')
    before = snapshot(db, *CONTACT_TABLES)
    with pytest.raises(ContactRefused, match=DUPLICATE_NUMBER):
        db.update_contact(other, entity_type='company', company_name='Zweite GmbH', customer_number='K-1',
                          abbreviation='ZWE')
    with pytest.raises(ContactRefused, match=DUPLICATE_ABBREVIATION):
        db.update_contact(other, entity_type='company', company_name='Zweite GmbH', customer_number='K-2',
                          abbreviation='MUS')
    assert snapshot(db, *CONTACT_TABLES) == before
    assert_not_locked(db)


def test_the_handlers_answer_a_taken_customer_number_with_the_message_on_the_contact_page(world):
    db = world.db
    other = db.insert_contact(contact_type='customer', entity_type='company', company_name='Zweite GmbH',
                              customer_number='K-2')
    before = snapshot(db, *CONTACT_TABLES)
    added = handlers.handle_add_contact(db, {**contact_add_form(world), 'customer_number': ['K-1']})
    assert added == (303, f'/masterdata/contacts?error={q(DUPLICATE_NUMBER)}')
    changed = handlers.handle_update_contact(db, {**contact_update_form(world), 'contact_id': [str(other)],
                                                  'customer_number': ['K-1']})
    assert changed == (303, f'/masterdata/contacts/edit?id={other}&error={q(DUPLICATE_NUMBER)}')
    assert snapshot(db, *CONTACT_TABLES) == before
    # Gegenprobe: eine freie Nummer wird gespeichert
    assert handlers.handle_add_contact(db, {**contact_add_form(world), 'customer_number': ['K-3']}) \
        == (303, '/masterdata/contacts')
    assert scalar(db, "SELECT COUNT(*) FROM Contacts WHERE CustomerNumber = 'K-3'") == 1


def test_a_taken_abbreviation_that_slips_past_the_precheck_is_a_message_too(world, monkeypatch):
    """Die Vorprüfung der Handler (``check_abbreviation_unique``) fängt es meist ab; wer ihr zuvorkommt (zwei Anfragen zugleich),
    bekommt dieselbe Art Meldung statt eines 500."""
    db = world.db
    other = db.insert_contact(contact_type='customer', entity_type='company', company_name='Zweite GmbH',
                              customer_number='K-2', abbreviation='ZWE')
    monkeypatch.setattr(Database, 'check_abbreviation_unique', lambda self, abbreviation, exclude_id=None: (True, abbreviation))
    before = snapshot(db, *CONTACT_TABLES)
    assert handlers.handle_add_contact(db, {**contact_add_form(world), 'abbreviation': ['MUS']}) \
        == (303, f'/masterdata/contacts?error={q(DUPLICATE_ABBREVIATION)}')
    assert handlers.handle_update_contact(db, {**contact_update_form(world), 'contact_id': [str(other)],
                                               'customer_number': ['K-2'], 'abbreviation': ['MUS']}) \
        == (303, f'/masterdata/contacts/edit?id={other}&error={q(DUPLICATE_ABBREVIATION)}')
    assert snapshot(db, *CONTACT_TABLES) == before


def test_a_company_that_does_not_exist_is_a_message_for_a_person(world):
    before = snapshot(world.db, *CONTACT_TABLES)
    result = handlers.handle_add_contact(world.db, form(entity_type='person', contact_type='employee', last_name='Probe',
                                                        company_contact_id=99999))
    assert result == (303, f'/masterdata/contacts?error={q(NOT_SAVED)}')
    assert snapshot(world.db, *CONTACT_TABLES) == before


# ── Artikel: Einheit und Steuersatz außerhalb der festen Auswahl bleiben ────
def select_options(html, name):
    """``[(Wert, vorgewählt, Beschriftung)]`` der Auswahl ``name`` – gelesen, wie der Browser sie liest."""
    block = re.search(rf'<select name="{name}">(.*?)</select>', html, re.S).group(1)
    return [(html_lib.unescape(value), bool(selected), html_lib.unescape(label))
            for value, selected, label in re.findall(r'<option value="([^"]*)"\s*(selected)?>(.*?)</option>', block, re.S)]


def submit_unchanged(db, article_id):
    """Den Artikel im Formular öffnen und mit dem speichern, was der Browser dann schickt (die vorgewählten Optionen)."""
    page = PageArticleEdit(db, article_id)
    unit = [value for value, selected, _label in select_options(page, 'unit') if selected]
    tax = [value for value, selected, _label in select_options(page, 'tax_rate') if selected]
    assert len(unit) == 1 and len(tax) == 1                               # genau eine Option ist vorgewählt
    row = db.get_article_by_id(article_id)
    return handlers.handle_update_article(db, {'id': [str(article_id)], 'name': [row[1]], 'unit': unit,
                                               'unit_price': [f'{row[3]:.2f}'], 'tax_rate': tax,
                                               'description': [row[5] or ''], 'active': ['1']})


@pytest.mark.parametrize('unit, tax_rate', [('km', 16), ('Std', 5.5), ('m³', 0.19), ('Stk.', 16), ('km', 19), ('kg', 7)])
def test_an_article_with_another_unit_or_tax_rate_keeps_them_when_it_is_opened_and_saved_unchanged(tmp_db, unit, tax_rate):
    tmp_db.insert_article(name='Importartikel', unit=unit, unit_price=12.5, tax_rate=tax_rate, description='Beschreibung')
    article_id = scalar(tmp_db, 'SELECT ID FROM Articles WHERE Name = ?', 'Importartikel')
    before = snapshot(tmp_db, 'Articles')

    assert submit_unchanged(tmp_db, article_id) == (303, '/masterdata/articles')

    assert snapshot(tmp_db, 'Articles') == before
    row = tmp_db.get_article_by_id(article_id)
    assert (row[2], row[4]) == (unit, float(tax_rate))


def test_the_form_offers_the_stored_value_as_an_additional_preselected_option(tmp_db):
    tmp_db.insert_article(name='Importartikel', unit='km', unit_price=1, tax_rate=16)
    page = PageArticleEdit(tmp_db, scalar(tmp_db, 'SELECT ID FROM Articles'))
    units, taxes = select_options(page, 'unit'), select_options(page, 'tax_rate')
    assert [v for v, _s, _l in units] == ['Stk.', 'Std.', 'kg', 'g', 'm', 'm²', 'l', 'Psch.', 'km']
    assert [(v, s) for v, s, _l in units if s] == [('km', True)]
    assert [(v, s, label) for v, s, label in taxes] == [('19', False, '19%'), ('7', False, '7%'), ('0', False, '0%'),
                                                       ('16.0', True, '16%')]


def test_the_form_of_an_article_inside_the_fixed_choice_offers_no_extra_option(tmp_db):
    tmp_db.insert_article(name='Normalartikel', unit='kg', unit_price=1, tax_rate=7)
    new, edit = PageArticles(tmp_db), PageArticleEdit(tmp_db, scalar(tmp_db, 'SELECT ID FROM Articles'))
    assert len(select_options(new, 'unit')) == len(select_options(edit, 'unit')) == 8
    assert len(select_options(new, 'tax_rate')) == len(select_options(edit, 'tax_rate')) == 3
    assert [(v, s) for v, s, _l in select_options(edit, 'unit') if s] == [('kg', True)]
    assert [(v, s) for v, s, _l in select_options(edit, 'tax_rate') if s] == [('7', True)]
    assert [(v, s) for v, s, _l in select_options(new, 'unit') if s] == [('Stk.', True)]     # ein neuer Artikel: wie bisher
    assert [(v, s) for v, s, _l in select_options(new, 'tax_rate') if s] == [('19', True)]


def test_an_article_without_a_tax_rate_shows_19_percent_like_the_list_instead_of_failing(tmp_db):
    tmp_db.insert_article(name='Ohne Satz', unit='Stk.', unit_price=1, tax_rate=19)
    with contextlib.closing(sqlite3.connect(tmp_db.db_name)) as con:         # so legt es nur die SQL-Konsole an
        con.execute('UPDATE Articles SET TaxRate = NULL')
        con.commit()
    page = PageArticleEdit(tmp_db, scalar(tmp_db, 'SELECT ID FROM Articles'))
    assert [(v, s) for v, s, _l in select_options(page, 'tax_rate') if s] == [('19', True)]


def test_a_hostile_stored_unit_is_escaped_in_the_option(tmp_db):
    hostile = '"><img src=x onerror=alert(1)>'
    tmp_db.insert_article(name='Feind', unit=hostile, unit_price=1, tax_rate=19)
    page = PageArticleEdit(tmp_db, scalar(tmp_db, 'SELECT ID FROM Articles'))
    assert '<img src=x' not in page and hostile not in page
    assert Dom(page).all('img') == []
    assert (hostile, True) in [(v, s) for v, s, _l in select_options(page, 'unit')]
    assert submit_unchanged(tmp_db, scalar(tmp_db, 'SELECT ID FROM Articles')) == (303, '/masterdata/articles')
    assert scalar(tmp_db, 'SELECT Unit FROM Articles') == hostile


# ── Beleg löschen über die ID ────────────────────────────────────────────────
def test_deleting_a_receipt_takes_exactly_one_even_if_another_has_the_same_number(tmp_db):
    tmp_db.insert_receipt('B-100', DAY, 'a.pdf', 'p/a.pdf', 'erster')
    tmp_db.insert_receipt('B-100', DAY, 'b.pdf', 'p/b.pdf', 'zweiter')
    first, second = [row[0] for row in tmp_db.fetch_receipts()]
    tmp_db.delete_receipt(first)
    assert [row[0] for row in tmp_db.fetch_receipts()] == [second]
    tmp_db.delete_receipt(first)                                          # schon weg: kein Fehler, nichts gelöscht
    tmp_db.delete_receipt('B-100')                                        # eine Nummer ist keine ID: trifft nichts
    assert [row[0] for row in tmp_db.fetch_receipts()] == [second]


@pytest.fixture
def books(auto_login):
    return make_books(auto_login)


def submit(port, books, path, fields=None):
    return post(port, path, headers={'Origin': own_origin(port)}, form=fields or {}, csrf=books.csrf)


def test_the_refusal_of_a_referenced_receipt_applies_only_to_that_one(server_port, books):
    db = books.db
    db.insert_receipt('B-100', DAY, 'a.pdf', 'p/a.pdf', 'erster')
    db.insert_receipt('B-100', DAY, 'b.pdf', 'p/b.pdf', 'zweiter')
    first, second = [row[0] for row in db.fetch_receipts()]
    db.link_booking_to_document(db.insert_booking(DAY, 10), first)
    before = snapshot(db)

    refused = submit(server_port, books, f'/receipts/delete?id={first}')

    assert refused.status == 303
    location = urllib.parse.urlsplit(refused.headers['Location'])
    assert location.path == '/receipts'
    assert urllib.parse.parse_qs(location.query)['err'] == [
        'Der Beleg lässt sich nicht löschen, solange andere Daten auf ihn verweisen: 1 Buchung.']
    assert snapshot(db) == before                                         # auch der zweite Beleg ist noch da

    deleted = submit(server_port, books, f'/receipts/delete?id={second}')

    assert deleted.status == 303 and deleted.headers['Location'] == '/receipts'
    assert [row[0] for row in db.fetch_receipts()] == [first]


def test_the_delete_route_for_receipts_no_longer_takes_a_number(server_port, books):
    books.db.insert_receipt('B-100', DAY, 'a.pdf', 'p/a.pdf', 'erster')
    before = snapshot(books.db)
    resp = submit(server_port, books, '/receipts/delete?number=B-100')
    assert resp.status == 400 and resp.text == 'Ungültige oder fehlende ID.'
    assert snapshot(books.db) == before


# ── link_booking_to_document ─────────────────────────────────────────────────
def test_an_existing_link_is_silent_and_a_link_into_the_void_is_a_message(world):
    db = world.db
    db.link_booking_to_document(world.booking, world.receipt, 'receipt')
    after_first = snapshot(db, 'BookingDocuments')
    db.link_booking_to_document(world.booking, world.receipt, 'invoice')    # gibt es schon: still, nichts ändert sich
    assert snapshot(db, 'BookingDocuments') == after_first and len(after_first['BookingDocuments']) == 1
    for booking, receipt in [(99999, world.receipt), (world.booking, 99999), (99999, 99999)]:
        with pytest.raises(ValueError, match=r'^Buchung oder Beleg gibt es nicht \(mehr\)\.$') as info:
            db.link_booking_to_document(booking, receipt, 'receipt')
        assert isinstance(info.value.__cause__, sqlite3.IntegrityError)
    assert snapshot(db, 'BookingDocuments') == after_first
    assert_not_locked(db)


def test_any_other_integrity_error_of_the_link_is_not_swallowed(world):
    with pytest.raises(sqlite3.IntegrityError, match='NOT NULL'):
        world.db.link_booking_to_document(None, world.receipt, 'receipt')


def test_the_link_handler_answers_400_with_the_message_and_a_repeated_link_with_the_redirect(world):
    db = world.db
    ok = handlers.handle_link_document(db, link_form(world))
    again = handlers.handle_link_document(db, link_form(world))
    assert ok == again == (303, f'/transactions/edit?id={world.booking}')
    assert scalar(db, 'SELECT COUNT(*) FROM BookingDocuments') == 1
    before = snapshot(db)
    assert handlers.handle_link_document(db, {**link_form(world), 'booking_id': ['99999']}) \
        == (400, 'Buchung oder Beleg gibt es nicht (mehr).')
    assert handlers.handle_link_document(db, {**link_form(world), 'document_id': ['99999']}) \
        == (400, 'Buchung oder Beleg gibt es nicht (mehr).')
    assert snapshot(db) == before


# ── über HTTP: Klartext im Body, nie im Location-Kopf ────────────────────────
REFUSED_OVER_HTTP = [
    pytest.param('/update_receipt', dict(id='abc', number='B-1', date=DAY, filename='a', path='b', info='c'), INVALID),
    pytest.param('/documents/link', dict(booking_id='abc', document_id='1'), INVALID),
    pytest.param('/documents/link', dict(booking_id='99999', document_id='99999'),
                 'Buchung oder Beleg gibt es nicht (mehr).', id='/documents/link ins Leere'),
    pytest.param('/masterdata/skr/update', dict(id='abc', name='X', group='G'), INVALID),
    pytest.param('/update_skr', dict(id='abc', name='X', group='G'), INVALID),
    pytest.param('/masterdata/contacts/update', dict(contact_id='abc'), INVALID),
    pytest.param('/update_contact', dict(contact_id='abc'), INVALID),
    pytest.param('/masterdata/contacts/add', dict(company_name='X GmbH', company_contact_id='abc'), INVALID),
    pytest.param('/masterdata/articles/update', dict(id='abc', name='X'), INVALID),
    pytest.param('/articles/update', dict(id='abc', name='X'), INVALID),
    pytest.param('/masterdata/bankaccounts/update', dict(id='abc', name='X'), INVALID),
    pytest.param('/masterdata/numberranges/update', dict(id='abc'), INVALID),
    pytest.param('/assets/update', dict(asset_id='abc', name='X'), INVALID),
    pytest.param('/assets/depreciate', dict(asset_id='abc', year='2026', coa_id='1'), INVALID),
    pytest.param('/assets/sell', dict(asset_id='abc', sale_date=DAY), INVALID),
    pytest.param('/asset_categories/update', dict(category_id='abc', name='X'), INVALID),
    pytest.param('/assets/add', dict(name='X', purchase_price='abc'), handlers.AMOUNT_ERROR, id='/assets/add Preis'),
    pytest.param('/assets/add', dict(name='X', useful_life_years='٣'), handlers.USEFUL_LIFE_ERROR,
                 id='/assets/add Nutzungsdauer'),
    pytest.param('/asset_categories/add', dict(name='X', coa_id='abc'), INVALID, id='/asset_categories/add'),
    pytest.param('/transactions/add', dict(transaction_id='0', date=DAY, amount='1', account='abc'), INVALID),
    pytest.param('/transactions/add', dict(transaction_id='0', date=DAY, amount='1', tax_rate='nan'),
                 'Steuersatz und Steuerbetrag müssen Zahlen sein.', id='/transactions/add Steuersatz'),
]


@pytest.mark.parametrize('path, fields, text', REFUSED_OVER_HTTP)
def test_a_refusal_is_plain_text_in_the_body_and_never_a_location(server_port, books, path, fields, text):
    before = snapshot(books.db)
    resp = submit(server_port, books, path, fields)
    assert resp.status == 400, resp.text
    assert resp.text == text
    assert resp.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert 'Location' not in resp.headers                                 # sonst bliebe die Seite leer
    assert snapshot(books.db) == before


MESSAGE_ROUTES = [
    pytest.param('/masterdata/numberranges/add', dict(type='invoice', year='abc', letter='R', number_format='{yy}{l}{nnn}'),
                 '/masterdata/numberranges', 'err', handlers.YEAR_ERROR, id='Nummernkreis'),
    pytest.param('/masterdata/bankaccounts/add', dict(name='X', skr_account='abc'),
                 '/masterdata/bankaccounts', 'error', handlers.SKR_ACCOUNT_ERROR, id='Bankkonto (die Weiterleitung war fest)'),
    pytest.param('/asset_categories/add', dict(name='X', useful_life_years='0'),
                 '/asset_categories', 'err', handlers.USEFUL_LIFE_ERROR, id='AfA-Kategorie'),
    pytest.param('/masterdata/skr/add', dict(name='X', group='G', framework_nr='4', account='9901',
                                             private_share_percent='abc'),
                 '/masterdata/skr', 'msg', handlers.PERCENT_ERROR, id='Konto des Kontenrahmens'),
]


@pytest.mark.parametrize('path, fields, target, param, message', MESSAGE_ROUTES)
def test_a_message_of_the_page_arrives_as_a_redirect_and_is_shown(server_port, books, path, fields, target, param, message):
    before = snapshot(books.db)
    resp = submit(server_port, books, path, fields)
    assert resp.status == 303
    location = urllib.parse.urlsplit(resp.headers['Location'])
    assert location.path == target and urllib.parse.parse_qs(location.query)[param] == [message]
    assert snapshot(books.db) == before
    page = get(server_port, resp.headers['Location'])
    assert page.status == 200
    if param != 'msg':                                                    # die Seite der Konten zeigt ``msg`` auf eigene Art
        assert shown_message(page.text) == [message, 'error']


def test_a_taken_customer_number_is_a_message_on_the_contact_page_over_http(server_port, books):
    before = snapshot(books.db)
    resp = submit(server_port, books, '/masterdata/contacts/add',
                  dict(company_name='Zweite GmbH', customer_number='K-100', entity_type='company', contact_type='customer'))
    assert resp.status == 303
    assert resp.headers['Location'] == f'/masterdata/contacts?error={q(DUPLICATE_NUMBER)}'
    assert snapshot(books.db) == before
    assert shown_message(get(server_port, resp.headers['Location']).text) == [DUPLICATE_NUMBER, 'error']
    resp = submit(server_port, books, '/masterdata/contacts/update',
                  dict(contact_id=str(books.own), company_name='Eigene GmbH', customer_number='K-100',
                       entity_type='company', contact_type='own'))
    assert resp.status == 303
    assert resp.headers['Location'] == f'/masterdata/contacts/edit?id={books.own}&error={q(DUPLICATE_NUMBER)}'
    assert snapshot(books.db) == before
    assert shown_message(get(server_port, resp.headers['Location']).text) == [DUPLICATE_NUMBER, 'error']


UNEXPECTED_OVER_HTTP = [
    pytest.param('/transactions/add', dict(transaction_id='0', date=DAY, amount='1'), 'insert_booking',
                 'Die Buchung konnte nicht gespeichert werden. Einzelheiten stehen im Server-Log.', id='Buchung'),
    pytest.param('/masterdata/contacts/add', dict(company_name='X GmbH'), 'insert_contact',
                 'Der Kontakt konnte nicht gespeichert werden. Einzelheiten stehen im Server-Log.', id='Kontakt anlegen'),
    pytest.param('/documents/link', dict(booking_id='1', document_id='1'), 'link_booking_to_document',
                 'Die Verknüpfung konnte nicht gespeichert werden. Einzelheiten stehen im Server-Log.', id='Verknüpfung'),
]


@pytest.mark.parametrize('path, fields, method, sentence', UNEXPECTED_OVER_HTTP)
def test_an_unexpected_error_is_a_500_with_a_fixed_sentence_in_the_body(server_port, books, monkeypatch, capsys, path,
                                                                       fields, method, sentence):
    monkeypatch.setattr(Database, method, boom)
    resp = submit(server_port, books, path, fields)
    assert resp.status == 500
    assert resp.text == sentence
    assert resp.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert 'Location' not in resp.headers
    assert_no_leak(resp.text)
    assert SECRET in capsys.readouterr().err                              # der Traceback steht im Server-Log


# ── Wächter gegen Rückfälle (statisch) ───────────────────────────────────────
def unprotected_conversions(source):
    """Zeilen, in denen ``int(…)``/``float(…)`` einen Wert aus ``post_data`` umwandelt (auch über ``[0]``, ``.get`` und ``or``)."""
    tree = ast.parse(source)
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('int', 'float')
            and any(isinstance(inner, ast.Name) and inner.id == 'post_data' for arg in node.args for inner in ast.walk(arg))]


def exception_text_with_5xx(source):
    """Zeilen, in denen ein ``except … as e`` einen 5xx zurückgibt, dessen Antwort die Ausnahme ``e`` nennt (``{e}``,
    ``str(e)``, auch in ``json.dumps``)."""
    tree = ast.parse(source)
    found = []
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name):
        for ret in (n for n in ast.walk(handler) if isinstance(n, ast.Return)):
            value = ret.value
            if not (isinstance(value, ast.Tuple) and value.elts and isinstance(value.elts[0], ast.Constant)
                    and isinstance(value.elts[0].value, int) and 500 <= value.elts[0].value <= 599):
                continue
            if any(isinstance(inner, ast.Name) and inner.id == handler.name
                   for part in value.elts[1:] for inner in ast.walk(part)):
                found.append(ret.lineno)
    return found


def _is_broad_except(node):
    """Fängt dieser Typ (die Angabe hinter ``except``) jede Ausnahme? Ohne Typ, ``Exception`` oder ``BaseException`` – auch als Teil
    eines Tupels."""
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in ('Exception', 'BaseException')
    if isinstance(node, ast.Tuple):
        return any(_is_broad_except(item) for item in node.elts)
    return False


def exception_text_in_redirect(source):
    """Zeilen, in denen ein breites ``except … as e`` (``Exception``/``BaseException``) eine Weiterleitung (3xx) zurückgibt, die die
    Ausnahme ``e`` nennt (``str(e)``, ``{e}``): So gelangte ihr Text in die Adresse und damit auf die Seite (Pfade, Fehler von
    SQLite oder 7-Zip). Eine Ablehnung mit eigener Klasse (``except _Refused as e``) ist gewollt und zählt nicht."""
    tree = ast.parse(source)
    found = []
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name and _is_broad_except(n.type)):
        for ret in (n for n in ast.walk(handler) if isinstance(n, ast.Return)):
            value = ret.value
            if not (isinstance(value, ast.Tuple) and value.elts and isinstance(value.elts[0], ast.Constant)
                    and isinstance(value.elts[0].value, int) and 300 <= value.elts[0].value <= 399):
                continue
            if any(isinstance(inner, ast.Name) and inner.id == handler.name
                   for part in value.elts[1:] for inner in ast.walk(part)):
                found.append(ret.lineno)
    return found


def _is_log_call(func):
    """``print(…)`` oder ``traceback.<Name>(…)``: Was dort steht, geht ins Server-Log und in keine Antwort."""
    if isinstance(func, ast.Name):
        return func.id == 'print'
    return isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == 'traceback'


def exception_name_used_in_broad_except(source):
    """Zeilen, in denen ein breites ``except … as e`` (``Exception``/``BaseException``) seine Ausnahme ``e`` benutzt – außer in
    ``raise … from e`` (die Ursache hängt an der Kette, nicht im Text) und als Argument von ``print(…)``/``traceback.…`` (das
    Server-Log). Alles andere (``str(e)``, ``{e}``, ``e.args``, ``wert = e``) kann den Text einer Systemausnahme – mit
    Serverpfaden – in eine Antwort tragen; dort steht ein fester Satz oder der Klartext einer eigenen Klasse
    (``except CamtError as e`` ist kein breiter Fang und zählt nicht)."""
    tree = ast.parse(source)
    found = []
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name and _is_broad_except(n.type)):
        allowed = set()
        for node in ast.walk(handler):
            if isinstance(node, ast.Raise) and node.cause is not None:
                allowed.update(id(inner) for inner in ast.walk(node.cause))
            elif isinstance(node, ast.Call) and _is_log_call(node.func):
                arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
                allowed.update(id(inner) for argument in arguments for inner in ast.walk(argument))
        found += [n.lineno for n in ast.walk(handler)
                  if isinstance(n, ast.Name) and n.id == handler.name and id(n) not in allowed]
    return sorted(found)


def handlers_source():
    with open(handlers.__file__, encoding='utf-8') as f:
        return f.read()


def upload_handler_source():
    with open(upload_handler.__file__, encoding='utf-8') as f:
        return f.read()


def test_handlers_py_converts_no_form_value_without_the_helpers():
    assert unprotected_conversions(handlers_source()) == []


def test_handlers_py_returns_no_exception_text_with_a_5xx_status():
    assert exception_text_with_5xx(handlers_source()) == []


def test_handlers_py_puts_no_exception_text_into_a_redirect():
    assert exception_text_in_redirect(handlers_source()) == []


def test_upload_handler_py_uses_no_exception_in_a_broad_except():
    """Die Antwort des Beleg-Uploads nennt je Datei den Klartext einer eigenen Klasse oder einen festen Satz – nie ``str(e)``
    einer Systemausnahme (ihr Text trägt Serverpfade)."""
    assert exception_name_used_in_broad_except(upload_handler_source()) == []


def test_the_guard_covers_the_receipt_upload_of_the_receipts_page():
    """Seit 2.5.11 legt ``handle_receipt_upload`` (``POST /receipts/upload``) jede Datei als Beleg ab: Der Wächter oben liest dieselbe
    Datei – steht der Beleg-Upload dort nicht mehr, deckt er ihn nicht mehr."""
    tree = ast.parse(upload_handler_source())
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert {'handle_file_upload', 'handle_receipt_upload', '_store_receipt'} <= defined


def test_receipt_files_py_uses_no_exception_in_a_broad_except():
    """``server/receipt_files.py`` (Ablage, Datum, Pfadprüfung, Löschen der Belegdateien): Ein breites ``except … as e`` nennt seine
    Ausnahme höchstens im Server-Log – nie in einem Rückgabewert, der in eine Antwort geht (der Text trägt Serverpfade)."""
    with open(receipt_files.__file__, encoding='utf-8') as f:
        assert exception_name_used_in_broad_except(f.read()) == []


def test_the_guards_find_what_they_are_meant_to_find():
    bad = '''
def a(post_data):
    x = int(post_data["id"][0])
    y = float(post_data.get("p", ["0"])[0] or 0)
    z = int(post_data.get("n", [0])[0] or 0)
    ok = _form_int(post_data.get("n", [""])[0], 0, 9)
    other = int(some_text)

def b():
    try:
        pass
    except Exception as e:
        return 500, f"Fehler: {str(e)}"

def c():
    try:
        pass
    except Exception as e:
        return 500, json.dumps({"error": f"Fehler: {e}"})

def d():
    try:
        pass
    except ValueError as exc:
        return 400, str(exc)
    except Exception:
        return 500, "fester Satz"
'''
    assert unprotected_conversions(bad) == [3, 4, 5]
    assert exception_text_with_5xx(bad) == [13, 19]


def test_the_redirect_guard_finds_what_it_is_meant_to_find():
    bad = '''
def a():
    try:
        pass
    except Exception as e:
        return 303, f"/miscellaneous?msg={quote(str(e))}"

def b():
    try:
        pass
    except (ValueError, Exception) as exc:
        return 303, "/x?msg=" + str(exc)

def c():
    try:
        pass
    except BaseException as e:
        return 302, f"/x?err={e}"

def d():
    try:
        pass
    except _Refused as e:
        return 303, f"/x?err={quote(str(e), safe='')}"

def e():
    try:
        pass
    except Exception:
        return 303, "/x?err=fest"

def f():
    try:
        pass
    except Exception as e:
        print(e)
        return 303, "/x?err=fest"

def g():
    try:
        pass
    except Exception as e:
        return 400, str(e)
'''
    assert exception_text_in_redirect(bad) == [6, 12, 18]


def test_the_exception_name_guard_finds_what_it_is_meant_to_find():
    bad = '''
def a():
    try:
        pass
    except Exception as e:
        return {"error": str(e)}

def b():
    try:
        pass
    except (ValueError, Exception) as exc:
        items.append({"error": f"{exc}"})

def c():
    try:
        pass
    except BaseException as e:
        raise RuntimeError("fest") from e

def d():
    try:
        pass
    except Exception as e:
        traceback.print_exc()
        print(e)
        print(f"Fehler: {e}", file=sys.stderr)
        log.append(traceback.format_exception(e))
        return {"error": FIXED}

def e():
    try:
        pass
    except ValueError as e:
        return {"error": str(e)}

def f():
    try:
        pass
    except Exception:
        return {"error": FIXED}

def g():
    try:
        pass
    except Exception as e:
        result = e

def h():
    try:
        pass
    except Exception as e:
        raise RuntimeError(str(e)) from e

def i():
    try:
        pass
    except Exception as e:
        return {"error": e.args}
'''
    assert exception_name_used_in_broad_except(bad) == [6, 12, 46, 52, 58]
    assert exception_name_used_in_broad_except('def j():\n    return 1\n') == []
