"""
Angebots-PDF-Generator (Phase 1).

Teilt sich die Dokument-Bausteine (Kopf, Adressen, Positionstabelle, Beträge,
Fließtext) mit dem Rechnungs-Generator über :mod:`export.pdf_document`. Abweichend von
der Rechnung: Titel „Angebot", Gültigkeitsdatum, Einleitungs-/Schlusstext (formatierter
Fließtext) statt Bank-/Zahlungsteil.

Phase 1 ist einseitig; der mehrseitige Fließtext-Satz folgt in Phase 2.
"""
import datetime
import os
from db import Database
from .pdf_core import load_image_xobject, build_single_page_pdf
from . import pdf_document as D


def _pct(tax_rate):
    if tax_rate is None:
        return 0
    return tax_rate * 100 if tax_rate <= 1 else tax_rate


def generate_quote_pdf(db: Database, quote_id: int):
    """PDF für ein bestehendes Angebot erzeugen. Returns (pdf_bytes, pdf_path)."""
    quote = db.get_invoice_by_id(quote_id)
    if not quote:
        print(f"Quote {quote_id} not found")
        return None, None

    items_rows = db.get_invoice_items(quote_id)
    if not items_rows:
        print(f"No items found for quote {quote_id}")
        return None, None

    quote_number = quote[1] or 'ENTWURF'
    quote_date = quote[2] or datetime.datetime.now().strftime('%Y-%m-%d')

    seller_name = quote[4] or ''
    seller_company = quote[5] or ''
    seller_street = quote[6] or ''
    seller_postal = quote[7] or ''
    seller_city = quote[8] or ''
    seller_email = quote[11] or ''
    seller_phone = quote[12] or ''
    seller_tax_id = ''

    buyer_name = quote[14] or ''
    buyer_company = quote[15] or ''
    buyer_street = quote[16] or ''
    buyer_postal = quote[17] or ''
    buyer_city = quote[18] or ''

    tax_rate = quote[35] or 0.19
    sum_net = quote[36] or 0
    tax_amount = quote[37] or 0
    sum_gross = quote[38] or 0

    valid_until = quote[46] if len(quote) > 46 else ''
    intro_text = quote[47] if len(quote) > 47 else ''
    closing_text = quote[48] if len(quote) > 48 else ''

    image = None
    own_company_id = quote[3]
    if own_company_id:
        own_contact = db.get_contact_by_id(own_company_id)
        if own_contact and len(own_contact) > 13 and own_contact[13]:
            image = load_image_xobject(own_contact[13])
        if own_contact and len(own_contact) > 11:
            seller_tax_id = own_contact[11] or ''

    # ── Content-Stream ────────────────────────────────────────────────────────
    ops = ["BT"]
    line_ops = []

    meta = [("Datum:", quote_date), ("Angebots-Nr.:", quote_number)]
    if valid_until:
        meta.append(("gültig bis:", valid_until))
    sender_line = f"{seller_company or seller_name} · {seller_street} · {seller_postal} {seller_city}"
    address_lines = [buyer_company, buyer_name, buyer_street, f"{buyer_postal} {buyer_city}".strip()]
    y = D.draw_letterhead(ops, image, "Angebot", meta, sender_line, address_lines)

    # Einleitungstext
    if intro_text:
        y = D.draw_richtext(ops, intro_text, y, size=10, leading=14)
        y -= 6

    items = [{'pos': it[2], 'quantity': it[5], 'unit': it[6] or 'Stk.',
              'description': it[4], 'price': it[7], 'total': it[8]} for it in items_rows]
    y, rule_ops = D.draw_item_table(ops, items, y, tax_rate_pct=_pct(tax_rate),
                                    sum_net=sum_net, tax_amount=tax_amount, sum_gross=sum_gross)
    line_ops += rule_ops

    # Schlusstext
    if closing_text:
        y -= 6
        y = D.draw_richtext(ops, closing_text, y, size=10, leading=14)

    # Dreispaltige Fußzeile (Anschrift | Kontakt)
    col_address = [seller_company or seller_name, seller_street, f"{seller_postal} {seller_city}".strip()]
    col_contact = []
    if seller_phone:
        col_contact.append(f"Tel: {seller_phone}")
    if seller_email:
        col_contact.append(f"E-Mail: {seller_email}")
    if seller_tax_id:
        col_contact.append(f"USt-IdNr: {seller_tax_id}")
    D.draw_footer_columns(ops, line_ops, [col_address, col_contact, []])

    ops.append("ET")
    ops += line_ops

    full_pdf = build_single_page_pdf(ops, image=image)

    year = datetime.datetime.now().year
    pdf_dir = f"data/quotes/{year}"
    os.makedirs(pdf_dir, exist_ok=True)
    safe_number = "".join(c for c in quote_number if c.isalnum() or c in ['-', '_'])
    pdf_path = os.path.join(pdf_dir, f"Angebot_{safe_number}.pdf")

    try:
        with open(pdf_path, 'wb') as f:
            f.write(full_pdf)
        print(f"Quote PDF saved to: {pdf_path}")
        db.update_invoice_pdf_path(quote_id, pdf_path)
    except Exception as e:
        print(f"Error saving quote PDF: {e}")
        return None, None

    return full_pdf, pdf_path
