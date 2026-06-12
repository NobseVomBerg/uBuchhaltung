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
from .pdf_core import load_image_xobject, build_single_page_pdf
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

    tax_rate = invoice[35] or 0.19
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
            image = load_image_xobject(own_contact[13])
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

    # ── Content-Stream aufbauen ──────────────────────────────────────────────
    ops = ["BT"]
    line_ops = []

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
    y = D.draw_letterhead(ops, image, "Rechnung", meta_rows, sender_line, address_lines)

    items = [{'pos': it[2], 'quantity': it[5], 'unit': it[6] or 'Stk.',
              'description': it[4], 'price': it[7], 'total': it[8]} for it in invoice_items]
    y, rule_ops = D.draw_item_table(ops, items, y, tax_rate_pct=_pct(tax_rate),
                                    sum_net=sum_net, tax_amount=tax_amount, sum_gross=sum_gross)
    line_ops += rule_ops

    # Zahlungsbedingungen (Fließtext)
    y -= 6
    y = D.draw_richtext(ops, payment_terms, y, size=9, leading=12)

    # Dreispaltige Fußzeile (Anschrift | Kontakt | Bankverbindung)
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
    D.draw_footer_columns(ops, line_ops, [col_address, col_contact, col_bank])

    ops.append("ET")
    ops += line_ops               # Linien außerhalb des Textobjekts zeichnen

    full_pdf = build_single_page_pdf(ops, image=image)

    # PDF-Datei speichern
    current_year = datetime.datetime.now().year
    pdf_dir = f"data/invoices/{current_year}"
    os.makedirs(pdf_dir, exist_ok=True)
    safe_number = "".join(c for c in invoice_number if c.isalnum() or c in ['-', '_'])
    pdf_filename = f"Rechnung_{safe_number}.pdf"
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
