# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Regressionstests gegen Stored-XSS: vom Nutzer eingegebene Texte müssen beim
Rendern in HTML escaped werden (kein Ausbruch aus Attribut/Textknoten).
"""
import json
import os

from server.pages_contacts import _contact_form
from server.pages_booking_groups import PageBookingGroups
from server.pages_transactions import PageTransactions
from server.pages_masterdata import PageArticles, PageBankAccounts, PageSkr
from server.pages_assets import PageAssets
from server.pages_invoice import PageInvoice
from server.pages_setup import PageSetup
from server.pages_miscellaneous import PageMiscellaneous

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


# ── Weitere Seiten (XSS-Rollout) ───────────────────────────────────────────────

def test_transactions_list_and_form_escaped(db_with_coa):
    bid = db_with_coa.insert_booking('2026-01-15', 100.0, recipient_client=SCRIPT,
                                     text=SCRIPT, document_number=SCRIPT, booking_type='entry')
    assert SCRIPT not in PageTransactions(db_with_coa)
    assert SCRIPT not in PageTransactions(db_with_coa, edit_transaction_id=bid)


def test_masterdata_articles_accounts_skr_escaped(db_with_coa):
    aid = db_with_coa.insert_article(name=SCRIPT, unit=SCRIPT, unit_price=1,
                                     tax_rate=19, description=SCRIPT)
    db_with_coa.insert_account(name=SCRIPT, holder=SCRIPT, number=SCRIPT,
                               bic=SCRIPT, bank_name=SCRIPT, skr_account=1200)
    assert SCRIPT not in PageArticles(db_with_coa)
    assert SCRIPT not in PageArticles(db_with_coa, edit_article_id=aid)
    assert SCRIPT not in PageBankAccounts(db_with_coa)
    assert SCRIPT not in PageSkr(db_with_coa)


def test_assets_list_and_detail_escaped(tmp_db):
    tmp_db.insert_asset(name=SCRIPT, description=SCRIPT, purchase_date='2026-01-01',
                        purchase_price=1000, useful_life_years=3,
                        depreciation_method='linear', serial_number=SCRIPT,
                        location=SCRIPT, notes=SCRIPT)
    import sqlite3
    con = sqlite3.connect(tmp_db.db_name)
    pid = con.execute('SELECT ID FROM Assets').fetchone()[0]
    con.close()
    assert SCRIPT not in PageAssets(tmp_db)
    assert SCRIPT not in PageAssets(tmp_db, edit_id=pid)


def test_invoice_create_form_html_and_script_context_escaped(db_with_coa):
    # HTML-Kontext + JSON-in-<script>-Kontext (</script>-Breakout).
    db_with_coa.insert_contact(contact_type='own', entity_type='company',
                               display_name='</script><img src=onerror>',
                               company_name='</script><img src=onerror>')
    db_with_coa.insert_contact(contact_type='customer', entity_type='company',
                               display_name=SCRIPT, company_name=SCRIPT)
    db_with_coa.insert_article(name=SCRIPT, unit='Stk.', unit_price=10,
                               tax_rate=19, description=SCRIPT)
    html = PageInvoice(db_with_coa, invoice_id=None)
    assert SCRIPT not in html
    assert '<img src=onerror>' not in html      # JSON-in-script-Breakout neutralisiert


def test_setup_message_escaped(tmp_db):
    assert SCRIPT not in PageSetup(tmp_db, message=SCRIPT)
    assert '&lt;script&gt;' in PageSetup(tmp_db, message=SCRIPT)


def test_miscellaneous_wiso_result_escaped(db_with_coa, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    os.makedirs('data', exist_ok=True)
    result = {
        'imported': 0, 'skipped': 0, 'updated': 0,
        'missing_coa': [SCRIPT], 'missing_skr': [SCRIPT],
        'not_found': [{'zeile': 1, 'datum': '2026-01-01', 'beleg': SCRIPT,
                       'betrag': '1,00', 'text': SCRIPT}],
        'errors': [SCRIPT], 'skipped_rows': [],
    }
    with open(os.path.join('data', 'wiso_import_result.json'), 'w', encoding='utf-8') as f:
        json.dump(result, f)
    html = PageMiscellaneous(db_with_coa)
    assert SCRIPT not in html
    assert '&lt;script&gt;' in html
