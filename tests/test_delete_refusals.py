# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Stammdaten, auf die andere Daten verweisen, lassen sich nicht löschen (2.3.8).

Vorher scheiterte das Löschen am Fremdschlüssel mit „Server Fehler“ (500) und ließ die Verbindung offen („database is
locked“); eine Person zu löschen nahm per ``ON DELETE CASCADE`` still ihre Arbeitszeiten und Fahrten mit.

* ``Database.delete_contact`` / ``delete_article`` / ``delete_account`` / ``delete_asset_category`` / ``delete_receipt``
  lehnen mit ``DeleteRefused`` und Klartext ab, solange etwas auf den Datensatz verweist – jeder Verweis sperrt, auch
  wo das Schema CASCADE oder SET NULL kennt. Nichts wird gelöscht, die Datenbank bleibt nicht gesperrt.
* Alle acht Löschmethoden (dazu ``delete_number_range``, ``delete_worktime``, ``delete_trip``) schließen ihre Verbindung
  auch dann, wenn das Löschen scheitert.
* Die fünf Routen antworten mit 303 auf ihre Listenseite samt Meldung (nie 500); die Seite zeigt sie nur als JSON im
  Skriptblock (``appMsg``), nie als Markup.

Alle Namen, Nummern und Beträge sind erfundene Testwerte.
"""
import contextlib
import json
import re
import sqlite3
import urllib.parse

import pytest

from db import DeleteRefused
from db.core import reference_texts
from server.pages_masterdata import PageArticles
from tests.document_helpers import add_contacts, add_document, make_books
from tests.dom_helpers import Dom, assert_same_structure
from tests.http_client import get, own_origin, post

SCRIPT = '<script>alert(1)</script>'
IMG = '<img src=x onerror=alert(1)>'
BREAK = '"><img src=x onerror=alert(1)>'

DAY = '2026-03-04'

CONTACT_LEAD = 'Der Kontakt lässt sich nicht löschen, solange andere Daten auf ihn verweisen: '
ARTICLE_LEAD = 'Der Artikel lässt sich nicht löschen, solange er in Belegen steht: '
ARTICLE_TAIL = '. Er lässt sich stattdessen auf inaktiv setzen.'     # nur bei einem aktiven Artikel
ARTICLE_NO_HINT = '.'
ACCOUNT_LEAD = 'Das Konto lässt sich nicht löschen, solange andere Daten auf es verweisen: '
CATEGORY_LEAD = 'Die Kategorie lässt sich nicht löschen, solange Anlagen sie verwenden: '
RECEIPT_LEAD = 'Der Beleg lässt sich nicht löschen, solange andere Daten auf ihn verweisen: '
CASH_REFUSED = 'Die Kasse lässt sich nicht löschen.'

CONTACT_TABLES = ('Contacts', 'ContactAddresses', 'CompanyDetails', 'PersonDetails', 'ContactTypeLinks', 'PersonRoles',
                  'Invoices', 'InvoiceItems', 'Bookings', 'Assets', 'WorkTimes', 'Trips')


# ── Hilfen ───────────────────────────────────────────────────────────────────
def snapshot(db, *tables):
    """Alle Zeilen der Tabellen, roh – zum Vergleich vorher/nachher."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return {table: con.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() for table in tables}


def scalar(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql, args).fetchone()[0]


def assert_not_locked(db):
    """Nach einer Ablehnung hält keine Verbindung mehr die Schreibsperre: Ein anderer Schreiber kommt sofort durch."""
    con = sqlite3.connect(db.db_name, timeout=0.5)
    try:
        con.execute('BEGIN IMMEDIATE')
        con.rollback()
    finally:
        con.close()


def refused(delete, *args):
    """Die Meldung der Ablehnung (``DeleteRefused``)."""
    with pytest.raises(DeleteRefused) as info:
        delete(*args)
    return str(info.value)


def shown_message(text):
    """Meldung und Art aus dem ``appMsg(…)``-Aufruf einer Seite. Die Meldung steht als JSON im Skriptblock, die Art je
    nach Seite in einfachen oder doppelten Anführungszeichen."""
    for script in Dom(text).scripts:
        found = re.search(r'function\(\)\{appMsg\(', script)
        if found:
            message, end = json.JSONDecoder().raw_decode(script, found.end())
            kind = re.match(r""",['"](\w+)['"]\);""", script[end:])
            assert kind, script[end:end + 40]
            return [message, kind.group(1)]
    raise AssertionError('kein appMsg-Aufruf in der Seite')


def company(db, name='Probefirma'):
    return db.insert_contact(contact_type='customer', entity_type='company', display_name=name, company_name=name)


def person(db, last_name='Probe', company_contact_id=None):
    return db.insert_contact(contact_type='employee', entity_type='person', first_name='Eva', last_name=last_name,
                             company_contact_id=company_contact_id)


def article(db, name='Probeartikel'):
    db.insert_article(name=name, unit_price=10, tax_rate=19)
    return scalar(db, 'SELECT ID FROM Articles WHERE Name = ?', name)


def put_article_active(db, article_id, value):
    """Die Spalte ``Active`` roh setzen (auch auf NULL – so legt es nur die SQL-Konsole an)."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute('UPDATE Articles SET Active = ? WHERE ID = ?', (value, article_id))
        con.commit()


def position_with(db, article_id, invoice_id, number=2):
    """Eine weitere Position des Belegs, die den Artikel führt."""
    db.insert_invoice_item({'invoice_id': invoice_id, 'position': number, 'article_id': article_id,
                            'description': 'Position mit Artikel', 'quantity': 1, 'unit': 'Stk.',
                            'price_per_unit': 10.00, 'total_net': 10.00, 'tax_category': 'S', 'tax_rate': 0.19})


def bank(db, name='Probekonto', skr=1810):
    db.insert_account(name, 'Inhaber', 'DE89370400440532013000', 'COBADEFFXXX', 'Probebank', is_cash=0,
                      skr_account=skr)
    return scalar(db, 'SELECT ID FROM Accounts WHERE Name = ?', name)


def cash(db):
    return scalar(db, 'SELECT ID FROM Accounts WHERE IsCash = 1')


def category(db, name='Probekategorie'):
    db.insert_asset_category(name, 3)
    return scalar(db, 'SELECT ID FROM AssetCategories WHERE Name = ?', name)


def receipt(db, number='B-100'):
    db.insert_receipt(number, DAY, 'beleg.pdf', 'pfad/beleg.pdf', 'Info')
    return scalar(db, 'SELECT ID FROM Documents WHERE Number = ? ORDER BY ID DESC', number)


# ── der gemeinsame Baustein ──────────────────────────────────────────────────
def test_reference_texts_name_only_the_counts_above_zero_in_the_order_of_the_list():
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE T (X INTEGER)')
    con.executemany('INSERT INTO T VALUES (?)', [(1,), (1,), (1,), (2,)])
    checks = [('SELECT COUNT(*) FROM T WHERE X = ?', (1,), 'Zeile', 'Zeilen'),
              ('SELECT COUNT(*) FROM T WHERE X = ?', (2,), 'Eintrag', 'Einträge'),
              ('SELECT COUNT(*) FROM T WHERE X = ?', (3,), 'Nichts', 'Nichtse')]
    assert reference_texts(con, checks) == ['3 Zeilen', '1 Eintrag']
    assert reference_texts(con, checks[2:]) == []
    assert reference_texts(con, list(reversed(checks))) == ['1 Eintrag', '3 Zeilen']
    con.close()


# ── Kontakt ──────────────────────────────────────────────────────────────────
def as_customer(db, cid):
    add_document(db, 'R-1', 'invoice', None, cid)


def as_seller(db, cid):
    add_document(db, 'R-1', 'invoice', cid, None)


def quoted_to(db, cid):
    add_document(db, 'A-1', 'quote', None, cid)


def in_booking(db, cid):
    db.insert_booking(DAY, 10, contact_id=cid)


def as_supplier(db, cid):
    db.insert_asset('Rechner', DAY, 100, 3, supplier_id=cid)


def as_employer(db, cid):
    person(db, 'Mitarbeit', company_contact_id=cid)


def as_person_of_a_worktime(db, cid):
    db.insert_worktime(cid, DAY)


def as_customer_of_a_worktime(db, cid):
    db.insert_worktime(person(db, 'Anderer'), DAY, customer_id=cid)


def as_driver(db, cid):
    db.insert_trip(cid, DAY, destination='Ziel')


CONTACT_REFERENCES = [
    pytest.param(as_customer, '1 Rechnung', id='Rechnung-als-Kunde'),
    pytest.param(as_seller, '1 Rechnung', id='Rechnung-als-eigene-Firma'),
    pytest.param(quoted_to, '1 Angebot', id='Angebot'),
    pytest.param(in_booking, '1 Buchung', id='Buchung'),
    pytest.param(as_supplier, '1 Anlage', id='Anlage-als-Lieferant'),
    pytest.param(as_employer, '1 zugeordnete Person', id='zugeordnete-Person'),
    pytest.param(as_person_of_a_worktime, '1 Arbeitszeit-Eintrag', id='Arbeitszeit-der-Person'),
    pytest.param(as_customer_of_a_worktime, '1 Arbeitszeit-Eintrag', id='Arbeitszeit-beim-Kunden'),
    pytest.param(as_driver, '1 Fahrt', id='Fahrt'),
]


@pytest.mark.parametrize('build, phrase', CONTACT_REFERENCES)
def test_a_contact_that_something_refers_to_is_not_deleted(tmp_db, build, phrase):
    cid = company(tmp_db)
    build(tmp_db, cid)
    before = snapshot(tmp_db, *CONTACT_TABLES)
    assert refused(tmp_db.delete_contact, cid) == CONTACT_LEAD + phrase + '.'
    assert snapshot(tmp_db, *CONTACT_TABLES) == before          # auch Arbeitszeiten und Fahrten stehen noch
    assert tmp_db.get_contact_by_id(cid) is not None
    assert_not_locked(tmp_db)
    tmp_db.insert_article(name='Nach der Ablehnung')            # und ein Schreibzugriff über die App gelingt


def test_every_kind_of_reference_is_named_with_its_count_in_a_fixed_order(tmp_db):
    cid = company(tmp_db)
    add_document(tmp_db, 'R-1', 'invoice', None, cid)
    add_document(tmp_db, 'R-2', 'invoice', cid, None)
    add_document(tmp_db, 'A-1', 'quote', None, cid)
    for _ in range(3):
        in_booking(tmp_db, cid)
    as_supplier(tmp_db, cid)
    person(tmp_db, 'Eins', company_contact_id=cid)
    person(tmp_db, 'Zwei', company_contact_id=cid)
    tmp_db.insert_worktime(cid, DAY)
    tmp_db.insert_worktime(cid, '2026-03-05')
    as_customer_of_a_worktime(tmp_db, cid)
    as_driver(tmp_db, cid)
    before = snapshot(tmp_db, *CONTACT_TABLES)
    assert refused(tmp_db.delete_contact, cid) == (
        CONTACT_LEAD + '2 Rechnungen, 1 Angebot, 3 Buchungen, 1 Anlage, 2 zugeordnete Personen, '
        '3 Arbeitszeit-Einträge, 1 Fahrt.')
    assert snapshot(tmp_db, *CONTACT_TABLES) == before


def test_a_row_that_names_the_contact_twice_counts_once(tmp_db):
    cid = company(tmp_db)
    add_document(tmp_db, 'R-1', 'invoice', cid, cid)            # eigene Firma und Kunde zugleich
    tmp_db.insert_worktime(cid, DAY, customer_id=cid)           # Person und Kunde zugleich
    assert refused(tmp_db.delete_contact, cid) == CONTACT_LEAD + '1 Rechnung, 1 Arbeitszeit-Eintrag.'


def test_a_contact_without_references_is_deleted_with_its_own_sub_tables(tmp_db):
    cid = tmp_db.insert_contact(contact_type='customer', entity_type='person', first_name='Eva', last_name='Probe',
                                street='Probeweg 1', type_keys=['supplier'], role_keys=['orderer'])
    firm = tmp_db.insert_contact(contact_type='customer', entity_type='company', company_name='Probefirma')
    for table in ('ContactAddresses', 'PersonDetails', 'ContactTypeLinks', 'PersonRoles'):
        assert scalar(tmp_db, f'SELECT COUNT(*) FROM {table} WHERE ContactID = ?', cid) > 0
    tmp_db.delete_contact(cid)
    tmp_db.delete_contact(firm)
    for table in ('Contacts', 'ContactAddresses', 'CompanyDetails', 'PersonDetails', 'ContactTypeLinks', 'PersonRoles'):
        key = 'ID' if table == 'Contacts' else 'ContactID'
        assert scalar(tmp_db, f'SELECT COUNT(*) FROM {table} WHERE {key} IN (?, ?)', cid, firm) == 0, table


def test_a_person_that_only_refers_to_itself_as_its_company_can_be_deleted(tmp_db):
    cid = person(tmp_db)
    with contextlib.closing(sqlite3.connect(tmp_db.db_name)) as con:
        con.execute('UPDATE PersonDetails SET CompanyContactID = ContactID WHERE ContactID = ?', (cid,))
        con.commit()
    tmp_db.delete_contact(cid)
    assert tmp_db.get_contact_by_id(cid) is None


def test_deleting_a_person_no_longer_takes_worktimes_and_trips_along(tmp_db):
    """Das war der stille Verlust: ON DELETE CASCADE nahm beides mit."""
    cid = person(tmp_db)
    tmp_db.insert_worktime(cid, DAY)
    tmp_db.insert_trip(cid, DAY, destination='Ziel')
    assert refused(tmp_db.delete_contact, cid) == CONTACT_LEAD + '1 Arbeitszeit-Eintrag, 1 Fahrt.'
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM WorkTimes') == 1 and scalar(tmp_db, 'SELECT COUNT(*) FROM Trips') == 1
    tmp_db.delete_worktime(scalar(tmp_db, 'SELECT ID FROM WorkTimes'))
    tmp_db.delete_trip(scalar(tmp_db, 'SELECT ID FROM Trips'))
    tmp_db.delete_contact(cid)                                  # ohne Verweise geht es
    assert tmp_db.get_contact_by_id(cid) is None


def test_an_unknown_contact_is_no_error(tmp_db):
    tmp_db.delete_contact(99999)


# ── Artikel ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('positions, phrase', [
    pytest.param(1, '1 Position in Rechnungen oder Angeboten', id='Einzahl'),
    pytest.param(3, '3 Positionen in Rechnungen oder Angeboten', id='Mehrzahl'),
])
def test_an_article_in_documents_is_not_deleted(tmp_db, positions, phrase):
    aid = article(tmp_db)
    own, customer = add_contacts(tmp_db)
    invoice = add_document(tmp_db, 'R-1', 'invoice', own, customer)
    quote = add_document(tmp_db, 'A-1', 'quote', own, customer)
    for number, document in enumerate([invoice, quote, invoice][:positions], start=2):
        position_with(tmp_db, aid, document, number)
    before = snapshot(tmp_db, 'Articles', 'Invoices', 'InvoiceItems')
    assert refused(tmp_db.delete_article, aid) == ARTICLE_LEAD + phrase + ARTICLE_TAIL
    assert snapshot(tmp_db, 'Articles', 'Invoices', 'InvoiceItems') == before
    assert_not_locked(tmp_db)


def deactivate_in_the_app(db, article_id):
    db.update_article(article_id, 'Probeartikel', unit_price=10, tax_rate=19, active=0)


def deactivate_by_sql(db, article_id):
    put_article_active(db, article_id, 0)


def empty_by_sql(db, article_id):
    put_article_active(db, article_id, None)


@pytest.mark.parametrize('make_inactive', [deactivate_in_the_app, deactivate_by_sql, empty_by_sql],
                         ids=['in-der-Maske-inaktiv', 'per-SQL-0', 'leer-gilt-wie-in-der-Maske-als-inaktiv'])
def test_an_inactive_article_is_refused_without_the_hint_to_deactivate_it(tmp_db, make_inactive):
    aid = article(tmp_db)
    own, customer = add_contacts(tmp_db)
    position_with(tmp_db, aid, add_document(tmp_db, 'R-1', 'invoice', own, customer))
    make_inactive(tmp_db, aid)
    before = snapshot(tmp_db, 'Articles', 'Invoices', 'InvoiceItems')
    message = refused(tmp_db.delete_article, aid)
    assert message == ARTICLE_LEAD + '1 Position in Rechnungen oder Angeboten' + ARTICLE_NO_HINT
    assert 'inaktiv' not in message
    assert snapshot(tmp_db, 'Articles', 'Invoices', 'InvoiceItems') == before
    assert_not_locked(tmp_db)


def test_the_hint_follows_the_state_of_the_article_at_the_time_of_the_request(tmp_db):
    aid = article(tmp_db)
    own, customer = add_contacts(tmp_db)
    position_with(tmp_db, aid, add_document(tmp_db, 'R-1', 'invoice', own, customer))
    used = ARTICLE_LEAD + '1 Position in Rechnungen oder Angeboten'
    assert refused(tmp_db.delete_article, aid) == used + ARTICLE_TAIL
    deactivate_in_the_app(tmp_db, aid)
    assert refused(tmp_db.delete_article, aid) == used + ARTICLE_NO_HINT
    tmp_db.update_article(aid, 'Probeartikel', unit_price=10, tax_rate=19, active=1)
    assert refused(tmp_db.delete_article, aid) == used + ARTICLE_TAIL


def test_the_article_mask_offers_what_the_refusal_suggests(tmp_db):
    """„Er lässt sich stattdessen auf inaktiv setzen“ gilt nur, solange die Artikelmaske das Feld „Aktiv“ kennt."""
    aid = article(tmp_db)
    form = PageArticles(tmp_db, edit_article_id=aid)
    assert [a for a in Dom(form).all('input') if a.get('type') == 'checkbox' and a.get('name') == 'active']


def test_an_article_without_positions_is_deleted(tmp_db):
    aid, other = article(tmp_db), article(tmp_db, 'Anderer Artikel')
    tmp_db.delete_article(aid)
    assert tmp_db.get_article_by_id(aid) is None and tmp_db.get_article_by_id(other) is not None


def test_an_unknown_article_is_no_error(tmp_db):
    tmp_db.delete_article(99999)


# ── Bankkonto und Kasse ──────────────────────────────────────────────────────
def test_the_cash_account_is_never_deleted_and_says_so(tmp_db):
    kasse = cash(tmp_db)
    before = snapshot(tmp_db, 'Accounts')
    assert refused(tmp_db.delete_account, kasse) == CASH_REFUSED
    assert snapshot(tmp_db, 'Accounts') == before
    assert_not_locked(tmp_db)


@pytest.mark.parametrize('use, phrase', [
    pytest.param('booking', '1 Buchung', id='Buchung'),
    pytest.param('invoice', '1 Rechnung', id='Rechnung'),
    pytest.param('quote', '1 Angebot', id='Angebot'),
    pytest.param('all', '2 Buchungen, 1 Rechnung, 1 Angebot', id='alle-in-fester-Reihenfolge'),
])
def test_a_bank_account_that_something_refers_to_is_not_deleted(tmp_db, use, phrase):
    account = bank(tmp_db)
    if use in ('booking', 'all'):
        tmp_db.insert_booking(DAY, 10, account_id=account, booking_type='bank')
    if use == 'all':
        tmp_db.insert_booking('2026-03-05', 20, account_id=account, booking_type='bank')
    if use in ('invoice', 'all'):
        add_document(tmp_db, 'R-1', 'invoice', bank_account_id=account)
    if use in ('quote', 'all'):
        add_document(tmp_db, 'A-1', 'quote', bank_account_id=account)
    before = snapshot(tmp_db, 'Accounts', 'Bookings', 'Invoices')
    assert refused(tmp_db.delete_account, account) == ACCOUNT_LEAD + phrase + '.'
    assert snapshot(tmp_db, 'Accounts', 'Bookings', 'Invoices') == before
    assert_not_locked(tmp_db)


def test_a_bank_account_without_references_is_deleted_and_the_cash_account_stays(tmp_db):
    account = bank(tmp_db)
    tmp_db.delete_account(account)
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM Accounts WHERE ID = ?', account) == 0
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM Accounts WHERE IsCash = 1') == 1


def test_an_unknown_account_is_no_error(tmp_db):
    tmp_db.delete_account(99999)


# ── AfA-Kategorie ────────────────────────────────────────────────────────────
@pytest.mark.parametrize('assets, phrase', [(1, '1 Anlage'), (2, '2 Anlagen')])
def test_a_category_that_assets_use_is_not_deleted(tmp_db, assets, phrase):
    cid = category(tmp_db)
    for number in range(assets):
        tmp_db.insert_asset(f'Rechner {number}', DAY, 100, 3, asset_category_id=cid)
    before = snapshot(tmp_db, 'AssetCategories', 'Assets')
    assert refused(tmp_db.delete_asset_category, cid) == CATEGORY_LEAD + phrase + '.'
    assert snapshot(tmp_db, 'AssetCategories', 'Assets') == before
    assert_not_locked(tmp_db)


def test_a_category_without_assets_is_deleted(tmp_db):
    cid, other = category(tmp_db), category(tmp_db, 'Andere Kategorie')
    tmp_db.insert_asset('Rechner', DAY, 100, 3, asset_category_id=other)
    tmp_db.delete_asset_category(cid)
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM AssetCategories WHERE ID = ?', cid) == 0
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM AssetCategories WHERE ID = ?', other) == 1


def test_an_unknown_category_is_no_error(tmp_db):
    tmp_db.delete_asset_category(99999)


# ── Beleg ────────────────────────────────────────────────────────────────────
def booked_on(db, doc_id, day=DAY):
    db.link_booking_to_document(db.insert_booking(day, 10), doc_id)


def asset_of(db, doc_id):
    db.insert_asset('Rechner', DAY, 100, 3, document_id=doc_id)


def trip_of(db, doc_id):
    db.insert_trip(person(db), DAY, destination='Ziel', document_id=doc_id)


@pytest.mark.parametrize('build, phrase', [
    pytest.param([booked_on], '1 Buchung', id='Buchung'),
    pytest.param([asset_of], '1 Anlage', id='Anlage'),
    pytest.param([trip_of], '1 Fahrt', id='Fahrt'),
    pytest.param([booked_on, lambda db, doc: booked_on(db, doc, '2026-03-05'), asset_of, trip_of],
                 '2 Buchungen, 1 Anlage, 1 Fahrt', id='alle-in-fester-Reihenfolge'),
])
def test_a_receipt_that_something_refers_to_is_not_deleted(tmp_db, build, phrase):
    doc_id = receipt(tmp_db)
    for step in build:
        step(tmp_db, doc_id)
    tables = ('Documents', 'BookingDocuments', 'Bookings', 'Assets', 'Trips')
    before = snapshot(tmp_db, *tables)
    # seit 2.4.2 über die ID des Belegs (vorher über die Nummer, die nicht eindeutig ist)
    assert refused(tmp_db.delete_receipt, doc_id) == RECEIPT_LEAD + phrase + '.'
    assert snapshot(tmp_db, *tables) == before
    assert_not_locked(tmp_db)


def test_only_the_references_of_this_receipt_count_not_those_of_another_one_with_the_same_number(tmp_db):
    # seit 2.4.2: Zwei Belege mit derselben Nummer sind zwei Belege – gezählt wird, was auf DIESE ID verweist
    first, second = receipt(tmp_db), receipt(tmp_db)
    booking = tmp_db.insert_booking(DAY, 10)
    tmp_db.link_booking_to_document(booking, first)
    booked_on(tmp_db, first, '2026-03-05')
    assert refused(tmp_db.delete_receipt, first) == RECEIPT_LEAD + '2 Buchungen.'
    tmp_db.delete_receipt(second)                                      # auf den zweiten verweist nichts: er geht
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', second) == 0
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', first) == 1


def test_a_receipt_without_references_is_deleted_alone_and_every_other_one_stays(tmp_db):
    # seit 2.4.2 genau EIN Beleg: Vorher nahm das Löschen alle Belege mit derselben Nummer mit
    first, second = receipt(tmp_db), receipt(tmp_db)                   # zwei Belege mit derselben Nummer
    other = receipt(tmp_db, 'B-200')
    tmp_db.delete_receipt(first)
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', first) == 0
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', second) == 1
    assert scalar(tmp_db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', other) == 1


def test_an_unknown_receipt_is_no_error(tmp_db):
    tmp_db.delete_receipt(99999)


# ── die Verbindung wird immer geschlossen ────────────────────────────────────
def _fail_on_delete(sql):
    if sql.lstrip().upper().startswith('DELETE'):
        raise sqlite3.OperationalError('simulierter Fehler beim Löschen')


class FailingCursor(sqlite3.Cursor):
    def execute(self, sql, *args):
        _fail_on_delete(sql)
        return super().execute(sql, *args)


class FailingConnection(sqlite3.Connection):
    """Eine Verbindung, deren DELETE scheitert (wie „database is locked“ oder ein Fremdschlüssel) – gleich, ob die
    Methode über die Verbindung oder über einen Cursor löscht – und die merkt, ob sie geschlossen wurde."""
    closed = False

    def execute(self, sql, *args):
        _fail_on_delete(sql)
        return super().execute(sql, *args)

    def cursor(self, factory=FailingCursor):
        return super().cursor(factory)

    def close(self):
        self.closed = True
        super().close()


def _range(db):
    db.insert_number_range('invoice', 2031, 'Z')
    return scalar(db, 'SELECT ID FROM NumberRanges WHERE Year = 2031')


def _worktime(db):
    return db.insert_worktime(person(db), DAY)


def _trip(db):
    return db.insert_trip(person(db), DAY, destination='Ziel')


def _receipt_id(db):
    return receipt(db)                                                 # seit 2.4.2 wird über die ID gelöscht


CLOSING_CASES = [
    pytest.param('delete_contact', company, id='Kontakt'),
    pytest.param('delete_article', article, id='Artikel'),
    pytest.param('delete_account', bank, id='Bankkonto'),
    pytest.param('delete_asset_category', category, id='AfA-Kategorie'),
    pytest.param('delete_receipt', _receipt_id, id='Beleg'),
    pytest.param('delete_number_range', _range, id='Nummernkreis'),
    pytest.param('delete_worktime', _worktime, id='Arbeitszeit'),
    pytest.param('delete_trip', _trip, id='Fahrt'),
]


@pytest.mark.parametrize('method, make', CLOSING_CASES)
def test_a_failing_delete_closes_the_connection_and_leaves_no_lock(tmp_db, monkeypatch, method, make):
    target = make(tmp_db)
    opened = []

    def connect():
        conn = sqlite3.connect(tmp_db.db_name, factory=FailingConnection)
        conn.execute('PRAGMA foreign_keys = ON')
        opened.append(conn)
        return conn

    monkeypatch.setattr(tmp_db, '_get_connection', connect)
    with pytest.raises(sqlite3.OperationalError, match='simulierter Fehler') as info:
        getattr(tmp_db, method)(target)
    assert opened and all(conn.closed for conn in opened)
    # ``info`` hält die Rückverfolgung und damit die Rahmen der Methode am Leben: Bliebe ihre Verbindung offen,
    # hielte sie hier noch die Schreibsperre
    assert info.value is not None
    assert_not_locked(tmp_db)


# ── über HTTP: Weiterleitung mit Meldung, nie 500 ────────────────────────────
@pytest.fixture
def books(auto_login):
    """Eigene Firma, ein Kunde, eine Rechnung und ein Angebot an ihn."""
    return make_books(auto_login)


def delete_over_http(port, books, path):
    return post(port, path, headers={'Origin': own_origin(port)}, csrf=books.csrf)


def assert_refused_over_http(port, books, delete_path, list_path, param, message):
    """Die Route leitet mit der Meldung auf die Listenseite, und die Seite zeigt sie nur über ``appMsg``."""
    resp = delete_over_http(port, books, delete_path)
    assert resp.status == 303, resp.text
    location = resp.headers['Location']
    assert location == f'{list_path}?{param}={urllib.parse.quote(message, safe="")}'
    page = get(port, location)
    assert page.status == 200
    assert shown_message(page.text) == [message, 'error']
    assert message not in re.sub(r'<script\b.*?</script>', '', page.text, flags=re.S)      # nur im Skriptblock
    return page


def test_a_contact_with_references_is_refused_over_http(server_port, books):
    before = snapshot(books.db, *CONTACT_TABLES)
    assert_refused_over_http(server_port, books, f'/masterdata/contacts/delete?id={books.customer}',
                             '/masterdata/contacts', 'error', CONTACT_LEAD + '1 Rechnung, 1 Angebot.')
    assert snapshot(books.db, *CONTACT_TABLES) == before


def test_the_own_company_is_refused_too_and_a_free_contact_is_deleted(server_port, books):
    assert_refused_over_http(server_port, books, f'/masterdata/contacts/delete?id={books.own}',
                             '/masterdata/contacts', 'error', CONTACT_LEAD + '1 Rechnung, 1 Angebot.')
    free = company(books.db, 'Freie Firma')
    resp = delete_over_http(server_port, books, f'/masterdata/contacts/delete?id={free}')
    assert resp.status == 303 and resp.headers['Location'] == '/masterdata/contacts'
    assert books.db.get_contact_by_id(free) is None


def test_an_article_in_documents_is_refused_over_http(server_port, books):
    aid = article(books.db)
    position_with(books.db, aid, books.invoice)
    assert_refused_over_http(server_port, books, f'/masterdata/articles/delete?id={aid}', '/masterdata/articles',
                             'error', ARTICLE_LEAD + '1 Position in Rechnungen oder Angeboten' + ARTICLE_TAIL)
    assert books.db.get_article_by_id(aid) is not None


def test_an_inactive_article_in_documents_is_refused_over_http_without_the_hint(server_port, books):
    aid = article(books.db)
    position_with(books.db, aid, books.invoice)
    put_article_active(books.db, aid, 0)
    assert_refused_over_http(server_port, books, f'/masterdata/articles/delete?id={aid}', '/masterdata/articles',
                             'error', ARTICLE_LEAD + '1 Position in Rechnungen oder Angeboten' + ARTICLE_NO_HINT)
    assert books.db.get_article_by_id(aid) is not None


def test_a_bank_account_with_references_and_the_cash_account_are_refused_over_http(server_port, books):
    account = bank(books.db)
    books.db.insert_booking(DAY, 10, account_id=account, booking_type='bank')
    assert_refused_over_http(server_port, books, f'/masterdata/bankaccounts/delete?id={account}',
                             '/masterdata/bankaccounts', 'error', ACCOUNT_LEAD + '1 Buchung.')
    assert_refused_over_http(server_port, books, f'/masterdata/bankaccounts/delete?id={cash(books.db)}',
                             '/masterdata/bankaccounts', 'error', CASH_REFUSED)
    assert scalar(books.db, 'SELECT COUNT(*) FROM Accounts WHERE ID = ?', account) == 1
    assert scalar(books.db, 'SELECT COUNT(*) FROM Accounts WHERE IsCash = 1') == 1


def test_a_category_that_assets_use_is_refused_over_http(server_port, books):
    cid = category(books.db)
    books.db.insert_asset('Rechner', DAY, 100, 3, asset_category_id=cid)
    assert_refused_over_http(server_port, books, f'/asset_categories/delete?id={cid}', '/asset_categories', 'err',
                             CATEGORY_LEAD + '1 Anlage.')
    assert scalar(books.db, 'SELECT COUNT(*) FROM AssetCategories WHERE ID = ?', cid) == 1


def test_a_receipt_with_references_is_refused_over_http(server_port, books):
    doc_id = receipt(books.db)
    booked_on(books.db, doc_id)
    assert_refused_over_http(server_port, books, f'/receipts/delete?id={doc_id}', '/receipts', 'err',
                             RECEIPT_LEAD + '1 Buchung.')
    assert scalar(books.db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', doc_id) == 1


def test_objects_without_references_are_still_deleted_over_http(server_port, books):
    free_article, free_account, free_category = article(books.db), bank(books.db), category(books.db)
    free_receipt = receipt(books.db, 'B-300')
    for path, target in [(f'/masterdata/articles/delete?id={free_article}', '/masterdata/articles'),
                         (f'/masterdata/bankaccounts/delete?id={free_account}', '/masterdata/bankaccounts'),
                         (f'/asset_categories/delete?id={free_category}', '/asset_categories'),
                         (f'/receipts/delete?id={free_receipt}', '/receipts')]:
        resp = delete_over_http(server_port, books, path)
        assert resp.status == 303 and resp.headers['Location'] == target, path
    assert books.db.get_article_by_id(free_article) is None
    assert scalar(books.db, 'SELECT COUNT(*) FROM Accounts WHERE ID = ?', free_account) == 0
    assert scalar(books.db, 'SELECT COUNT(*) FROM AssetCategories WHERE ID = ?', free_category) == 0
    assert scalar(books.db, 'SELECT COUNT(*) FROM Documents WHERE ID = ?', free_receipt) == 0


LIST_PAGES = [
    pytest.param('/masterdata/articles', 'error', id='Artikel'),
    pytest.param('/masterdata/bankaccounts', 'error', id='Bankkonten'),
    pytest.param('/masterdata/contacts', 'error', id='Kontakte'),
    pytest.param('/asset_categories', 'err', id='AfA-Kategorien'),
    pytest.param('/receipts', 'err', id='Belege'),
]


@pytest.mark.parametrize('path, param', LIST_PAGES)
def test_the_message_of_a_list_page_is_data_in_the_script_block_never_markup(server_port, auto_login, path, param):
    benign = get(server_port, f'{path}?{param}=Normale+Meldung')
    assert benign.status == 200
    assert shown_message(benign.text) == ['Normale Meldung', 'error']
    for hostile in (SCRIPT, IMG, BREAK, '</script><b>x</b>'):
        resp = get(server_port, f'{path}?{urllib.parse.urlencode({param: hostile})}')
        assert resp.status == 200, hostile
        assert hostile not in resp.text, hostile
        assert_same_structure(resp.text, benign.text)
        assert shown_message(resp.text) == [hostile, 'error']                  # die Meldung kommt unverändert an


@pytest.mark.parametrize('path', [p.values[0] for p in LIST_PAGES])
def test_a_list_page_without_a_message_shows_none(server_port, auto_login, path):
    resp = get(server_port, path)
    assert resp.status == 200
    assert not [s for s in Dom(resp.text).scripts if 'function(){appMsg(' in s]


def test_the_contact_list_keeps_its_filters_next_to_the_message(server_port, auto_login):
    auto_login.db.insert_contact(contact_type='customer', display_name='Nur Kunde', company_name='Nur Kunde')
    auto_login.db.insert_contact(contact_type='supplier', display_name='Nur Lieferant', company_name='Nur Lieferant')
    resp = get(server_port, '/masterdata/contacts?type=customer&error=Meldung')
    assert resp.status == 200
    assert 'Nur Kunde' in resp.text and 'Nur Lieferant' not in resp.text      # der Filter gilt weiter
    assert shown_message(resp.text) == ['Meldung', 'error']
