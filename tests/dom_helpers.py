# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen, um erzeugte Seiten so zu lesen, wie ein Browser sie liest.

``Dom`` zerlegt HTML in Starttags mit Attributen (HTML-Entitäten aufgelöst) und den Text der
<script>-Elemente – ein ``</script>`` im Text beendet dort das Element, genau wie im Browser.
So lässt sich prüfen, ob ein Wert aus Adresse oder Datenbank den Aufbau einer Seite verändert
(zusätzliche Elemente, ein vorzeitig geschlossener Skriptblock), statt nur nach Zeichenketten zu
suchen. Verwendet von test_period_validation.py und test_script_context_pages.py.
"""
import json
import re
from html.parser import HTMLParser


class Dom(HTMLParser):
    """Starttags mit Attributen und der Text der <script>-Elemente einer Seite."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.scripts, self._in_script = [], [], False
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == 'script':
            self._in_script = True
            self.scripts.append('')

    def handle_endtag(self, tag):
        if tag == 'script':
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.scripts[-1] += data

    @property
    def names(self):
        return [tag for tag, _ in self.tags]

    def one(self, tag, **attrs):
        """Attribute des einzigen Elements ``tag`` mit diesen Attributwerten."""
        found = [a for t, a in self.tags if t == tag and all(a.get(k) == v for k, v in attrs.items())]
        assert len(found) == 1, f'{tag} {attrs}: {len(found)} Treffer'
        return found[0]

    def all(self, tag):
        return [a for t, a in self.tags if t == tag]


def data_block(dom, name):
    """Der JSON-Block ``const <name> = {…};`` einer Seite als Python-Objekt."""
    for script in dom.scripts:
        found = re.search(rf'const {name} = (.*?);\n', script, re.S)
        if found:
            return json.loads(found.group(1))
    raise AssertionError(f'kein Datenblock {name}')


def app_msg_arguments(dom):
    """Die Argumente des Meldungsaufrufs ``appMsg(…)`` (nach DOMContentLoaded) als Python-Werte.

    Die Argumente stehen als JSON im Skriptblock; sie werden Wert für Wert gelesen, so wie
    JavaScript sie läse. Die Funktionsdefinition ``function appMsg(`` im Fuß der Seite zählt nicht."""
    decoder = json.JSONDecoder()
    for script in dom.scripts:
        found = re.search(r'function\(\)\{appMsg\(', script)
        if not found:
            continue
        position, values = found.end(), []
        while True:
            value, position = decoder.raw_decode(script, position)
            values.append(value)
            if script[position] != ',':
                break
            position += 1
        assert script[position] == ')', script[position:position + 40]
        return values
    raise AssertionError('kein appMsg-Aufruf in der Seite')


def assert_same_structure(hostile, benign):
    """Der Aufbau der Seite hängt nicht vom feindlichen Wert ab: dieselben Elemente in
    derselben Reihenfolge, dieselbe Zahl <script>-Blöcke, keine Ereignis-Attribute mit
    Skriptcode und keine zusätzlichen Elemente, die Code nachladen könnten."""
    a, b = Dom(hostile), Dom(benign)
    assert a.names == b.names, 'durch den Wert sind Elemente hinzugekommen oder weggefallen'
    assert len(a.scripts) == len(b.scripts)
    for tag, attrs in a.tags:
        for name, value in attrs.items():
            assert not (name.startswith('on') and 'alert(' in (value or '')), (tag, name, value)
    dangerous = ('img', 'iframe', 'svg', 'object', 'embed', 'link', 'meta', 'base')
    assert [t for t in a.names if t in dangerous] == [t for t in b.names if t in dangerous]
