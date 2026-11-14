# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Berater- und Mandantennummer der eigenen Firma (``CompanyDetails.DatevConsultantNo``/``DatevClientNo``, 2.3.0).

* Nur das Formular der eigenen Firma führt die Felder „DATEV-Beraternummer“ und „DATEV-Mandantennummer“; ein anderer
  Kontakt zeigt sie nicht und schreibt sie nie (auch nicht, wenn ein Client sie schickt).
* Sie müssen ganze Zahlen aus ASCII-Ziffern ohne führende Null sein (Berater 1001–9999999, Mandant 1–99999): Eine andere
  Eingabe lehnt der Handler mit Klartext auf dem Fehlerweg der Kontakt-Seite ab, ohne etwas zu schreiben; die
  Datenbank-Schicht verweigert sie auch.
* ``update_contact`` verliert sie nicht: Es löscht und schreibt die Firmendaten neu – ohne Angabe bleibt die Nummer.
* ``get_datev_numbers`` liefert beide oder keine; ein per SQL-Konsole gesetzter anderer Wert gilt als nicht eingetragen.
* Auf der Seite *Verschiedenes* steht unter dem Exportformular ein Hinweis, solange die Nummern fehlen.

Alle Namen und Nummern sind erfunden.
"""
import html
import re
from contextlib import closing
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import pytest

from db import datev_keys
from server.app import parse_form
from server.handlers import handle_add_contact, handle_update_contact
from server.pages_contacts import PageContacts
from server.pages_miscellaneous import PageMiscellaneous
from tests.entry_form_helpers import execute, query

HINT = ('Berater- und Mandantennummer fehlen – in den Stammdaten der eigenen Firma eintragen '
        '(Kontakt als Unternehmen angelegt). Der Stapel trägt bis dahin Platzhalter.')


def _post(**fields):
    base = {'entity_type': ['company'], 'type_keys': ['own'], 'company_name': ['Eigene Probe GmbH'],
            'display_name': [''], 'customer_number': [''], 'abbreviation': [''], 'country': ['DE']}
    base.update({name: [value] for name, value in fields.items()})
    return base


def _numbers(db, contact_id=None):
    sql = 'SELECT DatevConsultantNo, DatevClientNo FROM CompanyDetails'
    if contact_id is None:
        return query(db, sql + ' ORDER BY ContactID')
    return query(db, sql + ' WHERE ContactID = ?', (contact_id,))[0]


def _own(db, **numbers):
    return db.insert_contact(contact_type='own', entity_type='company', display_name='Eigene Probe GmbH',
                             company_name='Eigene Probe GmbH', **numbers)


def _error(location):
    return unquote(parse_qs(urlparse(location).query).get('error', [''])[0])


def _field(page, name):
    """Der Wert des Eingabefeldes ``name`` im Formular – ``None``, wenn es das Feld nicht gibt."""
    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
    if match is None:
        return None
    value = re.search(r'value="([^"]*)"', match.group(0))
    return html.unescape(value.group(1)) if value else ''


# ── Das Formular ─────────────────────────────────────────────────────────────

def test_the_form_of_the_own_company_has_both_fields_with_the_stored_values(tmp_db):
    own = _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')
    page = PageContacts(tmp_db, edit_contact_id=own)
    assert _field(page, 'datev_consultant_no') == '4711' and _field(page, 'datev_client_no') == '12'
    assert 'DATEV-Beraternummer' in page and 'DATEV-Mandantennummer' in page


def test_the_fields_are_empty_where_nothing_is_stored(tmp_db):
    page = PageContacts(tmp_db, edit_contact_id=_own(tmp_db))
    assert _field(page, 'datev_consultant_no') == '' and _field(page, 'datev_client_no') == ''


@pytest.mark.parametrize('kind', ['customer', 'supplier', 'other'])
def test_another_contact_does_not_show_the_fields(tmp_db, kind):
    contact = tmp_db.insert_contact(contact_type=kind, entity_type='company', display_name='Anderer', company_name='Anderer')
    page = PageContacts(tmp_db, edit_contact_id=contact)
    assert _field(page, 'datev_consultant_no') is None and 'DATEV-Beraternummer' not in page


def test_the_form_for_a_new_contact_and_the_own_person_do_not_show_them(tmp_db):
    assert _field(PageContacts(tmp_db), 'datev_consultant_no') is None
    person = tmp_db.insert_contact(contact_type='own', entity_type='person', display_name='Erika Probe',
                                   first_name='Erika', last_name='Probe')
    assert _field(PageContacts(tmp_db, edit_contact_id=person), 'datev_consultant_no') is None


def test_a_stored_value_is_masked_in_the_form(tmp_db):
    own = _own(tmp_db)
    execute(tmp_db, 'UPDATE CompanyDetails SET DatevConsultantNo = ? WHERE ContactID = ?',
            ('"><script>alert(1)</script>', own))
    page = PageContacts(tmp_db, edit_contact_id=own)
    assert '<script>alert(1)</script>' not in page and '&quot;&gt;&lt;script&gt;' in page


def test_the_contact_query_carries_the_numbers_at_the_end_without_moving_the_other_columns(tmp_db):
    own = _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')
    row = tmp_db.get_contact_by_id(own)
    assert len(row) == 34 and (row[32], row[33]) == ('4711', '12')
    assert (row['datev_consultant_no'], row['datev_client_no']) == ('4711', '12')
    assert row[3] == 'Eigene Probe GmbH' and row[26] is None and row[31] is None      # die alten Spalten an ihrem Platz
    customer = tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='K', company_name='K')
    assert tuple(tmp_db.get_contact_by_id(customer))[32:] == (None, None)


# ── Der Handler ──────────────────────────────────────────────────────────────

def test_adding_the_own_company_stores_the_numbers(tmp_db):
    status, location = handle_add_contact(tmp_db, _post(datev_consultant_no='4711', datev_client_no='12'))
    assert status == 303 and 'error' not in location
    assert _numbers(tmp_db) == [('4711', '12')] and tmp_db.get_datev_numbers() == ('4711', '12')


def test_updating_the_own_company_changes_and_clears_the_numbers(tmp_db):
    own = _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')

    assert handle_update_contact(tmp_db, _post(contact_id=str(own), datev_consultant_no='1234567',
                                               datev_client_no='99999'))[0] == 303
    assert _numbers(tmp_db, own) == ('1234567', '99999')

    handle_update_contact(tmp_db, _post(contact_id=str(own), datev_consultant_no='', datev_client_no='  '))
    assert _numbers(tmp_db, own) == (None, None) and tmp_db.get_datev_numbers() == (None, None)


@pytest.mark.parametrize('consultant, client', [('1001', '1'), ('9999999', '99999'), ('1002', '99998'), ('10000', '10')])
def test_the_ends_of_the_ranges_are_valid(tmp_db, consultant, client):
    own = _own(tmp_db)
    status, location = handle_update_contact(tmp_db, _post(contact_id=str(own), datev_consultant_no=consultant,
                                                           datev_client_no=client))
    assert status == 303 and 'error' not in location
    assert _numbers(tmp_db, own) == (consultant, client) and tmp_db.get_datev_numbers() == (consultant, client)


def test_the_form_fields_ask_for_numbers_without_a_leading_zero(tmp_db):
    page = PageContacts(tmp_db, edit_contact_id=_own(tmp_db))
    assert 'pattern="[1-9][0-9]{3,6}"' in page and 'pattern="[1-9][0-9]{0,4}"' in page
    assert 'placeholder="1001–9999999"' in page and 'placeholder="1–99999"' in page


def test_a_browser_body_with_blank_fields_clears_the_numbers_and_one_without_them_keeps_them(tmp_db):
    """Der Weg des Browsers: ``parse_form`` behält leere Felder – ein geleertes Feld kommt als ``['']`` an und leert die
    Nummer, ein Formular ohne die Felder (``None``) lässt sie."""
    own = _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')
    form = {'contact_id': str(own), 'entity_type': 'company', 'type_keys': 'own', 'company_name': 'Eigene Probe GmbH',
            'display_name': '', 'customer_number': '', 'abbreviation': '', 'country': 'DE'}

    assert handle_update_contact(tmp_db, parse_form(urlencode(form)))[0] == 303
    assert _numbers(tmp_db, own) == ('4711', '12')

    blank = parse_form(urlencode({**form, 'datev_consultant_no': '', 'datev_client_no': ''}))
    assert blank['datev_consultant_no'] == [''] and blank['datev_client_no'] == ['']
    assert handle_update_contact(tmp_db, blank)[0] == 303
    assert _numbers(tmp_db, own) == (None, None)

    filled = parse_form(urlencode({**form, 'datev_consultant_no': '1234567', 'datev_client_no': '99999'}))
    assert handle_update_contact(tmp_db, filled)[0] == 303
    assert _numbers(tmp_db, own) == ('1234567', '99999')


def test_a_form_without_the_fields_leaves_the_stored_numbers(tmp_db):
    """Das Formular eines anderen Browserstands oder Clients führt die Felder nicht: Die Nummern bleiben."""
    own = _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')
    assert handle_update_contact(tmp_db, _post(contact_id=str(own), company_name='Umbenannt GmbH'))[0] == 303
    assert _numbers(tmp_db, own) == ('4711', '12')
    assert query(tmp_db, 'SELECT CompanyName FROM CompanyDetails WHERE ContactID = ?', (own,))[0][0] == 'Umbenannt GmbH'


@pytest.mark.parametrize('consultant, client, message', [
    ('123', '12', datev_keys.CONSULTANT_NO_ERROR), ('12345678', '12', datev_keys.CONSULTANT_NO_ERROR),
    ('47x1', '12', datev_keys.CONSULTANT_NO_ERROR), ('47 11', '12', datev_keys.CONSULTANT_NO_ERROR),
    ('4711;', '12', datev_keys.CONSULTANT_NO_ERROR), ('4711"', '12', datev_keys.CONSULTANT_NO_ERROR),
    ('-4711', '12', datev_keys.CONSULTANT_NO_ERROR), ('４７１１', '12', datev_keys.CONSULTANT_NO_ERROR),
    ('4711', '123456', datev_keys.CLIENT_NO_ERROR), ('4711', 'a', datev_keys.CLIENT_NO_ERROR),
    ('4711', '1 2', datev_keys.CLIENT_NO_ERROR), ('4711', '1;2', datev_keys.CLIENT_NO_ERROR),
    ('4711', '-1', datev_keys.CLIENT_NO_ERROR),
    ('1000', '12', datev_keys.CONSULTANT_NO_ERROR), ('0999', '12', datev_keys.CONSULTANT_NO_ERROR),
    ('0000', '12', datev_keys.CONSULTANT_NO_ERROR), ('0', '12', datev_keys.CONSULTANT_NO_ERROR),
    ('01234', '12', datev_keys.CONSULTANT_NO_ERROR), ('10000000', '12', datev_keys.CONSULTANT_NO_ERROR),
    ('4711', '0', datev_keys.CLIENT_NO_ERROR), ('4711', '00', datev_keys.CLIENT_NO_ERROR),
    ('4711', '00000', datev_keys.CLIENT_NO_ERROR), ('4711', '012', datev_keys.CLIENT_NO_ERROR),
    ('4711', '100000', datev_keys.CLIENT_NO_ERROR)])
def test_other_input_than_digits_of_the_right_length_is_refused_with_a_plain_text_and_nothing_is_written(
        tmp_db, consultant, client, message):
    own = _own(tmp_db, datev_consultant_no='1001', datev_client_no='1')
    status, location = handle_update_contact(tmp_db, _post(contact_id=str(own), company_name='Nicht geschrieben',
                                                            datev_consultant_no=consultant, datev_client_no=client))
    assert status == 303 and _error(location) == message
    assert location.startswith(f'/masterdata/contacts/edit?id={own}&error=')           # das Formular öffnet wieder
    assert _numbers(tmp_db, own) == ('1001', '1')
    assert query(tmp_db, 'SELECT CompanyName FROM CompanyDetails WHERE ContactID = ?', (own,))[0][0] == 'Eigene Probe GmbH'

    status, location = handle_add_contact(tmp_db, _post(datev_consultant_no=consultant, datev_client_no=client))
    assert status == 303 and _error(location) == message and location.startswith('/masterdata/contacts?error=')
    assert len(_numbers(tmp_db)) == 1                                                   # keine zweite Firma entstanden


def test_surrounding_blanks_of_the_input_do_not_count(tmp_db):
    own = _own(tmp_db)
    handle_update_contact(tmp_db, _post(contact_id=str(own), datev_consultant_no=' 4711 ', datev_client_no='\t12\n'))
    assert _numbers(tmp_db, own) == ('4711', '12')


@pytest.mark.parametrize('kind', ['customer', 'supplier'])
def test_another_contact_never_gets_the_numbers_even_when_a_client_sends_them(tmp_db, kind):
    status, _location = handle_add_contact(tmp_db, dict(_post(datev_consultant_no='4711', datev_client_no='12'),
                                                        type_keys=[kind]))
    assert status == 303
    assert _numbers(tmp_db) == [(None, None)]


def test_leaving_the_type_own_clears_the_numbers_and_joining_it_does_not_set_them(tmp_db):
    own = _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')
    handle_update_contact(tmp_db, dict(_post(contact_id=str(own)), type_keys=['customer']))
    assert _numbers(tmp_db, own) == (None, None)
    handle_update_contact(tmp_db, dict(_post(contact_id=str(own), datev_consultant_no='4711', datev_client_no='12'),
                                       type_keys=['own']))
    assert _numbers(tmp_db, own) == ('4711', '12')


# ── Die Datenbank-Schicht ────────────────────────────────────────────────────

@pytest.mark.parametrize('kwargs, message', [
    ({'datev_consultant_no': '12'}, datev_keys.CONSULTANT_NO_ERROR), ({'datev_client_no': 'x'}, datev_keys.CLIENT_NO_ERROR)])
def test_the_database_layer_refuses_invalid_numbers_too(tmp_db, kwargs, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        _own(tmp_db, **kwargs)
    own = _own(tmp_db)
    with pytest.raises(ValueError, match=re.escape(message)):
        tmp_db.update_contact(own, contact_type='own', entity_type='company', display_name='X', company_name='X',
                              **kwargs)
    assert _numbers(tmp_db) == [(None, None)]


def test_update_contact_without_the_arguments_keeps_the_numbers_and_with_empty_ones_clears_them(tmp_db):
    own = _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')
    tmp_db.update_contact(own, contact_type='own', entity_type='company', display_name='Neu', company_name='Neu')
    assert _numbers(tmp_db, own) == ('4711', '12')
    tmp_db.update_contact(own, contact_type='own', entity_type='company', display_name='Neu', company_name='Neu',
                          datev_client_no='')
    assert _numbers(tmp_db, own) == ('4711', None)


def test_the_numbers_may_be_given_as_whole_numbers(tmp_db):
    own = _own(tmp_db, datev_consultant_no=4711, datev_client_no=12)
    assert _numbers(tmp_db, own) == ('4711', '12')


def test_turning_the_company_into_a_person_drops_the_company_details_with_the_numbers(tmp_db):
    own = _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')
    tmp_db.update_contact(own, contact_type='own', entity_type='person', display_name='Erika Probe',
                          first_name='Erika', last_name='Probe', datev_consultant_no='4711', datev_client_no='12')
    assert _numbers(tmp_db) == [] and tmp_db.get_datev_numbers() == (None, None)


# ── get_datev_numbers ────────────────────────────────────────────────────────

def test_get_datev_numbers_needs_both_valid_numbers(tmp_db):
    own = _own(tmp_db)
    assert tmp_db.get_datev_numbers() == (None, None)
    for stored in (('4711', None), (None, '12'), ('471', '12'), ('4711', '123456'), ('47x1', '12'), ('4711 ', '12'),
                   ('4711', '1;2'), ('', ''), (' ', ' '), ('1000', '12'), ('0999', '12'), ('0000', '12'),
                   ('4711', '0'), ('4711', '00'), ('4711', '012'), ('4711', '100000'), ('10000000', '12')):
        execute(tmp_db, 'UPDATE CompanyDetails SET DatevConsultantNo = ?, DatevClientNo = ? WHERE ContactID = ?',
                (*stored, own))
        assert tmp_db.get_datev_numbers() == (None, None), stored
    execute(tmp_db, 'UPDATE CompanyDetails SET DatevConsultantNo = ?, DatevClientNo = ? WHERE ContactID = ?',
            ('1234567', '99999', own))
    assert tmp_db.get_datev_numbers() == ('1234567', '99999')


def test_get_datev_numbers_reads_only_the_own_company(tmp_db):
    customer = tmp_db.insert_contact(contact_type='customer', entity_type='company', display_name='K', company_name='K')
    execute(tmp_db, 'UPDATE CompanyDetails SET DatevConsultantNo = ?, DatevClientNo = ? WHERE ContactID = ?',
            ('4711', '12', customer))
    assert tmp_db.get_datev_numbers() == (None, None)


def test_get_datev_numbers_takes_the_first_own_company_with_both_numbers(tmp_db):
    first, second = _own(tmp_db), _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')
    assert tmp_db.get_datev_numbers() == ('4711', '12')
    execute(tmp_db, 'UPDATE CompanyDetails SET DatevConsultantNo = ?, DatevClientNo = ? WHERE ContactID = ?',
            ('2222', '2', first))
    assert tmp_db.get_datev_numbers() == ('2222', '2') and second


# ── Der Hinweis auf der Seite ────────────────────────────────────────────────

def test_the_page_names_the_missing_numbers_under_the_export_form(tmp_db):
    page = PageMiscellaneous(tmp_db)
    assert html.escape(HINT, quote=True) in page
    assert page.index('DATEV-CSV exportieren') < page.index('Berater- und Mandantennummer fehlen')
    assert 'class="hint warnColor"' in page


def test_the_page_has_no_hint_with_both_numbers(tmp_db):
    _own(tmp_db, datev_consultant_no='4711', datev_client_no='12')
    assert 'Berater- und Mandantennummer fehlen' not in PageMiscellaneous(tmp_db)


@pytest.mark.parametrize('stored', [('4711', None), ('47', '12'), ('4711', '123456'), ('<b>', '1'), ('1000', '12'),
                                    ('0999', '12'), ('4711', '0'), ('4711', '012')])
def test_the_page_names_the_numbers_as_missing_when_one_is_missing_or_invalid(tmp_db, stored):
    own = _own(tmp_db)
    execute(tmp_db, 'UPDATE CompanyDetails SET DatevConsultantNo = ?, DatevClientNo = ? WHERE ContactID = ?',
            (*stored, own))
    page = PageMiscellaneous(tmp_db)
    assert 'Berater- und Mandantennummer fehlen' in page and '<b>' not in page.split('Berater- und')[0][-200:]


def test_the_export_text_no_longer_promises_the_tax_period_field(tmp_db):
    """Spalte 115 (Datum Zuord. Steuerperiode) bleibt leer; gesetzt wird weiter das Steuerdatum der Buchungen."""
    page = PageMiscellaneous(tmp_db)
    assert 'Datum Zuord. Steuerperiode' not in page and 'Steuerdatum' in page
