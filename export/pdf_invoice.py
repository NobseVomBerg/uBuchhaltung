# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Rechnungs-PDF-Generator.

Erzeugt ein einseitiges Rechnungs-PDF direkt aus den Datenbankdaten. Die gemeinsamen
Dokument-Bausteine (Kopf, Adressen, Positionstabelle, Beträge) liegen in
:mod:`export.pdf_document` und werden mit dem Angebots-Generator geteilt; hier bleiben
nur die rechnungsspezifischen Teile (Zahlungsbedingungen, Bankverbindung, Fußzeile).
"""
import datetime
import os
from db import Database
from .pdf_core import (load_image_xobject, build_multi_page_pdf, resolve_logo_path,
                       safe_filename_component)
from . import pdf_document as D


def _pct(tax_rate):
    """Steuersatz als Prozent (DB speichert teils 0.19, teils 19)."""
    if tax_rate is None:
        return 0
    return tax_rate * 100 if tax_rate <= 1 else tax_rate


def generate_invoice_pdf(db: Database, invoice_id: int):
    """PDF für eine bestehende Rechnung erzeugen.

    Returns:
        tuple: (pdf_bytes, pdf_path) oder (None, None), falls Rechnung/Positionen fehlen.
    """
    invoice = db.get_invoice_by_id(invoice_id)
    if not invoice:
        print(f"Invoice {invoice_id} not found")
        return None, None

    invoice_items = db.get_invoice_items(invoice_id)
    if not invoice_items:
        print(f"No items found for invoice {invoice_id}")
        return None, None

    invoice_number = invoice[1] or 'DRAFT'
    invoice_date = invoice[2] or datetime.datetime.now().strftime('%Y-%m-%d')

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
    # Absenderzeile im Adressfeld OHNE Zusatzzeile (sonst zu lang); die
    # Zusatzzeile erscheint nur in der Fußzeile.
    sender_line = " · ".join(p for p in [
        seller_company or seller_name, seller_street,
        f"{seller_postal} {seller_city}".strip()] if p)
    buyer_country = invoice[19] if len(invoice) > 19 else None
    address_lines = D.address_block(buyer_company, buyer_name, buyer_addr_extra,
                                    buyer_street, buyer_postal, buyer_city, buyer_country)
    flow.y = D.draw_letterhead(flow.ops, image, "Rechnung", meta_rows, sender_line, address_lines)

    items = [{'pos': it[2], 'quantity': it[5], 'unit': it[6] or 'Stk.',
              'description': it[4], 'price': it[7], 'total': it[8]} for it in invoice_items]
    D.draw_item_table(flow, items, tax_rate_pct=_pct(tax_rate),
                      sum_net=sum_net, tax_amount=tax_amount, sum_gross=sum_gross,
                      show_tax=show_tax)

    # Zahlungsbedingungen (Fließtext)
    D.flow_richtext(flow, payment_terms, size=9, leading=12, gap_before=6)

    full_pdf = build_multi_page_pdf(flow.finish(), image=image)

    # PDF-Datei speichern (im Datenbereich des aktuellen Nutzers)
    import userctx
    current_year = datetime.datetime.now().year
    pdf_dir = os.path.join(userctx.user_data_dir(), "invoices", str(current_year))
    os.makedirs(pdf_dir, exist_ok=True)
    # Namenskonvention "[Rechnungsnummer] [Kundenname]" (todo #1)
    customer = safe_filename_component(buyer_company or buyer_name)
    parts = [safe_filename_component(invoice_number), customer]
    pdf_filename = " ".join(p for p in parts if p) + ".pdf"
    pdf_path = os.path.join(pdf_dir, pdf_filename)

    try:
        with open(pdf_path, 'wb') as f:
            f.write(full_pdf)
        print(f"PDF saved to: {pdf_path}")
        db.update_invoice_pdf_path(invoice_id, pdf_path)
    except Exception as e:
        print(f"Error saving PDF file: {e}")
        return None, None

    return full_pdf, pdf_path
