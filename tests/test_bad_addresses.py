# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Unsinnige Adressen geben keinen 500er mehr (2.3.6), gegen einen echten Server auf 127.0.0.1.

Vorher scheiterten fast alle Seiten mit ID-Parameter an einer fehlenden oder keine Ganzzahl enthaltenden ID
(``IndexError`` bei Adressen ganz ohne ``?``, ``KeyError``/``ValueError`` bei ``?id=abc``) mit
„Server Fehler“ (500); die Löschaktionen (POST) ebenso.

* Seiten, die ein bestimmtes Objekt zeigen (bearbeiten, ansehen, herunterladen): Eine fehlende oder ungültige
  ID antwortet wie eine unbekannte – „… nicht gefunden“ (404) bzw. die Seite mit leerer Maske.
* Seiten mit einem Filter (``/invoice?id=``, ``/dashboard?acct=``, ``/transactions?from_invoice=`` …): Ein
  ungültiger Wert gilt als nicht angegeben.
* ``/worktime/pdf`` ohne gültige Person: 400 wie bisher (auch ganz ohne ``?``).
* Löschaktionen ohne gültige ID: 400 „Ungültige oder fehlende ID.“ (wie ``/invoice/delete``), nichts geändert.
* Die Schrift-Dateien des PDF-Aufbaus werden nach dem Untermengen-Bilden geschlossen (sonst ResourceWarning
  bei jedem Stundenzettel und jeder Rechnung).

Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import gc
import os
import re
import warnings

import pytest

import export.pdfa as pdfa
from server import app as server_app
from server.app import _ACTION_PATHS
from tests.document_helpers import CUSTOMER, INVOICE_NUMBER, make_books, state
from tests.http_client import get, own_origin, post

# Seiten, die ein bestimmtes Objekt zeigen: (Pfad, Name des ID-Parameters)
TABLES = ('Articles', 'Contacts', 'Accounts', 'Bookings', 'Invoices', 'InvoiceItems', 'Assets',
          'AssetCategories', 'NumberRanges', 'WorkTimes', 'Trips', 'ChartOfAccounts', 'Documents')

SUBJECT_PAGES = [
    ('/invoice/view', 'id'), ('/invoice/edit', 'id'), ('/invoice/xml_download', 'id'),
    ('/invoice/pdf_download', 'id'), ('/quote/edit', 'id'), ('/quote/pdf_download', 'id'),
    ('/masterdata/articles/edit', 'id'), ('/masterdata/contacts/edit', 'id'),
    ('/masterdata/skr/edit', 'id'), ('/masterdata/skr/copy', 'id'),
    ('/masterdata/bankaccounts/edit', 'id'), ('/receipts/edit', 'id'), ('/receipts/file', 'id'),
    ('/transactions/edit', 'id'), ('/masterdata/numberranges/edit', 'id'), ('/assets/edit', 'id'),
    ('/assets/view', 'id'), ('/asset_categories/edit', 'id'), ('/worktime/edit', 'id'), ('/trips/edit', 'id'),
]
# Seiten mit einem Filter: Ein ungültiger Wert gilt als nicht angegeben
FILTER_PAGES = [
    ('/invoice', 'id'), ('/invoice', 'copy_from'), ('/quote', 'id'), ('/transactions', 'from_invoice'),
    ('/assets/new', 'parent_id'), ('/dashboard', 'acct'),
]


@pytest.fixture
def books(auto_login):
    """Eigene Firma, ein Kunde, eine Rechnung und ein Angebot (ohne eigene Firma wäre /dashboard die Ersteinrichtung)."""
    return make_books(auto_login)


@pytest.mark.parametrize('path, name', SUBJECT_PAGES, ids=[f'{p}?{n}' for p, n in SUBJECT_PAGES])
def test_a_missing_or_invalid_id_answers_like_an_unknown_one(server_port, books, path, name):
    unknown = get(server_port, f'{path}?{name}=99999')
    assert unknown.status in (200, 404)
    # … und eine Zahl, die in keine 64 Bit passt (ließe sich nicht an eine Abfrage binden)
    for query in ('', f'?{name}=abc', f'?{name}=', f'?{name}=1.5', f'?{name}={2 ** 63}', f'?{name}={"9" * 30}'):
        resp = get(server_port, path + query)
        assert resp.status == unknown.status, query
        if len(unknown.body) < 200:                       # die Meldung „… nicht gefunden.“ selbst
            assert resp.body == unknown.body, query


@pytest.mark.parametrize('path, name', FILTER_PAGES, ids=[f'{p}?{n}' for p, n in FILTER_PAGES])
def test_an_invalid_filter_value_counts_as_not_given(server_port, books, path, name):
    plain = get(server_port, path)
    assert plain.status == 200
    for query in (f'?{name}=abc', f'?{name}=', f'?{name}=1.5'):
        resp = get(server_port, path + query)
        assert resp.status == 200 and resp.body == plain.body, query


def test_the_dashboard_keeps_the_valid_accounts_and_drops_the_others(server_port, books):
    assert get(server_port, '/dashboard?acct=abc&acct=1').body == get(server_port, '/dashboard?acct=1').body
    assert get(server_port, '/dashboard?acct=abc&acct=1').body != get(server_port, '/dashboard').body


@pytest.mark.parametrize('query', ['', '?id=abc', '?person=abc', '?person='])
def test_the_worktime_pdf_without_a_valid_person_is_400(server_port, books, query):
    resp = get(server_port, '/worktime/pdf' + query)
    assert resp.status == 400 and resp.text == 'Ungültige oder fehlende Person.'


def test_valid_ids_still_open_their_pages(server_port, books):
    assert INVOICE_NUMBER in get(server_port, f'/invoice/view?id={books.invoice}').text
    assert INVOICE_NUMBER in get(server_port, f'/invoice?id={books.invoice}').text
    assert CUSTOMER in get(server_port, f'/masterdata/contacts/edit?id={books.customer}').text
    resp = get(server_port, f'/invoice/pdf_download?id={books.invoice}')
    assert resp.status == 404 and resp.text != 'Rechnung nicht gefunden.'      # es gibt die Rechnung, nur keine Datei


def test_the_not_found_messages_are_the_known_ones(server_port, books):
    assert get(server_port, '/invoice/pdf_download').text == 'Rechnung nicht gefunden.'
    assert get(server_port, '/invoice/xml_download?id=abc').text == 'Rechnung nicht gefunden.'
    assert get(server_port, '/quote/pdf_download?id=').text == 'Angebot nicht gefunden.'
    assert get(server_port, '/masterdata/contacts/edit').text == 'Kontakt nicht gefunden.'
    assert get(server_port, '/receipts/edit').text == 'Beleg nicht gefunden.'


# ── Löschaktionen (POST) ─────────────────────────────────────────────────────
# Aktionen, die gar keine ID nennen (die ID-Prüfung gilt dort nicht; ``_post_action_routes``)
ACTIONS_WITHOUT_ID = frozenset({'/masterdata/skr/add_standard'})


def action(port, csrf, path_and_query):
    return post(port, path_and_query, headers={'Origin': own_origin(port)}, csrf=csrf)


@pytest.mark.parametrize('path', sorted(_ACTION_PATHS - ACTIONS_WITHOUT_ID))
def test_an_action_without_a_valid_id_is_400_and_changes_nothing(server_port, books, path):
    before = state(books.db, *TABLES)
    # /receipts/delete nimmt seit 2.4.2 die ID des Belegs (vorher die Nummer, die nicht eindeutig ist): wie jede Löschaktion
    wrong = ['', '?id=abc', '?id=', '?id=1.5']
    for query in wrong:
        resp = action(server_port, books.csrf, path + query)
        assert resp.status == 400, query
        assert resp.text == 'Ungültige oder fehlende ID.', query
        assert resp.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert state(books.db, *TABLES) == before


@pytest.mark.parametrize('path', sorted(ACTIONS_WITHOUT_ID))
@pytest.mark.parametrize('query', ['', '?id=abc', '?id=', '?id=1.5', '?id=1'])
def test_an_action_without_an_id_runs_whatever_the_id_parameter_says(server_port, books, path, query):
    resp = action(server_port, books.csrf, path + query)
    assert resp.status == 303 and resp.headers['Location'].startswith('/masterdata/skr?added=')


def test_an_action_with_a_valid_id_still_runs(server_port, books):
    books.db.insert_article(name='Probeartikel', unit_price=10, tax_rate=19)
    article_id = books.db.fetch_articles()[0][0]
    resp = action(server_port, books.csrf, f'/masterdata/articles/delete?id={article_id}')
    assert resp.status == 303 and resp.headers['Location'] == '/masterdata/articles'
    assert books.db.get_article_by_id(article_id) is None


def test_the_action_paths_still_need_a_post(server_port, books):
    for path in sorted(_ACTION_PATHS):
        assert get(server_port, path).status == 405


# ── Wächter gegen Rückfälle ──────────────────────────────────────────────────
def test_app_py_has_no_unprotected_query_parsing_left():
    with open(server_app.__file__, encoding='utf-8') as f:
        source = f.read()
    source = re.sub(r'def _int_param\(.*?\n\n\n', '', source, flags=re.S)       # sie selbst fängt den Fehler ab
    assert "split('?')[1]" not in source                            # IndexError ohne „?“
    assert not re.search(r'int\((qs|qc|query_components)\[', source)  # KeyError/ValueError bei fehlender/falscher ID
    assert 'int(pid)' not in source


# ── Schriftdateien ───────────────────────────────────────────────────────────
@pytest.mark.parametrize('font', ['LiberationSans-Regular.ttf', 'LiberationSans-Bold.ttf',
                                  'LiberationSans-Italic.ttf'])
def test_the_font_file_is_closed_after_subsetting(font):
    path = os.path.join(os.path.dirname(pdfa.__file__), 'fonts', font)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        data, metrics = pdfa._subset_font(path, set('Stundenzettel 0123456789'))
        gc.collect()
    assert [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)] == []
    assert data and len(metrics['widths']) == 224
