# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Rechnung und Angebot speichern: Fehler im Klartext und alles-oder-nichts.

SQLite meldet nur „FOREIGN KEY constraint failed“. Die Oberfläche soll sagen,
welcher Verweis ins Leere zeigt – und ein gescheiterter Speicherversuch darf
nichts halb Gespeichertes zurücklassen. Seit 2.0.7 gilt das auch für Angebote (Kopf und
Positionen in EINER Transaktion); die Meldungen nennen den Grund („Diese Nummer ist bereits
vergeben.“, „Angebot nicht gefunden.“), nie SQL- oder Python-Texte. Seit 2.1.2 vergibt das erste
Speichern – auch als Entwurf – die Rechnungsnummer, in derselben Transaktion: Scheitert das
Speichern, ist die Nummer nicht verbraucht (die Nummernkreise prüft test_invoice_numbering.py).
"""
import json
import sqlite3

import pytest

import server.handlers as handlers
from db.core import ForeignKeyError, explain_fk_violation, fk_explained
from server.handlers import handle_invoice_save
from server.pages_invoice import PageInvoice
from tests.test_invoice_recompute import _make_contacts


def _payload(own_id, cust_id, **over):
    p = {
        'invoiceNumber': '26R200', 'invoiceDate': '2026-06-07',
        'deliveryDate': '2026-06-05',
        'customerId': cust_id, 'ownCompanyId': own_id, 'taxRate': 0.19,
        'status': 'draft',
        'items': [{'position': 1, 'quantity': 1, 'unit': 'C62',
                   'description': 'Beratung', 'unitPrice': 100.0, 'taxRate': 19}],
    }
    p.update(over)
    return p


def _save(payload):
    return json.loads(handle_invoice_save(json.dumps(payload).encode()))


def _count(db, sql, *args):
    con = sqlite3.connect(db.db_name)
    try:
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


def test_unknown_bank_account_is_named_and_nothing_saved(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    resp = _save(_payload(own, cust, bankAccountId=999))
    assert resp['success'] is False
    assert 'Bankkonto mit ID 999 gibt es nicht' in resp['error']
    assert 'FOREIGN KEY' not in resp['error']
    assert 'nichts gespeichert' in resp['error']
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices') == 0


def test_failing_item_leaves_no_invoice_header(tmp_db, monkeypatch):
    """Scheitert eine Position, darf der Kopf nicht stehen bleiben – sonst
    scheitert der nächste Versuch an der schon vergebenen Rechnungs-Nr."""
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    bad = _payload(own, cust)
    bad['items'].append({'position': 2, 'quantity': 1, 'unit': 'C62',
                         'description': None, 'unitPrice': 5.0, 'taxRate': 19})
    resp = _save(bad)
    assert resp['success'] is False and 'nichts gespeichert' in resp['error']
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices') == 0
    assert _count(tmp_db, 'SELECT COUNT(*) FROM InvoiceItems') == 0
    # derselbe Versuch, jetzt korrekt, klappt mit derselben Nummer
    assert _save(_payload(own, cust))['success'] is True


def test_failed_update_keeps_existing_items(tmp_db, monkeypatch):
    """Bisher wurden die Positionen vor dem UPDATE gelöscht und committet –
    scheiterte das UPDATE, stand die Rechnung ohne Positionen da."""
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    iid = _save(_payload(own, cust))['invoice_id']
    resp = _save(_payload(own, cust, invoiceId=iid, bankAccountId=999))
    assert resp['success'] is False
    assert 'Bankkonto mit ID 999' in resp['error']
    assert len(tmp_db.get_invoice_items(iid)) == 1


def test_replay_log_only_after_commit(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    logged = []
    monkeypatch.setattr(tmp_db, '_log_sql', lambda sql, params, desc: logged.append(desc))
    _save(_payload(own, cust, bankAccountId=999))
    assert logged == []                       # zurückgerollt ⇒ nichts im Replay-Log
    assert _save(_payload(own, cust))['success'] is True
    assert logged == ['Insert invoice', 'Insert invoice item']


def test_explain_names_each_dangling_reference(tmp_db):
    own, cust = _make_contacts(tmp_db)
    con = tmp_db._get_connection()
    try:
        text = explain_fk_violation(con, 'Invoices', {
            'OwnCompanyId': own, 'CustomerId': 4711, 'BankAccountId': 815})
    finally:
        con.close()
    assert 'Kunde mit ID 4711 gibt es nicht (Tabelle Contacts)' in text
    assert 'Bankkonto mit ID 815 gibt es nicht (Tabelle Accounts)' in text
    assert 'Eigene Firma' not in text            # vorhanden ⇒ nicht gemeldet


def test_explain_reports_when_all_references_exist(tmp_db):
    """Meldet SQLite trotzdem einen Verstoß, sagt der Text das ausdrücklich –
    dann stimmt etwas mit der Datenbank selbst nicht."""
    own, cust = _make_contacts(tmp_db)
    con = tmp_db._get_connection()
    try:
        text = explain_fk_violation(con, 'Invoices', {'OwnCompanyId': own,
                                                      'CustomerId': cust})
    finally:
        con.close()
    assert 'alle Verweise vorhanden' in text
    assert f'Kunde {cust} → Contacts' in text


def test_fk_explained_leaves_other_integrity_errors_alone(tmp_db):
    con = tmp_db._get_connection()
    try:
        try:
            with fk_explained(con, 'Invoices', {}):
                raise sqlite3.IntegrityError('UNIQUE constraint failed: Invoices.InvoiceNumber')
        except sqlite3.IntegrityError as exc:
            assert not isinstance(exc, ForeignKeyError)
            assert 'UNIQUE' in str(exc)
    finally:
        con.close()


def test_form_puts_delivery_date_right_below_invoice_date(db_with_coa):
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    meta = html.split('class="invoice-meta-table"')[1].split('</table>')[0]
    assert '>Rechnungsdatum:<' in meta and '>Datum:<' not in meta
    order = [meta.index(f'id="{i}"') for i in
             ('invoice_date', 'delivery_date', 'invoice_number', 'customer_number')]
    assert order == sorted(order)


def test_form_loads_logo_for_new_invoices_too(db_with_coa):
    """Die Vorschau holte Logo und Absender nur im Bearbeiten-Modus."""
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    assert 'if (isEditMode)' not in html
    assert 'updateOwnCompany();' in html


def test_a_failing_replay_log_does_not_report_a_saved_invoice_as_failed(tmp_db, monkeypatch, capsys):
    """Das Replay-Log wird nach dem Commit geschrieben. Scheitert das (Platte voll,
    Rechte), ist die Rechnung trotzdem gespeichert – die Antwort darf nicht
    „nichts gespeichert“ lauten, sonst legt der Nutzer sie ein zweites Mal an."""
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)

    def full_disk(sql, params, desc):
        raise OSError(28, 'Auf dem Gerät ist kein Speicherplatz mehr verfügbar')
    monkeypatch.setattr(tmp_db, '_log_sql', full_disk)

    resp = _save(_payload(own, cust))

    assert resp['success'] is True
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices') == 1
    assert _count(tmp_db, 'SELECT COUNT(*) FROM InvoiceItems') == 1
    assert 'Replay-Log' in capsys.readouterr().out          # der Fehler steht im Server-Log


# ── Angebote: dieselbe Transaktion und dieselben Klartext-Meldungen (2.0.7) ──
def _quote_payload(own_id, cust_id, **over):
    p = {
        'quoteNumber': '26A200', 'quoteDate': '2026-06-07', 'customerId': cust_id, 'ownCompanyId': own_id,
        'validUntil': '2026-07-07', 'taxRate': 0.19, 'status': 'draft',
        'introText': '<b>Hallo</b>', 'closingText': 'Gruß',
        'items': [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung',
                   'unitPrice': 100.0, 'taxRate': 19}],
    }
    p.update(over)
    return p


def _save_quote(payload):
    return json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))


def _quote_state(db, quote_id):
    """Kopf (Nummer, Status, Gültigkeit, Texte, Summen) und Positionen des Angebots, roh."""
    con = sqlite3.connect(db.db_name)
    try:
        head = con.execute('SELECT InvoiceNumber, Status, ValidUntil, IntroText, ClosingText, SumNet, SumGross '
                           'FROM Invoices WHERE ID = ?', (quote_id,)).fetchone()
        items = con.execute('SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', (quote_id,)).fetchall()
    finally:
        con.close()
    return head, items


MISSING_QUANTITY = {'position': 2, 'description': 'Zweite Position', 'unitPrice': 5.0, 'taxRate': 19}
QUANTITY_MESSAGE = 'Speichern fehlgeschlagen: In einer Position fehlt die Menge – es wurde nichts gespeichert.'


def test_failing_quote_item_leaves_no_quote_header(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    bad = _quote_payload(own, cust)
    bad['items'].append(dict(MISSING_QUANTITY))
    resp = _save_quote(bad)
    assert resp == {'success': False, 'error': QUANTITY_MESSAGE}
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices') == 0
    assert _count(tmp_db, 'SELECT COUNT(*) FROM InvoiceItems') == 0
    # derselbe Versuch, jetzt korrekt, klappt mit derselben Nummer
    assert _save_quote(_quote_payload(own, cust))['success'] is True


def test_failed_quote_update_keeps_header_and_old_items(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    quote_id = _save_quote(_quote_payload(own, cust))['quote_id']
    before = _quote_state(tmp_db, quote_id)
    bad = _quote_payload(own, cust, quoteId=quote_id, quoteNumber='26A999', introText='neu', status='sent',
                         validUntil='2030-01-01')
    bad['items'] = [{'position': 1, 'quantity': 4, 'description': 'Geändert', 'unitPrice': 9.0, 'taxRate': 19},
                    dict(MISSING_QUANTITY)]
    assert _save_quote(bad) == {'success': False, 'error': QUANTITY_MESSAGE}
    assert _quote_state(tmp_db, quote_id) == before               # Kopf und die alte Position, unverändert
    assert len(before[1]) == 1


def test_failed_quote_update_through_a_bad_reference_keeps_everything(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    quote_id = _save_quote(_quote_payload(own, cust))['quote_id']
    before = _quote_state(tmp_db, quote_id)
    resp = _save_quote(_quote_payload(own, cust, quoteId=quote_id, quoteNumber='26A998', ownCompanyId=9999))
    assert resp['success'] is False and resp['error'] == 'Eigene Firma nicht gefunden'
    assert _quote_state(tmp_db, quote_id) == before


@pytest.mark.parametrize('save, payload_for, number_field', [
    pytest.param(_save, lambda own, cust, number: _payload(own, cust, invoiceNumber=number),
                 'invoiceNumber', id='Rechnung'),
    pytest.param(_save_quote, lambda own, cust, number: _quote_payload(own, cust, quoteNumber=number),
                 'quoteNumber', id='Angebot'),
])
def test_duplicate_number_is_reported_in_plain_words(tmp_db, monkeypatch, save, payload_for, number_field):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    assert save(payload_for(own, cust, '26X500'))['success'] is True
    again = save(payload_for(own, cust, '26X500'))
    assert again == {'success': False, 'error': 'Diese Nummer ist bereits vergeben.'}
    assert 'UNIQUE' not in again['error'] and 'Invoices' not in again['error']
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices WHERE InvoiceNumber = ?', '26X500') == 1


def test_a_quote_cannot_take_the_number_of_an_invoice_and_vice_versa(tmp_db, monkeypatch):
    # Rechnungen und Angebote teilen sich die Tabelle und damit die eindeutige Nummer
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    assert _save(_payload(own, cust, invoiceNumber='26X600'))['success'] is True
    assert _save_quote(_quote_payload(own, cust, quoteNumber='26X600'))['error'] == 'Diese Nummer ist bereits vergeben.'
    assert _save_quote(_quote_payload(own, cust, quoteNumber='26X601'))['success'] is True
    assert _save(_payload(own, cust, invoiceNumber='26X601'))['error'] == 'Diese Nummer ist bereits vergeben.'


def test_renumbering_a_quote_to_a_taken_number_keeps_the_quote(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    first = _save_quote(_quote_payload(own, cust, quoteNumber='26A001'))['quote_id']
    second = _save_quote(_quote_payload(own, cust, quoteNumber='26A002'))['quote_id']
    before = _quote_state(tmp_db, second)
    resp = _save_quote(_quote_payload(own, cust, quoteId=second, quoteNumber='26A001', introText='neu'))
    assert resp == {'success': False, 'error': 'Diese Nummer ist bereits vergeben.'}
    assert _quote_state(tmp_db, second) == before and first != second


@pytest.mark.parametrize('save, payload_for, key, expected', [
    pytest.param(_save, lambda o, c, i: _payload(o, c, invoiceId=i), 'invoice', 'Rechnung nicht gefunden.',
                 id='Rechnung-unbekannt'),
    pytest.param(_save_quote, lambda o, c, i: _quote_payload(o, c, quoteId=i), 'quote', 'Angebot nicht gefunden.',
                 id='Angebot-unbekannt'),
])
def test_updating_an_unknown_document_says_so(tmp_db, monkeypatch, save, payload_for, key, expected):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    assert save(payload_for(own, cust, 99999)) == {'success': False, 'error': expected}
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices') == 0


def test_updating_the_other_kind_says_not_found_and_changes_nothing(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    invoice_id = _save(_payload(own, cust, invoiceNumber='26R300'))['invoice_id']
    quote_id = _save_quote(_quote_payload(own, cust, quoteNumber='26A300'))['quote_id']
    invoice_before, quote_before = _quote_state(tmp_db, invoice_id), _quote_state(tmp_db, quote_id)
    assert _save_quote(_quote_payload(own, cust, quoteId=invoice_id, quoteNumber='26A301'))['error'] == \
        'Angebot nicht gefunden.'
    assert _save(_payload(own, cust, invoiceId=quote_id, invoiceNumber='26R301'))['error'] == \
        'Rechnung nicht gefunden.'
    assert _quote_state(tmp_db, invoice_id) == invoice_before and _quote_state(tmp_db, quote_id) == quote_before


@pytest.mark.parametrize('mutate, reason', [
    pytest.param(lambda item: item.update(quantity='abc'), 'Beträge, Mengen oder Positionen sind ungültig',
                 id='Menge-ist-Text'),
    pytest.param(lambda item: item.update(unitPrice='viel'), 'Beträge, Mengen oder Positionen sind ungültig',
                 id='Preis-ist-Text'),
    pytest.param(lambda item: item.update(description=None), 'In einer Position fehlt die Bezeichnung',
                 id='ohne-Bezeichnung'),
])
def test_save_errors_name_the_reason_without_sql_or_python_details(tmp_db, monkeypatch, capsys, mutate, reason):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    for save, payload_for in ((_save, _payload), (_save_quote, _quote_payload)):
        payload = payload_for(own, cust)
        mutate(payload['items'][0])
        resp = save(payload)
        assert resp == {'success': False, 'error': f'Speichern fehlgeschlagen: {reason} – es wurde nichts gespeichert.'}
        for leak in ('sqlite3', 'IntegrityError', 'NOT NULL', 'InvoiceItems', 'Decimal', 'ValueError', 'Traceback'):
            assert leak not in resp['error'], leak
    assert 'Traceback' in capsys.readouterr().err                   # die Einzelheiten stehen im Server-Log
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices') == 0


def test_unexpected_exception_is_reported_as_unexpected_without_its_text(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)

    def boom(*args, **kwargs):
        raise RuntimeError('Interna: /home/niemand/geheim.db')
    monkeypatch.setattr(tmp_db, 'insert_invoice_item', boom)
    for save, payload in ((_save, _payload(own, cust)), (_save_quote, _quote_payload(own, cust))):
        resp = save(payload)
        assert resp == {'success': False, 'error': 'Speichern fehlgeschlagen: Unerwarteter Fehler '
                                                    '(Einzelheiten im Server-Log) – es wurde nichts gespeichert.'}
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices') == 0


# ── Nummernkreis: das erste Speichern verbraucht die Nummer (2.1.2) ───────────
def _counter(db, letter='R'):
    return [r[5] for r in db.fetch_number_ranges('invoice') if r[3] == letter][0]


def test_saving_a_new_invoice_with_its_own_number_lifts_its_number_range(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    tmp_db.insert_number_range('invoice', 2026, 'B', current_number=40)         # andere Kreise bleiben
    resp = _save(_payload(own, cust, invoiceNumber='26R007', status='finalized'))
    assert resp['success'] is True and 'warning' not in resp                    # der Normalfall: kein Hinweis
    assert _counter(tmp_db, 'R') == 7 and _counter(tmp_db, 'B') == 40


@pytest.mark.parametrize('payload, stored_status', [
    pytest.param({'status': 'draft'}, 'draft', id='Entwurf'),
    pytest.param({'status': 'finalized'}, 'finalized', id='abgeschlossen'),
    pytest.param({'status': 'sent'}, 'draft', id='versendet-nur-ueber-den-Status'),
    pytest.param({'status': 'paid'}, 'draft', id='bezahlt-nur-ueber-den-Status'),
])
def test_the_first_save_uses_the_number_whatever_the_status(tmp_db, monkeypatch, payload, stored_status):
    """Auch ein Entwurf verbraucht die Nummer (sonst bekäme die zweite neue Rechnung dieselbe
    Nummer vorgeschlagen). Der Status kommt nie vom Client: nur Entwurf oder abgeschlossen."""
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    resp = _save(_payload(own, cust, invoiceNumber='26R007', **payload))
    assert resp['success'] is True
    assert _counter(tmp_db) == 7
    assert tmp_db.get_invoice_by_id(resp['invoice_id'])[40] == stored_status


def test_updating_a_finalized_invoice_does_not_touch_the_number_range_again(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    invoice_id = _save(_payload(own, cust, invoiceNumber='26R007', status='finalized'))['invoice_id']
    tmp_db.update_number_range(tmp_db.fetch_number_ranges('invoice')[0][0], 2026, 'R', '', 20, '', None)
    resp = _save(_payload(own, cust, invoiceNumber='26R007', status='finalized', invoiceId=invoice_id))
    assert resp['success'] is True and 'warning' not in resp
    assert _counter(tmp_db) == 20


def test_without_a_matching_number_range_nothing_is_lifted_and_nothing_is_reported(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    resp = _save(_payload(own, cust, invoiceNumber='26X007', status='finalized'))
    assert resp['success'] is True and 'warning' not in resp and _counter(tmp_db) == 5


@pytest.mark.parametrize('number', ['RE-2026-17', '26R01a', 'Rechnung', 'X'])
def test_a_number_outside_the_scheme_is_saved_as_it_is_and_moves_no_counter(tmp_db, monkeypatch, number):
    """Seit 2.1.2 gibt es keinen Hinweis „Nummernkreis nicht fortgeschrieben“ mehr: Der Zähler gehört
    zur Transaktion des Speicherns, eine eigene Nummer außerhalb der Formatvorlage lässt ihn stehen."""
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    resp = _save(_payload(own, cust, invoiceNumber=number, status='finalized'))
    assert resp['success'] is True and resp['invoice_id'] and 'warning' not in resp
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices WHERE InvoiceNumber = ?', number) == 1
    assert _counter(tmp_db) == 5


def test_a_failing_number_range_saves_nothing(tmp_db, monkeypatch):
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(tmp_db, 'allocate_invoice_number', locked)
    resp = _save(_payload(own, cust, invoiceNumber='26R007', status='finalized'))
    assert resp == {'success': False, 'error': 'Speichern fehlgeschlagen: Die Datenbank hat den Vorgang '
                                               'abgelehnt – es wurde nichts gespeichert.'}
    assert 'locked' not in json.dumps(resp)
    assert _count(tmp_db, 'SELECT COUNT(*) FROM Invoices') == 0 and _counter(tmp_db) == 5


def test_a_failed_save_does_not_use_the_number(tmp_db, monkeypatch):
    """Der Zähler rückt in der Transaktion des Speicherns vor: scheitert eine Position, ist die
    Nummer nicht verbraucht, und derselbe Versuch bekommt sie wieder."""
    own, cust = _make_contacts(tmp_db)
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    tmp_db.insert_number_range('invoice', 2026, 'R', current_number=5)
    bad = _payload(own, cust, invoiceNumber='26R006')
    bad['items'].append({'position': 2, 'quantity': 1, 'unit': 'C62', 'description': None,
                         'unitPrice': 5.0, 'taxRate': 19})
    assert _save(bad)['success'] is False
    assert _counter(tmp_db) == 5
    assert _save(_payload(own, cust, invoiceNumber='26R006'))['success'] is True
    assert _counter(tmp_db) == 6


def test_invoice_form_shows_the_warning_before_it_leaves_the_page(db_with_coa):
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    assert "if (data.warning)" in html and "showMessage(data.warning, 'warn')" in html
