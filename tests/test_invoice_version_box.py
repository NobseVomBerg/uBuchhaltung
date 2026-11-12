# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Der Kasten „Fassungen und Protokoll“ auf der Rechnungsseite (2.2.0).

* Er erscheint nur, wenn es etwas gibt (abgelegte Fassungen, Dateien in „Fassungen/<ID>“ oder Protokoll).
* Tabelle der Fassungen: Fassung (bei Dateien aus 2.1.2 ohne Tabellenzeile: „–“), „Versendet bis“, Anlass
  (berichtigt/zurückgeholt), Download je Datei (``appDownload``, Dateiname prozentkodiert).
* Protokoll: Datum und Uhrzeit (Ortszeit), Benutzer, Feld, alt → neu – Berichtigungen wie Statuswechsel (mit den
  Bezeichnungen der Oberfläche).
* Jeder Wert aus der Datenbank ist maskiert; Daten kommen nie per ``innerHTML`` in die Seite.

Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import datetime
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import pytest

import server.handlers as handlers
import userctx
from server.pages_invoice import PageInvoice
from tests.document_helpers import table_rows, write_file
from tests.dom_helpers import Dom
from tests.invoice_world import (archived_versions, build_world, columns, make_invoice, run_sql, save_open,
                                 set_invoice_status)

TODAY = datetime.date.today()
TODAY_DE = TODAY.strftime('%d.%m.%Y')


@pytest.fixture
def world(tmp_db, monkeypatch):
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    userctx.set_user('anna')
    return build_world(tmp_db)


def sent_invoice(w, number='26R100', **over):
    invoice_id = make_invoice(w, 'draft', number=number, **over)
    code, body = set_invoice_status(invoice_id, 'sent')
    assert code == 200 and 'warning' not in body, body
    return invoice_id


def box(w, invoice_id):
    """Der Kasten als HTML-Abschnitt (leer, wenn die Seite keinen hat)."""
    html = PageInvoice(w.db, {}, invoice_id)
    if 'id="invoice_versions"' not in html:
        return ''
    start = html.index('id="invoice_versions"')
    return html[start:html.index('</div>', start)]


def rows_of(section, title):
    """Die Zeilen der Tabelle unter der Überschrift ``title`` (ohne Kopfzeile), als Listen von Zellentexten."""
    part = section[section.index(f'<h4>{title}</h4>'):]
    part = part[:part.index('</table>')]
    return table_rows(part)[1:]


def downloads(section):
    """Was die Download-Links aufrufen: ``[(Rechnungs-ID, Dateiname)]``. Die Adresse steht als JavaScript-Zeichenkette
    im ``onclick`` (``&`` darin als ``\\u0026``); sie wird gelesen, wie der Browser sie läse."""
    found = []
    for a in Dom(section).all('a'):
        call = a.get('onclick', '')
        if not call.startswith('appDownload('):
            continue
        assert call.endswith(')') and a['href'] == 'javascript:void(0);'
        address = json.loads(call[len('appDownload('):-1])
        assert address.isascii() and "'" not in address and ' ' not in address       # alles kodiert
        query = parse_qs(urlsplit(address).query)
        assert urlsplit(address).path == '/invoice/version_download'
        found.append((int(query['id'][0]), query['file'][0]))
    return found


# ── Wann es den Kasten gibt ──────────────────────────────────────────────────

def test_a_new_draft_without_a_history_has_no_box(world):
    assert box(world, make_invoice(world, 'draft')) == ''


def test_an_invoice_that_was_sent_has_a_box_with_the_status_protocol(world):
    invoice_id = sent_invoice(world)
    section = box(world, invoice_id)
    assert 'Fassungen und Protokoll' in section and 'Abgelegte Fassungen' not in section   # noch nichts abgelegt
    rows = rows_of(section, 'Protokoll')
    assert len(rows) == 1
    assert rows[0][1:] == ['anna', 'Status', 'Entwurf → Versendet']
    assert datetime.datetime.strptime(rows[0][0], '%d.%m.%Y %H:%M')                        # Datum und Uhrzeit


def test_the_page_of_a_new_invoice_form_and_of_a_copy_has_no_box(world):
    invoice_id = sent_invoice(world)
    assert 'id="invoice_versions"' not in PageInvoice(world.db, {}, None)
    assert 'id="invoice_versions"' not in PageInvoice(world.db, {}, None, copy_from=invoice_id)


# ── Fassungen ────────────────────────────────────────────────────────────────

def test_a_correction_shows_the_archived_version_with_downloads_and_the_protocol(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, deliveryDate='2026-06-01')
    section = box(world, invoice_id)

    versions = rows_of(section, 'Abgelegte Fassungen')
    assert versions == [['1', TODAY_DE, 'berichtigt', 'PDF XML']]
    stored = sorted(os.listdir(os.path.join(userctx.DATA_ROOT, 'invoices', '2026', 'Fassungen', str(invoice_id))))
    assert sorted(downloads(section)) == [(invoice_id, name) for name in stored]    # genau die abgelegten Dateien

    protocol_rows = rows_of(section, 'Protokoll')
    assert [r[1:] for r in protocol_rows] == [['anna', 'Status', 'Entwurf → Versendet'],
                                              ['anna', 'Leistungsdatum', '2026-06-05 → 2026-06-01']]


def test_taking_back_adds_a_row_with_the_reason_taken_back(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, orderNumber='B-2')
    set_invoice_status(invoice_id, 'draft')
    versions = rows_of(box(world, invoice_id), 'Abgelegte Fassungen')
    assert [(r[0], r[2], r[3]) for r in versions] == [('1', 'berichtigt', 'PDF XML'),
                                                       ('2', 'zurückgeholt', 'PDF XML')]
    protocol_rows = rows_of(box(world, invoice_id), 'Protokoll')
    assert [r[2:] for r in protocol_rows] == [['Status', 'Entwurf → Versendet'],
                                              ['Bestellnummer', 'B-17 → B-2'],
                                              ['Status', 'Versendet → Entwurf']]


def test_a_file_from_2_1_2_without_a_table_row_appears_without_a_version_number(world):
    invoice_id = make_invoice(world, 'draft')
    folder = os.path.join(userctx.DATA_ROOT, 'invoices', '2026', 'Fassungen', str(invoice_id))
    write_file(os.path.join(folder, '26R100 Malermeister Pinsel – versendet bis 2026-03-04.pdf'))
    write_file(os.path.join(folder, '26R100 Malermeister Pinsel – versendet bis 2026-03-04.xml'))
    write_file(os.path.join(folder, 'ohne Datum.pdf'))
    write_file(os.path.join(folder, 'notizen.txt'))                                         # kein Dokument: nicht gezeigt
    section = box(world, invoice_id)

    versions = rows_of(section, 'Abgelegte Fassungen')
    assert sorted(versions) == sorted([['–', '04.03.2026', '–', 'PDF XML'], ['–', '–', '–', 'PDF']])
    assert 'notizen' not in section
    assert '<h4>Protokoll</h4>' not in section                                               # kein Protokoll vorhanden


def test_a_registered_version_whose_file_is_gone_shows_no_link(world):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, orderNumber='B-2')
    folder = os.path.join(userctx.DATA_ROOT, 'invoices', '2026', 'Fassungen', str(invoice_id))
    xml = next(n for n in os.listdir(folder) if n.endswith('.xml'))
    os.remove(os.path.join(folder, xml))

    section = box(world, invoice_id)

    assert rows_of(section, 'Abgelegte Fassungen') == [['1', TODAY_DE, 'berichtigt', 'PDF XML']]
    assert [name.rsplit('.', 1)[1] for _id, name in downloads(section)] == ['pdf']          # nur die PDF ist ladbar
    assert 'Datei nicht mehr vorhanden' in section


def test_the_download_address_is_percent_encoded_for_names_with_quotes_and_ampersands(world):
    world.db.update_contact(world.cust, contact_type='customer', entity_type='company',
                            display_name="O'Brien & Söhne", company_name="O'Brien & Söhne", customer_number='K-100',
                            street='Kundenweg 5', postal_code='54321', city='Kundenhausen',
                            email='info@example.com')
    invoice_id = sent_invoice(world, number='26R100')
    save_open(world, invoice_id, orderNumber='B-2')
    section = box(world, invoice_id)

    names = [name for _id, name in downloads(section)]
    assert len(names) == 2
    assert all(n.startswith("26R100 O'Brien & Söhne – versendet bis ") for n in names)
    assert "O'Brien" not in section and "& Söhne" not in section                         # im Markup nur kodiert


# ── Das Protokoll ────────────────────────────────────────────────────────────

def test_values_from_the_database_are_escaped_in_the_box(world):
    evil = '<script>alert(1)</script>'
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, paymentTerms=evil)
    run_sql(world.db, "UPDATE InvoiceCorrections SET UserName = ? WHERE Action = 'correct'", '<img src=x onerror=alert(2)>')
    run_sql(world.db, "INSERT INTO InvoiceCorrections (InvoiceID, Version, Action, Field, OldValue, NewValue) "
                      "VALUES (?, 2, 'correct', ?, ?, ?)", invoice_id, '<b>Feld</b>', '"a" & b', "'c' < d")
    run_sql(world.db, "UPDATE InvoiceDocumentVersions SET Reason = '<i>x</i>' WHERE Kind = 'pdf'")

    html = PageInvoice(world.db, {}, invoice_id)
    section = html[html.index('id="invoice_versions"'):]

    assert evil not in html and '<img src=x onerror=alert(2)>' not in html and '<b>Feld</b>' not in section
    assert '<i>x</i>' not in section
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in section and '&lt;b&gt;Feld&lt;/b&gt;' in section
    assert '&quot;a&quot; &amp; b' in section and '&lt;i&gt;x&lt;/i&gt;' in section
    dom = Dom(html)
    assert dom.names.count('img') == Dom(PageInvoice(world.db, {}, None)).names.count('img')     # kein Element hinzugekommen


def test_the_protocol_shows_unknown_fields_and_empty_values_in_plain_words(world):
    invoice_id = sent_invoice(world)
    run_sql(world.db, "INSERT INTO InvoiceCorrections (InvoiceID, Version, Action, Field, OldValue, NewValue) "
                      "VALUES (?, 2, 'correct', 'EtwasNeues', NULL, '')", invoice_id)
    run_sql(world.db, "INSERT INTO InvoiceCorrections (InvoiceID, Version, Action, Field, OldValue, NewValue) "
                      "VALUES (?, 2, 'status', 'Status', 'unbekannt', 'partial_payment')", invoice_id)
    rows = rows_of(box(world, invoice_id), 'Protokoll')
    assert rows[-2][2:] == ['EtwasNeues', '– → –'] and rows[-2][1] == '–'                  # kein Benutzer, nichts gesetzt
    assert rows[-1][2:] == ['Status', 'unbekannt → Teilzahlung']


@pytest.mark.skipif(not hasattr(time, 'tzset'), reason='Zeitzone lässt sich nur unter Unix umstellen')
def test_times_are_shown_in_local_time(world, monkeypatch):
    invoice_id = sent_invoice(world)
    save_open(world, invoice_id, orderNumber='B-2')
    run_sql(world.db, "UPDATE InvoiceCorrections SET ChangedAt = '2026-11-12 23:30:00'")
    run_sql(world.db, "UPDATE InvoiceDocumentVersions SET ArchivedAt = '2026-11-12 23:30:00'")
    try:
        with monkeypatch.context() as zone:
            zone.setenv('TZ', 'Europe/Berlin')
            time.tzset()
            section = box(world, invoice_id)
            assert [r[0] for r in rows_of(section, 'Protokoll')] == ['13.11.2026 00:30'] * 2
            assert rows_of(section, 'Abgelegte Fassungen')[0][1] == '13.11.2026'
    finally:
        time.tzset()


def test_the_box_survives_a_deleted_draft_page_for_an_unknown_id(world):
    html = PageInvoice(world.db, {}, 99999)                                 # unbekannte Rechnung: das leere Formular
    assert 'id="invoice_versions"' not in html
    assert columns(world.db, make_invoice(world, 'draft'))['DocumentVersion'] is None
    assert archived_versions(world.db, 99999) == []
