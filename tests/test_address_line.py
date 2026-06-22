"""Regression: Adress-Zusatzzeile (address_line1) in Rechnung/Angebot.

- Kundenanschrift: Zusatzzeile MUSS erscheinen (on-screen Vorschau).
- Eigene Anschrift: Absenderzeile OHNE Zusatzzeile, Footer MIT Zusatzzeile.
"""
from server.pages_invoice import PageInvoice
from server.pages_quote import PageQuote


ZUSATZ_KUNDE = 'z.Hd. Frau Beispiel'
ZUSATZ_EIGEN = 'Gebäude B, 2. OG'


def _seed_contacts(db):
    db.insert_contact(contact_type='own', entity_type='company',
                      display_name='Meine Firma', company_name='Meine Firma GmbH',
                      address_line1=ZUSATZ_EIGEN, street='Hauptstr. 1',
                      postal_code='10115', city='Berlin')
    db.insert_contact(contact_type='customer', entity_type='company',
                      display_name='Kunde AG', company_name='Kunde AG',
                      address_line1=ZUSATZ_KUNDE, street='Kundenweg 5',
                      postal_code='20095', city='Hamburg')


def test_invoice_customer_zusatzzeile_in_preview(db_with_coa):
    _seed_contacts(db_with_coa)
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    # Daten der Kundenanschrift werden eingebettet ...
    assert ZUSATZ_KUNDE in html
    # ... und von der Adress-Vorschau auch ausgegeben.
    assert 'customer.address_line1' in html


def test_invoice_own_footer_includes_sender_excludes(db_with_coa):
    _seed_contacts(db_with_coa)
    html = PageInvoice(db_with_coa, filters={}, invoice_id=None)
    # Footer (eigene Anschrift) nimmt die Zusatzzeile auf ...
    assert 'company.address_line1' in html
    assert "if (company.address_line1) addressHtml += company.address_line1" in html
    # ... die Absenderzeile dagegen nicht (Reihenfolge displayName -> street).
    assert "senderLine.textContent = displayName + ' · ' + company.street" in html


def test_quote_customer_zusatzzeile_in_preview(db_with_coa):
    _seed_contacts(db_with_coa)
    html = PageQuote(db_with_coa)
    assert ZUSATZ_KUNDE in html
    assert 'c.address_line1' in html
    # Absenderzeile des Angebots bleibt ohne Zusatzzeile.
    assert "sender.textContent = dn + ' · ' + c.street" in html
