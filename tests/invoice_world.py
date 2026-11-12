# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Gemeinsame Hilfen der Tests zu Rechnungen: Sperre, Nummern, Löschen (2.1.2), Berichtigen (2.2.0).

Eine kleine Welt (zwei eigene Firmen, zwei Kunden, zwei Bankkonten), das Formular einer Rechnung mit
zwei Positionen und Hilfen, um Rechnungen in einen Status zu versetzen und den Stand der Tabellen zu
vergleichen. Alle Namen, Adressen, IBANs und Nummern sind erfundene Testwerte.
"""
import contextlib
import copy
import json
import sqlite3
from types import SimpleNamespace

import server.handlers as handlers
from tests.document_helpers import state

GROSS = 2981000        # Minor Units: 250,50 netto + 47,60 USt = 298,10


def build_world(db):
    """Zwei eigene Firmen, zwei Kunden, zwei Bankkonten – damit sich auch ein Wechsel prüfen lässt."""
    def contact(kind, name, **extra):
        return db.insert_contact(contact_type=kind, entity_type='company', display_name=name,
                                 company_name=name, email='info@example.com', **extra)
    own = contact('own', 'Eigene Firma GmbH', street='Hauptstr. 1', postal_code='10115',
                  city='Berlin', phone='030 1234567', tax_id='DE123456789')
    own2 = contact('own', 'Zweite Firma GmbH', street='Nebenweg 2', postal_code='80331', city='München')
    cust = contact('customer', 'Malermeister Pinsel', customer_number='K-100', street='Kundenweg 5',
                   postal_code='54321', city='Kundenhausen')
    cust2 = contact('customer', 'Bau AG', customer_number='K-200', street='Baustr. 9',
                    postal_code='20095', city='Hamburg')
    for name, iban in (('Giro', 'DE89370400440532013000'), ('Zweitkonto', 'DE02200505501015871393')):
        db.insert_account(name, 'Eigene Firma GmbH', iban, 'TESTDE1X', 'Testbank AG', is_cash=0,
                          skr_account=1800)
    accounts = {a[1]: a[0] for a in db.fetch_accounts()}
    return SimpleNamespace(db=db, own=own, own2=own2, cust=cust, cust2=cust2,
                           bank=accounts['Giro'], bank2=accounts['Zweitkonto'])


ITEMS = [
    {'position': 1, 'quantity': 2, 'unit': 'HUR', 'description': 'Beratung', 'unitPrice': 100.0,
     'taxRate': 19},
    {'position': 2, 'quantity': 1, 'unit': 'C62', 'description': 'Material', 'unitPrice': 50.5,
     'taxRate': 19},
]


def make_payload(w, **over):
    """Das Formular einer Rechnung mit zwei Positionen (Einheiten als UN/ECE-Code gespeichert)."""
    payload = {
        'invoiceNumber': '26R100', 'invoiceDate': '2026-06-07', 'deliveryDate': '2026-06-05',
        'customerId': w.cust, 'ownCompanyId': w.own, 'bankAccountId': w.bank, 'taxRate': 0.19,
        'dueDate': '2026-06-21', 'paymentTerms': 'Zahlbar in 14 Tagen', 'orderNumber': 'B-17',
        'buyerReference': '04011000-1234512345-35', 'discountPercentage': 2, 'discountDays': 7,
        'items': copy.deepcopy(ITEMS),
    }
    payload.update(over)
    return payload


def save_raw(payload):
    return handlers.handle_invoice_save(json.dumps(payload).encode())


def save(payload):
    return json.loads(save_raw(payload))


def force_status(db, invoice_id, status):
    """Status von Hand setzen (wie ein Bestand, ein Import oder die SQL-Konsole es hinterlässt)."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute('UPDATE Invoices SET Status = ? WHERE ID = ?', (status, invoice_id))
        con.commit()


def make_invoice(w, status='draft', number='26R100', **over):
    resp = save(make_payload(w, invoiceNumber=number, **over))
    assert resp['success'] is True, resp
    if status != 'draft':
        force_status(w.db, resp['invoice_id'], status)
    return resp['invoice_id']


def add_payment(w, invoice_id, amount=50.0):
    """Eine Zahlung zuordnen, ohne Buchungssätze (so ordnet der WISO-Import zu)."""
    booking = w.db.insert_booking('2026-07-01', amount, booking_type='bank', account_id=w.bank)
    w.db.link_invoice_to_transaction(invoice_id, booking, amount)
    return booking


def snapshot(db):
    """Alles, was eine Rechnungsoperation schreiben kann – samt Protokoll und abgelegten Fassungen."""
    return state(db, 'Invoices', 'InvoiceItems', 'InvoicePayments', 'Bookings', 'NumberRanges',
                 'InvoiceCorrections', 'InvoiceDocumentVersions')


def columns(db, invoice_id):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.row_factory = sqlite3.Row
        return dict(con.execute('SELECT * FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone())


OPEN_COLUMNS = ('PaymentTerms', 'OrderNumber', 'BuyerReference', 'BuyerRouteID', 'DeliveryDate',
                'BuyerName', 'BuyerCompany', 'BuyerStreet', 'BuyerPostalCode', 'BuyerCity', 'BuyerCountry')


# Was eine Berichtigung nebenbei schreibt: der Zeitstempel, die Nummer der Fassung und die Dokumentpfade
CORRECTION_COLUMNS = ('UpdatedAt', 'DocumentVersion', 'PDFPath', 'XMLPath')


def finance(db, invoice_id):
    """Alles, was bei einer versendeten Rechnung gesperrt ist: die Zeile ohne die änderbaren Felder (und ohne
    das, was eine Berichtigung nebenbei schreibt), dazu die Positionen."""
    kept = {k: v for k, v in columns(db, invoice_id).items()
            if k not in OPEN_COLUMNS and k not in CORRECTION_COLUMNS}
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        kept['items'] = con.execute('SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY ID',
                                    (invoice_id,)).fetchall()
    return kept


def set_invoice_status(invoice_id, status):
    code, body = handlers.handle_update_invoice_status(
        json.dumps({'invoice_id': invoice_id, 'status': status}).encode())
    return code, json.loads(body)




def document_version(db, invoice_id):
    """Die Nummer der aktuellen Fassung (NULL in der Datenbank gilt als 1)."""
    return columns(db, invoice_id)['DocumentVersion'] or 1


def save_open(w, invoice_id, version=None, **fields):
    """Eine versendete Rechnung berichtigen, wie die Maske es tut: nur die genannten Felder, dazu die Fassung,
    die sie geladen hat (``version``; Vorgabe: die aktuelle). Liefert die Antwort als Dict."""
    return save({'invoiceId': invoice_id,
                 'documentVersion': document_version(w.db, invoice_id) if version is None else version,
                 **fields})


def query_rows(db, sql, *args):
    """Zeilen einer Abfrage als Dicts (Spaltenname -> Wert)."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, args)]


def run_sql(db, statement, *args):
    """Eine Anweisung an der App vorbei ausführen (wie die SQL-Konsole oder eine zurückgespielte Sicherung)."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute(statement, args)
        con.commit()


def protocol(db, invoice_id, action=None):
    """Das Protokoll der Rechnung (``InvoiceCorrections``), älteste Zeile zuerst; mit ``action`` nur diese Art."""
    where, args = ('AND Action = ?', (action,)) if action else ('', ())
    return query_rows(db, f'SELECT * FROM InvoiceCorrections WHERE InvoiceID = ? {where} ORDER BY ID',
                      invoice_id, *args)


def archived_versions(db, invoice_id):
    """Die abgelegten Fassungen der Rechnung (``InvoiceDocumentVersions``), ältere zuerst."""
    return query_rows(db, 'SELECT * FROM InvoiceDocumentVersions WHERE InvoiceID = ? ORDER BY Version, Kind',
                      invoice_id)


def booking_count(db):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute('SELECT COUNT(*) FROM Bookings').fetchone()[0]
