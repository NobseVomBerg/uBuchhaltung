# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Seite „Steuersätze“ der Stammdaten (2.8.0, Etappe E2): ``GET /masterdata/taxrates``, ihre Formulare und Aktionen – gegen einen echten Server.

* Die Liste nennt Bezeichnung, Satz, Art, Befreiungsgrund (gekürzt), Konten und das abgeleitete Wartekonto (nur die Nummer aus ``OPEN_REVENUE_ACCOUNTS``, für genau diesen
  Satz, sonst „keines – gebucht wird erst mit der Zahlung“); inaktive und ungültige Einträge tragen ihr festes Wort. Die festen Hinweise stehen da, ohne einen Satz im Text.
* Jeder Wert aus der Datenbank ist maskiert, die Meldung einer Aktion (``?err=``) kommt als JSON im Skriptblock und erscheint nur über ``appMsg``.
* Eine Seite, die nur liest, schreibt nichts (Abzug der Datenbank vorher und nachher gleich).
* Hinzufügen und Ändern: der Satz wird streng als Text gelesen (``5,5`` und ``5.5``), jeder Verstoß gegen die Regeln gibt genau seinen Satz auf der Seite und speichert nichts;
  eine ungültige ID ist 400. Umschalten und Löschen sind POST-Aktionen (GET: 405); der letzte wählbare Eintrag bleibt, sonst geht das Löschen immer.
* Der Fuß der Seite hat keinen Inline-Stil, und ein Konto, das ein Eintrag nennt, lässt sich im Kontenrahmen nicht löschen.
* Die Auswahl „Erlöskonto“ führt die Skonto-Konten nicht (``Database.payment_revenue_choices``: eine Erlösschmälerung ist kein Erlöskonto, das ein Satz tragen sollte); ein Eintrag, der
  eines trägt (er bleibt gültig und funktioniert weiter), zeigt es als gewählte Option, und das Speichern ändert es nicht.

Alle Bezeichnungen, Gründe, Konten und Nummern sind erfundene Testwerte.
"""
import contextlib
import html
import re
import sqlite3
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from db import coa_id
from db.invoice_payments import SKONTO_ACCOUNT_NUMBERS
from server import handlers, pages_tax_rates
from server.pages_tax_rates import ACCOUNTS_HINT, RATES_HINT, WAITING_NONE
from tax_groups import (TAX_ENTRY_EXEMPT_RATE, TAX_ENTRY_INPUT_TAX_ACCOUNT, TAX_ENTRY_KIND, TAX_ENTRY_LABEL, TAX_ENTRY_LAST_ACTIVE, TAX_ENTRY_RATE,
                        TAX_ENTRY_REASON, TAX_ENTRY_REVENUE_NOT_INCOME, TAX_ENTRY_REVENUE_REQUIRED, TAX_ENTRY_TAX_ACCOUNT_FIXED,
                        TAX_ENTRY_TAX_ACCOUNT_ZERO, TAX_ENTRY_VAT_ACCOUNT)
from tests.dom_helpers import Dom, app_msg_arguments, assert_same_structure
from tests.http_client import FOREIGN_ORIGIN, get, own_origin, post

LIST = '/masterdata/taxrates'
REASON = 'Beispielhafter Befreiungsgrund (erfunden)'
COLUMNS = 'ID, Label, RateBp, Kind, ExemptReason, RevenueCoaId, VatCoaId, InputTaxCoaId, Active, SortOrder'


# ── Hilfen ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def world(auto_login):
    """Der angemeldete Administrator mit seiner Datenbank und den Konten der Tests (aus dem Kontenrahmen und zwei angelegte im Bereich der Steuerkonten)."""
    db = auto_login.db
    for number, name in ((1430, 'Vorsteuer Testsatz'), (3845, 'Umsatzsteuer Testsatz'), (4711, 'Erlöse Testsatz')):
        assert db.insert_chart_of_accounts(4, number, name, 'Testkonten', is_standard=0)
    return SimpleNamespace(
        db=db, csrf=auto_login.csrf, revenue=db.get_coa_id_by_account_number(4400), expense=db.get_coa_id_by_account_number(6815),
        input_ok=db.get_coa_id_by_account_number(1406), test_input=coa_id(4, 1430), test_vat=coa_id(4, 3845), test_revenue=coa_id(4, 4711))


def rows(db):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(f'SELECT {COLUMNS} FROM TaxRates ORDER BY ID').fetchall()


def dump(db):
    """Alle Zeilen aller Tabellen, roh – zum Vergleich vorher/nachher."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        tables = sorted(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        return {table: con.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall() for table in tables}


def submit(port, world, path, fields=None, origin=True):
    headers = {'Origin': own_origin(port)} if origin else {}
    return post(port, path, headers=headers, form=fields, csrf=world.csrf)


def form(world, **over):
    """Die Felder des Formulars für einen gültigen Eintrag über 5,5 % mit Erlöskonto – ``over`` ändert einzelne."""
    return {**dict(label='Ermäßigt (erfunden)', rate='5,5', kind='standard', reason='', revenue_coa_id=str(world.revenue), vat_coa_id='', input_tax_coa_id='',
                   sort_order='30', active_field='1', active='1'), **over}


def err_location(sentence, path=LIST):
    return f"{path}{'&' if '?' in path else '?'}err={quote(sentence, safe='')}"


def without_scripts(text):
    return re.sub(r'<script\b.*?</script>', '', text, flags=re.S)


def table_rows(text):
    """Die Zeilen der Liste (ohne Kopf) als Texte ihrer Zellen – Markup entfernt, Entitäten aufgelöst."""
    table = re.search(r'<table>(.*?)</table>', text, re.S).group(1)
    result = []
    for tr in re.findall(r'<tr>(.*?)</tr>', table, re.S)[1:]:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        result.append([html.unescape(re.sub(r'<[^>]+>', '', cell)).replace('\u00a0', ' ').strip() for cell in cells])     # geschützte Leerzeichen halten Satz und Symbole zusammen
    return result


def options(text, name):
    """``[(Wert, vorgewählt)]`` der Auswahl ``name`` im Formular."""
    block = re.search(rf'<select name="{name}">(.*?)</select>', text, re.S).group(1)
    return [(html.unescape(value), bool(selected)) for value, selected in re.findall(r'<option value="([^"]*)"( selected)?>', block)]


def shown_message(text):
    return app_msg_arguments(Dom(text))


# ── Die Liste ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_page_lists_the_presets_with_their_waiting_accounts_and_the_form_for_a_new_entry(server_port, world):
    response = get(server_port, LIST)

    assert response.status == 200
    assert 'Neuer Steuersatz' in response.text
    waiting = world.db.OPEN_REVENUE_ACCOUNTS
    assert table_rows(response.text) == [
        ['19 %', '19 %', 'Standard', '–', 'Standard', '–', '–', str(waiting[19][0]), '✎ ☑ 🗑'],
        ['7 %', '7 %', 'Standard', '–', 'Standard', '–', '–', str(waiting[7][0]), '✎ ☑ 🗑']]


def test_the_list_names_kind_reason_accounts_and_the_waiting_account_of_each_entry(server_port, world):
    world.db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=world.revenue, vat_coa_id=world.test_vat, input_tax_coa_id=world.test_input)
    world.db.insert_tax_rate(label='Steuerfrei (erfunden)', bp=0, kind='exempt', reason=REASON, revenue_coa_id=world.test_revenue)
    world.db.insert_tax_rate(label='Nullsatz (erfunden)', bp=0, revenue_coa_id=world.revenue)
    world.db.insert_tax_rate(label='Satz mit Komma (erfunden)', bp=1070, revenue_coa_id=world.revenue)

    listed = table_rows(get(server_port, LIST).text)

    waiting = world.db.OPEN_REVENUE_ACCOUNTS
    assert [row[:8] for row in listed[2:]] == [
        ['Ermäßigt (erfunden)', '5,5 %', 'Standard', '–', '4400 Erlöse 19% USt', '3845 Umsatzsteuer Testsatz', '1430 Vorsteuer Testsatz', WAITING_NONE],
        ['Steuerfrei (erfunden)', '0 %', 'Steuerfrei', REASON, '4711 Erlöse Testsatz', '–', '–', str(waiting[0][0])],
        ['Nullsatz (erfunden)', '0 %', 'Standard', '–', '4400 Erlöse 19% USt', '–', '–', str(waiting[0][0])],
        ['Satz mit Komma (erfunden)', '10,7 %', 'Standard', '–', '4400 Erlöse 19% USt', '–', '–', WAITING_NONE]]


def test_the_waiting_account_comes_from_the_table_of_the_invoices_for_exactly_the_rate_of_the_entry(server_port, world):
    """4405, 4345 und 4340 stehen nicht im Seitentext, sondern kommen aus ``OPEN_REVENUE_ACCOUNTS`` – für 19 und 7 Prozent, für 0 (steuerfrei) und für keinen anderen Satz
    (nicht auf ganze Prozent gerundet: 7,4 % hat keines)."""
    world.db.insert_tax_rate(label='Fast sieben (erfunden)', bp=740, revenue_coa_id=world.revenue)
    source = open(pages_tax_rates.__file__, encoding='utf-8').read()

    listed = table_rows(get(server_port, LIST).text)

    assert [row[7] for row in listed] == ['4405', '4345', WAITING_NONE]
    assert not re.search(r'\b4[34]\d\d\b', source)                                   # keine Kontonummer im Quelltext der Seite


def test_the_hints_are_the_fixed_sentences_and_the_fixed_rates_come_from_the_trial_balance_table(server_port, world):
    text = html.unescape(get(server_port, LIST).text)

    assert RATES_HINT == ('Welcher Satz und welcher Befreiungsgrund gelten, klärt bitte das Steuerbüro. Änderungen wirken auf neue und neu gespeicherte Entwürfe – '
                          'versendete Rechnungen bleiben, wie sie sind.')
    assert ACCOUNTS_HINT == ('Ohne Steuerkonto steht die Steuer dieses Satzes in der Summen- und Saldenliste unter ‚Steuer ohne Steuerkonto‘ und in der EÜR unter '
                             '‚Vorsteuer ohne Steuerkonto‘. Für 19, 16, 7 und 5 % stehen die Steuerkonten fest.')
    assert RATES_HINT in text and ACCOUNTS_HINT in text
    assert WAITING_NONE == 'keines – gebucht wird erst mit der Zahlung'


def test_an_inactive_entry_and_an_invalid_one_carry_their_word_and_an_invalid_one_has_no_waiting_account(server_port, world):
    world.db.set_tax_rate_active(2, False)
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:
        con.execute("INSERT INTO TaxRates (Label, RateBp, Kind, Active, SortOrder) VALUES ('Kaputt (erfunden)', 'abc', 'free', 1, 30)")
        con.commit()

    listed = table_rows(get(server_port, LIST).text)

    assert [(row[0], row[1], row[2], row[7]) for row in listed] == [
        ('19 %', '19 %', 'Standard', '4405'), ('7 % inaktiv', '7 %', 'Standard', '4345'), ('Kaputt (erfunden) ungültig', 'abc', 'free', '–')]


def test_a_hostile_stored_value_is_escaped_in_the_list_the_form_and_the_attributes(server_port, world):
    hostile = '<script>alert(1)</script> "q" \'s\' &amp;'
    entry = world.db.insert_tax_rate(label=hostile[:60], bp=0, kind='exempt', reason=hostile * 2, revenue_coa_id=world.revenue)
    page = get(server_port, LIST)
    edit = get(server_port, f'{LIST}/edit?id={entry}')
    world.db.update_tax_rate(entry, label='Beispiel', bp=0, kind='exempt', reason='Beispiel', revenue_coa_id=world.revenue)         # dieselbe Zeile, harmlos
    benign_page, benign_edit = get(server_port, LIST), get(server_port, f'{LIST}/edit?id={entry}')

    for text in (page.text, edit.text):
        assert '<script>alert(1)' not in text and 'alert(1)</script>' not in text
        assert html.escape(hostile[:60], quote=True) in text
    assert_same_structure(page.text, benign_page.text)
    assert_same_structure(edit.text, benign_edit.text)
    dom = Dom(edit.text)
    assert dom.one('input', name='label')['value'] == hostile[:60]
    assert dom.one('input', name='reason')['value'] == hostile * 2


def test_a_row_with_garbage_values_renders_and_shows_them_escaped(server_port, world):
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:
        con.execute("UPDATE TaxRates SET Kind = '<b>x</b>', RateBp = '<i>abc</i>', RevenueCoaId = 'text', VatCoaId = 777777 WHERE ID = 2")
        con.commit()

    page = get(server_port, LIST)
    edit = get(server_port, f'{LIST}/edit?id=2')

    assert page.status == 200 and edit.status == 200
    assert '<b>x</b>' not in page.text and '&lt;b&gt;x&lt;/b&gt;' in page.text
    assert '&lt;i&gt;abc&lt;/i&gt;' in page.text
    assert 'Konto 777777 (nicht im Kontenrahmen)' in html.unescape(page.text)


def test_the_page_has_no_inline_style_and_no_account_number_of_its_own():
    source = open(pages_tax_rates.__file__, encoding='utf-8').read()

    assert 'style=' not in source and 'style:' not in source


# ── Die Meldung einer Aktion ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('path', [LIST, f'{LIST}/edit?id=1'])
def test_a_message_in_the_address_is_json_in_the_script_block_shown_only_through_appmsg(server_port, world, path):
    message = '</script><script>alert(1)</script> "x" & <b>fett</b>'
    separator = '&' if '?' in path else '?'

    page = get(server_port, f'{path}{separator}err={quote(message, safe="")}')

    assert page.status == 200
    assert shown_message(page.text) == [message, 'error']
    assert message not in without_scripts(page.text) and 'alert(1)' not in without_scripts(page.text)
    assert_same_structure(page.text, get(server_port, f'{path}{separator}err=x').text)


def test_a_page_without_a_message_shows_none(server_port, world):
    with pytest.raises(AssertionError, match='kein appMsg-Aufruf'):
        shown_message(get(server_port, LIST).text)


# ── Eine Seite, die nur liest, schreibt nichts ─────────────────────────────────────────────────────────────────────────────────

def test_reading_the_pages_writes_nothing(server_port, world):
    world.db.insert_tax_rate(label='Steuerfrei (erfunden)', bp=0, kind='exempt', reason=REASON, revenue_coa_id=world.revenue)
    before = dump(world.db)

    for path in (LIST, f'{LIST}?err=x', f'{LIST}/edit?id=1', f'{LIST}/edit?id=3', f'{LIST}/edit?id=99999', f'{LIST}/edit?id=abc', f'{LIST}/edit', '/masterdata'):
        assert get(server_port, path).status == 200, path

    assert dump(world.db) == before


def test_an_unknown_or_missing_id_opens_the_empty_form_like_a_new_entry(server_port, world):
    for query in ('', '?id=abc', '?id=', '?id=99999', f'?id={2 ** 63}'):
        text = get(server_port, f'{LIST}/edit{query}').text
        assert 'Neuer Steuersatz' in text and 'Steuersatz bearbeiten' not in text, query
        assert Dom(text).all('form')[0]['action'] == '/masterdata/taxrates/update'
        assert 'name="id"' not in text


def test_the_overview_and_the_submenu_of_the_master_data_link_to_the_page(server_port, world):
    text = get(server_port, '/masterdata').text

    assert text.count('/masterdata/taxrates') == 2
    assert "<a href='/masterdata/taxrates'>🧾 Steuersätze</a>" in text
    assert '→ Zu den Steuersätzen' in text


# ── Das Formular ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_form_of_a_new_entry_offers_the_revenue_accounts_and_only_the_tax_accounts_of_their_groups(server_port, world):
    text = get(server_port, LIST).text
    db = world.db

    assert [value for value, _ in options(text, 'revenue_coa_id')] == [''] + [str(row[0]) for row in db.payment_revenue_choices()]
    assert [value for value, _ in options(text, 'vat_coa_id')] == [''] + [str(row[0]) for row in db.get_tax_account_choices()['vat']]
    assert [value for value, _ in options(text, 'input_tax_coa_id')] == [''] + [str(row[0]) for row in db.get_tax_account_choices()['input']]
    assert str(world.test_vat) in [value for value, _ in options(text, 'vat_coa_id')] and str(world.test_input) in [value for value, _ in options(text, 'input_tax_coa_id')]
    assert str(world.revenue) not in [value for value, _ in options(text, 'input_tax_coa_id')] + [value for value, _ in options(text, 'vat_coa_id')]
    assert not any(selected for _value, selected in options(text, 'vat_coa_id')[1:]) and options(text, 'vat_coa_id')[0] == ('', True)
    assert re.search(r'<option value="" selected>Standard \(nur 19 % und 7 %\)</option>', text) and '– keines –' in text


def test_the_form_of_a_new_entry_suggests_the_next_place_checks_the_box_and_adds_with_its_own_button(server_port, world):
    text = get(server_port, LIST).text
    dom = Dom(text)

    assert dom.one('input', name='sort_order')['value'] == '30'
    assert 'checked' in dom.one('input', name='active')
    assert dom.one('input', name='active_field')['value'] == '1'
    assert dom.one('input', type='submit')['formaction'] == '/masterdata/taxrates/add'
    assert dom.one('form')['action'] == '/masterdata/taxrates/update'
    assert dom.one('input', name='label')['maxlength'] == '60' and dom.one('input', name='reason')['maxlength'] == '300'


def test_the_form_of_an_existing_entry_shows_what_is_stored(server_port, world):
    entry = world.db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=world.revenue, vat_coa_id=world.test_vat, input_tax_coa_id=world.test_input,
                                     sort_order=41, active=False)

    text = get(server_port, f'{LIST}/edit?id={entry}').text
    dom = Dom(text)

    assert 'Steuersatz bearbeiten' in text and 'Neuer Steuersatz' not in text
    assert dom.one('input', name='id')['value'] == str(entry)
    assert dom.one('input', name='label')['value'] == 'Ermäßigt (erfunden)'
    assert dom.one('input', name='rate')['value'] == '5,5'
    assert dom.one('input', name='sort_order')['value'] == '41'
    assert 'checked' not in dom.one('input', name='active')
    assert [value for value, selected in options(text, 'kind') if selected] == ['standard']
    assert [value for value, selected in options(text, 'revenue_coa_id') if selected] == [str(world.revenue)]
    assert [value for value, selected in options(text, 'vat_coa_id') if selected] == [str(world.test_vat)]
    assert [value for value, selected in options(text, 'input_tax_coa_id') if selected] == [str(world.test_input)]
    assert 'formaction' not in text and '← Abbrechen' in text
    assert dom.one('input', type='submit')['value'] == 'Aktualisieren'


def test_the_form_of_a_tax_free_entry_shows_the_kind_and_the_reason(server_port, world):
    entry = world.db.insert_tax_rate(label='Steuerfrei (erfunden)', bp=0, kind='exempt', reason=REASON, revenue_coa_id=world.revenue)

    text = get(server_port, f'{LIST}/edit?id={entry}').text

    assert [value for value, selected in options(text, 'kind') if selected] == ['exempt']
    assert Dom(text).one('input', name='reason')['value'] == REASON
    assert Dom(text).one('input', name='rate')['value'] == '0'


def test_a_stored_account_the_chart_no_longer_offers_stays_selected_so_that_saving_refuses_it_with_the_rule(server_port, world):
    entry = world.db.insert_tax_rate(**dict(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=world.revenue))
    with contextlib.closing(sqlite3.connect(world.db.db_name)) as con:
        con.execute('UPDATE TaxRates SET RevenueCoaId = ? WHERE ID = ?', (world.expense, entry))
        con.commit()

    text = get(server_port, f'{LIST}/edit?id={entry}').text

    selected = [value for value, chosen in options(text, 'revenue_coa_id') if chosen]
    assert selected == [str(world.expense)]
    response = submit(server_port, world, f'{LIST}/update', form(world, id=str(entry), revenue_coa_id=selected[0]))
    assert response.status == 303 and response.headers['Location'] == err_location(TAX_ENTRY_REVENUE_NOT_INCOME, f'{LIST}/edit?id={entry}')


def test_the_revenue_choice_offers_no_discount_account_but_every_other_revenue_account(server_port, world):
    """4730, 4731 und 4736 sind Erlösschmälerungen: Ein Satz, der eines als Erlöskonto trüge, buchte seinen Erlös dorthin (``tests/test_skonto_revenue_account.py``). Die Auswahl führt sie nicht."""
    offered = [value for value, _ in options(get(server_port, LIST).text, 'revenue_coa_id')]
    discount = {str(world.db.get_coa_id_by_account_number(number)) for number in SKONTO_ACCOUNT_NUMBERS}

    assert None not in {world.db.get_coa_id_by_account_number(number) for number in SKONTO_ACCOUNT_NUMBERS}      # der Standard-Kontenrahmen führt alle drei
    assert not discount & set(offered)
    assert {str(world.revenue), str(world.test_revenue)} <= set(offered)
    assert len(offered) == len(world.db.get_revenue_coas()) + 1 - len(discount)                                # die leere Option plus alle Erlöskonten ohne die drei


def test_an_entry_that_carries_a_discount_account_keeps_it_selected_and_saves_unchanged(server_port, world):
    """Ein vorhandener Eintrag mit 4736 bleibt gültig und funktioniert weiter: Das Formular zeigt das Konto als eigene, gewählte Option (nichts wird still umgestellt), und das Speichern ändert es nicht."""
    discount = world.db.get_coa_id_by_account_number(4736)
    entry = world.db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=discount)

    text = get(server_port, f'{LIST}/edit?id={entry}').text

    assert [value for value, chosen in options(text, 'revenue_coa_id') if chosen] == [str(discount)]
    assert f'<option value="{discount}" selected>4736 Gewährte Skonti 19 % USt</option>' in text
    assert [value for value, _ in options(text, 'revenue_coa_id')][:-1] == [''] + [str(row[0]) for row in world.db.payment_revenue_choices()]      # die Auswahl, dahinter das gespeicherte Konto
    response = submit(server_port, world, f'{LIST}/update', form(world, id=str(entry), revenue_coa_id=str(discount), sort_order='5'))
    assert (response.status, response.headers['Location']) == (303, LIST)
    assert rows(world.db)[2][5] == discount and world.db.get_tax_rate(entry).valid
    assert '<td>4736 Gewährte Skonti 19 % USt</td>' in html.unescape(get(server_port, LIST).text)           # die Liste nennt es weiter


# ── Hinzufügen ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('rate, bp', [('5,5', 550), ('5.5', 550), ('5,50', 550), ('5', 500), ('10,7', 1070), ('100', 10000), ('1', 100), ('0', 0)])
def test_the_rate_is_read_strictly_as_text_with_comma_or_point(server_port, world, rate, bp):
    over = dict(rate=rate, revenue_coa_id=str(world.revenue))
    response = submit(server_port, world, f'{LIST}/add', form(world, **over))

    assert response.status == 303 and response.headers['Location'] == LIST
    assert rows(world.db)[2][2] == bp


def test_an_entry_is_added_with_everything_the_form_sends_and_the_page_shows_it(server_port, world):
    response = submit(server_port, world, f'{LIST}/add', form(world, label='  Ermäßigt (erfunden)  ', vat_coa_id=str(world.test_vat),
                                                              input_tax_coa_id=str(world.test_input), sort_order='55'))

    assert (response.status, response.headers['Location']) == (303, LIST)
    assert rows(world.db)[2] == (3, 'Ermäßigt (erfunden)', 550, 'standard', None, world.revenue, world.test_vat, world.test_input, 1, 55)
    assert table_rows(get(server_port, LIST).text)[2][:3] == ['Ermäßigt (erfunden)', '5,5 %', 'Standard']


def test_a_tax_free_entry_is_added_with_its_reason_stripped_of_blanks(server_port, world):
    response = submit(server_port, world, f'{LIST}/add', form(world, label='Steuerfrei (erfunden)', rate='0', kind='exempt', reason=f'  {REASON}  '))

    assert response.status == 303
    assert rows(world.db)[2][1:5] == ('Steuerfrei (erfunden)', 0, 'exempt', REASON)


def test_an_empty_sort_order_puts_the_entry_at_the_end_and_a_missing_flag_field_means_active(server_port, world):
    fields = form(world, sort_order='')
    del fields['active_field'], fields['active']

    submit(server_port, world, f'{LIST}/add', fields)

    assert rows(world.db)[2][8:] == (1, 30)


def test_the_flag_field_of_the_form_decides_active_a_box_that_is_not_ticked_sends_nothing(server_port, world):
    fields = form(world)
    del fields['active']                                                       # ``active_field`` = 1 sagt: Das Formular führt das Kennzeichen, das Kästchen ist nicht angehakt

    submit(server_port, world, f'{LIST}/add', fields)

    assert rows(world.db)[2][8] == 0


# Jede Regel mit genau ihrem Satz, über das Formular: (Änderung der Felder, der feste Satz)
FORM_REFUSALS = [
    pytest.param(dict(label=''), TAX_ENTRY_LABEL, id='Bezeichnung leer'),
    pytest.param(dict(label='   '), TAX_ENTRY_LABEL, id='Bezeichnung nur Leerraum'),
    pytest.param(dict(label='x' * 61), TAX_ENTRY_LABEL, id='Bezeichnung zu lang'),
    pytest.param(dict(label='zwei\nZeilen'), TAX_ENTRY_LABEL, id='Bezeichnung mit Zeilenumbruch'),
    pytest.param(dict(rate=''), TAX_ENTRY_RATE, id='Satz leer'),
    pytest.param(dict(rate='abc'), TAX_ENTRY_RATE, id='Satz Text'),
    pytest.param(dict(rate='5,555'), TAX_ENTRY_RATE, id='Satz mit drei Nachkommastellen'),
    pytest.param(dict(rate='101'), TAX_ENTRY_RATE, id='Satz über 100'),
    pytest.param(dict(rate='1e2'), TAX_ENTRY_RATE, id='Satz als Zehnerpotenz'),
    pytest.param(dict(rate='-1'), TAX_ENTRY_RATE, id='Satz negativ'),
    pytest.param(dict(rate='0,5'), TAX_ENTRY_RATE, id='Satz unter 1 %'),
    pytest.param(dict(rate='٥'), TAX_ENTRY_RATE, id='Ziffer einer anderen Schrift'),
    pytest.param(dict(rate='5 %'), TAX_ENTRY_RATE, id='Satz mit Prozentzeichen'),
    pytest.param(dict(kind='free'), TAX_ENTRY_KIND, id='Art unbekannt'),
    pytest.param(dict(kind=''), TAX_ENTRY_KIND, id='Art leer'),
    pytest.param(dict(kind='exempt', reason=REASON), TAX_ENTRY_EXEMPT_RATE, id='steuerfrei mit Satz 5,5'),
    pytest.param(dict(kind='exempt', rate='0', reason=''), TAX_ENTRY_REASON, id='steuerfrei ohne Grund'),
    pytest.param(dict(kind='exempt', rate='0', reason='   '), TAX_ENTRY_REASON, id='steuerfrei, Grund nur Leerraum'),
    pytest.param(dict(kind='exempt', rate='0', reason='x' * 301), TAX_ENTRY_REASON, id='Grund zu lang'),
    pytest.param(dict(revenue_coa_id=''), TAX_ENTRY_REVENUE_REQUIRED, id='5,5 % ohne Erlöskonto'),
    pytest.param(dict(rate='0', revenue_coa_id=''), TAX_ENTRY_REVENUE_REQUIRED, id='0 % ohne Erlöskonto'),
    pytest.param(dict(revenue_coa_id='expense'), TAX_ENTRY_REVENUE_NOT_INCOME, id='Aufwandskonto als Erlöskonto'),
    pytest.param(dict(revenue_coa_id='999999'), TAX_ENTRY_REVENUE_NOT_INCOME, id='Erlöskonto gibt es nicht'),
    pytest.param(dict(rate='0', vat_coa_id='test_vat'), TAX_ENTRY_TAX_ACCOUNT_ZERO, id='0 % mit Umsatzsteuerkonto'),
    pytest.param(dict(rate='19', vat_coa_id='test_vat'), TAX_ENTRY_TAX_ACCOUNT_FIXED, id='19 % mit Umsatzsteuerkonto'),
    pytest.param(dict(rate='7', input_tax_coa_id='test_input'), TAX_ENTRY_TAX_ACCOUNT_FIXED, id='7 % mit Vorsteuerkonto'),
    pytest.param(dict(vat_coa_id='input_ok'), TAX_ENTRY_VAT_ACCOUNT, id='Vorsteuerkonto als Umsatzsteuerkonto'),
    pytest.param(dict(input_tax_coa_id='revenue'), TAX_ENTRY_INPUT_TAX_ACCOUNT, id='4400 als Vorsteuerkonto'),
]


def resolved(world, change):
    """Die Namen von Konten in ``change`` (``'expense'``, ``'test_vat'`` …) werden zu ihren IDs als Text."""
    return {key: str(getattr(world, value)) if isinstance(value, str) and hasattr(world, value) else value for key, value in change.items()}


@pytest.mark.parametrize('change, sentence', FORM_REFUSALS)
def test_adding_an_entry_that_breaks_a_rule_shows_exactly_its_sentence_and_stores_nothing(server_port, world, change, sentence):
    before = rows(world.db)

    response = submit(server_port, world, f'{LIST}/add', form(world, **resolved(world, change)))

    assert response.status == 303 and response.headers['Location'] == err_location(sentence)
    assert rows(world.db) == before
    assert shown_message(get(server_port, response.headers['Location']).text) == [sentence, 'error']


@pytest.mark.parametrize('change, sentence', FORM_REFUSALS)
def test_updating_an_entry_to_something_that_breaks_a_rule_shows_the_sentence_on_the_entry_and_changes_nothing(server_port, world, change, sentence):
    entry = world.db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=world.revenue)
    before = rows(world.db)

    response = submit(server_port, world, f'{LIST}/update', form(world, id=str(entry), **resolved(world, change)))

    assert response.status == 303 and response.headers['Location'] == err_location(sentence, f'{LIST}/edit?id={entry}')
    assert rows(world.db) == before
    page = get(server_port, response.headers['Location'])
    assert shown_message(page.text) == [sentence, 'error'] and 'Steuersatz bearbeiten' in page.text


def test_a_sort_order_that_is_no_number_in_range_is_a_message_of_the_page_and_nothing_is_stored(server_port, world):
    for value in ('abc', '-1', '10000', '1.5', '1e2', ' 5x'):
        before = rows(world.db)

        added = submit(server_port, world, f'{LIST}/add', form(world, sort_order=value))
        updated = submit(server_port, world, f'{LIST}/update', form(world, id='1', sort_order=value))

        assert added.headers['Location'] == err_location(handlers.TAX_RATE_ORDER_ERROR), value
        assert updated.headers['Location'] == err_location(handlers.TAX_RATE_ORDER_ERROR, f'{LIST}/edit?id=1'), value
        assert rows(world.db) == before
    assert handlers.TAX_RATE_ORDER_ERROR == 'Bitte eine Reihenfolge von 0 bis 9999 angeben.'


@pytest.mark.parametrize('field', ['revenue_coa_id', 'vat_coa_id', 'input_tax_coa_id'])
@pytest.mark.parametrize('value', ['abc', '0', '-5', '1.5', f'{2 ** 63}', '٣', '1e3'])
def test_an_account_that_is_not_an_id_is_a_400_with_plain_text_and_nothing_is_stored(server_port, world, field, value):
    before = rows(world.db)

    added = submit(server_port, world, f'{LIST}/add', form(world, **{field: value}))
    updated = submit(server_port, world, f'{LIST}/update', form(world, id='1', **{field: value}))

    for response in (added, updated):
        assert response.status == 400 and response.text == 'Ungültige Anfrage.'
        assert response.headers['Content-Type'] == 'text/plain; charset=utf-8' and 'Location' not in response.headers
    assert rows(world.db) == before


@pytest.mark.parametrize('entry_id', ['', 'abc', '0', '-1', '1.5', f'{2 ** 63}', '٣'])
def test_an_update_without_a_valid_id_is_a_400_and_nothing_is_stored(server_port, world, entry_id):
    before = rows(world.db)

    response = submit(server_port, world, f'{LIST}/update', form(world, id=entry_id))

    assert response.status == 400 and response.text == 'Ungültige Anfrage.'
    assert rows(world.db) == before


def test_an_update_of_an_entry_that_does_not_exist_is_a_message_on_the_page(server_port, world):
    response = submit(server_port, world, f'{LIST}/update', form(world, id='99999'))

    assert response.status == 303
    assert response.headers['Location'] == err_location('Diesen Steuersatz gibt es nicht (mehr).', f'{LIST}/edit?id=99999')
    assert rows(world.db)[:2] == [(1, '19 %', 1900, 'standard', None, None, None, None, 1, 10), (2, '7 %', 700, 'standard', None, None, None, None, 1, 20)]


# ── Ändern ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_update_changes_every_field_and_leads_back_to_the_list(server_port, world):
    entry = world.db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=world.revenue)

    response = submit(server_port, world, f'{LIST}/update', form(world, id=str(entry), label='Geändert (erfunden)', rate='10,7', vat_coa_id=str(world.test_vat),
                                                                 input_tax_coa_id=str(world.test_input), sort_order='5', active=''))

    assert (response.status, response.headers['Location']) == (303, LIST)
    assert rows(world.db)[2] == (entry, 'Geändert (erfunden)', 1070, 'standard', None, world.revenue, world.test_vat, world.test_input, 0, 5)


def test_an_update_without_the_flag_field_and_without_a_sort_order_leaves_both(server_port, world):
    entry = world.db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=world.revenue, sort_order=77, active=False)
    fields = form(world, id=str(entry), sort_order='')
    del fields['active_field'], fields['active']

    submit(server_port, world, f'{LIST}/update', fields)

    assert rows(world.db)[2][8:] == (0, 77)


def test_the_form_can_clear_an_account_and_turn_a_standard_entry_into_a_tax_free_one(server_port, world):
    entry = world.db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=world.revenue, vat_coa_id=world.test_vat)

    submit(server_port, world, f'{LIST}/update', form(world, id=str(entry), label='Steuerfrei (erfunden)', rate='0', kind='exempt', reason=REASON, vat_coa_id=''))

    assert rows(world.db)[2][1:8] == ('Steuerfrei (erfunden)', 0, 'exempt', REASON, world.revenue, None, None)


# ── Umschalten und Löschen ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('action', ['toggle', 'delete'])
def test_the_actions_are_post_only(server_port, world, action):
    before = rows(world.db)

    response = get(server_port, f'{LIST}/{action}?id=2')

    assert response.status == 405 and response.headers['Allow'] == 'POST'
    assert rows(world.db) == before


@pytest.mark.parametrize('action', ['toggle', 'delete'])
def test_an_action_without_a_valid_id_is_a_400_and_nothing_changes(server_port, world, action):
    before = rows(world.db)

    for query in ('', '?id=abc', '?id=', '?id=1.5'):
        response = submit(server_port, world, f'{LIST}/{action}{query}')
        assert response.status == 400 and response.text == 'Ungültige oder fehlende ID.', query
    assert rows(world.db) == before


def test_toggling_turns_an_entry_off_and_on_and_leads_back_to_the_list(server_port, world):
    first = submit(server_port, world, f'{LIST}/toggle?id=2')
    state_after_first = [row[8] for row in rows(world.db)]
    page = get(server_port, LIST).text
    second = submit(server_port, world, f'{LIST}/toggle?id=2')

    assert (first.status, first.headers['Location']) == (303, LIST) and (second.status, second.headers['Location']) == (303, LIST)
    assert state_after_first == [1, 0] and [row[8] for row in rows(world.db)] == [1, 1]
    assert '7 % inaktiv' in table_rows(page)[1][0] and "title='Einschalten (wählbar)'" in page


def test_the_last_selectable_entry_cannot_be_toggled_off_or_deleted_and_the_page_says_so(server_port, world):
    submit(server_port, world, f'{LIST}/delete?id=2')
    before = rows(world.db)

    toggled = submit(server_port, world, f'{LIST}/toggle?id=1')
    deleted = submit(server_port, world, f'{LIST}/delete?id=1')
    updated = submit(server_port, world, f'{LIST}/update', form(world, id='1', label='19 %', rate='19', revenue_coa_id='', active=''))

    assert toggled.headers['Location'] == deleted.headers['Location'] == err_location(TAX_ENTRY_LAST_ACTIVE)
    assert updated.headers['Location'] == err_location(TAX_ENTRY_LAST_ACTIVE, f'{LIST}/edit?id=1')
    assert rows(world.db) == before
    assert shown_message(get(server_port, toggled.headers['Location']).text) == [TAX_ENTRY_LAST_ACTIVE, 'error']


def test_an_entry_is_deleted_and_leads_back_to_the_list_without_a_message(server_port, world):
    entry = world.db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=world.revenue)

    response = submit(server_port, world, f'{LIST}/delete?id={entry}')

    assert (response.status, response.headers['Location']) == (303, LIST)
    assert [row[0] for row in rows(world.db)] == [1, 2]


def test_deleting_is_always_allowed_while_another_entry_is_selectable_also_with_documents_that_carry_the_snapshot(server_port, world):
    from tests.document_helpers import add_contacts, add_document
    own, customer = add_contacts(world.db)
    document = add_document(world.db, '26R001', 'invoice', own, customer)
    entry = world.db.insert_tax_rate(label='Steuerfrei (erfunden)', bp=0, kind='exempt', reason=REASON, revenue_coa_id=world.revenue)
    world.db.insert_invoice_item({'invoice_id': document, 'position': 2, 'description': 'Beratung', 'quantity': 1, 'unit': 'Std.', 'price_per_unit': 80.0,
                                  'total_net': 80.0, 'tax_rate': 0, 'tax_category': 'E', 'tax_rate_bp': 0, 'tax_exempt_reason': REASON, 'revenue_coa_id': world.revenue})
    items = dump(world.db)['InvoiceItems']

    response = submit(server_port, world, f'{LIST}/delete?id={entry}')

    assert response.headers['Location'] == LIST and entry not in [row[0] for row in rows(world.db)]
    assert dump(world.db)['InvoiceItems'] == items


def test_toggling_or_deleting_an_entry_that_does_not_exist(server_port, world):
    toggled = submit(server_port, world, f'{LIST}/toggle?id=99999')
    deleted = submit(server_port, world, f'{LIST}/delete?id=99999')

    assert toggled.headers['Location'] == err_location('Diesen Steuersatz gibt es nicht (mehr).')
    assert deleted.headers['Location'] == LIST
    assert [row[0] for row in rows(world.db)] == [1, 2]


# ── Zugriffsschutz ────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('path, fields', [
    ('add', None), ('update', dict(id='1')), ('toggle?id=2', None), ('delete?id=2', None)])
def test_every_write_needs_the_token_and_an_own_origin(server_port, world, path, fields):
    before = dump(world.db)
    fields = form(world, **(fields or {}))

    without_token = post(server_port, f'{LIST}/{path}', headers={'Origin': own_origin(server_port)}, form=fields)
    foreign = post(server_port, f'{LIST}/{path}', headers={'Origin': FOREIGN_ORIGIN}, form=fields, csrf=world.csrf)

    assert without_token.status == 403 and 'Sicherheits-Token' in without_token.text
    assert foreign.status == 403
    assert dump(world.db) == before


def test_the_handlers_take_no_value_of_the_form_without_the_helpers():
    """Satz und Reihenfolge werden als Text gelesen: kein ``float`` und kein ``int`` über Formularwerte (``tests/test_form_error_paths.py`` prüft es für die ganze Datei)."""
    source = open(handlers.__file__, encoding='utf-8').read()
    section = source[source.index('# ─── Steuersätze der Stammdaten (2.8.0)'):]

    assert 'float(' not in section and not re.search(r'\bint\(', section)


# ── Konten, die ein Eintrag nennt ────────────────────────────────────────────────────────────────────────────────────────────

def test_an_account_an_entry_names_cannot_be_deleted_from_the_chart_of_accounts(server_port, world):
    world.db.insert_tax_rate(label='Ermäßigt (erfunden)', bp=550, revenue_coa_id=world.test_revenue, vat_coa_id=world.test_vat, input_tax_coa_id=world.test_input)

    for account in (world.test_revenue, world.test_vat, world.test_input):
        response = submit(server_port, world, f'/masterdata/skr/delete?id={account}')
        assert response.status == 303
        assert 'Steuers%C3%A4tzen' in response.headers['Location'] and 'type=error' in response.headers['Location']
        assert world.db.coa_is_referenced(account) is True
