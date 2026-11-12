# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die abgelegten Fassungen herunterladen (2.2.0): ``GET /invoice/version_download?id=&file=``.

Rein lesend. ``file`` ist ein reiner Dateiname (kein Trenner, kein ``..``, kein Steuerzeichen); gefunden wird er nur
als ``.pdf``/``.xml``-Datei in einem Ordner ``Fassungen/<id>`` genau dieser Rechnung und nur, wenn sie nach dem
Auflösen von Links im Dokumentordner liegt. Alles andere – ein Pfadanteil, die ID einer anderen Rechnung oder
eines Angebots, ein Name, der nur außerhalb von „Fassungen“ existiert – ist 404 mit kurzem Klartext (ohne Pfade).
Auch eine Datei aus 2.1.2, zu der es keine Tabellenzeile gibt, lässt sich laden. Der Dateiname kommt prozentkodiert im
Header ``X-Filename`` (Umlaute!).

Im selben Zug: Bei automatischer Anmeldung ist der Administrator der Benutzer im Protokoll.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import hashlib
import json
import os
from urllib.parse import quote, unquote

import pytest

from tests import installation
from tests.document_helpers import (add_document, document_files, post_json, read_file, user_dir,
                                    write_file)
from tests.http_client import get
from tests.invoice_world import archived_versions, build_world, make_payload, protocol, run_sql


@pytest.fixture
def books(auto_login):
    w = build_world(auto_login.db)
    w.csrf = auto_login.csrf
    return w


def versions_folder(invoice_id, year='2026'):
    return os.path.join(user_dir(), 'invoices', year, 'Fassungen', str(invoice_id))


def corrected_over_http(port, w, **payload):
    """Eine Rechnung über den Server anlegen, versenden und berichtigen: Fassung 1 (PDF + XML) liegt in „Fassungen“."""
    created = post_json(port, '/invoice/save', make_payload(w, **payload), w.csrf)
    invoice_id = json.loads(created.text)['invoice_id']
    sent = post_json(port, '/invoice/status', {'invoice_id': invoice_id, 'status': 'sent'}, w.csrf)
    assert sent.status == 200 and 'warning' not in json.loads(sent.text), sent.text
    saved = post_json(port, '/invoice/save', {'invoiceId': invoice_id, 'documentVersion': 1, 'orderNumber': 'B-99'},
                      w.csrf)
    assert saved.status == 200 and json.loads(saved.text)['documentVersion'] == 2, saved.text
    return invoice_id


def archived_names(invoice_id):
    return sorted(os.listdir(versions_folder(invoice_id)))


def url(invoice_id, name):
    return f'/invoice/version_download?id={invoice_id}&file={quote(name, safe="")}'


# ── Ausgeliefert wird, was abgelegt ist ──────────────────────────────────────

def test_an_archived_version_is_delivered_byte_for_byte_with_its_name_and_type(server_port, books):
    invoice_id = corrected_over_http(server_port, books)
    pdf = next(n for n in archived_names(invoice_id) if n.endswith('.pdf'))
    xml = next(n for n in archived_names(invoice_id) if n.endswith('.xml'))
    assert '–' in pdf                                                            # ein Name mit Nicht-ASCII-Zeichen

    for name, content_type in ((pdf, 'application/pdf'), (xml, 'application/xml')):
        resp = get(server_port, url(invoice_id, name))
        assert resp.status == 200 and resp.headers['Content-Type'] == content_type
        assert resp.body == read_file(os.path.join(versions_folder(invoice_id), name))
        assert unquote(resp.headers['X-Filename']) == name and resp.headers['X-Filename'].isascii()
        assert resp.headers['Content-Disposition'].startswith('attachment')
        assert int(resp.headers['Content-Length']) == len(resp.body)

    sha = {r['Kind']: r['Sha256'] for r in archived_versions(books.db, invoice_id)}
    assert hashlib.sha256(get(server_port, url(invoice_id, pdf)).body).hexdigest() == sha['pdf']


def test_the_download_writes_nothing(server_port, books):
    invoice_id = corrected_over_http(server_port, books)
    name = archived_names(invoice_id)[0]
    before_db, before_files = installation.dump(books.db.db_name), document_files(user_dir())
    for _ in range(3):
        assert get(server_port, url(invoice_id, name)).status == 200
        assert get(server_port, url(invoice_id, 'gibt-es-nicht.pdf')).status == 404
    assert installation.dump(books.db.db_name) == before_db and document_files(user_dir()) == before_files


def test_at_automatic_login_the_administrator_is_the_user_of_the_protocol(server_port, books):
    invoice_id = corrected_over_http(server_port, books)
    rows = protocol(books.db, invoice_id)
    assert {r['UserName'] for r in rows} == {installation.ADMIN}
    assert [(r['Action'], r['Field']) for r in rows] == [('status', 'Status'), ('correct', 'OrderNumber')]


# ── Alles andere ist 404 ─────────────────────────────────────────────────────

BAD_NAMES = ['../Fassungen/1/x.pdf', 'unter/ordner.pdf', '..', '.', '..\\x.pdf', '/etc/passwd',
             'C:\\Windows\\win.ini', 'x\x00.pdf', 'a\nb.pdf', '\t.pdf']


@pytest.mark.parametrize('name', BAD_NAMES)
def test_a_name_with_a_path_part_or_a_control_character_is_404(server_port, books, name):
    invoice_id = corrected_over_http(server_port, books)
    resp = get(server_port, f'/invoice/version_download?id={invoice_id}&file={quote(name, safe="")}')
    assert resp.status == 404 and resp.headers['Content-Type'].startswith('text/plain')
    assert resp.text == 'Diese Fassung gibt es nicht.'
    assert user_dir() not in resp.text


@pytest.mark.parametrize('raw', ['%2e%2e%2fbuch.db', '..%5cbuch.db', '%2Fetc%2Fpasswd', '%2e%2e', '..%2f..%2fbuch.db'])
def test_an_encoded_traversal_is_404_too(server_port, books, raw):
    invoice_id = corrected_over_http(server_port, books)
    resp = get(server_port, f'/invoice/version_download?id={invoice_id}&file={raw}')
    assert resp.status == 404 and resp.text == 'Diese Fassung gibt es nicht.'


@pytest.mark.parametrize('query', ['', '?id=1', '?file=x.pdf', '?id=abc&file=x.pdf', '?id=1.5&file=x.pdf',
                                   '?id=&file='])
def test_missing_or_invalid_parameters_are_404(server_port, books, query):
    corrected_over_http(server_port, books)
    resp = get(server_port, '/invoice/version_download' + query)
    assert resp.status == 404 and resp.text == 'Diese Fassung gibt es nicht.'


def test_the_current_document_is_not_a_version(server_port, books):
    invoice_id = corrected_over_http(server_port, books)
    current = [n for n in os.listdir(os.path.join(user_dir(), 'invoices', '2026')) if n.endswith('.pdf')]
    assert current                                                                # die neue Fassung liegt dort
    for name in current:
        assert get(server_port, url(invoice_id, name)).status == 404               # nur „Fassungen/<id>“ zählt
    assert get(server_port, url(invoice_id, 'buch.db')).status == 404


def test_another_invoices_version_is_not_reachable_with_this_id(server_port, books):
    first = corrected_over_http(server_port, books)
    second = corrected_over_http(server_port, books, invoiceNumber='26R101', customerId=books.cust2)
    name = archived_names(first)[0]
    assert get(server_port, url(first, name)).status == 200
    other = get(server_port, url(second, name))
    assert other.status == 404 and other.text == 'Diese Fassung gibt es nicht.'


def test_an_unknown_invoice_is_404_with_its_own_text(server_port, books):
    resp = get(server_port, url(99999, 'x.pdf'))
    assert resp.status == 404 and resp.text == 'Rechnung nicht gefunden.'


def test_the_id_of_a_quote_is_404_even_with_a_file_in_its_folder(server_port, books):
    quote_id = add_document(books.db, '26A001', 'quote', books.own, books.cust)
    write_file(os.path.join(versions_folder(quote_id), 'Angebot – versendet bis 2026-03-04.pdf'), b'%PDF-1.4 Angebot')
    resp = get(server_port, url(quote_id, 'Angebot – versendet bis 2026-03-04.pdf'))
    assert resp.status == 404 and resp.text == 'Rechnung nicht gefunden.'


def test_only_pdf_and_xml_files_are_delivered(server_port, books):
    invoice_id = corrected_over_http(server_port, books)
    write_file(os.path.join(versions_folder(invoice_id), 'notizen.txt'), b'privat')
    write_file(os.path.join(versions_folder(invoice_id), 'skript.html'), b'<script>alert(1)</script>')
    for name in ('notizen.txt', 'skript.html'):
        assert get(server_port, url(invoice_id, name)).status == 404
    os.makedirs(os.path.join(versions_folder(invoice_id), 'Unterordner.pdf'))       # ein Ordner mit Dateinamen
    assert get(server_port, url(invoice_id, 'Unterordner.pdf')).status == 404


def test_a_link_out_of_the_document_folder_is_not_followed(server_port, books, tmp_path):
    invoice_id = corrected_over_http(server_port, books)
    outside = tmp_path / 'fremd.pdf'
    outside.write_bytes(b'%PDF-1.4 nicht ausliefern')
    link = os.path.join(versions_folder(invoice_id), 'Link – versendet bis 2026-03-04.pdf')
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip('Symlinks lassen sich hier nicht anlegen')
    resp = get(server_port, url(invoice_id, os.path.basename(link)))
    assert resp.status == 404 and b'nicht ausliefern' not in resp.body


# ── Altbestand und andere Jahresordner ───────────────────────────────────────

def test_a_file_from_2_1_2_without_a_table_row_can_be_downloaded(server_port, books):
    invoice_id = corrected_over_http(server_port, books)
    name = '26R100 Malermeister Pinsel – versendet bis 2026-03-04.pdf'
    write_file(os.path.join(versions_folder(invoice_id), name), b'%PDF-1.4 aus 2.1.2')
    assert [r['RelPath'] for r in archived_versions(books.db, invoice_id)].count(
        f'2026/Fassungen/{invoice_id}/{name}') == 0                                  # keine Tabellenzeile dazu
    resp = get(server_port, url(invoice_id, name))
    assert resp.status == 200 and resp.body == b'%PDF-1.4 aus 2.1.2' and unquote(resp.headers['X-Filename']) == name


def test_the_folder_of_another_year_is_searched_too(server_port, books):
    """Das Rechnungsdatum kann sich im Entwurf ändern: Die Fassung liegt dann im Ordner eines anderen Jahres."""
    invoice_id = corrected_over_http(server_port, books)
    name = 'Älterer Stand – versendet bis 2025-12-30.xml'
    write_file(os.path.join(versions_folder(invoice_id, '2025'), name), b'<alt/>')
    resp = get(server_port, url(invoice_id, name))
    assert resp.status == 200 and resp.body == b'<alt/>' and resp.headers['Content-Type'] == 'application/xml'
    assert unquote(resp.headers['X-Filename']) == name


def test_a_stored_relative_path_is_never_the_way_to_a_file(server_port, books):
    """Die Tabelle nennt Pfade, die Auslieferung geht aber nur nach dem Namen im Ordner: Ein manipulierter
    ``RelPath`` (SQL-Konsole, zurückgespielte Sicherung) bringt keine fremde Datei ins Haus."""
    invoice_id = corrected_over_http(server_port, books)
    run_sql(books.db, "UPDATE InvoiceDocumentVersions SET RelPath = '../../../buch.db' WHERE InvoiceID = ?",
            invoice_id)
    assert get(server_port, url(invoice_id, 'buch.db')).status == 404
    assert get(server_port, url(invoice_id, '../../../buch.db')).status == 404
    name = archived_names(invoice_id)[0]
    assert get(server_port, url(invoice_id, name)).status == 200
