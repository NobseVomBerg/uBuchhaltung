# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Rechnungs-PDF-Generator.

Erzeugt das Rechnungs-PDF direkt aus den Datenbankdaten – als hybride E-Rechnung
(ZUGFeRD 2.x / Factur-X, Profil EN 16931): PDF/A-3b mit eingebetteter
``factur-x.xml`` (CII). Die gemeinsamen
Dokument-Bausteine (Kopf, Adressen, Positionstabelle, Beträge) liegen in
:mod:`export.pdf_document` und werden mit dem Angebots-Generator geteilt; hier bleiben
nur die rechnungsspezifischen Teile (Zahlungsbedingungen, Bankverbindung, Fußzeile).
"""
import datetime
import html
import os
from db import Database
from db.invoice_lock import STORNO_KIND
from .pdf_core import (load_image_xobject, build_multi_page_pdf, resolve_logo_path)
from .cii_invoice import generate_cii
from .einvoice_model import correction_note_text, correction_notice, unit_display
from .invoice_files import drop_stale_file, invoice_file_path
from . import pdf_document as D

STORNO_TITLE = 'Stornorechnung'


def _german_date(value):
    """ISO-Datum (``JJJJ-MM-TT``) als ``TT.MM.JJJJ``; was sich nicht lesen lässt, bleibt, wie es ist."""
    try:
        return datetime.date.fromisoformat(str(value)[:10]).strftime('%d.%m.%Y')
    except ValueError:
        return str(value or '')


def _pct(tax_rate):
    """Steuersatz als Prozent (DB speichert teils 0.19, teils 19)."""
    if tax_rate is None:
        return 0
    return tax_rate * 100 if tax_rate <= 1 else tax_rate


def generate_invoice_pdf(db: Database, invoice_id: int):
    """PDF für eine bestehende Rechnung erzeugen.

    Returns:
        tuple: (pdf_bytes, pdf_path) oder (None, None), falls Rechnung/Positionen fehlen
        oder die ID zu einem Angebot gehört (dessen PDFPath bleibt unberührt).
    """
    invoice = db.get_document(invoice_id, 'invoice')
    if not invoice:
        print(f"Invoice {invoice_id} not found")
        return None, None

    invoice_items = db.get_invoice_items(invoice_id)
    if not invoice_items:
        print(f"No items found for invoice {invoice_id}")
        return None, None

    invoice_number = invoice[1] or 'DRAFT'
    invoice_date = invoice[2] or datetime.datetime.now().strftime('%Y-%m-%d')
    # Stornorechnung: eigener Titel, „zu Rechnung Nr. … vom …“ unter dem Titel, die gespeicherten (negativen)
    # Beträge; sie trägt nie den Hinweis einer Berichtigung
    storno = len(invoice) > 50 and invoice[50] == STORNO_KIND
    doc_title = STORNO_TITLE if storno else 'Rechnung'
    subtitle = None
    if storno:
        original = db.get_invoice_by_id(invoice[51]) if invoice[51] else None
        if original is not None:
            subtitle = f"zu Rechnung Nr. {original[1]} vom {_german_date(original[2])}"

    seller_name = invoice[4] or ''
    seller_company = invoice[5] or ''
    seller_street = invoice[6] or ''
    seller_postal = invoice[7] or ''
    seller_city = invoice[8] or ''
    seller_email = invoice[11] or ''
    seller_phone = invoice[12] or ''
    seller_tax_id = ''

    buyer_name = invoice[14] or ''
    buyer_company = invoice[15] or ''
    buyer_street = invoice[16] or ''
    buyer_postal = invoice[17] or ''
    buyer_city = invoice[18] or ''

    bank_name = invoice[31] or ''
    bank_iban = invoice[32] or ''
    bank_bic = invoice[33] or ''

    # 0 nicht zu 19% verfälschen; Sentinel -1 = Kleinunternehmer (§19), keine USt
    tax_rate = invoice[35] if invoice[35] is not None else 0.19
    show_tax = tax_rate >= 0
    sum_net = invoice[36] or 0
    tax_amount = invoice[37] or 0
    sum_gross = invoice[38] or 0

    payment_terms = invoice[26] or ('Bitte überweisen Sie den Gesamtbetrag ohne jeden '
        'Abzug unter Angabe der Rechnungsnummer innerhalb von 14 Tagen ab Rechnungsdatum '
        'auf das unten angegebene Konto. Vielen Dank.')

    # Logo, Steuernummer und Adress-Zusatzzeile der eigenen Firma laden
    image = None
    seller_addr_extra = ''
    own_company_id = invoice[3]
    if own_company_id:
        own_contact = db.get_contact_by_id(own_company_id)
        if own_contact and len(own_contact) > 13 and own_contact[13]:
            image = load_image_xobject(resolve_logo_path(own_contact[13]))
        if own_contact and len(own_contact) > 11:
            seller_tax_id = own_contact[11] or ''
        if own_contact and len(own_contact) > 25:
            seller_addr_extra = own_contact[25] or ''

    # Käufer-Kontakt für Kunden-Nr. und Adress-Zusatzzeile
    buyer_customer_number = ''
    buyer_addr_extra = ''
    customer_id = invoice[13]
    if customer_id:
        buyer_contact = db.get_contact_by_id(customer_id)
        if buyer_contact:
            if len(buyer_contact) > 2:
                buyer_customer_number = buyer_contact[2] or ''
            if len(buyer_contact) > 25:
                buyer_addr_extra = buyer_contact[25] or ''

    # Dreispaltige Fußzeile (Anschrift | Kontakt | Bankverbindung) – pro Seite
    col_address = [seller_company or seller_name]
    if seller_addr_extra:
        col_address.append(seller_addr_extra)
    col_address += [seller_street, f"{seller_postal} {seller_city}".strip()]
    col_contact = []
    if seller_phone:
        col_contact.append(f"Tel: {seller_phone}")
    if seller_email:
        col_contact.append(f"E-Mail: {seller_email}")
    if seller_tax_id:
        col_contact.append(f"USt-IdNr: {seller_tax_id}")
    col_bank = []
    if bank_name or bank_iban or bank_bic:
        col_bank.append("Bankverbindung")
        if bank_name:
            col_bank.append(f"Bank: {bank_name}")
        if bank_iban:
            col_bank.append(f"IBAN: {bank_iban}")
        if bank_bic:
            col_bank.append(f"BIC: {bank_bic}")

    # ── Content-Stream aufbauen (seitenfähig) ────────────────────────────────
    flow = D.PageFlow([col_address, col_contact, col_bank])

    meta_rows = [("Datum:", invoice_date), ("Rechnungs-Nr.:", invoice_number)]
    if buyer_customer_number:
        meta_rows.append(("Kunden-Nr.:", buyer_customer_number))
    if invoice[25]:
        meta_rows.append(("Leistungsdatum:", invoice[25]))
    # Eine berichtigte Fassung nennt ihre Nummer im Kopf und trägt den Hinweis vor der Positionstabelle
    # (derselbe Text steht als BT-22 in der eingebetteten E-Rechnung); er ergibt sich aus dem Stand der Datenbank
    notice = None if storno else correction_notice(db, invoice)
    if notice:
        meta_rows.append(("Fassung:", str(notice.version)))
    # Absenderzeile im Adressfeld OHNE Zusatzzeile (sonst zu lang); die
    # Zusatzzeile erscheint nur in der Fußzeile.
    sender_line = " · ".join(p for p in [
        seller_company or seller_name, seller_street,
        f"{seller_postal} {seller_city}".strip()] if p)
    buyer_country = invoice[19] if len(invoice) > 19 else None
    address_lines = D.address_block(buyer_company, buyer_name, buyer_addr_extra,
                                    buyer_street, buyer_postal, buyer_city, buyer_country)
    flow.y = D.draw_letterhead(flow.ops, image, doc_title, meta_rows, sender_line, address_lines,
                               subtitle=subtitle)
    if notice:
        D.flow_richtext(flow, f'<b>{html.escape(correction_note_text(notice), quote=False)}</b>',
                        size=9, leading=12)

    items = [{'pos': it[2], 'quantity': it[5], 'unit': unit_display(it[6]),
              'description': it[4], 'price': it[7], 'total': it[8]} for it in invoice_items]
    D.draw_item_table(flow, items, tax_rate_pct=_pct(tax_rate),
                      sum_net=sum_net, tax_amount=tax_amount, sum_gross=sum_gross,
                      show_tax=show_tax)

    # Zahlungsbedingungen (Fließtext)
    D.flow_richtext(flow, payment_terms, size=9, leading=12, gap_before=6)

    # Eingebettete E-Rechnung (CII) aus demselben Modell wie die UBL-XRechnung.
    # Schlägt die Erzeugung fehl, bricht das PDF ab – kein stilles Nicht-Hybrid-PDF.
    facturx_xml = generate_cii(invoice, invoice_items, db=db)
    full_pdf = build_multi_page_pdf(
        flow.finish(), image=image, title=f"{doc_title} {invoice_number}",
        author=seller_company or seller_name, subject=f"{doc_title} an {buyer_company or buyer_name}",
        facturx_xml=facturx_xml)

    # PDF-Datei speichern (im Datenbereich des aktuellen Nutzers) unter der
    # Namenskonvention "[Rechnungs-Nr.] [Kundenname]" (todo #2)
    pdf_path = invoice_file_path(invoice, 'pdf', db)

    try:
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        with open(pdf_path, 'wb') as f:
            f.write(full_pdf)
        print(f"PDF saved to: {pdf_path}")
        # Umbenannte Rechnung (neue Nummer/anderer Kunde): Vorgänger entfernen
        drop_stale_file(invoice[41] if len(invoice) > 41 else None, pdf_path, db, invoice_id)
        db.update_invoice_pdf_path(invoice_id, pdf_path)
    except Exception as e:
        print(f"Error saving PDF file: {e}")
        return None, None

    return full_pdf, pdf_path
