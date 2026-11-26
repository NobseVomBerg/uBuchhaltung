# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Tests für die Artikel-Stammdatenseite (server/pages_masterdata.PageArticles)."""
import html as html_lib
import re

import pytest

from server.pages_masterdata import PageArticles

ROW = re.compile(r"<tr class='article-row' data-active='\d+' data-tax='([^']*)' data-price='[^']*'><td>(.*?)</td>")


def test_zero_percent_tax_shown_as_zero_in_list(tmp_db):
    """0% MwSt darf in der Übersicht nicht zu 19% verfälscht werden (or-19-Bug)."""
    tmp_db.insert_article(name='Nullsteuer-Artikel', unit_price=10, tax_rate=0)
    html = PageArticles(tmp_db)
    assert "data-tax='0'" in html
    assert '<td>0%</td>' in html
    # keine fälschliche 19%-Anzeige für diesen (einzigen) Artikel
    assert "data-tax='19'" not in html
    assert '<td>19%</td>' not in html


def test_normal_tax_rate_unaffected(tmp_db):
    """19%/7% werden weiterhin korrekt angezeigt."""
    tmp_db.insert_article(name='Standard', unit_price=10, tax_rate=19)
    tmp_db.insert_article(name='Ermäßigt', unit_price=10, tax_rate=7)
    html = PageArticles(tmp_db)
    assert '<td>19%</td>' in html
    assert '<td>7%</td>' in html


def _filter_options(html):
    """Die Optionen des Steuerfilters der Liste als ``[(Wert, Beschriftung)]``."""
    block = re.search(r'<select id="taxFilter"[^>]*>(.*?)</select>', html, re.S).group(1)
    return [(html_lib.unescape(value), html_lib.unescape(label)) for value, label in re.findall(r'<option value="([^"]*)">(.*?)</option>', block, re.S)]


def _tax_options(html):
    """Die Optionen der Auswahl „MwSt“ im Formular als ``[(Wert, vorgewählt, Beschriftung)]``."""
    block = re.search(r'<select name="tax_rate">(.*?)</select>', html, re.S).group(1)
    return [(html_lib.unescape(value), bool(selected), html_lib.unescape(label))
            for value, selected, label in re.findall(r'<option value="([^"]*)"\s*(selected)?>(.*?)</option>', block, re.S)]


def test_a_fractional_tax_rate_is_shown_exactly_in_the_list_not_cut_to_a_whole_number(tmp_db):
    """Ein Artikel mit 5,5 % stand als „5%“ in der Liste und trug ``data-tax='5'``: ``int()`` kürzte den Satz."""
    tmp_db.insert_article(name='Ermäßigt', unit_price=10, tax_rate=5.5)
    html = PageArticles(tmp_db)
    assert '<td>5,5%</td>' in html and "data-tax='5.5'" in html
    assert '<td>5%</td>' not in html and "data-tax='5'" not in html


def test_the_tax_filter_offers_each_rate_that_occurs_and_finds_every_article_by_it(tmp_db):
    """Der Filter vergleicht den Wert der Auswahl genau mit ``data-tax`` der Zeile. Er fand einen Artikel mit 5,5 % nie: Die Auswahl kannte nur 0, 7 und 19 Prozent. Die drei bleiben,
    jeder weitere Satz, den ein Artikel trägt, kommt – nach Größe geordnet – dazu."""
    names = {'0': 'Frei', '5.5': 'Bruch', '7': 'Ermäßigt', '10.5': 'Zehnfünf', '16': 'Sechzehn', '19': 'Voll'}
    for value, name in names.items():
        tmp_db.insert_article(name=name, unit_price=10, tax_rate=float(value))
    html = PageArticles(tmp_db)

    options = _filter_options(html)
    assert options == [('', 'Alle'), ('0', '0%'), ('5.5', '5,5%'), ('7', '7%'), ('10.5', '10,5%'), ('16', '16%'), ('19', '19%')]
    rows = ROW.findall(html)
    assert sorted(name for _tax, name in rows) == sorted(names.values())
    for value, _label in options[1:]:
        assert [name for tax, name in rows if tax == value] == [names[value]]


def test_the_tax_filter_keeps_its_three_fixed_rates_and_an_empty_rate_still_shows_as_19(tmp_db):
    """Ohne einen Artikel mit anderem Satz sieht der Filter aus wie immer; ein leerer Satz zeigt wie bisher 19 %."""
    fixed = [('', 'Alle'), ('0', '0%'), ('7', '7%'), ('19', '19%')]
    assert _filter_options(PageArticles(tmp_db)) == fixed
    tmp_db.insert_article(name='Ohne Satz', unit_price=10, tax_rate=None)
    html = PageArticles(tmp_db)
    assert _filter_options(html) == fixed
    assert ROW.findall(html) == [('19', 'Ohne Satz')] and '<td>19%</td>' in html


@pytest.mark.parametrize('rate, text', [(5.5, '5,5'), (5.55, '5,55'), (10.5, '10,5'), (16, '16'), (12.0, '12'), (0.19, '0,19')])
def test_the_form_shows_a_stored_rate_outside_the_fixed_choice_with_a_comma_and_keeps_its_value(tmp_db, rate, text):
    tmp_db.insert_article(name='Importartikel', unit_price=10, tax_rate=rate)
    html = PageArticles(tmp_db, edit_article_id=tmp_db.fetch_articles()[0][0])
    assert _tax_options(html) == [('19', False, '19%'), ('7', False, '7%'), ('0', False, '0%'), (repr(float(rate)), True, text + '%')]
