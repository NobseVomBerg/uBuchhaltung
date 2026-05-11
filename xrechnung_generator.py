"""
XRechnung XML generator (EN 16931 compliant)
Generates XML files for electronic invoicing according to German XRechnung standard
"""
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime


class XRechnungGenerator:
    """Generate XRechnung XML files according to EN 16931"""
    
    def __init__(self):
        self.namespaces = {
            'xmlns': 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
            'xmlns:cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
            'xmlns:cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
        }
    
    def generate_xml(self, invoice, invoice_items):
        """Generate XRechnung XML from invoice and items data
        
        Args:
            invoice: Invoice tuple from database
            invoice_items: List of invoice item tuples from database
            
        Returns:
            str: XML string
        """
        # Register namespaces
        for prefix, uri in self.namespaces.items():
            if prefix == 'xmlns':
                ET.register_namespace('', uri)
            else:
                ET.register_namespace(prefix.replace('xmlns:', ''), uri)
        
        # Create root element
        root = ET.Element('Invoice', attrib=self.namespaces)
        
        # CustomizationID (XRechnung version)
        ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}CustomizationID').text = \
            'urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0'
        
        # ProfileID (CIUS-ID)
        ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ProfileID').text = \
            'urn:fdc:peppol.eu:2017:poacc:billing:01:1.0'
        
        # ID (Invoice number)
        ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = \
            invoice[1] or 'DRAFT'
        
        # IssueDate
        issue_date = invoice[2] or datetime.now().strftime('%Y-%m-%d')
        ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}IssueDate').text = issue_date
        
        # DueDate (if available)
        if invoice[27]:  # PaymentDueDate
            ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}DueDate').text = invoice[27]
        
        # InvoiceTypeCode (380 = Commercial invoice)
        ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}InvoiceTypeCode').text = '380'
        
        # DocumentCurrencyCode
        currency = invoice[24] or 'EUR'
        ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}DocumentCurrencyCode').text = currency
        
        # BuyerReference (Leitweg-ID for B2G)
        if invoice[22]:  # BuyerRouteID
            ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}BuyerReference').text = invoice[22]
        
        # OrderReference (if available)
        if invoice[23]:  # OrderNumber
            order_ref = ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}OrderReference')
            ET.SubElement(order_ref, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = invoice[23]
        
        # AccountingSupplierParty (Seller)
        self._add_supplier_party(root, invoice)
        
        # AccountingCustomerParty (Buyer)
        self._add_customer_party(root, invoice)
        
        # PaymentMeans (if bank details available)
        if invoice[32]:  # BankIBAN
            self._add_payment_means(root, invoice)
        
        # PaymentTerms
        if invoice[26]:  # PaymentTerms
            payment_terms = ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PaymentTerms')
            ET.SubElement(payment_terms, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Note').text = invoice[26]
        
        # TaxTotal
        self._add_tax_total(root, invoice)
        
        # LegalMonetaryTotal
        self._add_monetary_total(root, invoice)
        
        # InvoiceLines
        for item in invoice_items:
            self._add_invoice_line(root, item, invoice[35])  # TaxRate
        
        # Convert to pretty XML string
        xml_string = ET.tostring(root, encoding='unicode')
        dom = minidom.parseString(xml_string)
        return dom.toprettyxml(indent='  ')
    
    def _add_supplier_party(self, root, invoice):
        """Add AccountingSupplierParty (Seller) to XML"""
        supplier = ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}AccountingSupplierParty')
        party = ET.SubElement(supplier, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Party')
        
        # PartyName
        party_name = ET.SubElement(party, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PartyName')
        ET.SubElement(party_name, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Name').text = invoice[5] or 'Seller'  # SellerCompany
        
        # PostalAddress
        postal = ET.SubElement(party, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PostalAddress')
        ET.SubElement(postal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}StreetName').text = invoice[6] or ''   # SellerStreet
        ET.SubElement(postal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}CityName').text = invoice[8] or ''      # SellerCity
        ET.SubElement(postal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}PostalZone').text = invoice[7] or ''    # SellerPostalCode
        
        country = ET.SubElement(postal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Country')
        ET.SubElement(country, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}IdentificationCode').text = invoice[9] or 'DE'  # SellerCountry
        
        # PartyTaxScheme (VAT)
        if invoice[10]:  # SellerVATID
            tax_scheme = ET.SubElement(party, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PartyTaxScheme')
            ET.SubElement(tax_scheme, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}CompanyID').text = invoice[10]  # SellerVATID
            
            scheme = ET.SubElement(tax_scheme, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxScheme')
            ET.SubElement(scheme, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = 'VAT'
        
        # PartyLegalEntity
        legal = ET.SubElement(party, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PartyLegalEntity')
        ET.SubElement(legal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}RegistrationName').text = invoice[5] or 'Seller'  # SellerCompany
    
    def _add_customer_party(self, root, invoice):
        """Add AccountingCustomerParty (Buyer) to XML"""
        customer = ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}AccountingCustomerParty')
        party = ET.SubElement(customer, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Party')
        
        # PartyName
        party_name = ET.SubElement(party, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PartyName')
        ET.SubElement(party_name, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Name').text = invoice[15] or 'Buyer'  # BuyerCompany
        
        # PostalAddress
        postal = ET.SubElement(party, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PostalAddress')
        ET.SubElement(postal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}StreetName').text = invoice[16] or ''   # BuyerStreet
        ET.SubElement(postal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}CityName').text = invoice[18] or ''     # BuyerCity
        ET.SubElement(postal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}PostalZone').text = invoice[17] or ''   # BuyerPostalCode
        
        country = ET.SubElement(postal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Country')
        ET.SubElement(country, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}IdentificationCode').text = invoice[19] or 'DE'  # BuyerCountry
        
        # PartyLegalEntity
        legal = ET.SubElement(party, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PartyLegalEntity')
        ET.SubElement(legal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}RegistrationName').text = invoice[15] or 'Buyer'  # BuyerCompany
    
    def _add_payment_means(self, root, invoice):
        """Add PaymentMeans to XML"""
        payment = ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PaymentMeans')
        ET.SubElement(payment, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}PaymentMeansCode').text = '58'  # SEPA transfer
        
        # PayeeFinancialAccount
        account = ET.SubElement(payment, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PayeeFinancialAccount')
        ET.SubElement(account, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = invoice[32]        # BankIBAN
        ET.SubElement(account, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Name').text = invoice[31] or ''  # BankName
        
        # FinancialInstitutionBranch (BIC)
        if invoice[33]:  # BankBIC
            branch = ET.SubElement(account, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}FinancialInstitutionBranch')
            ET.SubElement(branch, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = invoice[33]
    
    def _add_tax_total(self, root, invoice):
        """Add TaxTotal to XML"""
        tax_total = ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxTotal')
        ET.SubElement(tax_total, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxAmount', 
                     attrib={'currencyID': invoice[24] or 'EUR'}).text = f"{invoice[37]:.2f}"  # TaxAmount
        
        # TaxSubtotal
        subtotal = ET.SubElement(tax_total, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxSubtotal')
        ET.SubElement(subtotal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxableAmount',
                     attrib={'currencyID': invoice[24] or 'EUR'}).text = f"{invoice[36]:.2f}"  # SumNet
        ET.SubElement(subtotal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxAmount',
                     attrib={'currencyID': invoice[24] or 'EUR'}).text = f"{invoice[37]:.2f}"  # TaxAmount
        
        # TaxCategory
        category = ET.SubElement(subtotal, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxCategory')
        ET.SubElement(category, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = invoice[34] or 'S'   # TaxCategory
        ET.SubElement(category, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Percent').text = f"{invoice[35]:.1f}"  # TaxRate
        
        scheme = ET.SubElement(category, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxScheme')
        ET.SubElement(scheme, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = 'VAT'
    
    def _add_monetary_total(self, root, invoice):
        """Add LegalMonetaryTotal to XML"""
        monetary = ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}LegalMonetaryTotal')
        currency = invoice[24] or 'EUR'
        
        ET.SubElement(monetary, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}LineExtensionAmount',
                     attrib={'currencyID': currency}).text = f"{invoice[36]:.2f}"  # SumNet
        ET.SubElement(monetary, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxExclusiveAmount',
                     attrib={'currencyID': currency}).text = f"{invoice[36]:.2f}"  # SumNet
        ET.SubElement(monetary, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxInclusiveAmount',
                     attrib={'currencyID': currency}).text = f"{invoice[38]:.2f}"  # SumGross
        ET.SubElement(monetary, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}PayableAmount',
                     attrib={'currencyID': currency}).text = f"{invoice[39] or invoice[38]:.2f}"  # AmountDue
    
    def _add_invoice_line(self, root, item, default_tax_rate):
        """Add InvoiceLine to XML"""
        line = ET.SubElement(root, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}InvoiceLine')
        
        # ID (Position)
        ET.SubElement(line, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = str(item[2])  # Position
        
        # InvoicedQuantity
        ET.SubElement(line, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}InvoicedQuantity',
                     attrib={'unitCode': item[6] or 'C62'}).text = f"{item[5]:.2f}"  # Unit, Quantity
        
        # LineExtensionAmount
        ET.SubElement(line, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}LineExtensionAmount',
                     attrib={'currencyID': 'EUR'}).text = f"{item[8]:.2f}"  # TotalNet
        
        # Item
        item_elem = ET.SubElement(line, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Item')
        ET.SubElement(item_elem, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Name').text = item[4] or 'Item'  # Description
        
        # ClassifiedTaxCategory
        tax_cat = ET.SubElement(item_elem, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}ClassifiedTaxCategory')
        ET.SubElement(tax_cat, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = item[9] or 'S'   # TaxCategory
        ET.SubElement(tax_cat, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Percent').text = f"{item[10] or default_tax_rate:.1f}"  # TaxRate
        
        scheme = ET.SubElement(tax_cat, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxScheme')
        ET.SubElement(scheme, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID').text = 'VAT'
        
        # Price
        price_elem = ET.SubElement(line, '{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Price')
        ET.SubElement(price_elem, '{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}PriceAmount',
                     attrib={'currencyID': 'EUR'}).text = f"{item[7]:.2f}"  # PricePerUnit
