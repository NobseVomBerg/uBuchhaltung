"""Regressionstests gegen Stored-XSS: vom Nutzer eingegebene Texte müssen beim
Rendern in HTML escaped werden (kein Ausbruch aus Attribut/Textknoten).
"""
from server.pages_contacts import _contact_form
from server.pages_booking_groups import PageBookingGroups

SCRIPT = '<script>alert(1)</script>'
ATTR_BREAK = '"><img src=x onerror=alert(1)>'


def _company_row(db, **over):
    fields = dict(contact_type='customer', entity_type='company',
                  display_name='ACME', company_name='ACME', street='S', city='C')
    fields.update(over)
    db.insert_contact(**fields)
    return list(db.fetch_contacts(entity_type='company'))[0]


def test_contact_form_escapes_script_in_company_name(tmp_db):
    c = _company_row(tmp_db, display_name=SCRIPT, company_name=SCRIPT)
    html = _contact_form(tmp_db, '/x', entity_type='company', c=c)
    assert SCRIPT not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html


def test_contact_form_escapes_attribute_breakout(tmp_db):
    c = _company_row(tmp_db, company_name=ATTR_BREAK, street=ATTR_BREAK)
    html = _contact_form(tmp_db, '/x', entity_type='company', c=c)
    # Weder das schließende Quote+Tag noch das rohe <img> dürfen durchkommen.
    assert ATTR_BREAK not in html
    assert '<img src=x' not in html
    assert '&quot;&gt;&lt;img' in html


def test_contact_form_escapes_person_fields(tmp_db):
    tmp_db.insert_contact(contact_type='customer', entity_type='person',
                          display_name='P', first_name=SCRIPT, last_name=SCRIPT,
                          job_title=SCRIPT, department=SCRIPT)
    c = list(tmp_db.fetch_contacts(entity_type='person'))[0]
    html = _contact_form(tmp_db, '/x', entity_type='person', c=c)
    assert SCRIPT not in html
    assert '&lt;script&gt;' in html


def test_booking_group_description_escaped_in_list_and_form(tmp_db):
    gid = tmp_db.create_booking_group(SCRIPT, None)
    list_html = PageBookingGroups(tmp_db)
    form_html = PageBookingGroups(tmp_db, view_id=gid)
    assert SCRIPT not in list_html
    assert SCRIPT not in form_html
    assert '&lt;script&gt;' in list_html


def test_contact_form_company_dropdown_escaped(tmp_db):
    # Eine Firma mit Payload erscheint im "Zugehörige Firma"-Dropdown des
    # Personen-Formulars und muss dort escaped sein.
    _company_row(tmp_db, display_name=SCRIPT, company_name=SCRIPT)
    tmp_db.insert_contact(contact_type='customer', entity_type='person',
                          display_name='Person', first_name='A', last_name='B')
    c = list(tmp_db.fetch_contacts(entity_type='person'))[0]
    html = _contact_form(tmp_db, '/x', entity_type='person', c=c)
    assert SCRIPT not in html
