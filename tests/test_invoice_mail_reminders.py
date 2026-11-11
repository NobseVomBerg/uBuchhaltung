# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Rechnung per E-Mail versenden und Mahnwesen (2.0.7).

Beide lasen Spalten einer ``SELECT *``-Zeile über falsche Positionen (der Anhang kam aus der Spalte
``SumGross``, die Mahnliste zeigte Verkäufer-Telefon und Netto statt Kundenname und offenem Betrag,
ein ungültiges Fälligkeitsdatum riss die Seite um). Jetzt stehen die Positionen benannt in
``db.invoices.InvoiceCol``.

* ``handle_send_invoice_email`` (SMTP ersetzt, kein Netz) hängt die abgelegte PDF an, nimmt Betreff und
  Absender aus den richtigen Spalten und verschickt nichts, wenn es keine PDF gibt;
* ``PageReminders`` zeigt je überfälliger bzw. bald fälliger Rechnung deren Kundenname und offenen Betrag.

Alle Namen, Adressen und Beträge sind erfundene Testwerte.
"""
import datetime
import json
import os
import re
from decimal import Decimal

import pytest

from export.pdf_invoice import generate_invoice_pdf
from money import fmt_eur
from server import handlers
from server.pages import PageReminders
from tests.document_helpers import (INVOICE_NUMBER, add_contacts, add_document, fake_smtp, make_books,
                                    post_json, read_file, set_column, table_rows, write_file)
from tests.http_client import get

MAIL = {'recipient_email': 'kunde@example.com', 'recipient_name': 'Erika Empfaenger'}


def send(**fields):
    return json.loads(handlers.handle_send_invoice_email(json.dumps(fields).encode()))


# ── E-Mail-Versand ───────────────────────────────────────────────────────────
@pytest.fixture
def mail(tmp_db, monkeypatch):
    """Datenbank mit eigener Firma, Kunde und einer Rechnung samt abgelegter PDF; SMTP ist ersetzt."""
    own, customer = add_contacts(tmp_db)
    # Summe (Spalte 38, früher als PDF-Pfad gelesen) und Verkäufer-Telefon sehen nicht wie Pfad oder Nummer aus:
    # eine falsch gelesene Spalte fiele sofort auf
    invoice = add_document(tmp_db, INVOICE_NUMBER, 'invoice', own, customer, sum_gross=238.00, sum_net=200.00,
                           tax_amount=38.00, amount_due=238.00, seller_phone='030 7654321')
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    _, pdf_path = generate_invoice_pdf(tmp_db, invoice)
    return type('Mail', (), dict(db=tmp_db, own=own, invoice=invoice, pdf=pdf_path, smtp=fake_smtp(monkeypatch)))


def test_mail_attaches_the_stored_pdf_with_its_file_name(mail):
    assert send(invoice_id=mail.invoice, **MAIL) == {'success': True}
    assert mail.smtp.attachments() == [(f'{INVOICE_NUMBER} Malermeister Pinsel.pdf', read_file(mail.pdf))]
    assert mail.smtp.attachments()[0][1].startswith(b'%PDF')


def test_mail_takes_recipient_subject_and_sender_from_the_right_columns(mail):
    assert send(invoice_id=mail.invoice, message_text='Anbei Ihre Rechnung.', **MAIL) == {'success': True}
    msg = mail.smtp.sent[0]
    assert msg['To'] == 'Erika Empfaenger <kunde@example.com>'
    assert msg['Subject'] == f'Rechnung {INVOICE_NUMBER}'                  # InvoiceNumber, nicht Summe oder Datum
    assert msg['From'] == 'Eigene Firma GmbH <info@example.com>'           # Firmenname und E-Mail der eigenen Firma
    body = [p for p in msg.walk() if p.get_content_type() == 'text/plain'][0]
    assert body.get_payload(decode=True).decode('utf-8') == 'Anbei Ihre Rechnung.'
    assert mail.smtp.logins == [('versand@example.com', 'geheim-test')]


def test_mail_without_a_message_text_uses_the_default_text_with_the_invoice_number(mail):
    send(invoice_id=mail.invoice, **MAIL)
    body = [p for p in mail.smtp.sent[0].walk() if p.get_content_type() == 'text/plain'][0]
    text = body.get_payload(decode=True).decode('utf-8')
    assert f'die Rechnung {INVOICE_NUMBER}' in text and text.rstrip().endswith('Eigene Firma GmbH')


def test_mail_attaches_the_pdf_even_if_the_database_path_is_missing(mail):
    set_column(mail.db, mail.invoice, 'PDFPath', None)                    # Fallback über das Namensschema
    assert send(invoice_id=mail.invoice, **MAIL) == {'success': True}
    assert mail.smtp.attachments()[0][0] == f'{INVOICE_NUMBER} Malermeister Pinsel.pdf'


def test_mail_without_a_stored_pdf_sends_nothing(mail):
    os.remove(mail.pdf)
    assert send(invoice_id=mail.invoice, **MAIL) == {'success': False,
                                                      'error': 'Kein PDF vorhanden – bitte zuerst erzeugen.'}
    assert mail.smtp.connections == [] and mail.smtp.sent == []


@pytest.mark.parametrize('fields, error', [
    pytest.param({'invoice_id': 99999, **MAIL}, 'Rechnung nicht gefunden', id='unbekannte-Rechnung'),
    pytest.param({'recipient_email': ''}, 'Fehlende Parameter', id='ohne-Empfänger'),
    pytest.param({'recipient_name': 'Erika'}, 'Fehlende Parameter', id='ohne-Adresse'),
])
def test_mail_rejects_unusable_requests_without_sending(mail, fields, error):
    assert send(**{'invoice_id': mail.invoice, **fields}) == {'success': False, 'error': error}
    assert mail.smtp.connections == []


def test_mail_without_a_sender_contact_sends_nothing(tmp_db, monkeypatch):
    invoice = add_document(tmp_db, INVOICE_NUMBER, 'invoice')             # keine eigene Firma in den Kontakten
    monkeypatch.setattr(handlers, 'Database', lambda *a, **k: tmp_db)
    generate_invoice_pdf(tmp_db, invoice)
    smtp = fake_smtp(monkeypatch)
    assert send(invoice_id=invoice, **MAIL) == {'success': False, 'error': 'Keine Absender-Kontaktdaten gefunden'}
    assert smtp.connections == []


def test_mail_without_smtp_credentials_reports_it_and_sends_nothing(mail, monkeypatch):
    monkeypatch.delenv('SMTP_USER')
    result = send(invoice_id=mail.invoice, **MAIL)
    assert result['success'] is False and 'SMTP-Zugangsdaten nicht konfiguriert' in result['error']
    assert mail.smtp.connections == []


def test_mail_route_over_http(server_port, auto_login, monkeypatch):
    smtp = fake_smtp(monkeypatch)
    books = make_books(auto_login)
    pdf = write_file(os.path.join(books.invoices, '2026', f'{INVOICE_NUMBER} Malermeister Pinsel.pdf'),
                     b'%PDF-1.4 abgelegt')
    books.db.update_invoice_pdf_path(books.invoice, pdf)
    resp = post_json(server_port, '/invoice/send-email', {'invoice_id': books.invoice, **MAIL}, books.csrf)
    assert resp.status == 200 and json.loads(resp.text) == {'success': True}
    assert smtp.sent[0]['Subject'] == f'Rechnung {INVOICE_NUMBER}'
    assert smtp.attachments() == [(os.path.basename(pdf), b'%PDF-1.4 abgelegt')]


# ── Mahnwesen ────────────────────────────────────────────────────────────────
TODAY = datetime.date.today()


def day(offset):
    return (TODAY + datetime.timedelta(days=offset)).isoformat()


@pytest.fixture
def dunning(tmp_db):
    """Überfällige und bald fällige Rechnungen mit unterscheidbaren Beträgen, Namen und Telefonnummern.

    Nummern, Verkäufer-Telefon, USt-IdNr. des Käufers und Netto sind je Rechnung andere Werte als Kundenname,
    Fälligkeit und offener Betrag – eine falsch gelesene Spalte zeigte sich sofort."""
    own, customer = add_contacts(tmp_db)

    def invoice(number, **over):
        fields = dict(status='sent', seller_phone='030 7654321', buyer_vat_id='DE123456789',
                      payment_due_date=day(-45), sum_net=200.00, tax_amount=38.00, sum_gross=238.00,
                      amount_due=100.00)
        fields.update(over)
        return add_document(tmp_db, number, 'invoice', own, customer, **fields)

    ids = {
        'teilzahlung': invoice('26R101', buyer_company='Alpha Kunde GmbH', buyer_name='Anna Alpha'),
        'stufe1': invoice('26R102', status='finalized', buyer_company='', buyer_name='Bernd Beta',
                          payment_due_date=day(-10), sum_gross=59.50, amount_due=59.50),
        'stufe3': invoice('26R103', buyer_company='Gamma KG', buyer_name='Greta Gamma', payment_due_date=day(-90),
                          amount_due=238.00),
        'bald': invoice('26R104', buyer_company='Delta AG', buyer_name='Dora Delta', payment_due_date=day(3),
                        amount_due=77.00),
        # gehören nicht auf die Seite
        'bezahlt': invoice('26R105', status='paid', amount_due=0, buyer_company='Epsilon'),
        'entwurf': invoice('26R106', status='draft', buyer_company='Zeta'),
        'storniert': invoice('26R107', status='cancelled', buyer_company='Eta'),
        'ohne_rest': invoice('26R108', amount_due=0, buyer_company='Theta'),
        'spaeter': invoice('26R109', payment_due_date=day(30), buyer_company='Iota'),
    }
    add_document(tmp_db, '26A110', 'quote', own, customer, status='sent', payment_due_date=day(-20),
                 buyer_company='Kappa Angebot')
    return type('Dunning', (), dict(db=tmp_db, ids=ids))


def reminder_tables(html):
    """(überfällig, bald fällig) als Zeilen der beiden Tabellen (ohne Kopfzeilen); eine Tabelle, die es
    mangels Rechnungen nicht gibt, ist leer."""
    overdue, soon, current = [], [], None
    for cells in table_rows(html):
        if cells[:1] == ['RE-Nr.']:
            current = soon if cells[2] == 'Fälligkeitsdatum' else overdue       # die Kopfzeile sagt, welche Tabelle
        elif current is not None:
            current.append(cells)
    return overdue, soon


def test_reminders_show_customer_name_and_open_amount_of_the_right_invoice(dunning):
    overdue, soon = reminder_tables(PageReminders(dunning.db))
    by_number = {r[0]: r for r in overdue}
    assert sorted(by_number) == ['26R101', '26R102', '26R103']
    # RE-Nr., Datum, Fällig seit, Kunde, Betrag (offener Rest, nicht Brutto/Netto), Verzug
    assert by_number['26R101'][2:5] == [day(-45), 'Alpha Kunde GmbH', fmt_eur(Decimal('100'))]
    assert by_number['26R102'][2:5] == [day(-10), 'Bernd Beta', fmt_eur(Decimal('59.50'))]   # ohne Firma: der Name
    assert by_number['26R103'][2:5] == [day(-90), 'Gamma KG', fmt_eur(Decimal('238'))]
    assert [r[0] for r in soon] == ['26R104']
    assert soon[0][2:5] == [day(3), 'Delta AG', fmt_eur(Decimal('77'))]


def test_reminders_never_show_seller_phone_buyer_vat_id_or_net_amounts(dunning):
    html = PageReminders(dunning.db)
    assert '030 7654321' not in html and 'DE123456789' not in html
    overdue, soon = reminder_tables(html)
    amounts = {r[4] for r in overdue + soon}
    assert amounts == {fmt_eur(Decimal(v)) for v in ('100', '59.50', '238', '77')}      # nie Netto (200) oder Steuer (38)


def test_reminders_count_days_and_name_the_dunning_level(dunning):
    overdue, soon = reminder_tables(PageReminders(dunning.db))
    by_number = {r[0]: r for r in overdue}
    assert by_number['26R102'][5].startswith('10 Tage') and 'Mahnstufe 1' in by_number['26R102'][5]
    assert by_number['26R101'][5].startswith('45 Tage') and 'Mahnstufe 2' in by_number['26R101'][5]
    assert by_number['26R103'][5].startswith('90 Tage') and 'Mahnstufe 3' in by_number['26R103'][5]
    assert soon[0][5] == '3 Tage'


def test_reminders_total_is_the_sum_of_the_open_amounts(dunning):
    html = PageReminders(dunning.db)
    total = fmt_eur(Decimal('100') + Decimal('59.50') + Decimal('238'))
    assert re.search(rf'<strong>3 Rechnung\(en\)</strong> überfällig \|\s*Offener Betrag: <strong[^>]*>{re.escape(total)}</strong>',
                     html)


def test_reminders_list_only_open_invoices(dunning):
    html = PageReminders(dunning.db)
    for name in ('Epsilon', 'Zeta', 'Eta', 'Theta', 'Iota', 'Kappa'):           # bezahlt, Entwurf, storniert, ohne Rest,
        assert name not in html, name                                            # erst später fällig, Angebot
    assert 'Keine überfälligen' not in html


def test_reminders_link_only_to_the_invoice_view(dunning):
    html = PageReminders(dunning.db)
    links = re.findall(r"href='(/invoice/[^']*)'", html)
    assert sorted(links) == sorted(f'/invoice/view?id={dunning.ids[k]}'
                                   for k in ('teilzahlung', 'stufe1', 'stufe3', 'bald'))
    assert '/invoice/reminder' not in html and 'Mahnung erstellen' not in html     # die Route gibt es nicht


def test_reminders_survive_an_invalid_due_date(dunning):
    # „2000-02-30“ ist kein Datum, aber als Text kleiner als heute: früher brach `fromisoformat` die Seite ab
    set_column(dunning.db, dunning.ids['stufe1'], 'PaymentDueDate', '2000-02-30')
    overdue, _ = reminder_tables(PageReminders(dunning.db))
    row = next(r for r in overdue if r[0] == '26R102')
    assert row[2] == '2000-02-30' and row[5] == '–'                                # keine Verzugstage, kein Absturz


def test_reminders_show_hostile_values_as_text(tmp_db):
    own, customer = add_contacts(tmp_db)
    add_document(tmp_db, '26R"><i>1', 'invoice', own, customer, status='sent', payment_due_date=day(-5),
                 buyer_company='<b>Fett</b> & Co', buyer_name='x', amount_due=10.0)
    html = PageReminders(tmp_db)
    assert '<b>Fett' not in html and '<i>1' not in html
    overdue, _ = reminder_tables(html)
    assert overdue[0][0] == '26R"><i>1' and overdue[0][3] == '<b>Fett</b> & Co'


def test_reminders_with_nothing_open_say_so(tmp_db):
    html = PageReminders(tmp_db)
    assert 'Keine überfälligen Rechnungen' in html and 'Keine Rechnungen in den nächsten 7 Tagen' in html
    assert '0 Rechnung(en)' in html


def test_reminders_over_http(server_port, auto_login):
    books = make_books(auto_login)
    set_column(books.db, books.invoice, 'Status', 'sent')
    set_column(books.db, books.invoice, 'PaymentDueDate', day(-12))
    resp = get(server_port, '/invoice/reminders')
    assert resp.status == 200
    overdue, _ = reminder_tables(resp.text)
    assert overdue[0][0] == INVOICE_NUMBER and overdue[0][3] == 'Malermeister Pinsel'
    assert overdue[0][4] == fmt_eur(Decimal('119'))
