"""
Rechnungs-PDF-Generator.

Erzeugt ein einseitiges Rechnungs-PDF direkt aus den Datenbankdaten und nutzt dafür
die gemeinsamen Primitive aus :mod:`export.pdf_core`.
"""
import datetime
import os
from db import Database
from .pdf_core import escape_pdf_string, load_image_xobject, build_single_page_pdf


def generate_invoice_pdf(db: Database, invoice_id: int):
    """PDF für eine bestehende Rechnung erzeugen.

    Args:
        db: Datenbank-Instanz.
        invoice_id: ID der Rechnung.

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

    # Rechnungsdaten (Spaltenindizes siehe db.get_invoice_by_id)
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

    # Logo + Steuernummer der eigenen Firma laden
    image = None
    own_company_id = invoice[3]
    if own_company_id:
        own_contact = db.get_contact_by_id(own_company_id)
        if own_contact and len(own_contact) > 13 and own_contact[13]:
            image = load_image_xobject(own_contact[13])
        if own_contact and len(own_contact) > 11:
            seller_tax_id = own_contact[11] or ''

    # ── Content-Stream aufbauen ──────────────────────────────────────────────
    ops = ["BT"]

    # Logo oben rechts (nur wenn vorhanden)
    if image:
        ops.append(f"q {image['width']} 0 0 {image['height']} 430 750 cm /Logo Do Q")

    # Titel
    ops.append("/F2 24 Tf")
    ops.append("50 750 Td")
    ops.append("(Rechnung) Tj")

    # Nummer + Datum
    ops.append("/F1 10 Tf")
    ops.append("0 -20 Td")
    ops.append(f"(Rechnungs-Nr.: {escape_pdf_string(invoice_number)}) Tj")
    ops.append("0 -15 Td")
    ops.append(f"(Datum: {escape_pdf_string(invoice_date)}) Tj")

    # Absenderzeile (klein)
    ops.append("0 -40 Td")
    ops.append("/F3 8 Tf")
    seller_line = f"{seller_company or seller_name} · {seller_street} · {seller_postal} {seller_city}"
    ops.append(f"({escape_pdf_string(seller_line)}) Tj")

    # Empfängeradresse
    ops.append("0 -25 Td")
    ops.append("/F1 10 Tf")
    if buyer_company:
        ops.append(f"({escape_pdf_string(buyer_company)}) Tj")
        ops.append("0 -15 Td")
    if buyer_name:
        ops.append(f"({escape_pdf_string(buyer_name)}) Tj")
        ops.append("0 -15 Td")
    if buyer_street:
        ops.append(f"({escape_pdf_string(buyer_street)}) Tj")
        ops.append("0 -15 Td")
    if buyer_postal or buyer_city:
        ops.append(f"({escape_pdf_string(f'{buyer_postal} {buyer_city}')}) Tj")
        ops.append("0 -15 Td")

    # Positionstabelle
    ops.append("0 -30 Td")
    ops.append("/F2 10 Tf")
    ops.append("(Pos.   Menge   Einheit   Bezeichnung                                 Einzelpreis   Gesamt) Tj")
    ops.append("0 -15 Td")
    ops.append("/F1 10 Tf")

    for item in invoice_items:
        pos = item[2] or 1
        quantity = item[5] or 0
        unit = item[6] or 'Stk.'
        description = item[4] or ''
        price = item[7] or 0
        total = item[8] or 0
        if len(description) > 35:
            description = description[:32] + '...'
        item_line = f"{pos}      {quantity:.2f}   {unit:8s}  {description:35s}  {price:8.2f} EUR  {total:8.2f} EUR"
        ops.append(f"({escape_pdf_string(item_line)}) Tj")
        ops.append("0 -15 Td")

    # Summen
    ops.append("0 -10 Td")
    ops.append(f"(Summe netto:                                                           {sum_net:8.2f} EUR) Tj")
    ops.append("0 -15 Td")
    tax_percent = int(tax_rate * 100)
    ops.append(f"(Mehrwertsteuer {tax_percent}%:                                                       {tax_amount:8.2f} EUR) Tj")
    ops.append("0 -15 Td")
    ops.append("/F2 10 Tf")
    ops.append(f"(Gesamtbetrag:                                                          {sum_gross:8.2f} EUR) Tj")

    # Zahlungsbedingungen (einfacher Wortumbruch)
    ops.append("0 -30 Td")
    ops.append("/F1 10 Tf")
    words = payment_terms.split()
    line = ""
    for word in words:
        if len(line + word) > 80:
            ops.append(f"({escape_pdf_string(line)}) Tj")
            ops.append("0 -15 Td")
            line = word + " "
        else:
            line += word + " "
    if line:
        ops.append(f"({escape_pdf_string(line.strip())}) Tj")

    # Bankverbindung
    ops.append("0 -30 Td")
    ops.append("/F2 10 Tf")
    ops.append("(Bankverbindung:) Tj")
    ops.append("0 -15 Td")
    ops.append("/F1 10 Tf")
    if bank_name:
        ops.append(f"(Bank: {escape_pdf_string(bank_name)}) Tj")
        ops.append("0 -15 Td")
    if bank_iban:
        ops.append(f"(IBAN: {escape_pdf_string(bank_iban)}) Tj")
        ops.append("0 -15 Td")
    if bank_bic:
        ops.append(f"(BIC: {escape_pdf_string(bank_bic)}) Tj")

    # Fußzeile
    ops.append("0 -50 Td")
    ops.append("/F3 8 Tf")
    footer_line1 = f"{seller_company or seller_name}    {seller_street}    {seller_postal} {seller_city}"
    ops.append(f"({escape_pdf_string(footer_line1)}) Tj")
    ops.append("0 -12 Td")
    footer_line2 = f"Tel: {seller_phone}    E-Mail: {seller_email}"
    if seller_tax_id:
        footer_line2 += f"    UStIdNr: {seller_tax_id}"
    ops.append(f"({escape_pdf_string(footer_line2)}) Tj")

    ops.append("ET")

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
