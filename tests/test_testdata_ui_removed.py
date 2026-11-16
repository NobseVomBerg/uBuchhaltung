# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""„Testdaten laden“ gibt es in der Oberfläche nicht mehr (2.3.9).

Für normale Benutzer ergibt die Funktion keinen Sinn; Beispieldaten gibt es im Demo-Modus, der sie beim Start selbst
lädt (``demo.prepare``: ``load_test_seed_data(include_invoices=False)`` und ``demo_seed``) – auch der Demo-Besucher
braucht keinen Knopf. Bis 2.3.8 lud der Knopf auf *Sonstiges* (und auf der Einrichtungsseite) Beispielkontakte,
-rechnungen, -buchungen und -anlagen in den Bestand, nur in einen leeren (``Database.stock_is_empty``,
``handlers.handle_load_testdata``, ``POST /setup/load_testdata``). All das ist weg.

* Weder die Seite *Sonstiges* noch die Einrichtungsseite bieten Testdaten an – für einen normalen Benutzer wie im
  Demo-Modus, bei leerem wie bei gefülltem Bestand.
* ``POST /setup/load_testdata`` gibt es nicht mehr: Die Antwort ist die eines unbekannten Pfades, der Bestand bleibt.
* ``Database.load_test_seed_data`` bleibt (Demo-Modus, Tests, Fixture ``db_with_coa``). Dass der Demo-Start seine Daten
  lädt, prüft test_demo.py (``test_seeds_the_demo_scenario`` u. a.).

Alle Namen sind erfundene Testwerte.
"""
import contextlib
import sqlite3
import urllib.parse

import pytest

from server.pages_miscellaneous import PageMiscellaneous
from server.pages_setup import PageSetup
from tests import installation
from tests.document_helpers import add_document
from tests.dom_helpers import Dom
from tests.http_client import get, own_origin, post
from tests.test_demo_http import demo_site  # noqa: F401  (Fixture: Besucher des Demo-Modus)

TABLES = ('Contacts', 'Invoices', 'InvoiceItems', 'Bookings', 'Assets', 'Articles')


def counts(db, *tables):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return {table: con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables}


def offers_test_data(html):
    """Was an einer Seite Testdaten anbietet: das Wort (auch englisch), das Reagenzglas oder ein Verweis auf die alte Route
    – ob als Formular, Knopf oder Text."""
    lowered = html.lower()
    return [token for token in ('testdat', '🧪', 'load_testdata') if token in lowered]


def _fill(db, filled):
    if filled:
        add_document(db, 'R-1', 'invoice')


# ── die Seiten ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize('filled', [False, True], ids=['leerer-Bestand', 'gefuellter-Bestand'])
def test_neither_page_offers_test_data(tmp_db, filled):
    _fill(tmp_db, filled)
    assert offers_test_data(PageMiscellaneous(tmp_db)) == []
    assert offers_test_data(PageSetup(tmp_db)) == []


def test_the_set_up_page_keeps_its_form(tmp_db):
    """Der Rest der Einrichtungsseite bleibt: das Formular für die eigenen Daten, das nach ``/setup/save`` sendet."""
    forms = Dom(PageSetup(tmp_db)).all('form')
    assert [form.get('action') for form in forms] == ['/setup/save']


@pytest.mark.parametrize('filled', [False, True], ids=['leerer-Bestand', 'gefuellter-Bestand'])
@pytest.mark.parametrize('path', ['/miscellaneous', '/setup'])
def test_the_running_server_offers_no_test_data(server_port, auto_login, path, filled):
    _fill(auto_login.db, filled)
    resp = get(server_port, path)
    assert resp.status == 200
    assert offers_test_data(resp.text) == []


@pytest.mark.parametrize('value', ['refused', '<script>alert(1)</script>'])
def test_a_leftover_address_parameter_changes_nothing(server_port, auto_login, value):
    """Bis 2.3.8 trug die Weiterleitung nach einer Ablehnung ``?testdata=refused``: Heute liest die Seite es nicht mehr."""
    resp = get(server_port, '/miscellaneous?' + urllib.parse.urlencode({'testdata': value}))
    assert resp.status == 200
    assert offers_test_data(resp.text) == []
    assert value not in resp.text


@pytest.mark.parametrize('path', ['/miscellaneous', '/setup'])
def test_the_demo_visitor_is_offered_no_test_data_either(server_port, demo_site, path):
    resp = get(server_port, path)
    assert resp.status == 200 and 'demoBanner' in resp.text      # die Seite der Demo, nicht eine Weiterleitung
    assert offers_test_data(resp.text) == []


# ── die Route ────────────────────────────────────────────────────────────────
def _post_as_the_visitor(port, login, path):
    return post(port, path, headers={'Origin': own_origin(port)}, csrf=login.csrf)


def test_the_old_route_loads_nothing_and_answers_like_an_unknown_path(server_port, auto_login):
    before = installation.dump(auto_login.db.db_name)
    old = _post_as_the_visitor(server_port, auto_login, '/setup/load_testdata')
    unknown = _post_as_the_visitor(server_port, auto_login, '/setup/gibt_es_nicht')
    assert (old.status, old.body) == (unknown.status, unknown.body)
    assert old.status == 404 and old.text == 'Seite nicht gefunden.'
    assert installation.dump(auto_login.db.db_name) == before
    assert not any(counts(auto_login.db, *TABLES).values())


def test_the_old_route_loads_nothing_for_the_demo_visitor_either(server_port, demo_site):
    before = counts(demo_site.db, *TABLES)
    resp = _post_as_the_visitor(server_port, demo_site, '/setup/load_testdata')
    assert resp.status == 404
    assert counts(demo_site.db, *TABLES) == before


# ── die Datenbankmethode bleibt ──────────────────────────────────────────────
def test_the_database_method_still_loads_the_test_data(tmp_db):
    """Der Demo-Modus, die Tests und die Fixture ``db_with_coa`` laden die Testdaten direkt."""
    tmp_db.load_test_seed_data()
    after = counts(tmp_db, *TABLES)
    assert all(after.values()), after


def test_the_database_method_does_not_ask_for_an_empty_stock(tmp_db):
    """Es prüft nichts – auch in einen Bestand mit Buchung lädt es (Tests und Demodaten verlassen sich darauf)."""
    tmp_db.insert_booking('2026-03-04', 10)
    before = counts(tmp_db, 'Contacts')['Contacts']
    tmp_db.load_test_seed_data()
    assert counts(tmp_db, 'Contacts')['Contacts'] > before
