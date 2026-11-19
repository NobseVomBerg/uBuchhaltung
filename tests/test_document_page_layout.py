# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Seiten „Rechnungen“ und „Angebote“ passen neben ihr Formular (seit 2.5.1).

Der Fehlfall: Das Belegformular rechts ist fest 820 Pixel breit (es bildet das Blatt nach), stand aber in der Drittelspalte
von ``.grid2Cols`` – bei 1920 Pixeln Fensterbreite ragte es 180 Pixel über den rechten Rand, und die Felder rechts waren
nur mit waagerechtem Rollen zu erreichen. Jetzt trägt das Raster die Breite des Formulars (``.gridDocForm``), die Liste
bekommt den Rest und macht sich schmal (``.docList``).

Geprüft wird, was sich ohne Browser prüfen lässt: Raster und Liste tragen die Klassen, kein Inline-Stil setzt die Breite
neben dem Stylesheet, und das Stylesheet kennt die Regeln. Die Maße selbst (kein Überstand von 1000 bis 1920 Pixel) sind
im Browser gemessen – auch so, wie ein Laptop mit 150 % Skalierung die Seite sieht (1280 Pixel).
"""
import os
import re

import pytest

from tests.document_helpers import make_books, set_column
from tests.http_client import get

FORM_WIDTH = '820px'


@pytest.fixture
def books(auto_login):
    return make_books(auto_login)


def css():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'buch.css')
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def rule(name):
    """Der Rumpf der ersten Regel, deren Selektor genau ``name`` ist."""
    found = re.search(r'(?m)^\s*' + re.escape(name) + r'\s*\{([^}]*)\}', css())
    assert found, f'buch.css kennt {name} nicht'
    return found.group(1)


@pytest.mark.parametrize('path', ['/invoice', '/quote'])
def test_the_grid_carries_the_form_width_and_no_inline_width(server_port, books, path):
    html = get(server_port, path).text
    assert '<div class="grid2Cols gridDocForm gridMain">' in html
    assert 'min-width:820px' not in html and 'min-width: 820px' not in html     # die Breite steht im Stylesheet
    assert '<div class="gridRightCol" style="order:2">' in html


@pytest.mark.parametrize('path, extra', [('/invoice', 1), ('/quote', 2)])
def test_the_list_is_compact_and_the_customer_takes_the_rest(server_port, books, path, extra):
    html = get(server_port, path).text
    assert '<table class="docList">' in html
    name = re.search(r'<td class="docName" title="([^"]+)">([^<]+)</td>', html)
    assert name and name.group(1) == name.group(2)                 # ein kurzer Name steht ganz in der Zelle
    assert html.count('<th class="num docWide">Netto</th>') == 1
    # Netto (und bei Angeboten „gültig bis“) weichen bei schmalem Fenster – je eine Kopf- und eine Datenzelle
    assert html.count('docWide') == 2 * extra


def test_a_long_customer_name_is_cut_in_the_cell_and_whole_in_the_tooltip(server_port, books):
    long_name = 'Sehr lange Firmenbezeichnung <b>&"x" mit Zusatz GmbH & Co. KG'
    set_column(books.db, books.invoice, 'BuyerName', long_name)
    html = get(server_port, '/invoice').text
    assert ('title="Sehr lange Firmenbezeichnung &lt;b&gt;&amp;&quot;x&quot; mit Zusatz GmbH &amp; Co. KG">'
            'Sehr lange Firmenbezeichnung &lt;</td>') in html                      # die Zelle nennt 30 Zeichen
    assert '<b>&"x"' not in html


def test_the_stylesheet_gives_the_form_its_width_and_the_list_the_rest():
    assert rule('.gridDocForm').split() == f'grid-template-columns: minmax(0, 1fr) {FORM_WIDTH};'.split()
    name = rule('.docList td.docName')
    assert 'width: 100%' in name and 'max-width: 0' in name and 'text-overflow: ellipsis' in name
    assert 'white-space: nowrap' in rule('.docList td, .docList th')
    sheet = css()
    # die Regel steht hinter .grid2Cols, sonst gewönne bei gleicher Spezifität die Drittelteilung
    assert sheet.index('.gridDocForm') > sheet.index('.grid2Cols')
    assert '@media (min-width: 1441px) and (max-width: 1700px) { .docList .docWide { display: none; } }' in sheet
    # schmales Fenster (auch: 1920 Pixel bei 150 % Skalierung = 1280): Formular unter der Liste, die Liste bleibt niedrig
    narrow = re.search(r'@media \(max-width: 1440px\) \{(.*?)\n\}', sheet, re.S)
    assert narrow
    assert '.gridDocForm { grid-template-columns: minmax(0, 1fr); }' in narrow.group(1)
    assert '.gridDocForm .gridLeftCol { max-height: 38vh; }' in narrow.group(1)
    assert f'.gridDocForm .gridRightCol {{ max-width: {FORM_WIDTH}; }}' in narrow.group(1)
