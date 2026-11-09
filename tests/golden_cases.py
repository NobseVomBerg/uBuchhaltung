# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Feste Rechnungsfälle für die Golden-Tests (UBL-XML, PDF-Text/Layout).

Referenzdateien liegen in ``tests/fixtures/golden/``. Neu erzeugen mit
``UPDATE_GOLDEN=1 python -m pytest tests/test_golden_invoice_outputs.py`` –
nur, wenn die Abweichung bewusst und begründet ist.
"""

_BASE = {
    'invoice_date': '2026-03-04',
    'seller_name': 'Anna Verkäufer',
    'seller_company': 'Muster & Söhne GmbH',
    'seller_street': 'Musterstraße 1',
    'seller_postal_code': '12345',
    'seller_city': 'Musterstadt',
    'seller_country': 'DE',
    'seller_vat_id': 'DE123456789',
    'seller_email': 'info@muster.de',
    'seller_phone': '+49 30 123456',
    'buyer_name': 'Max Mustermann',
    'buyer_company': 'Malermeister Pinsel',
    'buyer_street': 'Kundenweg 5',
    'buyer_postal_code': '54321',
    'buyer_city': 'Kundenhausen',
    'buyer_country': 'DE',
    'currency': 'EUR',
    'payment_terms': 'Zahlbar innerhalb von 14 Tagen.',
    'payment_due_date': '2026-03-18',
    'bank_name': 'Testbank AG',
    'bank_iban': 'DE89370400440532013000',
    'bank_bic': 'TESTDE1X',
    'tax_category': 'S',
    'status': 'finalized',
}

CASES = {
    'std19': dict(
        invoice={'invoice_number': 'G26001', 'tax_rate': 19.0,
                 'sum_net': 149.50, 'tax_amount': 28.41, 'sum_gross': 177.91,
                 'amount_due': 177.91, 'buyer_route_id': '04011000-1234512345-35',
                 'order_number': 'B-4711'},
        items=[('Anstrich Wohnzimmer (inkl. Material) & Anfahrt', 1, 'Stk.', 100.00, 100.00, 19.0),
               ('Zusatzleistung <Sonderfarbe>', 3, 'Std.', 16.50, 49.50, 19.0)]),
    'std7': dict(
        invoice={'invoice_number': 'G26002', 'tax_rate': 7.0,
                 'sum_net': 200.00, 'tax_amount': 14.00, 'sum_gross': 214.00,
                 'amount_due': 214.00},
        items=[('Fachbuch', 2, 'Stk.', 100.00, 200.00, 7.0)]),
    'ku': dict(
        invoice={'invoice_number': 'G26003', 'tax_rate': -1,
                 'sum_net': 100.00, 'tax_amount': 0.0, 'sum_gross': 100.00,
                 'amount_due': 100.00},
        items=[('Beratung', 1, 'Std.', 100.00, 100.00, 0.0)]),
}


def make_case(db, name):
    """Rechnung des Falls ``name`` anlegen; liefert die Rechnungs-ID."""
    case = CASES[name]
    iid = db.insert_invoice({**_BASE, **case['invoice']})
    for pos, (desc, qty, unit, price, total, rate) in enumerate(case['items'], 1):
        db.insert_invoice_item({
            'invoice_id': iid, 'position': pos, 'description': desc,
            'quantity': qty, 'unit': unit, 'price_per_unit': price,
            'total_net': total, 'tax_category': 'S', 'tax_rate': rate})
    return iid
