# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Ausgabe-Maskierung der Seiten (2.0.7); Ergänzung zu test_pages_xss.py und test_script_context_pages.py.

Jeder Wert aus Datenbank oder Anfrage wird beim Einsetzen maskiert: in HTML-Text und -Attributen mit
``html.escape``, in einem ``<script>``-Block über ``pages.script_json``. Die Seiten werden gelesen wie im
Browser (``tests/dom_helpers.py``): Ein feindlicher Wert darf den Aufbau der Seite nicht ändern (keine
zusätzlichen Elemente, kein Ausbruch aus einem Attribut), und er steht vollständig – als Wert, nicht als
Markup – an seiner Stelle.

Geprüft werden die Kontaktseiten, Rechnungsformular und -liste samt Suche, Angebotsformular, Artikel
(Seite, ``?error=`` und das Ablehnen von ``nan``/``inf`` beim Speichern), Nummernkreise und das Dashboard.
Alle Werte sind erfundene Testdaten.
"""
import html as html_lib
import json
import re
import sqlite3
import urllib.parse

import pytest

from server.pages_contacts import ENTITY_TYPES, PageContactEdit, PageContactNew, PageContacts, _entity_type
from server.pages_dashboard import PageDashboard
from server.pages_invoice import PageInvoice
from server.pages_masterdata import PageArticles, PageNumberRanges
from server.pages_quote import PageQuote
from tests.document_helpers import add_contacts, add_document, table_rows
from tests.dom_helpers import Dom, app_msg_arguments, assert_same_structure, data_block
from tests.http_client import get, post

SCRIPT = '<script>alert(1)</script>'
IMG = '<img src=x onerror=alert(1)>'
BREAK = '"><img src=x onerror=alert(1)>'
BREAK_SCRIPT = '"><script>alert(1)</script>'


def escaped(value):
    return html_lib.escape(value, quote=True)


def encode(**params):
    return urllib.parse.urlencode(params)


def sql(db, statement, *args):
    con = sqlite3.connect(db.db_name)
    try:
        con.execute(statement, args)
        con.commit()
    finally:
        con.close()


def inputs(html, **attrs):
    """Alle ``<input>``-Elemente mit diesen Attributwerten (Entitäten aufgelöst)."""
    return [a for a in Dom(html).all('input') if all(a.get(k) == v for k, v in attrs.items())]


def articles_message(html):
    """Meldung und Art aus dem ``appMsg(…)``-Aufruf der Artikelseite (die Meldung steht als JSON-Zeichenkette)."""
    found = re.search(r"appMsg\((\"(?:[^\"\\]|\\.)*\"),'(\w+)'\)", ''.join(Dom(html).scripts))
    assert found, 'kein appMsg-Aufruf in der Seite'
    return [json.loads(found.group(1)), found.group(2)]


# ── Kontaktseiten ────────────────────────────────────────────────────────────
def test_contact_error_message_is_data_in_the_script_block_not_markup(tmp_db):
    hostile = PageContacts(tmp_db, error_msg=SCRIPT)
    assert_same_structure(hostile, PageContacts(tmp_db, error_msg='Normale Meldung'))
    assert SCRIPT not in hostile and '<script>alert(1)' not in hostile
    assert app_msg_arguments(Dom(hostile)) == [SCRIPT, 'error']       # die Meldung kommt unverändert an


@pytest.mark.parametrize('path', ['/masterdata/contacts', '/masterdata/contacts/new'])
def test_contact_error_parameter_over_http(server_port, auto_login, path):
    benign = get(server_port, f'{path}?error=Normale+Meldung')
    hostile = get(server_port, f'{path}?{encode(error=SCRIPT)}')
    assert hostile.status == 200
    assert_same_structure(hostile.text, benign.text)
    assert SCRIPT not in hostile.text
    assert app_msg_arguments(Dom(hostile.text)) == [SCRIPT, 'error']


def test_contact_error_parameter_on_the_edit_page_over_http(server_port, auto_login):
    cid = auto_login.db.insert_contact(contact_type='customer', display_name='Probefirma', company_name='Probefirma')
    hostile = get(server_port, f'/masterdata/contacts/edit?{encode(id=cid, error=SCRIPT)}')
    assert hostile.status == 200 and SCRIPT not in hostile.text
    assert app_msg_arguments(Dom(hostile.text)) == [SCRIPT, 'error']


HOSTILE_ENTITY = "company';alert(1);//"


def test_entity_type_falls_back_to_an_allowed_value():
    assert ENTITY_TYPES == ('company', 'person')
    assert _entity_type('company') == 'company' and _entity_type('person') == 'person'
    assert _entity_type(HOSTILE_ENTITY) == 'company'
    for value in (None, '', 'Person', 'PERSON', ' person', 'person ', 'x', 5, ['person']):
        assert _entity_type(value) == 'company', repr(value)
    assert _entity_type(HOSTILE_ENTITY, default='person') == 'person'


@pytest.mark.parametrize('entity, expected', [
    pytest.param(HOSTILE_ENTITY, 'company', id='Ausbruch-aus-der-Zeichenkette'),
    pytest.param("person';alert(1);//", 'company', id='Person-mit-Anhang'),
    pytest.param('person', 'person', id='Person'),
    pytest.param('company', 'company', id='Firma'),
    pytest.param('PERSON', 'company', id='Schreibweise'),
    pytest.param('', 'company', id='leer'),
])
def test_new_contact_form_takes_only_an_allowed_entity_type(tmp_db, entity, expected):
    html = PageContactNew(tmp_db, entity_type=entity)
    assert [a['value'] for a in inputs(html, name='entity_type')] == [expected]
    assert f"const et = '{expected}';" in html
    assert "alert(1)" not in html and "';alert" not in html


def test_new_contact_form_over_http_ignores_a_hostile_entity_type(server_port, auto_login):
    resp = get(server_port, f'/masterdata/contacts/new?{encode(entity=HOSTILE_ENTITY)}')
    assert resp.status == 200 and 'alert(1)' not in resp.text
    assert [a['value'] for a in inputs(resp.text, name='entity_type')] == ['company']


def test_contact_list_filter_with_a_hostile_entity_does_not_change_the_page(server_port, auto_login):
    auto_login.db.insert_contact(contact_type='customer', display_name='Probefirma', company_name='Probefirma')
    resp = get(server_port, f'/masterdata/contacts?{encode(entity=HOSTILE_ENTITY)}')
    assert resp.status == 200 and 'alert(1)' not in resp.text
    assert_same_structure(resp.text, get(server_port, '/masterdata/contacts?entity=person').text)


def test_stored_entity_type_with_a_script_break_falls_back_in_the_edit_form(tmp_db):
    cid = tmp_db.insert_contact(contact_type='customer', display_name='Probefirma', company_name='Probefirma')
    sql(tmp_db, 'UPDATE Contacts SET EntityType = ? WHERE ID = ?', HOSTILE_ENTITY, cid)
    html = PageContactEdit(tmp_db, cid)
    assert [a['value'] for a in inputs(html, name='entity_type')] == ['company']
    assert 'alert(1)' not in html


@pytest.fixture
def hostile_contacts(tmp_db):
    """Firma und Person, deren Texte alle feindlich sind; liefert (Firma-ID, Person-ID)."""
    company = tmp_db.insert_contact(
        contact_type='customer', entity_type='company', display_name=IMG, company_name=IMG,
        customer_number=BREAK, abbreviation='XX', email=BREAK, phone=IMG, notes=SCRIPT,
        address_line1=IMG, street=IMG, postal_code=BREAK, city=SCRIPT, tax_id=BREAK, buyer_route_id=BREAK)
    person = tmp_db.insert_contact(
        contact_type='customer', entity_type='person', display_name=IMG, first_name=IMG, last_name=SCRIPT,
        job_title=IMG, department=BREAK, date_of_birth=BREAK_SCRIPT, company_name_free=BREAK,
        email=IMG, company_contact_id=company)
    return company, person


def _benign(db):
    """Dieselben Kontakte mit harmlosem Text an denselben Stellen (Maßstab für den Aufbau der Seite)."""
    for table, columns in (('Contacts', ('DisplayName', 'CustomerNumber', 'Abbreviation', 'Email', 'Phone', 'Notes')),
                           ('ContactAddresses', ('AddressLine1', 'Street', 'PostalCode', 'City')),
                           ('CompanyDetails', ('CompanyName', 'TaxID', 'BuyerRouteID')),
                           ('PersonDetails', ('FirstName', 'LastName', 'JobTitle', 'Department', 'DateOfBirth',
                                             'CompanyName_Free'))):
        for column in columns:
            sql(db, f'UPDATE {table} SET {column} = ? WHERE {column} IS NOT NULL', 'harmlos')


def test_contact_list_shows_hostile_names_as_text(tmp_db, hostile_contacts):
    hostile = PageContacts(tmp_db)
    assert IMG not in hostile and SCRIPT not in hostile
    assert escaped(IMG) in hostile
    _benign(tmp_db)
    assert_same_structure(hostile, PageContacts(tmp_db))
    assert Dom(hostile).all('img') == []


def test_contact_edit_header_and_form_show_a_hostile_name_as_text(tmp_db, hostile_contacts):
    company, _ = hostile_contacts
    hostile = PageContactEdit(tmp_db, company)
    assert f'Kontakt bearbeiten: <em>{escaped(IMG)}</em>' in hostile      # Kopf des Bearbeiten-Formulars
    assert IMG not in hostile
    # Die Felder stehen vollständig im Attribut – nichts davon bricht aus
    assert [a['value'] for a in inputs(hostile, name='customer_number')] == [BREAK]
    assert [a['value'] for a in inputs(hostile, name='email')] == [BREAK]
    assert [a['value'] for a in inputs(hostile, name='street')] == [IMG]
    assert [a['value'] for a in inputs(hostile, name='tax_id')] == [BREAK]
    assert [a['value'] for a in inputs(hostile, name='company_name')] == [IMG]
    _benign(tmp_db)
    assert_same_structure(hostile, PageContactEdit(tmp_db, company))
    assert Dom(hostile).all('img') == []


def test_person_form_shows_a_hostile_date_of_birth_as_a_value(tmp_db, hostile_contacts):
    _, person = hostile_contacts
    hostile = PageContactEdit(tmp_db, person)
    assert [a['value'] for a in inputs(hostile, name='date_of_birth')] == [BREAK_SCRIPT]
    assert BREAK_SCRIPT not in hostile and '"><script>' not in hostile
    assert [a['value'] for a in inputs(hostile, name='company_name_free')] == [BREAK]
    assert [a['value'] for a in inputs(hostile, name='last_name')] == [SCRIPT]
    _benign(tmp_db)
    assert_same_structure(hostile, PageContactEdit(tmp_db, person))


def test_contact_list_cells_read_back_as_the_stored_text(tmp_db, hostile_contacts):
    rows = [r for r in table_rows(PageContacts(tmp_db)) if IMG in r]
    assert len(rows) == 2                                             # beide Kontakte: der Anzeigename ist der Text selbst


def test_abbreviation_check_sets_its_message_without_innerhtml(tmp_db):
    html = PageContacts(tmp_db)
    assert 'feedback.innerHTML' not in html                           # der Vorschlag kommt aus der Eingabe
    assert 'createTextNode' in html and 'link.textContent = data.suggestion' in html


# ── Rechnungsformular und -liste ─────────────────────────────────────────────
DESCRIPTION = 'Monitor 27" <b>x</b>'
ORDER = '"><b>Bestellung</b>'
ROUTE = '991-"><u>Leitweg</u>'


@pytest.fixture
def invoice_world(tmp_db):
    own, customer = add_contacts(tmp_db)
    invoice = add_document(tmp_db, '26R"><i>1', 'invoice', own, customer, description=DESCRIPTION, unit=BREAK,
                           buyer_name=IMG, buyer_company=IMG, order_number=ORDER, buyer_route_id=ROUTE,
                           payment_due_date='2026-03-18', skonto_days=7, skonto_percent=2.5)
    return type('World', (), dict(db=tmp_db, own=own, customer=customer, invoice=invoice))


def _benign_invoice(db):
    sql(db, "UPDATE InvoiceItems SET Description = 'Monitor', Unit = 'Stk.'")
    sql(db, "UPDATE Invoices SET InvoiceNumber = '26R001', BuyerName = 'Max', BuyerCompany = 'Firma', "
            "OrderNumber = 'B-1', BuyerRouteID = '991-1'")


def test_invoice_form_puts_hostile_values_complete_and_escaped_into_the_fields(invoice_world):
    w = invoice_world
    hostile = PageInvoice(w.db, {}, w.invoice)
    dom = Dom(hostile)
    descriptions = [a['value'] for a in dom.all('input') if 'item-description' in a.get('class', '').split()]
    units = [a['value'] for a in dom.all('input') if 'item-unit' in a.get('class', '').split()]
    assert descriptions == [DESCRIPTION]                  # das `"` schneidet den Wert nicht ab, `<b>` bleibt Text
    assert units == [BREAK]
    assert dom.one('input', id='order_number')['value'] == ORDER
    assert dom.one('input', id='buyer_route_id')['value'] == ROUTE
    assert dom.one('input', id='invoice_number')['value'] == '26R"><i>1'
    assert dom.one('input', id='payment_due_date')['value'] == '2026-03-18'
    assert dom.one('input', id='discount_days')['value'] == '7'
    assert IMG not in hostile and '<b>Bestellung</b>' not in hostile
    _benign_invoice(w.db)
    assert_same_structure(hostile, PageInvoice(w.db, {}, w.invoice))     # keine zusätzlichen Elemente
    assert Dom(hostile).names.count('b') == Dom(PageInvoice(w.db, {}, w.invoice)).names.count('b')


def test_invoice_form_title_and_status_show_the_number_as_text(invoice_world):
    html = PageInvoice(invoice_world.db, {}, invoice_world.invoice)
    assert 'Rechnung 26R&quot;&gt;&lt;i&gt;1 bearbeiten' in html
    assert '<i>1' not in html


def test_invoice_form_survives_a_round_trip_of_hostile_text(invoice_world):
    # Was das Formular zeigt, schickt „Speichern“ wieder zurück: die Positionsbeschreibung bleibt ganz
    w = invoice_world
    description = [a['value'] for a in Dom(PageInvoice(w.db, {}, w.invoice)).all('input')
                   if 'item-description' in a.get('class', '').split()][0]
    assert description == DESCRIPTION == w.db.get_invoice_items(w.invoice)[0][4]


def test_invoice_form_customer_names_are_text_in_the_options_and_data_in_the_json(tmp_db):
    own, _ = add_contacts(tmp_db)
    customer = tmp_db.insert_contact(contact_type='customer', display_name='</script>' + IMG,
                                     company_name='</script>' + IMG, street=IMG)
    html = PageInvoice(tmp_db, {}, None)
    assert '</script><img' not in html and IMG not in html
    assert re.search(rf'<option value="{customer}"[^>]*>{re.escape(escaped("</script>" + IMG))}</option>', html)
    data = data_block(Dom(html), 'customersData')
    assert data[str(customer)]['company'] == '</script>' + IMG        # als JSON-Wert vollständig vorhanden


def test_invoice_list_shows_hostile_values_as_text_and_cuts_before_escaping(tmp_db):
    own, customer = add_contacts(tmp_db)
    long_name = 'x' * 29 + '<b>Rest</b>'
    add_document(tmp_db, '26R"><i>1', 'invoice', own, customer, buyer_name=long_name, buyer_company=long_name)
    add_document(tmp_db, '26R002', 'invoice', own, customer, buyer_name=IMG, buyer_company=IMG)
    html = PageInvoice(tmp_db, {})
    assert '<i>1' not in html and IMG not in html and '<b>Rest' not in html
    cells = {r[0]: r for r in table_rows(html) if len(r) == 7 and r[0].startswith('26R')}
    # 30 Zeichen des Namens, dann erst maskiert: das `<` ist ganz da, nie ein halbes `&l`
    assert cells['26R"><i>1'][2] == 'x' * 29 + '<'
    assert cells['26R002'][2] == IMG


@pytest.mark.parametrize('search, found', [
    pytest.param('26R001', ['26R001'], id='Nummer'),
    pytest.param('pinsel', ['26R001'], id='Firma-ohne-Beachtung-der-Schreibweise'),
    pytest.param('Malermeister', ['26R001'], id='Firma'),
    pytest.param('mustermann', ['26R001'], id='Käufername'),
    pytest.param('erika', ['26R002'], id='Käufername-der-zweiten'),
    pytest.param('blumen', ['26R002'], id='Firma-der-zweiten'),
    pytest.param('26R', ['26R001', '26R002'], id='gemeinsamer-Nummernanfang'),
    pytest.param('gibt-es-nicht', [], id='ohne-Treffer'),
    pytest.param('030 1234567', [], id='Telefon-des-Verkäufers-zählt-nicht'),
    pytest.param('info@example.com', [], id='E-Mail-des-Verkäufers-zählt-nicht'),
])
def test_invoice_search_finds_number_buyer_name_and_buyer_company(tmp_db, search, found):
    own, customer = add_contacts(tmp_db)
    add_document(tmp_db, '26R001', 'invoice', own, customer, buyer_name='Max Mustermann',
                 buyer_company='Malermeister Pinsel')
    add_document(tmp_db, '26R002', 'invoice', own, customer, buyer_name='Erika Probe',
                 buyer_company='Blumenhaus Rosengarten')
    add_document(tmp_db, '26A003', 'quote', own, customer, buyer_name='Max Mustermann')    # Angebote nie in der Liste
    html = PageInvoice(tmp_db, {'search': search})
    numbers = [r[0] for r in table_rows(html) if len(r) == 7 and r[0].startswith('26')]
    assert sorted(numbers) == found


def test_invoice_search_value_is_escaped_in_the_search_field(tmp_db):
    html = PageInvoice(tmp_db, {'search': BREAK})
    assert [a['value'] for a in inputs(html, id='searchQuery')] == [BREAK]
    assert_same_structure(html, PageInvoice(tmp_db, {'search': 'x'}))


# ── Angebotsformular und -liste ──────────────────────────────────────────────
@pytest.fixture
def quote_world(tmp_db):
    own, customer = add_contacts(tmp_db)
    quote = add_document(tmp_db, '26A"><i>1', 'quote', own, customer, description=DESCRIPTION, unit=BREAK,
                         buyer_name=IMG, buyer_company=IMG, valid_until='2026-05-01')
    return type('World', (), dict(db=tmp_db, own=own, customer=customer, quote=quote))


def test_quote_form_puts_hostile_values_complete_and_escaped_into_the_fields(quote_world):
    w = quote_world
    hostile = PageQuote(w.db, {}, w.quote)
    dom = Dom(hostile)
    assert [a['value'] for a in dom.all('input') if 'item-description' in a.get('class', '').split()] == [DESCRIPTION]
    assert [a['value'] for a in dom.all('input') if 'item-unit' in a.get('class', '').split()] == [BREAK]
    assert dom.one('input', id='quote_number')['value'] == '26A"><i>1'
    assert 'Angebot 26A&quot;&gt;&lt;i&gt;1 bearbeiten' in hostile and '<i>1' not in hostile
    sql(w.db, "UPDATE InvoiceItems SET Description = 'Monitor', Unit = 'Stk.'")
    sql(w.db, "UPDATE Invoices SET InvoiceNumber = '26A001', BuyerName = 'Max', BuyerCompany = 'Firma'")
    assert_same_structure(hostile, PageQuote(w.db, {}, w.quote))


def test_quote_list_shows_hostile_values_as_text_and_cuts_before_escaping(tmp_db):
    own, customer = add_contacts(tmp_db)
    add_document(tmp_db, '26A"><i>1', 'quote', own, customer, buyer_name='x' * 29 + '<b>Rest</b>',
                 valid_until=BREAK)
    html = PageQuote(tmp_db, {})
    assert '<i>1' not in html and '<b>Rest' not in html and BREAK not in html
    rows = [r for r in table_rows(html) if r and r[0].startswith('26A')]
    assert rows[0][0] == '26A"><i>1' and rows[0][2] == 'x' * 29 + '<' and rows[0][6] == BREAK


def test_quote_search_finds_number_and_buyer(tmp_db):
    own, customer = add_contacts(tmp_db)
    add_document(tmp_db, '26A001', 'quote', own, customer, buyer_name='Max Mustermann')
    add_document(tmp_db, '26A002', 'quote', own, customer, buyer_name='Erika Probe')
    found = lambda q: sorted(r[0] for r in table_rows(PageQuote(tmp_db, {'search': q})) if r and r[0].startswith('26A'))
    assert found('26a001') == ['26A001'] and found('erika') == ['26A002'] and found('26A') == ['26A001', '26A002']


# ── Artikel ──────────────────────────────────────────────────────────────────
def test_articles_page_shows_hostile_texts_as_text_and_in_the_edit_fields(tmp_db):
    tmp_db.insert_article(name=BREAK, unit=BREAK, unit_price=12.5, tax_rate=19, description=SCRIPT)
    article = tmp_db.fetch_articles()[0][0]
    listing = PageArticles(tmp_db)
    assert BREAK not in listing and SCRIPT not in listing
    row = next(r for r in table_rows(listing) if BREAK in r)
    assert row[0] == BREAK and row[1] == BREAK and row[4] == SCRIPT
    edit = PageArticles(tmp_db, edit_article_id=article)
    assert [a['value'] for a in inputs(edit, name='name')] == [BREAK]
    assert f'>{escaped(SCRIPT)}</textarea>' in edit
    tmp_db.update_article(article, name='Probe', unit='Stk.', unit_price=1, tax_rate=19, description='Text')
    assert_same_structure(edit, PageArticles(tmp_db, edit_article_id=article))
    assert_same_structure(listing, PageArticles(tmp_db))


@pytest.mark.parametrize('page', [PageInvoice, PageQuote], ids=['Rechnung', 'Angebot'])
def test_article_names_reach_the_document_forms_only_as_json_data(tmp_db, page):
    add_contacts(tmp_db)
    tmp_db.insert_article(name='</script>' + IMG, unit=IMG, unit_price=10, tax_rate=19, description='"><b>')
    article = tmp_db.fetch_articles()[0][0]
    html = page(tmp_db, {}, None)
    assert '</script><img' not in html and IMG not in html
    data = data_block(Dom(html), 'articlesData')
    assert data[str(article)]['name'] == '</script>' + IMG and data[str(article)]['description'] == '"><b>'
    # Der Browser baut die Auswahl aus DOM-Knoten (textContent), nicht aus Markup
    assert "name.textContent = a.name" in html and "'<strong>' + a.name" not in html


@pytest.mark.parametrize('page', [PageInvoice, PageQuote], ids=['Rechnung', 'Angebot'])
def test_document_forms_build_rows_without_data_in_the_markup_templates(tmp_db, page):
    # Zeilen aus einem Artikel: das Gerüst enthält nur den Zähler, Einheit/Text/Preis kommen als Wert
    import re
    html = page(tmp_db, {}, None)
    templates = re.findall(r'\.innerHTML = `(.*?)`', html, re.S)
    assert templates                                              # es gibt die Zeilenvorlage(n)
    for template in templates:
        assert re.findall(r'\$\{(.*?)\}', template) == ['rowCounter']
    assert '.item-description\').value =' in html and '.item-unit\').value =' in html


def test_articles_page_error_message_is_data_in_the_script_block(tmp_db):
    hostile = PageArticles(tmp_db, error=SCRIPT)
    assert_same_structure(hostile, PageArticles(tmp_db, error='Normale Meldung'))
    assert SCRIPT not in hostile and articles_message(hostile) == [SCRIPT, 'error']


def test_articles_page_error_parameter_over_http(server_port, auto_login):
    resp = get(server_port, f'/masterdata/articles?{encode(error=SCRIPT)}')
    assert resp.status == 200 and SCRIPT not in resp.text
    assert articles_message(resp.text) == [SCRIPT, 'error']
    assert_same_structure(resp.text, get(server_port, '/masterdata/articles?error=x').text)


# ── Artikel speichern: nan/inf werden abgelehnt ──────────────────────────────
ARTICLE_ERROR = 'Preis und Steuersatz müssen endliche Zahlen sein.'
ARTICLE_ROUTES = [
    pytest.param('/masterdata/articles/add', False, id='add'),
    pytest.param('/articles/add', False, id='add-kurz'),
    pytest.param('/masterdata/articles/update', True, id='update'),
    pytest.param('/articles/update', True, id='update-kurz'),
]
BAD_NUMBERS = [('unit_price', 'nan'), ('unit_price', 'NaN'), ('unit_price', 'Infinity'), ('unit_price', '-inf'),
               ('unit_price', 'inf'), ('unit_price', '1e999'), ('unit_price', '1e12'), ('unit_price', 'abc'),
               ('unit_price', '12,5'), ('tax_rate', 'nan'), ('tax_rate', 'abc'), ('tax_rate', 'Infinity'),
               ('tax_rate', '1e999')]


@pytest.fixture
def article(auto_login):
    auto_login.db.insert_article(name='Probeartikel', unit='Stk.', unit_price=10, tax_rate=19, description='Text')
    return auto_login.db.fetch_articles()[0][0]


def _articles(db):
    return db.fetch_articles()


@pytest.mark.parametrize('route, is_update', ARTICLE_ROUTES)
def test_non_finite_or_unreadable_numbers_redirect_with_an_error_and_save_nothing(
        server_port, auto_login, article, route, is_update):
    before = _articles(auto_login.db)
    for field, value in BAD_NUMBERS:
        form = {'id': str(article), 'name': 'Neuer Name', 'unit': 'Std.', 'unit_price': '5', 'tax_rate': '7',
                'description': 'Neu', 'active': '1', field: value}
        resp = post(server_port, route, form=form, csrf=auto_login.csrf)
        assert resp.status == 303, (field, value)
        location = resp.headers['Location']
        assert location == '/masterdata/articles?error=' + urllib.parse.quote(ARTICLE_ERROR), (field, value)
        assert _articles(auto_login.db) == before, (field, value)            # nichts gespeichert
    page = get(server_port, location)                                         # und die Seite zeigt den Grund
    assert page.status == 200 and articles_message(page.text) == [ARTICLE_ERROR, 'error']


@pytest.mark.parametrize('route, is_update', ARTICLE_ROUTES)
def test_valid_numbers_are_saved_and_redirect_to_the_list(server_port, auto_login, article, route, is_update):
    form = {'id': str(article), 'name': 'Neuer Name', 'unit': 'Std.', 'unit_price': '5.5', 'tax_rate': '7',
            'description': 'Neu', 'active': '1'}
    resp = post(server_port, route, form=form, csrf=auto_login.csrf)
    assert resp.status == 303 and resp.headers['Location'] == '/masterdata/articles'
    saved = [a for a in _articles(auto_login.db) if a[1] == 'Neuer Name']
    assert len(saved) == 1 and float(saved[0][3]) == 5.5 and saved[0][4] == 7


@pytest.mark.parametrize('route', ['/masterdata/articles/add', '/masterdata/articles/update'])
def test_zero_percent_tax_stays_zero_and_empty_fields_use_the_defaults(server_port, auto_login, article, route):
    form = {'id': str(article), 'name': 'Null Prozent', 'unit': 'Stk.', 'unit_price': '', 'tax_rate': '0'}
    assert post(server_port, route, form=form, csrf=auto_login.csrf).status == 303
    zero = [a for a in _articles(auto_login.db) if a[1] == 'Null Prozent']
    assert len(zero) == 1 and float(zero[0][3]) == 0 and zero[0][4] == 0      # 0 % wird nicht zu 19 %
    form['tax_rate'] = ''
    form['name'] = 'Standard'
    assert post(server_port, route, form=form, csrf=auto_login.csrf).status == 303
    default = [a for a in _articles(auto_login.db) if a[1] == 'Standard']
    assert len(default) == 1 and default[0][4] == 19


# ── Nummernkreise und Dashboard ──────────────────────────────────────────────
def test_number_range_page_shows_hostile_values_as_text_and_in_the_fields(tmp_db):
    tmp_db.insert_number_range('invoice', 2026, 'R', prefix=BREAK, current_number=3, description=SCRIPT,
                               number_format='{yy}' + BREAK)
    range_id = tmp_db.fetch_number_ranges('invoice')[0][0]
    listing = PageNumberRanges(tmp_db)
    assert SCRIPT not in listing and BREAK not in listing
    assert any(SCRIPT in r and BREAK in r for r in table_rows(listing))
    edit = PageNumberRanges(tmp_db, edit_id=range_id)
    assert [a['value'] for a in inputs(edit, name='prefix')] == [BREAK]
    assert [a['value'] for a in inputs(edit, name='number_format')] == ['{yy}' + BREAK]
    assert [a['value'] for a in inputs(edit, name='description')] == [SCRIPT]
    assert BREAK not in edit and SCRIPT not in edit


def test_dashboard_account_label_shows_a_hostile_name_as_text(tmp_db):
    tmp_db.insert_account(name=IMG, holder='Inhaber', number='DE89370400440532013000', bic='TESTDE1X',
                          bank_name='Testbank AG', skr_account=1200)
    html = PageDashboard(tmp_db)
    assert IMG not in html and f'>{escaped(IMG)}</label>' in html
    assert Dom(html).all('img') == []
    sql(tmp_db, "UPDATE Accounts SET Name = 'Girokonto' WHERE Name = ?", IMG)
    assert_same_structure(html, PageDashboard(tmp_db))
