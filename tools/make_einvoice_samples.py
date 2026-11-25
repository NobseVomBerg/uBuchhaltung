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
* ``zugferd-satz-5-5.pdf``              – Steuersätze aus den Stammdaten (seit 2.8.0): 19 % und 5,5 % auf einem Beleg (der Eintrag der Stammdaten
                                         mit seiner ``taxRateId`` an der Position), darunter eine Position über 0,09 € netto zu 5,5 %: Die Steuer
                                         der Gruppe ist einstufig gerundet 0,00 € (EN 16931, BR-CO-17) – zweistufig wären es 0,01 €, einen Cent zu viel
* ``zugferd-steuerfrei-gemischt.pdf``   – 19 % und steuerfrei auf einem Beleg: die Aufschlüsselung der steuerfreien Gruppe in Kategorie E, Satz 0, mit dem
                                         Befreiungsgrund aus den Stammdaten (BT-120, ohne Code)
* ``zugferd-steuerfrei.pdf``            – ein ganz steuerfreier Beleg (Eintrag im Kopf, Satz 0): Kategorie E mit Befreiungsgrund (BR-E-10)
* ``zugferd-zeitraum.pdf``               – Leistungszeitraum (seit 2.10.0): die Rechnung nennt statt des Leistungsdatums einen Zeitraum – die CII ``BillingSpecifiedPeriod``
                                         (BG-14, ohne ``ActualDeliverySupplyChainEvent``), im PDF zwei Zeilen im Kopf („Leistungszeitraum:“ und „bis …“)
* ``zugferd-bar.pdf``                   – Zahlungsart bar (Code 10): ``SpecifiedTradeSettlementPaymentMeans`` allein mit dem Code, ohne Konto
* ``zugferd-nicht-festgelegt.pdf``      – Zahlungsart nicht festgelegt (Code 1), ebenso ohne Konto
* ``xrechnung-normalfall.xml``          – UBL-XRechnung zur Normalfall-Rechnung
* ``xrechnung-kleinunternehmer.xml``    – UBL-XRechnung zum Kleinunternehmer-Fall
* ``xrechnung-berichtigt.xml``          – UBL-XRechnung zur berichtigten Fassung (``cbc:Note``)
* ``xrechnung-storno.xml``              – UBL-``CreditNote`` zur Stornorechnung (``cac:BillingReference``)
* ``xrechnung-ersatz.xml``              – UBL-XRechnung zur Ersatzrechnung (``cbc:Note``)
* ``xrechnung-skonto.xml``              – UBL-XRechnung mit der Skonto-Zeile (BR-DE-18) und ihrem Zeilenumbruch
* ``xrechnung-steuernummer.xml``        – UBL-XRechnung zum Fall „nur Steuernummer“ (``TAX`` und ``cac:PartyIdentification``)
* ``xrechnung-steuernummer-kleinunternehmer.xml`` – dasselbe als Kleinunternehmer
* ``xrechnung-mischsatz.xml``           – UBL-XRechnung zur Rechnung mit Steuersatz je Position (zwei ``cac:TaxSubtotal``)
* ``xrechnung-satz-5-5.xml``            – UBL-XRechnung zum Beleg mit 19 % und 5,5 % (zwei ``cac:TaxSubtotal``, die der 5,5 % mit Steuer 0,00)
* ``xrechnung-steuerfrei-gemischt.xml`` – UBL-XRechnung zum Beleg mit 19 % und steuerfrei (``cbc:TaxExemptionReason`` an der Kategorie E)
* ``xrechnung-steuerfrei.xml``          – UBL-XRechnung zum ganz steuerfreien Beleg
* ``xrechnung-zeitraum.xml``            – UBL-XRechnung mit ``cac:InvoicePeriod`` statt ``cac:Delivery``
* ``xrechnung-bar.xml``, ``xrechnung-nicht-festgelegt.xml`` – UBL-XRechnung mit ``cbc:PaymentMeansCode`` 10 bzw. 1, ohne ``cac:PayeeFinancialAccount``

Die drei Beispiele mit Steuersätzen aus den Stammdaten entstehen wie das mit Steuersatz je Position: Das Werkzeug legt die Einträge in seiner
Demo-Datenbank an (``Database.insert_tax_rate``, erfundene Bezeichnungen und Gründe, Erlöskonten des Standard-Kontenrahmens) und speichert die Entwürfe über den Weg
der Maske (``handlers.handle_invoice_save`` mit ``taxRateId``). Keiner der Einträge trägt einen Befreiungscode: Der Grund ist nur Text (BT-120).

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
    ``insert_invoice`` (z. B. die Steuernummer des Verkäufers). Die Positionen tragen die Art ``'E'``, aber nie die Momentaufnahme eines
    Steuersatzes aus den Stammdaten (Satz in Hundertstel Prozent, Grund, Erlöskonto): § 19 schließt sie aus – der Hinweis kommt aus dem Kopf (−1)."""
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
    mit den Sätzen der Positionen mit (nur ``None`` oder ``1``): Sonst entstünde ein Beleg mit zwei Sätzen ohne Kennzeichen.
    Ebenso die Momentaufnahme eines Steuersatzes aus den Stammdaten an den Positionen (Satz in Hundertstel Prozent, Befreiungsgrund,
    Erlöskonto; ``TaxCategory`` trägt die Art): Eine Kopie ist dieselbe Rechnung, nicht eine ohne Eintrag."""
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
            'tax_category': it[9], 'tax_rate': it[10],
            'tax_rate_bp': it[11], 'tax_exempt_reason': it[12], 'revenue_coa_id': it[13]})
    return new_id


# Positionen des Beispiels mit Steuersatz je Position: (Bezeichnung, Menge, Einheit, Einzelpreis netto, Steuersatz in Prozent) – netto
# 359,80 €: 310,00 € zu 19 % (Steuer 58,90 €) und 49,80 € zu 7 % (Steuer 3,49 €), brutto 422,19 €
MIXED_ITEMS = (
    ('Beratung Buchhaltung', 2, 'HUR', 95.0, 19),
    ('Fachbuch Einkommensteuer', 1, 'C62', 39.9, 7),
    ('Schulungsunterlagen', 3, 'C62', 3.3, 7),
    ('Reisekosten', 1, 'LS', 120.0, 19),
)


def _form_draft(db, invoice_id, number, items, label, **head):
    """Ein Entwurf über den Weg der Maske: ``handlers.handle_invoice_save`` mit den Positionen ``items`` (wie das Formular sie schickt) und den Angaben im Kopf
    ``head`` (``taxRate``, ``taxMode``, ``taxRateId``). Kunde, eigene Firma, Bankkonto und Fristen übernimmt er von der Demo-Rechnung ``invoice_id``, dazu die
    Leitweg-ID der anderen Beispiele. Liefert die ID; scheitert das Speichern, bricht das Werkzeug mit ``label`` und dem Grund ab."""
    import json
    from server import handlers
    inv = db.get_invoice_by_id(invoice_id)
    payload = {
        'invoiceNumber': number, 'invoiceDate': str(inv[2]), 'deliveryDate': str(inv[25] or inv[2]),
        'customerId': inv[13], 'ownCompanyId': inv[3],
        'bankAccountId': next((a[0] for a in db.fetch_accounts() if a[3] == inv[32]), None),
        'dueDate': inv[27], 'paymentTerms': inv[26], 'buyerReference': LEITWEG_ID, 'items': items, **head,
    }
    answer = json.loads(handlers.handle_invoice_save(json.dumps(payload).encode()))
    if not answer.get('success'):
        sys.exit(f'{label}: {answer.get("error")}')
    return answer['invoice_id']


def _mixed_rates_draft(db, invoice_id, number='MS-DEMO-1'):
    """Ein Entwurf mit Positionen zu 19 % und 7 % (``MIXED_ITEMS``) – angelegt über den Weg der Maske: ``handlers.handle_invoice_save`` mit
    ``taxMode: 'mixed'``, so, wie das Formular mit dem Häkchen „Steuersatz je Position“ es schickt (``_form_draft``). Liefert die ID."""
    items = [{'position': position, 'description': description, 'quantity': quantity, 'unit': unit, 'unitPrice': price, 'taxRate': rate}
             for position, (description, quantity, unit, price, rate) in enumerate(MIXED_ITEMS, 1)]
    return _form_draft(db, invoice_id, number, items, 'mischsatz', taxRate=0.19, taxMode='mixed')


# Die Einträge der Steuersätze, die die Beispiele mit Stammdaten wählen: erfundene Bezeichnungen und ein erfundener Befreiungsgrund (ohne Code – er ist nur Text,
# BT-120). Die Erlöskonten gehören zum Standard-Kontenrahmen: 4100 (Steuerfreie Umsätze) und 4180 (Erlöse nach Durchschnittssätzen, der Satz 5,5 %).
FREE_REASON = 'Steuerfreie Beispielleistung (erfundener Grund)'
TAX_ENTRIES = (
    ('free', 'Steuerfrei (Beispiel der E-Rechnung)', 'exempt', '0', FREE_REASON, 4100),
    ('reduced', '5,5 % (Beispiel der E-Rechnung)', 'standard', '5,5', None, 4180),
)

# Positionen der Beispiele mit Steuersätzen aus den Stammdaten: (Bezeichnung, Menge, Einheit, Einzelpreis netto, Steuersatz in Prozent, Schlüssel des Eintrags
# der Stammdaten oder ``None`` für eine Position wie bisher – 19 % ohne Eintrag).
# 19 % und 5,5 %: 310,00 € zu 19 % (Steuer 58,90 €) und 0,09 € zu 5,5 %: Die Steuer der Gruppe ist 0,09 × 5,5 % = 0,00495 €, einstufig auf den Cent gerundet
# (EN 16931, BR-CO-17) 0,00 € – über den Zwischenwert in Minor Units (0,0050 €) wären es 0,01 €. Brutto 310,09 € + 58,90 € = 368,99 €.
RATE_5_5_ITEMS = (
    ('Beratung Buchhaltung', 2, 'HUR', 95.0, 19, None),
    ('Reisekosten', 1, 'LS', 120.0, 19, None),
    ('Saatgut-Probe (Beispiel)', 1, 'C62', 0.09, 5.5, 'reduced'),
)
# 19 % und steuerfrei: 190,00 € zu 19 % (Steuer 36,10 €) und 80,00 € steuerfrei; brutto 306,10 €
FREE_MIXED_ITEMS = (
    ('Beratung Buchhaltung', 2, 'HUR', 95.0, 19, None),
    ('Beispielleistung ohne Umsatzsteuer', 1, 'C62', 80.0, 0, 'free'),
)
# ganz steuerfrei: 2 × 60,00 € und 1 × 35,50 €, Steuer 0,00 €, brutto 155,50 €. Den Eintrag wählt der Kopf (``taxRateId`` im Kopf, ohne ``taxMode``: für alle Positionen);
# eine Position nennt hier keinen
FREE_ITEMS = (
    ('Beispielleistung ohne Umsatzsteuer', 2, 'HUR', 60.0, 0, None),
    ('Beispielleistung, Nebenkosten', 1, 'C62', 35.5, 0, None),
)


def _tax_entries(db):
    """Die Einträge der Steuersätze der Beispiele (``TAX_ENTRIES``) in der Demo-Datenbank des Werkzeugs anlegen – über ``Database.insert_tax_rate``, mit den Regeln
    der Stammdaten (die Seite Steuersätze täte dasselbe). Liefert ``{Schlüssel: ID}``; fehlt ein Konto des Kontenrahmens oder lehnt eine Regel ab, bricht das
    Werkzeug ab."""
    from tax_groups import parse_rate_bp
    ids = {}
    for key, label, kind, rate, reason, account in TAX_ENTRIES:
        coa_id = db.get_coa_id_by_account_number(account)
        if coa_id is None:
            sys.exit(f'steuersaetze: Das Erlöskonto {account} fehlt im Kontenrahmen')
        try:
            ids[key] = db.insert_tax_rate(label=label, bp=parse_rate_bp(rate), kind=kind, reason=reason, revenue_coa_id=coa_id)
        except ValueError as refused:                      # ``TaxRateRefused``: fester Satz der Stammdaten
            sys.exit(f'steuersaetze: {label}: {refused}')
    return ids


def _entry_items(rows, entries):
    """Die Positionen eines Beispiels (``RATE_5_5_ITEMS`` …) als Formularzeilen: je Position der Satz, bei einem Eintrag der Stammdaten dazu seine ``taxRateId``."""
    return [{'position': position, 'description': description, 'quantity': quantity, 'unit': unit, 'unitPrice': price, 'taxRate': rate,
             **({'taxRateId': entries[entry]} if entry else {})}
            for position, (description, quantity, unit, price, rate, entry) in enumerate(rows, 1)]


def _entry_drafts(db, invoice_id):
    """Die drei Beispiele mit Steuersätzen aus den Stammdaten: die Einträge anlegen (``_tax_entries``) und die Entwürfe über den Weg der Maske speichern (Kunde, Firma,
    Bank und Fristen von der Demo-Rechnung ``invoice_id``). Liefert ``{Name der Dateien: ID}``: ``satz-5-5`` (19 % und 5,5 %, Satz je Position), ``steuerfrei-gemischt``
    (19 % und steuerfrei, Satz je Position) und ``steuerfrei`` (der Eintrag im Kopf, für alle Positionen)."""
    entries = _tax_entries(db)
    return {
        'satz-5-5': _form_draft(db, invoice_id, 'SA-DEMO-1', _entry_items(RATE_5_5_ITEMS, entries), 'satz-5-5', taxRate=0.19, taxMode='mixed'),
        'steuerfrei-gemischt': _form_draft(db, invoice_id, 'SG-DEMO-1', _entry_items(FREE_MIXED_ITEMS, entries), 'steuerfrei-gemischt',
                                           taxRate=0.19, taxMode='mixed'),
        'steuerfrei': _form_draft(db, invoice_id, 'SF-DEMO-1', _entry_items(FREE_ITEMS, entries), 'steuerfrei', taxRate=0, taxRateId=entries['free']),
    }


# Die Beispiele zu Leistungszeitraum und Zahlungsart (2.10.0): eine Dienstleistung über einen Monat. Keine Karte (48): Die XRechnung verlangt dafür die Ziffern der
# Kartennummer (BR-DE-24), die die App nicht führt – sie legt dann kein UBL an, und das Werkzeug bricht bei Lücken ab.
PERIOD_ITEMS = ({'position': 1, 'description': 'Monatliche Betreuung Buchhaltung', 'quantity': 1, 'unit': 'MON', 'unitPrice': 480.0, 'taxRate': 19},)


def _period_and_means_drafts(db, invoice_id):
    """Die drei Beispiele zu Leistungszeitraum und Zahlungsart als Entwürfe über den Weg der Maske (``_form_draft``, Kunde, Firma, Bank und Fristen von der Demo-Rechnung
    ``invoice_id``): ``zeitraum`` (vom Leistungsdatum der Demo-Rechnung bis 29 Tage später: BG-14 statt Lieferdatum), ``bar`` (Code 10) und ``nicht-festgelegt`` (Code 1) –
    beide ohne Konto in den Zahlungsangaben. Liefert ``{Name der Dateien: ID}``."""
    inv = db.get_invoice_by_id(invoice_id)
    start = str(inv[25] or inv[2])
    items = [dict(item) for item in PERIOD_ITEMS]
    return {
        'zeitraum': _form_draft(db, invoice_id, 'ZR-DEMO-1', items, 'zeitraum', taxRate=0.19, serviceDateEnd=_days_later(start, 29)),
        'bar': _form_draft(db, invoice_id, 'BAR-DEMO-1', items, 'bar', taxRate=0.19, paymentMeans='10'),
        'nicht-festgelegt': _form_draft(db, invoice_id, 'NF-DEMO-1', items, 'nicht-festgelegt', taxRate=0.19, paymentMeans='1'),
    }


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


def _days_later(iso_date, days):
    import datetime
    return (datetime.date.fromisoformat(str(iso_date)[:10]) + datetime.timedelta(days=days)).isoformat()


def _has_tax_snapshot(db, invoice_id):
    """Trägt eine Position des Belegs die Momentaufnahme eines Steuersatzes aus den Stammdaten (``InvoiceItems.TaxRateBp``, Index 11 der Zeile)? Das ist das Merkmal –
    nicht die Art ``'E'`` allein: Die Positionen des Beispiels nach § 19 tragen sie ohne Momentaufnahme."""
    return any(item[11] is not None for item in db.get_invoice_items(invoice_id))


def _normal_invoice(db):
    """Die Rechnung, aus der die Beispiele entstehen: die jüngste mit 19 % im Kopf – keine Stornorechnung, nie ein Beleg mit Steuersatz
    je Position (``Invoices.MixedTaxRates``): Bei ihm ist der Satz im Kopf nur ein Platzhalter (der höchste Satz der Positionen) – und nie ein Beleg, dessen Position
    die Momentaufnahme eines Steuersatzes aus den Stammdaten trägt (er könnte etwa 19 % mit einem eigenen Erlöskonto sein: der Kopf sähe aus wie ein Normalfall)."""
    from db.invoices import InvoiceCol, has_mixed_rates
    return next(i for i in db.fetch_invoices()
                if float(i[35] or 0) in (19, 0.19) and i[50] != 'storno' and not has_mixed_rates(i[InvoiceCol.MIXED_TAX_RATES])
                and not _has_tax_snapshot(db, i[0]))


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

    # Steuersätze aus den Stammdaten (seit 2.8.0), ohne Befreiungscode: 19 % und 5,5 % (mit der Position, an der zweistufiges Runden einen Cent zu viel ergäbe), 19 % und
    # steuerfrei mit Grund (Satz je Position) und ein ganz steuerfreier Beleg (der Eintrag im Kopf)
    entry_drafts = _entry_drafts(db, normal[0])

    # Leistungszeitraum und Zahlungsart (seit 2.10.0): ein Zeitraum statt des Leistungsdatums, bar und nicht festgelegt
    period_drafts = _period_and_means_drafts(db, normal[0])

    for label, iid in (('normalfall', normal[0]), ('kleinunternehmer', exempt_id), ('skonto', skonto_id),
                       ('steuernummer', tax_number_id), ('steuernummer-kleinunternehmer', tax_number_exempt_id),
                       ('mischsatz', mixed_id), *entry_drafts.items(), *period_drafts.items()):
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
