"""
PDF Invoice Generator
Generates PDF invoices directly from database data
"""
import datetime
import os
from db import Database


def generate_invoice_pdf(db: Database, invoice_id: int):
    """Generate PDF for an existing invoice from database
    
    Args:
        db: Database instance
        invoice_id: ID of the invoice to generate PDF for
        
    Returns:
        tuple: (pdf_bytes, pdf_path) or (None, None) if invoice not found
    """
    # Load invoice from database
    invoice = db.get_invoice_by_id(invoice_id)
    if not invoice:
        print(f"Invoice {invoice_id} not found")
        return None, None
    
    # Load invoice items
    invoice_items = db.get_invoice_items(invoice_id)
    if not invoice_items:
        print(f"No items found for invoice {invoice_id}")
        return None, None
    
    # Extract invoice data
    # Invoice structure: ID=0, InvoiceNumber=1, InvoiceDate=2, OwnCompanyId=3, 
    # SellerName=4, SellerCompany=5, SellerStreet=6, SellerPostal=7, SellerCity=8, 
    # SellerCountry=9, SellerEmail=10, SellerPhone=11, CustomerId=12, BuyerName=13, 
    # BuyerCompany=14, BuyerStreet=15, BuyerPostal=16, BuyerCity=17, BuyerCountry=18, 
    # BuyerEmail=19, BuyerRouteId=20, OrderNumber=21, Currency=22, DeliveryDate=23, 
    # PaymentTerms=24, PaymentDueDate=25, SkontoDays=26, SkontoPercent=27, 
    # BankAccountId=28, BankName=29, BankIBAN=30, BankBIC=31, TaxCategory=32, 
    # TaxRate=33, SumNet=34, TaxAmount=35, SumGross=36, AmountDue=37, Status=38, PDFPath=39
    
    invoice_number = invoice[1] or 'DRAFT'
    invoice_date = invoice[2] or datetime.datetime.now().strftime('%Y-%m-%d')
    
    # Seller (own company) data
    seller_name = invoice[4] or ''
    seller_company = invoice[5] or ''
    seller_street = invoice[6] or ''
    seller_postal = invoice[7] or ''
    seller_city = invoice[8] or ''
    seller_email = invoice[10] or ''
    seller_phone = invoice[11] or ''
    seller_tax_id = ''  # Need to load from contacts table if needed
    
    # Buyer (customer) data
    buyer_name = invoice[13] or ''
    buyer_company = invoice[14] or ''
    buyer_street = invoice[15] or ''
    buyer_postal = invoice[16] or ''
    buyer_city = invoice[17] or ''
    
    # Bank details
    bank_name = invoice[29] or ''
    bank_iban = invoice[30] or ''
    bank_bic = invoice[31] or ''
    
    # Amounts
    tax_rate = invoice[33] or 0.19
    sum_net = invoice[34] or 0
    tax_amount = invoice[35] or 0
    sum_gross = invoice[36] or 0
    
    # Payment terms
    payment_terms = invoice[24] or 'Bitte überweisen Sie den Gesamtbetrag ohne jeden Abzug unter Angabe der Rechnungsnummer innerhalb von 14 Tagen ab Rechnungsdatum auf das unten angegebene Konto. Vielen Dank.'
    
    # Load own company logo if available
    logo_data = None
    logo_width = 0
    logo_height = 0
    logo_filter = '/FlateDecode'
    
    own_company_id = invoice[3]
    if own_company_id:
        own_contact = db.get_contact_by_id(own_company_id)
        if own_contact and len(own_contact) > 13 and own_contact[13]:
            logo_path = own_contact[13]
            if os.path.exists(logo_path):
                try:
                    from PIL import Image
                    import zlib
                    
                    print(f"Loading logo from: {logo_path}")
                    img = Image.open(logo_path)
                    
                    # Resize if too large
                    max_width = 150
                    max_height = 80
                    if img.width > max_width or img.height > max_height:
                        img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
                    
                    # Convert to RGB if necessary
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    
                    logo_width = img.width
                    logo_height = img.height
                    
                    # Get raw RGB data
                    rgb_data = img.tobytes()
                    
                    # Compress with zlib
                    logo_data = zlib.compress(rgb_data)
                    logo_filter = '/FlateDecode'
                    
                    print(f"Logo loaded: {logo_width}x{logo_height}, {len(logo_data)} bytes compressed")
                except Exception as e:
                    print(f"Error loading logo: {e}")
            else:
                print(f"Logo file not found: {logo_path}")
        
        # Also load tax ID from own contact
        if own_contact and len(own_contact) > 11:
            seller_tax_id = own_contact[11] or ''
    
    # Build PDF content
    # Start with PDF objects
    objects = []
    obj_offsets = []
    binary_objects = {}
    
    def pdf_obj(num, content):
        return f"{num} 0 obj\n{content}\nendobj\n"
    
    def pdf_obj_binary(num, header, data):
        """Create a PDF object with binary data"""
        obj_header = f"{num} 0 obj\n{header}\nstream\n".encode('latin-1')
        obj_footer = b"\nendstream\nendobj\n"
        return obj_header + data + obj_footer
    
    # Object 1: Catalog
    objects.append(pdf_obj(1, "<< /Type /Catalog /Pages 2 0 R >>"))
    
    # Object 2: Pages
    objects.append(pdf_obj(2, "<< /Type /Pages /Kids [8 0 R] /Count 1 >>"))
    
    # Object 3: Font Helvetica
    objects.append(pdf_obj(3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    
    # Object 4: Font Helvetica-Bold
    objects.append(pdf_obj(4, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"))
    
    # Object 5: Font Helvetica-Oblique
    objects.append(pdf_obj(5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>"))
    
    # Object 6: Logo image (if available)
    xobject_ref = ""
    if logo_data:
        objects.append(None)  # Placeholder for image object
        binary_objects[5] = (None, logo_data)  # Store as tuple: (header, data)
        xobject_ref = "/XObject << /Logo 6 0 R >>"
    
    # Object 7: Page content stream
    content_stream = []
    content_stream.append("BT")
    
    # Logo (if available)
    if logo_data:
        # Position logo at top-right: x=430, y=750
        content_stream.append(f"q {logo_width} 0 0 {logo_height} 430 750 cm /Logo Do Q")
    
    # Invoice title
    content_stream.append("/F2 24 Tf")
    content_stream.append("50 750 Td")
    content_stream.append("(Rechnung) Tj")
    
    # Invoice number and date
    content_stream.append("/F1 10 Tf")
    content_stream.append("0 -20 Td")
    content_stream.append(f"(Rechnungs-Nr.: {invoice_number}) Tj")
    content_stream.append("0 -15 Td")
    content_stream.append(f"(Datum: {invoice_date}) Tj")
    
    # Seller address (top-left)
    content_stream.append("0 -40 Td")
    content_stream.append("/F3 8 Tf")
    seller_line = f"{seller_company or seller_name} · {seller_street} · {seller_postal} {seller_city}"
    content_stream.append(f"({seller_line}) Tj")
    
    # Buyer address
    content_stream.append("0 -25 Td")
    content_stream.append("/F1 10 Tf")
    if buyer_company:
        content_stream.append(f"({buyer_company}) Tj")
        content_stream.append("0 -15 Td")
    if buyer_name:
        content_stream.append(f"({buyer_name}) Tj")
        content_stream.append("0 -15 Td")
    if buyer_street:
        content_stream.append(f"({buyer_street}) Tj")
        content_stream.append("0 -15 Td")
    if buyer_postal or buyer_city:
        content_stream.append(f"({buyer_postal} {buyer_city}) Tj")
        content_stream.append("0 -15 Td")
    
    # Invoice items table
    content_stream.append("0 -30 Td")
    content_stream.append("/F2 10 Tf")
    content_stream.append("(Pos.   Menge   Einheit   Bezeichnung                                 Einzelpreis   Gesamt) Tj")
    content_stream.append("0 -15 Td")
    content_stream.append("/F1 10 Tf")
    
    # Add items
    for item in invoice_items:
        # item: ID=0, InvoiceId=1, Position=2, ArticleId=3, Description=4, 
        # Quantity=5, Unit=6, PricePerUnit=7, TotalNet=8, TaxCategory=9, TaxRate=10
        pos = item[2] or 1
        quantity = item[5] or 0
        unit = item[6] or 'Stk.'
        description = item[4] or ''
        price = item[7] or 0
        total = item[8] or 0
        
        # Truncate description if too long
        if len(description) > 35:
            description = description[:32] + '...'
        
        item_line = f"{pos}      {quantity:.2f}   {unit:8s}  {description:35s}  {price:8.2f} EUR  {total:8.2f} EUR"
        content_stream.append(f"({item_line}) Tj")
        content_stream.append("0 -15 Td")
    
    # Totals
    content_stream.append("0 -10 Td")
    content_stream.append(f"(Summe netto:                                                           {sum_net:8.2f} EUR) Tj")
    content_stream.append("0 -15 Td")
    tax_percent = int(tax_rate * 100)
    content_stream.append(f"(Mehrwertsteuer {tax_percent}%:                                                       {tax_amount:8.2f} EUR) Tj")
    content_stream.append("0 -15 Td")
    content_stream.append("/F2 10 Tf")
    content_stream.append(f"(Gesamtbetrag:                                                          {sum_gross:8.2f} EUR) Tj")
    
    # Payment terms
    content_stream.append("0 -30 Td")
    content_stream.append("/F1 10 Tf")
    # Split payment terms into lines (simple word wrap)
    words = payment_terms.split()
    line = ""
    for word in words:
        if len(line + word) > 80:
            content_stream.append(f"({line}) Tj")
            content_stream.append("0 -15 Td")
            line = word + " "
        else:
            line += word + " "
    if line:
        content_stream.append(f"({line.strip()}) Tj")
    
    # Bank details
    content_stream.append("0 -30 Td")
    content_stream.append("/F2 10 Tf")
    content_stream.append("(Bankverbindung:) Tj")
    content_stream.append("0 -15 Td")
    content_stream.append("/F1 10 Tf")
    if bank_name:
        content_stream.append(f"(Bank: {bank_name}) Tj")
        content_stream.append("0 -15 Td")
    if bank_iban:
        content_stream.append(f"(IBAN: {bank_iban}) Tj")
        content_stream.append("0 -15 Td")
    if bank_bic:
        content_stream.append(f"(BIC: {bank_bic}) Tj")
    
    # Footer with company details
    content_stream.append("0 -50 Td")
    content_stream.append("/F3 8 Tf")
    footer_line1 = f"{seller_company or seller_name}    {seller_street}    {seller_postal} {seller_city}"
    content_stream.append(f"({footer_line1}) Tj")
    content_stream.append("0 -12 Td")
    footer_line2 = f"Tel: {seller_phone}    E-Mail: {seller_email}"
    if seller_tax_id:
        footer_line2 += f"    UStIdNr: {seller_tax_id}"
    content_stream.append(f"({footer_line2}) Tj")
    
    content_stream.append("ET")
    
    # Join content stream
    content_stream_data = "\n".join(content_stream)
    content_stream_bytes = content_stream_data.encode('latin-1', errors='replace')
    
    # Store as binary object
    import zlib
    compressed_content = zlib.compress(content_stream_bytes)
    stream_header = f"<< /Length {len(compressed_content)} /Filter /FlateDecode >>\nstream\n"
    stream_footer = b"\nendstream"
    binary_objects[6] = stream_header.encode('latin-1') + compressed_content + stream_footer
    
    objects.append(None)  # Placeholder for content stream
    
    # Object 8: Page
    resources = f"<< /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> {xobject_ref}>>"
    objects.append(pdf_obj(8, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 7 0 R /Resources {resources} >>"))
    
    # Build PDF
    pdf_header = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    
    # Build body, handling binary objects
    pdf_body_parts = []
    for i, obj in enumerate(objects):
        obj_num = i + 1
        if i in binary_objects:
            bin_entry = binary_objects[i]
            if isinstance(bin_entry, tuple):
                # Image XObject: (header, data)
                header, bin_data = bin_entry
                pdf_body_parts.append(pdf_obj_binary(obj_num, f"<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter {logo_filter} /Length {len(bin_data)} >>", bin_data))
            else:
                # Content stream: already formatted bytes
                obj_header = f"{obj_num} 0 obj\n".encode('latin-1')
                obj_footer = b"\nendobj\n"
                pdf_body_parts.append(obj_header + bin_entry + obj_footer)
        elif obj is not None:
            pdf_body_parts.append(obj.encode('latin-1', errors='replace'))
    
    pdf_body = b"".join(pdf_body_parts)
    
    # Calculate xref offsets
    offset = len(pdf_header.encode('latin-1'))
    for i, part in enumerate(pdf_body_parts):
        obj_offsets.append(offset)
        offset += len(part)
    
    # Build xref table
    xref_offset = len(pdf_header.encode('latin-1')) + len(pdf_body)
    xref = f"xref\n0 {len(objects) + 1}\n"
    xref += "0000000000 65535 f \n"
    for off in obj_offsets:
        xref += f"{off:010d} 00000 n \n"
    
    # Trailer
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF"
    
    # Combine PDF
    full_pdf = pdf_header.encode('latin-1') + pdf_body + xref.encode('latin-1') + trailer.encode('latin-1')
    
    # Generate PDF path
    current_year = datetime.datetime.now().year
    pdf_dir = f"data/invoices/{current_year}"
    os.makedirs(pdf_dir, exist_ok=True)
    
    # Sanitize filename
    safe_number = "".join(c for c in invoice_number if c.isalnum() or c in ['-', '_'])
    pdf_filename = f"Rechnung_{safe_number}.pdf"
    pdf_path = os.path.join(pdf_dir, pdf_filename)
    
    # Save PDF file
    try:
        with open(pdf_path, 'wb') as f:
            f.write(full_pdf)
        print(f"PDF saved to: {pdf_path}")
        
        # Update invoice PDFPath in database
        db.update_invoice_pdf_path(invoice_id, pdf_path)
        
    except Exception as e:
        print(f"Error saving PDF file: {e}")
        return None, None
    
    return full_pdf, pdf_path
