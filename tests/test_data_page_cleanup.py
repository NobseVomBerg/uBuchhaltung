# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Seite „Daten“ (früher „Sonstiges“) und die About-Seite nach dem Aufräumen.

Die Seite heißt im Menü „Daten“, ihre Adresse ``/miscellaneous`` bleibt. Sie behält Datensicherung, vorhandene Sicherungen,
Wiederherstellung, DATEV-Export, beide WISO-Importe und die Prüfung der Zahlungszuordnungen. Drei Kästen sind auf die About-Seite
(``pages.PageAbout``) gezogen:

* die **Datenbank-Übersicht** (Tabelle und Zahl der Einträge) – für jeden angemeldeten Benutzer, alles maskiert, ohne Inline-Style,
* die **SQL-Konsole** (HTML und Skript unverändert) ganz unten und
* der **DB-Export** direkt darunter – beide nur dort, wo ``auth.sql_console_allowed()`` gilt: Wer die Konsole nicht benutzen darf,
  sieht weder einen Kasten noch einen Ersatzsatz noch ein Skript. ``POST /db_export`` läuft ebenso nur für Berechtigte (403 mit festem
  Satz, nichts geschrieben); das Ergebnis des Exports steht auf der About-Seite (``?export=ok&tables=…&rows=…`` bzw. ``?export=error``,
  streng gelesen, der Text fest, nur über ``appMsg``).

Dazu **Sicherungen löschen**: ``POST /backup/delete`` (Formularfeld ``file``, ``_ACTION_PATHS``) löscht allein, was
``backup.downloadable_backups`` für den angemeldeten Benutzer nennt – kein ``restore_*``/``replaced_*``, keine Teildatei, kein Ordner, kein
Link, kein Pfadanteil, nichts von einem anderen Benutzer –, nie während einer Sicherung oder Wiederherstellung, nie im Demo-Modus; jede
Meldung ist ein fester Satz ohne Pfad. Die Liste auf der Seite trägt je Zeile den Knopf „Löschen“ (``appConfirmPost`` mit dem Feld ``file``).

Alle Namen, Dateien und Inhalte sind erfundene Testwerte.
"""
import json
import os
import sqlite3
import pathlib
import re
import shutil
import subprocess
import urllib.parse
from types import SimpleNamespace

import pytest

import auth
import userctx
from server import app as server_app
from server import backup as backup_mod
from server import handlers
from server.pages import DB_EXPORT_FAILED_TEXT, Footer, PageAbout, export_message
from server.pages_miscellaneous import PageMiscellaneous
from tests import installation
from tests.dom_helpers import Dom, app_msg_arguments
from tests.http_client import FOREIGN_ORIGIN, get, own_origin, post, send
from tests.test_backup_download import MIGRATION, NAME, admin_dir, backup_folder, make_symlink, plant_decoys, put
from tests.test_demo_http import demo_site  # noqa: F401  (Fixture: Besucher des Demo-Modus)

#: Was zur SQL-Konsole gehört – auf keiner Seite, die sie nicht zeigt
CONSOLE_MARKS = ('id="sql_input"', 'id="sql_write"', 'id="sql_output"', 'id="sql_status"', 'executeSql', 'buildRangeDeleteSql',
                 'sqlStatus', 'Zeitbereich löschen', 'SQL-Befehle', '/execute_sql')
#: … und zum DB-Export
EXPORT_MARKS = ('<h2>DB-Export</h2>', '/db_export', 'db-export.sql', 'INSERT-Statements')
H2 = re.compile(r'<h2>(.*?)</h2>')
DEMO_DENIED = 'Im Demo-Modus lassen sich Sicherungen nicht löschen.'
NODE = shutil.which('node')
needs_node = pytest.mark.skipif(NODE is None, reason='Node.js nicht installiert – die statischen Tests decken nur die Form ab')


# ── Hilfen ───────────────────────────────────────────────────────────────────
@pytest.fixture
def people():
    """Installation mit Anmeldung (keine automatische): der Administrator ``admin`` und der gewöhnliche Benutzer ``bob``, je mit
    Sitzung, Token, Datenbank und Datenordner."""
    installation.set_up(auto_login=False, users=[('bob', False)])
    found = {}
    for name in ('admin', 'bob'):
        token = auth.create_session(name)
        found[name] = SimpleNamespace(name=name, db=installation.open_db(name), cookie={'Cookie': f'session={token}'},
                                      csrf=auth.csrf_for(token), dir=os.path.join(userctx.DATA_ROOT, 'users', name))
    return SimpleNamespace(**found)


def page_of(port, who=None, path='/about'):
    return get(port, path, who.cookie if who else None).text


def as_user(port, who, path, form=None, headers=None, csrf=True):
    """Ein POST wie ihn eine eigene Seite schickt: Herkunft, Sitzung und (wenn ``csrf``) das Token des Benutzers."""
    return post(port, path, headers={'Origin': own_origin(port), **who.cookie, **(headers or {})}, form=form,
                csrf=who.csrf if csrf else None)


def location_of(resp):
    assert resp.status == 303, (resp.status, resp.text)
    parts = urllib.parse.urlsplit(resp.headers['Location'])
    return parts.path, urllib.parse.parse_qs(parts.query)


def misc_message(page):
    """Die Meldung, die die Seite Daten als JSON im Skriptblock ``miscMsg`` trägt."""
    return json.loads(re.search(r'var miscMsg = (.*?);</script>', page).group(1))


def assert_no_console_or_export(page):
    for mark in (*CONSOLE_MARKS, *EXPORT_MARKS, 'nur Administratoren zur Verfügung'):
        assert mark not in page, mark


# ── Die Seite „Daten“ ────────────────────────────────────────────────────────
def test_the_menu_calls_the_page_Daten_and_the_address_stays(tmp_db):
    page, other = PageMiscellaneous(tmp_db), PageAbout(tmp_db)
    assert '<span id="ActivePage">Daten</span>' in page                       # die Seite selbst: der Menüpunkt ohne Link
    assert '<a href="/miscellaneous">Daten</a>' in other                      # auf jeder anderen Seite: der Link auf die alte Adresse
    for html in (page, other):
        assert 'Sonstiges' not in html


def test_the_Daten_page_keeps_its_boxes_and_has_none_of_the_three_moved_ones(tmp_db):
    page = PageMiscellaneous(tmp_db)
    assert H2.findall(page) == ['DATEV Export', 'Zahlungszuordnungen', 'WISO Mein Büro Import',
                                'WISO Mein Büro &ndash; Import aus der Datenbank', '&#x1F4BE; Datensicherung',
                                '&#x267B;&#xFE0F; Wiederherstellung']
    assert_no_console_or_export(page)
    assert 'Datenbank-Übersicht' not in page and 'Anzahl Einträge' not in page
    assert 'grid-column' not in page                                          # kein Rest des Platzes im Raster, den die Konsole hielt


class GridReader(Dom):
    """Die Klassen der Kästen, die direkt im Raster ``grid3Cols`` der Seite stehen (für das Raster ohne die drei Kästen)."""

    def __init__(self, text):
        self._stack, self.grid_children = [], []
        super().__init__(text)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag != 'div':
            return
        css = dict(attrs).get('class') or ''
        if self._stack and self._stack[-1] == 'grid3Cols gridMain':
            self.grid_children.append(css)
        self._stack.append(css)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == 'div' and self._stack:
            self._stack.pop()


def test_the_grid_of_the_Daten_page_has_no_hole_where_the_boxes_were(tmp_db):
    """Drei Spalten, zwei volle Zeilen: oben der Stapel DATEV-Export und Zahlungszuordnungen und die beiden WISO-Importe, darunter die
    Datensicherung über zwei Spalten (die Liste der Sicherungen braucht Platz) und die Wiederherstellung. Kein leerer Platz am Anfang,
    keine leere Zelle am Ende."""
    grid = GridReader(PageMiscellaneous(tmp_db))
    assert grid.grid_children == ['grid1Col gridMiddle', 'rectRounded', 'rectRounded', 'rectRounded box2', 'rectRounded']
    used = 0                                                                   # das Raster füllt seine Zeilen der Reihe nach (drei Spalten)
    for width in (2 if 'box2' in css else 1 for css in grid.grid_children):
        used = width if used + width > 3 else used + width
    assert used == 3                                                           # die letzte Zeile ist voll: keine leere Zelle am Ende


# ── Die About-Seite: wer was sieht ───────────────────────────────────────────
def test_the_administrator_sees_overview_console_and_export_in_this_order_at_the_bottom(server_port, people):
    page = page_of(server_port, people.admin)
    assert [h for h in H2.findall(page) if h != 'Projekt in Zahlen'] == ['Datenbank-Übersicht', 'SQL-Befehle ausführen', 'DB-Export']
    assert page.count('id="sql_input"') == 1 and page.count('action="/db_export"') == 1
    assert page.index('licenseContact') < page.index('<h2>Datenbank-Übersicht</h2>') < page.index('<h2>SQL-Befehle ausführen</h2>')
    if 'Projekt in Zahlen' in page:
        assert page.index('Projekt in Zahlen') < page.index('<h2>Datenbank-Übersicht</h2>')
    # der Export steht direkt beim Konsolenkasten, und danach kommt nur noch der Fuß der Seite
    assert re.search(r'</script>\s*</div><div class="rectRounded">\s*<h2>DB-Export</h2>', page)
    assert page[page.index('<h2>DB-Export</h2>'):].count('<h2>') == 1
    assert 'nur Administratoren zur Verfügung' not in page


def test_the_automatic_login_sees_the_console_too(server_port, auto_login):
    page = page_of(server_port)
    assert 'id="sql_input"' in page and 'executeSql' in page and '<h2>DB-Export</h2>' in page


def test_an_ordinary_user_sees_the_overview_only_no_box_no_notice_no_script(server_port, people):
    page = page_of(server_port, people.bob)
    assert [h for h in H2.findall(page) if h != 'Projekt in Zahlen'] == ['Datenbank-Übersicht']
    assert_no_console_or_export(page)
    assert 'rectRounded' not in page                                          # auch kein leerer Kasten
    assert '/execute_sql' not in ''.join(Dom(page).scripts)
    assert page.rstrip().endswith('</html>')


def test_the_demo_visitor_sees_the_overview_only(server_port, demo_site):
    page = page_of(server_port)
    assert [h for h in H2.findall(page) if h != 'Projekt in Zahlen'] == ['Datenbank-Übersicht']
    assert_no_console_or_export(page)
    assert 'rectRounded' not in page


def test_the_overview_belongs_to_the_logged_in_user_and_lists_every_table_with_its_count(server_port, people):
    people.bob.db.insert_contact(contact_type='customer', entity_type='company', display_name='Probefirma',
                                 company_name='Probefirma', street='Musterweg 1', city='Musterstadt')
    stats = dict(people.bob.db.get_table_statistics())
    assert stats['Contacts'] == 1 and dict(people.admin.db.get_table_statistics())['Contacts'] == 0
    for who in (people.bob, people.admin):
        page = page_of(server_port, who)
        assert '<tr><th>Tabelle</th><th class="num">Anzahl Einträge</th></tr>' in page
        for name, count in dict(who.db.get_table_statistics()).items():
            assert f'<tr><td>{name}</td><td class="num">{count}</td></tr>' in page, (who.name, name)


def test_a_table_with_an_unusual_name_is_counted_and_never_breaks_the_page(tmp_db):
    """Über die SQL-Konsole lässt sich eine Tabelle mit Leerzeichen, Anführungszeichen oder einem Schlüsselwort als Namen anlegen: Die Übersicht
    zählt sie (der Name steht als Bezeichner in Anführungszeichen), und die Seite steht."""
    conn = tmp_db._get_connection()
    try:
        conn.execute('CREATE TABLE "meine tabelle" (x)')
        conn.execute('CREATE TABLE "a""b; DROP TABLE Contacts; --" (x)')
        conn.execute('CREATE TABLE "select" (x)')
        conn.execute('INSERT INTO "meine tabelle" VALUES (1), (2)')
        conn.commit()
    finally:
        conn.close()
    stats = dict(tmp_db.get_table_statistics())
    assert stats['meine tabelle'] == 2 and stats['a"b; DROP TABLE Contacts; --'] == 0 and stats['select'] == 0
    assert 'Contacts' in stats
    page = PageAbout(tmp_db)
    assert '<tr><td>meine tabelle</td><td class="num">2</td></tr>' in page
    assert '<tr><td>a&quot;b; DROP TABLE Contacts; --</td><td class="num">0</td></tr>' in page


def test_the_export_keeps_unusual_names_and_values_as_what_they_are(tmp_db, tmp_path):
    """Der DB-Export schreibt Namen als Bezeichner und Werte als Literale: Eine Tabelle, deren Name wie ein Befehl aussieht, bleibt eine
    Tabelle (eingespielt läuft kein fremder Befehl mit), Binärdaten und Text mit Hochkomma kommen unverändert zurück."""
    hostile = 'x (a) VALUES (1); DELETE FROM Contacts; --'
    conn = tmp_db._get_connection()
    try:
        conn.execute(f'CREATE TABLE "{hostile}" (a, "b ]c")')
        conn.execute('CREATE TABLE "zeile\num" (wert)')
        conn.execute(f'INSERT INTO "{hostile}" VALUES (?, ?)', (b'\x00\xff\'', "O'Brien; DROP TABLE x"))
        conn.execute(f'INSERT INTO "{hostile}" VALUES (?, ?)', (1.5, None))
        conn.execute('INSERT INTO "zeile\num" VALUES (7)')
        conn.commit()
        schema = [row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        before = {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                  for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    finally:
        conn.close()
    target = str(tmp_path / 'export' / 'db-export.sql')

    tables, rows = tmp_db.export_to_sql(target)

    text = open(target, encoding='utf-8').read()
    assert tables == sum(1 for found in before.values() if found) and rows == sum(len(found) for found in before.values())
    assert f'INSERT INTO "{hostile}" ("a", "b ]c") VALUES' in text and "X'00ff27'" in text and "'O''Brien; DROP TABLE x'" in text
    assert '-- zeile um\n' in text                                             # der Kommentar endet nie vorzeitig
    copy = sqlite3.connect(':memory:')
    try:
        for statement in schema:
            copy.execute(statement)
        copy.executescript(text)
        for name, found in before.items():
            assert copy.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() == found, name
    finally:
        copy.close()


def test_the_overview_masks_every_value_and_has_no_inline_style(tmp_db, monkeypatch):
    monkeypatch.setattr(tmp_db, 'get_table_statistics', lambda: [('<script>x</script>', 3), ('a&b"c', '<i>')])
    page = PageAbout(tmp_db)
    overview = page[page.index('<h2>Datenbank-Übersicht</h2>'):]
    overview = overview[:overview.index('</table>')]
    assert '<script>x' not in overview and '&lt;script&gt;x&lt;/script&gt;' in overview
    assert 'a&amp;b&quot;c' in overview and '&lt;i&gt;' in overview
    assert 'style' not in overview


def test_without_a_database_the_page_stays_as_it_was_and_the_sections_come_in_before_the_footer(tmp_db):
    page = PageAbout()
    assert 'Datenbank-Übersicht' not in page and 'sql_input' not in page and '/db_export' not in page
    assert 'licenseContact' in page and page.rstrip().endswith('</html>')
    footer = page.index('\n<div id="app_confirm"')                                   # der Fuß beginnt mit einem Zeilenumbruch
    with_db = PageAbout(tmp_db)
    assert with_db.startswith(page[:footer]) and with_db.endswith(page[footer:])      # nur die drei Abschnitte kommen dazwischen


def test_a_user_who_may_not_use_the_console_gets_no_box_even_where_the_page_is_called_directly(tmp_db, monkeypatch):
    monkeypatch.setattr(auth, 'sql_console_allowed', lambda *args, **kwargs: False)
    page = PageAbout(tmp_db)
    assert 'Datenbank-Übersicht' in page
    assert_no_console_or_export(page)
    assert 'rectRounded' not in page and 'grid1Col gridMiddle' not in page


def test_viewing_the_about_page_writes_nothing_into_the_books(server_port, people):
    """Die Seite ist in der Positivliste des Diagnose-MCP (``/about``, ohne Query-Schlüssel): Sie liest nur – die Datenbank bleibt
    byteweise, der Datenordner Datei für Datei gleich."""
    from tools import mcp_ubuchhaltung as mcp
    assert mcp._PAGE_ALLOWLIST['/about'] == frozenset()
    page_of(server_port, people.admin)                                         # das erste Laden darf Ordner anlegen
    before = (installation.file_digest(people.admin.db.db_name), installation.dump(people.admin.db.db_name),
              installation.tree(people.admin.dir))
    for _ in range(2):
        assert get(server_port, '/about', people.admin.cookie).status == 200
    assert (installation.file_digest(people.admin.db.db_name), installation.dump(people.admin.db.db_name),
            installation.tree(people.admin.dir)) == before


# ── Das Ergebnis des DB-Exports auf der About-Seite ──────────────────────────
@pytest.mark.parametrize('query, expected', [
    pytest.param('export=ok&tables=3&rows=41', ('Export erfolgreich: 3 Tabellen, 41 Zeilen.', 'success'), id='ok'),
    pytest.param('export=ok&tables=0&rows=0', ('Export erfolgreich: 0 Tabellen, 0 Zeilen.', 'success'), id='ok-null'),
    pytest.param('export=ok&tables=007&rows=1', ('Export erfolgreich: 7 Tabellen, 1 Zeilen.', 'success'), id='ok-fuehrende-Null'),
    pytest.param('export=error', (DB_EXPORT_FAILED_TEXT, 'error'), id='Fehler'),
    pytest.param('export=error&msg=<script>alert(1)</script>&tables=9', (DB_EXPORT_FAILED_TEXT, 'error'), id='Fehler-ohne-fremden-Text'),
])
def test_the_result_of_an_export_is_shown_through_appMsg_with_a_fixed_text(server_port, auto_login, query, expected):
    page = page_of(server_port, path='/about?' + query)
    assert app_msg_arguments(Dom(page)) == list(expected)
    assert '<script>alert(1)' not in page


@pytest.mark.parametrize('query', [
    pytest.param('', id='ohne'),
    pytest.param('export=ok', id='ok-ohne-Zahlen'),
    pytest.param('export=ok&tables=3', id='ok-ohne-Zeilen'),
    pytest.param('export=ok&tables=abc&rows=1', id='Text'),
    pytest.param('export=ok&tables=-1&rows=1', id='negativ'),
    pytest.param('export=ok&tables=1.5&rows=1', id='Kommazahl'),
    pytest.param('export=ok&tables=1e3&rows=1', id='Exponent'),
    pytest.param('export=ok&tables=%EF%BC%91&rows=1', id='breite-Ziffer'),
    pytest.param('export=ok&tables=%C2%B2&rows=1', id='hochgestellte-Ziffer'),
    pytest.param('export=ok&tables=' + '9' * 19 + '&rows=1', id='zu-gross'),
    pytest.param('export=ok&tables=1&tables=2&rows=1', id='doppelt'),
    pytest.param('export=ok&export=error', id='zwei-Zustaende'),
    pytest.param('export=ok%00&tables=1&rows=1', id='Zustand-mit-NUL'),
    pytest.param('export=%3Cscript%3E&tables=1&rows=1', id='Zustand-fremd'),
    pytest.param('export=OK&tables=1&rows=1', id='Gross-Klein'),
])
def test_an_address_that_is_not_exactly_a_result_shows_no_message(server_port, auto_login, query):
    page = page_of(server_port, path='/about?' + query)
    assert not re.search(r'function\(\)\{appMsg\(', ''.join(Dom(page).scripts))
    assert export_message(urllib.parse.parse_qs(query)) is None


def test_the_result_of_an_export_is_shown_only_where_the_export_box_is(server_port, people):
    page = page_of(server_port, people.bob, '/about?export=ok&tables=3&rows=41')
    assert 'Export erfolgreich' not in page and not re.search(r'function\(\)\{appMsg\(', ''.join(Dom(page).scripts))


# ── POST /db_export: nur für Berechtigte ─────────────────────────────────────
def test_the_export_writes_the_file_and_leads_to_the_about_page_with_the_result(server_port, people):
    admin = people.admin
    resp = as_user(server_port, admin, '/db_export')
    path, query = location_of(resp)
    assert path == '/about' and set(query) == {'export', 'tables', 'rows'} and query['export'] == ['ok']
    tables, rows = int(query['tables'][0]), int(query['rows'][0])
    assert tables > 0 and rows > 0
    assert os.path.isfile(os.path.join(admin.dir, 'db-export.sql'))
    shown = app_msg_arguments(Dom(page_of(server_port, admin, resp.headers['Location'])))
    assert shown == [f'Export erfolgreich: {tables} Tabellen, {rows} Zeilen.', 'success']


def test_the_automatic_login_may_export_as_well(server_port, auto_login):
    resp = post(server_port, '/db_export', headers={'Origin': own_origin(server_port)}, csrf=auto_login.csrf)
    assert location_of(resp)[0] == '/about'
    assert os.path.isfile(os.path.join(admin_dir(), 'db-export.sql'))


def test_an_ordinary_user_cannot_export_and_nothing_is_written(server_port, people):
    bob = people.bob
    resp = as_user(server_port, bob, '/db_export')
    assert resp.status == 403 and resp.text == handlers.DB_EXPORT_REFUSED
    assert resp.text == 'Der DB-Export steht nur Administratoren zur Verfügung.'
    assert resp.headers['Content-Type'] == 'text/plain; charset=utf-8' and 'Location' not in resp.headers
    assert userctx.DATA_ROOT not in resp.text
    assert not os.path.exists(os.path.join(bob.dir, 'db-export.sql'))
    assert not os.path.exists(os.path.join(people.admin.dir, 'db-export.sql'))


def test_the_demo_visitor_cannot_export_either(server_port, demo_site):
    resp = post(server_port, '/db_export', headers={'Origin': own_origin(server_port)}, csrf=demo_site.csrf)
    assert resp.status == 403 and resp.text == handlers.DB_EXPORT_REFUSED
    assert not os.path.exists(os.path.join(userctx.DATA_ROOT, 'users', 'demo', 'db-export.sql'))


def test_the_handler_asks_before_it_touches_the_database(tmp_db, monkeypatch):
    calls = []
    monkeypatch.setattr(tmp_db, 'export_to_sql', lambda path: calls.append(path) or (1, 1))
    monkeypatch.setattr(auth, 'sql_console_allowed', lambda *args, **kwargs: False)
    assert handlers.handle_db_export(tmp_db) == (403, handlers.DB_EXPORT_REFUSED) and calls == []
    monkeypatch.setattr(auth, 'sql_console_allowed', lambda *args, **kwargs: True)
    assert handlers.handle_db_export(tmp_db) == (303, '/about?export=ok&tables=1&rows=1') and len(calls) == 1


# ── Sicherungen löschen: die Liste und der Knopf ─────────────────────────────
ONCLICK = re.compile(r"""appConfirmPost\('/backup/delete', (".*?"), \{file: (".*?")\}\)""", re.S)


def delete_buttons(page):
    """``[(Name, Frage)]`` der Knöpfe „Löschen“ in der Liste der Sicherungen, so gelesen, wie der Browser das Attribut liest."""
    found = []
    for tag, attrs in Dom(page).tags:
        if tag == 'button' and '/backup/delete' in (attrs.get('onclick') or ''):
            parts = ONCLICK.fullmatch(attrs['onclick'])
            assert parts, attrs['onclick']
            found.append((json.loads(parts.group(2)), json.loads(parts.group(1))))
    return found


def test_every_listed_backup_has_a_delete_button_next_to_the_download(server_port, auto_login):
    put(NAME, b'x')
    put(MIGRATION, b'y')
    page = page_of(server_port, path='/miscellaneous')
    assert sorted(delete_buttons(page)) == sorted([(NAME, f'Sicherung {NAME} wirklich löschen?'),
                                                   (MIGRATION, f'Sicherung {MIGRATION} wirklich löschen? Über sie führt der Weg zurück auf die Version vor der Umstellung.')])
    row = re.search(rf'<tr><td>{re.escape(NAME)}</td>(.*?)</tr>', page).group(1)
    assert f'<a href="/backup/download?file={NAME}">Herunterladen</a> <button type="button" class="coloredButton btn-sm bg-red"' in row
    assert row.count('Löschen</button>') == 1 and row.rstrip().endswith('</button></td>')


@pytest.mark.skipif(os.name == 'nt', reason='Dieser Dateiname ist unter Windows nicht möglich')
def test_a_hostile_file_name_cannot_leave_the_attribute_or_the_script(server_port, auto_login):
    name = 'x"<b>\'_Backup.zip'
    put(name, b'inhalt')
    page = page_of(server_port, path='/miscellaneous')
    assert name not in page and [attrs for tag, attrs in Dom(page).tags if tag == 'b'] == []
    assert delete_buttons(page) == [(name, f'Sicherung {name} wirklich löschen?')]       # gelesen kommt genau der Name heraus
    resp = as_user(server_port, auto_login, '/backup/delete', form={'file': name})
    assert location_of(resp)[1] == {'backup_delete': ['ok'], 'msg': [name]}
    assert backup_mod.downloadable_backups(admin_dir()) == []


def test_without_backups_the_page_has_no_button(server_port, auto_login):
    page = page_of(server_port, path='/miscellaneous')
    assert 'Noch keine Sicherungen vorhanden.' in page and '/backup/delete' not in page


def test_in_the_demo_mode_the_page_lists_the_backups_without_the_delete_button(server_port, demo_site):
    put(NAME, b'x', user_dir=os.path.join(userctx.DATA_ROOT, 'users', 'demo'))
    page = page_of(server_port, path='/miscellaneous')
    assert f'<a href="/backup/download?file={NAME}">Herunterladen</a></td>' in page        # herunterladen geht weiter
    assert delete_buttons(page) == [] and '/backup/delete' not in page and 'Löschen</button>' not in page


def test_the_page_names_the_result_of_a_deletion_through_the_message_of_the_server(server_port, auto_login):
    """Der Vorspann der Meldung steht fest im Skript; aus der Adresse kommt nur der Name bzw. der Grund – ein Link kann der Seite so keinen
    beliebigen Satz als Erfolgsmeldung unterschieben."""
    page = page_of(server_port, path='/miscellaneous?backup_delete=ok&msg=' + urllib.parse.quote('x_Backup.zip'))
    assert misc_message(page) == 'x_Backup.zip'
    script = [s for s in Dom(page).scripts if "get('backup_delete')" in s]
    assert len(script) == 1
    assert "(ok ? 'Sicherung gelöscht: ' : 'Sicherung nicht gelöscht: ') + miscMsg" in script[0]
    assert "appMsg(text, ok ? 'success' : 'error')" in script[0] and 'appMsg(miscMsg' not in script[0]
    hostile = '</script><img src=x onerror=alert(1)>'
    page = page_of(server_port, path='/miscellaneous?backup_delete=error&msg=' + urllib.parse.quote(hostile))
    assert misc_message(page) == hostile and '<img src=x' not in page and hostile not in page


FOOTER_RUNNER = r"""
const vm = require('vm');
const fs = require('fs');
const footer = fs.readFileSync(process.argv[2], 'utf8');
const click = fs.readFileSync(process.argv[3], 'utf8');
const log = {submitted: []};
class Fake {
  constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.textContent = ''; this.hidden = false; this.value = ''; }
  appendChild(child) { this.children.push(child); return child; }
  submit() { log.submitted.push({action: this.action, method: this.method, fields: this.children.map(child => [child.name, child.value])}); }
}
const elements = {};
for (const id of ['app_confirm', 'app_confirm_text', 'app_confirm_date_row', 'app_confirm_date', 'app_confirm_date_error',
                  'app_confirm_date_label', 'app_confirm_ok', 'app_msg']) elements[id] = new Fake('div');
const meta = {content: 'token-4711'};
const sandbox = {
  document: {getElementById: id => elements[id], createElement: tag => new Fake(tag), body: new Fake('body'),
             querySelector: selector => (selector === 'meta[name="csrf-token"]' ? meta : null), addEventListener() {}},
  window: {fetch() {}}, Headers: class {}, setTimeout() {}, URL: {},
};
vm.createContext(sandbox);
vm.runInContext(footer, sandbox);
vm.runInContext(click, sandbox);
log.question = elements.app_confirm_text.textContent;
log.dialog = elements.app_confirm.style.display;
log.beforeOk = log.submitted.length;
elements.app_confirm_ok.onclick();
console.log(JSON.stringify(log));
"""


def _click(tmp_path, onclick):
    """Das Skript im Fuß der Seite in Node, mit einem gespielten Browser, und der Aufruf aus ``onclick``; danach der Klick auf „OK“."""
    footer = [script for script in Dom(Footer()).scripts if 'function appConfirm(' in script]
    assert len(footer) == 1
    (tmp_path / 'footer.js').write_text(footer[0], encoding='utf-8')
    (tmp_path / 'click.js').write_text(onclick, encoding='utf-8')
    (tmp_path / 'runner.js').write_text(FOOTER_RUNNER, encoding='utf-8')
    done = subprocess.run([NODE, str(tmp_path / 'runner.js'), str(tmp_path / 'footer.js'), str(tmp_path / 'click.js')],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
def test_the_button_asks_first_and_then_posts_the_file_name_and_the_token(server_port, auto_login, tmp_path):
    put(NAME, b'x')
    buttons = [attrs for tag, attrs in Dom(page_of(server_port, path='/miscellaneous')).tags
               if tag == 'button' and '/backup/delete' in (attrs.get('onclick') or '')]
    assert len(buttons) == 1
    log = _click(tmp_path, buttons[0]['onclick'])
    assert log['question'] == f'Sicherung {NAME} wirklich löschen?' and log['dialog'] == 'flex'
    assert log['beforeOk'] == 0                                                 # bis zur Antwort wird nichts gesendet
    assert log['submitted'] == [{'action': '/backup/delete', 'method': 'post',
                                 'fields': [['file', NAME], ['csrf', 'token-4711']]}]


@needs_node
def test_the_other_delete_buttons_still_post_only_the_token(tmp_path):
    """``appConfirmPost(url, text)`` ohne Felder wie bisher: nur das Token, kein weiteres Feld."""
    log = _click(tmp_path, "appConfirmPost('/receipts/delete?id=3', 'Beleg wirklich löschen?')")
    assert log['question'] == 'Beleg wirklich löschen?' and log['beforeOk'] == 0
    assert log['submitted'] == [{'action': '/receipts/delete?id=3', 'method': 'post', 'fields': [['csrf', 'token-4711']]}]


def test_appConfirmPost_hands_its_fields_to_appPost():
    footer = Footer()
    assert 'function appConfirmPost(url, text, fields) {\n  appConfirm(text, function() { appPost(url, fields); });\n}' in footer


# ── POST /backup/delete ──────────────────────────────────────────────────────
KINDS = [
    pytest.param('20260105_Backup.zip', id='Archiv-zip'),
    pytest.param('20260105_Backup.7zip', id='Archiv-7zip'),
    pytest.param('20260105_Backup_2.7z', id='Archiv-7z-nummeriert'),
    pytest.param('20261120_vor_2.5.0_buch.db', id='Kopie-vor-Umstellung'),
    pytest.param('20261120_vor_2.5.0_2_buch.db', id='Kopie-vor-Umstellung-nummeriert'),
]


@pytest.mark.parametrize('name', KINDS)
def test_every_kind_of_the_positive_list_is_deleted_and_the_message_names_it(server_port, auto_login, name):
    kept = put('20250101_Backup.zip', b'bleibt')
    other = put('notiz.txt', b'gehoert-nicht-dazu')
    path = put(name, b'weg')
    resp = as_user(server_port, auto_login, '/backup/delete', form={'file': name})
    assert location_of(resp) == ('/miscellaneous', {'backup_delete': ['ok'], 'msg': [name]})
    assert not os.path.exists(path) and os.path.exists(kept) and os.path.exists(other)
    assert [entry.name for entry in backup_mod.downloadable_backups(admin_dir())] == ['20250101_Backup.zip']
    page = page_of(server_port, path=resp.headers['Location'])
    table = page.split('<table class="backupTable">')[1].split('</table>')[0]
    assert name not in table and '20250101_Backup.zip' in table
    assert misc_message(page) == name


REFUSED = [
    pytest.param('gibtsnicht_Backup.zip', id='unbekannt'),
    pytest.param('../buch.db', id='aufwaerts'),
    pytest.param('..\\buch.db', id='aufwaerts-rueckstrich'),
    pytest.param('/etc/passwd', id='absolut'),
    pytest.param('a/b_Backup.zip', id='mit-Pfadanteil'),
    pytest.param('backup/' + NAME, id='mit-Ordner'),
    pytest.param(NAME + '/', id='Schraegstrich-am-Ende'),
    pytest.param(NAME + '\0', id='NUL'),
    pytest.param('C:\\Windows\\x_Backup.zip', id='Laufwerk'),
    pytest.param('buch.db', id='die-Datenbank'),
    pytest.param('', id='leer'),
    pytest.param('a' * 300, id='zu-lang'),
    pytest.param('.versteckt_Backup.zip', id='versteckt'),
    pytest.param('replaced_x_Backup.zip', id='replaced-Datei'),
    pytest.param('restore_x', id='restore-Ordner'),
    pytest.param('replaced_x', id='replaced-Ordner'),
    pytest.param(NAME + '.part', id='Teildatei'),
    pytest.param(NAME + '.tmp', id='tmp-Datei'),
    pytest.param('20261120_Backup_3.zip', id='Ordner-mit-Archivnamen'),
    pytest.param('20261120_Backup_9.zip', id='Link'),
    pytest.param('20261120_vor_2.5.0_buch.db.part', id='halbe-Kopie'),
    pytest.param('20261120_vor_2.5_buch.db', id='Kopie-ohne-Patchstelle'),
    pytest.param('20261120_vor_2.5.0_anders.db', id='Kopie-anderer-Datei'),
    pytest.param('notiz.txt', id='fremde-Datei'),
]


def state_of(auto_login):
    """Der Stand von backup/ (Dateien samt Inhalt) und der Datenbank."""
    return installation.tree(backup_folder()), pathlib.Path(auto_login.db.db_name).read_bytes()


def plant_everything(auto_login):
    """Eine echte Sicherung, die Attrappen von ``plant_decoys`` und ein Link auf die Datenbank; liefert den Stand davon."""
    plant_decoys()
    try:
        os.symlink(os.path.join('..', 'buch.db'), os.path.join(backup_folder(), '20261120_Backup_9.zip'))
    except (OSError, NotImplementedError):
        pass                                                      # ohne Links lässt sich nur der Fall „Link“ nicht prüfen
    return state_of(auto_login)


@pytest.mark.parametrize('name', REFUSED)
def test_everything_outside_the_list_is_refused_with_one_sentence_and_nothing_is_deleted(server_port, auto_login, name):
    before = plant_everything(auto_login)
    if name == '20261120_Backup_9.zip' and not os.path.islink(os.path.join(backup_folder(), name)):
        pytest.skip('das Dateisystem kann keine symbolischen Links anlegen')
    resp = as_user(server_port, auto_login, '/backup/delete', form={'file': name})
    assert location_of(resp) == ('/miscellaneous', {'backup_delete': ['error'], 'msg': [backup_mod.DOWNLOAD_NOT_FOUND]})
    assert userctx.DATA_ROOT not in resp.headers['Location'] and 'buch.db' not in resp.headers['Location']
    assert state_of(auto_login) == before
    assert os.path.isfile(os.path.join(admin_dir(), 'backup', NAME))             # die echte Sicherung steht noch


@pytest.mark.parametrize('fields', [
    pytest.param([], id='kein-Feld'),
    pytest.param([('file', NAME), ('file', NAME)], id='zweimal-dasselbe'),
    pytest.param([('file', NAME), ('file', '20250101_Backup.zip')], id='zwei-Namen'),
    pytest.param([('datei', NAME)], id='anderer-Feldname'),
])
def test_no_name_or_several_names_delete_nothing(server_port, auto_login, fields):
    put(NAME, b'a')
    put('20250101_Backup.zip', b'b')
    before = installation.tree(backup_folder())
    body = urllib.parse.urlencode([*fields, ('csrf', auto_login.csrf)])
    resp = send(server_port, 'POST', '/backup/delete',
                {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': own_origin(server_port)}, body)
    assert location_of(resp) == ('/miscellaneous', {'backup_delete': ['error'], 'msg': [backup_mod.DOWNLOAD_NOT_FOUND]})
    assert installation.tree(backup_folder()) == before


def test_a_get_is_405_and_deletes_nothing(server_port, auto_login):
    path = put(NAME, b'x')
    for query in (f'?file={NAME}', '', f'?file={NAME}&id=1'):
        resp = get(server_port, '/backup/delete' + query)
        assert resp.status == 405 and resp.headers['Allow'] == 'POST'
    assert os.path.exists(path)


def test_the_route_is_an_action_and_nothing_else():
    assert '/backup/delete' in server_app._ACTION_PATHS
    for name in ('_GENERATE_PATHS', '_JSON_PATHS', '_POST_ONLY_JSON_PATHS', '_SEARCH_PATHS', '_UPLOAD_PATHS', '_LOCK_FREE_ROUTES'):
        assert '/backup/delete' not in getattr(server_app, name), name


@pytest.mark.parametrize('kind', [
    pytest.param('ohne-Token', id='ohne-Token'),
    pytest.param('falsches-Token', id='falsches-Token'),
    pytest.param('Token-des-anderen', id='Token-eines-anderen-Benutzers'),
])
def test_without_the_right_token_nothing_is_deleted(server_port, people, kind):
    path = put(NAME, b'x', user_dir=people.admin.dir)
    token = {'ohne-Token': None, 'falsches-Token': 'falsch', 'Token-des-anderen': people.bob.csrf}[kind]
    resp = post(server_port, '/backup/delete', headers={'Origin': own_origin(server_port), **people.admin.cookie},
                form={'file': NAME}, csrf=token)
    assert resp.status == 403 and 'Sicherheits-Token' in resp.text
    assert os.path.exists(path)


@pytest.mark.parametrize('headers', [
    pytest.param({'Origin': FOREIGN_ORIGIN}, id='fremder-Origin'),
    pytest.param({'Sec-Fetch-Site': 'cross-site'}, id='fremde-Seite'),
    pytest.param({'Sec-Fetch-Site': 'same-site'}, id='andere-Seite-derselben-Site'),
])
def test_from_a_foreign_page_nothing_is_deleted_even_with_the_token(server_port, auto_login, headers):
    path = put(NAME, b'x')
    resp = post(server_port, '/backup/delete', headers=headers, form={'file': NAME}, csrf=auto_login.csrf)
    assert resp.status == 403
    assert os.path.exists(path)


def test_in_the_demo_mode_the_route_is_403_and_nothing_is_deleted(server_port, demo_site):
    path = put(NAME, b'x', user_dir=os.path.join(userctx.DATA_ROOT, 'users', 'demo'))
    resp = post(server_port, '/backup/delete', headers={'Origin': own_origin(server_port)}, form={'file': NAME}, csrf=demo_site.csrf)
    assert resp.status == 403 and resp.text == handlers.BACKUP_DELETE_DEMO_REFUSED == DEMO_DENIED
    assert resp.headers['Content-Type'] == 'text/plain; charset=utf-8' and 'Location' not in resp.headers
    assert os.path.exists(path) and userctx.DATA_ROOT not in resp.text


def test_while_a_backup_or_restore_runs_the_deletion_is_refused_without_waiting(server_port, auto_login):
    path = put(NAME, b'x')
    with backup_mod._exclusive(admin_dir()):
        resp = as_user(server_port, auto_login, '/backup/delete', form={'file': NAME})
    assert location_of(resp) == ('/miscellaneous', {'backup_delete': ['error'], 'msg': [backup_mod.BUSY]})
    assert os.path.exists(path)
    resp = as_user(server_port, auto_login, '/backup/delete', form={'file': NAME})          # danach geht es
    assert location_of(resp)[1]['backup_delete'] == ['ok'] and not os.path.exists(path)


def test_a_user_deletes_only_in_the_own_folder(server_port, people):
    theirs = put(NAME, b'Sicherung des Administrators', user_dir=people.admin.dir)
    resp = as_user(server_port, people.bob, '/backup/delete', form={'file': NAME})           # bob hat keine Sicherung dieses Namens
    assert location_of(resp)[1] == {'backup_delete': ['error'], 'msg': [backup_mod.DOWNLOAD_NOT_FOUND]}
    assert os.path.exists(theirs)
    mine = put(NAME, b'Sicherung von Bob', user_dir=people.bob.dir)                          # derselbe Name im eigenen Ordner
    resp = as_user(server_port, people.bob, '/backup/delete', form={'file': NAME})
    assert location_of(resp)[1]['backup_delete'] == ['ok']
    assert not os.path.exists(mine) and os.path.exists(theirs)
    resp = as_user(server_port, people.bob, '/backup/delete', form={'file': f'../admin/backup/{NAME}'})
    assert location_of(resp)[1]['backup_delete'] == ['error'] and os.path.exists(theirs)


def test_a_failing_removal_gives_a_fixed_sentence_and_the_error_only_goes_to_the_log(server_port, auto_login, monkeypatch, capsys):
    path = put(NAME, b'x')
    real = os.remove

    def broken(target, *args, **kwargs):
        if os.fspath(target) == path:
            raise OSError('/geheim/pfad kaputt')
        return real(target, *args, **kwargs)
    monkeypatch.setattr(backup_mod.os, 'remove', broken)
    resp = as_user(server_port, auto_login, '/backup/delete', form={'file': NAME})
    assert location_of(resp) == ('/miscellaneous', {'backup_delete': ['error'], 'msg': [backup_mod.DELETE_FAILED]})
    assert 'geheim' not in resp.headers['Location'] and userctx.DATA_ROOT not in resp.headers['Location']
    assert '/geheim/pfad kaputt' in capsys.readouterr().err
    assert os.path.exists(path)


def test_an_unexpected_error_is_a_fixed_sentence_too(server_port, auto_login, monkeypatch, capsys):
    put(NAME, b'x')

    def boom(user_dir, name):
        raise RuntimeError('/geheim/pfad ganz anders')
    monkeypatch.setattr(backup_mod, 'delete_backup', boom)
    resp = as_user(server_port, auto_login, '/backup/delete', form={'file': NAME})
    assert location_of(resp)[1] == {'backup_delete': ['error'], 'msg': [backup_mod.DELETE_FAILED]}
    assert 'geheim' not in resp.headers['Location'] and '/geheim/pfad ganz anders' in capsys.readouterr().err


def test_the_handler_refuses_in_the_demo_mode_before_it_looks_at_anything(user_dir, monkeypatch):
    monkeypatch.setattr(userctx, 'user_data_dir', lambda: str(user_dir))
    (user_dir / 'backup' / NAME).write_bytes(b'x')
    assert handlers.handle_backup_delete({'file': [NAME]})[0] == 303 and not (user_dir / 'backup' / NAME).exists()
    (user_dir / 'backup' / NAME).write_bytes(b'x')
    monkeypatch.setattr(handlers.demo, 'is_active', lambda: True)
    assert handlers.handle_backup_delete({'file': [NAME]}) == (403, handlers.BACKUP_DELETE_DEMO_REFUSED)
    assert handlers.handle_backup_delete({}) == (403, handlers.BACKUP_DELETE_DEMO_REFUSED)
    assert (user_dir / 'backup' / NAME).exists()


# ── backup.delete_backup ─────────────────────────────────────────────────────
@pytest.fixture
def user_dir(tmp_path):
    folder = tmp_path / 'user'
    (folder / 'backup').mkdir(parents=True)
    (folder / 'buch.db').write_bytes(b'die Datenbank')
    return folder


def test_delete_backup_removes_exactly_one_listed_file_and_returns_its_entry(user_dir):
    for name in (NAME, MIGRATION, '20250101_Backup.zip'):
        (user_dir / 'backup' / name).write_bytes(name.encode())
    entry = backup_mod.delete_backup(str(user_dir), MIGRATION)
    assert entry.name == MIGRATION and entry.kind == 'migration' and entry.version == '2.5.0'
    assert sorted(os.listdir(user_dir / 'backup')) == ['20250101_Backup.zip', NAME]
    assert backup_mod.delete_backup(str(user_dir), NAME).kind == 'archive'
    assert os.listdir(user_dir / 'backup') == ['20250101_Backup.zip']


def test_delete_backup_never_creates_backup_and_says_not_found_without_it(tmp_path):
    folder = tmp_path / 'ohne'
    folder.mkdir()
    with pytest.raises(backup_mod.BackupNotFound) as raised:
        backup_mod.delete_backup(str(folder), NAME)
    assert str(raised.value) == backup_mod.DOWNLOAD_NOT_FOUND and os.listdir(folder) == []


@pytest.mark.parametrize('make', [
    pytest.param(lambda folder: (folder / 'restore_x').mkdir(), id='restore-Ordner'),
    pytest.param(lambda folder: (folder / '20261120_Backup_3.zip').mkdir(), id='Ordner-mit-Archivnamen'),
    pytest.param(lambda folder: (folder / (NAME + '.part')).write_bytes(b'x'), id='Teildatei'),
    pytest.param(lambda folder: (folder / 'replaced_x_Backup.zip').write_bytes(b'x'), id='replaced-Datei'),
    pytest.param(lambda folder: make_symlink(os.path.join('..', 'buch.db'), folder / '20261120_Backup_9.zip'), id='Link'),
])
def test_delete_backup_leaves_everything_else_alone(user_dir, make):
    folder = user_dir / 'backup'
    make(folder)
    name = sorted(os.listdir(folder))[0]
    before = installation.tree(user_dir)
    with pytest.raises(backup_mod.BackupNotFound):
        backup_mod.delete_backup(str(user_dir), name)
    assert installation.tree(user_dir) == before


def test_delete_backup_does_not_follow_a_link_that_replaces_the_file_after_the_list(user_dir, monkeypatch):
    """Zwischen der Liste und dem Löschen wird die Datei durch einen Link ersetzt: Das Ziel bleibt, wo es ist – gelöscht wird nie ein
    Link-Ziel, auch kein Ziel außerhalb von backup/."""
    make_symlink(os.path.join('..', 'buch.db'), user_dir / 'backup' / NAME)
    entry = backup_mod.BackupEntry(NAME, 'archive', 13, 0.0, None)
    monkeypatch.setattr(backup_mod, 'downloadable_backups', lambda folder: [entry])
    try:
        backup_mod.delete_backup(str(user_dir), NAME)
    except backup_mod.BackupNotFound:
        pass
    assert (user_dir / 'buch.db').read_bytes() == b'die Datenbank'


def test_delete_backup_works_when_backup_itself_is_a_link_to_another_place(tmp_path):
    elsewhere = tmp_path / 'woanders'
    elsewhere.mkdir()
    (elsewhere / NAME).write_bytes(b'auf dem anderen Laufwerk')
    folder = tmp_path / 'user'
    folder.mkdir()
    make_symlink(elsewhere, folder / 'backup')
    assert backup_mod.delete_backup(str(folder), NAME).name == NAME
    assert os.listdir(elsewhere) == []


def test_delete_backup_is_refused_while_a_backup_runs_and_does_not_wait(user_dir):
    (user_dir / 'backup' / NAME).write_bytes(b'x')
    with backup_mod._exclusive(str(user_dir)):
        with pytest.raises(backup_mod.BackupRefused) as raised:
            backup_mod.delete_backup(str(user_dir), NAME)
    assert str(raised.value) == backup_mod.BUSY and not isinstance(raised.value, backup_mod.BackupNotFound)
    assert (user_dir / 'backup' / NAME).exists()


def test_delete_backup_names_no_path_when_the_removal_fails(user_dir, monkeypatch):
    path = user_dir / 'backup' / NAME
    path.write_bytes(b'x')

    def broken(target, *args, **kwargs):
        raise PermissionError(13, 'Zugriff verweigert', os.fspath(target))
    monkeypatch.setattr(backup_mod.os, 'remove', broken)
    with pytest.raises(backup_mod.BackupRefused) as raised:
        backup_mod.delete_backup(str(user_dir), NAME)
    assert str(raised.value) == backup_mod.DELETE_FAILED and str(user_dir) not in str(raised.value)
    assert isinstance(raised.value.__cause__, PermissionError)                       # die Ursache hängt an der Kette (Server-Log)
    assert path.exists()


def test_the_visible_texts_that_name_the_page_say_Daten():
    assert backup_mod.DOWNLOAD_FOREIGN == 'Sicherungen lassen sich nur über die Seite „Daten“ herunterladen.'
    from db import invoice_payments
    for text in (invoice_payments.MIXED_RECEIVABLE_MISMATCH, invoice_payments.TAX_PAYMENT_NO_REVENUE,
                 invoice_payments.TAX_ENTRY_CONTRADICTION):
        assert '(Prüfung unter Daten)' in text and 'Sonstiges' not in text
