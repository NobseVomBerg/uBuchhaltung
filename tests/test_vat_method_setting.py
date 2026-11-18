# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Besteuerungsart der eigenen Firma (``Contacts.VatMethod``, 2.4.0) – nur festhalten und anzeigen.

* ``vat_method()`` liest streng: genau ``'ist'`` oder ``'soll'`` (als Text), alles andere ist „nicht festgelegt“.
* Nur ein eigener Kontakt (Firma oder Person) trägt sie, jeder andere bekommt NULL. ``None`` lässt den gespeicherten Wert,
  ein leerer Text leert, alles andere als ``ist``/``soll`` ist ein ``ValueError`` – dann ist nichts geschrieben.
* ``get_vat_method`` liest die Angabe des ersten eigenen Kontakts mit gültigem Wert; ein per SQL-Konsole gesetzter anderer
  Wert gilt als nicht festgelegt.
* Die Handler lesen das Formularfeld ``vat_method``: Fehlt es, bleibt die Angabe; ein ungültiger Wert lehnt mit Klartext ab.
* Das Formular eines eigenen Kontakts (Firma wie Person) führt die Auswahl samt Hinweis, jeder andere Kontakt nicht; ein
  gespeicherter Wert erscheint nie roh in der Seite.
* *Sonstiges* nennt unter dem DATEV-Formular nur bei ``soll`` einen Hinweis.
* **Nichts ändert sich an Buchungen und Auswertungen:** EÜR, Dashboard und DATEV-Stapel sind mit ``ist``, ``soll`` und ohne
  Angabe gleich, und das Speichern der Angabe schreibt keine Buchung.

Alle Namen und Beträge sind erfunden.
"""
import html
import re
import sqlite3
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import pytest

from db.contacts import VAT_METHOD_ERROR, VAT_METHOD_LABELS, VAT_METHODS, vat_method
from server.app import parse_form
from server.handlers import handle_add_contact, handle_update_contact
from server.pages_contacts import VAT_METHOD_HINT, PageContacts
from server.pages_miscellaneous import DATEV_VAT_SOLL_NOTE, PageMiscellaneous
from tests.datev_helpers import confirmed_export
from tests.entry_form_helpers import execute, query
from tests.http_client import get, own_origin, post

RANGE = ('2026-01-01', '2026-12-31')
FORM_HINT = ('Die App bucht Erlös und Umsatzsteuer einer Rechnung mit dem Zahlungseingang – so, wie es der Ist-Versteuerung '
             'entspricht. Bei Soll-Versteuerung entsteht die Umsatzsteuer schon mit der Leistung; das bildet die App noch '
             'nicht ab. Die Angabe ändert keine Buchung.')
SOLL_NOTE = ('Für die eigene Firma ist Soll-Versteuerung eingetragen. Die App weist Erlös und Umsatzsteuer weiterhin mit dem '
             'Zahlungseingang aus – auch im DATEV-Stapel.')
OPTIONS = [('', 'nicht festgelegt'), ('ist', 'Ist-Versteuerung (vereinnahmte Entgelte)'),
           ('soll', 'Soll-Versteuerung (vereinbarte Entgelte)')]


def _post(**fields):
    base = {'entity_type': ['company'], 'type_keys': ['own'], 'company_name': ['Eigene Probe GmbH'],
            'display_name': [''], 'customer_number': [''], 'abbreviation': [''], 'country': ['DE']}
    base.update({name: [value] for name, value in fields.items()})
    return base


def _person_post(**fields):
    post = _post(**fields)
    post.update(entity_type=['person'], first_name=['Erika'], last_name=['Probe'], company_name=[''])
    return post


def _own(db, kind='company', **fields):
    if kind == 'company':
        return db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Probe GmbH',
                                 company_name='Eigene Probe GmbH', **fields)
    return db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                             first_name='Erika', last_name='Probe', **fields)


def _update_own(db, contact_id, **more):
    """``update_contact`` für die eigene Firma mit den immer nötigen Angaben; ``more`` überschreibt sie."""
    args = dict(contact_type='own', entity_type='company', display_name='Neu', company_name='Neu')
    args.update(more)
    return db.update_contact(contact_id, **args)


def _stored(db, contact_id):
    return query(db, 'SELECT VatMethod FROM Contacts WHERE ID = ?', (contact_id,))[0][0]


def _set_raw(db, contact_id, value):
    execute(db, 'UPDATE Contacts SET VatMethod = ? WHERE ID = ?', (value, contact_id))


def _error(location):
    return unquote(parse_qs(urlparse(location).query).get('error', [''])[0])


def _select(page):
    """Die Auswahl ``vat_method`` im Formular: ``None``, wenn es sie nicht gibt, sonst ``(Optionen, vorgewählte Werte)``."""
    match = re.search(r'<select name="vat_method">(.*?)</select>', page, re.S)
    if match is None:
        return None
    options = re.findall(r'<option value="([^"]*)"\s*(selected)?\s*>([^<]*)</option>', match.group(1))
    return ([(value, html.unescape(label)) for value, _flag, label in options],
            [value for value, flag, _label in options if flag])


# ── Die Werte und das strenge Lesen ──────────────────────────────────────────

def test_the_values_the_labels_and_the_error_text_are_fixed():
    assert VAT_METHODS == ('ist', 'soll')
    assert VAT_METHOD_LABELS == {'ist': 'Ist-Versteuerung (vereinnahmte Entgelte)',
                                 'soll': 'Soll-Versteuerung (vereinbarte Entgelte)'}
    assert VAT_METHOD_ERROR == 'Bitte Ist- oder Soll-Versteuerung wählen.'


@pytest.mark.parametrize('value', ['ist', 'soll'])
def test_the_two_values_are_read_as_they_are(value):
    assert vat_method(value) == value


@pytest.mark.parametrize('value', [
    'IST', 'Ist', 'SOLL', 'Soll', ' ist', 'ist ', 'ist\n', '\tsoll', '', ' ', 'beides', 'ist;', 'ist\x00', 'ｉｓｔ', 'ıst',
    '"><script>alert(1)</script>', None, 1, 0, 1.0, True, False, ['ist'], ('soll',), {'ist'}])
def test_everything_else_counts_as_not_set(value):
    assert vat_method(value) is None


# ── Die Datenbank-Schicht ────────────────────────────────────────────────────

def test_a_new_own_company_or_person_carries_the_value(tmp_db):
    company, person = _own(tmp_db, vat_method='soll'), _own(tmp_db, 'person', vat_method='ist')
    assert (_stored(tmp_db, company), _stored(tmp_db, person)) == ('soll', 'ist')
    assert tmp_db.get_contact_by_id(company)['vat_method'] == 'soll'


def test_a_new_contact_without_the_argument_or_with_an_empty_one_has_none(tmp_db):
    assert _stored(tmp_db, _own(tmp_db)) is None
    assert _stored(tmp_db, _own(tmp_db, vat_method='')) is None


@pytest.mark.parametrize('kind', ['customer', 'supplier', 'other', 'employee'])
def test_another_contact_never_carries_it(tmp_db, kind):
    contact = tmp_db.insert_contact(contact_type=kind, entity_type='company', display_name='Anderer',
                                    company_name='Anderer', vat_method='soll')
    assert _stored(tmp_db, contact) is None
    _update_own(tmp_db, contact, contact_type=kind, display_name='Anderer', vat_method='ist')
    assert _stored(tmp_db, contact) is None


def test_update_without_the_argument_keeps_the_value_with_an_empty_text_clears_it_and_with_a_value_sets_it(tmp_db):
    own = _own(tmp_db, vat_method='soll')
    _update_own(tmp_db, own)
    assert _stored(tmp_db, own) == 'soll'                                # None: unverändert
    _update_own(tmp_db, own, vat_method='ist')
    assert _stored(tmp_db, own) == 'ist'
    _update_own(tmp_db, own, vat_method='')
    assert _stored(tmp_db, own) is None


def test_update_without_the_argument_leaves_even_a_stored_nonsense_value_untouched(tmp_db):
    """„Unverändert“ heißt unverändert: Der Rohwert bleibt; gelesen wird er überall als „nicht festgelegt“."""
    own = _own(tmp_db)
    _set_raw(tmp_db, own, 'IST')
    _update_own(tmp_db, own, display_name='Anderer Name')
    assert _stored(tmp_db, own) == 'IST' and tmp_db.get_vat_method() is None


def test_turning_the_company_into_a_person_keeps_the_value(tmp_db):
    """Sie steht an ``Contacts``, nicht an ``CompanyDetails``: Die eigene Firma darf auch eine Person sein."""
    own = _own(tmp_db, vat_method='soll')
    tmp_db.update_contact(own, contact_type='own', entity_type='person', display_name='Erika Probe',
                          first_name='Erika', last_name='Probe')
    assert _stored(tmp_db, own) == 'soll' and tmp_db.get_vat_method() == 'soll'


def test_leaving_the_type_own_clears_the_value_and_joining_it_does_not_bring_it_back(tmp_db):
    own = _own(tmp_db, vat_method='soll')
    _update_own(tmp_db, own, contact_type='customer', display_name='Jetzt Kunde')
    assert _stored(tmp_db, own) is None and tmp_db.get_vat_method() is None
    _update_own(tmp_db, own)                                             # wieder eigen, ohne Angabe: bleibt leer
    assert _stored(tmp_db, own) is None
    _update_own(tmp_db, own, vat_method='ist')                           # mit Angabe: gesetzt
    assert _stored(tmp_db, own) == 'ist'


@pytest.mark.parametrize('bad', ['IST', 'Soll', ' ist', 'ist ', 'ist\n', ' ', 'beides', 'ist;', 1, 0, True, False, ['ist']])
def test_an_invalid_value_is_refused_and_nothing_is_written(tmp_db, bad):
    own = _own(tmp_db, vat_method='ist')
    with pytest.raises(ValueError, match=re.escape(VAT_METHOD_ERROR)):
        _update_own(tmp_db, own, display_name='Nicht geschrieben', company_name='Nicht geschrieben', vat_method=bad)
    assert _stored(tmp_db, own) == 'ist'
    assert query(tmp_db, 'SELECT CompanyName FROM CompanyDetails WHERE ContactID = ?', (own,))[0][0] == 'Eigene Probe GmbH'
    assert query(tmp_db, 'SELECT DisplayName FROM Contacts WHERE ID = ?', (own,))[0][0] == 'Eigene Probe GmbH'

    with pytest.raises(ValueError, match=re.escape(VAT_METHOD_ERROR)):
        _own(tmp_db, 'person', vat_method=bad)
    assert len(query(tmp_db, 'SELECT ID FROM Contacts')) == 1            # keine zweite Firma entstanden


def test_an_invalid_value_is_refused_for_every_kind_of_contact(tmp_db):
    with pytest.raises(ValueError, match=re.escape(VAT_METHOD_ERROR)):
        tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='K', company_name='K',
                              vat_method='beides')
    assert query(tmp_db, 'SELECT ID FROM Contacts') == []


# ── get_vat_method ───────────────────────────────────────────────────────────

def test_without_an_own_contact_nothing_is_set(tmp_db):
    assert tmp_db.get_vat_method() is None
    tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='K', company_name='K')
    assert tmp_db.get_vat_method() is None


def test_get_vat_method_takes_the_first_own_contact_with_a_valid_value(tmp_db):
    first, second = _own(tmp_db), _own(tmp_db, 'person', vat_method='soll')
    assert tmp_db.get_vat_method() == 'soll'                             # der erste hat keine Angabe: der zweite gilt
    _set_raw(tmp_db, first, 'ist')
    assert tmp_db.get_vat_method() == 'ist'                              # beide haben eine: der erste (nach ID) gewinnt
    _set_raw(tmp_db, first, 'Unsinn')
    assert tmp_db.get_vat_method() == 'soll'                             # ein ungültiger Wert wird übersprungen
    _set_raw(tmp_db, second, None)
    assert tmp_db.get_vat_method() is None


def test_get_vat_method_reads_only_own_contacts(tmp_db):
    customer = tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='K', company_name='K')
    _set_raw(tmp_db, customer, 'soll')
    assert tmp_db.get_vat_method() is None


@pytest.mark.parametrize('stored', ['IST', ' ist', 'ist ', 'ist\n', 'beides', '', ' ', 1, 0, 1.5, '"><script>alert(1)</script>'])
def test_a_value_set_by_raw_sql_that_is_not_one_of_the_two_counts_as_not_set(tmp_db, stored):
    own = _own(tmp_db)
    _set_raw(tmp_db, own, stored)
    assert tmp_db.get_vat_method() is None


def test_get_vat_method_closes_its_connection_even_when_the_query_fails(tmp_db, monkeypatch):
    closed = []
    real = tmp_db._get_connection

    class Broken:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError('kaputt')

        def close(self):
            closed.append(True)
            self._connection.close()

    monkeypatch.setattr(tmp_db, '_get_connection', lambda: Broken(real()))
    with pytest.raises(sqlite3.OperationalError):
        tmp_db.get_vat_method()
    assert closed == [True]


# ── Der Handler ──────────────────────────────────────────────────────────────

def test_adding_the_own_company_or_person_stores_the_value(tmp_db):
    status, location = handle_add_contact(tmp_db, _post(vat_method='soll'))
    assert status == 303 and 'error' not in location
    status, location = handle_add_contact(tmp_db, _person_post(vat_method='ist'))
    assert status == 303 and 'error' not in location
    assert [row[0] for row in query(tmp_db, 'SELECT VatMethod FROM Contacts ORDER BY ID')] == ['soll', 'ist']
    assert tmp_db.get_vat_method() == 'soll'


def test_updating_the_own_company_changes_and_clears_the_value(tmp_db):
    own = _own(tmp_db)
    assert handle_update_contact(tmp_db, _post(contact_id=str(own), vat_method='ist'))[0] == 303
    assert _stored(tmp_db, own) == 'ist'
    assert handle_update_contact(tmp_db, _post(contact_id=str(own), vat_method='soll'))[0] == 303
    assert _stored(tmp_db, own) == 'soll'
    assert handle_update_contact(tmp_db, _post(contact_id=str(own), vat_method=''))[0] == 303
    assert _stored(tmp_db, own) is None


def test_updating_the_own_person_changes_the_value(tmp_db):
    own = _own(tmp_db, 'person')
    assert handle_update_contact(tmp_db, _person_post(contact_id=str(own), vat_method='soll'))[0] == 303
    assert _stored(tmp_db, own) == 'soll' and tmp_db.get_vat_method() == 'soll'


def test_a_browser_body_with_the_empty_option_clears_the_value_and_one_without_the_field_keeps_it(tmp_db):
    """Der Weg des Browsers: ``parse_form`` behält leere Felder – die gewählte Option „nicht festgelegt“ kommt als ``['']``
    an und leert die Angabe, ein Formular ohne das Feld (``None``) lässt sie."""
    own = _own(tmp_db, vat_method='soll')
    form = {'contact_id': str(own), 'entity_type': 'company', 'type_keys': 'own', 'company_name': 'Eigene Probe GmbH',
            'display_name': '', 'customer_number': '', 'abbreviation': '', 'country': 'DE'}

    assert handle_update_contact(tmp_db, parse_form(urlencode(form)))[0] == 303
    assert _stored(tmp_db, own) == 'soll'

    blank = parse_form(urlencode({**form, 'vat_method': ''}))
    assert blank['vat_method'] == ['']
    assert handle_update_contact(tmp_db, blank)[0] == 303
    assert _stored(tmp_db, own) is None

    assert handle_update_contact(tmp_db, parse_form(urlencode({**form, 'vat_method': 'ist'})))[0] == 303
    assert _stored(tmp_db, own) == 'ist'


def test_a_form_without_the_field_leaves_the_stored_value(tmp_db):
    """Das Formular eines anderen Browserstands oder Clients führt das Feld nicht: Die Angabe bleibt."""
    own = _own(tmp_db, vat_method='soll')
    assert handle_update_contact(tmp_db, _post(contact_id=str(own), company_name='Umbenannt GmbH'))[0] == 303
    assert _stored(tmp_db, own) == 'soll'
    assert query(tmp_db, 'SELECT CompanyName FROM CompanyDetails WHERE ContactID = ?', (own,))[0][0] == 'Umbenannt GmbH'


@pytest.mark.parametrize('bad', ['IST', 'Soll', ' ist', 'ist ', 'soll\n', ' ', 'beides', 'ist;', '1', 'on', 'true',
                                 '"><script>alert(1)</script>'])
def test_an_invalid_value_is_refused_with_a_plain_text_and_nothing_is_written(tmp_db, bad):
    own = _own(tmp_db, vat_method='ist')
    status, location = handle_update_contact(tmp_db, _post(contact_id=str(own), company_name='Nicht geschrieben',
                                                            vat_method=bad))
    assert status == 303 and _error(location) == VAT_METHOD_ERROR
    assert location.startswith(f'/masterdata/contacts/edit?id={own}&error=')           # das Formular öffnet wieder
    assert _stored(tmp_db, own) == 'ist'
    assert query(tmp_db, 'SELECT CompanyName FROM CompanyDetails WHERE ContactID = ?', (own,))[0][0] == 'Eigene Probe GmbH'

    status, location = handle_add_contact(tmp_db, _post(vat_method=bad))
    assert status == 303 and _error(location) == VAT_METHOD_ERROR and location.startswith('/masterdata/contacts?error=')
    assert len(query(tmp_db, 'SELECT ID FROM Contacts')) == 1                           # keine zweite Firma entstanden


@pytest.mark.parametrize('kind', ['customer', 'supplier'])
def test_another_contact_never_gets_the_value_even_when_a_client_sends_it(tmp_db, kind):
    status, _location = handle_add_contact(tmp_db, dict(_post(vat_method='soll'), type_keys=[kind]))
    assert status == 303
    assert [row[0] for row in query(tmp_db, 'SELECT VatMethod FROM Contacts')] == [None]


def test_leaving_the_type_own_in_the_form_clears_the_value(tmp_db):
    own = _own(tmp_db, vat_method='soll')
    handle_update_contact(tmp_db, dict(_post(contact_id=str(own), vat_method='soll'), type_keys=['customer']))
    assert _stored(tmp_db, own) is None


# ── Der echte Weg: Formular, Route, Seite ────────────────────────────────────

def test_the_browser_path_through_the_real_routes(server_port, auto_login):
    """Formular öffnen, speichern (``POST /masterdata/contacts/update``, mit Herkunft und CSRF-Token), wieder öffnen; ein
    ungültiger Wert kommt als Klartext auf die Seite zurück und ändert nichts."""
    db = auto_login.db
    own = _own(db)
    headers = {'Origin': own_origin(server_port)}
    edit = f'/masterdata/contacts/edit?id={own}'
    form = {'contact_id': str(own), 'entity_type': 'company', 'type_keys': 'own', 'company_name': 'Eigene Probe GmbH',
            'display_name': '', 'customer_number': '', 'abbreviation': '', 'country': 'DE'}

    assert _select(get(server_port, edit).text) == (OPTIONS, [''])
    assert 'Soll-Versteuerung eingetragen' not in get(server_port, '/miscellaneous').text

    saved = post(server_port, '/masterdata/contacts/update', headers=headers, csrf=auto_login.csrf,
                 form={**form, 'vat_method': 'soll'})
    assert saved.status == 303 and _stored(db, own) == 'soll'
    assert _select(get(server_port, edit).text) == (OPTIONS, ['soll'])
    assert html.escape(SOLL_NOTE, quote=True) in get(server_port, '/miscellaneous').text

    refused = post(server_port, '/masterdata/contacts/update', headers=headers, csrf=auto_login.csrf,
                   form={**form, 'vat_method': 'beides', 'company_name': 'Nicht geschrieben'})
    assert refused.status == 303 and _error(refused.headers['Location']) == VAT_METHOD_ERROR
    assert refused.headers['Location'].startswith(f'/masterdata/contacts/edit?id={own}&error=')
    assert _stored(db, own) == 'soll'
    assert query(db, 'SELECT CompanyName FROM CompanyDetails WHERE ContactID = ?', (own,))[0][0] == 'Eigene Probe GmbH'

    cleared = post(server_port, '/masterdata/contacts/update', headers=headers, csrf=auto_login.csrf,
                   form={**form, 'vat_method': ''})
    assert cleared.status == 303 and _stored(db, own) is None
    assert 'Soll-Versteuerung eingetragen' not in get(server_port, '/miscellaneous').text


# ── Das Formular ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('kind', ['company', 'person'])
@pytest.mark.parametrize('stored, selected', [(None, ['']), ('ist', ['ist']), ('soll', ['soll'])])
def test_the_form_of_an_own_contact_has_the_select_with_the_stored_value_preselected(tmp_db, kind, stored, selected):
    own = _own(tmp_db, kind, vat_method=stored)
    page = PageContacts(tmp_db, edit_contact_id=own)
    options, chosen = _select(page)
    assert options == OPTIONS and chosen == selected
    assert 'Besteuerungsart (Umsatzsteuer):' in page


@pytest.mark.parametrize('kind', ['company', 'person'])
def test_the_select_sits_inside_the_form_and_the_fixed_hint_stands_below_it(tmp_db, kind):
    assert VAT_METHOD_HINT == FORM_HINT
    page = PageContacts(tmp_db, edit_contact_id=_own(tmp_db, kind))
    form_start, form_end = page.index('<form method="POST"'), page.index('</form>')
    hint = html.escape(FORM_HINT, quote=True)
    assert form_start < page.index('<select name="vat_method">') < page.index(hint) < form_end
    assert f'<small class="muted">{hint}</small>' in page                                # vorhandene Hinweis-Klasse der Seite
    assert page.count('name="vat_method"') == 1


@pytest.mark.parametrize('stored', ['IST', ' ist', 'ist ', 'beides', '', 1])
def test_a_stored_value_that_is_not_one_of_the_two_shows_not_set(tmp_db, stored):
    own = _own(tmp_db)
    _set_raw(tmp_db, own, stored)
    assert _select(PageContacts(tmp_db, edit_contact_id=own))[1] == ['']


def test_a_stored_value_is_never_written_into_the_page_raw(tmp_db):
    for kind in ('company', 'person'):
        own = _own(tmp_db, kind)
        _set_raw(tmp_db, own, '"><script>alert(1)</script>')
        page = PageContacts(tmp_db, edit_contact_id=own)
        assert 'alert(1)' not in page and '&quot;&gt;' not in page                    # nirgends, auch nicht maskiert
        assert _select(page) == (OPTIONS, [''])


@pytest.mark.parametrize('kind', ['customer', 'supplier', 'other'])
def test_another_contact_does_not_show_the_select(tmp_db, kind):
    contact = tmp_db.insert_contact(contact_type=kind, entity_type='company', display_name='Anderer', company_name='Anderer')
    _set_raw(tmp_db, contact, 'soll')                                                  # auch nicht, wenn eine Angabe dasteht
    page = PageContacts(tmp_db, edit_contact_id=contact)
    assert _select(page) is None and 'Besteuerungsart' not in page and html.escape(FORM_HINT, quote=True) not in page


def test_the_form_for_a_new_contact_does_not_show_the_select(tmp_db):
    """Wie die DATEV-Nummern: Das Feld erscheint, sobald der gespeicherte Kontakt ein eigener ist."""
    assert _select(PageContacts(tmp_db)) is None
    assert _select(PageContacts(tmp_db, new_entity_type='person')) is None


# ── Der Hinweis auf der Seite Sonstiges ──────────────────────────────────────

def test_the_note_text_is_fixed():
    assert DATEV_VAT_SOLL_NOTE == SOLL_NOTE


def test_the_page_names_soll_under_the_export_form(tmp_db):
    _own(tmp_db, vat_method='soll')
    page = PageMiscellaneous(tmp_db)
    note = html.escape(SOLL_NOTE, quote=True)
    assert page.index('DATEV-CSV exportieren') < page.index(note)
    assert f'<p class="hint warnColor">{note}</p>' in page


@pytest.mark.parametrize('kind', ['company', 'person'])
def test_the_note_stands_for_an_own_person_too(tmp_db, kind):
    _own(tmp_db, kind, vat_method='soll')
    assert html.escape(SOLL_NOTE, quote=True) in PageMiscellaneous(tmp_db)


def test_the_page_has_no_note_without_soll(tmp_db):
    note = html.escape(SOLL_NOTE, quote=True)
    assert note not in PageMiscellaneous(tmp_db)                                       # kein eigener Kontakt
    own = _own(tmp_db)
    assert note not in PageMiscellaneous(tmp_db)                                       # nicht festgelegt
    _update_own(tmp_db, own, vat_method='ist')
    assert note not in PageMiscellaneous(tmp_db)                                       # Ist-Versteuerung
    _update_own(tmp_db, own, vat_method='soll')
    assert note in PageMiscellaneous(tmp_db)
    _update_own(tmp_db, own, vat_method='')
    assert note not in PageMiscellaneous(tmp_db)


@pytest.mark.parametrize('stored', ['SOLL', 'Soll', ' soll', 'soll ', 'beides', 1])
def test_the_page_has_no_note_for_a_value_that_is_not_exactly_soll(tmp_db, stored):
    own = _own(tmp_db)
    _set_raw(tmp_db, own, stored)
    assert html.escape(SOLL_NOTE, quote=True) not in PageMiscellaneous(tmp_db)


def test_the_page_has_no_note_for_soll_on_a_contact_that_is_not_own(tmp_db):
    customer = tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='K', company_name='K')
    _set_raw(tmp_db, customer, 'soll')
    assert html.escape(SOLL_NOTE, quote=True) not in PageMiscellaneous(tmp_db)


def test_the_note_does_not_replace_the_hint_for_the_missing_datev_numbers(tmp_db):
    _own(tmp_db, vat_method='soll')
    page = PageMiscellaneous(tmp_db)
    assert 'Berater- und Mandantennummer fehlen' in page and html.escape(SOLL_NOTE, quote=True) in page


# ── Nichts ändert sich an Buchungen und Auswertungen ─────────────────────────

def _books(db):
    """Eine versendete Rechnung mit Forderung, ihre Zahlung über die App zugeordnet (Bank an Debitoren, Wartekonto an
    Erlöskonto) und eine Ausgabe mit Vorsteuer."""
    db.insert_account('Probe-Bank', 'Ich', 'DE00PROBEBANK', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    account = next(a[0] for a in db.fetch_accounts() if a[1] == 'Probe-Bank')
    invoice = db.insert_invoice({
        'invoice_number': 'R-VAT-1', 'invoice_date': '2026-01-15', 'payment_due_date': '2099-12-31',
        'seller_name': 'Eigene Probe GmbH', 'seller_company': 'Eigene Probe GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG', 'tax_rate': 0.19, 'sum_net': 100.0, 'tax_amount': 19.0,
        'sum_gross': 119.0, 'amount_due': 119.0, 'status': 'sent'})
    db.ensure_receivable_booking(invoice)
    paid = db.insert_booking('2026-02-01', 119.0, account_id=account, booking_type='bank', recipient_client='Käufer AG')
    result = db.assign_invoice_payment(invoice, paid)
    assert result.ok, result.message
    spent = db.insert_booking('2026-03-05', -59.5, account_id=account, booking_type='bank', text='Büromaterial')
    coa = db.get_coa_id_by_account_number
    db.insert_booking('2026-03-05', -59.5, coa_id=coa(6815), counter_coa_id=coa(1800), tax_rate=0.19, tax_amount=-9.5,
                      document_number='ER-1', parent_booking_id=spent)


def _results(db):
    """Was die Auswertungen liefern: EÜR, Dashboard und der DATEV-Stapel (ohne die erste Zeile, den Vorlauf mit der Uhrzeit
    der Erzeugung) – über den echten Export-Handler."""
    stapel = confirmed_export(db, *RANGE)
    assert isinstance(stapel[0], bytes), f'DATEV-Export abgelehnt: {stapel}'
    return {'euer': db.get_euer_data(*RANGE), 'dashboard': db.get_dashboard_totals(*RANGE),
            'datev': stapel[0].split(b'\r\n', 1)[1]}


def test_euer_dashboard_and_datev_are_the_same_with_ist_soll_and_without_a_setting(tmp_db):
    own = _own(tmp_db)
    _books(tmp_db)

    results = {}
    for stored in (None, 'ist', 'soll', 'IST'):
        _set_raw(tmp_db, own, stored)
        results[stored] = _results(tmp_db)

    assert {number for number, _name, _total in results[None]['euer']} >= {4400, 3806, 6815}      # nicht leer verglichen
    assert results[None]['dashboard']['income'] > 0 and b'R-VAT-1' in results[None]['datev']
    assert results['ist'] == results['soll'] == results['IST'] == results[None]


def test_saving_the_setting_writes_only_contact_data(tmp_db):
    """Buchungen, Rechnungen, Zahlungen und jede andere Tabelle außerhalb der Kontaktdaten bleiben unverändert (das Speichern
    eines Kontakts schreibt Adresse und Details wie immer neu)."""
    own = _own(tmp_db)
    _books(tmp_db)
    contact_tables = ('Contacts', 'ContactAddresses', 'CompanyDetails', 'PersonDetails', 'ContactTypeLinks', 'PersonRoles')
    tables = [row[0] for row in query(tmp_db, "SELECT name FROM sqlite_master WHERE type = 'table' "
                                              "AND name NOT LIKE 'sqlite_%' ORDER BY name") if row[0] not in contact_tables]
    assert {'Bookings', 'Invoices', 'InvoicePayments', 'InvoiceItems'} <= set(tables)
    before = {table: query(tmp_db, f'SELECT * FROM "{table}" ORDER BY 1') for table in tables}

    assert handle_update_contact(tmp_db, _post(contact_id=str(own), vat_method='soll'))[0] == 303
    assert handle_update_contact(tmp_db, _post(contact_id=str(own), vat_method='ist'))[0] == 303

    assert {table: query(tmp_db, f'SELECT * FROM "{table}" ORDER BY 1') for table in tables} == before


def test_the_app_books_the_same_entries_with_soll_set_before_the_booking(tmp_db, tmp_path):
    """Mit eingetragener Soll-Versteuerung bucht die App weiter mit dem Zahlungseingang: dieselben Sätze, dieselben Konten."""
    from db import Database
    other = Database(db_name=str(tmp_path / 'ohne.db'))
    _own(other)
    _own(tmp_db, vat_method='soll')
    _books(other)
    _books(tmp_db)

    columns = 'DateBooking, Amount, COA_ID, CounterCOA_ID, TaxRate, TaxAmount, DocumentNumber, Status, BookingType, EntryForm'
    sql = f'SELECT {columns} FROM Bookings ORDER BY ID'
    assert query(tmp_db, sql) == query(other, sql) and len(query(tmp_db, sql)) >= 6
    assert _results(tmp_db)['euer'] == _results(other)['euer']
    assert tmp_db.get_vat_method() == 'soll' and other.get_vat_method() is None
