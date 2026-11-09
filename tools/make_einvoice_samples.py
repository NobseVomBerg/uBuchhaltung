# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Erzeugt Beispiel-E-Rechnungen aus den Demodaten (für CI-Validierung und Sichtprüfung).

Aufruf:  ``python tools/make_einvoice_samples.py <Ausgabeordner>``

Ergebnis im Ausgabeordner:

* ``zugferd-normalfall.pdf``            – hybride E-Rechnung, Demo-Rechnung mit 19 % USt
* ``zugferd-kleinunternehmer.pdf``      – § 19 UStG (Kategorie E mit Befreiungsgrund)
* ``xrechnung-normalfall.xml``          – UBL-XRechnung zur Normalfall-Rechnung
* ``xrechnung-kleinunternehmer.xml``    – UBL-XRechnung zum Kleinunternehmer-Fall

Alles entsteht in einem temporären Datenverzeichnis; echte Daten werden nie angefasst.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LEITWEG_ID = '991-12345-67'          # erfundene Beispiel-Leitweg-ID (XRechnung verlangt BT-10)


def _setup_demo():
    import demo
    import userctx
    tmp = tempfile.mkdtemp(prefix='ubuch-samples-')
    userctx.DATA_ROOT = os.path.join(tmp, 'data')
    userctx._mode_cache.clear()
    os.environ[demo.ENV_VAR] = '1'
    os.chdir(tmp)
    demo.prepare(log=lambda m: None)
    return tmp


def _clone_as_exempt(db, invoice_id):
    """Kopie einer Demo-Rechnung als Kleinunternehmer-Rechnung (§ 19 UStG)."""
    inv = db.get_invoice_by_id(invoice_id)
    items = db.get_invoice_items(invoice_id)
    net = sum(float(it[8]) for it in items)
    data = {
        'invoice_number': 'KU-DEMO-1', 'invoice_date': str(inv[2]),
        'own_company_id': inv[3], 'seller_name': inv[4], 'seller_company': inv[5],
        'seller_street': inv[6], 'seller_postal_code': inv[7], 'seller_city': inv[8],
        'seller_country': inv[9], 'seller_vat_id': inv[10], 'seller_email': inv[11],
        'seller_phone': inv[12], 'customer_id': inv[13], 'buyer_name': inv[14],
        'buyer_company': inv[15], 'buyer_street': inv[16], 'buyer_postal_code': inv[17],
        'buyer_city': inv[18], 'buyer_country': inv[19], 'buyer_route_id': LEITWEG_ID,
        'currency': inv[24], 'payment_terms': inv[26], 'payment_due_date': inv[27],
        'bank_name': inv[31], 'bank_iban': inv[32], 'bank_bic': inv[33],
        'tax_category': 'E', 'tax_rate': -1, 'sum_net': net, 'tax_amount': 0,
        'sum_gross': net, 'amount_due': net, 'status': 'finalized',
    }
    new_id = db.insert_invoice(data)
    for it in items:
        db.insert_invoice_item({
            'invoice_id': new_id, 'position': it[2], 'description': it[4], 'quantity': it[5],
            'unit': it[6], 'price_per_unit': it[7], 'total_net': it[8],
            'tax_category': 'E', 'tax_rate': 0})
    return new_id


def main(outdir):
    from db import Database
    from export.invoice_files import generate_invoice_documents, invoice_file_path
    from export.xrechnung_invoice import XRechnungGenerator

    os.makedirs(outdir, exist_ok=True)
    _setup_demo()
    db = Database()
    normal = next(i for i in db.fetch_invoices() if float(i[35] or 0) in (19, 0.19))
    with db._get_connection() as c:                      # nur in der temporären DB
        c.execute("UPDATE Invoices SET Status='draft', BuyerRouteID=? WHERE ID=?",
                  (LEITWEG_ID, normal[0]))
    exempt_id = _clone_as_exempt(db, normal[0])

    for label, iid in (('normalfall', normal[0]), ('kleinunternehmer', exempt_id)):
        result = generate_invoice_documents(db, iid, with_xml=False, overwrite_sent=True)
        if result['error']:
            sys.exit(f'{label}: {result["error"]}')
        shutil.copyfile(result['pdf_path'], os.path.join(outdir, f'zugferd-{label}.pdf'))
        inv, items = db.get_invoice_by_id(iid), db.get_invoice_items(iid)
        xml = XRechnungGenerator().generate_xml(inv, items, db=db)
        with open(os.path.join(outdir, f'xrechnung-{label}.xml'), 'w', encoding='utf-8') as f:
            f.write(xml)
    print('Beispiele in', outdir, sorted(os.listdir(outdir)))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'einvoice-samples')
