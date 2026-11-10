# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Rechnung speichern: Fehler im Klartext und alles-oder-nichts.

SQLite meldet nur „FOREIGN KEY constraint failed“. Die Oberfläche soll sagen,
welcher Verweis ins Leere zeigt – und ein gescheiterter Speicherversuch darf
nichts halb Gespeichertes zurücklassen.
"""
import json
import sqlite3

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
