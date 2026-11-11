# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen für die Tests rund um Rechnungen und Angebote.

Beide teilen sich die Tabelle ``Invoices`` (Spalte ``DocumentType``), ihre Positionen liegen in
``InvoiceItems``. Die Hilfen legen Belege über die Datenbank-Methoden der App an, vergleichen den
Stand der Tabellen vorher/nachher (roh, so wie gespeichert) und schicken JSON-Anfragen an den
echten Server (Fixture ``server_port``). Alle Namen, Adressen und Nummern sind erfundene Testwerte.
"""
import contextlib
import datetime
import json
import os
import sqlite3
from types import SimpleNamespace

import userctx
from tests import installation
from tests.http_client import send

INVOICE_NUMBER = '26R001'
QUOTE_NUMBER = '26A001'
CUSTOMER = 'Malermeister Pinsel'
YEAR = str(datetime.date.today().year)          # Ordnername unter quotes/ (Jahr der Erzeugung)
PDF = b'%PDF-1.4 abgelegte Fassung'
PDF_PATH = 41        # Spalte Invoices.PDFPath
XML_PATH = 42        # Spalte Invoices.XMLPath
STATUS = 40          # Spalte Invoices.Status


def add_contacts(db):
    """Eigene Firma und ein Kunde; liefert ``(own_id, customer_id)``."""
    own = db.insert_contact(contact_type='own', entity_type='company',
                            display_name='Eigene Firma', company_name='Eigene Firma GmbH',
                            street='Hauptstr. 1', postal_code='10115', city='Berlin',
                            email='info@example.com', phone='030 1234567', tax_id='DE123456789')
    customer = db.insert_contact(contact_type='customer', entity_type='company',
                                 display_name=CUSTOMER, company_name=CUSTOMER,
                                 customer_number='K-100', street='Kundenweg 5',
                                 postal_code='54321', city='Kundenhausen',
                                 email='pinsel@example.com')
    return own, customer


def add_document(db, number, document_type='invoice', own_id=None, customer_id=None,
                 description='Anstrich', unit='Stk.', **over):
    """Beleg mit einer Position (100,00 € netto, 19 %) anlegen; liefert die ID.

    ``description`` und ``unit`` sind Text und Einheit der Position, ``over`` überschreibt Felder
    von ``insert_invoice`` (z. B. ``status``, ``pdf_path``, ``order_number``)."""
    fields = {
        'invoice_number': number, 'invoice_date': '2026-03-04', 'delivery_date': '2026-03-04',
        'own_company_id': own_id, 'customer_id': customer_id,
        'seller_name': 'Eigene Firma GmbH', 'seller_company': 'Eigene Firma GmbH',
        'seller_street': 'Hauptstr. 1', 'seller_postal_code': '10115', 'seller_city': 'Berlin',
        'seller_email': 'info@example.com', 'seller_phone': '030 1234567',
        'buyer_name': 'Max Mustermann', 'buyer_company': CUSTOMER,
        'buyer_street': 'Kundenweg 5', 'buyer_postal_code': '54321', 'buyer_city': 'Kundenhausen',
        'currency': 'EUR', 'payment_terms': 'Zahlbar in 14 Tagen', 'payment_due_date': '2026-03-18',
        'tax_category': 'S', 'tax_rate': 0.19,
        'sum_net': 100.00, 'tax_amount': 19.00, 'sum_gross': 119.00, 'amount_due': 119.00,
        'status': 'draft', 'document_type': document_type,
    }
    if document_type == 'quote':
        fields.update(valid_until='2026-04-04', intro_text='<b>Guten Tag</b>',
                      closing_text='Mit freundlichen Grüßen')
    fields.update(over)
    doc_id = db.insert_invoice(fields)
    db.insert_invoice_item({'invoice_id': doc_id, 'position': 1, 'description': description,
                            'quantity': 1, 'unit': unit, 'price_per_unit': 100.00,
                            'total_net': 100.00, 'tax_category': 'S', 'tax_rate': 0.19})
    return doc_id


def make_books(auto_login):
    """Eine Rechnung und ein Angebot in der Datenbank des automatisch angemeldeten Administrators
    (Fixture ``auto_login``) samt Ablageordnern ``invoices``/``quotes`` seines Datenordners."""
    db = auto_login.db
    own, customer = add_contacts(db)
    return SimpleNamespace(
        db=db, csrf=auto_login.csrf, own=own, customer=customer,
        invoice=add_document(db, INVOICE_NUMBER, 'invoice', own, customer),
        quote=add_document(db, QUOTE_NUMBER, 'quote', own, customer),
        invoices=os.path.join(user_dir(), 'invoices'), quotes=os.path.join(user_dir(), 'quotes'))


def store_pdf(books, kind, data=PDF):
    """Abgelegte PDF des Belegs (``'invoice'`` | ``'quote'``) so, wie die App sie ablegt: Datei unter
    dem Namensschema und ihr Pfad in der Datenbank. Liefert den Pfad."""
    if kind == 'invoice':
        path = os.path.join(books.invoices, '2026', f'{INVOICE_NUMBER} {CUSTOMER}.pdf')
        books.db.update_invoice_pdf_path(books.invoice, path)
    else:
        path = os.path.join(books.quotes, YEAR, f'{QUOTE_NUMBER} {CUSTOMER}.pdf')
        books.db.update_invoice_pdf_path(books.quote, path)
    return write_file(path, data)


def state(db, *tables):
    """Alle Zeilen der Tabellen (Vorgabe: ``Invoices`` und ``InvoiceItems``), roh und nach ID
    sortiert – zum Vergleich vorher/nachher: Jede Spalte jeder Zeile zählt."""
    tables = tables or ('Invoices', 'InvoiceItems')
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return {t: con.execute(f'SELECT * FROM {t} ORDER BY ID').fetchall() for t in tables}


def row(db, doc_id):
    """Rohe Zeile des Belegs aus ``Invoices`` (``None``, wenn es sie nicht gibt)."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute('SELECT * FROM Invoices WHERE ID = ?', (doc_id,)).fetchone()


def table_rows(html):
    """Zeilen aller Tabellen einer Seite als Listen von Zellentexten (Entitäten aufgelöst, ohne Tags) –
    so, wie der Nutzer die Tabelle liest."""
    from html.parser import HTMLParser

    class Rows(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.rows, self._row, self._cell = [], None, None

        def handle_starttag(self, tag, attrs):
            if tag == 'tr':
                self._row = []
            elif tag in ('td', 'th') and self._row is not None:
                self._cell = ''

        def handle_endtag(self, tag):
            if tag in ('td', 'th') and self._cell is not None:
                self._row.append(self._cell.strip())
                self._cell = None
            elif tag == 'tr' and self._row is not None:
                self.rows.append(self._row)
                self._row = None

        def handle_data(self, data):
            if self._cell is not None:
                self._cell += data

    parser = Rows()
    parser.feed(html)
    parser.close()
    return parser.rows


def scalar(db, sql, *args):
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        return con.execute(sql, args).fetchone()[0]


def set_column(db, doc_id, column, value):
    """Eine Spalte der Zeile direkt in der Datenbank setzen – so, wie es die SQL-Konsole oder eine
    zurückgespielte Sicherung könnte (an der App-Logik vorbei)."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        con.execute(f'UPDATE Invoices SET {column} = ? WHERE ID = ?', (value, doc_id))
        con.commit()


def user_dir(name=installation.ADMIN):
    """Datenordner eines Benutzers (``<DATA_ROOT>/users/<Name>``)."""
    return os.path.join(userctx.DATA_ROOT, userctx.USERS_DIR, name)


@contextlib.contextmanager
def as_user(name=installation.ADMIN):
    """Der Test-Thread arbeitet als dieser Benutzer (wie ein Request): ``Database()`` und die
    Dokumentordner zeigen dann auf ``users/<Name>``. Danach ist der Kontext wieder leer."""
    userctx.set_user(name)
    try:
        yield
    finally:
        userctx.clear()


def write_file(path, data=b'%PDF-1.4 Testinhalt'):
    """Datei samt Ordnern anlegen; liefert den Pfad."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def read_file(path):
    with open(path, 'rb') as f:
        return f.read()


def stored_documents(root=None):
    """Alle abgelegten Dokumente des Benutzers (``invoices``, ``quotes``, ``worktime``) mit Prüfsumme –
    ohne Datenbank und Protokolle, die sich bei jedem Schreibvorgang ändern."""
    tree = installation.tree(root or user_dir())
    return {path: digest for path, digest in tree.items()
            if path.split(os.sep)[0] in ('invoices', 'quotes', 'worktime')}


def document_files(root):
    """Alle Dateien unter ``root`` (relative Pfade, sortiert); fehlt ``root``, ist die Liste leer."""
    return sorted(installation.tree(root))


def post_json(port, path, payload, csrf, headers=None):
    """JSON-POST wie das Frontend (``fetch``): Token im Header ``X-CSRF-Token``. ``payload`` ist
    ein Python-Objekt oder fertiger Text (Bytes) für absichtlich kaputte Bodys."""
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    hdrs = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf, **(headers or {})}
    return send(port, 'POST', path, hdrs, body)


def post_action(port, path, csrf, headers=None):
    """POST ohne Body, Token im Header – wie ``fetch(url, {method: 'POST'})`` des Footer-JS."""
    return send(port, 'POST', path, {'X-CSRF-Token': csrf, **(headers or {})}, b'')


class SmtpRecorder:
    """Ersatz für ``smtplib.SMTP``: merkt sich Verbindung, Anmeldung und gesendete Nachrichten."""

    def __init__(self):
        self.connections, self.logins, self.sent = [], [], []

    def smtp_class(self):
        recorder = self

        class FakeSMTP:
            def __init__(self, host, port):
                recorder.connections.append((host, port))

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def starttls(self):
                pass

            def login(self, user, password):
                recorder.logins.append((user, password))

            def send_message(self, msg):
                recorder.sent.append(msg)

        return FakeSMTP

    def attachments(self, index=-1):
        """``(Dateiname, Inhalt)`` aller Anhänge der gesendeten Nachricht."""
        msg = self.sent[index]
        return [(part.get_filename(), part.get_payload(decode=True))
                for part in msg.walk() if part.get_filename()]


def fake_smtp(monkeypatch):
    """SMTP-Versand ersetzen (kein Netz): Zugangsdaten sind gesetzt, ``smtplib.SMTP`` ist ein
    :class:`SmtpRecorder`. Liefert den Recorder."""
    import email_sender
    recorder = SmtpRecorder()
    monkeypatch.setenv('SMTP_USER', 'versand@example.com')
    monkeypatch.setenv('SMTP_PASSWORD', 'geheim-test')
    monkeypatch.setattr(email_sender.smtplib, 'SMTP', recorder.smtp_class())
    return recorder
