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
* ``zugferd-skonto.pdf``                – Rechnung mit Skonto: die Zeile ``#SKONTO#TAGE=…#PROZENT=…#`` und der Freitext
                                         in den Zahlungsbedingungen (BT-20), der Absatz im PDF
* ``zugferd-steuernummer.pdf``          – Verkäufer nur mit Steuernummer, ohne USt-IdNr.: BT-32 (``FC``) und – weil BR-CO-26
                                         eine Verkäuferkennung verlangt – dieselbe Nummer als BT-29 (``ram:SellerTradeParty/ram:ID``)
* ``zugferd-steuernummer-kleinunternehmer.pdf`` – dasselbe als Kleinunternehmer (§ 19 UStG, Kategorie E)
* ``zugferd-zahlungshinweis-raute.pdf`` – ein ``#`` im Zahlungshinweis (neben dem Skonto): das UBL entsteht nicht (BR-DE-18, die
                                         Lücke der App), die eingebettete CII bleibt gültig – deshalb nur das PDF, kein XML
* ``zugferd-mischsatz.pdf``              – Rechnung mit Steuersatz je Position (19 % und 7 %), angelegt über den Weg der Maske
                                         (``taxMode: 'mixed'``): zwei Steueraufschlüsselungen (BG-23, die Steuer je Satz aus der
                                         Summe der Nettobeträge der Gruppe, BR-CO-17), im PDF die Spalte „USt“ und eine Steuerzeile je Satz
* ``xrechnung-normalfall.xml``          – UBL-XRechnung zur Normalfall-Rechnung
* ``xrechnung-kleinunternehmer.xml``    – UBL-XRechnung zum Kleinunternehmer-Fall
* ``xrechnung-berichtigt.xml``          – UBL-XRechnung zur berichtigten Fassung (``cbc:Note``)
* ``xrechnung-storno.xml``              – UBL-``CreditNote`` zur Stornorechnung (``cac:BillingReference``)
* ``xrechnung-ersatz.xml``              – UBL-XRechnung zur Ersatzrechnung (``cbc:Note``)
* ``xrechnung-skonto.xml``              – UBL-XRechnung mit der Skonto-Zeile (BR-DE-18) und ihrem Zeilenumbruch
* ``xrechnung-steuernummer.xml``        – UBL-XRechnung zum Fall „nur Steuernummer“ (``TAX`` und ``cac:PartyIdentification``)
* ``xrechnung-steuernummer-kleinunternehmer.xml`` – dasselbe als Kleinunternehmer
* ``xrechnung-mischsatz.xml``           – UBL-XRechnung zur Rechnung mit Steuersatz je Position (zwei ``cac:TaxSubtotal``)

Jedem Beispiel mit UBL wird vor dem Schreiben nachgerechnet, dass ihm nichts fehlt (``einvoice_gaps``): Sonst bricht das
Werkzeug ab – die Prüfung der App und Mustang sollen übereinstimmen.

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


def _clone_as_exempt(db, invoice_id, number='KU-DEMO-1', **over):
    """Kopie einer Demo-Rechnung als Kleinunternehmer-Rechnung (§ 19 UStG); ``over`` überschreibt Felder von
    ``insert_invoice`` (z. B. die Steuernummer des Verkäufers)."""
    inv = db.get_invoice_by_id(invoice_id)
    items = db.get_invoice_items(invoice_id)
    net = sum(float(it[8]) for it in items)
    data = {
        'invoice_number': number, 'invoice_date': str(inv[2]),
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
    data.update(over)
    new_id = db.insert_invoice(data)
    for it in items:
        db.insert_invoice_item({
            'invoice_id': new_id, 'position': it[2], 'description': it[4], 'quantity': it[5],
            'unit': it[6], 'price_per_unit': it[7], 'total_net': it[8],
            'tax_category': 'E', 'tax_rate': 0})
    return new_id


def _clone_for_correction(db, invoice_id, number='BE-DEMO-1', **over):
    """Kopie einer Demo-Rechnung (19 % USt, Leitweg-ID) als Entwurf für den Berichtigen- und den Storno-Fall;
    ``over`` überschreibt Felder von ``insert_invoice`` (z. B. das Skonto). Das Kennzeichen „Steuersatz je Position“ geht
    mit den Sätzen der Positionen mit (nur ``None`` oder ``1``): Sonst entstünde ein Beleg mit zwei Sätzen ohne Kennzeichen."""
    from db.invoices import InvoiceCol, has_mixed_rates
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
        'mixed_tax_rates': 1 if has_mixed_rates(inv[InvoiceCol.MIXED_TAX_RATES]) else None,
    }
    data.update(over)
    new_id = db.insert_invoice(data)
    for it in items:
        db.insert_invoice_item({
            'invoice_id': new_id, 'position': it[2], 'description': it[4], 'quantity': it[5],
            'unit': it[6], 'price_per_unit': it[7], 'total_net': it[8],
            'tax_category': it[9], 'tax_rate': it[10]})
    return new_id


# Positionen des Beispiels mit Steuersatz je Position: (Bezeichnung, Menge, Einheit, Einzelpreis netto, Steuersatz in Prozent) – netto
# 359,80 €: 310,00 € zu 19 % (Steuer 58,90 €) und 49,80 € zu 7 % (Steuer 3,49 €), brutto 422,19 €
MIXED_ITEMS = (
    ('Beratung Buchhaltung', 2, 'HUR', 95.0, 19),
    ('Fachbuch Einkommensteuer', 1, 'C62', 39.9, 7),
    ('Schulungsunterlagen', 3, 'C62', 3.3, 7),
    ('Reisekosten', 1, 'LS', 120.0, 19),
)


def _mixed_rates_draft(db, invoice_id, number='MS-DEMO-1'):
    """Ein Entwurf mit Positionen zu 19 % und 7 % (``MIXED_ITEMS``) – angelegt über den Weg der Maske: ``handlers.handle_invoice_save`` mit
    ``taxMode: 'mixed'``, so, wie das Formular mit dem Häkchen „Steuersatz je Position“ es schickt. Kunde, eigene Firma, Bankkonto und
    Fristen übernimmt er von der Demo-Rechnung ``invoice_id``, dazu die Leitweg-ID der anderen Beispiele. Liefert die ID."""
    import json
    from server import handlers
    inv = db.get_invoice_by_id(invoice_id)
    payload = {
        'invoiceNumber': number, 'invoiceDate': str(inv[2]), 'deliveryDate': str(inv[25] or inv[2]),
        'customerId': inv[13], 'ownCompanyId': inv[3],
        'bankAccountId': next((a[0] for a in db.fetch_accounts() if a[3] == inv[32]), None),
        'taxRate': 0.19, 'taxMode': 'mixed', 'dueDate': inv[27], 'paymentTerms': inv[26], 'buyerReference': LEITWEG_ID,
        'items': [{'position': position, 'description': description, 'quantity': quantity, 'unit': unit, 'unitPrice': price,
                   'taxRate': rate}
                  for position, (description, quantity, unit, price, rate) in enumerate(MIXED_ITEMS, 1)],
    }
    answer = json.loads(handlers.handle_invoice_save(json.dumps(payload).encode()))
    if not answer.get('success'):
        sys.exit(f'mischsatz: {answer.get("error")}')
    return answer['invoice_id']


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


def _write_ubl(db, invoice_id, label, path):
    """Das UBL der Rechnung nach ``path`` schreiben – nachdem sich gezeigt hat, dass ihm nichts fehlt. Dieselbe Prüfung
    macht die App beim Erzeugen (``generate_invoice_documents``); fehlt etwas, brechen wir ab, statt eine Datei zu
    schreiben, die Mustang abweist (oder, schlimmer, ein Beispiel, das die Prüfung der App nie ausgäbe)."""
    from export.einvoice_model import build_einvoice, einvoice_gaps, gap_texts
    from export.xrechnung_invoice import XRechnungGenerator
    model = build_einvoice(db, db.get_invoice_by_id(invoice_id), db.get_invoice_items(invoice_id))
    gaps = einvoice_gaps(model)
    if gaps:
        sys.exit(f'{label}: es fehlt: {", ".join(gap_texts(gaps))}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(XRechnungGenerator().generate_from_model(model))


def _write_hash_sample(db, invoice_id, path):
    """Nur das PDF einer Rechnung, deren Zahlungshinweis ein ``#`` trägt: Das UBL bliebe ungültig (BR-DE-18), die App legt es
    deshalb nicht an – die CII im PDF ist es nicht. Fehlt der Rechnung etwas anderes, bricht das Werkzeug ab."""
    from export.einvoice_model import HASH_IN_PAYMENT_TERMS_GAP, build_einvoice, einvoice_gaps, gap_texts
    from export.invoice_files import generate_invoice_documents
    model = build_einvoice(db, db.get_invoice_by_id(invoice_id), db.get_invoice_items(invoice_id))
    if gap_texts(einvoice_gaps(model)) != [HASH_IN_PAYMENT_TERMS_GAP]:
        sys.exit(f'zahlungshinweis-raute: erwartet allein die Lücke zum „#“, gefunden: {gap_texts(einvoice_gaps(model))}')
    result = generate_invoice_documents(db, invoice_id, with_xml=False, overwrite_sent=True)
    if result['error']:
        sys.exit(f'zahlungshinweis-raute: {result["error"]}')
    shutil.copyfile(result['pdf_path'], path)


def _day_before(iso_date):
    import datetime
    return (datetime.date.fromisoformat(str(iso_date)[:10]) - datetime.timedelta(days=1)).isoformat()


def _normal_invoice(db):
    """Die Rechnung, aus der die Beispiele entstehen: die jüngste mit 19 % im Kopf – keine Stornorechnung und nie ein Beleg mit Steuersatz
    je Position (``Invoices.MixedTaxRates``): Bei ihm ist der Satz im Kopf nur ein Platzhalter (der höchste Satz der Positionen)."""
    from db.invoices import InvoiceCol, has_mixed_rates
    return next(i for i in db.fetch_invoices()
                if float(i[35] or 0) in (19, 0.19) and i[50] != 'storno' and not has_mixed_rates(i[InvoiceCol.MIXED_TAX_RATES]))


def main(outdir):
    from db import Database
    from export.invoice_files import generate_invoice_documents

    outdir = os.path.abspath(outdir)          # _setup_demo wechselt das Arbeitsverzeichnis
    os.makedirs(outdir, exist_ok=True)
    _setup_demo()
    db = Database()
    normal = _normal_invoice(db)
    with db._get_connection() as c:                      # nur in der temporären DB
        c.execute("UPDATE Invoices SET Status='draft', BuyerRouteID=? WHERE ID=?",
                  (LEITWEG_ID, normal[0]))
    exempt_id = _clone_as_exempt(db, normal[0])
    # Skonto: 2 % bei Zahlung in 7 Tagen; der Zahlungshinweis der Demodaten sagt „ohne Abzug“ und passt dazu nicht
    skonto_id = _clone_for_correction(
        db, normal[0], 'SK-DEMO-1', skonto_days=7, skonto_percent=2,
        payment_terms='Bitte überweisen Sie den Betrag unter Angabe der Rechnungsnummer auf das unten angegebene Konto.')

    # Nur eine Steuernummer, keine USt-IdNr. (die Momentaufnahme gilt vor dem Kontakt): BT-32 und, weil BR-CO-26 es verlangt,
    # dieselbe Nummer als BT-29 – mit Umsatzsteuer und als Kleinunternehmer
    tax_number = '12/345/67890'
    tax_number_id = _clone_for_correction(db, normal[0], 'SN-DEMO-1', seller_vat_id=tax_number)
    tax_number_exempt_id = _clone_as_exempt(db, normal[0], 'SK-DEMO-2', seller_vat_id=tax_number)

    # Steuersatz je Position: Positionen zu 19 % und 7 % – zwei Steueraufschlüsselungen, die Steuer je Satz aus der Nettosumme der Gruppe
    mixed_id = _mixed_rates_draft(db, normal[0])

    for label, iid in (('normalfall', normal[0]), ('kleinunternehmer', exempt_id), ('skonto', skonto_id),
                       ('steuernummer', tax_number_id), ('steuernummer-kleinunternehmer', tax_number_exempt_id),
                       ('mischsatz', mixed_id)):
        result = generate_invoice_documents(db, iid, with_xml=False, overwrite_sent=True)
        if result['error']:
            sys.exit(f'{label}: {result["error"]}')
        shutil.copyfile(result['pdf_path'], os.path.join(outdir, f'zugferd-{label}.pdf'))
        _write_ubl(db, iid, label, os.path.join(outdir, f'xrechnung-{label}.xml'))
    # Ein „#“ im Zahlungshinweis (neben dem Skonto): kein UBL, aber die CII im PDF soll gültig bleiben – das prüft Mustang hier
    hash_id = _clone_for_correction(
        db, normal[0], 'RZ-DEMO-1', skonto_days=7, skonto_percent=2, payment_terms='Bitte Kunden-Nr. #4711# angeben.')
    _write_hash_sample(db, hash_id, os.path.join(outdir, 'zugferd-zahlungshinweis-raute.pdf'))
    corrected_id = _clone_for_correction(db, normal[0])
    result = _correct_delivery_date(db, corrected_id)
    shutil.copyfile(result['pdf_path'], os.path.join(outdir, 'zugferd-berichtigt.pdf'))
    _write_ubl(db, corrected_id, 'berichtigt', os.path.join(outdir, 'xrechnung-berichtigt.xml'))
    storno_source = _clone_for_correction(db, normal[0], 'ST-DEMO-1')
    result, storno_id = _cancel_sample(db, storno_source)
    shutil.copyfile(result['pdf_path'], os.path.join(outdir, 'zugferd-storno.pdf'))
    _write_ubl(db, storno_id, 'storno', os.path.join(outdir, 'xrechnung-storno.xml'))
    replace_source = _clone_for_correction(db, normal[0], 'ER-DEMO-1')
    result, replacement_id = _replacement_sample(db, replace_source)
    shutil.copyfile(result['pdf_path'], os.path.join(outdir, 'zugferd-ersatz.pdf'))
    _write_ubl(db, replacement_id, 'ersatz', os.path.join(outdir, 'xrechnung-ersatz.xml'))
    print('Beispiele in', outdir, sorted(os.listdir(outdir)))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'einvoice-samples')
