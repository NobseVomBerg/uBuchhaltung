# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Erzeugt Beispiel-E-Rechnungen aus den Demodaten (für CI-Validierung und Sichtprüfung).

Aufruf:  ``python tools/make_einvoice_samples.py <Ausgabeordner>``

Ergebnis im Ausgabeordner:

* ``zugferd-normalfall.pdf``            – hybride E-Rechnung, Demo-Rechnung mit 19 % USt
* ``zugferd-kleinunternehmer.pdf``      – § 19 UStG (Kategorie E mit Befreiungsgrund)
* ``zugferd-berichtigt.pdf``             – berichtigte Fassung einer versendeten Rechnung (Leistungsdatum
                                         geändert): mit Hinweis als BT-22 (CII) und „Fassung: 2“ im PDF
* ``zugferd-storno.pdf``                 – Stornorechnung zu einer versendeten Rechnung: Gutschrift (Typ 381) mit
                                         Bezug auf die Ursprungsrechnung (BT-25/BT-26) und Hinweis als BT-22
* ``zugferd-ersatz.pdf``                 – Ersatzrechnung zu einer stornierten Rechnung (Rechnung versenden, stornieren,
                                         neu ausstellen, versenden): Typ 380 mit dem Hinweis „Ersetzt die stornierte
                                         Rechnung … (Stornorechnung …)“ als BT-22
* ``xrechnung-normalfall.xml``          – UBL-XRechnung zur Normalfall-Rechnung
* ``xrechnung-kleinunternehmer.xml``    – UBL-XRechnung zum Kleinunternehmer-Fall
* ``xrechnung-berichtigt.xml``          – UBL-XRechnung zur berichtigten Fassung (``cbc:Note``)
* ``xrechnung-storno.xml``              – UBL-``CreditNote`` zur Stornorechnung (``cac:BillingReference``)
* ``xrechnung-ersatz.xml``              – UBL-XRechnung zur Ersatzrechnung (``cbc:Note``)

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
    userctx.set_user(demo.USERNAME)          # Database() und Dateiablage zeigen auf data/users/demo/
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
        'currency': inv[24], 'delivery_date': inv[25] or str(inv[2]), 'payment_terms': inv[26], 'payment_due_date': inv[27],
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


def _clone_for_correction(db, invoice_id, number='BE-DEMO-1'):
    """Kopie einer Demo-Rechnung (19 % USt, Leitweg-ID) als Entwurf für den Berichtigen- und den Storno-Fall."""
    inv = db.get_invoice_by_id(invoice_id)
    items = db.get_invoice_items(invoice_id)
    data = {
        'invoice_number': number, 'invoice_date': str(inv[2]),
        'own_company_id': inv[3], 'seller_name': inv[4], 'seller_company': inv[5],
        'seller_street': inv[6], 'seller_postal_code': inv[7], 'seller_city': inv[8],
        'seller_country': inv[9], 'seller_vat_id': inv[10], 'seller_email': inv[11],
        'seller_phone': inv[12], 'customer_id': inv[13], 'buyer_name': inv[14],
        'buyer_company': inv[15], 'buyer_street': inv[16], 'buyer_postal_code': inv[17],
        'buyer_city': inv[18], 'buyer_country': inv[19], 'buyer_route_id': LEITWEG_ID,
        'currency': inv[24], 'delivery_date': inv[25] or str(inv[2]), 'payment_terms': inv[26],
        'payment_due_date': inv[27], 'bank_name': inv[31], 'bank_iban': inv[32], 'bank_bic': inv[33],
        'tax_category': inv[34], 'tax_rate': inv[35], 'sum_net': inv[36], 'tax_amount': inv[37],
        'sum_gross': inv[38], 'amount_due': inv[38], 'status': 'finalized',
    }
    new_id = db.insert_invoice(data)
    for it in items:
        db.insert_invoice_item({
            'invoice_id': new_id, 'position': it[2], 'description': it[4], 'quantity': it[5],
            'unit': it[6], 'price_per_unit': it[7], 'total_net': it[8],
            'tax_category': it[9], 'tax_rate': it[10]})
    return new_id


def _correct_delivery_date(db, invoice_id):
    """Die Rechnung versenden (Fassung 1 entsteht) und das Leistungsdatum berichtigen: Fassung 2 trägt den
    Hinweis. Genau diesen Weg geht die Oberfläche (Status „versendet“, danach Speichern als Berichtigung)."""
    from export.invoice_files import generate_invoice_documents
    change = db.set_invoice_status(invoice_id, 'sent', 'demo')
    if not change.ok:
        sys.exit(f'berichtigt: {change.message}')
    first = generate_invoice_documents(db, invoice_id, overwrite_sent=True)
    if first['error']:
        sys.exit(f'berichtigt (Fassung 1): {first["error"]}')
    delivery = db.get_invoice_by_id(invoice_id)[25]
    corrected = db.correct_invoice(invoice_id, {'delivery_date': _day_before(delivery)}, 1, 'demo')
    if not corrected.corrected or corrected.version != 2:
        sys.exit('berichtigt: die Berichtigung hat keine Fassung 2 angelegt')
    second = generate_invoice_documents(db, invoice_id, overwrite_sent=True)
    if second['error']:
        sys.exit(f'berichtigt (Fassung 2): {second["error"]}')
    return second


def _cancel_sample(db, invoice_id):
    """Die Rechnung versenden (ihr Dokument entsteht) und stornieren: Es entsteht die Stornorechnung – genau der Weg
    der Oberfläche (Status „versendet“, danach „Stornieren“). Liefert das Ergebnis von ``generate_invoice_documents``
    für die Stornorechnung und deren ID."""
    import datetime
    from export.invoice_files import generate_invoice_documents
    change = db.set_invoice_status(invoice_id, 'sent', 'demo')
    if not change.ok:
        sys.exit(f'storno: {change.message}')
    original = generate_invoice_documents(db, invoice_id, overwrite_sent=True)
    if original['error']:
        sys.exit(f'storno (Ursprungsrechnung): {original["error"]}')
    storno = db.cancel_invoice(invoice_id, datetime.date.today().isoformat(), 'demo')
    if not storno.ok or not storno.counter_booked:
        sys.exit(f'storno: {storno.message or "keine Gegenbuchung"}')
    result = generate_invoice_documents(db, storno.storno_id, overwrite_sent=True)
    if result['error']:
        sys.exit(f'storno (Stornorechnung): {result["error"]}')
    return result, storno.storno_id


def _replacement_sample(db, invoice_id):
    """Die Rechnung versenden, stornieren, neu ausstellen und die neue Rechnung versenden – genau der Weg der
    Oberfläche („Stornieren und neu ausstellen“, danach „Versenden“). Liefert das Ergebnis von
    ``generate_invoice_documents`` für die Ersatzrechnung und deren ID."""
    import datetime
    from export.invoice_files import generate_invoice_documents
    change = db.set_invoice_status(invoice_id, 'sent', 'demo')
    if not change.ok:
        sys.exit(f'ersatz: {change.message}')
    original = generate_invoice_documents(db, invoice_id, overwrite_sent=True)
    if original['error']:
        sys.exit(f'ersatz (stornierte Rechnung): {original["error"]}')
    storno = db.cancel_invoice(invoice_id, datetime.date.today().isoformat(), 'demo')
    if not storno.ok:
        sys.exit(f'ersatz (Stornierung): {storno.message}')
    draft = db.create_replacement_draft(invoice_id, 'demo')
    if not draft.ok:
        sys.exit(f'ersatz (neu ausstellen): {draft.message}')
    change = db.set_invoice_status(draft.invoice_id, 'sent', 'demo')
    if not change.ok:
        sys.exit(f'ersatz (Versand): {change.message}')
    result = generate_invoice_documents(db, draft.invoice_id, overwrite_sent=True)
    if result['error']:
        sys.exit(f'ersatz (Ersatzrechnung): {result["error"]}')
    return result, draft.invoice_id


def _day_before(iso_date):
    import datetime
    return (datetime.date.fromisoformat(str(iso_date)[:10]) - datetime.timedelta(days=1)).isoformat()


def main(outdir):
    from db import Database
    from export.invoice_files import generate_invoice_documents, invoice_file_path
    from export.xrechnung_invoice import XRechnungGenerator

    outdir = os.path.abspath(outdir)          # _setup_demo wechselt das Arbeitsverzeichnis
    os.makedirs(outdir, exist_ok=True)
    _setup_demo()
    db = Database()
    normal = next(i for i in db.fetch_invoices() if float(i[35] or 0) in (19, 0.19) and i[50] != 'storno')
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
    corrected_id = _clone_for_correction(db, normal[0])
    result = _correct_delivery_date(db, corrected_id)
    shutil.copyfile(result['pdf_path'], os.path.join(outdir, 'zugferd-berichtigt.pdf'))
    inv, items = db.get_invoice_by_id(corrected_id), db.get_invoice_items(corrected_id)
    with open(os.path.join(outdir, 'xrechnung-berichtigt.xml'), 'w', encoding='utf-8') as f:
        f.write(XRechnungGenerator().generate_xml(inv, items, db=db))
    storno_source = _clone_for_correction(db, normal[0], 'ST-DEMO-1')
    result, storno_id = _cancel_sample(db, storno_source)
    shutil.copyfile(result['pdf_path'], os.path.join(outdir, 'zugferd-storno.pdf'))
    inv, items = db.get_invoice_by_id(storno_id), db.get_invoice_items(storno_id)
    with open(os.path.join(outdir, 'xrechnung-storno.xml'), 'w', encoding='utf-8') as f:
        f.write(XRechnungGenerator().generate_xml(inv, items, db=db))
    replace_source = _clone_for_correction(db, normal[0], 'ER-DEMO-1')
    result, replacement_id = _replacement_sample(db, replace_source)
    shutil.copyfile(result['pdf_path'], os.path.join(outdir, 'zugferd-ersatz.pdf'))
    inv, items = db.get_invoice_by_id(replacement_id), db.get_invoice_items(replacement_id)
    with open(os.path.join(outdir, 'xrechnung-ersatz.xml'), 'w', encoding='utf-8') as f:
        f.write(XRechnungGenerator().generate_xml(inv, items, db=db))
    print('Beispiele in', outdir, sorted(os.listdir(outdir)))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'einvoice-samples')
